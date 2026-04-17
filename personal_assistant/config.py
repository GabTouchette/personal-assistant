from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # LinkedIn
    linkedin_email: str = ""
    linkedin_password: str = ""

    # Gmail
    gmail_address: str = ""
    gmail_app_password: str = ""

    # Database
    database_url: str = f"sqlite:///{BASE_DIR / 'jobs.db'}"

    # Logging
    log_level: str = "INFO"

    # Job search criteria
    job_titles: list[str] = Field(
        default=["Software Engineer", "Backend Engineer", "Full Stack Engineer"]
    )
    job_locations: list[str] = Field(default=["Montreal", "Remote"])
    home_city: str = "Montreal"  # Non-remote jobs outside this city get penalized
    min_salary: int = 80_000
    max_experience_years: int = 3  # Penalize jobs requiring more than this
    relevance_threshold: int = 60
    priority_industries: list[str] = Field(default=["medical", "health", "healthcare", "biotech"])

    # Anti-detection
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 8.0
    daily_profile_view_limit: int = 100
    daily_connection_request_limit: int = 50

    # Scheduling
    scrape_interval_hours: int = 8  # ~3x/day

    # Paths
    browser_data_dir: str = str(BASE_DIR / "browser_data")
    output_dir: str = str(BASE_DIR / "output")
    cv_template_dir: str = str(BASE_DIR / "personal_assistant" / "cv" / "templates")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
