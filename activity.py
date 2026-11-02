"""The home page's insights and recent activity, across the accounts a user can see.

The feed is read from what the app already records (event histories, document versions, doubts, published changes).
It says who did what to which item and leaves out comments and reasons: those stay on the item's own page.
"""
from datetime import timedelta

from flask import url_for

from models import (
    BacklogItem, Change, DocumentComment, DocumentVersion, KTDocument, KTEvent, KTProject, KTTopic, ProjectEvent, Question,
    QuestionReply, Sop, SopEvent,
)
from timeutil import format_date, ist_local_to_utc, ist_today, to_ist, utcnow

FEED_SIZE = 12
CHART_DAYS = 14

KT_ACTIONS = {
    "CREATED": "added KT topic",
    "STATUS_CHANGED": "changed the status of",
    "RECEIPT_CONFIRMED": "confirmed receiving",
    "SESSION_SCHEDULED": "scheduled a session for",
    "SESSION_HELD": "held a session for",
    "RATED": "rated their understanding of",
}
PROJECT_ACTIONS = {
    "CREATED": "started project",
    "CHECKLIST_ADDED": "added a checklist item to",
    "CHECKLIST_DONE": "completed a checklist item in",
    "CHECKLIST_REOPENED": "reopened a checklist item in",
    "CHECKLIST_REMOVED": "removed a checklist item from",
    "SIGN_OFF_REQUESTED": "asked the client to sign off",
    "CLIENT_SIGNED_OFF": "recorded client sign-off for",
    "SIGN_OFF_REVOKED": "revoked client sign-off for",
    "INTERNAL_SCHEDULED": "scheduled internal KT for",
    "INTERNAL_HELD": "held internal KT for",
    "INTERNAL_COMPLETED": "completed internal KT for",
    "DOCUMENT_ADDED": "added a document to",
    "DOCUMENT_REMOVED": "removed a document from",
    "TRANSCRIPT_ADDED": "added a meeting transcript to",
    "TRANSCRIPT_REMOVED": "removed a meeting transcript from",
    "TRANSCRIPT_APPLIED": "created items from a transcript in",
    "PHASE_PLANNED": "planned the phases of",
    "PHASE_STARTED": "started a phase in",
    "PHASE_SIGNED_OFF": "signed off a phase in",
    "PHASE_REOPENED": "reopened a phase in",
    "CRITERION_ADDED": "added an exit criterion to",
    "CRITERION_MET": "met an exit criterion in",
    "CRITERION_REOPENED": "reopened an exit criterion in",
    "CRITERION_REMOVED": "removed an exit criterion from",
}
SOP_ACTIONS = {
    "CREATED": "created SOP",
    "NEW_VERSION": "started a new version of",
    "EDITED": "edited",
    "SUBMITTED": "submitted for review",
    "APPROVED": "approved",
    "SENT_BACK": "sent back",
    "REVIEWED": "reviewed",
    "RETIRED": "retired",
}


def _item(when, user, action, title, url, account):
    return {"when": when, "who": user.full_name, "action": action, "title": title, "url": url, "account": account.code}


def _kt(e):
    action = "signed off" if e.event_type == "STATUS_CHANGED" and e.new_value == "SIGNED_OFF" \
        else KT_ACTIONS.get(e.event_type, "updated")
    t = e.topic
    return _item(e.created_at, e.actor, action, t.title, url_for("kt.topic", code=t.account.code, topic_id=t.id), t.account)


def _project(e):
    p = e.project
    return _item(e.created_at, e.actor, PROJECT_ACTIONS.get(e.event_type, "updated"), p.name,
                 url_for("projects.project", code=p.account.code, project_id=p.id), p.account)


def _sop(e):
    s = e.sop
    return _item(e.created_at, e.actor, SOP_ACTIONS.get(e.event_type, "updated"), f"{s.code} {s.title}",
                 url_for("sops.sop", code=s.account.code, sop_id=s.id), s.account)


def _document_url(d):
    return url_for("documents.view", code=d.account.code, doc_id=d.id)


def _version(v):
    d = v.document
    return _item(v.saved_at, v.saved_by, "created" if v.number == 1 else "edited", d.title, _document_url(d), d.account)


def _comment(c):
    d = c.document
    return _item(c.created_at, c.author, "commented on", d.title, _document_url(d), d.account)


def _question_url(q):
    return url_for("doubts.question", code=q.account.code, question_id=q.id)


def _question(q):
    return _item(q.created_at, q.asked_by, "raised doubt", f"{q.code} {q.title}", _question_url(q), q.account)


def _reply(r):
    q = r.question
    return _item(r.created_at, r.author, "replied to", f"{q.code} {q.title}", _question_url(q), q.account)


def _change(c):
    return _item(c.published_at, c.published_by, "published change", c.title,
                 url_for("transactions.change", code=c.account.code, change_id=c.id), c.account)


def _backlog(i):
    return _item(i.created_at, i.created_by, "added to the backlog", f"{i.code} {i.title}",
                 url_for("backlog.board", code=i.account.code), i.account)


def _sources(ids):
    """(query, its time column, row -> feed item) for everything shown in the feed. Drafts of changes are left out."""
    return [
        (KTEvent.query.join(KTTopic).filter(KTTopic.account_id.in_(ids)), KTEvent.created_at, _kt),
        (ProjectEvent.query.join(KTProject).filter(KTProject.account_id.in_(ids)), ProjectEvent.created_at, _project),
        (SopEvent.query.join(Sop).filter(Sop.account_id.in_(ids)), SopEvent.created_at, _sop),
        (DocumentVersion.query.join(KTDocument).filter(KTDocument.account_id.in_(ids)), DocumentVersion.saved_at, _version),
        (DocumentComment.query.join(KTDocument).filter(KTDocument.account_id.in_(ids)), DocumentComment.created_at, _comment),
        (Question.query.filter(Question.account_id.in_(ids)), Question.created_at, _question),
        (QuestionReply.query.join(Question).filter(Question.account_id.in_(ids)), QuestionReply.created_at, _reply),
        (Change.query.filter(Change.account_id.in_(ids), Change.status == "PUBLISHED"), Change.published_at, _change),
        (BacklogItem.query.filter(BacklogItem.account_id.in_(ids)), BacklogItem.created_at, _backlog),
    ]


def recent_activity(accounts, limit=FEED_SIZE):
    """The latest things done in these accounts, newest first."""
    ids = [a.id for a in accounts]
    if not ids:
        return []
    items = [build(row) for query, column, build in _sources(ids)
             for row in query.order_by(column.desc()).limit(limit)]
    return sorted(items, key=lambda i: i["when"], reverse=True)[:limit]


def activity_by_day(accounts, days=CHART_DAYS, today=None):
    """How much was done on each of the last `days` IST calendar days, oldest first."""
    today = today or ist_today()
    dates = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
    counts = dict.fromkeys(dates, 0)
    ids = [a.id for a in accounts]
    if ids:
        since = ist_local_to_utc(dates[0], "00:00")
        for query, column, _ in _sources(ids):
            for (when,) in query.with_entities(column).filter(column >= since):
                day = to_ist(when).date()
                if day in counts:
                    counts[day] += 1
    peak = max(counts.values()) or 1
    return [{"date": format_date(d), "day": d.day, "count": c, "height": round(100 * c / peak)} for d, c in counts.items()]


def time_ago(when, now=None):
    """'5 min ago', '3 hours ago', '2 days ago'; the date once it is more than a week old."""
    minutes = int(((now or utcnow()) - when).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    if days <= 7:
        return f"{days} day{'' if days == 1 else 's'} ago"
    return format_date(to_ist(when).date())
