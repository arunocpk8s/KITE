"""SOPs (versions, review and approval, review dates) and each account's scope of work.

SOP workflow, per version: DRAFT -> IN_REVIEW -> APPROVED. A reviewer can send a version back to DRAFT.
Approving a new version supersedes the previous approved one, which stays readable in the history.
Changing an approved SOP starts a new draft version; the approved one stays in force until the new one
is approved. Each approval sets the next review date (approval date + review interval). A periodic review
with no changes moves the review date on. Retired SOPs stay readable.

Who may do what (checked on the server): members view; SMEs and KT leads write SOPs; the owner or a KT
lead submits a version for review; KT leads approve, send back, retire and mark reviews, and may not
approve a version they wrote themselves (admins may). KT leads edit the scope of work.
"""
import click
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

import notify
from auth import login_required
from kt import URL_RE, members_of, visible_account_or_404
from models import (
    DEFAULT_REVIEW_MONTHS, SCOPE_KINDS, SOP_STATUS_LABELS, Account, KTProject, ScopeItem, Sop, SopVersion,
    add_sop_event, db,
)
from timeutil import add_months, format_date, ist_today, parse_date, utcnow

bp = Blueprint("sops", __name__, url_prefix="/accounts/<code>")

REVIEW_SOON_DAYS = 30


# --- access helpers ------------------------------------------------------------------------------
def can_author(user, account):
    return user.can_manage(account) or (user.role == "sme" and user.can_view(account))


def can_edit(user, account, sop):
    return user.can_manage(account) or (sop.owner_id == user.id and can_author(user, account))


def _sop_or_404(account, sop_id):
    sop = db.session.get(Sop, sop_id)
    if sop is None or sop.account_id != account.id:
        abort(404)
    return sop


def _require(allowed):
    if not allowed:
        abort(403)


def _back(sop):
    return redirect(url_for("sops.sop", code=sop.account.code, sop_id=sop.id))


def _sop_body(sop, intro):
    return (
        f"{intro}\n\n"
        f"Account: {sop.account.code} - {sop.account.name}\n"
        f"SOP:     {sop.code} {sop.title}\n"
        f"Owner:   {sop.owner.full_name if sop.owner else 'not assigned'}\n\n"
        f"Open the SOP: {notify.app_url(url_for('sops.sop', code=sop.account.code, sop_id=sop.id))}"
        + notify.footer()
    )


def sop_stats(account, today=None):
    today = today or ist_today()
    sops = Sop.query.filter_by(account_id=account.id).all()
    live = [s for s in sops if s.status != "RETIRED"]
    return {
        "total": len(live),
        "approved": sum(1 for s in live if s.status == "APPROVED"),
        "in_review": sum(1 for s in live if s.working_version and s.working_version.status == "IN_REVIEW"),
        "review_overdue": sum(1 for s in live if s.review_overdue(today)),
    }


# --- list ----------------------------------------------------------------------------------------
@bp.route("/sops")
@login_required
def sop_list(code):
    account = visible_account_or_404(code)
    today = ist_today()
    status = request.args.get("status", "").upper()
    sops = Sop.query.filter_by(account_id=account.id).order_by(Sop.number).all()
    if status == "REVIEW_DUE":
        sops = [s for s in sops if s.status == "APPROVED" and s.review_due
                and (s.review_due - today).days <= REVIEW_SOON_DAYS]
    elif status in SOP_STATUS_LABELS:
        sops = [s for s in sops if s.status == status]
    elif status != "ALL":
        sops = [s for s in sops if s.status != "RETIRED"]
    return render_template("sops/list.html", account=account, sops=sops, today=today, status=status,
                           labels=SOP_STATUS_LABELS, stats=sop_stats(account, today),
                           can_author=can_author(g.user, account), soon=REVIEW_SOON_DAYS)


# --- create / edit -------------------------------------------------------------------------------
def _read_sop(form):
    return {
        "title": " ".join(form.get("title", "").split()),
        "owner_id": form.get("owner_id", ""),
        "project_id": form.get("project_id", ""),
        "review_months": form.get("review_months", str(DEFAULT_REVIEW_MONTHS)).strip(),
        "purpose": form.get("purpose", "").strip(),
        "steps": form.get("steps", "").strip(),
        "link": form.get("link", "").strip(),
        "change_note": form.get("change_note", "").strip(),
    }


def _validate_sop(account, data):
    errors = {}
    if not data["title"] or len(data["title"]) > 200:
        errors["title"] = "Enter the SOP title (up to 200 characters)."
    owners = {u.id for u in members_of(account) if u.role in ("sme", "kt_lead", "admin")}
    if not data["owner_id"].isdigit() or int(data["owner_id"]) not in owners:
        errors["owner_id"] = "Choose the owner (an SME or KT lead of this account)."
    if data["project_id"]:
        project = db.session.get(KTProject, int(data["project_id"])) if data["project_id"].isdigit() else None
        if project is None or project.account_id != account.id:
            errors["project_id"] = "Choose a project of this account."
    if not data["review_months"].isdigit() or not 1 <= int(data["review_months"]) <= 36:
        errors["review_months"] = "Review every 1 to 36 months."
    if not data["purpose"] or len(data["purpose"]) > 4000:
        errors["purpose"] = "Say what the SOP is for (up to 4,000 characters)."
    if len(data["steps"]) > 20000:
        errors["steps"] = "At most 20,000 characters; keep long procedures in the SharePoint document."
    if not data["steps"] and not data["link"]:
        errors["steps"] = "Write the steps, or link the SOP document in SharePoint."
    if data["link"] and (not URL_RE.match(data["link"]) or len(data["link"]) > 500):
        errors["link"] = "The document link must start with https://."
    if len(data["change_note"]) > 2000:
        errors["change_note"] = "At most 2,000 characters."
    return errors


def _render_form(account, sop, data, errors, status=200):
    return render_template(
        "sops/form.html", account=account, sop=sop, form=data, errors=errors,
        owners=[u for u in members_of(account) if u.role in ("sme", "kt_lead", "admin")],
        projects=KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all(),
        manager=g.user.can_manage(account),
    ), status


@bp.route("/sops/new", methods=["GET", "POST"])
@login_required
def new_sop(code):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    if request.method == "GET":
        data = _read_sop({})
        data.update(owner_id=str(g.user.id), project_id=request.args.get("project", ""))
        return _render_form(account, None, data, {})
    data = _read_sop(request.form)
    if not g.user.can_manage(account):
        data["owner_id"] = str(g.user.id)  # SMEs write their own SOPs
    errors = _validate_sop(account, data)
    if errors:
        return _render_form(account, None, data, errors, 400)
    number = (db.session.query(func.max(Sop.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    sop = Sop(account_id=account.id, number=number, title=data["title"], owner_id=int(data["owner_id"]),
              project_id=int(data["project_id"]) if data["project_id"] else None,
              review_months=int(data["review_months"]), created_at=utcnow(), updated_at=utcnow())
    sop.versions.append(SopVersion(number=1, purpose=data["purpose"], steps=data["steps"], link=data["link"],
                                   change_note=data["change_note"] or "First version", created_by_id=g.user.id,
                                   created_at=utcnow(), updated_at=utcnow()))
    db.session.add(sop)
    add_sop_event(sop, g.user, "CREATED", 1)
    db.session.commit()
    flash(f"{sop.code} saved as a draft. Submit it for review when it is ready.", "success")
    return _back(sop)


@bp.route("/sops/<int:sop_id>/edit", methods=["GET", "POST"])
@login_required
def edit_sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(can_edit(g.user, account, sop))
    if sop.retired_at:
        abort(400, description="A retired SOP cannot be changed.")
    working = sop.working_version
    if working is not None and working.status == "IN_REVIEW":
        flash(f"Version {working.number} is in review. It must be approved or sent back before it can change.", "error")
        return _back(sop)
    base = working or sop.versions[-1]
    if request.method == "GET":
        data = {"title": sop.title, "owner_id": str(sop.owner_id or ""), "project_id": str(sop.project_id or ""),
                "review_months": str(sop.review_months), "purpose": base.purpose, "steps": base.steps,
                "link": base.link, "change_note": working.change_note if working else ""}
        return _render_form(account, sop, data, {})
    data = _read_sop(request.form)
    if not g.user.can_manage(account):
        data["owner_id"] = str(sop.owner_id or "")
    errors = _validate_sop(account, data)
    if working is None and not data["change_note"]:
        errors["change_note"] = "Say what changes in this new version."
    if errors:
        return _render_form(account, sop, data, errors, 400)
    sop.title, sop.owner_id = data["title"], int(data["owner_id"])
    sop.project_id = int(data["project_id"]) if data["project_id"] else None
    sop.review_months, sop.updated_at = int(data["review_months"]), utcnow()
    if working is None:
        working = SopVersion(number=base.number + 1, created_by_id=g.user.id, created_at=utcnow())
        sop.versions.append(working)
        add_sop_event(sop, g.user, "NEW_VERSION", working.number, data["change_note"])
        message = f"Version {working.number} started as a draft. Version {base.number} stays in force until it is approved."
    else:
        add_sop_event(sop, g.user, "EDITED", working.number)
        message = f"Draft version {working.number} saved."
    working.purpose, working.steps, working.link = data["purpose"], data["steps"], data["link"]
    working.change_note, working.updated_at = data["change_note"], utcnow()
    db.session.commit()
    flash(message, "success")
    return _back(sop)


# --- view ----------------------------------------------------------------------------------------
@bp.route("/sops/<int:sop_id>")
@login_required
def sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    wanted = request.args.get("v", "")
    shown = next((v for v in sop.versions if str(v.number) == wanted), None) if wanted else None
    shown = shown or sop.approved_version or sop.versions[-1]
    manager = g.user.can_manage(account)
    working = sop.working_version
    return render_template(
        "sops/sop.html", account=account, sop=sop, shown=shown, working=working, today=ist_today(),
        can_edit=can_edit(g.user, account, sop) and not sop.retired_at, manager=manager,
        can_approve=manager and working is not None and working.status == "IN_REVIEW"
        and (g.user.is_admin or working.created_by_id != g.user.id),
    )


# --- workflow ------------------------------------------------------------------------------------
@bp.route("/sops/<int:sop_id>/submit", methods=["POST"])
@login_required
def submit_sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(can_edit(g.user, account, sop))
    working = sop.working_version
    if sop.retired_at or working is None or working.status != "DRAFT":
        abort(400, description="Only a draft version can be submitted for review.")
    working.status, working.submitted_at, working.submitted_by_id = "IN_REVIEW", utcnow(), g.user.id
    add_sop_event(sop, g.user, "SUBMITTED", working.number)
    db.session.commit()
    leads = [u.email for u in members_of(account) if u.role == "kt_lead" and u.id != g.user.id]
    notify.send(leads, f"[{account.code}] SOP to review: {sop.code} {sop.title} v{working.number}",
                _sop_body(sop, f"{g.user.full_name} submitted version {working.number} for review."
                               + (f"\nWhat changed: {working.change_note}" if working.change_note else "")))
    flash(f"Version {working.number} submitted for review; the KT leads were emailed.", "success")
    return _back(sop)


@bp.route("/sops/<int:sop_id>/approve", methods=["POST"])
@login_required
def approve_sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(g.user.can_manage(account))
    working = sop.working_version
    if sop.retired_at or working is None or working.status != "IN_REVIEW":
        abort(400, description="Only a version in review can be approved.")
    if working.created_by_id == g.user.id and not g.user.is_admin:
        abort(403, description="Another KT lead must approve a version you wrote.")
    previous = sop.approved_version
    if previous is not None:
        previous.status = "SUPERSEDED"
    working.status, working.approved_at, working.approved_by_id = "APPROVED", utcnow(), g.user.id
    sop.review_due, sop.updated_at = add_months(ist_today(), sop.review_months), utcnow()
    add_sop_event(sop, g.user, "APPROVED", working.number, request.form.get("comment", "").strip()[:2000] or None)
    db.session.commit()
    if sop.owner:
        notify.send([sop.owner.email], f"[{account.code}] SOP approved: {sop.code} {sop.title} v{working.number}",
                    _sop_body(sop, f"{g.user.full_name} approved version {working.number}. "
                                   f"Next review by {format_date(sop.review_due)}."))
    flash(f"Version {working.number} approved. Next review by {format_date(sop.review_due)}.", "success")
    return _back(sop)


@bp.route("/sops/<int:sop_id>/send-back", methods=["POST"])
@login_required
def send_back_sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(g.user.can_manage(account))
    working = sop.working_version
    if working is None or working.status != "IN_REVIEW":
        abort(400, description="Only a version in review can be sent back.")
    comment = request.form.get("comment", "").strip()[:2000]
    if not comment:
        flash("Say what needs to change before sending the SOP back.", "error")
        return _back(sop)
    working.status, working.submitted_at, working.submitted_by_id = "DRAFT", None, None
    add_sop_event(sop, g.user, "SENT_BACK", working.number, comment)
    db.session.commit()
    if sop.owner:
        notify.send([sop.owner.email], f"[{account.code}] SOP sent back: {sop.code} {sop.title} v{working.number}",
                    _sop_body(sop, f"{g.user.full_name} sent version {working.number} back for changes.\n"
                                   f"Comment: {comment}"))
    flash(f"Version {working.number} sent back to the owner.", "success")
    return _back(sop)


@bp.route("/sops/<int:sop_id>/reviewed", methods=["POST"])
@login_required
def mark_reviewed(code, sop_id):
    """Periodic review with no changes: the approved version stays and the review date moves on."""
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(g.user.can_manage(account))
    if sop.status != "APPROVED":
        abort(400, description="Only an approved SOP can be marked as reviewed.")
    sop.review_due, sop.updated_at = add_months(ist_today(), sop.review_months), utcnow()
    add_sop_event(sop, g.user, "REVIEWED", sop.approved_version.number,
                  request.form.get("comment", "").strip()[:2000] or None)
    db.session.commit()
    flash(f"Reviewed with no changes. Next review by {format_date(sop.review_due)}.", "success")
    return _back(sop)


@bp.route("/sops/<int:sop_id>/retire", methods=["POST"])
@login_required
def retire_sop(code, sop_id):
    account = visible_account_or_404(code)
    sop = _sop_or_404(account, sop_id)
    _require(g.user.can_manage(account))
    reason = request.form.get("reason", "").strip()[:2000]
    if sop.retired_at:
        abort(400, description="The SOP is already retired.")
    if not reason:
        flash("Give a reason for retiring the SOP.", "error")
        return _back(sop)
    sop.retired_at, sop.updated_at = utcnow(), utcnow()
    add_sop_event(sop, g.user, "RETIRED", comment=reason)
    db.session.commit()
    flash(f"{sop.code} retired. It stays readable in the SOP list (filter: Retired).", "success")
    return _back(sop)


# --- review reminders (flask sop-review-reminders) -----------------------------------------------
def send_review_reminders(today=None, days=14):
    """Email each SOP owner about approved SOPs whose review is due within `days` or overdue.
    Returns the number of emails sent or queued."""
    today = today or ist_today()
    due = [s for s in Sop.query.join(Account).filter(Account.active.is_(True), Sop.retired_at.is_(None),
                                                      Sop.review_due.isnot(None)).all()
           if s.status == "APPROVED" and (s.review_due - today).days <= days]
    by_owner = {}
    for sop in due:
        by_owner.setdefault(sop.owner, []).append(sop)
    for owner, sops in by_owner.items():
        lines = [f"- {s.account.code} {s.code} {s.title}: review by {format_date(s.review_due)}"
                 + (" (OVERDUE)" if today > s.review_due else "") + "\n  "
                 + notify.app_url(f"/accounts/{s.account.code}/sops/{s.id}") for s in sops]
        recipients = [owner.email] if owner else \
            [u.email for u in members_of(sops[0].account) if u.role == "kt_lead"]
        notify.send(recipients, f"SOP reviews due ({len(sops)})",
                    "These SOPs are due for their periodic review. Review each one; if nothing changes, ask "
                    "the KT lead to mark it reviewed, otherwise edit it to start a new version.\n\n"
                    + "\n".join(lines) + notify.footer())
    return len(by_owner)


def register_cli(app):
    @app.cli.command("sop-review-reminders")
    @click.option("--days", default=14, show_default=True, help="Remind this many days before the review date.")
    def sop_review_reminders(days):
        """Email SOP owners about reviews that are due soon or overdue (run daily)."""
        with app.test_request_context():
            sent = send_review_reminders(days=days)
        click.echo(f"Sent review reminders to {sent} owner(s).")


# --- scope of work -------------------------------------------------------------------------------
def _scope_back(account, anchor=""):
    return redirect(url_for("sops.scope", code=account.code) + anchor)


@bp.route("/scope")
@login_required
def scope(code):
    account = visible_account_or_404(code)
    items = ScopeItem.query.filter_by(account_id=account.id).order_by(ScopeItem.position, ScopeItem.id).all()
    grouped = [(kind, label, [i for i in items if i.kind == kind]) for kind, label in SCOPE_KINDS.items()]
    return render_template("sops/scope.html", account=account, grouped=grouped, kinds=SCOPE_KINDS,
                           can_manage=g.user.can_manage(account), today=ist_today())


@bp.route("/scope/details", methods=["POST"])
@login_required
def scope_details(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    form = request.form
    start, end = parse_date(form.get("contract_start", "")), parse_date(form.get("contract_end", ""))
    link = form.get("scope_link", "").strip()
    errors = []
    if (form.get("contract_start", "").strip() and start is None) or (form.get("contract_end", "").strip() and end is None):
        errors.append("Use valid contract dates.")
    if start and end and end < start:
        errors.append("The contract end cannot be before its start.")
    if link and (not URL_RE.match(link) or len(link) > 500):
        errors.append("The statement of work link must start with https://.")
    if errors:
        for e in errors:
            flash(e, "error")
        return _scope_back(account)
    account.contract_start, account.contract_end, account.scope_link = start, end, link
    db.session.commit()
    flash("Contract details saved.", "success")
    return _scope_back(account)


def _read_item(form):
    return form.get("kind", ""), " ".join(form.get("title", "").split()), form.get("detail", "").strip()


def _item_errors(kind, title, detail):
    errors = []
    if kind not in SCOPE_KINDS:
        errors.append("Choose the section.")
    if not title or len(title) > 300:
        errors.append("Enter the item (up to 300 characters).")
    if len(detail) > 4000:
        errors.append("Details: at most 4,000 characters.")
    return errors


@bp.route("/scope/items", methods=["POST"])
@login_required
def add_scope_item(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    kind, title, detail = _read_item(request.form)
    errors = _item_errors(kind, title, detail)
    if errors:
        for e in errors:
            flash(e, "error")
        return _scope_back(account)
    position = (db.session.query(func.max(ScopeItem.position)).filter_by(account_id=account.id).scalar() or 0) + 1
    db.session.add(ScopeItem(account_id=account.id, kind=kind, title=title, detail=detail, position=position,
                             updated_at=utcnow(), updated_by_id=g.user.id))
    db.session.commit()
    flash(f"Added to {SCOPE_KINDS[kind]}.", "success")
    return _scope_back(account, f"#{kind}")


def _scope_item_or_404(account, item_id):
    item = db.session.get(ScopeItem, item_id)
    if item is None or item.account_id != account.id:
        abort(404)
    return item


@bp.route("/scope/items/<int:item_id>", methods=["POST"])
@login_required
def edit_scope_item(code, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    item = _scope_item_or_404(account, item_id)
    kind, title, detail = _read_item(request.form)
    errors = _item_errors(kind, title, detail)
    if errors:
        for e in errors:
            flash(e, "error")
        return _scope_back(account, f"#{item.kind}")
    item.kind, item.title, item.detail, item.updated_at, item.updated_by_id = kind, title, detail, utcnow(), g.user.id
    db.session.commit()
    flash("Scope item saved.", "success")
    return _scope_back(account, f"#{kind}")


@bp.route("/scope/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_scope_item(code, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    item = _scope_item_or_404(account, item_id)
    db.session.delete(item)
    db.session.commit()
    flash("Scope item removed.", "success")
    return _scope_back(account, f"#{item.kind}")
