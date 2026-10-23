"""Phase 5: transition phases per project, exit criteria, order rules, KT-phase link to client sign-off,
sign-off, reopen, permissions and next steps."""
import pytest

from conftest import account_id, login
from models import KTProject, TransitionPhase, db, ensure_phases
from timeutil import utcnow


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def make_project(app, signed=False):
    with app.app_context():
        p = KTProject(account_id=account_id("ACME"), name="Order to cash")
        ensure_phases(p)
        if signed:
            p.client_signed_at, p.client_signer_name = utcnow(), "Clara"
        db.session.add(p)
        db.session.commit()
        return p.id, [ph.id for ph in p.phases]


def base(pid):
    return f"/accounts/ACME/projects/{pid}/transition"


def status(app, phase_id):
    with app.app_context():
        return db.session.get(TransitionPhase, phase_id).status


def meet_all(client, app, pid, phase_id):
    with app.app_context():
        ids = [c.id for c in db.session.get(TransitionPhase, phase_id).criteria]
    for cid in ids:
        client.post(f"{base(pid)}/{phase_id}/criteria/{cid}/done", data={"note": "ok"})


def test_new_project_gets_four_phases_with_criteria(app, client):
    login(client, "lead")
    client.post("/accounts/ACME/projects/new", data={"name": "P"})
    with app.app_context():
        p = KTProject.query.one()
        assert [ph.phase for ph in p.phases] == ["KT", "SHADOW", "REVERSE_SHADOW", "STEADY_STATE"]
        assert all(ph.criteria for ph in p.phases) and all(ph.status == "NOT_STARTED" for ph in p.phases)
    page = client.get(base(p.id)).data
    assert b"Reverse shadow" in page and b"Start Knowledge transfer" in page


def test_full_transition_in_order(app, client, sent):
    pid, (kt, shadow, reverse, steady) = make_project(app)
    login(client, "lead")
    assert client.post(f"{base(pid)}/{shadow}/start").status_code == 400  # KT first
    client.post(f"{base(pid)}/{kt}/dates", data={"planned_start": "2026-11-01", "planned_end": "2026-11-30"})
    client.post(f"{base(pid)}/{kt}/start")
    assert status(app, kt) == "IN_PROGRESS" and "has started" in sent[-1][1]
    meet_all(client, app, pid, kt)
    # All criteria met, but the client has not signed off the project's KT yet.
    assert client.post(f"{base(pid)}/{kt}/sign-off").status_code == 400
    with app.app_context():
        p = db.session.get(KTProject, pid)
        p.client_signed_at, p.client_signer_name = utcnow(), "Clara"
        db.session.commit()
    assert "Sign off Knowledge transfer" in client.get("/").data.decode()  # next step for the KT lead
    client.post(f"{base(pid)}/{kt}/sign-off", data={"comment": "Good KT"})
    assert status(app, kt) == "SIGNED_OFF" and "signed off" in sent[-1][1]
    # Revoking the client's KT sign-off now needs the KT phase reopened first.
    client.post(f"/accounts/ACME/projects/{pid}/sign-off/revoke", data={"reason": "x"})
    with app.app_context():
        assert db.session.get(KTProject, pid).client_signed

    for phase in (shadow, reverse, steady):
        client.post(f"{base(pid)}/{phase}/start")
        assert client.post(f"{base(pid)}/{phase}/sign-off").status_code == 400  # criteria open
        meet_all(client, app, pid, phase)
        client.post(f"{base(pid)}/{phase}/sign-off")
        assert status(app, phase) == "SIGNED_OFF"
    assert "transition is complete" in sent[-1][2]
    assert b"Steady state reached" in client.get(base(pid)).data


def test_criteria_permissions_and_rules(app, client):
    pid, (kt, shadow, *_) = make_project(app)
    with app.app_context():
        cid = db.session.get(TransitionPhase, kt).criteria[0].id
    login(client, "mem1")
    client.post(f"{base(pid)}/{kt}/criteria/{cid}/done")  # phase not started: refused
    with app.app_context():
        assert db.session.get(TransitionPhase, kt).criteria[0].done_at is None
    assert client.post(f"{base(pid)}/{kt}/start").status_code == 403
    assert client.post(f"{base(pid)}/{kt}/criteria", data={"title": "x"}).status_code == 403
    client.get("/logout")
    login(client, "lead")
    client.post(f"{base(pid)}/{kt}/start")
    client.post(f"{base(pid)}/{kt}/criteria", data={"title": "Runbook walkthrough recorded"})
    client.get("/logout")
    login(client, "viewer")
    assert client.post(f"{base(pid)}/{kt}/criteria/{cid}/done").status_code == 403
    client.get("/logout")
    login(client, "mem1")
    client.post(f"{base(pid)}/{kt}/criteria/{cid}/done", data={"evidence_link": "https://acme.sharepoint.com/x"})
    with app.app_context():
        phase = db.session.get(TransitionPhase, kt)
        assert phase.criteria[0].done_by.username == "mem1" and len(phase.criteria) == 3
    assert client.post(f"{base(pid)}/{kt}/criteria/{cid}/undo").status_code == 403
    client.get("/logout")
    login(client, "mem2")
    assert client.get(base(pid)).status_code == 404


def test_reopen_only_latest_signed_phase(app, client):
    pid, (kt, shadow, *_) = make_project(app, signed=True)
    login(client, "lead")
    client.post(f"{base(pid)}/{kt}/start")
    meet_all(client, app, pid, kt)
    client.post(f"{base(pid)}/{kt}/sign-off")
    client.post(f"{base(pid)}/{kt}/reopen", data={"reason": ""})
    assert status(app, kt) == "SIGNED_OFF"  # a reason is required
    client.post(f"{base(pid)}/{shadow}/start")
    assert client.post(f"{base(pid)}/{kt}/reopen", data={"reason": "gap"}).status_code == 400  # shadow started
    with app.app_context():
        db.session.get(TransitionPhase, shadow).status = "NOT_STARTED"
        db.session.commit()
    client.post(f"{base(pid)}/{kt}/reopen", data={"reason": "Missed month-end"})
    with app.app_context():
        phase = db.session.get(TransitionPhase, kt)
        assert phase.status == "IN_PROGRESS" and phase.signed_off_at is None


def test_old_projects_get_phases_on_upgrade(app):
    from models import upgrade_schema
    with app.app_context():
        db.session.add(KTProject(account_id=account_id("ACME"), name="Old"))
        db.session.commit()
        upgrade_schema()
        assert len(KTProject.query.one().phases) == 4


def test_reports_show_transition_chart(app, client):
    make_project(app)
    login(client, "lead")
    page = client.get("/reports").data.decode()
    assert "Projects by transition phase" in page and "Steady state signed off" in page
