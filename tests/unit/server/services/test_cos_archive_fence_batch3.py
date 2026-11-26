"""Unit tests for Batch 3: archive fences, lock order, terminal-only clear, startup recovery, and local HTTP error capture."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ptc_agent.core.sandbox.providers import (
    ARCHIVABLE_KINDS,
    PROVIDER_KINDS,
    provider_supports_archive,
)
from ptc_agent.core.sandbox.providers.ssh_errors import SSHConnectError
from ptc_agent.core.sandbox.runtime import (
    RuntimeState,
    SandboxFailureKind,
    SandboxGoneError,
    SandboxRuntime,
    SandboxTransientError,
)
from src.server.database import computer as computer_db
from src.server.database import workspace_move as move_db
from src.server.database.sql_fences import FENCE_TAKES_WORKSPACES, takes_a_workspace_sql
from src.server.database.workspace_move import MoveRefused
from src.server.dependencies.principal import Principal, get_principal
from src.server.services.computer_errors import (
    ArchiveProviderError,
    ArchiveScopeChangedError,
    ArchiveUnsupportedError,
    ComputerArchivingError,
    ComputerBusyError,
    ComputerNotStoppedError,
)
from src.server.services.computer_manager import ComputerManager
from src.server.services.workspace_manager import WorkspaceManager

USER_ID = "user-batch3-1"
COMP_A = "11111111-1111-4111-8111-111111111111"
COMP_B = "22222222-2222-4222-8222-222222222222"
WS_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WS_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

FAKE_SECRET_BODY = (
    "Upstream failed: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
    "key=sk-proj_1234567890abcdef google=AIzaSyA1234567890abcdefXYZ "
    "dsn=postgresql://dbuser:s3cr3tP4ss@10.0.0.8:5432/app raw_vendor_payload=DO_NOT_LOG"
)


def _computer_row(
    cid: str = COMP_A,
    *,
    kind: str = "daytona",
    status: str = "stopped",
    config: dict[str, Any] | None = None,
    provider_ref: str | None = "sbx-daytona-1",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "computer_id": cid,
        "user_id": USER_ID,
        "kind": kind,
        "provider_ref": provider_ref,
        "name": "Daytona Box",
        "is_primary": False,
        "status": status,
        "resource_tier": "standard",
        "is_always_on": False,
        "root_dir": "/home/workspace",
        "config": dict(config or {}),
        "spec_change": None,
        "spec_change_stale": False,
        "created_at": now,
        "updated_at": now,
    }


def test_archivable_kinds_matches_runtime_classes():
    from ptc_agent.core.sandbox.providers.daytona_runtime import DaytonaRuntime
    from ptc_agent.core.sandbox.providers.docker import DockerRuntime
    from ptc_agent.core.sandbox.providers.e2b_runtime import E2BRuntime
    from ptc_agent.core.sandbox.providers.local_runtime import LocalRuntime
    from ptc_agent.core.sandbox.providers.ssh_runtime import SSHRuntime

    runtime_by_kind = {
        "daytona": DaytonaRuntime,
        "docker": DockerRuntime,
        "e2b": E2BRuntime,
        "local": LocalRuntime,
        "ssh": SSHRuntime,
    }
    assert set(runtime_by_kind) == set(PROVIDER_KINDS)
    for kind, cls in runtime_by_kind.items():
        assert provider_supports_archive(kind) is bool(cls.archives)
        assert (kind in ARCHIVABLE_KINDS) is bool(cls.archives)
    assert provider_supports_archive(None) is False
    assert provider_supports_archive("unknown") is False


def test_takes_a_workspace_sql_excludes_archiving_and_stopping():
    assert '{"archiving": true}' in FENCE_TAKES_WORKSPACES
    sql = takes_a_workspace_sql("c")
    assert '{"archiving": true}' in sql
    assert "c.status <> 'stopping'" in sql


@pytest.mark.asyncio
async def test_claim_workspace_move_locks_source_computer_and_blocks_archiving(monkeypatch):
    executed: list[tuple[str, Any]] = []

    class _FakeCur:
        def __init__(self, responses):
            self._responses = iter(responses)
            self._last = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            executed.append((sql, params))
            self._last = next(self._responses, None)

        async def fetchone(self):
            return self._last

        async def fetchall(self):
            return self._last or []

    def _conn_cm(responses):
        cur = _FakeCur(responses)

        @asynccontextmanager
        async def _tx():
            yield

        class _Conn:
            def transaction(self):
                return _tx()

            def cursor(self, row_factory=None):
                return cur

        @asynccontextmanager
        async def _get_conn():
            yield _Conn()

        return _get_conn

    # 1. Source computer carries config.archiving=true -> MoveRefused("busy")
    monkeypatch.setattr(
        move_db,
        "get_db_connection",
        _conn_cm(
            [
                None,  # UPDATE stale early moves
                {"computer_id": COMP_A},  # pre-read workspace source
                [
                    {
                        "computer_id": COMP_A,
                        "files_backup": True,
                        "config": {"archiving": True},
                        "takes_workspace": False,
                        "intent_taken_at": None,
                    },
                    {
                        "computer_id": COMP_B,
                        "files_backup": True,
                        "config": {},
                        "takes_workspace": True,
                        "intent_taken_at": None,
                    },
                ],
                {
                    "workspace_id": WS_1,
                    "user_id": USER_ID,
                    "computer_id": COMP_A,
                    "dir_name": "NVDA",
                    "status": "stopped",
                },
            ]
        ),
    )
    with pytest.raises(MoveRefused) as exc_info:
        await move_db.claim_workspace_move(
            user_id=USER_ID, workspace_id=WS_1, target_computer_id=COMP_B
        )
    assert exc_info.value.code == "busy"
    lock_sql = executed[2][0]
    assert "ORDER BY computer_id" in lock_sql
    assert "FOR UPDATE" in lock_sql
    assert "computer_id = %(source)s::uuid" in lock_sql

    # 2. Source drifted concurrently between pre-read and workspace lock -> move_in_progress
    monkeypatch.setattr(
        move_db,
        "get_db_connection",
        _conn_cm(
            [
                None,
                {"computer_id": COMP_A},
                [
                    {
                        "computer_id": COMP_A,
                        "files_backup": True,
                        "config": {},
                        "takes_workspace": True,
                        "intent_taken_at": None,
                    },
                    {
                        "computer_id": COMP_B,
                        "files_backup": True,
                        "config": {},
                        "takes_workspace": True,
                        "intent_taken_at": None,
                    },
                ],
                {
                    "workspace_id": WS_1,
                    "user_id": USER_ID,
                    "computer_id": str(uuid.uuid4()),
                    "dir_name": "NVDA",
                    "status": "stopped",
                },
            ]
        ),
    )
    with pytest.raises(MoveRefused) as exc_drift:
        await move_db.claim_workspace_move(
            user_id=USER_ID, workspace_id=WS_1, target_computer_id=COMP_B
        )
    assert exc_drift.value.code == "move_in_progress"


@pytest.mark.asyncio
async def test_claim_computer_for_archive_verifies_locks_snapshot_and_stale_moves(monkeypatch):
    from src.server.database import workspace_folders

    executed_sql: list[str] = []

    def _make_conn(computer_row, ws_rows, move_row):
        class _Cur:
            def __init__(self):
                self._step = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql, params=None):
                executed_sql.append(sql)
                self._step += 1

            async def fetchone(self):
                if self._step == 1:
                    return computer_row
                return move_row

            async def fetchall(self):
                return ws_rows

        @asynccontextmanager
        async def _tx():
            yield

        class _Conn:
            def transaction(self):
                return _tx()

            def cursor(self, row_factory=None):
                return _Cur()

        @asynccontextmanager
        async def _get_conn():
            yield _Conn()

        return _get_conn

    monkeypatch.setattr(
        workspace_folders,
        "computer_run_in_progress",
        create_autospec(workspace_folders.computer_run_in_progress, return_value=False),
    )
    monkeypatch.setattr(
        workspace_folders,
        "computer_command_running",
        create_autospec(workspace_folders.computer_command_running, return_value=False),
    )

    ws_rows = [
        {"workspace_id": WS_1, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
    ]

    # Unsupported provider -> ArchiveUnsupportedError
    monkeypatch.setattr(
        computer_db,
        "get_db_connection",
        _make_conn(_computer_row(kind="docker"), ws_rows, None),
    )
    with pytest.raises(ArchiveUnsupportedError):
        await computer_db.claim_computer_for_archive(COMP_A)

    # Not stopped -> ComputerNotStoppedError
    monkeypatch.setattr(
        computer_db,
        "get_db_connection",
        _make_conn(_computer_row(status="running"), ws_rows, None),
    )
    with pytest.raises(ComputerNotStoppedError):
        await computer_db.claim_computer_for_archive(COMP_A)

    # Snapshot mismatch -> ArchiveScopeChangedError
    monkeypatch.setattr(
        computer_db,
        "get_db_connection",
        _make_conn(_computer_row(), ws_rows, None),
    )
    with pytest.raises(ArchiveScopeChangedError):
        await computer_db.claim_computer_for_archive(
            COMP_A,
            expected_workspaces=[
                {"workspace_id": WS_1, "name": "OLD", "dir_name": "OLD", "status": "stopped", "is_stand_in": False},
            ],
        )

    # Blocking move (including stale staged/committed) -> ComputerBusyError
    executed_sql.clear()
    monkeypatch.setattr(
        computer_db,
        "get_db_connection",
        _make_conn(_computer_row(), ws_rows, {"?column?": 1}),
    )
    with pytest.raises(ComputerBusyError, match="move"):
        await computer_db.claim_computer_for_archive(COMP_A, expected_workspaces=ws_rows)
    assert "FOR UPDATE" in executed_sql[0]
    assert "ORDER BY workspace_id" in executed_sql[1]
    assert "FOR UPDATE" in executed_sql[1]
    assert "'staged', 'committed'" in executed_sql[2]

    # Happy path -> commits stopping + config.archiving=True in same transaction
    claimed_row = _computer_row(status="stopping", config={"archiving": True})
    update_mock = create_autospec(
        computer_db.update_computer_status, return_value=claimed_row
    )
    monkeypatch.setattr(computer_db, "update_computer_status", update_mock)
    monkeypatch.setattr(
        computer_db,
        "get_db_connection",
        _make_conn(_computer_row(), ws_rows, None),
    )
    claimed = await computer_db.claim_computer_for_archive(
        COMP_A, expected_workspaces=ws_rows, expected_workspace_id=WS_1
    )
    assert claimed["status"] == "stopping"
    assert claimed["config"]["archiving"] is True
    assert update_mock.await_args.args == (COMP_A, "stopping")
    assert update_mock.await_args.kwargs["expected"] == "stopped"
    merged_cfg = update_mock.await_args.kwargs["merge_config"]
    assert merged_cfg["archiving"] is True
    assert isinstance(merged_cfg.get("archiving_owner"), str) and merged_cfg["archiving_owner"]
    assert isinstance(merged_cfg.get("archiving_claimed_at"), str) and merged_cfg["archiving_claimed_at"]
    assert update_mock.await_args.kwargs["conn"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_state,expected_status,expected_clear",
    [
        ("archiving", "stopped", False),
        ("archived", "stopped", True),
        ("stopped", "stopped", True),
        ("error", "stopped", True),
        ("running", "running", True),
    ],
)
async def test_reconcile_provider_stop_clears_archiving_only_on_terminal_state(
    monkeypatch, provider_state, expected_status, expected_clear
):
    from src.server.services.computer_manager import _lifecycle

    cm = object.__new__(ComputerManager)
    settle_mock = create_autospec(cm._settle_machine_stop)
    cm._settle_machine_stop = settle_mock
    activity_mock = create_autospec(computer_db.update_computer_activity)
    monkeypatch.setattr(_lifecycle, "update_computer_activity", activity_mock)

    runtime = create_autospec(SandboxRuntime, instance=True)
    runtime.get_state.return_value = RuntimeState(provider_state)

    @asynccontextmanager
    async def _detached(*_a, **_kw):
        yield runtime

    cm._detached_runtime = _detached
    binding = SimpleNamespace(
        computer_id=COMP_A,
        workspace_id=WS_1,
        provider_ref="sbx-1",
        provider=SimpleNamespace(recreatable=True),
    )
    await cm._reconcile_provider_stop(binding)
    if expected_clear:
        settle_mock.assert_awaited_once_with(COMP_A, expected_status)
    else:
        settle_mock.assert_awaited_once_with(
            COMP_A, expected_status, clear_archiving=False
        )


@pytest.mark.asyncio
async def test_reconcile_archiving_computer_preserves_while_archiving_and_clears_on_archived(
    monkeypatch,
):
    from src.server.services.computer_manager import _machines

    cm = object.__new__(ComputerManager)

    @asynccontextmanager
    async def _noop_lock(*_a, **_kw):
        yield

    cm._observed_lock = _noop_lock
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    archiving_stopped = _computer_row(status="stopped", config={"archiving": True})
    cleared_stopped = _computer_row(status="stopped", config={})
    from src.dalton.services import archive_lifecycle
    clear_mock = create_autospec(computer_db.clear_computer_archiving, return_value=True)
    monkeypatch.setattr(_machines, "clear_computer_archiving", clear_mock)
    monkeypatch.setattr(
        archive_lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(archive_lifecycle.computer_stop_heartbeat_present, return_value=False),
    )

    # Provider still archiving -> keep fence
    monkeypatch.setattr(
        _machines,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=archiving_stopped),
    )

    @asynccontextmanager
    async def _still_archiving(*_a, **_kw):
        rt = create_autospec(SandboxRuntime, instance=True)
        rt.get_state.return_value = RuntimeState.ARCHIVING
        yield rt

    cm._detached_runtime = _still_archiving
    res = await cm.reconcile_archiving_computer(COMP_A)
    assert res["config"]["archiving"] is True
    clear_mock.assert_not_awaited()

    # Provider finished -> clear fence
    monkeypatch.setattr(
        _machines,
        "get_computer",
        create_autospec(
            computer_db.get_computer, side_effect=[archiving_stopped, cleared_stopped]
        ),
    )

    @asynccontextmanager
    async def _archived(*_a, **_kw):
        rt = create_autospec(SandboxRuntime, instance=True)
        rt.get_state.return_value = RuntimeState.ARCHIVED
        yield rt

    cm._detached_runtime = _archived
    res2 = await cm.reconcile_archiving_computer(COMP_A)
    assert res2["config"] == {}
    clear_mock.assert_awaited_once_with(COMP_A)


@pytest.mark.asyncio
async def test_startup_recovery_skips_when_stop_heartbeat_present(monkeypatch):
    from src.dalton.services import archive_lifecycle
    from src.server.services import workspace_manager as wm_mod

    wm = object.__new__(WorkspaceManager)
    stuck_row = _computer_row(status="stopping", config={"archiving": True})

    list_mock = create_autospec(
        computer_db.get_archiving_computers_by_status, return_value=[stuck_row]
    )
    reset_mock = create_autospec(
        computer_db.reset_abandoned_archiving_computer, return_value=stuck_row
    )
    shadow_mock = create_autospec(computer_db.update_computer_status)
    monkeypatch.setattr(archive_lifecycle, "get_archiving_computers_by_status", list_mock)
    monkeypatch.setattr(archive_lifecycle, "reset_abandoned_archiving_computer", reset_mock)
    monkeypatch.setattr(wm_mod, "update_computer_status", shadow_mock)

    # 1. Live heartbeat -> skip writeback completely.
    hb_mock = create_autospec(
        archive_lifecycle.computer_stop_heartbeat_present, return_value=True
    )
    monkeypatch.setattr(archive_lifecycle, "computer_stop_heartbeat_present", hb_mock)
    assert await wm.recover_archiving_computers_on_startup() == 0
    reset_mock.assert_not_awaited()
    shadow_mock.assert_not_awaited()

    # 2. Heartbeat unreadable (None) and durable claim still within 180s -> skip.
    hb_mock.return_value = None
    assert await wm.recover_archiving_computers_on_startup() == 0
    reset_mock.assert_not_awaited()
    shadow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_recovery_resets_only_abandoned_archiving_computer_without_shadow_write(
    monkeypatch,
):
    from src.dalton.services import archive_lifecycle
    from src.server.services import workspace_manager as wm_mod

    wm = object.__new__(WorkspaceManager)
    old_ts = datetime.now(timezone.utc) - timedelta(seconds=60)
    stuck_row = _computer_row(
        status="stopping",
        config={
            "archiving": True,
            "archiving_owner": "owner-expired-1",
            "archiving_claimed_at": old_ts.isoformat(),
        },
    )
    stuck_row["updated_at"] = old_ts
    reset_row = _computer_row(status="stopped", config={"archiving": True})

    list_mock = create_autospec(
        computer_db.get_archiving_computers_by_status, return_value=[stuck_row]
    )
    reset_mock = create_autospec(
        computer_db.reset_abandoned_archiving_computer, return_value=reset_row
    )
    shadow_mock = create_autospec(computer_db.update_computer_status)
    hb_mock = create_autospec(
        archive_lifecycle.computer_stop_heartbeat_present, return_value=False
    )
    monkeypatch.setattr(archive_lifecycle, "get_archiving_computers_by_status", list_mock)
    monkeypatch.setattr(archive_lifecycle, "reset_abandoned_archiving_computer", reset_mock)
    monkeypatch.setattr(wm_mod, "update_computer_status", shadow_mock)
    monkeypatch.setattr(archive_lifecycle, "computer_stop_heartbeat_present", hb_mock)

    count = await wm.recover_archiving_computers_on_startup()
    assert count == 1
    list_mock.assert_awaited_once_with("stopping", limit=1000)
    hb_mock.assert_awaited_once_with(COMP_A)
    reset_mock.assert_awaited_once_with(
        COMP_A,
        expected_updated_at=stuck_row["updated_at"],
        expected_archiving_owner="owner-expired-1",
    )
    # Must NOT shadow-write live workspaces via update_computer_status.
    shadow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_abandoned_archiving_computer_sql_updates_only_computers_table(
    monkeypatch,
):
    executed: list[tuple[str, Any]] = []

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            executed.append((sql, params))

        async def fetchone(self):
            return _computer_row(status="stopped", config={"archiving": True})

    @asynccontextmanager
    async def _cursor(conn=None):
        yield _Cur()

    monkeypatch.setattr(computer_db, "_computer_cursor", _cursor)
    ts = datetime.now(timezone.utc) - timedelta(seconds=200)
    res = await computer_db.reset_abandoned_archiving_computer(
        COMP_A, expected_updated_at=ts
    )
    assert res is not None
    assert res["status"] == "stopped"
    sql, params = executed[0]
    assert "UPDATE computers" in sql
    assert "UPDATE workspaces" not in sql
    assert "status = 'stopping'" in sql
    assert '{"archiving": true}' in sql
    assert "updated_at = %(expected_updated_at)s" in sql
    assert params["expected_updated_at"] == ts


@pytest.mark.asyncio
async def test_archive_http_routes_capture_sandbox_errors_locally_without_leaking_secrets(
    monkeypatch, caplog
):
    from src.server.app import computers as computers_app
    from src.server.app import workspaces as workspaces_app

    app = FastAPI()
    app.include_router(workspaces_app.router)
    app.include_router(computers_app.router)

    global_handler_called = False

    async def _sentinel_global_handler(request, exc):
        nonlocal global_handler_called
        global_handler_called = True
        raise AssertionError("Global _sandbox_failure_handler must not be reached")

    app.add_exception_handler(SandboxGoneError, _sentinel_global_handler)
    app.add_exception_handler(SandboxTransientError, _sentinel_global_handler)
    app.add_exception_handler(SSHConnectError, _sentinel_global_handler)

    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID, auth="session", scopes=frozenset()
    )

    ws_row = {
        "workspace_id": WS_1,
        "user_id": USER_ID,
        "name": "NVDA",
        "status": "stopped",
        "computer_id": COMP_A,
    }
    comp_row = _computer_row(status="stopped")

    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    comp_mgr = create_autospec(ComputerManager, instance=True)

    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_row),
    )
    monkeypatch.setattr(
        workspaces_app,
        "workspace_access",
        create_autospec(workspaces_app.workspace_access, return_value=None),
    )
    monkeypatch.setattr(
        workspaces_app.WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: ws_mgr),
    )
    monkeypatch.setattr(
        computers_app,
        "_reached_computer",
        create_autospec(computers_app._reached_computer, return_value=comp_row),
    )
    monkeypatch.setattr(computers_app, "_computer_manager", lambda: comp_mgr)

    caplog.set_level(logging.WARNING)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for exc, expected_status in [
            (SandboxTransientError(FAKE_SECRET_BODY), 503),
            (SandboxGoneError(FAKE_SECRET_BODY), 503),
            (RuntimeError(FAKE_SECRET_BODY), 400),
            (ArchiveScopeChangedError("情况已变化，请重新发起"), 409),
            (ArchiveUnsupportedError("Provider does not support archiving"), 400),
            (ComputerNotStoppedError("Only stopped"), 409),
        ]:
            caplog.clear()
            ws_mgr.archive_workspace.side_effect = exc
            r_ws = await client.post(f"/api/v1/workspaces/{WS_1}/archive")
            assert r_ws.status_code == expected_status
            assert "DO_NOT_LOG" not in r_ws.text
            assert "sk-proj_" not in r_ws.text
            assert "s3cr3tP4ss" not in r_ws.text
            assert "AIzaSyA" not in r_ws.text
            assert "eyJhbGci" not in r_ws.text

            comp_mgr.archive_computer.side_effect = exc
            r_c = await client.post(f"/api/v1/computers/{COMP_A}/archive")
            assert r_c.status_code == expected_status
            assert "DO_NOT_LOG" not in r_c.text
            assert "sk-proj_" not in r_c.text
            assert "s3cr3tP4ss" not in r_c.text

            log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
            assert "DO_NOT_LOG" not in log_blob
            assert "sk-proj_" not in log_blob
            assert "s3cr3tP4ss" not in log_blob
            assert "AIzaSyA" not in log_blob
            assert "eyJhbGci" not in log_blob

    assert global_handler_called is False


@pytest.mark.asyncio
async def test_workspace_archive_route_blocks_self_archive_and_runs_cli_approval(
    monkeypatch,
):
    from src.server.app import workspaces as workspaces_app

    app = FastAPI()
    app.include_router(workspaces_app.router)

    ws_row = {
        "workspace_id": WS_1,
        "user_id": USER_ID,
        "name": "NVDA",
        "status": "stopped",
        "computer_id": COMP_A,
    }
    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_row),
    )
    monkeypatch.setattr(
        workspaces_app,
        "workspace_access",
        create_autospec(workspaces_app.workspace_access, return_value=None),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(
            computer_db.get_computer_for_workspace,
            return_value=_computer_row(cid=COMP_A),
        ),
    )

    # 1. Token from COMP_A trying to archive workspace on COMP_A -> 403 self-archive refused
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID,
        auth="computer",
        scopes=frozenset({"workspace:write"}),
        computer_id=COMP_A,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r_self = await client.post(f"/api/v1/workspaces/{WS_1}/archive")
    assert r_self.status_code == 403
    assert "cannot stop, archive or remove itself" in r_self.text

    # 2. Token from COMP_B archiving workspace on COMP_A -> invokes cli_approval.computer_archive
    #    with read-only snapshot and passes the same computer_id + snapshot to archive_workspace.
    snapshot = [
        {"workspace_id": WS_1, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
    ]
    monkeypatch.setattr(
        computer_db,
        "get_live_workspaces_for_archive_snapshot",
        create_autospec(
            computer_db.get_live_workspaces_for_archive_snapshot,
            return_value=snapshot,
        ),
    )
    approval_mock = create_autospec(workspaces_app.cli_approval.computer_archive)
    monkeypatch.setattr(workspaces_app.cli_approval, "computer_archive", approval_mock)
    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.archive_workspace.return_value = {**ws_row, "archive_state": "archived"}
    monkeypatch.setattr(
        workspaces_app.WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: ws_mgr),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID,
        auth="computer",
        scopes=frozenset({"workspace:write"}),
        computer_id=COMP_B,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r_other = await client.post(f"/api/v1/workspaces/{WS_1}/archive")
    assert r_other.status_code == 200
    approval_mock.assert_awaited_once()
    assert approval_mock.await_args.kwargs["expected_workspaces"] == snapshot
    ws_mgr.archive_workspace.assert_awaited_once_with(
        WS_1,
        expected_computer_id=COMP_A,
        expected_workspaces=snapshot,
    )


@pytest.mark.asyncio
async def test_archive_machine_keeps_fence_when_provider_still_archiving_and_clears_on_terminal(
    monkeypatch,
):
    from src.server.services.computer_manager import _lifecycle

    cm = object.__new__(ComputerManager)

    @asynccontextmanager
    async def _noop_lock(*_a, **_kw):
        yield

    @asynccontextmanager
    async def _noop_hb(*_a, **_kw):
        yield

    cm._observed_lock = _noop_lock
    cm._machine_has_active_tasks = create_autospec(
        cm._machine_has_active_tasks, return_value=False
    )
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat", _noop_hb)

    claimed = _computer_row(status="stopping", config={"archiving": True})
    cm._claim_machine_for_archive = create_autospec(
        cm._claim_machine_for_archive, return_value=claimed
    )
    cm._stop_result = create_autospec(
        cm._stop_result, return_value=_computer_row(status="stopped")
    )

    # 1. Provider still 'archiving' -> keep config.archiving fence & report archiving in progress.
    settle_mock = create_autospec(cm._settle_machine_stop)
    cm._settle_machine_stop = settle_mock
    rt_archiving = create_autospec(SandboxRuntime, instance=True)
    rt_archiving.archives = True
    rt_archiving.refresh_state.return_value = RuntimeState.ARCHIVING

    @asynccontextmanager
    async def _detached_archiving(*_a, **_kw):
        yield rt_archiving

    cm._detached_runtime = _detached_archiving
    res_ing = await cm._archive_machine(COMP_A)
    assert res_ing["archive_state"] == "archiving"
    rt_archiving.archive.assert_awaited_once()
    rt_archiving.refresh_state.assert_awaited_once()
    settle_mock.assert_awaited_once_with(COMP_A, "stopped", clear_archiving=False)

    # 2. Provider confirmed 'archived' -> clear fence & report archived.
    settle_mock.reset_mock()
    rt_archived = create_autospec(SandboxRuntime, instance=True)
    rt_archived.archives = True
    rt_archived.refresh_state.return_value = RuntimeState.ARCHIVED

    @asynccontextmanager
    async def _detached_archived(*_a, **_kw):
        yield rt_archived

    cm._detached_runtime = _detached_archived
    res_done = await cm._archive_machine(COMP_A)
    assert res_done["archive_state"] == "archived"
    settle_mock.assert_awaited_once_with(COMP_A, "stopped", clear_archiving=True)

    # 3. Post-archive refresh_state raises SandboxGoneError -> clear fence & report archived.
    settle_mock.reset_mock()
    rt_gone = create_autospec(SandboxRuntime, instance=True)
    rt_gone.archives = True
    rt_gone.refresh_state.side_effect = SandboxGoneError("sbx-daytona-1", "gone")

    @asynccontextmanager
    async def _detached_gone(*_a, **_kw):
        yield rt_gone

    cm._detached_runtime = _detached_gone
    res_gone = await cm._archive_machine(COMP_A)
    assert res_gone["archive_state"] == "archived"
    settle_mock.assert_awaited_once_with(COMP_A, "stopped", clear_archiving=True)


@pytest.mark.asyncio
async def test_archive_http_routes_report_in_progress_when_still_archiving(monkeypatch):
    from src.server.app import computers as computers_app
    from src.server.app import workspaces as workspaces_app

    app = FastAPI()
    app.include_router(workspaces_app.router)
    app.include_router(computers_app.router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID, auth="session", scopes=frozenset()
    )

    ws_row = {
        "workspace_id": WS_1,
        "user_id": USER_ID,
        "name": "NVDA",
        "status": "stopped",
        "computer_id": COMP_A,
    }
    comp_row = _computer_row(status="stopped")

    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.archive_workspace.return_value = {**ws_row, "archive_state": "archiving"}
    comp_mgr = create_autospec(ComputerManager, instance=True)
    comp_mgr.archive_computer.return_value = {**comp_row, "archive_state": "archiving"}

    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_row),
    )
    monkeypatch.setattr(
        workspaces_app,
        "workspace_access",
        create_autospec(workspaces_app.workspace_access, return_value=None),
    )
    monkeypatch.setattr(
        workspaces_app.WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: ws_mgr),
    )
    monkeypatch.setattr(
        computers_app,
        "_reached_computer",
        create_autospec(computers_app._reached_computer, return_value=comp_row),
    )
    monkeypatch.setattr(computers_app, "_computer_manager", lambda: comp_mgr)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r_ws = await client.post(f"/api/v1/workspaces/{WS_1}/archive")
        r_c = await client.post(f"/api/v1/computers/{COMP_A}/archive")

    assert r_ws.status_code == 200
    assert "in progress" in r_ws.json()["message"].lower()
    assert r_c.status_code == 200
    assert "in progress" in r_c.json()["message"].lower()


@pytest.mark.asyncio
async def test_try_claim_computer_for_start_sql_rejects_archiving_fence(monkeypatch):
    executed: list[str] = []

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            executed.append(sql)

        async def fetchone(self):
            return None

    @asynccontextmanager
    async def _cursor(conn=None):
        yield _Cur()

    monkeypatch.setattr(computer_db, "_computer_cursor", _cursor)
    claimed = await computer_db.try_claim_computer_for_start(COMP_A, from_status="stopped")
    assert claimed is None
    assert '{"archiving": true}' in executed[0]
    assert "NOT COALESCE(c.config" in executed[0]


@pytest.mark.asyncio
async def test_workspace_start_and_session_acquire_share_archive_reconciliation_and_block_archiving(
    monkeypatch,
):
    from src.server.app import workspaces as workspaces_app
    from src.server.services.computer_manager import _lifecycle, _machines

    # 1. _acquire_session reconciles stopped+archiving via shared _ensure_not_archiving_locked
    #    and raises ComputerArchivingError without claiming start.
    cm = object.__new__(ComputerManager)

    @asynccontextmanager
    async def _noop_lock(*_a, **_kw):
        yield

    cm._observed_lock = _noop_lock
    cm._machines = {}
    cm._machine = lambda cid: SimpleNamespace(session=None, phase2_event=None)
    cm._take_valid_cached_session = create_autospec(
        cm._take_valid_cached_session, return_value=None
    )
    cm._claim_and_restart = create_autospec(cm._claim_and_restart)
    cm.resolve_binding = create_autospec(
        cm.resolve_binding,
        return_value=SimpleNamespace(
            computer_id=COMP_A,
            workspace_id=WS_1,
            user_id=USER_ID,
            kind="daytona",
            provider_ref="sbx-1",
            provider=SimpleNamespace(recreatable=True),
        ),
    )
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    archiving_comp = _computer_row(status="stopped", config={"archiving": True})
    ws_stopped = {
        "workspace_id": WS_1,
        "user_id": USER_ID,
        "name": "NVDA",
        "status": "stopped",
        "computer_id": COMP_A,
    }
    monkeypatch.setattr(
        _lifecycle,
        "refuse_machine_use",
        create_autospec(_lifecycle.refuse_machine_use, return_value=None),
    )
    monkeypatch.setattr(
        _lifecycle,
        "db_get_workspace",
        create_autospec(_lifecycle.db_get_workspace, return_value=ws_stopped),
    )
    monkeypatch.setattr(
        _lifecycle,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=archiving_comp),
    )
    from src.dalton.services import archive_lifecycle
    monkeypatch.setattr(
        archive_lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(archive_lifecycle.computer_stop_heartbeat_present, return_value=False),
    )

    rt_archiving = create_autospec(SandboxRuntime, instance=True)
    rt_archiving.get_state.return_value = RuntimeState.ARCHIVING

    @asynccontextmanager
    async def _detached_archiving(*_a, **_kw):
        yield rt_archiving

    cm._detached_runtime = _detached_archiving

    with pytest.raises(ComputerArchivingError):
        await cm._acquire_session(WS_1, user_id=USER_ID)
    cm._claim_and_restart.assert_not_awaited()

    # 2. /api/v1/workspaces/{id}/start (including lazy=True) calls shared
    #    ensure_computer_not_archiving and returns 409 computer_archiving.
    monkeypatch.setattr(
        _machines,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=archiving_comp),
    )
    app = FastAPI()
    app.include_router(workspaces_app.router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID, auth="session", scopes=frozenset()
    )
    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_stopped),
    )
    monkeypatch.setattr(
        workspaces_app,
        "workspace_access",
        create_autospec(workspaces_app.workspace_access, return_value=None),
    )
    from src.server.services import home_placement

    monkeypatch.setattr(
        home_placement,
        "runs_in",
        create_autospec(home_placement.runs_in, return_value=WS_1),
    )
    monkeypatch.setattr(
        workspaces_app.WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: cm),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/v1/workspaces/{WS_1}/start?lazy=true")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "computer_archiving"


@pytest.mark.asyncio
async def test_stage_workspace_move_locks_workspaces_by_workspace_id(monkeypatch):
    import psycopg

    executed: list[str] = []
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    async def _exec(sql, params=None):
        executed.append(sql)

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = [
        {"source_computer_id": COMP_A, "from_backup": False},
        {"move_id": "m-1", "phase": "staged"},
    ]

    @asynccontextmanager
    async def _tx():
        yield

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.transaction.side_effect = _tx
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _get_conn():
        yield conn

    monkeypatch.setattr(move_db, "get_db_connection", _get_conn)
    monkeypatch.setattr(
        move_db,
        "computer_run_in_progress",
        create_autospec(move_db.computer_run_in_progress, return_value=False),
    )
    monkeypatch.setattr(
        move_db,
        "computer_command_running",
        create_autospec(move_db.computer_command_running, return_value=False),
    )
    res = await move_db.stage_workspace_move(
        "m-1", "runner-1", source_folder="NVDA", leftovers_path=None
    )
    assert res is not None
    ws_lock_sql = executed[1]
    assert "ORDER BY workspace_id" in ws_lock_sql
    assert "created_at" not in ws_lock_sql
    assert "FOR UPDATE" in ws_lock_sql


@pytest.mark.asyncio
async def test_archive_machine_sanitizes_provider_exception_before_span_and_caller(
    monkeypatch, caplog
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from src.observability import tracing as tracing_mod
    from src.server.services.computer_manager import _lifecycle

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("test-archive-sanitize")
    monkeypatch.setattr(tracing_mod, "tracer", test_tracer)

    cm = object.__new__(ComputerManager)
    cm._machines = {}

    @asynccontextmanager
    async def _noop_lock_inner(*_a, **_kw):
        yield

    @asynccontextmanager
    async def _noop_hb(*_a, **_kw):
        yield

    cm._acquire_machine_lock = _noop_lock_inner
    cm._observed_lock = ComputerManager._observed_lock.__get__(cm, ComputerManager)
    cm._machine_has_active_tasks = create_autospec(
        cm._machine_has_active_tasks, return_value=False
    )
    cm._claim_machine_for_archive = create_autospec(
        cm._claim_machine_for_archive,
        return_value=_computer_row(status="stopping", config={"archiving": True}),
    )
    cm._reconcile_provider_stop = create_autospec(
        cm._reconcile_provider_stop,
        side_effect=RuntimeError(FAKE_SECRET_BODY),
    )
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat", _noop_hb)

    rt_fail = create_autospec(SandboxRuntime, instance=True)
    rt_fail.archives = True
    rt_fail.archive.side_effect = SandboxTransientError(FAKE_SECRET_BODY)

    @asynccontextmanager
    async def _detached_fail(*_a, **_kw):
        yield rt_fail

    cm._detached_runtime = _detached_fail

    caplog.set_level(logging.WARNING)
    caplog.clear()
    with pytest.raises(ArchiveProviderError) as exc_info:
        await cm._archive_machine(COMP_A, workspace_id=WS_1)

    err = exc_info.value
    assert err.category == "sandbox_transient"
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
        assert secret not in str(err)

    # Verify tracing span recorded only error category and zero raw provider text.
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "computer.archive"
    assert span.attributes.get("error.category") == "sandbox_transient"
    span_blob = repr(dict(span.attributes)) + "\n" + "\n".join(
        repr(dict(ev.attributes or {})) for ev in span.events
    )
    for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
        assert secret not in span_blob

    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
        assert secret not in log_blob


@pytest.mark.asyncio
async def test_reap_stuck_stopping_logs_only_whitelisted_fields(monkeypatch, caplog):
    from src.server.services import workspace_manager as wm_mod

    wm = object.__new__(WorkspaceManager)
    old_stopping = _computer_row(status="stopping", config={"archiving": True})
    old_stopping["updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=600)

    monkeypatch.setattr(
        wm_mod,
        "get_computers_by_status",
        create_autospec(wm_mod.get_computers_by_status, return_value=[old_stopping]),
    )
    wm.reconcile_stopping_computer = create_autospec(
        wm.reconcile_stopping_computer,
        side_effect=RuntimeError(FAKE_SECRET_BODY),
    )

    caplog.set_level(logging.WARNING)
    caplog.clear()
    settled = await wm.reap_stuck_stopping_computers()
    assert settled == 0
    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "stuck_stop_reconcile_failed" in log_blob
    assert COMP_A in log_blob
    assert "exc_type=RuntimeError" in log_blob
    for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
        assert secret not in log_blob


@pytest.mark.asyncio
async def test_daytona_get_maps_404_to_sandbox_gone_and_reconcile_clears_fence(
    monkeypatch,
):
    from daytona import DaytonaNotFoundError
    from daytona._async.daytona import AsyncDaytona
    from ptc_agent.core.sandbox.providers.daytona import DaytonaProvider
    from src.server.services.computer_manager import _machines

    provider = DaytonaProvider.__new__(DaytonaProvider)
    provider._working_dir = "/home/workspace"

    client_404 = create_autospec(AsyncDaytona, instance=True)
    client_404.get.side_effect = DaytonaNotFoundError("sandbox not found", status_code=404)
    provider._client = client_404
    with pytest.raises(SandboxGoneError) as gone_exc:
        await provider.get("sbx-missing-1")
    assert gone_exc.value.sandbox_id == "sbx-missing-1"

    # End-to-end with _reconcile_stopped_archiving_locked: SandboxGoneError clears fence.
    cm = object.__new__(ComputerManager)
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    @asynccontextmanager
    async def _detached_via_provider(sandbox_id, binding=None):
        yield await provider.get(sandbox_id)

    cm._detached_runtime = _detached_via_provider
    clear_mock = create_autospec(computer_db.clear_computer_archiving, return_value=True)
    cleared_row = _computer_row(status="stopped", config={})
    monkeypatch.setattr(_machines, "clear_computer_archiving", clear_mock)
    monkeypatch.setattr(
        _machines,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=cleared_row),
    )
    from src.dalton.services import archive_lifecycle
    monkeypatch.setattr(
        archive_lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(archive_lifecycle.computer_stop_heartbeat_present, return_value=False),
    )

    res = await cm._reconcile_stopped_archiving_locked(
        _computer_row(status="stopped", config={"archiving": True})
    )
    clear_mock.assert_awaited_once_with(COMP_A)
    assert res["config"] == {}


@pytest.mark.asyncio
async def test_daytona_get_preserves_transient_error_for_retry_and_keeps_fence(
    monkeypatch,
):
    from daytona import DaytonaError
    from daytona._async.daytona import AsyncDaytona
    from ptc_agent.core.sandbox.providers.daytona import DaytonaProvider
    from src.server.services.computer_manager import _machines

    provider = DaytonaProvider.__new__(DaytonaProvider)
    provider._working_dir = "/home/workspace"

    transient_err = DaytonaError("service unavailable", status_code=503)
    client_503 = create_autospec(AsyncDaytona, instance=True)
    client_503.get.side_effect = transient_err
    provider._client = client_503
    with pytest.raises(DaytonaError) as exc_info:
        await provider.get("sbx-transient-1")
    assert not isinstance(exc_info.value, SandboxGoneError)
    assert provider.classify_error(exc_info.value) is SandboxFailureKind.TRANSIENT

    # End-to-end with _reconcile_stopped_archiving_locked: transient keeps fence for retry.
    cm = object.__new__(ComputerManager)
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    @asynccontextmanager
    async def _detached_via_provider(sandbox_id, binding=None):
        yield await provider.get(sandbox_id)

    cm._detached_runtime = _detached_via_provider
    from src.dalton.services import archive_lifecycle
    clear_mock = create_autospec(computer_db.clear_computer_archiving, return_value=True)
    monkeypatch.setattr(_machines, "clear_computer_archiving", clear_mock)
    monkeypatch.setattr(
        archive_lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(archive_lifecycle.computer_stop_heartbeat_present, return_value=False),
    )

    archiving_row = _computer_row(status="stopped", config={"archiving": True})
    res = await cm._reconcile_stopped_archiving_locked(archiving_row)
    clear_mock.assert_not_awaited()
    assert res["config"]["archiving"] is True


@pytest.mark.asyncio
async def test_direct_archive_without_snapshot_collects_readonly_snapshot_and_never_adopts_unbound(
    monkeypatch,
):
    from src.dalton.services import archive_lifecycle

    snapshot = [
        {"workspace_id": WS_1, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
    ]

    # 1. WorkspaceManager.archive_workspace without snapshot collects read-only snapshot.
    wm = object.__new__(WorkspaceManager)
    wm.resolve_binding = create_autospec(wm.resolve_binding)
    wm._archive_machine = create_autospec(
        wm._archive_machine, return_value={"workspace_id": WS_1, "archive_state": "archived"}
    )
    get_comp_mock = create_autospec(
        archive_lifecycle.get_computer_for_workspace,
        return_value=_computer_row(cid=COMP_A),
    )
    monkeypatch.setattr(archive_lifecycle, "get_computer_for_workspace", get_comp_mock)
    snap_mock = create_autospec(
        archive_lifecycle.get_live_workspaces_for_archive_snapshot, return_value=snapshot
    )
    monkeypatch.setattr(
        archive_lifecycle, "get_live_workspaces_for_archive_snapshot", snap_mock
    )

    await wm.archive_workspace(WS_1)
    snap_mock.assert_awaited_once_with(COMP_A)
    wm._archive_machine.assert_awaited_once_with(
        COMP_A,
        workspace_id=WS_1,
        expected_workspaces=snapshot,
        expected_workspace_id=WS_1,
    )
    wm.resolve_binding.assert_not_awaited()

    # 2. Unbound workspace (get_computer_for_workspace -> None) is rejected without adoption.
    wm._archive_machine.reset_mock()
    get_comp_mock.return_value = None
    with pytest.raises(ArchiveScopeChangedError):
        await wm.archive_workspace(WS_1)
    wm.resolve_binding.assert_not_awaited()
    wm._archive_machine.assert_not_awaited()

    # 3. ComputerManager.archive_computer without snapshot collects read-only snapshot.
    cm = object.__new__(ComputerManager)
    cm._archive_machine = create_autospec(
        cm._archive_machine, return_value=_computer_row(cid=COMP_A)
    )
    snap_mock.reset_mock()
    await cm.archive_computer(COMP_A)
    snap_mock.assert_awaited_once_with(COMP_A)
    cm._archive_machine.assert_awaited_once_with(
        COMP_A, expected_workspaces=snapshot
    )


@pytest.mark.asyncio
async def test_reconcile_stopped_archiving_keeps_fence_when_heartbeat_present(
    monkeypatch,
):
    from src.dalton.services import archive_lifecycle
    from src.server.services.computer_manager import _machines

    cm = object.__new__(ComputerManager)
    clear_mock = create_autospec(computer_db.clear_computer_archiving, return_value=True)
    monkeypatch.setattr(_machines, "clear_computer_archiving", clear_mock)
    monkeypatch.setattr(
        archive_lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(archive_lifecycle.computer_stop_heartbeat_present, return_value=True),
    )
    archiving_row = _computer_row(status="stopped", config={"archiving": True})
    res = await cm._reconcile_stopped_archiving_locked(archiving_row)
    assert res["config"]["archiving"] is True
    clear_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_approval_computer_archive_binds_snapshot_into_digest_and_unbound_rejected(
    monkeypatch,
):
    from src.server.app import workspaces as workspaces_app
    from src.server.dependencies import cli_approval

    captured_asks = []

    async def _fake_gate(request, principal, build):
        captured_asks.append(await build(None))

    monkeypatch.setattr(cli_approval, "gate", _fake_gate)
    snap_a = [
        {"workspace_id": WS_1, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
    ]
    snap_b = [
        {"workspace_id": WS_1, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
        {"workspace_id": WS_2, "name": "AAPL", "dir_name": "AAPL", "status": "stopped", "is_stand_in": False},
    ]
    principal = Principal(
        user_id=USER_ID,
        auth="computer",
        scopes=frozenset({"workspace:write"}),
        computer_id=COMP_B,
    )
    comp = _computer_row(cid=COMP_A)
    await cli_approval.computer_archive(
        None, principal, comp, workspace_id=WS_1, expected_workspaces=snap_a
    )
    await cli_approval.computer_archive(
        None, principal, comp, workspace_id=WS_1, expected_workspaces=snap_b
    )
    assert len(captured_asks) == 2
    assert captured_asks[0].intent["acts_on"]["computer_id"] == COMP_A
    assert captured_asks[0].intent["acts_on"]["expected_workspaces"] == snap_a
    assert captured_asks[0].digest != captured_asks[1].digest

    # Unbound workspace via CLI token on /workspaces/{id}/archive -> 409 scope_changed (never skipped/adopted).
    app = FastAPI()
    app.include_router(workspaces_app.router)
    app.dependency_overrides[get_principal] = lambda: principal
    ws_unbound = {
        "workspace_id": WS_1,
        "user_id": USER_ID,
        "name": "Home",
        "status": "unbound",
        "computer_id": None,
    }
    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_unbound),
    )
    monkeypatch.setattr(
        workspaces_app,
        "workspace_access",
        create_autospec(workspaces_app.workspace_access, return_value=None),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(computer_db.get_computer_for_workspace, return_value=None),
    )
    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    monkeypatch.setattr(
        workspaces_app.WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: ws_mgr),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/v1/workspaces/{WS_1}/archive")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "scope_changed"
    ws_mgr.archive_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_recovery_does_not_take_over_fresh_claim_before_heartbeat_created(
    monkeypatch,
):
    """After claim commit and before Redis heartbeat is created, startup recovery must NOT take over."""
    from src.dalton.services import archive_lifecycle
    from src.server.services.computer_manager import _lifecycle

    wm = object.__new__(WorkspaceManager)
    now = datetime.now(timezone.utc)
    fresh_claim = _computer_row(
        status="stopping",
        config={
            "archiving": True,
            "archiving_owner": "owner-fresh-1",
            "archiving_claimed_at": now.isoformat(),
        },
    )
    fresh_claim["updated_at"] = now

    list_mock = create_autospec(
        computer_db.get_archiving_computers_by_status, return_value=[fresh_claim]
    )
    reset_mock = create_autospec(
        computer_db.reset_abandoned_archiving_computer, return_value=fresh_claim
    )
    hb_mock = create_autospec(
        archive_lifecycle.computer_stop_heartbeat_present, return_value=False
    )
    monkeypatch.setattr(archive_lifecycle, "get_archiving_computers_by_status", list_mock)
    monkeypatch.setattr(archive_lifecycle, "reset_abandoned_archiving_computer", reset_mock)
    monkeypatch.setattr(archive_lifecycle, "computer_stop_heartbeat_present", hb_mock)

    # 1. Startup recovery sees heartbeat=False, but claim is within STOP_HEARTBEAT_TTL_S -> skip!
    assert await wm.recover_archiving_computers_on_startup() == 0
    reset_mock.assert_not_awaited()

    # 2. _correct_stuck_stopping also respects the same claim grace window.
    cm = object.__new__(ComputerManager)
    cm._reconcile_provider_stop = create_autospec(cm._reconcile_provider_stop)
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat_present", hb_mock)
    monkeypatch.setattr(
        _lifecycle,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=fresh_claim),
    )
    binding = SimpleNamespace(
        computer_id=COMP_A,
        workspace_id=WS_1,
        provider_ref="sbx-daytona-1",
        provider=SimpleNamespace(recreatable=True),
    )
    await cm._correct_stuck_stopping(binding)
    cm._reconcile_provider_stop.assert_not_awaited()

    # 3. _archive_machine re-verifies claim ownership before calling provider; lost claim aborts.
    cm._machines = {}

    @asynccontextmanager
    async def _noop_lock(*_a, **_kw):
        yield

    @asynccontextmanager
    async def _noop_hb(*_a, **_kw):
        yield

    cm._observed_lock = _noop_lock
    cm._machine_has_active_tasks = create_autospec(
        cm._machine_has_active_tasks, return_value=False
    )
    cm._claim_machine_for_archive = create_autospec(
        cm._claim_machine_for_archive, return_value=fresh_claim
    )
    cm._retain_machine_archive_claim = create_autospec(
        cm._retain_machine_archive_claim, return_value=False
    )
    cm._binding_from_computer = lambda ws_id, row: binding
    rt = create_autospec(SandboxRuntime, instance=True)
    rt.archives = True

    @asynccontextmanager
    async def _detached(*_a, **_kw):
        yield rt

    cm._detached_runtime = _detached
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat", _noop_hb)

    with pytest.raises(ComputerBusyError, match="archive claim was lost"):
        await cm._archive_machine(COMP_A, workspace_id=WS_1)
    cm._retain_machine_archive_claim.assert_awaited_once_with(
        COMP_A, archiving_owner="owner-fresh-1"
    )
    rt.archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_stopping_computer_sanitizes_raw_exception_before_real_tracer_span(
    monkeypatch, caplog
):
    """Real reconcile_stopping_computer + real OTel TracerProvider + real Daytona SDK get() leak zero raw text."""
    import daytona._utils.otel_decorator as daytona_otel
    from daytona._async.daytona import AsyncDaytona
    from daytona_api_client_async.api.sandbox_api import SandboxApi
    from daytona_api_client_async.exceptions import ApiException
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from ptc_agent.core.sandbox.providers.daytona import DaytonaProvider
    from src.observability import tracing as tracing_mod
    from src.server.services import workspace_manager as wm_mod
    from src.server.services.computer_manager import _lifecycle

    exporter = InMemorySpanExporter()
    provider_tp = TracerProvider()
    provider_tp.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing_mod, "tracer", provider_tp.get_tracer("test-reconcile"))
    monkeypatch.setattr(
        daytona_otel, "_tracer", provider_tp.get_tracer("daytona-sdk-python")
    )

    # Real locked AsyncDaytona.get() raising a 503 ApiException carrying secrets.
    sdk_client = AsyncDaytona.__new__(AsyncDaytona)
    sdk_api = create_autospec(SandboxApi, instance=True)
    sdk_api.get_sandbox.side_effect = ApiException(status=503, body=FAKE_SECRET_BODY)
    sdk_client._sandbox_api = sdk_api

    daytona_provider = DaytonaProvider.__new__(DaytonaProvider)
    daytona_provider._working_dir = "/home/workspace"
    daytona_provider._client = sdk_client

    wm = object.__new__(WorkspaceManager)
    wm._machines = {}

    @asynccontextmanager
    async def _noop_lock_inner(*_a, **_kw):
        yield

    wm._acquire_machine_lock = _noop_lock_inner
    wm._observed_lock = ComputerManager._observed_lock.__get__(wm, ComputerManager)
    wm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    @asynccontextmanager
    async def _detached_real_daytona(sandbox_id, binding=None):
        yield await daytona_provider.get(sandbox_id)

    wm._detached_runtime = _detached_real_daytona

    old_ts = datetime.now(timezone.utc) - timedelta(seconds=600)
    old_stopping = _computer_row(
        status="stopping",
        config={
            "archiving": True,
            "archiving_owner": "owner-old-1",
            "archiving_claimed_at": old_ts.isoformat(),
        },
    )
    old_stopping["updated_at"] = old_ts

    monkeypatch.setattr(
        wm_mod,
        "get_computers_by_status",
        create_autospec(wm_mod.get_computers_by_status, return_value=[old_stopping]),
    )
    monkeypatch.setattr(
        _lifecycle,
        "get_computer",
        create_autospec(computer_db.get_computer, return_value=old_stopping),
    )
    monkeypatch.setattr(
        _lifecycle,
        "computer_stop_heartbeat_present",
        create_autospec(_lifecycle.computer_stop_heartbeat_present, return_value=False),
    )

    caplog.set_level(logging.WARNING)
    caplog.clear()
    settled = await wm.reap_stuck_stopping_computers()
    assert settled == 0
    sdk_api.get_sandbox.assert_awaited_once()

    spans = exporter.get_finished_spans()
    assert any(sp.name == "computer.stopping.reconcile" for sp in spans)
    for sp in spans:
        blob = (
            repr(dict(sp.attributes))
            + "\n"
            + repr(sp.status.description)
            + "\n"
            + "\n".join(repr(dict(ev.attributes or {})) for ev in sp.events)
        )
        for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
            assert secret not in blob, f"Leaked {secret!r} in span {sp.name}: {blob}"
    reconcile_span = [sp for sp in spans if sp.name == "computer.stopping.reconcile"][0]
    assert reconcile_span.attributes.get("error.category") == "sandbox_transient"

    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
        assert secret not in log_blob


@pytest.mark.asyncio
async def test_archive_machine_real_daytona_sdk_sanitizes_all_exported_spans(
    monkeypatch, caplog
):
    """Real locked Daytona SDK AsyncSandbox.archive() failing with secrets leaks nothing into ANY span."""
    import daytona._utils.otel_decorator as daytona_otel
    from daytona.internal.event_subscription_manager import AsyncEventSubscriptionManager
    from daytona._async.sandbox import AsyncSandbox
    from daytona_api_client_async.api.sandbox_api import SandboxApi
    from daytona_api_client_async.exceptions import ApiException
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from ptc_agent.core.sandbox.providers.daytona_runtime import DaytonaRuntime
    from src.observability import tracing as tracing_mod
    from src.server.services.computer_manager import _lifecycle

    exporter = InMemorySpanExporter()
    provider_tp = TracerProvider()
    provider_tp.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing_mod, "tracer", provider_tp.get_tracer("test-archive-sdk"))
    monkeypatch.setattr(
        daytona_otel, "_tracer", provider_tp.get_tracer("daytona-sdk-python")
    )

    sdk_sandbox = AsyncSandbox.model_construct(id="sbx-daytona-1")
    sdk_api = create_autospec(SandboxApi, instance=True)
    sdk_api.archive_sandbox.side_effect = ApiException(status=503, body=FAKE_SECRET_BODY)
    sub_mgr = create_autospec(AsyncEventSubscriptionManager, instance=True)
    sub_mgr.subscribe.return_value = "sub-1"
    object.__setattr__(sdk_sandbox, "_sandbox_api", sdk_api)
    object.__setattr__(sdk_sandbox, "_subscription_manager", sub_mgr)
    object.__setattr__(sdk_sandbox, "_sub_id", None)
    real_runtime = DaytonaRuntime(sdk_sandbox)

    cm = object.__new__(ComputerManager)
    cm._machines = {}

    @asynccontextmanager
    async def _noop_lock_inner(*_a, **_kw):
        yield

    @asynccontextmanager
    async def _noop_hb(*_a, **_kw):
        yield

    cm._acquire_machine_lock = _noop_lock_inner
    cm._observed_lock = ComputerManager._observed_lock.__get__(cm, ComputerManager)
    cm._machine_has_active_tasks = create_autospec(
        cm._machine_has_active_tasks, return_value=False
    )
    claimed = _computer_row(
        status="stopping",
        config={
            "archiving": True,
            "archiving_owner": "owner-sdk-1",
            "archiving_claimed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    cm._claim_machine_for_archive = create_autospec(
        cm._claim_machine_for_archive, return_value=claimed
    )
    cm._retain_machine_archive_claim = create_autospec(
        cm._retain_machine_archive_claim, return_value=True
    )
    cm._reconcile_provider_stop = create_autospec(
        cm._reconcile_provider_stop, side_effect=RuntimeError(FAKE_SECRET_BODY)
    )
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    @asynccontextmanager
    async def _detached_real(*_a, **_kw):
        yield real_runtime

    cm._detached_runtime = _detached_real
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat", _noop_hb)

    caplog.set_level(logging.WARNING)
    caplog.clear()
    with pytest.raises(ArchiveProviderError) as exc_info:
        await cm._archive_machine(COMP_A, workspace_id=WS_1)

    assert exc_info.value.category == "sandbox_transient"
    assert exc_info.value.__cause__ is None
    sdk_api.archive_sandbox.assert_awaited_once()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "computer.archive"
    assert spans[0].attributes.get("error.category") == "sandbox_transient"
    for sp in spans:
        blob = (
            repr(dict(sp.attributes))
            + "\n"
            + repr(sp.status.description)
            + "\n"
            + "\n".join(repr(dict(ev.attributes or {})) for ev in sp.events)
        )
        for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
            assert secret not in blob, f"Leaked {secret!r} in span {sp.name}: {blob}"


@pytest.mark.asyncio
async def test_archive_machine_daytona_sdk_post_submit_refresh_404_succeeds_and_clears_fence(
    monkeypatch,
):
    """Locked Daytona SDK archive() submits POST then calls refresh_data(); 404 on refresh_data settles archived."""
    import daytona._utils.otel_decorator as daytona_otel
    from daytona.internal.event_subscription_manager import AsyncEventSubscriptionManager
    from daytona._async.sandbox import AsyncSandbox
    from daytona_api_client_async.api.sandbox_api import SandboxApi
    from daytona_api_client_async.exceptions import NotFoundException
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from ptc_agent.core.sandbox.providers.daytona_runtime import DaytonaRuntime
    from src.observability import tracing as tracing_mod
    from src.server.services.computer_manager import _lifecycle

    exporter = InMemorySpanExporter()
    provider_tp = TracerProvider()
    provider_tp.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing_mod, "tracer", provider_tp.get_tracer("test-archive-404"))
    monkeypatch.setattr(
        daytona_otel, "_tracer", provider_tp.get_tracer("daytona-sdk-python")
    )

    # 1. Post-submit refresh 404: archive_sandbox succeeds, get_sandbox (in refresh_data) returns 404.
    sdk_sandbox = AsyncSandbox.model_construct(id="sbx-daytona-1")
    sdk_api = create_autospec(SandboxApi, instance=True)
    sdk_api.archive_sandbox.return_value = None
    sdk_api.get_sandbox.side_effect = NotFoundException(
        status=404, body=FAKE_SECRET_BODY
    )
    sub_mgr = create_autospec(AsyncEventSubscriptionManager, instance=True)
    sub_mgr.subscribe.return_value = "sub-1"
    object.__setattr__(sdk_sandbox, "_sandbox_api", sdk_api)
    object.__setattr__(sdk_sandbox, "_subscription_manager", sub_mgr)
    object.__setattr__(sdk_sandbox, "_sub_id", None)
    real_runtime = DaytonaRuntime(sdk_sandbox)

    cm = object.__new__(ComputerManager)
    cm._machines = {}

    @asynccontextmanager
    async def _noop_lock_inner(*_a, **_kw):
        yield

    @asynccontextmanager
    async def _noop_hb(*_a, **_kw):
        yield

    cm._acquire_machine_lock = _noop_lock_inner
    cm._observed_lock = ComputerManager._observed_lock.__get__(cm, ComputerManager)
    cm._machine_has_active_tasks = create_autospec(
        cm._machine_has_active_tasks, return_value=False
    )
    claimed = _computer_row(
        status="stopping",
        config={
            "archiving": True,
            "archiving_owner": "owner-404-1",
            "archiving_claimed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    cm._claim_machine_for_archive = create_autospec(
        cm._claim_machine_for_archive, return_value=claimed
    )
    cm._retain_machine_archive_claim = create_autospec(
        cm._retain_machine_archive_claim, return_value=True
    )
    settle_mock = create_autospec(cm._settle_machine_stop)
    cm._settle_machine_stop = settle_mock
    cm._stop_result = create_autospec(
        cm._stop_result, return_value=_computer_row(status="stopped")
    )
    cm._binding_from_computer = lambda ws_id, row: SimpleNamespace(
        computer_id=str(row["computer_id"]),
        workspace_id=ws_id,
        provider_ref=row.get("provider_ref"),
        provider=SimpleNamespace(recreatable=True),
    )

    @asynccontextmanager
    async def _detached_real(*_a, **_kw):
        yield real_runtime

    cm._detached_runtime = _detached_real
    monkeypatch.setattr(_lifecycle, "computer_stop_heartbeat", _noop_hb)

    result = await cm._archive_machine(COMP_A, workspace_id=WS_1)
    assert result["archive_state"] == "archived"
    sdk_api.archive_sandbox.assert_awaited_once()
    sdk_api.get_sandbox.assert_awaited_once()
    settle_mock.assert_awaited_once_with(
        COMP_A,
        "stopped",
        clear_archiving=True,
        expected_archiving_owner="owner-404-1",
    )
    for sp in exporter.get_finished_spans():
        blob = (
            repr(dict(sp.attributes))
            + "\n"
            + repr(sp.status.description)
            + "\n"
            + "\n".join(repr(dict(ev.attributes or {})) for ev in sp.events)
        )
        for secret in ("DO_NOT_LOG", "sk-proj_", "s3cr3tP4ss", "AIzaSyA", "eyJhbGci"):
            assert secret not in blob

    # 2. Pre-submit 404: archive_sandbox itself returns 404 -> still fails as sandbox_gone (not false success).
    sdk_api2 = create_autospec(SandboxApi, instance=True)
    sdk_api2.archive_sandbox.side_effect = NotFoundException(
        status=404, body=FAKE_SECRET_BODY
    )
    sdk_sandbox2 = AsyncSandbox.model_construct(id="sbx-daytona-1")
    object.__setattr__(sdk_sandbox2, "_sandbox_api", sdk_api2)
    object.__setattr__(sdk_sandbox2, "_subscription_manager", sub_mgr)
    object.__setattr__(sdk_sandbox2, "_sub_id", None)
    real_runtime2 = DaytonaRuntime(sdk_sandbox2)
    settle_mock.reset_mock()
    cm._reconcile_provider_stop = create_autospec(cm._reconcile_provider_stop)

    @asynccontextmanager
    async def _detached_pre404(*_a, **_kw):
        yield real_runtime2

    cm._detached_runtime = _detached_pre404
    with pytest.raises(ArchiveProviderError) as pre_exc:
        await cm._archive_machine(COMP_A, workspace_id=WS_1)
    assert pre_exc.value.category == "sandbox_gone"
    sdk_api2.archive_sandbox.assert_awaited_once()
    sdk_api2.get_sandbox.assert_not_awaited()
    cm._reconcile_provider_stop.assert_awaited_once()
