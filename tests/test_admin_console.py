"""Admin console: access, user management (roles and accounts), mail page and the email outbox."""
from conftest import PASSWORD, account_id, login, queue_email, user_id
from models import AdminEvent, EmailOutbox, User, db

NEW_USER = {
    "username": "ravi.k", "full_name": "Ravi Kumar", "email": "ravi@example.com",
    "role": "sme", "department": "SSG",
    "password": "new-password-1", "password2": "new-password-1",
}


def actions(app):
    with app.app_context():
        return [e.action for e in AdminEvent.query.order_by(AdminEvent.id)]


def codes(app, username):
    with app.app_context():
        return sorted(m.account.code for m in db.session.get(User, user_id(username)).memberships)


# --- access ------------------------------------------------------------------------------------
def test_console_is_admin_only(client):
    assert client.get("/admin/").status_code == 302
    for username in ("lead", "sme", "mem1", "viewer"):
        login(client, username)
        for path in ("/admin/", "/admin/users", "/admin/mail", "/admin/outbox", "/admin/accounts"):
            assert client.get(path).status_code == 403, (username, path)
        assert client.post("/admin/users/new", data=NEW_USER).status_code == 403
        assert b"Admin console" not in client.get("/").data
        client.get("/logout")
    login(client, "admin")
    for path in ("/admin/", "/admin/users", "/admin/users/new", "/admin/mail", "/admin/outbox",
                 "/admin/accounts", "/admin/accounts/new"):
        assert client.get(path).status_code == 200, path
    assert b"Admin console" in client.get("/").data


def test_csrf_enforced_on_console(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        with app.app_context():
            s["user_id"] = user_id("admin")
    assert client.post("/admin/users/new", data=NEW_USER).status_code == 400


# --- users -------------------------------------------------------------------------------------
def test_create_user_with_accounts_and_log_in(app, client):
    with app.app_context():
        acme, beta = account_id("ACME"), account_id("BETA")
    login(client, "admin")
    resp = client.post("/admin/users/new", data=dict(NEW_USER, accounts=[str(acme), str(beta)]))
    assert resp.status_code == 302
    with app.app_context():
        user = User.query.filter_by(username="ravi.k").one()
        assert (user.role, user.department, user.active) == ("sme", "SSG", True)
        assert "accounts ACME, BETA" in AdminEvent.query.one().detail
    assert codes(app, "ravi.k") == ["ACME", "BETA"]
    client.get("/logout")
    assert client.post("/login", data={"username": "ravi.k", "password": "new-password-1"}).status_code == 302
    assert b"Beta Bank" in client.get("/").data


def test_new_user_form_offers_all_roles_and_accounts(client):
    login(client, "admin")
    page = client.get("/admin/users/new").data
    for role in (b"admin", b"kt_lead", b"sme", b"member", b"viewer"):
        assert b'<option value="' + role + b'"' in page
    assert b"ACME" in page and b"Acme Corporation" in page and b'name="department"' in page


def test_new_user_link_from_account_preselects_it(app, client):
    with app.app_context():
        acme = account_id("ACME")
    login(client, "admin")
    page = client.get(f"/admin/users/new?account={acme}").data
    assert f'value="{acme}" checked'.encode() in page


def test_create_user_validation(app, client):
    login(client, "admin")
    cases = [
        (dict(NEW_USER, username="mem1"), b"already taken"),
        (dict(NEW_USER, username="MEM1"), b"already taken"),
        (dict(NEW_USER, username="a b"), b"letters, numbers"),
        (dict(NEW_USER, email="not-an-email"), b"valid email"),
        (dict(NEW_USER, role="superuser"), b"Choose a role"),
        (dict(NEW_USER, department="HR"), b"department from the list"),
        (dict(NEW_USER, accounts=["9999"]), b"Unknown account"),
        (dict(NEW_USER, password="short", password2="short"), b"at least 8"),
        (dict(NEW_USER, password2="different-pass"), b"do not match"),
    ]
    for data, message in cases:
        resp = client.post("/admin/users/new", data=data)
        assert resp.status_code == 400 and message in resp.data, data
    with app.app_context():
        assert User.query.filter_by(username="ravi.k").first() is None


def test_edit_user_role_and_accounts(app, client):
    with app.app_context():
        uid, beta = user_id("mem1"), account_id("BETA")
    login(client, "admin")
    resp = client.post(f"/admin/users/{uid}", data={
        "full_name": "Mem Renamed", "email": "renamed@example.com", "role": "kt_lead", "department": "",
        "accounts": [str(beta)],
    })
    assert resp.status_code == 302
    with app.app_context():
        user = db.session.get(User, uid)
        assert (user.full_name, user.email, user.role, user.department) == (
            "Mem Renamed", "renamed@example.com", "kt_lead", "")
        detail = AdminEvent.query.filter_by(action="USER_UPDATED").one().detail
        assert "role: member -> kt_lead" in detail and "added to BETA" in detail and "removed from ACME, OLD" in detail
    assert codes(app, "mem1") == ["BETA"]


def test_edit_form_shows_current_accounts(app, client):
    with app.app_context():
        uid, acme, beta = user_id("mem1"), account_id("ACME"), account_id("BETA")
    login(client, "admin")
    page = client.get(f"/admin/users/{uid}").data
    assert f'value="{acme}" checked'.encode() in page and f'value="{beta}" checked'.encode() not in page


def test_reset_password(app, client):
    with app.app_context():
        uid = user_id("mem1")
    login(client, "admin")
    client.post(f"/admin/users/{uid}/password", data={"password": "short", "password2": "short"})
    client.post(f"/admin/users/{uid}/password", data={"password": "brand-new-pass", "password2": "brand-new-pass"})
    assert actions(app) == ["PASSWORD_RESET"]
    with app.app_context():
        assert "brand-new-pass" not in (AdminEvent.query.one().detail or "")
    client.get("/logout")
    assert client.post("/login", data={"username": "mem1", "password": PASSWORD}).status_code == 401
    assert client.post("/login", data={"username": "mem1", "password": "brand-new-pass"}).status_code == 302


def test_deactivate_and_reactivate(app, client):
    with app.app_context():
        uid = user_id("mem1")
    member = app.test_client()
    login(member, "mem1")
    assert member.get("/").status_code == 200
    login(client, "admin")
    client.post(f"/admin/users/{uid}/active", data={"active": "0"})
    assert member.get("/").status_code == 302  # signed out at once
    assert login(member, "mem1").status_code == 401
    client.post(f"/admin/users/{uid}/active", data={"active": "1"})
    assert login(member, "mem1").status_code == 302
    assert actions(app) == ["USER_DEACTIVATED", "USER_ACTIVATED"]


def test_admin_cannot_lock_themselves_out(app, client):
    with app.app_context():
        admin_id = user_id("admin")
    login(client, "admin")
    resp = client.post(f"/admin/users/{admin_id}/active", data={"active": "0"}, follow_redirects=True)
    assert b"cannot deactivate your own account" in resp.data
    resp = client.post(f"/admin/users/{admin_id}", data={"full_name": "Admin", "email": "admin@example.com", "role": "sme"})
    assert resp.status_code == 400 and b"your own admin role" in resp.data
    with app.app_context():
        admin = db.session.get(User, admin_id)
        assert admin.active and admin.role == "admin"


def test_last_active_admin_is_protected(app, client):
    login(client, "admin")
    client.post("/admin/users/new", data=dict(NEW_USER, username="admin2", role="admin"))
    with app.app_context():
        admin_id, admin2 = user_id("admin"), user_id("admin2")
    other = app.test_client()
    other.post("/login", data={"username": "admin2", "password": NEW_USER["password"]})
    # Two active admins: admin2 may demote admin.
    resp = other.post(f"/admin/users/{admin_id}", data={"full_name": "Admin", "email": "admin@example.com", "role": "sme"})
    assert resp.status_code == 302
    # admin2 is now the last active admin and cannot demote themself.
    resp = other.post(f"/admin/users/{admin2}", data={"full_name": "A2", "email": "a2@example.com", "role": "sme"})
    assert resp.status_code == 400


def test_user_list_filters(app, client):
    with app.app_context():
        beta = account_id("BETA")
    login(client, "admin")
    page = client.get("/admin/users?role=kt_lead").data
    assert b"lead@example.com" in page and b"mem1@example.com" not in page
    page = client.get(f"/admin/users?account={beta}").data
    assert b"mem2@example.com" in page and b"sme@example.com" in page and b"mem1@example.com" not in page
    page = client.get("/admin/users?q=mem2").data
    assert b"mem2@example.com" in page and b"lead@example.com" not in page


# --- mail page and outbox ----------------------------------------------------------------------
def test_mail_page_never_shows_smtp_password(app, client):
    app.config["SMTP_PASSWORD"] = "super-secret-smtp"
    login(client, "admin")
    page = client.get("/admin/mail").data
    assert b"super-secret-smtp" not in page and b"using the password from .env" in page
    assert b"mailbox" not in page.lower() and b"SOS" not in page


def test_send_test_email(app, client, monkeypatch):
    login(client, "admin")
    resp = client.post("/admin/mail/test", data={"to": "me@example.com"}, follow_redirects=True)
    assert b"Test email failed" in resp.data
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append(to))
    resp = client.post("/admin/mail/test", data={"to": "me@example.com"}, follow_redirects=True)
    assert b"Test email sent" in resp.data and sent == [["me@example.com"]]
    assert actions(app) == ["TEST_EMAIL", "TEST_EMAIL"]


def test_outbox_retry_and_cancel(app, client, monkeypatch):
    first, second = queue_email(app, 2)
    login(client, "admin")
    page = client.get("/admin/outbox?status=PENDING").data
    assert b"Retry" in page and b"Resend all 2 pending" in page
    client.post(f"/admin/outbox/{second}/cancel")
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: None)
    client.post(f"/admin/outbox/{first}/retry")
    with app.app_context():
        assert db.session.get(EmailOutbox, first).status == "SENT"
        assert db.session.get(EmailOutbox, second).status == "CANCELLED"
    assert client.post(f"/admin/outbox/{first}/retry").status_code == 400
    assert client.post(f"/admin/outbox/{second}/cancel").status_code == 400
    assert actions(app) == ["EMAIL_CANCELLED", "EMAIL_RETRIED"]


def test_outbox_resend_all(app, client, monkeypatch):
    queue_email(app, 2)
    login(client, "admin")
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: None)
    resp = client.post("/admin/outbox/resend-all", follow_redirects=True)
    assert b"Resent 2 email(s); 0 still pending." in resp.data
    with app.app_context():
        assert {e.status for e in EmailOutbox.query} == {"SENT"}


def test_admin_events_are_append_only(app, client):
    login(client, "admin")
    client.post("/admin/users/new", data=NEW_USER)
    with app.app_context():
        event = AdminEvent.query.one()
        event.detail = "tampered"
        try:
            db.session.flush()
        except RuntimeError:
            db.session.rollback()
        else:
            raise AssertionError("admin events must be append-only")
