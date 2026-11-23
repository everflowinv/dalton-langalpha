"""Dalton web provider spec (`gemini_grounding`), kept free of runtime imports.

`src/tools/web/manifest.py` must stay pure data (no httpx/langchain/provider code),
so its hook imports this module rather than `src.dalton.web_search.gemini_grounding`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

from src.dalton.profile import is_core_profile
from src.tools.web.manifest import (
    CAPABILITY_SEARCH,
    CapabilitySpec,
    LevelSpec,
    WebProviderSpec,
    providers_with_capability,
)


def _current_search_api() -> str:
    """Resolve the active search_api from ``src.config.tool_settings``."""
    from src.config.tool_settings import get_search_api

    return str(get_search_api())


def register_gemini_grounding_web_provider(
    providers: Mapping[str, WebProviderSpec],
) -> Mapping[str, WebProviderSpec]:
    """Return ``providers`` with ``gemini_grounding`` when ``search_api`` selects it."""
    if _current_search_api() != "gemini_grounding" or "gemini_grounding" in providers:
        return providers
    extended = dict(providers)
    extended["gemini_grounding"] = WebProviderSpec(
        name="gemini_grounding",
        display_name="Gemini Grounding",
        env_key="GEMINI_API_KEY",
        capabilities=MappingProxyType(
            {
                CAPABILITY_SEARCH: CapabilitySpec(
                    verb=CAPABILITY_SEARCH,
                    tracking_name="GeminiGroundingSearchTool",
                    min_tier=None,
                    default_level="standard",
                    levels=(
                        LevelSpec(
                            name="standard",
                            display_name="Standard",
                            native_params={},
                            min_tier=None,
                            credits=0.0,
                        ),
                    ),
                    max_batch_size=1,
                )
            }
        ),
    )
    return MappingProxyType(extended)


def _gemini_grounding_builder(**kwargs: Any) -> Any:
    from src.dalton.web_search.gemini_grounding import build_web_search_tool

    return build_web_search_tool(**kwargs)


class _GeminiGroundingBuilderRegistry(Mapping[str, Callable[..., Any]]):
    """Expose ``gemini_grounding`` iff ``search_api == "gemini_grounding"``."""

    def __init__(self, base: Mapping[str, Callable[..., Any]]) -> None:
        self._base = base

    def _active(self) -> dict[str, Callable[..., Any]]:
        active = dict(self._base)
        if _current_search_api() == "gemini_grounding":
            active["gemini_grounding"] = _gemini_grounding_builder
        return active

    def __getitem__(self, key: str) -> Callable[..., Any]:
        return self._active()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._active())

    def __len__(self) -> int:
        return len(self._active())

    def __contains__(self, key: object) -> bool:
        return key in self._active()


def register_gemini_grounding_search_builder(
    builders: Mapping[str, Callable[..., Any]],
) -> Mapping[str, Callable[..., Any]]:
    """Wrap builders so ``gemini_grounding`` is registered iff ``search_api`` selects it."""
    return _GeminiGroundingBuilderRegistry(builders)


def _provider_has_credentials(spec: WebProviderSpec) -> bool:
    """Return ``True`` when a provider needs no key or its configured ``env_key`` is set."""
    if not spec.env_key:
        return True
    return bool(os.getenv(spec.env_key, "").strip())


def resolve_available_search_provider(
    provider: str | None = None,
    *,
    builders: Mapping[str, Callable[..., Any]] | None = None,
    fallback_any: bool = True,
) -> str | None:
    """Return the first search provider with a registered builder and available credentials.

    Checks ``provider`` (or the deployment default ``search_api``) first. When
    ``fallback_any`` is True and that provider has no credentials, scans the
    remaining manifest search providers in manifest order. Returns ``None`` if
    no search provider can currently run.
    """
    if builders is None:
        from src.tools.web.search import _PROVIDER_BUILDERS

        builders = _PROVIDER_BUILDERS

    search_providers = providers_with_capability(CAPABILITY_SEARCH)
    preferred = (provider if provider is not None else _current_search_api()).strip()

    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    if fallback_any:
        for name in search_providers:
            if name not in candidates:
                candidates.append(name)

    for name in candidates:
        spec = search_providers.get(name)
        if spec is None or name not in builders:
            continue
        if _provider_has_credentials(spec):
            return name
    return None


def has_available_search_provider(
    provider: str | None = None,
    *,
    builders: Mapping[str, Callable[..., Any]] | None = None,
    fallback_any: bool = True,
) -> bool:
    """Return ``True`` when a usable search provider is available for the current config."""
    return (
        resolve_available_search_provider(
            provider=provider,
            builders=builders,
            fallback_any=fallback_any,
        )
        is not None
    )


def resolve_web_search_provider_for_agent(
    provider: str | None = None,
    *,
    fallback_any: bool = True,
) -> str | None:
    """Return the search provider an agent/subagent should register ``WebSearch`` with, or ``None``.

    - In ``everflow`` profile: preserves existing behavior verbatim (always
      registers ``WebSearch`` with ``provider``).
    - In ``core`` profile: registers ``WebSearch`` only when a search provider
      actually has credentials available; returns ``None`` otherwise so
      ``WebSearch`` is omitted while ``WebFetch`` remains registered.
    """
    if not is_core_profile():
        return provider or ""
    return resolve_available_search_provider(provider=provider, fallback_any=fallback_any)
