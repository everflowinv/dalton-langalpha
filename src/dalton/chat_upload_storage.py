"""MinIO storage helpers for chat composer large-file uploads.

Reuses the dedicated ``DALTON_ASR_STORAGE_*`` S3 clients from
``src.dalton.transcription_storage`` without enabling the global ``STORAGE_*``
provider. Objects live exclusively under ``chat-uploads/`` so transcription
audio and upstream ``uploads/`` lifecycle rules remain untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import logging
from typing import Any

from src.dalton import transcription_storage as _store

logger = logging.getLogger(__name__)

CHAT_UPLOAD_KEY_PREFIX = "chat-uploads"
_VERIFY_CHUNK_BYTES = 1024 * 1024
_STREAM_CHUNK_BYTES = 4 * 1024 * 1024


def is_chat_upload_storage_configured() -> bool:
    """True when ``DALTON_ASR_STORAGE_*`` credentials, bucket, and endpoint are configured."""
    return _store.is_transcription_storage_configured()


def build_chat_upload_key(workspace_id: str, upload_id: str, safe_filename: str) -> str:
    """Build an isolated object key under ``chat-uploads/<workspace_id>/<upload_id>/<safe_filename>``."""
    ws = str(workspace_id or "").strip().strip("/")
    uid = str(upload_id or "").strip().strip("/")
    name = str(safe_filename or "attachment").strip().strip("/") or "attachment"
    if not ws or not uid or "/" in ws or "/" in uid or "/" in name:
        raise ValueError("Invalid chat upload key component")
    return f"{CHAT_UPLOAD_KEY_PREFIX}/{ws}/{uid}/{name}"


def is_chat_upload_key(object_key: str) -> bool:
    """Guard ensuring cleanup never touches keys outside ``chat-uploads/``."""
    cleaned = str(object_key or "").strip().lstrip("/")
    return cleaned.startswith(f"{CHAT_UPLOAD_KEY_PREFIX}/")


def is_chat_upload_key_for_workspace(object_key: str, workspace_id: str) -> bool:
    """Guard ensuring recovery/cleanup keys belong to the expected workspace prefix."""
    ws = str(workspace_id or "").strip().strip("/")
    if not ws or "/" in ws:
        return False
    cleaned = str(object_key or "").strip().lstrip("/")
    return cleaned.startswith(f"{CHAT_UPLOAD_KEY_PREFIX}/{ws}/")


def open_multipart_upload(object_key: str, *, content_type: str) -> str | None:
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    return _store.open_multipart_upload(object_key, content_type=content_type)


def upload_part_bytes(
    object_key: str,
    store_upload_id: str,
    *,
    part_number: int,
    data: bytes,
) -> str | None:
    """Upload one bounded multipart chunk and return its ``ETag``."""
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    try:
        resp = _store._get_client().upload_part(
            Bucket=_store._bucket_name(),
            Key=_store.resolve_full_key(object_key),
            UploadId=store_upload_id,
            PartNumber=int(part_number),
            ContentLength=len(data),
            Body=data,
        )
        etag = resp.get("ETag")
        return str(etag) if etag else None
    except Exception as exc:
        logger.error(
            "Failed to upload part %s for chat upload %s: %s",
            part_number,
            object_key,
            exc,
        )
        return None


def list_uploaded_parts(
    object_key: str, store_upload_id: str
) -> list[dict[str, Any]] | None:
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    return _store.list_uploaded_parts(object_key, store_upload_id)


def complete_multipart_upload(
    object_key: str, store_upload_id: str, parts: list[tuple[int, str]]
) -> bool:
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    return _store.complete_multipart_upload(object_key, store_upload_id, parts)


def abort_multipart_upload(object_key: str, store_upload_id: str) -> bool:
    if not is_chat_upload_key(object_key):
        logger.error("Refusing to abort non-chat-upload key %r", object_key)
        return False
    return _store.abort_multipart_upload(object_key, store_upload_id)


def delete_object(object_key: str) -> bool:
    if not is_chat_upload_key(object_key):
        logger.error("Refusing to delete non-chat-upload key %r", object_key)
        return False
    return _store.delete_object(object_key)


def object_size(object_key: str) -> int | None:
    if not is_chat_upload_key(object_key):
        return None
    return _store.object_size(object_key)


def sign_download_url(object_key: str, *, expires_in: int = 900) -> str | None:
    """Presign a GET URL against ``signing_endpoint_url()`` for sandbox ``pull_direct``."""
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    try:
        return _store._get_signing_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": _store._bucket_name(),
                "Key": _store.resolve_full_key(object_key),
            },
            ExpiresIn=max(60, int(expires_in)),
        )
    except Exception as exc:
        logger.error("Failed to sign download URL for %s: %s", object_key, exc)
        return None


def compute_stored_object_digest(object_key: str) -> tuple[int, str] | None:
    """Stream the assembled object in 1 MiB chunks to compute ``(size_bytes, sha256_hex)``."""
    if not is_chat_upload_key(object_key):
        return None
    body = None
    try:
        resp = _store._get_client().get_object(
            Bucket=_store._bucket_name(),
            Key=_store.resolve_full_key(object_key),
        )
        body = resp.get("Body")
        if body is None:
            return None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = body.read(_VERIFY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        return total, digest.hexdigest()
    except Exception as exc:
        logger.error("Failed to verify stored object %s: %s", object_key, exc)
        return None
    finally:
        if body is not None:
            try:
                body.close()
            except Exception:
                pass


def iter_stored_object_chunks(
    object_key: str, *, chunk_size: int = _STREAM_CHUNK_BYTES
) -> Iterator[bytes]:
    """Yield bounded chunks of ``object_key`` for non-direct sandbox fallback."""
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    step = max(65536, int(chunk_size))
    resp = _store._get_client().get_object(
        Bucket=_store._bucket_name(),
        Key=_store.resolve_full_key(object_key),
    )
    body = resp.get("Body")
    if body is None:
        return
    try:
        while True:
            chunk = body.read(step)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            body.close()
        except Exception:
            pass
