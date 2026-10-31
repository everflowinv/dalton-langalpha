"""Sector-layer automation helpers (P3): the one sector due-timezone rule.

A sector agenda item's due moment follows the sector subject's own clock, not
an exchange's: the sector knowledge doc's ``research_tz`` (P1 stored it, P3
wires it in) decides, falling back to the user's timezone when the doc does
not name one, and the effective choice is written into the watch state so the
fallback is visible rather than silent. Company due-ness keeps
``due.ticker_market_timezone`` — nothing in this module runs on a company path.

Resolution chain (task P3 §2, codex r2 #6):

1. ``sector.research_tz`` from the doc — an explicit IANA zone wins; an
   unparseable value counts as absent (never a crash at probe time).
2. The user's timezone (``users.timezone`` via ``automation.get_user_timezone``
   — the zone a user's own automations run on), source ``"user"``.
3. The deployment default (``autonomy.timezone``), source ``"default"``.

``parse_due_moment`` already prefers an offset-carrying moment over any
default zone — that rule is shared with companies and unchanged here.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)

SECTOR_TZ_SOURCE_DOC = "sector_doc"
SECTOR_TZ_SOURCE_USER = "user"
SECTOR_TZ_SOURCE_DEFAULT = "default"


def _valid_tz_name(raw: Any) -> str | None:
    """Return ``raw`` when it names a loadable IANA zone, else None."""
    name = str(raw or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


async def resolve_user_timezone_name(
    user_id: str,
    *,
    conn: Any = None,
) -> tuple[str, str]:
    """The user's timezone from ``users.timezone``, else the deployment default.

    ``users.timezone`` is the zone the user's own automations run on
    (``automation.get_user_timezone``) — the budget-reset timezone is a
    different setting and must not stand in for it (codex r2 #6). Returns
    ``(tz_name, source)`` with source ``"user"`` or ``"default"``; a missing
    row or an unloadable zone name falls through.
    """
    default_tz = str(get_dalton_settings().autonomy.timezone or "Asia/Shanghai")
    uid = str(user_id or "").strip()
    if uid:
        try:
            from src.server.database.automation import get_user_timezone

            valid = _valid_tz_name(await get_user_timezone(uid))
            if valid:
                return valid, SECTOR_TZ_SOURCE_USER
        except Exception as exc:
            logger.debug(
                "[DALTON_SECTOR] user timezone lookup failed for %s: %s", uid, exc
            )
    return _valid_tz_name(default_tz) or "Asia/Shanghai", SECTOR_TZ_SOURCE_DEFAULT


async def check_sector_automation_binding(
    automation: Mapping[str, Any],
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """复查一条行业自动化任务的单元绑定。

    Returns ``(None, unit_row)`` when the task still belongs to the unit's
    current active sector binding — safe to run. Otherwise returns
    ``(reason, unit_row_or_None)`` and the caller must skip this run and do
    nothing else (r3 lead decision: no watch-state write, no pause — pausing
    is ``tracking.disable``'s job, which owns the unit identity):

    - ``sector_unit_missing_unit_id`` — the metadata carries no unit identity;
    - ``sector_unit_lookup_failed`` — registry read failed (transient);
    - ``sector_unit_not_found`` — the unit row is gone;
    - ``sector_unit_not_sector`` / ``sector_unit_archived`` — the unit no
      longer is an active sector;
    - ``sector_unit_rebound`` — the unit is bound to another workspace now;
      this task sits on the old one.
    """
    meta = automation.get("metadata") if isinstance(automation.get("metadata"), Mapping) else {}
    unit_id = str(meta.get("unit_id") or "").strip()
    if not unit_id:
        return "sector_unit_missing_unit_id", None
    try:
        from src.dalton.database.coverage_units import get_coverage_unit

        unit = await get_coverage_unit(unit_id)
    except Exception as exc:
        logger.warning(
            "[DALTON_SECTOR] coverage unit lookup failed for %s: %s", unit_id, exc
        )
        return "sector_unit_lookup_failed", None
    if unit is None:
        return "sector_unit_not_found", None
    if str(unit.get("kind") or "").strip().lower() != "sector":
        return "sector_unit_not_sector", dict(unit)
    if str(unit.get("status") or "").strip().lower() != "active":
        return "sector_unit_archived", dict(unit)
    task_ws = str(automation.get("workspace_id") or "").strip()
    unit_ws = str(unit.get("workspace_id") or "").strip()
    if task_ws != unit_ws:
        return "sector_unit_rebound", dict(unit)
    return None, dict(unit)


async def fetch_sector_doc_content(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """The sector workspace's latest valid ``sector`` doc content, or None.

    ``latest_valid_version`` opens its own connection (it takes no ``conn``);
    ``conn`` is kept in the signature for the user-timezone leg so callers
    holding the automations lock pass it through where it is honored.
    """
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    try:
        from src.dalton.database import knowledge as db_knowledge

        row = await db_knowledge.latest_valid_version(wid, "sector")
    except Exception as exc:
        logger.debug("[DALTON_SECTOR] sector doc load failed for %s: %s", wid, exc)
        return None
    content = (row or {}).get("content") if isinstance(row, dict) else None
    return dict(content) if isinstance(content, Mapping) else None


def sector_due_timezone(
    sector_content: Mapping[str, Any] | None,
    user_tz_name: str,
    user_tz_source: str = SECTOR_TZ_SOURCE_USER,
) -> tuple[str, str]:
    """Pure form: ``(tz_name, source)`` for a sector subject.

    The doc's ``research_tz`` wins when it loads; otherwise the caller's
    already-resolved user fallback passes through with its own source tag.
    """
    doc_tz = _valid_tz_name((sector_content or {}).get("research_tz"))
    if doc_tz:
        return doc_tz, SECTOR_TZ_SOURCE_DOC
    valid_user = _valid_tz_name(user_tz_name)
    if valid_user:
        return valid_user, user_tz_source
    return "Asia/Shanghai", SECTOR_TZ_SOURCE_DEFAULT


async def resolve_sector_due_timezone(
    *,
    workspace_id: str,
    user_id: str,
    sector_content: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> tuple[str, str]:
    """Compose the chain: sector doc ``research_tz`` → user tz → deployment default.

    Loads the doc when ``sector_content`` is not supplied. Company paths never
    call this; sector callers pass the unit owner as ``user_id``.
    """
    if sector_content is None:
        sector_content = await fetch_sector_doc_content(workspace_id, conn=conn)
    user_tz_name, user_tz_source = await resolve_user_timezone_name(user_id, conn=conn)
    return sector_due_timezone(sector_content, user_tz_name, user_tz_source)
