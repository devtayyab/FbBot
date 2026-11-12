"""Shared Playwright browser using your own logged-in Facebook profile."""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

from .config import PROFILE_DIR, get_profile_dir


class NotLoggedIn(RuntimeError):
    pass


@contextmanager
def open_browser(agent_id: str | None = None, headless: bool = False, channel: str = "chrome"):
    """Yield a persistent browser context for a specific agent.

    channel="chrome" runs your installed Google Chrome with its own bot profile,
    so you log in to your real Facebook account once and it stays logged in.
    Each agent has its own profile directory under data/profiles/<agent_id>/.
    """
    from playwright.sync_api import sync_playwright

    profile_dir = get_profile_dir(agent_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=None if channel in ("", "chromium") else channel,
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            yield ctx
        finally:
            ctx.close()


def ensure_logged_in(page) -> None:
    """Stop the run if Facebook shows a login page or a security checkpoint."""
    url = page.url
    if "/checkpoint" in url:
        raise NotLoggedIn("Facebook shows a security checkpoint. Open it with `python bot.py login` and clear it.")
    if "/login" in url or (page.locator('input[name="pass"]').count() and page.locator('input[name="email"]').count()):
        raise NotLoggedIn("Not logged in to Facebook. Run `python bot.py login` first.")
