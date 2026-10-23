"""Rebuild an OAuth-backed chat client with a fresh access token.

A turn resolves its OAuth clients once, at the start, and bakes the access token
in as ``api_key``. Claude, Codex and Antigravity tokens live one to eight hours,
so a turn that outlasts the token it started with gets 401 on every later call.
The resolver tags each client it builds with what it was built from; on a 401
the resilience middleware rebuilds that client, which re-reads the stored token
(refreshing it under the shared Redis lock when due), and retries the same model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

_ATTR = "_dalton_oauth_rebuild"


@dataclass(frozen=True, slots=True)
class OAuthRebuildSpec:
    user_id: str
    model_name: str
    provider: str = ""
    # Short digest of the access token the client was built with, never the token:
    # tells a rebuild that came back with the very token the provider just refused.
    token_digest: str = ""
    reasoning_effort: str | None = None
    service_tier: str | None = None
    cache_key: str | None = None


def token_digest(access_token: str) -> str:
    return hashlib.sha256(access_token.encode()).hexdigest()[:16]


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


async def _resolve(spec: OAuthRebuildSpec) -> Any | None:
    from src.server.services.llm.clients import resolve_oauth_llm_client

    return await resolve_oauth_llm_client(
        spec.user_id,
        spec.model_name,
        spec.reasoning_effort,
        service_tier=spec.service_tier,
        cache_key=spec.cache_key,
    )


async def _expire_stored_token(spec: OAuthRebuildSpec) -> None:
    """Mark the stored token due, so the provider's own getter refreshes it under its lock.

    Reached only when the provider refused a token our record still calls valid
    (revoked early, or a lifetime shorter than the one recorded).
    """
    from src.dalton.credentials import credential_user_id
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE user_oauth_tokens SET expires_at = NOW() "
            "WHERE user_id = %s AND provider = %s AND expires_at > NOW()",
            (credential_user_id(spec.user_id), spec.provider),
        )


async def rebuild_oauth_client(client: Any) -> Any | None:
    """A new client for the same model with a token other than the refused one, or None.

    None when ``client`` was not built from an OAuth credential (API key, broker)
    or no different token can be had; the caller then falls back as before.
    """
    spec = oauth_rebuild_spec(client)
    if spec is None:
        return None
    try:
        fresh = await _resolve(spec)
        fresh_spec = oauth_rebuild_spec(fresh)
        if fresh is not None and fresh_spec is not None and spec.token_digest and (
            fresh_spec.token_digest == spec.token_digest
        ):
            if not spec.provider:
                return None
            await _expire_stored_token(spec)
            fresh = await _resolve(spec)
            fresh_spec = oauth_rebuild_spec(fresh)
            if fresh_spec is None or fresh_spec.token_digest == spec.token_digest:
                return None
        return _carry_prompt_cache_key(client, fresh)
    except Exception:
        return None


def _carry_prompt_cache_key(failed: Any, fresh: Any) -> Any:
    """Keep a subagent's narrowed cache key (and the Muse/Grok session derived from it).

    Subagent clients are ``narrow_prompt_cache_key`` copies of the turn's client,
    so they carry the parent's spec; rebuilt from it they would rejoin the parent
    session. Apply the same narrowing the copy had.
    """
    if fresh is None:
        return None
    key = (getattr(failed, "model_kwargs", None) or {}).get("prompt_cache_key")
    fresh_mk = getattr(fresh, "model_kwargs", None) or {}
    if not key or fresh_mk.get("prompt_cache_key") == key:
        return fresh
    return fresh.model_copy(update={"model_kwargs": {**fresh_mk, "prompt_cache_key": key}})
