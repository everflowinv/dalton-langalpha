"""Registration and binding service for coverage units (`dalton_coverage_units`, d041).

Input validation, normalized ``ticker_key`` computation, and §6 Decision 2
(personal ``dalton_coverage`` sync for bound listed company units) live here;
table constraints and locked transactions live in
``src/dalton/database/coverage_units.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from src.dalton.coverage.identity import normalize_ticker_key
from src.dalton.database import coverage_units as cu_db

logger = logging.getLogger(__name__)

VALID_UNIT_KINDS = frozenset({"company", "sector"})

# Lowercase wiki-style slug: stable identity for cross-references and the
# global active (kind, slug) unique index. Registered once, never rewritten.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _normalize_ticker(ticker: str) -> str:
    from src.dalton.coverage.identity import _extract_ticker_from_text

    extracted = _extract_ticker_from_text(str(ticker or "").strip())
    if not extracted:
        raise ValueError(
            f"invalid company ticker {ticker!r}: expected e.g. NVDA, 0700.HK, 600519.SS"
        )
    return extracted


async def _acquire_sorted_personal_coverage_locks(
    conn: Any,
    user_ids: list[str | None] | tuple[str | None, ...],
) -> list[str]:
    """Acquire ``pg_advisory_xact_lock('dalton_coverage', uid)`` in sorted order."""
    ordered = sorted({str(u).strip() for u in user_ids if str(u or "").strip()})
    if not ordered or conn is None or not hasattr(conn, "cursor"):
        return ordered
    from src.dalton.database import coverage as coverage_db

    for uid in ordered:
        await coverage_db.acquire_coverage_advisory_lock(conn, uid)
    return ordered


async def _sync_lead_personal_coverage(
    unit: dict[str, Any],
    *,
    conn: Any = None,
    acquire_lock: bool = True,
) -> None:
    """Idempotently sync a bound listed company unit into its lead analyst's ``dalton_coverage``."""
    if (
        str(unit.get("kind") or "") != "company"
        or str(unit.get("status") or "") != "active"
        or not unit.get("workspace_id")
        or not unit.get("ticker")
    ):
        return
    from src.dalton.coverage.service import sync_lead_analyst_personal_coverage

    await sync_lead_analyst_personal_coverage(
        unit, conn=conn, acquire_lock=acquire_lock
    )


async def _clear_unit_personal_coverage(
    unit: dict[str, Any] | None,
    *,
    conn: Any = None,
    acquire_lock: bool = True,
) -> None:
    """Remove a unit's synced canonical ``workspace_id`` from its previous lead's personal row."""
    if not unit or str(unit.get("kind") or "") != "company" or not unit.get("ticker"):
        return
    from src.dalton.coverage.service import clear_unit_from_personal_coverage

    await clear_unit_from_personal_coverage(
        unit, conn=conn, acquire_lock=acquire_lock
    )


async def lock_and_assert_managed_unit_write_allowed(
    *,
    owner_user_id: str,
    workspace_id: str | None,
    name: str | None,
    metadata: Mapping[str, Any] | None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Lock active coverage unit ``FOR SHARE`` inside caller's tx and enforce lead ownership.

    Returns the locked active unit when ``(workspace_id, name, metadata)`` is a
    managed unit automation on an active bound unit and ``owner_user_id`` is the
    current ``lead_analyst_user_id``; returns ``None`` for personal/unbound rows;
    raises ``HTTPException(403)`` when held by a non-lead user.
    """
    from fastapi import HTTPException
    from src.dalton.autonomy.classification import (
        is_managed_unit_automation,
        is_managed_unit_automation_for_unit,
    )
    from src.dalton.settings import is_coverage_units_enabled

    ws_id = str(workspace_id or "").strip()
    if not ws_id or not is_coverage_units_enabled():
        return None
    if not is_managed_unit_automation(name=name, metadata=metadata):
        return None
    has_unit_ref = isinstance(metadata, Mapping) and (
        bool(str(metadata.get("unit_id") or "").strip())
        or bool(str(metadata.get("ticker") or "").strip())
    )
    if not has_unit_ref:
        return None
    unit = await cu_db.get_active_unit_by_workspace(ws_id, conn=conn, for_share=True)
    if unit is None:
        return None
    if not is_managed_unit_automation_for_unit(name=name, metadata=metadata, unit=unit):
        return None
    lead_uid = str(
        unit.get("lead_analyst_user_id") or unit.get("owner_user_id") or ""
    ).strip()
    owner_uid = str(owner_user_id or "").strip()
    if lead_uid and owner_uid != lead_uid:
        raise HTTPException(
            status_code=403,
            detail=(
                f"工作区 {ws_id} 已绑定覆盖单元，受管自动任务仅可由当前主分析师（{lead_uid}）"
                "创建、恢复或修改。"
            ),
        )
    return unit


_UNREADABLE_IDENTITY_FIELDS = (
    "workspace_id",
    "owner_user_id",
    "lead_analyst_user_id",
    "created_by_user_id",
)


def clear_unreadable_unit_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Shared redaction: clear workspace UUID and derived owner/lead/creator IDs on unreadable bindings."""
    for field_name in _UNREADABLE_IDENTITY_FIELDS:
        entry[field_name] = None
    return entry


@dataclass(frozen=True)
class _PendingRedaction:
    """A write's response row, redacted only once the write has committed.

    The Web-read check behind the redaction reads other tables (and, under Dalton
    auth, user profiles through their own connections). Run inside the write's
    transaction it would take a second pool connection while holding row locks, and a
    swallowed query error there would let the outer COMMIT silently roll back while the
    caller is told the write succeeded. So the write returns this, and
    ``_run_in_unit_tx`` resolves it after its own transaction commits."""

    row: dict[str, Any]
    actor_user_id: str
    check_workspace_id: str | None = None


async def _redact_unit_workspace_for_actor(
    row: dict[str, Any],
    actor_user_id: str,
    *,
    check_workspace_id: str | None = None,
    conn: Any = None,
) -> _PendingRedaction:
    """Never return workspace UUID or creator/owner/lead IDs unless the actor holds Web read access.

    Inside a write this only records what to redact; see ``_PendingRedaction``."""
    del conn  # resolved after commit, on a connection of its own
    return _PendingRedaction(dict(row), actor_user_id, check_workspace_id)


async def _resolve_redaction(pending: Any, *, conn: Any = None) -> Any:
    if not isinstance(pending, _PendingRedaction):
        return pending
    out = dict(pending.row)
    target_ws = (
        str(pending.check_workspace_id).strip()
        if pending.check_workspace_id is not None
        else str(out.get("workspace_id") or "").strip()
    )
    if not target_ws:
        return out
    try:
        readable = await cu_db._can_read_workspace(target_ws, pending.actor_user_id, conn=conn)
    except Exception:
        # The write already committed; an unanswerable read check hides, never reveals.
        logger.warning("coverage unit response redaction check failed; hiding identity", exc_info=True)
        readable = False
    if not readable:
        clear_unreadable_unit_identity(out)
    return out


async def _run_in_unit_tx(conn: Any, fn: Any) -> Any:
    if conn is not None:
        # The caller owns the transaction (and its connection): resolve on it.
        return await _resolve_redaction(await fn(conn), conn=conn)
    async with cu_db.get_db_connection() as tx_conn:
        async with tx_conn.transaction():
            result = await fn(tx_conn)
    # Committed; the redaction reads on fresh connections.
    return await _resolve_redaction(result)


async def register_coverage_unit(
    owner_user_id: str,
    *,
    kind: str,
    slug: str,
    display_name: str = "",
    ticker: str | None = None,
    workspace_id: str | None = None,
    research_tz: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Register one coverage unit (active, optionally bound to ``workspace_id``)."""
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

    norm_ws: str | None = None
    if workspace_id is not None and str(workspace_id).strip():
        norm_ws = _require_uuid(str(workspace_id), "workspace_id")

    norm_ticker: str | None = None
    norm_ticker_key: str | None = None
    raw_ticker = str(ticker or "").strip()
    if norm_kind == "sector":
        if raw_ticker:
            raise ValueError("a sector coverage unit never carries a ticker")
    elif raw_ticker:
        norm_ticker = _normalize_ticker(raw_ticker)
        norm_ticker_key = normalize_ticker_key(norm_ticker)

    insert_kwargs: dict[str, Any] = {
        "kind": norm_kind,
        "owner_user_id": uid,
        "lead_analyst_user_id": uid,
        "slug": norm_slug,
        "display_name": str(display_name or "").strip() or norm_slug,
        "ticker": norm_ticker,
        "ticker_key": norm_ticker_key,
        "research_tz": (str(research_tz).strip() or None) if research_tz else None,
        "conn": conn,
    }
    if norm_ws is not None:
        insert_kwargs["workspace_id"] = norm_ws

    async def _do(c: Any) -> dict[str, Any]:
        kw = {**insert_kwargs, "conn": c}
        row = await cu_db.insert_coverage_unit(**kw)
        if (
            str(row.get("kind") or "") == "company"
            and str(row.get("status") or "") == "active"
            and row.get("workspace_id")
            and row.get("ticker")
        ):
            lead_uid = str(
                row.get("lead_analyst_user_id") or row.get("owner_user_id") or ""
            ).strip()
            await _acquire_sorted_personal_coverage_locks(c, [lead_uid])
            await _sync_lead_personal_coverage(row, conn=c, acquire_lock=False)
        return await _redact_unit_workspace_for_actor(row, uid, conn=c)

    return await _run_in_unit_tx(conn, _do)


async def archive_coverage_unit(
    unit_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Archive a coverage unit (refuses 409 if bound with active tracking/unstarted executions)."""
    unit_id = _require_uuid(unit_id, "unit_id")

    async def _do(c: Any) -> _PendingRedaction:
        prev_unit: dict[str, Any] = {}
        row = await cu_db.set_coverage_unit_status(
            unit_id, owner_user_id, "archived", conn=c, previous_out=prev_unit
        )
        if row is None:
            raise ValueError(f"coverage unit {unit_id} not found for this owner")
        target = prev_unit or row
        old_lead = str(
            target.get("lead_analyst_user_id") or target.get("owner_user_id") or ""
        ).strip()
        if str(target.get("kind") or "") == "company" and target.get("ticker"):
            await _acquire_sorted_personal_coverage_locks(c, [old_lead])
            await _clear_unit_personal_coverage(target, conn=c, acquire_lock=False)
        return await _redact_unit_workspace_for_actor(
            row,
            owner_user_id,
            check_workspace_id=str(target.get("workspace_id") or "").strip() or None,
            conn=c,
        )

    return await _run_in_unit_tx(conn, _do)


async def bind_coverage_unit_workspace(
    unit_id: str,
    workspace_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Bind an unbound active unit to `workspace_id` (or idempotently confirm same workspace).

    Rebinding a unit already bound to a different workspace is refused (409); callers
    must disable tracking and unbind first.
    """
    unit_id = _require_uuid(unit_id, "unit_id")
    workspace_id = _require_uuid(workspace_id, "workspace_id")

    async def _do(c: Any) -> _PendingRedaction:
        prev_unit: dict[str, Any] = {}
        row = await cu_db.bind_coverage_unit_workspace(
            unit_id, workspace_id, owner_user_id, conn=c, previous_out=prev_unit
        )
        old_ws = cu_db._norm_uuid(prev_unit.get("workspace_id"))
        new_ws = cu_db._norm_uuid(row.get("workspace_id"))
        if old_ws and old_ws == new_ws:
            return await _redact_unit_workspace_for_actor(
                row,
                owner_user_id,
                conn=c,
            )

        new_lead = str(
            row.get("lead_analyst_user_id") or row.get("owner_user_id") or ""
        ).strip()
        if (
            str(row.get("kind") or "") == "company"
            and str(row.get("status") or "") == "active"
            and row.get("workspace_id")
            and row.get("ticker")
            and new_lead
        ):
            await _acquire_sorted_personal_coverage_locks(c, [new_lead])
            await _sync_lead_personal_coverage(row, conn=c, acquire_lock=False)
        return await _redact_unit_workspace_for_actor(
            row,
            owner_user_id,
            conn=c,
        )

    return await _run_in_unit_tx(conn, _do)


async def unbind_coverage_unit_workspace(
    unit_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Clear a unit's workspace binding (refuses 409 if active tracking/unstarted executions exist)."""
    unit_id = _require_uuid(unit_id, "unit_id")

    async def _do(c: Any) -> _PendingRedaction:
        prev_unit: dict[str, Any] = {}
        row = await cu_db.unbind_coverage_unit_workspace(
            unit_id, owner_user_id, conn=c, previous_out=prev_unit
        )
        if row is None:
            raise ValueError(f"coverage unit {unit_id} not found for this owner")
        target = prev_unit or row
        old_lead = str(
            target.get("lead_analyst_user_id") or target.get("owner_user_id") or ""
        ).strip()
        if str(target.get("kind") or "") == "company" and target.get("ticker"):
            await _acquire_sorted_personal_coverage_locks(c, [old_lead])
            await _clear_unit_personal_coverage(target, conn=c, acquire_lock=False)
        return await _redact_unit_workspace_for_actor(
            row,
            owner_user_id,
            check_workspace_id=str(target.get("workspace_id") or "").strip() or None,
            conn=c,
        )

    return await _run_in_unit_tx(conn, _do)


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


def _slugify_for_unit(raw: str, *, kind: str) -> str:
    """Derive a deterministic `[a-z0-9][a-z0-9._-]{0,63}` slug from a ticker or display name."""
    cleaned = str(raw or "").strip()
    if not cleaned:
        return f"{kind}-unit"
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()
    has_non_ascii = any(ord(ch) > 127 for ch in cleaned)
    ascii_text = "".join(
        ch for ch in unicodedata.normalize("NFKD", cleaned) if not unicodedata.combining(ch)
    ).lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", ascii_text)
    base = re.sub(r"-{2,}", "-", base).strip("._-")
    if not base or not re.match(r"^[a-z0-9]", base):
        return f"{kind}-{digest[:8]}"
    if has_non_ascii:
        suffix = f"-{digest[:6]}"
        base = base[: 64 - len(suffix)].rstrip("._-")
        if not base or not re.match(r"^[a-z0-9]", base):
            return f"{kind}-{digest[:8]}"
        candidate = f"{base}{suffix}"
    else:
        candidate = base[:64].rstrip("._-")
    if not candidate or not _SLUG_RE.match(candidate):
        return f"{kind}-{digest[:8]}"
    return candidate


def _format_auto_register_failure(exc: ValueError, *, ticker: str | None) -> str:
    from src.dalton.coverage.unit_errors import classify_coverage_unit_error

    code, _ = classify_coverage_unit_error(exc)
    if code == "ticker_conflict":
        t_label = f" {ticker}" if ticker else ""
        return f"股票代码{t_label} 已被其他覆盖单元登记，工作区已创建但未自动登记覆盖单元。"
    if code == "slug_conflict":
        return "同名覆盖单元已被登记，工作区已创建但未自动登记覆盖单元。"
    if code == "workspace_conflict":
        return "该工作区已绑定其他覆盖单元。"
    if code == "invalid_ticker":
        return f"股票代码格式无效，工作区已创建但未自动登记覆盖单元（{exc}）。"
    if code == "sector_ticker_refused":
        return "行业覆盖单元不能填写股票代码，工作区已创建但未自动登记覆盖单元。"
    return f"覆盖单元自动登记未成功，工作区已创建但未绑定覆盖单元（{exc}）。"


async def auto_register_workspace_coverage_unit(
    user_id: str,
    *,
    workspace_id: str,
    workspace_name: str,
    coverage_kind: str | None,
    ticker: str | None = None,
    display_name: str | None = None,
    conn: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Auto-register and bind a coverage unit after workspace creation.

    Returns ``(unit_row, warning_message)``. Failures never raise or roll back
    the newly created workspace: external members are refused without calling
    ``register_coverage_unit``, and validation/conflict errors return a
    non-fatal hint in ``warning_message``.
    """
    raw_kind = str(coverage_kind or "").strip().lower()
    if raw_kind in ("", "none", "other"):
        return None, None
    if raw_kind not in VALID_UNIT_KINDS:
        return None, f"不支持的覆盖类型 {coverage_kind!r}，工作区已创建但未登记覆盖单元。"

    from src.dalton.access_tiers import is_internal_or_admin
    from src.dalton.settings import get_dalton_settings, is_coverage_units_enabled

    if not get_dalton_settings().enabled or not is_coverage_units_enabled():
        return None, "覆盖单元功能未开启，工作区已创建但未登记覆盖单元。"

    uid = str(user_id or "").strip()
    if not uid or not await is_internal_or_admin(uid):
        return None, "外部成员无权登记团队覆盖单元，工作区已创建但未自动登记。"

    eff_display_name = str(display_name or "").strip() or str(workspace_name or "").strip()
    eff_ticker = str(ticker or "").strip() or None

    try:
        if raw_kind == "company" and eff_ticker:
            norm_ticker = _normalize_ticker(eff_ticker)
            slug = _slugify_for_unit(norm_ticker, kind=raw_kind)
            eff_ticker = norm_ticker
        else:
            slug = _slugify_for_unit(eff_display_name or workspace_name, kind=raw_kind)

        row = await register_coverage_unit(
            uid,
            kind=raw_kind,
            slug=slug,
            display_name=eff_display_name,
            ticker=eff_ticker,
            workspace_id=workspace_id,
            conn=conn,
        )
        serializable = {
            k: (v.isoformat() if hasattr(v, "isoformat") else (str(v) if isinstance(v, uuid.UUID) else v))
            for k, v in row.items()
        }
        return serializable, None
    except ValueError as exc:
        logger.info(
            "Workspace %s created without coverage unit registration (%s)",
            workspace_id,
            type(exc).__name__,
        )
        return None, _format_auto_register_failure(exc, ticker=eff_ticker)
    except Exception as exc:
        logger.warning(
            "Unexpected error auto-registering coverage unit for workspace %s (%s)",
            workspace_id,
            type(exc).__name__,
        )
        return None, "覆盖单元自动登记失败，工作区已创建但未绑定覆盖单元，可在覆盖管理中手动登记。"
