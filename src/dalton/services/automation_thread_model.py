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

Execution model-config snapshot rules:
- ``automation_executions`` stores an immutable model-config snapshot
  (``llm_model``, ``reasoning_effort``, ``llm_model_system_only``), stamped
  once in the same transaction that creates the execution row from the same
  read of ``automations`` and never modified afterward.
- Every dispatch and continuation turn (initial firing, post-wait dispatch,
  HITL resume, wakeup, command/subagent/task report, server retry, deployment
  restart resume via ``automation_delivery`` / ``restart_from_run_id`` /
  ``/retry``, ``running_thread``, and web retry of an automation run) first
  resolves its execution via the execution-link rules, then uses that execution
  row's three snapshot fields directly. HITL resumes of an automation execution
  ignore the web composer's submitted model/effort and the thread's stored
  model.
- When no execution can be determined (including user-started commands,
  subagents, wakeups, restart resumes of ordinary user runs, or non-automation
  runs), the turn is treated as a normal non-managed owner turn
  (``llm_model_system_only = False``).
- Editing an automation's configuration after an execution starts only affects
  future executions created after the edit; in-flight executions keep the
  snapshot frozen at creation.

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
               e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
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


async def _hydrate_execution_snapshot(
    exec_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Lazily snapshot a rolling-upgrade execution row when ``snapshot_taken`` is False."""
    if exec_row is None or exec_row.get("snapshot_taken") is not False:
        return exec_row
    raw_exec_id = str(exec_row.get("automation_execution_id") or "").strip()
    if not raw_exec_id or normalize_uuid(raw_exec_id) is None:
        return exec_row
    hydrated = await exec_db.ensure_execution_snapshot(raw_exec_id)
    if hydrated is None:
        return exec_row
    return {**exec_row, **hydrated}


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
                       e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
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


def _extract_execution_id_from_metadata(meta: Mapping[str, Any] | None) -> str | None:
    """Extract the linked execution UUID from a ``conversation_responses.metadata`` dict."""
    if not isinstance(meta, Mapping):
        return None
    for key in ("automation_execution_id", "automation_source_execution_id"):
        raw = meta.get(key)
        if isinstance(raw, str) and raw.strip():
            norm = normalize_uuid(raw.strip())
            if norm is not None:
                return norm
    delivery = meta.get("automation_delivery")
    if isinstance(delivery, Mapping):
        raw_id = delivery.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            return normalize_uuid(raw_id.strip().split(".", 1)[0])
    return None


async def get_run_model_and_system_only(
    run_row: Mapping[str, Any] | None,
) -> tuple[str | None, bool | None]:
    """Read ``(model_name, system_only)`` from a single run row and its execution snapshot.

    Uses the execution link stored in ``run_row["metadata"]`` (falling back to
    ``_get_execution_for_run_id`` with that same run's id when metadata omits the
    link) so both the model name and ``llm_model_system_only`` always come from
    the same run.

    Returns ``(run_model, None)`` when reading or hydrating the execution
    snapshot fails, so callers know the flag was not resolved and can let
    handoff retry rather than locking in ``False``.
    """
    if not isinstance(run_row, Mapping):
        return None, False
    meta = run_row.get("metadata")
    run_model = (
        str(meta.get("llm_model") or "").strip() or None
        if isinstance(meta, Mapping)
        else None
    )
    exec_id = _extract_execution_id_from_metadata(meta)
    exec_row: dict[str, Any] | None = None
    try:
        if exec_id is not None:
            exec_row = await _get_execution_by_id(exec_id)
        else:
            raw_run_id = str(run_row.get("conversation_response_id") or "").strip()
            if raw_run_id and normalize_uuid(raw_run_id) is not None:
                exec_row = await _get_execution_for_run_id(raw_run_id)
    except RuntimeError as exc:
        if "pool" not in str(exc).lower():
            raise
        return run_model, None
    except Exception:
        logger.debug(
            "[AUTOMATION_THREAD_MODEL] Failed reading execution snapshot for run handoff",
            exc_info=True,
        )
        return run_model, None
    if exec_row is None:
        if exec_id is not None:
            return run_model, None
        return run_model, False
    if exec_row.get("snapshot_taken") is False:
        try:
            hydrated = await _hydrate_execution_snapshot(exec_row)
        except RuntimeError as exc:
            if "pool" not in str(exc).lower():
                raise
            return run_model, None
        except Exception:
            logger.debug(
                "[AUTOMATION_THREAD_MODEL] Failed hydrating execution snapshot for run handoff",
                exc_info=True,
            )
            return run_model, None
        if hydrated is None or hydrated.get("snapshot_taken") is not True:
            return run_model, None
        exec_row = hydrated
    if run_model is None and exec_row.get("llm_model"):
        run_model = str(exec_row["llm_model"]).strip() or None
    return run_model, bool(exec_row.get("llm_model_system_only"))


async def get_run_execution_system_only(run_id: str | None) -> bool:
    """Return the execution-level ``llm_model_system_only`` flag for ``run_id``.

    Returns ``False`` when ``run_id`` is missing, does not belong to any
    automation execution, or cannot be read (treated as a non-managed owner turn).
    """
    if not run_id or normalize_uuid(str(run_id)) is None:
        return False
    try:
        exec_row = await _get_execution_for_run_id(str(run_id))
    except RuntimeError as exc:
        if "pool" in str(exc).lower():
            return False
        raise
    except Exception:
        logger.debug(
            "[AUTOMATION_THREAD_MODEL] Failed reading execution system-only flag for run_id=%s",
            run_id,
            exc_info=True,
        )
        return False
    return bool(exec_row.get("llm_model_system_only")) if exec_row else False


async def _get_hitl_interrupt_run_id(thread_id: str, request: Any) -> str | None:
    """Resolve the ``run_id`` of the checkpoint holding the pending HITL interrupt.

    Follows the same checkpoint walk as ``report_back.notify_turn._card_waits`` so a
    subsequent failed or cancelled attempt that never wrote a new graph step cannot
    mask the run that originally produced the pending interrupt card.
    """
    if normalize_uuid(thread_id) is None:
        return None
    from src.server.utils.checkpoint_helpers import (
        build_checkpoint_config,
        get_checkpointer,
        interrupt_records,
        turn_base_checkpoint_id,
    )

    try:
        checkpointer = get_checkpointer()
    except (HTTPException, RuntimeError):
        return None
    except Exception:
        logger.debug(
            "[AUTOMATION_THREAD_MODEL] Checkpointer unavailable for HITL interrupt lookup on thread=%s",
            thread_id,
            exc_info=True,
        )
        return None

    try:
        base = getattr(request, "checkpoint_id", None) or await turn_base_checkpoint_id(
            checkpointer, thread_id
        )
        cp = await checkpointer.aget_tuple(build_checkpoint_config(thread_id, base))
        while cp is not None and (cp.metadata or {}).get("source") == "update":
            if cp.parent_config is None:
                return None
            parent = await checkpointer.aget_tuple(cp.parent_config)
            if parent is None or (
                parent.checkpoint["versions_seen"] != cp.checkpoint["versions_seen"]
            ):
                return None
            cp = parent
        if cp is None or not interrupt_records(cp.pending_writes):
            return None
        raw_run_id = (cp.metadata or {}).get("run_id")
        if not raw_run_id:
            return None
        return normalize_uuid(str(raw_run_id).strip())
    except Exception:
        logger.debug(
            "[AUTOMATION_THREAD_MODEL] Failed reading pending HITL interrupt run_id for thread=%s",
            thread_id,
            exc_info=True,
        )
        return None


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
                       e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
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
                       e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
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
        "restart_resume_run",
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


def _restart_from_run_id(request: Any) -> str | None:
    raw = getattr(request, "restart_from_run_id", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return normalize_uuid(raw.strip())


def _execution_id_matches(
    exec_row: Mapping[str, Any] | None,
    expected_execution_id: str,
) -> bool:
    if exec_row is None:
        return False
    expected = normalize_uuid(expected_execution_id)
    if expected is None:
        return False
    actual = normalize_uuid(str(exec_row.get("automation_execution_id") or ""))
    return actual == expected


async def _resolve_source_execution(
    request: Any,
    *,
    thread_id: str,
    retry_of_run_id: str | None = None,
    known_execution_id: str | None = None,
    allow_running_fallback: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the execution this notification turn belongs to.

    Explicit source hints (``retry_of_run_id``, explicit execution id, handoff
    ``automation_delivery``/``automation_earlier_runs``, ``restart_from_run_id``,
    ``report_source`` for ``command_report``/``task``, and
    ``wakeups.created_by_run_id`` for ``wakeup``) take precedence over falling
    back to the thread's latest ``running`` execution. When
    ``known_execution_id`` is provided, every matched row is verified against it
    and resolution never falls through to unrelated fallback lookups.
    """
    norm_known_exec_id = (
        normalize_uuid(known_execution_id)
        if known_execution_id is not None
        else None
    )
    if known_execution_id is not None and norm_known_exec_id is None:
        return None, "none"

    if retry_of_run_id:
        exec_row = await _get_execution_for_run_id(str(retry_of_run_id))
        if exec_row is not None:
            if norm_known_exec_id is not None and not _execution_id_matches(
                exec_row, norm_known_exec_id
            ):
                return None, "none"
            return exec_row, "retry_run"

    explicit_exec_id = _explicit_execution_id(request)
    if explicit_exec_id:
        if norm_known_exec_id is not None and explicit_exec_id != norm_known_exec_id:
            return None, "none"
        exec_row = await _get_execution_by_id(explicit_exec_id)
        if exec_row is not None and (
            norm_known_exec_id is None
            or _execution_id_matches(exec_row, norm_known_exec_id)
        ):
            return exec_row, "execution_id"
        return None, "none"

    delivery_exec_id = _delivery_execution_id(request)
    if delivery_exec_id:
        if norm_known_exec_id is not None and delivery_exec_id != norm_known_exec_id:
            return None, "none"
        exec_row = await _get_execution_by_id(delivery_exec_id)
        if exec_row is not None and (
            norm_known_exec_id is None
            or _execution_id_matches(exec_row, norm_known_exec_id)
        ):
            return exec_row, "automation_delivery"
        return None, "none"

    if norm_known_exec_id is not None:
        exec_row = await _get_execution_by_id(norm_known_exec_id)
        if _execution_id_matches(exec_row, norm_known_exec_id):
            return exec_row, "execution_id"
        return None, "none"

    restart_run_id = _restart_from_run_id(request)
    if restart_run_id:
        exec_row = await _get_execution_for_run_id(restart_run_id)
        if exec_row is not None:
            return exec_row, "restart_resume_run"
        # A deployment restart resume naming an interrupted run that does not
        # belong to any automation execution (i.e. an ordinary user turn) must
        # never borrow an unrelated running automation execution on the thread.
        return None, "none"

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
            # Explicit command report whose parent run does not belong to any
            # automation execution (e.g. a user-started command) must not borrow
            # an unrelated running automation execution on the thread.
            return None, "none"
        elif kind == "task" and (task_run_id or task_id):
            exec_row = await _get_execution_for_task(
                thread_id,
                task_run_id=str(task_run_id) if task_run_id else None,
                task_id=str(task_id) if task_id else None,
            )
            if exec_row is not None:
                return exec_row, "report_source"
            return None, "none"

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

    if retry_of_run_id:
        # Retrying an explicit run that has no execution link (and whose restored
        # report_source / wakeup notices matched no execution) must not borrow
        # an unrelated running execution on the same thread.
        return None, "none"

    if not allow_running_fallback:
        return None, "none"
    running = await exec_db.get_running_executions_for_thread(thread_id)
    if not running:
        return None, "none"
    return running[-1], "running_thread"


async def _get_execution_first_turn_settings(
    execution: Mapping[str, Any],
    *,
    fallback_thread_id: str,
) -> tuple[str | None, bool, str | None]:
    """Read ``(first_model, effort_configured, first_effort)`` from the execution's first turn.

    Reads the earliest ``conversation_responses`` row for this execution
    (``turn_index ASC, attempt_no ASC``) so idempotent ``conversation_queries``
    upserts on retries cannot overwrite the first turn's recorded effort.
    """
    exec_id = str(execution["automation_execution_id"])
    exec_thread_id = str(execution.get("conversation_thread_id") or fallback_thread_id)
    if normalize_uuid(exec_thread_id) is None:
        return None, False, None
    started_at = execution.get("started_at") or execution.get("created_at")

    first_model: str | None = None
    effort_configured = False
    first_effort: str | None = None

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
                       NULLIF(r.metadata->>'automation_reasoning_effort', '') AS reasoning_effort
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
                if first_model:
                    return first_model, effort_configured, first_effort

            if started_at is None:
                return first_model, effort_configured, first_effort

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
            )


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
    retry_source_execution_id: str | None = None,
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
    is_hitl_resume = bool(getattr(request, "hitl_response", None))
    if not is_server_turn and not is_user_retry and not is_hitl_resume:
        return None, None, None, False, False, None
    if normalize_uuid(thread_id) is None:
        return None, None, None, False, False, None

    known_execution_id = (
        (normalize_uuid(retry_source_execution_id) if retry_source_execution_id else None)
        or _explicit_execution_id(request)
        or _delivery_execution_id(request)
    )
    target_exec: dict[str, Any] | None = None
    match_kind = "none"
    try:
        if is_user_retry:
            exec_row = await _get_execution_for_run_id(str(retry_of_run_id))
            if (
                exec_row is None
                and known_execution_id is None
                and getattr(request, "query_type", None) == "wakeup"
            ):
                wakeup_ids = extract_wakeup_notice_ids(request)
                if wakeup_ids:
                    for creator_run_id in await _get_wakeup_creator_run_ids(
                        thread_id, wakeup_ids
                    ):
                        exec_row = await _get_execution_for_run_id(creator_run_id)
                        if exec_row is not None:
                            break
            target_exec = exec_row
            match_kind = "user_retry_run" if exec_row is not None else "none"
        elif is_hitl_resume and not is_server_turn:
            interrupt_run_id = await _get_hitl_interrupt_run_id(thread_id, request)
            if interrupt_run_id:
                exec_row = await _get_execution_for_run_id(interrupt_run_id)
                target_exec = exec_row
                match_kind = "hitl_resume_run" if exec_row is not None else "none"
        else:
            hitl_exec_mismatch = False
            if is_hitl_resume:
                interrupt_run_id = await _get_hitl_interrupt_run_id(thread_id, request)
                if interrupt_run_id:
                    exec_row = await _get_execution_for_run_id(interrupt_run_id)
                    if exec_row is not None:
                        if known_execution_id is not None and not _execution_id_matches(
                            exec_row, known_execution_id
                        ):
                            hitl_exec_mismatch = True
                        else:
                            target_exec = exec_row
                            match_kind = "hitl_resume_run"
            if target_exec is None and not hitl_exec_mismatch:
                target_exec, match_kind = await _resolve_source_execution(
                    request,
                    thread_id=thread_id,
                    retry_of_run_id=retry_of_run_id,
                    known_execution_id=known_execution_id,
                    allow_running_fallback=allow_running_fallback,
                )
    except HTTPException:
        raise
    except Exception as exc:
        if known_execution_id is not None:
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Failed resolving confirmed execution "
                f"{known_execution_id} for thread={thread_id}; refusing turn",
                exc_info=True,
            )
            raise _automation_model_unreadable() from exc
        logger.warning(
            f"[AUTOMATION_THREAD_MODEL] Failed resolving source execution "
            f"for thread={thread_id}; falling back to default resolution",
            exc_info=True,
        )
        return None, None, None, False, False, None
    if known_execution_id is not None:
        resolved_exec_id = (
            normalize_uuid(str(target_exec.get("automation_execution_id") or ""))
            if target_exec is not None
            else None
        )
        if target_exec is None or resolved_exec_id != known_execution_id:
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Confirmed execution {known_execution_id} "
                f"not found or mismatched (resolved={resolved_exec_id}) for thread={thread_id}; refusing turn"
            )
            raise _automation_model_unreadable()
    if target_exec is None:
        return None, None, None, False, False, None

    if target_exec.get("snapshot_taken") is False:
        try:
            target_exec = await _hydrate_execution_snapshot(target_exec)
        except Exception as exc:
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Failed hydrating execution snapshot "
                f"for thread={thread_id} execution_id={target_exec.get('automation_execution_id')}",
                exc_info=True,
            )
            raise _automation_model_unreadable() from exc
        if target_exec is None or target_exec.get("snapshot_taken") is not True:
            raise _automation_model_unreadable()

    inherited_system_only = bool(target_exec.get("llm_model_system_only"))
    is_hitl_automation = match_kind == "hitl_resume_run"
    fallback_eligible = is_server_turn and match_kind in _EXPLICIT_EXECUTION_MATCH_KINDS
    # Every continuation linked to an automation execution (including web /retry,
    # HITL confirm, server retry, wakeup, report, restart resume, and running_thread)
    # uses the execution's frozen snapshot for model, reasoning effort, and
    # llm_model_system_only — ignoring composer inputs and thread settings.
    target_exec = {
        **target_exec,
        "_effort_source_confirmed": True,
        "_execution_snapshot_override": True,
        **({"_provenance_source_confirmed": True, "_hitl_automation_override": True} if is_hitl_automation else {}),
    }
    need_model = True
    need_effort = True

    exec_id = str(target_exec["automation_execution_id"])
    chosen_model: str | None = None
    source: str | None = None
    effort_configured = False
    inherited_effort: str | None = None
    has_exec_snapshot = target_exec.get("snapshot_taken") is True
    if has_exec_snapshot:
        target_exec = {**target_exec, "_has_exec_snapshot": True}
    if has_exec_snapshot:
        raw_snap_model = target_exec.get("llm_model")
        if need_model and raw_snap_model:
            chosen_model = str(raw_snap_model).strip() or None
            if chosen_model:
                source = "execution_snapshot"
        if need_effort:
            effort_configured = True
            raw_snap_effort = target_exec.get("reasoning_effort")
            inherited_effort = str(raw_snap_effort).strip() if raw_snap_effort else None
    else:
        try:
            if _get_execution_first_turn_llm_model is not _DEFAULT_GET_FIRST_TURN_LLM_MODEL:
                first_model = await _get_execution_first_turn_llm_model(
                    target_exec, fallback_thread_id=thread_id
                )
                first_effort_configured = bool(first_model)
                first_effort = None
            else:
                (
                    first_model,
                    first_effort_configured,
                    first_effort,
                ) = await _get_execution_first_turn_settings(
                    target_exec, fallback_thread_id=thread_id
                )
            if need_model and first_model:
                chosen_model = first_model
                source = "first_turn"
            if need_effort and first_effort_configured:
                effort_configured = True
                inherited_effort = first_effort

            need_db_model = need_model and not chosen_model
            need_db_effort = need_effort and not effort_configured
            if need_db_model or need_db_effort:
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
            if inherited_system_only or is_hitl_automation:
                return target_exec, None, None, False, False, None
            return None, None, None, False, False, None

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
        # Execution link is already confirmed (any match_kind): keep target_exec and
        # chosen_model so callers never drop the execution link or fall back to composer,
        # thread, or account-default models.
        return (
            target_exec,
            chosen_model,
            None,
            fallback_eligible,
            effort_configured,
            inherited_effort,
        )

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
        if has_exec_snapshot:
            # Snapshot pinned an explicit model: do NOT silently downgrade to account default.
            # Pass chosen_model through normal turn resolution so subscription fallback (when
            # eligible) or the standard model-unavailable refusal handles it identically to
            # an ordinary turn requesting an unavailable model.
            logger.warning(
                f"[AUTOMATION_THREAD_MODEL] Snapshot model {chosen_model!r} for thread={thread_id} "
                f"execution_id={exec_id} source={source} via={match_kind} is unavailable in catalog; "
                f"keeping pinned model for standard refusal/fallback"
            )
            return (
                target_exec,
                chosen_model,
                prefs,
                fallback_eligible,
                effort_configured,
                inherited_effort,
            )
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
    known_execution_id: str | None = None,
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
            known_execution_id=known_execution_id,
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
    retry_source_execution_id: str | None = None,
    run_id: str | None = None,
    payer_user_id: str | None = None,
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
    is_server_turn = _is_server_notification_turn(
        request,
        from_service=from_service,
        retry_of_run_id=retry_of_run_id,
        is_server_retry=is_server_retry,
    )
    if (
        requested_model is None
        or flagged_subscription is not None
        or bool(retry_of_run_id)
        or bool(getattr(request, "hitl_response", None))
        or is_server_turn
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
            retry_source_execution_id=retry_source_execution_id,
            allow_running_fallback=(flagged_subscription is None),
            resolve_model=(requested_model is None),
        )

    inherited_system_only = bool(
        getattr(request, "llm_model_system_only", False)
        or (target_exec is not None and target_exec.get("llm_model_system_only"))
    )
    use_execution_snapshot = bool(
        target_exec is not None and target_exec.get("_execution_snapshot_override")
    )
    confirmed_for_context = target_exec is not None and (
        use_execution_snapshot
        or fallback_eligible
        or bool(target_exec.get("_effort_source_confirmed"))
    )
    if on_automation_context is not None:
        ctx_meta: dict[str, Any] = {}
        if target_exec is not None:
            exec_id = target_exec.get("automation_execution_id")
            if exec_id:
                # Always stamp automation_source_execution_id (NOT automation_execution_id)
                # whenever an execution is matched — including running_thread with
                # llm_model_system_only=False — so subsequent wakeups, reports, retries,
                # and HITL resumes link back to this execution's snapshot.
                ctx_meta["automation_source_execution_id"] = str(exec_id)
                if confirmed_for_context and effort_configured:
                    ctx_meta["automation_reasoning_effort_configured"] = True
                    ctx_meta["automation_reasoning_effort"] = inherited_effort
        if ctx_meta:
            on_automation_context(ctx_meta)

    apply_effort_override = (
        confirmed_for_context
        and effort_configured
        and _accepts_effort_override(resolve)
    )
    apply_system_only_override = (
        inherited_system_only and _accepts_ignore_custom_models_override(resolve)
    )

    from contextlib import nullcontext

    from src.dalton.llm.unattended_fallback import (
        UnattendedTurnContext,
        acquire_fallback_candidate_live,
        bind_unattended_turn_context,
        build_wakeup_cooldown_http_exception,
        check_wakeup_deferral_admission_deadline,
        classify_unattended_source_kind,
        evaluate_and_record_wakeup_deferral,
        format_unattended_quota_notice,
        inspect_subscription_availability,
        is_unattended_turn,
        resolve_unattended_fallback_chain,
        route_fingerprint_to_dict,
    )
    from src.dalton.settings import is_unattended_quota_fallback_enabled

    eff_payer_id = payer_user_id or user_id
    unattended_quota_on = is_unattended_quota_fallback_enabled() and is_unattended_turn(
        request,
        from_service=from_service,
    )
    unattended_ctx = (
        UnattendedTurnContext(
            is_unattended=True,
            subject_user_id=user_id,
            payer_user_id=eff_payer_id,
            source_kind=classify_unattended_source_kind(request),
            ignore_custom_models=inherited_system_only,
            run_id=run_id,
            thread_id=thread_id,
        )
        if unattended_quota_on
        else None
    )

    if apply_effort_override or apply_system_only_override or unattended_ctx is not None:
        async def _effective_resolve(model_name: str | None) -> Any:
            extra_kw: dict[str, Any] = {}
            if apply_effort_override:
                extra_kw["reasoning_effort_override"] = inherited_effort
            if apply_system_only_override:
                extra_kw["ignore_custom_models_override"] = True
            with (
                bind_unattended_turn_context(unattended_ctx)
                if unattended_ctx is not None
                else nullcontext()
            ):
                return await resolve(model_name, **extra_kw)
    else:
        _effective_resolve = resolve

    # Once a continuation is linked to an automation execution, its model and
    # reasoning effort come solely from the execution snapshot — ignoring
    # composer inputs and thread-stored model (a NULL snapshot llm_model
    # inherits the account default model, never the thread's stored model).
    effective_requested_model = None if use_execution_snapshot else requested_model
    effective_held_model = (
        None
        if (
            use_execution_snapshot
            and target_exec is not None
            and (
                target_exec.get("_has_exec_snapshot")
                or target_exec.get("_hitl_automation_override")
            )
        )
        else held_model
    )

    # Pre-dispatch wakeup deferral deadline check (runs BEFORE model availability so
    # previously held wakeups and checkpoint resumes cannot bypass the 24h deadline
    # when a model becomes available again or across internal HTTP retries).
    if unattended_quota_on and getattr(request, "query_type", None) == "wakeup":
        _admit_wake = getattr(request, "wakeup", None)
        if isinstance(_admit_wake, Mapping):
            _admit_root = str(
                _admit_wake.get("root_job_id") or _admit_wake.get("job_id") or ""
            ).strip()
            if _admit_root:
                _admit_notices = _admit_wake.get("notices") or []
                _admit_wid = str(_admit_wake.get("wakeup_id") or "").strip()
                if not _admit_wid and isinstance(_admit_notices, list) and _admit_notices:
                    _first_n = _admit_notices[0]
                    if isinstance(_first_n, Mapping):
                        _admit_wid = str(
                            _first_n.get("wakeup_id") or _first_n.get("id") or ""
                        ).strip()
                await check_wakeup_deferral_admission_deadline(
                    root_job_id=_admit_root,
                    wakeup_id=_admit_wid or _admit_root,
                    thread_id=thread_id,
                    owner_user_id=user_id,
                )

    # Pre-dispatch structured cooldown & wakeup shared-deferral gate when enabled.
    if unattended_quota_on and flagged_subscription is None:
        pref_for_probe = (
            prefs
            if prefs is not None
            else await user_models.get_model_preference(user_id)
        )
        default_pref_for_probe = (
            {
                k: v
                for k, v in pref_for_probe.items()
                if k not in ("custom_models", "custom_providers")
            }
            if inherited_system_only
            else pref_for_probe
        )
        primary_probe_model = (
            inherited_model
            or effective_requested_model
            or effective_held_model
            or thread_model.effective_default_model(default_pref_for_probe)
        )
        if primary_probe_model:
            primary_avail = await inspect_subscription_availability(
                user_id,
                primary_probe_model,
                prefs=default_pref_for_probe,
                ignore_custom_models=inherited_system_only,
            )
            if not primary_avail.available:
                availabilities = [primary_avail]
                cands = resolve_unattended_fallback_chain(
                    user_prefs=pref_for_probe,
                    primary_model=primary_probe_model,
                    skip_cooldown_keys=[primary_avail.cooldown_key]
                    if primary_avail.cooldown_key
                    else (),
                    user_id=user_id,
                    ignore_custom_models=inherited_system_only,
                )
                for cand in cands:
                    cand_avail = await inspect_subscription_availability(
                        user_id,
                        cand,
                        prefs=default_pref_for_probe,
                        ignore_custom_models=inherited_system_only,
                    )
                    availabilities.append(cand_avail)
                    if not cand_avail.available:
                        continue
                    async def _cached_cand_avail(*_a: Any, **_kw: Any) -> Any:
                        return cand_avail

                    admission = await acquire_fallback_candidate_live(
                        subject_user_id=user_id,
                        payer_user_id=eff_payer_id,
                        candidate_model=cand,
                        run_id=run_id,
                        call_id=f"pre:{run_id}:{cand}" if run_id else None,
                        ignore_custom_models=inherited_system_only,
                        model_pref=default_pref_for_probe,
                        availability_fn=_cached_cand_avail,
                    )
                    if not admission.admitted:
                        continue
                    try:
                        cfg = await _effective_resolve(cand)
                    except Exception:
                        continue
                    if admission.route is not None:
                        try:
                            setattr(
                                cfg,
                                "_dalton_active_fallback_route",
                                route_fingerprint_to_dict(admission.route),
                            )
                        except Exception:
                            pass
                    chosen = getattr(getattr(cfg, "llm", None), "name", None) or cand
                    kind = (
                        "oauth_auth_unavailable"
                        if primary_avail.kind == "auth_unavailable"
                        else "subscription_quota_exhausted"
                    )
                    record: dict[str, Any] = {
                        "original_model": primary_probe_model,
                        "fallback_model": chosen,
                        "provider": primary_avail.provider,
                        "reason": primary_avail.refusal_code or kind,
                        "notice": format_unattended_quota_notice(
                            kind,
                            model_name=primary_probe_model,
                            reset_at=primary_avail.reset_at,
                            fallback_model=chosen,
                        ),
                    }
                    if target_exec and target_exec.get("automation_execution_id"):
                        record["execution_id"] = str(target_exec["automation_execution_id"])
                    if on_fallback is not None:
                        on_fallback(record)
                    return cfg

                # All candidates unavailable pre-dispatch: for wakeup turns, consult shared
                # 3-defer / 24h ledger and return dalton_subscription_cooldown (or exhausted).
                if getattr(request, "query_type", None) == "wakeup":
                    wakeup_meta = getattr(request, "wakeup", None)
                    wakeup_map = wakeup_meta if isinstance(wakeup_meta, Mapping) else {}
                    root_job_id = str(
                        wakeup_map.get("root_job_id")
                        or wakeup_map.get("job_id")
                        or getattr(request, "request_key", None)
                        or ""
                    ).strip()
                    if root_job_id:
                        job_id = str(wakeup_map.get("job_id") or root_job_id)
                        attempts = int(wakeup_map.get("attempts") or 1)
                        notices = wakeup_map.get("notices") or []
                        wakeup_id = str(wakeup_map.get("wakeup_id") or "").strip()
                        if not wakeup_id and isinstance(notices, list) and notices:
                            first = notices[0]
                            if isinstance(first, Mapping):
                                wakeup_id = str(first.get("wakeup_id") or first.get("id") or "").strip()
                        wakeup_id = wakeup_id or root_job_id
                        from src.dalton.llm.unattended_fallback import _parse_iso_or_epoch

                        decision = await evaluate_and_record_wakeup_deferral(
                            root_job_id=root_job_id,
                            wakeup_id=wakeup_id,
                            thread_id=thread_id,
                            owner_user_id=user_id,
                            step_key=f"pre:{job_id}:{attempts}",
                            availabilities=availabilities,
                            first_due_at=_parse_iso_or_epoch(wakeup_map.get("first_due_at")),
                            workspace_id=getattr(request, "workspace_id", None),
                        )
                        raise build_wakeup_cooldown_http_exception(decision)

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
            unattended_gate=unattended_quota_on,
            payer_user_id=eff_payer_id,
            run_id=run_id,
        )
        if target_exec and target_exec.get("automation_execution_id"):
            record = {
                **record,
                "execution_id": str(target_exec["automation_execution_id"]),
            }
        if on_fallback is not None:
            on_fallback(record)
        return cfg

    effective_fallback_eligible = fallback_eligible or unattended_quota_on

    def _is_sub_unavailable_for_turn(exc: HTTPException) -> bool:
        if is_subscription_unavailable_error(exc):
            return True
        if (
            unattended_quota_on
            and isinstance(exc.detail, dict)
            and exc.detail.get("type") == "oauth_required"
        ):
            return True
        return False

    if flagged_subscription is not None:
        subscription, error_code = flagged_subscription
        flagged_exc = build_flagged_subscription_error(subscription, error_code)
        if not effective_fallback_eligible:
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
            and not effective_requested_model
            and not effective_held_model
        ):
            raise _automation_model_unreadable()
        if not effective_fallback_eligible:
            return await thread_model.resolve_turn_config(
                user_id,
                thread_id,
                named=effective_requested_model,
                held=effective_held_model,
                fall_back=from_service,
                resolve=_effective_resolve,
            )
        with capture_resolved_effective_model() as captured_model:
            try:
                return await thread_model.resolve_turn_config(
                    user_id,
                    thread_id,
                    named=effective_requested_model,
                    held=effective_held_model,
                    fall_back=False,
                    resolve=_tracked_resolve,
                )
            except HTTPException as exc:
                if not _is_sub_unavailable_for_turn(exc):
                    raise
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                rejected_model = (
                    (captured_model[-1] if captured_model else None)
                    or extract_rejected_model_from_exc(exc)
                    or (attempted_models[-1] if attempted_models else effective_requested_model)
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
                held=effective_held_model,
                fall_back=False if effective_fallback_eligible else from_service,
                resolve=_tracked_resolve,
            )
    except HTTPException as exc:
        if _is_sub_unavailable_for_turn(exc):
            if effective_fallback_eligible:
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
            if (target_exec and target_exec.get("_has_exec_snapshot")) or not _is_unservable_refusal(exc):
                raise
        elif (target_exec and target_exec.get("_has_exec_snapshot")) or not _is_unservable_refusal(exc):
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
            held=effective_held_model,
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
