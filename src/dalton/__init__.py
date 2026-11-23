"""Dalton on LangAlpha backend package.

Exports are resolved lazily via PEP 562 ``__getattr__`` so importing
stdlib-only submodules such as ``src.dalton.profile`` or
``src.dalton.sec_identity`` never triggers ``src.dalton.settings`` or
``src.config`` initialization as a side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.dalton.settings import (
        DaltonConfig,
        get_dalton_broker_token,
        get_dalton_config,
        get_dalton_researchdata_url,
        get_sec_user_agent,
        is_budget_gate_enabled,
    )

__all__ = [
    "DaltonConfig",
    "get_dalton_broker_token",
    "get_dalton_config",
    "get_dalton_researchdata_url",
    "get_sec_user_agent",
    "is_budget_gate_enabled",
]

_SETTINGS_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> Any:
    if name in _SETTINGS_EXPORTS:
        from src.dalton import settings

        return getattr(settings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
