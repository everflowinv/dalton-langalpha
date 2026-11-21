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
    assert "dalton:quota_exhausted:mark-xie:antigravity:gemini-4-neon-high" in store
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
