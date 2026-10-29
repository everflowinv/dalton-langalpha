"""R1 fix2: the reschedule's lease fence stays monotonic; the failure budget
counts reschedules out instead of rewinding ``attempts``.

The A→B→late-ack hazard, in SQL: A claims at attempts=N and reschedules; B
reclaims at attempts=N+1 (the claim's own +1); A's late ``ack_outbox_job``
fences on ``status='claimed' AND attempts=N``, which cannot match B's N+1.
That holds ONLY while the reschedule leaves attempts alone — the round-1
shape (``attempts = attempts - 1``) put B back at N and let A's stale ack
close B's claim. These tests pin each leg of that chain on the rendered
SQL, plus the ceiling sites that must count reschedules out so a long wait
never dead-letters (claim reclaim, the park sweep, nack, defer, requeue).
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from src.server.database.runs import outbox

_RUN = "00000000-0000-0000-0000-00000000000a"
_JOB = "00000000-0000-0000-0000-00000000000b"

_EFFECTIVE_BARE = (
    "(attempts - COALESCE((payload->>'reschedules')::bigint, 0))"
)


def _sql(mock_cursor) -> str:
    return re.sub(r"\s+", " ", mock_cursor.execute.call_args.args[0])


def _params(mock_cursor) -> tuple:
    return mock_cursor.execute.call_args.args[1]


@pytest.mark.asyncio
async def test_reschedule_never_rewinds_attempts(mock_db_connection, mock_cursor):
    """The fence leg: attempts is the claim generation — the reschedule bumps
    the payload's reschedules counter instead of decrementing the column, so
    the next claim mints N+1 and a paused owner's N stays spent."""
    mock_cursor.fetchone = AsyncMock(return_value={"status": "pending"})

    assert (
        await outbox.reschedule_claimed_job(_JOB, attempts=7, delay_seconds=30.0)
        == "pending"
    )

    sql = _sql(mock_cursor)
    assert "attempts - 1" not in sql  # never decremented — the token is spent
    assert "GREATEST" not in sql
    assert "AND attempts = %s" in sql  # fenced by the claiming generation
    # The budget counter bumps atomically in the same statement.
    assert "payload || jsonb_build_object( 'reschedules'" in sql or (
        "payload || jsonb_build_object('reschedules'" in sql
    )
    assert "COALESCE((payload->>'reschedules')::bigint, 0) + 1" in sql


@pytest.mark.asyncio
async def test_a_late_ack_fences_on_its_own_generation(mock_db_connection, mock_cursor):
    """The ack leg: done is written only against the exact claim generation,
    so an owner from before a reschedule-and-reclaim cycle (attempts moved on)
    can never close the new owner's row."""
    await outbox.ack_outbox_job(_JOB, attempts=7)

    sql = _sql(mock_cursor)
    assert "status = 'done'" in sql
    assert "AND attempts = %s" in sql
    assert _params(mock_cursor)[-1] == 7  # exact; B's claim sits at 8


@pytest.mark.asyncio
async def test_the_claim_mints_the_next_generation(mock_db_connection, mock_cursor):
    """The reclaim leg: every claim is attempts+1 — with the reschedule no
    longer taking one back, reschedule→reclaim always advances the fence."""
    mock_cursor.fetchall = AsyncMock(return_value=[])
    await outbox.claim_outbox_jobs(limit=1)

    sql = _sql(mock_cursor)
    assert "attempts = h.attempts + 1" in sql


@pytest.mark.asyncio
async def test_the_park_sweep_counts_reschedules_out(mock_db_connection, mock_cursor):
    """A wait of any length must not dead-letter: the sweep parks dead only
    past the EFFECTIVE count, not the raw claim counter."""
    mock_cursor.fetchall = AsyncMock(return_value=[])
    await outbox.park_exhausted_jobs(max_attempts=5)

    # Every statement the sweep builds — the steer/alert exhausts carry the
    # same ceiling fragment, and a fragment left uninterpolated (literal
    # braces) would be a runtime SQL error, so pin the rendered text on all
    # of them, not only the last.
    for call in mock_cursor.execute.call_args_list:
        rendered = re.sub(r"\s+", " ", call.args[0])
        assert "{_EFFECTIVE" not in rendered
    sql = _sql(mock_cursor)
    assert f"{_EFFECTIVE_BARE} >= %s" in sql
    assert " AND attempts >= %s" not in sql
    assert "(o.attempts - COALESCE" in re.sub(
        r"\s+", " ", mock_cursor.execute.call_args_list[0].args[0]
    )
    # The shared ceiling constant is interpolated, not brace-literal.
    assert "(o.attempts - COALESCE" in outbox._AT_CEILING_SQL
    assert "{_EFFECTIVE" not in outbox._AT_CEILING_SQL


@pytest.mark.asyncio
async def test_nack_defer_and_requeue_ceilings_count_reschedules_out(
    mock_db_connection, mock_cursor,
):
    """The same budget on every ceiling a waiting job can hit: a real
    delivery failure after many reschedules still has its full budget."""
    mock_cursor.fetchone = AsyncMock(return_value=None)

    await outbox.nack_outbox_job(_JOB, attempts=2, max_attempts=5)
    sql = _sql(mock_cursor)
    assert f"{_EFFECTIVE_BARE} >= %s" in sql
    assert " AND attempts >= %s" not in sql
    for call in mock_cursor.execute.call_args_list:
        assert "{_EFFECTIVE" not in re.sub(r"\s+", " ", call.args[0])

    await outbox.defer_claimed_job(_JOB, attempts=2, max_attempts=5)
    sql = _sql(mock_cursor)
    assert f"{_EFFECTIVE_BARE} >= %s" in sql
    assert " AND attempts >= %s" not in sql

    await outbox.requeue_job_with_key(
        _JOB, attempts=2, ordering_key="k", max_attempts=5
    )
    sql = _sql(mock_cursor)
    assert f"{_EFFECTIVE_BARE} >= %s" in sql
    assert " AND attempts >= %s" not in sql
