"""Company workspace deduplication before creating a new workspace.

Collects candidate workspaces (and active company coverage units, bound or
unbound, for internal team members) within strict tier visibility boundaries,
then delegates "is this the same company?" matching to an LLM via
`dalton_complete` using the Dalton assist model chain (`resolve_assist_model` /
`resolve_assist_fallback_models`, which filter out caller custom_models and
forbidden role variants).

Visibility, credentials, and authorization remain strictly deterministic in code:
- Active only when both `is_coverage_units_enabled()` and
  `is_dalton_supabase_auth_enabled()` are True.
- `company` tier callers bypass dedup.
- `external` tier callers check ONLY their own live, non-Home,
  non-historical-duplicate workspaces (no coverage units, no internal data).
  Credential resolution follows the standard per-user / team-sharing rules
  (`is_byok=True`, using team shared credentials when sharing is enabled, or
  own credentials / platform fallback otherwise); if model calls fail, dedup
  treats it as an error, logs a warning, and allows creation to proceed.
- `admin` / `internal` tier callers check all internal members' live,
  non-Home, non-historical-duplicate workspaces (all kinds: company, sector,
  topic) enriched with active company coverage units and latest company docs,
  plus active unbound company coverage units.
- Caller's own exact `workspace_name_key` exits early for `WorkspaceNameTaken`
  without calling the model.
- Each model call passes a per-call `scope_ref` containing `user_id` and a
  unique nonce (with `skip_journal=False`) so budget checks and usage recording
  run normally under the caller's account without cross-user/retry journal
  deduplication or replay.
- Advisory only: DB/LLM/timeout/budget/permission failures log a warning
  (exception class name only, no user data) and allow creation to proceed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

from src.dalton.access_tiers import get_user_tier
from src.dalton.coverage.identity import (
    _extract_ticker_from_text,
    is_historical_duplicate_workspace,
)
from src.dalton.credentials import get_team_credential_holder_id
from src.dalton.database.coverage_units import _norm_uuid
from src.dalton.llm.assist import (
    resolve_assist_fallback_models,
    resolve_assist_model,
)
import src.dalton.llm.complete as complete_mod
from src.dalton.llm.complete import (
    dalton_complete,
    model_requires_user_oauth,
)
from src.dalton.settings import (
    is_coverage_units_enabled,
    is_dalton_supabase_auth_enabled,
)
import src.dalton.sharing as sharing_mod
from src.llms.api_call import redact_parse_error_logs
from src.server.database.home_workspace import HOME_STAND_IN_KEY, is_reserved_row
from src.server.database.pool import get_db_connection
from src.server.database.workspace_names import WorkspaceNameInvalid, workspace_name_key
from src.server.services.runs.coordinator import spawn_protected

logger = logging.getLogger(__name__)

_REAL_GET_USER_TIER = get_user_tier
_REAL_DALTON_COMPLETE = dalton_complete

MAX_DUPLICATE_MATCHES = 3
DEDUP_BATCH_SIZE = 1000
DEDUP_TIMEOUT_SECONDS = 15.0
DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS = 9.0

DEDUP_SYSTEM_PROMPT = (
    "你是投研工作区查重助手。给定「待新建工作区名称」和「已有工作区候选清单」"
    "（含已登记但尚未绑定工作区的团队覆盖公司，其工作区名为 -），"
    "请判断清单中是否有与待新建名称指代同一家公司的条目。\n"
    "判断标准：\n"
    "1. 只有当待新建名称与候选指代同一家公司（同一法人或同一上市主体，包括中英文名、常用简称、去掉公司后缀后的名称、股票代码的不同市场写法）时，才算重复。\n"
    "2. 同一行业、上下游合作或竞争关系、母子公司（非同一上市主体）均不算重复。\n"
    "3. 若待新建名称是普通主题词（例如 \"Macro\"、\"AI\"），除非候选清单里有明显就是以该词为名称或代码的公司条目，否则不算重复。\n"
    "4. 把握不足、不确定时不要报告。\n"
    "输出要求：只返回确认为同一家公司的候选项，最多 3 条；每条包含清单中的编号 id、把握程度 confidence（仅限 \"high\" 或 \"medium\"）和一句话原因 reason；没有匹配时 matches 返回空列表。\n"
    "同时，请判断「待新建工作区名称」本身属于公司、行业还是其他，并填写附加字段（仅用于表单预填，只在把握高时给）：\n"
    "- suggested_kind：\"company\"（某家具体公司）、\"sector\"（某个行业或赛道）或 \"none\"（其他主题/个人/不确定）。\n"
    "- suggested_confidence：\"high\"、\"medium\" 或 \"low\"；只有非常确定时才填 \"high\"，不确定时填 \"low\" 或将 suggested_kind 设为 \"none\"。\n"
    "- suggested_ticker：仅当 suggested_kind 为 \"company\"、suggested_confidence 为 \"high\" 且能唯一确定股票代码时填写标准代码（如 \"MSFT\"、\"0700.HK\"、\"600519.SS\"）；未上市、存在歧义或把握不足时填 null。\n"
    "- suggested_display_name：仅当 suggested_kind 为 \"company\" 或 \"sector\" 且 suggested_confidence 为 \"high\" 时填写规范的公司名或行业名；把握不足时填 null。"
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SUGGESTION_PLACEHOLDERS = frozenset(
    {"-", "--", "n/a", "na", "none", "null", "unknown", "未知", "无"}
)


class DedupMatchItem(BaseModel):
    """Single duplicate candidate identified by the LLM."""

    id: str
    confidence: str
    reason: str = ""


class DedupLlmResult(BaseModel):
    """Structured LLM response for company workspace deduplication."""

    matches: list[DedupMatchItem]
    suggested_kind: str | None = None
    suggested_confidence: str | None = None
    suggested_ticker: str | None = None
    suggested_display_name: str | None = None


@dataclass(frozen=True)
class DedupCoverageSuggestion:
    """High-confidence workspace coverage classification suggested by the dedup LLM."""

    coverage_kind: Literal["company", "sector", "none"]
    ticker: str | None = None
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"coverage_kind": self.coverage_kind}
        if self.ticker:
            out["ticker"] = self.ticker
        if self.display_name:
            out["display_name"] = self.display_name
        return out


@dataclass(frozen=True)
class DuplicateWorkspaceCandidate:
    """Single existing workspace or unbound coverage unit matching the requested company."""

    workspace_name: str | None
    owner_name: str | None
    can_read: bool
    workspace_id: str | None = None
    lead_analyst_name: str | None = None
    ticker: str | None = None
    display_name: str | None = None
    unbound_unit: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.unbound_unit:
            return {
                "unbound_unit": True,
                "display_name": self.display_name,
                "ticker": self.ticker,
                "lead_analyst_name": self.lead_analyst_name,
                "can_read": True,
            }
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
    suggestion: DedupCoverageSuggestion | None = None

    @property
    def primary(self) -> DuplicateWorkspaceCandidate | None:
        return self.matches[0] if self.matches else None

    @property
    def has_matches(self) -> bool:
        return bool(self.matches)

    @property
    def workspace_id(self) -> str | None:
        return self.matches[0].workspace_id if (self.matches and self.matches[0].can_read) else None

    @property
    def workspace_name(self) -> str | None:
        return self.matches[0].workspace_name if self.matches else None

    @property
    def owner_name(self) -> str | None:
        return self.matches[0].owner_name if self.matches else None

    @property
    def lead_analyst_name(self) -> str | None:
        return self.matches[0].lead_analyst_name if (self.matches and self.matches[0].can_read) else None

    @property
    def ticker(self) -> str | None:
        return self.matches[0].ticker if (self.matches and self.matches[0].can_read) else None

    @property
    def can_read(self) -> bool:
        return self.matches[0].can_read if self.matches else False

    def matches_payload(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.matches]

    def suggestion_payload(self) -> dict[str, Any] | None:
        if self.suggestion is None:
            return None
        return self.suggestion.to_dict()


@dataclass(frozen=True)
class _CandidateEntry:
    """Internal candidate workspace or unbound unit representation before LLM matching."""

    short_id: str
    order_idx: int
    ws_id: str | None
    unit_id: str | None
    unbound_unit: bool
    workspace_name: str
    owner_name: str | None
    lead_analyst_name: str | None
    ticker: str | None
    unit_ticker: str | None
    unit_display_name: str | None
    doc_ticker: str | None
    doc_name: str | None
    doc_name_zh: str | None
    is_unit: bool
    activity_ts: float
    ws_row: dict[str, Any] | None


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
        "unit_updated_at",
        "updated_at",
        "workspace_created_at",
        "unit_created_at",
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


def _format_duplicate_message(
    company: str, primary: DuplicateWorkspaceCandidate
) -> str:
    if primary.unbound_unit:
        lead_part = (
            f"（主分析师 {primary.lead_analyst_name}）"
            if primary.lead_analyst_name
            else ""
        )
        return f"「{company}」已登记为团队覆盖公司{lead_part}，尚未绑定工作区"
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


async def _load_active_company_units(cur: Any) -> list[dict[str, Any]]:
    await cur.execute(
        """
        SELECT u.unit_id::text AS unit_id,
               u.kind,
               u.status,
               u.owner_user_id,
               COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
               u.display_name,
               u.workspace_id::text AS workspace_id,
               u.ticker,
               u.updated_at AS unit_updated_at,
               u.created_at AS unit_created_at,
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
        LEFT JOIN workspaces w ON w.workspace_id = u.workspace_id
        LEFT JOIN users uo ON uo.user_id = w.user_id
        LEFT JOIN users uuo ON uuo.user_id = u.owner_user_id
        LEFT JOIN users ul ON ul.user_id = COALESCE(u.lead_analyst_user_id, u.owner_user_id)
        WHERE u.status = 'active'
          AND u.kind = 'company'
          AND (
              u.workspace_id IS NULL
              OR (
                  w.workspace_id IS NOT NULL
                  AND w.status NOT IN ('deleted', 'unbound')
                  AND (w.config->'home_stand_in') IS DISTINCT FROM 'true'::jsonb
                  AND NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NULL
              )
          )
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def _load_live_workspaces(
    cur: Any,
    *,
    owner_user_id: str | None = None,
) -> list[dict[str, Any]]:
    owner_clause = "AND w.user_id = %s" if owner_user_id is not None else ""
    sql = f"""
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
          {owner_clause}
        """
    if owner_user_id is not None:
        await cur.execute(sql, (owner_user_id,))
    else:
        await cur.execute(sql)
    return [dict(r) for r in await cur.fetchall()]


async def _load_workspace_company_docs(
    cur: Any,
    ws_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Load latest valid `company` knowledge doc ticker & company names per workspace."""
    if not ws_ids:
        return {}
    await cur.execute(
        """
        SELECT DISTINCT ON (workspace_id)
               workspace_id::text AS workspace_id,
               jsonb_build_object(
                   'ticker', content->>'ticker',
                   'name', content->>'name',
                   'name_zh', content->>'name_zh'
               ) AS content
        FROM dalton_knowledge_versions
        WHERE schema_valid = TRUE
          AND doc_key = 'company'
          AND workspace_id = ANY(%s::uuid[])
        ORDER BY workspace_id, version_no DESC
        """,
        (ws_ids,),
    )
    out: dict[str, dict[str, str]] = {}
    for row in await cur.fetchall():
        if not isinstance(row, Mapping):
            continue
        if "doc_key" in row and str(row.get("doc_key") or "").strip().lower() != "company":
            continue
        ws_id = _norm_uuid(row.get("workspace_id"))
        if not ws_id:
            continue
        content = row.get("content")
        if not isinstance(content, Mapping):
            continue
        out[ws_id] = {
            "ticker": str(content.get("ticker") or "").strip().upper(),
            "name": str(content.get("name") or "").strip(),
            "name_zh": str(content.get("name_zh") or "").strip(),
        }
    return out


def _row_is_home_stand_in(
    row: Mapping[str, Any],
    *,
    config_key: str = "config",
) -> bool:
    if bool(row.get("is_home_stand_in")):
        return True
    cfg = row.get(config_key)
    return isinstance(cfg, Mapping) and bool(cfg.get(HOME_STAND_IN_KEY))


def _row_is_stand_in_or_historical(
    row: Mapping[str, Any],
    *,
    config_key: str = "config",
) -> bool:
    if _row_is_home_stand_in(row, config_key=config_key) or bool(
        row.get("is_historical_duplicate")
    ):
        return True
    cfg = row.get(config_key)
    if cfg is None:
        return False
    return is_historical_duplicate_workspace({"config": cfg})


def _is_excluded_workspace(
    *,
    ws_id: str,
    ws_owner_id: str,
    ws_status: str,
    row: Mapping[str, Any],
    config_key: str = "config",
) -> bool:
    if not ws_id or ws_status in ("deleted", "unbound"):
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


async def _resolve_caller_tier(uid: str) -> str:
    """Resolve caller tier strictly so roster/DB lookup failures raise instead of masquerading as 'external'."""
    if get_user_tier is not _REAL_GET_USER_TIER:
        return await get_user_tier(uid)
    from src.dalton.company_identity import is_company_system_user
    from src.dalton.invites import is_invite_pm
    from src.dalton.settings import get_dalton_allowed_emails, is_dalton_local_auth_enabled
    from src.server.database.user import get_user as get_user_row

    if is_company_system_user(uid):
        return "company"
    if is_dalton_local_auth_enabled():
        from src.dalton.auth.roles import get_user_role

        role = await get_user_role(uid)
        return role if role in ("admin", "internal", "external") else "external"
    if is_invite_pm(uid):
        return "admin"
    user = await get_user_row(uid)
    email = str((user or {}).get("email") or "").strip().lower()
    if email and email in get_dalton_allowed_emails():
        return "internal"
    return "external"


async def _check_workspace_readable(
    ws_id: str,
    uid: str,
    *,
    channel: str,
    workspace: Mapping[str, Any],
    conn: Any = None,
) -> bool:
    token = sharing_mod._raise_on_caller_tier_external.set(True)
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
    finally:
        sharing_mod._raise_on_caller_tier_external.reset(token)


def _caller_has_same_name_workspace(
    ws_rows: list[dict[str, Any]],
    *,
    uid: str,
    input_name_key: str | None,
) -> bool:
    if input_name_key is None:
        return False
    for ws in ws_rows:
        ws_owner_id = str(ws.get("user_id") or "").strip()
        ws_status = str(ws.get("status") or "").strip()
        ws_name = str(ws.get("name") or "").strip()
        ws_id = _norm_uuid(ws.get("workspace_id"))
        if (
            ws_owner_id == uid
            and ws_status not in ("deleted", "unbound")
            and not _row_is_home_stand_in(ws)
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
            return True
    return False


async def _collect_candidates(
    uid: str,
    raw_name: str,
    *,
    caller_tier: str,
    conn: Any = None,
    stats: dict[str, int] | None = None,
) -> tuple[bool, list[_CandidateEntry]]:
    """Collect candidate workspaces (and company units for internal callers).

    Returns ``(early_exit_same_name, candidates)`` with the DB connection released
    before returning.
    """
    input_name_key = _safe_workspace_name_key(raw_name)
    is_external = caller_tier == "external"

    raw_items: dict[str, dict[str, Any]] = {}

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            if is_external:
                unit_rows: list[dict[str, Any]] = []
                ws_rows = await _load_live_workspaces(cur, owner_user_id=uid)
                ws_rows = [
                    ws
                    for ws in ws_rows
                    if str(ws.get("user_id") or "").strip() == uid
                ]
            else:
                unit_rows = await _load_active_company_units(cur)
                ws_rows = await _load_live_workspaces(cur)

            if stats is not None:
                stats["workspace_count"] = len(ws_rows)

            if _caller_has_same_name_workspace(
                ws_rows, uid=uid, input_name_key=input_name_key
            ):
                return True, []

            units_by_ws: dict[str, dict[str, Any]] = {}
            unbound_units: dict[str, dict[str, Any]] = {}
            if not is_external:
                for u in unit_rows:
                    if str(u.get("kind") or "").strip().lower() != "company":
                        continue
                    if "status" in u and str(u.get("status") or "").strip().lower() != "active":
                        continue
                    ws_id_u = _norm_uuid(u.get("workspace_id"))
                    if not ws_id_u:
                        unit_id_u = _norm_uuid(u.get("unit_id"))
                        if not unit_id_u:
                            continue
                        unit_owner_id = str(u.get("owner_user_id") or "").strip()
                        unit_owner_email = (
                            u.get("unit_owner_email")
                            if "unit_owner_email" in u
                            else u.get("owner_email")
                        )
                        if not _is_internal_team_owner(unit_owner_id, unit_owner_email):
                            continue
                        unbound_units[unit_id_u] = u
                        continue
                    ws_owner_id = str(
                        u.get("workspace_owner_id") or u.get("owner_user_id") or ""
                    ).strip()
                    if not _is_internal_team_owner(ws_owner_id, u.get("owner_email")):
                        continue
                    unit_owner_id = str(u.get("owner_user_id") or ws_owner_id).strip()
                    unit_owner_email = (
                        u.get("unit_owner_email")
                        if "unit_owner_email" in u
                        else u.get("owner_email")
                    )
                    if (
                        unit_owner_id
                        and unit_owner_id != ws_owner_id
                        and not _is_internal_team_owner(unit_owner_id, unit_owner_email)
                    ):
                        continue
                    ws_name = str(u.get("workspace_name") or "").strip()
                    ws_status = str(u.get("workspace_status") or "").strip()
                    if not ws_name or _is_excluded_workspace(
                        ws_id=ws_id_u,
                        ws_owner_id=ws_owner_id,
                        ws_status=ws_status,
                        row=u,
                        config_key="workspace_config",
                    ):
                        continue
                    units_by_ws[ws_id_u] = u

            for ws in ws_rows:
                ws_id = _norm_uuid(ws.get("workspace_id"))
                ws_owner_id = str(ws.get("user_id") or "").strip()
                if is_external:
                    if ws_owner_id != uid:
                        continue
                elif not _is_internal_team_owner(ws_owner_id, ws.get("owner_email")):
                    continue

                ws_name = str(ws.get("name") or "").strip()
                ws_status = str(ws.get("status") or "").strip()
                if not ws_name or _is_excluded_workspace(
                    ws_id=ws_id,
                    ws_owner_id=ws_owner_id,
                    ws_status=ws_status,
                    row=ws,
                ):
                    continue

                owner_name = _resolve_display_name(
                    ws.get("owner_name"), ws.get("owner_email"), ws_owner_id
                )
                raw_items[ws_id] = {
                    "ws_id": ws_id,
                    "unit_id": None,
                    "unbound_unit": False,
                    "ws_name": ws_name,
                    "owner_name": owner_name,
                    "activity_ts": _activity_timestamp(ws),
                    "ws_row": {
                        "workspace_id": ws_id,
                        "user_id": ws_owner_id,
                        "name": ws_name,
                        "status": ws_status,
                        "config": {HOME_STAND_IN_KEY: _row_is_home_stand_in(ws)},
                    },
                    "unit": units_by_ws.get(ws_id),
                }

            for ws_id, u in units_by_ws.items():
                if ws_id in raw_items:
                    continue
                ws_owner_id = str(
                    u.get("workspace_owner_id") or u.get("owner_user_id") or ""
                ).strip()
                ws_name = str(u.get("workspace_name") or "").strip()
                ws_status = str(u.get("workspace_status") or "").strip()
                owner_name = _resolve_display_name(
                    u.get("owner_name"), u.get("owner_email"), ws_owner_id
                )
                raw_items[ws_id] = {
                    "ws_id": ws_id,
                    "unit_id": _norm_uuid(u.get("unit_id")) or None,
                    "unbound_unit": False,
                    "ws_name": ws_name,
                    "owner_name": owner_name,
                    "activity_ts": _activity_timestamp(u),
                    "ws_row": {
                        "workspace_id": ws_id,
                        "user_id": ws_owner_id,
                        "name": ws_name,
                        "status": ws_status,
                        "config": {
                            HOME_STAND_IN_KEY: _row_is_home_stand_in(
                                u, config_key="workspace_config"
                            )
                        },
                    },
                    "unit": u,
                }

            if stats is not None:
                stats["workspace_count"] = max(stats["workspace_count"], len(raw_items))

            if not raw_items and not unbound_units:
                return False, []

            docs_by_ws = (
                await _load_workspace_company_docs(cur, sorted(raw_items))
                if raw_items
                else {}
            )

    # Connection `c` is now released before LLM calls and permission checks.
    prelim: list[dict[str, Any]] = []
    for ws_id, item in raw_items.items():
        u = item["unit"]
        doc = docs_by_ws.get(ws_id) or {}
        unit_ticker = (
            str(u.get("ticker") or "").strip().upper() or None
            if u is not None
            else None
        )
        unit_display_name = (
            str(u.get("display_name") or "").strip() or None
            if u is not None
            else None
        )
        lead_uid = (
            str(u.get("lead_analyst_user_id") or u.get("owner_user_id") or "").strip()
            if u is not None
            else ""
        )
        lead_name = (
            _resolve_display_name(u.get("lead_name"), u.get("lead_email"), lead_uid)
            if u is not None and lead_uid
            else None
        )
        doc_ticker = str(doc.get("ticker") or "").strip().upper() or None
        doc_name = str(doc.get("name") or "").strip() or None
        doc_name_zh = str(doc.get("name_zh") or "").strip() or None
        effective_ticker = unit_ticker or doc_ticker

        prelim.append(
            {
                **item,
                "unit_id": _norm_uuid(u.get("unit_id")) or None if u is not None else None,
                "is_unit": u is not None,
                "lead_analyst_name": lead_name,
                "ticker": effective_ticker,
                "unit_ticker": unit_ticker,
                "unit_display_name": unit_display_name,
                "doc_ticker": doc_ticker,
                "doc_name": doc_name,
                "doc_name_zh": doc_name_zh,
            }
        )

    for unit_id_u, u in unbound_units.items():
        unit_ticker = str(u.get("ticker") or "").strip().upper() or None
        unit_display_name = str(u.get("display_name") or "").strip() or None
        if not unit_ticker and not unit_display_name:
            continue
        lead_uid = str(
            u.get("lead_analyst_user_id") or u.get("owner_user_id") or ""
        ).strip()
        lead_name = (
            _resolve_display_name(u.get("lead_name"), u.get("lead_email"), lead_uid)
            if lead_uid
            else None
        )
        prelim.append(
            {
                "ws_id": None,
                "unit_id": unit_id_u,
                "unbound_unit": True,
                "ws_name": "",
                "owner_name": None,
                "activity_ts": _activity_timestamp(u),
                "ws_row": None,
                "is_unit": True,
                "lead_analyst_name": lead_name,
                "ticker": unit_ticker,
                "unit_ticker": unit_ticker,
                "unit_display_name": unit_display_name,
                "doc_ticker": None,
                "doc_name": None,
                "doc_name_zh": None,
            }
        )

    prelim.sort(
        key=lambda x: (
            0 if x["is_unit"] else 1,
            -float(x["activity_ts"]),
            str(x["ws_name"] or x["unit_display_name"] or "").casefold(),
            str(x["ws_id"] or x["unit_id"] or ""),
        )
    )

    entries: list[_CandidateEntry] = []
    for idx, item in enumerate(prelim):
        entries.append(
            _CandidateEntry(
                short_id=f"c{idx + 1}",
                order_idx=idx,
                ws_id=item["ws_id"],
                unit_id=item["unit_id"],
                unbound_unit=item["unbound_unit"],
                workspace_name=item["ws_name"],
                owner_name=item["owner_name"],
                lead_analyst_name=item["lead_analyst_name"],
                ticker=item["ticker"],
                unit_ticker=item["unit_ticker"],
                unit_display_name=item["unit_display_name"],
                doc_ticker=item["doc_ticker"],
                doc_name=item["doc_name"],
                doc_name_zh=item["doc_name_zh"],
                is_unit=item["is_unit"],
                activity_ts=item["activity_ts"],
                ws_row=item["ws_row"],
            )
        )
    return False, entries


def _redact_uuids(text: str) -> str:
    return _UUID_RE.sub("[id]", text)


def _clean_cell(raw: str | None) -> str:
    if not raw:
        return ""
    return _redact_uuids(" ".join(str(raw).replace("|", "/").split()))


def _format_candidate_line(entry: _CandidateEntry) -> str:
    """Format one candidate as a fixed 4-column pipe-delimited line without UUID or owner."""
    tickers: list[str] = []
    for raw_t in (entry.unit_ticker, entry.doc_ticker):
        cleaned_t = _clean_cell(raw_t).upper()
        if cleaned_t and cleaned_t not in tickers:
            tickers.append(cleaned_t)

    names: list[str] = []
    seen_names: set[str] = set()
    for raw_n in (entry.unit_display_name, entry.doc_name, entry.doc_name_zh):
        cleaned_n = _clean_cell(raw_n)
        if not cleaned_n:
            continue
        folded = cleaned_n.casefold()
        if folded not in seen_names:
            seen_names.add(folded)
            names.append(cleaned_n)

    ws_col = _clean_cell(entry.workspace_name) or "-"
    ticker_col = " / ".join(tickers) if tickers else "-"
    name_col = " / ".join(names) if names else "-"
    return f"{entry.short_id} | {ws_col} | {ticker_col} | {name_col}"


def _build_batch_user_prompt(raw_name: str, batch: list[_CandidateEntry]) -> str:
    lines = "\n".join(_format_candidate_line(entry) for entry in batch)
    cleaned_name = _redact_uuids(" ".join(str(raw_name or "").split()))
    return (
        f"待新建工作区名称：{cleaned_name}\n\n"
        "已有工作区候选清单（每行固定 4 列，格式：编号 | 工作区名 | 代码 | 公司名，缺失项用 - 表示）：\n"
        f"{lines}"
    )


async def _resolve_dedup_models() -> list[str]:
    """Resolve assist primary and fallback models strictly from deployment config.

    Ignores per-user preferences (caller or team credential holder) so
    ``dalton_assist_model`` / ``custom_models`` cannot redirect candidate lists,
    while preserving ``is_forbidden_for_roles`` (L21) checks.
    """
    primary = await resolve_assist_model(role="assist", deployment_only=True)
    fallbacks = await resolve_assist_fallback_models(
        role="assist_fallback", deployment_only=True
    )
    models: list[str] = []
    for m in (primary, *fallbacks):
        cleaned = str(m or "").strip()
        if cleaned and cleaned not in models:
            models.append(cleaned)
    return models


def _get_dalton_complete_fn() -> Any:
    if dalton_complete is not _REAL_DALTON_COMPLETE:
        return dalton_complete
    if complete_mod.dalton_complete is not _REAL_DALTON_COMPLETE:
        return complete_mod.dalton_complete
    return dalton_complete


def _extract_suggestion(
    raw_kind: Any,
    raw_confidence: Any,
    raw_ticker: Any,
    raw_display_name: Any,
) -> DedupCoverageSuggestion | None:
    kind = str(raw_kind or "").strip().lower()
    if kind == "other":
        kind = "none"
    if kind not in ("company", "sector"):
        return None
    conf = str(raw_confidence or "").strip().lower()
    if conf != "high":
        return None

    clean_display = " ".join(str(raw_display_name or "").split()).strip()
    if not clean_display or clean_display.lower() in _SUGGESTION_PLACEHOLDERS:
        clean_display = None

    clean_ticker: str | None = None
    if kind == "company":
        raw_t = str(raw_ticker or "").strip()
        if raw_t and raw_t.lower() not in _SUGGESTION_PLACEHOLDERS:
            clean_ticker = _extract_ticker_from_text(raw_t)

    return DedupCoverageSuggestion(
        coverage_kind=kind,  # type: ignore[arg-type]
        ticker=clean_ticker,
        display_name=clean_display,
    )


def _parse_llm_payload(
    comp: Any,
    batch_by_id: Mapping[str, _CandidateEntry],
) -> tuple[list[tuple[_CandidateEntry, str, str]], DedupCoverageSuggestion | None]:
    """Parse structured LLM output, filter confidence & IDs, and extract high-confidence suggestion."""
    raw_data = getattr(comp, "data", None)
    raw_text = getattr(comp, "text", None)

    raw_matches: Any = None
    raw_s_kind: Any = None
    raw_s_conf: Any = None
    raw_s_ticker: Any = None
    raw_s_display: Any = None
    if isinstance(raw_data, DedupLlmResult):
        raw_matches = raw_data.matches
        raw_s_kind = raw_data.suggested_kind
        raw_s_conf = raw_data.suggested_confidence
        raw_s_ticker = raw_data.suggested_ticker
        raw_s_display = raw_data.suggested_display_name
    elif isinstance(raw_data, Mapping):
        if "matches" not in raw_data or not isinstance(raw_data["matches"], list):
            raise ValueError("Invalid dedup structured payload: missing matches list")
        raw_matches = raw_data["matches"]
        raw_s_kind = raw_data.get("suggested_kind")
        raw_s_conf = raw_data.get("suggested_confidence")
        raw_s_ticker = raw_data.get("suggested_ticker")
        raw_s_display = raw_data.get("suggested_display_name")
    elif raw_data is None and isinstance(raw_text, str) and raw_text.strip():
        txt = raw_text.strip()
        fence = _FENCE_RE.match(txt)
        if fence:
            txt = fence.group(1).strip()
        parsed = json.loads(txt)
        if not isinstance(parsed, Mapping) or not isinstance(
            parsed.get("matches"), list
        ):
            raise ValueError("Invalid dedup JSON text payload: missing matches list")
        raw_matches = parsed["matches"]
        raw_s_kind = parsed.get("suggested_kind")
        raw_s_conf = parsed.get("suggested_confidence")
        raw_s_ticker = parsed.get("suggested_ticker")
        raw_s_display = parsed.get("suggested_display_name")
    else:
        raise ValueError("Empty or unparseable dedup LLM payload")

    best_by_id: dict[str, tuple[_CandidateEntry, str, str]] = {}
    for item in raw_matches:
        if isinstance(item, DedupMatchItem):
            raw_id = item.id
            raw_conf = item.confidence
            raw_reason = item.reason
        elif isinstance(item, Mapping):
            raw_id = item.get("id")
            raw_conf = item.get("confidence")
            raw_reason = item.get("reason")
        else:
            continue

        cand_id = str(raw_id or "").strip()
        entry = batch_by_id.get(cand_id)
        if entry is None:
            continue

        conf = str(raw_conf or "").strip().lower()
        if conf not in ("high", "medium"):
            continue

        reason = str(raw_reason or "").strip()
        existing = best_by_id.get(cand_id)
        if existing is None or (existing[1] != "high" and conf == "high"):
            best_by_id[cand_id] = (entry, conf, reason)

    ranked = sorted(
        best_by_id.values(),
        key=lambda item: (
            0 if item[1] == "high" else 1,
            0 if item[0].is_unit else 1,
            -item[0].activity_ts,
            item[0].order_idx,
        ),
    )
    suggestion = _extract_suggestion(
        raw_s_kind, raw_s_conf, raw_s_ticker, raw_s_display
    )
    return ranked, suggestion


def _parse_llm_matches(
    comp: Any,
    batch_by_id: Mapping[str, _CandidateEntry],
) -> list[tuple[_CandidateEntry, str, str]]:
    """Parse structured LLM output and filter confidence & IDs."""
    matches, _ = _parse_llm_payload(comp, batch_by_id)
    return matches


async def _run_batch_with_fallback(
    *,
    uid: str,
    raw_name: str,
    batch: list[_CandidateEntry],
    models: list[str],
    deadline: float,
    is_external: bool = False,
    skip_journal: bool = False,
    suggestion_out: list[DedupCoverageSuggestion] | None = None,
) -> list[tuple[_CandidateEntry, str, str]]:
    batch_by_id = {entry.short_id: entry for entry in batch}
    user_prompt = _build_batch_user_prompt(raw_name, batch)
    complete_fn = _get_dalton_complete_fn()

    last_exc: Exception | None = None
    for idx, model_name in enumerate(models):
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise TimeoutError("Workspace dedup LLM deadline exceeded")
        model_deadline = (
            min(deadline, now + DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS)
            if idx == 0
            else deadline
        )
        model_timeout = model_deadline - now
        if model_timeout <= 0:
            continue

        call_kwargs: dict[str, Any] = {
            "user_id": uid,
            "system_prompt": DEDUP_SYSTEM_PROMPT,
            "user_prompt": user_prompt,
            "scope_ref": f"workspace_dedup:{uid}:{uuid.uuid4().hex}",
            "purpose": "workspace_dedup",
            "request_model": model_name,
            "response_schema": DedupLlmResult,
            "slot": "background",
            "timeout_s": model_timeout,
            "deadline": model_deadline,
            "persist_raw": False,
            "persist_response": False,
            "raise_on_error": False,
            "skip_journal": skip_journal,
            "record_provider_events": False,
            "ignore_custom_models": True,
        }
        if is_external:
            call_kwargs["is_byok"] = True
        else:
            call_kwargs["is_byok"] = False
            if model_requires_user_oauth(model_name):
                call_kwargs["credential_user_id"] = get_team_credential_holder_id()
            else:
                call_kwargs["credential_user_id"] = None

        try:
            with redact_parse_error_logs():
                call_task = spawn_protected(
                    complete_fn(**call_kwargs),
                    name=f"workspace-dedup-{model_name}",
                    log_failure=False,
                )
            _done_calls, pending_calls = await asyncio.wait(
                {call_task}, timeout=model_timeout
            )
            if pending_calls:
                # Keep call_task detached in spawn_protected so usage/journal writes
                # complete cleanly even after dedup stops waiting on this attempt.
                last_exc = TimeoutError(f"Dedup model {model_name} timed out")
                if time.monotonic() >= deadline:
                    raise last_exc
                continue
            comp = call_task.result()
            if not getattr(comp, "ok", False):
                err_kind = str(getattr(comp, "error_kind", None) or "").strip().lower()
                status = str(getattr(comp, "status", None) or "").strip().lower()
                if err_kind in ("timeout", "deadline"):
                    last_exc = TimeoutError(f"Dedup model {err_kind}")
                    if time.monotonic() >= deadline:
                        raise last_exc
                else:
                    last_exc = RuntimeError(
                        f"Dedup model call failed: {status or err_kind or 'unknown'}"
                    )
                continue
            matches, suggestion = _parse_llm_payload(comp, batch_by_id)
            if suggestion is not None and suggestion_out is not None:
                suggestion_out.append(suggestion)
            return matches
        except Exception as exc:
            last_exc = exc
            if (
                isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                and time.monotonic() >= deadline
            ):
                raise
            continue

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No assist model configured for workspace dedup")


def _pick_best_suggestion(
    suggestions: list[DedupCoverageSuggestion],
) -> DedupCoverageSuggestion | None:
    if not suggestions:
        return None
    ranked = sorted(
        suggestions,
        key=lambda s: (
            0 if s.coverage_kind == "company" and s.ticker else (1 if s.coverage_kind == "company" else 2),
            0 if s.display_name else 1,
        ),
    )
    return ranked[0]


async def _judge_candidates_with_llm(
    *,
    uid: str,
    raw_name: str,
    entries: list[_CandidateEntry],
    stats: dict[str, int] | None = None,
    is_external: bool = False,
    skip_journal: bool = False,
    suggestion_out: list[DedupCoverageSuggestion] | None = None,
) -> list[tuple[_CandidateEntry, str, str]]:
    """Run parallel batched LLM matching under a shared 15s deadline."""
    if not entries:
        return []
    deadline = time.monotonic() + DEDUP_TIMEOUT_SECONDS
    remaining_pref = deadline - time.monotonic()
    if remaining_pref <= 0:
        raise TimeoutError("Workspace dedup LLM deadline exceeded")
    models = await asyncio.wait_for(
        _resolve_dedup_models(),
        timeout=remaining_pref,
    )
    if not models:
        raise RuntimeError("No assist model configured for workspace dedup")

    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Workspace dedup LLM deadline exceeded")

    batches = [
        entries[i : i + DEDUP_BATCH_SIZE]
        for i in range(0, len(entries), DEDUP_BATCH_SIZE)
    ]

    batch_suggestions: list[DedupCoverageSuggestion] = []
    tasks = [
        asyncio.create_task(
            _run_batch_with_fallback(
                uid=uid,
                raw_name=raw_name,
                batch=batch,
                models=models,
                deadline=deadline,
                is_external=is_external,
                skip_journal=skip_journal,
                suggestion_out=batch_suggestions if suggestion_out is not None else None,
            )
        )
        for batch in batches
    ]

    remaining = max(0.0, deadline - time.monotonic())
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    succeeded = 0
    failed_exc: Exception | None = None
    all_hits: list[tuple[_CandidateEntry, str, str]] = []

    for task in done:
        exc = task.exception()
        if exc is not None:
            failed_exc = exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__)
            continue
        succeeded += 1
        all_hits.extend(task.result())

    if pending and failed_exc is None:
        failed_exc = TimeoutError("Workspace dedup LLM timed out")

    ws_count = stats["workspace_count"] if stats is not None else len(entries)
    if succeeded == 0:
        raise failed_exc or TimeoutError("Workspace dedup LLM timed out")

    if suggestion_out is not None and not is_external:
        best_suggestion = _pick_best_suggestion(batch_suggestions)
        if best_suggestion is not None:
            suggestion_out.append(best_suggestion)

    if failed_exc is not None:
        logger.warning(
            "Company workspace dedup partial batch failure (%s, succeeded=%d/%d, workspace_count=%d)",
            type(failed_exc).__name__,
            succeeded,
            len(batches),
            ws_count,
        )

    # Deduplicate by candidate identity (prefer higher confidence) and sort:
    # high before medium, then unit-backed, recent activity, original order.
    # Truncation to MAX_DUPLICATE_MATCHES happens after _refresh_live_hits.
    best_by_key: dict[str, tuple[_CandidateEntry, str, str]] = {}
    for entry, conf, reason in all_hits:
        dedup_key = f"ws:{entry.ws_id}" if entry.ws_id else f"unit:{entry.unit_id}"
        existing = best_by_key.get(dedup_key)
        if existing is None or (existing[1] != "high" and conf == "high"):
            best_by_key[dedup_key] = (entry, conf, reason)

    ranked = sorted(
        best_by_key.values(),
        key=lambda item: (
            0 if item[1] == "high" else 1,
            0 if item[0].is_unit else 1,
            -item[0].activity_ts,
            item[0].order_idx,
        ),
    )
    return ranked


async def _refresh_live_hits(
    hits: list[tuple[_CandidateEntry, str, str]],
    *,
    conn: Any = None,
) -> list[tuple[_CandidateEntry, dict[str, Any] | None, str, str]]:
    """Re-query hit workspaces and unit bindings after LLM return, releasing conn before permission checks."""
    if not hits:
        return []

    unit_ids = sorted({entry.unit_id for entry, _, _ in hits if entry.unit_id})
    ws_ids: set[str] = {entry.ws_id for entry, _, _ in hits if entry.ws_id}
    unit_state_by_id: dict[str, dict[str, Any]] = {}
    live_ws_by_id: dict[str, dict[str, Any]] = {}

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            if unit_ids:
                unit_set = set(unit_ids)
                await cur.execute(
                    """
                    SELECT u.unit_id::text AS unit_id,
                           u.status,
                           u.kind,
                           u.workspace_id::text AS workspace_id,
                           u.display_name,
                           u.ticker,
                           u.owner_user_id,
                           COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
                           ul.name AS lead_name,
                           ul.email AS lead_email
                    FROM dalton_coverage_units u
                    LEFT JOIN users ul ON ul.user_id = COALESCE(u.lead_analyst_user_id, u.owner_user_id)
                    WHERE u.unit_id = ANY(%s::uuid[])
                    """,
                    (unit_ids,),
                )
                for row in await cur.fetchall():
                    uid_row = _norm_uuid(row.get("unit_id"))
                    if not uid_row or uid_row not in unit_set:
                        continue
                    unit_state_by_id.setdefault(uid_row, dict(row))
                    u_row = unit_state_by_id[uid_row]
                    u_kind = str(u_row.get("kind") or "company").strip().lower()
                    u_status = str(
                        u_row.get("status") if "status" in u_row else "active"
                    ).strip().lower()
                    bound_ws = _norm_uuid(u_row.get("workspace_id"))
                    if bound_ws and u_kind == "company" and u_status == "active":
                        ws_ids.add(bound_ws)

            if ws_ids:
                await cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id,
                           w.user_id,
                           w.name,
                           w.status,
                           (w.config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
                           NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
                           u.name AS owner_name,
                           u.email AS owner_email
                    FROM workspaces w
                    LEFT JOIN users u ON u.user_id = w.user_id
                    WHERE w.workspace_id = ANY(%s::uuid[])
                    """,
                    (sorted(ws_ids),),
                )
                for row in await cur.fetchall():
                    wid = _norm_uuid(row.get("workspace_id"))
                    if not wid or wid not in ws_ids:
                        continue
                    ws_owner_id = str(row.get("user_id") or "").strip()
                    ws_status = str(row.get("status") or "").strip()
                    ws_name = str(row.get("name") or "").strip()
                    if not ws_name or _is_excluded_workspace(
                        ws_id=wid,
                        ws_owner_id=ws_owner_id,
                        ws_status=ws_status,
                        row=row,
                    ):
                        continue
                    has_owner_info = (
                        bool(ws_owner_id)
                        or "owner_name" in row
                        or "owner_email" in row
                    )
                    owner_name = (
                        _resolve_display_name(
                            row.get("owner_name"), row.get("owner_email"), ws_owner_id
                        )
                        if has_owner_info
                        else None
                    )
                    live_ws_by_id[wid] = {
                        "workspace_id": wid,
                        "user_id": ws_owner_id,
                        "name": ws_name,
                        "status": ws_status,
                        "owner_name": owner_name,
                        "config": {HOME_STAND_IN_KEY: _row_is_home_stand_in(row)},
                    }

    raw_refreshed: list[tuple[_CandidateEntry, dict[str, Any] | None, str, str]] = []
    for entry, conf, reason in hits:
        if entry.unit_id:
            u_row = unit_state_by_id.get(entry.unit_id)
            if u_row is None:
                continue
            u_kind = str(u_row.get("kind") or "company").strip().lower()
            u_status = str(
                u_row.get("status") if "status" in u_row else "active"
            ).strip().lower()
            if u_kind != "company" or u_status != "active":
                continue

            lead_uid = str(
                u_row.get("lead_analyst_user_id") or u_row.get("owner_user_id") or ""
            ).strip()
            fresh_lead = (
                _resolve_display_name(u_row.get("lead_name"), u_row.get("lead_email"), lead_uid)
                if ("lead_name" in u_row or "lead_email" in u_row) and lead_uid
                else entry.lead_analyst_name
            )
            fresh_display = str(u_row.get("display_name") or "").strip() or entry.unit_display_name
            current_ws_id = _norm_uuid(u_row.get("workspace_id"))
            same_ws = bool(
                current_ws_id and entry.ws_id and current_ws_id == entry.ws_id
            )
            unit_ticker_now = str(u_row.get("ticker") or "").strip().upper() or None
            kept_doc_ticker = entry.doc_ticker if same_ws else None
            if "ticker" in u_row:
                fresh_ticker = unit_ticker_now or kept_doc_ticker
            else:
                unit_ticker_now = entry.unit_ticker
                fresh_ticker = entry.unit_ticker or kept_doc_ticker

            if not current_ws_id:
                updated_entry = replace(
                    entry,
                    ws_id=None,
                    unbound_unit=True,
                    workspace_name="",
                    owner_name=None,
                    lead_analyst_name=fresh_lead,
                    ticker=fresh_ticker,
                    unit_ticker=unit_ticker_now,
                    doc_ticker=None,
                    unit_display_name=fresh_display,
                    ws_row=None,
                )
                raw_refreshed.append((updated_entry, None, conf, reason))
            else:
                fresh_ws = live_ws_by_id.get(current_ws_id)
                if fresh_ws is None:
                    continue
                updated_entry = replace(
                    entry,
                    ws_id=current_ws_id,
                    unbound_unit=False,
                    workspace_name=fresh_ws["name"],
                    owner_name=fresh_ws.get("owner_name") or entry.owner_name,
                    lead_analyst_name=fresh_lead,
                    ticker=fresh_ticker,
                    unit_ticker=unit_ticker_now,
                    doc_ticker=kept_doc_ticker,
                    unit_display_name=fresh_display,
                    ws_row=fresh_ws,
                )
                raw_refreshed.append((updated_entry, fresh_ws, conf, reason))
            continue

        if not entry.ws_id:
            continue
        fresh_ws = live_ws_by_id.get(entry.ws_id)
        if fresh_ws is None:
            continue
        updated_entry = replace(
            entry,
            workspace_name=fresh_ws["name"],
            owner_name=fresh_ws.get("owner_name") or entry.owner_name,
            ws_row=fresh_ws,
        )
        raw_refreshed.append((updated_entry, fresh_ws, conf, reason))

    # Deduplicate after refreshing bindings (e.g. a previously unbound unit bound
    # to a workspace that was also matched in the same batch/run).
    deduped: list[tuple[_CandidateEntry, dict[str, Any] | None, str, str]] = []
    seen_idx: dict[str, int] = {}
    for entry, fresh_ws, conf, reason in raw_refreshed:
        ws_key_id = str((fresh_ws or {}).get("workspace_id") or entry.ws_id or "").strip()
        dedup_key = (
            f"ws:{ws_key_id}"
            if (fresh_ws is not None and ws_key_id)
            else f"unit:{entry.unit_id}"
        )
        prev_pos = seen_idx.get(dedup_key)
        if prev_pos is None:
            seen_idx[dedup_key] = len(deduped)
            deduped.append((entry, fresh_ws, conf, reason))
        else:
            prev_entry, prev_ws, prev_conf, prev_reason = deduped[prev_pos]
            merged_entry = replace(
                prev_entry,
                is_unit=prev_entry.is_unit or entry.is_unit,
                unit_id=prev_entry.unit_id or entry.unit_id,
                lead_analyst_name=prev_entry.lead_analyst_name or entry.lead_analyst_name,
                ticker=prev_entry.ticker or entry.ticker,
                unit_display_name=prev_entry.unit_display_name or entry.unit_display_name,
            )
            deduped[prev_pos] = (merged_entry, prev_ws, prev_conf, prev_reason)

    return deduped


async def _resolve_live_hit_candidate(
    uid: str,
    entry: _CandidateEntry,
    fresh_ws: dict[str, Any] | None,
    *,
    channel: str,
    conn: Any = None,
) -> tuple[DuplicateWorkspaceCandidate, str | None]:
    """Resolve readability and build ``(DuplicateWorkspaceCandidate, ws_id)`` for one live hit."""
    if entry.unbound_unit and fresh_ws is None:
        return (
            DuplicateWorkspaceCandidate(
                workspace_name=None,
                owner_name=None,
                can_read=True,
                workspace_id=None,
                lead_analyst_name=entry.lead_analyst_name,
                ticker=entry.ticker,
                display_name=entry.unit_display_name or entry.ticker,
                unbound_unit=True,
            ),
            None,
        )

    ws_id = str((fresh_ws or {}).get("workspace_id") or entry.ws_id or "").strip()
    ws_name = str((fresh_ws or {}).get("name") or entry.workspace_name or "").strip()
    owner_name = str(
        (fresh_ws or {}).get("owner_name")
        or entry.owner_name
        or entry.lead_analyst_name
        or "Teammate"
    ).strip()
    ws_row = fresh_ws or entry.ws_row or {}
    can_read = False
    if uid and ws_id:
        can_read = await _check_workspace_readable(
            ws_id,
            uid,
            channel=channel,
            workspace=ws_row,
            conn=conn,
        )
    return (
        DuplicateWorkspaceCandidate(
            workspace_name=ws_name,
            owner_name=owner_name,
            can_read=can_read,
            workspace_id=ws_id if can_read else None,
            lead_analyst_name=entry.lead_analyst_name if can_read else None,
            ticker=entry.ticker if can_read else None,
        ),
        ws_id or None,
    )


async def _find_matches(
    uid: str,
    raw_name: str,
    *,
    channel: str,
    caller_tier: str,
    conn: Any = None,
    stats: dict[str, int] | None = None,
) -> DuplicateMatch | None:
    early_exit, entries = await _collect_candidates(
        uid,
        raw_name,
        caller_tier=caller_tier,
        conn=conn,
        stats=stats,
    )
    if early_exit or not entries:
        return None

    suggestion_out: list[DedupCoverageSuggestion] = []
    hits = await _judge_candidates_with_llm(
        uid=uid,
        raw_name=raw_name,
        entries=entries,
        stats=stats,
        is_external=(caller_tier == "external"),
        suggestion_out=suggestion_out,
    )
    if not hits:
        if suggestion_out:
            return DuplicateMatch(
                company=raw_name,
                message="",
                matches=(),
                matched_workspace_ids=(),
                suggestion=suggestion_out[0],
            )
        return None

    # Re-verify live workspace/unit status and bindings after LLM return, release DB
    # connection before permission checks, check read access across ALL live hits,
    # and truncate to MAX_DUPLICATE_MATCHES afterwards.
    all_live_hits = await _refresh_live_hits(hits, conn=conn)
    if not all_live_hits:
        if suggestion_out:
            return DuplicateMatch(
                company=raw_name,
                message="",
                matches=(),
                matched_workspace_ids=(),
                suggestion=suggestion_out[0],
            )
        return None

    resolved_all: list[tuple[DuplicateWorkspaceCandidate, str | None]] = []
    for entry, fresh_ws, _conf, _reason in all_live_hits:
        cand, ws_id = await _resolve_live_hit_candidate(
            uid,
            entry,
            fresh_ws,
            channel=channel,
            conn=conn,
        )
        resolved_all.append((cand, ws_id))

    all_hits_readable = all(cand.can_read for cand, _ in resolved_all)
    display_pairs = resolved_all[:MAX_DUPLICATE_MATCHES]
    resolved = [cand for cand, _ in display_pairs]
    matched_ids = [ws_id for _cand, ws_id in display_pairs if ws_id]

    message = _format_duplicate_message(raw_name, resolved[0])
    return DuplicateMatch(
        company=raw_name,
        message=message,
        matches=tuple(resolved),
        matched_workspace_ids=tuple(matched_ids),
        suggestion=suggestion_out[0] if (suggestion_out and all_hits_readable) else None,
    )


@dataclass(frozen=True)
class CompanyDedupRecheckSnapshot:
    """Result of the lock-free Phase 1 company duplicate recheck executed after
    user approval and before acquiring ``lock_user_writes``.

    - ``status == "clear"``: no undisclosed duplicate matched; ``checked_candidates``
      holds ``(workspace_id, workspace_name, unit_ticker)`` tuples for deterministic
      drift verification inside the write lock.
    - ``status == "matched"``: one or more undisclosed duplicates matched
      (``matched_workspace_ids`` names the matched workspace IDs).
    - ``status == "unavailable"``: tier lookup, candidate read, LLM call, or
      payload parse failed, so duplicate freedom could not be confirmed.
    """

    status: Literal["clear", "matched", "unavailable"]
    matched_workspace_ids: tuple[str, ...] = ()
    has_unbound_or_unreadable_match: bool = False
    checked_candidates: tuple[tuple[str, str, str | None], ...] = ()
    active: bool = True
    check_colleague_workspaces: bool = False


async def recheck_company_workspace_before_create(
    user_id: str | None,
    name: str | None,
    *,
    channel: str = "cos",
    expected_duplicate_workspace_id: str | None = None,
) -> CompanyDedupRecheckSnapshot:
    """Run lock-free company dedup (Phase 1) after confirmation approval.

    Never holds a DB lock while awaiting ``dalton_complete`` / budget / journal
    queries. Distinguishes explicit duplicate matches (with their workspace IDs)
    from check failures (``status="unavailable"``), and returns the observed
    candidate list for deterministic Phase 2 verification inside the lock.
    """
    if not is_coverage_units_enabled() or not is_dalton_supabase_auth_enabled():
        return CompanyDedupRecheckSnapshot(status="clear", active=False)

    cleaned_channel = str(channel or "cos").strip().lower()
    if cleaned_channel not in ("web", "cos"):
        raise ValueError(f"Unsupported channel: {channel!r}")

    uid = str(user_id or "").strip()
    raw_name = str(name or "").strip()
    if not uid or not raw_name:
        return CompanyDedupRecheckSnapshot(status="clear", active=False)

    stats = {"workspace_count": 0}
    try:
        caller_tier = await _resolve_caller_tier(uid)
        if caller_tier == "company":
            return CompanyDedupRecheckSnapshot(status="clear", active=False)
        check_colleagues = caller_tier != "external"
        early_exit, entries = await _collect_candidates(
            uid,
            raw_name,
            caller_tier=caller_tier,
            stats=stats,
        )
        checked_candidates = tuple(
            (entry.ws_id, entry.workspace_name, entry.unit_ticker)
            for entry in entries
            if entry.ws_id
        )
        if early_exit:
            return CompanyDedupRecheckSnapshot(
                status="matched",
                has_unbound_or_unreadable_match=True,
                checked_candidates=checked_candidates,
                check_colleague_workspaces=check_colleagues,
            )
        if not entries:
            return CompanyDedupRecheckSnapshot(
                status="clear",
                checked_candidates=(),
                check_colleague_workspaces=check_colleagues,
            )

        hits = await _judge_candidates_with_llm(
            uid=uid,
            raw_name=raw_name,
            entries=entries,
            stats=stats,
            is_external=(caller_tier == "external"),
        )
        if not hits:
            return CompanyDedupRecheckSnapshot(
                status="clear",
                checked_candidates=checked_candidates,
                check_colleague_workspaces=check_colleagues,
            )

        all_live_hits = await _refresh_live_hits(hits)
        if not all_live_hits:
            return CompanyDedupRecheckSnapshot(
                status="clear",
                checked_candidates=checked_candidates,
                check_colleague_workspaces=check_colleagues,
            )

        matched_ids: list[str] = []
        has_unbound_or_unreadable = False
        for entry, fresh_ws, _conf, _reason in all_live_hits:
            cand, ws_id = await _resolve_live_hit_candidate(
                uid,
                entry,
                fresh_ws,
                channel=cleaned_channel,
            )
            if ws_id:
                if ws_id not in matched_ids:
                    matched_ids.append(ws_id)
            if cand.unbound_unit or not cand.workspace_id:
                has_unbound_or_unreadable = True

        has_undisclosed = (
            expected_duplicate_workspace_id is None
            or has_unbound_or_unreadable
            or any(mid != expected_duplicate_workspace_id for mid in matched_ids)
            or not matched_ids
        )
        if has_undisclosed:
            return CompanyDedupRecheckSnapshot(
                status="matched",
                matched_workspace_ids=tuple(matched_ids),
                has_unbound_or_unreadable_match=has_unbound_or_unreadable,
                checked_candidates=checked_candidates,
                check_colleague_workspaces=check_colleagues,
            )
        return CompanyDedupRecheckSnapshot(
            status="clear",
            checked_candidates=checked_candidates,
            check_colleague_workspaces=check_colleagues,
        )
    except Exception as exc:
        logger.warning(
            "Lock-free company workspace dedup recheck unavailable (%s, workspace_count=%d)",
            type(exc).__name__,
            stats["workspace_count"],
        )
        return CompanyDedupRecheckSnapshot(status="unavailable")


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
        caller_tier = await _resolve_caller_tier(uid)
        if caller_tier == "company":
            return None
        return await _find_matches(
            uid,
            raw_name,
            channel=cleaned_channel,
            caller_tier=caller_tier,
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
