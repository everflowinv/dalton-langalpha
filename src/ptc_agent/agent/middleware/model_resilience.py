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
from typing import TYPE_CHECKING, Any, NoReturn

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

from ptc_agent.agent.middleware.thinking_binding import (
    UNSENT_THINKING_KEY,
    UnsentThinkingState,
    unsent_thinking,
    without_thinking,
)

from src.llms.error_classification import (
    extract_status_code,
    is_context_overflow,
    is_mid_stream_error,
    is_retryable_error,
    is_stalled_stream,
    quota_exhaustion_seconds,  # DALTON-HOOK(quota-exhaustion)
)
from src.llms.input_transformations import INPUT_TRANSFORMATIONS, REPEATED, thinking_drops
from src.llms.provider_compaction import is_compaction_request
from src.llms.reasoning_payload import strip_all_reasoning
from src.observability.langsmith_run import stamp_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

# Attribute attached to the primary exception on total exhaustion. Read by the
# server's ``_build_error_event`` to enrich the SSE error payload with the
# primary model name and the per-model attempt trace.
RESILIENCE_TRACE_ATTR = "__model_resilience__"

_ERROR_SUMMARY_MAX_CHARS = 300

#: The ``response_metadata`` key naming the fallback that wrote an answer. Its
#: usage counts that model's tokens, so compaction measures nothing for the
#: primary by it.
FALLBACK_KEY = "model_fallback"

_as_sent: ContextVar[bool] = ContextVar("model_resilience_as_sent", default=False)

#: Drop reasons that follow the thread rather than an edit of ours: a model
#: switch, or a thread moved between accounts.
_BY_DESIGN_DROPS = frozenset({"model_binding_mismatch", "organization_binding_mismatch"})

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


def _thread_id() -> str | None:
    try:
        from langgraph.config import get_config

        return (get_config().get("configurable") or {}).get("thread_id")
    except Exception:
        return None


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
        log = logger.info if set(counts) <= _BY_DESIGN_DROPS | {REPEATED} else logger.warning
        log(
            "[ModelResilience] earlier thinking dropped by the provider",
            model=model,
            thread_id=_thread_id(),
            **counts,
        )
        stamp_run(thinking_dropped=counts)


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


def _model_display_name(model: Any) -> str:
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


# The ``billing_type`` of a call the user's own credential pays for.
_OWN_BILLING = frozenset({"byok", "oauth"})


def _stamped(model: Any, key: str) -> str | None:
    """A fact ``LLM.get_llm()`` stamped in a client's metadata: its models.json
    name (``manifest_model``), the namespace fallback names are in, or who
    pays (``billing_type``). None for an unstamped client."""
    metadata = getattr(model, "metadata", None)
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def _window(model: Any) -> int | None:
    """The context window a client's profile declares, None when it declares none."""
    profile = getattr(model, "profile", None)
    window = profile.get("max_input_tokens") if isinstance(profile, dict) else None
    return window if isinstance(window, int) and window > 0 else None


def _mark_fallback(response: ModelResponse, name: str) -> ModelResponse:
    for msg in getattr(response, "result", None) or ():
        if isinstance(msg, AIMessage):
            msg.response_metadata = {**(msg.response_metadata or {}), FALLBACK_KEY: name}
    return response


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
        users_fallbacks: bool = False,
    ) -> None:
        super().__init__()
        self.primary_name = primary_name
        self.primary_client = primary_client
        # Repeats only; the model a request targets is dropped per request,
        # as the instance is shared into subagents on other models.
        self.fallbacks = _distinct(list(fallbacks or []), None)
        # Whether the fallbacks are a list the user saved (see _on_its_payer).
        self.users_fallbacks = users_fallbacks
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
        self, records: list[_AttemptRecord], candidates: list[tuple[str, Any | None]]
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
        if not records and self._quota_exhausted and candidates:
            remembered = self._quota_exhausted.get(candidates[0][0])
            raise (remembered if remembered is not None
                   else next(iter(self._quota_exhausted.values())))[1]
        if records and candidates:
            benched = self._quota_exhausted.get(candidates[0][0])
            if benched is not None and self._quota_skipped(candidates[0][0]):
                primary = _AttemptRecord(
                    candidates[0][0],
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
        return next(
            (
                j
                for j in range(index + 1, len(candidates))
                if not self._quota_skipped(candidates[j][0])
                and self._worth_trying(request, candidates[j], refused_at)
            ),
            None,
        )

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

    def _quota_skipped(self, name: str) -> bool:  # DALTON-HOOK(quota-exhaustion)
        """Whether ``name`` is still benched as out of quota for this run."""
        benched = self._quota_exhausted.get(name)
        return benched is not None and time.monotonic() < benched[0]

    def _remember_quota_exhausted(  # DALTON-HOOK(quota-exhaustion)
        self, name: str, horizon: float, exc: Exception
    ) -> None:
        """Benches ``name`` until its horizon passes. A fresh refusal writes
        a fresh deadline whenever the last bench has lapsed — an expired
        entry must never stand between the run and a re-armed bench — while a
        bench still live keeps its earliest refill."""
        benched = self._quota_exhausted.get(name)
        if benched is not None and time.monotonic() < benched[0]:
            return
        self._quota_exhausted[name] = (time.monotonic() + horizon, exc)

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

    def _notify_bench_switch(  # DALTON-HOOK(quota-exhaustion)
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
        for name, _ in candidates[:index]:
            if self._quota_skipped(name):
                benched_name = name
        if benched_name is None:
            return
        remembered = self._quota_exhausted.get(benched_name)
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
        if self.primary_client is None or request.model is self.primary_client:
            target = self.primary_name
        else:
            # A subagent on its own model, which may be one of the fallbacks.
            target = _stamped(request.model, "manifest_model")
        return [primary, *self._on_its_payer(request.model, _distinct(self.fallbacks, target))]

    def _on_its_payer(
        self, model: Any, fallbacks: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """A call the user's own key or subscription pays for moves onto the
        platform's key only through fallbacks the user saved.

        The turn's fallbacks were built for the main model, so on a turn the
        platform pays for they may run on its key; a subagent on a model of the
        user's own would otherwise fail over onto credits.
        """
        if self.users_fallbacks or _stamped(model, "billing_type") not in _OWN_BILLING:
            return fallbacks
        return [(name, client) for name, client in fallbacks
                if _stamped(client, "billing_type") != "platform"]

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

        A context overflow gets nothing, with compaction or without: the same
        request is over the window on every attempt, with or without its
        reasoning, and a provider's summary read as its text instead is no
        smaller for certain. Whether the chain then moves on or hands it to
        compaction is the loop's call.
        """
        status, retryable = self._classify(exc)
        if is_context_overflow(exc):
            return status, None
        if retryable and is_stalled_stream(exc) and attempts > 1:
            retryable = False
        if retryable and attempts <= self.max_retries:
            return status, _Recovery(delay=self._calculate_delay(attempts - 1))
        if (
            not escalated
            and status == 400
            and not is_mid_stream_error(exc)
            and not _as_sent.get()
        ):
            stripped = self._escalate_reasoning(req)
            if stripped is not None:
                logger.warning(
                    "[ModelResilience] retrying without reasoning after a 400",
                    model=_model_display_name(req.model),
                    thread_id=_thread_id(),
                    error=(str(exc) or type(exc).__name__)[:_STRIP_ERROR_MAX_CHARS],
                )
                return status, _Recovery(request=stripped)
        return status, None

    @staticmethod
    def _answered(
        response: ModelResponse,
        name: str,
        index: int,
        request: ModelRequest,
        stripped: list[str],
    ) -> ModelCallResult:
        """The answer, with the strip it took kept for the thread's later
        requests (``UnsentThinkingState``). Only the request's own model
        keeps one: a fallback refusing another model's reasoning says nothing
        about what the primary reads."""
        _report_drops(response, name, request.messages)
        if index:
            return _mark_fallback(response, name)
        if not stripped:
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
            if self._quota_skipped(name):
                continue
            if not tried_any:
                tried_any = True
                self._notify_bench_switch(request, candidates, index)
            if not self._worth_trying(request, candidates[index], refused_at):
                continue
            req = request if client is None else request.override(model=client)
            attempts = 0
            escalated = False
            stripped: list[str] = []
            while True:
                attempts += 1
                try:
                    response = handler(req)
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
                            self._remember_quota_exhausted(name, horizon, exc)
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
        return self._raise_run_exhausted(records, candidates)

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
            # DALTON-HOOK(quota-exhaustion): a candidate this run already
            # judged out of quota is not re-asked — the refusal that benched
            # it answers identically until the account is topped up. A call
            # that therefore starts on a fallback announces the switch once,
            # exactly as a failure-driven switch does.
            if self._quota_skipped(name):
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
            oauth_refreshed = False
            attempts = 0
            escalated = False
            stripped: list[str] = []
            while True:
                attempts += 1
                try:
                    response = await handler(req)
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
                            self._remember_quota_exhausted(name, horizon, exc)
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
        return self._raise_run_exhausted(records, candidates)
