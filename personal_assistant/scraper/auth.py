"""LinkedIn authentication with Playwright — cookie persistence & session refresh."""

import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from personal_assistant.config import settings
from personal_assistant.scraper.anti_detect import human_delay, human_type

logger = logging.getLogger(__name__)

STORAGE_STATE_PATH = Path(settings.browser_data_dir) / "storage_state.json"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"


class LinkedInSession:
    """Manages a persistent Playwright browser session for LinkedIn."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> "LinkedInSession":
        Path(settings.browser_data_dir).mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,  # visible for 2FA / initial login
            args=["--disable-blink-features=AutomationControlled"],
        )
        storage = str(STORAGE_STATE_PATH) if STORAGE_STATE_PATH.exists() else None
        self._context = await self._browser.new_context(
            storage_state=storage,
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        return self

    @property
    def page(self) -> Page:
        assert self._page is not None, "Session not started"
        return self._page

    async def is_logged_in(self) -> bool:
        """Navigate to feed and check if we're actually logged in."""
        try:
            await self._page.goto(FEED_URL, wait_until="domcontentloaded", timeout=15_000)
            await human_delay(1, 2)
            url = self._page.url
            return "/feed" in url and "/login" not in url
        except Exception:
            return False

    async def login(self) -> None:
        """Perform LinkedIn login via Google OAuth or email/password.

        Since you sign in with Google, the flow is:
        1. Go to LinkedIn login
        2. Click "Sign in with Google"
        3. Google OAuth handles auth (may use saved Google cookies)
        4. If Google requires interaction, wait for you to complete it
        5. Save session cookies once on /feed

        If LINKEDIN_PASSWORD is set, falls back to email/password login.
        """
        if await self.is_logged_in():
            logger.info("Already logged in via saved session")
            return

        logger.info("Logging in to LinkedIn...")
        await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await human_delay()

        # Try Google OAuth first (if no password configured)
        if not settings.linkedin_password:
            await self._login_with_google()
        else:
            await self._login_with_password()

        await self._save_session()
        logger.info("Login successful, session saved")

    async def _login_with_google(self) -> None:
        """Click 'Sign in with Google' and wait for the user to complete OAuth."""
        logger.info("Using Google OAuth sign-in...")

        # Click the Google sign-in button
        google_btn = self._page.locator(
            'button[data-litms-control-urn="login-submit-google"], '
            'a[href*="google"], '
            'button:has-text("Sign in with Google"), '
            'div.google-btn, '
            '[data-provider="google"]'
        ).first

        if await google_btn.count() > 0:
            await google_btn.click()
            await human_delay(2, 4)
        else:
            # Google button might be rendered differently — look for it by image/text
            alt_btn = self._page.locator('img[alt*="Google"], span:has-text("Google")').first
            if await alt_btn.count() > 0:
                await alt_btn.click()
                await human_delay(2, 4)
            else:
                logger.warning(
                    "Google sign-in button not found. "
                    "Please sign in manually in the browser..."
                )

        # Wait for either: Google account picker, Google login, or LinkedIn feed
        # The browser is visible so the user can interact with Google OAuth
        logger.info(
            "⏳ Waiting for Google OAuth to complete. "
            "If prompted, sign in with your Google account in the browser. "
            "You have 120 seconds..."
        )
        try:
            await self._page.wait_for_url(
                "**/feed/**", timeout=120_000
            )
        except Exception:
            # Check if we ended up on a challenge page
            if await self._is_challenge_page():
                logger.warning("LinkedIn verification required after Google sign-in...")
                await self._page.wait_for_url("**/feed/**", timeout=120_000)
            elif "/feed" not in self._page.url:
                raise RuntimeError(
                    "Google OAuth not completed in time. "
                    "Run again — your Google session may be cached next time."
                )

    async def _login_with_password(self) -> None:
        """Traditional email/password login."""
        await human_type(self._page, "#username", settings.linkedin_email)
        await human_delay(0.5, 1.0)
        await human_type(self._page, "#password", settings.linkedin_password)
        await human_delay(0.5, 1.0)

        await self._page.click('button[type="submit"]')
        await human_delay(2, 4)

        # Check for 2FA / verification challenge
        if await self._is_challenge_page():
            logger.warning(
                "⚠️  LinkedIn 2FA / verification required. "
                "Complete it in the browser within 120 seconds..."
            )
            try:
                await self._page.wait_for_url("**/feed/**", timeout=120_000)
            except Exception:
                raise RuntimeError(
                    "2FA not completed in time. Please run again and complete verification."
                )

    async def _is_challenge_page(self) -> bool:
        url = self._page.url
        challenge_indicators = ["/checkpoint/", "/challenge/", "security-verification"]
        return any(ind in url for ind in challenge_indicators)

    async def _save_session(self) -> None:
        state = await self._context.storage_state()
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORAGE_STATE_PATH.write_text(json.dumps(state))
        logger.debug("Storage state saved to %s", STORAGE_STATE_PATH)

    async def ensure_logged_in(self) -> None:
        """Re-login if session has expired."""
        if not await self.is_logged_in():
            logger.info("Session expired, re-authenticating...")
            await self.login()
        else:
            await self._save_session()  # refresh cookies

    async def close(self) -> None:
        if self._context:
            await self._save_session()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser session closed")
