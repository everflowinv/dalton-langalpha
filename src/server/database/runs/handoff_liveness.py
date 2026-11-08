"""What a hand-off's card reads by id: its run, and the steer it became when
it was sent into a running turn (``GET /threads/dispatches/liveness``).

Each read keeps to the caller's own runs and steers in the query itself,
through the thread's workspace, so a batch of ids is one query and a foreign
id is simply absent. Non-UUID ids are dropped before the bind: the id columns
are uuid, and one malformed client id would fail the whole batch.
"""

from typing import Any, Dict, List

from fastapi import HTTPException
from psycopg.rows import dict_row

# DALTON-HOOK(colleague-dispatch): colleague thread liveness access checks
from src.dalton import sharing as dalton_sharing
from src.dalton.settings import is_colleague_dispatch_enabled
from src.server.database import pool
from src.server.database.runs.lifecycle import (
    _LIVENESS_RUN_COLUMNS,
    _OWNED_RUNS_FROM,
)
from src.server.database.runs.outbox import (
    _HAND_OFF_STEER_SQL,
    _LIVE_STEER_SQL,
    _STEER_TAKER_SQL,
    _steer_sql,
)
from src.server.utils.pg_sanitize import normalize_uuid

# DALTON-HOOK(colleague-dispatch): extra ownership columns stripped before returning
_OWNERSHIP_META_COLUMNS = ("workspace_id", "thread_user_id", "workspace_owner_id")


# DALTON-HOOK(colleague-dispatch): verify caller can read a run/steer on a colleague workspace
async def _can_read_liveness_row(
    *,
    user_id: str,
    thread_id: Any,
    workspace_id: Any,
    thread_user_id: Any,
    workspace_owner_id: Any,
) -> bool:
    if thread_user_id is None and workspace_owner_id is None:
        return True
    uid = str(user_id or "").strip()
    if not uid:
        return False
    ws_owner = str(workspace_owner_id or "").strip()
    eff_thread_owner = str(thread_user_id or ws_owner or "").strip()
    # Workspace owner always retains full read access to runs/steers in their own workspace
    if ws_owner == uid:
        return True
    if not is_colleague_dispatch_enabled():
        return False
    if await dalton_sharing.get_user_tier(uid) == "external":
        return False
    if eff_thread_owner and eff_thread_owner != uid:
        return False
    try:
        await dalton_sharing.require_thread_access(
            str(thread_id),
            uid,
            channel="web",
            need="read",
            owner_id=eff_thread_owner or None,
            workspace_owner_id=ws_owner or None,
            workspace_id=str(workspace_id) if workspace_id else None,
        )
    except HTTPException:
        return False
    return True


async def get_owned_runs(
    run_ids: List[str], user_id: str
) -> Dict[str, Dict[str, Any]]:
    """Named runs, ownership-filtered as ``get_latest_attempts_for_threads``
    is: ``{run id: row}``.

    A hand-off is one run, and the thread's latest attempt stops describing
    it once the thread runs again.
    """
    normalized = [nid for nid in (normalize_uuid(r) for r in run_ids) if nid]
    if not normalized:
        return {}
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(colleague-dispatch): allow workspace owner or thread owner in SQL, then verify colleague access in Python.
            await cur.execute(
                f"""
                SELECT {_LIVENESS_RUN_COLUMNS},
                       ct.workspace_id,
                       ct.user_id AS thread_user_id,
                       w.user_id AS workspace_owner_id
                {_OWNED_RUNS_FROM}
                WHERE cr.conversation_response_id = ANY(%s)
                  AND (w.user_id = %s OR COALESCE(ct.user_id, w.user_id) = %s)
                """,
                (normalized, user_id, user_id),
            )
            rows = await cur.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        row_dict = dict(r)
        if not await _can_read_liveness_row(
            user_id=user_id,
            thread_id=row_dict.get("conversation_thread_id"),
            workspace_id=row_dict.get("workspace_id"),
            thread_user_id=row_dict.get("thread_user_id"),
            workspace_owner_id=row_dict.get("workspace_owner_id"),
        ):
            continue
        for col in _OWNERSHIP_META_COLUMNS:
            row_dict.pop(col, None)
        out[str(row_dict["conversation_response_id"])] = row_dict
    return out


# The steer's own columns in a ``get_owned_steers`` row; the rest are its
# taker's run, read as ``get_owned_runs`` reads a run.
_STEER_COLUMNS = (
    "steer_id",
    "steer_thread_id",
    "steer_live",
    "steer_taker",
    "steer_workspace_id",
    "steer_thread_user_id",
    "steer_workspace_owner_id",
)


async def get_owned_steers(
    steer_ids: List[str], user_id: str
) -> Dict[str, Dict[str, Any]]:
    """Hand-off steers by id: ``{steer id: {thread_id, state, run_id, run}}``.

    A steer sent into one run is taken by whichever run reads it first, so a
    view of a hand-off steered into a run follows the steer, not that run.
    ``state`` is ``taken`` when a run took it in (``_STEER_TAKER_SQL``), with
    ``run_id`` and ``run`` that run's row, else ``queued`` while it is still
    owed, else ``dropped``. A user's steer, a task's, one whose row was purged
    after it settled, and one whose taker a fork's rewind deleted are absent.
    """
    normalized = [sid for sid in (normalize_uuid(s) for s in steer_ids) if sid]
    if not normalized:
        return {}
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # The taker is on the steer's thread, so owning the steer is
            # owning its run.
            # DALTON-HOOK(colleague-dispatch): allow workspace owner or thread owner in SQL, then verify colleague access in Python.
            await cur.execute(
                f"""
                WITH steer AS (
                    SELECT o.hook_outbox_id AS steer_id,
                           o.conversation_thread_id AS steer_thread_id,
                           ct.workspace_id AS steer_workspace_id,
                           ct.user_id AS steer_thread_user_id,
                           w.user_id AS steer_workspace_owner_id,
                           {_steer_sql(_LIVE_STEER_SQL)} AS steer_live,
                           {_steer_sql(_STEER_TAKER_SQL)} AS steer_taker
                    FROM hook_outbox o
                    JOIN conversation_threads ct
                        ON ct.conversation_thread_id = o.conversation_thread_id
                    JOIN workspaces w ON w.workspace_id = ct.workspace_id
                    WHERE o.hook_outbox_id = ANY(%s::uuid[])
                      AND {_HAND_OFF_STEER_SQL}
                      AND (w.user_id = %s OR COALESCE(ct.user_id, w.user_id) = %s)
                )
                SELECT steer.*, {_LIVENESS_RUN_COLUMNS}
                FROM steer
                LEFT JOIN conversation_responses cr
                    ON cr.conversation_thread_id = steer.steer_thread_id
                   AND cr.conversation_response_id = steer.steer_taker::uuid
                """,
                (normalized, user_id, user_id),
            )
            rows = await cur.fetchall()
    steers: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        # DALTON-HOOK(colleague-dispatch): verify caller can read steer row on colleague workspace
        if not await _can_read_liveness_row(
            user_id=user_id,
            thread_id=row.get("steer_thread_id"),
            workspace_id=row.get("steer_workspace_id"),
            thread_user_id=row.get("steer_thread_user_id"),
            workspace_owner_id=row.get("steer_workspace_owner_id"),
        ):
            continue
        run = None
        if row["steer_taker"] is not None:
            if row["conversation_response_id"] is None:
                continue
            run = {k: v for k, v in row.items() if k not in _STEER_COLUMNS}
            state = "taken"
        else:
            state = "queued" if row["steer_live"] else "dropped"
        steers[str(row["steer_id"])] = {
            "thread_id": str(row["steer_thread_id"]),
            "state": state,
            "run_id": str(run["conversation_response_id"]) if run else None,
            "run": run,
        }
    return steers
