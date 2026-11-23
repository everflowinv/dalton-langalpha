"""MinIO / S3 storage helpers for chat composer large-file uploads.

Reads dedicated ``DALTON_CHAT_UPLOAD_STORAGE_*`` environment variables first,
falling back to ``DALTON_ASR_STORAGE_*`` via ``src.dalton.transcription_storage``
when the dedicated variables are unset (preserving existing Everflow behavior
without enabling the global ``STORAGE_*`` provider). Objects live exclusively
under ``chat-uploads/`` so transcription audio and upstream ``uploads/``
lifecycle rules remain untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import logging
import os
import threading
from typing import Any

from src.dalton import transcription_storage as _store

logger = logging.getLogger(__name__)

CHAT_UPLOAD_KEY_PREFIX = "chat-uploads"
_VERIFY_CHUNK_BYTES = 1024 * 1024
_STREAM_CHUNK_BYTES = 4 * 1024 * 1024

_DEDICATED_PRIMARY_ENV_VARS = (
    "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
    "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
    "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
)

_CLIENT_LOCK = threading.Lock()
_CLIENT_KEY: tuple[str, str, str, str, str] | None = None
_CLIENT: Any | None = None
_SIGNING_CLIENT_KEY: tuple[str, str, str, str, str] | None = None
_SIGNING_CLIENT: Any | None = None
_PARTIAL_ENV_WARNED_MISSING: tuple[str, ...] | None = None


def _has_dedicated_env() -> bool:
    """True only when all four primary ``DALTON_CHAT_UPLOAD_STORAGE_*`` env vars are set.

    When only a subset is configured, logs a warning listing the missing variable
    names (never their values) and falls back as a whole to ``DALTON_ASR_STORAGE_*``.
    """
    global _PARTIAL_ENV_WARNED_MISSING
    present = [
        name for name in _DEDICATED_PRIMARY_ENV_VARS if (os.getenv(name) or "").strip()
    ]
    if len(present) == len(_DEDICATED_PRIMARY_ENV_VARS):
        _PARTIAL_ENV_WARNED_MISSING = None
        return True
    if present:
        missing = tuple(
            name for name in _DEDICATED_PRIMARY_ENV_VARS if name not in present
        )
        if _PARTIAL_ENV_WARNED_MISSING != missing:
            _PARTIAL_ENV_WARNED_MISSING = missing
            logger.warning(
                "Incomplete DALTON_CHAT_UPLOAD_STORAGE_* configuration "
                "(missing: %s); falling back to DALTON_ASR_STORAGE_*",
                ", ".join(missing),
            )
        return False
    _PARTIAL_ENV_WARNED_MISSING = None
    return False


def _endpoint_url() -> str:
    if _has_dedicated_env():
        return (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL") or "").strip().rstrip("/")
    return _store._endpoint_url()


def signing_endpoint_url() -> str:
    """Endpoint URL used when presigning direct download URLs (Everflow profile)."""
    if _has_dedicated_env():
        explicit = (
            os.getenv("DALTON_CHAT_UPLOAD_STORAGE_SIGNING_ENDPOINT_URL") or ""
        ).strip().rstrip("/")
        if explicit:
            return explicit
        return _endpoint_url()
    return _store.signing_endpoint_url()


def _bucket_name() -> str:
    if _has_dedicated_env():
        return (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "").strip()
    return _store._bucket_name()


def _access_key_id() -> str:
    if _has_dedicated_env():
        return (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID") or "").strip()
    return _store._access_key_id()


def _secret_access_key() -> str:
    if _has_dedicated_env():
        return (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY") or "").strip()
    return _store._secret_access_key()


def _region() -> str:
    if _has_dedicated_env():
        explicit = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_REGION") or "").strip()
        if explicit:
            return explicit
    return _store._region()


def _addressing_style() -> str:
    if _has_dedicated_env():
        explicit = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE") or "").strip().lower()
        if explicit:
            return explicit
    return _store._addressing_style()


def _key_prefix() -> str:
    if _has_dedicated_env():
        if "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX" in os.environ:
            raw = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or "").strip().strip("/")
            return f"{raw}/" if raw else ""
        return ""
    return _store._key_prefix()


def resolve_full_key(object_key: str) -> str:
    """Resolve the full object key inside the active bucket, applying any configured prefix."""
    if not _has_dedicated_env():
        return _store.resolve_full_key(object_key)
    cleaned = str(object_key or "").strip().lstrip("/")
    prefix = _key_prefix()
    if prefix and not cleaned.startswith(prefix):
        return f"{prefix}{cleaned}"
    return cleaned


def _build_client(
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str,
    addressing_style: str,
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _get_client() -> Any:
    if not _has_dedicated_env():
        return _store._get_client()
    global _CLIENT, _CLIENT_KEY
    cfg = (_endpoint_url(), _access_key_id(), _secret_access_key(), _region(), _addressing_style())
    if _CLIENT is not None and _CLIENT_KEY == cfg:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY != cfg:
            _CLIENT = _build_client(*cfg)
            _CLIENT_KEY = cfg
        return _CLIENT


def _get_signing_client() -> Any:
    if not _has_dedicated_env():
        return _store._get_signing_client()
    global _SIGNING_CLIENT, _SIGNING_CLIENT_KEY
    cfg = (
        signing_endpoint_url(),
        _access_key_id(),
        _secret_access_key(),
        _region(),
        _addressing_style(),
    )
    if _SIGNING_CLIENT is not None and _SIGNING_CLIENT_KEY == cfg:
        return _SIGNING_CLIENT
    with _CLIENT_LOCK:
        if _SIGNING_CLIENT is None or _SIGNING_CLIENT_KEY != cfg:
            _SIGNING_CLIENT = _build_client(*cfg)
            _SIGNING_CLIENT_KEY = cfg
        return _SIGNING_CLIENT


def _reset_clients_for_test() -> None:
    """Drop cached S3 clients and partial-config warning state (test-only helper)."""
    global _CLIENT, _CLIENT_KEY, _SIGNING_CLIENT, _SIGNING_CLIENT_KEY, _PARTIAL_ENV_WARNED_MISSING
    with _CLIENT_LOCK:
        _CLIENT = None
        _CLIENT_KEY = None
        _SIGNING_CLIENT = None
        _SIGNING_CLIENT_KEY = None
        _PARTIAL_ENV_WARNED_MISSING = None


def is_chat_upload_storage_configured() -> bool:
    """True when chat upload storage (``DALTON_CHAT_UPLOAD_STORAGE_*`` or fallback ``DALTON_ASR_STORAGE_*``) is configured."""
    if _has_dedicated_env():
        return True
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
    if not _has_dedicated_env():
        return _store.open_multipart_upload(object_key, content_type=content_type)
    try:
        created = _get_client().create_multipart_upload(
            Bucket=_bucket_name(),
            Key=resolve_full_key(object_key),
            ContentType=content_type,
        )
        return created["UploadId"]
    except Exception as exc:
        logger.error("Failed to open a multipart upload for %s: %s", object_key, exc)
        return None


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
        resp = _get_client().upload_part(
            Bucket=_bucket_name(),
            Key=resolve_full_key(object_key),
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
    if not _has_dedicated_env():
        return _store.list_uploaded_parts(object_key, store_upload_id)
    parts: list[dict[str, Any]] = []
    marker = 0
    try:
        client = _get_client()
        while True:
            page = client.list_parts(
                Bucket=_bucket_name(),
                Key=resolve_full_key(object_key),
                UploadId=store_upload_id,
                PartNumberMarker=marker,
            )
            for part in page.get("Parts") or []:
                parts.append(
                    {
                        "part_number": int(part["PartNumber"]),
                        "size": int(part["Size"]),
                        "etag": part["ETag"],
                    }
                )
            if not page.get("IsTruncated"):
                break
            marker = int(page["NextPartNumberMarker"])
    except Exception as exc:
        logger.error("Failed to list parts for chat upload %s: %s", object_key, exc)
        return None
    parts.sort(key=lambda p: p["part_number"])
    return parts


def complete_multipart_upload(
    object_key: str, store_upload_id: str, parts: list[tuple[int, str]]
) -> bool:
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    if not _has_dedicated_env():
        return _store.complete_multipart_upload(object_key, store_upload_id, parts)
    try:
        _get_client().complete_multipart_upload(
            Bucket=_bucket_name(),
            Key=resolve_full_key(object_key),
            UploadId=store_upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": number, "ETag": etag}
                    for number, etag in sorted(parts)
                ]
            },
        )
        return True
    except Exception as exc:
        logger.error("Failed to complete multipart upload for %s: %s", object_key, exc)
        return False


def abort_multipart_upload(object_key: str, store_upload_id: str) -> bool:
    if not is_chat_upload_key(object_key):
        logger.error("Refusing to abort non-chat-upload key %r", object_key)
        return False
    if not _has_dedicated_env():
        return _store.abort_multipart_upload(object_key, store_upload_id)
    from botocore.exceptions import ClientError

    try:
        _get_client().abort_multipart_upload(
            Bucket=_bucket_name(),
            Key=resolve_full_key(object_key),
            UploadId=store_upload_id,
        )
        return True
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code") == "NoSuchUpload"
            or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        ):
            return True
        logger.error("Failed to abort multipart upload for %s: %s", object_key, exc)
        return False
    except Exception as exc:
        logger.error("Failed to abort multipart upload for %s: %s", object_key, exc)
        return False


def delete_object(object_key: str) -> bool:
    if not is_chat_upload_key(object_key):
        logger.error("Refusing to delete non-chat-upload key %r", object_key)
        return False
    if not _has_dedicated_env():
        return _store.delete_object(object_key)
    try:
        _get_client().delete_object(Bucket=_bucket_name(), Key=resolve_full_key(object_key))
        return True
    except Exception as exc:
        logger.error("Deletion failed for chat upload %s: %s", object_key, exc)
        return False


def object_size(object_key: str) -> int | None:
    if not is_chat_upload_key(object_key):
        return None
    if not _has_dedicated_env():
        return _store.object_size(object_key)
    from botocore.exceptions import ClientError

    try:
        head = _get_client().head_object(Bucket=_bucket_name(), Key=resolve_full_key(object_key))
        return int(head["ContentLength"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
            logger.error("Failed to read size of chat upload %s: %s", object_key, exc)
        return None
    except Exception as exc:
        logger.error("Failed to read size of chat upload %s: %s", object_key, exc)
        return None


def sign_download_url(object_key: str, *, expires_in: int = 900) -> str | None:
    """Presign a GET URL against ``signing_endpoint_url()`` for sandbox ``pull_direct``."""
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    try:
        return _get_signing_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": _bucket_name(),
                "Key": resolve_full_key(object_key),
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
        resp = _get_client().get_object(
            Bucket=_bucket_name(),
            Key=resolve_full_key(object_key),
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
    """Yield bounded chunks of ``object_key`` for streaming downloads and sandbox fallback."""
    if not is_chat_upload_key(object_key):
        raise ValueError(f"Refusing non-chat-upload object key: {object_key!r}")
    step = max(65536, int(chunk_size))
    resp = _get_client().get_object(
        Bucket=_bucket_name(),
        Key=resolve_full_key(object_key),
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
