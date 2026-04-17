"""Claude-powered CV tailoring and cover email generation.

Language:
  - detect_job_language(job) sniffs the job description for French markers.
  - French jobs get a CV tailored from base_cv_fr.yaml (French content) and a
    French cover email.
  - English is the fallback.

Quebec title rule:
  - The word "Engineer" / "Ingénieur" is a protected title in Quebec (OIQ).
  - Both base CVs already use "Developer" / "Développeur".
  - The tailoring prompt explicitly reminds Claude of this constraint.
"""

import json
import logging

import anthropic
import yaml

from personal_assistant.config import settings
from personal_assistant.cv.generator import generate_pdf, load_base_cv
from personal_assistant.db.models import Job, JobStatus
from personal_assistant.db.queries import update_job_status

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# ── Language detection ────────────────────────────────────────────────────────

# French markers found in job descriptions; 2+ hits → treat as French job
_FRENCH_MARKERS = [
    "français", "francophone", "bilingue", "en français",
    "french speaking", "french-speaking", "french required",
    "nous offrons", "nous sommes", "vous serez", "nous cherchons",
    "poste", "emploi", "cherchons", "recherchons", "développeur",
    "ingénieur", "logiciel", "maîtrise du français", "parlé et écrit",
    "milieu de travail", "rejoignez", "notre équipe",
]


def detect_job_language(job: Job) -> str:
    """Return 'fr' if the job is primarily French-speaking, else 'en'."""
    text = " ".join([
        job.title or "",
        job.company or "",
        (job.description or "")[:2000],
    ]).lower()
    hits = sum(1 for m in _FRENCH_MARKERS if m in text)
    lang = "fr" if hits >= 2 else "en"
    logger.debug("Job %d language detected: %s (hits=%d)", job.id, lang, hits)
    return lang


# ── Tailoring prompts ─────────────────────────────────────────────────────────

_TAILOR_PROMPT_EN = """\
You are an expert resume writer. Tailor the base CV (YAML) to maximize relevance for the target job.

Rules:
1. STRICT HONESTY — never invent, add, or imply experience, companies, technologies, or skills that
   are not already present in the base CV. Only rearrange, remove, or reword existing content.
2. Focus PRIMARILY on the Skills section — reorder categories and surface relevant skills the
   candidate already has. Do NOT add new skill items unless they appear in the base CV.
3. Tailor experience bullets: reword to highlight relevant technologies and responsibilities,
   reorder so the most relevant bullets come first, and REMOVE bullets irrelevant to this role.
4. Do NOT make the CV longer than the original. Cut content the company doesn't care about.
5. NEVER use the word "Engineer" or "Ingénieur" in any job title — use "Developer",
   "Technologist", or "Lead Developer" instead. This is a legal requirement in Quebec.
6. Do NOT write a professional_summary — leave it empty ("").
7. Add a top-level field `modification_note` with:
   a) ONE sentence describing the key changes made.
   b) If the job description values technologies or skills NOT in the base CV that would be
      genuinely useful to add, list them as suggestions in parentheses, e.g.:
      "Emphasized cloud experience. Suggestion: Knowledge of AWS and Redis Streams is valued
      in this role — would you like to add them?"
8. Output structure must be identical to the input YAML (plus the `modification_note` field).

Base CV:
```yaml
{base_cv_yaml}
```

Target job:
Title: {title}
Company: {company}
Description:
{description}

Return ONLY valid YAML — no markdown fences, no extra text.
"""

_TAILOR_PROMPT_FR = """\
Tu es un expert en rédaction de CV. Adapte le CV de base (YAML) pour maximiser la pertinence
par rapport au poste cible.

Règles :
1. HONNÊTETÉ STRICTE — n'invente, n'ajoute ou n'implique jamais d'expériences, d'entreprises,
   de technologies ou de compétences qui ne sont pas déjà présentes dans le CV de base.
   Réordonne, supprime ou reformule uniquement le contenu existant.
2. Concentre-toi PRINCIPALEMENT sur la section Compétences — réordonne les catégories et
   mets en avant les compétences que le candidat possède déjà. N'ajoute PAS de nouvelles
   compétences sauf si elles figurent dans le CV de base.
3. Adapte les points d'expérience : reformule pour mettre en valeur les technologies et
   responsabilités pertinentes, réordonne pour que les plus pertinents soient en premier,
   et SUPPRIME les points non pertinents pour ce poste.
4. Ne rends PAS le CV plus long que l'original. Retire le contenu non pertinent.
5. N'utilise JAMAIS le mot «Ingénieur» dans un titre de poste — utilise «Développeur»,
   «Développeur Principal» ou «Technologue». C'est une exigence légale au Québec (OIQ).
6. N'écris PAS de professional_summary — laisse le champ vide ("").
7. Ajoute un champ `modification_note` au premier niveau avec :
   a) UNE phrase décrivant les changements clés effectués.
   b) Si le poste valorise des technologies ou compétences absentes du CV qui seraient
      réellement utiles, liste-les comme suggestions entre parenthèses, ex. :
      «Mis en avant l'expérience cloud. Suggestion : La maîtrise d'AWS et de Redis Streams
      est valorisée dans ce poste — souhaites-tu les ajouter ?»
8. La structure de sortie doit être identique au YAML d'entrée (plus le champ `modification_note`).

CV de base :
```yaml
{base_cv_yaml}
```

Poste cible :
Titre : {title}
Entreprise : {company}
Description :
{description}

Retourne UNIQUEMENT du YAML valide — pas de balises markdown, pas de texte supplémentaire.
"""

_COVER_EN = """\
Write a concise, professional cover email for a software development role.

Candidate: {name}
Role: {title} at {company}
Key technologies from job: {tech_stack}
Relevant experience highlights:
{highlights}

Rules:
1. 3–4 short paragraphs max.
2. Professional but personable tone.
3. Reference specific aspects of the role and company.
4. No generic filler — every sentence adds value.
5. End with a clear call to action.
6. NEVER refer to the candidate as an "Engineer" — use "developer" or "developer and technologist".

Return ONLY the email body (no subject line, no salutation template).
"""

_COVER_FR = """\
Rédige un courriel de présentation concis et professionnel pour un poste de développement logiciel.

Candidat : {name}
Poste : {title} chez {company}
Technologies clés du poste : {tech_stack}
Points saillants de l'expérience :
{highlights}

Règles :
1. 3–4 courts paragraphes maximum.
2. Ton professionnel mais chaleureux.
3. Mentionne des aspects spécifiques du poste et de l'entreprise.
4. Pas de remplissage générique — chaque phrase apporte de la valeur.
5. Termine par un appel à l'action clair.
6. N'utilise JAMAIS le mot «ingénieur» pour décrire le candidat — utilise «développeur».

Retourne UNIQUEMENT le corps du courriel (sans ligne d'objet, sans formule de salutation modèle).
"""


# ── Core functions ────────────────────────────────────────────────────────────

def tailor_cv(job: Job, lang: str = "en") -> tuple[dict | None, str | None]:
    """Tailor the base CV for a job. Returns (cv_dict, modification_note) or (None, None)."""
    base_cv = load_base_cv(lang)
    base_yaml = yaml.dump(base_cv, default_flow_style=False, allow_unicode=True)

    prompt_template = _TAILOR_PROMPT_FR if lang == "fr" else _TAILOR_PROMPT_EN
    prompt = prompt_template.format(
        base_cv_yaml=base_yaml,
        title=job.title,
        company=job.company,
        description=(job.description or "")[:5000],
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]

        tailored = yaml.safe_load(text)
        if not isinstance(tailored, dict) or "name" not in tailored:
            logger.error("Tailored CV missing expected fields")
            return None, None

        modification_note = tailored.pop("modification_note", None)
        # Ensure professional_summary is empty
        tailored["professional_summary"] = ""
        return tailored, modification_note
    except Exception as e:
        logger.error("CV tailoring failed for job %d: %s", job.id, e)
        return None, None


def generate_cover_email(job: Job, lang: str = "en") -> str | None:
    """Generate a cover email in the given language using Claude."""
    base_cv = load_base_cv(lang)
    highlights = []
    for exp in base_cv.get("experience", [])[:2]:
        highlights.extend(exp.get("bullets", [])[:2])

    tech_stack = ""
    if job.tech_stack:
        try:
            tech_stack = ", ".join(json.loads(job.tech_stack))
        except (json.JSONDecodeError, TypeError):
            tech_stack = str(job.tech_stack)

    prompt_template = _COVER_FR if lang == "fr" else _COVER_EN
    prompt = prompt_template.format(
        name=base_cv.get("name", ""),
        title=job.title,
        company=job.company,
        tech_stack=tech_stack or "Non spécifié" if lang == "fr" else "Not specified",
        highlights="\n".join(f"- {h}" for h in highlights),
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error("Cover email generation failed for job %d: %s", job.id, e)
        return None


def tailor_and_generate(job: Job) -> tuple[str | None, str | None, str | None]:
    """Auto-detect language, tailor CV, generate PDF and cover email.

    Returns (pdf_path, cover_email_text, modification_note) or (None, None, None) on failure.
    """
    lang = detect_job_language(job)
    lang_tag = "FR" if lang == "fr" else "EN"
    logger.info("Tailoring CV [%s] for job %d: %s @ %s", lang_tag, job.id, job.title, job.company)

    tailored_cv, modification_note = tailor_cv(job, lang)
    if tailored_cv is None:
        return None, None, None

    filename = f"cv_job_{job.id}_{job.company.replace(' ', '_')[:20]}_{lang_tag}.pdf"
    pdf_path = generate_pdf(tailored_cv, filename)

    cover_email = generate_cover_email(job, lang)

    update_job_status(
        job.id,
        JobStatus.CV_GENERATED,
        tailored_cv_path=str(pdf_path),
        cover_email=cover_email,
    )

    logger.info("CV [%s] + cover email ready for job %d", lang_tag, job.id)
    return str(pdf_path), cover_email, modification_note
