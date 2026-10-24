#!/usr/bin/env python3
"""Prove `reextract_conversations.py apply` + `rollback` on a real Postgres copy of the cards table.

Lead-run only, through `reextract_scratch_check.sh`, which creates the scratch database first.
The unit tests use an in-memory double; this is the check that the SQL itself (casts, the
self-referencing FK, UNIQUE content_hash, the insert path) behaves on PostgreSQL 18 + pgvector.

It builds a synthetic staged run from the scratch table's own cards, so no LLM is called: for
half of the chosen old cards a new card with the same quote and event (a content_hash
collision), for the other half a card with a new quote (a fresh insert). It then runs apply
(dry run, then for real) and rollback through the script's own functions and compares the
table before and after: row count and an md5 over every column of every row in card_id order
(embedding, timestamps and arrays as text), except `files_written_at`, which rollback clears
on purpose on the run's dates so render rewrites them; rows on every other date must match
including the stamp. Constraint definitions and FK/unique integrity are checked too.
Exit 0 = PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from scripts.dalton import reextract_conversations as rx  # noqa: E402
from src.dalton.conversations.extractor import compute_content_hash  # noqa: E402
from src.dalton.database.conversations import build_card_search_text  # noqa: E402
from src.dalton.settings import get_dalton_settings  # noqa: E402


def scratch_dsn(scratch_db: str) -> str:
    """The app's own connection settings with only the database name swapped."""
    from src.server.database.pool import get_db_connection_string

    params = conninfo_to_dict(get_db_connection_string())
    app_db = str(params.get("dbname") or "")
    if "scratch" not in scratch_db or scratch_db == app_db:
        raise SystemExit(f"refusing: scratch db {scratch_db!r} must contain 'scratch' and differ from the app db")
    params["dbname"] = scratch_db
    if os.environ.get("SCRATCH_DB_HOST"):
        params["host"] = os.environ["SCRATCH_DB_HOST"]
    if os.environ.get("SCRATCH_DB_PORT"):
        params["port"] = os.environ["SCRATCH_DB_PORT"]
    return make_conninfo(**params)


async def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return [dict(r) for r in (await cur.fetchall() or [])]


def _row_expr(include_files_written_at: bool) -> str:
    cols = [
        name
        for name, _ in rx.FULL_ROW_COLUMNS
        if include_files_written_at or name != "files_written_at"
    ]
    return "concat_ws(chr(31), " + ", ".join(f"COALESCE({c}::text, '<NULL>')" for c in cols) + ")"


async def row_hashes(conn: Any) -> dict[str, tuple[str, str]]:
    """card_id -> (date_et, md5 over every column, stamp included)."""
    rows = await _rows(
        conn, f"SELECT card_id, date_et::text AS date_et, md5({_row_expr(True)}) AS h FROM dalton_conversation_cards"
    )
    return {str(r["card_id"]): (str(r["date_et"])[:10], str(r["h"])) for r in rows}


def outside_dates(hashes: dict[str, tuple[str, str]], dates: list[str]) -> dict[str, str]:
    skip = set(dates)
    return {cid: h for cid, (d, h) in hashes.items() if d not in skip}


async def fingerprint(conn: Any) -> dict[str, Any]:
    rows = await _rows(
        conn,
        f"""
        SELECT COUNT(*)::int AS n,
               md5(COALESCE(string_agg({_row_expr(False)}, chr(30) ORDER BY card_id), '')) AS md5_business,
               md5(COALESCE(string_agg({_row_expr(True)}, chr(30) ORDER BY card_id), '')) AS md5_all
        FROM dalton_conversation_cards
        """,
    )
    constraints = await _rows(
        conn,
        """
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'dalton_conversation_cards'::regclass
        ORDER BY conname
        """,
    )
    integrity = await _rows(
        conn,
        """
        SELECT
          (SELECT COUNT(*)::int FROM dalton_conversation_cards c
            WHERE c.supersedes IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM dalton_conversation_cards p WHERE p.card_id = c.supersedes)
          ) AS dangling_supersedes,
          (SELECT COUNT(*)::int FROM (
              SELECT content_hash FROM dalton_conversation_cards GROUP BY 1 HAVING COUNT(*) > 1
          ) d) AS duplicate_hashes
        """,
    )
    return {**rows[0], "constraints": constraints, **integrity[0]}


def _new_card_from(old: dict[str, Any], *, prompt_version: str, model: str, fresh: bool) -> dict[str, Any]:
    card = {
        k: old[k]
        for k, _ in rx.FULL_ROW_COLUMNS
        if k not in ("embedding", "rendered_locations", "files_written_at", "created_at", "supersedes", "status")
    }
    card.update(
        {
            "status": "active",
            "supersedes": None,
            "superseded_card_ids": [],
            "prompt_version": prompt_version,
            "extractor_model": model,
            "claim": f"[scratch] {old['claim']}",
        }
    )
    if fresh:
        card["quote"] = f"{old['quote']} [scratch]"
        h = compute_content_hash(card["quote"], card["quote_event_id"])
        card["content_hash"] = h
        card["card_id"] = f"cc_{h[:24]}"
    card["search_text"] = build_card_search_text(card)
    return json.loads(json.dumps(card, default=str))


async def build_synthetic_run(conn: Any, run_dir: Path, *, retract_pv: str, n_cards: int) -> dict[str, Any]:
    cfg = get_dalton_settings().conversations
    old = await _rows(
        conn,
        """
        SELECT card_id, agent, session_id, session_key, generation, channel, chat_type, event_ids,
               seq_range, ts::text AS ts, date_et::text AS date_et, speaker, speaker_role, tickers,
               entities, sectors, topic_label, type, claim, figures, quote, quote_event_id,
               cited_sources, confidence, extractor_model, prompt_version, content_hash,
               search_text, source_type
        FROM dalton_conversation_cards
        WHERE prompt_version = %s AND status = 'active'
        ORDER BY (figures <> '[]'::jsonb) DESC, card_id
        LIMIT %s
        """,
        (retract_pv, int(n_cards)),
    )
    if not old:
        raise SystemExit(f"no active {retract_pv} cards in the scratch table")
    segments: dict[str, dict[str, Any]] = {}
    records: dict[str, list[dict[str, Any]]] = {}
    for i, card in enumerate(old):
        events = [
            {
                "agent": card["agent"],
                "session_id": card["session_id"],
                "event_id": eid,
                "seq": j,
                "ts": card["ts"],
                "date_et": card["date_et"],
                "text": "scratch",
            }
            for j, eid in enumerate(card["event_ids"] or [card["quote_event_id"]])
        ]
        key = rx.segment_key(events)
        if key not in segments:
            segments[key] = {"idx": len(segments), "key": key, "window": [card["date_et"]] * 2, "chars": 7, "events": events}
            records[key] = []
        records[key].append(
            _new_card_from(card, prompt_version=cfg.prompt_version, model=rx.DEFAULT_MODEL, fresh=bool(i % 2))
        )
    seg_list = sorted(segments.values(), key=lambda s: s["idx"])
    dates = sorted(c["date_et"] for c in old)
    rx.write_jsonl_atomic(run_dir / rx.SEGMENTS_FILE, seg_list)
    rx.write_json_atomic(
        run_dir / rx.PLAN_FILE,
        {
            "run_id": run_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "retract_prompt_version": retract_pv,
            "prompt_version": cfg.prompt_version,
            "model": rx.DEFAULT_MODEL,
            "fallback_model": rx.DEFAULT_FALLBACK_MODEL,
            "extract_runs": [],
            "export_since_until": [dates[0], dates[-1]],
            "export": {"agents_present": ["scratch"], "missing_agents": [], "events_parse_failed": 0},
            "summary": {"segments": len(seg_list)},
            "segments_meta": rx._segments_meta(seg_list),
        },
    )
    lines = [
        {
            "key": s["key"],
            "idx": s["idx"],
            "ok": True,
            "attempt": "primary",
            "model_requested": rx.DEFAULT_MODEL,
            "model_used": rx.DEFAULT_MODEL,
            "prompt_version": cfg.prompt_version,
            "tokens": {"in": 0, "out": 0, "total": 0},
            "cards": records[s["key"]],
        }
        for s in seg_list
    ]
    rx.write_jsonl_atomic(run_dir / rx.STAGED_FILE, lines)
    return {"old_cards": len(old), "segments": len(seg_list), "staged_cards": sum(len(v) for v in records.values())}


async def main_async(args: argparse.Namespace) -> int:
    dsn = scratch_dsn(args.scratch_db)
    retract_pv = args.retract_prompt_version
    run_dir = rx.RUNS_ROOT / f"scratch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    failures: list[str] = []
    out: dict[str, Any] = {"run_dir": str(run_dir)}

    async def open_conn() -> Any:
        return await rx._open_autocommit_conn(dsn)

    conn = await open_conn()
    try:
        before = await fingerprint(conn)
        out["synthetic"] = await build_synthetic_run(conn, run_dir, retract_pv=retract_pv, n_cards=args.cards)
        # Hashed after the run is built, so the rows outside its dates can be compared later.
        hashes_before = await row_hashes(conn)
    finally:
        await conn.close()

    # The scratch database has its own advisory-lock namespace; skip the host pid-file check.
    async with rx.archive_guard(open_conn=open_conn, pid_files=[]) as lock_conn:
        store = rx.CardStore(lock_conn)
        dry = await rx.apply_run(run_dir, store, dry_run=True)
        after_dry = await fingerprint(lock_conn)
        if after_dry != before:
            failures.append("apply --dry-run changed the table")
        applied = await rx.apply_run(run_dir, store, dry_run=False)
        after_apply = await fingerprint(lock_conn)
        if after_apply["n"] != before["n"] + int(applied["inserted_new"]):
            failures.append("row count after apply != before + inserted_new")
        if after_apply["dangling_supersedes"] or after_apply["duplicate_hashes"]:
            failures.append("integrity broken after apply")
        rolled = await rx.rollback_run(run_dir, store, dry_run=False)
        after = await fingerprint(lock_conn)
        rollback_dates = list(rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE).get("rollback_dates") or [])
        stamps = await _rows(
            lock_conn,
            "SELECT COUNT(*)::int AS n FROM dalton_conversation_cards"
            " WHERE files_written_at IS NOT NULL AND date_et = ANY(%s::date[])",
            (rollback_dates,),
        )
        outside_after = outside_dates(await row_hashes(lock_conn), rollback_dates)
    outside_before = outside_dates(hashes_before, rollback_dates)

    for key in ("n", "md5_business", "constraints"):
        if after[key] != before[key]:
            failures.append(f"{key} differs after rollback")
    if after["dangling_supersedes"] or after["duplicate_hashes"]:
        failures.append("integrity broken after rollback")
    if not rollback_dates:
        failures.append("rollback recorded no rollback_dates")
    if stamps[0]["n"] != 0:
        failures.append("files_written_at not cleared on the rollback's dates")
    if outside_after != outside_before:
        failures.append("rows outside the rollback's dates changed (stamp included)")
    out.update(
        {
            "before": {k: before[k] for k in ("n", "md5_business", "md5_all")},
            "after_apply": {k: after_apply[k] for k in ("n", "dangling_supersedes", "duplicate_hashes")},
            "after_rollback": {k: after[k] for k in ("n", "md5_business", "md5_all")},
            "constraints": [c["conname"] for c in before["constraints"]],
            "apply_dry_run": {k: dry.get(k) for k in ("old_cards_to_retract", "content_hash_collisions", "inserted_new", "inserted_replacing_collision")},
            "apply": {k: applied.get(k) for k in ("retracted", "content_hash_collisions", "inserted_new", "inserted_replacing_collision", "uncovered_old_cards")},
            "rollback": {k: rolled.get(k) for k in ("new_cards_deleted", "replaced_rows_reinserted", "snapshot_rows_restored", "dates_to_rerender")},
            "rows_outside_rollback_dates_unchanged": outside_after == outside_before,
            "note": "md5_all differs by design: rollback clears files_written_at on its dates so render rewrites them",
            "failures": failures,
            "result": "PASS" if not failures else "FAIL",
        }
    )
    if applied.get("content_hash_collisions", 0) == 0 or applied.get("inserted_new", 0) == 0:
        out["warning"] = "the synthetic run did not exercise both the collision and the fresh path"
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scratch-db", default="dalton_reextract_scratch")
    parser.add_argument("--retract-prompt-version", default=rx.DEFAULT_RETRACT_PROMPT_VERSION)
    parser.add_argument("--cards", type=int, default=200, help="Old cards to build the synthetic run from")
    args = parser.parse_args(argv)
    # .env may hold unquoted values with spaces, which a shell `source` would execute as
    # commands; python-dotenv reads KEY=VALUE lines without a shell (note: unlike compose's
    # env_file, it still expands $VAR references in unquoted/double-quoted values).
    load_dotenv(REPO_ROOT / ".env", override=False)
    rx.configure_archive_logging()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
