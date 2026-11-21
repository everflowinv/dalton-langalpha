#!/usr/bin/env python3
"""Controlled ops script for backfilling reasoning_effort='medium' on legacy Opus automations.

Design reference: .agy/design.md §3.4 & §7 (PR 3).

Modes:
  --dry-run [--out <plan.json>]
      Read-only scan (SET TRANSACTION READ ONLY). Resolves each NULL-effort
      automation's real effective model using the executor's resolution chain,
      lists eligible non-Antigravity multi-tier Claude Opus automations, flags
      any user preference effort conflict (!= 'medium') or prior backfill, and
      optionally writes a deterministic 0600 plan file.

  --apply --plan <plan.json> [--allow-override-ids id1,id2] [--archive <path>] [--sentinel <path>]
      Consumes a reviewed plan file in a single transaction: locks
      user_preferences (user_id order) and target rows FOR UPDATE (automation_id
      order), re-verifies versions, llm_model, NULL reasoning_effort, prefs, and
      model resolution under lock, writes pre-image archive (0600), updates
      reasoning_effort='medium' with audit_history action='backfill_opus_medium',
      asserts exact affected row count, and writes the batch sentinel file.
      Exits with code 2 on sentinel presence, unapproved conflicts, prior
      backfill history, or any drift under lock (rolling back cleanly).

  --rollback <archive.json>
      Reverts rows still at post_config_version with reasoning_effort='medium'
      back to NULL in a single locked transaction; skips rows edited by users
      after backfill and prints a summary.

  --export-downgrade --out <export.json>
      Exports all non-NULL reasoning_effort rows (0600) prior to `alembic downgrade d041`.

  --restore-downgrade <export.json>
      Re-applies exported reasoning_effort values after re-upgrading to d042
      only where config_version and llm_model remained unchanged during downgrade.

  --reconcile-stale [--apply]
      Finds (and with --apply, clears) stale reasoning_effort values left behind
      when pre-d042 code edited llm_model on a row with the d042 column retained.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.database.company_automations import (
    prepare_update_history_columns,
)
from src.dalton.llm.antigravity_series import ANTIGRAVITY_PROVIDER
from src.llms.llm import LLM as LLMFactory
from src.llms.model_spec import (
    canonical_reasoning_efforts,
    clamp_reasoning_effort,
    default_reasoning_effort,
    reasoning_block,
)
from src.llms.preferences import resolve_turn_tuning
from src.server.database.home_workspace import is_home_id
from src.server.database.pool import get_db_connection, get_or_create_pool
from src.server.database.user import lock_user_preferences
from src.server.services.account import own_access, require_policy
from src.server.services.llm import user_models
from src.server.services.llm.availability import builtins_only, drop_dead_models
from src.server.services.llm.capabilities import PlanTier
from src.server.services.llm.claude_code import (
    chief_of_staff_default_runs,
    drop_claude_code_roles,
)
from src.server.services.llm.config import (
    _ROLE_PREF_KEYS,
    _SLOT_MODEL_MAP,
    default_to_first_allowed,
    gate_models_by_plan,
    gate_models_by_policy,
    select_model,
)
from src.server.services.llm.user_models import ModelSource

DEFAULT_OPERATOR_DIR = Path("var/dalton/operator-records")
DEFAULT_SENTINEL_PATH = DEFAULT_OPERATOR_DIR / "opus-automation-effort-applied.json"
BACKFILL_ACTION = "backfill_opus_medium"
ROLLBACK_ACTION = "rollback_backfill_opus_medium"
RECONCILE_STALE_ACTION = "reconcile_stale_reasoning_effort"
RESTORE_DOWNGRADE_ACTION = "restore_downgrade_reasoning_effort"
OPS_ACTOR_USER_ID = "system:ops-backfill"
TARGET_EFFORT = "medium"


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


def _model_pref_digest(model_pref: Mapping[str, Any] | None) -> str:
    canonical = json.dumps(
        dict(model_pref or {}),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _history_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _has_backfill_history(audit_history: Any) -> bool:
    return any(entry.get("action") == BACKFILL_ACTION for entry in _history_entries(audit_history))


def _is_multi_tier_non_antigravity_opus(
    model_name: str,
    source: str,
    entry: Mapping[str, Any] | None,
) -> tuple[bool, tuple[str, ...], str | None]:
    """Return ``(eligible, efforts, model_default)`` for a system built-in multi-tier Opus model."""
    if source != ModelSource.SYSTEM or not isinstance(entry, Mapping):
        return False, (), None
    if entry.get("provider") == ANTIGRAVITY_PROVIDER:
        return False, (), None
    model_id = str(entry.get("model_id") or "")
    if "opus" not in model_name.lower() and "opus" not in model_id.lower():
        return False, (), None
    block = reasoning_block(dict(entry))
    efforts = canonical_reasoning_efforts(block.get("efforts"))
    model_default = default_reasoning_effort(efforts, block.get("default"))
    if len(efforts) <= 1 or TARGET_EFFORT not in efforts:
        return False, efforts, model_default
    return True, efforts, model_default


async def _load_base_agent_config() -> Any:
    from src.server.app import setup

    if getattr(setup, "agent_config", None) is not None:
        return setup.agent_config
    from ptc_agent.config import load_from_files

    return await load_from_files()


async def resolve_effective_model_and_effort(
    *,
    base_config: Any,
    user_id: str,
    workspace_id: str | None,
    row_llm_model: str | None,
    raw_pref: dict[str, Any],
) -> tuple[str | None, str, dict[str, Any], str | None]:
    """Mirror ``resolve_llm_config`` selection rules and compute current effective effort.

    Returns ``(effective_model, model_source, model_entry, current_effective_effort)``.
    """
    from src.llms.claude_code import FEATURE, is_claude_code_model, rows_available

    policy = await require_policy(user_id)
    is_byok = policy.byok(
        (await own_access(user_id, sign_ins=False, policy=policy)).api_keys
    )
    config = base_config.model_copy(deep=True)
    saved_pref = builtins_only(policy, dict(raw_pref))
    role_keys = _ROLE_PREF_KEYS["primary"]
    model_pref, _ = drop_dead_models(saved_pref, role_keys)
    model_pref, _ = drop_claude_code_roles(model_pref, role_keys)
    model_pref = gate_models_by_policy(
        user_id, model_pref, row_llm_model, policy=policy
    )
    permits = PlanTier(user_id)
    model_pref = await gate_models_by_plan(
        base_config,
        user_id,
        model_pref,
        row_llm_model,
        slot="primary",
        permits=permits,
        is_byok=is_byok,
    )
    default_key = _SLOT_MODEL_MAP["primary"][1]
    in_home = bool(workspace_id and is_home_id(user_id, workspace_id))
    if (
        in_home
        and row_llm_model is None
        and is_claude_code_model(model_pref.get(default_key) or "")
    ):
        where = None
        if rows_available() and config.feature_enabled(FEATURE):
            where = await chief_of_staff_default_runs(user_id, workspace_id)
        if where is None:
            model_pref, _ = drop_claude_code_roles(model_pref, [default_key])

    await default_to_first_allowed(
        config,
        user_id,
        model_pref,
        "primary",
        row_llm_model,
        policy=policy,
        permits=permits,
        is_byok=is_byok,
    )
    selection = select_model(config, model_pref, "primary", row_llm_model)
    effective_model = selection.effective_model
    source, entry = await user_models.classify_model(
        user_id, effective_model, _pref_cache=raw_pref
    )
    entry_dict = dict(entry) if isinstance(entry, Mapping) else {}
    block = reasoning_block(entry_dict)
    efforts = canonical_reasoning_efforts(block.get("efforts"))
    model_default = default_reasoning_effort(efforts, block.get("default"))
    tuning = resolve_turn_tuning(model_pref, effective_model)
    if tuning.reasoning_effort:
        current_effort = clamp_reasoning_effort(
            efforts, model_default, tuning.reasoning_effort
        )
    else:
        current_effort = model_default
    return effective_model, source, entry_dict, current_effort


async def _evaluate_candidate_row(
    row: Mapping[str, Any],
    *,
    base_config: Any,
    raw_pref: dict[str, Any],
) -> dict[str, Any] | None:
    user_id = str(row["user_id"])
    workspace_id = str(row["workspace_id"]) if row.get("workspace_id") is not None else None
    row_llm_model = str(row["llm_model"]) if row.get("llm_model") is not None else None
    effective_model, source, entry, current_effort = await resolve_effective_model_and_effort(
        base_config=base_config,
        user_id=user_id,
        workspace_id=workspace_id,
        row_llm_model=row_llm_model,
        raw_pref=raw_pref,
    )
    if not effective_model:
        return None
    eligible, _efforts, _default = _is_multi_tier_non_antigravity_opus(
        effective_model, source, entry
    )
    if not eligible:
        return None
    return {
        "automation_id": str(row["automation_id"]),
        "user_id": user_id,
        "workspace_id": workspace_id,
        "name": str(row.get("name") or ""),
        "scope": str(row.get("scope") or "personal"),
        "status": str(row.get("status") or ""),
        "deleted_at": row["deleted_at"].isoformat() if row.get("deleted_at") else None,
        "llm_model": row_llm_model,
        "effective_model": effective_model,
        "config_version": int(row.get("config_version") or 1),
        "active_version": int(row.get("active_version") or 1),
        "model_preference_digest": _model_pref_digest(raw_pref),
        "current_effective_effort": current_effort,
        "has_effort_conflict": current_effort != TARGET_EFFORT,
        "previously_backfilled": _has_backfill_history(row.get("audit_history")),
    }


async def run_dry_run(
    *,
    out_path: Path | None = None,
    sentinel_path: Path = DEFAULT_SENTINEL_PATH,
    conn: Any = None,
    base_config: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Scan NULL-effort automations in a READ ONLY transaction and build a deterministic plan."""
    cfg = base_config if base_config is not None else await _load_base_agent_config()
    entries: list[dict[str, Any]] = []
    pref_cache: dict[str, dict[str, Any]] = {}

    async with get_db_connection(conn) as db:
        async with db.transaction():
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(
                    """
                    SELECT automation_id, user_id, workspace_id, name, scope, status,
                           deleted_at, llm_model, reasoning_effort,
                           config_version, active_version, audit_history
                    FROM automations
                    WHERE reasoning_effort IS NULL
                    ORDER BY automation_id ASC
                    """
                )
                rows = [dict(r) for r in await cur.fetchall()]
                unique_users = sorted({str(r["user_id"]) for r in rows})
                if unique_users:
                    await cur.execute(
                        """
                        SELECT user_id, model_preference
                        FROM user_preferences
                        WHERE user_id = ANY(%s::text[])
                        """,
                        (unique_users,),
                    )
                    for pref_row in await cur.fetchall():
                        mp = pref_row.get("model_preference")
                        pref_cache[str(pref_row["user_id"])] = (
                            dict(mp) if isinstance(mp, Mapping) else {}
                        )

        for row in rows:
            uid = str(row["user_id"])
            raw_pref = pref_cache.get(uid, {})
            evaluated = await _evaluate_candidate_row(
                row, base_config=cfg, raw_pref=raw_pref
            )
            if evaluated is not None:
                entries.append(evaluated)

    plan: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "target_effort": TARGET_EFFORT,
        "sentinel_path": str(sentinel_path),
        "sentinel_exists": sentinel_path.exists(),
        "total_candidates": len(entries),
        "conflict_count": sum(1 for e in entries if e["has_effort_conflict"]),
        "previously_backfilled_count": sum(1 for e in entries if e["previously_backfilled"]),
        "entries": entries,
    }
    if out_path is not None:
        _write_secure_json(out_path, plan)
    return 0, plan


async def run_apply(
    *,
    plan_path: Path,
    allow_override_ids: set[str] | None = None,
    archive_path: Path | None = None,
    sentinel_path: Path = DEFAULT_SENTINEL_PATH,
    conn: Any = None,
    base_config: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Apply a reviewed plan file inside a single locked transaction."""
    if sentinel_path.exists():
        return 2, {
            "ok": False,
            "error": "sentinel_exists",
            "message": f"Batch sentinel already exists at {sentinel_path}; refusing duplicate backfill.",
        }

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = sorted(
        list(plan.get("entries") or []),
        key=lambda e: str(e["automation_id"]),
    )
    allowed_overrides = {str(x).strip() for x in (allow_override_ids or set()) if str(x).strip()}

    if not entries:
        return 0, {"ok": True, "applied_count": 0, "message": "Plan contains 0 entries; nothing to apply."}

    prior_ids = [e["automation_id"] for e in entries if e.get("previously_backfilled")]
    if prior_ids:
        return 2, {
            "ok": False,
            "error": "previously_backfilled",
            "automation_ids": prior_ids,
            "message": f"Refusing to overwrite previously backfilled automations: {', '.join(prior_ids)}",
        }

    unapproved_conflicts = [
        e["automation_id"]
        for e in entries
        if e.get("has_effort_conflict") and e["automation_id"] not in allowed_overrides
    ]
    if unapproved_conflicts:
        return 2, {
            "ok": False,
            "error": "unapproved_effort_conflicts",
            "automation_ids": unapproved_conflicts,
            "message": (
                "Plan has automations whose current effective effort != 'medium'. "
                f"Pass --allow-override-ids to confirm: {', '.join(unapproved_conflicts)}"
            ),
        }

    cfg = base_config if base_config is not None else await _load_base_agent_config()
    now_iso = datetime.now(UTC).isoformat()
    ts_slug = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    eff_archive_path = archive_path or (
        DEFAULT_OPERATOR_DIR / f"opus-effort-archive-{ts_slug}.json"
    )

    plan_by_id = {str(e["automation_id"]): e for e in entries}
    target_ids = sorted(plan_by_id.keys())
    unique_users = sorted({str(e["user_id"]) for e in entries})

    pre_archive: dict[str, Any] = {
        "schema_version": 1,
        "status": "pending",
        "applied_at": now_iso,
        "plan_path": str(plan_path),
        "entries": [
            {
                "automation_id": e["automation_id"],
                "user_id": e["user_id"],
                "llm_model": e["llm_model"],
                "effective_model": e["effective_model"],
                "pre_reasoning_effort": None,
                "post_reasoning_effort": TARGET_EFFORT,
                "pre_config_version": e["config_version"],
                "pre_active_version": e["active_version"],
                "expected_post_config_version": int(e["config_version"]) + 1,
            }
            for e in entries
        ],
    }
    _write_secure_json(eff_archive_path, pre_archive)

    archive_results: list[dict[str, Any]] = []
    drift_errors: list[str] = []

    class _AbortApply(Exception):
        pass

    try:
        async with get_db_connection(conn) as db:
            async with db.transaction():
                locked_prefs: dict[str, dict[str, Any]] = {}
                for uid in unique_users:
                    locked_row_pref = await lock_user_preferences(uid, conn=db)
                    locked_prefs[uid] = (
                        dict(locked_row_pref) if isinstance(locked_row_pref, Mapping) else {}
                    )

                async with db.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT *
                        FROM automations
                        WHERE automation_id = ANY(%s::uuid[])
                        ORDER BY automation_id ASC
                        FOR UPDATE
                        """,
                        (target_ids,),
                    )
                    locked_rows = [dict(r) for r in await cur.fetchall()]

                if len(locked_rows) != len(target_ids):
                    found = {str(r["automation_id"]) for r in locked_rows}
                    missing = sorted(set(target_ids) - found)
                    drift_errors.append(f"Missing target rows under lock: {', '.join(missing)}")
                    raise _AbortApply()

                total_updated = 0
                for locked_row in locked_rows:
                    aid = str(locked_row["automation_id"])
                    planned = plan_by_id[aid]
                    uid = str(locked_row["user_id"])
                    if uid != str(planned["user_id"]):
                        drift_errors.append(f"{aid}: user_id drifted")
                        raise _AbortApply()
                    if int(locked_row.get("config_version") or 1) != int(planned["config_version"]):
                        drift_errors.append(
                            f"{aid}: config_version drifted ({locked_row.get('config_version')} != {planned['config_version']})"
                        )
                        raise _AbortApply()
                    if locked_row.get("llm_model") != planned["llm_model"]:
                        drift_errors.append(f"{aid}: llm_model drifted")
                        raise _AbortApply()
                    if locked_row.get("reasoning_effort") is not None:
                        drift_errors.append(
                            f"{aid}: reasoning_effort is no longer NULL ({locked_row.get('reasoning_effort')!r})"
                        )
                        raise _AbortApply()
                    if _has_backfill_history(locked_row.get("audit_history")):
                        drift_errors.append(f"{aid}: already has {BACKFILL_ACTION} in audit_history")
                        raise _AbortApply()

                    pref_row = locked_prefs.get(uid, {})
                    raw_mp = pref_row.get("model_preference")
                    mp_dict = dict(raw_mp) if isinstance(raw_mp, Mapping) else {}
                    if _model_pref_digest(mp_dict) != planned["model_preference_digest"]:
                        drift_errors.append(f"{aid}: user model_preference drifted since dry-run")
                        raise _AbortApply()

                    re_eval = await _evaluate_candidate_row(
                        locked_row, base_config=cfg, raw_pref=mp_dict
                    )
                    if re_eval is None or re_eval["effective_model"] != planned["effective_model"]:
                        drift_errors.append(f"{aid}: effective model drifted under lock")
                        raise _AbortApply()
                    if re_eval["has_effort_conflict"] and aid not in allowed_overrides:
                        drift_errors.append(f"{aid}: unapproved effort conflict under lock")
                        raise _AbortApply()

                    hist_cols, is_noop = await prepare_update_history_columns(
                        locked_row,
                        {"reasoning_effort": TARGET_EFFORT},
                        actor_user_id=OPS_ACTOR_USER_ID,
                        action=BACKFILL_ACTION,
                        expected_config_version=int(planned["config_version"]),
                        pref_row=pref_row,
                        changed_at_iso=now_iso,
                        conn=db,
                    )
                    if is_noop or not hist_cols:
                        drift_errors.append(f"{aid}: history update unexpectedly noop")
                        raise _AbortApply()

                    async with db.cursor() as cur:
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
                            """,
                            (
                                TARGET_EFFORT,
                                hist_cols["config_version"],
                                hist_cols["active_version"],
                                hist_cols["updated_by"],
                                Json(hist_cols["audit_history"]),
                                aid,
                                int(planned["config_version"]),
                            ),
                        )
                        if cur.rowcount != 1:
                            drift_errors.append(
                                f"{aid}: expected 1 updated row, got {cur.rowcount}"
                            )
                            raise _AbortApply()
                        total_updated += cur.rowcount

                    archive_results.append(
                        {
                            "automation_id": aid,
                            "user_id": uid,
                            "llm_model": locked_row.get("llm_model"),
                            "effective_model": planned["effective_model"],
                            "pre_reasoning_effort": None,
                            "post_reasoning_effort": TARGET_EFFORT,
                            "pre_config_version": int(planned["config_version"]),
                            "post_config_version": int(hist_cols["config_version"]),
                            "post_active_version": int(hist_cols["active_version"]),
                        }
                    )

                if total_updated != len(target_ids):
                    drift_errors.append(
                        f"Rowcount mismatch: updated {total_updated} != planned {len(target_ids)}"
                    )
                    raise _AbortApply()
    except _AbortApply:
        return 2, {
            "ok": False,
            "error": "locked_recheck_failed",
            "details": drift_errors,
            "message": "; ".join(drift_errors),
        }

    final_archive = {
        "schema_version": 1,
        "status": "applied",
        "applied_at": now_iso,
        "plan_path": str(plan_path),
        "applied_count": len(archive_results),
        "entries": archive_results,
    }
    _write_secure_json(eff_archive_path, final_archive)
    _write_secure_json(
        sentinel_path,
        {
            "schema_version": 1,
            "applied_at": now_iso,
            "plan_path": str(plan_path),
            "archive_path": str(eff_archive_path),
            "applied_count": len(archive_results),
            "automation_ids": [r["automation_id"] for r in archive_results],
        },
    )
    return 0, {
        "ok": True,
        "applied_count": len(archive_results),
        "archive_path": str(eff_archive_path),
        "sentinel_path": str(sentinel_path),
    }


async def run_rollback(
    *,
    archive_path: Path,
    conn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Revert unmodified backfilled rows back to NULL in a single locked transaction."""
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = sorted(
        list(archive.get("entries") or []),
        key=lambda e: str(e["automation_id"]),
    )
    if not entries:
        return 0, {"ok": True, "reverted_count": 0, "skipped": []}

    entry_by_id = {str(e["automation_id"]): e for e in entries}
    target_ids = sorted(entry_by_id.keys())
    unique_users = sorted({str(e["user_id"]) for e in entries})
    now_iso = datetime.now(UTC).isoformat()

    reverted: list[str] = []
    skipped: list[dict[str, Any]] = []

    async with get_db_connection(conn) as db:
        async with db.transaction():
            locked_prefs: dict[str, dict[str, Any]] = {}
            for uid in unique_users:
                pref_row = await lock_user_preferences(uid, conn=db)
                locked_prefs[uid] = dict(pref_row) if isinstance(pref_row, Mapping) else {}

            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM automations
                    WHERE automation_id = ANY(%s::uuid[])
                    ORDER BY automation_id ASC
                    FOR UPDATE
                    """,
                    (target_ids,),
                )
                locked_rows = {str(r["automation_id"]): dict(r) for r in await cur.fetchall()}

            for aid in target_ids:
                archived = entry_by_id[aid]
                locked_row = locked_rows.get(aid)
                if locked_row is None:
                    skipped.append({"automation_id": aid, "reason": "not_found"})
                    continue
                post_cv = int(
                    archived.get("post_config_version")
                    or archived.get("expected_post_config_version")
                    or 0
                )
                cur_cv = int(locked_row.get("config_version") or 1)
                if cur_cv != post_cv or locked_row.get("reasoning_effort") != TARGET_EFFORT:
                    skipped.append(
                        {
                            "automation_id": aid,
                            "reason": "modified_after_backfill",
                            "current_config_version": cur_cv,
                            "expected_post_config_version": post_cv,
                            "current_reasoning_effort": locked_row.get("reasoning_effort"),
                        }
                    )
                    continue

                uid = str(locked_row["user_id"])
                hist_cols, _ = await prepare_update_history_columns(
                    locked_row,
                    {"reasoning_effort": None},
                    actor_user_id=OPS_ACTOR_USER_ID,
                    action=ROLLBACK_ACTION,
                    expected_config_version=cur_cv,
                    pref_row=locked_prefs.get(uid, {}),
                    changed_at_iso=now_iso,
                    conn=db,
                )
                async with db.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET reasoning_effort = NULL,
                            config_version = %s,
                            active_version = %s,
                            updated_by = %s,
                            audit_history = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                          AND config_version = %s
                          AND reasoning_effort = %s
                        """,
                        (
                            hist_cols["config_version"],
                            hist_cols["active_version"],
                            hist_cols["updated_by"],
                            Json(hist_cols["audit_history"]),
                            aid,
                            cur_cv,
                            TARGET_EFFORT,
                        ),
                    )
                    assert cur.rowcount == 1, f"Expected 1 row reverted for {aid}, got {cur.rowcount}"
                reverted.append(aid)

    return 0, {
        "ok": True,
        "reverted_count": len(reverted),
        "reverted_ids": reverted,
        "skipped": skipped,
    }


async def run_export_downgrade(
    *,
    out_path: Path,
    conn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Export all rows with non-NULL reasoning_effort before `alembic downgrade d041`."""
    async with get_db_connection(conn) as db:
        async with db.transaction():
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(
                    """
                    SELECT automation_id, user_id, llm_model, reasoning_effort,
                           config_version, active_version
                    FROM automations
                    WHERE reasoning_effort IS NOT NULL
                    ORDER BY automation_id ASC
                    """
                )
                rows = [
                    {
                        "automation_id": str(r["automation_id"]),
                        "user_id": str(r["user_id"]),
                        "llm_model": r.get("llm_model"),
                        "reasoning_effort": r.get("reasoning_effort"),
                        "config_version": int(r.get("config_version") or 1),
                        "active_version": int(r.get("active_version") or 1),
                    }
                    for r in await cur.fetchall()
                ]
    payload = {
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "count": len(rows),
        "entries": rows,
    }
    _write_secure_json(out_path, payload)
    return 0, payload


async def run_restore_downgrade(
    *,
    export_path: Path,
    conn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Restore exported reasoning_effort values where config_version & llm_model stayed unchanged."""
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = sorted(
        list(exported.get("entries") or []),
        key=lambda e: str(e["automation_id"]),
    )
    if not entries:
        return 0, {"ok": True, "restored_count": 0, "skipped": []}

    by_id = {str(e["automation_id"]): e for e in entries}
    target_ids = sorted(by_id.keys())
    restored: list[str] = []
    skipped: list[dict[str, Any]] = []

    async with get_db_connection(conn) as db:
        async with db.transaction():
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT automation_id, llm_model, reasoning_effort, config_version
                    FROM automations
                    WHERE automation_id = ANY(%s::uuid[])
                    ORDER BY automation_id ASC
                    FOR UPDATE
                    """,
                    (target_ids,),
                )
                locked_rows = {str(r["automation_id"]): dict(r) for r in await cur.fetchall()}

            for aid in target_ids:
                saved = by_id[aid]
                row = locked_rows.get(aid)
                if row is None:
                    skipped.append({"automation_id": aid, "reason": "not_found"})
                    continue
                if (
                    int(row.get("config_version") or 1) != int(saved["config_version"])
                    or row.get("llm_model") != saved.get("llm_model")
                    or row.get("reasoning_effort") is not None
                ):
                    skipped.append(
                        {
                            "automation_id": aid,
                            "reason": "changed_during_downgrade",
                            "current_config_version": row.get("config_version"),
                            "exported_config_version": saved["config_version"],
                            "current_llm_model": row.get("llm_model"),
                            "exported_llm_model": saved.get("llm_model"),
                        }
                    )
                    continue
                async with db.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET reasoning_effort = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                          AND config_version = %s
                          AND reasoning_effort IS NULL
                        """,
                        (saved["reasoning_effort"], aid, int(saved["config_version"])),
                    )
                    assert cur.rowcount == 1
                restored.append(aid)

    return 0, {
        "ok": True,
        "restored_count": len(restored),
        "restored_ids": restored,
        "skipped": skipped,
    }


def _row_has_stale_effort_after_legacy_edit(
    row: Mapping[str, Any],
    *,
    custom_models: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """True when active snapshot lacks 'reasoning_effort' and model became Antigravity or changed."""
    if row.get("reasoning_effort") is None:
        return False
    history = _history_entries(row.get("audit_history"))
    if not history:
        return False
    active_ver = int(row.get("active_version") or 1)
    active_entry = next(
        (e for e in reversed(history) if e.get("version") == active_ver),
        history[-1],
    )
    active_snap = active_entry.get("snapshot")
    if not isinstance(active_snap, Mapping) or "reasoning_effort" in active_snap:
        return False

    cur_model = row.get("llm_model")
    if isinstance(cur_model, str) and cur_model and cur_model not in custom_models:
        try:
            entry = LLMFactory.get_model_config().get_model_config(cur_model)
        except Exception:
            entry = None
        if isinstance(entry, Mapping) and entry.get("provider") == ANTIGRAVITY_PROVIDER:
            return True

    last_with_effort = next(
        (
            e
            for e in reversed(history)
            if isinstance(e.get("snapshot"), Mapping)
            and "reasoning_effort" in e["snapshot"]
        ),
        None,
    )
    if last_with_effort is not None:
        last_model = last_with_effort["snapshot"].get("llm_model")
        if last_model != cur_model:
            return True
    return False


async def run_reconcile_stale(
    *,
    apply: bool = False,
    conn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Detect (and with ``apply=True``, clear) stale reasoning_effort after legacy-code edits."""
    now_iso = datetime.now(UTC).isoformat()
    stale_ids: list[str] = []
    cleared_ids: list[str] = []

    async with get_db_connection(conn) as db:
        async with db.transaction():
            async with db.cursor(row_factory=dict_row) as cur:
                if not apply:
                    await cur.execute("SET TRANSACTION READ ONLY")
                lock_sql = " FOR UPDATE" if apply else ""
                await cur.execute(
                    f"""
                    SELECT *
                    FROM automations
                    WHERE reasoning_effort IS NOT NULL
                    ORDER BY automation_id ASC{lock_sql}
                    """
                )
                rows = [dict(r) for r in await cur.fetchall()]

            unique_users = sorted({str(r["user_id"]) for r in rows})
            prefs_by_user: dict[str, dict[str, Any]] = {}
            if unique_users:
                if apply:
                    for uid in unique_users:
                        p = await lock_user_preferences(uid, conn=db)
                        prefs_by_user[uid] = dict(p) if isinstance(p, Mapping) else {}
                else:
                    async with db.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT user_id, model_preference, other_preference FROM user_preferences WHERE user_id = ANY(%s::text[])",
                            (unique_users,),
                        )
                        for p in await cur.fetchall():
                            prefs_by_user[str(p["user_id"])] = dict(p)

            from src.llms.preferences import custom_model_names

            for row in rows:
                aid = str(row["automation_id"])
                uid = str(row["user_id"])
                pref_row = prefs_by_user.get(uid, {})
                mp = pref_row.get("model_preference")
                customs = custom_model_names(dict(mp)) if isinstance(mp, Mapping) else set()
                if not _row_has_stale_effort_after_legacy_edit(row, custom_models=customs):
                    continue
                stale_ids.append(aid)
                if not apply:
                    continue
                cur_cv = int(row.get("config_version") or 1)
                hist_cols, _ = await prepare_update_history_columns(
                    row,
                    {"reasoning_effort": None},
                    actor_user_id=OPS_ACTOR_USER_ID,
                    action=RECONCILE_STALE_ACTION,
                    expected_config_version=cur_cv,
                    pref_row=pref_row,
                    changed_at_iso=now_iso,
                    conn=db,
                )
                async with db.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET reasoning_effort = NULL,
                            config_version = %s,
                            active_version = %s,
                            updated_by = %s,
                            audit_history = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                          AND config_version = %s
                        """,
                        (
                            hist_cols["config_version"],
                            hist_cols["active_version"],
                            hist_cols["updated_by"],
                            Json(hist_cols["audit_history"]),
                            aid,
                            cur_cv,
                        ),
                    )
                    assert cur.rowcount == 1
                cleared_ids.append(aid)

    return 0, {
        "ok": True,
        "apply": apply,
        "stale_count": len(stale_ids),
        "stale_ids": stale_ids,
        "cleared_count": len(cleared_ids),
        "cleared_ids": cleared_ids,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Controlled ops backfill for legacy Opus automations' reasoning_effort='medium'."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preview and optional plan file generation (default mode).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply a previously generated --plan file inside a single locked transaction.",
    )
    mode.add_argument(
        "--rollback",
        type=Path,
        metavar="ARCHIVE_JSON",
        help="Revert unmodified backfilled rows using an archive JSON file.",
    )
    mode.add_argument(
        "--export-downgrade",
        action="store_true",
        help="Export non-NULL reasoning_effort rows before `alembic downgrade d041`.",
    )
    mode.add_argument(
        "--restore-downgrade",
        type=Path,
        metavar="EXPORT_JSON",
        help="Restore exported reasoning_effort rows after re-upgrading to d042.",
    )
    mode.add_argument(
        "--reconcile-stale",
        action="store_true",
        help="Detect stale reasoning_effort left by pre-d042 model edits (combine with --apply-stale to clear).",
    )
    parser.add_argument("--out", type=Path, help="Output JSON file path (mode 0600).")
    parser.add_argument("--plan", type=Path, help="Plan JSON file path for --apply.")
    parser.add_argument("--archive", type=Path, help="Pre-image archive JSON path for --apply.")
    parser.add_argument(
        "--sentinel",
        type=Path,
        default=DEFAULT_SENTINEL_PATH,
        help=f"Batch completion sentinel file path (default: {DEFAULT_SENTINEL_PATH}).",
    )
    parser.add_argument(
        "--allow-override-ids",
        default="",
        help="Comma-separated automation UUIDs explicitly approved to override non-medium user preferences.",
    )
    parser.add_argument(
        "--apply-stale",
        action="store_true",
        help="With --reconcile-stale, clear detected stale reasoning_effort rows.",
    )
    return parser


def _print_dry_run_summary(plan: Mapping[str, Any]) -> None:
    entries = plan.get("entries") or []
    print(
        f"Opus automation backfill dry-run: {len(entries)} candidate(s), "
        f"{plan.get('conflict_count', 0)} effort conflict(s), "
        f"{plan.get('previously_backfilled_count', 0)} previously backfilled."
    )
    if plan.get("sentinel_exists"):
        print(f"WARNING: Sentinel already exists at {plan.get('sentinel_path')}")
    for e in entries:
        flags: list[str] = []
        if e.get("has_effort_conflict"):
            flags.append(f"CONFLICT(current={e.get('current_effective_effort')})")
        if e.get("previously_backfilled"):
            flags.append("PREVIOUSLY_BACKFILLED")
        flag_str = f"  [{' '.join(flags)}]" if flags else ""
        print(
            f"  - {e['automation_id']}  user={e['user_id']}  scope={e['scope']}  "
            f"status={e['status']}  llm_model={e['llm_model'] or '<NULL>'} -> "
            f"{e['effective_model']}  v{e['config_version']}{flag_str}"
        )


async def _async_main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    pool = get_or_create_pool()
    await pool.open()
    try:
        if args.apply:
            if not args.plan:
                parser.error("--apply requires --plan <plan.json>")
            allowed = {
                s.strip()
                for s in str(args.allow_override_ids or "").split(",")
                if s.strip()
            }
            rc, result = await run_apply(
                plan_path=args.plan,
                allow_override_ids=allowed,
                archive_path=args.archive,
                sentinel_path=args.sentinel,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return rc
        if args.rollback:
            rc, result = await run_rollback(archive_path=args.rollback)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return rc
        if args.export_downgrade:
            if not args.out:
                parser.error("--export-downgrade requires --out <export.json>")
            rc, result = await run_export_downgrade(out_path=args.out)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return rc
        if args.restore_downgrade:
            rc, result = await run_restore_downgrade(export_path=args.restore_downgrade)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return rc
        if args.reconcile_stale:
            rc, result = await run_reconcile_stale(apply=bool(args.apply_stale))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return rc

        rc, plan = await run_dry_run(out_path=args.out, sentinel_path=args.sentinel)
        _print_dry_run_summary(plan)
        if args.out:
            print(f"Plan written (0600): {args.out}")
        return rc
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
