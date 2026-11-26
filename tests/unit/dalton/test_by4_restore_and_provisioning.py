"""Tests for Item 8 (BY4): relayed staging path-folding guard, _restamp_dirs no-orphan-sweep, and 0/N backup skip."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext, current_project, project_bound
from ptc_agent.core.sandbox import path_resolution
from src.server.services.computer_manager._machine_backup import MachineBackupMixin
from src.server.services.computer_manager._provisioning import ProvisioningMixin
from src.server.services.persistence import restore as restore_mod


@pytest.mark.asyncio
async def test_stage_relayed_file_ignores_ambient_project_context_path_folding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BY4 Root Cause A: when an ambient ProjectContext (e.g. Home turn) is active,
    _stage_relayed_file must clear it before calling aupload_file_bytes so
    normalize_path/_root_entry_in_folder does NOT fold /home/workspace/ws-b/.wsfiles-relay-*
    into /home/workspace/Home/ws-b/.wsfiles-relay-*."""
    uploaded_normalized_paths: list[str] = []

    class _FakeSandbox:
        _work_dir = "/home/workspace"
        config = SimpleNamespace(
            filesystem=SimpleNamespace(
                allowed_directories=["/home/workspace", "/tmp"],
                denied_directories=[],
                enable_path_validation=True,
            )
        )

        def workspace(self, project: ProjectContext | None = None) -> WorkspaceLayout:
            ctx = project if project is not None else current_project()
            return WorkspaceLayout(
                root=self._work_dir,
                dir_name=ctx.dir_name if ctx is not None else None,
            )

        def normalize_path(self, path: str, project: ProjectContext | None = None) -> str:
            return path_resolution.normalize_path(self, path, project)

        def validate_path(self, filepath: str, project: ProjectContext | None = None) -> bool:
            return path_resolution.validate_path(self, filepath, project)

        async def _wait_ready(self) -> None:
            return None

        async def aupload_file_bytes(
            self, filepath: str, content: bytes, *, verify: bool = False
        ) -> bool:
            norm = self.normalize_path(filepath)
            uploaded_normalized_paths.append(norm)
            return True

    resolve_spy = create_autospec(
        restore_mod.resolve_file_bytes,
        return_value=b"hello-world",
    )
    monkeypatch.setattr(restore_mod, "resolve_file_bytes", resolve_spy)

    # Ambient project is Home, and target workspace "ws-b" is NOT in sibling_dir_names
    home_ctx = ProjectContext(
        workspace_id="ws-home",
        dir_name="Home",
        sibling_dir_names=("ws-old",),
    )
    target_layout = WorkspaceLayout(root="/home/workspace", dir_name="ws-b")
    sandbox = _FakeSandbox()

    with project_bound(home_ctx):
        staged = await restore_mod._stage_relayed_file(
            "u-1",
            sandbox,
            {"file_path": "notes.md", "content_text": "hello-world"},
            target_layout,
        )

    assert staged is not None
    staged_name, _digest, size = staged
    assert size == len(b"hello-world")
    assert uploaded_normalized_paths == [f"/home/workspace/ws-b/{staged_name}"]
    assert "/Home/" not in uploaded_normalized_paths[0]


@pytest.mark.asyncio
async def test_relayed_pack_items_ignores_ambient_project_context_path_folding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BY4 Root Cause A (pack path): _relayed_pack_items uploads pack staging files directly
    to target layout.workspace even when an ambient ProjectContext is active."""
    uploaded_normalized_paths: list[str] = []

    class _FakeSandbox:
        _work_dir = "/home/workspace"
        config = SimpleNamespace(
            filesystem=SimpleNamespace(
                allowed_directories=["/home/workspace", "/tmp"],
                denied_directories=[],
                enable_path_validation=True,
            )
        )

        def workspace(self, project: ProjectContext | None = None) -> WorkspaceLayout:
            ctx = project if project is not None else current_project()
            return WorkspaceLayout(
                root=self._work_dir,
                dir_name=ctx.dir_name if ctx is not None else None,
            )

        def normalize_path(self, path: str, project: ProjectContext | None = None) -> str:
            return path_resolution.normalize_path(self, path, project)

        async def aupload_file_bytes(
            self, filepath: str, content: bytes, *, verify: bool = False
        ) -> bool:
            uploaded_normalized_paths.append(self.normalize_path(filepath))
            return True

    fetch_spy = create_autospec(restore_mod.fetch_blob, return_value=b"pack-bytes")
    monkeypatch.setattr(restore_mod, "fetch_blob", fetch_spy)

    home_ctx = ProjectContext(
        workspace_id="ws-home",
        dir_name="Home",
        sibling_dir_names=(),
    )
    target_layout = WorkspaceLayout(root="/home/workspace", dir_name="ws-b")
    result = {"restored": 0, "errors": 0}
    pack_sha = "a" * 64

    with project_bound(home_ctx):
        items = await restore_mod._relayed_pack_items(
            "u-1",
            "ws-b-id",
            _FakeSandbox(),
            {pack_sha: [{"file_path": "a.txt", "pack_offset": 0, "pack_length": 5, "file_size": 5}]},
            result,
            target_layout,
        )

    assert len(items) == 1
    assert uploaded_normalized_paths == [f"/home/workspace/ws-b/.wsfiles-relay-{pack_sha}"]


@pytest.mark.asyncio
async def test_restamp_dirs_disables_orphan_sweep_to_protect_concurrent_relay_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BY4 Root Cause B: _restamp_dirs runs outside workspace_sync_lock and must pass
    sweep_orphans=False to pull_direct so it never unlinks active .wsfiles-relay-* files."""
    pull_spy = create_autospec(restore_mod.pull_direct, return_value={})
    monkeypatch.setattr(restore_mod, "pull_direct", pull_spy)

    layout = WorkspaceLayout(root="/home/workspace", dir_name="ws-b")
    await restore_mod._restamp_dirs(
        "ws-b-id",
        sandbox=object(),
        items=[{"kind": "dir", "path": "reports", "made": True}],
        layout=layout,
    )

    pull_spy.assert_awaited_once()
    assert pull_spy.await_args.kwargs.get("sweep_orphans") is False


@pytest.mark.asyncio
async def test_provisioning_backup_without_worker_session_skips_all_projects_and_logs_zero_of_n(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Documented BY4 trigger condition for 0/N backup skip: when the worker has no cached
    in-process session for computer_id, non-strict backup_project_files skips every workspace
    and _mirror_projects logs '0 of N'."""

    class _Harness(MachineBackupMixin, ProvisioningMixin):
        def _cached_session(self, computer_id: str):
            return None

    harness = _Harness()
    ordered = [f"ws-{i}" for i in range(4)]

    with caplog.at_level(logging.WARNING):
        mirrored = await harness._mirror_projects(
            "comp-1",
            ordered,
            strict=False,
            expected_sandbox_id="sb-1",
            session=None,
            routine=True,
        )

    assert mirrored == 0
    assert any("no attached session on this worker" in r.message for r in caplog.records)
    assert any("mirrored 0 of 4 project(s)" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_sessionless_idle_stop_defers_and_requests_cross_worker_mirror_then_proceeds_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BY4 r2/r3: when Redis is enabled and an idle stop lands on a worker without a cached
    session (with files_backup on), _finish_claimed_stop requests commands.mirror.mirror_computer
    and defers stop (settling back to RUNNING) within the grace window; once the grace window
    expires (no worker held a session) or the owed mirror completes, it proceeds with teardown.
    When Redis is disabled, no cross-worker mirror is possible so it does NOT defer."""
    from src.server.models.computer import ComputerStatus
    from src.server.services.commands import mirror as commands_mirror
    from src.server.services.computer_manager._lifecycle import SessionLifecycleMixin

    class _FakeRedis:
        def __init__(self) -> None:
            self.kv: dict[str, bytes] = {}
            self.zsets: dict[str, dict[str, float]] = {}

        async def get(self, key: str) -> bytes | None:
            return self.kv.get(key)

        async def set(self, key: str, value: str, ex: int | None = None) -> bool:
            self.kv[key] = value.encode()
            return True

        async def delete(self, key: str) -> int:
            return 1 if self.kv.pop(key, None) is not None else 0

        async def zscore(self, key: str, member: str) -> float | None:
            return self.zsets.get(key, {}).get(member)

    fake_redis = _FakeRedis()

    async def _fake_mirror_computer(computer_id: str) -> None:
        fake_redis.zsets.setdefault(commands_mirror._OWED_KEY, {})[computer_id] = 1.0

    mirror_spy = create_autospec(commands_mirror.mirror_computer, side_effect=_fake_mirror_computer)
    redis_spy = create_autospec(commands_mirror._redis, return_value=fake_redis)
    monkeypatch.setattr(commands_mirror, "mirror_computer", mirror_spy)
    monkeypatch.setattr(commands_mirror, "_redis", redis_spy)

    settled_states: list[tuple[str, Any]] = []
    teardown_calls: list[str] = []

    class _StopHarness(SessionLifecycleMixin):
        _IDLE_STOP_DEFER_GRACE_S = 60.0

        def _cached_session(self, computer_id: str):
            return None

        def _machine_if_known(self, computer_id: str):
            return None

        def _session_sandbox_id(self, session: Any):
            return None

        async def _backup_machine_files_to_db(self, *args: Any, **kwargs: Any) -> int:
            return 0

        async def refresh_computer_disk(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def _machine_has_active_tasks(self, *args: Any, **kwargs: Any) -> bool:
            return False

        async def _retain_machine_stop_claim(self, *args: Any, **kwargs: Any) -> bool:
            return True

        async def _detached_sandbox_teardown(self, sandbox_id: str, **kwargs: Any) -> None:
            teardown_calls.append(sandbox_id)

        async def _settle_machine_stop(self, computer_id: str, status: Any) -> None:
            settled_states.append((computer_id, status))

        async def _stop_result(self, computer_id: str, workspace_id: str | None) -> dict[str, Any]:
            return {"computer_id": computer_id, "status": "running" if not teardown_calls else "stopped"}

    harness = _StopHarness()
    binding = SimpleNamespace(
        computer_id="comp-idle-1",
        files_backup=True,
        provider=SimpleNamespace(recreatable=True),
    )

    # First idle stop on session-less worker with Redis enabled -> requests cross-worker mirror and defers teardown!
    res1 = await harness._finish_claimed_stop(
        binding,
        workspace_id="ws-1",
        durable_sandbox_id="sb-1",
        back_up=True,
        reason="idle",
    )
    assert res1["status"] == "running"
    mirror_spy.assert_awaited_once_with("comp-idle-1")
    assert teardown_calls == []
    assert settled_states == [("comp-idle-1", ComputerStatus.RUNNING)]

    # Second sweep still inside grace window while mirror debt is owed -> stays deferred
    res1b = await harness._finish_claimed_stop(
        binding,
        workspace_id="ws-1",
        durable_sandbox_id="sb-1",
        back_up=True,
        reason="idle",
    )
    assert res1b["status"] == "running"
    assert teardown_calls == []

    # Expire the deferral grace window (e.g. after backend restart when no worker holds a session)
    defer_key = "by4:idle_stop_mirror_defer:comp-idle-1"
    fake_redis.kv[defer_key] = f"{float(fake_redis.kv[defer_key].decode()) - 120.0:.3f}".encode()
    res2 = await harness._finish_claimed_stop(
        binding,
        workspace_id="ws-1",
        durable_sandbox_id="sb-1",
        back_up=True,
        reason="idle",
    )
    assert res2["status"] == "stopped"
    assert teardown_calls == ["sb-1"]
    assert settled_states[-1] == ("comp-idle-1", ComputerStatus.STOPPED)
    assert defer_key not in fake_redis.kv

    # When Redis is disabled, cross-worker mirror cannot run so session-less idle stop proceeds immediately
    redis_spy.return_value = None
    teardown_calls.clear()
    settled_states.clear()
    binding_no_redis = SimpleNamespace(
        computer_id="comp-idle-no-redis",
        files_backup=True,
        provider=SimpleNamespace(recreatable=True),
    )
    res_no_redis = await harness._finish_claimed_stop(
        binding_no_redis,
        workspace_id="ws-2",
        durable_sandbox_id="sb-2",
        back_up=True,
        reason="idle",
    )
    assert res_no_redis["status"] == "stopped"
    assert teardown_calls == ["sb-2"]
    assert settled_states == [("comp-idle-no-redis", ComputerStatus.STOPPED)]
