"""Unit tests for Dalton chat composer large-file uploads (<= 200 MiB) and sandbox materialization."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import time
from typing import Any
from unittest.mock import create_autospec, patch
import uuid

from alembic import op as alembic_op
from fastapi import FastAPI, HTTPException
import httpx
import pytest

from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.dalton import chat_upload_storage
import src.dalton.app.chat_uploads as chat_uploads_app
from src.dalton.app.chat_uploads import router as chat_uploads_router
from src.dalton.database import chat_uploads as db_chat_uploads
from src.dalton.migrations.versions import d033_chat_file_uploads
from src.dalton.services import chat_uploads as svc
from src.dalton.settings import DaltonConfig, get_dalton_config
from src.server.database.home_workspace import get_home_workspace_id
from src.server.handlers.chat.attachments import attach_request_files
from src.server.handlers.chat.request_prep import serialize_context_metadata
from src.server.models.additional_context import ChatUploadContext, MultimodalContext
from src.server.models.chat import ChatRequest
from src.server.services.runs.pending_input import PendingInput, input_rerun_request
from src.server.utils.api import get_current_user_id

_REAL_COMPUTE_STORED_OBJECT_DIGEST = chat_upload_storage.compute_stored_object_digest


# ---------------------------------------------------------------------------
# In-memory guarded DB & MinIO store fakes (multi-worker CAS simulation)
# ---------------------------------------------------------------------------


class InMemoryChatUploadLedger:
    """Thread/task-safe in-memory state machine mirroring ``dalton_chat_file_uploads`` SQL guards."""

    def __init__(self, workspaces: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.workspaces = workspaces if workspaces is not None else {}
        self._lock = asyncio.Lock()

    async def create_upload_guarded(
        self,
        *,
        upload_id: str,
        user_id: str,
        workspace_id: str,
        filename: str,
        safe_filename: str,
        mime_type: str,
        size_bytes: int,
        part_size: int,
        part_count: int,
        object_key: str,
        store_upload_id: str,
        expected_sha256: str | None,
        ttl: timedelta,
        max_active_uploads: int = 10,
        max_active_bytes: int = 1000 * 1024 * 1024,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            active_rows = [
                r
                for r in self.rows.values()
                if r["user_id"] == user_id
                and r["state"] in ("uploading", "completing")
                and r["expires_at"] is not None
                and r["expires_at"] > now
            ]
            if len(active_rows) >= max_active_uploads:
                return None
            if sum(int(r["size_bytes"]) for r in active_rows) + int(size_bytes) > max_active_bytes:
                return None
            row = {
                "upload_id": upload_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "filename": filename,
                "safe_filename": safe_filename,
                "mime_type": mime_type,
                "size_bytes": int(size_bytes),
                "part_size": int(part_size),
                "part_count": int(part_count),
                "object_key": object_key,
                "store_upload_id": store_upload_id,
                "expected_sha256": expected_sha256,
                "actual_sha256": None,
                "uploaded_parts": {},
                "state": "uploading",
                "error": None,
                "lease_until": None,
                "expires_at": now + ttl,
                "used_at": None,
                "cleaned_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self.rows[upload_id] = row
            return dict(row)

    async def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        row = self.rows.get(upload_id)
        return dict(row) if row else None

    async def get_workspace_permission_row(
        self,
        workspace_id: str,
        *,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        ws = self.workspaces.get(workspace_id)
        return dict(ws) if ws else None

    async def get_uploads_by_ids(self, upload_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {uid: dict(self.rows[uid]) for uid in upload_ids if uid in self.rows}

    async def record_uploaded_part(
        self,
        upload_id: str,
        *,
        part_number: int,
        etag: str,
        size_bytes: int,
        sha256: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["state"] != "uploading"
                or row["expires_at"] is None
                or row["expires_at"] <= now
            ):
                return None
            parts = dict(row["uploaded_parts"])
            parts[str(int(part_number))] = {
                "etag": etag,
                "size": int(size_bytes),
                "sha256": sha256,
            }
            row["uploaded_parts"] = parts
            row["updated_at"] = now
            return dict(row)

    async def claim_complete(
        self,
        upload_id: str,
        *,
        lease: timedelta,
        expected_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["expires_at"] is None or row["expires_at"] <= now:
                return None
            can_claim = row["state"] == "uploading" or (
                row["state"] == "completing"
                and (row["lease_until"] is None or row["lease_until"] < now)
            )
            if not can_claim:
                return None
            row["state"] = "completing"
            row["lease_until"] = now + lease
            if expected_sha256:
                row["expected_sha256"] = expected_sha256
            row["updated_at"] = now
            return dict(row)

    async def reopen_uploading(self, upload_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["state"] != "completing":
                return None
            row["state"] = "uploading"
            row["lease_until"] = None
            row["updated_at"] = now
            return dict(row)

    async def mark_ready(
        self,
        upload_id: str,
        *,
        actual_sha256: str,
        ready_ttl: timedelta,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["state"] != "completing":
                return None
            row["state"] = "ready"
            row["actual_sha256"] = actual_sha256
            row["lease_until"] = None
            row["error"] = None
            row["expires_at"] = max(row["expires_at"] or now, now + ready_ttl)
            row["updated_at"] = now
            return dict(row)

    async def mark_failed(self, upload_id: str, *, error: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["state"] not in ("uploading", "completing"):
                return None
            row["state"] = "failed"
            row["error"] = error[:500]
            row["lease_until"] = None
            row["expires_at"] = min(row["expires_at"] or now, now)
            row["updated_at"] = now
            return dict(row)

    async def abort_upload(self, upload_id: str, *, user_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["user_id"] != user_id:
                return None
            allowed = row["state"] in ("uploading", "completing", "failed") or (
                row["state"] == "ready" and row["used_at"] is None
            )
            if not allowed:
                return None
            row["state"] = "aborted"
            row["lease_until"] = None
            row["expires_at"] = min(row["expires_at"] or now, now)
            row["updated_at"] = now
            return dict(row)

    async def mark_used(
        self,
        upload_id: str,
        *,
        user_id: str,
        workspace_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        eff_now = now or datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["user_id"] != user_id
                or row["workspace_id"] != workspace_id
                or row["state"] != "ready"
                or row["cleaned_at"] is not None
            ):
                return None
            if row["used_at"] is None and (
                row["expires_at"] is None or row["expires_at"] <= eff_now
            ):
                return None
            row["used_at"] = row["used_at"] or eff_now
            row["expires_at"] = None
            row["updated_at"] = eff_now
            return dict(row)

    async def has_retained_workspace_uploads(
        self, workspace_id: str, *, conn: Any = None
    ) -> bool:
        return len(await self.list_retained_workspace_uploads(workspace_id, conn=conn)) > 0

    async def list_retained_workspace_uploads(
        self, workspace_id: str, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [
                dict(r)
                for r in self.rows.values()
                if r["workspace_id"] == workspace_id
                and r["state"] == "ready"
                and r["used_at"] is not None
                and r["cleaned_at"] is None
            ]
        rows.sort(key=lambda r: (r["created_at"], r["upload_id"]))
        return rows

    async def retire_uploads_by_ids(
        self, upload_ids: Any, *, workspace_id: str
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        wanted = {str(u) for u in upload_ids}
        retired: list[dict[str, Any]] = []
        async with self._lock:
            for uid in wanted:
                row = self.rows.get(uid)
                if row and row["workspace_id"] == workspace_id and row["cleaned_at"] is None:
                    row["state"] = "aborted"
                    row["lease_until"] = None
                    row["expires_at"] = now
                    row["updated_at"] = now
                    retired.append(dict(row))
        return retired

    async def retire_workspace_uploads(self, workspace_id: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        retired: list[dict[str, Any]] = []
        async with self._lock:
            for row in self.rows.values():
                if row["workspace_id"] == workspace_id and row["cleaned_at"] is None:
                    row["state"] = "aborted"
                    row["lease_until"] = None
                    row["expires_at"] = now
                    row["updated_at"] = now
                    retired.append(dict(row))
        return retired

    async def claim_cleanup_batch(
        self,
        *,
        limit: int = 25,
        claim_lease: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        eff_now = now or datetime.now(timezone.utc)
        claimed: list[dict[str, Any]] = []
        async with self._lock:
            candidates = sorted(
                self.rows.values(),
                key=lambda r: r["expires_at"] or r["updated_at"],
            )
            for row in candidates:
                if len(claimed) >= limit:
                    break
                if row["cleaned_at"] is not None:
                    continue
                if row["lease_until"] is not None and row["lease_until"] >= eff_now:
                    continue
                ws = self.workspaces.get(row["workspace_id"])
                ws_deleted = bool(ws and ws.get("status") == "deleted")
                due = (
                    row["state"] in ("aborted", "failed")
                    or (
                        row["state"] in ("uploading", "completing")
                        and row["expires_at"] is not None
                        and row["expires_at"] <= eff_now
                    )
                    or (
                        row["state"] == "ready"
                        and row["used_at"] is None
                        and row["expires_at"] is not None
                        and row["expires_at"] <= eff_now
                    )
                    or ws_deleted
                )
                if not due:
                    continue
                if row["state"] in ("uploading", "completing", "ready"):
                    row["state"] = "aborted"
                row["lease_until"] = eff_now + claim_lease
                row["updated_at"] = eff_now
                claimed.append(dict(row))
        return claimed

    async def mark_cleaned(self, upload_id: str) -> bool:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if row and row["cleaned_at"] is None:
                row["cleaned_at"] = now
                row["lease_until"] = None
                row["updated_at"] = now
                return True
            return False

    async def release_cleanup_claim(
        self, upload_id: str, *, retry_after: timedelta = timedelta(minutes=2)
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            if row := self.rows.get(upload_id):
                if row["cleaned_at"] is None:
                    row["lease_until"] = now + retry_after

    async def prune_cleaned_rows(
        self,
        *,
        older_than: timedelta = timedelta(days=14),
        limit: int = 200,
        now: datetime | None = None,
    ) -> int:
        eff_now = now or datetime.now(timezone.utc)
        cutoff = eff_now - older_than
        removed = 0
        async with self._lock:
            for uid, row in list(self.rows.items()):
                if removed >= limit:
                    break
                if row["cleaned_at"] is not None and row["cleaned_at"] < cutoff:
                    del self.rows[uid]
                    removed += 1
        return removed


class InMemoryMultipartStore:
    """In-memory multipart S3 simulator tracking open uploads, parts, and assembled objects."""

    def __init__(self) -> None:
        self.open_uploads: dict[tuple[str, str], dict[int, tuple[str, bytes]]] = {}
        self.objects: dict[str, bytes] = {}
        self._seq = 0

    def open_multipart_upload(self, object_key: str, *, content_type: str) -> str | None:
        assert chat_upload_storage.is_chat_upload_key(object_key)
        self._seq += 1
        mp_id = f"mp-{self._seq}"
        self.open_uploads[(object_key, mp_id)] = {}
        return mp_id

    def upload_part_bytes(
        self,
        object_key: str,
        store_upload_id: str,
        *,
        part_number: int,
        data: bytes,
    ) -> str | None:
        assert chat_upload_storage.is_chat_upload_key(object_key)
        parts = self.open_uploads.get((object_key, store_upload_id))
        if parts is None:
            return None
        etag = f'"{hashlib.md5(data).hexdigest()}"'
        parts[int(part_number)] = (etag, bytes(data))
        return etag

    def list_uploaded_parts(
        self, object_key: str, store_upload_id: str
    ) -> list[dict[str, Any]] | None:
        assert chat_upload_storage.is_chat_upload_key(object_key)
        parts = self.open_uploads.get((object_key, store_upload_id))
        if parts is None:
            return None
        return [
            {"part_number": pn, "size": len(buf), "etag": etag}
            for pn, (etag, buf) in sorted(parts.items())
        ]

    def complete_multipart_upload(
        self, object_key: str, store_upload_id: str, parts: list[tuple[int, str]]
    ) -> bool:
        assert chat_upload_storage.is_chat_upload_key(object_key)
        held = self.open_uploads.get((object_key, store_upload_id))
        if held is None:
            return False
        assembled = bytearray()
        for pn, etag in parts:
            entry = held.get(pn)
            if not entry or entry[0] != etag:
                return False
            assembled.extend(entry[1])
        self.objects[object_key] = bytes(assembled)
        del self.open_uploads[(object_key, store_upload_id)]
        return True

    def abort_multipart_upload(self, object_key: str, store_upload_id: str) -> bool:
        if not chat_upload_storage.is_chat_upload_key(object_key):
            return False
        self.open_uploads.pop((object_key, store_upload_id), None)
        return True

    def delete_object(self, object_key: str) -> bool:
        if not chat_upload_storage.is_chat_upload_key(object_key):
            return False
        self.objects.pop(object_key, None)
        return True

    def object_size(self, object_key: str) -> int | None:
        buf = self.objects.get(object_key)
        return len(buf) if buf is not None else None

    def compute_stored_object_digest(self, object_key: str) -> tuple[int, str] | None:
        buf = self.objects.get(object_key)
        if buf is None:
            return None
        return len(buf), hashlib.sha256(buf).hexdigest()

    def sign_download_url(self, object_key: str, *, expires_in: int = 900) -> str | None:
        assert chat_upload_storage.is_chat_upload_key(object_key)
        return f"http://host.docker.internal:9000/test-bucket/{object_key}?exp={expires_in}"


async def _bytes_stream(payload: bytes, chunk_size: int = 4096):
    for idx in range(0, len(payload), chunk_size):
        yield payload[idx : idx + chunk_size]


@pytest.fixture
def upload_env(monkeypatch: pytest.MonkeyPatch):
    """Patch Dalton settings, workspace checks, DB ledger, and S3 multipart store with autospecced fakes."""
    workspaces: dict[str, dict[str, Any]] = {}
    ledger = InMemoryChatUploadLedger(workspaces=workspaces)
    store = InMemoryMultipartStore()

    enabled_settings = DaltonConfig(enabled=True)
    mock_get_settings = create_autospec(get_dalton_config, return_value=enabled_settings)
    monkeypatch.setattr(svc, "get_dalton_settings", mock_get_settings)
    monkeypatch.setattr(chat_uploads_app, "get_dalton_settings", mock_get_settings)

    # Wire DB functions via create_autospec to guarantee signature fidelity
    for fn_name in (
        "create_upload_guarded",
        "get_upload",
        "get_workspace_permission_row",
        "get_uploads_by_ids",
        "record_uploaded_part",
        "claim_complete",
        "reopen_uploading",
        "mark_ready",
        "mark_failed",
        "abort_upload",
        "mark_used",
        "has_retained_workspace_uploads",
        "list_retained_workspace_uploads",
        "retire_uploads_by_ids",
        "retire_workspace_uploads",
        "claim_cleanup_batch",
        "mark_cleaned",
        "release_cleanup_claim",
        "prune_cleaned_rows",
    ):
        real_fn = getattr(db_chat_uploads, fn_name)
        fake_impl = getattr(ledger, fn_name)
        specced = create_autospec(real_fn, side_effect=fake_impl)
        monkeypatch.setattr(db_chat_uploads, fn_name, specced)

    # Wire storage functions via create_autospec
    monkeypatch.setattr(
        chat_upload_storage,
        "is_chat_upload_storage_configured",
        create_autospec(chat_upload_storage.is_chat_upload_storage_configured, return_value=True),
    )
    for store_fn in (
        "open_multipart_upload",
        "upload_part_bytes",
        "list_uploaded_parts",
        "complete_multipart_upload",
        "abort_multipart_upload",
        "delete_object",
        "object_size",
        "compute_stored_object_digest",
        "sign_download_url",
    ):
        real_fn = getattr(chat_upload_storage, store_fn)
        fake_impl = getattr(store, store_fn)
        monkeypatch.setattr(
            chat_upload_storage,
            store_fn,
            create_autospec(real_fn, side_effect=fake_impl),
        )

    async def _fake_requested_workspace(
        user_id: str,
        workspace_id: str | None,
        *,
        channel: str = "web",
        is_tail_turn: bool = False,
        is_existing_colleague_thread: bool = False,
    ) -> dict[str, Any] | None:
        if not workspace_id:
            raise HTTPException(status_code=400, detail="workspace_id required")
        home_id = get_home_workspace_id(user_id)
        if workspace_id == home_id and workspace_id not in workspaces:
            return None
        ws = workspaces.get(workspace_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if str(ws["user_id"]) != str(user_id):
            raise HTTPException(status_code=403, detail="Forbidden")
        return ws

    monkeypatch.setattr(
        svc,
        "requested_workspace",
        create_autospec(svc.requested_workspace, side_effect=_fake_requested_workspace),
    )

    return {
        "ledger": ledger,
        "store": store,
        "workspaces": workspaces,
    }


# ---------------------------------------------------------------------------
# 1. Migration d033 & storage prefix guard tests
# ---------------------------------------------------------------------------


def test_migration_d033_contract() -> None:
    assert d033_chat_file_uploads.revision == "d033"
    assert d033_chat_file_uploads.down_revision == "d030"

    executed_sql: list[str] = []
    with patch.object(
        alembic_op,
        "execute",
        autospec=True,
        side_effect=lambda sql: executed_sql.append(str(sql)),
    ):
        d033_chat_file_uploads.upgrade()
        assert any("CREATE TABLE IF NOT EXISTS dalton_chat_file_uploads" in s for s in executed_sql)
        assert any("idx_dalton_chat_file_uploads_cleanup" in s for s in executed_sql)

        executed_sql.clear()
        d033_chat_file_uploads.downgrade()
        assert any("DROP TABLE IF EXISTS dalton_chat_file_uploads" in s for s in executed_sql)

    # Guard against AmbiguousColumn in CTE UPDATE ... FROM due RETURNING:
    assert "RETURNING {_U_COLUMNS}" in inspect.getsource(db_chat_uploads.claim_cleanup_batch)
    for col_expr in db_chat_uploads._U_COLUMNS.split(","):
        assert col_expr.strip().startswith("u.")


def test_storage_key_isolation_never_touches_asr_prefix() -> None:
    ws_id = str(uuid.uuid4())
    up_id = str(uuid.uuid4())
    key = chat_upload_storage.build_chat_upload_key(ws_id, up_id, "deck.pdf")
    assert key == f"chat-uploads/{ws_id}/{up_id}/deck.pdf"
    assert chat_upload_storage.is_chat_upload_key(key) is True

    # ASR transcription keys and upstream uploads/ keys must be rejected unconditionally
    asr_key = f"transcriptions/{ws_id}/{up_id}/recording.wav"
    assert chat_upload_storage.is_chat_upload_key(asr_key) is False
    assert chat_upload_storage.is_chat_upload_key("uploads/user1/file.bin") is False

    with patch.object(chat_upload_storage._store, "delete_object", autospec=True) as mock_del, patch.object(
        chat_upload_storage._store, "abort_multipart_upload", autospec=True
    ) as mock_abort:
        assert chat_upload_storage.delete_object(asr_key) is False
        assert chat_upload_storage.abort_multipart_upload(asr_key, "mp-1") is False
        mock_del.assert_not_called()
        mock_abort.assert_not_called()

    with pytest.raises(ValueError, match="Refusing non-chat-upload"):
        chat_upload_storage.open_multipart_upload(asr_key, content_type="audio/wav")

    with pytest.raises(ValueError, match="Refusing non-chat-upload"):
        chat_upload_storage.sign_download_url(asr_key)


def test_filename_sanitization_and_sandbox_path_containment() -> None:
    display, safe = svc.sanitize_chat_upload_filename("../../etc/passwd\x00")
    assert "/" not in safe and ".." not in safe
    assert safe == "passwd"

    _, dot_safe = svc.sanitize_chat_upload_filename(".wsfiles-stage-evil.sh")
    assert not dot_safe.startswith(".wsfiles-")

    uid = str(uuid.uuid4())
    rel_path = svc.build_sandbox_upload_rel_path(
        {"upload_id": uid, "filename": "../Quarterly Report (Q3)!!.xlsx", "safe_filename": ""}
    )
    assert rel_path.startswith("work/uploads/")
    assert ".." not in rel_path
    assert rel_path.endswith(f"_{uid.replace('-', '')[:8]}.xlsx")


# ---------------------------------------------------------------------------
# 2. Byte limits, 200 MiB boundary, part SHA-256 & HTTP router checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_boundaries_empty_200mib_and_200mib_plus_one(upload_env) -> None:
    user_id = "user-alpha"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {"workspace_id": ws_id, "user_id": user_id, "status": "running"}

    # 0-byte empty file rejected with 400
    with pytest.raises(HTTPException) as empty_exc:
        await svc.initiate_chat_upload(
            user_id=user_id,
            workspace_id=ws_id,
            filename="empty.csv",
            size_bytes=0,
            mime_type="text/csv",
        )
    assert empty_exc.value.status_code == 400

    # 200 MiB + 1 byte rejected with 413
    with pytest.raises(HTTPException) as over_exc:
        await svc.initiate_chat_upload(
            user_id=user_id,
            workspace_id=ws_id,
            filename="too_big.bin",
            size_bytes=svc.MAX_CHAT_UPLOAD_BYTES + 1,
            mime_type="application/octet-stream",
        )
    assert over_exc.value.status_code == 413

    # Exact 200 MiB (209_715_200 bytes) accepted: 25 parts of 8 MiB
    exact = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="exact_200mb.zip",
        size_bytes=svc.MAX_CHAT_UPLOAD_BYTES,
        mime_type="application/zip",
    )
    assert exact["size_bytes"] == 200 * 1024 * 1024
    assert exact["part_size"] == 8 * 1024 * 1024
    assert exact["part_count"] == 25
    assert exact["state"] == "uploading"


@pytest.mark.asyncio
async def test_part_actual_byte_count_and_checksum_enforcement(upload_env) -> None:
    user_id = "user-alpha"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {"workspace_id": ws_id, "user_id": user_id, "status": "running"}

    total_size = svc.CHAT_UPLOAD_PART_SIZE + 1024  # 2 parts: 8 MiB + 1024 B
    created = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="dataset.parquet",
        size_bytes=total_size,
        mime_type="application/octet-stream",
    )
    uid = created["upload_id"]

    # Non-final part shorter than 8 MiB is rejected with 400
    with pytest.raises(HTTPException) as short_exc:
        await svc.receive_chat_upload_part(
            upload_id=uid,
            part_number=1,
            user_id=user_id,
            stream=_bytes_stream(b"too-short"),
        )
    assert short_exc.value.status_code == 400

    # Final part exceeding expected 1024 bytes is rejected with 413 without buffering unboundedly
    with pytest.raises(HTTPException) as over_exc:
        await svc.receive_chat_upload_part(
            upload_id=uid,
            part_number=2,
            user_id=user_id,
            stream=_bytes_stream(b"x" * 2048),
        )
    assert over_exc.value.status_code == 413

    # Final part with mismatched X-Part-Sha256 is rejected with 400
    part2_bytes = b"z" * 1024
    with pytest.raises(HTTPException) as sha_exc:
        await svc.receive_chat_upload_part(
            upload_id=uid,
            part_number=2,
            user_id=user_id,
            stream=_bytes_stream(part2_bytes),
            expected_part_sha256="0" * 64,
        )
    assert sha_exc.value.status_code == 400
    assert upload_env["store"].open_uploads[(f"chat-uploads/{ws_id}/{uid}/dataset.parquet", "mp-1")] == {}


@pytest.mark.asyncio
async def test_http_router_end_to_end_with_home_resolution(upload_env) -> None:
    user_id = "user-cos-1"
    expected_home_id = get_home_workspace_id(user_id)

    app = FastAPI()
    app.include_router(chat_uploads_router)
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    payload = b"hello-large-upload-relay" * 500
    full_sha = hashlib.sha256(payload).hexdigest()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        init_res = await client.post(
            "/api/v1/dalton/chat-uploads",
            json={
                "workspace_id": "home",
                "filename": "home_brief.pdf",
                "size_bytes": len(payload),
                "mime_type": "application/pdf",
                "sha256": full_sha,
            },
        )
        assert init_res.status_code == 200
        body = init_res.json()
        assert body["workspace_id"] == expected_home_id
        upload_id = body["upload_id"]

        part_res = await client.put(
            f"/api/v1/dalton/chat-uploads/{upload_id}/parts/1",
            content=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Part-Sha256": full_sha,
            },
        )
        assert part_res.status_code == 200
        assert part_res.json()["sha256"] == full_sha

        comp_res = await client.post(
            f"/api/v1/dalton/chat-uploads/{upload_id}/complete",
            json={"sha256": full_sha},
        )
        assert comp_res.status_code == 200
        assert comp_res.json()["state"] == "ready"
        assert comp_res.json()["sha256"] == full_sha


# ---------------------------------------------------------------------------
# 3. Ownership, workspace isolation & inline 30/60 MiB budget preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ownership_workspace_isolation_and_inline_budget(upload_env) -> None:
    alice = "user-alice"
    bob = "user-bob"
    ws_a = str(uuid.uuid4())
    ws_b = str(uuid.uuid4())
    upload_env["workspaces"][ws_a] = {"workspace_id": ws_a, "user_id": alice, "status": "running"}
    upload_env["workspaces"][ws_b] = {"workspace_id": ws_b, "user_id": alice, "status": "running"}

    data = b"model-workbook-bytes" * 200
    created = await svc.initiate_chat_upload(
        user_id=alice,
        workspace_id=ws_a,
        filename="model.xlsx",
        size_bytes=len(data),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    uid = created["upload_id"]

    # Cross-user part upload & cancel must be 403
    with pytest.raises(HTTPException) as bob_part_exc:
        await svc.receive_chat_upload_part(
            upload_id=uid,
            part_number=1,
            user_id=bob,
            stream=_bytes_stream(data),
        )
    assert bob_part_exc.value.status_code == 403

    with pytest.raises(HTTPException) as bob_del_exc:
        await svc.cancel_chat_upload(upload_id=uid, user_id=bob)
    assert bob_del_exc.value.status_code == 403

    # Uploading state rejected at message send with 409
    with pytest.raises(HTTPException) as unready_exc:
        await svc.validate_and_claim_request_attachments(
            [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
            user_id=alice,
            workspace_id=ws_a,
        )
    assert unready_exc.value.status_code == 409

    # Finish upload as Alice in ws_a
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=1, user_id=alice, stream=_bytes_stream(data)
    )
    await svc.complete_chat_upload(upload_id=uid, user_id=alice)

    # Cross-user send reference rejected with 403
    upload_env["workspaces"][ws_a]["user_id"] = bob
    with pytest.raises(HTTPException) as cross_user_exc:
        await svc.validate_and_claim_request_attachments(
            [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
            user_id=bob,
            workspace_id=ws_a,
        )
    assert cross_user_exc.value.status_code == 403
    upload_env["workspaces"][ws_a]["user_id"] = alice

    # Cross-workspace send reference (ws_a upload sent into ws_b or Home) rejected with 403
    with pytest.raises(HTTPException) as cross_ws_exc:
        await svc.validate_and_claim_request_attachments(
            [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
            user_id=alice,
            workspace_id=ws_b,
        )
    assert cross_ws_exc.value.status_code == 403

    with pytest.raises(HTTPException) as cross_home_exc:
        await svc.validate_and_claim_request_attachments(
            [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
            user_id=alice,
            workspace_id="home",
        )
    assert cross_home_exc.value.status_code == 403

    # Inline 30 MiB per-file budget remains strictly enforced
    oversize_inline_b64 = "A" * (((30 * 1024 * 1024 + 12) * 4) // 3)
    with pytest.raises(HTTPException) as inline_exc:
        await svc.validate_and_claim_request_attachments(
            [
                MultimodalContext(
                    type="file",
                    data=f"data:application/octet-stream;base64,{oversize_inline_b64}",
                    description="inline.bin",
                )
            ],
            user_id=alice,
            workspace_id=ws_a,
        )
    assert inline_exc.value.status_code == 413

    # Valid send in ws_a stamps used_at, blocks DELETE (409), and permits idempotent /retry
    claimed_rows = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
        user_id=alice,
        workspace_id=ws_a,
    )
    assert len(claimed_rows) == 1
    assert claimed_rows[0]["used_at"] is not None

    with pytest.raises(HTTPException) as cancel_used_exc:
        await svc.cancel_chat_upload(upload_id=uid, user_id=alice)
    assert cancel_used_exc.value.status_code == 409

    retried_rows = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
        user_id=alice,
        workspace_id=ws_a,
    )
    assert len(retried_rows) == 1


# ---------------------------------------------------------------------------
# 4. Cross-worker completion race, missing-part retry & expired cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_parts_retry_and_cross_worker_complete_race(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "user-race"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {"workspace_id": ws_id, "user_id": user_id, "status": "running"}

    part1 = b"a" * svc.CHAT_UPLOAD_PART_SIZE
    part2 = b"b" * 4096
    created = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="two_parts.bin",
        size_bytes=len(part1) + len(part2),
    )
    uid = created["upload_id"]

    # Upload only part 1, then call complete -> 409 missing_parts=[2] and reopened to 'uploading'
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=1, user_id=user_id, stream=_bytes_stream(part1, chunk_size=1024 * 1024)
    )
    with pytest.raises(HTTPException) as missing_exc:
        await svc.complete_chat_upload(upload_id=uid, user_id=user_id)
    assert missing_exc.value.status_code == 409
    assert missing_exc.value.detail["code"] == "missing_parts"
    assert missing_exc.value.detail["missing_parts"] == [2]

    row_after_missing = await db_chat_uploads.get_upload(uid)
    assert row_after_missing is not None and row_after_missing["state"] == "uploading"

    # Retry part 2, then race two workers calling complete_chat_upload concurrently
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=2, user_id=user_id, stream=_bytes_stream(part2)
    )

    store_digest = upload_env["store"].compute_stored_object_digest

    def _slow_digest(object_key: str) -> tuple[int, str] | None:
        time.sleep(0.02)
        return store_digest(object_key)

    monkeypatch.setattr(
        chat_upload_storage,
        "compute_stored_object_digest",
        create_autospec(_REAL_COMPUTE_STORED_OBJECT_DIGEST, side_effect=_slow_digest),
    )

    outcomes = await asyncio.gather(
        svc.complete_chat_upload(upload_id=uid, user_id=user_id),
        svc.complete_chat_upload(upload_id=uid, user_id=user_id),
        return_exceptions=True,
    )
    ready_results = [r for r in outcomes if isinstance(r, dict) and r.get("state") == "ready"]
    assert len(ready_results) >= 1
    for other in outcomes:
        if isinstance(other, Exception):
            assert isinstance(other, HTTPException)
            assert other.status_code == 409
            assert other.detail["code"] == "completing_in_progress"

    # Subsequent idempotent complete returns ready immediately
    settled = await svc.complete_chat_upload(upload_id=uid, user_id=user_id)
    assert settled["state"] == "ready"
    assert settled["sha256"] == hashlib.sha256(part1 + part2).hexdigest()


@pytest.mark.asyncio
async def test_expired_and_cancelled_cleanup_preserves_active_used_files_beyond_7_days(
    upload_env,
) -> None:
    user_id = "user-clean"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
        "sandbox_id": "sbx-old-1",
        "files_restore_incomplete": False,
    }

    # 1. Cancelled in-progress upload cleans multipart state immediately
    up_cancel = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="cancel.bin", size_bytes=1024
    )
    await svc.receive_chat_upload_part(
        upload_id=up_cancel["upload_id"],
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(b"x" * 1024),
    )
    await svc.cancel_chat_upload(upload_id=up_cancel["upload_id"], user_id=user_id)
    assert len(upload_env["store"].open_uploads) == 0

    # 2. Abandoned incomplete upload (expires after 6h)
    up_abandoned = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="abandoned.bin", size_bytes=1024
    )
    await svc.receive_chat_upload_part(
        upload_id=up_abandoned["upload_id"],
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(b"a" * 1024),
    )

    # 3. Unused ready upload (expires after 24h) vs accepted/used upload (durable: expires_at=None)
    up_unused = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="stale.bin", size_bytes=512
    )
    await svc.receive_chat_upload_part(
        upload_id=up_unused["upload_id"],
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(b"u" * 512),
    )
    await svc.complete_chat_upload(upload_id=up_unused["upload_id"], user_id=user_id)

    up_used = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="kept_200m.bin", size_bytes=512
    )
    await svc.receive_chat_upload_part(
        upload_id=up_used["upload_id"],
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(b"k" * 512),
    )
    await svc.complete_chat_upload(upload_id=up_used["upload_id"], user_id=user_id)
    claimed = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=up_used["upload_id"], name="kept_200m.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
    )
    assert claimed[0]["used_at"] is not None
    assert claimed[0]["expires_at"] is None

    # Capture object keys before advancing +30 days (sweep cleans storage AND prunes cleaned rows >14d)
    cancel_key = upload_env["ledger"].rows[up_cancel["upload_id"]]["object_key"]
    abandoned_key = upload_env["ledger"].rows[up_abandoned["upload_id"]]["object_key"]
    stale_key = upload_env["ledger"].rows[up_unused["upload_id"]]["object_key"]
    kept_key = upload_env["ledger"].rows[up_used["upload_id"]]["object_key"]
    assert stale_key in upload_env["store"].objects
    assert kept_key in upload_env["store"].objects

    # Advance clock +30 days (well beyond 7 days): unused + abandoned are swept & pruned, used stays durable
    plus_30_days = datetime.now(timezone.utc) + timedelta(days=30)
    cleaned = await svc.sweep_expired_chat_uploads_once(limit=10, now=plus_30_days)
    assert cleaned == 2

    assert cancel_key not in upload_env["store"].objects
    assert abandoned_key not in upload_env["store"].objects
    assert stale_key not in upload_env["store"].objects
    assert len(upload_env["store"].open_uploads) == 0
    assert up_cancel["upload_id"] not in upload_env["ledger"].rows
    assert up_abandoned["upload_id"] not in upload_env["ledger"].rows
    assert up_unused["upload_id"] not in upload_env["ledger"].rows
    assert up_used["upload_id"] in upload_env["ledger"].rows
    assert kept_key in upload_env["store"].objects
    assert upload_env["ledger"].rows[up_used["upload_id"]]["cleaned_at"] is None

    # Even at +30 days, /retry re-claiming the used attachment still succeeds
    reclaimed = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=up_used["upload_id"], name="kept_200m.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
    )
    assert len(reclaimed) == 1
    assert kept_key in upload_env["store"].objects


# ---------------------------------------------------------------------------
# 5. Sandbox materialization, cold recovery, permissions & lifecycle cleanup
# ---------------------------------------------------------------------------


class _FakeSandboxConfigFs:
    working_directory = "/home/workspace"


class _FakeSandboxConfig:
    filesystem = _FakeSandboxConfigFs()


class _FakeSandboxProvider:
    direct_blob_transfer = True


class _FakeExecResult:
    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code


class _FakeRuntime:
    @staticmethod
    def exec(*args: Any, **kwargs: Any) -> None:
        return None


class _FakeSandbox:
    config = _FakeSandboxConfig()
    provider = _FakeSandboxProvider()
    runtime = _FakeRuntime()

    def __init__(self, sandbox_id: str = "sbx-1") -> None:
        self.sandbox_id = sandbox_id
        self.locked_paths: list[str] = []
        self.files: dict[str, bytes] = {}

    def workspace(self, project: ProjectContext | None = None) -> WorkspaceLayout:
        dir_name = (project.dir_name if project is not None else None) or ""
        return WorkspaceLayout(root="/home/workspace", dir_name=dir_name)

    def normalize_path(self, path: str, project: ProjectContext | None = None) -> str:
        if path == "/home/workspace" or path.startswith("/home/workspace/"):
            return path
        rel = path.lstrip("/")
        if project and project.dir_name:
            return f"/home/workspace/{project.dir_name}/{rel}"
        return f"/home/workspace/{rel}"

    def validate_path(self, filepath: str, project: ProjectContext | None = None) -> bool:
        target = (
            filepath
            if filepath.startswith("/home/workspace/")
            else self.normalize_path(filepath, project)
        )
        return target.startswith("/home/workspace/") and ".." not in target.split("/")

    @asynccontextmanager
    async def path_write_lock(self, normalized_path: str):
        self.locked_paths.append(normalized_path)
        yield

    async def adownload_file_bytes(self, filepath: str) -> bytes | None:
        return self.files.get(filepath)

    async def aupload_file_bytes(
        self, filepath: str, content: bytes, *, verify: bool = False
    ) -> bool:
        if not self.validate_path(filepath):
            return False
        self.files[filepath] = bytes(content)
        return True

    async def _runtime_call(self, fn: Any, *args: Any, **kwargs: Any) -> _FakeExecResult:
        cmd = str(args[0]) if args else ""
        if "for p in " in cmd:
            # Missing-path probe from _probe_missing_sandbox_paths
            import shlex as _shlex

            prefix = cmd.split("for p in ", 1)[1].split("; do ", 1)[0]
            missing = [p for p in _shlex.split(prefix) if p not in self.files]
            return _FakeExecResult(
                stdout="\n".join(missing) + ("\n" if missing else ""),
                exit_code=0,
            )
        # Simulate plenty of free disk space (20 GiB free in 1K-blocks df output)
        df_out = "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 52428800 1048576 20971520 5% /home/workspace\n"
        return _FakeExecResult(stdout=df_out, exit_code=0)


class _FakeSession:
    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox


@pytest.mark.asyncio
async def test_attach_request_files_materializes_via_pull_direct_without_base64_in_messages(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "user-mat"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {"workspace_id": ws_id, "user_id": user_id, "status": "running"}

    raw_bytes = b"SECRET_BINARY_CONTENT_NEVER_IN_PROMPT_" * 2048
    raw_b64_Sample = base64.b64encode(raw_bytes[:48]).decode("ascii")
    created = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="valuation_model.xlsx",
        size_bytes=len(raw_bytes),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    uid = created["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=1, user_id=user_id, stream=_bytes_stream(raw_bytes)
    )
    completed = await svc.complete_chat_upload(upload_id=uid, user_id=user_id)

    pulled_entries: list[dict[str, Any]] = []

    async def _fake_pull_direct(
        sandbox: Any, entries: list[dict[str, Any]], *, layout: WorkspaceLayout
    ) -> dict[str, dict[str, Any]]:
        pulled_entries.extend(entries)
        return {entries[0]["path"]: {"status": "ok"}}

    monkeypatch.setattr(
        svc,
        "pull_direct",
        create_autospec(svc.pull_direct, side_effect=_fake_pull_direct),
    )

    sandbox = _FakeSandbox()
    session = _FakeSession(sandbox)
    project = ProjectContext(workspace_id=ws_id, dir_name="NVDA")
    req = ChatRequest(
        messages=[{"role": "user", "content": "Audit the DCF sheet"}],
        additional_context=[
            ChatUploadContext(
                upload_id=uid,
                name="valuation_model.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size=len(raw_bytes),
            )
        ],
    )
    messages = [{"role": "user", "content": "Audit the DCF sheet"}]

    out_messages = await attach_request_files(
        messages,
        req,
        session,
        effective_model="claude-sonnet-4-5",
        config=type("Cfg", (), {"input_modalities": ["text", "image"], "llm_client": None})(),
        project=project,
        user_id=user_id,
        workspace_id=ws_id,
    )

    assert len(pulled_entries) == 1
    assert pulled_entries[0]["size"] == len(raw_bytes)
    assert pulled_entries[0]["sha256"] == completed["sha256"]
    assert pulled_entries[0]["path"].startswith("work/uploads/valuation_model_")
    assert sandbox.locked_paths == [f"/home/workspace/NVDA/{pulled_entries[0]['path']}"]

    rendered = str(out_messages[0]["content"])
    assert pulled_entries[0]["path"] in rendered
    assert "<system-reminder>" in rendered
    assert "SECRET_BINARY_CONTENT_NEVER_IN_PROMPT_" not in rendered
    assert raw_b64_Sample not in rendered

    # Verify history metadata & /retry reconstruction carry zero base64 bytes
    query_meta: dict[str, Any] = {}
    serialize_context_metadata(req, query_meta, "Audit the DCF sheet")
    serialized_ctx = query_meta.get("additional_context")
    assert serialized_ctx == [
        {
            "type": "chat_upload",
            "upload_id": uid,
            "name": "valuation_model.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": len(raw_bytes),
        }
    ]
    att_meta = await svc.build_chat_upload_history_metadata(
        req.additional_context, user_id=user_id, workspace_id=ws_id
    )
    assert att_meta == [
        {
            "name": "valuation_model.xlsx",
            "type": "file",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": len(raw_bytes),
            "status": "uploaded",
            "upload_id": uid,
        }
    ]

    async def _fake_get_query_at_turn(thread_id: str, turn_index: int) -> dict[str, Any]:
        return {
            "content": "Audit the DCF sheet",
            "type": "user",
            "metadata": {
                "additional_context": serialized_ctx,
                "attachments": att_meta,
            },
        }

    with patch(
        "src.server.database.conversation.queries.get_query_at_turn",
        autospec=True,
        side_effect=_fake_get_query_at_turn,
    ):
        rerun = await input_rerun_request(
            "thread-mat-1",
            {"turn_index": 0, "metadata": {}},
            PendingInput(checkpoint_id=None, context=True),
            workspace_id=ws_id,
            body=None,
        )
    assert rerun.additional_context is not None
    assert len(rerun.additional_context) == 1
    assert getattr(rerun.additional_context[0], "upload_id", None) == uid


@pytest.mark.asyncio
async def test_cold_workspace_restore_and_future_turn_rematerialize_200mib_beyond_7_days(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When workspace_files is empty (>100 MiB skipped by sync_to_db), maybe_restore still
    rematerializes retained 200 MiB chat uploads on a recreated cold sandbox even +30 days later."""
    from src.server.services.persistence import file as persistence_file
    from src.server.services.persistence import restore as persistence_restore

    user_id = "user-cold-restore"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
        "sandbox_id": "sbx-old-1",
        "files_restore_incomplete": False,
    }

    # Create a ready + used 200 MiB upload record (without allocating 200 MiB in unit-test RAM)
    exact_200_mib = svc.MAX_CHAT_UPLOAD_BYTES
    uid = str(uuid.uuid4())
    obj_key = chat_upload_storage.build_chat_upload_key(ws_id, uid, "bundle_200m.zip")
    fake_sha = "d4" * 32
    now = datetime.now(timezone.utc)
    upload_env["store"].objects[obj_key] = b"placeholder-streamed-via-pull-direct"
    upload_env["ledger"].rows[uid] = {
        "upload_id": uid,
        "user_id": user_id,
        "workspace_id": ws_id,
        "filename": "bundle_200m.zip",
        "safe_filename": "bundle_200m.zip",
        "mime_type": "application/zip",
        "size_bytes": exact_200_mib,
        "part_size": svc.CHAT_UPLOAD_PART_SIZE,
        "part_count": 25,
        "object_key": obj_key,
        "store_upload_id": "mp-cold-1",
        "expected_sha256": fake_sha,
        "actual_sha256": fake_sha,
        "uploaded_parts": {},
        "state": "ready",
        "error": None,
        "lease_until": None,
        "expires_at": None,
        "used_at": now,
        "cleaned_at": None,
        "created_at": now,
        "updated_at": now,
    }

    # Advance sweeper +30 days: retained 200 MiB object must remain untouched
    swept = await svc.sweep_expired_chat_uploads_once(
        limit=10, now=now + timedelta(days=30)
    )
    assert swept == 0
    assert obj_key in upload_env["store"].objects

    # Offline stopped-workspace file tree includes the 200 MiB retained upload even when workspace_files is empty
    monkeypatch.setattr(
        persistence_file,
        "get_files_for_workspace",
        create_autospec(persistence_file.get_files_for_workspace, return_value=[]),
    )
    tree = await persistence_file.get_file_tree(ws_id)
    expected_rel = svc.build_sandbox_upload_rel_path(upload_env["ledger"].rows[uid])
    assert len(tree) == 1
    assert tree[0]["path"] == expected_rel
    assert tree[0]["size"] == exact_200_mib

    # Cold recreated sandbox: no .file_sync_marker and 0 rows in workspace_files
    cold_sandbox = _FakeSandbox(sandbox_id="sbx-cold-2")
    layout = WorkspaceLayout(root="/home/workspace", dir_name="AlphaProj")
    pulled_paths: list[str] = []

    async def _fake_pull_direct(
        sandbox: Any, entries: list[dict[str, Any]], *, layout: WorkspaceLayout
    ) -> dict[str, dict[str, Any]]:
        for entry in entries:
            pulled_paths.append(entry["path"])
            sandbox.files[f"{layout.workspace}/{entry['path']}"] = b"restored-header"
        return {entries[0]["path"]: {"status": "ok"}}

    monkeypatch.setattr(
        svc,
        "pull_direct",
        create_autospec(svc.pull_direct, side_effect=_fake_pull_direct),
    )
    monkeypatch.setattr(
        persistence_restore,
        "get_files_for_workspace",
        create_autospec(persistence_restore.get_files_for_workspace, return_value=[]),
    )
    monkeypatch.setattr(
        persistence_restore,
        "_kept_notes",
        create_autospec(persistence_restore._kept_notes, return_value=[]),
    )
    async def _fake_set_files_restore_incomplete(
        workspace_id: str,
        incomplete: bool,
        *,
        conn: Any = None,
        sandbox_id: Any = persistence_restore.ANY_SANDBOX,
    ) -> bool:
        ws_row = upload_env["workspaces"].get(workspace_id)
        if ws_row is None:
            return False
        if (
            sandbox_id is not persistence_restore.ANY_SANDBOX
            and ws_row.get("sandbox_id") != sandbox_id
        ):
            return False
        ws_row["files_restore_incomplete"] = bool(incomplete)
        return True

    @asynccontextmanager
    async def _fake_hold_skill_sync(workspace_id: str, *, wait_s: float):
        assert workspace_id == ws_id
        assert wait_s > 0
        yield "fake-skill-sync-session"

    @asynccontextmanager
    async def _fake_sync_lock(
        workspace_id: str,
        *,
        wait: str = persistence_restore.SYNC_LOCK_WAIT,
        conn: Any = None,
    ):
        assert workspace_id == ws_id
        yield conn or "fake-sync-session"

    monkeypatch.setattr(
        persistence_restore,
        "set_files_restore_incomplete",
        create_autospec(
            persistence_restore.set_files_restore_incomplete,
            side_effect=_fake_set_files_restore_incomplete,
        ),
    )
    monkeypatch.setattr(
        persistence_restore,
        "hold_workspace_skill_sync",
        create_autospec(
            persistence_restore.hold_workspace_skill_sync,
            side_effect=_fake_hold_skill_sync,
        ),
    )
    monkeypatch.setattr(
        persistence_restore,
        "workspace_sync_lock",
        create_autospec(persistence_restore.workspace_sync_lock, side_effect=_fake_sync_lock),
    )

    # Before the recreated sandbox identity is bound on the workspace, CAS rejects restore
    with pytest.raises(persistence_restore.RestoreIdentityLost):
        await persistence_restore.maybe_restore(ws_id, cold_sandbox, layout=layout)

    # Bind the recreated sandbox identity (as _bind_machine_identity / try_bind_computer_provider_ref does)
    upload_env["workspaces"][ws_id]["sandbox_id"] = cold_sandbox.sandbox_id

    restored_ok = await persistence_restore.maybe_restore(
        ws_id, cold_sandbox, layout=layout
    )
    assert restored_ok is True
    assert pulled_paths == [expected_rel]
    assert f"/home/workspace/AlphaProj/{expected_rel}" in cold_sandbox.files
    assert f"/home/workspace/AlphaProj/.file_sync_marker" in cold_sandbox.files
    assert upload_env["workspaces"][ws_id]["files_restore_incomplete"] is False

    # Subsequent future turn on warm sandbox sees the file already on disk and pulls 0 times
    pulled_paths.clear()
    turn_check = await svc.ensure_workspace_chat_uploads_present(
        ws_id,
        cold_sandbox,
        project=ProjectContext(workspace_id=ws_id, dir_name="AlphaProj"),
    )
    assert turn_check == {"restored": 0, "errors": 0}
    assert pulled_paths == []


@pytest.mark.asyncio
async def test_recovery_rejects_unauthorized_owner_key_mismatch_and_deleted_workspace(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = "user-alice-rec"
    mallory = "user-mallory-rec"
    alice_home = get_home_workspace_id(alice)
    ws_alice = str(uuid.uuid4())
    ws_other = str(uuid.uuid4())
    upload_env["workspaces"][alice_home] = {
        "workspace_id": alice_home,
        "user_id": alice,
        "status": "running",
    }
    upload_env["workspaces"][ws_alice] = {
        "workspace_id": ws_alice,
        "user_id": alice,
        "status": "running",
    }

    now = datetime.now(timezone.utc)
    # 1. Forged cross-user row inside Alice's Home workspace
    bad_home_uid = str(uuid.uuid4())
    upload_env["ledger"].rows[bad_home_uid] = {
        "upload_id": bad_home_uid,
        "user_id": mallory,
        "workspace_id": alice_home,
        "filename": "forged_home.bin",
        "safe_filename": "forged_home.bin",
        "mime_type": "application/octet-stream",
        "size_bytes": 1024,
        "part_size": svc.CHAT_UPLOAD_PART_SIZE,
        "part_count": 1,
        "object_key": chat_upload_storage.build_chat_upload_key(
            alice_home, bad_home_uid, "forged_home.bin"
        ),
        "store_upload_id": "mp-bad-1",
        "expected_sha256": "a" * 64,
        "actual_sha256": "a" * 64,
        "uploaded_parts": {},
        "state": "ready",
        "error": None,
        "lease_until": None,
        "expires_at": None,
        "used_at": now,
        "cleaned_at": None,
        "created_at": now,
        "updated_at": now,
    }

    # 2. Tampered object_key pointing into another workspace's prefix + unauthorized user row
    bad_key_uid = str(uuid.uuid4())
    bad_user_uid = str(uuid.uuid4())
    upload_env["ledger"].rows[bad_key_uid] = {
        **upload_env["ledger"].rows[bad_home_uid],
        "upload_id": bad_key_uid,
        "user_id": alice,
        "workspace_id": ws_alice,
        "object_key": chat_upload_storage.build_chat_upload_key(
            ws_other, bad_key_uid, "wrong_ws.bin"
        ),
    }
    upload_env["ledger"].rows[bad_user_uid] = {
        **upload_env["ledger"].rows[bad_home_uid],
        "upload_id": bad_user_uid,
        "user_id": mallory,
        "workspace_id": ws_alice,
        "object_key": chat_upload_storage.build_chat_upload_key(
            ws_alice, bad_user_uid, "mallory.bin"
        ),
    }

    monkeypatch.setattr(
        svc,
        "is_dalton_supabase_auth_enabled",
        create_autospec(svc.is_dalton_supabase_auth_enabled, return_value=False),
    )
    pull_spy = create_autospec(svc.pull_direct, return_value={})
    monkeypatch.setattr(svc, "pull_direct", pull_spy)

    sbx = _FakeSandbox()
    layout = WorkspaceLayout(root="/home/workspace", dir_name="Home")
    res_home = await svc.rematerialize_workspace_chat_uploads(
        alice_home, sbx, layout=layout
    )
    assert res_home == {"restored": 0, "errors": 0}

    res_ws = await svc.rematerialize_workspace_chat_uploads(
        ws_alice, sbx, layout=WorkspaceLayout(root="/home/workspace", dir_name="Proj")
    )
    assert res_ws == {"restored": 0, "errors": 0}
    pull_spy.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_file_delete_and_workspace_delete_cleanup_with_permission_checks(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = "user-owner-del"
    intruder = "user-intruder-del"
    ws_file_del = str(uuid.uuid4())
    ws_tombstone = str(uuid.uuid4())
    upload_env["workspaces"][ws_file_del] = {
        "workspace_id": ws_file_del,
        "user_id": owner,
        "status": "running",
    }
    upload_env["workspaces"][ws_tombstone] = {
        "workspace_id": ws_tombstone,
        "user_id": owner,
        "status": "running",
    }

    # Create and claim one attachment in each workspace
    created_1 = await svc.initiate_chat_upload(
        user_id=owner, workspace_id=ws_file_del, filename="drop_me.zip", size_bytes=2048
    )
    uid_1 = created_1["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_1, part_number=1, user_id=owner, stream=_bytes_stream(b"1" * 2048)
    )
    await svc.complete_chat_upload(upload_id=uid_1, user_id=owner)
    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid_1, name="drop_me.zip", size=2048)],
        user_id=owner,
        workspace_id=ws_file_del,
    )
    row_1 = upload_env["ledger"].rows[uid_1]
    rel_1 = svc.build_sandbox_upload_rel_path(row_1)
    assert row_1["object_key"] in upload_env["store"].objects

    # Unauthorized user cannot retire owner's deleted file
    with pytest.raises(HTTPException) as unauth_del:
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_file_del,
            caller_user_id=intruder,
            deleted_client_paths=[rel_1],
        )
    assert unauth_del.value.status_code == 403
    assert row_1["object_key"] in upload_env["store"].objects

    # Owner explicitly deletes work/uploads/drop_me_<id8>.zip -> object removed & never resurrected
    retired_n = await svc.retire_deleted_workspace_chat_uploads(
        workspace_id=ws_file_del,
        caller_user_id=owner,
        deleted_client_paths=[rel_1],
    )
    assert retired_n == 1
    assert row_1["object_key"] not in upload_env["store"].objects
    assert upload_env["ledger"].rows[uid_1]["cleaned_at"] is not None

    pull_spy = create_autospec(svc.pull_direct, return_value={})
    monkeypatch.setattr(svc, "pull_direct", pull_spy)
    remat_after_delete = await svc.rematerialize_workspace_chat_uploads(
        ws_file_del,
        _FakeSandbox(),
        layout=WorkspaceLayout(root="/home/workspace", dir_name="DropWS"),
    )
    assert remat_after_delete == {"restored": 0, "errors": 0}
    pull_spy.assert_not_called()

    # Workspace deletion cleanup: refused while workspace is still 'running' or for wrong owner
    created_2 = await svc.initiate_chat_upload(
        user_id=owner, workspace_id=ws_tombstone, filename="ws_bound.zip", size_bytes=2048
    )
    uid_2 = created_2["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_2, part_number=1, user_id=owner, stream=_bytes_stream(b"2" * 2048)
    )
    await svc.complete_chat_upload(upload_id=uid_2, user_id=owner)
    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid_2, name="ws_bound.zip", size=2048)],
        user_id=owner,
        workspace_id=ws_tombstone,
    )
    row_2 = upload_env["ledger"].rows[uid_2]

    with pytest.raises(HTTPException) as live_ws_exc:
        await svc.cleanup_deleted_workspace_chat_uploads(ws_tombstone, caller_user_id=owner)
    assert live_ws_exc.value.status_code == 409
    assert row_2["object_key"] in upload_env["store"].objects

    upload_env["workspaces"][ws_tombstone]["status"] = "deleted"
    with pytest.raises(HTTPException) as wrong_owner_exc:
        await svc.cleanup_deleted_workspace_chat_uploads(
            ws_tombstone, caller_user_id=intruder
        )
    assert wrong_owner_exc.value.status_code == 403
    assert row_2["object_key"] in upload_env["store"].objects

    purged_n = await svc.cleanup_deleted_workspace_chat_uploads(
        ws_tombstone, caller_user_id=owner
    )
    assert purged_n == 1
    assert row_2["object_key"] not in upload_env["store"].objects
    assert upload_env["ledger"].rows[uid_2]["cleaned_at"] is not None


@pytest.mark.asyncio
async def test_router_handle_api_exceptions_logger_and_real_sandbox_signatures(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify @handle_api_exceptions(..., logger) logs and maps unexpected errors to 500,
    and _FakeSandbox method signatures match real PTCSandbox parameters."""
    for method_name in (
        "workspace",
        "normalize_path",
        "validate_path",
        "path_write_lock",
        "adownload_file_bytes",
        "aupload_file_bytes",
    ):
        real_params = [
            (p.name, p.kind)
            for p in inspect.signature(getattr(PTCSandbox, method_name)).parameters.values()
        ]
        fake_params = [
            (p.name, p.kind)
            for p in inspect.signature(getattr(_FakeSandbox, method_name)).parameters.values()
        ]
        assert fake_params == real_params, f"Signature mismatch on {method_name}: {fake_params} != {real_params}"

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "user-err"
    app.include_router(chat_uploads_router)

    monkeypatch.setattr(
        chat_uploads_app,
        "get_chat_upload_status",
        create_autospec(
            chat_uploads_app.get_chat_upload_status,
            side_effect=RuntimeError("simulated storage failure"),
        ),
    )
    with patch.object(chat_uploads_app.logger, "exception") as log_exc:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/dalton/chat-uploads/{uuid.uuid4()}")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to get chat upload status"
    log_exc.assert_called_once()

