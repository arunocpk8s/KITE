"""KT tracker: an account's KT plan (projects -> modules -> topics), sessions, ratings, receiver
confirmation, status workflow and history.

Who may do what (all checked on the server):
- Members of the account (and admins) can view the KT plan.
- KT leads of the account and admins plan it: modules, topics, SME and receivers; they also sign off,
  cancel and reopen topics. A topic is signed off only after every receiver has confirmed receiving it.
- The topic's SME runs it: schedules and records sessions, edits materials, and moves the status up to
  Reverse KT.
- Receivers rate their own understanding and confirm they received the KT. Viewers only read.
- Once the client has signed off a project (projects.py), its modules and topics are locked.
"""
import csv
import io
import re

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

import notify
from auth import login_required
from models import (
    KT_CLOSED, KT_LEAD_ONLY, KT_STATUS_LABELS, KT_STATUSES, KT_TRANSITIONS, LOW_RATING, Account, KTAttendance,
    KTModule, KTProject, KTRating, KTReceiver, KTSession, KTTopic, User, add_kt_event, db, kt_can_move,
)
from timeutil import format_date, format_ist, ist_local_to_utc, ist_today, parse_date, utcnow

bp = Blueprint("kt", __name__, url_prefix="/accounts/<code>/kt")

URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
MAX_MATERIAL_LINES = 30


# --- access helpers ------------------------------------------------------------------------------
def visible_account_or_404(code):
    account = Account.query.filter_by(code=code.upper()).first()
    if account is None or not g.user.can_view(account) or (not account.active and not g.user.is_admin):
        abort(404)
    return account


def _topic_or_404(account, topic_id):
    topic = db.session.get(KTTopic, topic_id)
    if topic is None or topic.account_id != account.id:
        abort(404)
    return topic


def _module_or_404(account, module_id):
    module = db.session.get(KTModule, module_id)
    if module is None or module.account_id != account.id:
        abort(404)
    return module


def _require_manager(account):
    if not g.user.can_manage(account):
        abort(403)


def can_run(user, account, topic):
    """KT leads and admins, or the topic's SME."""
    return user.can_manage(account) or (topic.sme_id == user.id and user.role != "viewer")


def allowed_moves(user, account, topic):
    if not can_run(user, account, topic):
        return []
    manager = user.can_manage(account)
    return [s for s in KT_TRANSITIONS.get(topic.status, ())
            if manager or (topic.status, s) not in KT_LEAD_ONLY]


def parse_links(text):
    """'Label | https://...' or 'https://...' per line -> [(label, url)]; other lines are ignored."""
    links = []
    for line in (text or "").splitlines():
        label, _, url = line.rpartition("|") if "|" in line else ("", "", line)
        url, label = url.strip(), label.strip()
        if URL_RE.match(url):
            links.append((label or url, url))
    return links


def _bad_link_lines(text):
    bad = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        url = line.rpartition("|")[2].strip() if "|" in line else line.strip()
        if not URL_RE.match(url):
            bad.append(line.strip()[:60])
    return bad


def members_of(account):
    return account.member_users().filter(User.active.is_(True)).all()


def _leads(account):
    return [u for u in members_of(account) if u.role == "kt_lead"]


# --- plan ----------------------------------------------------------------------------------------
def plan_stats(account, today=None):
    """Counts for an account's KT plan; cancelled topics are left out of progress."""
    today = today or ist_today()
    topics = KTTopic.query.filter_by(account_id=account.id).all()
    active = [t for t in topics if t.status != "CANCELLED"]
    signed = [t for t in active if t.status == "SIGNED_OFF"]
    return {
        "total": len(active),
        "signed_off": len(signed),
        "progress": round(100 * len(signed) / len(active)) if active else 0,
        "overdue": sum(1 for t in active if t.is_overdue(today)),
        "low_understanding": sum(1 for t in active if t.low_ratings),
        "by_status": {s: sum(1 for t in topics if t.status == s) for s in KT_STATUSES},
    }


def _module_progress(module):
    active = [t for t in module.topics if t.status != "CANCELLED"]
    done = sum(1 for t in active if t.status == "SIGNED_OFF")
    return round(100 * done / len(active)) if active else 0


@bp.route("")
@login_required
def plan(code):
    account = visible_account_or_404(code)
    status = request.args.get("status", "").upper()
    who = request.args.get("who", "")
    project_id = request.args.get("project", "")
    projects = _projects(account)
    shown = [p for p in projects if not project_id or str(p.id) == project_id]
    today = ist_today()

    def keep(topic):
        if status == "OPEN" and not topic.is_open:
            return False
        if status == "OVERDUE" and not topic.is_overdue(today):
            return False
        if status in KT_STATUSES and topic.status != status:
            return False
        if who == "me" and topic.sme_id != g.user.id and not topic.is_receiver(g.user):
            return False
        return True

    sections = [(p, [(m, [t for t in m.topics if keep(t)], _module_progress(m)) for m in p.modules])
                for p in shown]
    return render_template(
        "kt/plan.html", account=account, sections=sections, stats=plan_stats(account, today), today=today,
        filters={"status": status, "who": who, "project": project_id}, statuses=KT_STATUSES,
        labels=KT_STATUS_LABELS, can_manage=g.user.can_manage(account), projects=projects,
    )


@bp.route("/export.csv")
@login_required
def export(code):
    account = visible_account_or_404(code)
    today = ist_today()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Project", "Module", "Topic", "Status", "SME", "Receivers", "Receivers confirmed",
                     "Planned date", "Target sign-off", "Overdue", "Sessions held", "Average understanding",
                     "Signed off"])
    for p in _projects(account):
        for m in p.modules:
            for t in m.topics:
                writer.writerow([
                    p.name, m.title, t.title, t.status_label, t.sme.full_name if t.sme else "",
                    "; ".join(u.full_name for u in t.receiver_users),
                    f"{len(t.receivers) - len(t.unconfirmed_receivers)}/{len(t.receivers)}",
                    format_date(t.planned_date), format_date(t.due_date), "yes" if t.is_overdue(today) else "",
                    sum(1 for s in t.sessions if s.held), t.average_rating or "", format_ist(t.signed_off_at),
                ])
    # Excel opens UTF-8 CSV correctly with a byte-order mark.
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="kite-kt-{account.code}-{today}.csv"'})


# --- modules -------------------------------------------------------------------------------------
SIGNED_PROJECT = "The client has signed off this project, so its KT plan is locked. Revoke the sign-off to change it."


def _projects(account):
    return KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all()


def _read_module(form):
    return {"title": " ".join(form.get("title", "").split()), "description": form.get("description", "").strip(),
            "project_id": form.get("project_id", "")}


def _validate_module(account, data, module=None):
    errors = {}
    if not data["title"] or len(data["title"]) > 200:
        errors["title"] = "Enter a module title (up to 200 characters)."
    if len(data["description"]) > 4000:
        errors["description"] = "At most 4,000 characters."
    project = db.session.get(KTProject, int(data["project_id"])) if data["project_id"].isdigit() else None
    if project is None or project.account_id != account.id:
        errors["project_id"] = "Choose a project."
    elif project.client_signed or (module is not None and module.project.client_signed
                                   and module.project_id != project.id):
        errors["project_id"] = SIGNED_PROJECT
    return errors


def _render_module_form(account, module, data, errors):
    return render_template("kt/module_form.html", account=account, module=module, form=data, errors=errors,
                           projects=_projects(account)), (400 if errors else 200)


@bp.route("/modules/new", methods=["GET", "POST"])
@login_required
def new_module(code):
    account = visible_account_or_404(code)
    _require_manager(account)
    if request.method == "GET" and not _projects(account):
        flash("Add a project first; KT modules belong to a project.", "info")
        return redirect(url_for("projects.new_project", code=account.code))
    data = _read_module(request.form) if request.method == "POST" else \
        {"title": "", "description": "", "project_id": request.args.get("project", "")}
    errors = _validate_module(account, data) if request.method == "POST" else {}
    if request.method == "GET" or errors:
        return _render_module_form(account, None, data, errors)
    position = (db.session.query(func.max(KTModule.position)).filter_by(account_id=account.id).scalar() or 0) + 1
    module = KTModule(account_id=account.id, position=position, title=data["title"],
                      description=data["description"], project_id=int(data["project_id"]))
    db.session.add(module)
    db.session.commit()
    flash(f"Module '{module.title}' added. Add its topics next.", "success")
    return redirect(url_for("kt.plan", code=account.code) + f"#module-{module.id}")


@bp.route("/modules/<int:module_id>/edit", methods=["GET", "POST"])
@login_required
def edit_module(code, module_id):
    account = visible_account_or_404(code)
    _require_manager(account)
    module = _module_or_404(account, module_id)
    data = _read_module(request.form) if request.method == "POST" else \
        {"title": module.title, "description": module.description, "project_id": str(module.project_id or "")}
    errors = _validate_module(account, data, module) if request.method == "POST" else {}
    if request.method == "GET" or errors:
        return _render_module_form(account, module, data, errors)
    module.title, module.description = data["title"], data["description"]
    module.project_id = int(data["project_id"])
    db.session.commit()
    flash("Module saved.", "success")
    return redirect(url_for("kt.plan", code=account.code) + f"#module-{module.id}")


@bp.route("/modules/<int:module_id>/delete", methods=["POST"])
@login_required
def delete_module(code, module_id):
    account = visible_account_or_404(code)
    _require_manager(account)
    module = _module_or_404(account, module_id)
    if module.topics:
        flash("Only an empty module can be deleted. Move or cancel its topics first.", "error")
    else:
        db.session.delete(module)
        db.session.commit()
        flash(f"Module '{module.title}' deleted.", "success")
    return redirect(url_for("kt.plan", code=account.code))


# --- topics --------------------------------------------------------------------------------------
TOPIC_FIELDS = ("title", "description", "module_id", "sme_id", "planned_date", "due_date", "materials")


def _read_topic(form):
    return {
        "title": " ".join(form.get("title", "").split()),
        "description": form.get("description", "").strip(),
        "module_id": form.get("module_id", ""),
        "sme_id": form.get("sme_id", ""),
        "planned_date": form.get("planned_date", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "materials": "\n".join(line.strip() for line in form.get("materials", "").splitlines() if line.strip()),
        "receiver_ids": sorted({int(v) for v in form.getlist("receivers") if v.isdigit()}),
    }


def _validate_topic(account, data, topic=None):
    errors = {}
    if not data["title"] or len(data["title"]) > 200:
        errors["title"] = "Enter a topic title (up to 200 characters)."
    if len(data["description"]) > 8000:
        errors["description"] = "At most 8,000 characters."
    module = db.session.get(KTModule, int(data["module_id"])) if data["module_id"].isdigit() else None
    if module is None or module.account_id != account.id:
        errors["module_id"] = "Choose a module."
    elif (topic is None or topic.module_id != module.id) and (
            module.project.client_signed or (topic is not None and topic.module.project.client_signed)):
        errors["module_id"] = SIGNED_PROJECT
    member_ids = {u.id for u in members_of(account)}
    if data["sme_id"] and (not data["sme_id"].isdigit() or int(data["sme_id"]) not in member_ids):
        errors["sme_id"] = "The SME must be an active member of this account."
    if set(data["receiver_ids"]) - member_ids:
        errors["receivers"] = "Receivers must be active members of this account."
    planned, due = parse_date(data["planned_date"]), parse_date(data["due_date"])
    if data["planned_date"] and planned is None:
        errors["planned_date"] = "Use a valid date."
    if data["due_date"] and due is None:
        errors["due_date"] = "Use a valid date."
    if planned and due and due < planned:
        errors["due_date"] = "The sign-off target cannot be before the planned date."
    bad = _bad_link_lines(data["materials"])
    if bad:
        errors["materials"] = "Each line must be a link starting with https:// (optionally 'Label | link'). Not valid: " + "; ".join(bad[:3])
    elif len(data["materials"].splitlines()) > MAX_MATERIAL_LINES:
        errors["materials"] = f"At most {MAX_MATERIAL_LINES} links."
    return errors


def _render_topic_form(account, topic, data, errors, status=200):
    members = members_of(account)
    return render_template(
        "kt/topic_form.html", account=account, topic=topic, form=data, errors=errors,
        modules=KTModule.query.filter_by(account_id=account.id).order_by(KTModule.position, KTModule.id).all(),
        smes=[u for u in members if u.role in ("sme", "kt_lead", "admin")],
        receivers=[u for u in members if u.role != "viewer"],
    ), status


def _apply_topic(topic, data):
    """Copy form data onto the topic. Returns a list of 'field: old -> new' changes."""
    values = {
        "title": data["title"], "description": data["description"], "module_id": int(data["module_id"]),
        "sme_id": int(data["sme_id"]) if data["sme_id"] else None,
        "planned_date": parse_date(data["planned_date"]), "due_date": parse_date(data["due_date"]),
        "materials": data["materials"],
    }
    changes = []
    for field, new in values.items():
        old = getattr(topic, field)
        if old != new:
            if field not in ("description", "materials"):
                changes.append(f"{field}: {old if old not in (None, '') else '(none)'} -> {new if new not in (None, '') else '(none)'}")
            else:
                changes.append(f"{field} updated")
            setattr(topic, field, new)
    current = {r.user_id: r for r in topic.receivers}
    wanted = set(data["receiver_ids"])
    for user_id, receiver in current.items():
        if user_id not in wanted:
            topic.receivers.remove(receiver)
    added = wanted - set(current)
    for user_id in added:
        topic.receivers.append(KTReceiver(user_id=user_id))
    if added or set(current) - wanted:
        changes.append("receivers updated")
    return changes


@bp.route("/topics/new", methods=["GET", "POST"])
@login_required
def new_topic(code):
    account = visible_account_or_404(code)
    _require_manager(account)
    if request.method == "GET":
        if not KTModule.query.filter_by(account_id=account.id).first():
            flash("Add a module first; topics belong to a module.", "info")
            return redirect(url_for("kt.new_module", code=account.code))
        data = {f: "" for f in TOPIC_FIELDS}
        data.update(module_id=request.args.get("module", ""), receiver_ids=[])
        return _render_topic_form(account, None, data, {})
    data = _read_topic(request.form)
    errors = _validate_topic(account, data)
    if errors:
        return _render_topic_form(account, None, data, errors, 400)
    module = db.session.get(KTModule, int(data["module_id"]))
    topic = KTTopic(account_id=account.id, status="PLANNED", position=len(module.topics) + 1,
                    created_at=utcnow(), updated_at=utcnow())
    _apply_topic(topic, data)
    db.session.add(topic)
    add_kt_event(topic, g.user, "CREATED", new_value="PLANNED")
    db.session.commit()
    if topic.sme and topic.sme_id != g.user.id:
        notify.send([topic.sme.email], f"[{account.code}] KT topic assigned to you: {topic.title}",
                    _topic_body(topic, f"{g.user.full_name} made you the SME for this KT topic."))
    flash(f"Topic '{topic.title}' added.", "success")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id))


@bp.route("/topics/<int:topic_id>/edit", methods=["GET", "POST"])
@login_required
def edit_topic(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    manager = g.user.can_manage(account)
    if not can_run(g.user, account, topic):
        abort(403)
    if request.method == "GET":
        data = {f: "" if getattr(topic, f) is None else str(getattr(topic, f)) for f in TOPIC_FIELDS}
        data["receiver_ids"] = sorted(r.user_id for r in topic.receivers)
        return _render_topic_form(account, topic, data, {})
    data = _read_topic(request.form)
    if not manager:
        # The SME may only change the description and materials; the plan stays with the KT lead.
        data.update(title=topic.title, module_id=str(topic.module_id), sme_id=str(topic.sme_id or ""),
                    planned_date=str(topic.planned_date or ""), due_date=str(topic.due_date or ""),
                    receiver_ids=sorted(r.user_id for r in topic.receivers))
    errors = _validate_topic(account, data, topic)
    if errors:
        return _render_topic_form(account, topic, data, errors, 400)
    old_sme = topic.sme_id
    changes = _apply_topic(topic, data)
    if changes:
        topic.updated_at = utcnow()
        add_kt_event(topic, g.user, "UPDATED", comment="; ".join(changes))
        db.session.commit()
        if topic.sme and topic.sme_id != old_sme and topic.sme_id != g.user.id:
            notify.send([topic.sme.email], f"[{account.code}] KT topic assigned to you: {topic.title}",
                        _topic_body(topic, f"{g.user.full_name} made you the SME for this KT topic."))
        flash("Topic saved.", "success")
    else:
        flash("No changes.", "info")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id))


@bp.route("/topics/<int:topic_id>")
@login_required
def topic(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    my_rating = next((r for r in topic.ratings if r.user_id == g.user.id), None)
    return render_template(
        "kt/topic.html", account=account, topic=topic, today=ist_today(), labels=KT_STATUS_LABELS,
        can_manage=g.user.can_manage(account), can_run=can_run(g.user, account, topic),
        moves=allowed_moves(g.user, account, topic), links=parse_links(topic.materials),
        is_receiver=topic.is_receiver(g.user), my_receipt=topic.receiver(g.user), my_rating=my_rating,
        low_rating=LOW_RATING, now=utcnow(), can_confirm=topic.status in CONFIRMABLE,
    )


def _topic_body(topic, intro):
    sme = topic.sme.full_name if topic.sme else "not assigned"
    return (
        f"{intro}\n\n"
        f"Account: {topic.account.code} - {topic.account.name}\n"
        f"Project: {topic.module.project.name}\n"
        f"Module:  {topic.module.title}\n"
        f"Topic:   {topic.title}\n"
        f"SME:     {sme}\n"
        f"Status:  {topic.status_label}\n"
        f"Target sign-off: {format_date(topic.due_date) or 'not set'}\n\n"
        f"Open the topic: {notify.app_url(url_for('kt.topic', code=topic.account.code, topic_id=topic.id))}"
        + notify.footer()
    )


def _move(topic, new_status, comment=""):
    old = topic.status
    topic.status = new_status
    topic.updated_at = utcnow()
    if new_status == "SIGNED_OFF":
        topic.signed_off_at, topic.signed_off_by_id = utcnow(), g.user.id
    elif old == "SIGNED_OFF":
        topic.signed_off_at, topic.signed_off_by_id = None, None
    add_kt_event(topic, g.user, "STATUS_CHANGED", old, new_status, comment or None)


@bp.route("/topics/<int:topic_id>/status", methods=["POST"])
@login_required
def change_status(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    new = request.form.get("status", "").upper()
    comment = request.form.get("comment", "").strip()[:2000]
    if not can_run(g.user, account, topic):
        abort(403)
    if not kt_can_move(topic.status, new):
        abort(400, description=f"A {topic.status_label} topic cannot move to {KT_STATUS_LABELS.get(new, new)}.")
    if new not in allowed_moves(g.user, account, topic):
        abort(403)
    if topic.module.project.client_signed:
        abort(400, description=SIGNED_PROJECT)
    back = redirect(url_for("kt.topic", code=account.code, topic_id=topic.id))
    if new == "CANCELLED" and not comment:
        flash("Give a reason for cancelling the topic.", "error")
        return back
    if new == "SIGNED_OFF" and (not topic.receivers or topic.unconfirmed_receivers):
        flash("Every receiver must confirm they received this KT before it is signed off. Waiting for: "
              + (", ".join(u.full_name for u in topic.unconfirmed_receivers) or "add the receivers first") + ".",
              "error")
        return back
    from projects import project_state, tell_if_ready  # projects imports this module
    was_ready = project_state(topic.module.project)["ready"]
    _move(topic, new, comment)
    db.session.commit()
    if new == "SIGNED_OFF":
        people = [u.email for u in topic.receiver_users] + ([topic.sme.email] if topic.sme else [])
        notify.send(people, f"[{account.code}] KT signed off: {topic.title}",
                    _topic_body(topic, f"{g.user.full_name} signed off this KT topic."))
    tell_if_ready(topic.module.project, was_ready)
    flash(f"Topic is now {topic.status_label}.", "success")
    return back


# --- receiver confirmation -----------------------------------------------------------------------
# Receivers confirm once the KT has been given to them.
CONFIRMABLE = ("DELIVERED", "REVERSE_KT")


@bp.route("/topics/<int:topic_id>/confirm", methods=["POST"])
@login_required
def confirm_receipt(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    receipt = topic.receiver(g.user)
    if receipt is None:
        abort(403)
    if topic.status not in CONFIRMABLE:
        abort(400, description="You can confirm once the KT has been delivered and before it is signed off.")
    receipt.confirmed_at, receipt.confirm_note = utcnow(), request.form.get("note", "").strip()[:2000]
    add_kt_event(topic, g.user, "RECEIPT_CONFIRMED", comment=receipt.confirm_note or None)
    db.session.commit()
    if not topic.unconfirmed_receivers:
        notify.send([u.email for u in _leads(account)], f"[{account.code}] All receivers confirmed: {topic.title}",
                    _topic_body(topic, "Every receiver has confirmed they received this KT. It can be signed off."))
    flash("Thanks, your confirmation is recorded.", "success")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#receivers")


# --- sessions ------------------------------------------------------------------------------------
@bp.route("/topics/<int:topic_id>/sessions", methods=["POST"])
@login_required
def add_session(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    if not can_run(g.user, account, topic):
        abort(403)
    if not topic.is_open:
        abort(400, description="Sessions cannot be added to a signed-off or cancelled topic.")
    form = request.form
    day, time = parse_date(form.get("date", "")), form.get("time", "").strip()
    starts_at = ist_local_to_utc(day, time) if day and TIME_RE.match(time) else None
    duration = form.get("duration", "60").strip()
    link, notes = form.get("meeting_link", "").strip(), form.get("notes", "").strip()[:2000]
    errors = []
    if starts_at is None:
        errors.append("Enter the session date and time (IST).")
    if not duration.isdigit() or not 15 <= int(duration) <= 480:
        errors.append("Duration must be 15 to 480 minutes.")
    if link and (not URL_RE.match(link) or len(link) > 500):
        errors.append("The meeting link must start with https://.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#sessions")
    session = KTSession(topic=topic, starts_at=starts_at, duration_minutes=int(duration), meeting_link=link,
                        notes=notes, created_by_id=g.user.id, created_at=utcnow())
    db.session.add(session)
    add_kt_event(topic, g.user, "SESSION_SCHEDULED", new_value=format_ist(starts_at))
    if topic.status in ("PLANNED", "ON_HOLD"):
        _move(topic, "SCHEDULED", "Session scheduled")
    db.session.commit()
    people = [u.email for u in topic.receiver_users] + ([topic.sme.email] if topic.sme else [])
    details = f"When: {format_ist(starts_at)} ({session.duration_minutes} minutes)\n" + \
        (f"Join: {link}\n" if link else "") + (f"Notes: {notes}\n" if notes else "")
    notify.send(people, f"[{account.code}] KT session: {topic.title} on {format_ist(starts_at)}",
                _topic_body(topic, "A KT session has been scheduled.\n\n" + details))
    flash(f"Session on {format_ist(starts_at)} added; the SME and receivers were emailed.", "success")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#sessions")


@bp.route("/topics/<int:topic_id>/sessions/<int:session_id>/held", methods=["POST"])
@login_required
def session_held(code, topic_id, session_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    session = db.session.get(KTSession, session_id)
    if session is None or session.topic_id != topic.id:
        abort(404)
    if not can_run(g.user, account, topic):
        abort(403)
    recording = request.form.get("recording_link", "").strip()
    if recording and (not URL_RE.match(recording) or len(recording) > 500):
        flash("The recording link must start with https://.", "error")
        return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#sessions")
    receiver_ids = {r.user_id for r in topic.receivers}
    present = {int(v) for v in request.form.getlist("attended") if v.isdigit()} & receiver_ids
    session.held = True
    session.recording_link = recording
    session.attendance = [KTAttendance(user_id=uid) for uid in sorted(present)]
    add_kt_event(topic, g.user, "SESSION_HELD", new_value=format_ist(session.starts_at),
                 comment=f"{len(present)} of {len(receiver_ids)} receivers attended")
    if topic.status == "SCHEDULED":
        _move(topic, "DELIVERED", "Session held")
    db.session.commit()
    flash("Session recorded as held.", "success")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#sessions")


# --- understanding ratings -----------------------------------------------------------------------
@bp.route("/topics/<int:topic_id>/rating", methods=["POST"])
@login_required
def rate(code, topic_id):
    account = visible_account_or_404(code)
    topic = _topic_or_404(account, topic_id)
    if not topic.is_receiver(g.user):
        abort(403)
    score = request.form.get("score", "")
    if score not in ("1", "2", "3", "4", "5"):
        flash("Choose how well you understood the topic (1 to 5).", "error")
        return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#understanding")
    comment = request.form.get("comment", "").strip()[:2000]
    rating = KTRating.query.filter_by(topic_id=topic.id, user_id=g.user.id).first()
    old = rating.score if rating else None
    if rating is None:
        rating = KTRating(topic=topic, user_id=g.user.id)
        db.session.add(rating)
    rating.score, rating.comment, rating.updated_at = int(score), comment, utcnow()
    add_kt_event(topic, g.user, "RATED", str(old) if old else None, score, comment or None)
    db.session.commit()
    if rating.score <= LOW_RATING and (old is None or old > LOW_RATING):
        people = [u.email for u in _leads(account)] + ([topic.sme.email] if topic.sme else [])
        notify.send(people, f"[{account.code}] Follow-up needed: {topic.title}",
                    _topic_body(topic, f"{g.user.full_name} rated their understanding {rating.score} of 5."
                                + (f"\nComment: {comment}" if comment else "")
                                + "\nPlease plan a follow-up session."))
    flash("Thanks, your rating is saved.", "success")
    return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id) + "#understanding")


# --- for the home page ---------------------------------------------------------------------------
def my_kt(user, limit=20):
    """Open topics where the user is the SME or a receiver, in their visible accounts, soonest first."""
    account_ids = [a.id for a in user.visible_accounts()]
    if not account_ids:
        return [], []
    as_receiver = db.session.query(KTReceiver.topic_id).filter(KTReceiver.user_id == user.id)
    topics = (KTTopic.query.filter(KTTopic.account_id.in_(account_ids), KTTopic.status.notin_(KT_CLOSED))
              .filter((KTTopic.sme_id == user.id) | KTTopic.id.in_(as_receiver))
              .order_by(KTTopic.due_date.is_(None), KTTopic.due_date, KTTopic.id).limit(limit).all())
    upcoming = (KTSession.query.join(KTTopic)
                .filter(KTTopic.id.in_([t.id for t in topics]), KTSession.held.is_(False),
                        KTSession.starts_at >= utcnow())
                .order_by(KTSession.starts_at).limit(10).all()) if topics else []
    return topics, upcoming

