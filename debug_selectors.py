"""Debug script — inspect LinkedIn's current DOM structure for job search pages."""

import asyncio
import json

from personal_assistant.scraper.auth import LinkedInSession
from personal_assistant.scraper.anti_detect import human_delay


JS_INSPECT = """
() => {
    const result = {};

    // 1. Look for common list containers
    const containerSelectors = [
        '.jobs-search-results-list',
        '.scaffold-layout__list',
        '.scaffold-layout__list-container',
        '.jobs-search-results',
        '.jobs-search-results__list',
        '[class*="jobs"][class*="list"]',
        '[class*="scaffold"]',
    ];
    result.containers = {};
    for (const sel of containerSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            result.containers[sel] = {
                tag: el.tagName,
                className: el.className.substring(0, 200),
                childCount: el.children.length,
            };
        }
    }

    // 2. Job cards by data attributes
    const dataJobId = document.querySelectorAll('[data-job-id]');
    result.dataJobIdCount = dataJobId.length;
    if (dataJobId.length > 0) {
        result.firstDataJobId = {
            tag: dataJobId[0].tagName,
            className: dataJobId[0].className.substring(0, 200),
            jobId: dataJobId[0].getAttribute('data-job-id'),
        };
    }

    const occludable = document.querySelectorAll('[data-occludable-job-id]');
    result.occludableJobIdCount = occludable.length;
    if (occludable.length > 0) {
        result.firstOccludable = {
            tag: occludable[0].tagName,
            className: occludable[0].className.substring(0, 200),
            jobId: occludable[0].getAttribute('data-occludable-job-id'),
        };
    }

    // 3. Job card elements
    const cardSelectors = [
        '[class*="job-card"]',
        '[class*="jobs-search"]',
        'li[class*="job"]',
        '.job-card-container',
        '.job-card-list__entity-lockup',
    ];
    result.cards = {};
    for (const sel of cardSelectors) {
        const els = document.querySelectorAll(sel);
        if (els.length > 0) {
            result.cards[sel] = {
                count: els.length,
                firstTag: els[0].tagName,
                firstClass: els[0].className.substring(0, 200),
            };
        }
    }

    // 4. Detail pane
    const detailSelectors = [
        '.jobs-search__job-details--wrapper',
        '.job-details-jobs-unified-top-card__container--two-pane',
        '.jobs-unified-top-card',
        '[class*="job-details"]',
        '[class*="jobs-details"]',
    ];
    result.detailPanes = {};
    for (const sel of detailSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            result.detailPanes[sel] = {
                tag: el.tagName,
                className: el.className.substring(0, 200),
            };
        }
    }

    // 5. Get the scrollable parent of job list
    const allULs = document.querySelectorAll('ul');
    result.ulElements = [];
    for (const ul of allULs) {
        if (ul.children.length > 3 && ul.scrollHeight > 500) {
            result.ulElements.push({
                className: ul.className.substring(0, 200),
                childCount: ul.children.length,
                scrollHeight: ul.scrollHeight,
                parentClass: ul.parentElement ? ul.parentElement.className.substring(0, 200) : null,
            });
        }
    }

    // 6. Sample the page URL
    result.url = window.location.href;

    return result;
}
"""


async def main():
    session = LinkedInSession()
    await session.start()
    await session.ensure_logged_in()
    page = session.page

    url = "https://www.linkedin.com/jobs/search/?keywords=Software+Engineer&location=Montreal&f_TPR=r86400&sortBy=DD"
    print("Navigating to jobs search...")
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(4, 6)

    # Take screenshot
    await page.screenshot(path="output/debug_jobs_page.png", full_page=False)
    print("Screenshot saved to output/debug_jobs_page.png")

    # Inspect DOM
    data = await page.evaluate(JS_INSPECT)
    print(json.dumps(data, indent=2))

    # Also try clicking the first job card if any exist
    if data.get("dataJobIdCount", 0) > 0:
        jid = data["firstDataJobId"]["jobId"]
        print(f"\nClicking first job card (ID: {jid})...")
        card = page.locator(f'[data-job-id="{jid}"]').first
        await card.click()
        await human_delay(2, 3)

        # Inspect detail pane after click
        detail_data = await page.evaluate("""
        () => {
            const result = {};
            // Look for detail content after clicking
            const selectors = [
                '.jobs-search__job-details--wrapper',
                '.job-details-jobs-unified-top-card__container--two-pane',
                '.jobs-unified-top-card',
                '[class*="job-details"]',
                '[class*="jobs-details"]',
                '.jobs-description',
                '[class*="description"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    result[sel] = {
                        tag: el.tagName,
                        className: el.className.substring(0, 200),
                    };
                }
            }
            // title
            const h1 = document.querySelector('h1');
            if (h1) result.h1 = h1.innerText.substring(0, 100);
            const h2 = document.querySelector('h2');
            if (h2) result.h2 = h2.innerText.substring(0, 100);

            // Easy Apply
            const easyApply = document.querySelector('button[class*="easy"], button span');
            result.buttons = [];
            document.querySelectorAll('button').forEach(b => {
                const text = b.innerText.trim();
                if (text.length > 0 && text.length < 50) {
                    result.buttons.push(text);
                }
            });
            return result;
        }
        """)
        print("\nDetail pane after click:")
        print(json.dumps(detail_data, indent=2))

    await session.close()


asyncio.run(main())
