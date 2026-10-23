"""'My next steps' on the home page: everything waiting on this person, so nobody is stuck wondering
how to move on. Each step says what to do and links straight to where it is done."""
from flask import url_for

from kt import CONFIRMABLE
from models import (
    LOW_RATING, Change, ChangeAck, InternalSession, KTProject, KTReceiver, KTTopic, Onboarding, ProjectTeamMember,
    Question, Sop, db,
)
from projects import project_state
from transitions import can_start, current_phase, sign_off_blockers
from timeutil import ist_today, utcnow

REVIEW_SOON_DAYS = 14


def _step(text, url, account, urgent=False, who="You"):
    return {"text": text, "url": url, "account": account.code, "urgent": urgent, "who": who}


def next_steps(user, today=None):
    today = today or ist_today()
    accounts = {a.id: a for a in user.visible_accounts()}
    if not accounts:
        return []
    ids = list(accounts)
    steps = []

    # As a receiver of KT from the client.
    receipts = (KTReceiver.query.join(KTTopic)
                .filter(KTReceiver.user_id == user.id, KTTopic.account_id.in_(ids), KTTopic.status.in_(CONFIRMABLE))
                .all())
    for r in receipts:
        t = r.topic
        link = url_for("kt.topic", code=t.account.code, topic_id=t.id)
        if r.confirmed_at is None:
            steps.append(_step(f"Confirm you received the KT for '{t.title}'", link + "#receivers", t.account))
        if not any(x.user_id == user.id for x in t.ratings):
            steps.append(_step(f"Rate how well you understood '{t.title}'", link + "#understanding", t.account))

    # Internal KT for the rest of the team.
    for m in (ProjectTeamMember.query.join(KTProject)
              .filter(ProjectTeamMember.user_id == user.id, ProjectTeamMember.completed_at.is_(None),
                      KTProject.account_id.in_(ids)).all()):
        p = m.project
        link = url_for("projects.project", code=p.account.code, project_id=p.id) + "#internal"
        if any(s.held for s in p.internal_sessions):
            steps.append(_step(f"Confirm you completed internal KT for '{p.name}'", link, p.account))
        elif not p.internal_sessions:
            steps.append(_step(f"Internal KT for '{p.name}' is not scheduled yet; ask a receiver to schedule it",
                               link, p.account))

    # Changes to acknowledge.
    for a in (ChangeAck.query.join(Change)
              .filter(ChangeAck.user_id == user.id, ChangeAck.acked_at.is_(None), Change.status == "PUBLISHED",
                      Change.account_id.in_(ids)).all()):
        c = a.change
        steps.append(_step(f"Acknowledge {c.code}: {c.title}",
                           url_for("transactions.change", code=c.account.code, change_id=c.id), c.account))

    # Doubts: to answer, and answers to check.
    for q in (Question.query.filter(Question.account_id.in_(ids), Question.assigned_to_id == user.id,
                                    Question.status == "OPEN").all()):
        steps.append(_step(f"Answer {q.code}{' (someone is blocked)' if q.blocking else ''}: {q.title}",
                           url_for("doubts.question", code=q.account.code, question_id=q.id), q.account,
                           urgent=q.blocking))
    for q in (Question.query.filter(Question.account_id.in_(ids), Question.asked_by_id == user.id,
                                    Question.status == "ANSWERED").all()):
        steps.append(_step(f"{q.code} has an answer: check it and resolve it, or reply",
                           url_for("doubts.question", code=q.account.code, question_id=q.id), q.account))

    # Onboarding: as the newcomer, and as the buddy.
    for ob in (Onboarding.query.filter(Onboarding.account_id.in_(ids), Onboarding.status == "ACTIVE",
                                       (Onboarding.newcomer_id == user.id) | (Onboarding.buddy_id == user.id)).all()):
        link = url_for("onboarding.view", code=ob.account.code, onboarding_id=ob.id)
        left = len(ob.required_items) - ob.understood
        if ob.newcomer_id == user.id and left:
            steps.append(_step(f"Continue your learning path '{ob.path.name}': {left} item(s) left", link, ob.account,
                               urgent=ob.is_late(today)))
        if ob.buddy_id == user.id and not left:
            text = (f"Mark {ob.newcomer.full_name} ready" if ob.reverse_kt_at
                    else f"Hold and record {ob.newcomer.full_name}'s reverse KT")
            steps.append(_step(text, link, ob.account))

    # As the SME delivering KT.
    now = utcnow()
    for t in KTTopic.query.filter(KTTopic.account_id.in_(ids), KTTopic.sme_id == user.id,
                                  KTTopic.status.in_(("PLANNED", "SCHEDULED"))).all():
        link = url_for("kt.topic", code=t.account.code, topic_id=t.id) + "#sessions"
        if t.status == "PLANNED" and not t.sessions:
            steps.append(_step(f"Schedule the KT session for '{t.title}'", link, t.account))
        elif any(not s.held and s.starts_at < now for s in t.sessions):
            steps.append(_step(f"Record the past session of '{t.title}' as held (attendance, recording)", link,
                               t.account))

    # SOPs this person owns.
    for s in Sop.query.filter(Sop.account_id.in_(ids), Sop.owner_id == user.id, Sop.retired_at.is_(None)).all():
        link = url_for("sops.sop", code=s.account.code, sop_id=s.id)
        last = s.events[-1].event_type if s.events else ""
        if s.working_version and s.working_version.status == "DRAFT" and last == "SENT_BACK":
            steps.append(_step(f"{s.code} was sent back: update it and resubmit", link, s.account))
        elif s.status == "APPROVED" and s.review_due and (s.review_due - today).days <= REVIEW_SOON_DAYS:
            steps.append(_step(f"{s.code} is due for review by {s.review_due:%d %b %Y}", link, s.account,
                               urgent=today > s.review_due))

    # As a KT lead (or admin) of the account.
    for account in accounts.values():
        if not user.can_manage(account):
            continue
        for t in KTTopic.query.filter_by(account_id=account.id, status="REVERSE_KT").all():
            if t.receivers and not t.unconfirmed_receivers:
                steps.append(_step(f"Sign off '{t.title}': every receiver has confirmed",
                                   url_for("kt.topic", code=account.code, topic_id=t.id), account))
        for t in KTTopic.query.filter(KTTopic.account_id == account.id,
                                      KTTopic.status.notin_(("SIGNED_OFF", "CANCELLED"))).all():
            if t.low_ratings:
                steps.append(_step(f"Plan a follow-up for '{t.title}' (understanding rated {LOW_RATING} or less)",
                                   url_for("kt.topic", code=account.code, topic_id=t.id) + "#understanding", account))
        for s in Sop.query.filter_by(account_id=account.id).filter(Sop.retired_at.is_(None)).all():
            w = s.working_version
            if w and w.status == "IN_REVIEW" and (user.is_admin or w.created_by_id != user.id):
                steps.append(_step(f"Review {s.code} v{w.number}: {s.title}",
                                   url_for("sops.sop", code=account.code, sop_id=s.id), account))
        for p in KTProject.query.filter_by(account_id=account.id).all():
            if project_state(p)["ready"]:
                steps.append(_step(f"Get the client's sign-off for '{p.name}'",
                                   url_for("projects.project", code=account.code, project_id=p.id) + "#sign-off",
                                   account))
            phase = current_phase(p) if p.phases else None
            link = url_for("transitions.view", code=account.code, project_id=p.id)
            if phase is not None and phase.status == "IN_PROGRESS" and not sign_off_blockers(phase):
                steps.append(_step(f"Sign off {phase.label} for '{p.name}': every exit criterion is met", link, account))
            elif phase is not None and can_start(phase) and phase.position > 1:
                steps.append(_step(f"Start {phase.label} for '{p.name}'", link, account))
        for q in Question.query.filter(Question.account_id == account.id, Question.assigned_to_id.is_(None),
                                       Question.status != "RESOLVED").all():
            steps.append(_step(f"Assign {q.code} to an SME: {q.title}",
                               url_for("doubts.question", code=account.code, question_id=q.id), account,
                               urgent=q.blocking))

    # As a client approver.
    for p in KTProject.query.filter(KTProject.account_id.in_(ids), KTProject.client_signed_at.is_(None)).all():
        if p.is_client_approver(user) and project_state(p)["ready"]:
            steps.append(_step(f"Sign off the KT for '{p.name}'",
                               url_for("projects.project", code=p.account.code, project_id=p.id) + "#sign-off",
                               p.account, urgent=True))

    steps.sort(key=lambda s: not s["urgent"])
    return steps


def upcoming_internal_sessions(user):
    """Internal KT sessions this person is on the team for, or presents, that have not happened yet."""
    team_projects = db.session.query(ProjectTeamMember.project_id).filter(ProjectTeamMember.user_id == user.id)
    return (InternalSession.query
            .filter(InternalSession.held.is_(False), InternalSession.starts_at >= utcnow(),
                    (InternalSession.project_id.in_(team_projects)) | (InternalSession.presenter_id == user.id))
            .order_by(InternalSession.starts_at).limit(10).all())

