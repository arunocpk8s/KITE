"""Phase 4: transaction catalogue, change log with acknowledgements, doubts, 'My next steps' and reports."""
from datetime import date, datetime, timedelta

import pytest

from conftest import login, user_id
from models import Change, ChangeAck, KTTopic, Question, Transaction, db
from test_kt import confirm_all, make_module, make_topic

A = "/accounts/ACME"


def uid(app, username):
    with app.app_context():
        return user_id(username)


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def add_txn(client, **overrides):
    data = {"code": "fb 60", "name": "Enter vendor invoice", "system": "SAP FI", "purpose": "Post supplier invoices",
            "steps": "1. Open FB60", "frequency": "Daily", "criticality": "High", "owner_id": "", "project_id": "",
            "topic_id": "", "sop_id": "", "link": ""}
    data.update(overrides)
    return client.post(f"{A}/transactions/new", data=data)


def add_change(client, **overrides):
    data = {"title": "New approval step", "change_type": "CHANGED", "description": "Invoices over 10k need approval",
            "impact": "Check the limit first", "effective_date": "2026-11-01", "project_id": "",
            "transaction_id": "", "sop_id": "", "topic_id": ""}
    data.update(overrides)
    return client.post(f"{A}/changes/new", data=data)


def one(app, model):
    with app.app_context():
        return model.query.one().id


# --- transaction catalogue -----------------------------------------------------------------------
def test_catalogue_add_search_and_permissions(app, client):
    for username in ("mem1", "viewer"):
        login(client, username)
        assert add_txn(client).status_code == 403
        client.get("/logout")
    login(client, "sme")
    assert add_txn(client).status_code == 302
    with app.app_context():
        txn = Transaction.query.one()
        assert (txn.code, txn.owner, txn.active) == ("FB60", None, True)
    resp = add_txn(client, code="FB60")
    assert resp.status_code == 400 and b"already in the catalogue" in resp.data
    resp = add_txn(client, code="X1", purpose="", frequency="Hourly", link="ftp://x")
    assert resp.status_code == 400 and b"Say what the transaction is for" in resp.data and b"https://" in resp.data
    add_txn(client, code="F110", name="Payment run", system="SAP FI", purpose="Pay vendors", criticality="Low")
    client.get("/logout")
    login(client, "viewer")
    page = client.get(f"{A}/transactions?q=payment").data
    assert b"F110" in page and b"FB60" not in page
    assert b"FB60" in client.get(f"{A}/transactions?criticality=High").data
    client.get("/logout")
    login(client, "mem2")
    assert client.get(f"{A}/transactions").status_code == 404


# --- change log ----------------------------------------------------------------------------------
def test_change_publish_and_acknowledge(app, client, sent):
    login(client, "sme")
    add_txn(client)
    txn = one(app, Transaction)
    add_change(client, transaction_id=str(txn))
    cid = one(app, Change)
    client.get("/logout")
    login(client, "mem1")
    assert client.get(f"{A}/changes/{cid}").status_code == 404  # drafts are private
    client.get("/logout")
    login(client, "sme")
    client.post(f"{A}/changes/{cid}/publish")
    with app.app_context():
        change = db.session.get(Change, cid)
        assert change.status == "PUBLISHED" and change.code == "CHG-001"
        # Everyone in ACME except the publisher and viewers.
        assert sorted(a.user.username for a in change.acks) == ["lead", "mem1"]
    assert sorted(sent[-1][0]) == ["lead@example.com", "mem1@example.com"] and "CHG-001" in sent[-1][1]
    assert client.get(f"{A}/changes/{cid}/edit").status_code == 403  # published entries are not edited
    client.get("/logout")
    login(client, "viewer")
    assert client.post(f"{A}/changes/{cid}/ack").status_code == 403
    client.get("/logout")
    login(client, "mem1")
    assert b"I have read and understood this change" in client.get(f"{A}/changes/{cid}").data
    client.post(f"{A}/changes/{cid}/ack", data={"comment": "Understood"})
    with app.app_context():
        ack = ChangeAck.query.filter_by(change_id=cid, user_id=user_id("mem1")).one()
        assert ack.acked_at and ack.comment == "Understood"
    assert client.post(f"{A}/changes/{cid}/remind").status_code == 403
    client.get("/logout")
    login(client, "lead")
    client.post(f"{A}/changes/{cid}/remind")
    assert sent[-1][0] == ["lead@example.com"] and "Reminder" in sent[-1][1]


def test_retired_change_retires_the_transaction(app, client):
    login(client, "lead")
    add_txn(client)
    txn = one(app, Transaction)
    resp = add_change(client, change_type="RETIRED")
    assert resp.status_code == 400 and b"which transaction" in resp.data
    add_change(client, change_type="RETIRED", title="FB60 replaced by Fiori app", transaction_id=str(txn))
    cid = one(app, Change)
    client.post(f"{A}/changes/{cid}/publish")
    with app.app_context():
        assert db.session.get(Transaction, txn).active is False
    assert b"FB60" not in client.get(f"{A}/transactions").data
    assert b"FB60" in client.get(f"{A}/transactions?show=all").data


def test_draft_delete(app, client):
    login(client, "sme")
    add_change(client)
    cid = one(app, Change)
    client.post(f"{A}/changes/{cid}/delete")
    with app.app_context():
        assert Change.query.count() == 0


# --- doubts --------------------------------------------------------------------------------------
def test_doubt_goes_to_the_topic_sme_and_back(app, client, sent):
    mid = make_module(app)
    tid = make_topic(app, mid, sme="sme", receivers=("mem1",))
    login(client, "viewer")
    assert client.post(f"{A}/doubts/new", data={"title": "x"}).status_code == 403
    client.get("/logout")
    login(client, "mem1")
    assert client.get(f"{A}/doubts/new?topic_id={tid}").status_code == 200
    client.post(f"{A}/doubts/new", data={"title": "Which GL for credit notes?", "detail": "Tried 4000",
                                         "topic_id": str(tid), "blocking": "on"})
    with app.app_context():
        q = Question.query.one()
        assert (q.code, q.assigned_to.username, q.blocking, q.status) == ("Q-001", "sme", True, "OPEN")
        assert q.project_id == db.session.get(KTTopic, tid).module.project_id
        qid = q.id
    assert sent[-1][0] == ["sme@example.com"] and "BLOCKING" in sent[-1][1]
    client.get("/logout")

    login(client, "sme")
    client.post(f"{A}/doubts/{qid}/reply", data={"body": "Use 4100 for credit notes"})
    assert sent[-1][0] == ["mem1@example.com"] and "Answer to Q-001" in sent[-1][1]
    assert client.post(f"{A}/doubts/{qid}/resolve").status_code == 403  # only the asker or a KT lead
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{A}/doubts/{qid}/reply", data={"body": "And for refunds?"})
    with app.app_context():
        assert db.session.get(Question, qid).status == "OPEN"
    assert sent[-1][0] == ["sme@example.com"]
    client.post(f"{A}/doubts/{qid}/resolve")
    with app.app_context():
        q = db.session.get(Question, qid)
        assert q.status == "RESOLVED" and q.resolved_at and len(q.replies) == 2
    assert b"Q-001" not in client.get(f"{A}/doubts").data  # open only by default
    assert b"Q-001" in client.get(f"{A}/doubts?show=all").data


def test_unassigned_doubt_goes_to_kt_leads(app, client, sent):
    login(client, "mem1")
    client.post(f"{A}/doubts/new", data={"title": "Who approves access?"})
    qid = one(app, Question)
    assert sent[-1][0] == ["lead@example.com"] and "not assigned" in sent[-1][2]
    assert client.post(f"{A}/doubts/{qid}/assign", data={"assigned_to_id": str(uid(app, "sme"))}).status_code == 403
    client.get("/logout")
    login(client, "lead")
    client.post(f"{A}/doubts/{qid}/assign", data={"assigned_to_id": str(uid(app, "mem1"))})  # not an SME
    with app.app_context():
        assert db.session.get(Question, qid).assigned_to_id is None
    client.post(f"{A}/doubts/{qid}/assign", data={"assigned_to_id": str(uid(app, "sme"))})
    with app.app_context():
        assert db.session.get(Question, qid).assigned_to.username == "sme"
    assert sent[-1][0] == ["sme@example.com"]


# --- next steps ----------------------------------------------------------------------------------
def test_next_steps_for_receiver_lead_and_sme(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid, title="Credit notes", status="DELIVERED", receivers=("mem1",))
    make_topic(app, mid, title="Dunning", status="PLANNED", sme="sme")
    login(client, "mem1")
    client.post(f"{A}/doubts/new", data={"title": "Blocked on access", "blocking": "on"})
    page = client.get("/").data.decode()
    assert "Confirm you received the KT for &#39;Credit notes&#39;" in page
    assert "Rate how well you understood" in page
    client.get("/logout")

    login(client, "sme")
    page = client.get("/").data.decode()
    assert "Schedule the KT session for &#39;Dunning&#39;" in page
    client.get("/logout")

    confirm_all(app, tid)
    with app.app_context():
        db.session.get(KTTopic, tid).status = "REVERSE_KT"
        db.session.commit()
    login(client, "lead")
    page = client.get("/").data.decode()
    assert "Sign off &#39;Credit notes&#39;" in page and "Assign Q-001" in page and "Urgent" in page


# --- reports -------------------------------------------------------------------------------------
def test_reports_page_and_scope(app, client):
    mid = make_module(app)
    t1 = make_topic(app, mid, title="Credit notes", status="REVERSE_KT", receivers=("mem1",))
    make_topic(app, mid, title="Dunning", status="SCHEDULED", receivers=())
    confirm_all(app, t1)
    login(client, "lead")
    client.post(f"{A}/kt/topics/{t1}/status", data={"status": "SIGNED_OFF"})
    page = client.get("/reports").data.decode()
    for text in ("KT progress by project", "Topics signed off", "Projects by stage", "Change acknowledgements",
                 "Team readiness", "50%", "Show as table", 'class="line-draw"'):
        assert text in page, text
    assert client.get("/reports?account=ACME").status_code == 200
    csv_text = client.get("/reports/readiness.csv?account=ACME").data.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Name,Role,KT topics received")
    assert any(line.startswith("Mem1,Team member,1,1") for line in csv_text.splitlines())
    client.get("/logout")
    login(client, "mem2")  # BETA only: ACME data must not appear
    page = client.get("/reports?account=ACME").data.decode()
    assert "Credit notes" not in page and "Mem1" not in page


def test_trend_and_scale_helpers():
    from reports import nice_max, upto_ist
    assert [nice_max(v) for v in (0, 3, 6, 11, 20, 21, 140)] == [5, 5, 10, 20, 20, 50, 200]
    day = date(2026, 10, 20)
    # 20 Oct 23:59 IST is 18:29 UTC: a sign-off at 18:00 UTC counts for that day, 19:00 UTC does not.
    assert upto_ist(day, [datetime(2026, 10, 20, 18, 0), datetime(2026, 10, 20, 19, 0)]) == 1
    assert upto_ist(day + timedelta(days=1), [datetime(2026, 10, 20, 19, 0)]) == 1
