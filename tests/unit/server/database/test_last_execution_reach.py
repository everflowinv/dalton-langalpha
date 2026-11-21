"""Computer tokens limit each last-execution probe and history page before LIMIT."""

from contextlib import asynccontextmanager, contextmanager
import sqlite3
from unittest.mock import MagicMock, create_autospec, patch

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
    marker = f"SELECT {alias}.automation_execution_id"
    first = sql.index(marker)
    start = sql.rindex("CASE", 0, first)
    second = sql.index(marker, first + len(marker))
    end = sql.index("END", second) + len("END")
    return sql[start:end]


def _assert_reached_probe(sql: str, alias: str, outer: str) -> None:
    window = _probe(sql, alias)
    assert f"task_ws.workspace_id = {outer}.workspace_id" in window
    assert "task_ws.workspace_id <> %s::uuid" in window
    assert "task_ws.computer_id IS NOT NULL" in window
    assert "task_ws.computer_id = %s::uuid" in window
    assert f"task_ws.user_id = {outer}.user_id" in window
    assert "thread_ws.workspace_id <> %s::uuid" in window
    assert "thread_ws.computer_id IS NOT NULL" in window
    assert "thread_ws.computer_id = %s::uuid" in window
    assert f"thread_ws.user_id = {outer}.user_id" in window
    assert f"{alias}.conversation_thread_id IS NOT NULL" in window
    assert "execution_workspace" not in window
    when_block = window[window.index("WHEN") : window.index("THEN")]
    assert " OR " not in when_block
    assert window.index("task_ws") < window.index("THEN")
    assert window.index("thread_ws") < window.rindex("ORDER BY")


def test_computer_reach_is_inside_each_probe_and_is_not_remembered():
    plain = auto_db._last_execution_join("automations")
    reached = auto_db._last_execution_join("automations", computer_id="comp-b")
    again = auto_db._last_execution_join("automations")

    assert "task_ws" not in plain
    assert "thread_ws" not in plain
    assert "%s" not in plain
    assert again == plain
    assert "comp-b" not in reached
    assert reached.count("%s") == 12
    for alias in ("u", "c", "n"):
        _assert_reached_probe(reached, alias, "automations")
    assert reached.index(_probe(reached, "u")) < reached.index(_probe(reached, "c"))
    assert reached.index(_probe(reached, "c")) < reached.index(_probe(reached, "n"))

    updated = auto_db._last_execution_join("updated", computer_id="comp-b")
    _assert_reached_probe(updated, "u", "updated")
    untouched = auto_db._last_execution_join("updated")
    assert "task_ws" not in untouched
    assert "thread_ws" not in untouched
    assert "%s" not in untouched


@pytest.mark.asyncio
async def test_list_and_detail_bind_reach_before_the_existing_predicates(db):
    home_id = get_home_workspace_id("user-1")
    expected_reach = (home_id, "comp-b") * 6
    db.fetchone.return_value = {"cnt": 0}
    db.fetchall.return_value = []

    await auto_db.list_automations("user-1", status="active", limit=2, offset=4)
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "task_ws" not in count_sql
    assert "task_ws" not in page_sql
    assert count_params == ("user-1", "active")
    assert page_params == ("user-1", "active", 2, 4)

    db.execute.reset_mock()
    await auto_db.list_automations(
        "user-1", status="active", computer_id="comp-b", limit=2, offset=4
    )
    count_sql, count_params = db.execute.await_args_list[0].args
    page_sql, page_params = db.execute.await_args_list[1].args
    assert "task_ws" not in count_sql
    assert "automations.workspace_id <> %s::uuid" in page_sql
    for alias in ("u", "c", "n"):
        _assert_reached_probe(page_sql, alias, "automations")
    assert count_params == ("user-1", "active", home_id, "comp-b")
    assert page_params == (
        *expected_reach,
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
    assert "task_ws" not in sql
    assert params == ("auto-1", "user-1")

    db.execute.reset_mock()
    await auto_db.get_automation("auto-1", "user-1", computer_id="comp-b")
    sql, params = db.execute.await_args.args
    for alias in ("u", "c", "n"):
        _assert_reached_probe(sql, alias, "automations")
    assert params == (
        *expected_reach,
        "auto-1",
        "user-1",
    )


@pytest.mark.asyncio
async def test_scoped_list_keeps_owner_filters_and_reaches_last_execution(db):
    home_id = get_home_workspace_id("user-1")
    expected_reach = (home_id, "comp-b") * 6
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
    assert "task_ws" not in count_sql
    assert "automations.user_id = %s" in page_sql
    assert "automations.scope = %s" in page_sql
    assert "automations.workspace_id <> %s::uuid" in page_sql
    for alias in ("u", "c", "n"):
        _assert_reached_probe(page_sql, alias, "automations")
    assert count_params == ("user-1", "personal", home_id, "comp-b", "active")
    assert page_params == (
        *expected_reach,
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
    assert "task_ws" not in count_sql
    assert "task_ws" not in page_sql
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
    assert "task_ws" not in page_sql
    assert page_params == ("user-1", "research_tracking", 5, 1)


def _build_sqlite_history_world() -> tuple[sqlite3.Connection, dict[str, str]]:
    """In-memory SQLite tables and indexes mirroring PostgreSQL for reach SQL."""
    sq = sqlite3.connect(":memory:")
    sq.row_factory = sqlite3.Row
    sq.executescript(
        """
        CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            computer_id TEXT
        );
        CREATE INDEX idx_workspaces_computer
            ON workspaces(computer_id)
            WHERE computer_id IS NOT NULL;

        CREATE TABLE automations (
            automation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            workspace_id TEXT,
            scope TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX idx_automations_user_id
            ON automations(user_id);
        CREATE INDEX idx_automations_user_workspace
            ON automations(user_id, workspace_id)
            WHERE workspace_id IS NOT NULL;

        CREATE TABLE conversation_threads (
            conversation_thread_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL
        );
        CREATE INDEX idx_conversation_threads_workspace
            ON conversation_threads(workspace_id);

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
        CREATE INDEX idx_automation_executions_automation_created
            ON automation_executions(automation_id, created_at DESC);
        CREATE INDEX idx_automation_executions_thread_created
            ON automation_executions(conversation_thread_id, created_at DESC, automation_execution_id DESC)
            WHERE conversation_thread_id IS NOT NULL;
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
        "comp_other": "44444444-4444-4444-8444-444444444444",
        "ws_a1": "aaaaaaaa-1111-4111-8111-111111111111",
        "ws_a2": "aaaaaaaa-2222-4222-8222-222222222222",
        "ws_b1": "bbbbbbbb-1111-4111-8111-111111111111",
        "ws_c1": "cccccccc-1111-4111-8111-111111111111",
        "ws_other_a": "dddddddd-1111-4111-8111-111111111111",
        "ws_other_b": "dddddddd-2222-4222-8222-222222222222",
        "ws_other_priv": "dddddddd-3333-4333-8333-333333333333",
        "ws_company_b": "eeeeeeee-1111-4111-8111-111111111111",
        "auto_a": "a0000001-0000-4000-8000-000000000001",
        "auto_home": "a0000002-0000-4000-8000-000000000002",
        "auto_other_a": "a0000003-0000-4000-8000-000000000003",
        "auto_company_b": "a0000004-0000-4000-8000-000000000004",
        "thread_a1": "t0000001-0000-4000-8000-000000000001",
        "thread_a2": "t0000002-0000-4000-8000-000000000002",
        "thread_b1": "t0000003-0000-4000-8000-000000000003",
        "thread_home": "t0000004-0000-4000-8000-000000000004",
        "thread_other_a": "t0000005-0000-4000-8000-000000000005",
        "thread_other_b": "t0000006-0000-4000-8000-000000000006",
        "thread_company_b": "t0000007-0000-4000-8000-000000000007",
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
        "INSERT INTO workspaces (workspace_id, user_id, computer_id) VALUES (?, ?, ?)",
        [
            (ids["ws_a1"], owner, ids["comp_a"]),
            (ids["ws_a2"], owner, ids["comp_a"]),
            (ids["ws_b1"], owner, ids["comp_b"]),
            (ids["ws_c1"], owner, ids["comp_c"]),
            # Home sits on comp_b to verify Home exclusion never leaks onto comp_b.
            (ids["home_id"], owner, ids["comp_b"]),
            (ids["ws_other_a"], other, ids["comp_a"]),
            (ids["ws_other_b"], other, ids["comp_b"]),
            (ids["ws_other_priv"], other, ids["comp_other"]),
            (ids["ws_company_b"], COMPANY_SYSTEM_USER_ID, ids["comp_b"]),
        ],
    )
    sq.executemany(
        "INSERT INTO automations (automation_id, user_id, name, trigger_type, workspace_id, scope, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (ids["auto_a"], owner, "Task A", "cron", ids["ws_a1"], "personal", None),
            (ids["auto_home"], owner, "Home Task", "cron", ids["home_id"], "personal", None),
            (ids["auto_other_a"], other, "Other Task", "cron", ids["ws_other_a"], "personal", None),
            (
                ids["auto_company_b"],
                COMPANY_SYSTEM_USER_ID,
                "Company Task",
                "cron",
                ids["ws_company_b"],
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
            (ids["thread_other_a"], ids["ws_other_a"]),
            (ids["thread_other_b"], ids["ws_other_b"]),
            (ids["thread_company_b"], ids["ws_company_b"]),
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
            (ids["exec_other_user_a"], ids["auto_other_a"], "completed", ids["thread_other_a"], "2026-11-20T08:00:00Z"),
            (ids["exec_other_user_cross"], ids["auto_other_a"], "completed", ids["thread_other_b"], "2026-11-20T09:00:00Z"),
            (ids["exec_company_b"], ids["auto_company_b"], "completed", ids["thread_company_b"], "2026-11-20T10:00:00Z"),
        ],
    )
    sq.commit()
    return sq, ids


def _sqlite_async_db(sq: sqlite3.Connection):
    """Wrap SQLite inside ``create_autospec`` psycopg async connection/cursor doubles."""
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    last_rows: list[dict] = []
    executed: list[tuple[str, tuple]] = []

    async def _execute(sql: str, params=()):
        nonlocal last_rows
        sqlite_sql = sql.replace("::uuid", "").replace("%s", "?")
        executed.append((sqlite_sql, tuple(params)))
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
    return _get_db_connection, cur, executed


def _run_detail_probe_sqlite(
    sq: sqlite3.Connection, automation_id: str, user_id: str, computer_id: str
) -> str | None:
    probe_sql = auto_db._reached_execution_probe("automations", "n", "").replace(
        "::uuid", ""
    ).replace("%s", "?")
    home_id = get_home_workspace_id(user_id)
    row = sq.execute(
        f"SELECT {probe_sql} AS last_exec_id "
        "FROM automations "
        "WHERE automation_id = ? AND user_id = ? AND deleted_at IS NULL",
        (home_id, computer_id, home_id, computer_id, automation_id, user_id),
    ).fetchone()
    return None if row is None else row["last_exec_id"]


@pytest.mark.asyncio
async def test_list_executions_same_computer_returns_single_row():
    sq, ids = _build_sqlite_history_world()
    get_conn, _, _ = _sqlite_async_db(sq)
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
    get_conn, _, _ = _sqlite_async_db(sq)
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
    get_conn, _, _ = _sqlite_async_db(sq)
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
    get_conn, _, _ = _sqlite_async_db(sq)
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
async def test_reused_or_deleted_thread_never_leaks_foreign_workspace_id_in_list_or_detail():
    """Round 2 item 1: deleted thread UUID reused by another account must fall back to task workspace."""
    sq, ids = _build_sqlite_history_world()
    get_conn, _, _ = _sqlite_async_db(sq)
    reused_priv_thread = "t9999991-0000-4000-8000-000000000001"
    reused_comp_b_thread = "t9999992-0000-4000-8000-000000000002"
    deleted_thread = "t9999993-0000-4000-8000-000000000003"
    exec_reused_priv = "e9999991-0000-4000-8000-000000000001"
    exec_reused_comp_b = "e9999992-0000-4000-8000-000000000002"
    exec_deleted_thread = "e9999993-0000-4000-8000-000000000003"
    exec_home_reused_comp_b = "e9999994-0000-4000-8000-000000000004"

    try:
        # Another account creates threads reusing UUIDs previously referenced by owner's executions.
        sq.executemany(
            "INSERT INTO conversation_threads (conversation_thread_id, workspace_id) VALUES (?, ?)",
            [
                (reused_priv_thread, ids["ws_other_priv"]),
                (reused_comp_b_thread, ids["ws_other_b"]),
            ],
        )
        sq.executemany(
            "INSERT INTO automation_executions "
            "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (exec_reused_priv, ids["auto_a"], "completed", reused_priv_thread, "2026-11-21T01:00:00Z"),
                (exec_reused_comp_b, ids["auto_a"], "completed", reused_comp_b_thread, "2026-11-21T02:00:00Z"),
                (exec_deleted_thread, ids["auto_a"], "completed", deleted_thread, "2026-11-21T03:00:00Z"),
                (exec_home_reused_comp_b, ids["auto_home"], "completed", reused_comp_b_thread, "2026-11-21T04:00:00Z"),
            ],
        )
        sq.commit()

        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            all_owner, _ = await exec_db.list_executions(ids["owner"], limit=50)
            rows_a, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_a"], limit=50
            )
            rows_b, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=50
            )
            rows_other_comp, _ = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_other"], limit=50
            )

        all_by_id = {r["automation_execution_id"]: r for r in all_owner}
        a_by_id = {r["automation_execution_id"]: r for r in rows_a}
        b_ids = {r["automation_execution_id"] for r in rows_b}

        # Default (non-computer-scoped) list never returns the other account's private workspace_id.
        assert all_by_id[exec_reused_priv]["workspace_id"] == ids["ws_a1"]
        assert all_by_id[exec_reused_comp_b]["workspace_id"] == ids["ws_a1"]
        assert all_by_id[exec_deleted_thread]["workspace_id"] == ids["ws_a1"]
        assert all_by_id[exec_home_reused_comp_b]["workspace_id"] == ids["home_id"]
        # Legitimate caller-owned cross-computer thread still projects its thread workspace_id.
        assert all_by_id[ids["exec_cross_a_to_b"]]["workspace_id"] == ids["ws_b1"]

        # Computer-scoped list on comp_a matches via task side and falls back to task workspace_id.
        assert a_by_id[exec_reused_priv]["workspace_id"] == ids["ws_a1"]
        assert a_by_id[exec_reused_comp_b]["workspace_id"] == ids["ws_a1"]
        assert a_by_id[exec_deleted_thread]["workspace_id"] == ids["ws_a1"]
        assert a_by_id[ids["exec_cross_a_to_b"]]["workspace_id"] == ids["ws_b1"]

        # Reused foreign thread never grants reach onto comp_other or comp_b.
        assert rows_other_comp == []
        assert exec_reused_priv not in b_ids
        assert exec_reused_comp_b not in b_ids
        assert exec_deleted_thread not in b_ids
        assert exec_home_reused_comp_b not in b_ids

        # Detail probe applies the same thread ownership rule:
        # auto_a on comp_a picks its newest run (exec_deleted_thread, by task side);
        # comp_b ignores other's reused thread and still picks owner's exec_cross_a_to_b;
        # comp_other sees None; auto_home on comp_b ignores other's reused thread and picks exec_home_thread_b.
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_a"]) == exec_deleted_thread
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_other"]) is None
        assert _run_detail_probe_sqlite(sq, ids["auto_home"], ids["owner"], ids["comp_b"]) == ids["exec_home_thread_b"]
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_list_executions_computer_scope_pagination_boundaries_and_dedup():
    """Round 2 item 2: split task/thread UNION preserves exact ordering, dedup, and pagination boundaries."""
    sq, ids = _build_sqlite_history_world()
    get_conn, _, _ = _sqlite_async_db(sq)
    auto_b = "a0000005-0000-4000-8000-000000000005"
    ws_b2 = "bbbbbbbb-2222-4222-8222-222222222222"
    thread_b2 = "t0000008-0000-4000-8000-000000000008"

    try:
        # Remove existing comp_b runs so the expected timeline is explicit and self-contained.
        sq.execute("DELETE FROM automation_executions")
        sq.execute(
            "INSERT INTO workspaces (workspace_id, user_id, computer_id) VALUES (?, ?, ?)",
            (ws_b2, ids["owner"], ids["comp_b"]),
        )
        sq.execute(
            "INSERT INTO conversation_threads (conversation_thread_id, workspace_id) VALUES (?, ?)",
            (thread_b2, ws_b2),
        )
        sq.execute(
            "INSERT INTO automations (automation_id, user_id, name, trigger_type, workspace_id, scope, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (auto_b, ids["owner"], "Task B", "cron", ids["ws_b1"], "personal", None),
        )
        # 7 distinct comp_b executions mixing task-only, thread-only, both-sides, and timestamp ties,
        # plus distractor executions strictly on comp_a.
        timeline = [
            # Tied newest timestamp across thread-only, both-sides, and task-only:
            ("e1000007-0000-4000-8000-000000000007", ids["auto_a"], ids["thread_b1"], "2026-11-21T10:00:00Z"),
            ("e1000006-0000-4000-8000-000000000006", auto_b, ids["thread_b1"], "2026-11-21T10:00:00Z"),
            ("e1000005-0000-4000-8000-000000000005", auto_b, None, "2026-11-21T10:00:00Z"),
            # Strictly decreasing timestamps:
            ("e1000004-0000-4000-8000-000000000004", auto_b, thread_b2, "2026-11-21T09:00:00Z"),
            ("e1000003-0000-4000-8000-000000000003", ids["auto_home"], ids["thread_b1"], "2026-11-21T08:00:00Z"),
            ("e1000002-0000-4000-8000-000000000002", auto_b, ids["thread_a1"], "2026-11-21T07:00:00Z"),
            ("e1000001-0000-4000-8000-000000000001", ids["auto_a"], thread_b2, "2026-11-21T06:00:00Z"),
        ]
        distractors = [
            (f"e20000{i:02d}-0000-4000-8000-0000000000{i:02d}", ids["auto_a"], ids["thread_a1"], f"2026-11-21T11:{i:02d}:00Z")
            for i in range(10)
        ]
        sq.executemany(
            "INSERT INTO automation_executions "
            "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, 'completed', ?, ?)",
            timeline + distractors,
        )
        sq.commit()

        expected_order = [row[0] for row in timeline]

        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            full_rows, full_more = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=20, offset=0
            )
            assert [r["automation_execution_id"] for r in full_rows] == expected_order
            assert full_more is False

            # Walk every page of size 2.
            collected: list[str] = []
            pages_more: list[bool] = []
            for offset in (0, 2, 4, 6):
                page, has_more = await exec_db.list_executions(
                    ids["owner"], computer_id=ids["comp_b"], limit=2, offset=offset
                )
                collected.extend(r["automation_execution_id"] for r in page)
                pages_more.append(has_more)
            assert collected == expected_order
            assert pages_more == [True, True, True, False]

            # Exact boundary: remaining == limit -> has_more must be False.
            exact_end, exact_more = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=2, offset=5
            )
            assert [r["automation_execution_id"] for r in exact_end] == expected_order[5:]
            assert exact_more is False

            # One before exact boundary -> has_more must be True.
            before_end, before_more = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_b"], limit=2, offset=4
            )
            assert [r["automation_execution_id"] for r in before_end] == expected_order[4:6]
            assert before_more is True

            # At and past end -> empty with has_more False.
            for past_offset in (7, 15):
                past_rows, past_more = await exec_db.list_executions(
                    ids["owner"], computer_id=ids["comp_b"], limit=2, offset=past_offset
                )
                assert past_rows == []
                assert past_more is False
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_computer_scoped_queries_narrow_by_target_workspace_without_full_history_scan():
    """Round 2 item 2: prove via EXPLAIN QUERY PLAN and VM step counts that other-computer history is not scanned."""
    sq, ids = _build_sqlite_history_world()
    get_conn, _, executed = _sqlite_async_db(sq)

    def _insert_comp_a_history(start: int, count: int) -> None:
        sq.executemany(
            "INSERT INTO automation_executions "
            "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, 'completed', ?, ?)",
            [
                (
                    f"f0000000-0000-4000-8000-{i:012d}",
                    ids["auto_a"],
                    ids["thread_a1"],
                    f"2026-10-{(i % 28) + 1:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z",
                )
                for i in range(start, start + count)
            ],
        )
        sq.commit()

    def _measure_vm_steps(sql: str, params: tuple) -> int:
        steps = 0

        def _count() -> int:
            nonlocal steps
            steps += 1
            return 0

        sq.set_progress_handler(_count, 1)
        try:
            sq.execute(sql, params).fetchall()
        finally:
            sq.set_progress_handler(None, 0)
        return steps

    try:
        # Seed a single thread on comp_c (with 1 cross-computer execution) so both branches are active.
        thread_c1 = "t0000099-0000-4000-8000-000000000099"
        exec_cross_a_to_c = "e0000099-0000-4000-8000-000000000099"
        sq.execute(
            "INSERT INTO conversation_threads (conversation_thread_id, workspace_id) VALUES (?, ?)",
            (thread_c1, ids["ws_c1"]),
        )
        sq.execute(
            "INSERT INTO automation_executions "
            "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, 'completed', ?, '2026-11-20T11:00:00Z')",
            (exec_cross_a_to_c, ids["auto_a"], thread_c1),
        )
        _insert_comp_a_history(0, 100)
        sq.execute("ANALYZE")

        # Capture the exact SQL and params emitted by list_executions for comp_c.
        with patch("src.server.database.automation_executions.get_db_connection", new=get_conn):
            rows_small, more_small = await exec_db.list_executions(
                ids["owner"], computer_id=ids["comp_c"], limit=20, offset=0
            )
        assert [r["automation_execution_id"] for r in rows_small] == [exec_cross_a_to_c]
        assert more_small is False
        list_sql, list_params = executed[-1]

        # 1. EXPLAIN QUERY PLAN for list_executions must narrow via indexes with zero full table scans on automation_executions.
        list_plan_lines = [
            row["detail"]
            for row in sq.execute(f"EXPLAIN QUERY PLAN {list_sql}", list_params).fetchall()
        ]
        list_plan_text = "\n".join(list_plan_lines)
        assert "SCAN e" not in list_plan_text, list_plan_text
        assert "SCAN automation_executions" not in list_plan_text, list_plan_text
        assert "idx_workspaces_computer" in list_plan_text, list_plan_text
        assert "idx_automation_executions_automation_created" in list_plan_text, list_plan_text
        assert "idx_automation_executions_thread_created" in list_plan_text, list_plan_text

        # 2. EXPLAIN QUERY PLAN for detail _reached_execution_probe on comp_c (thread-side branch).
        probe_sql = auto_db._reached_execution_probe("automations", "n", "").replace(
            "::uuid", ""
        ).replace("%s", "?")
        detail_sql = (
            f"SELECT {probe_sql} AS last_exec_id "
            "FROM automations "
            "WHERE automation_id = ? AND user_id = ? AND deleted_at IS NULL"
        )
        detail_params = (
            ids["home_id"],
            ids["comp_c"],
            ids["home_id"],
            ids["comp_c"],
            ids["auto_a"],
            ids["owner"],
        )
        detail_plan_lines = [
            row["detail"]
            for row in sq.execute(f"EXPLAIN QUERY PLAN {detail_sql}", detail_params).fetchall()
        ]
        detail_plan_text = "\n".join(detail_plan_lines)
        assert "SCAN n" not in detail_plan_text, detail_plan_text
        assert "SCAN automation_executions" not in detail_plan_text, detail_plan_text
        assert "idx_automation_executions_thread_created" in detail_plan_text, detail_plan_text

        # 3. VM step count stays O(1) as comp_a history grows 100x (100 -> 10,000 rows).
        list_steps_100 = _measure_vm_steps(list_sql, list_params)
        detail_steps_100 = _measure_vm_steps(detail_sql, detail_params)

        _insert_comp_a_history(100, 9900)
        sq.execute("ANALYZE")

        list_steps_10k = _measure_vm_steps(list_sql, list_params)
        detail_steps_10k = _measure_vm_steps(detail_sql, detail_params)

        assert list_steps_10k < 2000, f"expected O(1) VM steps, got {list_steps_10k}"
        assert detail_steps_10k < 2000, f"expected O(1) VM steps, got {detail_steps_10k}"
        assert list_steps_10k <= list_steps_100 * 2, (list_steps_100, list_steps_10k)
        assert detail_steps_10k <= detail_steps_100 * 2, (detail_steps_100, detail_steps_10k)
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_detail_last_execution_probe_uses_same_computer_ownership_rules():
    """Verify ``get_automation(..., computer_id=...)`` and its probe SQL apply task/thread reach without OR."""
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
        # Cross-computer (task on A, thread on B): visible from comp_b detail.
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]
        # Comp A sees its newest execution (exec_task_a_thread_home at 05:00); comp C sees None.
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_task_a_thread_home"]
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_c"]) is None

        # When the newest run on auto_a is the cross-computer run (delete newer rows), BOTH A and B pick it.
        sq.execute(
            "DELETE FROM automation_executions WHERE automation_execution_id IN (?, ?)",
            (ids["exec_no_thread_a"], ids["exec_task_a_thread_home"]),
        )
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_cross_a_to_b"]
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]

        # No-thread execution belongs by task workspace: re-insert exec_no_thread_a as newest -> A sees it, B still sees cross-computer run only.
        sq.execute(
            "INSERT INTO automation_executions (automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
            "VALUES (?, ?, 'completed', NULL, '2026-11-20T12:00:00Z')",
            (ids["exec_no_thread_a"], ids["auto_a"]),
        )
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_a"]) == ids["exec_no_thread_a"]
        assert _run_detail_probe_sqlite(sq, ids["auto_a"], ids["owner"], ids["comp_b"]) == ids["exec_cross_a_to_b"]

        # Unauthorized caller cannot read another user's or company automation detail.
        assert _run_detail_probe_sqlite(sq, ids["auto_other_a"], ids["owner"], ids["comp_a"]) is None
        assert _run_detail_probe_sqlite(sq, ids["auto_other_a"], ids["owner"], ids["comp_b"]) is None
        assert _run_detail_probe_sqlite(sq, ids["auto_company_b"], ids["owner"], ids["comp_b"]) is None

        # Home task with thread on B is reachable from B only; Home no-thread run is excluded on B when thread_b run is removed.
        assert _run_detail_probe_sqlite(sq, ids["auto_home"], ids["owner"], ids["comp_b"]) == ids["exec_home_thread_b"]
        sq.execute(
            "DELETE FROM automation_executions WHERE automation_execution_id = ?",
            (ids["exec_home_thread_b"],),
        )
        assert _run_detail_probe_sqlite(sq, ids["auto_home"], ids["owner"], ids["comp_b"]) is None
    finally:
        sq.close()


def test_d042_migration_creates_and_drops_execution_thread_and_automation_workspace_indexes():
    """Verify d042 migration chain and CONCURRENTLY index DDL."""
    from src.dalton.migrations.versions import (
        d042_automation_executions_thread_index as d042,
    )

    assert d042.revision == "d042"
    assert d042.down_revision == "d041"

    executed_sql: list[str] = []

    class _FakeResult:
        def fetchone(self):
            return None

    class _FakeConn:
        def execute(self, stmt, _params=None):
            executed_sql.append(str(stmt))
            return _FakeResult()

    @contextmanager
    def _autocommit_block():
        yield

    fake_ctx = MagicMock()
    fake_ctx.autocommit_block.side_effect = _autocommit_block
    fake_conn = _FakeConn()

    with (
        patch.object(d042.op, "get_context", return_value=fake_ctx),
        patch.object(d042.op, "get_bind", return_value=fake_conn),
    ):
        d042.upgrade()
        d042.downgrade()

    joined = "\n".join(executed_sql)
    assert f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {d042.EXECUTION_THREAD_INDEX}" in joined
    assert "ON automation_executions (conversation_thread_id, created_at DESC, automation_execution_id DESC)" in joined
    assert f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {d042.AUTOMATION_USER_WORKSPACE_INDEX}" in joined
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {d042.EXECUTION_THREAD_INDEX}" in joined
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {d042.AUTOMATION_USER_WORKSPACE_INDEX}" in joined
