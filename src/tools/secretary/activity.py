"""What the user has been doing, as the Chief of Staff's ``<activity>`` block states it.

Read once per turn as the agent is built. The baseline freezes it with the rest
of the thread's context, so a change underneath it (a hand-off finishing, an
automation running) reaches the model as one activity row rather than as a
re-rendered block. That is also why it carries no price: a quote would file a
row on almost every turn while the market is open.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timezone as dt_timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from src.server.database.automation_executions import list_settled_since
from src.server.database.conversation.threads_read import get_recent_threads_for_user
from src.server.database.portfolio import get_user_portfolio
from src.server.database.workspace import get_workspaces_for_user
from src.server.services.thread_lifecycle import project_lifecycle
from src.tools.secretary.utils import (
    ComputerPlacement,
    read_computer_placement,
    user_zone,
)

logger = logging.getLogger(__name__)

RECENT_WORKSPACES = 5
RECENT_THREADS = 5
AUTOMATION_RUNS = 10
HOLDINGS = 20

_EXCERPT_CHARS = 160
_TITLE_CHARS = 80

# The read sits on the turn's critical path, beside the agent build.
_READ_TIMEOUT_S = 2.0

# Keyed on every public status, which project_lifecycle always answers in, so
# a thread line indexes it directly. Idle is a thread with no run yet.
_THREAD_STATUS = {
    "idle": "not started",
    "queued": "queued",
    "running": "running",
    "recovering": "running",
    "stopping": "stopping",
    "interrupted": "waiting for an answer",
    "completed": "finished",
    "failed": "failed",
    "cancelled": "stopped",
}
_RUN_STATUS = {"completed": "finished", "failed": "failed", "timeout": "timed out"}
# The two failures the user has to act on, not the automation.
_RUN_FAILURE = {
    "usage_limit": "a usage limit stopped it",
    "provider_auth": "the provider rejected the user's own key",
}


async def read_activity(
    user_id: str, *, home_id: str, thread_id: str | None, timezone: str
) -> str | None:
    """The ``<activity>`` text, or None when a read did not answer or render.

    None rather than a partial block: the baseline reads None as a source that
    has not answered and asks again next turn, where a partial block would
    freeze as the truth and file a row once the missing part came back.
    """
    zone = user_zone(timezone)
    today = _now().astimezone(zone).date()
    try:
        (workspaces, total), threads, runs, holdings, placement = await asyncio.wait_for(
            asyncio.gather(
                get_workspaces_for_user(
                    user_id, limit=RECENT_WORKSPACES, sort_by="recent"
                ),
                get_recent_threads_for_user(
                    user_id, exclude_thread_id=thread_id, limit=RECENT_THREADS
                ),
                list_settled_since(
                    user_id,
                    datetime.combine(today, time(), zone),
                    limit=AUTOMATION_RUNS,
                ),
                get_user_portfolio(user_id),
                read_computer_placement(user_id),
            ),
            timeout=_READ_TIMEOUT_S,
        )
        # --- DALTON-HOOK(workspace-sharing-settings): begin ---
        colleague_workspaces: list[dict[str, Any]] | None = None
        from src.dalton.settings import is_dalton_supabase_auth_enabled

        if is_dalton_supabase_auth_enabled():
            from src.dalton.sharing import list_colleague_cos_workspaces

            try:
                colleague_workspaces = await list_colleague_cos_workspaces(user_id)
            except Exception:
                colleague_workspaces = None
        # --- DALTON-HOOK(workspace-sharing-settings): end ---
        return render_activity(
            workspaces=workspaces,
            workspace_total=total,
            threads=threads,
            runs=runs,
            holdings=holdings,
            home_id=home_id,
            placement=placement,
            zone=zone,
            today=today,
            colleague_workspaces=colleague_workspaces,
        )
    except Exception:  # noqa: BLE001 - context is never worth failing a turn for
        logger.warning("[activity] read or render failed", exc_info=True)
        return None


def render_activity(
    *,
    workspaces: list[dict[str, Any]],
    workspace_total: int,
    threads: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
    home_id: str,
    zone: ZoneInfo,
    today: date,
    placement: ComputerPlacement = ComputerPlacement(),
    # DALTON-HOOK(workspace-sharing-settings): colleague workspaces visible to CoS
    colleague_workspaces: list[dict[str, Any]] | None = None,
) -> str:
    """The block's text: byte-identical for equal rows, and empty for none.

    Dates rather than times, except for today's runs, so the text moves when
    something happened rather than when the clock did. A workspace names its
    folder and its computer as ``placement`` has them, so with none it names
    neither.
    """
    sections: list[str] = []
    if workspaces:
        sections.append(
            "\n".join(
                [
                    "Recently active workspaces "
                    f"({len(workspaces)} of {workspace_total}):",
                    *(
                        _workspace_line(ws, placement, zone)
                        for ws in workspaces
                    ),
                ]
            )
        )
    # --- DALTON-HOOK(workspace-sharing-settings): begin ---
    if colleague_workspaces:
        sections.append(
            "\n".join(
                [
                    "Colleague workspaces shared with your Chief of Staff "
                    f"({len(colleague_workspaces)}):",
                    *(
                        _colleague_workspace_line(ws)
                        for ws in colleague_workspaces
                    ),
                ]
            )
        )
    # --- DALTON-HOOK(workspace-sharing-settings): end ---
    if threads:
        sections.append(
            "\n".join(
                [
                    "Recent threads, this one aside:",
                    *(_thread_line(thread, home_id, zone) for thread in threads),
                ]
            )
        )
    if runs:
        sections.append(
            "\n".join(
                [
                    f"Automation runs today, {today.isoformat()} ({zone.key}):",
                    *(_run_line(run, zone) for run in runs),
                ]
            )
        )
    if holdings:
        largest = _largest_holdings(holdings)
        sections.append(
            "Holdings by currency, largest cost basis first "
            f"({len(largest)} of {len(holdings)}): "
            + "; ".join(_holding(h) for h in largest)
        )
    return "\n\n".join(sections)


def _workspace_line(
    ws: dict[str, Any], placement: ComputerPlacement, zone: ZoneInfo
) -> str:
    name = _one_line(ws.get("name"), _TITLE_CHARS) or "Untitled"
    described = _one_line(ws.get("description"), _EXCERPT_CHARS)
    facts = []
    active = ws.get("last_activity_at") or ws.get("updated_at")
    if active:
        facts.append(f"last active {_date(active, zone)}")
    if computer := placement.computer(ws):
        facts.append(f'computer "{_one_line(computer, _TITLE_CHARS)}"')
    if folder := placement.folder(ws):
        facts.append(f"folder `{_one_line(folder, _TITLE_CHARS)}`")
    facts.append(f"workspace_id `{ws['workspace_id']}`")
    return f"- {name}{f': {described}' if described else ''} ({', '.join(facts)})"


# --- DALTON-HOOK(workspace-sharing-settings): begin ---
def _colleague_workspace_line(ws: dict[str, Any]) -> str:
    name = _one_line(ws.get("name"), _TITLE_CHARS) or "Untitled"
    described = _one_line(ws.get("description"), _EXCERPT_CHARS)
    owner_label = (
        _one_line(ws.get("owner_name"), _TITLE_CHARS)
        or _one_line(ws.get("owner_email"), _TITLE_CHARS)
        or "colleague"
    )
    cos_access = str(ws.get("cos_access") or "ro")
    access_label = "read-only" if cos_access == "ro" else "read-write"
    facts = [
        f"owner: {owner_label}",
        f"access: {cos_access} ({access_label})",
        f"workspace_id `{ws['workspace_id']}`",
    ]
    return f"- {name}{f': {described}' if described else ''} ({', '.join(facts)})"
# --- DALTON-HOOK(workspace-sharing-settings): end ---


def _thread_line(thread: dict[str, Any], home_id: str, zone: ZoneInfo) -> str:
    where = (
        "Home"
        if str(thread["workspace_id"]) == home_id
        else _one_line(thread.get("workspace_name"), _TITLE_CHARS)
    )
    title = _one_line(thread.get("title"), _TITLE_CHARS) or "Untitled"
    status = _THREAD_STATUS[project_lifecycle(thread)["run_status"]]
    return (
        f'- "{title}" in {where}: {status}, '
        f"{_date(thread['updated_at'], zone)} "
        f"(thread_id `{thread['conversation_thread_id']}`)"
    )


def _run_line(run: dict[str, Any], zone: ZoneInfo) -> str:
    name = _one_line(run.get("automation_name"), _TITLE_CHARS) or "Untitled"
    status = str(run["status"])
    said = (
        _one_line(run.get("excerpt"), _EXCERPT_CHARS)
        if status == "completed"
        else _RUN_FAILURE.get(str(run.get("failure_reason")))
        or _one_line(run.get("error_message"), _EXCERPT_CHARS)
    )
    line = (
        f"- {run['completed_at'].astimezone(zone):%H:%M} {name}, "
        f"{_RUN_STATUS.get(status, status)}{f': {said}' if said else ''}"
    )
    thread = run.get("conversation_thread_id")
    return f"{line} (thread_id `{thread}`)" if thread else line


def _largest_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Up to ``HOLDINGS`` positions, grouped by currency, largest basis first.

    Bases in different currencies do not compare, so the cap takes each
    currency's largest in turn rather than whichever currency sorts first,
    which would drop a whole currency from a long portfolio.
    """
    ordered = sorted(holdings, key=_holding_order)
    depth: dict[str, int] = {}
    places = []  # (how many of its currency rank above it, its position)
    for position, holding in enumerate(ordered):
        currency = _currency(holding)
        places.append((depth.get(currency, 0), position))
        depth[currency] = depth.get(currency, 0) + 1
    kept = sorted(position for _, position in sorted(places)[:HOLDINGS])
    return [ordered[position] for position in kept]


def _holding_order(holding: dict[str, Any]) -> tuple[str, Decimal, str]:
    # The rendered text last: it leads with the symbol and settles any tie, so
    # equal rows read the same whatever order the portfolio came back in.
    basis = _decimal(holding.get("quantity")) * _decimal(holding.get("average_cost"))
    return _currency(holding), -basis, _holding(holding)


def _currency(holding: dict[str, Any]) -> str:
    return str(holding.get("currency") or "")


def _holding(holding: dict[str, Any]) -> str:
    text = f"{_one_line(holding.get('symbol'), 24)} {_number(holding.get('quantity'))}"
    cost = holding.get("average_cost")
    if cost is not None:
        text += f" @ {_number(cost)}"
        if holding.get("currency"):
            text += f" {_one_line(holding['currency'], 8)}"
    return text


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _number(value: Any) -> str:
    return format(_decimal(value).normalize(), "f")


def _date(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).date().isoformat()


def _one_line(value: Any, cap: int) -> str:
    """Free text the user or an agent wrote, made safe to sit inside the block.

    Only the angle brackets are escaped: they are what could close the element
    early, and an escaped ampersand would hand the model "S&amp;P" to repeat.
    """
    text = " ".join(str(value or "").split())
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _now() -> datetime:
    return datetime.now(dt_timezone.utc)
