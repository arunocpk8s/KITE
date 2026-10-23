# KITE – Knowledge Transfer tracker: plan

Developed by: **Arun Arumugam**

KITE helps a delivery team take over and run client work: it tracks knowledge transfer (KT), gets
newcomers productive quickly, keeps a catalogue of client transactions and changes, and holds each
client's scope of work and SOPs in one place. It starts from a copy of FAXORA (same stack: Flask,
PostgreSQL, Microsoft sign-in, Azure App Service), in its own folder, git repository and databases.

## 1. Users and roles

| Role | Who | Can do |
|---|---|---|
| Admin | Delivery or tool owner | Everything; client accounts, users, settings, reports |
| KT lead | Account or transition manager | Plans KT for their accounts, schedules sessions, signs off topics and phases, assigns newcomers |
| SME | Person who knows the work | Owns KT topics, SOPs and transactions; delivers sessions; answers questions |
| Team member / newcomer | Person receiving KT | Follows their learning path, attends KT, rates understanding, asks questions, does reverse KT |
| Viewer (optional) | Client or leadership | Read-only view of progress for one account |

Everyone signs in with Microsoft (as in FAXORA). Each person is assigned to one or more **client
accounts** and sees only those accounts (FAXORA's team scoping, reused).

## 2. Modules

### 2.1 KT tracker (requirement 1)
- **KT plan per account:** modules → topics. Topic: title, area, SME, receivers, planned date and
  duration, materials (links to SharePoint/Teams/recordings), notes.
- **Workflow:** `PLANNED → SCHEDULED → DELIVERED → REVERSE_KT → SIGNED_OFF`, plus `ON_HOLD` and
  `CANCELLED`; only allowed moves, full history (FAXORA's ticket workflow and events, reused).
- **Sessions:** date, attendees, recording link, attendance; email invite/reminder.
- **Understanding:** each receiver rates the topic 1–5; low scores flag a follow-up session.
- **Due dates and overdue** (FAXORA's SLA logic, reused) and a **% complete** per module and account.

### 2.2 Newcomer onboarding (requirement 2)
- **Learning path** per account and role: an ordered list of KT topics, SOPs and transactions to study.
- **Assignment:** KT lead assigns a path to a newcomer with a buddy (SME) and target date.
- **Progress:** each item marked *read*, *understood* (self-rating) or *needs help*; questions go to
  the SME as a comment thread on the item.
- **Readiness:** reverse KT done + all items understood → newcomer marked *ready*; readiness report.
- **Glossary / FAQ** per account (client terms, systems, contacts).

### 2.3 Transactions (requirement 3)
- **a. Transaction catalogue:** code, name, system/module, purpose, steps, frequency, criticality,
  owner SME, linked KT topic and SOP. Searchable; newcomers look things up here.
- **b. Transition details:** per account phases `KT → Shadow → Reverse shadow → Steady state`, with
  planned and actual dates, exit criteria checklist, status and sign-off by the KT lead.
- **c. Change log:** entries for new, changed or retired transactions and processes: date, what
  changed, impact, linked transaction / SOP / KT topic. The team is emailed and each member
  **acknowledges** it, so everyone stays current after KT.

### 2.4 Client scope of work and SOPs (requirement 4)
- **Scope of work:** in-scope and out-of-scope services, SLAs, responsibilities (RACI), contacts,
  contract dates.
- **SOPs:** title, purpose, steps, owner, version, `DRAFT → IN_REVIEW → APPROVED → RETIRED`, review
  date, version history, linked transactions. Review-due reminders.

### 2.5 Projects (added in phase 3)
- **Account → projects → modules → topics.** Each project has its own KT plan.
- **Receiver confirmation:** every receiver on our side confirms a topic's KT; sign-off waits for all.
- **Hand-over checklist** per project (standard items, editable), ticked with note and evidence link.
- **Client sign-off** once all topics are signed off and the checklist is complete: by the client
  approver in KITE (login with the approver email), or recorded by the KT lead with evidence. Locks
  the project's KT plan; revocable with a reason.
- **Internal KT:** after the client's KT, receivers hold internal sessions for the project's internal
  KT team; each member confirms. Client sign-off + internal KT = project complete.
- **KT documents:** SharePoint KT folder link and a register of document links per project.

### 2.6 Added (suggested)
- **Account dashboard:** KT % complete, overdue topics, newcomers and readiness, SOPs due for review,
  unacknowledged changes.
- **Search** across topics, SOPs, transactions and the glossary.
- **Email reminders:** session tomorrow, topic overdue, SOP review due, new change to acknowledge
  (FAXORA's email outbox, reused).
- **Reports and CSV:** KT status by account, newcomer readiness, SOP review status.
- **Audit log** of every change (reused).
- **Links, not uploads** at first: documents stay in SharePoint/Teams; KITE stores the link.

## 3. What is reused, changed and removed from FAXORA

| FAXORA | KITE |
|---|---|
| Users, roles, Admin console, Microsoft sign-in, break-glass | Kept; roles become admin, KT lead, SME, member, viewer |
| Teams (agents' team scoping) | Client accounts and account membership |
| Tickets, status workflow, history | KT topics; same workflow engine and append-only history |
| SLA due times and breach | Topic due dates, overdue, SOP review dates |
| Email outbox and retry | Invites, reminders, change notifications |
| Reports and CSV | KT, readiness and SOP reports |
| Azure / Vercel deployment scripts, `APP_ENV` checks | Kept, with **new** resources and databases |
| SOS, Medical, facility categories, emergency numbers | Removed |

## 4. Data model (first version)

`accounts`, `account_members` (user, account, role), `users`; `kt_modules`, `kt_topics`,
`kt_sessions`, `kt_attendance`, `kt_ratings`, `kt_events` (history); `learning_paths`,
`path_items`, `onboarding` (newcomer, path, buddy, target), `onboarding_progress`, `comments`;
`transactions`, `transition_phases`, `exit_criteria`, `changes`, `change_acks`; `scope_items`,
`sops`, `sop_versions`, `sop_events`; `kt_projects`, `project_checklist`, `project_team`,
`internal_kt_sessions`, `project_documents`, `project_events`; `glossary`; plus `email_outbox`, `settings`, `admin_events` from FAXORA.

## 5. Build phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Copy from FAXORA, fresh git repo, name KITE | **Done** (174 copied tests pass) |
| 1 | Rebrand; remove SOS/facility parts; client accounts and roles | **Done** (76 tests pass) |
| 2 | KT tracker: modules, topics, workflow, sessions, ratings, dashboard | **Done** (96 tests pass) |
| 3 | Scope of work and SOPs with versions, approval and review dates; plus projects (below) | **Done** (117 tests pass) |
| 4 | Transaction catalogue and change log with acknowledgements | Change published and acknowledged |
| 5 | Transition phases and exit criteria with sign-off | Phase signed off |
| 6 | Newcomer learning paths, progress, Q&A, readiness | Newcomer reaches *ready* |
| 7 | Reports, reminders, documents (HLD/LLD/Runbook), own GitHub repo, test and Azure deployment | Pilot with one account |

## 6. Decisions and open questions

- **Separation from FAXORA:** own folder, git repository (no remote yet), `.env`, `SECRET_KEY`,
  databases, Vercel project and Azure resources. Never point KITE at FAXORA's databases.
- **Client data is confidential:** recommend a **private** GitHub repository, and only dummy data
  on any test site; real client SOPs and transactions only in the Azure production copy.
- To confirm: file uploads later or links only? Quizzes for newcomers? Client viewers in phase 1?
  Account list and first pilot account? Who are the KT leads?
