"""Dalton-owned Google Antigravity provider package."""

from __future__ import annotations

from typing import Any

from .client import AntigravityApiError, ChatAntigravity


def build_antigravity_client(llm: Any) -> ChatAntigravity:
    """Build a ``ChatAntigravity`` client from a LangAlpha ``LLM`` factory instance."""
    params: dict[str, Any] = {
        "model": llm.model,
        "api_key": llm._resolve_api_key(),
    }
    params.update(llm._resolve_base_url("base_url"))

    if llm.default_headers:
        params["default_headers"] = dict(llm.default_headers)

    params.update(llm.parameters)
    return ChatAntigravity(**params)


__all__ = ["AntigravityApiError", "ChatAntigravity", "build_antigravity_client"]
