#!/usr/bin/env python3
"""Plan and apply model reference switches across Dalton's live Postgres tables.

Operational runbook:
  1. Always run in read-only plan mode first (omit ``--apply``) to review all
     planned changes and instruction excerpts.
  2. When the plan includes any company automation (``scope='company'``),
     ``--actor-user-id <uid>`` is mandatory and verified via
     ``is_internal_or_admin`` before any archive or write begins.
  3. Before ``--apply`` mutates any row, original values are archived with mode
     ``0600`` to ``var/dalton/operator-records/model-switch-<UTC>.json`` (and
     updated with the exact user-preference rows actually replaced vs skipped).
  4. User-defined models under ``model_preference.custom_models`` are NOT in
     scope and are left untouched.
  5. Re-running after a partial failure is idempotent: already-switched rows
     drop out of the plan.

Covers four surfaces for ``--from <old> --to <new>`` pairs (repeatable):
  1. ``automations.llm_model`` (``deleted_at IS NULL``) via
     ``src.server.services.automations.lifecycle.update_automation`` (and
     ``update_company_agent`` for ``scope='company'`` rows) — never direct SQL.
  2. ``conversation_threads.llm_model`` via
     ``src.dalton.database.model_switch.reassign_planned_thread_llm_models``,
     updating only the planned ``thread_id`` list inside one transaction and
     rolling back if ``rowcount != len(thread_ids)``.
  3. ``user_preferences.model_preference`` (``preferred_model``,
     ``fallback_models``, other model slot keys, and ``profiles``) with
     per-user CAS under ``lock_user_preferences`` + ``upsert_user_preferences``
     (``expect_other_preference`` for legacy keys), skipping and reporting any
     conflicted user without overwriting.
  4. ``automations.instruction`` mentions (e.g. ``6.1 Sol`` / ``6 Astra``):
     printed as read-only excerpts with the recommended prompt rewrite, never
     auto-edited.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCALAR_PREF_KEYS: tuple[str, ...] = (
    "preferred_model",
    "preferred_background_model",
    "compaction_model",
    "summarization_model",
    "fetch_model",
    "dalton_audit_model",
    "dalton_language_model",
    "dalton_assist_model",
    "dalton_extract_model",
)

LIST_PREF_KEYS: tuple[str, ...] = (
    "fallback_models",
    "dalton_audit_fallback_models",
    "dalton_assist_fallback_models",
    "dalton_reviewer_models",
)

OTHER_LIST_PREF_KEYS: tuple[str, ...] = (
    "starred_models",
    "hidden_quick_access_models",
)

OTHER_PREF_KEY_PREFIX = "other_preference."

RECOMMENDED_INSTRUCTION_REWRITE = (
    "建议改法：把「必须是 6.1 Sol，否则停止」改为"
    "「必须是本自动化指定的模型（以运行时服务端确认的为准），否则停止」，"
    "这样以后换模型就不用再改指令。"
)

DEFAULT_ARCHIVE_DIR = REPO_ROOT / "var" / "dalton" / "operator-records"


@dataclass(frozen=True)
class AutomationPlan:
    automation_id: str
    user_id: str
    name: str
    scope: str
    old_model: str
    new_model: str
    config_version: int | None
    original_row: dict[str, Any]


@dataclass(frozen=True)
class ThreadGroupPlan:
    owner_user_id: str
    old_model: str
    new_model: str
    thread_ids: list[str]
    planned_count: int


@dataclass(frozen=True)
class PreferencePlan:
    user_id: str
    patch: dict[str, Any]
    changed_keys: list[str]
    original_model_preference: dict[str, Any]
    original_other_preference: dict[str, Any]
    other_patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstructionMention:
    automation_id: str
    user_id: str
    name: str
    scope: str
    llm_model: str | None
    matched_terms: list[str]
    snippets: list[str]


@dataclass
class SwitchPlan:
    mappings: dict[str, str]
    automations: list[AutomationPlan] = field(default_factory=list)
    thread_groups: list[ThreadGroupPlan] = field(default_factory=list)
    preferences: list[PreferencePlan] = field(default_factory=list)
    instruction_mentions: list[InstructionMention] = field(default_factory=list)

    @property
    def total_threads(self) -> int:
        return sum(g.planned_count for g in self.thread_groups)


@dataclass
class SwitchProgress:
    completed_automations: list[str] = field(default_factory=list)
    failed_automation: tuple[str, str] | None = None
    pending_automations: list[str] = field(default_factory=list)
    completed_thread_groups: list[dict[str, Any]] = field(default_factory=list)
    failed_thread_group: dict[str, Any] | None = None
    pending_thread_groups: list[dict[str, Any]] = field(default_factory=list)
    completed_preferences: list[str] = field(default_factory=list)
    skipped_preferences: list[dict[str, str]] = field(default_factory=list)
    failed_preference: tuple[str, str] | None = None
    pending_preferences: list[str] = field(default_factory=list)


class SwitchApplyError(RuntimeError):
    """Raised when any write phase fails, carrying completed and pending progress."""

    def __init__(self, message: str, progress: SwitchProgress) -> None:
        super().__init__(message)
        self.progress = progress


def parse_mappings(from_models: list[str], to_models: list[str]) -> dict[str, str]:
    """Pair ``--from`` and ``--to`` arguments into an ordered ``{old: new}`` map."""
    if not from_models or not to_models or len(from_models) != len(to_models):
        raise ValueError("--from and --to must be provided in matching pairs")
    mappings: dict[str, str] = {}
    for src, dst in zip(from_models, to_models):
        src_clean = src.strip()
        dst_clean = dst.strip()
        if not src_clean or not dst_clean:
            raise ValueError("--from and --to values must be non-empty")
        if src_clean == dst_clean:
            raise ValueError(f"--from and --to cannot be identical: {src_clean!r}")
        if src_clean in mappings and mappings[src_clean] != dst_clean:
            raise ValueError(f"Duplicate --from model {src_clean!r} with conflicting targets")
        mappings[src_clean] = dst_clean
    chained = sorted(set(mappings) & set(mappings.values()))
    if chained:
        # A->B plus B->C in one run would move A's rows twice and drop A's profile.
        raise ValueError(f"Chained mappings are not allowed; {chained} is both a --from and a --to")
    return mappings


def validate_target_models(mappings: dict[str, str]) -> None:
    """Ensure every ``--to`` key resolves in the merged model catalog."""
    from src.llms.llm import ModelConfig

    catalog = ModelConfig().llm_config or {}
    missing = sorted({dst for dst in mappings.values() if dst not in catalog})
    if missing:
        raise ValueError(f"Target model(s) missing from merged catalog: {', '.join(missing)}")


def _replace_list_items(val: list[Any], mappings: dict[str, str]) -> tuple[list[Any], bool]:
    """Replace mapped model strings in-place and deduplicate, preserving non-string entries."""
    updated: list[Any] = []
    changed = False
    seen_strs: set[str] = set()
    for item in val:
        if isinstance(item, str) and item in mappings:
            mapped = mappings[item]
            changed = True
            if mapped not in seen_strs:
                updated.append(mapped)
                seen_strs.add(mapped)
        elif isinstance(item, str):
            if item not in seen_strs:
                updated.append(item)
                seen_strs.add(item)
        else:
            updated.append(item)
    return updated, changed


def build_preference_patch(
    model_preference: dict[str, Any] | None,
    other_preference: dict[str, Any] | None,
    mappings: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Compute a minimal ``model_preference`` merge-upsert patch for one user."""
    from src.server.services.llm.user_models import model_preference_of

    effective = model_preference_of(
        {
            "model_preference": model_preference or {},
            "other_preference": other_preference or {},
        }
    )
    patch: dict[str, Any] = {}
    changed_keys: list[str] = []

    for key in SCALAR_PREF_KEYS:
        val = effective.get(key)
        if isinstance(val, str) and val in mappings:
            patch[key] = mappings[val]
            changed_keys.append(key)

    for key in LIST_PREF_KEYS:
        val = effective.get(key)
        if not isinstance(val, list):
            continue
        updated, changed = _replace_list_items(val, mappings)
        if changed:
            patch[key] = updated
            changed_keys.append(key)

    profiles = effective.get("profiles")
    if isinstance(profiles, dict):
        profile_patch: dict[str, Any] = {}
        for model_key, cfg in profiles.items():
            if model_key not in mappings:
                continue
            target_key = mappings[model_key]
            existing_target = profiles.get(target_key)
            if isinstance(cfg, dict) and isinstance(existing_target, dict):
                merged_cfg = {**cfg, **existing_target}
            elif isinstance(existing_target, dict):
                merged_cfg = copy.deepcopy(existing_target)
            else:
                merged_cfg = copy.deepcopy(cfg)
            profile_patch[target_key] = merged_cfg
            profile_patch[model_key] = None
        if profile_patch:
            patch["profiles"] = profile_patch
            changed_keys.append("profiles")

    return patch, changed_keys


def build_other_preference_patch(
    other_preference: dict[str, Any] | None,
    mappings: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Compute a minimal ``other_preference`` patch for starred and hidden quick-access lists."""
    raw = other_preference or {}
    patch: dict[str, Any] = {}
    changed_keys: list[str] = []
    for key in OTHER_LIST_PREF_KEYS:
        val = raw.get(key)
        if not isinstance(val, list):
            continue
        updated, changed = _replace_list_items(val, mappings)
        if changed:
            patch[key] = updated
            changed_keys.append(f"{OTHER_PREF_KEY_PREFIX}{key}")
    return patch, changed_keys


def _instruction_terms(mappings: dict[str, str]) -> list[str]:
    terms: list[str] = []
    for old_key in mappings:
        terms.append(old_key)
        if "6.1-sol" in old_key:
            terms.append("6.1 Sol")
        if "6-astra" in old_key:
            terms.append("6 Astra")
    return list(dict.fromkeys(terms))


def find_instruction_mentions(
    instruction: str | None,
    terms: list[str],
) -> tuple[list[str], list[str]]:
    """Return ``(matched_terms, context_snippets)`` without altering ``instruction``."""
    if not instruction or not terms:
        return [], []
    matched: list[str] = []
    snippets: list[str] = []
    seen_snippets: set[str] = set()
    lines = instruction.splitlines()
    for term in terms:
        pat = re.compile(re.escape(term), re.IGNORECASE)
        if not pat.search(instruction):
            continue
        matched.append(term)
        for lineno, line in enumerate(lines, start=1):
            if pat.search(line):
                snippet = f"L{lineno}: {line.strip()}"
                if snippet not in seen_snippets:
                    seen_snippets.add(snippet)
                    snippets.append(snippet)
    return matched, snippets


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _write_archive_file(target: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n").encode(
        "utf-8"
    )
    # Write beside the target and swap in, so a failed refresh (disk full) never truncates
    # the archive that already holds the pre-switch values.
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, target)


def _build_archive_payload(
    plan: SwitchPlan,
    *,
    created_at: datetime,
    replaced_preferences: list[dict[str, Any]] | None = None,
    skipped_preferences: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "created_at": created_at.isoformat(),
        "mappings": plan.mappings,
        "automations": [a.original_row for a in plan.automations],
        "threads": [
            {
                "owner_user_id": g.owner_user_id,
                "old_model": g.old_model,
                "new_model": g.new_model,
                "thread_ids": g.thread_ids,
                "planned_count": g.planned_count,
            }
            for g in plan.thread_groups
        ],
        "user_preferences": (
            replaced_preferences
            if replaced_preferences is not None
            else [
                {
                    "user_id": p.user_id,
                    "changed_keys": p.changed_keys,
                    "patch": p.patch,
                    "other_patch": p.other_patch,
                    "original_model_preference": p.original_model_preference,
                    "original_other_preference": p.original_other_preference,
                }
                for p in plan.preferences
            ]
        ),
        "skipped_user_preferences": skipped_preferences or [],
        "instruction_mentions": [
            {
                "automation_id": m.automation_id,
                "user_id": m.user_id,
                "name": m.name,
                "scope": m.scope,
                "llm_model": m.llm_model,
                "matched_terms": m.matched_terms,
                "snippets": m.snippets,
            }
            for m in plan.instruction_mentions
        ],
    }


def write_archive(
    plan: SwitchPlan,
    *,
    archive_dir: Path,
    now: datetime | None = None,
) -> Path:
    """Persist original values to ``model-switch-<ts>.json`` with mode ``0600``."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    created_at = now or datetime.now(timezone.utc)
    ts = created_at.strftime("%Y%m%dT%H%M%SZ")
    target = archive_dir / f"model-switch-{ts}.json"
    payload = _build_archive_payload(plan, created_at=created_at)
    _write_archive_file(target, payload)
    return target


async def collect_plan(conn: Any, mappings: dict[str, str]) -> SwitchPlan:
    """Scan Postgres (read-only) and build the full switch plan."""
    from psycopg.rows import dict_row

    plan = SwitchPlan(mappings=dict(mappings))
    from_keys = list(mappings.keys())
    terms = _instruction_terms(mappings)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT automation_id, user_id, name, scope, llm_model, config_version, instruction
            FROM automations
            WHERE deleted_at IS NULL
            ORDER BY automation_id
            """
        )
        automation_rows = await cur.fetchall()

        for row in automation_rows:
            llm_model = row.get("llm_model")
            if llm_model in mappings:
                plan.automations.append(
                    AutomationPlan(
                        automation_id=str(row["automation_id"]),
                        user_id=str(row["user_id"]),
                        name=str(row.get("name") or ""),
                        scope=str(row.get("scope") or "personal"),
                        old_model=llm_model,
                        new_model=mappings[llm_model],
                        config_version=row.get("config_version"),
                        original_row={
                            "automation_id": str(row["automation_id"]),
                            "user_id": str(row["user_id"]),
                            "name": row.get("name"),
                            "scope": row.get("scope"),
                            "llm_model": llm_model,
                            "config_version": row.get("config_version"),
                        },
                    )
                )
            matched, snippets = find_instruction_mentions(row.get("instruction"), terms)
            if matched:
                plan.instruction_mentions.append(
                    InstructionMention(
                        automation_id=str(row["automation_id"]),
                        user_id=str(row["user_id"]),
                        name=str(row.get("name") or ""),
                        scope=str(row.get("scope") or "personal"),
                        llm_model=llm_model,
                        matched_terms=matched,
                        snippets=snippets,
                    )
                )

        await cur.execute(
            """
            SELECT t.conversation_thread_id,
                   COALESCE(t.user_id, w.user_id) AS owner_user_id,
                   t.llm_model
            FROM conversation_threads t
            JOIN workspaces w ON w.workspace_id = t.workspace_id
            WHERE w.status != 'deleted'
              AND t.llm_model = ANY(%s)
            ORDER BY owner_user_id, t.llm_model, t.conversation_thread_id
            """,
            (from_keys,),
        )
        thread_rows = await cur.fetchall()
        grouped_threads: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in thread_rows:
            grouped_threads[(str(row["owner_user_id"]), str(row["llm_model"]))].append(
                str(row["conversation_thread_id"])
            )
        for (owner_user_id, old_model), thread_ids in sorted(grouped_threads.items()):
            plan.thread_groups.append(
                ThreadGroupPlan(
                    owner_user_id=owner_user_id,
                    old_model=old_model,
                    new_model=mappings[old_model],
                    thread_ids=thread_ids,
                    planned_count=len(thread_ids),
                )
            )

        await cur.execute(
            """
            SELECT user_id, model_preference, other_preference
            FROM user_preferences
            WHERE model_preference IS NOT NULL OR other_preference IS NOT NULL
            ORDER BY user_id
            """
        )
        pref_rows = await cur.fetchall()
        for row in pref_rows:
            mp = row.get("model_preference") or {}
            op = row.get("other_preference") or {}
            patch, changed_keys = build_preference_patch(mp, op, mappings)
            other_patch, other_changed_keys = build_other_preference_patch(op, mappings)
            if patch or other_patch:
                plan.preferences.append(
                    PreferencePlan(
                        user_id=str(row["user_id"]),
                        patch=patch,
                        other_patch=other_patch,
                        changed_keys=[*changed_keys, *other_changed_keys],
                        original_model_preference=copy.deepcopy(mp),
                        original_other_preference=copy.deepcopy(op),
                    )
                )

    return plan


async def validate_company_actor(plan: SwitchPlan, actor_user_id: str | None) -> str | None:
    """Require and authorize an explicit ``--actor-user-id`` before any write if company rows exist."""
    if not any(item.scope == "company" for item in plan.automations):
        return None
    actor = str(actor_user_id or "").strip()
    if not actor:
        raise ValueError(
            "Company automation(s) in plan require --actor-user-id (internal or admin user)."
        )
    from src.dalton.access_tiers import is_internal_or_admin
    from src.dalton.company_identity import is_company_system_user

    if is_company_system_user(actor) or not await is_internal_or_admin(actor):
        raise ValueError(
            f"--actor-user-id={actor!r} is not an authorized internal or admin user."
        )
    return actor


def _conflicting_pref_keys(
    plan_mp: dict[str, Any],
    plan_op: dict[str, Any],
    live_mp: dict[str, Any],
    live_op: dict[str, Any],
    keys: list[str],
    mappings: dict[str, str],
) -> list[str]:
    from src.server.services.llm.user_models import model_preference_of

    plan_eff = model_preference_of({"model_preference": plan_mp, "other_preference": plan_op})
    live_eff = model_preference_of({"model_preference": live_mp, "other_preference": live_op})
    conflicts: list[str] = []
    for key in keys:
        if key.startswith(OTHER_PREF_KEY_PREFIX):
            sub_key = key[len(OTHER_PREF_KEY_PREFIX) :]
            if (plan_op or {}).get(sub_key) != (live_op or {}).get(sub_key):
                conflicts.append(key)
        elif key == "profiles":
            plan_profiles = plan_eff.get("profiles") or {}
            live_profiles = live_eff.get("profiles") or {}
            for old_key, new_key in mappings.items():
                if (
                    plan_profiles.get(old_key) != live_profiles.get(old_key)
                    or plan_profiles.get(new_key) != live_profiles.get(new_key)
                ):
                    conflicts.append("profiles")
                    break
        elif plan_eff.get(key) != live_eff.get(key):
            conflicts.append(key)
    return conflicts


def _expect_other_for_moved_keys(
    live_mp: dict[str, Any],
    live_op: dict[str, Any],
    changed_keys: list[str],
    other_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from src.llms.preferences import MOVED_MODEL_KEYS

    expected = {
        key: live_op.get(key)
        for key in changed_keys
        if key in MOVED_MODEL_KEYS and key not in live_mp and key in live_op
    }
    for key in other_patch or {}:
        expected[key] = live_op.get(key)
    return expected or None


def format_progress_summary(progress: SwitchProgress) -> str:
    """Format completed, skipped, failed, and pending items across all categories."""
    lines = ["=== Switch Progress Summary ==="]
    lines.append(f"Automations completed ({len(progress.completed_automations)}): {progress.completed_automations}")
    if progress.failed_automation:
        lines.append(f"Automations failed: {progress.failed_automation}")
    lines.append(f"Automations pending ({len(progress.pending_automations)}): {progress.pending_automations}")

    lines.append(
        f"Thread groups completed ({len(progress.completed_thread_groups)}): "
        f"{progress.completed_thread_groups}"
    )
    if progress.failed_thread_group:
        lines.append(f"Thread group failed: {progress.failed_thread_group}")
    lines.append(
        f"Thread groups pending ({len(progress.pending_thread_groups)}): "
        f"{progress.pending_thread_groups}"
    )

    lines.append(f"Preferences completed ({len(progress.completed_preferences)}): {progress.completed_preferences}")
    if progress.skipped_preferences:
        lines.append(
            f"Preferences skipped on CAS conflict ({len(progress.skipped_preferences)}): "
            f"{progress.skipped_preferences}"
        )
    if progress.failed_preference:
        lines.append(f"Preferences failed: {progress.failed_preference}")
    lines.append(f"Preferences pending ({len(progress.pending_preferences)}): {progress.pending_preferences}")
    return "\n".join(lines)


async def apply_plan(
    plan: SwitchPlan,
    *,
    conn: Any | None = None,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Validate actor, archive originals, and execute service/CAS updates."""
    from src.dalton.database.model_switch import reassign_planned_thread_llm_models
    from src.dalton.services.company_agent_writes import update_company_agent
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db
    from src.server.dependencies.principal import Principal
    from src.server.services.automations import lifecycle

    resolved_actor = await validate_company_actor(plan, actor_user_id)

    created_at = datetime.now(timezone.utc)
    archive_path = write_archive(plan, archive_dir=archive_dir, now=created_at)

    progress = SwitchProgress(
        pending_automations=[item.automation_id for item in plan.automations],
        pending_thread_groups=[
            {
                "owner_user_id": g.owner_user_id,
                "old_model": g.old_model,
                "new_model": g.new_model,
                "planned_count": g.planned_count,
            }
            for g in plan.thread_groups
        ],
        pending_preferences=[pref.user_id for pref in plan.preferences],
    )

    # 1. Automations
    for idx, item in enumerate(plan.automations):
        try:
            if item.scope == "company":
                assert resolved_actor is not None
                updated = await update_company_agent(
                    Principal(user_id=resolved_actor, auth="session"),
                    item.automation_id,
                    {"llm_model": item.new_model},
                    expected_config_version=item.config_version,
                )
            else:
                updated = await lifecycle.update_automation(
                    item.automation_id,
                    item.user_id,
                    {"llm_model": item.new_model},
                    expected_config_version=item.config_version,
                )
            if not updated or updated.get("llm_model") != item.new_model:
                raise RuntimeError(
                    f"Automation update mismatch for {item.automation_id}: "
                    f"expected {item.new_model!r}, got {updated.get('llm_model') if updated else None!r}"
                )
        except Exception as exc:
            progress.failed_automation = (item.automation_id, str(exc))
            progress.pending_automations = [a.automation_id for a in plan.automations[idx + 1 :]]
            raise SwitchApplyError(str(exc), progress) from exc
        progress.completed_automations.append(item.automation_id)
        progress.pending_automations = [a.automation_id for a in plan.automations[idx + 1 :]]

    # 2. Threads (only planned thread_ids inside one transaction, rolled back on mismatch)
    moved_threads = 0
    for idx, group in enumerate(plan.thread_groups):
        group_desc = {
            "owner_user_id": group.owner_user_id,
            "old_model": group.old_model,
            "new_model": group.new_model,
            "planned_count": group.planned_count,
        }
        try:
            moved = await reassign_planned_thread_llm_models(
                group.owner_user_id,
                group.old_model,
                group.new_model,
                group.thread_ids,
                conn=conn,
            )
            if moved != group.planned_count:
                raise RuntimeError(
                    f"Thread reassign mismatch for user={group.owner_user_id} "
                    f"({group.old_model}->{group.new_model}): "
                    f"planned={group.planned_count}, actual={moved}"
                )
        except Exception as exc:
            progress.failed_thread_group = {**group_desc, "error": str(exc)}
            progress.pending_thread_groups = [
                {
                    "owner_user_id": g.owner_user_id,
                    "old_model": g.old_model,
                    "new_model": g.new_model,
                    "planned_count": g.planned_count,
                }
                for g in plan.thread_groups[idx + 1 :]
            ]
            raise SwitchApplyError(str(exc), progress) from exc
        moved_threads += moved
        progress.completed_thread_groups.append({**group_desc, "moved": moved})
        progress.pending_thread_groups = [
            {
                "owner_user_id": g.owner_user_id,
                "old_model": g.old_model,
                "new_model": g.new_model,
                "planned_count": g.planned_count,
            }
            for g in plan.thread_groups[idx + 1 :]
        ]

    # 3. User preferences (re-read under profile lock + CAS; skip conflicted users without overwriting)
    replaced_pref_records: list[dict[str, Any]] = []
    for idx, pref in enumerate(plan.preferences):
        try:
            async with pool_mod.get_db_connection(conn) as tx_conn, tx_conn.transaction():
                locked_row = await user_db.lock_user_preferences(pref.user_id, conn=tx_conn)
                if locked_row is None:
                    progress.skipped_preferences.append(
                        {"user_id": pref.user_id, "reason": "user_preferences row missing at apply time"}
                    )
                    progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
                    continue

                live_mp = copy.deepcopy(locked_row.get("model_preference") or {})
                live_op = copy.deepcopy(locked_row.get("other_preference") or {})
                conflicts = _conflicting_pref_keys(
                    pref.original_model_preference,
                    pref.original_other_preference,
                    live_mp,
                    live_op,
                    pref.changed_keys,
                    plan.mappings,
                )
                if conflicts:
                    progress.skipped_preferences.append(
                        {
                            "user_id": pref.user_id,
                            "reason": f"CAS conflict on planned key(s): {', '.join(conflicts)}",
                        }
                    )
                    progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
                    continue

                actual_patch, actual_changed = build_preference_patch(live_mp, live_op, plan.mappings)
                actual_other_patch, actual_other_changed = build_other_preference_patch(
                    live_op, plan.mappings
                )
                all_actual_changed = [*actual_changed, *actual_other_changed]
                if not actual_patch and not actual_other_patch:
                    progress.skipped_preferences.append(
                        {"user_id": pref.user_id, "reason": "no matching old-model slots at apply time"}
                    )
                    progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
                    continue

                expect_other = _expect_other_for_moved_keys(
                    live_mp,
                    live_op,
                    actual_changed,
                    other_patch=actual_other_patch,
                )
                upsert_kwargs: dict[str, Any] = {
                    "user_id": pref.user_id,
                    "conn": tx_conn,
                    "expect_other_preference": expect_other,
                }
                if actual_patch:
                    upsert_kwargs["model_preference"] = actual_patch
                if actual_other_patch:
                    upsert_kwargs["other_preference"] = actual_other_patch
                try:
                    row = await user_db.upsert_user_preferences(**upsert_kwargs)
                except user_db.PreferenceChanged as exc:
                    progress.skipped_preferences.append(
                        {"user_id": pref.user_id, "reason": f"CAS conflict ({exc})"}
                    )
                    progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
                    continue
                if not row:
                    raise RuntimeError(
                        f"User preference update returned empty row for user={pref.user_id}"
                    )
        except SwitchApplyError:
            raise
        except Exception as exc:
            progress.failed_preference = (pref.user_id, str(exc))
            progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
            raise SwitchApplyError(str(exc), progress) from exc

        await user_db.invalidate_user_prefs_cache(pref.user_id)
        progress.completed_preferences.append(pref.user_id)
        progress.pending_preferences = [p.user_id for p in plan.preferences[idx + 1 :]]
        replaced_pref_records.append(
            {
                "user_id": pref.user_id,
                "changed_keys": all_actual_changed,
                "patch": actual_patch,
                "other_patch": actual_other_patch,
                "original_model_preference": live_mp,
                "original_other_preference": live_op,
            }
        )

    # Refresh archive with the exact replaced user-preference originals and any skipped conflicts.
    try:
        _write_archive_file(
            archive_path,
            _build_archive_payload(
                plan,
                created_at=created_at,
                replaced_preferences=replaced_pref_records,
                skipped_preferences=progress.skipped_preferences,
            ),
        )
    except Exception as exc:
        raise SwitchApplyError(
            f"all writes committed, but refreshing the archive {archive_path} failed: {exc}; "
            "the archive still holds the pre-switch values written before any change",
            progress,
        ) from exc

    return {
        "archive_path": str(archive_path),
        "automations_updated": len(progress.completed_automations),
        "threads_updated": moved_threads,
        "preferences_updated": len(progress.completed_preferences),
        "preferences_skipped_conflicts": progress.skipped_preferences,
        "instruction_mentions_untouched": len(plan.instruction_mentions),
    }


def format_plan_report(plan: SwitchPlan, *, apply_mode: bool) -> str:
    """Format a human-readable plan (and instruction-rewrite guidance) report."""
    lines: list[str] = []
    mode_label = "APPLY" if apply_mode else "PLAN (dry-run, no writes)"
    lines.append(f"=== switch_model_refs [{mode_label}] ===")
    lines.append("Mappings:")
    for src, dst in plan.mappings.items():
        lines.append(f"  {src} -> {dst}")

    lines.append(f"\n1. automations.llm_model (deleted_at IS NULL): {len(plan.automations)} row(s)")
    for item in plan.automations:
        lines.append(
            f"  - id={item.automation_id} user={item.user_id} scope={item.scope} "
            f"name={item.name!r} {item.old_model} -> {item.new_model}"
        )

    lines.append(
        f"\n2. conversation_threads.llm_model: {plan.total_threads} thread(s) across "
        f"{len(plan.thread_groups)} owner/model group(s)"
    )
    for group in plan.thread_groups:
        lines.append(
            f"  - owner={group.owner_user_id} {group.old_model} -> {group.new_model} "
            f"count={group.planned_count}"
        )

    lines.append(f"\n3. user_preferences.model_preference: {len(plan.preferences)} user(s)")
    for pref in plan.preferences:
        other_part = (
            f" other_patch={json.dumps(pref.other_patch, ensure_ascii=False)}"
            if pref.other_patch
            else ""
        )
        lines.append(
            f"  - user={pref.user_id} keys={pref.changed_keys} "
            f"patch={json.dumps(pref.patch, ensure_ascii=False)}{other_part}"
        )

    lines.append(
        f"\n4. automations.instruction mentions (read-only, NOT auto-edited): "
        f"{len(plan.instruction_mentions)} automation(s)"
    )
    for mention in plan.instruction_mentions:
        lines.append(
            f"  - id={mention.automation_id} user={mention.user_id} scope={mention.scope} "
            f"name={mention.name!r} terms={mention.matched_terms}"
        )
        for snip in mention.snippets:
            lines.append(f"      {snip}")
    if plan.instruction_mentions:
        lines.append(f"  * {RECOMMENDED_INSTRUCTION_REWRITE}")

    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    mappings = parse_mappings(args.from_models, args.to_models)
    if not args.skip_catalog_check:
        validate_target_models(mappings)

    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        plan = await collect_plan(conn, mappings)
        print(format_plan_report(plan, apply_mode=args.apply))
        if not args.apply:
            print("\nDry run complete: no database writes performed. Re-run with --apply to execute.")
            return 0
        try:
            summary = await apply_plan(
                plan,
                conn=conn,
                archive_dir=Path(args.archive_dir),
                actor_user_id=args.actor_user_id,
            )
        except SwitchApplyError as exc:
            print(f"\nERROR during --apply: {exc}", file=sys.stderr)
            print(format_progress_summary(exc.progress), file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"\nERROR before writes: {exc}", file=sys.stderr)
            return 1
    print("\n=== Apply summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply model key switches across automations, threads, and user preferences."
    )
    parser.add_argument(
        "--from",
        dest="from_models",
        action="append",
        required=True,
        help="Source model key to replace (repeatable, paired with --to).",
    )
    parser.add_argument(
        "--to",
        dest="to_models",
        action="append",
        required=True,
        help="Target model key (repeatable, paired with --from).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute writes after archiving original values; omit for read-only plan mode.",
    )
    parser.add_argument(
        "--actor-user-id",
        default=None,
        help="Internal/admin user_id required when updating scope='company' automations.",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(DEFAULT_ARCHIVE_DIR),
        help=f"Directory for 0600 pre-apply JSON archives (default: {DEFAULT_ARCHIVE_DIR}).",
    )
    parser.add_argument(
        "--skip-catalog-check",
        action="store_true",
        help="Skip verifying --to keys exist in ModelConfig.",
    )
    return parser


async def _run_standalone(args: argparse.Namespace) -> int:
    """Run as a script: the app pool is created closed (the server opens it at startup)."""
    from src.server.database.pool import get_or_create_pool

    pool = get_or_create_pool()
    await pool.open()
    try:
        return await _run(args)
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run_standalone(args))


if __name__ == "__main__":
    raise SystemExit(main())
