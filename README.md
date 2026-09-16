# linkedin-messages-cli (`limsg`)

Unofficial **agent-native** CLI for LinkedIn messaging. There is no public LinkedIn Messages API; this tool uses a persistent Chrome profile (Playwright `channel=chrome`) the same way [google-keep-cli](../google-keep-cli) and google-gmail-cli do.

**Not affiliated with LinkedIn.** Use only on accounts you control. Prefer dry-run for send.

## Install (Codefi Mac)

```bash
cd ~/Sites/truefrontier/linkedin-messages-cli
pipx install -e .
# or: pipx install -e ~/Sites/truefrontier/linkedin-messages-cli
which limsg
limsg --help
```

Requires Google Chrome installed. Playwright uses your system Chrome (`channel=chrome`); you do not need `playwright install chromium`.

## Session

```bash
limsg login
```

Opens headful Chrome to `https://www.linkedin.com/messaging/`. Sign in if needed; the CLI waits until the messaging UI is visible, then saves the profile under:

```text
~/.linkedin-messages-cli/chrome-profile/
```

Scrubbed API shape captures (paths + field names only; message bodies redacted) land in:

```text
~/.linkedin-messages-cli/capture/
```

Never commit `chrome-profile/` or `capture/`. Never paste cookies or tokens into chat, README samples, or commits.

## Commands

| Command | Purpose |
|--------|---------|
| `limsg login` | Headful Chrome login / session refresh |
| `limsg messages list [--limit N]` | Recent DM threads |
| `limsg messages send <thread_or_person> --text "..."` | **Dry-run by default** |
| `limsg messages send … --text "…" --yes` | Actually send (`--send` alias) |

### Agent UX

Same helpers as Keep (`agent_ux.py`):

- Auto-JSON when stdout is not a TTY
- `--format table\|json`, `--compact`, `--select fields`, `--csv`, `--quiet` / `-q`
- Exit codes: `0` ok, `2` usage, `3` not found, `4` auth, `5` API/runtime

Examples:

```bash
limsg messages list --limit 10 --compact
limsg messages list --limit 10 --compact | jq 'length'
limsg messages send Boardy --text "ping"           # dry-run only
limsg messages send <thread_id> --text "…" --yes   # live send (gated)
```

## How it talks to LinkedIn

Prefer replaying Voyager / Messaging GraphQL with the page’s own cookies (no cookie printing):

- List: `GET /voyager/api/voyagerMessagingGraphQL/graphql` (`queryId=messengerConversations.…`) with fallback `GET /voyager/api/messaging/conversations`
- Send: `POST /voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage`

`queryId` hashes rotate; login/list sniff captures update `~/.linkedin-messages-cli/capture/`.

## Safety

- **Default never sends.** Without `--yes` / `--send`, `messages send` prints a dry-run JSON preview and exits 0.
- Do not blast Boardy or anyone. Smoke-test `messages list` only unless you intentionally dry-run against a throwaway thread.

## License

MIT © True Frontier
