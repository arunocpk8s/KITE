"""Backlog: an account's open work (KT gaps, tasks, bugs, improvements) with priority, owner, target date and status.

It sits beside the change log, which announces what has changed to the team. Who may do what (checked on the server):
members view; everyone except viewers adds, edits and moves items; KT leads, admins and an item's author delete it.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from auth import login_required
from kt import members_of, visible_account_or_404
from models import (
    BACKLOG_PRIORITIES, BACKLOG_STATUSES, BACKLOG_TYPES, BacklogItem, KTProject, db,
)
from timeutil import ist_today, parse_date, utcnow

bp = Blueprint("backlog", __name__, url_prefix="/accounts/<code>/backlog")


def _item_or_404(account, item_id):
    item = db.session.get(BacklogItem, item_id)
    if item is None or item.account_id != account.id:
        abort(404)
    return item


def _require(allowed):
    if not allowed:
        abort(403)


def _projects(account):
    return KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all()


def _owners(account):
    return [u for u in members_of(account) if u.role != "viewer"]


@bp.route("")
@login_required
def board(code):
    account = visible_account_or_404(code)
    project_id = request.args.get("project", "")
    query = BacklogItem.query.filter_by(account_id=account.id)
    if project_id.isdigit():
        query = query.filter_by(project_id=int(project_id))
    items = query.order_by(BacklogItem.priority_rank, BacklogItem.target_date.is_(None), BacklogItem.target_date,
                           BacklogItem.id).all()
    columns = [(key, label, [i for i in items if i.status == key]) for key, label in BACKLOG_STATUSES.items()]
    return render_template("backlog/board.html", account=account, columns=columns, projects=_projects(account),
                           project_id=project_id, today=ist_today(), statuses=BACKLOG_STATUSES,
                           can_write=g.user.can_contribute(account))


def _read(form):
    return {
        "title": " ".join(form.get("title", "").split()), "detail": form.get("detail", "").strip(),
        "item_type": form.get("item_type", "TASK"), "priority": form.get("priority", "MEDIUM"),
        "project_id": form.get("project_id", ""), "owner_id": form.get("owner_id", ""),
        "target_date": form.get("target_date", "").strip(),
    }


def _errors(account, data):
    errors = {}
    if not data["title"] or len(data["title"]) > 200:
        errors["title"] = "Say what needs doing (up to 200 characters)."
    if len(data["detail"]) > 8000:
        errors["detail"] = "At most 8,000 characters."
    if data["item_type"] not in BACKLOG_TYPES:
        errors["item_type"] = "Choose a type."
    if data["priority"] not in BACKLOG_PRIORITIES:
        errors["priority"] = "Choose a priority."
    if data["project_id"]:
        project = db.session.get(KTProject, int(data["project_id"])) if data["project_id"].isdigit() else None
        if project is None or project.account_id != account.id:
            errors["project_id"] = "Choose a project of this account."
    if data["owner_id"] and (not data["owner_id"].isdigit()
                             or int(data["owner_id"]) not in {u.id for u in _owners(account)}):
        errors["owner_id"] = "The owner must be a team member of this account."
    if data["target_date"] and parse_date(data["target_date"]) is None:
        errors["target_date"] = "Use a valid date."
    return errors


def _apply(item, data):
    item.title, item.detail, item.item_type = data["title"], data["detail"], data["item_type"]
    item.priority, item.priority_rank = data["priority"], list(BACKLOG_PRIORITIES).index(data["priority"])
    item.project_id = int(data["project_id"]) if data["project_id"] else None
    item.owner_id = int(data["owner_id"]) if data["owner_id"] else None
    item.target_date, item.updated_at = parse_date(data["target_date"]), utcnow()


def _render(account, item, data, errors):
    return render_template("backlog/form.html", account=account, item=item, form=data, errors=errors,
                           projects=_projects(account), owners=_owners(account), types=BACKLOG_TYPES,
                           priorities=BACKLOG_PRIORITIES), (400 if errors else 200)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new(code):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    data = _read(request.form if request.method == "POST" else request.args)
    errors = _errors(account, data) if request.method == "POST" else {}
    if request.method == "GET" or errors:
        return _render(account, None, data, errors)
    number = (db.session.query(func.max(BacklogItem.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    item = BacklogItem(account_id=account.id, number=number, created_by_id=g.user.id, created_at=utcnow())
    _apply(item, data)
    db.session.add(item)
    db.session.commit()
    flash(f"{item.code} added to the backlog.", "success")
    return redirect(url_for(".board", code=account.code))


@bp.route("/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def edit(code, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    item = _item_or_404(account, item_id)
    if request.method == "GET":
        data = {"title": item.title, "detail": item.detail, "item_type": item.item_type, "priority": item.priority,
                "project_id": str(item.project_id or ""), "owner_id": str(item.owner_id or ""),
                "target_date": item.target_date.isoformat() if item.target_date else ""}
        return _render(account, item, data, {})
    data = _read(request.form)
    errors = _errors(account, data)
    if errors:
        return _render(account, item, data, errors)
    _apply(item, data)
    db.session.commit()
    flash(f"{item.code} saved.", "success")
    return redirect(url_for(".board", code=account.code))


@bp.route("/<int:item_id>/status", methods=["POST"])
@login_required
def move(code, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    item = _item_or_404(account, item_id)
    status = request.form.get("status", "")
    if status not in BACKLOG_STATUSES:
        abort(400, description="Choose a status.")
    item.status, item.updated_at = status, utcnow()
    item.done_at = utcnow() if status == "DONE" else None
    db.session.commit()
    return redirect(url_for(".board", code=account.code))


@bp.route("/<int:item_id>/delete", methods=["POST"])
@login_required
def delete(code, item_id):
    account = visible_account_or_404(code)
    item = _item_or_404(account, item_id)
    _require(g.user.can_manage(account) or (item.created_by_id == g.user.id and g.user.can_contribute(account)))
    db.session.delete(item)
    db.session.commit()
    flash(f"{item.code} deleted.", "success")
    return redirect(url_for(".board", code=account.code))
