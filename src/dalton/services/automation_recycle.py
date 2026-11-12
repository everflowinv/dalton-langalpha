"""Soft delete, restore, and one-step rollback for automations and data tasks.

Agent writes lock user preferences before the automation row when the caller
has not already locked them, then lock unfinished executions. Script writes
lock only the definition row. Physical deletion happens in the daily purge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from ptc_agent.core.paths import AUTOMATION_FILE_NAME

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID, is_company_system_user
from src.dalton.database import automation_recycle as recycle_db
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.database.company_automations import (
    _resolve_effective_once_run_at,
    lock_and_fetch_user_preferences_for_snapshot,
    resolve_snapshot_notify_fields,
)
from src.dalton.services.company_agent_writes import (
    _MIGRATION_FREEZE_STAGES,
    company_agent_write_response,
    company_migration_stage,
)
from src.dalton.services.company_automations import (
    ALLOWED_AUTOMATION_SNAPSHOT_FIELDS,
    AutomationVersionConflictError,
    build_automation_config_snapshot,
    compute_snapshot_changes,
    require_company_scope_read_access,
    resolve_desired_status_for_snapshot,
    validate_existing_history_state,
)
from src.dalton.services.company_script_writes import (
    _assert_migration_writable,
    _authorize_script_writer,
    _field_changes,
    _require_manifest_job,
    _script_history_or_refuse,
    _script_snapshot,
)
from src.dalton.services.company_task_read import (
    CompanyDataTaskReadError,
    InvalidAutomationId,
    get_script_automation,
    is_shared_company_agent,
    parse_automation_read_id,
)
from src.server.database.pool import get_db_connection
from src.server.dependencies.principal import Principal
from src.server.models.automation import AutomationReadResponse, AutomationResponse
from src.server.utils.api import raise_not_found

_UNFINISHED = frozenset({"pending", "waiting", "running"})
_KEEP_PREV = object()
_SNAPSHOT_COLUMNS = (
    "name",
    "description",
    "trigger_type",
    "cron_expression",
    "timezone",
    "instruction",
    "workspace_id",
    "llm_model",
    "additional_context",
    "thread_strategy",
    "conversation_thread_id",
    "max_failures",
    "trigger_config",
    "delivery_config",
    "notify_enabled",
    "notify_routes",
)


def pre_delete_desired_status(
    row: Mapping[str, Any],
    unfinished_statuses: Sequence[str],
) -> str:
    """Desired status recorded before a soft delete pauses the row."""
    status = str(row.get("status") or "").strip().lower()
    if status == "active":
        return "active"
    if status == "executing" and any(item in _UNFINISHED for item in unfinished_statuses):
        return "active"
    return "paused"


_AUTOMATION_FILE_NAME = re.compile(AUTOMATION_FILE_NAME)


def restored_file_name(file_name: str, shortid: str) -> str:
    """``<stem>-restored-<shortid>.json`` matching LiveFS ``AUTOMATION_FILE_NAME``.

    The pattern allows 1–64 of letters, digits, ``-`` and ``_``, starting with
    a letter or digit, then ``.json``. The ``-restored-<shortid>`` suffix is
    kept whole and the stem is cut to fit. The result is checked with that
    same pattern.
    """
    base = file_name[:-5] if file_name.endswith(".json") else file_name
    suffix = f"-restored-{shortid}"
    room = 64 - len(suffix)
    stem = (base or "automation")[:room] if room > 0 else "a"
    if not stem[:1].isalnum():
        stem = "a"
    name = f"{stem}{suffix}.json"
    if _AUTOMATION_FILE_NAME.fullmatch(name) is None:
        raise ValueError(f"restored file name is not a legal automation file name: {name}")
    return name


def decide_effective_state(
    *,
    trigger_type: str,
    desired_status: str,
    disable_reason: str | None,
    once_when: datetime | None,
    once_completed: bool,
    now: datetime,
    has_price_claim: bool,
    cron_next: datetime | None,
    kept_once_next: datetime | None,
    keep_paused_once_next: bool = True,
) -> tuple[str, datetime | None]:
    """Effective ``status`` and ``next_run_at`` for restore and rollback.

    A stored ``disable_reason`` forces ``disabled``. An expired one-shot whose
    target desire is active lands on ``paused`` with ``next_run_at`` cleared.
    A one-shot whose target desire is paused keeps the row's current
    ``next_run_at`` on restore. Rollback passes ``keep_paused_once_next``
    false, so that time stays only in the snapshot and ``next_run_at`` is NULL.
    A completed one-shot lands on ``completed``.
    """
    if disable_reason:
        return "disabled", None
    kind = str(trigger_type or "cron").strip() or "cron"
    if desired_status != "active":
        if kind == "once":
            kept = kept_once_next if keep_paused_once_next else None
            return "paused", kept
        return "paused", None
    if kind == "cron":
        return "active", cron_next
    if kind == "once":
        if once_when is not None and once_completed:
            return "completed", None
        if once_when is not None and once_when > now and not once_completed:
            return "active", once_when
        return "paused", None
    if kind == "price":
        if has_price_claim:
            return "executing", None
        return "active", None
    return "paused", None


def _parse_when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _history_list(raw: Any) -> list[Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, list):
        return list(raw)
    return []


def _find_entry(history: Sequence[Any], version: int) -> dict[str, Any] | None:
    for item in history:
        if isinstance(item, Mapping) and item.get("version") == version:
            return dict(item)
    return None


def _version_conflict(expected: int, current: int) -> None:
    raise AutomationVersionConflictError(
        f"Configuration version conflict: expected {expected}, current is {current}"
    )


def _script_conflict(expected: int, current: int) -> None:
    raise HTTPException(
        status_code=409,
        detail=f"config_version conflict: expected {expected}, found {current}",
    )


def _assert_company_recycle_row(row: Mapping[str, Any]) -> None:
    """Reuse the company identity, notify mode, and migration-freeze checks."""
    user_id = str(row.get("user_id") or "")
    scope = str(row.get("scope") or "").strip()
    if scope != "company" or not is_company_system_user(user_id):
        raise HTTPException(status_code=409, detail="Company automation identity changed")
    mode = str(row.get("notify_storage_mode") or "").strip()
    if mode != "task_row":
        raise HTTPException(
            status_code=409,
            detail="Company automations require notify_storage_mode='task_row'",
        )
    if company_migration_stage(row.get("metadata")) in _MIGRATION_FREEZE_STAGES:
        raise HTTPException(status_code=409, detail="Company automation is migration-frozen")


def _uuid_or_none(value: Any, field_name: str) -> UUID | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return UUID(text)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Rollback snapshot {field_name} is invalid",
        ) from exc


def _require_complete_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    missing = [
        name for name in ALLOWED_AUTOMATION_SNAPSHOT_FIELDS if name not in snapshot
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Rollback snapshot is incomplete: {', '.join(missing)}",
        )
    desired = snapshot.get("desired_status")
    if desired not in ("active", "paused"):
        raise HTTPException(
            status_code=409,
            detail="Rollback snapshot desired_status must be active or paused",
        )
    trigger = snapshot.get("trigger_type")
    if trigger not in ("cron", "once", "price"):
        raise HTTPException(
            status_code=409,
            detail="Rollback snapshot trigger_type is invalid",
        )
    copied = dict(snapshot)
    if not isinstance(copied.get("trigger_config"), Mapping):
        raise HTTPException(
            status_code=409,
            detail="Rollback snapshot trigger_config is invalid",
        )
    delivery = copied.get("delivery_config")
    if not isinstance(delivery, Mapping):
        raise HTTPException(
            status_code=409,
            detail="Rollback snapshot delivery_config is invalid",
        )
    routes = copied.get("notify_routes")
    if not isinstance(routes, list) or any(not isinstance(item, str) for item in routes):
        raise HTTPException(
            status_code=409,
            detail="Rollback snapshot notify_routes is invalid",
        )
    return copied


async def _append_agent_history(
    row: Mapping[str, Any],
    *,
    action: str,
    actor_user_id: str,
    post_row: Mapping[str, Any],
    post_desired: str,
    post_once: str | None,
    post_notify: tuple[bool, list[str]] | None,
    pref_row: Mapping[str, Any] | None,
    restored_from_version: int | None,
    prev_version: Any = _KEEP_PREV,
    conn: Any,
) -> tuple[list[Any], int]:
    """Seed an empty history, then append one audit entry. Returns history and new version."""
    validate_existing_history_state(row)
    pre_once = await _resolve_effective_once_run_at(row, conn=conn)
    pre_enabled, pre_routes = await resolve_snapshot_notify_fields(
        row,
        pref_row=pref_row,
        conn=conn,
    )
    pre_snapshot = build_automation_config_snapshot(
        row,
        desired_status=resolve_desired_status_for_snapshot(row),
        once_run_at=pre_once,
        notify_enabled=pre_enabled,
        notify_routes=pre_routes,
    )
    if post_notify is None:
        post_enabled, post_routes = await resolve_snapshot_notify_fields(
            post_row,
            pref_row=pref_row,
            conn=conn,
        )
    else:
        post_enabled, post_routes = post_notify
    post_snapshot = build_automation_config_snapshot(
        post_row,
        desired_status=post_desired,
        once_run_at=post_once,
        notify_enabled=post_enabled,
        notify_routes=post_routes,
    )
    changes = compute_snapshot_changes(pre_snapshot, post_snapshot)

    history = _history_list(row.get("audit_history"))
    now_iso = datetime.now(UTC).isoformat()
    if not history:
        created = row.get("created_at")
        seed_at = created.isoformat() if isinstance(created, datetime) else now_iso
        history.append(
            {
                "version": 1,
                "prev_version": None,
                "restored_from_version": None,
                "actor_user_id": row.get("created_by") or row.get("user_id"),
                "changed_at": seed_at,
                "action": "seed",
                "changes": {},
                "snapshot": pre_snapshot,
            }
        )
        old_version = 1
        old_active = 1
    else:
        old_version = int(row["config_version"])
        old_active = int(row["active_version"])
    new_version = old_version + 1
    history.append(
        {
            "version": new_version,
            "prev_version": old_active if prev_version is _KEEP_PREV else prev_version,
            "restored_from_version": restored_from_version,
            "actor_user_id": actor_user_id,
            "changed_at": now_iso,
            "action": action,
            "changes": changes,
            "snapshot": post_snapshot,
        }
    )
    return history, new_version


def _base_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    routes = row.get("notify_routes")
    route_list = list(routes) if isinstance(routes, list) else []
    trigger_config = row.get("trigger_config")
    additional = row.get("additional_context")
    delivery = row.get("delivery_config")
    return {
        "name": row.get("name"),
        "description": row.get("description"),
        "trigger_type": row.get("trigger_type"),
        "cron_expression": row.get("cron_expression"),
        "timezone": row.get("timezone") or "UTC",
        "instruction": row.get("instruction") or "",
        "workspace_id": _uuid_or_none(row.get("workspace_id"), "workspace_id"),
        "llm_model": row.get("llm_model"),
        "additional_context": additional if isinstance(additional, (dict, list)) else None,
        "thread_strategy": row.get("thread_strategy") or "new",
        "conversation_thread_id": _uuid_or_none(
            row.get("conversation_thread_id"),
            "conversation_thread_id",
        ),
        "max_failures": int(row.get("max_failures") or 3),
        "trigger_config": dict(trigger_config) if isinstance(trigger_config, Mapping) else {},
        "delivery_config": dict(delivery) if isinstance(delivery, Mapping) else {},
        "notify_enabled": bool(row.get("notify_enabled")),
        "notify_routes": [str(item) for item in route_list if isinstance(item, str)],
        "file_name": row.get("file_name"),
    }


async def _effective_for_row(
    row: Mapping[str, Any],
    *,
    desired: str,
    trigger_type: str,
    cron_expression: str | None,
    timezone_name: str | None,
    once_when: datetime | None,
    conn: Any,
    keep_paused_once_next: bool = True,
) -> tuple[str, datetime | None]:
    completed = False
    if trigger_type == "once" and once_when is not None:
        completed = await recycle_db.once_schedule_completed(
            str(row["automation_id"]),
            once_when,
            conn=conn,
        )
    price_claim = False
    if trigger_type == "price" and desired == "active":
        price_claim = await recycle_db.has_active_price_claim(
            str(row["automation_id"]),
            conn=conn,
        )
    cron_next = None
    if trigger_type == "cron" and desired == "active" and not row.get("disable_reason"):
        if not cron_expression:
            raise HTTPException(
                status_code=409,
                detail="Cannot schedule restored cron: cron_expression is empty",
            )
        from src.server.services.automation_scheduler import AutomationScheduler

        try:
            cron_next = AutomationScheduler.calculate_first_run(
                cron_expression,
                timezone_name or "UTC",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot schedule restored cron: {exc}",
            ) from exc
    return decide_effective_state(
        trigger_type=trigger_type,
        desired_status=desired,
        disable_reason=(
            str(row.get("disable_reason"))
            if row.get("disable_reason") is not None
            else None
        ),
        once_when=once_when,
        once_completed=completed,
        now=datetime.now(UTC),
        has_price_claim=price_claim,
        cron_next=cron_next,
        kept_once_next=row.get("next_run_at") if trigger_type == "once" else None,
        keep_paused_once_next=keep_paused_once_next,
    )


async def _prepare_prefs(
    row_owner: str,
    *,
    company: bool,
    prelocked_pref_row: Mapping[str, Any] | None,
    conn: Any,
) -> dict[str, Any]:
    if prelocked_pref_row is not None:
        return dict(prelocked_pref_row)
    if company:
        return {}
    locked = await lock_and_fetch_user_preferences_for_snapshot(
        row_owner,
        conn=conn,
        notify_storage_mode="legacy",
    )
    return dict(locked) if isinstance(locked, Mapping) else {}


def _notify_map_from_pref_row(
    pref_row: Mapping[str, Any],
    key: str,
    *,
    bags: tuple[str, ...],
) -> dict[str, Any]:
    """One notify map from the preference row already held in this transaction."""
    from src.dalton.llm.model_prefs import _normalize_stored_value

    for bag_key in bags:
        bag = pref_row.get(bag_key)
        if isinstance(bag, Mapping) and key in bag:
            normalized = _normalize_stored_value(key, bag.get(key))
            if isinstance(normalized, Mapping):
                return dict(normalized)
            return {}
    return {}


async def _finish_agent(
    automation_id: str,
    *,
    conn: Any,
    company: bool,
    principal: Principal | None,
    pref_row: Mapping[str, Any] | None = None,
) -> AutomationReadResponse:
    fresh = await recycle_db.fetch_automation_read_row(automation_id, conn=conn)
    if fresh is None:
        raise_not_found("Automation")
    if company:
        return company_agent_write_response(fresh)
    response = AutomationResponse.model_validate(fresh)
    if principal is not None:
        from src.dalton.llm.model_prefs import (
            DALTON_NOTIFY_OVERRIDES_KEY,
            DALTON_NOTIFY_ROUTES_KEY,
        )
        from src.dalton.notify.dispatch import populate_automation_notify_state

        if pref_row is None:
            await populate_automation_notify_state(principal.user_id, [response])
        else:
            # Built before commit. The cache and another connection still
            # have the preference row from before this write.
            held = pref_row

            async def _overrides(_user_id: str | None) -> dict[str, bool]:
                raw = _notify_map_from_pref_row(
                    held,
                    DALTON_NOTIFY_OVERRIDES_KEY,
                    bags=("model_preference", "other_preference"),
                )
                return {
                    str(item_key): value
                    for item_key, value in raw.items()
                    if isinstance(value, bool)
                }

            async def _routes(_user_id: str | None) -> dict[str, list[str]]:
                raw = _notify_map_from_pref_row(
                    held,
                    DALTON_NOTIFY_ROUTES_KEY,
                    bags=("model_preference",),
                )
                routes: dict[str, list[str]] = {}
                for item_key, value in raw.items():
                    if isinstance(value, list):
                        routes[str(item_key)] = [str(item) for item in value]
                return routes

            await populate_automation_notify_state(
                principal.user_id,
                [response],
                resolve_overrides_fn=_overrides,
                resolve_routes_fn=_routes,
            )
    return response


async def soft_delete_automation(
    automation_id: str,
    *,
    actor_user_id: str,
    expected_config_version: int | None,
    owner_user_id: str | None,
    company: bool,
    prelocked_pref_row: Mapping[str, Any] | None = None,
    conn: Any = None,
    principal: Principal | None = None,
) -> AutomationReadResponse | None:
    """Soft-delete one automation. None when the owner filter misses the row."""
    async with get_db_connection(conn) as db:
        async with db.transaction():
            pref_owner = owner_user_id or COMPANY_SYSTEM_USER_ID
            prefs = await _prepare_prefs(
                pref_owner,
                company=company,
                prelocked_pref_row=prelocked_pref_row,
                conn=db,
            )
            row = await recycle_db.lock_automation_for_recycle(
                automation_id,
                owner_user_id=owner_user_id,
                conn=db,
            )
            if row is None:
                return None
            if company:
                _assert_company_recycle_row(row)
            if row.get("deleted_at") is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Automation is already in the recycle bin",
                )
            current = int(row.get("config_version") or 1)
            if expected_config_version is None:
                expected_config_version = current
            elif expected_config_version != current:
                _version_conflict(expected_config_version, current)
            unfinished = await recycle_db.lock_unfinished_executions(
                automation_id,
                conn=db,
            )
            desired = pre_delete_desired_status(
                row,
                [str(item.get("status") or "") for item in unfinished],
            )
            once_iso = await _resolve_effective_once_run_at(row, conn=db)
            trigger = str(row.get("trigger_type") or "cron")
            next_run = row.get("next_run_at") if trigger == "once" else None
            post = dict(row)
            post["status"] = "paused"
            post["next_run_at"] = next_run
            history, new_version = await _append_agent_history(
                row,
                action="delete",
                actor_user_id=actor_user_id,
                post_row=post,
                post_desired=desired,
                post_once=once_iso,
                post_notify=None,
                pref_row=prefs,
                restored_from_version=None,
                conn=db,
            )
            await recycle_db.skip_unstarted_executions(
                automation_id,
                skip_reason="task_deleted",
                conn=db,
            )
            fields = _base_fields(row)
            fields.update(
                {
                    "status": "paused",
                    "next_run_at": next_run,
                    "deleted_by": actor_user_id,
                    "pre_delete_desired_status": desired,
                    "set_deleted_at": True,
                    "config_version": new_version,
                    "active_version": new_version,
                    "updated_by": actor_user_id,
                    "audit_history": history,
                }
            )
            wrote = await recycle_db.update_automation_recycle(
                automation_id,
                expected_config_version=current,
                fields=fields,
                conn=db,
            )
            if not wrote:
                _version_conflict(expected_config_version, current)
            return await _finish_agent(
                automation_id,
                conn=db,
                company=company,
                principal=principal,
            )


async def restore_automation(
    automation_id: str,
    *,
    actor_user_id: str,
    expected_config_version: int,
    owner_user_id: str | None,
    company: bool,
    prelocked_pref_row: Mapping[str, Any] | None = None,
    conn: Any = None,
    principal: Principal | None = None,
) -> AutomationReadResponse | None:
    """Restore one soft-deleted automation. None when the owner filter misses."""
    async with get_db_connection(conn) as db:
        async with db.transaction():
            pref_owner = owner_user_id or COMPANY_SYSTEM_USER_ID
            prefs = await _prepare_prefs(
                pref_owner,
                company=company,
                prelocked_pref_row=prelocked_pref_row,
                conn=db,
            )
            row = await recycle_db.lock_automation_for_recycle(
                automation_id,
                owner_user_id=owner_user_id,
                conn=db,
            )
            if row is None:
                return None
            if company:
                _assert_company_recycle_row(row)
            if row.get("deleted_at") is None:
                raise HTTPException(
                    status_code=409,
                    detail="Automation is not in the recycle bin",
                )
            current = int(row.get("config_version") or 1)
            if expected_config_version != current:
                _version_conflict(expected_config_version, current)
            desired = str(row.get("pre_delete_desired_status") or "paused")
            if desired not in ("active", "paused"):
                desired = "paused"
            trigger = str(row.get("trigger_type") or "cron")
            once_iso = await _resolve_effective_once_run_at(row, conn=db)
            once_when = _parse_when(once_iso) if trigger == "once" else None
            status, next_run = await _effective_for_row(
                row,
                desired=desired,
                trigger_type=trigger,
                cron_expression=row.get("cron_expression"),
                timezone_name=row.get("timezone"),
                once_when=once_when,
                conn=db,
            )
            file_name = await _free_restored_name(row, conn=db)
            post = dict(row)
            post["status"] = status
            post["next_run_at"] = next_run
            post["file_name"] = file_name
            history, new_version = await _append_agent_history(
                row,
                action="restore",
                actor_user_id=actor_user_id,
                post_row=post,
                post_desired=desired,
                post_once=once_iso,
                post_notify=None,
                pref_row=prefs,
                restored_from_version=None,
                conn=db,
            )
            fields = _base_fields(post)
            fields.update(
                {
                    "status": status,
                    "next_run_at": next_run,
                    "file_name": file_name,
                    "deleted_by": None,
                    "pre_delete_desired_status": None,
                    "set_deleted_at": False,
                    "config_version": new_version,
                    "active_version": new_version,
                    "updated_by": actor_user_id,
                    "audit_history": history,
                }
            )
            try:
                wrote = await recycle_db.update_automation_recycle(
                    automation_id,
                    expected_config_version=current,
                    fields=fields,
                    conn=db,
                )
            except UniqueViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Could not choose a free file name for the restored automation",
                ) from exc
            if not wrote:
                _version_conflict(expected_config_version, current)
            return await _finish_agent(
                automation_id,
                conn=db,
                company=company,
                principal=principal,
            )


async def _free_restored_name(row: Mapping[str, Any], *, conn: Any) -> str:
    current_name = str(row.get("file_name") or "automation.json")
    owner = str(row.get("user_id") or "")
    automation_id = str(row["automation_id"])
    if not await recycle_db.live_file_name_taken(
        owner,
        current_name,
        exclude_automation_id=automation_id,
        conn=conn,
    ):
        return current_name
    for _ in range(8):
        candidate = restored_file_name(current_name, uuid4().hex[:8])
        if not await recycle_db.live_file_name_taken(
            owner,
            candidate,
            exclude_automation_id=automation_id,
            conn=conn,
        ):
            return candidate
    raise HTTPException(
        status_code=409,
        detail="Could not choose a free file name for the restored automation",
    )


async def _write_legacy_rollback_notify(
    row: Mapping[str, Any],
    *,
    notify_enabled: bool,
    notify_routes: list[str],
    pref_row: Mapping[str, Any],
    conn: Any,
) -> tuple[dict[str, Any], str | None]:
    """Write a rollback's notify config into the owner's preference item.

    ``task_row`` storage does not call this. The route key is the same one
    dispatch reads. The returned preference row includes the write, so the
    snapshot can be re-read from it in this transaction. The user id is set
    when a row was written, so the caller can drop that cache after commit.
    """
    from src.dalton.llm.model_prefs import (
        DALTON_NOTIFY_OVERRIDES_KEY,
        DALTON_NOTIFY_ROUTES_KEY,
    )
    from src.dalton.notify.dispatch import _notify_route_keys
    from src.server.database.user import upsert_user_preferences

    updated = dict(pref_row)
    keys = _notify_route_keys(row)
    if not keys:
        return updated, None
    route_key = keys[0]
    user_id = str(row.get("user_id") or "")
    await upsert_user_preferences(
        user_id=user_id,
        model_preference={
            DALTON_NOTIFY_ROUTES_KEY: {route_key: list(notify_routes)},
            DALTON_NOTIFY_OVERRIDES_KEY: {route_key: bool(notify_enabled)},
        },
        conn=conn,
    )
    model = dict(updated.get("model_preference") or {})
    routes_map = dict(model.get(DALTON_NOTIFY_ROUTES_KEY) or {})
    overrides = dict(model.get(DALTON_NOTIFY_OVERRIDES_KEY) or {})
    routes_map[route_key] = list(notify_routes)
    overrides[route_key] = bool(notify_enabled)
    model[DALTON_NOTIFY_ROUTES_KEY] = routes_map
    model[DALTON_NOTIFY_OVERRIDES_KEY] = overrides
    updated["model_preference"] = model
    return updated, user_id.strip() or None


async def rollback_automation(
    automation_id: str,
    *,
    actor_user_id: str,
    expected_config_version: int,
    target_version: int,
    owner_user_id: str | None,
    company: bool,
    prelocked_pref_row: Mapping[str, Any] | None = None,
    conn: Any = None,
    principal: Principal | None = None,
) -> AutomationReadResponse | None:
    """Roll one automation back to the active entry's immediate previous version."""
    cache_user_id: str | None = None
    async with get_db_connection(conn) as db:
        async with db.transaction():
            pref_owner = owner_user_id or COMPANY_SYSTEM_USER_ID
            prefs = await _prepare_prefs(
                pref_owner,
                company=company,
                prelocked_pref_row=prelocked_pref_row,
                conn=db,
            )
            row = await recycle_db.lock_automation_for_recycle(
                automation_id,
                owner_user_id=owner_user_id,
                conn=db,
            )
            if row is None:
                return None
            if company:
                _assert_company_recycle_row(row)
            if row.get("deleted_at") is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Restore the automation before rolling it back",
                )
            current = int(row.get("config_version") or 1)
            if expected_config_version != current:
                _version_conflict(expected_config_version, current)
            validate_existing_history_state(row)
            history = _history_list(row.get("audit_history"))
            active = _find_entry(history, int(row.get("active_version") or 1))
            previous = None if active is None else active.get("prev_version")
            if active is None or not isinstance(previous, int) or isinstance(previous, bool):
                raise HTTPException(
                    status_code=400,
                    detail="target_version must be the previous active version",
                )
            if target_version != previous:
                raise HTTPException(
                    status_code=400,
                    detail="target_version must be the previous active version",
                )
            target = _find_entry(history, target_version)
            raw_snapshot = target.get("snapshot") if target else None
            if not isinstance(raw_snapshot, Mapping):
                raise HTTPException(
                    status_code=409,
                    detail="Rollback target snapshot is missing",
                )
            snapshot = _require_complete_snapshot(raw_snapshot)
            if str(row.get("scope") or "") == "company":
                snapshot["delivery_config"] = {}
            trigger = str(snapshot["trigger_type"])
            once_iso = (
                snapshot.get("once_run_at") if trigger == "once" else None
            )
            if once_iso is not None:
                once_iso = str(once_iso)
            once_when = _parse_when(once_iso) if trigger == "once" else None
            desired = str(snapshot["desired_status"])
            preview = dict(row)
            preview.update({name: snapshot.get(name) for name in _SNAPSHOT_COLUMNS})
            status, next_run = await _effective_for_row(
                row,
                desired=desired,
                trigger_type=trigger,
                cron_expression=snapshot.get("cron_expression"),
                timezone_name=snapshot.get("timezone"),
                once_when=once_when,
                conn=db,
                keep_paused_once_next=False,
            )
            if status not in ("active", "executing"):
                await recycle_db.lock_unfinished_executions(automation_id, conn=db)
                await recycle_db.skip_unstarted_executions(
                    automation_id,
                    skip_reason="paused",
                    conn=db,
                )
            post_notify = (
                bool(snapshot.get("notify_enabled")),
                [
                    str(item)
                    for item in snapshot.get("notify_routes") or []
                    if isinstance(item, str)
                ],
            )
            target_prev = target.get("prev_version") if target else None
            if isinstance(target_prev, bool) or (
                target_prev is not None and not isinstance(target_prev, int)
            ):
                target_prev = None
            new_history, new_version = await _append_agent_history(
                row,
                action="rollback",
                actor_user_id=actor_user_id,
                post_row=preview,
                post_desired=desired,
                post_once=once_iso,
                post_notify=post_notify,
                pref_row=prefs,
                restored_from_version=target_version,
                prev_version=target_prev,
                conn=db,
            )
            # History is appended from the prefs as they were, so the previous
            # snapshot still shows the old notify config. Then the live store
            # is restored and the new snapshot is re-read from that store.
            mode = str(row.get("notify_storage_mode") or "legacy").strip() or "legacy"
            finish_prefs: Mapping[str, Any] | None = None
            if mode == "task_row":
                effective_notify = post_notify
            else:
                prefs, written_user_id = await _write_legacy_rollback_notify(
                    preview,
                    notify_enabled=post_notify[0],
                    notify_routes=list(post_notify[1]),
                    pref_row=prefs,
                    conn=db,
                )
                cache_user_id = written_user_id
                finish_prefs = prefs
                effective_notify = await resolve_snapshot_notify_fields(
                    {**preview, "notify_storage_mode": "legacy"},
                    pref_row=prefs,
                    conn=db,
                )
            # Keep the target snapshot's desired_status. Overlay the notify
            # values that are now in effect, so the snapshot matches the response.
            stored_snapshot = dict(snapshot)
            stored_snapshot["notify_enabled"] = bool(effective_notify[0])
            stored_snapshot["notify_routes"] = [str(item) for item in effective_notify[1]]
            if new_history:
                new_history[-1]["snapshot"] = stored_snapshot
                pre_snapshot = new_history[-2]["snapshot"] if len(new_history) >= 2 else {}
                if isinstance(pre_snapshot, Mapping):
                    new_history[-1]["changes"] = compute_snapshot_changes(
                        pre_snapshot,
                        stored_snapshot,
                    )
            fields = _base_fields(row)
            fields.update(
                {
                    "name": str(snapshot.get("name") or ""),
                    "description": snapshot.get("description"),
                    "trigger_type": trigger,
                    "cron_expression": snapshot.get("cron_expression"),
                    "timezone": str(snapshot.get("timezone") or "UTC"),
                    "instruction": str(snapshot.get("instruction") or ""),
                    "workspace_id": _uuid_or_none(
                        snapshot.get("workspace_id"),
                        "workspace_id",
                    ),
                    "llm_model": snapshot.get("llm_model"),
                    "additional_context": (
                        snapshot.get("additional_context")
                        if isinstance(snapshot.get("additional_context"), (dict, list))
                        else None
                    ),
                    "thread_strategy": str(snapshot.get("thread_strategy") or "new"),
                    "conversation_thread_id": _uuid_or_none(
                        snapshot.get("conversation_thread_id"),
                        "conversation_thread_id",
                    ),
                    "max_failures": int(snapshot.get("max_failures") or 3),
                    "trigger_config": dict(snapshot.get("trigger_config") or {}),
                    "delivery_config": dict(snapshot.get("delivery_config") or {}),
                    "notify_enabled": bool(effective_notify[0]),
                    "notify_routes": [str(item) for item in effective_notify[1]],
                    "status": status,
                    "next_run_at": next_run,
                    "deleted_by": row.get("deleted_by"),
                    "pre_delete_desired_status": row.get("pre_delete_desired_status"),
                    "set_deleted_at": False,
                    "config_version": new_version,
                    "active_version": new_version,
                    "updated_by": actor_user_id,
                    "audit_history": new_history,
                }
            )
            wrote = await recycle_db.update_automation_recycle(
                automation_id,
                expected_config_version=current,
                fields=fields,
                conn=db,
            )
            if not wrote:
                _version_conflict(expected_config_version, current)
            response = await _finish_agent(
                automation_id,
                conn=db,
                company=company,
                principal=principal,
                pref_row=finish_prefs,
            )
        # The transaction has committed. A caller-owned connection has not,
        # and clearing the cache before that commit reloads the old row.
        if cache_user_id and conn is None:
            from src.server.database.user import invalidate_user_prefs_cache

            await invalidate_user_prefs_cache(cache_user_id)
        return response


async def soft_delete_owned_automation(
    automation_id: str,
    user_id: str,
    *,
    actor_user_id: str | None = None,
    expected_config_version: int | None = None,
    prelocked_pref_row: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    """Owner-scoped soft delete used by LiveFS and ``delete_automation``.

    ``expected_config_version is None`` checks the row against itself so a file
    delete that already holds the row cannot conflict with its own version.
    """
    try:
        result = await soft_delete_automation(
            automation_id,
            actor_user_id=actor_user_id or user_id,
            expected_config_version=expected_config_version,
            owner_user_id=user_id,
            company=False,
            prelocked_pref_row=prelocked_pref_row,
            conn=conn,
        )
    except HTTPException as exc:
        if exc.status_code == 409 and "already in the recycle bin" in str(exc.detail):
            return False
        raise
    return result is not None


def _script_seed(
    row: Mapping[str, Any],
    history: list[dict[str, Any]],
    *,
    enabled: bool,
    paused_by_user: bool,
    changed_at: str,
) -> list[dict[str, Any]]:
    if history:
        return history
    return [
        {
            "version": 1,
            "prev_version": None,
            "restored_from_version": None,
            "actor_user_id": row.get("updated_by"),
            "changed_at": changed_at,
            "action": "seed",
            "changes": {},
            "snapshot": _script_snapshot(
                row,
                enabled=enabled,
                paused_by_user=paused_by_user,
                cron_expr=str(row.get("cron_expr") or ""),
                timezone=str(row.get("timezone") or "UTC"),
            ),
        }
    ]


async def soft_delete_data_task(
    job_key: str,
    *,
    actor_user_id: str,
    expected_config_version: int,
    conn: Any = None,
) -> None:
    """Soft-delete one manifest data task and cancel its queued runs."""
    _require_manifest_job(job_key)
    async with get_db_connection(conn) as db:
        async with db.transaction():
            row = await recycle_db.lock_script_for_recycle(job_key, conn=db)
            if row is None:
                raise CompanyDataTaskReadError(
                    f"Company data task definition is missing: {job_key}"
                )
            if row.get("deleted_at") is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Automation is already in the recycle bin",
                )
            _assert_migration_writable(row.get("migration_state"))
            history = _script_history_or_refuse(row)
            current = int(row["config_version"])
            if current != expected_config_version:
                _script_conflict(expected_config_version, current)
            old_enabled = bool(row.get("enabled"))
            old_paused = bool(row.get("paused_by_user"))
            changed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            original = list(history)
            history = _script_seed(
                row,
                history,
                enabled=old_enabled,
                paused_by_user=old_paused,
                changed_at=changed_at,
            )
            if not original:
                new_version = 2
                prev_version = 1
            else:
                new_version = current + 1
                prev_version = int(row["active_version"])
            new_state = {
                "enabled": False,
                "paused_by_user": True,
                "cron_expr": str(row.get("cron_expr") or "").strip(),
                "timezone": str(row.get("timezone") or "").strip(),
            }
            old_state = {
                "enabled": old_enabled,
                "paused_by_user": old_paused,
                "cron_expr": new_state["cron_expr"],
                "timezone": new_state["timezone"],
            }
            history.append(
                {
                    "version": new_version,
                    "prev_version": prev_version,
                    "restored_from_version": None,
                    "actor_user_id": actor_user_id,
                    "changed_at": changed_at,
                    "action": "delete",
                    "changes": _field_changes(old_state, new_state),
                    "snapshot": _script_snapshot(
                        row,
                        enabled=False,
                        paused_by_user=True,
                        cron_expr=new_state["cron_expr"],
                        timezone=new_state["timezone"],
                    ),
                }
            )
            wrote = await recycle_db.update_script_recycle(
                job_key,
                expected_config_version=current,
                enabled=False,
                paused_by_user=True,
                deleted_by=actor_user_id,
                pre_delete_enabled=old_enabled,
                set_deleted_at=True,
                config_version=new_version,
                active_version=new_version,
                updated_by=actor_user_id,
                audit_history=history,
                conn=db,
            )
            if not wrote:
                _script_conflict(expected_config_version, current)
            await company_data_db.cancel_queued_script_runs(
                job_key,
                failure_reason="task_deleted",
                conn=db,
            )


async def restore_data_task(
    job_key: str,
    *,
    actor_user_id: str,
    expected_config_version: int,
    conn: Any = None,
) -> None:
    """Restore a soft-deleted data task. Cancelled runs stay cancelled."""
    _require_manifest_job(job_key)
    async with get_db_connection(conn) as db:
        async with db.transaction():
            row = await recycle_db.lock_script_for_recycle(job_key, conn=db)
            if row is None:
                raise CompanyDataTaskReadError(
                    f"Company data task definition is missing: {job_key}"
                )
            if row.get("deleted_at") is None:
                raise HTTPException(
                    status_code=409,
                    detail="Automation is not in the recycle bin",
                )
            _assert_migration_writable(row.get("migration_state"))
            original = _script_history_or_refuse(row)
            current = int(row["config_version"])
            if current != expected_config_version:
                _script_conflict(expected_config_version, current)
            stored_enabled = row.get("pre_delete_enabled")
            enabled = bool(stored_enabled) if stored_enabled is not None else False
            paused = not enabled
            changed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            history = _script_seed(
                row,
                list(original),
                enabled=bool(row.get("enabled")),
                paused_by_user=bool(row.get("paused_by_user")),
                changed_at=changed_at,
            )
            if not original:
                new_version = 2
                prev_version = 1
            else:
                new_version = current + 1
                prev_version = int(row["active_version"])
            new_state = {
                "enabled": enabled,
                "paused_by_user": paused,
                "cron_expr": str(row.get("cron_expr") or "").strip(),
                "timezone": str(row.get("timezone") or "").strip(),
            }
            old_state = {
                "enabled": bool(row.get("enabled")),
                "paused_by_user": bool(row.get("paused_by_user")),
                "cron_expr": new_state["cron_expr"],
                "timezone": new_state["timezone"],
            }
            history.append(
                {
                    "version": new_version,
                    "prev_version": prev_version,
                    "restored_from_version": None,
                    "actor_user_id": actor_user_id,
                    "changed_at": changed_at,
                    "action": "restore",
                    "changes": _field_changes(old_state, new_state),
                    "snapshot": _script_snapshot(
                        row,
                        enabled=enabled,
                        paused_by_user=paused,
                        cron_expr=new_state["cron_expr"],
                        timezone=new_state["timezone"],
                    ),
                }
            )
            wrote = await recycle_db.update_script_recycle(
                job_key,
                expected_config_version=current,
                enabled=enabled,
                paused_by_user=paused,
                deleted_by=None,
                pre_delete_enabled=row.get("pre_delete_enabled"),
                set_deleted_at=False,
                config_version=new_version,
                active_version=new_version,
                updated_by=actor_user_id,
                audit_history=history,
                conn=db,
            )
            if not wrote:
                _script_conflict(expected_config_version, current)


async def _agent_mode(principal: Principal, automation_id: str) -> str:
    """Return ``owner`` or ``company``. Missing and foreign rows are 404."""
    if is_company_system_user(principal.user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    identity = await company_data_db.get_automation_owner_identity(automation_id)
    if not is_shared_company_agent(identity):
        if identity is None or str(identity.get("user_id") or "") != principal.user_id:
            raise_not_found("Automation")
        return "owner"
    await require_company_scope_read_access(principal)
    return "company"


def _parse_task_id(automation_id: str):
    """Parse a public id. An unusable id is 422 before any write."""
    try:
        return parse_automation_read_id(automation_id)
    except InvalidAutomationId as exc:
        raise HTTPException(status_code=422, detail="Invalid automation id") from exc


async def delete_task(
    principal: Principal,
    automation_id: str,
    expected_config_version: int,
) -> AutomationReadResponse:
    """Soft-delete an agent UUID or a ``data:<job_key>``."""
    parsed = _parse_task_id(automation_id)
    if parsed.kind == "script":
        actor = await _authorize_script_writer(principal)
        await soft_delete_data_task(
            str(parsed.job_key),
            actor_user_id=actor,
            expected_config_version=expected_config_version,
        )
        return await get_script_automation(
            principal,
            str(parsed.job_key),
            allow_deleted=True,
        )
    mode = await _agent_mode(principal, str(parsed.agent_id))
    company = mode == "company"
    result = await soft_delete_automation(
        str(parsed.agent_id),
        actor_user_id=principal.user_id,
        expected_config_version=expected_config_version,
        owner_user_id=None if company else principal.user_id,
        company=company,
        principal=principal,
    )
    if result is None:
        raise_not_found("Automation")
    return result


async def restore_task(
    principal: Principal,
    automation_id: str,
    expected_config_version: int,
) -> AutomationReadResponse:
    """Restore an agent UUID or a ``data:<job_key>`` from the recycle bin."""
    parsed = _parse_task_id(automation_id)
    if parsed.kind == "script":
        actor = await _authorize_script_writer(principal)
        await restore_data_task(
            str(parsed.job_key),
            actor_user_id=actor,
            expected_config_version=expected_config_version,
        )
        return await get_script_automation(principal, str(parsed.job_key))
    mode = await _agent_mode(principal, str(parsed.agent_id))
    company = mode == "company"
    result = await restore_automation(
        str(parsed.agent_id),
        actor_user_id=principal.user_id,
        expected_config_version=expected_config_version,
        owner_user_id=None if company else principal.user_id,
        company=company,
        principal=principal,
    )
    if result is None:
        raise_not_found("Automation")
    return result


async def rollback_task(
    principal: Principal,
    automation_id: str,
    *,
    expected_config_version: int,
    target_version: int,
) -> AutomationReadResponse:
    """Roll back one step. Data tasks reuse ``rollback_data_task_config``."""
    parsed = _parse_task_id(automation_id)
    if parsed.kind == "script":
        actor = await _authorize_script_writer(principal)
        from src.dalton.autonomy.system_jobs import rollback_data_task_config

        await rollback_data_task_config(
            actor,
            str(parsed.job_key),
            expected_config_version=expected_config_version,
            target_version=target_version,
        )
        return await get_script_automation(principal, str(parsed.job_key))
    mode = await _agent_mode(principal, str(parsed.agent_id))
    company = mode == "company"
    result = await rollback_automation(
        str(parsed.agent_id),
        actor_user_id=principal.user_id,
        expected_config_version=expected_config_version,
        target_version=target_version,
        owner_user_id=None if company else principal.user_id,
        company=company,
        principal=principal,
    )
    if result is None:
        raise_not_found("Automation")
    return result


async def purge_expired_automations_if_due(
    now: datetime,
    *,
    force: bool = False,
) -> int:
    """Run the 30-day purge during the 03:00 UTC window, or when forced."""
    if not force and not (now.hour == 3 and now.minute < 2):
        return 0
    return await recycle_db.purge_expired_automations()


async def retry_claimed_cron_next_run(automation_id: str) -> bool:
    """Write ``next_run_at`` once more when the claim's version no longer matches.

    A paused, deleted, or non-cron row is left alone. Migration freeze is not
    added here; existing freeze checks stay where they already are.
    """
    from src.server.database.automation import update_automation_next_run
    from src.server.services.automation_scheduler import AutomationScheduler

    row = await recycle_db.fetch_schedule_guard(automation_id)
    if row is None or row.get("deleted_at") is not None:
        return False
    if str(row.get("trigger_type") or "") != "cron":
        return False
    if str(row.get("status") or "") != "active":
        return False
    if row.get("next_run_at") is not None:
        return False
    cron_expression = row.get("cron_expression")
    if not cron_expression:
        return False
    next_run = AutomationScheduler.calculate_first_run(
        str(cron_expression),
        str(row.get("timezone") or "UTC"),
    )
    return await update_automation_next_run(
        automation_id,
        next_run,
        config_version=int(row["config_version"]),
    )
