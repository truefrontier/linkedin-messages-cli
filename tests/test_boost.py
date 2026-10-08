"""Offline tests for limsg_cli.boost. Run: python3 tests/test_boost.py (or pytest)."""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from limsg_cli.boost import (  # noqa: E402
    _cm_get,
    analytics_path,
    analytics_window,
    boost_record,
    content_urns,
    derive_status,
    parse_target,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
START_MS = 1791491730522  # 2026-10-08T20:35:30Z
END_MS = 1791848700000  # 2026-10-12T23:45:00Z

CAMPAIGN = {
    "id": 111,
    "name": "Boost Post Website Visits",
    "status": "ACTIVE",
    "servingStatus": "RUNNABLE",
    "servingStatuses": ["RUNNABLE"],
    "objectiveType": "WEBSITE_VISIT",
    "totalBudget": {"currencyCode": "USD", "amount": "50"},
    "startsAt": START_MS,
    "endsAt": END_MS,
    "campaignGroupId": 222,
}
ACCOUNT = {"id": 333, "type": "PERSONAL", "status": "ACTIVE", "currencyCode": "USD"}
PREVIEW = (
    "https://linkedin.com/feed/update/urn:li:sponsoredContentV2:"
    "(urn:li:share:123456789,urn:li:sponsoredCreative:444)?actorCompanyId=0&viewContext=REVIEWER"
)
IN_REVIEW = {
    "id": 444,
    "campaignId": 111,
    "status": "CREATIVE_IN_REVIEW",
    "statusV2": {
        "status": "IN_REVIEW",
        "statusDetails": ["The ad is in review.\n\n<a href=\"https://example.com\">Learn more</a>"],
    },
    "previewUrl": PREVIEW,
    "destinationUrl": "https://example.com/event",
}
APPROVED = dict(IN_REVIEW, status="ACTIVE", statusV2={"status": "ACTIVE", "statusDetails": []})

SPEC_KEYS = [
    "contentUrn", "campaignId", "campaignName", "adAccountId", "campaignGroupId", "status",
    "statusDetail", "campaignStatus", "servingStatuses", "creativeStatus", "objective", "currency",
    "totalBudget", "dailyBudget", "spend", "spendUsd", "budgetRemaining", "spendPct", "impressions",
    "reach", "clicks", "landingPageClicks", "ctr", "cpc", "costPerLandingPageClick", "cpm",
    "reactions", "comments", "reposts", "follows", "totalEngagements", "startAt", "endAt",
    "dateRange", "metricsSource", "warnings", "destinationUrl", "previewUrl", "fetchedAt",
]


def _record(creatives, analytics, window=(date(2026, 10, 8), date(2026, 10, 9))):
    return boost_record(CAMPAIGN, creatives, analytics, account=ACCOUNT, date_range=window, now=NOW)


def test_share_urn_from_preview_url():
    assert content_urns([IN_REVIEW]) == ["urn:li:share:123456789"]
    encoded = dict(IN_REVIEW, previewUrl="https://linkedin.com/feed/update/urn%3Ali%3Ashare%3A987%2Curn")
    assert content_urns([encoded]) == ["urn:li:share:987"]
    assert content_urns([{"previewUrl": None}]) == []


def test_status_in_review_active_completed_scheduled():
    assert derive_status(CAMPAIGN, [IN_REVIEW], NOW) == "IN_REVIEW"
    assert derive_status(CAMPAIGN, [APPROVED], NOW) == "ACTIVE"
    assert derive_status(dict(CAMPAIGN, status="COMPLETED"), [IN_REVIEW], NOW) == "COMPLETED"
    after_end = datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert derive_status(CAMPAIGN, [APPROVED], after_end) == "COMPLETED"
    before_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert derive_status(CAMPAIGN, [APPROVED], before_start) == "SCHEDULED"
    rejected = dict(APPROVED, statusV2={"status": "REJECTED"})
    assert derive_status(CAMPAIGN, [rejected], NOW) == "REJECTED"
    assert derive_status(dict(CAMPAIGN, servingStatuses=["ACCOUNT_ON_HOLD"]), [APPROVED], NOW) == "ON_HOLD"


def test_zero_rows_report_zero_with_null_ratios():
    rec = _record([IN_REVIEW], (200, {"elements": [], "paging": {"total": 0}}))
    assert list(rec.keys()) == SPEC_KEYS
    assert rec["metricsSource"] == "adAnalytics:no-delivery-rows"
    assert (rec["impressions"], rec["clicks"], rec["landingPageClicks"], rec["spend"]) == (0, 0, 0, 0.0)
    assert (rec["reactions"], rec["comments"], rec["reposts"], rec["totalEngagements"]) == (0, 0, 0, 0)
    assert (rec["ctr"], rec["cpc"], rec["cpm"], rec["costPerLandingPageClick"]) == (None, None, None, None)
    assert (rec["reach"], rec["spendUsd"], rec["follows"]) == (None, None, None)
    assert (rec["totalBudget"], rec["budgetRemaining"], rec["spendPct"]) == (50.0, 50.0, 0.0)
    assert rec["status"] == "IN_REVIEW"
    assert rec["statusDetail"] == "The ad is in review. Learn more"
    assert rec["contentUrn"] == "urn:li:share:123456789"
    assert (rec["campaignId"], rec["adAccountId"], rec["campaignGroupId"]) == (111, 333, 222)
    assert (rec["startAt"], rec["endAt"]) == ("2026-10-08T20:35:30Z", "2026-10-12T23:45:00Z")
    assert rec["dateRange"] == {"start": "2026-10-08", "end": "2026-10-09"}
    assert rec["warnings"] == []


def test_metrics_row_computes_ratios():
    row = {
        "pivotValues": ["urn:li:sponsoredCampaign:111"],
        "baseMetrics": {
            "costInLocalCurrency": "12.34",
            "costInUsd": 12.34,
            "impressions": "2000",
            "clicks": 25,
            "landingPageClicks": 20,
            "totalEngagements": 40,
        },
        "socialMetrics": {"reactions": 9, "comments": 2, "shares": 1, "follows": 3},
        "deliveryMetrics": {"reach": 1500},
    }
    other = {"pivotValues": ["urn:li:sponsoredCampaign:999"], "baseMetrics": {"impressions": 7}}
    rec = _record([APPROVED], (200, {"elements": [other, row]}))
    assert rec["metricsSource"] == "adAnalytics"
    assert (rec["spend"], rec["impressions"], rec["clicks"], rec["reach"]) == (12.34, 2000, 25, 1500)
    assert (rec["ctr"], rec["cpc"], rec["costPerLandingPageClick"], rec["cpm"]) == (1.25, 0.49, 0.62, 6.17)
    assert (rec["reactions"], rec["comments"], rec["reposts"], rec["follows"]) == (9, 2, 1, 3)
    assert (rec["budgetRemaining"], rec["spendPct"]) == (37.66, 24.7)
    assert "ctrApi" not in rec
    with_api = dict(row, baseMetrics=dict(row["baseMetrics"], clickThroughRate=0.0125))
    rec = _record([APPROVED], (200, {"data": {"elements": [with_api]}}))
    assert rec["ctrApi"] == 0.0125 and rec["ctr"] == 1.25


def test_analytics_failure_reports_nulls():
    rec = _record([APPROVED], (500, {"message": "Internal Server Error"}))
    assert rec["metricsSource"] is None
    assert rec["warnings"] == ["adAnalytics HTTP 500"]
    for key in ("spend", "impressions", "clicks", "landingPageClicks", "ctr", "cpc", "budgetRemaining"):
        assert rec[key] is None, key


def test_unmatched_rows_are_not_borrowed():
    other = {"pivotValues": ["urn:li:sponsoredCampaign:999"], "baseMetrics": {"impressions": 7}}
    pivotless = {"baseMetrics": {"impressions": 5}}
    rec = _record([APPROVED], (200, {"elements": [other, pivotless]}))
    assert rec["impressions"] == 0 and rec["metricsSource"] == "adAnalytics:no-delivery-rows"
    rec = _record([APPROVED], (200, {"elements": [pivotless]}))
    assert rec["impressions"] == 5 and rec["metricsSource"] == "adAnalytics"


def test_non_json_200_is_a_failure():
    rec = _record([APPROVED], (200, None))
    assert rec["impressions"] is None and rec["metricsSource"] is None
    assert rec["warnings"] == ["adAnalytics HTTP 200 (non-JSON)"]


def test_empty_window_skips_analytics_as_zero_row():
    future = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert analytics_window(CAMPAIGN, since=None, until=None, now=future) is None
    rec = _record([APPROVED], None, window=None)
    assert rec["dateRange"] is None and rec["impressions"] == 0
    assert rec["metricsSource"] == "adAnalytics:no-delivery-rows"


def test_analytics_window_clamps():
    assert analytics_window(CAMPAIGN, since=None, until=None, now=NOW) == (date(2026, 10, 8), date(2026, 10, 9))
    late = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert analytics_window(CAMPAIGN, since=None, until=None, now=late) == (date(2026, 10, 8), date(2026, 10, 12))
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    until = datetime(2026, 10, 11, 5, tzinfo=timezone.utc)
    assert analytics_window(CAMPAIGN, since=since, until=until, now=late) == (date(2026, 10, 10), date(2026, 10, 11))


def test_analytics_query_string_exact():
    path = analytics_path([111, 222], date(2026, 10, 8), date(2026, 10, 9))
    assert path == (
        "campaignManagerAdAnalytics?q=statistics&pivots=List(CAMPAIGN)&timeGranularity=ALL"
        "&dateRange=(start:(day:8,month:10,year:2026),end:(day:9,month:10,year:2026))"
        "&campaignIds=List(111,222)"
        "&fields=baseMetrics:(costInLocalCurrency,costInUsd,impressions,clicks,landingPageClicks,"
        "clickThroughRate,costPerClick,totalEngagements,averageEngagementRate,costPerThousandImpressions),"
        "socialMetrics:(reactions,comments,shares,follows,otherEngagements,totalSocialActions),"
        "deliveryMetrics:(reach,averageFrequency),pivotValues,dateRange"
    )


def test_parse_target():
    assert parse_target("910") == ("campaign", 910)
    assert parse_target("urn:li:sponsoredCampaign:910") == ("campaign", 910)
    assert parse_target("urn:li:share:123") == ("urn", "urn:li:share:123")
    assert parse_target("https://www.linkedin.com/feed/update/urn:li:activity:55/") == ("urn", "urn:li:activity:55")
    try:
        parse_target("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_cm_get_refuses_writes():
    for method, path in (("POST", "campaignManagerAccounts"), ("GET", "x?action=fetchCurrentRunningSpend"),
                         ("GET", "billingConfiguration?ACTION%3DfindOrCreate"), ("GET", "https://evil/x")):
        try:
            asyncio.run(_cm_get(None, path, method=method))
        except RuntimeError:
            continue
        raise AssertionError(f"{method} {path} was not refused")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")
