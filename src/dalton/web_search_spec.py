"""The OpenClaw web provider spec, kept free of the provider's runtime imports.

`src/tools/web/manifest.py` must stay pure data (no httpx/langchain/provider code),
so its hook imports this module rather than `src.dalton.web_search.provider`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

from src.tools.web.manifest import (
    CAPABILITY_SEARCH,
    CapabilitySpec,
    LevelSpec,
    WebProviderSpec,
)


def _current_search_api() -> str:
    """Resolve the active search_api from ``src.config.tool_settings``."""
    from src.config.tool_settings import get_search_api

    return str(get_search_api())


def register_openclaw_web_provider(
    providers: Mapping[str, WebProviderSpec],
) -> Mapping[str, WebProviderSpec]:
    """Return ``providers`` with the ``openclaw`` spec added when ``search_api == "openclaw"``."""
    if _current_search_api() != "openclaw" or "openclaw" in providers:
        return providers
    extended = dict(providers)
    extended["openclaw"] = WebProviderSpec(
        name="openclaw",
        display_name="OpenClaw",
        env_key="DALTON_WEB_SEARCH_BRIDGE_TOKEN",
        capabilities=MappingProxyType(
            {
                CAPABILITY_SEARCH: CapabilitySpec(
                    verb=CAPABILITY_SEARCH,
                    tracking_name="OpenClawSearchTool",
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


def _openclaw_builder(**kwargs: Any) -> Any:
    from src.dalton.web_search.provider import build_web_search_tool

    return build_web_search_tool(**kwargs)


class _OpenClawBuilderRegistry(Mapping[str, Callable[..., Any]]):
    """Expose ``openclaw`` in ``_PROVIDER_BUILDERS`` iff ``search_api == "openclaw"``."""

    def __init__(self, base: Mapping[str, Callable[..., Any]]) -> None:
        self._base = dict(base)

    def _active(self) -> dict[str, Callable[..., Any]]:
        if _current_search_api() == "openclaw":
            return {**self._base, "openclaw": _openclaw_builder}
        return dict(self._base)

    def __getitem__(self, key: str) -> Callable[..., Any]:
        return self._active()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._active())

    def __len__(self) -> int:
        return len(self._active())

    def __contains__(self, key: object) -> bool:
        return key in self._active()


def register_openclaw_search_builder(
    builders: Mapping[str, Callable[..., Any]],
) -> Mapping[str, Callable[..., Any]]:
    """Wrap ``_PROVIDER_BUILDERS`` so ``openclaw`` is registered iff ``get_web_providers()`` includes it."""
    return _OpenClawBuilderRegistry(builders)
