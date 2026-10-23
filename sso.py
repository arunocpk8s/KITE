"""Microsoft Entra ID single sign-on: MSAL client, and matching a Microsoft account to a KITE user.

Account rules:
1. Known Microsoft account (users.sso_oid)          -> that user; name and email refreshed.
2. Email already in KITE, not yet linked            -> linked to that user (staff added in the console).
3. Anyone else in the company tenant                -> new account with the default role (team member).
Deactivated users are refused in every case. Role and client accounts always come from KITE, never from the token.
"""
import logging
import re

import msal
import requests
from flask import current_app
from sqlalchemy import func

from config import DEPARTMENTS
from models import DEFAULT_ROLE, User, add_admin_event, db
from timeutil import utcnow

log = logging.getLogger(__name__)

SCOPES = ["User.Read"]  # openid, profile and offline_access are added by MSAL
GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=department"
USERNAME_CHARS = re.compile(r"[^a-z0-9._-]")


class SignInRefused(Exception):
    """Raised with a message that is safe to show to the person signing in."""


def enabled():
    return bool(current_app.config.get("SSO_ENABLED"))


def redirect_uri():
    return current_app.config.get("AZURE_REDIRECT_URI") or current_app.config["APP_BASE_URL"] + "/auth/callback"


def authority():
    return f"https://login.microsoftonline.com/{current_app.config['AZURE_TENANT_ID']}"


def logout_url(post_logout_redirect):
    return f"{authority()}/oauth2/v2.0/logout?post_logout_redirect_uri={requests.utils.quote(post_logout_redirect, safe='')}"


def _client():
    # MSAL caches tenant metadata in the client object; keep one per app.
    client = current_app.extensions.get("kite_msal")
    if client is None:
        cfg = current_app.config
        client = msal.ConfidentialClientApplication(
            cfg["AZURE_CLIENT_ID"], authority=authority(), client_credential=cfg["AZURE_CLIENT_SECRET"],
        )
        current_app.extensions["kite_msal"] = client
    return client


def start_flow():
    """Begin the authorisation code flow. Returns MSAL's flow dict (state, nonce, PKCE verifier, auth_uri)."""
    return _client().initiate_auth_code_flow(SCOPES, redirect_uri=redirect_uri())


def finish_flow(flow, args):
    """Exchange the code. MSAL checks state, nonce, issuer, audience and expiry; returns the token result."""
    return _client().acquire_token_by_auth_code_flow(flow, dict(args))


def read_department(access_token):
    """Department from Microsoft Graph, if it is one of DEPARTMENTS; otherwise ''."""
    try:
        resp = requests.get(GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"}, timeout=5)
        resp.raise_for_status()
        value = (resp.json().get("department") or "").strip()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Could not read the department from Microsoft Graph: %s", exc)
        return ""
    return next((d for d in DEPARTMENTS if d.lower() == value.lower()), "")


def _new_username(email):
    base = USERNAME_CHARS.sub("", email.split("@", 1)[0].lower())[:36] or "user"
    base = base.ljust(3, "0")
    candidate, n = base, 1
    while User.query.filter(func.lower(User.username) == candidate).first():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def user_from_claims(claims, department=""):
    """Find, link or create the KITE user for validated ID token claims. Commits. Raises SignInRefused."""
    tenant = current_app.config["AZURE_TENANT_ID"].lower()
    if (claims.get("tid") or "").lower() != tenant:
        log.warning("SSO refused: token from tenant %s", claims.get("tid"))
        raise SignInRefused("Sign in with your company Microsoft account.")
    oid = claims.get("oid")
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    name = (claims.get("name") or "").strip() or email
    if not oid or "@" not in email:
        raise SignInRefused("Your Microsoft account did not provide an email address. Contact the KITE admin.")

    user = User.query.filter_by(sso_oid=oid).first()
    linked = False
    if user is None:
        user = User.query.filter(func.lower(User.email) == email, User.sso_oid.is_(None)).first()
        linked = user is not None
    if user is not None and not user.active:
        log.info("SSO refused: user '%s' is deactivated", user.username)
        raise SignInRefused("Your KITE account is deactivated. Contact the KITE admin.")

    if user is None:
        user = User(
            username=_new_username(email), full_name=name[:120], email=email[:200], role=DEFAULT_ROLE,
            department=department, password_hash="", sso_oid=oid,
        )
        db.session.add(user)
        db.session.flush()
        add_admin_event(user, "SSO_ACCOUNT_CREATED", user.username, f"{DEFAULT_ROLE}, {email}")
        log.info("SSO created user '%s'", user.username)
    else:
        if linked:
            user.sso_oid = oid
            add_admin_event(user, "SSO_LINKED", user.username, f"{user.role}, {email}")
            log.info("SSO linked Microsoft account to existing user '%s'", user.username)
        # Microsoft is the source of truth for name and email and department; KITE keeps role and accounts.
        user.full_name = name[:120]
        user.email = email[:200]
        if department:
            user.department = department
    user.auth_source = "sso"
    user.last_login_at = utcnow()
    db.session.commit()
    return user
