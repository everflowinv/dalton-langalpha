"""Native-subscription model check/sync/status service.

`check` fetches every native provider's model list with the team credential's
token and returns the diff without writing anything. `sync` applies the models
the PM selected from the last succeeded check to the runtime overlay
(`var/dalton/catalog/native_sync_overlay.json`) — add-only, atomic, and followed
by the same local cache invalidation OpenClaw sync uses, so every worker picks
the new catalog up through the overlay stamp. Nothing here schedules itself,
auto-syncs, removes deprecated models, or touches default model settings.
"""

from __future__ import annotations

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
    write_native_sync_overlay,
)
from src.dalton.services.openclaw_admin import (
    _collect_model_references,
    _invalidate_local_worker_model_caches,
)
from src.dalton.settings import get_assist_fallback_models, get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_NATIVE_CHECK_LOCK_BUSY_MESSAGE = "另一个原生模型检查或同步正在进行中，请稍候。"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _collect_reference_map() -> dict[str, list[str]]:
    """agent_config + user preferences (via OpenClaw's collector) + Dalton settings items."""
    refs = await _collect_model_references()
    settings = get_dalton_settings()
    for reviewer_model in settings.publish.reviewer_models:
        refs.setdefault(str(reviewer_model), []).append("dalton.publish.reviewer_models")
    extract_model = str(settings.publish.extract_model or "").strip()
    if extract_model:
        refs.setdefault(extract_model, []).append("dalton.publish.extract_model")
    extractor = str(settings.conversations.extractor_model or "").strip()
    if extractor:
        refs.setdefault(extractor, []).append("dalton.conversations.extractor_model")
    assist_model = str(settings.models.assist_model or "").strip()
    if assist_model:
        refs.setdefault(assist_model, []).append("dalton.models.assist_model")
    for fallback in get_assist_fallback_models():
        refs.setdefault(fallback, []).append("dalton.models.assist_fallback_model")
    return refs


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
                diff = build_native_diff(listings, llm_config)

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

    last_check = await fetch_last_succeeded_native_check()
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

    async with get_db_connection() as conn:
        locked = await try_acquire_native_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": _NATIVE_CHECK_LOCK_BUSY_MESSAGE},
            )
        try:
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
    # The stored diff is a snapshot from check time; keys a later sync already
    # added are part of the catalog now, so they stop counting as "new".
    from src.llms.llm import LLM

    llm_config = LLM.get_model_config().llm_config or {}
    pending_added = [
        item
        for item in check_diff.get("added") or []
        if str(item.get("suggested_key")) not in llm_config
    ]
    return {
        "providers": _listing_rows(last_succeeded),
        "last_check": last_check,
        "last_succeeded_check": last_succeeded,
        "last_sync": last_sync,
        "diff": {
            "added": pending_added,
            "deprecated": check_diff.get("deprecated") or [],
            "unchanged": check_diff.get("unchanged") or 0,
        },
        "overlay": {
            "exists": bool(overlay),
            "generated_at": overlay_meta.get("generated_at"),
            "model_count": overlay_meta.get("model_count") or len((overlay or {}).get("models") or {}),
            "path": str(NATIVE_SYNC_OVERLAY_PATH),
        },
    }
