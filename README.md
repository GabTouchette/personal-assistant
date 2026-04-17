# Personal Assistant — LinkedIn Job Application Agent

A Python agent that scrapes LinkedIn for software engineering jobs, sends SMS summaries for approval, generates tailored CVs and cover emails using Claude, submits applications, and reaches out to relevant contacts at target companies.

**Human-in-the-loop**: every CV and application goes through you first via SMS.

## Architecture

```
Scrape LinkedIn → Claude Analysis → SMS Notification → You approve
              → Claude CV Tailoring → SMS CV Review → You approve
              → Easy Apply / Email Submit → Networking Outreach
```

## Project Structure

```
personal_assistant/
├── cli.py              # CLI entry point (pa command)
├── config.py           # Settings via pydantic-settings + .env
├── pipeline.py         # Pipeline orchestrator (ties all stages)
├── scraper/
│   ├── auth.py         # Playwright login, cookie persistence, 2FA
│   ├── jobs.py         # Job search + detail extraction
│   └── anti_detect.py  # Human-like delays, scrolling, mouse movement
├── analyzer/
│   └── relevance.py    # Claude job scoring (0-100) + tech stack extraction
├── cv/
│   ├── base_cv.yaml    # Your base CV data (edit this!)
│   ├── generator.py    # HTML→PDF via Jinja2 + WeasyPrint
│   ├── tailoring.py    # Claude CV tailoring + cover email generation
│   └── templates/
│       └── cv_template.html
├── notifier/
│   └── sms.py          # Twilio SMS notifications + approval requests
├── applicator/
│   └── submit.py       # Easy Apply (Playwright) + email (SMTP)
├── networker/
│   ├── research.py     # Find hiring managers/recruiters at companies
│   └── outreach.py     # Claude drafts connection notes + cold emails
├── server/
│   └── webhook.py      # FastAPI endpoint for Twilio SMS replies
├── scheduler/
│   └── jobs.py         # APScheduler recurring pipeline runs
└── db/
    ├── models.py       # SQLAlchemy models (jobs, applications, contacts, messages)
    └── queries.py      # Convenience DB queries
```

## Quick Start

### 1. Install dependencies

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install project + Python 3.12
uv sync

# Install Playwright browser
uv run playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API keys, credentials, etc.
```

**Required credentials:**
- `ANTHROPIC_API_KEY` — Claude API key
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` — Twilio SMS
- `MY_PHONE_NUMBER` — Your phone for notifications
- `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` — LinkedIn login

**Optional:**
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` — For email applications
- `WEBHOOK_BASE_URL` — Cloudflare Tunnel URL for Twilio webhooks

### 3. Edit your base CV

Edit `personal_assistant/cv/base_cv.yaml` with your real information.

### 4. Initialize database

```bash
uv run python -m personal_assistant.cli init-db
```

### 5. Run

```bash
# Run discovery pipeline once (scrape → analyze → SMS notify)
uv run python -m personal_assistant.cli scrape

# Generate CVs for approved jobs
uv run python -m personal_assistant.cli cv

# Submit applications
uv run python -m personal_assistant.cli apply

# Run networking outreach
uv run python -m personal_assistant.cli network

# Run everything once
uv run python -m personal_assistant.cli run-all

# Start scheduler + webhook server (production mode)
uv run python -m personal_assistant.cli scheduler
```

## SMS Commands

Reply to notifications via text:

| Command | Action |
|---------|--------|
| `YES <job_id>` | Approve job → triggers CV generation |
| `NO <job_id>` | Reject job |
| `INFO <job_id>` | Get full job description |
| `APPLY <job_id>` | Approve CV → submit application |
| `REDO <job_id>` | Re-generate the tailored CV |
| `SKIP <job_id>` | Skip this application |
| `SEND <msg_id>` | Approve & send outreach message |
| `EDIT <msg_id>` | Request re-draft of outreach |

## Webhook Setup (Twilio)

For SMS replies to work, Twilio needs to reach your webhook:

```bash
# Option 1: Cloudflare Tunnel (free)
cloudflared tunnel --url http://localhost:8000

# Option 2: ngrok
ngrok http 8000
```

Set the tunnel URL as your Twilio webhook:
`https://your-tunnel.trycloudflare.com/webhook/sms`

## Job Pipeline States

```
discovered → summarized → notified → approved → cv_generated → cv_approved → applied → networking_done
                                   ↘ rejected
```

## Anti-Detection

- Randomized delays (2-8s between actions)
- Human-like scrolling and mouse movements
- Per-character typing with variable delays
- Persistent browser context with real cookies
- Daily action limits (configurable)

## Tech Stack

| Component | Tech |
|-----------|------|
| Language | Python 3.12+ |
| Browser automation | Playwright |
| LLM | Claude (Sonnet for speed) |
| SMS | Twilio |
| Webhook server | FastAPI |
| PDF generation | WeasyPrint |
| Database | SQLite + SQLAlchemy |
| Email | smtplib + Gmail App Password |
| Scheduler | APScheduler |

## Cost Estimate

~$10-25/month: Twilio ~$5, Claude API ~$5-15, hosting $0 locally.
