"""A 401 mid-turn rebuilds the OAuth client with a fresh token and retries the same model."""

from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.middleware.model_resilience import ModelResilienceMiddleware
from src.dalton.llm import oauth_refresh
from src.dalton.llm.oauth_refresh import (
    OAuthRebuildSpec,
    oauth_rebuild_spec,
    tag_oauth_client,
    token_digest,
)
from src.server.services.llm import clients as llm_clients


class _FakeModel:
    def __init__(self, name, token="t0"):
        self.model = name
        self.token = token


class _FakeRequest:
    # DALTON-HOOK(oauth-dispatch): B4's resilience chain reads model_settings
    # (compaction-request detection); a plain send carries none.
    def __init__(self, model, messages=None, state=None):
        self.model = model
        self.messages = messages if messages is not None else []
        self.state = state if state is not None else {"messages": []}
        self.model_settings = {}

    def override(self, *, model=None, messages=None):
        return _FakeRequest(
            model if model is not None else self.model,
            messages if messages is not None else self.messages,
            self.state,
        )


def _expired():
    exc = Exception(
        "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', "
        "'message': 'OAuth access token has expired. Re-authenticate to continue.'}}"
    )
    exc.status_code = 401
    return exc


def _oauth_model(name, token="t0", user="u1"):
    return tag_oauth_client(
        _FakeModel(name, token),
        OAuthRebuildSpec(
            user_id=user, model_name=name, provider="claude-oauth", token_digest=token_digest(token)
        ),
    )


@pytest.fixture
def events(monkeypatch):
    from langgraph._internal._constants import CONFIG_KEY_SEND
    from langgraph.constants import CONF

    captured = []
    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: captured.append)
    monkeypatch.setattr("langgraph.graph.ui.get_stream_writer", lambda: captured.append)
    monkeypatch.setattr(
        "langgraph.graph.ui.get_config",
        lambda: {CONF: {CONFIG_KEY_SEND: lambda writes: None}},
    )
    return captured


@pytest.fixture
def resolver(monkeypatch):
    """Stand-in for resolve_oauth_llm_client: each call returns a model with a newer token."""
    counter = {"n": 0}

    async def _resolve(user_id, model_name, reasoning_effort=None, service_tier=None, cache_key=None):
        counter["n"] += 1
        return _oauth_model(model_name, token=f"t{counter['n']}", user=user_id)

    fake = create_autospec(llm_clients.resolve_oauth_llm_client, side_effect=_resolve)
    monkeypatch.setattr(llm_clients, "resolve_oauth_llm_client", fake)
    return fake


def _mw(primary, fallbacks=()):
    return ModelResilienceMiddleware(
        primary_name="claude-opus-5-5-oauth",
        primary_client=primary,
        fallbacks=list(fallbacks),
        initial_delay=0.0,
        jitter=False,
    )


def _kinds(events):
    return [e.get("type") or e.get("name") for e in events]


@pytest.mark.asyncio
async def test_401_refreshes_and_retries_same_model_without_fallback(events, resolver):
    primary = _oauth_model("claude-opus-5-5-oauth", "t0")
    fallback = _FakeModel("gemini-4-argon-high-oauth")
    mw = _mw(primary, [("gemini-4-argon-high-oauth", fallback)])
    seen = []

    async def handler(req):
        seen.append((req.model.model, req.model.token))
        if req.model.token == "t0":
            raise _expired()
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert seen == [("claude-opus-5-5-oauth", "t0"), ("claude-opus-5-5-oauth", "t1")]
    assert "model_fallback" not in str(events)
    assert [e["type"] for e in events if e.get("type") == "model_retry"] == ["model_retry"]
    resolver.assert_awaited_once()

    # Later calls in the same turn go straight to the refreshed client.
    seen.clear()
    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert seen == [("claude-opus-5-5-oauth", "t1")]
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_still_401_after_refresh_falls_back(events, resolver):
    primary = _oauth_model("claude-opus-5-5-oauth", "t0")
    fallback = _FakeModel("muse-wonder-1.0-oauth")
    mw = _mw(primary, [("muse-wonder-1.0-oauth", fallback)])
    seen = []

    async def handler(req):
        seen.append(req.model.model)
        if req.model.model.startswith("claude"):
            raise _expired()
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    # One refresh only: original + refreshed attempt on claude, then the fallback.
    assert seen == ["claude-opus-5-5-oauth", "claude-opus-5-5-oauth", "muse-wonder-1.0-oauth"]
    assert resolver.await_count == 1
    assert "model_fallback" in str(events)


@pytest.mark.asyncio
async def test_non_oauth_client_falls_back_as_before(events, resolver):
    primary = _FakeModel("deepseek-flash")  # untagged: API-key client
    fallback = _FakeModel("muse-wonder-1.0-oauth")
    mw = _mw(primary, [("muse-wonder-1.0-oauth", fallback)])
    seen = []

    async def handler(req):
        seen.append(req.model.model)
        if req.model is primary:
            raise _expired()
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert seen == ["deepseek-flash", "muse-wonder-1.0-oauth"]
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_candidate_is_refreshed_too(events, resolver):
    primary = _FakeModel("deepseek-flash")
    fallback = _oauth_model("gemini-4-argon-high-oauth", "t0")
    mw = _mw(primary, [("gemini-4-argon-high-oauth", fallback)])
    seen = []

    async def handler(req):
        seen.append((req.model.model, req.model.token))
        if req.model is primary or req.model.token == "t0":
            raise _expired() if req.model is not primary else Exception("boom 400")
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert seen[-2:] == [("gemini-4-argon-high-oauth", "t0"), ("gemini-4-argon-high-oauth", "t1")]


@pytest.mark.asyncio
async def test_subagent_client_is_refreshed(events, resolver):
    primary = _oauth_model("claude-opus-5-5-oauth", "t0")
    sub = _oauth_model("grok-4.7-oauth", "t0")
    mw = _mw(primary)
    seen = []

    async def handler(req):
        seen.append((req.model.model, req.model.token))
        if req.model.token == "t0":
            raise _expired()
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(sub), handler) == "ok"
    assert seen == [("grok-4.7-oauth", "t0"), ("grok-4.7-oauth", "t1")]


@pytest.mark.asyncio
async def test_rebuild_failure_returns_none(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("no credential")

    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(llm_clients.resolve_oauth_llm_client, side_effect=_boom),
    )
    assert await oauth_refresh.rebuild_oauth_client(_oauth_model("claude-opus-5-5-oauth")) is None
    assert await oauth_refresh.rebuild_oauth_client(_FakeModel("x")) is None


def test_real_clients_carry_spec_without_serializing_it():
    from src.llms.llm import create_llm

    client = create_llm("claude-opus-5-5-oauth", api_key="dummy")
    spec = OAuthRebuildSpec(user_id="u1", model_name="claude-opus-5-5-oauth")
    tag_oauth_client(client, spec)
    assert oauth_rebuild_spec(client) == spec
    assert "u1" not in str(client.model_dump())


@pytest.mark.asyncio
async def test_same_token_back_forces_expiry_then_uses_refreshed(monkeypatch):
    """The store still calls the refused token valid: expire it, resolve again."""
    tokens = iter(["t0", "t1"])

    async def _resolve(user_id, model_name, reasoning_effort=None, service_tier=None, cache_key=None):
        return _oauth_model(model_name, token=next(tokens), user=user_id)

    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(llm_clients.resolve_oauth_llm_client, side_effect=_resolve),
    )
    expire = create_autospec(oauth_refresh._expire_stored_token)
    monkeypatch.setattr(oauth_refresh, "_expire_stored_token", expire)

    fresh = await oauth_refresh.rebuild_oauth_client(_oauth_model("claude-opus-5-5-oauth", "t0"))
    assert fresh.token == "t1"
    expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_token_even_after_expiry_gives_up(monkeypatch):
    async def _resolve(user_id, model_name, reasoning_effort=None, service_tier=None, cache_key=None):
        return _oauth_model(model_name, token="t0", user=user_id)

    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(llm_clients.resolve_oauth_llm_client, side_effect=_resolve),
    )
    monkeypatch.setattr(
        oauth_refresh, "_expire_stored_token", create_autospec(oauth_refresh._expire_stored_token)
    )
    assert await oauth_refresh.rebuild_oauth_client(_oauth_model("claude-opus-5-5-oauth", "t0")) is None


@pytest.mark.asyncio
async def test_resolver_tags_clients_without_the_token(monkeypatch):
    """The production resolver attaches the spec: provider and a digest, never the token."""
    from src.server.services import claude_oauth

    async def _token(user_id):
        return {"access_token": "secret-access-token", "plan_type": None}

    monkeypatch.setattr(claude_oauth, "get_valid_token", create_autospec(claude_oauth.get_valid_token, side_effect=_token))
    client = await llm_clients.resolve_oauth_llm_client("u1", "claude-opus-5-5-oauth")
    spec = oauth_rebuild_spec(client)
    assert spec.provider == "claude-oauth" and spec.model_name == "claude-opus-5-5-oauth"
    assert spec.token_digest == token_digest("secret-access-token")
    assert "secret-access-token" not in repr(spec)


@pytest.mark.asyncio
async def test_refresh_does_not_eat_the_stalled_stream_retry(events, resolver):
    """401 -> refresh -> stall still gets its one stall retry on the refreshed client."""
    primary = _oauth_model("claude-opus-5-5-oauth", "t0")
    mw = _mw(primary)
    calls = []

    async def handler(req):
        calls.append(req.model.token)
        if len(calls) == 1:
            raise _expired()
        if len(calls) == 2:
            raise _stall()
        return "ok"

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    assert calls == ["t0", "t1", "t1"]


def _stall():
    exc = TimeoutError("no chunk within the bound")
    exc.chunks_received = 0  # what is_stalled_stream keys on
    return exc


@pytest.mark.asyncio
async def test_second_stall_after_refresh_ends_the_candidate(events, resolver):
    primary = _oauth_model("claude-opus-5-5-oauth", "t0")
    fallback = _FakeModel("muse-wonder-1.0-oauth")
    mw = _mw(primary, [("muse-wonder-1.0-oauth", fallback)])
    calls = []

    async def handler(req):
        calls.append(req.model.model)
        if req.model is fallback:
            return "ok"
        if len(calls) == 1:
            raise _expired()
        raise _stall()

    assert await mw.awrap_model_call(_FakeRequest(primary), handler) == "ok"
    # refused token, refreshed stall, one stall retry, then the fallback
    assert calls == ["claude-opus-5-5-oauth"] * 3 + ["muse-wonder-1.0-oauth"]


@pytest.mark.asyncio
async def test_subagent_narrowed_cache_key_survives_rebuild(monkeypatch):
    """A narrowed subagent copy must not rejoin the parent session after a rebuild."""
    from src.llms.llm import create_llm, narrow_prompt_cache_key

    def _build(token):
        c = create_llm("grok-4.7-oauth", api_key=token, cache_key="thread-1")
        return tag_oauth_client(
            c,
            OAuthRebuildSpec(
                user_id="u1",
                model_name="grok-4.7-oauth",
                provider="xai-oauth",
                token_digest=token_digest(token),
                cache_key="thread-1",
            ),
        )

    async def _resolve(user_id, model_name, reasoning_effort=None, service_tier=None, cache_key=None):
        return _build("t1")

    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(llm_clients.resolve_oauth_llm_client, side_effect=_resolve),
    )
    sub = narrow_prompt_cache_key(_build("t0"), "research")
    assert sub.model_kwargs["prompt_cache_key"] == "thread-1:research"
    fresh = await oauth_refresh.rebuild_oauth_client(sub)
    assert fresh.model_kwargs["prompt_cache_key"] == "thread-1:research"
