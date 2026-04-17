"""Convenience queries for the job pipeline."""

from datetime import datetime

from sqlalchemy import select

from personal_assistant.db.models import (
    Job,
    JobStatus,
    Contact,
    Message,
    MessageStatus,
    get_session,
)


def upsert_job(linkedin_job_id: str, **fields) -> Job:
    """Insert a job or return existing one (dedup by linkedin_job_id)."""
    session = get_session()
    try:
        job = session.execute(
            select(Job).where(Job.linkedin_job_id == linkedin_job_id)
        ).scalar_one_or_none()
        if job:
            return job
        job = Job(linkedin_job_id=linkedin_job_id, **fields)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job
    finally:
        session.close()


def update_job_status(job_id: int, status: JobStatus, **extra_fields) -> None:
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            job.status = status
            for k, v in extra_fields.items():
                setattr(job, k, v)
            session.commit()
    finally:
        session.close()


def get_jobs_by_status(status: JobStatus) -> list[Job]:
    session = get_session()
    try:
        result = session.execute(
            select(Job).where(Job.status == status).order_by(Job.discovered_at.desc())
        ).scalars().all()
        # Detach from session so callers can use them
        session.expunge_all()
        return list(result)
    finally:
        session.close()


def get_job_by_id(job_id: int) -> Job | None:
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            session.expunge(job)
        return job
    finally:
        session.close()


def mark_job_applied(job_id: int) -> None:
    update_job_status(job_id, JobStatus.APPLIED, applied_at=datetime.utcnow())


def get_contacts_for_job(job_id: int) -> list[Contact]:
    session = get_session()
    try:
        result = session.execute(
            select(Contact).where(Contact.job_id == job_id)
        ).scalars().all()
        session.expunge_all()
        return list(result)
    finally:
        session.close()


def get_messages_for_contact(contact_id: int) -> list[Message]:
    session = get_session()
    try:
        result = session.execute(
            select(Message).where(Message.contact_id == contact_id)
        ).scalars().all()
        session.expunge_all()
        return list(result)
    finally:
        session.close()


def add_contact(job_id: int, **fields) -> Contact:
    session = get_session()
    try:
        contact = Contact(job_id=job_id, **fields)
        session.add(contact)
        session.commit()
        session.refresh(contact)
        session.expunge(contact)
        return contact
    finally:
        session.close()


def add_message(contact_id: int, **fields) -> Message:
    session = get_session()
    try:
        msg = Message(contact_id=contact_id, **fields)
        session.add(msg)
        session.commit()
        session.refresh(msg)
        session.expunge(msg)
        return msg
    finally:
        session.close()


def update_message_status(message_id: int, status: MessageStatus, **extra) -> None:
    session = get_session()
    try:
        msg = session.get(Message, message_id)
        if msg:
            msg.status = status
            for k, v in extra.items():
                setattr(msg, k, v)
            session.commit()
    finally:
        session.close()


# ── Dashboard queries ─────────────────────────────────────────────────────────

def get_all_jobs() -> list[Job]:
    """Return all jobs ordered by discovered_at desc."""
    session = get_session()
    try:
        result = session.execute(
            select(Job).order_by(Job.discovered_at.desc())
        ).scalars().all()
        session.expunge_all()
        return list(result)
    finally:
        session.close()


def get_job_detail(job_id: int) -> dict | None:
    """Return a job with its contacts, messages, and applications."""
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if not job:
            return None

        contacts = session.execute(
            select(Contact).where(Contact.job_id == job_id)
        ).scalars().all()

        contacts_data = []
        for c in contacts:
            msgs = session.execute(
                select(Message).where(Message.contact_id == c.id)
            ).scalars().all()
            contacts_data.append({
                "id": c.id,
                "name": c.name,
                "title": c.title,
                "role": c.role.value if c.role else None,
                "linkedin_url": c.linkedin_url,
                "email": c.email,
                "messages": [
                    {
                        "id": m.id,
                        "channel": m.channel,
                        "subject": m.subject,
                        "body": m.body,
                        "status": m.status.value if m.status else None,
                        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                    }
                    for m in msgs
                ],
            })

        from personal_assistant.db.models import Application
        apps = session.execute(
            select(Application).where(Application.job_id == job_id)
        ).scalars().all()
        apps_data = [
            {
                "method": a.method.value if a.method else None,
                "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
                "success": a.success,
                "notes": a.notes,
            }
            for a in apps
        ]

        data = {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "salary_text": job.salary_text,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "description": job.description,
            "job_url": job.job_url,
            "is_easy_apply": job.is_easy_apply,
            "is_remote": job.is_remote,
            "relevance_score": job.relevance_score,
            "tech_stack": job.tech_stack,
            "summary": job.summary,
            "status": job.status.value if job.status else None,
            "discovered_at": job.discovered_at.isoformat() if job.discovered_at else None,
            "applied_at": job.applied_at.isoformat() if job.applied_at else None,
            "tailored_cv_path": job.tailored_cv_path,
            "cover_email": job.cover_email,
            "user_notes": job.user_notes,
            "interview_date": job.interview_date.isoformat() if job.interview_date else None,
            "contacts": contacts_data,
            "applications": apps_data,
        }
        return data
    finally:
        session.close()


def update_job_notes(job_id: int, notes: str) -> None:
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            job.user_notes = notes
            session.commit()
    finally:
        session.close()
