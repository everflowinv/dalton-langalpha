"""Retry middleware for empty tool_calls with tool_use stop_reason.

Some LLM providers (observed on Anthropic-protocol coding-plan routes) may return
stop_reason="tool_use" but with an empty tool_calls list — the model intended
to call tools but the content was malformed/truncated and the SDK silently
dropped it.  LangGraph sees empty tool_calls and routes to __END__, stopping
the agent mid-task.

This middleware detects that mismatch, injects a remediation hint into the
message history so the model knows what went wrong, and retries the model call.
"""

# DALTON-HOOK(empty-turn-guard): the same retry loop also catches a final
# reply with no visible text at all (thinking-only, whitespace, or nothing)
# on a turn a user sent, and when every retry stays empty the last reply is
# replaced by an explicit failure notice instead of ending the turn as if it
# had been answered (case 65c4641e: a turn that "normally ended" while the
# user got nothing and no failure was ever shown).
import dataclasses
import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM

from ptc_agent.agent.middleware.model_resilience import RESILIENCE_TRACE_ATTR
from ptc_agent.agent.middleware.thinking_binding import is_bound_answer
from ptc_agent.agent.transcript.classify import EMPTY_CALL_RETRY_SOURCE
# DALTON-HOOK(empty-turn-guard): the one emptiness rule the transcript already
# renders by — thinking blocks never count as visible text, for any provider.
from ptc_agent.agent.transcript.render import visible_text
from src.llms.error_classification import UnfinishedReplyError
# DALTON-HOOK(empty-turn-guard): how a provider-compaction summary call is
# told apart from a turn's own model call, so the guard never retries one.
from src.llms.provider_compaction import is_compaction_request


# DALTON-HOOK(refusal-handling): lazy import avoids circular init via src.dalton.middleware.__init__.
def _refusal_fallback():
    from src.dalton.middleware import refusal_fallback
    return refusal_fallback

logger = logging.getLogger(__name__)

# stop_reason values that indicate the model intended to call tools
_TOOL_USE_STOP_REASONS = {"tool_use", "tool_calls"}

# DALTON-HOOK(empty-turn-guard): stop_reason / finish_reason suffixes indicating
# the reply hit the model's output token cap (Gemini, Anthropic, OpenAI;
# session 13e2d7b5, Neon truncated at 65536 output tokens). Suffix match handles
# streaming chunk aggregation such as "STOPMAX_TOKENS" while still matching
# exact values like "MAX_TOKENS".
_OUTPUT_CAP_STOP_REASONS = ("MAX_TOKENS", "max_tokens", "length")

# DALTON-HOOK(empty-turn-guard): stop_reason / finish_reason suffixes indicating
# Gemini generated malformed tool-call arguments and dropped tool_calls
# (session a30afade, gemini-4-carbon-high-oauth ended on MALFORMED_FUNCTION_CALL).
_MALFORMED_CALL_STOP_REASONS = ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL")

_REMEDIATION_HINT = (
    "Your previous response had stop_reason=tool_use but the tool_calls field "
    "was empty. This typically means the JSON in the tool-call arguments was "
    "malformed — most commonly an unescaped quote or control character inside "
    "a long string value, or the response was truncated mid-token.\n\n"
    "If you intended to call a tool, please retry now, paying special attention to:\n"
    "- Every `\"` inside a string value must be escaped as `\\\"`\n"
    "- Every `\\` inside a string value must be escaped as `\\\\`\n"
    "- Raw newlines and control characters are not allowed inside JSON string values\n"
    "- The full arguments object must be valid JSON end-to-end"
)

# DALTON-HOOK(empty-turn-guard): sent after a reply hit the output cap with
# discarded tool calls or no visible text (session 13e2d7b5, Neon 65536-token
# truncation), so the model splits large writes into smaller steps and resumes.
_OUTPUT_CAP_HINT = (
    "上一条回复超出了单条回复的输出上限并被截断，其中的工具调用未执行。"
    "请勿将大段内容或长脚本放入单次工具调用或单条回复中，改为拆分成多次较小的操作分步完成"
    "（例如写大文件时先写骨架、再分段追加）。"
    "请重新发起未执行的步骤，不要重复此前已完成的步骤。"
)

# DALTON-HOOK(empty-turn-guard): sent after a pure-text reply hit the output
# cap with visible text and no tool calls, so the model continues directly from
# where it was cut off while keeping the already-shown prefix in history.
_OUTPUT_CAP_CONTINUE_HINT = (
    "上一条回复超出了单条回复的输出上限并在此被截断。"
    "请直接从被截断处接着输出，不要重复已输出的内容；"
    "若后续需要生成大文件或长脚本，请拆分成多次较小的写入。"
)

# DALTON-HOOK(empty-turn-guard): sent after MALFORMED_FUNCTION_CALL /
# UNEXPECTED_TOOL_CALL (session a30afade) so the model re-emits valid JSON
# tool calls and splits oversized arguments when needed.
_MALFORMED_CALL_HINT = (
    "上一条回复中的工具调用格式错误，未执行。"
    "请确保参数为完整、合法的 JSON 后重新发起调用；"
    "如果参数过长（例如大段文件内容或脚本），请拆分成多次较小的调用。"
)

# DALTON-HOOK(empty-turn-guard): sent after a final reply that said nothing,
# so the model answers the user instead of ending the turn empty again.
_EMPTY_REPLY_HINT = (
    "上一条回复是空的，没有回答用户，也没有调用任何工具。"
    "请根据用户的请求，直接回答或调用需要的工具。"
)

# DALTON-HOOK(empty-turn-guard): what the turn ends on when every retry came
# back empty — a silent end reads as success to the user, so the failure has
# to be said outright. It claims nothing about the work itself: tool calls
# earlier in the turn may well have taken effect, so the user is told to
# check results, not to assume nothing ran.
_EMPTY_REPLY_NOTICE = (
    "这一轮模型没有给出最终回复。之前的操作可能已经执行，请先查看结果，再决定是否重新发送。"
)

# DALTON-HOOK(empty-turn-guard): what the turn ends on when every retry still
# hits the output cap (session 13e2d7b5), so the turn states why it stopped.
_OUTPUT_CAP_NOTICE = (
    "模型回复多次超出输出上限而被截断，任务未完成（被截断回复中的工具调用未执行）。"
    "可以回复“继续”，或把请求拆分成更小的步骤分步完成。"
)

# DALTON-HOOK(empty-turn-guard): what the turn ends on when every retry still
# produces malformed tool calls (session a30afade).
_MALFORMED_CALL_NOTICE = (
    "模型多次生成的工具调用格式错误（均未执行），任务未完成。"
    "请重试，或把请求拆分成更小的步骤后重新发送。"
)

_HINTS = {
    "tool_calls": _REMEDIATION_HINT,
    "output_cap": _OUTPUT_CAP_HINT,
    "malformed_call": _MALFORMED_CALL_HINT,
    "empty_reply": _EMPTY_REPLY_HINT,
}

_EXHAUSTED_NOTICES = {
    "output_cap": _OUTPUT_CAP_NOTICE,
    "malformed_call": _MALFORMED_CALL_NOTICE,
    "empty_reply": _EMPTY_REPLY_NOTICE,
}

_SANITIZED_KINDS = frozenset({"output_cap", "malformed_call"})

# DALTON-HOOK(empty-turn-guard): additional_kwargs key carried by the exhausted
# failure notice AIMessage in checkpoint state, so even a resumed /retry run
# with a fresh middleware instance fails non-user main-agent turns without
# re-invoking the model.
_EXHAUSTED_FAILURE_KWARG = "dalton_exhausted_failure"

_TOOL_BLOCK_TYPES = frozenset({"tool_use", "function_call", "functionCall"})


# DALTON-HOOK(empty-turn-guard): whether an output-capped reply carried any
# tool calls (parsed, invalid, additional_kwargs, or raw content blocks) that
# must be discarded before re-sending.
def _has_tool_calls(ai_msg: AIMessage) -> bool:
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return True
    additional = getattr(ai_msg, "additional_kwargs", None) or {}
    if additional.get("tool_calls") or additional.get("function_call"):
        return True
    content = ai_msg.content
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and (block.get("type") in _TOOL_BLOCK_TYPES or "functionCall" in block)
            for block in content
        )
    return False


# DALTON-HOOK(empty-turn-guard): whether the reply left nothing the user
# could read — no tool call (an invalid one is answered, not final) and no
# visible text once thinking blocks are skipped, which ``visible_text``
# already decides exactly as the transcript renders it. A compaction block
# is a provider's native summary, not a reply: it has no text on purpose.
def _visible_reply_empty(ai_msg: AIMessage) -> bool:
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return False
    content = ai_msg.content
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "compaction" for block in content
    ):
        return False
    return not visible_text(content).strip()


# DALTON-HOOK(empty-turn-guard): the run's config as the model node sees it.
# ``Runtime`` carries no config of its own, so langgraph's ambient get_config
# is the one route in (market_watch.py reads it the same way); empty outside
# a runnable context, which reads as provenance unknown.
def _turn_config() -> dict:
    try:
        return get_config()
    except RuntimeError:
        return {}


# DALTON-HOOK(empty-turn-guard): a compaction or summary call made inside the
# model node by CompactionMiddleware, which reaches the model through this
# stack's own handler. Its reply is not a turn's answer — a native compaction
# has no text on purpose, and a self-summary may come back empty for the
# summarizer's own retry — so neither is ever retried here. Read off the
# request (the provider-summary settings carry the compaction marker) and
# the TAG_NOSTREAM tag CompactionMiddleware puts on every such call; the
# content-block check above is the same rule from the answer's side.
def _compaction_call(request) -> bool:
    if is_compaction_request(getattr(request, "model_settings", None) or {}):
        return True
    # A between-turns ask (the model-switch hand-off) is the summarizer's
    # own call too, made outside any turn the user waits on.
    from ptc_agent.agent.middleware.compaction.between_turns import asked

    if asked() is not None:
        return True
    return TAG_NOSTREAM in (_turn_config().get("tags") or ())


# DALTON-HOOK(empty-turn-guard): whether a user's steer reached this turn,
# wherever it landed — the same two windows quiet.py reads (after the run's
# boundary, and ahead of its input back to the previous turn's last reply),
# so a steer that arrived while the turn was starting counts too.
def _user_steered(messages) -> bool:
    # Imported at call time: quiet imports this package's direct_mcp, which
    # would race this module's own package init at import time.
    from src.tools.wakeups.quiet import _last_boundary, _user_steer

    start = _last_boundary(messages)
    if any(_user_steer(m) for m in messages[start + 1 :]):
        return True
    for message in reversed(messages[: max(start, 0)]):
        if isinstance(message, AIMessage):
            break
        if _user_steer(message):
            return True
    return False


# DALTON-HOOK(empty-turn-guard): whether this model call serves a turn
# someone sent and waits on. A notification turn (a subagent's report-back,
# a market alert, a wakeup) may end with nothing, and a subagent's report is
# read by its parent, not the user: ``origin`` rides the turn's config
# metadata (``_origin_metadata`` stamps every server-posted turn ``system``)
# and the task-prefixed ``checkpoint_ns`` its configurable. ``agent`` is a
# Chief-of-Staff hand-off, whose dispatching thread waits on the result as
# the user does; and a user's steer into any turn makes it one somebody
# waits on, whatever started it. Unknown provenance never retries without a
# steer: fail open, the guard may not fire on a turn nobody waits on.
# DALTON-HOOK(empty-turn-guard): a compaction upstream hands the model a
# summary plus a kept tail, and the summary drops a steer's
# ``steering_delivered`` stamp; the graph state still holds the full turn.
def _turn_messages(request) -> list:
    state = getattr(request, "state", None)
    msgs = state.get("messages") if isinstance(state, dict) else None
    return list(msgs) if msgs else list(request.messages)


def _subagent_call() -> bool:
    configurable = _turn_config().get("configurable") or {}
    return str(configurable.get("checkpoint_ns") or "").split("|", 1)[0].startswith("task:")


def _user_sent_turn(messages) -> bool:
    if _subagent_call():
        return False
    config = _turn_config()
    if (config.get("metadata") or {}).get("origin") in ("user", "agent"):
        return True
    return _user_steered(messages)


# DALTON-HOOK(empty-turn-guard): stop reason indicating the reply hit the
# output token cap (session 13e2d7b5, Neon 65536-token truncation; suffix
# match covers merged streaming chunks like STOPMAX_TOKENS; also catches
# OpenAI Responses API status="incomplete" with reason="max_output_tokens").
# Shared with SubagentEventCaptureMiddleware and RunSSEProducer.
def output_cap_stop_reason(ai_msg: AIMessage) -> str | None:
    meta = getattr(ai_msg, "response_metadata", {}) or {}
    for key in ("stop_reason", "finish_reason"):
        reason = meta.get(key)
        if isinstance(reason, str) and reason.endswith(_OUTPUT_CAP_STOP_REASONS):
            return reason
    if meta.get("status") == "incomplete":
        details = meta.get("incomplete_details")
        inc_reason = (
            details.get("reason")
            if isinstance(details, dict)
            else getattr(details, "reason", None)
        )
        if inc_reason == "max_output_tokens":
            return "incomplete:max_output_tokens"
    return None


# DALTON-HOOK(empty-turn-guard): stop reason indicating Gemini produced
# malformed tool-call syntax and discarded tool calls (session a30afade).
# Shared with SubagentEventCaptureMiddleware and RunSSEProducer.
def malformed_call_stop_reason(ai_msg: AIMessage) -> str | None:
    meta = getattr(ai_msg, "response_metadata", {}) or {}
    for key in ("stop_reason", "finish_reason"):
        reason = meta.get(key)
        if isinstance(reason, str) and reason.endswith(_MALFORMED_CALL_STOP_REASONS):
            return reason
    return None


class EmptyToolCallRetryMiddleware(AgentMiddleware):
    """Retries when stop_reason indicates tool_use but tool_calls is empty."""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    def _should_retry(self, ai_msg: AIMessage) -> bool:
        meta = getattr(ai_msg, "response_metadata", {}) or {}
        stop = meta.get("stop_reason") or meta.get("finish_reason") or ""
        return (
            stop in _TOOL_USE_STOP_REASONS
            and not getattr(ai_msg, "tool_calls", None)
            and not getattr(ai_msg, "invalid_tool_calls", None)
        )

    # DALTON-HOOK(empty-turn-guard): which broken shape this reply is — a
    # compaction call is no shape at all, the upstream tool-call one wins
    # first, output_cap and malformed_call catch replies whose calls must not
    # run regardless of turn origin or subagent ns, and empty_reply handles
    # silent user-sent turns last.
    def _retry_kind(self, ai_msg: AIMessage, request) -> str | None:
        if _compaction_call(request):
            return None
        if self._should_retry(ai_msg):
            return "tool_calls"
        if output_cap_stop_reason(ai_msg) is not None:
            return "output_cap"
        if malformed_call_stop_reason(ai_msg) is not None:
            return "malformed_call"
        # DALTON-HOOK(refusal-handling): treat safety-intercepted replies on user-sent turns as retryable.
        if (_visible_reply_empty(ai_msg) or _refusal_fallback().is_refusal_reply(ai_msg)) and _user_sent_turn(_turn_messages(request)):
            return "empty_reply"
        return None

    @staticmethod
    def _hint_for(kind: str, ai_msg: AIMessage | None = None) -> str:
        # DALTON-HOOK(empty-turn-guard): each broken shape tells the model
        # what to fix in its own words; pure-text output_cap replies ask the
        # model to continue directly from the cut-off point.
        if (
            kind == "output_cap"
            and ai_msg is not None
            and visible_text(ai_msg.content, attachments=False).strip()
            and not _has_tool_calls(ai_msg)
        ):
            return _OUTPUT_CAP_CONTINUE_HINT
        return _HINTS.get(kind, _REMEDIATION_HINT)

    # DALTON-HOOK(empty-turn-guard): strip tool calls, tool blocks, thinking
    # blocks, and attachment placeholders from an output_cap/malformed_call
    # reply before re-sending or saving to history, keeping only plain visible
    # text plus id and metadata; return None when empty.
    @staticmethod
    def _sanitize_output_cap_reply(ai_msg: AIMessage) -> AIMessage | None:
        text = visible_text(ai_msg.content, attachments=False)
        if not text.strip():
            return None
        return AIMessage(
            content=text,
            id=ai_msg.id,
            response_metadata=dict(getattr(ai_msg, "response_metadata", None) or {}),
            usage_metadata=getattr(ai_msg, "usage_metadata", None),
        )

    def _log_retry(self, ai_msg: AIMessage, attempt: int, kind: str) -> None:
        meta = getattr(ai_msg, "response_metadata", {}) or {}
        stop = meta.get("stop_reason") or meta.get("finish_reason")
        if kind in _SANITIZED_KINDS:
            # DALTON-HOOK(empty-turn-guard): sessions 13e2d7b5 & a30afade;
            # log stop reason, output_tokens, and retry index.
            usage = getattr(ai_msg, "usage_metadata", None)
            output_tokens = (
                usage.get("output_tokens")
                if isinstance(usage, dict)
                else getattr(usage, "output_tokens", None)
            )
            matched = (
                output_cap_stop_reason(ai_msg)
                if kind == "output_cap"
                else malformed_call_stop_reason(ai_msg)
            )
            label = "output capped" if kind == "output_cap" else "malformed tool call"
            logger.warning(
                "[EmptyToolCallRetry] %s (stop_reason=%s, output_tokens=%s), "
                "retrying (%d/%d)",
                label,
                matched or stop,
                output_tokens,
                attempt + 1,
                self.max_retries,
            )
            return
        if kind == "empty_reply":
            # DALTON-HOOK(empty-turn-guard): an empty final reply has no
            # tool_calls story to tell, only that nothing was said.
            logger.warning(
                "[EmptyToolCallRetry] final reply is empty (stop_reason=%s), "
                "retrying (%d/%d)",
                stop,
                attempt + 1,
                self.max_retries,
            )
            return
        logger.warning(
            "[EmptyToolCallRetry] stop_reason=%s but tool_calls is empty, "
            "retrying (%d/%d)",
            stop,
            attempt + 1,
            self.max_retries,
        )

    def _inject_hint(self, request, ai_msg: AIMessage, hint: str) -> list:
        """Append the broken AIMessage + a corrective HumanMessage to request.

        Replaces ``request.messages`` with a new list so we don't mutate any
        list the caller may still reference. Returns the two messages, which
        go into history ahead of the answer: it was produced behind them, and
        its thinking stays valid only while every later request sends them too.
        """
        injected = [
            ai_msg,
            HumanMessage(
                content=hint,
                additional_kwargs={"lc_source": EMPTY_CALL_RETRY_SOURCE},
            ),
        ]
        request.messages = list(request.messages) + injected
        return injected

    # DALTON-HOOK(empty-turn-guard): build the sanitized output-capped reply
    # (when visible text exists) plus corrective hint; omit the AI message when
    # visible text is empty so no empty/tool-only message is re-sent.
    @staticmethod
    def _build_output_cap_injection(sanitized: AIMessage | None, hint: str) -> list:
        hint_msg = HumanMessage(
            content=hint,
            additional_kwargs={"lc_source": EMPTY_CALL_RETRY_SOURCE},
        )
        return [sanitized, hint_msg] if sanitized is not None else [hint_msg]

    def _inject_output_cap(self, request, sanitized: AIMessage | None, hint: str) -> list:
        injected = self._build_output_cap_injection(sanitized, hint)
        request.messages = list(request.messages) + injected
        return injected

    # DALTON-HOOK(empty-turn-guard): when non-sanitized shapes alternate before
    # any output_cap/malformed_call, replace both the preceding AI reply and
    # its hint with the latest pair so every hint follows its own reply.
    @staticmethod
    def _rehint(request, injected: list, ai_msg: AIMessage, hint_text: str) -> list:
        hint = HumanMessage(
            content=hint_text,
            additional_kwargs={"lc_source": EMPTY_CALL_RETRY_SOURCE},
        )
        request.messages = [*request.messages[:-2], ai_msg, hint]
        return [*injected[:-2], ai_msg, hint]

    # DALTON-HOOK(empty-turn-guard): keep injected messages when bound thinking
    # requires them, or when any output_cap/malformed_call occurred in this loop
    # so every already-streamed prefix stays in checkpoint history ahead of it.
    @staticmethod
    def _keep_injected(response, injected: list, *, keep_all: bool = False):
        """Only an answer carrying reasoning bound to its prefix (or an
        output_cap / malformed_call retry) needs them kept; any other answer
        goes into history alone."""
        if not injected:
            return response
        if not keep_all and not is_bound_answer(response.result[0]):
            return response
        return dataclasses.replace(response, result=[*injected, *response.result])

    # DALTON-HOOK(empty-turn-guard): retries out on an empty, output-capped, or
    # malformed-call reply — on empty_reply the last reply is swapped for the
    # failure notice; on output_cap/malformed_call every sanitized visible
    # prefix and hint remain in history with the failure notice appended at the
    # end, carrying ``{"kind": kind, "fail_turn": bool}`` in
    # ``additional_kwargs[_EXHAUSTED_FAILURE_KWARG]`` computed at failure time
    # so after_agent/aafter_agent (including after /retry checkpoint resume)
    # preserves the original non-user turn decision without re-checking origin.
    def _exhausted_failure(
        self,
        response,
        kind: str,
        *,
        prefix: list = (),
        turn_messages: list = (),
    ):
        last = response.result[0]
        configurable = _turn_config().get("configurable") or {}
        reason_map = {
            "output_cap": "hit output cap",
            "malformed_call": "malformed tool call",
        }
        reason = reason_map.get(kind, "final reply still empty")
        logger.warning(
            "[EmptyToolCallRetry] thread=%s %s after %d "
            "retries; replacing it with a failure notice",
            configurable.get("thread_id"),
            reason,
            self.max_retries,
        )
        last_meta = dict(getattr(last, "response_metadata", None) or {})
        additional_kwargs: dict = {}
        if kind in _SANITIZED_KINDS:
            fail_turn = not _subagent_call() and not _user_sent_turn(turn_messages)
            additional_kwargs[_EXHAUSTED_FAILURE_KWARG] = {
                "kind": kind,
                "fail_turn": fail_turn,
            }
        notice = AIMessage(
            content=_EXHAUSTED_NOTICES[kind],
            additional_kwargs=additional_kwargs,
            usage_metadata=getattr(last, "usage_metadata", None),
            response_metadata=last_meta,
        )
        return dataclasses.replace(response, result=[*prefix, notice])

    # DALTON-HOOK(empty-turn-guard): shared step-update and exhaustion helpers
    # for sync/async loops. Every output_cap/malformed_call appends its
    # sanitized reply and hint to both ``injected`` and ``request.messages``.
    # Non-sanitized retries keep their replacement behavior (replacing both
    # reply and hint with the latest attempt, even when consecutive attempts
    # share the same kind) before any sanitized kind, or append after one
    # without overwriting its hint.
    def _advance_retry(
        self,
        request,
        ai_msg: AIMessage,
        kind: str,
        injected: list,
        rehintable: bool,
    ) -> tuple[list, bool]:
        hint_text = self._hint_for(kind, ai_msg)
        if kind in _SANITIZED_KINDS:
            sanitized = self._sanitize_output_cap_reply(ai_msg)
            added = self._inject_output_cap(request, sanitized, hint_text)
            return [*injected, *added], False
        if not injected or not rehintable:
            added = self._inject_hint(request, ai_msg, hint_text)
            return [*injected, *added], True
        injected = self._rehint(request, injected, ai_msg, hint_text)
        return injected, True

    def _finish_retry(
        self,
        response,
        request,
        kind: str | None,
        injected: list,
        last_sanitized_kind: str | None,
        *,
        all_refused: bool = False,
    ):
        failure_kind = kind if kind in _SANITIZED_KINDS else (last_sanitized_kind or kind)
        # DALTON-HOOK(refusal-handling): return the safety-refusal notice when every attempt was refused.
        if failure_kind == "empty_reply" and (
            refusal := _refusal_fallback().handle_refusal_exhausted(response, all_refused=all_refused, max_retries=self.max_retries, config=_turn_config())
        ) is not None:
            return refusal
        if failure_kind in _EXHAUSTED_NOTICES:
            prefix = injected if last_sanitized_kind is not None else []
            return self._exhausted_failure(
                response,
                failure_kind,
                prefix=prefix,
                turn_messages=_turn_messages(request),
            )
        return self._keep_injected(response, injected)

    # DALTON-HOOK(empty-turn-guard): the loop now classifies each broken reply,
    # appends every output_cap/malformed_call sanitized segment + hint, keeps
    # each non-sanitized hint paired with its own reply, and preserves history.
    def wrap_model_call(self, request, handler):
        injected: list = []
        last_sanitized_kind: str | None = None
        rehintable = False
        kind: str | None = None
        all_refused = True  # DALTON-HOOK(refusal-handling)
        for attempt in range(1 + self.max_retries):
            response = handler(request)
            ai_msg = response.result[0]
            all_refused = all_refused and _refusal_fallback().is_refusal_reply(ai_msg)
            kind = self._retry_kind(ai_msg, request)
            if kind is None:
                return self._keep_injected(
                    response, injected, keep_all=(last_sanitized_kind is not None)
                )
            if kind in _SANITIZED_KINDS:
                last_sanitized_kind = kind
            if attempt == self.max_retries:
                if kind in _SANITIZED_KINDS:
                    injected = [
                        *injected,
                        *self._build_output_cap_injection(
                            self._sanitize_output_cap_reply(ai_msg),
                            self._hint_for(kind, ai_msg),
                        ),
                    ]
                break
            self._log_retry(ai_msg, attempt, kind)
            injected, rehintable = self._advance_retry(
                request, ai_msg, kind, injected, rehintable
            )
        return self._finish_retry(
            response, request, kind, injected, last_sanitized_kind, all_refused=all_refused
        )

    # DALTON-HOOK(empty-turn-guard): the async loop, using the same helpers.
    async def awrap_model_call(self, request, handler):
        injected: list = []
        last_sanitized_kind: str | None = None
        rehintable = False
        kind: str | None = None
        all_refused = True  # DALTON-HOOK(refusal-handling)
        for attempt in range(1 + self.max_retries):
            response = await handler(request)
            ai_msg = response.result[0]
            all_refused = all_refused and _refusal_fallback().is_refusal_reply(ai_msg)
            kind = self._retry_kind(ai_msg, request)
            if kind is None:
                return self._keep_injected(
                    response, injected, keep_all=(last_sanitized_kind is not None)
                )
            if kind in _SANITIZED_KINDS:
                last_sanitized_kind = kind
            if attempt == self.max_retries:
                if kind in _SANITIZED_KINDS:
                    injected = [
                        *injected,
                        *self._build_output_cap_injection(
                            self._sanitize_output_cap_reply(ai_msg),
                            self._hint_for(kind, ai_msg),
                        ),
                    ]
                break
            self._log_retry(ai_msg, attempt, kind)
            injected, rehintable = self._advance_retry(
                request, ai_msg, kind, injected, rehintable
            )
        return self._finish_retry(
            response, request, kind, injected, last_sanitized_kind, all_refused=all_refused
        )

    # DALTON-HOOK(empty-turn-guard): after the checkpoint commits the failure
    # notice on a non-user main-agent turn (session a30afade), read the marker
    # directly from state["messages"][-1].additional_kwargs and raise
    # UnfinishedReplyError with a resilience trace so the run (and any /retry
    # resuming from that checkpoint) settles as error -> failed.
    def _raise_if_non_user_exhausted(self, state) -> None:
        messages = state.get("messages") if isinstance(state, dict) else None
        if not messages:
            return
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return
        marker = (getattr(last, "additional_kwargs", None) or {}).get(
            _EXHAUSTED_FAILURE_KWARG
        )
        if isinstance(marker, dict):
            exhausted_kind = marker.get("kind")
            fail_turn = marker.get("fail_turn")
        else:
            exhausted_kind = marker
            fail_turn = None
        if exhausted_kind not in _SANITIZED_KINDS:
            return
        if fail_turn is False:
            return
        if fail_turn is not True and (_subagent_call() or _user_sent_turn(messages)):
            return
        notice_text = (
            last.content
            if isinstance(last.content, str) and last.content
            else _EXHAUSTED_NOTICES[exhausted_kind]
        )
        last_meta = getattr(last, "response_metadata", None) or {}
        model_name = last_meta.get("model_name")
        exc = UnfinishedReplyError(notice_text)
        trace: dict = {}
        if isinstance(model_name, str) and model_name:
            trace["model"] = model_name
        setattr(exc, RESILIENCE_TRACE_ATTR, trace)
        raise exc

    def after_agent(self, state, runtime=None) -> None:
        self._raise_if_non_user_exhausted(state)

    async def aafter_agent(self, state, runtime=None) -> None:
        self._raise_if_non_user_exhausted(state)
