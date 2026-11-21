"""Company workspace deduplication before creating a new workspace.

Checks active company coverage units (`dalton_coverage_units` + internal-member
aliases from `dalton_coverage` / `SUPPLEMENTAL_TICKER_ALIASES`) and existing
live, non-Home, non-historical-duplicate company workspaces owned by internal
team members when both `is_coverage_units_enabled()` and
`is_dalton_supabase_auth_enabled()` are True.

Advisory only: DB/permission/alias lookup failures log a warning (without
user data) and allow creation to proceed.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.access_tiers import get_user_tier
from src.dalton.autonomy.units_view import load_dalton_automations_by_user
from src.dalton.coverage.identity import (
    _CLEAN_TICKER_RE,
    is_historical_duplicate_workspace,
    resolve_workspace_coverage_identity,
)
from src.dalton.database.coverage_units import _norm_uuid
from src.dalton.settings import (
    is_coverage_units_enabled,
    is_dalton_supabase_auth_enabled,
)
import src.dalton.sharing as sharing_mod
from src.dalton.workflows.company_routing import (
    _doc_ticker_content,
    _is_sector_workspace,
    _load_workspace_docs,
    _ticker_key,
)
from src.server.database.home_workspace import HOME_STAND_IN_KEY, is_reserved_row
from src.server.database.pool import get_db_connection
from src.server.database.workspace_names import WorkspaceNameInvalid, workspace_name_key

logger = logging.getLogger(__name__)

MAX_DUPLICATE_MATCHES = 3

# Strip only whitespace and the specified punctuation:
# `,` `.` `-` `_` `'` `’` `·` `，` `。`.
# Symbols such as `+`, `#`, `&` are preserved so "C++" does not collapse to "c".
_NORMALIZE_STRIP_RE = re.compile(r"[\s,.\-_'’·，。]+")

_PAREN_INSIDE_RE = re.compile(r"[（(]([^（）()]+)[）)]")
_PAREN_BLOCK_RE = re.compile(r"[（(][^（）()]*[）)]")
_CJK_LATIN_SPACE_RE = re.compile(
    r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z])|(?<=[A-Za-z])\s+(?=[\u4e00-\u9fff])"
)

# Trailing corporate suffixes stripped repeatedly (longest-first within each script).
_LATIN_SUFFIX_PATTERN = "|".join(
    sorted(
        (
            "Corporation",
            "Limited",
            "Holdings",
            "Holding",
            "Group",
            "Corp",
            "Inc.",
            "Inc",
            "Co.",
            "Co",
            "Ltd.",
            "Ltd",
            "PBC",
            "PLC",
            "plc",
            "N.V.",
            "NV",
            "SA",
            "AG",
        ),
        key=len,
        reverse=True,
    )
).replace(".", r"\.")
_LATIN_SUFFIX_RE = re.compile(
    rf"(?:^|[\s,，]+|(?<=[\u4e00-\u9fff]))[,，\s]*(?:{_LATIN_SUFFIX_PATTERN})\s*$",
    re.IGNORECASE,
)

_CJK_SUFFIX_PATTERN = "|".join(
    sorted(
        ("股份有限公司", "有限公司", "股份", "集团", "控股"),
        key=len,
        reverse=True,
    )
)
_CJK_SUFFIX_RE = re.compile(rf"[,，\s]*(?:{_CJK_SUFFIX_PATTERN})\s*$")


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
        if not self.can_read:
            return {
                "workspace_name": self.workspace_name,
                "owner_name": self.owner_name,
                "can_read": False,
            }
        payload: dict[str, Any] = {
            "workspace_name": self.workspace_name,
            "owner_name": self.owner_name,
            "lead_analyst_name": self.lead_analyst_name,
            "ticker": self.ticker,
            "can_read": True,
        }
        if self.workspace_id:
            payload["workspace_id"] = self.workspace_id
        return payload


@dataclass(frozen=True)
class DuplicateMatch:
    """Result returned when one or more existing company workspaces match."""

    company: str
    message: str
    matches: tuple[DuplicateWorkspaceCandidate, ...]
    # Internal-only IDs for server audit logging when allow_duplicate_company=True;
    # never serialized into client or tool payloads.
    matched_workspace_ids: tuple[str, ...] = ()

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
        return self.matches[0].lead_analyst_name if self.matches[0].can_read else None

    @property
    def ticker(self) -> str | None:
        return self.matches[0].ticker if self.matches[0].can_read else None

    @property
    def can_read(self) -> bool:
        return self.matches[0].can_read

    def matches_payload(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.matches]


def normalize_company_name(raw: str | None) -> str:
    """Normalize a company/workspace name: trim, casefold, strip spaces and `, . - _ ' ’ · ， 。`."""
    text = unicodedata.normalize("NFC", str(raw or "")).strip().casefold()
    if not text:
        return ""
    return _NORMALIZE_STRIP_RE.sub("", text)


def _strip_one_trailing_suffix(text: str) -> str | None:
    stripped_latin = _LATIN_SUFFIX_RE.sub("", text).rstrip(" ,，\t\r\n")
    if stripped_latin != text:
        return stripped_latin
    stripped_cjk = _CJK_SUFFIX_RE.sub("", text).rstrip(" ,，\t\r\n")
    if stripped_cjk != text:
        return stripped_cjk
    return None


def derive_display_name_candidates(display_name: str | None) -> tuple[str, ...]:
    """Derive deterministic candidate names from a company coverage unit's ``display_name``."""
    raw = str(display_name or "").strip()
    if not raw:
        return ()

    seeds: list[str] = [raw]
    if any(ch in raw for ch in ("(", ")", "（", "）")):
        outside = " ".join(_PAREN_BLOCK_RE.sub(" ", raw).split()).strip()
        if outside:
            seeds.append(outside)
        for inside in _PAREN_INSIDE_RE.findall(raw):
            cleaned_inside = inside.strip()
            if cleaned_inside:
                seeds.append(cleaned_inside)

    expanded: list[str] = []
    for seed in seeds:
        expanded.append(seed)
        if _CJK_LATIN_SPACE_RE.search(seed):
            for part in _CJK_LATIN_SPACE_RE.split(seed):
                cleaned_part = part.strip()
                if cleaned_part:
                    expanded.append(cleaned_part)

    candidates: list[str] = []
    seen_norm: set[str] = set()

    def _add(val: str) -> None:
        cleaned = val.strip()
        norm = normalize_company_name(cleaned)
        if len(cleaned) < 2 or len(norm) < 2:
            return
        if norm not in seen_norm:
            seen_norm.add(norm)
            candidates.append(cleaned)

    for item in expanded:
        _add(item)
        cur = item
        while True:
            nxt = _strip_one_trailing_suffix(cur)
            if nxt is None or nxt == cur:
                break
            _add(nxt)
            cur = nxt

    return tuple(candidates)


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


def _is_internal_team_owner(owner_id: str | None, owner_email: Any) -> bool:
    tier = sharing_mod._classify_owner_row_tier(owner_id, owner_email)
    return tier in ("admin", "internal")


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


def _ticker_variants_for_units(units: Iterable[Mapping[str, Any]]) -> list[str]:
    variants: set[str] = set()
    for u in units:
        for raw in (u.get("ticker"), u.get("ticker_key")):
            sym = str(raw or "").strip().upper()
            if not sym:
                continue
            variants.add(sym)
            if sym.endswith(".SS"):
                base = sym[:-3]
                variants.update({base, f"{base}.SH"})
            elif sym.endswith((".SZ", ".BJ", ".SH")):
                variants.add(sym.rsplit(".", 1)[0])
            elif sym.endswith(".HK"):
                base = sym[:-3]
                stripped = base.lstrip("0") or "0"
                variants.update({base, stripped, f"{stripped}.HK", f"{base.zfill(5)}.HK", base.zfill(5)})
            elif sym.isdigit():
                stripped = sym.lstrip("0") or "0"
                variants.update({stripped, sym.zfill(5), f"{sym.zfill(5)}.HK", f"{stripped}.HK"})
            elif "." in sym:
                variants.add(sym.replace(".", "-"))
            elif "-" in sym:
                variants.add(sym.replace("-", "."))
    return sorted(variants)


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
               u.display_name,
               u.workspace_id::text AS workspace_id,
               u.ticker,
               u.ticker_key,
               w.name AS workspace_name,
               w.user_id AS workspace_owner_id,
               w.status AS workspace_status,
               (w.config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
               NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
               w.last_activity_at AS workspace_last_activity_at,
               w.updated_at AS workspace_updated_at,
               w.created_at AS workspace_created_at,
               uo.name AS owner_name,
               uo.email AS owner_email,
               uuo.email AS unit_owner_email,
               ul.name AS lead_name,
               ul.email AS lead_email
        FROM dalton_coverage_units u
        JOIN workspaces w ON w.workspace_id = u.workspace_id
        LEFT JOIN users uo ON uo.user_id = w.user_id
        LEFT JOIN users uuo ON uuo.user_id = u.owner_user_id
        LEFT JOIN users ul ON ul.user_id = COALESCE(u.lead_analyst_user_id, u.owner_user_id)
        WHERE u.status = 'active'
          AND u.kind IN ('company', 'sector')
          AND w.status NOT IN ('deleted', 'unbound')
          AND (w.config->'home_stand_in') IS DISTINCT FROM 'true'::jsonb
          AND NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NULL
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
               (w.config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
               NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
               w.last_activity_at,
               w.updated_at,
               w.created_at,
               u.name AS owner_name,
               u.email AS owner_email
        FROM workspaces w
        LEFT JOIN users u ON u.user_id = w.user_id
        WHERE w.status NOT IN ('deleted', 'unbound')
          AND (w.config->'home_stand_in') IS DISTINCT FROM 'true'::jsonb
          AND NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NULL
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def _load_coverage_aliases(
    cur: Any,
    company_units: Iterable[Mapping[str, Any]],
) -> dict[str, list[str]]:
    merged = _supplemental_aliases_by_key()
    variants = _ticker_variants_for_units(company_units)
    if not variants:
        return merged

    await cur.execute(
        """
        SELECT dc.ticker, dc.name, dc.aliases, dc.user_id, u.email AS owner_email
        FROM dalton_coverage dc
        LEFT JOIN users u ON u.user_id = dc.user_id
        WHERE UPPER(BTRIM(dc.ticker)) = ANY(%s)
        """,
        (variants,),
    )
    rows = await cur.fetchall()

    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        row_uid = str(row.get("user_id") or "").strip()
        if not _is_internal_team_owner(row_uid, row.get("owner_email")):
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


def _row_is_stand_in_or_historical(
    row: Mapping[str, Any],
    *,
    config_key: str = "config",
) -> bool:
    if bool(row.get("is_home_stand_in")) or bool(row.get("is_historical_duplicate")):
        return True
    cfg = row.get(config_key)
    if cfg is None:
        return False
    ws_check = {"config": cfg}
    return bool(
        (isinstance(cfg, Mapping) and cfg.get(HOME_STAND_IN_KEY))
        or is_historical_duplicate_workspace(ws_check)
    )


def _is_excluded_workspace(
    *,
    ws_id: str,
    ws_owner_id: str,
    ws_status: str,
    row: Mapping[str, Any],
    sector_ws_ids: set[str],
    config_key: str = "config",
) -> bool:
    if not ws_id or ws_status in ("deleted", "unbound"):
        return True
    if ws_id in sector_ws_ids:
        return True
    if _row_is_stand_in_or_historical(row, config_key=config_key):
        return True
    ws_check = {
        "workspace_id": ws_id,
        "user_id": ws_owner_id,
        "status": ws_status,
        "config": row.get(config_key),
    }
    if is_reserved_row(ws_check) or is_historical_duplicate_workspace(ws_check):
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

    display_name = str(unit.get("display_name") or "").strip()
    if display_name and norm_input:
        for cand in derive_display_name_candidates(display_name):
            if normalize_company_name(cand) == norm_input:
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


async def _check_workspace_readable(
    ws_id: str,
    uid: str,
    *,
    channel: str,
    workspace: Mapping[str, Any],
    conn: Any = None,
) -> bool:
    try:
        await sharing_mod.require_workspace_access(
            ws_id,
            uid,
            channel=channel,
            need="read",
            workspace=dict(workspace),
            conn=conn,
        )
        return True
    except HTTPException as exc:
        if isinstance(exc, sharing_mod.OwnerTierUnavailable):
            raise
        if exc.status_code in (401, 403, 404):
            return False
        raise
    except PermissionError:
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
    channel: str,
    conn: Any = None,
    stats: dict[str, int] | None = None,
) -> DuplicateMatch | None:
    norm_input = normalize_company_name(raw_name)
    is_ticker_input = _is_ticker_shaped(raw_name)
    input_ticker_key = _ticker_key(raw_name) if is_ticker_input else None
    if not norm_input and not input_ticker_key:
        return None
    input_name_key = _safe_workspace_name_key(raw_name)

    matched: dict[str, _ScoredCandidate] = {}

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            unit_rows = await _load_active_bound_units(cur)
            ws_rows = await _load_live_workspaces(cur)
            if stats is not None:
                stats["workspace_count"] = len(ws_rows)

            # Decision 6: if the caller already has a live non-reserved workspace with the
            # exact same workspace_name_key, stop dedup and let WorkspaceNameTaken report it.
            if input_name_key is not None:
                for ws in ws_rows:
                    ws_owner_id = str(ws.get("user_id") or "").strip()
                    ws_status = str(ws.get("status") or "").strip()
                    ws_name = str(ws.get("name") or "").strip()
                    ws_id = _norm_uuid(ws.get("workspace_id"))
                    if (
                        ws_owner_id == uid
                        and ws_status not in ("deleted", "unbound")
                        and not _row_is_stand_in_or_historical(ws)
                        and not is_reserved_row(
                            {
                                "workspace_id": ws_id,
                                "user_id": ws_owner_id,
                                "status": ws_status,
                                "config": ws.get("config"),
                            }
                        )
                        and _safe_workspace_name_key(ws_name) == input_name_key
                    ):
                        return None

            sector_ws_ids = {
                _norm_uuid(u.get("workspace_id"))
                for u in unit_rows
                if str(u.get("kind") or "").strip().lower() == "sector"
                and u.get("workspace_id")
            }

            units_by_ws: dict[str, dict[str, Any]] = {}
            internal_company_units: list[dict[str, Any]] = []
            for u in unit_rows:
                ws_id_u = _norm_uuid(u.get("workspace_id"))
                if ws_id_u:
                    units_by_ws[ws_id_u] = u
                if str(u.get("kind") or "").strip().lower() != "company":
                    continue
                ws_owner_id = str(u.get("workspace_owner_id") or u.get("owner_user_id") or "").strip()
                if not _is_internal_team_owner(ws_owner_id, u.get("owner_email")):
                    continue
                unit_owner_id = str(u.get("owner_user_id") or ws_owner_id).strip()
                unit_owner_email = u.get("unit_owner_email") if "unit_owner_email" in u else u.get("owner_email")
                if unit_owner_id and unit_owner_id != ws_owner_id and not _is_internal_team_owner(unit_owner_id, unit_owner_email):
                    continue
                internal_company_units.append(u)

            aliases_by_key = await _load_coverage_aliases(cur, internal_company_units)

            unit_candidates: dict[str, _ScoredCandidate] = {}
            for u in internal_company_units:
                ws_id = _norm_uuid(u.get("workspace_id"))
                ws_owner_id = str(u.get("workspace_owner_id") or u.get("owner_user_id") or "").strip()
                ws_name = str(u.get("workspace_name") or "").strip()
                ws_status = str(u.get("workspace_status") or "").strip()
                if not ws_name or _is_excluded_workspace(
                    ws_id=ws_id,
                    ws_owner_id=ws_owner_id,
                    ws_status=ws_status,
                    row=u,
                    sector_ws_ids=sector_ws_ids,
                    config_key="workspace_config",
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
                }
                unit_candidates[ws_id] = _ScoredCandidate(
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

            ws_pre_candidates: list[tuple[str, str, str | None, dict[str, Any]]] = []
            for ws in ws_rows:
                ws_id = _norm_uuid(ws.get("workspace_id"))
                if not ws_id or ws_id in unit_candidates:
                    continue
                ws_owner_id = str(ws.get("user_id") or "").strip()
                if not _is_internal_team_owner(ws_owner_id, ws.get("owner_email")):
                    continue
                ws_name = str(ws.get("name") or "").strip()
                ws_status = str(ws.get("status") or "").strip()
                if not ws_name or _is_excluded_workspace(
                    ws_id=ws_id,
                    ws_owner_id=ws_owner_id,
                    ws_status=ws_status,
                    row=ws,
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

                if matched_by:
                    ws_pre_candidates.append((ws_id, matched_by, matched_ticker, ws))

            check_ws_ids = sorted(
                set(unit_candidates) | {item[0] for item in ws_pre_candidates}
            )
            docs_by_ws: dict[str, dict[str, Any]] = {}
            autos_by_user: dict[str, list[dict[str, Any]]] = {}
            if check_ws_ids:
                docs_by_ws = await _load_workspace_docs(cur, check_ws_ids)
                owner_ids = sorted(
                    {
                        item.ws_row["user_id"]
                        for item in unit_candidates.values()
                        if item.ws_row.get("user_id")
                    }
                    | {
                        str(ws.get("user_id") or "").strip()
                        for _, _, _, ws in ws_pre_candidates
                        if str(ws.get("user_id") or "").strip()
                    }
                )
                if len(owner_ids) <= 3:
                    for oid in owner_ids:
                        autos_by_user.update(
                            await load_dalton_automations_by_user(cur, oid)
                        )
                else:
                    autos_by_user = await load_dalton_automations_by_user(cur, None)

            for ws_id, item in unit_candidates.items():
                docs = docs_by_ws.get(ws_id) or {}
                owner_autos = autos_by_user.get(item.ws_row["user_id"], [])
                if _is_sector_workspace(
                    unit=units_by_ws.get(ws_id),
                    doc_keys=set(docs),
                    automations=owner_autos,
                    workspace_id=ws_id,
                ):
                    continue
                matched[ws_id] = item

            for ws_id, matched_by, matched_ticker, ws in ws_pre_candidates:
                ws_owner_id = str(ws.get("user_id") or "").strip()
                ws_name = str(ws.get("name") or "").strip()
                ws_status = str(ws.get("status") or "").strip()
                docs = docs_by_ws.get(ws_id) or {}
                owner_autos = autos_by_user.get(ws_owner_id, [])
                if _is_sector_workspace(
                    unit=units_by_ws.get(ws_id),
                    doc_keys=set(docs),
                    automations=owner_autos,
                    workspace_id=ws_id,
                ):
                    continue

                ws_identity_row = {
                    "workspace_id": ws_id,
                    "user_id": ws_owner_id,
                    "name": ws_name,
                    "status": ws_status,
                }
                if matched_by == "workspace_name":
                    identity = await resolve_workspace_coverage_identity(
                        ws_identity_row,
                        company_content=_doc_ticker_content(docs, "company"),
                        thesis_content=_doc_ticker_content(docs, "thesis"),
                        registered_units=units_by_ws,
                        automations=owner_autos,
                    )
                    if not identity.is_company:
                        continue
                    if matched_ticker is None and identity.ticker:
                        matched_ticker = identity.ticker

                owner_name = _resolve_display_name(
                    ws.get("owner_name"), ws.get("owner_email"), ws_owner_id
                )
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
                    ws_row=ws_identity_row,
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

    # Connection `c` is already released above before checking workspace permissions.
    resolved: list[DuplicateWorkspaceCandidate] = []
    matched_ids: list[str] = []
    for item in ordered:
        cand = item.candidate
        ws_id = str(cand.workspace_id or "").strip()
        if ws_id:
            matched_ids.append(ws_id)
        can_read = False
        if uid and ws_id:
            can_read = await _check_workspace_readable(
                ws_id,
                uid,
                channel=channel,
                workspace=item.ws_row,
                conn=conn,
            )
        resolved.append(
            DuplicateWorkspaceCandidate(
                workspace_name=cand.workspace_name,
                owner_name=cand.owner_name,
                can_read=can_read,
                workspace_id=ws_id if can_read else None,
                lead_analyst_name=cand.lead_analyst_name if can_read else None,
                ticker=cand.ticker if can_read else None,
                matched_by=cand.matched_by,
            )
        )

    message = _format_duplicate_message(raw_name, resolved[0])
    return DuplicateMatch(
        company=raw_name,
        message=message,
        matches=tuple(resolved),
        matched_workspace_ids=tuple(matched_ids),
    )


async def find_existing_company_workspace(
    user_id: str | None,
    name: str | None,
    *,
    channel: str = "web",
    conn: Any = None,
) -> DuplicateMatch | None:
    """Return matching existing company workspaces, or None when clear / disabled / on error."""
    if not is_coverage_units_enabled() or not is_dalton_supabase_auth_enabled():
        return None

    cleaned_channel = str(channel or "web").strip().lower()
    if cleaned_channel not in ("web", "cos"):
        raise ValueError(f"Unsupported channel: {channel!r}")

    uid = str(user_id or "").strip()
    raw_name = str(name or "").strip()
    if not uid or not raw_name:
        return None

    stats = {"workspace_count": 0}
    try:
        caller_tier = await get_user_tier(uid)
        if caller_tier in ("external", "company"):
            return None
        return await _find_matches(
            uid,
            raw_name,
            channel=cleaned_channel,
            conn=conn,
            stats=stats,
        )
    except Exception as exc:
        logger.warning(
            "Company workspace dedup check failed (%s, workspace_count=%d); proceeding",
            type(exc).__name__,
            stats["workspace_count"],
        )
        return None
