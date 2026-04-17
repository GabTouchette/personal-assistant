"""Deterministic keyword-based job scorer with preference learning.

Two parts:
  1. score_job(job) — instant, free, deterministic weighted keyword match
  2. record_feedback(job, approved) — adjusts weights from YES/NO decisions

Weights are persisted in output/scoring_weights.json so they survive restarts
and are human-readable / editable.
"""

import json
import logging
import re
from pathlib import Path

from personal_assistant.config import settings
from personal_assistant.db.models import Job

logger = logging.getLogger(__name__)

WEIGHTS_PATH = Path(settings.output_dir) / "scoring_weights.json"

# ── Default weight tables ─────────────────────────────────────────────────────
# Each match adds the weight to the raw score. Negative = penalty.

_DEFAULT_WEIGHTS = {
    # === Positive signals ===
    "frameworks": {
        "flutter": 18,
        "django": 14,
        "vue": 12, "vuejs": 12, "vue.js": 12,
        "react": 10,
        "angular": 8,
        "fastapi": 14,
        "node": 6, "nodejs": 6, "node.js": 6,
        "next.js": 6, "nextjs": 6,
        "express": 5,
    },
    "languages": {
        "python": 12,
        "typescript": 10,
        "javascript": 6,
        "dart": 15,  # Flutter = Dart
        "html": 2,
        "css": 2,
    },
    "infra": {
        "docker": 5,
        "kubernetes": 6, "k8s": 6,
        "azure": 6,
        "terraform": 5,
        "argocd": 5,
        "ci/cd": 3, "ci cd": 3,
        "aws": 4,
        "gcp": 3,
    },
    "domain": {
        "healthcare": 20,
        "health tech": 20, "healthtech": 20,
        "medical": 20,
        "medtech": 20,
        "fhir": 18,
        "pacs": 18,
        "hipaa": 12,
        "ophthalmology": 25,
        "telemedicine": 15, "telehealth": 15,
        "biotech": 12,
        "pharmaceutical": 8,
    },
    "seniority": {
        "lead": 10,
        "senior": 8,
        "staff": 8,
        "principal": 6,
        "architect": 5,
        "manager": 3,
    },
    "location": {
        "remote": 8,
        "hybrid": 4,
        "montreal": 6, "montréal": 6,
        "canada": 4,
    },
    # === Negative signals (penalty) ===
    "penalties": {
        "10+ years": -15, "10 years": -10,
        "15+ years": -25, "15 years": -20,
        "phd required": -20, "ph.d. required": -20,
        "clearance required": -15, "security clearance": -15,
        "c# only": -12, ".net only": -12,
        "salesforce": -10,
        "sap ": -10,
        "cobol": -15,
        "mainframe": -15,
    },
    # === Learned adjustments (populated by feedback) ===
    "learned_boosts": {},      # keyword -> int  (from YES decisions)
    "learned_penalties": {},    # keyword -> int  (from NO decisions)
    "blocked_companies": [],    # companies rejected 3+ times → auto-reject
    "company_reject_count": {}, # company -> count of rejections
}


def _load_weights() -> dict:
    """Load weights from disk, or return defaults."""
    if WEIGHTS_PATH.exists():
        try:
            with open(WEIGHTS_PATH) as f:
                saved = json.load(f)
            # Merge with defaults so new categories are picked up
            merged = {**_DEFAULT_WEIGHTS}
            for key in merged:
                if key in saved:
                    if isinstance(merged[key], dict) and isinstance(saved[key], dict):
                        merged[key] = {**merged[key], **saved[key]}
                    else:
                        merged[key] = saved[key]
            return merged
        except Exception as e:
            logger.warning("Failed to load scoring weights: %s — using defaults", e)
    return json.loads(json.dumps(_DEFAULT_WEIGHTS))  # deep copy


def _save_weights(weights: dict) -> None:
    """Persist weights to disk."""
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHTS_PATH, "w") as f:
        json.dump(weights, f, indent=2, ensure_ascii=False)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


# ── Scoring ───────────────────────────────────────────────────────────────────

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
    weights = _load_weights()
    text = _normalize(" ".join([
        job.title or "",
        job.company or "",
        job.location or "",
        (job.description or "")[:3000],
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
    for category in ("frameworks", "languages", "infra", "domain", "seniority", "location"):
        for keyword, weight in weights.get(category, {}).items():
            if keyword in text:
                breakdown[keyword] = weight
                raw += weight

    # Penalties
    for keyword, penalty in weights.get("penalties", {}).items():
        if keyword in text:
            breakdown[keyword] = penalty
            raw += penalty

    # Learned boosts
    for keyword, boost in weights.get("learned_boosts", {}).items():
        if keyword in text:
            breakdown[f"learned:{keyword}"] = boost
            raw += boost

    # Learned penalties
    for keyword, penalty in weights.get("learned_penalties", {}).items():
        if keyword in text:
            breakdown[f"learned:{keyword}"] = penalty
            raw += penalty

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
    for cat in ("frameworks", "languages", "infra", "domain", "seniority"):
        for kw in weights.get(cat, {}):
            if kw in text:
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
