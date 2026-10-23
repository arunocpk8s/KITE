"""Database models: User, Account, AccountMember, EmailOutbox, Setting and AdminEvent."""
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


class Account(db.Model):
    """A client account (customer engagement). All KT content belongs to one account."""

    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

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
    """A group of KT topics in an account's KT plan (e.g. 'Billing', 'Month-end close')."""

    __tablename__ = "kt_modules"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    account = db.relationship("Account")
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

    @property
    def average_rating(self):
        scores = [r.score for r in self.ratings]
        return round(sum(scores) / len(scores), 1) if scores else None

    @property
    def low_ratings(self):
        return [r for r in self.ratings if r.score <= LOW_RATING]


class KTReceiver(db.Model):
    """A person who receives the KT for a topic."""

    __tablename__ = "kt_receivers"
    __table_args__ = (db.UniqueConstraint("topic_id", "user_id", name="uq_kt_receiver"),)

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("kt_topics.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

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


@event.listens_for(KTEvent, "before_update")
@event.listens_for(KTEvent, "before_delete")
def _kt_events_are_append_only(mapper, connection, target):
    raise RuntimeError("KTEvent rows are append-only")


def add_kt_event(topic, actor, event_type, old_value=None, new_value=None, comment=None):
    db.session.add(KTEvent(topic=topic, actor_id=actor.id, event_type=event_type, old_value=old_value,
                           new_value=new_value, comment=comment, created_at=utcnow()))


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
ADDED_COLUMNS = []


def upgrade_schema():
    """Bring an existing database up to date with the current models (safe to run repeatedly)."""
    if not ADDED_COLUMNS:
        return
    tables = {t for t, _, _ in ADDED_COLUMNS}
    inspector = sa.inspect(db.engine)
    existing = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    with db.engine.begin() as conn:
        for table, column, definition in ADDED_COLUMNS:
            if column not in existing[table]:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
