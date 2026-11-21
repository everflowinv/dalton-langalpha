"""The run list pages by reading one row past the page, never by a count."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from src.server.database import automation_executions as exec_db
# DALTON-HOOK(company-automations): computer execution reads exclude Home and other computers
from src.server.database.home_workspace import get_home_workspace_id


@pytest.fixture
def db(mock_connection, mock_cursor):
    @asynccontextmanager
    async def _connection():
        yield mock_connection

    with patch("src.server.database.automation_executions.get_db_connection", new=_connection):
        yield mock_cursor


@pytest.mark.asyncio
@pytest.mark.parametrize(("fetched", "has_more"), [(3, True), (2, False), (0, False)])
async def test_a_page_reads_one_row_past_itself(db, fetched, has_more):
    db.fetchall.return_value = [{"automation_execution_id": f"run-{i}"} for i in range(fetched)]

    rows, more = await exec_db.list_executions("user-1", limit=2, offset=4)

    assert [r["automation_execution_id"] for r in rows] == [f"run-{i}" for i in range(min(fetched, 2))]
    assert more is has_more
    db.execute.assert_awaited_once()
    sql, params = db.execute.await_args.args
    assert "COUNT" not in sql.upper()
    assert "execution_workspace" not in sql
    assert params[-2:] == (3, 4)
    assert sql.index("WHERE") < sql.index("LIMIT")


@pytest.mark.asyncio
async def test_computer_token_filters_the_run_workspace_before_the_page(db):
    # DALTON-HOOK(company-automations): filter task and execution workspaces before pagination
    # DALTON-HOOK(history-computer-scope): match when either task or thread workspace belongs to computer_id
    db.fetchall.return_value = []
    home_id = get_home_workspace_id("user-1")

    await exec_db.list_executions("user-1", computer_id="comp-b", limit=2, offset=0)

    sql, params = db.execute.await_args.args
    assert "execution_workspace.workspace_id <> %s::uuid" in sql
    assert "execution_workspace.computer_id = %s::uuid" in sql
    assert (
        "execution_workspace.workspace_id = a.workspace_id"
        " OR execution_workspace.workspace_id = t.workspace_id"
    ) in sql
    assert sql.index("execution_workspace") < sql.index("LIMIT")
    assert params == ("user-1", home_id, "comp-b", 3, 0)


@pytest.mark.asyncio
async def test_a_wait_reason_narrows_before_the_page_is_cut(db):
    """Runs waiting for a computer must not push a thread's waiting runs off
    the page, so the reason is a condition of the query, not a filter after."""
    db.fetchall.return_value = []

    await exec_db.list_executions(
        "user-1", thread_id="t-1", status="waiting", wait_reason="thread_busy", limit=10
    )

    sql, params = db.execute.await_args.args
    assert "COALESCE(e.wait_reason, CASE WHEN e.status = 'waiting' THEN 'thread_busy' END) = %s" in sql
    assert "thread_busy" in params
