"""KT documents: each client account is a workspace of folders and rich documents, started from templates, with
version history, comments on selected text and @mentions.

Who may do what (checked on the server): members read; everyone except viewers writes and comments; KT leads,
admins and a document's author delete it. Document HTML comes from the browser, so it is reduced to a short list
of harmless tags before it is stored, and again before it is shown. Two people saving at once is refused rather
than overwritten: this is not live co-editing.
"""
import re
from html import escape
from html.parser import HTMLParser
from io import BytesIO

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from markupsafe import Markup
from sqlalchemy import or_

import notify
import transcripts
from auth import login_required
from kt import members_of, visible_account_or_404
from models import DocFolder, DocumentComment, DocumentVersion, KTDocument, db
from timeutil import utcnow

bp = Blueprint("documents", __name__, url_prefix="/accounts/<code>/documents")

MAX_BODY = 500_000
WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TAGS = frozenset("p br h1 h2 h3 ul ol li strong b em i u s blockquote pre code a hr table thead tbody tr th td".split())
_VOID = frozenset(("br", "hr"))
_DROP_CONTENT = frozenset(("script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "title"))
_BLOCKS = frozenset("p h1 h2 h3 li blockquote pre tr hr br".split())
_SAFE_LINK = re.compile(r"^(https://|mailto:)[^\s\"'<>]*$", re.I)


TEMPLATES = {
    "blank": ("Blank document", ""),
    "kt_plan": ("KT plan", "<h2>Objective</h2><p></p><h2>Topics and owners</h2><table><tbody><tr><th>Topic</th><th>SME</th><th>Receivers</th><th>Date</th></tr><tr><td></td><td></td><td></td><td></td></tr></tbody></table><h2>Risks and dependencies</h2><ul><li></li></ul>"),
    "meeting": ("KT meeting notes", "<p><strong>Date:</strong> </p><p><strong>Attendees:</strong> </p><p><strong>Recording:</strong> </p><h2>Summary</h2><p></p><h2>Decisions</h2><ul><li></li></ul><h2>Actions and owners</h2><ul><li></li></ul><h2>Open questions</h2><ul><li></li></ul>"),
    "runbook": ("Runbook", "<h2>Service</h2><p></p><h2>Routine checks</h2><ol><li></li></ol><h2>Incident: symptoms and recovery</h2><ol><li></li></ol><h2>Escalation</h2><table><tbody><tr><th>Level</th><th>Contact</th><th>When</th></tr><tr><td></td><td></td><td></td></tr></tbody></table>"),
    "sop": ("SOP", "<h2>Purpose</h2><p></p><h2>Scope</h2><p></p><h2>Steps</h2><ol><li></li></ol><h2>Controls and evidence</h2><ul><li></li></ul>"),
    "raci": ("RACI matrix", "<table><tbody><tr><th>Activity</th><th>Responsible</th><th>Accountable</th><th>Consulted</th><th>Informed</th></tr><tr><td></td><td></td><td></td><td></td><td></td></tr></tbody></table>"),
    "handover": ("Hand-over checklist", "<ul><li>Access granted</li><li>Recordings saved</li><li>Documents, runbooks and SOPs reviewed</li><li>RACI agreed</li><li>Client sign-off</li><li>Shadow complete</li><li>Reverse shadow complete</li></ul>"),
    "application": ("Application KT", "<h2>Purpose and users</h2><p></p><h2>Key business flows</h2><ol><li></li></ol><h2>Technology stack</h2><ul><li></li></ul><h2>Repositories and documentation</h2><ul><li></li></ul><h2>Known issues</h2><ul><li></li></ul>"),
    "architecture": ("Architecture and environments", "<h2>Overview</h2><p></p><h2>Environments</h2><table><tbody><tr><th>Environment</th><th>URL or host</th><th>Owner</th><th>Notes</th></tr><tr><td>DEV</td><td></td><td></td><td></td></tr><tr><td>QA</td><td></td><td></td><td></td></tr><tr><td>PROD</td><td></td><td></td><td></td></tr></tbody></table><h2>Integrations</h2><table><tbody><tr><th>System</th><th>Direction</th><th>Method</th><th>Contact</th></tr><tr><td></td><td></td><td></td><td></td></tr></tbody></table>"),
    "release": ("Deployment and release", "<h2>Release calendar</h2><p></p><h2>Deployment steps</h2><ol><li></li></ol><h2>Verification</h2><ol><li></li></ol><h2>Rollback</h2><ol><li></li></ol><h2>Approvals</h2><ul><li></li></ul>"),
    "incident": ("Incident and on-call", "<h2>Severity and SLAs</h2><table><tbody><tr><th>Severity</th><th>Definition</th><th>Response</th><th>Resolution</th></tr><tr><td>P1</td><td></td><td></td><td></td></tr><tr><td>P2</td><td></td><td></td><td></td></tr></tbody></table><h2>Monitoring and alerts</h2><ul><li></li></ul><h2>Common incidents and fixes</h2><ol><li></li></ol><h2>On-call and escalation</h2><table><tbody><tr><th>Level</th><th>Team or person</th><th>Contact</th></tr><tr><td></td><td></td><td></td></tr></tbody></table>"),
    "access": ("Access matrix", "<table><tbody><tr><th>System</th><th>Access level</th><th>Who needs it</th><th>How to request</th><th>Approver</th></tr><tr><td></td><td></td><td></td><td></td><td></td></tr></tbody></table>"),
    "article": ("Knowledge article", "<h2>Problem</h2><p></p><h2>Cause</h2><p></p><h2>Solution</h2><ol><li></li></ol>"),
}


# --- safe HTML -----------------------------------------------------------------------------------
class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self.skip += 1
        elif not self.skip and tag in _TAGS:
            extra = ""
            if tag == "a":
                attrs = dict(attrs)
                href = (attrs.get("href") or "").strip()
                if not _SAFE_LINK.match(href):
                    return  # the link goes; its text stays
                embed = ' class="doc-embed"' if "doc-embed" in (attrs.get("class") or "").split() else ""
                extra = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"{embed}'
            self.out.append(f"<{tag}{extra}>")
            if tag not in _VOID:
                self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _DROP_CONTENT:
            self.skip -= 1

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.open:
            while self.open:  # closes anything left open inside it, so tags always balance
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))


TEMPLATE_HINTS = {
    "blank": "Start from an empty page.",
    "kt_plan": "Objective, topics, owners and risks.",
    "meeting": "Attendees, decisions, actions and questions.",
    "runbook": "Day-to-day operating steps.",
    "sop": "A standard procedure, step by step.",
    "raci": "Who is responsible, accountable, consulted, informed.",
    "handover": "What must be done before hand-over.",
    "application": "What the application does and how it is supported.",
    "architecture": "Components, environments and integrations.",
    "release": "How changes are built, deployed and rolled back.",
    "incident": "Monitoring, severity, escalation and on-call.",
    "access": "Who needs which access, and how to get it.",
    "article": "A short answer the team can reuse.",
}


def sanitize(body):
    parser = _Sanitizer()
    parser.feed(body or "")
    parser.close()
    return "".join(parser.out + [f"</{t}>" for t in reversed(parser.open)])


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_endtag(self, tag):
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(body):
    parser = _Text()
    parser.feed(sanitize(body))
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


# --- helpers -------------------------------------------------------------------------------------
def _require(allowed):
    if not allowed:
        abort(403)


def _doc_or_404(account, doc_id):
    doc = db.session.get(KTDocument, doc_id)
    if doc is None or doc.account_id != account.id:
        abort(404)
    return doc


def _folder_or_none(account, value):
    """The account's folder with this id, or None for an empty or foreign value."""
    folder = db.session.get(DocFolder, int(value)) if str(value or "").isdigit() else None
    return folder if folder is not None and folder.account_id == account.id else None


def _back(doc):
    return redirect(url_for(".view", code=doc.account.code, doc_id=doc.id))


def _read_form():
    return " ".join(request.form.get("title", "").split()), sanitize(request.form.get("body", ""))


def _form_errors(title, raw_body):
    if not title or len(title) > 200:
        return "Give the document a title (up to 200 characters)."
    if len(raw_body) > MAX_BODY:
        return "This document is too long to save. Split it into two documents."
    return None


def _add_version(doc):
    number = (doc.versions[-1].number + 1) if doc.versions else 1
    doc.versions.append(DocumentVersion(number=number, title=doc.title, body=doc.body, saved_by_id=g.user.id,
                                        saved_at=utcnow()))


def _folders(account):
    return DocFolder.query.filter_by(account_id=account.id).order_by(DocFolder.name).all()


# --- workspace -----------------------------------------------------------------------------------
@bp.route("")
@login_required
def workspace(code):
    account = visible_account_or_404(code)
    query = KTDocument.query.filter_by(account_id=account.id)
    folder = _folder_or_none(account, request.args.get("folder"))
    if folder:
        query = query.filter_by(folder_id=folder.id)
    q = " ".join(request.args.get("q", "").split())[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(KTDocument.title.ilike(like, escape="\\"), KTDocument.body.ilike(like, escape="\\")))
    return render_template("documents/workspace.html", account=account, folders=_folders(account), folder=folder, q=q,
                           documents=query.order_by(KTDocument.updated_at.desc()).all(), templates=TEMPLATES,
                           can_write=g.user.can_contribute(account), can_manage=g.user.can_manage(account))


@bp.route("/folders", methods=["POST"])
@login_required
def add_folder(code):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    name = " ".join(request.form.get("name", "").split())
    if not name or len(name) > 120:
        flash("Name the folder (up to 120 characters).", "error")
    elif any(f.name.lower() == name.lower() for f in _folders(account)):
        flash("There is already a folder with this name.", "error")
    else:
        db.session.add(DocFolder(account_id=account.id, name=name, created_by_id=g.user.id, created_at=utcnow()))
        db.session.commit()
    return redirect(url_for(".workspace", code=code))


@bp.route("/folders/<int:folder_id>/delete", methods=["POST"])
@login_required
def delete_folder(code, folder_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    folder = _folder_or_none(account, folder_id)
    if folder is None:
        abort(404)
    KTDocument.query.filter_by(folder_id=folder.id).update({"folder_id": None})
    db.session.delete(folder)
    db.session.commit()
    flash("Folder removed. Its documents are kept, outside any folder.", "success")
    return redirect(url_for(".workspace", code=code))


# --- documents -----------------------------------------------------------------------------------
@bp.route("/new", methods=["GET", "POST"])
@login_required
def new(code):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    if request.method == "GET":
        name, body = TEMPLATES.get(request.args.get("template"), TEMPLATES["blank"])
        return render_template("documents/edit.html", account=account, doc=None, title="" if not body else name, body=body,
                               folders=_folders(account), folder_id=request.args.get("folder", ""), error=None)
    title, body = _read_form()
    error = _form_errors(title, request.form.get("body", ""))
    if error:
        return render_template("documents/edit.html", account=account, doc=None, title=title, body=body, error=error,
                               folders=_folders(account), folder_id=request.form.get("folder_id", "")), 400
    folder = _folder_or_none(account, request.form.get("folder_id"))
    doc = KTDocument(account_id=account.id, folder_id=folder.id if folder else None, title=title, body=body,
                     created_by_id=g.user.id, updated_by_id=g.user.id, created_at=utcnow(), updated_at=utcnow())
    db.session.add(doc)
    _add_version(doc)
    db.session.commit()
    return _back(doc)


@bp.route("/<int:doc_id>")
@login_required
def view(code, doc_id):
    account = visible_account_or_404(code)
    doc = _doc_or_404(account, doc_id)
    return render_template("documents/view.html", account=account, doc=doc, body=Markup(sanitize(doc.body)),
                           can_write=g.user.can_contribute(account),
                           can_delete=g.user.can_manage(account) or (doc.created_by_id == g.user.id and g.user.can_contribute(account)),
                           people=[u for u in members_of(account)])


@bp.route("/<int:doc_id>/edit", methods=["GET", "POST"])
@login_required
def edit(code, doc_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    doc = _doc_or_404(account, doc_id)
    if request.method == "GET":
        return render_template("documents/edit.html", account=account, doc=doc, title=doc.title, body=sanitize(doc.body),
                               folders=_folders(account), folder_id=doc.folder_id or "", base=doc.versions[-1].number, error=None)
    title, body = _read_form()
    base = doc.versions[-1].number
    error = _form_errors(title, request.form.get("body", ""))
    if not error and request.form.get("base") != str(base):
        error = f"{doc.updated_by.full_name} saved this document while you were editing. Copy your changes, reload, and apply them again."
    if error:
        return render_template("documents/edit.html", account=account, doc=doc, title=title, body=body, error=error, base=base,
                               folders=_folders(account), folder_id=request.form.get("folder_id", "")), 400
    folder = _folder_or_none(account, request.form.get("folder_id"))
    doc.folder_id = folder.id if folder else None
    if (title, body) != (doc.title, doc.body):
        doc.title, doc.body = title, body
        doc.updated_by_id, doc.updated_at = g.user.id, utcnow()
        _add_version(doc)
    db.session.commit()
    return _back(doc)


@bp.route("/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete(code, doc_id):
    account = visible_account_or_404(code)
    doc = _doc_or_404(account, doc_id)
    _require(g.user.can_manage(account) or (doc.created_by_id == g.user.id and g.user.can_contribute(account)))
    db.session.delete(doc)
    db.session.commit()
    flash("Document deleted.", "success")
    return redirect(url_for(".workspace", code=code))


@bp.route("/<int:doc_id>/document.docx")
@login_required
def export(code, doc_id):
    account = visible_account_or_404(code)
    doc = _doc_or_404(account, doc_id)
    data = transcripts.word_document(doc.title, f"{account.name} · KITE", plain_text(doc.body))
    name = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', " ", doc.title)[:100].strip(" .") or "document"
    return send_file(BytesIO(data), mimetype=WORD_MIME, as_attachment=True, download_name=f"{name}.docx")


# --- history -------------------------------------------------------------------------------------
def _version_or_404(doc, number):
    version = next((v for v in doc.versions if v.number == number), None)
    if version is None:
        abort(404)
    return version


@bp.route("/<int:doc_id>/history")
@login_required
def history(code, doc_id):
    account = visible_account_or_404(code)
    doc = _doc_or_404(account, doc_id)
    number = request.args.get("version", "")
    shown = _version_or_404(doc, int(number)) if number.isdigit() else doc.versions[-1]
    return render_template("documents/history.html", account=account, doc=doc, shown=shown,
                           body=Markup(sanitize(shown.body)), can_write=g.user.can_contribute(account))


@bp.route("/<int:doc_id>/history/<int:number>/restore", methods=["POST"])
@login_required
def restore(code, doc_id, number):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    doc = _doc_or_404(account, doc_id)
    version = _version_or_404(doc, number)
    doc.title, doc.body = version.title, version.body
    doc.updated_by_id, doc.updated_at = g.user.id, utcnow()
    _add_version(doc)  # the current text stays in the history
    db.session.commit()
    flash(f"Version {number} restored.", "success")
    return _back(doc)


# --- comments ------------------------------------------------------------------------------------
def mentioned_users(account, text):
    """Members of the account named as @username; nobody outside the account is ever notified."""
    names = {name.lower() for name in re.findall(r"@([A-Za-z0-9._-]+)", text)}
    return [u for u in members_of(account) if u.username.lower() in names]


@bp.route("/<int:doc_id>/comments", methods=["POST"])
@login_required
def comment(code, doc_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    doc = _doc_or_404(account, doc_id)
    body = request.form.get("body", "").strip()
    if not body or len(body) > 4000:
        flash("Write the comment (up to 4,000 characters).", "error")
        return _back(doc)
    quote = " ".join(request.form.get("quote", "").split())[:500]
    db.session.add(DocumentComment(document=doc, author_id=g.user.id, quote=quote, body=body, created_at=utcnow()))
    db.session.commit()
    people = [u.email for u in mentioned_users(account, body) if u.id != g.user.id]
    if people:
        link = notify.app_url(url_for(".view", code=code, doc_id=doc.id))
        notify.send(people, f"[{account.code}] {g.user.full_name} mentioned you in {doc.title}",
                    f"{body}\n\nOpen the document: {link}" + notify.footer())
    return redirect(url_for(".view", code=code, doc_id=doc.id) + "#comments")


@bp.route("/<int:doc_id>/comments/<int:comment_id>/resolve", methods=["POST"])
@login_required
def resolve_comment(code, doc_id, comment_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    doc = _doc_or_404(account, doc_id)
    item = next((c for c in doc.comments if c.id == comment_id), None)
    if item is None:
        abort(404)
    item.resolved_at, item.resolved_by_id = (None, None) if item.resolved_at else (utcnow(), g.user.id)
    db.session.commit()
    return redirect(url_for(".view", code=code, doc_id=doc.id) + "#comments")
