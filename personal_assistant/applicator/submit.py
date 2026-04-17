"""Application submission — Easy Apply via Playwright or email via SMTP."""

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

from personal_assistant.config import settings
from personal_assistant.db.models import Application, ApplicationMethod, Job, JobStatus
from personal_assistant.db.queries import update_job_status, get_session
from personal_assistant.scraper.anti_detect import human_delay, human_type
from personal_assistant.scraper.auth import LinkedInSession

logger = logging.getLogger(__name__)


async def submit_easy_apply(session: LinkedInSession, job: Job) -> bool:
    """Submit via LinkedIn Easy Apply. Returns True on success."""
    page = session.page
    await session.ensure_logged_in()

    try:
        await page.goto(job.job_url, wait_until="domcontentloaded")
        await human_delay(2, 4)

        # Click Easy Apply button
        apply_btn = page.locator('button:has-text("Easy Apply")').first
        if await apply_btn.count() == 0:
            logger.warning("Easy Apply button not found for job %d", job.id)
            return False

        await apply_btn.click()
        await human_delay(1, 2)

        # Handle multi-step Easy Apply modal
        max_steps = 10
        for step in range(max_steps):
            # Check for submit button (final step)
            submit_btn = page.locator(
                'button[aria-label="Submit application"], '
                'button:has-text("Submit application")'
            ).first
            if await submit_btn.count() > 0:
                await submit_btn.click()
                await human_delay(2, 3)
                logger.info("Easy Apply submitted for job %d", job.id)
                _record_application(job.id, ApplicationMethod.EASY_APPLY, True)
                return True

            # Look for Next / Review buttons
            next_btn = page.locator(
                'button[aria-label="Continue to next step"], '
                'button:has-text("Next"), '
                'button:has-text("Review")'
            ).first
            if await next_btn.count() > 0:
                await next_btn.click()
                await human_delay(1, 2)
            else:
                # Maybe there's a required field we can't fill — bail out
                logger.warning(
                    "Easy Apply stuck at step %d for job %d — may need manual intervention",
                    step, job.id,
                )
                # Close the modal
                close_btn = page.locator('button[aria-label="Dismiss"]').first
                if await close_btn.count() > 0:
                    await close_btn.click()
                return False

        logger.warning("Easy Apply exceeded max steps for job %d", job.id)
        return False

    except Exception as e:
        logger.error("Easy Apply failed for job %d: %s", job.id, e)
        return False


def send_email_application(
    to_email: str,
    job: Job,
    cover_email: str,
    cv_path: str,
) -> bool:
    """Send application via Gmail SMTP with CV attachment."""
    if not settings.gmail_address or not settings.gmail_app_password:
        logger.error("Gmail credentials not configured")
        return False

    msg = MIMEMultipart()
    msg["From"] = settings.gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Application: {job.title} — {job.company}"

    msg.attach(MIMEText(cover_email, "plain"))

    # Attach CV PDF
    cv_file = Path(cv_path)
    if cv_file.exists():
        with open(cv_file, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="pdf")
            attachment.add_header(
                "Content-Disposition", "attachment",
                filename=cv_file.name,
            )
            msg.attach(attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)
        logger.info("Email application sent for job %d to %s", job.id, to_email)
        _record_application(job.id, ApplicationMethod.EMAIL, True)
        return True
    except Exception as e:
        logger.error("Email send failed for job %d: %s", job.id, e)
        return False


def flag_for_manual_application(job: Job) -> None:
    """Mark a job as needing manual application (external site)."""
    _record_application(job.id, ApplicationMethod.MANUAL, False, notes="External site — apply manually")
    update_job_status(job.id, JobStatus.APPLIED, applied_at=datetime.utcnow())
    logger.info("Job %d flagged for manual application: %s", job.id, job.job_url)


def _record_application(
    job_id: int,
    method: ApplicationMethod,
    success: bool,
    notes: str = "",
) -> None:
    """Record an application attempt in the DB."""
    session = get_session()
    try:
        app = Application(
            job_id=job_id,
            method=method,
            submitted_at=datetime.utcnow(),
            success=success,
            notes=notes,
        )
        session.add(app)
        session.commit()
        if success:
            update_job_status(job_id, JobStatus.APPLIED, applied_at=datetime.utcnow())
    finally:
        session.close()
