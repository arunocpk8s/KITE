"""The two project workflows, and meeting transcripts: reading, reviewing and creating items from them."""
import io
import json
import os
from types import SimpleNamespace

import pytest

import meetings
import transcripts
from conftest import account_id, login
from models import (
    INTERNAL_CHECKLIST, Account, AccountMember, AdminEvent, KTProject, MeetingTranscript, ProjectDocument, Question,
    ScopeItem, ServiceNowConnection, Sop, TranscriptItem, User, db, ensure_phases,
)

A = "/accounts/ACME"
SAMPLE = os.path.join(os.path.dirname(__file__), "sample_transcript.vtt")


def sample_bytes():
    with open(SAMPLE, "rb") as f:
        return f.read()


def make_project(app, **fields):
    with app.app_context():
        project = KTProject(account_id=account_id("ACME"), name="Payables", **fields)
        ensure_phases(project)
        db.session.add(project)
        db.session.commit()
        return project.id


def upload(client, pid, **fields):
    data = {"title": "AP KT", "meeting_date": "2026-11-02", "file": (io.BytesIO(sample_bytes()), "kt.vtt")}
    data.update(fields)
    return client.post(f"{A}/projects/{pid}/meetings/new", data=data, content_type="multipart/form-data")


def items_of(app):
    with app.app_context():
        return [(i.id, i.kind, i.title) for i in TranscriptItem.query.order_by(TranscriptItem.id)]


def create_all(app, client, pid, kinds=None):
    """Tick every proposed item, or only those of the given kinds."""
    with app.app_context():
        transcript = MeetingTranscript.query.one()
        tid = transcript.id
        data = {f"item-{i.id}": "1" for i in transcript.items if kinds is None or i.kind in kinds}
    return client.post(f"{A}/projects/{pid}/meetings/{tid}/create", data=data, follow_redirects=True)


# --- workflows -----------------------------------------------------------------------------------
def test_internal_workflow_has_its_own_checklist_and_phases(app, client):
    login(client, "lead")
    client.post(f"{A}/projects/new", data={"name": "Existing AP", "workflow": "INTERNAL_KT"})
    with app.app_context():
        project = KTProject.query.one()
        pid = project.id
        assert [i.title for i in project.checklist] == list(INTERNAL_CHECKLIST)
        assert [p.label for p in project.phases] == ["Internal KT", "Shadow", "Reverse shadow", "Onboarded to the team"]
    page = client.get(f"{A}/projects/{pid}").data
    assert b"Existing Project KT" in page and b"Onboard the new team members" in page
    client.post(f"{A}/projects/{pid}/edit", data={"name": "Renamed", "workflow": "CLIENT_KT"})
    with app.app_context():  # the workflow is fixed once chosen
        assert KTProject.query.one().workflow == "INTERNAL_KT"


def test_client_workflow_is_the_default_and_covers_the_requested_items(app, client):
    login(client, "lead")
    assert b"New Client Transaction KT" in client.get(f"{A}/projects/new").data
    client.post(f"{A}/projects/new", data={"name": "New AP"})
    with app.app_context():
        project = KTProject.query.one()
        assert project.workflow == "CLIENT_KT" and project.phases[-1].label == "Steady state"
        text = " ".join(i.title for i in project.checklist).lower()
    for word in ("scope", "raci", "record", "document", "runbook", "sop"):
        assert word in text
    assert client.post(f"{A}/projects/new", data={"name": "X", "workflow": "NOPE"}).status_code == 400


def test_home_page_has_two_plain_track_buttons(app, client):
    make_project(app, workflow="INTERNAL_KT")
    login(client, "lead")
    page = client.get("/").data.decode()
    assert page.index("New Client Transaction KT") < page.index("Existing Project KT") < page.index("My next steps")
    nav = page[page.index('class="track-grid"'):page.index("My next steps")]
    assert nav.count("track-button") == 2 and "/tracks/CLIENT_KT" in nav and "/tracks/INTERNAL_KT" in nav
    assert "Payables" not in nav and "in progress" not in nav  # names only: no status, no explanation
    assert "Track 1" not in page and "Track 2" not in page


def test_track_page_lists_only_its_projects_and_starts_with_the_track_selected(app, client):
    make_project(app, workflow="INTERNAL_KT")
    login(client, "lead")
    assert "Payables" in client.get("/tracks/INTERNAL_KT").data.decode()
    page = client.get("/tracks/CLIENT_KT").data.decode()
    assert "Payables" not in page and "No projects on this track" in page and "Add project" in page
    assert client.get("/tracks/NOPE").status_code == 404
    resp = client.get("/tracks/INTERNAL_KT/new?account=ACME")
    assert resp.status_code == 302 and "track=INTERNAL_KT" in resp.headers["Location"]
    form = client.get(resp.headers["Location"]).data.decode()
    assert 'value="INTERNAL_KT" checked' in form and 'value="CLIENT_KT" checked' not in form
    assert client.get("/tracks/NOPE/new?account=ACME").status_code == 404


def test_only_those_who_can_add_projects_can_start_one(app, client):
    make_project(app)
    login(client, "mem1")
    page = client.get("/tracks/CLIENT_KT").data.decode()
    assert "Payables" in page and "Add project" not in page
    assert client.get("/tracks/CLIENT_KT/new?account=ACME", follow_redirects=True).status_code == 403
    client.get("/logout")
    login(client, "mem2")  # projects of other accounts stay hidden
    assert "Payables" not in client.get("/tracks/CLIENT_KT").data.decode()
    assert client.get("/tracks/CLIENT_KT/new?account=ACME", follow_redirects=True).status_code in (403, 404)


# --- reading transcripts -------------------------------------------------------------------------
def test_read_file_formats():
    text = transcripts.read_file("kt.vtt", sample_bytes())
    assert "WEBVTT" not in text and "-->" not in text and "<v" not in text
    assert text.splitlines()[0].startswith("Maria Lopez: Welcome")
    assert transcripts.read_file("kt.txt", "Hello".encode("cp1252")) == "Hello"
    docx = transcripts.word_document("T & co", "sub", "Line <one>\nLine two")
    assert transcripts.read_file("kt.docx", docx) == "T & co\nsub\nLine <one>\nLine two"
    with pytest.raises(transcripts.TranscriptError):
        transcripts.read_file("kt.pdf", b"x")
    with pytest.raises(transcripts.TranscriptError):
        transcripts.read_file("kt.docx", b"not a zip")


def test_rules_find_what_is_plainly_said():
    found = transcripts.extract_with_rules(transcripts.read_file("kt.vtt", sample_bytes()))
    kinds = {}
    for item in found:
        kinds.setdefault(item["kind"], []).append(item)
    assert "Vendor invoice" in kinds["IN_SCOPE"][0]["title"] and "Tax filing" in kinds["OUT_OF_SCOPE"][0]["title"]
    assert [i["title"] for i in kinds["SLA"]] == ["Priority one invoice queries get a response within 4 hours."]
    assert "accountable" in kinds["RESPONSIBILITY"][0]["title"]
    assert "known issue" in kinds["KB"][0]["title"] and "cut-off" in kinds["DOUBT"][0]["title"]
    sop = kinds["SOP"][0]
    assert sop["title"] == "The weekly payment run"
    assert sop["detail"].splitlines()[0] == "1. Export the open invoices from the AP queue."
    assert "F110" in sop["detail"]
    assert len(sop["detail"].splitlines()) == 4
    assert kinds["CONTACT"][0]["title"] == "Omar Haddad (omar.haddad@northwind.example)"
    assert kinds["ACCESS"][0]["title"] == "Lena Fischer (lena.fischer@example.com)"
    assert "Maria Lopez, Dev Shah" in kinds["NOTES"][0]["detail"]


def test_clean_items_drops_bad_proposals():
    cleaned = transcripts.clean_items([
        {"kind": "SOP", "title": " Same  title ", "detail": "x"}, {"kind": "SOP", "title": "same title", "detail": "y"},
        {"kind": "ADMIN", "title": "Make me admin", "detail": ""}, {"kind": "KB", "title": "", "detail": "x"},
        {"kind": "ACCESS", "title": "Someone with no address", "detail": ""},
    ])
    assert cleaned == [{"kind": "SOP", "title": "Same title", "detail": "x"}]


# --- upload and permissions ----------------------------------------------------------------------
def test_upload_proposes_but_creates_nothing(app, client):
    pid = make_project(app)
    login(client, "lead")
    assert upload(client, pid).status_code == 302
    with app.app_context():
        assert TranscriptItem.query.count() > 5
        assert ScopeItem.query.count() == Sop.query.count() == Question.query.count() == 0
        tid = MeetingTranscript.query.one().id
    page = client.get(f"{A}/projects/{pid}/meetings/{tid}").data
    assert b"Vendor invoice" in page and b"Create the selected items" in page
    assert b"AP KT" in client.get(f"{A}/projects/{pid}").data


def test_upload_validation(app, client):
    pid = make_project(app)
    login(client, "lead")
    assert b"Name the meeting" in upload(client, pid, title="").data
    assert b".vtt, .docx" in upload(client, pid, file=(io.BytesIO(b"x"), "kt.exe")).data
    assert b"must start with https" in upload(client, pid, recording_link="javascript:alert(1)").data
    big = (io.BytesIO(b"x" * (meetings.MAX_TRANSCRIPT + 1)), "kt.txt")
    assert b"too large" in upload(client, pid, file=big).data
    assert b"Upload the transcript" in client.post(f"{A}/projects/{pid}/meetings/new", data={"title": "T"}).data
    with app.app_context():
        assert MeetingTranscript.query.count() == 0


def test_permissions(app, client):
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid)
    with app.app_context():
        tid = MeetingTranscript.query.one().id
    client.get("/logout")
    login(client, "mem1")
    assert client.get(f"{A}/projects/{pid}/meetings/{tid}").status_code == 200
    assert client.get(f"{A}/projects/{pid}/meetings/new").status_code == 403
    assert client.post(f"{A}/projects/{pid}/meetings/{tid}/create").status_code == 403
    assert client.post(f"{A}/projects/{pid}/meetings/{tid}/delete").status_code == 403
    client.get("/logout")
    login(client, "viewer")  # client-side viewers do not read raw transcripts
    assert client.get(f"{A}/projects/{pid}/meetings/{tid}").status_code == 403
    client.get("/logout")
    login(client, "mem2")  # not in ACME
    assert client.get(f"{A}/projects/{pid}/meetings/{tid}").status_code in (403, 404)


# --- creating items ------------------------------------------------------------------------------
def test_create_goes_to_each_home_and_only_once(app, client):
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid, recording_link="https://acme.sharepoint.com/rec.mp4")
    with app.app_context():
        transcript = MeetingTranscript.query.one()
        tid = transcript.id
        scope_item = next(i for i in transcript.items if i.kind == "IN_SCOPE")
        edited = {f"item-{scope_item.id}": "1", f"title-{scope_item.id}": "Invoice posting (edited)",
                  f"detail-{scope_item.id}": "Weekly"}
    client.post(f"{A}/projects/{pid}/meetings/{tid}/create", data=edited)
    with app.app_context():
        line = ScopeItem.query.one()  # only what was ticked, as edited
        assert (line.kind, line.title, line.detail) == ("IN_SCOPE", "Invoice posting (edited)", "Weekly")
    create_all(app, client, pid, kinds={"IN_SCOPE", "OUT_OF_SCOPE", "SLA", "RESPONSIBILITY", "CONTACT", "SOP", "RUNBOOK",
                                        "DOUBT", "NOTES"})
    with app.app_context():
        kinds = {s.kind for s in ScopeItem.query.all()}
        assert kinds == {"IN_SCOPE", "OUT_OF_SCOPE", "SLA", "RESPONSIBILITY", "CONTACT"}
        sop = Sop.query.one()
        assert sop.status == "DRAFT" and sop.project_id == pid and sop.versions[0].status == "DRAFT"
        assert "Export the open invoices" in sop.versions[0].steps
        assert Question.query.one().project_id == pid
        types = {d.doc_type for d in ProjectDocument.query.all()}
        assert types == {"Recording", "Process document"}  # SharePoint off: the SOP is a download, not registered
        before = ScopeItem.query.count()
    create_all(app, client, pid, kinds={"IN_SCOPE"})
    with app.app_context():
        assert ScopeItem.query.count() == before  # created items are not created again
    assert b"Created (" in client.get(f"{A}/projects/{pid}/meetings/{tid}").data


def test_word_document_download(app, client):
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid)
    with app.app_context():
        transcript = MeetingTranscript.query.one()
        tid = transcript.id
        sop = next(i for i in transcript.items if i.kind == "SOP")
        sid, scope = sop.id, next(i for i in transcript.items if i.kind == "IN_SCOPE").id
    resp = client.get(f"{A}/projects/{pid}/meetings/{tid}/items/{sid}/document.docx")
    assert resp.status_code == 200 and "2026-11-02 SOP - " in resp.headers["Content-Disposition"]
    assert "Export the open invoices" in transcripts.read_file("x.docx", resp.data)
    assert client.get(f"{A}/projects/{pid}/meetings/{tid}/items/{scope}/document.docx").status_code == 404


def test_kb_needs_servicenow_and_can_be_retried(app, client, monkeypatch):
    pid = make_project(app)
    login(client, "admin")
    upload(client, pid)
    resp = create_all(app, client, pid, kinds={"KB"})
    assert b"not connected to ServiceNow" in resp.data and b"could not be created" in resp.data
    with app.app_context():
        assert TranscriptItem.query.filter_by(kind="KB").one().created_at is None
        db.session.add(ServiceNowConnection(account_id=account_id("ACME"), instance="demo", username="", enabled=True))
        db.session.commit()
    resp = create_all(app, client, pid, kinds={"KB"})
    assert b"Created 1 items" in resp.data
    with app.app_context():
        item = TranscriptItem.query.filter_by(kind="KB").one()
        assert item.created_at and item.result_label.endswith("draft in ServiceNow") and item.error == ""


def test_kb_body_is_escaped(app, client, monkeypatch):
    pid = make_project(app)
    bodies = []
    monkeypatch.setattr("servicenow.create_draft", lambda conn, title, body: bodies.append(body) or ("KB1", None))
    with app.app_context():
        db.session.add(ServiceNowConnection(account_id=account_id("ACME"), instance="demo", username="", enabled=True))
        db.session.commit()
    login(client, "lead")
    upload(client, pid, file=(io.BytesIO(b"Known issue here"), "kt.txt"))
    with app.app_context():
        item = TranscriptItem.query.filter_by(kind="KB").one()
        iid, tid = item.id, item.transcript_id
    client.post(f"{A}/projects/{pid}/meetings/{tid}/create", data={f"item-{iid}": "1", f"detail-{iid}": "<script>x</script>"})
    assert "&lt;script&gt;" in bodies[0] and "<script>" not in bodies[0]


def test_access_is_admin_only_and_never_preselected(app, client):
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid)
    with app.app_context():
        tid = MeetingTranscript.query.one().id
    page = client.get(f"{A}/projects/{pid}/meetings/{tid}").data.decode()
    assert "An admin creates sign-ins" in page
    resp = create_all(app, client, pid, kinds={"ACCESS"})  # a forged form from a KT lead
    assert b"Only an admin" in resp.data
    with app.app_context():
        assert User.query.filter_by(email="lena.fischer@example.com").first() is None
    client.get("/logout")
    login(client, "admin")
    page = client.get(f"{A}/projects/{pid}/meetings/{tid}").data.decode()
    access_id = next(i for i, kind, _ in items_of(app) if kind == "ACCESS")
    box = page[page.index(f'name="item-{access_id}"'):][:60]
    assert "checked" not in box
    create_all(app, client, pid, kinds={"ACCESS"})
    with app.app_context():
        user = User.query.filter_by(email="lena.fischer@example.com").one()
        assert (user.full_name, user.role, user.password_hash) == ("Lena Fischer", "member", "")
        assert AccountMember.query.filter_by(user_id=user.id, account_id=account_id("ACME")).count() == 1
        assert AdminEvent.query.filter_by(action="USER_CREATED").count() == 1


def test_access_links_an_existing_user_without_changing_their_role(app, client):
    pid = make_project(app)
    login(client, "admin")
    upload(client, pid, file=(io.BytesIO(b"Mem Two (mem2@example.com) needs access"), "kt.txt"))
    create_all(app, client, pid, kinds={"ACCESS"})
    with app.app_context():
        user = User.query.filter_by(username="mem2").one()
        assert user.role == "member" and user.can_view(db.session.get(Account, account_id("ACME")))


def test_sharepoint_upload_registers_the_document(app, client, monkeypatch):
    app.config["SHAREPOINT_ENABLED"] = True
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid)
    resp = create_all(app, client, pid, kinds={"NOTES"})
    assert b"SharePoint KT folder link first" in resp.data  # no folder set on the project
    with app.app_context():
        db.session.get(KTProject, pid).sharepoint_url = "https://acme.sharepoint.com/sites/kt/AP"
        db.session.commit()
    sent = []

    def fake_upload(folder, filename, data):
        sent.append((folder, filename, data))
        return "https://acme.sharepoint.com/sites/kt/AP/notes.docx"

    monkeypatch.setattr(meetings, "upload_to_sharepoint", fake_upload)
    create_all(app, client, pid, kinds={"NOTES", "SOP"})
    assert sent[0][0] == "https://acme.sharepoint.com/sites/kt/AP" and sent[0][1].endswith(".docx")
    assert {"\\", "/", ":"}.isdisjoint(sent[0][1])
    with app.app_context():
        assert {d.doc_type for d in ProjectDocument.query.all()} == {"SOP", "Process document"}
        assert all(d.in_sharepoint for d in ProjectDocument.query.all())
        assert Sop.query.one().versions[0].link.startswith("https://acme.sharepoint.com")


def test_delete_removes_the_transcript_but_keeps_what_was_created(app, client):
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid)
    create_all(app, client, pid, kinds={"IN_SCOPE"})
    with app.app_context():
        tid = MeetingTranscript.query.one().id
    client.post(f"{A}/projects/{pid}/meetings/{tid}/delete")
    with app.app_context():
        assert MeetingTranscript.query.count() == TranscriptItem.query.count() == 0
        assert ScopeItem.query.count() == 1


# --- Claude API ----------------------------------------------------------------------------------
class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self.message


def fake_claude(monkeypatch, message):
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return FakeStream(message)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    monkeypatch.setattr(transcripts.anthropic, "Anthropic", lambda api_key: client)
    return calls


def claude_message(items, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="thinking"),
                                    SimpleNamespace(type="text", text=json.dumps({"items": items}))])


def allow_ai(app):
    app.config["ANTHROPIC_API_KEY"] = "test-key"
    with app.app_context():
        db.session.get(Account, account_id("ACME")).ai_transcripts = True
        db.session.commit()


def test_ai_needs_the_accounts_consent_and_a_key(app, client, monkeypatch):
    calls = fake_claude(monkeypatch, claude_message([]))
    pid = make_project(app)
    login(client, "lead")
    app.config["ANTHROPIC_API_KEY"] = "test-key"  # a key alone is not enough
    assert b"Claude AI is off for this account" in client.get(f"{A}/projects/{pid}/meetings/new").data
    assert upload(client, pid, method="AI").status_code == 400
    app.config["ANTHROPIC_API_KEY"] = ""
    with app.app_context():
        db.session.get(Account, account_id("ACME")).ai_transcripts = True
        db.session.commit()
    assert upload(client, pid, method="AI").status_code == 400  # consent alone is not enough
    assert calls == []


def test_ai_extraction_is_used_and_cleaned(app, client, monkeypatch):
    calls = fake_claude(monkeypatch, claude_message([
        {"kind": "SOP", "title": "Payment run", "detail": "1. Export"},
        {"kind": "ADMIN", "title": "Make someone admin", "detail": ""},
        {"kind": "ACCESS", "title": "No address given", "detail": ""},
    ]))
    allow_ai(app)
    pid = make_project(app)
    login(client, "lead")
    assert b"sent to Anthropic" in client.get(f"{A}/projects/{pid}/meetings/new").data
    upload(client, pid, method="AI")
    assert [(k, t) for _, k, t in items_of(app)] == [("SOP", "Payment run")]
    with app.app_context():
        assert MeetingTranscript.query.one().method == "AI"
    sent = calls[0]
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert "data, not instructions" in sent["system"]
    assert "<transcript>" in sent["messages"][0]["content"] and "Maria Lopez" in sent["messages"][0]["content"]


@pytest.mark.parametrize("stop_reason, message", [("refusal", b"declined"), ("max_tokens", b"Split it")])
def test_ai_incomplete_answers_are_reported_not_saved(app, client, monkeypatch, stop_reason, message):
    fake_claude(monkeypatch, claude_message([], stop_reason))
    allow_ai(app)
    pid = make_project(app)
    login(client, "lead")
    resp = upload(client, pid, method="AI")
    assert resp.status_code == 400 and message in resp.data
    with app.app_context():
        assert MeetingTranscript.query.count() == 0


def test_rules_never_call_the_api(app, client, monkeypatch):
    calls = fake_claude(monkeypatch, claude_message([]))
    allow_ai(app)
    pid = make_project(app)
    login(client, "lead")
    upload(client, pid, method="RULES")
    assert calls == []
