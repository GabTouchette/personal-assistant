"""Deterministic keyword-based job scorer with preference learning.

Two parts:
  1. score_job(job) — instant, free, deterministic weighted keyword match
  2. record_feedback(job, approved) — adjusts weights from YES/NO decisions

Weights are persisted in output/scoring_weights.json so they survive restarts
and are human-readable / editable.

User preferences (output/user_preferences.json) inject additional keywords
from technologies, domains, desired job titles, and deal-breakers so the
scorer works for any profession — not just software developers.
"""

import json
import logging
import re
from pathlib import Path

from personal_assistant.config import settings
from personal_assistant.db.models import Job

logger = logging.getLogger(__name__)

WEIGHTS_PATH = Path(settings.output_dir) / "scoring_weights.json"
PREFS_PATH = Path(settings.output_dir) / "user_preferences.json"

# ── Default weight tables ─────────────────────────────────────────────────────
# These are the base/fallback keywords. User preferences add more at runtime.

_DEFAULT_WEIGHTS = {
    # === Positive signals ===
    "skills": {},         # populated from user prefs "technologies"
    "domain": {},         # populated from user prefs "domains"
    "job_titles": {},     # populated from user prefs "desired_titles"
    "seniority": {
        "junior": 10,
        "intermediate": 8, "mid-level": 8,
        "new grad": 6, "entry level": 6, "entry-level": 6,
        "1-3 years": 5, "2+ years": 5, "1+ years": 5,
    },
    "location": {
        "remote": 8,
        "hybrid": 4,
    },
    # === Negative signals (penalty) ===
    "penalties": {
        # Seniority mismatch (dynamically adjusted from max_experience_years)
        "senior": -12,
        "staff": -15,
        "principal": -20,
        "architect": -10,
        "director": -25,
        "vp ": -25,
    },
    "deal_breakers": {},  # populated from user prefs "deal_breakers"
    # === Learned adjustments (populated by feedback) ===
    "learned_boosts": {},
    "learned_penalties": {},
    "blocked_companies": [],
    "company_reject_count": {},
}

# Default keyword weight when injected from user preferences
_USER_SKILL_WEIGHT = 10
_USER_DOMAIN_WEIGHT = 15
_USER_TITLE_WEIGHT = 12
_USER_DEAL_BREAKER_PENALTY = -20


def _load_prefs(user_id: int | None = None) -> dict:
    """Load user preferences from disk."""
    if user_id is not None:
        path = Path(settings.output_dir) / f"user_preferences_{user_id}.json"
    else:
        path = PREFS_PATH
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _load_weights(user_id: int | None = None) -> dict:
    """Load weights from disk, merge with defaults, then inject user preferences."""
    if WEIGHTS_PATH.exists():
        try:
            with open(WEIGHTS_PATH) as f:
                saved = json.load(f)
            # Merge with defaults so new categories are picked up
            merged = json.loads(json.dumps(_DEFAULT_WEIGHTS))
            for key in merged:
                if key in saved:
                    if isinstance(merged[key], dict) and isinstance(saved[key], dict):
                        merged[key] = {**merged[key], **saved[key]}
                    else:
                        merged[key] = saved[key]
        except Exception as e:
            logger.warning("Failed to load scoring weights: %s — using defaults", e)
            merged = json.loads(json.dumps(_DEFAULT_WEIGHTS))
    else:
        merged = json.loads(json.dumps(_DEFAULT_WEIGHTS))

    # Inject user-defined keywords from preferences
    prefs = _load_prefs(user_id)
    radar = prefs.get("weights", {})

    # Skills from preferences → "skills" category
    for tech in prefs.get("technologies", []):
        kw = tech.lower().strip()
        if kw and kw not in merged["skills"]:
            base = _USER_SKILL_WEIGHT
            factor = radar.get("skills_match", 50) / 50
            merged["skills"][kw] = max(1, int(base * factor))

    # Domains from preferences → "domain" category
    for dom in prefs.get("domains", []):
        kw = dom.lower().strip()
        if kw and kw not in merged["domain"]:
            base = _USER_DOMAIN_WEIGHT
            factor = radar.get("industry_match", 50) / 50
            merged["domain"][kw] = max(1, int(base * factor))

    # Desired job titles → "job_titles" category
    for title in prefs.get("desired_titles", []):
        kw = title.lower().strip()
        if kw and kw not in merged["job_titles"]:
            base = _USER_TITLE_WEIGHT
            merged["job_titles"][kw] = base

    # Home city → "location" category
    home = prefs.get("home_city", settings.home_city).lower().strip()
    if home:
        merged["location"][home] = merged["location"].get(home, 6)
        # Accent variant
        _ACCENT_MAP = {"montreal": "montréal", "montréal": "montreal"}
        if home in _ACCENT_MAP:
            merged["location"][_ACCENT_MAP[home]] = merged["location"].get(_ACCENT_MAP[home], 6)

    # Deal-breaker keywords → "deal_breakers" category
    for db_kw in prefs.get("deal_breakers", []):
        kw = db_kw.lower().strip()
        if kw and kw not in merged["deal_breakers"]:
            merged["deal_breakers"][kw] = _USER_DEAL_BREAKER_PENALTY

    # Experience penalties — dynamically build from max_experience_years
    max_yrs = int(prefs.get("max_experience_years", settings.max_experience_years))
    _exp_tiers = [
        (3, -5), (5, -10), (7, -15), (8, -18), (10, -25), (15, -35),
    ]
    for yrs, penalty in _exp_tiers:
        if yrs > max_yrs:
            for pattern in [f"{yrs}+ years", f"{yrs} years of experience", f"{yrs} years"]:
                merged["penalties"][pattern] = penalty

    # Apply radar multipliers to base categories
    _category_to_radar = {
        "skills": "skills_match",
        "domain": "industry_match",
        "seniority": "seniority_fit",
        "location": "location_fit",
    }
    for cat, radar_key in _category_to_radar.items():
        factor = radar.get(radar_key, 50) / 50
        if cat in merged and isinstance(merged[cat], dict):
            for kw in merged[cat]:
                merged[cat][kw] = max(1, int(abs(merged[cat][kw]) * factor)) if merged[cat][kw] > 0 else merged[cat][kw]

    return merged


def _save_weights(weights: dict) -> None:
    """Persist weights to disk."""
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHTS_PATH, "w") as f:
        json.dump(weights, f, indent=2, ensure_ascii=False)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Check if keyword appears in text using word boundaries where possible.

    Multi-word phrases and keywords with special chars (/, +, .) use substring match.
    Single "normal" words use \\b word boundaries to avoid false positives
    (e.g. "css" won't match "accessing").
    """
    if any(c in keyword for c in "/.+#") or " " in keyword:
        # Multi-word or special-char keywords: substring match is safer
        return keyword in text
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


# ── Scoring ───────────────────────────────────────────────────────────────────

_POSITIVE_CATEGORIES = ("skills", "domain", "job_titles", "seniority", "location")

def score_job(job: Job) -> dict:
    """Score a job using keyword weights. Returns dict with score + breakdown.

    Returns:
        {
            "score": int (0-100 clamped),
            "raw_score": int (unclamped),
            "breakdown": {"flutter": +18, "python": +12, ...},
            "tier": "high" | "borderline" | "low" | "auto_reject",
            "blocked": bool,
        }
    """
    weights = _load_weights(job.user_id)
    text = _normalize(" ".join([
        job.title or "",
        job.company or "",
        job.location or "",
        (job.description or "")[:4000],
    ]))

    # Check blocked companies
    company_lower = (job.company or "").lower().strip()
    if company_lower in [c.lower() for c in weights.get("blocked_companies", [])]:
        return {
            "score": 0, "raw_score": 0,
            "breakdown": {"blocked_company": -100},
            "tier": "auto_reject", "blocked": True,
        }

    breakdown = {}
    raw = 0

    # Positive categories
    for category in _POSITIVE_CATEGORIES:
        for keyword, weight in weights.get(category, {}).items():
            if _keyword_in_text(keyword, text):
                breakdown[keyword] = weight
                raw += weight

    # Penalties
    for keyword, penalty in weights.get("penalties", {}).items():
        if _keyword_in_text(keyword, text):
            breakdown[keyword] = penalty
            raw += penalty

    # Deal-breakers (user-defined negative keywords)
    for keyword, penalty in weights.get("deal_breakers", {}).items():
        if _keyword_in_text(keyword, text):
            breakdown[f"deal_breaker:{keyword}"] = penalty
            raw += penalty

    # Learned boosts
    for keyword, boost in weights.get("learned_boosts", {}).items():
        if _keyword_in_text(keyword, text):
            breakdown[f"learned:{keyword}"] = boost
            raw += boost

    # Learned penalties
    for keyword, penalty in weights.get("learned_penalties", {}).items():
        if _keyword_in_text(keyword, text):
            breakdown[f"learned:{keyword}"] = penalty
            raw += penalty

    # ── Salary penalty ────────────────────────────────────────────────────
    prefs = _load_prefs(job.user_id)
    min_salary = prefs.get("salary", settings.min_salary)
    radar = prefs.get("weights", {})
    salary_importance = radar.get("compensation", 50) / 100  # 0.0-1.0

    if min_salary and salary_importance > 0:
        # Extract salary numbers from job text
        salary_matches = re.findall(r"\$\s*([\d,]+)", text)
        if salary_matches:
            max_posted = max(int(s.replace(",", "")) for s in salary_matches)
            if max_posted < min_salary:
                penalty = int(-15 * salary_importance)
                breakdown["below_min_salary"] = penalty
                raw += penalty

    # ── Work style penalty ────────────────────────────────────────────────
    work_pref = prefs.get("work_mode", "any")
    work_importance = radar.get("work_style", 50) / 100

    if work_pref != "any" and work_importance > 0:
        loc_lower = _normalize(job.location or "")
        desc_start = _normalize((job.description or "")[:500])
        combined = loc_lower + " " + desc_start

        if work_pref == "remote" and "remote" not in combined:
            penalty = int(-10 * work_importance)
            breakdown["not_remote"] = penalty
            raw += penalty
        elif work_pref == "onsite" and "remote" in combined and "hybrid" not in combined:
            penalty = int(-5 * work_importance)
            breakdown["only_remote"] = penalty
            raw += penalty

    # ── Location penalty: non-local + non-remote → heavy penalty ──────────
    home_city = prefs.get("home_city", settings.home_city).lower().strip()
    _home_cities = {home_city}
    _ACCENT_MAP = {"montreal": "montréal", "montréal": "montreal"}
    if home_city in _ACCENT_MAP:
        _home_cities.add(_ACCENT_MAP[home_city])

    loc_lower = _normalize(job.location or "")
    title_lower = _normalize(job.title or "")
    desc_start = _normalize((job.description or "")[:500])
    is_remote = (
        "remote" in loc_lower
        or "remote" in title_lower
        or "remote" in desc_start
        or getattr(job, "is_remote", False)
    )
    _broad_regions = ["amérique du nord", "north america", "worldwide", "anywhere", "canada"]
    is_broad_region = any(region in loc_lower for region in _broad_regions)
    is_local = any(city in loc_lower for city in _home_cities)

    location_importance = radar.get("location_fit", 65) / 100
    if not is_remote and not is_broad_region and not is_local and loc_lower:
        penalty = int(-30 * location_importance)
        breakdown["non_local_onsite"] = penalty
        raw += penalty

    # ── Referral / connection boost ───────────────────────────────────────
    target_companies = [c.lower().strip() for c in prefs.get("target_companies", [])]
    connection_companies = [c.lower().strip() for c in prefs.get("_resolved_connection_companies", [])]
    all_referral_companies = set(target_companies + connection_companies)

    if company_lower and all_referral_companies:
        for ref_company in all_referral_companies:
            if ref_company and (ref_company in company_lower or company_lower in ref_company):
                boost = 20
                breakdown["referral_company"] = boost
                raw += boost
                break

    # ── Staleness penalty ─────────────────────────────────────────────────
    from datetime import datetime, timedelta
    posted_at = getattr(job, "posted_at", None)
    if posted_at:
        try:
            age_days = (datetime.utcnow() - posted_at).days
            if age_days > 21:
                penalty = -15
                breakdown["stale_listing"] = penalty
                raw += penalty
            elif age_days > 14:
                penalty = -8
                breakdown["stale_listing"] = penalty
                raw += penalty
        except Exception:
            pass

    clamped = max(0, min(100, raw))

    # Tier classification
    if clamped >= 70:
        tier = "high"
    elif clamped >= 25:
        tier = "borderline"
    elif clamped >= 10:
        tier = "low"
    else:
        tier = "auto_reject"

    return {
        "score": clamped,
        "raw_score": raw,
        "breakdown": breakdown,
        "tier": tier,
        "blocked": False,
    }


# ── Feedback learning ─────────────────────────────────────────────────────────

# How much to adjust weights per feedback event
_BOOST_STEP = 2
_PENALTY_STEP = -2
_COMPANY_BLOCK_THRESHOLD = 3


def record_feedback(job: Job, approved: bool) -> None:
    """Adjust scoring weights based on a YES/NO decision.

    - YES: boosts keywords found in the job title + first 500 chars of description
    - NO: penalizes keywords; tracks company rejection count → auto-block after 3
    """
    weights = _load_weights()
    text = _normalize(" ".join([
        job.title or "",
        (job.description or "")[:500],
    ]))

    # Extract which known keywords are in this job
    all_keywords = set()
    for cat in _POSITIVE_CATEGORIES:
        for kw in weights.get(cat, {}):
            if _keyword_in_text(kw, text):
                all_keywords.add(kw)

    learned_boosts = weights.setdefault("learned_boosts", {})
    learned_penalties = weights.setdefault("learned_penalties", {})

    if approved:
        for kw in all_keywords:
            learned_boosts[kw] = learned_boosts.get(kw, 0) + _BOOST_STEP
            # If it was previously penalized, reduce the penalty
            if kw in learned_penalties:
                learned_penalties[kw] = min(0, learned_penalties[kw] + 1)
                if learned_penalties[kw] >= 0:
                    del learned_penalties[kw]
        logger.info("Feedback YES: boosted %d keywords for job %d", len(all_keywords), job.id)
    else:
        for kw in all_keywords:
            learned_penalties[kw] = learned_penalties.get(kw, 0) + _PENALTY_STEP
            # If it was previously boosted, reduce the boost
            if kw in learned_boosts:
                learned_boosts[kw] = max(0, learned_boosts[kw] - 1)
                if learned_boosts[kw] <= 0:
                    del learned_boosts[kw]

        # Track company rejection count
        company = (job.company or "").strip()
        if company:
            reject_counts = weights.setdefault("company_reject_count", {})
            reject_counts[company] = reject_counts.get(company, 0) + 1
            if reject_counts[company] >= _COMPANY_BLOCK_THRESHOLD:
                blocked = weights.setdefault("blocked_companies", [])
                if company not in blocked:
                    blocked.append(company)
                    logger.info("Auto-blocked company '%s' after %d rejections", company, reject_counts[company])

        logger.info("Feedback NO: penalized %d keywords for job %d", len(all_keywords), job.id)

    _save_weights(weights)
