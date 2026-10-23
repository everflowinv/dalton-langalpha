"""Dalton-owned native LLM providers: SDK builders, OAuth token getters, kill switch (P0N.2, P0N.8)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.dalton.settings import get_disabled_providers

SdkBuilder = Callable[[Any, str | None], Any]
TokenGetter = Callable[[str], Awaitable[dict | None]]

DALTON_OAUTH_PROVIDERS: frozenset[str] = frozenset(
    {"antigravity-oauth", "muse-oauth", "xai-oauth"}
)


class ProviderDisabledError(RuntimeError):
    """Raised when a model provider is disabled in Dalton configuration (L17)."""

    def __init__(self, provider: str):
        super().__init__(
            f"模型供应商 {provider} 已在 Dalton 配置中关闭"
            "（DALTON_DISABLED_PROVIDERS / dalton.yaml models.disabled_providers）"
        )
        self.provider = provider
        self.status_code = 403


def _sdk_builders() -> dict[str, SdkBuilder]:
    from .antigravity import build_antigravity_client
    from .muse.client import build_muse_client
    from .xai_grok.client import build_grok_client

    return {
        "antigravity": lambda llm, ck: build_antigravity_client(llm),
        "muse": build_muse_client,
        "grok": build_grok_client,
    }


def _oauth_getters() -> dict[str, TokenGetter]:
    from .antigravity.oauth import get_valid_token as ag_get_valid_token
    from .muse.oauth import get_valid_token as muse_get_valid_token
    from .xai_grok.oauth import get_valid_token as xai_get_valid_token

    return {
        "antigravity-oauth": ag_get_valid_token,
        "muse-oauth": muse_get_valid_token,
        "xai-oauth": xai_get_valid_token,
    }


def build_dalton_client(llm: Any, *, cache_key: str | None = None) -> Any | None:
    """Build a Dalton-owned native SDK client for ``llm.sdk``, or return None if unknown."""
    builder = _sdk_builders().get(getattr(llm, "sdk", ""))
    return builder(llm, cache_key) if builder else None


def dalton_oauth_token_getter(provider: str) -> TokenGetter | None:
    """Return the async token getter for a Dalton-owned OAuth provider, or None."""
    return _oauth_getters().get(provider)


def is_dalton_oauth_provider(provider: str) -> bool:
    """Whether ``provider`` is a Dalton-owned OAuth provider."""
    return provider in DALTON_OAUTH_PROVIDERS


def is_isolated_coding_plan_provider(provider: str, mc: Any) -> bool:
    """Return True when ``provider`` is a coding-plan variant that only accepts its own slug key."""
    if not provider or mc is None:
        return False
    info = mc.get_provider_info(provider) if hasattr(mc, "get_provider_info") else None
    return isinstance(info, dict) and info.get("access_type") == "coding_plan"


def disabled_providers() -> set[str]:
    """Return the set of disabled provider slugs from env / dalton.yaml."""
    return get_disabled_providers()


def ensure_provider_enabled(provider: str, parent_provider: str | None = None) -> None:
    """Raise ``ProviderDisabledError`` if ``provider`` (or its parent) is disabled (P0N.8 / L17)."""
    if not provider:
        return
    disabled = get_disabled_providers()
    if not disabled:
        return
    p_norm = provider.strip().lower()
    parent_norm = (parent_provider or "").strip().lower()
    if not parent_norm:
        try:
            from src.llms.llm import LLM

            mc = LLM._model_config
            if mc is not None:
                parent_norm = (mc.get_parent_provider(provider) or "").strip().lower()
        except Exception:
            parent_norm = ""
    if p_norm in disabled or (parent_norm and parent_norm in disabled):
        raise ProviderDisabledError(provider)
