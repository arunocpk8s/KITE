"""Microsoft SSO, break-glass login, health check and production start-up checks. Microsoft is simulated."""
import pytest

import sso
from app import check_settings
from conftest import login
from models import AdminEvent, User, db

TENANT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sso_app(app):
    app.config.update(
        SSO_ENABLED=True, AZURE_TENANT_ID=TENANT, AZURE_CLIENT_ID="client-id", AZURE_CLIENT_SECRET="secret",
        SSO_READ_DEPARTMENT=True,
    )
    return app


@pytest.fixture
def microsoft(monkeypatch):
    """Simulated Microsoft: set .claims / .result before calling /auth/callback."""
    class Fake:
        claims = {}
        result = None
        department = "SSI"
        error = None

    fake = Fake()
    monkeypatch.setattr(sso, "start_flow", lambda: {"state": "s", "auth_uri": "https://login.microsoftonline.com/x"})

    def finish(flow, args):
        if fake.error:
            raise fake.error
        return fake.result or {"id_token_claims": fake.claims, "access_token": "token"}

    monkeypatch.setattr(sso, "finish_flow", finish)
    monkeypatch.setattr(sso, "read_department", lambda token: fake.department)
    return fake


def claims(oid="oid-1", email="new.person@company.com", name="New Person", tid=TENANT):
    return {"oid": oid, "tid": tid, "preferred_username": email, "name": name}


def sign_in(client, next_url=""):
    client.get("/auth/microsoft" + (f"?next={next_url}" if next_url else ""))
    return client.get("/auth/callback?code=abc&state=s")


def current_user(client):
    with client.session_transaction() as s:
        return s.get("user_id")


def test_login_page_shows_microsoft_button_only_when_enabled(app, client):
    assert b"Sign in with Microsoft" not in client.get("/login").data
    app.config.update(SSO_ENABLED=True, AZURE_TENANT_ID=TENANT)
    assert b"Sign in with Microsoft" in client.get("/login").data


def test_sso_routes_are_hidden_when_disabled(client):
    assert client.get("/auth/microsoft").status_code == 404
    assert client.get("/auth/callback").status_code == 404


def test_start_redirects_to_microsoft(sso_app, client, microsoft):
    resp = client.get("/auth/microsoft")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://login.microsoftonline.com/")


def test_first_sign_in_creates_team_member(sso_app, client, microsoft):
    microsoft.claims = claims()
    resp = sign_in(client, "/accounts/ACME")
    assert resp.headers["Location"].endswith("/accounts/ACME")
    with sso_app.app_context():
        user = User.query.filter_by(sso_oid="oid-1").one()
        assert (user.role, user.email, user.full_name, user.department) == (
            "member", "new.person@company.com", "New Person", "SSI")
        assert user.username == "new.person" and user.auth_source == "sso" and user.last_login_at
        assert not user.check_password("")
        assert current_user(client) == user.id
        assert AdminEvent.query.filter_by(action="SSO_ACCOUNT_CREATED").count() == 1


def test_people_registered_by_email_are_linked_and_keep_role(sso_app, client, microsoft):
    microsoft.claims = claims(oid="oid-lead", email="LEAD@example.com", name="Lead Renamed")
    sign_in(client)
    with sso_app.app_context():
        user = User.query.filter_by(username="lead").one()
        assert user.sso_oid == "oid-lead"
        assert (user.role, user.full_name) == ("kt_lead", "Lead Renamed")
        assert [m.account.code for m in user.memberships] == ["ACME"]
        assert current_user(client) == user.id
        assert AdminEvent.query.filter_by(action="SSO_LINKED", target="lead").count() == 1


def test_later_sign_in_matches_by_oid_and_refreshes_email(sso_app, client, microsoft):
    microsoft.claims = claims()
    sign_in(client)
    client.get("/logout")
    microsoft.claims = claims(email="renamed@company.com", name="Renamed Person")
    sign_in(client)
    with sso_app.app_context():
        users = User.query.filter_by(sso_oid="oid-1").all()
        assert len(users) == 1 and users[0].email == "renamed@company.com"


def test_username_is_unique(sso_app, client, microsoft):
    microsoft.claims = claims(email="admin@other-domain.com")  # 'admin' is taken
    sign_in(client)
    with sso_app.app_context():
        assert User.query.filter_by(sso_oid="oid-1").one().username == "admin2"


def test_deactivated_user_is_refused(sso_app, client, microsoft):
    with sso_app.app_context():
        user = User.query.filter_by(username="lead").one()
        user.active = False
        db.session.commit()
    microsoft.claims = claims(email="lead@example.com")
    resp = sign_in(client)
    assert resp.headers["Location"].endswith("/login")
    assert current_user(client) is None
    assert b"deactivated" in client.get("/login").data


def test_other_tenant_is_refused(sso_app, client, microsoft):
    microsoft.claims = claims(tid="99999999-0000-0000-0000-000000000000")
    sign_in(client)
    assert current_user(client) is None
    with sso_app.app_context():
        assert User.query.filter_by(sso_oid="oid-1").first() is None


def test_microsoft_error_and_bad_state_are_refused(sso_app, client, microsoft):
    microsoft.result = {"error": "access_denied", "error_description": "User cancelled"}
    sign_in(client)
    assert current_user(client) is None
    microsoft.result, microsoft.error = None, ValueError("state missing from auth_code_flow")
    sign_in(client)
    assert current_user(client) is None


def test_callback_without_started_flow_is_refused(sso_app, client, microsoft):
    microsoft.claims = claims()
    resp = client.get("/auth/callback?code=abc&state=s")
    assert resp.headers["Location"].endswith("/login")
    assert current_user(client) is None


def test_unknown_department_is_left_empty(sso_app, client, microsoft):
    microsoft.claims, microsoft.department = claims(), ""
    sign_in(client)
    with sso_app.app_context():
        assert User.query.filter_by(sso_oid="oid-1").one().department == ""


def test_sso_logout_also_signs_out_of_microsoft(sso_app, client, microsoft):
    microsoft.claims = claims()
    sign_in(client)
    resp = client.get("/logout")
    assert resp.headers["Location"].startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/logout")
    assert current_user(client) is None


def test_password_logout_stays_local(sso_app, client):
    login(client, "mem1")
    assert client.get("/logout").headers["Location"].endswith("/login")


def test_local_login_off_allows_only_break_glass(sso_app, client):
    sso_app.config.update(LOCAL_LOGIN_ENABLED=False, BREAK_GLASS_USERS=["admin"])
    assert login(client, "mem1").status_code == 401
    assert b"Emergency admin login" in client.get("/login").data
    assert login(client, "admin").status_code == 302
    with sso_app.app_context():
        assert AdminEvent.query.filter_by(action="BREAK_GLASS_LOGIN", target="admin").count() == 1


def test_password_login_records_last_sign_in(app, client):
    login(client, "mem1")
    with app.app_context():
        user = User.query.filter_by(username="mem1").one()
        assert user.last_login_at and user.auth_source == "local"


def test_admin_adds_user_without_password_when_sso_on(sso_app, client):
    login(client, "admin")
    resp = client.post("/admin/users/new", data={
        "username": "ravi", "full_name": "Ravi Kumar", "email": "ravi@company.com", "role": "sme",
        "password": "", "password2": "",
    })
    assert resp.status_code == 302
    with sso_app.app_context():
        assert User.query.filter_by(username="ravi").one().password_hash == ""
    client.get("/logout")
    assert login(client, "ravi").status_code == 401  # no password: Microsoft sign-in only


def test_admin_still_needs_password_without_sso(app, client):
    login(client, "admin")
    resp = client.post("/admin/users/new", data={
        "username": "ravi", "full_name": "Ravi Kumar", "email": "ravi@company.com", "role": "sme",
        "password": "", "password2": "",
    })
    assert resp.status_code == 400


def test_healthz_needs_no_login(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json == {"status": "ok", "database": "ok"}


BASE = {"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://u:p@h/db",
        "APP_BASE_URL": "https://kite.example.com", "APP_ENV": "production"}


def test_production_settings_pass():
    check_settings(dict(BASE))


@pytest.mark.parametrize("change, message", [
    ({"SECRET_KEY": ""}, "SECRET_KEY"),
    ({"SQLALCHEMY_DATABASE_URI": "sqlite:///instance/tracker.db"}, "PostgreSQL"),
    ({"SQLALCHEMY_DATABASE_URI": ""}, "PostgreSQL"),
    ({"APP_BASE_URL": "http://kite.example.com"}, "https://"),
    ({"SSO_ENABLED": True, "AZURE_TENANT_ID": TENANT}, "AZURE_CLIENT_ID, AZURE_CLIENT_SECRET"),
])
def test_unsafe_settings_refuse_to_start(change, message):
    with pytest.raises(RuntimeError, match=message):
        check_settings(BASE | change)


def test_database_url_from_parts(monkeypatch):
    import config
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setenv("PGHOST", "kite-prod.postgres.database.azure.com")
    monkeypatch.setenv("PGUSER", "kite_app")
    monkeypatch.setenv("PGPASSWORD", "p@ss:w/rd")
    monkeypatch.setenv("PGDATABASE", "kite")
    assert config._database_url() == (
        "postgresql+psycopg://kite_app:p%40ss%3Aw%2Frd@kite-prod.postgres.database.azure.com:5432/kite"
        "?sslmode=require")
