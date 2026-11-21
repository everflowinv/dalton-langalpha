#!/usr/bin/env python3
"""Controlled ops script for backfilling reasoning_effort='medium' on fixed Opus automations.

Scope (per locked spec):
  Only touches undeleted automations whose `llm_model` is explicitly fixed to a
  built-in Claude Opus 5.5 non-Antigravity catalog key (e.g. `claude-opus-5-5`,
  `claude-opus-5-5-oauth`, `claude-opus-5-5-code`) AND whose `reasoning_effort`
  is `NULL`. Automations following the default model (`llm_model IS NULL`),
  Antigravity suffixed keys (effort encoded in the ID), custom models, and
  deleted automations are excluded.

Modes:
  --dry-run [--out <plan.json>]  (default)
      Read-only scan (`SET TRANSACTION READ ONLY`). Prints automation_id, name,
      owner (user_id), current llm_model, and config_version for every target
      row, and optionally writes a 0600 plan JSON file.

  --apply [--plan <plan.json>] [--batch-id <id>] [--archive-dir <dir>]
      Generates a batch_id (unless supplied), locks user_preferences (user_id
      order) and target rows FOR UPDATE (automation_id order) in a single
      transaction, re-verifies deleted_at IS NULL, fixed Opus llm_model,
      reasoning_effort IS NULL, and config_version, archives pre-images (0600)
      under `var/dalton/operator-records/` before writing, updates
      `reasoning_effort = 'medium'` with `audit_history` entry carrying
      `action = 'backfill_opus_medium'` and `batch_id`, and asserts affected
      row count == dry-run count (rolling back the whole transaction otherwise).

  --rollback <batch_id> [--archive-dir <dir>]
      Reverts only rows belonging to `<batch_id>` that still have
      `reasoning_effort = 'medium'`, `config_version == after_config_version`,
      and a matching `audit_history` entry with `version == after_config_version`,
      `action == 'backfill_opus_medium'`, and `batch_id == <batch_id>`. Rows
      edited by users afterwards (or from unapplied/pending archives) are never
      reverted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.company_identity import is_company_system_user
from src.dalton.database.company_automations import (
    prepare_update_history_columns,
)
from src.dalton.llm.antigravity_series import ANTIGRAVITY_PROVIDER
from src.llms.llm import LLM as LLMFactory
from src.llms.model_spec import (
    canonical_reasoning_efforts,
    reasoning_block,
)
from src.server.database.pool import get_db_connection, get_or_create_pool
from src.server.database.user import lock_user_preferences

DEFAULT_ARCHIVE_DIR = Path("var/dalton/operator-records")
BACKFILL_ACTION = "backfill_opus_medium"
ROLLBACK_ACTION = "rollback_backfill_opus_medium"
OPS_ACTOR_USER_ID = "system:ops-backfill"
TARGET_EFFORT = "medium"

_ROW_SELECT_COLUMNS = """
    automation_id, user_id, name, file_name, description,
    trigger_type, cron_expression, timezone, trigger_config,
    next_run_at, last_run_at,
    instruction, workspace_id, llm_model, reasoning_effort, additional_context,
    thread_strategy, conversation_thread_id,
    status, max_failures, failure_count, disable_reason,
    delivery_config, metadata,
    scope, notify_enabled, notify_routes, notify_storage_mode,
    config_version, active_version, audit_history,
    created_by, updated_by, created_at, updated_at,
    deleted_at, deleted_by
"""


class OpsRefusalError(RuntimeError):
    """Raised when a precondition or locked recheck fails (CLI exit code 2)."""


def generate_batch_id(now: datetime | None = None) -> str:
    ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"opus-effort-{ts}-{uuid.uuid4().hex[:8]}"


def archive_path_for_batch(batch_id: str, archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> Path:
    safe_id = batch_id.strip()
    if not safe_id or "/" in safe_id or "\\" in safe_id or ".." in safe_id:
        raise OpsRefusalError(f"Invalid batch_id {batch_id!r}")
    return archive_dir / f"opus-effort-backfill-{safe_id}.json"


def _write_secure_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _history_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _has_matching_batch_audit_entry(
    audit_history: Any,
    *,
    version: int,
    batch_id: str,
) -> bool:
    for entry in _history_entries(audit_history):
        if (
            entry.get("version") == version
            and entry.get("action") == BACKFILL_ACTION
            and entry.get("batch_id") == batch_id
        ):
            return True
    return False


def eligible_opus_catalog_models(model_config: Any | None = None) -> set[str]:
    """Return built-in Claude Opus 5.5 non-Antigravity catalog keys that support ``medium``.

    Identified directly from the active model catalog (`LLM.get_model_config()`),
    excluding Antigravity channel keys (whose effort is encoded in the model ID).
    """
    mc = model_config if model_config is not None else LLMFactory.get_model_config()
    entries = getattr(mc, "llm_config", None) or {}
    eligible: set[str] = set()
    for key, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("provider") == ANTIGRAVITY_PROVIDER:
            continue
        model_id = str(entry.get("model_id") or "")
        if "opus" not in str(key).lower() and "opus" not in model_id.lower():
            continue
        block = reasoning_block(dict(entry))
        efforts = canonical_reasoning_efforts(block.get("efforts"))
        if len(efforts) > 1 and TARGET_EFFORT in efforts:
            eligible.add(str(key))
    return eligible


def _candidate_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "automation_id": str(row["automation_id"]),
        "name": str(row.get("name") or ""),
        "user_id": str(row.get("user_id") or ""),
        "llm_model": str(row.get("llm_model") or ""),
        "config_version": int(row.get("config_version") or 1),
    }


async def run_dry_run(
    *,
    conn: Any = None,
    out_path: Path | None = None,
    model_config: Any | None = None,
) -> dict[str, Any]:
    """Scan undeleted NULL-effort automations explicitly fixed to built-in Opus models."""
    eligible_models = sorted(eligible_opus_catalog_models(model_config))
    candidates: list[dict[str, Any]] = []

    if eligible_models:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.transaction():
                async with db_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET TRANSACTION READ ONLY")
                    await cur.execute(
                        f"""
                        SELECT {_ROW_SELECT_COLUMNS}
                        FROM automations
                        WHERE deleted_at IS NULL
                          AND reasoning_effort IS NULL
                          AND llm_model = ANY(%s)
                        ORDER BY automation_id
                        """,
                        (eligible_models,),
                    )
                    rows = await cur.fetchall()
            for row in rows or ():
                row_dict = dict(row)
                if row_dict.get("deleted_at") is not None:
                    continue
                if row_dict.get("reasoning_effort") is not None:
                    continue
                if str(row_dict.get("llm_model") or "") not in eligible_models:
                    continue
                candidates.append(_candidate_summary(row_dict))

    plan: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "target_effort": TARGET_EFFORT,
        "eligible_models": eligible_models,
        "expected_count": len(candidates),
        "candidates": candidates,
    }
    if out_path is not None:
        _write_secure_json(out_path, plan)
    return plan


async def _fetch_pref_rows_for_locked_users(
    conn: Any, user_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    prefs: dict[str, dict[str, Any]] = {}
    for uid in sorted({str(u) for u in user_ids if u and not is_company_system_user(str(u))}):
        await lock_user_preferences(uid, conn=conn)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT model_preference, other_preference
                FROM user_preferences
                WHERE user_id = %s
                """,
                (uid,),
            )
            row = await cur.fetchone()
        prefs[uid] = dict(row) if isinstance(row, Mapping) else {}
    return prefs


async def run_apply(
    *,
    plan_path: Path | None = None,
    plan_data: Mapping[str, Any] | None = None,
    batch_id: str | None = None,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    conn: Any = None,
    model_config: Any | None = None,
) -> dict[str, Any]:
    """Apply `reasoning_effort='medium'` to planned fixed-Opus automations in one transaction."""
    if plan_data is not None:
        plan = dict(plan_data)
    elif plan_path is not None:
        if not plan_path.exists():
            raise OpsRefusalError(f"Plan file not found: {plan_path}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        plan = await run_dry_run(conn=conn, model_config=model_config)

    candidates = [dict(c) for c in plan.get("candidates") or ()]
    expected_count = int(plan.get("expected_count", len(candidates)))
    if expected_count != len(candidates):
        raise OpsRefusalError(
            f"Plan expected_count={expected_count} does not match len(candidates)={len(candidates)}"
        )

    eff_batch_id = batch_id or generate_batch_id()
    archive_path = archive_path_for_batch(eff_batch_id, archive_dir)
    if archive_path.exists():
        raise OpsRefusalError(
            f"Archive for batch_id={eff_batch_id!r} already exists at {archive_path}"
        )

    eligible_models = eligible_opus_catalog_models(model_config)
    now_iso = datetime.now(UTC).isoformat()

    if not candidates:
        archive_payload: dict[str, Any] = {
            "batch_id": eff_batch_id,
            "action": BACKFILL_ACTION,
            "target_effort": TARGET_EFFORT,
            "status": "applied",
            "applied_at": now_iso,
            "expected_count": 0,
            "updated_count": 0,
            "rows": [],
        }
        _write_secure_json(archive_path, archive_payload)
        return {
            "batch_id": eff_batch_id,
            "archive_path": str(archive_path),
            "expected_count": 0,
            "updated_count": 0,
        }

    ordered_candidates = sorted(candidates, key=lambda c: str(c["automation_id"]))
    target_ids = [str(c["automation_id"]) for c in ordered_candidates]
    if len(set(target_ids)) != len(target_ids):
        raise OpsRefusalError("Plan contains duplicate automation_id entries")

    archive_rows: list[dict[str, Any]] = []
    updated_count = 0

    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            pref_users = [str(c["user_id"]) for c in ordered_candidates if c.get("user_id")]
            pref_rows = await _fetch_pref_rows_for_locked_users(db_conn, pref_users)

            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_ROW_SELECT_COLUMNS}
                    FROM automations
                    WHERE automation_id = ANY(%s::uuid[])
                    ORDER BY automation_id
                    FOR UPDATE
                    """,
                    (target_ids,),
                )
                locked_list = [dict(r) for r in (await cur.fetchall() or ())]

            locked_by_id = {str(r["automation_id"]): r for r in locked_list}
            if len(locked_by_id) != len(ordered_candidates):
                missing = sorted(set(target_ids) - set(locked_by_id))
                raise OpsRefusalError(
                    f"Locked row count mismatch: missing automation_id(s) {missing}"
                )

            staged_updates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            for item in ordered_candidates:
                aid = str(item["automation_id"])
                locked = locked_by_id[aid]
                if locked.get("deleted_at") is not None:
                    raise OpsRefusalError(f"Drift on {aid}: row was deleted since dry-run")
                if locked.get("reasoning_effort") is not None:
                    raise OpsRefusalError(
                        f"Drift on {aid}: reasoning_effort is no longer NULL "
                        f"({locked.get('reasoning_effort')!r})"
                    )
                expected_model = str(item["llm_model"])
                actual_model = str(locked.get("llm_model") or "")
                if actual_model != expected_model or actual_model not in eligible_models:
                    raise OpsRefusalError(
                        f"Drift on {aid}: llm_model changed from {expected_model!r} to {actual_model!r}"
                    )
                expected_cv = int(item["config_version"])
                actual_cv = int(locked.get("config_version") or 1)
                if actual_cv != expected_cv:
                    raise OpsRefusalError(
                        f"Drift on {aid}: config_version changed from {expected_cv} to {actual_cv}"
                    )

                owner_id = str(locked["user_id"])
                pref_row = (
                    {}
                    if str(locked.get("notify_storage_mode") or "legacy").strip() == "task_row"
                    else pref_rows.get(owner_id, {})
                )
                hist_cols, skipped = await prepare_update_history_columns(
                    locked,
                    {"reasoning_effort": TARGET_EFFORT},
                    actor_user_id=OPS_ACTOR_USER_ID,
                    action=BACKFILL_ACTION,
                    pref_row=pref_row,
                    changed_at_iso=now_iso,
                    skip_if_unchanged=False,
                    conn=db_conn,
                )
                if skipped or not hist_cols:
                    raise OpsRefusalError(f"History preparation unexpectedly skipped {aid}")

                history_list = list(hist_cols["audit_history"])
                history_list[-1] = {
                    **dict(history_list[-1]),
                    "batch_id": eff_batch_id,
                }
                hist_cols["audit_history"] = history_list

                archive_rows.append(
                    {
                        "automation_id": aid,
                        "name": str(locked.get("name") or ""),
                        "user_id": owner_id,
                        "llm_model": actual_model,
                        "before_reasoning_effort": locked.get("reasoning_effort"),
                        "after_reasoning_effort": TARGET_EFFORT,
                        "before_config_version": actual_cv,
                        "after_config_version": int(hist_cols["config_version"]),
                    }
                )
                staged_updates.append((locked, item, hist_cols))

            # Archive original values BEFORE issuing UPDATEs.
            pre_archive: dict[str, Any] = {
                "batch_id": eff_batch_id,
                "action": BACKFILL_ACTION,
                "target_effort": TARGET_EFFORT,
                "status": "pending",
                "created_at": now_iso,
                "expected_count": expected_count,
                "rows": archive_rows,
            }
            _write_secure_json(archive_path, pre_archive)

            for locked, item, hist_cols in staged_updates:
                aid = str(item["automation_id"])
                before_cv = int(locked.get("config_version") or 1)
                async with db_conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET reasoning_effort = %s,
                            config_version = %s,
                            active_version = %s,
                            updated_by = %s,
                            audit_history = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                          AND config_version = %s
                          AND reasoning_effort IS NULL
                          AND deleted_at IS NULL
                        """,
                        (
                            TARGET_EFFORT,
                            hist_cols["config_version"],
                            hist_cols["active_version"],
                            hist_cols["updated_by"],
                            Json(hist_cols["audit_history"]),
                            aid,
                            before_cv,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise OpsRefusalError(
                            f"Rowcount assertion failed on {aid}: expected 1, got {cur.rowcount}"
                        )
                    updated_count += cur.rowcount

            if updated_count != expected_count:
                raise OpsRefusalError(
                    f"Batch rowcount assertion failed: updated {updated_count} != expected {expected_count}"
                )

    final_archive = {
        **pre_archive,
        "status": "applied",
        "applied_at": datetime.now(UTC).isoformat(),
        "updated_count": updated_count,
    }
    _write_secure_json(archive_path, final_archive)

    return {
        "batch_id": eff_batch_id,
        "archive_path": str(archive_path),
        "expected_count": expected_count,
        "updated_count": updated_count,
    }


def _resolve_rollback_archive_path(batch_or_path: str, archive_dir: Path) -> Path:
    candidate = Path(batch_or_path)
    if candidate.exists() and candidate.is_file():
        return candidate
    return archive_path_for_batch(batch_or_path, archive_dir)


async def run_rollback(
    *,
    batch_or_path: str,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    conn: Any = None,
) -> dict[str, Any]:
    """Revert rows written by ``batch_id`` only if still untouched since that batch."""
    archive_path = _resolve_rollback_archive_path(batch_or_path, archive_dir)
    if not archive_path.exists():
        raise OpsRefusalError(f"Archive not found for {batch_or_path!r}: {archive_path}")

    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    batch_id = str(archive.get("batch_id") or "").strip()
    if not batch_id:
        raise OpsRefusalError(f"Archive {archive_path} is missing batch_id")
    if archive.get("status") != "applied":
        raise OpsRefusalError(
            f"Refusing rollback of archive {archive_path}: status is {archive.get('status')!r}, "
            "not 'applied' (the batch did not commit)"
        )

    rows = [dict(r) for r in archive.get("rows") or ()]
    if not rows:
        return {"batch_id": batch_id, "reverted": [], "skipped": []}

    ordered_rows = sorted(rows, key=lambda r: str(r["automation_id"]))
    target_ids = [str(r["automation_id"]) for r in ordered_rows]
    now_iso = datetime.now(UTC).isoformat()
    reverted: list[str] = []
    skipped: list[dict[str, str]] = []

    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            pref_users = [str(r["user_id"]) for r in ordered_rows if r.get("user_id")]
            pref_rows = await _fetch_pref_rows_for_locked_users(db_conn, pref_users)

            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_ROW_SELECT_COLUMNS}
                    FROM automations
                    WHERE automation_id = ANY(%s::uuid[])
                    ORDER BY automation_id
                    FOR UPDATE
                    """,
                    (target_ids,),
                )
                locked_by_id = {str(r["automation_id"]): dict(r) for r in (await cur.fetchall() or ())}

            for item in ordered_rows:
                aid = str(item["automation_id"])
                locked = locked_by_id.get(aid)
                if locked is None:
                    skipped.append({"automation_id": aid, "reason": "missing"})
                    continue
                if locked.get("deleted_at") is not None:
                    skipped.append({"automation_id": aid, "reason": "deleted"})
                    continue

                expected_after_cv = int(item["after_config_version"])
                actual_cv = int(locked.get("config_version") or 0)
                if actual_cv != expected_after_cv:
                    skipped.append(
                        {
                            "automation_id": aid,
                            "reason": f"version_changed:{actual_cv}!={expected_after_cv}",
                        }
                    )
                    continue
                if locked.get("reasoning_effort") != TARGET_EFFORT:
                    skipped.append(
                        {
                            "automation_id": aid,
                            "reason": f"effort_changed:{locked.get('reasoning_effort')!r}",
                        }
                    )
                    continue
                if not _has_matching_batch_audit_entry(
                    locked.get("audit_history"),
                    version=expected_after_cv,
                    batch_id=batch_id,
                ):
                    skipped.append(
                        {
                            "automation_id": aid,
                            "reason": "missing_matching_batch_audit_entry",
                        }
                    )
                    continue

                before_effort = item.get("before_reasoning_effort")
                owner_id = str(locked["user_id"])
                pref_row = (
                    {}
                    if str(locked.get("notify_storage_mode") or "legacy").strip() == "task_row"
                    else pref_rows.get(owner_id, {})
                )
                hist_cols, _ = await prepare_update_history_columns(
                    locked,
                    {"reasoning_effort": before_effort},
                    actor_user_id=OPS_ACTOR_USER_ID,
                    action=ROLLBACK_ACTION,
                    pref_row=pref_row,
                    changed_at_iso=now_iso,
                    skip_if_unchanged=False,
                    conn=db_conn,
                )
                history_list = list(hist_cols["audit_history"])
                history_list[-1] = {
                    **dict(history_list[-1]),
                    "rollback_of_batch_id": batch_id,
                }
                hist_cols["audit_history"] = history_list

                async with db_conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET reasoning_effort = %s,
                            config_version = %s,
                            active_version = %s,
                            updated_by = %s,
                            audit_history = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                          AND config_version = %s
                          AND reasoning_effort = %s
                          AND deleted_at IS NULL
                        """,
                        (
                            before_effort,
                            hist_cols["config_version"],
                            hist_cols["active_version"],
                            hist_cols["updated_by"],
                            Json(hist_cols["audit_history"]),
                            aid,
                            expected_after_cv,
                            TARGET_EFFORT,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise OpsRefusalError(
                            f"Rollback rowcount assertion failed on {aid}: expected 1, got {cur.rowcount}"
                        )
                reverted.append(aid)

    return {
        "batch_id": batch_id,
        "reverted": reverted,
        "skipped": skipped,
    }


def _print_dry_run(plan: Mapping[str, Any]) -> None:
    candidates = plan.get("candidates") or ()
    print(f"Eligible Opus catalog models: {', '.join(plan.get('eligible_models') or ())}")
    print(f"Candidates ({len(candidates)}):")
    for item in candidates:
        print(
            f"  {item['automation_id']} | name={item['name']!r} | owner={item['user_id']} "
            f"| model={item['llm_model']} | version={item['config_version']}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill reasoning_effort='medium' for undeleted automations explicitly "
            "fixed to built-in Claude Opus 5.5 models (excluding Antigravity suffixed keys)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preview listing target automations (default mode).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply reasoning_effort='medium' in a locked transaction with archive & rowcount check.",
    )
    mode.add_argument(
        "--rollback",
        metavar="BATCH_ID",
        help="Rollback a previously applied batch by batch_id (or archive path).",
    )
    parser.add_argument("--out", type=Path, help="Optional output path for --dry-run plan JSON.")
    parser.add_argument("--plan", type=Path, help="Optional reviewed plan JSON for --apply.")
    parser.add_argument("--batch-id", help="Optional explicit batch_id for --apply.")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help=f"Directory for pre-write archives (default: {DEFAULT_ARCHIVE_DIR}).",
    )
    return parser


async def _async_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    pool = get_or_create_pool()
    await pool.open()
    try:
        if args.rollback:
            result = await run_rollback(
                batch_or_path=args.rollback,
                archive_dir=args.archive_dir,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.apply:
            result = await run_apply(
                plan_path=args.plan,
                batch_id=args.batch_id,
                archive_dir=args.archive_dir,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        plan = await run_dry_run(out_path=args.out)
        _print_dry_run(plan)
        if args.out:
            print(f"Plan written to {args.out}")
        return 0
    except OpsRefusalError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
