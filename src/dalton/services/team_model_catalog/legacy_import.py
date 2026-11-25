"""Legacy ``native_sync_overlay.json`` startup import, degraded protection, and admin repair (§2.6).

Guarantees crash-safe commit-before-archive ordering:
1. Detect ``used_legacy_sync`` across DB history (``dalton_openclaw_sync_runs``),
   disk files/markers, and catalog row metadata.
2. Only transition ``pending -> not_applicable`` when ``used_legacy_sync`` is False.
3. On valid file: commit merged ``models`` / ``dalton_meta`` / ``retired``,
   ``last_known_legacy_retired``, ``bootstrap_import`` revision, and
   ``legacy_sync_import_status = 'imported'`` in ONE transaction BEFORE idempotently
   renaming the file to ``.migrated.<timestamp>``.
4. On missing/corrupt/truncated file when ``used_legacy_sync`` is True: mark
   ``legacy_sync_import_status = 'failed'``, preserve known retired keys as a
   read-only frozen set, pause native-sync-added models, and expose explicit
   admin exits ``retry_import`` and ``abandon_legacy_file``.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.database.team_model_catalog import (
    acquire_catalog_exclusive_lock,
    fetch_legacy_retired_history,
    get_catalog_row,
    has_legacy_native_sync_history,
    save_catalog_revision,
    update_legacy_import_state_only,
)
from src.dalton.llm.native_catalog import (
    NATIVE_SYNC_OVERLAY_PATH,
    NATIVE_SYNC_OVERLAY_SCHEMA,
    assert_no_credentials,
)
from src.dalton.services.team_model_catalog.compiled import (
    cache_compiled_catalog,
    compile_catalog_from_payload,
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
    CatalogConflictError,
    CatalogValidationError,
    empty_catalog_payload,
)
from src.dalton.services.team_model_catalog.follow import (
    invalidate_catalog_redis_caches,
    publish_catalog_changed,
)
from src.dalton.services.team_model_catalog.validation import trial_compile_and_validate_catalog
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_RETIRED_SECTION_RE = re.compile(r'"retired"\s*:\s*\[(.*)', re.DOTALL)
_RETIRED_KEY_RE = re.compile(r'"key"\s*:\s*"([^"]+)"')


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _has_disk_legacy_markers(legacy_path: Path) -> bool:
    """Return True if the legacy file, its ``.tmp``, or any ``.migrated.*`` / ``.corrupt.*`` exists."""
    if legacy_path.exists():
        return True
    tmp_path = legacy_path.with_suffix(legacy_path.suffix + ".tmp")
    if tmp_path.exists():
        return True
    parent = legacy_path.parent
    if not parent.is_dir():
        return False
    base = legacy_path.name
    try:
        for entry in parent.iterdir():
            name = entry.name
            if name.startswith(f"{base}.migrated.") or name.startswith(f"{base}.corrupt."):
                return True
    except OSError:
        return False
    return False


async def detect_used_legacy_sync(
    *,
    conn: Any,
    catalog_row: dict[str, Any] | None,
    legacy_path: Path | None = None,
) -> bool:
    """Determine whether this deployment ever used ``native_sync_overlay.json`` (§2.6 #2)."""
    target = legacy_path or NATIVE_SYNC_OVERLAY_PATH
    if _has_disk_legacy_markers(target):
        return True
    if catalog_row:
        if catalog_row.get("legacy_sync_imported_at") is not None:
            return True
        if catalog_row.get("last_known_legacy_retired"):
            return True
    try:
        if await has_legacy_native_sync_history(conn=conn):
            return True
    except Exception as exc:
        logger.warning(
            "[team_model_catalog] Failed to check dalton_openclaw_sync_runs history: %s",
            type(exc).__name__,
        )
        # Fail safe: if we cannot rule out prior legacy usage, treat as used so we don't mark not_applicable
        return True
    return False


def extract_disk_legacy_retired_readonly(legacy_path: Path | None = None) -> set[str]:
    """Best-effort read-only extraction of retired keys from a valid or truncated legacy file."""
    target = legacy_path or NATIVE_SYNC_OVERLAY_PATH
    if not target.is_file():
        return set()
    try:
        raw_text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and isinstance(parsed.get("retired"), list):
            return {
                str(item.get("key") or "").strip()
                for item in parsed["retired"]
                if isinstance(item, dict) and str(item.get("key") or "").strip()
            }
    except Exception:
        pass
    # Fallback for syntactically damaged/truncated JSON: scan after "retired": [
    match = _RETIRED_SECTION_RE.search(raw_text)
    if not match:
        return set()
    tail = match.group(1)
    # Stop at closing bracket if present
    end_idx = tail.find("]")
    snippet = tail[:end_idx] if end_idx != -1 else tail
    return {m.group(1).strip() for m in _RETIRED_KEY_RE.finditer(snippet) if m.group(1).strip()}


def _parse_and_validate_legacy_file(legacy_path: Path) -> dict[str, Any]:
    """Strictly parse and validate ``native_sync_overlay.json`` before DB import."""
    if not legacy_path.is_file():
        raise ValueError(f"Legacy overlay file not found at {legacy_path.name}")
    raw_text = legacy_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Legacy overlay JSON root must be an object")
    schema = data.get("schema")
    if schema is not None and schema != NATIVE_SYNC_OVERLAY_SCHEMA:
        raise ValueError(f"Unexpected legacy overlay schema {schema!r}")
    assert_no_credentials(data, path="native_sync_overlay")
    for dict_key in ("models", "dalton_meta"):
        val = data.get(dict_key)
        if val is not None and not isinstance(val, dict):
            raise ValueError(f"Legacy overlay '{dict_key}' must be an object")
    retired = data.get("retired")
    if retired is not None and not isinstance(retired, list):
        raise ValueError("Legacy overlay 'retired' must be a list")
    return data


def _merge_legacy_into_payload(
    current_payload: dict[str, Any],
    legacy_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge legacy ``models``, ``dalton_meta``, and ``retired`` into DB catalog payload."""
    merged = copy.deepcopy(current_payload) if isinstance(current_payload, dict) else empty_catalog_payload()
    merged["schema"] = CATALOG_SCHEMA
    target_keys: list[str] = []

    legacy_models = legacy_data.get("models") or {}
    if isinstance(legacy_models, dict) and legacy_models:
        dst_models = merged.setdefault("models", {})
        for k, v in legacy_models.items():
            if isinstance(v, dict):
                dst_models[str(k)] = copy.deepcopy(v)
                target_keys.append(str(k))

    legacy_meta = legacy_data.get("dalton_meta") or {}
    if isinstance(legacy_meta, dict) and legacy_meta:
        dst_meta = merged.setdefault("dalton_meta", {})
        for k, v in legacy_meta.items():
            if isinstance(v, dict):
                dst_meta[str(k)] = copy.deepcopy(v)

    existing_retired = list(merged.get("retired") or [])
    existing_retired_keys = {
        str(r.get("key") or "").strip()
        for r in existing_retired
        if isinstance(r, dict) and str(r.get("key") or "").strip()
    }
    retired_keys: list[str] = list(existing_retired_keys)
    for rec in legacy_data.get("retired") or []:
        if not isinstance(rec, dict):
            continue
        rkey = str(rec.get("key") or "").strip()
        if not rkey:
            continue
        if rkey not in existing_retired_keys:
            existing_retired.append(copy.deepcopy(rec))
            existing_retired_keys.add(rkey)
            target_keys.append(rkey)
        if rkey not in retired_keys:
            retired_keys.append(rkey)
    if existing_retired:
        merged["retired"] = existing_retired

    return merged, sorted(set(target_keys)), sorted(set(retired_keys))


def _archive_legacy_file(legacy_path: Path, *, suffix_kind: str = "migrated") -> Path | None:
    """Idempotently rename ``legacy_path`` to ``.<suffix_kind>.<timestamp>`` after DB commit."""
    if not legacy_path.exists():
        return None
    target = legacy_path.with_name(f"{legacy_path.name}.{suffix_kind}.{_utc_stamp()}")
    try:
        legacy_path.rename(target)
        logger.info("[team_model_catalog] Archived legacy overlay %s -> %s", legacy_path.name, target.name)
        return target
    except OSError as exc:
        logger.warning(
            "[team_model_catalog] Post-commit archive rename failed for %s (will retry on next boot): %s",
            legacy_path.name,
            exc,
        )
        return None


async def run_startup_legacy_sync_import(
    *,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """Run startup legacy ``native_sync_overlay.json`` import under exclusive catalog lock (§2.6)."""
    target = legacy_path or NATIVE_SYNC_OVERLAY_PATH
    post_commit_archive_kind: str | None = None
    committed_revision: int | None = None
    result_status: str = "not_applicable"

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                return {"status": "missing_table"}

            status = str(row["legacy_sync_import_status"])
            if status in ("imported", "abandoned"):
                result_status = status
                if target.exists():
                    post_commit_archive_kind = "migrated" if status == "imported" else "corrupt"
            elif status == "failed":
                result_status = "failed"
            elif status == "not_applicable":
                result_status = "not_applicable"
            else:
                # status == 'pending'
                used = await detect_used_legacy_sync(
                    conn=conn, catalog_row=row, legacy_path=target
                )
                if not target.exists():
                    if not used:
                        await update_legacy_import_state_only(
                            conn,
                            status="not_applicable",
                            error=None,
                            updated_by="system:startup",
                        )
                        result_status = "not_applicable"
                    else:
                        known = set(row.get("last_known_legacy_retired") or [])
                        try:
                            known |= await fetch_legacy_retired_history(conn=conn)
                        except Exception:
                            pass
                        await update_legacy_import_state_only(
                            conn,
                            status="failed",
                            error="Legacy native_sync_overlay.json is missing for a deployment with prior native sync history.",
                            last_known_legacy_retired=sorted(known),
                            updated_by="system:startup",
                        )
                        result_status = "failed"
                else:
                    try:
                        legacy_data = _parse_and_validate_legacy_file(target)
                        merged_payload, target_keys, retired_keys = _merge_legacy_into_payload(
                            row["payload"], legacy_data
                        )
                        # Trial compile before commit
                        compiled = compile_catalog_from_payload(
                            merged_payload,
                            revision=int(row["revision"]) + 1,
                            legacy_sync_import_status="imported",
                            last_known_legacy_retired=retired_keys,
                        )
                        saved = await save_catalog_revision(
                            conn,
                            expected_revision=int(row["revision"]),
                            action="bootstrap_import",
                            target_keys=target_keys,
                            payload=merged_payload,
                            diff_summary={
                                "imported_models": sorted((legacy_data.get("models") or {}).keys()),
                                "imported_retired": retired_keys,
                            },
                            side_effects=[],
                            actor_user_id="system:bootstrap_import",
                            actor_label="Startup Legacy Import",
                            comment="One-time import of legacy native_sync_overlay.json into PostgreSQL",
                            legacy_sync_import_status="imported",
                            legacy_sync_imported_at_now=True,
                            legacy_sync_import_error=None,
                            last_known_legacy_retired=retired_keys,
                        )
                        committed_revision = int(saved["catalog"]["revision"])
                        cache_compiled_catalog(compiled)
                        post_commit_archive_kind = "migrated"
                        result_status = "imported"
                    except Exception as exc:
                        err = sanitize_error_text(exc)
                        logger.error(
                            "[team_model_catalog] Startup legacy import failed (entering degraded mode): %s",
                            err,
                        )
                        known = set(row.get("last_known_legacy_retired") or [])
                        try:
                            known |= await fetch_legacy_retired_history(conn=conn)
                        except Exception:
                            pass
                        known |= extract_disk_legacy_retired_readonly(target)
                        await update_legacy_import_state_only(
                            conn,
                            status="failed",
                            error=err,
                            last_known_legacy_retired=sorted(known),
                            updated_by="system:startup",
                        )
                        result_status = "failed"

    # Step 2 (AFTER COMMIT): idempotently archive file & notify workers
    archived_to: str | None = None
    if post_commit_archive_kind is not None:
        archived = _archive_legacy_file(target, suffix_kind=post_commit_archive_kind)
        archived_to = str(archived) if archived else None
    if committed_revision is not None:
        await publish_catalog_changed(committed_revision)
        await invalidate_catalog_redis_caches()
    return {
        "status": result_status,
        "revision": committed_revision,
        "archived_to": archived_to,
    }


async def resolve_legacy_import(
    *,
    actor_user_id: str,
    action: str,
    persist_known_retired: bool = True,
    expected_revision: int | None = None,
    comment: str | None = None,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """Admin repair exit for ``legacy_sync_import_status == 'failed'`` (§2.6 #3)."""
    if action not in ("retry_import", "abandon_legacy_file"):
        raise CatalogValidationError(
            "invalid_legacy_import_action",
            f"Unsupported legacy import resolve action {action!r}",
        )
    target = legacy_path or NATIVE_SYNC_OVERLAY_PATH
    archive_kind: str | None = None

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_status = str(row["legacy_sync_import_status"])
            if cur_status != "failed":
                raise CatalogConflictError(
                    "legacy_import_not_failed",
                    f"Legacy import status is {cur_status!r}; repair is only applicable in 'failed' state.",
                    extra={"legacy_sync_import_status": cur_status},
                )
            cur_rev = int(row["revision"])
            if expected_revision is not None and cur_rev != int(expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": expected_revision, "current_revision": cur_rev},
                )

            if action == "retry_import":
                try:
                    legacy_data = _parse_and_validate_legacy_file(target)
                except Exception as exc:
                    raise CatalogValidationError(
                        "legacy_file_invalid",
                        f"Legacy overlay file validation failed: {sanitize_error_text(exc)}",
                    ) from exc
                merged_payload, target_keys, retired_keys = _merge_legacy_into_payload(
                    row["payload"], legacy_data
                )
                compiled = trial_compile_and_validate_catalog(
                    merged_payload,
                    revision=cur_rev + 1,
                    legacy_sync_import_status="imported",
                    last_known_legacy_retired=retired_keys,
                )
                saved = await save_catalog_revision(
                    conn,
                    expected_revision=cur_rev,
                    action="bootstrap_import",
                    target_keys=target_keys,
                    payload=merged_payload,
                    diff_summary={
                        "resolution": "retry_import",
                        "imported_models": sorted((legacy_data.get("models") or {}).keys()),
                        "imported_retired": retired_keys,
                    },
                    side_effects=[],
                    actor_user_id=actor_user_id,
                    comment=comment or "Admin retried legacy native_sync_overlay.json import",
                    legacy_sync_import_status="imported",
                    legacy_sync_imported_at_now=True,
                    legacy_sync_import_error=None,
                    last_known_legacy_retired=retired_keys,
                )
                archive_kind = "migrated"
            else:
                # abandon_legacy_file
                known_retired = set(row.get("last_known_legacy_retired") or [])
                try:
                    known_retired |= await fetch_legacy_retired_history(conn=conn)
                except Exception:
                    pass
                known_retired |= extract_disk_legacy_retired_readonly(target)

                next_payload = copy.deepcopy(row["payload"])
                next_payload["schema"] = CATALOG_SCHEMA
                persisted_retired: list[str] = []
                if persist_known_retired and known_retired:
                    existing_list = list(next_payload.get("retired") or [])
                    existing_keys = {
                        str(r.get("key") or "").strip()
                        for r in existing_list
                        if isinstance(r, dict) and str(r.get("key") or "").strip()
                    }
                    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    for key in sorted(known_retired):
                        if key not in existing_keys:
                            existing_list.append(
                                {
                                    "key": key,
                                    "provider": "",
                                    "retired_at": now_iso,
                                    "retired_by": actor_user_id,
                                }
                            )
                            existing_keys.add(key)
                            persisted_retired.append(key)
                    next_payload["retired"] = existing_list

                compiled = trial_compile_and_validate_catalog(
                    next_payload,
                    revision=cur_rev + 1,
                    legacy_sync_import_status="abandoned",
                    last_known_legacy_retired=sorted(known_retired),
                )
                saved = await save_catalog_revision(
                    conn,
                    expected_revision=cur_rev,
                    action="abandon_legacy_file",
                    target_keys=persisted_retired,
                    payload=next_payload,
                    diff_summary={
                        "resolution": "abandon_legacy_file",
                        "persisted_known_retired": persisted_retired,
                    },
                    side_effects=[],
                    actor_user_id=actor_user_id,
                    comment=comment or "Admin abandoned damaged legacy native_sync_overlay.json",
                    legacy_sync_import_status="abandoned",
                    legacy_sync_import_error=None,
                    last_known_legacy_retired=sorted(known_retired),
                )
                archive_kind = "corrupt"

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    if archive_kind:
        _archive_legacy_file(target, suffix_kind=archive_kind)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        "legacy_sync_import_status": saved["catalog"]["legacy_sync_import_status"],
        "revision_record": saved["revision_record"],
    }
