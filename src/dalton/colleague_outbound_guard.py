"""Analyst-only outbound guard & workspace-level ``pending_outbound_confirm`` pause (§7.3).

Design invariants:
- Applies **only** to ``role == "analyst"``; Chief of Staff existing behavior
  (such as copying to Home and sending) is completely unchanged.
- An analyst turn enters the outbound-blocked state:
  1. Immediately at START for a cross-user turn (``user_id != workspace_owner_id``), OR
  2. Immediately BEFORE reading/copying from another workspace on an own-user turn
     (``colleague_workspace`` ``read_file`` / ``read_thread`` / ``copy_file``, or
     cross-workspace ``knowledge_read``).
- At that exact entry point — BEFORE any colleague content is read or copied —
  Postgres durably records BOTH:
  - ``conversation_responses.metadata["colleague_outbound_blocked"] = true``, and
  - ``workspaces.config["pending_outbound_confirm"] = {"run_id": ..., "marked_at": ..., "reason": ...}``
    (protected via ``SERVER_OWNED_CONFIG_KEYS``).
  If persisting fails, the cross-user START or colleague tool action is refused.
- While ``pending_outbound_confirm`` is set on a workspace, ALL automatic
  ``keep_updated`` publications (``keep_updated_after_turn``,
  ``keep_updated_after_change``, ``republish_workspace_monitors_after_turn``)
  are paused — enforced both before snapshot capture AND inside the version
  commit transactions (``turn_version`` and ``commit_monitor_version_tx``) via
  a ``FOR SHARE`` read on the ``workspaces`` row.
- Only explicit confirmation/publish by the workspace owner clears
  ``pending_outbound_confirm`` and resumes automatic publishing.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)

OUTBOUND_BLOCKED_METADATA_KEY = "colleague_outbound_blocked"
PENDING_OUTBOUND_CONFIRM_CONFIG_KEY = "pending_outbound_confirm"

OUTBOUND_GUARD_REFUSAL_MESSAGE = (
    "安全策略限制：当前分析师轮次为跨用户协作或已读取/复制其他工作区的外部内容，"
    "为防止间接提示注入导致外发泄露，本轮禁止对外发送消息、写入飞书文档、"
    "提交定时报告或发布/更新监控面板。请仅在当前工作区内整理结果并向用户汇报，"
    "由工作区主人在确认内容与可见范围后的新轮次中手动执行外发或发布。"
)

OUTBOUND_GUARD_PERSIST_ERROR = (
    "安全策略限制：无法将当前分析师轮次的外发限制与工作区待确认暂停标记持久化到数据库，"
    "已拒绝本次跨工作区读取/复制操作，请稍后重试。"
)

PENDING_CONFIRM_REQUIRED_MESSAGE = (
    "当前工作区因跨用户协作或读取其他工作区内容，已暂停自动发布并标记为「待确认」。"
    "请先向工作区主人展示最新变更内容并确认可见范围，获得明确同意后再传入 "
    "user_confirmed_scope=true 执行本次发布/更新；"
    "若要解除工作区自动发布暂停，请由工作区主人在网页端手动发布或重新发布。"
)

ACTIVE_BLOCKED_RUN_CLEAR_NOTICE = "仍有进行中的同事协作轮次，结束后再确认"
PAUSE_TOKEN_MISMATCH_NOTICE = "工作区期间发生了新的同事协作或跨区读取，已保留自动发布暂停，请刷新后重新确认"
PAUSE_TOKEN_MISSING_NOTICE = "缺少当前暂停确认标识，已保留自动发布暂停，请刷新后在网页端重新确认"
NON_SESSION_CLEAR_FORBIDDEN_DETAIL = "请由工作区主人在网页端登录状态下手动发布以解除自动发布暂停"


class WorkspaceAutoPublishPaused(Exception):
    """Raised inside a commit transaction when ``pending_outbound_confirm`` blocks auto/legacy publish."""


def is_interactive_owner_session(
    request: Any | None,
    *,
    expected_user_id: str | None = None,
) -> bool:
    """Return True ONLY for an interactive user web session (never service token / delegated auth)."""
    if request is None:
        return False
    headers = getattr(request, "headers", None)
    if headers is not None and (
        headers.get("X-Service-Token") or headers.get("X-User-Id")
    ):
        return False
    scope = getattr(request, "scope", None)
    if not isinstance(scope, Mapping):
        return False
    from src.server.utils.api import caller_of

    caller = caller_of(scope)
    if caller is None or caller.auth != "session":
        return False
    if expected_user_id is not None and str(caller.user_id) != str(expected_user_id):
        return False
    return True


async def get_interactive_session_flag(
    request: Any,
    user_id: str,
) -> bool:
    """Shared helper for FastAPI dependencies verifying an interactive owner session."""
    return is_interactive_owner_session(request, expected_user_id=user_id)


ClearPauseStatus = Literal[
    "cleared",
    "not_paused",
    "active_blocked_run",
    "token_mismatch",
    "token_missing",
    "invalid_workspace",
    "error",
]


@dataclass(frozen=True)
class ClearPauseResult:
    cleared: bool
    status: ClearPauseStatus
    notice: str | None = None
    current_pause_token: str | None = None

_MAX_TRACKED_RUNS = 4096
_BLOCKED_RUN_IDS: OrderedDict[str, None] = OrderedDict()


def _track_blocked_run(run_id: str) -> None:
    cleaned = str(run_id or "").strip()
    if not cleaned:
        return
    _BLOCKED_RUN_IDS[cleaned] = None
    _BLOCKED_RUN_IDS.move_to_end(cleaned)
    while len(_BLOCKED_RUN_IDS) > _MAX_TRACKED_RUNS:
        _BLOCKED_RUN_IDS.popitem(last=False)


def _clear_tracked_blocked_runs_for_test() -> None:
    """Test helper to reset in-process blocked run cache."""
    _BLOCKED_RUN_IDS.clear()


def _extract_configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    cfg = config.get("configurable")
    return cfg if isinstance(cfg, Mapping) else {}


def _extract_run_id(config: Mapping[str, Any] | None) -> str:
    cfg = _extract_configurable(config)
    raw = cfg.get("run_id") or cfg.get("response_id")
    if not raw and isinstance(config, Mapping):
        raw = config.get("run_id")
    return str(raw or "").strip()


def _extract_workspace_id(
    config: Mapping[str, Any] | None, explicit_workspace_id: str | None = None
) -> str:
    if explicit_workspace_id and str(explicit_workspace_id).strip():
        return str(explicit_workspace_id).strip()
    cfg = _extract_configurable(config)
    raw = cfg.get("turn_admitted_workspace_id") or cfg.get("workspace_id")
    if not raw and isinstance(config, Mapping):
        raw = config.get("workspace_id")
    return str(raw or "").strip()


def _resolve_role(config: Mapping[str, Any] | None, explicit_role: str | None) -> str | None:
    if explicit_role:
        return str(explicit_role).strip().lower()
    cfg = _extract_configurable(config)
    raw = cfg.get("agent_role")
    if raw:
        return str(raw).strip().lower()
    return None


def _pending_marker_payload(
    *, run_id: str | None = None, reason: str = OUTBOUND_BLOCKED_METADATA_KEY
) -> dict[str, Any]:
    return {
        "run_id": str(run_id or ""),
        "marked_at": datetime.now(UTC).isoformat(),
        "pause_token": str(uuid4()),
        "reason": reason,
    }


class _PersistAborted(Exception):
    """Internal rollback sentinel when an atomic dual-row update matches 0 rows."""


async def mark_workspace_pending_outbound_confirm(
    workspace_id: str,
    *,
    run_id: str | None = None,
    reason: str = OUTBOUND_BLOCKED_METADATA_KEY,
    conn: Any = None,
) -> bool:
    """Persist workspace-level ``pending_outbound_confirm`` pause marker.

    When ``conn`` is provided (e.g. inside ``begin_run``'s START transaction),
    executes on that connection so the workspace pause commits atomically with
    the run row. Returns True iff the workspace row was updated.
    """
    ws_uid = normalize_uuid(str(workspace_id or "").strip())
    if ws_uid is None:
        return False
    marker = {
        PENDING_OUTBOUND_CONFIRM_CONFIG_KEY: _pending_marker_payload(
            run_id=run_id, reason=reason
        )
    }

    async def _apply(c: Any) -> bool:
        async with c.cursor() as cur:
            await cur.execute(
                """
                UPDATE workspaces
                SET config = COALESCE(config, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE workspace_id = %s
                RETURNING workspace_id
                """,
                (Json(marker), ws_uid),
            )
            return (await cur.fetchone()) is not None

    if conn is not None:
        return await _apply(conn)
    try:
        async with get_db_connection() as owned:
            async with owned.transaction():
                return await _apply(owned)
    except Exception as exc:
        logger.warning(
            "[colleague_outbound_guard] Failed to persist %s on workspace %s: %s",
            PENDING_OUTBOUND_CONFIRM_CONFIG_KEY,
            ws_uid,
            type(exc).__name__,
        )
        return False


async def _persist_run_outbound_blocked_db(
    run_id: str,
    workspace_id: str | None = None,
) -> bool:
    """Atomically persist ``colleague_outbound_blocked`` on the run AND
    workspace-level ``pending_outbound_confirm`` on the workspace.

    Returns True ONLY when Postgres confirms BOTH updates matched and committed.
    """
    run_uid = normalize_uuid(str(run_id or "").strip())
    ws_uid = normalize_uuid(str(workspace_id or "").strip())
    if run_uid is None or ws_uid is None:
        return False
    marker = {
        PENDING_OUTBOUND_CONFIRM_CONFIG_KEY: _pending_marker_payload(
            run_id=run_uid, reason=OUTBOUND_BLOCKED_METADATA_KEY
        )
    }
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # Lock workspaces BEFORE conversation_responses to match START's lock order.
                    await cur.execute(
                        """
                        UPDATE workspaces
                        SET config = COALESCE(config, '{}'::jsonb) || %s::jsonb,
                            updated_at = NOW()
                        WHERE workspace_id = %s
                        RETURNING workspace_id
                        """,
                        (Json(marker), ws_uid),
                    )
                    if await cur.fetchone() is None:
                        raise _PersistAborted("workspace_row_missing")
                    await cur.execute(
                        """
                        UPDATE conversation_responses
                        SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                        WHERE conversation_response_id = %s
                        RETURNING conversation_response_id
                        """,
                        (Json({OUTBOUND_BLOCKED_METADATA_KEY: True}), run_uid),
                    )
                    if await cur.fetchone() is None:
                        raise _PersistAborted("run_row_missing")
        return True
    except _PersistAborted:
        return False
    except Exception as exc:
        logger.warning(
            "[colleague_outbound_guard] Failed to persist %s / %s (run=%s ws=%s): %s",
            OUTBOUND_BLOCKED_METADATA_KEY,
            PENDING_OUTBOUND_CONFIRM_CONFIG_KEY,
            run_uid,
            ws_uid,
            type(exc).__name__,
        )
        return False


async def _fetch_run_outbound_blocked_db(run_id: str) -> bool:
    """Read ``colleague_outbound_blocked`` from ``conversation_responses.metadata``.

    Fails closed (returns True) when reading Postgres fails for a valid run UUID.
    """
    cleaned = str(run_id or "").strip()
    if not cleaned:
        return False
    uid = normalize_uuid(cleaned)
    if uid is None:
        return False
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s
                    """,
                    (uid,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        logger.warning(
            "[colleague_outbound_guard] Failed to read %s for run %s; failing closed: %s",
            OUTBOUND_BLOCKED_METADATA_KEY,
            cleaned,
            type(exc).__name__,
        )
        return True
    if not row:
        return False
    meta = row[0] if not isinstance(row, Mapping) else row.get("metadata")
    if isinstance(meta, Mapping):
        if str(meta.get("agent_role") or "").strip().lower() == "chief_of_staff":
            return False
        if bool(meta.get(OUTBOUND_BLOCKED_METADATA_KEY)):
            _track_blocked_run(cleaned)
            return True
    return False


async def mark_run_outbound_blocked(
    config: Mapping[str, Any] | None,
    *,
    role: str | None = None,
    workspace_id: str | None = None,
) -> bool:
    """Persist run outbound block AND workspace ``pending_outbound_confirm`` before sensitive action.

    Returns True iff persistence is confirmed (or role is ``chief_of_staff``).
    Callers MUST refuse reading/returning colleague content or uploading copied
    files when this returns False.
    """
    effective_role = _resolve_role(config, role)
    if effective_role == "chief_of_staff":
        return True
    run_id = _extract_run_id(config)
    ws_id = _extract_workspace_id(config, workspace_id)
    if not run_id or not ws_id or ws_id.lower() == "home":
        logger.warning(
            "[colleague_outbound_guard] Refusing colleague action: missing run_id or workspace_id"
        )
        return False
    persisted = await _persist_run_outbound_blocked_db(run_id, ws_id)
    if not persisted:
        return False
    if isinstance(config, Mapping):
        cfg = config.get("configurable")
        if isinstance(cfg, dict):
            cfg[OUTBOUND_BLOCKED_METADATA_KEY] = True
    _track_blocked_run(run_id)
    return True


async def is_run_outbound_blocked(
    config: Mapping[str, Any] | None,
    *,
    role: str | None = None,
) -> bool:
    """Return True iff the current turn is an outbound-blocked analyst turn.

    Always returns False for ``chief_of_staff``.
    ``_BLOCKED_RUN_IDS`` is strictly a monotone acceleration cache; authoritative
    state lives in Postgres, and DB read failures fail closed (True).
    """
    effective_role = _resolve_role(config, role)
    if effective_role == "chief_of_staff":
        return False
    cfg = _extract_configurable(config)
    if bool(cfg.get(OUTBOUND_BLOCKED_METADATA_KEY)):
        return True
    user_id = str(cfg.get("user_id") or "").strip()
    workspace_owner_id = str(cfg.get("workspace_owner_id") or "").strip()
    if user_id and workspace_owner_id and user_id != workspace_owner_id:
        return True
    run_id = _extract_run_id(config)
    if run_id:
        if run_id in _BLOCKED_RUN_IDS:
            return True
        if await _fetch_run_outbound_blocked_db(run_id):
            return True
    return False


def is_run_row_outbound_blocked(run: Mapping[str, Any] | None) -> bool:
    """Synchronous check on a DB run row (normal & detached finalize)."""
    if not isinstance(run, Mapping):
        return False
    meta = run.get("metadata")
    meta_map = meta if isinstance(meta, Mapping) else {}
    if str(meta_map.get("agent_role") or "").strip().lower() == "chief_of_staff":
        return False
    if bool(meta_map.get(OUTBOUND_BLOCKED_METADATA_KEY)):
        return True
    run_id = str(
        run.get("conversation_response_id")
        or run.get("run_id")
        or meta_map.get("run_id")
        or ""
    ).strip()
    if run_id and run_id in _BLOCKED_RUN_IDS:
        return True
    return False


# ---------------------------------------------------------------------------
# Workspace-level ``pending_outbound_confirm`` pause marker on ``workspaces.config``
# ---------------------------------------------------------------------------


def _extract_pause_token(raw: Any) -> str | None:
    if not raw:
        return None
    if isinstance(raw, Mapping):
        token = str(raw.get("pause_token") or raw.get("marked_at") or "").strip()
        return token or None
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return "paused"


def get_workspace_pause_state(
    workspace: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """Return ``(auto_publish_paused, auto_publish_pause_token)`` without exposing ``run_id``."""
    cfg = (workspace or {}).get("config")
    if not isinstance(cfg, Mapping):
        return False, None
    raw = cfg.get(PENDING_OUTBOUND_CONFIRM_CONFIG_KEY)
    if not raw:
        return False, None
    return True, _extract_pause_token(raw)


def is_workspace_pending_outbound_confirm(
    workspace: Mapping[str, Any] | None,
) -> bool:
    """Return True if ``workspace`` currently has auto-publish paused pending owner confirmation."""
    paused, _ = get_workspace_pause_state(workspace)
    return paused


async def is_workspace_pending_outbound_confirm_in_cur(
    cur: Any,
    workspace_id: str | None,
) -> bool:
    """Read ``workspaces.config->'pending_outbound_confirm'`` inside an active commit transaction.

    Takes ``FOR SHARE`` on the ``workspaces`` row so any in-flight transaction
    setting ``pending_outbound_confirm`` (``UPDATE workspaces``) linearizes
    strictly ahead of or behind this version commit — closing the concurrent
    auto-publish window completely.
    """
    ws_uid = normalize_uuid(str(workspace_id or "").strip())
    if ws_uid is None:
        return False
    await cur.execute(
        """
        SELECT config->%s AS pending
        FROM workspaces
        WHERE workspace_id = %s
        FOR SHARE
        """,
        (PENDING_OUTBOUND_CONFIRM_CONFIG_KEY, ws_uid),
    )
    row = await cur.fetchone()
    if not row:
        return False
    if isinstance(row, Mapping):
        val = row.get("pending")
    elif isinstance(row, (tuple, list)):
        val = row[0]
    else:
        val = getattr(row, "pending", None)
    return bool(val)


async def clear_pending_outbound_confirm(
    workspace_id: str,
    *,
    expected_pause_token: str | None = None,
) -> ClearPauseResult:
    """CAS-clear workspace-level ``pending_outbound_confirm`` after explicit owner web publish.

    Inside a single transaction (``FOR UPDATE`` on ``workspaces``):
    1. If not paused, returns ``status="not_paused"``.
    2. If any ``in_progress`` blocked run (root or subagent) is still active on
       this workspace, keeps pause intact and returns ``status="active_blocked_run"``
       with ``ACTIVE_BLOCKED_RUN_CLEAR_NOTICE``.
    3. Compares ``expected_pause_token`` against the stored pause token (or
       ``marked_at`` fallback); ONLY clears when equal, otherwise keeps pause
       intact and returns ``token_missing`` / ``token_mismatch``.
    """
    ws_uid = normalize_uuid(str(workspace_id or "").strip())
    if ws_uid is None:
        return ClearPauseResult(cleared=False, status="invalid_workspace")
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT config->%s AS pending
                        FROM workspaces
                        WHERE workspace_id = %s
                        FOR UPDATE
                        """,
                        (PENDING_OUTBOUND_CONFIRM_CONFIG_KEY, ws_uid),
                    )
                    row = await cur.fetchone()
                    if not row:
                        return ClearPauseResult(cleared=False, status="invalid_workspace")
                    raw_pending = row.get("pending") if isinstance(row, Mapping) else row[0]
                    if not raw_pending:
                        return ClearPauseResult(cleared=False, status="not_paused")
                    current_token = _extract_pause_token(raw_pending)

                    await cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM conversation_responses r
                            JOIN conversation_threads t
                              ON t.conversation_thread_id = r.conversation_thread_id
                            WHERE t.workspace_id = %s
                              AND r.status = 'in_progress'
                              AND (r.metadata->>%s) = 'true'
                            UNION ALL
                            SELECT 1
                            FROM subagent_runs s
                            JOIN conversation_responses r
                              ON r.conversation_response_id = s.parent_run_id
                            JOIN conversation_threads t
                              ON t.conversation_thread_id = s.thread_id
                            WHERE t.workspace_id = %s
                              AND s.status = 'in_progress'
                              AND (r.metadata->>%s) = 'true'
                        )
                        """,
                        (
                            ws_uid,
                            OUTBOUND_BLOCKED_METADATA_KEY,
                            ws_uid,
                            OUTBOUND_BLOCKED_METADATA_KEY,
                        ),
                    )
                    active_row = await cur.fetchone()
                    has_active_blocked = bool(
                        active_row[0]
                        if active_row and not isinstance(active_row, Mapping)
                        else (next(iter(active_row.values())) if active_row else False)
                    )
                    if has_active_blocked:
                        return ClearPauseResult(
                            cleared=False,
                            status="active_blocked_run",
                            notice=ACTIVE_BLOCKED_RUN_CLEAR_NOTICE,
                            current_pause_token=current_token,
                        )

                    expected = str(expected_pause_token or "").strip()
                    if not expected:
                        return ClearPauseResult(
                            cleared=False,
                            status="token_missing",
                            notice=PAUSE_TOKEN_MISSING_NOTICE,
                            current_pause_token=current_token,
                        )
                    if current_token != expected:
                        return ClearPauseResult(
                            cleared=False,
                            status="token_mismatch",
                            notice=PAUSE_TOKEN_MISMATCH_NOTICE,
                            current_pause_token=current_token,
                        )

                    await cur.execute(
                        """
                        UPDATE workspaces
                        SET config = COALESCE(config, '{}'::jsonb) - %s,
                            updated_at = NOW()
                        WHERE workspace_id = %s
                        RETURNING workspace_id
                        """,
                        (PENDING_OUTBOUND_CONFIRM_CONFIG_KEY, ws_uid),
                    )
                    if (await cur.fetchone()) is None:
                        return ClearPauseResult(cleared=False, status="invalid_workspace")
                    return ClearPauseResult(cleared=True, status="cleared")
    except Exception as exc:
        logger.warning(
            "[colleague_outbound_guard] Failed to clear %s for ws=%s: %s",
            PENDING_OUTBOUND_CONFIRM_CONFIG_KEY,
            ws_uid,
            type(exc).__name__,
        )
        return ClearPauseResult(
            cleared=False,
            status="error",
            notice="解除工作区自动发布暂停时出错，已保留暂停状态，请稍后重试",
        )


async def computer_has_active_cross_user_run(
    computer_id: str,
    owner_user_id: str,
) -> bool:
    """Return True if Postgres shows any ``in_progress`` cross-user run on ``computer_id``.

    Used as LiveFS's fail-closed fallback when ``CallContext`` is missing
    (e.g. Redis outage or expired context key): if a colleague currently has an
    active root or subagent run on this computer, user-data mutations are locked
    read-only; when no cross-user run is active, owner mutations proceed normally.
    DB errors fail closed (return True) for valid computer UUIDs.
    """
    comp_uid = normalize_uuid(str(computer_id or "").strip())
    owner = str(owner_user_id or "").strip()
    if comp_uid is None or not owner:
        return False
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM conversation_responses r
                        JOIN conversation_threads t
                          ON t.conversation_thread_id = r.conversation_thread_id
                        JOIN workspaces w ON w.workspace_id = t.workspace_id
                        WHERE r.status = 'in_progress'
                          AND w.computer_id = %s
                          AND COALESCE(r.user_id::text, r.metadata->>'user_id', '') <> ''
                          AND COALESCE(r.user_id::text, r.metadata->>'user_id') <> %s
                        UNION ALL
                        SELECT 1
                        FROM subagent_runs s
                        JOIN conversation_threads t
                          ON t.conversation_thread_id = s.thread_id
                        JOIN workspaces w ON w.workspace_id = t.workspace_id
                        WHERE s.status = 'in_progress'
                          AND w.computer_id = %s
                          AND COALESCE(s.user_id::text, '') <> ''
                          AND s.user_id::text <> %s
                    )
                    """,
                    (comp_uid, owner, comp_uid, owner),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                return bool(row[0] if not isinstance(row, Mapping) else next(iter(row.values())))
    except Exception as exc:
        logger.warning(
            "[colleague_outbound_guard] Failed checking active cross-user runs on computer %s; failing closed: %s",
            comp_uid,
            type(exc).__name__,
        )
        return True
