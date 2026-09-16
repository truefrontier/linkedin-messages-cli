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
DEFAULT_MSG_QUERY = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
SEND_PATH = "/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"
REST_CONVERSATIONS = "/voyager/api/messaging/conversations"
REST_EVENTS = "/voyager/api/messaging/conversations"
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
    if recipient_profile_urns and not conversation_urn:
        body["recipientProfileUrns"] = recipient_profile_urns

    url = f"https://www.linkedin.com{SEND_PATH}"
    status, data = await _page_fetch(page, url, method="POST", body=body)
    if do_capture:
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


def _ms_to_iso(value: Any) -> str:
    """Convert LinkedIn ms epoch (int/str) to ISO-8601 UTC; pass through strings that look ISO."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        if "T" in s or s.endswith("Z"):
            return s
        try:
            value = float(s)
        except ValueError:
            return s
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Heuristic: seconds vs milliseconds
    if ms < 1e12:
        ms *= 1000.0
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_conversation_urn(conversation_id_or_urn: str) -> tuple[str, str]:
    """Return (thread_id, conversation_urn_for_api).

    Accepts peer-thread id (2-…), entityUrn, or msg_conversation URN.
    """
    raw = (conversation_id_or_urn or "").strip()
    if not raw:
        raise ValueError("empty conversation id")
    m = re.search(r"(2-[A-Za-z0-9_=-]+)", raw)
    thread = m.group(1) if m else raw
    if raw.startswith("urn:li:"):
        # Keep full URN (including parenthetical msg_conversation forms)
        if "msg_conversation" in raw or "fs_conversation" in raw or "fsd_conversation" in raw:
            return thread, raw
        # Unknown urn type that still embeds a thread id → wrap as msg_conversation
        if m:
            return thread, f"urn:li:msg_conversation:{thread}"
        return thread, raw
    return thread, f"urn:li:msg_conversation:{thread}"


def _text_from_body(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("text", "body", "snippet"):
        v = obj.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            return v["text"]
    ab = obj.get("attributedBody") or obj.get("body")
    if isinstance(ab, dict) and isinstance(ab.get("text"), str):
        return ab["text"]
    if isinstance(ab, str):
        return ab
    return ""


def _sender_name_and_urn(obj: dict, by_urn: dict[str, dict]) -> tuple[str, str]:
    """Extract display name + sender profile/participant URN from a Message or Event."""
    sender_urn = ""
    name = ""

    def _name_from_participant(ref: dict | None) -> str:
        if not isinstance(ref, dict):
            return ""
        pt = ref.get("participantType") or {}
        if isinstance(pt, dict):
            for key in ("member", "organization", "agent"):
                cand = pt.get(key)
                if isinstance(cand, dict):
                    n = _name_from_member(cand)
                    if n:
                        return n
        n = _name_from_member(ref.get("member") or ref.get("miniProfile") or ref)
        return n

    # GraphQL messenger.Message uses *sender / *actor (plain sender is often absent)
    sender = obj.get("sender") or obj.get("*sender") or obj.get("actor") or obj.get("*actor")
    if isinstance(sender, str):
        sender_urn = sender
        ref = by_urn.get(sender) or {}
        name = _name_from_participant(ref)
    elif isinstance(sender, dict):
        sender_urn = str(sender.get("entityUrn") or sender.get("backendUrn") or "")
        name = _name_from_participant(sender)
        if not name and sender_urn:
            name = _name_from_participant(by_urn.get(sender_urn))

    # REST Event: *from / from
    if not name:
        from_ref = obj.get("*from") or obj.get("from")
        if isinstance(from_ref, str):
            sender_urn = sender_urn or from_ref
            ref = by_urn.get(from_ref) or {}
            name = _name_from_participant(ref) if ref else ""
            if not name and isinstance(ref, dict):
                mini = ref.get("miniProfile") or ref.get("member") or ref
                if isinstance(mini, str):
                    mini = by_urn.get(mini) or {}
                name = _name_from_member(mini if isinstance(mini, dict) else None)
                mp = ref.get("*miniProfile") or ref.get("miniProfile")
                if not name and isinstance(mp, str):
                    name = _name_from_member(by_urn.get(mp))
                if not name and isinstance(mp, dict):
                    name = _name_from_member(mp)
        elif isinstance(from_ref, dict):
            sender_urn = sender_urn or str(from_ref.get("entityUrn") or "")
            mini = from_ref.get("miniProfile") or from_ref
            name = _name_from_member(mini if isinstance(mini, dict) else None)

    return (name or "").strip(), sender_urn


def _label_from(name: str, sender_urn: str, self_urn: str | None) -> str:
    if self_urn and sender_urn and self_urn in sender_urn:
        return "me"
    if self_urn and sender_urn:
        # Compare profile id suffixes
        a = re.search(r"(ACoAA[A-Za-z0-9_-]+|fsd_profile:[A-Za-z0-9_-]+)", self_urn)
        b = re.search(r"(ACoAA[A-Za-z0-9_-]+|fsd_profile:[A-Za-z0-9_-]+)", sender_urn)
        if a and b and a.group(1).split(":")[-1] == b.group(1).split(":")[-1]:
            return "me"
    return name or "peer"


def parse_messages_payload(data: Any, *, self_urn: str | None = None) -> list[dict]:
    """Normalize GraphQL messengerMessages or REST conversation events → agent rows.

    Each row: id, time (ISO), from, text, senderUrn (optional).
    """
    rows: list[dict] = []
    if not isinstance(data, dict):
        return rows

    included_raw = data.get("included")
    if included_raw is None and isinstance(data.get("data"), dict):
        included_raw = data["data"].get("included")

    # included may be list (common) or dict keyed by index (rare REST variants)
    included_list: list[dict] = []
    by_urn: dict[str, dict] = {}
    if isinstance(included_raw, list):
        included_list = [o for o in included_raw if isinstance(o, dict)]
    elif isinstance(included_raw, dict):
        for v in included_raw.values():
            if isinstance(v, dict):
                included_list.append(v)
                # also allow lookup by string keys that look like URNs
        for k, v in included_raw.items():
            if isinstance(v, dict) and isinstance(k, str) and k.startswith("urn:"):
                by_urn[k] = v

    for obj in included_list:
        urn = obj.get("entityUrn") or obj.get("backendUrn") or ""
        if urn:
            by_urn[str(urn)] = obj

    messages: list[dict] = []
    for obj in included_list:
        t = str(obj.get("$type") or "")
        # GraphQL: com.linkedin.messenger.Message
        if ("messenger.Message" in t) or (t.endswith(".Message") and "Conversation" not in t):
            messages.append(obj)
            continue
        # REST: com.linkedin.voyager.messaging.Event
        if "messaging.Event" in t or t.endswith(".Event"):
            messages.append(obj)
            continue
        # Fallback: has body/deliveredAt typical of Message
        if obj.get("deliveredAt") is not None and (obj.get("body") is not None or obj.get("sender") is not None):
            messages.append(obj)
            continue
        if obj.get("eventContent") is not None and obj.get("createdAt") is not None:
            messages.append(obj)

    # REST sometimes puts events under data.elements
    elements = data.get("elements")
    if isinstance(elements, list) and not messages:
        for el in elements:
            if isinstance(el, dict):
                messages.append(el)

    # GraphQL collection under data.data.messengerMessagesByConversation etc.
    if not messages and isinstance(data.get("data"), dict):
        d0 = data["data"]
        # nested data.data.*.elements
        stack = [d0]
        while stack and not messages:
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            for key, val in cur.items():
                if key in {"elements", "*elements"} and isinstance(val, list):
                    for el in val:
                        if isinstance(el, str):
                            ref = by_urn.get(el)
                            if ref:
                                messages.append(ref)
                        elif isinstance(el, dict):
                            messages.append(el)
                elif isinstance(val, dict) and (
                    "Message" in key or "message" in key or "messenger" in key.lower()
                ):
                    stack.append(val)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("entityUrn") or msg.get("backendUrn") or msg.get("id") or "")
        # Skip non-message scaffolding
        t = str(msg.get("$type") or "")
        if "Conversation" in t and "Message" not in t:
            continue

        text = _text_from_body(msg.get("body"))
        if not text:
            text = _text_from_body(msg.get("eventContent"))
        if not text:
            text = _text_from_body(msg.get("attributedBody"))
        if not text and isinstance(msg.get("renderContent"), list):
            # attachment-only — leave empty text with marker
            text = ""

        when = msg.get("deliveredAt") or msg.get("createdAt") or msg.get("lastActivityAt") or ""
        name, sender_urn = _sender_name_and_urn(msg, by_urn)
        from_label = _label_from(name, sender_urn, self_urn)

        # Skip empty system stubs with no id and no text
        if not mid and not text:
            continue

        rows.append(
            {
                "id": mid,
                "time": _ms_to_iso(when),
                "from": from_label,
                "text": text,
                "senderUrn": sender_urn,
            }
        )

    def sort_key(r: dict) -> float:
        t = r.get("time") or ""
        # ISO sorts lexicographically for Z times; also try raw ms in id-less cases
        if isinstance(t, str) and t.endswith("Z") and "T" in t:
            try:
                from datetime import datetime

                return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        try:
            return float(t)
        except (TypeError, ValueError):
            return 0.0

    # Newest last in chat UIs; Career wants "last N" — return chronological ascending
    # then caller can slice [-limit:]. We sort ascending here.
    rows.sort(key=sort_key)
    # Dedupe by id
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        k = r.get("id") or f"{r.get('time')}|{r.get('from')}|{r.get('text')}"
        if k in seen:
            continue
        seen.add(str(k))
        out.append(r)
    return out


def resolve_messages_query_id() -> str:
    found = load_latest_query_ids()
    return (
        found.get("messengerMessages")
        or found.get("messengerMessagesByConversation")
        or DEFAULT_MSG_QUERY
    )


async def get_messages(
    page: "Page",
    conversation_id_or_urn: str,
    *,
    limit: int = 20,
    do_capture: bool = True,
    peer_hint: str | None = None,
) -> list[dict]:
    """Fetch last N messages for a conversation (READ-ONLY).

    Tries Messaging GraphQL messengerMessages, then REST .../events.
    Optionally navigates to the thread URL so the sniffer can capture a fresh queryId.
    """
    thread, conv_urn = normalize_conversation_urn(conversation_id_or_urn)
    self_urn = await discover_mailbox_urn(page)
    rows: list[dict] = []
    last_status = 0
    last_data: Any = None
    last_url = ""

    # Soft-open thread so LinkedIn fires messengerMessages (sniffer saves scrubbed capture).
    try:
        thread_url = f"https://www.linkedin.com/messaging/thread/{quote(thread, safe='')}/"
        await page.goto(thread_url, wait_until="domcontentloaded")
        await page.evaluate('() => new Promise(r => setTimeout(r, 2500))')
    except Exception:
        pass

    qid = resolve_messages_query_id()
    enc_urn = quote(conv_urn, safe="")
    # Primary: GraphQL messengerMessages
    for variables in (
        f"(conversationUrn:{enc_urn})",
        f"(conversationUrn:{enc_urn},count:{max(limit, 20)})",
        # Some builds want fs_conversation form
        f"(conversationUrn:{quote('urn:li:fs_conversation:' + thread, safe='')})",
    ):
        url = f"https://www.linkedin.com{MESSAGING_GQL}?queryId={qid}&variables={variables}"
        last_url = url
        last_status, last_data = await _page_fetch(page, url)
        if last_status == 200:
            rows = parse_messages_payload(last_data, self_urn=self_urn)
            if do_capture:
                save_capture(
                    kind="messages_graphql",
                    url=url,
                    method="GET",
                    status=last_status,
                    response_shape=last_data,
                )
            if rows:
                break

    if not rows:
        # REST fallback — conversation events (newest page)
        enc_thread = quote(thread, safe="")
        url = (
            f"https://www.linkedin.com{REST_EVENTS}/{enc_thread}/events"
            f"?keyVersion=LEGACY_INBOX&q=events&count={max(limit, 20)}"
        )
        last_url = url
        last_status, last_data = await _page_fetch(page, url)
        if last_status == 200:
            rows = parse_messages_payload(last_data, self_urn=self_urn)
            if do_capture:
                save_capture(
                    kind="messages_rest_events",
                    url=url,
                    method="GET",
                    status=last_status,
                    response_shape=last_data,
                )

    if last_status in {401, 403}:
        raise RuntimeError("Auth failed talking to Voyager. Run: limsg login")
    if last_status and last_status >= 400 and not rows:
        raise RuntimeError(
            f"Voyager messages failed HTTP {last_status} for {last_url.split('?')[0]}"
        )

    # Relabel unknown peer using thread title hint (prefer first name before comma)
    if peer_hint:
        hint = peer_hint.split(",")[0].strip() or peer_hint
        for r in rows:
            if r.get("from") in {"", "peer"}:
                r["from"] = hint

    # Last N (most recent), chronological ascending for reading
    if len(rows) > limit:
        rows = rows[-limit:]
    return rows


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
