"""SYNC3 B4 (#649): Claude Code models merge in but stay closed for Dalton.

The kill switch lives in config.dalton.yaml (claude_code_runtime.enabled:
false), exactly as for the B3 MCP-description switch. These tests load that
real overlay through the cached loader the runtime uses — APP_ENV is set and
the caches cleared inside the test — and then exercise the two surfaces the
B4 card names, without patching the flag anywhere:

1. the catalog a Dalton user is offered (ModelConfig.get_model_metadata /
   get_configured_llm_models via listed_models → provider_listed);
2. admission (resolve_llm_config → require_runnable), the layer that answers
   a request naming a Claude Code model directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import DaytonaConfig, FilesystemConfig, LoggingConfig, MCPConfig, SandboxConfig, SecurityConfig
from src.config.core import clear_config_cache
from src.llms import claude_code
from src.llms.llm import ModelConfig, get_configured_llm_models

CODE_ROWS = {"claude-haiku-5-5-code", "claude-sonnet-5-5-code", "claude-opus-5-5-code"}


@pytest.fixture()
def dalton_overlay(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dalton")
    clear_config_cache()
    try:
        yield
    finally:
        # Leave the caches empty so no later test in this process resolves
        # against the dalton overlay.
        clear_config_cache()


def test_dalton_overlay_keeps_the_claude_code_runtime_flag_dark(dalton_overlay):
    from src.config.features import is_feature_enabled_system

    assert is_feature_enabled_system(claude_code.FEATURE) is False


def test_the_runtime_is_unavailable_under_the_dalton_overlay(dalton_overlay):
    # No patch on rows_available: every input it reads is the real one, and
    # the overlay's flag is the first to fail. (#652 split the old
    # runtime_available into rows_available/backend_available; rows_available
    # is the one every offering reads.)
    assert claude_code.rows_available() is False


def test_no_claude_code_rows_are_listed_for_dalton_users(dalton_overlay):
    from src.server.services.llm.provider_catalog import provider_catalog

    metadata = ModelConfig().get_model_metadata()
    assert not CODE_ROWS & set(metadata)
    grouped = get_configured_llm_models()
    assert "claude-code" not in grouped
    assert not CODE_ROWS & {m for names in grouped.values() for m in names}
    provider_catalog.cache_clear()
    try:
        assert "claude-code" not in {p["provider"] for p in provider_catalog()}
    finally:
        provider_catalog.cache_clear()
    # The subscription rows beside them stay offered.
    assert "claude-opus-5-5-oauth" in metadata


def test_without_the_overlay_the_runtime_still_needs_an_operator_opt_in(monkeypatch):
    # The overlay line is not the only thing holding Claude Code closed: the
    # upstream code default is off with an opt-in gate too. This pins the
    # tests above against a future overlay removal reading as a regression.
    monkeypatch.delenv("APP_ENV", raising=False)
    clear_config_cache()
    try:
        assert claude_code.rows_available() is False
    finally:
        clear_config_cache()


def _base() -> AgentConfig:
    return AgentConfig(
        llm=LLMConfig(name="claude-sonnet-5-5", background="claude-haiku-5-5", fallback=["claude-haiku-5-5"]),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )


@pytest.mark.asyncio
async def test_a_request_naming_a_claude_code_model_is_refused_at_admission(dalton_overlay):
    """The layer that refuses: resolve_llm_config, before any client or graph
    is built. It answers HTTP 400, detail type "claude_code_off", reason
    "server" — the row is never cleared as removed, and no other model runs
    in its place. Unlike upstream's routing tests, runtime_available is not
    patched: the overlay's flag is what fails."""
    from src.server.services.llm.config import resolve_llm_config

    cleanup = AsyncMock(return_value=[])
    with (
        patch("src.server.services.llm.user_models.get_model_preference", new_callable=AsyncMock, return_value={}),
        patch("src.server.services.llm.clients.resolve_oauth_llm_client", new_callable=AsyncMock, return_value=None),
        patch(
            "src.server.services.llm.config.effective_flags_for_user",
            new_callable=AsyncMock,
            return_value={"claude_code_runtime": False},
        ),
        patch("src.server.services.llm.config.cleanup_stale_model_preferences", new=cleanup),
    ):
        with pytest.raises(HTTPException) as refused:
            await resolve_llm_config(
                _base(), "user-1", "claude-sonnet-5-5-code", False, slot="primary", role="analyst"
            )
    assert refused.value.status_code == 400
    assert refused.value.detail == {
        "message": "This server does not run Claude Code models. Pick another model.",
        "type": "claude_code_off",
        "model": "claude-sonnet-5-5-code",
        "reason": "server",
    }
