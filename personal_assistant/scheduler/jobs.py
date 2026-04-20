"""APScheduler — runs the full discovery pipeline once daily at 08:00."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from personal_assistant.pipeline import run_discovery_pipeline

logger = logging.getLogger(__name__)


def _get_admin_user_id() -> int:
    """Return the first admin user's ID (used by scheduled jobs)."""
    from personal_assistant.db.models import get_session, User
    from sqlalchemy import select
    s = get_session()
    try:
        admin = s.execute(select(User).where(User.is_admin == True)).scalar_one_or_none()
        return admin.id if admin else 1
    finally:
        s.close()


async def _run_discovery_for_admin() -> None:
    await run_discovery_pipeline(_get_admin_user_id())


def create_scheduler() -> AsyncIOScheduler:
    """Single daily job: scrape + analyze + Telegram notify at 08:00."""
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        _run_discovery_for_admin,
        "cron",
        hour=8,
        minute=0,
        id="daily_discovery",
        name="Daily Job Discovery (08:00)",
        max_instances=1,
        misfire_grace_time=3600,  # fire within 1h if process was down at 08:00
    )

    logger.info("Scheduler: daily discovery registered at 08:00")
    return scheduler
