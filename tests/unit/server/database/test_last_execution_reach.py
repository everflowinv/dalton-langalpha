"""Computer tokens limit each last-execution probe before LIMIT."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import company_automations as company_auto_db
from src.server.database import automation as auto_db
from src.server.database.home_workspace import get_home_workspace_id


@pytest.fixture
def db(mock_connection, mock_cursor):
    @asynccontextmanager
    async def _connection(conn=None):
        yield mock_connection

    with (
        patch("src.server.database.automation.get_db_connection", new=_connection),
        patch(
            "src.dalton.database.company_automations.get_db_connection",
            new=_connection,
        ),
    ):
        yield mock_cursor


def _probe(sql: str, alias: str) -> str:
    start = sql.index(f"FROM automation_executions {alias}")
    return sql[start : sql.index("LIMIT 1", start)]


def _assert_reached_probe(sql: str, alias: str, outer: str) -> None:
    window = _probe(sql, alias)
    effective = f"COALESCE(reach_thread.workspace_id, {outer}.workspace_id)"
    assert effective in window
    assert "execution_workspace.workspace_id = " + effective in window
    assert "execution_workspace.computer_id = %s::uuid" in window
    assert window.index("execution_workspace") < window.index("ORDER BY")


def test_computer_reach_is_inside_each_probe_and_is_not_remembered():
    plain = auto_db._last_execution_join("automations")
    reached = auto_db._last_execution_join("automations", computer_id="comp-b")
    again = auto_db._last_execution_join("automations")

    assert "execution_workspace" not in plain
    assert "%s" not in plain
    assert again == plain
    assert "comp-b" not in reached
    assert reached.count("%s") == 6
    for alias in ("u", "c", "n"):
        _assert_reached_probe(reached, alias, "automations")
    assert reached.index(_probe(reached, "u")) < reached.index(_probe(reached, "c"))
    assert reached.index(_probe(reached, "c")) < reached.index(_probe(reached, "n"))

    updated = auto_db._last_execution_join("updated", computer_id="comp-b")
    _assert_reached_probe(updated, "u", "updated")
    untouched = auto_db._last_execution_join("updated")
    assert "execution_workspace" not in untouched
    assert "%s" not in untouched


@pytest.mark.asyncio
async def test_list_and_detail_bind_reach_before_the_existing_predicates(db):
    home_id = get_home_workspace_id("user-1")
    db.fetchone.return_value = {"cnt": 0}
    db.fetchall.return_value = []

    await auto_db.list_automations("user-1", status="active", limit=2, offset=4)
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "execution_workspace" not in count_sql
    assert "execution_workspace" not in page_sql
    assert count_params == ("user-1", "active")
    assert page_params == ("user-1", "active", 2, 4)

    db.execute.reset_mock()
    await auto_db.list_automations(
        "user-1", status="active", computer_id="comp-b", limit=2, offset=4
    )
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "execution_workspace" not in count_sql
    assert "automations.workspace_id <> %s::uuid" in page_sql
    for alias in ("u", "c", "n"):
        _assert_reached_probe(page_sql, alias, "automations")
    assert count_params == ("user-1", "active", home_id, "comp-b")
    assert page_params == (
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        "user-1",
        "active",
        home_id,
        "comp-b",
        2,
        4,
    )

    db.execute.reset_mock()
    db.fetchone.return_value = None
    await auto_db.get_automation("auto-1", "user-1")
    sql, params = db.execute.await_args.args
    assert "execution_workspace" not in sql
    assert params == ("auto-1", "user-1")

    db.execute.reset_mock()
    await auto_db.get_automation("auto-1", "user-1", computer_id="comp-b")
    sql, params = db.execute.await_args.args
    for alias in ("u", "c", "n"):
        _assert_reached_probe(sql, alias, "automations")
    assert params == (
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        "auto-1",
        "user-1",
    )


@pytest.mark.asyncio
async def test_scoped_list_keeps_owner_filters_and_reaches_last_execution(db):
    home_id = get_home_workspace_id("user-1")
    db.fetchone.return_value = {"cnt": 0}
    db.fetchall.return_value = []

    await company_auto_db.list_automations_for_scope_db(
        scope="personal",
        caller_user_id="user-1",
        status="active",
        computer_id="comp-b",
        limit=2,
        offset=4,
    )
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "execution_workspace" not in count_sql
    assert "automations.user_id = %s" in page_sql
    assert "automations.scope = %s" in page_sql
    assert "automations.workspace_id <> %s::uuid" in page_sql
    for alias in ("u", "c", "n"):
        _assert_reached_probe(page_sql, alias, "automations")
    assert count_params == ("user-1", "personal", home_id, "comp-b", "active")
    assert page_params == (
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        home_id,
        "comp-b",
        "user-1",
        "personal",
        home_id,
        "comp-b",
        "active",
        2,
        4,
    )

    db.execute.reset_mock()
    await company_auto_db.list_automations_for_scope_db(
        scope="company",
        caller_user_id="user-1",
        limit=2,
        offset=0,
    )
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "execution_workspace" not in count_sql
    assert "execution_workspace" not in page_sql
    assert "w.computer_id" not in page_sql
    assert count_params == (COMPANY_SYSTEM_USER_ID,)
    assert page_params == (COMPANY_SYSTEM_USER_ID, 2, 0)

    db.execute.reset_mock()
    await company_auto_db.list_automations_for_scope_db(
        scope="research_tracking",
        caller_user_id="user-1",
        limit=5,
        offset=1,
    )
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "execution_workspace" not in page_sql
    assert page_params == ("user-1", "research_tracking", 5, 1)
