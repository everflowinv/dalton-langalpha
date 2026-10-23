"""Rebuild an OAuth-backed chat client with a fresh access token.

A turn resolves its OAuth clients once, at the start, and bakes the access token
in as ``api_key``. Claude, Codex and Antigravity tokens live one to eight hours,
so a turn that outlasts the token it started with gets 401 on every later call.
The resolver tags each client it builds with what it was built from; on a 401
the resilience middleware rebuilds that client, which re-reads the stored token
(refreshing it under the shared Redis lock when due), and retries the same model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ATTR = "_dalton_oauth_rebuild"


@dataclass(frozen=True, slots=True)
class OAuthRebuildSpec:
    user_id: str
    model_name: str
    reasoning_effort: str | None = None
    service_tier: str | None = None
    cache_key: str | None = None


def tag_oauth_client(client: Any, spec: OAuthRebuildSpec) -> Any:
    """Record how ``client`` was built. Not a model field: never serialized or sent."""
    if client is not None:
        try:
            object.__setattr__(client, _ATTR, spec)
        except (AttributeError, TypeError):
            pass
    return client


def oauth_rebuild_spec(client: Any) -> OAuthRebuildSpec | None:
    spec = getattr(client, _ATTR, None)
    return spec if isinstance(spec, OAuthRebuildSpec) else None


async def rebuild_oauth_client(client: Any) -> Any | None:
    """A new client for the same model with a currently valid token, or None.

    None when ``client`` was not built from an OAuth credential (API key, broker)
    or the credential can no longer be resolved; the caller then falls back as
    before.
    """
    spec = oauth_rebuild_spec(client)
    if spec is None:
        return None
    from src.server.services.llm.clients import resolve_oauth_llm_client

    try:
        return await resolve_oauth_llm_client(
            spec.user_id,
            spec.model_name,
            spec.reasoning_effort,
            service_tier=spec.service_tier,
            cache_key=spec.cache_key,
        )
    except Exception:
        return None
