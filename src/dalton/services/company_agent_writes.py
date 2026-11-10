"""Update, pause, and resume an existing company-owned agent.

The row must already have both ``scope='company'`` and
``user_id=COMPANY_SYSTEM_USER_ID``. The caller is the real internal member
recorded as ``updated_by`` and as the new history actor. The database owner
stays the fixed company identity. This module does not create company agents,
change notification columns, or run a task.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID, is_company_system_user
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.services.company_automations import (
    apply_company_notify_representation,
    automation_history_context,
    require_company_scope_read_access,
    validate_expected_config_version,
)
from src.dalton.services.company_task_read import (
    is_shared_company_agent,
    scrub_company_agent_row,
)
from src.server.database import automation as auto_db
from src.server.dependencies.principal import Principal
from src.server.models.automation import AutomationResponse, AutomationUpdate
from src.server.services.automations import lifecycle
from src.server.utils.api import raise_not_found

# Stages stored on ``metadata.company_migration.stage``. The names are the
# ``dalton_company_migration_batches.stage`` check values. ``running``,
# ``done``, and ``rolled_back`` do not block a write.
_MIGRATION_FREEZE_STAGES = frozenset({"frozen", "rollback_frozen", "switching"})

_PROTECTED_WRITE_KEYS = (
    "scope",
    "user_id",
    "created_by",
    "updated_by",
    "audit_history",
    "config_version",
    "active_version",
    "notify_storage_mode",
    "notify_enabled",
    "notify_routes",
    "delivery_config",
    "deleted_at",
    "deleted_by",
)


def company_migration_stage(metadata: Any) -> str | None:
    """Return the stored ``company_migration.stage`` text, or None when absent."""
    if not isinstance(metadata, Mapping):
        return None
    block = metadata.get("company_migration")
    if not isinstance(block, Mapping):
        return None
    stage = block.get("stage")
    if stage is None:
        return None
    text = str(stage).strip()
    return text or None


def company_delivery_is_nonempty(raw: Any) -> bool:
    """True when a delivery body names a method or any key other than ``methods``.

    Omitted, null, ``{}``, and blank method strings are empty. A webhook or
    other extra key is not empty, even when ``DeliveryConfig`` would drop it.
    """
    if raw is None:
        return False
    if not isinstance(raw, Mapping):
        return True
    methods = raw.get("methods")
    if isinstance(methods, str):
        if methods.strip():
            return True
    elif isinstance(methods, Sequence) and not isinstance(methods, (str, bytes)):
        if any(str(item).strip() for item in methods):
            return True
    elif methods:
        return True
    return any(key != "methods" for key in raw)


def assert_locked_company_agent(locked_row: Mapping[str, Any]) -> None:
    """Re-check company identity, deletion, notify mode, and migration freeze.

    Call this on the row locked in the write transaction, before history is
    prepared. Raises ``HTTPException`` so that transaction rolls back.
    """
    if locked_row.get("deleted_at") is not None:
        raise_not_found("Automation")
    user_id = str(locked_row.get("user_id") or "")
    scope = str(locked_row.get("scope") or "").strip()
    if scope != "company" or not is_company_system_user(user_id):
        raise HTTPException(status_code=409, detail="Company automation identity changed")
    mode = str(locked_row.get("notify_storage_mode") or "").strip()
    if mode != "task_row":
        raise HTTPException(
            status_code=409,
            detail="Company automations require notify_storage_mode='task_row'",
        )
    if company_migration_stage(locked_row.get("metadata")) in _MIGRATION_FREEZE_STAGES:
        raise HTTPException(status_code=409, detail="Company automation is migration-frozen")


def company_agent_write_response(row: Mapping[str, Any]) -> AutomationResponse:
    """Company read shape: scrub secrets and represent task-row notify."""
    response = AutomationResponse.model_validate(scrub_company_agent_row(row))
    apply_company_notify_representation(response, row)
    return response


def _require_company_version(raw_value: Any) -> int:
    """Company writes require a positive integer ``expected_config_version``."""
    validated = validate_expected_config_version(raw_value, provided=True)
    if validated is None:
        raise HTTPException(
            status_code=400,
            detail="expected_config_version must be a positive integer",
        )
    return validated


def _strip_company_protected_kwargs(effective_kwargs: dict[str, Any]) -> None:
    for key in _PROTECTED_WRITE_KEYS:
        effective_kwargs.pop(key, None)


def _raw_delivery(request: AutomationUpdate | None, fields: Mapping[str, Any]) -> Any:
    if request is not None and "delivery_config_raw" in request.model_fields_set:
        return request.delivery_config_raw
    if "delivery_config" in fields:
        return fields.get("delivery_config")
    return None


def _refuse_company_delivery(request: AutomationUpdate | None, fields: Mapping[str, Any]) -> None:
    if company_delivery_is_nonempty(_raw_delivery(request, fields)):
        raise HTTPException(
            status_code=400,
            detail="Company automations cannot set a non-empty delivery_config",
        )


async def _company_write_allowed(principal: Principal, automation_id: str) -> bool:
    """True when ``automation_id`` is a live shared company agent this caller may write.

    Returns False when the id is not a shared company agent, so the route can
    answer 404 without treating a private or research row as company-owned.
    """
    identity = await company_data_db.get_automation_owner_identity(automation_id)
    if not is_shared_company_agent(identity):
        return False
    await require_company_scope_read_access(principal)
    if is_company_system_user(principal.user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    if identity is not None and identity.get("deleted_at") is not None:
        raise_not_found("Automation")
    return True


async def _model_pref_for_lock() -> dict[str, Any] | None:
    """Load the company user's model preference before the row lock, when needed."""
    from src.server.services.claude_code.hosted import checks_automations

    if not checks_automations():
        return None
    from src.server.services.llm import user_models

    return await user_models.get_model_preference(COMPANY_SYSTEM_USER_ID)


async def update_company_agent(
    principal: Principal,
    automation_id: str,
    fields: dict[str, Any],
    *,
    request: AutomationUpdate | None = None,
    expected_config_version: int | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Update one existing company agent.

    Returns None when the id is not a shared company agent. The caller must
    already be an internal member or admin without ``computer_id``. The stored
    company notify columns are left unchanged.
    """
    if not await _company_write_allowed(principal, automation_id):
        return None
    version = _require_company_version(expected_config_version)
    _refuse_company_delivery(request, fields)
    writable = {
        key: value
        for key, value in fields.items()
        if key not in _PROTECTED_WRITE_KEYS and key != "expected_config_version"
    }
    try:
        prepared = await lifecycle.prepare_owned_automation_update(
            automation_id,
            COMPANY_SYSTEM_USER_ID,
            writable,
            conn=conn,
            model_pref=None,
            delivery_checked=False,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False, include_input=False),
        ) from None
    if prepared is None:
        return None

    pref_for_lock = await _model_pref_for_lock()

    async def _reconcile(
        locked_row: Mapping[str, Any],
        effective_kwargs: dict[str, Any],
        locked_conn: Any,
    ) -> None:
        assert_locked_company_agent(locked_row)
        _strip_company_protected_kwargs(effective_kwargs)
        if not prepared.update_kwargs:
            return
        await lifecycle.recheck_locked_automation_update(
            user_id=COMPANY_SYSTEM_USER_ID,
            update=prepared.update,
            current=prepared.current,
            stored_in=prepared.stored_in,
            stored_pin=prepared.stored_pin,
            new_cron=prepared.new_cron,
            new_tz=prepared.new_tz,
            pref_for_lock=pref_for_lock,
            locked_row=locked_row,
            effective_kwargs=effective_kwargs,
            locked_conn=locked_conn,
        )

    record_history = bool(prepared.update_kwargs)
    with automation_history_context(
        actor_user_id=principal.user_id,
        action="update",
        expected_config_version=version,
        record_history=record_history,
        locked_row_reconciler=_reconcile,
        prelocked_pref_row={},
    ):
        if prepared.update_kwargs:
            return await auto_db.update_automation(
                automation_id,
                COMPANY_SYSTEM_USER_ID,
                conn=conn,
                **prepared.update_kwargs,
            )
        return await auto_db.update_automation(
            automation_id,
            COMPANY_SYSTEM_USER_ID,
            conn=conn,
        )


async def pause_company_agent(
    principal: Principal,
    automation_id: str,
    *,
    expected_config_version: int | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Pause one existing company agent. Returns None when it is not company-owned."""
    if not await _company_write_allowed(principal, automation_id):
        return None
    version = _require_company_version(expected_config_version)
    current = await auto_db.get_automation(automation_id, COMPANY_SYSTEM_USER_ID, conn=conn)
    if current is None:
        return None
    update_kwargs = lifecycle.pause_update_fields(current)

    async def _reconcile(
        locked_row: Mapping[str, Any],
        effective_kwargs: dict[str, Any],
        _locked_conn: Any,
    ) -> None:
        assert_locked_company_agent(locked_row)
        _strip_company_protected_kwargs(effective_kwargs)
        lifecycle.apply_locked_pause_schedule(locked_row, effective_kwargs)

    with automation_history_context(
        actor_user_id=principal.user_id,
        action="pause",
        expected_config_version=version,
        locked_row_reconciler=_reconcile,
        prelocked_pref_row={},
    ):
        return await auto_db.update_automation(
            automation_id,
            COMPANY_SYSTEM_USER_ID,
            conn=conn,
            **update_kwargs,
        )


async def resume_company_agent(
    principal: Principal,
    automation_id: str,
    *,
    expected_config_version: int | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Resume one existing company agent. Returns None when it is not company-owned."""
    if not await _company_write_allowed(principal, automation_id):
        return None
    version = _require_company_version(expected_config_version)
    current = await auto_db.get_automation(automation_id, COMPANY_SYSTEM_USER_ID, conn=conn)
    if current is None:
        return None
    update_kwargs = lifecycle.resume_update_fields(current)

    async def _reconcile(
        locked_row: Mapping[str, Any],
        effective_kwargs: dict[str, Any],
        _locked_conn: Any,
    ) -> None:
        assert_locked_company_agent(locked_row)
        _strip_company_protected_kwargs(effective_kwargs)
        lifecycle.apply_locked_resume_schedule(locked_row, effective_kwargs)

    with automation_history_context(
        actor_user_id=principal.user_id,
        action="resume",
        expected_config_version=version,
        locked_row_reconciler=_reconcile,
        prelocked_pref_row={},
    ):
        return await auto_db.update_automation(
            automation_id,
            COMPANY_SYSTEM_USER_ID,
            conn=conn,
            **update_kwargs,
        )
