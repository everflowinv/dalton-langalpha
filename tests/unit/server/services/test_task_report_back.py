"""A subagent's report-back: the executor re-asks its verdict before each POST.

The 2f1387ec sequence: TaskOutput delivered a worker_lost run's result (and
stamped ``result_delivered_at``) while the report-back job was already past
its claim-time verdict, waiting out a busy thread — its POST then announced a
failure the agent had already read. The pipeline rechecks, as the command
pipeline does, so a seen result never opens a turn.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.report_back import subagent as sub


_THREAD = "11111111-1111-1111-1111-111111111111"
_T0 = datetime(2026, 11, 2, 15, 1, 45, tzinfo=timezone.utc)


def _job(**payload):
    return {
        "hook_outbox_id": "00000000-0000-0000-0000-0000000000bb",
        "hook_type": "task_report_back",
        "conversation_thread_id": _THREAD,
        "attempts": 1,
        "payload": {
            "task_id": "lZIfbQ",
            "display_id": "Task-LZIfbQ",
            "task_run_id": "tr-1",
            "final_status": "error",
            "subagent_type": "equity-analyst",
            "description": "Value NVDA",
            "failure_error": "worker_lost: no live executor holds this task's "
            "namespace fence",
            "user_id": "u-1",
            "workspace_id": "ws-1",
            **payload,
        },
    }


def _run_row(delivered: bool):
    return {
        "task_run_id": "tr-1",
        "thread_id": _THREAD,
        "task_id": "lZIfbQ",
        "status": "error",
        "parent_run_id": None,
        "result_delivered_at": _T0 if delivered else None,
    }


@contextmanager
def _patches(*ps):
    with ExitStack() as stack:
        yield [stack.enter_context(p) for p in ps]


def _idle_thread_env(post, order=None):
    """The edges of a claim on an idle thread; ``order`` records the run-id
    save (a dispatched turn is the one thing it must stay empty of)."""
    order = order if order is not None else []
    return _patches(
        patch(
            "src.server.database.runs.lifecycle.get_latest_attempt",
            new=AsyncMock(
                return_value={"status": "completed", "conversation_response_id": "r0"}
            ),
        ),
        patch("src.server.services.report_back.notify_turn.post_notification_turn", new=post),
        patch(
            "src.server.database.runs.outbox.merge_job_payload",
            new=AsyncMock(side_effect=lambda *a: order.append("merge")),
        ),
        patch(
            "src.server.services.report_back.notify_turn.await_run_terminal",
            new=AsyncMock(return_value="terminal"),
        ),
        patch("src.server.services.report_back.handoff.wake.publish_wake", new=AsyncMock()),
        patch(
            "src.server.services.report_back.subagent.publish_cleared_wake", new=AsyncMock()
        ),
    )


def test_the_pipeline_reasks_before_each_post():
    assert sub.TASK_PIPELINE.recheck is True


@pytest.mark.asyncio
async def test_a_result_delivered_while_the_post_waited_opens_no_turn():
    """TaskOutput stamped the run delivered while the job waited out a busy
    thread; the POST asks again before it goes and drops the job."""
    reads = AsyncMock(side_effect=[_run_row(delivered=False), _run_row(delivered=True)])

    async def post(**kw):
        return ("dispatched", "run-9") if await kw["still_due"]() else ("superseded", None)

    order: list[str] = []
    with patch(
        "src.server.database.runs.subagent_runs.get_task_run", new=reads
    ), _idle_thread_env(post, order):
        await sub.execute_task_report_back(_job())

    assert reads.await_count == 2  # claim-time owed, re-ask no longer owed
    assert order == []  # no run id saved: no turn was dispatched


@pytest.mark.asyncio
async def test_a_result_still_owed_at_the_reask_opens_its_turn():
    reads = AsyncMock(return_value=_run_row(delivered=False))

    async def post(**kw):
        return ("dispatched", "run-9") if await kw["still_due"]() else ("superseded", None)

    order: list[str] = []
    with patch(
        "src.server.database.runs.subagent_runs.get_task_run", new=reads
    ), _idle_thread_env(post, order):
        await sub.execute_task_report_back(_job())

    assert reads.await_count == 2  # claim-time owed, re-ask still owed
    assert order == ["merge"]  # the turn was dispatched and its run id saved
