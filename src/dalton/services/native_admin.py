"""Native-subscription model check/sync/retire/restore/status service.

Batch 1 (team-model-catalog d051) converges reads & writes onto PostgreSQL
``dalton_team_model_catalog`` / ``dalton_team_model_catalog_revisions`` under
the exclusive catalog advisory lock while preserving ``dalton_openclaw_sync_runs``
history records for the Settings card.
"""

from __future__ import annotations

import copy
import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from src.dalton.database.native_sync import (
    create_native_sync_run,
    fetch_last_succeeded_native_check,
    fetch_recent_native_runs,
    finish_native_sync_run,
    release_native_sync_lock,
    try_acquire_native_sync_lock,
)
from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.database.team_model_catalog import (
    acquire_catalog_exclusive_lock,
    fetch_legacy_retired_history,
    get_catalog_row,
    save_catalog_revision,
)
from src.dalton.llm.native_catalog import (
    NATIVE_LISTING_PROVIDERS,
    NATIVE_SYNC_OVERLAY_PATH,
    build_native_diff,
    build_overlay_entries,
    collect_native_listings,
    read_retired_keys_strict,
)
from src.dalton.services import model_references
from src.dalton.services.model_references import ReferenceSourceUnavailable
from src.dalton.services.team_model_catalog.compiled import (
    cache_compiled_catalog,
    compile_catalog_from_payload,
    get_compiled_catalog_async,
)
from src.dalton.services.team_model_catalog.constants import CATALOG_SCHEMA
from src.dalton.services.team_model_catalog.follow import (
    invalidate_catalog_redis_caches,
    publish_catalog_changed,
)
from src.dalton.services.team_model_catalog.legacy_import import (
    extract_disk_legacy_retired_readonly,
)
from src.dalton.services.team_model_catalog.validation import trial_compile_and_validate_catalog
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_NATIVE_CHECK_LOCK_BUSY_MESSAGE = "另一个原生模型检查或同步正在进行中，请稍候。"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _collect_reference_map(*, strict: bool = False) -> dict[str, list[str]]:
    """Merge every reference source into `{model_key: [source, ...]}`."""
    merged: dict[str, list[str]] = {}
    for source, collect in (
        ("agent_config", model_references.agent_config_refs),
        ("dalton_settings", model_references.dalton_settings_refs),
        ("user_preferences", model_references.user_preference_refs),
        ("user_subagents", model_references.user_subagent_refs),
        ("plugin_subagents", model_references.plugin_subagent_refs),
        ("automations", model_references.automation_refs),
    ):
        try:
            source_refs = collect()
            if inspect.isawaitable(source_refs):
                source_refs = await source_refs
        except Exception as exc:
            unavailable = (
                exc
                if isinstance(exc, ReferenceSourceUnavailable)
                else ReferenceSourceUnavailable(source, exc)
            )
            if strict:
                raise unavailable from exc
            logger.debug("Reference source %s unavailable: %s", source, unavailable)
            continue
        for key, locations in source_refs.items():
            merged.setdefault(key, []).extend(locations)
    return merged


def _listing_rows(succeeded_check: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-provider rows for the status card, read off the last succeeded check."""
    stored = ((succeeded_check or {}).get("diff") or {}).get("providers") or []
    if stored:
        return list(stored)
    return [
        {
            "provider": provider,
            "display_name": display,
            "status": "never",
            "model_count": 0,
            "message": "尚未检查",
        }
        for provider, display in NATIVE_LISTING_PROVIDERS.items()
    ]


async def run_native_check(*, user_id: str) -> dict[str, Any]:
    """Fetch all native listings and diff them against the DB-backed merged catalog. No writes."""
    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
            run_id = await create_native_sync_run(started_by=user_id, kind="check", conn=conn)
            try:
                listings = await collect_native_listings(user_id)

                compiled = await get_compiled_catalog_async(conn=conn)
                llm_config = compiled.model_config.llm_config or {}
                awaiting_keys = {
                    key
                    for key, meta in (compiled.dalton_meta or {}).items()
                    if isinstance(meta, dict) and meta.get("awaiting_release")
                }
                all_retired = set(
                    compiled.factory_retired_keys
                    | compiled.readonly_legacy_retired_keys
                    | compiled.team_retired_keys
                )
                diff = build_native_diff(
                    listings, llm_config, all_retired, awaiting_release_keys=awaiting_keys
                )

                provider_rows = [
                    {
                        "provider": listing.provider,
                        "display_name": listing.display_name,
                        "status": listing.status,
                        "model_count": len(listing.models),
                        "message": listing.message,
                        "error_kind": listing.error_kind,
                    }
                    for listing in listings.values()
                ]
                counts = {
                    "providers_ok": sum(1 for p in provider_rows if p["status"] == "ok"),
                    "providers_unsupported": sum(
                        1 for p in provider_rows if p["status"] == "unsupported"
                    ),
                    "providers_error": sum(1 for p in provider_rows if p["status"] == "error"),
                    "added": len(diff["added"]),
                    "deprecated": len(diff["deprecated"]),
                    "unchanged": diff["unchanged"],
                }
                warnings = [
                    f"{p['display_name']}：{p['message'] or p.get('error_kind') or '检查失败'}"
                    for p in provider_rows
                    if p["status"] == "error"
                ]

                if counts["providers_ok"] == 0:
                    message = "；".join(warnings) or "没有任何供应商返回可用模型列表"
                    await finish_native_sync_run(
                        run_id=run_id,
                        status="failed",
                        counts=counts,
                        diff={"providers": provider_rows},
                        warnings=warnings or None,
                        error="no_provider_ok",
                        conn=conn,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "no_provider_ok",
                            "message": f"本次检查没有任何供应商成功：{message}",
                            "providers": provider_rows,
                        },
                    )

                refs = await _collect_reference_map()
                for dep in diff["deprecated"]:
                    dep["in_use_by"] = sorted(set(refs.get(dep["key"], [])))

                diff_out = {**diff, "providers": provider_rows}
                await finish_native_sync_run(
                    run_id=run_id,
                    status="succeeded",
                    counts=counts,
                    diff=diff_out,
                    warnings=warnings or None,
                    conn=conn,
                )
                return {
                    "run_id": run_id,
                    "checked_at": _utcnow_iso(),
                    "catalog_revision": compiled.revision,
                    "providers": provider_rows,
                    "diff": {
                        "added": diff["added"],
                        "deprecated": diff["deprecated"],
                        "unchanged": diff["unchanged"],
                    },
                    "counts": counts,
                    "warnings": warnings,
                }
            except HTTPException:
                raise
            except Exception as exc:
                err = sanitize_error_text(exc)
                logger.exception("[native_admin] check failed: %s", err)
                await finish_native_sync_run(
                    run_id=run_id, status="failed", error=err, conn=conn
                )
                raise HTTPException(
                    status_code=500, detail={"code": "check_failed", "message": err}
                ) from exc
        finally:
            await release_native_sync_lock(conn)


async def run_native_sync(*, user_id: str, model_keys: list[str]) -> dict[str, Any]:
    """Apply selected `model_keys` from the last succeeded check into ``dalton_team_model_catalog``."""
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要加入的模型。"},
        )

    committed_compiled = None
    committed_rev: int | None = None

    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
            last_check = await fetch_last_succeeded_native_check(conn=conn)
            check_diff = (last_check or {}).get("diff") or {}
            added_by_key = {
                str(item.get("suggested_key")): item for item in check_diff.get("added") or []
            }

            unknown = [k for k in model_keys if k not in added_by_key]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "unknown_models",
                        "message": "以下模型不在最近一次检查的新增列表中，请重新检查后再确认：",
                        "models": unknown,
                    },
                )
            conflicting = [k for k in model_keys if added_by_key[k].get("conflict")]
            if conflicting:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "key_conflict",
                        "message": "以下建议的 key 存在冲突，需人工确认命名后再同步：",
                        "models": [
                            f"{k}（{added_by_key[k].get('conflict_reason') or '冲突'}）"
                            for k in conflicting
                        ],
                    },
                )

            flagged_retired = [
                k for k in model_keys if added_by_key[k].get("previously_retired")
            ]
            if flagged_retired:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "previously_retired",
                        "message": "以下模型之前已下线、上游重新出现；如需恢复请在“已下线的模型”中点恢复：",
                        "models": flagged_retired,
                    },
                )

            # Pre-flight fail-closed check before creating a sync run row:
            # missing catalog row, failed legacy import, unreadable pending legacy file,
            # or a key retired since the last check must reject without polluting run history.
            cat_preview = await get_catalog_row(conn=conn)
            if cat_preview is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "retired_list_unreadable",
                        "message": "无法读取团队模型目录的已下线名单（dalton_team_model_catalog 缺失），已拒绝同步。",
                    },
                )
            if cat_preview["legacy_sync_import_status"] == "failed":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "legacy_overlay_import_failed",
                        "message": "旧目录文件导入失败，已暂停原生模型同步写入；请先在团队模型目录中完成修复（重试导入或确认放弃旧文件）。",
                    },
                )
            pending_disk_retired: set[str] = set()
            if cat_preview["legacy_sync_import_status"] == "pending":
                try:
                    pending_disk_retired = read_retired_keys_strict()
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "retired_list_unreadable",
                            "message": "无法读取旧同步文件的已下线名单，已拒绝同步；请先检查或修复旧目录文件。",
                        },
                    ) from exc

            preview_compiled = compile_catalog_from_payload(
                cat_preview["payload"],
                revision=int(cat_preview["revision"]),
                legacy_sync_import_status=cat_preview["legacy_sync_import_status"],
                last_known_legacy_retired=cat_preview["last_known_legacy_retired"],
            )
            preview_retired = (
                preview_compiled.factory_retired_keys
                | preview_compiled.readonly_legacy_retired_keys
                | preview_compiled.team_retired_keys
                | pending_disk_retired
            )
            previously_retired_preview = [k for k in model_keys if k in preview_retired]
            if previously_retired_preview:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "previously_retired",
                        "message": "以下模型之前已下线、上游重新出现；如需恢复请在“已下线的模型”中点恢复：",
                        "models": previously_retired_preview,
                    },
                )

            run_id = await create_native_sync_run(started_by=user_id, kind="sync", conn=conn)
            try:
                async with conn.transaction():
                    await acquire_catalog_exclusive_lock(conn)
                    cat_row = await get_catalog_row(conn=conn, for_update=True)
                    if cat_row is None:
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "retired_list_unreadable",
                                "message": "无法读取团队模型目录的已下线名单（dalton_team_model_catalog 缺失），已拒绝同步。",
                            },
                        )
                    # §2.6 #3: lock native sync writes while legacy import is in 'failed' state
                    if cat_row["legacy_sync_import_status"] == "failed":
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "legacy_overlay_import_failed",
                                "message": "旧目录文件导入失败，已暂停原生模型同步写入；请先在团队模型目录中完成修复（重试导入或确认放弃旧文件）。",
                            },
                        )

                    cur_rev = int(cat_row["revision"])
                    current_compiled = compile_catalog_from_payload(
                        cat_row["payload"],
                        revision=cur_rev,
                        legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                        last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                    )
                    live_retired = (
                        current_compiled.factory_retired_keys
                        | current_compiled.readonly_legacy_retired_keys
                        | current_compiled.team_retired_keys
                        | pending_disk_retired
                    )
                    previously_retired = [
                        k
                        for k in model_keys
                        if k in live_retired or added_by_key[k].get("previously_retired")
                    ]
                    if previously_retired:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "previously_retired",
                                "message": "以下模型之前已下线、上游重新出现；如需恢复请在“已下线的模型”中点恢复：",
                                "models": previously_retired,
                            },
                        )

                    llm_config = current_compiled.model_config.llm_config or {}
                    selected = [added_by_key[k] for k in model_keys]
                    models_out, meta_out = build_overlay_entries(
                        selected, llm_config, current_compiled.dalton_meta
                    )
                    skipped = [k for k in model_keys if k not in models_out]
                    applied_keys = sorted(models_out)
                    stamp: str | None = None
                    if models_out:
                        next_payload = copy.deepcopy(cat_row["payload"])
                        next_payload["schema"] = CATALOG_SCHEMA
                        dst_models = next_payload.setdefault("models", {})
                        dst_meta = next_payload.setdefault("dalton_meta", {})
                        for mk, mv in models_out.items():
                            dst_models.setdefault(mk, copy.deepcopy(mv))
                        for mk, mv in meta_out.items():
                            dst_meta.setdefault(mk, copy.deepcopy(mv))
                        committed_compiled = trial_compile_and_validate_catalog(
                            next_payload,
                            revision=cur_rev + 1,
                            legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                            last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                        )
                        saved = await save_catalog_revision(
                            conn,
                            expected_revision=cur_rev,
                            action="native_sync",
                            target_keys=applied_keys,
                            payload=next_payload,
                            diff_summary={"added_keys": applied_keys, "skipped_keys": skipped},
                            side_effects=[],
                            actor_user_id=user_id,
                            comment="NativeModelsCard sync",
                        )
                        committed_rev = int(saved["catalog"]["revision"])
                        stamp = f"db:rev:{committed_rev}"

                await finish_native_sync_run(
                    run_id=run_id,
                    status="succeeded",
                    counts={"added": len(applied_keys), "skipped": len(skipped)},
                    diff={"added_keys": applied_keys, "skipped_keys": skipped},
                    overlay_stamp=stamp,
                    conn=conn,
                )
            except HTTPException as exc:
                await finish_native_sync_run(
                    run_id=run_id,
                    status="failed",
                    error=str(exc.detail),
                    conn=conn,
                )
                raise
            except Exception as exc:
                err = sanitize_error_text(exc)
                logger.exception("[native_admin] native sync failed: %s", err)
                await finish_native_sync_run(
                    run_id=run_id, status="failed", error=err, conn=conn
                )
                raise HTTPException(
                    status_code=500, detail={"code": "sync_failed", "message": err}
                ) from exc
        finally:
            await release_native_sync_lock(conn)

    if committed_compiled is not None and committed_rev is not None:
        cache_compiled_catalog(committed_compiled)
        await publish_catalog_changed(committed_rev)
        await invalidate_catalog_redis_caches(
            actor_user_id=user_id, compiled=committed_compiled
        )
    return {
        "run_id": run_id,
        "applied": bool(applied_keys),
        "added_keys": applied_keys,
        "skipped_keys": skipped,
        "catalog_revision": committed_rev,
        "synced_at": _utcnow_iso(),
    }


async def run_native_retire(*, user_id: str, model_keys: list[str]) -> dict[str, Any]:
    """Retire selected deprecated models into ``dalton_team_model_catalog`` under exclusive lock."""
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要下线的模型。"},
        )

    committed_compiled = None
    committed_rev: int | None = None

    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
            run_id = await create_native_sync_run(started_by=user_id, kind="retire", conn=conn)
            try:
                last_check = await fetch_last_succeeded_native_check(conn=conn)
                deprecated_by_key = {
                    str(item.get("key")): item
                    for item in (((last_check or {}).get("diff") or {}).get("deprecated") or [])
                }

                retired_at = _utcnow_iso()
                async with conn.transaction():
                    await acquire_catalog_exclusive_lock(conn)
                    try:
                        refs = await _collect_reference_map(strict=True)
                    except ReferenceSourceUnavailable as exc:
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "error": "reference_check_failed",
                                "source": exc.source,
                                "message": f"引用检查失败（{exc.source}），已拒绝下线；请稍后重试或查看服务端日志。",
                            },
                        ) from exc.cause

                    cat_row = await get_catalog_row(conn=conn, for_update=True)
                    if cat_row is None:
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "catalog_unavailable",
                                "error": "catalog_unavailable",
                                "message": "团队模型目录未初始化（缺少 dalton_team_model_catalog 基线行），已拒绝下线。",
                            },
                        )
                    cur_rev = int(cat_row["revision"])
                    current_compiled = compile_catalog_from_payload(
                        cat_row["payload"],
                        revision=cur_rev,
                        legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                        last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                    )

                    already_retired = (
                        current_compiled.factory_retired_keys
                        | current_compiled.readonly_legacy_retired_keys
                        | current_compiled.team_retired_keys
                    )
                    results: list[dict[str, Any]] = []
                    records: list[dict[str, Any]] = []
                    for key in model_keys:
                        if key not in deprecated_by_key:
                            results.append({"key": key, "status": "not_deprecated"})
                            continue
                        in_use_by = sorted(set(refs.get(key, [])))
                        if in_use_by:
                            results.append({"key": key, "status": "in_use", "in_use_by": in_use_by})
                            continue
                        results.append({"key": key, "status": "retired"})
                        if key not in already_retired:
                            records.append(
                                {
                                    "key": key,
                                    "provider": str(deprecated_by_key[key].get("provider") or ""),
                                    "retired_at": retired_at,
                                    "retired_by": user_id,
                                }
                            )

                    stamp: str | None = None
                    if records:
                        next_payload = copy.deepcopy(cat_row["payload"])
                        next_payload["schema"] = CATALOG_SCHEMA
                        retired_list = list(next_payload.get("retired") or [])
                        retired_list.extend(records)
                        next_payload["retired"] = retired_list
                        committed_compiled = trial_compile_and_validate_catalog(
                            next_payload,
                            revision=cur_rev + 1,
                            legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                            last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                        )
                        saved = await save_catalog_revision(
                            conn,
                            expected_revision=cur_rev,
                            action="native_retire",
                            target_keys=sorted(r["key"] for r in records),
                            payload=next_payload,
                            diff_summary={"kind": "retire", "results": results},
                            side_effects=[],
                            actor_user_id=user_id,
                            comment="NativeModelsCard retire",
                        )
                        committed_rev = int(saved["catalog"]["revision"])
                        stamp = f"db:rev:{committed_rev}"

                counts = {
                    "retired": sum(1 for r in results if r["status"] == "retired"),
                    "in_use": sum(1 for r in results if r["status"] == "in_use"),
                    "not_deprecated": sum(1 for r in results if r["status"] == "not_deprecated"),
                }
                await finish_native_sync_run(
                    run_id=run_id,
                    status="succeeded",
                    counts=counts,
                    diff={"kind": "retire", "results": results},
                    overlay_stamp=stamp,
                    conn=conn,
                )
            except HTTPException as exc:
                await finish_native_sync_run(
                    run_id=run_id,
                    status="failed",
                    error=f"retire_rejected: {sanitize_error_text(str(exc.detail))}",
                    conn=conn,
                )
                raise
            except Exception as exc:
                err = sanitize_error_text(exc)
                logger.exception("[native_admin] native retire failed: %s", err)
                await finish_native_sync_run(
                    run_id=run_id, status="failed", error=err, conn=conn
                )
                raise HTTPException(
                    status_code=500, detail={"code": "retire_failed", "message": err}
                ) from exc
        finally:
            await release_native_sync_lock(conn)

    if committed_compiled is not None and committed_rev is not None:
        cache_compiled_catalog(committed_compiled)
        await publish_catalog_changed(committed_rev)
        await invalidate_catalog_redis_caches(
            actor_user_id=user_id, compiled=committed_compiled
        )
    return {
        "run_id": run_id,
        "results": results,
        "retired_keys": sorted(r["key"] for r in records),
        "catalog_revision": committed_rev,
        "retired_at": retired_at,
    }


async def run_native_restore(*, user_id: str, model_keys: list[str]) -> dict[str, Any]:
    """Unhide retired models from ``dalton_team_model_catalog`` (refuses readonly legacy keys 409)."""
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要恢复的模型。"},
        )

    committed_compiled = None
    committed_rev: int | None = None

    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
            run_id = await create_native_sync_run(started_by=user_id, kind="restore", conn=conn)
            try:
                async with conn.transaction():
                    await acquire_catalog_exclusive_lock(conn)
                    cat_row = await get_catalog_row(conn=conn, for_update=True)
                    if cat_row is None:
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "catalog_unavailable",
                                "error": "catalog_unavailable",
                                "message": "团队模型目录未初始化（缺少 dalton_team_model_catalog 基线行），已拒绝恢复。",
                            },
                        )
                    cur_rev = int(cat_row["revision"])

                    extra_readonly: set[str] = set()
                    if cat_row["legacy_sync_import_status"] == "failed":
                        extra_readonly |= extract_disk_legacy_retired_readonly()
                        try:
                            extra_readonly |= await fetch_legacy_retired_history(conn=conn)
                        except Exception:
                            pass

                    current_compiled = compile_catalog_from_payload(
                        cat_row["payload"],
                        revision=cur_rev,
                        legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                        last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                        extra_readonly_legacy_retired=extra_readonly,
                    )

                    # §2.2 Step C & §2.6 #3: readonly legacy keys cannot be restored until import resolved
                    locked_legacy = [
                        k for k in model_keys if k in current_compiled.readonly_legacy_retired_keys
                    ]
                    if locked_legacy:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "legacy_retired_locked_until_import_resolved",
                                "models": locked_legacy,
                                "message": "以下模型属于导入失败期间的只读遗产退役集，需先执行重试导入或确认放弃旧文件后才可恢复。",
                            },
                        )

                    factory_locked = [
                        k for k in model_keys if k in current_compiled.factory_retired_keys
                    ]
                    if factory_locked:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "factory_retired_immutable",
                                "models": factory_locked,
                                "message": "出厂硬退役模型不可恢复。",
                            },
                        )

                    req_set = set(model_keys)
                    next_payload = copy.deepcopy(cat_row["payload"])
                    next_payload["schema"] = CATALOG_SCHEMA
                    existing_retired = list(next_payload.get("retired") or [])
                    remaining: list[dict[str, Any]] = []
                    restored_keys: list[str] = []
                    for rec in existing_retired:
                        rkey = str(rec.get("key") or "") if isinstance(rec, dict) else ""
                        if rkey and rkey in req_set:
                            restored_keys.append(rkey)
                        else:
                            remaining.append(rec)
                    restored_keys = sorted(set(restored_keys))

                    stamp: str | None = None
                    if restored_keys:
                        next_payload["retired"] = remaining
                        committed_compiled = trial_compile_and_validate_catalog(
                            next_payload,
                            revision=cur_rev + 1,
                            legacy_sync_import_status=cat_row["legacy_sync_import_status"],
                            last_known_legacy_retired=cat_row["last_known_legacy_retired"],
                        )
                        saved = await save_catalog_revision(
                            conn,
                            expected_revision=cur_rev,
                            action="native_restore",
                            target_keys=restored_keys,
                            payload=next_payload,
                            diff_summary={"restored_keys": restored_keys},
                            side_effects=[],
                            actor_user_id=user_id,
                            comment="NativeModelsCard restore",
                        )
                        committed_rev = int(saved["catalog"]["revision"])
                        stamp = f"db:rev:{committed_rev}"

                results = [
                    {"key": k, "status": "restored" if k in restored_keys else "not_retired"}
                    for k in model_keys
                ]
                await finish_native_sync_run(
                    run_id=run_id,
                    status="succeeded",
                    counts={"restored": len(restored_keys)},
                    diff={"kind": "restore", "results": results},
                    overlay_stamp=stamp,
                    conn=conn,
                )
            except HTTPException as exc:
                await finish_native_sync_run(
                    run_id=run_id, status="failed", error=str(exc.detail), conn=conn
                )
                raise
            except Exception as exc:
                err = sanitize_error_text(exc)
                logger.exception("[native_admin] native restore failed: %s", err)
                await finish_native_sync_run(
                    run_id=run_id, status="failed", error=err, conn=conn
                )
                raise HTTPException(
                    status_code=500, detail={"code": "restore_failed", "message": err}
                ) from exc
        finally:
            await release_native_sync_lock(conn)

    if committed_compiled is not None and committed_rev is not None:
        cache_compiled_catalog(committed_compiled)
        await publish_catalog_changed(committed_rev)
        await invalidate_catalog_redis_caches(
            actor_user_id=user_id, compiled=committed_compiled
        )
    return {
        "run_id": run_id,
        "results": results,
        "restored_keys": restored_keys,
        "catalog_revision": committed_rev,
        "restored_at": _utcnow_iso(),
    }


async def get_native_status() -> dict[str, Any]:
    """Last check/sync runs, per-provider rows, and DB-backed retired models (no fetch)."""
    last_check, last_sync = await fetch_recent_native_runs()
    last_succeeded = await fetch_last_succeeded_native_check()
    compiled = await get_compiled_catalog_async()
    cat_row = await get_catalog_row()
    check_diff = (last_succeeded or {}).get("diff") or {}

    retired: list[dict[str, Any]] = []
    seen_retired: set[str] = set()
    for rec in compiled.payload.get("retired") or []:
        if isinstance(rec, dict) and rec.get("key"):
            rkey = str(rec["key"])
            seen_retired.add(rkey)
            retired.append(
                {
                    **rec,
                    "readonly_legacy": rkey in compiled.readonly_legacy_retired_keys,
                }
            )
    for rkey in sorted(compiled.readonly_legacy_retired_keys):
        if rkey not in seen_retired:
            seen_retired.add(rkey)
            retired.append({"key": rkey, "provider": "", "readonly_legacy": True})

    all_retired_keys = (
        compiled.factory_retired_keys
        | compiled.readonly_legacy_retired_keys
        | compiled.team_retired_keys
    )
    llm_config = compiled.model_config.llm_config or {}
    pending_added = [
        {**item, "previously_retired": str(item.get("suggested_key")) in all_retired_keys}
        for item in check_diff.get("added") or []
        if str(item.get("suggested_key")) not in llm_config
    ]
    pending_deprecated = [
        item
        for item in check_diff.get("deprecated") or []
        if str(item.get("key")) not in all_retired_keys
    ]
    return {
        "providers": _listing_rows(last_succeeded),
        "last_check": last_check,
        "last_succeeded_check": last_succeeded,
        "last_sync": last_sync,
        "catalog_revision": compiled.revision,
        "legacy_sync_import_status": compiled.legacy_sync_import_status,
        "legacy_sync_import_error": (cat_row or {}).get("legacy_sync_import_error"),
        "diff": {
            "added": pending_added,
            "deprecated": pending_deprecated,
            "unchanged": check_diff.get("unchanged") or 0,
        },
        "retired": retired,
        "overlay": {
            "exists": bool(compiled.payload.get("models")),
            "generated_at": (cat_row or {}).get("updated_at"),
            "model_count": len(compiled.payload.get("models") or {}),
            "path": str(NATIVE_SYNC_OVERLAY_PATH),
            "storage": "postgresql:dalton_team_model_catalog",
        },
    }
