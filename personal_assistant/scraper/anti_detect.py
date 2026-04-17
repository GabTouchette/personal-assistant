"""Human-like delay and anti-detection utilities for Playwright."""

import asyncio
import logging
import random

from personal_assistant.config import settings

logger = logging.getLogger(__name__)


async def human_delay(minimum: float | None = None, maximum: float | None = None) -> None:
    """Sleep for a randomized duration to mimic human behaviour."""
    lo = minimum or settings.min_delay_seconds
    hi = maximum or settings.max_delay_seconds
    delay = random.uniform(lo, hi)
    logger.debug("Sleeping %.1fs", delay)
    await asyncio.sleep(delay)


async def human_scroll(page, scrolls: int = 3) -> None:
    """Scroll down the page in a human-like pattern."""
    for _ in range(scrolls):
        distance = random.randint(300, 700)
        await page.mouse.wheel(0, distance)
        await human_delay(0.5, 1.5)


async def human_type(page, selector: str, text: str) -> None:
    """Type text into an input field with per-character delays."""
    await page.click(selector)
    await human_delay(0.3, 0.6)
    for char in text:
        await page.keyboard.type(char, delay=random.randint(50, 150))


async def random_mouse_movement(page) -> None:
    """Move mouse to a random viewport position."""
    vp = page.viewport_size or {"width": 1280, "height": 720}
    x = random.randint(100, vp["width"] - 100)
    y = random.randint(100, vp["height"] - 100)
    await page.mouse.move(x, y)
    await human_delay(0.2, 0.5)
