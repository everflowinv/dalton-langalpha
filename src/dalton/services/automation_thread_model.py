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

from src.dalton.services.automation_subscription_fallback import (
    build_flagged_subscription_error,
    capture_resolved_effective_model,
    extract_rejected_model_from_exc,
    is_subscription_unavailable_error,
    load_fallback_prefs_or_reraise,
    resolve_with_subscription_fallback,
)
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


def _response_execution_id_sql(alias: str) -> str:
    """SQL expression resolving execution UUID from ``automation_execution_id`` or ``automation_delivery.id``."""
    return (
        f"COALESCE("
        f"NULLIF({alias}.metadata->>'automation_execution_id', ''), "
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
    {"retry_run", "execution_id", "automation_delivery", "report_source"}
)


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
    ``automation_delivery``/``automation_earlier_runs``, and ``report_source``
    for ``command_report``/``task``) take precedence over falling back to the
    thread's latest ``running`` execution.
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

    if not allow_running_fallback:
        return None, "none"
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


def _automation_model_unreadable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "type": "automation_model_unavailable",
            "message": "暂时无法读取本次自动任务的模型设置；为避免改用其他模型，本回合已停止，请稍后重试。",
        },
    )


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
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, bool]:
    """Resolve ``(target_exec, inherited_model, user_prefs, fallback_eligible)``."""
    if not _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    ):
        return None, None, None, False
    if normalize_uuid(thread_id) is None:
        return None, None, None, False

    try:
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
        return None, None, None, False
    if target_exec is None:
        return None, None, None, False

    fallback_eligible = match_kind in _EXPLICIT_EXECUTION_MATCH_KINDS
    if not resolve_model or getattr(request, "llm_model", None):
        return target_exec, None, None, fallback_eligible

    exec_id = str(target_exec["automation_execution_id"])
    chosen_model: str | None = None
    source: str | None = None
    try:
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
    except Exception:
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed reading execution model for thread={thread_id} "
            f"execution_id={exec_id} via={match_kind}",
            exc_info=True,
        )
        if fallback_eligible:
            # Keep the confirmed link; the caller stops the turn if nothing else names
            # a model (it must not run on the account default instead).
            return {**target_exec, "_model_read_failed": True}, None, None, fallback_eligible
        return None, None, None, False

    if not chosen_model:
        return target_exec, None, None, fallback_eligible

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
            return target_exec, chosen_model, None, fallback_eligible
        # Inferred link only: the previous behavior, default resolution.
        return None, None, None, False

    if not model_resolves(prefs, chosen_model):
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Model {chosen_model!r} for thread={thread_id} "
            f"execution_id={exec_id} source={source} via={match_kind} is unavailable; "
            f"using default resolution"
        )
        return target_exec, None, prefs, fallback_eligible

    logger.info(
        f"[AUTOMATION_THREAD_MODEL] thread={thread_id} execution_id={exec_id} "
        f"model={chosen_model} source={source} via={match_kind}"
    )
    return target_exec, chosen_model, prefs, fallback_eligible


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
    _target_exec, chosen_model, _prefs, _eligible = await _resolve_automation_notification_context(
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
    resolve: Callable[[str | None], Awaitable[Any]],
    flagged_subscription: tuple[Any, str | None] | None = None,
    on_fallback: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Resolve turn LLM config, inheriting an automation execution's model with fallback.

    Only server notification turns that explicitly identify an automation
    execution (``retry_of_run_id``, explicit execution ID, ``automation_delivery``/
    ``automation_earlier_runs``, or ``report_source``) qualify for
    ``fallback_models`` subscription fallback. Thread-level ``running`` heuristics
    keep legacy inheritance only, and failed subscription fallbacks raise the
    original refusal instead of dropping to thread/account default models.
    """
    target_exec: dict[str, Any] | None = None
    inherited_model: str | None = None
    prefs: dict[str, Any] | None = None
    fallback_eligible = False
    if requested_model is None or flagged_subscription is not None or _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    ):
        (
            target_exec,
            inherited_model,
            prefs,
            fallback_eligible,
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
        orig = (
            None
            if failed_subject is not None
            else (original_model or thread_model.effective_default_model(pref_map))
        )
        cfg, record = await resolve_with_subscription_fallback(
            user_id=user_id,
            prefs=pref_map,
            original_model=orig,
            failed_provider=failed_provider,
            reason=reason,
            resolve_candidate=resolve,
            exc_to_raise=exc_to_raise,
            model_resolves_fn=model_resolves,
            failed_subject=failed_subject,
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
        return await resolve(model_name)

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
                resolve=resolve,
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
            resolve=resolve,
        )
