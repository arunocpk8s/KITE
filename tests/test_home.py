"""The home page: insights, recent activity, search across workspaces and the template gallery."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import ProgrammingError

import activity
from conftest import account_id, login, user_id
from models import Account, KTDocument, Question, db
from timeutil import IST, utcnow


def add_doc(client, code="ACME", title="AP runbook", body="<p>Payment run</p>"):
    return client.post(f"/accounts/{code}/documents/new", data={"title": title, "body": body})


def test_home_shows_cards_and_only_activity_from_your_workspaces(app, client):
    login(client, "sme")
    add_doc(client, "ACME", "Acme runbook")
    add_doc(client, "BETA", "Beta runbook")
    client.get("/logout")

    login(client, "mem2")  # member of BETA only
    page = client.get("/").data.decode()
    for text in ("Recent activity", "Insights", "Create Document", "Template Gallery", "Hi Mem2!"):
        assert text in page
    assert "Add Workspace" not in page  # admins only
    assert "created" in page and "Beta runbook" in page and "Acme runbook" not in page
    client.get("/logout")

    login(client, "viewer")  # can read, cannot create
    page = client.get("/").data.decode()
    assert "Acme runbook" in page and "Create Document" not in page and "Template Gallery" not in page
    client.get("/logout")

    login(client, "admin")
    assert "Add Workspace" in client.get("/").data.decode()


def test_insights_count_only_visible_accounts(app, client):
    with app.app_context():
        for code, blocking in (("ACME", True), ("BETA", False)):
            db.session.add(Question(account_id=account_id(code), number=1, title="Why", detail="x", blocking=blocking,
                                    asked_by_id=user_id("sme")))
            db.session.add(KTDocument(account_id=account_id(code), title="Doc", body="", created_by_id=user_id("sme"),
                                      updated_by_id=user_id("sme")))
        db.session.commit()
        accounts = [Account.query.filter_by(code="BETA").one()]
        from app import home_insights
        insights = home_insights(accounts, [{"total": 4, "signed_off": 1, "overdue": 2}])
    assert insights["open_doubts"] == 1 and insights["blocking"] == 0 and insights["documents"] == 1
    assert insights["progress"] == 25 and insights["overdue"] == 2


def test_feed_leaves_out_comments_and_draft_changes(app, client):
    login(client, "lead")
    add_doc(client)
    with app.app_context():
        doc_id = KTDocument.query.one().id
    client.post(f"/accounts/ACME/documents/{doc_id}/comments", data={"body": "A private remark"})
    client.post("/accounts/ACME/changes/new", data={"title": "Unpublished change", "change_type": "CHANGED", "description": "x"})
    page = client.get("/").data.decode()
    assert "commented on" in page and "A private remark" not in page
    assert "Unpublished change" not in page


def test_activity_by_day_uses_ist_days_and_a_fixed_window(app, client):
    login(client, "lead")
    add_doc(client)
    with app.app_context():
        accounts = [Account.query.filter_by(code="ACME").one()]
        days = activity.activity_by_day(accounts)
        assert len(days) == 14 and days[-1]["count"] == 1 and days[-1]["height"] == 100
        assert sum(d["count"] for d in days[:-1]) == 0
        # Seen from a week later, it is in the middle of the window; from a month later it is gone.
        today = datetime.now(IST).date()
        assert days[-1]["count"] == activity.activity_by_day(accounts, today=today + timedelta(days=7))[-8]["count"]
        assert sum(d["count"] for d in activity.activity_by_day(accounts, today=today + timedelta(days=30))) == 0
        assert activity.activity_by_day([])[-1]["count"] == 0


def test_database_setup_survives_a_collision_with_another_copy_starting(app, monkeypatch):
    import app as app_module
    calls = []

    def collide(times):
        def create_all():
            calls.append(1)
            if len(calls) <= times:
                raise ProgrammingError("CREATE TABLE doc_folders", {}, Exception("already exists"))
        return create_all

    with app.app_context():
        monkeypatch.setattr(db, "create_all", collide(1))
        app_module.prepare_database(wait=0)
        assert len(calls) == 2
        calls.clear()
        monkeypatch.setattr(db, "create_all", collide(99))
        with pytest.raises(ProgrammingError):  # a real problem is still reported
            app_module.prepare_database(attempts=2, wait=0)
        assert len(calls) == 2


def test_time_ago():
    now = utcnow()
    assert activity.time_ago(now, now) == "just now"
    assert activity.time_ago(now - timedelta(minutes=5), now) == "5 min ago"
    assert activity.time_ago(now - timedelta(hours=1), now) == "1 hour ago"
    assert activity.time_ago(now - timedelta(days=2), now) == "2 days ago"
    assert activity.time_ago(datetime(2026, 1, 10, 12), datetime(2026, 3, 1)) == "10 Jan 2026"


def test_search_covers_visible_workspaces_only(app, client):
    login(client, "sme")
    add_doc(client, "ACME", "Dunning runbook")
    add_doc(client, "BETA", "Dunning at Beta")
    add_doc(client, "BETA", "Tagged", "<p><strong>Other words</strong></p>")
    client.get("/logout")

    login(client, "mem2")
    page = client.get("/search?q=dunning").data.decode()
    assert "Dunning at Beta" in page and "Dunning runbook" not in page and "1 result" in page
    assert "Beta Bank" in client.get("/search?q=beta+bank").data.decode()
    assert "Acme Corporation" not in client.get("/search?q=acme").data.decode()
    # The words of the document count, not its HTML tags; % is an ordinary character.
    assert "Tagged" not in client.get("/search?q=strong").data.decode()
    assert "Tagged" in client.get("/search?q=other+words").data.decode()
    assert "0 results" in client.get("/search?q=%25%25").data.decode()
    assert "at least 2" in client.get("/search?q=x").data.decode()
    assert "Dunning at Beta" in client.get("/accounts/BETA/search?q=dunning").data.decode()


def test_template_gallery_starts_a_document_in_the_chosen_workspace(app, client):
    login(client, "sme")
    page = client.get("/templates").data.decode()
    assert "Incident and on-call" in page and "Acme Corporation" in page and "Beta Bank" in page
    start = client.get("/templates/start?account=BETA&template=incident")
    assert start.status_code == 302 and "/accounts/BETA/documents/new?template=incident" in start.headers["Location"]
    assert client.get("/templates/start?template=incident").status_code == 404
    # A workspace you are not in stays closed however you get there.
    login(client, "mem2")
    assert client.get("/accounts/ACME/documents/new?template=incident").status_code == 404
    client.get("/logout")
    login(client, "viewer")
    assert "You can create documents" in client.get("/templates").data.decode()


def test_create_document_goes_straight_in_with_one_workspace(app, client):
    login(client, "lead")
    assert 'href="/accounts/ACME/documents/new"' in client.get("/").data.decode()
    client.get("/logout")
    login(client, "sme")
    assert 'href="/accounts/ACME/documents/new"' not in client.get("/").data.decode()


def test_rail_and_workspace_panel(app, client):
    login(client, "lead")
    home = client.get("/").data.decode()
    assert 'class="rail"' in home and 'class="workspace-panel"' not in home
    page = client.get("/accounts/ACME").data.decode()
    assert 'class="workspace-panel"' in page and "/accounts/ACME/documents" in page and "/accounts/ACME/glossary" in page
