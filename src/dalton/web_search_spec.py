"""The OpenClaw web provider spec, kept free of the provider's runtime imports.

`src/tools/web/manifest.py` must stay pure data (no httpx/langchain/provider code),
so its hook imports this module rather than `src.dalton.web_search.provider`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from src.tools.web.manifest import (
    CAPABILITY_SEARCH,
    CapabilitySpec,
    LevelSpec,
    WebProviderSpec,
)


def register_openclaw_web_provider(
    providers: Mapping[str, WebProviderSpec],
) -> Mapping[str, WebProviderSpec]:
    """Return ``providers`` with the ``openclaw`` spec added when ``search_api == "openclaw"``."""
    from src.config.tool_settings import get_search_api

    if get_search_api() != "openclaw" or "openclaw" in providers:
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
