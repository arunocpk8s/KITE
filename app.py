"""KITE – Knowledge Transfer tracker: app factory, start-up checks, home and account pages, CLI commands."""
import logging
import os
import re
import traceback

import click
from flask import Blueprint, Flask, current_app, g, jsonify, render_template
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

import notify
from admin import bp as admin_bp
import sops
from doubts import bp as doubts_bp
from kt import bp as kt_bp
from kt import my_kt, plan_stats, visible_account_or_404
from projects import account_projects
from projects import bp as projects_bp
from nextsteps import next_steps, upcoming_internal_sessions
from reports import bp as reports_bp
from transactions import bp as transactions_bp
from auth import bp as auth_bp
from auth import login_required
from config import DEPARTMENTS, Config
from models import ROLE_LABELS, ROLES, Account, AccountMember, Change, Question, Transaction, User, db, upgrade_schema
from timeutil import format_date, format_ist

MIN_PASSWORD_LENGTH = 8
ACCOUNT_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,19}$")

csrf = CSRFProtect()
bp = Blueprint("main", __name__)


def check_settings(config):
    """Refuse to start with settings that are unsafe for a hosted or production copy."""
    if not config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be set in the app settings (environment variables).")
    uri = config.get("SQLALCHEMY_DATABASE_URI") or ""
    if config.get("APP_ENV") == "production":
        if not uri.startswith("postgresql"):
            raise RuntimeError(
                "APP_ENV=production needs its own PostgreSQL database: set DATABASE_URL (or PGHOST, PGUSER, "
                "PGPASSWORD, PGDATABASE). SQLite is not allowed in production."
            )
        if not (config.get("APP_BASE_URL") or "").startswith("https://"):
            raise RuntimeError("APP_BASE_URL must be the https:// address of the production site.")
    if config.get("SSO_ENABLED"):
        missing = [k for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET") if not config.get(k)]
        if missing:
            raise RuntimeError("SSO_ENABLED is true but these settings are empty: " + ", ".join(missing) + ".")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    check_settings(app.config)
    if app.config["TRUST_PROXY"]:
        # One proxy in front (Azure App Service front end or Vercel): trust its client IP and https scheme.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass  # read-only file system (e.g. Vercel); only the local SQLite file lives there

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(kt_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(sops.bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(doubts_bp)
    app.register_blueprint(reports_bp)
    app.jinja_env.filters["ist"] = format_ist
    app.jinja_env.filters["date"] = format_date

    with app.app_context():
        db.create_all()
        upgrade_schema()

    # Company logo: drop logo.svg / logo.png / logo.jpg into static/ to show it in the header.
    logo = next(
        (n for n in ("logo.svg", "logo.png", "logo.jpg") if os.path.exists(os.path.join(app.static_folder, n))),
        None,
    )

    @app.context_processor
    def inject_branding():
        return {
            "app_name": app.config["APP_NAME"],
            "logo_file": logo,
            "role_labels": ROLE_LABELS,
            # Hosted copy without a real database: data is lost on restart, so say so on every page.
            "temporary_database": bool(os.environ.get("VERCEL"))
            and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"),
        }

    register_cli(app)
    sops.register_cli(app)
    return app


def register_cli(app):
    @app.cli.command("resend-failed")
    def resend_failed():
        """Retry sending emails that previously failed."""
        sent, pending = notify.resend_pending()
        click.echo(f"Resent {sent} email(s); {pending} still pending.")

    @app.cli.command("test-email")
    @click.argument("to")
    def test_email(to):
        """Send a test email to TO to check the SMTP settings."""
        error = notify.send_test(to)
        if error:
            raise click.ClickException(f"Sending failed: {error}")
        click.echo(f"Test email sent to {to}.")

    @app.cli.command("set-email")
    @click.argument("username")
    @click.argument("email")
    def set_email(username, email):
        """Change the email address of USERNAME."""
        user = _user_or_fail(username)
        user.email = email.strip()
        db.session.commit()
        click.echo(f"Email for '{username}' set to {user.email}.")

    @app.cli.command("set-role")
    @click.argument("username")
    @click.argument("role", type=click.Choice(ROLES))
    def set_role(username, role):
        """Change the role of USERNAME."""
        user = _user_or_fail(username)
        user.role = role
        db.session.commit()
        click.echo(f"'{username}' is now {ROLE_LABELS[role]}.")

    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--name", "full_name", required=True, help="Full name shown in the app.")
    @click.option("--email", required=True)
    @click.option("--role", type=click.Choice(ROLES), default="member", show_default=True)
    @click.option("--department", default="", help=f"One of: {', '.join(DEPARTMENTS)}.")
    @click.password_option()
    def create_user(username, full_name, email, role, department, password):
        """Create a user. The password is asked for and never shown."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException(f"User '{username}' already exists.")
        if department and department not in DEPARTMENTS:
            raise click.ClickException(f"Department must be one of: {', '.join(DEPARTMENTS)}.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        user = User(username=username, full_name=full_name, email=email, role=role, department=department)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created {ROLE_LABELS[role]} '{username}'.")

    @app.cli.command("create-account")
    @click.argument("code")
    @click.option("--name", required=True, help="Client account name.")
    @click.option("--description", default="")
    def create_account(code, name, description):
        """Create a client account with a short CODE (e.g. ACME)."""
        code = code.strip().upper()
        if not ACCOUNT_CODE_RE.match(code):
            raise click.ClickException("CODE: 2-20 characters, letters, numbers, dash or underscore.")
        if Account.query.filter_by(code=code).first():
            raise click.ClickException(f"Account '{code}' already exists.")
        db.session.add(Account(code=code, name=name.strip(), description=description.strip()))
        db.session.commit()
        click.echo(f"Created account {code} ({name}).")

    @app.cli.command("add-member")
    @click.argument("code")
    @click.argument("username")
    def add_member(code, username):
        """Give USERNAME access to account CODE."""
        account = Account.query.filter_by(code=code.strip().upper()).first()
        if account is None:
            raise click.ClickException(f"No account '{code}'.")
        user = _user_or_fail(username)
        if not any(m.account_id == account.id for m in user.memberships):
            db.session.add(AccountMember(account=account, user=user))
            db.session.commit()
        click.echo(f"'{username}' is a member of {account.code}.")


def _user_or_fail(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        raise click.ClickException(f"No user named '{username}'.")
    return user


# --- routes ------------------------------------------------------------------------------

@bp.route("/healthz")
def healthz():
    """For Azure App Service Health check: 200 when the app and its database answer. No login needed."""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        current_app.logger.exception("Health check: database not reachable")
        return jsonify(status="error", database="unreachable"), 503
    return jsonify(status="ok", database="ok")


@bp.route("/")
@login_required
def dashboard():
    accounts = g.user.visible_accounts().all()
    topics, upcoming = my_kt(g.user)
    return render_template("dashboard.html", accounts=[(a, plan_stats(a)) for a in accounts],
                           my_topics=topics, upcoming=upcoming, steps=next_steps(g.user),
                           internal=upcoming_internal_sessions(g.user))


@bp.route("/accounts/<code>")
@login_required
def account_detail(code):
    account = visible_account_or_404(code)
    return render_template(
        "account_detail.html", account=account, members=account.member_users().all(),
        can_manage=g.user.can_manage(account), stats=plan_stats(account), projects=account_projects(account),
        sop_stats=sops.sop_stats(account), phase4=account_counts(account),
    )


def account_counts(account):
    return {
        "transactions": Transaction.query.filter_by(account_id=account.id, active=True).count(),
        "changes": Change.query.filter_by(account_id=account.id, status="PUBLISHED").count(),
        "open_doubts": Question.query.filter(Question.account_id == account.id, Question.status != "RESOLVED").count(),
    }


def safe_create_app():
    """create_app(), or a minimal app that explains why start-up failed (credentials in URLs masked)."""
    try:
        return create_app()
    except Exception as exc:  # noqa: BLE001 - report any start-up failure
        logging.getLogger(__name__).error("KITE failed to start:\n%s", traceback.format_exc())
        reason = re.sub(r"://[^/\s@]+@", "://***@", f"{type(exc).__name__}: {exc}")[:500]

        def startup_error(environ, start_response):
            body = (
                "KITE could not start on this deployment.\n\n"
                f"Reason: {reason}\n\n"
                "Check the app settings (environment variables) of this deployment, such as SECRET_KEY, "
                "DATABASE_URL and the AZURE_* sign-in settings, then restart or redeploy.\n"
            ).encode()
            start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8"),
                                                       ("Content-Length", str(len(body)))])
            return [body]

        return startup_error
