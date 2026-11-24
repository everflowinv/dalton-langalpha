"""Resolve the model for notification turns during an open automation execution.

Instead of writing an automation's ``llm_model`` onto ``conversation_threads``,
server-initiated notification turns inherit the automation execution's model at
request resolution time in ``messaging.py``.

Source-execution selection rules:
1. If the request carries explicit source execution information
   (``retry_of_run_id``, ``automation_delivery`` / ``automation_earlier_runs``
   from a handoff, or ``report_source`` for ``command_report`` / ``task``),
   resolve and use that specific execution's model.
2. Otherwise (e.g. ``restart_resume``, ``wakeup``, ``market_target``, or a
   handoff without delivery metadata), pick the thread's latest ``running``
   execution (``status = 'running'``, regardless of whether background work is
   currently pending so deployment-interrupted turns awaiting recovery also
   inherit). Settled executions without an explicit source do not match, so a
   ``wakeup`` firing after settlement never inherits.

First-turn model resolution rules:
- Do NOT rely on ``automation_executions.conversation_response_id`` because
  ``take_over_execution_run`` advances it to continuation turns.
- Instead, look up the execution's earliest non-notification turn in
  ``conversation_responses`` / ``conversation_queries`` matching
  ``metadata->>'automation_execution_id'``; if missing there, fall back to the
  earliest ``origin.type == 'automation'`` turn on the thread at or after
  ``execution.started_at`` (or ``created_at``).
- If that first turn still has no ``llm_model``, fall back to the automation's
  configured ``llm_model``.

Fallback rules:
- If the inherited model cannot be served (``_UNSERVABLE_REFUSALS``), catch the
  refusal, log a warning, and re-run ``thread_model.resolve_turn_config`` with
  ``named=None`` so the thread's held model or account default takes over.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.services.automation_subscription_fallback import (
    build_flagged_subscription_error,
    capture_resolved_effective_model,
    extract_rejected_model_from_exc,
    is_subscription_unavailable_error,
    load_fallback_prefs_or_reraise,
    resolve_with_subscription_fallback,
)
from src.dalton.sharing import extract_wakeup_notice_ids
from src.server.contracts.turn_kinds import NOTIFICATION_QUERY_TYPES
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.pool import get_db_connection
from src.server.services.llm import thread_model, user_models
from src.server.services.llm.availability import model_resolves
from src.server.services.llm.thread_model import _UNSERVABLE_REFUSALS
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)


def _is_unservable_refusal(exc: HTTPException) -> bool:
    return (
        exc.status_code == 400
        and isinstance(exc.detail, dict)
        and exc.detail.get("type") in _UNSERVABLE_REFUSALS
    )


def _is_server_notification_turn(
    request: Any,
    *,
    from_service: bool,
    retry_of_run_id: str | None = None,
    is_server_retry: bool = False,
) -> bool:
    """Return True for server-authored notification turns, agent steers, and server retries.

    Retry turns require explicit server authentication (``is_server_retry``),
    never relying on ``from_service`` alone, because in OSS mode without
    ``INTERNAL_SERVICE_TOKEN`` ``_is_service_request`` returns True for user
    requests too.
    """
    if retry_of_run_id:
        return bool(from_service and is_server_retry)
    if not from_service:
        return False
    platform = getattr(request, "platform", None)
    if getattr(request, "query_type", None) in NOTIFICATION_QUERY_TYPES and not platform:
        return True
    origin = getattr(request, "origin", None)
    return bool(
        getattr(request, "steer_job_id", None)
        and getattr(origin, "type", None) == "agent"
        and not platform
    )


def _delivery_execution_id(request: Any) -> str | None:
    """Extract an automation execution UUID from ``automation_delivery`` / ``automation_earlier_runs``."""
    candidates: list[Any] = []
    delivery = getattr(request, "automation_delivery", None)
    if isinstance(delivery, Mapping):
        candidates.append(delivery)
    earlier = getattr(request, "automation_earlier_runs", None)
    if isinstance(earlier, (list, tuple)):
        candidates.extend(reversed(earlier))
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        exec_id = normalize_uuid(raw_id.strip().split(".", 1)[0])
        if exec_id is not None:
            return exec_id
    return None


async def _get_execution_by_id(
    execution_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    sql = """
        SELECT e.automation_execution_id, e.automation_id, e.status,
               e.conversation_thread_id, e.started_at, e.created_at,
               a.user_id, a.thread_strategy
        FROM automation_executions e
        JOIN automations a ON a.automation_id = e.automation_id
        WHERE e.automation_execution_id = %s::uuid
        LIMIT 1
    """
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (execution_id,))
            row = await cur.fetchone()
            return dict(row) if row else None
    async with get_db_connection() as own_conn:
        async with own_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (execution_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


def _response_execution_id_sql(alias: str) -> str:
    """SQL expression resolving execution UUID from firing, source-inheritance, or delivery metadata."""
    return (
        f"COALESCE("
        f"NULLIF({alias}.metadata->>'automation_execution_id', ''), "
        f"NULLIF({alias}.metadata->>'automation_source_execution_id', ''), "
        f"NULLIF(split_part(COALESCE({alias}.metadata->'automation_delivery'->>'id', ''), '.', 1), '')"
        f")"
    )


async def _get_execution_for_run_id(run_id: str) -> dict[str, Any] | None:
    if normalize_uuid(run_id) is None:
        return None
    exec_expr = _response_execution_id_sql("r")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM conversation_responses r
                JOIN automation_executions e
                  -- A handoff notification records its firing as automation_delivery.id.
                  ON e.automation_execution_id::text = {exec_expr}
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE r.conversation_response_id = %s::uuid
                LIMIT 1
                """,
                (run_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def _get_execution_for_command(
    thread_id: str, command_id: str
) -> dict[str, Any] | None:
    exec_expr = _response_execution_id_sql("pr")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM command_runs cr
                JOIN conversation_responses pr
                  ON pr.conversation_response_id = cr.run_id
                JOIN automation_executions e
                  ON e.automation_execution_id::text = {exec_expr}
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE cr.command_id = %s::text
                  AND cr.conversation_thread_id = %s::uuid
                LIMIT 1
                """,
                (command_id, thread_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def _get_execution_for_task(
    thread_id: str,
    *,
    task_run_id: str | None,
    task_id: str | None,
) -> dict[str, Any] | None:
    if task_run_id and normalize_uuid(task_run_id) is not None:
        where_sql = "sr.thread_id = %s::uuid AND sr.task_run_id = %s::uuid"
        param = task_run_id
    elif task_id:
        where_sql = "sr.thread_id = %s::uuid AND sr.task_id = %s::text"
        param = task_id
    else:
        return None
    exec_expr = _response_execution_id_sql("pr")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM subagent_runs sr
                JOIN conversation_responses pr
                  ON pr.conversation_response_id = sr.parent_run_id
                JOIN automation_executions e
                  ON e.automation_execution_id::text = {exec_expr}
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE {where_sql}
                ORDER BY sr.started_at DESC
                LIMIT 1
                """,
                (thread_id, param),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


_EXPLICIT_EXECUTION_MATCH_KINDS = frozenset(
    {
        "retry_run",
        "execution_id",
        "automation_delivery",
        "report_source",
        "wakeup_creator_run",
    }
)


async def _get_wakeup_creator_run_ids(
    thread_id: str,
    wakeup_ids: Sequence[str],
) -> list[str]:
    """Return non-null ``wakeups.created_by_run_id`` values in notice order for ``thread_id``."""
    valid_ids = [
        wid for raw in wakeup_ids if (wid := normalize_uuid(str(raw).strip())) is not None
    ]
    if not valid_ids or normalize_uuid(thread_id) is None:
        return []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT wakeup_id::text AS wakeup_id,
                       created_by_run_id::text AS created_by_run_id
                FROM wakeups
                WHERE conversation_thread_id = %s::uuid
                  AND wakeup_id = ANY(%s::uuid[])
                """,
                (thread_id, valid_ids),
            )
            rows = await cur.fetchall()
    by_wakeup: dict[str, str] = {}
    for row in rows or ():
        wid = normalize_uuid(str(row.get("wakeup_id") or ""))
        creator = normalize_uuid(str(row.get("created_by_run_id") or ""))
        if wid and creator:
            by_wakeup[wid] = creator
    ordered: list[str] = []
    seen: set[str] = set()
    for wid in valid_ids:
        creator = by_wakeup.get(wid)
        if creator and creator not in seen:
            seen.add(creator)
            ordered.append(creator)
    return ordered


def _explicit_execution_id(request: Any) -> str | None:
    raw = getattr(request, "automation_execution_id", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return normalize_uuid(raw.strip())


async def _resolve_source_execution(
    request: Any,
    *,
    thread_id: str,
    retry_of_run_id: str | None = None,
    allow_running_fallback: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the execution this notification turn belongs to.

    Explicit source hints (``retry_of_run_id``, explicit execution id, handoff
    ``automation_delivery``/``automation_earlier_runs``, ``report_source``
    for ``command_report``/``task``, and ``wakeups.created_by_run_id`` for
    ``wakeup``) take precedence over falling back to the thread's latest
    ``running`` execution.
    """
    if retry_of_run_id:
        exec_row = await _get_execution_for_run_id(str(retry_of_run_id))
        if exec_row is not None:
            return exec_row, "retry_run"

    explicit_exec_id = _explicit_execution_id(request)
    if explicit_exec_id:
        exec_row = await _get_execution_by_id(explicit_exec_id)
        if exec_row is not None:
            return exec_row, "execution_id"

    delivery_exec_id = _delivery_execution_id(request)
    if delivery_exec_id:
        exec_row = await _get_execution_by_id(delivery_exec_id)
        if exec_row is not None:
            return exec_row, "automation_delivery"

    source = getattr(request, "report_source", None)
    if source is not None:
        if isinstance(source, Mapping):
            kind = source.get("kind")
            command_id = source.get("command_id")
            task_run_id = source.get("task_run_id")
            task_id = source.get("task_id")
        else:
            kind = getattr(source, "kind", None)
            command_id = getattr(source, "command_id", None)
            task_run_id = getattr(source, "task_run_id", None)
            task_id = getattr(source, "task_id", None)
        if kind == "command_report" and command_id:
            exec_row = await _get_execution_for_command(thread_id, str(command_id))
            if exec_row is not None:
                return exec_row, "report_source"
        elif kind == "task" and (task_run_id or task_id):
            exec_row = await _get_execution_for_task(
                thread_id,
                task_run_id=str(task_run_id) if task_run_id else None,
                task_id=str(task_id) if task_id else None,
            )
            if exec_row is not None:
                return exec_row, "report_source"

    if getattr(request, "query_type", None) == "wakeup":
        wakeup_ids = extract_wakeup_notice_ids(request)
        if wakeup_ids:
            creator_run_ids = await _get_wakeup_creator_run_ids(thread_id, wakeup_ids)
            if creator_run_ids:
                for creator_run_id in creator_run_ids:
                    exec_row = await _get_execution_for_run_id(creator_run_id)
                    if exec_row is not None:
                        return exec_row, "wakeup_creator_run"
                # Wakeup(s) had an explicit creator run that does not belong to
                # any automation execution (e.g. a user turn's wakeup): refuse
                # falling back to an unrelated running execution on the thread.
                return None, "none"

    if not allow_running_fallback:
        return None, "none"
    running = await exec_db.get_running_executions_for_thread(thread_id)
    if not running:
        return None, "none"
    return running[-1], "running_thread"


async def _get_execution_first_turn_run_settings(
    execution: Mapping[str, Any],
    *,
    fallback_thread_id: str,
) -> tuple[str | None, bool, str | None, bool | None]:
    """Read ``(first_model, effort_configured, first_effort, system_only)`` from the execution's first turn.

    Reads the earliest ``conversation_responses`` row for this execution
    (``turn_index ASC, attempt_no ASC``) so idempotent ``conversation_queries``
    upserts on retries cannot overwrite the first turn's recorded effort or
    ``llm_model_system_only`` marker. ``system_only`` is ``None`` only when no
    first-turn ``conversation_responses`` row was found.
    """
    exec_id = str(execution["automation_execution_id"])
    exec_thread_id = str(execution.get("conversation_thread_id") or fallback_thread_id)
    if normalize_uuid(exec_thread_id) is None:
        return None, False, None, None
    started_at = execution.get("started_at") or execution.get("created_at")

    first_model: str | None = None
    effort_configured = False
    first_effort: str | None = None
    first_system_only: bool | None = None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COALESCE(
                         NULLIF(r.metadata->>'llm_model', ''),
                         NULLIF(q.metadata->>'llm_model', '')
                       ) AS llm_model,
                       CASE
                         WHEN r.metadata ? 'automation_reasoning_effort_configured'
                         THEN COALESCE(
                           (r.metadata->>'automation_reasoning_effort_configured')::boolean,
                           FALSE
                         )
                         ELSE FALSE
                       END AS effort_configured,
                       NULLIF(r.metadata->>'automation_reasoning_effort', '') AS reasoning_effort,
                       COALESCE((r.metadata->>'llm_model_system_only')::boolean, FALSE) AS llm_model_system_only
                FROM conversation_responses r
                LEFT JOIN conversation_queries q
                  ON q.conversation_thread_id = r.conversation_thread_id
                 AND q.turn_index = r.turn_index
                WHERE r.conversation_thread_id = %s::uuid
                  AND r.metadata->>'automation_execution_id' = %s
                  AND (q.type IS NULL OR q.type NOT IN ('system', 'wakeup', 'market_target'))
                ORDER BY (COALESCE(q.metadata->'origin'->>'type', '') = 'automation'
                          OR r.metadata->>'run_origin' = 'automation') DESC NULLS LAST,
                         r.turn_index ASC,
                         r.attempt_no ASC
                LIMIT 1
                """,
                (exec_thread_id, exec_id),
            )
            row = await cur.fetchone()
            if row:
                raw_model = row.get("llm_model")
                if raw_model:
                    first_model = str(raw_model)
                effort_configured = bool(row.get("effort_configured"))
                raw_effort = row.get("reasoning_effort")
                first_effort = str(raw_effort) if raw_effort else None
                first_system_only = bool(row.get("llm_model_system_only"))
                if first_model:
                    return first_model, effort_configured, first_effort, first_system_only

            if started_at is None:
                return first_model, effort_configured, first_effort, first_system_only

            await cur.execute(
                """
                SELECT q.metadata->>'llm_model' AS llm_model
                FROM conversation_queries q
                WHERE q.conversation_thread_id = %s::uuid
                  AND q.created_at >= %s
                  AND q.metadata->'origin'->>'type' = 'automation'
                ORDER BY q.turn_index ASC, q.created_at ASC
                LIMIT 1
                """,
                (exec_thread_id, started_at),
            )
            fallback_row = await cur.fetchone()
            fallback_model = fallback_row.get("llm_model") if fallback_row else None
            return (
                str(fallback_model) if fallback_model else first_model,
                effort_configured,
                first_effort,
                first_system_only,
            )


async def _get_execution_first_turn_settings(
    execution: Mapping[str, Any],
    *,
    fallback_thread_id: str,
) -> tuple[str | None, bool, str | None]:
    """Read ``(first_model, effort_configured, first_effort)`` from the execution's first turn."""
    (
        first_model,
        effort_configured,
        first_effort,
        _first_system_only,
    ) = await _get_execution_first_turn_run_settings(
        execution, fallback_thread_id=fallback_thread_id
    )
    return first_model, effort_configured, first_effort


_DEFAULT_GET_FIRST_TURN_SETTINGS = _get_execution_first_turn_settings


async def _get_execution_first_turn_llm_model(
    execution: Mapping[str, Any],
    *,
    fallback_thread_id: str,
) -> str | None:
    """Backwards-compatible helper returning only the execution's first turn ``llm_model``."""
    model, _effort_configured, _first_effort = await _get_execution_first_turn_settings(
        execution, fallback_thread_id=fallback_thread_id
    )
    return model


_DEFAULT_GET_FIRST_TURN_LLM_MODEL = _get_execution_first_turn_llm_model


def _automation_model_unreadable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "type": "automation_model_unavailable",
            "message": "暂时无法读取本次自动任务的模型设置；为避免改用其他模型，本回合已停止，请稍后重试。",
        },
    )


def _accepts_effort_override(resolve: Callable[..., Awaitable[Any]]) -> bool:
    try:
        sig = inspect.signature(resolve)
    except (TypeError, ValueError):
        return False
    for name, param in sig.parameters.items():
        if name == "reasoning_effort_override" or param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _accepts_ignore_custom_models_override(resolve: Callable[..., Awaitable[Any]]) -> bool:
    try:
        sig = inspect.signature(resolve)
    except (TypeError, ValueError):
        return False
    for name, param in sig.parameters.items():
        if (
            name == "ignore_custom_models_override"
            or param.kind == inspect.Parameter.VAR_KEYWORD
        ):
            return True
    return False


async def _resolve_automation_notification_context(
    *,
    user_id: str,
    thread_id: str,
    request: Any,
    from_service: bool,
    is_server_retry: bool = False,
    retry_of_run_id: str | None = None,
    allow_running_fallback: bool = True,
    resolve_model: bool = True,
) -> tuple[
    dict[str, Any] | None,
    str | None,
    dict[str, Any] | None,
    bool,
    bool,
    str | None,
]:
    """Resolve ``(target_exec, inherited_model, user_prefs, fallback_eligible, effort_configured, inherited_effort)``."""
    is_server_turn = _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    )
    is_user_retry = bool(retry_of_run_id) and not is_server_turn
    if not is_server_turn and not is_user_retry:
        return None, None, None, False, False, None
    if normalize_uuid(thread_id) is None:
        return None, None, None, False, False, None

    try:
        if is_user_retry:
            exec_row = await _get_execution_for_run_id(str(retry_of_run_id))
            target_exec = exec_row
            match_kind = "user_retry_run" if exec_row is not None else "none"
        else:
            target_exec, match_kind = await _resolve_source_execution(
                request,
                thread_id=thread_id,
                retry_of_run_id=retry_of_run_id,
                allow_running_fallback=allow_running_fallback,
            )
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed resolving source execution "
            f"for thread={thread_id}; falling back to default resolution",
            exc_info=True,
        )
        return None, None, None, False, False, None
    if target_exec is None:
        return None, None, None, False, False, None

    fallback_eligible = is_server_turn and match_kind in _EXPLICIT_EXECUTION_MATCH_KINDS
    confirmed_source = fallback_eligible or match_kind == "user_retry_run"
    if confirmed_source and not fallback_eligible:
        target_exec = {**target_exec, "_effort_source_confirmed": True}
    explicit_request_model = getattr(request, "llm_model", None)
    explicit_request_effort = getattr(request, "reasoning_effort", None)
    need_model = bool(is_server_turn and resolve_model and not explicit_request_model)
    need_effort = bool(confirmed_source and explicit_request_effort is None)
    if not need_model and not need_effort:
        return target_exec, None, None, fallback_eligible, False, None

    exec_id = str(target_exec["automation_execution_id"])
    chosen_model: str | None = None
    source: str | None = None
    effort_configured = False
    inherited_effort: str | None = None
    inherited_system_only = False
    first_system_only: bool | None = None
    try:
        if _get_execution_first_turn_llm_model is not _DEFAULT_GET_FIRST_TURN_LLM_MODEL:
            first_model = await _get_execution_first_turn_llm_model(
                target_exec, fallback_thread_id=thread_id
            )
            first_effort_configured = bool(first_model)
            first_effort = None
            first_system_only = False if first_model else None
        elif _get_execution_first_turn_settings is not _DEFAULT_GET_FIRST_TURN_SETTINGS:
            (
                first_model,
                first_effort_configured,
                first_effort,
            ) = await _get_execution_first_turn_settings(
                target_exec, fallback_thread_id=thread_id
            )
            first_system_only = False
        else:
            (
                first_model,
                first_effort_configured,
                first_effort,
                first_system_only,
            ) = await _get_execution_first_turn_run_settings(
                target_exec, fallback_thread_id=thread_id
            )
        if need_model and first_model:
            chosen_model = first_model
            source = "first_turn"
        if confirmed_source and first_effort_configured:
            effort_configured = True
            inherited_effort = first_effort
        if first_system_only is not None:
            inherited_system_only = bool(first_system_only)

        need_db_model = need_model and not chosen_model
        need_db_effort = need_effort and not effort_configured
        need_db_system_only = first_system_only is None
        if need_db_model or need_db_effort or need_db_system_only:
            try:
                automation = await auto_db.get_automation_including_deleted(
                    str(target_exec["automation_id"]),
                    target_exec.get("user_id"),
                )
            except Exception:
                if need_db_model:
                    raise
                logger.warning(
                    f"[AUTOMATION_THREAD_MODEL] Failed reading automation effort config for "
                    f"thread={thread_id} execution_id={exec_id} via={match_kind}",
                    exc_info=True,
                )
                automation = None
            if automation:
                if need_db_model:
                    cfg_model = automation.get("llm_model")
                    if cfg_model:
                        chosen_model = str(cfg_model)
                        source = "automation_config"
                if need_db_effort:
                    effort_configured = True
                    cfg_effort = automation.get("reasoning_effort")
                    inherited_effort = str(cfg_effort) if cfg_effort else None
                if need_db_system_only:
                    auto_meta = automation.get("metadata")
                    if isinstance(auto_meta, Mapping) and bool(
                        auto_meta.get("llm_model_system_only")
                    ):
                        inherited_system_only = True
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed reading execution settings for thread={thread_id} "
            f"execution_id={exec_id} via={match_kind}",
            exc_info=True,
        )
        if not need_model:
            return target_exec, None, None, fallback_eligible, False, None
        if fallback_eligible:
            # Keep the confirmed link; the caller stops the turn if nothing else names
            # a model (it must not run on the account default instead).
            return (
                {**target_exec, "_model_read_failed": True},
                None,
                None,
                fallback_eligible,
                False,
                None,
            )
        return None, None, None, False, False, None

    if inherited_system_only:
        target_exec = {**target_exec, "_llm_model_system_only": True}

    if not chosen_model:
        return (
            target_exec,
            None,
            None,
            fallback_eligible,
            effort_configured,
            inherited_effort,
        )

    try:
        prefs = await user_models.get_model_preference(user_id)
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed reading user model preference for thread={thread_id} "
            f"execution_id={exec_id} via={match_kind}",
            exc_info=True,
        )
        if fallback_eligible:
            # Confirmed link: keep the execution's model and its fallback rules.
            return (
                target_exec,
                chosen_model,
                None,
                fallback_eligible,
                effort_configured,
                inherited_effort,
            )
        # Inferred link only: the previous behavior, default resolution.
        return None, None, None, False, False, None

    prefs_for_resolve = (
        {
            k: v
            for k, v in prefs.items()
            if k not in ("custom_models", "custom_providers")
        }
        if inherited_system_only
        else prefs
    )
    if not model_resolves(prefs_for_resolve, chosen_model):
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Model {chosen_model!r} for thread={thread_id} "
            f"execution_id={exec_id} source={source} via={match_kind} is unavailable; "
            f"using default resolution"
        )
        return (
            target_exec,
            None,
            prefs,
            fallback_eligible,
            effort_configured,
            inherited_effort,
        )

    logger.info(
        f"[AUTOMATION_THREAD_MODEL] thread={thread_id} execution_id={exec_id} "
        f"model={chosen_model} effort={inherited_effort} source={source} via={match_kind}"
    )
    return (
        target_exec,
        chosen_model,
        prefs,
        fallback_eligible,
        effort_configured,
        inherited_effort,
    )


async def is_automation_notification_turn(
    *,
    thread_id: str,
    request: Any,
    from_service: bool,
    is_server_retry: bool = False,
    retry_of_run_id: str | None = None,
) -> bool:
    """Return True when the request explicitly links to an automation execution."""
    if not _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    ):
        return False
    if normalize_uuid(thread_id) is None:
        return False
    try:
        target_exec, match_kind = await _resolve_source_execution(
            request,
            thread_id=thread_id,
            retry_of_run_id=retry_of_run_id,
            allow_running_fallback=False,
        )
        return (
            target_exec is not None
            and match_kind in _EXPLICIT_EXECUTION_MATCH_KINDS
        )
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed checking automation notification status "
            f"for thread={thread_id}",
            exc_info=True,
        )
        return False


async def resolve_automation_notification_model(
    *,
    user_id: str,
    thread_id: str,
    request: Any,
    from_service: bool,
    is_server_retry: bool = False,
    retry_of_run_id: str | None = None,
) -> str | None:
    """Return the automation execution's model for a notification turn, or ``None``."""
    if getattr(request, "llm_model", None):
        return None
    (
        _target_exec,
        chosen_model,
        _prefs,
        _eligible,
        _effort_configured,
        _inherited_effort,
    ) = await _resolve_automation_notification_context(
        user_id=user_id,
        thread_id=thread_id,
        request=request,
        from_service=from_service,
        is_server_retry=is_server_retry,
        retry_of_run_id=retry_of_run_id,
    )
    return chosen_model


async def resolve_turn_config_with_automation_inheritance(
    *,
    user_id: str,
    thread_id: str,
    request: Any,
    requested_model: str | None,
    held_model: str | None,
    from_service: bool,
    is_server_retry: bool = False,
    retry_of_run_id: str | None = None,
    resolve: Callable[..., Awaitable[Any]],
    flagged_subscription: tuple[Any, str | None] | None = None,
    on_fallback: Callable[[dict[str, Any]], None] | None = None,
    on_automation_context: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Resolve turn LLM config, inheriting an automation execution's model and effort with fallback.

    Only server notification turns that explicitly identify an automation
    execution (``retry_of_run_id``, explicit execution ID, ``automation_delivery``/
    ``automation_earlier_runs``, ``report_source``, or ``wakeup`` via
    ``wakeups.created_by_run_id``) qualify for ``fallback_models`` subscription
    fallback. Thread-level ``running`` heuristics keep legacy inheritance only,
    and failed subscription fallbacks raise the original refusal instead of
    dropping to thread/account default models.
    """
    target_exec: dict[str, Any] | None = None
    inherited_model: str | None = None
    prefs: dict[str, Any] | None = None
    fallback_eligible = False
    effort_configured = False
    inherited_effort: str | None = None
    if (
        requested_model is None
        or flagged_subscription is not None
        or bool(retry_of_run_id)
        or _is_server_notification_turn(
            request,
            from_service=from_service,
            retry_of_run_id=retry_of_run_id,
            is_server_retry=is_server_retry,
        )
    ):
        (
            target_exec,
            inherited_model,
            prefs,
            fallback_eligible,
            effort_configured,
            inherited_effort,
        ) = await _resolve_automation_notification_context(
            user_id=user_id,
            thread_id=thread_id,
            request=request,
            from_service=from_service,
            is_server_retry=is_server_retry,
            retry_of_run_id=retry_of_run_id,
            allow_running_fallback=(requested_model is None and flagged_subscription is None),
            resolve_model=(requested_model is None),
        )

    inherited_system_only = bool(
        getattr(request, "llm_model_system_only", False)
        or (target_exec or {}).get("_llm_model_system_only")
    )
    confirmed_for_context = target_exec is not None and (
        fallback_eligible or bool(target_exec.get("_effort_source_confirmed"))
    )
    if confirmed_for_context and on_automation_context is not None:
        exec_id = target_exec.get("automation_execution_id")
        if exec_id:
            # Stamp automation_source_execution_id (NOT automation_execution_id) so
            # restart recovery never mistakes a wakeup/continuation/retry run for a
            # scheduler-refirable firing turn while still preserving provenance.
            ctx_meta: dict[str, Any] = {
                "automation_source_execution_id": str(exec_id),
            }
            if effort_configured:
                ctx_meta["automation_reasoning_effort_configured"] = True
                ctx_meta["automation_reasoning_effort"] = inherited_effort
            if inherited_system_only:
                ctx_meta["llm_model_system_only"] = True
            on_automation_context(ctx_meta)

    explicit_request_effort = getattr(request, "reasoning_effort", None)
    apply_effort_override = (
        confirmed_for_context
        and effort_configured
        and explicit_request_effort is None
        and _accepts_effort_override(resolve)
    )
    apply_system_only_override = (
        inherited_system_only and _accepts_ignore_custom_models_override(resolve)
    )
    if apply_effort_override or apply_system_only_override:
        async def _effective_resolve(model_name: str | None) -> Any:
            extra_kw: dict[str, Any] = {}
            if apply_effort_override:
                extra_kw["reasoning_effort_override"] = inherited_effort
            if apply_system_only_override:
                extra_kw["ignore_custom_models_override"] = True
            return await resolve(model_name, **extra_kw)
    else:
        _effective_resolve = resolve

    async def _try_subscription_fallback(
        *,
        original_model: str | None,
        failed_provider: str | None,
        reason: str | None,
        exc_to_raise: BaseException,
        failed_subject: str | None = None,
    ) -> Any:
        pref_map = (
            prefs
            if prefs is not None
            else await load_fallback_prefs_or_reraise(
                user_id,
                exc_to_raise,
                context=f"user={user_id} thread={thread_id}",
            )
        )
        default_pref_map = (
            {
                k: v
                for k, v in pref_map.items()
                if k not in ("custom_models", "custom_providers")
            }
            if inherited_system_only
            else pref_map
        )
        orig = (
            None
            if failed_subject is not None
            else (original_model or thread_model.effective_default_model(default_pref_map))
        )
        cfg, record = await resolve_with_subscription_fallback(
            user_id=user_id,
            prefs=pref_map,
            original_model=orig,
            failed_provider=failed_provider,
            reason=reason,
            resolve_candidate=_effective_resolve,
            exc_to_raise=exc_to_raise,
            model_resolves_fn=model_resolves,
            failed_subject=failed_subject,
            ignore_custom_models=inherited_system_only,
        )
        if target_exec and target_exec.get("automation_execution_id"):
            record = {
                **record,
                "execution_id": str(target_exec["automation_execution_id"]),
            }
        if on_fallback is not None:
            on_fallback(record)
        return cfg

    if flagged_subscription is not None:
        subscription, error_code = flagged_subscription
        flagged_exc = build_flagged_subscription_error(subscription, error_code)
        if not fallback_eligible:
            raise flagged_exc
        sub_name = getattr(subscription, "account_name", None) or getattr(
            subscription, "id", None
        )
        failed_subject = f"{sub_name} 订阅" if sub_name else "当前订阅"
        return await _try_subscription_fallback(
            original_model=None,
            failed_subject=failed_subject,
            failed_provider=getattr(subscription, "id", None),
            reason="oauth_reauth_required",
            exc_to_raise=flagged_exc,
        )

    attempted_models: list[str | None] = []

    async def _tracked_resolve(model_name: str | None) -> Any:
        attempted_models.append(model_name)
        return await _effective_resolve(model_name)

    if inherited_model is None:
        if (
            fallback_eligible
            and target_exec
            and target_exec.get("_model_read_failed")
            and not requested_model
            and not held_model
        ):
            raise _automation_model_unreadable()
        if not fallback_eligible:
            return await thread_model.resolve_turn_config(
                user_id,
                thread_id,
                named=requested_model,
                held=held_model,
                fall_back=from_service,
                resolve=_effective_resolve,
            )
        with capture_resolved_effective_model() as captured_model:
            try:
                return await thread_model.resolve_turn_config(
                    user_id,
                    thread_id,
                    named=requested_model,
                    held=held_model,
                    fall_back=False,
                    resolve=_tracked_resolve,
                )
            except HTTPException as exc:
                if not is_subscription_unavailable_error(exc):
                    raise
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                rejected_model = (
                    (captured_model[-1] if captured_model else None)
                    or extract_rejected_model_from_exc(exc)
                    or (attempted_models[-1] if attempted_models else requested_model)
                )
                return await _try_subscription_fallback(
                    original_model=rejected_model,
                    failed_provider=detail.get("provider"),
                    reason=detail.get("type"),
                    exc_to_raise=exc,
                )

    try:
        with capture_resolved_effective_model() as captured_model:
            return await thread_model.resolve_turn_config(
                user_id,
                thread_id,
                named=inherited_model,
                held=held_model,
                fall_back=False if fallback_eligible else from_service,
                resolve=_tracked_resolve,
            )
    except HTTPException as exc:
        if is_subscription_unavailable_error(exc):
            if fallback_eligible:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                rejected_model = (
                    (captured_model[-1] if captured_model else None)
                    or extract_rejected_model_from_exc(exc)
                    or (attempted_models[-1] if attempted_models else inherited_model)
                )
                return await _try_subscription_fallback(
                    original_model=rejected_model,
                    failed_provider=detail.get("provider"),
                    reason=detail.get("type"),
                    exc_to_raise=exc,
                )
            if not _is_unservable_refusal(exc):
                raise
        elif not _is_unservable_refusal(exc):
            raise
        reason = exc.detail.get("type") if isinstance(exc.detail, dict) else None
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Inherited model {inherited_model!r} for "
            f"thread={thread_id} cannot serve ({reason}); falling back to "
            f"thread/default model resolution"
        )
        return await thread_model.resolve_turn_config(
            user_id,
            thread_id,
            named=None,
            held=held_model,
            fall_back=from_service,
            resolve=_effective_resolve,
        )


def _extract_eligible_execution_and_model(
    *,
    request: Any,
    effective_model: str | None,
    run_metadata: Mapping[str, Any] | None = None,
    from_server: bool = False,
    is_shared: bool = False,
    turn_index: int | None = None,
) -> tuple[str, str] | None:
    """Validate common automation turn preconditions and return ``(exec_id, model)``."""
    if not from_server or is_shared:
        return None
    if turn_index is None or isinstance(turn_index, bool) or turn_index != 0:
        return None
    if getattr(request, "query_type", None) in NOTIFICATION_QUERY_TYPES:
        return None
    if getattr(request, "retry_of_run_id", None):
        return None
    exec_id = (
        run_metadata.get("automation_execution_id")
        if isinstance(run_metadata, Mapping)
        else None
    )
    if exec_id is None or isinstance(exec_id, bool) or not str(exec_id).strip():
        return None
    if isinstance(run_metadata, Mapping) and run_metadata.get("subscription_fallback"):
        return None
    if isinstance(effective_model, str) and effective_model.strip():
        return str(exec_id).strip(), effective_model.strip()
    return None


def resolve_automation_thread_model_to_remember(
    *,
    request: Any,
    effective_model: str | None,
    run_metadata: Mapping[str, Any] | None = None,
    from_server: bool = False,
    is_shared: bool = False,
    turn_index: int | None = None,
    thread_strategy: str | None = None,
) -> str | None:
    """Return the model to persist onto ``conversation_threads.llm_model``, or ``None``.

    Persists only on the first turn (``turn_index == 0``) of an executor-started
    automation run (requiring non-empty ``run_metadata["automation_execution_id"]``;
    ``origin.type == "automation"`` alone is insufficient) whose automation uses
    ``thread_strategy == "new"`` in a non-shared workspace. Automations that reuse a
    pinned thread (``thread_strategy == "continue"``, including a pre-created empty
    thread) and subsequent turns are skipped so threads meant to follow the account
    default stay NULL. Turns that triggered ``automation-subscription-fallback``
    (``run_metadata["subscription_fallback"]``) also persist nothing.
    """
    extracted = _extract_eligible_execution_and_model(
        request=request,
        effective_model=effective_model,
        run_metadata=run_metadata,
        from_server=from_server,
        is_shared=is_shared,
        turn_index=turn_index,
    )
    if extracted is None:
        return None
    strategy = thread_strategy
    if strategy is None and isinstance(run_metadata, Mapping):
        raw_strategy = run_metadata.get("thread_strategy")
        if isinstance(raw_strategy, str):
            strategy = raw_strategy
    if not isinstance(strategy, str) or strategy.strip().lower() != "new":
        return None
    return extracted[1]


async def remember_thread_llm_model_if_unset(
    conversation_thread_id: str,
    llm_model: str,
    *,
    conn: Any = None,
) -> bool:
    """Persist ``llm_model`` onto ``conversation_threads.llm_model`` only while NULL.

    Uses ``WHERE llm_model IS NULL`` so any model set manually by the user
    (via web UI or ``switch_thread_model``) or by an earlier turn is preserved.
    Leaves ``updated_at`` untouched.
    """
    thread_id = str(conversation_thread_id or "").strip()
    model = str(llm_model or "").strip()
    if not thread_id or not model:
        return False
    sql = """
        UPDATE conversation_threads
        SET llm_model = %s
        WHERE conversation_thread_id = %s
          AND llm_model IS NULL
    """
    if conn is not None:
        async with conn.cursor() as cur:
            await cur.execute(sql, (model, thread_id))
            return cur.rowcount > 0
    async with get_db_connection() as own_conn:
        async with own_conn.cursor() as cur:
            await cur.execute(sql, (model, thread_id))
            return cur.rowcount > 0


async def remember_automation_thread_model(
    *,
    thread_id: str,
    request: Any,
    effective_model: str | None,
    run_metadata: Mapping[str, Any] | None = None,
    from_server: bool = False,
    is_shared: bool = False,
    turn_index: int | None = None,
    thread_strategy: str | None = None,
    conn: Any = None,
) -> bool:
    """Remember a ``thread_strategy='new'`` automation turn's model when NULL."""
    extracted = _extract_eligible_execution_and_model(
        request=request,
        effective_model=effective_model,
        run_metadata=run_metadata,
        from_server=from_server,
        is_shared=is_shared,
        turn_index=turn_index,
    )
    if extracted is None:
        return False
    exec_id, _ = extracted
    resolved_strategy = thread_strategy
    if resolved_strategy is None and isinstance(run_metadata, Mapping):
        raw_strategy = run_metadata.get("thread_strategy")
        if isinstance(raw_strategy, str):
            resolved_strategy = raw_strategy
    if resolved_strategy is None:
        norm_exec_id = normalize_uuid(exec_id)
        if norm_exec_id is None:
            return False
        try:
            exec_row = (
                await _get_execution_by_id(norm_exec_id, conn=conn)
                if conn is not None
                else await _get_execution_by_id(norm_exec_id)
            )
        except Exception:
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Failed reading thread_strategy for execution_id={norm_exec_id} thread={thread_id}",
                exc_info=True,
            )
            return False
        if not exec_row:
            return False
        raw_db_strategy = exec_row.get("thread_strategy")
        resolved_strategy = str(raw_db_strategy) if isinstance(raw_db_strategy, str) else ""
    model = resolve_automation_thread_model_to_remember(
        request=request,
        effective_model=effective_model,
        run_metadata=run_metadata,
        from_server=from_server,
        is_shared=is_shared,
        turn_index=turn_index,
        thread_strategy=resolved_strategy,
    )
    if not model:
        return False
    try:
        updated = await remember_thread_llm_model_if_unset(
            thread_id, model, conn=conn
        )
        if updated:
            logger.info(
                f"[AUTOMATION_THREAD_MODEL] Remembered model {model!r} on thread={thread_id}"
            )
        return updated
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed persisting model {model!r} onto thread={thread_id}",
            exc_info=True,
        )
        return False
