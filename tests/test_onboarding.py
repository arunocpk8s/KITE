"""Phase 6: learning paths, assigning newcomers, progress, needs-help doubts, reverse KT, readiness,
glossary and search."""
import pytest

from conftest import login, user_id
from models import GlossaryEntry, LearningPath, Onboarding, PathItem, Question, db
from test_kt import make_module, make_topic

A = "/accounts/ACME"


def uid(app, username):
    with app.app_context():
        return user_id(username)


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def build_path(app, client):
    """As the SME: a path with a required KT topic, a required link and an optional link. Returns path id."""
    tid = make_topic(app, make_module(app), title="Credit notes")
    login(client, "sme")
    client.post(f"{A}/onboarding/paths/new", data={"name": "AP analyst", "for_role": "Analyst"})
    with app.app_context():
        pid = LearningPath.query.one().id
    client.post(f"{A}/onboarding/paths/{pid}/items", data={"kind": "TOPIC", "topic_id": str(tid), "required": "on"})
    client.post(f"{A}/onboarding/paths/{pid}/items", data={"kind": "LINK", "title": "AP runbook",
                                                           "url": "https://acme.sharepoint.com/ap", "required": "on"})
    client.post(f"{A}/onboarding/paths/{pid}/items", data={"kind": "LINK", "title": "Nice to read",
                                                           "url": "https://acme.sharepoint.com/extra"})
    client.get("/logout")
    return pid


def items(app, pid):
    with app.app_context():
        return [i.id for i in db.session.get(LearningPath, pid).items]


def test_paths_are_built_by_smes_and_leads(app, client):
    login(client, "mem1")
    assert client.post(f"{A}/onboarding/paths/new", data={"name": "x"}).status_code == 403
    client.get("/logout")
    pid = build_path(app, client)
    with app.app_context():
        path = db.session.get(LearningPath, pid)
        assert [i.label for i in path.items] == ["Credit notes", "AP runbook", "Nice to read"]
        assert [i.required for i in path.items] == [True, True, False]
    login(client, "sme")
    client.post(f"{A}/onboarding/paths/{pid}/items", data={"kind": "LINK", "title": "Bad", "url": "javascript:x"})
    first, second, _ = items(app, pid)
    client.post(f"{A}/onboarding/paths/{pid}/items/{second}/up")
    assert items(app, pid)[:2] == [second, first]
    client.post(f"{A}/onboarding/paths/{pid}/items/{first}/delete")
    assert len(items(app, pid)) == 2


def test_newcomer_journey_to_ready(app, client, sent):
    pid = build_path(app, client)
    login(client, "sme")  # SMEs build paths but only KT leads assign them
    data = {"path_id": str(pid), "newcomer_id": str(uid(app, "mem1")), "buddy_id": str(uid(app, "sme"))}
    assert client.post(f"{A}/onboarding/assign", data=data).status_code == 403
    client.get("/logout")
    login(client, "lead")
    client.post(f"{A}/onboarding/assign", data={**data, "buddy_id": str(uid(app, "mem1"))})  # buddy = newcomer
    with app.app_context():
        assert Onboarding.query.count() == 0
    client.post(f"{A}/onboarding/assign", data={**data, "target_date": "2026-12-01"})
    with app.app_context():
        ob = Onboarding.query.one()
        oid = ob.id
    assert sorted(sent[-1][0]) == ["mem1@example.com", "sme@example.com"] and "Welcome" in sent[-1][1]
    client.post(f"{A}/onboarding/assign", data=data)  # already on this path
    with app.app_context():
        assert Onboarding.query.count() == 1
    client.get("/logout")

    topic_item, link_item, optional_item = items(app, pid)
    login(client, "mem1")
    assert "Continue your learning path" in client.get("/").data.decode()
    client.post(f"{A}/onboarding/{oid}/items/{topic_item}", data={"status": "NEEDS_HELP", "note": "Which GL?"})
    with app.app_context():
        q = Question.query.one()
        assert q.assigned_to.username == "sme" and q.topic_id is not None and q.title.startswith("Help needed")
    assert sent[-1][0] == ["sme@example.com"] and "needs help" in sent[-1][1]
    client.post(f"{A}/onboarding/{oid}/items/{topic_item}", data={"status": "UNDERSTOOD"})
    assert client.post(f"{A}/onboarding/{oid}/reverse-kt").status_code == 403  # the newcomer cannot
    client.post(f"{A}/onboarding/{oid}/items/{link_item}", data={"status": "UNDERSTOOD"})
    assert "ready for reverse KT" in sent[-1][1]  # optional item not needed
    client.get("/logout")

    login(client, "sme")  # the buddy
    assert client.post(f"{A}/onboarding/{oid}/ready").status_code == 400  # reverse KT first
    assert "reverse KT" in client.get("/").data.decode()
    client.post(f"{A}/onboarding/{oid}/reverse-kt", data={"note": "Explained clearly"})
    client.post(f"{A}/onboarding/{oid}/ready")
    with app.app_context():
        ob = db.session.get(Onboarding, oid)
        assert ob.status == "READY" and ob.ready_by.username == "sme" and ob.percent == 100
    assert "is ready" in sent[-1][1] and "lead@example.com" in sent[-1][0]
    client.get("/logout")
    login(client, "mem1")
    assert client.post(f"{A}/onboarding/{oid}/items/{topic_item}", data={"status": "READ"}).status_code == 400


def test_onboarding_privacy_and_cancel(app, client):
    pid = build_path(app, client)
    login(client, "lead")
    client.post(f"{A}/onboarding/assign", data={"path_id": str(pid), "newcomer_id": str(uid(app, "mem1")),
                                                 "buddy_id": str(uid(app, "sme"))})
    with app.app_context():
        oid = Onboarding.query.one().id
    client.post(f"{A}/onboarding/{oid}/cancel", data={"reason": ""})
    with app.app_context():
        assert db.session.get(Onboarding, oid).status == "ACTIVE"
    client.get("/logout")
    login(client, "viewer")  # not the newcomer, buddy, SME or KT lead
    assert client.get(f"{A}/onboarding/{oid}").status_code == 404
    client.get("/logout")
    login(client, "lead")
    client.post(f"{A}/onboarding/{oid}/cancel", data={"reason": "Moved to another account"})
    with app.app_context():
        ob = db.session.get(Onboarding, oid)
        assert ob.status == "CANCELLED" and ob.cancel_reason == "Moved to another account"


def test_glossary_and_search(app, client):
    make_topic(app, make_module(app), title="Dunning letters")
    login(client, "mem1")
    assert client.post(f"{A}/glossary", data={"term": "x", "meaning": "y"}).status_code == 403
    client.get("/logout")
    login(client, "sme")
    client.post(f"{A}/glossary", data={"kind": "TERM", "term": "Dunning", "meaning": "Payment reminder letters"})
    client.post(f"{A}/glossary", data={"kind": "SYSTEM", "term": "", "meaning": "no term"})
    with app.app_context():
        assert GlossaryEntry.query.count() == 1
        eid = GlossaryEntry.query.one().id
    client.post(f"{A}/glossary/{eid}", data={"kind": "TERM", "term": "Dunning", "meaning": "Reminder letters to customers"})
    client.get("/logout")
    login(client, "viewer")
    assert b"Reminder letters to customers" in client.get(f"{A}/glossary?q=customers").data
    page = client.get(f"{A}/search?q=dunning").data.decode()
    assert "Dunning letters" in page and "Glossary" in page and "2 results" in page
    client.get("/logout")
    login(client, "mem2")
    assert client.get(f"{A}/search?q=dunning").status_code == 404


def test_reports_list_newcomers(app, client):
    pid = build_path(app, client)
    login(client, "lead")
    client.post(f"{A}/onboarding/assign", data={"path_id": str(pid), "newcomer_id": str(uid(app, "mem1")),
                                                 "buddy_id": str(uid(app, "sme"))})
    page = client.get("/reports").data.decode()
    assert "Newcomer readiness" in page and "AP analyst" in page


def test_path_item_needs_matching_reference(app, client):
    login(client, "lead")
    client.post(f"{A}/onboarding/paths/new", data={"name": "P"})
    with app.app_context():
        pid = LearningPath.query.one().id
    client.post(f"{A}/onboarding/paths/{pid}/items", data={"kind": "TOPIC", "topic_id": "99999"})
    with app.app_context():
        assert PathItem.query.count() == 0
