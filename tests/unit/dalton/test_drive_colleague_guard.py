"""Drive default-off posture, colleague-access isolation, and MinIO lifecycle rule tests.

Covers:
1. ``config.dalton.yaml`` and ``config.core.yaml`` keep ``features.drive.enabled: false``.
2. Even when Drive is enabled for the computer owner, cross-user colleague turns
   cannot mount, read, write, edit, glob, grep, or shell-access ``.agents/drive``
   across ``DriveRoute``, ``build_filesystem_backend``, ``mount_guard``,
   ``livefs.drive_mount`` lifecycle, and ``app.drive_mount`` HTTP endpoints.
3. Standalone Core ``scripts/standalone/minio_init.py`` idempotently installs the
   7-day ``AbortIncompleteMultipartUpload`` lifecycle rule.
"""

from __future__ import annotations

import hashlib
import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
import yaml
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ptc_agent.agent.backends.drive import DriveAbsent, DriveRefused, DriveRoute
from ptc_agent.agent.filesystem_routes import (
    IdentityGates,
    build_filesystem_backend,
)
from ptc_agent.agent.tools import mount_guard
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import drive_mount as core_drive_mount
from ptc_agent.core.sandbox.drive_mount import DRIVE_NOT_HERE, DriveDisk
from ptc_agent.core.sandbox.livefs_mount import CallContext, MountOutcome
from src.config import features as feature_catalog
from src.config.features import SystemFlag, effective_flags
from src.server.app import drive_mount as drive_mount_app
from src.server.database.drive.records import Content, DriveError, Entry, Space
from src.server.services import drive as drive_service_mod
from src.server.services.livefs import drive_mount as livefs_drive_mount
from src.server.services.livefs import outcomes as livefs_outcomes
from src.server.services.livefs.tokens import LivefsIdentity

REPO_ROOT = Path(__file__).resolve().parents[3]
OWNER = "owner-user-1"
COLLEAGUE = "colleague-user-2"
COMPUTER = "11111111-1111-1111-1111-111111111111"
ROOT = "/home/workspace"
SECRET_PATH = f"{ROOT}/.agents/drive/personal/notes/secret.md"
T0 = datetime(2026, 11, 25, tzinfo=UTC)


def _make_entry(path: str, data: bytes) -> Entry:
    return Entry(
        entry_id=hashlib.md5(path.encode()).hexdigest(),
        space_id="space-owner",
        path=path,
        parent=path.rpartition("/")[0],
        name=path.rpartition("/")[2],
        kind="file",
        head_seq=1,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        content_type="text/markdown",
        is_text=True,
        created_at=T0,
        modified_at=T0,
        changed_seq=1,
        deleted_at=None,
        trash_id=None,
        deleted_by=None,
        modified_by=None,
    )


class _SpyDriveService:
    """In-memory DriveService stub recording whether ``reach`` was ever invoked."""

    def __init__(self) -> None:
        self.reach_calls: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {"notes/secret.md": b"owner confidential memo\n"}

    async def reach(self, user_id: str, space_ref: str) -> Space:
        self.reach_calls.append((user_id, space_ref))
        return Space(
            space_id="space-owner",
            kind="personal",
            user_id=user_id,
            created_at=T0,
        )

    async def stat(self, space: Space, path: str | None = None) -> Entry:
        if path in self.files:
            return _make_entry(path, self.files[path])
        raise DriveError("not_found", f"Nothing at {path}")

    async def tree(self, space: Space, path: str = "") -> list[Entry]:
        return [
            _make_entry(p, d)
            for p, d in sorted(self.files.items())
            if not path or p.startswith(path + "/")
        ]

    async def content(self, space: Space, path: str | None = None) -> tuple[Entry, Content]:
        entry = await self.stat(space, path)
        data = self.files[entry.path]
        return entry, Content(
            space_id=space.space_id,
            sha256=entry.sha256 or "",
            size=len(data),
            is_text=True,
            content_type="text/markdown",
            blob_user_id=None,
        )

    async def read_range(self, found: Content, offset: int, length: int) -> bytes:
        data = next(
            d
            for d in self.files.values()
            if hashlib.sha256(d).hexdigest() == found.sha256
        )
        return data[offset : offset + length]

    async def stream(self, found: Content, start: int = 0, end: int | None = None):
        data = await self.read_range(found, 0, found.size)
        yield data[start:end]

    async def text_pages(
        self,
        space: Space,
        entry_ids: list[str] | None = None,
        *,
        under: str | None = None,
    ):
        page = []
        for path, data in sorted(self.files.items()):
            entry = _make_entry(path, data)
            if under and not path.startswith(under + "/"):
                continue
            if entry_ids is not None and entry.entry_id not in entry_ids:
                continue
            page.append((entry, data))
        if page:
            yield page

    async def write(
        self,
        space: Space,
        path: str,
        data: bytes,
        *,
        actor,
        if_match: str | None = None,
    ) -> Entry:
        self.files[path] = data
        return _make_entry(path, data)


class _StubSandboxBackend:
    computer_root = ROOT
    filesystem_config = None

    def __init__(self) -> None:
        self.sandbox = SimpleNamespace(drive=core_drive_mount.NO_DRIVE)

    def normalize_path(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return f"{ROOT}/{path.lstrip('./')}"

    def virtualize_path(self, path: str) -> str:
        return path.removeprefix(f"{ROOT}/")

    def validate_path(self, path: str) -> bool:
        return True


def test_drive_disabled_by_default_in_dalton_and_core_configs(monkeypatch):
    """Both Dalton prod and Standalone Core overlays pin features.drive.enabled=false."""
    for name in ("config.dalton.yaml", "config.core.yaml"):
        raw = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
        assert raw["features"]["drive"]["enabled"] is False, name

    real_system_flag = feature_catalog.system_flag

    def _disabled_drive(key: str) -> SystemFlag:
        if key == "drive":
            return SystemFlag(
                enabled=False,
                gate=feature_catalog.FeatureGate.OPT_IN,
                min_tier=None,
            )
        return real_system_flag(key)

    monkeypatch.setattr(feature_catalog, "system_flag", _disabled_drive)
    resolved = effective_flags({"drive": True})
    assert resolved["drive"] is False


@pytest.mark.asyncio
async def test_drive_route_blocks_cross_user_caller_when_drive_enabled():
    """Even if DriveRoute is constructed directly with Drive on, a colleague caller sees nothing."""
    svc = _SpyDriveService()
    sb = _StubSandboxBackend()
    colleague_route = DriveRoute(
        service=svc,
        user_id=OWNER,
        sandbox_backend=sb,
        root_prefix=f"{ROOT}/.agents/drive/",
        call=CallContext(workspace_id="ws-1", thread_id="t-1", caller_user_id=COLLEAGUE),
    )

    with pytest.raises(DriveRefused, match="Drive is not available"):
        await colleague_route.aread_text(SECRET_PATH)
    with pytest.raises(DriveRefused, match="Drive is not available"):
        await colleague_route.aread_range(SECRET_PATH, offset=0, limit=10)
    with pytest.raises(DriveRefused, match="Drive is not available"):
        await colleague_route.adownload_file_bytes_capped(SECRET_PATH, lambda _: 1024)
    with pytest.raises(DriveRefused, match="Drive is not available"):
        await colleague_route.awrite_text(SECRET_PATH, "overwrite")

    edit_res = await colleague_route.aedit_text(SECRET_PATH, "owner", "colleague")
    assert edit_res == {"success": False, "error": DRIVE_NOT_HERE}
    assert await colleague_route.aglob_paths("**/*.md", f"{ROOT}/.agents/drive") == []
    assert await colleague_route.agrep_rich("confidential", f"{ROOT}/.agents/drive") == []
    assert svc.reach_calls == []

    owner_route = DriveRoute(
        service=svc,
        user_id=OWNER,
        sandbox_backend=sb,
        root_prefix=f"{ROOT}/.agents/drive/",
        call=CallContext(workspace_id="ws-1", thread_id="t-1", caller_user_id=OWNER),
    )
    assert await owner_route.aread_text(SECRET_PATH) == "owner confidential memo\n"
    assert await owner_route.aglob_paths("**/*.md", f"{ROOT}/.agents/drive") == [SECRET_PATH]
    assert svc.reach_calls == [(OWNER, "personal")]


def test_filesystem_backend_mounts_drive_absent_for_colleague_even_when_gates_drive_enabled():
    """With gates.drive=True, cross-user turns mount DriveAbsent rather than owner's DriveRoute."""
    sb = _StubSandboxBackend()
    gates = IdentityGates(
        user_memory=False,
        workspace_memory=False,
        memo=False,
        user_data=False,
        workflow=False,
        workflow_fs=False,
        workflow_tool=False,
        channels=False,
        drive=True,
    )

    colleague_fs, _ = build_filesystem_backend(
        backend=sb,
        gates=gates,
        store=None,
        user_id=COLLEAGUE,
        workspace_id="ws-1",
        workspace_owner_id=OWNER,
        call=CallContext(workspace_id="ws-1", caller_user_id=COLLEAGUE),
    )
    assert isinstance(colleague_fs.route_for(SECRET_PATH), DriveAbsent)

    call_only_cross_fs, _ = build_filesystem_backend(
        backend=sb,
        gates=gates,
        store=None,
        user_id=OWNER,
        workspace_id="ws-1",
        call=CallContext(workspace_id="ws-1", caller_user_id=COLLEAGUE),
    )
    assert isinstance(call_only_cross_fs.route_for(SECRET_PATH), DriveAbsent)

    owner_fs, _ = build_filesystem_backend(
        backend=sb,
        gates=gates,
        store=None,
        user_id=OWNER,
        workspace_id="ws-1",
        workspace_owner_id=OWNER,
        call=CallContext(workspace_id="ws-1", caller_user_id=OWNER),
    )
    assert isinstance(owner_fs.route_for(SECRET_PATH), DriveRoute)


@pytest.mark.asyncio
async def test_mount_guard_blocks_colleague_shell_access_to_drive_even_when_on_disk():
    """Bash/ExecuteCode commands naming .agents/drive are blocked for colleague callers."""
    drive_handle = SimpleNamespace(
        user_id=OWNER,
        on_disk=AsyncMock(return_value=DriveDisk.ON),
        push_saves=AsyncMock(return_value=False),
        after=AsyncMock(return_value=""),
    )
    sandbox = SimpleNamespace(drive=drive_handle, call_filer=AsyncMock(), paid_calls=None)
    backend = SimpleNamespace(
        sandbox=sandbox,
        config_folder=AsyncMock(return_value=None),
        settled_livefs=AsyncMock(return_value=None),
    )

    run_colleague = AsyncMock(return_value=("leaked", {}))
    text, _ = await mount_guard.run_guarded(
        backend,
        "cat .agents/drive/personal/notes/secret.md",
        CallContext(workspace_id="ws-1", thread_id="t-1", caller_user_id=COLLEAGUE),
        run_colleague,
        memory_error="memory",
        files_error="files {dir}",
        blocked_event="blocked",
    )
    assert text == DRIVE_NOT_HERE
    run_colleague.assert_not_awaited()
    drive_handle.on_disk.assert_not_awaited()

    run_owner = AsyncMock(return_value=("ok", {}))
    owner_text, _ = await mount_guard.run_guarded(
        backend,
        "cat .agents/drive/personal/notes/secret.md",
        CallContext(workspace_id="ws-1", thread_id="t-1", caller_user_id=OWNER),
        run_owner,
        memory_error="memory",
        files_error="files {dir}",
        blocked_event="blocked",
    )
    assert owner_text == "ok"
    run_owner.assert_awaited_once()


@pytest.mark.asyncio
async def test_drive_mount_due_and_converge_block_and_unmount_on_colleague_turn(monkeypatch):
    """With Drive enabled for the owner, colleague turns report due=False and stop any mount."""
    sandbox = SimpleNamespace(
        mounts_livefs=True,
        runtime=object(),
        layout=SandboxLayout.for_root(ROOT),
        sandbox_id="sb-1",
    )
    monkeypatch.setattr(livefs_drive_mount, "wanted", AsyncMock(return_value=True))

    assert (
        await livefs_drive_mount.due(
            sandbox, OWNER, computer_id=COMPUTER, caller_user_id=COLLEAGUE
        )
        is False
    )

    with (
        patch(
            "src.dalton.settings.is_dalton_sharing_auth_enabled",
            return_value=True,
        ),
        patch(
            "src.dalton.colleague_outbound_guard.computer_has_active_cross_user_run",
            AsyncMock(return_value=True),
        ),
    ):
        assert await livefs_drive_mount.due(sandbox, OWNER, computer_id=COMPUTER) is False

    store: dict[str, object] = {}

    async def _get(key: str):
        return store.get(key)

    async def _set(key: str, value: object, ttl: int | None = None):
        store[key] = value

    async def _delete(*keys: str):
        for key in keys:
            store.pop(key, None)

    @asynccontextmanager
    async def _lock(computer_id: str, *, wait_s: float):
        yield object()

    stop_mock = create_autospec(
        core_drive_mount.stop,
        return_value=MountOutcome(ok=True),
    )
    start_mock = create_autospec(
        core_drive_mount.start,
        return_value=MountOutcome(ok=True, started=True),
    )

    monkeypatch.setattr(
        livefs_drive_mount,
        "get_cache_client",
        lambda: SimpleNamespace(get=_get, set=_set, delete=_delete),
    )
    monkeypatch.setattr(livefs_drive_mount, "workspace_folders_lock", _lock)
    monkeypatch.setattr(core_drive_mount, "stop", stop_mock)
    monkeypatch.setattr(core_drive_mount, "start", start_mock)

    result = await livefs_drive_mount.converge(
        sandbox,
        computer_id=COMPUTER,
        user_id=OWNER,
        provider="docker",
        caller_user_id=COLLEAGUE,
    )
    assert result.done is livefs_drive_mount.Converged.STOPPED
    stop_mock.assert_awaited_once_with(sandbox)
    start_mock.assert_not_awaited()


def test_drive_mount_http_endpoints_refuse_colleague_calls_when_drive_enabled(monkeypatch):
    """Drive mount HTTP routes return 404 when a colleague call or active cross-user run exists."""
    app = FastAPI()
    app.include_router(drive_mount_app.router)
    identity = LivefsIdentity(computer_id=COMPUTER, user_id=OWNER, root_dir=ROOT)
    app.dependency_overrides[drive_mount_app._caller] = lambda: identity
    client = TestClient(app)

    monkeypatch.setattr(drive_service_mod, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        drive_service_mod,
        "spaces",
        AsyncMock(
            return_value=[
                Space(space_id="sp-1", kind="personal", user_id=OWNER, created_at=T0)
            ]
        ),
    )
    monkeypatch.setattr(
        livefs_outcomes,
        "call_context",
        AsyncMock(
            return_value=CallContext(
                workspace_id="ws-1",
                thread_id="t-1",
                caller_user_id=COLLEAGUE,
            )
        ),
    )
    monkeypatch.setattr(livefs_outcomes, "record", AsyncMock(return_value=True))

    headers = {"X-Livefs-Call": "call12345678"}
    with patch("src.dalton.settings.is_dalton_sharing_auth_enabled", return_value=False):
        assert client.get("/api/v1/livefs/drive/spaces", headers=headers).status_code == 404
        assert (
            client.get(
                "/api/v1/livefs/drive/manifest",
                params={"space": "personal"},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/api/v1/livefs/drive/write",
                params={"space": "personal", "path": "notes/secret.md"},
                content=b"blocked",
                headers=headers,
            ).status_code
            == 404
        )

    # Without X-Livefs-Call, active cross-user run on the computer also fails closed.
    monkeypatch.setattr(livefs_outcomes, "call_context", AsyncMock(return_value=None))
    with (
        patch("src.dalton.settings.is_dalton_sharing_auth_enabled", return_value=True),
        patch(
            "src.dalton.colleague_outbound_guard.computer_has_active_cross_user_run",
            AsyncMock(return_value=True),
        ),
    ):
        assert client.get("/api/v1/livefs/drive/spaces").status_code == 404


def test_minio_init_configures_seven_day_multipart_lifecycle_rule():
    """Standalone MinIO init installs a >=7-day AbortIncompleteMultipartUpload rule idempotently."""
    spec = importlib.util.spec_from_file_location(
        "minio_init", REPO_ROOT / "scripts/standalone/minio_init.py"
    )
    assert spec is not None and spec.loader is not None
    minio_init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(minio_init)

    class _FakeS3Client:
        def __init__(self, rules: list[dict[str, object]] | None = None) -> None:
            self.rules = rules
            self.put_calls: list[dict[str, object]] = []

        def get_bucket_lifecycle_configuration(self, *, Bucket: str):
            if self.rules is None:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchLifecycleConfiguration", "Message": "none"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "GetBucketLifecycleConfiguration",
                )
            return {"Rules": list(self.rules)}

        def put_bucket_lifecycle_configuration(
            self, *, Bucket: str, LifecycleConfiguration: dict[str, object]
        ):
            self.put_calls.append({"Bucket": Bucket, "Config": LifecycleConfiguration})
            self.rules = list(LifecycleConfiguration["Rules"])  # type: ignore[arg-type]

    # 1. Empty bucket gets the 7-day rule.
    client = _FakeS3Client(rules=None)
    assert minio_init._ensure_multipart_lifecycle_rule(client, bucket="chat-uploads") == 0
    assert len(client.put_calls) == 1
    assert client.rules == [
        {
            "ID": minio_init.MULTIPART_LIFECYCLE_RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    ]

    # 2. Second run is idempotent (no duplicate PUT).
    assert minio_init._ensure_multipart_lifecycle_rule(client, bucket="chat-uploads") == 0
    assert len(client.put_calls) == 1

    # 3. Existing unrelated rule is preserved alongside the new multipart rule.
    other_rule = {
        "ID": "expire-temp",
        "Status": "Enabled",
        "Filter": {"Prefix": "tmp/"},
        "Expiration": {"Days": 30},
    }
    client2 = _FakeS3Client(rules=[other_rule])
    assert minio_init._ensure_multipart_lifecycle_rule(client2, bucket="chat-uploads") == 0
    assert len(client2.put_calls) == 1
    assert other_rule in client2.rules
    assert any(
        r.get("ID") == minio_init.MULTIPART_LIFECYCLE_RULE_ID
        and r.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": 7}
        for r in client2.rules
    )
