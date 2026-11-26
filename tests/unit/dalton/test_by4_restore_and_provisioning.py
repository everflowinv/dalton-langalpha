"""Tests for Item 8 (BY4): relayed staging path-folding guard, _restamp_dirs no-orphan-sweep, and 0/N backup skip."""

from __future__ import annotations

import logging
from types import SimpleNamespace
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
