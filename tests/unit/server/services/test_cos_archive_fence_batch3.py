"""Unit tests for Batch 3: archive fences, lock order, terminal-only clear, startup recovery, and local HTTP error capture."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
    ArchiveScopeChangedError,
    ArchiveUnsupportedError,
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
    assert update_mock.await_args.kwargs["merge_config"] == {"archiving": True}
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
    settle_mock.assert_awaited_once_with(
        COMP_A, expected_status, clear_archiving=expected_clear
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
    clear_mock = create_autospec(computer_db.clear_computer_archiving, return_value=True)
    monkeypatch.setattr(_machines, "clear_computer_archiving", clear_mock)

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
async def test_startup_recovery_resets_archiving_computers_to_stopped_preserving_fence(
    monkeypatch,
):
    from src.server.services import workspace_manager as wm_mod

    wm = object.__new__(WorkspaceManager)
    stuck_row = _computer_row(status="stopping", config={"archiving": True})
    reset_row = _computer_row(status="stopped", config={"archiving": True})

    list_mock = create_autospec(
        computer_db.get_archiving_computers_by_status, return_value=[stuck_row]
    )
    update_mock = create_autospec(
        computer_db.update_computer_status, return_value=reset_row
    )
    monkeypatch.setattr(wm_mod, "get_archiving_computers_by_status", list_mock)
    monkeypatch.setattr(wm_mod, "update_computer_status", update_mock)

    count = await wm.recover_archiving_computers_on_startup()
    assert count == 1
    list_mock.assert_awaited_once_with("stopping", limit=1000)
    update_mock.assert_awaited_once_with(COMP_A, "stopped", expected="stopping")


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
    approval_mock = create_autospec(workspaces_app.cli_approval.computer_archive)
    monkeypatch.setattr(workspaces_app.cli_approval, "computer_archive", approval_mock)
    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.archive_workspace.return_value = ws_row
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
