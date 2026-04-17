"""Pipeline orchestrator.

Scheduled:
  run_discovery_pipeline() — called by APScheduler daily at 08:00
    scrape → keyword score → LLM analyze borderline → Telegram notify

Event-driven (triggered when user taps Apply in Telegram):
  run_cv_and_email_plan(job_id)
    tailor CV (Sonnet) → research contacts → draft outreach →
    attempt Easy Apply → send plan via Telegram + email
"""

import logging

from personal_assistant.analyzer.relevance import analyze_new_jobs
from personal_assistant.applicator.submit import submit_easy_apply
from personal_assistant.cv.tailoring import tailor_and_generate
from personal_assistant.db.models import Contact, JobStatus
from personal_assistant.db.queries import (
    get_contacts_for_job,
    get_job_by_id,
    get_jobs_by_status,
    get_messages_for_contact,
    mark_job_applied,
)
from personal_assistant.networker.outreach import draft_outreach
from personal_assistant.networker.research import find_company_contacts
from personal_assistant.notifier.email_plan import send_apply_plan_email
from personal_assistant.notifier.telegram import (
    send_apply_plan as send_telegram_apply_plan,
    send_batch_notifications,
    send_message as send_telegram_message,
)
from personal_assistant.scraper.auth import LinkedInSession
from personal_assistant.scraper.jobs import scrape_jobs

logger = logging.getLogger(__name__)


async def run_discovery_pipeline() -> None:
    """Stage 1 (scheduled 08:00): Scrape \u2192 Analyze \u2192 SMS notify."""
    logger.info("=== Starting discovery pipeline ===")

    session = LinkedInSession()
    await session.start()
    try:
        await scrape_jobs(session)
    finally:
        await session.close()

    above_threshold = analyze_new_jobs()

    if above_threshold:
        await send_batch_notifications(above_threshold)
        logger.info("Notified about %d relevant jobs", len(above_threshold))
    else:
        logger.info("No jobs above relevance threshold this run")


async def run_cv_and_email_plan(job_id: int) -> None:
    """Event-driven: triggered when user taps Apply in Telegram.

    1. Tailor CV and generate PDF (Claude Sonnet)
    2. Research LinkedIn contacts + draft outreach messages (Playwright)
    3. Attempt Easy Apply if available
    4. Send apply plan via Telegram + email backup
    5. Update job status
    """
    from personal_assistant.db.models import get_session as _get_db_session

    job = get_job_by_id(job_id)
    if not job:
        logger.error("run_cv_and_email_plan: job %d not found", job_id)
        return

    logger.info("=== CV + plan for job %d: %s @ %s ===", job.id, job.title, job.company)

    # \u2500\u2500 Step 1: Tailor CV \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    pdf_path, _cover_email, cv_changes_summary = tailor_and_generate(job)
    if not pdf_path:
        await send_telegram_message(f"\u26a0\ufe0f CV tailoring failed for job #{job_id}. Check logs.")
        return
    logger.info("CV generated: %s", pdf_path)
    job = get_job_by_id(job_id)  # refresh after status update

    # \u2500\u2500 Step 2 + 3: LinkedIn contacts, outreach drafts, optional Easy Apply \u2500
    easy_apply_attempted = False
    session = LinkedInSession()
    await session.start()
    try:
        # Research contacts
        try:
            contacts_data = await find_company_contacts(session, job)
        except Exception as e:
            logger.warning("Contact research failed: %s", e)
            contacts_data = []

        # Draft a LinkedIn outreach note per contact
        for cd in contacts_data:
            try:
                db = _get_db_session()
                contact = db.get(Contact, cd["id"])
                if contact:
                    db.expunge(contact)
                db.close()
                if contact:
                    draft_outreach(job, contact, channel="linkedin")
            except Exception as e:
                logger.warning("Outreach draft failed for contact %s: %s", cd.get("id"), e)

        # Attempt Easy Apply
        if job.is_easy_apply:
            try:
                easy_apply_attempted = await submit_easy_apply(session, job)
                if easy_apply_attempted:
                    mark_job_applied(job_id)
                    logger.info("Easy Apply submitted for job %d", job_id)
            except Exception as e:
                logger.warning("Easy Apply failed for job %d: %s", job_id, e)
    finally:
        await session.close()

    # \u2500\u2500 Step 4: Gather contacts + messages for the email \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    contacts = get_contacts_for_job(job_id)
    contacts_with_messages = [
        (c, (get_messages_for_contact(c.id) or [None])[0])
        for c in contacts
    ]

    # \u2500\u2500 Step 5: Email plan \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    job = get_job_by_id(job_id)  # final refresh

    # Build contacts info and outreach messages for Telegram
    contacts_info = []
    outreach_messages = {}
    for c, msg in contacts_with_messages:
        info = {
            "id": c.id,
            "name": c.name,
            "title": getattr(c, "title", ""),
            "linkedin_url": getattr(c, "linkedin_url", ""),
            "channel": "LinkedIn",
        }
        if msg and hasattr(msg, "content") and msg.content:
            info["message_preview"] = msg.content[:150]
            outreach_messages[str(c.id)] = msg.content
        contacts_info.append(info)

    # Send via Telegram (with CV PDF inline)
    await send_telegram_apply_plan(
        job, pdf_path, cv_changes_summary, contacts_info, outreach_messages, easy_apply_attempted
    )

    # Also send email as backup
    send_apply_plan_email(job, contacts_with_messages, pdf_path, easy_apply_attempted)
    logger.info("=== Apply plan complete for job %d ===", job_id)
