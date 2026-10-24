"""Daily reminders: what goes into each person's digest, once per day, and the protected cron URL."""
from datetime import timedelta

import pytest

from conftest import account_id, user_id
from models import Change, ChangeAck, KTSession, Question, db
from test_kt import make_module, make_topic
from timeutil import ist_local_to_utc, ist_today, utcnow


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: messages.append((to, subject, body)))
    return messages


def seed(app):
    today = ist_today()
    mid = make_module(app)
    late = make_topic(app, mid, title="Late topic", status="SCHEDULED", due=today - timedelta(days=2))
    soon = make_topic(app, mid, title="Tomorrow topic", status="SCHEDULED", receivers=("mem1",))
    with app.app_context():
        db.session.add(KTSession(topic_id=soon, starts_at=ist_local_to_utc(today + timedelta(days=1), "10:00"),
                                 created_by_id=user_id("sme")))
        change = Change(account_id=account_id("ACME"), number=1, title="Old change", description="d",
                        status="PUBLISHED", created_by_id=user_id("sme"),
                        published_at=utcnow() - timedelta(days=5))
        change.acks = [ChangeAck(user_id=user_id("mem1"))]
        db.session.add(change)
        db.session.add(Question(account_id=account_id("ACME"), number=1, title="Stuck", blocking=True,
                                asked_by_id=user_id("mem1"), assigned_to_id=user_id("sme"),
                                updated_at=utcnow() - timedelta(days=3)))
        db.session.commit()
    return late


def test_digest_contents_and_once_a_day(app, sent):
    seed(app)
    with app.test_request_context():
        from tasks import send_daily
        assert send_daily() == {"sent": 3, "skipped": False}
        assert send_daily() == {"sent": 0, "skipped": True}  # already ran today
    mail = {to[0]: body for to, subject, body in sent}
    assert "KT session tomorrow" in mail["mem1@example.com"] and "Please acknowledge CHG-001" in mail["mem1@example.com"]
    assert "Late topic" in mail["sme@example.com"] and "(BLOCKING)" in mail["sme@example.com"]
    assert "Late topic" in mail["lead@example.com"]  # KT leads hear about overdue topics
    assert "http://kite.test/accounts/ACME/" in mail["mem1@example.com"]


def test_cron_url_needs_the_secret(app, client, sent):
    seed(app)
    assert client.get("/tasks/daily-reminders").status_code == 404  # off without CRON_SECRET
    app.config["CRON_SECRET"] = "s3cret-value"
    assert client.get("/tasks/daily-reminders").status_code == 401
    assert client.get("/tasks/daily-reminders", headers={"Authorization": "Bearer wrong"}).status_code == 401
    resp = client.post("/tasks/daily-reminders", headers={"Authorization": "Bearer s3cret-value"})
    assert resp.status_code == 200 and resp.get_json() == {"sent": 3, "skipped": False}


def test_cli_command(app, sent):
    seed(app)
    runner = app.test_cli_runner()
    assert "Sent reminders to 3 people" in runner.invoke(args=["daily-reminders"]).output
    assert "Already sent today" in runner.invoke(args=["daily-reminders"]).output
    assert "Sent reminders" in runner.invoke(args=["daily-reminders", "--force"]).output
