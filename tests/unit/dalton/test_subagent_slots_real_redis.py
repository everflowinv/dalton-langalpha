"""Real-Redis smoke tests for background subagent slot Lua scripts.

Runs only when ``DALTON_TEST_REDIS_URL`` is set in the environment; skipped
otherwise. Exercises ``ACQUIRE_SLOT_LUA`` (acquire, renew, reregister, FIFO
queueing, lease expiry cleanup), ``QUERY_QUEUE_LUA``, and ``RELEASE_SLOT_LUA``
against a live Redis server to verify Lua return types (such as ``ZRANK`` and
``ZSCORE`` returning ``false`` in Redis Lua when absent).
"""

from __future__ import annotations

import os
import secrets

import pytest
import redis.asyncio as redis_async

from src.dalton.subagent_slots import (
    ACQUIRE_SLOT_LUA,
    QUERY_QUEUE_LUA,
    RELEASE_SLOT_LUA,
)

_REDIS_URL = os.environ.get("DALTON_TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.enable_socket,
    pytest.mark.skipif(
        not _REDIS_URL,
        reason="DALTON_TEST_REDIS_URL is not set; skipping real-Redis Lua smoke tests",
    ),
]


def _make_keys(prefix: str) -> tuple[str, str, str, str, str]:
    return (
        f"{prefix}:thread:t1",
        f"{prefix}:global",
        f"{prefix}:q_order:t1",
        f"{prefix}:q_lease:t1",
        f"{prefix}:q_seq:t1",
    )


@pytest.mark.asyncio
async def test_real_redis_lua_acquire_queue_renew_reregister_expiry_and_release() -> None:
    prefix = f"dalton:test:subagent_slots:{secrets.token_hex(8)}"
    t_key, g_key, qo_key, ql_key, seq_key = _make_keys(prefix)
    client = redis_async.from_url(_REDIS_URL, decode_responses=True)
    try:
        now = 1_000_000.0
        ttl = 90.0

        # 1. First member acquires slot (cap: per-thread=1, global=2)
        res1 = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m1",
            str(now),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in res1] == [1, 1, 1, 0]

        # Query queue for running member -> not queued ([0, 0, 0, 0])
        q_m1 = await client.eval(
            QUERY_QUEUE_LUA,
            4,
            t_key,
            g_key,
            qo_key,
            ql_key,
            "m1",
            "m1",
            str(now),
        )
        assert [int(x) for x in q_m1] == [0, 0, 0, 0]

        # 2. Second and third members queue in FIFO order (queue_ahead=0 and 1)
        res2 = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m2:run2",
            str(now),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in res2] == [0, 1, 1, 0]

        res3 = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m3:run3",
            str(now),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in res3] == [0, 1, 1, 1]

        # 3. Read-only QUERY_QUEUE_LUA returns accurate queue rank (both exact and base fallback)
        q_m2 = await client.eval(
            QUERY_QUEUE_LUA,
            4,
            t_key,
            g_key,
            qo_key,
            ql_key,
            "m2:run2",
            "m2",
            str(now),
        )
        assert [int(x) for x in q_m2] == [1, 1, 1, 0]

        q_m3 = await client.eval(
            QUERY_QUEUE_LUA,
            4,
            t_key,
            g_key,
            qo_key,
            ql_key,
            "m3:run3",
            "m3",
            str(now),
        )
        assert [int(x) for x in q_m3] == [1, 1, 1, 1]

        # 4. Renew on active m1 succeeds ([1, 1, 1, 0]); renew on missing m_missing returns 0
        renew_m1 = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m1",
            str(now + 30.0),
            str(ttl),
            "1",
            "2",
            "renew",
        )
        assert [int(x) for x in renew_m1] == [1, 1, 1, 0]

        renew_missing = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m_missing",
            str(now + 30.0),
            str(ttl),
            "1",
            "2",
            "renew",
        )
        assert [int(x) for x in renew_missing] == [0, 1, 1, 0]

        # 5. Reregister forces a running task into both ZSETs even when cap=1 is already full
        rereg = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m_recovered",
            str(now + 30.0),
            str(ttl),
            "1",
            "2",
            "reregister",
        )
        assert [int(x) for x in rereg] == [1, 2, 2, 0]

        # Release m_recovered and m1 -> m2:run2 is head of FIFO queue and acquires next
        rel_rec = await client.eval(
            RELEASE_SLOT_LUA, 4, t_key, g_key, qo_key, ql_key, "m_recovered"
        )
        assert int(rel_rec) == 1
        rel_m1 = await client.eval(
            RELEASE_SLOT_LUA, 4, t_key, g_key, qo_key, ql_key, "m1"
        )
        assert int(rel_m1) == 1

        # m3:run3 (rank 1) cannot jump ahead of m2:run2 (rank 0) while m2's queue lease is live
        try_m3_early = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m3:run3",
            str(now + 35.0),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in try_m3_early] == [0, 0, 0, 1]

        # m2:run2 (rank 0) acquires the slot
        acq_m2 = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m2:run2",
            str(now + 35.0),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in acq_m2] == [1, 1, 1, 0]

        # 6. Lease expiry: advance past m2:run2's expiry (now + 35 + 90 = now + 125)
        # Keep m3:run3's queue lease refreshed at now + 80 so only m2:run2 expires at now + 130
        await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m3:run3",
            str(now + 80.0),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        acq_m3_after_expiry = await client.eval(
            ACQUIRE_SLOT_LUA,
            5,
            t_key,
            g_key,
            qo_key,
            ql_key,
            seq_key,
            "m3:run3",
            str(now + 130.0),
            str(ttl),
            "1",
            "2",
            "acquire",
        )
        assert [int(x) for x in acq_m3_after_expiry] == [1, 1, 1, 0]

        # Clean release of m3:run3
        rel_m3 = await client.eval(
            RELEASE_SLOT_LUA, 4, t_key, g_key, qo_key, ql_key, "m3:run3"
        )
        assert int(rel_m3) == 1
        # Idempotent second release returns 0
        rel_m3_again = await client.eval(
            RELEASE_SLOT_LUA, 4, t_key, g_key, qo_key, ql_key, "m3:run3"
        )
        assert int(rel_m3_again) == 0
    finally:
        await client.delete(t_key, g_key, qo_key, ql_key, seq_key)
        await client.aclose()
