"""Create the first users and a DEMO client account.

Passwords come from SEED_ADMIN_PASSWORD, SEED_LEAD_PASSWORD, SEED_SME_PASSWORD and SEED_MEMBER_PASSWORD
(environment or .env). Users without a password are skipped, so production can create only the admin.
Existing users and accounts are left unchanged, so the script is safe to run more than once.
"""
import os
import sys

from app import create_app
from models import Account, AccountMember, User, db

USERS = [
    {"username": "admin", "full_name": "KITE Admin", "role": "admin",
     "email_env": "SEED_ADMIN_EMAIL", "default_email": "admin@example.com", "password_env": "SEED_ADMIN_PASSWORD"},
    {"username": "lead", "full_name": "Sample KT Lead", "role": "kt_lead",
     "email_env": "SEED_LEAD_EMAIL", "default_email": "lead@example.com", "password_env": "SEED_LEAD_PASSWORD"},
    {"username": "sme", "full_name": "Sample SME", "role": "sme",
     "email_env": "SEED_SME_EMAIL", "default_email": "sme@example.com", "password_env": "SEED_SME_PASSWORD"},
    {"username": "member", "full_name": "Sample Newcomer", "role": "member",
     "email_env": "SEED_MEMBER_EMAIL", "default_email": "member@example.com", "password_env": "SEED_MEMBER_PASSWORD"},
]
DEMO_ACCOUNT = {"code": "DEMO", "name": "Demo client",
                "description": "Sample account for trying KITE. Use test data only."}

MIN_PASSWORD_LENGTH = 8


def main():
    app = create_app()
    exit_code = 0
    with app.app_context():
        samples = []
        for spec in USERS:
            username = spec["username"]
            if User.query.filter_by(username=username).first():
                print(f"User '{username}' already exists; skipped.")
                continue
            password = os.environ.get(spec["password_env"], "")
            if len(password) < MIN_PASSWORD_LENGTH:
                print(f"{spec['password_env']} not set (8+ characters); '{username}' not created.", file=sys.stderr)
                if spec["role"] == "admin":
                    exit_code = 1
                continue
            user = User(username=username, full_name=spec["full_name"], role=spec["role"],
                        email=os.environ.get(spec["email_env"]) or spec["default_email"])
            user.set_password(password)
            db.session.add(user)
            if spec["role"] != "admin":
                samples.append(user)
            print(f"Created {spec['role']} user '{username}'.")

        # The sample users get a DEMO account to try things in; never created for the admin alone.
        if samples:
            account = Account.query.filter_by(code=DEMO_ACCOUNT["code"]).first()
            if account is None:
                account = Account(**DEMO_ACCOUNT)
                db.session.add(account)
                print(f"Created client account {account.code}.")
            for user in samples:
                db.session.add(AccountMember(account=account, user=user))
        db.session.commit()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
