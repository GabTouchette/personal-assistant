"""CV generation — render HTML template to PDF using WeasyPrint."""

import logging
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from personal_assistant.config import settings

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(settings.cv_template_dir)
_BASE_CVS = {
    "en": Path(__file__).parent / "base_cv.yaml",
    "fr": Path(__file__).parent / "base_cv_fr.yaml",
}
OUTPUT_DIR = Path(settings.output_dir)


def load_base_cv(lang: str = "en") -> dict:
    """Load the base CV YAML for the given language ('en' or 'fr')."""
    path = _BASE_CVS.get(lang, _BASE_CVS["en"])
    with open(path) as f:
        return yaml.safe_load(f)


def render_cv_html(cv_data: dict) -> str:
    """Render CV data into HTML using the Jinja2 template."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("cv_template.html")
    return template.render(**cv_data)


def generate_pdf(cv_data: dict, filename: str) -> Path:
    """Generate a PDF from CV data. Returns the output path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html_content = render_cv_html(cv_data)
    output_path = OUTPUT_DIR / filename
    HTML(string=html_content).write_pdf(str(output_path))
    logger.info("CV PDF generated: %s", output_path)
    return output_path
