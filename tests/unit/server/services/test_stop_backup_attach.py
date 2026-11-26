"""Reproduction & regression tests for BY4:

1. Stop-time backup on a session-less worker temporarily attaches a running-only
   sandbox session (with configurable timeout, default 60s), releases it after
   backup, falls back cleanly on timeout/failure, records skipped workspace
   count in stop result and logs, and keeps ``backup_project_files`` non-strict
   contract unchanged.
2. Concurrent orphan cleanup (``_restamp_dirs`` or a concurrent sweeping
   ``pull_direct``) racing with ``_restore_relay`` upload/placement, and ambient
   ``ProjectContext`` folding during relay staging.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

from ptc_agent.config.agent import AgentConfig
from ptc_agent.config.core import (
    CoreConfig,
    DockerConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.project_context import ProjectContext, project_bound
from ptc_agent.core.sandbox import wsfiles_transfer_runtime as rt
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import (
    RuntimeState,
    SandboxProvider,
    SandboxRuntime,
)
from src.dalton.services.stop_backup import attach_stop_backup_session
from src.server.services.computer_manager import ComputerBinding, ComputerManager
from src.server.services.persistence import restore, transfer
from src.server.services.persistence.sync_result import SyncResult
from src.server.services.workspace_manager import WorkspaceManager

COMPUTER_ID = "22222222-2222-4222-8222-222222222222"
SANDBOX_ID = "sandbox-by4-test"

_LIFECYCLE = "src.server.services.computer_manager._lifecycle"
_MACHINES = "src.server.services.computer_manager._machines"
_MACHINE_BACKUP = "src.server.services.computer_manager._machine_backup"
_PROVISIONING = "src.server.services.computer_manager._provisioning"


def _make_agent_config(working_dir: str = "/home/workspace") -> AgentConfig:
    core = CoreConfig(
        sandbox=SandboxConfig(
            provider="docker",
            docker=DockerConfig(),
        ),
        filesystem=FilesystemConfig(
            working_directory=working_dir,
            allowed_directories=[working_dir, "/tmp"],
        ),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        mcp=MCPConfig(servers=[]),
    )
    cfg = create_autospec(AgentConfig, instance=True)
    cfg.sandbox = core.sandbox
    cfg.filesystem = core.filesystem
    cfg.to_core_config.side_effect = lambda: core.model_copy(deep=True)
    return cfg


def _make_binding(
    workspace_id: str = "ws-1", *, kind: str = "docker"
) -> ComputerBinding:
    return ComputerBinding(
        workspace_id=workspace_id,
        computer_id=COMPUTER_ID,
        kind=kind,
        root_dir="/home/workspace",
        provider_ref=SANDBOX_ID,
        files_backup=True,
    )


def _make_provider_and_runtime(
    *,
    state: RuntimeState = RuntimeState.RUNNING,
    work_dir: str = "/home/workspace",
    get_side_effect=None,
) -> tuple[SandboxProvider, SandboxRuntime]:
    runtime = create_autospec(SandboxRuntime, instance=True)
    runtime.id = SANDBOX_ID
    runtime.working_dir = work_dir
    runtime.get_state = AsyncMock(return_value=state)
    runtime.fetch_working_dir = AsyncMock(return_value=work_dir)
    runtime.start = AsyncMock()
    runtime.stop = AsyncMock()

    provider = create_autospec(SandboxProvider, instance=True)
    if get_side_effect is not None:
        provider.get = AsyncMock(side_effect=get_side_effect)
    else:
        provider.get = AsyncMock(return_value=runtime)
    provider.close = AsyncMock()
    provider.is_host_unavailable.return_value = False
    provider.is_transient_error.return_value = False
    return provider, runtime


def _setup_manager_for_stop(
    ws_ids: list[str],
    provider: SandboxProvider,
) -> WorkspaceManager:
    ComputerManager.reset_instance()
    manager = WorkspaceManager(_make_agent_config())
    manager._provider_for = lambda _binding: provider
    manager._machine_has_active_tasks = AsyncMock(return_value=False)
    manager._retain_machine_stop_claim = AsyncMock(return_value=True)
    manager._settle_machine_stop = AsyncMock()
    manager._detached_sandbox_teardown = AsyncMock()
    manager.refresh_computer_disk = AsyncMock(return_value=(None, None))
    manager._stop_result = AsyncMock(
        return_value={"computer_id": COMPUTER_ID, "status": "stopped"}
    )

    @asynccontextmanager
    async def _held_folder(_ws_id: str):
        yield f"dir-{_ws_id}"

    manager._held_workspace_folder = _held_folder
    manager.resolve_binding = AsyncMock(
        side_effect=lambda ws_id, **_: _make_binding(ws_id)
    )
    return manager


class TestStopBackupTemporaryAttach:
    """BY4 Issue 1: session-less worker stop-time backup."""

    def teardown_method(self):
        ComputerManager.reset_instance()

    @pytest.mark.asyncio
    async def test_sessionless_stop_temporarily_attaches_mirrors_all_and_releases(
        self, caplog
    ):
        """A worker with no cached session attaches temporarily for stop backup,
        mirrors every workspace on the running sandbox, releases the temporary
        provider/session afterward, and reports 0 skipped workspaces."""
        ws_ids = [f"ws-{i}" for i in range(3)]
        provider, runtime = _make_provider_and_runtime(state=RuntimeState.RUNNING)
        manager = _setup_manager_for_stop(ws_ids, provider)
        assert manager._cached_session(COMPUTER_ID) is None

        synced_workspaces: list[tuple[str, str]] = []

        async def fake_sync_to_db(workspace_id, sandbox, *, layout, routine=False):
            assert sandbox.runtime is runtime
            assert sandbox.sandbox_id == SANDBOX_ID
            assert routine is True
            synced_workspaces.append((workspace_id, layout.workspace))
            return SyncResult(synced=1)

        with (
            caplog.at_level(logging.INFO),
            patch(
                f"{_MACHINE_BACKUP}.get_live_workspace_ids_for_computer",
                AsyncMock(return_value=ws_ids),
            ),
            patch(
                f"{_PROVISIONING}.FilePersistenceService.sync_to_db",
                AsyncMock(side_effect=fake_sync_to_db),
            ),
            patch(f"{_LIFECYCLE}.note_stop_phases"),
        ):
            result = await manager._finish_claimed_stop(
                _make_binding(ws_ids[0]),
                workspace_id=ws_ids[0],
                durable_sandbox_id=SANDBOX_ID,
            )

        assert [ws for ws, _ in synced_workspaces] == ws_ids
        assert result["status"] == "stopped"
        assert result["backup_skipped_workspaces"] == 0
        # Temporary session was released and never left cached on the worker.
        provider.close.assert_awaited_once()
        assert manager._cached_session(COMPUTER_ID) is None
        runtime.start.assert_not_awaited()
        assert any("skipped 0" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_sessionless_stop_attach_timeout_falls_back_and_records_skipped(
        self, monkeypatch, caplog
    ):
        """When temporary attach exceeds the configured timeout, stop continues,
        preserves the previous mirror, releases the provider, and records the
        skipped workspace count in both the stop result and logs."""
        ws_ids = [f"ws-{i}" for i in range(4)]

        async def slow_get(_sandbox_id: str):
            await asyncio.sleep(5.0)

        provider, _runtime = _make_provider_and_runtime(get_side_effect=slow_get)
        manager = _setup_manager_for_stop(ws_ids, provider)
        monkeypatch.setenv("DALTON_STOP_BACKUP_ATTACH_TIMEOUT_S", "0.05")

        with (
            caplog.at_level(logging.INFO),
            patch(
                f"{_MACHINE_BACKUP}.get_live_workspace_ids_for_computer",
                AsyncMock(return_value=ws_ids),
            ),
            patch(f"{_LIFECYCLE}.note_stop_phases"),
        ):
            result = await manager._finish_claimed_stop(
                _make_binding(ws_ids[0]),
                workspace_id=ws_ids[0],
                durable_sandbox_id=SANDBOX_ID,
            )

        assert result["status"] == "stopped"
        assert result["backup_skipped_workspaces"] == 4
        provider.close.assert_awaited_once()
        manager._detached_sandbox_teardown.assert_awaited_once()
        manager._settle_machine_stop.assert_awaited_once()
        assert any("timed out" in rec.getMessage() for rec in caplog.records)
        assert any("skipped 4" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_sessionless_stop_attach_failure_falls_back_and_records_skipped(
        self, caplog
    ):
        """When temporary attach raises an error, stop logs, skips backup, and
        records skipped workspace count in result and log."""
        ws_ids = ["ws-a", "ws-b"]
        provider, _runtime = _make_provider_and_runtime(
            get_side_effect=ConnectionError("docker daemon unreachable")
        )
        manager = _setup_manager_for_stop(ws_ids, provider)

        with (
            caplog.at_level(logging.INFO),
            patch(
                f"{_MACHINE_BACKUP}.get_live_workspace_ids_for_computer",
                AsyncMock(return_value=ws_ids),
            ),
            patch(f"{_LIFECYCLE}.note_stop_phases"),
        ):
            result = await manager._finish_claimed_stop(
                _make_binding(ws_ids[0]),
                workspace_id=ws_ids[0],
                durable_sandbox_id=SANDBOX_ID,
            )

        assert result["status"] == "stopped"
        assert result["backup_skipped_workspaces"] == 2
        provider.close.assert_awaited_once()
        assert any("skipped 2" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_sessionless_stop_does_not_start_non_running_sandbox(self, caplog):
        """If the sandbox is already stopped, temporary attach must NOT start it;
        it closes the provider, skips backup, and records skipped count."""
        ws_ids = ["ws-a", "ws-b"]
        provider, runtime = _make_provider_and_runtime(state=RuntimeState.STOPPED)
        manager = _setup_manager_for_stop(ws_ids, provider)

        with (
            caplog.at_level(logging.INFO),
            patch(
                f"{_MACHINE_BACKUP}.get_live_workspace_ids_for_computer",
                AsyncMock(return_value=ws_ids),
            ),
            patch(f"{_LIFECYCLE}.note_stop_phases"),
        ):
            result = await manager._finish_claimed_stop(
                _make_binding(ws_ids[0]),
                workspace_id=ws_ids[0],
                durable_sandbox_id=SANDBOX_ID,
            )

        runtime.start.assert_not_awaited()
        provider.close.assert_awaited_once()
        assert result["backup_skipped_workspaces"] == 2

    @pytest.mark.asyncio
    async def test_sessionless_stop_non_running_near_timeout_does_not_double_yield(
        self, monkeypatch, caplog
    ):
        """When provider.get() returns a non-RUNNING runtime just before the
        attach timeout expires and the caller's subsequent backup await runs
        past that timeout window (or raises TimeoutError), stop continues with
        backup_skipped_workspaces and never raises RuntimeError from a second
        context-manager yield."""
        ws_ids = ["ws-a", "ws-b", "ws-c"]
        runtime = create_autospec(SandboxRuntime, instance=True)
        runtime.id = SANDBOX_ID
        runtime.working_dir = "/home/workspace"
        runtime.get_state = AsyncMock(return_value=RuntimeState.STOPPED)
        runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        runtime.start = AsyncMock()

        async def slow_non_running_get(_sandbox_id: str):
            await asyncio.sleep(0.05)
            return runtime

        provider, _ = _make_provider_and_runtime(get_side_effect=slow_non_running_get)
        manager = _setup_manager_for_stop(ws_ids, provider)
        monkeypatch.setenv("DALTON_STOP_BACKUP_ATTACH_TIMEOUT_S", "0.12")

        async def slow_workspace_lookup(_computer_id: str):
            # Exceeds the 0.12s attach timeout budget (0.05s + 0.15s > 0.12s).
            await asyncio.sleep(0.15)
            return ws_ids

        with (
            caplog.at_level(logging.INFO),
            patch(
                f"{_MACHINE_BACKUP}.get_live_workspace_ids_for_computer",
                AsyncMock(side_effect=slow_workspace_lookup),
            ),
            patch(f"{_LIFECYCLE}.note_stop_phases"),
        ):
            result = await manager._finish_claimed_stop(
                _make_binding(ws_ids[0]),
                workspace_id=ws_ids[0],
                durable_sandbox_id=SANDBOX_ID,
            )

        assert result["status"] == "stopped"
        assert result["backup_skipped_workspaces"] == 3
        provider.close.assert_awaited_once()
        runtime.start.assert_not_awaited()
        manager._detached_sandbox_teardown.assert_awaited_once()
        manager._settle_machine_stop.assert_awaited_once()
        assert not any("timed out" in rec.getMessage() for rec in caplog.records)

        # Also verify caller TimeoutError inside the context manager propagates
        # directly (never caught/re-yielded as RuntimeError) across all three
        # fallback branches: non-running, attach timeout, and connect error.
        async def slow_branch_get(_sid: str):
            await asyncio.sleep(5.0)

        for branch_provider in (
            _make_provider_and_runtime(state=RuntimeState.STOPPED)[0],
            _make_provider_and_runtime(get_side_effect=slow_branch_get)[0],
            _make_provider_and_runtime(
                get_side_effect=ConnectionError("daemon down")
            )[0],
        ):
            branch_mgr = _setup_manager_for_stop(ws_ids, branch_provider)
            with pytest.raises(TimeoutError, match="caller backup timeout"):
                async with attach_stop_backup_session(
                    branch_mgr,
                    _make_binding(ws_ids[0]),
                    SANDBOX_ID,
                    timeout_s=0.05,
                ) as temp_session:
                    assert temp_session is None
                    raise TimeoutError("caller backup timeout")
            branch_provider.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owner_machine_non_transient_probe_error_rethrows_without_unbound_local(
        self, caplog
    ):
        """On SSH/local computers (recreatable=False), a non-transient error
        during the pre-backup _owner_machine_state probe must propagate the
        original stop error cleanly without hitting UnboundLocalError."""
        provider, _ = _make_provider_and_runtime(
            get_side_effect=RuntimeError("ssh control plane failure")
        )
        manager = _setup_manager_for_stop(["ws-1"], provider)
        binding = _make_binding("ws-1", kind="ssh")
        assert binding.provider.recreatable is False

        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(RuntimeError, match="ssh control plane failure"),
        ):
            await manager._finish_claimed_stop(
                binding,
                workspace_id="ws-1",
                durable_sandbox_id=SANDBOX_ID,
            )

        assert not any(
            "UnboundLocalError" in (rec.exc_text or rec.getMessage())
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_owner_machine_probe_error_reconciled_without_unbound_local(
        self, caplog
    ):
        """If the pre-backup probe on an SSH/local computer raises a
        non-transient error and _reconcile_provider_stop subsequently settles
        the stop, reading backup_skipped_workspaces must not raise
        UnboundLocalError."""
        calls = 0

        async def fail_then_timeout(_sandbox_id: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("ssh control plane failure")
            raise TimeoutError("machine went unreachable")

        provider, _ = _make_provider_and_runtime(get_side_effect=fail_then_timeout)
        manager = _setup_manager_for_stop(["ws-1"], provider)
        binding = _make_binding("ws-1", kind="ssh")
        assert binding.provider.recreatable is False

        with caplog.at_level(logging.WARNING):
            result = await manager._finish_claimed_stop(
                binding,
                workspace_id="ws-1",
                durable_sandbox_id=SANDBOX_ID,
            )

        assert result == {"computer_id": COMPUTER_ID, "status": "stopped"}
        assert "backup_skipped_workspaces" not in result
        assert not any(
            "Could not reconcile failed stop" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_backup_project_files_non_strict_contract_unchanged(self):
        """Upstream contract: direct non-strict backup_project_files without a
        session still returns False without attaching a sandbox."""
        provider, _runtime = _make_provider_and_runtime(state=RuntimeState.RUNNING)
        manager = _setup_manager_for_stop(["ws-1"], provider)

        mirrored = await manager.backup_project_files(
            "ws-1",
            computer_id=COMPUTER_ID,
            strict=False,
            expected_sandbox_id=SANDBOX_ID,
            routine=True,
        )

        assert mirrored is False
        provider.get.assert_not_awaited()


class TestRelayStagingConcurrentCleanup:
    """BY4 Issue 2 (server side): concurrent cleanup and ambient ProjectContext."""

    @pytest.mark.asyncio
    async def test_concurrent_cleanup_pull_during_relay_restore_succeeds(
        self, tmp_path: Path
    ):
        """When a concurrent orphan-sweeping pull (or _restamp_dirs) runs while
        _restore_relay is uploading .wsfiles-relay-* files before placement, the
        staged files must NOT disappear and restore must succeed."""
        root = str(tmp_path)
        ws_dir = tmp_path / "ws-b"
        ws_dir.mkdir()
        layout = SandboxLayout.for_root(root).for_workspace("ws-b")

        upload_done = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_upload = asyncio.Event()

        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.sandbox_id = "sb-race-1"
        sandbox.working_dir = root
        sandbox.runtime = create_autospec(SandboxRuntime, instance=True)

        async def fake_upload(filepath: str, content: bytes, *, verify: bool = False) -> bool:
            target = Path(filepath)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            upload_done.set()
            # Wait briefly so concurrent cleanup attempts to run before placement.
            await asyncio.wait_for(release_upload.wait(), timeout=2.0)
            return True

        sandbox.aupload_file_bytes = AsyncMock(side_effect=fake_upload)

        async def fake_run_transfer_op(_sb, op: str, spec: dict, *, timeout_s: int, reports=()):
            assert op == "pull"
            return rt.pull(spec)

        row = {
            "file_path": "report.txt",
            "is_binary": False,
            "content_binary": None,
            "content_text": "important data",
            "content_hash": hashlib.sha256(b"important data").hexdigest(),
            "file_size": len(b"important data"),
        }
        result = {"restored": 0, "errors": 0}

        with patch.object(transfer, "run_transfer_op", AsyncMock(side_effect=fake_run_transfer_op)):
            restore_task = asyncio.create_task(
                restore._restore_relay(
                    "user-1",
                    "ws-b",
                    sandbox,
                    [row],
                    result,
                    layout=layout,
                )
            )
            await asyncio.wait_for(upload_done.wait(), timeout=2.0)

            async def concurrent_cleanup():
                cleanup_started.set()
                # Both an unlocked _restamp_dirs AND a sweeping pull_direct on the same layout.
                await restore._restamp_dirs(
                    "ws-b",
                    sandbox,
                    [{"path": "sub", "kind": "dir", "mode": 0o755, "mtime_ns": 1, "made": True}],
                    layout=layout,
                )
                await transfer.pull_direct(
                    sandbox,
                    [{"path": "other_dir", "kind": "dir", "mode": 0o755, "mtime_ns": 1}],
                    layout=layout,
                    sweep_orphans=True,
                )

            cleanup_task = asyncio.create_task(concurrent_cleanup())
            await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
            # Yield to let concurrent cleanup hit the sweep if unguarded.
            await asyncio.sleep(0.05)
            release_upload.set()

            await asyncio.wait_for(asyncio.gather(restore_task, cleanup_task), timeout=5.0)

        assert result == {"restored": 1, "errors": 0}
        assert (ws_dir / "report.txt").read_bytes() == b"important data"

    @pytest.mark.asyncio
    async def test_relay_staging_ignores_ambient_project_context(self, tmp_path: Path):
        """Uploading a .wsfiles-relay-* staging file inside another workspace's
        ProjectContext must not fold the path into that other workspace's folder."""
        root = str(tmp_path)
        (tmp_path / "Home").mkdir()
        (tmp_path / "ws-b").mkdir()
        layout_b = SandboxLayout.for_root(root).for_workspace("ws-b")

        provider, runtime = _make_provider_and_runtime(work_dir=root)
        written: dict[str, bytes] = {}

        async def _upload_file(content: bytes, path: str) -> None:
            written[path] = content

        runtime.upload_file = AsyncMock(side_effect=_upload_file)

        core = CoreConfig(
            sandbox=SandboxConfig(provider="docker", docker=DockerConfig()),
            filesystem=FilesystemConfig(
                working_directory=root,
                allowed_directories=[root, "/tmp"],
            ),
            security=SecurityConfig(),
            logging=LoggingConfig(),
            mcp=MCPConfig(servers=[]),
        )
        sandbox = PTCSandbox(core, None)
        sandbox.provider = provider
        sandbox.runtime = runtime
        sandbox.sandbox_id = SANDBOX_ID
        sandbox._work_dir = root

        row = {
            "file_path": "notes.md",
            "is_binary": False,
            "content_binary": None,
            "content_text": "hello ws-b",
            "content_hash": hashlib.sha256(b"hello ws-b").hexdigest(),
            "file_size": len(b"hello ws-b"),
        }

        # Active Home turn that does NOT list "ws-b" in sibling_dir_names.
        home_ctx = ProjectContext(workspace_id="ws-home", dir_name="Home", sibling_dir_names=())
        with project_bound(home_ctx):
            staged = await restore._stage_relayed_file("user-1", sandbox, row, layout_b)

        assert staged is not None
        staged_name, _digest, _size = staged
        assert f"{root}/ws-b/{staged_name}" in written
        assert f"{root}/Home/ws-b/{staged_name}" not in written
