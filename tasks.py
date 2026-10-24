"""Daily reminders: one email per person listing what needs them.

Covers KT and internal sessions tomorrow, overdue KT topics, SOP reviews due, changes not acknowledged
after ACK_GRACE_DAYS, doubts unanswered after DOUBT_GRACE_DAYS, and newcomers past their target date.

Run it once a day, either:
- `python -m flask --app app daily-reminders` (Task Scheduler on a PC), or
- GET/POST /tasks/daily-reminders with the header `Authorization: Bearer <CRON_SECRET>` (Vercel Cron, or a
  scheduled GitHub Actions workflow for Azure). Without CRON_SECRET the URL does not exist.
It runs at most once per IST day (a second call the same day sends nothing) unless forced from the CLI.
"""
import hmac
from collections import defaultdict
from datetime import datetime, time, timedelta

import click
from flask import Blueprint, abort, current_app, jsonify, request

import notify
from models import (
    Account, Change, ChangeAck, InternalSession, KTSession, KTTopic, Onboarding, Question, Setting, Sop, db,
    get_setting,
)
from timeutil import IST, format_date, format_ist, ist_today, utcnow

bp = Blueprint("tasks", __name__, url_prefix="/tasks")

ACK_GRACE_DAYS = 3
DOUBT_GRACE_DAYS = 2
REVIEW_SOON_DAYS = 14
LAST_RUN_KEY = "daily_reminders_last_run"


def _ist_day_utc(day):
    """[start, end) of an IST calendar day as naive UTC datetimes."""
    start = (datetime.combine(day, time.min, IST) - IST.utcoffset(None)).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def collect(today):
    """{email: [(account code, line, path)]} of everything due for each active person."""
    due = defaultdict(list)
    active = {a.id for a in Account.query.filter_by(active=True)}

    def add(user, account_id, line, path):
        if user is not None and user.active and account_id in active:
            due[user.email].append((db.session.get(Account, account_id).code, line, path))

    def leads(account_id):
        return [u for u in db.session.get(Account, account_id).member_users() if u.role == "kt_lead"]

    start, end = _ist_day_utc(today + timedelta(days=1))
    for s in KTSession.query.filter(KTSession.held.is_(False), KTSession.starts_at >= start, KTSession.starts_at < end):
        t = s.topic
        for u in t.receiver_users + ([t.sme] if t.sme else []):
            add(u, t.account_id, f"KT session tomorrow {format_ist(s.starts_at)}: {t.title}",
                f"/accounts/{t.account.code}/kt/topics/{t.id}")
    for s in InternalSession.query.filter(InternalSession.held.is_(False), InternalSession.starts_at >= start,
                                          InternalSession.starts_at < end):
        p = s.project
        for u in set(p.team_users) | {s.presenter}:
            add(u, p.account_id, f"Internal KT session tomorrow {format_ist(s.starts_at)}: {p.name}",
                f"/accounts/{p.account.code}/projects/{p.id}#internal")

    for t in KTTopic.query.filter(KTTopic.status.notin_(("SIGNED_OFF", "CANCELLED", "ON_HOLD")),
                                  KTTopic.due_date.isnot(None), KTTopic.due_date < today):
        path = f"/accounts/{t.account.code}/kt/topics/{t.id}"
        for u in ([t.sme] if t.sme else []) + leads(t.account_id):
            add(u, t.account_id, f"KT topic overdue since {format_date(t.due_date)}: {t.title}", path)

    for s in Sop.query.filter(Sop.retired_at.is_(None), Sop.review_due.isnot(None),
                              Sop.review_due <= today + timedelta(days=REVIEW_SOON_DAYS)):
        if s.status == "APPROVED":
            late = " (OVERDUE)" if s.review_due < today else ""
            for u in [s.owner] if s.owner else leads(s.account_id):
                add(u, s.account_id, f"SOP review due {format_date(s.review_due)}{late}: {s.code} {s.title}",
                    f"/accounts/{s.account.code}/sops/{s.id}")

    cutoff = utcnow() - timedelta(days=ACK_GRACE_DAYS)
    for a in ChangeAck.query.join(Change).filter(ChangeAck.acked_at.is_(None), Change.status == "PUBLISHED",
                                                 Change.published_at < cutoff):
        c = a.change
        add(a.user, c.account_id, f"Please acknowledge {c.code}: {c.title}", f"/accounts/{c.account.code}/changes/{c.id}")

    cutoff = utcnow() - timedelta(days=DOUBT_GRACE_DAYS)
    for q in Question.query.filter(Question.status == "OPEN", Question.updated_at < cutoff):
        path = f"/accounts/{q.account.code}/doubts/{q.id}"
        flag = " (BLOCKING)" if q.blocking else ""
        for u in [q.assigned_to] if q.assigned_to else leads(q.account_id):
            add(u, q.account_id, f"Doubt waiting for an answer{flag}: {q.code} {q.title}", path)

    for ob in Onboarding.query.filter(Onboarding.status == "ACTIVE", Onboarding.target_date.isnot(None),
                                      Onboarding.target_date < today):
        path = f"/accounts/{ob.account.code}/onboarding/{ob.id}"
        for u in ([ob.buddy] if ob.buddy else []) + leads(ob.account_id):
            add(u, ob.account_id, f"{ob.newcomer.full_name} is past the target date "
                f"{format_date(ob.target_date)} ({ob.understood}/{len(ob.required_items)} understood)", path)
    return due


def send_daily(today=None, force=False):
    """Send the digests. Returns {"sent": people emailed, "skipped": True if it already ran today}."""
    today = today or ist_today()
    if not force and get_setting(LAST_RUN_KEY) == today.isoformat():
        return {"sent": 0, "skipped": True}
    due = collect(today)
    for email, lines in due.items():
        seen, body = set(), []
        for code, line, path in sorted(lines):
            if (code, line) not in seen:
                seen.add((code, line))
                body.append(f"- [{code}] {line}\n  {notify.app_url(path)}")
        notify.send([email], f"KITE reminders for {format_date(today)} ({len(body)})",
                    "Here is what needs you in KITE today:\n\n" + "\n".join(body) + notify.footer())
    row = db.session.get(Setting, LAST_RUN_KEY) or Setting(key=LAST_RUN_KEY)
    row.value, row.updated_at = today.isoformat(), utcnow()
    db.session.add(row)
    db.session.commit()
    return {"sent": len(due), "skipped": False}


@bp.route("/daily-reminders", methods=["GET", "POST"])
def daily_reminders():
    secret = current_app.config.get("CRON_SECRET") or ""
    if not secret:
        abort(404)
    given = request.headers.get("Authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {secret}".encode()):
        abort(401)
    return jsonify(send_daily())


def register_cli(app):
    @app.cli.command("daily-reminders")
    @click.option("--force", is_flag=True, help="Send even if the reminders already went out today.")
    def daily_reminders_command(force):
        """Email each person a digest of what needs them (run once a day)."""
        with app.test_request_context():
            result = send_daily(force=force)
        click.echo("Already sent today; use --force to send again." if result["skipped"]
                   else f"Sent reminders to {result['sent']} people.")
