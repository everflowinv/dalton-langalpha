"""Gating the user's search and crawl preferences behind their plan tier.

Nothing here resolves a model, which is why it is not in ``config``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import logger

#: Whether the user's plan reaches a manifest min tier. Fetches the tier at
#: most once, on first use, for whichever check needs it first.
TierPermits = Callable[[int], Awaitable[bool]]


@dataclass(frozen=True)
class SearchGate:
    """The search provider and depth to run, and why a set one was not.

    A fallback is ``"not_on_plan"``, ``"unknown"`` for a provider the manifest
    lacks, or, for a depth, ``"not_offered"`` by the provider left standing;
    None when the set value runs or none was set.
    """

    provider: str
    depth: str
    provider_fallback: str | None
    depth_fallback: str | None


def _set(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def gate_search(
    provider: str | None,
    depth: str | None,
    *,
    effective_provider: str,
    effective_depth: str,
    permits: TierPermits | None,
) -> SearchGate:
    """The search pair to run for a set ``provider`` and ``depth`` (None inherits).

    ``effective_*`` is the pair already running, the user's gated one or the
    deployment's, so a value equal to it is never tier-checked. A depth is
    judged against the provider left after the provider check, which may not
    offer even the effective depth, and one that falls back takes the
    effective depth where that provider offers it, else the provider's
    default. ``permits`` None gates nothing.
    """
    from src.tools.web.manifest import CAPABILITY_SEARCH, get_capability, resolve_min_tier

    async def reached(spec) -> bool:
        return permits is None or await permits(resolve_min_tier(spec))

    chosen, provider_fallback = effective_provider, None
    if provider is not None and provider != effective_provider:
        cap = get_capability(provider, CAPABILITY_SEARCH)
        if cap is None:
            logger.warning(f"[CHAT] Ignoring unknown search provider: {provider!r}")
            provider_fallback = "unknown"
        elif await reached(cap):
            chosen = provider
        else:
            provider_fallback = "not_on_plan"

    cap = get_capability(chosen, CAPABILITY_SEARCH)
    inherited = effective_depth
    if cap is not None and cap.level(effective_depth) is None:
        inherited = cap.default_level
    if depth is None or (depth == effective_depth and chosen == effective_provider):
        return SearchGate(chosen, inherited, provider_fallback, None)
    spec = cap.level(depth) if cap is not None else None
    if spec is None:
        return SearchGate(chosen, inherited, provider_fallback, "not_offered")
    # The user's own depth is never re-checked against the plan, here or on another provider.
    if depth == effective_depth or await reached(spec):
        return SearchGate(chosen, depth, provider_fallback, None)
    return SearchGate(chosen, inherited, provider_fallback, "not_on_plan")


async def gate_capabilities(config, user_id: str, model_pref: dict) -> TierPermits:
    """Apply the user's search and crawl preferences, each behind its manifest tier.

    Resolve time is the enforcement point, so a stale preference left by a
    downgraded plan is ignored at consumption rather than trusted from storage.
    Returns the tier check it used, so the rest of the turn's gates share its
    one tier fetch.
    """
    from src.config.settings import HOST_MODE

    is_platform = HOST_MODE == "platform"
    user_tier: int | None = None

    async def _tier_permits(min_tier: int) -> bool:
        """One polarity for every capability gate: OSS/BYOK deployments are
        never gated; platform users must meet the manifest min tier."""
        nonlocal user_tier
        if not is_platform:
            return True
        if user_tier is None:
            # ChatAuthResult.access_tier is -1 for BYOK/OAuth users, so the
            # true tier is resolved here — lazily, Redis-cached, once per turn.
            from src.server.dependencies.usage_limits import _fetch_platform_tier

            tier = await _fetch_platform_tier(user_id)
            # Unknown (the platform down, nothing on record) gates as no plan:
            # the turn loses a capability for the outage, not its answer.
            user_tier = -1 if tier is None else tier
        return user_tier >= min_tier

    # The pair the search tool will run, so a depth the provider lacks reads
    # as the provider's own wherever the main agent's settings are shown.
    gate = await gate_search(
        _set(model_pref.get("search_provider")),
        _set(model_pref.get("search_depth")),
        effective_provider=config.search_api,
        effective_depth=config.search_depth,
        permits=_tier_permits,
    )
    config.search_api, config.search_depth = gate.provider, gate.depth
    if gate.provider_fallback or gate.depth_fallback:
        logger.debug(f"[CHAT] Search preference fell back: {gate}")

    # Crawl tools (WebCrawl/WebMap): experimental opt-in via the site_crawl
    # feature; for opted-in users the crawl capability's manifest min_tier
    # still applies (_tier_permits keeps OSS deployments ungated). The tool
    # factory self-skips when the provider key is unset.
    if config.feature_enabled("site_crawl"):
        from src.config.tool_settings import get_crawl_provider
        from src.tools.web.manifest import CAPABILITY_CRAWL, get_capability, resolve_min_tier

        crawl_cap = get_capability(get_crawl_provider(), CAPABILITY_CRAWL)
        if crawl_cap is not None and not await _tier_permits(resolve_min_tier(crawl_cap)):
            config.features["site_crawl"] = False
            logger.debug(f"[CHAT] crawl tools disabled (tier below {resolve_min_tier(crawl_cap)})")

    return _tier_permits


