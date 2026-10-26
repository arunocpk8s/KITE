"""Database models: users and client accounts; KT plan, projects and SOPs; scope of work; email, settings, logs."""
import sqlalchemy as sa
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

from timeutil import utcnow

db = SQLAlchemy()

# admin: everything. kt_lead: plans KT and signs off for their accounts. sme: owns knowledge
# (KT topics, SOPs, transactions). member: receives KT (newcomers). viewer: read-only.
ROLES = ("admin", "kt_lead", "sme", "member", "viewer")
ROLE_LABELS = {
    "admin": "Admin",
    "kt_lead": "KT lead",
    "sme": "SME",
    "member": "Team member",
    "viewer": "Viewer",
}
# Role given to people who first sign in with Microsoft.
DEFAULT_ROLE = "member"


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(120), nullable=False, default="")
    role = db.Column(db.String(20), nullable=False, default=DEFAULT_ROLE)
    # Empty for people who only sign in with Microsoft.
    password_hash = db.Column(db.String(255), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    # Microsoft Entra ID object id (the "oid" claim): the unchanging link to the company account.
    sso_oid = db.Column(db.String(64), unique=True, index=True)
    # How the person last signed in: "local" (password) or "sso" (Microsoft).
    auth_source = db.Column(db.String(10), nullable=False, default="local", server_default="local")
    last_login_at = db.Column(db.DateTime)

    memberships = db.relationship("AccountMember", back_populates="user", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def visible_accounts(self, include_inactive=False):
        """Admins see every account; everyone else only the accounts they are a member of."""
        query = Account.query
        if not self.is_admin:
            query = query.join(AccountMember).filter(AccountMember.user_id == self.id)
        if not include_inactive:
            query = query.filter(Account.active.is_(True))
        return query.order_by(Account.name)

    def can_view(self, account):
        if self.is_admin:
            return True
        return any(m.account_id == account.id for m in self.memberships)

    def can_manage(self, account):
        """Admins, and KT leads who are members of the account."""
        return self.is_admin or (self.role == "kt_lead" and self.can_view(account))

    def can_contribute(self, account):
        """Everyone in the account except viewers (ticks checklists, adds documents)."""
        return self.role != "viewer" and self.can_view(account)


class Account(db.Model):
    """A client account (customer engagement). All KT content belongs to one account."""

    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # Scope of work: contract dates and the link to the signed statement of work.
    contract_start = db.Column(db.Date)
    contract_end = db.Column(db.Date)
    scope_link = db.Column(db.String(500), nullable=False, default="", server_default="")

    members = db.relationship("AccountMember", back_populates="account", cascade="all, delete-orphan",
                              order_by="AccountMember.id")

    def member_users(self):
        return (User.query.join(AccountMember).filter(AccountMember.account_id == self.id)
                .order_by(User.active.desc(), User.full_name))


class AccountMember(db.Model):
    __tablename__ = "account_members"
    __table_args__ = (db.UniqueConstraint("account_id", "user_id", name="uq_account_member"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    added_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account", back_populates="members")
    user = db.relationship("User", back_populates="memberships")


# --- KT tracker --------------------------------------------------------------------------------
KT_STATUSES = ("PLANNED", "SCHEDULED", "DELIVERED", "REVERSE_KT", "SIGNED_OFF", "ON_HOLD", "CANCELLED")
KT_STATUS_LABELS = {
    "PLANNED": "Planned",
    "SCHEDULED": "Scheduled",
    "DELIVERED": "Delivered",
    "REVERSE_KT": "Reverse KT",
    "SIGNED_OFF": "Signed off",
    "ON_HOLD": "On hold",
    "CANCELLED": "Cancelled",
}
KT_CLOSED = ("SIGNED_OFF", "CANCELLED")
# Allowed moves, enforced on the server. SIGNED_OFF -> DELIVERED reopens a topic.
KT_TRANSITIONS = {
    "PLANNED": ("SCHEDULED", "ON_HOLD", "CANCELLED"),
    "SCHEDULED": ("DELIVERED", "PLANNED", "ON_HOLD", "CANCELLED"),
    "DELIVERED": ("REVERSE_KT", "SCHEDULED", "ON_HOLD", "CANCELLED"),
    "REVERSE_KT": ("SIGNED_OFF", "DELIVERED", "ON_HOLD", "CANCELLED"),
    "ON_HOLD": ("PLANNED", "SCHEDULED", "CANCELLED"),
    "SIGNED_OFF": ("DELIVERED",),
    "CANCELLED": (),
}
# Moves only a KT lead or admin may make (sign-off, cancel and reopening a signed-off topic).
KT_LEAD_ONLY = {("REVERSE_KT", "SIGNED_OFF"), ("SIGNED_OFF", "DELIVERED")} | {
    (s, "CANCELLED") for s in KT_STATUSES
}
# A rating at or below this flags the topic for a follow-up session.
LOW_RATING = 2


def kt_can_move(old, new):
    return new in KT_TRANSITIONS.get(old, ())


class KTModule(db.Model):
    """A group of KT topics in a project's KT plan (e.g. 'Billing', 'Month-end close')."""

    __tablename__ = "kt_modules"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    # Nullable only for databases from before projects; upgrade_schema() fills it in.
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account")
    project = db.relationship("KTProject", back_populates="modules")
    topics = db.relationship("KTTopic", back_populates="module", order_by="(KTTopic.position, KTTopic.id)")


class KTTopic(db.Model):
    __tablename__ = "kt_topics"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey("kt_modules.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    # The SME who owns and delivers the topic.
    sme_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    planned_date = db.Column(db.Date)
    # Target date for sign-off; open topics past it are overdue.
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False, default="PLANNED", index=True)
    # Links to documents and recordings, one per line: "Label | https://..." or just the URL.
    materials = db.Column(db.Text, nullable=False, default="")
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    signed_off_at = db.Column(db.DateTime)
    signed_off_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    account = db.relationship("Account")
    module = db.relationship("KTModule", back_populates="topics")
    sme = db.relationship("User", foreign_keys=[sme_id])
    signed_off_by = db.relationship("User", foreign_keys=[signed_off_by_id])
    receivers = db.relationship("KTReceiver", back_populates="topic", cascade="all, delete-orphan")
    sessions = db.relationship("KTSession", back_populates="topic", order_by="KTSession.starts_at",
                               cascade="all, delete-orphan")
    ratings = db.relationship("KTRating", back_populates="topic", cascade="all, delete-orphan")
    events = db.relationship("KTEvent", back_populates="topic", order_by="KTEvent.id", passive_deletes="all")

    @property
    def status_label(self):
        return KT_STATUS_LABELS.get(self.status, self.status)

    @property
    def is_open(self):
        return self.status not in KT_CLOSED

    def is_overdue(self, today):
        return self.is_open and self.status != "ON_HOLD" and self.due_date is not None and today > self.due_date

    @property
    def receiver_users(self):
        return sorted((r.user for r in self.receivers), key=lambda u: u.full_name)

    def is_receiver(self, user):
        return any(r.user_id == user.id for r in self.receivers)

    def receiver(self, user):
        return next((r for r in self.receivers if r.user_id == user.id), None)

    @property
    def unconfirmed_receivers(self):
        return sorted((r.user for r in self.receivers if r.confirmed_at is None), key=lambda u: u.full_name)

    @property
    def average_rating(self):
        scores = [r.score for r in self.ratings]
        return round(sum(scores) / len(scores), 1) if scores else None

    @property
    def low_ratings(self):
        return [r for r in self.ratings if r.score <= LOW_RATING]


class KTReceiver(db.Model):
    """A person on our side who receives the KT for a topic, and confirms once they have received it."""

    __tablename__ = "kt_receivers"
    __table_args__ = (db.UniqueConstraint("topic_id", "user_id", name="uq_kt_receiver"),)

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    confirmed_at = db.Column(db.DateTime)
    confirm_note = db.Column(db.Text, nullable=False, default="", server_default="")

    topic = db.relationship("KTTopic", back_populates="receivers")
    user = db.relationship("User")


class KTSession(db.Model):
    __tablename__ = "kt_sessions"

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), nullable=False, index=True)
    starts_at = db.Column(db.DateTime, nullable=False)  # UTC
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    meeting_link = db.Column(db.String(500), nullable=False, default="")
    recording_link = db.Column(db.String(500), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    held = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    topic = db.relationship("KTTopic", back_populates="sessions")
    attendance = db.relationship("KTAttendance", back_populates="session", cascade="all, delete-orphan")

    def attended(self, user):
        return any(a.user_id == user.id for a in self.attendance)


class KTAttendance(db.Model):
    __tablename__ = "kt_attendance"
    __table_args__ = (db.UniqueConstraint("session_id", "user_id", name="uq_kt_attendance"),)

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("kt_sessions.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    session = db.relationship("KTSession", back_populates="attendance")
    user = db.relationship("User")


class KTRating(db.Model):
    """A receiver's own rating of how well they understood the topic (1-5)."""

    __tablename__ = "kt_ratings"
    __table_args__ = (db.UniqueConstraint("topic_id", "user_id", name="uq_kt_rating"),)

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    topic = db.relationship("KTTopic", back_populates="ratings")
    user = db.relationship("User")


class KTEvent(db.Model):
    """Append-only history of a KT topic."""

    __tablename__ = "kt_events"

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(30), nullable=False)
    old_value = db.Column(db.String(200))
    new_value = db.Column(db.String(200))
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    topic = db.relationship("KTTopic", back_populates="events")
    actor = db.relationship("User")


def add_kt_event(topic, actor, event_type, old_value=None, new_value=None, comment=None):
    db.session.add(KTEvent(topic=topic, actor_id=actor.id, event_type=event_type, old_value=old_value,
                           new_value=new_value, comment=comment, created_at=utcnow()))


# --- Projects: KT checklist, client sign-off, internal KT and KT documents -----------------------
# Checklist items every new project starts with; the KT lead can change them.
DEFAULT_CHECKLIST = (
    "Access to the client's systems and tools granted to the team",
    "KT documents and recordings saved in the SharePoint KT folder",
    "Runbooks and SOPs handed over",
    "Contacts and escalation matrix shared",
    "Open issues, backlog and known problems handed over",
)
DOC_TYPES = ("KT deck", "Recording", "Runbook", "SOP", "Process document", "Architecture", "Other")


class KTProject(db.Model):
    """A client project inside an account, with its own KT plan, checklist, client sign-off,
    internal KT for the rest of our team, and a register of KT documents kept in SharePoint."""

    __tablename__ = "kt_projects"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    # The project's KT folder in SharePoint (or Teams files).
    sharepoint_url = db.Column(db.String(500), nullable=False, default="")
    # The client person who signs off the KT. With a KITE login on this email they can sign off themselves.
    client_approver_name = db.Column(db.String(120), nullable=False, default="")
    client_approver_email = db.Column(db.String(200), nullable=False, default="")
    # Client sign-off: by the approver in KITE, or recorded by a KT lead with evidence.
    client_signed_at = db.Column(db.DateTime)
    client_signer_name = db.Column(db.String(120), nullable=False, default="")
    client_signer_email = db.Column(db.String(200), nullable=False, default="")
    client_signoff_evidence = db.Column(db.String(500), nullable=False, default="")
    client_signoff_comment = db.Column(db.Text, nullable=False, default="")
    client_signoff_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    client_signed_in_kite = db.Column(db.Boolean, nullable=False, default=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account")
    client_signoff_by = db.relationship("User", foreign_keys=[client_signoff_by_id])
    modules = db.relationship("KTModule", back_populates="project", order_by="(KTModule.position, KTModule.id)")
    checklist = db.relationship("ProjectChecklistItem", back_populates="project", cascade="all, delete-orphan",
                                order_by="(ProjectChecklistItem.position, ProjectChecklistItem.id)")
    team = db.relationship("ProjectTeamMember", back_populates="project", cascade="all, delete-orphan")
    internal_sessions = db.relationship("InternalSession", back_populates="project", cascade="all, delete-orphan",
                                        order_by="InternalSession.starts_at")
    documents = db.relationship("ProjectDocument", back_populates="project", cascade="all, delete-orphan",
                                order_by="(ProjectDocument.doc_type, ProjectDocument.title)")
    events = db.relationship("ProjectEvent", back_populates="project", order_by="ProjectEvent.id",
                             passive_deletes="all")
    phases = db.relationship("TransitionPhase", back_populates="project", cascade="all, delete-orphan",
                             order_by="TransitionPhase.position")

    @property
    def topics(self):
        return [t for m in self.modules for t in m.topics]

    @property
    def client_signed(self):
        return self.client_signed_at is not None

    @property
    def team_users(self):
        return sorted((m.user for m in self.team), key=lambda u: u.full_name)

    def team_member(self, user):
        return next((m for m in self.team if m.user_id == user.id), None)

    def is_client_approver(self, user):
        email = self.client_approver_email.strip().lower()
        return bool(email) and (user.email or "").strip().lower() == email


class ProjectChecklistItem(db.Model):
    __tablename__ = "project_checklist"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    title = db.Column(db.String(300), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    done_at = db.Column(db.DateTime)
    done_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.Text, nullable=False, default="")
    evidence_link = db.Column(db.String(500), nullable=False, default="")

    project = db.relationship("KTProject", back_populates="checklist")
    done_by = db.relationship("User")


class ProjectTeamMember(db.Model):
    """Someone in our team who must complete internal KT for the project (given by our receivers)."""

    __tablename__ = "project_team"
    __table_args__ = (db.UniqueConstraint("project_id", "user_id", name="uq_project_team"),)

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    completed_at = db.Column(db.DateTime)
    comment = db.Column(db.Text, nullable=False, default="")

    project = db.relationship("KTProject", back_populates="team")
    user = db.relationship("User")


class InternalSession(db.Model):
    """An internal KT session: our receivers pass the client's KT on to the rest of the team."""

    __tablename__ = "internal_kt_sessions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    starts_at = db.Column(db.DateTime, nullable=False)  # UTC
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    presenter_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    agenda = db.Column(db.Text, nullable=False, default="")
    meeting_link = db.Column(db.String(500), nullable=False, default="")
    recording_link = db.Column(db.String(500), nullable=False, default="")
    held = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    project = db.relationship("KTProject", back_populates="internal_sessions")
    presenter = db.relationship("User", foreign_keys=[presenter_id])


class ProjectDocument(db.Model):
    """A KT document kept in SharePoint; KITE stores the link, not the file."""

    __tablename__ = "project_documents"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    doc_type = db.Column(db.String(40), nullable=False, default="Other")
    url = db.Column(db.String(500), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"))
    added_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    added_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    project = db.relationship("KTProject", back_populates="documents")
    topic = db.relationship("KTTopic")
    added_by = db.relationship("User")

    @property
    def in_sharepoint(self):
        parts = self.url.split("/")
        return len(parts) > 2 and parts[2].lower().endswith(".sharepoint.com")


class ProjectEvent(db.Model):
    """Append-only history of a project (checklist, sign-off, internal KT, documents)."""

    __tablename__ = "project_events"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(30), nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    project = db.relationship("KTProject", back_populates="events")
    actor = db.relationship("User")


def add_project_event(project, actor, event_type, detail=None):
    db.session.add(ProjectEvent(project=project, actor_id=actor.id, event_type=event_type, detail=detail,
                                created_at=utcnow()))


# --- SOPs ----------------------------------------------------------------------------------------
# A version moves DRAFT -> IN_REVIEW -> APPROVED; approving a newer version makes the previous one
# SUPERSEDED. IN_REVIEW -> DRAFT sends it back. The SOP as a whole can be retired.
SOP_VERSION_LABELS = {"DRAFT": "Draft", "IN_REVIEW": "In review", "APPROVED": "Approved",
                      "SUPERSEDED": "Superseded"}
SOP_STATUS_LABELS = {"DRAFT": "Draft", "IN_REVIEW": "In review", "APPROVED": "Approved", "RETIRED": "Retired"}
DEFAULT_REVIEW_MONTHS = 12


class Sop(db.Model):
    __tablename__ = "sops"
    __table_args__ = (db.UniqueConstraint("account_id", "number", name="uq_sop_number"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    review_months = db.Column(db.Integer, nullable=False, default=DEFAULT_REVIEW_MONTHS)
    # Set when a version is approved: the approval date plus review_months.
    review_due = db.Column(db.Date)
    retired_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account")
    project = db.relationship("KTProject")
    owner = db.relationship("User")
    versions = db.relationship("SopVersion", back_populates="sop", order_by="SopVersion.number",
                               cascade="all, delete-orphan")
    events = db.relationship("SopEvent", back_populates="sop", order_by="SopEvent.id", passive_deletes="all")

    @property
    def code(self):
        return f"SOP-{self.number:03d}"

    @property
    def approved_version(self):
        return next((v for v in reversed(self.versions) if v.status == "APPROVED"), None)

    @property
    def working_version(self):
        """The newest version while it is still a draft or in review."""
        latest = self.versions[-1] if self.versions else None
        return latest if latest is not None and latest.status in ("DRAFT", "IN_REVIEW") else None

    @property
    def status(self):
        if self.retired_at:
            return "RETIRED"
        if self.approved_version:
            return "APPROVED"
        return self.working_version.status if self.working_version else "DRAFT"

    @property
    def status_label(self):
        return SOP_STATUS_LABELS[self.status]

    def review_overdue(self, today):
        return self.status == "APPROVED" and self.review_due is not None and today > self.review_due


class SopVersion(db.Model):
    __tablename__ = "sop_versions"
    __table_args__ = (db.UniqueConstraint("sop_id", "number", name="uq_sop_version"),)

    id = db.Column(db.Integer, primary_key=True)
    sop_id = db.Column(db.Integer, db.ForeignKey("sops.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    purpose = db.Column(db.Text, nullable=False, default="")
    steps = db.Column(db.Text, nullable=False, default="")
    # The SOP document in SharePoint.
    link = db.Column(db.String(500), nullable=False, default="")
    change_note = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="DRAFT")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    submitted_at = db.Column(db.DateTime)
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    sop = db.relationship("Sop", back_populates="versions")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    submitted_by = db.relationship("User", foreign_keys=[submitted_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    @property
    def status_label(self):
        return SOP_VERSION_LABELS[self.status]


class SopEvent(db.Model):
    """Append-only history of an SOP."""

    __tablename__ = "sop_events"

    id = db.Column(db.Integer, primary_key=True)
    sop_id = db.Column(db.Integer, db.ForeignKey("sops.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(30), nullable=False)
    version = db.Column(db.Integer)
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    sop = db.relationship("Sop", back_populates="events")
    actor = db.relationship("User")


def add_sop_event(sop, actor, event_type, version=None, comment=None):
    db.session.add(SopEvent(sop=sop, actor_id=actor.id, event_type=event_type, version=version, comment=comment,
                            created_at=utcnow()))


# --- Scope of work -------------------------------------------------------------------------------
SCOPE_KINDS = {
    "IN_SCOPE": "In scope",
    "OUT_OF_SCOPE": "Out of scope",
    "SLA": "Service levels (SLAs)",
    "RESPONSIBILITY": "Responsibilities (RACI)",
    "CONTACT": "Contacts",
}


class ScopeItem(db.Model):
    """One line of an account's scope of work: a service in or out of scope, an SLA, a responsibility or a contact."""

    __tablename__ = "scope_items"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    detail = db.Column(db.Text, nullable=False, default="")
    position = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    updated_by = db.relationship("User")


# --- Transition phases ---------------------------------------------------------------------------
# Each project moves through these in order. A phase starts once the previous one is signed off, and is
# signed off by a KT lead when all its exit criteria are met (the KT phase also needs the client's
# sign-off of the project's KT).
TRANSITION_PHASES = {
    "KT": "Knowledge transfer",
    "SHADOW": "Shadow",
    "REVERSE_SHADOW": "Reverse shadow",
    "STEADY_STATE": "Steady state",
}
PHASE_STATUS_LABELS = {"NOT_STARTED": "Not started", "IN_PROGRESS": "In progress", "SIGNED_OFF": "Signed off"}
DEFAULT_EXIT_CRITERIA = {
    "KT": ("Access to all systems in place for the whole team",
           "KT documents and recordings in the SharePoint KT folder"),
    "SHADOW": ("Team shadowed the client on live work for the agreed period",
               "Volumes, SLAs and peak periods observed and recorded",
               "Doubts raised during shadowing resolved"),
    "REVERSE_SHADOW": ("Team ran the work with the client watching",
                       "SLAs met during reverse shadow",
                       "Client confirms the team can run the work independently"),
    "STEADY_STATE": ("Hypercare period completed",
                     "Escalation path and on-call agreed",
                     "Steady-state reporting agreed with the client"),
}


class TransitionPhase(db.Model):
    __tablename__ = "transition_phases"
    __table_args__ = (db.UniqueConstraint("project_id", "phase", name="uq_transition_phase"),)

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), nullable=False, index=True)
    phase = db.Column(db.String(20), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    planned_start = db.Column(db.Date)
    planned_end = db.Column(db.Date)
    actual_start = db.Column(db.Date)
    actual_end = db.Column(db.Date)
    status = db.Column(db.String(12), nullable=False, default="NOT_STARTED")
    signed_off_at = db.Column(db.DateTime)
    signed_off_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    signoff_comment = db.Column(db.Text, nullable=False, default="")

    project = db.relationship("KTProject", back_populates="phases")
    signed_off_by = db.relationship("User")
    criteria = db.relationship("ExitCriterion", back_populates="phase", cascade="all, delete-orphan",
                               order_by="(ExitCriterion.position, ExitCriterion.id)")

    @property
    def label(self):
        return TRANSITION_PHASES[self.phase]

    @property
    def status_label(self):
        return PHASE_STATUS_LABELS[self.status]

    @property
    def criteria_done(self):
        return sum(1 for c in self.criteria if c.done_at)

    def is_late(self, today):
        return self.status != "SIGNED_OFF" and self.planned_end is not None and today > self.planned_end


class ExitCriterion(db.Model):
    __tablename__ = "exit_criteria"

    id = db.Column(db.Integer, primary_key=True)
    phase_id = db.Column(db.Integer, db.ForeignKey("transition_phases.id"), nullable=False, index=True)
    title = db.Column(db.String(300), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    done_at = db.Column(db.DateTime)
    done_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.Text, nullable=False, default="")
    evidence_link = db.Column(db.String(500), nullable=False, default="")

    phase = db.relationship("TransitionPhase", back_populates="criteria")
    done_by = db.relationship("User")


def ensure_phases(project):
    """Give the project its four transition phases with the default exit criteria (once). True if added."""
    if project.phases:
        return False
    for i, key in enumerate(TRANSITION_PHASES, start=1):
        phase = TransitionPhase(phase=key, position=i)
        for j, title in enumerate(DEFAULT_EXIT_CRITERIA[key], start=1):
            phase.criteria.append(ExitCriterion(title=title, position=j))
        project.phases.append(phase)
    return True


# --- Transactions and change log -----------------------------------------------------------------
TXN_FREQUENCIES = ("Daily", "Weekly", "Monthly", "Quarterly", "Yearly", "Ad hoc")
TXN_CRITICALITY = ("High", "Medium", "Low")
CHANGE_TYPES = {"NEW": "New", "CHANGED": "Changed", "RETIRED": "Retired"}


class Transaction(db.Model):
    """A client transaction (e.g. an ERP transaction code or a business process step) newcomers look up."""

    __tablename__ = "transactions"
    __table_args__ = (db.UniqueConstraint("account_id", "code", name="uq_transaction_code"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    code = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    system = db.Column(db.String(120), nullable=False, default="")
    purpose = db.Column(db.Text, nullable=False, default="")
    steps = db.Column(db.Text, nullable=False, default="")
    frequency = db.Column(db.String(20), nullable=False, default="Ad hoc")
    criticality = db.Column(db.String(10), nullable=False, default="Medium")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"))
    sop_id = db.Column(db.Integer, db.ForeignKey("sops.id"))
    link = db.Column(db.String(500), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    account = db.relationship("Account")
    project = db.relationship("KTProject")
    owner = db.relationship("User", foreign_keys=[owner_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    topic = db.relationship("KTTopic")
    sop = db.relationship("Sop")


class Change(db.Model):
    """A change-log entry: a new, changed or retired transaction or process. Once published, the team
    is emailed and each member acknowledges it. Published entries are not edited; publish a new one."""

    __tablename__ = "changes"
    __table_args__ = (db.UniqueConstraint("account_id", "number", name="uq_change_number"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    change_type = db.Column(db.String(10), nullable=False, default="CHANGED")
    description = db.Column(db.Text, nullable=False, default="")
    impact = db.Column(db.Text, nullable=False, default="")
    effective_date = db.Column(db.Date)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    sop_id = db.Column(db.Integer, db.ForeignKey("sops.id"))
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"))
    status = db.Column(db.String(10), nullable=False, default="DRAFT", index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    published_at = db.Column(db.DateTime)
    published_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    account = db.relationship("Account")
    project = db.relationship("KTProject")
    transaction = db.relationship("Transaction")
    sop = db.relationship("Sop")
    topic = db.relationship("KTTopic")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    published_by = db.relationship("User", foreign_keys=[published_by_id])
    acks = db.relationship("ChangeAck", back_populates="change", cascade="all, delete-orphan")

    @property
    def code(self):
        return f"CHG-{self.number:03d}"

    @property
    def type_label(self):
        return CHANGE_TYPES.get(self.change_type, self.change_type)

    @property
    def acked(self):
        return [a for a in self.acks if a.acked_at]

    @property
    def pending_acks(self):
        return sorted((a for a in self.acks if a.acked_at is None), key=lambda a: a.user.full_name)

    def ack_for(self, user):
        return next((a for a in self.acks if a.user_id == user.id), None)


class ChangeAck(db.Model):
    """Someone who must acknowledge a published change (everyone in the team at publish time)."""

    __tablename__ = "change_acks"
    __table_args__ = (db.UniqueConstraint("change_id", "user_id", name="uq_change_ack"),)

    id = db.Column(db.Integer, primary_key=True)
    change_id = db.Column(db.Integer, db.ForeignKey("changes.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    acked_at = db.Column(db.DateTime)
    comment = db.Column(db.Text, nullable=False, default="")

    change = db.relationship("Change", back_populates="acks")
    user = db.relationship("User")


# --- Doubts: technical questions that keep people from moving on ---------------------------------
QUESTION_STATUSES = {"OPEN": "Open", "ANSWERED": "Answered", "RESOLVED": "Resolved"}


class Question(db.Model):
    """A doubt raised by someone in the team, assigned to an SME. OPEN -> ANSWERED (someone replied)
    -> RESOLVED (the asker is unblocked). A reply from the asker reopens it."""

    __tablename__ = "questions"
    __table_args__ = (db.UniqueConstraint("account_id", "number", name="uq_question_number"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), index=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), index=True)
    number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    detail = db.Column(db.Text, nullable=False, default="")
    # Blocking: the person cannot move on with their work or KT until it is answered.
    blocking = db.Column(db.Boolean, nullable=False, default=False)
    asked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    status = db.Column(db.String(10), nullable=False, default="OPEN", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    resolved_at = db.Column(db.DateTime)

    account = db.relationship("Account")
    project = db.relationship("KTProject")
    topic = db.relationship("KTTopic")
    transaction = db.relationship("Transaction")
    asked_by = db.relationship("User", foreign_keys=[asked_by_id])
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    replies = db.relationship("QuestionReply", back_populates="question", order_by="QuestionReply.id",
                              cascade="all, delete-orphan")
    kb_links = db.relationship("QuestionKBLink", back_populates="question", order_by="QuestionKBLink.id",
                               cascade="all, delete-orphan")

    @property
    def code(self):
        return f"Q-{self.number:03d}"

    @property
    def status_label(self):
        return QUESTION_STATUSES.get(self.status, self.status)

    @property
    def is_open(self):
        return self.status != "RESOLVED"


class QuestionReply(db.Model):
    __tablename__ = "question_replies"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    question = db.relationship("Question", back_populates="replies")
    author = db.relationship("User")


# --- ServiceNow knowledge base -------------------------------------------------------------------
KB_LINK_SOURCES = {"LINKED": "Linked", "PUBLISHED": "Published from KITE"}


class ServiceNowConnection(db.Model):
    """The client's ServiceNow instance, used to find KB articles for doubts and to publish solutions
    as draft articles. One per account; set in the admin console. The password is stored encrypted."""

    __tablename__ = "servicenow_connections"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, unique=True)
    # Host name such as acme.service-now.com, or "demo" for built-in sample articles.
    instance = db.Column(db.String(200), nullable=False)
    username = db.Column(db.String(200), nullable=False, default="")
    password_enc = db.Column(db.Text, nullable=False, default="")
    # sys_id of the knowledge base: limits suggestions to it and receives published drafts. Optional.
    knowledge_base = db.Column(db.String(32), nullable=False, default="")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    account = db.relationship("Account")


class QuestionKBLink(db.Model):
    """A ServiceNow KB article that holds the solution to a doubt (linked, or published from it)."""

    __tablename__ = "question_kb_links"
    __table_args__ = (db.UniqueConstraint("question_id", "number", name="uq_question_kb"),)

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False, index=True)
    number = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(300), nullable=False, default="")
    url = db.Column(db.String(500), nullable=False)
    source = db.Column(db.String(10), nullable=False, default="LINKED")
    added_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    added_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    question = db.relationship("Question", back_populates="kb_links")
    added_by = db.relationship("User")

    @property
    def source_label(self):
        return KB_LINK_SOURCES.get(self.source, self.source)


# --- Newcomer onboarding -------------------------------------------------------------------------
PATH_ITEM_KINDS = {"TOPIC": "KT topic", "SOP": "SOP", "TRANSACTION": "Transaction", "LINK": "Document or link"}
PROGRESS_STATUSES = {"READ": "Read", "UNDERSTOOD": "Understood", "NEEDS_HELP": "Needs help"}
GLOSSARY_KINDS = {"TERM": "Term", "SYSTEM": "System", "CONTACT": "Contact", "FAQ": "FAQ"}


class LearningPath(db.Model):
    """An ordered list of what a newcomer in a role studies: KT topics, SOPs, transactions and documents."""

    __tablename__ = "learning_paths"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("kt_projects.id"), index=True)
    name = db.Column(db.String(200), nullable=False)
    for_role = db.Column(db.String(120), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account")
    project = db.relationship("KTProject")
    items = db.relationship("PathItem", back_populates="path", cascade="all, delete-orphan",
                            order_by="(PathItem.position, PathItem.id)")
    onboardings = db.relationship("Onboarding", back_populates="path")


class PathItem(db.Model):
    __tablename__ = "path_items"

    id = db.Column(db.Integer, primary_key=True)
    path_id = db.Column(db.Integer, db.ForeignKey("learning_paths.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    kind = db.Column(db.String(12), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"))
    sop_id = db.Column(db.Integer, db.ForeignKey("sops.id"))
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    title = db.Column(db.String(200), nullable=False, default="")  # for links
    url = db.Column(db.String(500), nullable=False, default="")  # for links
    note = db.Column(db.Text, nullable=False, default="")
    required = db.Column(db.Boolean, nullable=False, default=True)

    path = db.relationship("LearningPath", back_populates="items")
    topic = db.relationship("KTTopic")
    sop = db.relationship("Sop")
    transaction = db.relationship("Transaction")

    @property
    def kind_label(self):
        return PATH_ITEM_KINDS[self.kind]

    @property
    def label(self):
        if self.kind == "TOPIC" and self.topic:
            return self.topic.title
        if self.kind == "SOP" and self.sop:
            return f"{self.sop.code} {self.sop.title}"
        if self.kind == "TRANSACTION" and self.transaction:
            return f"{self.transaction.code} {self.transaction.name}"
        return self.title or self.url


class Onboarding(db.Model):
    """A newcomer following a learning path, with a buddy (SME) and a target date. ACTIVE -> READY
    once every required item is understood and the newcomer has given reverse KT to the buddy."""

    __tablename__ = "onboardings"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    path_id = db.Column(db.Integer, db.ForeignKey("learning_paths.id"), nullable=False, index=True)
    newcomer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    buddy_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    target_date = db.Column(db.Date)
    status = db.Column(db.String(10), nullable=False, default="ACTIVE", index=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reverse_kt_at = db.Column(db.DateTime)
    reverse_kt_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reverse_kt_note = db.Column(db.Text, nullable=False, default="")
    ready_at = db.Column(db.DateTime)
    ready_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancel_reason = db.Column(db.Text, nullable=False, default="")

    account = db.relationship("Account")
    path = db.relationship("LearningPath", back_populates="onboardings")
    newcomer = db.relationship("User", foreign_keys=[newcomer_id])
    buddy = db.relationship("User", foreign_keys=[buddy_id])
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    reverse_kt_by = db.relationship("User", foreign_keys=[reverse_kt_by_id])
    ready_by = db.relationship("User", foreign_keys=[ready_by_id])
    progress = db.relationship("OnboardingProgress", back_populates="onboarding", cascade="all, delete-orphan")

    def progress_for(self, item):
        return next((p for p in self.progress if p.item_id == item.id), None)

    @property
    def required_items(self):
        return [i for i in self.path.items if i.required]

    @property
    def understood(self):
        return sum(1 for i in self.required_items
                   if (p := self.progress_for(i)) is not None and p.status == "UNDERSTOOD")

    @property
    def percent(self):
        items = self.required_items
        return round(100 * self.understood / len(items)) if items else 100

    @property
    def needs_help(self):
        return [p for p in self.progress if p.status == "NEEDS_HELP"]

    def is_late(self, today):
        return self.status == "ACTIVE" and self.target_date is not None and today > self.target_date


class OnboardingProgress(db.Model):
    __tablename__ = "onboarding_progress"
    __table_args__ = (db.UniqueConstraint("onboarding_id", "item_id", name="uq_onboarding_item"),)

    id = db.Column(db.Integer, primary_key=True)
    onboarding_id = db.Column(db.Integer, db.ForeignKey("onboardings.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("path_items.id"), nullable=False, index=True)
    status = db.Column(db.String(12), nullable=False)
    note = db.Column(db.Text, nullable=False, default="")
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"))
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    onboarding = db.relationship("Onboarding", back_populates="progress")
    item = db.relationship("PathItem")
    question = db.relationship("Question")

    @property
    def status_label(self):
        return PROGRESS_STATUSES[self.status]


class GlossaryEntry(db.Model):
    """Client terms, systems, contacts and FAQs a newcomer needs."""

    __tablename__ = "glossary"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default="TERM")
    term = db.Column(db.String(200), nullable=False)
    meaning = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    updated_by = db.relationship("User")

    @property
    def kind_label(self):
        return GLOSSARY_KINDS[self.kind]


@event.listens_for(KTEvent, "before_update")
@event.listens_for(KTEvent, "before_delete")
@event.listens_for(ProjectEvent, "before_update")
@event.listens_for(ProjectEvent, "before_delete")
@event.listens_for(SopEvent, "before_update")
@event.listens_for(SopEvent, "before_delete")
def _history_is_append_only(mapper, connection, target):
    raise RuntimeError(f"{type(target).__name__} rows are append-only")


class EmailOutbox(db.Model):
    """Every email attempt. PENDING rows are retried by `flask resend-failed`."""

    __tablename__ = "email_outbox"

    id = db.Column(db.Integer, primary_key=True)
    recipients = db.Column(db.String(500), nullable=False)
    subject = db.Column(db.String(300), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="PENDING", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime)


class Setting(db.Model):
    """Settings changed in the admin console; they override the .env defaults."""

    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))


class AdminEvent(db.Model):
    """Append-only log of admin console actions and sign-in events. Never contains passwords."""

    __tablename__ = "admin_events"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    target = db.Column(db.String(200), nullable=False, default="")
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    actor = db.relationship("User")


@event.listens_for(AdminEvent, "before_update")
@event.listens_for(AdminEvent, "before_delete")
def _admin_events_are_append_only(mapper, connection, target):
    raise RuntimeError("AdminEvent rows are append-only")


def get_setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row is not None else default


def delete_setting(key):
    row = db.session.get(Setting, key)
    if row is not None:
        db.session.delete(row)


def set_setting(key, value, actor):
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    row.updated_at = utcnow()
    row.updated_by_id = actor.id
    db.session.add(row)


def add_admin_event(actor, action, target="", detail=None):
    db.session.add(AdminEvent(actor_id=actor.id, action=action, target=target, detail=detail, created_at=utcnow()))


# Columns added after the first release: (table, column, SQL definition). Applied at start-up.
ADDED_COLUMNS = [
    ("accounts", "contract_start", "DATE"),
    ("accounts", "contract_end", "DATE"),
    ("accounts", "scope_link", "VARCHAR(500) NOT NULL DEFAULT ''"),
    ("kt_modules", "project_id", "INTEGER REFERENCES kt_projects(id)"),
    ("kt_receivers", "confirmed_at", "TIMESTAMP"),
    ("kt_receivers", "confirm_note", "TEXT NOT NULL DEFAULT ''"),
]


def upgrade_schema():
    """Bring an existing database up to date with the current models (safe to run repeatedly)."""
    tables = {t for t, _, _ in ADDED_COLUMNS}
    inspector = sa.inspect(db.engine)
    existing = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    with db.engine.begin() as conn:
        for table, column, definition in ADDED_COLUMNS:
            if column not in existing[table]:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    # Modules from before projects go into a "General" project of their account.
    orphans = KTModule.query.filter(KTModule.project_id.is_(None)).all()
    projects = {}
    for module in orphans:
        if module.account_id not in projects:
            projects[module.account_id] = KTProject(account_id=module.account_id, name="General",
                                                    description="KT planned before projects were added.")
            db.session.add(projects[module.account_id])
        module.project = projects[module.account_id]
    if orphans:
        db.session.commit()
    # Projects from before transition phases get theirs.
    if any([ensure_phases(p) for p in KTProject.query.all()]):
        db.session.commit()
