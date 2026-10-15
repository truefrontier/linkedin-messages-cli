"""Publish a LinkedIn post (text + exactly one image) from the signed-in personal account.

Dry-run is the default. A dry-run validates the inputs and makes one read:

  GET  /voyager/api/me                                                   (login check + author)

Only `limsg post create --confirm` (without --dry-run) makes the three writes, in order:

  POST /voyager/api/voyagerVideoDashMediaUploadMetadata?action=upload    (register image)
  PUT  {singleUploadUrl}                                                 (image bytes)
  POST /voyager/api/contentcreation/normShares                           (create post)

Every call is an in-page fetch from https://www.linkedin.com/robots.txt (same origin,
no app JS) with the profile's own cookies. Writes never follow redirects and never retry.

Shapes match LinkedIn's share box as reported by OSS Voyager clients (gabros20/linkedin-relay:
normShares verified 201 on 2026-08-13, register verified 2026-09-25; bcharleson/linkedincli).
"""

from __future__ import annotations

import base64
import json
import re
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from limsg_cli.activity import AuthRequired, _auth_wall, assert_profile_idle
from limsg_cli.api import _page_fetch
from limsg_cli.browser import csrf_from_cookies, has_session, open_context, voyager_headers

MAX_TEXT = 3000
# LinkedIn's share box accepts larger images, but 10 MB keeps the single in-page upload small.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

VOYAGER = "https://www.linkedin.com/voyager/api"
ME_URL = f"{VOYAGER}/me"
REGISTER_URL = f"{VOYAGER}/voyagerVideoDashMediaUploadMetadata?action=upload"
CREATE_URL = f"{VOYAGER}/contentcreation/normShares"
LANDING_URL = "https://www.linkedin.com/robots.txt"
FEED_UPDATE = "https://www.linkedin.com/feed/update/{urn}/"
AUTH_MESSAGE = "LinkedIn session expired or not signed in."

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_POST_URN = re.compile(r"urn:li:(?:share|ugcPost|activity):\d+")


# ---------------------------------------------------------------- validation (no network)


def utf16_len(text: str) -> int:
    """Length as LinkedIn's composer counts it (UTF-16 code units, so an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def load_text(text: str | None, text_file: str | None) -> str:
    if (text is None) == (text_file is None):
        raise ValueError("Pass exactly one of --text or --text-file.")
    if text_file is not None:
        try:
            text = Path(text_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot read --text-file {text_file}: {exc}") from exc
    text = text.strip()
    if not text:
        raise ValueError("Post text is empty.")
    n = utf16_len(text)
    if n > MAX_TEXT:
        raise ValueError(f"Post text is {n} characters; LinkedIn allows {MAX_TEXT}.")
    return text


def _dimensions(mime: str, data: bytes) -> tuple[int, int] | None:
    if mime == "image/png" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if mime == "image/gif" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if mime == "image/jpeg":
        i = 2
        while i + 9 < len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
            elif marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone, no length
                i += 2
            # SOF0..SOF15 carry the frame size, except DHT (C4), JPG (C8), DAC (CC).
            elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            else:
                i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None


def load_image(path: str) -> dict[str, Any]:
    """Validate one image and return {path, filename, type, bytes, width, height, data}."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"Image not found: {path}")
    with p.open("rb") as fh:
        data = fh.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise ValueError(f"Image is empty: {path}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is over {MAX_IMAGE_BYTES} bytes (10 MB): {path}")
    mime = next((m for magic, m in _MAGIC if data.startswith(magic)), None)
    if mime is None:
        raise ValueError(f"Image must be JPEG, PNG, or GIF (checked file contents): {path}")
    dims = _dimensions(mime, data)
    return {
        "path": str(p.resolve()),
        "filename": p.name,
        "type": mime,
        "bytes": len(data),
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "data": data,
    }


def image_summary(image: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in image.items() if k != "data"}


# ---------------------------------------------------------------- response parsing


def _value(data: Any) -> dict:
    """Restli action/record payload, whether or not the normalized response wraps it in data."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    value = inner.get("value")
    return value if isinstance(value, dict) else inner


def parse_me(data: Any) -> dict[str, Any] | None:
    """Author from GET /voyager/api/me, or None when the payload has no profile."""
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    candidates = [o for o in data.get("included") or [] if isinstance(o, dict)]
    if isinstance(inner.get("miniProfile"), dict):
        candidates.append(inner["miniProfile"])
    profile = next((o for o in candidates if o.get("publicIdentifier")), None)
    if profile is None:
        return None
    urn = str(profile.get("dashEntityUrn") or profile.get("entityUrn") or "")
    plain_id = inner.get("plainId")
    return {
        "name": f"{profile.get('firstName') or ''} {profile.get('lastName') or ''}".strip(),
        "publicIdentifier": profile.get("publicIdentifier"),
        "profileUrn": urn.replace("fs_miniProfile", "fsd_profile") or None,
        "memberUrn": profile.get("objectUrn") or (f"urn:li:member:{plain_id}" if plain_id else None),
    }


def parse_register(data: Any) -> tuple[str, str, dict[str, str]]:
    """(asset urn, upload url, extra upload headers) from the register response."""
    value = _value(data)
    if value.get("type") not in (None, "SINGLE"):
        raise RuntimeError(f"LinkedIn asked for a {value['type']} upload, which limsg does not do. Nothing was posted.")
    urn = value.get("urn")
    url = value.get("singleUploadUrl")
    if not urn or not url:
        raise RuntimeError("Image register response had no asset urn or upload URL. Nothing was posted.")
    headers = value.get("singleUploadHeaders")
    return str(urn), str(url), {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}


def parse_created_urn(data: Any, text: str, restli_id: str | None = None) -> str | None:
    """Post urn from the create response.

    data.status.urn is where text-only clients read it, then the Rest.li x-restli-id header.
    The body of an image post has not been published anywhere, so last comes the first post
    urn in the body, after removing the echoed post text (which may itself contain a urn).
    """
    value = _value(data)
    for holder in (value.get("status"), value):
        if isinstance(holder, dict):
            m = _POST_URN.fullmatch(str(holder.get("urn") or ""))
            if m:
                return m.group(0)
    m = _POST_URN.fullmatch(unquote(restli_id or ""))
    if m:
        return m.group(0)
    if not isinstance(data, dict):
        return None
    blob = json.dumps(data).replace(json.dumps(text)[1:-1], "")
    m = _POST_URN.search(blob)
    return m.group(0) if m else None


def share_body(text: str, asset_urn: str) -> dict[str, Any]:
    return {
        "visibleToConnectionsOnly": False,
        "externalAudienceProviders": [],
        "commentaryV2": {"text": text, "attributes": []},
        "origin": "FEED",
        "allowedCommentersScope": "ALL",
        "postState": "PUBLISHED",
        "media": [{"category": "IMAGE", "mediaUrn": asset_urn, "tapTargets": []}],
    }


def post_url(urn: str) -> str:
    return FEED_UPDATE.format(urn=urn)


# ---------------------------------------------------------------- network


_WRITE_JS = """
async ({ url, method, headers, json, b64 }) => {
  const opts = { method, headers, credentials: 'include', redirect: 'error' };
  if (json !== null) opts.body = JSON.stringify(json);
  if (b64 !== null) opts.body = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  try {
    const res = await fetch(url, opts);
    return { status: res.status, restliId: res.headers.get('x-restli-id'), text: await res.text() };
  } catch (e) {
    return { status: 0, text: '' };  // network error or a redirect: outcome unknown
  }
}
"""


async def fetch_author(page) -> dict[str, Any]:
    """Read-only login check. Raises AuthRequired unless /me returns a profile."""
    if not csrf_from_cookies(await page.context.cookies()):
        raise AuthRequired(AUTH_MESSAGE)
    status, data = await _page_fetch(page, ME_URL)
    author = parse_me(data) if status == 200 else None
    if author is None:
        raise AuthRequired(f"{AUTH_MESSAGE} (GET /voyager/api/me HTTP {status})")
    return author


async def _write(page, url: str, *, method: str = "POST", json_body: Any = None, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, Any, str | None]:
    """One write request, sent once. Returns (status, JSON or None, x-restli-id); status 0 = no response."""
    csrf = csrf_from_cookies(await page.context.cookies()) or ""
    hdrs = {**voyager_headers(csrf), "content-type": "application/json"} if json_body is not None else {"csrf-token": csrf}
    hdrs.update(headers or {})
    try:
        result = await page.evaluate(
            _WRITE_JS,
            {
                "url": url,
                "method": method,
                "headers": hdrs,
                "json": json_body,
                "b64": base64.b64encode(data).decode() if data is not None else None,
            },
        )
    except Exception:  # browser died mid-request: same as no response, outcome unknown
        return 0, None, None
    try:
        parsed = json.loads(result.get("text") or "null")
    except json.JSONDecodeError:
        parsed = None
    return int(result.get("status") or 0), parsed, result.get("restliId")


def _check_upload_url(url: str) -> None:
    parts = urlparse(url)
    host = parts.hostname or ""
    # Chrome reads a backslash as "/", so https://evil\@www.linkedin.com/ goes to evil; urlparse disagrees.
    odd = "\\" in url or any(c.isspace() or ord(c) < 32 for c in url) or parts.netloc.lower() != host
    if odd or parts.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise RuntimeError(f"Refusing image upload to unexpected URL host {parts.netloc!r}. Nothing was posted.")


async def publish(page, text: str, image: dict[str, Any]) -> dict[str, Any]:
    """Register -> upload -> create. Each step runs once; a failure stops before the next write."""
    status, data, _ = await _write(
        page,
        REGISTER_URL,
        json_body={"mediaUploadType": "IMAGE_SHARING", "fileSize": image["bytes"], "filename": image["filename"]},
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"Image register failed HTTP {status}. Nothing was posted.")
    asset_urn, upload_url, extra = parse_register(data)

    _check_upload_url(upload_url)
    status, _, _ = await _write(
        page, upload_url, method="PUT", data=image["data"], headers={"content-type": image["type"], **extra}
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"Image upload failed HTTP {status}. Nothing was posted.")

    status, data, restli_id = await _write(page, CREATE_URL, json_body=share_body(text, asset_urn))
    if 400 <= status < 500:
        raise RuntimeError(f"LinkedIn rejected the post (HTTP {status}). Nothing was posted.")
    if not 200 <= status < 300:
        raise RuntimeError(
            f"Create post got {'no response' if status == 0 else f'HTTP {status}'}. The post may be live; "
            "check your profile before retrying."
        )
    urn = parse_created_urn(data, text, restli_id)
    out: dict[str, Any] = {"urn": urn, "url": post_url(urn) if urn else None, "assetUrn": asset_urn}
    if not urn:
        # 2xx means LinkedIn accepted it. Reporting failure here would invite a second post.
        out["warning"] = (
            f"LinkedIn accepted the post (HTTP {status}) but the response had no post urn. "
            "Check your profile; do NOT re-run or it will post twice."
        )
    return out


async def run(page, text: str, image: dict[str, Any], *, publish_now: bool) -> dict[str, Any]:
    """Login check, then either the dry-run preview or the publish result."""
    author = await fetch_author(page)
    out: dict[str, Any] = {
        "dryRun": not publish_now,
        "loggedIn": True,
        "author": author,
        "text": text,
        "textLength": utf16_len(text),
        "image": image_summary(image),
    }
    if not publish_now:
        return out
    result = await publish(page, text, image)
    out["image"]["assetUrn"] = result.pop("assetUrn")
    return {**out, "posted": True, **result}


@asynccontextmanager
async def _session_page():
    if not has_session():
        raise AuthRequired("No LinkedIn session found.")
    assert_profile_idle()
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        context = await open_context(p, headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=60000)
            if _auth_wall(page.url):
                raise AuthRequired(AUTH_MESSAGE)
            yield page
        finally:
            try:
                await context.close()
            except Exception:  # never let teardown hide whether the post went out
                pass


async def create_post(text: str, image: dict[str, Any], *, publish_now: bool) -> dict[str, Any]:
    async with _session_page() as page:
        return await run(page, text, image, publish_now=publish_now)
