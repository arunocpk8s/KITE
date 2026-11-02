"""Meeting transcripts: upload one, review what KITE proposes from it, and create the selected items.

Each item goes to its proper home: scope of work and RACI (sops.py pages), draft SOPs, doubts, draft KB articles
in the account's ServiceNow, documents in the project's SharePoint KT folder, and sign-in with account access for
named people. Everything is a draft or a line that can be edited afterwards, and nothing is created until a person
has reviewed it.

Who may do what (checked on the server): KT leads and admins upload, create and remove; the rest of the team
(not viewers) can read. Creating sign-ins is for admins only, as in the admin console.
"""
import base64
import re
from io import BytesIO

import requests
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from markupsafe import escape
from sqlalchemy import func

import servicenow
import sso
import transcripts
from auth import login_required
from kt import URL_RE, visible_account_or_404
from models import (
    DEFAULT_ROLE, SCOPE_KINDS, AccountMember, KTProject, MeetingTranscript, ProjectDocument, Question, ScopeItem, Sop,
    SopVersion, TranscriptItem, User, add_admin_event, add_project_event, add_sop_event, db,
)
from timeutil import format_date, parse_date, utcnow

bp = Blueprint("meetings", __name__, url_prefix="/accounts/<code>/projects/<int:project_id>/meetings")

MAX_TRANSCRIPT = 2_000_000
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
DOCUMENT_TYPES = {"SOP": "SOP", "RUNBOOK": "Runbook", "NOTES": "Process document"}
WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class CreateError(Exception):
    """An item could not be created; the message is safe to show, and it can be tried again."""


# --- access helpers ------------------------------------------------------------------------------
def _project_or_404(account, project_id):
    project = db.session.get(KTProject, project_id)
    if project is None or project.account_id != account.id:
        abort(404)
    return project


def _transcript_or_404(project, transcript_id):
    transcript = db.session.get(MeetingTranscript, transcript_id)
    if transcript is None or transcript.project_id != project.id:
        abort(404)
    return transcript


def _require(allowed):
    if not allowed:
        abort(403)


def ai_available(account):
    return bool(account.ai_transcripts and current_app.config.get("ANTHROPIC_API_KEY"))


def _meeting_label(transcript):
    date = format_date(transcript.meeting_date)
    return f"{transcript.title} ({date})" if date else transcript.title


# --- upload --------------------------------------------------------------------------------------
@bp.route("/new", methods=["GET", "POST"])
@login_required
def new(code, project_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    form = {"title": "", "meeting_date": "", "recording_link": "", "pasted": "", "method": "RULES"}
    errors = {}
    if request.method == "POST":
        form.update({k: request.form.get(k, "").strip() for k in form})
        form["method"] = form["method"] or "RULES"
        text, method = "", form["method"]
        upload = request.files.get("file")
        if not form["title"] or len(form["title"]) > 200:
            errors["title"] = "Name the meeting (up to 200 characters)."
        meeting_date = parse_date(form["meeting_date"])
        if form["meeting_date"] and meeting_date is None:
            errors["meeting_date"] = "Use a valid date."
        if form["recording_link"] and (not URL_RE.match(form["recording_link"]) or len(form["recording_link"]) > 500):
            errors["recording_link"] = "The recording link must start with https://."
        if method not in ("RULES", "AI") or (method == "AI" and not ai_available(account)):
            errors["method"] = "Claude AI is not switched on for this account."
        try:
            if upload and upload.filename:
                data = upload.read(MAX_TRANSCRIPT + 1)
                if len(data) > MAX_TRANSCRIPT:
                    raise transcripts.TranscriptError("The transcript is too large (2 MB at most). Split it first.")
                text = transcripts.read_file(upload.filename, data)
            else:
                text = form["pasted"]
        except transcripts.TranscriptError as err:
            errors["file"] = str(err)
        if not errors and not text:
            errors["file"] = "Upload the transcript or paste its text."
        if not errors:
            try:
                items = (transcripts.extract_with_ai(text, current_app.config["ANTHROPIC_API_KEY"]) if method == "AI"
                         else transcripts.extract_with_rules(text))
            except transcripts.TranscriptError as err:
                errors["file"] = str(err)
        if not errors:
            transcript = MeetingTranscript(project=project, title=form["title"], meeting_date=meeting_date,
                                           recording_link=form["recording_link"], text=text, method=method,
                                           uploaded_by_id=g.user.id, created_at=utcnow())
            transcript.items = [TranscriptItem(**item) for item in items]
            db.session.add(transcript)
            add_project_event(project, g.user, "TRANSCRIPT_ADDED", f"{form['title']}: {len(items)} items proposed")
            db.session.commit()
            flash(f"{len(items)} items proposed from the transcript. Check them, then create what you want.", "success")
            return redirect(url_for(".review", code=code, project_id=project.id, transcript_id=transcript.id))
    return render_template("meetings/new.html", account=account, project=project, form=form, errors=errors,
                           ai_available=ai_available(account), ai_consented=account.ai_transcripts), 400 if errors else 200


# --- review --------------------------------------------------------------------------------------
@bp.route("/<int:transcript_id>")
@login_required
def review(code, project_id, transcript_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    project = _project_or_404(account, project_id)
    transcript = _transcript_or_404(project, transcript_id)
    return render_template(
        "meetings/review.html", account=account, project=project, transcript=transcript,
        can_manage=g.user.can_manage(account), snow=servicenow.connection_for(account), document_kinds=DOCUMENT_TYPES,
        sharepoint_ready=_sharepoint_on() and bool(project.sharepoint_url), sharepoint_on=_sharepoint_on(),
    )


@bp.route("/<int:transcript_id>/items/<int:item_id>/document.docx")
@login_required
def document(code, project_id, transcript_id, item_id):
    account = visible_account_or_404(code)
    _require(g.user.can_contribute(account))
    transcript = _transcript_or_404(_project_or_404(account, project_id), transcript_id)
    item = next((i for i in transcript.items if i.id == item_id and i.kind in DOCUMENT_TYPES), None)
    if item is None:
        abort(404)
    return send_file(BytesIO(_word(item)), mimetype=WORD_MIME, as_attachment=True, download_name=_filename(item))


@bp.route("/<int:transcript_id>/delete", methods=["POST"])
@login_required
def delete(code, project_id, transcript_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    transcript = _transcript_or_404(project, transcript_id)
    add_project_event(project, g.user, "TRANSCRIPT_REMOVED", transcript.title)
    db.session.delete(transcript)
    db.session.commit()
    flash("Transcript removed from KITE. Items already created stay where they are.", "success")
    return redirect(url_for("projects.project", code=code, project_id=project.id) + "#meetings")


@bp.route("/<int:transcript_id>/create", methods=["POST"])
@login_required
def create(code, project_id, transcript_id):
    account = visible_account_or_404(code)
    _require(g.user.can_manage(account))
    project = _project_or_404(account, project_id)
    transcript = _transcript_or_404(project, transcript_id)
    created, failed = 0, 0
    for item in transcript.items:
        if item.created_at:
            continue
        title = " ".join(request.form.get(f"title-{item.id}", item.title).split())[:300]
        if title:
            item.title = title
        item.detail = request.form.get(f"detail-{item.id}", item.detail).strip()[:20000]
        if request.form.get(f"item-{item.id}") != "1":
            continue
        try:
            item.result_label, item.result_url = CREATORS[item.kind](account, project, transcript, item)
        except (CreateError, servicenow.ServiceNowError) as err:
            db.session.rollback()
            item = db.session.get(TranscriptItem, item.id)
            item.error = str(err)[:500]
            failed += 1
        else:
            item.created_at, item.created_by_id, item.error = utcnow(), g.user.id, ""
            created += 1
        # One by one: what reached ServiceNow or SharePoint must stay recorded if a later item fails.
        db.session.commit()
    if created:
        _register_recording(project, transcript)
        add_project_event(project, g.user, "TRANSCRIPT_APPLIED", f"{transcript.title}: {created} items created")
    db.session.commit()
    if created:
        flash(f"Created {created} items from the transcript.", "success")
    if failed:
        flash(f"{failed} items could not be created; see the reason beside each. You can try them again.", "error")
    if not created and not failed:
        flash("Nothing was selected.", "info")
    return redirect(url_for(".review", code=code, project_id=project.id, transcript_id=transcript.id))


def _register_recording(project, transcript):
    link = transcript.recording_link
    if link and not any(d.url == link for d in project.documents):
        db.session.add(ProjectDocument(project=project, title=f"Recording: {_meeting_label(transcript)}"[:200], url=link,
                                       doc_type="Recording", added_by_id=g.user.id, added_at=utcnow()))


# --- creating each kind --------------------------------------------------------------------------
def _create_scope(account, project, transcript, item):
    position = (db.session.query(func.max(ScopeItem.position)).filter_by(account_id=account.id).scalar() or 0) + 1
    db.session.add(ScopeItem(account_id=account.id, kind=item.kind, title=item.title, detail=item.detail,
                             position=position, updated_at=utcnow(), updated_by_id=g.user.id))
    return SCOPE_KINDS[item.kind], url_for("sops.scope", code=account.code)


def _create_sop(account, project, transcript, item):
    number = (db.session.query(func.max(Sop.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    link, note = _upload_document(project, item)
    sop = Sop(account_id=account.id, number=number, title=item.title[:200], owner_id=g.user.id, project_id=project.id,
              created_at=utcnow(), updated_at=utcnow())
    sop.versions.append(SopVersion(
        number=1, purpose=f"Drafted from the meeting {_meeting_label(transcript)}. Check it before submitting for review.",
        steps=item.detail, link=link or "", change_note="First version, from a meeting transcript",
        created_by_id=g.user.id, created_at=utcnow(), updated_at=utcnow()))
    db.session.add(sop)
    db.session.flush()
    add_sop_event(sop, g.user, "CREATED", 1, "From a meeting transcript")
    if link:
        _register_document(project, item, link)
    return f"{sop.code} draft{note}", url_for("sops.sop", code=account.code, sop_id=sop.id)


def _create_document(account, project, transcript, item):
    link, note = _upload_document(project, item)
    url = link or url_for(".document", code=account.code, project_id=project.id, transcript_id=transcript.id,
                          item_id=item.id)
    _register_document(project, item, url)
    return f"{DOCUMENT_TYPES[item.kind]}{note}", url


def _register_document(project, item, url):
    db.session.add(ProjectDocument(project=project, title=item.title[:200], url=url, doc_type=DOCUMENT_TYPES[item.kind],
                                   added_by_id=g.user.id, added_at=utcnow()))


def _create_kb(account, project, transcript, item):
    conn = servicenow.connection_for(account)
    if conn is None:
        raise CreateError("This account is not connected to ServiceNow.")
    if not conn.knowledge_base and not servicenow.is_demo(conn):
        raise CreateError("No knowledge base is set for this account's ServiceNow.")
    body = "".join(f"<p>{escape(line)}</p>" for line in item.detail.splitlines() if line.strip())
    body += f"<p>Source: KITE meeting transcript, {escape(_meeting_label(transcript))} ({escape(account.code)}).</p>"
    number, url = servicenow.create_draft(conn, item.title, body)
    return f"{number} draft in ServiceNow", url or ""


def _create_doubt(account, project, transcript, item):
    number = (db.session.query(func.max(Question.number)).filter_by(account_id=account.id).scalar() or 0) + 1
    question = Question(account_id=account.id, number=number, title=item.title[:200],
                        detail=(item.detail + f"\n\nFrom the meeting {_meeting_label(transcript)}.").strip(),
                        asked_by_id=g.user.id, project_id=project.id, created_at=utcnow(), updated_at=utcnow())
    db.session.add(question)
    db.session.flush()
    return question.code, url_for("doubts.question", code=account.code, question_id=question.id)


def _create_access(account, project, transcript, item):
    """A sign-in with Microsoft and membership of the account. The person's role stays the default until an admin
    changes it, whatever the transcript says, and they can only sign in with an account of the company tenant."""
    if not g.user.is_admin:
        raise CreateError("Only an admin can create sign-ins.")
    match = transcripts.EMAIL_RE.search(item.title)
    if match is None:
        raise CreateError("Write the person as Name (email) in the title.")
    email = match.group(0).lower()
    user = User.query.filter(func.lower(User.email) == email).first()
    created = user is None
    if created:
        name = item.title[:match.start()].strip(" (<,") or email
        user = User(username=sso._new_username(email), full_name=name[:120], email=email, role=DEFAULT_ROLE,
                    department="", password_hash="", auth_source="sso")
        db.session.add(user)
        db.session.flush()
        add_admin_event(g.user, "USER_CREATED", user.username, f"{DEFAULT_ROLE}, Microsoft sign-in, from a meeting transcript")
    elif not user.active:
        raise CreateError(f"{email} is deactivated. Reactivate them in the admin console first.")
    if not any(m.account_id == account.id for m in user.memberships):
        db.session.add(AccountMember(account=account, user=user))
        add_admin_event(g.user, "MEMBER_ADDED", account.code, f"{user.username}, from a meeting transcript")
    elif not created:
        return f"{user.full_name} already has access", url_for("admin.edit_user", user_id=user.id)
    return f"Sign-in and {account.code} access for {user.full_name}", url_for("admin.edit_user", user_id=user.id)


CREATORS = {
    "IN_SCOPE": _create_scope, "OUT_OF_SCOPE": _create_scope, "SLA": _create_scope, "RESPONSIBILITY": _create_scope,
    "CONTACT": _create_scope, "SOP": _create_sop, "RUNBOOK": _create_document, "NOTES": _create_document,
    "KB": _create_kb, "DOUBT": _create_doubt, "ACCESS": _create_access,
}


# --- documents and SharePoint --------------------------------------------------------------------
def _word(item):
    transcript = item.transcript
    return transcripts.word_document(item.title, f"{transcript.project.name} · {_meeting_label(transcript)} · draft",
                                     item.detail)


def _filename(item):
    transcript = item.transcript
    date = transcript.meeting_date.isoformat() + " " if transcript.meeting_date else ""
    name = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', " ", f"{date}{item.kind_label.split(' (')[0]} - {item.title}")
    return " ".join(name.split())[:120].strip(" .") + ".docx"


def _sharepoint_on():
    return bool(current_app.config.get("SHAREPOINT_ENABLED"))


def _upload_document(project, item):
    """Upload the Word document to the project's KT folder. Returns (link, note); no link when SharePoint is off."""
    if not _sharepoint_on():
        return None, ", download it from KITE (SharePoint is off)"
    if not project.sharepoint_url:
        raise CreateError("Add the project's SharePoint KT folder link first.")
    return upload_to_sharepoint(project.sharepoint_url, _filename(item), _word(item)), ", in SharePoint"


def upload_to_sharepoint(folder_url, filename, data):
    """Put a file in the SharePoint folder a sharing link points to. Tests replace this function."""
    token = sso._client().acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in token:
        raise CreateError("KITE could not sign in to Microsoft Graph; check the app registration's permission.")
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    share = "u!" + base64.urlsafe_b64encode(folder_url.encode()).decode().rstrip("=")
    try:
        folder = requests.get(f"{GRAPH}/shares/{share}/driveItem", headers=headers, timeout=15)
        if folder.status_code in (401, 403, 404):
            raise CreateError("KITE cannot open the SharePoint KT folder; check the link and the app's permission.")
        folder.raise_for_status()
        info = folder.json()
        drive = info["parentReference"]["driveId"]
        path = requests.utils.quote(filename)
        answer = requests.put(f"{GRAPH}/drives/{drive}/items/{info['id']}:/{path}:/content",
                              params={"@microsoft.graph.conflictBehavior": "rename"},
                              headers={**headers, "Content-Type": WORD_MIME}, data=data, timeout=30)
        if answer.status_code in (401, 403):
            raise CreateError("KITE may not write to the SharePoint KT folder; check the app's permission.")
        answer.raise_for_status()
        return answer.json()["webUrl"][:500]
    except (requests.RequestException, KeyError, ValueError) as err:
        raise CreateError(f"SharePoint upload failed: {type(err).__name__}.") from err
