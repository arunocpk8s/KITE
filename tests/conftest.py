import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from models import Account, AccountMember, User, db  # noqa: E402

PASSWORD = "test-password-123"

# username -> (role, accounts)
USERS = {
    "admin": ("admin", ()),
    "lead": ("kt_lead", ("ACME",)),
    "sme": ("sme", ("ACME", "BETA")),
    "mem1": ("member", ("ACME", "OLD")),
    "mem2": ("member", ("BETA",)),
    "viewer": ("viewer", ("ACME",)),
}
ACCOUNTS = {
    "ACME": ("Acme Corporation", True),
    "BETA": ("Beta Bank", True),
    "OLD": ("Old Client", False),
}


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            "WTF_CSRF_ENABLED": False,
            "SMTP_HOST": "",
            "APP_BASE_URL": "http://kite.test",
        }
    )
    with app.app_context():
        accounts = {}
        for code, (name, active) in ACCOUNTS.items():
            accounts[code] = Account(code=code, name=name, description=f"{name} engagement", active=active)
            db.session.add(accounts[code])
        for username, (role, codes) in USERS.items():
            user = User(username=username, full_name=username.title(), role=role, department="DPS",
                        email=f"{username}@example.com")
            user.set_password(PASSWORD)
            db.session.add(user)
            for code in codes:
                db.session.add(AccountMember(account=accounts[code], user=user))
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username):
    return client.post("/login", data={"username": username, "password": PASSWORD})


def user_id(username):
    return User.query.filter_by(username=username).one().id


def account_id(code):
    return Account.query.filter_by(code=code).one().id


def queue_email(app, count=2):
    """Record emails that stay PENDING (no SMTP host in tests). Returns their ids."""
    import notify
    from models import EmailOutbox
    with app.app_context():
        for i in range(count):
            notify.send([f"person{i}@example.com"], f"Subject {i}", "Body")
        return [e.id for e in EmailOutbox.query.order_by(EmailOutbox.id)]
