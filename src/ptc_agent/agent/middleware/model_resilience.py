"""Model resilience middleware: retry with backoff, model fallback, SSE signals.

Replaces the stock langchain ``ModelRetryMiddleware`` + ``ModelFallbackMiddleware``
pair so that:

- non-retryable errors (bad request / auth / unknown model, or a crash in
  parsing the response) skip straight to the next fallback model instead of
  burning retries on a call that cannot succeed,
- a context overflow moves on only to a fallback with a larger window, and
  otherwise goes up to compaction, the one thing that makes the request
  smaller, when the stack has compaction,
- every retry and fallback switch is emitted as a custom stream event
  (``model_retry`` / ``model_fallback``) for the client to render, and
- on total exhaustion the PRIMARY model's exception is re-raised with the full
  attempt trace attached, so the user sees the error for the model they
  configured rather than the last fallback's.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import structlog
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.types import Command

from ptc_agent.agent.middleware._utils import append_to_system_message, run_ids  # DALTON-HOOK(model-fallback-identity)
from ptc_agent.agent.middleware.thinking_binding import (
    UNSENT_THINKING_KEY,
    UnsentThinkingState,
    unsent_thinking,
    without_thinking,
)

from src.llms.error_classification import (
    DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS,  # DALTON-HOOK(quota-exhaustion)
    UnfinishedReplyError,
    extract_status_code,
    is_context_overflow,
    is_mid_stream_error,
    is_retryable_error,
    is_stalled_stream,
    quota_exhaustion_seconds,  # DALTON-HOOK(quota-exhaustion)
    refuses_input,
)
from src.llms.provider_errors import is_terminal_refusal
from src.llms.input_transformations import (
    BY_DESIGN_DROPS,
    INPUT_TRANSFORMATIONS,
    REPEATED,
    thinking_drops,
)
from src.llms.model_identity import CATALOG_MODEL_KEY, api_model_id, catalog_model, client_label  # DALTON-HOOK(model-fallback-identity)
from src.llms.provider_compaction import is_compaction_request
from src.llms.reasoning_payload import (
    is_unfinished_reply,
    strip_all_reasoning,
    strip_reasoning_references,
)
from src.observability.langsmith_run import stamp_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

# Attribute attached to the primary exception on total exhaustion. Read by the
# server's ``_build_error_event`` to enrich the SSE error payload with the
# primary model name and the per-model attempt trace.
RESILIENCE_TRACE_ATTR = "__model_resilience__"

_ERROR_SUMMARY_MAX_CHARS = 300

_QUOTA_EXHAUSTED_REDIS_PREFIX = "dalton:quota_exhausted"
_HKT = ZoneInfo("Asia/Hong_Kong")


def quota_redis_key(  # DALTON-HOOK(quota-exhaustion)
    name: str,
    client: Any,
    *,
    user_id: str | None = None,
) -> str | None:
    """Coordination key scoped by credential owner, provider, model, and subscription account."""
    if client is None:
        return None
    from src.dalton.credentials import credential_user_id
    from src.dalton.llm.antigravity_series import quota_cooldown_target  # DALTON-HOOK(antigravity-series)
    from src.dalton.llm.oauth_refresh import oauth_rebuild_spec

    spec = oauth_rebuild_spec(client)
    raw_user: str | None = None
    provider: str | None = None
    model_id = name
    cred_id = ""
    is_custom = False
    if spec is not None:
        raw_user = spec.user_id or None
        provider = spec.provider or _stamped(client, "pricing_provider")
        model_id = spec.model_name or name
        is_custom = spec.custom_entry is not None
        cred_id = (spec.credential_id or "").strip()
        if not cred_id:
            acct = (spec.account_id or "").strip()
            if acct and not acct.startswith("muse-api-key"):
                cred_id = acct
    else:
        provider = _stamped(client, "pricing_provider")
        model_id = _stamped(client, "manifest_model") or catalog_model(client) or name
        raw_user = user_id
        if not raw_user:
            try:
                from langgraph.config import get_config

                cfg_user = (get_config().get("configurable") or {}).get("user_id")
                raw_user = str(cfg_user) if cfg_user else None
            except Exception:
                raw_user = None
        if not raw_user and _stamped(client, "billing_type") == "platform":
            raw_user = "platform"

    if not raw_user or not provider or not model_id:
        return None
    if raw_user != "platform":
        try:
            cred_user = credential_user_id(str(raw_user))
        except Exception:
            cred_user = str(raw_user)
    else:
        cred_user = "platform"
    if not cred_user:
        return None
    # DALTON-HOOK(antigravity-series): built-in antigravity-oauth variants share
    # a disjoint `:series:{prefix}` namespace; other providers and custom models
    # use `:model:{model_id}`.
    scope_kind, target = quota_cooldown_target(provider, model_id, is_custom=is_custom)
    base = f"{_QUOTA_EXHAUSTED_REDIS_PREFIX}:{cred_user}:{provider}:{scope_kind}:{target}"
    return f"{base}:{cred_id}" if cred_id else base


def format_quota_cooldown_message(  # DALTON-HOOK(quota-exhaustion)
    model_name: str,
    expires_at: float | int | None,
) -> str:
    """Format an actionable pre-dispatch quota exhaustion message with HKT recovery time."""
    ts = (
        float(expires_at)
        if isinstance(expires_at, (int, float))
        else time.time() + DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS
    )
    dt_hkt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_HKT)
    when = dt_hkt.strftime("%Y-%m-%d %H:%M") + " HKT"
    label = model_name or "当前模型"
    return (
        f"{label} 的订阅额度已用完，预计 {when} 恢复；"
        "可以换用其他模型派发，或等恢复后再派"
    )


async def check_oauth_quota_cooldown(  # DALTON-HOOK(quota-exhaustion)
    client: Any,
    *,
    model_name: str = "",
    user_id: str | None = None,
) -> str | None:
    """Return a pre-dispatch refusal message when an OAuth client is in Redis cooldown."""
    if client is None:
        return None
    from src.dalton.llm.oauth_refresh import oauth_rebuild_spec

    spec = oauth_rebuild_spec(client)
    billing = _stamped(client, "billing_type")
    if billing != "oauth" and spec is None:
        return None
    label = (
        model_name
        or (spec.model_name if spec is not None else "")
        or _stamped(client, "manifest_model")
        or catalog_model(client)
        or client_label(client)
        or ""
    )
    redis_key = quota_redis_key(label, client, user_id=user_id)
    if redis_key is None:
        return None
    try:
        from src.utils.cache.redis_cache import get_cache_client

        data = await get_cache_client().get(redis_key)
    except Exception as err:
        logger.debug(
            "[ModelResilience] Redis pre-dispatch quota read unavailable, failing open",
            key=redis_key,
            error=str(err),
        )
        return None
    if not isinstance(data, dict):
        return None
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time():
        return None
    display = label or str(data.get("model") or "当前模型")
    return format_quota_cooldown_message(display, expires_at)


class _RemoteQuotaExhaustedError(RuntimeError):  # DALTON-HOOK(quota-exhaustion)
    """Refusal placeholder reconstructed from Redis cross-worker quota state."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

#: The ``response_metadata`` key naming the fallback that wrote an answer. Its
#: usage counts that model's tokens, so compaction measures nothing for the
#: primary by it.
FALLBACK_KEY = "model_fallback"

_as_sent: ContextVar[bool] = ContextVar("model_resilience_as_sent", default=False)
_fallback: ContextVar[bool] = ContextVar("model_resilience_fallback", default=False)


def answering_as_fallback() -> bool:
    """Whether the model call in progress is a fallback answering for the
    primary, whose state the thread goes back to: what the primary's
    reasoning is bound to stays as it was (``SystemPinMiddleware``)."""
    return _fallback.get()


@contextmanager
def _answering(index: int) -> Iterator[None]:
    token = _fallback.set(index > 0)
    try:
        yield
    finally:
        _fallback.reset(token)

_STRIP_ERROR_MAX_CHARS = 1000


@contextmanager
def as_sent() -> Iterator[None]:
    """Model calls made inside go to the primary as sent, retried but never
    moved to a fallback or stripped of reasoning: for an answer only that
    model reading that request can give, such as a summary of what only its
    provider can read."""
    token = _as_sent.set(True)
    try:
        yield
    finally:
        _as_sent.reset(token)


def _last_message_id(request: ModelRequest) -> str | None:
    """The newest message of the state the call was made from."""
    state = getattr(request, "state", None)
    messages = state.get("messages") if isinstance(state, dict) else None
    return getattr(messages[-1], "id", None) if messages else None


def _stripped_ids(sent: list[AnyMessage], stripped: list[AnyMessage]) -> list[str]:
    """The ids of the messages in ``sent`` the strip changed or removed."""
    kept = {id(message) for message in stripped}
    return [m.id for m in sent if id(m) not in kept and getattr(m, "id", None)]


def _keep_unsent(request: ModelRequest) -> ModelRequest:
    """``request`` with the messages sent without their reasoning sent so
    again, where no compaction view already did it."""
    messages = without_thinking(request.messages, unsent_thinking(getattr(request, "state", None)))
    return request if messages is request.messages else request.override(messages=messages)


def _report_drops(response: ModelResponse, model: str, sent: list[AnyMessage]) -> None:
    """Log and stamp the earlier thinking the API dropped before answering.

    One line per answer with any, so a thread's count is a sum over its own
    log lines and runs, whichever worker served each call. A block dropped
    again because the request still sends it counts as ``repeated``, judged
    against the newest answer in ``sent`` that carries the list, so the sum
    counts each loss once.
    """
    earlier = next(
        (
            m
            for m in reversed(sent)
            if isinstance(m, AIMessage)
            and INPUT_TRANSFORMATIONS in (getattr(m, "response_metadata", None) or {})
        ),
        None,
    )
    for message in getattr(response, "result", None) or ():
        counts = thinking_drops(message, earlier)
        if not counts:
            continue
        log = logger.info if set(counts) <= BY_DESIGN_DROPS | {REPEATED} else logger.warning
        thread_id, agent = run_ids()
        log(
            "[ModelResilience] earlier thinking dropped by the provider",
            model=model,
            thread_id=thread_id,
            agent=agent,
            **counts,
        )
        stamp_run(thinking_dropped=counts)


def _refuse_unfinished(response: Any) -> None:
    """Raise for an answer whose stream ended before its response did, so the
    loop retries it rather than the thread keeping it (``is_unfinished_reply``)."""
    result = getattr(getattr(response, "model_response", response), "result", None)
    if any(is_unfinished_reply(message) for message in result or ()):
        raise UnfinishedReplyError("the stream ended before the response completed")


def _summarize_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    # DALTON-HOOK(fallback-reason-log): scrub credentials before logging or
    # checkpointing fallback error text.
    try:
        from src.server.utils.error_sanitization import sanitize_error_text

        text = sanitize_error_text(text)
    except Exception:
        pass
    return text[:_ERROR_SUMMARY_MAX_CHARS]


@dataclass(slots=True)
class _Recovery:
    """One more attempt on the same candidate: back off, or send a fixed request.

    ``request`` set means the payload was repaired rather than merely retried,
    which the caller records so it only escalates once per candidate.
    """

    delay: float = 0.0
    request: ModelRequest | None = None


def _stamped(model: Any, key: str) -> str | None:
    """A fact ``LLM.get_llm()`` stamped in a client's metadata: the namespace
    fallback names are in, or who pays (``billing_type``). None for an
    unstamped client."""
    metadata = getattr(model, "metadata", None)
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def _window(model: Any) -> int | None:
    """The context window a client's profile declares, None when it declares none."""
    profile = getattr(model, "profile", None)
    window = profile.get("max_input_tokens") if isinstance(profile, dict) else None
    return window if isinstance(window, int) and window > 0 else None


def _mark(response: ModelResponse, marks: dict[str, str]) -> None:
    for msg in getattr(response, "result", None) or ():
        if isinstance(msg, AIMessage):
            msg.response_metadata = {**(msg.response_metadata or {}), **marks}


def _distinct(fallbacks: list[tuple[str, Any]], primary: str | None) -> list[tuple[str, Any]]:
    """``fallbacks`` without ``primary`` and without a name listed twice.

    Moving to the model that just failed re-sends the same request to it, so
    the "fallback" is one more identical attempt that fails the same way.
    """
    seen = {primary} if primary else set()
    distinct = []
    for name, client in fallbacks:
        if name not in seen:
            seen.add(name)
            distinct.append((name, client))
    return distinct


def build_fallback_pairs(config: Any) -> list[tuple[str, Any]]:
    """Resolve ``(display_name, client)`` pairs for the configured fallbacks.

    Prefers the pre-resolved OAuth/BYOK-aware clients (with the parallel names
    list kept aligned by ``_resolve_fallback_clients``); falls back to
    resolving names via the platform manifest. A name with no usable key is
    skipped, as the chat path does: a fallback that cannot run must not stop
    the graph from building for the primary.
    """
    clients = getattr(config, "fallback_llm_clients", None)
    if clients is not None:
        names = getattr(config, "fallback_llm_names", None)
        if names and len(names) == len(clients):
            return list(zip(names, clients))
        return [(api_model_id(client) or type(client).__name__, client) for client in clients]

    fallback_names = getattr(config.llm, "fallback", None) or []
    if not fallback_names:
        return []
    from src.llms import get_llm_by_type

    pairs = []
    for name in fallback_names:
        try:
            pairs.append((name, get_llm_by_type(name)))
        except Exception as e:
            logger.warning(
                "[ModelResilience] Skipping unusable fallback model",
                model=name,
                error=str(e),
            )
    return pairs


@dataclass
class _AttemptRecord:
    model: str
    exc: Exception
    status_code: int | None
    attempts: int


def _stable_lane(namespace: str) -> str:  # DALTON-HOOK(quota-exhaustion)
    """The stable lane of a checkpoint namespace: everything after the last
    ``NS_SEP`` — the per-execution segment — dropped.

    LangGraph composes a nested node's namespace as
    ``<parent>{NS_SEP}<node>{NS_END}<task id>`` (``NS_SEP`` is ``|`` and
    ``NS_END`` is ``:`` — ``langgraph/pregel/_algo.py`` ``checkpoint_ns =
    f"{parent_ns}{NS_SEP}{name}"`` / ``task_checkpoint_ns =
    f"{checkpoint_ns}{NS_END}{task_id}"``), and the task id is minted per
    execution: the main agent's model calls read ``model:<uuid>`` on one
    call and a fresh uuid the next. LangGraph's own subgraph plumbing
    derives the stable parent the same way — ``split(NS_SEP)[:-1]``
    (``langgraph/pregel/_tools.py``, ``pregel/_messages.py``). The main
    graph collapses to the root (``""``); a subagent keeps its
    ``task:<id>`` prefix.
    """
    from langgraph.constants import NS_SEP

    return NS_SEP.join(namespace.split(NS_SEP)[:-1])


class ModelResilienceMiddleware(AgentMiddleware):
    """Retry + fallback across models with client-visible progress events.

    One instance is shared across the main and subagent stacks, so all loop
    state is call-local.
    """

    state_schema = UnsentThinkingState

    def __init__(
        self,
        *,
        primary_name: str,
        primary_client: Any | None = None,
        fallbacks: list[tuple[str, Any]] | None = None,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
        compacts_overflow: bool = False,
        user_id: str | None = None,
    ) -> None:
        super().__init__()
        self.primary_name = primary_name
        self.primary_client = primary_client
        self.user_id = user_id
        # Repeats only; the model a request targets is dropped per request,
        # as the instance is shared into subagents on other models.
        self.fallbacks = _distinct(list(fallbacks or []), None)
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.jitter = jitter
        # Whether a compaction middleware outside this one takes an overflow.
        # Without one, nothing would make the request smaller, so an overflow
        # moves down the chain like any other refusal, where a fallback with a
        # longer window may take it.
        self.compacts_overflow = compacts_overflow
        # DALTON-HOOK(oauth-dispatch): clients rebuilt after a mid-turn 401, keyed by the
        # id of the client they replace (kept alongside, so the id cannot be reused). Lives
        # on this per-build instance only; every later call of the turn uses the fresh one.
        self._oauth_replacements: dict[int, tuple[Any, Any]] = {}
        # DALTON-HOOK(quota-exhaustion): candidates this run has judged out of
        # quota — a 402 balance refusal or a quota 429 whose reset outlasts
        # every backoff below — as ``name -> (refills_at_monotonic, refusal)``.
        # Instance state, like the OAuth replacements above: the instance is
        # built per turn and shared with its subagents, so the memory lasts
        # exactly one run and crosses no process boundary. A named horizon
        # re-admits the candidate once it has elapsed, so a short Retry-After
        # never benches a model for a whole turn.
        self._quota_exhausted: dict[str, tuple[float, Exception]] = {}
        # DALTON-HOOK(quota-exhaustion): (lane, benched, switched-to) pairs a
        # bench-driven switch has already announced this run.
        self._quota_notified: set[tuple[str, str, str]] = set()

    def _current_client(self, client: Any) -> Any:  # DALTON-HOOK(oauth-dispatch)
        hit = self._oauth_replacements.get(id(client))
        return hit[1] if hit is not None and hit[0] is client else client

    async def _refresh_oauth_client(self, base: Any, failed: Any) -> Any | None:  # DALTON-HOOK(oauth-dispatch)
        from src.dalton.llm.oauth_refresh import rebuild_oauth_client

        fresh = await rebuild_oauth_client(failed)
        if fresh is not None:
            self._oauth_replacements[id(base)] = (base, fresh)
        return fresh

    # ------------------------------------------------------------------
    # Helpers

    def _catalog_name(self, request: ModelRequest) -> str | None:
        """The catalog name of the model ``request`` targets, None for an
        unstamped client. The instance is shared into subagent stacks, so the
        configured name is claimed only for a request on the primary client;
        a subagent's own client names itself."""
        if self.primary_client is None or request.model is self.primary_client:
            return self.primary_name
        return catalog_model(request.model)

    def _primary_display_name(self, request: ModelRequest) -> str:
        return (
            self._catalog_name(request)
            or api_model_id(request.model)
            or type(request.model).__name__
        )

    def _with_fallback_identity(  # DALTON-HOOK(model-fallback-identity)
        self,
        req: ModelRequest,
        original_request: ModelRequest,
        fallback_name: str,
        fallback_client: Any,
    ) -> ModelRequest:
        """Attach a request-only note on fallback calls so the model knows its
        own identity and which designated model it stands in for (named
        directly, since not every graph renders a ``<model>`` block). A
        fallback that binds its reasoning to the prefix (Claude, GPT-6.2 on;
        see ``binds_reasoning``) stays untouched, so ``SystemPinMiddleware``
        keeps the pinned system message byte for byte and never stores the
        note in the pin."""
        from ptc_agent.agent.middleware.thinking_binding import binds_reasoning

        if not hasattr(req, "system_message") or binds_reasoning(fallback_client):
            return req
        primary_label = (
            client_label(original_request.model, self._catalog_name(original_request))
            or self._primary_display_name(original_request)
        )
        fallback_label = (
            client_label(fallback_client, fallback_name)
            or fallback_name
            or api_model_id(fallback_client)
            or type(fallback_client).__name__
        )
        note = (
            f"[Fallback call] This individual call was moved to fallback model "
            f"{fallback_label}; the model the server designated for this run is {primary_label}."
        )
        try:
            return req.override(
                system_message=append_to_system_message(req.system_message, note)
            )
        except TypeError:
            return req

    def _calculate_delay(self, retry_number: int) -> float:
        # Local copy of langchain.agents.middleware._retry.calculate_delay
        # (private module — not imported to survive library refactors).
        if self.backoff_factor == 0.0:
            delay = self.initial_delay
        else:
            delay = self.initial_delay * (self.backoff_factor**retry_number)
        delay = min(delay, self.max_delay)
        if self.jitter and delay > 0:
            jitter_amount = delay * 0.25
            delay += random.uniform(-jitter_amount, jitter_amount)
            delay = max(0, delay)
        return delay

    @staticmethod
    def _emit(payload: dict[str, Any]) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            if writer is not None:
                writer(payload)
        except Exception:
            # Stream writer is unavailable outside a streaming graph run
            # (tests, sync invocations); resilience must not depend on it.
            logger.debug("[ModelResilience] stream_writer unavailable, skipping event")

    def _emit_retry(
        self,
        model: str,
        attempt: int,
        exc: Exception,
        status_code: int | None,
        delay: float,
    ) -> None:
        # ``attempt`` counts calls that have failed so far; the retry about to
        # happen is attempt + 1 of max_retries + 1 total.
        self._emit(
            {
                "type": "model_retry",
                "model": model,
                "attempt": attempt,
                "max_retries": self.max_retries,
                "error": _summarize_error(exc),
                "status_code": status_code,
                "delay_seconds": round(delay, 2),
            }
        )

    def _emit_fallback(
        self,
        record: _AttemptRecord,
        to_model: str,
        to_client: Any,
        request: ModelRequest,
        *,
        from_is_primary: bool,
    ) -> None:
        # Unlike retries, fallbacks must survive replay: push_ui_message
        # dual-writes the record to the custom stream (live SSE) and the
        # checkpointed ``ui`` channel, which replay projects per turn. The
        # turn's records keep no step, so the record names the message the
        # call followed, which replay places the notice after.
        error_type = type(record.exc).__name__
        error_summary = _summarize_error(record.exc)
        props = {
            "from_model": record.model,
            "to_model": to_model,
            "from_is_primary": from_is_primary,
            "error": error_summary,
            "error_type": error_type,
            "status_code": record.status_code,
            "attempts_on_from": record.attempts,
        }
        # A switch can change who pays, so the notice says who pays for the
        # model it moved to.
        if (paid_by := _stamped(to_client, "billing_type")) is not None:
            props["to_paid_by"] = paid_by
            # The provider the call went through. A model the user added on a
            # sign-in may share its name with another provider's model, so the
            # name alone does not say which plan pays.
            if (provider := _stamped(to_client, "pricing_provider")) is not None:
                props["to_provider"] = provider
        # DALTON-HOOK(fallback-reason-log): emit a server WARNING log with
        # from_model, to_model, status_code, error_type, and sanitized error
        # snippet so mid-turn model switches are traceable in logs and durable
        # model_fallback run/ui records.
        logger.warning(
            "[ModelResilience] Switching model on fallback",
            from_model=record.model,
            to_model=to_model,
            from_is_primary=from_is_primary,
            status_code=record.status_code,
            error_type=error_type,
            attempts_on_from=record.attempts,
            error=error_summary,
        )
        try:
            from langgraph.graph.ui import push_ui_message

            after = _last_message_id(request)
            push_ui_message(
                name="model_fallback",
                props=props,
                metadata={"after_message_id": after} if after else None,
            )
        except Exception:
            # Runtime context is unavailable outside a graph run (tests, sync
            # invocations); resilience must not depend on it.
            logger.debug("[ModelResilience] ui emit unavailable, skipping fallback event")

    def _raise_exhausted(
        self, records: list[_AttemptRecord], raised: Exception | None = None
    ) -> NoReturn:
        """Raise ``raised``, else the primary's error, with the attempt trace."""
        primary = records[0]
        exc = raised or primary.exc
        trace = {
            "model": primary.model,
            "attempted_models": [
                {
                    "model": r.model,
                    "error": _summarize_error(r.exc),
                    "status_code": r.status_code,
                    "attempts": r.attempts,
                }
                for r in records
            ],
        }
        try:
            setattr(exc, RESILIENCE_TRACE_ATTR, trace)
        except Exception:
            # Exceptions with __slots__ reject arbitrary attributes; the
            # primary error still surfaces, just without the trace.
            pass
        logger.warning(
            "[ModelResilience] All models exhausted"
            if raised is None
            else "[ModelResilience] Context overflow, left to compaction",
            primary_model=primary.model,
            attempted=[r.model for r in records],
        )
        raise exc

    def _hands_up(self, exc: Exception) -> bool:
        return self.compacts_overflow and is_context_overflow(exc)

    def _raise_run_exhausted(  # DALTON-HOOK(quota-exhaustion)
        self,
        records: list[_AttemptRecord],
        candidates: list[tuple[str, Any | None]],
        request: ModelRequest,
    ) -> NoReturn:
        """The total-exhaustion raise, quota-aware.

        Every candidate benched leaves no attempt to trace — the refusal
        itself surfaces (the primary's when it is among them), never an
        empty-record crash. And when the primary sits this call out on a
        bench while the remaining candidates failed, the error the user sees
        is the primary's remembered refusal — the model they configured —
        with the fallback attempts riding along in the trace, not the last
        fallback's failure dressed up as the primary's.
        """
        # DALTON-HOOK(antigravity-series): resolve primary cooldown via effective
        # candidate client (falling back to request.model when cand_client is None).
        if not records and self._quota_exhausted and candidates:
            primary_name, primary_client = candidates[0]
            eff_primary = request.model if primary_client is None else primary_client
            primary_scope = self._quota_scope_key(primary_name, eff_primary)
            remembered = self._quota_exhausted.get(primary_scope)
            raise (remembered if remembered is not None
                   else next(iter(self._quota_exhausted.values())))[1]
        if records and candidates:
            primary_name, primary_client = candidates[0]
            eff_primary = request.model if primary_client is None else primary_client
            primary_scope = self._quota_scope_key(primary_name, eff_primary)
            benched = self._quota_exhausted.get(primary_scope)
            if benched is not None and self._quota_skipped(primary_name, eff_primary):
                primary = _AttemptRecord(
                    primary_name,
                    benched[1],
                    extract_status_code(benched[1]),
                    0,
                )
                return self._raise_exhausted([primary, *records])
        return self._raise_exhausted(records)

    def _raise_overflow(self, records: list[_AttemptRecord], exc: Exception) -> NoReturn:
        """Hand an overflow up to compaction, which sits outside this middleware.

        Reached once no fallback with a larger window is left: one no larger
        is sent the same history and refuses it the same way, so the compacted
        request is the one worth sending, and it comes back through here with
        the whole chain. The overflow is what is raised even when another
        candidate failed otherwise, since it is the failure the compaction can
        act on.
        """
        self._raise_exhausted(records, exc)

    @staticmethod
    def _worth_trying(
        request: ModelRequest, candidate: tuple[str, Any | None], refused_at: float | None
    ) -> bool:
        """Whether ``candidate`` may take a request a window of ``refused_at``
        refused as too long; every candidate may before any overflow. A window
        that is not declared is not known to be larger."""
        if refused_at is None:
            return True
        window = _window(request.model if candidate[1] is None else candidate[1])
        return window is not None and window > refused_at

    def _next_tried(
        self,
        request: ModelRequest,
        candidates: list[tuple[str, Any | None]],
        index: int,
        refused_at: float | None,
    ) -> int | None:
        # DALTON-HOOK(quota-exhaustion): a candidate benched as out of quota
        # is named by no switch — announcing a move to a model this call
        # would never dial misleads the user's trace.
        for j in range(index + 1, len(candidates)):
            cand_name, cand_client = candidates[j]
            effective = request.model if cand_client is None else cand_client
            if not self._quota_skipped(cand_name, effective) and self._worth_trying(
                request, candidates[j], refused_at
            ):
                return j
        return None

    @staticmethod
    def _credit_gate_exhausted() -> bool:  # DALTON-HOOK(budget-gate)
        from ptc_agent.agent.middleware.credit_gate import current_credit_gate

        gate = current_credit_gate.get()
        if gate is None:
            return False
        lease = gate.lease
        return bool(lease.should_stop(lease.spend()))

    def _retry_budget_s(self) -> float:  # DALTON-HOOK(quota-exhaustion)
        """The whole backoff this middleware could wait out on one candidate,
        without jitter — the yardstick a quota reset horizon is weighed
        against."""
        return sum(
            min(self.initial_delay * (self.backoff_factor**i), self.max_delay)
            for i in range(self.max_retries)
        )

    def _quota_horizon_s(self, exc: Exception) -> float | None:  # DALTON-HOOK(quota-exhaustion)
        try:
            return quota_exhaustion_seconds(exc)
        except Exception:
            return None

    def _local_quota_skipped(self, scope_key: str) -> bool:  # DALTON-HOOK(antigravity-series)
        """Whether ``scope_key`` is still benched locally in this middleware instance."""
        benched = self._quota_exhausted.get(scope_key)
        return benched is not None and time.monotonic() < benched[0]

    def _apply_remote_quota_state(  # DALTON-HOOK(antigravity-series)
        self, scope_key: str, name: str, data: Any
    ) -> bool:
        if not isinstance(data, dict):
            return False
        expires_at = data.get("expires_at")
        if isinstance(expires_at, (int, float)):
            remaining = float(expires_at) - time.time()
        else:
            remaining = float(DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS)
        if remaining <= 0:
            return False
        status_code = data.get("status_code")
        msg = str(data.get("message") or f"Quota exhausted for {name}")
        refusal = _RemoteQuotaExhaustedError(
            msg, status_code if isinstance(status_code, int) else None
        )
        self._quota_exhausted[scope_key] = (time.monotonic() + remaining, refusal)
        return True

    @staticmethod
    def _build_quota_redis_payload(  # DALTON-HOOK(quota-exhaustion)
        name: str, horizon: float, exc: Exception
    ) -> tuple[int, dict[str, Any]]:
        ttl_s = (
            max(1, math.ceil(horizon))
            if math.isfinite(horizon) and horizon > 0
            else DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS
        )
        payload = {
            "expires_at": time.time() + ttl_s,
            "message": str(exc)[:_ERROR_SUMMARY_MAX_CHARS],
            "status_code": extract_status_code(exc),
            "model": name,
        }
        return ttl_s, payload

    @staticmethod
    def _run_sync_cache_call(coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro_fn())
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro_fn())).result(timeout=2.0)

    def _sync_redis_get(self, redis_key: str) -> Any:  # DALTON-HOOK(quota-exhaustion)
        import json
        import redis as sync_redis
        import redis.asyncio as async_redis
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if isinstance(getattr(cache, "client", None), async_redis.Redis):
            if not getattr(cache, "enabled", True):
                return None
            with sync_redis.Redis.from_url(
                cache.url,
                socket_timeout=getattr(cache, "socket_timeout", 2),
                socket_connect_timeout=getattr(cache, "socket_connect_timeout", 2),
                decode_responses=False,
            ) as client:
                raw = client.get(redis_key)
                return json.loads(raw) if raw is not None else None
        return self._run_sync_cache_call(lambda: cache.get(redis_key))

    def _sync_redis_set(  # DALTON-HOOK(quota-exhaustion)
        self, redis_key: str, payload: dict[str, Any], ttl_s: int
    ) -> None:
        import json
        import redis as sync_redis
        import redis.asyncio as async_redis
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if isinstance(getattr(cache, "client", None), async_redis.Redis):
            if not getattr(cache, "enabled", True):
                return
            with sync_redis.Redis.from_url(
                cache.url,
                socket_timeout=getattr(cache, "socket_timeout", 2),
                socket_connect_timeout=getattr(cache, "socket_connect_timeout", 2),
                decode_responses=False,
            ) as client:
                client.setex(redis_key, ttl_s, json.dumps(payload, ensure_ascii=False))
            return
        self._run_sync_cache_call(lambda: cache.set(redis_key, payload, ttl=ttl_s))

    def _quota_skipped(  # DALTON-HOOK(antigravity-series)
        self, name: str, client: Any = None
    ) -> bool:
        """Whether ``name`` (or its cooldown scope) is benched locally or in Redis."""
        scope_key = self._quota_scope_key(name, client)
        if self._local_quota_skipped(scope_key):
            return True
        redis_key = self._quota_redis_key(name, client)
        if redis_key is None:
            return False
        try:
            data = self._sync_redis_get(redis_key)
        except Exception as err:
            logger.debug(
                "[ModelResilience] Redis sync quota read unavailable, using local state",
                key=redis_key,
                error=str(err),
            )
            return False
        return self._apply_remote_quota_state(scope_key, name, data)

    def _quota_redis_key(  # DALTON-HOOK(quota-exhaustion)
        self, name: str, client: Any = None
    ) -> str | None:
        """Coordination key scoped by credential owner, provider, model, and subscription account."""
        target = self._current_client(self.primary_client if client is None else client)
        return quota_redis_key(name, target, user_id=self.user_id)

    def _quota_scope_key(  # DALTON-HOOK(antigravity-series)
        self, name: str, client: Any = None
    ) -> str:
        """Unified local/Redis cooldown scope key; falls back to ``local:model:<name>``."""
        redis_key = self._quota_redis_key(name, client)
        return redis_key if redis_key is not None else f"local:model:{name}"

    async def _aquota_skipped(  # DALTON-HOOK(antigravity-series)
        self, name: str, client: Any = None
    ) -> bool:
        """Check both local memory and Redis advisory quota state (fails open to local)."""
        scope_key = self._quota_scope_key(name, client)
        if self._local_quota_skipped(scope_key):
            return True
        redis_key = self._quota_redis_key(name, client)
        if redis_key is None:
            return False
        try:
            from src.utils.cache.redis_cache import get_cache_client

            data = await get_cache_client().get(redis_key)
        except Exception as err:
            logger.debug(
                "[ModelResilience] Redis quota read unavailable, using local state",
                key=redis_key,
                error=str(err),
            )
            return False
        return self._apply_remote_quota_state(scope_key, name, data)

    def _remember_quota_exhausted_local(  # DALTON-HOOK(antigravity-series)
        self, scope_key: str, horizon: float, exc: Exception
    ) -> None:
        """Benches ``scope_key`` until its horizon passes. A fresh refusal writes
        a fresh deadline whenever the last bench has lapsed — an expired
        entry must never stand between the run and a re-armed bench — while a
        bench still live keeps its earliest refill."""
        benched = self._quota_exhausted.get(scope_key)
        if benched is not None and time.monotonic() < benched[0]:
            return
        self._quota_exhausted[scope_key] = (time.monotonic() + horizon, exc)

    def _remember_quota_exhausted(  # DALTON-HOOK(antigravity-series)
        self, name: str, horizon: float, exc: Exception, client: Any = None
    ) -> None:
        """Record exhaustion locally and coordinate across workers via Redis (sync)."""
        scope_key = self._quota_scope_key(name, client)
        self._remember_quota_exhausted_local(scope_key, horizon, exc)
        redis_key = self._quota_redis_key(name, client)
        if redis_key is None:
            return
        ttl_s, payload = self._build_quota_redis_payload(name, horizon, exc)
        try:
            self._sync_redis_set(redis_key, payload, ttl_s)
        except Exception as err:
            logger.debug(
                "[ModelResilience] Redis sync quota write unavailable, keeping local state only",
                key=redis_key,
                error=str(err),
            )

    async def _aremember_quota_exhausted(  # DALTON-HOOK(antigravity-series)
        self, name: str, horizon: float, exc: Exception, client: Any = None
    ) -> None:
        """Record exhaustion locally and coordinate across workers via Redis."""
        scope_key = self._quota_scope_key(name, client)
        self._remember_quota_exhausted_local(scope_key, horizon, exc)
        redis_key = self._quota_redis_key(name, client)
        if redis_key is None:
            return
        ttl_s, payload = self._build_quota_redis_payload(name, horizon, exc)
        try:
            from src.utils.cache.redis_cache import get_cache_client

            await get_cache_client().set(redis_key, payload, ttl=ttl_s)
        except Exception as err:
            logger.debug(
                "[ModelResilience] Redis quota write unavailable, keeping local state only",
                key=redis_key,
                error=str(err),
            )

    async def _anext_tried(  # DALTON-HOOK(quota-exhaustion)
        self,
        request: ModelRequest,
        candidates: list[tuple[str, Any | None]],
        index: int,
        refused_at: float | None,
    ) -> int | None:
        for j in range(index + 1, len(candidates)):
            cand_name, cand_client = candidates[j]
            effective = request.model if cand_client is None else cand_client
            if not await self._aquota_skipped(cand_name, effective) and self._worth_trying(
                request, candidates[j], refused_at
            ):
                return j
        return None

    def _lane(self) -> str:  # DALTON-HOOK(quota-exhaustion)
        """Which lane this call runs on: the main graph, or the subagent
        namespace a subgraph runs in. A bench notice has to reach the main
        lane even when a subagent armed the bench (subagent frames never
        ride the main stream), so the once-per-run notice guard is keyed
        per lane — on the lane's STABLE key, or every model call would be
        its own lane (see ``_stable_lane``)."""
        try:
            from langgraph.config import get_config

            namespace = (
                get_config().get("configurable") or {}
            ).get("checkpoint_ns") or ""
            return _stable_lane(namespace)
        except Exception:
            return ""


    def _emit_fallback_once(  # DALTON-HOOK(quota-exhaustion)
        self,
        record: _AttemptRecord,
        to_model: str,
        to_client: Any,
        request: ModelRequest,
        *,
        from_is_primary: bool,
    ) -> None:
        """``_emit_fallback`` behind the run's once-per-lane-per-switch
        guard: a failure-driven switch and the later bench-driven skip of
        the same pair announce as one, whichever comes first. The lane key
        is the STABLE one — the raw namespace's per-execution segment would
        make every model call its own lane (codex r2 #3: four calls, four
        notices)."""
        key = (self._lane(), record.model, to_model)
        if key in self._quota_notified:
            return
        self._quota_notified.add(key)
        self._emit_fallback(
            record, to_model, to_client, request, from_is_primary=from_is_primary
        )

    def _notify_bench_switch(  # DALTON-HOOK(antigravity-series)
        self,
        request: ModelRequest,
        candidates: list[tuple[str, Any | None]],
        index: int,
    ) -> None:
        """The fallback notice for a switch the bench itself made: an earlier
        candidate sits this call out on quota, so the call starts on
        ``candidates[index]``. Shaped exactly like a failure-driven switch,
        and emitted at most once per lane and switch pair per run — a
        subagent's notice neither satisfies nor suppresses the main
        agent's."""
        benched_name: str | None = None
        benched_eff_client: Any = None
        for name, cand_client in candidates[:index]:
            eff_client = request.model if cand_client is None else cand_client
            if self._quota_skipped(name, eff_client):
                benched_name = name
                benched_eff_client = eff_client
        if benched_name is None:
            return
        benched_scope = self._quota_scope_key(benched_name, benched_eff_client)
        remembered = self._quota_exhausted.get(benched_scope)
        if remembered is None:
            return
        to_name, to_client = candidates[index]
        self._emit_fallback_once(
            _AttemptRecord(
                benched_name,
                remembered[1],
                extract_status_code(remembered[1]),
                0,
            ),
            to_name,
            to_client,
            request,
            from_is_primary=candidates[0][0] == benched_name,
        )

    def _candidates(self, request: ModelRequest) -> list[tuple[str, Any | None]]:
        primary = (self._primary_display_name(request), None)
        # A compaction request asks the primary's provider for its own
        # summary; another model would answer it as a turn, and the caller
        # has a summarizer of its own to fall back to.
        # DALTON-HOOK(budget-gate): daily/task budget admission precedes fallback
        # selection on both main agent and subagent stacks.
        if (
            is_compaction_request(request.model_settings)
            or _as_sent.get()
            or self._credit_gate_exhausted()
        ):
            return [primary]
        # A subagent on its own model may be one of the fallbacks.
        target = self._catalog_name(request)
        return [primary, *self._on_its_payer(request.model, _distinct(self.fallbacks, target))]

    def _on_its_payer(
        self, model: Any, fallbacks: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """A call falls back only onto a model the same party pays for, and a
        call a subscription pays for falls back nowhere.

        The user's own key moves to another of their keys, our credits to
        another model on credits, never across, whoever saved the list: who
        pays for a turn stays what the user chose. The turn's list was built
        for the main model, so a subagent on another payer's model filters it
        too. A subscription (a ChatGPT, Claude or Grok sign-in, or Claude Code)
        answers for itself: its limit or outage ends the call with its own
        card, and the user picks the next model, as Claude Code's plan limit
        always has.
        """
        billing = _stamped(model, "billing_type")
        if billing == "oauth":
            return []
        if billing is None:
            return fallbacks
        return [(name, client) for name, client in fallbacks
                if _stamped(client, "billing_type") == billing]

    def _classify(self, exc: Exception) -> tuple[int | None, bool]:
        status = extract_status_code(exc)
        # A used-up plan or balance arrives as a 429 like any burst limit, but
        # no retry in the next seconds gets past it: move on to the fallback.
        return status, not is_terminal_refusal(exc) and is_retryable_error(exc, status)

    @staticmethod
    def _escalate_reasoning(
        request: ModelRequest, *, references_only: bool = False
    ) -> ModelRequest | None:
        """Rebuild the request without any reasoning payload.

        ``references_only`` answers a server that could not find an item the
        input names: only reasoning sent by id alone can be missing, so that is
        all it strips, and the rest of the history keeps its reasoning. With no
        such item it strips everything, as for a 400.

        ``None`` when there is nothing to strip, so the caller never spends a
        call re-sending an identical request. Retrying in place is safe: a
        request-validation 400 is raised while creating the stream, before any
        chunk is emitted or any token billed.

        Deliberately does *not* also disable thinking. Measured against
        api.anthropic.com and the claude-oauth route across enabled, adaptive and
        interleaved modes: replaying an assistant turn whose thinking block was
        stripped is accepted while thinking is on. Disabling it would buy nothing
        and would mask genuine config 400s — ``max_tokens`` below
        ``thinking.budget_tokens`` succeeds once thinking is off, turning a
        surfaced error into a silently thinking-free answer.
        """
        messages = request.messages
        if references_only:
            messages = strip_reasoning_references(messages)
        if messages is request.messages:
            messages = strip_all_reasoning(messages)
        if messages is request.messages:
            return None
        return request.override(messages=messages)

    def _decide(
        self, exc: Exception, attempts: int, req: ModelRequest, *, escalated: bool
    ) -> tuple[int | None, _Recovery | None]:
        """What to do about a failed attempt, shared by the sync and async loops.

        ``None`` means give up on this candidate. A reasoning-payload 400 is the
        one non-retryable status worth another call: the provider rejected the
        history's reasoning blocks, and the same request without them can
        succeed. So is a 404 naming the request's ``input``, an item there the
        server does not hold, which strips only the reasoning sent by id.
        Detected structurally, the status plus "we did send reasoning", rather
        than by matching provider error prose, which drifts between API versions
        and fails silently when it does. The cost is
        one wasted call when an unrelated refusal coincides with reasoning in
        history, on a turn that is already failing anyway. An answer the strip
        bought is remembered for the thread, so the next call skips the wasted one.

        A mid-stream failure is excluded because that repair cannot apply to it.
        Stripping reasoning answers a request rejected during validation, before
        it ran; a provider that accepted the request, opened the stream and only
        then reported failure has already judged the payload fine, and re-sending
        bills the tokens it emitted a second time. Content rejected by an input
        filter is the case that made this concrete: every attempt is refused
        identically, so the call is pure latency before the fallback that works.

        A stream that went silent past its chunk bound gets one retry, not the
        full count, whether or not its first chunk had arrived: each attempt
        waits out the whole bound before failing, so the full count held one
        candidate for most of an hour before its fallback, and stalls seen in
        prod repeated at the same point on every attempt. A stream the server
        closed before its response finished gets one too: a cut that repeats on
        a fresh connection is the server's limit, and each attempt is billed.

        A context overflow gets nothing, with compaction or without: the same
        request is over the window on every attempt, with or without its
        reasoning, and a provider's summary read as its text instead is no
        smaller for certain. Whether the chain then moves on or hands it to
        compaction is the loop's call.
        """
        status, retryable = self._classify(exc)
        if is_context_overflow(exc):
            return status, None
        if retryable and attempts > 1 and (
            is_stalled_stream(exc) or isinstance(exc, UnfinishedReplyError)
        ):
            retryable = False
        if retryable and attempts <= self.max_retries:
            return status, _Recovery(delay=self._calculate_delay(attempts - 1))
        missing_item = status == 404 and refuses_input(exc)
        if (
            not escalated
            and (status == 400 or missing_item)
            and not is_mid_stream_error(exc)
            # A 400 that names the account (Anthropic's low credit balance,
            # DashScope's overdue account) refuses the stripped request too.
            and not is_terminal_refusal(exc)
            and not _as_sent.get()
        ):
            stripped = self._escalate_reasoning(req, references_only=missing_item)
            if stripped is not None:
                logger.warning(
                    "[ModelResilience] retrying without reasoning after a refused request",
                    status=status,
                    model=api_model_id(req.model) or type(req.model).__name__,
                    thread_id=run_ids()[0],
                    error=(str(exc) or type(exc).__name__)[:_STRIP_ERROR_MAX_CHARS],
                )
                return status, _Recovery(request=stripped)
        return status, None

    def _answered(
        self,
        response: ModelResponse,
        name: str,
        index: int,
        request: ModelRequest,
        stripped: list[str],
    ) -> ModelCallResult:
        """The answer, stamped with the model that wrote it, and with the
        strip it took kept for the thread's later requests
        (``UnsentThinkingState``). Only the request's own model keeps one: a
        fallback refusing another model's reasoning says nothing about what
        the primary reads."""
        _report_drops(response, name, request.messages)
        marks = {FALLBACK_KEY: name} if index else {}
        catalog = name if index else self._catalog_name(request)
        if catalog:
            marks[CATALOG_MODEL_KEY] = catalog
        _mark(response, marks)
        if index or not stripped:
            return response
        return ExtendedModelResponse(
            model_response=response, command=Command(update={UNSENT_THINKING_KEY: set(stripped)})
        )

    # ------------------------------------------------------------------
    # Hooks

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        # Sync twin of awrap_model_call. Unused in production (the graph is
        # only driven async) — time.sleep here would stall an event loop.
        request = _keep_unsent(request)
        candidates = self._candidates(request)
        records: list[_AttemptRecord] = []
        # The largest window that refused the request as too long (infinite
        # when it declared none), and the refusal compaction is handed.
        refused_at: float | None = None
        overflow: Exception | None = None

        tried_any = False
        for index, (name, client) in enumerate(candidates):
            # DALTON-HOOK(quota-exhaustion): a candidate this run already
            # judged out of quota is not re-asked — the refusal that benched
            # it answers identically until the account is topped up. A call
            # that therefore starts on a fallback announces the switch once,
            # exactly as a failure-driven switch does.
            cand_client = request.model if client is None else client
            if self._quota_skipped(name, cand_client):
                continue
            if not tried_any:
                tried_any = True
                self._notify_bench_switch(request, candidates, index)
            if not self._worth_trying(request, candidates[index], refused_at):
                continue
            req = request if client is None else request.override(model=client)
            if index > 0:  # DALTON-HOOK(model-fallback-identity)
                req = self._with_fallback_identity(req, request, name, req.model)
            attempts = 0
            escalated = False
            stripped: list[str] = []
            while True:
                attempts += 1
                try:
                    with _answering(index):
                        response = handler(req)
                    _refuse_unfinished(response)
                    return self._answered(response, name, index, request, stripped)
                except Exception as exc:
                    status, recovery = self._decide(
                        exc, attempts, req, escalated=escalated
                    )
                    # DALTON-HOOK(quota-exhaustion): a quota refusal whose
                    # reset horizon outlasts every backoff this run could wait
                    # gives up on the candidate now, however retryable a 429
                    # reads on its own, and the run benches it.
                    horizon = self._quota_horizon_s(exc)
                    benched = horizon is not None and horizon > self._retry_budget_s()
                    if benched:
                        recovery = None
                    if recovery is None:
                        records.append(_AttemptRecord(name, exc, status, attempts))
                        if benched:
                            self._remember_quota_exhausted(name, horizon, exc, req.model)
                        if self._hands_up(exc):
                            overflow = exc
                            refused_at = max(refused_at or 0, _window(req.model) or math.inf)
                        break
                    self._emit_retry(name, attempts, exc, status, recovery.delay)
                    if recovery.request is not None:
                        stripped = _stripped_ids(req.messages, recovery.request.messages)
                        req, escalated = recovery.request, True
                    elif recovery.delay > 0:
                        time.sleep(recovery.delay)
                    continue
            # DALTON-HOOK(budget-gate): do not initiate fallback when the budget lease is exhausted
            if self._credit_gate_exhausted():
                break
            following = self._next_tried(request, candidates, index, refused_at)
            if following is not None:
                to_model, to_client = candidates[following]
                self._emit_fallback_once(
                    records[-1],
                    to_model,
                    to_client,
                    request,
                    from_is_primary=index == 0,
                )

        if overflow is not None:
            self._raise_overflow(records, overflow)
        # DALTON-HOOK(quota-exhaustion): the quota-aware total-exhaustion
        # raise — see ``_raise_run_exhausted``.
        # DALTON-HOOK(antigravity-series): forward request so primary scope uses request.model.
        return self._raise_run_exhausted(records, candidates, request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        request = _keep_unsent(request)
        candidates = self._candidates(request)
        records: list[_AttemptRecord] = []
        # The largest window that refused the request as too long (infinite
        # when it declared none), and the refusal compaction is handed.
        refused_at: float | None = None
        overflow: Exception | None = None

        tried_any = False
        for index, (name, client) in enumerate(candidates):
            # DALTON-HOOK(quota-exhaustion): a candidate this run (or another worker
            # sharing the credential in Redis) already judged out of quota is not
            # re-asked — the refusal that benched it answers identically until the
            # account is topped up. A call that therefore starts on a fallback
            # announces the switch once, exactly as a failure-driven switch does.
            cand_client = request.model if client is None else client
            if await self._aquota_skipped(name, cand_client):
                continue
            if not tried_any:
                tried_any = True
                self._notify_bench_switch(request, candidates, index)
            if not self._worth_trying(request, candidates[index], refused_at):
                continue
            req = request if client is None else request.override(model=client)
            # DALTON-HOOK(oauth-dispatch): use a client already refreshed earlier in the turn;
            # on a 401 rebuild it once with a fresh token and retry the same model before
            # falling back, since the stored credential is usually fine.
            base = req.model
            current = self._current_client(base)
            if current is not base:
                req = req.override(model=current)
            if index > 0:  # DALTON-HOOK(model-fallback-identity)
                req = self._with_fallback_identity(req, request, name, req.model)
            oauth_refreshed = False
            attempts = 0
            escalated = False
            stripped: list[str] = []
            while True:
                attempts += 1
                try:
                    with _answering(index):
                        response = await handler(req)
                    _refuse_unfinished(response)
                    return self._answered(response, name, index, request, stripped)
                except Exception as exc:
                    if not oauth_refreshed and extract_status_code(exc) == 401:
                        oauth_refreshed = True
                        fresh = await self._refresh_oauth_client(base, req.model)
                        if fresh is not None:
                            self._emit_retry(name, attempts, exc, 401, 0.0)
                            req = req.override(model=fresh)
                            # The refreshed client starts its own count, so the
                            # one-retry rule for a stalled stream still applies.
                            attempts = 0
                            continue
                    status, recovery = self._decide(
                        exc, attempts, req, escalated=escalated
                    )
                    # DALTON-HOOK(quota-exhaustion): a quota refusal whose
                    # reset horizon outlasts every backoff this run could wait
                    # gives up on the candidate now, however retryable a 429
                    # reads on its own, and the run benches it.
                    horizon = self._quota_horizon_s(exc)
                    benched = horizon is not None and horizon > self._retry_budget_s()
                    if benched:
                        recovery = None
                    if recovery is None:
                        records.append(_AttemptRecord(name, exc, status, attempts))
                        if benched:
                            await self._aremember_quota_exhausted(
                                name, horizon, exc, req.model
                            )
                        if self._hands_up(exc):
                            overflow = exc
                            refused_at = max(refused_at or 0, _window(req.model) or math.inf)
                        break
                    self._emit_retry(name, attempts, exc, status, recovery.delay)
                    if recovery.request is not None:
                        stripped = _stripped_ids(req.messages, recovery.request.messages)
                        req, escalated = recovery.request, True
                    elif recovery.delay > 0:
                        await asyncio.sleep(recovery.delay)
                    continue
            # DALTON-HOOK(budget-gate): do not initiate fallback when the budget lease is exhausted
            if self._credit_gate_exhausted():
                break
            following = await self._anext_tried(request, candidates, index, refused_at)
            if following is not None:
                to_model, to_client = candidates[following]
                self._emit_fallback_once(
                    records[-1],
                    to_model,
                    to_client,
                    request,
                    from_is_primary=index == 0,
                )

        if overflow is not None:
            self._raise_overflow(records, overflow)
        # DALTON-HOOK(quota-exhaustion): the quota-aware total-exhaustion
        # raise — see ``_raise_run_exhausted``.
        # DALTON-HOOK(antigravity-series): forward request so primary scope uses request.model.
        return self._raise_run_exhausted(records, candidates, request)
