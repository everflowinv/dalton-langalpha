"""`dalton_publish_earnings_calendar` (server-side mirror publish) and the
`calendar_state.py publish` CLI's no-fallback behavior.

The sandbox cannot reach the mirror endpoint (the 10-31 incident: the CLI
dialed 127.0.0.1:8000 from inside the sandbox and got Connection refused), so
publishing moved into a server-side tool whose docstring is prompt surface.
These tests pin the tool's contract (validated ledger in, canonical hash out,
failures named instead of raised) and that the CLI refuses to dial anything
without an explicitly configured backend URL."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.backends.sandbox import SandboxBackend
from src.dalton.coverage.models import (
    canonical_ledger_sha256,
    validate_earnings_calendar_ledger,
)
from src.dalton.coverage.service import (
    publish_user_earnings_calendar_mirror as _REAL_PUBLISH,
)
from src.dalton.tools import director_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
CALENDAR_STATE_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-earnings-calendar"
    / "scripts"
    / "calendar_state.py"
)

USER = "user-1"


def _ledger() -> dict[str, Any]:
    return {
        "updated": "2026-10-23",
        "entries": {
            "META": {
                "name": "Meta Platforms",
                "status": "confirmed",
                "date": "2026-10-28",
                "date_status": "confirmed",
                "quarter": "FY26Q3",
            },
            "GOOGL": {
                "name": "Alphabet",
                "status": "pending",
                "tentative_date": "2026-11-05",
            },
        },
        "reported": [{"ticker": "MSFT", "quarter": "FY26Q2", "date": "2026-07-29"}],
    }


def _ledger_text() -> str:
    return json.dumps(_ledger(), ensure_ascii=False)


def _make_backend(text: str | None, *, read_error: bool = False) -> Any:
    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread_text(path: str) -> str | None:
        if read_error:
            raise RuntimeError("sandbox unreachable")
        assert path == "dalton/earnings-calendar.json", path
        return text

    backend.aread_text.side_effect = _aread_text
    return backend


def _config() -> dict[str, Any]:
    return {"configurable": {"user_id": USER}}


def _build(backend: Any | None):
    tools = {
        t.name: t
        for t in director_tools.build_director_tools(
            role="chief_of_staff", filesystem_backend=backend
        )
    }
    return tools["dalton_publish_earnings_calendar"]


@pytest.mark.asyncio
async def test_director_publish_calendar_tool_publishes_validated_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid ledger reaches the service in its VALIDATED form with the raw
    byte count, and the returned hash is the canonical one check_done compares."""
    publish = create_autospec(_REAL_PUBLISH)

    async def _publish(user_id: str, ledger: Any, *, raw_bytes_len: int | None = None):
        assert user_id == USER
        assert raw_bytes_len == len(_ledger_text().encode("utf-8"))
        return {
            "ok": True,
            "user_id": user_id,
            "entries_count": 2,
            "reported_count": 1,
            "published_at": "2026-11-02T00:00:00+00:00",
        }

    publish.side_effect = _publish
    monkeypatch.setattr(director_tools, "publish_user_earnings_calendar_mirror", publish)

    tool = _build(_make_backend(_ledger_text()))
    result = json.loads(await tool.ainvoke({}, config=_config()))
    assert result["ok"] is True, result
    assert result["entries_count"] == 2
    assert result["reported_count"] == 1
    assert result["published_at"] == "2026-11-02T00:00:00+00:00"
    expected = canonical_ledger_sha256(validate_earnings_calendar_ledger(_ledger()))
    assert result["ledger_sha256"] == expected
    # The stored payload is the validated ledger, not the raw JSON.
    stored_ledger = publish.call_args[0][1]
    assert stored_ledger == validate_earnings_calendar_ledger(_ledger())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "read_error", "expected_error"),
    [
        (None, False, "ledger_not_found"),
        ("{not json", False, "invalid_json"),
        ('{"updated": "2026-10-23"}', False, "invalid_ledger"),
        ('{"entries": {"META": {"status": "confirmed"}}}', False, "invalid_ledger"),
        ('{"updated": "   ", "entries": {"META": {"status": "confirmed"}}}', False, "invalid_ledger"),
        (None, True, "read_failed"),
    ],
)
async def test_director_publish_calendar_tool_names_each_failure(
    monkeypatch: pytest.MonkeyPatch,
    text: str | None,
    read_error: bool,
    expected_error: str,
) -> None:
    """Every failure comes back as {ok: false, error, message} — nothing is
    raised to the model, and the service is never reached. A ledger without a
    usable `updated` date is invalid: validation must not fill the date in
    from the clock (a same-file hash would then drift across midnight)."""
    publish = create_autospec(_REAL_PUBLISH)
    publish.side_effect = AssertionError("publish must not be called")
    monkeypatch.setattr(director_tools, "publish_user_earnings_calendar_mirror", publish)

    tool = _build(_make_backend(text, read_error=read_error))
    result = json.loads(await tool.ainvoke({}, config=_config()))
    assert result["ok"] is False
    assert result["error"] == expected_error
    assert result["message"]
    publish.assert_not_called()
    if expected_error == "invalid_ledger" and "entries" in (text or ""):
        assert "台账缺少 updated 日期" in result["message"]


@pytest.mark.parametrize(
    "bad_ledger",
    [
        {"entries": {"META": {"status": "confirmed"}}, "reported": []},
        {"updated": "", "entries": {"META": {"status": "confirmed"}}, "reported": []},
        {"updated": "   ", "entries": {}, "reported": []},
    ],
)
def test_calendar_ledger_validation_requires_updated_date(bad_ledger: dict) -> None:
    from src.dalton.coverage.models import validate_earnings_calendar_ledger

    with pytest.raises(ValueError, match="台账缺少 updated 日期"):
        validate_earnings_calendar_ledger(bad_ledger)


def test_calendar_ledger_validation_hash_is_stable_across_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same file must validate (and hash) identically on both sides of a
    date change: validation reads no clock, so nothing about the result can
    drift. This is the cross-midnight false-mismatch regression — the mirror
    published at 23:59 and the check at 00:01 compare the same hash."""
    from datetime import date as _date

    from src.dalton.coverage import models as coverage_models
    from src.dalton.coverage.models import (
        canonical_ledger_sha256,
        validate_earnings_calendar_ledger,
    )

    class _FrozenDate(_date):
        today_iso = "2026-10-31"

        @classmethod
        def today(cls) -> _date:
            return _date.fromisoformat(cls.today_iso)

    monkeypatch.setattr(coverage_models, "date", _FrozenDate)
    ledger = _ledger()

    before = validate_earnings_calendar_ledger(ledger)
    sha_before = canonical_ledger_sha256(before)

    _FrozenDate.today_iso = "2026-11-01"  # the clock crosses midnight
    after = validate_earnings_calendar_ledger(ledger)
    assert after == before
    assert canonical_ledger_sha256(after) == sha_before


# --- _default_state_path: the director's cwd IS the Home workspace root ---


def _load_calendar_state():
    spec = importlib.util.spec_from_file_location(
        "dalton_calendar_state_under_test", CALENDAR_STATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calendar_state_default_path_from_home_cwd_writes_dalton_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """From a cwd named Home the default ledger is dalton/earnings-calendar.json
    — the old Home-first guess made `mark` create Home/Home/dalton/… on first
    write, which the publish tool and check_done (both reading dalton/…)
    would never see."""
    monkeypatch.delenv("DALTON_EARNINGS_CALENDAR_PATH", raising=False)
    home_cwd = tmp_path / "Home"
    home_cwd.mkdir()
    monkeypatch.chdir(home_cwd)
    calendar_state = _load_calendar_state()

    assert calendar_state._default_state_path() == Path("dalton/earnings-calendar.json")

    # The write path a `mark` would take: the ledger lands under dalton/,
    # never under a nested Home/Home/.
    ledger = calendar_state.load_ledger(calendar_state._default_state_path(), create=True)
    assert ledger["updated"]
    assert (home_cwd / "dalton" / "earnings-calendar.json").exists()
    assert not (home_cwd / "Home").exists()


def test_calendar_state_from_home_moves_a_nested_legacy_ledger_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Home/Home/dalton/… ledger an older run created is moved to the path the
    publish tool and check_done read, instead of being written to forever."""
    monkeypatch.delenv("DALTON_EARNINGS_CALENDAR_PATH", raising=False)
    home_cwd = tmp_path / "Home"
    (home_cwd / "Home" / "dalton").mkdir(parents=True)
    nested = home_cwd / "Home" / "dalton" / "earnings-calendar.json"
    nested.write_text(_ledger_text(), encoding="utf-8")
    monkeypatch.chdir(home_cwd)
    calendar_state = _load_calendar_state()

    assert calendar_state._default_state_path() == Path("dalton/earnings-calendar.json")
    moved = home_cwd / "dalton" / "earnings-calendar.json"
    assert moved.read_text(encoding="utf-8") == _ledger_text()
    assert not nested.exists()


def test_calendar_state_default_path_prefers_existing_home_subdir_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cwd that CONTAINS a Home/ subdirectory keeps the legacy
    Home/dalton/… layout; without one, the local dalton/… path is the default."""
    monkeypatch.delenv("DALTON_EARNINGS_CALENDAR_PATH", raising=False)
    calendar_state = _load_calendar_state()

    legacy = tmp_path / "workdir"
    (legacy / "Home" / "dalton").mkdir(parents=True)
    (legacy / "Home" / "dalton" / "earnings-calendar.json").write_text(
        _ledger_text(), encoding="utf-8"
    )
    monkeypatch.chdir(legacy)
    assert (
        calendar_state._default_state_path()
        == Path("Home/dalton/earnings-calendar.json")
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert calendar_state._default_state_path() == Path("dalton/earnings-calendar.json")


def test_calendar_state_default_path_env_var_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "elsewhere" / "ledger.json"
    monkeypatch.setenv("DALTON_EARNINGS_CALENDAR_PATH", str(env_path))
    calendar_state = _load_calendar_state()
    assert calendar_state._default_state_path() == env_path


@pytest.mark.asyncio
async def test_director_publish_calendar_tool_requires_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = create_autospec(_REAL_PUBLISH)
    publish.side_effect = AssertionError("publish must not be called")
    monkeypatch.setattr(director_tools, "publish_user_earnings_calendar_mirror", publish)

    tool = _build(None)
    result = json.loads(await tool.ainvoke({}, config=_config()))
    assert result["ok"] is False
    assert result["error"] == "no_backend"
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_director_publish_calendar_tool_chief_of_staff_only() -> None:
    cos_names = {
        t.name
        for t in director_tools.build_director_tools(role="chief_of_staff")
    }
    analyst_names = {
        t.name
        for t in director_tools.build_director_tools(role="analyst")
    }
    assert "dalton_publish_earnings_calendar" in cos_names
    assert "dalton_publish_earnings_calendar" not in analyst_names


@pytest.mark.asyncio
async def test_director_publish_calendar_tool_wraps_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service-layer error is reported, not raised (the ledger is valid; the
    store itself failed)."""
    publish = create_autospec(_REAL_PUBLISH)
    publish.side_effect = RuntimeError("mirror store down")
    monkeypatch.setattr(director_tools, "publish_user_earnings_calendar_mirror", publish)

    tool = _build(_make_backend(_ledger_text()))
    result = json.loads(await tool.ainvoke({}, config=_config()))
    assert result["ok"] is False
    assert result["error"] == "publish_failed"
    assert "mirror store down" in result["message"]


# --- calendar_state.py publish: no 127.0.0.1 fallback, no dialing without a URL ---


def _publish_args(tmp_path: Path, api_url: str | None) -> argparse.Namespace:
    ledger = tmp_path / "earnings-calendar.json"
    ledger.write_text(_ledger_text(), encoding="utf-8")
    return argparse.Namespace(
        state=str(ledger),
        api_url=api_url,
        token=None,
        service_token=None,
        user_id=None,
    )


def test_calendar_state_publish_without_api_url_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --api-url and no env URL: exit non-zero with no_api_url, and the
    network layer is never touched (the old 127.0.0.1:8000 fallback is gone)."""
    calendar_state = _load_calendar_state()
    for env in ("DALTON_API_URL", "LANGALPHA_API_URL", "API_BASE_URL"):
        monkeypatch.delenv(env, raising=False)

    def _no_dial(*_args: Any, **_kwargs: Any):
        raise AssertionError("publish must not dial anything without a backend URL")

    monkeypatch.setattr(
        "urllib.request.urlopen", _no_dial
    )

    rc = calendar_state.cmd_publish(_publish_args(tmp_path, api_url=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False
    assert out["error"] == "no_api_url"
    assert "dalton_publish_earnings_calendar" in out["message"]


def test_calendar_state_publish_with_api_url_dials_the_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With an explicit URL the CLI still works (host-side use) and dials
    exactly <base>/api/v1/dalton/earnings/calendar/mirror."""

    class _FakeResponse:
        def read(self) -> bytes:
            return b'{"ok": true, "entries_count": 2}'

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

    dialed: dict[str, Any] = {}

    def _urlopen(req: Any, **_kwargs: Any) -> _FakeResponse:
        dialed["url"] = req.full_url
        dialed["method"] = req.get_method()
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    calendar_state = _load_calendar_state()

    rc = calendar_state.cmd_publish(
        _publish_args(tmp_path, api_url="https://backend.example/")
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["entries_count"] == 2
    assert dialed["url"] == "https://backend.example/api/v1/dalton/earnings/calendar/mirror"
    assert dialed["method"] == "PUT"
