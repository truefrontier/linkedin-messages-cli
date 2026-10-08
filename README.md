# linkedin-messages-cli (`limsg`)

Unofficial **agent-native** CLI for LinkedIn messaging. There is no public LinkedIn Messages API; this tool uses a persistent Chrome profile (Playwright `channel=chrome`) the same way [google-keep-cli](../google-keep-cli) and google-gmail-cli do.

**Not affiliated with LinkedIn.** Use only on accounts you control. Prefer dry-run for send.

## Install (M4 MacBook)

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
| `limsg messages history <thread> [--limit N]` | Last N messages (who / text / time) |
| `limsg messages read <thread> [--limit N]` | Alias for `history` |
| `limsg messages send <thread_or_person> --text "..."` | **Dry-run by default** |
| `limsg messages send … --text "…" --yes` | Actually send (`--send` alias) |
| `limsg activity comments [--limit N] [--since ISO]` | Your recent comments (READ-ONLY) |
| `limsg activity reactions [--limit N] [--since ISO]` | Your recent reactions (READ-ONLY) |
| `limsg activity posts [--limit N] [--since ISO]` | Your recent posts and shares (READ-ONLY) |
| `limsg activity export [--format json\|csv] [--out PATH] [--since ISO] [--limit N]` | Comments + reactions + posts in one JSON object, or CSV with a `kind` column |
| `limsg boost list [--since DATE] [--until DATE] [--account ID]` | Every boosted campaign across your personal Boost ad accounts (READ-ONLY) |
| `limsg boost stats <share-urn\|activity-urn\|campaign-id> [--since DATE] [--until DATE]` | Spend, impressions, clicks, CTR, and review status for one boost (READ-ONLY) |

### Agent UX

Same helpers as Keep (`agent_ux.py`):

- Auto-JSON when stdout is not a TTY
- `--format table\|json`, `--compact`, `--select fields`, `--csv`, `--quiet` / `-q`
- Exit codes: `0` ok, `2` usage, `3` not found, `4` auth, `5` API/runtime

Examples:

```bash
limsg messages list --limit 10 --compact
limsg messages list --limit 10 --compact | jq 'length'
limsg messages history Boardy --limit 5
limsg messages read Boardy --limit 5 --format json
limsg messages history Boardy --limit 5 --compact | jq 'map({from,time})'
limsg messages send Boardy --text "ping"           # dry-run only
limsg messages send <thread_id> --text "…" --yes   # live send (gated)
limsg activity comments --limit 10 --compact
limsg activity reactions --since 2026-10-01 --format json
limsg activity posts --limit 5
limsg activity export --format csv --out activity.csv --limit 20
```

`history` / `read` resolve `<thread>` like `send`: peer name substring, thread id (`2-…`), or `entityUrn`. Default `--limit` is 20. Table columns: `time`, `from`, `text` (truncated in table). JSON fields: `id`, `time`, `from`, `text`, `senderUrn`.

Activity rows:

- comments: `id`, `time`, `type`, `text`, `targetUrl`, `targetTitle`
- reactions: `id`, `time`, `type`, `reaction`, `targetUrl`, `targetTitle`
- posts: `id`, `time`, `type`, `text`, `url`

`activity export` JSON is `{"comments": [...], "reactions": [...], "posts": [...]}`. CSV adds a `kind` column. `--since` keeps rows at or after an ISO-8601 timestamp.

### Boost stats (READ-ONLY)

`boost` reads your personal Boost ad accounts (the "Boost" button on a post) from Campaign Manager. It is GET-only.

```bash
limsg boost list                                   # every boosted campaign, newest first
limsg boost list --compact
limsg boost list --since 2026-10-01 --until 2026-10-31 --format json
limsg boost list --account 520268012 --csv
limsg boost stats urn:li:share:7514057966338318336  # one object, not an array
limsg boost stats 910747076                         # by campaign id
limsg boost stats urn:li:share:7514057966338318336 --select status,spend,impressions,clicks,ctr
```

`stats` takes a share urn, an activity urn, a feed URL that contains one, or a campaign id. Boost previews usually reference the `urn:li:share:…` form, so an activity urn may not match; use the share urn or the campaign id from `boost list`. If one post has several boosts, `stats` returns the latest (by start) and adds `otherCampaignIds`. No match exits `3`.

`--since` / `--until` take an ISO date or datetime (UTC; `--until` includes that whole day). The analytics window is your range intersected with the campaign's run: from the campaign start date to the earlier of its end date and today (UTC). `boost list` also drops campaigns that ended before `--since` or start after `--until`.

JSON fields (list items and `stats` share one shape, in this order):

- `contentUrn`, `campaignId`, `campaignName`, `adAccountId`, `campaignGroupId`
- `status` — derived: `COMPLETED` / `ARCHIVED` / `CANCELED` > `REJECTED` > `IN_REVIEW` > `PAUSED` / `DRAFT` > `ON_HOLD` > `SCHEDULED` > `ACTIVE` (else the raw campaign status)
- `statusDetail` (creative review text, HTML stripped), `campaignStatus`, `servingStatuses`, `creativeStatus`
- `objective`, `currency`, `totalBudget`, `dailyBudget`
- `spend`, `spendUsd`, `budgetRemaining`, `spendPct`
- `impressions`, `reach`, `clicks`, `landingPageClicks` (website visits)
- `ctr` (percent), `cpc`, `costPerLandingPageClick`, `cpm` — computed from the raw numbers; `ctrApi` appears only when LinkedIn returns its own CTR
- `reactions`, `comments`, `reposts`, `follows`, `totalEngagements`
- `startAt`, `endAt`, `dateRange` (`{"start","end"}` UTC dates actually queried)
- `metricsSource`, `warnings`, `destinationUrl`, `previewUrl`, `fetchedAt`

`--compact`: `contentUrn`, `campaignId`, `status`, `spend`, `totalBudget`, `currency`, `impressions`, `clicks`, `landingPageClicks`, `ctr`, `cpc`.

Example (`limsg boost stats urn:li:share:7514057966338318336 --format json`, a boost still in ads review):

```json
{
  "contentUrn": "urn:li:share:7514057966338318336",
  "campaignId": 910747076,
  "campaignName": "Boost Post Website Visits October 8, 2026 at 8:35 PM",
  "adAccountId": 520268012,
  "campaignGroupId": 1220524136,
  "status": "IN_REVIEW",
  "statusDetail": "The ad is in review, and will start running once it has been approved. Ads can take up to 24 hours to be approved. Learn more",
  "campaignStatus": "ACTIVE",
  "servingStatuses": ["RUNNABLE"],
  "creativeStatus": "CREATIVE_IN_REVIEW",
  "objective": "WEBSITE_VISIT",
  "currency": "USD",
  "totalBudget": 50.0,
  "dailyBudget": null,
  "spend": 0.0,
  "spendUsd": null,
  "budgetRemaining": 50.0,
  "spendPct": 0.0,
  "impressions": 0,
  "reach": null,
  "clicks": 0,
  "landingPageClicks": 0,
  "ctr": null,
  "cpc": null,
  "costPerLandingPageClick": null,
  "cpm": null,
  "reactions": 0,
  "comments": 0,
  "reposts": 0,
  "follows": null,
  "totalEngagements": 0,
  "startAt": "2026-10-08T20:35:30Z",
  "endAt": "2026-10-12T23:45:00Z",
  "dateRange": {"start": "2026-10-08", "end": "2026-10-08"},
  "metricsSource": "adAnalytics:no-delivery-rows",
  "warnings": [],
  "destinationUrl": "https://www.eventbrite.com/…",
  "previewUrl": "https://linkedin.com/feed/update/urn:li:sponsoredContentV2:(urn:li:share:7514057966338318336,urn:li:sponsoredCreative:1637955746)?actorCompanyId=0&viewContext=REVIEWER",
  "fetchedAt": "2026-10-08T21:00:00Z"
}
```

Null rules (numbers are never invented):

- Analytics returned a row → `metricsSource: "adAnalytics"`. A metric missing from that row is `null`.
- Analytics succeeded (HTTP 200) with no row → LinkedIn reports no delivery in that range: `impressions`, `clicks`, `landingPageClicks`, `reactions`, `comments`, `reposts`, `totalEngagements`, and `spend` are `0`; `metricsSource: "adAnalytics:no-delivery-rows"`. The same applies when the window is empty (for example a boost that has not started); then `dateRange` is `null` and no analytics call is made.
- Analytics failed → every metric is `null`, `metricsSource: null`, and `warnings` names the HTTP status.
- `ctr`, `cpm` are `null` when impressions are 0 or null; `cpc` when clicks are; `costPerLandingPageClick` when landing page clicks are.

Engagement metrics are paid (sponsored) engagement as reported by Campaign Manager. Organic engagement on the same post is not included.

If the session is missing or Campaign Manager wants sign-in, `boost` exits `4`. Run `limsg login`, then open https://www.linkedin.com/campaignmanager/accounts once in that Chrome if Campaign Manager asks for anything.

## How it talks to LinkedIn

Prefer replaying Voyager / Messaging GraphQL with the page’s own cookies (no cookie printing):

- List: `GET /voyager/api/voyagerMessagingGraphQL/graphql` (`queryId=messengerConversations.…`) with fallback `GET /voyager/api/messaging/conversations`
- History: `GET …/voyagerMessagingGraphQL/graphql` (`queryId=messengerMessages.…`, `variables=(conversationUrn:…)`) with fallback `GET /voyager/api/messaging/conversations/{threadId}/events`
- Send: `POST /voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage`
- Activity (READ-ONLY GET, sniffed from Recent activity — no `queryId`): the signed-in profile documents
  - `GET /in/{vanity}/recent-activity/comments/`
  - `GET /in/{vanity}/recent-activity/reactions/`
  - `GET /in/{vanity}/recent-activity/all/` (posts and shares)

- Boost (READ-ONLY GET, sniffed from the Boost management page) from `https://www.linkedin.com/robots.txt` (same origin, no app JS), all under `https://www.linkedin.com/campaign-manager-api/`:
  - `GET campaignManagerAccounts?q=memberBoostAccounts`
  - `GET campaignManagerReportingCampaigns?accountId={acct}&q=accountAndCampaignGroups`
  - `GET campaignManagerCampaigns/{id}`
  - `GET campaignManagerReportingCreatives?accountId={acct}&q=cursorBasedCriteria&campaignIds=List({id})&count=10`
  - `GET campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL&dateRange=(…)&campaignIds=List({id})&fields=…` (the field list is fixed; an unknown field name returns HTTP 500)

`/in/me/recent-activity/...` only identifies the vanity URL; the list itself renders on `/in/{vanity}/recent-activity/...`. `queryId` hashes for messaging rotate; login/list/activity sniff captures update `~/.linkedin-messages-cli/capture/`.

## Safety

- **Default never sends.** Without `--yes` / `--send`, `messages send` prints a dry-run JSON preview and exits 0.
- **`history` / `read` are READ-ONLY** (GET only). They never call `createMessage`.
- **`activity` is READ-ONLY.** `comments`, `reactions`, `posts`, and `export` only open Recent activity and read it. They do not post, react, comment, or send DMs, and they never call `createMessage`.
- **`boost` is READ-ONLY:** GET only against campaign-manager-api; never edits, pauses, resumes, deletes, or changes budgets; never opens Campaign Manager UI pages. It never calls `?action=` endpoints such as `fetchCurrentRunningSpend` or `billingConfiguration`.
- Do not blast Boardy or anyone. Prefer `messages list` / `messages history` for smoke tests; dry-run send only on throwaway threads.

## License

MIT © True Frontier
