"""DALTON-HOOK(workspace-file-gc-kill): the rollback switch keeps the sweep from starting."""

import pytest

from src.server.services import workspace_file_gc
from src.server.services.workspace_file_gc import WorkspaceFileGCService


@pytest.mark.asyncio
async def test_disabled_by_env_never_starts(monkeypatch):
    monkeypatch.setenv("DALTON_WORKSPACE_FILE_GC_DISABLED", "1")
    monkeypatch.setattr(workspace_file_gc, "is_storage_enabled", lambda: True)
    service = WorkspaceFileGCService()
    await service.start()
    assert service._task is None


@pytest.mark.asyncio
async def test_without_env_starts(monkeypatch):
    monkeypatch.delenv("DALTON_WORKSPACE_FILE_GC_DISABLED", raising=False)
    monkeypatch.setattr(workspace_file_gc, "is_storage_enabled", lambda: True)
    service = WorkspaceFileGCService(interval_seconds=3600)

    async def idle_loop():
        await service._shutdown_event.wait()

    monkeypatch.setattr(service, "_loop", idle_loop)
    await service.start()
    assert service._task is not None
    await service.stop()
