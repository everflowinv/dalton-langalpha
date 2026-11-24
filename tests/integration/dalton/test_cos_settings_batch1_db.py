"""Real PostgreSQL concurrency tests for Batch 1 managed automation row locking.

Run outside the sandbox against a temporary *_test PostgreSQL database:
  TEST_DB_NAME=langalpha_test uv run pytest -m integration \
      tests/integration/dalton/test_cos_settings_batch1_db.py -v
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
import uuid
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
from psycopg.rows import dict_row

from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.database import company_automations as company_automations_db
from src.dalton.services import automation_thread_model as atm_mod
from src.dalton.services import company_automations
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database import computer as computer_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database import workspace_folders as ws_folders_db
from src.server.database import workspace_move as ws_move_db
from src.server.database.computer import create_computer
from src.server.database.user import create_user
from src.server.database.workspace_move import (
    advance_workspace_move,
    commit_workspace_move,
    stage_workspace_move,
)
from src.server.database.workspace_names import HOME_FOLDER
from src.server.services.automations import lifecycle as lifecycle_mod
from src.server.services.llm import user_models
from src.server.services.workspace_move import claim_workspace_move

# Loaded before patched_get_db_connection sweeps sys.modules so every holder uses test_db_pool.
_POOL_HOLDERS = (
    schedule_mod,
    company_automations_db,
    atm_mod,
    auto_db,
    exec_db,
    computer_db,
    user_db,
    ws_db,
    ws_folders_db,
    ws_move_db,
)

# Must share the session event loop with ``test_db_pool`` (``scope="session"``);
# otherwise ``psycopg_pool``'s background workers live on the paused session loop and
# cannot grow the pool beyond ``min_size=1`` when a second concurrent task checks out a connection.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _wait_for_lock_waiter(
    test_db_uri: str,
    *,
    blocker_pid: int,
    waiter_task: asyncio.Task[Any],
    holder_task: asyncio.Task[Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Poll ``pg_stat_activity`` until ``waiter_task`` is blocked on a PostgreSQL Lock held by ``blocker_pid``."""
    deadline = time.monotonic() + timeout
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as probe_conn:
        async with probe_conn.cursor(row_factory=dict_row) as cur:
            while time.monotonic() < deadline:
                if waiter_task.done():
                    exc = waiter_task.exception()
                    if exc is not None:
                        raise AssertionError(
                            f"Waiter task raised before blocking on pid {blocker_pid}: {exc!r}"
                        ) from exc
                    raise AssertionError(
                        f"Waiter task completed without blocking on pid {blocker_pid}: "
                        f"{waiter_task.result()!r}"
                    )
                if holder_task is not None and holder_task.done():
                    exc = holder_task.exception()
                    if exc is not None:
                        raise AssertionError(
                            f"Holder task raised while holding lock (pid {blocker_pid}): {exc!r}"
                        ) from exc
                    raise AssertionError(
                        f"Holder task ended early before waiter blocked (pid {blocker_pid}): "
                        f"{holder_task.result()!r}"
                    )
                await cur.execute(
                    """
                    SELECT pid, state, wait_event_type, wait_event, query,
                           pg_blocking_pids(pid) AS blocking_pids
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND %(blocker_pid)s = ANY(pg_blocking_pids(pid))
                    LIMIT 1
                    """,
                    {"blocker_pid": blocker_pid},
                )
                row = await cur.fetchone()
                if row is not None:
                    return dict(row)
                await asyncio.sleep(0.05)
            await cur.execute(
                """
                SELECT pid, state, wait_event_type, wait_event,
                       pg_blocking_pids(pid) AS blocking_pids,
                       LEFT(query, 200) AS query
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                """
            )
            snapshot = [dict(r) for r in (await cur.fetchall() or ())]
    raise AssertionError(
        f"Expected another session blocked by pid {blocker_pid} within {timeout}s; "
        f"pg_stat_activity={snapshot!r}"
    )


async def _setup_user_and_two_computers(user_id: str) -> tuple[str, str, str]:
    await create_user(user_id=user_id, email=f"{user_id}@test.local", name="Analyst")
    c1 = await create_computer(
        user_id,
        kind="docker",
        name="Primary",
        is_primary=True,
        status="running",
        provider_ref=f"b1-c1-{uuid.uuid4()}",
    )
    c2 = await create_computer(
        user_id,
        kind="docker",
        name="Second",
        is_primary=False,
        status="running",
        provider_ref=f"b1-c2-{uuid.uuid4()}",
    )
    ws = await ws_db.create_workspace_on_computer(user_id, "NVDA", str(c1["computer_id"]))
    assert ws is not None
    return str(c1["computer_id"]), str(c2["computer_id"]), str(ws["workspace_id"])


def _stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lifecycle_mod,
        "get_configured_llm_models",
        create_autospec(
            lifecycle_mod.get_configured_llm_models,
            return_value={"anthropic": ["claude-sonnet-4-6"]},
        ),
    )

    async def _classify(
        uid: str, model_name: str, _pref_cache=None, ignore_custom_models: bool = False
    ):
        if model_name == "claude-sonnet-4-6":
            return user_models.ModelSource.SYSTEM, {
                "provider": "anthropic",
                "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
            }
        return user_models.ModelSource.UNKNOWN, {}

    monkeypatch.setattr(
        user_models,
        "classify_model",
        create_autospec(user_models.classify_model, side_effect=_classify),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)


async def test_automation_tx_for_share_blocks_concurrent_commit_workspace_move_until_commit(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While schedule_dalton_automation_helper holds FOR SHARE on computers + workspaces,
    a concurrent commit_workspace_move on another connection blocks on Lock until commit."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-lock-{uuid.uuid4().hex[:10]}"
    c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    locks_held = asyncio.Event()
    allow_commit = asyncio.Event()
    holder_pid_box: list[int] = []
    orig_create = auto_db.create_automation

    async def _paused_create(*args: Any, **kwargs: Any) -> Any:
        # Called inside the open transaction AFTER _lock_and_verify_workspace_placement took FOR SHARE
        locked_conn = kwargs.get("conn")
        assert locked_conn is not None, "create_automation must run on the locked transaction connection"
        holder_pid_box.append(int(locked_conn.info.backend_pid))
        locks_held.set()
        await asyncio.wait_for(allow_commit.wait(), timeout=15.0)
        return await orig_create(*args, **kwargs)

    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(orig_create, side_effect=_paused_create),
    )

    auto_task = asyncio.create_task(
        schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Monitor NVDA.",
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "high",
            },
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    commit_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(locks_held.wait(), timeout=5.0)

        # On a separate connection, insert a staged move row with valid runner_id and source_folder
        # so commit_workspace_move attempts FOR UPDATE OF w while auto_task holds FOR SHARE.
        move_id = str(uuid.uuid4())
        runner_id = str(uuid.uuid4())
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as aux_conn:
            async with aux_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO workspace_moves (
                        move_id, workspace_id, user_id, source_computer_id, target_computer_id,
                        phase, make_primary, runner_id, source_folder, leftovers_path,
                        staged_at, heartbeat_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'staged', false, %s, %s, %s, NOW(), NOW())
                    """,
                    (move_id, ws_id, user_id, c1_id, c2_id, runner_id, "NVDA", None),
                )

        commit_task = asyncio.create_task(commit_workspace_move(move_id, runner_id))
        await asyncio.sleep(0)
        # Confirm via pg_stat_activity / pg_blocking_pids that commit_workspace_move is blocked by auto_task's lock holder pid
        holder_pid = holder_pid_box[0]
        waiter = await _wait_for_lock_waiter(
            test_db_uri,
            blocker_pid=holder_pid,
            waiter_task=commit_task,
            holder_task=auto_task,
            timeout=5.0,
        )
        assert waiter["pid"] != holder_pid
        assert holder_pid in waiter["blocking_pids"]
        assert waiter["wait_event_type"] == "Lock"
        assert not commit_task.done(), "commit_workspace_move bypassed FOR SHARE lock held by automation transaction"

        allow_commit.set()
        auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
        assert auto_res["ok"] is True

        committed = await asyncio.wait_for(commit_task, timeout=5.0)
        assert committed is not None
        assert str(committed["phase"]) == "committed"
    finally:
        allow_commit.set()
        for pending in (auto_task, commit_task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (auto_task, commit_task) if t is not None),
            return_exceptions=True,
        )


async def test_concurrent_workspace_move_before_lock_refuses_automation_save_with_workspace_changed(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When another connection moves the workspace between pre-lock snapshot and lock acquisition,
    schedule_dalton_automation_helper aborts with workspace_changed and commits no row."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-drift-{uuid.uuid4().hex[:10]}"
    _c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    snapshot_taken = asyncio.Event()
    move_finished = asyncio.Event()
    orig_snapshot = schedule_mod._read_workspace_computer_snapshot

    async def _paused_snapshot(*args: Any, **kwargs: Any):
        snap = await orig_snapshot(*args, **kwargs)
        snapshot_taken.set()
        await asyncio.wait_for(move_finished.wait(), timeout=5.0)
        return snap

    monkeypatch.setattr(
        schedule_mod,
        "_read_workspace_computer_snapshot",
        create_autospec(orig_snapshot, side_effect=_paused_snapshot),
    )

    auto_task = asyncio.create_task(
        schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Monitor NVDA.",
                "llm_model": "claude-sonnet-4-6",
            },
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    await asyncio.wait_for(snapshot_taken.wait(), timeout=5.0)

    # Complete a real workspace move c1 -> c2 before auto_task enters its write transaction
    move = await claim_workspace_move(
        workspace_id=ws_id,
        user_id=user_id,
        target_computer_id=c2_id,
    )
    mid = str(move["move_id"])
    runner_id = str(move["runner_id"])
    source_folder = str(move.get("source_folder") or "NVDA")
    advanced = await advance_workspace_move(mid, runner_id, "backing_up")
    assert advanced is not None
    staged = await stage_workspace_move(
        mid, runner_id, source_folder=source_folder, leftovers_path=None
    )
    assert staged is not None
    committed = await commit_workspace_move(mid, runner_id)
    assert committed is not None
    move_finished.set()

    auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
    assert auto_res["ok"] is False
    assert auto_res["error"] == "workspace_changed"
    assert auto_res["message"] == "情况已变化，请重新发起"
    rows = await auto_db.list_all_automations(user_id)
    assert rows == []


async def test_waiting_automation_tx_observes_just_committed_workspace_move_after_for_share(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a concurrent transaction holds FOR UPDATE on workspaces and inserts a preparing move
    before committing, a waiting automation tx sees the move in its post-lock query and aborts."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-postlock-{uuid.uuid4().hex[:10]}"
    c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    ws_locked_by_move = asyncio.Event()
    release_move_tx = asyncio.Event()
    holder_pid_box: list[int] = []

    async def _holding_move_claim() -> None:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=False) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT workspace_id FROM workspaces WHERE workspace_id = %s FOR UPDATE",
                    (ws_id,),
                )
                holder_pid_box.append(int(conn.info.backend_pid))
                ws_locked_by_move.set()
                await asyncio.wait_for(release_move_tx.wait(), timeout=15.0)
                await cur.execute(
                    """
                    INSERT INTO workspace_moves (
                        move_id, workspace_id, user_id, source_computer_id, target_computer_id,
                        phase, make_primary, runner_id, source_folder, heartbeat_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'preparing', false, %s, %s, NOW())
                    """,
                    (str(uuid.uuid4()), ws_id, user_id, c1_id, c2_id, str(uuid.uuid4()), "NVDA"),
                )
            await conn.commit()

    move_holder = asyncio.create_task(_holding_move_claim())
    auto_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(ws_locked_by_move.wait(), timeout=5.0)

        auto_task = asyncio.create_task(
            schedule_mod.schedule_dalton_automation_helper(
                "dalton-nvda-watch",
                {
                    "trigger_type": "cron",
                    "cron_expression": "0 9 * * 1-5",
                    "instruction": "Monitor NVDA.",
                    "llm_model": "claude-sonnet-4-6",
                },
                user_id=user_id,
                workspace_id=ws_id,
            )
        )
        await asyncio.sleep(0)
        # Confirm via pg_stat_activity / pg_blocking_pids that auto_task is actively blocked by move_holder's pid
        holder_pid = holder_pid_box[0]
        waiter = await _wait_for_lock_waiter(
            test_db_uri,
            blocker_pid=holder_pid,
            waiter_task=auto_task,
            holder_task=move_holder,
            timeout=5.0,
        )
        assert waiter["pid"] != holder_pid
        assert holder_pid in waiter["blocking_pids"]
        assert waiter["wait_event_type"] == "Lock"
        assert not auto_task.done(), "automation tx should wait behind FOR UPDATE on workspaces"

        release_move_tx.set()
        await asyncio.wait_for(move_holder, timeout=5.0)

        auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
        assert auto_res["ok"] is False
        assert auto_res["error"] == "workspace_changed"
        assert auto_res["message"] == "情况已变化，请重新发起"
        rows = await auto_db.list_all_automations(user_id)
        assert rows == []
    finally:
        release_move_tx.set()
        for pending in (move_holder, auto_task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (move_holder, auto_task) if t is not None),
            return_exceptions=True,
        )


async def test_home_earnings_calendar_owner_vs_other_home_job_on_real_db(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real DB verification of Home FOR SHARE locking + dalton-earnings-calendar owner exception."""
    _stub_catalog(monkeypatch)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda _uid: False)

    user_id = f"b1-home-{uuid.uuid4().hex[:10]}"
    c1_id, _c2_id, _ws_id = await _setup_user_and_two_computers(user_id)
    home = await ws_db.get_or_create_home_workspace(user_id)
    assert home is not None
    await ws_db.bind_workspace_to_computer(
        str(home["workspace_id"]), c1_id, expected_computer_id=None, dir_name=HOME_FOLDER
    )

    ok_res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-earnings-calendar",
        {
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
            "instruction": "Refresh earnings calendar.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert ok_res["ok"] is True

    refused_res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-director-daily",
        {
            "trigger_type": "cron",
            "cron_expression": "30 7 * * 1-5",
            "instruction": "Daily brief.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert refused_res["ok"] is False
    assert refused_res["error"] == "forbidden_pm"


async def test_execution_creation_stamps_system_only_atomically_and_later_config_edits_do_not_change_it(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 real DB test: all execution-creation SQL paths stamp
    automation_executions.llm_model_system_only in the same transaction from the automation row,
    and subsequent automation config updates never mutate already-created executions."""
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    user_id = f"b1-exec-sys-{uuid.uuid4().hex[:10]}"
    _c1_id, _c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    now = datetime.now(UTC)
    due_auto = await auto_db.create_automation(
        user_id=user_id,
        name="due-sys-only",
        trigger_type="cron",
        cron_expression="0 9 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check NVDA.",
        workspace_id=ws_id,
        llm_model="claude-sonnet-4-6",
        next_run_at=now - timedelta(minutes=1),
        metadata={"dalton_managed": True, "llm_model_system_only": True},
    )
    due_auto_id = str(due_auto["automation_id"])

    price_auto = await auto_db.create_automation(
        user_id=user_id,
        name="price-sys-only",
        trigger_type="price",
        trigger_config={"symbol": "NVDA", "conditions": [{"type": "price_above", "value": 100}]},
        timezone="Asia/Shanghai",
        instruction="Price alert.",
        workspace_id=ws_id,
        llm_model="claude-sonnet-4-6",
        metadata={"dalton_managed": True, "llm_model_system_only": True},
    )
    price_auto_id = str(price_auto["automation_id"])

    # 1. claim_due_automations stamps llm_model_system_only=True in the same tx
    claimed = await auto_db.claim_due_automations(now, server_id="srv-r5", limit=10)
    matched_due = [r for r in claimed if str(r["automation_id"]) == due_auto_id]
    assert len(matched_due) == 1
    exec_claimed_id = str(matched_due[0]["_execution_id"])

    # 2. create_execution_for_live_parent stamps llm_model_system_only=True in the same tx
    exec_live_id, _parent = await exec_db.create_execution_for_live_parent(
        due_auto_id, user_id, now, "srv-r5"
    )

    # 3. create_execution stamps llm_model_system_only=True in the same tx
    exec_direct_id = await exec_db.create_execution(due_auto_id, now, "srv-r5")

    # 4. claim_price_firing stamps llm_model_system_only=True in the same tx
    exec_price_id = await auto_db.claim_price_firing(price_auto_id, "srv-r5")
    assert exec_price_id is not None

    async def _read_snapshot(eid: str) -> tuple[str | None, str | None, bool]:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT llm_model, reasoning_effort, llm_model_system_only
                    FROM automation_executions
                    WHERE automation_execution_id = %s
                    """,
                    (eid,),
                )
                row = await cur.fetchone()
                assert row is not None
                return (
                    row["llm_model"],
                    row["reasoning_effort"],
                    bool(row["llm_model_system_only"]),
                )

    for eid in (exec_claimed_id, exec_live_id, exec_direct_id, exec_price_id):
        assert await _read_snapshot(eid) == ("claude-sonnet-4-6", None, True)
        fetched = await atm_mod._get_execution_by_id(eid)
        assert fetched is not None
        assert fetched["llm_model"] == "claude-sonnet-4-6"
        assert fetched["reasoning_effort"] is None
        assert fetched["llm_model_system_only"] is True

    # begin_execution_if_not_deleted returns full snapshot from the execution row
    started = await exec_db.begin_execution_if_not_deleted(exec_live_id, started_at=now)
    assert started is not None
    assert started["llm_model"] == "claude-sonnet-4-6"
    assert started["reasoning_effort"] is None
    assert started["llm_model_system_only"] is True

    # create_execution with nonexistent parent raises ValueError instead of returning an unpersisted id
    with pytest.raises(ValueError, match="Automation not found"):
        await exec_db.create_execution(str(uuid.uuid4()), now, "srv-r5")

    # Edit automation config to change llm_model, reasoning_effort, and clear llm_model_system_only
    updated = await auto_db.update_automation(
        due_auto_id,
        user_id,
        llm_model="gpt-6.2-sol",
        reasoning_effort="high",
        metadata={"dalton_managed": True},
    )
    assert updated is not None

    # All previously created executions keep their frozen snapshot (immutable!)
    for eid in (exec_claimed_id, exec_live_id, exec_direct_id, exec_price_id):
        assert await _read_snapshot(eid) == ("claude-sonnet-4-6", None, True)
        fetched = await atm_mod._get_execution_by_id(eid)
        assert fetched is not None
        assert fetched["llm_model"] == "claude-sonnet-4-6"
        assert fetched["llm_model_system_only"] is True

    # New executions created AFTER the config edit pick up the new snapshot
    exec_after_live_id, _ = await exec_db.create_execution_for_live_parent(
        due_auto_id, user_id, now, "srv-r5"
    )
    exec_after_direct_id = await exec_db.create_execution(due_auto_id, now, "srv-r5")
    assert await _read_snapshot(exec_after_live_id) == ("gpt-6.2-sol", "high", False)
    assert await _read_snapshot(exec_after_direct_id) == ("gpt-6.2-sol", "high", False)


async def test_concurrent_automation_config_edit_preserves_existing_execution_and_serializes_new_execution_claim(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 real DB concurrency test:
    While an automation config UPDATE holds FOR UPDATE on the automations row (clearing
    llm_model_system_only), a concurrent create_execution_for_live_parent on another connection
    blocks on PostgreSQL Lock until commit, then stamps the post-commit False value, while an
    execution created before the edit keeps its frozen True value."""
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    user_id = f"b1-exec-conc-{uuid.uuid4().hex[:10]}"
    _c1_id, _c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    now = datetime.now(UTC)
    auto = await auto_db.create_automation(
        user_id=user_id,
        name="conc-sys-only",
        trigger_type="cron",
        cron_expression="0 9 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check AAPL.",
        workspace_id=ws_id,
        llm_model="claude-sonnet-4-6",
        metadata={"dalton_managed": True, "llm_model_system_only": True},
    )
    auto_id = str(auto["automation_id"])

    # Execution created before the concurrent edit is frozen at True
    exec_before_id, _ = await exec_db.create_execution_for_live_parent(
        auto_id, user_id, now, "srv-r5"
    )

    update_locked = asyncio.Event()
    allow_update_commit = asyncio.Event()
    holder_pid_box: list[int] = []

    async def _reconcile_pause(locked_row: dict[str, Any], effective: dict[str, Any], locked_conn: Any) -> None:
        assert locked_conn is not None
        holder_pid_box.append(int(locked_conn.info.backend_pid))
        update_locked.set()
        await asyncio.wait_for(allow_update_commit.wait(), timeout=15.0)

    async def _run_config_edit() -> Any:
        with company_automations.automation_history_context(
            actor_user_id=user_id,
            locked_row_reconciler=_reconcile_pause,
        ):
            return await auto_db.update_automation(
                auto_id,
                user_id,
                metadata={"dalton_managed": True},
            )

    edit_task = asyncio.create_task(_run_config_edit())
    claim_task: asyncio.Task[Any] | None = None
    try:
        lock_waiter = asyncio.create_task(update_locked.wait())
        done, pending_wait = await asyncio.wait(
            {edit_task, lock_waiter},
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending_wait:
            if p is lock_waiter:
                p.cancel()
        if edit_task in done:
            exc = edit_task.exception()
            if exc is not None:
                raise AssertionError(f"edit_task failed before acquiring FOR UPDATE: {exc!r}") from exc
            raise AssertionError(
                f"edit_task finished without invoking locked_row_reconciler: {edit_task.result()!r}"
            )
        if lock_waiter not in done:
            raise TimeoutError("Timed out waiting for update_automation to acquire FOR UPDATE")

        # Launch concurrent execution creation while update_automation holds FOR UPDATE on automations
        claim_task = asyncio.create_task(
            exec_db.create_execution_for_live_parent(auto_id, user_id, now, "srv-r5")
        )
        await asyncio.sleep(0)

        holder_pid = holder_pid_box[0]
        waiter = await _wait_for_lock_waiter(
            test_db_uri,
            blocker_pid=holder_pid,
            waiter_task=claim_task,
            holder_task=edit_task,
            timeout=5.0,
        )
        assert waiter["pid"] != holder_pid
        assert holder_pid in waiter["blocking_pids"]
        assert waiter["wait_event_type"] == "Lock"
        assert not claim_task.done(), "create_execution_for_live_parent should wait behind automations FOR UPDATE"

        allow_update_commit.set()
        edited_row = await asyncio.wait_for(edit_task, timeout=5.0)
        assert edited_row is not None
        exec_after_id, _ = await asyncio.wait_for(claim_task, timeout=5.0)

        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT automation_execution_id::text AS eid, llm_model_system_only
                    FROM automation_executions
                    WHERE automation_execution_id IN (%s, %s)
                    """,
                    (exec_before_id, exec_after_id),
                )
                by_id = {r["eid"]: bool(r["llm_model_system_only"]) for r in (await cur.fetchall() or ())}
        assert by_id[exec_before_id] is True
        assert by_id[exec_after_id] is False
    finally:
        allow_update_commit.set()
        for pending in (edit_task, claim_task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (edit_task, claim_task) if t is not None),
            return_exceptions=True,
        )


async def test_execution_post_wait_dispatch_uses_frozen_snapshot_after_automation_model_and_flag_edit(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 real DB test:
    Create an execution with (llm_model='claude-sonnet-4-6', reasoning_effort='low',
    llm_model_system_only=False). While AutomationExecutor.execute waits for a busy pinned
    thread, update the automation in PostgreSQL to (llm_model='gpt-6.2-sol',
    reasoning_effort='high', metadata.llm_model_system_only=True). Once the wait finishes
    and reloads the updated automation row from PostgreSQL, the dispatched turn STILL uses
    the execution row's frozen snapshot."""
    from types import SimpleNamespace

    from src.dalton.autonomy import preflight as dalton_preflight
    from src.server.database.conversation import threads_write
    from src.server.handlers import chat as chat_handler_mod
    from src.server.services import account as account_mod
    from src.server.services import automation_executor as exec_mod
    from src.server.services.shared_workspaces.turn import TurnIdentity
    from src.server.services.turn_runtime import TurnRoute

    _stub_catalog(monkeypatch)
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    user_id = f"b1-wait-snap-{uuid.uuid4().hex[:10]}"
    _c1_id, _c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    thread_id = str(uuid.uuid4())
    await threads_write.ensure_thread_exists(ws_id, thread_id, user_id, "Check NVDA.")

    now = datetime.now(UTC)
    auto = await auto_db.create_automation(
        user_id=user_id,
        name="wait-snap-auto",
        trigger_type="cron",
        cron_expression="0 9 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check NVDA.",
        workspace_id=ws_id,
        llm_model="claude-sonnet-4-6",
        reasoning_effort="low",
        thread_strategy="continue",
        conversation_thread_id=thread_id,
        metadata={"dalton_managed": True},
    )
    auto_id = str(auto["automation_id"])

    # Create execution row in real PostgreSQL: freezes ('claude-sonnet-4-6', 'low', False)
    exec_id, _ = await exec_db.create_execution_for_live_parent(auto_id, user_id, now, "srv-r6")

    waiting_for_thread = asyncio.Event()
    allow_thread_idle = asyncio.Event()

    async def _wait_and_reload(auto_arg: dict[str, Any], eid: str, tid: str, deadline: float):
        waiting_for_thread.set()
        await asyncio.wait_for(allow_thread_idle.wait(), timeout=15.0)
        return await auto_db.get_automation(auto_id, user_id)

    monkeypatch.setattr(exec_mod, "_keep_alive", create_autospec(exec_mod._keep_alive, return_value=None))
    monkeypatch.setattr(exec_mod, "late_note", create_autospec(exec_mod.late_note, return_value=None))
    monkeypatch.setattr(
        account_mod,
        "read_standing",
        create_autospec(
            account_mod.read_standing,
            return_value=SimpleNamespace(refuse_if_suspended=lambda: None),
        ),
    )
    monkeypatch.setattr(exec_mod, "_thread_busy", create_autospec(exec_mod._thread_busy, return_value=True))
    route = TurnRoute(workspace_id=ws_id, owner_id=user_id, role="analyst")
    monkeypatch.setattr(
        exec_mod,
        "_turn_place",
        create_autospec(
            exec_mod._turn_place,
            return_value=SimpleNamespace(workspace={"workspace_id": ws_id, "user_id": user_id}),
        ),
    )
    monkeypatch.setattr(exec_mod, "turn_route", create_autospec(exec_mod.turn_route, return_value=route))
    monkeypatch.setattr(TurnRoute, "bind", create_autospec(TurnRoute.bind, return_value=route))
    monkeypatch.setattr(exec_mod, "park_for_computer", create_autospec(exec_mod.park_for_computer, return_value=False))
    monkeypatch.setattr(
        dalton_preflight,
        "is_dalton_automation",
        create_autospec(dalton_preflight.is_dalton_automation, return_value=False),
    )
    monkeypatch.setattr(exec_mod, "_credentials", create_autospec(exec_mod._credentials, return_value=(False, False)))
    monkeypatch.setattr(
        exec_mod, "enforce_credit_limit", create_autospec(exec_mod.enforce_credit_limit, return_value=None)
    )
    monkeypatch.setattr(
        exec_mod,
        "_verify_thread_matches_automation",
        create_autospec(exec_mod._verify_thread_matches_automation, return_value=None),
    )
    monkeypatch.setattr(
        exec_mod.TurnIdentity,
        "of",
        create_autospec(
            TurnIdentity.of,
            return_value=TurnIdentity(sender=user_id, owner=user_id, recorded_as=user_id),
        ),
    )

    captured_requests: list[Any] = []

    async def _fake_astream(**kw: Any):
        captured_requests.append(kw["request"])
        if False:
            yield None

    monkeypatch.setattr(
        chat_handler_mod,
        "astream_ptc_workflow",
        create_autospec(chat_handler_mod.astream_ptc_workflow, side_effect=_fake_astream),
    )

    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    executor._wait_for_thread = create_autospec(executor._wait_for_thread, side_effect=_wait_and_reload)
    executor._resolve_thread = create_autospec(executor._resolve_thread, return_value=thread_id)
    executor._start_delivery = create_autospec(executor._start_delivery, return_value=None)
    executor._note_admission = create_autospec(executor._note_admission, return_value=True)
    executor._hand_to_run = create_autospec(executor._hand_to_run, return_value=True)

    exec_task = asyncio.create_task(executor.execute(dict(auto), exec_id))
    try:
        await asyncio.wait_for(waiting_for_thread.wait(), timeout=5.0)

        # While the execution waits for the busy thread, PM edits the automation in real PostgreSQL
        updated = await auto_db.update_automation(
            auto_id,
            user_id,
            llm_model="gpt-6.2-sol",
            reasoning_effort="high",
            metadata={"dalton_managed": True, "llm_model_system_only": True},
        )
        assert updated is not None
        assert updated["llm_model"] == "gpt-6.2-sol"
        assert updated["reasoning_effort"] == "high"
        assert updated["metadata"]["llm_model_system_only"] is True

        allow_thread_idle.set()
        await asyncio.wait_for(exec_task, timeout=5.0)

        assert len(captured_requests) == 1
        dispatched_req = captured_requests[0]
        assert dispatched_req.llm_model == "claude-sonnet-4-6"
        assert dispatched_req.reasoning_effort == "low"
        assert dispatched_req.llm_model_system_only is False
    finally:
        allow_thread_idle.set()
        if not exec_task.done():
            exec_task.cancel()
        await asyncio.gather(exec_task, return_exceptions=True)


async def test_d050_migration_upgrade_backfills_existing_executions_and_downgrade_drops_columns(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 (grok R5 high): real PostgreSQL d046 -> d050 upgrade backfills existing
    automation_executions rows from their earliest run's metadata (when runs exist) or from
    current automations config (when no runs exist yet), and d050 -> d046 downgrade drops
    all three snapshot columns cleanly."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from psycopg.types.json import Json

    from src.server.database.conversation import threads_write

    project_root = Path(__file__).resolve().parents[3]
    # Omit config_file_name so env.py does not call fileConfig() and disable existing pytest loggers.
    alembic_cfg = Config()
    alembic_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    sa_url = test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    alembic_cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))

    _stub_catalog(monkeypatch)
    user_id = f"b1-d050-mig-{uuid.uuid4().hex[:10]}"
    _c1_id, _c2_id, ws_id = await _setup_user_and_two_computers(user_id)
    thread_id = str(uuid.uuid4())
    await threads_write.ensure_thread_exists(ws_id, thread_id, user_id, "d050 migration test")

    # 1. Automation whose earlier system-only execution already has runs, but whose config
    #    was later edited to non-system-only + different model/effort.
    auto_sys_ran = await auto_db.create_automation(
        user_id=user_id,
        name="d050-sys-ran",
        trigger_type="cron",
        cron_expression="0 9 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check NVDA.",
        workspace_id=ws_id,
        llm_model="owner-custom-later",
        reasoning_effort="low",
        metadata={"dalton_managed": True},
    )
    # 2. Automation whose earlier normal execution already has a run (explicit None effort,
    #    llm_model_system_only=False), but whose config was later edited to system-only=True.
    auto_norm_ran = await auto_db.create_automation(
        user_id=user_id,
        name="d050-norm-ran",
        trigger_type="cron",
        cron_expression="0 10 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check AAPL.",
        workspace_id=ws_id,
        llm_model="gpt-6.2-sol",
        reasoning_effort="high",
        metadata={"dalton_managed": True, "llm_model_system_only": True},
    )
    # 3. System-only automation with a pending execution (no runs yet).
    auto_sys_pending = await auto_db.create_automation(
        user_id=user_id,
        name="d050-sys-pending",
        trigger_type="cron",
        cron_expression="0 11 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check MSFT.",
        workspace_id=ws_id,
        llm_model="gpt-6.2-sol",
        reasoning_effort="medium",
        metadata={"dalton_managed": True, "llm_model_system_only": True},
    )
    # 4. Normal automation with a pending execution (no runs yet).
    auto_norm_pending = await auto_db.create_automation(
        user_id=user_id,
        name="d050-norm-pending",
        trigger_type="cron",
        cron_expression="0 12 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Check GOOG.",
        workspace_id=ws_id,
        llm_model="claude-sonnet-4-6",
        reasoning_effort=None,
        metadata={"dalton_managed": True},
    )

    exec_sys_ran_id = str(uuid.uuid4())
    exec_norm_ran_id = str(uuid.uuid4())
    exec_sys_pending_id = str(uuid.uuid4())
    exec_norm_pending_id = str(uuid.uuid4())
    run_sys_first_id = str(uuid.uuid4())
    run_sys_later_id = str(uuid.uuid4())
    run_norm_first_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    try:
        # Downgrade to d046 (pre-d050 schema without the 3 snapshot columns).
        await asyncio.to_thread(command.downgrade, alembic_cfg, "d046")

        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=False) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'automation_executions'
                      AND column_name IN ('llm_model', 'reasoning_effort', 'llm_model_system_only')
                    """
                )
                assert await cur.fetchall() == []

                # Insert pre-d050 executions (2 with runs, 2 pending without runs).
                await cur.execute(
                    """
                    INSERT INTO automation_executions (
                        automation_execution_id, automation_id, status,
                        conversation_thread_id, conversation_response_id,
                        scheduled_at, started_at, server_id, created_at
                    )
                    VALUES
                        (%s::uuid, %s::uuid, 'running', %s::uuid, %s::uuid, %s, %s, 'srv-pre', %s),
                        (%s::uuid, %s::uuid, 'running', %s::uuid, %s::uuid, %s, %s, 'srv-pre', %s),
                        (%s::uuid, %s::uuid, 'pending', NULL, NULL, %s, NULL, 'srv-pre', %s),
                        (%s::uuid, %s::uuid, 'pending', NULL, NULL, %s, NULL, 'srv-pre', %s)
                    """,
                    (
                        exec_sys_ran_id,
                        str(auto_sys_ran["automation_id"]),
                        thread_id,
                        run_sys_first_id,
                        now,
                        now,
                        now,
                        exec_norm_ran_id,
                        str(auto_norm_ran["automation_id"]),
                        thread_id,
                        run_norm_first_id,
                        now,
                        now,
                        now,
                        exec_sys_pending_id,
                        str(auto_sys_pending["automation_id"]),
                        now,
                        now,
                        exec_norm_pending_id,
                        str(auto_norm_pending["automation_id"]),
                        now,
                        now,
                    ),
                )

                # Earliest run + a later continuation run for exec_sys_ran, and earliest run for exec_norm_ran.
                _seed = (
                    (
                        run_sys_first_id,
                        thread_id,
                        Json(
                            {
                                "automation_execution_id": exec_sys_ran_id,
                                "llm_model": "claude-sonnet-4-6",
                                "llm_model_system_only": True,
                                "automation_reasoning_effort_configured": True,
                                "automation_reasoning_effort": "high",
                            }
                        ),
                        now,
                        str(uuid.uuid4()),
                        user_id,
                        run_sys_later_id,
                        thread_id,
                        Json(
                            {
                                "automation_source_execution_id": exec_sys_ran_id,
                                "llm_model": "owner-custom-later",
                                "llm_model_system_only": False,
                                "automation_reasoning_effort_configured": True,
                                "automation_reasoning_effort": "low",
                            }
                        ),
                        now + timedelta(seconds=10),
                        str(uuid.uuid4()),
                        user_id,
                        run_norm_first_id,
                        thread_id,
                        Json(
                            {
                                "automation_execution_id": exec_norm_ran_id,
                                "llm_model": "owner-custom-model",
                                "llm_model_system_only": False,
                                "automation_reasoning_effort_configured": True,
                                "automation_reasoning_effort": None,
                            }
                        ),
                        now + timedelta(seconds=20),
                        str(uuid.uuid4()),
                        user_id,
                    )
                )
                # Rows are born in_progress (lifecycle guard) and a thread holds one in_progress row at a
                # time (uq_responses_in_progress_slot): insert each run, then settle it before the next.
                for turn_index in range(3):
                    rid, tid, meta, created, req_key, uid = _seed[turn_index * 6 : turn_index * 6 + 6]
                    await cur.execute(
                        """
                        INSERT INTO conversation_responses (
                            conversation_response_id, conversation_thread_id,
                            turn_index, status, metadata, created_at,
                            attempt_no, retry_of_run_id, request_key, user_id
                        )
                        VALUES (%s::uuid, %s::uuid, %s, 'in_progress', %s, %s, 1, NULL, %s, %s)
                        """,
                        (rid, tid, turn_index, meta, created, req_key, uid),
                    )
                    await cur.execute(
                        "UPDATE conversation_responses SET status = 'completed' WHERE conversation_response_id = %s::uuid",
                        (rid,),
                    )
            await conn.commit()

        # Upgrade d046 -> d050: adds the 3 columns and backfills existing rows.
        await asyncio.to_thread(command.upgrade, alembic_cfg, "d050")

        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT automation_execution_id::text AS eid,
                           llm_model, reasoning_effort, llm_model_system_only
                    FROM automation_executions
                    WHERE automation_execution_id = ANY(%s::uuid[])
                    """,
                    (
                        [
                            exec_sys_ran_id,
                            exec_norm_ran_id,
                            exec_sys_pending_id,
                            exec_norm_pending_id,
                        ],
                    ),
                )
                rows = {r["eid"]: dict(r) for r in await cur.fetchall()}

        # System-only execution with runs: took earliest run metadata (NOT later run / mutated automation).
        assert rows[exec_sys_ran_id]["llm_model"] == "claude-sonnet-4-6"
        assert rows[exec_sys_ran_id]["reasoning_effort"] == "high"
        assert rows[exec_sys_ran_id]["llm_model_system_only"] is True

        # Normal execution with runs: took earliest run metadata (explicit NULL effort & False flag preserved).
        assert rows[exec_norm_ran_id]["llm_model"] == "owner-custom-model"
        assert rows[exec_norm_ran_id]["reasoning_effort"] is None
        assert rows[exec_norm_ran_id]["llm_model_system_only"] is False

        # Pending system-only execution (no runs yet): took current automations config.
        assert rows[exec_sys_pending_id]["llm_model"] == "gpt-6.2-sol"
        assert rows[exec_sys_pending_id]["reasoning_effort"] == "medium"
        assert rows[exec_sys_pending_id]["llm_model_system_only"] is True

        # Pending normal execution (no runs yet): took current automations config.
        assert rows[exec_norm_pending_id]["llm_model"] == "claude-sonnet-4-6"
        assert rows[exec_norm_pending_id]["reasoning_effort"] is None
        assert rows[exec_norm_pending_id]["llm_model_system_only"] is False

        # Downgrade d050 -> d046 drops all 3 columns cleanly.
        await asyncio.to_thread(command.downgrade, alembic_cfg, "d046")
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'automation_executions'
                      AND column_name IN ('llm_model', 'reasoning_effort', 'llm_model_system_only')
                    """
                )
                assert await cur.fetchall() == []
    finally:
        # Always restore Dalton schema to head (d050) for subsequent tests.
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
