"""Utility helpers for background subagent middleware."""

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Who sent the steers waiting, empty when none: truthy is "wake up".
MessageChecker = Callable[[], Awaitable[list[str]]]

#: The sender a checker names for a market target's alert waiting on the
#: thread, which nobody sent.
MARKET_ALERT_SENDER = "market_alert"


def config_own_run_id(config: dict | None) -> str | None:
    """The run stamped into this invocation's config metadata (v4 2.4c) —
    the same identity SteeringMiddleware filters deliveries by."""
    return ((config or {}).get("metadata") or {}).get("run_id")


async def build_message_checker(
    thread_id: str | None,
    own_run_id: str | None = None,
    *,
    market_alerts: bool = False,
) -> MessageChecker | None:
    """An async closure: who sent the steers waiting that this run would take
    in, empty when none.

    Mirrors what SteeringMiddleware takes (``pending_steer_senders`` shares its
    predicate): counting a steer this run cannot take would wake a wait or
    re-invoke the agent for a message that never arrives. A failed read
    counts as nothing waiting. None without a thread, so callers skip it.
    With ``market_alerts``, for a run whose steering takes reports in, a
    target's alert waiting with no steer counts as ``MARKET_ALERT_SENDER``:
    held until the tasks finish, it could arrive after the price moved on.
    """
    if not thread_id:
        return None

    async def checker() -> list[str]:
        from src.server.database.runs import outbox as outbox_db

        try:
            senders = await outbox_db.pending_steer_senders(thread_id, own_run_id)
            if senders or not market_alerts:
                return senders
            if await outbox_db.has_steerable_market_alert(thread_id):
                return [MARKET_ALERT_SENDER]
            return []
        except Exception:
            logger.warning(
                f"[Steering] Checking {thread_id} for steers failed", exc_info=True
            )
            return []

    return checker
