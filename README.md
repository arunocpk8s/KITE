# KITE – Knowledge Transfer tracker

KITE helps delivery teams take over and run client work: it tracks knowledge transfer (KT), gets
newcomers productive quickly, keeps a catalogue of client transactions and changes, and holds each
client's scope of work and SOPs. Flask + SQLAlchemy + server-rendered Jinja2 templates.

Developed by: **Arun Arumugam**

The plan and build phases are in [PLAN.md](PLAN.md). **Phase 1 (current):** sign-in, client
accounts, roles, account membership and the Admin console. KT topics, SOPs, transactions and
newcomer onboarding follow in phases 2–6.

| Environment | Where | Database | Sign-in |
|---|---|---|---|
| Development | your PC (`flask run`) | SQLite `instance/kite.db` | username and password |
| Test | its own Vercel project (dummy data only) | its own Neon PostgreSQL | username and password |
| Production | Azure App Service | its own Azure Database for PostgreSQL | Microsoft (Entra ID) SSO, break-glass admin |

KITE was started from a copy of FAXORA but shares nothing with it: separate folder, git
repository, settings, secrets and databases.

## Roles and client accounts

| Role | Can do (Phase 1) | Later phases |
|---|---|---|
| Admin | Everything; Admin console (accounts, users, members, mail, outbox); sees every account | |
| KT lead | Sees their accounts and team | Plans KT, schedules sessions, signs off, assigns newcomers |
| SME | Sees their accounts and team | Owns and delivers KT topics, SOPs and transactions |
| Team member | Sees their accounts and team | Follows learning paths, rates understanding, does reverse KT |
| Viewer | Sees their accounts (read-only) | Read-only progress |

Every piece of KT content belongs to a **client account** (code such as `ACME`, name,
description). People are **members** of one or more accounts and only see those; admins see all.
A deactivated account is hidden from its members; nothing is deleted.

## Install and run (Windows, company PC)

```powershell
cd C:\Users\aarumugam\Desktop\KT-Tracker
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then set SECRET_KEY and the SEED_* passwords
.venv\Scripts\python.exe seed.py
.venv\Scripts\python.exe -m flask --app app run
```

Open http://localhost:5001 (KITE uses port 5001, so it can run next to FAXORA on 5000). `seed.py` creates `admin`, `lead` (KT lead), `sme` and `member`
(newcomer) with the `SEED_*_PASSWORD` values from `.env`, and a **DEMO** account for the sample
users. Users without a password are skipped, so production creates only the admin.

> Company PCs block `.exe` launchers such as `flask.exe` and `pip.exe`, so every command runs as
> `.venv\Scripts\python.exe -m ...`.

Generate a secret key with `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"`.

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

GitHub Actions runs the same tests on every push once the repository has a remote.

## Admin console (`/admin`, admins only)

- **Overview:** active accounts, people by role, pending emails, accounts without a KT lead, and
  the last 30 admin actions.
- **Client accounts:** add an account (code, name, description); edit; add or remove members;
  deactivate or reactivate.
- **Users:** add, edit, reset password, deactivate; set the role, department and client accounts.
  With Microsoft sign-in on, users can be added without a password (linked by email on their
  first sign-in). The list shows each person's accounts, sign-in method and last sign-in.
- **Mail server:** SMTP host, port, STARTTLS, login, encrypted password, sender and the KITE
  address used in email links; send a test email.
- **Email outbox:** every email with status and last error; retry, cancel or resend all.

Every console action is written to the append-only `admin_events` table; passwords are never logged.

## Command line

```powershell
.venv\Scripts\python.exe -m flask --app app create-account ACME --name "Acme Corporation"
.venv\Scripts\python.exe -m flask --app app create-user ravi --name "Ravi Kumar" --email ravi@company.com --role sme
.venv\Scripts\python.exe -m flask --app app add-member ACME ravi
.venv\Scripts\python.exe -m flask --app app set-role ravi kt_lead
.venv\Scripts\python.exe -m flask --app app set-email ravi ravi.k@company.com
.venv\Scripts\python.exe -m flask --app app test-email you@company.com
.venv\Scripts\python.exe -m flask --app app resend-failed
```

Roles: `admin`, `kt_lead`, `sme`, `member`, `viewer`.

## Microsoft sign-in (SSO)

Off by default. When on, people sign in with their company Microsoft account (Microsoft Entra ID,
OpenID Connect authorisation code flow with PKCE, via `msal`).

- Matched by the Microsoft object id (`oid`); on the first sign-in, by email to an existing user
  (who is then linked); otherwise a new **team member** is created with no accounts until an admin
  adds them. Role and accounts always come from KITE, never from Microsoft.
- Deactivated users and other tenants are refused. Creation, linking and break-glass logins are logged.
- IT provides a single-tenant app registration: redirect URI `https://<host>/auth/callback` (and
  `http://localhost:5001/auth/callback`), delegated `openid profile email User.Read` with admin
  consent, tenant id, client id and a client secret.

| Setting | Meaning |
|---|---|
| `SSO_ENABLED` | `true` shows "Sign in with Microsoft" |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | app registration; required when SSO is on |
| `AZURE_REDIRECT_URI` | optional; default `APP_BASE_URL` + `/auth/callback` |
| `LOCAL_LOGIN_ENABLED` | password login for everyone (default `true`); `false` at go-live |
| `BREAK_GLASS_USERS` | usernames that may still use a password when local login is off |
| `SESSION_HOURS` | session length, default 8 |

## Production on Azure App Service

Production runs on Azure App Service (Linux, Python 3.12, gunicorn) with its **own** Azure Database
for PostgreSQL. With `APP_ENV=production` the app refuses to start without PostgreSQL, a
`SECRET_KEY` or an `https://` `APP_BASE_URL`. Requirements: Azure CLI (`az login`), PowerShell 7.3+.

1. Create the resources once (billable):
   `.\deploy\azure\provision.ps1 -ResourceGroup rg-kite-prod -AppName kite-prod -PgServer kite-prod-pg`
2. Deploy the last commit (plus the logo from this PC):
   `.\deploy\azure\deploy.ps1 -ResourceGroup rg-kite-prod -AppName kite-prod`
3. Create the first admin: App Service → SSH →
   `cd $APP_PATH && source antenv/bin/activate`, `export SEED_ADMIN_PASSWORD='<long random password>'`, `python seed.py`.
4. Microsoft sign-in: set the `AZURE_*` settings and `SSO_ENABLED=true`, restart.
5. Mail: Admin console → Mail server (company relay on 587 with STARTTLS; port 25 is blocked in Azure).
6. Check `https://<app>.azurewebsites.net/healthz` → `{"status": "ok", "database": "ok"}`.

Client SOPs and transactions are confidential: keep the GitHub repository **private**, use only
dummy data on any test site, and keep secrets in App Service settings (Key Vault recommended).

## Project layout

```
app.py        app factory, start-up checks, home and account pages, CLI commands
auth.py       login (password, Microsoft, break-glass), logout, login_required
sso.py        Microsoft Entra ID sign-in (MSAL) and account matching
admin.py      Admin console: client accounts, members, users, mail server, outbox
models.py     User, Account, AccountMember, EmailOutbox, Setting, AdminEvent
notify.py     SMTP email sending, outbox and retry
timeutil.py   UTC storage, IST display, time left, month ranges
config.py     settings from environment variables (APP_ENV, database, SSO)
seed.py       first users and the DEMO account
wsgi.py       entry point for gunicorn (Azure) and Vercel: wsgi:app
templates/    Jinja2 pages (admin/ holds the console)
static/       style.css, app.js, logo
tests/        pytest tests
deploy/azure/ provision.ps1 and deploy.ps1
PLAN.md       requirements, design and build phases
```
