"""KT documents: workspace, folders, templates, safe HTML, versions, comments and @mentions."""
import pytest

import documents
import transcripts
from conftest import account_id, login
from models import DocFolder, DocumentComment, DocumentVersion, KTDocument, db

A = "/accounts/ACME/documents"


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def make_doc(client, title="AP runbook", body="<p>First</p>", **fields):
    client.post(f"{A}/new", data={"title": title, "body": body, **fields})
    return KTDocument.query.order_by(KTDocument.id.desc()).first().id


@pytest.mark.parametrize("dirty, clean", [
    ("<p onclick='x()'>Hi</p>", "<p>Hi</p>"),
    ("<script>alert(1)</script><p>ok</p>", "<p>ok</p>"),
    ("<style>p{}</style>ok", "ok"),
    ('<a href="javascript:alert(1)">x</a>', "x"),
    ('<a href="http://plain.example">x</a>', "x"),
    ('<a href="https://a.example/?q=1&amp;r=2" class="doc-embed other" onmouseover="y">x</a>',
     '<a href="https://a.example/?q=1&amp;r=2" target="_blank" rel="noopener noreferrer" class="doc-embed">x</a>'),
    ('<img src=x onerror=alert(1)>t', "t"),
    ('<iframe src="https://evil.example">inside</iframe>t', "t"),
    ('<svg><script>alert(1)</script></svg>t', "t"),
    ("<p>open <strong>bold", "<p>open <strong>bold</strong></p>"),
    ("&lt;script&gt;x&lt;/script&gt;", "&lt;script&gt;x&lt;/script&gt;"),
    ("<table><tbody><tr><td>a</td></tr></tbody></table>", "<table><tbody><tr><td>a</td></tr></tbody></table>"),
])
def test_sanitize(dirty, clean):
    assert documents.sanitize(dirty) == clean


def test_every_template_survives_cleaning_unchanged():
    for _, body in documents.TEMPLATES.values():
        assert documents.sanitize(body) == body


def test_create_view_edit_and_history(app, client):
    login(client, "mem1")
    assert b"KT plan" in client.get(A).data and b"RACI matrix" in client.get(A).data
    assert b"Hand-over checklist" in client.get(f"{A}/new?template=handover").data
    with app.app_context():
        doc_id = make_doc(client, body="<p>First</p><script>alert(1)</script>")
        assert KTDocument.query.one().body == "<p>First</p>"
    page = client.get(f"{A}/{doc_id}").data
    assert b"<p>First</p>" in page and b"alert(1)" not in page
    client.post(f"{A}/{doc_id}/edit", data={"title": "AP runbook", "body": "<p>Second</p>", "base": "1"})
    client.post(f"{A}/{doc_id}/edit", data={"title": "AP runbook", "body": "<p>Second</p>", "base": "2"})
    with app.app_context():
        assert DocumentVersion.query.count() == 2  # a save without changes adds no version
    assert b"First" in client.get(f"{A}/{doc_id}/history?version=1").data
    client.post(f"{A}/{doc_id}/history/1/restore")
    with app.app_context():
        doc = db.session.get(KTDocument, doc_id)
        assert doc.body == "<p>First</p>" and doc.versions[-1].number == 3
    assert client.get(f"{A}/{doc_id}/history?version=99").status_code == 404


def test_simultaneous_saves_are_refused(app, client):
    login(client, "mem1")
    with app.app_context():
        doc_id = make_doc(client)
    client.post(f"{A}/{doc_id}/edit", data={"title": "T", "body": "<p>Theirs</p>", "base": "1"})
    resp = client.post(f"{A}/{doc_id}/edit", data={"title": "T", "body": "<p>Mine</p>", "base": "1"})
    assert resp.status_code == 400 and b"while you were editing" in resp.data and b"Mine" in resp.data
    with app.app_context():
        assert db.session.get(KTDocument, doc_id).body == "<p>Theirs</p>"


def test_validation(app, client):
    login(client, "mem1")
    assert client.post(f"{A}/new", data={"title": "", "body": "x"}).status_code == 400
    assert client.post(f"{A}/new", data={"title": "T", "body": "x" * (documents.MAX_BODY + 1)}).status_code == 400


def test_folders_and_search(app, client):
    login(client, "lead")
    client.post(f"{A}/folders", data={"name": "Payables"})
    assert b"already a folder" in client.post(f"{A}/folders", data={"name": "payables"}, follow_redirects=True).data
    with app.app_context():
        folder_id = DocFolder.query.one().id
        inside = make_doc(client, title="Inside", body="<p>F110 steps</p>", folder_id=folder_id)
        make_doc(client, title="Outside", body="<p>Other</p>")
    page = client.get(f"{A}?folder={folder_id}").data
    assert b"Inside" in page and b"Outside" not in page
    page = client.get(f"{A}?q=F110").data
    assert b"Inside" in page and b"Outside" not in page
    assert b"Outside" not in client.get(f"{A}?q=%25%25x").data  # % is a plain character in a search
    client.post(f"{A}/folders/{folder_id}/delete")
    with app.app_context():
        assert DocFolder.query.count() == 0 and db.session.get(KTDocument, inside).folder_id is None


def test_foreign_folder_is_ignored(app, client):
    login(client, "sme")
    client.post("/accounts/BETA/documents/folders", data={"name": "Beta only"})
    with app.app_context():
        foreign = DocFolder.query.one().id
        doc_id = make_doc(client, folder_id=foreign)
        assert db.session.get(KTDocument, doc_id).folder_id is None


def test_permissions(app, client):
    login(client, "mem1")
    with app.app_context():
        doc_id = make_doc(client)
    client.get("/logout")
    login(client, "viewer")
    assert client.get(f"{A}/{doc_id}").status_code == 200
    assert client.post(f"{A}/new", data={"title": "T", "body": ""}).status_code == 403
    assert client.post(f"{A}/{doc_id}/edit", data={"title": "T", "body": "", "base": "1"}).status_code == 403
    assert client.post(f"{A}/{doc_id}/comments", data={"body": "x"}).status_code == 403
    assert client.post(f"{A}/{doc_id}/delete").status_code == 403
    assert b"New folder" not in client.get(A).data
    client.get("/logout")
    login(client, "sme")  # may write, but did not write this one and does not manage the account
    assert client.post(f"{A}/{doc_id}/delete").status_code == 403
    client.get("/logout")
    login(client, "mem2")  # another account
    assert client.get(f"{A}/{doc_id}").status_code in (403, 404)
    assert client.get(f"/accounts/BETA/documents/{doc_id}").status_code == 404
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{A}/{doc_id}/delete")
    with app.app_context():
        assert KTDocument.query.count() == DocumentVersion.query.count() == 0


def test_comments_and_mentions(app, client, sent):
    login(client, "mem1")
    with app.app_context():
        doc_id = make_doc(client)
    client.post(f"{A}/{doc_id}/comments", data={
        "body": "Please check @sme @mem2 @mem1 <b>now</b>", "quote": "First"})
    assert len(sent) == 1 and sent[0][0] == ["sme@example.com"]  # not outsiders, not yourself
    page = client.get(f"{A}/{doc_id}").data
    assert b"&lt;b&gt;now" in page and b"<blockquote>First</blockquote>" in page
    with app.app_context():
        cid = DocumentComment.query.one().id
    client.post(f"{A}/{doc_id}/comments/{cid}/resolve")
    with app.app_context():
        assert DocumentComment.query.one().resolved_at
    assert client.post(f"{A}/{doc_id}/comments/999/resolve").status_code == 404


def test_word_export(app, client):
    login(client, "mem1")
    with app.app_context():
        doc_id = make_doc(client, title='A/B: "runbook"', body="<h1>One</h1><ul><li>Two &amp; three</li></ul>")
    resp = client.get(f"{A}/{doc_id}/document.docx")
    assert resp.status_code == 200 and "/" not in resp.headers["Content-Disposition"].split("filename=")[1]
    text = transcripts.read_file("x.docx", resp.data)
    assert "One" in text and "Two & three" in text


def test_account_page_links_to_documents(app, client):
    login(client, "mem1")
    assert f'href="{A}"'.encode() in client.get("/accounts/ACME").data


def test_it_templates_are_offered(app, client):
    login(client, "mem1")
    page = client.get(A).data
    for name in (b"Application KT", b"Architecture and environments", b"Deployment and release",
                 b"Incident and on-call", b"Access matrix"):
        assert name in page
