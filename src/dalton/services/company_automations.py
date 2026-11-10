"""Business logic, permissions, version checks, and history snapshots for AU1 automations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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


class AutomationVersionConflictError(ValueError):
    """Raised when ``expected_config_version`` does not match the locked row's ``config_version``."""


class AutomationHistoryCorruptError(ValueError):
    """Raised when a locked row's existing ``audit_history`` or version counters are inconsistent."""


class _UnsetType:
    """Sentinel distinguishing an omitted argument from an explicit ``None``."""


UNSET = _UnsetType()


@dataclass
class AutomationHistoryContext:
    """Per-operation history recording context propagated to the database layer."""

    record_history: bool = True
    actor_user_id: str | None | _UnsetType = UNSET
    action: str | None = None
    expected_config_version: int | None = None
    skip_if_unchanged: bool = False
    locked_row_reconciler: (
        Callable[[Mapping[str, Any], dict[str, Any], Any], Awaitable[None]] | None
    ) = None

    @property
    def has_explicit_actor(self) -> bool:
        """Return True when ``actor_user_id`` was explicitly bound (including ``None``)."""
        return not isinstance(self.actor_user_id, _UnsetType)

    @property
    def has_explicit_action(self) -> bool:
        """Return True when ``action`` was explicitly bound to a non-empty action name."""
        return isinstance(self.action, str) and bool(self.action.strip())


@dataclass
class FileCoalesceState:
    """State for coalescing two-step ``AutomationFile.commit`` operations into one version."""

    mode: str  # "create_pause" or "update_action"
    has_action_step: bool = False
    step1_pre_row: dict[str, Any] | None = None
    step1_pref_row: dict[str, Any] | None = None


_HISTORY_CTX_VAR: ContextVar[AutomationHistoryContext | None] = ContextVar(
    "dalton_automation_history_ctx", default=None
)
_FILE_COALESCE_VAR: ContextVar[FileCoalesceState | None] = ContextVar(
    "dalton_automation_file_coalesce_ctx", default=None
)


def get_current_history_context() -> AutomationHistoryContext | None:
    """Return the active ``AutomationHistoryContext`` if one is set in the current task."""
    return _HISTORY_CTX_VAR.get()


def get_current_file_coalesce_state() -> FileCoalesceState | None:
    """Return the active ``FileCoalesceState`` if inside an ``AutomationFile`` coalesced save."""
    return _FILE_COALESCE_VAR.get()


@contextmanager
def automation_history_context(
    *,
    record_history: bool = True,
    actor_user_id: str | None | _UnsetType = UNSET,
    action: str | None = None,
    expected_config_version: int | None = None,
    skip_if_unchanged: bool | None = None,
    locked_row_reconciler: (
        Callable[[Mapping[str, Any], dict[str, Any], Any], Awaitable[None]] | None
    ) = None,
) -> Iterator[AutomationHistoryContext]:
    """Bind history recording parameters for the enclosed automation write call."""
    parent = _HISTORY_CTX_VAR.get()
    eff_expected = (
        expected_config_version
        if expected_config_version is not None
        else (parent.expected_config_version if parent is not None else None)
    )
    eff_actor = (
        actor_user_id
        if not isinstance(actor_user_id, _UnsetType)
        else (parent.actor_user_id if parent is not None else UNSET)
    )
    eff_action = (
        action
        if action is not None
        else (parent.action if parent is not None else None)
    )
    eff_skip_unchanged = (
        skip_if_unchanged
        if skip_if_unchanged is not None
        else (parent.skip_if_unchanged if parent is not None else False)
    )
    ctx = AutomationHistoryContext(
        record_history=record_history,
        actor_user_id=eff_actor,
        action=eff_action,
        expected_config_version=eff_expected,
        skip_if_unchanged=eff_skip_unchanged,
        locked_row_reconciler=locked_row_reconciler,
    )
    token = _HISTORY_CTX_VAR.set(ctx)
    try:
        yield ctx
    finally:
        _HISTORY_CTX_VAR.reset(token)


@contextmanager
def coalesce_file_create_pause(*, pause: bool) -> Iterator[FileCoalesceState | None]:
    """Coalesce ``AutomationFile`` create + immediate pause into a single ``v1`` history entry."""
    if not pause:
        yield None
        return
    state = FileCoalesceState(mode="create_pause", has_action_step=True)
    token = _FILE_COALESCE_VAR.set(state)
    try:
        yield state
    finally:
        _FILE_COALESCE_VAR.reset(token)


@contextmanager
def coalesce_file_update_with_action(
    *, has_fields: bool, has_action: bool
) -> Iterator[FileCoalesceState | None]:
    """Coalesce ``AutomationFile`` field update + pause/resume into one version increment."""
    if not (has_fields and has_action):
        yield None
        return
    state = FileCoalesceState(mode="update_action", has_action_step=True)
    token = _FILE_COALESCE_VAR.set(state)
    try:
        yield state
    finally:
        _FILE_COALESCE_VAR.reset(token)


def validate_expected_config_version(
    raw_value: Any,
    *,
    provided: bool = True,
) -> int | None:
    """Validate ``expected_config_version`` when supplied by a caller.

    Omitted values return ``None`` for backward compatibility. Any explicitly
    supplied value that is ``None``, boolean, non-integer, or ``< 1`` raises
    ``HTTPException(400)``.
    """
    if not provided:
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 1:
        raise HTTPException(
            status_code=400,
            detail="expected_config_version must be a positive integer",
        )
    return raw_value


def extract_expected_config_version(body: Any) -> int | None:
    """Extract and validate optional ``expected_config_version`` from a model or mapping."""
    if body is None or type(body).__name__ in ("Body", "FieldInfo"):
        return None
    if isinstance(body, BaseModel):
        if "expected_config_version" not in body.model_fields_set:
            return None
        return validate_expected_config_version(
            getattr(body, "expected_config_version", None),
            provided=True,
        )
    if isinstance(body, Mapping):
        if "expected_config_version" not in body:
            return None
        return validate_expected_config_version(
            body.get("expected_config_version"),
            provided=True,
        )
    raise HTTPException(
        status_code=400,
        detail="expected_config_version must be a positive integer",
    )

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


def resolve_desired_status_for_snapshot(
    row: Mapping[str, Any],
    *,
    explicit_desired_status: str | None = None,
) -> str:
    """Resolve snapshot ``desired_status`` strictly to ``'active'`` or ``'paused'``."""
    if explicit_desired_status in ("active", "paused"):
        return explicit_desired_status
    raw_status = str(row.get("status") or "active").strip().lower()
    meta = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    if raw_status == "paused":
        return "paused"
    if raw_status == "active":
        return "active"
    if raw_status == "executing":
        return "paused" if bool(meta.get("paused_by_user")) else "active"
    raw_history = row.get("audit_history")
    active_ver = row.get("active_version")
    if isinstance(raw_history, list) and raw_history:
        for item in reversed(raw_history):
            if not isinstance(item, Mapping):
                continue
            if active_ver is not None and item.get("version") != active_ver:
                continue
            snap = item.get("snapshot")
            if isinstance(snap, Mapping) and snap.get("desired_status") in (
                "active",
                "paused",
            ):
                return str(snap["desired_status"])
        for item in reversed(raw_history):
            if not isinstance(item, Mapping):
                continue
            snap = item.get("snapshot")
            if isinstance(snap, Mapping) and snap.get("desired_status") in (
                "active",
                "paused",
            ):
                return str(snap["desired_status"])
    if bool(meta.get("paused_by_user")):
        return "paused"
    return "active"


def build_automation_config_snapshot(
    row: Mapping[str, Any],
    *,
    desired_status: str | None = None,
    once_run_at: str | None = None,
    notify_enabled: bool = False,
    notify_routes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Construct a complete 18-field §2.1.3 configuration snapshot from ``row``."""
    eff_desired = resolve_desired_status_for_snapshot(
        row, explicit_desired_status=desired_status
    )
    trigger_type = str(row.get("trigger_type") or "cron").strip() or "cron"
    eff_once_run_at = once_run_at if trigger_type == "once" else None
    ws_raw = row.get("workspace_id")
    ws_str = str(ws_raw).strip() if ws_raw is not None and str(ws_raw).strip() else None
    conv_raw = row.get("conversation_thread_id")
    conv_str = (
        str(conv_raw).strip()
        if conv_raw is not None and str(conv_raw).strip()
        else None
    )
    raw_trigger_cfg = row.get("trigger_config")
    trigger_cfg = dict(raw_trigger_cfg) if isinstance(raw_trigger_cfg, Mapping) else {}
    raw_add_ctx = row.get("additional_context")
    if isinstance(raw_add_ctx, Mapping):
        add_ctx: Any = dict(raw_add_ctx)
    elif isinstance(raw_add_ctx, list):
        add_ctx = list(raw_add_ctx)
    else:
        add_ctx = None
    raw_delivery = row.get("delivery_config")
    delivery_cfg = dict(raw_delivery) if isinstance(raw_delivery, Mapping) else {}
    routes_list = [
        str(t).strip()
        for t in (notify_routes or [])
        if isinstance(t, str) and str(t).strip()
    ]
    try:
        max_failures = int(
            3 if row.get("max_failures") is None else row.get("max_failures")
        )
    except (TypeError, ValueError):
        max_failures = 3

    raw_snapshot: dict[str, Any] = {
        "name": str(row.get("name") or ""),
        "description": (
            str(row["description"]) if row.get("description") is not None else None
        ),
        "desired_status": eff_desired,
        "trigger_type": trigger_type,
        "cron_expression": (
            str(row["cron_expression"])
            if row.get("cron_expression") is not None
            else None
        ),
        "once_run_at": eff_once_run_at,
        "trigger_config": trigger_cfg,
        "timezone": str(row.get("timezone") or "UTC"),
        "instruction": str(row.get("instruction") or ""),
        "workspace_id": ws_str,
        "llm_model": (
            str(row["llm_model"]) if row.get("llm_model") is not None else None
        ),
        "additional_context": add_ctx,
        "thread_strategy": str(row.get("thread_strategy") or "new"),
        "conversation_thread_id": conv_str,
        "max_failures": max_failures,
        "notify_enabled": bool(notify_enabled),
        "notify_routes": routes_list,
        "delivery_config": delivery_cfg,
    }
    return sanitize_automation_snapshot(raw_snapshot)


def compute_snapshot_changes(
    pre_snapshot: Mapping[str, Any],
    post_snapshot: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Compute field-level ``{old, new}`` diff across the 18 allowed snapshot fields."""
    changes: dict[str, dict[str, Any]] = {}
    for field_name in ALLOWED_AUTOMATION_SNAPSHOT_FIELDS:
        old_val = pre_snapshot.get(field_name)
        new_val = post_snapshot.get(field_name)
        if old_val != new_val:
            changes[field_name] = {"old": old_val, "new": new_val}
    return changes


def validate_existing_history_state(row: Mapping[str, Any]) -> None:
    """Validate that a locked row's version counters and ``audit_history`` are consistent.

    Raises ``AutomationHistoryCorruptError`` instead of silently overwriting
    when ``config_version > 1`` has an empty ``audit_history`` or when existing
    history entries are missing/corrupt for ``active_version``.
    """
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
        raise AutomationHistoryCorruptError(
            f"Corrupt automation version state: config_version={raw_cv!r}, active_version={raw_av!r}"
        )

    raw_history = row.get("audit_history")
    if not isinstance(raw_history, list):
        raise AutomationHistoryCorruptError(
            "Corrupt automation history: audit_history is not a list"
        )

    if len(raw_history) == 0:
        if raw_cv != 1 or raw_av != 1:
            raise AutomationHistoryCorruptError(
                f"Corrupt automation history: empty audit_history with config_version={raw_cv}, active_version={raw_av}"
            )
        return

    seen_versions: set[int] = set()
    active_entry: Mapping[str, Any] | None = None
    for item in raw_history:
        if not isinstance(item, Mapping):
            raise AutomationHistoryCorruptError(
                "Corrupt automation history: entry is not an object"
            )
        ver = item.get("version")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise AutomationHistoryCorruptError(
                f"Corrupt automation history: invalid entry version {ver!r}"
            )
        if ver in seen_versions:
            raise AutomationHistoryCorruptError(
                f"Corrupt automation history: duplicate version {ver}"
            )
        seen_versions.add(ver)
        snap = item.get("snapshot")
        if not isinstance(snap, Mapping):
            raise AutomationHistoryCorruptError(
                f"Corrupt automation history: version {ver} missing snapshot object"
            )
        if ver == raw_av:
            active_entry = item

    if max(seen_versions) != raw_cv:
        raise AutomationHistoryCorruptError(
            f"Corrupt automation history: max entry version {max(seen_versions)} != config_version {raw_cv}"
        )
    if active_entry is None:
        raise AutomationHistoryCorruptError(
            f"Corrupt automation history: active_version {raw_av} not found in audit_history"
        )
    active_snap = active_entry.get("snapshot")
    if (
        not isinstance(active_snap, Mapping)
        or "name" not in active_snap
        or "trigger_type" not in active_snap
    ):
        raise AutomationHistoryCorruptError(
            f"Corrupt automation history: active_version {raw_av} snapshot is incomplete"
        )
    if "desired_status" in active_snap and active_snap.get("desired_status") not in (
        "active",
        "paused",
    ):
        raise AutomationHistoryCorruptError(
            f"Corrupt automation history: invalid desired_status {active_snap.get('desired_status')!r}"
        )


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


_resolve_effective_once_run_at = company_auto_db._resolve_effective_once_run_at
