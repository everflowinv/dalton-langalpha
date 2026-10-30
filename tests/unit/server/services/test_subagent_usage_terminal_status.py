"""A subagent's usage row reports its task run's terminal state — R3a #4.

The 10/30 export: 136 task usage rows, every one `completed`, beside a
subagent ledger holding 37 `error` rows. The run's real spend is kept; only
the blanket `completed` goes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTask
from src.server.services.runs.subagent_usage import persist_subagent_usage

USAGE_MODULE = "src.server.services.persistence.usage"
STATUSES_MODULE = "src.server.database.runs.subagent_runs"

_RECORD = {
    "model_name": "grok-4.7",
    "usage": {"input_tokens": 100, "output_tokens": 50},
    "billing_type": "platform",
}


def _make_task(task_id: str = "task01", **kwargs) -> BackgroundTask:
    defaults = dict(
        tool_call_id=f"tc-{task_id}",
        task_id=task_id,
        description="d",
        prompt="p",
        subagent_type="general-purpose",
        agent_id=f"general-purpose:{task_id}",
        per_call_records=[dict(_RECORD)],
        tool_usage={},
        collector_response_id="resp-1",
        task_run_id="run-1",
    )
    defaults.update(kwargs)
    return BackgroundTask(**defaults)


async def _bill_with_ledger_status(ledger_statuses: dict[str, str]):
    """One billing pass with the ledger answering ``ledger_statuses``; the
    real usage service with only its persist seam autospec'd, so the numbers
    this test compares are the real calculation's."""
    from src.server.services.persistence.usage import UsagePersistenceService
    from unittest.mock import create_autospec

    service = UsagePersistenceService(
        thread_id="thread-1", workspace_id="ws-1", user_id="user-1"
    )
    service.persist_usage = create_autospec(service.persist_usage, return_value=True)

    async def _statuses(task_run_ids):
        return {rid: ledger_statuses[rid] for rid in task_run_ids if rid in ledger_statuses}

    with (
        patch(f"{USAGE_MODULE}.UsagePersistenceService", return_value=service),
        patch(f"{STATUSES_MODULE}.get_task_run_statuses", new=_statuses),
    ):
        await persist_subagent_usage(
            response_id="resp-1",
            tasks=[_make_task()],
            thread_id="thread-1",
            workspace_id="ws-1",
            user_id="user-1",
        )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger_status", "expected"),
    [
        ("error", "error"),
        ("cancelled", "cancelled"),
        ("interrupted", "interrupted"),
        ("completed", "completed"),
        # No ledger row (pre-ledger task) or a race with finalize: the old
        # default stands rather than inventing a verdict.
        ("in_progress", "completed"),
    ],
)
async def test_the_row_reports_the_task_runs_own_terminal_state(
    ledger_status, expected
):
    service = await _bill_with_ledger_status({"run-1": ledger_status})
    service.persist_usage.assert_awaited_once()
    kwargs = service.persist_usage.await_args.kwargs
    assert kwargs["status"] == expected
    assert kwargs["msg_type"] == "task"
    assert kwargs["settle_task_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_a_failed_run_keeps_the_usage_it_really_spent():
    """The numbers on the row are the run's real spend whatever its status:
    billing a failed run and a completed one over the same records writes
    byte-identical token_usage, and only the status differs."""
    failed = await _bill_with_ledger_status({"run-1": "error"})
    succeeded = await _bill_with_ledger_status({"run-1": "completed"})

    failed_usage = failed.persist_usage.await_args.kwargs
    ok_usage = succeeded.persist_usage.await_args.kwargs

    assert failed_usage["status"] == "error"
    assert ok_usage["status"] == "completed"
    # The token payload the row persists is the same calculation either way.
    assert (
        failed._token_usage["by_model"]["grok-4.7"]
        == succeeded._token_usage["by_model"]["grok-4.7"]
    )
