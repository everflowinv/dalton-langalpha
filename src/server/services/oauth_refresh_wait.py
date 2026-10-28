"""What a request does while another one holds the lock to refresh the same
OAuth token."""

import asyncio
from datetime import datetime, timedelta, timezone

from src.server.database.oauth_tokens import get_oauth_tokens

# How close to expiry a token is refreshed rather than used.
REFRESH_BUFFER = timedelta(minutes=5)
# The refresh lock lives 35 seconds; a provider answers a refresh in a few,
# so a waiter gives up well before the lock would.
REFRESH_LOCK_TTL_MS = 35_000
# The whole refresh call, inside the lease: httpx bounds each phase only, so a
# slow trickle could outlast it and let a second worker spend the same
# single-use refresh token.
REFRESH_DEADLINE_S = 25
# Saving what the refresh returned, retries included, in what the lease has left.
REFRESH_SAVE_DEADLINE_S = 8
_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.5


def wait_window(refresh_deadline_s: float) -> float:
    """How long a waiter waits for the lock holder's refresh: as long as that
    provider's whole refresh plus its save can take. The default window was
    written for the common 25-second lease; a provider with long retries (the
    xAI CLI path refreshes up to 120 s) would have its waiters give up while
    the refresh is still honestly running (DALTON-HOOK(oauth-dispatch))."""
    return max(_WAIT_SECONDS, refresh_deadline_s + REFRESH_SAVE_DEADLINE_S)


def token_expiry(tokens: dict) -> datetime:
    expires_at = tokens["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at


async def await_refreshed_tokens(
    user_id: str, provider: str, *, wait_seconds: float = _WAIT_SECONDS
) -> dict | None:
    """The stored tokens once the refresh lands; None when it has not landed
    in time and the stored token has already expired. A row the refresh
    flagged as needing sign-in comes back at once, for the caller to refuse.

    The old token is handed out only while it still works: an expired one
    comes back from the provider as a 401, which reads the same as a key the
    user revoked, and an automation is switched off for that.

    ``wait_seconds`` is the lock holder's whole window (``wait_window`` of its
    ``refresh_deadline_s``); waiting the default while a long-retry provider
    refreshes would report an outage that is only patience (DALTON-HOOK(oauth-dispatch)).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        tokens = await get_oauth_tokens(user_id, provider)
        if not tokens:
            return None
        if tokens.get("needs_reauth_at"):
            return tokens
        now = datetime.now(timezone.utc)
        if token_expiry(tokens) > now + REFRESH_BUFFER:
            return tokens
        if loop.time() >= deadline:
            return tokens if token_expiry(tokens) > now else None
