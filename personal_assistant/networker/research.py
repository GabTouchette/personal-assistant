"""People research — find hiring managers, recruiters, leads at target companies."""

import logging
import re

from playwright.async_api import Page

from personal_assistant.config import settings
from personal_assistant.db.models import ContactRole, Job
from personal_assistant.db.queries import add_contact
from personal_assistant.scraper.anti_detect import human_delay, human_scroll, random_mouse_movement
from personal_assistant.scraper.auth import LinkedInSession

logger = logging.getLogger(__name__)

# Role keyword mapping
ROLE_KEYWORDS = {
    ContactRole.RECRUITER: [
        "recruiter", "talent acquisition", "recruiting", "sourcer",
    ],
    ContactRole.HIRING_MANAGER: [
        "hiring manager", "engineering manager", "dev manager",
        "director of engineering", "vp engineering",
    ],
    ContactRole.ENGINEERING_LEAD: [
        "tech lead", "staff engineer", "principal engineer",
        "senior software engineer", "lead engineer", "cto",
    ],
}


def _classify_role(title: str) -> ContactRole:
    title_lower = title.lower()
    for role, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return role
    return ContactRole.OTHER


async def find_company_contacts(
    session: LinkedInSession,
    job: Job,
    max_results: int = 5,
) -> list[dict]:
    """Search LinkedIn for relevant people at the job's company."""
    await session.ensure_logged_in()
    page = session.page
    contacts = []

    search_queries = [
        f"recruiter {job.company}",
        f"engineering manager {job.company}",
        f"software engineer {job.company}",
    ]

    for query in search_queries:
        if len(contacts) >= max_results:
            break

        url = f"https://www.linkedin.com/search/results/people/?keywords={query.replace(' ', '%20')}"
        await page.goto(url, wait_until="domcontentloaded")
        await human_delay(2, 4)
        await random_mouse_movement(page)

        # Extract people cards
        cards = await page.locator(".reusable-search__result-container").all()

        for card in cards[:3]:
            if len(contacts) >= max_results:
                break

            try:
                name_el = card.locator(".entity-result__title-text a span[aria-hidden='true']").first
                name = (await name_el.inner_text()).strip() if await name_el.count() else ""

                title_el = card.locator(".entity-result__primary-subtitle").first
                person_title = (await title_el.inner_text()).strip() if await title_el.count() else ""

                link_el = card.locator(".entity-result__title-text a").first
                profile_url = await link_el.get_attribute("href") if await link_el.count() else ""

                if not name:
                    continue

                role = _classify_role(person_title)

                contact_data = {
                    "name": name,
                    "title": person_title,
                    "role": role,
                    "linkedin_url": profile_url or "",
                    "company": job.company,
                }

                # Save to DB
                db_contact = add_contact(job.id, **contact_data)
                contact_data["id"] = db_contact.id
                contacts.append(contact_data)

                logger.info("Found contact: %s (%s) at %s", name, person_title, job.company)

            except Exception as e:
                logger.debug("Error extracting contact card: %s", e)

        await human_delay(3, 6)

    logger.info("Found %d contacts for %s", len(contacts), job.company)
    return contacts
