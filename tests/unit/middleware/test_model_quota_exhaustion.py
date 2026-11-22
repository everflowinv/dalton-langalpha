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
    scope_key = mw._quota_scope_key("gemini-4-argon-high-oauth", primary)
    mw._quota_exhausted[scope_key] = (
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
    scope_key = mw._quota_scope_key("gemini-4-argon-high-oauth", primary)
    mw._quota_exhausted[scope_key] = (
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


@pytest.mark.asyncio
async def test_a_benched_primary_is_not_readmitted_when_the_fallbacks_stream_cuts(
    no_sleep, events
):
    """Upstream #651's truncation refusal under the Dalton bench: the primary
    sits the run out on its 402 bench while the fallback's every stream gets
    cut. The cut reply is refused and retried once (a cut that repeats is the
    server's limit), the run raises the primary's remembered 402, and the
    next call of the run still never re-asks the benched primary."""
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    def _cut_reply():
        # The shape a Responses stream leaves when the server closes it
        # before the response's terminal event: an id, no terminal fields.
        return ModelResponse(
            result=[
                AIMessage(
                    content=[{"type": "reasoning", "id": "rs_cut", "summary": []}],
                    response_metadata={"id": "resp_1"},
                )
            ]
        )

    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("spare")
    mw = _make_middleware(primary, [(fallback.model, fallback)])
    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is primary:
            raise _grok_402()
        return _cut_reply()

    with pytest.raises(Exception, match="usage balance exhausted"):
        await mw.awrap_model_call(_FakeRequest(primary), handler)
    # One 402, then the cut twice: the repeating cut gets its one retry.
    assert calls == ["grok-4.7", "spare", "spare"]

    calls.clear()
    with pytest.raises(Exception, match="usage balance exhausted") as caught:
        await mw.awrap_model_call(_FakeRequest(primary), handler)
    assert calls == ["spare", "spare"], "the benched primary is not re-asked"
    trace = getattr(caught.value, "__model_resilience__", None)
    assert trace, "the attempt trace rides the raised refusal"
    assert trace["model"] == "grok-4.7"
    assert trace["attempted_models"][0]["attempts"] == 0  # benched, never dialed


@pytest.mark.asyncio
async def test_the_missing_item_repair_answers_on_the_fallback_while_the_primary_sits_benched(
    no_sleep, events
):
    """Upstream #651's 404 repair under the Dalton bench: the fallback's
    "Item with id 'rs_cut' not found" strips the bare reasoning item and
    answers on the resend, while the benched primary is neither re-asked nor
    named as a switch target."""
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage, HumanMessage

    sealed = {
        "type": "reasoning",
        "id": "rs_done",
        "summary": [],
        "encrypted_content": "opaque",
    }
    bare = {"type": "reasoning", "id": "rs_cut", "summary": []}
    history = [
        HumanMessage("q", id="h-1"),
        AIMessage(content=[sealed, {"type": "text", "text": "a"}], id="a-1"),
        AIMessage(content=[bare], id="a-2"),
        HumanMessage("what happened?", id="h-2"),
    ]

    def _not_found():
        exc = Exception("Item with id 'rs_cut' not found.")
        exc.status_code = 404
        exc.param = "input"
        return exc

    primary, fallback = _FakeModel("grok-4.7"), _FakeModel("spare")
    mw = _make_middleware(primary, [(fallback.model, fallback)])
    calls = []
    seen = []
    not_found_raised = False

    async def handler(req):
        calls.append(req.model.model)
        seen.append([m.id for m in req.messages])
        if req.model is primary:
            raise _grok_402()
        nonlocal not_found_raised
        if not not_found_raised:
            not_found_raised = True
            raise _not_found()
        return ModelResponse(result=[AIMessage("ok")])

    result = await mw.awrap_model_call(_FakeRequest(primary, history), handler)

    assert calls == ["grok-4.7", "spare", "spare"]
    # The resend dropped the bare item the server never stored; the sealed
    # one carries itself and stays.
    assert seen[1:] == [["h-1", "a-1", "a-2", "h-2"], ["h-1", "a-1", "h-2"]]
    assert result.result[0].content == "ok"

    switches = [e["props"] for e in events if e.get("name") == "model_fallback"]
    assert switches and switches[0]["from_model"] == "grok-4.7"
    assert all(p["to_model"] == "spare" for p in switches)

    calls.clear()
    assert await mw.awrap_model_call(_FakeRequest(primary, history), handler)
    assert calls == ["spare"], "the benched primary is not re-asked"


@pytest.mark.asyncio
async def test_quota_exhaustion_shared_via_redis_across_instances_and_recovers_after_ttl(
    no_sleep, events, monkeypatch
):
    """Instance A records quota exhaustion to Redis; Instance B skips the
    exhausted model and routes directly to fallback within TTL, recovers after
    TTL expiry, and degrades gracefully to local behavior if Redis raises."""
    import time
    from unittest.mock import create_autospec

    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    from ptc_agent.agent.middleware.model_resilience import (
        DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS,
    )
    from src.dalton.llm.oauth_refresh import OAuthRebuildSpec, tag_oauth_client
    from src.utils.cache.redis_cache import RedisCacheClient

    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    # Prevent time.monotonic from staying cached when we advance fake_now
    fake_mono = [500.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_mono[0])

    store: dict[str, tuple[float, dict]] = {}
    ttl_calls: list[int | None] = []

    async def _redis_get(key: str):
        entry = store.get(key)
        if entry is None:
            return None
        exp_at, payload = entry
        if fake_now[0] >= exp_at:
            del store[key]
            return None
        return payload

    async def _redis_set(key: str, value, ttl: int | None = None):
        ttl_calls.append(ttl)
        eff_ttl = ttl if ttl is not None else DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS
        store[key] = (fake_now[0] + eff_ttl, value)
        return True

    cache_mock = create_autospec(RedisCacheClient, instance=True)
    cache_mock.get.side_effect = _redis_get
    cache_mock.set.side_effect = _redis_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache_mock)

    primary_a = _FakeModel("gemini-4-neon-high")
    tag_oauth_client(
        primary_a,
        OAuthRebuildSpec(
            user_id="mark-xie",
            model_name="gemini-4-neon-high",
            provider="antigravity",
        ),
    )
    primary_b = _FakeModel("gemini-4-neon-high")
    tag_oauth_client(
        primary_b,
        OAuthRebuildSpec(
            user_id="mark-xie",
            model_name="gemini-4-neon-high",
            provider="antigravity",
        ),
    )
    fallback = _FakeModel("spare-model")

    mw_a = _make_middleware(primary_a, [(fallback.model, fallback)])
    mw_b = _make_middleware(primary_b, [(fallback.model, fallback)])

    calls_a: list[str] = []

    async def handler_a(req):
        calls_a.append(req.model.model)
        if req.model is primary_a:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return ModelResponse(result=[AIMessage("ok-a")])

    res_a = await mw_a.awrap_model_call(_FakeRequest(primary_a), handler_a)
    assert res_a.result[0].content == "ok-a"
    assert calls_a == ["gemini-4-neon-high", "spare-model"]
    assert "dalton:quota_exhausted:mark-xie:antigravity:model:gemini-4-neon-high" in store
    # 6h49m18s == 24558s
    assert ttl_calls[-1] == 24558

    # Instance B (fresh middleware / worker) within TTL skips primary immediately
    calls_b: list[str] = []

    async def handler_b(req):
        calls_b.append(req.model.model)
        if req.model is primary_b:
            raise AssertionError("Instance B should not call exhausted primary inside TTL")
        return ModelResponse(result=[AIMessage("ok-b")])

    res_b = await mw_b.awrap_model_call(_FakeRequest(primary_b), handler_b)
    assert res_b.result[0].content == "ok-b"
    assert calls_b == ["spare-model"]

    # After TTL expires, fresh Instance C retries primary and succeeds
    fake_now[0] += 25000.0
    fake_mono[0] += 25000.0
    primary_c = _FakeModel("gemini-4-neon-high")
    tag_oauth_client(
        primary_c,
        OAuthRebuildSpec(
            user_id="mark-xie",
            model_name="gemini-4-neon-high",
            provider="antigravity",
        ),
    )
    mw_c = _make_middleware(primary_c, [(fallback.model, fallback)])
    calls_c: list[str] = []

    async def handler_c(req):
        calls_c.append(req.model.model)
        return ModelResponse(result=[AIMessage("recovered")])

    res_c = await mw_c.awrap_model_call(_FakeRequest(primary_c), handler_c)
    assert res_c.result[0].content == "recovered"
    assert calls_c == ["gemini-4-neon-high"]

    # Default conservative TTL used when refusal has infinity horizon (402 / unparsed)
    store.clear()
    await mw_a._aremember_quota_exhausted("gemini-4-neon-high", float("inf"), _grok_402(), primary_a)
    assert ttl_calls[-1] == DEFAULT_QUOTA_EXHAUSTION_TTL_SECONDS

    # Redis errors must fail open to local behavior without raising
    broken_cache = create_autospec(RedisCacheClient, instance=True)
    broken_cache.get.side_effect = RuntimeError("redis down")
    broken_cache.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: broken_cache)

    mw_err = _make_middleware(primary_b, [(fallback.model, fallback)])
    calls_err: list[str] = []

    async def handler_err(req):
        calls_err.append(req.model.model)
        if req.model is primary_b:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return ModelResponse(result=[AIMessage("ok-fallback")])

    res_err = await mw_err.awrap_model_call(_FakeRequest(primary_b), handler_err)
    assert res_err.result[0].content == "ok-fallback"
    assert calls_err == ["gemini-4-neon-high", "spare-model"]
    # Second call in same instance still uses local memory despite broken Redis
    calls_err.clear()
    res_err2 = await mw_err.awrap_model_call(_FakeRequest(primary_b), handler_err)
    assert res_err2.result[0].content == "ok-fallback"
    assert calls_err == ["spare-model"]


@pytest.mark.asyncio
async def test_oauth_account_switch_uses_distinct_redis_key_and_pre_dispatch_blocks_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth cooldown Redis key includes subscription credential identity and blocks pre-dispatch."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from typing import Any
    from unittest.mock import create_autospec
    from langchain_core.messages import ToolMessage
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )
    from ptc_agent.agent.middleware.background_subagent.task_actions import (
        handle_task_action,
    )
    from ptc_agent.agent.middleware.background_subagent import dispatch as bg_dispatch
    from ptc_agent.agent.middleware.background_subagent.spawn import SpawnError
    from ptc_agent.agent.middleware.model_resilience import (
        check_oauth_quota_cooldown,
        quota_redis_key,
    )
    from src.dalton.llm.oauth_refresh import (
        OAuthRebuildSpec,
        credential_identity,
        tag_oauth_client,
    )
    from src.utils.cache.redis_cache import RedisCacheClient

    store: dict[str, dict] = {}
    fake_cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(k: str):
        return store.get(k)

    async def fake_set(k: str, v: dict, ttl=None):
        store[k] = v

    fake_cache.get.side_effect = fake_get
    fake_cache.set.side_effect = fake_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: fake_cache)

    # 1. Credential identity includes account_id / updated_at without touching tokens
    cid_a = credential_identity(account_id="acct-A")
    cid_b = credential_identity(account_id="acct-B")
    cid_ts1 = credential_identity(account_id="", updated_at=datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))
    cid_ts2 = credential_identity(account_id="", updated_at=datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc))
    assert cid_a != cid_b
    assert cid_ts1 != cid_ts2

    client_a = _FakeModel("claude-opus-oauth")
    client_a.metadata = {"billing_type": "oauth", "pricing_provider": "claude-oauth"}
    tag_oauth_client(
        client_a,
        OAuthRebuildSpec(
            user_id="user-1",
            model_name="claude-opus-oauth",
            provider="claude-oauth",
            account_id="acct-A",
            credential_id=cid_a,
        ),
    )

    client_b = _FakeModel("claude-opus-oauth")
    client_b.metadata = {"billing_type": "oauth", "pricing_provider": "claude-oauth"}
    tag_oauth_client(
        client_b,
        OAuthRebuildSpec(
            user_id="user-1",
            model_name="claude-opus-oauth",
            provider="claude-oauth",
            account_id="acct-B",
            credential_id=cid_b,
        ),
    )

    key_a = quota_redis_key("claude-opus-oauth", client_a)
    key_b = quota_redis_key("claude-opus-oauth", client_b)
    assert key_a is not None and key_b is not None and key_a != key_b

    # Put acct-A in cooldown; acct-B must NOT inherit acct-A's cooldown
    await fake_set(
        key_a,
        {
            "expires_at": datetime(2026, 11, 21, 8, 30, tzinfo=timezone.utc).timestamp(),
            "message": "quota exhausted",
            "status_code": 429,
            "model": "claude-opus-oauth",
        },
        ttl=3600,
    )
    # Force current clock before 2026-11-21T08:30Z
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: datetime(2026, 11, 21, 7, 0, tzinfo=timezone.utc).timestamp(),
    )

    msg_a = await check_oauth_quota_cooldown(client_a)
    assert msg_a is not None
    assert "claude-opus-oauth 的订阅额度已用完" in msg_a
    assert "2026-11-21 16:30 HKT" in msg_a
    assert "可以换用其他模型派发，或等恢复后再派" in msg_a
    assert await check_oauth_quota_cooldown(client_b) is None

    # 2. Pre-dispatch refuses Task init without registering or spawning subagent
    mw = BackgroundSubagentMiddleware(
        subagent_types=frozenset({"research"}),
        default_model_client=client_a,
    )
    register_calls: list[Any] = []
    monkeypatch.setattr(mw.registry, "register", create_autospec(mw.registry.register, side_effect=lambda *a, **kw: register_calls.append(kw)))
    handler_calls: list[Any] = []

    async def fake_handler(req):
        handler_calls.append(req)
        return ToolMessage(content="should not run", tool_call_id="tc-1", name="Task")

    request = SimpleNamespace(
        tool_call={
            "id": "tc-1",
            "name": "Task",
            "args": {
                "action": "init",
                "subagent_type": "research",
                "description": "scan",
                "prompt": "do research",
            },
        },
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "t-1", "user_id": "user-1"}}),
    )
    reply = await handle_task_action(mw, request, fake_handler)
    assert isinstance(reply, ToolMessage)
    assert reply.status == "error"
    assert "claude-opus-oauth 的订阅额度已用完" in str(reply.content)
    assert "2026-11-21 16:30 HKT" in str(reply.content)
    assert register_calls == []
    assert handler_calls == []

    # 3. Direct dispatch (dispatch_background_subagent) also blocks before register when in cooldown
    with pytest.raises(SpawnError, match="claude-opus-oauth 的订阅额度已用完"):
        await bg_dispatch.dispatch_background_subagent(
            mw,
            {"research": object()},
            subagent_type="research",
            description="scan",
            prompt="do research",
            parent_thread_id="t-1",
            run_id="r-1",
            base_configurable={"user_id": "user-1"},
        )
    assert register_calls == []


def test_sync_wrap_model_call_shares_redis_quota_state_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync wrap_model_call and _next_tried read/write Redis quota state identically to async path."""
    from unittest.mock import create_autospec
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage
    from src.dalton.llm.oauth_refresh import OAuthRebuildSpec, tag_oauth_client
    from src.utils.cache.redis_cache import RedisCacheClient

    store: dict[str, dict] = {}
    fake_cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(k: str):
        return store.get(k)

    async def fake_set(k: str, v: dict, ttl=None):
        _ = ttl
        store[k] = v
        return True

    fake_cache.get.side_effect = fake_get
    fake_cache.set.side_effect = fake_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: fake_cache)

    primary = _FakeModel("primary-oauth")
    tag_oauth_client(
        primary,
        OAuthRebuildSpec(
            user_id="user-sync-1",
            model_name="primary-oauth",
            provider="codex-oauth",
        ),
    )
    fallback = _FakeModel("fallback-m")

    mw_a = _make_middleware(primary, [(fallback.model, fallback)], max_retries=1, initial_delay=0.0)
    mw_b = _make_middleware(primary, [(fallback.model, fallback)], max_retries=1, initial_delay=0.0)

    calls_a: list[str] = []

    def handler_a(req):
        calls_a.append(req.model.model)
        if req.model.model == "primary-oauth":
            exc = RuntimeError(
                "Error code: 429 - {'error': {'code': 'rate_limit_exceeded', "
                "'message': 'Subscription quota exhausted.', 'type': 'rate_limit_error'}}"
            )
            exc.status_code = 429  # type: ignore[attr-defined]
            raise exc
        return ModelResponse(result=[AIMessage("from-fallback-a")])

    req_a = _FakeRequest(primary)
    res_a = mw_a.wrap_model_call(req_a, handler_a)
    assert res_a.result[0].content == "from-fallback-a"
    assert calls_a == ["primary-oauth", "fallback-m"]
    assert any("primary-oauth" in k for k in store)

    # Instance B (fresh local memory) skips primary via Redis on sync wrap_model_call
    calls_b: list[str] = []

    def handler_b(req):
        calls_b.append(req.model.model)
        return ModelResponse(result=[AIMessage("from-fallback-b")])

    res_b = mw_b.wrap_model_call(_FakeRequest(primary), handler_b)
    assert res_b.result[0].content == "from-fallback-b"
    assert calls_b == ["fallback-m"]

    # _next_tried on a fresh instance C also skips Redis-benched candidate
    mw_c = _make_middleware(primary, [(fallback.model, fallback)], max_retries=1, initial_delay=0.0)
    candidates = [("other", primary), ("primary-oauth", primary), ("fallback-m", fallback)]
    assert mw_c._next_tried(_FakeRequest(primary), candidates, 0, None) == 2


def _make_antigravity_client(
    model_name: str,
    *,
    user_id: str = "user-1",
    provider: str = "antigravity-oauth",
    account_id: str = "acct-google-1",
    credential_id: str = "cred-google-1",
    custom_entry: dict | None = None,
    billing_type: str | None = "oauth",
) -> _FakeModel:
    from src.dalton.llm.oauth_refresh import OAuthRebuildSpec, tag_oauth_client

    client = _FakeModel(model_name)
    if billing_type is not None:
        client.metadata = {
            "billing_type": billing_type,
            "pricing_provider": provider,
            "manifest_model": model_name,
        }
    tag_oauth_client(
        client,
        OAuthRebuildSpec(
            user_id=user_id,
            model_name=model_name,
            provider=provider,
            account_id=account_id,
            credential_id=credential_id,
            custom_entry=custom_entry,
        ),
    )
    return client


def _make_memory_redis_cache(
    store: dict[str, dict],
    ttl_calls: list[tuple[str, int | None]] | None = None,
):
    from unittest.mock import create_autospec

    from src.utils.cache.redis_cache import RedisCacheClient

    cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(key: str):
        return store.get(key)

    async def fake_set(key: str, value: dict, ttl=None):
        if ttl_calls is not None:
            ttl_calls.append((key, ttl))
        store[key] = value
        return True

    cache.get.side_effect = fake_get
    cache.set.side_effect = fake_set
    return cache


@pytest.mark.asyncio
async def test_antigravity_vendor_cooldown_shared_with_oauth_billing_and_isolated_by_vendor_and_account(
    no_sleep, events, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With realistic billing_type='oauth', OAuth calls do not auto-fallback; exhausting one
    Antigravity model immediately cools down all models of the same upstream vendor on that
    account (0 upstream requests) while leaving other vendors and other Google accounts unaffected."""
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    from ptc_agent.agent.middleware.model_resilience import (
        RESILIENCE_TRACE_ATTR,
        check_oauth_quota_cooldown,
        quota_redis_key,
    )

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    neon_high = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u1", credential_id="cred-a"
    )
    neon_medium = _make_antigravity_client(
        "gemini-4-neon-medium-oauth", user_id="u1", credential_id="cred-a"
    )
    hydrogen_high = _make_antigravity_client(
        "gemini-4-hydrogen-high-oauth", user_id="u1", credential_id="cred-a"
    )
    argon_low = _make_antigravity_client(
        "gemini-4-argon-low-oauth", user_id="u1", credential_id="cred-a"
    )
    claude_opus = _make_antigravity_client(
        "claude-opus-5-5-high-oauth", user_id="u1", credential_id="cred-a"
    )
    claude_sonnet = _make_antigravity_client(
        "claude-sonnet-5-5-low-oauth", user_id="u1", credential_id="cred-a"
    )
    gpt_oss = _make_antigravity_client(
        "gpt-oss-120b-medium-oauth", user_id="u1", credential_id="cred-a"
    )
    neon_other_acct = _make_antigravity_client(
        "gemini-4-neon-medium-oauth",
        user_id="u1",
        account_id="acct-google-2",
        credential_id="cred-b",
    )

    # Disjoint `:vendor:` keys grouped by upstream vendor (google / anthropic / openai)
    google_key = "dalton:quota_exhausted:u1:antigravity-oauth:vendor:google:cred-a"
    anthropic_key = "dalton:quota_exhausted:u1:antigravity-oauth:vendor:anthropic:cred-a"
    openai_key = "dalton:quota_exhausted:u1:antigravity-oauth:vendor:openai:cred-a"
    assert quota_redis_key("gemini-4-neon-high-oauth", neon_high) == google_key
    assert quota_redis_key("gemini-4-neon-medium-oauth", neon_medium) == google_key
    assert quota_redis_key("gemini-4-hydrogen-high-oauth", hydrogen_high) == google_key
    assert quota_redis_key("gemini-4-argon-low-oauth", argon_low) == google_key
    assert quota_redis_key("claude-opus-5-5-high-oauth", claude_opus) == anthropic_key
    assert quota_redis_key("claude-sonnet-5-5-low-oauth", claude_sonnet) == anthropic_key
    assert quota_redis_key("gpt-oss-120b-medium-oauth", gpt_oss) == openai_key
    assert (
        quota_redis_key("gemini-4-neon-medium-oauth", neon_other_acct)
        == "dalton:quota_exhausted:u1:antigravity-oauth:vendor:google:cred-b"
    )

    # Call 1: Neon High (billing_type="oauth") exhausts Google pool; OAuth does not fallback
    # even when fallbacks are passed, and records exactly 1 attempt (no duplicate attempts=0).
    mw_neon = _make_middleware(
        neon_high,
        [("claude-opus-5-5-high-oauth", claude_opus)],
    )
    calls_neon: list[str] = []

    async def neon_handler(req):
        calls_neon.append(req.model.model)
        raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)

    with pytest.raises(Exception, match="Individual quota reached") as first_err:
        await mw_neon.awrap_model_call(_FakeRequest(neon_high), neon_handler)
    assert calls_neon == ["gemini-4-neon-high-oauth"]
    assert google_key in store
    first_trace = getattr(first_err.value, RESILIENCE_TRACE_ATTR, None)
    assert first_trace is not None
    assert len(first_trace["attempted_models"]) == 1
    assert first_trace["attempted_models"][0]["model"] == "gemini-4-neon-high-oauth"
    assert first_trace["attempted_models"][0]["attempts"] == 1

    # Same-vendor models (Hydrogen / Neon Medium) on fresh worker immediately fail with quota
    # exhaustion without dialing upstream.
    for sibling in (hydrogen_high, neon_medium, argon_low):
        mw_sib = _make_middleware(sibling, [("claude-opus-5-5-high-oauth", claude_opus)])
        sib_calls: list[str] = []

        async def sib_handler(req):
            sib_calls.append(req.model.model)
            raise AssertionError("Same-vendor cooled model must not dial upstream")

        with pytest.raises(Exception, match=f"{sibling.model} 的订阅额度已用完") as sib_err:
            await mw_sib.awrap_model_call(_FakeRequest(sibling), sib_handler)
        assert sib_calls == []
        assert getattr(sib_err.value, "status_code", None) == 429
        assert "HKT" in str(sib_err.value)
        sib_trace = getattr(sib_err.value, RESILIENCE_TRACE_ATTR, None)
        assert sib_trace is not None
        assert sib_trace["model"] == sibling.model
        assert sib_trace["attempted_models"][0]["attempts"] == 0
        assert sib_trace["attempted_models"][0]["status_code"] == 429

    # Pre-dispatch check also shares vendor cooldown across all Gemini models, not other vendors/accounts
    msg = await check_oauth_quota_cooldown(hydrogen_high)
    assert msg is not None and "gemini-4-hydrogen-high-oauth 的订阅额度已用完" in msg
    assert await check_oauth_quota_cooldown(claude_opus) is None
    assert await check_oauth_quota_cooldown(gpt_oss) is None
    assert await check_oauth_quota_cooldown(neon_other_acct) is None

    # Different vendors on the SAME account (Anthropic Claude & OpenAI GPT-OSS) succeed
    for other_vendor in (claude_opus, gpt_oss):
        mw_other = _make_middleware(other_vendor, [])
        other_calls: list[str] = []

        async def other_handler(req):
            other_calls.append(req.model.model)
            return ModelResponse(result=[AIMessage(f"ok:{req.model.model}")])

        res = await mw_other.awrap_model_call(_FakeRequest(other_vendor), other_handler)
        assert res.result[0].content == f"ok:{other_vendor.model}"
        assert other_calls == [other_vendor.model]

    # Exhausting Claude Opus cools down Claude Sonnet (same anthropic vendor), not GPT-OSS
    mw_opus = _make_middleware(claude_opus, [])

    async def opus_429(req):
        raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)

    with pytest.raises(Exception, match="Individual quota reached"):
        await mw_opus.awrap_model_call(_FakeRequest(claude_opus), opus_429)
    assert anthropic_key in store

    mw_sonnet = _make_middleware(claude_sonnet, [])
    sonnet_calls: list[str] = []

    async def sonnet_handler(req):
        sonnet_calls.append(req.model.model)
        raise AssertionError("Claude Sonnet should share anthropic vendor cooldown")

    with pytest.raises(Exception, match="claude-sonnet-5-5-low-oauth 的订阅额度已用完"):
        await mw_sonnet.awrap_model_call(_FakeRequest(claude_sonnet), sonnet_handler)
    assert sonnet_calls == []

    # GPT-OSS (openai vendor) remains unaffected
    mw_gpt = _make_middleware(gpt_oss, [])

    async def gpt_handler(req):
        return ModelResponse(result=[AIMessage("gpt-ok")])

    gpt_res = await mw_gpt.awrap_model_call(_FakeRequest(gpt_oss), gpt_handler)
    assert gpt_res.result[0].content == "gpt-ok"

    # Different Google account (cred-b) is isolated and still runs Neon Medium
    mw_acct_b = _make_middleware(neon_other_acct, [])
    acct_b_calls: list[str] = []

    async def acct_b_handler(req):
        acct_b_calls.append(req.model.model)
        return ModelResponse(result=[AIMessage("ok-b")])

    res_b = await mw_acct_b.awrap_model_call(_FakeRequest(neon_other_acct), acct_b_handler)
    assert res_b.result[0].content == "ok-b"
    assert acct_b_calls == ["gemini-4-neon-medium-oauth"]


@pytest.mark.asyncio
async def test_all_skipped_cooldown_classified_as_upstream_plan_limit_with_reset_time(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Gemini same-vendor cooldown skips all candidates (via Redis or local state),
    the raised error carries `reason='plan_limit'` and `plan_limit.until` (plus 429,
    current primary model, and attempts=0) so the frontend renders the subscription
    quota-exhausted card with reset time rather than a generic 429 rate-limit banner."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from ptc_agent.config.agent import CredentialPayer
    from src.server.services.runs.stream_errors import (
        error_event_data,
        failure_kind,
        failure_kind_metadata,
        model_call_failure,
    )

    fixed_now = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    expires_at = datetime(2026, 11, 21, 8, 30, tzinfo=timezone.utc).timestamp()
    expected_until = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    monkeypatch.setattr("ptc_agent.agent.middleware.model_resilience.time.time", lambda: fixed_now)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    google_key = "dalton:quota_exhausted:u1:antigravity-oauth:vendor:google:cred-a"
    store: dict[str, dict] = {
        google_key: {
            "expires_at": expires_at,
            "model": "gemini-4-neon-high-oauth",
        }
    }
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    hydrogen = _make_antigravity_client(
        "gemini-4-hydrogen-high-oauth", user_id="u1", credential_id="cred-a"
    )
    mw = _make_middleware(hydrogen, [])

    async def should_not_dial(req):
        raise AssertionError("Should not dial upstream when vendor is cooled")

    with pytest.raises(Exception) as caught:
        await mw.awrap_model_call(_FakeRequest(hydrogen), should_not_dial)

    exc = caught.value
    oauth_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="Google Antigravity"),
    )
    assert getattr(exc, "status_code", None) == 429
    assert "gemini-4-hydrogen-high-oauth" in str(exc)
    assert "2026-11-21 16:30 HKT" in str(exc)

    assert failure_kind(exc) == {
        "kind": "upstream",
        "status_code": 429,
        "provider_module": None,
    }
    assert failure_kind_metadata(exc) == {"error_kind": "upstream"}
    assert model_call_failure(exc, oauth_cfg) == {
        "error_status_code": 429,
        "error_credential_owned": True,
    }

    frame = error_event_data("thread-1", str(exc), exc=exc, config=oauth_cfg)
    assert frame["error_kind"] == "upstream"
    assert frame["status_code"] == 429
    assert frame["model"] == "gemini-4-hydrogen-high-oauth"
    assert frame["payer"] == "subscription"
    assert frame["account"] == "Google Antigravity"
    assert frame["reason"] == "plan_limit"
    assert frame["hints"] == ["try_another_model"]
    assert frame["plan_limit"] == {
        "plan": "Google Antigravity",
        "window": None,
        "until": expected_until,
    }
    assert "2026-11-21 16:30 HKT" in frame["error"]
    assert len(frame["attempted_models"]) == 1
    assert frame["attempted_models"][0]["model"] == "gemini-4-hydrogen-high-oauth"
    assert frame["attempted_models"][0]["status_code"] == 429
    assert frame["attempted_models"][0]["attempts"] == 0

    # Same-instance local vendor cooldown (after live 429 on Neon) produces the same plan_limit card
    store.clear()
    neon = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u1", credential_id="cred-a"
    )
    mw_local = _make_middleware(neon, [])

    async def neon_429(req):
        raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)

    with pytest.raises(Exception, match="Individual quota reached"):
        await mw_local.awrap_model_call(_FakeRequest(neon), neon_429)

    with pytest.raises(Exception) as local_caught:
        await mw_local.awrap_model_call(_FakeRequest(hydrogen), should_not_dial)
    local_frame = error_event_data(
        "thread-1", str(local_caught.value), exc=local_caught.value, config=oauth_cfg
    )
    assert local_frame["error_kind"] == "upstream"
    assert local_frame["status_code"] == 429
    assert local_frame["reason"] == "plan_limit"
    assert local_frame["hints"] == ["try_another_model"]
    assert local_frame["plan_limit"]["plan"] == "Google Antigravity"
    assert local_frame["plan_limit"]["until"] is not None
    assert local_frame["attempted_models"][0]["attempts"] == 0


@pytest.mark.asyncio
async def test_codex_plan_limit_cooldown_preserves_plan_tier_window_and_reset(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other providers (e.g. Codex `usage_limit_reached`) keep their original plan tier,
    window, and reset timestamp across both in-process and cross-worker Redis cooldowns."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import httpx
    import openai

    from ptc_agent.config.agent import CredentialPayer
    from src.server.services.runs.stream_errors import error_event_data, model_call_failure

    fixed_now = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    reset_epoch = int(datetime(2026, 11, 21, 9, 45, tzinfo=timezone.utc).timestamp())
    expected_until = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).isoformat()
    monkeypatch.setattr("ptc_agent.agent.middleware.model_resilience.time.time", lambda: fixed_now)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    codex_client = _make_antigravity_client(
        "gpt-6.2-sol-oauth",
        user_id="u-codex",
        provider="codex-oauth",
        credential_id="cred-codex-1",
    )
    codex_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="ChatGPT"),
    )

    body = {
        "error": {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "plus",
            "resets_at": reset_epoch,
            "limit_window_minutes": 300,
            "resets_in_seconds": int(reset_epoch - fixed_now),
        }
    }
    req_http = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    codex_err = openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=req_http, json=body),
        body=body,
    )

    mw_a = _make_middleware(codex_client, [])

    async def codex_429(req):
        raise codex_err

    with pytest.raises(openai.RateLimitError):
        await mw_a.awrap_model_call(_FakeRequest(codex_client), codex_429)

    # 1. Same middleware instance (in-process cooldown) preserves plan tier, window, and reset
    async def should_not_dial(req):
        raise AssertionError("Cooled Codex client must not dial upstream")

    with pytest.raises(Exception) as local_exc:
        await mw_a.awrap_model_call(_FakeRequest(codex_client), should_not_dial)

    local_frame = error_event_data(
        "thread-codex", str(local_exc.value), exc=local_exc.value, config=codex_cfg
    )
    assert local_frame["error_kind"] == "upstream"
    assert local_frame["status_code"] == 429
    assert local_frame["reason"] == "plan_limit"
    assert local_frame["hints"] == ["try_another_model"]
    assert local_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }
    assert local_frame["attempted_models"][0]["attempts"] == 0
    assert model_call_failure(local_exc.value, codex_cfg) == {
        "error_status_code": 429,
        "error_credential_owned": True,
    }

    # 2. Fresh worker (cross-worker Redis cooldown) preserves the exact same Codex fields
    mw_b = _make_middleware(codex_client, [])
    with pytest.raises(Exception) as remote_exc:
        await mw_b.awrap_model_call(_FakeRequest(codex_client), should_not_dial)

    remote_frame = error_event_data(
        "thread-codex", str(remote_exc.value), exc=remote_exc.value, config=codex_cfg
    )
    assert remote_frame["error_kind"] == "upstream"
    assert remote_frame["status_code"] == 429
    assert remote_frame["reason"] == "plan_limit"
    assert remote_frame["hints"] == ["try_another_model"]
    assert remote_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }
    assert remote_frame["attempted_models"][0]["attempts"] == 0
    assert model_call_failure(remote_exc.value, codex_cfg) == {
        "error_status_code": 429,
        "error_credential_owned": True,
    }


@pytest.mark.asyncio
async def test_plain_retry_after_429_cooldown_keeps_rate_limited_classification(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary 429 rate limit whose Retry-After exceeds retry budget stays
    classified as `rate_limited` (not `plan_limit`) in both local and Redis cooldowns."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import httpx
    import openai

    from ptc_agent.config.agent import CredentialPayer
    from src.server.services.runs.stream_errors import error_event_data

    fixed_now = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("ptc_agent.agent.middleware.model_resilience.time.time", lambda: fixed_now)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    client = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-rl", credential_id="cred-rl"
    )
    oauth_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="Google Antigravity"),
    )

    body = {"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
    req_http = httpx.Request("POST", "https://example.invalid/v1/generate")
    rate_limit_err = openai.RateLimitError(
        "Error code: 429 - Rate limit exceeded",
        response=httpx.Response(429, request=req_http, json=body, headers={"Retry-After": "120"}),
        body=body,
    )

    mw_a = _make_middleware(client, [])

    async def rate_limited_handler(req):
        raise rate_limit_err

    with pytest.raises(openai.RateLimitError):
        await mw_a.awrap_model_call(_FakeRequest(client), rate_limited_handler)

    async def should_not_dial(req):
        raise AssertionError("Cooled model must not dial upstream")

    # 1. Local cooldown keeps `rate_limited` (no `plan_limit`)
    with pytest.raises(Exception) as local_exc:
        await mw_a.awrap_model_call(_FakeRequest(client), should_not_dial)
    local_frame = error_event_data(
        "thread-rl", str(local_exc.value), exc=local_exc.value, config=oauth_cfg
    )
    assert local_frame["error_kind"] == "upstream"
    assert local_frame["status_code"] == 429
    assert local_frame["reason"] == "rate_limited"
    assert "plan_limit" not in local_frame
    assert local_frame["hints"] == ["rate_limited", "try_another_model"]
    assert local_frame["attempted_models"][0]["attempts"] == 0

    # 2. Cross-worker Redis cooldown also keeps `rate_limited` (no `plan_limit`)
    mw_b = _make_middleware(client, [])
    with pytest.raises(Exception) as remote_exc:
        await mw_b.awrap_model_call(_FakeRequest(client), should_not_dial)
    remote_frame = error_event_data(
        "thread-rl", str(remote_exc.value), exc=remote_exc.value, config=oauth_cfg
    )
    assert remote_frame["error_kind"] == "upstream"
    assert remote_frame["status_code"] == 429
    assert remote_frame["reason"] == "rate_limited"
    assert "plan_limit" not in remote_frame
    assert remote_frame["hints"] == ["rate_limited", "try_another_model"]
    assert remote_frame["attempted_models"][0]["attempts"] == 0


@pytest.mark.asyncio
async def test_codex_relative_resets_in_seconds_freezes_absolute_until_across_time(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex `usage_limit_reached` with relative `resets_in_seconds` converts to an
    absolute UTC reset timestamp once on first receipt; reading 30s later from either
    local memory or Redis must not shift the reset time forward."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import httpx
    import openai

    from ptc_agent.config.agent import CredentialPayer
    from src.llms import provider_errors as pe_mod
    from src.server.services.runs.stream_errors import error_event_data

    t0_dt = datetime(2026, 11, 21, 5, 0, 0, tzinfo=timezone.utc)
    clock = {"dt": t0_dt, "mono": 1000.0}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["dt"] if tz is not None else clock["dt"].replace(tzinfo=None)

    monkeypatch.setattr(pe_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: clock["dt"].timestamp(),
    )
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.monotonic",
        lambda: clock["mono"],
    )
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    codex_client = _make_antigravity_client(
        "gpt-6.2-sol-oauth",
        user_id="u-codex-rel",
        provider="codex-oauth",
        credential_id="cred-codex-rel",
    )
    codex_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="ChatGPT"),
    )

    body = {
        "error": {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "plus",
            "limit_window_minutes": 300,
            "resets_in_seconds": 2625,
        }
    }
    req_http = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    codex_err = openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=req_http, json=body, headers={"Retry-After": "2625"}),
        body=body,
    )

    expected_until = (t0_dt + timedelta(seconds=2625)).isoformat()

    mw_a = _make_middleware(codex_client, [])

    async def codex_429(req):
        raise codex_err

    with pytest.raises(openai.RateLimitError):
        await mw_a.awrap_model_call(_FakeRequest(codex_client), codex_429)

    # Advance wall clock, provider_errors.datetime.now, and monotonic clock by 30 seconds
    clock["dt"] = t0_dt + timedelta(seconds=30)
    clock["mono"] += 30.0

    async def should_not_dial(req):
        raise AssertionError("Cooled Codex client must not dial upstream")

    # 1. Local read 30s later must keep the original frozen reset timestamp (not +30s)
    with pytest.raises(Exception) as local_exc:
        await mw_a.awrap_model_call(_FakeRequest(codex_client), should_not_dial)
    local_frame = error_event_data(
        "thread-codex-rel", str(local_exc.value), exc=local_exc.value, config=codex_cfg
    )
    assert local_frame["reason"] == "plan_limit"
    assert local_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }

    # 2. Fresh worker Redis read 30s later must return the exact same frozen reset timestamp
    mw_b = _make_middleware(codex_client, [])
    with pytest.raises(Exception) as remote_exc:
        await mw_b.awrap_model_call(_FakeRequest(codex_client), should_not_dial)
    remote_frame = error_event_data(
        "thread-codex-rel", str(remote_exc.value), exc=remote_exc.value, config=codex_cfg
    )
    assert remote_frame["reason"] == "plan_limit"
    assert remote_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }


@pytest.mark.asyncio
async def test_gemini_per_minute_quota_phrase_429_stays_rate_limited_on_first_and_cooldown(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 containing both `exceeded your current quota` and a per-minute metric
    (`generate_content_requests_per_minute`) classifies as `rate_limited` on the
    initial call AND throughout local and Redis cooldown (never promoted to `plan_limit`)."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from ptc_agent.config.agent import CredentialPayer
    from src.server.services.runs.stream_errors import error_event_data

    fixed_now = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("ptc_agent.agent.middleware.model_resilience.time.time", lambda: fixed_now)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    client = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-rpm", credential_id="cred-rpm"
    )
    oauth_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="Google Antigravity"),
    )

    rpm_err = _refusal(
        "429 Too Many Requests. You exceeded your current quota. "
        "Quota exceeded for metric: generate_content_requests_per_minute, limit: 15",
        429,
    )
    mw_a = _make_middleware(client, [])

    async def rpm_handler(req):
        raise rpm_err

    # 1. Initial call classifies as `rate_limited`
    with pytest.raises(Exception) as first_exc:
        await mw_a.awrap_model_call(_FakeRequest(client), rpm_handler)
    first_frame = error_event_data(
        "thread-rpm", str(first_exc.value), exc=first_exc.value, config=oauth_cfg
    )
    assert first_frame["reason"] == "rate_limited"
    assert "plan_limit" not in first_frame

    async def should_not_dial(req):
        raise AssertionError("Cooled model must not dial upstream")

    # 2. Local cooldown keeps `rate_limited`
    with pytest.raises(Exception) as local_exc:
        await mw_a.awrap_model_call(_FakeRequest(client), should_not_dial)
    local_frame = error_event_data(
        "thread-rpm", str(local_exc.value), exc=local_exc.value, config=oauth_cfg
    )
    assert local_frame["reason"] == "rate_limited"
    assert "plan_limit" not in local_frame
    assert local_frame["hints"] == ["rate_limited", "try_another_model"]
    assert local_frame["attempted_models"][0]["attempts"] == 0

    # 3. Cross-worker Redis cooldown keeps `rate_limited`
    mw_b = _make_middleware(client, [])
    with pytest.raises(Exception) as remote_exc:
        await mw_b.awrap_model_call(_FakeRequest(client), should_not_dial)
    remote_frame = error_event_data(
        "thread-rpm", str(remote_exc.value), exc=remote_exc.value, config=oauth_cfg
    )
    assert remote_frame["reason"] == "rate_limited"
    assert "plan_limit" not in remote_frame
    assert remote_frame["hints"] == ["rate_limited", "try_another_model"]
    assert remote_frame["attempted_models"][0]["attempts"] == 0


@pytest.mark.asyncio
async def test_pre_dispatch_cooldown_message_respects_recorded_rate_limited_reason(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check_oauth_quota_cooldown` formats its pre-dispatch refusal from the stored
    `refusal.reason`: a `rate_limited` Retry-After cooldown reports rate limiting,
    never '订阅额度已用完'."""
    from datetime import datetime, timezone

    import httpx
    import openai

    from ptc_agent.agent.middleware.model_resilience import check_oauth_quota_cooldown

    fixed_now = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("ptc_agent.agent.middleware.model_resilience.time.time", lambda: fixed_now)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    client = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-predisp", credential_id="cred-predisp"
    )
    body = {"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
    req_http = httpx.Request("POST", "https://example.invalid/v1/generate")
    rl_err = openai.RateLimitError(
        "Error code: 429 - Rate limit exceeded",
        response=httpx.Response(429, request=req_http, json=body, headers={"Retry-After": "120"}),
        body=body,
    )

    mw = _make_middleware(client, [])

    async def rl_handler(req):
        raise rl_err

    with pytest.raises(openai.RateLimitError):
        await mw.awrap_model_call(_FakeRequest(client), rl_handler)

    msg = await check_oauth_quota_cooldown(client)
    assert msg is not None
    assert "请求频率限制" in msg
    assert "订阅额度已用完" not in msg
    assert "2026-11-21 13:02 HKT" in msg


@pytest.mark.asyncio
async def test_codex_resets_in_seconds_only_writes_local_and_redis_cooldown_with_shared_until(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Codex `usage_limit_reached` provides ONLY `resets_in_seconds` (no `resets_at`,
    no `Retry-After`) exceeding retry budget, it writes both local and Redis cooldown records
    with an absolute UTC reset timestamp shared across the initial card (even if Redis set
    takes 2s) and subsequent skipped calls."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import create_autospec

    import httpx
    import openai

    from ptc_agent.config.agent import CredentialPayer
    from src.llms import provider_errors as pe_mod
    from src.server.services.runs.stream_errors import error_event_data
    from src.utils.cache.redis_cache import RedisCacheClient

    t0_dt = datetime(2026, 11, 21, 5, 0, 0, tzinfo=timezone.utc)
    clock = {"dt": t0_dt, "mono": 1000.0}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["dt"] if tz is not None else clock["dt"].replace(tzinfo=None)

    monkeypatch.setattr(pe_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: clock["dt"].timestamp(),
    )
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.monotonic",
        lambda: clock["mono"],
    )
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    ttl_calls: list[tuple[str, int | None]] = []
    cache = create_autospec(RedisCacheClient, instance=True)

    async def slow_set(key: str, value: dict, ttl=None):
        # Simulate a 2-second Redis write to prove Call 1's card still uses T0's frozen reset
        clock["dt"] = clock["dt"] + timedelta(seconds=2)
        clock["mono"] += 2.0
        ttl_calls.append((key, ttl))
        store[key] = value
        return True

    async def fake_get(key: str):
        return store.get(key)

    cache.get.side_effect = fake_get
    cache.set.side_effect = slow_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    codex_client = _make_antigravity_client(
        "gpt-6.2-sol-oauth",
        user_id="u-codex-only",
        provider="codex-oauth",
        credential_id="cred-codex-only",
    )
    codex_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="ChatGPT"),
    )

    # No `resets_at`, no `Retry-After` header — ONLY `resets_in_seconds`
    body = {
        "error": {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "plus",
            "limit_window_minutes": 300,
            "resets_in_seconds": 2625,
        }
    }
    req_http = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    codex_err = openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=req_http, json=body),
        body=body,
    )

    expected_until = (t0_dt + timedelta(seconds=2625)).isoformat()
    expected_key = (
        "dalton:quota_exhausted:u-codex-only:codex-oauth:model:gpt-6.2-sol-oauth:cred-codex-only"
    )

    mw_a = _make_middleware(codex_client, [])
    calls: list[str] = []

    async def codex_429(req):
        calls.append(req.model.model)
        raise codex_err

    # 1. Initial call benches locally & writes Redis, and Call 1's card shares T0's frozen until
    with pytest.raises(openai.RateLimitError) as first_exc:
        await mw_a.awrap_model_call(_FakeRequest(codex_client), codex_429)
    assert calls == ["gpt-6.2-sol-oauth"]
    assert expected_key in store
    assert (expected_key, 2625) in ttl_calls
    first_frame = error_event_data(
        "thread-codex-only", str(first_exc.value), exc=first_exc.value, config=codex_cfg
    )
    assert first_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }

    # 2. Subsequent calls on same instance AND fresh worker skip upstream with identical `until`
    clock["dt"] = t0_dt + timedelta(seconds=30)
    clock["mono"] += 28.0

    async def should_not_dial(req):
        raise AssertionError("Cooled Codex model must not dial upstream")

    with pytest.raises(Exception) as local_exc:
        await mw_a.awrap_model_call(_FakeRequest(codex_client), should_not_dial)
    local_frame = error_event_data(
        "thread-codex-only", str(local_exc.value), exc=local_exc.value, config=codex_cfg
    )
    assert local_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }

    mw_b = _make_middleware(codex_client, [])
    with pytest.raises(Exception) as remote_exc:
        await mw_b.awrap_model_call(_FakeRequest(codex_client), should_not_dial)
    remote_frame = error_event_data(
        "thread-codex-only", str(remote_exc.value), exc=remote_exc.value, config=codex_cfg
    )
    assert remote_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }


@pytest.mark.asyncio
async def test_antigravity_vendor_cooldown_team_sharing_and_custom_same_name_isolation(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under credentials.sharing == 'team', all members share the holder's vendor cooldown;
    user-defined custom models with the same name stay in `:model:` and are unaffected."""
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    from ptc_agent.agent.middleware.model_resilience import quota_redis_key

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: "pm-holder")

    alice_neon_high = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="alice", credential_id="shared-cred"
    )
    bob_carbon_high = _make_antigravity_client(
        "gemini-4-carbon-high-oauth", user_id="bob", credential_id="shared-cred"
    )
    bob_custom_neon_high = _make_antigravity_client(
        "gemini-4-neon-high-oauth",
        user_id="bob",
        credential_id="shared-cred",
        custom_entry={
            "name": "gemini-4-neon-high-oauth",
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-neon-high",
        },
    )
    literal_vendor_name = _make_antigravity_client(
        "google", user_id="bob", credential_id="shared-cred"
    )

    expected_vendor_key = (
        "dalton:quota_exhausted:pm-holder:antigravity-oauth:vendor:google:shared-cred"
    )
    assert quota_redis_key("gemini-4-neon-high-oauth", alice_neon_high) == expected_vendor_key
    assert quota_redis_key("gemini-4-carbon-high-oauth", bob_carbon_high) == expected_vendor_key
    assert (
        quota_redis_key("gemini-4-neon-high-oauth", bob_custom_neon_high)
        == "dalton:quota_exhausted:pm-holder:antigravity-oauth:model:gemini-4-neon-high-oauth:shared-cred"
    )
    assert (
        quota_redis_key("google", literal_vendor_name)
        == "dalton:quota_exhausted:pm-holder:antigravity-oauth:model:google:shared-cred"
    )

    # Alice exhausts Neon High -> shared holder vendor key written
    mw_alice = _make_middleware(alice_neon_high, [])

    async def alice_handler(req):
        raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)

    with pytest.raises(Exception, match="Individual quota reached"):
        await mw_alice.awrap_model_call(_FakeRequest(alice_neon_high), alice_handler)

    # Bob calling built-in Carbon High hits the shared team vendor cooldown (0 upstream requests)
    mw_bob = _make_middleware(bob_carbon_high, [])
    bob_calls: list[str] = []

    async def bob_handler(req):
        bob_calls.append(req.model.model)
        raise AssertionError("Shared team vendor cooldown should prevent dialing upstream")

    with pytest.raises(Exception, match="gemini-4-carbon-high-oauth 的订阅额度已用完"):
        await mw_bob.awrap_model_call(_FakeRequest(bob_carbon_high), bob_handler)
    assert bob_calls == []

    # Bob's custom model with the same name is NOT affected by the built-in vendor cooldown
    mw_custom = _make_middleware(bob_custom_neon_high, [])
    custom_calls: list[str] = []

    async def custom_handler(req):
        custom_calls.append(req.model.model)
        return ModelResponse(result=[AIMessage("custom-ok")])

    res_custom = await mw_custom.awrap_model_call(
        _FakeRequest(bob_custom_neon_high), custom_handler
    )
    assert res_custom.result[0].content == "custom-ok"
    assert custom_calls == ["gemini-4-neon-high-oauth"]

    # Contrast: in personal mode (credentials.sharing != "team"), alice and bob stay isolated
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)
    assert (
        quota_redis_key("gemini-4-neon-high-oauth", alice_neon_high)
        == "dalton:quota_exhausted:alice:antigravity-oauth:vendor:google:shared-cred"
    )
    assert (
        quota_redis_key("gemini-4-carbon-high-oauth", bob_carbon_high)
        == "dalton:quota_exhausted:bob:antigravity-oauth:vendor:google:shared-cred"
    )


@pytest.mark.asyncio
async def test_antigravity_vendor_cooldown_works_locally_when_redis_unavailable(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Redis raises on get/set, local `_quota_exhausted` still shares cooldown
    across same-vendor models via `_quota_scope_key` and keeps other vendors/accounts/customs separate."""
    from unittest.mock import create_autospec

    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    from src.utils.cache.redis_cache import RedisCacheClient

    broken_cache = create_autospec(RedisCacheClient, instance=True)
    broken_cache.get.side_effect = RuntimeError("redis down")
    broken_cache.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: broken_cache)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    neon_high = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-local", credential_id="cred-1"
    )
    hydrogen_high = _make_antigravity_client(
        "gemini-4-hydrogen-high-oauth", user_id="u-local", credential_id="cred-1"
    )
    neon_custom = _make_antigravity_client(
        "gemini-4-neon-medium-oauth",
        user_id="u-local",
        credential_id="cred-1",
        custom_entry={"name": "gemini-4-neon-medium-oauth", "provider": "antigravity-oauth"},
    )
    claude_opus = _make_antigravity_client(
        "claude-opus-5-5-high-oauth", user_id="u-local", credential_id="cred-1"
    )
    neon_other_acct = _make_antigravity_client(
        "gemini-4-neon-medium-oauth", user_id="u-local", credential_id="cred-2"
    )

    mw = _make_middleware(neon_high, [])
    calls: list[str] = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is neon_high:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return ModelResponse(result=[AIMessage("ok")])

    # Even with broken Redis, neon-high benches the `google` vendor locally
    with pytest.raises(Exception, match="Individual quota reached"):
        await mw.awrap_model_call(_FakeRequest(neon_high), handler)
    assert calls == ["gemini-4-neon-high-oauth"]

    # Same-vendor Hydrogen on the same middleware instance is skipped locally (0 upstream calls)
    with pytest.raises(Exception, match="gemini-4-hydrogen-high-oauth 的订阅额度已用完"):
        await mw.awrap_model_call(_FakeRequest(hydrogen_high), handler)
    assert calls == ["gemini-4-neon-high-oauth"]

    # Different vendor, custom same-name model, and different account remain unaffected locally
    assert await mw._aquota_skipped("gemini-4-hydrogen-high-oauth", hydrogen_high) is True
    assert await mw._aquota_skipped("claude-opus-5-5-high-oauth", claude_opus) is False
    assert await mw._aquota_skipped("gemini-4-neon-medium-oauth", neon_custom) is False
    assert await mw._aquota_skipped("gemini-4-neon-medium-oauth", neon_other_acct) is False


@pytest.mark.asyncio
async def test_notify_bench_switch_and_raise_run_exhausted_use_effective_candidate_client(
    no_sleep, events, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_notify_bench_switch` and `_raise_run_exhausted` resolve each candidate's
    effective client (`request.model` when `cand_client is None`, else `cand_client`),
    and `_notify_bench_switch` stops at the first skipped candidate so fallback notices dedupe."""
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    store: dict[str, dict] = {}
    cache = _make_memory_redis_cache(store)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    # Unstamped clients (billing_type=None) so the fallback chain runs for _notify_bench_switch tests.
    mw_primary = _make_antigravity_client(
        "claude-opus-5-5-high-oauth",
        user_id="u1",
        credential_id="cred-main",
        billing_type=None,
    )
    neon_high = _make_antigravity_client(
        "gemini-4-neon-high-oauth",
        user_id="u1",
        credential_id="cred-sub",
        billing_type=None,
    )
    neon_medium = _make_antigravity_client(
        "gemini-4-neon-medium-oauth",
        user_id="u1",
        credential_id="cred-sub",
        billing_type=None,
    )
    spare = _FakeModel("spare-model")

    mw = _make_middleware(mw_primary, [(spare.model, spare)])

    # Bench Google vendor on cred-sub via neon_high
    neon_err = _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
    await mw._aremember_quota_exhausted("gemini-4-neon-high-oauth", 3600.0, neon_err, neon_high)

    # 1. Primary cand_client is None -> must inspect request.model (neon_medium), NOT mw.primary_client!
    calls: list[str] = []

    async def ok_spare_handler(req):
        calls.append(req.model.model)
        return ModelResponse(result=[AIMessage("from-spare")])

    res = await mw.awrap_model_call(_FakeRequest(neon_medium), ok_spare_handler)
    assert res.result[0].content == "from-spare"
    assert calls == ["spare-model"]
    switches = [e["props"] for e in events if e.get("name") == "model_fallback"]
    assert len(switches) == 1
    assert switches[0]["from_model"] == "gemini-4-neon-medium-oauth"
    assert switches[0]["to_model"] == "spare-model"
    assert switches[0]["from_is_primary"] is True
    assert switches[0]["status_code"] == 429

    # 2. Same-vendor fallback right after primary: `_notify_bench_switch` stops at the first
    # skipped candidate (primary) so Call 2 does NOT overwrite from_model or emit a duplicate notice.
    events.clear()
    mw_chain = _make_middleware(
        neon_high,
        [("gemini-4-neon-medium-oauth", neon_medium), ("spare-model", spare)],
    )
    store.clear()

    async def chain_handler(req):
        if req.model is neon_high:
            raise _refusal(ANTIGRAVITY_LONG_RESET_429, 429)
        return ModelResponse(result=[AIMessage("chain-ok")])

    await mw_chain.awrap_model_call(_FakeRequest(neon_high), chain_handler)
    await mw_chain.awrap_model_call(_FakeRequest(neon_high), chain_handler)
    chain_switches = [e["props"] for e in events if e.get("name") == "model_fallback"]
    assert len(chain_switches) == 1
    assert chain_switches[0]["from_model"] == "gemini-4-neon-high-oauth"
    assert chain_switches[0]["to_model"] == "spare-model"
    assert chain_switches[0]["from_is_primary"] is True

    # 3. Non-primary benched fallback in `_notify_bench_switch` must use cand_client (neon_medium),
    # not fall back to mw.primary_client (claude / cred-main).
    events.clear()
    mw._quota_notified.clear()
    other_primary = _FakeModel("other-primary")
    candidates = [
        ("other-primary", None),
        ("gemini-4-neon-medium-oauth", neon_medium),
        ("spare-model", spare),
    ]
    mw._notify_bench_switch(_FakeRequest(other_primary), candidates, index=2)
    fb_switches = [e["props"] for e in events if e.get("name") == "model_fallback"]
    assert len(fb_switches) == 1
    assert fb_switches[0]["from_model"] == "gemini-4-neon-medium-oauth"
    assert fb_switches[0]["to_model"] == "spare-model"
    assert fb_switches[0]["from_is_primary"] is False

    # 4. `_raise_run_exhausted` when primary (cand_client=None -> request.model=neon_medium) is benched
    # and spare fails with 400 -> raises Neon Medium's 429 refusal with attempt trace!
    async def failing_spare_handler(req):
        raise _refusal("Error code: 400 - spare failed", 400)

    with pytest.raises(Exception, match="gemini-4-neon-medium-oauth 的订阅额度已用完") as caught:
        await mw.awrap_model_call(_FakeRequest(neon_medium), failing_spare_handler)
    trace = getattr(caught.value, "__model_resilience__", None)
    assert trace is not None
    assert trace["model"] == "gemini-4-neon-medium-oauth"
    assert trace["attempted_models"][0]["model"] == "gemini-4-neon-medium-oauth"
    assert trace["attempted_models"][0]["attempts"] == 0
    assert trace["attempted_models"][0]["status_code"] == 429
    assert trace["attempted_models"][1]["model"] == "spare-model"

    # 5. `_raise_run_exhausted` when ALL candidates are benched (empty records) -> raises request.model's
    # cooldown refusal with trace even though mw.primary_client has a different refusal in `_quota_exhausted`!
    claude_err = _refusal("Anthropic 429 refusal", 429)
    await mw._aremember_quota_exhausted(
        "claude-opus-5-5-high-oauth", 3600.0, claude_err, mw_primary
    )
    mw_all_benched = _make_middleware(mw_primary, [])
    mw_all_benched._quota_exhausted = dict(mw._quota_exhausted)
    with pytest.raises(Exception, match="gemini-4-neon-medium-oauth 的订阅额度已用完") as all_caught:
        await mw_all_benched.awrap_model_call(_FakeRequest(neon_medium), ok_spare_handler)
    all_trace = getattr(all_caught.value, "__model_resilience__", None)
    assert all_trace is not None
    assert all_trace["model"] == "gemini-4-neon-medium-oauth"
    assert all_trace["attempted_models"][0]["attempts"] == 0
    assert all_trace["attempted_models"][0]["status_code"] == 429


@pytest.mark.asyncio
async def test_401_oauth_rebuild_preserves_original_suffixed_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vendor cooldown never alters OAuthRebuildSpec.model_name; 401 refresh rebuilds the exact tier."""
    from unittest.mock import create_autospec

    from ptc_agent.agent.middleware.model_resilience import quota_redis_key
    from src.dalton.llm import oauth_refresh
    from src.dalton.llm.oauth_refresh import (
        OAuthRebuildSpec,
        oauth_rebuild_spec,
        rebuild_oauth_client,
        tag_oauth_client,
    )
    from src.server.services.llm import clients as llm_clients

    client = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u1", credential_id="cred-1"
    )
    rebuilt = _FakeModel("gemini-4-neon-high-oauth")
    tag_oauth_client(
        rebuilt,
        OAuthRebuildSpec(
            user_id="u1",
            model_name="gemini-4-neon-high-oauth",
            provider="antigravity-oauth",
            token_digest="new-digest",
            credential_id="cred-1",
        ),
    )

    resolve_stub = create_autospec(llm_clients.resolve_oauth_llm_client, return_value=rebuilt)
    monkeypatch.setattr(llm_clients, "resolve_oauth_llm_client", resolve_stub)
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    assert (
        quota_redis_key("gemini-4-neon-high-oauth", client)
        == "dalton:quota_exhausted:u1:antigravity-oauth:vendor:google:cred-1"
    )
    spec = oauth_rebuild_spec(client)
    assert spec is not None and spec.model_name == "gemini-4-neon-high-oauth"

    fresh = await rebuild_oauth_client(client)
    assert fresh is rebuilt
    resolve_stub.assert_awaited_once_with(
        "u1",
        "gemini-4-neon-high-oauth",
        None,
        service_tier=None,
        cache_key=None,
        custom_entry=None,
    )
    assert oauth_refresh.oauth_rebuild_spec(fresh).model_name == "gemini-4-neon-high-oauth"


@pytest.mark.asyncio
async def test_cooldown_write_does_not_shorten_or_downgrade_active_redis_record(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up #3: a late-arriving rate_limited or shorter cooldown response must never
    downgrade or shorten an active plan_limit cooldown in Redis or local memory."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import create_autospec

    import httpx
    import openai

    from src.llms import provider_errors as pe_mod
    from src.utils.cache.redis_cache import RedisCacheClient

    t0_dt = datetime(2026, 11, 21, 5, 0, 0, tzinfo=timezone.utc)
    clock = {"dt": t0_dt, "mono": 5000.0}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["dt"] if tz is not None else clock["dt"].replace(tzinfo=None)

    monkeypatch.setattr(pe_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: clock["dt"].timestamp(),
    )
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.monotonic",
        lambda: clock["mono"],
    )
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    ttl_history: list[tuple[str, int | None, dict]] = []
    cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(key: str):
        val = store.get(key)
        return dict(val) if isinstance(val, dict) else val

    async def fake_set(key: str, value: dict, ttl=None):
        store[key] = dict(value)
        ttl_history.append((key, ttl, dict(value)))
        return True

    cache.get.side_effect = fake_get
    cache.set.side_effect = fake_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    client_a = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-race", credential_id="cred-race"
    )
    client_b = _make_antigravity_client(
        "gemini-4-neon-medium-oauth", user_id="u-race", credential_id="cred-race"
    )
    expected_key = "dalton:quota_exhausted:u-race:antigravity-oauth:vendor:google:cred-race"

    mw_a = _make_middleware(client_a, [])
    mw_b = _make_middleware(client_b, [])

    # 1) Worker A records an active 1-hour plan_limit cooldown.
    plan_limit_err = _refusal("Individual quota reached. Resets in 1h0m0s.", 429)
    await mw_a._aremember_quota_exhausted(
        "gemini-4-neon-high-oauth", 3600.0, plan_limit_err, client_a, now_epoch=clock["dt"].timestamp()
    )
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    initial_exp = store[expected_key]["expires_at"]
    assert initial_exp == pytest.approx(clock["dt"].timestamp() + 3600.0)
    writes_after_first = len(ttl_history)

    # 2) Worker B's late-arriving 429 rate_limited (5m) must NOT overwrite or downgrade Redis.
    req_http = httpx.Request("POST", "https://example.invalid/v1/models")
    rate_limit_5m = openai.RateLimitError(
        "Error code: 429 - rate limit exceeded",
        response=httpx.Response(429, request=req_http, headers={"Retry-After": "300"}),
        body={"error": {"code": "rate_limit_exceeded", "message": "rate limit exceeded"}},
    )
    await mw_b._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 300.0, rate_limit_5m, client_b, now_epoch=clock["dt"].timestamp()
    )
    assert len(ttl_history) == writes_after_first
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    assert store[expected_key]["expires_at"] == pytest.approx(initial_exp)

    # 3) Shorter plan_limit (10m) must NOT shorten the active 1-hour plan_limit.
    plan_limit_10m = _refusal("Individual quota reached. Resets in 10m0s.", 429)
    await mw_b._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 600.0, plan_limit_10m, client_b, now_epoch=clock["dt"].timestamp()
    )
    assert len(ttl_history) == writes_after_first
    assert store[expected_key]["expires_at"] == pytest.approx(initial_exp)

    # 4) Longer rate_limited (2h) preserves plan_limit classification while extending expires_at.
    rate_limit_2h = openai.RateLimitError(
        "Error code: 429 - rate limit exceeded",
        response=httpx.Response(429, request=req_http, headers={"Retry-After": "7200"}),
        body={"error": {"code": "rate_limit_exceeded", "message": "rate limit exceeded"}},
    )
    await mw_b._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 7200.0, rate_limit_2h, client_b, now_epoch=clock["dt"].timestamp()
    )
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    assert store[expected_key]["expires_at"] == pytest.approx(clock["dt"].timestamp() + 7200.0)

    # 5) Upgrading active rate_limited (30m) with shorter plan_limit (10m) upgrades reason without shortening.
    store.clear()
    await mw_b._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 1800.0, rate_limit_5m, client_b, now_epoch=clock["dt"].timestamp()
    )
    assert store[expected_key]["refusal"]["reason"] == "rate_limited"
    await mw_a._aremember_quota_exhausted(
        "gemini-4-neon-high-oauth", 600.0, plan_limit_10m, client_a, now_epoch=clock["dt"].timestamp()
    )
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    assert store[expected_key]["expires_at"] == pytest.approx(clock["dt"].timestamp() + 1800.0)

    # 6) Once expired, a fresh record replaces it normally.
    clock["dt"] = t0_dt + timedelta(hours=3)
    clock["mono"] += 10800.0
    await mw_b._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 300.0, rate_limit_5m, client_b, now_epoch=clock["dt"].timestamp()
    )
    assert store[expected_key]["refusal"]["reason"] == "rate_limited"
    assert store[expected_key]["expires_at"] == pytest.approx(clock["dt"].timestamp() + 300.0)


@pytest.mark.asyncio
async def test_cooldown_extension_syncs_refusal_until_across_redis_and_local(
    no_sleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extending cooldown expires_at during Redis/local merge must sync refusal.until
    so check_oauth_quota_cooldown, _build_benched_refusal, and plan_limit_detail all
    report the effective cooldown deadline."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import create_autospec

    import httpx
    import openai

    from ptc_agent.agent.middleware.model_resilience import (
        _UPSERT_QUOTA_COOLDOWN_LUA,
        check_oauth_quota_cooldown,
        format_quota_cooldown_message,
    )
    from ptc_agent.config.agent import CredentialPayer
    from src.llms import provider_errors as pe_mod
    from src.server.services.runs.stream_errors import error_event_data, plan_limit_detail
    from src.utils.cache.redis_cache import RedisCacheClient

    assert "sync_refusal_until" in _UPSERT_QUOTA_COOLDOWN_LUA
    assert "epoch_to_iso" in _UPSERT_QUOTA_COOLDOWN_LUA

    t0_dt = datetime(2026, 11, 21, 5, 0, 0, tzinfo=timezone.utc)
    clock = {"dt": t0_dt, "mono": 8000.0}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["dt"] if tz is not None else clock["dt"].replace(tzinfo=None)

    monkeypatch.setattr(pe_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: clock["dt"].timestamp(),
    )
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.monotonic",
        lambda: clock["mono"],
    )
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(key: str):
        val = store.get(key)
        return dict(val) if isinstance(val, dict) else val

    async def fake_set(key: str, value: dict, ttl=None):
        store[key] = dict(value)
        return True

    cache.get.side_effect = fake_get
    cache.set.side_effect = fake_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    client_a = _make_antigravity_client(
        "gemini-4-neon-high-oauth", user_id="u-sync", credential_id="cred-sync"
    )
    client_b = _make_antigravity_client(
        "gemini-4-neon-medium-oauth", user_id="u-sync", credential_id="cred-sync"
    )
    ag_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="Antigravity"),
    )
    expected_key = "dalton:quota_exhausted:u-sync:antigravity-oauth:vendor:google:cred-sync"
    req_http = httpx.Request("POST", "https://example.invalid/v1/models")

    async def must_not_call(req):
        raise AssertionError("Should skip upstream while cooled down")

    # Scenario 1: 30m rate_limited followed by 10m plan_limit -> plan_limit kept at 30m deadline.
    mw_local_1 = _make_middleware(client_a, [])
    rate_limit_30m = openai.RateLimitError(
        "Error code: 429 - rate limit exceeded",
        response=httpx.Response(429, request=req_http, headers={"Retry-After": "1800"}),
        body={"error": {"code": "rate_limit_exceeded", "message": "rate limit exceeded"}},
    )
    plan_limit_10m = _refusal("Individual quota reached. Resets in 10m0s.", 429)

    await mw_local_1._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 1800.0, rate_limit_30m, client_b, now_epoch=clock["dt"].timestamp()
    )
    await mw_local_1._aremember_quota_exhausted(
        "gemini-4-neon-high-oauth", 600.0, plan_limit_10m, client_a, now_epoch=clock["dt"].timestamp()
    )

    exp_30m = t0_dt + timedelta(seconds=1800)
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    assert store[expected_key]["expires_at"] == pytest.approx(exp_30m.timestamp())
    assert store[expected_key]["refusal"]["until"] == exp_30m.isoformat()

    pre_msg_1 = await check_oauth_quota_cooldown(client_a)
    assert pre_msg_1 == format_quota_cooldown_message("gemini-4-neon-high-oauth", exp_30m.timestamp())

    scope_key = mw_local_1._quota_scope_key("gemini-4-neon-high-oauth", client_a)
    local_exc_1, _ = mw_local_1._build_benched_refusal(
        "gemini-4-neon-high-oauth", mw_local_1._quota_exhausted[scope_key]
    )
    assert str(local_exc_1) == format_quota_cooldown_message(
        "gemini-4-neon-high-oauth", exp_30m.timestamp()
    )
    assert plan_limit_detail(pe_mod.read_refusal(local_exc_1), ag_cfg.credential_payer)[
        "until"
    ] == exp_30m.isoformat()
    with pytest.raises(Exception) as local_call_exc_1:
        await mw_local_1.awrap_model_call(_FakeRequest(client_a), must_not_call)
    assert error_event_data(
        "th-1", str(local_call_exc_1.value), exc=local_call_exc_1.value, config=ag_cfg
    )["plan_limit"]["until"] == exp_30m.isoformat()

    mw_fresh_1 = _make_middleware(client_a, [])
    with pytest.raises(Exception) as redis_exc_1:
        await mw_fresh_1.awrap_model_call(_FakeRequest(client_a), must_not_call)
    assert plan_limit_detail(pe_mod.read_refusal(redis_exc_1.value), ag_cfg.credential_payer)[
        "until"
    ] == exp_30m.isoformat()
    assert error_event_data(
        "th-1", str(redis_exc_1.value), exc=redis_exc_1.value, config=ag_cfg
    )["plan_limit"]["until"] == exp_30m.isoformat()

    # Scenario 2: 10m plan_limit followed by 2h rate_limited -> plan_limit extended to 2h deadline.
    store.clear()
    mw_local_2 = _make_middleware(client_a, [])
    rate_limit_2h = openai.RateLimitError(
        "Error code: 429 - rate limit exceeded",
        response=httpx.Response(429, request=req_http, headers={"Retry-After": "7200"}),
        body={"error": {"code": "rate_limit_exceeded", "message": "rate limit exceeded"}},
    )

    await mw_local_2._aremember_quota_exhausted(
        "gemini-4-neon-high-oauth", 600.0, plan_limit_10m, client_a, now_epoch=clock["dt"].timestamp()
    )
    await mw_local_2._aremember_quota_exhausted(
        "gemini-4-neon-medium-oauth", 7200.0, rate_limit_2h, client_b, now_epoch=clock["dt"].timestamp()
    )

    exp_2h = t0_dt + timedelta(seconds=7200)
    assert store[expected_key]["refusal"]["reason"] == "plan_limit"
    assert store[expected_key]["expires_at"] == pytest.approx(exp_2h.timestamp())
    assert store[expected_key]["refusal"]["until"] == exp_2h.isoformat()

    pre_msg_2 = await check_oauth_quota_cooldown(client_a)
    assert pre_msg_2 == format_quota_cooldown_message("gemini-4-neon-high-oauth", exp_2h.timestamp())

    local_exc_2, _ = mw_local_2._build_benched_refusal(
        "gemini-4-neon-high-oauth", mw_local_2._quota_exhausted[scope_key]
    )
    assert str(local_exc_2) == format_quota_cooldown_message(
        "gemini-4-neon-high-oauth", exp_2h.timestamp()
    )
    assert plan_limit_detail(pe_mod.read_refusal(local_exc_2), ag_cfg.credential_payer)[
        "until"
    ] == exp_2h.isoformat()
    with pytest.raises(Exception) as local_call_exc_2:
        await mw_local_2.awrap_model_call(_FakeRequest(client_a), must_not_call)
    assert error_event_data(
        "th-2", str(local_call_exc_2.value), exc=local_call_exc_2.value, config=ag_cfg
    )["plan_limit"]["until"] == exp_2h.isoformat()

    mw_fresh_2 = _make_middleware(client_a, [])
    with pytest.raises(Exception) as redis_exc_2:
        await mw_fresh_2.awrap_model_call(_FakeRequest(client_a), must_not_call)
    assert plan_limit_detail(pe_mod.read_refusal(redis_exc_2.value), ag_cfg.credential_payer)[
        "until"
    ] == exp_2h.isoformat()
    assert error_event_data(
        "th-2", str(redis_exc_2.value), exc=redis_exc_2.value, config=ag_cfg
    )["plan_limit"]["until"] == exp_2h.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resets_in_seconds", "retry_after", "expected_seconds"),
    [
        (3600, "60", 3600),
        (60, "3600", 3600),
    ],
)
async def test_codex_resets_in_seconds_and_retry_after_disagreement_uses_later_for_card_and_cooldown(
    resets_in_seconds: int,
    retry_after: str,
    expected_seconds: int,
    no_sleep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow-up #4: when body resets_in_seconds and Retry-After header disagree, both the
    error card and the local/Redis cooldown record use the later of the two."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import create_autospec

    import httpx
    import openai

    from ptc_agent.config.agent import CredentialPayer
    from src.llms import provider_errors as pe_mod
    from src.server.services.runs.stream_errors import error_event_data
    from src.utils.cache.redis_cache import RedisCacheClient

    t0_dt = datetime(2026, 11, 21, 5, 0, 0, tzinfo=timezone.utc)
    clock = {"dt": t0_dt, "mono": 2000.0}

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["dt"] if tz is not None else clock["dt"].replace(tzinfo=None)

    monkeypatch.setattr(pe_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.time",
        lambda: clock["dt"].timestamp(),
    )
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.model_resilience.time.monotonic",
        lambda: clock["mono"],
    )
    monkeypatch.setattr("src.dalton.credentials.get_team_credential_holder_id", lambda: None)

    store: dict[str, dict] = {}
    ttl_calls: list[tuple[str, int | None]] = []
    cache = create_autospec(RedisCacheClient, instance=True)

    async def fake_get(key: str):
        return store.get(key)

    async def fake_set(key: str, value: dict, ttl=None):
        ttl_calls.append((key, ttl))
        store[key] = value
        return True

    cache.get.side_effect = fake_get
    cache.set.side_effect = fake_set
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: cache)

    codex_client = _make_antigravity_client(
        "gpt-6.2-sol-oauth",
        user_id="u-codex-mismatch",
        provider="codex-oauth",
        credential_id="cred-codex-mismatch",
    )
    codex_cfg = SimpleNamespace(
        credential_source="oauth",
        credential_payer=CredentialPayer(payer="subscription", account="ChatGPT"),
    )
    body = {
        "error": {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "plan_type": "plus",
            "limit_window_minutes": 300,
            "resets_in_seconds": resets_in_seconds,
        }
    }
    req_http = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    codex_err = openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=req_http, json=body, headers={"Retry-After": retry_after}),
        body=body,
    )

    expected_dt = t0_dt + timedelta(seconds=expected_seconds)
    expected_until = expected_dt.isoformat()
    expected_key = (
        "dalton:quota_exhausted:u-codex-mismatch:codex-oauth:model:gpt-6.2-sol-oauth:cred-codex-mismatch"
    )

    mw = _make_middleware(codex_client, [])

    async def fail_429(req):
        raise codex_err

    with pytest.raises(openai.RateLimitError) as first_exc:
        await mw.awrap_model_call(_FakeRequest(codex_client), fail_429)

    # 1) First error card uses the later reset timestamp.
    first_frame = error_event_data(
        "thread-codex-mismatch", str(first_exc.value), exc=first_exc.value, config=codex_cfg
    )
    assert first_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }
    # 2) Redis cooldown record uses the exact same later timestamp & TTL.
    assert (expected_key, expected_seconds) in ttl_calls
    assert store[expected_key]["expires_at"] == pytest.approx(expected_dt.timestamp())
    assert store[expected_key]["refusal"]["until"] == expected_until

    # 3) Subsequent benched call on a fresh worker uses the same later reset timestamp.
    clock["dt"] = t0_dt + timedelta(seconds=120)
    clock["mono"] += 120.0
    mw_fresh = _make_middleware(codex_client, [])

    async def must_not_call(req):
        raise AssertionError("Should skip upstream while cooled down")

    with pytest.raises(Exception) as benched_exc:
        await mw_fresh.awrap_model_call(_FakeRequest(codex_client), must_not_call)
    benched_frame = error_event_data(
        "thread-codex-mismatch", str(benched_exc.value), exc=benched_exc.value, config=codex_cfg
    )
    assert benched_frame["plan_limit"] == {
        "plan": "ChatGPT Plus",
        "window": "five_hour",
        "until": expected_until,
    }
