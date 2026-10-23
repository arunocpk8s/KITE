"""Reports: KT progress, sign-off trend, project stages, change acknowledgements, doubts and team
readiness, for the accounts the person can see (or one of them). Charts are drawn server-side as
HTML/SVG (no chart library), animated with CSS, with a table view for every chart."""
import csv
import io
from datetime import datetime, time, timedelta

from flask import Blueprint, Response, g, render_template, request

from auth import login_required
from models import TRANSITION_PHASES, Change, KTProject, KTTopic, Onboarding, ProjectTeamMember, Question, Sop, User
from projects import STAGES, project_state
from transitions import current_phase
from timeutil import IST, ist_today

bp = Blueprint("reports", __name__, url_prefix="/reports")

WEEKS = 12
MAX_BARS = 12
# Ordered progress stages share one blue ramp, light -> dark = further along (validated: >= 2:1 on white,
# adjacent steps >= 15 dE normal vision and CVD).
RAMP = ("#86b6ef", "#2a78d6", "#104281")
SERIES = "#2a78d6"
KT_GROUPS = (("Not started", ("PLANNED", "ON_HOLD")), ("In progress", ("SCHEDULED", "DELIVERED", "REVERSE_KT")),
             ("Signed off", ("SIGNED_OFF",)))
DOUBT_GROUPS = (("Open", ("OPEN",)), ("Answered", ("ANSWERED",)), ("Resolved", ("RESOLVED",)))


def nice_max(value):
    """The smallest of 5, 10, 20, 50, 100, ... (1-2-5 steps) that is >= value."""
    step = 1
    while True:
        for m in (1, 2, 5):
            if m * step >= max(value, 5):
                return m * step
        step *= 10


def _scope():
    accounts = g.user.visible_accounts().all()
    code = request.args.get("account", "").upper()
    chosen = [a for a in accounts if a.code == code]
    return accounts, (chosen or accounts), (chosen[0] if chosen else None)


def stacked_bars(rows, groups):
    """rows: [(label, items)] where each item has .status -> bar dicts with segments sized to the longest bar."""
    bars = []
    for label, items in rows:
        counts = [sum(1 for i in items if i.status in statuses) for _, statuses in groups]
        bars.append({"label": label, "counts": counts, "total": sum(counts)})
    bars = sorted(bars, key=lambda b: -b["total"])[:MAX_BARS]
    top = max([b["total"] for b in bars] + [1])
    for b in bars:
        b["width"] = round(100 * b["total"] / top, 1)
        b["segments"] = [{"name": name, "count": n, "color": RAMP[i], "share": round(100 * n / b["total"], 1)}
                         for i, ((name, _), n) in enumerate(zip(groups, b["counts"])) if n]
    return {"bars": bars, "legend": [(name, RAMP[i]) for i, (name, _) in enumerate(groups)]}


def signoff_trend(topics, today):
    """Cumulative signed-off topics at the end of each of the last WEEKS weeks, as SVG geometry."""
    ends = [today - timedelta(days=7 * (WEEKS - 1 - i)) for i in range(WEEKS)]
    signed = [t.signed_off_at for t in topics if t.status == "SIGNED_OFF" and t.signed_off_at]
    values = [upto_ist(d, signed) for d in ends]
    top = nice_max(max(values + [0]))
    x0, x1, y0, y1 = 44, 624, 196, 16
    xs = [x0 + (x1 - x0) * i / (WEEKS - 1) for i in range(WEEKS)]
    ys = [y0 - (y0 - y1) * v / top for v in values]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    area = f"{line} L{xs[-1]:.1f},{y0} L{xs[0]:.1f},{y0} Z"
    ticks = [(v, y0 - (y0 - y1) * v / top) for v in (0, top // 2, top)]
    slot = (x1 - x0) / (WEEKS - 1)
    points = [{"x": x, "y": y, "value": v, "label": f"Week to {d:%d %b}", "hit_x": x - slot / 2, "hit_w": slot}
              for x, y, v, d in zip(xs, ys, values, ends)]
    return {"line": line, "area": area, "ticks": ticks, "points": points, "x0": x0, "x1": x1, "y0": y0,
            "first": f"{ends[0]:%d %b}", "last": f"{ends[-1]:%d %b}", "total": values[-1],
            "added": values[-1] - values[0]}


def upto_ist(day, signed):
    """How many sign-offs (naive UTC datetimes) happened by the end of `day` in IST."""
    end_utc = (datetime.combine(day, time.max, IST) - IST.utcoffset(None)).replace(tzinfo=None)
    return sum(1 for s in signed if s <= end_utc)


def readiness(accounts, today):
    """One row per non-viewer member of the accounts: what they have received, confirmed and acknowledged."""
    ids = [a.id for a in accounts]
    people = {}
    for a in accounts:
        for u in a.member_users().filter(User.active.is_(True)).all():
            if u.role != "viewer":
                people[u.id] = u
    topics = KTTopic.query.filter(KTTopic.account_id.in_(ids), KTTopic.status != "CANCELLED").all()
    teams = ProjectTeamMember.query.join(KTProject).filter(KTProject.account_id.in_(ids)).all()
    changes = Change.query.filter(Change.account_id.in_(ids), Change.status == "PUBLISHED").all()
    doubts = Question.query.filter(Question.account_id.in_(ids), Question.status != "RESOLVED").all()
    rows = []
    for u in sorted(people.values(), key=lambda u: u.full_name):
        received = [t for t in topics if t.is_receiver(u)]
        confirmed = sum(1 for t in received if t.receiver(u).confirmed_at)
        ratings = [r.score for t in received for r in t.ratings if r.user_id == u.id]
        mine = [m for m in teams if m.user_id == u.id]
        acks = [a for c in changes for a in c.acks if a.user_id == u.id]
        asked = [q for q in doubts if q.asked_by_id == u.id]
        missing = []
        if confirmed < len(received):
            missing.append(f"{len(received) - confirmed} KT to confirm")
        if any(m.completed_at is None for m in mine):
            missing.append("internal KT")
        if any(a.acked_at is None for a in acks):
            missing.append(f"{sum(1 for a in acks if a.acked_at is None)} change(s) to acknowledge")
        if any(q.blocking for q in asked):
            missing.append("blocked by a doubt")
        rows.append({
            "user": u, "received": len(received), "confirmed": confirmed,
            "understanding": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "internal_done": sum(1 for m in mine if m.completed_at), "internal_total": len(mine),
            "acked": sum(1 for a in acks if a.acked_at), "acks_total": len(acks),
            "open_doubts": len(asked), "blocking": sum(1 for q in asked if q.blocking),
            "missing": missing, "ready": not missing and (received or mine or acks),
        })
    return rows


@bp.route("")
@login_required
def overview():
    accounts, scope, chosen = _scope()
    today = ist_today()
    ids = [a.id for a in scope]
    topics = KTTopic.query.filter(KTTopic.account_id.in_(ids)).all() if ids else []
    projects = KTProject.query.filter(KTProject.account_id.in_(ids)).order_by(KTProject.account_id,
                                                                               KTProject.position).all() if ids else []
    states = [(p, project_state(p)) for p in projects]
    active = [t for t in topics if t.status != "CANCELLED"]
    signed = sum(1 for t in active if t.status == "SIGNED_OFF")
    changes = (Change.query.filter(Change.account_id.in_(ids), Change.status == "PUBLISHED")
               .order_by(Change.published_at.desc()).limit(8).all()) if ids else []
    questions = Question.query.filter(Question.account_id.in_(ids)).all() if ids else []
    sops = Sop.query.filter(Sop.account_id.in_(ids)).all() if ids else []
    many = len(scope) > 1

    def name(p):
        return f"{p.account.code} · {p.name}" if many else p.name

    stage_counts = [(label, sum(1 for _, s in states if s["stage"] == key)) for key, label in STAGES.items()]
    stage_top = max([n for _, n in stage_counts] + [1])
    phase_keys = [current_phase(p).phase if p.phases and current_phase(p) else ("DONE" if p.phases else None)
                  for p in projects]
    phase_counts = [(label, phase_keys.count(key)) for key, label in
                    list(TRANSITION_PHASES.items()) + [("DONE", "Steady state signed off")]]
    phase_top = max([n for _, n in phase_counts] + [1])
    doubt_rows = [(a.code, [q for q in questions if q.account_id == a.id]) for a in scope] if many else \
        [(p.name, [q for q in questions if q.project_id == p.id]) for p in projects] + \
        [("No project", [q for q in questions if q.project_id is None])]
    tiles = {
        "progress": round(100 * signed / len(active)) if active else 0, "signed": signed, "topics": len(active),
        "complete": sum(1 for _, s in states if s["stage"] == "COMPLETE"), "projects": len(projects),
        "open_doubts": sum(1 for q in questions if q.is_open),
        "blocking": sum(1 for q in questions if q.is_open and q.blocking),
        "pending_acks": sum(len(c.pending_acks) for c in
                            Change.query.filter(Change.account_id.in_(ids), Change.status == "PUBLISHED").all()) if ids else 0,
        "reviews_overdue": sum(1 for s in sops if s.review_overdue(today)),
        "overdue": sum(1 for t in active if t.is_overdue(today)),
    }
    return render_template(
        "reports/overview.html", accounts=accounts, chosen=chosen, tiles=tiles,
        kt=stacked_bars([(name(p), [t for t in p.topics if t.status != "CANCELLED"]) for p in projects], KT_GROUPS),
        trend=signoff_trend(active, today),
        stages=[{"label": label, "count": n, "height": round(100 * n / stage_top, 1)} for label, n in stage_counts],
        phases=[{"label": label, "count": n, "height": round(100 * n / phase_top, 1)} for label, n in phase_counts],
        acks=[{"change": c, "done": len(c.acked), "total": len(c.acks),
               "pct": round(100 * len(c.acked) / len(c.acks)) if c.acks else 100} for c in changes],
        doubts=stacked_bars([r for r in doubt_rows if r[1]], DOUBT_GROUPS),
        people=readiness(scope, today), series=SERIES, ramp=RAMP, today=today,
        newcomers=Onboarding.query.filter(Onboarding.account_id.in_(ids), Onboarding.status != "CANCELLED")
        .order_by(Onboarding.status, Onboarding.target_date).all() if ids else [],
    )


@bp.route("/readiness.csv")
@login_required
def readiness_csv():
    _, scope, chosen = _scope()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Name", "Role", "KT topics received", "Confirmed", "Average understanding",
                     "Internal KT done", "Internal KT total", "Changes acknowledged", "Changes total",
                     "Open doubts", "Blocking doubts", "Ready", "Still to do"])
    for r in readiness(scope, ist_today()):
        writer.writerow([r["user"].full_name, r["user"].role_label, r["received"], r["confirmed"],
                         r["understanding"] or "", r["internal_done"], r["internal_total"], r["acked"],
                         r["acks_total"], r["open_doubts"], r["blocking"], "yes" if r["ready"] else "no",
                         "; ".join(r["missing"])])
    suffix = chosen.code if chosen else "all"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="kite-readiness-{suffix}-{ist_today()}.csv"'})
