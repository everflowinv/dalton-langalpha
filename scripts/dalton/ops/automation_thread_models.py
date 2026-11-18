#!/usr/bin/env python3
"""Read-only audit of automation executions whose notification turns changed model.

Locates each execution's first turn by ``metadata->>'automation_execution_id'``
(falling back to the earliest ``origin.type = 'automation'`` turn on the thread
at or after ``started_at`` — never via ``conversation_response_id``, which
``take_over_execution_run`` advances to continuation turns).

Only compares notification turns (``system``, ``wakeup``, ``market_target`` or
corresponding ``run_origin``) inside ``--days`` that occurred before the
execution's ``completed_at`` (unsettled executions have no upper bound). User
turns are excluded. Executions whose first turn lacks ``llm_model`` are skipped
and counted separately, and soft-deleted automations are explicitly annotated.

Usage:
    DATABASE_URL=postgresql://... .venv/bin/python scripts/dalton/ops/automation_thread_models.py
    .venv/bin/python scripts/dalton/ops/automation_thread_models.py --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

READ_ONLY_SQL = "SET TRANSACTION READ ONLY"

_FIRST_TURN_LATERAL_SQL = """
    LEFT JOIN LATERAL (
        SELECT
            q.turn_index AS first_turn_index,
            NULLIF(q.metadata->>'llm_model', '') AS first_model,
            q.created_at AS first_turn_at
        FROM conversation_queries q
        LEFT JOIN conversation_responses r
          ON r.conversation_thread_id = q.conversation_thread_id
         AND r.turn_index = q.turn_index
        WHERE q.conversation_thread_id = e.conversation_thread_id
          AND (
              (
                  r.metadata->>'automation_execution_id' = e.automation_execution_id::text
                  AND q.type NOT IN ('system', 'wakeup', 'market_target')
              )
              OR (
                  COALESCE(e.started_at, e.created_at) IS NOT NULL
                  AND q.created_at >= COALESCE(e.started_at, e.created_at)
                  AND q.metadata->'origin'->>'type' = 'automation'
              )
          )
        ORDER BY
            (r.metadata->>'automation_execution_id' = e.automation_execution_id::text) DESC NULLS LAST,
            q.turn_index ASC,
            q.created_at ASC
        LIMIT 1
    ) ft ON TRUE
"""

SKIPPED_COUNT_SQL = f"""
SELECT COUNT(DISTINCT e.automation_execution_id)::int AS skipped_count
FROM automation_executions e
{_FIRST_TURN_LATERAL_SQL}
WHERE e.conversation_thread_id IS NOT NULL
  AND ft.first_turn_index IS NOT NULL
  AND ft.first_model IS NULL
  AND ft.first_turn_at >= NOW() - make_interval(days => %(days)s)
"""

MISMATCH_QUERY_SQL = f"""
WITH exec_baselines AS (
    SELECT
        e.automation_execution_id,
        e.automation_id,
        e.conversation_thread_id,
        e.status AS execution_status,
        e.started_at AS execution_started_at,
        e.completed_at AS execution_completed_at,
        a.name AS automation_name,
        a.thread_strategy,
        a.llm_model AS automation_llm_model,
        (a.deleted_at IS NOT NULL) AS automation_deleted,
        a.deleted_at AS automation_deleted_at,
        t.llm_model AS thread_llm_model,
        ft.first_turn_index,
        ft.first_model,
        ft.first_turn_at
    FROM automation_executions e
    JOIN automations a
      ON a.automation_id = e.automation_id
    JOIN conversation_threads t
      ON t.conversation_thread_id = e.conversation_thread_id
    {_FIRST_TURN_LATERAL_SQL}
    WHERE e.conversation_thread_id IS NOT NULL
      AND ft.first_turn_index IS NOT NULL
      AND ft.first_model IS NOT NULL
),
window_notification_turns AS (
    SELECT DISTINCT ON (q.conversation_thread_id, q.turn_index)
        q.conversation_thread_id,
        q.turn_index AS later_turn_index,
        q.type AS later_query_type,
        NULLIF(q.metadata->>'llm_model', '') AS later_model,
        q.created_at AS later_turn_at,
        r.metadata->>'automation_execution_id' AS explicit_execution_id
    FROM conversation_queries q
    LEFT JOIN conversation_responses r
      ON r.conversation_thread_id = q.conversation_thread_id
     AND r.turn_index = q.turn_index
    WHERE q.created_at >= NOW() - make_interval(days => %(days)s)
      AND (
          q.type IN ('system', 'wakeup', 'market_target')
          OR r.metadata->>'run_origin' IN ('report', 'wakeup', 'market_target')
      )
    ORDER BY q.conversation_thread_id, q.turn_index, r.attempt_no DESC NULLS LAST
),
attributed_turns AS (
    SELECT
        wt.later_turn_index,
        wt.later_query_type,
        wt.later_model,
        wt.later_turn_at,
        eb.*
    FROM window_notification_turns wt
    JOIN LATERAL (
        SELECT *
        FROM exec_baselines eb
        WHERE eb.conversation_thread_id = wt.conversation_thread_id
          AND eb.first_turn_index < wt.later_turn_index
          AND (
              eb.execution_completed_at IS NULL
              OR wt.later_turn_at <= eb.execution_completed_at
          )
          AND (
              wt.explicit_execution_id IS NULL
              OR eb.automation_execution_id::text = wt.explicit_execution_id
          )
        ORDER BY eb.first_turn_index DESC
        LIMIT 1
    ) eb ON TRUE
)
SELECT
    conversation_thread_id::text AS thread_id,
    automation_execution_id::text AS execution_id,
    automation_id::text AS automation_id,
    automation_name,
    automation_deleted,
    automation_deleted_at,
    execution_status,
    execution_completed_at,
    thread_strategy,
    automation_llm_model,
    thread_llm_model,
    first_turn_index,
    first_model,
    first_turn_at,
    later_turn_index,
    later_query_type,
    later_model,
    later_turn_at
FROM attributed_turns
WHERE later_model IS DISTINCT FROM first_model
ORDER BY later_turn_at DESC, conversation_thread_id, later_turn_index ASC
"""


@dataclass(frozen=True)
class AuditResult:
    """Audit findings across automation executions in the requested window."""

    mismatches: list[dict[str, Any]]
    skipped_missing_first_model: int


def _read_database_url(explicit_url: str | None = None) -> str:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key.strip() == "DATABASE_URL":
                return val.strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL is required (via --database-url, env, or .env)")


def open_read_only_connection(database_url: str | None = None) -> psycopg.Connection[Any]:
    url = _read_database_url(database_url)
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.read_only = True
    return conn


def fetch_model_audit(
    *,
    days: int = 7,
    conn: psycopg.Connection[Any] | None = None,
    database_url: str | None = None,
) -> AuditResult:
    """Audit automation executions in the last ``days`` days for notification turn model drift."""
    if days <= 0:
        raise ValueError("days must be positive")
    owns_conn = conn is None
    active_conn = conn if conn is not None else open_read_only_connection(database_url)
    try:
        with active_conn.cursor() as cur:
            cur.execute(READ_ONLY_SQL)
            cur.execute(SKIPPED_COUNT_SQL, {"days": days})
            skipped_row = cur.fetchone()
            skipped_count = int((skipped_row or {}).get("skipped_count") or 0)
            cur.execute(MISMATCH_QUERY_SQL, {"days": days})
            rows = [dict(row) for row in cur.fetchall()]
            return AuditResult(
                mismatches=rows,
                skipped_missing_first_model=skipped_count,
            )
    finally:
        if owns_conn:
            active_conn.close()


def format_report(result: AuditResult, *, days: int) -> str:
    rows = result.mismatches
    skipped = result.skipped_missing_first_model
    skipped_note = f"Skipped executions with missing first-turn llm_model: {skipped}"
    if not rows:
        return (
            f"No automation executions with first-turn != notification-turn llm_model "
            f"in the last {days} day(s).\n{skipped_note}"
        )
    executions = len({r["execution_id"] for r in rows})
    threads = len({r["thread_id"] for r in rows})
    lines = [
        f"Found {len(rows)} mismatched notification turn(s) across {executions} execution(s) "
        f"({threads} thread(s)) in the last {days} day(s):",
        skipped_note,
    ]
    for r in rows:
        deleted_tag = " [SOFT-DELETED]" if r.get("automation_deleted") else ""
        lines.append(
            "  "
            f"exec={r['execution_id']} "
            f"thread={r['thread_id']} "
            f"automation={r.get('automation_name') or r.get('automation_id') or '-'}{deleted_tag} "
            f"strategy={r.get('thread_strategy') or '-'} "
            f"auto_model={r.get('automation_llm_model') or 'NULL'} "
            f"thread_model={r.get('thread_llm_model') or 'NULL'} "
            f"turn[{r['first_turn_index']}]={r.get('first_model') or 'NULL'} -> "
            f"turn[{r['later_turn_index']}]({r.get('later_query_type') or '-'})="
            f"{r.get('later_model') or 'NULL'} "
            f"at={r.get('later_turn_at')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check for automation executions with notification-turn model drift."
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    result = fetch_model_audit(days=args.days, database_url=args.database_url)
    if args.json:
        payload = {
            "days": args.days,
            "skipped_missing_first_model": result.skipped_missing_first_model,
            "mismatches": result.mismatches,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(result, days=args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
