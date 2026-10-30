"""Run-scoped quota exhaustion in ModelResilienceMiddleware — R3a #1.

A 402 balance refusal (grok "Grok Build usage balance exhausted", 1,922× on
10/30) must not burn its four attempts on every model call of a run, and a
quota 429 whose own body says it lifts hours later (Antigravity "Resets in
6h49m18s", 34×) is the same verdict with a clock on it. The memory lives on
the middleware instance — one per turn build — and crosses no run boundary.
"""

from __future__ import annotations

import asyncio

import pytest

from ptc_agent.agent.middleware.model_resilience import ModelResilienceMiddleware

GROK_402_MESSAGE = (
    "Error code: 402 - {'error': 'Grok Build usage balance exhausted'}"
)

ANTIGRAVITY_LONG_RESET_429 = (
    "Antigravity API error 429: Individual quota reached. Please upgrade "
    "your subscription to increase your limits. Resets in 6h49m18s."
)

ANTIGRAVITY_SHORT_RESET_429 = (
    "Antigravity API error 429: Individual quota reached. Please upgrade "
    "your subscription to increase your limits. Resets in 5s."
)


class _FakeModel:
    def __init__(self, name):
        self.model = name

    def model_copy(self, *, update):
        return _FakeModel(self.model)


class _FakeRequest:
    """Duck-typed stand-in for langchain's ModelRequest, as the sibling
    resilience suite builds them."""

    def __init__(self, model, messages=None, state=None, model_settings=None):
        self.model = model
        self.messages = messages if messages is not None else []
        self.state = state if state is not None else {"messages": []}
        self.model_settings = model_settings if model_settings is not None else {}

    def override(self, *, model=None, messages=None):
        return _FakeRequest(
            model if model is not None else self.model,
            messages if messages is not None else self.messages,
            self.state,
            self.model_settings,
        )


def _refusal(message: str, status: int) -> Exception:
    exc = Exception(message)
    exc.status_code = status
    return exc


def _grok_402() -> Exception:
    return _refusal(GROK_402_MESSAGE, 402)


def _make_middleware(primary, fallbacks=(), **kwargs):
    kwargs.setdefault("initial_delay", 1.0)
    kwargs.setdefault("jitter", False)
    return ModelResilienceMiddleware(
        primary_name=getattr(primary, "model", "primary-model"),
        primary_client=primary,
        fallbacks=list(fallbacks),
        **kwargs,
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """The backoff waits real seconds by default; the retry-count semantics
    under test do not need them."""
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    return None


async def _instant_sleep(_delay, *_, **__):
    return None


@pytest.fixture
def events(monkeypatch):
    """Capture the ui records a fallback switch pushes (as the sibling
    resilience suite captures them)."""
    from langgraph._internal._constants import CONFIG_KEY_SEND
    from langgraph.constants import CONF

    captured = []
    monkeypatch.setattr(
        "langgraph.config.get_stream_writer", lambda: captured.append
    )
    monkeypatch.setattr(
        "langgraph.graph.ui.get_stream_writer", lambda: captured.append
    )
    monkeypatch.setattr(
        "langgraph.graph.ui.get_config",
        lambda: {CONF: {CONFIG_KEY_SEND: lambda writes: None}},
    )
    return captured


@pytest.mark.asyncio
async def test_grok_402_falls_back_after_one_attempt_not_four(no_sleep, events):
    """The incident shape: `attempts_on_from=4` on every call. The refusal is
    payment-declined; every retry answers identically, so the candidate is
    dropped on its first failure."""
    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("gemini-4-argon-high-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _grok_402()
        return "ok"

    result = await mw.awrap_model_call(_FakeRequest(primary), handler)

    assert result == "ok"
    assert calls.count("grok-4.7") == 1
    switches = [e["props"] for e in events
                if e.get("name") == "model_fallback"]
    assert switches and switches[0]["attempts_on_from"] == 1
    assert switches[0]["status_code"] == 402


@pytest.mark.asyncio
async def test_within_one_run_the_exhausted_primary_is_not_asked_again(
    no_sleep, events
):
    """The same middleware instance serves the turn's later calls; the second
    call starts on the fallback, not on the balance-exhausted primary."""
    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("gemini-4-argon-high-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _grok_402()
        return "ok"

    first = await mw.awrap_model_call(_FakeRequest(primary), handler)
    calls_before = list(calls)
    second = await mw.awrap_model_call(_FakeRequest(primary), handler)

    assert first == second == "ok"
    assert calls_before.count("grok-4.7") == 1
    assert calls[len(calls_before):] == ["gemini-4-argon-high-oauth"]


@pytest.mark.asyncio
async def test_every_candidate_exhausted_raises_the_remembered_refusal(no_sleep):
    """With nothing left to try, the second call raises the primary's own 402
    without spending a request — not an empty attempt-trace crash."""
    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("spare")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        raise _grok_402()

    with pytest.raises(Exception, match="usage balance exhausted"):
        await mw.awrap_model_call(_FakeRequest(primary), handler)
    spent = len(calls)

    with pytest.raises(Exception, match="usage balance exhausted"):
        await mw.awrap_model_call(_FakeRequest(primary), handler)

    assert spent == 2  # one refusal per candidate, once
    assert len(calls) == spent  # the second call spent nothing


@pytest.mark.asyncio
async def test_a_quota_429_past_the_budget_is_not_retried_and_benches(no_sleep):
    """`Resets in 6h49m18s` outlasts the whole 15s backoff budget (1+2+4+8):
    no retries, then benched for the rest of the run."""
    primary, fallback = _FakeModel("gemini-4-argon-high-oauth"), _FakeModel("muse-wonder-1.0-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 1

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 1


@pytest.mark.asyncio
async def test_a_quota_429_inside_the_budget_keeps_its_retries(no_sleep):
    """`Resets in 5s` fits inside the backoff budget (1+2+4=7s) — the model
    keeps today's full retry count and is not benched for the run."""
    primary, fallback = _FakeModel("gemini-4-argon-high-oauth"), _FakeModel("muse-wonder-1.0-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _refusal(ANTIGRAVITY_SHORT_RESET_429, 429)
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    # max_retries=3 → four attempts on the primary, the pre-fix behaviour.
    assert calls.count("gemini-4-argon-high-oauth") == 4

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 8


@pytest.mark.asyncio
async def test_a_benched_candidate_is_readmitted_once_its_horizon_passes(no_sleep):
    """A named horizon is a clock, not a life sentence: past it the candidate
    is asked again (the run may long outlast a short quota window)."""
    primary, fallback = _FakeModel("gemini-4-argon-high-oauth"), _FakeModel("muse-wonder-1.0-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []
    primary_refuses = True

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary and primary_refuses:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls == ["gemini-4-argon-high-oauth", "muse-wonder-1.0-oauth"]
    primary_refuses = False

    # Still inside the 6h49m horizon: benched, the fallback answers alone.
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls[-1] == "muse-wonder-1.0-oauth"

    # The horizon, aged out as far as the bench is concerned.
    import time as _time

    refusal = _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
    mw._quota_exhausted["gemini-4-argon-high-oauth"] = (
        _time.monotonic() - 1.0,
        refusal,
    )
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls[-1] == "gemini-4-argon-high-oauth"


@pytest.mark.asyncio
async def test_a_transient_429_is_untouched_by_the_bench(no_sleep):
    """A plain rate limit never names a quota or a reset: full retries, and
    the next call starts on the primary again."""
    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("gemini-4-argon-high-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _refusal(
                "Error code: 429 - {'error': 'Rate limit exceeded'}", 429
            )
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("grok-4.7") == 4
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("grok-4.7") == 8


@pytest.mark.asyncio
async def test_a_repeat_refusal_after_lapse_re_arms_the_bench(no_sleep):
    """codex r1 #8: an expired bench must not stand between the run and a
    fresh one. The horizon lapses, the candidate is asked again, refuses
    again — the bench comes back with a new deadline, and the next call
    inside it is skipped again."""
    import time as _time

    primary, fallback = _FakeModel("gemini-4-argon-high-oauth"), _FakeModel("muse-wonder-1.0-oauth")
    mw = _make_middleware(primary, [(fallback.model, fallback)])
    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 1

    # The horizon lapses; the primary is asked once more and refuses again.
    mw._quota_exhausted["gemini-4-argon-high-oauth"] = (
        _time.monotonic() - 1.0,
        _refusal(ANTIGRAVITY_LONG_RESET_429, 429),
    )
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 2

    # Re-armed: the third call, inside the NEW horizon, skips the primary.
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls.count("gemini-4-argon-high-oauth") == 2


@pytest.mark.asyncio
async def test_a_benched_candidate_is_never_named_as_the_switch_target(no_sleep, events):
    """codex r1 #7: after fb1 is benched, a failing primary switches to fb2
    — the notice names fb2, and fb1 is never dialed."""
    primary = _FakeModel("grok-4.7")
    fb1 = _FakeModel("gemini-4-argon-high-oauth")
    fb2 = _FakeModel("muse-wonder-1.0-oauth")
    mw = _make_middleware(primary, [(fb1.model, fb1), (fb2.model, fb2)])
    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is fb1:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        if req.model is primary:
            raise _refusal("Error code: 400 - no reasoning here", 400)
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    spent = list(calls)
    # fb1 got benched on the first pass.
    assert "gemini-4-argon-high-oauth" in spent

    seen_switches = len(events)
    calls.clear()
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert "gemini-4-argon-high-oauth" not in calls, "the benched model is not dialed"

    switches = [
        e["props"] for e in events[seen_switches:] if e.get("name") == "model_fallback"
    ]
    assert switches, "the primary's own switch is announced"
    assert all(p["to_model"] != "gemini-4-argon-high-oauth" for p in switches)


@pytest.mark.asyncio
async def test_the_primarys_refusal_is_the_error_when_only_it_was_benched(no_sleep):
    """codex r1 #7: the primary sits the call out on its bench, every
    remaining candidate fails — the raised error is the primary's own
    refusal, not the last fallback's, and the trace carries the fallback
    attempts."""
    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("spare")
    mw = _make_middleware(primary, [(fallback.model, fallback)])

    async def handler(req):
        if req.model is primary:
            raise _grok_402()
        raise _refusal("Error code: 400 - spare refuses too", 400)

    # First call: the primary's own 402 is records[0], so it already leads.
    with pytest.raises(Exception, match="usage balance exhausted"):
        await mw.awrap_model_call(_FakeRequest(primary), handler)

    # Second call: primary benched (records would lead with the spare's
    # 400) — the 402 still leads the error.
    with pytest.raises(Exception, match="usage balance exhausted") as caught:
        await mw.awrap_model_call(_FakeRequest(primary), handler)
    trace = getattr(caught.value, "__model_resilience__", None)
    assert trace, "the attempt trace rides the raised refusal"
    assert trace["model"] == "grok-4.7"
    attempted = {record["model"] for record in trace["attempted_models"]}
    assert attempted == {"grok-4.7", "spare"}


def test_the_lane_key_is_stable_across_model_node_executions():
    """The lane key drops the per-execution segment of the namespace, so
    every model call of one graph is one lane. Shapes are the real ones:
    LangGraph composes ``<parent>|<node>:<task id>`` (NS_SEP "|", NS_END
    ":"), the task id minted per execution — the main agent's model node
    reads ``model:<uuid>``, a subagent's ``task:<id>|model:<uuid>``."""
    from langgraph.constants import NS_SEP

    from ptc_agent.agent.middleware.model_resilience import _stable_lane

    assert NS_SEP == "|"  # the composition the shapes below are written in

    # Main agent: two different executions of the model node, one lane.
    assert _stable_lane("model:1f0e5bd1-2c3d-4e5f-8a90-000000000001") == ""
    assert _stable_lane("model:9d8c7b6a-5e4f-4032-b110-000000000002") == ""
    assert _stable_lane("") == ""

    # A subagent: its task namespace is the lane, whatever the model node's
    # execution id; a sibling task is a lane of its own.
    task_ns = "task:LPn8CwQAwG8A"
    assert (
        _stable_lane(f"{task_ns}|model:1f0e5bd1-2c3d-4e5f-8a90-000000000001")
        == _stable_lane(f"{task_ns}|model:9d8c7b6a-5e4f-4032-b110-000000000002")
        == task_ns
    )
    assert _stable_lane("task:53tvog1JqXzP|model:aaa") == "task:53tvog1JqXzP"
    assert _stable_lane("task:53tvog1JqXzP|model:aaa") != _stable_lane(
        f"{task_ns}|model:aaa"
    )


@pytest.mark.asyncio
async def test_one_fallback_notice_for_a_whole_main_agent_run(events, monkeypatch):
    """codex r2 #3's repro, on the real graph: four model calls through
    ``create_agent``, the primary refusing with the production 402 on the
    first — the main lane sees exactly ONE fallback notice for the run,
    failure-driven on the first call and never repeated by the bench-driven
    skips after it. ``_lane`` is the real one; the namespace's per-execution
    segment is what it has to survive."""
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver

    from ptc_agent.agent.middleware.model_resilience import ModelResilienceMiddleware

    class GrokRefuses(BaseChatModel):
        """The model the run is configured on: every call is the
        production 402 balance refusal."""

        @property
        def _llm_type(self) -> str:
            return "grok-402"

        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

        def _generate(self, *args, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, **kwargs):
            raise _refusal(GROK_402_MESSAGE, 402)

    class SpareAnswers(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "spare"

        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

        def _generate(self, *args, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, **kwargs):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="ok"))]
            )

    primary, spare = GrokRefuses(), SpareAnswers()
    agent = create_agent(
        model=primary,
        tools=[],
        middleware=[
            ModelResilienceMiddleware(
                primary_name="grok-4.7",
                primary_client=primary,
                fallbacks=[("gemini-4-argon-high-oauth", spare)],
                max_retries=0,
                initial_delay=0.0,
            )
        ],
        checkpointer=InMemorySaver(),
    )

    cfg = {"configurable": {"thread_id": "a1b2c3d4-0000-4000-8000-000000000000"}}
    for _ in range(4):
        state = await agent.ainvoke({"messages": [HumanMessage("go")]}, cfg)
        assert state["messages"][-1].content == "ok"

    switches = [e["props"] for e in events if e.get("name") == "model_fallback"]
    assert len(switches) == 1, f"one notice per run, got {len(switches)}"
    assert switches[0]["from_model"] == "grok-4.7"
    assert switches[0]["to_model"] == "gemini-4-argon-high-oauth"
    assert switches[0]["attempts_on_from"] == 1
    assert switches[0]["status_code"] == 402
