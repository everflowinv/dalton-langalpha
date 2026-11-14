"""Unit tests for Dalton chat composer large-file uploads (<= 200 MiB) and sandbox materialization."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import os
import threading
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
_REAL_COMPLETE_MULTIPART_UPLOAD = chat_upload_storage.complete_multipart_upload
_REAL_UPLOAD_PART_BYTES = chat_upload_storage.upload_part_bytes
_REAL_PULL_DIRECT = svc.pull_direct
_REAL_CLAIM_CLEANUP_BATCH = db_chat_uploads.claim_cleanup_batch
_REAL_IS_UPLOAD_RETAINED_FOR_RECOVERY = db_chat_uploads.is_upload_retained_for_recovery


# ---------------------------------------------------------------------------
# In-memory guarded DB & MinIO store fakes (multi-worker CAS simulation)
# ---------------------------------------------------------------------------


class InMemoryChatUploadLedger:
    """Thread/task-safe in-memory state machine mirroring ``dalton_chat_file_uploads`` SQL guards."""

    def __init__(self, workspaces: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.workspaces = workspaces if workspaces is not None else {}
        self.accepted_request_keys: set[str] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _has_live_claim(row: dict[str, Any], now: datetime) -> bool:
        claims = row.get("pending_claims") or {}
        for raw_until in claims.values():
            try:
                parsed = datetime.fromisoformat(str(raw_until).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed > now:
                return True
        return False

    @staticmethod
    def _pruned_claims(
        row: dict[str, Any], now: datetime, *, drop_key: str | None = None
    ) -> dict[str, str]:
        kept: dict[str, str] = {}
        for k, raw_until in (row.get("pending_claims") or {}).items():
            if drop_key is not None and k == drop_key:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw_until).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed > now:
                kept[k] = str(raw_until)
        return kept

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
                and r["cleaned_at"] is None
                and (
                    (
                        r["state"] in ("uploading", "completing")
                        and r["expires_at"] is not None
                        and r["expires_at"] > now
                    )
                    or (
                        r["state"] == "ready"
                        and r["used_at"] is None
                        and (
                            (r["expires_at"] is not None and r["expires_at"] > now)
                            or self._has_live_claim(r, now)
                        )
                    )
                )
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
                "pending_claims": {},
                "state": "uploading",
                "error": None,
                "claim_token": None,
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
        claim_token: str | None = None,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        token = str(claim_token).strip() if claim_token else str(uuid.uuid4())
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["cleaned_at"] is not None
                or row["expires_at"] is None
                or row["expires_at"] <= now
            ):
                return None
            can_claim = row["state"] == "uploading" or (
                row["state"] == "completing"
                and (row["lease_until"] is None or row["lease_until"] < now)
            )
            if not can_claim:
                return None
            row["state"] = "completing"
            row["claim_token"] = token
            row["lease_until"] = now + lease
            if row["expected_sha256"] is None and expected_sha256:
                row["expected_sha256"] = expected_sha256
            row["updated_at"] = now
            return dict(row)

    async def reopen_uploading(
        self,
        upload_id: str,
        *,
        claim_token: str,
    ) -> dict[str, Any] | None:
        token = str(claim_token or "").strip()
        if not token:
            return None
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["state"] != "completing"
                or row.get("claim_token") != token
                or row["cleaned_at"] is not None
            ):
                return None
            row["state"] = "uploading"
            row["claim_token"] = None
            row["lease_until"] = None
            row["updated_at"] = now
            return dict(row)

    async def mark_ready(
        self,
        upload_id: str,
        *,
        claim_token: str,
        actual_sha256: str,
        ready_ttl: timedelta,
    ) -> dict[str, Any] | None:
        token = str(claim_token or "").strip()
        if not token:
            return None
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["state"] != "completing"
                or row.get("claim_token") != token
                or row["cleaned_at"] is not None
            ):
                return None
            row["state"] = "ready"
            row["actual_sha256"] = actual_sha256
            row["claim_token"] = None
            row["lease_until"] = None
            row["error"] = None
            row["expires_at"] = max(row["expires_at"] or now, now + ready_ttl)
            row["updated_at"] = now
            return dict(row)

    async def mark_failed(
        self,
        upload_id: str,
        *,
        claim_token: str,
        error: str,
    ) -> dict[str, Any] | None:
        token = str(claim_token or "").strip()
        if not token:
            return None
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["state"] != "completing"
                or row.get("claim_token") != token
            ):
                return None
            row["state"] = "failed"
            row["error"] = error[:500]
            row["claim_token"] = None
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
                row["state"] == "ready"
                and row["used_at"] is None
                and not self._has_live_claim(row, now)
            )
            if not allowed:
                return None
            had_completing = row["state"] == "completing"
            row["state"] = "aborted"
            row["claim_token"] = None
            row["lease_until"] = None
            row["pending_claims"] = {}
            row["cleaned_at"] = None
            row["expires_at"] = min(row["expires_at"] or now, now)
            row["updated_at"] = now
            return {**row, "had_completing": had_completing}

    async def reopen_terminal_cleanup(self, upload_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if not row or row["state"] not in ("aborted", "failed"):
                return None
            row["cleaned_at"] = None
            row["claim_token"] = None
            row["lease_until"] = None
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
            if (
                row["used_at"] is None
                and (row["expires_at"] is None or row["expires_at"] <= eff_now)
                and not self._has_live_claim(row, eff_now)
            ):
                return None
            row["used_at"] = row["used_at"] or eff_now
            row["expires_at"] = None
            row["pending_claims"] = self._pruned_claims(row, eff_now)
            row["updated_at"] = eff_now
            return dict(row)

    async def claim_provisional_batch(
        self,
        upload_ids: Sequence[str],
        *,
        user_id: str,
        workspace_id: str,
        claim_key: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]] | None:
        unique_ids = [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
        if not unique_ids:
            return {}
        key = str(claim_key or "").strip()
        if not key:
            return None
        eff_now = now or datetime.now(timezone.utc)
        async with self._lock:
            for uid in unique_ids:
                row = self.rows.get(uid)
                if (
                    not row
                    or row["user_id"] != user_id
                    or row["workspace_id"] != workspace_id
                    or row["state"] != "ready"
                    or row["cleaned_at"] is not None
                ):
                    return None
                if (
                    row["used_at"] is None
                    and (row["expires_at"] is None or row["expires_at"] <= eff_now)
                    and not self._has_live_claim(row, eff_now)
                ):
                    return None
            out: dict[str, dict[str, Any]] = {}
            until_iso = (eff_now + ttl).isoformat()
            for uid in unique_ids:
                row = self.rows[uid]
                claims = self._pruned_claims(row, eff_now, drop_key=key)
                claims[key] = until_iso
                row["pending_claims"] = claims
                row["updated_at"] = eff_now
                out[uid] = dict(row)
            return out

    async def confirm_provisional_claims(
        self,
        upload_ids: Sequence[str],
        *,
        user_id: str,
        workspace_id: str,
        claim_key: str | None = None,
        conn: Any = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]] | None:
        del conn
        unique_ids = [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
        if not unique_ids:
            return {}
        clean_key = str(claim_key).strip() if claim_key else None
        eff_now = now or datetime.now(timezone.utc)
        async with self._lock:
            for uid in unique_ids:
                row = self.rows.get(uid)
                if (
                    not row
                    or row["user_id"] != user_id
                    or row["workspace_id"] != workspace_id
                    or row["state"] != "ready"
                    or row["cleaned_at"] is not None
                ):
                    return None
                if (
                    row["used_at"] is None
                    and (row["expires_at"] is None or row["expires_at"] <= eff_now)
                    and not self._has_live_claim(row, eff_now)
                ):
                    return None
            out: dict[str, dict[str, Any]] = {}
            for uid in unique_ids:
                row = self.rows[uid]
                row["used_at"] = row["used_at"] or eff_now
                row["expires_at"] = None
                row["pending_claims"] = self._pruned_claims(
                    row, eff_now, drop_key=clean_key
                )
                row["updated_at"] = eff_now
                out[uid] = dict(row)
            return out

    async def release_provisional_claims(
        self,
        *,
        user_id: str,
        claim_key: str,
        request_key: str | None = None,
        upload_ids: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> int:
        del request_key
        key = str(claim_key or "").strip()
        if not key:
            return 0
        wanted = (
            {str(uid) for uid in upload_ids if uid}
            if upload_ids is not None
            else None
        )
        if wanted is not None and not wanted:
            return 0
        eff_now = now or datetime.now(timezone.utc)
        async with self._lock:
            matched = [
                row
                for uid, row in self.rows.items()
                if row["user_id"] == user_id
                and (wanted is None or uid in wanted)
                and key in (row.get("pending_claims") or {})
            ]
            if not matched:
                return 0
            for row in matched:
                row["pending_claims"] = self._pruned_claims(
                    row, eff_now, drop_key=key
                )
                row["updated_at"] = eff_now
            return len(matched)

    async def has_retained_workspace_uploads(
        self, workspace_id: str, *, conn: Any = None
    ) -> bool:
        return len(await self.list_retained_workspace_uploads(workspace_id, conn=conn)) > 0

    async def is_upload_retained_for_recovery(
        self,
        upload_id: str,
        workspace_id: str,
        *,
        conn: Any = None,
    ) -> bool:
        del conn
        async with self._lock:
            row = self.rows.get(upload_id)
            return bool(
                row
                and row["workspace_id"] == workspace_id
                and row["state"] == "ready"
                and row["used_at"] is not None
                and row["cleaned_at"] is None
                and (row.get("error") or "") != "explicit_delete_pending"
            )

    async def list_retained_workspace_uploads(
        self,
        workspace_id: str,
        *,
        include_delete_pending: bool = False,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        del conn
        async with self._lock:
            rows = [
                dict(r)
                for r in self.rows.values()
                if r["workspace_id"] == workspace_id
                and r["state"] == "ready"
                and r["used_at"] is not None
                and r["cleaned_at"] is None
                and (
                    include_delete_pending
                    or (r.get("error") or "") != "explicit_delete_pending"
                )
            ]
        rows.sort(key=lambda r: (r["created_at"], r["upload_id"]))
        return rows

    async def mark_uploads_delete_pending(
        self, upload_ids: Sequence[str], *, workspace_id: str
    ) -> list[str]:
        now = datetime.now(timezone.utc)
        wanted = [str(u) for u in dict.fromkeys(upload_ids) if u]
        marked: list[str] = []
        async with self._lock:
            for uid in wanted:
                row = self.rows.get(uid)
                if (
                    row
                    and row["workspace_id"] == workspace_id
                    and row["state"] == "ready"
                    and row["used_at"] is not None
                    and row["cleaned_at"] is None
                ):
                    row["error"] = "explicit_delete_pending"
                    row["updated_at"] = now
                    marked.append(uid)
        return marked

    async def clear_uploads_delete_pending(
        self, upload_ids: Sequence[str], *, workspace_id: str
    ) -> int:
        now = datetime.now(timezone.utc)
        wanted = [str(u) for u in dict.fromkeys(upload_ids) if u]
        cleared = 0
        async with self._lock:
            for uid in wanted:
                row = self.rows.get(uid)
                if (
                    row
                    and row["workspace_id"] == workspace_id
                    and row["state"] == "ready"
                    and row.get("error") == "explicit_delete_pending"
                ):
                    row["error"] = None
                    row["updated_at"] = now
                    cleared += 1
        return cleared

    async def retire_uploads_by_ids(
        self, upload_ids: Sequence[str], *, workspace_id: str
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        wanted = {str(u) for u in upload_ids if u}
        retired: list[dict[str, Any]] = []
        async with self._lock:
            for uid in wanted:
                row = self.rows.get(uid)
                if row and row["workspace_id"] == workspace_id and row["cleaned_at"] is None:
                    had_completing = row["state"] == "completing"
                    row["state"] = "aborted"
                    row["claim_token"] = None
                    row["lease_until"] = None
                    row["pending_claims"] = {}
                    row["cleaned_at"] = None
                    row["expires_at"] = now
                    row["updated_at"] = now
                    retired.append({**row, "had_completing": had_completing})
        return retired

    async def retire_workspace_uploads(self, workspace_id: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        retired: list[dict[str, Any]] = []
        async with self._lock:
            for row in self.rows.values():
                if row["workspace_id"] == workspace_id and row["cleaned_at"] is None:
                    had_completing = row["state"] == "completing"
                    row["state"] = "aborted"
                    row["claim_token"] = None
                    row["lease_until"] = None
                    row["pending_claims"] = {}
                    row["cleaned_at"] = None
                    row["expires_at"] = now
                    row["updated_at"] = now
                    retired.append({**row, "had_completing": had_completing})
        return retired

    async def claim_cleanup_batch(
        self,
        *,
        limit: int = 25,
        claim_lease: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
        claim_token: str | None = None,
    ) -> list[dict[str, Any]]:
        eff_now = now or datetime.now(timezone.utc)
        token = str(claim_token).strip() if claim_token else str(uuid.uuid4())
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
                        and not self._has_live_claim(row, eff_now)
                    )
                    or ws_deleted
                )
                if not due:
                    continue
                if row["state"] in ("uploading", "completing", "ready"):
                    row["state"] = "aborted"
                row["claim_token"] = token
                row["pending_claims"] = {}
                row["lease_until"] = eff_now + claim_lease
                row["updated_at"] = eff_now
                claimed.append(dict(row))
        return claimed

    async def mark_cleaned(
        self,
        upload_id: str,
        *,
        claim_token: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        async with self._lock:
            row = self.rows.get(upload_id)
            if (
                not row
                or row["state"] not in ("aborted", "failed")
                or row["cleaned_at"] is not None
            ):
                return False
            if claim_token is not None:
                token = str(claim_token).strip()
                if not token or row.get("claim_token") != token:
                    return False
            row["cleaned_at"] = now
            row["claim_token"] = None
            row["lease_until"] = None
            row["updated_at"] = now
            return True

    async def release_cleanup_claim(
        self,
        upload_id: str,
        *,
        claim_token: str,
        retry_after: timedelta = timedelta(minutes=2),
    ) -> None:
        token = str(claim_token or "").strip()
        if not token:
            return
        now = datetime.now(timezone.utc)
        async with self._lock:
            if row := self.rows.get(upload_id):
                if (
                    row["state"] in ("aborted", "failed")
                    and row["cleaned_at"] is None
                    and row.get("claim_token") == token
                ):
                    row["claim_token"] = None
                    row["lease_until"] = now + retry_after
                    row["updated_at"] = now

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
        "reopen_terminal_cleanup",
        "mark_used",
        "claim_provisional_batch",
        "confirm_provisional_claims",
        "release_provisional_claims",
        "has_retained_workspace_uploads",
        "is_upload_retained_for_recovery",
        "list_retained_workspace_uploads",
        "mark_uploads_delete_pending",
        "clear_uploads_delete_pending",
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
        if ws is None or ws.get("status") == "deleted":
            raise HTTPException(status_code=404, detail="Workspace not found")
        if str(ws["user_id"]) != str(user_id):
            if ws.get("web_access") == "rw" and user_id in (ws.get("colleagues") or ()):
                return ws
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
    assert d033_chat_file_uploads.down_revision == "d032"

    executed_sql: list[str] = []
    with patch.object(
        alembic_op,
        "execute",
        autospec=True,
        side_effect=lambda sql: executed_sql.append(str(sql)),
    ):
        d033_chat_file_uploads.upgrade()
        assert any("CREATE TABLE IF NOT EXISTS dalton_chat_file_uploads" in s for s in executed_sql)
        assert any("claim_token TEXT" in s for s in executed_sql)
        assert any("pending_claims JSONB" in s for s in executed_sql)
        assert any("chk_dalton_chat_file_uploads_cleaned_terminal" in s for s in executed_sql)
        assert any("idx_dalton_chat_file_uploads_cleanup" in s for s in executed_sql)

        executed_sql.clear()
        d033_chat_file_uploads.downgrade()
        assert any("DROP TABLE IF EXISTS dalton_chat_file_uploads" in s for s in executed_sql)

    # Guard against AmbiguousColumn in CTE UPDATE ... FROM ... RETURNING:
    for cte_fn in (
        db_chat_uploads.abort_upload,
        db_chat_uploads.retire_uploads_by_ids,
        db_chat_uploads.retire_workspace_uploads,
        db_chat_uploads.claim_cleanup_batch,
    ):
        assert "RETURNING {_U_COLUMNS}" in inspect.getsource(cte_fn)
    for col_expr in db_chat_uploads._U_COLUMNS.split(","):
        assert col_expr.strip().startswith("u.")

    # Ensure InMemoryChatUploadLedger mirrors every public async helper in db_chat_uploads
    for name, member in inspect.getmembers(db_chat_uploads, inspect.iscoroutinefunction):
        if member.__module__ != db_chat_uploads.__name__:
            continue
        ledger_fn = getattr(InMemoryChatUploadLedger, name, None)
        assert ledger_fn is not None, f"Missing {name} on InMemoryChatUploadLedger"
        real_params = [
            (p.name, p.kind)
            for p in inspect.signature(member).parameters.values()
        ]
        fake_params = [
            (p.name, p.kind)
            for p in list(inspect.signature(ledger_fn).parameters.values())[1:]
        ]
        assert fake_params == real_params, f"Signature mismatch on {name}: {fake_params} != {real_params}"


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

    # Valid pre-stream claim acquires a bounded provisional claim (leaving used_at unset),
    # blocks DELETE (409) while in-flight, and stamps used_at at durable acceptance.
    claimed_rows = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
        user_id=alice,
        workspace_id=ws_a,
        claim_key="req:alice-turn-1",
    )
    assert len(claimed_rows) == 1
    assert claimed_rows[0]["used_at"] is None
    assert "req:alice-turn-1" in claimed_rows[0]["pending_claims"]

    with pytest.raises(HTTPException) as cancel_inflight_exc:
        await svc.cancel_chat_upload(upload_id=uid, user_id=alice)
    assert cancel_inflight_exc.value.status_code == 409

    accepted_rows = await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=uid, name="model.xlsx", size=len(data))],
        user_id=alice,
        workspace_id=ws_a,
        claim_key="req:alice-turn-1",
    )
    assert accepted_rows[0]["used_at"] is not None
    assert accepted_rows[0]["expires_at"] is None

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
    provisional = await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=up_used["upload_id"], name="kept_200m.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key="req:clean-turn-1",
    )
    assert provisional[0]["used_at"] is None
    claimed = await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=up_used["upload_id"], name="kept_200m.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key="req:clean-turn-1",
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
    def __init__(self, stdout: str = "", exit_code: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
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
        import json as _json
        import shlex as _shlex

        cmd = str(args[0]) if args else ""
        if "parent, lock_p, grant_d, grant_p = sys.argv[1]" in cmd:
            parts = _shlex.split(cmd)
            lock_p, grant_p = parts[-3], parts[-1]
            self.files.setdefault(lock_p, b"")
            self.files[grant_p] = b"1"
            return _FakeExecResult(stdout="", exit_code=0)
        if "grant_p = sys.argv[1]" in cmd and "os.unlink(grant_p)" in cmd:
            parts = _shlex.split(cmd)
            grant_p = parts[-1]
            self.files.pop(grant_p, None)
            return _FakeExecResult(stdout="", exit_code=0)
        if "items, unlink_tgt = json.loads(sys.argv[1])" in cmd:
            parts = _shlex.split(cmd)
            items = _json.loads(parts[-2])
            unlink_tgt = parts[-1] == "1"
            for parent, lock_p, grant_d, g_pref, abs_p in items:
                self.files.setdefault(lock_p, b"")
                for k in list(self.files):
                    if k.startswith(f"{grant_d}/") or k.startswith(f"{parent}/{g_pref}"):
                        self.files.pop(k, None)
                if unlink_tgt:
                    self.files.pop(abs_p, None)
            return _FakeExecResult(stdout="", exit_code=0)
        if "for p in " in cmd:
            # Missing-path probe from _probe_missing_sandbox_paths
            prefix = cmd.split("for p in ", 1)[1].split("; do ", 1)[0]
            missing = [p for p in _shlex.split(prefix) if p not in self.files]
            return _FakeExecResult(
                stdout="\n".join(missing) + ("\n" if missing else ""),
                exit_code=0,
            )
        if "p, exp_sz, exp_sha = sys.argv[1]" in cmd:
            parts = _shlex.split(cmd)
            if len(parts) >= 3:
                target_p, exp_sz_str, exp_sha = parts[-3], parts[-2], parts[-1]
                buf = self.files.get(target_p)
                if (
                    buf is not None
                    and len(buf) == int(exp_sz_str)
                    and hashlib.sha256(buf).hexdigest() == exp_sha.lower()
                ):
                    return _FakeExecResult(stdout="", exit_code=0)
            return _FakeExecResult(stdout="", exit_code=1)
        # Simulate free disk space (defaults to 20 GiB free in 1K-blocks df output)
        avail_kb = getattr(self, "free_bytes", 20 * 1024 * 1024 * 1024) // 1024
        df_out = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"overlay 52428800 1048576 {avail_kb} 5% /home/workspace\n"
        )
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
        sandbox: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del defer_dir_modes
        assert sweep_orphans is False
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

    await svc.validate_and_claim_request_attachments(
        req.additional_context,
        user_id=user_id,
        workspace_id=ws_id,
        claim_key="req:test-mat-direct",
    )
    await svc.confirm_request_chat_uploads(
        req.additional_context,
        user_id=user_id,
        workspace_id=ws_id,
        claim_key="req:test-mat-direct",
    )

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
    assert ".wsfiles-grants-" in pulled_entries[0]["grant_file"]
    assert ".wsfiles-tlock-" in pulled_entries[0]["lock_file"]
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
        "claim_token": None,
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
        sandbox: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del defer_dir_modes
        assert sweep_orphans is False
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
    assert "/home/workspace/AlphaProj/.file_sync_marker" in cold_sandbox.files
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
        "claim_token": None,
        "lease_until": None,
        "expires_at": None,
        "used_at": now,
        "cleaned_at": None,
        "created_at": now,
        "updated_at": now,
    }

    # 2. Tampered object_key pointing into another workspace's prefix + unaccepted upload row
    bad_key_uid = str(uuid.uuid4())
    unaccepted_uid = str(uuid.uuid4())
    colleague_accepted_uid = str(uuid.uuid4())
    upload_env["ledger"].rows[bad_key_uid] = {
        **upload_env["ledger"].rows[bad_home_uid],
        "upload_id": bad_key_uid,
        "user_id": alice,
        "workspace_id": ws_alice,
        "object_key": chat_upload_storage.build_chat_upload_key(
            ws_other, bad_key_uid, "wrong_ws.bin"
        ),
    }
    upload_env["ledger"].rows[unaccepted_uid] = {
        **upload_env["ledger"].rows[bad_home_uid],
        "upload_id": unaccepted_uid,
        "user_id": mallory,
        "workspace_id": ws_alice,
        "used_at": None,
        "expires_at": now + timedelta(hours=12),
        "object_key": chat_upload_storage.build_chat_upload_key(
            ws_alice, unaccepted_uid, "unaccepted.bin"
        ),
    }

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

    # 3. Deleted workspace never recovers even accepted rows
    upload_env["workspaces"][ws_alice]["status"] = "deleted"
    upload_env["ledger"].rows[colleague_accepted_uid] = {
        **upload_env["ledger"].rows[bad_home_uid],
        "upload_id": colleague_accepted_uid,
        "user_id": mallory,
        "workspace_id": ws_alice,
        "used_at": now,
        "expires_at": None,
        "object_key": chat_upload_storage.build_chat_upload_key(
            ws_alice, colleague_accepted_uid, "shared_model.xlsx"
        ),
    }
    res_deleted = await svc.rematerialize_workspace_chat_uploads(
        ws_alice, sbx, layout=WorkspaceLayout(root="/home/workspace", dir_name="Proj")
    )
    assert res_deleted == {"restored": 0, "errors": 0}
    pull_spy.assert_not_called()

    # 4. In a live workspace, an already-accepted colleague file recovers for the owner even
    # after the colleague's access is removed, while the removed colleague gets 403 on new uploads.
    upload_env["workspaces"][ws_alice]["status"] = "running"
    expected_colleague_rel = svc.build_sandbox_upload_rel_path(
        upload_env["ledger"].rows[colleague_accepted_uid]
    )
    pull_spy.reset_mock()
    pull_spy.return_value = {expected_colleague_rel: {"status": "ok"}}
    res_owner_recover = await svc.rematerialize_workspace_chat_uploads(
        ws_alice, sbx, layout=WorkspaceLayout(root="/home/workspace", dir_name="Proj")
    )
    assert res_owner_recover == {"restored": 1, "errors": 0}
    pull_spy.assert_awaited_once()

    with pytest.raises(HTTPException) as removed_colleague_exc:
        await svc.initiate_chat_upload(
            user_id=mallory,
            workspace_id=ws_alice,
            filename="new_attempt.bin",
            size_bytes=1024,
        )
    assert removed_colleague_exc.value.status_code == 403


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
    await svc.confirm_request_chat_uploads(
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
    await svc.confirm_request_chat_uploads(
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


# ---------------------------------------------------------------------------
# 6. Lease-recovery completion ownership & late-assembly cancellation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_lease_expiry_and_reclaim_preserves_ready_and_used_upload(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Worker A stalls past lease_until after assembly, Worker B reclaims and marks
    ready (+ used); Worker A resuming must return idempotently without deleting B's object."""
    user_id = "user-lease-ready"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    payload = b"durable-lease-payload-" * 256
    expected_sha = hashlib.sha256(payload).hexdigest()
    created = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="lease_race.bin",
        size_bytes=len(payload),
        expected_sha256=expected_sha,
    )
    uid = created["upload_id"]
    row = upload_env["ledger"].rows[uid]
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=1, user_id=user_id, stream=_bytes_stream(payload)
    )

    entered_a = threading.Event()
    release_a = threading.Event()
    call_count = 0
    store_digest = upload_env["store"].compute_stored_object_digest

    def _controlled_digest(object_key: str) -> tuple[int, str] | None:
        nonlocal call_count
        call_count += 1
        res = store_digest(object_key)
        if call_count == 1:
            entered_a.set()
            if not release_a.wait(5):
                raise TimeoutError("Worker A release timed out")
        return res

    monkeypatch.setattr(
        chat_upload_storage,
        "compute_stored_object_digest",
        create_autospec(_REAL_COMPUTE_STORED_OBJECT_DIGEST, side_effect=_controlled_digest),
    )

    worker_a = asyncio.create_task(
        svc.complete_chat_upload(upload_id=uid, user_id=user_id, expected_sha256=expected_sha)
    )
    assert await asyncio.to_thread(entered_a.wait, 3)

    # Expire Worker A's completion lease; Worker B reclaims, completes, and chat turn marks used_at
    row["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        second = await svc.complete_chat_upload(
            upload_id=uid, user_id=user_id, expected_sha256=expected_sha
        )
        assert second["state"] == "ready"
        assert second["sha256"] == expected_sha

        claimed_turn = await svc.confirm_request_chat_uploads(
            [ChatUploadContext(upload_id=uid, name="lease_race.bin", size=len(payload))],
            user_id=user_id,
            workspace_id=ws_id,
        )
        assert claimed_turn[0]["used_at"] is not None
    finally:
        release_a.set()

    first = await worker_a
    assert first["state"] == "ready"
    assert first["sha256"] == expected_sha
    assert row["state"] == "ready"
    assert row["used_at"] is not None
    assert row["cleaned_at"] is None
    assert upload_env["store"].objects.get(row["object_key"]) == payload


@pytest.mark.asyncio
async def test_stale_completion_errors_cannot_reopen_fail_or_delete_successor(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale Worker A hitting transient reopen or checksum-mismatch mark_failed cannot
    clobber active Worker B or delete B's ready/used object."""
    user_id = "user-stale-err"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }
    store_digest = upload_env["store"].compute_stored_object_digest

    # 1. Stale Worker A gets transient None digest while Worker B actively holds completing
    payload_1 = b"reopen-guard-" * 128
    up_1 = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="reopen_guard.bin", size_bytes=len(payload_1)
    )
    uid_1 = up_1["upload_id"]
    row_1 = upload_env["ledger"].rows[uid_1]
    await svc.receive_chat_upload_part(
        upload_id=uid_1, part_number=1, user_id=user_id, stream=_bytes_stream(payload_1)
    )

    entered_1a = threading.Event()
    release_1a = threading.Event()
    entered_1b = threading.Event()
    release_1b = threading.Event()
    seq_1 = 0

    def _digest_reopen_race(object_key: str) -> tuple[int, str] | None:
        nonlocal seq_1
        seq_1 += 1
        if seq_1 == 1:
            entered_1a.set()
            if not release_1a.wait(5):
                raise TimeoutError("1a timeout")
            return None
        entered_1b.set()
        if not release_1b.wait(5):
            raise TimeoutError("1b timeout")
        return store_digest(object_key)

    monkeypatch.setattr(
        chat_upload_storage,
        "compute_stored_object_digest",
        create_autospec(_REAL_COMPUTE_STORED_OBJECT_DIGEST, side_effect=_digest_reopen_race),
    )

    task_1a = asyncio.create_task(svc.complete_chat_upload(upload_id=uid_1, user_id=user_id))
    assert await asyncio.to_thread(entered_1a.wait, 3)
    row_1["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    task_1b = asyncio.create_task(svc.complete_chat_upload(upload_id=uid_1, user_id=user_id))
    assert await asyncio.to_thread(entered_1b.wait, 3)
    b_token = row_1["claim_token"]
    assert b_token is not None

    # Release stale A while B is still completing: A cannot reopen B and gets 409 completing_in_progress
    release_1a.set()
    with pytest.raises(HTTPException) as stale_reopen_exc:
        await task_1a
    assert stale_reopen_exc.value.status_code == 409
    assert stale_reopen_exc.value.detail["code"] == "completing_in_progress"
    assert row_1["state"] == "completing"
    assert row_1["claim_token"] == b_token

    release_1b.set()
    done_1b = await task_1b
    assert done_1b["state"] == "ready"
    assert row_1["cleaned_at"] is None
    assert upload_env["store"].objects.get(row_1["object_key"]) == payload_1

    # 2. Stale Worker A gets mismatched digest after Worker B marked ready + used_at
    payload_2 = b"fail-guard-" * 128
    sha_2 = hashlib.sha256(payload_2).hexdigest()
    up_2 = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="fail_guard.bin",
        size_bytes=len(payload_2),
        expected_sha256=sha_2,
    )
    uid_2 = up_2["upload_id"]
    row_2 = upload_env["ledger"].rows[uid_2]
    await svc.receive_chat_upload_part(
        upload_id=uid_2, part_number=1, user_id=user_id, stream=_bytes_stream(payload_2)
    )

    entered_2a = threading.Event()
    release_2a = threading.Event()
    seq_2 = 0

    def _digest_fail_race(object_key: str) -> tuple[int, str] | None:
        nonlocal seq_2
        seq_2 += 1
        if seq_2 == 1:
            entered_2a.set()
            if not release_2a.wait(5):
                raise TimeoutError("2a timeout")
            return (len(payload_2), "0" * 64)
        return store_digest(object_key)

    monkeypatch.setattr(
        chat_upload_storage,
        "compute_stored_object_digest",
        create_autospec(_REAL_COMPUTE_STORED_OBJECT_DIGEST, side_effect=_digest_fail_race),
    )

    task_2a = asyncio.create_task(
        svc.complete_chat_upload(upload_id=uid_2, user_id=user_id, expected_sha256=sha_2)
    )
    assert await asyncio.to_thread(entered_2a.wait, 3)
    row_2["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        done_2b = await svc.complete_chat_upload(
            upload_id=uid_2, user_id=user_id, expected_sha256=sha_2
        )
        assert done_2b["state"] == "ready"
        await svc.confirm_request_chat_uploads(
            [ChatUploadContext(upload_id=uid_2, name="fail_guard.bin", size=len(payload_2))],
            user_id=user_id,
            workspace_id=ws_id,
        )
    finally:
        release_2a.set()

    done_2a = await task_2a
    assert done_2a["state"] == "ready"
    assert done_2a["sha256"] == sha_2
    assert row_2["state"] == "ready"
    assert row_2["used_at"] is not None
    assert row_2["cleaned_at"] is None
    assert upload_env["store"].objects.get(row_2["object_key"]) == payload_2

    # Direct defense-in-depth check: _cleanup_storage_objects refuses non-terminal rows
    assert await svc._cleanup_storage_objects(row_2) is False
    assert upload_env["store"].objects.get(row_2["object_key"]) == payload_2


@pytest.mark.asyncio
async def test_cancel_during_finalization_cleans_late_assembled_object_and_sweeper_fallback(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during complete_multipart_upload removes late-assembled objects both
    cooperatively when the completion worker resumes and durably via the sweeper on crash."""
    user_id = "user-cancel-finalizing"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }
    payload = b"late-assembly-bytes-" * 200

    # 1. Cooperative cleanup when CompleteMultipartUpload lands right after cancel_chat_upload
    up_1 = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="late_coop.bin", size_bytes=len(payload)
    )
    uid_1 = up_1["upload_id"]
    row_1 = upload_env["ledger"].rows[uid_1]
    key_1 = row_1["object_key"]
    await svc.receive_chat_upload_part(
        upload_id=uid_1, part_number=1, user_id=user_id, stream=_bytes_stream(payload)
    )

    assembling_1 = threading.Event()
    finish_assembly_1 = threading.Event()

    def _late_complete_1(
        object_key: str, store_upload_id: str, parts: list[tuple[int, str]]
    ) -> bool:
        assembling_1.set()
        if not finish_assembly_1.wait(5):
            raise TimeoutError("finish_assembly_1 timeout")
        # Simulate S3/MinIO committing CompleteMultipartUpload just after Abort/Delete ran
        upload_env["store"].objects[object_key] = payload
        return True

    monkeypatch.setattr(
        chat_upload_storage,
        "complete_multipart_upload",
        create_autospec(_REAL_COMPLETE_MULTIPART_UPLOAD, side_effect=_late_complete_1),
    )

    comp_task_1 = asyncio.create_task(svc.complete_chat_upload(upload_id=uid_1, user_id=user_id))
    assert await asyncio.to_thread(assembling_1.wait, 3)

    try:
        cancelled_1 = await svc.cancel_chat_upload(upload_id=uid_1, user_id=user_id)
        assert cancelled_1["state"] == "aborted"
        # While in-flight completion was active, cleaned_at stays unset until late assembly resolves
        assert row_1["cleaned_at"] is None
    finally:
        finish_assembly_1.set()

    with pytest.raises(HTTPException) as cancel_race_exc:
        await comp_task_1
    assert cancel_race_exc.value.status_code == 409
    assert key_1 not in upload_env["store"].objects
    assert row_1["state"] == "aborted"
    assert row_1["cleaned_at"] is not None

    # 2. Sweeper fallback if completion worker crashes right after MinIO writes late object
    up_2 = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="late_crash.bin", size_bytes=len(payload)
    )
    uid_2 = up_2["upload_id"]
    row_2 = upload_env["ledger"].rows[uid_2]
    key_2 = row_2["object_key"]
    await svc.receive_chat_upload_part(
        upload_id=uid_2, part_number=1, user_id=user_id, stream=_bytes_stream(payload)
    )

    claimed_2 = await db_chat_uploads.claim_complete(uid_2, lease=svc.COMPLETE_LEASE)
    assert claimed_2 is not None and claimed_2["state"] == "completing"

    await svc.cancel_chat_upload(upload_id=uid_2, user_id=user_id)
    assert row_2["state"] == "aborted"
    assert row_2["cleaned_at"] is None

    # Simulate MinIO finishing late object creation right as the worker process died
    upload_env["store"].objects[key_2] = payload
    assert key_2 in upload_env["store"].objects

    swept = await svc.sweep_expired_chat_uploads_once()
    assert swept == 1
    assert key_2 not in upload_env["store"].objects
    assert row_2["cleaned_at"] is not None


# ---------------------------------------------------------------------------
# 7. Batch validation, provisional claims, part deadlines, steers & ready quotas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_validation_and_provisional_claims_release_on_rejection_and_protect_accepted(
    upload_env,
) -> None:
    """Rejected 4x200 MiB (413), later invalid ID (404), or admission rejection leaves
    uploads unused/cancellable with expiry intact, while protecting concurrent accepted requests."""
    user_id = "user-batch-prov"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }
    now = datetime.now(timezone.utc)

    # Seed 4 ready 200 MiB rows directly
    uids_200: list[str] = []
    for idx in range(4):
        uid = str(uuid.uuid4())
        uids_200.append(uid)
        obj_key = chat_upload_storage.build_chat_upload_key(ws_id, uid, f"f{idx}.bin")
        upload_env["store"].objects[obj_key] = b"x"
        upload_env["ledger"].rows[uid] = {
            "upload_id": uid,
            "user_id": user_id,
            "workspace_id": ws_id,
            "filename": f"f{idx}.bin",
            "safe_filename": f"f{idx}.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": svc.MAX_CHAT_UPLOAD_BYTES,
            "part_size": svc.CHAT_UPLOAD_PART_SIZE,
            "part_count": 25,
            "object_key": obj_key,
            "store_upload_id": f"mp-b-{idx}",
            "expected_sha256": "a" * 64,
            "actual_sha256": "a" * 64,
            "uploaded_parts": {},
            "pending_claims": {},
            "state": "ready",
            "error": None,
            "claim_token": None,
            "lease_until": None,
            "expires_at": now + timedelta(hours=24),
            "used_at": None,
            "cleaned_at": None,
            "created_at": now,
            "updated_at": now,
        }

    # 1. 4 x 200 MiB = 800 MiB > 600 MiB rejected 413 BEFORE touching any row
    with pytest.raises(HTTPException) as over_total_exc:
        await svc.validate_and_claim_request_attachments(
            [
                ChatUploadContext(upload_id=u, name="f.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)
                for u in uids_200
            ],
            user_id=user_id,
            workspace_id=ws_id,
            claim_key="req:over-total",
        )
    assert over_total_exc.value.status_code == 413
    for u in uids_200:
        r = upload_env["ledger"].rows[u]
        assert r["used_at"] is None
        assert r["expires_at"] is not None
        assert r["pending_claims"] == {}

    # 2. Valid first ID followed by nonexistent second ID -> 404, first ID stays unused & cancellable
    with pytest.raises(HTTPException) as bad_later_exc:
        await svc.validate_and_claim_request_attachments(
            [
                ChatUploadContext(upload_id=uids_200[0], name="f0.bin", size=svc.MAX_CHAT_UPLOAD_BYTES),
                ChatUploadContext(upload_id=str(uuid.uuid4()), name="missing.bin", size=1024),
            ],
            user_id=user_id,
            workspace_id=ws_id,
            claim_key="req:bad-later",
        )
    assert bad_later_exc.value.status_code == 404
    assert upload_env["ledger"].rows[uids_200[0]]["used_at"] is None
    assert upload_env["ledger"].rows[uids_200[0]]["pending_claims"] == {}

    # 3. ChatUploadSendScope rejected before admission releases its claim so cancel succeeds
    target_uid = uids_200[0]
    with pytest.raises(HTTPException) as conflict_exc:
        async with svc.ChatUploadSendScope(
            additional_context=[
                ChatUploadContext(upload_id=target_uid, name="f0.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)
            ],
            user_id=user_id,
            workspace_id=ws_id,
            request_key=str(uuid.uuid4()),
        ):
            assert upload_env["ledger"].rows[target_uid]["used_at"] is None
            assert len(upload_env["ledger"].rows[target_uid]["pending_claims"]) == 1
            raise HTTPException(status_code=409, detail="running")
    assert conflict_exc.value.status_code == 409
    assert upload_env["ledger"].rows[target_uid]["used_at"] is None
    assert upload_env["ledger"].rows[target_uid]["pending_claims"] == {}
    assert upload_env["ledger"].rows[target_uid]["expires_at"] is not None

    # 4. Concurrent accepted + rejected requests referencing the same upload:
    # releasing the rejected request preserves the accepted request's retention.
    shared_uid = uids_200[1]
    req_accepted = str(uuid.uuid4())
    req_rejected = str(uuid.uuid4())
    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=shared_uid, name="f1.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=f"req:{req_accepted}",
    )
    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=shared_uid, name="f1.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=f"req:{req_rejected}",
    )
    await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=shared_uid, name="f1.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=f"req:{req_accepted}",
    )
    await svc.release_request_chat_uploads(
        user_id=user_id,
        claim_key=f"req:{req_rejected}",
        request_key=req_rejected,
        additional_context=[ChatUploadContext(upload_id=shared_uid, name="f1.bin", size=svc.MAX_CHAT_UPLOAD_BYTES)],
    )
    assert upload_env["ledger"].rows[shared_uid]["used_at"] is not None
    assert upload_env["ledger"].rows[shared_uid]["expires_at"] is None

    # Meanwhile unused target_uid can still be cancelled immediately
    cancelled = await svc.cancel_chat_upload(upload_id=target_uid, user_id=user_id)
    assert cancelled["state"] == "aborted"


@pytest.mark.asyncio
async def test_part_upload_idle_and_total_deadlines_release_slots_and_recheck_expiry(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stalled part streams fail with 408, release all 6 semaphore slots, and expired/aborted
    uploads fail fast after slot wait without sending bytes to MinIO."""
    user_id = "user-part-deadline"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    monkeypatch.setattr(
        svc,
        "get_chat_upload_part_idle_timeout_s",
        create_autospec(svc.get_chat_upload_part_idle_timeout_s, return_value=0.05),
    )
    monkeypatch.setattr(
        svc,
        "get_chat_upload_part_total_timeout_s",
        create_autospec(svc.get_chat_upload_part_total_timeout_s, return_value=0.15),
    )

    payload = b"p" * 4096
    up = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="deadline.bin", size_bytes=len(payload)
    )
    uid = up["upload_id"]

    async def _stalled_stream():
        yield b"p" * 512
        await asyncio.sleep(5.0)

    # Saturate all 6 semaphore slots simultaneously with stalled streams -> all time out 408 & release slots
    stalled_outcomes = await asyncio.gather(
        *(
            svc.receive_chat_upload_part(
                upload_id=uid,
                part_number=1,
                user_id=user_id,
                stream=_stalled_stream(),
            )
            for _ in range(6)
        ),
        return_exceptions=True,
    )
    for err in stalled_outcomes:
        assert isinstance(err, HTTPException)
        assert err.status_code == 408

    # Subsequent valid part upload immediately succeeds because all semaphore slots were freed
    ok_part = await svc.receive_chat_upload_part(
        upload_id=uid,
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(payload),
    )
    assert ok_part["size_bytes"] == len(payload)

    # Expired upload fails with 410 before touching MinIO upload_part_bytes
    up_exp = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="expired_part.bin", size_bytes=len(payload)
    )
    uid_exp = up_exp["upload_id"]
    upload_env["ledger"].rows[uid_exp]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as exp_exc:
        await svc.receive_chat_upload_part(
            upload_id=uid_exp,
            part_number=1,
            user_id=user_id,
            stream=_bytes_stream(payload),
        )
    assert exp_exc.value.status_code == 410


@pytest.mark.asyncio
async def test_ready_unused_uploads_count_toward_quota_until_cancelled_swept_or_accepted(
    upload_env,
) -> None:
    """Completed-but-unused ready uploads count toward the 10-file / 1000 MiB per-user cap;
    cancelling, sweeping, or durably accepting an upload releases temporary quota."""
    user_id = "user-ready-quota"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    # Complete 10 small uploads to 'ready' without sending -> 11th initiate must hit 429
    ready_ids: list[str] = []
    for i in range(svc.MAX_ACTIVE_USER_UPLOADS):
        created = await svc.initiate_chat_upload(
            user_id=user_id,
            workspace_id=ws_id,
            filename=f"u{i}.bin",
            size_bytes=512,
        )
        uid = created["upload_id"]
        await svc.receive_chat_upload_part(
            upload_id=uid, part_number=1, user_id=user_id, stream=_bytes_stream(b"q" * 512)
        )
        done = await svc.complete_chat_upload(upload_id=uid, user_id=user_id)
        assert done["state"] == "ready"
        ready_ids.append(uid)

    with pytest.raises(HTTPException) as cap_count_exc:
        await svc.initiate_chat_upload(
            user_id=user_id,
            workspace_id=ws_id,
            filename="u10.bin",
            size_bytes=512,
        )
    assert cap_count_exc.value.status_code == 429

    # Cancelling one ready unused upload releases a slot
    await svc.cancel_chat_upload(upload_id=ready_ids[0], user_id=user_id)
    eleventh = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="u10_ok.bin", size_bytes=512
    )
    await svc.cancel_chat_upload(upload_id=eleventh["upload_id"], user_id=user_id)

    # Durably accepting ready_ids[1] into a message moves it out of temporary upload quota
    await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=ready_ids[1], name="u1.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
    )
    after_accept = await svc.initiate_chat_upload(
        user_id=user_id, workspace_id=ws_id, filename="after_accept.bin", size_bytes=512
    )
    assert after_accept["state"] == "uploading"


# ---------------------------------------------------------------------------
# 8. Mixed steer staging, provisional release isolation, live part permission
#    rechecks, durable steer failure replay, and disk budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_attachments_in_steers_ordered_staging_and_partial_failure_rollback(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stage_request_attachments_for_steer persists mixed 200 MiB + inline PNG/PDF batches
    in exact order, supports inline-only and file-only batches, and rolls back newly staged
    inline blobs if any item or admission fails."""
    user_id = "user-mixed-steer"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    # Pre-upload a ready file
    file_bytes = b"large-model-data-" * 512
    created = await svc.initiate_chat_upload(
        user_id=user_id,
        workspace_id=ws_id,
        filename="dataset_200m.parquet",
        size_bytes=len(file_bytes),
        mime_type="application/octet-stream",
    )
    uid_file = created["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_file,
        part_number=1,
        user_id=user_id,
        stream=_bytes_stream(file_bytes),
    )
    await svc.complete_chat_upload(upload_id=uid_file, user_id=user_id)

    png_raw = b"\x89PNG\r\n\x1a\n" + b"png-pixels-" * 32
    pdf_raw = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"
    png_url = f"data:image/png;base64,{base64.b64encode(png_raw).decode()}"
    pdf_url = f"data:application/pdf;base64,{base64.b64encode(pdf_raw).decode()}"

    mixed_batch = [
        ChatUploadContext(
            upload_id=uid_file,
            name="dataset_200m.parquet",
            mime_type="application/octet-stream",
            size=len(file_bytes),
        ),
        MultimodalContext(type="image", data=png_url, description="chart_q4.png"),
        MultimodalContext(type="pdf", data=pdf_url, description="summary_memo.pdf"),
    ]

    # 1. Partial staging failure on second inline item aborts & deletes first staged inline item
    real_upload_part = upload_env["store"].upload_part_bytes
    calls = 0

    def _fail_on_second_inline(
        object_key: str,
        store_upload_id: str,
        *,
        part_number: int,
        data: bytes,
    ) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            return None
        return real_upload_part(
            object_key,
            store_upload_id,
            part_number=part_number,
            data=data,
        )

    with patch.object(
        chat_upload_storage,
        "upload_part_bytes",
        create_autospec(_REAL_UPLOAD_PART_BYTES, side_effect=_fail_on_second_inline),
    ):
        with pytest.raises(HTTPException) as stage_fail_exc:
            await svc.stage_request_attachments_for_steer(
                mixed_batch,
                user_id=user_id,
                workspace_id=ws_id,
                claim_key="req:stage-fail",
            )
        assert stage_fail_exc.value.status_code == 503

    # Caller's pre-existing upload stays ready; zero newly staged inline blobs or open uploads remain
    assert upload_env["ledger"].rows[uid_file]["state"] == "ready"
    assert len(upload_env["store"].objects) == 1
    assert upload_env["store"].open_uploads == {}
    for row_uid, row_val in upload_env["ledger"].rows.items():
        if row_uid == uid_file:
            continue
        assert row_val["state"] in ("aborted", "failed")
        assert row_val["cleaned_at"] is not None
        assert row_val["claim_token"] is None
        assert row_val["lease_until"] is None

    # Post-claim verification failure inside _stage_single_inline_attachment marks failed and cleans up
    with patch.object(
        chat_upload_storage,
        "compute_stored_object_digest",
        create_autospec(_REAL_COMPUTE_STORED_OBJECT_DIGEST, return_value=None),
    ):
        with pytest.raises(HTTPException) as verify_fail_exc:
            await svc.stage_request_attachments_for_steer(
                [MultimodalContext(type="image", data=png_url, description="verify_fail.png")],
                user_id=user_id,
                workspace_id=ws_id,
                claim_key="req:verify-fail",
            )
        assert verify_fail_exc.value.status_code == 503
    assert len(upload_env["store"].objects) == 1
    assert upload_env["store"].open_uploads == {}
    for row_uid, row_val in upload_env["ledger"].rows.items():
        if row_uid == uid_file:
            continue
        assert row_val["state"] in ("aborted", "failed")
        assert row_val["cleaned_at"] is not None
        assert row_val["claim_token"] is None
        assert row_val["lease_until"] is None

    # 2. Full mixed staging succeeds in exact order [dataset_200m.parquet, chart_q4.png, summary_memo.pdf]
    claim_mixed = svc.chat_upload_claim_key(
        request_key=str(uuid.uuid4()), run_id=str(uuid.uuid4())
    )
    descriptors, staged_ids = await svc.stage_request_attachments_for_steer(
        mixed_batch,
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=claim_mixed,
    )
    assert len(descriptors) == 3
    assert len(staged_ids) == 2
    assert [d["name"] for d in descriptors] == [
        "dataset_200m.parquet",
        "chart_q4.png",
        "summary_memo.pdf",
    ]
    assert [d["source_kind"] for d in descriptors] == ["chat_upload", "image", "pdf"]

    hist_meta = await svc.build_chat_upload_history_metadata(
        descriptors, user_id=user_id, workspace_id=ws_id
    )
    assert [m["type"] for m in hist_meta] == ["file", "image", "pdf"]
    assert [m["name"] for m in hist_meta] == [
        "dataset_200m.parquet",
        "chart_q4.png",
        "summary_memo.pdf",
    ]

    # Confirm all 3 atomically (as admit_steer does)
    confirmed = await svc.confirm_request_chat_uploads(
        descriptors,
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=claim_mixed,
    )
    assert len(confirmed) == 3
    assert all(r["used_at"] is not None and r["expires_at"] is None for r in confirmed)

    # 3. Non-accepted staged inline steer cleans up its staged blob immediately
    desc_unaccepted, staged_unaccepted = await svc.stage_request_attachments_for_steer(
        [MultimodalContext(type="image", data=png_url, description="temp_shot")],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key="req:unaccepted-steer",
    )
    assert len(staged_unaccepted) == 1
    unaccepted_uid = staged_unaccepted[0]
    unaccepted_key = upload_env["ledger"].rows[unaccepted_uid]["object_key"]
    assert unaccepted_key in upload_env["store"].objects

    await svc.cleanup_unaccepted_staged_uploads(
        staged_unaccepted,
        user_id=user_id,
        claim_key="req:unaccepted-steer",
    )
    assert upload_env["ledger"].rows[unaccepted_uid]["state"] == "aborted"
    assert upload_env["ledger"].rows[unaccepted_uid]["cleaned_at"] is not None
    assert unaccepted_key not in upload_env["store"].objects


@pytest.mark.asyncio
async def test_provisional_release_never_confirms_unrelated_uploads_on_same_request_key(
    upload_env,
) -> None:
    """Duplicate/concurrent requests sharing request_key with different uploads must never
    confirm the rejected upload set on release_provisional_claims."""
    user_id = "user-dup-claim"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    async def _make_ready(name: str) -> str:
        data = f"payload-{name}".encode() * 64
        up = await svc.initiate_chat_upload(
            user_id=user_id, workspace_id=ws_id, filename=name, size_bytes=len(data)
        )
        uid = up["upload_id"]
        await svc.receive_chat_upload_part(
            upload_id=uid, part_number=1, user_id=user_id, stream=_bytes_stream(data)
        )
        await svc.complete_chat_upload(upload_id=uid, user_id=user_id)
        return uid

    uid_accepted = await _make_ready("accepted.bin")
    uid_rejected = await _make_ready("rejected.bin")

    shared_rk = str(uuid.uuid4())
    upload_env["ledger"].accepted_request_keys.add(shared_rk)

    claim_a = svc.chat_upload_claim_key(request_key=shared_rk, run_id=str(uuid.uuid4()))
    claim_b = svc.chat_upload_claim_key(request_key=shared_rk, run_id=str(uuid.uuid4()))
    assert claim_a != claim_b

    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid_accepted, name="accepted.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=claim_a,
    )
    await svc.validate_and_claim_request_attachments(
        [ChatUploadContext(upload_id=uid_rejected, name="rejected.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=claim_b,
    )

    # Confirm only Copy A's upload
    await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=uid_accepted, name="accepted.bin", size=512)],
        user_id=user_id,
        workspace_id=ws_id,
        claim_key=claim_a,
    )

    # Release Copy B's rejected claim under the same request_key
    await svc.release_request_chat_uploads(
        user_id=user_id,
        claim_key=claim_b,
        request_key=shared_rk,
        additional_context=[
            ChatUploadContext(upload_id=uid_rejected, name="rejected.bin", size=512)
        ],
    )

    row_acc = upload_env["ledger"].rows[uid_accepted]
    row_rej = upload_env["ledger"].rows[uid_rejected]
    assert row_acc["used_at"] is not None
    assert row_acc["expires_at"] is None

    # Rejected upload MUST remain unused, keep its expiry, and be cancellable!
    assert row_rej["used_at"] is None
    assert row_rej["expires_at"] is not None
    assert row_rej["pending_claims"] == {}
    cancelled = await svc.cancel_chat_upload(upload_id=uid_rejected, user_id=user_id)
    assert cancelled["state"] == "aborted"


@pytest.mark.asyncio
async def test_part_upload_rechecks_live_workspace_write_permission_before_slot_and_mid_stream(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """receive_chat_upload_part rejects workspace write revocation before reading, after
    waiting on _PART_UPLOAD_SEMAPHORE, and mid-stream before MinIO upload_part_bytes."""
    owner_id = "user-ws-owner"
    colleague_id = "user-ws-colleague"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": owner_id,
        "status": "running",
        "web_access": "rw",
        "colleagues": {colleague_id},
    }

    payload = b"part-permission-check-" * 128
    part_sha = hashlib.sha256(payload).hexdigest()

    up_pre = await svc.initiate_chat_upload(
        user_id=colleague_id, workspace_id=ws_id, filename="pre.bin", size_bytes=len(payload)
    )
    up_slot = await svc.initiate_chat_upload(
        user_id=colleague_id, workspace_id=ws_id, filename="slot.bin", size_bytes=len(payload)
    )
    up_mid = await svc.initiate_chat_upload(
        user_id=colleague_id, workspace_id=ws_id, filename="mid.bin", size_bytes=len(payload)
    )

    upload_part_spy = getattr(chat_upload_storage, "upload_part_bytes")
    upload_part_spy.reset_mock()

    # 1. Revoke write access BEFORE part call -> 403, zero MinIO calls
    upload_env["workspaces"][ws_id]["web_access"] = "ro"
    with pytest.raises(HTTPException) as pre_exc:
        await svc.receive_chat_upload_part(
            upload_id=up_pre["upload_id"],
            part_number=1,
            user_id=colleague_id,
            stream=_bytes_stream(payload),
            expected_part_sha256=part_sha,
        )
    assert pre_exc.value.status_code == 403
    assert upload_part_spy.call_count == 0

    # 2. Restore rw, block on semaphore, revoke WHILE waiting for slot -> 403, zero MinIO calls
    upload_env["workspaces"][ws_id]["web_access"] = "rw"
    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(svc, "_PART_UPLOAD_SEMAPHORE", sem)
    await sem.acquire()
    slot_task = asyncio.create_task(
        svc.receive_chat_upload_part(
            upload_id=up_slot["upload_id"],
            part_number=1,
            user_id=colleague_id,
            stream=_bytes_stream(payload),
            expected_part_sha256=part_sha,
        )
    )
    await asyncio.sleep(0.02)
    upload_env["workspaces"][ws_id]["web_access"] = "ro"
    sem.release()
    with pytest.raises(HTTPException) as slot_exc:
        await slot_task
    assert slot_exc.value.status_code == 403
    assert upload_part_spy.call_count == 0

    # 3. Restore rw, revoke MID-STREAM while reading slow body -> 403, zero MinIO calls, slot freed
    upload_env["workspaces"][ws_id]["web_access"] = "rw"
    chunk1_read = asyncio.Event()
    proceed_chunk2 = asyncio.Event()

    async def _mid_revoke_stream():
        half = len(payload) // 2
        yield payload[:half]
        chunk1_read.set()
        await proceed_chunk2.wait()
        yield payload[half:]

    mid_task = asyncio.create_task(
        svc.receive_chat_upload_part(
            upload_id=up_mid["upload_id"],
            part_number=1,
            user_id=colleague_id,
            stream=_mid_revoke_stream(),
            expected_part_sha256=part_sha,
        )
    )
    await asyncio.wait_for(chunk1_read.wait(), timeout=2.0)
    upload_env["workspaces"][ws_id]["web_access"] = "ro"
    proceed_chunk2.set()
    with pytest.raises(HTTPException) as mid_exc:
        await mid_task
    assert mid_exc.value.status_code == 403
    assert upload_part_spy.call_count == 0
    assert sem._value == 1


@pytest.mark.asyncio
async def test_materialize_charges_disk_only_for_missing_or_corrupt_files(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """materialize_chat_uploads skips disk charges and MinIO pulls when the exact SHA-verified
    file is already on disk, but charges disk and rejects when the file is missing or corrupt."""
    from src.server.services.computer_disk import CRITICAL_FREE_BYTES

    user_id = "user-disk-budget"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": user_id,
        "status": "running",
    }

    uid = str(uuid.uuid4())
    sample_bytes = b"verified-content-digest" * 1024
    sha_hex = hashlib.sha256(sample_bytes).hexdigest()
    obj_key = chat_upload_storage.build_chat_upload_key(ws_id, uid, "big.bin")
    upload_env["store"].objects[obj_key] = sample_bytes
    now = datetime.now(timezone.utc)
    row = {
        "upload_id": uid,
        "user_id": user_id,
        "workspace_id": ws_id,
        "filename": "big.bin",
        "safe_filename": "big.bin",
        "mime_type": "application/octet-stream",
        "size_bytes": len(sample_bytes),
        "part_size": svc.CHAT_UPLOAD_PART_SIZE,
        "part_count": 1,
        "object_key": obj_key,
        "store_upload_id": "mp-disk-1",
        "expected_sha256": sha_hex,
        "actual_sha256": sha_hex,
        "uploaded_parts": {},
        "pending_claims": {},
        "state": "ready",
        "error": None,
        "claim_token": None,
        "lease_until": None,
        "expires_at": None,
        "used_at": now,
        "cleaned_at": None,
        "created_at": now,
        "updated_at": now,
    }
    upload_env["ledger"].rows[uid] = row

    project = ProjectContext(workspace_id=ws_id, dir_name="DISK_WS")
    sandbox = _FakeSandbox()
    rel_path = svc.build_sandbox_upload_rel_path(row)
    abs_path = sandbox.normalize_path(rel_path, project=project)

    # Place valid file on disk and set free_bytes BELOW len(sample_bytes) above CRITICAL_FREE_BYTES
    sandbox.files[abs_path] = sample_bytes
    sandbox.free_bytes = CRITICAL_FREE_BYTES + 4

    pull_calls = 0

    async def _spy_pull_direct(
        sbx: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del sbx, layout, defer_dir_modes
        assert sweep_orphans is False
        nonlocal pull_calls
        pull_calls += 1
        sandbox.files[abs_path] = sample_bytes
        return {entries[0]["path"]: {"status": "ok"}}

    monkeypatch.setattr(
        svc,
        "pull_direct",
        create_autospec(_REAL_PULL_DIRECT, side_effect=_spy_pull_direct),
    )

    ctx = [ChatUploadContext(upload_id=uid, name="big.bin", size=len(sample_bytes))]

    # 1. Already present with matching size + SHA-256 -> succeeds with 0 pulls despite tight disk!
    notes = await svc.materialize_chat_uploads(
        ctx, sandbox=sandbox, user_id=user_id, workspace_id=ws_id, project=project
    )
    assert len(notes) == 1 and rel_path in notes[0]
    assert pull_calls == 0

    # 2. Same-size corrupt file on disk -> SHA mismatch detected, charges disk, fails under tight free space
    sandbox.files[abs_path] = b"X" * len(sample_bytes)
    with pytest.raises(RuntimeError, match="Not enough free disk space"):
        await svc.materialize_chat_uploads(
            ctx, sandbox=sandbox, user_id=user_id, workspace_id=ws_id, project=project
        )
    assert pull_calls == 0

    # 3. With enough free space for 1 copy (not 2!), duplicate descriptors [ctx, ctx] deduplicate
    #    target path, charge once, pull once, and return 2 ordered notes!
    sandbox.free_bytes = CRITICAL_FREE_BYTES + len(sample_bytes) + 2
    notes_dup = await svc.materialize_chat_uploads(
        ctx + ctx, sandbox=sandbox, user_id=user_id, workspace_id=ws_id, project=project
    )
    assert len(notes_dup) == 2
    assert pull_calls == 1
    assert sandbox.files[abs_path] == sample_bytes


@pytest.mark.asyncio
async def test_replay_projects_table_sourced_steering_returned_for_failed_steers() -> None:
    """project_turn and stub_lines emit table-sourced steering_returned items for permanently
    failed main-thread steers and invalidate lines_key when failed_steers change."""
    from src.server.database.conversation.replay_rows import ThreadRows
    from src.server.services.history.reader import TurnAnchor
    from src.server.services.history.replay import run_lane
    from src.server.services.history.replay.facts import RunFacts
    from src.server.services.history.replay.keys import lines_key
    from src.server.services.history.replay.stubs import _stub_items
    from src.server.services.history.replay.turn import Inputs, project_turn
    from src.server.services.history.slices import TurnSlice

    tid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    rk = str(uuid.uuid4())
    query_row = {
        "conversation_query_id": str(uuid.uuid4()),
        "turn_index": 0,
        "content": "Initial question",
        "type": "user",
        "created_at": datetime.now(timezone.utc),
        "metadata": {},
        "xmin": 1,
    }
    resp_row = {
        "conversation_response_id": rid,
        "turn_index": 0,
        "status": "completed",
        "created_at": datetime.now(timezone.utc),
        "metadata": {},
        "xmin": 2,
    }
    failed_entry = {
        "turn_index": 0,
        "reason": "attachment_failed",
        "error": "Attachment has expired or was removed",
        "error_code": "http_410",
        "request_key": rk,
        "content": "Steer with retired file",
        "attachments": [
            {
                "name": "retired.csv",
                "type": "file",
                "size": 4096,
                "status": "uploaded",
                "upload_id": str(uuid.uuid4()),
            }
        ],
        "timestamp": 1700000200.0,
        "job_id": str(uuid.uuid4()),
    }

    rows_without = ThreadRows(
        thread_id=tid,
        owner_id="u-1",
        thread={"latest_checkpoint_id": "cp-1"},
        queries=[query_row],
        responses_by_turn={0: resp_row},
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[],
    )
    rows_with = ThreadRows(
        thread_id=tid,
        owner_id="u-1",
        thread={"latest_checkpoint_id": "cp-1"},
        queries=[query_row],
        responses_by_turn={0: resp_row},
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[failed_entry],
    )

    inputs_without = Inputs.of(rows_without)
    inputs_with = Inputs.of(rows_with)
    anchor = TurnAnchor(
        turn_ordinal=0,
        turn_index=0,
        input_checkpoint_id="cp-0",
        tail_checkpoint_id="cp-1",
        ending_interrupts=[],
    )
    assert lines_key(inputs_without, 0, anchor, resp_row) != lines_key(
        inputs_with, 0, anchor, resp_row
    )

    turn_slice = TurnSlice(
        anchor=anchor,
        messages=[],
        new_ui_records=[],
    )
    from langgraph.checkpoint.memory import InMemorySaver
    from src.server.services.history.reader import CheckpointHistoryReader

    lane = run_lane.RunLane(CheckpointHistoryReader(InMemorySaver()), tid, RunFacts.of([]))
    projected_items, _ = project_turn(inputs_with, 0, turn_slice, resp_row, lane)
    returned_proj = [
        it for it in projected_items if it["event"] == "steering_returned"
    ]
    assert len(returned_proj) == 1
    assert returned_proj[0]["data"]["request_key"] == rk
    assert returned_proj[0]["data"]["reason"] == "attachment_failed"
    assert returned_proj[0]["data"]["error"] == "Attachment has expired or was removed"
    assert returned_proj[0]["data"]["turn_index"] == 0

    stub_out = _stub_items(tid, 0, [query_row], resp_row, None, [failed_entry])
    returned_stub = [it for it in stub_out if it["event"] == "steering_returned"]
    assert len(returned_stub) == 1
    assert returned_stub[0]["data"]["request_key"] == rk


# ---------------------------------------------------------------------------
# 9. Concurrency isolation (chunked fallback, direct transfer, explicit delete)
#    and durable failed steer retention / post-run_end status reconciliation
# ---------------------------------------------------------------------------


class _RealFsExecSandbox:
    """Sandbox double that executes real shell/python commands and file writes on ``tmp_path``."""

    class _Runtime:
        @staticmethod
        def exec(cmd: str, *args: Any, **kwargs: Any) -> _FakeExecResult:
            del args, kwargs
            import subprocess

            proc = subprocess.run(
                ["/bin/sh", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            res = _FakeExecResult(stdout=proc.stdout, exit_code=proc.returncode)
            res.stderr = proc.stderr
            return res

        @staticmethod
        def upload_file(chunk: bytes, dst: str, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            import os

            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(chunk)
            return True

    runtime = _Runtime()

    async def _runtime_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        kwargs.pop("total_timeout", None)
        kwargs.pop("retry_policy", None)
        return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.mark.asyncio
async def test_chunked_stream_fallback_isolates_temp_files_and_verifies_descriptor_identity(
    upload_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping _materialize_via_chunked_stream transfers of the same upload_id
    use per-invocation temp files, never delete each other's stage/part files, and
    reject any inode replacement between validation and publication."""
    import os

    uid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    raw_bytes = (b"CHUNK_A_" * (512 * 1024)) + (b"CHUNK_B_" * (256 * 1024))  # 6 MiB (2 chunks)
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()
    obj_key = chat_upload_storage.build_chat_upload_key(ws_id, uid, "overlap.bin")
    upload_env["store"].objects[obj_key] = raw_bytes

    def _iter_chunks(key: str, *, chunk_size: int = 4 * 1024 * 1024):
        buf = upload_env["store"].objects[key]
        for idx in range(0, len(buf), chunk_size):
            yield buf[idx : idx + chunk_size]

    monkeypatch.setattr(
        chat_upload_storage,
        "iter_stored_object_chunks",
        create_autospec(
            chat_upload_storage.iter_stored_object_chunks, side_effect=_iter_chunks
        ),
    )

    sbx = _RealFsExecSandbox()
    uploads_dir = tmp_path / "work" / "uploads"
    target_file = uploads_dir / f"overlap_{uid.replace('-', '')[:8]}.bin"

    # Synchronize Transfer 1 inside finalize_script right between fd validation and os.replace
    ready_file = tmp_path / "w1_validated.flag"
    release_file = tmp_path / "w1_release.flag"
    pause_expr = (
        f"import time; open({ repr(str(ready_file)) }, 'w').write(stage); "
        f"deadline = time.time() + 10.0\n"
        f"    while not os.path.exists({ repr(str(release_file)) }) and time.time() < deadline:\n"
        f"        time.sleep(0.02)"
    )

    t1 = asyncio.create_task(
        svc._materialize_via_chunked_stream(
            sbx,
            object_key=obj_key,
            abs_target_path=str(target_file),
            expected_size=len(raw_bytes),
            expected_sha256=expected_sha,
            upload_id=uid,
            pause_hook_expr=pause_expr,
        )
    )

    for _ in range(200):
        if ready_file.exists():
            break
        await asyncio.sleep(0.02)
    assert ready_file.exists(), "Transfer 1 did not reach validation pause point"
    w1_stage_path = ready_file.read_text().strip()
    assert os.path.exists(w1_stage_path)

    # Run Transfer 2 while Transfer 1 is paused between validation and publication;
    # force Transfer 2 to stream by temporarily unlinking target if needed (target does not exist yet!)
    assert not target_file.exists()
    t2_ok = await svc._materialize_via_chunked_stream(
        sbx,
        object_key=obj_key,
        abs_target_path=str(target_file),
        expected_size=len(raw_bytes),
        expected_sha256=expected_sha,
        upload_id=uid,
    )
    assert t2_ok is True
    # Transfer 2 must NOT have deleted Transfer 1's active stage file!
    assert os.path.exists(w1_stage_path)
    assert target_file.exists()
    assert target_file.stat().st_size == len(raw_bytes)
    assert hashlib.sha256(target_file.read_bytes()).hexdigest() == expected_sha

    # Release Transfer 1 -> completes cleanly, target remains exact
    release_file.write_text("go")
    t1_ok = await t1
    assert t1_ok is True
    assert not os.path.exists(w1_stage_path)
    assert target_file.read_bytes() == raw_bytes
    leftover_temps = [p.name for p in uploads_dir.iterdir() if p.name.startswith(".wsfiles-")]
    assert leftover_temps == []

    # 2. Prove descriptor-identity check: if stage_path is unlinked and replaced by a partial
    # file while paused between validation and os.replace, finalize_script exits 3 and refuses publication!
    target_swap = uploads_dir / "inode_swap_target.bin"
    swap_ready = tmp_path / "swap_ready.flag"
    swap_release = tmp_path / "swap_release.flag"
    swap_pause = (
        f"import time; open({ repr(str(swap_ready)) }, 'w').write(stage); "
        f"deadline = time.time() + 10.0\n"
        f"    while not os.path.exists({ repr(str(swap_release)) }) and time.time() < deadline:\n"
        f"        time.sleep(0.02)"
    )
    t_swap = asyncio.create_task(
        svc._materialize_via_chunked_stream(
            sbx,
            object_key=obj_key,
            abs_target_path=str(target_swap),
            expected_size=len(raw_bytes),
            expected_sha256=expected_sha,
            upload_id=uid,
            pause_hook_expr=swap_pause,
        )
    )
    for _ in range(200):
        if swap_ready.exists():
            break
        await asyncio.sleep(0.02)
    swap_stage = swap_ready.read_text().strip()
    os.unlink(swap_stage)
    with open(swap_stage, "wb") as f:
        f.write(b"partial-corrupt-bytes")
    swap_release.write_text("go")
    with pytest.raises(RuntimeError, match="verification failed"):
        await t_swap
    assert not target_swap.exists()


@pytest.mark.asyncio
async def test_direct_transfer_concurrent_downloads_and_restore_orphan_sweep_isolation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent wsfiles_transfer_runtime.pull downloads in the same workspace
    never delete each other's .wsfiles-* temp files, a concurrent full restore
    (sweep_orphans=True) preserves active flock-held downloads while cleaning unlocked orphans,
    and an independent OS process running _sweep_orphan_staging while _download_to_temp or
    _extract_member is paused between NamedTemporaryFile creation and flock acquisition
    cannot unlink the newly created temp file."""
    import io
    import subprocess
    import sys
    from pathlib import Path
    from ptc_agent.core.sandbox import wsfiles_transfer_runtime as rt

    blob_slow = b"SLOW_DIRECT_ATTACHMENT_" * (64 * 1024)
    blob_fast = b"FAST_DIRECT_ATTACHMENT_" * (32 * 1024)
    blob_restore = b"RESTORED_WORKSPACE_FILE_" * (16 * 1024)
    sha_slow = hashlib.sha256(blob_slow).hexdigest()
    sha_fast = hashlib.sha256(blob_fast).hexdigest()
    sha_restore = hashlib.sha256(blob_restore).hexdigest()

    slow_half_sent = threading.Event()
    release_slow = threading.Event()

    class _InMemoryBlobResponse:
        def __init__(self, body: bytes, *, pause_midway: bool = False) -> None:
            self._body = body
            self._offset = 0
            self._pause_midway = pause_midway
            self._paused_once = False
            self.status = 200

        def getheader(self, name: str, default: str | None = None) -> str | None:
            if name.lower() == "content-length":
                return str(len(self._body))
            return default

        def read(self, amt: int = 1048576) -> bytes:
            if self._offset >= len(self._body):
                return b""
            if self._pause_midway and not self._paused_once:
                half = max(1, len(self._body) // 2)
                chunk = self._body[self._offset : half]
                self._offset = half
                self._paused_once = True
                slow_half_sent.set()
                release_slow.wait(timeout=10.0)
                return chunk
            chunk = self._body[self._offset : self._offset + amt]
            self._offset += len(chunk)
            return chunk

        def close(self) -> None:
            pass

    def _fake_request(method: str, url: str, timeout_s: float) -> Any:
        del method, timeout_s
        if url.endswith("/slow.bin"):
            return _InMemoryBlobResponse(blob_slow, pause_midway=True)
        if url.endswith("/fast.bin"):
            return _InMemoryBlobResponse(blob_fast)
        if url.endswith("/restore.bin"):
            return _InMemoryBlobResponse(blob_restore)
        raise RuntimeError(f"Unexpected URL: {url}")

    monkeypatch.setattr(
        rt, "_request", create_autospec(rt._request, side_effect=_fake_request)
    )

    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    abandoned_orphan = ws_root / ".wsfiles-abandoned-orphan"
    abandoned_orphan.write_bytes(b"stale-unclaimed-temp")

    try:
        slow_spec = {
            "root": str(ws_root),
            "concurrency": 2,
            "timeout_s": 30,
            "sweep_orphans": False,
            "items": [
                {
                    "kind": "file",
                    "path": "work/uploads/slow.bin",
                    "size": len(blob_slow),
                    "mode": 0o644,
                    "sha256": sha_slow,
                    "url": "https://blob.local/slow.bin",
                }
            ],
        }
        fast_spec = {
            "root": str(ws_root),
            "concurrency": 2,
            "timeout_s": 30,
            "sweep_orphans": False,
            "items": [
                {
                    "kind": "file",
                    "path": "work/uploads/fast.bin",
                    "size": len(blob_fast),
                    "mode": 0o644,
                    "sha256": sha_fast,
                    "url": "https://blob.local/fast.bin",
                }
            ],
        }
        restore_spec = {
            "root": str(ws_root),
            "concurrency": 2,
            "timeout_s": 30,
            "sweep_orphans": True,
            "items": [
                {
                    "kind": "file",
                    "path": "notes/restore.bin",
                    "size": len(blob_restore),
                    "mode": 0o644,
                    "sha256": sha_restore,
                    "url": "https://blob.local/restore.bin",
                }
            ],
        }

        slow_fut = asyncio.create_task(asyncio.to_thread(rt.pull, slow_spec))
        assert await asyncio.to_thread(slow_half_sent.wait, 5.0)

        # While slow.bin is mid-download in ws_root, run both incremental fast_spec
        # (sweep_orphans=False) AND regular workspace restore_spec (sweep_orphans=True)!
        fast_out = await asyncio.to_thread(rt.pull, fast_spec)
        assert fast_out["results"]["work/uploads/fast.bin"]["status"] == "ok"
        assert abandoned_orphan.exists()

        restore_out = await asyncio.to_thread(rt.pull, restore_spec)
        assert restore_out["results"]["notes/restore.bin"]["status"] == "ok"
        assert not abandoned_orphan.exists()

        release_slow.set()
        slow_out = await slow_fut
        assert slow_out["results"]["work/uploads/slow.bin"]["status"] == "ok"

        p_slow = ws_root / "work" / "uploads" / "slow.bin"
        p_fast = ws_root / "work" / "uploads" / "fast.bin"
        p_rest = ws_root / "notes" / "restore.bin"
        assert p_slow.read_bytes() == blob_slow
        assert hashlib.sha256(p_slow.read_bytes()).hexdigest() == sha_slow
        assert p_fast.read_bytes() == blob_fast
        assert hashlib.sha256(p_fast.read_bytes()).hexdigest() == sha_fast
        assert p_rest.read_bytes() == blob_restore
        assert hashlib.sha256(p_rest.read_bytes()).hexdigest() == sha_restore
        assert [
            p.name
            for p in ws_root.iterdir()
            if p.name.startswith(".wsfiles-") and p.name != rt._SWEEP_LOCK_NAME
        ] == []

        # Cross-process test: pause _download_to_temp AND _extract_member right after
        # NamedTemporaryFile creation and BEFORE _acquire_temp_ownership acquires the per-file
        # flock, while an independent OS process runs _sweep_orphan_staging!
        rt_file = str(Path(rt.__file__).resolve())
        for mode in ("download", "extract"):
            created_evt = threading.Event()
            resume_create_evt = threading.Event()
            created_paths: list[str] = []

            def _on_temp_created(temp_path: str) -> None:
                created_paths.append(temp_path)
                created_evt.set()
                resume_create_evt.wait(timeout=5.0)

            monkeypatch.setattr(rt, "_TEMP_CREATED_BEFORE_LOCK_HOOK", _on_temp_created)
            stale_for_subproc = ws_root / f".wsfiles-stale-{mode}"
            stale_for_subproc.write_bytes(b"stale")

            if mode == "download":
                op_fut = asyncio.create_task(
                    asyncio.to_thread(
                        rt._download_to_temp, "https://blob.local/fast.bin", str(ws_root), 10.0
                    )
                )
            else:
                member_bytes = b"PACK_MEMBER_PAYLOAD_" * 128
                member_spec = {
                    "path": "notes/from_pack.bin",
                    "offset": 0,
                    "size": len(member_bytes),
                    "sha256": hashlib.sha256(member_bytes).hexdigest(),
                    "mode": 0o644,
                }
                op_fut = asyncio.create_task(
                    asyncio.to_thread(
                        rt._extract_member,
                        str(ws_root),
                        io.BytesIO(member_bytes),
                        member_spec,
                        200,
                    )
                )

            assert await asyncio.to_thread(created_evt.wait, 5.0)
            assert len(created_paths) == 1
            assert os.path.exists(created_paths[0])

            sub_code = (
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('rt', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "mod._sweep_orphan_staging(sys.argv[2], [], sweep_orphans=True)\n"
            )
            sweep_proc = subprocess.Popen(
                [sys.executable, "-c", sub_code, rt_file, str(ws_root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            await asyncio.sleep(0.08)
            assert os.path.exists(created_paths[0])
            resume_create_evt.set()

            op_res = await op_fut
            _, stderr_bytes = await asyncio.to_thread(sweep_proc.communicate, 5.0)
            assert sweep_proc.returncode == 0, stderr_bytes.decode("utf-8", "replace")
            assert not stale_for_subproc.exists()
            if mode == "download":
                tmp_name, got_sha, got_n, got_status = op_res
                assert os.path.exists(tmp_name)
                assert got_sha == sha_fast and got_n == len(blob_fast) and got_status == 200
                rt._unlink_quiet(tmp_name)
            else:
                assert op_res["status"] == "ok"
                assert (ws_root / "notes" / "from_pack.bin").read_bytes() == member_bytes
            monkeypatch.setattr(rt, "_TEMP_CREATED_BEFORE_LOCK_HOOK", None)

        with pytest.raises(OSError):
            rt._acquire_temp_ownership(str(ws_root / ".wsfiles-nonexistent-temp"))
    finally:
        release_slow.set()
        monkeypatch.setattr(rt, "_TEMP_CREATED_BEFORE_LOCK_HOOK", None)


@pytest.mark.asyncio
async def test_failed_steer_durable_replay_survives_outbox_retention_purge() -> None:
    """Failed steer user content, ordered attachments, request_key, turn_index, and error
    survive purge_terminal_jobs via conversation_responses.replay_facts without duplicate entries."""
    from src.server.database.conversation.replay_rows import ThreadRows
    from src.server.database.runs.outbox import (
        _failed_steer_entry,
        merge_failed_steer_entries,
    )
    from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor
    from src.server.services.history.replay import run_lane
    from src.server.services.history.replay.facts import RunFacts
    from src.server.services.history.replay.keys import lines_key
    from src.server.services.history.replay.turn import Inputs, project_turn
    from src.server.services.history.slices import TurnSlice
    from langgraph.checkpoint.memory import InMemorySaver

    tid = str(uuid.uuid4())
    run_0 = str(uuid.uuid4())
    run_1 = str(uuid.uuid4())
    job_held = str(uuid.uuid4())
    job_parked = str(uuid.uuid4())
    rk_held = str(uuid.uuid4())
    rk_parked = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    outbox_row_held = {
        "hook_outbox_id": job_held,
        "run_id": run_0,
        "created_at": now,
        "turn_index": 0,
        "payload": {
            "content": "Live held steer with 200MiB + inline PNG",
            "request_key": rk_held,
            "timestamp": 1700001000.0,
            "failed_run_id": run_0,
            "delivery_error": {
                "code": "http_403",
                "message": "Workspace write access was revoked",
            },
            "attachments": [
                {"name": "model_200m.bin", "type": "file", "size": 209715200, "status": "uploaded", "upload_id": "u-1"},
                {"name": "inline_chart.png", "type": "image", "size": 4096, "status": "uploaded", "upload_id": "u-2"},
            ],
        },
    }
    outbox_row_parked = {
        "hook_outbox_id": job_parked,
        "run_id": run_0,
        "created_at": now + timedelta(seconds=5),
        "turn_index": 0,
        "payload": {
            "content": "Parked steer failed after run_end",
            "request_key": rk_parked,
            "timestamp": 1700001005.0,
            "failed_run_id": run_0,
            "delivery_error": {
                "code": "http_410",
                "message": "Attachment has expired or was removed",
            },
            "attachments": [
                {"name": "deleted_doc.pdf", "type": "pdf", "size": 65536, "status": "uploaded", "upload_id": "u-3"},
            ],
        },
    }

    durable_entry_held = {**_failed_steer_entry(outbox_row_held), "turn_index": 0}
    durable_entry_parked = {**_failed_steer_entry(outbox_row_parked), "turn_index": 0}
    durable_rows = [
        {
            "turn_index": 0,
            # Include duplicate entry to verify idempotency deduplication by job_id / request_key
            "failed_steers": [durable_entry_held, durable_entry_held, durable_entry_parked],
        }
    ]

    # 1. Before outbox purge (both durable replay_facts AND hook_outbox rows exist): deduplicated to 2
    merged_before_purge = merge_failed_steer_entries(
        durable_rows, [outbox_row_held, outbox_row_parked]
    )
    assert len(merged_before_purge) == 2
    assert [m["request_key"] for m in merged_before_purge] == [rk_held, rk_parked]

    # 2. After purge_terminal_jobs deletes hook_outbox rows (outbox_rows = []): identical 2 entries!
    merged_after_purge = merge_failed_steer_entries(durable_rows, [])
    assert merged_after_purge == merged_before_purge

    # 3. Verify turn association stays on turn 0 even after turn 1 completes later
    q0 = {
        "conversation_query_id": str(uuid.uuid4()),
        "turn_index": 0,
        "content": "Turn 0 question",
        "type": "user",
        "created_at": now,
        "metadata": {},
        "xmin": 1,
    }
    q1 = {
        "conversation_query_id": str(uuid.uuid4()),
        "turn_index": 1,
        "content": "Turn 1 follow-up",
        "type": "user",
        "created_at": now + timedelta(minutes=1),
        "metadata": {},
        "xmin": 2,
    }
    r0 = {
        "conversation_response_id": run_0,
        "turn_index": 0,
        "status": "completed",
        "created_at": now,
        "metadata": {},
        "version": "10",
    }
    r1 = {
        "conversation_response_id": run_1,
        "turn_index": 1,
        "status": "completed",
        "created_at": now + timedelta(minutes=1),
        "metadata": {},
        "version": "11",
    }
    t_rows = ThreadRows(
        thread_id=tid,
        owner_id="u-owner",
        thread={"latest_checkpoint_id": "cp-2"},
        queries=[q0, q1],
        responses_by_turn={0: r0, 1: r1},
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=merged_after_purge,
    )
    inputs = Inputs.of(t_rows)
    assert len(inputs.failed_steers_by_turn.get(0, [])) == 2
    assert inputs.failed_steers_by_turn.get(1, []) == []

    anchor0 = TurnAnchor(0, 0, "cp-0", "cp-1", [])
    anchor1 = TurnAnchor(1, 1, "cp-1", "cp-2", [])
    lane = run_lane.RunLane(CheckpointHistoryReader(InMemorySaver()), tid, RunFacts.of([]))
    items0, _ = project_turn(inputs, 0, TurnSlice(anchor=anchor0), r0, lane)
    items1, _ = project_turn(inputs, 1, TurnSlice(anchor=anchor1), r1, lane)
    ret0 = [it["data"] for it in items0 if it["event"] == "steering_returned"]
    ret1 = [it["data"] for it in items1 if it["event"] == "steering_returned"]
    assert [d["request_key"] for d in ret0] == [rk_held, rk_parked]
    assert [len(d["attachments"]) for d in ret0] == [2, 1]
    assert ret1 == []
    assert lines_key(inputs, 0, anchor0, r0) == lines_key(
        Inputs.of(
            ThreadRows(
                thread_id=tid,
                owner_id="u-owner",
                thread={"latest_checkpoint_id": "cp-2"},
                queries=[q0, q1],
                responses_by_turn={0: r0, 1: r1},
                usages=[],
                provenance=[],
                run_facts=[],
                open_runs={},
                failed_steers=merged_before_purge,
            )
        ),
        0,
        anchor0,
        r0,
    )


@pytest.mark.asyncio
async def test_parked_steer_failure_after_run_end_surfaces_via_status_and_wakes_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When execute_steer fails verify_steered_chat_uploads with SteerAttachmentPermanentError
    after run_end, it settles outcome='failed', wakes the thread, and /status returns failed_steers
    for the owner while hiding them from non-owner readers."""
    from src.server.app.threads import messaging as messaging_app
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back import steer_executor

    tid = str(uuid.uuid4())
    ended_run_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    req_key = str(uuid.uuid4())
    owner_id = "user-steer-owner"
    ws_id = str(uuid.uuid4())

    job_row = {
        "hook_outbox_id": job_id,
        "idempotency_key": f"steer:{req_key}",
        "conversation_thread_id": tid,
        "run_id": ended_run_id,
        "status": "claimed",
        "attempts": 1,
        "payload": {
            "sender": "user",
            "target": "thread",
            "user_id": owner_id,
            "workspace_id": ws_id,
            "request_key": req_key,
            "content": "Please inspect the revoked file",
            "timestamp": 1700002000.0,
            "chat_uploads": [{"type": "chat_upload", "upload_id": str(uuid.uuid4()), "name": "revoked.bin", "size": 1024}],
            "attachments": [{"name": "revoked.bin", "type": "file", "size": 1024, "status": "uploaded"}],
        },
    }

    finished_calls: list[tuple[str, int, str, dict[str, Any] | None]] = []
    woken_threads: list[str] = []

    async def _fake_finish_claimed(
        jid: str, *, attempts: int, outcome: str, patch: dict[str, Any] | None = None
    ) -> bool:
        finished_calls.append((jid, attempts, outcome, patch))
        return True

    async def _fake_wake(thread_id: str) -> None:
        woken_threads.append(thread_id)

    monkeypatch.setattr(
        outbox_db,
        "get_steer_job",
        create_autospec(outbox_db.get_steer_job, return_value=job_row),
    )
    monkeypatch.setattr(
        outbox_db,
        "finish_claimed_steer",
        create_autospec(outbox_db.finish_claimed_steer, side_effect=_fake_finish_claimed),
    )
    orig_get_latest_attempt = tl_db.get_latest_attempt
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            orig_get_latest_attempt,
            return_value={"conversation_response_id": ended_run_id, "status": "completed"},
        ),
    )
    monkeypatch.setattr(
        steer_executor,
        "_owner",
        create_autospec(steer_executor._owner, return_value=(owner_id, ws_id)),
    )
    monkeypatch.setattr(
        svc,
        "verify_steered_chat_uploads",
        create_autospec(
            svc.verify_steered_chat_uploads,
            side_effect=svc.SteerAttachmentPermanentError(
                "http_410", "Attachment has expired or was removed"
            ),
        ),
    )
    monkeypatch.setattr(
        steer_executor,
        "publish_cleared_wake",
        create_autospec(steer_executor.publish_cleared_wake, side_effect=_fake_wake),
    )

    await steer_executor.execute_steer(
        {
            "hook_outbox_id": job_id,
            "idempotency_key": f"steer:{req_key}",
            "conversation_thread_id": tid,
            "attempts": 1,
        }
    )

    assert len(finished_calls) == 1
    assert finished_calls[0][0] == job_id
    assert finished_calls[0][2] == "failed"
    assert finished_calls[0][3] == {
        "failed_run_id": ended_run_id,
        "delivery_error": {
            "code": "http_410",
            "message": "Attachment has expired or was removed",
        },
    }
    assert woken_threads == [tid]

    # Verify GET /api/v1/threads/{thread_id}/status exposes failed_steers to owner and [] to non-owner
    expected_failed = [
        {
            "job_id": job_id,
            "request_key": req_key,
            "content": "Please inspect the revoked file",
            "reason": "attachment_failed",
            "error": "Attachment has expired or was removed",
            "error_code": "http_410",
            "failed_run_id": ended_run_id,
            "turn_index": 0,
            "attachments": [{"name": "revoked.bin", "type": "file", "size": 1024, "status": "uploaded"}],
        }
    ]
    monkeypatch.setattr(
        outbox_db,
        "list_pending_steers",
        create_autospec(outbox_db.list_pending_steers, return_value=[]),
    )
    monkeypatch.setattr(
        outbox_db,
        "list_failed_steers_for_status",
        create_autospec(
            outbox_db.list_failed_steers_for_status, return_value=expected_failed
        ),
    )
    from src.dalton import settings as dalton_settings
    from src.dalton import sharing as dalton_sharing
    from src.server.database import conversation as conv_db
    from src.server.services import thread_status as ts_mod

    orig_get_thread_auth_meta = conv_db.get_thread_auth_meta
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_thread_auth_meta,
            return_value={"user_id": owner_id, "is_shared": False, "workspace_owner_id": owner_id, "workspace_id": str(uuid.uuid4())},
        ),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_thread_reader",
        create_autospec(dalton_sharing.require_thread_reader, return_value=None),
    )
    monkeypatch.setattr(
        ts_mod,
        "read_thread_runtime_status",
        create_autospec(
            ts_mod.read_thread_runtime_status,
            return_value={"thread_id": tid, "can_reconnect": False, "status": "completed", "latest_turn_index": 0},
        ),
    )
    owner_status = await messaging_app.get_thread_status(tid, x_user_id=owner_id, fields=None)
    assert owner_status["failed_steers"] == expected_failed

    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_thread_auth_meta,
            return_value={"user_id": owner_id, "is_shared": True, "workspace_owner_id": owner_id, "workspace_id": str(uuid.uuid4())},
        ),
    )
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            orig_get_latest_attempt,
            return_value={"status": "completed", "created_at": datetime.now(timezone.utc)},
        ),
    )
    monkeypatch.setattr(
        conv_db,
        "get_latest_turn_index",
        create_autospec(conv_db.get_latest_turn_index, return_value=0),
    )
    non_owner_status = await messaging_app.get_thread_status(tid, x_user_id="colleague-viewer", fields=None)
    assert non_owner_status["failed_steers"] == []


@pytest.mark.asyncio
async def test_explicit_file_delete_interleaved_with_recovery_never_resurrects_deleted_upload(
    upload_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit file deletion interleaved with rematerialize_workspace_chat_uploads (both when
    recovery reads ready before delete acquires path_write_lock and when delete pauses right
    after rm) never resurrects the deleted file, while failed rm preserves recoverability."""
    from src.server.app.workspace_files import crud as ws_crud

    owner = "user-del-race"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
    }

    async def _seed_accepted(filename: str, content: bytes) -> tuple[str, str]:
        created = await svc.initiate_chat_upload(
            user_id=owner, workspace_id=ws_id, filename=filename, size_bytes=len(content)
        )
        uid = created["upload_id"]
        await svc.receive_chat_upload_part(
            upload_id=uid, part_number=1, user_id=owner, stream=_bytes_stream(content)
        )
        await svc.complete_chat_upload(upload_id=uid, user_id=owner)
        await svc.confirm_request_chat_uploads(
            [ChatUploadContext(upload_id=uid, name=filename, size=len(content))],
            user_id=owner,
            workspace_id=ws_id,
        )
        row = upload_env["ledger"].rows[uid]
        return uid, svc.build_sandbox_upload_rel_path(row)

    uid_a, rel_a = await _seed_accepted("race_a.bin", b"A" * 4096)
    uid_b, rel_b = await _seed_accepted("race_b.bin", b"B" * 4096)
    uid_fail_rm, rel_fail_rm = await _seed_accepted("keep_on_rm_fail.bin", b"K" * 4096)

    layout = WorkspaceLayout(root="/home/workspace", dir_name="RaceWS")
    abs_a = f"{layout.workspace}/{rel_a}"
    abs_b = f"{layout.workspace}/{rel_b}"
    abs_fail = f"{layout.workspace}/{rel_fail_rm}"

    class _CoordinatedSandbox(_FakeSandbox):
        def __init__(self) -> None:
            super().__init__(sandbox_id="sbx-race")
            self._path_locks: dict[str, asyncio.Lock] = {}
            self.rm_should_fail: set[str] = set()
            self.pause_after_rm_for: str | None = None
            self.paused_after_rm = asyncio.Event()
            self.resume_after_rm = asyncio.Event()

        @asynccontextmanager
        async def path_write_lock(self, normalized_path: str):
            lock = self._path_locks.setdefault(normalized_path, asyncio.Lock())
            self.locked_paths.append(normalized_path)
            async with lock:
                yield

        async def execute_bash_command(self, cmd: str) -> dict[str, Any]:
            import shlex as _shlex

            tokens = _shlex.split(cmd)
            targets = [t for t in tokens[2:] if not t.startswith("-")]
            for t in targets:
                if t in self.rm_should_fail:
                    return {"success": False, "stderr": "permission denied"}
            for t in targets:
                self.files.pop(t, None)
            if self.pause_after_rm_for and self.pause_after_rm_for in targets:
                self.paused_after_rm.set()
                await self.resume_after_rm.wait()
            return {"success": True, "stdout": "", "stderr": ""}

    sbx = _CoordinatedSandbox()
    sbx.files[abs_a] = b"A" * 4096
    sbx.files[abs_b] = b"B" * 4096
    sbx.files[abs_fail] = b"K" * 4096

    async def _pull_into_fake_sbx(
        sandbox: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del defer_dir_modes, sweep_orphans
        for e in entries:
            grant_file = e.get("grant_file")
            if grant_file and grant_file not in sandbox.files:
                return {e["path"]: {"status": "superseded"}}
            if grant_file:
                sandbox.files.pop(grant_file, None)
            p = f"{layout.workspace}/{e['path']}"
            sandbox.files[p] = b"RESTORED"
        return {entries[0]["path"]: {"status": "ok"}}

    monkeypatch.setattr(
        svc,
        "pull_direct",
        create_autospec(_REAL_PULL_DIRECT, side_effect=_pull_into_fake_sbx),
    )

    # Scenario A: Recovery reads list_retained_workspace_uploads(ready) and probes missing BEFORE
    # acquiring path_write_lock; while recovery waits on path_write_lock, explicit delete removes
    # abs_a and retires uid_a; recovery resumes and must NOT restore abs_a!
    del sbx.files[abs_a]  # simulate missing on disk when recovery probes
    lock_a = sbx._path_locks.setdefault(abs_a, asyncio.Lock())
    await lock_a.acquire()

    orig_probe_missing = svc._probe_missing_sandbox_paths
    probe_done = asyncio.Event()

    async def _notify_probe_missing(sandbox: Any, abs_paths: list[str]) -> set[str]:
        res = await orig_probe_missing(sandbox, abs_paths)
        probe_done.set()
        return res

    monkeypatch.setattr(
        svc,
        "_probe_missing_sandbox_paths",
        create_autospec(orig_probe_missing, side_effect=_notify_probe_missing),
    )

    rec_task_a = asyncio.create_task(
        svc.rematerialize_workspace_chat_uploads(ws_id, sbx, layout=layout)
    )
    await asyncio.wait_for(probe_done.wait(), timeout=2.0)

    # Explicit delete of rel_a runs while recovery is blocked waiting for lock_a
    await svc.begin_delete_workspace_chat_uploads(
        workspace_id=ws_id, caller_user_id=owner, candidate_client_paths=[rel_a]
    )
    sbx.files.pop(abs_a, None)
    await svc.retire_deleted_workspace_chat_uploads(
        workspace_id=ws_id, caller_user_id=owner, deleted_client_paths=[rel_a]
    )
    lock_a.release()

    res_a = await rec_task_a
    assert res_a == {"restored": 0, "errors": 0}
    assert abs_a not in sbx.files
    assert upload_env["ledger"].rows[uid_a]["state"] == "aborted"
    assert upload_env["ledger"].rows[uid_a]["object_key"] not in upload_env["store"].objects

    # Scenario B: Explicit delete fences uid_b, runs rm (removing abs_b), and pauses IMMEDIATELY
    # after rm before retire_deleted_workspace_chat_uploads; a second worker runs recovery (even
    # with lock fallback) and must NOT recreate abs_b!
    sbx.pause_after_rm_for = abs_b

    async def _run_delete_b() -> None:
        await svc.begin_delete_workspace_chat_uploads(
            workspace_id=ws_id, caller_user_id=owner, candidate_client_paths=[rel_b]
        )
        res = await sbx.execute_bash_command(f"rm -f {abs_b}")
        assert res["success"] is True
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_id, caller_user_id=owner, deleted_client_paths=[rel_b]
        )

    del_task_b = asyncio.create_task(_run_delete_b())
    await asyncio.wait_for(sbx.paused_after_rm.wait(), timeout=2.0)
    assert abs_b not in sbx.files
    assert upload_env["ledger"].rows[uid_b]["error"] == "explicit_delete_pending"

    # Worker 2 runs recovery while Worker 1 is paused right after rm
    res_during_pause = await svc.rematerialize_workspace_chat_uploads(
        ws_id, sbx, layout=layout
    )
    assert res_during_pause == {"restored": 0, "errors": 0}
    assert abs_b not in sbx.files

    sbx.resume_after_rm.set()
    await del_task_b
    assert abs_b not in sbx.files
    assert upload_env["ledger"].rows[uid_b]["state"] == "aborted"
    assert upload_env["ledger"].rows[uid_b]["object_key"] not in upload_env["store"].objects

    # Scenario C: Failed rm clears the fence via abort_delete_workspace_chat_uploads so the upload
    # remains recoverable when later missing on disk!
    await svc.begin_delete_workspace_chat_uploads(
        workspace_id=ws_id, caller_user_id=owner, candidate_client_paths=[rel_fail_rm]
    )
    assert upload_env["ledger"].rows[uid_fail_rm]["error"] == "explicit_delete_pending"
    await svc.abort_delete_workspace_chat_uploads(
        workspace_id=ws_id, caller_user_id=owner, failed_client_paths=[rel_fail_rm]
    )
    assert upload_env["ledger"].rows[uid_fail_rm]["error"] is None
    del sbx.files[abs_fail]
    res_after_abort = await svc.rematerialize_workspace_chat_uploads(
        ws_id, sbx, layout=layout
    )
    assert res_after_abort == {"restored": 1, "errors": 0}
    assert abs_fail in sbx.files
    assert ws_crud is not None

    # Scenario D: Restore pauses AFTER its final is_upload_retained_for_recovery DB check
    # and BEFORE remote publication while explicit delete completes (and without any post-copy rm):
    # the grant revocation prevents target publication so abs_fail is NOT recreated!
    del sbx.files[abs_fail]
    after_db_check_evt = asyncio.Event()
    resume_publication_evt = asyncio.Event()

    async def _pause_after_final_db_check(_uid: str, _abs_target: str) -> None:
        after_db_check_evt.set()
        await resume_publication_evt.wait()

    monkeypatch.setattr(svc, "_AFTER_FINAL_RECOVERY_DB_CHECK_HOOK", _pause_after_final_db_check)
    try:
        paused_restore_task = asyncio.create_task(
            svc.rematerialize_workspace_chat_uploads(ws_id, sbx, layout=layout)
        )
        await asyncio.wait_for(after_db_check_evt.wait(), timeout=2.0)

        # While restore is paused AFTER its final DB check, explicit delete runs and retires uid_fail_rm
        await svc.begin_delete_workspace_chat_uploads(
            workspace_id=ws_id,
            caller_user_id=owner,
            candidate_client_paths=[rel_fail_rm],
            sandbox=sbx,
            layout=layout,
        )
        sbx.files.pop(abs_fail, None)
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_id,
            caller_user_id=owner,
            deleted_client_paths=[rel_fail_rm],
            sandbox=sbx,
            layout=layout,
        )
        resume_publication_evt.set()

        paused_res = await paused_restore_task
        assert paused_res == {"restored": 0, "errors": 0}
        assert abs_fail not in sbx.files
        assert upload_env["ledger"].rows[uid_fail_rm]["state"] == "aborted"
    finally:
        resume_publication_evt.set()
        monkeypatch.setattr(svc, "_AFTER_FINAL_RECOVERY_DB_CHECK_HOOK", None)


class _RealPosixSandbox:
    """Sandbox double backed by a real local POSIX directory executing actual shell/python commands."""

    provider = _FakeSandboxProvider()

    def __init__(self, root_dir: str, dir_name: str = "PosixWS") -> None:
        self.sandbox_id = "sbx-real-posix"
        self.root_dir = os.path.realpath(root_dir)
        self.dir_name = dir_name
        self.config = type(
            "Cfg",
            (),
            {"filesystem": type("FS", (), {"working_directory": self.root_dir})()},
        )()
        self._path_locks: dict[str, asyncio.Lock] = {}
        self.exec_interceptor: Any = None
        parent_sbx = self

        class _BoundRuntime:
            @staticmethod
            async def exec(cmd: str, **kwargs: Any) -> _FakeExecResult:
                return await parent_sbx._runtime_call("exec", cmd, **kwargs)

            @staticmethod
            async def upload_file(data: bytes, dest: str, **kwargs: Any) -> _FakeExecResult:
                return await parent_sbx._runtime_call("upload_file", data, dest, **kwargs)

        self.runtime = _BoundRuntime()

    def workspace(self, project: ProjectContext | None = None) -> WorkspaceLayout:
        dn = (project.dir_name if project is not None else None) or self.dir_name
        return WorkspaceLayout(root=self.root_dir, dir_name=dn)

    def normalize_path(self, path: str, project: ProjectContext | None = None) -> str:
        ws_root = self.workspace(project).workspace
        if path == ws_root or path.startswith(f"{ws_root}/"):
            return path
        if path == self.root_dir or path.startswith(f"{self.root_dir}/"):
            return path
        return f"{ws_root}/{path.lstrip('/')}"

    def validate_path(self, filepath: str, project: ProjectContext | None = None) -> bool:
        ws_root = self.workspace(project).workspace
        target = (
            filepath
            if filepath.startswith(f"{ws_root}/")
            else self.normalize_path(filepath, project)
        )
        return target.startswith(f"{ws_root}/") and ".." not in target.split("/")

    @asynccontextmanager
    async def path_write_lock(self, normalized_path: str):
        lock = self._path_locks.setdefault(normalized_path, asyncio.Lock())
        async with lock:
            yield

    async def execute_bash_command(self, cmd: str, **_kwargs: Any) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return {
            "success": proc.returncode == 0,
            "stdout": out_b.decode("utf-8", errors="replace"),
            "stderr": err_b.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode or 0,
        }

    async def _runtime_call(self, fn: Any, *args: Any, **kwargs: Any) -> _FakeExecResult:
        del kwargs
        if callable(self.exec_interceptor):
            intercepted = await self.exec_interceptor(fn, *args)
            if intercepted is not None:
                return intercepted
        if fn == getattr(self.runtime, "upload_file", None) or (
            len(args) == 2 and isinstance(args[0], (bytes, bytearray))
        ):
            data, dest = bytes(args[0]), str(args[1])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            return _FakeExecResult(stdout="", stderr="", exit_code=0)
        cmd = str(args[0]) if args else ""
        if cmd.startswith("df -Pk"):
            avail_kb = (20 * 1024 * 1024 * 1024) // 1024
            df_out = (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"overlay 52428800 1048576 {avail_kb} 5% {self.root_dir}\n"
            )
            return _FakeExecResult(stdout=df_out, stderr="", exit_code=0)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return _FakeExecResult(
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
            exit_code=int(proc.returncode if proc.returncode is not None else 1),
        )


async def _wait_for_flag(path: Any, timeout: float = 10.0) -> str:
    p = str(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(p):
            content = open(p, "r", encoding="utf-8").read().strip()
            if content:
                return content
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for coordination flag {p}")


@pytest.mark.asyncio
async def test_target_lock_inode_stability_and_multiprocess_flock_coordination(
    tmp_path,
) -> None:
    """Verify across independent OS processes using real flock and real publication/revocation scripts:
    1. A single stable .wsfiles-tlock-* inode is preserved across _create_restore_grant,
       _cleanup_restore_grant, _revoke_restore_grants_in_sandbox, _build_stream_finalize_script,
       and wsfiles_transfer_runtime._place (attempt cleanup never unlinks the shared target lock).
    2. Concrete ordering: Restore A opens the target lock; Restore B's _cleanup_restore_grant runs;
       Restore A acquires LOCK_EX on the unchanged inode, checks its valid grant, and pauses before
       publication; explicit deletion and overlapping Restore C both block on the same lock inode;
       independent Target 2 remains usable and publishes while Target 1's lock is held;
       competing critical sections never overlap; completed delete leaves Target 1 absent after
       all surviving attempts exit.
    3. Termination (SIGKILL) of an attempt while holding LOCK_EX releases the kernel lock on the
       stable inode so subsequent revocation/restore operations proceed cleanly."""
    import json
    import signal
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[3])
    src_dir = os.path.join(repo_root, "src")
    subproc_env_base = {
        **os.environ,
        "PYTHONPATH": f"{src_dir}:{repo_root}:{os.path.join(repo_root, 'libs', 'chartkit')}:{os.path.join(repo_root, 'libs', 'market-protocol')}",
    }

    direct_worker_code = (
        "import json, os, sys\n"
        "from ptc_agent.core.sandbox import wsfiles_transfer_runtime as rt\n"
        "tmp_p, final_p, item_json, out_p = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]\n"
        "item = json.loads(item_json)\n"
        "res = rt._place(tmp_p, final_p, item, 200)\n"
        "with open(out_p, 'w', encoding='utf-8') as f:\n"
        "    json.dump(res, f)\n"
        "sys.exit(0 if res.get('status') == 'ok' else (5 if res.get('status') == 'superseded' else 1))\n"
    )

    for mode in ("direct", "stream"):
        mode_dir = tmp_path / f"lock_coord_{mode}"
        mode_dir.mkdir(parents=True, exist_ok=True)
        sbx = _RealPosixSandbox(str(mode_dir), dir_name="ws")
        layout = sbx.workspace()
        os.makedirs(f"{layout.workspace}/work/uploads", exist_ok=True)

        uid_1 = str(uuid.uuid4())
        uid_2 = str(uuid.uuid4())
        row_1 = {"upload_id": uid_1, "filename": "model_1.bin"}
        row_2 = {"upload_id": uid_2, "filename": "model_2.bin"}
        rel_1 = svc.build_sandbox_upload_rel_path(row_1)
        rel_2 = svc.build_sandbox_upload_rel_path(row_2)
        abs_1 = f"{layout.workspace}/{rel_1}"
        abs_2 = f"{layout.workspace}/{rel_2}"

        payload_1 = f"PAYLOAD_ONE_{mode}_".encode("utf-8") * 512
        payload_2 = f"PAYLOAD_TWO_{mode}_".encode("utf-8") * 512
        sha_1 = hashlib.sha256(payload_1).hexdigest()
        sha_2 = hashlib.sha256(payload_2).hexdigest()

        # Establish 3 overlapping restore grants for Target 1 (A, B, C) and 1 for Target 2
        grant_a, lock_1 = await svc._create_restore_grant(sbx, abs_1, uid_1, "att-a")
        grant_b, lock_1_b = await svc._create_restore_grant(sbx, abs_1, uid_1, "att-b")
        grant_c, lock_1_c = await svc._create_restore_grant(sbx, abs_1, uid_1, "att-c")
        grant_t2, lock_2 = await svc._create_restore_grant(sbx, abs_2, uid_2, "att-t2")

        assert lock_1 == lock_1_b == lock_1_c
        assert lock_1 != lock_2
        assert os.path.exists(lock_1)
        initial_lock_ino = os.stat(lock_1).st_ino
        assert os.path.exists(grant_a) and os.path.exists(grant_b) and os.path.exists(grant_c)

        def _spawn_publish(
            tag: str,
            target_abs: str,
            payload: bytes,
            sha_hex: str,
            grant_p: str,
            lock_p: str,
            extra_env: dict[str, str],
        ) -> tuple[subprocess.Popen, str]:
            stage_p = str(mode_dir / f"stage_{tag}.bin")
            with open(stage_p, "wb") as f:
                f.write(payload)
            out_json = str(mode_dir / f"out_{tag}.json")
            env = {**subproc_env_base, **extra_env}
            if mode == "direct":
                item = {
                    "kind": "file",
                    "size": len(payload),
                    "mode": 0o644,
                    "sha256": sha_hex,
                    "grant_file": grant_p,
                    "lock_file": lock_p,
                }
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        direct_worker_code,
                        stage_p,
                        target_abs,
                        json.dumps(item),
                        out_json,
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            else:
                fin_cmd = svc._build_stream_finalize_script(
                    stage_p,
                    target_abs,
                    len(payload),
                    sha_hex,
                    grant_path=grant_p,
                    lock_path=lock_p,
                )
                proc = subprocess.Popen(
                    fin_cmd,
                    shell=True,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            return proc, stage_p

        open_ready_a = mode_dir / "open_ready_a.flag"
        open_resume_a = mode_dir / "open_resume_a.flag"
        grant_ready_a = mode_dir / "grant_ready_a.flag"
        grant_resume_a = mode_dir / "grant_resume_a.flag"

        proc_a, stage_a = _spawn_publish(
            "a",
            abs_1,
            payload_1,
            sha_1,
            grant_a,
            lock_1,
            {
                "WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN": f"{open_ready_a}:{open_resume_a}",
                "WSFILES_TEST_PAUSE_AFTER_GRANT_CHECK": f"{grant_ready_a}:{grant_resume_a}",
            },
        )
        try:
            # 1. Restore A has opened the target lock (before flock)
            open_info_a = await _wait_for_flag(open_ready_a)
            assert int(open_info_a.split(":")[1]) == initial_lock_ino

            # 2. Another attempt's cleanup (Attempt B) runs while A has the lock open
            await svc._cleanup_restore_grant(sbx, grant_b, lock_1)
            assert not os.path.exists(grant_b)
            assert os.path.exists(grant_a)
            assert os.path.exists(grant_c)
            # Lock pathname and inode MUST remain unchanged
            assert os.path.exists(lock_1)
            assert os.stat(lock_1).st_ino == initial_lock_ino

            # 3. Restore A acquires the lock, checks its valid grant, and pauses before publication
            open_resume_a.write_text("go", encoding="utf-8")
            await _wait_for_flag(grant_ready_a)
            a_cs_Active_ns = time.monotonic_ns()
            assert not os.path.exists(grant_a)  # consumed under LOCK_EX

            # 4. Start explicit deletion (revocation + unlink_target) in an independent OS process:
            #    it must open the SAME lock inode and block on LOCK_EX while A is paused!
            del_in_lock_ready = mode_dir / "del_in_lock_ready.flag"
            del_in_lock_resume = mode_dir / "del_in_lock_resume.flag"
            del_task = asyncio.create_task(
                svc._revoke_restore_grants_in_sandbox(
                    sbx,
                    layout,
                    [row_1],
                    unlink_target=True,
                    pause_in_lock_expr=(
                        f"open({str(del_in_lock_ready)!r}, 'w').write(f'{{os.getpid()}}:{{os.fstat(fd).st_ino}}:{time.monotonic_ns()}'); "
                        f"dl = time.monotonic() + 15.0\n"
                        f"        while not os.path.exists({str(del_in_lock_resume)!r}) and time.monotonic() < dl:\n"
                        f"            time.sleep(0.01)"
                    ),
                )
            )
            await asyncio.sleep(0.08)
            assert not del_in_lock_ready.exists(), "Delete entered critical section while Restore A held LOCK_EX!"
            assert not del_task.done()

            # 5. While Target 1's lock is held by Restore A and contended by Delete,
            #    independent Target 2 restores and publishes immediately without blocking!
            proc_t2, _ = _spawn_publish(
                "t2", abs_2, payload_2, sha_2, grant_t2, lock_2, {}
            )
            rc_t2 = await asyncio.to_thread(proc_t2.wait, 5.0)
            assert rc_t2 == 0
            assert os.path.isfile(abs_2)
            assert open(abs_2, "rb").read() == payload_2
            assert not del_in_lock_ready.exists()

            # 6. Resume Restore A -> Restore A publishes and releases LOCK_EX -> Delete immediately
            #    acquires the SAME lock inode (non-overlapping critical section!)
            a_release_ns = time.monotonic_ns()
            grant_resume_a.write_text("go", encoding="utf-8")
            rc_a = await asyncio.to_thread(proc_a.wait, 5.0)
            assert rc_a == 0

            del_info = await _wait_for_flag(del_in_lock_ready)
            del_entered_ns = time.monotonic_ns()
            del_parts = del_info.split(":")
            assert int(del_parts[1]) == initial_lock_ino
            assert del_entered_ns >= a_release_ns > a_cs_Active_ns

            # 7. While Delete holds LOCK_EX, overlapping Restore Attempt C starts and blocks on LOCK_EX
            grant_ready_c = mode_dir / "grant_ready_c.flag"
            proc_c, stage_c = _spawn_publish(
                "c",
                abs_1,
                payload_1,
                sha_1,
                grant_c,
                lock_1,
                {"WSFILES_TEST_PAUSE_AFTER_GRANT_CHECK": f"{grant_ready_c}:{grant_resume_a}"},
            )
            await asyncio.sleep(0.08)
            assert proc_c.poll() is None, "Restore C did not block on Delete's LOCK_EX!"

            # Release Delete: it revokes grant_c and unlinks abs_1 (which A had just published)
            del_in_lock_resume.write_text("go", encoding="utf-8")
            await del_task

            # Restore C now acquires LOCK_EX, sees grant_c was revoked, and exits superseded (5)
            rc_c = await asyncio.to_thread(proc_c.wait, 5.0)
            assert rc_c == 5
            assert not os.path.exists(stage_c)

            # After every surviving attempt (A, B, C) and Delete have exited, Target 1 MUST be absent!
            assert not os.path.exists(abs_1)
            assert os.path.isfile(abs_2)
            assert os.stat(lock_1).st_ino == initial_lock_ino

            # 8. Termination (SIGKILL) of an attempt while holding LOCK_EX on the stable inode:
            grant_kill, _ = await svc._create_restore_grant(sbx, abs_1, uid_1, "att-kill")
            kill_ready = mode_dir / "kill_ready.flag"
            kill_never = mode_dir / "kill_never.flag"
            proc_kill, stage_kill = _spawn_publish(
                "kill",
                abs_1,
                payload_1,
                sha_1,
                grant_kill,
                lock_1,
                {"WSFILES_TEST_PAUSE_AFTER_GRANT_CHECK": f"{kill_ready}:{kill_never}"},
            )
            await _wait_for_flag(kill_ready)
            os.kill(proc_kill.pid, signal.SIGKILL)
            await asyncio.to_thread(proc_kill.wait, 5.0)
            if os.path.exists(stage_kill):
                os.unlink(stage_kill)

            # Kernel releases flock on SIGKILL; subsequent delete revocation acquires LOCK_EX cleanly
            await svc._revoke_restore_grants_in_sandbox(
                sbx, layout, [row_1], unlink_target=True
            )
            assert not os.path.exists(abs_1)
            assert os.stat(lock_1).st_ino == initial_lock_ino
        finally:
            for p in (proc_a,):
                if p.poll() is None:
                    p.kill()


@pytest.mark.asyncio
async def test_crud_delete_requires_remote_grant_revocation_and_retries_cleanly_across_processes(
    tmp_path,
    upload_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the actual CRUD delete_workspace_files handler with an independent restore process
    already past its final DB check:
    1. Nonzero revocation command exit in begin_delete_workspace_chat_uploads reports path in errors
       (not deleted) and preserves explicit_delete_pending while ordinary rm remains available.
    2. Transport failure on revocation reports path in errors (not deleted) and preserves
       explicit_delete_pending without clearing the restriction on uncertain timeout.
    3. Real POSIX unlink PermissionError (chmod 0500 on .wsfiles-grants-*) while ordinary rm in
       work/uploads remains available causes revocation to fail, reporting path in errors (not deleted)
       and preserving explicit_delete_pending.
    4. Retire-stage failure after rm reports path in errors (not deleted), preserves
       explicit_delete_pending, and does not prematurely delete the stored S3 object.
    5. Subsequent retry of delete_workspace_files succeeds, leaves the file absent after the
       restore process exits, retires the DB row, and deletes the S3 object."""
    import json
    import shlex
    import subprocess
    from pathlib import Path
    from src.server.app.workspace_files import crud as ws_crud
    from src.server.database import workspace as ws_db

    owner = "user-crud-revoke"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
        "dir_name": "CrudWS",
    }

    payload = b"CRUD_DELETE_REVOKE_SAFETY_" * 512
    sha_hex = hashlib.sha256(payload).hexdigest()
    created = await svc.initiate_chat_upload(
        user_id=owner,
        workspace_id=ws_id,
        filename="protected_dataset.bin",
        size_bytes=len(payload),
    )
    uid = created["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid, part_number=1, user_id=owner, stream=_bytes_stream(payload)
    )
    await svc.complete_chat_upload(upload_id=uid, user_id=owner)
    await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=uid, name="protected_dataset.bin", size=len(payload))],
        user_id=owner,
        workspace_id=ws_id,
    )
    row = upload_env["ledger"].rows[uid]
    obj_key = row["object_key"]
    rel_path = svc.build_sandbox_upload_rel_path(row)

    sbx = _RealPosixSandbox(str(tmp_path), dir_name="CrudWS")
    layout = sbx.workspace()
    abs_path = f"{layout.workspace}/{rel_path}"
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(payload)

    # Wire ws_crud.delete_workspace_files dependencies to our workspace and real POSIX sandbox
    ws_meta = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
        "dir_name": "CrudWS",
        "computer_root_dir": str(tmp_path),
    }

    @asynccontextmanager
    async def _fake_acquire_sandbox(_ws_id: str, _owner_id: str):
        yield sbx, ws_meta

    monkeypatch.setattr(
        ws_crud,
        "owned_workspace_on_computer",
        create_autospec(ws_crud.owned_workspace_on_computer, return_value=ws_meta),
    )
    monkeypatch.setattr(
        ws_crud,
        "acquire_sandbox_to_change",
        create_autospec(ws_crud.acquire_sandbox_to_change, side_effect=_fake_acquire_sandbox),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_work_dir",
        create_autospec(ws_crud.owner_work_dir, return_value=layout.workspace),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_layout",
        create_autospec(ws_crud.owner_layout, return_value=layout),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_meta),
    )

    del_body = ws_crud.DeleteFilesRequest(paths=[rel_path])

    # Start an independent restore process that has ALREADY passed its final DB check
    # (holding a valid grant on disk in .wsfiles-grants-<uid_hex>) and is paused before acquiring
    # the target lock to publish!
    assert await db_chat_uploads.is_upload_retained_for_recovery(uid, ws_id) is True
    grant_path, lock_path = await svc._create_restore_grant(sbx, abs_path, uid, "crud-restore-1")
    grant_dir = os.path.dirname(grant_path)
    assert os.path.exists(grant_path)

    repo_root = str(Path(__file__).resolve().parents[3])
    src_dir = os.path.join(repo_root, "src")
    stage_path = str(tmp_path / "crud_restore_stage.bin")
    with open(stage_path, "wb") as f:
        f.write(payload)
    restore_ready = tmp_path / "crud_restore_ready.flag"
    restore_resume = tmp_path / "crud_restore_resume.flag"

    fin_cmd = svc._build_stream_finalize_script(
        stage_path,
        abs_path,
        len(payload),
        sha_hex,
        grant_path=grant_path,
        lock_path=lock_path,
    )
    restore_proc = subprocess.Popen(
        fin_cmd,
        shell=True,
        env={
            **os.environ,
            "PYTHONPATH": f"{src_dir}:{repo_root}:{os.path.join(repo_root, 'libs', 'chartkit')}:{os.path.join(repo_root, 'libs', 'market-protocol')}",
            "WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN": f"{restore_ready}:{restore_resume}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        await _wait_for_flag(restore_ready)

        # 1. Inject nonzero revocation command exit while ordinary rm remains available
        async def _fail_revoke_nonzero(_fn: Any, *args: Any) -> _FakeExecResult | None:
            cmd = str(args[0]) if args else ""
            if "items, unlink_tgt = json.loads(sys.argv[1])" in cmd:
                return _FakeExecResult(stdout="", stderr="injected revoke nonzero", exit_code=1)
            return None

        sbx.exec_interceptor = _fail_revoke_nonzero
        res_nonzero = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body
        )
        assert res_nonzero["deleted"] == []
        assert len(res_nonzero["errors"]) == 1
        assert res_nonzero["errors"][0]["path"] == rel_path
        # DB deletion restriction MUST be preserved!
        assert upload_env["ledger"].rows[uid]["error"] == "explicit_delete_pending"
        assert await db_chat_uploads.is_upload_retained_for_recovery(uid, ws_id) is False
        assert os.path.exists(grant_path)
        assert obj_key in upload_env["store"].objects

        # 2. Inject transport timeout/exception on revocation while ordinary rm remains available
        async def _fail_revoke_transport(_fn: Any, *args: Any) -> _FakeExecResult | None:
            cmd = str(args[0]) if args else ""
            if "items, unlink_tgt = json.loads(sys.argv[1])" in cmd:
                raise TimeoutError("sandbox exec transport timeout")
            return None

        sbx.exec_interceptor = _fail_revoke_transport
        res_transport = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body
        )
        assert res_transport["deleted"] == []
        assert len(res_transport["errors"]) == 1
        assert res_transport["errors"][0]["path"] == rel_path
        # Uncertain remote timeout MUST NOT clear explicit_delete_pending!
        assert upload_env["ledger"].rows[uid]["error"] == "explicit_delete_pending"
        assert await db_chat_uploads.is_upload_retained_for_recovery(uid, ws_id) is False
        assert os.path.exists(grant_path)
        assert obj_key in upload_env["store"].objects
        sbx.exec_interceptor = None

        # 3. Real POSIX unlink PermissionError on grant_dir (chmod 0500) while ordinary rm in
        #    work/uploads (0755) remains completely available!
        os.chmod(grant_dir, 0o500)
        try:
            # Prove ordinary rm in work/uploads is available right now
            probe_rm_file = f"{os.path.dirname(abs_path)}/ordinary_rm_ok.tmp"
            with open(probe_rm_file, "wb") as f:
                f.write(b"ok")
            rm_probe = await sbx.execute_bash_command(f"rm -f {shlex.quote(probe_rm_file)}")
            assert rm_probe["success"] is True and not os.path.exists(probe_rm_file)

            res_perm = await ws_crud.delete_workspace_files(
                workspace_id=ws_id, x_user_id=owner, body=del_body
            )
            assert res_perm["deleted"] == []
            assert len(res_perm["errors"]) == 1
            assert res_perm["errors"][0]["path"] == rel_path
            assert "Permission denied" in res_perm["errors"][0]["detail"] or "PermissionError" in res_perm["errors"][0]["detail"]
            assert upload_env["ledger"].rows[uid]["error"] == "explicit_delete_pending"
            assert os.path.exists(grant_path)
            assert os.path.exists(abs_path)
            assert obj_key in upload_env["store"].objects
        finally:
            os.chmod(grant_dir, 0o700)

        # 4. Retire-stage failure: begin_delete revokes grant_path and rm removes abs_path,
        #    but retire_deleted_workspace_chat_uploads fails on its unlink_target=True pass!
        async def _fail_retire_stage(_fn: Any, *args: Any) -> _FakeExecResult | None:
            cmd = str(args[0]) if args else ""
            if "items, unlink_tgt = json.loads(sys.argv[1])" in cmd and cmd.rstrip().endswith(" 1"):
                return _FakeExecResult(stdout="", stderr="injected retire revocation failure", exit_code=1)
            return None

        sbx.exec_interceptor = _fail_retire_stage
        res_retire_fail = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body
        )
        assert res_retire_fail["deleted"] == []
        assert len(res_retire_fail["errors"]) == 1
        assert res_retire_fail["errors"][0]["path"] == rel_path
        # Grant was revoked during begin_delete, DB row stays ready + explicit_delete_pending
        # (not prematurely retired or deleted from S3!)
        assert not os.path.exists(grant_path)
        assert upload_env["ledger"].rows[uid]["state"] == "ready"
        assert upload_env["ledger"].rows[uid]["error"] == "explicit_delete_pending"
        assert obj_key in upload_env["store"].objects

        # Release the paused restore process: because its grant was revoked, it exits 5 (superseded)
        # and cannot recreate abs_path!
        restore_resume.write_text("go", encoding="utf-8")
        rc_restore = await asyncio.to_thread(restore_proc.wait, 5.0)
        assert rc_restore == 5
        assert not os.path.exists(abs_path)

        # 5. Subsequent retry of delete_workspace_files succeeds and retires DB row + S3 object!
        sbx.exec_interceptor = None
        res_retry_ok = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body
        )
        assert res_retry_ok == {"deleted": [rel_path], "errors": []}
        assert not os.path.exists(abs_path)
        assert upload_env["ledger"].rows[uid]["state"] == "aborted"
        assert upload_env["ledger"].rows[uid]["cleaned_at"] is not None
        assert obj_key not in upload_env["store"].objects
    finally:
        if os.path.exists(grant_dir):
            os.chmod(grant_dir, 0o700)
        if restore_proc.poll() is None:
            restore_proc.kill()

    # 6. Independent backend subprocess running rematerialize_workspace_chat_uploads with explicit
    #    sandbox/layout against a shared file-backed DB state while parent backend process runs
    #    ws_crud.delete_workspace_files (covering failed revocation -> restore recreates file while
    #    API returned error, then successful retry -> file absent after all restore processes exit).
    import sys

    created_2 = await svc.initiate_chat_upload(
        user_id=owner,
        workspace_id=ws_id,
        filename="multiprocess_backend.bin",
        size_bytes=len(payload),
    )
    uid_2 = created_2["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_2, part_number=1, user_id=owner, stream=_bytes_stream(payload)
    )
    await svc.complete_chat_upload(upload_id=uid_2, user_id=owner)
    await svc.confirm_request_chat_uploads(
        [ChatUploadContext(upload_id=uid_2, name="multiprocess_backend.bin", size=len(payload))],
        user_id=owner,
        workspace_id=ws_id,
    )
    row_2 = upload_env["ledger"].rows[uid_2]
    obj_key_2 = row_2["object_key"]
    rel_2 = svc.build_sandbox_upload_rel_path(row_2)
    abs_2 = f"{layout.workspace}/{rel_2}"
    del_body_2 = ws_crud.DeleteFilesRequest(paths=[rel_2])

    shared_db_file = tmp_path / "shared_db_state.json"

    def _sync_shared_db_to_disk() -> None:
        r = upload_env["ledger"].rows[uid_2]
        shared_db_file.write_text(
            json.dumps(
                {
                    "upload_id": uid_2,
                    "user_id": r["user_id"],
                    "workspace_id": r["workspace_id"],
                    "filename": r["filename"],
                    "mime_type": r["mime_type"],
                    "size_bytes": r["size_bytes"],
                    "actual_sha256": r["actual_sha256"],
                    "object_key": r["object_key"],
                    "state": r["state"],
                    "error": r.get("error"),
                    "used_at": r["used_at"].isoformat() if r.get("used_at") else None,
                    "cleaned_at": r["cleaned_at"].isoformat() if r.get("cleaned_at") else None,
                }
            ),
            encoding="utf-8",
        )

    _sync_shared_db_to_disk()
    orig_mark_pending = db_chat_uploads.mark_uploads_delete_pending
    orig_clear_pending = db_chat_uploads.clear_uploads_delete_pending
    orig_retire_by_ids = db_chat_uploads.retire_uploads_by_ids

    async def _synced_mark_pending(upload_ids: Sequence[str], *, workspace_id: str) -> list[str]:
        out = await orig_mark_pending(upload_ids, workspace_id=workspace_id)
        _sync_shared_db_to_disk()
        return out

    async def _synced_clear_pending(upload_ids: Sequence[str], *, workspace_id: str) -> int:
        out = await orig_clear_pending(upload_ids, workspace_id=workspace_id)
        _sync_shared_db_to_disk()
        return out

    async def _synced_retire_by_ids(
        upload_ids: Sequence[str], *, workspace_id: str
    ) -> list[dict[str, Any]]:
        out = await orig_retire_by_ids(upload_ids, workspace_id=workspace_id)
        _sync_shared_db_to_disk()
        return out

    monkeypatch.setattr(db_chat_uploads, "mark_uploads_delete_pending", _synced_mark_pending)
    monkeypatch.setattr(db_chat_uploads, "clear_uploads_delete_pending", _synced_clear_pending)
    monkeypatch.setattr(db_chat_uploads, "retire_uploads_by_ids", _synced_retire_by_ids)

    backend_worker_script = (
        "import asyncio, json, os, shlex, sys\n"
        "from ptc_agent.core.paths import WorkspaceLayout\n"
        "from ptc_agent.core.project_context import ProjectContext\n"
        "from src.dalton import chat_upload_storage\n"
        "from src.dalton.database import chat_uploads as db\n"
        "from src.dalton.services import chat_uploads as svc\n"
        "root_dir, ws_id, owner, db_file, ready_f, resume_f, out_f, payload_hex = sys.argv[1:9]\n"
        "payload = bytes.fromhex(payload_hex)\n"
        "def _read_row(): return json.loads(open(db_file, 'r', encoding='utf-8').read())\n"
        "class _SubprocSandbox:\n"
        "    provider = type('P', (), {'direct_blob_transfer': False})()\n"
        "    def __init__(self):\n"
        "        self.config = type('C', (), {'filesystem': type('F', (), {'working_directory': root_dir})()})()\n"
        "        s = self\n"
        "        class _R:\n"
        "            @staticmethod\n"
        "            async def exec(cmd, **kw): return await s._runtime_call('exec', cmd, **kw)\n"
        "            @staticmethod\n"
        "            async def upload_file(data, dest, **kw): return await s._runtime_call('upload_file', data, dest, **kw)\n"
        "        self.runtime = _R()\n"
        "    def workspace(self, project=None):\n"
        "        return WorkspaceLayout(root=root_dir, dir_name='CrudWS')\n"
        "    def validate_path(self, fp, project=None): return True\n"
        "    def path_write_lock(self, p):\n"
        "        from contextlib import asynccontextmanager\n"
        "        @asynccontextmanager\n"
        "        async def _lk(): yield\n"
        "        return _lk()\n"
        "    async def _runtime_call(self, fn, *args, **kw):\n"
        "        if fn == self.runtime.upload_file or (len(args) == 2 and isinstance(args[0], (bytes, bytearray))):\n"
        "            os.makedirs(os.path.dirname(args[1]), exist_ok=True)\n"
        "            open(args[1], 'wb').write(args[0])\n"
        "            return type('Res', (), {'stdout': '', 'stderr': '', 'exit_code': 0})()\n"
        "        cmd = str(args[0])\n"
        "        if cmd.startswith('df -Pk'):\n"
        "            return type('Res', (), {'stdout': f'Filesystem 1024-blocks Used Available Capacity Mounted on\\noverlay 52428800 1048576 20971520 5% {root_dir}\\n', 'stderr': '', 'exit_code': 0})()\n"
        "        p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)\n"
        "        o, e = await p.communicate()\n"
        "        return type('Res', (), {'stdout': o.decode(), 'stderr': e.decode(), 'exit_code': p.returncode or 0})()\n"
        "svc.get_dalton_settings = lambda: type('S', (), {'enabled': True})()\n"
        "chat_upload_storage.is_chat_upload_storage_configured = lambda: True\n"
        "chat_upload_storage.iter_stored_object_chunks = lambda k, chunk_size=4194304: iter([payload])\n"
        "async def _list_ret(w, *, conn=None):\n"
        "    r = _read_row()\n"
        "    return [r] if r['state'] == 'ready' and not r.get('error') else []\n"
        "async def _is_ret(u, w, *, conn=None):\n"
        "    r = _read_row()\n"
        "    return r['upload_id'] == u and r['state'] == 'ready' and not r.get('error')\n"
        "async def _ws_perm(w, *, conn=None):\n"
        "    return {'workspace_id': ws_id, 'user_id': owner, 'status': 'running'}\n"
        "db.list_retained_workspace_uploads = _list_ret\n"
        "db.is_upload_retained_for_recovery = _is_ret\n"
        "db.get_workspace_permission_row = _ws_perm\n"
        "async def _hook(u, abs_t):\n"
        "    open(ready_f, 'w').write(u)\n"
        "    while not os.path.exists(resume_f): await asyncio.sleep(0.01)\n"
        "svc._AFTER_FINAL_RECOVERY_DB_CHECK_HOOK = _hook\n"
        "async def _main():\n"
        "    sbx = _SubprocSandbox()\n"
        "    res = await svc.rematerialize_workspace_chat_uploads(ws_id, sbx, layout=sbx.workspace())\n"
        "    open(out_f, 'w').write(json.dumps(res))\n"
        "asyncio.run(_main())\n"
    )

    b1_ready = tmp_path / "b1_ready.flag"
    b1_resume = tmp_path / "b1_resume.flag"
    b1_out = tmp_path / "b1_out.json"
    b2_ready = tmp_path / "b2_ready.flag"
    b2_resume = tmp_path / "b2_resume.flag"
    b2_out = tmp_path / "b2_out.json"

    proc_b1 = subprocess.Popen(
        [
            sys.executable,
            "-c",
            backend_worker_script,
            sbx.root_dir,
            ws_id,
            owner,
            str(shared_db_file),
            str(b1_ready),
            str(b1_resume),
            str(b1_out),
            payload.hex(),
        ],
        env={**os.environ, "PYTHONPATH": f"{src_dir}:{repo_root}:{os.path.join(repo_root, 'libs', 'chartkit')}:{os.path.join(repo_root, 'libs', 'market-protocol')}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc_b2 = subprocess.Popen(
        [
            sys.executable,
            "-c",
            backend_worker_script,
            sbx.root_dir,
            ws_id,
            owner,
            str(shared_db_file),
            str(b2_ready),
            str(b2_resume),
            str(b2_out),
            payload.hex(),
        ],
        env={**os.environ, "PYTHONPATH": f"{src_dir}:{repo_root}:{os.path.join(repo_root, 'libs', 'chartkit')}:{os.path.join(repo_root, 'libs', 'market-protocol')}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        await _wait_for_flag(b1_ready)
        await _wait_for_flag(b2_ready)

        # Both independent backend processes have created their grants and passed their final DB checks!
        # Inject nonzero revocation failure in parent CRUD delete:
        sbx.exec_interceptor = _fail_revoke_nonzero
        res_b_fail = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body_2
        )
        assert res_b_fail["deleted"] == []
        assert len(res_b_fail["errors"]) == 1
        assert upload_env["ledger"].rows[uid_2]["error"] == "explicit_delete_pending"

        # Allow Backend Process 1 to resume while revocation had failed:
        # Process 1 recreates abs_2, proving why res_b_fail MUST NOT have reported deleted=[rel_2]!
        sbx.exec_interceptor = None
        b1_resume.write_text("go", encoding="utf-8")
        rc_b1 = await asyncio.to_thread(proc_b1.wait, 5.0)
        assert rc_b1 == 0
        assert json.loads(b1_out.read_text(encoding="utf-8")) == {"restored": 1, "errors": 0}
        assert os.path.exists(abs_2)

        # Now retry ws_crud.delete_workspace_files while Backend Process 2 is still paused after its
        # final DB check: the retry revokes Backend Process 2's grant, removes abs_2, and retires uid_2!
        res_b_ok = await ws_crud.delete_workspace_files(
            workspace_id=ws_id, x_user_id=owner, body=del_body_2
        )
        assert res_b_ok == {"deleted": [rel_2], "errors": []}

        # Resume Backend Process 2: its grant was revoked, so it exits with restored=0 and abs_2 stays absent!
        b2_resume.write_text("go", encoding="utf-8")
        rc_b2 = await asyncio.to_thread(proc_b2.wait, 5.0)
        assert rc_b2 == 0
        assert json.loads(b2_out.read_text(encoding="utf-8")) == {"restored": 0, "errors": 0}
        assert not os.path.exists(abs_2)
        assert upload_env["ledger"].rows[uid_2]["state"] == "aborted"
        assert obj_key_2 not in upload_env["store"].objects
    finally:
        for p in (proc_b1, proc_b2):
            if p.poll() is None:
                p.kill()


@pytest.mark.asyncio
async def test_materialize_chat_uploads_direct_and_chunked_revoked_after_bytes_read_raises_410_without_recreating_file(
    upload_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify materialize_chat_uploads (via attach_request_files and materialize_steered_chat_uploads)
    for both direct (pull_direct) and chunked (_materialize_via_chunked_stream) transfers:
    when a transfer has already verified DB retention and read all bytes from storage into staging,
    pauses before target publication, and a concurrent file deletion completes before resume,
    the transfer aborts with HTTP 410 / SteerAttachmentPermanentError('http_410'), never recreates
    the target file on disk, cleans up staging/grant files, and emits no saved-file reminder."""
    import json
    import sys
    from contextlib import suppress
    from pathlib import Path

    from src.server.app.workspace_files import crud as ws_crud
    from src.server.database import workspace as ws_db

    owner = "user-mat-revoke"
    ws_id = str(uuid.uuid4())
    upload_env["workspaces"][ws_id] = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
    }

    root_dir = str(tmp_path / "posix_mat_revoke_root")
    sbx_place = _RealPosixSandbox(root_dir, dir_name="MatRevokeWS")
    sbx_delete = _RealPosixSandbox(root_dir, dir_name="MatRevokeWS")
    project = ProjectContext(workspace_id=ws_id, dir_name="MatRevokeWS")
    layout = sbx_place.workspace(project)
    ws_meta = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
        "dir_name": "MatRevokeWS",
    }

    @asynccontextmanager
    async def _fake_acquire_sandbox(_ws_id: str, _owner_id: str):
        yield sbx_delete, ws_meta

    monkeypatch.setattr(
        ws_crud,
        "owned_workspace_on_computer",
        create_autospec(ws_crud.owned_workspace_on_computer, return_value=ws_meta),
    )
    monkeypatch.setattr(
        ws_crud,
        "acquire_sandbox_to_change",
        create_autospec(ws_crud.acquire_sandbox_to_change, side_effect=_fake_acquire_sandbox),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_work_dir",
        create_autospec(ws_crud.owner_work_dir, return_value=layout.workspace),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_layout",
        create_autospec(ws_crud.owner_layout, return_value=layout),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_meta),
    )

    repo_root = str(Path(__file__).resolve().parents[3])
    src_dir = os.path.join(repo_root, "src")

    direct_worker_code = (
        "import json, os, sys\n"
        "from ptc_agent.core.sandbox import wsfiles_transfer_runtime as rt\n"
        "stage_p, target_p, item_json, out_p = sys.argv[1:5]\n"
        "item = json.loads(item_json)\n"
        "res = rt._place(stage_p, target_p, item, 200)\n"
        "with open(out_p, 'w', encoding='utf-8') as f:\n"
        "    json.dump(res, f)\n"
    )

    for mode in ("direct", "chunked"):
        for entrypoint in ("attach_request_files", "materialize_steered_chat_uploads"):
            payload = f"PAYLOAD_{mode}_{entrypoint}_".encode("utf-8") * 512
            filename = f"model_{mode}_{entrypoint}.bin"
            created = await svc.initiate_chat_upload(
                user_id=owner,
                workspace_id=ws_id,
                filename=filename,
                size_bytes=len(payload),
                mime_type="application/octet-stream",
            )
            uid = created["upload_id"]
            await svc.receive_chat_upload_part(
                upload_id=uid,
                part_number=1,
                user_id=owner,
                stream=_bytes_stream(payload),
            )
            await svc.complete_chat_upload(upload_id=uid, user_id=owner)
            ctx_item = ChatUploadContext(
                upload_id=uid,
                name=filename,
                mime_type="application/octet-stream",
                size=len(payload),
            )
            claim_key = f"req:{mode}:{entrypoint}:{uid}"
            await svc.validate_and_claim_request_attachments(
                [ctx_item],
                user_id=owner,
                workspace_id=ws_id,
                claim_key=claim_key,
            )
            await svc.confirm_request_chat_uploads(
                [ctx_item],
                user_id=owner,
                workspace_id=ws_id,
                claim_key=claim_key,
            )

            row = upload_env["ledger"].rows[uid]
            obj_key = str(row["object_key"])
            rel_path = svc.build_sandbox_upload_rel_path(row)
            abs_path = f"{layout.workspace}/{rel_path}"

            ready_flag = tmp_path / f"mat_{mode}_{entrypoint}_ready.flag"
            resume_flag = tmp_path / f"mat_{mode}_{entrypoint}_resume.flag"
            bytes_read_count = 0

            if mode == "direct":
                sbx_place.provider.direct_blob_transfer = True

                async def _real_posix_pull_direct(
                    sandbox: Any,
                    entries: list[dict[str, Any]],
                    *,
                    layout: WorkspaceLayout,
                    defer_dir_modes: bool = False,
                    sweep_orphans: bool = True,
                    _obj_key: str = obj_key,
                    _uid: str = uid,
                    _ready_flag: Path = ready_flag,
                    _resume_flag: Path = resume_flag,
                ) -> dict[str, dict[str, Any]]:
                    nonlocal bytes_read_count
                    del sandbox, defer_dir_modes, sweep_orphans
                    item = dict(entries[0])
                    assert item.get("grant_file"), "materialize_chat_uploads must supply grant_file"
                    assert item.get("lock_file"), "materialize_chat_uploads must supply lock_file"
                    target_abs = f"{layout.workspace}/{item['path']}"
                    parent_dir = os.path.dirname(target_abs)
                    os.makedirs(parent_dir, exist_ok=True)
                    # Read bytes from storage into a staging temp file before pausing
                    raw_blob = upload_env["store"].objects[_obj_key]
                    bytes_read_count += len(raw_blob)
                    stage_file = f"{parent_dir}/.wsfiles-tmp-direct-{_uid[:8]}"
                    with open(stage_file, "wb") as sf:
                        sf.write(raw_blob)
                    out_file = str(tmp_path / f"direct_out_{_uid[:8]}.json")
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        direct_worker_code,
                        stage_file,
                        target_abs,
                        json.dumps(item),
                        out_file,
                        env={
                            **os.environ,
                            "PYTHONPATH": f"{src_dir}:{repo_root}:{os.path.join(repo_root, 'libs', 'chartkit')}:{os.path.join(repo_root, 'libs', 'market-protocol')}",
                            "WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN": f"{_ready_flag}:{_resume_flag}",
                        },
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                    res_obj = json.loads(Path(out_file).read_text(encoding="utf-8"))
                    return {item["path"]: res_obj}

                monkeypatch.setattr(svc, "pull_direct", _real_posix_pull_direct)
            else:
                sbx_place.provider.direct_blob_transfer = False

                def _tracked_iter_chunks(key: str, chunk_size: int = 4194304):
                    nonlocal bytes_read_count
                    raw_blob = upload_env["store"].objects[key]
                    for idx in range(0, len(raw_blob), chunk_size):
                        piece = raw_blob[idx : idx + chunk_size]
                        bytes_read_count += len(piece)
                        yield piece

                monkeypatch.setattr(
                    svc.storage, "iter_stored_object_chunks", _tracked_iter_chunks
                )
                monkeypatch.setenv(
                    "WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN", f"{ready_flag}:{resume_flag}"
                )

            if entrypoint == "attach_request_files":
                req = ChatRequest(
                    messages=[{"role": "user", "content": "Inspect attachment"}],
                    additional_context=[ctx_item],
                )
                in_messages = [{"role": "user", "content": "Inspect attachment"}]
                mat_task = asyncio.create_task(
                    attach_request_files(
                        in_messages,
                        req,
                        _FakeSession(sbx_place),
                        effective_model="claude-sonnet-4-5",
                        config=type(
                            "Cfg",
                            (),
                            {"input_modalities": ["text", "image"], "llm_client": None},
                        )(),
                        project=project,
                        user_id=owner,
                        workspace_id=ws_id,
                    )
                )
            else:
                steer_payload = {
                    "content": "Steer with attachment",
                    "user_id": owner,
                    "workspace_id": ws_id,
                    "is_colleague": False,
                    "chat_uploads": [ctx_item.model_dump()],
                }
                mat_task = asyncio.create_task(
                    svc.materialize_steered_chat_uploads(
                        steer_payload,
                        session=_FakeSession(sbx_place),
                        project=project,
                    )
                )

            try:
                # Wait until all bytes have been read from storage into staging and the
                # transfer is paused at WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN before publication.
                await _wait_for_flag(ready_flag)
                assert bytes_read_count == len(payload)
                assert not os.path.exists(abs_path)

                # Concurrent explicit file deletion runs and completes while transfer is paused.
                del_res = await ws_crud.delete_workspace_files(
                    workspace_id=ws_id,
                    x_user_id=owner,
                    body=ws_crud.DeleteFilesRequest(paths=[rel_path]),
                )
                assert del_res == {"deleted": [rel_path], "errors": []}
                assert upload_env["ledger"].rows[uid]["state"] == "aborted"
                assert upload_env["ledger"].rows[uid]["cleaned_at"] is not None
                assert obj_key not in upload_env["store"].objects

                # Resume the paused transfer: it must abort with HTTP 410 / SteerAttachmentPermanentError('http_410')
                # without recreating abs_path or generating any saved-file reminder!
                resume_flag.write_text("go", encoding="utf-8")

                if entrypoint == "attach_request_files":
                    with pytest.raises(HTTPException) as exc_info:
                        await mat_task
                    assert exc_info.value.status_code == 410
                    assert "expired or was removed" in str(exc_info.value.detail)
                    assert in_messages == [{"role": "user", "content": "Inspect attachment"}]
                else:
                    with pytest.raises(svc.SteerAttachmentPermanentError) as exc_info:
                        await mat_task
                    assert exc_info.value.code == "http_410"
                    assert "expired or was removed" in str(exc_info.value.message)

                assert not os.path.exists(abs_path)
                uploads_dir = os.path.dirname(abs_path)
                if os.path.isdir(uploads_dir):
                    leftover = [
                        n
                        for n in os.listdir(uploads_dir)
                        if n.startswith(
                            (
                                ".wsfiles-tmp-",
                                ".wsfiles-stage-",
                                ".wsfiles-part-",
                                ".wsfiles-grants-",
                            )
                        )
                    ]
                    assert leftover == []
            finally:
                monkeypatch.delenv("WSFILES_TEST_PAUSE_AFTER_LOCK_OPEN", raising=False)
                if not resume_flag.exists():
                    resume_flag.write_text("go", encoding="utf-8")
                if not mat_task.done():
                    with suppress(Exception):
                        await mat_task


@pytest.mark.asyncio
async def test_delete_pending_not_swept_by_time_and_shared_writer_deletes_with_colleague_dispatch_disabled(
    upload_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify:
    1. claim_cleanup_batch SQL and sweep_expired_chat_uploads_once never select or delete a ready
       upload solely because error='explicit_delete_pending' and >5 minutes elapsed; recovery stays
       blocked until confirmed deletion retires the upload.
    2. With Dalton Supabase auth enabled and colleague dispatch disabled, a shared writer
       (web_access='rw') can delete both ordinary workspace files and retained chat attachments via
       delete_workspace_files (and clear fences via abort_delete_workspace_chat_uploads on rm failure),
       while read-only (web_access='ro') and hidden (web_access='hidden') callers are denied and
       upload admission / conversation dispatch remain blocked for non-owners."""
    from pathlib import Path

    from src.dalton import settings as dalton_settings
    from src.dalton import sharing as dalton_sharing
    from src.server.app.workspace_files import crud as ws_crud
    from src.server.database import workspace as ws_db
    from src.server.services import turn_runtime, workspace_files

    # ------------------------------------------------------------------
    # Part 1: Real claim_cleanup_batch SQL check + sweeper time-advance check
    # ------------------------------------------------------------------
    captured_sql: list[str] = []

    class _CaptureCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            del params
            captured_sql.append(sql)

        async def fetchall(self) -> list[dict[str, Any]]:
            return []

    class _CaptureConn:
        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, **_kwargs):
            return _CaptureCursor()

    @asynccontextmanager
    async def _fake_db_conn():
        yield _CaptureConn()

    with patch.object(db_chat_uploads, "get_db_connection", _fake_db_conn):
        await _REAL_CLAIM_CLEANUP_BATCH(limit=10, now=datetime.now(timezone.utc))

    assert len(captured_sql) == 1
    sql_text = captured_sql[0]
    assert "explicit_delete_pending" not in sql_text
    assert "u.state IN ('aborted', 'failed')" in sql_text
    assert "w.status = 'deleted'" in sql_text

    owner = "user-owner-r15"
    colleague_rw = "user-colleague-rw-r15"
    colleague_ro = "user-colleague-ro-r15"
    ws_id = str(uuid.uuid4())
    ws_meta = {
        "workspace_id": ws_id,
        "user_id": owner,
        "name": "SharedWriteWS",
        "status": "running",
        "dir_name": "SharedWriteWS",
        "computer_id": "comp-r15",
    }
    upload_env["workspaces"][ws_id] = ws_meta

    root_dir = str(tmp_path / "r15_posix_root")
    sbx = _RealPosixSandbox(root_dir, dir_name="SharedWriteWS")
    project = ProjectContext(workspace_id=ws_id, dir_name="SharedWriteWS")
    layout = sbx.workspace(project)

    # Create a retained used attachment + an unused expired attachment
    payload = b"RETENTION_SAFE_DURING_DELETE_PENDING_" * 128
    created_retained = await svc.initiate_chat_upload(
        user_id=owner,
        workspace_id=ws_id,
        filename="keep_until_confirmed.bin",
        size_bytes=len(payload),
    )
    uid_retained = created_retained["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_retained,
        part_number=1,
        user_id=owner,
        stream=_bytes_stream(payload),
    )
    await svc.complete_chat_upload(upload_id=uid_retained, user_id=owner)
    ctx_retained = ChatUploadContext(
        upload_id=uid_retained,
        name="keep_until_confirmed.bin",
        size=len(payload),
    )
    await svc.confirm_request_chat_uploads(
        [ctx_retained], user_id=owner, workspace_id=ws_id
    )
    row_retained = upload_env["ledger"].rows[uid_retained]
    key_retained = str(row_retained["object_key"])
    rel_retained = svc.build_sandbox_upload_rel_path(row_retained)
    abs_retained = f"{layout.workspace}/{rel_retained}"
    os.makedirs(os.path.dirname(abs_retained), exist_ok=True)
    Path(abs_retained).write_bytes(payload)

    created_unused = await svc.initiate_chat_upload(
        user_id=owner,
        workspace_id=ws_id,
        filename="expired_unused.bin",
        size_bytes=len(payload),
    )
    uid_unused = created_unused["upload_id"]
    await svc.receive_chat_upload_part(
        upload_id=uid_unused,
        part_number=1,
        user_id=owner,
        stream=_bytes_stream(payload),
    )
    await svc.complete_chat_upload(upload_id=uid_unused, user_id=owner)
    row_unused = upload_env["ledger"].rows[uid_unused]
    key_unused = str(row_unused["object_key"])
    base_now = datetime.now(timezone.utc)
    row_unused["expires_at"] = base_now + timedelta(minutes=2)

    # Fence the retained upload as explicit_delete_pending (e.g. after uncertain rm / grant revoke failure)
    fenced_ids = await db_chat_uploads.mark_uploads_delete_pending(
        [uid_retained], workspace_id=ws_id
    )
    assert fenced_ids == [uid_retained]
    row_retained["updated_at"] = base_now - timedelta(minutes=20)
    assert await db_chat_uploads.is_upload_retained_for_recovery(uid_retained, ws_id) is False

    # Advance sweeper 30 minutes past updated_at: unused upload is cleaned, but explicit_delete_pending
    # retained upload MUST remain ready with its storage object intact while recovery stays blocked!
    swept = await svc.sweep_expired_chat_uploads_once(
        now=base_now + timedelta(minutes=30)
    )
    assert swept == 1
    assert key_unused not in upload_env["store"].objects
    assert upload_env["ledger"].rows[uid_unused]["state"] == "aborted"
    assert upload_env["ledger"].rows[uid_unused]["cleaned_at"] is not None

    assert upload_env["ledger"].rows[uid_retained]["state"] == "ready"
    assert upload_env["ledger"].rows[uid_retained]["error"] == "explicit_delete_pending"
    assert upload_env["ledger"].rows[uid_retained]["cleaned_at"] is None
    assert key_retained in upload_env["store"].objects
    assert await db_chat_uploads.is_upload_retained_for_recovery(uid_retained, ws_id) is False

    # ------------------------------------------------------------------
    # Part 2: Route-level delete_workspace_files with Dalton Supabase auth enabled
    #         and colleague dispatch disabled (is_colleague_dispatch_enabled() == False)
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def _fake_acquire_sandbox(_ws_id: str, _owner_id: str):
        yield sbx, ws_meta

    monkeypatch.setattr(
        ws_crud,
        "acquire_sandbox_to_change",
        create_autospec(ws_crud.acquire_sandbox_to_change, side_effect=_fake_acquire_sandbox),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_work_dir",
        create_autospec(ws_crud.owner_work_dir, return_value=layout.workspace),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_layout",
        create_autospec(ws_crud.owner_layout, return_value=layout),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_meta),
    )
    monkeypatch.setattr(
        workspace_files,
        "db_get_workspace",
        create_autospec(workspace_files.db_get_workspace, return_value=ws_meta),
    )

    # Enable Dalton Supabase auth while keeping colleague_dispatch disabled!
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_sharing.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=False),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_sharing.is_colleague_dispatch_enabled, return_value=False),
    )

    async def _fake_team_reader(uid: str | None) -> str:
        if uid in (owner, colleague_rw, colleague_ro):
            return str(uid)
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        dalton_sharing,
        "require_team_reader",
        create_autospec(dalton_sharing.require_team_reader, side_effect=_fake_team_reader),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(dalton_sharing.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "classify_owner_tier",
        create_autospec(dalton_sharing.classify_owner_tier, return_value="internal"),
    )

    current_web_access = "rw"

    async def _fake_get_ws_sharing(target_ws_id: str, *, conn: Any = None) -> dict[str, Any]:
        del conn
        assert target_ws_id == ws_id
        return {"workspace_id": ws_id, "web_access": current_web_access, "cos_access": "ro"}

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(dalton_sharing.get_workspace_sharing, side_effect=_fake_get_ws_sharing),
    )

    # Restore real requested_workspace on svc to verify upload admission / conversation dispatch
    # still blocks non-owners when colleague dispatch is disabled!
    monkeypatch.setattr(svc, "requested_workspace", turn_runtime.requested_workspace)
    with pytest.raises(HTTPException) as dispatch_disabled_exc:
        await svc.initiate_chat_upload(
            user_id=colleague_rw,
            workspace_id=ws_id,
            filename="blocked_upload.bin",
            size_bytes=128,
        )
    assert dispatch_disabled_exc.value.status_code == 403
    assert dispatch_disabled_exc.value.detail == "该功能尚未开放"

    # Create an ordinary workspace file alongside the retained chat attachment
    ordinary_rel = "work/notes/summary.md"
    ordinary_abs = f"{layout.workspace}/{ordinary_rel}"
    os.makedirs(os.path.dirname(ordinary_abs), exist_ok=True)
    Path(ordinary_abs).write_text("shared notes", encoding="utf-8")

    # 1. Read-only caller (web_access='ro') is denied (403) on delete_workspace_files
    current_web_access = "ro"
    with pytest.raises(HTTPException) as ro_exc:
        await ws_crud.delete_workspace_files(
            workspace_id=ws_id,
            x_user_id=colleague_ro,
            body=ws_crud.DeleteFilesRequest(paths=[ordinary_rel, rel_retained]),
        )
    assert ro_exc.value.status_code == 403
    with pytest.raises(HTTPException) as ro_svc_exc:
        await svc.begin_delete_workspace_chat_uploads(
            workspace_id=ws_id,
            caller_user_id=colleague_ro,
            candidate_client_paths=[rel_retained],
        )
    assert ro_svc_exc.value.status_code == 403
    assert os.path.exists(ordinary_abs)
    assert os.path.exists(abs_retained)
    assert key_retained in upload_env["store"].objects

    # 2. Hidden workspace access (web_access='hidden') is denied with 404
    current_web_access = "hidden"
    with pytest.raises(HTTPException) as hidden_exc:
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_id,
            caller_user_id=colleague_ro,
            deleted_client_paths=[rel_retained],
        )
    assert hidden_exc.value.status_code == 404

    # 3. Shared writer (web_access='rw') with colleague dispatch disabled:
    #    First verify abort_delete_workspace_chat_uploads clears the fence when rm fails
    current_web_access = "rw"
    cleared_count = await svc.abort_delete_workspace_chat_uploads(
        workspace_id=ws_id,
        caller_user_id=colleague_rw,
        failed_client_paths=[rel_retained],
    )
    assert cleared_count == 1
    assert upload_env["ledger"].rows[uid_retained]["error"] is None
    assert await db_chat_uploads.is_upload_retained_for_recovery(uid_retained, ws_id) is True

    #    Next, shared writer deletes ordinary file via delete_workspace_files -> succeeds!
    del_ordinary = await ws_crud.delete_workspace_files(
        workspace_id=ws_id,
        x_user_id=colleague_rw,
        body=ws_crud.DeleteFilesRequest(paths=[ordinary_rel]),
    )
    assert del_ordinary == {"deleted": [ordinary_rel], "errors": []}
    assert not os.path.exists(ordinary_abs)
    assert os.path.exists(abs_retained)
    assert key_retained in upload_env["store"].objects

    #    Finally, shared writer deletes retained chat attachment via delete_workspace_files -> succeeds,
    #    removes file from sandbox, retires DB row to aborted, and deletes S3 object!
    del_attachment = await ws_crud.delete_workspace_files(
        workspace_id=ws_id,
        x_user_id=colleague_rw,
        body=ws_crud.DeleteFilesRequest(paths=[rel_retained]),
    )
    assert del_attachment == {"deleted": [rel_retained], "errors": []}
    assert not os.path.exists(abs_retained)
    assert upload_env["ledger"].rows[uid_retained]["state"] == "aborted"
    assert upload_env["ledger"].rows[uid_retained]["cleaned_at"] is not None
    assert key_retained not in upload_env["store"].objects


@pytest.mark.asyncio
async def test_materialize_placement_errors_recheck_retained_state_and_preserve_transient_or_cancellation(
    upload_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify placement exceptions in materialize_chat_uploads and materialize_steered_chat_uploads:
    - Direct 404 and chunked NoSuchKey after concurrent deletion (retained -> False) become HTTP 410 /
      SteerAttachmentPermanentError('http_410') with no target file and no reminder.
    - Unchanged retained state (retained -> True) preserves the original exception / SteerAttachmentTransientError.
    - Failed post-error DB recheck or asyncio.CancelledError never falsely turns into 410."""
    from botocore.exceptions import ClientError
    from src.server.database import workspace as ws_db

    owner = "user-r17-recheck"
    ws_id = str(uuid.uuid4())
    ws_meta = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
        "dir_name": "RecheckWS",
    }
    upload_env["workspaces"][ws_id] = ws_meta
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(ws_db.get_workspace, return_value=ws_meta)
    )

    sbx = _RealPosixSandbox(str(tmp_path / "r17_root"), dir_name="RecheckWS")
    project = ProjectContext(workspace_id=ws_id, dir_name="RecheckWS")
    layout = sbx.workspace(project)

    async def _make_retained_upload(tag: str) -> tuple[str, ChatUploadContext, str, str]:
        payload = f"R17_PAYLOAD_{tag}_".encode("utf-8") * 128
        created = await svc.initiate_chat_upload(
            user_id=owner,
            workspace_id=ws_id,
            filename=f"doc_{tag}.bin",
            size_bytes=len(payload),
        )
        uid = created["upload_id"]
        await svc.receive_chat_upload_part(
            upload_id=uid, part_number=1, user_id=owner, stream=_bytes_stream(payload)
        )
        await svc.complete_chat_upload(upload_id=uid, user_id=owner)
        ctx = ChatUploadContext(
            upload_id=uid,
            name=f"doc_{tag}.bin",
            mime_type="application/octet-stream",
            size=len(payload),
        )
        await svc.confirm_request_chat_uploads([ctx], user_id=owner, workspace_id=ws_id)
        row = upload_env["ledger"].rows[uid]
        rel = svc.build_sandbox_upload_rel_path(row)
        return uid, ctx, rel, f"{layout.workspace}/{rel}"

    # 1. Direct 404 after concurrent deletion (before storage bytes arrive) -> permanent 410 / http_410
    uid_d, ctx_d, rel_d, abs_d = await _make_retained_upload("direct_del")
    sbx.provider.direct_blob_transfer = True

    async def _direct_404_after_delete(
        sandbox: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del sandbox, layout, defer_dir_modes, sweep_orphans
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_id, caller_user_id=owner, deleted_client_paths=[rel_d]
        )
        return {entries[0]["path"]: {"status": "failed", "error": "404"}}

    monkeypatch.setattr(
        svc, "pull_direct", create_autospec(_REAL_PULL_DIRECT, side_effect=_direct_404_after_delete)
    )
    in_msgs = [{"role": "user", "content": "Check file"}]
    with pytest.raises(HTTPException) as exc_d:
        await attach_request_files(
            in_msgs,
            ChatRequest(messages=in_msgs, additional_context=[ctx_d]),
            _FakeSession(sbx),
            effective_model="claude-sonnet-4-5",
            config=type("Cfg", (), {"input_modalities": ["text"], "llm_client": None})(),
            project=project,
            user_id=owner,
            workspace_id=ws_id,
        )
    assert exc_d.value.status_code == 410
    assert in_msgs == [{"role": "user", "content": "Check file"}]
    assert not os.path.exists(abs_d)

    uid_ds, ctx_ds, rel_ds, abs_ds = await _make_retained_upload("direct_steer_del")

    async def _direct_404_steer(
        sandbox: Any,
        entries: list[dict[str, Any]],
        *,
        layout: WorkspaceLayout,
        defer_dir_modes: bool = False,
        sweep_orphans: bool = True,
    ) -> dict[str, dict[str, Any]]:
        del sandbox, layout, defer_dir_modes, sweep_orphans
        await svc.retire_deleted_workspace_chat_uploads(
            workspace_id=ws_id, caller_user_id=owner, deleted_client_paths=[rel_ds]
        )
        return {entries[0]["path"]: {"status": "failed", "error": "404"}}

    monkeypatch.setattr(
        svc, "pull_direct", create_autospec(_REAL_PULL_DIRECT, side_effect=_direct_404_steer)
    )
    with pytest.raises(svc.SteerAttachmentPermanentError) as exc_ds:
        await svc.materialize_steered_chat_uploads(
            {
                "content": "Steer",
                "user_id": owner,
                "workspace_id": ws_id,
                "chat_uploads": [ctx_ds.model_dump()],
            },
            session=_FakeSession(sbx),
            project=project,
        )
    assert exc_ds.value.code == "http_410"
    assert not os.path.exists(abs_ds)

    # 2. Chunked NoSuchKey after concurrent deletion (before first byte read) -> permanent 410 / http_410
    sbx.provider.direct_blob_transfer = False
    real_iter_chunks = chat_upload_storage.iter_stored_object_chunks
    uid_c, ctx_c, rel_c, abs_c = await _make_retained_upload("chunk_del")
    no_such_key = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        "GetObject",
    )

    def _chunked_nosuchkey_after_delete(object_key: str, *, chunk_size: int = 4194304):
        del object_key, chunk_size
        upload_env["ledger"].rows[uid_c]["error"] = "explicit_delete_pending"
        raise no_such_key
        yield b""

    monkeypatch.setattr(
        chat_upload_storage,
        "iter_stored_object_chunks",
        create_autospec(
            real_iter_chunks,
            side_effect=_chunked_nosuchkey_after_delete,
        ),
    )
    with pytest.raises(svc.SteerAttachmentPermanentError) as exc_cs:
        await svc.materialize_steered_chat_uploads(
            {
                "content": "Steer chunked",
                "user_id": owner,
                "workspace_id": ws_id,
                "chat_uploads": [ctx_c.model_dump()],
            },
            session=_FakeSession(sbx),
            project=project,
        )
    assert exc_cs.value.code == "http_410"
    assert not os.path.exists(abs_c)

    # 3. Still-retained attachment keeps original exception (and SteerAttachmentTransientError on steer)
    uid_t, ctx_t, _, abs_t = await _make_retained_upload("still_retained")

    def _chunked_transient_error(object_key: str, *, chunk_size: int = 4194304):
        del object_key, chunk_size
        raise no_such_key
        yield b""

    monkeypatch.setattr(
        chat_upload_storage,
        "iter_stored_object_chunks",
        create_autospec(
            real_iter_chunks,
            side_effect=_chunked_transient_error,
        ),
    )
    with pytest.raises(ClientError) as exc_orig:
        await svc.materialize_chat_uploads(
            [ctx_t], sandbox=sbx, user_id=owner, workspace_id=ws_id, project=project
        )
    assert exc_orig.value is no_such_key
    with pytest.raises(svc.SteerAttachmentTransientError):
        await svc.materialize_steered_chat_uploads(
            {
                "content": "Steer transient",
                "user_id": owner,
                "workspace_id": ws_id,
                "chat_uploads": [ctx_t.model_dump()],
            },
            session=_FakeSession(sbx),
            project=project,
        )
    assert not os.path.exists(abs_t)

    # 4. Failed DB recheck after placement failure never falsely asserts permanent 410 deletion
    orig_is_retained = db_chat_uploads.is_upload_retained_for_recovery
    recheck_calls = 0

    async def _fail_on_post_error_recheck(
        upload_id: str, workspace_id: str, *, conn: Any = None
    ) -> bool:
        nonlocal recheck_calls
        recheck_calls += 1
        if recheck_calls >= 4:
            raise RuntimeError("transient db error during recheck")
        return await orig_is_retained(upload_id, workspace_id, conn=conn)

    monkeypatch.setattr(
        db_chat_uploads,
        "is_upload_retained_for_recovery",
        create_autospec(_REAL_IS_UPLOAD_RETAINED_FOR_RECOVERY, side_effect=_fail_on_post_error_recheck),
    )
    with pytest.raises(svc.SteerAttachmentTransientError):
        await svc.materialize_steered_chat_uploads(
            {
                "content": "Steer db recheck error",
                "user_id": owner,
                "workspace_id": ws_id,
                "chat_uploads": [ctx_t.model_dump()],
            },
            session=_FakeSession(sbx),
            project=project,
        )
    monkeypatch.setattr(db_chat_uploads, "is_upload_retained_for_recovery", orig_is_retained)

    # 5. Cancellation (asyncio.CancelledError) propagates directly and never turns into 410
    async def _cancel_during_place(*_args: Any, **_kwargs: Any) -> bool:
        raise asyncio.CancelledError("cancelled placement")

    monkeypatch.setattr(
        svc,
        "_place_row_in_sandbox",
        create_autospec(svc._place_row_in_sandbox, side_effect=_cancel_during_place),
    )
    with pytest.raises(asyncio.CancelledError):
        await svc.materialize_steered_chat_uploads(
            {
                "content": "Steer cancelled",
                "user_id": owner,
                "workspace_id": ws_id,
                "chat_uploads": [ctx_t.model_dump()],
            },
            session=_FakeSession(sbx),
            project=project,
        )


@pytest.mark.asyncio
async def test_materialize_multi_target_sibling_deleted_during_later_transfer_raises_410_without_reminders(
    upload_env, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify R5-1: in a multi-attachment batch (A sorting before B), if earlier target A
    (either already present on disk matching probe or newly placed by this invocation) is
    deleted by another worker while later target B is transferring, materialize_chat_uploads
    rechecks retained state for all unique targets inside AsyncExitStack before building
    reminders, raising HTTP 410 / SteerAttachmentPermanentError('http_410') with no reminders."""
    from contextlib import suppress
    from pathlib import Path

    from src.server.app.workspace_files import crud as ws_crud
    from src.server.database import workspace as ws_db

    owner = "user-r18-sibling"
    ws_id = str(uuid.uuid4())
    ws_meta = {
        "workspace_id": ws_id,
        "user_id": owner,
        "status": "running",
        "dir_name": "SiblingWS",
    }
    upload_env["workspaces"][ws_id] = ws_meta

    root_dir = str(tmp_path / "r18_sibling_root")
    sbx_place = _RealPosixSandbox(root_dir, dir_name="SiblingWS")
    sbx_delete = _RealPosixSandbox(root_dir, dir_name="SiblingWS")
    sbx_place.provider.direct_blob_transfer = False
    project = ProjectContext(workspace_id=ws_id, dir_name="SiblingWS")
    layout = sbx_place.workspace(project)

    @asynccontextmanager
    async def _fake_acquire_sandbox(_ws_id: str, _owner_id: str):
        yield sbx_delete, ws_meta

    monkeypatch.setattr(
        ws_crud,
        "owned_workspace_on_computer",
        create_autospec(ws_crud.owned_workspace_on_computer, return_value=ws_meta),
    )
    monkeypatch.setattr(
        ws_crud,
        "acquire_sandbox_to_change",
        create_autospec(ws_crud.acquire_sandbox_to_change, side_effect=_fake_acquire_sandbox),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_work_dir",
        create_autospec(ws_crud.owner_work_dir, return_value=layout.workspace),
    )
    monkeypatch.setattr(
        ws_crud,
        "owner_layout",
        create_autospec(ws_crud.owner_layout, return_value=layout),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_meta),
    )

    real_iter_chunks = chat_upload_storage.iter_stored_object_chunks

    def _iter_store_chunks(object_key: str, *, chunk_size: int = 4194304):
        raw_blob = upload_env["store"].objects[object_key]
        for idx in range(0, len(raw_blob), chunk_size):
            yield raw_blob[idx : idx + chunk_size]

    monkeypatch.setattr(
        chat_upload_storage,
        "iter_stored_object_chunks",
        create_autospec(real_iter_chunks, side_effect=_iter_store_chunks),
    )

    async def _make_retained_upload(
        stem: str, tag: str, *, preexisting_on_disk: bool
    ) -> tuple[str, ChatUploadContext, str, str]:
        filename = f"{stem}_{tag}.bin"
        payload = f"R18_{stem.upper()}_{tag}_".encode() * 128
        created = await svc.initiate_chat_upload(
            user_id=owner,
            workspace_id=ws_id,
            filename=filename,
            size_bytes=len(payload),
        )
        uid = created["upload_id"]
        await svc.receive_chat_upload_part(
            upload_id=uid, part_number=1, user_id=owner, stream=_bytes_stream(payload)
        )
        await svc.complete_chat_upload(upload_id=uid, user_id=owner)
        ctx = ChatUploadContext(
            upload_id=uid,
            name=filename,
            mime_type="application/octet-stream",
            size=len(payload),
        )
        await svc.confirm_request_chat_uploads([ctx], user_id=owner, workspace_id=ws_id)
        row = upload_env["ledger"].rows[uid]
        rel = svc.build_sandbox_upload_rel_path(row)
        abs_p = f"{layout.workspace}/{rel}"
        if preexisting_on_disk:
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_bytes(payload)
        else:
            assert not os.path.exists(abs_p)
        return uid, ctx, rel, abs_p

    for a_preexisting in (True, False):
        for entrypoint in ("materialize_chat_uploads", "materialize_steered_chat_uploads"):
            tag = f"{'pre' if a_preexisting else 'placed'}_{entrypoint}"
            uid_a, ctx_a, rel_a, abs_a = await _make_retained_upload(
                "alpha_a", tag, preexisting_on_disk=a_preexisting
            )
            uid_b, ctx_b, _rel_b, abs_b = await _make_retained_upload(
                "zeta_b", tag, preexisting_on_disk=False
            )
            assert abs_a < abs_b

            b_paused_evt = asyncio.Event()
            b_resume_evt = asyncio.Event()

            async def _pause_b_after_final_check(
                uid: str,
                _abs_target: str,
                _target_b: str = uid_b,
                _paused: asyncio.Event = b_paused_evt,
                _resume: asyncio.Event = b_resume_evt,
            ) -> None:
                if uid == _target_b:
                    _paused.set()
                    await _resume.wait()

            monkeypatch.setattr(svc, "_AFTER_FINAL_RECOVERY_DB_CHECK_HOOK", _pause_b_after_final_check)
            mat_task: asyncio.Task[Any] | None = None
            try:
                if entrypoint == "materialize_chat_uploads":
                    mat_task = asyncio.create_task(
                        svc.materialize_chat_uploads(
                            [ctx_a, ctx_b],
                            sandbox=sbx_place,
                            user_id=owner,
                            workspace_id=ws_id,
                            project=project,
                        )
                    )
                else:
                    mat_task = asyncio.create_task(
                        svc.materialize_steered_chat_uploads(
                            {
                                "content": "Steer multi",
                                "user_id": owner,
                                "workspace_id": ws_id,
                                "chat_uploads": [ctx_a.model_dump(), ctx_b.model_dump()],
                            },
                            session=_FakeSession(sbx_place),
                            project=project,
                        )
                    )

                await asyncio.wait_for(b_paused_evt.wait(), timeout=5.0)
                # By the time B reaches its final DB check, A has either matched the probe
                # or finished its own placement and cleaned up its grant.
                assert os.path.exists(abs_a)

                # Another worker deletes A via ws_crud.delete_workspace_files while B is paused.
                del_res = await ws_crud.delete_workspace_files(
                    workspace_id=ws_id,
                    x_user_id=owner,
                    body=ws_crud.DeleteFilesRequest(paths=[rel_a]),
                )
                assert del_res == {"deleted": [rel_a], "errors": []}
                assert upload_env["ledger"].rows[uid_a]["state"] == "aborted"
                assert not os.path.exists(abs_a)

                b_resume_evt.set()
                if entrypoint == "materialize_chat_uploads":
                    with pytest.raises(HTTPException) as exc_info:
                        await mat_task
                    assert exc_info.value.status_code == 410
                    assert "expired or was removed" in str(exc_info.value.detail)
                else:
                    with pytest.raises(svc.SteerAttachmentPermanentError) as exc_steer:
                        await mat_task
                    assert exc_steer.value.code == "http_410"
                assert not os.path.exists(abs_a)
            finally:
                b_resume_evt.set()
                if mat_task is not None and not mat_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(mat_task), timeout=5.0)
                    except BaseException:
                        if not mat_task.done():
                            mat_task.cancel()
                            with suppress(BaseException):
                                await mat_task
                monkeypatch.setattr(svc, "_AFTER_FINAL_RECOVERY_DB_CHECK_HOOK", None)

    # Success assertion: two retained targets succeed and return reminders in descriptor order
    _uid_a_ok, ctx_a_ok, rel_a_ok, abs_a_ok = await _make_retained_upload(
        "alpha_a", "success", preexisting_on_disk=False
    )
    _uid_b_ok, ctx_b_ok, rel_b_ok, abs_b_ok = await _make_retained_upload(
        "zeta_b", "success", preexisting_on_disk=False
    )
    assert abs_a_ok < abs_b_ok
    notes = await svc.materialize_chat_uploads(
        [ctx_b_ok, ctx_a_ok],
        sandbox=sbx_place,
        user_id=owner,
        workspace_id=ws_id,
        project=project,
    )
    assert os.path.exists(abs_a_ok)
    assert os.path.exists(abs_b_ok)
    assert len(notes) == 2
    assert rel_b_ok in notes[0]
    assert rel_a_ok in notes[1]
