"""Login (password and Microsoft SSO), logout and access-control decorators."""
import logging
from functools import wraps

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for,
)
from sqlalchemy import func

import sso
from models import User, add_admin_event, db
from timeutil import utcnow

log = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_current_user():
    user_id = session.get("user_id")
    g.user = db.session.get(User, user_id) if user_id else None
    if g.user is not None and not g.user.active:
        session.clear()
        g.user = None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Login required"), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _safe_next(target):
    # Only allow redirects to local paths.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.dashboard")


def _break_glass(username):
    return username.lower() in {u.lower() for u in current_app.config["BREAK_GLASS_USERS"]}


def _password_login_allowed(username):
    """Everyone while LOCAL_LOGIN_ENABLED; afterwards only the break-glass accounts."""
    return current_app.config["LOCAL_LOGIN_ENABLED"] or _break_glass(username)


def _start_session(user, method):
    session.clear()
    session.permanent = True  # ends after SESSION_HOURS
    session["user_id"] = user.id
    session["auth_method"] = method


def _render_login(username="", next_url="", status=200):
    return render_template(
        "login.html", username=username, next=next_url, sso_enabled=sso.enabled(),
        local_login=current_app.config["LOCAL_LOGIN_ENABLED"],
    ), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "")
        user = User.query.filter(func.lower(User.username) == username.lower()).first() if username else None
        if user and user.active and _password_login_allowed(user.username) and user.check_password(password):
            _start_session(user, "local")
            user.auth_source = "local"
            user.last_login_at = utcnow()
            if not current_app.config["LOCAL_LOGIN_ENABLED"]:
                add_admin_event(user, "BREAK_GLASS_LOGIN", user.username, request.remote_addr)
                log.warning("Break-glass password login by '%s' from %s", user.username, request.remote_addr)
            db.session.commit()
            log.info("Login succeeded for user '%s'", user.username)
            return redirect(_safe_next(next_url))
        log.info("Login failed for user '%s'", username)
        flash("Invalid username or password.", "error")
        return _render_login(username, next_url, 401)
    if g.user:
        return redirect(url_for("main.dashboard"))
    return _render_login(next_url=request.args.get("next", ""))


@bp.route("/auth/microsoft")
def sso_start():
    if not sso.enabled():
        abort(404)
    flow = sso.start_flow()
    session["sso_flow"] = flow
    session["sso_next"] = request.args.get("next", "")
    return redirect(flow["auth_uri"])


@bp.route("/auth/callback")
def sso_callback():
    if not sso.enabled():
        abort(404)
    flow = session.pop("sso_flow", None)
    next_url = session.pop("sso_next", "")
    if flow is None:
        flash("Your sign-in expired. Please try again.", "error")
        return redirect(url_for("auth.login"))
    try:
        result = sso.finish_flow(flow, request.args)
    except ValueError as exc:  # state mismatch or a replayed/forged callback
        log.warning("SSO callback rejected: %s", exc)
        flash("Sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("auth.login"))
    if "error" in result:
        log.warning("SSO sign-in failed: %s %s", result.get("error"), result.get("error_description", "")[:300])
        flash("Microsoft sign-in did not complete. Please try again.", "error")
        return redirect(url_for("auth.login"))
    department = ""
    if current_app.config["SSO_READ_DEPARTMENT"] and result.get("access_token"):
        department = sso.read_department(result["access_token"])
    try:
        user = sso.user_from_claims(result.get("id_token_claims") or {}, department)
    except sso.SignInRefused as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    _start_session(user, "sso")
    log.info("SSO login succeeded for user '%s'", user.username)
    return redirect(_safe_next(next_url))


@bp.route("/logout")
def logout():
    signed_in_with_microsoft = session.get("auth_method") == "sso"
    session.clear()
    if signed_in_with_microsoft and sso.enabled():
        # Also end the Microsoft session, then come back to the KITE login page.
        return redirect(sso.logout_url(current_app.config["APP_BASE_URL"] + url_for("auth.login")))
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
