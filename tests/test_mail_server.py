"""Editable mail server settings in the admin console."""
import pytest

import notify
from conftest import login
from models import AdminEvent, Setting, db

SERVER = {
    "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USE_TLS": "1",
    "SMTP_USER": "kite.test@gmail.com", "SMTP_PASSWORD": "app-pass-1234",
    "SMTP_FROM": "kite.test@gmail.com", "APP_BASE_URL": "https://kite.example.com/",
}


class FakeSMTP:
    """Records what would have been sent instead of connecting anywhere."""
    calls = []

    def __init__(self, host, port, timeout=None):
        self.record = {"host": host, "port": port, "tls": False, "login": None, "to": None}
        FakeSMTP.calls.append(self.record)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.record["tls"] = True

    def login(self, user, password):
        self.record["login"] = (user, password)

    def send_message(self, msg):
        self.record["to"] = msg["To"]
        self.record["from"] = msg["From"]


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.calls = []
    monkeypatch.setattr("notify.smtplib.SMTP", FakeSMTP)
    return FakeSMTP.calls


def test_save_server_settings_and_send(app, client, fake_smtp):
    login(client, "admin")
    assert client.post("/admin/mail/server", data=SERVER).status_code == 302
    with app.app_context():
        stored = db.session.get(Setting, notify.SMTP_PASSWORD_KEY).value
        assert stored and "app-pass-1234" not in stored  # encrypted at rest
        event = AdminEvent.query.filter_by(action="MAIL_SERVER_UPDATED").one()
        assert "SMTP password changed" in event.detail and "app-pass-1234" not in event.detail
        assert db.session.get(Setting, "APP_BASE_URL").value == "https://kite.example.com"
    page = client.get("/admin/mail").data
    assert b"app-pass-1234" not in page and b"smtp.gmail.com" in page and b"A password is saved" in page

    resp = client.post("/admin/mail/test", data={"to": "me@example.com"}, follow_redirects=True)
    assert b"Test email sent" in resp.data
    assert fake_smtp[-1] == {"host": "smtp.gmail.com", "port": 587, "tls": True,
                             "login": ("kite.test@gmail.com", "app-pass-1234"),
                             "to": "me@example.com", "from": "kite.test@gmail.com"}


def test_blank_password_keeps_saved_one_and_clear_removes_it(app, client, fake_smtp):
    login(client, "admin")
    client.post("/admin/mail/server", data=SERVER)
    client.post("/admin/mail/server", data=dict(SERVER, SMTP_PASSWORD="", SMTP_PORT="2525"))
    with app.app_context():
        settings = notify.smtp_settings()
        assert (settings["port"], settings["password"]) == (2525, "app-pass-1234")
    client.post("/admin/mail/server", data=dict(SERVER, SMTP_PASSWORD="", clear_password="1"))
    with app.app_context():
        assert notify.smtp_settings()["password"] == ""
        assert notify.smtp_settings()["password_status"] == "none"


def test_email_links_use_saved_base_url(app, client):
    with app.app_context():
        assert notify.app_url("/accounts/ACME") == "http://kite.test/accounts/ACME"  # .env value
    login(client, "admin")
    client.post("/admin/mail/server", data=SERVER)
    with app.app_context():
        assert notify.app_url("/accounts/ACME") == "https://kite.example.com/accounts/ACME"


def test_server_settings_validation(app, client):
    login(client, "admin")
    resp = client.post("/admin/mail/server", data=dict(
        SERVER, SMTP_HOST="bad host!", SMTP_PORT="99999", SMTP_FROM="nope", APP_BASE_URL="kite"))
    assert resp.status_code == 400
    for message in (b"host name", b"port number", b"sender email", b"address people use"):
        assert message in resp.data
    with app.app_context():
        assert Setting.query.filter(Setting.key.like("SMTP%")).count() == 0


def test_reset_to_env(app, client):
    login(client, "admin")
    client.post("/admin/mail/server", data=SERVER)
    client.post("/admin/mail/server/reset")
    with app.app_context():
        assert notify.smtp_settings()["host"] == ""  # tests run with SMTP_HOST empty in .env config
        assert notify.smtp_settings()["password_status"] == "env"
        assert [e.action for e in AdminEvent.query.order_by(AdminEvent.id)] == ["MAIL_SERVER_UPDATED", "MAIL_SERVER_RESET"]


def test_password_unreadable_after_secret_key_change(app, client):
    login(client, "admin")
    client.post("/admin/mail/server", data=SERVER)
    app.config["SECRET_KEY"] = "a-different-key"
    with app.app_context():
        assert notify.smtp_settings()["password_status"] == "unreadable"
        assert notify.send_test("me@example.com").startswith("RuntimeError: Saved SMTP password cannot be read")


def test_only_admin_can_change_server(client):
    login(client, "lead")
    assert client.post("/admin/mail/server", data=SERVER).status_code == 403
    assert client.post("/admin/mail/server/reset").status_code == 403
