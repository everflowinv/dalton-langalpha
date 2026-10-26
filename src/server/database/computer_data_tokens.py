"""What every worker knows of the market-data token each computer's sandbox
holds: when it runs out and which sandbox took it, never the token itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import FENCE_BINDABLE


@dataclass(frozen=True)
class DataTokenRow:
    #: When the token in the sandbox runs out; None with none, or revoked.
    expires_at: datetime | None = None
    #: The sandbox that took it: None until a write lands, since one can fail
    #: after its mint.
    held_by: str | None = None
    #: What a save names to replace this row; 0 with no row.
    generation: int = 0


async def load(computer_id: str) -> DataTokenRow:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT expires_at, held_by, generation FROM computer_data_tokens"
                " WHERE computer_id = %s",
                (computer_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return DataTokenRow()
    return DataTokenRow(row["expires_at"], row["held_by"], int(row["generation"]))


@dataclass(frozen=True)
class Saved:
    #: False when the computer is not its user's or is leaving service.
    serving: bool
    #: The row's new generation; None when it moved since it was read.
    generation: int | None


async def save(
    computer_id: str,
    user_id: str,
    sandbox_id: str,
    expires_at: datetime,
    generation: int,
) -> Saved:
    """Record a token expiring at ``expires_at``, held by no sandbox yet, if
    the row is still at ``generation`` and the computer still runs on
    ``sandbox_id``.

    The share lock on the computer orders the save against a stop, as the file
    mount's token does: one saved first is revoked after it, and one waiting
    on it finds the computer stopping. The generation fences out a save that
    read the row before a stop and lands after the computer started again, and
    the sandbox one that read it before a rebuild or a tier move: its token
    would go to a sandbox the computer has left and stay good there until it
    runs out.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH serving AS (
                    SELECT computer_id, user_id FROM computers
                    WHERE computer_id = %(computer_id)s AND user_id = %(user_id)s
                      AND provider_ref = %(sandbox_id)s
                      AND {FENCE_BINDABLE}
                    FOR SHARE
                ),
                saved AS (
                    INSERT INTO computer_data_tokens AS t
                        (computer_id, user_id, expires_at, held_by, generation,
                         updated_at)
                    SELECT computer_id, user_id, %(expires_at)s, NULL,
                           %(generation)s + 1, NOW()
                    FROM serving
                    ON CONFLICT (computer_id) DO UPDATE
                        SET expires_at = EXCLUDED.expires_at, held_by = NULL,
                            user_id = EXCLUDED.user_id,
                            generation = t.generation + 1, updated_at = NOW()
                        WHERE t.generation = %(generation)s
                    RETURNING generation
                )
                SELECT EXISTS (SELECT 1 FROM serving) AS serving,
                       (SELECT generation FROM saved) AS generation
                """,
                {
                    "computer_id": computer_id,
                    "user_id": user_id,
                    "sandbox_id": sandbox_id,
                    "expires_at": expires_at,
                    "generation": generation,
                },
            )
            row = await cur.fetchone()
    return Saved(bool(row["serving"]), row["generation"])


async def mark_held(computer_id: str, generation: int, sandbox_id: str | None) -> bool:
    """Record that ``sandbox_id`` took the token saved at ``generation``, and
    only that one."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "UPDATE computer_data_tokens SET held_by = %s, updated_at = NOW()"
            " WHERE computer_id = %s AND generation = %s",
            (sandbox_id, computer_id, generation),
        )
        return cur.rowcount > 0


async def revoke(computer_id: str) -> list[str]:
    """Leave the row with no token and a new generation, inserting it if the
    computer has none, and answer whose tokens to revoke at the platform.

    Kept rather than deleted, so a mint in flight across the revoke finds the
    generation it read gone; nothing remains to revoke when the computer row
    itself is gone, since the cascade took this one with it.
    """
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            WITH tomb AS (
                INSERT INTO computer_data_tokens AS t
                    (computer_id, user_id, expires_at, held_by, generation,
                     updated_at)
                SELECT computer_id, user_id, NULL, NULL, 1, NOW()
                FROM computers WHERE computer_id = %(computer_id)s
                ON CONFLICT (computer_id) DO UPDATE
                    SET expires_at = NULL, held_by = NULL,
                        generation = t.generation + 1, updated_at = NOW()
                RETURNING user_id
            )
            SELECT user_id FROM tomb
            UNION
            SELECT user_id FROM computers WHERE computer_id = %(computer_id)s
            """,
            {"computer_id": computer_id},
        )
        return [str(row[0]) for row in await cur.fetchall() if row[0]]


async def claim_due(
    *,
    refresh_below: timedelta,
    retry_after: timedelta,
    limit: int,
) -> list[dict[str, Any]]:
    """Take up to ``limit`` running computers whose sandbox holds no token with
    more than ``refresh_below`` left, marking each attempted so that no worker
    takes it again for ``retry_after``.

    Never-tried computers come first, then the longest untried. A computer
    whose sandbox keeps failing (not running, unreachable, or the mint refused)
    is marked like the rest, so it waits behind the others rather than taking
    the front of every pass, and is tried about once per ``retry_after``
    however many workers sweep. Each row carries what the idle reaper judges
    the computer by, always-on and when it was last used, since its idle limit
    is its provider's own.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH due AS (
                    SELECT c.computer_id, c.user_id
                    FROM computers c
                    LEFT JOIN computer_data_tokens t
                        ON t.computer_id = c.computer_id
                    WHERE c.status = 'running' AND c.provider_ref IS NOT NULL
                      AND (t.expires_at IS NULL
                           OR t.expires_at < NOW() + %(refresh_below)s
                           OR t.held_by IS DISTINCT FROM c.provider_ref)
                      AND (t.attempted_at IS NULL
                           OR t.attempted_at < NOW() - %(retry_after)s)
                    ORDER BY t.attempted_at ASC NULLS FIRST,
                             t.expires_at ASC NULLS FIRST, c.computer_id
                    LIMIT %(limit)s
                ),
                claimed AS (
                    -- A computer with no row yet gets one at generation 0,
                    -- which is what a save that read no row names.
                    INSERT INTO computer_data_tokens AS t
                        (computer_id, user_id, generation, attempted_at)
                    SELECT computer_id, user_id, 0, NOW() FROM due
                    ON CONFLICT (computer_id) DO UPDATE SET attempted_at = NOW()
                        WHERE t.attempted_at IS NULL
                           OR t.attempted_at < NOW() - %(retry_after)s
                    RETURNING t.computer_id
                )
                SELECT c.computer_id, c.user_id, c.provider_ref, c.kind,
                       c.provider_config, c.root_dir, c.is_always_on,
                       COALESCE(c.last_activity_at, c.updated_at) AS last_activity
                FROM claimed JOIN computers c USING (computer_id)
                """,
                {
                    "refresh_below": refresh_below,
                    "retry_after": retry_after,
                    "limit": limit,
                },
            )
            return [dict(row) for row in await cur.fetchall()]
