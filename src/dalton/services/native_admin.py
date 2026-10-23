"""Native-subscription model check/sync/retire/status service.

`check` fetches every native provider's model list with the team credential's
token and returns the diff without writing anything. `sync` applies the models
the PM selected from the last succeeded check to the runtime overlay
(`var/dalton/catalog/native_sync_overlay.json`) — add-only, atomic, and followed
by the same local cache invalidation OpenClaw sync uses, so every worker picks
the new catalog up through the overlay stamp. `retire`/`restore` manage the
overlay's `retired` list (hide/unhide in the merged catalog) under the same
lock; a model referenced by any setting, preference, subagent, or automation is
refused. Nothing here schedules itself, auto-syncs, auto-restores, or touches
default model settings.
"""

from __future__ import annotations

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
from src.dalton.llm.native_catalog import (
    NATIVE_LISTING_PROVIDERS,
    NATIVE_SYNC_OVERLAY_PATH,
    build_native_diff,
    build_overlay_entries,
    collect_native_listings,
    load_native_sync_overlay,
    load_retired_models,
    read_retired_keys_strict,
    retired_key_set,
    retire_native_models,
    restore_native_models,
    write_native_sync_overlay,
)
from src.dalton.services import model_references
from src.dalton.services.model_references import ReferenceSourceUnavailable
from src.dalton.services.openclaw_admin import _invalidate_local_worker_model_caches
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_NATIVE_CHECK_LOCK_BUSY_MESSAGE = "另一个原生模型检查或同步正在进行中，请稍候。"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _collect_reference_map(*, strict: bool = False) -> dict[str, list[str]]:
    """Merge every reference source into `{model_key: [source, ...]}`.

    Lenient (the check's "in use" display): a source that cannot be read is
    logged and skipped. Strict (retire): the first unreadable source raises
    `ReferenceSourceUnavailable`, because a model must not be hidden while any
    source that could be holding a reference to it is a question mark.
    """
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
    """Fetch all native listings and diff them against the merged catalog. No writes."""
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

                from src.llms.llm import LLM

                llm_config = LLM.get_model_config().llm_config or {}
                diff = build_native_diff(listings, llm_config, retired_key_set())

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

                # No provider succeeding (or answering anything parseable)
                # means this check proved nothing: record it failed so the last
                # succeeded check keeps feeding the card and a confirm-sync.
                # This run's own provider errors ride its diff; the succeeded
                # check's diff stays untouched.
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
    """Apply selected `model_keys` from the last succeeded check to the runtime overlay."""
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要加入的模型。"},
        )

    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
            # The lock is held before the check is read (same rule as retire):
            # neither a retire nor another confirm can swap the selection basis
            # under this sync. previously_retired is re-judged from the live
            # retire list, since a retire may have landed after the check.
            last_check = await fetch_last_succeeded_native_check(conn=conn)
            check_diff = ((last_check or {}).get("diff") or {})
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
            # The retire list is read loud: an unreadable overlay answering
            # "nothing retired" would let this sync re-add a retired model.
            try:
                live_retired = read_retired_keys_strict()
            except ValueError:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "retired_list_unreadable",
                        "message": "已下线名单读取失败，无法校验所选模型；请检查运行时 overlay 文件后重试。",
                    },
                )
            previously_retired = [
                k
                for k in model_keys
                if k in live_retired or added_by_key[k].get("previously_retired")
            ]
            if previously_retired:
                # The overlay merge hides retired keys, so syncing one would
                # write an entry the catalog then filters out. Restore it
                # first instead.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "previously_retired",
                        "message": "以下模型之前已下线、上游重新出现；如需恢复请在“已下线的模型”中点恢复：",
                        "models": previously_retired,
                    },
                )

            run_id = await create_native_sync_run(started_by=user_id, kind="sync", conn=conn)
            try:
                from src.dalton.llm.overlay import load_dalton_meta
                from src.llms.llm import LLM

                llm_config = LLM.get_model_config().llm_config or {}
                selected = [added_by_key[k] for k in model_keys]
                models_out, meta_out = build_overlay_entries(
                    selected, llm_config, load_dalton_meta()
                )
                skipped = [k for k in model_keys if k not in models_out]
                applied_keys = sorted(models_out)
                stamp: str | None = None
                if models_out:
                    # Writing the file is what every worker sees: each one re-stats
                    # the overlay sources (throttled to 1 stat / 2s) and rebuilds
                    # its model config when the stamp moves; the local cache clear
                    # just makes the worker that served this request immediate.
                    stamp = write_native_sync_overlay(
                        models_out, meta_out, added_keys=applied_keys
                    )
                    _invalidate_local_worker_model_caches()

                await finish_native_sync_run(
                    run_id=run_id,
                    status="succeeded",
                    counts={"added": len(applied_keys), "skipped": len(skipped)},
                    diff={"added_keys": applied_keys, "skipped_keys": skipped},
                    overlay_stamp=stamp,
                    conn=conn,
                )
                return {
                    "run_id": run_id,
                    "applied": bool(applied_keys),
                    "added_keys": applied_keys,
                    "skipped_keys": skipped,
                    "synced_at": _utcnow_iso(),
                }
            except HTTPException:
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


async def run_native_retire(*, user_id: str, model_keys: list[str]) -> dict[str, Any]:
    """Hide selected deprecated models by adding them to the overlay's `retired` list.

    A key must appear in the last succeeded check's deprecated list (retire is
    not a way to hide arbitrary models). A referenced model is refused with its
    reference locations; the rest still retire. Nothing is deleted — the model
    entry stays in the overlay/upstream, so a restore brings it straight back.
    The reference check is strict: any unreadable source rejects the whole
    retire, because a hidden model with a dangling reference breaks every turn
    that tries it.
    """
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要下线的模型。"},
        )

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
                # The lock is held before the check is read: a check running
                # beside this retire cannot swap the deprecated list under us.
                last_check = await fetch_last_succeeded_native_check(conn=conn)
                deprecated_by_key = {
                    str(item.get("key")): item
                    for item in (((last_check or {}).get("diff") or {}).get("deprecated") or [])
                }

                try:
                    refs = await _collect_reference_map(strict=True)
                except ReferenceSourceUnavailable as exc:
                    # The exception text (file paths, driver errors, anything a
                    # source leaked) never reaches the client or the run row
                    # unsanitized; the client gets a fixed sentence and the
                    # source name, the run row the sanitized cause.
                    await finish_native_sync_run(
                        run_id=run_id,
                        status="failed",
                        error=f"reference_check_failed: {sanitize_error_text(exc)}",
                        conn=conn,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error": "reference_check_failed",
                            "source": exc.source,
                            "message": f"引用检查失败（{exc.source}），已拒绝下线；请稍后重试或查看服务端日志。",
                        },
                    ) from exc.cause

                already_retired = retired_key_set()
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
                                "retired_at": _utcnow_iso(),
                                "retired_by": user_id,
                            }
                        )

                stamp: str | None = None
                if records:
                    stamp = retire_native_models(records)
                    _invalidate_local_worker_model_caches()

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
                return {
                    "run_id": run_id,
                    "results": results,
                    "retired_keys": sorted(r["key"] for r in records),
                    "retired_at": _utcnow_iso(),
                }
            except HTTPException:
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


async def run_native_restore(*, user_id: str, model_keys: list[str]) -> dict[str, Any]:
    """Unhide retired models by removing their keys from the overlay's `retired` list."""
    if not model_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_selection", "message": "未选择要恢复的模型。"},
        )
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
                stamp, restored_keys = restore_native_models(list(model_keys))
                if restored_keys:
                    _invalidate_local_worker_model_caches()
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
                return {
                    "run_id": run_id,
                    "results": results,
                    "restored_keys": restored_keys,
                    "restored_at": _utcnow_iso(),
                }
            except HTTPException:
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


async def get_native_status() -> dict[str, Any]:
    """Last check/sync runs and per-provider rows — reads stored results only, no fetch.

    The diff and provider rows come from the last SUCCEEDED check; the most
    recent run (any status) only timestamps "上次检查". A failed check keeps its
    own provider errors in its run row and never displaces the good diff.
    """
    last_check, last_sync = await fetch_recent_native_runs()
    last_succeeded = await fetch_last_succeeded_native_check()
    overlay = load_native_sync_overlay()
    overlay_meta = (overlay or {}).get("meta") or {}
    check_diff = ((last_succeeded or {}).get("diff") or {})
    retired = load_retired_models()
    retired_keys = {str(item.get("key")) for item in retired}
    # The stored diff is a snapshot from check time; keys a later sync already
    # added are part of the catalog now, so they stop counting as "new".
    from src.llms.llm import LLM

    llm_config = LLM.get_model_config().llm_config or {}
    pending_added = [
        {**item, "previously_retired": str(item.get("suggested_key")) in retired_keys}
        for item in check_diff.get("added") or []
        if str(item.get("suggested_key")) not in llm_config
    ]
    # Retired models are already out of the merged catalog, so the stored diff's
    # deprecated list may lag one check behind — drop anything since retired.
    pending_deprecated = [
        item
        for item in check_diff.get("deprecated") or []
        if str(item.get("key")) not in retired_keys
    ]
    return {
        "providers": _listing_rows(last_succeeded),
        "last_check": last_check,
        "last_succeeded_check": last_succeeded,
        "last_sync": last_sync,
        "diff": {
            "added": pending_added,
            "deprecated": pending_deprecated,
            "unchanged": check_diff.get("unchanged") or 0,
        },
        "retired": retired,
        "overlay": {
            "exists": bool(overlay),
            "generated_at": overlay_meta.get("generated_at"),
            "model_count": overlay_meta.get("model_count") or len((overlay or {}).get("models") or {}),
            "path": str(NATIVE_SYNC_OVERLAY_PATH),
        },
    }
