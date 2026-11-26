"""Dalton-owned PostgreSQL helpers for whole-computer archive fencing and claims.

Extracted from ``src/server/database/computer.py`` so the upstream database
module keeps only a thin ``DALTON-HOOK`` re-export. Collaborators are looked up
through ``src.server.database.computer`` so callers and tests patching that
module continue to work unchanged.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg.rows import dict_row

from src.server.database.sql_fences import (
    FENCE_NOT_DELETED,
    MOVE_EARLY_PHASES,
    MOVE_SET_ASIDE_PHASES,
    move_stale,
)


def normalize_archive_workspace_snapshot(
    rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Canonicalize live workspace rows on a computer for exact snapshot comparison."""
    from src.server.database import computer as computer_db

    normalized: List[Dict[str, Any]] = []
    for row in rows:
        ws_id = computer_db.normalize_uuid(str(row.get("workspace_id") or ""))
        normalized.append(
            {
                "workspace_id": ws_id or str(row.get("workspace_id") or ""),
                "name": str(row.get("name") or ""),
                "dir_name": str(row.get("dir_name") or ""),
                "status": str(row.get("status") or ""),
                "is_stand_in": bool(row.get("is_stand_in", False)),
            }
        )
    normalized.sort(key=lambda item: item["workspace_id"])
    return normalized


async def get_live_workspaces_for_archive_snapshot(
    computer_id: str,
    *,
    conn=None,
) -> List[Dict[str, Any]]:
    """Read-only snapshot of all live workspaces (including Home and stand-in) on ``computer_id``."""
    from src.server.database import computer as computer_db

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        return []
    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            """
            SELECT workspace_id, name, dir_name, status,
                   COALESCE(config, '{}'::jsonb) ? 'home_stand_in' AS is_stand_in
            FROM workspaces
            WHERE computer_id = %(computer_id)s AND status <> 'deleted'
            ORDER BY workspace_id
            """,
            {"computer_id": cid},
        )
        rows = await cur.fetchall()
    return normalize_archive_workspace_snapshot(rows)


async def computer_has_blocking_archive_move(
    computer_id: str,
    *,
    conn=None,
) -> bool:
    """Whether any active move (including stale staged/committed moves) blocks archiving."""
    from src.server.database import computer as computer_db

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        return False
    set_aside = ", ".join(f"'{p}'" for p in MOVE_SET_ASIDE_PHASES)
    early = ", ".join(f"'{p}'" for p in MOVE_EARLY_PHASES)
    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            f"""
            SELECT 1
            FROM workspace_moves m
            WHERE (m.source_computer_id = %(computer_id)s
                   OR m.target_computer_id = %(computer_id)s)
              AND (
                  m.phase IN ({set_aside})
                  OR (m.phase IN ({early}) AND NOT {move_stale("m.")})
              )
            LIMIT 1
            """,
            {"computer_id": cid},
        )
        return await cur.fetchone() is not None


async def clear_computer_archiving(
    computer_id: str,
    *,
    expected_archiving_owner: Optional[str] = None,
    conn=None,
) -> bool:
    """Clear the ``config.archiving`` fence once the provider confirms a terminal state."""
    from src.server.database import computer as computer_db

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        return False
    owner_guard = (
        "\n                  AND COALESCE(config, '{}'::jsonb)->>'archiving_owner' = %(expected_owner)s"
        if expected_archiving_owner is not None
        else ""
    )
    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            f"""
            WITH comp AS (
                UPDATE computers
                SET config = COALESCE(config, '{{}}'::jsonb)
                    - ARRAY['archiving', 'archiving_owner', 'archiving_claimed_at']::text[],
                    updated_at = NOW()
                WHERE computer_id = %(computer_id)s
                  AND {FENCE_NOT_DELETED}
                  AND COALESCE(config, '{{}}'::jsonb) @> '{{"archiving": true}}'::jsonb{owner_guard}
                RETURNING computer_id, status
            ),
            ws AS (
                UPDATE workspaces w
                SET status = 'stopped', stopped_at = NOW()
                FROM comp
                WHERE w.computer_id = comp.computer_id
                  AND comp.status = 'stopped'
                  AND w.status = 'stopping'
            )
            SELECT computer_id FROM comp
            """,
            {
                "computer_id": cid,
                "expected_owner": expected_archiving_owner,
            },
        )
        return await cur.fetchone() is not None


async def retain_computer_archive_claim(
    computer_id: str,
    *,
    archiving_owner: str,
    conn=None,
) -> bool:
    """Re-verify and touch ``updated_at`` for the active archive claim before contacting the provider."""
    from src.server.database import computer as computer_db

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        return False
    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            f"""
            UPDATE computers
            SET updated_at = NOW()
            WHERE computer_id = %(computer_id)s
              AND {FENCE_NOT_DELETED}
              AND status = 'stopping'
              AND COALESCE(config, '{{}}'::jsonb) @> '{{"archiving": true}}'::jsonb
              AND COALESCE(config, '{{}}'::jsonb)->>'archiving_owner' = %(archiving_owner)s
            RETURNING computer_id
            """,
            {
                "computer_id": cid,
                "archiving_owner": archiving_owner,
            },
        )
        return await cur.fetchone() is not None


async def reset_abandoned_archiving_computer(
    computer_id: str,
    *,
    expected_updated_at: Optional[datetime] = None,
    expected_archiving_owner: Optional[str] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Reset one abandoned ``stopping`` archiving computer back to ``stopped`` without shadow-writing workspaces.

    Matches only a row still in ``stopping`` with ``config.archiving = true`` (and
    the exact ``expected_updated_at`` / ``expected_archiving_owner`` when given) so
    startup recovery never stomps a newer claim or overwrites live workspaces on that computer.
    """
    from src.server.database import computer as computer_db

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        return None
    updated_guard = (
        "\n              AND updated_at = %(expected_updated_at)s"
        if expected_updated_at is not None
        else ""
    )
    owner_guard = (
        "\n              AND COALESCE(config, '{}'::jsonb)->>'archiving_owner' = %(expected_archiving_owner)s"
        if expected_archiving_owner is not None
        else ""
    )
    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            f"""
            UPDATE computers
            SET status = 'stopped',
                stopped_at = NOW(),
                updated_at = NOW()
            WHERE computer_id = %(computer_id)s
              AND status = 'stopping'
              AND COALESCE(config, '{{}}'::jsonb) @> '{{"archiving": true}}'::jsonb{updated_guard}{owner_guard}
            RETURNING {computer_db._COMPUTER_COLS}
            """,
            {
                "computer_id": cid,
                "expected_updated_at": expected_updated_at,
                "expected_archiving_owner": expected_archiving_owner,
            },
        )
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def get_archiving_computers_by_status(
    status: str,
    limit: int = 1000,
    *,
    conn=None,
) -> List[Dict[str, Any]]:
    """List non-deleted computers in ``status`` carrying the ``config.archiving`` fence."""
    from src.server.database import computer as computer_db

    async with computer_db._computer_cursor(conn) as cur:
        await cur.execute(
            f"""
            SELECT {computer_db._COMPUTER_COLS}
            FROM computers
            WHERE status = %s
              AND COALESCE(config, '{{}}'::jsonb) @> '{{"archiving": true}}'::jsonb
            ORDER BY updated_at ASC, computer_id
            LIMIT %s
            """,
            (status, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def claim_computer_for_archive(
    computer_id: str,
    *,
    expected_workspaces: Optional[Sequence[Mapping[str, Any]]] = None,
    expected_workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically lock computer + all live workspaces, verify snapshot & moves, and fence archiving.

    Lock order matches ``claim_workspace_move`` and ``commit_workspace_move``:
    ``computers`` (FOR UPDATE) -> ``workspaces ORDER BY workspace_id`` (FOR UPDATE).
    """
    from ptc_agent.core.sandbox.providers import provider_supports_archive
    from src.server.database import computer as computer_db
    from src.server.database.workspace_folders import (
        computer_command_running,
        computer_run_in_progress,
    )
    from src.server.services.computer_errors import (
        ArchiveScopeChangedError,
        ArchiveUnsupportedError,
        ComputerArchivingError,
        ComputerBusyError,
        ComputerNotStoppedError,
    )

    cid = computer_db.normalize_uuid(computer_id)
    if cid is None:
        raise ValueError(f"Computer {computer_id} not found")

    set_aside = ", ".join(f"'{p}'" for p in MOVE_SET_ASIDE_PHASES)
    early = ", ".join(f"'{p}'" for p in MOVE_EARLY_PHASES)
    expected_ws_norm = (
        normalize_archive_workspace_snapshot(expected_workspaces)
        if expected_workspaces is not None
        else None
    )
    expected_target_ws = (
        computer_db.normalize_uuid(expected_workspace_id)
        if expected_workspace_id is not None
        else None
    )

    async with computer_db.get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            # 1. Lock the computer row first.
            await cur.execute(
                f"""
                SELECT {computer_db._COMPUTER_COLS}
                FROM computers
                WHERE computer_id = %(computer_id)s AND {FENCE_NOT_DELETED}
                FOR UPDATE
                """,
                {"computer_id": cid},
            )
            computer = await cur.fetchone()
            if computer is None:
                raise ValueError(f"Computer {computer_id} not found")
            if not provider_supports_archive(computer.get("kind")):
                raise ArchiveUnsupportedError("Provider does not support archiving")
            if computer["status"] != "stopped":
                raise ComputerNotStoppedError(
                    f"Cannot archive computer in '{computer['status']}' state. "
                    "Only stopped computers can be archived."
                )
            if not computer.get("provider_ref"):
                raise RuntimeError("No sandbox associated with this computer")
            cfg = computer.get("config") or {}
            if cfg.get("retire_when_empty"):
                raise ComputerBusyError("Computer is being retired")
            if cfg.get("archiving"):
                raise ComputerArchivingError(
                    "This computer is already being archived."
                )
            spec_change = computer.get("spec_change")
            if (
                isinstance(spec_change, Mapping)
                and spec_change.get("state") == "in_progress"
                and not computer.get("spec_change_stale")
            ):
                raise ComputerBusyError("Computer has a spec change in progress")

            # 2. Lock every live workspace row on this computer in deterministic order.
            await cur.execute(
                """
                SELECT workspace_id, name, dir_name, status,
                       COALESCE(config, '{}'::jsonb) ? 'home_stand_in' AS is_stand_in
                FROM workspaces
                WHERE computer_id = %(computer_id)s AND status <> 'deleted'
                ORDER BY workspace_id
                FOR UPDATE
                """,
                {"computer_id": cid},
            )
            live_rows = await cur.fetchall()
            actual_snapshot = normalize_archive_workspace_snapshot(live_rows)
            if expected_target_ws is not None and not any(
                row["workspace_id"] == expected_target_ws for row in actual_snapshot
            ):
                raise ArchiveScopeChangedError("情况已变化，请重新发起")
            if expected_ws_norm is not None and actual_snapshot != expected_ws_norm:
                raise ArchiveScopeChangedError("情况已变化，请重新发起")

            # 3. Under both computer & workspace locks, re-check active runs,
            #    background commands, and any workspace move (including stale
            #    staged/committed moves whose folders were already set aside).
            if await computer_run_in_progress(cur, cid):
                raise ComputerBusyError(
                    "Cannot archive a computer that still has work running"
                )
            if await computer_command_running(cur, cid):
                raise ComputerBusyError(
                    "Cannot archive a computer that has background commands running"
                )
            await cur.execute(
                f"""
                SELECT 1
                FROM workspace_moves m
                WHERE (m.source_computer_id = %(computer_id)s
                       OR m.target_computer_id = %(computer_id)s)
                  AND (
                      m.phase IN ({set_aside})
                      OR (m.phase IN ({early}) AND NOT {move_stale("m.")})
                  )
                LIMIT 1
                """,
                {"computer_id": cid},
            )
            if await cur.fetchone() is not None:
                raise ComputerBusyError(
                    "Cannot archive a computer while a workspace move is in progress"
                )

        # 4. Stamp stopping + config.archiving=True + owner/timestamp atomically in the same transaction.
        claimed = await computer_db.update_computer_status(
            cid,
            "stopping",
            expected="stopped",
            merge_config={
                "archiving": True,
                "archiving_owner": _uuid.uuid4().hex,
                "archiving_claimed_at": datetime.now(timezone.utc).isoformat(),
            },
            conn=conn,
        )
        if claimed is None:
            raise ComputerBusyError("Computer changed while preparing archive; retry")
        return claimed
