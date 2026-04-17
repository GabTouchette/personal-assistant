"""Scrape LinkedIn job listings using Playwright."""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import Page, Locator

from personal_assistant.config import settings
from personal_assistant.db.queries import upsert_job
from personal_assistant.scraper.anti_detect import human_delay, human_scroll, random_mouse_movement
from personal_assistant.scraper.auth import LinkedInSession

logger = logging.getLogger(__name__)


@dataclass
class RawJob:
    linkedin_job_id: str
    title: str
    company: str
    location: str = ""
    salary_text: str = ""
    description: str = ""
    job_url: str = ""
    is_easy_apply: bool = False
    is_remote: bool = False
    posted_at_text: str = ""


def _build_search_url(keywords: str, location: str, time_filter: str = "r86400",
                      *, network_filter: bool = False) -> str:
    """Build a LinkedIn Jobs search URL.

    time_filter: r86400 = past 24h,  r604800 = past week
    network_filter: if True, add f_CR (company relationship) = your connections
    """
    base = "https://www.linkedin.com/jobs/search/?"
    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": time_filter,  # time posted
        "sortBy": "DD",  # sort by date
    }
    if network_filter:
        params["f_CR"] = "F"   # F = 1st-degree connections at the company
    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    return base + query


def _build_company_jobs_url(company_slug: str) -> str:
    """Build the LinkedIn company jobs page URL."""
    return f"https://www.linkedin.com/company/{quote_plus(company_slug)}/jobs/"


async def _find_scrollable_list(page: Page) -> Locator | None:
    """Find the scrollable job list container using multiple fallback selectors."""
    candidates = [
        ".jobs-search-results-list",
        ".scaffold-layout__list-container",
        ".scaffold-layout__list",
        ".jobs-search-results__list",
        "[class*='jobs-search'][class*='list']",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            logger.debug("Found job list container: %s", sel)
            return loc
    return None


async def _extract_job_ids_from_list(page: Page, max_pages: int = 3) -> list[str]:
    """Scroll through the job list sidebar and collect job IDs."""
    job_ids: list[str] = []

    for page_num in range(max_pages):
        logger.info("Scanning job list page %d", page_num + 1)

        # Try to find and scroll the job list panel
        job_list = await _find_scrollable_list(page)
        if job_list:
            for _ in range(5):
                try:
                    await job_list.evaluate("el => el.scrollTop += 400")
                except Exception:
                    break
                await human_delay(0.5, 1.2)
        else:
            # Fallback: scroll the whole page to trigger lazy-loading
            logger.debug("No list container found, scrolling page body")
            for _ in range(5):
                await page.mouse.wheel(0, 500)
                await human_delay(0.5, 1.2)

        await human_delay(1, 2)

        # Primary: data-job-id attributes (most stable)
        cards = await page.locator("[data-job-id]").all()
        for card in cards:
            job_id = await card.get_attribute("data-job-id")
            if job_id and job_id.isdigit() and job_id not in job_ids:
                job_ids.append(job_id)

        # Fallback: data-occludable-job-id
        if not job_ids:
            cards = await page.locator("[data-occludable-job-id]").all()
            for card in cards:
                job_id = await card.get_attribute("data-occludable-job-id")
                if job_id and job_id.isdigit() and job_id not in job_ids:
                    job_ids.append(job_id)

        # Fallback: extract from job card link hrefs (/jobs/view/12345/)
        if not job_ids:
            links = await page.locator("a[href*='/jobs/view/']").all()
            for link in links:
                href = await link.get_attribute("href") or ""
                match = re.search(r"/jobs/view/(\d+)", href)
                if match and match.group(1) not in job_ids:
                    job_ids.append(match.group(1))

        logger.info("Found %d unique job IDs so far", len(job_ids))

        if not job_ids:
            logger.warning("No job IDs found on this page — LinkedIn may have changed layout")
            break

        # Try to go to next page
        next_btn = page.locator(
            'button[aria-label="View next page"], '
            'button[aria-label="Next"], '
            'li[class*="next"] button'
        )
        if await next_btn.count() > 0 and await next_btn.first.is_enabled():
            await next_btn.first.click()
            await human_delay(2, 4)
        else:
            break

    return job_ids


async def _safe_text(locator: Locator) -> str:
    """Get inner text from a locator, returning empty string on failure."""
    try:
        if await locator.count() > 0:
            return (await locator.first.inner_text()).strip()
    except Exception:
        pass
    return ""


async def _extract_job_details(page: Page, job_id: str) -> RawJob | None:
    """Click on a job card and extract details from the detail pane."""
    try:
        # Click on the job card
        card = page.locator(f'[data-job-id="{job_id}"]').first
        if await card.count() == 0:
            card = page.locator(f'[data-occludable-job-id="{job_id}"]').first
        if await card.count() == 0:
            card = page.locator(f'a[href*="/jobs/view/{job_id}"]').first

        if await card.count() == 0:
            logger.debug("Could not find clickable element for job %s", job_id)
            return None

        await card.click()
        await human_delay(2, 4)

        # Wait for any detail content to appear (multiple fallback selectors)
        detail_loaded = False
        for sel in [
            ".jobs-search__job-details--wrapper",
            ".job-details-jobs-unified-top-card__container--two-pane",
            ".jobs-unified-top-card",
            "[class*='job-details']",
            "[class*='jobs-details']",
        ]:
            try:
                await page.locator(sel).first.wait_for(timeout=5_000)
                detail_loaded = True
                break
            except Exception:
                continue

        if not detail_loaded:
            # Last resort: just wait a bit and try to extract anyway
            await human_delay(2, 3)

        # Title — try multiple selectors
        title = ""
        for sel in ["h1 a", "h2 a", "h1", ".t-24", "[class*='top-card'] h1", "[class*='top-card'] h2"]:
            title = await _safe_text(page.locator(sel))
            if title:
                break
        if not title:
            title = "Unknown"

        # Company
        company = ""
        for sel in [
            ".job-details-jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name",
            "[class*='company-name'] a",
            "[class*='company-name']",
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name",
        ]:
            company = await _safe_text(page.locator(sel))
            if company:
                break

        # Location
        location = ""
        for sel in [
            ".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
            ".job-details-jobs-unified-top-card__bullet",
            "[class*='top-card'] [class*='bullet']",
            "[class*='top-card'] [class*='location']",
            ".jobs-unified-top-card__bullet",
        ]:
            location = await _safe_text(page.locator(sel))
            if location:
                break

        # Description
        description = ""
        for sel in [
            ".jobs-description__content",
            ".jobs-box__html-content",
            ".jobs-description-content__text",
            "[class*='description__content']",
            "[class*='description-content']",
            "#job-details",
        ]:
            description = await _safe_text(page.locator(sel))
            if description:
                break

        # Easy Apply check
        easy_apply_btn = page.locator('button:has-text("Easy Apply")')
        is_easy_apply = await easy_apply_btn.count() > 0

        # Salary (if shown)
        salary_text = ""
        for sel in [
            "[class*='salary']",
            "[class*='compensation']",
            "[class*='Salary']",
        ]:
            salary_text = await _safe_text(page.locator(sel))
            if salary_text:
                break

        # Job URL
        job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

        # Remote check
        is_remote = bool(re.search(r"\bremote\b", f"{location} {title} {description[:500]}", re.IGNORECASE))

        # Posted time text
        posted_at_text = ""
        for sel in [
            "[class*='posted-date']",
            "[class*='posted-time']",
            ".jobs-unified-top-card__posted-date",
            "span[class*='timeago']",
            "time",
        ]:
            posted_at_text = await _safe_text(page.locator(sel))
            if posted_at_text:
                break

        logger.info("Extracted: %s @ %s (%s) [%s]", title[:50], company[:30], location[:30], posted_at_text or "no date")

        return RawJob(
            linkedin_job_id=job_id,
            title=title,
            company=company,
            location=location,
            salary_text=salary_text,
            description=description[:10_000],
            job_url=job_url,
            is_easy_apply=is_easy_apply,
            is_remote=is_remote,
            posted_at_text=posted_at_text,
        )
    except Exception as e:
        logger.warning("Failed to extract details for job %s: %s", job_id, e)
        return None


def _load_search_prefs() -> dict:
    """Load user preferences for search parameters."""
    prefs_path = Path(settings.output_dir) / "user_preferences.json"
    if prefs_path.exists():
        try:
            return json.load(prefs_path.open())
        except Exception:
            pass
    return {}


def _parse_posted_at(text: str) -> "datetime | None":
    """Parse LinkedIn's relative time text (e.g. '2 weeks ago') to a datetime."""
    from datetime import datetime, timedelta
    if not text:
        return None
    text = text.lower().strip()
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month)", text)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2)
    delta_map = {
        "second": timedelta(seconds=num),
        "minute": timedelta(minutes=num),
        "hour": timedelta(hours=num),
        "day": timedelta(days=num),
        "week": timedelta(weeks=num),
        "month": timedelta(days=num * 30),
    }
    return datetime.utcnow() - delta_map.get(unit, timedelta())


async def _persist_raw(raw: RawJob, all_jobs: list[RawJob]) -> None:
    """Deduplicate and persist a scraped job."""
    posted_at = _parse_posted_at(raw.posted_at_text)
    upsert_job(
        linkedin_job_id=raw.linkedin_job_id,
        title=raw.title,
        company=raw.company,
        location=raw.location,
        salary_text=raw.salary_text,
        description=raw.description,
        job_url=raw.job_url,
        is_easy_apply=raw.is_easy_apply,
        is_remote=raw.is_remote,
        posted_at=posted_at,
    )
    all_jobs.append(raw)


async def _scrape_search(page: Page, keyword: str, location: str,
                         all_jobs: list[RawJob], *, network_filter: bool = False) -> None:
    """Run a single keyword+location search and collect results."""
    url = _build_search_url(keyword, location, network_filter=network_filter)
    tag = f"'{keyword}' in '{location}'"
    if network_filter:
        tag += " [network]"
    logger.info("Searching: %s", tag)
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(3, 6)
    await random_mouse_movement(page)

    job_ids = await _extract_job_ids_from_list(page)
    logger.info("Found %d jobs for %s", len(job_ids), tag)

    for jid in job_ids:
        raw = await _extract_job_details(page, jid)
        if raw:
            await _persist_raw(raw, all_jobs)
            await random_mouse_movement(page)

    await human_delay(3, 6)


async def _scrape_company_page(page: Page, company_name: str,
                               all_jobs: list[RawJob]) -> None:
    """Scrape the jobs tab of a company page."""
    # Convert human name to likely slug (lowercase, hyphenated)
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    url = _build_company_jobs_url(slug)
    logger.info("Scraping company jobs: %s → %s", company_name, url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await human_delay(3, 6)

        # Company pages list jobs differently — try job cards + hrefs
        job_ids: list[str] = []
        links = await page.locator("a[href*='/jobs/view/']").all()
        for link in links:
            href = await link.get_attribute("href") or ""
            match = re.search(r"/jobs/view/(\d+)", href)
            if match and match.group(1) not in job_ids:
                job_ids.append(match.group(1))

        logger.info("Found %d jobs at %s", len(job_ids), company_name)

        for jid in job_ids:
            raw = await _extract_job_details(page, jid)
            if raw:
                await _persist_raw(raw, all_jobs)
                await random_mouse_movement(page)

        await human_delay(2, 5)
    except Exception as e:
        logger.warning("Failed to scrape company page for '%s': %s", company_name, e)


async def _resolve_connection_company(page: Page, profile_url: str) -> str | None:
    """Visit a LinkedIn profile and extract their current company name."""
    try:
        # Normalize URL
        if not profile_url.startswith("http"):
            profile_url = "https://www.linkedin.com/in/" + profile_url.strip("/")
        logger.info("Visiting connection profile: %s", profile_url)
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        await human_delay(3, 6)

        # Extract current company from headline or experience section
        for sel in [
            ".pv-text-details__right-panel-item-link span",
            "[class*='experience'] [class*='company-name']",
            "section[id='experience'] span[class*='t-14']",
            ".inline-show-more-text--is-collapsed",
        ]:
            text = await _safe_text(page.locator(sel))
            if text and len(text) < 100:
                logger.info("Connection company: %s → %s", profile_url, text)
                return text

        # Fallback: look for company link in top card
        company_link = page.locator("a[href*='/company/']").first
        if await company_link.count() > 0:
            text = (await company_link.inner_text()).strip()
            if text:
                logger.info("Connection company (from link): %s → %s", profile_url, text)
                return text
    except Exception as e:
        logger.warning("Failed to resolve company for %s: %s", profile_url, e)
    return None


async def scrape_jobs(session: LinkedInSession) -> list[RawJob]:
    """Run the full scraping pipeline: search → list → details → DB.

    Sources:
    1. Keyword search — uses desired_titles from prefs (falls back to config.job_titles)
    2. Network filter search — same keywords with LinkedIn "In Your Network" filter
    3. Target company pages — scrape jobs tab of specific companies
    4. Connection companies — visit connection profiles, then scrape their companies' jobs
    """
    await session.ensure_logged_in()
    page = session.page
    all_jobs: list[RawJob] = []

    prefs = _load_search_prefs()

    # ── 1. Keyword search ──────────────────────────────────────────
    # Prefer desired_titles from preferences over hardcoded config
    search_titles = prefs.get("desired_titles") or list(settings.job_titles)
    search_locations = list(settings.job_locations)
    home = prefs.get("home_city") or settings.home_city
    if home and home not in search_locations:
        search_locations.append(home)

    for keyword in search_titles:
        for location in search_locations:
            await _scrape_search(page, keyword, location, all_jobs)

    # ── 2. Network filter search ───────────────────────────────────
    if prefs.get("prefer_connection_companies"):
        for keyword in search_titles:
            for location in search_locations:
                await _scrape_search(page, keyword, location, all_jobs,
                                     network_filter=True)

    # ── 3. Target company pages ────────────────────────────────────
    for company in prefs.get("target_companies", []):
        await _scrape_company_page(page, company, all_jobs)

    # ── 4. Connection companies ────────────────────────────────────
    connection_urls = prefs.get("linkedin_connections", [])
    resolved_companies: set[str] = set()
    for url in connection_urls:
        company = await _resolve_connection_company(page, url)
        if company and company not in resolved_companies:
            resolved_companies.add(company)
            await _scrape_company_page(page, company, all_jobs)

    # Save resolved connection companies so the scorer can apply referral boosts
    if resolved_companies:
        prefs_path = Path(settings.output_dir) / "user_preferences.json"
        try:
            current = json.loads(prefs_path.read_text()) if prefs_path.exists() else {}
            current["_resolved_connection_companies"] = sorted(resolved_companies)
            prefs_path.write_text(json.dumps(current, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning("Could not save resolved connection companies: %s", e)

    logger.info("Scraping complete: %d total jobs found", len(all_jobs))
    return all_jobs
