"""FastAPI dashboard — Kanban board for job application tracking."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from personal_assistant.db.models import JobStatus, init_db
from personal_assistant.db.queries import (
    delete_job,
    get_all_jobs,
    get_job_by_id,
    get_job_detail,
    update_job_notes,
    update_job_status,
)
from personal_assistant.server.auth import (
    _COOKIE_NAME,
    approve_user,
    create_session_cookie,
    create_user,
    get_current_user_from_request,
    get_pending_users,
    get_user_by_username,
    reject_user,
    set_user_telegram_chat_id,
    user_count,
    verify_password,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Job Dashboard")

# ── Start Telegram bot alongside dashboard ────────────────────────────────────
_bot_task = None

@app.on_event("startup")
async def _start_telegram_bot():
    """Launch the Telegram bot polling in a background task so inline buttons work."""
    global _bot_task
    from personal_assistant.config import settings
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.info("Telegram not configured — skipping bot startup")
        return
    try:
        from personal_assistant.server.telegram_handler import build_bot_app
        bot_app = build_bot_app()
        # Initialize the bot application
        await bot_app.initialize()
        await bot_app.start()
        # Start polling in a background task (non-blocking)
        _bot_task = asyncio.create_task(
            bot_app.updater.start_polling(drop_pending_updates=True)
        )
        logger.info("Telegram bot polling started alongside dashboard")
    except Exception:
        logger.exception("Failed to start Telegram bot — buttons won't work")

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


# ── Auth middleware ────────────────────────────────────────────────────────────

_PUBLIC_PATHS = {"/login", "/auth/login", "/auth/register", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/output/"):
            return await call_next(request)
        user = get_current_user_from_request(request)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        request.state.user = user
        return await call_next(request)


app.add_middleware(AuthMiddleware)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # If already logged in, redirect to dashboard
    user = get_current_user_from_request(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@app.post("/auth/login")
async def auth_login(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return JSONResponse({"ok": False, "error": "Username and password required"})
    user = get_user_by_username(username)
    if not user or not verify_password(password, user.password_hash):
        return JSONResponse({"ok": False, "error": "Invalid credentials"})
    if not user.is_approved:
        return JSONResponse({"ok": False, "error": "Your account is pending admin approval"})
    response = JSONResponse({"ok": True})
    response.set_cookie(_COOKIE_NAME, create_session_cookie(user.id),
                        max_age=60*60*24*30, httponly=True, samesite="lax")
    return response


@app.post("/auth/register")
async def auth_register(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or len(username) < 3:
        return JSONResponse({"ok": False, "error": "Username must be at least 3 characters"})
    if len(password) < 6:
        return JSONResponse({"ok": False, "error": "Password must be at least 6 characters"})
    existing = get_user_by_username(username)
    if existing:
        return JSONResponse({"ok": False, "error": "Username already taken"})

    # First user ever is auto-approved + admin
    is_first = user_count() == 0
    user = create_user(username, password, is_approved=is_first, is_admin=is_first)

    if is_first:
        # Auto-login the first user
        response = JSONResponse({"ok": True, "auto_approved": True})
        response.set_cookie(_COOKIE_NAME, create_session_cookie(user.id),
                            max_age=60*60*24*30, httponly=True, samesite="lax")
        return response

    # Notify admin via Telegram
    try:
        from personal_assistant.config import settings
        if settings.telegram_bot_token and settings.telegram_chat_id:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
            bot = Bot(token=settings.telegram_bot_token)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"user_approve:{user.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"user_reject:{user.id}"),
            ]])
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=f"🔔 <b>New account request</b>\n\nUsername: <code>{username}</code>\n\nApprove this user?",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.warning("Failed to send registration notification: %s", e)

    return JSONResponse({"ok": True})


@app.get("/auth/logout")
async def auth_logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(_COOKIE_NAME)
    return response


# Serve tailored CV PDFs
_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
if _OUTPUT_DIR.exists():
    app.mount("/output", StaticFiles(directory=str(_OUTPUT_DIR)), name="output")

# Source CV files
_CV_DIR = Path(__file__).resolve().parent.parent / "cv"

# User preferences — per-user JSON files
_OUTPUT_BASE = Path(__file__).resolve().parent.parent.parent / "output"


def _prefs_path(user_id: int) -> Path:
    return _OUTPUT_BASE / f"user_preferences_{user_id}.json"


def _load_prefs(user_id: int) -> dict | None:
    p = _prefs_path(user_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _save_prefs(prefs: dict, user_id: int) -> None:
    p = _prefs_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prefs, indent=2, ensure_ascii=False))


# ── Kanban column mapping ────────────────────────────────────────────────────

KANBAN_COLUMNS = [
    {
        "id": "pending",
        "label": "Pending Review",
        "statuses": ["notified"],
        "color": "#3b82f6",
        "primary_status": "notified",
    },
    {
        "id": "in_progress",
        "label": "In Progress",
        "statuses": ["approved", "cv_generated", "cv_approved"],
        "color": "#f59e0b",
        "primary_status": "approved",
    },
    {
        "id": "applied",
        "label": "Applied",
        "statuses": ["applied", "networking_done"],
        "color": "#8b5cf6",
        "primary_status": "applied",
    },
    {
        "id": "response",
        "label": "Response",
        "statuses": ["response_received"],
        "color": "#06b6d4",
        "primary_status": "response_received",
    },
    {
        "id": "interview",
        "label": "Interview",
        "statuses": ["interview_scheduled", "interview_done", "second_interview"],
        "color": "#10b981",
        "primary_status": "interview_scheduled",
    },
    {
        "id": "closed",
        "label": "Offer / Closed",
        "statuses": ["offer", "hired", "denied", "withdrawn"],
        "color": "#ef4444",
        "primary_status": "offer",
    },
]

COLLAPSED_STATUSES = {"discovered", "summarized", "rejected", "failed"}

# Human-friendly status labels
STATUS_LABELS = {s.value: s.value.replace("_", " ").title() for s in JobStatus}

# Column IDs for manual update dropdown (matches kanban columns)
COLUMN_STATUSES = [
    {"column_id": col["id"], "label": col["label"], "status": col["primary_status"]}
    for col in KANBAN_COLUMNS
]


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    init_db()
    user = request.state.user
    jobs = get_all_jobs(user.id)

    # Build columns
    columns_data = []
    for col in KANBAN_COLUMNS:
        col_jobs = [j for j in jobs if j.status and j.status.value in col["statuses"]]
        columns_data.append({**col, "jobs": col_jobs})

    collapsed_jobs = [j for j in jobs if j.status and j.status.value in COLLAPSED_STATUSES]

    # Stats
    total = len(jobs)
    applied = sum(1 for j in jobs if j.status and j.status.value in (
        "applied", "networking_done", "response_received",
        "interview_scheduled", "interview_done", "second_interview",
        "offer", "hired", "denied", "withdrawn",
    ))
    interviews = sum(1 for j in jobs if j.status and j.status.value in (
        "interview_scheduled", "interview_done", "second_interview",
    ))
    responses = sum(1 for j in jobs if j.status and j.status.value in (
        "response_received", "interview_scheduled", "interview_done",
        "second_interview", "offer", "hired",
    ))

    # List source CV files (the user's real base CVs, not tailored versions)
    cv_files = []
    if _CV_DIR.exists():
        for f in sorted(_CV_DIR.glob("base_cv*.yaml")):
            label = "CV (English)" if f.name == "base_cv.yaml" else "CV (French)" if "_fr" in f.name else f.stem
            cv_files.append({"name": label, "filename": f.name, "path": f"/api/cv-source/{f.name}"})

    prefs = _load_prefs(user.id)

    # Connection status
    linkedin_connected = _is_linkedin_connected()
    telegram_connected = _is_telegram_connected(user)
    gmail_connected = _is_gmail_connected()

    return templates.TemplateResponse(request, "dashboard.html", {
        "columns": columns_data,
        "collapsed_jobs": collapsed_jobs,
        "stats": {
            "total": total,
            "applied": applied,
            "interviews": interviews,
            "responses": responses,
            "response_rate": round(responses / applied * 100) if applied else 0,
        },
        "status_labels": STATUS_LABELS,
        "column_statuses": COLUMN_STATUSES,
        "cv_files": cv_files,
        "has_prefs": prefs is not None,
        "prefs": prefs or {},
        "linkedin_connected": linkedin_connected,
        "telegram_connected": telegram_connected,
        "gmail_connected": gmail_connected,
        "current_username": user.username,
    })


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: int, request: Request):
    user = request.state.user
    detail = get_job_detail(job_id, user.id)
    if not detail:
        return JSONResponse({"error": "not found"}, status_code=404)
    return detail


@app.post("/api/jobs/{job_id}/status")
async def api_update_status(job_id: int, request: Request):
    user = request.state.user
    if not get_job_by_id(job_id, user.id):
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json()
    new_status = data.get("status")
    interview_date = data.get("interview_date")

    if new_status not in [s.value for s in JobStatus]:
        return JSONResponse({"error": "invalid status"}, status_code=400)

    extra = {}
    if interview_date:
        extra["interview_date"] = datetime.fromisoformat(interview_date)

    update_job_status(job_id, JobStatus(new_status), **extra)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/notes")
async def api_update_notes(job_id: int, request: Request):
    user = request.state.user
    if not get_job_by_id(job_id, user.id):
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json()
    notes = data.get("notes", "")
    update_job_notes(job_id, notes)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/cv")
async def api_download_cv(job_id: int, request: Request):
    user = request.state.user
    job = get_job_by_id(job_id, user.id)
    if not job or not job.tailored_cv_path:
        return JSONResponse({"error": "no CV"}, status_code=404)
    path = Path(job.tailored_cv_path)
    if not path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.post("/api/jobs/bulk-status")
async def api_bulk_status(request: Request):
    """Move multiple jobs to a new status (e.g. send to review)."""
    user = request.state.user
    data = await request.json()
    job_ids = data.get("job_ids", [])
    new_status = data.get("status")
    if new_status not in [s.value for s in JobStatus]:
        return JSONResponse({"error": "invalid status"}, status_code=400)
    for jid in job_ids:
        if get_job_by_id(int(jid), user.id):
            update_job_status(int(jid), JobStatus(new_status))
    return {"ok": True, "count": len(job_ids)}


# ── Source CV endpoints ────────────────────────────────────────────────────────

@app.get("/api/cv-source/{filename}")
async def api_get_cv_source(filename: str):
    """Return source CV YAML content."""
    # Sanitise: only allow base_cv*.yaml
    if not filename.startswith("base_cv") or not filename.endswith(".yaml"):
        return JSONResponse({"error": "invalid file"}, status_code=400)
    path = _CV_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"filename": filename, "content": path.read_text(encoding="utf-8")})


@app.post("/api/cv-source/{filename}")
async def api_save_cv_source(filename: str, request: Request):
    """Save updated source CV YAML content."""
    if not filename.startswith("base_cv") or not filename.endswith(".yaml"):
        return JSONResponse({"error": "invalid file"}, status_code=400)
    path = _CV_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json()
    content = data.get("content", "")
    path.write_text(content, encoding="utf-8")
    return {"ok": True}


# ── Delete job endpoint ───────────────────────────────────────────────────────

@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: int, request: Request):
    """Permanently delete a job from the database."""
    user = request.state.user
    deleted = delete_job(job_id, user.id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


# ── Preferences API ──────────────────────────────────────────────────────────

@app.get("/api/cv-extract-prefs")
async def api_cv_extract_prefs():
    """Parse the English base CV and extract suggested preference fields."""
    import yaml
    cv_path = _CV_DIR / "base_cv.yaml"
    if not cv_path.exists():
        return JSONResponse({"error": "base_cv.yaml not found"}, status_code=404)
    try:
        cv = yaml.safe_load(cv_path.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"YAML parse error: {e}"}, status_code=400)

    # Job titles from experience (deduplicated, preserving order)
    titles = []
    for exp in cv.get("experience", []):
        t = exp.get("title", "").strip()
        if t and t not in titles:
            titles.append(t)
    # Top-level title too
    top_title = cv.get("title", "").strip()
    if top_title and top_title not in titles:
        titles.insert(0, top_title)

    # Technologies from skills section
    technologies = []
    for cat in cv.get("skills", []):
        cat_name = (cat.get("category") or "").lower()
        if cat_name in ("spoken languages", "langues", "languages"):
            continue  # skip spoken languages, not tech skills
        for tech in cat.get("technologies", []):
            cleaned = tech.strip()
            if cleaned and cleaned not in technologies:
                technologies.append(cleaned)

    # Domains from "Domain Expertise" category and experience bullets
    domains = []
    _DOMAIN_MAP = {
        "healthcare": "Healthcare", "health": "Healthcare", "medical": "Healthcare",
        "ophthalmology": "Healthcare", "biotech": "Biotech", "pharma": "Pharma",
        "fintech": "Fintech", "finance": "Fintech",
        "edtech": "EdTech", "education": "EdTech",
        "gaming": "Gaming", "saas": "SaaS",
        "e-commerce": "E-commerce", "ecommerce": "E-commerce",
        "cybersecurity": "Cybersecurity", "security": "Cybersecurity",
        "logistics": "Logistics", "automotive": "Automotive",
        "aerospace": "Aerospace", "manufacturing": "Manufacturing",
        "ai": "AI / ML", "machine learning": "AI / ML", "ml": "AI / ML",
    }
    # Scan domain expertise skills
    for cat in cv.get("skills", []):
        cat_name = (cat.get("category") or "").lower()
        if "domain" in cat_name or "expertise" in cat_name:
            for tech in cat.get("technologies", []):
                for key, label in _DOMAIN_MAP.items():
                    if key in tech.lower() and label not in domains:
                        domains.append(label)
    # Scan experience bullets too
    for exp in cv.get("experience", []):
        for bullet in exp.get("bullets", []):
            bl = bullet.lower()
            for key, label in _DOMAIN_MAP.items():
                if key in bl and label not in domains:
                    domains.append(label)

    # Location
    location = cv.get("location", "")
    # Extract city only (before the first comma)
    home_city = location.split(",")[0].strip() if location else ""

    # Past companies as potential targets
    companies = []
    for exp in cv.get("experience", []):
        c = exp.get("company", "").strip()
        if c and c not in companies:
            companies.append(c)

    return {
        "desired_titles": titles,
        "technologies": technologies,
        "domains": domains,
        "home_city": home_city,
        "companies": companies,
    }

@app.get("/api/preferences")
async def api_get_preferences(request: Request):
    user = request.state.user
    prefs = _load_prefs(user.id)
    if not prefs:
        return JSONResponse({"exists": False})
    return {"exists": True, **prefs}


@app.post("/api/preferences")
async def api_save_preferences(request: Request):
    user = request.state.user
    data = await request.json()
    _save_prefs(data, user.id)

    # Update keyword scorer weights from preference weights
    _apply_prefs_to_scorer(data)

    return {"ok": True}


def _apply_prefs_to_scorer(prefs: dict) -> None:
    """Apply user preference weights to the keyword scorer.

    The keyword scorer now reads preferences directly from the JSON file
    at scoring time, so we only need to sync runtime config values and
    reset the saved weights to defaults so the scorer can rebuild them
    from scratch with the new preferences.
    """
    from personal_assistant.analyzer.keyword_scorer import _DEFAULT_WEIGHTS, _save_weights

    # Reset saved weights to defaults — the scorer will inject user prefs at load time
    import json
    clean = json.loads(json.dumps(_DEFAULT_WEIGHTS))

    # Preserve learned adjustments and blocked companies from current weights
    from personal_assistant.analyzer.keyword_scorer import _load_weights as _raw_load
    # Load raw file (not the merged version) to get learned data
    from personal_assistant.analyzer.keyword_scorer import WEIGHTS_PATH
    if WEIGHTS_PATH.exists():
        try:
            with open(WEIGHTS_PATH) as f:
                old = json.load(f)
            for key in ("learned_boosts", "learned_penalties", "blocked_companies", "company_reject_count"):
                if key in old:
                    clean[key] = old[key]
        except Exception:
            pass

    _save_weights(clean)

    # Sync home_city and max_experience to runtime config
    from personal_assistant.config import settings
    if "home_city" in prefs:
        settings.home_city = prefs["home_city"]
    if "max_experience_years" in prefs:
        settings.max_experience_years = int(prefs["max_experience_years"])


# ── Connection status helpers ─────────────────────────────────────────────────

def _is_linkedin_connected() -> bool:
    """Check if LinkedIn session cookies exist."""
    from personal_assistant.config import settings
    storage = Path(settings.browser_data_dir) / "storage_state.json"
    return storage.exists() and storage.stat().st_size > 100


def _is_telegram_connected(user=None) -> bool:
    """Check if Telegram is configured: bot token globally + chat_id for this user."""
    from personal_assistant.config import settings
    if not settings.telegram_bot_token:
        return False
    if user is not None:
        return bool(user.telegram_chat_id)
    # Fallback: check global chat ID (legacy)
    return bool(settings.telegram_chat_id)


def _is_gmail_connected() -> bool:
    """Check if Gmail address & app password are configured."""
    from personal_assistant.config import settings
    return bool(settings.gmail_address) and bool(settings.gmail_app_password)


# ── LinkedIn connect endpoint ─────────────────────────────────────────────────

@app.post("/api/connect/linkedin")
async def api_connect_linkedin():
    """Launch headless=False browser for interactive LinkedIn login."""
    try:
        from personal_assistant.scraper.auth import LinkedInSession
        session = await LinkedInSession().start()
        logged_in = await session.is_logged_in()
        if not logged_in:
            await session.login()
            logged_in = await session.is_logged_in()
        await session.close()
        return {"ok": logged_in, "message": "Connected" if logged_in else "Login required — check browser window"}
    except Exception as e:
        logger.exception("LinkedIn connect failed")
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


# ── Telegram setup endpoint ──────────────────────────────────────────────────

@app.post("/api/connect/telegram")
async def api_connect_telegram(request: Request):
    """Save Telegram bot token & chat ID, test the connection."""
    data = await request.json()
    bot_token = data.get("bot_token", "").strip()
    chat_id = data.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        return JSONResponse({"ok": False, "message": "Both bot token and chat ID are required"}, status_code=400)

    # Write to .env file
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    # Update or add the keys
    updated = {"TELEGRAM_BOT_TOKEN": bot_token, "TELEGRAM_CHAT_ID": chat_id}
    found_keys = set()
    for i, line in enumerate(env_lines):
        for key in updated:
            if line.startswith(f"{key}="):
                env_lines[i] = f"{key}={updated[key]}"
                found_keys.add(key)
    for key in updated:
        if key not in found_keys:
            env_lines.append(f"{key}={updated[key]}")

    env_path.write_text("\n".join(env_lines) + "\n")

    # Update runtime settings
    from personal_assistant.config import settings
    os.environ["TELEGRAM_BOT_TOKEN"] = bot_token
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    settings.telegram_bot_token = bot_token
    settings.telegram_chat_id = chat_id

    # Test connection
    try:
        from telegram import Bot
        bot = Bot(token=bot_token)
        await bot.send_message(chat_id=int(chat_id), text="✅ Dashboard connected successfully!")
        return {"ok": True, "message": "Connected — check your Telegram!"}
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Saved but test failed: {e}"}, status_code=500)


@app.get("/api/connect/status")
async def api_connection_status(request: Request):
    user = request.state.user
    return {
        "linkedin": _is_linkedin_connected(),
        "telegram": _is_telegram_connected(user),
        "gmail": _is_gmail_connected(),
    }


# ── Gmail connect endpoint ────────────────────────────────────────────────────

@app.post("/api/connect/gmail")
async def api_connect_gmail(request: Request):
    """Save Gmail address & app password, test SMTP connection."""
    data = await request.json()
    address = data.get("address", "").strip()
    app_password = data.get("app_password", "").strip()

    if not address or not app_password:
        return JSONResponse({"ok": False, "message": "Both email and app password are required"}, status_code=400)

    # Write to .env file
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    updated = {"GMAIL_ADDRESS": address, "GMAIL_APP_PASSWORD": app_password}
    found_keys = set()
    for i, line in enumerate(env_lines):
        for key in updated:
            if line.startswith(f"{key}="):
                env_lines[i] = f"{key}={updated[key]}"
                found_keys.add(key)
    for key in updated:
        if key not in found_keys:
            env_lines.append(f"{key}={updated[key]}")

    env_path.write_text("\n".join(env_lines) + "\n")

    # Update runtime settings
    from personal_assistant.config import settings
    os.environ["GMAIL_ADDRESS"] = address
    os.environ["GMAIL_APP_PASSWORD"] = app_password
    settings.gmail_address = address
    settings.gmail_app_password = app_password

    # Test SMTP connection
    try:
        import smtplib
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(address, app_password)
        return {"ok": True, "message": "Gmail connected!"}
    except Exception as e:
        return {"ok": True, "message": f"Saved but SMTP test failed: {e}"}


# ── Pipeline trigger endpoint ────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/run-plan")
async def api_run_plan(job_id: int, request: Request):
    """Trigger the CV + outreach pipeline for a job (like Telegram Apply)."""
    user = request.state.user
    job = get_job_by_id(job_id, user.id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        from personal_assistant.pipeline import run_cv_and_email_plan
        await run_cv_and_email_plan(job_id)
        return {"ok": True}
    except Exception as e:
        logger.exception("Pipeline failed for job %d", job_id)
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


# ── Send to review via Telegram ──────────────────────────────────────────────

@app.post("/api/jobs/send-to-review")
async def api_send_to_review(request: Request):
    """Move jobs to notified and send Telegram notifications."""
    user = request.state.user
    data = await request.json()
    job_ids = data.get("job_ids", [])

    if not job_ids:
        return JSONResponse({"ok": False, "message": "No jobs selected"}, status_code=400)

    # Only update jobs owned by this user
    for jid in job_ids:
        if get_job_by_id(int(jid), user.id):
            update_job_status(int(jid), JobStatus.NOTIFIED)

    # Send via Telegram if connected
    if _is_telegram_connected():
        try:
            from personal_assistant.notifier.telegram import send_job_notification
            for jid in job_ids:
                job = get_job_by_id(int(jid), user.id)
                if job:
                    await send_job_notification(job)
        except Exception:
            logger.exception("Failed to send Telegram notifications")
            return {"ok": True, "count": len(job_ids), "telegram": False, "message": "Status updated but Telegram send failed"}

    return {"ok": True, "count": len(job_ids), "telegram": _is_telegram_connected()}
