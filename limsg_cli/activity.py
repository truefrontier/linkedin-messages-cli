"""Read-only LinkedIn recent activity.

The signed-in activity lists are the Recent activity documents sniffed from
the limsg Chrome profile (not messaging GraphQL):

  GET /in/{vanity}/recent-activity/comments/
  GET /in/{vanity}/recent-activity/reactions/
  GET /in/{vanity}/recent-activity/all/

`/in/me/recent-activity/...` redirects the shell but does not render the list.
The vanity URL is taken from links on that page. This module only GETs those
documents and reads the rendered cards. It never posts, messages, or calls
createMessage.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from limsg_cli.api import attach_response_sniffer
from limsg_cli.browser import has_session, open_context
from limsg_cli.capture import save_capture

ME_ACTIVITY = "https://www.linkedin.com/in/me/recent-activity/comments/"

KIND_PATH = {
    "comments": "comments",
    "reactions": "reactions",
    "posts": "all",
}
KIND_FEED = {
    "comments": "PROFILE_COMMENTS",
    "reactions": "PROFILE_REACTIONS",
    "posts": "CREATOR_PROFILE_ALL_CONTENT_VIEW",
}

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_REL_TIME = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_ABS_TIME = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:,\s*(\d{4}))?$",
    re.I,
)
_TIME_LINE = re.compile(
    r"^(\d+\s*[smhdw]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s*\d{4})?)\s*•?$",
    re.I,
)
_URN = re.compile(r"urn:li:[A-Za-z0-9_:-]+")
_NOISE = re.compile(
    r"^(feed post|follow|following|load more comments|… more|\.\.\. more|"
    r"•\s*(you|1st|2nd|3rd|following)|you|"
    r"\d+|[\d,]+\s+impressions?|book an appointment|"
    r"this post type can't be boosted\.?)$",
    re.I,
)
_END = re.compile(r"^(… more|\.\.\. more|load more comments|\d+\s+impressions?)$", re.I)


class AuthRequired(RuntimeError):
    pass


class ProfileBusy(RuntimeError):
    pass


def assert_profile_idle() -> None:
    proc = subprocess.run(
        ["pgrep", "-lf", "-i", "chrome.*linkedin-messages-cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip() and "pgrep" not in ln]
    if lines:
        raise ProfileBusy(
            "Chrome is already using ~/.linkedin-messages-cli/chrome-profile/. Close it and retry."
        )


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Invalid --since. Use ISO-8601, for example 2026-10-01 or 2026-10-01T00:00:00Z.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def linkedin_time_to_iso(token: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    text = (token or "").replace("•", "").strip()
    rel = _REL_TIME.fullmatch(text)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return _iso(now - delta)
    abs_m = _ABS_TIME.fullmatch(text)
    if abs_m:
        year = int(abs_m.group(3) or now.year)
        month = _MONTHS[abs_m.group(1).lower()[:3]]
        day = int(abs_m.group(2))
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return text
        if abs_m.group(3) is None and dt > now + timedelta(days=1):
            dt = dt.replace(year=year - 1)
        return _iso(dt)
    return text


def _is_time_line(line: str) -> bool:
    return bool(_TIME_LINE.fullmatch(line.strip()))


def _clean_time_token(line: str) -> str:
    return line.replace("•", "").strip()


def _is_noise(line: str) -> bool:
    return bool(_NOISE.fullmatch(line.strip()))


def _is_end(line: str) -> bool:
    text = line.strip()
    if _END.fullmatch(text):
        return True
    if re.fullmatch(r"\d+", text):
        return True
    return False


def _you_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if low in {"• you", "you"} or low.endswith("• you"):
            return i
    return None


def _text_after_own_time(lines: list[str]) -> tuple[str, str]:
    """Return (time token, text) from the viewer's block on a card."""
    you = _you_index(lines)
    if you is None:
        return "", ""
    time_token = ""
    seen_time = False
    chunks: list[str] = []
    for line in lines[you + 1 :]:
        if not seen_time:
            if _is_time_line(line):
                time_token = _clean_time_token(line)
                seen_time = True
            continue
        if _is_end(line) or _is_noise(line):
            break
        chunks.append(line)
    return time_token, "\n".join(chunks).strip()


def _first_time(lines: list[str]) -> str:
    for line in lines:
        if _is_time_line(line):
            return _clean_time_token(line)
    return ""


def _content_after_first_time(lines: list[str]) -> str:
    seen = False
    chunks: list[str] = []
    for line in lines:
        if not seen:
            if _is_time_line(line):
                seen = True
            continue
        if _is_end(line) or _is_noise(line):
            if chunks:
                break
            continue
        chunks.append(line)
        if len(chunks) >= 8:
            break
    return "\n".join(chunks).strip()


def _reaction_label(lines: list[str]) -> str:
    blob = " ".join(lines[:8]).lower()
    for phrase, label in (
        ("found this funny", "funny"),
        ("finds this funny", "funny"),
        ("found this insightful", "insightful"),
        ("finds this insightful", "insightful"),
        ("celebrates", "celebrate"),
        ("celebrate", "celebrate"),
        ("supports", "support"),
        ("supported", "support"),
        ("loved", "love"),
        ("loves", "love"),
        ("likes", "like"),
        ("liked", "like"),
    ):
        if phrase in blob:
            return label
    return ""


def _target(links: list[dict[str, str]]) -> tuple[str, str]:
    pairs = _link_pairs(links)
    preferred = ("/feed/update/", "/pulse/", "/posts/")
    for href, text in pairs:
        if any(part in href for part in preferred):
            return href, text
    social: list[tuple[str, str]] = []
    for href, text in pairs:
        if "/recent-activity/" in href or "/safety/" in href:
            continue
        if "/in/" in href or "/company/" in href:
            social.append((href, text))
    # The viewer's profile is usually the first link. The next person or company is the target.
    if len(social) >= 2:
        href, text = social[1]
        if not text:
            text = next((label for link, label in social if link == href and label), "")
        return href, text
    if social:
        return social[0]
    return "", ""


def _link_pairs(links: list[dict[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for link in links:
        href = (link.get("href") or "").strip()
        if not href or href.startswith("javascript:"):
            continue
        if href.startswith("/"):
            href = urljoin("https://www.linkedin.com", href)
        text = re.sub(r"\s+", " ", (link.get("text") or "").strip())
        out.append((href, text))
    return out


def _stable_id(raw_id: str, href: str) -> str:
    match = _URN.search(href or "")
    if match:
        return match.group(0)
    token = raw_id or ""
    token = re.sub(r"^expanded", "", token)
    token = re.sub(r"FeedType_.*$", "", token)
    return token or href


def normalize_card(kind: str, card: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    lines = [str(x).strip() for x in (card.get("lines") or []) if str(x).strip()]
    links = card.get("links") or []
    target_url, target_title = _target(links)
    own_time, own_text = _text_after_own_time(lines)
    post_url = ""
    for href, _text in _link_pairs(links):
        if "/feed/update/" in href or "/pulse/" in href or "/posts/" in href:
            post_url = href
            break
    row_id = _stable_id(str(card.get("id") or ""), post_url or target_url)
    if kind == "comments":
        when = own_time or _first_time(lines)
        text = own_text or _content_after_first_time(lines)
        return {
            "id": row_id,
            "time": linkedin_time_to_iso(when, now=now) if when else "",
            "type": "comment",
            "text": text,
            "targetUrl": target_url,
            "targetTitle": target_title,
        }
    if kind == "reactions":
        when = _first_time(lines)
        content_title = _content_after_first_time(lines).split("\n", 1)[0]
        if content_title:
            target_title = content_title
        return {
            "id": row_id,
            "time": linkedin_time_to_iso(when, now=now) if when else "",
            "type": "reaction",
            "reaction": _reaction_label(lines),
            "targetUrl": target_url,
            "targetTitle": target_title,
        }
    when = own_time or _first_time(lines)
    text = own_text or _content_after_first_time(lines)
    return {
        "id": row_id,
        "time": linkedin_time_to_iso(when, now=now) if when else "",
        "type": "post",
        "text": text,
        "url": post_url or target_url,
    }


def apply_since(rows: list[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
    if since is None:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        stamp = str(row.get("time") or "")
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= since:
            kept.append(row)
    return kept


_CARD_JS = """
(feedType) => {
  const cards = [...document.querySelectorAll('div[id^="expanded"][id*="FeedType_"]')]
    .filter(el => el.id.includes(feedType));
  const seen = new Set();
  const out = [];
  for (const card of cards) {
    if (seen.has(card.id)) continue;
    seen.add(card.id);
    const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    const links = [...card.querySelectorAll('a')].map(a => ({
      href: (a.getAttribute('href') || '').split('?')[0],
      text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 180),
    })).filter(l => l.href);
    out.push({id: card.id, lines, links});
  }
  return out;
}
"""


async def _read_cards(page, feed_type: str) -> list[dict[str, Any]]:
    data = await page.evaluate(_CARD_JS, feed_type)
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


async def _scroll_for(page, feed_type: str, limit: int) -> list[dict[str, Any]]:
    cards = await _read_cards(page, feed_type)
    stable = 0
    while len(cards) < limit and stable < 2:
        before = len(cards)
        await page.mouse.wheel(0, 3200)
        await page.evaluate(
            """() => {
              const el = document.scrollingElement || document.body;
              window.scrollTo(0, el.scrollHeight);
            }"""
        )
        await page.wait_for_timeout(1200)
        cards = await _read_cards(page, feed_type)
        if len(cards) <= before:
            stable += 1
        else:
            stable = 0
    return cards


def _auth_wall(url: str) -> bool:
    low = url.lower()
    return any(token in low for token in ("login", "checkpoint", "authwall", "uas/login"))


async def _discover_vanity(page) -> str:
    await page.goto(ME_ACTIVITY, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1500)
    if _auth_wall(page.url):
        raise AuthRequired("LinkedIn session expired or not signed in.")
    vanity = await page.evaluate(
        """() => {
          const hrefs = [...document.querySelectorAll('a')]
            .map(a => a.getAttribute('href') || a.href || '');
          for (const href of hrefs) {
            const m = href.match(/\\/in\\/([^/?#]+)\\/recent-activity\\//);
            if (m && m[1] && m[1] !== 'me') return m[1];
          }
          const path = location.pathname.match(/^\\/in\\/([^/]+)\\//);
          if (path && path[1] !== 'me') return path[1];
          return '';
        }"""
    )
    vanity = str(vanity or "").strip()
    if not vanity or vanity == "me":
        raise RuntimeError(
            "Could not resolve the signed-in profile activity URL. Run: limsg login"
        )
    return vanity


async def _open_kind(page, vanity: str, kind: str) -> str:
    url = f"https://www.linkedin.com/in/{vanity}/recent-activity/{KIND_PATH[kind]}/"
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    if _auth_wall(page.url):
        raise AuthRequired("LinkedIn session expired or not signed in.")
    save_capture(
        kind=f"activity_{kind}",
        url=page.url,
        method="GET",
        status=200,
        response_shape={"feedType": KIND_FEED[kind], "path": KIND_PATH[kind]},
    )
    return page.url


async def _collect(page, kind: str, *, limit: int, since: datetime | None) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    target = limit
    if since is not None:
        target = max(limit, 40)
    cards = await _scroll_for(page, KIND_FEED[kind], target)
    rows = [normalize_card(kind, card, now=now) for card in cards]
    rows = [row for row in rows if row.get("text") or row.get("reaction") or row.get("targetTitle") or row.get("url")]
    # Drop empty shells (replaceable placeholders have no lines).
    rows = apply_since(rows, since)
    return rows[:limit]


async def fetch_activity(
    kind: str,
    *,
    limit: int = 20,
    since: datetime | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    if kind not in KIND_PATH:
        raise RuntimeError(f"Unknown activity kind {kind}")
    if not has_session():
        raise AuthRequired("No LinkedIn session found.")
    assert_profile_idle()
    async with async_playwright() as p:
        context = await open_context(p, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        await attach_response_sniffer(page)
        try:
            vanity = await _discover_vanity(page)
            await _open_kind(page, vanity, kind)
            return await _collect(page, kind, limit=limit, since=since)
        finally:
            await context.close()


async def fetch_export(
    *,
    limit: int = 20,
    since: datetime | None = None,
    headless: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    if not has_session():
        raise AuthRequired("No LinkedIn session found.")
    assert_profile_idle()
    async with async_playwright() as p:
        context = await open_context(p, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        await attach_response_sniffer(page)
        try:
            vanity = await _discover_vanity(page)
            out: dict[str, list[dict[str, Any]]] = {}
            for kind in ("comments", "reactions", "posts"):
                await _open_kind(page, vanity, kind)
                out[kind] = await _collect(page, kind, limit=limit, since=since)
            return out
        finally:
            await context.close()
