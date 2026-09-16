"""Voyager messaging API helpers (page-context credentials)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import quote

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

from limsg_cli.browser import csrf_from_cookies, voyager_headers
from limsg_cli.capture import is_interesting, load_latest_query_ids, save_capture

# Known defaults (may rotate; capture + page discovery preferred).
DEFAULT_CONV_QUERY = "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
SEND_PATH = "/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"
REST_CONVERSATIONS = "/voyager/api/messaging/conversations"
MESSAGING_GQL = "/voyager/api/voyagerMessagingGraphQL/graphql"


def _textish(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("text", "value", "localized", "name"):
            if k in val:
                return _textish(val.get(k))
        return ""
    return str(val).strip()


def _name_from_member(member: dict | None) -> str:
    if not isinstance(member, dict):
        return ""
    # Newer messenger shape: firstName/lastName as {text: ...}
    fn = _textish(member.get("firstName"))
    ln = _textish(member.get("lastName"))
    if fn or ln:
        return f"{fn} {ln}".strip()
    return _textish(member.get("name") or member.get("publicIdentifier") or "")


def _preview_from_conv(conv: dict) -> str:
    for key in ("lastMessage", "messages", "preview"):
        lm = conv.get(key)
        if isinstance(lm, dict):
            for tkey in ("text", "body", "snippet"):
                s = _textish(lm.get(tkey))
                if s:
                    return s[:160]
            ab = lm.get("attributedBody") or lm.get("body")
            s = _textish(ab)
            if s:
                return s[:160]
        if isinstance(lm, str):
            return lm[:160]
    return ""


def parse_conversations_payload(data: Any) -> list[dict]:
    """Normalize GraphQL or REST conversation list → agent rows."""
    rows: list[dict] = []
    if not isinstance(data, dict):
        return rows

    included = data.get("included")
    # GraphQL sometimes nests included under data
    if included is None and isinstance(data.get("data"), dict):
        included = data["data"].get("included")
    if not isinstance(included, list):
        included = []

    by_urn: dict[str, dict] = {}
    for obj in included:
        if not isinstance(obj, dict):
            continue
        urn = obj.get("entityUrn") or obj.get("backendUrn") or ""
        if urn:
            by_urn[urn] = obj

    participant_names: dict[str, str] = {}
    for obj in included:
        if not isinstance(obj, dict):
            continue
        t = obj.get("$type") or ""
        if "MessagingParticipant" in t or "Participant" in t:
            member = None
            pt = obj.get("participantType") or {}
            if isinstance(pt, dict):
                member = pt.get("member") or pt.get("organization")
            if member is None:
                member = obj.get("member") or obj
            name = _name_from_member(member if isinstance(member, dict) else None)
            urn = obj.get("entityUrn") or ""
            if name and urn:
                participant_names[urn] = name

    conversations: list[dict] = []
    for obj in included:
        if not isinstance(obj, dict):
            continue
        t = obj.get("$type") or ""
        if "Conversation" in t and "Participant" not in t:
            conversations.append(obj)

    # REST list: data.elements
    elements = data.get("elements")
    if isinstance(elements, list) and not conversations:
        for el in elements:
            if isinstance(el, dict):
                conversations.append(el)

    for conv in conversations:
        entity = conv.get("entityUrn") or conv.get("backendUrn") or conv.get("conversationId") or ""
        thread = ""
        m = re.search(r"(2-[A-Za-z0-9_-]+)", str(entity))
        if m:
            thread = m.group(1)
        elif entity:
            thread = str(entity)

        refs = (
            conv.get("*conversationParticipants")
            or conv.get("conversationParticipants")
            or conv.get("participants")
            or []
        )
        names: list[str] = []
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, str):
                    names.append(participant_names.get(r) or by_urn.get(r, {}).get("name") or "")
                elif isinstance(r, dict):
                    n = _name_from_member(r.get("member") if isinstance(r.get("member"), dict) else r)
                    if not n:
                        mini = r.get("miniProfile") or {}
                        n = _name_from_member(mini if isinstance(mini, dict) else None)
                    names.append(n)
        names = [n for n in names if n]
        peer = ", ".join(names) if names else (conv.get("title") or "(unknown)")

        unread = conv.get("unreadCount")
        if unread is None:
            unread = conv.get("unread")
        last_at = (
            conv.get("lastActivityAt")
            or conv.get("lastActivityTime")
            or conv.get("deliveredAt")
            or ""
        )
        preview = _preview_from_conv(conv)

        rows.append(
            {
                "id": thread or str(entity),
                "entityUrn": str(entity) if entity else "",
                "peer": peer,
                "preview": preview,
                "time": last_at,
                "unread": int(unread) if isinstance(unread, (int, float)) else (1 if unread else 0),
            }
        )

    # Sort by time desc when numeric
    def sort_key(r: dict) -> float:
        t = r.get("time")
        try:
            return float(t)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=sort_key, reverse=True)
    return rows


async def _page_fetch(page: Page, url: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, Any]:
    cookies = await page.context.cookies()
    csrf = csrf_from_cookies(cookies)
    if not csrf:
        raise RuntimeError("No JSESSIONID/csrf in session. Run: limsg login")

    headers = voyager_headers(csrf)
    # Prefer in-page fetch so cookies + TLS fingerprint match the browser.
    script = """
    async ({ url, method, headers, body }) => {
      const opts = { method, headers, credentials: 'include' };
      if (body !== null && body !== undefined) {
        opts.headers = Object.assign({}, headers, {'content-type': 'application/json'});
        opts.body = JSON.stringify(body);
      }
      const res = await fetch(url, opts);
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (e) { json = { rawType: 'text', length: text.length }; }
      return { status: res.status, json };
    }
    """
    result = await page.evaluate(
        script,
        {"url": url, "method": method, "headers": headers, "body": body},
    )
    return int(result.get("status") or 0), result.get("json")


async def discover_mailbox_urn(page: Page) -> str | None:
    """Best-effort mailbox / profile URN from the messaging page."""
    html = await page.content()
    # Prefer fsd_profile / miniProfile ids
    ids = re.findall(r"urn:li:(?:fsd_profile|fs_miniProfile):([A-Za-z0-9_-]+)", html)
    if ids:
        # most common id
        best = max(set(ids), key=ids.count)
        return f"urn:li:fsd_profile:{best}"
    acoas = re.findall(r"ACoAA[A-Za-z0-9_-]{5,}", html)
    if acoas:
        best = max(set(acoas), key=acoas.count)
        return f"urn:li:fsd_profile:{best}"
    # /voyager/api/me
    try:
        status, data = await _page_fetch(page, "https://www.linkedin.com/voyager/api/me")
        if status == 200 and isinstance(data, dict):
            for obj in data.get("included") or []:
                if isinstance(obj, dict) and obj.get("publicIdentifier"):
                    urn = obj.get("entityUrn") or ""
                    if "profile" in urn:
                        return urn.replace("fs_miniProfile", "fsd_profile")
            # miniProfile in data
            mp = (data.get("data") or {}).get("miniProfile") or data.get("miniProfile")
            if isinstance(mp, dict) and mp.get("entityUrn"):
                return str(mp["entityUrn"]).replace("fs_miniProfile", "fsd_profile")
    except Exception:
        pass
    return None


def resolve_conversations_query_id() -> str:
    found = load_latest_query_ids()
    return found.get("messengerConversations") or DEFAULT_CONV_QUERY


async def list_conversations(page: Page, *, limit: int = 20, do_capture: bool = True) -> list[dict]:
    mailbox = await discover_mailbox_urn(page)
    qid = resolve_conversations_query_id()
    rows: list[dict] = []
    last_status = 0
    last_data: Any = None
    last_url = ""

    if mailbox:
        enc = quote(mailbox, safe="")
        url = (
            f"https://www.linkedin.com{MESSAGING_GQL}"
            f"?queryId={qid}&variables=(mailboxUrn:{enc})"
        )
        last_url = url
        last_status, last_data = await _page_fetch(page, url)
        if last_status == 200:
            rows = parse_conversations_payload(last_data)
            if do_capture:
                save_capture(
                    kind="list_graphql",
                    url=url,
                    method="GET",
                    status=last_status,
                    response_shape=last_data,
                )

    if not rows:
        # REST fallback
        url = (
            f"https://www.linkedin.com{REST_CONVERSATIONS}"
            f"?keyVersion=LEGACY_INBOX&q=search&count={max(limit, 20)}"
        )
        last_url = url
        last_status, last_data = await _page_fetch(page, url)
        if last_status == 200:
            rows = parse_conversations_payload(last_data)
            if do_capture:
                save_capture(
                    kind="list_rest",
                    url=url,
                    method="GET",
                    status=last_status,
                    response_shape=last_data,
                )

    if last_status in {401, 403}:
        raise RuntimeError("Auth failed talking to Voyager. Run: limsg login")
    if last_status and last_status >= 400 and not rows:
        raise RuntimeError(f"Voyager list failed HTTP {last_status} for {last_url.split('?')[0]}")

    return rows[:limit]


async def send_message(
    page: Page,
    *,
    conversation_urn: str | None,
    recipient_profile_urns: list[str] | None,
    text: str,
    mailbox_urn: str | None = None,
    do_capture: bool = True,
) -> dict:
    """POST createMessage using the dash MessengerMessages body the web UI sends.

    Captured UI shape (2026-09):
      {
        message: {
          body: { attributes: [], text },
          renderContentUnions: [],
          conversationUrn,
          originToken  # uuid4
        },
        mailboxUrn,
        trackingId,  # 16-char token
        dedupeByClientGeneratedToken: false
      }
    """
    mailbox = mailbox_urn or await discover_mailbox_urn(page)
    if not mailbox:
        raise RuntimeError("Could not resolve mailboxUrn")
    if not conversation_urn and not recipient_profile_urns:
        raise RuntimeError("Need conversationUrn or recipientProfileUrns")

    origin = str(uuid.uuid4())
    # UI uses a short opaque tracking id; uuid hex slice is accepted in practice.
    tracking = uuid.uuid4().hex[:16]

    message: dict[str, Any] = {
        "body": {"attributes": [], "text": text},
        "renderContentUnions": [],
        "originToken": origin,
    }
    if conversation_urn:
        message["conversationUrn"] = conversation_urn

    body: dict[str, Any] = {
        "message": message,
        "mailboxUrn": mailbox,
        "trackingId": tracking,
        "dedupeByClientGeneratedToken": False,
    }
    # New-thread path (no conversation yet)
    if recipient_profile_urns and not conversation_urn:
        body["recipientProfileUrns"] = recipient_profile_urns

    url = f"https://www.linkedin.com{SEND_PATH}"
    status, data = await _page_fetch(page, url, method="POST", body=body)
    if do_capture:
        # Store keys + nested message keys only (never message text).
        save_capture(
            kind="send_createMessage",
            url=url,
            method="POST",
            status=status,
            request_body_keys=sorted(body.keys()),
            request_message_keys=sorted(message.keys()),
            response_shape=(
                data
                if status < 400
                else {
                    "error": True,
                    "status": status,
                    "response_keys": list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                }
            ),
        )
    if status >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("message") or data.get("code") or data.get("status") or "")[:200]
        raise RuntimeError(f"Send failed HTTP {status}" + (f": {detail}" if detail else ""))
    return {"status": status, "ok": True, "originToken": origin}


async def attach_response_sniffer(page: Page) -> list[dict]:
    """Record interesting messaging API responses while the page loads."""
    hits: list[dict] = []

    async def on_response(response):
        try:
            url = response.url
            if not is_interesting(url):
                return
            method = response.request.method
            status = response.status
            shape = None
            try:
                if "json" in (response.headers.get("content-type") or ""):
                    shape = await response.json()
            except Exception:
                shape = None
            path = save_capture(
                kind="sniff",
                url=url,
                method=method,
                status=status,
                response_shape=shape,
            )
            hits.append({"path": str(path), "url_path": url.split("?")[0], "status": status})
        except Exception:
            return

    page.on("response", on_response)
    return hits
