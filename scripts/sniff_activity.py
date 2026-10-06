#!/usr/bin/env python3
"""Open the limsg Chrome profile, sniff Recent activity, exit. READ-ONLY."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from playwright.async_api import async_playwright

from limsg_cli.api import attach_response_sniffer
from limsg_cli.browser import open_context

SEEDS = (
    "https://www.linkedin.com/in/me/recent-activity/all/",
    "https://www.linkedin.com/in/me/recent-activity/comments/",
    "https://www.linkedin.com/in/me/recent-activity/reactions/",
)


def _profile_busy() -> bool:
    proc = subprocess.run(
        ["pgrep", "-lf", "-i", "chrome.*linkedin-messages-cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    return any(ln.strip() and "pgrep" not in ln for ln in proc.stdout.splitlines())


async def _scroll(page) -> None:
    for _ in range(4):
        await page.mouse.wheel(0, 2800)
        await page.wait_for_timeout(800)


async def _vanity(page) -> str:
    return str(
        await page.evaluate(
            """() => {
              const hrefs = [...document.querySelectorAll('a')].map(a => a.href || '');
              for (const href of hrefs) {
                const m = href.match(/\\/in\\/([^/?#]+)\\/recent-activity\\//);
                if (m && m[1] && m[1] !== 'me') return m[1];
              }
              return '';
            }"""
        )
        or ""
    )


async def main() -> int:
    if _profile_busy():
        print("Chrome already using the limsg profile.", file=sys.stderr)
        return 5
    async with async_playwright() as p:
        context = await open_context(p, headless=True)
        page = context.pages[0] if context.pages else await context.new_page()
        hits = await attach_response_sniffer(page)
        try:
            await page.goto(SEEDS[1], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
            landed = page.url
            print(f"landed {landed}", file=sys.stderr)
            if any(x in landed.lower() for x in ("login", "checkpoint", "authwall", "uas/login")):
                print("AUTH WALL", file=sys.stderr)
                return 4
            vanity = await _vanity(page)
            urls = SEEDS
            if vanity:
                urls = tuple(
                    f"https://www.linkedin.com/in/{vanity}/recent-activity/{part}/"
                    for part in ("all", "comments", "reactions")
                )
            for url in urls:
                print(f"goto {url}", file=sys.stderr)
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
                await _scroll(page)
        finally:
            await context.close()
    print(f"hits {len(hits)}", file=sys.stderr)
    print(json.dumps([{k: h.get(k) for k in ("method", "status", "url_path", "queryId")} for h in hits[-30:]], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
