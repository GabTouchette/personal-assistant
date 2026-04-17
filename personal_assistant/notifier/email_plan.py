"""Send the application plan email when a job is approved.

Email contains:
  - Full job details (title, company, location, salary, description, link)
  - How to apply (Easy Apply note or manual instructions)
  - Contacts found + drafted outreach messages per contact
  - Tailored CV PDF attached
"""

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from personal_assistant.config import settings
from personal_assistant.db.models import Contact, Job, Message

logger = logging.getLogger(__name__)

_SEP = "=" * 60
_DIV = "-" * 60


def _build_body(
    job: Job,
    contacts_with_messages: list[tuple[Contact, Message | None]],
    pdf_path: str | None,
    easy_apply_attempted: bool,
) -> str:
    # Salary
    if job.salary_min and job.salary_max:
        salary = f"${job.salary_min // 1000}K\u2013${job.salary_max // 1000}K CAD"
    elif job.salary_text:
        salary = job.salary_text
    else:
        salary = "Not listed"

    remote_tag = "Remote" if job.is_remote else "On-site/Hybrid"
    posted = job.discovered_at.strftime("%Y-%m-%d") if job.discovered_at else "N/A"

    # Apply section
    if easy_apply_attempted:
        apply_section = "\u2705 Easy Apply \u2014 already submitted automatically via LinkedIn."
    elif job.is_easy_apply:
        apply_section = (
            "\u2705 Easy Apply available on LinkedIn.\n"
            f"   Navigate to: {job.job_url or 'see link above'}"
        )
    else:
        apply_section = (
            "\U0001f4cb Manual / External application required.\n"
            f"   \u2192 {job.job_url or 'see LinkedIn'}"
        )

    # Contacts section
    if contacts_with_messages:
        contact_blocks = []
        for i, (contact, message) in enumerate(contacts_with_messages, 1):
            block = [
                f"[{i}] {contact.name}",
                f"    Title:    {contact.title or 'N/A'}",
                f"    Role:     {contact.role.value if contact.role else 'N/A'}",
                f"    LinkedIn: {contact.linkedin_url or 'N/A'}",
            ]
            if message:
                block += [
                    "",
                    "    Drafted message:",
                    "    \u250c" + "\u2500" * 50,
                    *[f"    \u2502 {line}" for line in message.body.splitlines()],
                    "    \u2514" + "\u2500" * 50,
                ]
            contact_blocks.append("\n".join(block))
        contacts_section = "\n\n".join(contact_blocks)
    else:
        contacts_section = (
            "No contacts found for this company.\n"
            "Try searching LinkedIn manually for a recruiter or engineering manager."
        )

    attachment_note = (
        "Your tailored CV is attached as a PDF."
        if pdf_path
        else "CV generation failed \u2014 check logs and re-run."
    )

    return f"""\
Hi Gabriel,

You approved Job #{job.id}. Here\u2019s your full apply plan.

{_SEP}
JOB DETAILS
{_SEP}
Title:     {job.title}
Company:   {job.company}
Location:  {job.location or 'N/A'}  ({remote_tag})
Salary:    {salary}
Score:     {job.relevance_score}/100
Found:     {posted}
Link:      {job.job_url or 'N/A'}

{_DIV}
FULL JOB DESCRIPTION
{_DIV}

{job.description or 'No description available.'}

{_SEP}
HOW TO APPLY
{_SEP}
{apply_section}

{_SEP}
CONTACTS TO REACH OUT TO
{_SEP}
{contacts_section}

{_SEP}
{attachment_note}

Good luck! \U0001f680
"""


def send_apply_plan_email(
    job: Job,
    contacts_with_messages: list[tuple[Contact, Message | None]],
    pdf_path: str | None = None,
    easy_apply_attempted: bool = False,
) -> bool:
    """Send the application plan email with tailored CV attached."""
    subject = f"[Apply Plan] {job.title} @ {job.company} \u2014 Job #{job.id}"
    body = _build_body(job, contacts_with_messages, pdf_path, easy_apply_attempted)

    msg = MIMEMultipart()
    msg["From"] = settings.gmail_address
    msg["To"] = settings.gmail_address
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if pdf_path:
        path = Path(pdf_path)
        if path.exists():
            with path.open("rb") as f:
                part = MIMEApplication(f.read(), Name=path.name)
            part["Content-Disposition"] = f'attachment; filename="{path.name}"'
            msg.attach(part)
            logger.info("Attaching CV: %s", path.name)
        else:
            logger.warning("CV PDF not found at %s \u2014 sending without attachment", pdf_path)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)
        logger.info("Apply plan email sent for job %d", job.id)
        return True
    except Exception as e:
        logger.error("Failed to send apply plan email for job %d: %s", job.id, e)
        return False
