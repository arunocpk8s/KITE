"""Login, security basics, branding, health check, email outbox and command-line tools."""
from conftest import PASSWORD, account_id, login, queue_email, user_id
from models import Account, EmailOutbox, User, db


def test_login_required_redirects(client):
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/accounts/ACME").status_code == 302


def test_login_and_logout(client):
    assert login(client, "mem1").status_code == 302
    assert b"Mem1" in client.get("/").data
    client.get("/logout")
    assert client.get("/").status_code == 302
    resp = client.post("/login", data={"username": "mem1", "password": "wrong-password"})
    assert resp.status_code == 401 and b"Invalid username or password" in resp.data


def test_login_rejects_external_next(client):
    resp = client.post("/login", data={"username": "mem1", "password": PASSWORD, "next": "//evil.example.com"})
    assert resp.headers["Location"] == "/"
    resp = client.post("/login", data={"username": "mem1", "password": PASSWORD, "next": "/accounts/ACME"})
    assert resp.headers["Location"] == "/accounts/ACME"


def test_csrf_enforced(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    assert client.post("/login", data={"username": "mem1", "password": PASSWORD}).status_code == 400


def test_output_is_escaped(app, client):
    with app.app_context():
        db.session.get(Account, account_id("ACME")).description = "<script>alert(1)</script>"
        db.session.commit()
    login(client, "mem1")
    body = client.get("/accounts/ACME").data
    assert b"<script>alert(1)</script>" not in body and b"&lt;script&gt;" in body


def test_branding_in_header(client):
    page = client.get("/login").data
    assert b"KITE" in page and b"Knowledge Transfer" in page
    assert b"SOS" not in page and b"Facility" not in page


def test_healthz_needs_no_login(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json == {"status": "ok", "database": "ok"}


def test_email_failure_is_kept_and_resent(app, monkeypatch):
    queue_email(app, 2)
    with app.app_context():
        assert all(e.status == "PENDING" and e.attempts == 1 and e.last_error for e in EmailOutbox.query)
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append(to))
    result = app.test_cli_runner().invoke(args=["resend-failed"])
    assert "Resent 2 email(s); 0 still pending." in result.output and len(sent) == 2
    with app.app_context():
        assert EmailOutbox.query.filter_by(status="SENT").count() == 2


def test_test_email_command(app, monkeypatch):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["test-email", "someone@example.com"])
    assert result.exit_code != 0 and "SMTP_HOST is not configured" in result.output
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append((to, subject)))
    result = runner.invoke(args=["test-email", "someone@example.com"])
    assert result.exit_code == 0 and sent == [(["someone@example.com"], "KITE test email")]
    with app.app_context():
        assert EmailOutbox.query.count() == 0


def test_user_commands(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["set-email", "mem1", "real@example.org"]).exit_code == 0
    assert runner.invoke(args=["set-email", "nobody", "x@example.org"]).exit_code != 0
    assert runner.invoke(args=["set-role", "mem1", "sme"]).exit_code == 0
    assert runner.invoke(args=["set-role", "mem1", "agent"]).exit_code != 0
    result = runner.invoke(args=["create-user", "priya", "--name", "Priya S", "--email", "priya@example.com",
                                 "--role", "kt_lead", "--department", "DPS"], input="long-password\nlong-password\n")
    assert result.exit_code == 0, result.output
    result = runner.invoke(args=["create-user", "short", "--name", "S", "--email", "s@example.com"],
                           input="short\nshort\n")
    assert result.exit_code != 0
    with app.app_context():
        mem1 = db.session.get(User, user_id("mem1"))
        assert (mem1.email, mem1.role) == ("real@example.org", "sme")
        assert User.query.filter_by(username="priya").one().role == "kt_lead"
        assert User.query.filter_by(username="short").first() is None


def test_account_commands(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["create-account", "gamma", "--name", "Gamma Ltd"]).exit_code == 0
    assert runner.invoke(args=["create-account", "GAMMA", "--name", "Again"]).exit_code != 0
    assert runner.invoke(args=["create-account", "x", "--name", "Too short"]).exit_code != 0
    assert runner.invoke(args=["add-member", "GAMMA", "mem2"]).exit_code == 0
    assert runner.invoke(args=["add-member", "GAMMA", "mem2"]).exit_code == 0  # already a member: no duplicate
    assert runner.invoke(args=["add-member", "NOPE", "mem2"]).exit_code != 0
    with app.app_context():
        mem2 = db.session.get(User, user_id("mem2"))
        assert sorted(m.account.code for m in mem2.memberships) == ["BETA", "GAMMA"]
