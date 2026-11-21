"""Company workspace deduplication before creating a new workspace.

Checks active company coverage units (`dalton_coverage_units` + aliases from
`dalton_coverage` / `SUPPLEMENTAL_TICKER_ALIASES`) and existing live, non-Home,
non-historical-duplicate workspaces across all users when
`is_coverage_units_enabled()` is True. Advisory only: DB lookup failures log a
warning and allow creation to proceed.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row

from src.dalton.coverage.identity import (
    _CLEAN_TICKER_RE,
    is_historical_duplicate_workspace,
)
from src.dalton.database.coverage_units import _can_read_workspace, _norm_uuid
from src.dalton.settings import is_coverage_units_enabled
from src.dalton.workflows.company_routing import _ticker_key
from src.server.database.home_workspace import is_reserved_row
from src.server.database.pool import get_db_connection
from src.server.database.workspace_names import WorkspaceNameInvalid, workspace_name_key

logger = logging.getLogger(__name__)

MAX_DUPLICATE_MATCHES = 3

# Extra symbols not covered by Unicode punctuation/separator categories.
_SYMBOL_STRIP_RE = re.compile(r"[+=~^|<>$`]+")


@dataclass(frozen=True)
class DuplicateWorkspaceCandidate:
    """Single existing workspace matching the requested company name or ticker."""

    workspace_name: str
    owner_name: str
    can_read: bool
    workspace_id: str | None = None
    lead_analyst_name: str | None = None
    ticker: str | None = None
    matched_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workspace_name": self.workspace_name,
            "owner_name": self.owner_name,
            "lead_analyst_name": self.lead_analyst_name if self.can_read else None,
            "ticker": self.ticker,
            "can_read": self.can_read,
        }
        if self.can_read and self.workspace_id:
            payload["workspace_id"] = self.workspace_id
        return payload


@dataclass(frozen=True)
class DuplicateMatch:
    """Result returned when one or more existing company workspaces match."""

    company: str
    message: str
    matches: tuple[DuplicateWorkspaceCandidate, ...]

    @property
    def primary(self) -> DuplicateWorkspaceCandidate:
        return self.matches[0]

    @property
    def workspace_id(self) -> str | None:
        return self.matches[0].workspace_id if self.matches[0].can_read else None

    @property
    def workspace_name(self) -> str:
        return self.matches[0].workspace_name

    @property
    def owner_name(self) -> str:
        return self.matches[0].owner_name

    @property
    def lead_analyst_name(self) -> str | None:
        return self.matches[0].lead_analyst_name

    @property
    def ticker(self) -> str | None:
        return self.matches[0].ticker

    @property
    def can_read(self) -> bool:
        return self.matches[0].can_read

    def matches_payload(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.matches]


def normalize_company_name(raw: str | None) -> str:
    """Normalize a company/workspace name: trim, casefold, strip whitespace & punctuation."""
    text = unicodedata.normalize("NFC", str(raw or "")).strip().casefold()
    if not text:
        return ""
    stripped = "".join(
        ch for ch in text if unicodedata.category(ch)[0] not in ("P", "Z", "C")
    )
    return _SYMBOL_STRIP_RE.sub("", stripped)


def _is_ticker_shaped(raw: str | None) -> bool:
    cleaned = str(raw or "").strip()
    if not cleaned or cleaned.lower() == "home" or "_" in cleaned:
        return False
    return bool(_CLEAN_TICKER_RE.match(cleaned))


def _safe_workspace_name_key(raw: str | None) -> str | None:
    try:
        return workspace_name_key(raw)
    except WorkspaceNameInvalid:
        return None


def _resolve_display_name(
    name: Any,
    email: Any,
    user_id: Any = None,
) -> str:
    cleaned_name = str(name or "").strip()
    if cleaned_name:
        return cleaned_name
    cleaned_email = str(email or "").strip()
    if "@" in cleaned_email:
        return cleaned_email.split("@", 1)[0]
    if cleaned_email:
        return cleaned_email
    cleaned_uid = str(user_id or "").strip()
    return cleaned_uid or "Teammate"


def _activity_timestamp(row: Mapping[str, Any]) -> float:
    for key in (
        "workspace_last_activity_at",
        "last_activity_at",
        "workspace_updated_at",
        "updated_at",
        "workspace_created_at",
        "created_at",
    ):
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, datetime):
            dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        if isinstance(val, str) and val.strip():
            try:
                dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    return 0.0


def _extract_alias_strings(raw_aliases: Any) -> list[str]:
    out: list[str] = []
    if isinstance(raw_aliases, Mapping):
        for s_name in raw_aliases.get("search_names") or ():
            cleaned = str(s_name or "").strip()
            if cleaned:
                out.append(cleaned)
        for key in ("adr", "earnings_tracker"):
            alt = str(raw_aliases.get(key) or "").strip()
            if alt:
                out.append(alt)
    elif isinstance(raw_aliases, (list, tuple, set)):
        for item in raw_aliases:
            cleaned = str(item or "").strip()
            if cleaned:
                out.append(cleaned)
    return out


def _supplemental_aliases_by_key() -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    try:
        from src.dalton.conversations.extractor import SUPPLEMENTAL_TICKER_ALIASES
    except Exception:
        return merged
    for raw_ticker, names in SUPPLEMENTAL_TICKER_ALIASES.items():
        t_key = _ticker_key(raw_ticker)
        if not t_key:
            continue
        bucket = merged.setdefault(t_key, [])
        for candidate in (raw_ticker, *names):
            cleaned = str(candidate or "").strip()
            if cleaned and cleaned not in bucket:
                bucket.append(cleaned)
    return merged


def _format_duplicate_message(
    company: str, primary: DuplicateWorkspaceCandidate
) -> str:
    if primary.can_read:
        lead_part = (
            f"，主分析师 {primary.lead_analyst_name}"
            if primary.lead_analyst_name
            else ""
        )
        return (
            f"「{company}」已有工作区「{primary.workspace_name}」"
            f"（所有者 {primary.owner_name}{lead_part}），"
            "请直接在那里继续。确实要另建，请确认后重试。"
        )
    return (
        f"「{company}」已有工作区「{primary.workspace_name}」"
        f"（所有者 {primary.owner_name}），"
        f"你目前没有访问权限，可以请 {primary.owner_name} 开放共享。"
        "确实要另建，请确认后重试。"
    )


async def _load_active_bound_units(cur: Any) -> list[dict[str, Any]]:
    await cur.execute(
        """
        SELECT u.unit_id::text AS unit_id,
               u.kind,
               u.owner_user_id,
               COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
               u.slug,
               u.display_name,
               u.workspace_id::text AS workspace_id,
               u.ticker,
               u.ticker_key,
               w.name AS workspace_name,
               w.user_id AS workspace_owner_id,
               w.status AS workspace_status,
               w.config AS workspace_config,
               w.last_activity_at AS workspace_last_activity_at,
               w.updated_at AS workspace_updated_at,
               w.created_at AS workspace_created_at,
               uo.name AS owner_name,
               uo.email AS owner_email,
               ul.name AS lead_name,
               ul.email AS lead_email
        FROM dalton_coverage_units u
        JOIN workspaces w ON w.workspace_id = u.workspace_id
        LEFT JOIN users uo ON uo.user_id = w.user_id
        LEFT JOIN users ul ON ul.user_id = COALESCE(u.lead_analyst_user_id, u.owner_user_id)
        WHERE u.status = 'active'
          AND w.status NOT IN ('deleted', 'unbound')
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def _load_live_workspaces(cur: Any) -> list[dict[str, Any]]:
    await cur.execute(
        """
        SELECT w.workspace_id::text AS workspace_id,
               w.user_id,
               w.name,
               w.status,
               w.config,
               w.last_activity_at,
               w.updated_at,
               w.created_at,
               u.name AS owner_name,
               u.email AS owner_email
        FROM workspaces w
        LEFT JOIN users u ON u.user_id = w.user_id
        WHERE w.status NOT IN ('deleted', 'unbound')
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def _load_coverage_aliases(conn: Any, cur: Any) -> dict[str, list[str]]:
    merged = _supplemental_aliases_by_key()
    try:
        tx_fn = getattr(conn, "transaction", None)
        if callable(tx_fn):
            async with tx_fn():
                await cur.execute("SELECT ticker, name, aliases FROM dalton_coverage")
                rows = await cur.fetchall()
        else:
            await cur.execute("SELECT ticker, name, aliases FROM dalton_coverage")
            rows = await cur.fetchall()
    except Exception as exc:
        logger.debug("Optional dalton_coverage alias read skipped: %s", exc)
        return merged

    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        t_key = _ticker_key(row.get("ticker"))
        if not t_key:
            continue
        bucket = merged.setdefault(t_key, [])
        comp_name = str(row.get("name") or "").strip()
        if comp_name and comp_name not in bucket:
            bucket.append(comp_name)
        for alias in _extract_alias_strings(row.get("aliases")):
            if alias not in bucket:
                bucket.append(alias)
    return merged


def _is_excluded_workspace(
    *,
    ws_id: str,
    ws_owner_id: str,
    ws_name: str,
    ws_status: str,
    ws_config: Any,
    caller_user_id: str,
    input_name_key: str | None,
    sector_ws_ids: set[str],
) -> bool:
    if not ws_id or ws_status in ("deleted", "unbound"):
        return True
    if ws_id in sector_ws_ids:
        return True
    ws_check = {
        "workspace_id": ws_id,
        "user_id": ws_owner_id,
        "status": ws_status,
        "config": ws_config,
    }
    if is_reserved_row(ws_check) or is_historical_duplicate_workspace(ws_check):
        return True
    if (
        caller_user_id
        and ws_owner_id == caller_user_id
        and input_name_key is not None
        and _safe_workspace_name_key(ws_name) == input_name_key
    ):
        return True
    return False


def _unit_matches_input(
    unit: Mapping[str, Any],
    *,
    norm_input: str,
    input_ticker_key: str | None,
    aliases_by_key: Mapping[str, Iterable[str]],
) -> bool:
    raw_ticker = str(unit.get("ticker") or "").strip()
    unit_ticker_key = str(unit.get("ticker_key") or "").strip().upper() or (
        _ticker_key(raw_ticker) if raw_ticker else None
    )
    if input_ticker_key and unit_ticker_key and input_ticker_key == unit_ticker_key:
        return True
    if raw_ticker and norm_input and normalize_company_name(raw_ticker) == norm_input:
        return True

    for field_name in ("display_name", "name", "slug"):
        val = str(unit.get(field_name) or "").strip()
        if val and norm_input and normalize_company_name(val) == norm_input:
            return True

    alias_candidates: list[str] = _extract_alias_strings(unit.get("aliases"))
    if unit_ticker_key and unit_ticker_key in aliases_by_key:
        alias_candidates.extend(aliases_by_key[unit_ticker_key])
    for alias in alias_candidates:
        if norm_input and normalize_company_name(alias) == norm_input:
            return True
        if input_ticker_key and _is_ticker_shaped(alias) and _ticker_key(alias) == input_ticker_key:
            return True
    return False


@dataclass(frozen=True)
class _ScoredCandidate:
    is_unit: bool
    activity_ts: float
    candidate: DuplicateWorkspaceCandidate
    ws_row: dict[str, Any]


async def _find_matches(
    uid: str,
    raw_name: str,
    *,
    conn: Any = None,
) -> DuplicateMatch | None:
    norm_input = normalize_company_name(raw_name)
    is_ticker_input = _is_ticker_shaped(raw_name)
    input_ticker_key = _ticker_key(raw_name) if is_ticker_input else None
    if not norm_input and not input_ticker_key:
        return None
    input_name_key = _safe_workspace_name_key(raw_name)

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            unit_rows = await _load_active_bound_units(cur)
            ws_rows = await _load_live_workspaces(cur)
            aliases_by_key = await _load_coverage_aliases(c, cur)

        sector_ws_ids = {
            _norm_uuid(u.get("workspace_id"))
            for u in unit_rows
            if str(u.get("kind") or "").strip().lower() == "sector"
            and u.get("workspace_id")
        }

        matched: dict[str, _ScoredCandidate] = {}

        for u in unit_rows:
            if str(u.get("kind") or "").strip().lower() != "company":
                continue
            ws_id = _norm_uuid(u.get("workspace_id"))
            ws_owner_id = str(u.get("workspace_owner_id") or u.get("owner_user_id") or "").strip()
            ws_name = str(u.get("workspace_name") or "").strip()
            ws_status = str(u.get("workspace_status") or "").strip()
            ws_config = u.get("workspace_config")
            if not ws_name or _is_excluded_workspace(
                ws_id=ws_id,
                ws_owner_id=ws_owner_id,
                ws_name=ws_name,
                ws_status=ws_status,
                ws_config=ws_config,
                caller_user_id=uid,
                input_name_key=input_name_key,
                sector_ws_ids=sector_ws_ids,
            ):
                continue
            if not _unit_matches_input(
                u,
                norm_input=norm_input,
                input_ticker_key=input_ticker_key,
                aliases_by_key=aliases_by_key,
            ):
                continue

            owner_name = _resolve_display_name(
                u.get("owner_name"), u.get("owner_email"), ws_owner_id
            )
            lead_uid = str(
                u.get("lead_analyst_user_id") or u.get("owner_user_id") or ""
            ).strip()
            lead_name = (
                _resolve_display_name(u.get("lead_name"), u.get("lead_email"), lead_uid)
                if lead_uid
                else None
            )
            unit_ticker = str(u.get("ticker") or "").strip().upper() or None
            ws_row = {
                "workspace_id": ws_id,
                "user_id": ws_owner_id,
                "name": ws_name,
                "status": ws_status,
                "config": ws_config,
            }
            matched[ws_id] = _ScoredCandidate(
                is_unit=True,
                activity_ts=_activity_timestamp(u),
                candidate=DuplicateWorkspaceCandidate(
                    workspace_name=ws_name,
                    owner_name=owner_name,
                    can_read=False,
                    workspace_id=ws_id,
                    lead_analyst_name=lead_name,
                    ticker=unit_ticker,
                    matched_by="coverage_unit",
                ),
                ws_row=ws_row,
            )

        for ws in ws_rows:
            ws_id = _norm_uuid(ws.get("workspace_id"))
            if not ws_id or ws_id in matched:
                continue
            ws_owner_id = str(ws.get("user_id") or "").strip()
            ws_name = str(ws.get("name") or "").strip()
            ws_status = str(ws.get("status") or "").strip()
            ws_config = ws.get("config")
            if not ws_name or _is_excluded_workspace(
                ws_id=ws_id,
                ws_owner_id=ws_owner_id,
                ws_name=ws_name,
                ws_status=ws_status,
                ws_config=ws_config,
                caller_user_id=uid,
                input_name_key=input_name_key,
                sector_ws_ids=sector_ws_ids,
            ):
                continue

            matched_by = ""
            matched_ticker: str | None = None
            if norm_input and normalize_company_name(ws_name) == norm_input:
                matched_by = "workspace_name"
            elif (
                input_ticker_key
                and _is_ticker_shaped(ws_name)
                and _ticker_key(ws_name) == input_ticker_key
            ):
                matched_by = "workspace_ticker"
                matched_ticker = ws_name.strip().upper()

            if not matched_by:
                continue

            owner_name = _resolve_display_name(
                ws.get("owner_name"), ws.get("owner_email"), ws_owner_id
            )
            ws_row = {
                "workspace_id": ws_id,
                "user_id": ws_owner_id,
                "name": ws_name,
                "status": ws_status,
                "config": ws_config,
            }
            matched[ws_id] = _ScoredCandidate(
                is_unit=False,
                activity_ts=_activity_timestamp(ws),
                candidate=DuplicateWorkspaceCandidate(
                    workspace_name=ws_name,
                    owner_name=owner_name,
                    can_read=False,
                    workspace_id=ws_id,
                    lead_analyst_name=None,
                    ticker=matched_ticker,
                    matched_by=matched_by,
                ),
                ws_row=ws_row,
            )

        if not matched:
            return None

        ordered = sorted(
            matched.values(),
            key=lambda item: (
                0 if item.is_unit else 1,
                -item.activity_ts,
                item.candidate.workspace_name.casefold(),
                item.candidate.workspace_id or "",
            ),
        )[:MAX_DUPLICATE_MATCHES]

        resolved: list[DuplicateWorkspaceCandidate] = []
        for item in ordered:
            cand = item.candidate
            ws_id = str(cand.workspace_id or "").strip()
            can_read = False
            if uid and ws_id:
                try:
                    can_read = await _can_read_workspace(
                        ws_id, uid, workspace=item.ws_row, conn=c
                    )
                except Exception as exc:
                    logger.warning(
                        "Workspace read check failed during dedup (ws=%s, user=%s): %s",
                        ws_id,
                        uid,
                        exc,
                    )
                    can_read = False
            resolved.append(
                DuplicateWorkspaceCandidate(
                    workspace_name=cand.workspace_name,
                    owner_name=cand.owner_name,
                    can_read=can_read,
                    workspace_id=ws_id if can_read else None,
                    lead_analyst_name=cand.lead_analyst_name if can_read else None,
                    ticker=cand.ticker,
                    matched_by=cand.matched_by,
                )
            )

    message = _format_duplicate_message(raw_name, resolved[0])
    return DuplicateMatch(
        company=raw_name,
        message=message,
        matches=tuple(resolved),
    )


async def find_existing_company_workspace(
    user_id: str | None,
    name: str | None,
    *,
    conn: Any = None,
) -> DuplicateMatch | None:
    """Return matching existing company workspaces, or None when clear / disabled / on error."""
    if not is_coverage_units_enabled():
        return None
    uid = str(user_id or "").strip()
    raw_name = str(name or "").strip()
    if not raw_name:
        return None
    try:
        return await _find_matches(uid, raw_name, conn=conn)
    except Exception as exc:
        logger.warning(
            "Company workspace dedup check failed for user %s (%r); proceeding: %s",
            uid,
            raw_name,
            exc,
        )
        return None
