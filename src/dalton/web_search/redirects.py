"""Resolve Gemini grounding redirect links in OpenClaw search results.

``vertexaisearch.cloud.google.com/grounding-api-redirect/...`` citations 403 or
come back empty on a later fetch, because the redirector is gated for the
grounding API, not for ordinary clients. Before results leave the provider,
each such link is resolved to the source URL it forwards to — headers only, the
body is never downloaded — and the original link is kept beside it for
provenance. A resolution that fails (timeout, no Location, blocked target)
keeps the original link and marks the row ``url_unresolved: true``.

Egress follows the web tools' existing rules: every hop passes the crawler's
literal-address check (``_validate_url``) before the request, and the requests
ride ``GuardedAsyncTransport``, which re-validates the resolved IP on every
connection — so a redirect into a private network is refused, never followed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import httpx

from src.tools.web.inhouse.guard import BlockedAddressError, GuardedAsyncTransport

logger = logging.getLogger(__name__)

GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
GROUNDING_REDIRECT_PREFIX = "/grounding-api-redirect"

# One resolution follows at most this many hops before giving up on the chain.
_MAX_HOPS = 5
_USER_AGENT = "Mozilla/5.0 (compatible; LangAlpha/1.0)"


def is_grounding_redirect(url: str) -> bool:
    """Whether ``url`` is a Gemini grounding-api redirect link."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        (parsed.hostname or "").lower() == GROUNDING_REDIRECT_HOST
        and parsed.path.startswith(GROUNDING_REDIRECT_PREFIX)
    )


def _hop_blocked(url: str) -> bool:
    """The crawler's literal SSRF rule (scheme / localhost / literal private IP)."""
    from src.tools.web.inhouse.extractors import ExtractorError
    from src.tools.web.inhouse.extractors.base import _validate_url

    try:
        _validate_url(url)
    except ExtractorError:
        return True
    return False


async def _follow_hops(client: httpx.AsyncClient, url: str) -> str | None:
    """The URL the redirect chain ends at, or None when it never lands on a page.

    Only response headers are read; each stream is closed before the body is
    downloaded. A hop that is itself a redirect keeps the chain going, a 2xx
    ends it, and anything else (4xx/5xx, missing Location) gives up.
    """
    current = url
    for _ in range(_MAX_HOPS):
        if _hop_blocked(current):
            return None
        try:
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if 200 <= response.status_code < 400:
                    return str(response.url or current)
                return None
        except (httpx.HTTPError, BlockedAddressError, OSError):
            return None
    return None


async def _resolve_row_url(
    client: httpx.AsyncClient, url: str, timeout_s: float
) -> str | None:
    """Resolve one grounding link under its own deadline."""
    try:
        return await asyncio.wait_for(_follow_hops(client, url), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        return None


async def resolve_grounding_redirects(
    results: List[Dict[str, Any]],
    cards: List[Dict[str, str]],
    *,
    timeout_s: float = 5.0,
    transport: Any = None,
) -> None:
    """Rewrite grounding redirect links in ``results``/``cards`` in place.

    ``url`` becomes the resolved source URL and the redirector link moves to
    ``redirect_url``; cards only carry the swapped ``url``. Rows whose link
    could not be resolved keep it and gain ``url_unresolved: true``.
    ``timeout_s`` bounds one whole resolution and ``transport`` lets tests
    substitute an ``httpx.MockTransport`` for the guarded one.
    """
    targets = [
        (idx, str(row.get("url") or ""))
        for idx, row in enumerate(results)
        if isinstance(row, dict) and is_grounding_redirect(str(row.get("url") or ""))
    ]
    if not targets:
        return
    timeout = httpx.Timeout(timeout_s, connect=timeout_s)
    async with httpx.AsyncClient(
        transport=transport if transport is not None else GuardedAsyncTransport(),
        follow_redirects=False,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resolved = await asyncio.gather(
            *(_resolve_row_url(client, url, timeout_s) for _, url in targets),
            return_exceptions=True,
        )
    failed = 0
    for (idx, original), source in zip(targets, resolved):
        row = results[idx]
        if not isinstance(source, str) or not source.strip() or source == original:
            row["url_unresolved"] = True
            failed += 1
            continue
        row["redirect_url"] = original
        row["url"] = str(source).strip()
        for card in cards:
            if card.get("url") == original:
                card["url"] = row["url"]
                break
    if failed:
        logger.info(
            "Grounding redirect resolution failed for %d/%d link(s)",
            failed,
            len(targets),
        )
