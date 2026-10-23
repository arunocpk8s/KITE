"""Transition phases per project: Knowledge transfer -> Shadow -> Reverse shadow -> Steady state.

Each phase has planned and actual dates, an exit-criteria checklist and a sign-off. A phase can start
only once the previous one is signed off; it is signed off by a KT lead when every exit criterion is
met. The KT phase also needs the client's sign-off of the project's KT (projects.py). Signing off
Steady state completes the transition. A KT lead can reopen the latest signed-off phase (with a reason)
while the next one has not started.

Who may do what (checked on the server): members view; KT leads and admins plan dates, start, sign off
and reopen phases and edit the criteria list; anyone in the team except viewers ticks criteria off.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import notify
from auth import login_required
from kt import URL_RE, members_of, visible_account_or_404
from models import (
    ExitCriterion, KTProject, TransitionPhase, add_project_event, db, ensure_phases,
)
from timeutil import format_date, ist_today, parse_date, utcnow

bp = Blueprint("transitions", __name__, url_prefix="/accounts/<code>/projects/<int:project_id>/transition")


def _project_or_404(account, project_id):
    project = db.session.get(KTProject, project_id)
    if project is None or project.account_id != account.id:
        abort(404)
    if ensure_phases(project):  # projects made before phases existed, or directly in the database
        db.session.commit()
    return project


def _phase_or_404(project, phase_id):
    phase = db.session.get(TransitionPhase, phase_id)
    if phase is None or phase.project_id != project.id:
        abort(404)
    return phase


def _criterion_or_404(phase, criterion_id):
    criterion = db.session.get(ExitCriterion, criterion_id)
    if criterion is None or criterion.phase_id != phase.id:
        abort(404)
    return criterion


def _require(allowed):
    if not allowed:
        abort(403)


def _back(project):
    return redirect(url_for("transitions.view", code=project.account.code, project_id=project.id))


def _previous(phase):
    phases = phase.project.phases
    i = phases.index(phase)
    return phases[i - 1] if i else None


def _next(phase):
    phases = phase.project.phases
    i = phases.index(phase)
    return phases[i + 1] if i + 1 < len(phases) else None


def current_phase(project):
    """The phase in progress, else the first not started, else None (transition complete)."""
    return next((p for p in project.phases if p.status == "IN_PROGRESS"), None) or \
        next((p for p in project.phases if p.status == "NOT_STARTED"), None)


def can_start(phase):
    previous = _previous(phase)
    return phase.status == "NOT_STARTED" and (previous is None or previous.status == "SIGNED_OFF")


def sign_off_blockers(phase):
    """What still stops the phase being signed off (empty list = it can be)."""
    blockers = []
    if phase.status != "IN_PROGRESS":
        blockers.append("the phase has not started")
    open_criteria = len(phase.criteria) - phase.criteria_done
    if open_criteria:
        blockers.append(f"{open_criteria} exit criteria still open")
    if phase.phase == "KT" and not phase.project.client_signed:
        blockers.append("the client has not signed off the project's KT")
    return blockers


def _body(project, intro):
    return (
        f"{intro}\n\n"
        f"Account: {project.account.code} - {project.account.name}\n"
        f"Project: {project.name}\n\n"
        f"Open the transition plan: "
        f"{notify.app_url(url_for('transitions.view', code=project.account.code, project_id=project.id))}"
        + notify.footer()
    )


def _team_emails(project):
    return [u.email for u in members_of(project.account) if u.role != "viewer"]


@bp.route("")
@login_required
def view(code, project_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    return render_template(
        "transitions/view.html", account=account, project=project, today=ist_today(),
        current=current_phase(project), manager=g.user.can_manage(account),
        can_tick=g.user.can_contribute(account), can_start=can_start, blockers=sign_off_blockers,
        reopenable=[p for p in project.phases if p.status == "SIGNED_OFF"
                    and (_next(p) is None or _next(p).status == "NOT_STARTED")],
    )


@bp.route("/<int:phase_id>/dates", methods=["POST"])
@login_required
def plan_dates(code, project_id, phase_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    form = request.form
    start, end = parse_date(form.get("planned_start", "")), parse_date(form.get("planned_end", ""))
    if (form.get("planned_start", "").strip() and start is None) or (form.get("planned_end", "").strip() and end is None):
        flash("Use valid dates.", "error")
    elif start and end and end < start:
        flash("The planned end cannot be before the planned start.", "error")
    else:
        phase.planned_start, phase.planned_end = start, end
        add_project_event(project, g.user, "PHASE_PLANNED",
                          f"{phase.label}: {format_date(start) or '?'} – {format_date(end) or '?'}")
        db.session.commit()
        flash(f"{phase.label} dates saved.", "success")
    return _back(project)


@bp.route("/<int:phase_id>/start", methods=["POST"])
@login_required
def start(code, project_id, phase_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    if not can_start(phase):
        abort(400, description="A phase starts only after the previous one is signed off.")
    phase.status, phase.actual_start = "IN_PROGRESS", ist_today()
    add_project_event(project, g.user, "PHASE_STARTED", phase.label)
    db.session.commit()
    notify.send(_team_emails(project), f"[{account.code}] {project.name}: {phase.label} has started",
                _body(project, f"{g.user.full_name} started the {phase.label} phase."
                      + (f" Planned to end by {format_date(phase.planned_end)}." if phase.planned_end else "")))
    flash(f"{phase.label} started.", "success")
    return _back(project)


@bp.route("/<int:phase_id>/sign-off", methods=["POST"])
@login_required
def sign_off(code, project_id, phase_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    blockers = sign_off_blockers(phase)
    if blockers:
        abort(400, description="The phase cannot be signed off yet: " + "; ".join(blockers) + ".")
    phase.status, phase.actual_end = "SIGNED_OFF", ist_today()
    phase.signed_off_at, phase.signed_off_by_id = utcnow(), g.user.id
    phase.signoff_comment = request.form.get("comment", "").strip()[:2000]
    add_project_event(project, g.user, "PHASE_SIGNED_OFF", phase.label)
    db.session.commit()
    following = _next(phase)
    notify.send(_team_emails(project), f"[{account.code}] {project.name}: {phase.label} signed off",
                _body(project, f"{g.user.full_name} signed off the {phase.label} phase."
                      + (f" Next: {following.label}." if following else " The transition is complete: steady state.")))
    flash(f"{phase.label} signed off." + (f" You can start {following.label} now." if following
                                          else " The transition is complete."), "success")
    return _back(project)


@bp.route("/<int:phase_id>/reopen", methods=["POST"])
@login_required
def reopen(code, project_id, phase_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    following = _next(phase)
    if phase.status != "SIGNED_OFF" or (following is not None and following.status != "NOT_STARTED"):
        abort(400, description="Only the latest signed-off phase can be reopened.")
    reason = request.form.get("reason", "").strip()[:2000]
    if not reason:
        flash("Give a reason for reopening the phase.", "error")
        return _back(project)
    phase.status, phase.actual_end, phase.signed_off_at, phase.signed_off_by_id = "IN_PROGRESS", None, None, None
    add_project_event(project, g.user, "PHASE_REOPENED", f"{phase.label}. Reason: {reason}")
    db.session.commit()
    flash(f"{phase.label} reopened.", "success")
    return _back(project)


# --- exit criteria -------------------------------------------------------------------------------
def _editable(phase):
    if phase.status == "SIGNED_OFF":
        flash("This phase is signed off. Reopen it to change its exit criteria.", "error")
        return False
    return True


@bp.route("/<int:phase_id>/criteria", methods=["POST"])
@login_required
def add_criterion(code, project_id, phase_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    title = " ".join(request.form.get("title", "").split())
    if not title or len(title) > 300:
        flash("Enter the exit criterion (up to 300 characters).", "error")
    elif _editable(phase):
        phase.criteria.append(ExitCriterion(title=title, position=len(phase.criteria) + 1))
        add_project_event(project, g.user, "CRITERION_ADDED", f"{phase.label}: {title}")
        db.session.commit()
    return _back(project)


@bp.route("/<int:phase_id>/criteria/<int:criterion_id>/done", methods=["POST"])
@login_required
def criterion_done(code, project_id, phase_id, criterion_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    criterion = _criterion_or_404(phase, criterion_id)
    evidence = request.form.get("evidence_link", "").strip()
    if evidence and (not URL_RE.match(evidence) or len(evidence) > 500):
        flash("The evidence link must start with https://.", "error")
    elif phase.status != "IN_PROGRESS":
        flash("Exit criteria are ticked off while the phase is in progress.", "error")
    else:
        criterion.done_at, criterion.done_by_id = utcnow(), g.user.id
        criterion.note, criterion.evidence_link = request.form.get("note", "").strip()[:2000], evidence
        add_project_event(project, g.user, "CRITERION_MET", f"{phase.label}: {criterion.title}")
        db.session.commit()
        if not sign_off_blockers(phase):
            notify.send([u.email for u in members_of(account) if u.role == "kt_lead"],
                        f"[{account.code}] {project.name}: {phase.label} is ready for sign-off",
                        _body(project, f"Every exit criterion of the {phase.label} phase is met."))
    return _back(project)


@bp.route("/<int:phase_id>/criteria/<int:criterion_id>/undo", methods=["POST"])
@login_required
def criterion_undo(code, project_id, phase_id, criterion_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    criterion = _criterion_or_404(phase, criterion_id)
    if _editable(phase):
        criterion.done_at, criterion.done_by_id, criterion.note, criterion.evidence_link = None, None, "", ""
        add_project_event(project, g.user, "CRITERION_REOPENED", f"{phase.label}: {criterion.title}")
        db.session.commit()
    return _back(project)


@bp.route("/<int:phase_id>/criteria/<int:criterion_id>/delete", methods=["POST"])
@login_required
def delete_criterion(code, project_id, phase_id, criterion_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    phase = _phase_or_404(project, phase_id)
    criterion = _criterion_or_404(phase, criterion_id)
    if _editable(phase):
        phase.criteria.remove(criterion)
        add_project_event(project, g.user, "CRITERION_REMOVED", f"{phase.label}: {criterion.title}")
        db.session.commit()
    return _back(project)
