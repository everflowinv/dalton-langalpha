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

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

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


async def _get_execution_by_id(execution_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE e.automation_execution_id = %s::uuid
                LIMIT 1
                """,
                (execution_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def _get_execution_for_run_id(run_id: str) -> dict[str, Any] | None:
    if normalize_uuid(run_id) is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM conversation_responses r
                JOIN automation_executions e
                  -- A handoff notification records its firing as automation_delivery.id.
                  ON e.automation_execution_id::text = COALESCE(
                      r.metadata->>'automation_execution_id',
                      r.metadata->'automation_delivery'->>'id'
                  )
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
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.started_at, e.created_at,
                       a.user_id
                FROM command_runs cr
                JOIN conversation_responses pr
                  ON pr.conversation_response_id = cr.run_id
                JOIN automation_executions e
                  ON e.automation_execution_id::text = pr.metadata->>'automation_execution_id'
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
                  ON e.automation_execution_id::text = pr.metadata->>'automation_execution_id'
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE {where_sql}
                ORDER BY sr.started_at DESC
                LIMIT 1
                """,
                (thread_id, param),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def _resolve_source_execution(
    request: Any,
    *,
    thread_id: str,
    retry_of_run_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the execution this notification turn belongs to.

    Explicit source hints (``retry_of_run_id``, handoff ``automation_delivery``,
    and ``report_source`` for ``command_report`` / ``task``) take precedence over
    falling back to the thread's latest ``running`` execution.
    """
    if retry_of_run_id:
        exec_row = await _get_execution_for_run_id(str(retry_of_run_id))
        if exec_row is not None:
            return exec_row, "retry_run"

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

    running = await exec_db.get_running_executions_for_thread(thread_id)
    if not running:
        return None, "none"
    return running[-1], "running_thread"


async def _get_execution_first_turn_llm_model(
    execution: Mapping[str, Any],
    *,
    fallback_thread_id: str,
) -> str | None:
    """Find the execution's first turn ``llm_model`` without reading ``conversation_response_id``.

    ``take_over_execution_run`` advances ``automation_executions.conversation_response_id``
    to continuation turns, so we locate the first turn by ``metadata->>'automation_execution_id'``
    first and otherwise by the earliest ``origin.type == 'automation'`` turn on the
    thread at or after ``execution.started_at`` (or ``created_at``).
    """
    exec_id = str(execution["automation_execution_id"])
    exec_thread_id = str(execution.get("conversation_thread_id") or fallback_thread_id)
    if normalize_uuid(exec_thread_id) is None:
        return None
    started_at = execution.get("started_at") or execution.get("created_at")

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT q.metadata->>'llm_model' AS llm_model
                FROM conversation_responses r
                JOIN conversation_queries q
                  ON q.conversation_thread_id = r.conversation_thread_id
                 AND q.turn_index = r.turn_index
                WHERE r.conversation_thread_id = %s::uuid
                  AND r.metadata->>'automation_execution_id' = %s
                  AND q.type NOT IN ('system', 'wakeup', 'market_target')
                ORDER BY (q.metadata->'origin'->>'type' = 'automation'
                          OR r.metadata->>'run_origin' = 'automation') DESC NULLS LAST,
                         r.turn_index ASC,
                         r.attempt_no ASC
                LIMIT 1
                """,
                (exec_thread_id, exec_id),
            )
            row = await cur.fetchone()
            model = row.get("llm_model") if row else None
            if model:
                return str(model)

            if started_at is None:
                return None

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
            return str(fallback_model) if fallback_model else None


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
    if not _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    ):
        return None
    if normalize_uuid(thread_id) is None:
        return None

    try:
        target_exec, match_kind = await _resolve_source_execution(
            request,
            thread_id=thread_id,
            retry_of_run_id=retry_of_run_id,
        )
        if target_exec is None:
            return None

        exec_id = str(target_exec["automation_execution_id"])
        chosen_model = await _get_execution_first_turn_llm_model(
            target_exec, fallback_thread_id=thread_id
        )
        source = "first_turn" if chosen_model else None

        if not chosen_model:
            automation = await auto_db.get_automation_including_deleted(
                str(target_exec["automation_id"]),
                target_exec.get("user_id"),
            )
            if automation:
                cfg_model = automation.get("llm_model")
                if cfg_model:
                    chosen_model = str(cfg_model)
                    source = "automation_config"

        if not chosen_model:
            return None

        prefs = await user_models.get_model_preference(user_id)
        if not model_resolves(prefs, chosen_model):
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Model {chosen_model!r} for thread={thread_id} "
                f"execution_id={exec_id} source={source} via={match_kind} is unavailable; "
                f"using default resolution"
            )
            return None

        logger.info(
            f"[AUTOMATION_THREAD_MODEL] thread={thread_id} execution_id={exec_id} "
            f"model={chosen_model} source={source} via={match_kind}"
        )
        return chosen_model
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed resolving automation execution model "
            f"for thread={thread_id}; falling back to default resolution",
            exc_info=True,
        )
        return None


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
    resolve: Callable[[str | None], Awaitable[Any]],
) -> Any:
    """Resolve turn LLM config, inheriting an automation execution's model with fallback.

    If the inherited model triggers an unservable refusal (``_UNSERVABLE_REFUSALS``),
    log a warning and re-resolve with ``named=None`` so the thread's held model or
    account default still serves the notification turn instead of dropping it.
    """
    inherited_model: str | None = None
    if requested_model is None:
        inherited_model = await resolve_automation_notification_model(
            user_id=user_id,
            thread_id=thread_id,
            request=request,
            from_service=from_service,
            is_server_retry=is_server_retry,
            retry_of_run_id=retry_of_run_id,
        )

    if inherited_model is None:
        return await thread_model.resolve_turn_config(
            user_id,
            thread_id,
            named=requested_model,
            held=held_model,
            fall_back=from_service,
            resolve=resolve,
        )

    try:
        return await thread_model.resolve_turn_config(
            user_id,
            thread_id,
            named=inherited_model,
            held=held_model,
            fall_back=from_service,
            resolve=resolve,
        )
    except HTTPException as exc:
        if not _is_unservable_refusal(exc):
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
            resolve=resolve,
        )
