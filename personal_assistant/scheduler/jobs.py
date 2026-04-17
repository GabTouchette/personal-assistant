"""APScheduler — runs the full discovery pipeline once daily at 08:00."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from personal_assistant.pipeline import run_discovery_pipeline

logger = logging.getLogger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    """Single daily job: scrape + analyze + Telegram notify at 08:00."""
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_discovery_pipeline,
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
