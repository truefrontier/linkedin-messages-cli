"""Offline tests for assert_profile_idle. Never launches Chrome. Run: python -m pytest tests"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from limsg_cli import activity  # noqa: E402
from limsg_cli.activity import ProfileBusy, assert_profile_idle  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST = "test-host.local"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "chrome profile"  # space on purpose: argv paths are space-joined
    path.mkdir()
    monkeypatch.setattr(activity, "PROFILE", path)
    monkeypatch.setattr(activity.socket, "gethostname", lambda: HOST)
    return path


def fake_ps(monkeypatch, procs: dict[int, str]) -> None:
    """Fake ps over a {pid: command} table, printing argv newlines raw like macOS ps."""

    def run(args, **kwargs):
        if args == ["ps", "-axww", "-o", "pid=,command="]:
            rows = procs
        else:
            assert args[:3] == ["ps", "-ww", "-p"] and args[4:] == ["-o", "pid=,command="]
            pid = int(args[3])
            rows = {pid: procs[pid]} if pid in procs else {}
        out = "".join(f"{pid:5d} {command}\n" for pid, command in rows.items())
        return subprocess.CompletedProcess(args, 0 if rows else 1, stdout=out, stderr="")

    monkeypatch.setattr(activity.subprocess, "run", run)


def fake_kill(monkeypatch, exc: type[OSError] | None) -> list[tuple[int, int]]:
    calls = []

    def kill(pid, sig):
        calls.append((pid, sig))
        if exc:
            raise exc()

    monkeypatch.setattr(activity.os, "kill", kill)
    return calls


def singletons(profile: Path, target: str) -> None:
    os.symlink(target, profile / "SingletonLock")
    os.symlink("/tmp/.com.google.Chrome.x/SingletonSocket", profile / "SingletonSocket")
    os.symlink("1234567890", profile / "SingletonCookie")


def test_unrelated_process_mentioning_chrome_and_cli_is_not_busy(profile, monkeypatch):
    fake_ps(
        monkeypatch,
        {
            34597: "/Users/kk/.local/bin/claude --model x -- fix the false busy in linkedin-messages-cli.\n"
            f"ps shows:\n34597 {CHROME} --user-data-dir={profile}\n777 {CHROME} --user-data-dir={profile}\n",
            901: f"codex exec use Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={profile}",
            902: f"{CHROME} --user-data-dir=/Users/kk/Library/Application Support/Google/Chrome",
            os.getpid(): f"{CHROME} --user-data-dir={profile}",
        },
    )
    fake_kill(monkeypatch, None)

    assert assert_profile_idle() is None


@pytest.mark.parametrize(
    "command",
    [
        f"{CHROME} --disable-breakpad --user-data-dir={{profile}} --remote-debugging-pipe about:blank",
        "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/1/Helpers/"
        "Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer "
        "--user-data-dir={profile}/",
        "/opt/google/chrome/chrome --user-data-dir={profile}",
        "/Users/kk/Library/Caches/ms-playwright/chromium-1187/chrome-mac-arm64/Google Chrome for Testing.app/"
        "Contents/MacOS/Google Chrome for Testing --user-data-dir={profile} https://www.linkedin.com/feed/",
    ],
)
def test_real_chrome_with_matching_user_data_dir_is_busy(profile, monkeypatch, command):
    fake_ps(monkeypatch, {4242: command.format(profile=profile)})

    with pytest.raises(ProfileBusy) as exc:
        assert_profile_idle()

    assert str(exc.value) == (
        "Chrome is already using ~/.linkedin-messages-cli/chrome-profile/ (pid 4242). Close it and retry."
    )


def test_stale_singleton_lock_with_dead_pid_is_cleared(profile, monkeypatch):
    fake_ps(monkeypatch, {})
    calls = fake_kill(monkeypatch, ProcessLookupError)
    singletons(profile, f"{HOST}-77777")

    assert assert_profile_idle() is None

    assert calls == [(77777, 0)]
    assert sorted(p.name for p in profile.iterdir()) == []


@pytest.mark.parametrize("exc", [None, PermissionError])
def test_live_singleton_lock_is_busy(profile, monkeypatch, exc):
    fake_ps(monkeypatch, {})
    fake_kill(monkeypatch, exc)
    singletons(profile, f"{HOST}-5150")

    with pytest.raises(ProfileBusy, match=r"\(pid 5150\)"):
        assert_profile_idle()

    assert os.path.lexists(profile / "SingletonLock")


def test_singleton_lock_from_other_host_is_left_alone(profile, monkeypatch):
    fake_ps(monkeypatch, {})
    calls = fake_kill(monkeypatch, ProcessLookupError)
    singletons(profile, "other-host.local-77777")

    assert assert_profile_idle() is None

    assert calls == []
    assert os.path.lexists(profile / "SingletonLock")
