"""15-minute Redis cache for successful search results.

The backend runs as several workers. The cache lives in Redis, not in a
process-local dict, so a hit on one worker is a hit on the others. Errors are
not stored. A Redis miss or outage is a cache miss: the search still runs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from src.utils.cache.cache_keys import CacheKeyBuilder
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 15 * 60
_KEYS = CacheKeyBuilder("dalton:search")


def _json_param(value: Any) -> str:
    """One cache-key value. Lists stay JSON arrays, so boundaries survive."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def search_cache_key(provider: str, params: Mapping[str, Any]) -> str:
    """Key of ``provider`` plus a hash of the normalized parameters.

    Each value is deterministic JSON (sorted keys, compact separators). A
    one-item list whose text contains a comma does not collide with a
    two-item list, and list order is part of the key. ``None`` values are
    dropped.
    """
    normalized = {
        key: _json_param(value)
        for key, value in params.items()
        if value is not None
    }
    return _KEYS.build(provider, normalized, use_hash=True)


async def get_cached_search(key: str) -> tuple[Any, dict[str, Any]] | None:
    """The stored ``(content, artifact)``, or None on miss, error, or a bad row."""
    try:
        payload = await get_cache_client().get(key)
    except Exception as exc:
        logger.warning("search cache read failed: %s", type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    artifact = payload.get("artifact")
    if "content" not in payload or not isinstance(artifact, dict):
        return None
    if artifact.get("error"):
        return None
    return payload["content"], artifact


async def store_cached_search(key: str, content: Any, artifact: dict[str, Any]) -> None:
    """Store one successful result. An artifact that carries ``error`` is ignored."""
    if artifact.get("error"):
        return
    try:
        await get_cache_client().set(
            key,
            {"content": content, "artifact": artifact},
            ttl=CACHE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("search cache write failed: %s", type(exc).__name__)
