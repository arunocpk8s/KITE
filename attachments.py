"""Files on a KT plan or topic: Word, PDF, Visio and pictures such as workflow diagrams, and KT topics read from a
Word document.

Files live in the database, because a hosted copy has no lasting disk, so each is limited in size. What people upload
is not trusted: the type must match the file's first bytes, and only pictures open in the browser; anything else
downloads. Who may do what (checked on the server): members view; everyone except viewers uploads; KT leads, admins
and the uploader remove a file; KT leads and admins create topics from a Word document, which they check first.
"""
from io import BytesIO

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

import transcripts
from auth import login_required
from kt import find_or_create_module, new_topic_position, visible_account_or_404
from models import MODULE_KINDS, KTAttachment, KTProject, KTTopic, add_kt_event, db
from timeutil import utcnow

bp = Blueprint("attachments", __name__, url_prefix="/accounts/<code>/kt/files")

MAX_FILE = 4 * 1024 * 1024  # a hosted request cannot carry much more than this
MAX_ROWS = 200
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# extension -> (type, first bytes)
TYPES = {
    "jpg": ("image/jpeg", b"\xff\xd8\xff"), "jpeg": ("image/jpeg", b"\xff\xd8\xff"), "png": ("image/png", b"\x89PNG"),
    "pdf": ("application/pdf", b"%PDF"), "docx": (WORD, b"PK"), "doc": ("application/msword", b"\xd0\xcf\x11\xe0"),
    "vsdx": ("application/vnd.ms-visio.drawing", b"PK"),
}


def _require(allowed):
    if not allowed:
        abort(403)


def _project_or_404(account, value):
    project = db.session.get(KTProject, int(value)) if str(value).isdigit() else None
    if project is None or project.account_id != account.id:
        abort(404)
    return project


def _file_or_404(account, file_id):
    row = db.session.get(KTAttachment, file_id)
    if row is None or row.account_id != account.id:
        abort(404)
    return row


def _clean_name(filename):
    return filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:200]


def _read_upload(upload):
    """(name, type, bytes) of an upload, or (None, message, None) when it cannot be accepted."""
    if upload is None or not upload.filename:
        return None, "Choose a file.", None
    name = _clean_name(upload.filename)
    known = TYPES.get(name.rsplit(".", 1)[-1].lower() if "." in name else "")
    if known is None:
        return None, "Upload a Word, PDF, Visio, JPG or PNG file.", None
    data = upload.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        return None, "That file is larger than 4 MB. Keep it in SharePoint and add the link as a material.", None
    if not data.startswith(known[1]):
        return None, f"'{name}' is not really a file of that type.", None
    return name, known[0], data


def _save(account, project, topic, name, content_type, data):
    row = KTAttachment(account_id=account.id, project_id=project.id, topic_id=topic.id if topic else None, filename=name,
                       content_type=content_type, size=len(data), data=data, uploaded_by_id=g.user.id,
                       created_at=utcnow())
    db.session.add(row)
    return row


def _back(account, project, topic=None):
    if topic is not None:
        return redirect(url_for("kt.topic", code=account.code, topic_id=topic.id))
    return redirect(url_for("kt.plan", code=account.code) + f"#project-{project.id}")


@bp.route("", methods=["POST"])
@login_required
def upload(code):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    project = _project_or_404(account, request.form.get("project_id"))
    topic = None
    if request.form.get("topic_id"):
        topic = db.session.get(KTTopic, int(request.form["topic_id"])) if request.form["topic_id"].isdigit() else None
        if topic is None or topic.module.project_id != project.id:
            abort(404)
    name, content_type, data = _read_upload(request.files.get("file"))
    if name is None:
        flash(content_type, "error")
    else:
        _save(account, project, topic, name, content_type, data)
        db.session.commit()
        flash(f"'{name}' added.", "success")
    return _back(account, project, topic)


@bp.route("/<int:file_id>")
@login_required
def view(code, file_id):
    account = visible_account_or_404(code)
    row = _file_or_404(account, file_id)
    response = send_file(BytesIO(row.data), mimetype=row.content_type, as_attachment=not row.is_image,
                         download_name=row.filename)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.route("/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(code, file_id):
    account = visible_account_or_404(code)
    row = _file_or_404(account, file_id)
    _require(g.user.can_manage(account) or (row.uploaded_by_id == g.user.id and g.user.can_contribute(account)))
    project, topic = row.project, row.topic
    db.session.delete(row)
    db.session.commit()
    flash(f"'{row.filename}' removed.", "success")
    return _back(account, project, topic)


# --- KT topics from a Word document ----------------------------------------------------------------
@bp.route("/import", methods=["POST"])
@login_required
def import_word(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, request.form.get("project_id"))
    name, content_type, data = _read_upload(request.files.get("file"))
    if name is None or not name.lower().endswith(".docx"):
        flash(content_type if name is None else "Topics can only be read from a .docx file. Save it as .docx first.", "error")
        return _back(account, project)
    try:
        rows = transcripts.read_kt_outline(data)[:MAX_ROWS]
    except transcripts.TranscriptError as err:
        flash(str(err), "error")
        return _back(account, project)
    if not rows:
        flash(f"No KT topics were found in '{name}'.", "info")
        return _back(account, project)
    _save(account, project, None, name, content_type, data)
    db.session.commit()
    return render_template("kt/import_review.html", account=account, project=project, rows=rows, filename=name,
                           kinds=MODULE_KINDS)


@bp.route("/import/create", methods=["POST"])
@login_required
def import_create(code):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, request.form.get("project_id"))
    if project.client_signed:
        flash("The client has signed off this project, so its KT plan is locked.", "error")
        return _back(account, project)
    created = 0
    for i in range(min(request.form.get("rows", type=int, default=0), MAX_ROWS)):
        title = " ".join(request.form.get(f"title-{i}", "").split())[:200]
        area = " ".join(request.form.get(f"area-{i}", "").split())[:200]
        kind = request.form.get(f"kind-{i}", "")
        if request.form.get(f"use-{i}") != "1" or not title or not area or kind not in MODULE_KINDS:
            continue
        module = find_or_create_module(account, project, kind, area)
        topic = KTTopic(account_id=account.id, module=module, title=title, status="PLANNED",
                        position=new_topic_position(module), created_at=utcnow(), updated_at=utcnow())
        db.session.add(topic)
        add_kt_event(topic, g.user, "CREATED", new_value="PLANNED")
        created += 1
    db.session.commit()
    flash(f"{created} KT topic{'' if created == 1 else 's'} added." if created else "Nothing was selected.",
          "success" if created else "info")
    return _back(account, project)
