"""Projects: checklist, receiver confirmation, client sign-off (in KITE or recorded), locking, internal KT,
documents, permissions and the upgrade of modules from before projects."""
import pytest

from conftest import account_id, login, user_id
from models import (
    DEFAULT_CHECKLIST, KTModule, KTProject, ProjectChecklistItem, ProjectDocument, ProjectEvent, User,
    add_project_event, db, upgrade_schema,
)
from test_kt import confirm_all, make_module, make_topic, topic_status

BASE = "/accounts/ACME/projects"


def uid(app, username):
    with app.app_context():
        return user_id(username)


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def new_project(app, client, **overrides):
    data = {"name": "Order to cash", "description": "AR and billing", "start_date": "2026-11-01",
            "target_date": "2026-12-15", "sharepoint_url": "https://acme.sharepoint.com/sites/kt/O2C",
            "client_approver_name": "Clara Client", "client_approver_email": "viewer@example.com",
            "team": [str(uid(app, "mem1"))]}
    data.update(overrides)
    return client.post(f"{BASE}/new", data=data)


def project(app, pid):
    return db.session.get(KTProject, pid)


def only_project_id(app):
    with app.app_context():
        return KTProject.query.one().id


def tick_all(client, app, pid):
    with app.app_context():
        ids = [i.id for i in project(app, pid).checklist]
    for iid in ids:
        assert client.post(f"{BASE}/{pid}/checklist/{iid}/done", data={"note": "ok"}).status_code == 302


def ready_project(app, client):
    """A project whose one topic is signed off and whose checklist is complete. Returns (project, topic) ids."""
    login(client, "lead")
    new_project(app, client)
    pid = only_project_id(app)
    mid = make_module(app, project=pid)
    tid = make_topic(app, mid, status="REVERSE_KT", receivers=("mem1",))
    confirm_all(app, tid)
    client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "SIGNED_OFF"})
    assert topic_status(app, tid) == "SIGNED_OFF"
    tick_all(client, app, pid)
    client.get("/logout")
    return pid, tid


# --- create and permissions ----------------------------------------------------------------------
def test_create_project_with_default_checklist_and_team(app, client, sent):
    login(client, "lead")
    form = client.get(f"{BASE}/new")
    assert form.status_code == 200 and b"Internal KT team" in form.data
    assert new_project(app, client).status_code == 302
    with app.app_context():
        p = KTProject.query.one()
        assert [i.title for i in p.checklist] == list(DEFAULT_CHECKLIST)
        assert [u.username for u in p.team_users] == ["mem1"] and p.client_approver_email == "viewer@example.com"
    page = client.get(f"{BASE}/{p.id}").data
    assert b"Open the SharePoint KT folder" in page and b"Hand-over checklist" in page
    # Adding someone to the team later emails them.
    new_team = [str(uid(app, "mem1")), str(uid(app, "sme"))]
    client.post(f"{BASE}/{p.id}/edit", data={"name": "Order to cash", "team": new_team})
    assert sent[-1][0] == ["sme@example.com"] and "Internal KT" in sent[-1][1]


def test_project_validation(app, client):
    login(client, "lead")
    resp = new_project(app, client, name="", sharepoint_url="ftp://x", client_approver_email="nope",
                       target_date="2026-10-01", team=[str(uid(app, "viewer"))])
    assert resp.status_code == 400
    for text in (b"Enter a project name", b"must start with https://", b"valid email", b"before the start date",
                 b"not viewers"):
        assert text in resp.data, text


def test_only_managers_create_and_members_only_view(app, client):
    for username in ("sme", "mem1", "viewer"):
        login(client, username)
        assert new_project(app, client).status_code == 403
        client.get("/logout")
    login(client, "lead")
    new_project(app, client)
    pid = only_project_id(app)
    client.get("/logout")
    login(client, "mem2")  # BETA only
    assert client.get(f"{BASE}/{pid}").status_code == 404
    assert client.get(f"/accounts/BETA/projects/{pid}").status_code == 404  # wrong account


# --- receiver confirmation -----------------------------------------------------------------------
def test_receiver_confirmation_rules(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid, status="SCHEDULED", receivers=("mem1",))
    login(client, "mem1")
    assert client.post(f"/accounts/ACME/kt/topics/{tid}/confirm").status_code == 400  # not delivered yet
    client.get("/logout")
    login(client, "sme")
    client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "DELIVERED"})
    assert client.post(f"/accounts/ACME/kt/topics/{tid}/confirm").status_code == 403  # not a receiver
    client.get("/logout")
    login(client, "mem1")
    assert b"I confirm I have received this KT" in client.get(f"/accounts/ACME/kt/topics/{tid}").data
    client.post(f"/accounts/ACME/kt/topics/{tid}/confirm", data={"note": "Clear"})
    page = client.get(f"/accounts/ACME/kt/topics/{tid}").data
    assert b"confirmed" in page and b"Clear" in page and b"Update my confirmation" in page


def test_sign_off_needs_receivers(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid, status="REVERSE_KT", receivers=())
    login(client, "lead")
    resp = client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "SIGNED_OFF"},
                       follow_redirects=True)
    assert topic_status(app, tid) == "REVERSE_KT" and b"add the receivers first" in resp.data


# --- checklist -----------------------------------------------------------------------------------
def test_checklist_permissions(app, client):
    login(client, "lead")
    new_project(app, client)
    pid = only_project_id(app)
    client.post(f"{BASE}/{pid}/checklist", data={"title": "VPN tokens issued"})
    with app.app_context():
        item = ProjectChecklistItem.query.filter_by(title="VPN tokens issued").one()
        iid = item.id
    client.get("/logout")
    login(client, "viewer")
    assert client.post(f"{BASE}/{pid}/checklist/{iid}/done").status_code == 403
    client.get("/logout")
    login(client, "mem1")
    assert client.post(f"{BASE}/{pid}/checklist", data={"title": "x"}).status_code == 403
    assert client.post(f"{BASE}/{pid}/checklist/{iid}/done",
                       data={"evidence_link": "not a link"}).status_code == 302
    with app.app_context():
        assert db.session.get(ProjectChecklistItem, iid).done_at is None  # bad link rejected
    client.post(f"{BASE}/{pid}/checklist/{iid}/done",
                data={"note": "All 6 issued", "evidence_link": "https://acme.sharepoint.com/vpn"})
    with app.app_context():
        item = db.session.get(ProjectChecklistItem, iid)
        assert item.done_at and item.done_by.username == "mem1" and item.note == "All 6 issued"
    assert client.post(f"{BASE}/{pid}/checklist/{iid}/undo").status_code == 403  # only the KT lead reopens
    client.get("/logout")
    login(client, "lead")
    client.post(f"{BASE}/{pid}/checklist/{iid}/undo")
    client.post(f"{BASE}/{pid}/checklist/{iid}/delete")
    with app.app_context():
        assert db.session.get(ProjectChecklistItem, iid) is None
        kinds = [e.event_type for e in ProjectEvent.query.order_by(ProjectEvent.id)]
        assert kinds == ["CREATED", "CHECKLIST_ADDED", "CHECKLIST_DONE", "CHECKLIST_REOPENED", "CHECKLIST_REMOVED"]


# --- client sign-off -----------------------------------------------------------------------------
def test_sign_off_opens_only_when_ready(app, client, sent):
    login(client, "lead")
    new_project(app, client)
    pid = only_project_id(app)
    mid = make_module(app, project=pid)
    tid = make_topic(app, mid, status="REVERSE_KT")
    resp = client.post(f"{BASE}/{pid}/sign-off", data={"signer_name": "C", "evidence_link": "https://x.example"})
    assert resp.status_code == 400
    assert client.post(f"{BASE}/{pid}/sign-off/request").status_code == 400
    confirm_all(app, tid)
    client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "SIGNED_OFF"})
    assert not any("Ready for client sign-off" in m[1] for m in sent)  # checklist still open
    tick_all(client, app, pid)
    assert "Ready for client sign-off" in sent[-1][1] and sent[-1][0] == ["lead@example.com"]
    with app.app_context():
        from projects import project_state
        assert project_state(project(app, pid))["stage"] == "READY_FOR_SIGN_OFF"


def test_client_approver_signs_off_in_kite_then_internal_kt(app, client, sent):
    pid, tid = ready_project(app, client)
    login(client, "lead")
    client.post(f"{BASE}/{pid}/sign-off/request")
    assert sent[-1][0] == ["viewer@example.com"] and "Please sign off" in sent[-1][1]
    client.get("/logout")
    login(client, "mem1")  # not the approver
    assert client.post(f"{BASE}/{pid}/sign-off").status_code == 403
    client.get("/logout")
    login(client, "viewer")  # the approver's email matches this viewer
    assert b"I sign off the KT for this project" in client.get(f"{BASE}/{pid}").data
    client.post(f"{BASE}/{pid}/sign-off", data={"comment": "Thanks team"})
    with app.app_context():
        p = project(app, pid)
        assert p.client_signed and p.client_signed_in_kite and p.client_signer_name == "Viewer"
    assert "Client signed off" in sent[-1][1] and "mem1@example.com" in sent[-1][0]
    assert client.post(f"{BASE}/{pid}/sign-off").status_code == 400  # only once
    client.get("/logout")

    # The KT plan of a signed-off project is locked.
    login(client, "lead")
    resp = client.post("/accounts/ACME/kt/modules/new", data={"title": "Late", "project_id": pid})
    assert resp.status_code == 400 and b"locked" in resp.data
    assert client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "DELIVERED"}).status_code == 400
    with app.app_context():
        iid = project(app, pid).checklist[0].id
    client.post(f"{BASE}/{pid}/checklist/{iid}/undo")
    with app.app_context():
        assert project(app, pid).checklist[0].done_at is not None

    # Internal KT: confirm only after a session is held; a receiver runs the session.
    client.get("/logout")
    login(client, "mem1")  # receiver of the topic and on the internal team
    client.post(f"{BASE}/{pid}/internal/complete")
    with app.app_context():
        assert project(app, pid).team[0].completed_at is None
    client.post(f"{BASE}/{pid}/internal/sessions", data={
        "date": "2026-12-01", "time": "15:00", "duration": "60", "presenter_id": str(uid(app, "mem1")),
        "agenda": "Billing walkthrough"})
    assert "Internal KT session" in sent[-1][1]
    with app.app_context():
        sid = project(app, pid).internal_sessions[0].id
    client.post(f"{BASE}/{pid}/internal/sessions/{sid}/held", data={"recording_link": "https://stream.example/r"})
    client.post(f"{BASE}/{pid}/internal/complete", data={"comment": "Done"})
    with app.app_context():
        from projects import project_state
        assert project_state(project(app, pid))["stage"] == "COMPLETE"
    assert "KT complete" in sent[-1][1]
    client.get("/logout")
    login(client, "sme")  # not on the team
    assert client.post(f"{BASE}/{pid}/internal/complete").status_code == 403


def test_kt_lead_records_sign_off_with_evidence_and_can_revoke(app, client):
    pid, tid = ready_project(app, client)
    login(client, "lead")
    client.post(f"{BASE}/{pid}/sign-off", data={"signer_name": "Clara Client"})
    with app.app_context():
        assert not project(app, pid).client_signed  # evidence is required
    client.post(f"{BASE}/{pid}/sign-off", data={
        "signer_name": "Clara Client", "signer_email": "clara@acme.example",
        "evidence_link": "https://acme.sharepoint.com/kt/signoff.msg"})
    with app.app_context():
        p = project(app, pid)
        assert p.client_signed and not p.client_signed_in_kite and p.client_signoff_by.username == "lead"
    client.post(f"{BASE}/{pid}/sign-off/revoke", data={"reason": ""})
    with app.app_context():
        assert project(app, pid).client_signed
    client.post(f"{BASE}/{pid}/sign-off/revoke", data={"reason": "Client found a gap in month-end"})
    with app.app_context():
        p = project(app, pid)
        assert not p.client_signed and p.client_signer_name == ""
        assert "Clara Client" in p.events[-1].detail
    # Unlocked again.
    client.post(f"/accounts/ACME/kt/topics/{tid}/status", data={"status": "DELIVERED"})
    assert topic_status(app, tid) == "DELIVERED"


def test_viewer_who_is_not_the_approver_cannot_sign(app, client):
    pid, _ = ready_project(app, client)
    with app.app_context():
        project(app, pid).client_approver_email = "someone.else@acme.example"
        db.session.commit()
    login(client, "viewer")
    assert client.post(f"{BASE}/{pid}/sign-off").status_code == 403


# --- documents -----------------------------------------------------------------------------------
def test_documents_register(app, client):
    login(client, "lead")
    new_project(app, client)
    pid = only_project_id(app)
    other = make_module(app, project=None, title="Elsewhere")  # in another project
    other_topic = make_topic(app, other, title="Not here")
    mid = make_module(app, project=pid)
    tid = make_topic(app, mid)
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{BASE}/{pid}/documents", data={"title": "Billing deck", "url": "https://acme.sharepoint.com/d.pptx",
                                                 "doc_type": "KT deck", "topic_id": str(tid)})
    client.post(f"{BASE}/{pid}/documents", data={"title": "Bad", "url": "javascript:alert(1)", "doc_type": "Other"})
    client.post(f"{BASE}/{pid}/documents", data={"title": "Wrong topic", "url": "https://x.example",
                                                 "doc_type": "Other", "topic_id": str(other_topic)})
    with app.app_context():
        docs = ProjectDocument.query.all()
        assert [d.title for d in docs] == ["Billing deck"] and docs[0].in_sharepoint and docs[0].topic_id == tid
        did = docs[0].id
    page = client.get(f"{BASE}/{pid}").data
    assert b'href="https://acme.sharepoint.com/d.pptx"' in page and b"SharePoint" in page
    client.get("/logout")
    login(client, "sme")  # did not add it, not a KT lead
    assert client.post(f"{BASE}/{pid}/documents/{did}/delete").status_code == 403
    client.get("/logout")
    login(client, "viewer")
    assert client.post(f"{BASE}/{pid}/documents", data={"title": "x", "url": "https://x"}).status_code == 403
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{BASE}/{pid}/documents/{did}/delete")
    with app.app_context():
        assert ProjectDocument.query.count() == 0


# --- account page, history and upgrade -----------------------------------------------------------
def test_account_page_lists_projects(app, client):
    login(client, "lead")
    new_project(app, client)
    page = client.get("/accounts/ACME").data
    assert b"Order to cash" in page and b"Planning" in page and b"Scope of work" in page


def test_project_history_is_append_only(app):
    with app.app_context():
        p = KTProject(account_id=account_id("ACME"), name="P")
        db.session.add(p)
        add_project_event(p, db.session.get(User, uid(app, "lead")), "CREATED")
        db.session.commit()
        event = ProjectEvent.query.one()
        event.detail = "changed"
        with pytest.raises(RuntimeError):
            db.session.commit()
        db.session.rollback()


def test_upgrade_puts_old_modules_in_a_general_project(app):
    with app.app_context():
        db.session.add_all([KTModule(account_id=account_id("ACME"), title="Old 1"),
                            KTModule(account_id=account_id("ACME"), title="Old 2"),
                            KTModule(account_id=account_id("BETA"), title="Old 3")])
        db.session.commit()
        upgrade_schema()
        upgrade_schema()  # safe to repeat
        projects = KTProject.query.order_by(KTProject.id).all()
        assert [(p.account.code, p.name, len(p.modules)) for p in projects] == [("ACME", "General", 2),
                                                                                ("BETA", "General", 1)]
