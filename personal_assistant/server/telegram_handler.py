"""Telegram bot application — long-polling handler for inline buttons + text chat.

Runs as a persistent process alongside the scheduler.
"""

import logging
import re

import anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from personal_assistant.config import settings
from personal_assistant.analyzer.keyword_scorer import record_feedback
from personal_assistant.db.models import JobStatus
from personal_assistant.db.queries import get_contacts_for_job, get_job_by_id, get_jobs_by_status, update_job_status

logger = logging.getLogger(__name__)

_haiku = anthropic.Anthropic(api_key=settings.anthropic_api_key)

HTML = "HTML"

from personal_assistant.server.auth import get_user_by_telegram_chat_id

# Per-user state: which job they're asking about
_asking_about: dict[int, int] = {}  # chat_id -> job_id

# Conversation context for tweak / ask follow-ups
# chat_id -> {"job_id": int, "mode": "ask"|"tweakcv"|"tweakmsg", "description": str}
_conversation_ctx: dict[int, dict] = {}


def _get_user_for_chat(update: Update):
    """Return the User linked to this Telegram chat, or None if unregistered."""
    chat_id = str(update.effective_chat.id)
    return get_user_by_telegram_chat_id(chat_id)


# ── Haiku chat ────────────────────────────────────────────────────────────────

def _ask_haiku(message: str, system: str = "") -> str:
    try:
        resp = _haiku.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=system or "You are a concise job-search assistant. Keep replies under 500 characters.",
            messages=[{"role": "user", "content": message}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error("Haiku chat error: %s", e)
        return "Sorry, couldn't process that right now."


# ── /start command ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_user_for_chat(update)
    if not user:
        await update.message.reply_text(
            "\u26a0\ufe0f This Telegram account is not linked to any dashboard user.\n\n"
            "Log in to the dashboard, go to Settings \u2192 Connect Telegram, and click \u2018Link this account\u2019."
        )
        return
    await update.message.reply_text(
        f"<b>LinkedIn Job Agent</b> \u2014 <i>{user.username}</i>\n\n"
        "<b>Commands</b>\n"
        "\u2022 /status \u2014 pipeline stats\n"
        "\u2022 /pending \u2014 jobs awaiting review\n"
        "\u2022 /run \u2014 trigger discovery now\n\n"
        "I'll send job cards with <b>Apply</b> / <b>Skip</b> buttons.\n"
        "You can also type any message to chat.",
        parse_mode=HTML,
    )


# ── /status command ───────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_user_for_chat(update)
    if not user:
        await update.message.reply_text("Account not linked. Use /start for instructions.")
        return
    from personal_assistant.db.models import JobStatus

    counts = {}
    for status in JobStatus:
        jobs = get_jobs_by_status(status, user.id)
        if jobs:
            counts[status.value] = len(jobs)

    if not counts:
        await update.message.reply_text("<i>No jobs in database yet.</i> Run /run to start scraping.", parse_mode=HTML)
        return

    lines = ["<b>Pipeline Status</b>", ""]
    for status, count in counts.items():
        lines.append(f"\u2022 <b>{status}</b>: {count}")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


# ── /pending command ──────────────────────────────────────────────────────────

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_user_for_chat(update)
    if not user:
        await update.message.reply_text("Account not linked. Use /start for instructions.")
        return

    jobs = get_jobs_by_status(JobStatus.NOTIFIED, user.id)
    if not jobs:
        await update.message.reply_text("<i>No pending jobs to review.</i>", parse_mode=HTML)
        return

    lines = [f"<b>{len(jobs)} jobs awaiting review</b>", ""]
    for j in jobs[:20]:
        from html import escape
        lines.append(f"\u2022 <b>#{j.id}</b> \u2014 {escape(j.title or '')} @ <i>{escape(j.company or '')}</i> ({j.relevance_score}/100)")

    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


# ── /run command ──────────────────────────────────────────────────────────────

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_user_for_chat(update)
    if not user:
        await update.message.reply_text("Account not linked. Use /start for instructions.")
        return
    await update.message.reply_text("<b>Starting discovery pipeline...</b>\n<i>This may take a few minutes.</i>", parse_mode=HTML)

    from personal_assistant.pipeline import run_discovery_pipeline
    try:
        await run_discovery_pipeline(user.id)
        await update.message.reply_text("\u2705 <b>Discovery pipeline complete!</b>", parse_mode=HTML)
    except Exception as e:
        logger.error("Pipeline error: %s", e)
        from html import escape
        await update.message.reply_text(f"\u26a0\ufe0f <b>Pipeline failed</b>\n<pre>{escape(str(e))}</pre>", parse_mode=HTML)


# ── Callback query handler (inline buttons) ──────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    data = query.data  # e.g. "apply:42", "skip:42", "user_approve:5"
    parts = data.split(":", 1)
    if len(parts) != 2:
        await query.answer()
        return

    action, value_str = parts

    # ── User approval/rejection (admin action, no user lookup required) ──────
    if action in ("user_approve", "user_reject"):
        await query.answer()
        from personal_assistant.server.auth import approve_user, reject_user, get_user_by_id
        try:
            uid = int(value_str)
        except ValueError:
            return
        target = get_user_by_id(uid)
        if not target:
            await query.edit_message_text("User not found.")
            return
        if action == "user_approve":
            approve_user(uid)
            await query.edit_message_text(f"✅ <b>{target.username}</b> has been approved.", parse_mode="HTML")
        else:
            reject_user(uid)
            await query.edit_message_text(f"❌ <b>{target.username}</b> has been rejected.", parse_mode="HTML")
        return

    # ── Job actions — require a linked account ───────────────────────────────
    user = _get_user_for_chat(update)
    if not user:
        await query.answer("Account not linked to dashboard", show_alert=True)
        return
    await query.answer()  # dismiss the loading spinner

    try:
        job_id = int(value_str)
    except ValueError:
        return

    job = get_job_by_id(job_id, user.id)
    if not job:
        await query.edit_message_text(f"Job #{job_id} not found.")
        return

    chat_id = update.effective_chat.id

    if action == "apply":
        update_job_status(job.id, JobStatus.APPROVED)
        record_feedback(job, approved=True)

        # Delete the original notification message
        try:
            await query.message.delete()
        except Exception:
            logger.debug("Could not delete notification message for job %d", job.id)

        from html import escape
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"\u2705 <b>Job #{job.id} approved!</b>\n\n"
                f"\u2022 Tailoring your CV\n"
                f"\u2022 Researching contacts\n\n"
                f"<i>I'll send the apply plan here when ready.</i>"
            ),
            parse_mode=HTML,
        )

        context.application.create_task(
            _run_cv_plan_safe(job.id, chat_id, context),
            name=f"cv_plan_{job.id}",
        )

    elif action == "skip":
        update_job_status(job.id, JobStatus.REJECTED)
        record_feedback(job, approved=False)
        # Delete the original notification message
        try:
            await query.message.delete()
        except Exception:
            logger.debug("Could not delete notification message for job %d", job.id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u274c <b>Job #{job.id} skipped.</b>",
            parse_mode=HTML,
        )

    elif action == "ask":
        _conversation_ctx[chat_id] = {
            "job_id": job.id,
            "mode": "ask",
            "description": (job.description or "")[:2000],
        }
        from html import escape
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>Ask about Job #{job.id}</b>\n"
                f"<i>{escape(job.title or '')} @ {escape(job.company or '')}</i>\n\n"
                f"Type your question:"
            ),
            parse_mode=HTML,
        )

    elif action == "tweakcv":
        _conversation_ctx[chat_id] = {
            "job_id": job.id,
            "mode": "tweakcv",
            "description": (job.description or "")[:2000],
        }
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>Tweak CV for Job #{job.id}</b>\n"
                f"What would you like me to change on the CV? "
                f"(e.g. \"add more emphasis on Python\", \"remove the projects section\")"
            ),
            parse_mode=HTML,
        )

    elif action == "tweakmsg":
        _conversation_ctx[chat_id] = {
            "job_id": job.id,
            "mode": "tweakmsg",
            "description": (job.description or "")[:2000],
        }
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>Tweak Outreach for Job #{job.id}</b>\n"
                f"What should I change in the outreach messages? "
                f"(e.g. \"make it shorter\", \"mention my Flutter experience\")"
            ),
            parse_mode=HTML,
        )

    elif action == "easyonly":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<b>Submitting Easy Apply</b> for job #{job.id}...",
            parse_mode=HTML,
        )
        context.application.create_task(
            _easy_apply_only_safe(job.id, chat_id, context),
            name=f"easyapply_{job.id}",
        )

    elif action == "applyall":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<b>Applying to all channels</b> for job #{job.id}...",
            parse_mode=HTML,
        )
        context.application.create_task(
            _easy_apply_only_safe(job.id, chat_id, context),
            name=f"applyall_{job.id}",
        )
        if get_contacts_for_job(job.id):
            context.application.create_task(
                _send_all_outreach_safe(job.id, chat_id, context),
                name=f"outreach_{job.id}",
            )

    elif action == "sendall":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<b>Sending all outreach messages</b> for job #{job.id}...",
            parse_mode=HTML,
        )
        context.application.create_task(
            _send_all_outreach_safe(job.id, chat_id, context),
            name=f"outreach_{job.id}",
        )

    elif action == "done":
        update_job_status(job.id, JobStatus.APPLIED)

        # Delete all plan messages for this job
        from personal_assistant.notifier.telegram import _plan_message_ids
        plan_ids = _plan_message_ids.pop(job.id, [])
        for mid in plan_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                logger.debug("Could not delete plan message %d for job %d", mid, job.id)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u2705 <b>Job #{job.id} marked as applied!</b> Good luck!",
            parse_mode=HTML,
        )


# ── Background tasks ─────────────────────────────────────────────────────────

async def _run_cv_plan_safe(job_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run CV + plan pipeline and send results via Telegram."""
    from personal_assistant.pipeline import run_cv_and_email_plan

    try:
        await run_cv_and_email_plan(job_id)
    except Exception as e:
        logger.error("CV+plan failed for job %d: %s", job_id, e)
        from html import escape
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u26a0\ufe0f <b>Apply plan failed</b> for job #{job_id}\n<pre>{escape(str(e))}</pre>",
            parse_mode=HTML,
        )


async def _send_all_outreach_safe(job_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send all drafted outreach messages for a job."""
    from personal_assistant.db.queries import get_contacts_for_job, get_messages_for_contact

    try:
        contacts = get_contacts_for_job(job_id)
        sent = 0
        for contact in contacts:
            msgs = get_messages_for_contact(contact.id)
            if msgs:
                # TODO: actually send via LinkedIn/email when infrastructure is ready
                sent += 1

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u2705 Queued <b>{sent}</b> outreach message(s) for job #{job_id}.",
            parse_mode=HTML,
        )
    except Exception as e:
        logger.error("Outreach send failed for job %d: %s", job_id, e)
        from html import escape
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u26a0\ufe0f <b>Outreach failed</b> for job #{job_id}\n<pre>{escape(str(e))}</pre>",
            parse_mode=HTML,
        )


async def _easy_apply_only_safe(job_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit Easy Apply for a job."""
    from personal_assistant.applicator.submit import submit_easy_apply
    from personal_assistant.db.queries import mark_job_applied
    from personal_assistant.scraper.auth import LinkedInSession

    try:
        job = get_job_by_id(job_id)
        if not job or not job.is_easy_apply:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\u26a0\ufe0f Job #{job_id} is not eligible for Easy Apply.",
                parse_mode=HTML,
            )
            return

        session = LinkedInSession()
        await session.start()
        try:
            success = await submit_easy_apply(session, job)
        finally:
            await session.close()

        if success:
            mark_job_applied(job_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\u2705 <b>Easy Apply submitted</b> for job #{job_id}!",
                parse_mode=HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\u26a0\ufe0f Easy Apply could not be completed for job #{job_id}.",
                parse_mode=HTML,
            )
    except Exception as e:
        logger.error("Easy Apply failed for job %d: %s", job_id, e)
        from html import escape
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\u26a0\ufe0f <b>Easy Apply failed</b> for job #{job_id}\n<pre>{escape(str(e))}</pre>",
            parse_mode=HTML,
        )


# ── Free-text handler ─────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_user_for_chat(update)
    if not user:
        return  # silently ignore unknown accounts

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    # If they have an active conversation context (ask / tweakcv / tweakmsg)
    if chat_id in _conversation_ctx:
        ctx = _conversation_ctx[chat_id]
        job_id = ctx["job_id"]
        mode = ctx["mode"]
        desc = ctx.get("description", "")
        job = get_job_by_id(job_id, user.id)

        if not job:
            _conversation_ctx.pop(chat_id, None)
            await update.message.reply_text(f"Job #{job_id} not found.")
            return

        if mode == "ask":
            system = (
                f"Answer questions about this job concisely.\n"
                f"Job #{job.id}: {job.title} at {job.company}, {job.location}, "
                f"salary: {job.salary_text or 'unknown'}.\n"
                f"Description: {desc}"
            )
            reply = _ask_haiku(text, system)
            # Keep context active for follow-up questions
            await update.message.reply_text(reply)
            return

        elif mode == "tweakcv":
            _conversation_ctx.pop(chat_id, None)
            system = (
                f"You are a CV improvement assistant. The user wants to tweak their CV "
                f"for this job:\nJob: {job.title} at {job.company}\n"
                f"Description: {desc}\n\n"
                f"Acknowledge the requested change and confirm you'll regenerate the CV. "
                f"Be concise (2-3 sentences)."
            )
            reply = _ask_haiku(text, system)
            await update.message.reply_text(reply)
            # Trigger CV re-generation in background
            context.application.create_task(
                _run_cv_plan_safe(job_id, chat_id, context),
                name=f"tweakcv_{job_id}",
            )
            return

        elif mode == "tweakmsg":
            _conversation_ctx.pop(chat_id, None)
            system = (
                f"You are an outreach message assistant. The user wants to tweak "
                f"outreach messages for:\nJob: {job.title} at {job.company}\n"
                f"Description: {desc}\n\n"
                f"Acknowledge the requested change. Be concise (2-3 sentences)."
            )
            reply = _ask_haiku(text, system)
            await update.message.reply_text(reply)
            # TODO: re-draft outreach with user's feedback when infra supports it
            return

    # General Haiku chat
    reply = _ask_haiku(text)
    await update.message.reply_text(reply)


# ── Bot builder ───────────────────────────────────────────────────────────────

def build_bot_app() -> Application:
    """Build and return the Telegram bot Application (call .run_polling() to start)."""
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("run", cmd_run))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
