"""Object storage & deployment-wide Postgres concurrency lock for DM chart snapshots.

Reuses ``src.dalton.chat_upload_storage`` dual-profile credentials and S3/MinIO client pool:
- ``DALTON_CHAT_UPLOAD_STORAGE_*`` first (pre-configured for ``DALTON_PROFILE=core`` MinIO
  ``http://minio:9000`` / bucket ``chat-uploads`` in ``docker-compose.core.yml``).
- Falls back transparently to Everflow's ``DALTON_ASR_STORAGE_*`` via ``transcription_storage``.

All objects live strictly under ``channel-snapshots/<thread_id>/<sha256[:16]>/chart_<layer_id>.png``
so chat upload and transcription audio prefixes remain isolated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from src.dalton import chat_upload_storage as _upload_store
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

CHANNEL_SNAPSHOT_KEY_PREFIX = "channel-snapshots"
CHANNEL_SNAPSHOT_DEFAULT_MAX_CONCURRENCY = 2
CHANNEL_SNAPSHOT_RENDER_TIMEOUT_S = 25.0
CHANNEL_SNAPSHOT_LOCK_WAIT_TIMEOUT_S = 20.0
CHANNEL_SNAPSHOT_MAX_PER_TURN = 4

# Strict snapshot key contract (§2.3): blocks path traversal, percent-encoding bypass,
# uppercase/non-UUID thread IDs, and arbitrary suffixes.
SNAPSHOT_KEY_RE = re.compile(
    r"^channel-snapshots/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"([0-9a-f]{16})/"
    r"chart_([A-Za-z0-9_.-]+)\.png\Z"
)

_SAFE_LAYER_ID_CHAR_RE = re.compile(r"[^A-Za-z0-9_.-]")
_UUID_LOWER_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")

# Stable 64-bit signed Postgres advisory lock base namespace for chart snapshot render slots.
_ADVISORY_LOCK_NS = 0x51A0_7C01_0000_0000

# Per-event-loop process-local semaphore map so tests creating fresh loops never collide.
_LOOP_SEMAPHORES: dict[tuple[int, int], asyncio.Semaphore] = {}


def get_snapshot_max_concurrency() -> int:
    """Resolve deployment-wide Chromium snapshot concurrency cap (default 2)."""
    raw = (os.getenv("DALTON_CHANNEL_SNAPSHOT_MAX_CONCURRENCY") or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed >= 1:
                return min(parsed, 8)
        except ValueError:
            pass
    return CHANNEL_SNAPSHOT_DEFAULT_MAX_CONCURRENCY


def _get_process_semaphore(max_concurrency: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = (id(loop), max(1, int(max_concurrency)))
    sem = _LOOP_SEMAPHORES.get(key)
    if sem is not None:
        return sem
    sem = asyncio.Semaphore(key[1])
    _LOOP_SEMAPHORES[key] = sem
    return sem


def _advisory_slot_key(slot_index: int) -> int:
    """Deterministic signed 64-bit Postgres advisory lock key for render slot ``slot_index``."""
    raw = _ADVISORY_LOCK_NS + int(slot_index)
    # Keep strictly inside signed bigint range [-(2^63), 2^63 - 1].
    if raw >= (1 << 63):
        raw -= 1 << 64
    return raw


def is_snapshot_storage_configured() -> bool:
    """True when ``DALTON_CHAT_UPLOAD_STORAGE_*`` (Core MinIO) or fallback ``DALTON_ASR_STORAGE_*`` is configured."""
    return _upload_store.is_chat_upload_storage_configured()


def sanitize_snapshot_layer_id(layer_id: str) -> str:
    """Sanitize a layer id into safe ``[A-Za-z0-9_.-]+`` characters without traversal segments."""
    cleaned = _SAFE_LAYER_ID_CHAR_RE.sub("_", str(layer_id or "").strip())
    cleaned = cleaned.replace("..", "_").strip("._-")
    if not cleaned:
        digest = hashlib.sha256(str(layer_id or "").encode("utf-8")).hexdigest()[:12]
        return f"layer_{digest}"
    return cleaned[:96]


def build_snapshot_key(thread_id: str, sha256_hex: str, layer_id: str) -> str:
    """Build and validate ``channel-snapshots/{thread_id}/{sha256[:16]}/chart_{layer_id}.png``."""
    tid = str(thread_id or "").strip().lower()
    if not _UUID_LOWER_RE.match(tid):
        raise ValueError(f"Invalid snapshot thread_id: {thread_id!r}")
    sha_prefix = str(sha256_hex or "").strip().lower()[:16]
    if not _HEX16_RE.match(sha_prefix):
        raise ValueError(f"Invalid snapshot sha256 prefix: {sha256_hex!r}")
    safe_layer = sanitize_snapshot_layer_id(layer_id)
    key = f"{CHANNEL_SNAPSHOT_KEY_PREFIX}/{tid}/{sha_prefix}/chart_{safe_layer}.png"
    if parse_snapshot_key(key) is None:
        raise ValueError(f"Constructed snapshot key failed contract check: {key!r}")
    return key


def parse_snapshot_key(snapshot_key: str | None) -> tuple[str, str, str] | None:
    """Validate ``snapshot_key`` against the strict regex and return ``(thread_id, sha16, safe_layer)``.

    Rejects leading/trailing whitespace, ``..``, percent-encoding, backslashes, and NUL bytes.
    """
    if not isinstance(snapshot_key, str) or not snapshot_key:
        return None
    if (
        snapshot_key != snapshot_key.strip()
        or ".." in snapshot_key
        or "%" in snapshot_key
        or "\\" in snapshot_key
        or "\x00" in snapshot_key
    ):
        return None
    m = SNAPSHOT_KEY_RE.match(snapshot_key)
    if not m:
        return None
    layer_part = m.group(3)
    if not layer_part or ".." in layer_part or "/" in layer_part:
        return None
    return m.group(1), m.group(2), layer_part


def is_valid_snapshot_key(snapshot_key: str | None) -> bool:
    """Return True iff ``snapshot_key`` strictly matches the snapshot object key contract."""
    return parse_snapshot_key(snapshot_key) is not None


def _ensure_bucket_if_missing(client: Any, bucket: str) -> None:
    """Create bucket on first NoSuchBucket error (Core MinIO fresh stack convenience)."""
    try:
        client.create_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 - best-effort; caller retries put_object
        logger.debug("create_bucket(%s) ignored: %s", bucket, exc)


def put_snapshot_bytes(
    snapshot_key: str,
    data: bytes,
    *,
    content_type: str = "image/png",
) -> bool:
    """Store a PNG chart snapshot under ``channel-snapshots/`` in the active object store."""
    if not is_valid_snapshot_key(snapshot_key):
        raise ValueError(f"Refusing non-snapshot object key: {snapshot_key!r}")
    if not data:
        raise ValueError("Refusing empty snapshot payload")
    if not is_snapshot_storage_configured():
        return False

    bucket = _upload_store._bucket_name()
    full_key = _upload_store.resolve_full_key(snapshot_key)
    client = _upload_store._get_client()

    try:
        client.put_object(
            Bucket=bucket,
            Key=full_key,
            Body=data,
            ContentType=content_type,
            ContentLength=len(data),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        err_code = ""
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict):
            err_code = str(resp.get("Error", {}).get("Code") or "")
        if err_code in ("NoSuchBucket", "404"):
            _ensure_bucket_if_missing(client, bucket)
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=full_key,
                    Body=data,
                    ContentType=content_type,
                    ContentLength=len(data),
                )
                return True
            except Exception as retry_exc:  # noqa: BLE001
                logger.error(
                    "Failed to upload chart snapshot %s after bucket init: %s",
                    snapshot_key,
                    retry_exc,
                )
                return False
        logger.error("Failed to upload chart snapshot %s: %s", snapshot_key, exc)
        return False


def get_snapshot_bytes(snapshot_key: str) -> bytes | None:
    """Read a chart snapshot object from the active object store; returns None if missing/failed."""
    if not is_valid_snapshot_key(snapshot_key):
        return None
    if not is_snapshot_storage_configured():
        return None

    bucket = _upload_store._bucket_name()
    full_key = _upload_store.resolve_full_key(snapshot_key)
    body = None
    try:
        resp = _upload_store._get_client().get_object(Bucket=bucket, Key=full_key)
        body = resp.get("Body")
        if body is None:
            return None
        data = body.read()
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None
    except Exception as exc:  # noqa: BLE001
        resp = getattr(exc, "response", None)
        code = (
            str(resp.get("Error", {}).get("Code") or "")
            if isinstance(resp, dict)
            else ""
        )
        if code not in ("404", "NoSuchKey"):
            logger.error("Failed to read chart snapshot %s: %s", snapshot_key, exc)
        return None
    finally:
        if body is not None:
            try:
                body.close()
            except Exception as close_exc:  # noqa: BLE001
                logger.debug("Failed to close snapshot body stream: %s", close_exc)


def delete_snapshot_object(snapshot_key: str) -> bool:
    """Delete a chart snapshot object under ``channel-snapshots/``."""
    if not is_valid_snapshot_key(snapshot_key):
        logger.error("Refusing to delete non-snapshot key %r", snapshot_key)
        return False
    if not is_snapshot_storage_configured():
        return False
    try:
        _upload_store._get_client().delete_object(
            Bucket=_upload_store._bucket_name(),
            Key=_upload_store.resolve_full_key(snapshot_key),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete chart snapshot %s: %s", snapshot_key, exc)
        return False


class SnapshotRenderSlotTimeoutError(TimeoutError):
    """Raised when waiting for a deployment-wide Chromium snapshot render slot timed out."""


@asynccontextmanager
async def acquire_snapshot_render_slot(
    *,
    timeout_s: float = CHANNEL_SNAPSHOT_LOCK_WAIT_TIMEOUT_S,
    max_concurrency: int | None = None,
    local_concurrency: int | None = 1,
    poll_interval_s: float = 0.2,
    conn_factory: Callable[[], Any] | None = None,
) -> AsyncIterator[int]:
    """Acquire both a process-local semaphore slot and a deployment-wide Postgres advisory lock slot.

    ``local_concurrency`` defaults to 1 to match ``chartkit``'s single-threaded ``RENDERER`` so
    one worker process never holds more global advisory lock slots than it can concurrently render.
    Limits total concurrent Chromium renders across all backend worker processes to
    ``max_concurrency`` (default 2). Raises ``SnapshotRenderSlotTimeoutError`` when no
    slot becomes free within ``timeout_s``.
    """
    cap = max(1, int(max_concurrency or get_snapshot_max_concurrency()))
    local_cap = cap if local_concurrency is None else min(cap, max(1, int(local_concurrency)))
    sem = _get_process_semaphore(local_cap)
    deadline = time.monotonic() + max(0.05, float(timeout_s))

    remaining = max(0.01, deadline - time.monotonic())
    try:
        await asyncio.wait_for(sem.acquire(), timeout=remaining)
    except TimeoutError as exc:
        raise SnapshotRenderSlotTimeoutError(
            f"Timed out waiting for local chart snapshot render slot ({timeout_s:.1f}s)"
        ) from exc

    open_conn = conn_factory or get_db_connection
    acquired_slot: int | None = None
    acquired_lock_key: int | None = None
    try:
        async with open_conn() as conn:
            while True:
                for slot_idx in range(cap):
                    lock_key = _advisory_slot_key(slot_idx)
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
                        row = await cur.fetchone()
                    got = bool(row[0]) if row else False
                    if got:
                        acquired_slot = slot_idx
                        acquired_lock_key = lock_key
                        break
                if acquired_slot is not None:
                    break
                if time.monotonic() >= deadline:
                    raise SnapshotRenderSlotTimeoutError(
                        f"Timed out waiting for Postgres chart snapshot render slot ({timeout_s:.1f}s)"
                    )
                sleep_for = min(poll_interval_s, max(0.01, deadline - time.monotonic()))
                await asyncio.sleep(sleep_for)
            try:
                yield acquired_slot
            finally:
                if acquired_lock_key is not None:
                    try:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT pg_advisory_unlock(%s)", (acquired_lock_key,)
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Failed to release Postgres snapshot advisory lock slot %s: %s",
                            acquired_slot,
                            exc,
                        )
    finally:
        sem.release()
