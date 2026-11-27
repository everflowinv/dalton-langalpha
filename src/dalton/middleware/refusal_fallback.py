"""Dalton refusal detection, user-facing notice, and cross-family backup retry.

Keeps refusal wording, surface detection, and team-catalog cross-family model
selection inside Dalton-owned code so upstream ``empty_call_retry.py`` and
``model_resilience.py`` stay minimal thin ``DALTON-HOOK(refusal-handling)``
delegates.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.messages import AIMessage

from ptc_agent.agent.transcript.render import visible_text
from src.llms.attachment_payload import is_file_block, is_image_block
from src.llms.claude_code import is_claude_code_model
from src.llms.llm import LLM as LLMFactory, get_input_modalities

logger = logging.getLogger(__name__)

# User-facing notices when all retry attempts ended with a model safety refusal.
REFUSAL_NOTICE_CHANNEL = (
    "模型拒绝回答这一轮。可能是对话里较早的内容触发了模型的安全拦截，"
    "与这次的问题无关。可以发送 /new 开始新对话后再问。"
)
REFUSAL_NOTICE_WEB = (
    "模型拒绝回答这一轮。可能是对话里较早的内容触发了模型的安全拦截，"
    "与这次的问题无关。可以新建对话后再问。"
)

# Web surfaces without a `/new` slash command.
_WEB_SURFACES = frozenset({"", "web", "market_view"})

# Exact or suffix-matched stop_reason / finish_reason values indicating refusal.
_REFUSAL_SUFFIXES = ("refusal", "prohibited_content", "content_filter")
_REFUSAL_EXACT = frozenset({"refusal", "safety", "prohibited_content", "content_filter"})

_refusal_fallback_armed: ContextVar[bool] = ContextVar(
    "dalton_refusal_fallback_armed", default=False
)
_in_refusal_fallback: ContextVar[bool] = ContextVar(
    "dalton_in_refusal_fallback", default=False
)


class ModelRefusalExhausted(Exception):
    """Raised from EmptyToolCallRetryMiddleware to ModelResilienceMiddleware
    when every retry attempt ended with a refusal and backup retry is armed."""

    def __init__(self, response: Any, *, attempts: int) -> None:
        super().__init__("model refused every retry attempt")
        self.response = response
        self.attempts = attempts


@contextmanager
def arm_refusal_fallback(*, enabled: bool) -> Iterator[None]:
    """Arm one cross-family backup retry for the primary attempt inside ModelResilienceMiddleware."""
    token = _refusal_fallback_armed.set(bool(enabled))
    try:
        yield
    finally:
        _refusal_fallback_armed.reset(token)


@contextmanager
def arm_refusal_fallback_for_call(middleware: Any, request: Any, index: int) -> Iterator[None]:
    """Arm refusal backup retry only on the primary candidate when policy/budget permit."""
    from ptc_agent.agent.middleware.model_resilience import _as_sent
    from src.llms.provider_compaction import is_compaction_request

    enabled = (
        index == 0
        and not is_compaction_request(getattr(request, "model_settings", None))
        and not _as_sent.get()
        and not middleware._credit_gate_exhausted()
    )
    with arm_refusal_fallback(enabled=enabled):
        yield


def is_refusal_fallback_armed() -> bool:
    return _refusal_fallback_armed.get()


@contextmanager
def enter_refusal_fallback() -> Iterator[None]:
    """Mark the single backup retry call so EmptyToolCallRetryMiddleware does not re-loop."""
    token_in = _in_refusal_fallback.set(True)
    token_armed = _refusal_fallback_armed.set(False)
    try:
        yield
    finally:
        _refusal_fallback_armed.reset(token_armed)
        _in_refusal_fallback.reset(token_in)


def in_refusal_fallback() -> bool:
    return _in_refusal_fallback.get()


def refusal_stop_reason(ai_msg: Any) -> str | None:
    """Return the matched refusal stop/finish reason, or ``None`` if not a refusal."""
    if not isinstance(ai_msg, AIMessage):
        return None
    meta = getattr(ai_msg, "response_metadata", None) or {}
    for key in ("stop_reason", "finish_reason"):
        reason = meta.get(key)
        if not isinstance(reason, str):
            continue
        norm = reason.strip().lower()
        if not norm:
            continue
        if norm in _REFUSAL_EXACT or norm.endswith(_REFUSAL_SUFFIXES):
            return reason
    return None


def is_refusal_reply(ai_msg: Any) -> bool:
    """True when ``ai_msg`` ended on a refusal stop/finish reason without tool calls."""
    if refusal_stop_reason(ai_msg) is None:
        return False
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return False
    return True


def all_attempts_refused(attempts: Sequence[Any]) -> bool:
    """True when at least one attempt ran and every attempt was a refusal."""
    return bool(attempts) and all(is_refusal_reply(msg) for msg in attempts)


def supports_slash_new(config: Mapping[str, Any] | None) -> bool:
    """Whether the active turn surface supports sending ``/new``."""
    if not isinstance(config, Mapping):
        return False
    configurable = config.get("configurable")
    metadata = config.get("metadata")
    platform: Any = None
    if isinstance(configurable, Mapping):
        platform = configurable.get("platform")
    if not platform and isinstance(metadata, Mapping):
        platform = metadata.get("platform")
    surface_name = str(platform or "").strip().lower().split(":", 1)[0]
    return surface_name not in _WEB_SURFACES


def refusal_notice_text(config: Mapping[str, Any] | None) -> str:
    """Return the channel (``/new``) or web (``新建对话``) refusal notice."""
    return REFUSAL_NOTICE_CHANNEL if supports_slash_new(config) else REFUSAL_NOTICE_WEB


def build_refusal_notice_response(response: Any, config: Mapping[str, Any] | None) -> Any:
    """Replace the response's reply with the user-facing refusal notice."""
    last = response.result[0]
    last_meta = dict(getattr(last, "response_metadata", None) or {})
    notice = AIMessage(
        content=refusal_notice_text(config),
        usage_metadata=getattr(last, "usage_metadata", None),
        response_metadata=last_meta,
    )
    return dataclasses.replace(response, result=[notice])


def handle_refusal_exhausted(
    response: Any,
    attempts: Sequence[Any],
    max_retries: int,
    config: Mapping[str, Any] | None,
) -> Any | None:
    """Handle exhaustion when every retry ended with a refusal.

    Returns a replacement ``ModelResponse`` when handled locally, raises
    :class:`ModelRefusalExhausted` when ``ModelResilienceMiddleware`` armed a
    cross-family backup retry, or returns ``None`` when not every attempt was a
    refusal so the caller falls back to its normal exhaustion notice.
    """
    if not all_attempts_refused(attempts):
        return None
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
    logger.warning(
        "[RefusalFallback] thread=%s all %d attempts ended with stop_reason=refusal (max_retries=%d)",
        thread_id,
        len(attempts),
        max_retries,
    )
    if is_refusal_fallback_armed():
        raise ModelRefusalExhausted(response, attempts=len(attempts))
    return build_refusal_notice_response(response, config)


def _message_modalities(messages: Sequence[Any]) -> tuple[bool, bool]:
    """Return ``(has_image, has_pdf)`` across ``messages``."""
    has_image = False
    has_pdf = False
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if not has_image and is_image_block(block):
                has_image = True
            if not has_pdf and is_file_block(block):
                has_pdf = True
            if has_image and has_pdf:
                return True, True
    return has_image, has_pdf


def _modality_score(model_key: str, *, need_image: bool, need_pdf: bool) -> int:
    """Higher score when ``model_key`` natively supports every attachment modality in history."""
    if not need_image and not need_pdf:
        return 0
    try:
        mods = get_input_modalities(model_key)
    except Exception:
        mods = frozenset()
    score = 0
    if need_image and "image" in mods:
        score += 1
    if need_pdf and "pdf" in mods:
        score += 1
    return score


async def _resolve_family(model_key: str | None, user_id: str | None) -> str:
    """Resolve canonical model family respecting the active deployment profile."""
    if not model_key:
        return "unknown"
    from src.dalton.llm.role_policy import family_of, resolve_model_family

    try:
        return await resolve_model_family(model_key, user_id=user_id)
    except Exception:
        return family_of(model_key)


async def _load_catalog_candidates() -> list[str]:
    """Ordered model keys from the team model catalog (curated setup first, then other visible models)."""
    compiled = None
    try:
        from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_async
        from src.dalton.services.team_model_catalog.service import derive_curated_setup_for_catalog

        compiled = await get_compiled_catalog_async()
        curated = derive_curated_setup_for_catalog(compiled).get("dalton_setup_candidates") or []
    except Exception as exc:
        logger.debug("[RefusalFallback] compiled catalog unavailable (%s); falling back to ModelConfig", exc)
        curated = []

    ordered: list[str] = []
    seen: set[str] = set()
    for key in curated:
        k = str(key or "").strip()
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)

    try:
        mc = compiled.model_config if compiled is not None else LLMFactory.get_model_config()
        llm_cfg = getattr(mc, "llm_config", None) or {}
    except Exception:
        llm_cfg = {}

    for key, entry in llm_cfg.items():
        if not isinstance(entry, Mapping) or entry.get("visible") is False:
            continue
        k = str(key or "").strip()
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


async def _explain_availability(
    user_id: str, *, require_tool_calling: bool
) -> dict[str, dict[str, Any]]:
    from src.dalton.llm.role_policy import explain_model_availability

    return await explain_model_availability(
        user_id=user_id,
        require_tool_calling=require_tool_calling,
    )


async def _resolve_backup_client(
    user_id: str, model_key: str, *, thread_id: str | None
) -> Any | None:
    from src.server.services.llm.clients import resolve_model_client

    resolved = await resolve_model_client(
        user_id,
        model_key,
        is_byok=True,
        allow_platform_fallback=True,
        cache_key=thread_id,
    )
    return resolved.client


async def select_cross_family_backup(
    current_model: str,
    *,
    user_id: str | None,
    require_tool_calling: bool,
    messages: Sequence[Any],
    fallbacks: Sequence[tuple[str, Any]] = (),
    quota_skipped_fn: Callable[[str, Any], Awaitable[bool]] | None = None,
    thread_id: str | None = None,
) -> tuple[str, Any] | None:
    """Pick one available different-family backup model from fallbacks or the team model catalog."""
    current_family = await _resolve_family(current_model, user_id)
    if not current_family or current_family == "unknown":
        return None

    need_image, need_pdf = _message_modalities(messages)

    async def _is_skipped(name: str, client: Any) -> bool:
        if quota_skipped_fn is None:
            return False
        try:
            return bool(await quota_skipped_fn(name, client))
        except Exception:
            return False

    # 1. Pre-resolved turn fallbacks with a different vendor family.
    fb_candidates: list[tuple[int, int, str, Any]] = []
    for idx, (fb_name, fb_client) in enumerate(fallbacks):
        if not fb_name or fb_name == current_model or fb_client is None:
            continue
        if is_claude_code_model(fb_name):
            continue
        fb_family = await _resolve_family(fb_name, user_id)
        if fb_family == "unknown" or fb_family == current_family:
            continue
        score = _modality_score(fb_name, need_image=need_image, need_pdf=need_pdf)
        fb_candidates.append((-score, idx, fb_name, fb_client))
    fb_candidates.sort(key=lambda item: (item[0], item[1]))
    for _neg_score, _idx, fb_name, fb_client in fb_candidates:
        if not await _is_skipped(fb_name, fb_client):
            return fb_name, fb_client

    # 2. Team model catalog candidates (uses existing catalog, availability, and client resolution).
    if not user_id:
        return None
    try:
        catalog_keys = await _load_catalog_candidates()
        availability = await _explain_availability(
            user_id, require_tool_calling=require_tool_calling
        )
    except Exception as exc:
        logger.warning(
            "[RefusalFallback] Unable to evaluate team catalog backup models: %s",
            type(exc).__name__,
        )
        return None

    ranked: list[tuple[int, int, str]] = []
    for idx, cand in enumerate(catalog_keys):
        if cand == current_model or is_claude_code_model(cand):
            continue
        entry = availability.get(cand)
        if not isinstance(entry, Mapping) or not entry.get("available"):
            continue
        cand_family = await _resolve_family(cand, user_id)
        if cand_family == "unknown" or cand_family == current_family:
            continue
        score = _modality_score(cand, need_image=need_image, need_pdf=need_pdf)
        ranked.append((-score, idx, cand))

    ranked.sort(key=lambda item: (item[0], item[1]))
    for _neg_score, _idx, cand in ranked:
        try:
            client = await _resolve_backup_client(user_id, cand, thread_id=thread_id)
        except Exception as exc:
            logger.debug(
                "[RefusalFallback] Skipping candidate %s (client resolution failed: %s)",
                cand,
                type(exc).__name__,
            )
            continue
        if client is None:
            continue
        if await _is_skipped(cand, client):
            continue
        return cand, client
    return None


def _reply_is_usable(response: Any) -> bool:
    result = getattr(response, "result", None) or ()
    if not result:
        return False
    msg = result[0]
    if not isinstance(msg, AIMessage):
        return False
    if is_refusal_reply(msg):
        return False
    if getattr(msg, "tool_calls", None) or getattr(msg, "invalid_tool_calls", None):
        return True
    return bool(visible_text(msg.content).strip())


def _turn_config_safe() -> dict[str, Any]:
    try:
        from langgraph.config import get_config

        cfg = get_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


async def retry_refusal_with_backup(
    middleware: Any,
    request: Any,
    clean_messages: Sequence[Any],
    handler: Callable[[Any], Awaitable[Any]],
    refusal_exc: ModelRefusalExhausted,
) -> Any:
    """Try one cross-family backup model after primary refusal exhaustion.

    Falls back to the Requirement 4 user-facing refusal notice when no backup
    model is configured/available or when the backup model also refuses or errors.
    """
    from ptc_agent.agent.middleware.model_resilience import (
        _AttemptRecord,
        _answering,
        _refuse_unfinished,
    )

    config = _turn_config_safe()
    configurable = config.get("configurable") if isinstance(config.get("configurable"), Mapping) else {}
    user_id = str(configurable.get("user_id") or getattr(middleware, "user_id", None) or "").strip() or None
    thread_id = str(configurable.get("thread_id") or "").strip() or None

    current_model = middleware._catalog_name(request) or middleware._primary_display_name(request)
    backup = await select_cross_family_backup(
        current_model,
        user_id=user_id,
        require_tool_calling=bool(getattr(request, "tools", None)),
        messages=clean_messages,
        fallbacks=getattr(middleware, "fallbacks", ()) or (),
        quota_skipped_fn=getattr(middleware, "_aquota_skipped", None),
        thread_id=thread_id,
    )
    if backup is None:
        return build_refusal_notice_response(refusal_exc.response, config)

    backup_name, backup_client = backup
    logger.warning(
        "[RefusalFallback] thread=%s primary=%s exhausted on refusal; retrying once with cross-family backup=%s",
        thread_id,
        current_model,
        backup_name,
    )
    backup_req = request.override(model=backup_client, messages=list(clean_messages))
    if hasattr(middleware, "_with_fallback_identity"):
        backup_req = middleware._with_fallback_identity(
            backup_req, request, backup_name, backup_client
        )
    if hasattr(middleware, "_emit_fallback"):
        middleware._emit_fallback(
            _AttemptRecord(current_model, refusal_exc, None, refusal_exc.attempts),
            backup_name,
            backup_client,
            request,
            from_is_primary=True,
        )

    try:
        with _answering(1), enter_refusal_fallback():
            backup_resp = await handler(backup_req)
        _refuse_unfinished(backup_resp)
    except Exception as exc:
        logger.warning(
            "[RefusalFallback] thread=%s backup=%s failed after refusal switch (%s); returning refusal notice",
            thread_id,
            backup_name,
            type(exc).__name__,
        )
        return build_refusal_notice_response(refusal_exc.response, config)

    if not _reply_is_usable(backup_resp):
        logger.warning(
            "[RefusalFallback] thread=%s backup=%s also refused or returned empty; returning refusal notice",
            thread_id,
            backup_name,
        )
        return build_refusal_notice_response(refusal_exc.response, config)

    return middleware._answered(backup_resp, backup_name, 1, request, [])


__all__ = [
    "ModelRefusalExhausted",
    "REFUSAL_NOTICE_CHANNEL",
    "REFUSAL_NOTICE_WEB",
    "all_attempts_refused",
    "arm_refusal_fallback",
    "arm_refusal_fallback_for_call",
    "build_refusal_notice_response",
    "enter_refusal_fallback",
    "handle_refusal_exhausted",
    "in_refusal_fallback",
    "is_refusal_fallback_armed",
    "is_refusal_reply",
    "refusal_notice_text",
    "refusal_stop_reason",
    "retry_refusal_with_backup",
    "select_cross_family_backup",
    "supports_slash_new",
]
