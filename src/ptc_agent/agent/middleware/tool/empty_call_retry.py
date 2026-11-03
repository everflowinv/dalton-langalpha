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

from ptc_agent.agent.middleware.thinking_binding import is_bound_answer
from ptc_agent.agent.transcript.classify import EMPTY_CALL_RETRY_SOURCE
# DALTON-HOOK(empty-turn-guard): the one emptiness rule the transcript already
# renders by — thinking blocks never count as visible text, for any provider.
from ptc_agent.agent.transcript.render import visible_text
# DALTON-HOOK(empty-turn-guard): how a provider-compaction summary call is
# told apart from a turn's own model call, so the guard never retries one.
from src.llms.provider_compaction import is_compaction_request

logger = logging.getLogger(__name__)

# stop_reason values that indicate the model intended to call tools
_TOOL_USE_STOP_REASONS = {"tool_use", "tool_calls"}

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

# DALTON-HOOK(empty-turn-guard): sent after a final reply that said nothing,
# so the model answers the user instead of ending the turn empty again.
_EMPTY_REPLY_HINT = (
    "上一次回复是空的，没有回答用户，也没有调用任何工具。"
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


def _user_sent_turn(messages) -> bool:
    config = _turn_config()
    configurable = config.get("configurable") or {}
    if str(configurable.get("checkpoint_ns") or "").split("|", 1)[0].startswith("task:"):
        return False
    if (config.get("metadata") or {}).get("origin") in ("user", "agent"):
        return True
    return _user_steered(messages)


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
    # compaction call is no shape at all, and the upstream tool-call one wins
    # when both readings fit, so its JSON-hint behavior is unchanged.
    def _retry_kind(self, ai_msg: AIMessage, request) -> str | None:
        if _compaction_call(request):
            return None
        if self._should_retry(ai_msg):
            return "tool_calls"
        if _visible_reply_empty(ai_msg) and _user_sent_turn(_turn_messages(request)):
            return "empty_reply"
        return None

    @staticmethod
    def _hint_for(kind: str) -> str:
        # DALTON-HOOK(empty-turn-guard): each broken shape tells the model
        # what to fix in its own words.
        return _EMPTY_REPLY_HINT if kind == "empty_reply" else _REMEDIATION_HINT

    def _log_retry(self, ai_msg: AIMessage, attempt: int, kind: str) -> None:
        meta = getattr(ai_msg, "response_metadata", {}) or {}
        stop = meta.get("stop_reason") or meta.get("finish_reason")
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

    # DALTON-HOOK(empty-turn-guard): the failure shape changed mid-retry —
    # swap the hint for the current kind's wording, so the next retry is told
    # what is wrong now. The broken reply ahead of it stays the first one:
    # it is what the model has already been sent and answered behind.
    def _rehint(self, request, injected: list, kind: str) -> list:
        hint = HumanMessage(
            content=self._hint_for(kind),
            additional_kwargs={"lc_source": EMPTY_CALL_RETRY_SOURCE},
        )
        request.messages[-1:] = [hint]
        return [injected[0], hint]

    @staticmethod
    def _keep_injected(response, injected: list):
        """Only an answer carrying Claude's thinking needs them kept; any
        other answer goes into history alone, as it did."""
        if not injected or not is_bound_answer(response.result[0]):
            return response
        return dataclasses.replace(response, result=[*injected, *response.result])

    # DALTON-HOOK(empty-turn-guard): retries out on an empty reply — the last
    # reply is swapped for the failure notice, so the turn ends saying what
    # happened instead of nothing at all. The notice inherits the last reply's
    # usage and response metadata, so the turn's token counters and the live
    # stream's finish_reason stay honest, but not its id: the stream's dedupe
    # has already seen that one and would drop the notice.
    def _empty_reply_failure(self, response):
        last = response.result[0]
        configurable = _turn_config().get("configurable") or {}
        logger.warning(
            "[EmptyToolCallRetry] thread=%s final reply still empty after %d "
            "retries; replacing it with a failure notice",
            configurable.get("thread_id"),
            self.max_retries,
        )
        notice = AIMessage(
            content=_EMPTY_REPLY_NOTICE,
            usage_metadata=getattr(last, "usage_metadata", None),
            response_metadata=dict(getattr(last, "response_metadata", None) or {}),
        )
        return dataclasses.replace(response, result=[notice])

    # DALTON-HOOK(empty-turn-guard): the loop now classifies each broken reply,
    # rewords the hint when the failure shape changes mid-retry, and, when an
    # empty final reply exhausts the retries, ends the turn on the failure
    # notice instead of the last empty reply. A broken last reply is logged
    # only while a retry is still left to make.
    def wrap_model_call(self, request, handler):
        injected: list = []
        injected_kind: str | None = None
        kind: str | None = None
        for attempt in range(1 + self.max_retries):
            response = handler(request)
            ai_msg = response.result[0]
            kind = self._retry_kind(ai_msg, request)
            if kind is None:
                return self._keep_injected(response, injected)
            if attempt == self.max_retries:
                break
            self._log_retry(ai_msg, attempt, kind)
            if not injected:
                injected = self._inject_hint(request, ai_msg, self._hint_for(kind))
            elif injected_kind != kind:
                injected = self._rehint(request, injected, kind)
            injected_kind = kind
        if kind == "empty_reply":
            return self._empty_reply_failure(response)
        return self._keep_injected(response, injected)  # last response even if still broken

    # DALTON-HOOK(empty-turn-guard): the async loop, changed the same way.
    async def awrap_model_call(self, request, handler):
        injected: list = []
        injected_kind: str | None = None
        kind: str | None = None
        for attempt in range(1 + self.max_retries):
            response = await handler(request)
            ai_msg = response.result[0]
            kind = self._retry_kind(ai_msg, request)
            if kind is None:
                return self._keep_injected(response, injected)
            if attempt == self.max_retries:
                break
            self._log_retry(ai_msg, attempt, kind)
            if not injected:
                injected = self._inject_hint(request, ai_msg, self._hint_for(kind))
            elif injected_kind != kind:
                injected = self._rehint(request, injected, kind)
            injected_kind = kind
        if kind == "empty_reply":
            return self._empty_reply_failure(response)
        return self._keep_injected(response, injected)  # last response even if still broken
