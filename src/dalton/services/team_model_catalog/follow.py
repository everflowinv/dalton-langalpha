"""Background catalog prewarm follower (Redis Pub/Sub + 60s PG poll) & cache invalidation.

Mirrors ``src/server/services/setup_settings/follow.py`` while keeping PostgreSQL as
the authoritative revision source: Redis pub/sub and the 60s timer only monotonically
prewarm ``_COMPILED_BY_REVISION`` in the background.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from src.dalton.services.team_model_catalog.constants import CATALOG_CHANGED_CHANNEL
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

_TICK_SECONDS = 60.0
_POLL_SECONDS = 5.0
_RETRY_SECONDS = 10.0

_task: asyncio.Task[None] | None = None


async def publish_catalog_changed(revision: int) -> None:
    """Publish a best-effort Redis Pub/Sub prewarm nudge after committing ``revision``."""
    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        return
    try:
        await cache.client.publish(CATALOG_CHANGED_CHANNEL, str(int(revision)))
    except Exception:
        logger.debug("[team_model_catalog] changed nudge not published", exc_info=True)


async def invalidate_catalog_redis_caches(
    *,
    actor_user_id: str | None = None,
    compiled: Any = None,
) -> None:
    """Invalidate Redis ``available_models`` caches for team holder + actor and gateway catalogs (§2.3 #4)."""
    from src.dalton.credentials import get_team_credential_holder_id_async
    from src.server.services.llm.provider_models.service import invalidate_available_models

    target_users: set[str] = set()
    try:
        holder_id = await get_team_credential_holder_id_async()
        if holder_id:
            target_users.add(holder_id)
    except Exception:
        pass
    if actor_user_id and str(actor_user_id).strip():
        target_users.add(str(actor_user_id).strip())

    for uid in target_users:
        try:
            await invalidate_available_models(uid)
        except Exception:
            logger.debug("[team_model_catalog] invalidate_available_models failed for %s", uid)

    # Clear gateway model_catalog:{url_key}:{provider} keys if any
    cache = get_cache_client()
    client = cache.client if cache.enabled and cache.client else None
    if client is not None and compiled is not None:
        mc = getattr(compiled, "model_config", None)
        flat = getattr(mc, "flat_providers", None) or {}
        for info in flat.values():
            cfg = info.get("model_catalog") if isinstance(info, dict) else None
            if isinstance(cfg, dict) and cfg.get("url") and cfg.get("provider"):
                url_key = hashlib.sha256(str(cfg["url"]).encode()).hexdigest()[:16]
                redis_key = f"model_catalog:{url_key}:{cfg['provider']}"
                try:
                    await client.delete(redis_key)
                except Exception:
                    pass

    # Clear local worker process caches
    try:
        from src.dalton.services.openclaw_admin import _invalidate_local_worker_model_caches

        _invalidate_local_worker_model_caches()
    except Exception:
        pass


async def _prewarm_quietly(min_revision: int | None = None) -> None:
    from src.dalton.services.team_model_catalog.compiled import prewarm_compiled_catalog

    try:
        await prewarm_compiled_catalog(expected_min_revision=min_revision)
    except Exception as exc:
        logger.warning("[team_model_catalog] background prewarm failed: %s", type(exc).__name__)


async def _listen(client: Any) -> None:
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(CATALOG_CHANGED_CHANNEL)
        await _prewarm_quietly()
        read_at = time.monotonic()
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=_POLL_SECONDS
            )
            if message:
                min_rev: int | None = None
                try:
                    raw_data = message.get("data")
                    if raw_data is not None:
                        min_rev = int(str(raw_data).strip())
                except (ValueError, TypeError):
                    min_rev = None
                await _prewarm_quietly(min_revision=min_rev)
                read_at = time.monotonic()
            elif time.monotonic() - read_at >= _TICK_SECONDS:
                await _prewarm_quietly()
                read_at = time.monotonic()
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            pass


async def run_catalog_follow_loop() -> None:
    """Subscribe to ``dalton:team_model_catalog:changed`` with 60s DB poll fallback."""
    from src.server.services.workspace_status_pubsub import get_shared_pubsub_client

    while True:
        try:
            client = await get_shared_pubsub_client()
            if client is None:
                await asyncio.sleep(_TICK_SECONDS)
                await _prewarm_quietly()
                continue
            await _listen(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[team_model_catalog] follower listener error, retrying: %s",
                type(exc).__name__,
            )
            await _prewarm_quietly()
        await asyncio.sleep(_RETRY_SECONDS)


def start_following() -> asyncio.Task[None]:
    """Start the background prewarm loop task named ``dalton-team-model-catalog-follow``."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(
            run_catalog_follow_loop(),
            name="dalton-team-model-catalog-follow",
        )
    return _task


async def stop_following() -> None:
    """Cancel and await the background prewarm loop task."""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _should_stamp_catalog_revision_path(path: str) -> bool:
    if path == "/api/v1/users/me/preferences":
        return True
    return path.startswith("/api/v1/threads/") and path.endswith(
        ("/messages", "/messages/stream", "/retry")
    )


class CatalogRevisionHeaderMiddleware:
    """Pure-ASGI middleware emitting ``X-Dalton-Catalog-Revision`` on chat and preferences routes.

    Non-buffering so SSE ``StreamingResponse`` streams are never delayed; no-op when
    Dalton is disabled.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _should_stamp_catalog_revision_path(
            str(scope.get("path") or "")
        ):
            await self.app(scope, receive, send)
            return

        from src.dalton.settings import is_dalton_enabled

        if not is_dalton_enabled():
            await self.app(scope, receive, send)
            return

        from src.dalton.services.team_model_catalog.compiled import (
            get_compiled_catalog_async,
        )
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_REVISION_HEADER,
        )

        header_key = CATALOG_REVISION_HEADER.lower().encode("ascii")

        async def _send_with_revision(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                raw_headers = list(message.get("headers") or [])
                has_header = any(
                    isinstance(k, (bytes, bytearray)) and bytes(k).lower() == header_key
                    for k, _ in raw_headers
                )
                if not has_header:
                    rev_value: int | None = None
                    try:
                        compiled = await get_compiled_catalog_async()
                        rev_value = int(compiled.revision)
                    except Exception:
                        from src.llms.llm import LLM

                        raw_rev = getattr(LLM._model_config, "catalog_revision", None)
                        if isinstance(raw_rev, int):
                            rev_value = raw_rev
                    if rev_value is not None:
                        raw_headers.append(
                            (header_key, str(rev_value).encode("ascii"))
                        )
                        message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, receive, _send_with_revision)
