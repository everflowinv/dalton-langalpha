"""Redis write-timeout diagnostics, connection-seam timing, and PG-elected server sampler.

Implements Production Audit 2 §4 ("diagnose first, do not alter Redis write behavior"):
1. ``InstrumentedBlockingConnectionPool`` + ``InstrumentedAsyncConnection`` split each
   Redis operation into:
   - ``pool_queue_ms``: pool-slot queue wait before ``ensure_connection``.
   - ``health_check_ms``: TCP/AUTH handshake (``connect_check_health``) and idle-reuse
     ``PING`` round-trips (``check_health``).
   - ``acquire_ms = pool_queue_ms + health_check_ms``.
   - ``wire_ms``: pure socket write/drain (``_send_packed_command``) and response read
     (``read_response``, excluding ``disconnect()`` cleanup time on read error) after
     checkout while ``_in_health_check`` is False, covering both single commands and
     ``pipeline(transaction=True).execute()``.
2. Per-worker rolling 5-minute diagnostics window maintained in memory on the hot path
   and flushed atomically off the event loop (via ``asyncio.to_thread``) to
   ``var/dalton/ops/redis_diag_worker_{pid}.json`` every 30s and on timeout/transport_lost
   edges (process-local diagnostics only — never consulted as business truth).
3. Cluster-wide single-leader Redis server sampling (``INFO persistence/memory/stats/clients``,
   ``SLOWLOG GET 5``, ``LATENCY LATEST``) using a dedicated standalone diagnostic Redis
   connection (never borrowing from the business cache pool, 1s timeout) and elected via a
   single PostgreSQL session advisory lock
   ``pg_try_advisory_lock(hashtextextended('dalton:diag:redis_sampler_leader', 0))``
   held on a dedicated non-pooled ``AsyncConnection`` opened via
   ``src.server.database.pool.open_session_connection()``, re-verified via ``SELECT 1``
   before every off-loop write to ``var/dalton/ops/redis_diag_server.json``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Optional
import uuid
import weakref

import psycopg
import redis.asyncio as redis_async
import redis.exceptions as redis_exceptions
from redis.asyncio.connection import BlockingConnectionPool, Connection

from src.dalton.settings import get_redis_diag_ops_dir, is_redis_diagnostics_enabled
from src.observability.heavy_tasks import heavy_task
from src.utils.concurrency import cancel_and_join

logger = logging.getLogger(__name__)

# Thresholds aligned with prod-audit-2.md §4.3
SLOW_WRITE_THRESHOLD_MS = 500.0
HEAVY_SERIALIZE_THRESHOLD_BYTES = 256 * 1024  # 256 KiB
LARGE_PAYLOAD_ALERT_BYTES = 512 * 1024  # 512 KiB
WORKER_FLUSH_INTERVAL_S = 30.0
SERVER_SAMPLE_INTERVAL_S = 30.0
EDGE_FLUSH_MIN_INTERVAL_S = 1.0
EDGE_SERVER_SAMPLE_COOLDOWN_S = 60.0
SERVER_SAMPLE_REDIS_TIMEOUT_S = 1.0
WINDOW_SECONDS = 300.0
STALE_WORKER_SECONDS = 180.0
STALE_SERVER_SECONDS = SERVER_SAMPLE_INTERVAL_S * 3  # 90s (3x sampling interval)
SLOW_LOG_MAX_PER_SECOND = 5
TRANSPORT_LOST_LOG_MAX_PER_SECOND = 2
MAX_RECENT_EVENTS = 25

# Cluster alert thresholds (prod-audit-2.md §4.3 决定 3)
P2_RECOVERED_5M_THRESHOLD = 5
P2_POOL_UTILIZATION_THRESHOLD = 0.80
P2_LOOP_LAG_MS_THRESHOLD = 1500.0
P2_LATEST_FORK_USEC_THRESHOLD = 200_000
P2_SLOWLOG_US_THRESHOLD = 100_000  # 100 ms

LEADER_LOCK_NAME = "dalton:diag:redis_sampler_leader"
LEADER_LOCK_ACQUIRE_SQL = "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))"
LEADER_LOCK_RELEASE_SQL = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"
LEADER_LOCK_VERIFY_SQL = (
    "WITH k AS (SELECT hashtextextended(%s, 0) AS key) "
    "SELECT EXISTS ("
    "  SELECT 1 FROM pg_locks, k "
    "  WHERE locktype = 'advisory' "
    "    AND objsubid = 1 "
    "    AND pid = pg_backend_pid() "
    "    AND granted "
    "    AND classid::text::bigint = ((k.key >> 32) & 4294967295::bigint) "
    "    AND objid::text::bigint = (k.key & 4294967295::bigint)"
    ")"
)
SERVER_EPOCH_FILENAME = ".redis_diag_server.epoch.json"

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_ALLOWED_SUB_NAMESPACES = frozenset(
    {
        "stream",
        "meta",
        "events",
        "lock",
        "state",
        "cancel",
        "control",
        "index",
        "diag",
        "ops",
        "quota",
        "heartbeat",
        "cache",
    }
)
_UUID_INLINE_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_COLON_KEY_INLINE_RE = re.compile(r"\b[A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+\b")
_TUPLE_ID_INLINE_RE = re.compile(r"\(['\"][^'\"]{1,128}['\"],\s*['\"][^'\"]{1,128}['\"]\)")
_LONG_HEX_INLINE_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")


def get_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _short_key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:8]


def sanitize_key_prefix(key: Any) -> str:
    """Return ONLY a safe prefix category plus truncated SHA-256 hash — never the raw key.

    Examples:
        ``overview:BRK-B`` -> ``overview#<8hex>``
        ``workflow:stream:<uuid>:<run>`` -> ``workflow:stream#<8hex>``
        ``subagent:stream:<thread>:<task>`` -> ``subagent:stream#<8hex>``
        ``BRK-B`` -> ``key#<8hex>``
        ``Cache set error for overview:BRK-B`` -> ``cache_set#<8hex>``
    """
    raw = str(key or "").strip()
    if not raw:
        return "unknown"
    digest = _short_key_hash(raw)

    # Free-form context strings (e.g. "Cache set error for overview:BRK-B" or tuple labels)
    if any(ch.isspace() for ch in raw) or raw.startswith("("):
        low = raw.lower()
        if "cache set" in low:
            cat = "cache_set"
        elif "cache get" in low:
            cat = "cache_get"
        elif "cache del" in low:
            cat = "cache_del"
        elif "stream" in low or "event" in low:
            cat = "stream"
        else:
            cat = "cache_error"
        return f"{cat}#{digest}"

    parts = [p for p in raw.split(":") if p]
    if len(parts) <= 1:
        # Single-segment string IS the raw key — never echo it verbatim.
        return f"key#{digest}"

    seg0 = parts[0]
    if not _CATEGORY_RE.match(seg0):
        return f"key#{digest}"

    if len(parts) >= 2 and parts[1] in _ALLOWED_SUB_NAMESPACES:
        return f"{seg0}:{parts[1]}#{digest}"
    return f"{seg0}#{digest}"


def sanitize_diagnostic_reason(
    reason: str = "",
    *,
    exc: BaseException | None = None,
    phase: str = "",
) -> str:
    """Redact raw Redis keys, UUIDs, thread/run tuples, and long IDs from diagnostic text."""
    text = (reason or "").strip()
    if not text and exc is not None:
        text = f"{type(exc).__name__}: {exc}"
    if not text:
        return phase or "unknown"
    scrubbed = _TUPLE_ID_INLINE_RE.sub("<redacted_pair>", text)
    # Redact full colon-delimited keys BEFORE UUID substitution so embedded UUIDs
    # inside keys like ``subagent:stream:<uuid>:<run_id>`` do not split the key token.
    scrubbed = _COLON_KEY_INLINE_RE.sub(
        lambda m: sanitize_key_prefix(m.group(0)),
        scrubbed,
    )
    scrubbed = _UUID_INLINE_RE.sub("<uuid>", scrubbed)
    scrubbed = _LONG_HEX_INLINE_RE.sub("<id>", scrubbed)
    return scrubbed[:200]


def _estimate_str_utf8_bytes(text: str) -> int:
    """O(1) UTF-8 byte-size upper-bound estimate with zero encoding allocation.

    ``str.isascii()`` reads CPython's cached ASCII flag in O(1); non-ASCII text
    (e.g. CJK at 3 bytes/char) uses a 3x multiplier so ~100K Chinese characters
    (~300 KiB) are accurately flagged as >= 256 KiB without encoding.
    """
    return len(text) if text.isascii() else len(text) * 3


def estimate_serialized_bytes(serialized: str | bytes | bytearray) -> int:
    """O(1) byte length estimate for an already-serialized payload (no re-encoding)."""
    if isinstance(serialized, (bytes, bytearray)):
        return len(serialized)
    if isinstance(serialized, str):
        return _estimate_str_utf8_bytes(serialized)
    return 0


def is_large_serialize_value(
    value: Any,
    *,
    threshold_bytes: int = HEAVY_SERIALIZE_THRESHOLD_BYTES,
    max_steps: int = 256,
) -> bool:
    """Cheaply estimate whether serializing ``value`` to JSON is likely >= ``threshold_bytes``.

    Strictly bounded to ``max_steps`` element visits (O(1) worst-case) and accounts
    for multi-byte UTF-8 (e.g. CJK) via O(1) ``str.isascii()`` without encoding.
    """
    if isinstance(value, (bytes, bytearray)):
        return len(value) >= threshold_bytes
    if isinstance(value, str):
        return _estimate_str_utf8_bytes(value) >= threshold_bytes
    if not isinstance(value, (dict, list, tuple)):
        return False

    estimated = 0
    steps = 0
    stack: list[Any] = [value]
    while stack:
        cur = stack.pop()
        steps += 1
        if steps >= max_steps:
            return True
        if isinstance(cur, (bytes, bytearray)):
            estimated += len(cur) + 2
        elif isinstance(cur, str):
            estimated += _estimate_str_utf8_bytes(cur) + 2
        elif isinstance(cur, dict):
            estimated += 2 + len(cur) * 8
            if estimated >= threshold_bytes:
                return True
            for k, v in cur.items():
                steps += 1
                if steps >= max_steps:
                    return True
                if isinstance(k, str):
                    estimated += _estimate_str_utf8_bytes(k)
                if isinstance(v, (bytes, bytearray)):
                    estimated += len(v)
                elif isinstance(v, str):
                    estimated += _estimate_str_utf8_bytes(v)
                elif isinstance(v, (dict, list, tuple)):
                    stack.append(v)
                else:
                    estimated += 8
                if estimated >= threshold_bytes:
                    return True
        elif isinstance(cur, (list, tuple)):
            estimated += 2 + len(cur) * 4
            if estimated >= threshold_bytes:
                return True
            for item in cur:
                steps += 1
                if steps >= max_steps:
                    return True
                if isinstance(item, (bytes, bytearray)):
                    estimated += len(item)
                elif isinstance(item, str):
                    estimated += _estimate_str_utf8_bytes(item)
                elif isinstance(item, (dict, list, tuple)):
                    stack.append(item)
                else:
                    estimated += 8
                if estimated >= threshold_bytes:
                    return True
        if estimated >= threshold_bytes:
            return True
    return False


def dumps_with_heavy_task(
    value: Any,
    *,
    cls: Any = None,
    ensure_ascii: bool = False,
    tag: str = "redis.cache.serialize",
    threshold_bytes: int = HEAVY_SERIALIZE_THRESHOLD_BYTES,
) -> str:
    """``json.dumps`` wrapped in ``heavy_task(tag)`` when diagnostics are enabled and payload >= threshold."""
    if not is_redis_diagnostics_enabled():
        return json.dumps(value, ensure_ascii=ensure_ascii, cls=cls)
    if is_large_serialize_value(value, threshold_bytes=threshold_bytes):
        with heavy_task(tag):
            return json.dumps(value, ensure_ascii=ensure_ascii, cls=cls)
    return json.dumps(value, ensure_ascii=ensure_ascii, cls=cls)


def encode_stream_frame_with_heavy_task(
    event: str | bytes,
    *,
    tag: str = "redis.stream.encode",
    threshold_bytes: int = HEAVY_SERIALIZE_THRESHOLD_BYTES,
) -> bytes:
    """UTF-8 encode an SSE stream frame, tagging large frames (>=256 KiB, incl. CJK) with ``heavy_task``."""
    if isinstance(event, bytes):
        return event
    if not is_redis_diagnostics_enabled():
        return event.encode("utf-8")
    if _estimate_str_utf8_bytes(event) >= threshold_bytes:
        with heavy_task(tag):
            return event.encode("utf-8")
    return event.encode("utf-8")


@dataclass
class RedisOpTiming:
    """Per-operation timing accumulator stored in a task-local ``ContextVar``."""

    op: str
    key_prefix: str
    payload_bytes: int = 0
    is_pipeline: bool = False
    command_count: int = 1
    pool_queue_ms: float = 0.0
    health_check_ms: float = 0.0
    wire_ms: float = 0.0
    started_monotonic: float = field(default_factory=time.monotonic)
    pool_ref: Any = None

    @property
    def acquire_ms(self) -> float:
        return self.pool_queue_ms + self.health_check_ms


_CURRENT_OP_TIMING: ContextVar[RedisOpTiming | None] = ContextVar(
    "_dalton_redis_op_timing", default=None
)
_POOL_QUEUE_START: ContextVar[float | None] = ContextVar(
    "_dalton_redis_pool_queue_start", default=None
)
_CURRENT_STREAM_PAYLOAD_BYTES: ContextVar[int] = ContextVar(
    "_dalton_redis_stream_payload_bytes", default=0
)
_ACTIVE_DIAG_TIMEOUT: ContextVar[asyncio.Timeout | None] = ContextVar(
    "_dalton_redis_active_timeout", default=None
)
_LAST_RECORDED_SPAN_EXC_REF: ContextVar[weakref.ReferenceType[BaseException] | None] = ContextVar(
    "_dalton_redis_last_span_exc_ref", default=None
)


def _mark_exc_recorded(exc: BaseException | None) -> None:
    """Mark an exception as already accounted for by ``redis_op_span`` so ``_log_error`` does not double-count it."""
    if exc is None:
        return
    try:
        setattr(exc, "_dalton_redis_diag_recorded", True)
    except Exception:
        pass
    try:
        _LAST_RECORDED_SPAN_EXC_REF.set(weakref.ref(exc))
    except TypeError:
        pass


def _consume_recorded_exc(exc: BaseException | None) -> bool:
    """Return True (and clear the marker) if ``exc`` was already recorded by ``redis_op_span``."""
    if exc is None:
        return False
    matched = False
    if getattr(exc, "_dalton_redis_diag_recorded", False):
        matched = True
        try:
            delattr(exc, "_dalton_redis_diag_recorded")
        except Exception:
            try:
                setattr(exc, "_dalton_redis_diag_recorded", False)
            except Exception:
                pass
    ref = _LAST_RECORDED_SPAN_EXC_REF.get()
    if ref is not None and ref() is exc:
        matched = True
        _LAST_RECORDED_SPAN_EXC_REF.set(None)
    return matched


@asynccontextmanager
async def diag_timeout(delay: float) -> AsyncIterator[None]:
    """``asyncio.timeout`` wrapper that lets an inner ``redis_op_span`` tell timeout-cancellation apart.

    When ``asyncio.timeout(3.5)`` fires, inner coroutines receive ``asyncio.CancelledError``
    before ``asyncio.timeout.__aexit__`` translates it to ``TimeoutError``. Exposing the
    ``asyncio.Timeout`` handle via a ``ContextVar`` lets ``redis_op_span`` inspect
    ``cm.expired()`` in its ``finally`` block and classify the attempt accurately as a timeout
    even when a subsequent retry recovers.
    """
    cm = asyncio.timeout(delay)
    if not is_redis_diagnostics_enabled():
        async with cm:
            yield
        return
    tok = _ACTIVE_DIAG_TIMEOUT.set(cm)
    try:
        async with cm:
            yield
    finally:
        _ACTIVE_DIAG_TIMEOUT.reset(tok)


def current_op_timing() -> RedisOpTiming | None:
    """Return the active ``RedisOpTiming`` for the current coroutine, if any."""
    return _CURRENT_OP_TIMING.get()


def _add_pool_queue_ms(ms: float) -> None:
    timing = _CURRENT_OP_TIMING.get()
    if timing is not None and ms > 0.0:
        timing.pool_queue_ms += ms


def _add_health_check_ms(ms: float) -> None:
    timing = _CURRENT_OP_TIMING.get()
    if timing is not None and ms > 0.0:
        timing.health_check_ms += ms


def _add_wire_ms(ms: float) -> None:
    timing = _CURRENT_OP_TIMING.get()
    if timing is not None and ms > 0.0:
        timing.wire_ms += ms


class InstrumentedAsyncConnection(Connection):
    """``redis.asyncio.Connection`` subclass that splits handshake/PING from pure wire I/O.

    - ``connect_check_health`` & ``check_health`` set ``self._in_health_check = True``
      and record elapsed time into ``health_check_ms``.
    - ``_send_packed_command`` & ``read_response`` record elapsed time into ``wire_ms``
      ONLY while ``self._in_health_check`` is False.
    - ``disconnect`` time incurred inside ``read_response`` error cleanup is subtracted
      so ``wire_ms`` reflects pure socket I/O only.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._in_health_check: bool = False
        self._in_wire_read: bool = False
        self._disconnect_ms_in_read: float = 0.0

    async def disconnect(self, nowait: bool = False, **kwargs: Any) -> Any:
        if not self._in_wire_read:
            return await super().disconnect(nowait=nowait, **kwargs)
        d0 = time.monotonic()
        try:
            return await super().disconnect(nowait=nowait, **kwargs)
        finally:
            self._disconnect_ms_in_read += (time.monotonic() - d0) * 1000.0

    async def connect_check_health(
        self,
        check_health: bool = True,
        retry_socket_connect: bool = True,
    ) -> Any:
        if self.is_connected:
            return await super().connect_check_health(
                check_health=check_health,
                retry_socket_connect=retry_socket_connect,
            )
        prev = self._in_health_check
        self._in_health_check = True
        t0 = time.monotonic()
        try:
            return await super().connect_check_health(
                check_health=check_health,
                retry_socket_connect=retry_socket_connect,
            )
        finally:
            self._in_health_check = prev
            if not prev:
                _add_health_check_ms((time.monotonic() - t0) * 1000.0)

    async def check_health(self) -> Any:
        needs_check = bool(
            self.health_check_interval
            and asyncio.get_running_loop().time() > self.next_health_check
        )
        if not needs_check:
            return None
        prev = self._in_health_check
        self._in_health_check = True
        t0 = time.monotonic()
        try:
            return await super().check_health()
        finally:
            self._in_health_check = prev
            if not prev:
                _add_health_check_ms((time.monotonic() - t0) * 1000.0)

    async def send_packed_command(
        self,
        command: bytes | str | Iterable[bytes],
        check_health: bool = True,
    ) -> None:
        if self.socket_timeout:
            await super().send_packed_command(command, check_health=check_health)
            return
        # When socket_timeout is None/0, upstream Connection.send_packed_command writes
        # directly to self._writer bypassing _send_packed_command. Route through
        # _send_packed_command so wire write latency is always measured consistently.
        if not self.is_connected:
            await self.connect_check_health(check_health=False)
        if check_health:
            await self.check_health()
        try:
            if isinstance(command, str):
                command = command.encode()
            if isinstance(command, bytes):
                command = [command]
            await self._send_packed_command(command)
        except OSError as e:
            await self.disconnect(nowait=True)
            if len(e.args) == 1:
                err_no, errmsg = "UNKNOWN", e.args[0]
            else:
                err_no = e.args[0]
                errmsg = e.args[1]
            raise redis_exceptions.ConnectionError(
                f"Error {err_no} while writing to socket. {errmsg}."
            ) from e
        except BaseException:
            await self.disconnect(nowait=True)
            raise

    async def _send_packed_command(self, command: Iterable[bytes]) -> None:
        if self._in_health_check:
            await super()._send_packed_command(command)
            return
        t0 = time.monotonic()
        try:
            await super()._send_packed_command(command)
        finally:
            _add_wire_ms((time.monotonic() - t0) * 1000.0)

    async def read_response(
        self,
        disable_decoding: bool = False,
        timeout: Optional[float] = None,
        *,
        disconnect_on_error: bool = True,
        push_request: Optional[bool] = False,
    ) -> Any:
        if self._in_health_check:
            return await super().read_response(
                disable_decoding=disable_decoding,
                timeout=timeout,
                disconnect_on_error=disconnect_on_error,
                push_request=push_request,
            )
        prev_in_read = self._in_wire_read
        prev_disc_ms = self._disconnect_ms_in_read
        self._in_wire_read = True
        self._disconnect_ms_in_read = 0.0
        t0 = time.monotonic()
        try:
            return await super().read_response(
                disable_decoding=disable_decoding,
                timeout=timeout,
                disconnect_on_error=disconnect_on_error,
                push_request=push_request,
            )
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            disc_ms = self._disconnect_ms_in_read
            self._in_wire_read = prev_in_read
            self._disconnect_ms_in_read = prev_disc_ms
            _add_wire_ms(max(0.0, elapsed_ms - disc_ms))


class InstrumentedBlockingConnectionPool(BlockingConnectionPool):
    """``BlockingConnectionPool`` subclass that times only slot-queue waiting."""

    async def get_connection(self, *args: Any, **kwargs: Any) -> Any:
        timing = _CURRENT_OP_TIMING.get()
        if timing is not None and timing.pool_ref is None:
            timing.pool_ref = self
        q_start = time.monotonic()
        token = _POOL_QUEUE_START.set(q_start)
        try:
            return await super().get_connection(*args, **kwargs)
        except BaseException:
            pending_start = _POOL_QUEUE_START.get()
            if pending_start is not None:
                _POOL_QUEUE_START.set(None)
                _add_pool_queue_ms((time.monotonic() - pending_start) * 1000.0)
            raise
        finally:
            _POOL_QUEUE_START.reset(token)

    async def ensure_connection(self, connection: Any) -> Any:
        pending_start = _POOL_QUEUE_START.get()
        if pending_start is not None:
            _POOL_QUEUE_START.set(None)
            _add_pool_queue_ms((time.monotonic() - pending_start) * 1000.0)
        return await super().ensure_connection(connection)


def create_cache_connection_pool(
    url: str,
    *,
    fallback_pool_cls: type[BlockingConnectionPool] = BlockingConnectionPool,
    **kwargs: Any,
) -> BlockingConnectionPool:
    """Create the Redis cache pool, instrumented when diagnostics are enabled."""
    if is_redis_diagnostics_enabled():
        pool_kwargs = dict(kwargs)
        pool_kwargs.setdefault("connection_class", InstrumentedAsyncConnection)
        return InstrumentedBlockingConnectionPool.from_url(url, **pool_kwargs)
    return fallback_pool_cls.from_url(url, **kwargs)


def _snapshot_single_pool(pool: Any) -> dict[str, Any]:
    if pool is None:
        return {"in_use": 0, "available": 0, "max": 0, "utilization": 0.0}
    try:
        in_use = len(getattr(pool, "_in_use_connections", ()) or ())
        avail = len(getattr(pool, "_available_connections", ()) or ())
        max_conn = int(getattr(pool, "max_connections", 0) or 0)
        util = (in_use / max_conn) if max_conn > 0 else 0.0
        return {
            "in_use": in_use,
            "available": avail,
            "max": max_conn,
            "utilization": round(util, 4),
        }
    except Exception:
        return {"in_use": 0, "available": 0, "max": 0, "utilization": 0.0}


def _snapshot_all_pools() -> dict[str, dict[str, Any]]:
    pools: dict[str, dict[str, Any]] = {}
    try:
        from src.observability.redis_pool_callbacks import _live_pools

        for label, pool in _live_pools():
            pools[label] = _snapshot_single_pool(pool)
    except Exception:
        pass
    return pools


def _snapshot_loop_lag() -> dict[str, Any]:
    try:
        from src.observability.loop_lag import EventLoopLagMonitor

        inst = EventLoopLagMonitor._instance
        if inst is not None:
            return inst.snapshot_recent_lag()
    except Exception:
        pass
    return {
        "max_5s_ms": 0.0,
        "max_60s_ms": 0.0,
        "over_300ms_count_60s": 0,
        "last_heartbeat_age_ms": 0.0,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(rendered, encoding="utf-8")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _read_json_epoch(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            raw = loaded.get("leader_epoch", 0)
            val = int(raw or 0)
            return val if val > 0 else 0
    except Exception:
        pass
    return 0


def _claim_leader_epoch(ops_dir: Path, worker_id: str) -> int:
    """Atomically advance and persist the monotonic leader epoch under an exclusive file lock."""
    ops_dir.mkdir(parents=True, exist_ok=True)
    server_path = ops_dir / "redis_diag_server.json"
    epoch_path = ops_dir / SERVER_EPOCH_FILENAME
    lock_path = ops_dir / "redis_diag_server.json.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            current = max(_read_json_epoch(epoch_path), _read_json_epoch(server_path))
            next_epoch = current + 1
            _atomic_write_json(
                epoch_path,
                {
                    "leader_epoch": next_epoch,
                    "leader_worker_id": worker_id,
                    "claimed_at": datetime.now(UTC).isoformat(),
                },
            )
            return next_epoch
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _read_current_leader_epoch(ops_dir: Path) -> int:
    """Read the highest persisted leader epoch under the snapshot file lock."""
    if not ops_dir.is_dir():
        return 0
    server_path = ops_dir / "redis_diag_server.json"
    epoch_path = ops_dir / SERVER_EPOCH_FILENAME
    lock_path = ops_dir / "redis_diag_server.json.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        try:
            return max(_read_json_epoch(epoch_path), _read_json_epoch(server_path))
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _fenced_write_server_snapshot(
    path: Path,
    payload: Mapping[str, Any],
    *,
    expected_epoch: int,
) -> bool:
    """Atomically publish ``redis_diag_server.json`` ONLY if ``expected_epoch`` is still current.

    Prevents a deposed leader whose off-loop write completes late from overwriting
    a newer leader's already-published snapshot.
    """
    if expected_epoch <= 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    epoch_path = path.with_name(SERVER_EPOCH_FILENAME)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            current = max(_read_json_epoch(epoch_path), _read_json_epoch(path))
            if current > 0 and expected_epoch != current:
                return False
            _atomic_write_json(path, payload)
            return True
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _is_timeout_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, redis_exceptions.TimeoutError)):
        return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text


@dataclass(frozen=True)
class _WindowSample:
    ts_monotonic: float
    kind: str  # "op" | "recovery" | "transport_lost" | "error"
    total_ms: float = 0.0
    pool_queue_ms: float = 0.0
    health_check_ms: float = 0.0
    wire_ms: float = 0.0
    payload_bytes: int = 0
    is_slow: bool = False
    is_timeout: bool = False
    is_large_payload: bool = False


class _PerSecondRateLimiter:
    """Simple fixed-1s-bucket rate limiter for per-event diagnostic log lines."""

    def __init__(self, max_per_second: int = SLOW_LOG_MAX_PER_SECOND) -> None:
        self._max_per_second = max(1, int(max_per_second))
        self._bucket_sec: int = -1
        self._count: int = 0
        self.suppressed_total: int = 0

    def allow(self, now: float | None = None) -> bool:
        sec = int(time.monotonic() if now is None else now)
        if sec != self._bucket_sec:
            self._bucket_sec = sec
            self._count = 0
        if self._count < self._max_per_second:
            self._count += 1
            return True
        self.suppressed_total += 1
        return False


class WorkerDiagnosticsRecorder:
    """Per-worker in-memory 5-minute diagnostics window (diagnostics only, never state)."""

    def __init__(
        self,
        *,
        worker_id: str | None = None,
        pid: int | None = None,
        window_seconds: float = WINDOW_SECONDS,
        slow_log_max_per_second: int = SLOW_LOG_MAX_PER_SECOND,
        transport_lost_log_max_per_second: int = TRANSPORT_LOST_LOG_MAX_PER_SECOND,
    ) -> None:
        self.pid = os.getpid() if pid is None else int(pid)
        self.worker_id = worker_id or f"{socket.gethostname()}:{self.pid}"
        self.window_seconds = float(window_seconds)
        self._samples: deque[_WindowSample] = deque(maxlen=8192)
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_EVENTS)
        self._log_limiter = _PerSecondRateLimiter(slow_log_max_per_second)
        self._transport_lost_log_limiter = _PerSecondRateLimiter(transport_lost_log_max_per_second)
        self.transport_lost_total: int = 0
        self.recovered_writes_total: int = 0
        self.active_subagent_streams: int = 0
        self._last_transport_lost_at: str | None = None
        self._edge_callback: Callable[[str], None] | None = None

    def set_edge_callback(self, callback: Callable[[str], None] | None) -> None:
        self._edge_callback = callback

    def _prune(self, now: float) -> None:
        if not self._samples:
            return
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0].ts_monotonic < cutoff:
            self._samples.popleft()

    def _emit_edge(self, reason: str) -> None:
        cb = self._edge_callback
        if cb is not None:
            try:
                cb(reason)
            except Exception:
                pass

    def record_op(
        self,
        timing: RedisOpTiming,
        *,
        total_ms: float,
        exc: BaseException | None = None,
        pool: Any = None,
        now_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        is_slow = total_ms >= SLOW_WRITE_THRESHOLD_MS
        is_error = exc is not None
        is_large = timing.payload_bytes >= LARGE_PAYLOAD_ALERT_BYTES

        # Fast path: healthy sub-500ms writes do zero allocation, zero snapshotting, zero logging.
        if not (is_slow or is_error or is_large):
            return None

        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prune(now)
        is_timeout = _is_timeout_error(exc)

        self._samples.append(
            _WindowSample(
                ts_monotonic=now,
                kind="op",
                total_ms=total_ms,
                pool_queue_ms=timing.pool_queue_ms,
                health_check_ms=timing.health_check_ms,
                wire_ms=timing.wire_ms,
                payload_bytes=timing.payload_bytes,
                is_slow=is_slow,
                is_timeout=is_timeout,
                is_large_payload=is_large,
            )
        )

        pool_snap = _snapshot_single_pool(pool if pool is not None else timing.pool_ref)
        lag_snap = _snapshot_loop_lag()
        outcome = "timeout" if is_timeout else ("error" if is_error else ("slow" if is_slow else "large_payload"))
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "worker_id": self.worker_id,
            "op": timing.op,
            "key_prefix": timing.key_prefix,
            "payload_bytes": int(timing.payload_bytes),
            "is_pipeline": bool(timing.is_pipeline),
            "command_count": int(timing.command_count),
            "total_ms": round(total_ms, 2),
            "pool_queue_ms": round(timing.pool_queue_ms, 2),
            "health_check_ms": round(timing.health_check_ms, 2),
            "acquire_ms": round(timing.acquire_ms, 2),
            "wire_ms": round(timing.wire_ms, 2),
            "pool_in_use": pool_snap["in_use"],
            "pool_available": pool_snap["available"],
            "pool_max": pool_snap["max"],
            "loop_lag_max_5s_ms": lag_snap["max_5s_ms"],
            "last_heartbeat_age_ms": lag_snap["last_heartbeat_age_ms"],
            "outcome": outcome,
            "error_type": type(exc).__name__ if exc is not None else None,
        }
        self._recent_events.append(event)

        if (is_slow or is_error) and self._log_limiter.allow(now):
            logger.warning(
                "[redis-diag] worker_id=%s op=%s key_prefix=%s payload_bytes=%d "
                "is_pipeline=%s cmds=%d total_ms=%.2f pool_queue_ms=%.2f "
                "health_check_ms=%.2f acquire_ms=%.2f wire_ms=%.2f "
                "pool=%d/%d(avail=%d) loop_lag_max_5s_ms=%.2f "
                "last_heartbeat_age_ms=%.2f outcome=%s error_type=%s",
                event["worker_id"],
                event["op"],
                event["key_prefix"],
                event["payload_bytes"],
                event["is_pipeline"],
                event["command_count"],
                event["total_ms"],
                event["pool_queue_ms"],
                event["health_check_ms"],
                event["acquire_ms"],
                event["wire_ms"],
                event["pool_in_use"],
                event["pool_max"],
                event["pool_available"],
                event["loop_lag_max_5s_ms"],
                event["last_heartbeat_age_ms"],
                event["outcome"],
                event["error_type"],
            )
        if is_timeout:
            self._emit_edge("timeout")
        return event

    def record_recovery(
        self,
        key_or_label: str,
        *,
        event_id: int | None = None,
        attempt: int = 1,
        how: str = "",
        payload_bytes: int = 0,
        now_monotonic: float | None = None,
    ) -> None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prune(now)
        self.recovered_writes_total += 1
        self._samples.append(
            _WindowSample(
                ts_monotonic=now,
                kind="recovery",
                payload_bytes=int(payload_bytes),
                is_large_payload=payload_bytes >= LARGE_PAYLOAD_ALERT_BYTES,
            )
        )
        lag_snap = _snapshot_loop_lag()
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "worker_id": self.worker_id,
            "op": "stream_append",
            "key_prefix": sanitize_key_prefix(key_or_label),
            "event_id": event_id,
            "attempt": attempt,
            "via": how,
            "payload_bytes": int(payload_bytes),
            "loop_lag_max_5s_ms": lag_snap["max_5s_ms"],
            "last_heartbeat_age_ms": lag_snap["last_heartbeat_age_ms"],
            "outcome": "recovered",
        }
        self._recent_events.append(event)
        if self._log_limiter.allow(now):
            logger.warning(
                "[redis-diag] worker_id=%s op=stream_append key_prefix=%s "
                "payload_bytes=%d outcome=recovered attempt=%d via=%s "
                "loop_lag_max_5s_ms=%.2f last_heartbeat_age_ms=%.2f",
                event["worker_id"],
                event["key_prefix"],
                event["payload_bytes"],
                attempt,
                how,
                event["loop_lag_max_5s_ms"],
                event["last_heartbeat_age_ms"],
            )

    def record_transport_lost(
        self,
        key_or_label: str,
        *,
        event_id: int | None = None,
        payload_bytes: int = 0,
        exc: BaseException | None = None,
        reason: str = "",
        phase: str = "",
        now_monotonic: float | None = None,
    ) -> None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prune(now)
        self.transport_lost_total += 1
        ts_iso = datetime.now(UTC).isoformat()
        self._last_transport_lost_at = ts_iso
        is_to = _is_timeout_error(exc) or ("timeout" in (reason or "").lower()) or (phase == "timeout")
        self._samples.append(
            _WindowSample(
                ts_monotonic=now,
                kind="transport_lost",
                payload_bytes=int(payload_bytes),
                is_timeout=is_to,
                is_large_payload=payload_bytes >= LARGE_PAYLOAD_ALERT_BYTES,
            )
        )
        lag_snap = _snapshot_loop_lag()
        safe_reason = sanitize_diagnostic_reason(reason, exc=exc, phase=phase)
        event = {
            "ts": ts_iso,
            "worker_id": self.worker_id,
            "op": "stream_append",
            "key_prefix": sanitize_key_prefix(key_or_label),
            "event_id": event_id,
            "payload_bytes": int(payload_bytes),
            "loop_lag_max_5s_ms": lag_snap["max_5s_ms"],
            "last_heartbeat_age_ms": lag_snap["last_heartbeat_age_ms"],
            "outcome": "transport_lost",
            "phase": phase or None,
            "error_type": type(exc).__name__ if exc is not None else None,
            "reason": safe_reason,
        }
        self._recent_events.append(event)
        if self._transport_lost_log_limiter.allow(now):
            logger.warning(
                "[redis-diag] worker_id=%s op=stream_append key_prefix=%s "
                "payload_bytes=%d outcome=transport_lost error_type=%s "
                "loop_lag_max_5s_ms=%.2f last_heartbeat_age_ms=%.2f",
                event["worker_id"],
                event["key_prefix"],
                event["payload_bytes"],
                event["error_type"],
                event["loop_lag_max_5s_ms"],
                event["last_heartbeat_age_ms"],
            )
        self._emit_edge("transport_lost")

    def record_error(
        self,
        context: str,
        exc: BaseException,
        *,
        pool: Any = None,
        now_monotonic: float | None = None,
    ) -> None:
        if _consume_recorded_exc(exc):
            return
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prune(now)
        is_timeout = _is_timeout_error(exc)
        self._samples.append(
            _WindowSample(
                ts_monotonic=now,
                kind="error",
                is_timeout=is_timeout,
            )
        )
        if is_timeout:
            pool_snap = _snapshot_single_pool(pool)
            lag_snap = _snapshot_loop_lag()
            self._recent_events.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "worker_id": self.worker_id,
                    "op": "cache_error",
                    "key_prefix": sanitize_key_prefix(context),
                    "pool_in_use": pool_snap["in_use"],
                    "pool_available": pool_snap["available"],
                    "pool_max": pool_snap["max"],
                    "loop_lag_max_5s_ms": lag_snap["max_5s_ms"],
                    "last_heartbeat_age_ms": lag_snap["last_heartbeat_age_ms"],
                    "outcome": "timeout",
                    "error_type": type(exc).__name__,
                }
            )
            self._emit_edge("timeout")

    def build_snapshot(
        self,
        *,
        now_monotonic: float | None = None,
        now_utc: datetime | None = None,
        pools_override: dict[str, dict[str, Any]] | None = None,
        loop_lag_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prune(now)
        ts = (now_utc or datetime.now(UTC)).astimezone(UTC)

        slow_writes_5m = 0
        timeout_errors_5m = 0
        recovered_writes_5m = 0
        transport_lost_5m = 0
        large_payloads_5m = 0
        max_total_ms = 0.0
        max_pool_queue_ms = 0.0
        max_health_check_ms = 0.0
        max_acquire_ms = 0.0
        max_wire_ms = 0.0
        max_payload_bytes = 0

        for s in tuple(self._samples):
            if s.kind == "op":
                if s.is_slow:
                    slow_writes_5m += 1
                if s.is_timeout:
                    timeout_errors_5m += 1
            elif s.kind == "recovery":
                recovered_writes_5m += 1
            elif s.kind == "transport_lost":
                transport_lost_5m += 1
                if s.is_timeout:
                    timeout_errors_5m += 1
            elif s.kind == "error" and s.is_timeout:
                timeout_errors_5m += 1
            if s.is_large_payload:
                large_payloads_5m += 1
            if s.total_ms > max_total_ms:
                max_total_ms = s.total_ms
            if s.pool_queue_ms > max_pool_queue_ms:
                max_pool_queue_ms = s.pool_queue_ms
            if s.health_check_ms > max_health_check_ms:
                max_health_check_ms = s.health_check_ms
            acq = s.pool_queue_ms + s.health_check_ms
            if acq > max_acquire_ms:
                max_acquire_ms = acq
            if s.wire_ms > max_wire_ms:
                max_wire_ms = s.wire_ms
            if s.payload_bytes > max_payload_bytes:
                max_payload_bytes = s.payload_bytes

        pools = pools_override if pools_override is not None else _snapshot_all_pools()
        loop_lag = loop_lag_override if loop_lag_override is not None else _snapshot_loop_lag()
        return {
            "schema_version": 1,
            "worker_id": self.worker_id,
            "hostname": socket.gethostname(),
            "pid": self.pid,
            "updated_at": ts.isoformat(),
            "window_seconds": int(self.window_seconds),
            "counts": {
                "slow_writes_5m": slow_writes_5m,
                "timeout_errors_5m": timeout_errors_5m,
                "recovered_writes_5m": recovered_writes_5m,
                "transport_lost_5m": transport_lost_5m,
                "transport_lost_total": self.transport_lost_total,
                "recovered_writes_total": self.recovered_writes_total,
                "large_payloads_5m": large_payloads_5m,
                "active_subagent_streams": self.active_subagent_streams,
                "slow_logs_suppressed_total": self._log_limiter.suppressed_total,
                "transport_lost_logs_suppressed_total": self._transport_lost_log_limiter.suppressed_total,
            },
            "last_transport_lost_at": self._last_transport_lost_at,
            "latency_5m": {
                "max_total_ms": round(max_total_ms, 2),
                "max_pool_queue_ms": round(max_pool_queue_ms, 2),
                "max_health_check_ms": round(max_health_check_ms, 2),
                "max_acquire_ms": round(max_acquire_ms, 2),
                "max_wire_ms": round(max_wire_ms, 2),
                "max_payload_bytes": int(max_payload_bytes),
            },
            "pools": pools,
            "loop_lag": loop_lag,
            "recent_events": list(self._recent_events),
        }

    def flush_to_file(
        self,
        ops_dir: Path | None = None,
        *,
        now_utc: datetime | None = None,
    ) -> Path:
        target_dir = ops_dir if ops_dir is not None else get_redis_diag_ops_dir()
        path = target_dir / f"redis_diag_worker_{self.pid}.json"
        _atomic_write_json(path, self.build_snapshot(now_utc=now_utc))
        return path

    async def flush_to_file_async(
        self,
        ops_dir: Path | None = None,
        *,
        now_utc: datetime | None = None,
    ) -> Path:
        """Build snapshot in memory and offload JSON serialization + atomic write to a worker thread."""
        target_dir = ops_dir if ops_dir is not None else get_redis_diag_ops_dir()
        path = target_dir / f"redis_diag_worker_{self.pid}.json"
        snapshot = self.build_snapshot(now_utc=now_utc)
        await asyncio.to_thread(_atomic_write_json, path, snapshot)
        return path


_DEFAULT_RECORDER: WorkerDiagnosticsRecorder | None = None


def get_worker_diagnostics_recorder() -> WorkerDiagnosticsRecorder:
    global _DEFAULT_RECORDER
    if _DEFAULT_RECORDER is None:
        _DEFAULT_RECORDER = WorkerDiagnosticsRecorder()
    return _DEFAULT_RECORDER


def reset_worker_diagnostics_recorder_for_tests() -> WorkerDiagnosticsRecorder:
    global _DEFAULT_RECORDER
    _DEFAULT_RECORDER = WorkerDiagnosticsRecorder()
    return _DEFAULT_RECORDER


@contextmanager
def redis_op_span(
    op: str,
    key: Any,
    *,
    payload_bytes: int = 0,
    is_pipeline: bool = False,
    command_count: int = 1,
    pool: Any = None,
    recorder: WorkerDiagnosticsRecorder | None = None,
) -> Iterator[RedisOpTiming | None]:
    """Measure one Redis command or pipeline across pool queue, health check, and pure wire I/O."""
    if not is_redis_diagnostics_enabled():
        yield None
        return

    timing = RedisOpTiming(
        op=op,
        key_prefix=sanitize_key_prefix(key),
        payload_bytes=max(0, int(payload_bytes)),
        is_pipeline=bool(is_pipeline),
        command_count=max(1, int(command_count)),
        pool_ref=pool,
    )
    token = _CURRENT_OP_TIMING.set(timing)
    caught: BaseException | None = None
    try:
        yield timing
    except BaseException as exc:
        caught = exc
        raise
    finally:
        _CURRENT_OP_TIMING.reset(token)
        total_ms = (time.monotonic() - timing.started_monotonic) * 1000.0
        effective_exc = caught
        if isinstance(caught, asyncio.CancelledError):
            active_to = _ACTIVE_DIAG_TIMEOUT.get()
            if active_to is not None and active_to.expired():
                effective_exc = TimeoutError("outer attempt timeout expired")
        try:
            rec = recorder or get_worker_diagnostics_recorder()
            rec.record_op(timing, total_ms=total_ms, exc=effective_exc, pool=pool)
            if caught is not None:
                _mark_exc_recorded(caught)
                if effective_exc is not None and effective_exc is not caught:
                    _mark_exc_recorded(effective_exc)
        except Exception:
            pass


@contextmanager
def redis_event_buffer_span(
    stream_key: str,
    payload: bytes,
    record: bytes | None,
    reset_epoch: bool,
    heal_retention: bool,
    ttl: int | None,
    pool: Any,
) -> Iterator[RedisOpTiming | None]:
    """Thin upstream helper for ``RedisCacheClient.pipelined_event_buffer``."""
    if not is_redis_diagnostics_enabled():
        yield None
        return
    is_pipe = bool(reset_epoch or heal_retention or ttl is not None)
    cmd_cnt = (1 + int(reset_epoch) + int(ttl is not None or heal_retention)) if is_pipe else 1
    total_bytes = len(payload) + (len(record) if record is not None else 0)
    tok = _CURRENT_STREAM_PAYLOAD_BYTES.set(total_bytes)
    try:
        with redis_op_span(
            "pipeline_xadd" if is_pipe else "xadd",
            stream_key,
            payload_bytes=total_bytes,
            is_pipeline=is_pipe,
            command_count=cmd_cnt,
            pool=pool,
        ) as timing:
            yield timing
    finally:
        _CURRENT_STREAM_PAYLOAD_BYTES.reset(tok)


@contextmanager
def redis_v2_xadd_span(
    cache: Any,
    v2_key: str,
    payload_bytes: int,
    *,
    attempt: int = 1,
    seq: int | None = None,
) -> Iterator[RedisOpTiming | None]:
    """Thin upstream helper for subagent v2 XADD: splits wire latency and records retry recoveries."""
    if not is_redis_diagnostics_enabled():
        yield None
        return
    succeeded = False
    with redis_op_span(
        "subagent_v2_xadd",
        v2_key,
        payload_bytes=payload_bytes,
        pool=getattr(cache, "pool", None),
    ) as timing:
        yield timing
        succeeded = True
    if succeeded and attempt > 1:
        record_stream_recovery(
            v2_key,
            event_id=seq,
            attempt=attempt,
            how="v2_retry",
            payload_bytes=payload_bytes,
        )


@contextmanager
def track_subagent_stream(
    recorder: WorkerDiagnosticsRecorder | None = None,
) -> Iterator[None]:
    """Track in-flight subagent stream spill concurrency on the current worker."""
    if not is_redis_diagnostics_enabled():
        yield
        return
    rec = recorder or get_worker_diagnostics_recorder()
    rec.active_subagent_streams += 1
    try:
        yield
    finally:
        rec.active_subagent_streams = max(0, rec.active_subagent_streams - 1)


def record_stream_recovery(
    key_or_label: str,
    *,
    event_id: int | None = None,
    attempt: int = 1,
    how: str = "",
    payload_bytes: int | None = None,
) -> None:
    if not is_redis_diagnostics_enabled():
        return
    try:
        resolved_bytes = (
            int(payload_bytes)
            if payload_bytes is not None
            else int(_CURRENT_STREAM_PAYLOAD_BYTES.get() or 0)
        )
        get_worker_diagnostics_recorder().record_recovery(
            key_or_label,
            event_id=event_id,
            attempt=attempt,
            how=how,
            payload_bytes=resolved_bytes,
        )
    except Exception:
        pass


def observe_stream_append(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Thin decorator for ``stream_append_with_retry``: tracks payload length (zero re-encode) and fatal errors."""

    @functools.wraps(fn)
    async def _wrapped(
        cache: Any,
        stream_key: str,
        *,
        event_id: int,
        max_size: int,
        stream_event: str | bytes,
        stream_record: str | bytes | None = None,
        label: str = "",
    ) -> Any:
        if not is_redis_diagnostics_enabled():
            return await fn(
                cache,
                stream_key,
                event_id=event_id,
                max_size=max_size,
                stream_event=stream_event,
                stream_record=stream_record,
                label=label,
            )
        # O(1) length estimation — never re-encode stream_event/stream_record on the hot path.
        est_bytes = estimate_serialized_bytes(stream_event) + (
            0 if stream_record is None else estimate_serialized_bytes(stream_record)
        )
        tok = _CURRENT_STREAM_PAYLOAD_BYTES.set(est_bytes)
        try:
            return await fn(
                cache,
                stream_key,
                event_id=event_id,
                max_size=max_size,
                stream_event=stream_event,
                stream_record=stream_record,
                label=label,
            )
        except Exception as exc:
            actual_bytes = int(_CURRENT_STREAM_PAYLOAD_BYTES.get() or est_bytes)
            record_stream_transport_lost(
                stream_key,
                event_id=event_id,
                payload_bytes=actual_bytes,
                exc=exc,
                phase="stream_append",
            )
            raise
        finally:
            _CURRENT_STREAM_PAYLOAD_BYTES.reset(tok)

    return _wrapped


def observe_subagent_spill(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Thin decorator tracking active subagent stream spill concurrency on this worker."""

    @functools.wraps(fn)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with track_subagent_stream():
            return await fn(*args, **kwargs)

    return _wrapped


def record_subagent_spill_lost(
    task_id: str,
    *,
    phase: str,
    exc: BaseException | None = None,
    seq: int | None = None,
) -> None:
    """Thin upstream helper for ``_open_spill_circuit``: records v2/timeout circuit opens without leaking keys."""
    if not is_redis_diagnostics_enabled():
        return
    if phase not in ("v2_pipeline", "timeout", "exception"):
        return
    record_stream_transport_lost(
        f"subagent:stream:{task_id}",
        event_id=seq,
        exc=exc,
        reason=phase,
        phase=phase,
    )


def record_stream_transport_lost(
    key_or_label: str,
    *,
    event_id: int | None = None,
    payload_bytes: int = 0,
    exc: BaseException | None = None,
    reason: str = "",
    phase: str = "",
) -> None:
    if not is_redis_diagnostics_enabled():
        return
    try:
        get_worker_diagnostics_recorder().record_transport_lost(
            key_or_label,
            event_id=event_id,
            payload_bytes=payload_bytes,
            exc=exc,
            reason=reason,
            phase=phase,
        )
    except Exception:
        pass


def record_redis_error(context: str, exc: BaseException, *, pool: Any = None) -> None:
    if not is_redis_diagnostics_enabled():
        return
    try:
        get_worker_diagnostics_recorder().record_error(context, exc, pool=pool)
    except Exception:
        pass


def _decode_str(val: Any) -> str:
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _sanitize_slowlog_entries(entries: Any) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    if not isinstance(entries, (list, tuple)):
        return sanitized
    for item in entries[:5]:
        try:
            if isinstance(item, Mapping):
                entry_id = int(item.get("id", 0))
                start_time = int(item.get("start_time", 0))
                duration_us = int(item.get("duration", 0))
                raw_cmd = item.get("command", b"")
                if isinstance(raw_cmd, (bytes, bytearray)):
                    parts = _decode_str(raw_cmd).split()
                elif isinstance(raw_cmd, (list, tuple)):
                    parts = [_decode_str(p) for p in raw_cmd]
                else:
                    parts = str(raw_cmd).split()
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                entry_id = int(item[0])
                start_time = int(item[1])
                duration_us = int(item[2])
                raw_parts = item[3] if isinstance(item[3], (list, tuple)) else [item[3]]
                parts = [_decode_str(p) for p in raw_parts]
            else:
                continue
            cmd_name = (parts[0].upper() if parts else "UNKNOWN")[:32]
            key_prefix = sanitize_key_prefix(parts[1]) if len(parts) > 1 else ""
            sanitized.append(
                {
                    "id": entry_id,
                    "start_time": start_time,
                    "duration_us": duration_us,
                    "duration_ms": round(duration_us / 1000.0, 2),
                    "command": cmd_name,
                    "key_prefix": key_prefix,
                }
            )
        except Exception:
            continue
    return sanitized


def _parse_latency_latest(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 4:
                out.append(
                    {
                        "event": _decode_str(item[0]),
                        "timestamp": int(item[1]),
                        "latest_ms": int(item[2]),
                        "max_ms": int(item[3]),
                    }
                )
        except Exception:
            continue
    return out


class RedisDiagnosticsLeaderSampler:
    """Single-leader Redis server metrics sampler backed by one dedicated PG advisory lock.

    - Holds one non-pooled ``psycopg.AsyncConnection`` opened via
      ``src.server.database.pool.open_session_connection()`` (same DB_* / DB_SSLMODE
      config across Everflow and core deployments) and contends for
      ``pg_try_advisory_lock(hashtextextended('dalton:diag:redis_sampler_leader', 0))``.
    - Uses its own dedicated standalone diagnostic Redis client (1s timeout, never
      borrowing from the business cache pool) so sampling cannot contend with writes.
    - Re-verifies leadership via ``SELECT 1`` before every off-loop write to
      ``redis_diag_server.json``.
    """

    def __init__(
        self,
        *,
        worker_id: str | None = None,
        ops_dir: Path | None = None,
        pg_dsn: str | None = None,
        pg_connect_fn: Callable[[], Any] | None = None,
        redis_url: str | None = None,
        redis_client_getter: Callable[[], Any] | None = None,
        redis_sample_timeout_s: float = SERVER_SAMPLE_REDIS_TIMEOUT_S,
        lock_name: str = LEADER_LOCK_NAME,
    ) -> None:
        self.worker_id = worker_id or get_worker_id()
        self._ops_dir = ops_dir
        self._pg_dsn = pg_dsn
        self._pg_connect_fn = pg_connect_fn
        self._redis_url = redis_url
        self._redis_client_getter = redis_client_getter
        self._redis_sample_timeout_s = float(redis_sample_timeout_s)
        self._lock_name = lock_name
        self._lock_conn: Any = None
        self._is_leader: bool = False
        self._leader_epoch: int = 0
        self._diag_redis: Any = None
        self._pg_connect_failed_logged: bool = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def leader_epoch(self) -> int:
        return self._leader_epoch

    def _is_eligible_for_leadership(self) -> bool:
        """Workers with a disabled business Redis client must not run for or hold leadership."""
        if self._redis_client_getter is not None or self._redis_url:
            return True
        try:
            from src.utils.cache.redis_cache import get_cache_client

            cache = get_cache_client()
            return bool(getattr(cache, "enabled", False))
        except Exception:
            return False

    def _resolve_ops_dir(self) -> Path:
        return self._ops_dir if self._ops_dir is not None else get_redis_diag_ops_dir()

    async def _open_lock_connection(self) -> Any:
        if self._pg_connect_fn is not None:
            return await self._pg_connect_fn()
        if self._pg_dsn:
            from src.server.database.pool import _CONNECTION_KWARGS

            return await psycopg.AsyncConnection.connect(
                self._pg_dsn,
                autocommit=True,
                prepare_threshold=None,
                **_CONNECTION_KWARGS,
            )
        from src.server.database.pool import open_session_connection

        return await open_session_connection()

    async def _close_diag_redis(self) -> None:
        client, self._diag_redis = self._diag_redis, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _drop_lock_connection(self, *, unlock: bool = False) -> None:
        conn, self._lock_conn = self._lock_conn, None
        was_leader, self._is_leader = self._is_leader, False
        self._leader_epoch = 0
        await self._close_diag_redis()
        if conn is None:
            return
        if not bool(getattr(conn, "closed", False)):
            if unlock and was_leader:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(LEADER_LOCK_RELEASE_SQL, (self._lock_name,))
                        await cur.fetchone()
                except Exception:
                    pass
            try:
                await conn.close()
            except Exception:
                pass

    async def verify_leader_alive(self) -> bool:
        """Confirm this PG session still holds the advisory lock and owns the current leader epoch."""
        conn = self._lock_conn
        if (
            not self._is_leader
            or self._leader_epoch <= 0
            or conn is None
            or bool(getattr(conn, "closed", False))
        ):
            self._is_leader = False
            self._leader_epoch = 0
            return False
        try:
            async with conn.cursor() as cur:
                await cur.execute(LEADER_LOCK_VERIFY_SQL, (self._lock_name,))
                row = await cur.fetchone()
                held = False
                if row is not None:
                    held = bool(
                        next(iter(row.values()), False) if isinstance(row, Mapping) else row[0]
                    )
                if not held:
                    await self._drop_lock_connection(unlock=False)
                    return False
        except Exception:
            await self._drop_lock_connection(unlock=False)
            return False

        current_epoch = await asyncio.to_thread(_read_current_leader_epoch, self._resolve_ops_dir())
        if current_epoch > 0 and current_epoch != self._leader_epoch:
            await self._drop_lock_connection(unlock=False)
            return False
        return True

    async def ensure_leadership(self) -> bool:
        """Maintain or attempt to acquire the single PostgreSQL session advisory lock."""
        if not self._is_eligible_for_leadership():
            if self._is_leader or self._lock_conn is not None:
                await self._drop_lock_connection(unlock=True)
            return False

        if self._is_leader:
            if await self.verify_leader_alive():
                return True

        if self._lock_conn is None or bool(getattr(self._lock_conn, "closed", False)):
            try:
                self._lock_conn = await self._open_lock_connection()
                if self._pg_connect_failed_logged:
                    logger.info("[redis-diag] dedicated PG lock connection recovered")
                    self._pg_connect_failed_logged = False
            except Exception as exc:
                if not self._pg_connect_failed_logged:
                    logger.warning(
                        "[redis-diag] failed to open dedicated PG lock connection: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    self._pg_connect_failed_logged = True
                else:
                    logger.debug(
                        "[redis-diag] dedicated PG lock connection retry failed: %s",
                        exc,
                    )
                self._lock_conn = None
                self._is_leader = False
                self._leader_epoch = 0
                return False

        try:
            async with self._lock_conn.cursor() as cur:
                await cur.execute(LEADER_LOCK_ACQUIRE_SQL, (self._lock_name,))
                row = await cur.fetchone()
            acquired = False
            if row is not None:
                acquired = bool(
                    next(iter(row.values()), False) if isinstance(row, Mapping) else row[0]
                )
            if not acquired:
                self._is_leader = False
                self._leader_epoch = 0
                return False
            epoch = await asyncio.to_thread(
                _claim_leader_epoch,
                self._resolve_ops_dir(),
                self.worker_id,
            )
            self._is_leader = True
            self._leader_epoch = epoch
            return True
        except Exception as exc:
            logger.warning("[redis-diag] PG leader advisory lock check failed: %s", exc)
            await self._drop_lock_connection(unlock=False)
            return False

    def _resolve_diagnostic_redis_url(self) -> str | None:
        if self._redis_url:
            return self._redis_url
        try:
            from src.utils.cache.redis_cache import get_cache_client

            cache = get_cache_client()
            if not getattr(cache, "enabled", False):
                return None
            cache_url = getattr(cache, "url", None)
            if cache_url:
                return str(cache_url)
        except Exception:
            pass
        try:
            from src.config.settings import get_nested_config, is_redis_cache_enabled

            cache_enabled_env = os.getenv("REDIS_CACHE_ENABLED", "true").lower() in (
                "true",
                "1",
                "yes",
            )
            if not (is_redis_cache_enabled() and cache_enabled_env):
                return None
            cfg_url = get_nested_config("redis.url")
            if cfg_url:
                return str(cfg_url)
        except Exception:
            pass
        return os.getenv("REDIS_URL", "redis://localhost:6379/0")

    def _get_diagnostic_redis_client(self) -> Any:
        """Return a dedicated standalone Redis client (never borrowing from the business pool)."""
        if self._redis_client_getter is not None:
            return self._redis_client_getter()
        if self._diag_redis is not None:
            return self._diag_redis
        url = self._resolve_diagnostic_redis_url()
        if not url:
            return None
        self._diag_redis = redis_async.Redis.from_url(
            url,
            max_connections=1,
            socket_timeout=self._redis_sample_timeout_s,
            socket_connect_timeout=self._redis_sample_timeout_s,
            decode_responses=False,
            health_check_interval=0,
        )
        return self._diag_redis

    async def _collect_redis_server_metrics(self, client: Any) -> dict[str, Any]:
        async with asyncio.timeout(self._redis_sample_timeout_s):
            persistence = await client.info("persistence")
            memory = await client.info("memory")
            stats = await client.info("stats")
            clients_info = await client.info("clients")
            try:
                raw_slowlog = await client.execute_command("SLOWLOG", "GET", 5)
            except Exception:
                raw_slowlog = []
            try:
                raw_latency = await client.execute_command("LATENCY", "LATEST")
            except Exception:
                raw_latency = []

        slowlog = _sanitize_slowlog_entries(raw_slowlog)
        latency_latest = _parse_latency_latest(raw_latency)
        return {
            "persistence": {
                "rdb_bgsave_in_progress": int(persistence.get("rdb_bgsave_in_progress", 0) or 0),
                "rdb_last_bgsave_status": str(persistence.get("rdb_last_bgsave_status", "") or ""),
                "rdb_last_bgsave_time_sec": int(persistence.get("rdb_last_bgsave_time_sec", -1)),
                "rdb_changes_since_last_save": int(
                    persistence.get("rdb_changes_since_last_save", 0) or 0
                ),
                "aof_enabled": int(persistence.get("aof_enabled", 0) or 0),
                "aof_rewrite_in_progress": int(
                    persistence.get("aof_rewrite_in_progress", 0) or 0
                ),
            },
            "memory": {
                "used_memory": int(memory.get("used_memory", 0) or 0),
                "used_memory_rss": int(memory.get("used_memory_rss", 0) or 0),
                "maxmemory": int(memory.get("maxmemory", 0) or 0),
                "mem_fragmentation_ratio": float(
                    memory.get("mem_fragmentation_ratio", 0.0) or 0.0
                ),
            },
            "stats": {
                "instantaneous_ops_per_sec": int(
                    stats.get("instantaneous_ops_per_sec", 0) or 0
                ),
                "latest_fork_usec": int(stats.get("latest_fork_usec", 0) or 0),
                "rejected_connections": int(stats.get("rejected_connections", 0) or 0),
                "evicted_keys": int(stats.get("evicted_keys", 0) or 0),
            },
            "clients": {
                "connected_clients": int(clients_info.get("connected_clients", 0) or 0),
                "blocked_clients": int(clients_info.get("blocked_clients", 0) or 0),
            },
            "slowlog": slowlog,
            "latency_latest": latency_latest,
        }

    async def sample_once(
        self,
        *,
        trigger: str = "periodic",
        now_utc: datetime | None = None,
    ) -> dict[str, Any] | None:
        """If this worker holds the single PG advisory lock, sample Redis and write server JSON."""
        if not await self.ensure_leadership():
            return None

        client = self._get_diagnostic_redis_client()
        if client is None:
            # Business Redis client is disabled -> release the PG leader lock immediately
            # so a healthy worker can take over sampling.
            await self._drop_lock_connection(unlock=True)
            return None

        epoch = self._leader_epoch
        payload: dict[str, Any]
        t0 = time.monotonic()
        try:
            metrics = await self._collect_redis_server_metrics(client)
            sample_ms = round((time.monotonic() - t0) * 1000.0, 2)
            write_ts = (now_utc or datetime.now(UTC)).astimezone(UTC)
            ts_iso = write_ts.isoformat()
            payload = {
                "schema_version": 1,
                "leader_epoch": epoch,
                "written_at": ts_iso,
                "updated_at": ts_iso,
                "leader_worker_id": self.worker_id,
                "trigger": trigger,
                "status": "ok",
                "sample_duration_ms": sample_ms,
                "error_type": None,
                "error": None,
                **metrics,
            }
        except Exception as exc:
            sample_ms = round((time.monotonic() - t0) * 1000.0, 2)
            status = "timeout" if _is_timeout_error(exc) else "error"
            if self._redis_client_getter is None:
                # Drop the standalone diagnostic connection on failure so a wedged socket is replaced cleanly.
                await self._close_diag_redis()
            write_ts = (now_utc or datetime.now(UTC)).astimezone(UTC)
            ts_iso = write_ts.isoformat()
            payload = {
                "schema_version": 1,
                "leader_epoch": epoch,
                "written_at": ts_iso,
                "updated_at": ts_iso,
                "leader_worker_id": self.worker_id,
                "trigger": trigger,
                "status": status,
                "sample_duration_ms": sample_ms,
                "error_type": type(exc).__name__,
                "error": sanitize_diagnostic_reason(str(exc), exc=exc),
            }

        # Re-verify business Redis eligibility and PG session lock + leader epoch before writing.
        if not self._is_eligible_for_leadership():
            await self._drop_lock_connection(unlock=True)
            return None
        if not await self.verify_leader_alive() or self._leader_epoch != epoch:
            return None

        out_path = self._resolve_ops_dir() / "redis_diag_server.json"
        wrote = await asyncio.to_thread(
            _fenced_write_server_snapshot,
            out_path,
            payload,
            expected_epoch=epoch,
        )
        if not wrote:
            await self._drop_lock_connection(unlock=False)
            return None
        return payload

    async def close(self) -> None:
        await self._drop_lock_connection(unlock=True)


class RedisDiagnosticsService:
    """Lifespan-managed per-worker off-loop flush loop + PG-elected single-leader server sampler."""

    _instance: Optional["RedisDiagnosticsService"] = None

    def __init__(
        self,
        *,
        recorder: WorkerDiagnosticsRecorder | None = None,
        sampler: RedisDiagnosticsLeaderSampler | None = None,
        ops_dir: Path | None = None,
        flush_interval_s: float = WORKER_FLUSH_INTERVAL_S,
        server_sample_interval_s: float = SERVER_SAMPLE_INTERVAL_S,
        edge_sample_cooldown_s: float = EDGE_SERVER_SAMPLE_COOLDOWN_S,
    ) -> None:
        self._recorder = recorder or get_worker_diagnostics_recorder()
        self._ops_dir = ops_dir
        self._sampler = sampler or RedisDiagnosticsLeaderSampler(
            worker_id=self._recorder.worker_id,
            ops_dir=ops_dir,
        )
        self._flush_interval_s = float(flush_interval_s)
        self._server_sample_interval_s = float(server_sample_interval_s)
        self._edge_sample_cooldown_s = float(edge_sample_cooldown_s)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._edge_event = asyncio.Event()
        self._last_edge_flush_at: float = 0.0
        self._last_edge_server_sample_at: float = 0.0

    @classmethod
    def get_instance(cls) -> "RedisDiagnosticsService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def notify_edge(self, _reason: str = "") -> None:
        if self._task is None or self._task.done():
            return
        self._edge_event.set()

    def start(self) -> None:
        if not is_redis_diagnostics_enabled():
            logger.info("[redis-diag] disabled via DALTON_REDIS_DIAG_ENABLED")
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._edge_event.clear()
        self._recorder.set_edge_callback(self.notify_edge)
        self._task = asyncio.create_task(self._run(), name="dalton-redis-diagnostics")
        logger.info("[redis-diag] worker diagnostics & PG leader sampler started")

    async def stop(self) -> None:
        self._stopping.set()
        self._edge_event.set()
        self._recorder.set_edge_callback(None)
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_join(task)
        try:
            await self._sampler.close()
        except Exception:
            pass

    async def _run(self) -> None:
        next_periodic = 0.0
        while not self._stopping.is_set():
            if not is_redis_diagnostics_enabled():
                # Re-check kill switch dynamically: release leader lock and sleep until re-enabled or stopped.
                if self._sampler.is_leader or getattr(self._sampler, "_lock_conn", None) is not None:
                    try:
                        await self._sampler.close()
                    except Exception:
                        pass
                self._edge_event.clear()
                wait_disabled = max(0.2, min(self._flush_interval_s, 5.0))
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=wait_disabled)
                except asyncio.TimeoutError:
                    pass
                continue

            now = time.monotonic()
            triggered_by_edge = False
            if now >= next_periodic:
                try:
                    await self._recorder.flush_to_file_async(self._ops_dir)
                except Exception:
                    logger.debug("[redis-diag] worker flush failed", exc_info=True)
                try:
                    await self._sampler.sample_once(trigger="periodic")
                except Exception:
                    logger.debug("[redis-diag] server sample failed", exc_info=True)
                next_periodic = time.monotonic() + min(
                    self._flush_interval_s, self._server_sample_interval_s
                )
            elif self._edge_event.is_set():
                self._edge_event.clear()
                triggered_by_edge = True
                if (now - self._last_edge_flush_at) >= EDGE_FLUSH_MIN_INTERVAL_S:
                    self._last_edge_flush_at = now
                    try:
                        await self._recorder.flush_to_file_async(self._ops_dir)
                    except Exception:
                        logger.debug("[redis-diag] edge worker flush failed", exc_info=True)
                if (now - self._last_edge_server_sample_at) >= self._edge_sample_cooldown_s:
                    self._last_edge_server_sample_at = now
                    try:
                        await self._sampler.sample_once(trigger="edge")
                    except Exception:
                        logger.debug("[redis-diag] edge server sample failed", exc_info=True)

            if self._stopping.is_set():
                break
            wait_s = max(0.2, next_periodic - time.monotonic())
            if triggered_by_edge:
                wait_s = min(wait_s, EDGE_FLUSH_MIN_INTERVAL_S)
            try:
                await asyncio.wait_for(self._edge_event.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def evaluate_cluster_alerts(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Evaluate cluster-wide P1 / P2 / P3 Redis diagnostics alerts from an aggregated summary."""
    alerts: list[dict[str, Any]] = []
    cluster = summary.get("cluster") or {}
    server = summary.get("server") or {}

    transport_lost_5m = int(cluster.get("total_transport_lost_5m", 0) or 0)
    if transport_lost_5m >= 1:
        alerts.append(
            {
                "severity": "P1",
                "code": "redis_diag_p1",
                "reason": "transport_lost",
                "detail": (
                    f"集群 5 分钟内发生 transport_lost={transport_lost_5m} 次"
                    f"（活跃 worker={cluster.get('active_worker_count', 0)}，"
                    f"max_wire_ms={cluster.get('max_wire_ms', 0)}，"
                    f"max_acquire_ms={cluster.get('max_acquire_ms', 0)}，"
                    f"max_loop_lag_5s_ms={cluster.get('max_loop_lag_5s_ms', 0)}）"
                ),
            }
        )

    p2_reasons: list[str] = []
    recovered_5m = int(cluster.get("total_recovered_writes_5m", 0) or 0)
    if recovered_5m >= P2_RECOVERED_5M_THRESHOLD:
        p2_reasons.append(f"recovered_5m={recovered_5m}")
    max_pool_util = float(cluster.get("max_pool_utilization", 0.0) or 0.0)
    if max_pool_util >= P2_POOL_UTILIZATION_THRESHOLD:
        p2_reasons.append(f"pool_util={int(round(max_pool_util * 100))}%")
    max_lag_ms = max(
        float(cluster.get("max_loop_lag_5s_ms", 0.0) or 0.0),
        float(cluster.get("max_loop_lag_60s_ms", 0.0) or 0.0),
        float(cluster.get("max_heartbeat_age_ms", 0.0) or 0.0),
    )
    if max_lag_ms >= P2_LOOP_LAG_MS_THRESHOLD:
        p2_reasons.append(f"loop_lag_ms={round(max_lag_ms, 1)}")
    latest_fork_usec = int(((server.get("stats") or {}).get("latest_fork_usec", 0)) or 0)
    if latest_fork_usec >= P2_LATEST_FORK_USEC_THRESHOLD:
        p2_reasons.append(f"latest_fork_usec={latest_fork_usec}")
    slowlog_entries = server.get("slowlog") or []
    slow_over_100ms = [
        e
        for e in slowlog_entries
        if isinstance(e, Mapping) and int(e.get("duration_us", 0) or 0) >= P2_SLOWLOG_US_THRESHOLD
    ]
    if slow_over_100ms:
        max_slow_ms = max(
            round(int(e.get("duration_us", 0) or 0) / 1000.0, 1) for e in slow_over_100ms
        )
        p2_reasons.append(f"slowlog_ge_100ms={len(slow_over_100ms)}(max={max_slow_ms}ms)")
    if p2_reasons:
        alerts.append(
            {
                "severity": "P2",
                "code": "redis_diag_p2",
                "reason": ",".join(p2_reasons),
                "detail": "Redis 写入链路退化：" + "，".join(p2_reasons),
            }
        )

    max_payload_bytes = int(cluster.get("max_payload_bytes_5m", 0) or 0)
    if max_payload_bytes >= LARGE_PAYLOAD_ALERT_BYTES:
        alerts.append(
            {
                "severity": "P3",
                "code": "redis_diag_p3",
                "reason": "large_payload",
                "detail": f"检测到超大单帧载荷 payload_bytes={max_payload_bytes}（阈值 {LARGE_PAYLOAD_ALERT_BYTES}）",
            }
        )

    return alerts


def aggregate_redis_diagnostics(
    ops_dir: Path | None = None,
    *,
    now: datetime | None = None,
    stale_worker_seconds: float = STALE_WORKER_SECONDS,
    stale_server_seconds: float = STALE_SERVER_SECONDS,
    prune_stale_workers: bool = False,
    include_live_worker: bool = False,
) -> dict[str, Any]:
    """Merge ``redis_diag_server.json`` and active ``redis_diag_worker_*.json`` files."""
    target_dir = ops_dir if ops_dir is not None else get_redis_diag_ops_dir()
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)

    server_data: dict[str, Any] | None = None
    server_path = target_dir / "redis_diag_server.json"
    if server_path.is_file():
        try:
            loaded = json.loads(server_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                written_dt = _parse_iso_utc(loaded.get("written_at") or loaded.get("updated_at"))
                if written_dt is not None:
                    server_age_s = (now_dt - written_dt).total_seconds()
                    if server_age_s <= stale_server_seconds:
                        server_data = loaded
        except Exception:
            server_data = None

    workers_by_id: dict[str, dict[str, Any]] = {}
    pruned_files: list[str] = []
    if target_dir.is_dir():
        for wf in sorted(target_dir.glob("redis_diag_worker_*.json")):
            try:
                loaded = json.loads(wf.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("not a dict")
                updated_dt = _parse_iso_utc(loaded.get("updated_at"))
                if updated_dt is None:
                    raise ValueError("missing updated_at")
                age_s = (now_dt - updated_dt).total_seconds()
                if age_s > stale_worker_seconds:
                    if prune_stale_workers:
                        try:
                            wf.unlink()
                            pruned_files.append(wf.name)
                        except OSError:
                            pass
                    continue
                wid = str(loaded.get("worker_id") or wf.stem)
                workers_by_id[wid] = loaded
            except Exception:
                if prune_stale_workers:
                    try:
                        age_mtime = now_dt.timestamp() - wf.stat().st_mtime
                        if age_mtime > stale_worker_seconds:
                            wf.unlink()
                            pruned_files.append(wf.name)
                    except OSError:
                        pass

    if include_live_worker:
        live = get_worker_diagnostics_recorder().build_snapshot(now_utc=now_dt)
        workers_by_id[str(live["worker_id"])] = live

    workers = sorted(workers_by_id.values(), key=lambda item: str(item.get("worker_id") or ""))

    total_slow_writes_5m = 0
    total_timeout_errors_5m = 0
    total_recovered_writes_5m = 0
    total_transport_lost_5m = 0
    total_transport_lost_ever = 0
    total_large_payloads_5m = 0
    total_active_subagent_streams = 0
    max_total_ms = 0.0
    max_pool_queue_ms = 0.0
    max_health_check_ms = 0.0
    max_acquire_ms = 0.0
    max_wire_ms = 0.0
    max_payload_bytes = 0
    max_pool_util = 0.0
    max_pool_util_name: str | None = None
    max_lag_5s = 0.0
    max_lag_60s = 0.0
    max_hb_age = 0.0
    latest_transport_lost_at = ""

    for w in workers:
        counts = w.get("counts") or {}
        lat = w.get("latency_5m") or {}
        lag = w.get("loop_lag") or {}
        pools = w.get("pools") or {}

        total_slow_writes_5m += int(counts.get("slow_writes_5m", 0) or 0)
        total_timeout_errors_5m += int(counts.get("timeout_errors_5m", 0) or 0)
        total_recovered_writes_5m += int(counts.get("recovered_writes_5m", 0) or 0)
        total_transport_lost_5m += int(counts.get("transport_lost_5m", 0) or 0)
        total_transport_lost_ever += int(counts.get("transport_lost_total", 0) or 0)
        total_large_payloads_5m += int(counts.get("large_payloads_5m", 0) or 0)
        total_active_subagent_streams += int(counts.get("active_subagent_streams", 0) or 0)

        tl_at = str(w.get("last_transport_lost_at") or "")
        if tl_at and tl_at > latest_transport_lost_at:
            latest_transport_lost_at = tl_at

        max_total_ms = max(max_total_ms, float(lat.get("max_total_ms", 0.0) or 0.0))
        max_pool_queue_ms = max(
            max_pool_queue_ms, float(lat.get("max_pool_queue_ms", 0.0) or 0.0)
        )
        max_health_check_ms = max(
            max_health_check_ms, float(lat.get("max_health_check_ms", 0.0) or 0.0)
        )
        max_acquire_ms = max(max_acquire_ms, float(lat.get("max_acquire_ms", 0.0) or 0.0))
        max_wire_ms = max(max_wire_ms, float(lat.get("max_wire_ms", 0.0) or 0.0))
        max_payload_bytes = max(
            max_payload_bytes, int(lat.get("max_payload_bytes", 0) or 0)
        )

        max_lag_5s = max(max_lag_5s, float(lag.get("max_5s_ms", 0.0) or 0.0))
        max_lag_60s = max(max_lag_60s, float(lag.get("max_60s_ms", 0.0) or 0.0))
        max_hb_age = max(max_hb_age, float(lag.get("last_heartbeat_age_ms", 0.0) or 0.0))

        for pool_name, pool_stats in pools.items():
            if not isinstance(pool_stats, Mapping):
                continue
            util = float(pool_stats.get("utilization", 0.0) or 0.0)
            if util > max_pool_util:
                max_pool_util = util
                max_pool_util_name = f"{w.get('worker_id')}:{pool_name}"

    summary: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now_dt.isoformat(),
        "enabled": is_redis_diagnostics_enabled(),
        "stale_worker_seconds": int(stale_worker_seconds),
        "stale_server_seconds": int(stale_server_seconds),
        "pruned_worker_files": pruned_files,
        "cluster": {
            "active_worker_count": len(workers),
            "total_slow_writes_5m": total_slow_writes_5m,
            "total_timeout_errors_5m": total_timeout_errors_5m,
            "total_recovered_writes_5m": total_recovered_writes_5m,
            "total_transport_lost_5m": total_transport_lost_5m,
            "total_transport_lost_ever": total_transport_lost_ever,
            "last_transport_lost_at": latest_transport_lost_at or None,
            "total_large_payloads_5m": total_large_payloads_5m,
            "total_active_subagent_streams": total_active_subagent_streams,
            "max_total_ms": round(max_total_ms, 2),
            "max_pool_queue_ms": round(max_pool_queue_ms, 2),
            "max_health_check_ms": round(max_health_check_ms, 2),
            "max_acquire_ms": round(max_acquire_ms, 2),
            "max_wire_ms": round(max_wire_ms, 2),
            "max_payload_bytes_5m": max_payload_bytes,
            "max_pool_utilization": round(max_pool_util, 4),
            "max_pool_utilization_target": max_pool_util_name,
            "max_loop_lag_5s_ms": round(max_lag_5s, 2),
            "max_loop_lag_60s_ms": round(max_lag_60s, 2),
            "max_heartbeat_age_ms": round(max_hb_age, 2),
        },
        "server": server_data,
        "workers": workers,
    }
    summary["alerts"] = evaluate_cluster_alerts(summary)
    return summary
