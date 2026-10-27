"""CompactionMiddleware: two-tier context management for LangGraph agents.

Compaction is a view over the checkpoint, never a rewrite of it: each model
call is sent the history after the last summary with the recorded Tier 1 cuts
applied, so the checkpoint keeps every message and a later compaction builds
on the one before.

- Tier 1: at a turn start after a long pause, when the provider's prompt
  cache has expired, large tool arguments are cut to a pointer at the
  transcript file that keeps them and stale Read results are hidden.
- Tier 2: when the context reaches its threshold, or the model refuses it
  as too long, the start of the view is summarized (see ``compact``), and
  the attachments in what it keeps are taken out until it fits; and between
  turns when the user asks (see ``between_turns``).
- The window: on the main agent, at each pass through ``before_agent``, the
  runs a summary stands in for are trimmed from the checkpoint once the
  server holds them (see ``window``).

Each step is reported as a ``context_window`` event, whose ``action`` values
are wire protocol: ``token_usage`` after each model call, ``summarize``
(start, complete or error) around a summary, and ``offload`` (complete) after
Tier 1.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.config import get_config, get_stream_writer
from langgraph.constants import TAG_NOSTREAM
from langgraph.types import Command
from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)

from src.llms.error_classification import is_context_overflow
from src.llms.provider_compaction import (
    compaction_settings,
    is_opaque,
    kept_user_texts,
    native_answer,
    native_kind,
)
from src.llms.token_counter import extract_token_usage

from ptc_agent.agent.state import ensure_message_ids
from ptc_agent.agent.middleware.compaction.between_turns import Ask, HandOff, asked
from ptc_agent.agent.middleware.compaction.types import CompactionState, OffloadSettings
from ptc_agent.agent.middleware.compaction.compact import (
    Compaction,
    Native,
    NativeAnswer,
    Summarizer,
)
from ptc_agent.agent.middleware.compaction.summary_request import (
    HANDOFF_SUMMARY_PROMPT,
    NATIVE_SUMMARY_PROMPT,
)
from ptc_agent.agent.middleware.compaction.utils import (
    attachment_tokens,
    context_tokens,
    find_group_safe_cutoff,
    get_effective_messages,
    handed_off,
    pdf_pages,
    provider_cutoff,
    read_ahead,
    stamp_compaction_summary,
    stamp_context_threshold,
    target_cost,
    with_summary_text,
)
from ptc_agent.agent.middleware.compaction.offloading import (
    is_idle,
    offload_view,
    record_offloads,
    recorded_offloads,
    removed_attachments,
    select_offloads,
)
from ptc_agent.agent.middleware.file_operations.multimodal_strip import remove_attachments
from ptc_agent.agent.middleware.compaction.window import (
    WindowCarry,
    WindowCoverage,
    trim,
    window_cut,
)
from ptc_agent.agent.middleware.model_resilience import as_sent
from ptc_agent.agent.middleware.reasoning_compat import ORIGIN_KEY
from ptc_agent.agent.transcript import TranscriptTarget, Window
from ptc_agent.agent.transcript.classify import human_kind, is_summary_message
from ptc_agent.agent.transcript.identity import DIGEST_VERSION
from ptc_agent.agent.transcript.pointer import (
    aexport_transcript,
    shell_reads_transcripts,
    transcript_target,
)

if TYPE_CHECKING:
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.config.agent import AgentConfig

logger = logging.getLogger(__name__)

# The coverage read sits in front of a turn. It walks the thread's turn
# boundaries, which takes tens of milliseconds; past this the trim waits for
# the next pass rather than hold the turn.
_COVERAGE_TIMEOUT_S = 5.0


@contextmanager
def _unstreamed() -> Iterator[None]:
    """Tag the model calls made inside ``nostream``. The model node calls the
    model with the config it finds here, so the tag reaches the call, and the
    callbacks that bill and trace it still see it."""
    ambient = var_child_runnable_config.get() or {}
    token = var_child_runnable_config.set(
        {**ambient, "tags": [*(ambient.get("tags") or ()), TAG_NOSTREAM]}
    )
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def _user_texts(messages: Sequence[AnyMessage]) -> list[str]:
    """What the user wrote in ``messages``, oldest first, with what an
    earlier OpenAI summary among them kept: the messages Codex keeps beside
    its item, none of the rows the runtime stamps."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        if is_summary_message(message):
            texts.extend(kept_user_texts(message))
        elif human_kind(message) in ("plain", "steering"):
            texts.append(message.text)
    return texts


def _reported_usage(response: ModelResponse) -> tuple[AIMessage, int, int] | None:
    """The newest AI message in ``response`` that reports its input tokens,
    with its (input, output) tokens; None when none does."""
    for msg in reversed(response.result or ()):
        if not isinstance(msg, AIMessage):
            continue
        usage = extract_token_usage(msg)
        input_tokens = usage.get("input_tokens", 0)
        if input_tokens > 0:
            return msg, input_tokens, usage.get("output_tokens", 0)
    return None


def _log_usage(call: str, message: AIMessage | None) -> None:
    """Log what a call the turn's stream never shows read from the cache."""
    usage = (message.usage_metadata if message is not None else None) or {}
    details = usage.get("input_token_details") or {}
    logger.info(
        "[Compaction] %s: %s in, %s cached, %s out",
        call,
        usage.get("input_tokens"),
        details.get("cache_read"),
        usage.get("output_tokens"),
    )


def _summary_fields(compaction: Compaction) -> dict[str, Any]:
    """The summarize complete signal's payload, one shape for the live signal
    and the stamp replay reads it back from."""
    return {
        "summary_length": len(compaction.summary.text),
        "original_message_count": compaction.original_count,
        "summary_text": compaction.summary.text,
        "source": compaction.summary.source,
    }


class CompactionMiddleware(AgentMiddleware):
    """Keeps an agent's context under its threshold (see the module docstring).

    Every per-run value is read from graph state on each call, never kept on
    the instance, so one instance serves concurrent runs.
    """

    state_schema = CompactionState

    def __init__(
        self,
        summarizer: Summarizer,
        *,
        token_threshold: int,
        keep_messages: int,
        offload: OffloadSettings = OffloadSettings(),
        backend: SandboxBackend | None = None,
        workspace_id: str | None = None,
        native: bool = True,
        receives: Callable[[Any], Callable[[Any], bool]] | None = None,
        page_tokens: Callable[[Any], int | None] | None = None,
    ) -> None:
        """
        Args:
            summarizer: Writes the summaries, and counts the tokens that
                trigger one.
            token_threshold: The context size that triggers a summary.
            keep_messages: The newest messages a summary leaves out.
            offload: Tier 1 settings.
            backend: The sandbox, which holds the transcript and the
                attachments a summary saves. None for a build without a
                sandbox, which has neither.
            workspace_id: The workspace the turn runs in, whose folder must
                reach the transcript before anything points at it.
            native: Whether a provider that can compact the conversation
                itself does.
            receives: Which attachment blocks a call to a model is sent as
                they are (``MultimodalStripMiddleware.receives``). The strip
                sits inside this middleware and turns the rest into short
                placeholders, so only these count toward the trigger and the
                fit. None counts every attachment.
            page_tokens: What a call to a model is charged for a PDF page
                (``MultimodalStripMiddleware.pdf_page_tokens``), or None
                where that is not known, which counts ``PDF_PAGE_TOKENS``.
        """
        super().__init__()
        self._summarizer = summarizer
        self._token_threshold = token_threshold
        self._keep_messages = keep_messages
        self._offload = offload
        self._backend = backend
        self._workspace_id = workspace_id
        self._native = native
        self._receives = receives
        self._page_tokens = page_tokens
        # The notes folder this agent's summaries name; see with_scratchpad_notes.
        self._notes_dir: str | None = None
        # The main agent's window; see with_window.
        self._coverage: WindowCoverage | None = None
        self._carries: tuple[WindowCarry, ...] = ()

    @classmethod
    def for_agent(
        cls,
        config: AgentConfig,
        *,
        backend: SandboxBackend | None = None,
        workspace_id: str | None = None,
        receives: Callable[[Any], Callable[[Any], bool]] | None = None,
        page_tokens: Callable[[Any], int | None] | None = None,
    ) -> CompactionMiddleware | None:
        """The middleware ``config.compaction`` describes, or None when it is off.

        The ``provider_compaction`` feature is read here rather than with the
        user's settings, so a build that resolves no user, a system insight,
        still honors an operator who turned it off.
        """
        settings = config.compaction
        if not settings.enabled:
            return None
        return cls(
            Summarizer.for_agent(config),
            token_threshold=settings.token_threshold,
            keep_messages=settings.keep_messages,
            offload=OffloadSettings.from_config(settings),
            backend=backend,
            workspace_id=workspace_id,
            native=settings.native and config.feature_enabled("provider_compaction"),
            receives=receives,
            page_tokens=page_tokens,
        )

    # =========================================================================
    # Tier 2: every model call, summarizing first at the threshold
    # =========================================================================

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Send the model the view, summarizing its start first once the
        context reaches the threshold, or when the model refuses the view as
        too long, and taking attachments out of what is kept until it fits.
        A call made between turns (``between_turns``) is answered with what
        it asks instead."""
        ensure_message_ids(request.messages)
        view = await self.view(request.messages, request.state)
        await read_ahead(view)
        between = asked()
        if between is not None:
            between.answer = await self._between_turns(request, handler, view, between.ask)
            between.answered = True
            return ModelResponse(result=[])

        system = [request.system_message] if request.system_message is not None else []
        cost = target_cost(
            self._receives(request.model) if self._receives is not None else None,
            self._page_tokens(request.model) if self._page_tokens is not None else None,
        )
        tokens = context_tokens(request.state, view, self._summarizer.counter, system, cost)
        refusal: Exception | None = None
        if tokens < self._token_threshold:
            try:
                return self._answered(await handler(request.override(messages=view)))
            except Exception as exc:
                # Matched across providers, most of which LangChain does not
                # classify as an overflow.
                if not is_context_overflow(exc):
                    raise
                logger.warning("[Compaction] Context overflow, summarizing: %s", exc)
                refusal = exc
        else:
            logger.info("[Compaction] Triggered: %d >= %d tokens", tokens, self._token_threshold)

        cutoff = (
            find_group_safe_cutoff(view, len(view) - self._keep_messages)
            if len(view) > self._keep_messages
            else 0
        )
        if cutoff <= 0:
            # Too few messages to summarize: the attachments are all that can go.
            response, removed, spared = await self._send_fitted(
                request, handler, view, system, refusal=refusal, cost=cost
            )
            return self._answered(
                response, self._removal(request.state, removed), spared=spared
            )

        compaction = await self._summarize(request, handler, view, cutoff)
        # A refusal of the view says nothing of what the summary made room
        # for, so its messages are fitted by estimate like any others.
        response, removed, spared = await self._send_fitted(
            request, handler, compaction.messages, system, cost=cost
        )
        self._stamp_summary(response, compaction)
        update = compaction.update(request.state)
        return self._answered(
            response, {**update, **self._removal(update, removed)}, spared=spared
        )

    async def _summarize(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        view: list[AnyMessage],
        cutoff: int,
    ) -> Compaction:
        """Summarize ``view`` before ``cutoff``, by the agent's provider where
        it compacts the conversation itself."""
        native: Native | None = None
        kind = native_kind(request.model) if self._native else None
        # Claude reads its block only where the kept turns can follow it.
        cut = provider_cutoff(view, cutoff) if kind == "anthropic" else cutoff
        if kind is not None and cut > 0:
            cutoff = cut
            native = functools.partial(self._provider_summary, request, handler)
        return await self._compact(request, view, cutoff, self._transcript_target(), native)

    async def _between_turns(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        view: list[AnyMessage],
        ask: Ask,
    ) -> Compaction | HumanMessage | None:
        """What a call between turns asks for (see ``between_turns``)."""
        if isinstance(ask, HandOff):
            return await self._hand_off(request, handler, view)
        if len(view) <= ask.keep_messages:
            raise ValueError(
                f"Not enough messages to compact. Have {len(view)}, "
                f"need more than {ask.keep_messages} to preserve."
            )
        cutoff = find_group_safe_cutoff(view, len(view) - ask.keep_messages)
        if cutoff <= 0:
            raise ValueError("Cannot determine valid cutoff point for compaction")
        return await self._summarize(request, handler, view, cutoff)

    @staticmethod
    async def _hand_off(
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        view: list[AnyMessage],
    ) -> HumanMessage | None:
        """The view's opening summary written out by the agent's model, when
        it is one only that model's provider can read, else None.

        The summary goes out as the provider's own answer (``adopt``, inside
        ``handler``), from the provider's cache, and the model writes it out
        as text. Tools stay bound so the prefix is the one the cache holds;
        none may be called. Only this model reading that answer can write it,
        so a failure is never moved to a fallback model or retried without it.
        """
        head = view[0] if view and is_summary_message(view[0]) else None
        if not isinstance(head, HumanMessage) or not is_opaque(head) or handed_off(head):
            return None
        with as_sent():
            response = await handler(
                request.override(
                    messages=[head, HumanMessage(HANDOFF_SUMMARY_PROMPT)],
                    tool_choice="none",
                )
            )
        message = next(
            (m for m in reversed(response.result or ()) if isinstance(m, AIMessage)), None
        )
        _log_usage("hand-off", message)
        text = message.text.strip() if message is not None and not message.tool_calls else ""
        written = with_summary_text(head, text) if text else None
        if written is None:
            raise ValueError("the model wrote no hand-off summary")
        return written

    async def _send_fitted(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        messages: list[AnyMessage],
        system: list[Any],
        *,
        refusal: Exception | None = None,
        cost: Callable[[Any], int] | None = None,
    ) -> tuple[ModelResponse, set[str], int]:
        """Send ``messages`` with as many of their attachments taken out as
        it takes to fit. Returns the response, the ids of the messages that
        lose theirs, and what the provider counted for the ones this send
        still carried (see ``_measured_fit``).

        Below the threshold is what fits, by estimate: the oldest go first,
        since the newest are what the agent is working on. Left over it, the
        next call would summarize again at once. A refusal means the estimate
        fell short, so after one every attachment goes. ``refusal`` is one
        ``messages`` already met as they are, which is not sent again.
        """
        removed: set[str] = set()
        if refusal is None:
            removed = self._to_remove(messages, system, everything=False, cost=cost)
            try:
                response = await handler(
                    request.override(messages=self._without(messages, removed))
                )
            except Exception as exc:
                if not is_context_overflow(exc):
                    raise
                refusal = exc
            else:
                more, spared = self._measured_fit(response, messages, removed, system, cost)
                return response, removed | more, spared
        everything = self._to_remove(messages, system, everything=True, cost=cost)
        if everything == removed:
            raise refusal
        logger.warning("[Compaction] Context overflow, removing every attachment kept")
        response = await handler(request.override(messages=self._without(messages, everything)))
        return response, everything, 0

    def _measured_fit(
        self,
        response: ModelResponse,
        messages: list[AnyMessage],
        removed: set[str],
        system: list[Any],
        cost: Callable[[Any], int] | None,
    ) -> tuple[set[str], int]:
        """The ids of the messages whose attachments go too after a send
        fitted by estimate that the provider measured over the threshold, and
        the tokens they took.

        The estimate can fall short of the provider's count, which includes
        the tool schemas and a PDF's real pages. Left over the threshold, the
        next call would summarize again and remove nothing, and so would each
        one after. The measurement corrects the estimate: each attachment
        counts at its estimate times measured over estimated for the view
        that was sent, the oldest going first until the excess is covered.
        """
        usage = _reported_usage(response)
        if usage is None:
            return set(), 0
        _, input_tokens, output_tokens = usage
        over = input_tokens + output_tokens - self._token_threshold
        if over < 0:
            return set(), 0
        fitted = self._without(messages, removed)
        estimate = self._summarizer.counter([*system, *fitted]) + attachment_tokens(fitted, cost)
        if estimate <= 0:
            return set(), 0
        more: set[str] = set()
        spared = 0
        for msg in messages:
            if over < 0:
                break
            if not msg.id or msg.id in removed:
                continue
            taken = round(attachment_tokens([msg], cost) * input_tokens / estimate)
            if taken:
                more.add(msg.id)
                spared += taken
                over -= taken
        return more, spared

    def _to_remove(
        self,
        messages: list[AnyMessage],
        system: list[Any],
        *,
        everything: bool,
        cost: Callable[[Any], int] | None = None,
    ) -> set[str]:
        """The ids of the messages in ``messages`` whose attachments go.

        Only attachments the target is sent count: one it cannot read reaches
        it as the strip's placeholder, whose guidance a removal would replace.
        """
        costs = [(msg.id, attachment_tokens([msg], cost)) for msg in messages]
        costs = [(msg_id, tokens) for msg_id, tokens in costs if tokens and msg_id]
        if everything:
            return {msg_id for msg_id, _ in costs}
        over = (
            self._summarizer.counter([*system, *messages])
            + attachment_tokens(messages, cost)
            - self._token_threshold
        )
        removed: set[str] = set()
        for msg_id, tokens in costs:
            if over < 0:
                break
            removed.add(msg_id)
            over -= tokens
        return removed

    @staticmethod
    def _without(messages: list[AnyMessage], removed: set[str]) -> list[AnyMessage]:
        return remove_attachments(messages, removed, pages=pdf_pages)

    @staticmethod
    def _removal(state: Mapping[str, Any], removed: set[str]) -> dict[str, Any]:
        """The update recording ``removed`` beside what ``state`` holds."""
        if not removed:
            return {}
        return {"_removed_attachment_ids": removed_attachments(state) | removed}

    async def _compact(
        self,
        request: ModelRequest,
        view: list[AnyMessage],
        cutoff: int,
        transcript: TranscriptTarget | None,
        native: Native | None = None,
    ) -> Compaction:
        """Summarize ``view`` before ``cutoff``, by ``native`` when given,
        this agent's model being the fallback (see ``Summarizer.compact``).

        Start is outside the try, so a start that fails opened no window. A
        cancellation still emits error before it propagates: otherwise the
        stream would keep an orphan start and its compaction window open.
        """
        self._emit_context_signal("summarize", "start")
        try:
            compaction = await self._summarizer.compact(
                request.messages,
                view,
                cutoff,
                backend=self._backend,
                workspace_id=self._workspace_id,
                transcript=transcript,
                fallback=request.model,
                notes_dir=self._notes_dir,
                window=Window.of(request.state),
                # Its own action: a client built before the count reads any
                # summarize signal but start as the summary's end.
                progress=lambda tokens: self._emit_context_signal(
                    "summary_progress", "progress", summary_tokens=tokens
                ),
                native=native,
            )
        except BaseException as e:
            self._emit_context_signal("summarize", "error", error=str(e))
            raise
        self._emit_context_signal("summarize", "complete", **_summary_fields(compaction))
        return compaction

    @staticmethod
    async def _provider_summary(
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        to_summarize: list[AnyMessage],
    ) -> NativeAnswer:
        """The agent's provider compacting ``to_summarize`` itself, with the
        summary prompt in place of its own where it takes one (Claude).

        Sent through ``handler`` so the request is the conversation's own:
        the same system prompt, tools and thinking, which its cache and the
        thinking in the kept turns are checked against. It stays out of the
        turn's message stream, where its reply would read as the answer.
        OpenAI's goes after the user's own messages it summarized.
        """
        settings = compaction_settings(
            request.model, request.model_settings, NATIVE_SUMMARY_PROMPT
        )
        with _unstreamed():
            response = await handler(
                request.override(messages=to_summarize, model_settings=settings)
            )
        message = next(
            (m for m in reversed(response.result or ()) if isinstance(m, AIMessage)), None
        )
        _log_usage("provider summary", message)
        answer = native_answer(message, _user_texts(to_summarize))
        if message is None or answer is None:
            stop = (message.response_metadata or {}).get("stop_reason") if message else None
            raise ValueError(f"no compaction (stop reason {stop})")
        return NativeAnswer(answer, (message.response_metadata or {}).get(ORIGIN_KEY))

    @staticmethod
    def _stamp_summary(response: ModelResponse, compaction: Compaction) -> None:
        """Stamp the summary on the response that answered from it, for
        replay to show where it fired: live, its signal comes right before
        this response streams."""
        for msg in reversed(response.result or ()):
            if isinstance(msg, AIMessage):
                stamp_compaction_summary(
                    msg, compaction.event["summary_message"].id, _summary_fields(compaction)
                )
                return

    def _answered(
        self, response: ModelResponse, update: dict[str, Any] | None = None, *, spared: int = 0
    ) -> ExtendedModelResponse:
        """``response``, with ``update`` and the state the next call reads:
        the context size this call measured, less the ``spared`` tokens of
        attachments ``update`` takes out of the next one, and when the model
        answered, which the next turn's Tier 1 measures its pause from."""
        input_tokens, output_tokens = self._extract_token_usage(response)
        return ExtendedModelResponse(
            model_response=response,
            command=Command(
                update={
                    **(update or {}),
                    "_cached_input_tokens": max(input_tokens - spared, 0),
                    "_cached_output_tokens": output_tokens,
                    "_last_model_response_at": time.time(),
                }
            ),
        )

    def _extract_token_usage(self, response: ModelResponse) -> tuple[int, int]:
        """The (input, output) tokens the newest AI message reports, emitted
        to the frontend with the threshold they fill and stamped with it for
        replay, or (0, 0) when none does."""
        usage = _reported_usage(response)
        if usage is None:
            return (0, 0)
        msg, input_tokens, output_tokens = usage
        logger.debug("[Compaction] Token usage: input=%d, output=%d", input_tokens, output_tokens)
        stamp_context_threshold(msg, self._token_threshold)
        self._emit_context_signal(
            "token_usage",
            "complete",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            threshold=self._token_threshold,
        )
        return (input_tokens, output_tokens)

    # =========================================================================
    # Tier 1: trimming old tool args and Read results after a long pause
    # =========================================================================

    @override
    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Tier 1 when the turn starts after a long pause, then the window's
        trim. A resume after an interrupt does not pass through here, since
        the graph picks up where it stopped; an orchestrator re-entry does."""
        update = await self._offload_idle(state)
        trimmed = await self._trim({**state, **update})
        return {**update, **(trimmed or {})} or None

    async def _offload_idle(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Tier 1, once per turn and only when the model last answered more
        than the idle threshold ago: by then the provider's prompt cache has
        expired, so hiding part of the prefix costs no cache hit, while doing
        it mid-turn would throw a warm one away. An argument is hidden only
        once the transcript holding it is saved where the workspace can read
        it, since that is then its one copy."""
        if not is_idle(state, self._offload, time.time()):
            return {}
        messages = state["messages"]
        effective = get_effective_messages(messages, state.get("_summarization_event"))
        args, reads = select_offloads(effective, self._offload, recorded_offloads(state))
        if args and await aexport_transcript(
            self._backend,
            self._transcript_target(),
            messages,
            workspace_id=self._workspace_id,
            window=Window.of(state),
        ) is None:
            args = set()
        if args:
            self._emit_context_signal("offload", "complete", kind="args", offloaded_args=len(args))
        if reads:
            self._emit_context_signal(
                "offload", "complete", kind="reads", offloaded_reads=len(reads)
            )
        return record_offloads(state, args, reads)

    async def _trim(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        """The window's trim (see ``window``), or None while it may not:
        without the server to ask (any stack but the main agent's, see
        ``with_window``), before a summary with an anchor, or while the
        server's slices do not hold exactly the messages it would drop, in
        which case the update keeps the refusal.

        A refusal is kept in the checkpoint with the anchor and cut it was
        for, and the digest it was checked under, and not asked again until
        one of them moves: a thread whose slices do not match pays for the
        check once per summary, not per turn, and one backfilled since trims
        after its next summary.
        """
        if self._coverage is None:
            return None
        messages = state["messages"]
        event = state.get("_summarization_event")
        cut = window_cut(messages, event)
        if cut <= 0:
            return None
        asked = {"anchor": event["anchor_message_id"], "cut": cut, "digest": DIGEST_VERSION}
        if state.get("_window_refused") == asked:
            return None
        try:
            async with asyncio.timeout(_COVERAGE_TIMEOUT_S):
                head = await self._coverage(Window.of(state), messages[:cut])
        except Exception:  # noqa: BLE001 - an unanswered check only delays the trim
            logger.warning("[Compaction] window coverage unread", exc_info=True)
            return None
        if head is None:
            logger.info(
                "[Compaction] window waits for the next summary: slices do not "
                "hold the %d messages before the cut",
                cut,
            )
            return {"_window_refused": asked}
        logger.info(
            "[Compaction] window trimmed %d messages, runs up to %d", cut, head.runs
        )
        return trim(state, cut, self._carries, head=head)

    def with_window(
        self, coverage: WindowCoverage, carries: Sequence[WindowCarry] = ()
    ) -> CompactionMiddleware:
        """This middleware, trimming the main agent's checkpoint to its window.

        ``coverage`` asks the server whether turn slices hold the runs a trim
        would drop; ``carries`` keep what other readers counted in them. For
        the main agent's stack only: a subagent's run is bounded, and its
        transcript reads its namespace's whole history.
        """
        main = copy.copy(self)
        main._coverage = coverage
        main._carries = tuple(carries)
        return main

    def with_scratchpad_notes(self, notes_dir: str) -> CompactionMiddleware:
        """This middleware, with each summary naming ``notes_dir``'s files.

        For the main agent's stack only: a subagent compacts its own run,
        which keeps no notes, so its stack holds the plain instance. A copy
        is enough because the instance keeps nothing per invocation.
        """
        main = copy.copy(self)
        main._notes_dir = notes_dir
        return main

    async def view(self, messages: list[AnyMessage], state: Mapping[str, Any]) -> list[AnyMessage]:
        """What this agent's model is sent of ``messages``: what the summary
        trigger counts, and so what the scratchpad's fill counts too."""
        return await offload_view(
            messages,
            state,
            max_length=self._offload.max_length,
            transcript=self._transcript_target(),
            shell=shell_reads_transcripts(self._backend),
        )

    def _transcript_target(self) -> TranscriptTarget | None:
        """This agent's transcript, or None where nothing serves one."""
        try:
            configurable = get_config().get("configurable", {})
        except RuntimeError:
            return None
        return transcript_target(
            self._backend,
            configurable.get("thread_id"),
            str(configurable.get("checkpoint_ns") or ""),
        )

    # =========================================================================
    # Events
    # =========================================================================

    def _emit_context_signal(self, action: str, signal: str, **kwargs: Any) -> None:
        """Emit a context_window event via the stream writer.

        Args:
            action: Action discriminator ("summarize", "summary_progress",
                "offload", "token_usage")
            signal: Signal type ("start", "progress", "complete", or "error")
            **kwargs: Additional payload fields (summary_length, error, etc.)
        """
        try:
            stream_writer = get_stream_writer()
            payload: dict[str, Any] = {
                "type": "context_window",
                "action": action,
                "signal": signal,
            }
            # Include checkpoint_ns for agent identification by streaming handler
            try:
                checkpoint_ns = get_config().get("configurable", {}).get("checkpoint_ns", "")
                if checkpoint_ns:
                    payload["checkpoint_ns"] = checkpoint_ns
            except RuntimeError:
                pass
            payload.update(kwargs)
            stream_writer(payload)
            if signal == "error":
                logger.warning(
                    "[Compaction] Emitted %s error signal: %s", action, kwargs.get("error")
                )
            else:
                logger.debug("[Compaction] Emitted %s %s signal", action, signal)
        except Exception as e:
            logger.debug("Could not emit context_window %s/%s signal: %s", action, signal, e)
