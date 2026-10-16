"""Offline tests for `limsg post create`. Network is faked; nothing reaches LinkedIn.

Most tests fake page.evaluate (the in-page fetch boundary). One test runs the real write
JavaScript in a throwaway headless Chrome against a local HTTP server; it skips without Chrome.

Run: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from limsg_cli import post  # noqa: E402
from limsg_cli.activity import AuthRequired, ProfileBusy  # noqa: E402
from limsg_cli.cli import main  # noqa: E402

ME = "https://www.linkedin.com/voyager/api/me"
REGISTER = "https://www.linkedin.com/voyager/api/voyagerVideoDashMediaUploadMetadata?action=upload"
UPLOAD = "https://www.linkedin.com/dms-uploads/sp/v2/D4E10AQ_fake/uploaded-image/0"
CREATE = "https://www.linkedin.com/voyager/api/contentcreation/normShares"
ASSET = "urn:li:digitalmediaAsset:D4E10AQ_fake"
SHARE = "urn:li:share:7384000000000000001"
REAL_CONFIG_FILES = post.CONFIG_FILES  # before the autouse fixture swaps in tmp paths


def resp(status: int, body=None, restli_id=None) -> dict:
    """A page.evaluate result in both shapes: api._page_fetch reads json, post._write reads text."""
    return {"status": status, "json": body, "text": "" if body is None else json.dumps(body), "restliId": restli_id}


MINI = "com.linkedin.voyager.identity.shared.MiniProfile"
TEST_PROFILE = {
    "$type": MINI,
    "entityUrn": "urn:li:fs_miniProfile:ACoAAFake",
    "dashEntityUrn": "urn:li:fsd_profile:ACoAAFake",
    "objectUrn": "urn:li:member:4242",
    "firstName": "Test",
    "lastName": "Author",
    "publicIdentifier": "test-author",
}
# The real incident: two accounts named Kevin Kirchner. kevnk is the one posts must come from.
KEVNK = {
    "$type": MINI,
    "entityUrn": "urn:li:fs_miniProfile:ACoAAAIJK2MBOkk_kX24uyFpDexBxwtR6BXzBqw",
    "dashEntityUrn": "urn:li:fsd_profile:ACoAAAIJK2MBOkk_kX24uyFpDexBxwtR6BXzBqw",
    "objectUrn": "urn:li:member:34155363",
    "firstName": "Kevin",
    "lastName": "Kirchner",
    "publicIdentifier": "kevnk",
}
OTHER = {
    "$type": MINI,
    "entityUrn": "urn:li:fs_miniProfile:ACoAAOtherKevin",
    "dashEntityUrn": "urn:li:fsd_profile:ACoAAOtherKevin",
    "objectUrn": "urn:li:member:999",
    "firstName": "Kevin",
    "lastName": "Kirchner",
    "publicIdentifier": "kevin-kirchner-817709300",
}


def me_as(profile: dict, *others: dict) -> dict:
    """GET /me signed in as `profile`, with `others` listed first in included."""
    return resp(
        200,
        {"data": {"*miniProfile": profile["entityUrn"], "$type": "com.linkedin.voyager.common.Me"}, "included": [*others, profile]},
    )


ME_OK = resp(
    200,
    {
        "data": {"plainId": 4242, "*miniProfile": "urn:li:fs_miniProfile:ACoAAFake", "$type": "com.linkedin.voyager.common.Me"},
        "included": [TEST_PROFILE],
    },
)
AUTHOR = {
    "name": "Test Author",
    "publicIdentifier": "test-author",
    "profileUrn": "urn:li:fsd_profile:ACoAAFake",
    "memberUrn": "urn:li:member:4242",
}
REGISTER_OK = resp(
    200,
    {
        "data": {
            "value": {
                "urn": ASSET,
                "singleUploadUrl": UPLOAD,
                "singleUploadHeaders": {"media-type-family": "STILLIMAGE"},
                "type": "SINGLE",
            },
            "$type": "com.linkedin.restli.common.ActionResponse",
        },
        "included": [],
    },
)
UPLOAD_OK = resp(201)
CREATE_OK = resp(
    201,
    {"data": {"status": {"urn": SHARE, "toastCtaText": "View post"}}, "included": [{"urn": "urn:li:activity:7384000000000000009"}]},
)
ALL_OK = {("GET", ME): ME_OK, ("POST", REGISTER): REGISTER_OK, ("PUT", UPLOAD): UPLOAD_OK, ("POST", CREATE): CREATE_OK}
PUBLISH_CALLS = [("GET", ME), ("POST", REGISTER), ("PUT", UPLOAD), ("GET", ME), ("POST", CREATE)]


@pytest.fixture(autouse=True)
def author_config(monkeypatch, tmp_path):
    """Never read the real ~/.linkedin-messages-cli config. Expect the fixture author unless a test says otherwise."""
    monkeypatch.setattr(post, "CONFIG_FILES", (tmp_path / "config.toml", tmp_path / "config.json"))
    monkeypatch.setenv(post.EXPECT_AUTHOR_ENV, "test-author")


class FakePage:
    """Answers page.evaluate by (method, url) and records every call. Unknown calls fail the test.

    A list answers successive calls in order.
    """

    def __init__(self, responses: dict, cookies: list[dict] | None = None):
        self.responses = responses
        self.cookie_jar = [{"name": "JSESSIONID", "value": '"ajax:0000"'}] if cookies is None else cookies
        self.calls: list[dict] = []
        self.context = self

    async def cookies(self):
        return self.cookie_jar

    async def evaluate(self, script, arg):
        self.calls.append(arg)
        key = (arg["method"], arg["url"])
        assert key in self.responses, f"unexpected network call {key}"
        answer = self.responses[key]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def methods_urls(self):
        return [(c["method"], c["url"]) for c in self.calls]


def png_bytes(width=1200, height=627, pad=64) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\0\0\0" + b"\0" * pad


def jpeg_bytes(width=640, height=480, fill=b"") -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
    sof0 = fill + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\0" * 9
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "card.png"
    path.write_bytes(png_bytes())
    return path


def run(page, text, image_path, *, publish_now, expect_author="test-author"):
    image = post.load_image(str(image_path))
    return asyncio.run(post.run(page, text, image, publish_now=publish_now, expect_author=expect_author))


# ---------------------------------------------------------------- dry-run makes no writes


def test_dry_run_only_reads_me(png):
    page = FakePage(ALL_OK)

    out = run(page, "Hello feed", png, publish_now=False)

    assert page.methods_urls() == [("GET", ME)]
    assert out == {
        "dryRun": True,
        "loggedIn": True,
        "author": AUTHOR,
        "expectedAuthor": "test-author",
        "authorMatches": True,
        "text": "Hello feed",
        "textLength": 10,
        "image": {
            "path": str(png.resolve()),
            "filename": "card.png",
            "type": "image/png",
            "bytes": len(png_bytes()),
            "width": 1200,
            "height": 627,
        },
    }


def test_not_logged_in_raises_auth_and_makes_no_writes(png):
    for responses, cookies in (
        ({("GET", ME): resp(401, {"status": 401})}, None),
        ({("GET", ME): resp(200, {"rawType": "text", "length": 900})}, None),  # followed a login redirect
        ({}, []),  # no JSESSIONID cookie: no request at all
    ):
        page = FakePage(responses, cookies=cookies)
        with pytest.raises(AuthRequired):
            run(page, "Hello", png, publish_now=True)
        assert all(c["method"] == "GET" for c in page.calls)


# ---------------------------------------------------------------- confirm path


def test_confirm_registers_uploads_creates_in_order(png):
    page = FakePage(ALL_OK)

    out = run(page, "Launch day.\n\nDetails below.", png, publish_now=True)

    assert page.methods_urls() == PUBLISH_CALLS
    register, upload, _, create = page.calls[1:]
    assert register["json"] == {"mediaUploadType": "IMAGE_SHARING", "fileSize": len(png_bytes()), "filename": "card.png"}
    assert register["headers"]["csrf-token"] == "ajax:0000"
    assert register["headers"]["content-type"] == "application/json"
    assert register["b64"] is None
    assert base64.b64decode(upload["b64"]) == png_bytes()
    assert upload["json"] is None
    assert upload["headers"] == {"csrf-token": "ajax:0000", "content-type": "image/png", "media-type-family": "STILLIMAGE"}
    assert create["json"] == {
        "visibleToConnectionsOnly": False,
        "externalAudienceProviders": [],
        "commentaryV2": {"text": "Launch day.\n\nDetails below.", "attributes": []},
        "origin": "FEED",
        "allowedCommentersScope": "ALL",
        "postState": "PUBLISHED",
        "media": [{"category": "IMAGE", "mediaUrn": ASSET, "tapTargets": []}],
    }
    assert out["dryRun"] is False and out["posted"] is True
    assert out["urn"] == SHARE
    assert out["url"] == "https://www.linkedin.com/feed/update/urn:li:share:7384000000000000001/"
    assert out["image"]["assetUrn"] == ASSET
    assert out["author"] == AUTHOR
    assert out["expectedAuthor"] == "test-author" and out["authorMatches"] is True
    assert "warning" not in out


@pytest.mark.parametrize(
    "register, message",
    [
        (resp(500, {}), "register failed HTTP 500. Nothing was posted"),
        (resp(0), "register failed HTTP 0. Nothing was posted"),
        (resp(200, {"data": {"value": {"urn": ASSET}}}), "no asset urn or upload URL. Nothing was posted"),
        (resp(200, {"data": {"value": {"urn": ASSET, "type": "MULTIPART", "partUploadRequests": []}}}), "MULTIPART upload"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "https://evil.example/up"}}), "unexpected URL host 'evil.example'"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "http://www.linkedin.com/up"}}), "unexpected URL host"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "https://evil.example\\@www.linkedin.com/up"}}), "unexpected URL host"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "https://u:p@www.linkedin.com/up"}}), "unexpected URL host"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "https://www.linkedin.com:8443/up"}}), "unexpected URL host"),
        (resp(200, {"value": {"urn": ASSET, "singleUploadUrl": "https://linkedin.com.evil.example/up"}}), "unexpected URL host"),
    ],
)
def test_register_problems_stop_before_upload(png, register, message):
    page = FakePage({**ALL_OK, ("POST", REGISTER): register})

    with pytest.raises(RuntimeError, match=message):
        run(page, "Hello", png, publish_now=True)

    assert page.methods_urls() == [("GET", ME), ("POST", REGISTER)]


@pytest.mark.parametrize("status", [403, 0])
def test_upload_failure_stops_before_create(png, status):
    page = FakePage({**ALL_OK, ("PUT", UPLOAD): resp(status)})

    with pytest.raises(RuntimeError, match=f"upload failed HTTP {status}. Nothing was posted"):
        run(page, "Hello", png, publish_now=True)

    assert page.methods_urls() == [("GET", ME), ("POST", REGISTER), ("PUT", UPLOAD)]


@pytest.mark.parametrize(
    "create, message",
    [
        (resp(422, {"message": "bad"}), r"rejected the post \(HTTP 422\). Nothing was posted"),
        (resp(500, {}), "got HTTP 500. The post may be live; check your profile before retrying"),
        (resp(0), "got no response. The post may be live; check your profile before retrying"),
        (RuntimeError("Target page, context or browser has been closed"), "got no response. The post may be live"),
    ],
)
def test_create_failures_are_sent_once_and_say_whether_it_may_be_live(png, create, message):
    page = FakePage({**ALL_OK, ("POST", CREATE): create})

    with pytest.raises(RuntimeError, match=message):
        run(page, "Hello", png, publish_now=True)

    assert page.methods_urls().count(("POST", CREATE)) == 1


def test_create_urn_from_restli_id_header_when_body_is_empty(png):
    page = FakePage({**ALL_OK, ("POST", CREATE): resp(201, restli_id="urn%3Ali%3Ashare%3A42")})

    out = run(page, "Hello", png, publish_now=True)

    assert (out["urn"], out["url"]) == ("urn:li:share:42", "https://www.linkedin.com/feed/update/urn:li:share:42/")
    assert "warning" not in out


@pytest.mark.parametrize(
    "create",
    [
        resp(201),
        # A post urn elsewhere in the body may be another post, even someone else's: not ours.
        resp(201, {"data": {}, "included": [{"entityUrn": "urn:li:share:999", "urn": "urn:li:activity:998"}]}),
    ],
)
def test_create_2xx_without_urn_reports_posted_with_warning(png, create):
    page = FakePage({**ALL_OK, ("POST", CREATE): create})

    out = run(page, "Hello", png, publish_now=True)

    assert out["posted"] is True and out["urn"] is None and out["url"] is None
    assert out["warning"] == (
        "LinkedIn accepted the post (HTTP 201) but the response had no post urn. "
        "Check your profile; do NOT re-run or it will post twice."
    )


def test_cli_2xx_without_urn_exits_0_and_warns(monkeypatch, png):
    fake_session(monkeypatch, FakePage({**ALL_OK, ("POST", CREATE): resp(201)}))

    result, _ = cli("--text", "hi", "--image", str(png), "--confirm", "--format", "text")

    assert result.exit_code == 0, result.output
    assert "WARNING: LinkedIn accepted the post (HTTP 201)" in result.output
    assert "URN:    (unknown)" in result.output


def test_created_urn_parsing():
    assert post.parse_created_urn({"data": {"status": {"urn": SHARE}}}) == SHARE
    assert post.parse_created_urn({"value": {"urn": "urn:li:ugcPost:12"}}) == "urn:li:ugcPost:12"
    assert post.parse_created_urn({"data": {}}, "urn%3Ali%3Ashare%3A42") == "urn:li:share:42"
    assert post.parse_created_urn({"data": {}}) is None
    assert post.parse_created_urn(None, "not-a-urn") is None
    # Post urns elsewhere in the body (included items, echoed text) are never reported as the new post.
    assert post.parse_created_urn({"data": {"x": 1}, "included": [{"urn": "urn:li:activity:99"}]}) is None
    assert post.parse_created_urn({"included": [{"commentary": {"text": "urn:li:share:111"}}, {"entityUrn": "urn:li:share:222"}]}) is None


# ---------------------------------------------------------------- author identity


def test_parse_me_takes_the_profile_mini_profile_points_at_not_the_first_included():
    body = {"data": {"plainId": 34155363, "*miniProfile": KEVNK["entityUrn"]}, "included": [OTHER, KEVNK]}

    assert post.parse_me(body) == {
        "name": "Kevin Kirchner",
        "publicIdentifier": "kevnk",
        "profileUrn": "urn:li:fsd_profile:ACoAAAIJK2MBOkk_kX24uyFpDexBxwtR6BXzBqw",
        "memberUrn": "urn:li:member:34155363",
    }
    body["data"]["*miniProfile"] = OTHER["entityUrn"]
    body["included"] = [KEVNK, OTHER]
    assert post.parse_me(body)["publicIdentifier"] == "kevin-kirchner-817709300"


@pytest.mark.parametrize(
    "body, handle",
    [
        ({"miniProfile": KEVNK, "plainId": 34155363}, "kevnk"),  # not normalized: profile inline
        ({"data": {"miniProfile": {"entityUrn": KEVNK["entityUrn"]}}, "included": [OTHER, KEVNK]}, "kevnk"),
        ({"data": {"*miniProfile": KEVNK["dashEntityUrn"]}, "included": [OTHER, KEVNK]}, "kevnk"),
        ({"data": {}, "included": [{"$type": "x"}, KEVNK]}, "kevnk"),  # no pointer, one profile
        ({"data": {}, "included": [OTHER, KEVNK]}, None),  # no pointer, two profiles
        ({"data": {"*miniProfile": "urn:li:fs_miniProfile:ACoAAGone"}, "included": [OTHER]}, None),
        ({"data": {"*miniProfile": KEVNK["entityUrn"]}, "included": [OTHER]}, None),
        ({"data": {"*miniProfile": KEVNK["entityUrn"]}, "included": [KEVNK, dict(KEVNK)]}, None),  # pointer matches two
        ({"data": {}, "included": []}, None),
        ({"data": {}, "included": [{"publicIdentifier": "kevnk"}]}, None),  # no urns to re-check against
        ({"data": {}, "included": [{**KEVNK, "objectUrn": None}]}, None),  # no member urn, no plainId
        ({"data": {"plainId": 34155363}, "included": [{**KEVNK, "objectUrn": None}]}, "kevnk"),  # plainId fills it
    ],
)
def test_parse_me_needs_one_unambiguous_profile(body, handle):
    author = post.parse_me(body)

    assert (author["publicIdentifier"] if author else None) == handle


def test_wrong_account_refuses_publish_with_zero_writes(png):
    page = FakePage({**ALL_OK, ("GET", ME): me_as(OTHER, KEVNK)})  # old parse_me reported kevnk here

    out = run(page, "Hello", png, publish_now=True, expect_author="kevnk")

    assert page.methods_urls() == [("GET", ME)]
    assert out["posted"] is False and out["authorMatches"] is False
    assert out["expectedAuthor"] == "kevnk"
    assert out["author"] == {
        "name": "Kevin Kirchner",
        "publicIdentifier": "kevin-kirchner-817709300",
        "profileUrn": "urn:li:fsd_profile:ACoAAOtherKevin",
        "memberUrn": "urn:li:member:999",
    }
    assert "urn" not in out and "assetUrn" not in out["image"]


def test_matching_account_publishes_ignoring_handle_case(png):
    page = FakePage({**ALL_OK, ("GET", ME): me_as(KEVNK, OTHER)})

    out = run(page, "Hello", png, publish_now=True, expect_author="KevNK")

    assert page.methods_urls() == PUBLISH_CALLS
    assert out["posted"] is True and out["authorMatches"] is True and out["urn"] == SHARE
    assert out["author"]["publicIdentifier"] == "kevnk"


@pytest.mark.parametrize(
    "second_me, message",
    [
        (me_as(OTHER), "changed from Test Author (test-author, urn:li:member:4242) to "
                       "Kevin Kirchner (kevin-kirchner-817709300, urn:li:member:999)"),
        (me_as({**TEST_PROFILE, "objectUrn": "urn:li:member:5"}), "changed from Test Author"),  # same handle, other member
        (resp(200, {"data": {}, "included": [OTHER, KEVNK]}), "could not re-check the signed-in account"),
        (resp(401, {}), "could not re-check the signed-in account"),
        (RuntimeError("Target page, context or browser has been closed"), "could not re-check the signed-in account"),
    ],
)
def test_author_recheck_before_create_refuses_if_account_changed(png, second_me, message):
    page = FakePage({**ALL_OK, ("GET", ME): [ME_OK, second_me]})

    with pytest.raises(post.AuthorRefused, match=re.escape(message) + ".*Nothing was posted"):
        run(page, "Hello", png, publish_now=True)

    assert page.methods_urls() == [("GET", ME), ("POST", REGISTER), ("PUT", UPLOAD), ("GET", ME)]


def test_expected_author_resolution_order(monkeypatch):
    home = Path.home() / ".linkedin-messages-cli"
    assert REAL_CONFIG_FILES == (home / "config.toml", home / "config.json")
    toml, js = post.CONFIG_FILES
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)

    assert post.resolve_expect_author(None) == "kevnk"
    js.write_text('{"expect_author": "from-json"}', encoding="utf-8")
    assert post.resolve_expect_author(None) == "from-json"
    toml.write_text('[other]\nexpect_author = "not-top-level"\n', encoding="utf-8")
    assert post.resolve_expect_author(None) == "from-json"
    toml.write_text('# mine\nexpect_author = "from-toml"  # handle\n', encoding="utf-8")
    assert post.resolve_expect_author(None) == "from-toml"
    monkeypatch.setenv(post.EXPECT_AUTHOR_ENV, "from-env")
    assert post.resolve_expect_author(None) == "from-env"
    assert post.resolve_expect_author(" From-Flag ") == "From-Flag"


@pytest.mark.parametrize(
    "flag, env, file, content, message",
    [
        ("", None, None, None, "Expected author must be a LinkedIn handle"),
        (None, "  ", None, None, "Expected author must be a LinkedIn handle"),
        (None, None, "config.json", '{"expect_author": 7}', "got 7"),
        (None, None, "config.json", '{"expect_author": ""}', "got ''"),
        (None, None, "config.json", "{not json", "Cannot read config"),
        (None, None, "config.json", '["kevnk"]', "must be a JSON object"),
        (None, None, "config.toml", "expect_author = kevnk", "Cannot read config"),
        (None, None, "config.toml", 'expect_author = ""', "got ''"),
    ],
)
def test_expected_author_errors(monkeypatch, tmp_path, flag, env, file, content, message):
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)
    if env is not None:
        monkeypatch.setenv(post.EXPECT_AUTHOR_ENV, env)
    if file:
        (tmp_path / file).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(message)):
        post.resolve_expect_author(flag)


# ---------------------------------------------------------------- validation


def test_text_and_text_file_are_mutually_exclusive_and_one_is_required(tmp_path):
    f = tmp_path / "post.txt"
    f.write_text("from file\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one of --text or --text-file"):
        post.load_text("a", str(f))
    with pytest.raises(ValueError, match="exactly one of --text or --text-file"):
        post.load_text(None, None)
    assert post.load_text(None, str(f)) == "from file"
    assert post.load_text("  padded \n", None) == "padded"


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_empty_text_is_rejected(text):
    with pytest.raises(ValueError, match="Post text is empty"):
        post.load_text(text, None)


def test_text_length_limit_counts_utf16_units():
    assert post.load_text("x" * 3000, None) == "x" * 3000
    with pytest.raises(ValueError, match="3001 characters; LinkedIn allows 3000"):
        post.load_text("x" * 3001, None)
    with pytest.raises(ValueError, match="3001 characters"):
        post.load_text("x" * 2999 + "😀", None)  # emoji = 2 UTF-16 units
    assert post.utf16_len("😀") == 2


def test_unreadable_text_file_is_a_usage_error(tmp_path):
    with pytest.raises(ValueError, match="Cannot read --text-file"):
        post.load_text(None, str(tmp_path / "nope.txt"))
    latin1 = tmp_path / "latin1.txt"
    latin1.write_bytes("café".encode("latin-1"))
    with pytest.raises(ValueError, match="Cannot read --text-file"):
        post.load_text(None, str(latin1))


def test_image_validation(tmp_path):
    with pytest.raises(ValueError, match="Image not found"):
        post.load_image(str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="Image not found"):
        post.load_image(str(tmp_path))
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="Image is empty"):
        post.load_image(str(empty))
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"RIFF\0\0\0\0WEBPVP8 ")  # webp named .png
    with pytest.raises(ValueError, match="must be JPEG, PNG, or GIF"):
        post.load_image(str(fake))
    big = tmp_path / "big.png"
    big.write_bytes(png_bytes(pad=post.MAX_IMAGE_BYTES))
    with pytest.raises(ValueError, match=r"over 10485760 bytes \(10 MB\)"):
        post.load_image(str(big))


def test_image_types_and_dimensions(tmp_path):
    def probe(name, data):
        path = tmp_path / name
        path.write_bytes(data)
        img = post.load_image(str(path))
        return img["type"], img["width"], img["height"]

    assert probe("photo.jpeg", jpeg_bytes(640, 480)) == ("image/jpeg", 640, 480)
    assert probe("fill.jpg", jpeg_bytes(800, 600, fill=b"\xff\xff")) == ("image/jpeg", 800, 600)
    assert probe("truncated.jpg", b"\xff\xd8\xff\xe0\0") == ("image/jpeg", None, None)
    assert probe("anim.gif", b"GIF89a" + struct.pack("<HH", 10, 20) + b"\0" * 8) == ("image/gif", 10, 20)
    assert probe("named-wrong.gif", png_bytes(7, 9)) == ("image/png", 7, 9)  # contents decide, not extension
    at_limit = tmp_path / "limit.png"
    at_limit.write_bytes(png_bytes(pad=post.MAX_IMAGE_BYTES - len(png_bytes(pad=0))))
    assert post.load_image(str(at_limit))["bytes"] == post.MAX_IMAGE_BYTES


# ---------------------------------------------------------------- CLI


def fake_session(monkeypatch, page):
    @asynccontextmanager
    async def session_page():
        yield page

    monkeypatch.setattr(post, "_session_page", session_page)
    return page


def cli(*args):
    result = CliRunner().invoke(main, ["post", "create", *args])
    lines = result.output.strip().splitlines()
    data = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else None
    return result, data


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--dry-run", "--confirm"], ["--confirm", "--dry-run"]])
def test_cli_dry_run_is_default_and_wins_over_confirm(monkeypatch, png, flags):
    page = fake_session(monkeypatch, FakePage(ALL_OK))

    result, data = cli("--text", "hi", "--image", str(png), "--format", "json", *flags)

    assert result.exit_code == 0, result.output
    assert "DRY-RUN: nothing was posted" in result.output
    assert data["dryRun"] is True and "urn" not in data
    assert (data["author"], data["expectedAuthor"], data["authorMatches"]) == (AUTHOR, "test-author", True)
    assert page.methods_urls() == [("GET", ME)]


def test_cli_confirm_publishes_and_prints_urn_url(monkeypatch, png):
    page = fake_session(monkeypatch, FakePage(ALL_OK))

    result, data = cli("--text", "hi", "--image", str(png), "--confirm", "--format", "json")

    assert result.exit_code == 0, result.output
    assert data["urn"] == SHARE
    assert data["url"] == f"https://www.linkedin.com/feed/update/{SHARE}/"
    assert (data["posted"], data["author"], data["expectedAuthor"], data["authorMatches"]) == (True, AUTHOR, "test-author", True)
    assert page.methods_urls() == PUBLISH_CALLS


def test_cli_text_format_shows_full_text_and_image(monkeypatch, png):
    fake_session(monkeypatch, FakePage(ALL_OK))

    result, _ = cli("--text", "line one\nline two", "--image", str(png), "--format", "text")

    assert result.exit_code == 0, result.output
    assert "Author: Test Author (test-author)  urn:li:fsd_profile:ACoAAFake  urn:li:member:4242" in result.output
    assert "Expect: test-author  MATCH" in result.output
    assert f"Image:  {png.resolve()}  image/png  {len(png_bytes()):,} bytes  1200x627" in result.output
    assert "Text (17 chars):\nline one\nline two" in result.output


@pytest.mark.parametrize(
    "args, message",
    [
        (["--image", "{png}"], "exactly one of --text or --text-file"),
        (["--text", "a", "--text-file", "{png}", "--image", "{png}"], "exactly one of --text or --text-file"),
        (["--text", " ", "--image", "{png}"], "Post text is empty"),
        (["--text", "a", "--image", "/nope/missing.png"], "Image not found"),
        (["--text", "a"], "Missing option '--image'"),
        (["--text", "a", "--image", "{png}", "--expect-author", " "], "Expected author must be a LinkedIn handle"),
    ],
)
def test_cli_validation_errors_exit_2_without_touching_linkedin(monkeypatch, png, args, message):
    @asynccontextmanager
    async def no_session():
        raise AssertionError("validation errors must not open a session")
        yield

    monkeypatch.setattr(post, "_session_page", no_session)
    result, _ = cli(*[a.format(png=png) for a in args], "--confirm")

    assert result.exit_code == 2
    assert message in result.output


def test_cli_bad_config_exits_2_without_touching_linkedin(monkeypatch, png):
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)
    post.CONFIG_FILES[0].write_text("expect_author = \n", encoding="utf-8")
    monkeypatch.setattr(post, "_session_page", lambda: pytest.fail("bad config must not open a session"))

    result, _ = cli("--text", "hi", "--image", str(png), "--confirm")

    assert result.exit_code == 2
    assert "Cannot read config" in result.output


def test_cli_dry_run_warns_loudly_on_author_mismatch_and_exits_4(monkeypatch, png):
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)  # built-in default: kevnk
    page = fake_session(monkeypatch, FakePage(ALL_OK))

    result, data = cli("--text", "hi", "--image", str(png), "--format", "json")

    assert result.exit_code == 4
    assert (
        "WARNING: AUTHOR MISMATCH. Signed in as Test Author (test-author, urn:li:member:4242), "
        "but the expected author is kevnk. Sign in as kevnk with `limsg login`. --confirm will refuse to post."
    ) in result.output
    assert data["dryRun"] is True
    assert (data["author"], data["expectedAuthor"], data["authorMatches"]) == (AUTHOR, "kevnk", False)
    assert page.methods_urls() == [("GET", ME)]


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_cli_confirm_as_wrong_account_is_refused_with_zero_writes(monkeypatch, png, fmt):
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)
    page = fake_session(monkeypatch, FakePage({**ALL_OK, ("GET", ME): me_as(OTHER, KEVNK)}))

    result, data = cli("--text", "hi", "--image", str(png), "--confirm", "--format", fmt)

    assert result.exit_code == 4
    assert "REFUSED: Signed in as Kevin Kirchner (kevin-kirchner-817709300, urn:li:member:999)" in result.output
    assert "Nothing was uploaded or posted" in result.output
    assert "POSTED" not in result.output
    assert page.methods_urls() == [("GET", ME)]
    if fmt == "json":
        assert (data["dryRun"], data["posted"], data["authorMatches"], data["expectedAuthor"]) == (False, False, False, "kevnk")
        assert data["author"]["memberUrn"] == "urn:li:member:999"
    else:
        assert "Expect: kevnk  MISMATCH" in result.output and "URN:" not in result.output


def test_cli_confirm_as_kevnk_publishes_with_default_expectation(monkeypatch, png):
    monkeypatch.delenv(post.EXPECT_AUTHOR_ENV)
    page = fake_session(monkeypatch, FakePage({**ALL_OK, ("GET", ME): me_as(KEVNK, OTHER)}))

    result, data = cli("--text", "hi", "--image", str(png), "--confirm", "--format", "json")

    assert result.exit_code == 0, result.output
    assert (data["posted"], data["urn"], data["expectedAuthor"], data["authorMatches"]) == (True, SHARE, "kevnk", True)
    assert data["author"] == {
        "name": "Kevin Kirchner",
        "publicIdentifier": "kevnk",
        "profileUrn": "urn:li:fsd_profile:ACoAAAIJK2MBOkk_kX24uyFpDexBxwtR6BXzBqw",
        "memberUrn": "urn:li:member:34155363",
    }
    assert page.methods_urls() == PUBLISH_CALLS


def test_cli_account_change_before_create_exits_4_without_creating(monkeypatch, png):
    page = fake_session(monkeypatch, FakePage({**ALL_OK, ("GET", ME): [ME_OK, me_as(OTHER)]}))

    result, _ = cli("--text", "hi", "--image", str(png), "--confirm")

    assert result.exit_code == 4
    assert "REFUSED: the signed-in account changed" in result.output
    assert ("POST", CREATE) not in page.methods_urls()


def test_cli_not_logged_in_exits_4(monkeypatch, png):
    page = fake_session(monkeypatch, FakePage({("GET", ME): resp(401, {})}))

    result, _ = cli("--text", "hi", "--image", str(png), "--confirm")

    assert result.exit_code == 4
    assert page.methods_urls() == [("GET", ME)]


def test_browser_close_error_never_hides_publish_result(monkeypatch, png):
    page = FakePage(ALL_OK)
    page.url = post.LANDING_URL

    async def goto(*args, **kwargs):
        pass

    class Context:
        pages = [page]

        async def close(self):
            raise RuntimeError("Browser has been closed")

    class Playwright:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    async def open_context(p, headless):
        return Context()

    page.goto = goto
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Playwright())
    monkeypatch.setattr(post, "has_session", lambda: True)
    monkeypatch.setattr(post, "assert_profile_idle", lambda: None)
    monkeypatch.setattr(post, "open_context", open_context)

    out = asyncio.run(post.create_post("hi", post.load_image(str(png)), publish_now=True, expect_author="test-author"))
    assert out["urn"] == SHARE

    page.calls.clear()
    page.responses = {**ALL_OK, ("POST", CREATE): resp(503)}
    with pytest.raises(RuntimeError, match="may be live"):
        asyncio.run(post.create_post("hi", post.load_image(str(png)), publish_now=True, expect_author="test-author"))


def test_busy_profile_stops_before_chrome_launch(monkeypatch, png):
    def busy():
        raise ProfileBusy("Chrome is already using ~/.linkedin-messages-cli/chrome-profile/ (pid 9). Close it and retry.")

    monkeypatch.setattr(post, "has_session", lambda: True)
    monkeypatch.setattr(post, "assert_profile_idle", busy)
    monkeypatch.setattr(post, "open_context", lambda *a, **k: pytest.fail("Chrome must not launch"))
    result, _ = cli("--text", "hi", "--image", str(png))

    assert result.exit_code == 5
    assert "(pid 9)" in result.output


def test_agent_help_documents_post_create():
    result = CliRunner().invoke(main, ["agent-help", "post", "create"])

    assert result.exit_code == 0
    assert "DRY-RUN IS THE DEFAULT" in result.output
    assert "--confirm publishes for real" in result.output
    assert CliRunner().invoke(main, ["agent-help", "nope"]).exit_code == 3


# ---------------------------------------------------------------- the real write JS, local server only


class _Recorder(BaseHTTPRequestHandler):
    hits: list[tuple[str, str, dict, bytes]] = []

    def log_message(self, *args):
        pass

    def _record(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        _Recorder.hits.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html></html>")

    def do_PUT(self):
        self._record()
        self.send_response(201)
        self.end_headers()

    def do_POST(self):
        self._record()
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("location", "/elsewhere")
            self.end_headers()
            return
        payload = b'{"data":{"status":{"urn":"urn:li:share:5"}}}'
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.send_header("x-restli-id", "urn:li:share:5")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_write_js_sends_exact_bytes_headers_and_never_follows_redirects():
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    _Recorder.hits = []
    blob = bytes(range(256)) * 300  # every byte value, 76.8 KB

    async def go():
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:  # no system Chrome on this machine
                pytest.skip(f"Chrome unavailable: {exc}")
            try:
                context = await browser.new_context()
                await context.add_cookies([{"name": "JSESSIONID", "value": '"ajax:test"', "url": base}])
                page = await context.new_page()
                await page.goto(base + "/")
                put = await post._write(page, base + "/upload", method="PUT", data=blob,
                                        headers={"content-type": "image/png", "media-type-family": "STILLIMAGE"})
                created = await post._write(page, base + "/create", json_body={"a": "é😀"})
                redirected = await post._write(page, base + "/redirect", json_body={"b": 1})
                return put, created, redirected
            finally:
                await browser.close()

    try:
        put, created, redirected = asyncio.run(go())
    finally:
        server.shutdown()

    assert put == (201, None, None)
    assert created == (201, {"data": {"status": {"urn": "urn:li:share:5"}}}, "urn:li:share:5")
    assert redirected == (0, None, None)
    writes = [(m, path, h, b) for m, path, h, b in _Recorder.hits if m != "GET"]
    assert [(m, path) for m, path, _, _ in writes] == [("PUT", "/upload"), ("POST", "/create"), ("POST", "/redirect")]
    (_, _, put_headers, put_body), (_, _, post_headers, post_body), _ = writes
    assert put_body == blob
    assert (put_headers["csrf-token"], put_headers["content-type"], put_headers["media-type-family"]) == ("ajax:test", "image/png", "STILLIMAGE")
    assert json.loads(post_body) == {"a": "é😀"}
    assert (post_headers["csrf-token"], post_headers["content-type"]) == ("ajax:test", "application/json")
    assert not any(path == "/elsewhere" for _, path, _, _ in _Recorder.hits)
