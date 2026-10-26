"""Raw psycopg3 SQL queries for the Feishu wiki read-scope whitelist (`dalton_feishu_read_scopes`)."""

from __future__ import annotations

import re
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

MAX_READ_SCOPES = 200

# Advisory-lock key serializing whole-table whitelist replacement (one fixed
# key: the table is small and there is exactly one whitelist).
READ_SCOPES_LOCK_KEY = 717001

# Feishu wiki space ids are opaque slugs of letters, digits, `_`, `-`.
SPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def normalize_space_id(raw: Any) -> str:
    """Strip and validate a wiki ``space_id``; raises ``ValueError`` on anything malformed."""
    space_id = str(raw or "").strip()
    if not space_id or not SPACE_ID_RE.match(space_id):
        raise ValueError(f"Invalid feishu space_id: {str(raw)[:50]!r}")
    return space_id


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["space_id"] = str(out.get("space_id") or "")
    out["name"] = str(out.get("name") or "")
    out["added_by"] = str(out.get("added_by") or "")
    out["added_at"] = out.get("added_at").isoformat() if out.get("added_at") else None
    return out


async def list_read_scopes(*, conn: Any = None) -> list[dict[str, Any]]:
    """Return all whitelist rows ordered by ``added_at`` ascending."""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT space_id, name, added_by, added_at
                FROM dalton_feishu_read_scopes
                ORDER BY added_at ASC, space_id ASC
                """
            )
            rows = await cur.fetchall()
            return [r for r in (_normalize_row(r) for r in rows) if r]


async def get_read_scope_space_ids(*, conn: Any = None) -> set[str]:
    """Return the set of whitelisted wiki ``space_id`` values."""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT space_id FROM dalton_feishu_read_scopes")
            rows = await cur.fetchall()
            return {
                str(r["space_id"]).strip()
                for r in rows
                if r and str(r.get("space_id") or "").strip()
            }


async def replace_read_scopes(
    scopes: list[dict[str, Any]],
    *,
    added_by: str,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Atomically replace the whole whitelist with ``scopes`` entries.

    Each entry needs a valid ``space_id`` (letters/digits/``_``/``-`` only)
    and optional ``name``; the caller identity is stamped on every row.
    Duplicated ``space_id`` values collapse to the last occurrence. The
    transaction takes an advisory lock first so concurrent replacements run
    serially instead of interleaving DELETEs and INSERTs.
    """
    deduped: dict[str, str] = {}
    for entry in scopes or []:
        space_id = normalize_space_id((entry or {}).get("space_id"))
        deduped[space_id] = str((entry or {}).get("name") or "").strip()[:200]
    if len(deduped) > MAX_READ_SCOPES:
        raise ValueError(f"Too many read scopes ({len(deduped)} > {MAX_READ_SCOPES})")

    async with get_db_connection(conn) as c:
        async with c.transaction():
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (READ_SCOPES_LOCK_KEY,)
                )
                await cur.execute("DELETE FROM dalton_feishu_read_scopes")
                for space_id, name in deduped.items():
                    await cur.execute(
                        """
                        INSERT INTO dalton_feishu_read_scopes (space_id, name, added_by)
                        VALUES (%s, %s, %s)
                        """,
                        (space_id, name, added_by),
                    )
    return await list_read_scopes()
