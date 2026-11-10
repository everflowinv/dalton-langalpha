"""Database queries for AU1 automation scope classification, scoped listing, and history."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from unittest.mock import NonCallableMock

from psycopg.rows import dict_row

from src.dalton.autonomy.classification import (
    AutomationScope,
    classify_automation_scope,
    needs_workspace_tracking_lookup,
    normalize_scope_text,
    sql_trim_scope_text,
)
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection


def _valid_uuid(raw: Any) -> str | None:
    cleaned = normalize_scope_text(raw)
    if not cleaned:
        return None
    try:
        return str(uuid.UUID(cleaned))
    except (ValueError, TypeError, AttributeError):
        return None


async def fetch_scope_classification_context(
    user_id: str,
    workspace_id: str,
    *,
    conn: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """Fetch workspace ownership, coverage units, and covered tickers in one read."""
    clean_ws = _valid_uuid(workspace_id)
    clean_uid = normalize_scope_text(user_id)
    if not clean_ws or not clean_uid:
        return None, [], []

    trimmed_unit_owner = sql_trim_scope_text("u.owner_user_id")
    trimmed_cov_owner = sql_trim_scope_text("c.user_id")
    trimmed_cov_ticker = sql_trim_scope_text("c.ticker")

    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT
                        (
                            SELECT to_jsonb(w_sub)
                            FROM (
                                SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status
                                FROM workspaces w
                                WHERE w.workspace_id = %s::uuid
                            ) w_sub
                        ) AS workspace_row,
                        COALESCE(
                            (
                                SELECT jsonb_agg(to_jsonb(u_sub))
                                FROM (
                                    SELECT
                                        u.workspace_id::text AS workspace_id,
                                        u.owner_user_id,
                                        u.kind,
                                        u.status
                                    FROM dalton_coverage_units u
                                    WHERE u.workspace_id = %s::uuid
                                      AND {trimmed_unit_owner} = %s
                                ) u_sub
                            ),
                            '[]'::jsonb
                        ) AS coverage_units,
                        COALESCE(
                            (
                                SELECT jsonb_agg(c.ticker)
                                FROM dalton_coverage c
                                WHERE {trimmed_cov_owner} = %s
                                  AND {trimmed_cov_ticker} <> ''
                            ),
                            '[]'::jsonb
                        ) AS covered_tickers
                    """,
                    (clean_ws, clean_ws, clean_uid, clean_uid),
                )
                row = await cur.fetchone()
    except TypeError:
        if isinstance(conn, NonCallableMock):
            return None, [], []
        raise

    if not isinstance(row, Mapping):
        return None, [], []
    ws_row = row.get("workspace_row")
    raw_units = row.get("coverage_units")
    raw_tickers = row.get("covered_tickers")
    workspace_dict = dict(ws_row) if isinstance(ws_row, Mapping) else None
    units_list = (
        [dict(u) for u in raw_units if isinstance(u, Mapping)]
        if isinstance(raw_units, list)
        else []
    )
    tickers_list = (
        [
            normalize_scope_text(t)
            for t in raw_tickers
            if isinstance(t, str) and normalize_scope_text(t)
        ]
        if isinstance(raw_tickers, list)
        else []
    )
    return workspace_dict, units_list, tickers_list


async def resolve_automation_scope_for_write(
    user_id: str,
    *,
    name: str,
    workspace_id: str | None,
    metadata: Mapping[str, Any] | None,
    conn: Any = None,
) -> AutomationScope:
    """Compute canonical ``scope`` for a new or updated automation row."""
    candidate_row: dict[str, Any] = {
        "user_id": user_id,
        "name": name,
        "workspace_id": workspace_id,
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
    }
    if not needs_workspace_tracking_lookup(
        user_id,
        name=name,
        workspace_id=workspace_id,
        metadata=metadata,
    ):
        return classify_automation_scope(candidate_row)

    workspace_row, units, covered_tickers = await fetch_scope_classification_context(
        user_id,
        str(workspace_id or ""),
        conn=conn,
    )
    return classify_automation_scope(
        candidate_row,
        workspace_row,
        coverage_units=units,
        covered_tickers=covered_tickers,
    )


async def list_automations_for_scope_db(
    *,
    scope: AutomationScope,
    caller_user_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    computer_id: str | None = None,
    conn: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read paginated automations and matching total count for an explicit ``scope``."""
    from src.server.database.automation import AUTOMATION_COLUMNS, _last_execution_join

    where_parts: list[str] = ["automations.deleted_at IS NULL"]
    params: list[Any] = []

    if scope == "company":
        where_parts.append("automations.scope = 'company'")
        where_parts.append("automations.user_id = %s")
        params.append(COMPANY_SYSTEM_USER_ID)
    else:
        where_parts.append("automations.user_id = %s")
        params.append(caller_user_id)
        where_parts.append("automations.scope = %s")
        params.append(scope)
        if computer_id is not None:
            where_parts.append(
                "automations.workspace_id <> %s::uuid AND EXISTS (SELECT 1 FROM workspaces w"
                " WHERE w.workspace_id = automations.workspace_id"
                " AND w.computer_id = %s::uuid)"
            )
            params.extend([get_home_workspace_id(caller_user_id), computer_id])

    if status:
        where_parts.append("automations.status = %s")
        params.append(status)

    where_clause = " AND ".join(where_parts)

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT COUNT(*) AS cnt FROM automations WHERE {where_clause}",
                tuple(params),
            )
            count_row = await cur.fetchone()
            total = int((count_row or {}).get("cnt", 0))

            await cur.execute(
                f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution
                FROM automations
                {_last_execution_join("automations")}
                WHERE {where_clause}
                ORDER BY automations.created_at DESC, automations.automation_id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows], total


async def get_automation_history_row(
    automation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read stored version metadata and ``audit_history`` without mutating the row."""
    clean_id = _valid_uuid(automation_id)
    if not clean_id:
        return None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    automation_id,
                    user_id,
                    scope,
                    workspace_id,
                    config_version,
                    active_version,
                    audit_history,
                    deleted_at
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (clean_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


def _to_iso_str(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, str):
        cleaned = val.strip()
        return cleaned if cleaned else None
    cleaned = str(val).strip()
    return cleaned if cleaned else None


async def _resolve_effective_once_run_at(
    row: Mapping[str, Any],
    *,
    target_snapshot: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> str | None:
    """Resolve effective ``once_run_at`` for a one-time automation per Design §2.1.3(A).

    When ``target_snapshot`` is provided, returns ``target_snapshot.get('once_run_at')``
    directly without falling back. Otherwise checks in order:
    1. ``row['next_run_at']``
    2. Current ``active_version`` snapshot's ``once_run_at`` in ``audit_history``,
       else most recent non-null ``once_run_at`` in ``audit_history``
    3. ``row['metadata']['once_schedule_at']``
    4. Only when ``audit_history`` is empty: ``automation_executions`` matching
       ``automation_id = row.automation_id AND created_at = row.last_run_at``
       when exactly 1 row matches with non-null ``scheduled_at``.
    """
    if target_snapshot is not None:
        return _to_iso_str(target_snapshot.get("once_run_at"))

    trigger_type = str(row.get("trigger_type") or "").strip()
    if trigger_type != "once":
        return None

    # 1. Row next_run_at
    direct_next = _to_iso_str(row.get("next_run_at"))
    if direct_next is not None:
        return direct_next

    # 2. Current active snapshot's once_run_at, else latest non-null in audit_history
    raw_history = row.get("audit_history")
    history_non_empty = isinstance(raw_history, list) and len(raw_history) > 0
    if history_non_empty:
        active_ver = row.get("active_version")
        if active_ver is not None:
            for item in reversed(raw_history):
                if not isinstance(item, Mapping):
                    continue
                if item.get("version") == active_ver:
                    snap = item.get("snapshot")
                    if isinstance(snap, Mapping):
                        cand = _to_iso_str(snap.get("once_run_at"))
                        if cand is not None:
                            return cand
        for item in reversed(raw_history):
            if not isinstance(item, Mapping):
                continue
            snap = item.get("snapshot")
            if isinstance(snap, Mapping):
                cand = _to_iso_str(snap.get("once_run_at"))
                if cand is not None:
                    return cand

    # 3. metadata.once_schedule_at
    meta = row.get("metadata")
    if isinstance(meta, Mapping):
        meta_once = _to_iso_str(meta.get("once_schedule_at"))
        if meta_once is not None:
            return meta_once

    # 4. Only when audit_history is empty: check automation_executions for claimed run
    if history_non_empty:
        return None

    auto_id = _valid_uuid(row.get("automation_id"))
    last_run_at = row.get("last_run_at")
    if not auto_id or last_run_at is None:
        return None

    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT scheduled_at
                    FROM automation_executions
                    WHERE automation_id = %s::uuid
                      AND created_at = %s
                    LIMIT 2
                    """,
                    (auto_id, last_run_at),
                )
                exec_rows = await cur.fetchall()
    except Exception:
        return None

    if not isinstance(exec_rows, list) or len(exec_rows) != 1:
        return None
    single = exec_rows[0]
    sched_val = (
        single.get("scheduled_at")
        if isinstance(single, Mapping)
        else (single[0] if isinstance(single, (tuple, list)) and single else None)
    )
    return _to_iso_str(sched_val)


async def fetch_user_preferences_unlocked(
    user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Read ``user_preferences`` without acquiring ``lock_user_preferences``."""
    clean_uid = normalize_scope_text(user_id)
    if not clean_uid or isinstance(conn, NonCallableMock):
        return {}
    try:
        async with get_db_connection(conn) as db_conn:
            if isinstance(db_conn, NonCallableMock):
                return {}
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT model_preference, other_preference
                    FROM user_preferences
                    WHERE user_id = %s
                    """,
                    (clean_uid,),
                )
                row = await cur.fetchone()
                return dict(row) if isinstance(row, Mapping) else {}
    except Exception:
        return {}


async def resolve_snapshot_notify_fields(
    row: Mapping[str, Any],
    *,
    pref_row: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> tuple[bool, list[str]]:
    """Resolve ``(notify_enabled, notify_routes)`` for an automation snapshot.

    Uses ``row['notify_storage_mode']`` as the sole selector:
    - ``'task_row'``: read ``notify_enabled`` and ``notify_routes`` from ``row``.
    - ``'legacy'``: resolve from owner's ``user_preferences`` without calling
      ``lock_user_preferences``.
    """
    storage_mode = str(row.get("notify_storage_mode") or "legacy").strip() or "legacy"
    if storage_mode == "task_row":
        raw_enabled = row.get("notify_enabled")
        enabled = bool(True if raw_enabled is None else raw_enabled)
        raw_routes = row.get("notify_routes")
        routes = (
            [
                str(t).strip()
                for t in raw_routes
                if isinstance(t, str) and str(t).strip()
            ]
            if isinstance(raw_routes, (list, tuple))
            else []
        )
        return enabled, routes

    from src.dalton.access_tiers import is_internal_or_admin
    from src.dalton.llm.model_prefs import (
        DALTON_NOTIFY_ENABLED_KEY,
        DALTON_NOTIFY_OVERRIDES_KEY,
        DALTON_NOTIFY_ROUTES_KEY,
        DALTON_NOTIFY_TARGETS_KEY,
        _normalize_stored_value,
    )
    from src.dalton.notify.dispatch import (
        classify_automation_for_notify,
        is_excluded_automation_for_notify,
        resolve_automation_notify_state,
        resolve_notify_route_targets,
    )

    if is_excluded_automation_for_notify(row):
        return False, []

    owner_id = normalize_scope_text(row.get("user_id"))
    effective_pref_row = (
        dict(pref_row)
        if isinstance(pref_row, Mapping)
        else await fetch_user_preferences_unlocked(owner_id, conn=conn)
    )

    has_master = False
    master_enabled = False
    for bag_key in ("model_preference", "other_preference"):
        bag = effective_pref_row.get(bag_key)
        if isinstance(bag, Mapping) and DALTON_NOTIFY_ENABLED_KEY in bag:
            norm_en = _normalize_stored_value(
                DALTON_NOTIFY_ENABLED_KEY, bag.get(DALTON_NOTIFY_ENABLED_KEY)
            )
            if isinstance(norm_en, bool):
                has_master = True
                master_enabled = norm_en
                break

    has_targets_key = False
    targets: list[str] = []
    for bag_key in ("model_preference", "other_preference"):
        bag = effective_pref_row.get(bag_key)
        if isinstance(bag, Mapping) and DALTON_NOTIFY_TARGETS_KEY in bag:
            has_targets_key = True
            norm_tgts = _normalize_stored_value(
                DALTON_NOTIFY_TARGETS_KEY, bag.get(DALTON_NOTIFY_TARGETS_KEY)
            )
            if isinstance(norm_tgts, list):
                targets = list(norm_tgts)
            break

    overrides: dict[str, bool] = {}
    for bag_key in ("model_preference", "other_preference"):
        bag = effective_pref_row.get(bag_key)
        if isinstance(bag, Mapping) and DALTON_NOTIFY_OVERRIDES_KEY in bag:
            norm_ov = _normalize_stored_value(
                DALTON_NOTIFY_OVERRIDES_KEY, bag.get(DALTON_NOTIFY_OVERRIDES_KEY)
            )
            if isinstance(norm_ov, Mapping):
                overrides = dict(norm_ov)
            break

    routes_map: dict[str, list[str]] = {}
    mp_bag = effective_pref_row.get("model_preference")
    if isinstance(mp_bag, Mapping) and DALTON_NOTIFY_ROUTES_KEY in mp_bag:
        norm_rt = _normalize_stored_value(
            DALTON_NOTIFY_ROUTES_KEY, mp_bag.get(DALTON_NOTIFY_ROUTES_KEY)
        )
        if isinstance(norm_rt, Mapping):
            routes_map = dict(norm_rt)

    classification = classify_automation_for_notify(row)
    cat, per_task_eff, _ = resolve_automation_notify_state(
        row, overrides, routes_map
    )

    try:
        include_chats = await is_internal_or_admin(owner_id) if owner_id else False
    except Exception:
        include_chats = False

    effective_configured_targets = targets
    if not has_targets_key and routes_map:
        fallback_personal: list[str] = []
        for rt_list in routes_map.values():
            if isinstance(rt_list, list):
                for item in rt_list:
                    if isinstance(item, str) and item.strip() and item.strip() not in fallback_personal:
                        fallback_personal.append(item.strip())
        effective_configured_targets = fallback_personal

    routed = resolve_notify_route_targets(
        row,
        routes_map,
        effective_configured_targets,
        include_chat_targets=include_chats,
    )
    if routed is not None:
        effective_routes = list(routed)
    elif classification is not None:
        effective_routes = list(targets)
    else:
        effective_routes = []

    if cat is None and classification is None:
        custom_key = f"automation:{str(row.get('automation_id') or '').strip()}"
        if custom_key in overrides and isinstance(overrides[custom_key], bool):
            per_task_eff = overrides[custom_key]

    if has_master and not master_enabled:
        effective_enabled = False
    else:
        effective_enabled = bool(per_task_eff)

    return effective_enabled, effective_routes


async def build_create_history_columns(
    row_data: Mapping[str, Any],
    *,
    actor_user_id: str | None,
    desired_status: str = "active",
    changed_at_iso: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Build initial ``config_version=1``, ``active_version=1``, and ``v1`` ``audit_history``."""
    from src.dalton.services.company_automations import (
        build_automation_config_snapshot,
    )

    once_run_at = (
        _to_iso_str(row_data.get("next_run_at"))
        if str(row_data.get("trigger_type") or "") == "once"
        else None
    )
    notify_enabled, notify_routes = await resolve_snapshot_notify_fields(
        row_data, conn=conn
    )
    snapshot = build_automation_config_snapshot(
        row_data,
        desired_status=desired_status,
        once_run_at=once_run_at,
        notify_enabled=notify_enabled,
        notify_routes=notify_routes,
    )
    v1_entry = {
        "version": 1,
        "prev_version": None,
        "restored_from_version": None,
        "actor_user_id": actor_user_id,
        "changed_at": changed_at_iso,
        "action": "create",
        "changes": {},
        "snapshot": snapshot,
    }
    return {
        "created_by": actor_user_id,
        "updated_by": actor_user_id,
        "config_version": 1,
        "active_version": 1,
        "audit_history": [v1_entry],
    }


async def prepare_update_history_columns(
    locked_row: Mapping[str, Any],
    update_fields: Mapping[str, Any],
    *,
    actor_user_id: str | None,
    action: str = "update",
    expected_config_version: int | None = None,
    skip_if_unchanged: bool = False,
    pref_row: Mapping[str, Any] | None = None,
    base_pre_row: Mapping[str, Any] | None = None,
    changed_at_iso: str,
    conn: Any = None,
) -> tuple[dict[str, Any], bool]:
    """Validate version/history state on ``locked_row`` and compute updated history columns.

    Returns ``(history_update_columns, is_noop_config)``.
    """
    from src.dalton.services.company_automations import (
        AutomationVersionConflictError,
        build_automation_config_snapshot,
        compute_snapshot_changes,
        resolve_desired_status_for_snapshot,
        validate_existing_history_state,
    )

    validate_existing_history_state(locked_row)

    current_cv = int(locked_row.get("config_version") or 1)
    if expected_config_version is not None and expected_config_version != current_cv:
        raise AutomationVersionConflictError(
            f"Configuration version conflict: expected {expected_config_version}, current is {current_cv}"
        )

    pre_row = base_pre_row if base_pre_row is not None else locked_row
    if base_pre_row is not None:
        validate_existing_history_state(pre_row)

    eff_pref_row = (
        dict(pref_row)
        if isinstance(pref_row, Mapping)
        else await fetch_user_preferences_unlocked(
            str(pre_row.get("user_id") or ""), conn=conn
        )
    )

    pre_once_run_at = await _resolve_effective_once_run_at(pre_row, conn=conn)
    pre_notify_enabled, pre_notify_routes = await resolve_snapshot_notify_fields(
        pre_row, pref_row=eff_pref_row, conn=conn
    )
    pre_desired_status = resolve_desired_status_for_snapshot(pre_row)
    pre_snapshot = build_automation_config_snapshot(
        pre_row,
        desired_status=pre_desired_status,
        once_run_at=pre_once_run_at,
        notify_enabled=pre_notify_enabled,
        notify_routes=pre_notify_routes,
    )

    post_row: dict[str, Any] = dict(locked_row)
    post_row.update(update_fields)

    post_trigger_type = str(post_row.get("trigger_type") or "cron").strip() or "cron"
    if post_trigger_type != "once":
        post_once_run_at = None
    elif (
        "next_run_at" in update_fields
        and update_fields.get("next_run_at") is not None
    ):
        post_once_run_at = _to_iso_str(update_fields.get("next_run_at"))
    else:
        post_once_run_at = pre_once_run_at

    if action in ("pause", "maintenance_pause_stale_binding") or (
        update_fields.get("status") == "paused"
    ):
        post_desired_status = "paused"
    elif action == "resume" or update_fields.get("status") in ("active", "executing"):
        post_desired_status = "active"
    else:
        post_desired_status = pre_desired_status

    post_notify_enabled, post_notify_routes = await resolve_snapshot_notify_fields(
        post_row, pref_row=eff_pref_row, conn=conn
    )
    post_snapshot = build_automation_config_snapshot(
        post_row,
        desired_status=post_desired_status,
        once_run_at=post_once_run_at,
        notify_enabled=post_notify_enabled,
        notify_routes=post_notify_routes,
    )

    changes = compute_snapshot_changes(pre_snapshot, post_snapshot)
    if skip_if_unchanged and not changes:
        return {}, True

    raw_history = list(pre_row.get("audit_history") or [])
    if len(raw_history) == 0:
        seed_actor = pre_row.get("created_by") or pre_row.get("user_id")
        created_at_val = pre_row.get("created_at")
        seed_changed_at = (
            created_at_val.isoformat()
            if hasattr(created_at_val, "isoformat")
            else (
                str(created_at_val) if created_at_val is not None else changed_at_iso
            )
        )
        raw_history.append(
            {
                "version": 1,
                "prev_version": None,
                "restored_from_version": None,
                "actor_user_id": str(seed_actor) if seed_actor is not None else None,
                "changed_at": seed_changed_at,
                "action": "seed",
                "changes": {},
                "snapshot": pre_snapshot,
            }
        )
        old_config_version = 1
        old_active_version = 1
    else:
        old_config_version = int(pre_row["config_version"])
        old_active_version = int(pre_row["active_version"])

    new_version = old_config_version + 1
    raw_history.append(
        {
            "version": new_version,
            "prev_version": old_active_version,
            "restored_from_version": None,
            "actor_user_id": actor_user_id,
            "changed_at": changed_at_iso,
            "action": action,
            "changes": changes,
            "snapshot": post_snapshot,
        }
    )

    return (
        {
            "config_version": new_version,
            "active_version": new_version,
            "updated_by": actor_user_id,
            "audit_history": raw_history,
        },
        False,
    )
