"""Projects: each client project's KT checklist, client sign-off, internal KT and KT documents.

The flow for a project:
1. KT from the client: topics in the project's modules are delivered, our receivers confirm they
   received each one, and the KT lead signs each topic off (see kt.py).
2. Checklist: the hand-over items are ticked off.
3. Client sign-off: once every topic is signed off and the checklist is complete, the client approver
   signs off in KITE (if they have a login with the approver email), or a KT lead records the client's
   sign-off with evidence (e.g. the sign-off email in SharePoint).
4. Internal KT: our receivers hold internal sessions for the rest of the team, and each team member
   confirms they completed it. Client sign-off plus everyone's internal KT = project complete.

Who may do what (checked on the server): members view; KT leads and admins manage the project,
checklist items, team and sign-off; any non-viewer member ticks checklist items and adds documents;
KT leads, and the project's SMEs and receivers, run internal sessions.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

import notify
from auth import login_required
from kt import TIME_RE, URL_RE, members_of, visible_account_or_404
from models import (
    DEFAULT_CHECKLIST, DOC_TYPES, InternalSession, KTProject, KTTopic, ProjectChecklistItem, ProjectDocument,
    ProjectTeamMember, User, add_project_event, db, ensure_phases,
)
from timeutil import format_ist, ist_local_to_utc, parse_date, utcnow

bp = Blueprint("projects", __name__, url_prefix="/accounts/<code>/projects")

STAGES = {
    "PLANNING": "Planning",
    "KT_IN_PROGRESS": "KT in progress",
    "CHECKLIST": "Checklist open",
    "READY_FOR_SIGN_OFF": "Ready for client sign-off",
    "INTERNAL_KT": "Internal KT",
    "COMPLETE": "Complete",
}


# --- state ---------------------------------------------------------------------------------------
def project_state(project):
    """Where a project is: KT topics, checklist, client sign-off and internal KT."""
    topics = [t for t in project.topics if t.status != "CANCELLED"]
    signed = sum(1 for t in topics if t.status == "SIGNED_OFF")
    checked = sum(1 for i in project.checklist if i.done_at)
    team_done = sum(1 for m in project.team if m.completed_at)
    kt_done = bool(topics) and signed == len(topics)
    checklist_done = checked == len(project.checklist)
    internal_done = team_done == len(project.team)
    if project.client_signed:
        stage = "COMPLETE" if internal_done else "INTERNAL_KT"
    elif not topics:
        stage = "PLANNING"
    elif not kt_done:
        stage = "KT_IN_PROGRESS"
    elif not checklist_done:
        stage = "CHECKLIST"
    else:
        stage = "READY_FOR_SIGN_OFF"
    return {
        "topics": len(topics), "signed_off": signed,
        "progress": round(100 * signed / len(topics)) if topics else 0,
        "checklist_total": len(project.checklist), "checklist_done": checked,
        "team_total": len(project.team), "team_done": team_done,
        "kt_done": kt_done, "ready": kt_done and checklist_done and not project.client_signed,
        "stage": stage, "stage_label": STAGES[stage],
    }


def account_projects(account):
    projects = (KTProject.query.filter_by(account_id=account.id)
                .order_by(KTProject.position, KTProject.id).all())
    return [(p, project_state(p)) for p in projects]


# --- access helpers ------------------------------------------------------------------------------
def _project_or_404(account, project_id):
    project = db.session.get(KTProject, project_id)
    if project is None or project.account_id != account.id:
        abort(404)
    return project


def _require(allowed):
    if not allowed:
        abort(403)


def can_run_internal(user, account, project):
    """KT leads and admins, and the SMEs and receivers of the project's topics."""
    if user.can_manage(account):
        return True
    if not user.can_contribute(account):
        return False
    return any(t.sme_id == user.id or t.is_receiver(user) for t in project.topics)


def _back(project, anchor=""):
    return redirect(url_for("projects.project", code=project.account.code, project_id=project.id) + anchor)


def _link_ok(url):
    return not url or (URL_RE.match(url) is not None and len(url) <= 500)


def _leads(account):
    return [u.email for u in members_of(account) if u.role == "kt_lead"]


def _project_body(project, intro):
    return (
        f"{intro}\n\n"
        f"Account: {project.account.code} - {project.account.name}\n"
        f"Project: {project.name}\n\n"
        f"Open the project: {notify.app_url(url_for('projects.project', code=project.account.code, project_id=project.id))}"
        + notify.footer()
    )


def tell_if_ready(project, was_ready):
    """Email the KT leads when the project has just become ready for client sign-off."""
    if not was_ready and project_state(project)["ready"]:
        notify.send(_leads(project.account), f"[{project.account.code}] Ready for client sign-off: {project.name}",
                    _project_body(project, "Every KT topic is signed off and the checklist is complete. "
                                           "The project is ready for the client's sign-off."))


# --- project create / edit -----------------------------------------------------------------------
def _read_project(form):
    return {
        "name": " ".join(form.get("name", "").split()),
        "description": form.get("description", "").strip(),
        "start_date": form.get("start_date", "").strip(),
        "target_date": form.get("target_date", "").strip(),
        "sharepoint_url": form.get("sharepoint_url", "").strip(),
        "client_approver_name": " ".join(form.get("client_approver_name", "").split()),
        "client_approver_email": form.get("client_approver_email", "").strip(),
        "team_ids": sorted({int(v) for v in form.getlist("team") if v.isdigit()}),
    }


def _validate_project(account, data):
    errors = {}
    if not data["name"] or len(data["name"]) > 200:
        errors["name"] = "Enter a project name (up to 200 characters)."
    if len(data["description"]) > 4000:
        errors["description"] = "At most 4,000 characters."
    start, target = parse_date(data["start_date"]), parse_date(data["target_date"])
    if data["start_date"] and start is None:
        errors["start_date"] = "Use a valid date."
    if data["target_date"] and target is None:
        errors["target_date"] = "Use a valid date."
    if start and target and target < start:
        errors["target_date"] = "The target date cannot be before the start date."
    if not _link_ok(data["sharepoint_url"]):
        errors["sharepoint_url"] = "The SharePoint folder link must start with https://."
    if len(data["client_approver_name"]) > 120:
        errors["client_approver_name"] = "At most 120 characters."
    email = data["client_approver_email"]
    if email and ("@" not in email or " " in email or len(email) > 200):
        errors["client_approver_email"] = "Enter a valid email address."
    allowed = {u.id for u in members_of(account) if u.role != "viewer"}
    if set(data["team_ids"]) - allowed:
        errors["team"] = "The internal KT team must be active members of this account (not viewers)."
    return errors


def _render_form(account, project, data, errors, status=200):
    return render_template(
        "projects/project_form.html", account=account, project=project, form=data, errors=errors,
        people=[u for u in members_of(account) if u.role != "viewer"],
    ), status


def _apply_project(project, data):
    project.name, project.description = data["name"], data["description"]
    project.start_date, project.target_date = parse_date(data["start_date"]), parse_date(data["target_date"])
    project.sharepoint_url = data["sharepoint_url"]
    project.client_approver_name = data["client_approver_name"]
    project.client_approver_email = data["client_approver_email"]
    current = {m.user_id: m for m in project.team}
    wanted = set(data["team_ids"])
    for user_id, member in current.items():
        if user_id not in wanted:
            project.team.remove(member)
    for user_id in sorted(wanted - set(current)):
        project.team.append(ProjectTeamMember(user_id=user_id))
    return wanted != set(current)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_project(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    if request.method == "GET":
        return _render_form(account, None, _read_project(MultiDict()), {})
    data = _read_project(request.form)
    errors = _validate_project(account, data)
    if errors:
        return _render_form(account, None, data, errors, 400)
    position = len(KTProject.query.filter_by(account_id=account.id).all()) + 1
    project = KTProject(account_id=account.id, position=position, created_at=utcnow())
    _apply_project(project, data)
    for i, title in enumerate(DEFAULT_CHECKLIST, start=1):
        project.checklist.append(ProjectChecklistItem(title=title, position=i))
    ensure_phases(project)
    db.session.add(project)
    add_project_event(project, g.user, "CREATED")
    db.session.commit()
    flash(f"Project '{project.name}' added with the standard hand-over checklist. Add its KT modules next.",
          "success")
    return _back(project)


@bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def edit_project(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    if request.method == "GET":
        data = {f: "" if getattr(project, f) is None else str(getattr(project, f))
                for f in ("name", "description", "start_date", "target_date", "sharepoint_url",
                          "client_approver_name", "client_approver_email")}
        data["team_ids"] = sorted(m.user_id for m in project.team)
        return _render_form(account, project, data, {})
    data = _read_project(request.form)
    errors = _validate_project(account, data)
    if errors:
        return _render_form(account, project, data, errors, 400)
    added = set(data["team_ids"]) - {m.user_id for m in project.team}
    team_changed = _apply_project(project, data)
    add_project_event(project, g.user, "UPDATED", "Internal KT team updated" if team_changed else None)
    db.session.commit()
    if added:
        emails = [db.session.get(User, uid).email for uid in added]
        notify.send(emails, f"[{account.code}] Internal KT for {project.name}",
                    _project_body(project, f"{g.user.full_name} added you to the internal KT team of this project. "
                                           "Join the internal sessions and confirm in KITE once you have completed it."))
    flash("Project saved.", "success")
    return _back(project)


@bp.route("/<int:project_id>")
@login_required
def project(code, project_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    if ensure_phases(project):
        db.session.commit()
    state = project_state(project)
    return render_template(
        "projects/project.html", account=account, project=project, state=state, stages=STAGES,
        can_manage=g.user.can_manage(account), can_contribute=g.user.can_contribute(account),
        can_run_internal=can_run_internal(g.user, account, project), is_approver=project.is_client_approver(g.user),
        me_on_team=project.team_member(g.user), doc_types=DOC_TYPES, now=utcnow(),
        presenters=sorted({u for t in project.topics for u in t.receiver_users + ([t.sme] if t.sme else [])}
                          | ({g.user} if g.user.can_contribute(account) else set()), key=lambda u: u.full_name),
    )


# --- checklist -----------------------------------------------------------------------------------
def _item_or_404(project, item_id):
    item = db.session.get(ProjectChecklistItem, item_id)
    if item is None or item.project_id != project.id:
        abort(404)
    return item


def _require_not_signed(project):
    if project.client_signed:
        flash("The client has signed off this project. Revoke the sign-off first to change it.", "error")
        return _back(project, "#checklist")
    return None


@bp.route("/<int:project_id>/checklist", methods=["POST"])
@login_required
def add_item(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    blocked = _require_not_signed(project)
    if blocked:
        return blocked
    title = " ".join(request.form.get("title", "").split())
    if not title or len(title) > 300:
        flash("Enter the checklist item (up to 300 characters).", "error")
        return _back(project, "#checklist")
    project.checklist.append(ProjectChecklistItem(title=title, position=len(project.checklist) + 1))
    add_project_event(project, g.user, "CHECKLIST_ADDED", title)
    db.session.commit()
    flash("Checklist item added.", "success")
    return _back(project, "#checklist")


@bp.route("/<int:project_id>/checklist/<int:item_id>/done", methods=["POST"])
@login_required
def item_done(code, project_id, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    project = _project_or_404(account, project_id)
    item = _item_or_404(project, item_id)
    blocked = _require_not_signed(project)
    if blocked:
        return blocked
    evidence = request.form.get("evidence_link", "").strip()
    if not _link_ok(evidence):
        flash("The evidence link must start with https://.", "error")
        return _back(project, "#checklist")
    was_ready = project_state(project)["ready"]
    item.done_at, item.done_by_id = utcnow(), g.user.id
    item.note, item.evidence_link = request.form.get("note", "").strip()[:2000], evidence
    add_project_event(project, g.user, "CHECKLIST_DONE", item.title)
    db.session.commit()
    tell_if_ready(project, was_ready)
    flash(f"Marked complete: {item.title}", "success")
    return _back(project, "#checklist")


@bp.route("/<int:project_id>/checklist/<int:item_id>/undo", methods=["POST"])
@login_required
def item_undo(code, project_id, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    item = _item_or_404(project, item_id)
    blocked = _require_not_signed(project)
    if blocked:
        return blocked
    item.done_at, item.done_by_id, item.note, item.evidence_link = None, None, "", ""
    add_project_event(project, g.user, "CHECKLIST_REOPENED", item.title)
    db.session.commit()
    flash(f"Reopened: {item.title}", "success")
    return _back(project, "#checklist")


@bp.route("/<int:project_id>/checklist/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_item(code, project_id, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    item = _item_or_404(project, item_id)
    blocked = _require_not_signed(project)
    if blocked:
        return blocked
    was_ready = project_state(project)["ready"]
    project.checklist.remove(item)
    add_project_event(project, g.user, "CHECKLIST_REMOVED", item.title)
    db.session.commit()
    tell_if_ready(project, was_ready)
    flash(f"Removed: {item.title}", "success")
    return _back(project, "#checklist")


# --- client sign-off -----------------------------------------------------------------------------
@bp.route("/<int:project_id>/sign-off/request", methods=["POST"])
@login_required
def request_signoff(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    if not project_state(project)["ready"]:
        abort(400, description="The project is not ready for client sign-off yet.")
    if not project.client_approver_email:
        flash("Add the client approver's email to the project first.", "error")
        return redirect(url_for("projects.edit_project", code=account.code, project_id=project.id))
    notify.send(
        [project.client_approver_email], f"[{account.code}] Please sign off the KT for {project.name}",
        _project_body(project, f"Dear {project.client_approver_name or 'colleague'},\n\n"
                               f"{g.user.full_name} asks you to sign off the knowledge transfer for this project: "
                               "every KT topic has been delivered, confirmed by our receivers and signed off, and "
                               "the hand-over checklist is complete.\n\n"
                               "If you have a KITE login, open the project and choose 'Sign off'. Otherwise please "
                               f"reply to {g.user.email} with your approval."))
    add_project_event(project, g.user, "SIGN_OFF_REQUESTED", project.client_approver_email)
    db.session.commit()
    flash(f"Sign-off request emailed to {project.client_approver_email}.", "success")
    return _back(project, "#sign-off")


@bp.route("/<int:project_id>/sign-off", methods=["POST"])
@login_required
def sign_off(code, project_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    by_client = project.is_client_approver(g.user)
    _require(by_client or g.user.can_manage(account))
    if project.client_signed:
        abort(400, description="The client has already signed off this project.")
    if not project_state(project)["ready"]:
        abort(400, description="Every KT topic must be signed off and the checklist complete first.")
    form = request.form
    comment = form.get("comment", "").strip()[:2000]
    if by_client:
        signer, email, evidence = g.user.full_name, g.user.email, ""
    else:
        signer = " ".join(form.get("signer_name", "").split())
        email, evidence = form.get("signer_email", "").strip(), form.get("evidence_link", "").strip()
        errors = []
        if not signer or len(signer) > 120:
            errors.append("Enter the name of the client person who signed off.")
        if email and ("@" not in email or len(email) > 200):
            errors.append("Enter a valid email for the client signer.")
        if not evidence or not _link_ok(evidence):
            errors.append("Add the https:// link to the evidence (e.g. the sign-off email saved in SharePoint).")
        if errors:
            for e in errors:
                flash(e, "error")
            return _back(project, "#sign-off")
    project.client_signed_at, project.client_signoff_by_id = utcnow(), g.user.id
    project.client_signer_name, project.client_signer_email = signer, email
    project.client_signoff_evidence, project.client_signoff_comment = evidence, comment
    project.client_signed_in_kite = by_client
    add_project_event(project, g.user, "CLIENT_SIGNED_OFF",
                      f"Signed off in KITE by {signer}" if by_client else f"Recorded for {signer}; evidence: {evidence}")
    db.session.commit()
    people = _leads(account) + [u.email for u in project.team_users] + \
        [u.email for t in project.topics for u in t.receiver_users]
    notify.send(people, f"[{account.code}] Client signed off the KT: {project.name}",
                _project_body(project, f"{signer} signed off the KT for this project on behalf of the client."
                                       + ("\n\nInternal KT: please complete it and confirm in KITE." if project.team else "")))
    flash("Client sign-off recorded." + (" Next: internal KT for the team." if project.team else ""), "success")
    return _back(project, "#sign-off")


@bp.route("/<int:project_id>/sign-off/revoke", methods=["POST"])
@login_required
def revoke_signoff(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    reason = request.form.get("reason", "").strip()[:2000]
    if not project.client_signed:
        abort(400, description="The project has not been signed off.")
    if any(p.phase == "KT" and p.status == "SIGNED_OFF" for p in project.phases):
        flash("The KT transition phase is signed off. Reopen it on the transition plan before revoking.", "error")
        return _back(project, "#sign-off")
    if not reason:
        flash("Give a reason for revoking the client sign-off.", "error")
        return _back(project, "#sign-off")
    add_project_event(project, g.user, "SIGN_OFF_REVOKED",
                      f"Was signed off by {project.client_signer_name} on {format_ist(project.client_signed_at)}. "
                      f"Reason: {reason}")
    project.client_signed_at, project.client_signoff_by_id, project.client_signed_in_kite = None, None, False
    project.client_signer_name = project.client_signer_email = ""
    project.client_signoff_evidence = project.client_signoff_comment = ""
    db.session.commit()
    flash("Client sign-off revoked. The project and its KT can be changed again.", "success")
    return _back(project, "#sign-off")


# --- internal KT ---------------------------------------------------------------------------------
@bp.route("/<int:project_id>/internal/sessions", methods=["POST"])
@login_required
def add_internal_session(code, project_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    _require(can_run_internal(g.user, account, project))
    form = request.form
    day, time = parse_date(form.get("date", "")), form.get("time", "").strip()
    starts_at = ist_local_to_utc(day, time) if day and TIME_RE.match(time) else None
    duration = form.get("duration", "60").strip()
    link, agenda = form.get("meeting_link", "").strip(), form.get("agenda", "").strip()[:2000]
    presenter_id = form.get("presenter_id", "")
    presenter = db.session.get(User, int(presenter_id)) if presenter_id.isdigit() else None
    errors = []
    if starts_at is None:
        errors.append("Enter the session date and time (IST).")
    if not duration.isdigit() or not 15 <= int(duration) <= 480:
        errors.append("Duration must be 15 to 480 minutes.")
    if not _link_ok(link):
        errors.append("The meeting link must start with https://.")
    if presenter is None or not presenter.active or not presenter.can_contribute(account):
        errors.append("Choose who presents the session.")
    if errors:
        for e in errors:
            flash(e, "error")
        return _back(project, "#internal")
    session = InternalSession(project=project, starts_at=starts_at, duration_minutes=int(duration),
                              presenter_id=presenter.id, agenda=agenda, meeting_link=link,
                              created_by_id=g.user.id, created_at=utcnow())
    db.session.add(session)
    add_project_event(project, g.user, "INTERNAL_SCHEDULED", f"{format_ist(starts_at)}, presented by {presenter.full_name}")
    db.session.commit()
    details = f"When: {format_ist(starts_at)} ({session.duration_minutes} minutes)\nPresenter: {presenter.full_name}\n" + \
        (f"Join: {link}\n" if link else "") + (f"Agenda: {agenda}\n" if agenda else "")
    notify.send([u.email for u in project.team_users] + [presenter.email],
                f"[{account.code}] Internal KT session: {project.name} on {format_ist(starts_at)}",
                _project_body(project, "An internal KT session has been scheduled.\n\n" + details))
    flash(f"Internal session on {format_ist(starts_at)} added; the team was emailed.", "success")
    return _back(project, "#internal")


@bp.route("/<int:project_id>/internal/sessions/<int:session_id>/held", methods=["POST"])
@login_required
def internal_session_held(code, project_id, session_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    _require(can_run_internal(g.user, account, project))
    session = db.session.get(InternalSession, session_id)
    if session is None or session.project_id != project.id:
        abort(404)
    recording = request.form.get("recording_link", "").strip()
    if not _link_ok(recording):
        flash("The recording link must start with https://.", "error")
        return _back(project, "#internal")
    session.held, session.recording_link = True, recording
    add_project_event(project, g.user, "INTERNAL_HELD", format_ist(session.starts_at))
    db.session.commit()
    flash("Internal session recorded as held. Team members can now confirm their internal KT.", "success")
    return _back(project, "#internal")


@bp.route("/<int:project_id>/internal/complete", methods=["POST"])
@login_required
def complete_internal(code, project_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    member = project.team_member(g.user)
    _require(member is not None)
    if not any(s.held for s in project.internal_sessions):
        flash("Internal KT can be confirmed once an internal session has been held.", "error")
        return _back(project, "#internal")
    first = member.completed_at is None
    member.completed_at, member.comment = utcnow(), request.form.get("comment", "").strip()[:2000]
    add_project_event(project, g.user, "INTERNAL_COMPLETED", member.comment or None)
    db.session.commit()
    if first and project_state(project)["stage"] == "COMPLETE":
        notify.send(_leads(account), f"[{account.code}] KT complete: {project.name}",
                    _project_body(project, "The client has signed off and the whole team has completed internal KT."))
    flash("Thanks, your internal KT is recorded as complete.", "success")
    return _back(project, "#internal")


# --- documents -----------------------------------------------------------------------------------
@bp.route("/<int:project_id>/documents", methods=["POST"])
@login_required
def add_document(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    project = _project_or_404(account, project_id)
    form = request.form
    title, url = " ".join(form.get("title", "").split()), form.get("url", "").strip()
    doc_type, topic_id = form.get("doc_type", "Other"), form.get("topic_id", "")
    topic = db.session.get(KTTopic, int(topic_id)) if topic_id.isdigit() else None
    errors = []
    if not title or len(title) > 200:
        errors.append("Enter the document title (up to 200 characters).")
    if not url or not _link_ok(url):
        errors.append("Paste the document's SharePoint link (https://...).")
    if doc_type not in DOC_TYPES:
        errors.append("Choose the document type.")
    if topic_id and (topic is None or topic.module.project_id != project.id):
        errors.append("The linked topic must belong to this project.")
    if errors:
        for e in errors:
            flash(e, "error")
        return _back(project, "#documents")
    db.session.add(ProjectDocument(project=project, title=title, url=url, doc_type=doc_type,
                                   topic_id=topic.id if topic else None, added_by_id=g.user.id, added_at=utcnow()))
    add_project_event(project, g.user, "DOCUMENT_ADDED", f"{doc_type}: {title}")
    db.session.commit()
    flash(f"Document '{title}' added.", "success")
    return _back(project, "#documents")


@bp.route("/<int:project_id>/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(code, project_id, doc_id):
    account = visible_account_or_404(code)
    project = _project_or_404(account, project_id)
    doc = db.session.get(ProjectDocument, doc_id)
    if doc is None or doc.project_id != project.id:
        abort(404)
    _require(g.user.can_manage(account) or (doc.added_by_id == g.user.id and g.user.can_contribute(account)))
    project.documents.remove(doc)
    add_project_event(project, g.user, "DOCUMENT_REMOVED", f"{doc.doc_type}: {doc.title}")
    db.session.commit()
    flash(f"Removed '{doc.title}' from the register (the file stays in SharePoint).", "success")
    return _back(project, "#documents")
