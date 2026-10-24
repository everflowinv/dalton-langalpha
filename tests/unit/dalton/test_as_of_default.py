"""The US coverage workflows' ``as_of`` default: the last completed US trading
day on the exchange calendar (America/New_York), independent of the caller's
timezone — and the resume path's adoption of the previous run's recorded date.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from ptc_agent.agent.backends.sandbox import SandboxBackend
from src.dalton.workflows.as_of import (
    adopt_resumed_as_of,
    apply_as_of_default,
    last_completed_trading_day,
    resolve_launch_as_of,
)

BEIJING = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")

# 2026-10-23 is a Friday, 10-24/25 the weekend, 10-26 a Monday — plain days,
# no holiday, so the published calendar and its weekday fallback agree.
FRIDAY = "2026-10-23"


def test_saturday_beijing_morning_anchors_friday() -> None:
    """Saturday 10:00 Beijing is Friday 22:00 ET: Friday's close has passed
    and Saturday has no session — the anchor is Friday, not the caller's
    Saturday."""
    now = datetime(2026, 10, 24, 10, 0, tzinfo=BEIJING)
    assert last_completed_trading_day("US", now) == FRIDAY
    params = apply_as_of_default(
        "initial-coverage", {"ticker": "GOOGL", "workspace_dir": "."}, now=now
    )
    assert params["as_of"] == FRIDAY


def test_monday_before_the_us_open_anchors_previous_friday() -> None:
    """Monday 08:00 ET is before the 09:30 open: Monday's session has not
    closed, so the anchor is the previous Friday."""
    now = datetime(2026, 10, 26, 8, 0, tzinfo=ET)
    assert last_completed_trading_day("US", now) == FRIDAY


def test_monday_after_the_close_anchors_monday() -> None:
    now = datetime(2026, 10, 26, 16, 1, tzinfo=ET)
    assert last_completed_trading_day("US", now) == "2026-10-26"


def test_thanksgiving_midday_anchors_wednesday() -> None:
    """2026-11-26 (Thursday) is an NYSE holiday: midday Thursday the anchor is
    Wednesday, per the exchange calendar rather than the weekday table."""
    now = datetime(2026, 11, 26, 12, 0, tzinfo=ET)
    assert last_completed_trading_day("US", now) == "2026-11-25"


def test_latest_and_missing_take_the_default_others_stay() -> None:
    now = datetime(2026, 10, 24, 10, 0, tzinfo=BEIJING)
    assert apply_as_of_default("initial-coverage", {"as_of": "latest"}, now=now)["as_of"] == FRIDAY
    assert apply_as_of_default("bullbear-doc", {}, now=now)["as_of"] == FRIDAY
    # An explicit date is never overridden, a non-US market is none of the US
    # calendar's business, and workflows outside the set keep their own
    # conventions (topic-research's deliberate "latest").
    assert (
        apply_as_of_default("full-coverage", {"as_of": "2026-10-20"}, now=now)["as_of"]
        == "2026-10-20"
    )
    assert "as_of" not in apply_as_of_default(
        "initial-coverage", {"market": "CN"}, now=now
    )
    assert "as_of" not in apply_as_of_default("topic-research", {}, now=now)
    assert apply_as_of_default("initial-coverage", None, now=now) is None


@pytest.mark.asyncio
async def test_resumed_run_adopts_the_previous_runs_as_of() -> None:
    """A resume must not re-fill the default (a later day would shift every
    date-keyed prompt hash and void the replay): the caller did not restate
    as_of, so the source run's recorded args.json decides."""
    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread_text(path: str) -> str | None:
        assert path == ".agents/threads/t1/workflows/old-run/args.json"
        return json.dumps({"ticker": "GOOGL", "as_of": FRIDAY})

    backend.aread_text.side_effect = _aread_text

    params = await adopt_resumed_as_of(backend, {"ticker": "GOOGL"}, ".agents/threads/t1/workflows/old-run")
    assert params["as_of"] == FRIDAY
    # A restated date wins; an unreadable record leaves the params untouched.
    params = await adopt_resumed_as_of(
        backend, {"ticker": "GOOGL", "as_of": "2026-10-20"}, ".agents/threads/t1/workflows/old-run"
    )
    assert params["as_of"] == "2026-10-20"

    async def _dead(path: str) -> str | None:
        raise RuntimeError("sandbox unreachable")

    backend.aread_text.side_effect = _dead
    params = await adopt_resumed_as_of(backend, {"ticker": "GOOGL"}, ".agents/threads/t1/workflows/old-run")
    assert "as_of" not in params


@pytest.mark.asyncio
async def test_resume_falls_back_to_the_default_when_args_unreadable() -> None:
    """A resume whose source args.json cannot be read (and no restated date)
    still launches on a concrete trading day, never an undefined one."""
    backend = create_autospec(SandboxBackend, instance=True)

    async def _dead(path: str) -> str | None:
        raise RuntimeError("sandbox unreachable")

    backend.aread_text.side_effect = _dead

    params = await resolve_launch_as_of(
        backend, "initial-coverage", {"ticker": "GOOGL"}, ".agents/threads/t1/workflows/old"
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", params["as_of"])
    assert params["as_of"] != "latest"


@pytest.mark.asyncio
async def test_resolve_launch_as_of_adopts_then_leaves_restated_dates_alone() -> None:
    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread_text(path: str) -> str | None:
        return json.dumps({"as_of": FRIDAY})

    backend.aread_text.side_effect = _aread_text

    # Adopted date wins over the default; a restated date wins over adoption.
    params = await resolve_launch_as_of(
        backend, "initial-coverage", {"ticker": "GOOGL"}, ".agents/threads/t1/workflows/old"
    )
    assert params["as_of"] == FRIDAY
    params = await resolve_launch_as_of(
        backend,
        "initial-coverage",
        {"ticker": "GOOGL", "as_of": "2026-10-20"},
        ".agents/threads/t1/workflows/old",
    )
    assert params["as_of"] == "2026-10-20"
    # Fresh run, non-US workflow: nothing filled.
    params = await resolve_launch_as_of(backend, "topic-research", {"topic": "x"}, None)
    assert "as_of" not in params


def test_launch_injection_is_wired_into_run_workflow() -> None:
    """The seam sits in the launch path under its hook marker, and ahead of
    the args.json snapshot — the run record must carry the effective params."""
    from pathlib import Path

    import ptc_agent.agent.middleware.background_subagent.workflow.tool as tool_module

    source = Path(tool_module.__file__).read_text(encoding="utf-8")
    assert "DALTON-HOOK(workflow-as-of-default)" in source
    assert source.index("resolve_launch_as_of(backend, meta.name, params, journal_source)") < (
        source.index('"args.json": json.dumps(params')
    )


def test_last_completed_day_handles_unknown_market() -> None:
    assert last_completed_trading_day("XX", datetime(2026, 10, 23, tzinfo=UTC)) is None
