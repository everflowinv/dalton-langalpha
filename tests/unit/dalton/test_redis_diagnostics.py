"""Unit tests for Redis write-timeout diagnostics (Prod Audit 2 Batch 1 / §4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import psycopg
import pytest
import redis.asyncio as redis_async
import redis.exceptions as redis_exceptions

from src.dalton.observability import redis_diagnostics as rd
from src.ptc_agent.agent.middleware.background_subagent.registry import BackgroundTask
from src.ptc_agent.agent.middleware.background_subagent.redis_stream import (
    _open_spill_circuit,
    _spill_one_locked,
)
from src.utils.cache.redis_cache import RedisCacheClient
from src.utils.cache.stream_append import StreamAppendError, stream_append_with_retry
from src.utils.cache.stream_quota import StreamQuota

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_EXPORTER_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod" / "export_ops_status.py"


def _load_ops_exporter() -> Any:
    spec = importlib.util.spec_from_file_location("export_ops_status_diag_test", OPS_EXPORTER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeAsyncWriter:
    def __init__(self, *, write_delay_s: float = 0.0, on_write: Any = None) -> None:
        self.write_delay_s = write_delay_s
        self.on_write = on_write
        self.written: list[bytes] = []

    def writelines(self, chunks: Any) -> None:
        raw = b"".join(chunks)
        self.written.append(raw)
        if self.on_write is not None:
            self.on_write(raw)

    async def drain(self) -> None:
        if self.write_delay_s > 0:
            await asyncio.sleep(self.write_delay_s)

    def close(self) -> None:
        pass


class _FakeAsyncParser:
    def __init__(self, responses: list[tuple[float, Any]]) -> None:
        self._responses = list(responses)

    async def read_response(self, disable_decoding: bool = False, push_request: Any = False) -> Any:
        delay_s, val = self._responses.pop(0)
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if isinstance(val, BaseException):
            raise val
        return val

    async def can_read_destructive(self) -> bool:
        return False

    def on_disconnect(self) -> None:
        pass


class _StubInstrumentedConnection(rd.InstrumentedAsyncConnection):
    """Real ``InstrumentedAsyncConnection`` wired to in-memory writer/parser for socket-seam tests."""

    def __init__(
        self,
        *args: Any,
        responses: list[tuple[float, Any]] | None = None,
        write_delay_s: float = 0.0,
        disconnect_delay_s: float = 0.0,
        initially_connected: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._stub_responses = list(responses or [])
        self._stub_write_delay_s = write_delay_s
        self._stub_disconnect_delay_s = disconnect_delay_s
        self._stub_connected = initially_connected
        self._writer = _FakeAsyncWriter(write_delay_s=write_delay_s)
        self._parser = _FakeAsyncParser(self._stub_responses)

    @property
    def is_connected(self) -> bool:
        return self._stub_connected

    async def _connect(self) -> None:
        self._stub_connected = True

    async def on_connect_check_health(self, check_health: bool = True) -> None:
        pass

    async def can_read_destructive(self) -> bool:
        return False

    async def disconnect(self, nowait: bool = False, **kwargs: Any) -> None:
        if self._in_wire_read:
            import time

            d0 = time.monotonic()
            try:
                if self._stub_disconnect_delay_s > 0:
                    await asyncio.sleep(self._stub_disconnect_delay_s)
                self._stub_connected = False
            finally:
                self._disconnect_ms_in_read += (time.monotonic() - d0) * 1000.0
            return
        if self._stub_disconnect_delay_s > 0:
            await asyncio.sleep(self._stub_disconnect_delay_s)
        self._stub_connected = False


@pytest.mark.asyncio
async def test_reused_connection_idle_ping_delay_attributes_to_health_check_not_wire() -> None:
    """Idle reused connection triggering health_check_interval PING delay must be counted
    in ``health_check_ms`` while ``wire_ms`` stays low."""
    recorder = rd.WorkerDiagnosticsRecorder(worker_id="test-host:101", pid=101)
    conn = _StubInstrumentedConnection(
        health_check_interval=30,
        socket_timeout=5.0,
        responses=[
            (0.08, b"PONG"),  # delayed idle PING during check_health()
            (0.0, b"1-0"),    # fast actual XADD response
        ],
    )
    conn.next_health_check = 0.0

    with rd.redis_op_span("xadd", "workflow:stream:t1:r1", payload_bytes=64, recorder=recorder) as timing:
        assert timing is not None
        await conn.send_command("XADD", "workflow:stream:t1:r1", "1-0", "event", "hi")
        reply = await conn.read_response()

    assert reply == b"1-0"
    assert timing.health_check_ms >= 65.0
    assert timing.acquire_ms >= 65.0
    assert timing.wire_ms < 25.0


@pytest.mark.asyncio
async def test_transaction_pipeline_socket_delay_attributes_to_wire_not_pool_queue() -> None:
    """``pipeline(transaction=True).execute()`` socket read/write delay must land in
    ``wire_ms`` without contaminating ``pool_queue_ms`` or ``health_check_ms``."""
    recorder = rd.WorkerDiagnosticsRecorder(worker_id="test-host:102", pid=102)
    conn = _StubInstrumentedConnection(
        health_check_interval=0,
        socket_timeout=5.0,
        write_delay_s=0.02,
        responses=[
            (0.0, b"OK"),              # MULTI
            (0.0, b"QUEUED"),          # DEL
            (0.0, b"QUEUED"),          # XADD
            (0.0, b"QUEUED"),          # PERSIST
            (0.07, [1, b"1-0", True]), # EXEC delayed on wire
        ],
    )
    pool = rd.InstrumentedBlockingConnectionPool(max_connections=2, timeout=1.0)
    pool._available_connections.append(conn)
    pool._created_connections = 1
    client = redis_async.Redis(connection_pool=pool)

    raw_key = "workflow:stream:550e8400-e29b-41d4-a716-446655440000:run1"
    with rd.redis_op_span(
        "pipeline_xadd",
        raw_key,
        payload_bytes=128,
        is_pipeline=True,
        command_count=3,
        pool=pool,
        recorder=recorder,
    ) as timing:
        assert timing is not None
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(raw_key)
            pipe.xadd(raw_key, {b"event": b"hello"}, id="1-0")
            pipe.persist(raw_key)
            res = await pipe.execute()

    assert res == [1, b"1-0", True]
    assert timing.wire_ms >= 75.0
    assert timing.pool_queue_ms < 20.0
    assert timing.health_check_ms < 20.0
    assert timing.acquire_ms < 20.0
    assert timing.key_prefix.startswith("workflow:stream#")
    assert "550e8400" not in timing.key_prefix
    assert "run1" not in timing.key_prefix


@pytest.mark.asyncio
async def test_read_response_excludes_disconnect_cleanup_from_wire_ms() -> None:
    """When ``read_response`` fails and calls ``disconnect()``, disconnect cleanup latency
    must be excluded from ``wire_ms``."""
    recorder = rd.WorkerDiagnosticsRecorder(worker_id="test-host:103", pid=103)
    conn = _StubInstrumentedConnection(
        health_check_interval=0,
        socket_timeout=5.0,
        disconnect_delay_s=0.08,
        responses=[(0.0, OSError("connection reset by peer"))],
    )
    with pytest.raises(redis_exceptions.ConnectionError):
        with rd.redis_op_span("xadd", "workflow:stream:t1:r1", payload_bytes=32, recorder=recorder) as timing:
            assert timing is not None
            await conn.read_response()

    assert timing.wire_ms < 30.0


@pytest.mark.asyncio
async def test_pool_queue_wait_attributes_to_pool_queue_ms() -> None:
    """When all pool slots are busy, a waiting coroutine records slot-queue wait in ``pool_queue_ms``."""
    conn = _StubInstrumentedConnection(health_check_interval=0, socket_timeout=5.0, responses=[(0.0, b"OK")])
    pool = rd.InstrumentedBlockingConnectionPool(max_connections=1, timeout=1.0)
    pool._available_connections.append(conn)
    pool._created_connections = 1

    held = await pool.get_connection()

    async def _release_later() -> None:
        await asyncio.sleep(0.07)
        await pool.release(held)

    releaser = asyncio.create_task(_release_later())
    try:
        with rd.redis_op_span("set", "cache:item:123", payload_bytes=32, pool=pool) as timing:
            assert timing is not None
            got = await pool.get_connection()
            await pool.release(got)
    finally:
        await releaser

    assert timing.pool_queue_ms >= 55.0
    assert timing.health_check_ms < 20.0
    assert timing.wire_ms < 20.0


@pytest.mark.asyncio
async def test_leader_sampler_default_pg_connection_uses_pool_open_session_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default PG connection path must call ``src.server.database.pool.open_session_connection``
    (never the nonexistent ``conversation.get_db_connection_string``)."""
    import src.server.database.pool as db_pool

    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.closed = False
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone = AsyncMock(side_effect=[(True,), (1,)])

    open_sess = create_autospec(db_pool.open_session_connection, return_value=mock_conn)
    monkeypatch.setattr(db_pool, "open_session_connection", open_sess)

    mock_redis = create_autospec(redis_async.Redis, instance=True)
    mock_redis.info = AsyncMock(return_value={})
    mock_redis.execute_command = AsyncMock(return_value=[])

    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-default:2000",
        ops_dir=tmp_path,
        redis_client_getter=lambda: mock_redis,
    )
    res = await sampler.sample_once(trigger="periodic")
    assert res is not None
    assert res["status"] == "ok"
    assert open_sess.await_count == 1
    await sampler.close()


@pytest.mark.asyncio
async def test_leader_sampler_uses_standalone_diag_redis_with_1s_timeout_not_business_pool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Server sampling must open a dedicated standalone Redis connection (1s timeout)
    and never borrow from the business ``get_cache_client().client`` pool."""
    assert rd.SERVER_SAMPLE_REDIS_TIMEOUT_S == 1.0

    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.closed = False
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone = AsyncMock(side_effect=[(True,), (1,)])

    business_client = create_autospec(redis_async.Redis, instance=True)
    business_cache = create_autospec(RedisCacheClient, instance=True)
    business_cache.enabled = True
    business_cache.url = "redis://127.0.0.1:6379/2"
    business_cache.client = business_client

    diag_client = create_autospec(redis_async.Redis, instance=True)
    diag_client.info = AsyncMock(return_value={"rdb_bgsave_in_progress": 0, "latest_fork_usec": 100})
    diag_client.execute_command = AsyncMock(return_value=[])
    diag_client.aclose = AsyncMock(return_value=None)

    from_url_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_from_url(*args: Any, **kwargs: Any) -> Any:
        from_url_calls.append((args, kwargs))
        return diag_client

    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: business_cache)
    monkeypatch.setattr(redis_async.Redis, "from_url", staticmethod(_fake_from_url))

    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-diag:2002",
        ops_dir=tmp_path,
        pg_connect_fn=AsyncMock(return_value=mock_conn),
    )
    res = await sampler.sample_once(trigger="periodic")
    assert res is not None
    assert res["status"] == "ok"
    # Business client must never have been touched!
    assert business_client.info.call_count == 0
    assert business_client.execute_command.call_count == 0
    # Dedicated diagnostic client opened with 1s timeout and max_connections=1.
    assert len(from_url_calls) == 1
    call_args, call_kwargs = from_url_calls[0]
    assert call_args == ("redis://127.0.0.1:6379/2",)
    assert call_kwargs["max_connections"] == 1
    assert call_kwargs["socket_timeout"] == 1.0
    assert call_kwargs["socket_connect_timeout"] == 1.0

    await sampler.close()
    assert diag_client.aclose.await_count == 1


@pytest.mark.asyncio
async def test_async_flush_and_server_sample_offload_disk_io_via_to_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker snapshot flush and server snapshot write must offload file I/O via ``asyncio.to_thread``."""
    to_thread_targets: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def _recording_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        to_thread_targets.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(rd.asyncio, "to_thread", _recording_to_thread)

    rec = rd.WorkerDiagnosticsRecorder(worker_id="host-bg:3001", pid=3001)
    out_worker = await rec.flush_to_file_async(tmp_path)
    assert out_worker.is_file()

    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.closed = False
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone = AsyncMock(side_effect=[(True,), (1,)])

    mock_redis = create_autospec(redis_async.Redis, instance=True)
    mock_redis.info = AsyncMock(return_value={})
    mock_redis.execute_command = AsyncMock(return_value=[])

    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-bg:3001",
        ops_dir=tmp_path,
        pg_connect_fn=AsyncMock(return_value=mock_conn),
        redis_client_getter=lambda: mock_redis,
    )
    await sampler.sample_once(trigger="periodic")
    assert (tmp_path / "redis_diag_server.json").is_file()
    assert rd._atomic_write_json in to_thread_targets
    assert rd._fenced_write_server_snapshot in to_thread_targets
    await sampler.close()


@pytest.mark.asyncio
async def test_leader_sampler_verifies_select_1_before_writing_timeout_and_skips_on_lost_lock(
    tmp_path: Path,
) -> None:
    """Leader writes server timeout status only when dedicated PG connection SELECT 1 succeeds;
    if SELECT 1 fails (lock lost), it drops leadership and does not touch ``redis_diag_server.json``."""
    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.closed = False
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_conn.cursor.return_value = mock_cur

    mock_cur.fetchone = AsyncMock(side_effect=[(True,), (1,)])

    mock_redis = create_autospec(redis_async.Redis, instance=True)
    mock_redis.info = AsyncMock(side_effect=redis_exceptions.TimeoutError("Timeout reading from redis"))

    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-a:2001",
        ops_dir=tmp_path,
        pg_connect_fn=AsyncMock(return_value=mock_conn),
        redis_client_getter=lambda: mock_redis,
    )
    res = await sampler.sample_once(trigger="periodic")
    assert res is not None
    assert res["status"] == "timeout"
    assert res["leader_worker_id"] == "host-a:2001"
    server_file = tmp_path / "redis_diag_server.json"
    assert server_file.is_file()
    written = json.loads(server_file.read_text(encoding="utf-8"))
    assert written["status"] == "timeout"

    original_content = server_file.read_text(encoding="utf-8")
    mock_cur.fetchone = AsyncMock(side_effect=[(1,), psycopg.OperationalError("connection closed")])
    mock_redis.info = AsyncMock(side_effect=redis_exceptions.TimeoutError("still timing out"))

    res2 = await sampler.sample_once(trigger="periodic")
    assert res2 is None
    assert sampler.is_leader is False
    assert server_file.read_text(encoding="utf-8") == original_content


def test_key_and_reason_redaction_never_leaks_raw_keys_or_tickers() -> None:
    """Logs and diagnostic snapshots must record only prefix category + hash, never raw keys/tickers/IDs."""
    overview = rd.sanitize_key_prefix("overview:BRK-B")
    assert overview.startswith("overview#")
    assert len(overview.split("#")[1]) == 8
    assert "BRK-B" not in overview

    wf = rd.sanitize_key_prefix("workflow:stream:550e8400-e29b-41d4-a716-446655440000:run-secret")
    assert wf.startswith("workflow:stream#")
    assert "550e8400" not in wf
    assert "run-secret" not in wf

    single = rd.sanitize_key_prefix("BRK-B")
    assert single.startswith("key#")
    assert "BRK-B" not in single

    ctx = rd.sanitize_key_prefix("Cache set error for overview:BRK-B")
    assert ctx.startswith("cache_set#")
    assert "BRK-B" not in ctx

    # Verify _open_spill_circuit + record_transport_lost never write raw v2_key / thread_id / task_run_id into snapshot.
    rec = rd.reset_worker_diagnostics_recorder_for_tests()
    task = BackgroundTask(
        task_id="task-xyz-99",
        tool_call_id="tc-1",
        subagent_type="research",
        description="test",
        prompt="test prompt",
        task_run_id="run-secret-42",
    )
    raw_v2_key = "subagent:stream:550e8400-e29b-41d4-a716-446655440000:run-secret-42"
    _open_spill_circuit(
        task,
        phase="v2_pipeline",
        reason=f"asyncio.TimeoutError: v2 xadd to {raw_v2_key} did not answer in 5.0s",
        task_run_id="run-secret-42",
        seq=12,
    )
    rec.record_transport_lost(
        "overview:BRK-B",
        reason=f"Timeout writing {raw_v2_key} for overview:BRK-B",
    )
    snap_json = json.dumps(rec.build_snapshot())
    assert "BRK-B" not in snap_json
    assert "550e8400-e29b-41d4-a716-446655440000" not in snap_json
    assert "run-secret-42" not in snap_json
    assert "task-xyz-99" not in snap_json


@pytest.mark.asyncio
async def test_outer_diag_timeout_records_timeout_even_when_retry_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When outer ``diag_timeout`` cancels attempt 1 inside ``redis_event_buffer_span``,
    inner span must classify it as a timeout (not generic CancelledError) even if attempt 2 recovers."""
    import src.utils.cache.stream_append as sa

    rec = rd.reset_worker_diagnostics_recorder_for_tests()
    monkeypatch.setattr(sa, "_attempt_timeout_s", lambda: 0.05)
    monkeypatch.setattr(sa, "_acquire_timeout_s", lambda: 0.01)
    monkeypatch.setattr(sa, "_default_budget_s", lambda: 1.0)

    attempts = 0
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = True
    cache.pool = None

    async def _flaky_buffer(
        stream_key: str,
        *,
        event_id: int,
        max_size: int,
        stream_event: str | bytes,
        stream_record: str | bytes | None = None,
        ttl: int | None = None,
        bare: bool = False,
    ) -> None:
        nonlocal attempts
        attempts += 1
        payload = stream_event.encode("utf-8") if isinstance(stream_event, str) else stream_event
        with rd.redis_event_buffer_span(stream_key, payload, None, False, False, ttl, None):
            if attempts == 1:
                await asyncio.sleep(0.5)

    cache.pipelined_event_buffer = _flaky_buffer
    cache.stream_tail = AsyncMock(return_value=None)

    await stream_append_with_retry(
        cache,
        "workflow:stream:t1:r1",
        event_id=3,
        max_size=100,
        stream_event="id: 3\ndata: ok\n\n",
    )

    assert attempts == 2
    snap = rec.build_snapshot()
    assert snap["counts"]["timeout_errors_5m"] == 1
    assert snap["counts"]["recovered_writes_5m"] == 1
    assert any(ev["outcome"] == "timeout" and ev["error_type"] == "TimeoutError" for ev in snap["recent_events"])


@pytest.mark.asyncio
async def test_subagent_v2_recovery_branches_count_toward_recovered_writes() -> None:
    """Subagent v2 recovery paths (``v2_timeout_landed``, ``v2_error_landed``, ``v2_retry``)
    must all increment ``recovered_writes_5m``."""
    rec = rd.reset_worker_diagnostics_recorder_for_tests()

    # 1. v2_timeout_landed via _spill_one_locked
    cache1 = create_autospec(RedisCacheClient, instance=True)
    cache1.enabled = True
    cache1.pool = None
    client1 = create_autospec(redis_async.Redis, instance=True)
    client1.xadd = AsyncMock(side_effect=asyncio.TimeoutError("v2 timeout"))
    client1.xrevrange = AsyncMock(return_value=[(b"1-0", {b"payload": b'{"seq": 5}'})])
    cache1.client = client1

    task1 = BackgroundTask(
        task_id="t1",
        tool_call_id="tc1",
        subagent_type="research",
        description="d",
        prompt="p",
        task_run_id="run1",
    )
    await _spill_one_locked(
        cache1,
        "thread-1",
        task1,
        {"seq": 5, "event": "message_chunk", "data": {"x": 1}},
        "subagent:stream:thread-1:t1",
        append=AsyncMock(return_value=None),
        append_exc=StreamAppendError,
        quota=StreamQuota(max_events=100, max_bytes=1024 * 1024),
    )

    # 2. v2_error_landed via _spill_one_locked
    cache2 = create_autospec(RedisCacheClient, instance=True)
    cache2.enabled = True
    cache2.pool = None
    client2 = create_autospec(redis_async.Redis, instance=True)
    client2.xadd = AsyncMock(side_effect=redis_exceptions.ConnectionError("Error while reading from redis"))
    client2.xrevrange = AsyncMock(return_value=[(b"1-0", {b"payload": b'{"seq": 6}'})])
    cache2.client = client2

    task2 = BackgroundTask(
        task_id="t2",
        tool_call_id="tc2",
        subagent_type="research",
        description="d",
        prompt="p",
        task_run_id="run2",
    )
    await _spill_one_locked(
        cache2,
        "thread-1",
        task2,
        {"seq": 6, "event": "message_chunk", "data": {"x": 2}},
        "subagent:stream:thread-1:t2",
        append=AsyncMock(return_value=None),
        append_exc=StreamAppendError,
        quota=StreamQuota(max_events=100, max_bytes=1024 * 1024),
    )

    # 3. v2_retry (attempt 1 pre-send error, attempt 2 succeeds)
    cache3 = create_autospec(RedisCacheClient, instance=True)
    cache3.enabled = True
    cache3.pool = None
    client3 = create_autospec(redis_async.Redis, instance=True)
    client3.xadd = AsyncMock(
        side_effect=[
            redis_exceptions.ConnectionError("Bad response from PING health check"),
            b"2-0",
        ]
    )
    cache3.client = client3

    task3 = BackgroundTask(
        task_id="t3",
        tool_call_id="tc3",
        subagent_type="research",
        description="d",
        prompt="p",
        task_run_id="run3",
    )
    await _spill_one_locked(
        cache3,
        "thread-1",
        task3,
        {"seq": 7, "event": "message_chunk", "data": {"x": 3}},
        "subagent:stream:thread-1:t3",
        append=AsyncMock(return_value=None),
        append_exc=StreamAppendError,
        quota=StreamQuota(max_events=100, max_bytes=1024 * 1024),
    )

    snap = rec.build_snapshot()
    assert snap["counts"]["recovered_writes_5m"] == 3
    vias = [ev.get("via") for ev in snap["recent_events"] if ev.get("outcome") == "recovered"]
    assert vias == ["v2_timeout_landed", "v2_error_landed", "v2_retry"]


def test_transport_lost_log_rate_limiter_caps_burst(caplog: pytest.LogCaptureFixture) -> None:
    """Burst of ``transport_lost`` events must be rate-limited instead of flooding sync logs."""
    rec = rd.WorkerDiagnosticsRecorder(
        worker_id="host-rl:4001",
        pid=4001,
        transport_lost_log_max_per_second=2,
    )
    with caplog.at_level(logging.WARNING, logger="src.dalton.observability.redis_diagnostics"):
        for idx in range(10):
            rec.record_transport_lost("workflow:stream:t1:r1", event_id=idx, now_monotonic=500.1)

    warn_lines = [r for r in caplog.records if "outcome=transport_lost" in r.getMessage()]
    assert len(warn_lines) == 2
    snap = rec.build_snapshot(now_monotonic=500.2)
    assert snap["counts"]["transport_lost_total"] == 10
    assert snap["counts"]["transport_lost_logs_suppressed_total"] == 8


def test_multi_pid_aggregation_stale_cleanup_and_exporter_dedup(tmp_path: Path) -> None:
    """Active worker files merge cleanly, stale PID files (>180s) are pruned, and
    cluster-level P1/P2/P3 alerts deduplicate across repeated exporter ticks."""
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    stale_time = now - timedelta(seconds=240)

    (tmp_path / "redis_diag_worker_1001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worker_id": "node:1001",
                "pid": 1001,
                "updated_at": (now - timedelta(seconds=10)).isoformat(),
                "counts": {
                    "slow_writes_5m": 2,
                    "timeout_errors_5m": 1,
                    "recovered_writes_5m": 3,
                    "transport_lost_5m": 1,
                    "transport_lost_total": 1,
                    "large_payloads_5m": 1,
                },
                "last_transport_lost_at": (now - timedelta(seconds=15)).isoformat(),
                "latency_5m": {"max_wire_ms": 620.0, "max_acquire_ms": 12.0, "max_payload_bytes": 600_000},
                "pools": {"cache": {"in_use": 42, "available": 8, "max": 50, "utilization": 0.84}},
                "loop_lag": {"max_5s_ms": 200.0, "max_60s_ms": 400.0, "last_heartbeat_age_ms": 50.0},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "redis_diag_worker_1002.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worker_id": "node:1002",
                "pid": 1002,
                "updated_at": (now - timedelta(seconds=20)).isoformat(),
                "counts": {
                    "slow_writes_5m": 1,
                    "timeout_errors_5m": 0,
                    "recovered_writes_5m": 3,
                    "transport_lost_5m": 0,
                    "transport_lost_total": 0,
                    "large_payloads_5m": 0,
                },
                "latency_5m": {"max_wire_ms": 180.0, "max_acquire_ms": 5.0, "max_payload_bytes": 4096},
                "pools": {"cache": {"in_use": 10, "available": 40, "max": 50, "utilization": 0.2}},
                "loop_lag": {"max_5s_ms": 1600.0, "max_60s_ms": 1600.0, "last_heartbeat_age_ms": 100.0},
            }
        ),
        encoding="utf-8",
    )
    stale_file = tmp_path / "redis_diag_worker_9999.json"
    stale_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worker_id": "node:9999",
                "pid": 9999,
                "updated_at": stale_time.isoformat(),
                "counts": {"transport_lost_5m": 99},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "redis_diag_server.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": (now - timedelta(seconds=5)).isoformat(),
                "leader_worker_id": "node:1001",
                "status": "ok",
                "stats": {"latest_fork_usec": 250_000},
                "slowlog": [{"id": 1, "start_time": 1700000000, "duration_us": 125_000, "command": "XADD", "key_prefix": "workflow:stream#12345678"}],
            }
        ),
        encoding="utf-8",
    )

    exporter = _load_ops_exporter()
    summary = exporter.aggregate_redis_diagnostics(tmp_path, now=now, prune_stale=True)
    assert not stale_file.exists(), "expired PID worker file must be pruned"
    assert summary["pruned_worker_files"] == ["redis_diag_worker_9999.json"]
    assert summary["cluster"]["active_worker_count"] == 2
    assert summary["cluster"]["total_recovered_writes_5m"] == 6
    assert summary["cluster"]["total_transport_lost_5m"] == 1
    assert summary["cluster"]["max_pool_utilization"] == pytest.approx(0.84)
    assert summary["cluster"]["max_loop_lag_5s_ms"] == pytest.approx(1600.0)

    sent: list[tuple[str, str]] = []

    def _sender(code: str, detail: str) -> tuple[bool, int | None, str | None]:
        sent.append((code, detail))
        return True, 200, None

    state_file = tmp_path / "alert_state.json"
    first = exporter.dispatch_redis_diag_alerts(summary, state_file=state_file, now=now, sender=_sender)
    assert [a["code"] for a in first] == ["redis_diag_p1", "redis_diag_p2", "redis_diag_p3"]
    assert len(sent) == 3

    second = exporter.dispatch_redis_diag_alerts(
        summary,
        state_file=state_file,
        now=now + timedelta(seconds=60),
        sender=_sender,
    )
    assert second == []
    assert len(sent) == 3

    third = exporter.dispatch_redis_diag_alerts(
        summary,
        state_file=state_file,
        now=now + timedelta(minutes=31),
        sender=_sender,
    )
    assert [a["code"] for a in third] == ["redis_diag_p2", "redis_diag_p3"]


def test_everflow_default_ops_alert_transport_partial_cooldown_and_p1_p2_dedup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Covers:
    1. Everflow default transport resolves gateway URL to ``http://127.0.0.1:8962`` and reads
       ``INTERNAL_SERVICE_TOKEN`` from ``channel-gateway.env`` (refusing symlinks).
    2. Grok #1: cooldown state is committed ONLY for alerts that succeeded (failed P2 retries next tick).
    3. Codex Row 7: P1 still fires within 5m when an old PID expires and a new worker hits transport_lost.
    4. Codex Row 7: P2 does NOT re-fire after 30m for the same lingering SLOWLOG ID, but DOES fire for a new ID.
    """
    exporter = _load_ops_exporter()
    monkeypatch.delenv("DALTON_CHANNEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DALTON_OPS_ALERTS_URL", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)

    gw_env = tmp_path / "channel-gateway.env"
    gw_env.write_text('export INTERNAL_SERVICE_TOKEN="tok_everflow_123"\n', encoding="utf-8")
    gw_url, tok = exporter.resolve_ops_alert_transport(gateway_env_file=gw_env)
    assert gw_url == "http://127.0.0.1:8962"
    assert tok == "tok_everflow_123"

    # Symlink env file must be refused.
    sym_env = tmp_path / "symlink.env"
    sym_env.symlink_to(gw_env)
    _, sym_tok = exporter.resolve_ops_alert_transport(gateway_env_file=sym_env)
    assert sym_tok == ""

    # Partial failure cooldown commit (Grok #1): P1 succeeds, P2 fails (502), P3 succeeds.
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    summary_all = {
        "cluster": {
            "active_worker_count": 1,
            "total_transport_lost_5m": 1,
            "total_transport_lost_ever": 3,
            "last_transport_lost_at": "2026-11-27T11:59:50+00:00",
            "total_recovered_writes_5m": 0,
            "max_pool_utilization": 0.1,
            "max_loop_lag_5s_ms": 10.0,
            "max_payload_bytes_5m": 600_000,
        },
        "server": {
            "stats": {"latest_fork_usec": 1000},
            "slowlog": [{"id": 7, "start_time": 1700000000, "duration_us": 150_000, "command": "XADD"}],
        },
        "workers": [{"worker_id": "node:1001", "counts": {"transport_lost_total": 3}}],
    }
    state_file = tmp_path / "partial_state.json"

    def _partial_sender(code: str, _detail: str) -> tuple[bool, int | None, str | None]:
        if code == "redis_diag_p2":
            return False, 502, "HTTP 502: Feishu timeout"
        return True, 200, None

    res1 = exporter.dispatch_redis_diag_alerts(summary_all, state_file=state_file, now=now, sender=_partial_sender)
    assert [(a["code"], a["ok"]) for a in res1] == [
        ("redis_diag_p1", True),
        ("redis_diag_p2", False),
        ("redis_diag_p3", True),
    ]
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert "p1_last_sent_ts" in persisted
    assert "p3_last_sent_ts" in persisted
    assert "p2_last_sent_ts" not in persisted
    assert "p2_last_slowlog_id" not in persisted

    # Next tick 60s later: P2 MUST retry immediately while P1 & P3 remain suppressed!
    retried_codes: list[str] = []

    def _ok_sender(code: str, _detail: str) -> tuple[bool, int | None, str | None]:
        retried_codes.append(code)
        return True, 200, None

    res2 = exporter.dispatch_redis_diag_alerts(
        summary_all,
        state_file=state_file,
        now=now + timedelta(seconds=60),
        sender=_ok_sender,
    )
    assert [a["code"] for a in res2] == ["redis_diag_p2"]
    assert retried_codes == ["redis_diag_p2"]

    # Codex Row 7 (P1 old PID expiry): 2 minutes later (within 5m window), node:1001 expired and
    # node:1002 hits 1 new transport_lost -> cluster ever drops from 3 to 1, P1 MUST still fire!
    summary_new_worker = {
        "cluster": {
            "active_worker_count": 1,
            "total_transport_lost_5m": 1,
            "total_transport_lost_ever": 1,
            "last_transport_lost_at": "2026-11-27T12:02:00+00:00",
            "total_recovered_writes_5m": 0,
            "max_pool_utilization": 0.1,
            "max_loop_lag_5s_ms": 10.0,
            "max_payload_bytes_5m": 1000,
        },
        "server": {"stats": {"latest_fork_usec": 1000}, "slowlog": []},
        "workers": [{"worker_id": "node:1002", "counts": {"transport_lost_total": 1}}],
    }
    res3 = exporter.dispatch_redis_diag_alerts(
        summary_new_worker,
        state_file=state_file,
        now=now + timedelta(minutes=2),
        sender=_ok_sender,
    )
    assert [a["code"] for a in res3] == ["redis_diag_p1"]

    # Codex Row 7 (P2 lingering slowlog ID): 32 minutes later, same slowlog id=7 is still present -> NO P2!
    summary_same_slowlog = {
        "cluster": {
            "active_worker_count": 1,
            "total_transport_lost_5m": 0,
            "total_transport_lost_ever": 0,
            "total_recovered_writes_5m": 0,
            "max_pool_utilization": 0.1,
            "max_loop_lag_5s_ms": 10.0,
            "max_payload_bytes_5m": 1000,
        },
        "server": {
            "stats": {"latest_fork_usec": 1000},
            "slowlog": [{"id": 7, "start_time": 1700000000, "duration_us": 150_000, "command": "XADD"}],
        },
        "workers": [{"worker_id": "node:1002", "counts": {"transport_lost_total": 0}}],
    }
    res4 = exporter.dispatch_redis_diag_alerts(
        summary_same_slowlog,
        state_file=state_file,
        now=now + timedelta(minutes=32),
        sender=_ok_sender,
    )
    assert res4 == []

    # New slowlog id=8 arrives -> P2 FIRES!
    summary_new_slowlog = {
        **summary_same_slowlog,
        "server": {
            "stats": {"latest_fork_usec": 1000},
            "slowlog": [
                {"id": 8, "start_time": 1700001920, "duration_us": 120_000, "command": "SET"},
                {"id": 7, "start_time": 1700000000, "duration_us": 150_000, "command": "XADD"},
            ],
        },
    }
    res5 = exporter.dispatch_redis_diag_alerts(
        summary_new_slowlog,
        state_file=state_file,
        now=now + timedelta(minutes=33),
        sender=_ok_sender,
    )
    assert [a["code"] for a in res5] == ["redis_diag_p2"]


@pytest.mark.asyncio
async def test_stream_append_records_recovery_and_transport_lost_without_changing_behavior() -> None:
    """Diagnostic hooks in ``stream_append_with_retry`` record recovery & fatal transport_lost
    while preserving exact upstream retry and exception semantics."""
    rec = rd.reset_worker_diagnostics_recorder_for_tests()
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = True
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.TimeoutError("Timeout reading from redis"),
            None,
        ]
    )
    cache.stream_tail = AsyncMock(return_value=None)

    await stream_append_with_retry(
        cache,
        "workflow:stream:t1:r1",
        event_id=7,
        max_size=1000,
        stream_event="id: 7\ndata: ok\n\n",
    )
    snap = rec.build_snapshot()
    assert snap["counts"]["recovered_writes_5m"] == 1
    assert snap["counts"]["transport_lost_5m"] == 0

    cache2 = create_autospec(RedisCacheClient, instance=True)
    cache2.enabled = True
    cache2.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.ResponseError("OOM command not allowed")
    )
    with pytest.raises(StreamAppendError, match="OOM"):
        await stream_append_with_retry(
            cache2,
            "workflow:stream:t1:r2",
            event_id=8,
            max_size=1000,
            stream_event="id: 8\ndata: fail\n\n",
        )
    snap2 = rec.build_snapshot()
    assert snap2["counts"]["transport_lost_5m"] == 1


@pytest.mark.asyncio
async def test_admin_redis_diagnostics_endpoint_requires_admin_and_reads_consistent_disk_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Admin route must enforce admin tier and read persisted disk snapshots consistently
    (never overwriting disk counts with process-local recorder memory)."""
    from fastapi import HTTPException

    from src.dalton.app.router import get_admin_redis_diagnostics_endpoint

    monkeypatch.setenv("DALTON_REDIS_DIAG_DIR", str(tmp_path))
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    now = datetime.now(UTC)
    (tmp_path / "redis_diag_worker_5001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worker_id": "node:5001",
                "pid": 5001,
                "updated_at": now.isoformat(),
                "counts": {"slow_writes_5m": 4, "recovered_writes_5m": 1, "transport_lost_5m": 0},
                "latency_5m": {"max_wire_ms": 510.0},
            }
        ),
        encoding="utf-8",
    )
    # Mutate process-local recorder — endpoint must still return exact disk count (active=1, slow=4).
    local_rec = rd.reset_worker_diagnostics_recorder_for_tests()
    local_rec.record_recovery("workflow:stream:local:r1")

    async def _fake_tier_member(user_id: str | None) -> str:
        return "internal"

    async def _fake_tier_admin(user_id: str | None) -> str:
        return "admin"

    monkeypatch.setattr("src.dalton.auth_guards._default_get_user_tier", _fake_tier_member)
    with pytest.raises(HTTPException) as exc_info:
        await get_admin_redis_diagnostics_endpoint(user_id="member-1")
    assert exc_info.value.status_code == 403

    monkeypatch.setattr("src.dalton.auth_guards._default_get_user_tier", _fake_tier_admin)
    payload = await get_admin_redis_diagnostics_endpoint(user_id="admin-1")
    assert payload["schema_version"] == 1
    assert payload["cluster"]["active_worker_count"] == 1
    assert payload["cluster"]["total_slow_writes_5m"] == 4
    assert payload["cluster"]["total_recovered_writes_5m"] == 1


@pytest.mark.asyncio
async def test_diagnostics_kill_switch_disables_pool_hot_path_and_running_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``DALTON_REDIS_DIAG_ENABLED=0`` must disable pool instrumentation, skip pre-serialize
    traversal & heavy_task tagging, and cause a running service loop to release leadership & pause."""
    from redis.asyncio.connection import BlockingConnectionPool

    monkeypatch.setenv("DALTON_REDIS_DIAG_ENABLED", "0")
    pool = rd.create_cache_connection_pool("redis://127.0.0.1:6379/0", max_connections=4)
    assert type(pool) is BlockingConnectionPool

    # When disabled, dumps_with_heavy_task must NOT call is_large_serialize_value.
    called_probe = False

    def _fail_probe(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal called_probe
        called_probe = True
        return True

    monkeypatch.setattr(rd, "is_large_serialize_value", _fail_probe)
    assert rd.dumps_with_heavy_task({"a": "x" * 300_000}) is not None
    assert called_probe is False

    # Running service re-checks kill switch dynamically and releases lock when flipped off.
    sampler = create_autospec(rd.RedisDiagnosticsLeaderSampler, instance=True)
    sampler.is_leader = True
    sampler._lock_conn = object()
    sampler.sample_once = AsyncMock(return_value=None)
    sampler.close = AsyncMock(return_value=None)

    rec = rd.WorkerDiagnosticsRecorder(worker_id="host-ks:6001", pid=6001)
    svc = rd.RedisDiagnosticsService(
        recorder=rec,
        sampler=sampler,
        ops_dir=tmp_path,
        flush_interval_s=0.05,
        server_sample_interval_s=0.05,
    )
    monkeypatch.setenv("DALTON_REDIS_DIAG_ENABLED", "1")
    svc.start()
    await asyncio.sleep(0.08)
    assert sampler.sample_once.await_count >= 1
    # Flip off dynamically while running -> must close sampler (release PG lock) and stop sampling.
    monkeypatch.setenv("DALTON_REDIS_DIAG_ENABLED", "0")
    calls_before = sampler.sample_once.await_count
    await asyncio.sleep(0.15)
    assert sampler.close.await_count >= 1
    assert sampler.sample_once.await_count <= calls_before + 1
    await svc.stop()

    monkeypatch.setenv("DALTON_REDIS_DIAG_ENABLED", "1")
    pool_on = rd.create_cache_connection_pool("redis://127.0.0.1:6379/0", max_connections=4)
    assert isinstance(pool_on, rd.InstrumentedBlockingConnectionPool)
    assert pool_on.connection_class is rd.InstrumentedAsyncConnection


@pytest.mark.asyncio
async def test_stale_leader_snapshot_arriving_after_new_leader_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Item 1: If an old leader's off-loop snapshot write arrives after a new leader has
    claimed a higher epoch and published its snapshot first, the stale write MUST be discarded.
    Published snapshot files must include ``leader_epoch`` and ``written_at``."""
    conn1 = create_autospec(psycopg.AsyncConnection, instance=True)
    conn1.closed = False
    cur1 = create_autospec(psycopg.AsyncCursor, instance=True)
    cur1.__aenter__.return_value = cur1
    cur1.__aexit__.return_value = False
    conn1.cursor.return_value = cur1
    # Worker 1: acquire lock -> True, pre-write verify -> True (before PG session drops)
    cur1.fetchone = AsyncMock(side_effect=[(True,), (True,)])

    conn2 = create_autospec(psycopg.AsyncConnection, instance=True)
    conn2.closed = False
    cur2 = create_autospec(psycopg.AsyncCursor, instance=True)
    cur2.__aenter__.return_value = cur2
    cur2.__aexit__.return_value = False
    conn2.cursor.return_value = cur2
    # Worker 2: acquire lock -> True, pre-write verify -> True
    cur2.fetchone = AsyncMock(side_effect=[(True,), (True,)])

    redis1 = create_autospec(redis_async.Redis, instance=True)
    redis1.info = AsyncMock(return_value={"latest_fork_usec": 111})
    redis1.execute_command = AsyncMock(return_value=[])

    redis2 = create_autospec(redis_async.Redis, instance=True)
    redis2.info = AsyncMock(return_value={"latest_fork_usec": 222})
    redis2.execute_command = AsyncMock(return_value=[])

    w1 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-old:1001",
        ops_dir=tmp_path,
        pg_connect_fn=AsyncMock(return_value=conn1),
        redis_client_getter=lambda: redis1,
    )
    w2 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="host-new:1002",
        ops_dir=tmp_path,
        pg_connect_fn=AsyncMock(return_value=conn2),
        redis_client_getter=lambda: redis2,
    )

    real_to_thread = asyncio.to_thread
    w1_write_entered = asyncio.Event()
    release_w1_write = asyncio.Event()

    async def _stalling_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            func is rd._fenced_write_server_snapshot
            and kwargs.get("expected_epoch") == 1
        ):
            # Pause Worker 1 right after verify_leader_alive() succeeded, before its disk write runs.
            w1_write_entered.set()
            await release_w1_write.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(rd.asyncio, "to_thread", _stalling_to_thread)

    t1 = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 27, 12, 0, 5, tzinfo=UTC)

    w1_task = asyncio.create_task(w1.sample_once(trigger="periodic", now_utc=t1))
    await w1_write_entered.wait()
    assert w1.leader_epoch == 1

    # New leader takes over and publishes epoch=2 snapshot FIRST.
    w2_res = await w2.sample_once(trigger="periodic", now_utc=t2)
    assert w2_res is not None
    assert w2.leader_epoch == 2
    assert w2_res["leader_epoch"] == 2
    assert w2_res["written_at"] == t2.isoformat()

    server_file = tmp_path / "redis_diag_server.json"
    snap_before_w1 = json.loads(server_file.read_text(encoding="utf-8"))
    assert snap_before_w1["leader_worker_id"] == "host-new:1002"
    assert snap_before_w1["leader_epoch"] == 2
    assert snap_before_w1["written_at"] == t2.isoformat()

    # Now release Worker 1's delayed epoch=1 write -> MUST be discarded!
    release_w1_write.set()
    w1_res = await w1_task
    assert w1_res is None
    assert w1.is_leader is False
    assert w1.leader_epoch == 0

    # Disk snapshot MUST still be Worker 2's epoch=2 snapshot.
    snap_after = json.loads(server_file.read_text(encoding="utf-8"))
    assert snap_after["leader_worker_id"] == "host-new:1002"
    assert snap_after["leader_epoch"] == 2
    assert snap_after["written_at"] == t2.isoformat()
    assert snap_after["stats"]["latest_fork_usec"] == 222

    # Verify LEADER_LOCK_VERIFY_SQL was issued under the PG session lock.
    executed_sqls = [call.args[0] for call in cur1.execute.await_args_list]
    assert rd.LEADER_LOCK_VERIFY_SQL in executed_sqls

    await w1.close()
    await w2.close()


@pytest.mark.asyncio
async def test_disabled_business_redis_worker_skips_election_and_releases_lock_if_disabled_later(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Item 2: A worker whose business Redis client is disabled must not enter leader election;
    if an already-elected leader becomes disabled, it must release the PG advisory lock immediately."""
    business_cache = create_autospec(RedisCacheClient, instance=True)
    business_cache.enabled = False
    business_cache.url = "redis://127.0.0.1:6379/0"
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: business_cache)

    pg_connect = AsyncMock()
    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="disabled-worker:2001",
        ops_dir=tmp_path,
        pg_connect_fn=pg_connect,
    )

    # Part A: Disabled worker must NOT open PG connection or attempt leader election.
    assert await sampler.ensure_leadership() is False
    assert await sampler.sample_once(trigger="periodic") is None
    assert pg_connect.await_count == 0
    assert sampler.is_leader is False
    assert not (tmp_path / "redis_diag_server.json").exists()

    # Part B: Worker starts enabled, wins leadership, then business Redis becomes disabled ->
    # must release the PG advisory lock immediately without writing status="disabled".
    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.closed = False
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone = AsyncMock(side_effect=[(True,), (True,), (True,)])
    pg_connect.return_value = mock_conn

    diag_client = create_autospec(redis_async.Redis, instance=True)
    diag_client.info = AsyncMock(return_value={"latest_fork_usec": 100})
    diag_client.execute_command = AsyncMock(return_value=[])
    diag_client.aclose = AsyncMock(return_value=None)
    monkeypatch.setattr(redis_async.Redis, "from_url", staticmethod(lambda *_a, **_kw: diag_client))

    business_cache.enabled = True
    res_ok = await sampler.sample_once(trigger="periodic")
    assert res_ok is not None
    assert res_ok["status"] == "ok"
    assert sampler.is_leader is True
    snap_ok_text = (tmp_path / "redis_diag_server.json").read_text(encoding="utf-8")

    # Business client becomes disabled -> leader MUST unlock and drop leadership immediately.
    business_cache.enabled = False
    res_disabled = await sampler.sample_once(trigger="periodic")
    assert res_disabled is None
    assert sampler.is_leader is False
    assert sampler.leader_epoch == 0
    executed_sqls = [call.args[0] for call in mock_cur.execute.await_args_list]
    assert rd.LEADER_LOCK_RELEASE_SQL in executed_sqls
    assert mock_conn.close.await_count == 1
    assert diag_client.aclose.await_count == 1
    # Existing healthy snapshot must NOT be overwritten with status="disabled".
    assert (tmp_path / "redis_diag_server.json").read_text(encoding="utf-8") == snap_ok_text


def test_stale_server_snapshot_treated_as_no_data_and_suppresses_p2(tmp_path: Path) -> None:
    """Item 3: Exporter and core aggregator only accept server snapshots within 3x sampling
    interval (90s) by ``written_at``; expired snapshots are treated as no data and never fire P2."""
    exporter = _load_ops_exporter()
    assert exporter.DEFAULT_REDIS_SERVER_STALE_SECONDS == 90
    assert rd.STALE_SERVER_SECONDS == 90.0

    now = datetime(2026, 11, 27, 12, 5, 0, tzinfo=UTC)
    server_file = tmp_path / "redis_diag_server.json"

    # Fresh snapshot (85s old <= 90s): loaded and triggers P2 for high latest_fork_usec.
    server_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leader_epoch": 3,
                "written_at": (now - timedelta(seconds=85)).isoformat(),
                "updated_at": (now - timedelta(seconds=85)).isoformat(),
                "leader_worker_id": "node:1001",
                "status": "ok",
                "stats": {"latest_fork_usec": 350_000},
                "slowlog": [
                    {"id": 11, "start_time": 1700000100, "duration_us": 150_000, "command": "XADD"}
                ],
            }
        ),
        encoding="utf-8",
    )
    fresh_summary = exporter.aggregate_redis_diagnostics(tmp_path, now=now, prune_stale=False)
    assert fresh_summary["server"] is not None
    fresh_alerts, _ = exporter.evaluate_redis_diag_alerts(fresh_summary, state={}, now=now)
    assert [a["code"] for a in fresh_alerts] == ["redis_diag_p2"]

    fresh_core = rd.aggregate_redis_diagnostics(tmp_path, now=now, prune_stale_workers=False)
    assert fresh_core["server"] is not None
    assert any(a["code"] == "redis_diag_p2" for a in fresh_core["alerts"])

    # Expired snapshot (95s old > 90s): treated as no data (server=None) -> NO P2 alert!
    server_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "leader_epoch": 3,
                "written_at": (now - timedelta(seconds=95)).isoformat(),
                "updated_at": (now - timedelta(seconds=95)).isoformat(),
                "leader_worker_id": "node:1001",
                "status": "ok",
                "stats": {"latest_fork_usec": 350_000},
                "slowlog": [
                    {"id": 11, "start_time": 1700000100, "duration_us": 150_000, "command": "XADD"}
                ],
            }
        ),
        encoding="utf-8",
    )
    stale_summary = exporter.aggregate_redis_diagnostics(tmp_path, now=now, prune_stale=False)
    assert stale_summary["server"] is None
    stale_alerts, _ = exporter.evaluate_redis_diag_alerts(stale_summary, state={}, now=now)
    assert stale_alerts == []

    stale_core = rd.aggregate_redis_diagnostics(tmp_path, now=now, prune_stale_workers=False)
    assert stale_core["server"] is None
    assert stale_core["alerts"] == []


def test_alert_cooldown_committed_per_alert_immediately_and_gateway_timeout_matches_feishu(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Item 4:
    1. Gateway alert request timeout matches Feishu's 30s timeout (DEFAULT_FEISHU_HTTP_TIMEOUT_MS=30_000).
    2. ``post_gateway_ops_alert`` catches read-response ``TimeoutError`` cleanly instead of raising.
    3. ``dispatch_redis_diag_alerts`` commits each alert's cooldown state to disk IMMEDIATELY after
       that alert succeeds, so a subsequent alert timing out never loses earlier delivered cooldowns.
    """
    import inspect
    import urllib.request

    exporter = _load_ops_exporter()
    assert exporter.DEFAULT_GATEWAY_ALERT_TIMEOUT_SECONDS == 30.0
    sig = inspect.signature(exporter.post_gateway_ops_alert)
    assert sig.parameters["timeout_s"].default == 30.0

    # Verify read-response TimeoutError from opener.open is caught and returned as (False, None, ...).
    fake_opener = create_autospec(urllib.request.OpenerDirector, instance=True)
    fake_opener.open.side_effect = TimeoutError("timed out reading gateway response")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: fake_opener)
    ok, status, err = exporter.post_gateway_ops_alert(
        "redis_diag_p2",
        "detail",
        gateway_url="http://127.0.0.1:8962",
        service_token="tok_test",
    )
    assert ok is False
    assert status is None
    assert err is not None and "TimeoutError" in err
    assert fake_opener.open.call_args.kwargs["timeout"] == 30.0

    # Verify P1's cooldown is ALREADY persisted on disk BEFORE P2 runs (and survives P2 TimeoutError).
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    summary_all = {
        "cluster": {
            "active_worker_count": 1,
            "total_transport_lost_5m": 1,
            "total_transport_lost_ever": 1,
            "last_transport_lost_at": "2026-11-27T11:59:50+00:00",
            "total_recovered_writes_5m": 6,
            "max_pool_utilization": 0.1,
            "max_loop_lag_5s_ms": 10.0,
            "max_payload_bytes_5m": 600_000,
        },
        "server": {"stats": {"latest_fork_usec": 1000}, "slowlog": []},
        "workers": [{"worker_id": "node:1001", "counts": {"transport_lost_total": 1}}],
    }
    state_file = tmp_path / "immediate_commit_state.json"
    p1_committed_before_p2 = False

    def _timeout_on_p2_sender(code: str, _detail: str) -> tuple[bool, int | None, str | None]:
        nonlocal p1_committed_before_p2
        if code == "redis_diag_p1":
            return True, 200, None
        if code == "redis_diag_p2":
            # P1's cooldown MUST already be on disk before P2 finishes or times out!
            if state_file.is_file():
                on_disk = json.loads(state_file.read_text(encoding="utf-8"))
                p1_committed_before_p2 = "p1_last_sent_ts" in on_disk
            raise TimeoutError("gateway read timed out after 30s")
        return True, 200, None

    dispatched = exporter.dispatch_redis_diag_alerts(
        summary_all,
        state_file=state_file,
        now=now,
        sender=_timeout_on_p2_sender,
    )
    assert p1_committed_before_p2 is True
    assert [(a["code"], a["ok"]) for a in dispatched] == [
        ("redis_diag_p1", True),
        ("redis_diag_p2", False),
        ("redis_diag_p3", True),
    ]
    assert "TimeoutError" in str(dispatched[1]["error"])

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert "p1_last_sent_ts" in persisted
    assert "p3_last_sent_ts" in persisted
    assert "p2_last_sent_ts" not in persisted

    # Next tick 60s later: P1 and P3 stay suppressed, ONLY P2 retries.
    retried: list[str] = []

    def _retry_sender(code: str, _detail: str) -> tuple[bool, int | None, str | None]:
        retried.append(code)
        return True, 200, None

    exporter.dispatch_redis_diag_alerts(
        summary_all,
        state_file=state_file,
        now=now + timedelta(seconds=60),
        sender=_retry_sender,
    )
    assert retried == ["redis_diag_p2"]


@pytest.mark.asyncio
async def test_cache_set_timeout_counted_once_in_timeout_errors_5m() -> None:
    """Item 5: One ``RedisCacheClient.set()`` timeout must increment ``timeout_errors_5m``
    exactly once (deduplicating ``redis_op_span`` and ``_log_error``), while non-span errors
    like ``get()`` timeouts still count once."""
    rec = rd.reset_worker_diagnostics_recorder_for_tests()

    cache = RedisCacheClient(url="redis://127.0.0.1:6379/0")
    cache.enabled = True
    mock_client = create_autospec(redis_async.Redis, instance=True)
    shared_timeout = redis_exceptions.TimeoutError("Timeout reading from socket")
    mock_client.setex = AsyncMock(side_effect=shared_timeout)
    mock_client.get = AsyncMock(side_effect=shared_timeout)
    cache.client = mock_client

    ok1 = await cache.set("overview:AAPL", {"price": 200}, ttl=60)
    assert ok1 is False
    snap1 = rec.build_snapshot()
    assert snap1["counts"]["timeout_errors_5m"] == 1
    assert [ev["op"] for ev in snap1["recent_events"]] == ["set"]

    # Second SET timeout (even reusing the same exception object from AsyncMock) -> count = 2.
    ok2 = await cache.set("overview:MSFT", {"price": 400}, ttl=60)
    assert ok2 is False
    snap2 = rec.build_snapshot()
    assert snap2["counts"]["timeout_errors_5m"] == 2
    assert [ev["op"] for ev in snap2["recent_events"]] == ["set", "set"]

    # Non-span GET timeout goes through _log_error only -> count = 3.
    val = await cache.get("overview:AAPL")
    assert val is None
    snap3 = rec.build_snapshot()
    assert snap3["counts"]["timeout_errors_5m"] == 3
    assert [ev["op"] for ev in snap3["recent_events"]] == ["set", "set", "cache_error"]
