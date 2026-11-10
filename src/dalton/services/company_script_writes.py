"""Update, pause, and resume one existing manifest data script.

The caller is a human internal member or admin. The company system identity
cannot be the actor, and the body cannot name a different actor. The write
locks only the definition row, then checks deletion, migration freeze, history,
and ``expected_config_version`` on that locked row. It does not take the admin
preference lock, rewrite notification columns, or insert a definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException

from src.dalton.company_identity import is_company_system_user
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.services.company_automations import require_company_scope_read_access
from src.dalton.services.company_task_read import (
    ALLOWED_SCRIPT_SNAPSHOT_FIELDS,
    CompanyDataTaskReadError,
    get_script_automation,
    load_manifest_job_keys,
)
from src.server.dependencies.principal import Principal
from src.server.models.automation import CompanyScriptUpdate, ScriptAutomationResponse
from src.server.utils.api import raise_not_found

_MIGRATION_FREEZE_STAGES = frozenset({"frozen", "rollback_frozen", "switching"})
_VERSION_FIELDS = ("enabled", "paused_by_user", "cron_expr", "timezone")
_PAUSED_BEFORE_START = "paused_before_start"
_SCHEDULE_CHANGED_BEFORE_START = "schedule_changed_before_start"
_CancelMode = Literal["none", "queued", "queued_cron"]


async def update_company_script(
    principal: Principal,
    job_key: str,
    request: CompanyScriptUpdate,
    *,
    conn: Any = None,
) -> ScriptAutomationResponse:
    """Change cron and timezone on one manifest script. Returns the filtered detail."""
    actor_user_id = await _authorize_script_writer(principal)
    _require_manifest_job(job_key)
    version = _positive_version(request.expected_config_version)
    await _apply_script_change(
        job_key,
        actor_user_id=actor_user_id,
        expected_config_version=version,
        action="update",
        cron_expr=request.cron_expression,
        timezone_name=request.timezone,
        enabled=None,
        paused_by_user=None,
        cancel_mode="queued_cron",
        conn=conn,
    )
    return await get_script_automation(principal, job_key)


async def pause_company_script(
    principal: Principal,
    job_key: str,
    *,
    expected_config_version: int,
    conn: Any = None,
) -> ScriptAutomationResponse:
    """Set ``enabled`` false and ``paused_by_user`` true. Cancel queued runs only."""
    actor_user_id = await _authorize_script_writer(principal)
    _require_manifest_job(job_key)
    version = _positive_version(expected_config_version)
    await _apply_script_change(
        job_key,
        actor_user_id=actor_user_id,
        expected_config_version=version,
        action="pause",
        cron_expr=None,
        timezone_name=None,
        enabled=False,
        paused_by_user=True,
        cancel_mode="queued",
        conn=conn,
    )
    return await get_script_automation(principal, job_key)


async def resume_company_script(
    principal: Principal,
    job_key: str,
    *,
    expected_config_version: int,
    conn: Any = None,
) -> ScriptAutomationResponse:
    """Set ``enabled`` true and ``paused_by_user`` false when no manual trial is active."""
    actor_user_id = await _authorize_script_writer(principal)
    _require_manifest_job(job_key)
    version = _positive_version(expected_config_version)
    await _apply_script_change(
        job_key,
        actor_user_id=actor_user_id,
        expected_config_version=version,
        action="resume",
        cron_expr=None,
        timezone_name=None,
        enabled=True,
        paused_by_user=False,
        cancel_mode="none",
        conn=conn,
    )
    return await get_script_automation(principal, job_key)


async def _authorize_script_writer(principal: Principal) -> str:
    """Internal or admin human. Computer tokens and the company system user are refused."""
    await require_company_scope_read_access(principal)
    if is_company_system_user(principal.user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return str(principal.user_id)


def _require_manifest_job(job_key: str) -> None:
    if job_key not in set(load_manifest_job_keys()):
        raise_not_found("Automation")


def _positive_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(
            status_code=422,
            detail="expected_config_version must be a positive integer",
        )
    return value


async def _apply_script_change(
    job_key: str,
    *,
    actor_user_id: str,
    expected_config_version: int,
    action: str,
    cron_expr: str | None,
    timezone_name: str | None,
    enabled: bool | None,
    paused_by_user: bool | None,
    cancel_mode: _CancelMode,
    conn: Any,
) -> None:
    async with company_data_db.get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            locked = await company_data_db.lock_script_def_for_update(
                job_key, conn=db_conn
            )
            if locked is None:
                raise CompanyDataTaskReadError(
                    f"Company data task definition is missing: {job_key}"
                )
            if locked.get("deleted_at") is not None:
                raise_not_found("Automation")
            _assert_migration_writable(locked.get("migration_state"))
            history = _script_history_or_refuse(locked)
            current_version = int(locked["config_version"])
            if current_version != expected_config_version:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "config_version conflict: "
                        f"expected {expected_config_version}, found {current_version}"
                    ),
                )
            new_enabled = bool(locked["enabled"] if enabled is None else enabled)
            new_paused = bool(
                locked["paused_by_user"] if paused_by_user is None else paused_by_user
            )
            new_cron = str(locked["cron_expr"]).strip() if cron_expr is None else cron_expr
            new_zone = (
                str(locked["timezone"]).strip() if timezone_name is None else timezone_name
            )
            if new_enabled and not bool(locked["enabled"]):
                await _reject_active_manual_trial(db_conn, job_key, locked)
            old_state = {
                "enabled": bool(locked["enabled"]),
                "paused_by_user": bool(locked["paused_by_user"]),
                "cron_expr": str(locked["cron_expr"]).strip(),
                "timezone": str(locked["timezone"]).strip(),
            }
            new_state = {
                "enabled": new_enabled,
                "paused_by_user": new_paused,
                "cron_expr": new_cron,
                "timezone": new_zone,
            }
            changes = _field_changes(old_state, new_state)
            if not changes:
                return
            new_version = current_version + 1
            entry = {
                "version": new_version,
                "prev_version": int(locked["active_version"]),
                "actor_user_id": actor_user_id,
                "changed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "action": action,
                "changes": changes,
                "snapshot": _script_snapshot(
                    locked,
                    enabled=new_enabled,
                    paused_by_user=new_paused,
                    cron_expr=new_cron,
                    timezone=new_zone,
                ),
            }
            updated = await company_data_db.update_locked_script_def(
                job_key,
                enabled=new_enabled,
                paused_by_user=new_paused,
                cron_expr=new_cron,
                timezone=new_zone,
                config_version=new_version,
                active_version=new_version,
                updated_by=actor_user_id,
                audit_history=[*history, entry],
                expected_config_version=expected_config_version,
                conn=db_conn,
            )
            if not updated:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "config_version conflict: "
                        f"expected {expected_config_version}, found {current_version}"
                    ),
                )
            if cancel_mode == "queued":
                await company_data_db.cancel_queued_script_runs(
                    job_key,
                    failure_reason=_PAUSED_BEFORE_START,
                    conn=db_conn,
                )
            elif cancel_mode == "queued_cron":
                await company_data_db.cancel_queued_script_runs(
                    job_key,
                    failure_reason=_SCHEDULE_CHANGED_BEFORE_START,
                    trigger_source="cron",
                    conn=db_conn,
                )


async def _reject_active_manual_trial(
    db_conn: Any,
    job_key: str,
    locked: Mapping[str, Any],
) -> None:
    """Reuse the data-task trial guard. The definition row is already locked."""
    from src.dalton.autonomy import system_jobs as system_jobs_mod

    async with db_conn.cursor() as cur:
        try:
            await system_jobs_mod._reject_enable_if_trial_run_active(
                cur,
                job_key,
                current_enabled=bool(locked["enabled"]),
                target_enabled=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _assert_migration_writable(state: Any) -> None:
    """Refuse frozen stages and any migration payload that is not a clear object.

    ``{}`` and a payload without ``company_migration`` are writable. A missing
    stage on a ``company_migration`` object is not frozen. Non-text, blank, and
    freeze stages are refused. Stages are not stringified.
    """
    if not isinstance(state, Mapping):
        raise HTTPException(
            status_code=409,
            detail="Company data task migration state is malformed",
        )
    if "company_migration" not in state:
        return
    block = state.get("company_migration")
    if not isinstance(block, Mapping):
        raise HTTPException(
            status_code=409,
            detail="Company data task migration state is malformed",
        )
    if "stage" not in block:
        return
    stage = block.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        raise HTTPException(
            status_code=409,
            detail="Company data task migration state is malformed",
        )
    if stage.strip() in _MIGRATION_FREEZE_STAGES:
        raise HTTPException(
            status_code=409,
            detail="Company data task is migration-frozen",
        )


def _script_history_or_refuse(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Accept the stored script history, or refuse the write. Do not rewrite it."""
    raw_cv = row.get("config_version")
    raw_av = row.get("active_version")
    if (
        isinstance(raw_cv, bool)
        or not isinstance(raw_cv, int)
        or raw_cv < 1
        or isinstance(raw_av, bool)
        or not isinstance(raw_av, int)
        or raw_av < 1
        or raw_av > raw_cv
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Corrupt script history: "
                f"config_version={raw_cv!r}, active_version={raw_av!r}"
            ),
        )
    raw_history = row.get("audit_history")
    if not isinstance(raw_history, list):
        raise HTTPException(
            status_code=409,
            detail="Corrupt script history: audit_history is not a list",
        )
    if len(raw_history) == 0:
        if raw_cv != 1 or raw_av != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Corrupt script history: empty audit_history with "
                    f"config_version={raw_cv}, active_version={raw_av}"
                ),
            )
        return []
    seen: set[int] = set()
    active_found = False
    copied: list[dict[str, Any]] = []
    for item in raw_history:
        if not isinstance(item, Mapping):
            raise HTTPException(
                status_code=409,
                detail="Corrupt script history: entry is not an object",
            )
        version = item.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise HTTPException(
                status_code=409,
                detail=f"Corrupt script history: invalid entry version {version!r}",
            )
        if version in seen:
            raise HTTPException(
                status_code=409,
                detail=f"Corrupt script history: duplicate version {version}",
            )
        seen.add(version)
        if not isinstance(item.get("snapshot"), Mapping):
            raise HTTPException(
                status_code=409,
                detail=f"Corrupt script history: version {version} missing snapshot object",
            )
        if version == raw_av:
            active_found = True
        copied.append(dict(item))
    if max(seen) != raw_cv:
        raise HTTPException(
            status_code=409,
            detail=(
                "Corrupt script history: "
                f"max entry version {max(seen)} != config_version {raw_cv}"
            ),
        )
    if not active_found:
        raise HTTPException(
            status_code=409,
            detail=(
                "Corrupt script history: "
                f"active_version {raw_av} not found in audit_history"
            ),
        )
    return copied


def _field_changes(
    old_state: Mapping[str, Any],
    new_state: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for name in _VERSION_FIELDS:
        if old_state[name] != new_state[name]:
            changes[name] = {"old": old_state[name], "new": new_state[name]}
    return changes


def _script_snapshot(
    row: Mapping[str, Any],
    *,
    enabled: bool,
    paused_by_user: bool,
    cron_expr: str,
    timezone: str,
) -> dict[str, Any]:
    """Store the definition's current notify and cos values, not an effective route."""
    routes = row.get("notify_routes")
    if isinstance(routes, list):
        routes = list(routes)
    values = {
        "enabled": enabled,
        "paused_by_user": paused_by_user,
        "notify_routes": routes,
        "cron_expr": cron_expr,
        "timezone": timezone,
        "notify_enabled": bool(row.get("notify_enabled")),
        "cos_mode": row.get("cos_mode"),
        "notify_storage_mode": row.get("notify_storage_mode"),
    }
    return {name: values[name] for name in ALLOWED_SCRIPT_SNAPSHOT_FIELDS}
