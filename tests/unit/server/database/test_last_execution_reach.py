"""Computer tokens limit each last-execution probe before LIMIT."""

from contextlib import asynccontextmanager
import sqlite3
from unittest.mock import create_autospec, patch

import psycopg
import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import company_automations as company_auto_db
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
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
    assert "execution_workspace.workspace_id <> %s::uuid" in window
    assert "execution_workspace.computer_id = %s::uuid" in window
    assert (
        f"execution_workspace.workspace_id = {outer}.workspace_id"
        " OR execution_workspace.workspace_id = reach_thread.workspace_id"
    ) in window
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


def _build_sqlite_history_world() -> tuple[sqlite3.Connection, dict[str, str]]:
    """In-memory SQLite tables mirroring the real Postgres schema for reach SQL."""
    sq = sqlite3.connect(":memory:")
    sq.row_factory = sqlite3.Row
    sq.executescript(
        """
        CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            computer_id TEXT
        );
        CREATE TABLE automations (
            automation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            workspace_id TEXT,
            scope TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE TABLE conversation_threads (
            conversation_thread_id TEXT PRIMARY KEY,
            workspace_id TEXT
        );
        CREATE TABLE automation_executions (
            automation_execution_id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            conversation_thread_id TEXT,
            scheduled_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            error_message TEXT,
            skip_reason TEXT,
            failure_reason TEXT,
            server_id TEXT,
            delivery_result TEXT,
            created_at TEXT NOT NULL,
            dismissed_at TEXT,
            wait_reason TEXT,
            result_excerpt TEXT
        );
        """
    )

    owner = "user-owner"
    other = "user-other"
    home_id = get_home_workspace_id(owner)
    ids = {
        "owner": owner,
        "other": other,
        "home_id": home_id,
        "comp_a": "11111111-1111-4111-8111-111111111111",
        "comp_b": "22222222-2222-4222-8222-222222222222",
        "comp_c": "33333333-3333-4333-8333-333333333333",
        "ws_a1": "aaaaaaaa-1111-4111-8111-111111111111",
        "ws_a2": "aaaaaaaa-2222-4222-8222-222222222222",
        "ws_b1": "bbbbbbbb-1111-4111-8111-111111111111",
        "ws_c1": "cccccccc-1111-4111-8111-111111111111",
        "auto_a": "a0000001-0000-4000-8000-000000000001",
        "auto_home": "a0000002-0000-4000-8000-000000000002",
        "auto_other_a": "a0000003-0000-4000-8000-000000000003",
        "auto_company_b": "a0000004-0000-4000-8000-000000000004",
        "thread_a1": "t0000001-0000-4000-8000-000000000001",
        "thread_a2": "t0000002-0000-4000-8000-000000000002",
        "thread_b1": "t0000003-0000-4000-8000-000000000003",
        "thread_home": "t0000004-0000-4000-8000-000000000004",
        "exec_same_ws": "e0000001-0000-4000-8000-000000000001",
        "exec_same_comp_diff_ws": "e0000002-0000-4000-8000-000000000002",
        "exec_cross_a_to_b": "e0000003-0000-4000-8000-000000000003",
        "exec_no_thread_a": "e0000004-0000-4000-8000-000000000004",
        "exec_task_a_thread_home": "e0000005-0000-4000-8000-000000000005",
        "exec_home_no_thread": "e0000006-0000-4000-8000-000000000006",
        "exec_home_thread_b": "e0000007-0000-4000-8000-000000000007",
        "exec_other_user_a": "e0000008-0000-4000-8000-000000000008",
        "exec_other_user_cross": "e0000009-0000-4000-8000-000000000009",
        "exec_company_b": "e0000010-0000-4000-8000-000000000010",
    }

    sq.executemany(
        "INSERT INTO workspaces (workspace_id, computer_id) VALUES (?, ?)",
        [
            (ids["ws_a1"], ids["comp_a"]),
            (ids["ws_a2"], ids["comp_a"]),
            (ids["ws_b1"], ids["comp_b"]),
            (ids["ws_c1"], ids["comp_c"]),
            # Home sits on comp_b to verify Home exclusion never leaks onto comp_b.
            (ids["home_id"], ids["comp_b"]),
        ],
    )
    sq.executemany(
        "INSERT INTO automations (automation_id, user_id, name, trigger_type, workspace_id, scope, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (ids["auto_a"], owner, "Task A", "cron", ids["ws_a1"], "personal", None),
            (ids["auto_home"], owner, "Home Task", "cron", ids["home_id"], "personal", None),
            (ids["auto_other_a"], other, "Other Task", "cron", ids["ws_a1"], "personal", None),
            (
                ids["auto_company_b"],
                COMPANY_SYSTEM_USER_ID,
                "Company Task",
                "cron",
                ids["ws_b1"],
                "company",
                None,
            ),
        ],
    )
    sq.executemany(
        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id) VALUES (?, ?)",
        [
            (ids["thread_a1"], ids["ws_a1"]),
            (ids["thread_a2"], ids["ws_a2"]),
            (ids["thread_b1"], ids["ws_b1"]),
            (ids["thread_home"], ids["home_id"]),
        ],
    )
    sq.executemany(
        "INSERT INTO automation_executions "
        "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (ids["exec_same_ws"], ids["auto_a"], "completed", ids["thread_a1"], "2026-11-20T01:00:00Z"),
            (ids["exec_same_comp_diff_ws"], ids["auto_a"], "completed", ids["thread_a2"], "2026-11-20T02:00:00Z"),
            (ids["exec_cross_a_to_b"], ids["auto_a"], "completed", ids["thread_b1"], "2026-11-20T03:00:00Z"),
            (ids["exec_no_thread_a"], ids["auto_a"], "completed", None, "2026-11-20T04:00:00Z"),
            (ids["exec_task_a_thread_home"], ids["auto_a"], "completed", ids["thread_home"], "2026-11-20T05:00:00Z"),
            (ids["exec_home_no_thread"], ids["auto_home"], "completed", None, "2026-11-20T06:00:00Z"),
            (ids["exec_home_thread_b"], ids["auto_home"], "completed", ids["thread_b1"], "2026-11-20T07:00:00Z"),
            (ids["exec_other_user_a"], ids["auto_other_a"], "completed", ids["thread_a1"], "2026-11-20T08:00:00Z"),
            (ids["exec_other_user_cross"], ids["auto_other_a"], "completed", ids["thread_b1"], "2026-11-20T09:00:00Z"),
            (ids["exec_company_b"], ids["auto_company_b"], "completed", ids["thread_b1"], "2026-11-20T10:00:00Z"),
        ],
    )
    sq.commit()
    return sq, ids


def _sqlite_async_db(sq: sqlite3.Connection):
    """Wrap SQLite inside ``create_autospec`` psycopg async connection/cursor doubles."""
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    last_rows: list[dict] = []

    async def _execute(sql: str, params=()):
        nonlocal last_rows
        sqlite_sql = sql.replace("::uuid", "").replace("%s", "?")
        sq_cur = sq.execute(sqlite_sql, tuple(params))
        last_rows = [dict(row) for row in sq_cur.fetchall()]

    async def _fetchall():
        return list(last_rows)

    async def _fetchone():
        return last_rows[0] if last_rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone

    @asynccontextmanager
    async def _cursor(**_kwargs):
        yield cur

    @asynccontextmanager
    async def _get_db_connection(existing=None):
        yield conn

    conn.cursor.side_effect = _cursor
    return _get_db_connection, cur


@pytest.mark.asyncio
async def test_list_executions_same_computer_returns_single_row():
    sq, ids = _build_sqlite_history_world()
    get_conn, _ = _sqlite_async_db(sq)
    try:
        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            rows_a, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_a"], limit=50
            )
            rows_b, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=50
            )

        ids_a = [r["automation_execution_id"] for r in rows_a]
        ids_b = [r["automation_execution_id"] for r in rows_b]
        assert ids_a.count(ids["exec_same_ws"]) == 1
        assert ids_a.count(ids["exec_same_comp_diff_ws"]) == 1
        assert ids["exec_same_ws"] not in ids_b
        assert ids["exec_same_comp_diff_ws"] not in ids_b
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_list_executions_cross_computer_visible_on_both_without_duplicates():
    sq, ids = _build_sqlite_history_world()
    get_conn, _ = _sqlite_async_db(sq)
    try:
        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            rows_a, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_a"], limit=50
            )
            rows_b, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=50
            )
            rows_c, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_c"], limit=50
            )
            single_b, _ = await exec_db.list_executions(
                ids["owner"],
                automation_id=ids["auto_a"],
                computer_id=ids["comp_b"],
                limit=50,
            )

        ids_a = [r["automation_execution_id"] for r in rows_a]
        ids_b = [r["automation_execution_id"] for r in rows_b]
        ids_c = [r["automation_execution_id"] for r in rows_c]
        assert ids_a.count(ids["exec_cross_a_to_b"]) == 1
        assert ids_b.count(ids["exec_cross_a_to_b"]) == 1
        assert len(ids_a) == len(set(ids_a))
        assert len(ids_b) == len(set(ids_b))
        assert ids_c == []
        assert [r["automation_execution_id"] for r in single_b] == [ids["exec_cross_a_to_b"]]
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_list_executions_unauthorized_and_home_records_excluded():
    sq, ids = _build_sqlite_history_world()
    get_conn, _ = _sqlite_async_db(sq)
    try:
        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            rows_a, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_a"], limit=50
            )
            rows_b, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=50
            )
            other_rows_b, _ = await exec_db.list_executions(
                ids["other"], computer_id=ids["comp_b"], limit=50
            )

        ids_a = {r["automation_execution_id"] for r in rows_a}
        ids_b = {r["automation_execution_id"] for r in rows_b}
        for forbidden in (
            ids["exec_other_user_a"],
            ids["exec_other_user_cross"],
            ids["exec_company_b"],
            ids["exec_home_no_thread"],
        ):
            assert forbidden not in ids_a
            assert forbidden not in ids_b
        # Task on comp_a with Home thread stays on comp_a and never leaks onto Home's computer (comp_b).
        assert ids["exec_task_a_thread_home"] in ids_a
        assert ids["exec_task_a_thread_home"] not in ids_b
        # Home task whose thread moved to comp_b appears on comp_b only.
        assert ids["exec_home_thread_b"] in ids_b
        assert ids["exec_home_thread_b"] not in ids_a
        # Other user only sees their own cross-computer run on comp_b, never owner's.
        assert [r["automation_execution_id"] for r in other_rows_b] == [
            ids["exec_other_user_cross"]
        ]
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_list_executions_without_thread_belongs_by_task_workspace():
    sq, ids = _build_sqlite_history_world()
    get_conn, _ = _sqlite_async_db(sq)
    try:
        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            rows_a, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_a"], limit=50
            )
            rows_b, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=50
            )

        ids_a = [r["automation_execution_id"] for r in rows_a]
        ids_b = [r["automation_execution_id"] for r in rows_b]
        assert ids_a.count(ids["exec_no_thread_a"]) == 1
        assert ids["exec_no_thread_a"] not in ids_b
        assert ids["exec_home_no_thread"] not in ids_a
        assert ids["exec_home_no_thread"] not in ids_b
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_detail_last_execution_probe_uses_same_computer_ownership_rules():
    """Verify ``get_automation(..., computer_id=...)`` and its probe SQL apply the OR rule."""
    sq, ids = _build_sqlite_history_world()
    try:
        # 1. Confirm get_automation embeds the probe and keeps owner scoping via autospec DB doubles.
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        cur.fetchone.return_value = None
        conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _cursor(**_kwargs):
            yield cur

        @asynccontextmanager
        async def _get_db(existing=None):
            yield conn

        conn.cursor.side_effect = _cursor
        with patch("src.server.database.automation.get_db_connection", new=_get_db):
            await auto_db.get_automation(
                ids["auto_a"], ids["owner"], computer_id=ids["comp_b"]
            )
        detail_sql, detail_params = cur.execute.await_args.args
        assert "WHERE automation_id = %s AND user_id = %s AND deleted_at IS NULL" in detail_sql
        for alias in ("u", "c", "n"):
            _assert_reached_probe(detail_sql, alias, "automations")
        assert detail_params == (
            *auto_db._last_execution_computer_params(ids["owner"], ids["comp_b"]),
            ids["auto_a"],
            ids["owner"],
        )

        # 2. Execute the real generated probe SQL in SQLite for each scenario.
        probe_sql = auto_db._reached_execution_probe("automations", "n", "").replace(
            "::uuid", ""
        ).replace("%s", "?")

        def _detail_last_exec(automation_id: str, user_id: str, computer_id: str) -> str | None:
            home_id = get_home_workspace_id(user_id)
            row = sq.execute(
                f"SELECT {probe_sql} AS last_exec_id "
                "FROM automations "
                "WHERE automation_id = ? AND user_id = ? AND deleted_at IS NULL",
                (home_id, computer_id, automation_id, user_id),
            ).fetchone()
            return None if row is None else row["last_exec_id"]

        # Cross-computer (task on A, thread on B): visible from comp_b detail.
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]
        # Comp A sees its newest execution (exec_task_a_thread_home at 05:00); comp C sees None.
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_task_a_thread_home"]
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_c"]) is None

        # When the newest run on auto_a is the cross-computer run (delete newer rows), BOTH A and B pick it.
        sq.execute(
            "DELETE FROM automation_executions WHERE automation_execution_id IN (?, ?)",
            (ids["exec_no_thread_a"], ids["exec_task_a_thread_home"]),
        )
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_cross_a_to_b"]
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]

        # No-thread execution belongs by task workspace: re-insert exec_no_thread_a as newest -> A sees it, B still sees cross-computer run only.
        sq.execute(
            "INSERT INTO automation_executions (automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, 'completed', NULL, '2026-11-20T12:00:00Z')",
            (ids["exec_no_thread_a"], ids["auto_a"]),
        )
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_no_thread_a"]
        assert _detail_last_exec(ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]

        # Unauthorized caller cannot read another user's or company automation detail.
        assert _detail_last_exec(ids["auto_other_a"], ids["owner"], ids["comp_a"]) is None
        assert _detail_last_exec(ids["auto_other_a"], ids["owner"], ids["comp_b"]) is None
        assert _detail_last_exec(ids["auto_company_b"], ids["owner"], ids["comp_b"]) is None

        # Home task with thread on B is reachable from B only; Home no-thread run is excluded on B when thread_b run is removed.
        assert _detail_last_exec(ids["auto_home"], ids["owner"], ids["comp_b"]) == ids["exec_home_thread_b"]
        sq.execute(
            "DELETE FROM automation_executions WHERE automation_execution_id = ?",
            (ids["exec_home_thread_b"],),
        )
        assert _detail_last_exec(ids["auto_home"], ids["owner"], ids["comp_b"]) is None
    finally:
        sq.close()
