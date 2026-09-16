"""Chrome persistent profile helpers for LinkedIn messaging."""

from __future__ import annotations

from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

HOME = Path.home() / ".linkedin-messages-cli"
PROFILE = HOME / "chrome-profile"
CAPTURE = HOME / "capture"
STEALTH = ["--disable-blink-features=AutomationControlled"]
MESSAGING_URL = "https://www.linkedin.com/messaging/"


def ensure_dirs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    CAPTURE.mkdir(parents=True, exist_ok=True)


def has_session() -> bool:
    if not PROFILE.exists():
        return False
    # Cookie DB or Preferences usually appear after a real Chrome session.
    markers = (
        "Default/Cookies",
        "Default/Network/Cookies",
        "Default/Preferences",
        "Local State",
    )
    return any((PROFILE / m).exists() for m in markers) or any(PROFILE.iterdir())


async def open_context(
    playwright: "Playwright",
    *,
    headless: bool,
) -> "BrowserContext":
    ensure_dirs()
    return await playwright.chromium.launch_persistent_context(
        str(PROFILE),
        headless=headless,
        channel="chrome",
        args=STEALTH,
        viewport={"width": 1280, "height": 900},
    )


def csrf_from_cookies(cookies: list[dict]) -> str | None:
    for c in cookies:
        if c.get("name") == "JSESSIONID":
            val = (c.get("value") or "").strip().strip('"')
            return val or None
    return None


def voyager_headers(csrf: str) -> dict[str, str]:
    return {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "csrf-token": csrf,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-page-instance": "urn:li:page:messaging_index;",
    }
