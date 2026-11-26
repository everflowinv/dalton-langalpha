"""Channel Phase 2 Batch 2: DM chart_layer snapshot pipeline, cross-process Postgres state, and verified download.

Implements §2.3 & Lead Pre-Dev Decisions #1 and #2:
- Persists per-layer snapshot generation status (``status: "pending" | "ready" | "failed"``) in
  Postgres ``conversation_responses.metadata.chart_snapshots`` inside the SAME transaction that
  commits the chart layers (both synchronous ``layers.py::_publish`` and background command
  ``command_runs.py::finalize``).
- Prefetches OHLCV bars on the backend's main asyncio event loop for the turn owner's ``user_id``
  via ``DailyCacheService`` / ``IntradayCacheService`` and supplies a pure in-memory synchronous
  closure ``backend_market_bars_tool`` to ``chartkit``'s single-thread ``RENDERER``.
- Enforces deployment-wide Chromium concurrency via ``acquire_snapshot_render_slot`` (process-local
  semaphore + Postgres advisory lock slots, default max 2, 25s per-chart timeout, max 4 per turn).
- Cleans up stale ``pending`` snapshots left by crashed render workers after 10 minutes (600s).
- Authenticates DM-only snapshot downloads via ``GET /internal/snapshots/{snapshot_key:path}``:
  strict key regex, thread/run ownership, ``is_run_row_outbound_blocked == False``, platform
  sub-capability check, and live verification that ``target_address`` is the thread owner's
  actively bound DM target.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
import tempfile
import threading
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    require_channel_platform_capability,
)
from src.dalton.channel_snapshot_storage import (
    CHANNEL_SNAPSHOT_LOCK_WAIT_TIMEOUT_S,
    CHANNEL_SNAPSHOT_MAX_PER_TURN,
    CHANNEL_SNAPSHOT_RENDER_TIMEOUT_S,
    SnapshotRenderSlotTimeoutError,
    acquire_snapshot_render_slot,
    build_snapshot_key,
    get_snapshot_bytes,
    is_snapshot_storage_configured,
    parse_snapshot_key,
    put_snapshot_bytes,
)
from src.dalton.colleague_outbound_guard import (
    is_run_outbound_blocked,
    is_run_row_outbound_blocked,
)
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.database.pool import get_db_connection
from src.server.models.chart_layer import LayerRef

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_WIDTH = 1200
DEFAULT_SNAPSHOT_HEIGHT = 720
DEFAULT_SNAPSHOT_THEME = "light"
STALE_PENDING_TTL_SECONDS = 600  # 10 minutes (Lead Decision #1)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Intraday default lookback windows (aligned with chartkit/data.py)
_INTRADAY_LOOKBACK_DAYS: dict[str, int] = {
    "1min": 5,
    "5min": 20,
    "15min": 40,
    "30min": 60,
    "1hour": 120,
    "4hour": 365,
}
_DAILY_LOOKBACK_DAYS = 730

# Retain strong references to background render tasks until completion.
_BACKGROUND_RENDER_TASKS: set[asyncio.Task[Any]] = set()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(dt: datetime | None = None) -> str:
    value = dt or _utc_now()
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _infer_platform_from_target(target_address: str | None) -> str | None:
    cleaned = str(target_address or "").strip().lower()
    if cleaned.startswith("feishu:"):
        return "feishu"
    if cleaned.startswith("discord:"):
        return "discord"
    if cleaned.startswith(("weixin:", "wechat:")):
        return "weixin"
    return None


def _is_dm_target_address(platform: str, target_address: str) -> bool:
    """Return True iff ``target_address`` is a direct-message target on ``platform``.

    Accepts canonical ``<platform>:@me`` as well as explicit personal DM targets
    (``<platform>:user:<id>`` and ``<platform>:dm:<id>``, plus ``wechat:`` alias for weixin).
    Group / channel targets (e.g. ``feishu:oc_...``, ``feishu:chat:...``, ``discord:<guild>/<ch>``)
    return False so callers consistently deny them with ``snapshot_dm_only``.
    """
    plat = str(platform or "").strip().lower()
    cleaned = str(target_address or "").strip()
    if not plat or not cleaned:
        return False
    lower = cleaned.lower()
    prefixes = (f"{plat}:", "wechat:") if plat == "weixin" else (f"{plat}:",)
    rest: str | None = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            rest = cleaned[len(prefix) :].strip()
            break
    if rest is None:
        return False
    if rest == "@me":
        return True
    rest_lower = rest.lower()
    for sub in ("user:", "dm:"):
        if rest_lower.startswith(sub):
            return bool(rest[len(sub) :].strip())
    return False


def _extract_explicit_dm_recipient_id(platform: str, target_address: str) -> str | None:
    """Extract explicit platform user id from DM target address, or None for generic ``<platform>:@me``."""
    cleaned = str(target_address or "").strip()
    lower = cleaned.lower()
    prefix = f"{platform}:"
    if lower.startswith(prefix):
        rest = cleaned[len(prefix) :].strip()
    elif platform == "weixin" and lower.startswith("wechat:"):
        rest = cleaned[len("wechat:") :].strip()
    else:
        return None
    if rest == "@me":
        return None
    for sub in ("user:", "dm:"):
        if rest.lower().startswith(sub):
            val = rest[len(sub) :].strip()
            return val or None
    return None


def _pending_entry_from_layer(
    ref: LayerRef,
    body: Mapping[str, Any],
    *,
    command_id: str | None = None,
    now_iso: str,
) -> dict[str, Any]:
    title = str(body.get("title") or ref.layer_id).strip() or ref.layer_id
    return {
        "artifact_type": "chart_layer",
        "status": "pending",
        "registration_id": uuid4().hex,
        "workspace_id": str(ref.workspace_id),
        "chart_id": str(ref.chart_id),
        "symbol": str(ref.symbol),
        "timeframe": str(ref.timeframe),
        "layer_id": str(ref.layer_id),
        "title": title,
        "command_id": str(command_id) if command_id else None,
        "snapshot_key": None,
        "sha256": None,
        "size_bytes": None,
        "width": None,
        "height": None,
        "error": None,
        "pending_since": now_iso,
        "updated_at": now_iso,
    }


def _merge_chart_snapshot_entries(
    existing: Any,
    new_entries: Sequence[dict[str, Any]],
    *,
    max_per_turn: int = CHANNEL_SNAPSHOT_MAX_PER_TURN,
) -> list[dict[str, Any]]:
    """Merge snapshot entries by ``(chart_id, layer_id)`` preserving insertion order up to ``max_per_turn``."""
    merged: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}

    if isinstance(existing, list):
        for raw in existing:
            if not isinstance(raw, dict):
                continue
            lid = str(raw.get("layer_id") or "").strip()
            cid = str(raw.get("chart_id") or "").strip()
            if not lid or not cid:
                continue
            key = (cid, lid)
            if key in index_by_key:
                merged[index_by_key[key]] = dict(raw)
            else:
                index_by_key[key] = len(merged)
                merged.append(dict(raw))

    for entry in new_entries:
        lid = str(entry.get("layer_id") or "").strip()
        cid = str(entry.get("chart_id") or "").strip()
        if not lid or not cid:
            continue
        key = (cid, lid)
        if key in index_by_key:
            merged[index_by_key[key]] = dict(entry)
        elif len(merged) < max_per_turn:
            index_by_key[key] = len(merged)
            merged.append(dict(entry))

    return merged


# ---------------------------------------------------------------------------
# DM Eligibility & Same-Transaction Pending Registration
# ---------------------------------------------------------------------------


async def is_thread_eligible_for_dm_chart_snapshots(
    conn: Any,
    *,
    thread_id: str | None,
    run_id: str | None,
    user_id: str | None = None,
) -> bool:
    """Return True iff CHANNEL_GATEWAY is active, run is not outbound-blocked, and thread is DM-linked.

    Group chats and non-channel threads skip PNG rendering (group chats always emit deep links).
    """
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return False
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not tid or not rid:
        return False

    try:
        if await is_run_outbound_blocked(
            {"configurable": {"run_id": rid, "user_id": str(user_id or "")}}
        ):
            return False
    except Exception:
        return False

    async with conn.cursor(row_factory=dict_row) as cur:
        # 1. Verify run row itself is not outbound-blocked in Postgres
        await cur.execute(
            """
            SELECT conversation_response_id, conversation_thread_id::text AS thread_id, metadata
            FROM conversation_responses
            WHERE conversation_response_id = %s
            """,
            (rid,),
        )
        run_row = await cur.fetchone()
        if not run_row or str(run_row.get("thread_id") or "") != tid:
            return False
        if is_run_row_outbound_blocked(dict(run_row)):
            return False

        # 2. Reuse conversation source lookup (uses dalton_channel_inbound_events.chat_address,
        # dalton_channel_background_runs, and dalton_channel_thread_watches) scoped to this run/thread.
        source_row = await cg_db._find_current_conversation_source(
            cur,
            user_id=user_id,
            run_id=rid,
            thread_id=tid,
        )
        if source_row is not None:
            src_table = str(source_row.get("_source_table") or "").strip().lower()
            plat = str(source_row.get("platform") or "").strip().lower()
            addr = str(source_row.get("target_address") or "").strip()
            if src_table == "inbound_events":
                return str(source_row.get("chat_type") or "").strip().lower() == "dm"
            return bool(plat and addr and _is_dm_target_address(plat, addr))

        # 3. Check registered automation runs on this thread for an authorized DM target
        await cur.execute(
            """
            SELECT targets
            FROM dalton_channel_automation_runs
            WHERE thread_id = %s::uuid
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (tid,),
        )
        auto_runs = await cur.fetchall()
        for auto_row in auto_runs or ():
            raw_targets = auto_row.get("targets")
            if not isinstance(raw_targets, list):
                continue
            for t_item in raw_targets:
                if not isinstance(t_item, dict) or t_item.get("ok") is not True:
                    continue
                addr = str(t_item.get("address") or "").strip()
                plat = _infer_platform_from_target(addr)
                if plat and addr and _is_dm_target_address(plat, addr):
                    return True

        # 4. Check automation delivery runs associated with this thread for DM target
        await cur.execute(
            """
            SELECT platform, target_address
            FROM dalton_channel_deliveries
            WHERE thread_id = %s::uuid
              AND kind IN ('automation', 'automation_notify')
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (tid,),
        )
        auto_dels = await cur.fetchall()
        for d in auto_dels or ():
            plat = str(d.get("platform") or "").strip().lower()
            addr = str(d.get("target_address") or "").strip()
            if plat and addr and _is_dm_target_address(plat, addr):
                return True

    return False


async def register_pending_chart_snapshots_in_tx(
    conn: Any,
    *,
    thread_id: str | None,
    run_id: str | None,
    user_id: str | None = None,
    bodies: Sequence[tuple[LayerRef, Mapping[str, Any]]],
    command_id: str | None = None,
    max_per_turn: int = CHANNEL_SNAPSHOT_MAX_PER_TURN,
    skip_eligibility_check: bool = False,
) -> list[dict[str, Any]]:
    """Persist ``status='pending'`` entries into ``conversation_responses.metadata.chart_snapshots``
    inside the caller's active database transaction (same tx as layer upsert / command finalize).

    Returns the newly registered pending entries (capped at ``max_per_turn``), or ``[]`` when
    ineligible or disabled.
    """
    if not bodies or not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return []
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not tid or not rid:
        return []

    if not skip_eligibility_check:
        eligible = await is_thread_eligible_for_dm_chart_snapshots(
            conn, thread_id=tid, run_id=rid, user_id=user_id
        )
        if not eligible:
            return []

    now_iso = _iso_utc()
    candidates = [
        _pending_entry_from_layer(ref, body, command_id=command_id, now_iso=now_iso)
        for ref, body in bodies[:max_per_turn]
    ]
    if not candidates:
        return []

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT metadata
            FROM conversation_responses
            WHERE conversation_response_id = %s
              AND conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (rid, tid),
        )
        row = await cur.fetchone()
        if not row:
            return []
        meta = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
        merged = _merge_chart_snapshot_entries(
            meta.get("chart_snapshots"), candidates, max_per_turn=max_per_turn
        )
        meta["chart_snapshots"] = merged
        await cur.execute(
            """
            UPDATE conversation_responses
            SET metadata = %s::jsonb
            WHERE conversation_response_id = %s
            """,
            (json.dumps(meta, ensure_ascii=False), rid),
        )

    registered_keys = {(c["chart_id"], c["layer_id"]) for c in candidates}
    return [
        entry
        for entry in merged
        if (entry["chart_id"], entry["layer_id"]) in registered_keys
    ]


async def update_chart_snapshot_status(
    *,
    run_id: str,
    chart_id: str,
    layer_id: str,
    status: str,
    snapshot_key: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    width: int | None = None,
    height: int | None = None,
    error: str | None = None,
    expected_registration_id: str | None = None,
    require_pending: bool = True,
    conn: Any = None,
) -> bool:
    """Atomically transition one ``chart_snapshots`` item to ``ready`` or ``failed`` in Postgres.

    Guarded by ``require_pending=True`` and optional ``expected_registration_id`` so a late or
    superseded render task can never overwrite a re-published layer registration or revive a
    reaped ``failed`` entry back to ``ready``.
    """
    if status not in ("pending", "ready", "failed"):
        raise ValueError(f"Invalid chart snapshot status: {status!r}")
    rid = str(run_id or "").strip()
    cid = str(chart_id or "").strip()
    lid = str(layer_id or "").strip()
    if not rid or not cid or not lid:
        return False
    expected_reg = str(expected_registration_id or "").strip() or None

    now_iso = _iso_utc()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s
                    FOR UPDATE
                    """,
                    (rid,),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                meta = (
                    dict(row.get("metadata") or {})
                    if isinstance(row.get("metadata"), dict)
                    else {}
                )
                items = meta.get("chart_snapshots")
                if not isinstance(items, list):
                    return False

                updated = False
                new_items: list[dict[str, Any]] = []
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    item = dict(raw)
                    if (
                        str(item.get("chart_id") or "") == cid
                        and str(item.get("layer_id") or "") == lid
                    ):
                        current_status = str(item.get("status") or "").strip().lower()
                        current_reg = str(item.get("registration_id") or "").strip() or None
                        if require_pending and current_status != "pending":
                            new_items.append(item)
                            continue
                        if expected_reg is not None and current_reg != expected_reg:
                            new_items.append(item)
                            continue
                        item["status"] = status
                        item["updated_at"] = now_iso
                        if status == "ready":
                            item["snapshot_key"] = snapshot_key
                            item["sha256"] = sha256
                            item["size_bytes"] = int(size_bytes) if size_bytes is not None else None
                            item["width"] = int(width) if width is not None else None
                            item["height"] = int(height) if height is not None else None
                            item["error"] = None
                        elif status == "failed":
                            item["error"] = str(error or "render_failed")[:300]
                        updated = True
                    new_items.append(item)

                if not updated:
                    return False
                meta["chart_snapshots"] = new_items
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET metadata = %s::jsonb
                    WHERE conversation_response_id = %s
                    """,
                    (json.dumps(meta, ensure_ascii=False), rid),
                )
    return True


async def expire_stale_pending_chart_snapshots(
    *,
    stale_after_seconds: int = STALE_PENDING_TTL_SECONDS,
    now: datetime | None = None,
    limit: int = 200,
    conn: Any = None,
) -> int:
    """Mark ``chart_snapshots`` items stuck in ``status='pending'`` past ``stale_after_seconds`` as ``failed``.

    Called periodically by ``RecoveryScanner`` (default 10 minutes / 600s, Lead Decision #1) so a
    crashed render worker never strands pending entries indefinitely.
    """
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return 0
    cutoff = (now or _utc_now()) - timedelta(seconds=max(1, int(stale_after_seconds)))
    now_iso = _iso_utc(now)
    expired_count = 0

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT conversation_response_id, metadata
                    FROM conversation_responses
                    WHERE metadata ? 'chart_snapshots'
                      AND metadata->'chart_snapshots' @> '[{"status": "pending"}]'::jsonb
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (int(limit),),
                )
                rows = await cur.fetchall()
                for row in rows or ():
                    rid = str(row["conversation_response_id"])
                    meta = (
                        dict(row.get("metadata") or {})
                        if isinstance(row.get("metadata"), dict)
                        else {}
                    )
                    items = meta.get("chart_snapshots")
                    if not isinstance(items, list):
                        continue
                    changed = False
                    new_items: list[dict[str, Any]] = []
                    for raw in items:
                        if not isinstance(raw, dict):
                            continue
                        item = dict(raw)
                        if str(item.get("status") or "").strip().lower() == "pending":
                            pending_dt = _parse_iso_utc(
                                item.get("pending_since") or item.get("updated_at")
                            )
                            if pending_dt is None or pending_dt <= cutoff:
                                item["status"] = "failed"
                                item["error"] = "stale_pending_timeout"
                                item["updated_at"] = now_iso
                                changed = True
                                expired_count += 1
                        new_items.append(item)
                    if changed:
                        meta["chart_snapshots"] = new_items
                        await cur.execute(
                            """
                            UPDATE conversation_responses
                            SET metadata = %s::jsonb
                            WHERE conversation_response_id = %s
                            """,
                            (json.dumps(meta, ensure_ascii=False), rid),
                        )

    if expired_count:
        logger.info(
            "[channel_chart_snapshots] marked %d stale pending chart snapshot(s) as failed",
            expired_count,
        )
    return expired_count


# ---------------------------------------------------------------------------
# Cross-Process Read (own run + command_report linked source run)
# ---------------------------------------------------------------------------


def _extract_report_chart_layers(
    query_meta: Any,
    *,
    scrub_titles: bool = False,
) -> list[dict[str, Any]]:
    """Extract deduplicated ``chart_layers`` from query ``report_source`` and ``command_reports``."""
    if not isinstance(query_meta, Mapping):
        return []
    raw_lists: list[Any] = []
    report_source = query_meta.get("report_source")
    if isinstance(report_source, Mapping):
        raw_lists.append(report_source.get("chart_layers"))
    command_reports = query_meta.get("command_reports")
    if isinstance(command_reports, list):
        for item in command_reports:
            if isinstance(item, Mapping):
                raw_lists.append(item.get("chart_layers"))

    out: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for raw_list in raw_lists:
        if not isinstance(raw_list, list):
            continue
        for raw in raw_list:
            if not isinstance(raw, Mapping):
                continue
            cid = str(raw.get("chart_id") or "").strip()
            lid = str(raw.get("layer_id") or raw.get("id") or "").strip()
            if not cid or not lid:
                continue
            entry: dict[str, Any] = {
                "chart_id": cid,
                "layer_id": lid,
            }
            for field in ("workspace_id", "symbol", "timeframe"):
                val = raw.get(field)
                if isinstance(val, str) and val.strip():
                    entry[field] = val.strip()
            if not scrub_titles:
                title = raw.get("title")
                if isinstance(title, str) and title.strip():
                    entry["title"] = title.strip()
            key = (cid, lid)
            if key in index_by_key:
                out[index_by_key[key]] = entry
            else:
                index_by_key[key] = len(out)
                out.append(entry)
    return out


async def _load_query_metadata_for_run(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
) -> dict[str, Any]:
    await cur.execute(
        """
        SELECT q.metadata
        FROM conversation_responses r
        JOIN conversation_queries q
          ON q.conversation_thread_id = r.conversation_thread_id
         AND q.turn_index = r.turn_index
        WHERE r.conversation_response_id = %s
          AND r.conversation_thread_id = %s::uuid
        LIMIT 1
        """,
        (run_id, thread_id),
    )
    q_row = await cur.fetchone()
    if not q_row or not isinstance(q_row.get("metadata"), dict):
        return {}
    return dict(q_row["metadata"])


async def _load_command_report_linked_snapshots(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    query_meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """If ``run_id`` is a ``command_report`` turn, also fetch snapshots persisted on the command's launching run(s)."""
    q_meta = (
        dict(query_meta)
        if isinstance(query_meta, Mapping)
        else await _load_query_metadata_for_run(cur, thread_id=thread_id, run_id=run_id)
    )
    command_ids = _extract_report_command_ids(q_meta)
    if not command_ids:
        return []

    collected: list[dict[str, Any]] = []
    for command_id in command_ids:
        await cur.execute(
            """
            SELECT run_id::text AS source_run_id
            FROM command_runs
            WHERE command_id = %s
              AND conversation_thread_id = %s::uuid
            LIMIT 1
            """,
            (command_id, thread_id),
        )
        cmd_row = await cur.fetchone()
        source_run_id = str((cmd_row or {}).get("source_run_id") or "").strip()
        if not source_run_id or source_run_id == run_id:
            continue

        await cur.execute(
            """
            SELECT conversation_response_id, conversation_thread_id::text AS thread_id, metadata
            FROM conversation_responses
            WHERE conversation_response_id = %s
              AND conversation_thread_id = %s::uuid
            LIMIT 1
            """,
            (source_run_id, thread_id),
        )
        src_row = await cur.fetchone()
        if not src_row or is_run_row_outbound_blocked(dict(src_row)):
            continue
        src_meta = src_row.get("metadata") if isinstance(src_row.get("metadata"), dict) else {}
        raw_snaps = src_meta.get("chart_snapshots")
        if not isinstance(raw_snaps, list):
            continue
        collected.extend(
            dict(item)
            for item in raw_snaps
            if isinstance(item, dict) and str(item.get("command_id") or "") == command_id
        )
    return collected


async def get_run_end_chart_payload(
    *,
    run_id: str,
    thread_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Read ``outbound_blocked``, persisted ``chart_snapshots``, and report-back ``chart_layers`` for ``run_end``."""
    rid = str(run_id or "").strip()
    tid = str(thread_id or "").strip()
    if not rid:
        return {
            "run_id": rid,
            "thread_id": tid,
            "outbound_blocked": None,
            "chart_snapshots": [],
            "chart_layers": [],
        }

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT conversation_response_id, conversation_thread_id::text AS thread_id, metadata
                FROM conversation_responses
                WHERE conversation_response_id = %s
                """,
                (rid,),
            )
            run_row = await cur.fetchone()
            if not run_row:
                return {
                    "run_id": rid,
                    "thread_id": tid,
                    "outbound_blocked": None,
                    "chart_snapshots": [],
                    "chart_layers": [],
                }
            run_row = dict(run_row)
            actual_tid = str(run_row.get("thread_id") or "").strip()
            if tid and actual_tid != tid:
                return {
                    "run_id": rid,
                    "thread_id": tid,
                    "outbound_blocked": None,
                    "chart_snapshots": [],
                    "chart_layers": [],
                }

            blocked = is_run_row_outbound_blocked(run_row)
            q_meta = await _load_query_metadata_for_run(
                cur, thread_id=actual_tid, run_id=rid
            )
            report_layers = _extract_report_chart_layers(
                q_meta, scrub_titles=blocked
            )
            if blocked:
                return {
                    "run_id": rid,
                    "thread_id": actual_tid,
                    "outbound_blocked": True,
                    "chart_snapshots": [],
                    "chart_layers": report_layers,
                }

            meta = run_row.get("metadata") if isinstance(run_row.get("metadata"), dict) else {}
            own_items = meta.get("chart_snapshots")
            own_list = (
                [dict(item) for item in own_items if isinstance(item, dict)]
                if isinstance(own_items, list)
                else []
            )
            linked_list = await _load_command_report_linked_snapshots(
                cur, thread_id=actual_tid, run_id=rid, query_meta=q_meta
            )

    merged = _merge_chart_snapshot_entries(linked_list, own_list)
    return {
        "run_id": rid,
        "thread_id": actual_tid,
        "outbound_blocked": False,
        "chart_snapshots": merged,
        "chart_layers": report_layers,
    }


async def get_run_chart_snapshots_status(
    *,
    run_id: str,
    thread_id: str | None = None,
    platform: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Read authoritative Postgres ``chart_snapshots`` status for a run (including linked command_report runs)."""
    if platform:
        require_channel_platform_capability(platform)
    rid = str(run_id or "").strip()
    tid = str(thread_id or "").strip()
    if not rid:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_snapshot_request", "message": "run_id is required."},
        )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT conversation_response_id, conversation_thread_id::text AS thread_id, metadata
                FROM conversation_responses
                WHERE conversation_response_id = %s
                """,
                (rid,),
            )
            run_row = await cur.fetchone()
            if not run_row:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "snapshot_run_not_found", "message": "Run record not found."},
                )
            run_row = dict(run_row)
            actual_tid = str(run_row.get("thread_id") or "").strip()
            if tid and actual_tid != tid:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "snapshot_thread_mismatch",
                        "message": "run_id does not belong to the specified thread_id.",
                    },
                )
            if is_run_row_outbound_blocked(run_row):
                return {
                    "run_id": rid,
                    "thread_id": actual_tid,
                    "outbound_blocked": True,
                    "has_pending": False,
                    "snapshots": [],
                }

            meta = run_row.get("metadata") if isinstance(run_row.get("metadata"), dict) else {}
            own_items = meta.get("chart_snapshots")
            own_list = (
                [dict(item) for item in own_items if isinstance(item, dict)]
                if isinstance(own_items, list)
                else []
            )
            linked_list = await _load_command_report_linked_snapshots(
                cur, thread_id=actual_tid, run_id=rid
            )

    merged = _merge_chart_snapshot_entries(linked_list, own_list)
    has_pending = any(
        str(item.get("status") or "").strip().lower() == "pending" for item in merged
    )
    return {
        "run_id": rid,
        "thread_id": actual_tid,
        "outbound_blocked": False,
        "has_pending": has_pending,
        "snapshots": merged,
    }


def _extract_report_command_ids(query_meta: Any) -> list[str]:
    if not isinstance(query_meta, Mapping):
        return []
    out: list[str] = []
    report_source = query_meta.get("report_source")
    if isinstance(report_source, Mapping):
        if str(report_source.get("kind") or "").strip() == "command_report":
            cid = str(report_source.get("command_id") or "").strip()
            if cid:
                out.append(cid)
    command_reports = query_meta.get("command_reports")
    if isinstance(command_reports, list):
        for item in command_reports:
            if not isinstance(item, Mapping):
                continue
            cid = str(item.get("command_id") or "").strip()
            if cid and cid not in out:
                out.append(cid)
    return out


async def resolve_replay_chart_snapshots_by_run(
    *,
    thread_id: str,
    queries: Sequence[Mapping[str, Any]] | None,
    responses_by_turn: Mapping[int, Mapping[str, Any]] | None,
    conn: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve persisted ``chart_snapshots`` per run for ``/messages/replay`` ``replay_done``.

    Includes snapshots persisted on a background command's launching run when mapped onto the
    subsequent ``command_report`` turn's ``run_id``.
    """
    if not isinstance(responses_by_turn, Mapping) or not responses_by_turn:
        return {}

    query_meta_by_turn: dict[int, Mapping[str, Any]] = {}
    for q in queries or ():
        if not isinstance(q, Mapping):
            continue
        t_idx = q.get("turn_index")
        q_meta = q.get("metadata")
        if isinstance(t_idx, int) and isinstance(q_meta, Mapping):
            query_meta_by_turn[t_idx] = q_meta

    # Index non-blocked snapshots already loaded in memory by command_id so in-page
    # command_report runs resolve without an extra DB round-trip.
    in_mem_by_cmd: dict[str, list[dict[str, Any]]] = {}
    for resp_row in responses_by_turn.values():
        if not isinstance(resp_row, Mapping) or is_run_row_outbound_blocked(dict(resp_row)):
            continue
        resp_meta = resp_row.get("metadata")
        if not isinstance(resp_meta, Mapping):
            continue
        raw_cs = resp_meta.get("chart_snapshots")
        if not isinstance(raw_cs, list):
            continue
        for item in raw_cs:
            if not isinstance(item, Mapping):
                continue
            cmd_id = str(item.get("command_id") or "").strip()
            if cmd_id:
                in_mem_by_cmd.setdefault(cmd_id, []).append(dict(item))

    result: dict[str, list[dict[str, Any]]] = {}
    db_fallback_runs: list[tuple[str, list[dict[str, Any]]]] = []

    for turn_index, resp_row in responses_by_turn.items():
        if not isinstance(resp_row, Mapping) or is_run_row_outbound_blocked(dict(resp_row)):
            continue
        rid = str(
            resp_row.get("conversation_response_id") or resp_row.get("run_id") or ""
        ).strip()
        if not rid:
            continue
        resp_meta = resp_row.get("metadata") if isinstance(resp_row.get("metadata"), Mapping) else {}
        own_raw = resp_meta.get("chart_snapshots")
        own_list = (
            [dict(item) for item in own_raw if isinstance(item, Mapping)]
            if isinstance(own_raw, list)
            else []
        )

        linked_list: list[dict[str, Any]] = []
        missing_cmd_ids: list[str] = []
        for cmd_id in _extract_report_command_ids(query_meta_by_turn.get(turn_index)):
            found_mem = in_mem_by_cmd.get(cmd_id)
            if found_mem:
                linked_list.extend(dict(item) for item in found_mem)
            else:
                missing_cmd_ids.append(cmd_id)

        if missing_cmd_ids:
            db_fallback_runs.append((rid, own_list))
            if linked_list or own_list:
                result[rid] = _merge_chart_snapshot_entries(linked_list, own_list)
        else:
            merged = _merge_chart_snapshot_entries(linked_list, own_list)
            if merged:
                result[rid] = merged

    if db_fallback_runs and thread_id:
        try:
            async with get_db_connection(conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    for rid, own_list in db_fallback_runs:
                        db_linked = await _load_command_report_linked_snapshots(
                            cur,
                            thread_id=str(thread_id),
                            run_id=rid,
                        )
                        merged = _merge_chart_snapshot_entries(
                            db_linked,
                            result.get(rid, own_list),
                        )
                        if merged:
                            result[rid] = merged
        except Exception:
            logger.debug(
                "[channel_chart_snapshots] replay DB fallback for command_report snapshots failed",
                exc_info=True,
            )

    return result


# ---------------------------------------------------------------------------
# Main-Loop Async OHLCV Prefetch & Pure In-Memory Sync Closure
# ---------------------------------------------------------------------------


def _extract_layer_time_bounds(layer_body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract earliest and latest YYYY-MM-DD dates referenced anywhere in a validated layer body."""
    dates: list[str] = []

    def _collect(val: Any) -> None:
        if isinstance(val, str):
            s = val.strip()
            if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-" and s[:4].isdigit():
                dates.append(s[:10])
        elif isinstance(val, Mapping):
            for k, v in val.items():
                if k in ("time", "from", "to", "until", "as_of", "source_start", "source_end"):
                    _collect(v)
                elif isinstance(v, (Mapping, list)):
                    _collect(v)
        elif isinstance(val, list):
            for item in val:
                _collect(item)

    _collect(layer_body)
    if not dates:
        return None, None
    dates.sort()
    return dates[0], dates[-1]


def _epoch_ms_to_iso_date(ms_val: Any, *, daily: bool) -> str | None:
    if isinstance(ms_val, str) and ms_val.strip():
        return ms_val.strip()
    if not isinstance(ms_val, (int, float)):
        return None
    dt = datetime.fromtimestamp(float(ms_val) / 1000.0, tz=UTC)
    if daily:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _convert_cache_points_to_chartkit_rows(
    raw_data: Sequence[Mapping[str, Any]] | None,
    *,
    daily: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pt in raw_data or ():
        if not isinstance(pt, Mapping):
            continue
        date_str = pt.get("date") or _epoch_ms_to_iso_date(pt.get("time"), daily=daily)
        if not date_str:
            continue
        rows.append(
            {
                "date": str(date_str),
                "open": float(pt.get("open", 0.0)),
                "high": float(pt.get("high", 0.0)),
                "low": float(pt.get("low", 0.0)),
                "close": float(pt.get("close", 0.0)),
                "volume": float(pt.get("volume", 0.0) or 0.0),
            }
        )
    return rows


async def prefetch_market_bars_for_layer(
    *,
    symbol: str,
    timeframe: str,
    user_id: str,
    layer_body: Mapping[str, Any],
) -> dict[str, Any]:
    """Prefetch OHLCV bars on the backend's main asyncio event loop for ``user_id``.

    Aligns with ``src/server/app/market_data.py`` (``DataReadUserId``) using
    ``DailyCacheService`` for ``1day`` and ``IntradayCacheService`` for intraday intervals.
    """
    from src.server.app.market_data import _series_meta, _series_symbol
    from src.server.services.cache.daily_cache_service import DailyCacheService
    from src.server.services.cache.intraday_cache_service import IntradayCacheService

    tf = str(timeframe or "1day").strip()
    daily = tf == "1day"
    is_index = str(symbol or "").strip().startswith("^")

    try:
        cache_symbol = _series_symbol(symbol, is_index=is_index)
    except Exception:
        cache_symbol = str(symbol or "").strip().upper()

    today = _utc_now().date()
    earliest_str, latest_str = _extract_layer_time_bounds(layer_body)
    if daily:
        default_from = today - timedelta(days=_DAILY_LOOKBACK_DAYS)
        from_date = default_from.strftime("%Y-%m-%d")
        if earliest_str:
            try:
                earliest_dt = datetime.strptime(earliest_str, "%Y-%m-%d").date() - timedelta(days=90)
                if earliest_dt < default_from:
                    from_date = earliest_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        to_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        if latest_str and latest_str > to_date:
            to_date = latest_str
        service = DailyCacheService.get_instance()
        result = await service.get_stock_daily(
            symbol=cache_symbol,
            from_date=from_date,
            to_date=to_date,
            is_index=is_index,
            user_id=user_id,
        )
    else:
        lookback_days = _INTRADAY_LOOKBACK_DAYS.get(tf, 60)
        default_from = today - timedelta(days=lookback_days)
        from_date = default_from.strftime("%Y-%m-%d")
        if earliest_str:
            try:
                earliest_dt = datetime.strptime(earliest_str, "%Y-%m-%d").date() - timedelta(days=5)
                if earliest_dt < default_from:
                    from_date = earliest_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        to_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        service = IntradayCacheService.get_instance()
        fetch_fn = service.get_index_intraday if is_index else service.get_stock_intraday
        result = await fetch_fn(
            symbol=cache_symbol,
            interval=tf,
            from_date=from_date,
            to_date=to_date,
            user_id=user_id,
        )

    if getattr(result, "error", None):
        return {
            "error": "market_data_error",
            "detail": str(result.error),
            "data": [],
            "timezone": None,
            "source": None,
            "currency": None,
        }

    meta = _series_meta(cache_symbol, result, is_index=is_index, equity=not is_index)
    rows = _convert_cache_points_to_chartkit_rows(getattr(result, "data", None), daily=daily)
    return {
        "symbol": str(symbol or cache_symbol).strip().upper(),
        "data": rows,
        "timezone": meta.fields.get("timezone") or "America/New_York",
        "source": meta.source or "cache",
        "currency": meta.fields.get("currency"),
    }


def make_backend_market_bars_tool(
    prefetched: Mapping[str, Any],
) -> Callable[..., dict[str, Any]]:
    """Wrap prefetched OHLCV payload into a pure in-memory synchronous closure for ``ChartBars``.

    Safe to call from ``chartkit-bars`` daemon threads without touching any asyncio event loop.
    """
    frozen_rows = [dict(r) for r in (prefetched.get("data") or ())]
    base_meta = {
        "symbol": prefetched.get("symbol"),
        "timezone": prefetched.get("timezone") or "America/New_York",
        "source": prefetched.get("source") or "cache",
        "currency": prefetched.get("currency"),
    }
    err = prefetched.get("error")
    err_detail = prefetched.get("detail")

    def backend_market_bars_tool(
        *,
        symbol: str | None = None,
        interval: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        _ = (symbol, interval)
        if err:
            return {"error": err, "detail": err_detail}
        start_day = str(start_date or "").strip()[:10]
        end_day = str(end_date or "").strip()[:10]
        if start_day or end_day:
            filtered = [
                dict(r)
                for r in frozen_rows
                if (not start_day or str(r.get("date", ""))[:10] >= start_day)
                and (not end_day or str(r.get("date", ""))[:10] <= end_day)
            ]
        else:
            filtered = [dict(r) for r in frozen_rows]
        return {**base_meta, "data": filtered}

    return backend_market_bars_tool


# ---------------------------------------------------------------------------
# Background Snapshot Rendering & Upload
# ---------------------------------------------------------------------------


def _build_snapshot_job(
    *,
    ref: LayerRef,
    layer_body: Mapping[str, Any],
    out_path: str,
    prefetched: Mapping[str, Any],
    width: int = DEFAULT_SNAPSHOT_WIDTH,
    height: int = DEFAULT_SNAPSHOT_HEIGHT,
    theme: str = DEFAULT_SNAPSHOT_THEME,
) -> Any:
    from chartkit._bars_source import ChartBars
    from chartkit._renderer import SnapshotJob
    from chartkit._view import Frame

    tz = str(prefetched.get("timezone") or "America/New_York")
    bars_tool = make_backend_market_bars_tool(prefetched)
    bars = ChartBars(
        symbol=ref.symbol,
        tf=ref.timeframe,
        tz=tz,
        df_rows=None,
        tool=bars_tool,
    )
    return SnapshotJob(
        symbol=ref.symbol,
        timeframe=ref.timeframe,
        tz=tz,
        layer=dict(layer_body),
        path=out_path,
        theme=theme,
        width=width,
        height=height,
        bars=bars,
        frame=Frame(kind="layer", layer_id=ref.layer_id),
        currency=prefetched.get("currency"),
    )


async def render_and_store_chart_snapshot(
    *,
    thread_id: str,
    run_id: str,
    user_id: str,
    ref: LayerRef,
    layer_body: Mapping[str, Any],
    registration_id: str | None = None,
    width: int = DEFAULT_SNAPSHOT_WIDTH,
    height: int = DEFAULT_SNAPSHOT_HEIGHT,
    render_timeout_s: float = CHANNEL_SNAPSHOT_RENDER_TIMEOUT_S,
    lock_wait_timeout_s: float = CHANNEL_SNAPSHOT_LOCK_WAIT_TIMEOUT_S,
    prefetch_fn: Callable[..., Any] | None = None,
    render_fn: Callable[..., Any] | None = None,
    put_bytes_fn: Callable[..., bool] | None = None,
    slot_ctx_fn: Callable[..., Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Prefetch bars on main loop, render PNG under deployment-wide lock, upload, and update Postgres.

    Never raises: any error transitions the Postgres entry to ``status='failed'`` so main flows
    and gateways always degrade cleanly to web interactive deep links.
    """
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return {"status": "failed", "error": "channel_gateway_disabled"}

    if put_bytes_fn is None and not is_snapshot_storage_configured():
        await update_chart_snapshot_status(
            run_id=rid,
            chart_id=ref.chart_id,
            layer_id=ref.layer_id,
            status="failed",
            error="storage_not_configured",
            expected_registration_id=registration_id,
            conn=conn,
        )
        return {"status": "failed", "error": "storage_not_configured"}

    fetch = prefetch_fn or prefetch_market_bars_for_layer
    put_fn = put_bytes_fn or put_snapshot_bytes
    slot_cm = slot_ctx_fn or acquire_snapshot_render_slot

    tmp_path: str | None = None
    try:
        # 1. Main-loop async OHLCV prefetch using owner user_id
        prefetched = await fetch(
            symbol=ref.symbol,
            timeframe=ref.timeframe,
            user_id=user_id,
            layer_body=layer_body,
        )

        # 2. Render PNG under process semaphore + Postgres advisory lock slot
        fd, tmp_path = tempfile.mkstemp(prefix="dalton_chart_", suffix=".png")
        os.close(fd)

        async with slot_cm(timeout_s=lock_wait_timeout_s):
            if render_fn is not None:
                render_task = asyncio.create_task(
                    render_fn(
                        ref=ref,
                        layer_body=layer_body,
                        out_path=tmp_path,
                        prefetched=prefetched,
                        width=width,
                        height=height,
                        timeout_s=render_timeout_s,
                    )
                )
                try:
                    rendered = await asyncio.shield(render_task)
                except asyncio.CancelledError:
                    render_task.cancel()
                    while not render_task.done():
                        try:
                            await asyncio.shield(render_task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    raise
            else:
                from chartkit._renderer import RENDERER

                job = _build_snapshot_job(
                    ref=ref,
                    layer_body=layer_body,
                    out_path=tmp_path,
                    prefetched=prefetched,
                    width=width,
                    height=height,
                )
                cancel_event = threading.Event()
                render_task = asyncio.create_task(
                    asyncio.to_thread(
                        RENDERER.run,
                        job,
                        timeout_s=render_timeout_s,
                        cancel_event=cancel_event,
                    )
                )
                try:
                    rendered = await asyncio.shield(render_task)
                except asyncio.CancelledError:
                    cancel_event.set()
                    while not render_task.done():
                        try:
                            await asyncio.shield(render_task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    raise

        if not tmp_path or not os.path.isfile(tmp_path):
            raise RuntimeError("snapshot PNG file was not written by renderer")
        with open(tmp_path, "rb") as f:
            png_bytes = f.read()
        if not png_bytes or not png_bytes.startswith(_PNG_MAGIC):
            raise RuntimeError("rendered output is not a valid PNG image")

        sha256_hex = hashlib.sha256(png_bytes).hexdigest()
        snapshot_key = build_snapshot_key(tid, sha256_hex, ref.layer_id)
        uploaded = await asyncio.to_thread(
            put_fn, snapshot_key, png_bytes, content_type="image/png"
        )
        if not uploaded:
            raise RuntimeError("object storage upload returned False")

        r_size = getattr(rendered, "size", None)
        out_w = int(r_size[0]) if isinstance(r_size, (tuple, list)) and len(r_size) >= 2 and r_size[0] else width
        out_h = int(r_size[1]) if isinstance(r_size, (tuple, list)) and len(r_size) >= 2 and r_size[1] else height

        updated = await update_chart_snapshot_status(
            run_id=rid,
            chart_id=ref.chart_id,
            layer_id=ref.layer_id,
            status="ready",
            snapshot_key=snapshot_key,
            sha256=sha256_hex,
            size_bytes=len(png_bytes),
            width=out_w,
            height=out_h,
            expected_registration_id=registration_id,
            conn=conn,
        )
        if not updated:
            return {
                "status": "stale_ignored",
                "snapshot_key": snapshot_key,
                "sha256": sha256_hex,
            }
        return {
            "status": "ready",
            "snapshot_key": snapshot_key,
            "sha256": sha256_hex,
            "size_bytes": len(png_bytes),
            "width": out_w,
            "height": out_h,
        }
    except SnapshotRenderSlotTimeoutError as exc:
        logger.warning(
            "[channel_chart_snapshots] render slot timeout for run=%s layer=%s: %s",
            rid,
            ref.layer_id,
            exc,
        )
        await _safe_mark_failed(
            rid,
            ref.chart_id,
            ref.layer_id,
            "render_slot_timeout",
            registration_id=registration_id,
            conn=conn,
        )
        return {"status": "failed", "error": "render_slot_timeout"}
    except Exception as exc:  # noqa: BLE001 - isolated from publish/settle main path
        err_msg = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[channel_chart_snapshots] snapshot render failed for run=%s layer=%s: %s",
            rid,
            ref.layer_id,
            err_msg,
        )
        await _safe_mark_failed(
            rid,
            ref.chart_id,
            ref.layer_id,
            err_msg,
            registration_id=registration_id,
            conn=conn,
        )
        return {"status": "failed", "error": err_msg}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _safe_mark_failed(
    run_id: str,
    chart_id: str,
    layer_id: str,
    error: str,
    *,
    registration_id: str | None = None,
    conn: Any = None,
) -> None:
    try:
        await update_chart_snapshot_status(
            run_id=run_id,
            chart_id=chart_id,
            layer_id=layer_id,
            status="failed",
            error=error,
            expected_registration_id=registration_id,
            conn=conn,
        )
    except Exception:
        logger.debug(
            "[channel_chart_snapshots] failed to persist failed status for run=%s layer=%s",
            run_id,
            layer_id,
            exc_info=True,
        )


def schedule_chart_snapshots_render(
    *,
    thread_id: str,
    run_id: str,
    user_id: str,
    bodies: Sequence[tuple[LayerRef, Mapping[str, Any]]],
    registered_entries: Sequence[Mapping[str, Any]] | None = None,
    max_per_turn: int = CHANNEL_SNAPSHOT_MAX_PER_TURN,
) -> asyncio.Task[list[dict[str, Any]]] | None:
    """Spawn a background asyncio task to render up to ``max_per_turn`` chart layers sequentially."""
    if not bodies or not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return None
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    uid = str(user_id or "").strip()
    if not tid or not rid or not uid:
        return None

    reg_by_key: dict[tuple[str, str], str] = {}
    for entry in registered_entries or ():
        if not isinstance(entry, Mapping):
            continue
        cid = str(entry.get("chart_id") or "").strip()
        lid = str(entry.get("layer_id") or "").strip()
        reg_id = str(entry.get("registration_id") or "").strip()
        if cid and lid and reg_id:
            reg_by_key[(cid, lid)] = reg_id

    selected = [(ref, dict(body)) for ref, body in bodies[:max_per_turn]]

    async def _run_batch() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for ref, body in selected:
            res = await render_and_store_chart_snapshot(
                thread_id=tid,
                run_id=rid,
                user_id=uid,
                ref=ref,
                layer_body=body,
                registration_id=reg_by_key.get((str(ref.chart_id), str(ref.layer_id))),
            )
            results.append(res)
        return results

    task = asyncio.create_task(
        _run_batch(),
        name=f"dalton-chart-snapshots-{rid[:12]}",
    )
    _BACKGROUND_RENDER_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_RENDER_TASKS.discard)
    return task


# ---------------------------------------------------------------------------
# Authenticated DM-Only Snapshot Download
# ---------------------------------------------------------------------------


async def _verify_owner_active_dm_binding(
    cur: Any,
    *,
    owner_user_id: str,
    platform: str,
    target_address: str,
    run_id: str,
    thread_id: str,
    delivery_key: str | None = None,
    delivery_attempt: int | None = None,
    owned_conn: Any,
) -> None:
    """Verify ``target_address`` is a DM belonging to ``owner_user_id``'s active bound account.

    - Explicit DM targets (``<platform>:user:<id>`` / ``<platform>:dm:<id>``) must match the
      owner's current active binding (``dalton_channel_weixin_accounts.status='active'`` for
      Weixin; ``dalton_channel_identities`` for Feishu/Discord).
    - Canonical ``<platform>:@me`` targets MUST specify ``delivery_key`` and ``delivery_attempt``
      matching that exact ``in_flight`` DM delivery for this ``run_id`` with a non-empty pinned
      ``dm_recipient_platform_user_id`` (and for Weixin, non-empty matching ``weixin_account_id``
      and ``weixin_bot_generation``) matching the owner's active binding.
    """
    if not _is_dm_target_address(platform, target_address):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "snapshot_dm_only",
                "message": "Chart snapshots are restricted to direct messages with the thread owner.",
            },
        )

    roster_st = await cg_db.check_team_member_status_live(owner_user_id, conn=owned_conn)
    if roster_st == "lookup_error":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "roster_lookup_error",
                "retryable": True,
                "message": "Team member roster lookup failed.",
            },
        )
    if roster_st != "active":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "snapshot_recipient_denied",
                "message": "Thread owner is no longer active in the team roster.",
            },
        )

    explicit_recipient = _extract_explicit_dm_recipient_id(platform, target_address)
    active_bound_uid = ""
    active_wx_account_id: str | None = None
    active_wx_bot_gen: int | None = None

    if platform == "weixin":
        wx_user_tier = await cg_db.get_user_tier(owner_user_id, conn=owned_conn)
        wx_cfg = await wx_db.ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
        wx_acc_ok, _wx_deny = wx_db.evaluate_weixin_member_access(
            wx_user_tier,
            channel_enabled=bool(wx_cfg["enabled"]),
            db_allow_external=wx_cfg.get("allow_external"),
        )
        if not wx_acc_ok:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Thread owner does not have active Weixin channel access.",
                },
            )
        await cur.execute(
            """
            SELECT account_id::text AS account_id,
                   user_id,
                   ilink_user_id,
                   bot_generation,
                   status
            FROM dalton_channel_weixin_accounts
            WHERE user_id = %s
            LIMIT 1
            """,
            (owner_user_id,),
        )
        wx_row = await cur.fetchone()
        if (
            not wx_row
            or str(wx_row.get("status") or "").strip().lower() != "active"
            or not str(wx_row.get("ilink_user_id") or "").strip()
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Thread owner has no active Weixin DM binding.",
                },
            )
        active_bound_uid = str(wx_row["ilink_user_id"]).strip()
        active_wx_account_id = str(wx_row.get("account_id") or "").strip() or None
        if wx_row.get("bot_generation") is not None:
            active_wx_bot_gen = int(wx_row["bot_generation"])
    else:
        # Feishu / Discord: must match dalton_channel_identities active binding for owner_user_id
        await cur.execute(
            """
            SELECT platform_user_id
            FROM dalton_channel_identities
            WHERE user_id = %s AND platform = %s
            LIMIT 1
            """,
            (owner_user_id, platform),
        )
        ident = await cur.fetchone()
        active_bound_uid = str((ident or {}).get("platform_user_id") or "").strip()
        if not active_bound_uid:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": f"Thread owner has no active {platform} binding.",
                },
            )

    if explicit_recipient is not None and explicit_recipient != active_bound_uid:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "snapshot_recipient_denied",
                "message": "Target DM recipient does not match thread owner's active binding.",
            },
        )

    def _delivery_pinned_uid(row: Mapping[str, Any]) -> str:
        if platform == "weixin":
            return str(
                row.get("dm_recipient_platform_user_id")
                or row.get("weixin_recipient_id")
                or ""
            ).strip()
        return str(row.get("dm_recipient_platform_user_id") or "").strip()

    def _delivery_matches_active_binding(row: Mapping[str, Any]) -> bool:
        if str(row.get("status") or "").strip().lower() != "in_flight":
            return False
        if str(row.get("run_id") or "").strip() != run_id:
            return False
        if str(row.get("user_id") or "").strip() != owner_user_id:
            return False
        if str(row.get("platform") or "").strip().lower() != platform:
            return False
        row_tid = str(row.get("thread_id") or "").strip().lower()
        if row_tid and thread_id and row_tid != thread_id.strip().lower():
            return False
        d_kind = str(row.get("kind") or "").strip().lower()
        is_allowed_kind = (
            d_kind in ("agent_send", "fallback") and bool(row.get("is_current"))
        ) or (d_kind == "automation" and bool(row.get("execution_id")))
        if not is_allowed_kind:
            return False
        if not _is_dm_target_address(platform, str(row.get("target_address") or "")):
            return False
        pinned = _delivery_pinned_uid(row)
        if not pinned or pinned != active_bound_uid:
            return False
        if platform == "weixin":
            d_aid = str(row.get("weixin_account_id") or "").strip() or None
            if not d_aid or not active_wx_account_id or d_aid != active_wx_account_id:
                return False
            d_gen = row.get("weixin_bot_generation")
            if (
                d_gen is None
                or active_wx_bot_gen is None
                or int(d_gen) != active_wx_bot_gen
            ):
                return False
        return True

    cleaned_delivery_key = str(delivery_key or "").strip()
    parsed_attempt: int | None = None
    if isinstance(delivery_attempt, bool):
        parsed_attempt = -1
    elif delivery_attempt is not None:
        try:
            parsed_attempt = int(delivery_attempt)
        except (TypeError, ValueError):
            parsed_attempt = -1

    if explicit_recipient is None:
        # Canonical `<platform>:@me`: MUST identify the exact in-flight DM delivery attempt
        # for this run whose pinned recipient (and Weixin account/generation) matches active binding.
        if not cleaned_delivery_key or parsed_attempt is None or parsed_attempt <= 0:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "delivery_key and delivery_attempt are required for @me snapshot download.",
                },
            )
        await cur.execute(
            """
            SELECT delivery_key, kind, is_current, user_id,
                   thread_id::text AS thread_id, run_id, platform,
                   target_address, status, delivery_attempt,
                   execution_id::text AS execution_id,
                   dm_recipient_platform_user_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_bot_generation,
                   weixin_recipient_id
            FROM dalton_channel_deliveries
            WHERE delivery_key = %s
            LIMIT 1
            """,
            (cleaned_delivery_key,),
        )
        delivery_row = await cur.fetchone()
        if not delivery_row:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Matching in-flight DM delivery not found for @me download.",
                },
            )
        d_row = dict(delivery_row)
        row_attempt_raw = d_row.get("delivery_attempt")
        row_attempt = int(row_attempt_raw) if row_attempt_raw is not None else None
        if row_attempt != parsed_attempt or not _delivery_matches_active_binding(d_row):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Delivery attempt or pinned DM recipient does not match owner's active binding.",
                },
            )
    elif cleaned_delivery_key:
        await cur.execute(
            """
            SELECT delivery_key, kind, is_current, user_id,
                   thread_id::text AS thread_id, run_id, platform,
                   target_address, status, delivery_attempt,
                   execution_id::text AS execution_id,
                   dm_recipient_platform_user_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_bot_generation,
                   weixin_recipient_id
            FROM dalton_channel_deliveries
            WHERE delivery_key = %s
            LIMIT 1
            """,
            (cleaned_delivery_key,),
        )
        delivery_row = await cur.fetchone()
        if not delivery_row:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Specified delivery record not found.",
                },
            )
        d_row = dict(delivery_row)
        row_attempt_raw = d_row.get("delivery_attempt")
        row_attempt = int(row_attempt_raw) if row_attempt_raw is not None else None
        if (
            (parsed_attempt is not None and row_attempt != parsed_attempt)
            or not _delivery_matches_active_binding(d_row)
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "snapshot_recipient_denied",
                    "message": "Specified DM delivery attempt does not match owner's active binding.",
                },
            )
    else:
        # Explicit recipient without delivery_key: already verified against active binding;
        # still reject if any current in-flight delivery on this run pinned a different recipient.
        await cur.execute(
            """
            SELECT delivery_key, kind, is_current, target_address, status,
                   execution_id::text AS execution_id,
                   dm_recipient_platform_user_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_bot_generation,
                   weixin_recipient_id
            FROM dalton_channel_deliveries
            WHERE run_id = %s
              AND user_id = %s
              AND platform = %s
              AND status = 'in_flight'
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (run_id, owner_user_id, platform),
        )
        delivery_rows = await cur.fetchall() or []
        for d_row in delivery_rows:
            pinned = _delivery_pinned_uid(d_row)
            if pinned and pinned != active_bound_uid:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "snapshot_recipient_denied",
                        "message": "Pinned DM delivery recipient no longer matches owner's active binding.",
                    },
                )


async def download_verified_chart_snapshot(
    *,
    snapshot_key: str,
    run_id: str,
    target_address: str,
    delivery_key: str | None = None,
    delivery_attempt: int | None = None,
    gateway_epoch: int | str | None = None,
    conn: Any = None,
    get_bytes_fn: Callable[[str], bytes | None] | None = None,
) -> tuple[bytes, str, str]:
    """Authenticate and serve a DM-only PNG chart snapshot.

    Enforces:
    1. Strict regex validation on ``snapshot_key`` (blocking traversal and encoding bypasses).
    2. Platform resolution from ``target_address`` + ``require_channel_platform_capability``.
    3. DM-only ``target_address`` check (group targets always denied with 403).
    4. Thread/run consistency + ``is_run_row_outbound_blocked == False``.
    5. Snapshot entry present in run's persisted ``chart_snapshots`` (or linked ``command_report``)
       with ``status == "ready"`` and matching ``snapshot_key`` / ``sha256``.
    6. Thread owner's active roster & platform DM binding match ``target_address``.
    7. Object bytes fetched from storage match PNG magic bytes and recorded ``sha256``.
    """
    parsed = parse_snapshot_key(snapshot_key)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_snapshot_key",
                "message": "Snapshot key failed strict format validation.",
            },
        )
    key_thread_id, _key_sha16, safe_layer = parsed

    cleaned_run_id = str(run_id or "").strip()
    cleaned_target = str(target_address or "").strip()
    if not cleaned_run_id or not cleaned_target:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_snapshot_request",
                "message": "run_id and target_address are required.",
            },
        )

    platform = _infer_platform_from_target(cleaned_target)
    if platform is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_snapshot_target",
                "message": "Unable to determine channel platform from target_address.",
            },
        )
    require_channel_platform_capability(platform)

    if not _is_dm_target_address(platform, cleaned_target):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "snapshot_dm_only",
                "message": "Chart snapshots are only available for direct messages.",
            },
        )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                parsed_epoch: int | None = None
                if isinstance(gateway_epoch, bool):
                    raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
                elif isinstance(gateway_epoch, int):
                    if gateway_epoch <= 0:
                        raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
                    parsed_epoch = gateway_epoch
                elif gateway_epoch is not None and str(gateway_epoch).strip():
                    try:
                        parsed_epoch = int(str(gateway_epoch).strip())
                    except ValueError as exc:
                        raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header") from exc
                    if parsed_epoch <= 0:
                        raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header")

                if platform == "weixin":
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=True,
                        platform="weixin",
                    )
                elif parsed_epoch is not None:
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=True,
                        platform=platform,
                        enforce_post_lease_for_non_weixin=False,
                    )

                await cur.execute(
                    """
                    SELECT r.conversation_response_id,
                           r.conversation_thread_id::text AS thread_id,
                           r.metadata,
                           t.user_id AS owner_user_id
                    FROM conversation_responses r
                    JOIN conversation_threads t
                      ON t.conversation_thread_id = r.conversation_thread_id
                    WHERE r.conversation_response_id = %s
                    """,
                    (cleaned_run_id,),
                )
                run_row = await cur.fetchone()
                if not run_row:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "snapshot_not_found", "message": "Run record not found."},
                    )
                run_row = dict(run_row)
                if str(run_row.get("thread_id") or "").strip().lower() != key_thread_id:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "snapshot_thread_mismatch",
                            "message": "Snapshot thread_id does not match the run's thread.",
                        },
                    )
                if is_run_row_outbound_blocked(run_row):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "snapshot_outbound_blocked",
                            "message": "Run is outbound-blocked for chart snapshot delivery.",
                        },
                    )

                owner_user_id = str(run_row.get("owner_user_id") or "").strip()
                if not owner_user_id:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "snapshot_recipient_denied",
                            "message": "Thread owner could not be verified.",
                        },
                    )

                await _verify_owner_active_dm_binding(
                    cur,
                    owner_user_id=owner_user_id,
                    platform=platform,
                    target_address=cleaned_target,
                    run_id=cleaned_run_id,
                    thread_id=key_thread_id,
                    delivery_key=delivery_key,
                    delivery_attempt=delivery_attempt,
                    owned_conn=owned_conn,
                )

                meta = run_row.get("metadata") if isinstance(run_row.get("metadata"), dict) else {}
                own_items = meta.get("chart_snapshots")
                own_list = (
                    [dict(item) for item in own_items if isinstance(item, dict)]
                    if isinstance(own_items, list)
                    else []
                )
                linked_list = await _load_command_report_linked_snapshots(
                    cur, thread_id=key_thread_id, run_id=cleaned_run_id
                )

    all_snaps = _merge_chart_snapshot_entries(linked_list, own_list)
    matched: dict[str, Any] | None = None
    for item in all_snaps:
        if str(item.get("snapshot_key") or "").strip() == snapshot_key:
            matched = item
            break

    if matched is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "snapshot_not_found",
                "message": "Snapshot key is not registered on this run.",
            },
        )
    if str(matched.get("status") or "").strip().lower() != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_not_ready",
                "status": str(matched.get("status") or "pending"),
                "message": "Snapshot is not ready for download.",
            },
        )

    expected_sha256 = str(matched.get("sha256") or "").strip().lower()
    if not expected_sha256 or len(expected_sha256) != 64:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_integrity_mismatch",
                "message": "Snapshot metadata is missing a valid sha256 digest.",
            },
        )

    reader = get_bytes_fn or get_snapshot_bytes
    raw_bytes = await asyncio.to_thread(reader, snapshot_key)
    if not raw_bytes:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "snapshot_blob_missing",
                "message": "Snapshot image is missing from object storage.",
            },
        )
    if not raw_bytes.startswith(_PNG_MAGIC):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_integrity_mismatch",
                "message": "Stored snapshot object is not a valid PNG.",
            },
        )
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_integrity_mismatch",
                "message": "Stored snapshot sha256 does not match persisted metadata.",
            },
        )

    filename = f"chart_{safe_layer}.png"
    return raw_bytes, "image/png", filename
