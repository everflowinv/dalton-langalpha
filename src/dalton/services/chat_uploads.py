"""Service layer for chat composer large-file uploads and sandbox materialization.

Enforces byte limits (1 byte .. 200 MiB per file, 600 MiB per message, 30/60 MiB
inline budget), owner and workspace/Home access, bounded streaming part uploads,
idempotent completion/cancellation across workers, periodic MinIO cleanup under
``chat-uploads/``, and verified sandbox materialization via ``pull_direct`` (with
a bounded 4 MiB chunked fallback when direct pull is unavailable).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import math
import os
import posixpath
import re
import shlex
from typing import Any
import uuid

from fastapi import HTTPException

from ptc_agent.core.paths import WorkspaceLayout, lexical_path
from ptc_agent.core.project_context import ProjectContext, current_project
from ptc_agent.core.sandbox.retry import RetryPolicy
from src.dalton import chat_upload_storage as storage
from src.dalton.database import chat_uploads as db
from src.dalton.settings import get_dalton_settings, is_dalton_supabase_auth_enabled
from src.server.database.home_workspace import get_home_workspace_id, is_home_id, is_home_row
from src.server.services.computer_disk import CRITICAL_FREE_BYTES, disk_command, parse_df
from src.server.services.persistence.transfer import pull_direct, transfer_timeout_s
from src.server.services.turn_runtime import requested_workspace, turn_route
from src.server.utils.multimodal_context import data_url_decoded_size

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024

MAX_CHAT_UPLOAD_BYTES = 200 * _MIB
MAX_TOTAL_MESSAGE_UPLOAD_BYTES = 600 * _MIB
MAX_MESSAGE_ATTACHMENTS = 5

MAX_INLINE_FILE_BYTES = 30 * _MIB
MAX_INLINE_TOTAL_BYTES = 60 * _MIB

CHAT_UPLOAD_PART_SIZE = 8 * _MIB
MAX_ACTIVE_USER_UPLOADS = 10
MAX_ACTIVE_USER_UPLOAD_BYTES = 1000 * _MIB

UPLOADING_TTL = timedelta(hours=6)
COMPLETE_LEASE = timedelta(minutes=5)
READY_UNUSED_TTL = timedelta(hours=24)

CLEANUP_INTERVAL_S = 300.0
CLEANUP_BATCH_LIMIT = 25

_FALLBACK_CHUNK_BYTES = 4 * _MIB
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Bounds concurrent in-flight 8 MiB part buffers in one backend worker process.
_PART_UPLOAD_SEMAPHORE = asyncio.Semaphore(6)


def _ensure_feature_ready() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    if not storage.is_chat_upload_storage_configured():
        raise HTTPException(
            status_code=503,
            detail="File upload storage is not configured",
        )


def _validate_uuid(value: str, *, field_name: str = "upload_id") -> str:
    cleaned = str(value or "").strip().lower()
    try:
        return str(uuid.UUID(cleaned))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}",
        ) from exc


def _normalize_sha256(value: str | None, *, field_name: str = "sha256") -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    if not _SHA256_RE.match(cleaned):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return cleaned


def sanitize_chat_upload_filename(raw_name: str | None) -> tuple[str, str]:
    """Return ``(display_filename, safe_storage_filename)`` rejecting unsafe components."""
    raw = str(raw_name or "").replace("\x00", "").strip()
    base = posixpath.basename(raw.replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        base = "attachment"
    # Strip control characters from display name while preserving Unicode titles.
    display = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()[:200]
    if not display or display in (".", ".."):
        display = "attachment"

    stem, dot, ext = display.rpartition(".")
    if not dot or not stem:
        raw_stem, clean_ext = display, ""
    else:
        raw_stem = stem
        ext_alnum = re.sub(r"[^A-Za-z0-9]", "", ext)[:12].lower()
        clean_ext = f".{ext_alnum}" if ext_alnum else ""

    safe_stem = _SAFE_CHAR_RE.sub("_", raw_stem).strip("._-")
    if not safe_stem or safe_stem.lower().startswith("wsfiles-"):
        safe_stem = "attachment"
    safe_stem = safe_stem[:72]
    safe_name = f"{safe_stem}{clean_ext}"
    return display, safe_name


def build_sandbox_upload_rel_path(row: dict[str, Any]) -> str:
    """Deterministic workspace-relative destination ``work/uploads/<stem>_<id8><ext>``."""
    _, safe_name = sanitize_chat_upload_filename(
        row.get("safe_filename") or row.get("filename")
    )
    upload_id8 = str(row["upload_id"]).replace("-", "")[:8]
    stem, dot, ext = safe_name.rpartition(".")
    if dot and stem:
        dest_name = f"{stem[:64]}_{upload_id8}.{ext}"
    else:
        dest_name = f"{safe_name[:64]}_{upload_id8}"
    rel = lexical_path(f"work/uploads/{dest_name}")
    if not rel.startswith("work/uploads/") or rel == "work/uploads":
        raise ValueError(f"Unsafe upload target path: {rel!r}")
    return rel


def expected_part_size(row: dict[str, Any], part_number: int) -> int:
    part_count = int(row["part_count"])
    part_size = int(row["part_size"])
    size_bytes = int(row["size_bytes"])
    if part_number < 1 or part_number > part_count:
        raise HTTPException(
            status_code=400,
            detail=f"part_number must be between 1 and {part_count}",
        )
    if part_number < part_count:
        return part_size
    return size_bytes - part_size * (part_count - 1)


def _format_human_bytes(size_bytes: int) -> str:
    if size_bytes >= _MIB:
        return f"{size_bytes / _MIB:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _serialize_upload_row(row: dict[str, Any]) -> dict[str, Any]:
    uploaded_parts = row.get("uploaded_parts") or {}
    part_numbers = sorted(
        int(k) for k in uploaded_parts.keys() if str(k).isdigit()
    )
    expires_at = row.get("expires_at")
    return {
        "upload_id": str(row["upload_id"]),
        "workspace_id": str(row["workspace_id"]),
        "filename": str(row["filename"]),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"]),
        "part_size": int(row["part_size"]),
        "part_count": int(row["part_count"]),
        "uploaded_part_numbers": part_numbers,
        "state": str(row["state"]),
        "sha256": row.get("actual_sha256"),
        "expires_at": (
            expires_at.isoformat()
            if hasattr(expires_at, "isoformat")
            else expires_at
        ),
    }


async def resolve_and_verify_upload_workspace(
    user_id: str,
    workspace_id: str | None,
    *,
    is_existing_colleague_thread: bool = False,
) -> str:
    """Resolve ``workspace_id`` (including ``'home'``) and verify caller write access."""
    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    raw_ws = str(workspace_id or "").strip()
    if not raw_ws or raw_ws.lower() == "home":
        target_ws = get_home_workspace_id(uid)
    else:
        target_ws = _validate_uuid(raw_ws, field_name="workspace_id")

    ws_row = await requested_workspace(
        uid,
        target_ws,
        channel="web",
        is_existing_colleague_thread=is_existing_colleague_thread,
    )
    route = turn_route(uid, ws_row)
    return route.workspace_id


async def initiate_chat_upload(
    *,
    user_id: str,
    workspace_id: str | None,
    filename: str,
    size_bytes: int,
    mime_type: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate limits and workspace permissions, open S3 multipart upload, and persist row."""
    _ensure_feature_ready()

    if int(size_bytes) <= 0:
        raise HTTPException(status_code=400, detail="File is empty")
    if int(size_bytes) > MAX_CHAT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File exceeds the 200 MB limit",
        )

    normalized_sha = _normalize_sha256(expected_sha256, field_name="sha256")
    resolved_ws = await resolve_and_verify_upload_workspace(user_id, workspace_id)
    display_name, safe_name = sanitize_chat_upload_filename(filename)
    clean_mime = (
        str(mime_type or "").strip().split(";")[0].strip()[:120]
        or "application/octet-stream"
    )

    upload_id = str(uuid.uuid4())
    part_size = CHAT_UPLOAD_PART_SIZE
    part_count = max(1, math.ceil(int(size_bytes) / part_size))
    object_key = storage.build_chat_upload_key(resolved_ws, upload_id, safe_name)

    store_upload_id = await asyncio.to_thread(
        storage.open_multipart_upload,
        object_key,
        content_type=clean_mime,
    )
    if not store_upload_id:
        raise HTTPException(
            status_code=503,
            detail="Could not start file upload. Please try again.",
        )

    try:
        row = await db.create_upload_guarded(
            upload_id=upload_id,
            user_id=user_id,
            workspace_id=resolved_ws,
            filename=display_name,
            safe_filename=safe_name,
            mime_type=clean_mime,
            size_bytes=int(size_bytes),
            part_size=part_size,
            part_count=part_count,
            object_key=object_key,
            store_upload_id=store_upload_id,
            expected_sha256=normalized_sha,
            ttl=UPLOADING_TTL,
            max_active_uploads=MAX_ACTIVE_USER_UPLOADS,
            max_active_bytes=MAX_ACTIVE_USER_UPLOAD_BYTES,
        )
    except Exception:
        await asyncio.to_thread(
            storage.abort_multipart_upload, object_key, store_upload_id
        )
        raise

    if row is None:
        await asyncio.to_thread(
            storage.abort_multipart_upload, object_key, store_upload_id
        )
        raise HTTPException(
            status_code=429,
            detail="Too many uploads in progress. Wait for current uploads to finish or remove one.",
        )

    return _serialize_upload_row(row)


async def _require_owned_upload(upload_id: str, user_id: str) -> dict[str, Any]:
    normalized_id = _validate_uuid(upload_id, field_name="upload_id")
    row = await db.get_upload(normalized_id)
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found")
    if str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return row


async def get_chat_upload_status(*, upload_id: str, user_id: str) -> dict[str, Any]:
    _ensure_feature_ready()
    row = await _require_owned_upload(upload_id, user_id)
    return _serialize_upload_row(row)


async def receive_chat_upload_part(
    *,
    upload_id: str,
    part_number: int,
    user_id: str,
    stream: AsyncIterator[bytes],
    content_length_header: str | None = None,
    expected_part_sha256: str | None = None,
) -> dict[str, Any]:
    """Stream one bounded part from client to MinIO without buffering > ``part_size``."""
    _ensure_feature_ready()
    row = await _require_owned_upload(upload_id, user_id)

    state = str(row["state"])
    if state == "ready":
        uploaded = (row.get("uploaded_parts") or {}).get(str(int(part_number)))
        if uploaded:
            return {
                "upload_id": str(row["upload_id"]),
                "part_number": int(part_number),
                "size_bytes": int(uploaded["size"]),
                "sha256": str(uploaded["sha256"]),
                "state": "ready",
            }
        raise HTTPException(status_code=409, detail="Upload is already completed")
    if state != "uploading":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot upload part while upload is {state}",
        )

    expected_len = expected_part_size(row, int(part_number))
    normalized_part_sha = _normalize_sha256(
        expected_part_sha256, field_name="X-Part-Sha256"
    )

    if content_length_header is not None and str(content_length_header).strip():
        try:
            declared = int(str(content_length_header).strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length header"
            ) from exc
        if declared > expected_len:
            raise HTTPException(
                status_code=413, detail="Part exceeds expected byte length"
            )
        if declared < expected_len:
            raise HTTPException(
                status_code=400, detail="Part is shorter than expected byte length"
            )

    async with _PART_UPLOAD_SEMAPHORE:
        buf = bytearray()
        digest = hashlib.sha256()
        async for chunk in stream:
            if not chunk:
                continue
            if len(buf) + len(chunk) > expected_len:
                raise HTTPException(
                    status_code=413,
                    detail="Part exceeds expected byte length",
                )
            buf.extend(chunk)
            digest.update(chunk)

        if len(buf) != expected_len:
            raise HTTPException(
                status_code=400,
                detail=f"Expected {expected_len} bytes for part {part_number}, got {len(buf)}",
            )

        part_sha = digest.hexdigest()
        if normalized_part_sha and not hmac.compare_digest(part_sha, normalized_part_sha):
            raise HTTPException(
                status_code=400,
                detail="Part checksum mismatch",
            )

        payload = bytes(buf)
        buf.clear()

        etag = await asyncio.to_thread(
            storage.upload_part_bytes,
            str(row["object_key"]),
            str(row["store_upload_id"]),
            part_number=int(part_number),
            data=payload,
        )
        del payload

    if not etag:
        raise HTTPException(
            status_code=503,
            detail="Failed to store upload part. Please retry.",
        )

    updated = await db.record_uploaded_part(
        str(row["upload_id"]),
        part_number=int(part_number),
        etag=etag,
        size_bytes=expected_len,
        sha256=part_sha,
    )
    if updated is None:
        current = await db.get_upload(str(row["upload_id"]))
        if current and current.get("state") in ("aborted", "failed"):
            await _cleanup_storage_objects(current)
        raise HTTPException(
            status_code=409,
            detail="Upload is no longer accepting parts",
        )

    return {
        "upload_id": str(updated["upload_id"]),
        "part_number": int(part_number),
        "size_bytes": expected_len,
        "sha256": part_sha,
        "state": str(updated["state"]),
    }


async def complete_chat_upload(
    *,
    upload_id: str,
    user_id: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Assemble multipart upload, stream-verify actual bytes + SHA-256, and mark ``ready``."""
    _ensure_feature_ready()
    normalized_sha = _normalize_sha256(expected_sha256, field_name="sha256")
    row = await _require_owned_upload(upload_id, user_id)
    uid = str(row["upload_id"])

    if row["state"] == "ready":
        if normalized_sha and row.get("actual_sha256") != normalized_sha:
            raise HTTPException(status_code=400, detail="File checksum mismatch")
        return _serialize_upload_row(row)

    if row["state"] in ("aborted", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Upload cannot be completed (state={row['state']})",
        )

    # Re-verify workspace write permission before finalizing.
    await resolve_and_verify_upload_workspace(user_id, str(row["workspace_id"]))

    claimed = await db.claim_complete(
        uid,
        lease=COMPLETE_LEASE,
        expected_sha256=normalized_sha,
    )
    if claimed is None:
        latest = await db.get_upload(uid)
        if latest and latest["state"] == "ready":
            if normalized_sha and latest.get("actual_sha256") != normalized_sha:
                raise HTTPException(status_code=400, detail="File checksum mismatch")
            return _serialize_upload_row(latest)
        if latest and latest["state"] == "completing":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "completing_in_progress",
                    "message": "Upload completion is already in progress",
                },
            )
        raise HTTPException(
            status_code=409,
            detail="Upload is no longer active",
        )

    object_key = str(claimed["object_key"])
    store_upload_id = str(claimed["store_upload_id"])
    expected_total = int(claimed["size_bytes"])
    part_count = int(claimed["part_count"])

    try:
        parts = await asyncio.to_thread(
            storage.list_uploaded_parts, object_key, store_upload_id
        )
        if parts is None:
            # A prior attempt may have assembled the object right before losing its lease.
            held_size = await asyncio.to_thread(storage.object_size, object_key)
            if held_size is None:
                await db.reopen_uploading(uid)
                raise HTTPException(
                    status_code=503,
                    detail="Could not verify uploaded parts. Please retry.",
                )
        else:
            by_num = {int(p["part_number"]): p for p in parts}
            missing = [
                n
                for n in range(1, part_count + 1)
                if n not in by_num
                or int(by_num[n]["size"]) != expected_part_size(claimed, n)
            ]
            if missing:
                await db.reopen_uploading(uid)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "missing_parts",
                        "missing_parts": missing,
                        "message": "Some upload parts are missing",
                    },
                )
            numbered = [(n, str(by_num[n]["etag"])) for n in range(1, part_count + 1)]
            completed = await asyncio.to_thread(
                storage.complete_multipart_upload,
                object_key,
                store_upload_id,
                numbered,
            )
            if not completed:
                held_size = await asyncio.to_thread(storage.object_size, object_key)
                if held_size != expected_total:
                    await db.reopen_uploading(uid)
                    raise HTTPException(
                        status_code=503,
                        detail="Could not finalize upload. Please retry.",
                    )

        verified = await asyncio.to_thread(
            storage.compute_stored_object_digest, object_key
        )
        if verified is None:
            await db.reopen_uploading(uid)
            raise HTTPException(
                status_code=503,
                detail="Could not verify uploaded file. Please retry.",
            )

        actual_size, actual_sha256 = verified
        want_sha = claimed.get("expected_sha256")
        if actual_size != expected_total or actual_size > MAX_CHAT_UPLOAD_BYTES:
            failed_row = await db.mark_failed(
                uid, error="Assembled file size did not match declared size"
            )
            await _cleanup_storage_objects(failed_row or claimed)
            raise HTTPException(
                status_code=400,
                detail="Uploaded file size mismatch",
            )
        if want_sha and not hmac.compare_digest(actual_sha256, str(want_sha)):
            failed_row = await db.mark_failed(
                uid, error="Assembled file checksum mismatch"
            )
            await _cleanup_storage_objects(failed_row or claimed)
            raise HTTPException(
                status_code=400,
                detail="Uploaded file checksum mismatch",
            )

        ready_row = await db.mark_ready(
            uid,
            actual_sha256=actual_sha256,
            ready_ttl=READY_UNUSED_TTL,
        )
        if ready_row is None:
            # Concurrently aborted on another worker while completing; clean up object.
            await _cleanup_storage_objects(claimed)
            raise HTTPException(
                status_code=409,
                detail="Upload was cancelled before completion finished",
            )
        return _serialize_upload_row(ready_row)
    except HTTPException:
        raise
    except Exception:
        await db.reopen_uploading(uid)
        raise


async def _cleanup_storage_objects(row: dict[str, Any]) -> bool:
    object_key = str(row.get("object_key") or "")
    store_upload_id = str(row.get("store_upload_id") or "")
    if not storage.is_chat_upload_key(object_key):
        logger.error("Skipping cleanup of non-chat-upload key %r", object_key)
        return False

    aborted = True
    if store_upload_id:
        aborted = await asyncio.to_thread(
            storage.abort_multipart_upload, object_key, store_upload_id
        )
    deleted = await asyncio.to_thread(storage.delete_object, object_key)
    if aborted and deleted:
        await db.mark_cleaned(str(row["upload_id"]))
        return True
    return False


async def cancel_chat_upload(*, upload_id: str, user_id: str) -> dict[str, Any]:
    """Idempotently cancel an upload and remove temporary multipart parts/object."""
    _ensure_feature_ready()
    row = await _require_owned_upload(upload_id, user_id)
    uid = str(row["upload_id"])

    if row["state"] == "aborted":
        if row.get("cleaned_at") is None:
            await _cleanup_storage_objects(row)
        return {"upload_id": uid, "state": "aborted"}

    if row["state"] == "ready" and row.get("used_at") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attachment has already been sent in a conversation",
        )

    aborted = await db.abort_upload(uid, user_id=user_id)
    if aborted is None:
        latest = await db.get_upload(uid)
        if latest and latest["state"] == "aborted":
            if latest.get("cleaned_at") is None:
                await _cleanup_storage_objects(latest)
            return {"upload_id": uid, "state": "aborted"}
        raise HTTPException(
            status_code=409,
            detail="Attachment can no longer be cancelled",
        )

    await _cleanup_storage_objects(aborted)
    return {"upload_id": uid, "state": "aborted"}


# ---------------------------------------------------------------------------
# Chat request validation, metadata & sandbox materialization
# ---------------------------------------------------------------------------


def extract_chat_upload_contexts(
    additional_context: Sequence[Any] | None,
) -> list[dict[str, Any]]:
    """Extract normalized ``chat_upload`` descriptors from ``additional_context``."""
    if not additional_context:
        return []
    extracted: list[dict[str, Any]] = []
    for item in additional_context:
        if isinstance(item, dict):
            if item.get("type") == "chat_upload":
                extracted.append(
                    {
                        "type": "chat_upload",
                        "upload_id": str(item.get("upload_id") or "").strip(),
                        "name": item.get("name"),
                        "mime_type": item.get("mime_type"),
                        "size": item.get("size"),
                    }
                )
        elif getattr(item, "type", None) == "chat_upload":
            extracted.append(
                {
                    "type": "chat_upload",
                    "upload_id": str(getattr(item, "upload_id", "") or "").strip(),
                    "name": getattr(item, "name", None),
                    "mime_type": getattr(item, "mime_type", None),
                    "size": getattr(item, "size", None),
                }
            )
    return extracted


async def validate_and_claim_request_attachments(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str,
    workspace_id: str,
    is_existing_colleague_thread: bool = False,
) -> list[dict[str, Any]]:
    """Validate inline budgets + ``chat_upload`` references pre-stream, stamping ``used_at``.

    Returns the verified ``dalton_chat_file_uploads`` rows in attachment order.
    """
    if not additional_context:
        return []

    inline_count = 0
    inline_bytes = 0
    for item in additional_context:
        item_type = (
            item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        )
        if item_type in ("image", "pdf", "file"):
            inline_count += 1
            data_url = (
                item.get("data", "")
                if isinstance(item, dict)
                else getattr(item, "data", "")
            ) or ""
            decoded = data_url_decoded_size(str(data_url))
            if decoded <= 0:
                raise HTTPException(
                    status_code=400, detail="Empty attachment is not allowed"
                )
            if decoded > MAX_INLINE_FILE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Inline attachment exceeds the 30 MB inline limit",
                )
            inline_bytes += decoded
            if inline_bytes > MAX_INLINE_TOTAL_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Inline attachments exceed the 60 MB total inline limit",
                )

    upload_items = extract_chat_upload_contexts(additional_context)
    total_attachments = inline_count + len(upload_items)
    if total_attachments > MAX_MESSAGE_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_MESSAGE_ATTACHMENTS} attachments are allowed per message",
        )

    if not upload_items:
        return []

    _ensure_feature_ready()
    canonical_ws = await resolve_and_verify_upload_workspace(
        user_id,
        workspace_id,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )

    upload_ids = [
        _validate_uuid(u["upload_id"], field_name="upload_id") for u in upload_items
    ]
    rows_by_id = await db.get_uploads_by_ids(upload_ids)

    verified_rows: list[dict[str, Any]] = []
    total_bytes = inline_bytes
    for uid in upload_ids:
        row = rows_by_id.get(uid)
        if not row:
            raise HTTPException(status_code=404, detail="Uploaded attachment not found")
        if str(row["user_id"]) != str(user_id):
            raise HTTPException(status_code=403, detail="Forbidden")
        if str(row["workspace_id"]) != str(canonical_ws):
            raise HTTPException(
                status_code=403,
                detail="Attachment belongs to a different workspace",
            )
        if not storage.is_chat_upload_key_for_workspace(
            str(row.get("object_key") or ""), canonical_ws
        ):
            raise HTTPException(
                status_code=403,
                detail="Attachment storage key does not match workspace",
            )
        state = str(row["state"])
        if state in ("uploading", "completing"):
            raise HTTPException(
                status_code=409,
                detail="Attachment is still uploading. Wait for it to finish before sending.",
            )
        if state != "ready" or row.get("cleaned_at") is not None:
            raise HTTPException(
                status_code=410,
                detail="Attachment has expired or was removed",
            )

        stamped = await db.mark_used(
            uid,
            user_id=user_id,
            workspace_id=canonical_ws,
        )
        if stamped is None:
            raise HTTPException(
                status_code=410,
                detail="Attachment has expired or was removed",
            )
        size = int(stamped["size_bytes"])
        if size <= 0 or size > MAX_CHAT_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail="Attachment exceeds the 200 MB limit"
            )
        total_bytes += size
        if total_bytes > MAX_TOTAL_MESSAGE_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Attachments exceed the per-message total size limit",
            )
        verified_rows.append(stamped)

    return verified_rows


async def build_chat_upload_history_metadata(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build persisted ``query_metadata['attachments']`` entries for ``chat_upload`` items."""
    upload_items = extract_chat_upload_contexts(additional_context)
    if not upload_items:
        return []

    valid_ids: list[str] = []
    for item in upload_items:
        try:
            valid_ids.append(_validate_uuid(item["upload_id"]))
        except HTTPException:
            continue
    rows_by_id = await db.get_uploads_by_ids(valid_ids) if valid_ids else {}

    out: list[dict[str, Any]] = []
    for item in upload_items:
        raw_id = item["upload_id"]
        row = rows_by_id.get(raw_id.lower())
        if (
            row is not None
            and (user_id is None or str(row["user_id"]) == str(user_id))
            and (workspace_id is None or str(row["workspace_id"]) == str(workspace_id))
        ):
            mime = str(row["mime_type"])
            att_type = (
                "pdf"
                if mime == "application/pdf"
                else "image"
                if mime.startswith("image/")
                else "file"
            )
            out.append(
                {
                    "name": str(row["filename"]),
                    "type": att_type,
                    "mime_type": mime,
                    "size": int(row["size_bytes"]),
                    "status": "uploaded",
                    "upload_id": str(row["upload_id"]),
                }
            )
        else:
            out.append(
                {
                    "name": str(item.get("name") or "file")[:200],
                    "type": "file",
                    "size": int(item.get("size") or 0),
                    "status": "uploaded",
                    "upload_id": raw_id,
                }
            )
    return out


async def _free_disk_bytes(sandbox: Any, work_dir: str) -> int | None:
    command, timeout = disk_command(work_dir, breakdown=False)
    try:
        result = await sandbox._runtime_call(
            sandbox.runtime.exec, command, timeout, retry_policy=RetryPolicy.SAFE
        )
    except Exception:
        return None
    reading = parse_df(getattr(result, "stdout", "") or "")
    return reading.free_bytes if reading else None


async def _materialize_via_chunked_stream(
    sandbox: Any,
    *,
    object_key: str,
    abs_target_path: str,
    expected_size: int,
    expected_sha256: str,
    upload_id: str,
) -> None:
    """Fallback streaming writer (4 MiB chunks) when ``pull_direct`` cannot reach MinIO."""
    uid_hex = upload_id.replace("-", "")[:12]
    parent_dir = posixpath.dirname(abs_target_path)
    stage_path = f"{parent_dir}/.wsfiles-stage-{uid_hex}"
    part_path = f"{parent_dir}/.wsfiles-part-{uid_hex}"

    # Fast-path idempotency check if target already holds the exact bytes.
    verify_existing_cmd = (
        "python3 -c '"
        "import hashlib, os, sys; "
        "p, exp_sz, exp_sha = sys.argv[1], int(sys.argv[2]), sys.argv[3]; "
        "sys.exit(1) if (not os.path.isfile(p) or os.path.islink(p) or os.path.getsize(p) != exp_sz) else None; "
        "h = hashlib.sha256();\n"
        "with open(p, \"rb\") as f:\n"
        "    while True:\n"
        "        b = f.read(1048576)\n"
        "        if not b: break\n"
        "        h.update(b)\n"
        "sys.exit(0 if h.hexdigest() == exp_sha else 1)"
        "' "
        f"{shlex.quote(abs_target_path)} {int(expected_size)} {shlex.quote(expected_sha256)}"
    )
    if expected_sha256:
        probe_timeout = max(20, transfer_timeout_s(expected_size))
        probe = await sandbox._runtime_call(
            sandbox.runtime.exec,
            verify_existing_cmd,
            timeout=probe_timeout,
            total_timeout=max(120.0, float(probe_timeout) + 30.0),
            retry_policy=RetryPolicy.SAFE,
        )
        if getattr(probe, "exit_code", 1) == 0:
            return

    prep = await sandbox._runtime_call(
        sandbox.runtime.exec,
        f"mkdir -p -- {shlex.quote(parent_dir)} && rm -f -- {shlex.quote(stage_path)} {shlex.quote(part_path)}",
        timeout=20,
        retry_policy=RetryPolicy.SAFE,
    )
    if getattr(prep, "exit_code", 1) != 0:
        raise RuntimeError("Failed to prepare upload directory in sandbox")

    def _open_iterator():
        return storage.iter_stored_object_chunks(
            object_key, chunk_size=_FALLBACK_CHUNK_BYTES
        )

    it = await asyncio.to_thread(_open_iterator)
    try:
        while True:
            chunk = await asyncio.to_thread(next, it, None)
            if chunk is None:
                break
            await sandbox._runtime_call(
                sandbox.runtime.upload_file,
                chunk,
                part_path,
                retry_policy=RetryPolicy.SAFE,
            )
            append_res = await sandbox._runtime_call(
                sandbox.runtime.exec,
                f"cat -- {shlex.quote(part_path)} >> {shlex.quote(stage_path)} && rm -f -- {shlex.quote(part_path)}",
                timeout=30,
                retry_policy=RetryPolicy.SAFE,
            )
            if getattr(append_res, "exit_code", 1) != 0:
                raise RuntimeError("Failed to append streamed chunk in sandbox")

        finalize_script = (
            "python3 -c '"
            "import hashlib, os, sys; "
            "stage, target, exp_sz, exp_sha = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]; "
            "h = hashlib.sha256(); n = 0;\n"
            "with open(stage, \"rb\") as f:\n"
            "    while True:\n"
            "        b = f.read(1048576)\n"
            "        if not b: break\n"
            "        n += len(b); h.update(b)\n"
            "if n != exp_sz or h.hexdigest() != exp_sha:\n"
            "    os.unlink(stage); sys.exit(2)\n"
            "os.chmod(stage, 0o644)\n"
            "os.replace(stage, target)\n"
            "' "
            f"{shlex.quote(stage_path)} {shlex.quote(abs_target_path)} "
            f"{int(expected_size)} {shlex.quote(expected_sha256)}"
        )
        fin_timeout = max(30, transfer_timeout_s(expected_size))
        fin_res = await sandbox._runtime_call(
            sandbox.runtime.exec,
            finalize_script,
            timeout=fin_timeout,
            total_timeout=max(120.0, float(fin_timeout) + 30.0),
            retry_policy=RetryPolicy.SAFE,
        )
        if getattr(fin_res, "exit_code", 1) != 0:
            raise RuntimeError("Streamed sandbox upload verification failed")
    except Exception:
        try:
            await sandbox._runtime_call(
                sandbox.runtime.exec,
                f"rm -f -- {shlex.quote(stage_path)} {shlex.quote(part_path)}",
                timeout=15,
                retry_policy=RetryPolicy.SAFE,
            )
        except Exception:
            pass
        raise
    finally:
        try:
            await asyncio.to_thread(it.close)
        except Exception:
            pass


async def _place_row_in_sandbox(
    sandbox: Any,
    row: dict[str, Any],
    *,
    layout: WorkspaceLayout,
    abs_path: str,
    rel_path: str,
    can_direct: bool,
) -> None:
    """Place one verified ``dalton_chat_file_uploads`` row at ``abs_path``."""
    size_bytes = int(row["size_bytes"])
    sha256_hex = str(row["actual_sha256"] or "")
    object_key = str(row["object_key"])
    uid = str(row["upload_id"])

    placed = False
    if can_direct and sha256_hex:
        signed_url = await asyncio.to_thread(
            storage.sign_download_url,
            object_key,
            expires_in=transfer_timeout_s(size_bytes) + 300,
        )
        if signed_url:
            results = await pull_direct(
                sandbox,
                [
                    {
                        "kind": "file",
                        "path": rel_path,
                        "size": size_bytes,
                        "mode": 0o644,
                        "sha256": sha256_hex,
                        "url": signed_url,
                    }
                ],
                layout=layout,
            )
            res = results.get(rel_path) or {}
            status_val = res.get("status")
            if status_val == "ok":
                placed = True
            elif status_val != "unreachable":
                raise RuntimeError(
                    f"Failed to materialize chat attachment {row['filename']}: "
                    f"{res.get('error') or status_val}"
                )

    if not placed:
        await _materialize_via_chunked_stream(
            sandbox,
            object_key=object_key,
            abs_target_path=abs_path,
            expected_size=size_bytes,
            expected_sha256=sha256_hex,
            upload_id=uid,
        )


def _sandbox_layout(sandbox: Any, project: ProjectContext | None) -> WorkspaceLayout:
    workspace_fn = getattr(sandbox, "workspace", None)
    if callable(workspace_fn):
        return workspace_fn(project)
    computer_root = str(
        getattr(getattr(sandbox, "config", None), "filesystem", None)
        and sandbox.config.filesystem.working_directory
        or "/home/workspace"
    )
    dir_name = (project.dir_name or "") if project is not None else ""
    return WorkspaceLayout(root=computer_root, dir_name=dir_name)


async def materialize_chat_uploads(
    additional_context: Sequence[Any] | None,
    *,
    sandbox: Any | None,
    user_id: str | None,
    workspace_id: str | None,
    project: ProjectContext | None = None,
    is_existing_colleague_thread: bool = False,
) -> list[str]:
    """Re-verify permissions and materialize ``chat_upload`` attachments into ``work/uploads/``.

    Returns per-file note strings ready for ``build_file_reminder(notes)``.
    Never places raw file bytes into model messages.
    """
    upload_items = extract_chat_upload_contexts(additional_context)
    if not upload_items:
        return []

    if not user_id or not workspace_id:
        raise PermissionError("Missing caller or workspace identity for chat upload")

    verified_rows = await validate_and_claim_request_attachments(
        upload_items,
        user_id=user_id,
        workspace_id=workspace_id,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )

    eff_project = project if project is not None else current_project()
    notes: list[str] = []

    if sandbox is None:
        for row in verified_rows:
            notes.append(
                f"The user attached a file ({row['filename']}, {row['mime_type']}, "
                f"{_format_human_bytes(int(row['size_bytes']))}), but the workspace "
                "sandbox was unavailable so it could not be placed on disk."
            )
        return notes

    layout = _sandbox_layout(sandbox, eff_project)

    total_needed = sum(int(r["size_bytes"]) for r in verified_rows)
    free_bytes = await _free_disk_bytes(sandbox, layout.workspace)
    if free_bytes is not None and free_bytes - CRITICAL_FREE_BYTES < total_needed:
        raise RuntimeError(
            f"Not enough free disk space in workspace ({max(free_bytes, 0) // _MIB} MB free)"
        )

    can_direct = bool(
        getattr(getattr(sandbox, "provider", None), "direct_blob_transfer", True)
    )

    for row in verified_rows:
        rel_path = build_sandbox_upload_rel_path(row)
        if not sandbox.validate_path(rel_path, eff_project):
            raise PermissionError(f"Invalid sandbox upload target: {rel_path!r}")
        abs_path = sandbox.normalize_path(rel_path, project=eff_project)
        size_bytes = int(row["size_bytes"])

        async with sandbox.path_write_lock(abs_path):
            # Re-verify workspace write access right before placing on disk.
            await resolve_and_verify_upload_workspace(
                user_id,
                workspace_id,
                is_existing_colleague_thread=is_existing_colleague_thread,
            )
            await _place_row_in_sandbox(
                sandbox,
                row,
                layout=layout,
                abs_path=abs_path,
                rel_path=rel_path,
                can_direct=can_direct,
            )

        notes.append(
            f"The user attached a file ({row['filename']}, {row['mime_type']}, "
            f"{_format_human_bytes(size_bytes)}). It has been saved to {rel_path}. "
            "Use Python to process it."
        )

    return notes


async def materialize_steered_chat_uploads(payload: dict[str, Any]) -> str:
    """Materialize ``chat_uploads`` carried on a held steer job and return its reminder block."""
    chat_uploads = payload.get("chat_uploads")
    if not chat_uploads:
        return ""
    user_id = str(payload.get("user_id") or "").strip()
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not user_id or not workspace_id:
        return ""

    from src.server.database.workspace import get_workspace
    from src.server.services.workspace_manager import WorkspaceManager
    from src.server.utils.multimodal_context import build_file_reminder

    ws_row = await get_workspace(workspace_id)
    ws_owner = (
        str(ws_row["user_id"])
        if ws_row and ws_row.get("user_id")
        else user_id
    )
    is_colleague = ws_owner != user_id
    wm = WorkspaceManager.get_instance()
    session = await wm.get_session_for_workspace(workspace_id, user_id=ws_owner)
    notes = await materialize_chat_uploads(
        chat_uploads,
        sandbox=session.sandbox if session else None,
        user_id=user_id,
        workspace_id=workspace_id,
        project=current_project(),
        is_existing_colleague_thread=is_colleague,
    )
    return build_file_reminder(notes) if notes else ""


# ---------------------------------------------------------------------------
# Cold-workspace recovery, file/workspace delete hooks & bounded sweeper
# ---------------------------------------------------------------------------


async def _load_workspace_permission_state(
    workspace_id: str,
    *,
    conn: Any = None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Return ``(owner_user_id, status, workspace_row)`` for ``workspace_id``."""
    try:
        normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    except HTTPException:
        return None, "invalid", None

    ws_dict = await db.get_workspace_permission_row(normalized_ws, conn=conn)
    if not ws_dict:
        return None, None, None
    owner_id = str(ws_dict.get("user_id") or "").strip() or None
    status = str(ws_dict.get("status") or "").strip() or None
    return owner_id, status, ws_dict


async def _is_authorized_row_for_recovery(
    row: dict[str, Any],
    *,
    workspace_id: str,
    ws_owner_id: str | None,
    ws_status: str | None,
    ws_row: dict[str, Any] | None,
    conn: Any = None,
) -> bool:
    """Verify row ownership, workspace scope, key isolation, and live workspace permissions."""
    if ws_status in ("deleted", "invalid"):
        return False
    if str(row.get("workspace_id") or "") != str(workspace_id):
        return False
    if (
        str(row.get("state") or "") != "ready"
        or row.get("used_at") is None
        or row.get("cleaned_at") is not None
    ):
        return False

    object_key = str(row.get("object_key") or "")
    if not storage.is_chat_upload_key_for_workspace(object_key, workspace_id):
        return False

    size_bytes = int(row.get("size_bytes") or 0)
    if size_bytes <= 0 or size_bytes > MAX_CHAT_UPLOAD_BYTES:
        return False

    row_user_id = str(row.get("user_id") or "").strip()
    if not row_user_id:
        return False

    if ws_owner_id is None:
        # Unbound Home before its workspaces row is inserted: must match row_user_id's Home ID.
        return is_home_id(row_user_id, workspace_id)

    if row_user_id == ws_owner_id:
        return True

    # Cross-user upload in Home is never allowed.
    if is_home_row(ws_row) or is_home_id(ws_owner_id, workspace_id):
        return False

    if not is_dalton_supabase_auth_enabled() or ws_row is None:
        return False

    from src.dalton.sharing import ColleagueAccessRevokedError, require_workspace_access

    for channel in ("web", "cos"):
        try:
            await require_workspace_access(
                ws_row,
                row_user_id,
                channel=channel,
                need="write",
                workspace=ws_row,
                conn=conn,
            )
            return True
        except (HTTPException, ColleagueAccessRevokedError):
            continue
    return False


async def has_retained_workspace_chat_uploads(
    workspace_id: str,
    *,
    conn: Any = None,
) -> bool:
    """True when ``workspace_id`` has active accepted chat uploads owed on cold restore."""
    if not get_dalton_settings().enabled:
        return False
    if not storage.is_chat_upload_storage_configured():
        return False
    try:
        normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    except HTTPException:
        return False
    return await db.has_retained_workspace_uploads(normalized_ws, conn=conn)


async def _probe_missing_sandbox_paths(
    sandbox: Any, abs_paths: Sequence[str]
) -> set[str]:
    if not abs_paths:
        return set()
    quoted = " ".join(shlex.quote(p) for p in abs_paths)
    probe_cmd = (
        f"for p in {quoted}; do "
        'if [ ! -f "$p" ] || [ -L "$p" ]; then printf \'%s\\n\' "$p"; fi; '
        "done"
    )
    res = await sandbox._runtime_call(
        sandbox.runtime.exec,
        probe_cmd,
        timeout=20,
        retry_policy=RetryPolicy.SAFE,
    )
    if getattr(res, "exit_code", 1) != 0:
        return set(abs_paths)
    stdout = getattr(res, "stdout", "") or ""
    return {line.strip() for line in stdout.splitlines() if line.strip()}


async def rematerialize_workspace_chat_uploads(
    workspace_id: str,
    sandbox: Any | None,
    *,
    layout: WorkspaceLayout,
    conn: Any = None,
) -> dict[str, int]:
    """Rematerialize missing accepted chat uploads into ``layout`` on cold restore or turn start."""
    if (
        sandbox is None
        or not workspace_id
        or not get_dalton_settings().enabled
        or not storage.is_chat_upload_storage_configured()
    ):
        return {"restored": 0, "errors": 0}

    try:
        normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    except HTTPException:
        return {"restored": 0, "errors": 0}

    rows = await db.list_retained_workspace_uploads(normalized_ws, conn=conn)
    if not rows:
        return {"restored": 0, "errors": 0}

    ws_owner_id, ws_status, ws_row = await _load_workspace_permission_state(
        normalized_ws, conn=conn
    )
    if ws_status in ("deleted", "invalid"):
        return {"restored": 0, "errors": 0}

    target_project = ProjectContext(
        workspace_id=normalized_ws,
        dir_name=layout.dir_name,
    )
    authorized: list[tuple[dict[str, Any], str, str]] = []
    for row in rows:
        ok = await _is_authorized_row_for_recovery(
            row,
            workspace_id=normalized_ws,
            ws_owner_id=ws_owner_id,
            ws_status=ws_status,
            ws_row=ws_row,
            conn=conn,
        )
        if not ok:
            logger.warning(
                "Skipping unauthorized chat upload %s during recovery for workspace %s",
                row.get("upload_id"),
                normalized_ws,
            )
            continue
        rel_path = build_sandbox_upload_rel_path(row)
        abs_path = posixpath.join(layout.workspace, rel_path)
        if not sandbox.validate_path(rel_path, target_project):
            logger.warning(
                "Skipping invalid sandbox target %r for chat upload %s",
                abs_path,
                row.get("upload_id"),
            )
            continue
        authorized.append((row, rel_path, abs_path))

    if not authorized:
        return {"restored": 0, "errors": 0}

    missing_abs = await _probe_missing_sandbox_paths(
        sandbox, [abs_p for _, _, abs_p in authorized]
    )
    missing_entries = [
        entry for entry in authorized if entry[2] in missing_abs
    ]
    if not missing_entries:
        return {"restored": 0, "errors": 0}

    total_needed = sum(int(r["size_bytes"]) for r, _, _ in missing_entries)
    free_bytes = await _free_disk_bytes(sandbox, layout.workspace)
    if free_bytes is not None and free_bytes - CRITICAL_FREE_BYTES < total_needed:
        logger.warning(
            "Insufficient free disk space (%s bytes) to restore %d chat upload(s) in workspace %s",
            free_bytes,
            len(missing_entries),
            normalized_ws,
        )
        return {"restored": 0, "errors": len(missing_entries)}

    can_direct = bool(
        getattr(getattr(sandbox, "provider", None), "direct_blob_transfer", True)
    )
    restored = 0
    errors = 0
    for row, rel_path, abs_path in missing_entries:
        try:
            async with sandbox.path_write_lock(abs_path):
                await _place_row_in_sandbox(
                    sandbox,
                    row,
                    layout=layout,
                    abs_path=abs_path,
                    rel_path=rel_path,
                    can_direct=can_direct,
                )
            restored += 1
        except Exception as exc:
            errors += 1
            logger.warning(
                "Failed to rematerialize chat upload %s (%s) in workspace %s: %s",
                row.get("upload_id"),
                rel_path,
                normalized_ws,
                exc,
            )
    return {"restored": restored, "errors": errors}


async def ensure_workspace_chat_uploads_present(
    workspace_id: str | None,
    sandbox: Any | None,
    *,
    project: ProjectContext | None = None,
) -> dict[str, int]:
    """Ensure retained workspace chat uploads exist on disk before a turn runs."""
    if not workspace_id or sandbox is None:
        return {"restored": 0, "errors": 0}
    eff_project = project if project is not None else current_project()
    layout = _sandbox_layout(sandbox, eff_project)
    return await rematerialize_workspace_chat_uploads(
        workspace_id, sandbox, layout=layout
    )


async def list_retained_workspace_file_tree_entries(
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Return offline file-tree metadata entries for retained chat uploads."""
    if not get_dalton_settings().enabled:
        return []
    try:
        normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    except HTTPException:
        return []
    rows = await db.list_retained_workspace_uploads(normalized_ws)
    if not rows:
        return []
    ws_owner_id, ws_status, ws_row = await _load_workspace_permission_state(
        normalized_ws
    )
    if ws_status in ("deleted", "invalid"):
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        if not await _is_authorized_row_for_recovery(
            row,
            workspace_id=normalized_ws,
            ws_owner_id=ws_owner_id,
            ws_status=ws_status,
            ws_row=ws_row,
        ):
            continue
        rel_path = build_sandbox_upload_rel_path(row)
        out.append(
            {
                "path": rel_path,
                "name": posixpath.basename(rel_path),
                "size": int(row["size_bytes"]),
                "mime_type": row.get("mime_type"),
                "is_binary": True,
                "modified_at": row.get("used_at") or row.get("updated_at"),
            }
        )
    return out


async def retire_deleted_workspace_chat_uploads(
    *,
    workspace_id: str,
    caller_user_id: str,
    deleted_client_paths: Sequence[str],
) -> int:
    """Retire and clean retained chat uploads whose ``work/uploads/...`` file was deleted."""
    if not deleted_client_paths or not get_dalton_settings().enabled:
        return 0

    canonical_ws = await resolve_and_verify_upload_workspace(
        caller_user_id, workspace_id
    )
    normalized_deleted: set[str] = set()
    for raw_p in deleted_client_paths:
        cleaned = str(raw_p or "").strip().lstrip("/")
        if not cleaned:
            continue
        try:
            normalized_deleted.add(lexical_path(cleaned))
        except Exception:
            continue
    if not normalized_deleted:
        return 0

    retained = await db.list_retained_workspace_uploads(canonical_ws)
    if not retained:
        return 0

    matched_ids: list[str] = []
    for row in retained:
        rel_path = build_sandbox_upload_rel_path(row)
        if rel_path in normalized_deleted or any(
            rel_path.startswith(f"{d}/") for d in normalized_deleted
        ):
            matched_ids.append(str(row["upload_id"]))

    if not matched_ids:
        return 0

    retired_rows = await db.retire_uploads_by_ids(
        matched_ids, workspace_id=canonical_ws
    )
    cleaned_count = 0
    if storage.is_chat_upload_storage_configured():
        for row in retired_rows:
            if not storage.is_chat_upload_key_for_workspace(
                str(row.get("object_key") or ""), canonical_ws
            ):
                continue
            if await _cleanup_storage_objects(row):
                cleaned_count += 1
    return cleaned_count


async def cleanup_deleted_workspace_chat_uploads(
    workspace_id: str,
    *,
    caller_user_id: str | None = None,
) -> int:
    """Retire and clean all chat uploads for a tombstoned (``status='deleted'``) workspace."""
    if not get_dalton_settings().enabled:
        return 0
    normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    ws_owner_id, ws_status, _ = await _load_workspace_permission_state(normalized_ws)
    if caller_user_id is not None and str(caller_user_id) != str(ws_owner_id or ""):
        raise HTTPException(status_code=403, detail="Forbidden")
    if ws_status != "deleted":
        raise HTTPException(
            status_code=409,
            detail="Workspace must be deleted before purging workspace chat uploads",
        )

    retired_rows = await db.retire_workspace_uploads(normalized_ws)
    cleaned_count = 0
    if storage.is_chat_upload_storage_configured():
        for row in retired_rows:
            if not storage.is_chat_upload_key_for_workspace(
                str(row.get("object_key") or ""), normalized_ws
            ):
                continue
            if await _cleanup_storage_objects(row):
                cleaned_count += 1
    return cleaned_count


async def sweep_expired_chat_uploads_once(
    *,
    limit: int = CLEANUP_BATCH_LIMIT,
    now: datetime | None = None,
) -> int:
    """Clean up one bounded batch of expired/aborted/failed ``chat-uploads/`` objects."""
    if not get_dalton_settings().enabled:
        return 0
    if not storage.is_chat_upload_storage_configured():
        return 0

    claimed = await db.claim_cleanup_batch(limit=limit, now=now)
    cleaned_count = 0
    for row in claimed:
        uid = str(row["upload_id"])
        try:
            ok = await _cleanup_storage_objects(row)
            if ok:
                cleaned_count += 1
            else:
                await db.release_cleanup_claim(uid)
        except Exception as exc:
            logger.warning("Cleanup failed for chat upload %s: %s", uid, exc)
            await db.release_cleanup_claim(uid)

    await db.prune_cleaned_rows(now=now)
    return cleaned_count


async def run_chat_upload_cleanup_loop(
    *, interval_s: float = CLEANUP_INTERVAL_S
) -> None:
    """Periodic lifespan cleanup loop for ``dalton_chat_file_uploads``."""
    if os.getenv("DALTON_CHAT_UPLOAD_CLEANUP_ENABLED", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    while True:
        await asyncio.sleep(interval_s)
        try:
            await sweep_expired_chat_uploads_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Chat upload cleanup sweep error: %s", exc)
