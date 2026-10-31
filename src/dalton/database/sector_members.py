"""DB access for the sector-membership projection (`dalton_sector_members`, d025, sector layer P2).

The table is a query projection of each sector document's ``members`` array —
the ONLY authority is the versioned ``sector`` knowledge document. Every write
path therefore goes through :func:`sync_members_projection`, which runs inside
the caller's transaction (the sector version insert itself), replaces the
unit's rows wholesale with what the new document says, and never merges.

Read helpers come in two shapes: cursor-taking ones (the board already holds a
connection and a fake-friendly cursor in tests) and connection-opening ones
(``verify`` / ``rebuild``) for operators.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_MEMBER_COLS = """
    sector_unit_id::text AS sector_unit_id,
    member_id,
    member_name,
    ticker,
    company_unit_id::text AS company_unit_id,
    workspace_created,
    scope,
    reason,
    valid_until,
    position,
    synced_version_no,
    updated_at
"""


def expected_member_rows(content: Any) -> list[dict[str, Any]]:
    """Derive the projection rows for one sector document's content (pure).

    Tolerant by design: this runs against whatever the (schema-validated)
    document carries; malformed members are skipped rather than raising, so a
    legacy or partially-edited document can still project its clean entries.
    """
    if not isinstance(content, dict):
        return []
    rows: list[dict[str, Any]] = []
    for pos, m in enumerate(content.get("members") or []):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        name = str(m.get("name") or "").strip()
        if not mid or not name:
            continue
        ticker = str(m.get("ticker") or "").strip().upper() or None
        unit_ref = str(m.get("unit_id") or "").strip() or None
        valid_until_raw = str(m.get("valid_until") or "").strip() or None
        try:
            valid_until = date.fromisoformat(valid_until_raw) if valid_until_raw else None
        except ValueError:
            valid_until = None
        rows.append(
            {
                "member_id": mid,
                "member_name": name,
                "ticker": ticker,
                "company_unit_id": unit_ref,
                "workspace_created": bool(m.get("workspace_created")),
                "scope": str(m.get("scope") or "").strip(),
                "reason": str(m.get("reason") or "").strip(),
                "valid_until": valid_until,
                "position": pos,
            }
        )
    return rows


async def _active_sector_unit_on_cursor(
    cur: Any, workspace_id: str
) -> dict[str, Any] | None:
    await cur.execute(
        """
        SELECT unit_id::text AS unit_id, owner_user_id, slug
        FROM dalton_coverage_units
        WHERE workspace_id = %s::uuid AND status = 'active' AND kind = 'sector'
        """,
        (str(workspace_id).strip(),),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def sync_members_projection(
    cur: Any,
    workspace_id: str,
    content: Any,
    version_no: int,
) -> dict[str, Any] | None:
    """Rewrite one sector unit's projection rows inside the CALLER's transaction.

    Must run in the same transaction as the sector version insert so the table
    can never disagree with a committed document. Returns a small summary for
    logs/tests, or ``None`` when the workspace has no active sector unit (the
    write then is not a sector-profile write; nothing to project).
    """
    unit = await _active_sector_unit_on_cursor(cur, workspace_id)
    if unit is None:
        return None
    unit_id = str(unit["unit_id"])
    rows = expected_member_rows(content)
    try:
        await cur.execute(
            "DELETE FROM dalton_sector_members WHERE sector_unit_id = %s::uuid",
            (unit_id,),
        )
    except pg_errors.UndefinedTable as exc:
        raise RuntimeError(
            "dalton_sector_members is missing — run the d025 migration before "
            "writing sector documents with coverage_units.enabled on"
        ) from exc
    for row in rows:
        await cur.execute(
            """
            INSERT INTO dalton_sector_members (
                sector_unit_id, member_id, member_name, ticker, company_unit_id,
                workspace_created, scope, reason, valid_until, position,
                synced_version_no, updated_at
            ) VALUES (
                %s::uuid, %s, %s, %s, %s::uuid,
                %s, %s, %s, %s, %s,
                %s, NOW()
            )
            """,
            (
                unit_id,
                row["member_id"],
                row["member_name"],
                row["ticker"],
                row["company_unit_id"],
                row["workspace_created"],
                row["scope"],
                row["reason"],
                row["valid_until"],
                row["position"],
                int(version_no),
            ),
        )
    return {
        "sector_unit_id": unit_id,
        "members": len(rows),
        "synced_version_no": int(version_no),
    }


async def fetch_members_for_sectors(
    cur: Any, sector_unit_ids: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    """Cursor-taking read: the projection rows of the given sector units, doc order."""
    ids = [str(u).strip() for u in sector_unit_ids if str(u or "").strip()]
    if not ids:
        return []
    await cur.execute(
        f"""
        SELECT {_MEMBER_COLS}
        FROM dalton_sector_members
        WHERE sector_unit_id = ANY(%s::uuid[])
        ORDER BY sector_unit_id, position, member_id
        """,
        (ids,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_members_for_company(
    company_unit_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Reverse lookup: which sectors list this company unit as a member."""
    uid = str(company_unit_id or "").strip()
    if not uid:
        return []
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_MEMBER_COLS}
                FROM dalton_sector_members
                WHERE company_unit_id = %s::uuid
                ORDER BY sector_unit_id, position, member_id
                """,
                (uid,),
            )
            return [dict(r) for r in await cur.fetchall()]


async def _sector_docs_with_units() -> list[dict[str, Any]]:
    """Active sector units with a workspace binding, joined to their latest valid sector doc."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT u.unit_id::text AS unit_id, u.workspace_id::text AS workspace_id,
                       kv.version_no, kv.content
                FROM dalton_coverage_units u
                JOIN LATERAL (
                    SELECT version_no, content
                    FROM dalton_knowledge_versions
                    WHERE workspace_id = u.workspace_id
                      AND doc_key = 'sector'
                      AND schema_valid = TRUE
                    ORDER BY version_no DESC
                    LIMIT 1
                ) kv ON TRUE
                WHERE u.status = 'active' AND u.kind = 'sector'
                    AND u.workspace_id IS NOT NULL
                """
            )
            return [dict(r) for r in await cur.fetchall()]


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("member_id")),
        str(row.get("member_name")),
        str(row.get("ticker") or ""),
        str(row.get("company_unit_id") or ""),
        bool(row.get("workspace_created")),
        str(row.get("scope") or ""),
        str(row.get("reason") or ""),
        str(row.get("valid_until") or ""),
    )


async def verify_sector_members_projection(
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """READ-ONLY check: does the projection match what the sector documents say?

    Returns ``{ok, checked_units, drifts}`` where each drift names the unit and
    the member rows present on exactly one side. Drift can only come from a
    database restored without a matching document history, or a write that
    bypassed the knowledge pipeline; the cure is
    :func:`rebuild_sector_members_projection` (or simply the sector's next
    document write, which resyncs in its transaction).
    """
    docs: list[dict[str, Any]] = []
    try:
        docs = await _sector_docs_with_units()
    except pg_errors.UndefinedTable:
        return {"ok": True, "checked_units": 0, "drifts": [], "note": "table missing"}
    drifts: list[dict[str, Any]] = []
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            for doc in docs:
                expected = {
                    r["member_id"]: r for r in expected_member_rows(doc.get("content"))
                }
                actual_rows = await fetch_members_for_sectors(cur, [str(doc["unit_id"])])
                actual = {r["member_id"]: r for r in actual_rows}
                for mid, row in expected.items():
                    if mid not in actual:
                        drifts.append(
                            {"sector_unit_id": doc["unit_id"], "member_id": mid, "side": "document_only"}
                        )
                    elif _row_key(row) != _row_key(actual[mid]):
                        drifts.append(
                            {"sector_unit_id": doc["unit_id"], "member_id": mid, "side": "content_mismatch"}
                        )
                for mid in actual:
                    if mid not in expected:
                        drifts.append(
                            {"sector_unit_id": doc["unit_id"], "member_id": mid, "side": "projection_only"}
                        )
    return {
        "ok": not drifts,
        "checked_units": len(docs),
        "drifts": drifts,
    }


async def rebuild_sector_members_projection(
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Operator write path: resync every sector unit's rows from its latest valid doc.

    Not called from any production flow — the transactional sync keeps the
    table current; this exists for the d025 cutover (documents written under
    P1 predate the table) and for restoring after a ``verify`` reports drift.

    Each unit is rebuilt under the SAME advisory lock and transaction shape
    as ``insert_version`` (codex r1 #8): lock taken first, the latest valid
    document re-read INSIDE the lock, then the projection rewritten. An
    online write that commits between the listing and the rebuild is thereby
    either serialized before us (we see its document) or after us (our rows
    match the document we read, and its own sync supersedes us) — the
    projection can no longer be rolled back to a stale version.
    """
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as list_cur:
            await list_cur.execute(
                """
                SELECT unit_id::text AS unit_id, workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE status = 'active' AND kind = 'sector'
                  AND workspace_id IS NOT NULL
                ORDER BY unit_id
                """
            )
            units = [dict(r) for r in await list_cur.fetchall()]

    rebuilt: list[dict[str, Any]] = []
    for unit in units:
        ws_id = str(unit["workspace_id"])
        lock_key = f"dalton:kv:{ws_id}:sector"  # insert_version 的同一把锁
        async with get_db_connection(conn) as c:
            async with c.transaction():
                async with c.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,)
                    )
                    await cur.execute(
                        """
                        SELECT version_no, content
                        FROM dalton_knowledge_versions
                        WHERE workspace_id = %s::uuid
                          AND doc_key = 'sector'
                          AND schema_valid = TRUE
                        ORDER BY version_no DESC
                        LIMIT 1
                        """,
                        (ws_id,),
                    )
                    doc_row = await cur.fetchone()
                    if doc_row is None:
                        continue
                    summary = await sync_members_projection(
                        cur, ws_id, doc_row["content"], int(doc_row["version_no"])
                    )
                    if summary is not None:
                        rebuilt.append(summary)
    return {"rebuilt_units": len(rebuilt), "details": rebuilt}
