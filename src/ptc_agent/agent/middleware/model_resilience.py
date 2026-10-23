"""Model resilience middleware: retry with backoff, model fallback, SSE signals.

Replaces the stock langchain ``ModelRetryMiddleware`` + ``ModelFallbackMiddleware``
pair so that:

- non-retryable errors (bad request / auth / unknown model) skip straight to
  the next fallback model instead of burning retries on a call that cannot
  succeed,
- every retry and fallback switch is emitted as a custom stream event
  (``model_retry`` / ``model_fallback``) for the client to render, and
- on total exhaustion the PRIMARY model's exception is re-raised with the full
  attempt trace attached, so the user sees the error for the model they
  configured rather than the last fallback's.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

import structlog
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from src.llms.error_classification import (
    extract_status_code,
    is_mid_stream_error,
    is_retryable_error,
    is_stalled_stream,
)
from src.llms.reasoning_payload import strip_all_reasoning

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

# Attribute attached to the primary exception on total exhaustion. Read by the
# server's ``_build_error_event`` to enrich the SSE error payload with the
# primary model name and the per-model attempt trace.
RESILIENCE_TRACE_ATTR = "__model_resilience__"

_ERROR_SUMMARY_MAX_CHARS = 300


def _last_message_id(request: ModelRequest) -> str | None:
    """The newest message of the state the call was made from."""
    state = getattr(request, "state", None)
    messages = state.get("messages") if isinstance(state, dict) else None
    return getattr(messages[-1], "id", None) if messages else None


def _summarize_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:_ERROR_SUMMARY_MAX_CHARS]


@dataclass(slots=True)
class _Recovery:
    """One more attempt on the same candidate: back off, or send a fixed request.

    ``request`` set means the payload was repaired rather than merely retried,
    which the caller records so it only escalates once per candidate.
    """

    delay: float = 0.0
    request: ModelRequest | None = None


def _model_display_name(model: Any) -> str:
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


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
        return [(_model_display_name(client), client) for client in clients]

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


class ModelResilienceMiddleware(AgentMiddleware):
    """Retry + fallback across models with client-visible progress events.

    One instance is shared across the main and subagent stacks, so all loop
    state is call-local.
    """

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
    ) -> None:
        super().__init__()
        self.primary_name = primary_name
        self.primary_client = primary_client
        self.fallbacks = list(fallbacks or [])
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.jitter = jitter
        # DALTON-HOOK(oauth-dispatch): clients rebuilt after a mid-turn 401, keyed by the
        # id of the client they replace (kept alongside, so the id cannot be reused). Lives
        # on this per-build instance only; every later call of the turn uses the fresh one.
        self._oauth_replacements: dict[int, tuple[Any, Any]] = {}

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

    def _primary_display_name(self, request: ModelRequest) -> str:
        # The instance is shared into subagent stacks where request.model is a
        # subagent-specific client — only claim the configured name when the
        # request actually targets the primary client.
        if self.primary_client is not None and request.model is self.primary_client:
            return self.primary_name
        if self.primary_client is None:
            return self.primary_name
        return _model_display_name(request.model)

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
        request: ModelRequest,
        *,
        from_is_primary: bool,
    ) -> None:
        # Unlike retries, fallbacks must survive replay: push_ui_message
        # dual-writes the record to the custom stream (live SSE) and the
        # checkpointed ``ui`` channel, which replay projects per turn. The
        # turn's records keep no step, so the record names the message the
        # call followed, which replay places the notice after.
        props = {
            "from_model": record.model,
            "to_model": to_model,
            "from_is_primary": from_is_primary,
            "error": _summarize_error(record.exc),
            "status_code": record.status_code,
            "attempts_on_from": record.attempts,
        }
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

    def _raise_exhausted(self, records: list[_AttemptRecord]) -> NoReturn:
        primary = records[0]
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
            setattr(primary.exc, RESILIENCE_TRACE_ATTR, trace)
        except Exception:
            # Exceptions with __slots__ reject arbitrary attributes; the
            # primary error still surfaces, just without the trace.
            pass
        logger.warning(
            "[ModelResilience] All models exhausted",
            primary_model=primary.model,
            attempted=[r.model for r in records],
        )
        raise primary.exc

    def _candidates(self, request: ModelRequest) -> list[tuple[str, Any | None]]:
        return [(self._primary_display_name(request), None), *self.fallbacks]

    def _classify(self, exc: Exception) -> tuple[int | None, bool]:
        status = extract_status_code(exc)
        return status, is_retryable_error(exc, status)

    @staticmethod
    def _escalate_reasoning(request: ModelRequest) -> ModelRequest | None:
        """Rebuild the request without any reasoning payload.

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
        messages = strip_all_reasoning(request.messages)
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
        succeed. Detected structurally, a 400 plus "we did send reasoning",
        rather than by matching provider error prose, which drifts between API
        versions and fails silently when it does. The cost is one wasted call
        when an unrelated 400 coincides with reasoning in history, on a turn that
        is already failing anyway.

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
        prod repeated at the same point on every attempt.
        """
        status, retryable = self._classify(exc)
        if retryable and is_stalled_stream(exc) and attempts > 1:
            retryable = False
        if retryable and attempts <= self.max_retries:
            return status, _Recovery(delay=self._calculate_delay(attempts - 1))
        if not escalated and status == 400 and not is_mid_stream_error(exc):
            stripped = self._escalate_reasoning(req)
            if stripped is not None:
                return status, _Recovery(request=stripped)
        return status, None

    # ------------------------------------------------------------------
    # Hooks

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        # Sync twin of awrap_model_call. Unused in production (the graph is
        # only driven async) — time.sleep here would stall an event loop.
        candidates = self._candidates(request)
        records: list[_AttemptRecord] = []

        for index, (name, client) in enumerate(candidates):
            req = request if client is None else request.override(model=client)
            attempts = 0
            escalated = False
            while True:
                attempts += 1
                try:
                    return handler(req)
                except Exception as exc:
                    status, recovery = self._decide(
                        exc, attempts, req, escalated=escalated
                    )
                    if recovery is None:
                        records.append(_AttemptRecord(name, exc, status, attempts))
                        break
                    self._emit_retry(name, attempts, exc, status, recovery.delay)
                    if recovery.request is not None:
                        req, escalated = recovery.request, True
                    elif recovery.delay > 0:
                        time.sleep(recovery.delay)
                    continue
            if index + 1 < len(candidates):
                self._emit_fallback(
                    records[-1],
                    candidates[index + 1][0],
                    request,
                    from_is_primary=index == 0,
                )

        return self._raise_exhausted(records)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        candidates = self._candidates(request)
        records: list[_AttemptRecord] = []

        for index, (name, client) in enumerate(candidates):
            req = request if client is None else request.override(model=client)
            # DALTON-HOOK(oauth-dispatch): use a client already refreshed earlier in the turn;
            # on a 401 rebuild it once with a fresh token and retry the same model before
            # falling back, since the stored credential is usually fine.
            base = req.model
            current = self._current_client(base)
            if current is not base:
                req = req.override(model=current)
            oauth_refreshed = False
            attempts = 0
            escalated = False
            while True:
                attempts += 1
                try:
                    return await handler(req)
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
                    if recovery is None:
                        records.append(_AttemptRecord(name, exc, status, attempts))
                        break
                    self._emit_retry(name, attempts, exc, status, recovery.delay)
                    if recovery.request is not None:
                        req, escalated = recovery.request, True
                    elif recovery.delay > 0:
                        await asyncio.sleep(recovery.delay)
                    continue
            if index + 1 < len(candidates):
                self._emit_fallback(
                    records[-1],
                    candidates[index + 1][0],
                    request,
                    from_is_primary=index == 0,
                )

        return self._raise_exhausted(records)
