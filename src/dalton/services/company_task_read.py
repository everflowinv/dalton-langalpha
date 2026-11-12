"""Read-only company catalog: real company agents plus manifest data scripts.

List, detail, execution, and history reads fail when the database cannot
supply the current definition. They do not insert rows, seed history, or copy
manifest defaults into the response.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any, Literal

from fastapi import HTTPException

from src.dalton.company_identity import is_company_system_user
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.services.company_automations import (
    AutomationFieldChangeResponse,
    AutomationHistoryEntryResponse,
    AutomationHistoryResponse,
    _scrub_nested_value,
    require_company_scope_read_access,
)
from src.dalton.services.company_automations import Principal
from src.server.models.automation import (
    AutomationResponse,
    AutomationRunResponse,
    AutomationRunsListResponse,
    AutomationsListResponse,
    ScriptAutomationResponse,
    ScriptExecutionResponse,
    ScriptRunResponse,
)

# Legal script snapshot fields stored by the data-task definition. This is not
# the agent snapshot allowlist: cron_expr and enabled would be dropped by it.
ALLOWED_SCRIPT_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "enabled",
    "paused_by_user",
    "notify_routes",
    "cron_expr",
    "timezone",
    "notify_enabled",
    "cos_mode",
    "notify_storage_mode",
)
ALLOWED_SCRIPT_SNAPSHOT_FIELD_SET: frozenset[str] = frozenset(
    ALLOWED_SCRIPT_SNAPSHOT_FIELDS
)

_DEF_REQUIRED_FIELDS: tuple[str, ...] = (
    "job_key",
    "title",
    "cron_expr",
    "timezone",
    "enabled",
    "paused_by_user",
    "notify_enabled",
    "notify_storage_mode",
    "config_version",
    "active_version",
    "updated_at",
)

_DATA_ID_RE = re.compile(r"data:(?P<key>[a-z0-9]+(?:-[a-z0-9]+)*)\Z")

_COMPANY_AGENT_DROP_KEYS: frozenset[str] = frozenset(
    {
        "command_argv",
        "workdir",
        "required_secrets",
        "secret_names",
        "migration_state",
        "audit_history",
        "outbox_payload",
        "notify_preview",
        "frozen_manifest",
        "frozen_snapshot_dir",
        "file_name",
    }
)


class InvalidAutomationId(ValueError):
    """The path id is neither a UUID nor a canonical ``data:<job_key>``."""


class CompanyDataTaskReadError(HTTPException):
    """An authorized read cannot see a real definition row."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=503, detail=detail)


@dataclass(frozen=True)
class ParsedAutomationId:
    kind: Literal["agent", "script"]
    agent_id: str | None = None
    job_key: str | None = None


def parse_automation_read_id(raw: str) -> ParsedAutomationId:
    """Classify a read id. Illegal text raises ``InvalidAutomationId``."""
    text = str(raw or "")
    matched = _DATA_ID_RE.fullmatch(text)
    if matched is not None:
        return ParsedAutomationId(kind="script", job_key=matched.group("key"))
    try:
        import uuid

        return ParsedAutomationId(kind="agent", agent_id=str(uuid.UUID(text)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidAutomationId(text) from exc


def is_shared_company_agent(row: Mapping[str, Any] | None) -> bool:
    """True only when the row is both company-scoped and company-owned."""
    if not isinstance(row, Mapping):
        return False
    scope = str(row.get("scope") or "").strip()
    return scope == "company" and is_company_system_user(str(row.get("user_id") or ""))


def script_display_status(*, enabled: bool, paused_by_user: bool) -> str:
    """Map definition flags to the catalog status. Run status is not an input."""
    if paused_by_user:
        return "paused"
    if enabled:
        return "active"
    return "disabled"


def script_matches_list_status(
    status: str | None,
    *,
    enabled: bool,
    paused_by_user: bool,
) -> bool:
    """List ``status`` filters script configuration, not ``dalton_data_task_runs.status``."""
    if not status:
        return True
    return script_display_status(enabled=enabled, paused_by_user=paused_by_user) == status


def company_catalog_sort_key(item: Any) -> tuple[float, str, str]:
    """Company list order: ``updated_at DESC, engine_kind ASC, automation_id ASC``.

    The negative timestamp is the descending part. ``reverse=True`` would also
    reverse the engine and id tie breaks.
    """
    updated = getattr(item, "updated_at", None)
    epoch = _aware_epoch(updated)
    return (
        -epoch,
        str(getattr(item, "engine_kind", "") or ""),
        str(getattr(item, "automation_id", "") or ""),
    )


def execution_feed_sort_key(row: Mapping[str, Any]) -> tuple[float, str, str]:
    """Company execution order: ``created_at DESC, engine_kind ASC, execution_id ASC``."""
    return (
        -_aware_epoch(row.get("created_at")),
        str(row.get("engine_kind") or ""),
        str(row.get("automation_execution_id") or "").lower(),
    )


def merge_execution_windows(
    agent_rows: Sequence[Mapping[str, Any]],
    script_rows: Sequence[Mapping[str, Any]],
    *,
    offset: int,
    limit: int,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Merge two engine windows and slice one page.

    Each input is the leading ``offset + limit + 1`` rows of that engine in
    this same order. A row in the global leading window is inside the leading
    window of its own engine, so the merged prefix is the global prefix.
    """
    merged = sorted([*agent_rows, *script_rows], key=execution_feed_sort_key)
    page_end = offset + limit
    return list(merged[offset:page_end]), len(merged) > page_end


def scrub_company_agent_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Drop credential and migration payloads from a company agent read."""
    cleaned = {
        key: value
        for key, value in dict(row).items()
        if key not in _COMPANY_AGENT_DROP_KEYS
    }
    for key in ("metadata", "delivery_config", "additional_context", "trigger_config"):
        if key in cleaned:
            cleaned[key] = _scrub_nested_value(cleaned[key])
    last = cleaned.get("last_execution")
    if isinstance(last, Mapping):
        last_clean = dict(last)
        if "delivery_result" in last_clean:
            last_clean["delivery_result"] = _scrub_nested_value(
                last_clean.get("delivery_result")
            )
        cleaned["last_execution"] = last_clean
    return cleaned


def sanitize_script_snapshot(raw_snapshot: Any) -> dict[str, Any]:
    """Keep script configuration fields and strip nested secrets."""
    if not isinstance(raw_snapshot, Mapping):
        return {}
    sanitized: dict[str, Any] = {}
    for field_name in ALLOWED_SCRIPT_SNAPSHOT_FIELDS:
        if field_name not in raw_snapshot:
            continue
        sanitized[field_name] = _scrub_nested_value(raw_snapshot[field_name])
    return sanitized


def sanitize_script_changes(raw_changes: Any) -> dict[str, AutomationFieldChangeResponse]:
    """Keep script configuration fields in a history ``changes`` object."""
    if not isinstance(raw_changes, Mapping):
        return {}
    sanitized: dict[str, AutomationFieldChangeResponse] = {}
    for field_name in ALLOWED_SCRIPT_SNAPSHOT_FIELDS:
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


def _adapt_data_task_to_automation_response(
    def_row: Mapping[str, Any],
    *,
    latest_run: Mapping[str, Any] | None = None,
    notify_routes: list[str] | None = None,
    notify_source: str | None = None,
) -> ScriptAutomationResponse:
    """Adapt one real definition row. Missing required values raise.

    ``name`` is the definition title. ``description`` stays null. Schedule,
    flags, versions, and the latest run come from the database, not the manifest.
    """
    _require_def_fields(def_row)
    job_key = str(def_row["job_key"])
    enabled = bool(def_row["enabled"])
    paused_by_user = bool(def_row["paused_by_user"])
    cron_expr = str(def_row["cron_expr"])
    timezone = str(def_row["timezone"])
    source = str(notify_source or def_row["notify_storage_mode"])
    routes = list(notify_routes or [])
    updated_by = def_row.get("updated_by")
    return ScriptAutomationResponse(
        automation_id=f"data:{job_key}",
        name=str(def_row["title"]),
        status=script_display_status(enabled=enabled, paused_by_user=paused_by_user),
        enabled=enabled,
        paused_by_user=paused_by_user,
        cron_expression=cron_expr,
        timezone=timezone,
        next_run_at=_script_next_run_at(
            enabled=enabled,
            paused_by_user=paused_by_user,
            cron_expr=cron_expr,
            timezone=timezone,
        ),
        last_run_at=_script_last_run_at(latest_run),
        config_version=int(def_row["config_version"]),
        active_version=int(def_row["active_version"]),
        updated_by=str(updated_by) if updated_by is not None else None,
        updated_at=_require_datetime(def_row["updated_at"], job_key, "updated_at"),
        notify_storage_mode=str(def_row["notify_storage_mode"]),
        dalton_notify_category="data_task",
        dalton_notify_effective=bool(def_row["notify_enabled"]),
        dalton_notify_source=source,
        dalton_notify_route_targets=routes,
        last_execution=_script_execution(job_key, latest_run),
        deleted_at=def_row.get("deleted_at"),
        deleted_by=(
            str(def_row["deleted_by"]) if def_row.get("deleted_by") is not None else None
        ),
    )


async def list_company_automations(
    principal: Principal,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_deleted: bool = False,
) -> AutomationsListResponse:
    """Merge company agents and manifest scripts, then sort and page once."""
    await require_company_scope_read_access(principal)
    keys = await _require_complete_manifest()
    agent_rows = await company_data_db.list_company_agent_rows(
        status=status,
        include_deleted=include_deleted,
    )
    def_rows = await company_data_db.list_active_manifest_def_rows(
        keys,
        include_deleted=include_deleted,
    )
    runs = await company_data_db.list_latest_script_runs(
        [str(row["job_key"]) for row in def_rows]
    )
    route_map = await _legacy_routes_by_job(def_rows)
    items: list[AutomationResponse | ScriptAutomationResponse] = [
        _company_agent_response(row) for row in agent_rows
    ]
    for def_row in def_rows:
        _require_def_fields(def_row)
        if not script_matches_list_status(
            status,
            enabled=bool(def_row["enabled"]),
            paused_by_user=bool(def_row["paused_by_user"]),
        ):
            continue
        job_key = str(def_row["job_key"])
        routes, source = _notify_view(def_row, route_map.get(job_key, []))
        items.append(
            _adapt_data_task_to_automation_response(
                def_row,
                latest_run=runs.get(job_key),
                notify_routes=routes,
                notify_source=source,
            )
        )
    ordered = sorted(items, key=company_catalog_sort_key)
    return AutomationsListResponse(
        automations=ordered[offset : offset + limit],
        total=len(ordered),
    )


async def get_script_automation(
    principal: Principal,
    job_key: str,
    *,
    allow_deleted: bool = False,
) -> ScriptAutomationResponse:
    """Read one manifest script. Unknown keys are 404; a missing row is 503."""
    await require_company_scope_read_access(principal)
    _require_known_manifest_key(job_key)
    def_row = await company_data_db.get_script_def_row(job_key)
    if def_row is None:
        raise CompanyDataTaskReadError(
            f"Company data task definition is missing: {job_key}"
        )
    if def_row.get("deleted_at") is not None and not allow_deleted:
        raise HTTPException(status_code=404, detail="Automation not found")
    runs = await company_data_db.list_latest_script_runs([job_key])
    route_map = await _legacy_routes_by_job([def_row])
    routes, source = _notify_view(def_row, route_map.get(job_key, []))
    return _adapt_data_task_to_automation_response(
        def_row,
        latest_run=runs.get(job_key),
        notify_routes=routes,
        notify_source=source,
    )


async def list_company_execution_feed(
    principal: Principal,
    *,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AutomationRunsListResponse:
    """Merge company agent executions and manifest script runs."""
    await require_company_scope_read_access(principal)
    keys = await _require_complete_manifest()
    return await _execution_page(
        agent_automation_id=None,
        script_job_key=None,
        script_job_keys=keys,
        include_agents=True,
        include_scripts=thread_id is None,
        thread_id=thread_id,
        status=status,
        limit=limit,
        offset=offset,
    )


async def list_script_executions(
    principal: Principal,
    job_key: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> AutomationRunsListResponse:
    """Read runs for one manifest script. A script run has no thread."""
    await require_company_scope_read_access(principal)
    _require_known_manifest_key(job_key)
    def_row = await company_data_db.get_script_def_row(job_key)
    if def_row is None:
        raise CompanyDataTaskReadError(
            f"Company data task definition is missing: {job_key}"
        )
    if def_row.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="Automation not found")
    return await _execution_page(
        agent_automation_id=None,
        script_job_key=job_key,
        script_job_keys=[job_key],
        include_agents=False,
        include_scripts=True,
        thread_id=None,
        status=None,
        limit=limit,
        offset=offset,
    )


async def list_company_agent_executions(
    principal: Principal,
    automation_id: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> AutomationRunsListResponse:
    """Read executions of one company agent. The caller has already passed the gate."""
    await require_company_scope_read_access(principal)
    return await _execution_page(
        agent_automation_id=automation_id,
        script_job_key=None,
        script_job_keys=[],
        include_agents=True,
        include_scripts=False,
        thread_id=None,
        status=None,
        limit=limit,
        offset=offset,
    )


async def get_script_modification_history(
    principal: Principal,
    job_key: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> AutomationHistoryResponse:
    """Read stored script history. An empty audit list stays empty."""
    await require_company_scope_read_access(principal)
    _require_known_manifest_key(job_key)
    row = await company_data_db.get_script_history_row(job_key)
    if row is None:
        raise CompanyDataTaskReadError(
            f"Company data task definition is missing: {job_key}"
        )
    if row.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="Automation not found")
    parsed_entries = _script_history_entries(row.get("audit_history"))
    parsed_entries.sort(key=lambda entry: entry.version, reverse=True)
    return AutomationHistoryResponse(
        automation_id=f"data:{job_key}",
        scope="company",
        engine_kind="script",
        config_version=int(row.get("config_version") or 1),
        active_version=int(row.get("active_version") or 1),
        entries=parsed_entries[offset : offset + limit],
        total=len(parsed_entries),
        limit=limit,
        offset=offset,
    )


def _company_agent_response(row: Mapping[str, Any]) -> AutomationResponse:
    from src.dalton.services.company_automations import apply_company_notify_representation

    resp = AutomationResponse.model_validate(scrub_company_agent_row(row))
    apply_company_notify_representation(resp, row)
    return resp


async def _execution_page(
    *,
    agent_automation_id: str | None,
    script_job_key: str | None,
    script_job_keys: list[str],
    include_agents: bool,
    include_scripts: bool,
    thread_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> AutomationRunsListResponse:
    fetch_limit = offset + limit + 1
    agent_rows: list[dict[str, Any]] = []
    script_rows: list[dict[str, Any]] = []
    if include_agents:
        agent_rows = await company_data_db.list_company_agent_execution_window(
            automation_id=agent_automation_id,
            thread_id=thread_id,
            status=status,
            fetch_limit=fetch_limit,
        )
    if include_scripts:
        script_rows = await company_data_db.list_script_execution_window(
            job_keys=script_job_keys,
            job_key=script_job_key,
            status=status,
            fetch_limit=fetch_limit,
        )
    page, has_more = merge_execution_windows(
        agent_rows,
        script_rows,
        offset=offset,
        limit=limit,
    )
    executions: list[AutomationRunResponse | ScriptRunResponse] = []
    for row in page:
        if str(row.get("engine_kind") or "") == "script":
            executions.append(_script_run_model(row))
        else:
            executions.append(_agent_run_model(row))
    return AutomationRunsListResponse(executions=executions, has_more=has_more)


def _agent_run_model(row: Mapping[str, Any]) -> AutomationRunResponse:
    payload = dict(row)
    payload["engine_kind"] = "agent"
    if "delivery_result" in payload:
        payload["delivery_result"] = _scrub_nested_value(payload.get("delivery_result"))
    return AutomationRunResponse.model_validate(payload)


def _script_run_model(row: Mapping[str, Any]) -> ScriptRunResponse:
    job_key = str(row.get("automation_id") or "")
    if job_key.startswith("data:"):
        job_key = job_key.split(":", 1)[1]
    execution = _script_execution_from_values(
        job_key,
        run_id=row.get("automation_execution_id"),
        status=row.get("status"),
        scheduled_at=row.get("scheduled_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        created_at=row.get("created_at"),
        exit_code=row.get("exit_code"),
        failure_reason=row.get("failure_reason"),
        notify_status=row.get("notify_status"),
        triggered_by_user_id=row.get("triggered_by_user_id"),
        log_tail=row.get("log_tail"),
        trigger_source=row.get("trigger_source"),
    )
    return ScriptRunResponse(
        automation_execution_id=execution.automation_execution_id,
        automation_id=execution.automation_id,
        automation_name=str(row.get("automation_name") or ""),
        status=execution.status,
        scheduled_at=execution.scheduled_at,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        created_at=execution.created_at,
        exit_code=execution.exit_code,
        failure_reason=execution.failure_reason,
        notify_status=execution.notify_status,
        triggered_by_user_id=execution.triggered_by_user_id,
        log_tail=execution.log_tail,
        trigger_source=execution.trigger_source,
    )


def _script_execution(
    job_key: str,
    run: Mapping[str, Any] | None,
) -> ScriptExecutionResponse | None:
    if not isinstance(run, Mapping) or not run:
        return None
    return _script_execution_from_values(
        job_key,
        run_id=run.get("run_id"),
        status=run.get("status"),
        scheduled_at=run.get("scheduled_slot_utc"),
        started_at=run.get("started_at"),
        completed_at=run.get("completed_at"),
        created_at=run.get("created_at"),
        exit_code=run.get("exit_code"),
        failure_reason=run.get("failure_reason"),
        notify_status=run.get("notify_status"),
        triggered_by_user_id=run.get("triggered_by_user_id"),
        log_tail=run.get("log_tail"),
        trigger_source=run.get("trigger_source"),
    )


def _script_execution_from_values(
    job_key: str,
    *,
    run_id: Any,
    status: Any,
    scheduled_at: Any,
    started_at: Any,
    completed_at: Any,
    created_at: Any,
    exit_code: Any,
    failure_reason: Any,
    notify_status: Any,
    triggered_by_user_id: Any,
    log_tail: Any,
    trigger_source: Any,
) -> ScriptExecutionResponse:
    if run_id is None or status is None or scheduled_at is None or created_at is None:
        raise CompanyDataTaskReadError(
            f"Company data task run is incomplete: {job_key}"
        )
    return ScriptExecutionResponse(
        automation_execution_id=run_id,
        automation_id=f"data:{job_key}",
        status=str(status),
        scheduled_at=_require_datetime(scheduled_at, job_key, "scheduled_slot_utc"),
        started_at=_optional_datetime(started_at),
        completed_at=_optional_datetime(completed_at),
        created_at=_require_datetime(created_at, job_key, "created_at"),
        exit_code=int(exit_code) if exit_code is not None else None,
        failure_reason=str(failure_reason) if failure_reason is not None else None,
        notify_status=str(notify_status) if notify_status is not None else None,
        triggered_by_user_id=(
            str(triggered_by_user_id) if triggered_by_user_id is not None else None
        ),
        log_tail=_bounded_log_tail(log_tail),
        trigger_source=str(trigger_source) if trigger_source is not None else None,
    )


def _script_history_entries(raw_history: Any) -> list[AutomationHistoryEntryResponse]:
    if not isinstance(raw_history, list):
        return []
    entries: list[AutomationHistoryEntryResponse] = []
    for item in raw_history:
        parsed = _parse_script_history_entry(item)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_script_history_entry(raw_entry: Any) -> AutomationHistoryEntryResponse | None:
    if not isinstance(raw_entry, Mapping):
        return None
    try:
        version = int(raw_entry.get("version"))
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None

    def _opt_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    changed_raw = raw_entry.get("changed_at")
    if hasattr(changed_raw, "isoformat"):
        changed_at: str | None = changed_raw.isoformat()
    elif changed_raw is not None:
        changed_at = str(changed_raw)
    else:
        changed_at = None
    actor_raw = raw_entry.get("actor_user_id")
    return AutomationHistoryEntryResponse(
        version=version,
        prev_version=_opt_int(raw_entry.get("prev_version")),
        restored_from_version=_opt_int(raw_entry.get("restored_from_version")),
        actor_user_id=str(actor_raw) if actor_raw is not None else None,
        changed_at=changed_at,
        action=str(raw_entry.get("action") or "update"),
        changes=sanitize_script_changes(raw_entry.get("changes")),
        snapshot=sanitize_script_snapshot(raw_entry.get("snapshot")),
    )


def load_manifest_job_keys() -> list[str]:
    from src.dalton.data_jobs.manifest import load_manifest_entries

    keys = [entry.job_key for entry in load_manifest_entries()]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Data task manifest has duplicate job keys")
    return keys


def _require_known_manifest_key(job_key: str) -> None:
    if job_key not in set(load_manifest_job_keys()):
        raise HTTPException(status_code=404, detail="Automation not found")


async def _require_complete_manifest() -> list[str]:
    keys = load_manifest_job_keys()
    found = await company_data_db.find_present_job_keys(keys)
    missing = sorted(set(keys) - found)
    if missing:
        raise CompanyDataTaskReadError(
            "Company data task definitions are incomplete: " + ", ".join(missing)
        )
    return keys


def _require_def_fields(def_row: Mapping[str, Any]) -> None:
    missing = [
        key
        for key in _DEF_REQUIRED_FIELDS
        if key not in def_row or def_row[key] is None
    ]
    if missing:
        job_key = str(def_row.get("job_key") or "")
        label = job_key or "unknown"
        raise CompanyDataTaskReadError(
            "Company data task definition is incomplete: "
            f"{label} missing {', '.join(missing)}"
        )
    mode = str(def_row.get("notify_storage_mode") or "")
    if mode not in ("legacy", "task_row"):
        raise CompanyDataTaskReadError(
            "Company data task definition is incomplete: "
            f"{def_row.get('job_key')} notify_storage_mode"
        )


def _notify_view(
    def_row: Mapping[str, Any],
    legacy_routes: list[str],
) -> tuple[list[str], str]:
    mode = str(def_row["notify_storage_mode"])
    if mode == "task_row":
        return _clean_routes(def_row.get("notify_routes")), "task_row"
    return list(legacy_routes), "legacy"


async def _legacy_routes_by_job(def_rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    from src.dalton.settings import get_configured_pm_user_ids

    pm_ids = get_configured_pm_user_ids()
    owners: dict[str, str] = {}
    for row in def_rows:
        if str(row.get("notify_storage_mode") or "") == "task_row":
            continue
        owner = _legacy_notify_owner(row, pm_ids)
        if owner:
            owners[str(row["job_key"])] = owner
    if not owners:
        return {}
    prefs = await company_data_db.read_user_model_preferences(sorted(set(owners.values())))
    routes: dict[str, list[str]] = {}
    for job_key, owner in owners.items():
        if owner not in prefs:
            routes[job_key] = []
            continue
        routes[job_key] = _routes_for_job(_model_pref_mapping(prefs[owner]), job_key)
    return routes


def _legacy_notify_owner(row: Mapping[str, Any], pm_ids: Sequence[str]) -> str | None:
    state = _mapping_or_json(row.get("migration_state"), "migration_state")
    raw_owner = state.get("legacy_notify_owner_user_id")
    if isinstance(raw_owner, str) and raw_owner.strip():
        return raw_owner.strip()
    if pm_ids:
        return str(pm_ids[0])
    return None


def _routes_for_job(model_pref: Mapping[str, Any], job_key: str) -> list[str]:
    from src.dalton.autonomy.system_jobs import _extract_data_routes_from_model_pref

    return list(_extract_data_routes_from_model_pref(model_pref).get(job_key, []))


def _model_pref_mapping(raw: Any) -> Mapping[str, Any]:
    return _mapping_or_json(raw, "model_preference")


def _mapping_or_json(raw: Any, label: str) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} is not valid JSON") from exc
    if isinstance(raw, Mapping):
        return raw
    raise RuntimeError(f"{label} is not an object")


def _clean_routes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = _mapping_or_json_list(raw)
    if not isinstance(raw, list):
        raise RuntimeError("notify_routes is not a list")
    return [str(item).strip() for item in raw if isinstance(item, str) and str(item).strip()]


def _mapping_or_json_list(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("notify_routes is not valid JSON") from exc


def _script_next_run_at(
    *,
    enabled: bool,
    paused_by_user: bool,
    cron_expr: str,
    timezone: str,
) -> datetime | None:
    if not enabled or paused_by_user:
        return None
    from src.dalton.autonomy.system_jobs import _compute_next_data_task_cron_iso

    iso_value = _compute_next_data_task_cron_iso(cron_expr, timezone)
    return _optional_datetime(iso_value)


def _script_last_run_at(run: Mapping[str, Any] | None) -> datetime | None:
    if not isinstance(run, Mapping):
        return None
    for key in ("completed_at", "started_at", "created_at", "scheduled_slot_utc"):
        parsed = _optional_datetime(run.get(key))
        if parsed is not None:
            return parsed
    return None


def _bounded_log_tail(raw: Any) -> str | None:
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    from src.dalton.data_jobs.container_runner import truncate_log_tail

    return truncate_log_tail(text)


def _require_datetime(value: Any, job_key: str, field_name: str) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise CompanyDataTaskReadError(
            f"Company data task definition is incomplete: {job_key} missing {field_name}"
        )
    return parsed


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _aware_epoch(value: Any) -> float:
    if not isinstance(value, datetime):
        return float("-inf")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()
