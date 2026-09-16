"""limsg — LinkedIn Messages CLI (Chrome session)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click
from playwright.async_api import async_playwright
from rich.console import Console
from rich.table import Table

from limsg_cli import __version__
from limsg_cli.agent_ux import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_NOT_FOUND,
    EXIT_OK,
    agent_output_options,
    compact_rows,
    die,
    emit_csv,
    emit_json,
    note_showing,
    resolve_format,
    select_fields,
)
from limsg_cli.api import (
    attach_response_sniffer,
    get_messages,
    list_conversations,
    send_message,
)
from limsg_cli.browser import (
    MESSAGING_URL,
    PROFILE,
    has_session,
    open_context,
)

console = Console(stderr=True)

COMPACT_FIELDS = ("id", "peer", "time", "unread", "preview")
MSG_COMPACT_FIELDS = ("time", "from", "text")
MSG_TABLE_FIELDS = ("time", "from", "text")


def _need_session() -> None:
    if not has_session():
        die(
            "No LinkedIn session found.",
            EXIT_AUTH,
            hint="Run: limsg login",
        )


async def _login_async() -> None:
    console.print(
        "Opening Chrome to LinkedIn Messaging. "
        "Sign in on Codefi if prompted; waiting for the messaging UI…"
    )
    async with async_playwright() as p:
        context = await open_context(p, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await attach_response_sniffer(page)
        await page.goto(MESSAGING_URL, wait_until="domcontentloaded")
        ok = False
        for _ in range(180):  # ~15 min
            await asyncio.sleep(5)
            try:
                url = page.url
            except Exception:
                continue
            if "linkedin.com" not in url:
                continue
            if any(x in url.lower() for x in ("login", "uas/login", "checkpoint", "authwall")):
                continue
            # Messaging shell markers
            try:
                ready = await page.evaluate(
                    """() => {
                      const u = location.href;
                      if (!u.includes('/messaging')) return false;
                      return !!(
                        document.querySelector('.msg-conversations-container') ||
                        document.querySelector('[data-test-conversation-list]') ||
                        document.querySelector('.msg-overlay-list-bubble') ||
                        document.querySelector('main') && u.includes('/messaging')
                      );
                    }"""
                )
            except Exception:
                ready = "/messaging" in url and "login" not in url.lower()
            if ready:
                ok = True
                break
        if not ok:
            await context.close()
            die("Login timed out waiting for messaging UI.", EXIT_AUTH, hint="Run: limsg login")
        # Brief settle so list GraphQL fires into capture/
        await asyncio.sleep(3)
        await context.close()
    console.print(f"[green]Session saved.[/green] Profile: {PROFILE}")


async def _with_messaging_page(headless: bool = True):
    _need_session()
    p = await async_playwright().start()
    context = await open_context(p, headless=headless)
    page = context.pages[0] if context.pages else await context.new_page()
    await attach_response_sniffer(page)
    await page.goto(MESSAGING_URL, wait_until="domcontentloaded")
    # Wait for either login wall or messaging
    for _ in range(60):
        url = page.url
        if any(x in url.lower() for x in ("login", "checkpoint", "authwall")):
            await context.close()
            await p.stop()
            die(
                "LinkedIn session expired or not signed in.",
                EXIT_AUTH,
                hint="Kevin must sign in: limsg login (headful Chrome on Codefi).",
            )
        try:
            ready = await page.locator(".msg-conversations-container, main").count()
        except Exception:
            ready = 0
        if "/messaging" in url and ready:
            break
        await asyncio.sleep(1)
    return p, context, page


def _emit_threads(
    rows: list[dict],
    *,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    if as_csv:
        fields = list(COMPACT_FIELDS) if compact else (list(rows[0].keys()) if rows else list(COMPACT_FIELDS))
        data = select_fields(rows, select) if select else rows
        if isinstance(data, list):
            emit_csv(data, fields=fields if not select else None)
        return
    fmt = resolve_format(fmt)
    data: Any = rows
    if compact:
        data = compact_rows(rows, COMPACT_FIELDS)
    data = select_fields(data, select)
    if fmt == "json":
        emit_json(data)
    else:
        table = Table(title="LinkedIn DMs")
        cols = list(COMPACT_FIELDS) if compact else ["id", "peer", "preview", "time", "unread"]
        for c in cols:
            table.add_column(c)
        for r in rows:
            table.add_row(*[str(r.get(c, "") if r.get(c) is not None else "")[:80] for c in cols])
        Console().print(table)
    note_showing(len(rows), quiet=quiet, noun="threads")


async def _list_async(limit: int) -> list[dict]:
    p, context, page = await _with_messaging_page(headless=True)
    try:
        return await list_conversations(page, limit=limit, do_capture=True)
    finally:
        await context.close()
        await p.stop()


def _resolve_thread(rows: list[dict], thread_or_person: str) -> dict | None:
    q = thread_or_person.strip().lower()
    for r in rows:
        if r.get("id", "").lower() == q or r.get("entityUrn", "").lower() == q:
            return r
    matches = [r for r in rows if q in (r.get("peer") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        die(
            f"Ambiguous person match ({len(matches)} threads). Use thread id.",
            EXIT_NOT_FOUND,
        )
    return None



def _truncate(s: str, n: int = 72) -> str:
    s = s or ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _emit_messages(
    rows: list[dict],
    *,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    fields = list(MSG_COMPACT_FIELDS)
    if as_csv:
        data = select_fields(rows, select) if select else rows
        if isinstance(data, list):
            emit_csv(data, fields=fields if not select else None)
        return
    fmt = resolve_format(fmt)
    data: Any = rows
    if compact:
        data = compact_rows(rows, MSG_COMPACT_FIELDS)
    data = select_fields(data, select)
    if fmt == "json":
        emit_json(data)
    else:
        table = Table(title="LinkedIn messages")
        cols = list(MSG_TABLE_FIELDS)
        for c in cols:
            table.add_column(c)
        for r in rows:
            vals = []
            for c in cols:
                v = r.get(c, "")
                if v is None:
                    v = ""
                s = str(v)
                if c == "text":
                    s = _truncate(s, 72)
                elif c == "time":
                    s = s[:19] + ("Z" if s.endswith("Z") and len(s) > 19 else "")
                    if len(s) > 25:
                        s = s[:25]
                vals.append(s)
            table.add_row(*vals)
        Console().print(table)
    note_showing(len(rows), quiet=quiet, noun="messages")


async def _history_async(thread_or_person: str, limit: int) -> list[dict]:
    p, context, page = await _with_messaging_page(headless=True)
    try:
        # Search window for name resolution
        search_limit = max(limit, 50)
        threads = await list_conversations(page, limit=search_limit, do_capture=False)
        target = _resolve_thread(threads, thread_or_person)
        if not target:
            # Allow raw thread id / URN even if not in recent list window
            q = thread_or_person.strip()
            if q.startswith("urn:li:") or q.startswith("2-"):
                target = {"id": q, "entityUrn": q if q.startswith("urn:li:") else "", "peer": ""}
            else:
                die(
                    f"No thread matching {thread_or_person!r}.",
                    EXIT_NOT_FOUND,
                    hint="Run: limsg messages list --compact",
                )
        conv = target.get("entityUrn") or target.get("id") or ""
        return await get_messages(
            page,
            conv,
            limit=limit,
            do_capture=True,
            peer_hint=(target.get("peer") or None),
        )
    finally:
        await context.close()
        await p.stop()


@click.group()
@click.version_option(__version__, prog_name="limsg")
def main() -> None:
    """LinkedIn Messages CLI (unofficial). Session: ~/.linkedin-messages-cli/chrome-profile/"""


@main.command("login")
def login_cmd() -> None:
    """Open headful Chrome to LinkedIn Messaging and save the session."""
    try:
        asyncio.run(_login_async())
    except SystemExit:
        raise
    except Exception as e:
        die(str(e), EXIT_API)


@main.group("messages")
def messages_group() -> None:
    """List, read, and send LinkedIn DMs."""


@messages_group.command("list")
@click.option("--limit", default=20, show_default=True, type=int, help="Max threads.")
@agent_output_options(formats=("table", "json"))
def messages_list(
    limit: int,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    """List recent DM threads (peer, preview, time, unread)."""
    try:
        rows = asyncio.run(_list_async(limit=limit))
    except SystemExit:
        raise
    except Exception as e:
        die(str(e), EXIT_API)
    _emit_threads(
        rows,
        fmt=fmt,
        compact=compact,
        select=select,
        quiet=quiet,
        as_csv=as_csv,
    )
    raise SystemExit(EXIT_OK)




def _messages_history_impl(
    thread_or_person: str,
    limit: int,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    try:
        rows = asyncio.run(_history_async(thread_or_person, limit=limit))
    except SystemExit:
        raise
    except Exception as e:
        die(str(e), EXIT_API)
    _emit_messages(
        rows,
        fmt=fmt,
        compact=compact,
        select=select,
        quiet=quiet,
        as_csv=as_csv,
    )
    raise SystemExit(EXIT_OK)


@messages_group.command("history")
@click.argument("thread_or_person")
@click.option("--limit", default=20, show_default=True, type=int, help="Max messages (most recent).")
@agent_output_options(formats=("table", "json"))
def messages_history(
    thread_or_person: str,
    limit: int,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    """Read last N messages in a thread (who / text / time). READ-ONLY."""
    _messages_history_impl(thread_or_person, limit, fmt, compact, select, quiet, as_csv)


@messages_group.command("read")
@click.argument("thread_or_person")
@click.option("--limit", default=20, show_default=True, type=int, help="Max messages (most recent).")
@agent_output_options(formats=("table", "json"))
def messages_read(
    thread_or_person: str,
    limit: int,
    fmt: str | None,
    compact: bool,
    select: str | None,
    quiet: bool,
    as_csv: bool,
) -> None:
    """Alias for `messages history` — last N messages (READ-ONLY)."""
    _messages_history_impl(thread_or_person, limit, fmt, compact, select, quiet, as_csv)




@messages_group.command("send")
@click.argument("thread_or_person")
@click.option("--text", required=True, help="Message body.")
@click.option(
    "--yes",
    "--send",
    "do_send",
    is_flag=True,
    help="Actually send. Without this flag, dry-run only.",
)
@click.option("--limit", default=50, show_default=True, type=int, help="Thread search window.")
def messages_send(thread_or_person: str, text: str, do_send: bool, limit: int) -> None:
    """Send a DM. Default is dry-run; pass --yes (or --send) to deliver."""

    async def _run() -> dict:
        p, context, page = await _with_messaging_page(headless=True)
        try:
            rows = await list_conversations(page, limit=limit, do_capture=False)
            target = _resolve_thread(rows, thread_or_person)
            if not target:
                die(
                    f"No thread matching {thread_or_person!r}.",
                    EXIT_NOT_FOUND,
                    hint="Run: limsg messages list --compact",
                )
            preview = {
                "dry_run": not do_send,
                "to": target.get("peer"),
                "thread_id": target.get("id"),
                "entityUrn": target.get("entityUrn"),
                "text": text,
            }
            if not do_send:
                return preview
            # Live send (gated)
            conv = target.get("entityUrn") or None
            result = await send_message(
                page,
                conversation_urn=conv,
                recipient_profile_urns=None,
                text=text,
            )
            preview["sent"] = result
            preview["dry_run"] = False
            return preview
        finally:
            await context.close()
            await p.stop()

    try:
        out = asyncio.run(_run())
    except SystemExit:
        raise
    except Exception as e:
        die(str(e), EXIT_API)

    if out.get("dry_run"):
        click.echo("DRY-RUN (not sent). Re-run with --yes to send.")
        emit_json(out)
        raise SystemExit(EXIT_OK)
    click.echo("SENT", err=True)
    emit_json(out)
    raise SystemExit(EXIT_OK)


if __name__ == "__main__":
    main()
