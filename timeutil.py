"""Time helpers: UTC storage, IST display, time left and month ranges.

All datetimes are stored as naive UTC. They are shown to users in Asia/Kolkata (UTC+05:30).
"""
from datetime import datetime, timedelta, timezone

# India has no daylight saving, so a fixed offset is exact for Asia/Kolkata.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def utcnow():
    """Current UTC time as a naive datetime (the form stored in the database)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_overdue(due_at, now=None):
    return due_at is not None and (now or utcnow()) > due_at


def time_left(due_at, now=None):
    """Return (text, overdue) describing the time remaining until due_at."""
    now = now or utcnow()
    seconds = int((due_at - now).total_seconds())
    overdue = seconds < 0
    days, rem = divmod(abs(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}m"
    else:
        text = f"{minutes}m"
    return (f"Overdue by {text}" if overdue else text), overdue


def to_ist(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def format_ist(dt):
    return to_ist(dt).strftime("%d %b %Y %H:%M IST") if dt else ""


def ist_today(now=None):
    """Today's date in IST (deadlines are calendar dates)."""
    return to_ist(now or utcnow()).date()


def format_date(d):
    return d.strftime("%d %b %Y") if d else ""


def parse_date(value):
    """'2026-10-31' -> date, or None if empty or invalid."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def ist_local_to_utc(date_value, time_value):
    """A date and 'HH:MM' typed in IST -> naive UTC datetime, or None if the time is invalid."""
    try:
        local = datetime.strptime(f"{date_value.isoformat()} {time_value.strip()}", "%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return None
    return local.replace(tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def parse_month(value):
    """'2026-10' -> (2026, 10), or None if not a valid month."""
    try:
        year, month = (int(part) for part in value.split("-"))
    except (AttributeError, ValueError):
        return None
    return (year, month) if 2000 <= year <= 2100 and 1 <= month <= 12 else None


def ist_month_range_utc(year, month):
    """[start, end) of an IST calendar month, as naive UTC."""
    start = datetime(year, month, 1, tzinfo=IST)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=IST)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def recent_months(count=12, now=None):
    """[('2026-10', 'Oct 2026'), ...] newest first, in IST."""
    local = to_ist(now or utcnow())
    year, month = local.year, local.month
    months = []
    for _ in range(count):
        months.append((f"{year:04d}-{month:02d}", datetime(year, month, 1).strftime("%b %Y")))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return months

