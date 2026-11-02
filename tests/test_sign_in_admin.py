"""Azure SSO switched on from the Admin console, and new projects showing on the home page."""
from datetime import timedelta

import msal
import pytest

import notify
import sso
from conftest import account_id, login
from models import AdminEvent, KTProject, Setting, db
from timeutil import utcnow

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FORM = {"AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": CLIENT, "AZURE_CLIENT_SECRET": "top-secret-value", "SSO_ENABLED": "1"}


@pytest.fixture
def https_app(app):
    app.config["APP_BASE_URL"] = "https://kite.test"
    return app


def test_only_admins_reach_the_page(https_app, client):
    login(client, "lead")
    assert client.get("/admin/sign-in").status_code == 403
    assert client.post("/admin/sign-in", data=FORM).status_code == 403
    assert client.post("/admin/sign-in/test").status_code == 403
    assert client.post("/admin/sign-in/reset").status_code == 403
    assert b"Sign in with Microsoft" not in client.get("/logout", follow_redirects=True).data
    with https_app.app_context():
        assert Setting.query.count() == 0


def test_switching_it_on_from_the_console_shows_it_at_login(https_app, client):
    login(client, "admin")
    page = client.get("/admin/sign-in").data.decode()
    assert "https://kite.test/auth/callback" in page and ">Off<" in page
    assert client.post("/admin/sign-in", data=FORM).status_code == 302
    page = client.get("/admin/sign-in").data.decode()
    assert ">On<" in page and TENANT in page and "top-secret-value" not in page  # the secret is never shown
    with https_app.app_context():
        stored = db.session.get(Setting, sso.SECRET_SETTING).value
        assert "top-secret-value" not in stored and notify.decrypt_secret(stored) == "top-secret-value"
        event = AdminEvent.query.filter_by(action="SSO_SETTINGS_UPDATED").one()
        assert "top-secret-value" not in (event.detail or "") and "secret changed" in event.detail
    client.get("/logout")
    page = client.get("/login").data.decode()
    assert "Sign in with Microsoft" in page and 'name="password"' in page  # passwords stay on: no lock-out
    assert client.post("/login", data={"username": "admin", "password": "test-password-123"}).status_code == 302


def test_the_console_values_are_the_ones_used(https_app, client, monkeypatch):
    login(client, "admin")
    client.post("/admin/sign-in", data=FORM)
    made = []

    class Fake:
        def __init__(self, client_id, authority, client_credential):
            made.append((client_id, authority, client_credential))

        def initiate_auth_code_flow(self, scopes, redirect_uri):
            return {"state": "s", "auth_uri": "https://login.microsoftonline.com/x", "redirect_uri": redirect_uri}

    monkeypatch.setattr(msal, "ConfidentialClientApplication", Fake)
    resp = client.get("/auth/microsoft")
    assert resp.status_code == 302 and resp.headers["Location"].startswith("https://login.microsoftonline.com/")
    assert made == [(CLIENT, f"https://login.microsoftonline.com/{TENANT}", "top-secret-value")]
    # A new secret takes effect at once, without a restart.
    client.post("/admin/sign-in", data={**FORM, "AZURE_CLIENT_SECRET": "second-secret"})
    client.get("/auth/microsoft")
    assert made[-1][2] == "second-secret" and len(made) == 2


def test_empty_secret_keeps_the_saved_one_and_switching_off_works(https_app, client):
    login(client, "admin")
    client.post("/admin/sign-in", data=FORM)
    client.post("/admin/sign-in", data={"AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": CLIENT, "AZURE_CLIENT_SECRET": ""})
    with https_app.app_context():
        assert not sso.enabled() and sso.settings()["secret"] == "top-secret-value"
    client.get("/logout")
    assert b"Sign in with Microsoft" not in client.get("/login").data
    assert client.get("/auth/microsoft").status_code == 404  # switched off: the route is closed too


@pytest.mark.parametrize("fields, message", [
    ({"AZURE_TENANT_ID": "company.com"}, "The tenant ID looks like"),
    ({"AZURE_TENANT_ID": ""}, "The tenant ID looks like"),
    ({"AZURE_CLIENT_ID": "abc"}, "The client ID looks like"),
    ({"AZURE_CLIENT_SECRET": ""}, "Enter the client secret"),
])
def test_it_cannot_be_switched_on_half_set_up(https_app, client, fields, message):
    login(client, "admin")
    resp = client.post("/admin/sign-in", data={**FORM, **fields})
    assert resp.status_code == 400 and message in resp.data.decode()
    with https_app.app_context():
        assert Setting.query.count() == 0 and not sso.enabled()


def test_it_needs_https_except_on_this_pc(app, client):
    login(client, "admin")
    resp = client.post("/admin/sign-in", data=FORM)  # the test site address is http://kite.test
    assert resp.status_code == 400 and "needs an https address" in resp.data.decode()
    app.config["APP_BASE_URL"] = "http://localhost:5001"
    assert client.post("/admin/sign-in", data=FORM).status_code == 302


def test_check_with_microsoft(https_app, client, monkeypatch):
    login(client, "admin")
    assert "Enter the tenant ID" in client.post("/admin/sign-in/test", follow_redirects=True).data.decode()
    client.post("/admin/sign-in", data=FORM)
    answers = [{"access_token": "t"}, {"error": "invalid_client", "error_description": "AADSTS7000215 details"}, {"error": "other"}]

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def acquire_token_for_client(self, scopes):
            return answers.pop(0)

    monkeypatch.setattr(msal, "ConfidentialClientApplication", Fake)
    assert "Microsoft accepted" in client.post("/admin/sign-in/test", follow_redirects=True).data.decode()
    assert "client ID or secret" in client.post("/admin/sign-in/test", follow_redirects=True).data.decode()
    assert "Microsoft refused (other)" in client.post("/admin/sign-in/test", follow_redirects=True).data.decode()


def test_unreachable_microsoft_is_a_message_not_a_crash(https_app, client, monkeypatch):
    login(client, "admin")
    client.post("/admin/sign-in", data=FORM)

    def boom(*args, **kwargs):
        raise ValueError("no network")
    monkeypatch.setattr(msal, "ConfidentialClientApplication", boom)
    resp = client.post("/admin/sign-in/test", follow_redirects=True)
    assert resp.status_code == 200 and "Could not reach Microsoft" in resp.data.decode()


def test_reset_goes_back_to_the_server_settings(https_app, client):
    login(client, "admin")
    client.post("/admin/sign-in", data=FORM)
    client.post("/admin/sign-in/reset")
    with https_app.app_context():
        assert Setting.query.count() == 0 and not sso.enabled()
    https_app.config.update(SSO_ENABLED=True, AZURE_TENANT_ID=TENANT, AZURE_CLIENT_ID=CLIENT, AZURE_CLIENT_SECRET="from-env")
    with https_app.app_context():
        assert sso.enabled() and sso.settings()["secret"] == "from-env"


def test_unreadable_secret_turns_sign_in_off_rather_than_breaking(https_app, client):
    login(client, "admin")
    client.post("/admin/sign-in", data=FORM)
    with https_app.app_context():
        db.session.get(Setting, sso.SECRET_SETTING).value = "not-a-valid-token"
        db.session.commit()
        assert not sso.enabled()
    assert "can no longer be read" in client.get("/admin/sign-in").data.decode()


# --- new projects on the home page -------------------------------------------------------------------
def make(app, name, workflow, code="ACME", age=0):
    with app.app_context():
        db.session.add(KTProject(account_id=account_id(code), name=name, workflow=workflow, created_at=utcnow() - timedelta(days=age)))
        db.session.commit()


def section(page):
    return page[page.index('class="track-projects"'):page.index('class="section-title" id="workspaces"')] if 'class="track-projects"' in page else ""


def test_member_sees_new_projects_of_both_tracks_on_home(app, client):
    make(app, "Client take-over", "CLIENT_KT")
    make(app, "Existing hand-over", "INTERNAL_KT")
    make(app, "Older project", "CLIENT_KT", age=30)
    make(app, "Beta only project", "CLIENT_KT", code="BETA")
    login(client, "mem1")
    page = client.get("/").data.decode()
    lists = section(page)
    assert "Client take-over" in lists and "Existing hand-over" in lists and "Older project" in lists
    assert "Beta only project" not in page  # another account
    assert lists.count(">New<") == 2  # only the recent ones
    assert lists.index("Client take-over") < lists.index("Existing hand-over")  # under their own tracks
    nav = page[page.index('class="track-grid"'):page.index("</nav>", page.index('class="track-grid"'))]
    assert "Client take-over" not in nav  # the buttons themselves stay names only


def test_viewer_and_empty_tracks(app, client):
    make(app, "Client take-over", "CLIENT_KT")
    login(client, "viewer")
    lists = section(client.get("/").data.decode())
    assert "Client take-over" in lists and "No projects yet" in lists


def test_home_lists_only_the_latest_and_links_to_all(app, client):
    for n in range(8):
        make(app, f"Project number {n}", "CLIENT_KT")
    login(client, "mem1")
    lists = section(client.get("/").data.decode())
    assert "Project number 7" in lists and "Project number 2" not in lists and "/tracks/CLIENT_KT" in lists
