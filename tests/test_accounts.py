"""Client accounts: who sees what, and managing accounts and members in the Admin console."""
from conftest import account_id, login, user_id
from models import Account, AccountMember, AdminEvent, User, db


def actions(app):
    with app.app_context():
        return [e.action for e in AdminEvent.query.order_by(AdminEvent.id)]


# --- visibility ------------------------------------------------------------------------------
def test_home_lists_only_my_active_accounts(client):
    login(client, "mem1")
    page = client.get("/").data
    assert b"Acme Corporation" in page
    assert b"Beta Bank" not in page          # not a member
    assert b"Old Client" not in page         # member, but the account is deactivated


def test_admin_sees_every_active_account(client):
    login(client, "admin")
    page = client.get("/").data
    assert b"Acme Corporation" in page and b"Beta Bank" in page and b"Old Client" not in page


def test_account_page_is_members_only(client):
    login(client, "mem2")
    assert client.get("/accounts/BETA").status_code == 200
    assert client.get("/accounts/ACME").status_code == 404
    assert client.get("/accounts/acme").status_code == 404
    assert client.get("/accounts/NOPE").status_code == 404


def test_account_page_shows_team(client):
    login(client, "viewer")
    page = client.get("/accounts/acme").data  # code is case-insensitive in the link
    for name in (b"Lead", b"Sme", b"Mem1", b"Viewer"):
        assert name in page
    assert b"Mem2" not in page
    assert b"KT lead" in page and b"Team member" in page
    assert b"Manage this account" not in page


def test_deactivated_account_hidden_from_members_but_not_admin(client):
    login(client, "mem1")
    assert client.get("/accounts/OLD").status_code == 404
    client.get("/logout")
    login(client, "admin")
    page = client.get("/accounts/OLD").data
    assert b"Deactivated" in page and b"Manage this account" in page


def test_role_rules(app):
    with app.app_context():
        acme, beta = Account.query.filter_by(code="ACME").one(), Account.query.filter_by(code="BETA").one()
        users = {u.username: u for u in User.query}
        assert users["admin"].can_manage(beta)
        assert users["lead"].can_manage(acme) and not users["lead"].can_manage(beta)
        assert not users["sme"].can_manage(acme) and users["sme"].can_view(beta)
        assert not users["viewer"].can_manage(acme)
        assert [a.code for a in users["sme"].visible_accounts()] == ["ACME", "BETA"]
        assert [a.code for a in users["mem1"].visible_accounts(include_inactive=True)] == ["ACME", "OLD"]


def test_user_without_accounts_sees_hint(app, client):
    with app.app_context():
        AccountMember.query.filter_by(user_id=user_id("mem2")).delete()
        db.session.commit()
    login(client, "mem2")
    assert b"not a member of any client account" in client.get("/").data


# --- admin console ---------------------------------------------------------------------------
def test_accounts_console_is_admin_only(client):
    for username in ("lead", "sme", "mem1", "viewer"):
        login(client, username)
        assert client.get("/admin/accounts").status_code == 403
        assert client.post("/admin/accounts/new", data={"code": "X1", "name": "X"}).status_code == 403
        client.get("/logout")
    login(client, "admin")
    page = client.get("/admin/accounts").data
    assert b"ACME" in page and b"OLD" in page


def test_create_account(app, client):
    login(client, "admin")
    resp = client.post("/admin/accounts/new", data={"code": " gamma-1 ", "name": "  Gamma   Ltd ", "description": "Payments"})
    assert resp.status_code == 302
    with app.app_context():
        account = Account.query.filter_by(code="GAMMA-1").one()
        assert (account.name, account.description, account.active) == ("Gamma Ltd", "Payments", True)
    assert actions(app) == ["ACCOUNT_CREATED"]


def test_create_account_validation(app, client):
    login(client, "admin")
    for data, message in [
        ({"code": "ACME", "name": "Dup"}, b"already used"),
        ({"code": "A", "name": "Short code"}, b"2-20 characters"),
        ({"code": "BAD CODE", "name": "Space"}, b"2-20 characters"),
        ({"code": "NEW1", "name": ""}, b"client account name"),
    ]:
        resp = client.post("/admin/accounts/new", data=data)
        assert resp.status_code == 400 and message in resp.data, data
    with app.app_context():
        assert Account.query.count() == 3


def test_edit_account_keeps_code(app, client):
    with app.app_context():
        aid = account_id("ACME")
    login(client, "admin")
    client.post(f"/admin/accounts/{aid}", data={"code": "HACK", "name": "Acme Corp", "description": "New scope"})
    with app.app_context():
        account = db.session.get(Account, aid)
        assert (account.code, account.name, account.description) == ("ACME", "Acme Corp", "New scope")
        assert "name: Acme Corporation -> Acme Corp" in AdminEvent.query.one().detail


def test_add_and_remove_member(app, client):
    with app.app_context():
        aid, mem2 = account_id("ACME"), user_id("mem2")
    login(client, "admin")
    assert b"Mem2" in client.get(f"/admin/accounts/{aid}").data  # offered as a candidate
    client.post(f"/admin/accounts/{aid}/members", data={"user_id": str(mem2)})
    client.post(f"/admin/accounts/{aid}/members", data={"user_id": str(mem2)})  # second add is ignored
    with app.app_context():
        assert AccountMember.query.filter_by(account_id=aid, user_id=mem2).count() == 1
    other = app.test_client()
    login(other, "mem2")
    assert other.get("/accounts/ACME").status_code == 200
    client.post(f"/admin/accounts/{aid}/members/{mem2}/remove")
    assert other.get("/accounts/ACME").status_code == 404
    assert client.post(f"/admin/accounts/{aid}/members/{mem2}/remove").status_code == 404
    assert actions(app) == ["MEMBER_ADDED", "MEMBER_REMOVED"]


def test_deactivate_and_reactivate_account(app, client):
    with app.app_context():
        aid = account_id("BETA")
    member = app.test_client()
    login(member, "mem2")
    login(client, "admin")
    client.post(f"/admin/accounts/{aid}/active", data={"active": "0"})
    assert member.get("/accounts/BETA").status_code == 404
    client.post(f"/admin/accounts/{aid}/active", data={"active": "1"})
    assert member.get("/accounts/BETA").status_code == 200
    assert actions(app) == ["ACCOUNT_DEACTIVATED", "ACCOUNT_ACTIVATED"]


def test_overview_flags_accounts_without_kt_lead(client):
    login(client, "admin")
    page = client.get("/admin/").data
    assert b"No active KT lead in" in page and b"BETA" in page
    assert b"Active client accounts" in page
