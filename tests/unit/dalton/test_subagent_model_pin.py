"""A subagent that pins a model the turn cannot serve is demoted to Inherit, never silently run on a copy of the main client."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.subagents.definition import SubagentDefinition
from ptc_agent.agent.subagents.merge import ResolvedSubagents
from ptc_agent.config.agent import CredentialSource, SubagentsConfig
from src.server.services.llm import clients
from src.server.services.llm.clients import ResolvedClient
from src.server.services.llm.subagents import demote_clientless, subagent_roles
from src.server.services.llm.user_models import ModelSource

MAIN = "main-model"
PINNED = "sub-model"


class _Client:
    def __init__(self, model: str, effort: str | None = None) -> None:
        self.model = model
        self.effort = effort

    def model_copy(self) -> "_Client":
        return _Client(self.model, self.effort)


def _roster(**models_and_efforts: tuple[str | None, str | None]) -> ResolvedSubagents:
    defns = {
        name: SubagentDefinition(name=name, description="", model=model, reasoning_effort=effort)
        for name, (model, effort) in models_and_efforts.items()
    }
    return ResolvedSubagents(definitions=defns, enabled=tuple(defns))


def test_only_a_pinned_model_opts_out_of_the_main_client_fallback() -> None:
    roster = _roster(pinned=(PINNED, None), effort_only=(None, "high"))

    roles = {r.key: r for r in subagent_roles(roster, {}, MAIN)}

    assert roles["subagent:pinned"].fallback_to_main is False
    # Effort-only runs the turn's own model, so its path is unchanged.
    assert roles["subagent:effort_only"].model == MAIN
    assert roles["subagent:effort_only"].fallback_to_main is True


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", (CredentialSource.BYOK, CredentialSource.OAUTH))
async def test_an_own_key_turn_demotes_a_keyless_pin_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, credential: CredentialSource
) -> None:
    async def _resolve(user_id, model_name, **_kwargs):
        if model_name == MAIN:
            return ResolvedClient(_Client(MAIN), ModelSource.SYSTEM, credential)
        return ResolvedClient(None, ModelSource.SYSTEM, CredentialSource.NONE)

    resolver = create_autospec(clients.resolve_model_client, side_effect=_resolve)
    monkeypatch.setattr(clients, "resolve_model_client", resolver)
    monkeypatch.setattr(clients, "_stamp", create_autospec(clients._stamp))
    monkeypatch.setattr(
        clients.user_models,
        "guidance_for",
        create_autospec(clients.user_models.guidance_for, side_effect=lambda _p, m: f"guidance:{m}"),
    )

    roster = _roster(desk=(PINNED, None))
    config = SimpleNamespace(
        credential_source=credential,
        llm_client=_Client(MAIN),
        prompt_guidance=f"guidance:{MAIN}",
        subsidiary_llm_clients={},
        role_prompt_guidance={},
        subagents=SubagentsConfig(resolved=roster),
    )

    await clients._resolve_role_clients(
        config, "user-1", subagent_roles(roster, {}, MAIN), {}, {},
        is_byok=True, cache_key="t-1", main_model=MAIN,
    )
    # No copy of the main client stands in for the pinned model.
    assert "subagent:desk" not in config.subsidiary_llm_clients
    # The guidance stamped from the pin is still there for demotion to drop.
    assert config.role_prompt_guidance["subagent:desk"] == f"guidance:{PINNED}"

    with caplog.at_level(logging.WARNING):
        demote_clientless(config)

    assert config.subagents.resolved.definitions["desk"].model is None
    # Popped, so the compiler falls back to the turn model's guidance.
    assert "subagent:desk" not in config.role_prompt_guidance
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "'desk'" in m and repr(PINNED) in m and "runs on this turn's model instead" in m
        for m in warnings
    )


def test_a_pin_that_got_its_client_is_kept_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    roster = _roster(desk=(PINNED, None))
    config = SimpleNamespace(
        subagents=SubagentsConfig(resolved=roster),
        subsidiary_llm_clients={"subagent:desk": _Client(PINNED)},
        role_prompt_guidance={"subagent:desk": f"guidance:{PINNED}"},
    )

    with caplog.at_level(logging.WARNING):
        demote_clientless(config)

    assert config.subagents.resolved.definitions["desk"].model == PINNED
    assert config.role_prompt_guidance == {"subagent:desk": f"guidance:{PINNED}"}
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    (
        CredentialSource.BYOK,
        CredentialSource.OAUTH,
        CredentialSource.PLATFORM,
        CredentialSource.NONE,
    ),
)
async def test_a_pin_with_set_effort_falling_back_to_main_model_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    credential: CredentialSource,
) -> None:
    async def _resolve(user_id, model_name, *, reasoning_effort=None, **_kwargs):
        if model_name == MAIN:
            return ResolvedClient(
                _Client(MAIN, reasoning_effort), ModelSource.SYSTEM, credential
            )
        return ResolvedClient(None, ModelSource.SYSTEM, CredentialSource.NONE)

    resolver = create_autospec(clients.resolve_model_client, side_effect=_resolve)
    monkeypatch.setattr(clients, "resolve_model_client", resolver)
    monkeypatch.setattr(clients, "_stamp", create_autospec(clients._stamp))
    monkeypatch.setattr(
        clients.user_models,
        "guidance_for",
        create_autospec(
            clients.user_models.guidance_for,
            side_effect=lambda _p, m: f"guidance:{m}",
        ),
    )

    roster = _roster(desk=(PINNED, "high"))
    config = SimpleNamespace(
        credential_source=credential,
        llm_client=_Client(MAIN),
        prompt_guidance=f"guidance:{MAIN}",
        subsidiary_llm_clients={},
        role_prompt_guidance={},
        subagents=SubagentsConfig(resolved=roster),
    )

    with caplog.at_level(logging.WARNING):
        await clients._resolve_role_clients(
            config,
            "user-1",
            subagent_roles(roster, {}, MAIN),
            {},
            {},
            is_byok=True,
            cache_key="t-1",
            main_model=MAIN,
        )

    client = config.subsidiary_llm_clients["subagent:desk"]
    assert (client.model, client.effort) == (MAIN, "high")
    assert config.role_prompt_guidance["subagent:desk"] == f"guidance:{MAIN}"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "'desk'" in m
        and repr(PINNED) in m
        and repr(MAIN) in m
        and "'high'" in m
        for m in warnings
    )
