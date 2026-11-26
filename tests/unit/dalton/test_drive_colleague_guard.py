"""Drive kill switch, route 404 gating, colleague-access isolation, and MinIO init tests.

Covers:
1. ``config.dalton.yaml`` and ``config.core.yaml`` keep ``features.drive.enabled: false``.
2. When ``features.drive.enabled`` is True and Dalton colleague access or
   ``computer_cli_tokens`` is enabled, Drive stays forced off with a startup
   error log; when both are off, Drive follows upstream enabled behavior.
3. Both ``/api/v1/drive/*`` and ``/api/v1/livefs/drive/*`` check the deployment
   switch first and return 404 before auth, permission, or parameter validation.
4. Second-layer colleague-access guards across ``DriveRoute``,
   ``build_filesystem_backend``, ``mount_guard``, ``livefs.drive_mount``, and
   ``app.drive_mount``.
5. Standalone Core ``scripts/standalone/minio_init.py`` skips unsupported
   lifecycle writes, preserves bucket policy error semantics, and deduplicates
   buckets by ``(endpoint, bucket)``.
6. The guidance an operator and a user read: ``langalpha-doc``'s Drive
   reference names the administrator's deployment switch before the user's
   own, and the multipart-cleanup startup warning says what to set on MinIO,
   whose lifecycle API has no rule to add.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import boto3
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
from src.config.features import (
    SystemFlag,
    effective_flags,
    is_feature_enabled_system,
)
from src.config.models import FeatureFlagOverride, InfrastructureConfig
from src.dalton.capabilities import check_drive_startup_guard
from src.server.app import drive as drive_app
from src.server.app import drive_mount as drive_mount_app
from src.server.database.drive.records import Content, DriveError, Entry, Space
from src.server.dependencies.principal import Principal, get_principal
from src.server.services import drive as drive_service_mod
from src.server.services.livefs import drive_mount as livefs_drive_mount
from src.server.services.livefs import outcomes as livefs_outcomes
from src.server.services.livefs.tokens import LivefsIdentity

REPO_ROOT = Path(__file__).resolve().parents[3]
LANGALPHA_DOC_DRIVE = (
    REPO_ROOT / "plugins" / "langalpha_service" / "skills" / "langalpha-doc" / "references" / "drive.md"
)
SETUP_PY_PATH = REPO_ROOT / "src" / "server" / "app" / "setup.py"
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
    with (
        patch.object(drive_mount_app, "is_feature_enabled_system", return_value=True),
        patch("src.dalton.settings.is_dalton_sharing_auth_enabled", return_value=False),
    ):
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

    # Without X-Livefs-Call, active cross-user run on the computer also fails closed
    # at the second-layer mount guard even if the deployment switch is forced open.
    monkeypatch.setattr(livefs_outcomes, "call_context", AsyncMock(return_value=None))
    with (
        patch.object(drive_mount_app, "is_feature_enabled_system", return_value=True),
        patch("src.dalton.settings.is_dalton_sharing_auth_enabled", return_value=True),
        patch(
            "src.dalton.colleague_outbound_guard.computer_has_active_cross_user_run",
            AsyncMock(return_value=True),
        ),
    ):
        assert client.get("/api/v1/livefs/drive/spaces").status_code == 404


def test_drive_kill_switch_blocks_when_colleague_access_or_cli_tokens_enabled(
    monkeypatch, caplog
):
    """Drive configured on stays off when colleague access or computer_cli_tokens is on,
    and follows upstream enabled behavior when both are off."""
    cfg = InfrastructureConfig(features={"drive": FeatureFlagOverride(enabled=True)})
    monkeypatch.setattr(feature_catalog, "get_infrastructure_config", lambda: cfg)
    monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("DALTON_AUTH_MODE", raising=False)
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "false")

    app = FastAPI()
    app.include_router(drive_app.router)
    app.include_router(drive_mount_app.router)
    client = TestClient(app)

    test_logger = logging.getLogger("test.drive_kill_switch")

    # 1. Drive on + colleague access on -> Drive stays off across all entries and startup logs an error.
    monkeypatch.setenv("DALTON_CAP_COLLEAGUE_SHARING", "true")
    monkeypatch.setenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", "false")
    with caplog.at_level(logging.ERROR, logger=test_logger.name):
        assert check_drive_startup_guard(test_logger) is True
    assert any(
        "colleague_sharing" in rec.message and "Drive" in rec.message
        for rec in caplog.records
    )
    assert is_feature_enabled_system("drive") is False
    assert effective_flags({"drive": True})["drive"] is False
    assert client.get("/api/v1/drive/spaces").status_code == 404
    assert client.get("/api/v1/livefs/drive/manifest").status_code == 404

    # 2. Drive on + computer_cli_tokens on -> Drive stays off and startup logs an error.
    caplog.clear()
    monkeypatch.setenv("DALTON_CAP_COLLEAGUE_SHARING", "false")
    monkeypatch.setenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", "true")
    with caplog.at_level(logging.ERROR, logger=test_logger.name):
        assert check_drive_startup_guard(test_logger) is True
    assert any("computer_cli_tokens" in rec.message for rec in caplog.records)
    assert is_feature_enabled_system("drive") is False
    assert effective_flags({"drive": True})["drive"] is False
    assert client.get("/api/v1/drive/spaces").status_code == 404
    assert client.get("/api/v1/livefs/drive/manifest").status_code == 404

    # 3. Drive on + both off -> Drive opens per upstream behavior with no error log.
    caplog.clear()
    monkeypatch.setenv("DALTON_CAP_COLLEAGUE_SHARING", "false")
    monkeypatch.setenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", "false")
    with caplog.at_level(logging.ERROR, logger=test_logger.name):
        assert check_drive_startup_guard(test_logger) is False
    assert caplog.records == []
    assert is_feature_enabled_system("drive") is True
    assert effective_flags({"drive": True})["drive"] is True


def test_drive_routes_return_404_before_auth_permission_and_param_checks_when_off(
    monkeypatch,
):
    """Both Drive routers return 404 before auth, permission, or query/body validation when off."""
    cfg = InfrastructureConfig(features={"drive": FeatureFlagOverride(enabled=False)})
    monkeypatch.setattr(feature_catalog, "get_infrastructure_config", lambda: cfg)

    app = FastAPI()
    app.include_router(drive_app.router)
    app.include_router(drive_mount_app.router)

    # Valid service credential emptying trash would return 403 if the kill switch ran late.
    service_principal = Principal(OWNER, "service", scopes=("drive:read", "drive:write"))
    app.dependency_overrides[get_principal] = lambda: service_principal
    # Valid mount identity with missing required `space` query param would return 422 if late.
    identity = LivefsIdentity(computer_id=COMPUTER, user_id=OWNER, root_dir=ROOT)
    app.dependency_overrides[drive_mount_app._caller] = lambda: identity

    client = TestClient(app)

    trash_resp = client.delete("/api/v1/drive/personal/trash")
    assert trash_resp.status_code == 404
    assert trash_resp.json()["detail"]["type"] == "not_found"

    bad_json_resp = client.post(
        "/api/v1/drive/personal/folders",
        content=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert bad_json_resp.status_code == 404
    assert bad_json_resp.json()["detail"]["type"] == "not_found"

    manifest_resp = client.get("/api/v1/livefs/drive/manifest")
    assert manifest_resp.status_code == 404
    assert manifest_resp.json()["code"] == "not_found"

    # Without any auth overrides (unauthenticated requests), both routers still return 404.
    app.dependency_overrides.clear()
    assert client.delete("/api/v1/drive/personal/trash").status_code == 404
    assert client.get("/api/v1/livefs/drive/manifest").status_code == 404


def _load_minio_init_module():
    spec = importlib.util.spec_from_file_location(
        "minio_init_drive_guard_test", REPO_ROOT / "scripts/standalone/minio_init.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_minio_init_skips_lifecycle_writes_and_keeps_policy_error_semantics(monkeypatch):
    """Standalone MinIO init does not write lifecycle rules and fails on policy errors other than NoSuchBucketPolicy."""
    minio_init = _load_minio_init_module()

    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "sk")
    monkeypatch.delenv("STORAGE_BUCKET_NAME", raising=False)
    monkeypatch.delenv("DALTON_ASR_STORAGE_BUCKET_NAME", raising=False)

    s3_spec = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )

    # 1. NoSuchBucketPolicy -> exit 0, and lifecycle APIs are never touched.
    fake_ok = create_autospec(s3_spec)
    fake_ok.delete_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucketPolicy", "Message": "none"}},
        "DeleteBucketPolicy",
    )
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_ok),
    )
    assert minio_init.main() == 0
    fake_ok.get_bucket_lifecycle_configuration.assert_not_called()
    fake_ok.put_bucket_lifecycle_configuration.assert_not_called()

    # 2. Other policy errors (e.g., AccessDenied) still fail with exit 1.
    fake_fail = create_autospec(s3_spec)
    fake_fail.delete_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "DeleteBucketPolicy",
    )
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_fail),
    )
    assert minio_init.main() == 1
    fake_fail.put_bucket_lifecycle_configuration.assert_not_called()


def test_minio_init_dedups_asr_bucket_by_endpoint_and_bucket_name(monkeypatch):
    """ASR bucket deduplication keys on (endpoint, bucket), not bucket name alone."""
    minio_init = _load_minio_init_module()

    s3_spec = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )

    # Case 1: STORAGE_BUCKET_NAME is on external S3 while ASR uses the same bucket name on local MinIO.
    fake_s3 = create_autospec(s3_spec)
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_s3),
    )
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("MINIO_ROOT_USER", "ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "sk")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://s3.external.example")
    monkeypatch.setenv("STORAGE_BUCKET_NAME", "shared-media")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "shared-media")

    assert minio_init.main() == 0
    cleared = [call.kwargs["Bucket"] for call in fake_s3.delete_bucket_policy.call_args_list]
    assert cleared == ["chat-uploads", "shared-media"]

    # Case 2: Both STORAGE_BUCKET_NAME and ASR point to the same local MinIO endpoint and bucket -> init once.
    fake_s3_same = create_autospec(s3_spec)
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_s3_same),
    )
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")

    assert minio_init.main() == 0
    cleared_same = [
        call.kwargs["Bucket"] for call in fake_s3_same.delete_bucket_policy.call_args_list
    ]
    assert cleared_same == ["chat-uploads", "shared-media"]


def test_langalpha_doc_drive_guide_leads_with_the_administrators_switch():
    """While ``features.drive.enabled`` is off the Experiments row is hidden too, so
    a user sent straight to Experiments looks for a setting that is not there. The
    reference names the administrator's switch first and the user's own second."""
    # The prose the agent reads: without the hook marker standing over it.
    source = LANGALPHA_DOC_DRIVE.read_text(encoding="utf-8")
    intro = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL).split("## What it is")[0]

    # The switch that hides the other one, by the key the administrator sets.
    assert "features.drive.enabled" in intro
    deployment_at = intro.index("features.drive.enabled")
    assert "administrator" in intro[: deployment_at + 200]

    # Then where the user's own switch is, once the deployment's is on.
    users_at = intro.index("Experiments")
    assert deployment_at < users_at

    # The sentence that made the user's switch sound like the only one.
    assert "A user turns it on among the experiments in their settings." not in intro


def test_multipart_cleanup_warning_says_what_to_set_on_minio():
    """MinIO's lifecycle API serves no ``AbortIncompleteMultipartUpload`` rule, so
    against it the rule check is ``False`` at every startup and the warning is all
    an operator gets: it has to name MinIO's own setting beside the S3 and R2 rule."""
    tree = ast.parse(SETUP_PY_PATH.read_text(encoding="utf-8"))
    messages = [
        node.args[0].value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "warning"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and "Storage bucket has no rule expiring incomplete multipart uploads" in node.args[0].value
        )
    ]
    assert len(messages) == 1, messages
    message = messages[0]

    assert "AbortIncompleteMultipartUpload" in message
    assert "stale_uploads_expiry" in message
    assert "MINIO_API_STALE_UPLOADS_EXPIRY" in message
