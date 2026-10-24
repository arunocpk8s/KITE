"""Configuration read from environment variables (and an optional .env file)."""
import logging
import os
import re
import secrets
import tempfile
from datetime import timedelta
from urllib.parse import quote

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DEPARTMENTS = tuple(
    d.strip() for d in (os.environ.get("DEPARTMENTS") or "DPS,SSG,SSI,FSI,CYBER").split(",") if d.strip()
)


def _bool(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


# Vercel sets VERCEL=1; Azure App Service sets WEBSITE_SITE_NAME.
ON_VERCEL = bool(os.environ.get("VERCEL"))
ON_AZURE = bool(os.environ.get("WEBSITE_SITE_NAME"))
# development (default), test (e.g. the Vercel copy) or production. Production refuses unsafe settings.
APP_ENV = (os.environ.get("APP_ENV") or "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"


def _hosted_postgres_url():
    """Vercel storage may add a prefix (e.g. STORAGE_DATABASE_URL); find any Postgres URL variable."""
    for name in sorted(os.environ):
        if name.endswith(("DATABASE_URL", "POSTGRES_URL")) and "UNPOOLED" not in name:
            value = os.environ[name]
            if value.startswith(("postgres://", "postgresql://")):
                return value
    return None


def _url_from_parts():
    """Build a URL from PGHOST/PGUSER/PGPASSWORD/PGDATABASE, so a password with @ or : needs no escaping."""
    host = os.environ.get("PGHOST", "").strip()
    if not host:
        return None
    user = quote(os.environ.get("PGUSER", ""), safe="")
    password = quote(os.environ.get("PGPASSWORD", ""), safe="")
    port = os.environ.get("PGPORT", "").strip() or "5432"
    name = os.environ.get("PGDATABASE", "").strip() or "kite"
    sslmode = os.environ.get("PGSSLMODE", "").strip() or "require"
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def _clean_url(value):
    """Undo copy-paste slips: quotes, <brackets>, a 'DATABASE_URL=' prefix or a whole `psql '...'` command."""
    value = (value or "").strip()
    found = re.search(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"<>]+", value)
    if found:
        return found.group(0)
    if "=" in value.split("://", 1)[0]:
        value = value.split("=", 1)[1].strip()
    value = value.strip("'\"<> ")
    if value and "://" not in value:
        raise RuntimeError(
            f"DATABASE_URL is not a database URL (it starts with {value[:12]!r}). "
            "Copy the value that starts with postgresql:// from Vercel -> Storage -> .env.local."
        )
    return value


def _database_url():
    url = _clean_url(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")) or _url_from_parts()
    if not url and ON_VERCEL:
        url = _hosted_postgres_url()
    if not url and ON_VERCEL:
        # Vercel's disk is read-only except /tmp, which is wiped often: usable only until Postgres is connected.
        logging.getLogger(__name__).warning("DATABASE_URL is not set; using a temporary SQLite database.")
        return "sqlite:///" + os.path.join(tempfile.gettempdir(), "kite.db").replace("\\", "/")
    if not url and IS_PRODUCTION:
        # Production must have its own PostgreSQL; never fall back to a local file. create_app() explains.
        return ""
    url = url or "sqlite:///instance/kite.db"
    # Hosted Postgres (Vercel/Neon, Azure) gives postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme):]
    # Resolve relative SQLite paths against the project folder, not the current directory.
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:" and not os.path.isabs(path):
            url = prefix + os.path.join(BASE_DIR, path).replace("\\", "/")
    return url


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if not key and (ON_VERCEL or ON_AZURE or IS_PRODUCTION):
        # A random key per serverless instance would break logins and saved SMTP passwords;
        # create_app() refuses to start with an empty key and explains why.
        return ""
    if not key:
        logging.getLogger(__name__).warning(
            "SECRET_KEY is not set; using a random key (sessions reset on restart)."
        )
        key = secrets.token_hex(32)
    return key


def _csv(name):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


class Config:
    APP_ENV = APP_ENV
    APP_NAME = os.environ.get("APP_NAME") or "KITE"
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Hosted databases drop idle connections; check each one before use.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "http://localhost:5001").rstrip("/")

    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT") or 25)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM") or "kite-noreply@example.com"
    SMTP_USE_TLS = _bool("SMTP_USE_TLS")
    SMTP_TIMEOUT = 10

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", default=ON_VERCEL or ON_AZURE or IS_PRODUCTION)
    # Signed-in sessions end after this many hours (then a fresh sign-in, with Microsoft when SSO is on).
    PERMANENT_SESSION_LIFETIME = timedelta(hours=float(os.environ.get("SESSION_HOURS") or 8))
    # Behind Azure App Service / Vercel the HTTPS proxy sets X-Forwarded-For and X-Forwarded-Proto.
    TRUST_PROXY = _bool("TRUST_PROXY", default=ON_VERCEL or ON_AZURE)

    # Microsoft Entra ID single sign-on (OpenID Connect, authorisation code flow with PKCE via MSAL).
    SSO_ENABLED = _bool("SSO_ENABLED")
    AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "").strip()
    AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip()
    AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
    # Default: APP_BASE_URL + /auth/callback. Must match a redirect URI in the app registration exactly.
    AZURE_REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI", "").strip()
    # Read the department from Microsoft Graph (/me) at sign-in.
    SSO_READ_DEPARTMENT = _bool("SSO_READ_DEPARTMENT", default=True)
    # Username/password sign-in. Turn off at go-live; BREAK_GLASS_USERS (comma-separated usernames)
    # can still use a password, and every such login is recorded in the admin log.
    LOCAL_LOGIN_ENABLED = _bool("LOCAL_LOGIN_ENABLED", default=True)
    BREAK_GLASS_USERS = _csv("BREAK_GLASS_USERS")

    # Shared secret for /tasks/daily-reminders (Vercel Cron sends it as "Authorization: Bearer ...").
    # Empty = the URL is switched off; use the daily-reminders CLI command instead.
    CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
