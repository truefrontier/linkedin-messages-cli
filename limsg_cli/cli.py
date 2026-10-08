"""limsg — LinkedIn Messages CLI (Chrome session)."""

from __future__ import annotations

import asyncio
import json
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
    EXIT_USAGE,
    agent_output_options,
    compact_rows,
    die,
    emit_csv,
    emit_json,
    note_showing,
    resolve_format,
    select_fields,
)
from limsg_cli.activity import (
    AuthRequired,
    ProfileBusy,
    fetch_activity,
    fetch_export,
    parse_since,
)
from limsg_cli.boost import NotFound, fetch_boost_stats, fetch_boosts
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
        "Sign in on the M4 MacBook if prompted; waiting for the messaging UI…"
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
                hint="Kevin must sign in: limsg login (headful Chrome on the M4 MacBook).",
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


COMMENT_FIELDS = ("id", "time", "type", "text", "targetUrl", "targetTitle")
REACTION_FIELDS = ("id", "time", "type", "reaction", "targetUrl", "targetTitle")
POST_FIELDS = ("id", "time", "type", "text", "url")
ACTIVITY_FIELDS = {
    "comments": COMMENT_FIELDS,
    "reactions": REACTION_FIELDS,
    "posts": POST_FIELDS,
}
ACTIVITY_COMPACT = {
    "comments": ("id", "time", "type", "text"),
    "reactions": ("id", "time", "type", "reaction"),
    "posts": ("id", "time", "type", "text"),
}


def _run_activity(coro, *, auth_hint: str = "Run: limsg login"):
    try:
        return asyncio.run(coro)
    except AuthRequired as exc:
        die(str(exc), EXIT_AUTH, hint=auth_hint)
    except ProfileBusy as exc:
        die(str(exc), EXIT_API)
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    except NotFound as exc:
        die(str(exc), EXIT_NOT_FOUND)
    except SystemExit:
        raise
    except Exception as exc:
        die(str(exc), EXIT_API)


def _emit_activity(kind: str, rows: list[dict], *, fmt, compact, select, quiet, as_csv) -> None:
    fields = list(ACTIVITY_COMPACT[kind] if compact else ACTIVITY_FIELDS[kind])
    if as_csv:
        data = select_fields(rows, select) if select else rows
        if isinstance(data, list):
            emit_csv(data, fields=fields if not select else None)
        return
    fmt = resolve_format(fmt)
    data: Any = compact_rows(rows, fields) if compact else rows
    data = select_fields(data, select)
    if fmt == "json":
        emit_json(data)
    else:
        table = Table(title=f"LinkedIn {kind}")
        for col in fields:
            table.add_column(col)
        for row in rows:
            vals = []
            for col in fields:
                text = str(row.get(col) or "")
                if col in {"text", "targetTitle", "targetUrl", "url"}:
                    text = _truncate(text, 72)
                vals.append(text)
            table.add_row(*vals)
        Console().print(table)
    note_showing(len(rows), quiet=quiet, noun=kind)


def _activity_list(kind: str, limit: int, since: str | None, fmt, compact, select, quiet, as_csv) -> None:
    since_dt = parse_since(since)
    rows = _run_activity(fetch_activity(kind, limit=limit, since=since_dt))
    _emit_activity(kind, rows, fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@main.group("activity")
def activity_group() -> None:
    """Read-only recent activity: comments, reactions, and your posts."""


def _activity_options(fn):
    fn = click.option("--limit", default=20, show_default=True, type=int, help="Max rows.")(fn)
    fn = click.option("--since", default=None, help="Only rows at or after this ISO-8601 time.")(fn)
    return fn


@activity_group.command("comments")
@_activity_options
@agent_output_options(formats=("table", "json"))
def activity_comments(limit, since, fmt, compact, select, quiet, as_csv) -> None:
    """Your recent comments (READ-ONLY)."""
    _activity_list("comments", limit, since, fmt, compact, select, quiet, as_csv)


@activity_group.command("reactions")
@_activity_options
@agent_output_options(formats=("table", "json"))
def activity_reactions(limit, since, fmt, compact, select, quiet, as_csv) -> None:
    """Your recent reactions (READ-ONLY)."""
    _activity_list("reactions", limit, since, fmt, compact, select, quiet, as_csv)


@activity_group.command("posts")
@_activity_options
@agent_output_options(formats=("table", "json"))
def activity_posts(limit, since, fmt, compact, select, quiet, as_csv) -> None:
    """Your recent posts and shares (READ-ONLY)."""
    _activity_list("posts", limit, since, fmt, compact, select, quiet, as_csv)


_EXPORT_FIELDS = (
    "kind",
    "id",
    "time",
    "type",
    "text",
    "reaction",
    "targetUrl",
    "targetTitle",
    "url",
)


@activity_group.command("export")
@click.option("--limit", default=20, show_default=True, type=int, help="Max rows per kind.")
@click.option("--since", default=None, help="Only rows at or after this ISO-8601 time.")
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False), help="Write to PATH instead of stdout.")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True)
@click.option("--compact", is_flag=True, help="High-gravity fields only.")
@click.option("--select", default=None, help="Comma-separated fields.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-data messages on stderr.")
@click.option("--csv", "as_csv", is_flag=True, help="CSV to stdout (same as --format csv).")
def activity_export(limit, since, out_path, fmt, compact, select, quiet, as_csv) -> None:
    """Bundle comments, reactions, and posts. JSON object or CSV with a kind column. READ-ONLY."""
    since_dt = parse_since(since)
    bundle = _run_activity(fetch_export(limit=limit, since=since_dt))
    if as_csv:
        fmt = "csv"
    if compact:
        slim = {}
        for kind, rows in bundle.items():
            slim[kind] = compact_rows(rows, ACTIVITY_COMPACT[kind])
        bundle = slim
    if select:
        bundle = {kind: select_fields(rows, select) for kind, rows in bundle.items()}
    flat = []
    for kind, rows in bundle.items():
        for row in rows:
            item = {"kind": kind}
            item.update(row)
            flat.append(item)
    if fmt == "csv":
        if out_path:
            import csv

            with open(out_path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(_EXPORT_FIELDS), extrasaction="ignore")
                writer.writeheader()
                for row in flat:
                    writer.writerow({key: row.get(key) for key in _EXPORT_FIELDS})
            if not quiet:
                click.echo(f"Wrote {out_path}", err=True)
        else:
            emit_csv(flat, fields=_EXPORT_FIELDS if not select else None)
    else:
        payload = bundle
        if out_path:
            import json

            with open(out_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)
                handle.write("\n")
            if not quiet:
                click.echo(f"Wrote {out_path}", err=True)
        else:
            emit_json(payload)
    raise SystemExit(EXIT_OK)


BOOST_COMPACT = (
    "contentUrn",
    "campaignId",
    "status",
    "spend",
    "totalBudget",
    "currency",
    "impressions",
    "clicks",
    "landingPageClicks",
    "ctr",
    "cpc",
)
BOOST_TABLE = (
    "contentUrn",
    "status",
    "spend/budget",
    "impressions",
    "clicks",
    "landingPageClicks",
    "ctr",
    "cpc",
    "startAt",
    "endAt",
)
BOOST_AUTH_HINT = (
    "Run: limsg login  (then open https://www.linkedin.com/campaignmanager/accounts once in that Chrome "
    "if Campaign Manager asks for anything)"
)


def _boost_window(since: str | None, until: str | None):
    try:
        since_dt = parse_since(since)
        until_dt = parse_since(until, flag="--until")
    except ValueError as exc:
        die(str(exc), EXIT_USAGE)
    if since_dt and until_dt and since_dt.date() > until_dt.date():
        die("--since is after --until.", EXIT_USAGE)
    return since_dt, until_dt


def _dash(value: Any) -> str:
    return "—" if value is None else str(value)


def _boost_cell(row: dict, col: str) -> str:
    if col == "contentUrn":
        return (row.get("contentUrn") or "—").removeprefix("urn:li:")
    if col == "spend/budget":
        return f"{_dash(row.get('spend'))}/{_dash(row.get('totalBudget'))} {row.get('currency') or ''}".strip()
    if col == "ctr" and row.get("ctr") is not None:
        return f"{row['ctr']}%"
    return _dash(row.get(col))


def _csv_cells(rows: list[dict]) -> list[dict]:
    return [
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()}
        for row in rows
    ]


def _emit_boosts(rows: list[dict], *, single: bool, fmt, compact, select, quiet, as_csv) -> None:
    data: Any = compact_rows(rows, BOOST_COMPACT) if compact else rows
    data = select_fields(data, select)
    if as_csv:
        emit_csv(_csv_cells(data))
        return
    if resolve_format(fmt) == "json":
        emit_json(data[0] if single else data)
    elif single:
        table = Table(title="LinkedIn Boost stats")
        table.add_column("field")
        table.add_column("value")
        for key, value in data[0].items():
            table.add_row(key, json.dumps(value) if isinstance(value, (list, dict)) else _dash(value))
        Console().print(table)
    else:
        table = Table(title="LinkedIn Boosts")
        for col in BOOST_TABLE:
            table.add_column(col)
        for row in rows:
            table.add_row(*[_boost_cell(row, col) for col in BOOST_TABLE])
        Console().print(table)
    if not single:
        note_showing(len(rows), quiet=quiet, noun="boosts")


@main.group("boost")
def boost_group() -> None:
    """Read-only LinkedIn Boost (sponsored post) stats from Campaign Manager."""


def _boost_window_options(fn):
    fn = click.option("--until", default=None, help="Analytics end, ISO date or datetime (inclusive day, UTC).")(fn)
    fn = click.option("--since", default=None, help="Analytics start, ISO date or datetime (UTC).")(fn)
    return fn


@boost_group.command("list")
@_boost_window_options
@click.option("--account", default=None, type=int, help="Only this Boost ad account id.")
@agent_output_options(formats=("table", "json"))
def boost_list(since, until, account, fmt, compact, select, quiet, as_csv) -> None:
    """Every boosted campaign across your personal Boost ad accounts (READ-ONLY)."""
    since_dt, until_dt = _boost_window(since, until)
    rows = _run_activity(fetch_boosts(since_dt, until_dt, account=account), auth_hint=BOOST_AUTH_HINT)
    _emit_boosts(rows, single=False, fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


@boost_group.command("stats")
@click.argument("target")
@_boost_window_options
@agent_output_options(formats=("table", "json"))
def boost_stats(target, since, until, fmt, compact, select, quiet, as_csv) -> None:
    """Stats for one boost: urn:li:share:…, urn:li:activity:…, or a campaign id (READ-ONLY)."""
    since_dt, until_dt = _boost_window(since, until)
    row = _run_activity(fetch_boost_stats(target, since_dt, until_dt), auth_hint=BOOST_AUTH_HINT)
    _emit_boosts([row], single=True, fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
    raise SystemExit(EXIT_OK)


if __name__ == "__main__":
    main()
