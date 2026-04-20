"""CLI entry point for the personal assistant."""

import argparse
import asyncio
import logging
import os
import sys

from personal_assistant.config import settings
from personal_assistant.db.models import init_db


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_scrape(args):
    """Run the discovery pipeline once."""
    from personal_assistant.pipeline import run_discovery_pipeline
    from personal_assistant.db.models import get_session, User
    from sqlalchemy import select
    s = get_session()
    admin = s.execute(select(User).where(User.is_admin == True)).scalar_one_or_none()
    s.close()
    uid = admin.id if admin else 1
    asyncio.run(run_discovery_pipeline(uid))


def cmd_cv(args):
    """Generate CVs for approved jobs."""
    from personal_assistant.pipeline import run_cv_pipeline

    asyncio.run(run_cv_pipeline())


def cmd_apply(args):
    """Submit applications for CV-approved jobs."""
    from personal_assistant.pipeline import run_application_pipeline

    asyncio.run(run_application_pipeline())


def cmd_network(args):
    """Run networking pipeline for applied jobs."""
    from personal_assistant.pipeline import run_networking_pipeline

    asyncio.run(run_networking_pipeline())


def cmd_run_all(args):
    """Run all pipeline stages once."""
    from personal_assistant.pipeline import run_full_pipeline

    asyncio.run(run_full_pipeline())


def cmd_scheduler(args):
    """Start the scheduler + Telegram bot together."""
    from personal_assistant.scheduler.jobs import create_scheduler
    from personal_assistant.server.telegram_handler import build_bot_app

    bot_app = build_bot_app()

    # Register scheduler as a post-init hook on the bot's event loop
    async def post_init(application):
        scheduler = create_scheduler()
        scheduler.start()
        logging.getLogger(__name__).info("Scheduler started alongside Telegram bot")

    bot_app.post_init = post_init

    # run_polling() blocks and manages the event loop
    bot_app.run_polling()


def cmd_telegram(args):
    """Start the Telegram bot only (no scheduler)."""
    from personal_assistant.server.telegram_handler import build_bot_app

    bot_app = build_bot_app()
    bot_app.run_polling()


def cmd_dashboard(args):
    """Start the web dashboard only."""
    import uvicorn
    from personal_assistant.server.dashboard import app

    init_db()
    port = getattr(args, "port", None) or int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def cmd_all_services(args):
    """Start Telegram bot + dashboard together."""
    import threading
    import uvicorn
    from personal_assistant.server.dashboard import app as dashboard_app
    from personal_assistant.server.telegram_handler import build_bot_app

    init_db()
    port = getattr(args, "port", None) or int(os.environ.get("PORT", 8080))

    # Run dashboard in a background thread
    def run_dashboard():
        uvicorn.run(dashboard_app, host="0.0.0.0", port=port, log_level="info")

    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    logging.getLogger(__name__).info("Dashboard started on http://localhost:%d", port)

    # Run Telegram bot in main thread (blocks)
    bot_app = build_bot_app()
    bot_app.run_polling()


def cmd_init_db(args):
    """Initialize the database."""
    init_db()
    print("Database initialized.")


def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="LinkedIn Job Application Agent",
        prog="pa",
    )
    subparsers = parser.add_subparsers(dest="command")

    # scrape
    p_scrape = subparsers.add_parser("scrape", help="Run discovery pipeline")
    p_scrape.set_defaults(func=cmd_scrape)

    # cv
    p_cv = subparsers.add_parser("cv", help="Generate CVs for approved jobs")
    p_cv.set_defaults(func=cmd_cv)

    # apply
    p_apply = subparsers.add_parser("apply", help="Submit applications")
    p_apply.set_defaults(func=cmd_apply)

    # network
    p_network = subparsers.add_parser("network", help="Run networking pipeline")
    p_network.set_defaults(func=cmd_network)

    # run-all
    p_all = subparsers.add_parser("run-all", help="Run all pipelines once")
    p_all.set_defaults(func=cmd_run_all)

    # scheduler
    p_sched = subparsers.add_parser("scheduler", help="Start scheduler + Telegram bot")
    p_sched.set_defaults(func=cmd_scheduler)

    # telegram
    p_tg = subparsers.add_parser("telegram", help="Start Telegram bot only")
    p_tg.set_defaults(func=cmd_telegram)

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Start web dashboard only")
    p_dash.add_argument("--port", type=int, default=8080, help="Dashboard port (default: 8080)")
    p_dash.set_defaults(func=cmd_dashboard)

    # start (bot + dashboard together)
    p_start = subparsers.add_parser("start", help="Start Telegram bot + dashboard")
    p_start.add_argument("--port", type=int, default=8080, help="Dashboard port (default: 8080)")
    p_start.set_defaults(func=cmd_all_services)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize database")
    p_init.set_defaults(func=cmd_init_db)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Always ensure DB exists
    init_db()

    args.func(args)


if __name__ == "__main__":
    main()
