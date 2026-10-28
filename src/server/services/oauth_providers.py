"""The subscription providers a user can sign in with, and what sets each apart.

Everything that keeps a connection usable (``oauth_health``), serves a turn on
it (``llm.clients``) or lists its models (``llm.provider_models``) reads a
provider from ``SUBSCRIPTIONS`` rather than branching on its id. The registry
is constant configuration, the same in every worker.
"""

from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Optional

from src.server.services import claude_oauth, codex_oauth, grok_oauth
from src.server.services.oauth_protocol import TokenSet, permanent_refresh_failure
from src.server.services.oauth_refresh_wait import (  # DALTON-HOOK(oauth-dispatch): per-provider pacing defaults
    REFRESH_BUFFER,
    REFRESH_DEADLINE_S,
    REFRESH_LOCK_TTL_MS,
)


def _refused_on_401(status: Any, error_text: str) -> bool:
    return status == 401


def _never(status: Any, error_text: str) -> bool:
    return False


@dataclass(frozen=True)
class SubscriptionProvider:
    """One subscription provider.

    ``refresh(refresh_token)`` trades a refresh token at the token endpoint;
    ``permanent_failure(exc)`` reads its failure as the code to flag the
    connection with, or None for a transient one. ``token_refused(status,
    error_text)`` says a model call refused the access token itself, which a
    refresh may fix; ``plan_refused`` that it refused the account's plan,
    which nothing on our side fixes. ``request_headers(tokens)`` are sent with
    every call the token makes.
    """

    id: str
    account_name: str
    refresh: Callable[[str], Awaitable[TokenSet]]
    refresh_buffer: Callable[[TokenSet], timedelta] = lambda _tokens: REFRESH_BUFFER
    permanent_failure: Callable[[BaseException], Optional[str]] = permanent_refresh_failure
    token_refused: Callable[[Any, str], bool] = _refused_on_401
    plan_refused: Callable[[Any, str], bool] = _never
    request_headers: Callable[[TokenSet], dict[str, str]] = lambda _tokens: {}
    # DALTON-HOOK(oauth-dispatch): a provider-owned token protocol. When set,
    # ``valid_token`` delegates to it instead of running the generic refresh
    # path — for a line whose storage is not a refreshable ``TokenSet`` (Muse
    # stores a minted API key with its own invalid/legacy markers).
    token_getter: Optional[Callable[[str], Awaitable[Optional[TokenSet]]]] = None
    # DALTON-HOOK(oauth-dispatch): per-provider refresh pacing. The defaults
    # are the common lease; a refresh with long retries (xAI's Cloudflare
    # backoff) needs a deadline that outlives them and a lock that outlives
    # the deadline, or the lease lapses under a slow write and a second
    # worker spends the same single-use refresh token.
    refresh_deadline_s: float = REFRESH_DEADLINE_S
    lock_ttl_ms: int = REFRESH_LOCK_TTL_MS


# DALTON-HOOK(oauth-dispatch): Dalton-owned OAuth lines (Google Antigravity,
# Meta Muse, xAI's SuperGrok CLI proxy) join the registry so model listing,
# availability and client building treat them like any subscription instead
# of branching on their ids. Their adapters live in Dalton's own tree.
from src.dalton.llm.subscriptions import dalton_subscription_providers  # noqa: E402

_entries: dict[str, SubscriptionProvider] = {
    provider.id: provider
    for provider in (
        SubscriptionProvider(
            id=codex_oauth.CODEX_PROVIDER,
            account_name="ChatGPT",
            refresh=codex_oauth.refresh_tokens,
            request_headers=codex_oauth.request_headers,
        ),
        SubscriptionProvider(
            id=claude_oauth.CLAUDE_PROVIDER,
            account_name="Claude",
            refresh=claude_oauth.refresh_tokens,
        ),
        SubscriptionProvider(
            id=grok_oauth.GROK_PROVIDER,
            account_name="Grok",
            refresh=grok_oauth.refresh_tokens,
            refresh_buffer=grok_oauth.refresh_buffer,
            permanent_failure=grok_oauth.permanent_grok_refresh_failure,
            # xAI also answers a stale token with a 403, which otherwise
            # means the plan has no API access.
            token_refused=grok_oauth.stale_token_refusal,
            plan_refused=grok_oauth.plan_refusal,
        ),
    )
}
_entries.update({p.id: p for p in dalton_subscription_providers()})  # DALTON-HOOK(oauth-dispatch)

SUBSCRIPTIONS: Mapping[str, SubscriptionProvider] = MappingProxyType(_entries)
