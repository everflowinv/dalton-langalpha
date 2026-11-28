"""Persistence helpers for ``dalton_sandbox_feishu_vault_scrub`` (§3.3).

Separated from ``sandbox_scrub.py`` so database SQL stays isolated from sandbox
runtime I/O and background scheduling, and integration tests can bind the test
pool holder cleanly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import computer_cols

_SAFE_EXC_TYPE_RE = re.compile(r"[^A-Za-z0-9_.]")
_MAX_EXC_TYPE_LEN = 120


def _sanitize_error_type(exc_type: str | None) -> str:
    """Keep only a short exception class identifier so no secret text enters storage."""
    cleaned = _SAFE_EXC_TYPE_RE.sub("", str(exc_type or "").strip())
    return cleaned[:_MAX_EXC_TYPE_LEN] or "Exception"


def _normalize_scrub_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("computer_id") is not None:
        out["computer_id"] = str(out["computer_id"])
    return out


async def get_scrub_state(
    computer_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return the scrub status row for ``computer_id``, or ``None`` if absent."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT computer_id, provider_ref, status, attempt_count,
                       last_error, cleaned_at, updated_at
                FROM dalton_sandbox_feishu_vault_scrub
                WHERE computer_id = %s::uuid
                """,
                (str(computer_id),),
            )
            return _normalize_scrub_row(await cur.fetchone())


async def mark_cleaned(
    computer_id: str,
    provider_ref: str | None,
    *,
    conn: Any = None,
) -> None:
    """Record ``(computer_id, provider_ref)`` as verified clean."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_sandbox_feishu_vault_scrub (
                        computer_id,
                        provider_ref,
                        status,
                        attempt_count,
                        last_error,
                        cleaned_at,
                        updated_at
                    )
                    VALUES (%s::uuid, %s, 'cleaned', 1, NULL, NOW(), NOW())
                    ON CONFLICT (computer_id) DO UPDATE SET
                        provider_ref = EXCLUDED.provider_ref,
                        status = 'cleaned',
                        attempt_count = dalton_sandbox_feishu_vault_scrub.attempt_count + 1,
                        last_error = NULL,
                        cleaned_at = NOW(),
                        updated_at = NOW()
                    """,
                    (str(computer_id), provider_ref),
                )


async def mark_failed(
    computer_id: str,
    provider_ref: str | None,
    exc_type: str,
    *,
    conn: Any = None,
) -> None:
    """Record a failed scrub attempt, incrementing ``attempt_count`` atomically in SQL."""
    safe_err = _sanitize_error_type(exc_type)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_sandbox_feishu_vault_scrub (
                        computer_id,
                        provider_ref,
                        status,
                        attempt_count,
                        last_error,
                        cleaned_at,
                        updated_at
                    )
                    VALUES (%s::uuid, %s, 'failed', 1, %s, NULL, NOW())
                    ON CONFLICT (computer_id) DO UPDATE SET
                        provider_ref = EXCLUDED.provider_ref,
                        status = 'failed',
                        attempt_count = dalton_sandbox_feishu_vault_scrub.attempt_count + 1,
                        last_error = EXCLUDED.last_error,
                        cleaned_at = NULL,
                        updated_at = NOW()
                    """,
                    (str(computer_id), provider_ref, safe_err),
                )


async def mark_deferred_stopped(
    computer_ids: Sequence[str],
    *,
    conn: Any = None,
) -> int:
    """Mark non-running uncleaned computers as ``deferred_stopped`` for next-start scrubbing."""
    normalized = list(dict.fromkeys(str(cid) for cid in computer_ids if cid))
    if not normalized:
        return 0
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_sandbox_feishu_vault_scrub (
                        computer_id,
                        provider_ref,
                        status,
                        attempt_count,
                        last_error,
                        cleaned_at,
                        updated_at
                    )
                    SELECT u.cid, c.provider_ref, 'deferred_stopped', 0, NULL, NULL, NOW()
                    FROM UNNEST(%s::uuid[]) AS u(cid)
                    LEFT JOIN computers c ON c.computer_id = u.cid
                    ON CONFLICT (computer_id) DO UPDATE SET
                        provider_ref = COALESCE(
                            EXCLUDED.provider_ref,
                            dalton_sandbox_feishu_vault_scrub.provider_ref
                        ),
                        status = 'deferred_stopped',
                        updated_at = NOW()
                    WHERE dalton_sandbox_feishu_vault_scrub.status <> 'cleaned'
                       OR (
                           EXCLUDED.provider_ref IS NOT NULL
                           AND dalton_sandbox_feishu_vault_scrub.provider_ref
                               IS DISTINCT FROM EXCLUDED.provider_ref
                       )
                    """,
                    (normalized,),
                )
                return max(cur.rowcount or 0, 0)


async def list_non_running_uncleaned_computer_ids(
    *,
    conn: Any = None,
) -> list[str]:
    """List non-deleted, non-running computers that lack a current ``cleaned`` record."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT c.computer_id
                FROM computers c
                LEFT JOIN dalton_sandbox_feishu_vault_scrub s
                  ON s.computer_id = c.computer_id
                WHERE c.status <> 'running'
                  AND c.status <> 'deleted'
                  AND (
                      s.computer_id IS NULL
                      OR (
                          s.status <> 'deferred_stopped'
                          AND (
                              s.status <> 'cleaned'
                              OR (
                                  c.provider_ref IS NOT NULL
                                  AND s.provider_ref IS DISTINCT FROM c.provider_ref
                              )
                          )
                      )
                  )
                ORDER BY c.computer_id
                """
            )
            return [str(r["computer_id"]) for r in await cur.fetchall()]


async def list_running_uncleaned(
    limit: int = 25,
    after: str | None = None,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List running computers needing Feishu Vault verification or scrubbing.

    Selects ``computers`` rows where ``status = 'running'`` and ``provider_ref``
    is present, excluding rows already marked ``cleaned`` for that exact
    ``provider_ref``. A recreated sandbox with a new ``provider_ref`` is
    automatically included for re-verification.
    """
    cols = computer_cols("c")
    params: list[Any] = []
    cursor_clause = ""
    if after is not None:
        cursor_clause = " AND c.computer_id > %s::uuid"
        params.append(str(after))
    params.append(int(limit))

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {cols}
                FROM computers c
                LEFT JOIN dalton_sandbox_feishu_vault_scrub s
                  ON s.computer_id = c.computer_id
                WHERE c.status = 'running'
                  AND c.provider_ref IS NOT NULL
                  AND (
                      s.computer_id IS NULL
                      OR s.status <> 'cleaned'
                      OR s.provider_ref IS DISTINCT FROM c.provider_ref
                  )
                  {cursor_clause}
                ORDER BY c.computer_id
                LIMIT %s
                """,
                tuple(params),
            )
            rows = []
            for row in await cur.fetchall():
                item = dict(row)
                item["computer_id"] = str(item["computer_id"])
                rows.append(item)
            return rows


__all__ = [
    "get_scrub_state",
    "list_non_running_uncleaned_computer_ids",
    "list_running_uncleaned",
    "mark_cleaned",
    "mark_deferred_stopped",
    "mark_failed",
]
