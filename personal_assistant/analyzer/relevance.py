"""Two-stage job analysis: local keyword scorer (free) → LLM only for borderline jobs.

Stage 1 — keyword_scorer.score_job():
  Instant, deterministic, free. Produces a raw score + tier:
    high (≥70)       → straight to notification, skip LLM
    borderline (25–69) → send to Haiku for nuanced scoring
    low (10–24)       → auto-reject, skip LLM
    auto_reject (<10) → trash, skip LLM

Stage 2 — Haiku batch call:
  Only borderline jobs go here (~30% of total). This saves ~60–70% of tokens.

Learning:
  keyword_scorer.record_feedback() adjusts weights from YES/NO decisions.
  Called by the webhook when user approves or rejects.
"""

import json
import logging

import anthropic

from personal_assistant.analyzer.keyword_scorer import score_job
from personal_assistant.config import settings
from personal_assistant.db.models import Job, JobStatus
from personal_assistant.db.queries import get_jobs_by_status, update_job_status

logger = logging.getLogger(__name__)

ANALYSIS_MODEL = "claude-haiku-4-5-20251001"
DESCRIPTION_LIMIT = 2000

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

_PREFS_PATH = __import__("pathlib").Path(settings.output_dir) / "user_preferences.json"


def _build_system_prompt() -> str:
    """Build the LLM scoring system prompt dynamically from user preferences."""
    prefs = {}
    if _PREFS_PATH.exists():
        try:
            prefs = json.loads(_PREFS_PATH.read_text())
        except Exception:
            pass

    # Extract preference values with sensible defaults
    techs = prefs.get("technologies", [])
    domains = prefs.get("domains", [])
    titles = prefs.get("desired_titles", [])
    deal_breakers = prefs.get("deal_breakers", [])
    home_city = prefs.get("home_city", settings.home_city)
    work_mode = prefs.get("work_mode", "any")
    max_exp = prefs.get("max_experience_years", settings.max_experience_years)
    yoe = prefs.get("years_experience", 3)
    salary = prefs.get("salary", settings.min_salary)
    extra = prefs.get("extra_comments", "")
    radar = prefs.get("weights", {})

    # Build candidate profile section
    profile_parts = []
    if techs:
        profile_parts.append(f"- Key skills/technologies: {', '.join(techs)}")
    if titles:
        profile_parts.append(f"- Looking for roles such as: {', '.join(titles)}")
    if domains:
        profile_parts.append(f"- Preferred industries: {', '.join(domains)}")
    profile_parts.append(f"- Experience: ~{yoe} year(s) of professional experience")
    profile_parts.append(f"- Location: {home_city} — {'remote preferred' if work_mode == 'remote' else 'open to ' + work_mode if work_mode != 'any' else 'flexible on work mode'}")
    if salary:
        profile_parts.append(f"- Minimum salary: ${salary:,} CAD")
    if extra:
        profile_parts.append(f"- Additional context: {extra}")
    profile_block = "\n".join(profile_parts)

    # Build constraints section from radar weights (higher = stricter)
    constraints = []
    if int(max_exp) <= 5:
        constraints.append(f"- Jobs requiring more than {max_exp} years experience: cap at 40.")
    constraints.append(f"- Jobs titled Senior/Staff/Principal if candidate has ~{yoe} years: cap at 35 unless description shows it's flexible.")
    if work_mode == "remote":
        constraints.append(f"- Non-remote jobs outside {home_city}: cap at 20.")
    elif work_mode != "any":
        constraints.append(f"- Jobs in cities other than {home_city} that don't match '{work_mode}' mode: cap at 20.")
    else:
        constraints.append(f"- Non-remote jobs outside {home_city}: penalize but don't cap below 40.")
    if deal_breakers:
        constraints.append(f"- Deal-breaker keywords (auto-reject if central to the role): {', '.join(deal_breakers)}")
    constraints_block = "\n".join(constraints)

    # Build scoring guide based on radar priorities
    skills_w = radar.get("skills_match", 50)
    industry_w = radar.get("industry_match", 50)
    location_w = radar.get("location_fit", 65)

    return f"""\
You are a job relevance scorer for a candidate searching for jobs.

Candidate profile (be strict — score high only on genuine fit):
{profile_block}

CONSTRAINTS — apply strictly:
{constraints_block}

Scoring priorities (from the candidate's preferences):
- Skills/tech match importance: {skills_w}/100
- Industry/domain match importance: {industry_w}/100
- Location match importance: {location_w}/100

Score 80-100: strong match on skills + preferred industry + location fits + seniority appropriate.
Score 60-79: decent overlap on most dimensions, minor concerns.
Score 40-59: partial match but notable gaps (seniority, location, or skill mismatch).
Score 0-39: poor fit (wrong field, too senior, wrong location, or deal-breaker present).
"""

BATCH_USER_PROMPT = """\
Analyze the following {n} jobs. Return a JSON array with exactly {n} objects in the same order.

Each object must have:
{{
  "id": <job_id int>,
  "relevance_score": <0-100>,
  "summary": "<1-2 sentences: role + why fit or not>",
  "tech_stack": ["relevant", "techs"],
  "estimated_salary_min": <int CAD or null>,
  "estimated_salary_max": <int CAD or null>,
  "is_remote": <bool>,
  "is_priority_industry": <bool — true if medical/health/biotech>
}}

Return ONLY the JSON array, no extra text.

Jobs:
{jobs_json}
"""


def _parse_batch(text: str) -> dict[int, dict]:
    """Parse Claude's batch response into {job_id: analysis} dict."""
    text = text.strip()
    if "```" in text:
        start = text.find("[")
        end = text.rfind("]") + 1
        text = text[start:end]
    try:
        results = json.loads(text)
        if not isinstance(results, list):
            raise ValueError("Expected JSON array")
        return {item["id"]: item for item in results if "id" in item}
    except Exception as e:
        logger.error("Failed to parse batch analysis: %s | raw: %s", e, text[:400])
        return {}


def _llm_analyze_batch(jobs: list[Job]) -> dict[int, dict]:
    """Send a batch of borderline jobs to Haiku. Returns {job_id: analysis}."""
    if not jobs:
        return {}

    jobs_payload = [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "location": j.location or "",
            "salary": j.salary_text or "",
            "description": (j.description or "")[:DESCRIPTION_LIMIT],
        }
        for j in jobs
    ]

    prompt = BATCH_USER_PROMPT.format(
        n=len(jobs_payload),
        jobs_json=json.dumps(jobs_payload, ensure_ascii=False),
    )

    try:
        system_prompt = _build_system_prompt()
        response = client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=150 * len(jobs),
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        logger.info(
            "LLM batch used %d input + %d output tokens for %d borderline jobs",
            response.usage.input_tokens, response.usage.output_tokens, len(jobs),
        )
        return _parse_batch(response.content[0].text)
    except Exception as e:
        logger.error("Batch Haiku analysis failed: %s", e)
        return {}


def analyze_new_jobs() -> list[Job]:
    """Two-stage analysis of all discovered jobs. Returns jobs above threshold."""
    discovered = get_jobs_by_status(JobStatus.DISCOVERED)
    if not discovered:
        logger.info("No discovered jobs to analyze")
        return []

    # ── Stage 1: Local keyword scoring ────────────────────────────────────────
    # Load domain keywords for priority industry detection
    from personal_assistant.analyzer.keyword_scorer import _load_weights as _kw_load
    _wts = _kw_load()
    weights_domain_keywords = set(_wts.get("domain", {}).keys())

    high_jobs: list[Job] = []
    borderline_jobs: list[Job] = []
    rejected_count = 0

    for job in discovered:
        result = score_job(job)
        tier = result["tier"]
        kw_score = result["score"]

        # Attach keyword breakdown for logging / dashboard
        job._keyword_result = result  # transient, not saved to DB

        if tier == "high":
            high_jobs.append(job)
            logger.info(
                "Stage 1 HIGH (%d): %s @ %s | %s",
                kw_score, job.title, job.company,
                ", ".join(f"{k}={v:+d}" for k, v in list(result["breakdown"].items())[:6]),
            )
        elif tier == "borderline":
            borderline_jobs.append(job)
            logger.info("Stage 1 BORDERLINE (%d): %s @ %s", kw_score, job.title, job.company)
        else:
            # low or auto_reject — skip LLM
            rejected_count += 1
            update_job_status(
                job.id, JobStatus.SUMMARIZED,
                relevance_score=kw_score,
                summary=f"Auto-scored {kw_score}/100 (keyword match). Below threshold.",
                tech_stack="[]",
            )
            logger.debug("Stage 1 REJECT (%d): %s @ %s", kw_score, job.title, job.company)

    logger.info(
        "Stage 1 complete: %d high, %d borderline, %d rejected (no LLM needed)",
        len(high_jobs), len(borderline_jobs), rejected_count,
    )

    # ── Stage 2: LLM for borderline jobs only ─────────────────────────────────
    llm_results = _llm_analyze_batch(borderline_jobs)

    # ── Merge results ─────────────────────────────────────────────────────────
    above_threshold: list[Job] = []

    # Process high-scoring jobs (no LLM needed — just generate a summary locally)
    for job in high_jobs:
        result = job._keyword_result
        top_matches = sorted(result["breakdown"].items(), key=lambda x: x[1], reverse=True)[:5]
        summary = (
            f"Strong keyword match ({result['score']}/100). "
            f"Top signals: {', '.join(f'{k} (+{v})' for k, v in top_matches if v > 0)}."
        )
        tech_list = [k for k, v in result["breakdown"].items()
                     if v > 0 and not k.startswith("learned:")]

        update_job_status(
            job.id, JobStatus.SUMMARIZED,
            relevance_score=result["score"],
            summary=summary,
            tech_stack=json.dumps(tech_list),
            is_remote="remote" in (job.location or "").lower()
                      or "remote" in (job.description or "").lower()[:500],
            is_priority_industry=any(
                kw in result["breakdown"]
                for kw in weights_domain_keywords
            ),
        )

        if result["score"] >= settings.relevance_threshold:
            job.relevance_score = result["score"]
            job.summary = summary
            job.tech_stack = json.dumps(tech_list)
            above_threshold.append(job)

    # Process borderline jobs (LLM-scored)
    for job in borderline_jobs:
        analysis = llm_results.get(job.id)
        kw_result = job._keyword_result

        if analysis is None:
            # LLM didn't return a result — fall back to keyword score
            score = kw_result["score"]
            summary = f"Keyword score {score}/100 (LLM unavailable)."
            tech_stack = "[]"
        else:
            # Average the keyword score and LLM score for stability
            kw_score = kw_result["score"]
            llm_score = analysis.get("relevance_score", 0)
            score = (kw_score + llm_score) // 2
            summary = analysis.get("summary", "")
            tech_stack = json.dumps(analysis.get("tech_stack", []))

            logger.info(
                "Stage 2: job %d — keyword=%d, LLM=%d, final=%d | %s @ %s",
                job.id, kw_score, llm_score, score, job.title, job.company,
            )

        update_job_status(
            job.id, JobStatus.SUMMARIZED,
            relevance_score=score,
            summary=summary,
            tech_stack=tech_stack,
            is_remote=analysis.get("is_remote", False) if analysis else False,
            is_priority_industry=analysis.get("is_priority_industry", False) if analysis else False,
            salary_min=analysis.get("estimated_salary_min") if analysis else None,
            salary_max=analysis.get("estimated_salary_max") if analysis else None,
        )

        if score >= settings.relevance_threshold:
            job.relevance_score = score
            job.summary = summary
            job.tech_stack = tech_stack
            above_threshold.append(job)

    logger.info(
        "Analysis complete: %d jobs above threshold (%d)",
        len(above_threshold), settings.relevance_threshold,
    )
    return above_threshold
