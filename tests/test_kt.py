"""KT tracker: plan, modules, topics, permissions, workflow, sessions, ratings, emails and exports."""
from datetime import date, timedelta

import pytest

from conftest import account_id, login, user_id
from models import (
    KTEvent, KTModule, KTRating, KTReceiver, KTSession, KTTopic, User, add_kt_event, db, kt_can_move,
)
from timeutil import ist_today

BASE = "/accounts/ACME/kt"


@pytest.fixture
def sent(monkeypatch):
    """Capture emails instead of sending them: list of (recipients, subject, body)."""
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def make_module(app, title="Billing", code="ACME"):
    with app.app_context():
        module = KTModule(account_id=account_id(code), title=title)
        db.session.add(module)
        db.session.commit()
        return module.id


def make_topic(app, module_id, title="Credit notes", status="PLANNED", sme="sme", receivers=("mem1",),
               due=None, code="ACME"):
    with app.app_context():
        topic = KTTopic(account_id=account_id(code), module_id=module_id, title=title, status=status,
                        sme_id=user_id(sme) if sme else None, due_date=due)
        for name in receivers:
            topic.receivers.append(KTReceiver(user_id=user_id(name)))
        db.session.add(topic)
        add_kt_event(topic, db.session.get(User, user_id("lead")), "CREATED", new_value=status)
        db.session.commit()
        return topic.id


def topic_status(app, topic_id):
    with app.app_context():
        return db.session.get(KTTopic, topic_id).status


def topic_form(app, module, **overrides):
    with app.app_context():
        data = {"title": "Month-end close", "description": "Steps and checks", "module_id": str(module),
                "sme_id": str(user_id("sme")), "planned_date": "2026-11-02", "due_date": "2026-11-20",
                "materials": "Runbook | https://example.sharepoint.com/runbook\nhttps://example.com/video",
                "receivers": [str(user_id("mem1"))]}
    data.update(overrides)
    return data


# --- workflow rules ------------------------------------------------------------------------------
def test_transition_table():
    assert kt_can_move("PLANNED", "SCHEDULED") and kt_can_move("REVERSE_KT", "SIGNED_OFF")
    assert kt_can_move("SIGNED_OFF", "DELIVERED")  # reopen
    assert not kt_can_move("PLANNED", "SIGNED_OFF") and not kt_can_move("CANCELLED", "PLANNED")


# --- viewing -------------------------------------------------------------------------------------
def test_plan_visible_to_members_only(app, client):
    mid = make_module(app)
    make_topic(app, mid)
    for username in ("lead", "sme", "mem1", "viewer", "admin"):
        login(client, username)
        page = client.get(BASE)
        assert page.status_code == 200 and b"Credit notes" in page.data, username
        client.get("/logout")
    login(client, "mem2")  # member of BETA only
    assert client.get(BASE).status_code == 404
    assert client.get(f"{BASE}/topics/1").status_code == 404


def test_topic_of_another_account_is_404(app, client):
    beta_module = make_module(app, code="BETA")
    tid = make_topic(app, beta_module, receivers=("mem2",), code="BETA")
    login(client, "lead")
    assert client.get(f"{BASE}/topics/{tid}").status_code == 404
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "SCHEDULED"}).status_code == 404


def test_empty_plan_guides_the_kt_lead(client):
    login(client, "lead")
    assert b"Add the first module" in client.get(BASE).data
    client.get("/logout")
    login(client, "mem1")
    page = client.get(BASE).data
    assert b"has not planned any KT yet" in page and b"Add module" not in page


# --- planning (KT lead and admin only) -----------------------------------------------------------
def test_only_managers_plan(app, client):
    mid = make_module(app)
    for username in ("sme", "mem1", "viewer"):
        login(client, username)
        assert client.post(f"{BASE}/modules/new", data={"title": "X"}).status_code == 403
        assert client.post(f"{BASE}/topics/new", data=topic_form(app, mid)).status_code == 403
        assert client.get(f"{BASE}/modules/{mid}/edit").status_code == 403
        client.get("/logout")
    login(client, "admin")
    assert client.post(f"{BASE}/modules/new", data={"title": "Admin module"}).status_code == 302


def test_kt_lead_of_another_account_cannot_plan_here(app, client):
    with app.app_context():
        from models import AccountMember
        db.session.add(AccountMember(account_id=account_id("BETA"), user_id=user_id("lead")))
        db.session.commit()
    login(client, "lead")
    assert client.post("/accounts/BETA/kt/modules/new", data={"title": "Mine"}).status_code == 302
    with app.app_context():
        from models import AccountMember
        AccountMember.query.filter_by(account_id=account_id("BETA"), user_id=user_id("lead")).delete()
        db.session.commit()
    assert client.post("/accounts/BETA/kt/modules/new", data={"title": "Mine"}).status_code == 404


def test_create_module_and_topic(app, client, sent):
    login(client, "lead")
    assert client.get(f"{BASE}/topics/new").status_code == 302  # no module yet: sent to add one
    client.post(f"{BASE}/modules/new", data={"title": "  Month   end ", "description": "Closing the books"})
    with app.app_context():
        module = KTModule.query.one()
        assert (module.title, module.account.code) == ("Month end", "ACME")
    resp = client.post(f"{BASE}/topics/new", data=topic_form(app, module.id))
    assert resp.status_code == 302
    with app.app_context():
        topic = KTTopic.query.one()
        assert (topic.status, topic.sme.username, topic.due_date) == ("PLANNED", "sme", date(2026, 11, 20))
        assert [u.username for u in topic.receiver_users] == ["mem1"]
        assert KTEvent.query.filter_by(topic_id=topic.id, event_type="CREATED").count() == 1
    assert sent[0][0] == ["sme@example.com"] and "assigned to you" in sent[0][1]
    page = client.get(f"{BASE}/topics/{topic.id}").data
    assert b'href="https://example.sharepoint.com/runbook"' in page and b"Runbook" in page
    assert b'rel="noopener noreferrer"' in page


def test_topic_validation(app, client):
    mid = make_module(app)
    other = make_module(app, code="BETA")
    login(client, "lead")
    cases = [
        ({"title": ""}, b"Enter a topic title"),
        ({"module_id": str(other)}, b"Choose a module"),
        ({"sme_id": str(_uid(app, "mem2"))}, b"active member of this account"),
        ({"receivers": [str(_uid(app, "mem2"))]}, b"Receivers must be active members"),
        ({"planned_date": "2026-13-01"}, b"valid date"),
        ({"planned_date": "2026-11-20", "due_date": "2026-11-01"}, b"cannot be before"),
        ({"materials": "javascript:alert(1)"}, b"must be a link"),
        ({"materials": "Notes | ftp://files.example.com"}, b"must be a link"),
    ]
    for overrides, message in cases:
        resp = client.post(f"{BASE}/topics/new", data=topic_form(app, mid, **overrides))
        assert resp.status_code == 400 and message in resp.data, overrides
    with app.app_context():
        assert KTTopic.query.count() == 0


def _uid(app, username):
    with app.app_context():
        return user_id(username)


def test_sme_edits_only_description_and_materials(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid)
    login(client, "sme")
    resp = client.post(f"{BASE}/topics/{tid}/edit", data=topic_form(
        app, mid, title="Hijacked", due_date="2030-01-01", receivers=[], description="Better steps",
        materials="https://example.com/new"))
    assert resp.status_code == 302
    with app.app_context():
        topic = db.session.get(KTTopic, tid)
        assert topic.title == "Credit notes" and topic.due_date is None
        assert [u.username for u in topic.receiver_users] == ["mem1"]
        assert (topic.description, topic.materials) == ("Better steps", "https://example.com/new")
    client.get("/logout")
    login(client, "mem1")
    assert client.get(f"{BASE}/topics/{tid}/edit").status_code == 403


def test_module_delete_only_when_empty(app, client):
    mid = make_module(app)
    empty = make_module(app, title="Empty")
    make_topic(app, mid)
    login(client, "lead")
    client.post(f"{BASE}/modules/{mid}/delete")
    client.post(f"{BASE}/modules/{empty}/delete")
    with app.app_context():
        assert [m.title for m in KTModule.query] == ["Billing"]


# --- workflow ------------------------------------------------------------------------------------
def test_full_lifecycle_with_sessions_and_sign_off(app, client, sent):
    mid = make_module(app)
    tid = make_topic(app, mid, receivers=("mem1", "viewer"))
    login(client, "sme")
    resp = client.post(f"{BASE}/topics/{tid}/sessions", data={
        "date": "2026-11-05", "time": "10:30", "duration": "90", "meeting_link": "https://teams.example.com/j/1",
        "notes": "Agenda: credit note flow"})
    assert resp.status_code == 302
    assert topic_status(app, tid) == "SCHEDULED"  # scheduling moves a planned topic on
    invite = sent[-1]
    assert sorted(invite[0]) == ["mem1@example.com", "sme@example.com", "viewer@example.com"]
    assert "05 Nov 2026 10:30 IST" in invite[1] and "https://teams.example.com/j/1" in invite[2]
    with app.app_context():
        session = KTSession.query.one()
        assert session.starts_at.isoformat() == "2026-11-05T05:00:00"  # stored in UTC
        sid, mem1 = session.id, user_id("mem1")

    client.post(f"{BASE}/topics/{tid}/sessions/{sid}/held", data={
        "attended": [str(mem1), "99999"], "recording_link": "https://stream.example.com/rec"})
    assert topic_status(app, tid) == "DELIVERED"
    with app.app_context():
        session = db.session.get(KTSession, sid)
        assert [a.user.username for a in session.attendance] == ["mem1"]  # non-receivers ignored

    client.post(f"{BASE}/topics/{tid}/status", data={"status": "REVERSE_KT"})
    assert topic_status(app, tid) == "REVERSE_KT"
    # The SME cannot sign off; the KT lead can.
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "SIGNED_OFF"}).status_code == 403
    client.get("/logout")
    login(client, "lead")
    client.post(f"{BASE}/topics/{tid}/status", data={"status": "SIGNED_OFF", "comment": "Reverse KT went well"})
    with app.app_context():
        topic = db.session.get(KTTopic, tid)
        assert topic.status == "SIGNED_OFF" and topic.signed_off_by.username == "lead" and topic.signed_off_at
        assert [e.event_type for e in topic.events] == [
            "CREATED", "SESSION_SCHEDULED", "STATUS_CHANGED", "SESSION_HELD", "STATUS_CHANGED", "STATUS_CHANGED",
            "STATUS_CHANGED"]
    assert "signed off" in sent[-1][1].lower()
    # Reopen clears the sign-off.
    client.post(f"{BASE}/topics/{tid}/status", data={"status": "DELIVERED"})
    with app.app_context():
        topic = db.session.get(KTTopic, tid)
        assert topic.status == "DELIVERED" and topic.signed_off_at is None


def test_invalid_moves_and_permissions(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid)
    login(client, "lead")
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "SIGNED_OFF"}).status_code == 400
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "NONSENSE"}).status_code == 400
    client.get("/logout")
    for username in ("mem1", "viewer"):
        login(client, username)
        assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "SCHEDULED"}).status_code == 403
        assert client.post(f"{BASE}/topics/{tid}/sessions", data={"date": "2026-11-05", "time": "10:00"}).status_code == 403
        assert b"Update status" not in client.get(f"{BASE}/topics/{tid}").data
        client.get("/logout")
    login(client, "sme")
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "CANCELLED", "comment": "x"}).status_code == 403
    assert topic_status(app, tid) == "PLANNED"


def test_cancel_needs_a_reason_and_is_final(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid)
    login(client, "lead")
    client.post(f"{BASE}/topics/{tid}/status", data={"status": "CANCELLED"})
    assert topic_status(app, tid) == "PLANNED"
    client.post(f"{BASE}/topics/{tid}/status", data={"status": "CANCELLED", "comment": "Out of scope"})
    assert topic_status(app, tid) == "CANCELLED"
    assert client.post(f"{BASE}/topics/{tid}/status", data={"status": "PLANNED"}).status_code == 400
    assert client.post(f"{BASE}/topics/{tid}/sessions", data={"date": "2026-11-05", "time": "10:00"}).status_code == 400


def test_session_validation(app, client):
    mid = make_module(app)
    tid = make_topic(app, mid)
    login(client, "sme")
    for data, message in [
        ({"date": "", "time": "10:00"}, b"date and time"),
        ({"date": "2026-11-05", "time": "25:99"}, b"date and time"),
        ({"date": "2026-11-05", "time": "10:00", "duration": "5"}, b"15 to 480"),
        ({"date": "2026-11-05", "time": "10:00", "meeting_link": "javascript:alert(1)"}, b"must start with https"),
    ]:
        resp = client.post(f"{BASE}/topics/{tid}/sessions", data=data, follow_redirects=True)
        assert message in resp.data, data
    with app.app_context():
        assert KTSession.query.count() == 0


# --- understanding ---------------------------------------------------------------------------------
def test_receivers_rate_and_low_rating_alerts(app, client, sent):
    mid = make_module(app)
    tid = make_topic(app, mid, receivers=("mem1",))
    login(client, "viewer")  # not a receiver of this topic
    assert client.post(f"{BASE}/topics/{tid}/rating", data={"score": "4"}).status_code == 403
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{BASE}/topics/{tid}/rating", data={"score": "9"})
    with app.app_context():
        assert KTRating.query.count() == 0
    client.post(f"{BASE}/topics/{tid}/rating", data={"score": "2", "comment": "Lost on the approval step"})
    assert sorted(sent[-1][0]) == ["lead@example.com", "sme@example.com"] and "Follow-up" in sent[-1][1]
    count = len(sent)
    client.post(f"{BASE}/topics/{tid}/rating", data={"score": "1"})  # still low: no second alert
    assert len(sent) == count
    client.post(f"{BASE}/topics/{tid}/rating", data={"score": "5"})
    with app.app_context():
        assert KTRating.query.one().score == 5
        assert db.session.get(KTTopic, tid).average_rating == 5


# --- dashboards ----------------------------------------------------------------------------------
def test_progress_overdue_and_follow_up_counts(app, client):
    mid = make_module(app)
    yesterday = ist_today() - timedelta(days=1)
    make_topic(app, mid, title="Done", status="SIGNED_OFF", due=yesterday)
    make_topic(app, mid, title="Late", status="DELIVERED", due=yesterday)
    make_topic(app, mid, title="Paused", status="ON_HOLD", due=yesterday)  # on hold is not overdue
    make_topic(app, mid, title="Dropped", status="CANCELLED")              # not counted
    low = make_topic(app, mid, title="Hard", status="REVERSE_KT")
    with app.app_context():
        db.session.add(KTRating(topic_id=low, user_id=user_id("mem1"), score=1))
        db.session.commit()
    login(client, "mem1")
    page = client.get("/accounts/ACME").data
    assert b"25%" in page  # 1 of 4 active topics signed off
    plan = client.get(BASE).data
    assert b"(overdue)" in plan and b"follow up" in plan
    overdue = client.get(f"{BASE}?status=OVERDUE").data
    assert b"Late" in overdue and b"Paused" not in overdue and b"Done" not in overdue
    home = client.get("/").data
    assert b"KT 1/4 signed off" in home and b"1 overdue" in home


def test_my_kt_on_home_page(app, client):
    mid = make_module(app)
    make_topic(app, mid, title="I receive this", receivers=("mem1",))
    make_topic(app, mid, title="Not mine", receivers=("viewer",))
    make_topic(app, mid, title="Finished", status="SIGNED_OFF", receivers=("mem1",))
    login(client, "mem1")
    page = client.get("/").data
    assert b"My KT" in page and b"I receive this" in page
    assert b"Not mine" not in page and b"Finished" not in page
    client.get("/logout")
    login(client, "sme")
    assert b"I receive this" in client.get("/").data and b"SME" in client.get("/").data
    plan = client.get(f"{BASE}?who=me").data
    assert b"I receive this" in plan


def test_export_csv(app, client):
    mid = make_module(app)
    make_topic(app, mid, title="Credit notes, part 1", due=date(2026, 11, 20))
    login(client, "viewer")
    resp = client.get(f"{BASE}/export.csv")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    text = resp.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Module,Topic,Status,SME")
    assert '"Credit notes, part 1"' in text and "20 Nov 2026" in text


def test_output_is_escaped(app, client):
    mid = make_module(app, title="<b>bold</b>")
    tid = make_topic(app, mid, title="<script>alert(1)</script>")
    login(client, "mem1")
    for path in (BASE, f"{BASE}/topics/{tid}", "/"):
        body = client.get(path).data
        assert b"<script>alert(1)</script>" not in body and b"<b>bold</b>" not in body, path


def test_kt_history_is_append_only(app):
    mid = make_module(app)
    tid = make_topic(app, mid)
    with app.app_context():
        event = KTEvent.query.filter_by(topic_id=tid).first()
        event.comment = "tampered"
        with pytest.raises(RuntimeError):
            db.session.flush()
