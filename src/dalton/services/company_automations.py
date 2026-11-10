"""Read-only business logic, permissions, and history sanitization for AU1 automations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.autonomy.classification import AUTOMATION_SCOPES, AutomationScope
from src.dalton.company_identity import is_company_system_user
from src.dalton.database import company_automations as company_auto_db
from src.server.database.principal_reach import workspace_or_home
from src.server.dependencies.principal import Principal, assert_reaches

ALLOWED_AUTOMATION_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "desired_status",
    "trigger_type",
    "cron_expression",
    "once_run_at",
    "trigger_config",
    "timezone",
    "instruction",
    "workspace_id",
    "llm_model",
    "additional_context",
    "thread_strategy",
    "conversation_thread_id",
    "max_failures",
    "notify_enabled",
    "notify_routes",
    "delivery_config",
)

ALLOWED_AUTOMATION_SNAPSHOT_FIELD_SET: frozenset[str] = frozenset(
    ALLOWED_AUTOMATION_SNAPSHOT_FIELDS
)

_SENSITIVE_NESTED_KEYS: frozenset[str] = frozenset(
    {
        "metadata",
        "pre_company_migration",
        "company_migration",
        "company_migration_batches",
        "created_secrets",
        "preexisting_verified_secrets",
        "vault_source_user_id",
        "legacy_notify_owner_user_id",
        "secret",
        "secrets",
        "secret_value",
        "encrypted_value",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "bot_token",
        "password",
        "credential",
        "credentials",
        "authorization",
        "webhook_secret",
    }
)


class AutomationFieldChangeResponse(BaseModel):
    """Sanitized before/after value pair for a single versioned configuration field."""

    model_config = ConfigDict(extra="ignore")

    old: Any = None
    new: Any = None


class AutomationHistoryEntryResponse(BaseModel):
    """Explicit response model for a single modification history entry."""

    model_config = ConfigDict(extra="ignore")

    version: int
    prev_version: int | None = None
    restored_from_version: int | None = None
    actor_user_id: str | None = None
    changed_at: str | None = None
    action: str
    changes: dict[str, AutomationFieldChangeResponse] = Field(default_factory=dict)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class AutomationHistoryResponse(BaseModel):
    """Paginated read-only modification history for an automation."""

    model_config = ConfigDict(extra="ignore")

    automation_id: UUID
    scope: str
    config_version: int
    active_version: int
    entries: list[AutomationHistoryEntryResponse] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


def _scrub_nested_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            key_lower = key_str.strip().lower()
            if (
                key_lower in _SENSITIVE_NESTED_KEYS
                or "secret" in key_lower
                or "token" in key_lower
                or "password" in key_lower
                or "credential" in key_lower
            ):
                continue
            cleaned[key_str] = _scrub_nested_value(v)
        return cleaned
    if isinstance(value, list):
        return [_scrub_nested_value(item) for item in value]
    return value


def sanitize_automation_snapshot(raw_snapshot: Any) -> dict[str, Any]:
    """Keep only §2.1.3 allowed configuration fields and strip nested secrets/metadata."""
    if not isinstance(raw_snapshot, Mapping):
        return {}
    sanitized: dict[str, Any] = {}
    for field_name in ALLOWED_AUTOMATION_SNAPSHOT_FIELDS:
        if field_name not in raw_snapshot:
            continue
        val = raw_snapshot[field_name]
        if field_name in (
            "trigger_config",
            "additional_context",
            "notify_routes",
            "delivery_config",
        ):
            val = _scrub_nested_value(val)
        sanitized[field_name] = val
    return sanitized


def sanitize_automation_changes(
    raw_changes: Any,
) -> dict[str, AutomationFieldChangeResponse]:
    """Keep only §2.1.3 allowed configuration fields in ``changes``."""
    if not isinstance(raw_changes, Mapping):
        return {}
    sanitized: dict[str, AutomationFieldChangeResponse] = {}
    for field_name in ALLOWED_AUTOMATION_SNAPSHOT_FIELDS:
        if field_name not in raw_changes:
            continue
        entry = raw_changes[field_name]
        if isinstance(entry, Mapping):
            old_val = _scrub_nested_value(entry.get("old"))
            new_val = _scrub_nested_value(entry.get("new"))
        else:
            old_val = None
            new_val = _scrub_nested_value(entry)
        sanitized[field_name] = AutomationFieldChangeResponse(old=old_val, new=new_val)
    return sanitized


def _parse_history_entry(raw_entry: Any) -> AutomationHistoryEntryResponse | None:
    if not isinstance(raw_entry, Mapping):
        return None
    raw_ver = raw_entry.get("version")
    try:
        version = int(raw_ver)
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None

    def _opt_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    changed_raw = raw_entry.get("changed_at")
    changed_at = (
        changed_raw.isoformat()
        if hasattr(changed_raw, "isoformat")
        else (str(changed_raw) if changed_raw is not None else None)
    )
    actor_raw = raw_entry.get("actor_user_id")
    actor_user_id = str(actor_raw) if actor_raw is not None else None
    action = str(raw_entry.get("action") or "update")

    return AutomationHistoryEntryResponse(
        version=version,
        prev_version=_opt_int(raw_entry.get("prev_version")),
        restored_from_version=_opt_int(raw_entry.get("restored_from_version")),
        actor_user_id=actor_user_id,
        changed_at=changed_at,
        action=action,
        changes=sanitize_automation_changes(raw_entry.get("changes")),
        snapshot=sanitize_automation_snapshot(raw_entry.get("snapshot")),
    )


def apply_company_notify_representation(
    resp: Any,
    raw_row: Mapping[str, Any] | None = None,
) -> None:
    """Populate row-backed notify fields on a company automation without caller preferences.

    Database constraint ``automations_company_notify_mode_check`` requires every
    ``scope = 'company'`` row to use ``notify_storage_mode = 'task_row'``.
    """
    from src.dalton.notify.dispatch import (
        classify_automation_for_notify,
        is_excluded_automation_for_notify,
    )

    auto_probe = {
        "name": getattr(resp, "name", None),
        "metadata": getattr(resp, "metadata", None),
    }
    classification = classify_automation_for_notify(auto_probe)
    if classification is not None:
        cat: str | None = classification.category
    elif is_excluded_automation_for_notify(auto_probe):
        cat = None
    else:
        cat = "custom"

    storage_mode = str(
        (raw_row or {}).get("notify_storage_mode")
        if isinstance(raw_row, Mapping) and raw_row.get("notify_storage_mode") is not None
        else getattr(resp, "notify_storage_mode", None) or ""
    ).strip()
    if storage_mode != "task_row":
        raise ValueError(
            f"Company automations require notify_storage_mode='task_row', got {storage_mode!r}"
        )

    raw_enabled = (
        raw_row.get("notify_enabled")
        if isinstance(raw_row, Mapping) and "notify_enabled" in raw_row
        else getattr(resp, "notify_enabled", True)
    )
    enabled = bool(True if raw_enabled is None else raw_enabled)
    raw_routes = (
        raw_row.get("notify_routes")
        if isinstance(raw_row, Mapping) and "notify_routes" in raw_row
        else getattr(resp, "notify_routes", None)
    )
    route_targets = (
        [
            str(t).strip()
            for t in raw_routes
            if isinstance(t, str) and str(t).strip()
        ]
        if isinstance(raw_routes, Sequence)
        and not isinstance(raw_routes, (str, bytes))
        else []
    )

    setattr(resp, "notify_storage_mode", "task_row")
    setattr(resp, "dalton_notify_category", cat)
    setattr(resp, "dalton_notify_effective", enabled if cat is not None else None)
    setattr(resp, "dalton_notify_source", "task_row" if cat is not None else None)
    setattr(
        resp,
        "dalton_notify_route_targets",
        route_targets if cat is not None else None,
    )


async def require_company_scope_read_access(principal: Principal) -> None:
    """Allow reading ``scope=company`` only for human internal/admin callers without computer_id."""
    if principal.computer_id is not None:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not await is_internal_or_admin(principal.user_id):
        raise HTTPException(status_code=403, detail="Forbidden")


async def list_automations_for_scope(
    principal: Principal,
    *,
    scope: AutomationScope,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Read-only scoped automation listing enforcing company/computer boundaries."""
    from src.dalton.notify.dispatch import populate_automation_notify_state
    from src.server.models.automation import AutomationResponse, AutomationsListResponse

    if scope not in AUTOMATION_SCOPES:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {scope!r}")

    if scope == "company":
        await require_company_scope_read_access(principal)

    rows, total = await company_auto_db.list_automations_for_scope_db(
        scope=scope,
        caller_user_id=principal.user_id,
        status=status,
        limit=limit,
        offset=offset,
        computer_id=principal.computer_id,
    )
    items = [AutomationResponse.model_validate(r) for r in rows]

    if scope == "company":
        for resp, raw_row in zip(items, rows):
            apply_company_notify_representation(resp, raw_row)
    else:
        await populate_automation_notify_state(principal.user_id, items)

    return AutomationsListResponse(automations=items, total=total)


async def get_automation_modification_history(
    principal: Principal,
    automation_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> AutomationHistoryResponse:
    """Read-only version history for an automation; never seeds or mutates DB state."""
    row = await company_auto_db.get_automation_history_row(automation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    owner_id = str(row.get("user_id") or "").strip()
    row_scope = str(row.get("scope") or "personal").strip() or "personal"
    if row_scope == "company" or is_company_system_user(owner_id):
        row_scope = "company"
        await require_company_scope_read_access(principal)
    else:
        if owner_id != principal.user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if principal.computer_id is not None:
            await assert_reaches(
                principal,
                workspace_id=workspace_or_home(
                    principal.user_id, row.get("workspace_id")
                ),
            )

    raw_history = row.get("audit_history")
    parsed_entries: list[AutomationHistoryEntryResponse] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            parsed = _parse_history_entry(item)
            if parsed is not None:
                parsed_entries.append(parsed)

    parsed_entries.sort(key=lambda entry: entry.version, reverse=True)
    total = len(parsed_entries)
    page_entries = parsed_entries[offset : offset + limit]

    return AutomationHistoryResponse(
        automation_id=UUID(str(row["automation_id"])),
        scope=row_scope,
        config_version=int(row.get("config_version") or 1),
        active_version=int(row.get("active_version") or 1),
        entries=page_entries,
        total=total,
        limit=limit,
        offset=offset,
    )
