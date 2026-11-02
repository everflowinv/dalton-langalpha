"""Discovery of the Feishu chats the notify bot is in (notify-routes).

Read-only: the bridge's ``/feishu/chats`` answers with ``chat_id`` + ``name``
per chat. One process-local TTL cache serves it — the list is deployment-wide,
read-only, and has no consistency requirement, so per-worker copies under the
multi-worker server are acceptable (a divergent copy only means a briefly
stale group list on the settings page or in route validation). Only successful
lookups are cached; a failure retries on the next call.
"""

from __future__ import annotations

import logging
import time

from src.dalton.feishu import FeishuBridgeError, post_bridge_feishu

logger = logging.getLogger(__name__)

FEISHU_GROUPS_CACHE_TTL_S = 300.0
_groups_cache: tuple[float, list[dict[str, str]]] | None = None


class FeishuGroupsUnavailable(RuntimeError):
    """The bot's chat list could not be discovered (bridge or Feishu refused)."""


async def discover_feishu_notify_groups() -> list[dict[str, str]]:
    """The chats the bot is in, ``[{chat_id, name}]``, cached 5 minutes.

    Raises :class:`FeishuGroupsUnavailable` when the lookup fails; the caller
    decides whether that is a 503 (a write that needs the list) or a degrade.
    """
    global _groups_cache
    cached = _groups_cache
    if cached is not None and time.monotonic() - cached[0] < FEISHU_GROUPS_CACHE_TTL_S:
        return list(cached[1])

    try:
        res = await post_bridge_feishu("chats", {})
    except FeishuBridgeError as exc:
        logger.warning("Feishu group discovery failed: %s", type(exc).__name__)
        raise FeishuGroupsUnavailable(str(exc)) from exc
    raw_chats = res.get("chats") if isinstance(res, dict) else None
    if res.get("ok") is not True or not isinstance(raw_chats, list):
        raise FeishuGroupsUnavailable("feishu_chats_unavailable")

    groups: list[dict[str, str]] = []
    for item in raw_chats:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("chat_id") or "").strip()
        if chat_id:
            groups.append({"chat_id": chat_id, "name": str(item.get("name") or "")})
    _groups_cache = (time.monotonic(), groups)
    return list(groups)
