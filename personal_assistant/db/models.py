import enum
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from personal_assistant.config import settings


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    SUMMARIZED = "summarized"
    NOTIFIED = "notified"
    APPROVED = "approved"
    REJECTED = "rejected"
    CV_GENERATED = "cv_generated"
    CV_APPROVED = "cv_approved"
    APPLIED = "applied"
    NETWORKING_DONE = "networking_done"
    FAILED = "failed"
    # Human outcome tracking
    RESPONSE_RECEIVED = "response_received"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_DONE = "interview_done"
    SECOND_INTERVIEW = "second_interview"
    OFFER = "offer"
    HIRED = "hired"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class ApplicationMethod(str, enum.Enum):
    EASY_APPLY = "easy_apply"
    EMAIL = "email"
    EXTERNAL = "external"
    MANUAL = "manual"


class ContactRole(str, enum.Enum):
    HIRING_MANAGER = "hiring_manager"
    RECRUITER = "recruiter"
    ENGINEERING_LEAD = "engineering_lead"
    OTHER = "other"


class MessageStatus(str, enum.Enum):
    DRAFTED = "drafted"
    SENT_FOR_APPROVAL = "sent_for_approval"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    REJECTED = "rejected"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    linkedin_job_id = Column(String(64), unique=True, nullable=False, index=True)
    title = Column(String(256), nullable=False)
    company = Column(String(256), nullable=False)
    location = Column(String(256))
    salary_text = Column(String(128))
    salary_min = Column(Integer)
    salary_max = Column(Integer)
    description = Column(Text)
    job_url = Column(String(1024))
    is_easy_apply = Column(Boolean, default=False)
    is_remote = Column(Boolean, default=False)
    posted_at = Column(DateTime)

    # Analysis fields
    relevance_score = Column(Integer)
    tech_stack = Column(Text)  # JSON list
    summary = Column(Text)
    is_priority_industry = Column(Boolean, default=False)

    # Status tracking
    status = Column(Enum(JobStatus), default=JobStatus.DISCOVERED, index=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    applied_at = Column(DateTime)

    # CV tailoring
    tailored_cv_path = Column(String(512))
    cover_email = Column(Text)

    # Ownership
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Human tracking
    user_notes = Column(Text)
    interview_date = Column(DateTime)

    # Relationships
    user = relationship("User", back_populates="jobs")
    applications = relationship("Application", back_populates="job")
    contacts = relationship("Contact", back_populates="job")


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    method = Column(Enum(ApplicationMethod))
    submitted_at = Column(DateTime)
    confirmation_text = Column(Text)
    success = Column(Boolean)
    notes = Column(Text)

    job = relationship("Job", back_populates="applications")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    name = Column(String(256), nullable=False)
    title = Column(String(256))
    role = Column(Enum(ContactRole))
    linkedin_url = Column(String(1024))
    email = Column(String(256))
    company = Column(String(256))

    job = relationship("Job", back_populates="contacts")
    messages = relationship("Message", back_populates="contact")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    channel = Column(String(32))  # "linkedin" or "email"
    subject = Column(String(512))
    body = Column(Text, nullable=False)
    status = Column(Enum(MessageStatus), default=MessageStatus.DRAFTED)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime)

    contact = relationship("Contact", back_populates="messages")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(128), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    is_approved = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    telegram_chat_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    jobs = relationship("Job", back_populates="user")


# Engine & session factory
engine = create_engine(settings.database_url, echo=False)


def init_db():
    """Create all tables (additive — safe to re-run)."""
    Base.metadata.create_all(engine)

    # Add columns that may not exist yet (SQLite ALTER TABLE)
    import sqlite3
    db_path = settings.database_url.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    for col, coltype in [
        ("user_notes", "TEXT"),
        ("interview_date", "DATETIME"),
        ("user_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Users table migrations
    try:
        conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.close()


def get_session() -> Session:
    """Return a new database session."""
    return sessionmaker(bind=engine)()
