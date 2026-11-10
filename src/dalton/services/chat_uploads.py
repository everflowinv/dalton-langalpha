"""Service layer for chat composer large-file uploads and sandbox materialization.

Enforces byte limits (1 byte .. 200 MiB per file, 600 MiB per message, 30/60 MiB
inline budget), owner and workspace/Home access, bounded streaming part uploads,
idempotent completion/cancellation across workers, periodic MinIO cleanup under
``chat-uploads/``, and verified sandbox materialization via ``pull_direct`` (with
a bounded 4 MiB chunked fallback when direct pull is unavailable).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import math
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
from src.dalton.settings import (
    get_chat_upload_part_idle_timeout_s,
    get_chat_upload_part_total_timeout_s,
    get_dalton_settings,
    is_chat_upload_cleanup_enabled,
)
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
PROVISIONAL_CLAIM_TTL = timedelta(minutes=15)

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


async def _verify_upload_row_write_scope(row: dict[str, Any], user_id: str) -> str:
    """Verify owner identity, live workspace/Home write permission, and object-key scope."""
    if str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    target_ws = str(row["workspace_id"])
    resolved_ws = await resolve_and_verify_upload_workspace(user_id, target_ws)
    if str(resolved_ws) != target_ws:
        raise HTTPException(
            status_code=403,
            detail="Attachment belongs to a different workspace",
        )
    if not storage.is_chat_upload_key_for_workspace(
        str(row.get("object_key") or ""), resolved_ws
    ):
        raise HTTPException(
            status_code=403,
            detail="Attachment storage key does not match workspace",
        )
    return resolved_ws


def _as_utc(ts: Any) -> datetime | None:
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _has_live_provisional_claim(row: dict[str, Any], now_utc: datetime) -> bool:
    claims = row.get("pending_claims")
    if not isinstance(claims, dict) or not claims:
        return False
    for raw_until in claims.values():
        if not raw_until:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw_until).replace("Z", "+00:00"))
        except ValueError:
            continue
        until_utc = _as_utc(parsed)
        if until_utc is not None and until_utc > now_utc:
            return True
    return False


async def get_chat_upload_status(*, upload_id: str, user_id: str) -> dict[str, Any]:
    _ensure_feature_ready()
    row = await _require_owned_upload(upload_id, user_id)
    return _serialize_upload_row(row)


def _check_part_accepting_row(
    row: dict[str, Any],
    part_number: int,
    *,
    now_utc: datetime,
    expected_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Return an idempotent response dict if already ``ready``, or raise if not accepting parts."""
    state = str(row["state"])
    if state == "ready" and row.get("cleaned_at") is None:
        uploaded = (row.get("uploaded_parts") or {}).get(str(int(part_number)))
        if uploaded:
            recorded_sha = str(uploaded["sha256"])
            if expected_sha256 and not hmac.compare_digest(recorded_sha, expected_sha256):
                raise HTTPException(status_code=400, detail="Part checksum mismatch")
            return {
                "upload_id": str(row["upload_id"]),
                "part_number": int(part_number),
                "size_bytes": int(uploaded["size"]),
                "sha256": recorded_sha,
                "state": "ready",
            }
        raise HTTPException(status_code=409, detail="Upload is already completed")
    if state != "uploading" or row.get("cleaned_at") is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot upload part while upload is {state}",
        )
    exp = _as_utc(row.get("expires_at"))
    if exp is None or exp <= now_utc:
        raise HTTPException(
            status_code=410,
            detail="Upload has expired",
        )
    return None


async def receive_chat_upload_part(
    *,
    upload_id: str,
    part_number: int,
    user_id: str,
    stream: AsyncIterator[bytes],
    content_length_header: str | None = None,
    expected_part_sha256: str | None = None,
) -> dict[str, Any]:
    """Stream one bounded part from client to MinIO under idle and total deadlines."""
    _ensure_feature_ready()
    normalized_part_sha = _normalize_sha256(
        expected_part_sha256, field_name="X-Part-Sha256"
    )
    row = await _require_owned_upload(upload_id, user_id)
    await _verify_upload_row_write_scope(row, user_id)

    early_ready = _check_part_accepting_row(
        row,
        int(part_number),
        now_utc=datetime.now(timezone.utc),
        expected_sha256=normalized_part_sha,
    )
    if early_ready is not None:
        return early_ready

    expected_len = expected_part_size(row, int(part_number))

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

    idle_timeout_s = get_chat_upload_part_idle_timeout_s()
    total_timeout_s = get_chat_upload_part_total_timeout_s()
    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + total_timeout_s

    try:
        await asyncio.wait_for(
            _PART_UPLOAD_SEMAPHORE.acquire(),
            timeout=max(0.01, overall_deadline - loop.time()),
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=408,
            detail="Part upload timed out waiting for an available upload slot",
        ) from exc

    buf = bytearray()
    payload: bytes | None = None
    etag: str | None = None
    part_sha = ""
    try:
        # Re-verify live workspace write scope, durable state, and expiry after acquiring the slot.
        row = await _require_owned_upload(upload_id, user_id)
        await _verify_upload_row_write_scope(row, user_id)
        slot_ready = _check_part_accepting_row(
            row,
            int(part_number),
            now_utc=datetime.now(timezone.utc),
            expected_sha256=normalized_part_sha,
        )
        if slot_ready is not None:
            return slot_ready

        digest = hashlib.sha256()
        aiter = stream.__aiter__()
        while True:
            remaining_total = overall_deadline - loop.time()
            if remaining_total <= 0:
                raise HTTPException(
                    status_code=408,
                    detail="Part upload exceeded total body read deadline",
                )
            step_timeout = min(idle_timeout_s, remaining_total)
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=step_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                detail = (
                    "Part upload timed out waiting for request body bytes"
                    if remaining_total > idle_timeout_s
                    else "Part upload exceeded total body read deadline"
                )
                raise HTTPException(status_code=408, detail=detail) from exc
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

        # Re-check live workspace write permission, upload state, and expiry right before
        # mutating object storage so a mid-stream revocation/cancel/expiry never writes to MinIO.
        fresh = await _require_owned_upload(str(row["upload_id"]), user_id)
        await _verify_upload_row_write_scope(fresh, user_id)
        post_read_ready = _check_part_accepting_row(
            fresh,
            int(part_number),
            now_utc=datetime.now(timezone.utc),
            expected_sha256=part_sha,
        )
        if post_read_ready is not None:
            return post_read_ready

        existing_part = (fresh.get("uploaded_parts") or {}).get(str(int(part_number)))
        if (
            isinstance(existing_part, dict)
            and existing_part.get("etag")
            and int(existing_part.get("size") or 0) == expected_len
            and hmac.compare_digest(str(existing_part.get("sha256") or ""), part_sha)
        ):
            return {
                "upload_id": str(fresh["upload_id"]),
                "part_number": int(part_number),
                "size_bytes": expected_len,
                "sha256": part_sha,
                "state": str(fresh["state"]),
            }

        payload = bytes(buf)
        buf.clear()

        etag = await asyncio.to_thread(
            storage.upload_part_bytes,
            str(fresh["object_key"]),
            str(fresh["store_upload_id"]),
            part_number=int(part_number),
            data=payload,
        )
    finally:
        buf.clear()
        payload = None
        _PART_UPLOAD_SEMAPHORE.release()

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
            if current.get("cleaned_at") is not None:
                current = (
                    await db.reopen_terminal_cleanup(str(row["upload_id"])) or current
                )
            await _cleanup_storage_objects(current, mark_cleaned_on_success=True)
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


async def _handle_lost_completion_claim(
    upload_id: str,
    *,
    normalized_sha: str | None,
    assembled_in_this_attempt: bool = False,
    had_prior_claim: bool = False,
) -> dict[str, Any]:
    """Resolve a completion attempt whose claim was not acquired or was superseded."""
    latest = await db.get_upload(upload_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Upload not found")

    latest_state = str(latest["state"])
    if latest_state == "ready" and latest.get("cleaned_at") is None:
        if normalized_sha and not hmac.compare_digest(
            str(latest.get("actual_sha256") or ""), normalized_sha
        ):
            raise HTTPException(status_code=400, detail="File checksum mismatch")
        return _serialize_upload_row(latest)

    if latest_state == "completing":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "completing_in_progress",
                "message": "Upload completion is already in progress",
            },
        )

    if latest_state in ("aborted", "failed"):
        if assembled_in_this_attempt or latest.get("cleaned_at") is None:
            target = latest
            if assembled_in_this_attempt and latest.get("cleaned_at") is not None:
                target = await db.reopen_terminal_cleanup(upload_id) or latest
            await _cleanup_storage_objects(target, mark_cleaned_on_success=True)
        detail = (
            "Upload was cancelled before completion finished"
            if latest_state == "aborted"
            else f"Upload cannot be completed (state={latest_state})"
        )
        raise HTTPException(status_code=409, detail=detail)

    if had_prior_claim and latest_state == "uploading":
        raise HTTPException(
            status_code=503,
            detail="Upload finalization was superseded. Please retry.",
        )

    raise HTTPException(status_code=409, detail="Upload is no longer active")


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
    await _verify_upload_row_write_scope(row, user_id)
    uid = str(row["upload_id"])

    if row["state"] == "ready" and row.get("cleaned_at") is None:
        if normalized_sha and not hmac.compare_digest(
            str(row.get("actual_sha256") or ""), normalized_sha
        ):
            raise HTTPException(status_code=400, detail="File checksum mismatch")
        return _serialize_upload_row(row)

    if row["state"] in ("aborted", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Upload cannot be completed (state={row['state']})",
        )

    if (
        normalized_sha
        and row.get("expected_sha256")
        and not hmac.compare_digest(str(row["expected_sha256"]), normalized_sha)
    ):
        raise HTTPException(status_code=400, detail="File checksum mismatch")

    claimed = await db.claim_complete(
        uid,
        lease=COMPLETE_LEASE,
        expected_sha256=normalized_sha,
    )
    if claimed is None:
        return await _handle_lost_completion_claim(
            uid,
            normalized_sha=normalized_sha,
            had_prior_claim=False,
        )

    claim_token = str(claimed.get("claim_token") or "")
    if (
        normalized_sha
        and claimed.get("expected_sha256")
        and not hmac.compare_digest(str(claimed["expected_sha256"]), normalized_sha)
    ):
        await db.reopen_uploading(uid, claim_token=claim_token)
        raise HTTPException(status_code=400, detail="File checksum mismatch")

    object_key = str(claimed["object_key"])
    store_upload_id = str(claimed["store_upload_id"])
    expected_total = int(claimed["size_bytes"])
    part_count = int(claimed["part_count"])
    assembled_here = False

    try:
        parts = await asyncio.to_thread(
            storage.list_uploaded_parts, object_key, store_upload_id
        )
        if parts is None:
            # A prior attempt may have assembled the object right before losing its lease.
            held_size = await asyncio.to_thread(storage.object_size, object_key)
            if held_size is None:
                reopened = await db.reopen_uploading(uid, claim_token=claim_token)
                if reopened is None:
                    return await _handle_lost_completion_claim(
                        uid,
                        normalized_sha=normalized_sha,
                        assembled_in_this_attempt=assembled_here,
                        had_prior_claim=True,
                    )
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
                reopened = await db.reopen_uploading(uid, claim_token=claim_token)
                if reopened is None:
                    return await _handle_lost_completion_claim(
                        uid,
                        normalized_sha=normalized_sha,
                        assembled_in_this_attempt=assembled_here,
                        had_prior_claim=True,
                    )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "missing_parts",
                        "missing_parts": missing,
                        "message": "Some upload parts are missing",
                    },
                )

            current = await db.get_upload(uid)
            if (
                not current
                or str(current.get("state") or "") != "completing"
                or current.get("claim_token") != claim_token
            ):
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=False,
                    had_prior_claim=True,
                )

            numbered = [(n, str(by_num[n]["etag"])) for n in range(1, part_count + 1)]
            completed = await asyncio.to_thread(
                storage.complete_multipart_upload,
                object_key,
                store_upload_id,
                numbered,
            )
            if completed:
                assembled_here = True
            else:
                held_size = await asyncio.to_thread(storage.object_size, object_key)
                if held_size != expected_total:
                    reopened = await db.reopen_uploading(uid, claim_token=claim_token)
                    if reopened is None:
                        return await _handle_lost_completion_claim(
                            uid,
                            normalized_sha=normalized_sha,
                            assembled_in_this_attempt=assembled_here,
                            had_prior_claim=True,
                        )
                    raise HTTPException(
                        status_code=503,
                        detail="Could not finalize upload. Please retry.",
                    )

            current = await db.get_upload(uid)
            if (
                not current
                or str(current.get("state") or "") != "completing"
                or current.get("claim_token") != claim_token
            ):
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=assembled_here,
                    had_prior_claim=True,
                )

        verified = await asyncio.to_thread(
            storage.compute_stored_object_digest, object_key
        )
        if verified is None:
            reopened = await db.reopen_uploading(uid, claim_token=claim_token)
            if reopened is None:
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=assembled_here,
                    had_prior_claim=True,
                )
            raise HTTPException(
                status_code=503,
                detail="Could not verify uploaded file. Please retry.",
            )

        actual_size, actual_sha256 = verified
        want_sha = claimed.get("expected_sha256")
        if actual_size != expected_total or actual_size > MAX_CHAT_UPLOAD_BYTES:
            failed_row = await db.mark_failed(
                uid,
                claim_token=claim_token,
                error="Assembled file size did not match declared size",
            )
            if failed_row is None:
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=assembled_here,
                    had_prior_claim=True,
                )
            await _cleanup_storage_objects(failed_row)
            raise HTTPException(
                status_code=400,
                detail="Uploaded file size mismatch",
            )
        if want_sha and not hmac.compare_digest(actual_sha256, str(want_sha)):
            failed_row = await db.mark_failed(
                uid,
                claim_token=claim_token,
                error="Assembled file checksum mismatch",
            )
            if failed_row is None:
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=assembled_here,
                    had_prior_claim=True,
                )
            await _cleanup_storage_objects(failed_row)
            raise HTTPException(
                status_code=400,
                detail="Uploaded file checksum mismatch",
            )

        ready_row = await db.mark_ready(
            uid,
            claim_token=claim_token,
            actual_sha256=actual_sha256,
            ready_ttl=READY_UNUSED_TTL,
        )
        if ready_row is None:
            return await _handle_lost_completion_claim(
                uid,
                normalized_sha=normalized_sha,
                assembled_in_this_attempt=assembled_here,
                had_prior_claim=True,
            )
        return _serialize_upload_row(ready_row)
    except HTTPException:
        raise
    except Exception:
        reopened = await db.reopen_uploading(uid, claim_token=claim_token)
        if reopened is None:
            latest = await db.get_upload(uid)
            if latest and str(latest.get("state") or "") != "uploading":
                return await _handle_lost_completion_claim(
                    uid,
                    normalized_sha=normalized_sha,
                    assembled_in_this_attempt=assembled_here,
                    had_prior_claim=True,
                )
        raise


async def _cleanup_storage_objects(
    row: dict[str, Any],
    *,
    claim_token: str | None = None,
    mark_cleaned_on_success: bool = True,
) -> bool:
    uid = str(row.get("upload_id") or "")
    row_state = str(row.get("state") or "")
    if not uid or row_state not in ("aborted", "failed"):
        logger.warning(
            "Refusing storage cleanup for non-terminal chat upload %r (state=%r)",
            uid,
            row_state,
        )
        return False

    object_key = str(row.get("object_key") or "")
    store_upload_id = str(row.get("store_upload_id") or "")
    if not storage.is_chat_upload_key(object_key):
        logger.error("Skipping cleanup of non-chat-upload key %r", object_key)
        return False

    # Re-verify durable terminal state right before touching object storage.
    fresh = await db.get_upload(uid)
    if not fresh or str(fresh.get("state") or "") not in ("aborted", "failed"):
        logger.warning(
            "Skipping storage cleanup for chat upload %s; durable state is %r",
            uid,
            fresh.get("state") if fresh else None,
        )
        return False
    if claim_token is not None and fresh.get("claim_token") != claim_token:
        logger.info(
            "Skipping storage cleanup for chat upload %s; cleanup claim token changed",
            uid,
        )
        return False

    aborted = True
    if store_upload_id:
        aborted = await asyncio.to_thread(
            storage.abort_multipart_upload, object_key, store_upload_id
        )
    deleted = await asyncio.to_thread(storage.delete_object, object_key)
    if not (aborted and deleted):
        return False

    if not mark_cleaned_on_success:
        return True

    if claim_token is not None:
        return await db.mark_cleaned(uid, claim_token=claim_token)

    if fresh.get("cleaned_at") is not None:
        return True
    return await db.mark_cleaned(uid)


async def cancel_chat_upload(*, upload_id: str, user_id: str) -> dict[str, Any]:
    """Idempotently cancel an upload and remove temporary multipart parts/object."""
    _ensure_feature_ready()
    row = await _require_owned_upload(upload_id, user_id)
    uid = str(row["upload_id"])

    if row["state"] == "aborted":
        if row.get("cleaned_at") is None:
            await _cleanup_storage_objects(row, mark_cleaned_on_success=True)
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
                await _cleanup_storage_objects(latest, mark_cleaned_on_success=True)
            return {"upload_id": uid, "state": "aborted"}
        raise HTTPException(
            status_code=409,
            detail="Attachment can no longer be cancelled",
        )

    # When an in-flight completion worker held 'completing', delete visible parts/objects
    # immediately while leaving cleaned_at unset so either the exiting completion worker
    # or the background sweeper cleans any late-assembled object and stamps cleaned_at.
    had_completing = bool(aborted.get("had_completing"))
    await _cleanup_storage_objects(
        aborted, mark_cleaned_on_success=not had_completing
    )
    return {"upload_id": uid, "state": "aborted"}


# ---------------------------------------------------------------------------
# Chat request validation, metadata & sandbox materialization
# ---------------------------------------------------------------------------


_INLINE_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}


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
                        "source_kind": item.get("source_kind"),
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
                    "source_kind": getattr(item, "source_kind", None),
                }
            )
    return extracted


def has_any_attachment_contexts(additional_context: Sequence[Any] | None) -> bool:
    """True when ``additional_context`` carries any inline or ``chat_upload`` attachment."""
    if not additional_context:
        return False
    for item in additional_context:
        item_type = (
            item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        )
        if item_type in ("image", "pdf", "file", "chat_upload"):
            return True
    return False


def _decode_inline_data_url(data_url: str, *, fallback_type: str) -> tuple[bytes, str]:
    """Validate and decode an inline base64 data URL into ``(raw_bytes, mime_type)``."""
    raw_url = str(data_url or "").strip()
    header, sep, b64_payload = raw_url.partition(",")
    if not sep or not header.startswith("data:") or ";base64" not in header:
        raise HTTPException(status_code=400, detail="Malformed attachment data URL")
    mime_part = header[len("data:") :].split(";", 1)[0].strip().lower()
    if not mime_part:
        mime_part = (
            "application/pdf"
            if fallback_type == "pdf"
            else "image/png"
            if fallback_type == "image"
            else "application/octet-stream"
        )
    try:
        decoded = base64.b64decode(b64_payload.strip(), validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="Invalid base64 attachment payload"
        ) from exc
    if len(decoded) <= 0:
        raise HTTPException(status_code=400, detail="Empty attachment is not allowed")
    if len(decoded) > MAX_INLINE_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Inline attachment exceeds the 30 MB inline limit",
        )
    return decoded, mime_part[:120]


class SteerAttachmentTransientError(RuntimeError):
    """Retryable failure while materializing a steered chat upload."""


class SteerAttachmentPermanentError(Exception):
    """Non-retryable failure (revoked access, deleted upload, invalid reference) on a steered chat upload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def chat_upload_claim_key(
    *, request_key: str | None = None, run_id: str | None = None
) -> str:
    """Provisional claim token scoped to a single send invocation."""
    clean_req = str(request_key or "").strip()
    clean_run = str(run_id or "").strip()
    if clean_req and clean_run:
        return f"req:{clean_req}:{clean_run}"
    if clean_req:
        return f"req:{clean_req}"
    if clean_run:
        return f"run:{clean_run}"
    return f"ephemeral:{uuid.uuid4()}"


def _canonical_workspace_id(user_id: str, workspace_id: str | None) -> str:
    raw_ws = str(workspace_id or "").strip()
    if not raw_ws or raw_ws.lower() == "home":
        return get_home_workspace_id(str(user_id).strip())
    return _validate_uuid(raw_ws, field_name="workspace_id")


def _row_to_history_attachment(row: dict[str, Any]) -> dict[str, Any]:
    mime = str(row["mime_type"])
    att_type = (
        "pdf"
        if mime == "application/pdf"
        else "image"
        if mime.startswith("image/")
        else "file"
    )
    return {
        "name": str(row["filename"]),
        "type": att_type,
        "mime_type": mime,
        "size": int(row["size_bytes"]),
        "status": "uploaded",
        "upload_id": str(row["upload_id"]),
    }


async def _validate_attachment_batch(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str,
    workspace_id: str,
    is_existing_colleague_thread: bool = False,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Validate the entire inline + ``chat_upload`` batch before mutating any DB state."""
    if not additional_context:
        return "", [], []

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
            _decode_inline_data_url(str(data_url), fallback_type=str(item_type))
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
        return "", [], []

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
    now_utc = datetime.now(timezone.utc)

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
        exp = _as_utc(row.get("expires_at"))
        if (
            row.get("used_at") is None
            and (exp is None or exp <= now_utc)
            and not _has_live_provisional_claim(row, now_utc)
        ):
            raise HTTPException(
                status_code=410,
                detail="Attachment has expired or was removed",
            )

        size = int(row["size_bytes"])
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
        verified_rows.append(row)

    return canonical_ws, upload_ids, verified_rows


async def _stage_single_inline_attachment(
    *,
    data_url: str,
    description: str | None,
    fallback_type: str,
    user_id: str,
    canonical_ws: str,
) -> dict[str, Any]:
    """Persist one validated inline attachment into ``chat-uploads/`` + ``ready`` ledger row."""
    raw_bytes, mime_type = _decode_inline_data_url(
        data_url, fallback_type=fallback_type
    )
    raw_name = str(description or "").strip() or fallback_type
    if "." not in posixpath.basename(raw_name.replace("\\", "/")):
        raw_name = f"{raw_name}{_INLINE_MIME_EXT.get(mime_type, '')}"
    display_name, safe_name = sanitize_chat_upload_filename(raw_name)

    size_bytes = len(raw_bytes)
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()
    upload_id = str(uuid.uuid4())
    part_size = CHAT_UPLOAD_PART_SIZE
    part_count = max(1, math.ceil(size_bytes / part_size))
    object_key = storage.build_chat_upload_key(canonical_ws, upload_id, safe_name)

    store_upload_id = await asyncio.to_thread(
        storage.open_multipart_upload,
        object_key,
        content_type=mime_type,
    )
    if not store_upload_id:
        raise HTTPException(
            status_code=503,
            detail="Could not stage inline attachment. Please try again.",
        )

    row_created = False
    claim_token: str | None = None
    try:
        row = await db.create_upload_guarded(
            upload_id=upload_id,
            user_id=user_id,
            workspace_id=canonical_ws,
            filename=display_name,
            safe_filename=safe_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            part_size=part_size,
            part_count=part_count,
            object_key=object_key,
            store_upload_id=store_upload_id,
            expected_sha256=expected_sha,
            ttl=UPLOADING_TTL,
            max_active_uploads=MAX_ACTIVE_USER_UPLOADS + MAX_MESSAGE_ATTACHMENTS,
            max_active_bytes=MAX_ACTIVE_USER_UPLOAD_BYTES + MAX_INLINE_TOTAL_BYTES,
        )
        if row is None:
            raise HTTPException(
                status_code=429,
                detail="Too many uploads in progress. Wait for current uploads to finish or remove one.",
            )
        row_created = True

        ordered_parts: list[tuple[int, str]] = []
        for idx in range(part_count):
            part_num = idx + 1
            chunk = raw_bytes[idx * part_size : (idx + 1) * part_size]
            part_sha = hashlib.sha256(chunk).hexdigest()
            etag = await asyncio.to_thread(
                storage.upload_part_bytes,
                object_key,
                store_upload_id,
                part_number=part_num,
                data=chunk,
            )
            if not etag:
                raise HTTPException(
                    status_code=503,
                    detail="Failed to stage inline attachment part. Please retry.",
                )
            updated = await db.record_uploaded_part(
                upload_id,
                part_number=part_num,
                etag=etag,
                size_bytes=len(chunk),
                sha256=part_sha,
            )
            if updated is None:
                raise HTTPException(
                    status_code=503,
                    detail="Failed to record staged inline attachment part.",
                )
            ordered_parts.append((part_num, etag))

        claimed = await db.claim_complete(
            upload_id,
            lease=COMPLETE_LEASE,
            expected_sha256=expected_sha,
        )
        if claimed is None:
            raise HTTPException(
                status_code=503,
                detail="Failed to finalize staged inline attachment.",
            )
        claim_token = str(claimed.get("claim_token") or "")

        completed = await asyncio.to_thread(
            storage.complete_multipart_upload,
            object_key,
            store_upload_id,
            ordered_parts,
        )
        if not completed:
            raise HTTPException(
                status_code=503,
                detail="Failed to assemble staged inline attachment.",
            )

        digest = await asyncio.to_thread(
            storage.compute_stored_object_digest,
            object_key,
        )
        if digest is None:
            raise HTTPException(
                status_code=503,
                detail="Failed to verify staged inline attachment.",
            )
        actual_size, actual_sha = digest
        if actual_size != size_bytes or not hmac.compare_digest(
            actual_sha, expected_sha
        ):
            raise HTTPException(
                status_code=503,
                detail="Staged inline attachment verification mismatch.",
            )

        ready_row = await db.mark_ready(
            upload_id,
            actual_sha256=actual_sha,
            ready_ttl=READY_UNUSED_TTL,
            claim_token=claim_token,
        )
        if ready_row is None:
            raise HTTPException(
                status_code=503,
                detail="Failed to mark staged inline attachment ready.",
            )
        return ready_row
    except BaseException:
        try:
            if row_created:
                if claim_token:
                    failed_row = await db.mark_failed(
                        upload_id,
                        claim_token=claim_token,
                        error="Inline attachment staging failed",
                    )
                    if failed_row is not None:
                        await _cleanup_storage_objects(
                            failed_row, mark_cleaned_on_success=True
                        )
                else:
                    aborted = await db.abort_upload(upload_id, user_id=user_id)
                    if aborted is not None:
                        await _cleanup_storage_objects(
                            aborted, mark_cleaned_on_success=True
                        )
            else:
                await asyncio.to_thread(
                    storage.abort_multipart_upload, object_key, store_upload_id
                )
                await asyncio.to_thread(storage.delete_object, object_key)
        except Exception:
            logger.warning(
                "Cleanup after failed inline steer staging failed for %s",
                upload_id,
                exc_info=True,
            )
        raise


async def cleanup_unaccepted_staged_uploads(
    staged_upload_ids: Sequence[str] | None,
    *,
    user_id: str,
    claim_key: str | None = None,
) -> None:
    """Release claims and immediately delete server-staged inline blobs when steer admission does not accept."""
    unique_ids = [str(uid).strip() for uid in dict.fromkeys(staged_upload_ids or ()) if uid]
    if not unique_ids or not user_id:
        return
    clean_key = str(claim_key or "").strip()
    if clean_key:
        try:
            await db.release_provisional_claims(
                user_id=user_id,
                claim_key=clean_key,
                upload_ids=unique_ids,
            )
        except Exception:
            logger.warning(
                "Releasing provisional claim %s on unaccepted staged uploads failed",
                clean_key,
                exc_info=True,
            )
    for uid in unique_ids:
        try:
            await cancel_chat_upload(upload_id=uid, user_id=user_id)
        except Exception:
            logger.warning(
                "Cleaning unaccepted staged inline upload %s failed",
                uid,
                exc_info=True,
            )


async def stage_request_attachments_for_steer(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str,
    workspace_id: str,
    is_existing_colleague_thread: bool = False,
    claim_key: str | None = None,
    ttl: timedelta = PROVISIONAL_CLAIM_TTL,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate and stage all ordered steer attachments as object-backed ``chat_upload`` descriptors.

    Returns ``(ordered_chat_upload_contexts, newly_staged_inline_upload_ids)``.
    Any pre-acceptance failure aborts and deletes all newly staged inline objects while
    leaving caller-owned ``chat_upload`` rows intact.
    """
    if not has_any_attachment_contexts(additional_context):
        return [], []

    _ensure_feature_ready()
    canonical_ws = await resolve_and_verify_upload_workspace(
        user_id,
        workspace_id,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )
    _, _, verified_existing_rows = await _validate_attachment_batch(
        additional_context,
        user_id=user_id,
        workspace_id=canonical_ws,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )
    existing_by_id = {str(r["upload_id"]): r for r in verified_existing_rows}

    eff_key = str(claim_key or "").strip() or chat_upload_claim_key()
    ordered_descriptors: list[dict[str, Any]] = []
    staged_inline_ids: list[str] = []

    try:
        for item in additional_context or ():
            item_type = (
                item.get("type")
                if isinstance(item, dict)
                else getattr(item, "type", None)
            )
            if item_type == "chat_upload":
                raw_uid = (
                    item.get("upload_id")
                    if isinstance(item, dict)
                    else getattr(item, "upload_id", "")
                )
                uid = _validate_uuid(str(raw_uid or ""), field_name="upload_id")
                row = existing_by_id[uid]
                src_kind = (
                    item.get("source_kind")
                    if isinstance(item, dict)
                    else getattr(item, "source_kind", None)
                ) or "chat_upload"
                ordered_descriptors.append(
                    {
                        "type": "chat_upload",
                        "upload_id": uid,
                        "name": str(row["filename"]),
                        "mime_type": str(row["mime_type"]),
                        "size": int(row["size_bytes"]),
                        "source_kind": src_kind,
                    }
                )
            elif item_type in ("image", "pdf", "file"):
                data_url = (
                    item.get("data", "")
                    if isinstance(item, dict)
                    else getattr(item, "data", "")
                ) or ""
                desc = (
                    item.get("description")
                    if isinstance(item, dict)
                    else getattr(item, "description", None)
                )
                staged_row = await _stage_single_inline_attachment(
                    data_url=str(data_url),
                    description=desc,
                    fallback_type=str(item_type),
                    user_id=user_id,
                    canonical_ws=canonical_ws,
                )
                staged_uid = str(staged_row["upload_id"])
                staged_inline_ids.append(staged_uid)
                ordered_descriptors.append(
                    {
                        "type": "chat_upload",
                        "upload_id": staged_uid,
                        "name": str(staged_row["filename"]),
                        "mime_type": str(staged_row["mime_type"]),
                        "size": int(staged_row["size_bytes"]),
                        "source_kind": str(item_type),
                    }
                )

        all_ids = [d["upload_id"] for d in ordered_descriptors]
        if all_ids:
            claimed_by_id = await db.claim_provisional_batch(
                all_ids,
                user_id=user_id,
                workspace_id=canonical_ws,
                claim_key=eff_key,
                ttl=ttl,
            )
            if claimed_by_id is None:
                raise HTTPException(
                    status_code=410,
                    detail="Attachment has expired or was removed",
                )
    except BaseException:
        if staged_inline_ids:
            await asyncio.shield(
                cleanup_unaccepted_staged_uploads(
                    staged_inline_ids,
                    user_id=user_id,
                    claim_key=eff_key,
                )
            )
        raise

    return ordered_descriptors, staged_inline_ids


async def validate_and_claim_request_attachments(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str,
    workspace_id: str,
    is_existing_colleague_thread: bool = False,
    claim_key: str | None = None,
    ttl: timedelta = PROVISIONAL_CLAIM_TTL,
) -> list[dict[str, Any]]:
    """Validate the full attachment batch first, then acquire a bounded provisional claim.

    Permanent retention (``used_at``, ``expires_at = NULL``) is deferred until
    durable run START or steer acceptance via :func:`confirm_request_chat_uploads`.
    """
    canonical_ws, upload_ids, _ = await _validate_attachment_batch(
        additional_context,
        user_id=user_id,
        workspace_id=workspace_id,
        is_existing_colleague_thread=is_existing_colleague_thread,
    )
    if not upload_ids:
        return []

    eff_key = str(claim_key or "").strip() or chat_upload_claim_key()
    claimed_by_id = await db.claim_provisional_batch(
        upload_ids,
        user_id=user_id,
        workspace_id=canonical_ws,
        claim_key=eff_key,
        ttl=ttl,
    )
    if claimed_by_id is None:
        raise HTTPException(
            status_code=410,
            detail="Attachment has expired or was removed",
        )
    return [claimed_by_id[uid] for uid in upload_ids]


async def confirm_request_chat_uploads(
    additional_context: Sequence[Any] | None,
    *,
    user_id: str,
    workspace_id: str,
    claim_key: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Finalize permanent retention inside the durable START or steer admission transaction."""
    upload_items = extract_chat_upload_contexts(additional_context)
    if not upload_items:
        return []
    target_ws = _canonical_workspace_id(user_id, workspace_id)
    upload_ids = [
        _validate_uuid(u["upload_id"], field_name="upload_id") for u in upload_items
    ]
    confirmed = await db.confirm_provisional_claims(
        upload_ids,
        user_id=user_id,
        workspace_id=target_ws,
        claim_key=claim_key,
        conn=conn,
    )
    if confirmed is None:
        raise HTTPException(
            status_code=410,
            detail="Attachment has expired or was removed",
        )
    return [confirmed[uid] for uid in upload_ids]


async def release_request_chat_uploads(
    *,
    user_id: str,
    claim_key: str,
    request_key: str | None = None,
    additional_context: Sequence[Any] | None = None,
) -> int:
    """Release a provisional claim on rejection without altering durable retention."""
    upload_ids: list[str] | None = None
    if additional_context is not None:
        upload_items = extract_chat_upload_contexts(additional_context)
        if not upload_items:
            return 0
        parsed_ids: list[str] = []
        for item in upload_items:
            try:
                parsed_ids.append(_validate_uuid(item["upload_id"]))
            except HTTPException:
                continue
        if not parsed_ids:
            return 0
        upload_ids = parsed_ids
    return await db.release_provisional_claims(
        user_id=user_id,
        claim_key=claim_key,
        request_key=request_key,
        upload_ids=upload_ids,
    )


class ChatUploadSendScope:
    """Request-scoped provisional claim guard across chat turn/steer admission."""

    def __init__(
        self,
        *,
        additional_context: Sequence[Any] | None,
        user_id: str,
        workspace_id: str,
        request_key: str | None = None,
        run_id: str | None = None,
        claim_key: str | None = None,
        is_existing_colleague_thread: bool = False,
        preclaimed: bool = False,
    ) -> None:
        self.additional_context = additional_context
        self.user_id = str(user_id or "")
        self.workspace_id = str(workspace_id or "")
        self.request_key = request_key
        self.claim_key = str(claim_key or "").strip() or chat_upload_claim_key(
            request_key=request_key, run_id=run_id
        )
        self.is_existing_colleague_thread = is_existing_colleague_thread
        self._has_uploads = bool(extract_chat_upload_contexts(additional_context))
        self._claimed = bool(preclaimed and self._has_uploads)
        self._admitted = False

    async def __aenter__(self) -> "ChatUploadSendScope":
        if not self._claimed and self.additional_context:
            await validate_and_claim_request_attachments(
                self.additional_context,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                is_existing_colleague_thread=self.is_existing_colleague_thread,
                claim_key=self.claim_key,
            )
            self._claimed = self._has_uploads
        return self

    def mark_admitted(self) -> None:
        self._admitted = True

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._has_uploads and not self._admitted and self.user_id:
            try:
                await asyncio.shield(
                    release_request_chat_uploads(
                        user_id=self.user_id,
                        claim_key=self.claim_key,
                        request_key=self.request_key,
                        additional_context=self.additional_context,
                    )
                )
            except Exception:
                logger.warning(
                    "Releasing provisional chat upload claims failed for key %s",
                    self.claim_key,
                    exc_info=True,
                )
        return False


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

    canonical_ws: str | None = None
    if workspace_id is not None and user_id is not None:
        try:
            canonical_ws = _canonical_workspace_id(user_id, workspace_id)
        except HTTPException:
            canonical_ws = workspace_id
    else:
        canonical_ws = workspace_id

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
            and (
                canonical_ws is None
                or str(row["workspace_id"]) == str(canonical_ws)
            )
        ):
            out.append(_row_to_history_attachment(row))
        else:
            mime_hint = str(item.get("mime_type") or "").strip()
            src_kind = str(item.get("source_kind") or "").strip()
            att_type = (
                src_kind
                if src_kind in ("image", "pdf")
                else "pdf"
                if mime_hint == "application/pdf"
                else "image"
                if mime_hint.startswith("image/")
                else "file"
            )
            entry: dict[str, Any] = {
                "name": str(item.get("name") or "file")[:200],
                "type": att_type,
                "size": int(item.get("size") or 0),
                "status": "uploaded",
                "upload_id": raw_id,
            }
            if mime_hint:
                entry["mime_type"] = mime_hint
            out.append(entry)
    return out


async def build_ordered_turn_attachment_metadata(
    additional_context: Sequence[Any] | None,
    *,
    inline_metadata: Sequence[dict[str, Any]] | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Combine inline and ``chat_upload`` metadata in exact ``additional_context`` order."""
    if not additional_context:
        return list(inline_metadata or [])
    upload_meta = await build_chat_upload_history_metadata(
        additional_context,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    inline_iter = iter(inline_metadata or ())
    upload_iter = iter(upload_meta)
    combined: list[dict[str, Any]] = []
    for item in additional_context:
        item_type = (
            item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        )
        if item_type in ("image", "pdf", "file"):
            nxt = next(inline_iter, None)
            if nxt is not None:
                combined.append(nxt)
        elif item_type == "chat_upload":
            nxt = next(upload_iter, None)
            if nxt is not None:
                combined.append(nxt)
    combined.extend(inline_iter)
    combined.extend(upload_iter)
    return combined


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


async def _probe_sandbox_file_matches(
    sandbox: Any,
    abs_target_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    """Return True iff ``abs_target_path`` is a regular file with exact ``expected_size`` and SHA-256."""
    clean_sha = str(expected_sha256 or "").strip().lower()
    if not clean_sha or int(expected_size) <= 0:
        return False
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
        f"{shlex.quote(abs_target_path)} {int(expected_size)} {shlex.quote(clean_sha)}"
    )
    probe_timeout = max(20, transfer_timeout_s(int(expected_size)))
    try:
        probe = await sandbox._runtime_call(
            sandbox.runtime.exec,
            verify_existing_cmd,
            timeout=probe_timeout,
            total_timeout=max(120.0, float(probe_timeout) + 30.0),
            retry_policy=RetryPolicy.SAFE,
        )
    except Exception:
        return False
    return getattr(probe, "exit_code", 1) == 0


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
    if await _probe_sandbox_file_matches(
        sandbox,
        abs_target_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    ):
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

    _, _, verified_rows = await _validate_attachment_batch(
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
    can_direct = bool(
        getattr(getattr(sandbox, "provider", None), "direct_blob_transfer", True)
    )

    ordered_targets: list[tuple[dict[str, Any], str, str, int]] = []
    unique_by_abs: dict[str, tuple[dict[str, Any], str, str, int]] = {}
    for row in verified_rows:
        rel_path = build_sandbox_upload_rel_path(row)
        if not sandbox.validate_path(rel_path, eff_project):
            raise PermissionError(f"Invalid sandbox upload target: {rel_path!r}")
        abs_path = sandbox.normalize_path(rel_path, project=eff_project)
        size_bytes = int(row["size_bytes"])
        entry = (row, rel_path, abs_path, size_bytes)
        ordered_targets.append(entry)
        unique_by_abs.setdefault(abs_path, entry)

    async with AsyncExitStack() as lock_stack:
        for abs_path in sorted(unique_by_abs):
            await lock_stack.enter_async_context(sandbox.path_write_lock(abs_path))

        # Re-verify workspace write access right before probing/placing on disk.
        await resolve_and_verify_upload_workspace(
            user_id,
            workspace_id,
            is_existing_colleague_thread=is_existing_colleague_thread,
        )

        missing_or_mismatched: list[tuple[dict[str, Any], str, str, int]] = []
        for abs_path in sorted(unique_by_abs):
            t_row, t_rel, t_abs, t_size = unique_by_abs[abs_path]
            exp_sha = str(
                t_row.get("actual_sha256") or t_row.get("expected_sha256") or ""
            )
            if not await _probe_sandbox_file_matches(
                sandbox,
                t_abs,
                expected_size=t_size,
                expected_sha256=exp_sha,
            ):
                missing_or_mismatched.append((t_row, t_rel, t_abs, t_size))

        if missing_or_mismatched:
            total_needed = sum(t_size for _, _, _, t_size in missing_or_mismatched)
            free_bytes = await _free_disk_bytes(sandbox, layout.workspace)
            if (
                free_bytes is not None
                and free_bytes - CRITICAL_FREE_BYTES < total_needed
            ):
                raise RuntimeError(
                    f"Not enough free disk space in workspace ({max(free_bytes, 0) // _MIB} MB free)"
                )

            for t_row, t_rel, t_abs, _ in missing_or_mismatched:
                await resolve_and_verify_upload_workspace(
                    user_id,
                    workspace_id,
                    is_existing_colleague_thread=is_existing_colleague_thread,
                )
                await _place_row_in_sandbox(
                    sandbox,
                    t_row,
                    layout=layout,
                    abs_path=t_abs,
                    rel_path=t_rel,
                    can_direct=can_direct,
                )

    for row, rel_path, _, size_bytes in ordered_targets:
        mime = str(row.get("mime_type") or "")
        hint = (
            "Read it directly or use Python to inspect it."
            if mime == "application/pdf" or mime.startswith("image/")
            else "Use Python to process it."
        )
        notes.append(
            f"The user attached a file ({row['filename']}, {mime}, "
            f"{_format_human_bytes(size_bytes)}). It has been saved to {rel_path}. "
            f"{hint}"
        )

    return notes


async def verify_steered_chat_uploads(
    payload: dict[str, Any],
) -> tuple[str, str, bool, list[dict[str, Any]]]:
    """Verify workspace write access and attachment state for a steered payload.

    Returns ``(normalized_workspace_id, workspace_owner_id, is_colleague, verified_rows)``.
    Raises :class:`SteerAttachmentPermanentError` on permanent failures and
    :class:`SteerAttachmentTransientError` on transient infrastructure errors.
    """
    chat_uploads = payload.get("chat_uploads")
    if not chat_uploads:
        return "", "", False, []
    user_id = str(payload.get("user_id") or "").strip()
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not user_id or not workspace_id:
        raise SteerAttachmentPermanentError(
            "invalid_steer_payload",
            "Steered attachment is missing user or workspace identity",
        )

    from src.dalton.sharing import ColleagueAccessRevokedError
    from src.server.database.workspace import get_workspace

    try:
        normalized_ws = _validate_uuid(workspace_id, field_name="workspace_id")
    except HTTPException as exc:
        raise SteerAttachmentPermanentError(
            "invalid_workspace",
            str(exc.detail),
        ) from exc

    try:
        ws_row = await get_workspace(normalized_ws)
    except Exception as exc:
        raise SteerAttachmentTransientError(str(exc)) from exc
    if ws_row is None and not is_home_id(user_id, normalized_ws):
        raise SteerAttachmentPermanentError(
            "workspace_not_found",
            "Target workspace no longer exists",
        )
    if ws_row is not None and str(ws_row.get("status") or "") == "deleted":
        raise SteerAttachmentPermanentError(
            "workspace_deleted",
            "Target workspace has been deleted",
        )

    ws_owner = (
        str(ws_row["user_id"])
        if ws_row and ws_row.get("user_id")
        else user_id
    )
    is_colleague = ws_owner != user_id

    try:
        _, _, verified_rows = await _validate_attachment_batch(
            chat_uploads,
            user_id=user_id,
            workspace_id=normalized_ws,
            is_existing_colleague_thread=is_colleague,
        )
    except ColleagueAccessRevokedError as exc:
        raise SteerAttachmentPermanentError(exc.code, exc.message) from exc
    except PermissionError as exc:
        raise SteerAttachmentPermanentError("forbidden", str(exc)) from exc
    except HTTPException as exc:
        if exc.status_code in (400, 401, 403, 404, 410, 413):
            detail = exc.detail
            msg = (
                str(detail.get("message") or detail)
                if isinstance(detail, dict)
                else str(detail)
            )
            raise SteerAttachmentPermanentError(
                f"http_{exc.status_code}",
                msg,
            ) from exc
        raise SteerAttachmentTransientError(str(exc.detail)) from exc
    except Exception as exc:
        raise SteerAttachmentTransientError(str(exc)) from exc

    return normalized_ws, ws_owner, is_colleague, verified_rows


async def materialize_steered_chat_uploads(
    payload: dict[str, Any],
    *,
    project: ProjectContext | None = None,
    session: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Materialize ``chat_uploads`` on a held steer job into the verified workspace folder.

    Returns ``(model_file_reminder, verified_attachment_metadata)``.
    Raises :class:`SteerAttachmentPermanentError` on authorization/reference failures
    and :class:`SteerAttachmentTransientError` on temporary sandbox/storage errors.
    """
    chat_uploads = payload.get("chat_uploads")
    if not chat_uploads:
        return "", []
    user_id = str(payload.get("user_id") or "").strip()

    from src.dalton.sharing import ColleagueAccessRevokedError
    from src.server.services.workspace_layout import (
        WorkspaceFolderMoving,
        WorkspaceLayoutUnavailable,
        resolve_project_context,
    )
    from src.server.services.workspace_manager import WorkspaceManager
    from src.server.utils.multimodal_context import build_file_reminder

    normalized_ws, ws_owner, is_colleague, verified_rows = (
        await verify_steered_chat_uploads(payload)
    )

    if session is None:
        try:
            wm = WorkspaceManager.get_instance()
            session = await wm.get_session_for_workspace(normalized_ws, user_id=ws_owner)
        except Exception as exc:
            raise SteerAttachmentTransientError(str(exc)) from exc

    sandbox = getattr(session, "sandbox", None) if session is not None else None
    if sandbox is None:
        raise SteerAttachmentTransientError(
            "Workspace sandbox is unavailable for steered upload"
        )

    eff_project = project if project is not None else current_project()
    try:
        if (
            eff_project is None
            or str(eff_project.workspace_id or "") != normalized_ws
            or not str(eff_project.dir_name or "").strip()
        ):
            eff_project = await resolve_project_context(session, normalized_ws)
    except (WorkspaceLayoutUnavailable, WorkspaceFolderMoving) as exc:
        raise SteerAttachmentTransientError(str(exc)) from exc
    except Exception as exc:
        raise SteerAttachmentTransientError(str(exc)) from exc

    if (
        eff_project is None
        or str(eff_project.workspace_id or "") != normalized_ws
        or not str(eff_project.dir_name or "").strip()
    ):
        raise SteerAttachmentTransientError(
            f"Verified workspace folder is unavailable for workspace {normalized_ws}"
        )

    try:
        notes = await materialize_chat_uploads(
            chat_uploads,
            sandbox=sandbox,
            user_id=user_id,
            workspace_id=normalized_ws,
            project=eff_project,
            is_existing_colleague_thread=is_colleague,
        )
    except ColleagueAccessRevokedError as exc:
        raise SteerAttachmentPermanentError(exc.code, exc.message) from exc
    except PermissionError as exc:
        raise SteerAttachmentPermanentError("forbidden", str(exc)) from exc
    except HTTPException as exc:
        if exc.status_code in (400, 401, 403, 404, 410, 413):
            raise SteerAttachmentPermanentError(
                f"http_{exc.status_code}",
                str(exc.detail),
            ) from exc
        raise SteerAttachmentTransientError(str(exc.detail)) from exc
    except Exception as exc:
        raise SteerAttachmentTransientError(str(exc)) from exc

    verified_meta = [_row_to_history_attachment(row) for row in verified_rows]
    return (build_file_reminder(notes) if notes else ""), verified_meta


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
    """Verify retained-row durability, workspace binding, Home single-owner isolation, and key prefix."""
    del conn
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

    # Cross-user upload in Home is never allowed.
    if is_home_row(ws_row) or is_home_id(ws_owner_id, workspace_id):
        return row_user_id == ws_owner_id

    # Accepted files in a live non-Home workspace belong to that workspace's retained content
    # even if a former collaborator's sharing permission was later removed.
    return ws_row is not None


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
    for row, rel_path, abs_path in sorted(missing_entries, key=lambda e: e[2]):
        try:
            async with sandbox.path_write_lock(abs_path):
                if await _probe_sandbox_file_matches(
                    sandbox,
                    abs_path,
                    expected_size=int(row["size_bytes"]),
                    expected_sha256=str(row.get("actual_sha256") or ""),
                ):
                    continue
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
            had_completing = bool(row.get("had_completing"))
            if await _cleanup_storage_objects(
                row, mark_cleaned_on_success=not had_completing
            ):
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
            had_completing = bool(row.get("had_completing"))
            if await _cleanup_storage_objects(
                row, mark_cleaned_on_success=not had_completing
            ):
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
        row_claim_token = str(row.get("claim_token") or "")
        try:
            ok = await _cleanup_storage_objects(
                row,
                claim_token=row_claim_token or None,
                mark_cleaned_on_success=True,
            )
            if ok:
                cleaned_count += 1
            elif row_claim_token:
                await db.release_cleanup_claim(uid, claim_token=row_claim_token)
        except Exception as exc:
            logger.warning("Cleanup failed for chat upload %s: %s", uid, exc)
            if row_claim_token:
                await db.release_cleanup_claim(uid, claim_token=row_claim_token)

    await db.prune_cleaned_rows(now=now)
    return cleaned_count


async def run_chat_upload_cleanup_loop(
    *, interval_s: float = CLEANUP_INTERVAL_S
) -> None:
    """Periodic lifespan cleanup loop for ``dalton_chat_file_uploads``."""
    if not is_chat_upload_cleanup_enabled():
        return
    while True:
        await asyncio.sleep(interval_s)
        try:
            await sweep_expired_chat_uploads_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Chat upload cleanup sweep error: %s", exc)
