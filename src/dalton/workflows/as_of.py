"""The US coverage workflows' ``as_of`` default: the last completed US trading day.

The SKILL text tells the agent "默认今天", but "today" in the caller's
timezone is the wrong anchor for US research — a Saturday-morning Beijing
call would stamp Saturday, and any local "today" drifts with the viewer. The
default lives here, on the exchange calendar, in America/New_York (the
runtime clock's prebuilt ``XNYS`` calendar; no second calendar
implementation), and the launch path applies it before the script ever sees
``args`` — so the effective ``as_of`` is what the run's ``args.json`` records
and what a resume replays against.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ptc_agent.agent.middleware.runtime_context.clock import get_market_calendar

logger = logging.getLogger(__name__)

# The workflows whose research anchor is a US trading day: their contracts key
# every artifact path off ``as_of``. topic-research keeps its deliberate
# date-free "latest" mode; weekly's ``end`` is the screen's own input.
US_AS_OF_WORKFLOWS = frozenset({"initial-coverage", "full-coverage", "bullbear-doc"})


def last_completed_trading_day(market: str, now: datetime) -> str | None:
    """ISO date of the newest session whose regular close is at or before ``now``.

    Market-local day and close (holidays, early closes) from the runtime
    clock's exchange calendars; a day past the published horizon falls back to
    the weekday table. ``None`` only when the market has no calendar at all —
    callers then leave the parameter untouched rather than guess.
    """
    calendar = get_market_calendar(market)
    if calendar is None:
        return None
    instant = now.astimezone(UTC)
    day = now.astimezone(calendar.tz).date()
    # The longest gap a covered calendar knows is ~10 days; the walk stays
    # bounded well past that for the fallback table's sake.
    for _ in range(21):
        close = calendar.regular_close(day)
        if close is not None and close.astimezone(UTC) <= instant:
            return day.isoformat()
        day -= timedelta(days=1)
    return None


def apply_as_of_default(
    workflow: str, params: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Fill ``as_of`` for the US coverage workflows when the caller did not.

    Missing, empty or ``latest`` all take the last completed US trading day,
    independent of the caller's timezone. A non-US ``market`` stays untouched
    (its trading day is not the US calendar's business), and so does any
    workflow outside :data:`US_AS_OF_WORKFLOWS`.
    """
    if workflow not in US_AS_OF_WORKFLOWS or not isinstance(params, dict):
        return params
    if str(params.get("market") or "US").strip().upper() not in ("US", ""):
        return params
    current = params.get("as_of")
    if isinstance(current, str) and current.strip() and current.strip() != "latest":
        return params
    moment = now or datetime.now(UTC)
    trading_day = last_completed_trading_day("US", moment)
    if trading_day is None:
        return params
    return {**params, "as_of": trading_day}


async def adopt_resumed_as_of(
    backend: Any, params: dict[str, Any], source_dir: str
) -> dict[str, Any]:
    """A resumed run keeps the PREVIOUS run's effective ``as_of``.

    The default is computed at launch, so re-filling it on a later day would
    change every date-keyed prompt hash and void the replay the resume exists
    for. The source run's ``args.json`` recorded the effective params; adopt
    its ``as_of`` when the caller did not restate one. Best-effort: an
    unreadable record leaves ``params`` untouched (the run still works, it
    just re-dispatches).
    """
    if not isinstance(params, dict):
        return params
    current = params.get("as_of")
    if isinstance(current, str) and current.strip() and current.strip() != "latest":
        return params
    try:
        text = await backend.aread_text(f"{source_dir}/args.json")
        recorded = json.loads(text or "")
    except Exception:
        logger.debug("resumed args.json unreadable", extra={"source": source_dir}, exc_info=True)
        return params
    if not isinstance(recorded, dict):
        return params
    as_of = recorded.get("as_of")
    if isinstance(as_of, str) and as_of.strip() and as_of.strip() != "latest":
        return {**params, "as_of": as_of.strip()}
    return params


async def resolve_launch_as_of(
    backend: Any, workflow: str, params: Any, journal_source: str | None
) -> Any:
    """The launch path's one ``as_of`` seam.

    A resumed run first adopts the previous run's recorded date (so replay
    prompt hashes stay stable); whatever is still open — a resume whose
    source ``args.json`` could not be read, or a fresh run — takes the
    trading-day default, never an undefined date. A workflow outside the
    set, a non-US market, or a restated date passes through untouched.
    """
    if not isinstance(params, dict):
        return params
    if journal_source is not None:
        params = await adopt_resumed_as_of(backend, params, journal_source)
    return apply_as_of_default(workflow, params)
