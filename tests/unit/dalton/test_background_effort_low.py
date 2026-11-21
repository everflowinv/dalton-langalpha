"""Background-slot calls that name no reasoning level run at low (user decision 2026-11-21)."""

from unittest.mock import create_autospec, patch

import pytest

from src.server.services.llm import config as cfg_mod
from tests.unit.server.services.llm.test_claude_code_routing import _resolve


async def _effort_passed(slot: str, pref: dict | None = None, **turn) -> str | None:
    real = cfg_mod.resolve_clients
    spy = create_autospec(real, side_effect=real)
    with patch.object(cfg_mod, "resolve_clients", spy):
        await _resolve(pref or {}, slot=slot, **turn)
    assert spy.await_count == 1
    return spy.await_args.kwargs["reasoning_effort"]


@pytest.mark.asyncio
async def test_background_slot_defaults_to_low():
    assert await _effort_passed("background") == "low"


@pytest.mark.asyncio
async def test_background_slot_keeps_an_explicit_level():
    assert await _effort_passed("background", reasoning_effort="high") == "high"


@pytest.mark.asyncio
async def test_background_call_that_names_its_model_is_unchanged():
    """Dalton assist calls (number audit, language review, dedup) name their model."""
    assert await _effort_passed("background", request_model="claude-sonnet-5-5") is None


@pytest.mark.asyncio
async def test_primary_slot_is_unchanged():
    assert await _effort_passed("primary") is None


@pytest.mark.asyncio
async def test_a_level_the_user_set_for_the_model_wins():
    pref = {"profiles": {"claude-haiku-5-5": {"reasoning_effort": "high"}}}
    assert await _effort_passed("background", pref) is None


def test_models_without_a_reasoning_ladder_are_untouched():
    assert cfg_mod.background_default_effort({}, "no-such-model") is None
    assert cfg_mod.background_default_effort({}, None) is None
    assert cfg_mod.background_default_effort({}, "claude-haiku-5-5") in ("low", None)


def _agent_config(*, name: str = "claude-sonnet-5-5", background: str | None = "claude-haiku-5-5"):
    from ptc_agent.config.agent import AgentConfig, LLMConfig
    from ptc_agent.config.core import (
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )

    return AgentConfig(
        llm=LLMConfig(name=name, background=background),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )


def _client_effort(client) -> str | None:
    if getattr(client, "output_config", None):
        return client.output_config.get("effort")
    if getattr(client, "reasoning", None):
        return client.reasoning.get("effort")
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("background_model", "env_key"),
    [
        ("claude-haiku-5-5", "ANTHROPIC_API_KEY"),
        ("gpt-6.2-sol", "OPENAI_API_KEY"),
    ],
)
async def test_llm_service_system_background_slot_builds_low_effort_client(
    monkeypatch: pytest.MonkeyPatch, background_model: str, env_key: str
):
    from src.llms import api_call as api_call_mod
    from src.server.services.llm import service as service_mod

    monkeypatch.setenv(env_key, "sk-test-system")
    captured: list = []

    async def _fake_api_call(llm, *args, **kwargs):
        captured.append(llm)
        return "ok"

    spy = create_autospec(api_call_mod.make_api_call, side_effect=_fake_api_call)
    monkeypatch.setattr(service_mod, "make_api_call", spy)

    svc = service_mod.LLMService(agent_config=_agent_config(background=background_model))
    assert await svc.complete(user_id=None, user_prompt="extract") == "ok"
    assert len(captured) == 1
    client = captured[0]
    assert _client_effort(client) == "low"
    assert client.metadata.get("reasoning_effort") == "low"


@pytest.mark.asyncio
async def test_llm_service_system_path_preserves_explicit_effort_model_and_primary_slot(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.llms import api_call as api_call_mod
    from src.server.services.llm import service as service_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-system")
    captured: list = []

    async def _fake_api_call(llm, *args, **kwargs):
        captured.append(llm)
        return "ok"

    spy = create_autospec(api_call_mod.make_api_call, side_effect=_fake_api_call)
    monkeypatch.setattr(service_mod, "make_api_call", spy)

    svc = service_mod.LLMService(agent_config=_agent_config(background="claude-haiku-5-5"))

    # 1) Explicit reasoning_effort is preserved.
    await svc.complete(user_id=None, user_prompt="extract", reasoning_effort="high")
    assert _client_effort(captured[-1]) == "high"
    assert captured[-1].metadata.get("reasoning_effort") == "high"

    # 2) Explicit request_model keeps model default (medium), not low.
    await svc.complete(user_id=None, user_prompt="extract", request_model="claude-sonnet-5-5")
    assert _client_effort(captured[-1]) == "medium"
    assert captured[-1].metadata.get("reasoning_effort") == "medium"

    # 3) Primary slot keeps model default (medium), not low.
    await svc.complete(user_id=None, user_prompt="extract", slot="primary")
    assert _client_effort(captured[-1]) == "medium"
    assert captured[-1].metadata.get("reasoning_effort") == "medium"


@pytest.mark.asyncio
async def test_llm_service_system_background_slot_leaves_ladderless_model_untouched(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.llms import api_call as api_call_mod
    from src.server.services.llm import service as service_mod

    monkeypatch.setenv("XAI_API_KEY", "sk-test-xai")
    captured: list = []

    async def _fake_api_call(llm, *args, **kwargs):
        captured.append(llm)
        return "ok"

    spy = create_autospec(api_call_mod.make_api_call, side_effect=_fake_api_call)
    monkeypatch.setattr(service_mod, "make_api_call", spy)

    svc = service_mod.LLMService(agent_config=_agent_config(background="grok-build"))
    await svc.complete(user_id=None, user_prompt="extract")
    assert len(captured) == 1
    assert captured[0].metadata.get("reasoning_effort") is None
