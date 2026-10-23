"""SOPs (versions, review, approval, review dates, retirement, reminders) and the scope of work."""
from datetime import date, timedelta

import pytest

from conftest import account_id, login, user_id
from models import Account, ScopeItem, Sop, SopEvent, db
from timeutil import add_months, ist_today

BASE = "/accounts/ACME/sops"


def uid(app, username):
    with app.app_context():
        return user_id(username)


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def sop_form(**overrides):
    data = {"title": "Month-end accruals", "owner_id": "", "project_id": "", "review_months": "12",
            "purpose": "Post accruals before close", "steps": "1. Pull report\n2. Post journal",
            "link": "https://acme.sharepoint.com/sops/accruals.docx", "change_note": ""}
    data.update(overrides)
    return data


def the_sop(app):
    return Sop.query.one()


def create_as_sme(app, client):
    login(client, "sme")
    assert client.post(f"{BASE}/new", data=sop_form(owner_id=str(uid(app, "lead")))).status_code == 302
    client.get("/logout")
    with app.app_context():
        return the_sop(app).id


def test_sme_writes_a_draft_and_members_cannot(app, client):
    for username in ("mem1", "viewer"):
        login(client, username)
        assert client.post(f"{BASE}/new", data=sop_form()).status_code == 403
        client.get("/logout")
    login(client, "sme")
    assert client.get(f"{BASE}/new").status_code == 200
    client.get("/logout")
    sid = create_as_sme(app, client)
    login(client, "sme")
    assert client.get(f"{BASE}/{sid}/edit").status_code == 200
    client.get("/logout")
    with app.app_context():
        sop = the_sop(app)
        # The SME is made the owner whatever the form says.
        assert (sop.code, sop.owner.username, sop.status, sop.versions[0].number) == ("SOP-001", "sme", "DRAFT", 1)
    login(client, "viewer")
    page = client.get(f"{BASE}/{sid}").data
    assert b"Month-end accruals" in page and b"Submit for review" not in page
    assert client.get(BASE).status_code == 200


def test_validation(app, client):
    login(client, "lead")
    resp = client.post(f"{BASE}/new", data=sop_form(title="", owner_id=str(uid(app, "mem1")), purpose="",
                                                    steps="", link="", review_months="99"))
    assert resp.status_code == 400
    for text in (b"Enter the SOP title", b"Choose the owner", b"Say what the SOP is for", b"Write the steps",
                 b"1 to 36 months"):
        assert text in resp.data, text
    resp = client.post(f"{BASE}/new", data=sop_form(owner_id=str(uid(app, "sme")), link="javascript:x"))
    assert resp.status_code == 400 and b"must start with https://" in resp.data


def test_review_approve_and_new_version(app, client, sent):
    sid = create_as_sme(app, client)
    login(client, "sme")
    client.post(f"{BASE}/{sid}/submit")
    assert sent[-1][0] == ["lead@example.com"] and "SOP to review" in sent[-1][1]
    # In review: no editing until it is approved or sent back.
    client.post(f"{BASE}/{sid}/edit", data=sop_form(steps="changed"))
    with app.app_context():
        assert the_sop(app).versions[0].steps.startswith("1. Pull")
    assert client.post(f"{BASE}/{sid}/approve").status_code == 403  # SMEs do not approve
    client.get("/logout")

    login(client, "lead")
    client.post(f"{BASE}/{sid}/send-back", data={"comment": ""})
    with app.app_context():
        assert the_sop(app).working_version.status == "IN_REVIEW"  # a comment is required
    client.post(f"{BASE}/{sid}/send-back", data={"comment": "Add the approval limit"})
    assert "sent back" in sent[-1][1] and sent[-1][0] == ["sme@example.com"]
    client.get("/logout")
    login(client, "sme")
    client.post(f"{BASE}/{sid}/edit", data=sop_form(steps="1. Pull report\n2. Check limit\n3. Post"))
    client.post(f"{BASE}/{sid}/submit")
    client.get("/logout")
    login(client, "lead")
    client.post(f"{BASE}/{sid}/approve")
    with app.app_context():
        sop = the_sop(app)
        assert sop.status == "APPROVED" and sop.approved_version.number == 1
        assert sop.review_due == add_months(ist_today(), 12)
        assert "Check limit" in sop.approved_version.steps
    assert "SOP approved" in sent[-1][1]

    # Changing an approved SOP starts version 2; version 1 stays in force.
    resp = client.post(f"{BASE}/{sid}/edit", data=sop_form(owner_id=str(uid(app, "sme")), steps="v2 steps"))
    assert resp.status_code == 400 and b"Say what changes" in resp.data
    client.post(f"{BASE}/{sid}/edit", data=sop_form(owner_id=str(uid(app, "sme")), steps="v2 steps",
                                                    change_note="New ERP screen"))
    with app.app_context():
        sop = the_sop(app)
        assert [v.status for v in sop.versions] == ["APPROVED", "DRAFT"]
        assert sop.status == "APPROVED" and sop.working_version.number == 2
    client.post(f"{BASE}/{sid}/submit")
    # The lead wrote version 2, so another KT lead (or an admin) must approve it.
    assert client.post(f"{BASE}/{sid}/approve").status_code == 403
    client.get("/logout")
    login(client, "admin")
    client.post(f"{BASE}/{sid}/approve")
    with app.app_context():
        sop = the_sop(app)
        assert [v.status for v in sop.versions] == ["SUPERSEDED", "APPROVED"]
        assert sop.approved_version.steps == "v2 steps"
        assert [e.event_type for e in sop.events] == [
            "CREATED", "SUBMITTED", "SENT_BACK", "EDITED", "SUBMITTED", "APPROVED", "NEW_VERSION", "SUBMITTED",
            "APPROVED"]
    page = client.get(f"{BASE}/{sid}?v=1").data
    assert b"Version 1" in page and b"Superseded" in page


def test_periodic_review_retire_and_reminders(app, client, sent):
    sid = create_as_sme(app, client)
    login(client, "sme")
    client.post(f"{BASE}/{sid}/submit")
    client.get("/logout")
    login(client, "lead")
    client.post(f"{BASE}/{sid}/approve")
    with app.app_context():
        sop = the_sop(app)
        sop.review_due = ist_today() - timedelta(days=3)
        db.session.commit()
        assert sop.review_overdue(ist_today())
    assert b"Review overdue" in client.get(BASE).data

    with app.test_request_context():
        from sops import send_review_reminders
        assert send_review_reminders() == 1
    assert sent[-1][0] == ["sme@example.com"] and "OVERDUE" in sent[-1][2]

    client.post(f"{BASE}/{sid}/reviewed", data={"comment": "Still correct"})
    with app.app_context():
        assert the_sop(app).review_due == add_months(ist_today(), 12)
    with app.test_request_context():
        from sops import send_review_reminders
        assert send_review_reminders() == 0

    client.post(f"{BASE}/{sid}/retire", data={"reason": ""})
    with app.app_context():
        assert the_sop(app).retired_at is None
    client.post(f"{BASE}/{sid}/retire", data={"reason": "Process moved to the client"})
    with app.app_context():
        assert the_sop(app).status == "RETIRED"
    assert client.get(f"{BASE}/{sid}/edit").status_code == 400
    assert b"Month-end accruals" not in client.get(BASE).data  # hidden by default
    assert b"Month-end accruals" in client.get(f"{BASE}?status=RETIRED").data


def test_sop_of_another_account_is_404(app, client):
    sid = create_as_sme(app, client)
    login(client, "mem2")
    assert client.get(f"{BASE}/{sid}").status_code == 404
    assert client.get(f"/accounts/BETA/sops/{sid}").status_code == 404


def test_sop_history_is_append_only(app, client):
    create_as_sme(app, client)
    with app.app_context():
        event = SopEvent.query.first()
        db.session.delete(event)
        with pytest.raises(RuntimeError):
            db.session.commit()
        db.session.rollback()


# --- scope of work -------------------------------------------------------------------------------
def test_scope_of_work(app, client):
    login(client, "lead")
    client.post("/accounts/ACME/scope/items", data={"kind": "IN_SCOPE", "title": "Accounts receivable"})
    client.post("/accounts/ACME/scope/items", data={"kind": "SLA", "title": "P1: respond 30 min",
                                                    "detail": "24x7"})
    client.post("/accounts/ACME/scope/items", data={"kind": "BOGUS", "title": "x"})
    client.post("/accounts/ACME/scope/details", data={"contract_start": "2026-11-01", "contract_end": "2026-10-01"})
    with app.app_context():
        assert Account.query.filter_by(code="ACME").one().contract_start is None  # end before start rejected
    client.post("/accounts/ACME/scope/details", data={"contract_start": "2026-11-01", "contract_end": "2029-10-31",
                                                      "scope_link": "https://acme.sharepoint.com/sow.pdf"})
    with app.app_context():
        assert [i.kind for i in ScopeItem.query.order_by(ScopeItem.id)] == ["IN_SCOPE", "SLA"]
        acme = Account.query.filter_by(code="ACME").one()
        assert acme.contract_end == date(2029, 10, 31)
        sla = ScopeItem.query.filter_by(kind="SLA").one().id
    client.post(f"/accounts/ACME/scope/items/{sla}", data={"kind": "SLA", "title": "P1: respond 15 min"})
    client.get("/logout")
    login(client, "viewer")
    page = client.get("/accounts/ACME/scope").data
    assert b"P1: respond 15 min" in page and b"Open the signed SOW" in page and b"Add to" not in page
    assert client.post("/accounts/ACME/scope/items", data={"kind": "IN_SCOPE", "title": "x"}).status_code == 403
    assert client.post(f"/accounts/ACME/scope/items/{sla}/delete").status_code == 403
    client.get("/logout")
    login(client, "mem2")
    assert client.get("/accounts/ACME/scope").status_code == 404
    with app.app_context():
        assert ScopeItem.query.filter_by(account_id=account_id("ACME")).count() == 2
