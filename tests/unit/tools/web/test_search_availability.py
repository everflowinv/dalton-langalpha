"""Unit tests for ``has_available_search_provider`` and ``resolve_available_search_provider``."""

from __future__ import annotations

import pytest

from src.config.core import clear_config_cache
from src.tools.web.manifest import get_web_providers
from src.tools.web.search import (
    has_available_search_provider,
    resolve_available_search_provider,
)

_SEARCH_ENV_KEYS = (
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "BOCHA_API_KEY",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
)


def _clear_search_env(monkeypatch: pytest.MonkeyPatch, *, app_env: str = "core") -> None:
    for key in _SEARCH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    clear_config_cache()
    get_web_providers.cache_clear()


def test_no_search_keys_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_search_env(monkeypatch, app_env="core")
    assert resolve_available_search_provider() is None
    assert has_available_search_provider() is False


def test_configured_provider_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_search_env(monkeypatch, app_env="core")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-key")

    assert resolve_available_search_provider("tavily") == "tavily"
    assert has_available_search_provider("tavily") is True
    assert resolve_available_search_provider("serper") == "serper"


def test_fallback_to_any_available_search_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_search_env(monkeypatch, app_env="core")
    monkeypatch.setenv("SERPER_API_KEY", "serper-key")

    assert resolve_available_search_provider("tavily", fallback_any=True) == "serper"
    assert has_available_search_provider("tavily", fallback_any=True) is True
    assert resolve_available_search_provider("tavily", fallback_any=False) is None
    assert has_available_search_provider("tavily", fallback_any=False) is False


def test_gemini_grounding_availability_tracks_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_search_env(monkeypatch, app_env="dalton")
    assert resolve_available_search_provider("gemini_grounding", fallback_any=False) is None
    assert has_available_search_provider("gemini_grounding", fallback_any=False) is False

    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    assert (
        resolve_available_search_provider("gemini_grounding", fallback_any=False)
        == "gemini_grounding"
    )
    assert has_available_search_provider("gemini_grounding", fallback_any=False) is True
