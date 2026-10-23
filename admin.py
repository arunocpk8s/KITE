"""Admin console: users, client accounts and members, mail server, email outbox. Admins only."""
import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

import notify
from config import DEPARTMENTS
from models import (
    ROLE_LABELS, ROLES, Account, AccountMember, AdminEvent, EmailOutbox, Setting, User, add_admin_event, db,
    delete_setting, set_setting,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")

MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
ACCOUNT_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,19}$")
OUTBOX_STATUSES = ("PENDING", "SENT", "CANCELLED")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
SERVER_KEYS = notify.SMTP_SETTING_KEYS + (notify.SMTP_PASSWORD_KEY,)
USER_FIELDS = ("full_name", "email", "role", "department")


@bp.before_request
def require_admin():
    if g.user is None:
        return redirect(url_for("auth.login", next=request.path))
    if not g.user.is_admin:
        abort(403)


# --- helpers ---------------------------------------------------------------------------------
def _active_admins():
    return User.query.filter_by(role="admin", active=True).count()


def _read_user_form(form):
    data = {f: " ".join(form.get(f, "").split()) for f in USER_FIELDS}
    data["role"] = data["role"].lower()
    data["account_ids"] = sorted({int(v) for v in form.getlist("accounts") if v.isdigit()})
    return data


def _validate_user(data):
    errors = {}
    if not data["full_name"]:
        errors["full_name"] = "Enter the full name."
    elif len(data["full_name"]) > 120:
        errors["full_name"] = "At most 120 characters."
    if not EMAIL_RE.match(data["email"]) or len(data["email"]) > 200:
        errors["email"] = "Enter a valid email address."
    if data["role"] not in ROLES:
        errors["role"] = "Choose a role."
    if data["department"] and data["department"] not in DEPARTMENTS:
        errors["department"] = "Choose a department from the list."
    known = {a.id for a in Account.query.filter(Account.id.in_(data["account_ids"]))} if data["account_ids"] else set()
    if set(data["account_ids"]) - known:
        errors["accounts"] = "Unknown account."
    return errors


def _validate_password(password, confirm):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords do not match."
    return None


def _render_user_form(user, data, errors, status=200):
    return render_template(
        "admin/user_form.html", user=user, form=data, errors=errors, roles=ROLES, departments=DEPARTMENTS,
        accounts=Account.query.order_by(Account.active.desc(), Account.name).all(),
        sso_enabled=current_app.config["SSO_ENABLED"],
    ), status


def _set_memberships(user, account_ids):
    """Make the user's accounts exactly account_ids. Returns (added codes, removed codes)."""
    current = {m.account_id: m for m in user.memberships}
    added, removed = [], []
    for account_id, membership in current.items():
        if account_id not in account_ids:
            removed.append(membership.account.code)
            db.session.delete(membership)
    for account in Account.query.filter(Account.id.in_(account_ids)) if account_ids else []:
        if account.id not in current:
            db.session.add(AccountMember(account=account, user=user))
            added.append(account.code)
    return added, removed


# --- overview --------------------------------------------------------------------------------
@bp.route("/")
def overview():
    by_role = dict(db.session.query(User.role, func.count(User.id)).filter(User.active.is_(True)).group_by(User.role).all())
    by_mail = dict(db.session.query(EmailOutbox.status, func.count(EmailOutbox.id)).group_by(EmailOutbox.status).all())
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    accounts_without_lead = [
        a for a in accounts
        if not User.query.join(AccountMember).filter(
            AccountMember.account_id == a.id, User.role == "kt_lead", User.active.is_(True)).first()
    ]
    return render_template(
        "admin/overview.html",
        by_role=by_role,
        inactive=User.query.filter_by(active=False).count(),
        account_count=len(accounts),
        by_mail=by_mail,
        accounts_without_lead=accounts_without_lead,
        events=AdminEvent.query.order_by(AdminEvent.id.desc()).limit(30).all(),
    )


# --- users -----------------------------------------------------------------------------------
@bp.route("/users")
def users():
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    account = request.args.get("account", "")
    q = request.args.get("q", "").strip()
    query = User.query
    if role in ROLES:
        query = query.filter(User.role == role)
    if status == "active":
        query = query.filter(User.active.is_(True))
    elif status == "inactive":
        query = query.filter(User.active.is_(False))
    if account.isdigit():
        query = query.join(AccountMember).filter(AccountMember.account_id == int(account))
    if q:
        like = f"%{q}%"
        query = query.filter(User.username.ilike(like) | User.full_name.ilike(like) | User.email.ilike(like))
    return render_template(
        "admin/users.html",
        users=query.order_by(User.active.desc(), User.role, User.full_name).all(),
        roles=ROLES, accounts=Account.query.order_by(Account.name).all(),
        filters={"role": role, "status": status, "account": account, "q": q},
    )


@bp.route("/users/new", methods=["GET", "POST"])
def new_user():
    if request.method == "GET":
        preset = request.args.get("account", "")
        return _render_user_form(None, {"username": "", "full_name": "", "email": "", "role": "member",
                                        "department": "", "account_ids": [int(preset)] if preset.isdigit() else []}, {})
    data = _read_user_form(request.form)
    data["username"] = request.form.get("username", "").strip()
    errors = _validate_user(data)
    if not USERNAME_RE.match(data["username"]):
        errors["username"] = "3-40 characters: letters, numbers, dot, dash or underscore."
    elif User.query.filter(func.lower(User.username) == data["username"].lower()).first():
        errors["username"] = "This username is already taken."
    password = request.form.get("password", "")
    # With SSO the person signs in with Microsoft (linked by email), so a password is optional.
    sso_only = current_app.config["SSO_ENABLED"] and not password and not request.form.get("password2")
    password_error = None if sso_only else _validate_password(password, request.form.get("password2", ""))
    if password_error:
        errors["password"] = password_error
    if errors:
        return _render_user_form(None, data, errors, 400)

    user = User(username=data["username"], full_name=data["full_name"], email=data["email"],
                department=data["department"], role=data["role"], active=True)
    if not sso_only:
        user.set_password(password)
    db.session.add(user)
    added, _ = _set_memberships(user, data["account_ids"])
    detail = f"role {ROLE_LABELS[user.role]}" + (f", accounts {', '.join(added)}" if added else "") \
        + (", Microsoft sign-in" if sso_only else "")
    add_admin_event(g.user, "USER_CREATED", user.username, detail)
    db.session.commit()
    current_app.logger.info("Admin %s created user %s", g.user.username, user.username)
    flash(f"User '{user.username}' created.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def edit_user(user_id):
    user = db.get_or_404(User, user_id)
    if request.method == "GET":
        data = {f: getattr(user, f) or "" for f in USER_FIELDS}
        data.update(username=user.username, account_ids=sorted(m.account_id for m in user.memberships))
        return _render_user_form(user, data, {})

    data = _read_user_form(request.form)
    data["username"] = user.username
    errors = _validate_user(data)
    losing_admin = user.role == "admin" and data["role"] != "admin"
    if losing_admin and user.id == g.user.id:
        errors["role"] = "You cannot remove your own admin role."
    elif losing_admin and user.active and _active_admins() <= 1:
        errors["role"] = "This is the last active admin; add another admin first."
    if errors:
        return _render_user_form(user, data, errors, 400)

    changes = []
    for field in USER_FIELDS:
        old, new = getattr(user, field) or "", data[field]
        if old != new:
            changes.append(f"{field}: {old or '(none)'} -> {new or '(none)'}")
            setattr(user, field, new)
    added, removed = _set_memberships(user, data["account_ids"])
    if added:
        changes.append("added to " + ", ".join(added))
    if removed:
        changes.append("removed from " + ", ".join(removed))
    if changes:
        add_admin_event(g.user, "USER_UPDATED", user.username, "; ".join(changes))
        db.session.commit()
        flash(f"User '{user.username}' updated.", "success")
    else:
        flash("No changes.", "info")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@bp.route("/users/<int:user_id>/password", methods=["POST"])
def reset_password(user_id):
    user = db.get_or_404(User, user_id)
    error = _validate_password(request.form.get("password", ""), request.form.get("password2", ""))
    if error:
        flash(error, "error")
        return redirect(url_for("admin.edit_user", user_id=user.id))
    user.set_password(request.form["password"])
    add_admin_event(g.user, "PASSWORD_RESET", user.username)
    db.session.commit()
    current_app.logger.info("Admin %s reset the password of %s", g.user.username, user.username)
    flash(f"Password for '{user.username}' changed. Tell them the new password securely.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@bp.route("/users/<int:user_id>/active", methods=["POST"])
def set_active(user_id):
    user = db.get_or_404(User, user_id)
    activate = request.form.get("active") == "1"
    if not activate:
        if user.id == g.user.id:
            flash("You cannot deactivate your own account.", "error")
            return redirect(url_for("admin.edit_user", user_id=user.id))
        if user.role == "admin" and user.active and _active_admins() <= 1:
            flash("This is the last active admin; add another admin first.", "error")
            return redirect(url_for("admin.edit_user", user_id=user.id))
    if user.active != activate:
        user.active = activate
        add_admin_event(g.user, "USER_ACTIVATED" if activate else "USER_DEACTIVATED", user.username)
        db.session.commit()
    flash(f"User '{user.username}' is now {'active' if activate else 'deactivated and signed out'}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


# --- client accounts -------------------------------------------------------------------------
def _read_account_form(form):
    return {
        "code": form.get("code", "").strip().upper(),
        "name": " ".join(form.get("name", "").split()),
        "description": form.get("description", "").strip(),
    }


def _validate_account(data, account=None):
    errors = {}
    if account is None:
        if not ACCOUNT_CODE_RE.match(data["code"]):
            errors["code"] = "2-20 characters: capital letters, numbers, dash or underscore (e.g. ACME)."
        elif Account.query.filter_by(code=data["code"]).first():
            errors["code"] = "This code is already used."
    if not data["name"] or len(data["name"]) > 120:
        errors["name"] = "Enter the client account name (up to 120 characters)."
    if len(data["description"]) > 2000:
        errors["description"] = "At most 2,000 characters."
    return errors


def _render_account_form(account, data, errors, status=200):
    members = account.member_users().all() if account else []
    member_ids = {u.id for u in members}
    candidates = [u for u in User.query.filter_by(active=True).order_by(User.full_name) if u.id not in member_ids]
    return render_template("admin/account_form.html", account=account, form=data, errors=errors,
                           members=members, candidates=candidates), status


@bp.route("/accounts")
def accounts():
    rows = (db.session.query(Account, func.count(AccountMember.id))
            .outerjoin(AccountMember).group_by(Account.id).order_by(Account.active.desc(), Account.name).all())
    return render_template("admin/accounts.html", rows=rows)


@bp.route("/accounts/new", methods=["GET", "POST"])
def new_account():
    if request.method == "GET":
        return _render_account_form(None, {"code": "", "name": "", "description": ""}, {})
    data = _read_account_form(request.form)
    errors = _validate_account(data)
    if errors:
        return _render_account_form(None, data, errors, 400)
    account = Account(code=data["code"], name=data["name"], description=data["description"], active=True)
    db.session.add(account)
    add_admin_event(g.user, "ACCOUNT_CREATED", account.code, account.name)
    db.session.commit()
    flash(f"Account {account.code} created. Add its members below.", "success")
    return redirect(url_for("admin.edit_account", account_id=account.id))


@bp.route("/accounts/<int:account_id>", methods=["GET", "POST"])
def edit_account(account_id):
    account = db.get_or_404(Account, account_id)
    if request.method == "GET":
        return _render_account_form(account, {"code": account.code, "name": account.name,
                                              "description": account.description}, {})
    data = _read_account_form(request.form)
    data["code"] = account.code
    errors = _validate_account(data, account)
    if errors:
        return _render_account_form(account, data, errors, 400)
    changes = [f"{f}: {getattr(account, f) or '(none)'} -> {data[f] or '(none)'}"
               for f in ("name", "description") if getattr(account, f) != data[f]]
    if changes:
        account.name, account.description = data["name"], data["description"]
        add_admin_event(g.user, "ACCOUNT_UPDATED", account.code, "; ".join(changes))
        db.session.commit()
        flash(f"Account {account.code} updated.", "success")
    else:
        flash("No changes.", "info")
    return redirect(url_for("admin.edit_account", account_id=account.id))


@bp.route("/accounts/<int:account_id>/active", methods=["POST"])
def set_account_active(account_id):
    account = db.get_or_404(Account, account_id)
    activate = request.form.get("active") == "1"
    if account.active != activate:
        account.active = activate
        add_admin_event(g.user, "ACCOUNT_ACTIVATED" if activate else "ACCOUNT_DEACTIVATED", account.code)
        db.session.commit()
    flash(f"Account {account.code} is now {'active' if activate else 'deactivated (hidden from members)'}.", "success")
    return redirect(url_for("admin.edit_account", account_id=account.id))


@bp.route("/accounts/<int:account_id>/members", methods=["POST"])
def add_account_member(account_id):
    account = db.get_or_404(Account, account_id)
    user_id = request.form.get("user_id", "")
    user = db.session.get(User, int(user_id)) if user_id.isdigit() else None
    if user is None:
        flash("Choose a person to add.", "error")
    elif any(m.account_id == account.id for m in user.memberships):
        flash(f"{user.full_name} is already a member.", "info")
    else:
        db.session.add(AccountMember(account=account, user=user))
        add_admin_event(g.user, "MEMBER_ADDED", account.code, user.username)
        db.session.commit()
        flash(f"{user.full_name} added to {account.code}.", "success")
    return redirect(url_for("admin.edit_account", account_id=account.id))


@bp.route("/accounts/<int:account_id>/members/<int:user_id>/remove", methods=["POST"])
def remove_account_member(account_id, user_id):
    account = db.get_or_404(Account, account_id)
    membership = AccountMember.query.filter_by(account_id=account.id, user_id=user_id).first_or_404()
    username = membership.user.username
    db.session.delete(membership)
    add_admin_event(g.user, "MEMBER_REMOVED", account.code, username)
    db.session.commit()
    flash(f"{username} removed from {account.code}.", "success")
    return redirect(url_for("admin.edit_account", account_id=account.id))


# --- mail server -----------------------------------------------------------------------------
def _smtp_form_values():
    s = notify.smtp_settings()
    return {"SMTP_HOST": s["host"], "SMTP_PORT": str(s["port"]), "SMTP_USE_TLS": s["use_tls"],
            "SMTP_USER": s["user"], "SMTP_FROM": s["sender"], "APP_BASE_URL": s["base_url"],
            "password_status": s["password_status"]}


def _render_mail(status=200, smtp=None, smtp_errors=None):
    saved = {row.key for row in Setting.query.filter(Setting.key.in_(SERVER_KEYS))}
    return render_template(
        "admin/mail.html", smtp=smtp or _smtp_form_values(), smtp_errors=smtp_errors or {}, smtp_saved=bool(saved),
    ), status


@bp.route("/mail")
def mail():
    return _render_mail()


@bp.route("/mail/server", methods=["POST"])
def mail_server():
    form = request.form
    values = {
        "SMTP_HOST": form.get("SMTP_HOST", "").strip(),
        "SMTP_PORT": form.get("SMTP_PORT", "").strip(),
        "SMTP_USE_TLS": form.get("SMTP_USE_TLS") == "1",
        "SMTP_USER": form.get("SMTP_USER", "").strip(),
        "SMTP_FROM": form.get("SMTP_FROM", "").strip(),
        "APP_BASE_URL": form.get("APP_BASE_URL", "").strip().rstrip("/"),
    }
    password = form.get("SMTP_PASSWORD", "")
    clear_password = form.get("clear_password") == "1"
    errors = {}
    if values["SMTP_HOST"] and not HOST_RE.match(values["SMTP_HOST"]):
        errors["SMTP_HOST"] = "Enter a host name such as smtp.gmail.com, or leave empty to stop sending."
    if not values["SMTP_PORT"].isdigit() or not 1 <= int(values["SMTP_PORT"]) <= 65535:
        errors["SMTP_PORT"] = "Enter a port number (usually 25 or 587)."
    if len(values["SMTP_USER"]) > 200:
        errors["SMTP_USER"] = "At most 200 characters."
    if not EMAIL_RE.match(values["SMTP_FROM"]):
        errors["SMTP_FROM"] = "Enter the sender email address."
    if not re.match(r"^https?://[^\s/]+", values["APP_BASE_URL"]):
        errors["APP_BASE_URL"] = "Enter the address people use to open KITE, e.g. https://kite.company.local"
    if len(password) > 500:
        errors["SMTP_PASSWORD"] = "Password is too long."
    if errors:
        smtp = dict(values, password_status=notify.smtp_settings()["password_status"])
        return _render_mail(400, smtp=smtp, smtp_errors=errors)

    current = _smtp_form_values()
    changes = []
    for key in notify.SMTP_SETTING_KEYS:
        new = values[key]
        if new != current[key]:
            set_setting(key, ("true" if new else "false") if key == "SMTP_USE_TLS" else new, g.user)
            changes.append(f"{key}: {current[key] or '(none)'} -> {new or '(none)'}")
    if clear_password:
        set_setting(notify.SMTP_PASSWORD_KEY, "", g.user)
        changes.append("SMTP password removed")
    elif password:
        set_setting(notify.SMTP_PASSWORD_KEY, notify.encrypt_secret(password), g.user)
        changes.append("SMTP password changed")
    if changes:
        add_admin_event(g.user, "MAIL_SERVER_UPDATED", "mail server", "; ".join(changes))
        db.session.commit()
        current_app.logger.info("Admin %s updated the mail server settings", g.user.username)
        flash("Mail server settings saved. Send a test email to check them.", "success")
    else:
        flash("No changes.", "info")
    return redirect(url_for("admin.mail"))


@bp.route("/mail/server/reset", methods=["POST"])
def mail_server_reset():
    for key in SERVER_KEYS:
        delete_setting(key)
    add_admin_event(g.user, "MAIL_SERVER_RESET", "mail server", "using .env values again")
    db.session.commit()
    flash("Mail server settings reset to the .env values.", "success")
    return redirect(url_for("admin.mail"))


@bp.route("/mail/test", methods=["POST"])
def test_mail():
    to = request.form.get("to", "").strip()
    if not EMAIL_RE.match(to):
        flash("Enter a valid email address for the test.", "error")
        return redirect(url_for("admin.mail"))
    error = notify.send_test(to)
    add_admin_event(g.user, "TEST_EMAIL", to, f"failed: {error}" if error else "sent")
    db.session.commit()
    if error:
        flash(f"Test email failed: {error}", "error")
    else:
        flash(f"Test email sent to {to}.", "success")
    return redirect(url_for("admin.mail"))


# --- email outbox ----------------------------------------------------------------------------
@bp.route("/outbox")
def outbox():
    status = request.args.get("status", "").upper()
    query = EmailOutbox.query
    if status in OUTBOX_STATUSES:
        query = query.filter(EmailOutbox.status == status)
    counts = dict(db.session.query(EmailOutbox.status, func.count(EmailOutbox.id)).group_by(EmailOutbox.status).all())
    return render_template(
        "admin/outbox.html",
        emails=query.order_by(EmailOutbox.id.desc()).limit(200).all(),
        status=status, statuses=OUTBOX_STATUSES, counts=counts,
    )


def _pending_or_400(email_id):
    entry = db.get_or_404(EmailOutbox, email_id)
    if entry.status != "PENDING":
        abort(400, description="Only pending emails can be retried or cancelled.")
    return entry


@bp.route("/outbox/<int:email_id>/retry", methods=["POST"])
def retry_email(email_id):
    entry = _pending_or_400(email_id)
    sent = notify.retry(entry)
    add_admin_event(g.user, "EMAIL_RETRIED", f"email #{entry.id}", "sent" if sent else f"failed: {entry.last_error}")
    db.session.commit()
    flash(f"Email #{entry.id} sent." if sent else f"Email #{entry.id} failed again: {entry.last_error}",
          "success" if sent else "error")
    return redirect(url_for("admin.outbox", status=request.args.get("status", "")))


@bp.route("/outbox/<int:email_id>/cancel", methods=["POST"])
def cancel_email(email_id):
    entry = _pending_or_400(email_id)
    entry.status = "CANCELLED"
    add_admin_event(g.user, "EMAIL_CANCELLED", f"email #{entry.id}", entry.subject)
    db.session.commit()
    flash(f"Email #{entry.id} cancelled.", "success")
    return redirect(url_for("admin.outbox", status=request.args.get("status", "")))


@bp.route("/outbox/resend-all", methods=["POST"])
def resend_all():
    sent, pending = notify.resend_pending()
    add_admin_event(g.user, "EMAILS_RESENT", "outbox", f"sent {sent}, still pending {pending}")
    db.session.commit()
    flash(f"Resent {sent} email(s); {pending} still pending.", "success" if not pending else "error")
    return redirect(url_for("admin.outbox"))
