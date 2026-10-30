"""Registration service for coverage units (`dalton_coverage_units`, d023, sector layer P0).

Internal service functions, no API/frontend in this phase. Input validation and
friendly errors live here; the table's constraints remain the authority (see
`src/dalton/database/coverage_units.py`). A unit is registered unbound and
bound to a workspace as a separate step, because binding carries its own
checks (workspace exists, owned, not Home, slot free).
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from src.dalton.database import coverage_units as cu_db

VALID_UNIT_KINDS = frozenset({"company", "sector"})

# Lowercase wiki-style slug: stable identity for cross-references and the
# (owner, kind, slug) unique key. Registered once, never rewritten.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _normalize_ticker(ticker: str) -> str:
    from src.dalton.coverage.identity import _extract_ticker_from_text

    extracted = _extract_ticker_from_text(str(ticker or "").strip())
    if not extracted:
        raise ValueError(
            f"invalid company ticker {ticker!r}: expected e.g. NVDA, 0700.HK, 600519.SS"
        )
    return extracted


async def register_coverage_unit(
    owner_user_id: str,
    *,
    kind: str,
    slug: str,
    display_name: str = "",
    ticker: str | None = None,
    research_tz: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Register one coverage unit (active, unbound).

    A sector never carries a ticker; a company ticker is normalized to the same
    shape auto-tracking accepts. Binding to a workspace is a separate call
    (`bind_coverage_unit_workspace`).
    """
    uid = str(owner_user_id or "").strip()
    if not uid:
        raise ValueError("owner_user_id must not be empty")

    norm_kind = str(kind or "").strip().lower()
    if norm_kind not in VALID_UNIT_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_UNIT_KINDS)}, got {kind!r}")

    norm_slug = str(slug or "").strip().lower()
    if not _SLUG_RE.match(norm_slug):
        raise ValueError(
            f"invalid slug {slug!r}: 1-64 chars, lowercase letters/digits, then "
            "letters/digits/._- (e.g. ai-infrastructure)"
        )

    if research_tz:
        try:
            ZoneInfo(str(research_tz))
        except Exception as exc:
            raise ValueError(f"invalid research_tz {research_tz!r}: not an IANA zone") from exc

    norm_ticker: str | None = None
    raw_ticker = str(ticker or "").strip()
    if norm_kind == "sector":
        if raw_ticker:
            raise ValueError("a sector coverage unit never carries a ticker")
    elif raw_ticker:
        norm_ticker = _normalize_ticker(raw_ticker)

    return await cu_db.insert_coverage_unit(
        kind=norm_kind,
        owner_user_id=uid,
        slug=norm_slug,
        display_name=str(display_name or "").strip() or norm_slug,
        ticker=norm_ticker,
        research_tz=(str(research_tz).strip() or None) if research_tz else None,
        conn=conn,
    )


async def archive_coverage_unit(
    unit_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Archive a unit: it stops resolving as its workspace's identity and frees the binding slot."""
    _require_uuid(unit_id, "unit_id")
    row = await cu_db.set_coverage_unit_status(unit_id, owner_user_id, "archived", conn=conn)
    if row is None:
        raise ValueError(f"coverage unit {unit_id} not found for this owner")
    return row


async def bind_coverage_unit_workspace(
    unit_id: str,
    workspace_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Bind an active unit to one of the owner's non-Home workspaces (one active unit per workspace)."""
    _require_uuid(unit_id, "unit_id")
    _require_uuid(workspace_id, "workspace_id")
    return await cu_db.bind_coverage_unit_workspace(
        unit_id, workspace_id, owner_user_id, conn=conn
    )


async def unbind_coverage_unit_workspace(
    unit_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Clear a unit's workspace binding."""
    _require_uuid(unit_id, "unit_id")
    row = await cu_db.unbind_coverage_unit_workspace(unit_id, owner_user_id, conn=conn)
    if row is None:
        raise ValueError(f"coverage unit {unit_id} not found for this owner")
    return row


async def get_registered_units(
    owner_user_id: str | None = None,
    *,
    include_archived: bool = False,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List registered units (all owners when `owner_user_id` is None)."""
    return await cu_db.list_coverage_units(
        owner_user_id,
        include_archived=include_archived,
        conn=conn,
    )


def _require_uuid(raw: str, field: str) -> str:
    val = str(raw or "").strip()
    try:
        return str(uuid.UUID(val))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID, got {raw!r}") from exc
