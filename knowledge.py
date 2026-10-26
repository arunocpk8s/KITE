"""Glossary / FAQ per account, and one search across an account's KT topics, SOPs, transactions,
glossary, doubts and KT documents. SMEs and KT leads edit the glossary; every member reads and searches."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_

import servicenow
from auth import login_required
from kt import visible_account_or_404
from models import (
    GLOSSARY_KINDS, GlossaryEntry, KTProject, KTTopic, ProjectDocument, Question, Sop, SopVersion, Transaction, db,
)
from sops import can_author
from timeutil import utcnow

bp = Blueprint("knowledge", __name__, url_prefix="/accounts/<code>")

MAX_RESULTS = 25


@bp.route("/glossary")
@login_required
def glossary(code):
    account = visible_account_or_404(code)
    q = " ".join(request.args.get("q", "").split())[:100]
    kind = request.args.get("kind", "")
    query = GlossaryEntry.query.filter_by(account_id=account.id)
    if q:
        query = query.filter(or_(GlossaryEntry.term.ilike(f"%{q}%"), GlossaryEntry.meaning.ilike(f"%{q}%")))
    if kind in GLOSSARY_KINDS:
        query = query.filter_by(kind=kind)
    return render_template("knowledge/glossary.html", account=account, entries=query.order_by(GlossaryEntry.term).all(),
                           kinds=GLOSSARY_KINDS, q=q, kind=kind, can_edit=can_author(g.user, account))


def _read_entry(form):
    return form.get("kind", "TERM"), " ".join(form.get("term", "").split()), form.get("meaning", "").strip()


def _entry_error(kind, term, meaning):
    if kind not in GLOSSARY_KINDS:
        return "Choose term, system, contact or FAQ."
    if not term or len(term) > 200:
        return "Enter the term or question (up to 200 characters)."
    if not meaning or len(meaning) > 4000:
        return "Enter the meaning or answer (up to 4,000 characters)."
    return None


@bp.route("/glossary", methods=["POST"])
@login_required
def add_entry(code):
    account = visible_account_or_404(code)
    if not can_author(g.user, account):
        abort(403)
    kind, term, meaning = _read_entry(request.form)
    error = _entry_error(kind, term, meaning)
    if error:
        flash(error, "error")
    else:
        db.session.add(GlossaryEntry(account_id=account.id, kind=kind, term=term, meaning=meaning,
                                     updated_at=utcnow(), updated_by_id=g.user.id))
        db.session.commit()
        flash(f"Added '{term}'.", "success")
    return redirect(url_for("knowledge.glossary", code=account.code))


def _entry_or_404(account, entry_id):
    entry = db.session.get(GlossaryEntry, entry_id)
    if entry is None or entry.account_id != account.id:
        abort(404)
    return entry


@bp.route("/glossary/<int:entry_id>", methods=["POST"])
@login_required
def edit_entry(code, entry_id):
    account = visible_account_or_404(code)
    if not can_author(g.user, account):
        abort(403)
    entry = _entry_or_404(account, entry_id)
    if request.form.get("delete") == "1":
        db.session.delete(entry)
        db.session.commit()
        flash(f"Removed '{entry.term}'.", "success")
        return redirect(url_for("knowledge.glossary", code=account.code))
    kind, term, meaning = _read_entry(request.form)
    error = _entry_error(kind, term, meaning)
    if error:
        flash(error, "error")
    else:
        entry.kind, entry.term, entry.meaning, entry.updated_at, entry.updated_by_id = \
            kind, term, meaning, utcnow(), g.user.id
        db.session.commit()
        flash("Saved.", "success")
    return redirect(url_for("knowledge.glossary", code=account.code) + f"#entry-{entry.id}")


@bp.route("/search")
@login_required
def search(code):
    account = visible_account_or_404(code)
    q = " ".join(request.args.get("q", "").split())[:100]
    groups = []
    if len(q) >= 2:
        like = f"%{q}%"
        a = account.id
        topics = KTTopic.query.filter(KTTopic.account_id == a,
                                      or_(KTTopic.title.ilike(like), KTTopic.description.ilike(like))).limit(MAX_RESULTS)
        sop_ids = {v.sop_id for v in SopVersion.query.join(Sop).filter(
            Sop.account_id == a, or_(SopVersion.purpose.ilike(like), SopVersion.steps.ilike(like))).all()}
        sops = Sop.query.filter(Sop.account_id == a, or_(Sop.title.ilike(like), Sop.id.in_(sop_ids))).limit(MAX_RESULTS)
        txns = Transaction.query.filter(Transaction.account_id == a, or_(
            Transaction.code.ilike(like), Transaction.name.ilike(like), Transaction.purpose.ilike(like),
            Transaction.steps.ilike(like), Transaction.system.ilike(like))).limit(MAX_RESULTS)
        terms = GlossaryEntry.query.filter(GlossaryEntry.account_id == a, or_(
            GlossaryEntry.term.ilike(like), GlossaryEntry.meaning.ilike(like))).limit(MAX_RESULTS)
        doubts = Question.query.filter(Question.account_id == a, or_(
            Question.title.ilike(like), Question.detail.ilike(like))).limit(MAX_RESULTS)
        docs = ProjectDocument.query.join(KTProject).filter(KTProject.account_id == a,
                                                             ProjectDocument.title.ilike(like)).limit(MAX_RESULTS)
        groups = [
            ("KT topics", [(t.title, url_for("kt.topic", code=code, topic_id=t.id), t.status_label) for t in topics]),
            ("SOPs", [(f"{s.code} {s.title}", url_for("sops.sop", code=code, sop_id=s.id), s.status_label) for s in sops]),
            ("Transactions", [(f"{t.code} {t.name}", url_for("transactions.transaction", code=code, txn_id=t.id),
                               t.system) for t in txns]),
            ("Glossary", [(e.term, url_for("knowledge.glossary", code=code) + f"#entry-{e.id}", e.meaning[:120])
                          for e in terms]),
            ("Doubts", [(f"{d.code} {d.title}", url_for("doubts.question", code=code, question_id=d.id),
                         d.status_label) for d in doubts]),
            ("KT documents", [(d.title, d.url, d.doc_type) for d in docs]),
        ]
    return render_template("knowledge/search.html", account=account, q=q, groups=groups,
                           found=sum(len(items) for _, items in groups),
                           kb_conn=servicenow.connection_for(account) if len(q) >= 2 else None)
