"""Transaction catalogue and change log with acknowledgements.

- Catalogue: each client transaction (code, name, system, purpose, steps, frequency, criticality, owner
  SME, linked KT topic and SOP, SharePoint link). Searchable, so newcomers look things up here.
- Change log: an entry for a new, changed or retired transaction or process. A draft is published by
  its author or a KT lead; publishing emails the team, and everyone who was an active non-viewer member
  at that moment must acknowledge it. Published entries are not edited (publish a correcting entry).
  Publishing a "Retired" entry retires its linked transaction; "New" re-activates it.

Who may do what (checked on the server): members view; SMEs and KT leads edit the catalogue and write
change entries; KT leads publish any draft and send reminders; each person acknowledges for themselves.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_

import notify
from auth import login_required
from kt import URL_RE, members_of, visible_account_or_404
from models import (
    CHANGE_TYPES, TXN_CRITICALITY, TXN_FREQUENCIES, Change, ChangeAck, KTProject, KTTopic, Question, Sop, Transaction,
    db,
)
from sops import can_author
from timeutil import format_date, parse_date, utcnow

bp = Blueprint("transactions", __name__, url_prefix="/accounts/<code>")


def _require(allowed):
    if not allowed:
        abort(403)


def _in_account(model, value, account):
    """The row of `model` with id `value` if it belongs to the account, else None."""
    row = db.session.get(model, int(value)) if str(value).isdigit() else None
    return row if row is not None and row.account_id == account.id else None


def _choices(account):
    return {
        "projects": KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all(),
        "topics": KTTopic.query.filter_by(account_id=account.id).order_by(KTTopic.title).all(),
        "sops": Sop.query.filter_by(account_id=account.id).filter(Sop.retired_at.is_(None)).order_by(Sop.number).all(),
    }


# --- catalogue -----------------------------------------------------------------------------------
@bp.route("/transactions")
@login_required
def catalogue(code):
    account = visible_account_or_404(code)
    q = " ".join(request.args.get("q", "").split())[:100]
    criticality = request.args.get("criticality", "")
    show = request.args.get("show", "")
    query = Transaction.query.filter_by(account_id=account.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Transaction.code.ilike(like), Transaction.name.ilike(like),
                                 Transaction.system.ilike(like), Transaction.purpose.ilike(like)))
    if criticality in TXN_CRITICALITY:
        query = query.filter_by(criticality=criticality)
    if show != "all":
        query = query.filter_by(active=True)
    return render_template("transactions/catalogue.html", account=account,
                           transactions=query.order_by(Transaction.code).all(),
                           filters={"q": q, "criticality": criticality, "show": show},
                           criticalities=TXN_CRITICALITY, can_edit=can_author(g.user, account))


TXN_FIELDS = ("code", "name", "system", "purpose", "steps", "frequency", "criticality", "owner_id",
              "project_id", "topic_id", "sop_id", "link")


def _read_txn(form):
    data = {f: form.get(f, "").strip() for f in TXN_FIELDS}
    data["code"] = data["code"].upper().replace(" ", "")
    data["name"] = " ".join(data["name"].split())
    return data


def _validate_txn(account, data, txn=None):
    errors = {}
    if not data["code"] or len(data["code"]) > 40:
        errors["code"] = "Enter the transaction code (up to 40 characters, no spaces)."
    else:
        clash = Transaction.query.filter_by(account_id=account.id, code=data["code"]).first()
        if clash is not None and clash is not txn:
            errors["code"] = f"{data['code']} is already in the catalogue."
    if not data["name"] or len(data["name"]) > 200:
        errors["name"] = "Enter the name (up to 200 characters)."
    if len(data["system"]) > 120:
        errors["system"] = "At most 120 characters."
    if not data["purpose"] or len(data["purpose"]) > 4000:
        errors["purpose"] = "Say what the transaction is for (up to 4,000 characters)."
    if len(data["steps"]) > 20000:
        errors["steps"] = "At most 20,000 characters."
    if data["frequency"] not in TXN_FREQUENCIES:
        errors["frequency"] = "Choose how often it is run."
    if data["criticality"] not in TXN_CRITICALITY:
        errors["criticality"] = "Choose the criticality."
    owners = {u.id for u in members_of(account) if u.role in ("sme", "kt_lead", "admin")}
    if data["owner_id"] and (not data["owner_id"].isdigit() or int(data["owner_id"]) not in owners):
        errors["owner_id"] = "The owner must be an SME or KT lead of this account."
    for field, model, label in (("project_id", KTProject, "project"), ("topic_id", KTTopic, "KT topic"),
                                ("sop_id", Sop, "SOP")):
        if data[field] and _in_account(model, data[field], account) is None:
            errors[field] = f"Choose a {label} of this account."
    if data["link"] and (not URL_RE.match(data["link"]) or len(data["link"]) > 500):
        errors["link"] = "The link must start with https://."
    return errors


def _render_txn_form(account, txn, data, errors, status=200):
    return render_template(
        "transactions/form.html", account=account, txn=txn, form=data, errors=errors,
        owners=[u for u in members_of(account) if u.role in ("sme", "kt_lead", "admin")],
        frequencies=TXN_FREQUENCIES, criticalities=TXN_CRITICALITY, **_choices(account),
    ), status


def _apply_txn(txn, data):
    for f in ("code", "name", "system", "purpose", "steps", "frequency", "criticality", "link"):
        setattr(txn, f, data[f])
    for f in ("owner_id", "project_id", "topic_id", "sop_id"):
        setattr(txn, f, int(data[f]) if data[f] else None)
    txn.updated_at, txn.updated_by_id = utcnow(), g.user.id


def _txn_or_404(account, txn_id):
    txn = db.session.get(Transaction, txn_id)
    if txn is None or txn.account_id != account.id:
        abort(404)
    return txn


@bp.route("/transactions/new", methods=["GET", "POST"])
@login_required
def new_transaction(code):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    if request.method == "GET":
        data = {f: "" for f in TXN_FIELDS}
        data.update(frequency="Monthly", criticality="Medium", owner_id=str(g.user.id))
        return _render_txn_form(account, None, data, {})
    data = _read_txn(request.form)
    errors = _validate_txn(account, data)
    if errors:
        return _render_txn_form(account, None, data, errors, 400)
    txn = Transaction(account_id=account.id, created_at=utcnow())
    _apply_txn(txn, data)
    db.session.add(txn)
    db.session.commit()
    flash(f"{txn.code} added to the catalogue. Publish a change-log entry if the team needs to know.", "success")
    return redirect(url_for("transactions.transaction", code=account.code, txn_id=txn.id))


@bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(code, txn_id):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    txn = _txn_or_404(account, txn_id)
    if request.method == "GET":
        data = {f: "" if getattr(txn, f) is None else str(getattr(txn, f)) for f in TXN_FIELDS}
        return _render_txn_form(account, txn, data, {})
    data = _read_txn(request.form)
    errors = _validate_txn(account, data, txn)
    if errors:
        return _render_txn_form(account, txn, data, errors, 400)
    _apply_txn(txn, data)
    db.session.commit()
    flash("Transaction saved.", "success")
    return redirect(url_for("transactions.transaction", code=account.code, txn_id=txn.id))


@bp.route("/transactions/<int:txn_id>")
@login_required
def transaction(code, txn_id):
    account = visible_account_or_404(code)
    txn = _txn_or_404(account, txn_id)
    changes = Change.query.filter_by(transaction_id=txn.id, status="PUBLISHED").order_by(Change.published_at.desc()).all()
    doubts = Question.query.filter_by(transaction_id=txn.id).order_by(Question.id.desc()).all()
    return render_template("transactions/transaction.html", account=account, txn=txn, changes=changes,
                           doubts=doubts, can_edit=can_author(g.user, account))


# --- change log ----------------------------------------------------------------------------------
@bp.route("/changes")
@login_required
def change_log(code):
    account = visible_account_or_404(code)
    changes = Change.query.filter_by(account_id=account.id).order_by(
        Change.status != "DRAFT", Change.published_at.desc(), Change.number.desc()).all()
    manager = g.user.can_manage(account)
    visible = [c for c in changes if c.status == "PUBLISHED" or manager or c.created_by_id == g.user.id]
    return render_template("transactions/changes.html", account=account, changes=visible,
                           can_write=can_author(g.user, account), types=CHANGE_TYPES)


CHANGE_FIELDS = ("title", "change_type", "description", "impact", "effective_date", "project_id",
                 "transaction_id", "sop_id", "topic_id")


def _read_change(form):
    data = {f: form.get(f, "").strip() for f in CHANGE_FIELDS}
    data["title"] = " ".join(data["title"].split())
    return data


def _validate_change(account, data):
    errors = {}
    if not data["title"] or len(data["title"]) > 200:
        errors["title"] = "Enter a short title (up to 200 characters)."
    if data["change_type"] not in CHANGE_TYPES:
        errors["change_type"] = "Choose new, changed or retired."
    if not data["description"] or len(data["description"]) > 8000:
        errors["description"] = "Describe what changed (up to 8,000 characters)."
    if len(data["impact"]) > 4000:
        errors["impact"] = "At most 4,000 characters."
    if data["effective_date"] and parse_date(data["effective_date"]) is None:
        errors["effective_date"] = "Use a valid date."
    for field, model, label in (("project_id", KTProject, "project"), ("transaction_id", Transaction, "transaction"),
                                ("sop_id", Sop, "SOP"), ("topic_id", KTTopic, "KT topic")):
        if data[field] and _in_account(model, data[field], account) is None:
            errors[field] = f"Choose a {label} of this account."
    if data["change_type"] == "RETIRED" and not (data["transaction_id"] or data["sop_id"]):
        errors["transaction_id"] = "Say which transaction (or SOP) is retired."
    return errors


def _render_change_form(account, change, data, errors, status=200):
    return render_template(
        "transactions/change_form.html", account=account, change=change, form=data, errors=errors, types=CHANGE_TYPES,
        transactions=Transaction.query.filter_by(account_id=account.id).order_by(Transaction.code).all(),
        **_choices(account),
    ), status


def _apply_change(change, data):
    change.title, change.change_type = data["title"], data["change_type"]
    change.description, change.impact = data["description"], data["impact"]
    change.effective_date = parse_date(data["effective_date"])
    for f in ("project_id", "transaction_id", "sop_id", "topic_id"):
        setattr(change, f, int(data[f]) if data[f] else None)


def _change_or_404(account, change_id):
    change = db.session.get(Change, change_id)
    if change is None or change.account_id != account.id:
        abort(404)
    if change.status == "DRAFT" and not (g.user.can_manage(account) or change.created_by_id == g.user.id):
        abort(404)  # drafts are private to their author and the KT leads
    return change


def _can_edit_draft(account, change):
    return change.status == "DRAFT" and (g.user.can_manage(account) or change.created_by_id == g.user.id)


@bp.route("/changes/new", methods=["GET", "POST"])
@login_required
def new_change(code):
    account = visible_account_or_404(code)
    _require(can_author(g.user, account))
    if request.method == "GET":
        data = {f: "" for f in CHANGE_FIELDS}
        data.update(change_type="CHANGED", transaction_id=request.args.get("transaction", ""))
        return _render_change_form(account, None, data, {})
    data = _read_change(request.form)
    errors = _validate_change(account, data)
    if errors:
        return _render_change_form(account, None, data, errors, 400)
    number = (db.session.query(func.max(Change.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    change = Change(account_id=account.id, number=number, created_by_id=g.user.id, created_at=utcnow())
    _apply_change(change, data)
    db.session.add(change)
    db.session.commit()
    flash(f"{change.code} saved as a draft. Publish it to email the team.", "success")
    return redirect(url_for("transactions.change", code=account.code, change_id=change.id))


@bp.route("/changes/<int:change_id>/edit", methods=["GET", "POST"])
@login_required
def edit_change(code, change_id):
    account = visible_account_or_404(code)
    change = _change_or_404(account, change_id)
    if not _can_edit_draft(account, change):
        abort(403)
    if request.method == "GET":
        data = {f: "" if getattr(change, f) is None else str(getattr(change, f)) for f in CHANGE_FIELDS}
        return _render_change_form(account, change, data, {})
    data = _read_change(request.form)
    errors = _validate_change(account, data)
    if errors:
        return _render_change_form(account, change, data, errors, 400)
    _apply_change(change, data)
    db.session.commit()
    flash("Draft saved.", "success")
    return redirect(url_for("transactions.change", code=account.code, change_id=change.id))


@bp.route("/changes/<int:change_id>/delete", methods=["POST"])
@login_required
def delete_change(code, change_id):
    account = visible_account_or_404(code)
    change = _change_or_404(account, change_id)
    if not _can_edit_draft(account, change):
        abort(403)
    db.session.delete(change)
    db.session.commit()
    flash("Draft deleted.", "success")
    return redirect(url_for("transactions.change_log", code=account.code))


@bp.route("/changes/<int:change_id>")
@login_required
def change(code, change_id):
    account = visible_account_or_404(code)
    change = _change_or_404(account, change_id)
    return render_template("transactions/change.html", account=account, change=change,
                           can_edit=_can_edit_draft(account, change), my_ack=change.ack_for(g.user),
                           manager=g.user.can_manage(account))


def _change_body(change, intro):
    links = [f"Transaction: {change.transaction.code} {change.transaction.name}" if change.transaction else "",
             f"SOP: {change.sop.code} {change.sop.title}" if change.sop else "",
             f"KT topic: {change.topic.title}" if change.topic else ""]
    return (
        f"{intro}\n\n"
        f"Account: {change.account.code} - {change.account.name}\n"
        f"{change.code} ({change.type_label}): {change.title}\n"
        + (f"Effective: {format_date(change.effective_date)}\n" if change.effective_date else "")
        + "".join(f"{line}\n" for line in links if line)
        + f"\nWhat changed:\n{change.description}\n"
        + (f"\nImpact:\n{change.impact}\n" if change.impact else "")
        + f"\nOpen it and acknowledge: "
          f"{notify.app_url(url_for('transactions.change', code=change.account.code, change_id=change.id))}"
        + notify.footer()
    )


@bp.route("/changes/<int:change_id>/publish", methods=["POST"])
@login_required
def publish_change(code, change_id):
    account = visible_account_or_404(code)
    change = _change_or_404(account, change_id)
    if not _can_edit_draft(account, change):
        abort(403)
    audience = [u for u in members_of(account) if u.role != "viewer" and u.id != g.user.id]
    change.status, change.published_at, change.published_by_id = "PUBLISHED", utcnow(), g.user.id
    change.acks = [ChangeAck(user_id=u.id) for u in audience]
    if change.transaction is not None and change.change_type in ("RETIRED", "NEW"):
        change.transaction.active = change.change_type == "NEW"
        change.transaction.updated_at, change.transaction.updated_by_id = utcnow(), g.user.id
    db.session.commit()
    notify.send([u.email for u in audience], f"[{account.code}] {change.type_label}: {change.title} ({change.code})",
                _change_body(change, f"{g.user.full_name} published a change. Please read it and acknowledge it in KITE."))
    flash(f"{change.code} published; {len(audience)} people were asked to acknowledge it.", "success")
    return redirect(url_for("transactions.change", code=account.code, change_id=change.id))


@bp.route("/changes/<int:change_id>/ack", methods=["POST"])
@login_required
def acknowledge(code, change_id):
    account = visible_account_or_404(code)
    change = _change_or_404(account, change_id)
    ack = change.ack_for(g.user)
    if change.status != "PUBLISHED" or ack is None:
        abort(403)
    if ack.acked_at is None:
        ack.acked_at = utcnow()
    ack.comment = request.form.get("comment", "").strip()[:2000]
    db.session.commit()
    flash("Thanks, your acknowledgement is recorded.", "success")
    return redirect(url_for("transactions.change", code=account.code, change_id=change.id))


@bp.route("/changes/<int:change_id>/remind", methods=["POST"])
@login_required
def remind(code, change_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    change = _change_or_404(account, change_id)
    pending = [a.user for a in change.pending_acks if a.user.active]
    if change.status != "PUBLISHED" or not pending:
        flash("Nobody is waiting to acknowledge this change.", "info")
    else:
        notify.send([u.email for u in pending], f"[{account.code}] Reminder: please acknowledge {change.code} {change.title}",
                    _change_body(change, f"Reminder from {g.user.full_name}: you have not acknowledged this change yet."))
        flash(f"Reminder emailed to {len(pending)} people.", "success")
    return redirect(url_for("transactions.change", code=account.code, change_id=change.id))
