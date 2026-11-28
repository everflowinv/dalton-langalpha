"""Live Redis + PostgreSQL fault-injection integration tests for Prod Audit 2 Batch 1 (§4).

Run by lead against real Redis + PostgreSQL test DB (database name MUST end with ``_test``):
    DALTON_TEST_PG_DSN="postgresql://.../dalton_test" \\
    DALTON_TEST_REDIS_URL="redis://127.0.0.1:6379/0" \\
    .venv/bin/python -m pytest -q -o addopts="" -p no:cacheprovider \\
        tests/integration/dalton/test_redis_sampler_live.py

Skipped automatically when ``DALTON_TEST_PG_DSN`` or ``DALTON_TEST_REDIS_URL`` is unset.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
from typing import Any

import pytest
import redis.asyncio as redis_async

from src.dalton.observability import redis_diagnostics as rd
from tests.integration.dalton._db_guard import extract_effective_test_db_name

PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()
REDIS_URL = os.getenv("DALTON_TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.enable_socket,
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN or not REDIS_URL,
        reason="DALTON_TEST_PG_DSN and DALTON_TEST_REDIS_URL are required for live Redis/PG diagnostics tests",
    ),
]


def _require_safe_pg_dsn() -> str:
    extract_effective_test_db_name(PG_DSN, source="DALTON_TEST_PG_DSN")
    return PG_DSN


@pytest.mark.asyncio
async def test_live_pool_saturation_attributes_to_pool_queue_ms() -> None:
    """With max_connections=2 saturated, a 3rd concurrent write records pool_queue_ms dominant."""
    _require_safe_pg_dsn()
    pool = rd.InstrumentedBlockingConnectionPool.from_url(
        REDIS_URL,
        max_connections=2,
        timeout=2.0,
        socket_timeout=5.0,
        socket_connect_timeout=2.0,
        connection_class=rd.InstrumentedAsyncConnection,
        health_check_interval=0,
    )
    client = redis_async.Redis(connection_pool=pool)
    key = f"dalton:test:diag:queue:{secrets.token_hex(6)}"
    try:
        # Warm 2 connections and check both out to saturate max_connections=2.
        c1 = await pool.get_connection()
        c2 = await pool.get_connection()

        async def _release_one() -> None:
            await asyncio.sleep(0.12)
            await pool.release(c1)

        releaser = asyncio.create_task(_release_one())
        try:
            with rd.redis_op_span("set", key, payload_bytes=16, pool=pool) as timing:
                assert timing is not None
                await client.set(key, b"ok", ex=30)
        finally:
            await releaser
            await pool.release(c2)

        assert timing.pool_queue_ms >= 90.0
        assert timing.wire_ms < 50.0
    finally:
        try:
            await client.delete(key)
        except Exception:
            pass
        await client.aclose()
        await pool.disconnect()


@pytest.mark.asyncio
async def test_live_reused_connection_idle_ping_attributes_to_health_check_ms() -> None:
    """Reused idle connection whose health_check_interval PING is delayed attributes latency
    to ``health_check_ms`` while ``wire_ms`` stays low."""
    _require_safe_pg_dsn()

    class _SlowPingConnection(rd.InstrumentedAsyncConnection):
        async def _send_ping(self) -> Any:
            await asyncio.sleep(0.10)
            return await super()._send_ping()

    pool = rd.InstrumentedBlockingConnectionPool.from_url(
        REDIS_URL,
        max_connections=2,
        timeout=2.0,
        socket_timeout=5.0,
        socket_connect_timeout=2.0,
        connection_class=_SlowPingConnection,
        health_check_interval=30,
    )
    client = redis_async.Redis(connection_pool=pool)
    key = f"dalton:test:diag:ping:{secrets.token_hex(6)}"
    try:
        # Establish connection first, then force next_health_check into the past.
        await client.set(key, b"init", ex=30)
        for conn in list(pool._available_connections):
            conn.next_health_check = 0.0

        with rd.redis_op_span("set", key, payload_bytes=16, pool=pool) as timing:
            assert timing is not None
            await client.set(key, b"after_ping", ex=30)

        assert timing.health_check_ms >= 85.0
        assert timing.acquire_ms >= 85.0
        assert timing.wire_ms < 40.0
    finally:
        try:
            await client.delete(key)
        except Exception:
            pass
        await client.aclose()
        await pool.disconnect()


@pytest.mark.asyncio
async def test_live_client_pause_transaction_pipeline_attributes_to_wire_ms() -> None:
    """Under Redis CLIENT PAUSE, ``pipeline(transaction=True).execute()`` records
    ``acquire_ms < 20ms`` and ``wire_ms`` dominant."""
    _require_safe_pg_dsn()
    pool = rd.InstrumentedBlockingConnectionPool.from_url(
        REDIS_URL,
        max_connections=4,
        timeout=2.0,
        socket_timeout=5.0,
        socket_connect_timeout=2.0,
        connection_class=rd.InstrumentedAsyncConnection,
        health_check_interval=0,
    )
    client = redis_async.Redis(connection_pool=pool)
    admin_client = redis_async.from_url(REDIS_URL)
    key = f"dalton:test:diag:pipe:{secrets.token_hex(6)}"
    try:
        # Warm one pooled connection so checkout has zero handshake cost.
        await client.ping()

        # Pause write commands on Redis server for 200ms from a separate client.
        await admin_client.execute_command("CLIENT", "PAUSE", 200, "WRITE")

        with rd.redis_op_span(
            "pipeline_xadd",
            key,
            payload_bytes=64,
            is_pipeline=True,
            command_count=3,
            pool=pool,
        ) as timing:
            assert timing is not None
            async with client.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.xadd(key, {b"event": b"id: 1\ndata: test\n\n"}, id="1-0")
                pipe.expire(key, 30)
                await pipe.execute()

        assert timing.acquire_ms < 20.0
        assert timing.wire_ms >= 150.0
    finally:
        try:
            await admin_client.execute_command("CLIENT", "UNPAUSE")
        except Exception:
            pass
        try:
            await client.delete(key)
        except Exception:
            pass
        await admin_client.aclose()
        await client.aclose()
        await pool.disconnect()


@pytest.mark.asyncio
async def test_live_two_worker_pg_single_advisory_lock_no_split_brain_and_failover(
    tmp_path: Path,
) -> None:
    """Two workers sharing real PG + Redis:
    1. Worker 1 acquires the single PG session advisory lock; during a simulated local
       Redis timeout on Worker 1, Worker 2 still cannot acquire the lock (zero split-brain).
    2. When Worker 1's dedicated PG connection closes (process exit / drop), PG releases
       the session advisory lock and Worker 2 automatically takes over and writes ``status='ok'``.
    """
    dsn = _require_safe_pg_dsn()
    lock_name = f"dalton:diag:redis_sampler_leader:test:{secrets.token_hex(6)}"
    real_redis = redis_async.from_url(REDIS_URL)

    class _SlowLocalRedisProxy:
        """Simulates local Redis timeout on Worker 1 while PG remains healthy."""

        async def info(self, section: str | None = None) -> dict[str, Any]:
            await asyncio.sleep(0.5)
            return {}

        async def execute_command(self, *args: Any) -> Any:
            await asyncio.sleep(0.5)
            return []

    w1 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="worker-1:1001",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        redis_client_getter=lambda: _SlowLocalRedisProxy(),
        redis_sample_timeout_s=0.1,
        lock_name=lock_name,
    )
    w2 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="worker-2:1002",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        redis_client_getter=lambda: real_redis,
        redis_sample_timeout_s=3.0,
        lock_name=lock_name,
    )
    try:
        # Step 1: Worker 1 wins the single PG advisory lock and experiences local Redis timeout.
        r1 = await w1.sample_once(trigger="periodic")
        assert w1.is_leader is True
        assert r1 is not None
        assert r1["leader_worker_id"] == "worker-1:1001"
        assert r1["status"] == "timeout"

        # Worker 2 tries concurrently while Worker 1 holds the PG lock -> MUST NOT acquire or write.
        r2_blocked = await w2.sample_once(trigger="periodic")
        assert w2.is_leader is False
        assert r2_blocked is None

        server_path = tmp_path / "redis_diag_server.json"
        snap_before = json.loads(server_path.read_text(encoding="utf-8"))
        assert snap_before["leader_worker_id"] == "worker-1:1001"
        assert snap_before["status"] == "timeout"

        # Step 2: Simulate Worker 1 crash by closing its dedicated PG connection directly
        # without calling pg_advisory_unlock (verifying PG auto-releases session lock).
        assert w1._lock_conn is not None
        await w1._lock_conn.close()
        w1._lock_conn = None
        w1._is_leader = False

        # Worker 2 takes over leadership on next cycle and samples real Redis cleanly.
        r2_takeover = await w2.sample_once(trigger="periodic")
        assert w2.is_leader is True
        assert r2_takeover is not None
        assert r2_takeover["leader_worker_id"] == "worker-2:1002"
        assert r2_takeover["status"] == "ok"
        assert "persistence" in r2_takeover
        assert "memory" in r2_takeover
        assert "stats" in r2_takeover
        assert "clients" in r2_takeover

        snap_after = json.loads(server_path.read_text(encoding="utf-8"))
        assert snap_after["leader_worker_id"] == "worker-2:1002"
        assert snap_after["status"] == "ok"
    finally:
        await w1.close()
        await w2.close()
        await real_redis.aclose()


def _parse_pg_dsn_to_db_env(dsn: str) -> dict[str, str]:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(dsn)
    env: dict[str, str] = {
        "DB_HOST": parsed.hostname or "localhost",
        "DB_PORT": str(parsed.port or 5432),
        "DB_NAME": parsed.path.lstrip("/") or "postgres",
        "DB_USER": unquote(parsed.username or "postgres"),
        "DB_PASSWORD": unquote(parsed.password or ""),
    }
    return env


@pytest.mark.asyncio
async def test_live_default_pg_connection_path_and_standalone_diag_redis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the real production connection path (NO ``pg_dsn`` / ``pg_connect_fn`` /
    ``redis_client_getter`` overrides): ``open_session_connection()`` via ``DB_*`` env vars
    plus dedicated standalone diagnostic Redis client (1s timeout)."""
    dsn = _require_safe_pg_dsn()
    for k, v in _parse_pg_dsn_to_db_env(dsn).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_CACHE_ENABLED", "true")

    lock_name = f"dalton:diag:redis_sampler_leader:default_path:{secrets.token_hex(6)}"
    sampler = rd.RedisDiagnosticsLeaderSampler(
        worker_id="prod-path-worker:1",
        ops_dir=tmp_path,
        redis_url=REDIS_URL,
        lock_name=lock_name,
    )
    try:
        res = await sampler.sample_once(trigger="periodic")
        assert sampler.is_leader is True
        assert res is not None
        assert res["status"] == "ok"
        assert "persistence" in res and "memory" in res and "stats" in res
        server_path = tmp_path / "redis_diag_server.json"
        assert server_path.is_file()
        disk = json.loads(server_path.read_text(encoding="utf-8"))
        assert disk["leader_worker_id"] == "prod-path-worker:1"
        assert disk["status"] == "ok"
    finally:
        await sampler.close()


@pytest.mark.asyncio
async def test_live_two_os_processes_pg_leader_lock_failover(tmp_path: Path) -> None:
    """Real two-process OS-level acceptance test:
    1. Spawn two separate Python processes using the default ``DB_*`` connection path and real Redis.
    2. Verify strictly one OS process acquires the single PG advisory lock and writes ``redis_diag_server.json``.
    3. Kill the leader OS process (SIGKILL) and verify PG releases the session lock so process 2 takes over.
    """
    import subprocess
    import sys

    dsn = _require_safe_pg_dsn()
    lock_name = f"dalton:diag:redis_sampler_leader:twoproc:{secrets.token_hex(6)}"
    child_env = {**os.environ, **_parse_pg_dsn_to_db_env(dsn), "REDIS_URL": REDIS_URL, "REDIS_CACHE_ENABLED": "true"}

    worker_script = (
        "import asyncio, json, os, sys\n"
        "from pathlib import Path\n"
        "from src.dalton.observability.redis_diagnostics import RedisDiagnosticsLeaderSampler\n"
        "async def main():\n"
        "    s = RedisDiagnosticsLeaderSampler(\n"
        "        worker_id=sys.argv[1],\n"
        "        ops_dir=Path(sys.argv[2]),\n"
        "        redis_url=os.environ['REDIS_URL'],\n"
        "        lock_name=sys.argv[3],\n"
        "    )\n"
        "    while True:\n"
        "        res = await s.sample_once(trigger='periodic')\n"
        "        print(json.dumps({'worker': sys.argv[1], 'leader': s.is_leader, 'ok': res is not None}), flush=True)\n"
        "        await asyncio.sleep(0.15)\n"
        "asyncio.run(main())\n"
    )

    p1 = subprocess.Popen(
        [sys.executable, "-c", worker_script, "os-worker-1", str(tmp_path), lock_name],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p2: subprocess.Popen[str] | None = None
    try:
        # Wait for p1 to become leader and write redis_diag_server.json.
        server_path = tmp_path / "redis_diag_server.json"
        for _ in range(40):
            if server_path.is_file():
                data = json.loads(server_path.read_text(encoding="utf-8"))
                if data.get("leader_worker_id") == "os-worker-1" and data.get("status") == "ok":
                    break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(f"os-worker-1 never became leader; stderr={p1.stderr.read() if p1.stderr else ''}")

        # Start p2 while p1 holds the lock: p2 must NOT overwrite redis_diag_server.json.
        p2 = subprocess.Popen(
            [sys.executable, "-c", worker_script, "os-worker-2", str(tmp_path), lock_name],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        await asyncio.sleep(0.6)
        mid = json.loads(server_path.read_text(encoding="utf-8"))
        assert mid["leader_worker_id"] == "os-worker-1"

        # Hard-kill p1 (no graceful unlock): PG must auto-release session lock and p2 must take over.
        p1.kill()
        p1.wait(timeout=5)

        for _ in range(50):
            data = json.loads(server_path.read_text(encoding="utf-8"))
            if data.get("leader_worker_id") == "os-worker-2" and data.get("status") == "ok":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("os-worker-2 failed to take over after os-worker-1 was killed")
    finally:
        for proc in (p1, p2):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_live_stale_leader_epoch_snapshot_discarded_after_failover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Real PG + Redis acceptance for Item 1:
    1. Worker 1 acquires the single PG advisory lock (epoch=1) and pauses right before writing.
    2. Worker 1's PG lock session drops; Worker 2 acquires the PG lock (epoch=2) and publishes
       its snapshot first (with ``leader_epoch=2`` and ``written_at``).
    3. Worker 1's delayed epoch=1 write resumes and is discarded; disk retains Worker 2's snapshot.
    """
    dsn = _require_safe_pg_dsn()
    lock_name = f"dalton:diag:redis_sampler_leader:epoch:{secrets.token_hex(6)}"

    w1 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="live-old-leader:1",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        redis_url=REDIS_URL,
        lock_name=lock_name,
    )
    w2 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="live-new-leader:2",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        redis_url=REDIS_URL,
        lock_name=lock_name,
    )

    real_to_thread = asyncio.to_thread
    w1_write_ready = asyncio.Event()
    release_w1_write = asyncio.Event()

    async def _stalling_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            func is rd._fenced_write_server_snapshot
            and kwargs.get("expected_epoch") == 1
        ):
            w1_write_ready.set()
            await release_w1_write.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(rd.asyncio, "to_thread", _stalling_to_thread)

    try:
        t1 = asyncio.create_task(w1.sample_once(trigger="periodic"))
        await w1_write_ready.wait()
        assert w1.leader_epoch == 1

        # Drop Worker 1's PG session directly so PG releases the advisory lock.
        assert w1._lock_conn is not None
        await w1._lock_conn.close()

        # Worker 2 takes over on real PG + Redis and publishes epoch=2 first.
        r2 = await w2.sample_once(trigger="periodic")
        assert r2 is not None
        assert r2["leader_worker_id"] == "live-new-leader:2"
        assert r2["leader_epoch"] == 2
        assert isinstance(r2["written_at"], str) and r2["written_at"]

        # Release Worker 1's stale epoch=1 write -> MUST be discarded.
        release_w1_write.set()
        r1 = await t1
        assert r1 is None
        assert w1.is_leader is False

        disk = json.loads((tmp_path / "redis_diag_server.json").read_text(encoding="utf-8"))
        assert disk["leader_worker_id"] == "live-new-leader:2"
        assert disk["leader_epoch"] == 2
        assert disk["written_at"] == r2["written_at"]
    finally:
        await w1.close()
        await w2.close()


@pytest.mark.asyncio
async def test_live_disabled_worker_yields_pg_lock_to_healthy_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Real PG + Redis acceptance for Item 2:
    Disabled business Redis client never takes leadership; if an elected leader becomes
    disabled, it releases the real PG advisory lock immediately so a healthy worker takes over.
    """
    from unittest.mock import create_autospec
    from src.utils.cache.redis_cache import RedisCacheClient

    dsn = _require_safe_pg_dsn()
    lock_name = f"dalton:diag:redis_sampler_leader:disabled:{secrets.token_hex(6)}"

    fake_cache = create_autospec(RedisCacheClient, instance=True)
    fake_cache.enabled = False
    fake_cache.url = REDIS_URL
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: fake_cache)

    w1 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="live-w1",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        lock_name=lock_name,
    )
    w2 = rd.RedisDiagnosticsLeaderSampler(
        worker_id="live-w2",
        ops_dir=tmp_path,
        pg_dsn=dsn,
        redis_url=REDIS_URL,
        lock_name=lock_name,
    )
    try:
        # Disabled w1 does not participate in election -> healthy w2 wins immediately.
        assert await w1.sample_once(trigger="periodic") is None
        assert w1.is_leader is False

        r2 = await w2.sample_once(trigger="periodic")
        assert r2 is not None and r2["status"] == "ok" and w2.is_leader is True
        await w2.close()

        # Enable w1 -> w1 wins lock -> then disable w1 -> w1 releases lock immediately so w2 wins.
        fake_cache.enabled = True
        r1 = await w1.sample_once(trigger="periodic")
        assert r1 is not None and r1["status"] == "ok" and w1.is_leader is True

        fake_cache.enabled = False
        assert await w1.sample_once(trigger="periodic") is None
        assert w1.is_leader is False

        r2_after = await w2.sample_once(trigger="periodic")
        assert r2_after is not None and r2_after["status"] == "ok" and w2.is_leader is True
        assert r2_after["leader_epoch"] > r1["leader_epoch"]
    finally:
        await w1.close()
        await w2.close()
