"""Dalton-owned OAuth lines in the upstream subscription registry.

Upstream's ``oauth_providers.SUBSCRIPTIONS`` drives connection listing,
availability and client building; the three Dalton lines (Google Antigravity,
Meta Muse, xAI's SuperGrok CLI proxy) register here so they flow through the
same doors. Two of them converge on the common refresh protocol with adapters
for their token answers; Muse keeps its own token getter, because its storage
is a minted API key rather than a refreshable ``TokenSet`` and only its own
code can tell a live key from an invalidated or legacy row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.server.services.oauth_providers import SubscriptionProvider
from src.server.services.oauth_protocol import TokenSet


def _token_set_from_answer(data: dict[str, Any], *, previous_refresh_token: str = "") -> TokenSet:
    """A Dalton token-endpoint answer as the common protocol's ``TokenSet``.

    Antigravity and xAI answers are OAuth refresh answers (``access_token``,
    maybe ``refresh_token``, ``expires_in``); the identity claims ride the
    answer where the provider sends them.
    """
    expires_raw = data.get("expires_at")
    if isinstance(expires_raw, datetime):
        expires_at = expires_raw
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(data.get("expires_in") or 3600)
        )
    return TokenSet(
        access_token=str(data["access_token"]),
        refresh_token=str(
            data.get("refresh_token") or data.get("refresh") or previous_refresh_token
        ),
        expires_at=expires_at,
        account_id=str(data.get("account_id") or data.get("accountId") or ""),
        email=data.get("email"),
        plan_type=data.get("plan_type"),
    )


async def _antigravity_refresh(refresh_token: str) -> TokenSet:
    from .providers.antigravity.oauth import refresh_tokens

    return _token_set_from_answer(await refresh_tokens(refresh_token))


async def _xai_refresh(refresh_token: str) -> TokenSet:
    # Keeps the Cloudflare-challenge retry and endpoint discovery of the xAI
    # CLI path; its pacing is set on the provider entry (``refresh_deadline_s``
    # / ``lock_ttl_ms``), and a challenge or timeout reads as transient, so a
    # slow or blocked refresh never flags the connection for good.
    from dataclasses import replace

    from .providers.xai_grok.oauth import XAI_PLAN_TYPE, refresh_tokens

    fresh = _token_set_from_answer(await refresh_tokens(refresh_token))
    # TokenSet is frozen; the CLI answer names no plan, the SuperGrok one stands.
    if not fresh.plan_type:
        fresh = replace(fresh, plan_type=XAI_PLAN_TYPE)
    return fresh


async def _muse_valid_token(user_id: str) -> Optional[TokenSet]:
    from .providers.muse.oauth import get_valid_token

    data = await get_valid_token(user_id)
    if not data:
        return None
    # The delegate path never consults ``expires_at`` — Muse's own getter
    # refuses an expired or invalidated key itself — so it only has to be
    # a datetime for the dataclass.
    return TokenSet(
        access_token=data["access_token"],
        refresh_token="",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        plan_type=data.get("plan_type"),
    )


async def _muse_not_refreshable(_refresh_token: str) -> TokenSet:
    raise RuntimeError("the Muse line has no refresh token; its token_getter is the protocol")


def dalton_subscription_providers() -> list[SubscriptionProvider]:
    """The Dalton-owned OAuth lines, as registry entries."""
    from .providers.antigravity.oauth import ANTIGRAVITY_PROVIDER
    from .providers.muse.oauth import MUSE_PROVIDER
    from .providers.xai_grok.oauth import (
        XAI_PROVIDER,
        XAI_REFRESH_HARD_TIMEOUT_S,
        XAI_REFRESH_LOCK_TTL_MS,
    )

    return [
        SubscriptionProvider(
            id=ANTIGRAVITY_PROVIDER,
            account_name="Google Antigravity",
            refresh=_antigravity_refresh,
        ),
        SubscriptionProvider(
            id=MUSE_PROVIDER,
            account_name="Muse Code",
            # No refresh callable: the delegate below is the whole protocol.
            refresh=_muse_not_refreshable,
            token_getter=_muse_valid_token,
        ),
        SubscriptionProvider(
            id=XAI_PROVIDER,
            account_name="Grok (SuperGrok)",
            refresh=_xai_refresh,
            refresh_deadline_s=float(XAI_REFRESH_HARD_TIMEOUT_S),
            lock_ttl_ms=int(XAI_REFRESH_LOCK_TTL_MS),
        ),
    ]
