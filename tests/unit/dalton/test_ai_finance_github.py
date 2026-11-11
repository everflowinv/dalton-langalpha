"""Offline checks for GitHub momentum and the trending collection boundary."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from scripts.dalton.ai_finance import ai_finance_github_momentum as momentum
from scripts.dalton.ai_finance import ai_finance_github_trending as trending


FIXED = datetime(2026, 10, 11, 12, 1, tzinfo=timezone.utc)
REPO = "acme/widget"


def _weeks(through: date) -> list[dict[str, object]]:
    start = through - timedelta(days=27)
    weeks = []
    for index in range(5):
        week_start = start + timedelta(days=7 * index)
        counts = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            counts.append(1000 if day <= through else 0)
        stamp = datetime(week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc)
        weeks.append({"week": int(stamp.timestamp()), "days": counts, "total": sum(counts)})
    return weeks


def _repo_payload() -> dict[str, object]:
    return {
        "full_name": REPO,
        "description": "demo",
        "stargazers_count": 20000,
        "forks_count": 1,
        "open_issues_count": 0,
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "pushed_at": "2026-10-01T00:00:00Z",
        "homepage": "",
        "license": None,
        "topics": [],
        "archived": False,
    }


def test_complete_through_uses_the_twelve_hour_buffer() -> None:
    early = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)
    assert momentum.complete_through(early) == date(2026, 10, 9)
    assert momentum.complete_through(FIXED) == date(2026, 10, 10)


def test_tiny_gain_is_not_meaningful() -> None:
    curve = {
        "daily_gain": 30,
        "gain_7d": 60,
        "phase": "watch",
        "daily_velocity_3d": 30.0,
        "velocity_ratio": 1.0,
    }
    assert momentum.significance(curve, 20000, False)["meaningful"] is False


def test_two_completed_days_cool_and_same_day_rerun_stays_unconfirmed() -> None:
    through = date(2026, 10, 20)
    days = {through - timedelta(days=offset): 1000 for offset in range(40)}
    for offset in range(4):
        days[through - timedelta(days=offset)] = 10
    cooled = momentum.evaluate_history(days, through, None)
    assert cooled["phase"] == "cooling"
    assert cooled["momentum_active"] is False

    slowing = {through - timedelta(days=offset): 1000 for offset in range(40)}
    slowing[through] = 100
    first = momentum.evaluate_history(slowing, through, None)
    assert first["phase"] == "slowing_unconfirmed"
    assert first["momentum_active"] is True
    second = momentum.evaluate_history(slowing, through, first)
    assert second["phase"] == "slowing_unconfirmed"
    assert second["complete_through"] == through.isoformat()


def test_recent_tokens_follow_the_injected_clock() -> None:
    captured = FIXED - timedelta(hours=30)
    state = {
        "snapshots": [
            {
                "captured_at": captured.isoformat().replace("+00:00", "Z"),
                "repos": {REPO: {"name_tokens": ["widget"], "mention_tokens": ["widget"]}},
            }
        ]
    }
    assert REPO in trending.recent_repo_tokens(state, 48, now=FIXED)
    assert REPO not in trending.recent_repo_tokens(state, 48, now=FIXED + timedelta(hours=100))


def test_select_candidates_keeps_continuing_when_new_limit_is_zero() -> None:
    def item(name: str, reported: bool, score: float) -> dict[str, object]:
        return {
            "full_name": name,
            "report_eligible": True,
            "previously_reported": reported,
            "significance": {"breakout_score": score},
            "momentum": {"daily_velocity_3d": 1.0, "gain_7d": 10},
        }

    selected = trending.select_candidates(
        [item("acme/continuing", True, 1.0), item("acme/new", False, 9.0)],
        0,
    )
    assert [row["full_name"] for row in selected] == ["acme/continuing"]


def test_github_json_keeps_gh_credential_and_drops_other_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_testtokenvalue")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-value")
    monkeypatch.setenv("PATH", "/usr/bin")
    completed = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout='{"ok": true}', stderr="")

    def run(*_args: object, input: object = None, capture_output: bool = False, timeout: object = None, check: bool = False, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del input, capture_output, check
        assert timeout == 40
        assert kwargs["env"]["GH_TOKEN"] == "ghp_testtokenvalue"  # type: ignore[index]
        assert "GEMINI_API_KEY" not in kwargs["env"]  # type: ignore[operator]
        return completed

    fake_run = create_autospec(subprocess.run, side_effect=run)
    monkeypatch.setattr(momentum.subprocess, "run", fake_run)
    monkeypatch.setattr(momentum.shutil, "which", lambda _command: "/usr/bin/gh")

    assert momentum.github_json("rate_limit") == {"ok": True}
    fake_run.assert_called_once()


def test_github_json_redacts_token_and_timeout_omits_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ghp_testtokenvalue"
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setenv("PATH", "/usr/bin")
    real_run = subprocess.run
    denied = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr=f"denied {token}")

    def run(*_args: object, input: object = None, capture_output: bool = False, timeout: object = None, check: bool = False, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        del input, capture_output, timeout, check
        return denied

    fake_run = create_autospec(real_run, side_effect=run)
    monkeypatch.setattr(momentum.subprocess, "run", fake_run)
    monkeypatch.setattr(momentum.shutil, "which", lambda _command: "/usr/bin/gh")
    with pytest.raises(RuntimeError, match="redacted") as denied_error:
        momentum.github_json("rate_limit")
    assert token not in str(denied_error.value)

    def explode(*_args: object, input: object = None, capture_output: bool = False, timeout: object = None, check: bool = False, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        del input, capture_output, timeout, check
        raise subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=40)

    fake_run.side_effect = explode
    with pytest.raises(RuntimeError) as timeout_error:
        momentum.github_json("rate_limit")
    assert str(timeout_error.value) == "GitHub API timed out for rate_limit"
    assert token not in str(timeout_error.value)


def test_http_fallback_when_gh_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    captured: dict[str, object] = {}

    def opener(url: object, data: object = None, timeout: object = None, *, cafile: object = None, capath: object = None, cadefault: bool = False, context: object = None) -> io.BytesIO:
        del data, cafile, capath, cadefault, context
        captured["url"] = url
        captured["timeout"] = timeout
        return io.BytesIO(b'{"ok": true}')

    fake_open = create_autospec(momentum.urlopen, side_effect=opener)
    fake_run = create_autospec(subprocess.run)
    monkeypatch.setattr(momentum, "urlopen", fake_open)
    monkeypatch.setattr(momentum.subprocess, "run", fake_run)
    monkeypatch.setattr(momentum.shutil, "which", lambda _command: None)

    assert momentum.github_json("rate_limit") == {"ok": True}
    request = captured["url"]
    assert captured["timeout"] == 30
    assert request.get_header("Authorization") is None  # type: ignore[attr-defined]
    fake_run.assert_not_called()


def test_resolve_explicit_path_rejects_host_assistant_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".feishu-daily-ai-finance").mkdir(parents=True)
    (home / ".openclaw").mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ValueError, match="host assistant state"):
        momentum.resolve_explicit_path(Path("~/.feishu-daily-ai-finance/seen.json"))
    with pytest.raises(ValueError, match="host assistant state"):
        momentum.resolve_explicit_path(Path("~/.openclaw/secrets.env"))


def test_collect_updates_watch_pool_and_leaves_seen_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "github_trending_history.json"
    seen = tmp_path / "github_seen.json"
    seen.write_bytes(b'{"repos": {}}\n')
    seen_bytes = seen.read_bytes()
    calls: list[str] = []

    def fake_discover() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        return (
            {
                REPO: {
                    "full_name": REPO,
                    "repository_description": "demo",
                    "discovery": {"daily": {"rank": 1, "stars_gained": 1000}},
                    "tags": [],
                }
            },
            {"sources": {"daily": 1}, "errors": {}},
        )

    def fake_github_json(endpoint: str) -> dict[str, object] | list[dict[str, object]]:
        calls.append(endpoint)
        if "stargazers/history" in endpoint:
            return _weeks(momentum.complete_through(FIXED))
        return _repo_payload()

    monkeypatch.setattr(trending, "discover", fake_discover)
    monkeypatch.setattr(trending, "github_json", fake_github_json)

    payload = trending.collect_trending(state_file=state, seen_file=seen, now=FIXED)
    assert payload["complete_through"] == "2026-10-10"
    assert payload["generated_at"] == "2026-10-11T12:01:00Z"
    assert [item["full_name"] for item in payload["candidates"]] == [REPO]
    assert calls
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["tracked_repos"][REPO]["momentum_active"] is True
    assert seen.read_bytes() == seen_bytes


def test_github_failure_does_not_cool_pool_or_touch_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "github_trending_history.json"
    seen = tmp_path / "github_seen.json"
    state.write_text(
        json.dumps({"snapshots": [], "tracked_repos": {REPO: {"momentum_active": True, "phase": "sustained"}}}),
        encoding="utf-8",
    )
    seen.write_bytes(b'{"repos": {"acme/widget": {"last_ts": 1}}}\n')
    state_bytes = state.read_bytes()
    seen_bytes = seen.read_bytes()

    def fake_discover() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        return ({REPO: {"full_name": REPO, "discovery": {}, "tags": []}}, {"sources": {}, "errors": {}})

    def fake_github_json(endpoint: str) -> dict[str, object]:
        raise RuntimeError(f"down {endpoint}")

    monkeypatch.setattr(trending, "discover", fake_discover)
    monkeypatch.setattr(trending, "github_json", fake_github_json)
    with pytest.raises(RuntimeError, match="All official GitHub history checks failed"):
        trending.collect_trending(state_file=state, seen_file=seen, now=FIXED)
    assert state.read_bytes() == state_bytes
    assert seen.read_bytes() == seen_bytes
