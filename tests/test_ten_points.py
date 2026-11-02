"""KT plan by technology and process, simple add form, files, topics from Word, sign-off on the transition page,
onboarding delete, recording and transcript views, SOPs from a transcript, doubts for everyone, and the backlog."""
import io
import zipfile

import pytest

import transcripts
from conftest import account_id, login, user_id
from models import (
    BacklogItem, KTAttachment, KTModule, KTProject, KTTopic, LearningPath, MeetingTranscript, Onboarding, PathItem, Question,
    Sop, TranscriptItem, db,
)

A = "/accounts/ACME"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50


def make_project(app, name="Payments platform", workflow="CLIENT_KT", code="ACME"):
    with app.app_context():
        project = KTProject(account_id=account_id(code), name=name, workflow=workflow)
        db.session.add(project)
        db.session.commit()
        return project.id


def word(body):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as docx:
        docx.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return out.getvalue()


def para(text, style=None):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"


def table(*rows):
    cells = "".join("<w:tr>" + "".join(f"<w:tc>{para(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows)
    return f"<w:tbl>{cells}</w:tbl>"


# --- points 1, 5 and 7: add a KT plan simply, by technology or process, with its status ----------------------
def test_kt_plan_page_offers_add_kt_plan_to_kt_leads_only(app, client):
    login(client, "lead")
    assert "Add KT plan" in client.get(f"{A}/kt").data.decode()
    assert "Add KT plan" in client.get(A).data.decode()
    client.get("/logout")
    login(client, "mem1")
    assert "Add KT plan" not in client.get(f"{A}/kt").data.decode()


def test_simple_form_creates_the_plan_the_technology_and_the_topic(app, client):
    login(client, "lead")
    page = client.get(f"{A}/kt/topics/new").data.decode()
    assert "Technology or process" in page and "Status" in page
    resp = client.post(f"{A}/kt/topics/new", data={
        "new_plan": "Cloud migration", "new_workflow": "INTERNAL_KT", "area_kind": "TECHNOLOGY", "area_name": "Kubernetes",
        "title": "Cluster upgrades", "initial_status": "SCHEDULED"})
    assert resp.status_code == 302
    with app.app_context():
        project = KTProject.query.filter_by(name="Cloud migration").one()
        assert project.workflow == "INTERNAL_KT" and project.phases  # a full project, with its phases
        topic = KTTopic.query.one()
        assert topic.status == "SCHEDULED" and topic.module.title == "Kubernetes" and topic.module.kind == "TECHNOLOGY"
        pid = project.id
    # The same technology is reused, whatever its case; a process is its own group.
    client.post(f"{A}/kt/topics/new", data={"kt_project_id": pid, "area_kind": "TECHNOLOGY", "area_name": "kubernetes", "title": "Ingress"})
    client.post(f"{A}/kt/topics/new", data={"kt_project_id": pid, "area_kind": "PROCESS", "area_name": "Incident management", "title": "Sev 1 calls"})
    with app.app_context():
        assert sorted((m.title, m.kind, len(m.topics)) for m in KTModule.query) == [
            ("Incident management", "PROCESS", 1), ("Kubernetes", "TECHNOLOGY", 2)]
    plan = client.get(f"{A}/kt").data.decode()
    assert plan.index(">Technology</h3>") < plan.index("Kubernetes") < plan.index(">Process</h3>") < plan.index("Incident management")
    only = client.get(f"{A}/kt?area=PROCESS").data.decode()
    assert "Incident management" in only and "Kubernetes" not in only


@pytest.mark.parametrize("fields, message", [
    ({"area_name": "", "title": "T"}, "Name the technology or process"),
    ({"area_name": "A", "title": ""}, "Enter a topic title"),
    ({"area_name": "A", "title": "T", "area_kind": "NOPE"}, "Technology or Process"),
    ({"area_name": "A", "title": "T", "initial_status": "SIGNED_OFF"}, "Choose a starting status"),
])
def test_simple_form_validation(app, client, fields, message):
    pid = make_project(app)
    login(client, "lead")
    data = {"kt_project_id": pid, "area_kind": "TECHNOLOGY", "initial_status": "PLANNED", **fields}
    resp = client.post(f"{A}/kt/topics/new", data=data)
    assert resp.status_code == 400 and message in resp.data.decode()


def test_simple_form_refuses_a_plan_of_another_account(app, client):
    other = make_project(app, code="BETA")
    login(client, "lead")
    resp = client.post(f"{A}/kt/topics/new", data={"kt_project_id": other, "area_kind": "TECHNOLOGY", "area_name": "A", "title": "T"})
    assert resp.status_code == 400
    with app.app_context():
        assert KTModule.query.count() == 0


def test_status_can_be_changed_from_the_plan(app, client):
    pid = make_project(app)
    login(client, "lead")
    client.post(f"{A}/kt/topics/new", data={"kt_project_id": pid, "area_kind": "TECHNOLOGY", "area_name": "A", "title": "T"})
    with app.app_context():
        tid = KTTopic.query.one().id
    assert f'action="{A}/kt/topics/{tid}/status"' in client.get(f"{A}/kt").data.decode()
    resp = client.post(f"{A}/kt/topics/{tid}/status", data={"status": "SCHEDULED", "next": "plan"})
    assert resp.headers["Location"].endswith(f"{A}/kt")
    with app.app_context():
        assert KTTopic.query.one().status == "SCHEDULED"
    client.get("/logout")
    login(client, "mem1")
    assert f'action="{A}/kt/topics/{tid}/status"' not in client.get(f"{A}/kt").data.decode()


def test_it_modules_are_technology_and_process(app, client):
    login(client, "lead")
    client.post(f"{A}/projects/new", data={"name": "With", "it_modules": "1"})
    with app.app_context():
        kinds = {m.title: m.kind for m in KTModule.query}
    assert kinds["Architecture and environments"] == "TECHNOLOGY" and kinds["Support process and SLAs"] == "PROCESS"


# --- point 4: files, and topics from Word ------------------------------------------------------------------
def upload(client, pid, name="flow.png", data=PNG, **extra):
    return client.post(f"{A}/kt/files", data={"project_id": pid, "file": (io.BytesIO(data), name), **extra},
                       content_type="multipart/form-data")


def test_files_upload_view_and_remove(app, client):
    pid = make_project(app)
    login(client, "mem1")
    upload(client, pid)
    upload(client, pid, "plan.docx", word(para("x")))
    with app.app_context():
        picture, document = KTAttachment.query.order_by(KTAttachment.id).all()
        picture_id, document_id = picture.id, document.id
    page = client.get(f"{A}/kt").data.decode()
    assert "flow.png" in page and f'<img src="{A}/kt/files/{picture_id}"' in page
    shown = client.get(f"{A}/kt/files/{picture_id}")
    assert shown.data == PNG and shown.mimetype == "image/png" and "attachment" not in shown.headers.get("Content-Disposition", "")
    assert shown.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in client.get(f"{A}/kt/files/{document_id}").headers["Content-Disposition"]
    client.get("/logout")

    login(client, "sme")  # neither the uploader nor a KT lead
    assert client.post(f"{A}/kt/files/{picture_id}/delete").status_code == 403
    client.get("/logout")
    login(client, "mem2")  # another account
    assert client.get(f"{A}/kt/files/{picture_id}").status_code == 404
    client.get("/logout")
    login(client, "viewer")
    assert client.get(f"{A}/kt/files/{picture_id}").status_code == 200
    assert upload(client, pid).status_code == 403
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{A}/kt/files/{picture_id}/delete")
    with app.app_context():
        assert KTAttachment.query.count() == 1


@pytest.mark.parametrize("name, data, message", [
    ("evil.html", b"<script>", "Upload a Word"),
    ("evil.svg", b"<svg onload=x>", "Upload a Word"),
    ("fake.png", b"<script>alert(1)</script>", "not really"),
    ("large.png", b"\x89PNG" + b"0" * (4 * 1024 * 1024), "larger than 4 MB"),
], ids=["html", "svg", "fake-png", "too-large"])
def test_files_refused(app, client, name, data, message):
    pid = make_project(app)
    login(client, "mem1")
    page = client.post(f"{A}/kt/files", data={"project_id": pid, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", follow_redirects=True).data.decode()
    assert message in page
    with app.app_context():
        assert KTAttachment.query.count() == 0


def test_file_on_a_topic_of_another_project_is_refused(app, client):
    one, two = make_project(app), make_project(app, "Second")
    login(client, "lead")
    client.post(f"{A}/kt/topics/new", data={"kt_project_id": one, "area_kind": "TECHNOLOGY", "area_name": "A", "title": "T"})
    with app.app_context():
        tid = KTTopic.query.one().id
    assert upload(client, two, topic_id=tid).status_code == 404
    upload(client, one, topic_id=tid)
    assert "flow.png" in client.get(f"{A}/kt/topics/{tid}").data.decode()


def test_read_kt_outline():
    rows = transcripts.read_kt_outline(word(
        para("KT plan for payments", "Title") + para("Kubernetes", "Heading1") + para("Cluster upgrades")
        + para("Support process", "Heading2") + para("Sev 1 calls &amp; bridges")
        + para("This is a long paragraph of prose that explains the background of the whole transition in detail and it is certainly not a topic that anybody should have to deliver.")
        + table(("Technology", "KT topic", "Owner"), ("Ansible", "Playbooks", "Ravi"), ("", "Inventory", "Ravi"))))
    assert [(r["area"], r["kind"], r["title"]) for r in rows] == [
        ("Kubernetes", "TECHNOLOGY", "Cluster upgrades"),
        ("Support process", "PROCESS", "Sev 1 calls & bridges"),
        ("Ansible", "TECHNOLOGY", "Playbooks"),
        ("Ansible", "TECHNOLOGY", "Inventory"),
    ]
    with pytest.raises(transcripts.TranscriptError):
        transcripts.read_kt_outline(b"not a word file")


def test_topics_from_word_are_checked_first(app, client):
    pid = make_project(app)
    login(client, "mem1")
    data = {"project_id": pid, "file": (io.BytesIO(word(para("Kubernetes", "Heading1") + para("Upgrades") + para("Ingress"))), "plan.docx")}
    assert client.post(f"{A}/kt/files/import", data=data, content_type="multipart/form-data").status_code == 403
    client.get("/logout")
    login(client, "lead")
    data = {"project_id": pid, "file": (io.BytesIO(word(para("Kubernetes", "Heading1") + para("Upgrades") + para("Ingress"))), "plan.docx")}
    page = client.post(f"{A}/kt/files/import", data=data, content_type="multipart/form-data").data.decode()
    assert 'value="Upgrades"' in page
    with app.app_context():
        assert KTTopic.query.count() == 0 and KTAttachment.query.count() == 1  # nothing yet, but the file is kept
    client.post(f"{A}/kt/files/import/create", data={
        "project_id": pid, "rows": 2, "use-0": "1", "kind-0": "TECHNOLOGY", "area-0": "Kubernetes", "title-0": "Upgrades",
        "kind-1": "TECHNOLOGY", "area-1": "Kubernetes", "title-1": "Ingress"})
    with app.app_context():
        assert [t.title for t in KTTopic.query] == ["Upgrades"]
    bad = {"project_id": pid, "file": (io.BytesIO(b"\xd0\xcf\x11\xe0old"), "old.doc")}
    assert "Save it as .docx" in client.post(f"{A}/kt/files/import", data=bad, content_type="multipart/form-data", follow_redirects=True).data.decode()


# --- point 2: client sign-off and internal KT moved to the transition page ----------------------------------
def test_sign_off_and_internal_kt_are_on_the_transition_page(app, client):
    pid = make_project(app)
    login(client, "lead")
    project = client.get(f"{A}/projects/{pid}").data.decode()
    assert 'id="sign-off"' not in project and 'id="internal"' not in project and "Internal sessions" not in project
    assert f"{A}/projects/{pid}/transition" in project
    transition = client.get(f"{A}/projects/{pid}/transition").data.decode()
    assert 'id="sign-off"' in transition and 'id="internal"' in transition
    account = client.get(A).data.decode()
    assert "<th>Client sign-off</th>" not in account and "<th>Internal KT</th>" not in account
    resp = client.post(f"{A}/projects/{pid}/sign-off/request")
    assert resp.status_code in (302, 400)


# --- point 3: onboarding ----------------------------------------------------------------------------------
def make_path(app, items=True):
    with app.app_context():
        path = LearningPath(account_id=account_id("ACME"), name="New starter")
        if items:
            path.items.append(PathItem(position=1, kind="LINK", title="Read this", url="https://x.example"))
        db.session.add(path)
        db.session.commit()
        return path.id


def test_add_onboarding_is_always_offered_to_kt_leads(app, client):
    login(client, "lead")
    page = client.get(f"{A}/onboarding").data.decode()
    assert "Add onboarding" in page and "Add a learning path first" in page
    make_path(app)
    page = client.get(f"{A}/onboarding").data.decode()
    assert "Assign and email them" in page
    client.get("/logout")
    login(client, "mem1")
    assert "Add onboarding" not in client.get(f"{A}/onboarding").data.decode()


def test_delete_onboarding_and_learning_path(app, client):
    path_id = make_path(app)
    with app.app_context():
        ob = Onboarding(account_id=account_id("ACME"), path_id=path_id, newcomer_id=user_id("mem1"), buddy_id=user_id("sme"),
                        assigned_by_id=user_id("lead"))
        db.session.add(ob)
        db.session.commit()
        ob_id = ob.id
    login(client, "mem1")
    assert client.post(f"{A}/onboarding/{ob_id}/delete").status_code == 403
    assert client.post(f"{A}/onboarding/paths/{path_id}/delete").status_code == 403
    client.get("/logout")
    login(client, "lead")
    assert f"/onboarding/{ob_id}/delete" in client.get(f"{A}/onboarding").data.decode()
    # A path somebody follows stays until they are removed.
    page = client.post(f"{A}/onboarding/paths/{path_id}/delete", follow_redirects=True).data.decode()
    assert "still follow" in page
    with app.app_context():
        assert db.session.get(LearningPath, path_id) is not None
    client.post(f"{A}/onboarding/{ob_id}/delete")
    client.post(f"{A}/onboarding/paths/{path_id}/delete")
    with app.app_context():
        assert Onboarding.query.count() == 0 and LearningPath.query.count() == 0 and PathItem.query.count() == 0


# --- point 6: recording and transcript --------------------------------------------------------------------
def make_transcript(app, pid, text="Priya: first do this\nThen that\nFinally the rest", link="https://rec.example/1"):
    with app.app_context():
        transcript = MeetingTranscript(project_id=pid, title="KT session", text=text, recording_link=link,
                                       uploaded_by_id=user_id("lead"))
        db.session.add(transcript)
        db.session.commit()
        return transcript.id


def test_recording_and_transcript_can_be_opened_from_the_project(app, client):
    pid = make_project(app)
    tid = make_transcript(app, pid)
    base = f"{A}/projects/{pid}/meetings/{tid}"
    login(client, "mem1")
    project = client.get(f"{A}/projects/{pid}").data.decode()
    assert "https://rec.example/1" in project and f"{base}/transcript" in project
    page = client.get(f"{base}/transcript").data.decode()
    assert "Priya: first do this" in page and "Open the recording" in page
    download = client.get(f"{base}/transcript?download=1")
    assert b"Priya: first do this" in download.data and "attachment" in download.headers["Content-Disposition"]
    assert client.post(f"{base}/recording", data={"recording_link": "https://rec.example/2"}).status_code == 403
    client.get("/logout")
    login(client, "lead")
    assert 'name="recording_link"' in client.get(base).data.decode()
    client.post(f"{base}/recording", data={"recording_link": "javascript:alert(1)"})
    with app.app_context():
        assert db.session.get(MeetingTranscript, tid).recording_link == "https://rec.example/1"
    client.post(f"{base}/recording", data={"recording_link": "https://rec.example/2"})
    with app.app_context():
        assert db.session.get(MeetingTranscript, tid).recording_link == "https://rec.example/2"


# --- point 10: SOPs and scope from a transcript -------------------------------------------------------------
STEPS = "Priya: the process for patching is as follows\nPriya: first drain the node\nPriya: then patch it\nPriya: finally restart it\nPriya: payroll is in scope"


def test_sop_page_offers_it_to_kt_leads_only(app, client):
    login(client, "lead")
    assert f"{A}/from-recording/sop" in client.get(f"{A}/sops").data.decode()
    assert f"{A}/from-recording/scope" in client.get(f"{A}/scope").data.decode()
    client.get("/logout")
    login(client, "sme")
    assert f"{A}/from-recording/sop" not in client.get(f"{A}/sops").data.decode()
    assert client.get(f"{A}/from-recording/sop").status_code == 403
    login(client, "lead")
    assert client.get(f"{A}/from-recording/nope").status_code == 404


def test_new_transcript_proposes_only_sops_and_creates_a_draft(app, client):
    pid = make_project(app)
    login(client, "lead")
    resp = client.post(f"{A}/from-recording/sop", data={"project_id": pid, "title": "Patching KT", "pasted": STEPS,
                                                        "recording_link": "https://rec.example/1", "method": "RULES"})
    assert resp.status_code == 302 and "/meetings/" in resp.headers["Location"]
    with app.app_context():
        items = TranscriptItem.query.all()
        assert [i.kind for i in items] == ["SOP"]  # no scope, no notes
        item_id, tid = items[0].id, items[0].transcript_id
        assert db.session.get(MeetingTranscript, tid).recording_link == "https://rec.example/1"
    client.post(f"{A}/projects/{pid}/meetings/{tid}/create", data={f"item-{item_id}": "1", f"title-{item_id}": "Patching", f"detail-{item_id}": "1. Drain"})
    with app.app_context():
        sop = Sop.query.one()
        assert sop.title == "Patching" and sop.versions[0].status == "DRAFT"


def test_runbook_found_on_the_sop_page_becomes_an_sop(app, client):
    pid = make_project(app)
    login(client, "lead")
    assert "restart" in STEPS  # the rules would call this a runbook
    client.post(f"{A}/from-recording/sop", data={"project_id": pid, "title": "Patching KT", "pasted": STEPS})
    with app.app_context():
        assert TranscriptItem.query.one().kind == "SOP"


def test_existing_transcript_is_reused_for_scope_without_repeats(app, client):
    pid = make_project(app)
    tid = make_transcript(app, pid, STEPS)
    login(client, "lead")
    for _ in range(2):
        resp = client.post(f"{A}/from-recording/scope", data={"project_id": pid, "transcript_id": tid})
        assert f"/meetings/{tid}" in resp.headers["Location"] and "focus=scope" in resp.headers["Location"]
    with app.app_context():
        assert [i.kind for i in TranscriptItem.query] == ["IN_SCOPE"]
        assert MeetingTranscript.query.count() == 1


def test_ai_is_used_only_when_allowed_and_nothing_else_is_kept(app, client, monkeypatch):
    pid = make_project(app)
    login(client, "lead")
    resp = client.post(f"{A}/from-recording/sop", data={"project_id": pid, "title": "T", "pasted": STEPS, "method": "AI"})
    assert resp.status_code == 400 and "not switched on" in resp.data.decode()
    assert "Claude AI is off for this account" in client.get(f"{A}/from-recording/sop").data.decode()

    from models import Account
    with app.app_context():
        Account.query.filter_by(code="ACME").one().ai_transcripts = True
        db.session.commit()
    app.config["ANTHROPIC_API_KEY"] = "test-key"
    seen = {}

    def fake_ai(text, key):
        seen["text"], seen["key"] = text, key
        return [{"kind": "SOP", "title": "From Claude", "detail": "1. Step"}, {"kind": "ACCESS", "title": "A (a@b.example)", "detail": ""}]
    monkeypatch.setattr(transcripts, "extract_with_ai", fake_ai)
    assert 'value="AI" checked' in client.get(f"{A}/from-recording/sop").data.decode()
    client.post(f"{A}/from-recording/sop", data={"project_id": pid, "title": "T", "pasted": STEPS, "method": "AI"})
    assert seen == {"text": STEPS, "key": "test-key"}
    with app.app_context():
        assert [(i.kind, i.title) for i in TranscriptItem.query] == [("SOP", "From Claude")]
        assert db.session.get(MeetingTranscript, TranscriptItem.query.one().transcript_id).method == "AI"


def test_from_recording_stays_inside_the_account(app, client):
    other = make_project(app, code="BETA")
    tid = make_transcript(app, other)
    mine = make_project(app)
    login(client, "lead")
    assert client.post(f"{A}/from-recording/sop", data={"project_id": other, "title": "T", "pasted": STEPS}).status_code == 400
    assert client.post(f"{A}/from-recording/sop", data={"project_id": mine, "transcript_id": tid}).status_code == 400
    assert "KT session" not in client.get(f"{A}/from-recording/sop").data.decode()


# --- point 8: raising a doubt ------------------------------------------------------------------------------
@pytest.mark.parametrize("username", ["mem1", "sme", "lead", "viewer"])
def test_everyone_finds_raise_a_doubt_on_every_workspace_page(app, client, username):
    login(client, username)
    for path in (A, f"{A}/kt", f"{A}/sops", f"{A}/doubts"):
        assert f'href="{A}/doubts/new"' in client.get(path).data.decode(), path
    assert client.get(f"{A}/doubts/new").status_code == 200


def test_viewer_raises_a_doubt_and_replies_only_on_their_own(app, client):
    login(client, "mem1")
    client.post(f"{A}/doubts/new", data={"title": "Theirs"})
    client.get("/logout")
    login(client, "viewer")
    assert client.post(f"{A}/doubts/new", data={"title": "Mine"}).status_code == 302
    with app.app_context():
        mine = Question.query.filter_by(title="Mine").one().id
        theirs = Question.query.filter_by(title="Theirs").one().id
    assert client.post(f"{A}/doubts/{mine}/reply", data={"body": "More detail"}).status_code == 302
    assert client.post(f"{A}/doubts/{theirs}/reply", data={"body": "No"}).status_code == 403
    assert 'name="body"' in client.get(f"{A}/doubts/{mine}").data.decode()
    assert 'name="body"' not in client.get(f"{A}/doubts/{theirs}").data.decode()


# --- point 9: backlog beside the change log --------------------------------------------------------------------
def test_backlog_add_move_edit_and_delete(app, client):
    pid = make_project(app)
    login(client, "mem1")
    page = client.get(A).data.decode()
    assert f"{A}/backlog" in page and f"{A}/changes" in page  # both in the menu
    resp = client.post(f"{A}/backlog/new", data={"title": "Close the runbook gap", "item_type": "KT_GAP", "priority": "HIGH",
                                                  "project_id": pid, "owner_id": user_id_in(app, "sme"), "target_date": "2030-01-05"})
    assert resp.status_code == 302
    with app.app_context():
        item = BacklogItem.query.one()
        assert item.code == "B-001" and item.status == "TODO" and item.owner.username == "sme"
        iid = item.id
    board = client.get(f"{A}/backlog").data.decode()
    assert "Close the runbook gap" in board and "High" in board
    client.post(f"{A}/backlog/{iid}/status", data={"status": "DONE"})
    with app.app_context():
        item = db.session.get(BacklogItem, iid)
        assert item.status == "DONE" and item.done_at is not None
    assert client.post(f"{A}/backlog/{iid}/status", data={"status": "NOPE"}).status_code == 400
    client.post(f"{A}/backlog/{iid}/status", data={"status": "IN_PROGRESS"})
    with app.app_context():
        assert db.session.get(BacklogItem, iid).done_at is None
    client.post(f"{A}/backlog/{iid}/edit", data={"title": "Renamed", "item_type": "TASK", "priority": "LOW"})
    with app.app_context():
        assert db.session.get(BacklogItem, iid).title == "Renamed"
    client.get("/logout")

    login(client, "sme")  # may work on it, may not delete someone else's
    assert client.post(f"{A}/backlog/{iid}/delete").status_code == 403
    client.get("/logout")
    login(client, "viewer")
    assert "Renamed" in client.get(f"{A}/backlog").data.decode()
    assert client.post(f"{A}/backlog/new", data={"title": "x"}).status_code == 403
    assert client.post(f"{A}/backlog/{iid}/status", data={"status": "DONE"}).status_code == 403
    client.get("/logout")
    login(client, "mem2")
    assert client.get(f"{A}/backlog").status_code == 404
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{A}/backlog/{iid}/delete")
    with app.app_context():
        assert BacklogItem.query.count() == 0


@pytest.mark.parametrize("fields, message", [
    ({"title": ""}, "Say what needs doing"),
    ({"title": "T", "item_type": "NOPE"}, "Choose a type"),
    ({"title": "T", "priority": "NOPE"}, "Choose a priority"),
    ({"title": "T", "target_date": "soon"}, "valid date"),
    ({"title": "T", "owner_id": "99999"}, "team member"),
    ({"title": "T", "project_id": "99999"}, "Choose a project"),
])
def test_backlog_validation(app, client, fields, message):
    login(client, "mem1")
    resp = client.post(f"{A}/backlog/new", data=fields)
    assert resp.status_code == 400 and message in resp.data.decode()


def test_backlog_owner_and_project_must_be_of_this_account(app, client):
    other = make_project(app, code="BETA")
    login(client, "mem1")
    assert client.post(f"{A}/backlog/new", data={"title": "T", "project_id": other}).status_code == 400
    assert client.post(f"{A}/backlog/new", data={"title": "T", "owner_id": user_id_in(app, "mem2")}).status_code == 400


def test_backlog_is_in_search_and_recent_activity(app, client):
    login(client, "mem1")
    client.post(f"{A}/backlog/new", data={"title": "Unique backlog words"})
    assert "Unique backlog words" in client.get(f"{A}/search?q=unique+backlog").data.decode()
    assert "Unique backlog words" in client.get("/").data.decode()


def user_id_in(app, username):
    with app.app_context():
        return user_id(username)
