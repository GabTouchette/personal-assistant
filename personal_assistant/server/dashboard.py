"""FastAPI dashboard — Kanban board for job application tracking."""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from personal_assistant.db.models import JobStatus, init_db
from personal_assistant.db.queries import (
    get_all_jobs,
    get_job_by_id,
    get_job_detail,
    update_job_notes,
    update_job_status,
)

app = FastAPI(title="Job Dashboard")

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Serve tailored CV PDFs
_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
if _OUTPUT_DIR.exists():
    app.mount("/output", StaticFiles(directory=str(_OUTPUT_DIR)), name="output")


# ── Kanban column mapping ────────────────────────────────────────────────────

KANBAN_COLUMNS = [
    {
        "id": "pending",
        "label": "Pending Review",
        "statuses": ["notified"],
        "color": "#3b82f6",
    },
    {
        "id": "in_progress",
        "label": "In Progress",
        "statuses": ["approved", "cv_generated", "cv_approved"],
        "color": "#f59e0b",
    },
    {
        "id": "applied",
        "label": "Applied",
        "statuses": ["applied", "networking_done"],
        "color": "#8b5cf6",
    },
    {
        "id": "response",
        "label": "Response",
        "statuses": ["response_received"],
        "color": "#06b6d4",
    },
    {
        "id": "interview",
        "label": "Interview",
        "statuses": ["interview_scheduled", "interview_done", "second_interview"],
        "color": "#10b981",
    },
    {
        "id": "closed",
        "label": "Offer / Closed",
        "statuses": ["offer", "hired", "denied", "withdrawn"],
        "color": "#ef4444",
    },
]

COLLAPSED_STATUSES = {"discovered", "summarized", "rejected", "failed"}

# Human-friendly status labels
STATUS_LABELS = {s.value: s.value.replace("_", " ").title() for s in JobStatus}

# Statuses available for manual update (post-application human tracking)
MANUAL_STATUSES = [
    "response_received",
    "interview_scheduled",
    "interview_done",
    "second_interview",
    "offer",
    "hired",
    "denied",
    "withdrawn",
    "rejected",
]


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    init_db()
    jobs = get_all_jobs()

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
        "manual_statuses": MANUAL_STATUSES,
    })


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: int):
    detail = get_job_detail(job_id)
    if not detail:
        return JSONResponse({"error": "not found"}, status_code=404)
    return detail


@app.post("/api/jobs/{job_id}/status")
async def api_update_status(job_id: int, request: Request):
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
    data = await request.json()
    notes = data.get("notes", "")
    update_job_notes(job_id, notes)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/cv")
async def api_download_cv(job_id: int):
    job = get_job_by_id(job_id)
    if not job or not job.tailored_cv_path:
        return JSONResponse({"error": "no CV"}, status_code=404)
    path = Path(job.tailored_cv_path)
    if not path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)
