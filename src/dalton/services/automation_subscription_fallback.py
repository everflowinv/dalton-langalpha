"""Subscription-unavailable model fallback and fallback-notice surfacing for automations.

Only automation turns (AutomationExecutor first turns and server-initiated
notification turns belonging to an automation execution) fall back to the
user's configured ``fallback_models`` when the primary model fails due to an
unavailable subscription (``oauth_reauth_required``,
``oauth_plan_no_api_access``, ``oauth_refresh_unavailable``, or the messaging
flagged-subscription gate). Interactive chat turns keep failing loudly.

Known limitation: if a report-back turn arrives after its automation execution
has already reached a terminal state and triggers subscription fallback, the
notice remains in that turn's run metadata (``subscription_fallback``) without
updating the already-settled execution summary or delivery text.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.services.llm import thread_model, user_models
from src.server.services.llm.availability import (
    model_resolves,
    raise_oauth_reauth_required,
)
from src.server.services.llm.clients import subscription_route
from src.server.services.llm.config import deployment_llm, resolve_llm_config
from src.server.services.llm.user_models import ModelSource
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)

SUBSCRIPTION_UNAVAILABLE_REFUSALS = frozenset(
    {
        "oauth_reauth_required",
        "oauth_plan_no_api_access",
        "oauth_refresh_unavailable",
    }
)

_EXCERPT_MAX_CHARS = 800


def is_subscription_unavailable_error(exc: BaseException) -> bool:
    """Return True when *exc* is a subscription-unavailable HTTP refusal."""
    return (
        isinstance(exc, HTTPException)
        and isinstance(exc.detail, dict)
        and exc.detail.get("type") in SUBSCRIPTION_UNAVAILABLE_REFUSALS
    )


def format_subscription_fallback_notice(
    original_model: str, fallback_model: str
) -> str:
    """Build the user-visible notice recorded on execution results/deliveries."""
    sep = "" if original_model.endswith("订阅") else " "
    return (
        f"{original_model}{sep}的登录已失效，本次改用 {fallback_model}；"
        f"请到设置里重新连接"
    )


def build_subscription_fallback_record(
    *,
    original_model: str,
    fallback_model: str,
    provider: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build the structured metadata payload stored on ``conversation_responses``."""
    return {
        "original_model": original_model,
        "fallback_model": fallback_model,
        "provider": provider,
        "reason": reason,
        "notice": format_subscription_fallback_notice(original_model, fallback_model),
    }


def is_automation_executor_turn(
    request: Any, run_metadata: Mapping[str, Any] | None
) -> bool:
    """Return True when ``astream_ptc_workflow`` is running an automation firing."""
    if isinstance(run_metadata, Mapping) and run_metadata.get(
        "automation_execution_id"
    ):
        return True
    origin = getattr(request, "origin", None)
    return getattr(origin, "type", None) == "automation"


def build_flagged_subscription_error(
    subscription: Any, code: str | None
) -> HTTPException:
    """Build the exact ``oauth_reauth_required`` HTTPException from ``availability``."""
    try:
        raise_oauth_reauth_required(subscription, None, code)
    except HTTPException as exc:
        return exc
    raise AssertionError("raise_oauth_reauth_required must raise HTTPException")


async def _subscription_provider_for_model(
    user_id: str,
    model_name: str,
    prefs: dict[str, Any],
) -> str | None:
    """Return the subscription provider ID serving *model_name*, or ``None``."""
    try:
        from src.llms.llm import LLM as LLMFactory

        mc = LLMFactory.get_model_config()
        source, entry = await user_models.classify_model(
            user_id, model_name, _pref_cache=prefs
        )
        route = subscription_route(
            model_name,
            mc,
            custom_entry=entry if source == ModelSource.CUSTOM else None,
        )
        return route.subscription.id if route is not None else None
    except Exception:
        logger.debug(
            "[AUTOMATION_SUB_FALLBACK] Could not inspect subscription route for %r",
            model_name,
            exc_info=True,
        )
        return None


_REAUTH_MODEL_RE = re.compile(r"Reconnect it to use '([^']+)'")

_resolved_effective_model: ContextVar[list[str] | None] = ContextVar(
    "_resolved_effective_model", default=None
)


@contextmanager
def capture_resolved_effective_model():
    """Capture ``selection.effective_model`` selected inside ``resolve_llm_config``."""
    sink: list[str] = []
    token = _resolved_effective_model.set(sink)
    try:
        yield sink
    finally:
        _resolved_effective_model.reset(token)


def note_resolved_effective_model(model_name: str | None) -> None:
    """Record the effective model chosen by ``resolve_llm_config`` when captured."""
    sink = _resolved_effective_model.get()
    if sink is not None and isinstance(model_name, str) and model_name.strip():
        sink[:] = [model_name.strip()]


def extract_rejected_model_from_exc(exc: BaseException) -> str | None:
    """Extract the model name from ``oauth_reauth_required`` error text when present."""
    if not isinstance(exc, HTTPException) or not isinstance(exc.detail, Mapping):
        return None
    msg = exc.detail.get("message")
    if not isinstance(msg, str):
        return None
    match = _REAUTH_MODEL_RE.search(msg)
    return match.group(1) if match else None


def _is_quota_outage(exc: HTTPException) -> bool:
    return (
        exc.status_code == 503
        and isinstance(exc.detail, dict)
        and exc.detail.get("type") == "service_unavailable"
    )


async def load_fallback_prefs_or_reraise(
    user_id: str,
    exc: BaseException,
    *,
    context: str = "",
) -> dict[str, Any]:
    """Load user model preferences for fallback; re-raise *exc* if the read fails."""
    try:
        return await user_models.get_model_preference(user_id)
    except Exception as pref_exc:
        logger.warning(
            "[AUTOMATION_SUB_FALLBACK] Failed loading fallback_models (%s); "
            "re-raising original refusal (%s)",
            context or f"user={user_id}",
            type(pref_exc).__name__,
            exc_info=True,
        )
        raise exc from pref_exc


async def resolve_with_subscription_fallback(
    *,
    user_id: str,
    prefs: dict[str, Any],
    original_model: str | None,
    failed_provider: str | None,
    reason: str | None,
    resolve_candidate: Callable[[str], Awaitable[Any]],
    exc_to_raise: BaseException,
    model_resolves_fn: Callable[[dict[str, Any], str], bool] = model_resolves,
    failed_subject: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Try each usable fallback model not on *failed_provider*.

    Uses the user's saved ``prefs['fallback_models']`` when present (an explicit
    empty list ``[]`` disables fallback). When the user has never saved
    ``fallback_models`` (missing key or ``None``), falls back to the merged
    deployment default ``llm.fallback``.

    Raises *exc_to_raise* if no fallback candidate can serve the turn.
    """
    saved_candidates = prefs.get("fallback_models")
    if saved_candidates is None:
        llm_cfg = deployment_llm()
        candidates = list(llm_cfg.fallback) if llm_cfg and llm_cfg.fallback else []
    else:
        candidates = saved_candidates
    if not isinstance(candidates, list) or not candidates:
        raise exc_to_raise

    if not failed_provider and original_model:
        failed_provider = await _subscription_provider_for_model(
            user_id, original_model, prefs
        )

    seen: set[str] = set()
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, str):
            continue
        candidate = raw_candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)

        if original_model and candidate == original_model:
            continue
        if not model_resolves_fn(prefs, candidate):
            continue

        if failed_provider:
            cand_provider = await _subscription_provider_for_model(
                user_id, candidate, prefs
            )
            if cand_provider == failed_provider:
                continue

        try:
            config = await resolve_candidate(candidate)
        except HTTPException as cand_exc:
            if _is_quota_outage(cand_exc):
                raise
            logger.debug(
                "[AUTOMATION_SUB_FALLBACK] Candidate %r failed for user=%s: %s",
                candidate,
                user_id,
                cand_exc.detail,
            )
            continue
        except ValueError as cand_exc:
            logger.debug(
                "[AUTOMATION_SUB_FALLBACK] Candidate %r invalid for user=%s: %s",
                candidate,
                user_id,
                cand_exc,
            )
            continue
        except Exception as cand_exc:
            logger.warning(
                "[AUTOMATION_SUB_FALLBACK] Candidate %r resolution raised %s for user=%s: %s",
                candidate,
                type(cand_exc).__name__,
                user_id,
                cand_exc,
            )
            continue

        if getattr(config, "llm_client", None) is None:
            builder = getattr(config, "get_llm_client", None)
            if not callable(builder):
                logger.debug(
                    "[AUTOMATION_SUB_FALLBACK] Candidate %r resolved with llm_client=None and no builder for user=%s; skipping",
                    candidate,
                    user_id,
                )
                continue
            try:
                built_client = builder()
            except HTTPException as build_exc:
                if _is_quota_outage(build_exc):
                    raise
                logger.debug(
                    "[AUTOMATION_SUB_FALLBACK] Candidate %r client build failed for user=%s: %s",
                    candidate,
                    user_id,
                    build_exc.detail,
                )
                continue
            except Exception as build_exc:
                logger.debug(
                    "[AUTOMATION_SUB_FALLBACK] Candidate %r client build raised %s for user=%s: %s",
                    candidate,
                    type(build_exc).__name__,
                    user_id,
                    build_exc,
                )
                continue
            if built_client is None:
                logger.debug(
                    "[AUTOMATION_SUB_FALLBACK] Candidate %r client build returned None for user=%s; skipping",
                    candidate,
                    user_id,
                )
                continue

        chosen_model = getattr(getattr(config, "llm", None), "name", None) or candidate
        orig_label = (
            failed_subject
            or original_model
            or (f"{failed_provider} 订阅" if failed_provider else "当前订阅")
        )
        record = build_subscription_fallback_record(
            original_model=orig_label,
            fallback_model=chosen_model,
            provider=failed_provider,
            reason=reason,
        )
        logger.warning(
            "[AUTOMATION_SUB_FALLBACK] user=%s primary=%r (%s provider=%s) -> fallback=%r",
            user_id,
            orig_label,
            reason,
            failed_provider,
            chosen_model,
        )
        return config, record

    raise exc_to_raise


async def resolve_automation_turn_llm_config(
    base_config: Any,
    user_id: str,
    request_model: str | None,
    is_byok: bool | None = None,
    *,
    reasoning_effort: str | None = None,
    fast_mode: bool | None = None,
    thread_id: str | None = None,
    enabled_subagents: list[str] | None = None,
    workspace_id: str | None = None,
    identity: Any = None,
    role: Any = "analyst",
) -> tuple[Any, dict[str, Any] | None]:
    """Resolve an automation first turn's LLM config with subscription fallback."""
    async def _resolve(model_name: str | None) -> Any:
        return await resolve_llm_config(
            base_config,
            user_id,
            model_name,
            is_byok,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
            thread_id=thread_id,
            enabled_subagents=enabled_subagents,
            workspace_id=workspace_id,
            identity=identity,
            role=role,
        )

    with capture_resolved_effective_model() as captured_model:
        try:
            return await _resolve(request_model), None
        except HTTPException as exc:
            if not is_subscription_unavailable_error(exc):
                raise
            prefs = await load_fallback_prefs_or_reraise(
                user_id, exc, context=f"user={user_id}"
            )
            original_model = (
                (captured_model[-1] if captured_model else None)
                or extract_rejected_model_from_exc(exc)
                or request_model
                or thread_model.effective_default_model(prefs)
            )
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return await resolve_with_subscription_fallback(
                user_id=user_id,
                prefs=prefs,
                original_model=original_model,
                failed_provider=detail.get("provider"),
                reason=detail.get("type"),
                resolve_candidate=_resolve,
                exc_to_raise=exc,
            )


async def resolve_ptc_turn_llm_config(
    base_config: Any,
    user_id: str,
    request: Any,
    *,
    is_byok: bool | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    identity: Any = None,
    role: Any = "analyst",
    run_metadata: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Resolve LLM config for ``ptc_run`` with automation subscription fallback when eligible."""
    if is_automation_executor_turn(request, run_metadata):
        config, fallback_record = await resolve_automation_turn_llm_config(
            base_config,
            user_id,
            getattr(request, "llm_model", None),
            is_byok,
            reasoning_effort=getattr(request, "reasoning_effort", None),
            fast_mode=getattr(request, "fast_mode", None),
            thread_id=thread_id,
            enabled_subagents=getattr(request, "subagents_enabled", None),
            workspace_id=workspace_id,
            identity=identity,
            role=role,
        )
        if fallback_record is not None:
            run_metadata = {
                **(run_metadata or {}),
                "subscription_fallback": fallback_record,
            }
        return config, run_metadata

    config = await resolve_llm_config(
        base_config,
        user_id,
        getattr(request, "llm_model", None),
        is_byok,
        reasoning_effort=getattr(request, "reasoning_effort", None),
        fast_mode=getattr(request, "fast_mode", None),
        thread_id=thread_id,
        enabled_subagents=getattr(request, "subagents_enabled", None),
        workspace_id=workspace_id,
        identity=identity,
        role=role,
    )
    return config, run_metadata


def _notice_from_metadata(metadata: Any) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    fb = metadata.get("subscription_fallback")
    if isinstance(fb, Mapping):
        notice = fb.get("notice")
        if isinstance(notice, str) and notice.strip():
            return notice.strip()
    return None


def execution_has_fallback_marker(exec_row: Mapping[str, Any] | None) -> bool:
    """Return True when an execution's ``delivery`` JSONB records a fallback occurrence."""
    if not isinstance(exec_row, Mapping):
        return False
    delivery = exec_row.get("delivery")
    return isinstance(delivery, Mapping) and bool(delivery.get("subscription_fallback"))


async def mark_execution_subscription_fallback(
    execution_id: str,
    *,
    conn: Any = None,
) -> None:
    """Stamp ``subscription_fallback: true`` onto ``automation_executions.delivery`` inside START."""
    exec_uuid = normalize_uuid(str(execution_id or "").strip())
    if exec_uuid is None:
        return
    sql = """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            COALESCE(delivery, '{}'::jsonb),
            '{subscription_fallback}',
            'true'::jsonb,
            true
        )
        WHERE automation_execution_id = %s::uuid
    """
    if conn is not None:
        async with conn.cursor() as cur:
            await cur.execute(sql, (exec_uuid,))
        return
    async with get_db_connection() as own:
        async with own.cursor() as cur:
            await cur.execute(sql, (exec_uuid,))


async def get_execution_fallback_notice(
    execution_id: str,
    *,
    thread_id: str | None = None,
    run: Mapping[str, Any] | None = None,
    exec_row: Mapping[str, Any] | None = None,
) -> str | None:
    """Return deduplicated fallback notices across turns of *execution_id* in order.

    Skips querying ``conversation_responses`` when neither the settling *run*
    nor *exec_row* carries a fallback marker, and scopes DB lookups by
    ``conversation_thread_id`` so PostgreSQL uses its thread index.
    """
    run_notice = _notice_from_metadata((run or {}).get("metadata"))
    if not run_notice and not execution_has_fallback_marker(exec_row):
        return None

    exec_text = str(execution_id or "").strip()
    tid_uuid = normalize_uuid(str(thread_id or "").strip()) if thread_id else None
    notices: list[str] = []
    seen: set[str] = set()

    def _add(notice: str | None) -> None:
        if notice and notice not in seen:
            seen.add(notice)
            notices.append(notice)

    if exec_text:
        thread_clause = "AND r.conversation_thread_id = %(tid)s::uuid" if tid_uuid else ""
        params: dict[str, Any] = {"eid": exec_text}
        if tid_uuid:
            params["tid"] = tid_uuid
        try:
            async with get_db_connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        SELECT r.metadata
                        FROM conversation_responses r
                        WHERE r.metadata ? 'subscription_fallback'
                          {thread_clause}
                          AND (
                            r.metadata->>'automation_execution_id' = %(eid)s
                            OR r.metadata->'subscription_fallback'->>'execution_id' = %(eid)s
                            OR split_part(COALESCE(r.metadata->'automation_delivery'->>'id', ''), '.', 1) = %(eid)s
                            OR EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(
                                CASE
                                  WHEN jsonb_typeof(r.metadata->'automation_earlier_runs') = 'array'
                                  THEN r.metadata->'automation_earlier_runs'
                                  ELSE '[]'::jsonb
                                END
                              ) AS elem
                              WHERE split_part(COALESCE(elem->>'id', ''), '.', 1) = %(eid)s
                            )
                            OR r.conversation_response_id IN (
                              SELECT e.conversation_response_id
                              FROM automation_executions e
                              WHERE e.automation_execution_id::text = %(eid)s
                                AND e.conversation_response_id IS NOT NULL
                            )
                          )
                        ORDER BY r.run_seq ASC NULLS LAST,
                                 r.created_at ASC,
                                 r.turn_index ASC,
                                 r.attempt_no ASC
                        """,
                        params,
                    )
                    rows = await cur.fetchall()
            for row in rows or ():
                _add(
                    _notice_from_metadata(
                        row.get("metadata") if isinstance(row, Mapping) else None
                    )
                )
        except Exception:
            logger.debug(
                "[AUTOMATION_SUB_FALLBACK] Failed reading fallback notice for execution_id=%s",
                execution_id,
                exc_info=True,
            )

    _add(run_notice)
    return "\n".join(notices) if notices else None


def prepend_fallback_notice_to_excerpt(
    notice: str, answer_excerpt: str | None, *, max_chars: int = _EXCERPT_MAX_CHARS
) -> str:
    """Prepend *notice* to *answer_excerpt* strictly bounded by *max_chars*."""
    if max_chars <= 0:
        return ""
    if not answer_excerpt or len(notice) + 1 >= max_chars:
        if len(notice) <= max_chars:
            return notice
        if max_chars == 1:
            return "…"
        return notice[: max_chars - 1].rstrip() + "…"
    combined = f"{notice}\n{answer_excerpt}"
    if len(combined) <= max_chars:
        return combined
    limit = max_chars - 1
    cut = combined[:limit]
    min_cut = len(notice) + 1
    if len(cut) > min_cut:
        space = cut.rfind(" ", min_cut)
        if space > min_cut:
            cut = cut[:space]
    trimmed = cut.rstrip(" ,;:-")
    if len(trimmed) < min_cut:
        trimmed = cut
    return trimmed + "…"


def prepend_fallback_notice_to_text(notice: str, text: str | None) -> str:
    """Prepend *notice* to delivery text."""
    if text and text.strip():
        return f"{notice}\n\n{text}"
    return notice


async def apply_execution_fallback_notice(
    execution_id: str,
    *,
    thread_id: str | None = None,
    run: Mapping[str, Any] | None = None,
    exec_row: Mapping[str, Any] | None = None,
    excerpt: str | None = None,
    text: str | None = None,
    include_text: bool = False,
) -> tuple[str | None, str | None]:
    """Query fallback notices for *execution_id* and prepend them to *excerpt* (and *text* when *include_text* is True)."""
    notice = await get_execution_fallback_notice(
        execution_id, thread_id=thread_id, run=run, exec_row=exec_row
    )
    if not notice:
        return excerpt, text
    new_excerpt = prepend_fallback_notice_to_excerpt(notice, excerpt)
    new_text = prepend_fallback_notice_to_text(notice, text) if include_text else text
    return new_excerpt, new_text


async def _has_started_response_in_db(
    execution_id: str,
    *,
    thread_id: str | None = None,
    conn: Any = None,
) -> bool:
    """Return True if ``conversation_responses`` has any row whose metadata names *execution_id*."""
    exec_text = str(execution_id or "").strip()
    if not exec_text:
        return False
    tid_uuid = normalize_uuid(str(thread_id or "").strip()) if thread_id else None
    if tid_uuid:
        sql = """
            SELECT 1 AS started
            FROM conversation_responses
            WHERE conversation_thread_id = %s::uuid
              AND metadata->>'automation_execution_id' = %s
            LIMIT 1
        """
        sql_params: tuple[Any, ...] = (tid_uuid, exec_text)
    else:
        sql = """
            SELECT 1 AS started
            FROM conversation_responses
            WHERE metadata->>'automation_execution_id' = %s
            LIMIT 1
        """
        sql_params = (exec_text,)

    async def _query(c: Any) -> bool:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, sql_params)
            row = await cur.fetchone()
            if row is None:
                return False
            if isinstance(row, (Mapping, tuple, list)):
                return bool(row)
            raise TypeError(f"Unsupported fetchone() result type: {type(row)!r}")

    if conn is not None:
        if not callable(getattr(conn, "cursor", None)):
            raise TypeError(f"Unsupported DB connection without cursor(): {type(conn)!r}")
        return await _query(conn)

    async with get_db_connection() as own:
        return await _query(own)


async def execution_has_started_run(
    execution_id: str,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    settled_row: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    """Return True once *execution_id* has had a turn enter START in ``conversation_responses``."""
    if run_id is not None and str(run_id).strip():
        return True
    if isinstance(settled_row, Mapping):
        linked_run = settled_row.get("conversation_response_id")
        if linked_run is not None and str(linked_run).strip():
            return True
        if not thread_id and settled_row.get("conversation_thread_id") is not None:
            thread_id = str(settled_row.get("conversation_thread_id"))
    return await _has_started_response_in_db(execution_id, thread_id=thread_id, conn=conn)


async def release_claimed_signals_on_executor_failed_settle(
    conn: Any,
    *,
    automation: Mapping[str, Any],
    execution_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    settled_row: Mapping[str, Any] | None = None,
) -> int:
    """Release signals claimed by a Dalton execution only on synchronous executor pre-START ``failed`` settle."""
    if not isinstance(automation, Mapping):
        return 0
    from src.dalton.autonomy.preflight import is_dalton_automation
    from src.dalton.database import watch as watch_db

    if not is_dalton_automation(dict(automation)):
        return 0
    if await execution_has_started_run(
        execution_id,
        thread_id=thread_id,
        run_id=run_id,
        settled_row=settled_row,
        conn=conn,
    ):
        logger.info(
            "[AUTOMATION_SUB_FALLBACK] Keeping claimed signal(s) after START for execution_id=%s",
            execution_id,
        )
        return 0
    released = await watch_db.release_signals_claimed_by(execution_id, conn=conn)
    if released:
        logger.info(
            "[AUTOMATION_SUB_FALLBACK] Released %d pre-START claimed signal(s) on failed settle for execution_id=%s",
            released,
            execution_id,
        )
    return released
