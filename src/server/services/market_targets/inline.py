"""A notice a live turn takes in, through the server's report inbox.

A notice's job waits in the outbox like a report owed to the thread, and
``SteeredReportInbox`` hands it to the thread's running turn before its next
model call, holds it, and closes it once that call has begun. These are the
two market-target steps of that road: what the turn is offered, and the
delivery stamp the confirm leaves on the target row, which is what tells a
later claim of the same notice that nothing is owed. The offer leaves that
stamp itself for a notice whose wake turn already shows it.
"""

from __future__ import annotations

import logging

from ptc_agent.agent.middleware.steering import SteeredReport
from src.server.services.report_back.notify_turn import NotOwed

logger = logging.getLogger(__name__)

_LOG = "[MarketTarget]"

# An expiring notice reads better with the price, but it is taken in just
# before a model call the turn is waiting on.
_QUOTE_TIMEOUT = 2.0


async def market_alert_offer(job: dict) -> SteeredReport | NotOwed | None:
    """The notice to hand the live turn, ``NotOwed`` to close its job, or
    None to leave it to its executor.

    A notice whose owner has market watch off is closed unread, as its
    executor would drop it: the thread API no longer shows the target. One
    whose wake was admitted is that turn's message already: it is stamped to
    that run and closed, as its executor's next claim would. Left open, a
    job held unstamped would be offered to every turn and taken by none.
    """
    from src.server.database import market_targets as mt_db
    from src.server.services.features import user_feature_enabled
    from src.server.services.market_targets.delivery import (
        NOTICES,
        earlier_wake,
        last_quote,
        owed,
        request_key,
    )
    from src.server.services.market_targets.message import (
        notice_payload,
        render_notice,
    )

    payload = job.get("payload") or {}
    job_id = str(job["hook_outbox_id"])
    target_id = str(payload.get("target_id") or "")
    notice = payload.get("notice")
    if not target_id or notice not in NOTICES:
        return None
    target = await mt_db.get_target(target_id)
    if not owed(target, notice):
        return NotOwed(f"{notice} notice of target {target_id} is not owed")
    user_id = str(payload.get("user_id") or target["user_id"])
    if not await user_feature_enabled(user_id, "market_watch"):
        return NotOwed(
            f"{notice} notice of target {target_id}: market watch is off for "
            f"its owner"
        )
    thread_id = str(target["conversation_thread_id"])
    earlier = await earlier_wake(request_key(job_id), thread_id)
    if earlier is not None:
        run_id = str(earlier["conversation_response_id"])
        if not await mt_db.mark_delivered(target_id, notice, run_id):
            logger.info(
                f"{_LOG} {notice} notice of target {target_id} was already "
                f"stamped when its wake run {run_id} was found"
            )
        return NotOwed(
            f"{notice} notice of target {target_id} is shown by its wake run "
            f"{run_id}"
        )
    quote = None
    if notice == "expiring":
        quote = await last_quote(target, user_id, timeout=_QUOTE_TIMEOUT)
    return SteeredReport(
        job_id=job_id,
        content=render_notice(target, notice, quote),
        market_alert=notice_payload(target, notice),
    )


async def stamp_held_alerts(job_ids: list[str], run_id: str) -> None:
    """Stamp delivered to ``run_id`` each notice among ``job_ids`` it holds,
    ahead of the confirm that closes their jobs.

    A stamp that fails is logged and the job closed regardless: the notice
    is in the checkpointed message, and leaving the job open would show it
    again as a wake.
    """
    from src.server.database import market_targets as mt_db

    try:
        alerts = await mt_db.held_alerts(job_ids, run_id)
    except Exception:
        logger.warning(
            f"{_LOG} Reading the notices run {run_id} holds failed", exc_info=True
        )
        return
    for alert in alerts:
        try:
            if not await mt_db.mark_delivered(
                alert["target_id"], alert["notice"], run_id
            ):
                logger.info(
                    f"{_LOG} {alert['notice']} notice of target "
                    f"{alert['target_id']} was already stamped when run {run_id} "
                    f"took it in"
                )
        except Exception:
            logger.warning(
                f"{_LOG} Stamping the {alert['notice']} notice of target "
                f"{alert['target_id']} to run {run_id} failed",
                exc_info=True,
            )
