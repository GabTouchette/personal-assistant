"""Outreach message drafting and delivery — LinkedIn messages and emails."""

import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import anthropic

from personal_assistant.config import settings
from personal_assistant.cv.tailoring import detect_job_language
from personal_assistant.db.models import Contact, Job, Message, MessageStatus
from personal_assistant.db.queries import add_message, update_message_status
from personal_assistant.scraper.anti_detect import human_delay, human_type
from personal_assistant.scraper.auth import LinkedInSession

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

_HIGHLIGHTS_EN = (
    "Lead Software Developer at a medical device startup (Flutter, Python, Azure, Kubernetes); "
    "Full Stack Developer with Vue.js and Django; B.Eng. Software Engineering, Polytechnique Montréal"
)
_HIGHLIGHTS_FR = (
    "Développeur Logiciel Principal dans une startup de dispositifs médicaux (Flutter, Python, Azure, Kubernetes) ; "
    "Développeur Full Stack avec Vue.js et Django ; B.Ing. Génie logiciel, Polytechnique Montréal"
)

CONNECTION_NOTE_EN = """\
Write a short LinkedIn connection request note (max 280 characters).

Context: {candidate_name} applied for {job_title} at {company} and is reaching out to {contact_name} ({contact_title}).
Relevant background: {highlights}

Rules:
1. Genuine and specific — reference the role.
2. No filler like "I'd love to connect".
3. Professional but warm.
4. Under 280 characters total.
5. Never call the candidate an "engineer" — use "developer".

Return ONLY the note text.
"""

CONNECTION_NOTE_FR = """\
Rédige une courte note de demande de connexion LinkedIn (280 caractères max).

Contexte : {candidate_name} a postulé pour {job_title} chez {company} et contacte {contact_name} ({contact_title}).
Parcours pertinent : {highlights}

Règles :
1. Authentique et spécifique — mentionne le poste.
2. Pas de remplissage comme «j'aimerais me connecter».
3. Professionnel mais chaleureux.
4. 280 caractères maximum au total.
5. Ne jamais appeler le candidat «ingénieur» — utiliser «développeur».

Retourne UNIQUEMENT le texte de la note.
"""

COLD_EMAIL_EN = """\
Write a short cold outreach email to a contact at a company the candidate applied to.

Candidate: {candidate_name}  |  Role: {job_title} at {company}
Contact: {contact_name}, {contact_title}
Background: {highlights}

Rules:
1. Subject line on first line, blank line, then body.
2. 2–3 short paragraphs max.
3. Reference the specific role and something about the company.
4. Professional, concise, compelling.
5. Clear ask (coffee chat, referral, or team info).
6. Never call the candidate an "engineer" — use "developer".

Return subject line + body only.
"""

COLD_EMAIL_FR = """\
Rédige un courriel de prospection court pour un contact dans une entreprise où le candidat a postulé.

Candidat : {candidate_name}  |  Poste : {job_title} chez {company}
Contact : {contact_name}, {contact_title}
Parcours : {highlights}

Règles :
1. Ligne d'objet en première ligne, ligne vide, puis corps du message.
2. 2–3 courts paragraphes maximum.
3. Mentionne le poste spécifique et quelque chose sur l'entreprise.
4. Professionnel, concis, percutant.
5. Demande claire (café virtuel, recommandation ou info sur l'équipe).
6. Ne jamais appeler le candidat «ingénieur» — utiliser «développeur».

Retourne uniquement l'objet et le corps du message.
"""


def draft_outreach(job: Job, contact: Contact, channel: str = "linkedin") -> Message | None:
    """Draft an outreach message in the job's language (auto-detected)."""
    lang = detect_job_language(job)
    highlights = _HIGHLIGHTS_FR if lang == "fr" else _HIGHLIGHTS_EN

    if channel == "linkedin":
        template = CONNECTION_NOTE_FR if lang == "fr" else CONNECTION_NOTE_EN
    else:
        template = COLD_EMAIL_FR if lang == "fr" else COLD_EMAIL_EN

    prompt = template.format(
        candidate_name="Gabriel Touchette",
        job_title=job.title,
        company=job.company,
        contact_name=contact.name,
        contact_title=contact.title or ("membre de l'équipe" if lang == "fr" else "team member"),
        highlights=highlights,
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        subject = ""
        body = text
        if channel == "email" and "\n" in text:
            subject, body = text.split("\n", 1)
            body = body.strip()

        msg = add_message(
            contact_id=contact.id,
            channel=channel,
            subject=subject,
            body=body,
            status=MessageStatus.DRAFTED,
        )
        logger.info("Drafted %s message for contact %d", channel, contact.id)
        return msg

    except Exception as e:
        logger.error("Outreach drafting failed: %s", e)
        return None


async def send_linkedin_connection(
    session: LinkedInSession,
    contact: Contact,
    message: Message,
) -> bool:
    """Send a LinkedIn connection request with a note via Playwright."""
    if not contact.linkedin_url:
        logger.warning("No LinkedIn URL for contact %d", contact.id)
        return False

    page = session.page
    await session.ensure_logged_in()

    try:
        await page.goto(contact.linkedin_url, wait_until="domcontentloaded")
        await human_delay(2, 4)

        # Look for Connect button
        connect_btn = page.locator('button:has-text("Connect")').first
        if await connect_btn.count() == 0:
            # Maybe it's under "More"
            more_btn = page.locator('button:has-text("More")').first
            if await more_btn.count() > 0:
                await more_btn.click()
                await human_delay(0.5, 1)
                connect_btn = page.locator('button:has-text("Connect")').first

        if await connect_btn.count() == 0:
            logger.warning("Connect button not found for contact %d", contact.id)
            return False

        await connect_btn.click()
        await human_delay(1, 2)

        # Click "Add a note"
        add_note_btn = page.locator('button:has-text("Add a note")').first
        if await add_note_btn.count() > 0:
            await add_note_btn.click()
            await human_delay(0.5, 1)

            # Type the note
            note_input = page.locator('textarea[name="message"], textarea#custom-message')
            await human_type(page, 'textarea[name="message"], textarea#custom-message', message.body[:280])
            await human_delay(0.5, 1)

        # Send
        send_btn = page.locator('button:has-text("Send")').first
        await send_btn.click()
        await human_delay(1, 2)

        update_message_status(message.id, MessageStatus.SENT, sent_at=datetime.utcnow())
        logger.info("LinkedIn connection sent to %s", contact.name)
        return True

    except Exception as e:
        logger.error("LinkedIn connection failed for contact %d: %s", contact.id, e)
        return False


def send_cold_email(contact: Contact, message: Message) -> bool:
    """Send a cold email via Gmail SMTP."""
    if not contact.email:
        logger.warning("No email for contact %d", contact.id)
        return False
    if not settings.gmail_address or not settings.gmail_app_password:
        logger.error("Gmail credentials not configured")
        return False

    msg = MIMEText(message.body, "plain")
    msg["From"] = settings.gmail_address
    msg["To"] = contact.email
    msg["Subject"] = message.subject or f"Regarding the {contact.company} opportunity"

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)
        update_message_status(message.id, MessageStatus.SENT, sent_at=datetime.utcnow())
        logger.info("Cold email sent to %s (%s)", contact.name, contact.email)
        return True
    except Exception as e:
        logger.error("Email send failed for contact %d: %s", contact.id, e)
        return False
