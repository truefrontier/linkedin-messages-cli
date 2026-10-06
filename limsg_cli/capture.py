"""Scrubbed network capture for Voyager messaging endpoints.

Saves path + field-name shapes only under ~/.linkedin-messages-cli/capture/.
Never writes cookie values, tokens, or message body text.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from limsg_cli.browser import CAPTURE, ensure_dirs

INTERESTING = re.compile(
    r"(voyagerMessagingGraphQL|/messaging/conversations|MessengerMessages|"
    r"voyagerMessagingDash|messengerConversations|messengerMessages|"
    r"voyagerIdentityDash|voyagerFeedDash|voyagerSocialDash|"
    r"profileUpdates|ProfileUpdates|"
    r"/voyager/api/identity/|/voyager/api/feed/|/voyager/api/social/|"
    r"/voyager/api/graphql|"
    r"memberComments|memberReactions|memberShareFeed|memberFeed|"
    r"recent-activity|profileActivity|member-activity|"
    r"flagship-web/feed|flagship-web/in/|"
    r"comment|reaction|activity)",
    re.I,
)

# Keys whose values are treated as personal content and redacted.
REDACT_VALUE_KEYS = re.compile(
    r"(text|body|preview|message|subject|snippet|attributedBody|"
    r"commentary|html|content|cookie|token|li_at|password|secret)",
    re.I,
)


def _scrub(obj: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "..."
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if REDACT_VALUE_KEYS.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = "<redacted>"
            elif isinstance(v, str) and len(v) > 120 and not k.startswith("$") and "Urn" not in k and "urn" not in k.lower():
                # Long free-text strings → type only
                out[k] = f"<str len={len(v)}>"
            else:
                out[k] = _scrub(v, depth=depth + 1)
        return out
    if isinstance(obj, list):
        if not obj:
            return []
        # Keep shape of first 2 elements only
        return [_scrub(x, depth=depth + 1) for x in obj[:2]] + (
            [f"...+{len(obj) - 2} more"] if len(obj) > 2 else []
        )
    if isinstance(obj, str):
        if obj.startswith("urn:li:") or re.match(r"^ACoAA", obj):
            return obj[:48] + ("…" if len(obj) > 48 else "")
        if len(obj) > 80:
            return f"<str len={len(obj)}>"
        return obj
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return str(type(obj).__name__)


def path_only(url: str) -> str:
    p = urlparse(url)
    return p.path


def _scrub_qs_value(key: str, value: str) -> str:
    """Keep structural query params; strip profile ids and URNs."""
    if key == "queryId":
        return value
    v = unquote(value)
    v = re.sub(r"urn:li:[A-Za-z0-9_.-]+:[A-Za-z0-9_-]+", "<urn>", v)
    v = re.sub(r"ACoAA[A-Za-z0-9_-]+", "<id>", v)
    if len(v) > 240:
        v = v[:240] + "…"
    return v


def summarize_url(url: str) -> dict[str, Any]:
    p = urlparse(url)
    qs = parse_qs(p.query)
    query_id = (qs.get("queryId") or [None])[0]
    return {
        "path": p.path,
        "queryId": query_id,
        "query_keys": sorted(qs.keys()),
        "query": {k: _scrub_qs_value(k, vs[0]) for k, vs in qs.items() if vs},
    }


def is_interesting(url: str) -> bool:
    return bool(INTERESTING.search(url))


def save_capture(
    *,
    kind: str,
    url: str,
    method: str,
    status: int | None,
    request_body_keys: list[str] | None = None,
    request_message_keys: list[str] | None = None,
    response_shape: Any = None,
) -> Path:
    ensure_dirs()
    CAPTURE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_kind = re.sub(r"[^a-zA-Z0-9_-]+", "_", kind)[:60]
    path = CAPTURE / f"{stamp}_{safe_kind}.json"
    n = 0
    while path.exists():
        n += 1
        path = CAPTURE / f"{stamp}_{safe_kind}_{n}.json"
    payload = {
        "capturedAt": stamp,
        "kind": kind,
        "method": method,
        "status": status,
        "url": summarize_url(url),
        "requestBodyKeys": request_body_keys or [],
        "requestMessageKeys": request_message_keys or [],
        "responseShape": _scrub(response_shape) if response_shape is not None else None,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path


def load_latest_query_ids() -> dict[str, str]:
    """Return {messengerConversations: queryId, messengerMessages: queryId, ...} from captures."""
    ensure_dirs()
    found: dict[str, str] = {}
    if not CAPTURE.exists():
        return found
    files = sorted(CAPTURE.glob("*.json"), reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        qid = (data.get("url") or {}).get("queryId")
        if not qid or "." not in qid:
            continue
        name = qid.split(".", 1)[0]
        found.setdefault(name, qid)
    return found
