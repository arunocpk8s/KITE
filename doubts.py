"""Doubts: technical questions that keep someone from moving on, answered by the SMEs.

Anyone in the team (not viewers) raises a doubt, optionally about a project, KT topic or transaction,
and marks it *blocking* if they cannot continue without an answer. It is assigned to the topic's SME,
else the transaction's owner, else left for the KT leads to assign. Replies from others mark it
Answered and email the asker; a reply from the asker reopens it and emails the assignee. The asker
(or a KT lead) resolves it once they can move on.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

import notify
import servicenow
from auth import login_required
from kt import members_of, visible_account_or_404
from models import QUESTION_STATUSES, KTProject, KTTopic, Question, QuestionReply, Transaction, User, db
from timeutil import utcnow

bp = Blueprint("doubts", __name__, url_prefix="/accounts/<code>/doubts")


def _require(allowed):
    if not allowed:
        abort(403)


def _question_or_404(account, qid):
    question = db.session.get(Question, qid)
    if question is None or question.account_id != account.id:
        abort(404)
    return question


def _answerers(account):
    return [u for u in members_of(account) if u.role in ("sme", "kt_lead", "admin")]


def _body(question, intro):
    about = [f"Project: {question.project.name}" if question.project else "",
             f"KT topic: {question.topic.title}" if question.topic else "",
             f"Transaction: {question.transaction.code} {question.transaction.name}" if question.transaction else ""]
    return (
        f"{intro}\n\n"
        f"Account: {question.account.code} - {question.account.name}\n"
        f"{question.code}{' (BLOCKING)' if question.blocking else ''}: {question.title}\n"
        + "".join(f"{line}\n" for line in about if line)
        + f"\nOpen it: {notify.app_url(url_for('doubts.question', code=question.account.code, question_id=question.id))}"
        + notify.footer()
    )


@bp.route("")
@login_required
def doubt_list(code):
    account = visible_account_or_404(code)
    show = request.args.get("show", "open")
    query = Question.query.filter_by(account_id=account.id)
    if show == "open":
        query = query.filter(Question.status != "RESOLVED")
    elif show == "blocking":
        query = query.filter(Question.status != "RESOLVED", Question.blocking.is_(True))
    elif show == "mine":
        query = query.filter((Question.asked_by_id == g.user.id) | (Question.assigned_to_id == g.user.id))
    elif show == "unassigned":
        query = query.filter(Question.status != "RESOLVED", Question.assigned_to_id.is_(None))
    questions = query.order_by(Question.blocking.desc(), Question.status, Question.updated_at.desc()).all()
    return render_template("doubts/list.html", account=account, questions=questions, show=show,
                           can_ask=g.user.can_contribute(account), labels=QUESTION_STATUSES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_question(code):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    projects = KTProject.query.filter_by(account_id=account.id).order_by(KTProject.position, KTProject.id).all()
    topics = KTTopic.query.filter_by(account_id=account.id).order_by(KTTopic.title).all()
    transactions = Transaction.query.filter_by(account_id=account.id, active=True).order_by(Transaction.code).all()
    form = request.form if request.method == "POST" else request.args
    data = {f: form.get(f, "").strip() for f in ("title", "detail", "project_id", "topic_id", "transaction_id",
                                                 "assigned_to_id")}
    data["title"] = " ".join(data["title"].split())
    data["blocking"] = form.get("blocking") == "on"
    errors = {}
    if request.method == "POST":
        if not data["title"] or len(data["title"]) > 200:
            errors["title"] = "Say in a line what you need to know (up to 200 characters)."
        if len(data["detail"]) > 8000:
            errors["detail"] = "At most 8,000 characters."
        picked = {}
        for field, model in (("project_id", KTProject), ("topic_id", KTTopic), ("transaction_id", Transaction)):
            row = db.session.get(model, int(data[field])) if data[field].isdigit() else None
            if data[field] and (row is None or row.account_id != account.id):
                errors[field] = "Choose one from this account."
            picked[field] = row
        answerer_ids = {u.id for u in _answerers(account)}
        if data["assigned_to_id"] and (not data["assigned_to_id"].isdigit()
                                       or int(data["assigned_to_id"]) not in answerer_ids):
            errors["assigned_to_id"] = "Choose an SME or KT lead of this account."
    if request.method == "GET" or errors:
        return render_template("doubts/form.html", account=account, form=data, errors=errors, projects=projects,
                               topics=topics, transactions=transactions, answerers=_answerers(account)), \
            (400 if errors else 200)
    topic, txn = picked["topic_id"], picked["transaction_id"]
    assignee = int(data["assigned_to_id"]) if data["assigned_to_id"] else \
        (topic.sme_id if topic and topic.sme_id else (txn.owner_id if txn and txn.owner_id else None))
    if assignee == g.user.id:
        assignee = None  # nobody answers their own doubt; the KT leads assign it
    project = picked["project_id"] or (topic.module.project if topic else (txn.project if txn else None))
    number = (db.session.query(func.max(Question.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    question = Question(account_id=account.id, number=number, title=data["title"], detail=data["detail"],
                        blocking=data["blocking"], asked_by_id=g.user.id, assigned_to_id=assignee,
                        project_id=project.id if project else None, topic_id=topic.id if topic else None,
                        transaction_id=txn.id if txn else None, created_at=utcnow(), updated_at=utcnow())
    db.session.add(question)
    db.session.commit()
    to = [question.assigned_to.email] if question.assigned_to else \
        [u.email for u in members_of(account) if u.role == "kt_lead"]
    notify.send(to, f"[{account.code}] {'BLOCKING doubt' if question.blocking else 'New doubt'}: {question.title}",
                _body(question, f"{g.user.full_name} raised a doubt"
                                + ("" if question.assigned_to else " (not assigned yet; please assign an SME)") + "."
                                + (f"\n\n{question.detail}" if question.detail else "")))
    flash(f"{question.code} raised" + (f" and assigned to {question.assigned_to.full_name}." if question.assigned_to
                                       else "; the KT leads will assign it."), "success")
    return redirect(url_for("doubts.question", code=account.code, question_id=question.id))


@bp.route("/<int:question_id>")
@login_required
def question(code, question_id):
    account = visible_account_or_404(code)
    question = _question_or_404(account, question_id)
    manager = g.user.can_manage(account)
    return render_template(
        "doubts/question.html", account=account, question=question, can_reply=g.user.can_contribute(account),
        can_resolve=question.asked_by_id == g.user.id or manager,
        can_assign=manager or question.assigned_to_id == g.user.id, answerers=_answerers(account),
        kb_conn=servicenow.connection_for(account), can_publish=servicenow.can_publish(g.user, account),
    )


@bp.route("/<int:question_id>/reply", methods=["POST"])
@login_required
def reply(code, question_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    question = _question_or_404(account, question_id)
    body = request.form.get("body", "").strip()
    if not body or len(body) > 8000:
        flash("Write your reply (up to 8,000 characters).", "error")
        return redirect(url_for("doubts.question", code=account.code, question_id=question.id))
    question.replies.append(QuestionReply(author_id=g.user.id, body=body, created_at=utcnow()))
    by_asker = question.asked_by_id == g.user.id
    question.status = "OPEN" if by_asker else "ANSWERED"
    question.resolved_at, question.updated_at = None, utcnow()
    db.session.commit()
    if by_asker:
        to = [question.assigned_to.email] if question.assigned_to else \
            [u.email for u in members_of(account) if u.role == "kt_lead"]
        notify.send(to, f"[{account.code}] Follow-up on {question.code}: {question.title}",
                    _body(question, f"{g.user.full_name} added:\n\n{body}"))
    else:
        notify.send([question.asked_by.email], f"[{account.code}] Answer to {question.code}: {question.title}",
                    _body(question, f"{g.user.full_name} answered:\n\n{body}\n\n"
                                    "If this unblocks you, mark the doubt resolved; otherwise reply."))
    flash("Reply posted.", "success")
    return redirect(url_for("doubts.question", code=account.code, question_id=question.id) + "#replies")


@bp.route("/<int:question_id>/resolve", methods=["POST"])
@login_required
def resolve(code, question_id):
    account = visible_account_or_404(code)
    question = _question_or_404(account, question_id)
    _require(question.asked_by_id == g.user.id or g.user.can_manage(account))
    if request.form.get("reopen") == "1":
        question.status, question.resolved_at = "OPEN", None
        flash("Doubt reopened.", "success")
    else:
        question.status, question.resolved_at = "RESOLVED", utcnow()
        flash("Doubt resolved.", "success")
    question.updated_at = utcnow()
    db.session.commit()
    return redirect(url_for("doubts.question", code=account.code, question_id=question.id))


@bp.route("/<int:question_id>/assign", methods=["POST"])
@login_required
def assign(code, question_id):
    account = visible_account_or_404(code)
    question = _question_or_404(account, question_id)
    _require(g.user.can_manage(account) or question.assigned_to_id == g.user.id)
    value = request.form.get("assigned_to_id", "")
    user = db.session.get(User, int(value)) if value.isdigit() else None
    if user is None or user not in _answerers(account):
        flash("Choose an SME or KT lead of this account.", "error")
    else:
        question.assigned_to_id, question.updated_at = user.id, utcnow()
        db.session.commit()
        notify.send([user.email], f"[{account.code}] Doubt assigned to you: {question.title}",
                    _body(question, f"{g.user.full_name} asked you to answer this doubt."))
        flash(f"Assigned to {user.full_name}.", "success")
    return redirect(url_for("doubts.question", code=account.code, question_id=question.id))
