"""Object storage for A1 transcription audio, independent of the global ``STORAGE_*`` store.

The global store stays off in production precisely because turning it on would
change twenty-plus upstream features at once; transcription therefore gets its
own ``DALTON_ASR_STORAGE_*`` configuration — the same variables the bridge's
``SourceObjectStore`` (``scripts/dalton/asr_jobs.py``) already reads — so the
backend uploads, the bridge downloads, and no upstream behavior moves.

The key-prefix rule must stay byte-identical to the bridge's
``SourceObjectStore.resolve_full_key``: the backend writes the objects the
bridge reads, so any divergence is an object the bridge cannot find.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)


def _endpoint_url() -> str:
    return (os.getenv("DALTON_ASR_STORAGE_ENDPOINT_URL") or "").strip().rstrip("/")


def signing_endpoint_url() -> str:
    """The endpoint part-upload URLs are signed for.

    ``DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL`` is the address the sandbox
    reaches MinIO at (``http://host.docker.internal:9000`` in production),
    which differs from the backend container's own endpoint; it falls back to
    the plain endpoint when unset.
    """
    explicit = (
        os.getenv("DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL") or ""
    ).strip().rstrip("/")
    return explicit or _endpoint_url()


def _bucket_name() -> str:
    return (os.getenv("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()


def _access_key_id() -> str:
    return (os.getenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID") or "").strip()


def _secret_access_key() -> str:
    return (os.getenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY") or "").strip()


def _region() -> str:
    return (os.getenv("DALTON_ASR_STORAGE_REGION") or "").strip() or "us-east-1"


def _addressing_style() -> str:
    return (
        (os.getenv("DALTON_ASR_STORAGE_ADDRESSING_STYLE") or "").strip().lower()
        or "path"
    )


def is_transcription_storage_configured() -> bool:
    """True only when endpoint, bucket and both credentials are all set."""
    return bool(
        _endpoint_url()
        and _bucket_name()
        and _access_key_id()
        and _secret_access_key()
    )


def _key_prefix() -> str:
    raw = (os.getenv("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    return f"{raw}/" if raw else ""


def resolve_full_key(object_key: str) -> str:
    """The key as the store holds it: the prefix plus the key, prefix-free keys kept as-is.

    Mirrors ``SourceObjectStore.resolve_full_key`` exactly (prefix stripped of
    stray slashes, key stripped and left-trimmed, an already-prefixed key not
    double-prefixed); the parity is pinned by a unit test calling both sides.
    """
    cleaned = str(object_key or "").strip().lstrip("/")
    prefix = _key_prefix()
    if prefix and not cleaned.startswith(prefix):
        return f"{prefix}{cleaned}"
    return cleaned


# boto3 is imported inside `_build_client`, never at module top: the module
# must import on a Python without boto3, the same contract the bridge's
# ``SourceObjectStore`` holds (tests/unit/dalton/test_asr_boto3_lazy_import.py).

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any | None = None
_SIGNING_CLIENT: Any | None = None


def _build_client(endpoint_url: str) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=_access_key_id(),
        aws_secret_access_key=_secret_access_key(),
        region_name=_region(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": _addressing_style()},
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _get_client() -> Any:
    global _CLIENT
    client = _CLIENT
    if client is not None:
        return client
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = _build_client(_endpoint_url())
        return _CLIENT


def _get_signing_client() -> Any:
    global _SIGNING_CLIENT
    client = _SIGNING_CLIENT
    if client is not None:
        return client
    with _CLIENT_LOCK:
        if _SIGNING_CLIENT is None:
            _SIGNING_CLIENT = _build_client(signing_endpoint_url())
        return _SIGNING_CLIENT


def _reset_clients_for_test() -> None:
    """Drop the cached clients. Test-only — production never re-initializes."""
    global _CLIENT, _SIGNING_CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None
        _SIGNING_CLIENT = None


def open_multipart_upload(key: str, *, content_type: str) -> str | None:
    """Start a multipart upload under ``key``; its upload id, or ``None`` if refused."""
    try:
        created = _get_client().create_multipart_upload(
            Bucket=_bucket_name(), Key=resolve_full_key(key), ContentType=content_type
        )
        return created["UploadId"]
    except Exception as e:
        logger.error(f"Failed to open a multipart upload for {key}: {e}")
        return None


def sign_upload_part(
    key: str,
    upload_id: str,
    *,
    part_number: int,
    size: int,
    expires_in: int = 900,
) -> str | None:
    """Presign one part's PUT with its length signed in; ``None`` if signing failed.

    The length binds the body, so whoever holds the URL writes exactly that
    many bytes to that part or nothing. Always signed for
    ``signing_endpoint_url()`` — the URL's consumer is the sandbox, which
    reaches MinIO at a different address than the backend container.
    """
    try:
        return _get_signing_client().generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": _bucket_name(),
                "Key": resolve_full_key(key),
                "UploadId": upload_id,
                "PartNumber": part_number,
                "ContentLength": size,
            },
            ExpiresIn=expires_in,
        )
    except Exception as e:
        logger.error(f"Failed to presign part {part_number} of {key}: {e}")
        return None


def list_uploaded_parts(key: str, upload_id: str) -> list[dict[str, Any]] | None:
    """The parts the store holds for an open upload, in order, as it counts them.

    Each is ``{"part_number", "size", "etag"}``. ``None`` when the store cannot
    say, including for an upload that no longer exists. Read from the store
    rather than taken from the uploader, so assembling never trusts a client's
    word for which parts arrived or how large they were.
    """
    parts: list[dict[str, Any]] = []
    marker = 0
    try:
        client = _get_client()
        while True:
            page = client.list_parts(
                Bucket=_bucket_name(),
                Key=resolve_full_key(key),
                UploadId=upload_id,
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
    except Exception as e:
        logger.error(f"Failed to list the parts of {key}: {e}")
        return None
    parts.sort(key=lambda p: p["part_number"])
    return parts


def complete_multipart_upload(
    key: str, upload_id: str, parts: list[tuple[int, str]]
) -> bool:
    """Assemble an uploaded multipart into its object. ``parts`` is (number, etag)."""
    try:
        _get_client().complete_multipart_upload(
            Bucket=_bucket_name(),
            Key=resolve_full_key(key),
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": number, "ETag": etag}
                    for number, etag in sorted(parts)
                ]
            },
        )
        return True
    except Exception as e:
        logger.error(f"Failed to complete multipart upload for {key}: {e}")
        return False


def abort_multipart_upload(key: str, upload_id: str) -> bool:
    """Discard an unfinished multipart upload and the parts already stored.

    Returns whether the store agreed, which an upload it no longer knows
    counts as: aborted before, or assembled, it holds no parts.
    """
    from botocore.exceptions import ClientError

    try:
        _get_client().abort_multipart_upload(
            Bucket=_bucket_name(), Key=resolve_full_key(key), UploadId=upload_id
        )
        return True
    except ClientError as e:
        if (
            e.response.get("Error", {}).get("Code") == "NoSuchUpload"
            or e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        ):
            return True
        logger.error(f"Failed to abort multipart upload for {key}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to abort multipart upload for {key}: {e}")
        return False


def object_size(key: str) -> int | None:
    """The stored object's length, or ``None`` when there is none or the store did not say."""
    from botocore.exceptions import ClientError

    try:
        head = _get_client().head_object(
            Bucket=_bucket_name(), Key=resolve_full_key(key)
        )
        return int(head["ContentLength"])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
            logger.error(f"Failed to read the size of {key}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to read the size of {key}: {e}")
        return None


def delete_object(key: str) -> bool:
    """Delete an object from the transcription store."""
    from botocore.exceptions import ClientError

    try:
        _get_client().delete_object(Bucket=_bucket_name(), Key=resolve_full_key(key))
        logger.debug(f"Deleted {key}")
        return True
    except ClientError as e:
        logger.error(f"Deletion failed for {key}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error deleting {key}: {e}")
        return False
