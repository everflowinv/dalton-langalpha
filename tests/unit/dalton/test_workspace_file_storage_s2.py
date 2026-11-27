"""Unit tests for Batch 1 Step 2: storage_config, write-inline-only & upload fallback,
startup reference gate, 60s download ticket, MinIO bucket rules, and exclude_rel_globs.

All storage facade and database doubles use ``create_autospec``; S3/DB/sandbox test
fakes use explicit classes with concrete method signatures (no ``Mock(**kwargs)``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import wsfiles_transfer_runtime as transfer_rt
from ptc_agent.core.sandbox.files import FileOverCap
from src.dalton import (
    download_ticket,
    storage_bucket_rules,
    storage_config,
    storage_refs,
    wsfiles_write_mode,
)
from src.dalton.app import workspace_file_download
from src.dalton.auth import bootstrap as auth_bootstrap
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.profile import DaltonStartupConfigError
from src.dalton.settings import (
    DALTON_CORE_YAML_PATH,
    DALTON_YAML_PATH,
    DaltonConfig,
    _load_profile_dalton_raw,
)
from src.server.app.workspace_files import crud as ws_crud
from src.server.database import workspace_file as wf_db
from src.server.services import workspace_file_containment as containment
from src.server.services.persistence import backup, blobs, download_link, restore, transfer
from src.server.services.persistence.sync_result import UnsavedFile
from src.server.services.persistence.transfer import PACK_CUTOFF, ScanEntry, ScanResult
from tests.unit.server.services._no_db import NoDbConn


# ---------------------------------------------------------------------------
# Helpers & explicit fakes (no Mock(**kwargs))
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(
    path: str,
    data: bytes,
    *,
    mode: int = 0o644,
    mtime_ns: int = 1_700_000_000_000_000_000,
    is_binary: bool = False,
) -> ScanEntry:
    return ScanEntry(
        path=path,
        kind="file",
        size=len(data),
        mtime_ns=mtime_ns,
        mode=mode,
        sha256=_sha(data),
        symlink_target=None,
        is_binary=is_binary,
    )


def _scan(*entries: ScanEntry) -> ScanResult:
    return ScanResult(
        entries=list(entries),
        oversized=[],
        errors=[],
        hashed=len(entries),
        reused=0,
    )


class _FakeSandboxConfig:
    def __init__(self, provider: str = "docker") -> None:
        self.sandbox = type("_Sb", (), {"provider": provider})()


class _FakeSandbox:
    """Explicit sandbox double for sync/restore tests."""

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        working_dir: str = "/workspace",
        dir_name: str = "ws-s2",
        provider: str = "docker",
    ) -> None:
        self.working_dir = working_dir
        self.recreatable = True
        self._dir_name = dir_name
        self._prefix = f"{working_dir}/{dir_name}/"
        self.config = _FakeSandboxConfig(provider=provider)
        self.files: dict[str, bytes] = dict(files)
        self.uploaded_staged: dict[str, bytes] = {}
        self.restored_files: dict[str, bytes] = {}

    async def adownload_file_bytes(self, abs_path: str) -> bytes | None:
        if abs_path.startswith(self._prefix):
            rel = abs_path[len(self._prefix) :]
        elif abs_path.startswith(f"{self.working_dir}/"):
            rel = abs_path[len(self.working_dir) + 1 :]
        else:
            rel = abs_path
        return self.files.get(rel)

    async def adownload_file_bytes_capped(self, abs_path: str, cap: Any) -> bytes | None:
        data = await self.adownload_file_bytes(abs_path)
        if data is None:
            return None
        buf = bytearray()
        for i in range(0, len(data), 4096):
            buf.extend(data[i : i + 4096])
            limit = cap(buf)
            if len(buf) > limit:
                raise FileOverCap(limit)
        return bytes(buf)

    async def aupload_file_bytes(self, abs_path: str, data: bytes) -> bool:
        self.uploaded_staged[abs_path] = data
        return True

    async def acreate_directory(self, _path: str) -> bool:
        return True

    async def acreate_directories(self, _paths: list[str]) -> bool:
        return True

    async def _runtime_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return None


class _FakeClientError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message or code}}


class _FakeS3Client:
    """Explicit S3 client double with concrete method signatures."""

    def __init__(
        self,
        *,
        buckets: set[str] | None = None,
        versioning: dict[str, str] | None = None,
        lifecycle: dict[str, list[dict[str, Any]]] | None = None,
        policy: dict[str, str] | None = None,
        corrupt_readback: str | None = None,
    ) -> None:
        self.buckets: set[str] = set(buckets or {"dalton-main", "dalton-asr"})
        self.versioning: dict[str, str] = dict(versioning or {})
        self.lifecycle: dict[str, list[dict[str, Any]]] = {
            k: list(v) for k, v in (lifecycle or {}).items()
        }
        self.policy: dict[str, str] = dict(policy or {})
        self.corrupt_readback = corrupt_readback

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.buckets:
            raise _FakeClientError("404", f"Missing bucket {Bucket}")
        return {}

    def get_bucket_policy(self, *, Bucket: str) -> dict[str, Any]:
        if self.corrupt_readback == "policy":
            return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": []})}
        if Bucket not in self.policy:
            raise _FakeClientError("NoSuchBucketPolicy")
        return {"Policy": self.policy[Bucket]}

    def put_bucket_policy(self, *, Bucket: str, Policy: str) -> dict[str, Any]:
        self.policy[Bucket] = Policy
        return {}

    def delete_bucket_policy(self, *, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.policy:
            raise _FakeClientError("NoSuchBucketPolicy")
        del self.policy[Bucket]
        return {}

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, Any]:
        if self.corrupt_readback == "versioning":
            return {"Status": "Suspended"}
        status = self.versioning.get(Bucket)
        return {"Status": status} if status is not None else {}

    def put_bucket_versioning(
        self, *, Bucket: str, VersioningConfiguration: dict[str, Any]
    ) -> dict[str, Any]:
        self.versioning[Bucket] = str(VersioningConfiguration.get("Status") or "")
        return {}

    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> dict[str, Any]:
        if self.corrupt_readback == "lifecycle":
            return {"Rules": []}
        if Bucket not in self.lifecycle:
            raise _FakeClientError("NoSuchLifecycleConfiguration")
        return {"Rules": list(self.lifecycle[Bucket])}

    def put_bucket_lifecycle_configuration(
        self, *, Bucket: str, LifecycleConfiguration: dict[str, Any]
    ) -> dict[str, Any]:
        self.lifecycle[Bucket] = list(LifecycleConfiguration.get("Rules") or [])
        return {}


class _FakeProbeCursor:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._result: Any = None

    async def __aenter__(self) -> _FakeProbeCursor:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self._result = self._handler(sql, params)

    async def fetchone(self) -> Any:
        return self._result

    async def fetchall(self) -> list[Any]:
        if self._result is None:
            return []
        if isinstance(self._result, list):
            return self._result
        return [self._result]


class _FakeProbeConn:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    @asynccontextmanager
    async def transaction(self):
        yield

    def cursor(self) -> _FakeProbeCursor:
        return _FakeProbeCursor(self._handler)


# ---------------------------------------------------------------------------
# 1. storage_config.py tests
# ---------------------------------------------------------------------------


class TestStorageConfig:
    def test_bucket_resolution_matrix_storage_vs_s3_bucket(self) -> None:
        # STORAGE_BUCKET_NAME unset
        assert storage_config.resolve_storage_bucket("s3", env={}) == ""
        assert (
            storage_config.resolve_storage_bucket("s3", env={"S3_BUCKET_NAME": "legacy-b"})
            == "legacy-b"
        )
        # STORAGE_BUCKET_NAME explicit empty string disables without falling back to S3_BUCKET_NAME
        assert (
            storage_config.resolve_storage_bucket(
                "s3", env={"STORAGE_BUCKET_NAME": "", "S3_BUCKET_NAME": "legacy-b"}
            )
            == ""
        )
        assert (
            storage_config.resolve_storage_bucket(
                "s3", env={"STORAGE_BUCKET_NAME": "   ", "S3_BUCKET_NAME": "legacy-b"}
            )
            == ""
        )
        # STORAGE_BUCKET_NAME non-empty wins over S3_BUCKET_NAME
        assert (
            storage_config.resolve_storage_bucket(
                "s3",
                env={"STORAGE_BUCKET_NAME": "dalton-main", "S3_BUCKET_NAME": "legacy-b"},
            )
            == "dalton-main"
        )
        # OSS reads OSS_BUCKET_NAME
        assert (
            storage_config.resolve_storage_bucket("oss", env={"OSS_BUCKET_NAME": "oss-b"})
            == "oss-b"
        )

    def test_endpoint_resolution_falls_back_to_s3_endpoint(self) -> None:
        assert storage_config.resolve_storage_endpoint_url(env={}) == ""
        assert (
            storage_config.resolve_storage_endpoint_url(
                env={"S3_ENDPOINT_URL": "http://127.0.0.1:9000"}
            )
            == "http://127.0.0.1:9000"
        )
        assert (
            storage_config.resolve_storage_endpoint_url(
                env={
                    "STORAGE_ENDPOINT_URL": "  ",
                    "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
                }
            )
            == "http://127.0.0.1:9000"
        )
        assert (
            storage_config.resolve_storage_endpoint_url(
                env={
                    "STORAGE_ENDPOINT_URL": "http://minio:9000",
                    "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
                }
            )
            == "http://minio:9000"
        )

    def test_env_provider_none_overrides_yaml_s3(self) -> None:
        yaml_s3 = {"storage": {"provider": "s3"}}
        assert (
            storage_config.resolve_storage_provider(
                env={"STORAGE_PROVIDER": "none"}, agent_config=yaml_s3
            )
            == "none"
        )
        assert (
            storage_config.resolve_storage_provider(
                env={"STORAGE_PROVIDER": "  NONE "}, agent_config=yaml_s3
            )
            == "none"
        )
        # Empty yaml provider delegates to env
        assert (
            storage_config.resolve_storage_provider(
                env={"STORAGE_PROVIDER": "s3"}, agent_config={"storage": {"provider": ""}}
            )
            == "s3"
        )
        # Everflow existing config: yaml s3 + no bucket -> disabled
        resolved = storage_config.resolve(env={}, agent_config=yaml_s3)
        assert resolved.is_storage_enabled is False
        assert resolved.provider == "none"

    @pytest.mark.parametrize("bad_provider", ["oss", "r2"])
    def test_core_profile_rejects_non_s3_providers_everflow_allows(
        self, bad_provider: str
    ) -> None:
        core_env = {"DALTON_PROFILE": "core", "STORAGE_PROVIDER": bad_provider}
        with pytest.raises(DaltonStartupConfigError) as exc_info:
            storage_config.validate_core_storage_provider(
                env=core_env, agent_config={"storage": {"provider": ""}}
            )
        assert storage_config.CORE_STORAGE_PROVIDER_ERROR in str(exc_info.value)

        # Also rejected if set via yaml in core profile
        with pytest.raises(DaltonStartupConfigError) as exc_yaml:
            storage_config.validate_core_storage_provider(
                env={"DALTON_PROFILE": "core"},
                agent_config={"storage": {"provider": bad_provider}},
            )
        assert storage_config.CORE_STORAGE_PROVIDER_ERROR in str(exc_yaml.value)

        # Core allows s3 and none
        storage_config.validate_core_storage_provider(
            env={"DALTON_PROFILE": "core", "STORAGE_PROVIDER": "s3"},
            agent_config={"storage": {"provider": ""}},
        )
        storage_config.validate_core_storage_provider(
            env={"DALTON_PROFILE": "core", "STORAGE_PROVIDER": "none"},
            agent_config={"storage": {"provider": "s3"}},
        )

        # Everflow does NOT restrict oss / r2
        storage_config.validate_core_storage_provider(
            env={"STORAGE_PROVIDER": bad_provider},
            agent_config={"storage": {"provider": bad_provider}},
        )

    def test_core_defaults_off_by_default_and_opt_in(self) -> None:
        core_env = {"DALTON_PROFILE": "core"}
        yaml_s3 = {"storage": {"provider": "s3"}}
        cfg_default = storage_config.resolve(env=core_env, agent_config=yaml_s3)
        assert cfg_default.is_storage_enabled is False
        assert cfg_default.bucket_name == ""
        assert cfg_default.endpoint_url == ""

        cfg_with_defaults = storage_config.resolve(
            env=core_env, agent_config=yaml_s3, apply_core_defaults=True
        )
        assert cfg_with_defaults.is_storage_enabled is True
        assert cfg_with_defaults.bucket_name == "dalton-storage"
        assert cfg_with_defaults.endpoint_url == "http://minio:9000"

        # Explicit empty STORAGE_BUCKET_NAME="" disables even when apply_core_defaults=True
        cfg_explicit_off = storage_config.resolve(
            env={"DALTON_PROFILE": "core", "STORAGE_BUCKET_NAME": ""},
            agent_config=yaml_s3,
            apply_core_defaults=True,
        )
        assert cfg_explicit_off.is_storage_enabled is False
        assert cfg_explicit_off.bucket_name == ""

    def test_show_cli_prints_json_with_no_secrets(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sentinels = {
            "STORAGE_ACCESS_KEY_ID": "AKIA-SENTINEL-ACCESS-111",
            "STORAGE_SECRET_ACCESS_KEY": "SECRET-SENTINEL-KEY-222",
            "AWS_SECRET_ACCESS_KEY": "AWS-SENTINEL-KEY-333",
            "MINIO_ROOT_PASSWORD": "MINIO-SENTINEL-PASS-444",
            "JWT_SIGNING_SECRET": "JWT-SENTINEL-SECRET-555",
            "INTERNAL_SERVICE_TOKEN": "INTERNAL-SENTINEL-666",
        }
        for k, v in sentinels.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("STORAGE_BUCKET_NAME", "dalton-main")
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")

        rc = storage_config.main(["show"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert set(parsed.keys()) == {
            "is_storage_enabled",
            "provider",
            "bucket_name",
            "endpoint_url",
            "write_inline_only",
            "exclude_rel_globs",
        }
        assert parsed["is_storage_enabled"] is True
        assert parsed["bucket_name"] == "dalton-main"
        for secret_val in sentinels.values():
            assert secret_val not in out


# ---------------------------------------------------------------------------
# 2. Write-inline-only switch tests
# ---------------------------------------------------------------------------


class TestWriteInlineOnlySwitch:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " Yes "])
    def test_env_truthy_values(self, raw: str) -> None:
        assert storage_config.is_wsfiles_write_inline_only(
            env={"DALTON_WSFILES_WRITE_INLINE_ONLY": raw}
        ) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " No "])
    def test_env_falsy_values(self, raw: str) -> None:
        assert storage_config.is_wsfiles_write_inline_only(
            env={"DALTON_WSFILES_WRITE_INLINE_ONLY": raw}
        ) is False

    def test_invalid_env_logs_warning_and_returns_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="src.dalton.storage_config"):
            val = storage_config.is_wsfiles_write_inline_only(
                env={"DALTON_WSFILES_WRITE_INLINE_ONLY": "maybe"}
            )
        assert val is False
        assert any("DALTON_WSFILES_WRITE_INLINE_ONLY" in rec.message for rec in caplog.records)

    def test_yaml_fallback_and_live_re_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_WRITE_INLINE_ONLY", raising=False)
        assert storage_config.is_wsfiles_write_inline_only() is False

        with patch.object(
            storage_config,
            "_load_dalton_workspace_files_raw",
            return_value={"write_inline_only": True},
        ):
            assert storage_config.is_wsfiles_write_inline_only() is True
            monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "0")
            assert storage_config.is_wsfiles_write_inline_only() is False
            monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "1")
            assert storage_config.is_wsfiles_write_inline_only() is True


# ---------------------------------------------------------------------------
# 3. Write routing + cold restore (§7 Acceptance 3 ②) + Upload fallback
# ---------------------------------------------------------------------------


class TestWriteRoutingAndFallback:
    WS = "ws-s2-routing"
    USER = "user-s2"
    ROOT = "/workspace"
    DIR_NAME = "ws-s2"
    LAYOUT = SandboxLayout.for_root(ROOT).for_workspace(DIR_NAME)
    CLOCK = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)

    @asynccontextmanager
    async def _fake_lock(self, _ws: str, *, conn: Any = None):
        yield NoDbConn()

    @pytest.mark.asyncio
    async def test_write_inline_only_routing_and_packed_member_transition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Switch ON: 10 MiB & <=256 KiB non-git -> inline; 150 MiB & .git -> object storage;
        unchanged packed member stays packed, changed packed member moves inline clearing pack pointers.
        """
        small_unchanged = b"unchanged-small"
        small_changed_old = b"old-small"
        small_changed_new = b"new-small-content"
        git_head = b"ref: refs/heads/main\n"
        ten_mib = b"\x00\xff" + b"A" * ((10 << 20) - 2)
        one_fifty_mib = b"\x00\xff" + b"B" * ((150 << 20) - 2)

        old_chunk = _sha(small_unchanged + small_changed_old + git_head)
        existing = {
            "unchanged.txt": {
                "kind": "file",
                "file_size": len(small_unchanged),
                "content_hash": _sha(small_unchanged),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "pack_sha256": old_chunk,
                "pack_offset": 0,
                "blob_sha256": None,
                "is_binary": False,
            },
            "changed.txt": {
                "kind": "file",
                "file_size": len(small_changed_old),
                "content_hash": _sha(small_changed_old),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "pack_sha256": old_chunk,
                "pack_offset": len(small_unchanged),
                "blob_sha256": None,
                "is_binary": False,
            },
            ".git/HEAD": {
                "kind": "file",
                "file_size": len(git_head),
                "content_hash": _sha(git_head),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "pack_sha256": old_chunk,
                "pack_offset": len(small_unchanged) + len(small_changed_old),
                "blob_sha256": None,
                "is_binary": False,
            },
        }
        sandbox_files = {
            "unchanged.txt": small_unchanged,
            "changed.txt": small_changed_new,
            ".git/HEAD": git_head,
            "ten.bin": ten_mib,
            "huge.bin": one_fifty_mib,
        }
        sandbox = _FakeSandbox(sandbox_files, working_dir=self.ROOT, dir_name=self.DIR_NAME)
        scan_result = _scan(
            _entry("unchanged.txt", small_unchanged),
            _entry("changed.txt", small_changed_new),
            _entry(".git/HEAD", git_head),
            _entry("ten.bin", ten_mib, is_binary=True),
            _entry("huge.bin", one_fifty_mib, is_binary=True),
        )

        stored_rows: dict[str, dict[str, Any]] = {}
        stored_blobs: dict[str, bytes] = {}

        async def _fake_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            for r in rows:
                stored_rows[r["file_path"]] = dict(r)
            return len(rows)

        async def _fake_store_blob(_uid: str, sha: str, content: bytes) -> None:
            stored_blobs[sha] = content

        async def _fake_pack_direct(
            _sb: Any, members: list[dict[str, Any]], *, layout: Any, release: list[str] | None = None
        ) -> dict[str, Any]:
            # Only .git/HEAD should reach pack_direct
            payload = b"".join(sandbox_files[m["path"]] for m in members)
            chunk_sha = _sha(payload)
            chunk_path = f"_internal/packs/chunk-{chunk_sha}"
            sandbox.files[chunk_path] = payload
            out_members = []
            offset = 0
            for m in members:
                raw = sandbox_files[m["path"]]
                out_members.append(
                    {"path": m["path"], "offset": offset, "size": len(raw), "sha256": m["sha256"]}
                )
                offset += len(raw)
            return {
                "chunks": [
                    {
                        "path": chunk_path,
                        "sha256": chunk_sha,
                        "size": len(payload),
                        "members": out_members,
                    }
                ],
                "changed": [],
            }

        monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "1")
        with (
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "s3"),
            patch.object(backup, "workspace_sync_lock", self._fake_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=self.CLOCK)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value=existing)),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", create_autospec(backup.delete_removed_files, return_value=0)),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=True),
            patch.object(backup, "workspace_owner", create_autospec(backup.workspace_owner, return_value=self.USER)),
            patch.object(blobs, "registered_blobs", create_autospec(blobs.registered_blobs, return_value=set())),
            patch.object(blobs, "store_blob", create_autospec(blobs.store_blob, side_effect=_fake_store_blob)),
            patch.object(blobs, "pack_direct", create_autospec(blobs.pack_direct, side_effect=_fake_pack_direct)) as mock_pack,
            patch.object(blobs, "unlink_direct", create_autospec(blobs.unlink_direct, return_value=1)),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            res = await backup.sync_to_db(self.WS, sandbox, layout=self.LAYOUT)

        assert res.unsaved == []
        # Unchanged packed member stays untouched (not overwritten in stored_rows)
        assert "unchanged.txt" not in stored_rows
        # Changed small non-git file moved inline and cleared pack pointers
        assert stored_rows["changed.txt"]["content_text"] == small_changed_new.decode()
        assert stored_rows["changed.txt"]["pack_sha256"] is None
        assert stored_rows["changed.txt"]["pack_offset"] is None
        assert stored_rows["changed.txt"]["blob_sha256"] is None
        # 10 MiB non-git file written inline
        assert stored_rows["ten.bin"]["content_binary"] == ten_mib
        assert stored_rows["ten.bin"]["blob_sha256"] is None
        assert stored_rows["ten.bin"]["pack_sha256"] is None
        # 150 MiB file written to main bucket as blob pointer
        assert stored_rows["huge.bin"]["blob_sha256"] == _sha(one_fifty_mib)
        assert stored_rows["huge.bin"]["content_binary"] is None
        assert _sha(one_fifty_mib) in stored_blobs
        # .git/HEAD stayed in pack_members and packed to main bucket
        assert stored_rows[".git/HEAD"]["pack_sha256"] is not None
        assert stored_rows[".git/HEAD"]["content_text"] is None
        packed_paths = [m["path"] for m in mock_pack.await_args.args[1]]
        assert packed_paths == [".git/HEAD"]

    @pytest.mark.asyncio
    async def test_write_inline_only_off_keeps_existing_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        small_data = b"hello-pack"
        ten_mib = b"Z" * (10 << 20)
        sandbox_files = {"small.txt": small_data, "ten.bin": ten_mib}
        sandbox = _FakeSandbox(sandbox_files, working_dir=self.ROOT, dir_name=self.DIR_NAME)
        scan_result = _scan(
            _entry("small.txt", small_data),
            _entry("ten.bin", ten_mib, is_binary=True),
        )
        stored_rows: dict[str, dict[str, Any]] = {}
        stored_blobs: dict[str, bytes] = {}

        async def _fake_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            for r in rows:
                stored_rows[r["file_path"]] = dict(r)
            return len(rows)

        async def _fake_store_blob(_uid: str, sha: str, content: bytes) -> None:
            stored_blobs[sha] = content

        async def _fake_pack_direct(
            _sb: Any, members: list[dict[str, Any]], *, layout: Any, release: list[str] | None = None
        ) -> dict[str, Any]:
            payload = b"".join(sandbox_files[m["path"]] for m in members)
            csha = _sha(payload)
            cpath = f"_internal/packs/chunk-{csha}"
            sandbox.files[cpath] = payload
            return {
                "chunks": [
                    {
                        "path": cpath,
                        "sha256": csha,
                        "size": len(payload),
                        "members": [
                            {"path": m["path"], "offset": 0, "size": m["size"], "sha256": m["sha256"]}
                            for m in members
                        ],
                    }
                ],
                "changed": [],
            }

        monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "0")
        with (
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "s3"),
            patch.object(backup, "workspace_sync_lock", self._fake_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=self.CLOCK)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value={})),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", create_autospec(backup.delete_removed_files, return_value=0)),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=True),
            patch.object(backup, "workspace_owner", create_autospec(backup.workspace_owner, return_value=self.USER)),
            patch.object(blobs, "registered_blobs", create_autospec(blobs.registered_blobs, return_value=set())),
            patch.object(blobs, "store_blob", create_autospec(blobs.store_blob, side_effect=_fake_store_blob)),
            patch.object(blobs, "pack_direct", create_autospec(blobs.pack_direct, side_effect=_fake_pack_direct)),
            patch.object(blobs, "unlink_direct", create_autospec(blobs.unlink_direct, return_value=1)),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            res = await backup.sync_to_db(self.WS, sandbox, layout=self.LAYOUT)

        assert res.unsaved == []
        assert stored_rows["small.txt"]["pack_sha256"] is not None
        assert stored_rows["ten.bin"]["blob_sha256"] == _sha(ten_mib)
        assert stored_rows["ten.bin"]["content_binary"] is None

    @pytest.mark.asyncio
    async def test_acceptance_3_2_rewrite_150m_and_10m_inline_only_then_cold_restore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§7 Acceptance 3 ②: with DALTON_WSFILES_WRITE_INLINE_ONLY=1, overwrite 150 MiB
        and 10 MiB files, sync, then cold-restore: both restore with new contents,
        150 MiB is a pointer row and 10 MiB is an inline row.
        """
        old_150_sha = _sha(b"old-150")
        old_10_sha = _sha(b"old-10")
        new_150 = b"\x00\xff\x02" + b"\x02" * ((150 << 20) - 3)
        new_10 = b"\x00\xff\x03" + b"\x03" * ((10 << 20) - 3)

        existing = {
            "big150.bin": {
                "kind": "file",
                "file_size": 150 << 20,
                "content_hash": old_150_sha,
                "mtime_ns": 1_600_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": old_150_sha,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": True,
            },
            "mid10.bin": {
                "kind": "file",
                "file_size": 10 << 20,
                "content_hash": old_10_sha,
                "mtime_ns": 1_600_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": old_10_sha,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": True,
            },
        }
        sandbox_files = {"big150.bin": new_150, "mid10.bin": new_10}
        sync_sandbox = _FakeSandbox(sandbox_files, working_dir=self.ROOT, dir_name=self.DIR_NAME)
        scan_result = _scan(
            _entry("big150.bin", new_150, is_binary=True),
            _entry("mid10.bin", new_10, is_binary=True),
        )
        manifest_rows: dict[str, dict[str, Any]] = {}
        blob_store: dict[str, bytes] = {}

        async def _fake_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            for r in rows:
                manifest_rows[r["file_path"]] = dict(r)
            return len(rows)

        async def _fake_store_blob(_uid: str, sha: str, content: bytes) -> None:
            blob_store[sha] = content

        monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "1")
        with (
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "s3"),
            patch.object(backup, "workspace_sync_lock", self._fake_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=self.CLOCK)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value=existing)),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", create_autospec(backup.delete_removed_files, return_value=0)),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=True),
            patch.object(backup, "workspace_owner", create_autospec(backup.workspace_owner, return_value=self.USER)),
            patch.object(blobs, "registered_blobs", create_autospec(blobs.registered_blobs, return_value=set())),
            patch.object(blobs, "store_blob", create_autospec(blobs.store_blob, side_effect=_fake_store_blob)),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            sync_res = await backup.sync_to_db(self.WS, sync_sandbox, layout=self.LAYOUT)

        assert sync_res.unsaved == []
        # Verify storage shapes: 150 MiB is pointer row, 10 MiB is inline row (pointers cleared)
        assert manifest_rows["big150.bin"]["blob_sha256"] == _sha(new_150)
        assert manifest_rows["big150.bin"]["content_binary"] is None
        assert manifest_rows["mid10.bin"]["blob_sha256"] is None
        assert manifest_rows["mid10.bin"]["pack_sha256"] is None
        assert manifest_rows["mid10.bin"]["content_binary"] == new_10

        # Cold restore into a brand new sandbox
        restore_sandbox = _FakeSandbox({}, working_dir=self.ROOT, dir_name=self.DIR_NAME)

        async def _fake_pull_direct(sb: _FakeSandbox, items: list[dict[str, Any]], **_kw: Any) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for item in items:
                staged_rel = item["file"]
                staged_abs = f"{self.LAYOUT.workspace}/{staged_rel}"
                sb.restored_files[item["path"]] = sb.uploaded_staged[staged_abs]
                out[item["path"]] = {"status": "ok"}
            return out

        async def _fake_get_files(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [dict(v) for v in manifest_rows.values()]

        async def _fake_fetch_blob(_uid: str, sha: str) -> bytes | None:
            return blob_store.get(sha)

        @asynccontextmanager
        async def _noop_lock(*_a: Any, **_kw: Any):
            yield NoDbConn()

        from src.server.services.persistence import resolve as persist_resolve

        with (
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "s3"),
            patch.object(restore, "workspace_sync_lock", _noop_lock),
            patch.object(restore, "hold_workspace_skill_sync", _noop_lock),
            patch.object(restore, "set_files_restore_incomplete", create_autospec(restore.set_files_restore_incomplete, return_value=True)),
            patch.object(restore, "workspace_owner", create_autospec(restore.workspace_owner, return_value=self.USER)),
            patch.object(restore, "get_files_for_workspace", create_autospec(wf_db.get_files_for_workspace, side_effect=_fake_get_files)),
            patch.object(restore, "pull_direct", create_autospec(restore.pull_direct, side_effect=_fake_pull_direct)),
            patch.object(persist_resolve, "fetch_blob", create_autospec(persist_resolve.fetch_blob, side_effect=_fake_fetch_blob)),
        ):
            restored_res = await restore.restore_to_sandbox(
                self.WS, restore_sandbox, layout=self.LAYOUT
            )

        assert restored_res == {"restored": 2, "errors": 0}
        assert _sha(restore_sandbox.restored_files["big150.bin"]) == _sha(new_150)
        assert len(restore_sandbox.restored_files["big150.bin"]) == 150 << 20
        assert _sha(restore_sandbox.restored_files["mid10.bin"]) == _sha(new_10)
        assert len(restore_sandbox.restored_files["mid10.bin"]) == 10 << 20

    @pytest.mark.asyncio
    async def test_upload_failure_bounded_64m_inline_fallback_and_git_hold(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failed non-git <=100 MiB uploads fall back inline within 64 MiB budget with WARNING;
        over-budget, >100 MiB, .git files, and reason=='changed' stay in unsaved and .git stays held.
        """
        ten_mib = b"F" * (10 << 20)
        huge = b"H" * (110 << 20)
        changed_data = b"C" * (1 << 20)
        git_obj = b"G" * (PACK_CUTOFF + 10)
        git_ok = b"K" * (PACK_CUTOFF + 20)
        small_pack_fail = b"small-packed-fail"

        sandbox_files = {
            f"f{i}.bin": bytes([i]) + ten_mib[1:] for i in range(7)
        }
        sandbox_files["huge.bin"] = huge
        sandbox_files["changed.bin"] = changed_data
        sandbox_files[".git/objects/aa/fail"] = git_obj
        sandbox_files[".git/objects/bb/ok"] = git_ok
        sandbox_files["small.txt"] = small_pack_fail

        sandbox = _FakeSandbox(sandbox_files, working_dir=self.ROOT, dir_name=self.DIR_NAME)
        entries = [
            _entry(f"f{i}.bin", sandbox_files[f"f{i}.bin"], is_binary=True)
            for i in range(7)
        ] + [
            _entry("huge.bin", huge, is_binary=True),
            _entry("changed.bin", changed_data, is_binary=True),
            _entry(".git/objects/aa/fail", git_obj, is_binary=True),
            _entry(".git/objects/bb/ok", git_ok, is_binary=True),
            _entry("small.txt", small_pack_fail, is_binary=False),
        ]
        scan_result = _scan(*entries)

        inline_upserted: dict[str, dict[str, Any]] = {}
        final_rows_upserted: list[dict[str, Any]] = []

        async def _fake_blobs_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            for r in rows:
                inline_upserted[r["file_path"]] = dict(r)
            return len(rows)

        async def _fake_backup_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            final_rows_upserted.extend(rows)
            return len(rows)

        async def _fake_persist_blobs(
            _uid: str, _ws: str, _sb: Any, blob_entries: list[ScanEntry], *, layout: Any
        ) -> tuple[list[dict[str, Any]], list[UnsavedFile]]:
            ok_rows = []
            unsaved = []
            for e in blob_entries:
                if e.path == ".git/objects/bb/ok":
                    ok_rows.append({"file_path": e.path, "blob_sha256": e.sha256})
                elif e.path == "changed.bin":
                    unsaved.append(UnsavedFile(e.path, "changed", e.size))
                else:
                    unsaved.append(UnsavedFile(e.path, "failed", e.size))
            return ok_rows, unsaved

        async def _fake_persist_packed(
            _uid: str, _ws: str, _sb: Any, members: list[ScanEntry], _existing: Any, **_kw: Any
        ) -> tuple[list[dict[str, Any]], list[UnsavedFile], int]:
            return [], [UnsavedFile(m.path, "failed", m.size) for m in members], 0

        monkeypatch.setenv("DALTON_WSFILES_WRITE_INLINE_ONLY", "0")
        with (
            caplog.at_level(logging.WARNING, logger="src.dalton.wsfiles_write_mode"),
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "s3"),
            patch.object(backup, "workspace_sync_lock", self._fake_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=self.CLOCK)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value={})),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", create_autospec(backup.delete_removed_files, return_value=0)),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_backup_upsert)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_blobs_upsert)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=True),
            patch.object(backup, "workspace_owner", create_autospec(backup.workspace_owner, return_value=self.USER)),
            patch.object(backup, "_persist_blobs", create_autospec(blobs._persist_blobs, side_effect=_fake_persist_blobs)),
            patch.object(backup, "_persist_packed", create_autospec(blobs._persist_packed, side_effect=_fake_persist_packed)),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            res = await backup.sync_to_db(self.WS, sandbox, layout=self.LAYOUT)

        # First 6 x 10 MiB = 60 MiB + small.txt (17 B) <= 64 MiB fell back inline!
        for i in range(6):
            assert f"f{i}.bin" in inline_upserted
        assert "small.txt" in inline_upserted
        # 7th 10 MiB file would push total to 70 MiB > 64 MiB -> stays unsaved
        assert "f6.bin" not in inline_upserted
        # >100 MiB, changed, and .git never fall back inline
        assert "huge.bin" not in inline_upserted
        assert "changed.bin" not in inline_upserted
        assert ".git/objects/aa/fail" not in inline_upserted

        unsaved_map = {(u.path, u.reason) for u in res.unsaved}
        assert ("f6.bin", "failed") in unsaved_map
        assert ("huge.bin", "failed") in unsaved_map
        assert ("changed.bin", "changed") in unsaved_map
        assert (".git/objects/aa/fail", "failed") in unsaved_map
        # hold_repos protected the git repo whole (dropped .git/objects/bb/ok from final_rows)
        assert (".git", "history_held") in unsaved_map
        assert all(r["file_path"] != ".git/objects/bb/ok" for r in final_rows_upserted)
        # Succeeded fallback files are not in unsaved, not in final rows, and counted in synced
        for i in range(6):
            assert all(u.path != f"f{i}.bin" for u in res.unsaved)
            assert all(r["file_path"] != f"f{i}.bin" for r in final_rows_upserted)
        assert res.synced == 7  # 6 x 10 MiB + small.txt
        # One WARNING per fallback file naming path, size, workspace
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_msgs) == 7
        assert any("f0.bin" in m and self.WS in m and str(len(ten_mib)) in m for m in warn_msgs)


# ---------------------------------------------------------------------------
# 4. Startup reference gate tests
# ---------------------------------------------------------------------------


class TestStartupReferenceGate:
    @pytest.mark.asyncio
    async def test_skips_probes_when_storage_enabled(self) -> None:
        called = False

        def _handler(_sql: str, _params: Any = None) -> Any:
            nonlocal called
            called = True
            return (1,)

        with patch.object(storage_refs, "is_storage_enabled", return_value=True):
            outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_handler)
            )
        assert outcomes == []
        assert called is False

    @pytest.mark.asyncio
    async def test_passes_when_storage_off_and_no_references(self) -> None:
        def _handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            return (0,)

        with patch.object(storage_refs, "is_storage_enabled", return_value=False):
            outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_handler)
            )
        assert len(outcomes) == len(storage_refs.STARTUP_MAIN_BUCKET_PROBES)
        assert all(not o.has_references and o.hit_count == 0 and o.error is None for o in outcomes)

    @pytest.mark.asyncio
    async def test_warns_only_never_refuses_when_consumer_hits(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM workspace_files" in sql:
                return (7,)
            if "FROM chat_attachments" in sql:
                return (storage_refs.STARTUP_COUNT_CAP,)
            return (0,)

        with (
            caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"),
            patch.object(storage_refs, "is_storage_enabled", return_value=False),
        ):
            outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_handler)
            )
        ws_out = next(o for o in outcomes if o.name == "workspace_files")
        att_out = next(o for o in outcomes if o.name == "chat_attachments")
        assert ws_out.has_references is True and ws_out.hit_count == 7 and not ws_out.hit_count_capped
        assert att_out.has_references is True and att_out.hit_count_capped is True
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "public.workspace_files (workspace_files): 7" in m
            and f"public.chat_attachments (chat_attachments): {storage_refs.STARTUP_COUNT_CAP}+" in m
            and "check --strict" in m
            for m in warn_msgs
        )

    @pytest.mark.asyncio
    async def test_missing_table_skipped_and_query_timeout_allows_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _missing_handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (None,) if params[0] == "public.drive_contents" else (params[0],)
            return (0,)

        with (
            caplog.at_level(logging.INFO, logger="src.dalton.storage_refs"),
            patch.object(storage_refs, "is_storage_enabled", return_value=False),
        ):
            outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_missing_handler)
            )
        drive_out = next(o for o in outcomes if o.name == "drive_contents")
        assert drive_out.skipped_missing_table is True
        assert drive_out.error is None

        # Probe timeout/error logs WARNING and allows startup
        caplog.clear()

        def _timeout_handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM user_skills" in sql:
                raise TimeoutError("canceling statement due to statement timeout")
            return (0,)

        with (
            caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"),
            patch.object(storage_refs, "is_storage_enabled", return_value=False),
        ):
            timeout_outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_timeout_handler)
            )
        skill_out = next(o for o in timeout_outcomes if o.name == "user_skills")
        assert skill_out.error is not None
        assert "statement timeout" in skill_out.error
        assert skill_out.has_references is False
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("user_skills" in m for m in warn_msgs)
        assert any("allowing startup" in m for m in warn_msgs)

    @pytest.mark.asyncio
    async def test_run_auth_db_startup_hook_calls_storage_checks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_validate_provider = create_autospec(
            storage_config.validate_core_storage_provider, return_value=None
        )
        mock_warn = create_autospec(
            storage_refs.warn_main_bucket_references_on_startup, return_value=[]
        )
        mock_validate_auth = create_autospec(
            auth_bootstrap.validate_dalton_auth_config, return_value=None
        )
        with (
            patch.object(storage_config, "validate_core_storage_provider", mock_validate_provider),
            patch.object(storage_refs, "warn_main_bucket_references_on_startup", mock_warn),
            patch.object(auth_bootstrap, "validate_dalton_auth_config", mock_validate_auth),
            patch.object(auth_bootstrap, "is_dalton_local_auth_enabled", return_value=False),
            patch.object(auth_bootstrap, "get_dalton_auth_mode", return_value="oss"),
        ):
            res = await auth_bootstrap.run_auth_db_startup_hook("oss")

        assert res is True
        mock_validate_provider.assert_called_once()
        mock_warn.assert_awaited_once()
        mock_validate_auth.assert_called_once()

        # Even if the startup storage check unexpectedly raises, bootstrap MUST NOT refuse startup.
        failing_warn = create_autospec(
            storage_refs.warn_main_bucket_references_on_startup,
            side_effect=RuntimeError("db pool unavailable"),
        )
        with (
            caplog.at_level(logging.WARNING),
            patch.object(storage_config, "validate_core_storage_provider", mock_validate_provider),
            patch.object(storage_refs, "warn_main_bucket_references_on_startup", failing_warn),
            patch.object(auth_bootstrap, "validate_dalton_auth_config", mock_validate_auth),
            patch.object(auth_bootstrap, "is_dalton_local_auth_enabled", return_value=False),
            patch.object(auth_bootstrap, "get_dalton_auth_mode", return_value="oss"),
        ):
            res_after_err = await auth_bootstrap.run_auth_db_startup_hook("oss")
        assert res_after_err is True
        assert any(
            r.levelno == logging.WARNING and "continuing startup" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 5. Download ticket tests
# ---------------------------------------------------------------------------


class TestDownloadTicket:
    def test_issue_verify_roundtrip_skew_and_uniform_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JWT_SIGNING_SECRET", "s" * 40)
        now = 1_700_000_000
        tok = download_ticket.issue_download_ticket(
            user_id="u-1",
            workspace_id="ws-1",
            path="reports/big.parquet",
            backup_ok=True,
            now=now,
        )
        assert tok is not None

        # Reusable within 60s and within 5s clock skew boundary
        claims1 = download_ticket.verify_download_ticket(tok, now=now + 30)
        claims2 = download_ticket.verify_download_ticket(tok, now=now + 65)
        assert claims1 == claims2
        assert claims1.user_id == "u-1"
        assert claims1.workspace_id == "ws-1"
        assert claims1.path == "reports/big.parquet"
        assert claims1.backup_ok is True

        # Expired past 5s skew
        bad_cases: list[tuple[str, int]] = [(tok, now + 66)]

        # Tampered payload
        p_b64, s_b64 = tok.split(".")
        raw_payload = json.loads(download_ticket._b64url_decode(p_b64).decode())
        tampered_payload = dict(raw_payload, uid="u-2")
        bad_payload_b64 = download_ticket._b64url_encode(
            json.dumps(tampered_payload, separators=(",", ":"), sort_keys=True).encode()
        )
        bad_cases.append((f"{bad_payload_b64}.{s_b64}", now + 1))

        # Tampered signature
        bad_sig = "A" * len(s_b64)
        bad_cases.append((f"{p_b64}.{bad_sig}", now + 1))

        # Properly signed wrong typ & missing field
        key = download_ticket._derive_ticket_hmac_key()
        assert key is not None
        for mutated in (
            {**raw_payload, "typ": "ws_ticket"},
            {k: v for k, v in raw_payload.items() if k != "nonce"},
        ):
            pb = download_ticket._b64url_encode(
                json.dumps(mutated, separators=(",", ":"), sort_keys=True).encode()
            )
            sb = download_ticket._b64url_encode(
                download_ticket.hmac.new(key, pb.encode("ascii"), hashlib.sha256).digest()
            )
            bad_cases.append((f"{pb}.{sb}", now + 1))

        details: set[str] = set()
        for bad_tok, ts in bad_cases:
            with pytest.raises(HTTPException) as exc:
                download_ticket.verify_download_ticket(bad_tok, now=ts)
            assert exc.value.status_code == 401
            details.add(str(exc.value.detail))
        assert details == {download_ticket.INVALID_DOWNLOAD_TICKET_DETAIL}

    def test_no_secret_seeds_issues_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JWT_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
        monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
        assert (
            download_ticket.issue_download_ticket(
                user_id="u-1", workspace_id="ws-1", path="a.bin", backup_ok=True
            )
            is None
        )

    def test_redemption_route_delegates_and_rejects_company_and_suspended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-seed-12345")
        monkeypatch.delenv("JWT_SIGNING_SECRET", raising=False)
        app = FastAPI()
        app.include_router(workspace_file_download.router)
        client = TestClient(app)

        mock_dl = create_autospec(
            ws_crud.download_workspace_file,
            return_value=Response(content=b"stream-ok", media_type="application/octet-stream"),
        )
        mock_admit = create_autospec(workspace_file_download.admit_caller, return_value=None)

        tok = download_ticket.issue_download_ticket(
            user_id="user-42",
            workspace_id="ws-99",
            path="dir/big.bin",
            backup_ok=True,
        )
        assert tok is not None

        with (
            patch.object(ws_crud, "download_workspace_file", mock_dl),
            patch.object(workspace_file_download, "admit_caller", mock_admit),
        ):
            resp = client.get("/api/v1/dalton/workspace-files/download", params={"dl_token": tok})

        assert resp.status_code == 200
        assert resp.content == b"stream-ok"
        mock_dl.assert_awaited_once()
        kw = mock_dl.await_args.kwargs
        assert kw["workspace_id"] == "ws-99"
        assert kw["x_user_id"] == "user-42"
        assert kw["path"] == "dir/big.bin"
        assert kw["attachment"] is True
        assert kw["backup_ok"] is True

        # Company system identity rejected
        sys_tok = download_ticket.issue_download_ticket(
            user_id=COMPANY_SYSTEM_USER_ID,
            workspace_id="ws-99",
            path="dir/big.bin",
            backup_ok=False,
        )
        assert sys_tok is not None
        mock_dl.reset_mock()
        with patch.object(ws_crud, "download_workspace_file", mock_dl):
            resp_sys = client.get(
                "/api/v1/dalton/workspace-files/download", params={"dl_token": sys_tok}
            )
        assert resp_sys.status_code in (401, 403)
        mock_dl.assert_not_awaited()

        # Suspended account rejected by admit_caller (403)
        suspended_admit = create_autospec(
            workspace_file_download.admit_caller,
            side_effect=HTTPException(status_code=403, detail="Account suspended"),
        )
        with (
            patch.object(ws_crud, "download_workspace_file", mock_dl),
            patch.object(workspace_file_download, "admit_caller", suspended_admit),
        ):
            resp_susp = client.get(
                "/api/v1/dalton/workspace-files/download", params={"dl_token": tok}
            )
        assert resp_susp.status_code == 403
        mock_dl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_download_link_sign_skips_when_browser_endpoint_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_signed = create_autospec(
            download_link.get_signed_url, return_value="https://browser.example.com/signed"
        )
        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "")
        with patch.object(download_link, "get_signed_url", mock_signed):
            res_empty = await download_link._sign("u-1", "a" * 64, "big.bin")
        assert res_empty is None
        mock_signed.assert_not_called()

        monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "https://browser.example.com")
        with patch.object(download_link, "get_signed_url", mock_signed):
            res_set = await download_link._sign("u-1", "a" * 64, "big.bin")
        assert res_set == "https://browser.example.com/signed"
        mock_signed.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_url_ticket_issuance_boundaries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JWT_SIGNING_SECRET", "k" * 40)

        # Backup: >8 MiB -> dl_token issued; <=8 MiB or missing locator -> omitted
        big_locator = {"file_size": (8 << 20) + 1}
        small_locator = {"file_size": 8 << 20}
        with patch.object(
            download_ticket,
            "get_file_locator",
            create_autospec(wf_db.get_file_locator, return_value=big_locator),
        ):
            out_big = await download_ticket.maybe_attach_backup_download_ticket(
                {"url": None},
                user_id="u-1",
                workspace_id="ws-1",
                path="big.bin",
                normalized_path="big.bin",
                backup_ok=True,
            )
        assert out_big.get("dl_token")

        with patch.object(
            download_ticket,
            "get_file_locator",
            create_autospec(wf_db.get_file_locator, return_value=small_locator),
        ):
            out_small = await download_ticket.maybe_attach_backup_download_ticket(
                {"url": None},
                user_id="u-1",
                workspace_id="ws-1",
                path="small.bin",
                normalized_path="small.bin",
                backup_ok=True,
            )
        assert "dl_token" not in out_small

        with patch.object(
            download_ticket,
            "get_file_locator",
            create_autospec(wf_db.get_file_locator, return_value=None),
        ):
            out_missing = await download_ticket.maybe_attach_backup_download_ticket(
                {"url": None},
                user_id="u-1",
                workspace_id="ws-1",
                path="missing.bin",
                normalized_path="missing.bin",
                backup_ok=True,
            )
        assert "dl_token" not in out_missing

        # Live: >8 MiB -> dl_token issued; error reading size -> omitted
        big_slice = containment.ContainedSlice(
            canonical="/workspace/ws/big.bin",
            size=(8 << 20) + 100,
            version=None,
            head=b"\x00",
            content=b"\x00",
        )
        with patch.object(
            download_ticket,
            "read_contained_sandbox_range",
            create_autospec(containment.read_contained_sandbox_range, return_value=big_slice),
        ):
            out_live = await download_ticket.maybe_attach_live_download_ticket(
                {"url": None},
                user_id="u-1",
                workspace_id="ws-1",
                path="big.bin",
                backup_ok=False,
                sandbox=object(),
                canonical="/workspace/ws/big.bin",
                work_dir="/workspace/ws",
            )
        assert out_live.get("dl_token")

        with patch.object(
            download_ticket,
            "read_contained_sandbox_range",
            create_autospec(
                containment.read_contained_sandbox_range,
                side_effect=RuntimeError("stat failed"),
            ),
        ):
            out_err = await download_ticket.maybe_attach_live_download_ticket(
                {"url": None},
                user_id="u-1",
                workspace_id="ws-1",
                path="big.bin",
                backup_ok=False,
                sandbox=object(),
                canonical="/workspace/ws/big.bin",
                work_dir="/workspace/ws",
            )
        assert "dl_token" not in out_err

        # Route-level non-owner and virtual file return {"url": None} with no dl_token
        ws_other = {"workspace_id": "ws-1", "user_id": "owner-1", "status": "stopped"}
        with patch.object(
            ws_crud,
            "reached_workspace_on_computer",
            create_autospec(ws_crud.reached_workspace_on_computer, return_value=(ws_other, None)),
        ):
            res_non_owner = await ws_crud.workspace_file_download_url(
                workspace_id="ws-1", x_user_id="colleague-2", path="big.bin"
            )
        assert res_non_owner == {"url": None}

        ws_own = {"workspace_id": "ws-1", "user_id": "owner-1", "status": "stopped"}
        with (
            patch.object(
                ws_crud,
                "reached_workspace_on_computer",
                create_autospec(ws_crud.reached_workspace_on_computer, return_value=(ws_own, None)),
            ),
            patch(
                "src.server.app.workspace_files._shared.owner_work_dir",
                return_value="/workspace/ws-1",
            ),
            patch.object(ws_crud, "_virtual_file", return_value="virtual-kind"),
        ):
            res_virtual = await ws_crud.workspace_file_download_url(
                workspace_id="ws-1", x_user_id="owner-1", path=".agents/virtual.json"
            )
        assert res_virtual == {"url": None}


# ---------------------------------------------------------------------------
# 6. MinIO bucket rules tests
# ---------------------------------------------------------------------------


class TestStorageBucketRules:
    def test_main_and_secondary_rules_merge_versioning_and_policy(self) -> None:
        unmanaged_rule = {
            "ID": "custom-operator-rule",
            "Status": "Enabled",
            "Filter": {"Prefix": "tmp/"},
            "Expiration": {"Days": 7},
        }
        stale_uploads_rule = {
            "ID": storage_bucket_rules.RULE_ID_UPLOADS_EXPIRE,
            "Status": "Enabled",
            "Filter": {"Prefix": "uploads/"},
            "Expiration": {"Days": 2},
        }
        client = _FakeS3Client(
            buckets={"dalton-main", "dalton-asr"},
            versioning={"dalton-main": "Suspended", "dalton-asr": "Suspended"},
            lifecycle={
                "dalton-main": [unmanaged_rule],
                "dalton-asr": [unmanaged_rule, stale_uploads_rule],
            },
            policy={
                "dalton-asr": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::dalton-asr/*"}],
                    }
                )
            },
        )

        res_main = storage_bucket_rules.apply_bucket_rules(
            client, bucket="dalton-main", role="main", key_prefix="", uploads_expire_days=2
        )
        assert res_main.versioning_status == "Enabled"
        assert client.versioning["dalton-main"] == "Enabled"
        assert res_main.managed_rule_ids == (
            "dalton-uploads-expire",
            "dalton-noncurrent-expire",
            "dalton-expired-delete-marker",
        )
        assert len(res_main.preserved_rules) == 1
        assert res_main.preserved_rules[0]["ID"] == "custom-operator-rule"
        main_policy = json.loads(client.policy["dalton-main"])
        assert len(main_policy["Statement"]) == 1
        assert main_policy["Statement"][0]["Action"] == ["s3:GetObject"]
        assert main_policy["Statement"][0]["Resource"] == ["arn:aws:s3:::dalton-main/avatars/*"]

        res_sec = storage_bucket_rules.apply_bucket_rules(
            client, bucket="dalton-asr", role="secondary", key_prefix=""
        )
        assert res_sec.versioning_status == "Enabled"
        assert client.versioning["dalton-asr"] == "Enabled"
        assert res_sec.managed_rule_ids == (
            "dalton-noncurrent-expire",
            "dalton-expired-delete-marker",
        )
        assert res_sec.removed_secondary_uploads_rule is True
        sec_ids = [r["ID"] for r in client.lifecycle["dalton-asr"]]
        assert "custom-operator-rule" in sec_ids
        assert "dalton-uploads-expire" not in sec_ids
        assert "dalton-asr" not in client.policy

    def test_key_prefix_applied_to_lifecycle_and_policy(self) -> None:
        client = _FakeS3Client(buckets={"shared-bucket"})
        res = storage_bucket_rules.apply_bucket_rules(
            client,
            bucket="shared-bucket",
            role="main",
            key_prefix="staging",
            uploads_expire_days=1,
        )
        assert res.key_prefix == "staging/"
        rules_by_id = {r["ID"]: r for r in client.lifecycle["shared-bucket"]}
        assert rules_by_id["dalton-uploads-expire"]["Filter"]["Prefix"] == "staging/uploads/"
        assert rules_by_id["dalton-uploads-expire"]["Expiration"]["Days"] == 1
        assert rules_by_id["dalton-noncurrent-expire"]["Filter"]["Prefix"] == "staging/"
        assert rules_by_id["dalton-expired-delete-marker"]["Filter"]["Prefix"] == "staging/"
        pol = json.loads(client.policy["shared-bucket"])
        assert pol["Statement"][0]["Resource"] == ["arn:aws:s3:::shared-bucket/staging/avatars/*"]

    @pytest.mark.parametrize("corrupt", ["versioning", "lifecycle", "policy"])
    def test_readback_mismatch_raises(self, corrupt: str) -> None:
        client = _FakeS3Client(buckets={"dalton-main"}, corrupt_readback=corrupt)
        with pytest.raises(storage_bucket_rules.BucketRuleVerificationError):
            storage_bucket_rules.apply_bucket_rules(
                client, bucket="dalton-main", role="main"
            )

    def test_policy_verification_rejects_get_object_version(self) -> None:
        bad_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": ["arn:aws:s3:::dalton-main/avatars/*"],
                }
            ],
        }
        with pytest.raises(storage_bucket_rules.BucketRuleVerificationError):
            storage_bucket_rules.verify_bucket_policy(
                bad_policy, bucket="dalton-main", role="main"
            )


# ---------------------------------------------------------------------------
# 7. Exclusion globs tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeDirEntry:
    name: str
    entry_kind: str  # "file" | "dir" | "symlink"

    def is_symlink(self) -> bool:
        return self.entry_kind == "symlink"

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self.entry_kind == "dir"

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self.entry_kind == "file"


class TestExclusionGlobs:
    EXPECTED_BASE_KEYS = {
        "exclude_dir_names",
        "exclude_rel_dirs",
        "exclude_rel_dir_prefixes",
        "exclude_rel_files",
        "exclude_basenames",
        "exclude_root_basenames",
        "exclude_root_basename_prefixes",
        "exclude_suffixes",
        "whole_dir_names",
        "max_whole_dir_bytes",
        "max_file_bytes",
    }

    def test_empty_config_preserves_exact_exclusion_spec_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WSFILES_EXCLUDE_REL_DIRS", raising=False)
        monkeypatch.delenv("DALTON_WSFILES_EXCLUDE_REL_GLOBS", raising=False)
        spec = transfer.exclusion_spec(100 << 20)
        assert set(spec.keys()) == self.EXPECTED_BASE_KEYS
        assert "exclude_rel_globs" not in spec

    def test_env_overrides_yaml_and_appends_to_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.object(
            storage_config,
            "_load_dalton_workspace_files_raw",
            return_value={
                "exclude_dir_names": ["custom_cache"],
                "exclude_rel_dirs": ["yaml/dir"],
                "exclude_rel_globs": ["yaml/*/cache"],
            },
        ):
            monkeypatch.setenv("DALTON_WSFILES_EXCLUDE_REL_DIRS", " data/prices_long , , ")
            monkeypatch.setenv("DALTON_WSFILES_EXCLUDE_REL_GLOBS", "cn_screening_*/data, *.tmp_glob")
            spec = transfer.exclusion_spec(100 << 20)

        assert "custom_cache" in spec["exclude_dir_names"]
        assert "node_modules" in spec["exclude_dir_names"]
        assert "data/prices_long" in spec["exclude_rel_dirs"]
        assert "yaml/dir" not in spec["exclude_rel_dirs"]
        assert spec["exclude_rel_globs"] == ["cn_screening_*/data", "*.tmp_glob"]

    def test_runtime_exclusions_glob_and_hardcoded_protections(self) -> None:
        ex = transfer_rt._Exclusions(
            {
                "whole_dir_names": [".git"],
                "exclude_rel_globs": [
                    "cn_screening_*/data",
                    "data/*.cache",
                    "*",
                    "work/*",
                    "work/uploads*",
                    ".git*",
                ],
            }
        )
        # Matching dir and file are excluded
        assert ex.kind(_FakeDirEntry("data", "dir"), "cn_screening_a/data", False) is None
        assert ex.kind(_FakeDirEntry("foo.cache", "file"), "data/foo.cache", False) is None
        assert ex.kind(_FakeDirEntry("other.txt", "file"), "work/other.txt", False) is None

        # Hardcoded protections: .git (root and nested), work dir, work/uploads, and children survive
        assert ex.kind(_FakeDirEntry(".git", "dir"), ".git", True) == "dir"
        assert ex.kind(_FakeDirEntry("HEAD", "file"), ".git/HEAD", False) == "file"
        assert ex.kind(_FakeDirEntry(".git", "dir"), "sub/repo/.git", False) == "dir"
        assert ex.kind(_FakeDirEntry("work", "dir"), "work", True) == "dir"
        assert ex.kind(_FakeDirEntry("uploads", "dir"), "work/uploads", False) == "dir"
        assert ex.kind(_FakeDirEntry("doc.pdf", "file"), "work/uploads/doc.pdf", False) == "file"

    def test_dalton_config_validates_yaml_with_workspace_files(self) -> None:
        _load_profile_dalton_raw.cache_clear()
        raw_everflow = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8"))
        raw_core = yaml.safe_load(DALTON_CORE_YAML_PATH.read_text(encoding="utf-8"))
        assert "workspace_files" in raw_everflow
        assert "workspace_files" in raw_core
        assert raw_everflow["workspace_files"] == {
            "write_inline_only": False,
            "exclude_dir_names": [],
            "exclude_rel_dirs": [],
            "exclude_rel_globs": [],
        }
        assert raw_core["workspace_files"] == {
            "write_inline_only": False,
            "exclude_dir_names": [],
            "exclude_rel_dirs": [],
            "exclude_rel_globs": [],
        }
        # DaltonConfig must accept both YAML files without raising validation errors
        cfg_e = DaltonConfig.model_validate(raw_everflow)
        cfg_c = DaltonConfig.model_validate(raw_core)
        assert cfg_e.enabled is True
        assert cfg_c.enabled is True


# ---------------------------------------------------------------------------
# 8. Step 2 Round 2 regression tests (Fixes 1-8)
# ---------------------------------------------------------------------------


class _FakeCliConn:
    """Explicit async connection double for CLI probe tests."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        yield

    def cursor(self) -> _FakeProbeCursor:
        def _wrapped(sql: str, params: Any = None) -> Any:
            self.executed.append((sql, params))
            return self._handler(sql, params)

        return _FakeProbeCursor(_wrapped)

    async def close(self) -> None:
        self.closed = True


class TestStep2Round2Fixes:
    def test_fix1_and_fix7_no_recent_window_probes_and_all_17_strict_consumers_registered(
        self,
    ) -> None:
        strict_names = [c.name for c in storage_refs.STRICT_MAIN_BUCKET_CONSUMERS]
        assert len(strict_names) == 17
        assert "widgets_checkpoint_writes" in strict_names
        assert "widgets_checkpoint_blobs" in strict_names
        assert "widgets_turn_slices" in strict_names
        assert "users_avatars" in strict_names
        for probe in storage_refs.STARTUP_MAIN_BUCKET_PROBES:
            assert "ORDER BY created_at DESC LIMIT" not in probe.count_sql
            assert f"LIMIT {storage_refs.STARTUP_COUNT_CAP}" in probe.count_sql
        for spec in storage_refs.STRICT_MAIN_BUCKET_CONSUMERS:
            assert "ORDER BY created_at DESC LIMIT" not in spec.exists_sql
            assert "ORDER BY created_at DESC LIMIT" not in spec.batch_sql_first
            assert "recent_threads" not in spec.exists_sql

    def test_fix2_external_avatar_allowed_site_origin_prefix_key_matched(self) -> None:
        # No site public URL base configured -> external avatar URL never matches
        assert (
            storage_refs.is_site_avatar_url(
                "https://lh3.googleusercontent.com/avatars/user-1.png",
                env={},
            )
            is False
        )
        assert storage_refs.site_avatar_url_rules(env={}) == []

        # Configured explicit STORAGE_PUBLIC_URL_BASE + STORAGE_KEY_PREFIX
        env_prefixed = {
            "STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage",
            "STORAGE_KEY_PREFIX": "staging",
        }
        # External origins are allowed
        assert (
            storage_refs.is_site_avatar_url(
                "https://lh3.googleusercontent.com/storage/staging/avatars/u1.png",
                env=env_prefixed,
            )
            is False
        )
        assert (
            storage_refs.is_site_avatar_url(
                "https://cdn.other.org/avatars/u1.jpg",
                env=env_prefixed,
            )
            is False
        )
        # Same origin but missing/wrong key prefix is allowed
        assert (
            storage_refs.is_site_avatar_url(
                "https://app.example.com/storage/avatars/u1.png",
                env=env_prefixed,
            )
            is False
        )
        assert (
            storage_refs.is_site_avatar_url(
                "https://app.example.com/storage/prod/avatars/u1.png",
                env=env_prefixed,
            )
            is False
        )
        # Same origin + prefix but wrong object key shape is allowed
        assert (
            storage_refs.is_site_avatar_url(
                "https://app.example.com/storage/staging/images/u1.png",
                env=env_prefixed,
            )
            is False
        )
        assert (
            storage_refs.is_site_avatar_url(
                "https://app.example.com/storage/staging/avatars/nested/u1.png",
                env=env_prefixed,
            )
            is False
        )
        # All three match (origin + configured prefix + avatars/<id>.<ext>) -> True
        assert (
            storage_refs.is_site_avatar_url(
                "https://APP.EXAMPLE.COM/storage/staging/avatars/user-42.webp?v=2",
                env=env_prefixed,
            )
            is True
        )

        # Without prefix, PUBLIC_APP_URL fallback matches /storage/avatars/<id>.<ext>
        env_pub = {"PUBLIC_APP_URL": "https://everflow.example.com"}
        assert (
            storage_refs.is_site_avatar_url(
                "https://everflow.example.com/storage/avatars/user-9.png",
                env=env_pub,
            )
            is True
        )
        assert (
            storage_refs.is_site_avatar_url(
                "https://everflow.example.com/other/avatars/user-9.png",
                env=env_pub,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_fix3_glob_excluding_ancestor_preserves_nested_git_in_sync_and_restore(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_dir = tmp_path / "ws"
        # Nested git repo inside glob-excluded ancestors
        (ws_dir / "projects" / "myrepo" / ".git" / "refs" / "heads").mkdir(parents=True)
        (ws_dir / "projects" / "myrepo" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (ws_dir / "projects" / "myrepo" / ".git" / "refs" / "heads" / "main").write_bytes(b"abc123\n")
        (ws_dir / "projects" / "myrepo" / "cache.bin").write_bytes(b"junk-cache")
        (ws_dir / "projects" / "empty_cache").mkdir(parents=True)
        (ws_dir / "projects" / "empty_cache" / "temp.dat").write_bytes(b"temp")
        (ws_dir / "cn_screening_a" / "data" / "sub" / ".git").mkdir(parents=True)
        (ws_dir / "cn_screening_a" / "data" / "sub" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/dev\n")
        (ws_dir / "cn_screening_a" / "data" / "big.parquet").write_bytes(b"parquet-junk")

        scan_out = transfer_rt.scan(
            {
                "root": str(ws_dir),
                "whole_dir_names": [".git"],
                "exclude_rel_globs": ["projects/*", "cn_screening_*/data"],
            }
        )
        scanned_paths = {e["path"] for e in scan_out["entries"]}
        # Protected nested .git histories survive scan
        assert "projects/myrepo/.git/HEAD" in scanned_paths
        assert "projects/myrepo/.git/refs/heads/main" in scanned_paths
        assert "cn_screening_a/data/sub/.git/HEAD" in scanned_paths
        # Excluded non-git files and dirs without .git are omitted
        assert "projects/myrepo/cache.bin" not in scanned_paths
        assert "projects/empty_cache" not in scanned_paths
        assert "projects/empty_cache/temp.dat" not in scanned_paths
        assert "cn_screening_a/data/big.parquet" not in scanned_paths

        # Run full sync_to_db -> verify pruning keeps .git rows and removes excluded cache.bin
        sandbox_files = {
            e["path"]: (ws_dir / e["path"]).read_bytes()
            for e in scan_out["entries"]
            if e["kind"] == "file"
        }
        scan_entries = [
            ScanEntry(
                path=e["path"],
                kind=e["kind"],
                size=e["size"],
                mtime_ns=e["mtime_ns"],
                mode=e["mode"],
                sha256=e["sha256"],
                symlink_target=e["symlink_target"],
                is_binary=e.get("is_binary"),
            )
            for e in scan_out["entries"]
        ]
        scan_result = ScanResult(
            entries=scan_entries,
            oversized=[],
            errors=[],
            hashed=len(scan_entries),
            reused=0,
        )
        existing = {
            "projects/myrepo/.git/HEAD": {
                "kind": "file",
                "file_size": len(b"ref: refs/heads/main\n"),
                "content_hash": _sha(b"ref: refs/heads/main\n"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
            "projects/myrepo/cache.bin": {
                "kind": "file",
                "file_size": len(b"junk-cache"),
                "content_hash": _sha(b"junk-cache"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
        }
        layout = SandboxLayout.for_root("/workspace").for_workspace("ws-git")
        sync_sandbox = _FakeSandbox(sandbox_files, working_dir="/workspace", dir_name="ws-git")
        manifest_rows: dict[str, dict[str, Any]] = {}
        pruned_kept_sets: list[set[str]] = []

        async def _fake_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            for r in rows:
                manifest_rows[r["file_path"]] = dict(r)
            return len(rows)

        async def _fake_delete_removed(
            _ws: str, active: set[str], **_kw: Any
        ) -> int:
            pruned_kept_sets.append(set(active))
            return sum(1 for p in existing if p not in active)

        @asynccontextmanager
        async def _noop_lock(*_a: Any, **_kw: Any):
            yield NoDbConn()

        clock = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
        with (
            patch.object(backup, "workspace_sync_lock", _noop_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=clock)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value=existing)),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", create_autospec(backup.delete_removed_files, side_effect=_fake_delete_removed)),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=False),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            sync_res = await backup.sync_to_db("ws-git", sync_sandbox, layout=layout)

        assert sync_res.unsaved == []
        assert pruned_kept_sets
        assert "projects/myrepo/.git/HEAD" in pruned_kept_sets[0]
        assert "cn_screening_a/data/sub/.git/HEAD" in pruned_kept_sets[0]
        assert "projects/myrepo/cache.bin" not in pruned_kept_sets[0]
        assert sync_res.deleted == 1

        # Cold restore brings back nested .git history intact
        restore_sandbox = _FakeSandbox({}, working_dir="/workspace", dir_name="ws-git")
        # Ensure already-existing unchanged HEAD row also has content for restore
        manifest_rows.setdefault(
            "projects/myrepo/.git/HEAD",
            {
                "file_path": "projects/myrepo/.git/HEAD",
                "kind": "file",
                "file_size": len(b"ref: refs/heads/main\n"),
                "content_hash": _sha(b"ref: refs/heads/main\n"),
                "content_text": "ref: refs/heads/main\n",
                "content_binary": None,
                "is_binary": False,
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "permissions": "0644",
                "sandbox_modified_at": clock,
            },
        )

        async def _fake_pull_direct(sb: _FakeSandbox, items: list[dict[str, Any]], **_kw: Any) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for item in items:
                staged_abs = f"{layout.workspace}/{item['file']}"
                sb.restored_files[item["path"]] = sb.uploaded_staged[staged_abs]
                out[item["path"]] = {"status": "ok"}
            return out

        async def _fake_get_files(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [dict(v) for v in manifest_rows.values() if v.get("kind", "file") == "file"]

        with (
            patch("src.utils.storage.BLOB_TRANSFER_MODE", "relay"),
            patch("src.utils.storage.STORAGE_PROVIDER", "none"),
            patch.object(restore, "workspace_sync_lock", _noop_lock),
            patch.object(restore, "hold_workspace_skill_sync", _noop_lock),
            patch.object(restore, "set_files_restore_incomplete", create_autospec(restore.set_files_restore_incomplete, return_value=True)),
            patch.object(restore, "workspace_owner", create_autospec(restore.workspace_owner, return_value="u-git")),
            patch.object(restore, "get_files_for_workspace", create_autospec(wf_db.get_files_for_workspace, side_effect=_fake_get_files)),
            patch.object(restore, "pull_direct", create_autospec(restore.pull_direct, side_effect=_fake_pull_direct)),
        ):
            restored = await restore.restore_to_sandbox("ws-git", restore_sandbox, layout=layout)

        assert restored["errors"] == 0
        assert restore_sandbox.restored_files["projects/myrepo/.git/HEAD"] == b"ref: refs/heads/main\n"
        assert restore_sandbox.restored_files["projects/myrepo/.git/refs/heads/main"] == b"abc123\n"
        assert restore_sandbox.restored_files["cn_screening_a/data/sub/.git/HEAD"] == b"ref: refs/heads/dev\n"

    @pytest.mark.asyncio
    async def test_fix4_inline_bounded_read_rejects_1b_budget_4b_actual_as_changed(
        self,
    ) -> None:
        """Both inline entry points enforce bounded read + scan size/digest check:
        a file scanned as 1 byte (budget=1) that grew to 4 bytes is NOT written and
        stays in result.unsaved with reason='changed'.
        """
        layout = SandboxLayout.for_root("/workspace").for_workspace("ws-cap")
        scanned_entry = _entry("grow.txt", b"a")  # scanned size = 1 byte
        assert scanned_entry.size == 1

        # Sandbox now holds 4 bytes instead of scanned 1 byte
        sandbox = _FakeSandbox({"grow.txt": b"abcd"}, working_dir="/workspace", dir_name="ws-cap")
        upserted: list[dict[str, Any]] = []

        async def _fake_upsert(_ws: str, rows: list[dict[str, Any]], *, conn: Any = None) -> int:
            upserted.extend(rows)
            return len(rows)

        # Entry point 1: fallback_failed_uploads_inline with budget_bytes=1
        res_fb = backup.SyncResult(max_file_bytes=100 << 20)
        res_fb.unsaved.append(UnsavedFile("grow.txt", "failed", 1))
        with patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)):
            out_rows = await wsfiles_write_mode.fallback_failed_uploads_inline(
                blobs_on=True,
                workspace_id="ws-cap",
                sandbox=sandbox,
                scan_entries=[scanned_entry],
                rows=[],
                result=res_fb,
                scanned_unsaved=0,
                layout=layout,
                conn=NoDbConn(),
                budget_bytes=1,
            )
        assert out_rows == []
        assert upserted == []
        assert res_fb.synced == 0
        assert res_fb.unsaved == [UnsavedFile("grow.txt", "changed", 1)]

        # Entry point 2: persist_needs_bytes_for_blobs_on (write_inline_only=True)
        res_io = backup.SyncResult(max_file_bytes=100 << 20)
        mock_blobs = create_autospec(blobs._persist_blobs, return_value=([], []))
        with patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)):
            await wsfiles_write_mode.persist_needs_bytes_for_blobs_on(
                user_id="u-1",
                workspace_id="ws-cap",
                sandbox=sandbox,
                needs_bytes=[scanned_entry],
                write_inline_only=True,
                layout=layout,
                conn=NoDbConn(),
                rows=[],
                result=res_io,
                persist_blobs_fn=mock_blobs,
            )
        assert upserted == []
        assert res_io.synced == 0
        assert res_io.unsaved == [UnsavedFile("grow.txt", "changed", 1)]

        # Same-size (1 byte) but mutated digest is also rejected as 'changed'
        sandbox_mut = _FakeSandbox({"grow.txt": b"z"}, working_dir="/workspace", dir_name="ws-cap")
        res_mut = backup.SyncResult(max_file_bytes=100 << 20)
        with patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, side_effect=_fake_upsert)):
            await wsfiles_write_mode.persist_needs_bytes_for_blobs_on(
                user_id="u-1",
                workspace_id="ws-cap",
                sandbox=sandbox_mut,
                needs_bytes=[scanned_entry],
                write_inline_only=True,
                layout=layout,
                conn=NoDbConn(),
                rows=[],
                result=res_mut,
                persist_blobs_fn=mock_blobs,
            )
        assert upserted == []
        assert res_mut.synced == 0
        assert res_mut.unsaved == [UnsavedFile("grow.txt", "changed", 1)]

    def test_fix5_uvicorn_access_log_redacts_dl_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from uvicorn.logging import AccessFormatter

        monkeypatch.setenv("JWT_SIGNING_SECRET", "z" * 40)
        raw_ticket = download_ticket.issue_download_ticket(
            user_id="u-log",
            workspace_id="ws-log",
            path="reports/big.parquet",
            backup_ok=True,
        )
        assert raw_ticket is not None

        cfg = download_ticket.build_uvicorn_log_config()
        assert "dalton_redact_dl_token" in cfg["filters"]
        assert "dalton_redact_dl_token" in cfg["loggers"]["uvicorn.access"]["filters"]

        captured: list[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        test_logger = logging.getLogger("test.uvicorn.access.dl_token")
        test_logger.handlers.clear()
        test_logger.filters.clear()
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        handler = _CaptureHandler()
        handler.setFormatter(
            AccessFormatter(fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
        )
        test_logger.addHandler(handler)
        download_ticket.install_download_ticket_log_filter(test_logger)

        test_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:54321",
            "GET",
            f"/api/v1/dalton/workspace-files/download?dl_token={raw_ticket}&foo=bar",
            "1.1",
            200,
        )
        assert len(captured) == 1
        line = captured[0]
        assert raw_ticket not in line
        assert "dl_token" not in line
        assert "foo=bar" not in line
        assert "?" not in line
        assert "/api/v1/dalton/workspace-files/download HTTP/1.1" in line

        # Also covered by SensitiveQueryLogFilter attached in configure_logging()
        from src.dalton.auth.dispatch import redact_sensitive_query_params

        redacted = redact_sensitive_query_params(
            f"/api/v1/dalton/workspace-files/download?dl_token={raw_ticket}&foo=bar"
        )
        assert redacted == "/api/v1/dalton/workspace-files/download"

    def test_fix6_storage_refs_cli_opens_and_closes_readonly_connection(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SET SESSION"):
                return None
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            return (0,)

        fake_conn = _FakeCliConn(_handler)
        mock_open = create_autospec(
            storage_refs.open_session_connection, return_value=fake_conn
        )
        with patch.object(storage_refs, "open_session_connection", mock_open):
            rc = storage_refs.main(["probe"])

        assert rc == 0
        mock_open.assert_awaited_once()
        assert fake_conn.closed is True
        assert any("READ ONLY" in sql for sql, _ in fake_conn.executed)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["errors"] == []
        assert parsed["hits"] == []
        assert len(parsed["consumers"]) == len(storage_refs.STARTUP_MAIN_BUCKET_PROBES)

    def test_fix8_ptc_config_file_selected_and_env_vars_expanded(
        self, tmp_path: Any
    ) -> None:
        # Custom YAML pointed to by PTC_CONFIG_FILE with NO storage section:
        # must NOT fall back to repo root agent_config.yaml (which sets provider: s3).
        no_storage_yaml = tmp_path / "custom_agent.yaml"
        no_storage_yaml.write_text("llm:\n  name: test-model\n", encoding="utf-8")

        resolved_off = storage_config.resolve(
            env={
                "PTC_CONFIG_FILE": str(no_storage_yaml),
                "STORAGE_BUCKET_NAME": "dalton-main",
            }
        )
        assert resolved_off.provider == "none"
        assert resolved_off.is_storage_enabled is False

        # Env var expansion (${VAR} and $VAR) in PTC_CONFIG_FILE
        env_yaml = tmp_path / "env_agent.yaml"
        env_yaml.write_text("storage:\n  provider: ${CUSTOM_STORE_PROV}\n", encoding="utf-8")
        resolved_on = storage_config.resolve(
            env={
                "PTC_CONFIG_FILE": str(env_yaml),
                "CUSTOM_STORE_PROV": "s3",
                "STORAGE_BUCKET_NAME": "dalton-main",
            }
        )
        assert resolved_on.provider == "s3"
        assert resolved_on.is_storage_enabled is True

        simple_yaml = tmp_path / "simple_env_agent.yaml"
        simple_yaml.write_text("storage:\n  provider: $CUSTOM_STORE_PROV\n", encoding="utf-8")
        resolved_simple = storage_config.resolve(
            env={
                "PTC_CONFIG_FILE": str(simple_yaml),
                "CUSTOM_STORE_PROV": "s3",
                "STORAGE_BUCKET_NAME": "dalton-main",
            }
        )
        assert resolved_simple.provider == "s3"
        assert resolved_simple.is_storage_enabled is True


# ---------------------------------------------------------------------------
# 9. Step 2 Round 3 regression tests (Lead decisions + Codex High 4 / Med 5)
# ---------------------------------------------------------------------------


class _FakeOtelSpan:
    """Explicit OpenTelemetry span double for ``drop_span_query`` tests."""

    def __init__(self, attributes: dict[str, str], *, recording: bool = True) -> None:
        self.attributes = dict(attributes)
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, key: str, value: str) -> None:
        self.attributes[key] = value


class TestStep2Round3Fixes:
    @pytest.mark.asyncio
    async def test_r3_startup_hits_warn_only_never_block_bootstrap(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Startup main-bucket check only logs WARNING (table + count, no content) and never blocks."""

        def _hit_handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("5s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM workspace_file_blobs" in sql:
                return (3,)
            return (0,)

        with (
            caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"),
            patch.object(storage_refs, "is_storage_enabled", return_value=False),
        ):
            outcomes = await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_hit_handler)
            )

        assert any(o.name == "workspace_file_blobs" and o.hit_count == 3 for o in outcomes)
        warn_lines = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "public.workspace_file_blobs (workspace_file_blobs): 3" in m for m in warn_lines
        )

        # Even inside bootstrap.run_auth_db_startup_hook, hits NEVER raise DaltonStartupConfigError
        async def _warn_with_hits(**_kw: Any) -> list[storage_refs.ConsumerCheckOutcome]:
            return await storage_refs.warn_main_bucket_references_on_startup(
                conn=_FakeProbeConn(_hit_handler)
            )

        with (
            patch.object(storage_refs, "is_storage_enabled", return_value=False),
            patch.object(
                storage_refs,
                "warn_main_bucket_references_on_startup",
                create_autospec(
                    storage_refs.warn_main_bucket_references_on_startup,
                    side_effect=_warn_with_hits,
                ),
            ),
            patch.object(
                storage_config,
                "validate_core_storage_provider",
                create_autospec(storage_config.validate_core_storage_provider, return_value=None),
            ),
            patch.object(
                auth_bootstrap,
                "validate_dalton_auth_config",
                create_autospec(auth_bootstrap.validate_dalton_auth_config, return_value=None),
            ),
            patch.object(auth_bootstrap, "is_dalton_local_auth_enabled", return_value=False),
            patch.object(auth_bootstrap, "get_dalton_auth_mode", return_value="oss"),
        ):
            assert await auth_bootstrap.run_auth_db_startup_hook("oss") is True

    def test_r3_strict_cli_no_false_positive_on_plain_messages_and_hits_real_widgets(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``check --strict`` ignores plain message strings containing data_ref/widgets/ (exit 0),
        reports exit 1 on real ShowWidget data_ref.key (fail-fast), and exit 2 on error/interrupt.
        """
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.checkpoint.serde.types import _DeltaSnapshot

        serde = JsonPlusSerializer()
        # Plain messages mentioning data_ref and widgets/ + inlined ShowWidget (has 'data')
        plain_human = HumanMessage(
            content='Please check {"data_ref":{"key":"widgets/t1/abc.json"}} in show_widget.py'
        )
        plain_ai = AIMessage(content="Look at artifact data_ref widgets/t1/abc.json")
        non_widget_tool = ToolMessage(
            content='{"data_ref":{"key":"widgets/t1/abc.json"}}',
            tool_call_id="c1",
            name="Read",
            artifact={"data_ref": {"key": "widgets/t1/not-a-widget.json"}},
        )
        inlined_widget = ToolMessage(
            content="Widget rendered",
            tool_call_id="c2",
            name="ShowWidget",
            artifact={
                "type": "html_widget",
                "html": "<div/>",
                "data": {"a.json": "1"},
                "data_ref": {"key": "widgets/t1/ignored-because-inlined.json"},
            },
        )
        plain_type, plain_blob = serde.dumps_typed(
            _DeltaSnapshot([plain_human, plain_ai, non_widget_tool, inlined_widget])
        )
        plain_slice_lines = "\n".join(
            [
                "message_chunk\t\t"
                + json.dumps({"content": '{"data_ref":{"key":"widgets/t1/plain.json"}}'}),
                "artifact\t\t"
                + json.dumps(
                    {
                        "artifact_type": "file_operation",
                        "payload": {"data_ref": {"key": "widgets/t1/not-widget.json"}},
                    }
                ),
                "artifact\t\t"
                + json.dumps(
                    {
                        "artifact_type": "html_widget",
                        "payload": {
                            "data": {"x": "1"},
                            "data_ref": {"key": "widgets/t1/already-inlined.json"},
                        },
                    }
                ),
            ]
        )

        # Real widget ToolMessage with data_ref.key (no inlined 'data')
        real_widget = ToolMessage(
            content="Widget rendered: rev",
            tool_call_id="c3",
            name="ShowWidget",
            artifact={
                "type": "html_widget",
                "html": "<div/>",
                "data_ref": {"key": "widgets/t9/real.json", "sha256": "deadbeef", "size": 2048},
            },
        )
        real_type, real_blob = serde.dumps_typed([real_widget])
        real_slice_line = "artifact\tw\t" + json.dumps(
            {
                "artifact_type": "html_widget",
                "payload": {
                    "type": "html_widget",
                    "data_ref": {"key": "widgets/t9/slice.json", "sha256": "abcd", "size": 1024},
                },
            }
        )

        def _make_strict_handler(
            *,
            slice_rows: list[tuple[Any, ...]] | None = None,
            blob_rows: list[tuple[Any, ...]] | None = None,
            write_rows: list[tuple[Any, ...]] | None = None,
            raise_on_table: str | None = None,
            interrupt_on_table: str | None = None,
        ) -> Any:
            s_rows = list(slice_rows or [])
            b_rows = list(blob_rows or [])
            w_rows = list(write_rows or [])

            def _handler(sql: str, params: Any = None) -> Any:
                if sql.startswith("SET SESSION"):
                    return None
                if sql.startswith("SELECT set_config"):
                    return ("30s",)
                if sql.startswith("SELECT to_regclass"):
                    return (params[0],)
                if "FROM pg_attribute" in sql:
                    return None
                if sql.startswith("SELECT EXISTS"):
                    return (False,)
                if raise_on_table and f"FROM {raise_on_table}" in sql:
                    raise RuntimeError(f"boom on {raise_on_table}")
                if interrupt_on_table and f"FROM {interrupt_on_table}" in sql:
                    raise KeyboardInterrupt()
                if "FROM turn_slices" in sql:
                    limit = int(params[-1])
                    if "WHERE conversation_response_id >" in sql:
                        cursor = params[0]
                        page = [r for r in s_rows if r[0] > cursor][:limit]
                    else:
                        page = s_rows[:limit]
                    return page
                if "FROM checkpoint_blobs" in sql:
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        cursor = tuple(params[:-1])
                        page = [r for r in b_rows if r[:4] > cursor][:limit]
                    else:
                        page = b_rows[:limit]
                    return page
                if "FROM checkpoint_writes" in sql:
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        cursor = tuple(params[:-1])
                        page = [r for r in w_rows if r[:5] > cursor][:limit]
                    else:
                        page = w_rows[:limit]
                    return page
                return []

            return _handler

        # Case 1: Plain messages across multiple batches (batch_size=2) -> exit 0, progress emitted
        conn_clean = _FakeCliConn(
            _make_strict_handler(
                slice_rows=[
                    ("r-01", plain_slice_lines),
                    ("r-02", None),
                    ("r-03", plain_slice_lines),
                ],
                blob_rows=[
                    ("t-01", "", "messages", "v1", plain_type, plain_blob),
                    ("t-02", "", "messages", "v1", None, None),
                    ("t-03", "", "messages", "v1", plain_type, plain_blob),
                ],
                write_rows=[
                    ("t-01", "", "cp1", "task1", 0, plain_type, plain_blob),
                    ("t-02", "", "cp1", "task1", 0, plain_type, plain_blob),
                    ("t-03", "", "cp1", "task1", 0, plain_type, plain_blob),
                ],
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_clean),
        ):
            rc_clean = storage_refs.main(["check", "--strict", "--batch-size", "2"])
        captured_clean = capsys.readouterr()
        assert rc_clean == 0
        assert conn_clean.closed is True
        report_clean = json.loads(captured_clean.out)
        assert report_clean["exit_code"] == 0
        assert report_clean["hits"] == []
        assert report_clean["errors"] == []
        assert "[storage_refs]" in captured_clean.err
        # Verified multi-batch pagination scanned all 3 rows per table
        ts_out = next(c for c in report_clean["consumers"] if c["name"] == "widgets_turn_slices")
        assert ts_out["scanned_rows"] == 3
        cw_out = next(
            c for c in report_clean["consumers"] if c["name"] == "widgets_checkpoint_writes"
        )
        assert cw_out["scanned_rows"] == 3

        # Case 2: Real widget in checkpoint_writes -> exit 1 (fail-fast stops immediately)
        conn_cw_hit = _FakeCliConn(
            _make_strict_handler(
                write_rows=[
                    ("t-01", "", "cp1", "task1", 0, plain_type, plain_blob),
                    ("t-02", "", "cp1", "task1", 0, real_type, real_blob),
                    ("t-03", "", "cp1", "task1", 0, plain_type, plain_blob),
                ],
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_cw_hit),
        ):
            rc_cw = storage_refs.main(["check", "--strict", "--batch-size", "2"])
        report_cw = json.loads(capsys.readouterr().out)
        assert rc_cw == 1
        assert conn_cw_hit.closed is True
        assert report_cw["hits"] == ["widgets_checkpoint_writes"]
        assert report_cw["stopped_early"] is True
        cw_hit_out = next(
            c for c in report_cw["consumers"] if c["name"] == "widgets_checkpoint_writes"
        )
        assert cw_hit_out["scanned_rows"] == 2  # stopped immediately on 2nd row

        # Case 3: Real widget in turn_slices -> exit 1 (fail-fast stops before checkpoint tables)
        conn_ts_hit = _FakeCliConn(
            _make_strict_handler(slice_rows=[("r-01", real_slice_line)])
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_ts_hit),
        ):
            rc_ts = storage_refs.main(["check", "--strict"])
        report_ts = json.loads(capsys.readouterr().out)
        assert rc_ts == 1
        assert report_ts["hits"] == ["widgets_turn_slices"]
        assert report_ts["stopped_early"] is True

        # Case 4: Real widget in checkpoint_blobs -> exit 1
        conn_cb_hit = _FakeCliConn(
            _make_strict_handler(
                blob_rows=[("t-09", "", "messages", "v9", real_type, real_blob)]
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_cb_hit),
        ):
            rc_cb = storage_refs.main(["check", "--strict"])
        report_cb = json.loads(capsys.readouterr().out)
        assert rc_cb == 1
        assert report_cb["hits"] == ["widgets_checkpoint_blobs"]

        # Case 5: Query error -> exit 2, connection closed
        conn_err = _FakeCliConn(_make_strict_handler(raise_on_table="provenance_result_bodies"))
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_err),
        ):
            rc_err = storage_refs.main(["check", "--strict"])
        report_err = json.loads(capsys.readouterr().out)
        assert rc_err == 2
        assert conn_err.closed is True
        assert "provenance_result_bodies" in report_err["errors"]

        # Case 6: KeyboardInterrupt -> exit 2, connection closed
        conn_int = _FakeCliConn(_make_strict_handler(interrupt_on_table="turn_slices"))
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_int),
        ):
            rc_int = storage_refs.main(["check", "--strict"])
        capsys.readouterr()
        assert rc_int == 2
        assert conn_int.closed is True

    @pytest.mark.asyncio
    async def test_r3_strict_cli_avatar_url_shape_warns_only_exit_0_recorded_key_exits_1(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Site-shaped ``users.avatar_url`` without a recorded object key logs WARNING only (exit 0);
        only an upload-recorded object key column counts as a confirmed hit (exit 1).
        """
        site_env = {
            "STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage",
            "STORAGE_KEY_PREFIX": "prod",
        }
        avatar_spec = tuple(
            c for c in storage_refs.STRICT_MAIN_BUCKET_CONSUMERS if c.name == "users_avatars"
        )

        # Subcase A: No recorded object key column; site-shaped URL -> suspected WARNING only, exit 0
        def _url_only_handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("30s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM pg_attribute" in sql:
                return None
            if "FROM users" in sql:
                if "WHERE user_id >" in sql:
                    return []
                return [
                    ("u-1", None, "https://app.example.com/storage/prod/avatars/u-1.png"),
                    ("u-2", None, "https://lh3.googleusercontent.com/a/ext.png"),
                ]
            return []

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"):
            report_url_only = await storage_refs.run_strict_main_bucket_check(
                conn=_FakeProbeConn(_url_only_handler),
                consumers=avatar_spec,
                env=site_env,
            )
        assert report_url_only.exit_code == 0
        assert report_url_only.confirmed_hits == []
        assert report_url_only.suspected_warnings == {"users_avatars": 1}
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("1 row(s)" in m and "users" in m for m in warn_msgs)
        assert all("u-1.png" not in m for m in warn_msgs)  # does not leak URL/user_id

        # Subcase B: Recorded object key column exists and is populated -> confirmed hit, exit 1
        def _recorded_key_handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("30s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM pg_attribute" in sql:
                return ("avatar_object_key",)
            if "FROM users" in sql:
                return [
                    ("u-1", "avatars/u-1.png", "https://app.example.com/storage/prod/avatars/u-1.png")
                ]
            return []

        report_key = await storage_refs.run_strict_main_bucket_check(
            conn=_FakeProbeConn(_recorded_key_handler),
            consumers=avatar_spec,
            env=site_env,
        )
        assert report_key.exit_code == 1
        assert report_key.confirmed_hits == ["users_avatars"]

    @pytest.mark.asyncio
    async def test_r3_glob_ancestor_dir_read_failure_preserves_manifest_and_git_rows(
        self, tmp_path: Any
    ) -> None:
        """When an ancestor dir matched by ``exclude_rel_globs`` (or a subdir inside it)
        fails ``os.scandir`` with ``PermissionError``, ``scan`` records a read error and
        ``backup.sync_to_db`` keeps every manifest row (never pruning existing ``.git`` rows).
        """
        import errno as errno_mod
        import os as os_mod

        ws_dir = tmp_path / "ws"
        (ws_dir / "projects" / "myrepo" / ".git").mkdir(parents=True)
        (ws_dir / "projects" / "myrepo" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (ws_dir / "projects" / "outer" / "locked_sub" / ".git").mkdir(parents=True)
        (ws_dir / "projects" / "outer" / "locked_sub" / ".git" / "HEAD").write_bytes(
            b"ref: refs/heads/dev\n"
        )
        (ws_dir / "notes.txt").write_bytes(b"keep-me")

        real_scandir = os_mod.scandir
        blocked_top = str(ws_dir / "projects" / "myrepo")
        blocked_nested = str(ws_dir / "projects" / "outer" / "locked_sub")

        def _deny_top(path: Any = ".") -> Any:
            if str(path) == blocked_top:
                raise PermissionError(errno_mod.EACCES, "Permission denied", str(path))
            return real_scandir(path)

        # Subcase 1: Glob-matched ancestor dir itself unreadable -> scan reports EACCES
        with patch.object(transfer_rt.os, "scandir", side_effect=_deny_top):
            scan_out = transfer_rt.scan(
                {
                    "root": str(ws_dir),
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                }
            )
            sweep_out = transfer_rt.sweep(
                {
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                    "projects": [
                        {
                            "key": "ws-lock",
                            "root": str(ws_dir),
                            "mark": {
                                "ns": 9_999_999_999_999_999_999,
                                "boot_id": transfer_rt._boot_id(),
                                "offset_ns": transfer_rt._clock_offset_ns(),
                            },
                        }
                    ],
                }
            )

        assert any(
            e["path"] == "projects/myrepo" and e["errno"] == errno_mod.EACCES
            for e in scan_out["errors"]
        )
        assert "ws-lock" in sweep_out["changed"]

        # Feed that scan result into backup.sync_to_db: delete_removed_files MUST NOT run!
        scan_entries = [
            ScanEntry(
                path=e["path"],
                kind=e["kind"],
                size=e["size"],
                mtime_ns=e["mtime_ns"],
                mode=e["mode"],
                sha256=e["sha256"],
                symlink_target=e["symlink_target"],
                is_binary=e.get("is_binary"),
            )
            for e in scan_out["entries"]
        ]
        scan_result = ScanResult(
            entries=scan_entries,
            oversized=[],
            errors=scan_out["errors"],
            hashed=len(scan_entries),
            reused=0,
        )
        existing = {
            "projects/myrepo/.git/HEAD": {
                "kind": "file",
                "file_size": len(b"ref: refs/heads/main\n"),
                "content_hash": _sha(b"ref: refs/heads/main\n"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
            "notes.txt": {
                "kind": "file",
                "file_size": len(b"keep-me"),
                "content_hash": _sha(b"keep-me"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
        }
        layout = SandboxLayout.for_root("/workspace").for_workspace("ws-lock")
        sandbox = _FakeSandbox({"notes.txt": b"keep-me"}, working_dir="/workspace", dir_name="ws-lock")
        mock_delete_removed = create_autospec(backup.delete_removed_files, return_value=0)

        @asynccontextmanager
        async def _noop_lock(*_a: Any, **_kw: Any):
            yield NoDbConn()

        clock = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
        with (
            patch.object(backup, "workspace_sync_lock", _noop_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=clock)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value=existing)),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", mock_delete_removed),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, return_value=0)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, return_value=0)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=False),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            sync_res = await backup.sync_to_db("ws-lock", sandbox, layout=layout)

        mock_delete_removed.assert_not_awaited()
        assert sync_res.deleted == 0
        assert sync_res.pruned is False
        assert any(
            u.path == "projects/myrepo" and u.reason == "unreadable" for u in sync_res.unsaved
        )

        # Subcase 2: Nested child dir inside matched ancestor unreadable -> also records EACCES
        def _deny_nested(path: Any = ".") -> Any:
            if str(path) == blocked_nested:
                raise PermissionError(errno_mod.EACCES, "Permission denied", str(path))
            return real_scandir(path)

        with patch.object(transfer_rt.os, "scandir", side_effect=_deny_nested):
            scan_nested = transfer_rt.scan(
                {
                    "root": str(ws_dir),
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                }
            )
        assert any(
            e["path"] == "projects/outer" and e["errno"] == errno_mod.EACCES
            for e in scan_nested["errors"]
        )

    def test_r3_download_ticket_drops_entire_query_in_uvicorn_access_and_otel_span(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redemption route drops the ENTIRE query string (including URL-encoded param names
        and URL-encoded path segments) in Uvicorn access logs and OpenTelemetry span attributes.
        """
        from src.dalton.auth.dispatch import SensitiveQueryLogFilter
        from src.observability.private_query import AccessLogQueryFilter, drop_span_query

        monkeypatch.setenv("JWT_SIGNING_SECRET", "q" * 40)
        tok = download_ticket.issue_download_ticket(
            user_id="u-otel",
            workspace_id="ws-otel",
            path="reports/secret.parquet",
            backup_ok=True,
        )
        assert tok is not None

        encoded_targets = [
            f"/api/v1/dalton/workspace-files/download?dl_token={tok}&extra=1",
            f"/api/v1/dalton/workspace-files/download?%64l_token={tok}&extra=1",
            f"/api/v1/dalton/workspace-files/download?%64%6c_token={tok}",
            f"/api/v1/dalton/workspace-files/%64ownload?%64l_token={tok}",
        ]

        # 1. RedactDownloadTicketLogFilter, AccessLogQueryFilter, SensitiveQueryLogFilter
        for flt in (
            download_ticket.RedactDownloadTicketLogFilter(),
            AccessLogQueryFilter(),
            SensitiveQueryLogFilter(),
        ):
            for target in encoded_targets:
                rec = logging.LogRecord(
                    "uvicorn.access",
                    logging.INFO,
                    __file__,
                    1,
                    '%s - "%s %s HTTP/%s" %d',
                    ("127.0.0.1:50000", "GET", target, "1.1", 200),
                    None,
                )
                assert flt.filter(rec) is True
                logged_target = rec.args[2]
                assert tok not in logged_target
                assert "?" not in logged_target
                assert "extra=1" not in logged_target
                assert logged_target == target.partition("?")[0]

            # Unrelated route keeps its normal query string
            rec_ok = logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                __file__,
                1,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:50000", "GET", "/api/v1/threads?limit=20&offset=0", "1.1", 200),
                None,
            )
            flt.filter(rec_ok)
            assert rec_ok.args[2] == "/api/v1/threads?limit=20&offset=0"

        # 2. OpenTelemetry span hook drops entire query string for plain and URL-encoded params
        for raw_query in (f"dl_token={tok}&extra=1", f"%64l_token={tok}&extra=1"):
            span = _FakeOtelSpan(
                {
                    "http.url": f"https://app.example.com/api/v1/dalton/workspace-files/download?{raw_query}",
                    "url.full": f"https://app.example.com/api/v1/dalton/workspace-files/download?{raw_query}",
                    "http.target": f"/api/v1/dalton/workspace-files/download?{raw_query}",
                    "url.path": "/api/v1/dalton/workspace-files/download",
                    "url.query": raw_query,
                }
            )
            drop_span_query(
                span,
                {
                    "path": "/api/v1/dalton/workspace-files/download",
                    "query_string": raw_query.encode("latin-1"),
                },
            )
            assert span.attributes == {
                "http.url": "https://app.example.com/api/v1/dalton/workspace-files/download",
                "url.full": "https://app.example.com/api/v1/dalton/workspace-files/download",
                "http.target": "/api/v1/dalton/workspace-files/download",
                "url.path": "/api/v1/dalton/workspace-files/download",
                "url.query": "",
            }
            for v in span.attributes.values():
                assert tok not in v
                assert "?" not in v

        # 3. Non-ASCII malformed ticket returns uniform 401 (never 500 UnicodeEncodeError)
        with pytest.raises(HTTPException) as exc_non_ascii:
            download_ticket.verify_download_ticket("非ASCII.sig")
        assert exc_non_ascii.value.status_code == 401
        assert exc_non_ascii.value.detail == download_ticket.INVALID_DOWNLOAD_TICKET_DETAIL


class TestStep2Round4Fixes:
    """Targeted regression tests for the 6 Codex round-3 review fixes."""

    def test_r4_workspace_files_startup_union_branches_parenthesized(self) -> None:
        """Both UNION branches of ``workspace_files`` startup probe must be parenthesized before LIMIT."""
        ws_probe = next(
            p for p in storage_refs.STARTUP_MAIN_BUCKET_PROBES if p.name == "workspace_files"
        )
        cap = storage_refs.STARTUP_COUNT_CAP
        assert (
            f"(SELECT workspace_file_id FROM workspace_files WHERE blob_sha256 IS NOT NULL LIMIT {cap})"
            in ws_probe.count_sql
        )
        assert (
            f"(SELECT workspace_file_id FROM workspace_files WHERE pack_sha256 IS NOT NULL LIMIT {cap})"
            in ws_probe.count_sql
        )
        assert ") UNION (" in ws_probe.count_sql

    def test_r4_strict_cli_detects_checkpoint_captured_images_and_legacy_image_capture_ui(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``check --strict`` returns 1 when response images exist ONLY in checkpoint
        ``AIMessage.response_metadata.captured_images`` or legacy ``ui`` ``image_capture`` records,
        while ignoring plain message text containing those strings (exit 0).
        """
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.checkpoint.serde.types import _DeltaSnapshot

        serde = JsonPlusSerializer()

        # Plain text mentioning captured_images / image_capture / response-images/ must NOT trigger
        plain_human = HumanMessage(
            content='{"captured_images":{"charts/a.png":"https://cdn.example.com/response-images/t1/0123456789abcdef/a.png"}}'
        )
        plain_ai = AIMessage(
            content="Discussing image_capture and captured_images response-images/t1/a.png"
        )
        plain_type, plain_blob = serde.dumps_typed(_DeltaSnapshot([plain_human, plain_ai]))

        # Real captured_images ONLY in AIMessage.response_metadata (no replay_facts.images)
        captured_ai = AIMessage(
            id="ai-cap-1",
            content="Here is the chart: ![rev](charts/rev.png)",
            response_metadata={
                "captured_images": {
                    "charts/rev.png": "https://cdn.example.com/response-images/t1/0123456789abcdef/rev.png"
                }
            },
        )
        cap_type, cap_blob = serde.dumps_typed(_DeltaSnapshot([captured_ai]))

        # Legacy image_capture UI record in channel='ui'
        ui_record = [
            {
                "id": "ui-cap-1",
                "name": "image_capture",
                "props": {
                    "path_to_url": {
                        "charts/legacy.png": "https://cdn.example.com/response-images/t2/fedcba9876543210/legacy.png"
                    }
                },
            }
        ]
        ui_type, ui_blob = serde.dumps_typed(ui_record)

        # Unrelated UI record must NOT trigger
        other_ui_type, other_ui_blob = serde.dumps_typed(
            [{"id": "ui-other", "name": "model_fallback", "props": {"reason": "rate_limit"}}]
        )

        # Also verify response_images_turns SQL covers replay_facts->'legacy'->'images' and 'verbatim'
        turns_spec = next(
            c for c in storage_refs.STRICT_MAIN_BUCKET_CONSUMERS if c.name == "response_images_turns"
        )
        assert "replay_facts -> 'legacy' -> 'images'" in turns_spec.batch_sql_first
        assert "replay_facts -> 'legacy' -> 'verbatim'" in turns_spec.batch_sql_first

        def _make_handler(
            *,
            blob_rows: list[tuple[Any, ...]] | None = None,
            write_rows: list[tuple[Any, ...]] | None = None,
        ) -> Any:
            b_rows = list(blob_rows or [])
            w_rows = list(write_rows or [])

            def _handler(sql: str, params: Any = None) -> Any:
                if sql.startswith("SET SESSION"):
                    return None
                if sql.startswith("SELECT set_config"):
                    return ("30s",)
                if sql.startswith("SELECT to_regclass"):
                    return (params[0],)
                if "FROM pg_attribute" in sql:
                    return None
                if sql.startswith("SELECT EXISTS"):
                    return (False,)
                if "FROM checkpoint_blobs" in sql:
                    assert "captured_images" in sql and "image_capture" in sql
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        cursor = tuple(params[:-1])
                        return [r for r in b_rows if r[:4] > cursor][:limit]
                    return b_rows[:limit]
                if "FROM checkpoint_writes" in sql:
                    assert "captured_images" in sql and "image_capture" in sql
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        cursor = tuple(params[:-1])
                        return [r for r in w_rows if r[:5] > cursor][:limit]
                    return w_rows[:limit]
                return []

            return _handler

        # Case 1: Plain text + unrelated UI record -> exit 0
        conn_clean = _FakeCliConn(
            _make_handler(
                blob_rows=[
                    ("t-1", "", "messages", "v1", plain_type, plain_blob, "messages"),
                    ("t-1", "", "ui", "v1", other_ui_type, other_ui_blob, "ui"),
                ],
                write_rows=[
                    ("t-1", "", "cp1", "task1", 0, plain_type, plain_blob, "messages"),
                ],
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_clean),
        ):
            rc_clean = storage_refs.main(["check", "--strict"])
        capsys.readouterr()
        assert rc_clean == 0

        # Case 2: Reference ONLY in checkpoint_blobs AIMessage.response_metadata.captured_images -> exit 1
        conn_cap_blob = _FakeCliConn(
            _make_handler(
                blob_rows=[
                    ("t-2", "", "messages", "v1", cap_type, cap_blob, "messages"),
                ]
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_cap_blob),
        ):
            rc_cap_blob = storage_refs.main(["check", "--strict"])
        rep_cap_blob = json.loads(capsys.readouterr().out)
        assert rc_cap_blob == 1
        assert rep_cap_blob["hits"] == ["widgets_checkpoint_blobs"]

        # Case 3: Reference ONLY in checkpoint_writes AIMessage.response_metadata.captured_images -> exit 1
        conn_cap_write = _FakeCliConn(
            _make_handler(
                write_rows=[
                    ("t-3", "", "cp1", "task1", 0, cap_type, cap_blob, "messages"),
                ]
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_cap_write),
        ):
            rc_cap_write = storage_refs.main(["check", "--strict"])
        rep_cap_write = json.loads(capsys.readouterr().out)
        assert rc_cap_write == 1
        assert rep_cap_write["hits"] == ["widgets_checkpoint_writes"]

        # Case 4: Reference ONLY in legacy ui image_capture record -> exit 1
        conn_ui = _FakeCliConn(
            _make_handler(
                blob_rows=[
                    ("t-4", "", "ui", "v1", ui_type, ui_blob, "ui"),
                ]
            )
        )
        with patch.object(
            storage_refs,
            "open_session_connection",
            create_autospec(storage_refs.open_session_connection, return_value=conn_ui),
        ):
            rc_ui = storage_refs.main(["check", "--strict"])
        rep_ui = json.loads(capsys.readouterr().out)
        assert rc_ui == 1
        assert rep_ui["hits"] == ["widgets_checkpoint_blobs"]

    @pytest.mark.asyncio
    async def test_r4_malformed_avatar_url_treated_as_no_shape_evidence_not_exit_2(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A single malformed ``users.avatar_url`` must be treated as no shape evidence
        rather than raising ``ValueError`` from ``urlsplit`` and turning the whole audit into exit 2.
        """
        site_env = {
            "STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage",
            "STORAGE_KEY_PREFIX": "prod",
        }
        malformed_urls = [
            "https://[invalid/avatars/u-bad.png",
            "http://[::1/avatars/u-bad2.png",
            "https://bad[host]/storage/prod/avatars/u-bad3.png",
        ]
        for bad_url in malformed_urls:
            assert storage_refs.is_site_avatar_url(bad_url, env=site_env) is False

        # Malformed STORAGE_PUBLIC_URL_BASE also does not crash rule parsing
        assert storage_refs.site_avatar_url_rules(env={"STORAGE_PUBLIC_URL_BASE": "https://[bad"}) == []

        avatar_spec = tuple(
            c for c in storage_refs.STRICT_MAIN_BUCKET_CONSUMERS if c.name == "users_avatars"
        )

        def _handler(sql: str, params: Any = None) -> Any:
            if sql.startswith("SELECT set_config"):
                return ("30s",)
            if sql.startswith("SELECT to_regclass"):
                return (params[0],)
            if "FROM pg_attribute" in sql:
                return None
            if "FROM users" in sql:
                if "WHERE user_id >" in sql:
                    return []
                return [
                    ("u-bad-1", None, "https://[invalid/avatars/u-bad.png"),
                    ("u-bad-2", None, "http://[::1/avatars/u-bad2.png"),
                    ("u-site", None, "https://app.example.com/storage/prod/avatars/u-site.png"),
                    ("u-ext", None, "https://lh3.googleusercontent.com/a/ext.png"),
                ]
            return []

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"):
            report = await storage_refs.run_strict_main_bucket_check(
                conn=_FakeProbeConn(_handler),
                consumers=avatar_spec,
                env=site_env,
            )

        assert report.exit_code == storage_refs.EXIT_CLEAN
        assert report.errors == []
        assert report.confirmed_hits == []
        assert report.suspected_warnings == {"users_avatars": 1}

    @pytest.mark.asyncio
    async def test_r4_glob_ancestor_dir_vanished_mid_probe_preserves_manifest_and_git_rows(
        self, tmp_path: Any
    ) -> None:
        """``FileNotFoundError`` during ``_has_prot`` (ancestor or nested subdir briefly renamed/removed)
        is converted into a non-ENOENT read error so ``backup.sync_to_db`` keeps every manifest row
        and never prunes existing ``.git`` rows.
        """
        import errno as errno_mod
        import os as os_mod

        ws_dir = tmp_path / "ws"
        (ws_dir / "projects" / "myrepo" / ".git").mkdir(parents=True)
        (ws_dir / "projects" / "myrepo" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (ws_dir / "projects" / "outer" / "renamed_sub" / ".git").mkdir(parents=True)
        (ws_dir / "projects" / "outer" / "renamed_sub" / ".git" / "HEAD").write_bytes(
            b"ref: refs/heads/dev\n"
        )
        (ws_dir / "notes.txt").write_bytes(b"keep-me")

        real_scandir = os_mod.scandir
        vanished_nested = str(ws_dir / "projects" / "outer" / "renamed_sub")
        vanished_top = str(ws_dir / "projects" / "myrepo")

        def _vanish_nested(path: Any = ".") -> Any:
            if str(path) == vanished_nested:
                raise FileNotFoundError(errno_mod.ENOENT, "No such file or directory", str(path))
            return real_scandir(path)

        # Subcase 1: Nested subdir vanishes mid-probe -> scan records EAGAIN (not swallowed / not ENOENT)
        with patch.object(transfer_rt.os, "scandir", side_effect=_vanish_nested):
            scan_out = transfer_rt.scan(
                {
                    "root": str(ws_dir),
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                }
            )
            sweep_out = transfer_rt.sweep(
                {
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                    "projects": [
                        {
                            "key": "ws-vanish",
                            "root": str(ws_dir),
                            "mark": {
                                "ns": 9_999_999_999_999_999_999,
                                "boot_id": transfer_rt._boot_id(),
                                "offset_ns": transfer_rt._clock_offset_ns(),
                            },
                        }
                    ],
                }
            )

        assert any(
            e["path"] == "projects/outer" and e["errno"] == errno_mod.EAGAIN
            for e in scan_out["errors"]
        )
        assert "ws-vanish" in sweep_out["changed"]

        # Feed into backup.sync_to_db: delete_removed_files MUST NOT run; existing .git rows preserved
        scan_entries = [
            ScanEntry(
                path=e["path"],
                kind=e["kind"],
                size=e["size"],
                mtime_ns=e["mtime_ns"],
                mode=e["mode"],
                sha256=e["sha256"],
                symlink_target=e["symlink_target"],
                is_binary=e.get("is_binary"),
            )
            for e in scan_out["entries"]
        ]
        scan_result = ScanResult(
            entries=scan_entries,
            oversized=[],
            errors=scan_out["errors"],
            hashed=len(scan_entries),
            reused=0,
        )
        existing = {
            "projects/outer/renamed_sub/.git/HEAD": {
                "kind": "file",
                "file_size": len(b"ref: refs/heads/dev\n"),
                "content_hash": _sha(b"ref: refs/heads/dev\n"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
            "notes.txt": {
                "kind": "file",
                "file_size": len(b"keep-me"),
                "content_hash": _sha(b"keep-me"),
                "mtime_ns": 1_700_000_000_000_000_000,
                "permissions": "0644",
                "blob_sha256": None,
                "pack_sha256": None,
                "pack_offset": None,
                "is_binary": False,
            },
        }
        layout = SandboxLayout.for_root("/workspace").for_workspace("ws-vanish")
        sandbox = _FakeSandbox(
            {"notes.txt": b"keep-me"}, working_dir="/workspace", dir_name="ws-vanish"
        )
        mock_delete_removed = create_autospec(backup.delete_removed_files, return_value=0)

        @asynccontextmanager
        async def _noop_lock(*_a: Any, **_kw: Any):
            yield NoDbConn()

        clock = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
        with (
            patch.object(backup, "workspace_sync_lock", _noop_lock),
            patch.object(backup, "manifest_clock", create_autospec(backup.manifest_clock, return_value=clock)),
            patch.object(backup, "get_file_metadata_for_sync", create_autospec(backup.get_file_metadata_for_sync, return_value=existing)),
            patch.object(backup, "files_restore_incomplete", create_autospec(backup.files_restore_incomplete, return_value=False)),
            patch.object(backup, "delete_removed_files", mock_delete_removed),
            patch.object(backup, "get_workspace_total_size", create_autospec(backup.get_workspace_total_size, return_value=0)),
            patch.object(backup, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, return_value=0)),
            patch.object(blobs, "bulk_upsert_files", create_autospec(wf_db.bulk_upsert_files, return_value=0)),
            patch.object(backup, "bulk_update_file_stamps", create_autospec(backup.bulk_update_file_stamps, return_value=None)),
            patch.object(backup, "is_storage_enabled", return_value=False),
            patch.object(backup, "scan_workspace", create_autospec(backup.scan_workspace, return_value=scan_result)),
        ):
            sync_res = await backup.sync_to_db("ws-vanish", sandbox, layout=layout)

        mock_delete_removed.assert_not_awaited()
        assert sync_res.deleted == 0
        assert sync_res.pruned is False
        assert any(
            u.path == "projects/outer" and u.reason == "unreadable" for u in sync_res.unsaved
        )

        # Subcase 2: Top matched ancestor dir vanishes right at os.scandir -> also records EAGAIN
        def _vanish_top(path: Any = ".") -> Any:
            if str(path) == vanished_top:
                raise FileNotFoundError(errno_mod.ENOENT, "No such file or directory", str(path))
            return real_scandir(path)

        with patch.object(transfer_rt.os, "scandir", side_effect=_vanish_top):
            scan_top = transfer_rt.scan(
                {
                    "root": str(ws_dir),
                    "whole_dir_names": [".git"],
                    "exclude_rel_globs": ["projects/*"],
                }
            )
        assert any(
            e["path"] == "projects/myrepo" and e["errno"] == errno_mod.EAGAIN
            for e in scan_top["errors"]
        )

    def test_r4_download_ticket_raw_quote_query_truncated_at_first_question_mark(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Query values containing raw quotes (``, ', <, >, #) before ``dl_token`` must be
        truncated directly at the first ``?`` so no filter leaks the trailing ticket into logs.
        """
        from src.dalton.auth.dispatch import SensitiveQueryLogFilter
        from src.observability.private_query import AccessLogQueryFilter

        monkeypatch.setenv("JWT_SIGNING_SECRET", "r" * 40)
        tok = download_ticket.issue_download_ticket(
            user_id="u-quote",
            workspace_id="ws-quote",
            path="reports/quoted.parquet",
            backup_ok=True,
        )
        assert tok is not None

        tricky_targets = [
            f'/api/v1/dalton/workspace-files/download?a=1"2&dl_token={tok}',
            f'/api/v1/dalton/workspace-files/download?x="quoted"&%64l_token={tok}&tail=1',
            f"/api/v1/dalton/workspace-files/download?a='1'#frag&dl_token={tok}",
            f'/api/v1/dalton/workspace-files/%64ownload?q=<tag>"x"&dl_token={tok}',
        ]

        # 1. Chained filter pipeline (exact order used on uvicorn.access) & each filter solo
        pipeline = (
            download_ticket.RedactDownloadTicketLogFilter(),
            AccessLogQueryFilter(),
            SensitiveQueryLogFilter(),
        )
        for target in tricky_targets:
            expected_path = target.partition("?")[0]

            # Chained execution on one LogRecord
            rec_chain = logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                __file__,
                1,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:50000", "GET", target, "1.1", 200),
                None,
            )
            for flt in pipeline:
                assert flt.filter(rec_chain) is True
            assert rec_chain.args[2] == expected_path
            formatted = rec_chain.getMessage()
            assert tok not in formatted
            assert "?" not in formatted
            assert "dl_token" not in formatted

            # Pre-formatted msg string (embedded in quotes)
            rec_msg = logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                __file__,
                1,
                f'127.0.0.1:50000 - "GET {target} HTTP/1.1" 200',
                None,
                None,
            )
            assert download_ticket.RedactDownloadTicketLogFilter().filter(rec_msg) is True
            assert tok not in rec_msg.msg
            assert "?" not in rec_msg.msg
            assert expected_path in rec_msg.msg


class TestStep2Round5Fixes:
    """Targeted regression tests for Codex round-4 review fixes:
    1. ``replay_facts.legacy.verbatim`` response-image scanning in ``check --strict``.
    2. Origin (site / main bucket) + object-path-prefix verification for response-image URLs
       (external URLs with ``response-images/`` only in query strings never trigger false positives).
    """

    def test_r5_response_image_url_checks_origin_and_object_path_prefix(self) -> None:
        site_env = {
            "STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage",
            "STORAGE_KEY_PREFIX": "prod",
        }
        # Valid site origin + configured prefix + response-images/ object key -> True
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://app.example.com/storage/prod/response-images/t1/0123456789abcdef/chart.png",
                env=site_env,
            )
            is True
        )
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://APP.EXAMPLE.COM:443/storage/prod/response-images/0123456789abcdef/chart.png?v=1",
                env=site_env,
            )
            is True
        )
        # Site-relative URL matching configured path prefix -> True
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "/storage/prod/response-images/t1/0123456789abcdef/chart.png",
                env=site_env,
            )
            is True
        )

        # External URL with response-images/ ONLY in query string -> False (both with env and env={})
        ext_query_urls = [
            "https://external.example.com/page?next=response-images/t1/0123456789abcdef/chart.png",
            "https://external.example.com/view?img=https://app.example.com/storage/prod/response-images/t1/0123456789abcdef/chart.png",
            "https://app.example.com/storage/prod/other?ref=response-images/t1/0123456789abcdef/chart.png",
            "https://app.example.com/storage/prod/page#response-images/t1/0123456789abcdef/chart.png",
        ]
        for url in ext_query_urls:
            assert storage_refs.is_main_bucket_response_image_url(url, env=site_env) is False
            assert storage_refs.is_main_bucket_response_image_url(url, env={}) is False

        # External origin even with matching path prefix -> False when site/bucket env is configured
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://other.example.com/storage/prod/response-images/t1/0123456789abcdef/chart.png",
                env=site_env,
            )
            is False
        )
        # Same origin with wrong path prefix or missing/wrong STORAGE_KEY_PREFIX -> False
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://app.example.com/storage/response-images/t1/0123456789abcdef/chart.png",
                env=site_env,
            )
            is False
        )
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://app.example.com/other/prod/response-images/t1/0123456789abcdef/chart.png",
                env=site_env,
            )
            is False
        )
        # Path traversal or empty object tail or malformed URL -> False
        for bad_url in (
            "https://app.example.com/storage/prod/response-images/",
            "https://app.example.com/storage/prod/response-images/../secret.png",
            "https://[invalid/storage/prod/response-images/t1/0123456789abcdef/chart.png",
        ):
            assert storage_refs.is_main_bucket_response_image_url(bad_url, env=site_env) is False

        # Main-bucket S3 + MinIO endpoint origins
        s3_env = {
            "STORAGE_BUCKET_NAME": "dalton-main",
            "STORAGE_REGION": "us-east-1",
            "STORAGE_ENDPOINT_URL": "http://minio:9000",
        }
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "http://minio:9000/dalton-main/response-images/t1/0123456789abcdef/a.png",
                env=s3_env,
            )
            is True
        )
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "https://dalton-main.s3.us-east-1.amazonaws.com/response-images/t1/0123456789abcdef/a.png",
                env=s3_env,
            )
            is True
        )
        assert (
            storage_refs.is_main_bucket_response_image_url(
                "http://minio:9000/other-bucket/response-images/t1/0123456789abcdef/a.png",
                env=s3_env,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_r5_strict_audit_scans_legacy_verbatim_and_rejects_external_query_urls(
        self,
    ) -> None:
        """``check --strict`` detects main-bucket images stored ONLY in
        ``replay_facts.legacy.verbatim`` (including chunk-split URLs) while returning exit 0
        when only external/query-string URLs appear across ``replay_facts``, ``subagent_runs``,
        and checkpoint blobs/writes.
        """
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        site_env = {"STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage"}

        ext_query_url = (
            "https://external.example.com/viewer?next=response-images/t1/0123456789abcdef/a.png"
        )
        foreign_origin_url = (
            "https://other.example.com/storage/response-images/t1/0123456789abcdef/a.png"
        )
        real_site_url = (
            "https://app.example.com/storage/response-images/t1/0123456789abcdef/chart.png"
        )

        # Checkpoint AIMessage & UI record holding ONLY external query-string / foreign-origin URLs
        ext_ai = AIMessage(
            id="ai-ext",
            content="![x](charts/x.png)",
            response_metadata={
                "captured_images": {
                    "charts/x.png": ext_query_url,
                    "charts/y.png": foreign_origin_url,
                }
            },
        )
        ext_ai_type, ext_ai_blob = serde.dumps_typed([ext_ai])
        ext_ui_type, ext_ui_blob = serde.dumps_typed(
            [
                {
                    "id": "ui-ext",
                    "name": "image_capture",
                    "props": {"path_to_url": {"charts/x.png": ext_query_url}},
                }
            ]
        )

        def _make_handler(
            *,
            resp_rows: list[tuple[Any, Any]] | None = None,
            sub_rows: list[tuple[Any, Any]] | None = None,
            blob_rows: list[tuple[Any, ...]] | None = None,
            write_rows: list[tuple[Any, ...]] | None = None,
        ) -> Any:
            r_rows = list(resp_rows or [])
            s_rows = list(sub_rows or [])
            b_rows = list(blob_rows or [])
            w_rows = list(write_rows or [])

            def _handler(sql: str, params: Any = None) -> Any:
                if sql.startswith("SELECT set_config"):
                    return ("30s",)
                if sql.startswith("SELECT to_regclass"):
                    return (params[0],)
                if "FROM pg_attribute" in sql:
                    return None
                if sql.startswith("SELECT EXISTS"):
                    return (False,)
                if "FROM conversation_responses" in sql:
                    limit = int(params[-1])
                    if "WHERE conversation_response_id >" in sql:
                        return [r for r in r_rows if r[0] > params[0]][:limit]
                    return r_rows[:limit]
                if "FROM subagent_runs" in sql:
                    limit = int(params[-1])
                    if "WHERE task_run_id >" in sql:
                        return [r for r in s_rows if r[0] > params[0]][:limit]
                    return s_rows[:limit]
                if "FROM checkpoint_blobs" in sql:
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        return [r for r in b_rows if r[:4] > tuple(params[:-1])][:limit]
                    return b_rows[:limit]
                if "FROM checkpoint_writes" in sql:
                    limit = int(params[-1])
                    if "WHERE (" in sql:
                        return [r for r in w_rows if r[:5] > tuple(params[:-1])][:limit]
                    return w_rows[:limit]
                return []

            return _handler

        # Case 1: External query-string + foreign-origin URLs everywhere -> exit 0
        clean_report = await storage_refs.run_strict_main_bucket_check(
            conn=_FakeProbeConn(
                _make_handler(
                    resp_rows=[
                        (
                            "resp-1",
                            {
                                "images": {"a.png": ext_query_url},
                                "legacy": {
                                    "v": 3,
                                    "images": {"b.png": foreign_origin_url},
                                    "verbatim": [
                                        {
                                            "event": "message_chunk",
                                            "data": {
                                                "id": "lc-1",
                                                "agent": "main",
                                                "content_type": "text",
                                                "content": (
                                                    f"See [link]({ext_query_url}) and "
                                                    f"![ext]({foreign_origin_url})"
                                                ),
                                            },
                                        }
                                    ],
                                },
                            },
                        )
                    ],
                    sub_rows=[("run-1", {"images": {"s.png": ext_query_url}})],
                    blob_rows=[
                        ("t-1", "", "messages", "v1", ext_ai_type, ext_ai_blob, "messages"),
                        ("t-1", "", "ui", "v1", ext_ui_type, ext_ui_blob, "ui"),
                    ],
                    write_rows=[
                        ("t-1", "", "cp1", "task1", 0, ext_ai_type, ext_ai_blob, "messages"),
                    ],
                )
            ),
            env=site_env,
        )
        assert clean_report.exit_code == storage_refs.EXIT_CLEAN
        assert clean_report.confirmed_hits == []

        # Case 2: Main-bucket response image ONLY in replay_facts.legacy.verbatim -> exit 1
        verbatim_report = await storage_refs.run_strict_main_bucket_check(
            conn=_FakeProbeConn(
                _make_handler(
                    resp_rows=[
                        (
                            "resp-verbatim",
                            {
                                "legacy": {
                                    "v": 3,
                                    "verbatim": [
                                        {
                                            "event": "message_chunk",
                                            "data": {
                                                "id": "lc-v1",
                                                "agent": "main",
                                                "content_type": "text",
                                                "content": f"Here is the chart: ![rev]({real_site_url})",
                                            },
                                        }
                                    ],
                                }
                            },
                        )
                    ]
                )
            ),
            env=site_env,
        )
        assert verbatim_report.exit_code == storage_refs.EXIT_REFERENCES_FOUND
        assert verbatim_report.confirmed_hits == ["response_images_turns"]

        # Case 3: Chunk-split URL across two message_chunk rows in legacy.verbatim -> exit 1
        split_idx = real_site_url.index("response-images/") + 9
        chunk_split_report = await storage_refs.run_strict_main_bucket_check(
            conn=_FakeProbeConn(
                _make_handler(
                    resp_rows=[
                        (
                            "resp-split",
                            {
                                "legacy": {
                                    "v": 3,
                                    "verbatim": [
                                        {
                                            "event": "message_chunk",
                                            "data": {
                                                "id": "lc-split",
                                                "agent": "main",
                                                "content_type": "text",
                                                "content": f"![chart]({real_site_url[:split_idx]}",
                                            },
                                        },
                                        {
                                            "event": "message_chunk",
                                            "data": {
                                                "id": "lc-split",
                                                "agent": "main",
                                                "content_type": "text",
                                                "content": f"{real_site_url[split_idx:]})",
                                            },
                                        },
                                    ],
                                }
                            },
                        )
                    ]
                )
            ),
            env=site_env,
        )
        assert chunk_split_report.exit_code == storage_refs.EXIT_REFERENCES_FOUND
        assert chunk_split_report.confirmed_hits == ["response_images_turns"]
