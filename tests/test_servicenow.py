"""ServiceNow knowledge base: admin connection, KB suggestions for doubts, linking, publishing drafts, search."""
import pytest

import servicenow
from conftest import login
from models import AdminEvent, Question, QuestionKBLink, ServiceNowConnection, db

A = "/accounts/ACME"
SYS_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def empty_cache():
    servicenow._cache.clear()


@pytest.fixture
def fake_snow(monkeypatch):
    """Replace the HTTP call; records (method, path, params, body) and answers from `replies`."""
    calls, replies = [], {}

    def fake(conn, method, path, params=None, body=None):
        calls.append((method, path, params, body))
        answer = replies.get((method, path), {"result": []})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(servicenow, "_request", fake)
    return calls, replies


def connect(client, **overrides):
    data = {"instance": "demo", "username": "", "password": "", "knowledge_base": "", "enabled": "1"}
    data.update(overrides)
    return client.post("/admin/servicenow/ACME", data=data)


def raise_doubt(client, title="Which GL account for vendor credit notes?", detail="Tried 4000 in FB65"):
    client.post(f"{A}/doubts/new", data={"title": title, "detail": detail})
    return Question.query.order_by(Question.id.desc()).first().id


def test_keywords_weigh_title_and_keep_codes():
    words = servicenow.keywords(("Which GL account for credit notes?", 3), ("Tried GL 4000 in FB65", 1))
    assert words == ["gl", "account", "credit", "notes", "4000", "fb65"]


def test_admin_connects_an_account(app, client):
    login(client, "lead")
    assert client.get("/admin/servicenow").status_code == 403
    client.get("/logout")
    login(client, "admin")
    assert b"Not connected" in client.get("/admin/servicenow").data
    resp = connect(client, instance="not a host", knowledge_base="xyz")
    assert resp.status_code == 400 and b"instance host" in resp.data and b"32 letters" in resp.data
    resp = connect(client, instance="https://acme.service-now.com/now/nav/ui", username="kite.integration")
    assert resp.status_code == 400 and b"password" in resp.data
    connect(client, instance="https://Acme.service-now.com/now/nav/ui", username="kite.integration",
            password="s3cret!", knowledge_base=SYS_ID.upper())
    with app.app_context():
        conn = ServiceNowConnection.query.one()
        assert (conn.instance, conn.knowledge_base, conn.enabled) == ("acme.service-now.com", SYS_ID, True)
        assert conn.password_enc and "s3cret" not in conn.password_enc
        event = AdminEvent.query.filter_by(action="SERVICENOW_UPDATED").one()
        assert "s3cret" not in event.detail
    connect(client, instance="acme.service-now.com", username="kite.integration")  # empty keeps the password
    with app.app_context():
        assert ServiceNowConnection.query.one().password_enc
    assert b"acme.service-now.com" in client.get("/admin/servicenow").data
    client.post("/admin/servicenow/ACME/remove")
    with app.app_context():
        assert ServiceNowConnection.query.count() == 0


def test_test_connection_reports_errors(app, client, fake_snow):
    calls, replies = fake_snow
    login(client, "admin")
    connect(client, instance="acme.service-now.com", username="u", password="p", knowledge_base=SYS_ID)
    replies[("GET", "/api/now/table/kb_knowledge")] = {"result": [{"number": "KB1"}]}
    replies[("GET", f"/api/now/table/kb_knowledge_base/{SYS_ID}")] = {"result": {"title": "IT"}}
    resp = client.post("/admin/servicenow/ACME/test", follow_redirects=True)
    assert b"Connected to acme.service-now.com" in resp.data and b"&#39;IT&#39;" in resp.data
    replies[("GET", "/api/now/table/kb_knowledge")] = servicenow.ServiceNowError("ServiceNow rejected the user name")
    assert b"Connection failed: ServiceNow rejected" in client.post("/admin/servicenow/ACME/test",
                                                                    follow_redirects=True).data


def test_doubt_shows_kb_only_when_connected(app, client):
    login(client, "mem1")
    with app.app_context():
        qid = raise_doubt(client)
    assert b"not connected to ServiceNow" in client.get(f"{A}/doubts/{qid}").data
    assert client.get(f"{A}/doubts/{qid}/kb").status_code == 404
    client.get("/logout")
    login(client, "admin")
    connect(client)
    client.get("/logout")
    login(client, "mem1")
    page = client.get(f"{A}/doubts/{qid}").data
    assert b"ServiceNow knowledge" in page and f"/doubts/{qid}/kb".encode() in page
    assert b"Publish the solution" not in page  # members do not publish


def test_suggestions_link_and_unlink(app, client):
    login(client, "admin")
    connect(client)
    client.get("/logout")
    login(client, "mem1")
    with app.app_context():
        qid = raise_doubt(client)
    fragment = client.get(f"{A}/doubts/{qid}/kb").data
    assert b"KB0010001" in fragment and b"credit notes" in fragment and b"This solves it" in fragment
    client.post(f"{A}/doubts/{qid}/kb/link", data={"number": "kb0010001"})
    with app.app_context():
        link = QuestionKBLink.query.one()
        assert (link.number, link.source, link.title) == ("KB0010001", "LINKED",
                                                          "Which GL account to use for vendor credit notes")
    assert b"KB0010001" not in client.get(f"{A}/doubts/{qid}/kb").data  # linked ones are not suggested again
    assert b"already linked" in client.post(f"{A}/doubts/{qid}/kb/link", data={"number": "KB0010001"},
                                            follow_redirects=True).data
    assert b"not a published article" in client.post(f"{A}/doubts/{qid}/kb/link", data={"number": "KB0099999"},
                                                     follow_redirects=True).data
    assert b"such as KB0010001" in client.post(f"{A}/doubts/{qid}/kb/link", data={"number": "INC001"},
                                               follow_redirects=True).data
    assert client.get(f"{A}/kb/demo/KB0010001").status_code == 200
    client.get("/logout")
    login(client, "viewer")
    assert client.get(f"{A}/doubts/{qid}/kb").status_code == 200
    assert client.post(f"{A}/doubts/{qid}/kb/link", data={"number": "KB0010002"}).status_code == 403
    client.get("/logout")
    login(client, "mem2")  # not in ACME
    assert client.get(f"{A}/doubts/{qid}/kb").status_code in (403, 404)
    client.get("/logout")
    login(client, "mem1")
    with app.app_context():
        link_id = QuestionKBLink.query.one().id
    client.post(f"{A}/doubts/{qid}/kb/{link_id}/remove")
    with app.app_context():
        assert QuestionKBLink.query.count() == 0


def test_suggestions_follow_the_solution(app, client):
    login(client, "admin")
    connect(client)
    client.get("/logout")
    login(client, "mem1")
    with app.app_context():
        qid = raise_doubt(client, title="Supplier money not sent", detail="Nothing paid last night")
    assert b"KB0010004" not in client.get(f"{A}/doubts/{qid}/kb").data
    client.get("/logout")
    login(client, "sme")
    client.post(f"{A}/doubts/{qid}/reply", data={"body": "Check the payment method in the vendor master (F110)"})
    servicenow._cache.clear()
    assert b"KB0010004" in client.get(f"{A}/doubts/{qid}/kb").data


def test_real_instance_query_and_publish(app, client, fake_snow):
    calls, replies = fake_snow
    login(client, "admin")
    connect(client, instance="acme.service-now.com", username="u", password="p", knowledge_base=SYS_ID)
    client.get("/logout")
    login(client, "mem1")
    with app.app_context():
        qid = raise_doubt(client, detail="Tried 4000 <b>bold</b>")
    replies[("GET", "/api/now/table/kb_knowledge")] = {"result": [
        {"number": "KB0012345", "short_description": "Credit notes", "text": "<p>Use&nbsp;4100</p>",
         "sys_updated_on": "2026-10-01 10:00:00"}]}
    fragment = client.get(f"{A}/doubts/{qid}/kb").data
    assert b"https://acme.service-now.com/kb_view.do?sysparm_article=KB0012345" in fragment
    assert b"Use 4100" in fragment
    method, path, params, _ = calls[-1]
    assert "workflow_state=published" in params["sysparm_query"]
    assert "123TEXTQUERY321=" in params["sysparm_query"] and " OR " in params["sysparm_query"]
    assert params["sysparm_query"].endswith(f"^kb_knowledge_base={SYS_ID}")
    client.get(f"{A}/doubts/{qid}/kb")
    assert len(calls) == 1  # cached

    assert client.post(f"{A}/doubts/{qid}/kb/publish").status_code == 403  # members do not publish
    client.get("/logout")
    login(client, "sme")
    assert b"Answer the doubt first" in client.post(f"{A}/doubts/{qid}/kb/publish", follow_redirects=True).data
    client.post(f"{A}/doubts/{qid}/reply", data={"body": "Use 4100 & document type KG"})
    replies[("POST", "/api/now/table/kb_knowledge")] = {"result": {"number": "KB0020001", "sys_id": "a" * 32}}
    assert b"Created draft KB0020001" in client.post(f"{A}/doubts/{qid}/kb/publish", follow_redirects=True).data
    method, path, _, body = calls[-1]
    assert (method, body["workflow_state"], body["kb_knowledge_base"]) == ("POST", "draft", SYS_ID)
    assert "&lt;b&gt;bold&lt;/b&gt;" in body["text"] and "4100 &amp; document type KG" in body["text"]
    assert "Q-001 (ACME)" in body["text"] and "answered by Sme" in body["text"]
    with app.app_context():
        link = QuestionKBLink.query.one()
        assert (link.source, link.url) == ("PUBLISHED", f"https://acme.service-now.com/kb_knowledge.do?sys_id={'a' * 32}")
    assert b"already published" in client.post(f"{A}/doubts/{qid}/kb/publish", follow_redirects=True).data

    replies[("GET", "/api/now/table/kb_knowledge")] = servicenow.ServiceNowError("Could not reach acme")
    servicenow._cache.clear()
    assert b"Could not reach acme" in client.get(f"{A}/doubts/{qid}/kb").data


def test_publish_needs_a_knowledge_base(app, client, fake_snow):
    login(client, "admin")
    connect(client, instance="acme.service-now.com", username="u", password="p")
    client.get("/logout")
    login(client, "sme")
    with app.app_context():
        qid = raise_doubt(client)
    assert b"No knowledge base is set" in client.post(f"{A}/doubts/{qid}/kb/publish", follow_redirects=True).data


def test_account_search_includes_servicenow(app, client):
    login(client, "mem1")
    assert b"ServiceNow knowledge base" not in client.get(f"{A}/search?q=credit").data
    client.get("/logout")
    login(client, "admin")
    connect(client)
    client.get("/logout")
    login(client, "mem1")
    assert b"ServiceNow knowledge base" in client.get(f"{A}/search?q=credit").data
    assert b"KB0010001" in client.get(f"{A}/search/kb?q=credit+notes").data
    assert b"No published ServiceNow article" in client.get(f"{A}/search/kb?q=credit+printer").data
