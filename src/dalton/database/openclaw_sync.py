"""Raw psycopg3 SQL queries for `dalton_openclaw_sync_runs` (P0.7 §3.4)."""

from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


async def try_acquire_sync_lock(conn: Any) -> bool:
    """Try to acquire session-level advisory lock `dalton:openclaw_sync`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT pg_try_advisory_lock(hashtext('dalton:openclaw_sync')) AS locked"
        )
        row = await cur.fetchone()
        return bool(row and row.get("locked"))


async def release_sync_lock(conn: Any) -> None:
    """Release session-level advisory lock `dalton:openclaw_sync`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(hashtext('dalton:openclaw_sync'))")


async def create_sync_run(
    *,
    started_by: str,
    dry_run: bool,
    forced: bool = False,
    conn: Any | None = None,
) -> str:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_openclaw_sync_runs (started_by, dry_run, forced, status)
                VALUES (%s, %s, %s, 'running')
                RETURNING run_id::text AS run_id
                """,
                (started_by, dry_run, forced),
            )
            row = await cur.fetchone()
            assert row is not None
            return str(row["run_id"])


async def finish_sync_run(
    *,
    run_id: str,
    status: str,
    counts: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
    overlay_stamp: str | None = None,
    public_extract_sha256: str | None = None,
    conn: Any | None = None,
) -> None:
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_openclaw_sync_runs
                SET status = %s,
                    finished_at = NOW(),
                    counts = %s::jsonb,
                    diff = %s::jsonb,
                    warnings = %s::jsonb,
                    error = %s,
                    overlay_stamp = %s,
                    public_extract_sha256 = %s
                WHERE run_id = %s
                """,
                (
                    status,
                    json.dumps(counts) if counts is not None else None,
                    json.dumps(diff) if diff is not None else None,
                    json.dumps(warnings) if warnings is not None else None,
                    error,
                    overlay_stamp,
                    public_extract_sha256,
                    run_id,
                ),
            )


def _normalize_sync_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["run_id"] = str(out["run_id"])
    # P0.7 §3.4: mark `running` records older than 5 minutes as `interrupted` at read time
    if out.get("status") == "running" and out.get("is_stale_running"):
        out["status"] = "interrupted"
    out.pop("is_stale_running", None)
    for ts_key in ("started_at", "finished_at"):
        val = out.get(ts_key)
        if hasattr(val, "isoformat"):
            out[ts_key] = val.isoformat()
    return out


async def fetch_recent_sync_runs() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return `(last_run, last_succeeded_run)` from `dalton_openclaw_sync_runs`."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    *,
                    (status = 'running' AND started_at < NOW() - INTERVAL '5 minutes') AS is_stale_running
                FROM dalton_openclaw_sync_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            last_run = _normalize_sync_row(await cur.fetchone())

            await cur.execute(
                """
                SELECT
                    *,
                    FALSE AS is_stale_running
                FROM dalton_openclaw_sync_runs
                WHERE status = 'succeeded' AND dry_run = FALSE
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            last_succeeded = _normalize_sync_row(await cur.fetchone())
            return last_run, last_succeeded


async def find_user_referenced_models() -> dict[str, list[str]]:
    """Return `{model_key: [reference_source, ...]}` across all users' preferences and subagents (P0.7 §3.1)."""
    refs: dict[str, list[str]] = {}

    def _add(model_key: Any, source: str) -> None:
        if isinstance(model_key, str) and model_key.strip():
            refs.setdefault(model_key.strip(), []).append(source)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, model_preference, other_preference, agent_preference
                FROM user_preferences
                """
            )
            for row in await cur.fetchall():
                uid = str(row.get("user_id") or "user")
                mp = row.get("model_preference") or {}
                op = row.get("other_preference") or {}
                ap = row.get("agent_preference") or {}
                for bag in (mp, op):
                    if not isinstance(bag, dict):
                        continue
                    for k in (
                        "preferred_model",
                        "preferred_background_model",
                        "fetch_model",
                        "compaction_model",
                    ):
                        if bag.get(k):
                            _add(bag.get(k), f"user:{uid}:{k}")
                    fb = bag.get("fallback_models")
                    if isinstance(fb, list):
                        for item in fb:
                            _add(item, f"user:{uid}:fallback_models")

                if isinstance(ap, dict):
                    sub_models = ap.get("subagent_models") or ap.get("subagents") or {}
                    if isinstance(sub_models, dict):
                        for sname, sval in sub_models.items():
                            if isinstance(sval, str):
                                _add(sval, f"user:{uid}:subagent:{sname}")
                            elif isinstance(sval, dict) and sval.get("model"):
                                _add(sval.get("model"), f"user:{uid}:subagent:{sname}")

    return refs
