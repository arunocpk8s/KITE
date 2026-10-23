"""Newcomer onboarding: learning paths, assignment with a buddy, progress, help and readiness.

- A learning path (per account, optionally per project and role) is an ordered list of KT topics,
  SOPs, transactions and document links. SMEs and KT leads build paths.
- A KT lead assigns a path to a newcomer with a buddy (an SME) and a target date.
- The newcomer marks each item Read, Understood or Needs help. Needs help raises a doubt for the buddy
  (doubts.py), so the question is answered where the team answers all questions.
- When every required item is understood, the buddy (or a KT lead) records the newcomer's reverse KT;
  then the newcomer can be marked Ready. A KT lead can cancel an onboarding with a reason.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

import notify
from auth import login_required
from kt import URL_RE, members_of, visible_account_or_404
from models import (
    PATH_ITEM_KINDS, PROGRESS_STATUSES, KTProject, KTTopic, LearningPath, Onboarding, OnboardingProgress, PathItem,
    Question, Sop, Transaction, db,
)
from sops import can_author
from timeutil import format_date, ist_today, parse_date, utcnow

bp = Blueprint("onboarding", __name__, url_prefix="/accounts/<code>/onboarding")


def _require(allowed):
    if not allowed:
        abort(403)


def _path_or_404(account, path_id):
    path = db.session.get(LearningPath, path_id)
    if path is None or path.account_id != account.id:
        abort(404)
    return path


def _onboarding_or_404(account, onboarding_id):
    ob = db.session.get(Onboarding, onboarding_id)
    if ob is None or ob.account_id != account.id:
        abort(404)
    if not (g.user.id in (ob.newcomer_id, ob.buddy_id) or can_author(g.user, account)):
        abort(404)  # an onboarding is visible to the newcomer, the buddy, SMEs and KT leads
    return ob


def _can_guide(account, ob):
    """The buddy or a KT lead."""
    return g.user.can_manage(account) or ob.buddy_id == g.user.id


def _body(ob, intro):
    return (
        f"{intro}\n\n"
        f"Account: {ob.account.code} - {ob.account.name}\n"
        f"Learning path: {ob.path.name}\n"
        f"Newcomer: {ob.newcomer.full_name}\n"
        f"Buddy: {ob.buddy.full_name if ob.buddy else 'not assigned'}\n"
        f"Target date: {format_date(ob.target_date) or 'not set'}\n\n"
        f"Open it: {notify.app_url(url_for('onboarding.view', code=ob.account.code, onboarding_id=ob.id))}"
        + notify.footer()
    )


def is_ready_to_close(ob):
    return ob.status == "ACTIVE" and ob.understood == len(ob.required_items)


# --- overview ------------------------------------------------------------------------------------
@bp.route("")
@login_required
def index(code):
    account = visible_account_or_404(code)
    author = can_author(g.user, account)
    query = Onboarding.query.filter_by(account_id=account.id)
    if not author:
        query = query.filter((Onboarding.newcomer_id == g.user.id) | (Onboarding.buddy_id == g.user.id))
    onboardings = query.order_by(Onboarding.status, Onboarding.target_date.is_(None), Onboarding.target_date).all()
    paths = LearningPath.query.filter_by(account_id=account.id).order_by(LearningPath.name).all()
    people = [u for u in members_of(account) if u.role != "viewer"]
    return render_template("onboarding/index.html", account=account, onboardings=onboardings, paths=paths,
                           author=author, manager=g.user.can_manage(account), today=ist_today(),
                           newcomers=people, buddies=[u for u in people if u.role in ("sme", "kt_lead", "admin")])


# --- learning paths ------------------------------------------------------------------------------
def _read_path(form):
    return {"name": " ".join(form.get("name", "").split()), "for_role": " ".join(form.get("for_role", "").split()),
            "description": form.get("description", "").strip(), "project_id": form.get("project_id", "")}


def _path_errors(account, data):
    errors = {}
    if not data["name"] or len(data["name"]) > 200:
        errors["name"] = "Enter the path name (up to 200 characters)."
    if len(data["for_role"]) > 120:
        errors["for_role"] = "At most 120 characters."
    if len(data["description"]) > 4000:
        errors["description"] = "At most 4,000 characters."
    if data["project_id"]:
        project = db.session.get(KTProject, int(data["project_id"])) if data["project_id"].isdigit() else None
        if project is None or project.account_id != account.id:
            errors["project_id"] = "Choose a project of this account."
    return errors


def _projects(account):
    return KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all()


@bp.route("/paths/new", methods=["GET", "POST"])
@login_required
def new_path(code):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    data = _read_path(request.form) if request.method == "POST" else _read_path({})
    errors = _path_errors(account, data) if request.method == "POST" else {}
    if request.method == "GET" or errors:
        return render_template("onboarding/path_form.html", account=account, path=None, form=data, errors=errors,
                               projects=_projects(account)), (400 if errors else 200)
    path = LearningPath(account_id=account.id, name=data["name"], for_role=data["for_role"],
                        description=data["description"],
                        project_id=int(data["project_id"]) if data["project_id"] else None, created_at=utcnow())
    db.session.add(path)
    db.session.commit()
    flash(f"Learning path '{path.name}' created. Add what a newcomer should study, in order.", "success")
    return redirect(url_for("onboarding.path", code=account.code, path_id=path.id))


@bp.route("/paths/<int:path_id>/edit", methods=["GET", "POST"])
@login_required
def edit_path(code, path_id):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    path = _path_or_404(account, path_id)
    data = _read_path(request.form) if request.method == "POST" else \
        {"name": path.name, "for_role": path.for_role, "description": path.description,
         "project_id": str(path.project_id or "")}
    errors = _path_errors(account, data) if request.method == "POST" else {}
    if request.method == "GET" or errors:
        return render_template("onboarding/path_form.html", account=account, path=path, form=data, errors=errors,
                               projects=_projects(account)), (400 if errors else 200)
    path.name, path.for_role, path.description = data["name"], data["for_role"], data["description"]
    path.project_id = int(data["project_id"]) if data["project_id"] else None
    db.session.commit()
    flash("Learning path saved.", "success")
    return redirect(url_for("onboarding.path", code=account.code, path_id=path.id))


@bp.route("/paths/<int:path_id>")
@login_required
def path(code, path_id):
    account = visible_account_or_404(code)
    path = _path_or_404(account, path_id)
    people = [u for u in members_of(account) if u.role != "viewer"]
    return render_template(
        "onboarding/path.html", account=account, path=path, kinds=PATH_ITEM_KINDS, author=can_author(g.user, account),
        manager=g.user.can_manage(account), newcomers=people,
        buddies=[u for u in people if u.role in ("sme", "kt_lead", "admin")],
        topics=KTTopic.query.filter_by(account_id=account.id).filter(KTTopic.status != "CANCELLED")
        .order_by(KTTopic.title).all(),
        sops=Sop.query.filter_by(account_id=account.id).filter(Sop.retired_at.is_(None)).order_by(Sop.number).all(),
        transactions=Transaction.query.filter_by(account_id=account.id, active=True).order_by(Transaction.code).all(),
    )


@bp.route("/paths/<int:path_id>/items", methods=["POST"])
@login_required
def add_item(code, path_id):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    path = _path_or_404(account, path_id)
    form = request.form
    kind = form.get("kind", "")
    item = PathItem(kind=kind, note=form.get("note", "").strip()[:2000], required=form.get("required") == "on",
                    position=len(path.items) + 1)
    error = None
    refs = {"TOPIC": ("topic_id", KTTopic), "SOP": ("sop_id", Sop), "TRANSACTION": ("transaction_id", Transaction)}
    if kind in refs:
        field, model = refs[kind]
        value = form.get(field, "")
        row = db.session.get(model, int(value)) if value.isdigit() else None
        if row is None or row.account_id != account.id:
            error = f"Choose the {PATH_ITEM_KINDS[kind]}."
        else:
            setattr(item, field, row.id)
    elif kind == "LINK":
        item.title, item.url = " ".join(form.get("title", "").split())[:200], form.get("url", "").strip()
        if not item.title or not URL_RE.match(item.url) or len(item.url) > 500:
            error = "Give the document a title and an https:// link."
    else:
        error = "Choose what to add."
    if error:
        flash(error, "error")
    else:
        path.items.append(item)
        db.session.commit()
        flash(f"Added: {item.label}", "success")
    return redirect(url_for("onboarding.path", code=account.code, path_id=path.id) + "#items")


@bp.route("/paths/<int:path_id>/items/<int:item_id>/<action>", methods=["POST"])
@login_required
def change_item(code, path_id, item_id, action):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    path = _path_or_404(account, path_id)
    item = db.session.get(PathItem, item_id)
    if item is None or item.path_id != path.id or action not in ("up", "down", "delete"):
        abort(404)
    items = list(path.items)
    i = items.index(item)
    if action == "delete":
        OnboardingProgress.query.filter_by(item_id=item.id).delete()
        path.items.remove(item)
    else:
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
        for n, it in enumerate(items, start=1):
            it.position = n
    db.session.commit()
    return redirect(url_for("onboarding.path", code=account.code, path_id=path.id) + "#items")


# --- assigning a newcomer ------------------------------------------------------------------------
@bp.route("/assign", methods=["POST"])
@login_required
def assign(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    form = request.form
    path = _path_or_404(account, int(form["path_id"])) if form.get("path_id", "").isdigit() else None
    people = {u.id: u for u in members_of(account) if u.role != "viewer"}
    newcomer = people.get(int(form["newcomer_id"])) if form.get("newcomer_id", "").isdigit() else None
    buddy = people.get(int(form["buddy_id"])) if form.get("buddy_id", "").isdigit() else None
    target = parse_date(form.get("target_date", ""))
    errors = []
    if path is None:
        errors.append("Choose the learning path.")
    elif not path.items:
        errors.append("Add items to the learning path first.")
    if newcomer is None:
        errors.append("Choose the newcomer (an active member of this account).")
    if buddy is None or buddy.role not in ("sme", "kt_lead", "admin"):
        errors.append("Choose a buddy (an SME or KT lead of this account).")
    elif newcomer is not None and buddy.id == newcomer.id:
        errors.append("The buddy must be someone other than the newcomer.")
    if form.get("target_date", "").strip() and target is None:
        errors.append("Use a valid target date.")
    if path and newcomer and Onboarding.query.filter_by(path_id=path.id, newcomer_id=newcomer.id,
                                                        status="ACTIVE").first():
        errors.append(f"{newcomer.full_name} is already following this path.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(request.referrer or url_for("onboarding.index", code=account.code))
    ob = Onboarding(account_id=account.id, path_id=path.id, newcomer_id=newcomer.id, buddy_id=buddy.id,
                    target_date=target, assigned_by_id=g.user.id, created_at=utcnow())
    db.session.add(ob)
    db.session.commit()
    notify.send([newcomer.email, buddy.email], f"[{account.code}] Welcome: your learning path '{path.name}'",
                _body(ob, f"{g.user.full_name} assigned the learning path '{path.name}' to {newcomer.full_name}, "
                          f"with {buddy.full_name} as buddy. Work through each item and mark it Understood; "
                          "choose Needs help if you are stuck and your buddy will answer."))
    flash(f"{path.name} assigned to {newcomer.full_name}; both were emailed.", "success")
    return redirect(url_for("onboarding.view", code=account.code, onboarding_id=ob.id))


# --- following the path --------------------------------------------------------------------------
@bp.route("/<int:onboarding_id>")
@login_required
def view(code, onboarding_id):
    account = visible_account_or_404(code)
    ob = _onboarding_or_404(account, onboarding_id)
    return render_template("onboarding/view.html", account=account, ob=ob, statuses=PROGRESS_STATUSES,
                           me_newcomer=ob.newcomer_id == g.user.id, can_guide=_can_guide(account, ob),
                           manager=g.user.can_manage(account), today=ist_today(),
                           all_understood=is_ready_to_close(ob))


@bp.route("/<int:onboarding_id>/items/<int:item_id>", methods=["POST"])
@login_required
def update_item(code, onboarding_id, item_id):
    account = visible_account_or_404(code)
    ob = _onboarding_or_404(account, onboarding_id)
    _require(ob.newcomer_id == g.user.id)
    item = db.session.get(PathItem, item_id)
    if item is None or item.path_id != ob.path_id:
        abort(404)
    if ob.status != "ACTIVE":
        abort(400, description="This onboarding is closed.")
    status = request.form.get("status", "")
    if status not in PROGRESS_STATUSES:
        abort(400, description="Choose Read, Understood or Needs help.")
    note = request.form.get("note", "").strip()[:2000]
    progress = ob.progress_for(item)
    if progress is None:
        progress = OnboardingProgress(item_id=item.id, status=status)
        ob.progress.append(progress)
    progress.status, progress.note, progress.updated_at = status, note, utcnow()
    question = None
    if status == "NEEDS_HELP" and (progress.question is None or not progress.question.is_open):
        number = (db.session.query(func.max(Question.number)).filter_by(account_id=account.id).scalar() or 0) + 1
        question = Question(account_id=account.id, number=number, title=f"Help needed: {item.label}"[:200],
                            detail=note or f"Learning path '{ob.path.name}'.", asked_by_id=g.user.id,
                            assigned_to_id=ob.buddy_id, topic_id=item.topic_id, transaction_id=item.transaction_id,
                            project_id=ob.path.project_id, created_at=utcnow(), updated_at=utcnow())
        db.session.add(question)
        progress.question = question
    db.session.commit()
    if question is not None and ob.buddy:
        notify.send([ob.buddy.email], f"[{account.code}] {ob.newcomer.full_name} needs help: {item.label}",
                    _body(ob, f"{ob.newcomer.full_name} needs help with '{item.label}'."
                              + (f"\n\n{note}" if note else "") + f"\n\nAnswer it as doubt {question.code}."))
        flash(f"Your buddy was asked to help ({question.code}).", "success")
    else:
        flash(f"Marked {PROGRESS_STATUSES[status].lower()}: {item.label}", "success")
    if is_ready_to_close(ob) and status == "UNDERSTOOD" and ob.buddy:
        notify.send([ob.buddy.email], f"[{account.code}] {ob.newcomer.full_name} is ready for reverse KT",
                    _body(ob, "Every required item is understood. Hold the reverse KT and record it in KITE."))
    return redirect(url_for("onboarding.view", code=account.code, onboarding_id=ob.id) + f"#item-{item.id}")


@bp.route("/<int:onboarding_id>/reverse-kt", methods=["POST"])
@login_required
def reverse_kt(code, onboarding_id):
    account = visible_account_or_404(code)
    ob = _onboarding_or_404(account, onboarding_id)
    _require(_can_guide(account, ob))
    if not is_ready_to_close(ob):
        abort(400, description="Reverse KT is recorded once every required item is understood.")
    ob.reverse_kt_at, ob.reverse_kt_by_id = utcnow(), g.user.id
    ob.reverse_kt_note = request.form.get("note", "").strip()[:2000]
    db.session.commit()
    flash("Reverse KT recorded. The newcomer can now be marked ready.", "success")
    return redirect(url_for("onboarding.view", code=account.code, onboarding_id=ob.id))


@bp.route("/<int:onboarding_id>/ready", methods=["POST"])
@login_required
def mark_ready(code, onboarding_id):
    account = visible_account_or_404(code)
    ob = _onboarding_or_404(account, onboarding_id)
    _require(_can_guide(account, ob))
    if not is_ready_to_close(ob) or ob.reverse_kt_at is None:
        abort(400, description="Every required item must be understood and the reverse KT recorded first.")
    ob.status, ob.ready_at, ob.ready_by_id = "READY", utcnow(), g.user.id
    db.session.commit()
    leads = [u.email for u in members_of(account) if u.role == "kt_lead"]
    notify.send([ob.newcomer.email] + leads, f"[{account.code}] {ob.newcomer.full_name} is ready",
                _body(ob, f"{g.user.full_name} marked {ob.newcomer.full_name} ready after the learning path "
                          "and reverse KT. Congratulations!"))
    flash(f"{ob.newcomer.full_name} is ready.", "success")
    return redirect(url_for("onboarding.view", code=account.code, onboarding_id=ob.id))


@bp.route("/<int:onboarding_id>/cancel", methods=["POST"])
@login_required
def cancel(code, onboarding_id):
    account = visible_account_or_404(code)
    ob = _onboarding_or_404(account, onboarding_id)
    _require(g.user.can_manage(account))
    reason = request.form.get("reason", "").strip()[:2000]
    if ob.status != "ACTIVE":
        abort(400, description="Only an active onboarding can be cancelled.")
    if not reason:
        flash("Give a reason for cancelling.", "error")
    else:
        ob.status, ob.cancel_reason = "CANCELLED", reason
        db.session.commit()
        flash("Onboarding cancelled.", "success")
    return redirect(url_for("onboarding.view", code=account.code, onboarding_id=ob.id))


def my_onboarding(user):
    """Active onboardings where the user is the newcomer or the buddy."""
    return (Onboarding.query.filter(Onboarding.status == "ACTIVE",
                                    (Onboarding.newcomer_id == user.id) | (Onboarding.buddy_id == user.id)).all())

