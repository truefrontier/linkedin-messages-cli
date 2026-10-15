"""Agent-native output helpers (printing-press-inspired).

Exit codes: 0 ok, 2 usage, 3 not found, 4 auth, 5 api/runtime.
Auto-JSON when stdout is not a TTY (unless --format text).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Sequence

import click

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_API = 5

# `limsg agent-help [COMMAND]` prints these. Keyed by command path.
AGENT_HELP: dict[str, str] = {
    "post create": """\
limsg post create (--text STR | --text-file PATH) --image PATH [--dry-run] [--confirm] [--format json|text]

Publishes ONE LinkedIn post (text + exactly one image) as the signed-in personal account.

DRY-RUN IS THE DEFAULT. Without --confirm nothing is uploaded or posted. A dry-run validates
the inputs, checks the session is logged in (read-only GET /voyager/api/me), and prints the
author, the full text, and the image path, type, size, and dimensions. --dry-run together
with --confirm is still a dry-run.

--confirm publishes for real (register image -> upload -> create post). The post is public.
Get a human to approve the exact dry-run output first. Each write is sent once; never
retry --confirm on your own:
  - error says "Nothing was posted"  -> safe to fix the input and run again
  - error says "may be live"         -> check the profile before doing anything
  - exit 0 with a "warning" and urn null -> it IS posted; running again posts twice

Inputs:
  --text / --text-file   exactly one; trimmed; non-empty; <= 3000 characters
                         (counted like LinkedIn: UTF-16 units, so an emoji counts 2)
  --image                JPEG, PNG, or GIF by file contents (magic bytes); <= 10 MB

Output: JSON when piped or with --format json. Dry-run: dryRun, loggedIn, author, text,
textLength, image. --confirm adds posted, urn, url (https://www.linkedin.com/feed/update/<urn>/),
and image.assetUrn.

Exit codes: 0 ok, 2 bad input, 4 not logged in (run: limsg login),
5 API error or Chrome profile busy (close the limsg Chrome window).
""",
}


def wants_json(fmt: str | None) -> bool:
    """JSON when explicitly requested, or when table output is piped."""
    if fmt == "json":
        return True
    if fmt in {"text", "csv"}:
        return False
    # fmt is table / None → auto-json when not a TTY
    return not sys.stdout.isatty()


def resolve_format(fmt: str | None, *, default: str = "table") -> str:
    fmt = fmt or default
    if fmt == "table" and wants_json("table"):
        return "json"
    return fmt


def select_fields(data: Any, select: str | None) -> Any:
    if not select:
        return data
    keys = [k.strip() for k in select.split(",") if k.strip()]
    if not keys:
        return data
    if isinstance(data, list):
        return [{k: (item or {}).get(k) for k in keys} for item in data]
    if isinstance(data, dict):
        return {k: data.get(k) for k in keys}
    return data


def compact_rows(rows: Iterable[dict], fields: Sequence[str]) -> list[dict]:
    out = []
    for row in rows:
        out.append({k: row.get(k) for k in fields})
    return out


def emit_json(data: Any) -> None:
    if sys.stdout.isatty():
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        click.echo(json.dumps(data, separators=(",", ":"), default=str))


def emit_csv(rows: Sequence[dict], fields: Sequence[str] | None = None) -> None:
    import csv

    rows = list(rows)
    if not rows:
        return
    if fields is None:
        fields = list(rows[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in fields})


def die(message: str, code: int = EXIT_API, hint: str | None = None) -> None:
    click.echo(message, err=True)
    if hint:
        click.echo(hint, err=True)
    raise SystemExit(code)


def note_showing(n: int, *, quiet: bool, noun: str = "results") -> None:
    if quiet or not sys.stderr.isatty():
        return
    click.echo(
        f"Showing {n} {noun}. To narrow: add --limit, --format json --select, or --compact.",
        err=True,
    )


def agent_output_options(
    *,
    default_format: str = "table",
    formats: tuple[str, ...] = ("table", "json"),
) -> Callable:
    """Click decorator: --format / --compact / --select / --quiet / --csv."""

    def decorator(f: Callable) -> Callable:
        f = click.option(
            "--format",
            "fmt",
            type=click.Choice(list(formats)),
            default=default_format,
            show_default=True,
            help="Output format. Piped stdout auto-uses json when format is table.",
        )(f)
        f = click.option(
            "--compact",
            is_flag=True,
            help="High-gravity fields only (id, title/timestamps).",
        )(f)
        f = click.option(
            "--select",
            default=None,
            help="Comma-separated fields (JSON/CSV).",
        )(f)
        f = click.option(
            "--quiet",
            "-q",
            is_flag=True,
            help="Suppress non-data messages on stderr.",
        )(f)
        f = click.option(
            "--csv",
            "as_csv",
            is_flag=True,
            help="CSV to stdout (implies machine output).",
        )(f)
        return f

    return decorator
