"""Dedicated earnings tracker calendar service: official MCP mapping, legacy bridge relay, and disconnect purge (§4.5(2), §5.3).

Never logs OAuth tokens, bridge tokens, calendar IDs, or event contents.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from psycopg.rows import dict_row

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.database import user_integrations as integrations_db
from src.dalton.database.automation_reports import (
    _clean_identity,
    _early_identity_code,
    _identity_code,
    lock_report_execution,
)
from src.dalton.database.user_integrations import (
    PurgeLedgerRow,
    TriggerReason,
)
from src.dalton.deployment_credentials.catalog import (
    GOOGLE_OAUTH_CLIENT_ID_KEY,
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
)
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.constants import (
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_CALENDAR_WRITE_SCOPES,
    is_google_calendar_write_scope,
    scope_satisfied,
)
from src.dalton.integrations.google_calendar import (
    GoogleCalendarAPIError,
    GoogleCalendarClientProtocol,
    GoogleMCPElicitationRequired,
    build_google_list_events_args,
    format_local_datetime_for_tz,
    get_google_calendar_client,
)
from src.dalton.integrations.lock_guard import assert_no_advisory_lock_held
from src.dalton.integrations.oauth_clients import (
    GoogleOAuthClientProtocol,
    get_google_oauth_client,
)
from src.dalton.services.integration_data_purge import (
    register_tracker_purge_handler,
)
from src.dalton.services.user_integrations_oauth import (
    PersonalIntegrationError,
    ensure_fresh_access_token,
)
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_tracker_calendar_automation_id,
    get_tracker_calendar_owner_id,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

TRACKER_DEFAULT_TIMEZONE = "America/New_York"
TRACKER_ALLOWED_OPERATIONS: frozenset[str] = frozenset(
    {"list_events", "insert_event", "delete_event", "health_check"}
)
TRACKER_WRITE_OPERATIONS: frozenset[str] = frozenset({"insert_event", "delete_event"})

TRACKER_WRITE_SCOPE_MISSING_CODE = "tracker_calendar_write_scope_missing"
TRACKER_WRITE_SCOPE_MISSING_MESSAGE = (
    "财报日历写入需要日历写权限，会随第 2 批开放；当前请保持 legacy_gws"
)

TRACKER_GCAL_MARKER = "Created by Dalton earnings tracker"
TRACKER_GCAL_OLD_MARKERS: tuple[str, ...] = ("Scanned ", "扫描日期：")
TRACKER_GCAL_SUMMARY_MAX = 300
TRACKER_GCAL_DESCRIPTION_MAX = 8000
TRACKER_GCAL_DEFAULT_LIMIT = 50
TRACKER_GCAL_MAX_LIMIT = 250
TRACKER_GCAL_MAX_WINDOW = timedelta(days=400)

TRACKER_GCAL_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TRACKER_GCAL_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,128}$")
TRACKER_GCAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRACKER_GCAL_TRACKER_SUMMARY_RE = re.compile(r"^.+ \([A-Z0-9.]+\) Earnings — ")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_COMMON_KEYS = frozenset({"route", "operation", "timeout", "mode"})
_OP_KEYS: dict[str, frozenset[str]] = {
    "list_events": frozenset({"query", "time_min", "time_max", "limit"}),
    "insert_event": frozenset({"summary", "description", "start", "end"}),
    "delete_event": frozenset({"event_id"}),
    "health_check": frozenset(),
}

_HINT_WINDOW = "time_min 到 time_max 的窗口不能超过 400 天。"
_HINT_ORDER = "time_min 必须早于 time_max。"
_HINT_TIME = "time_min 和 time_max 需为 YYYY-MM-DD 或带时区的 ISO 时间。"
_HINT_SHAPE = 'start 和 end 只能是 {"date": "YYYY-MM-DD"} 或 {"dateTime", "timeZone"}。'
_HINT_WHEN_ORDER = "end 必须晚于 start。"
_HINT_REFUSED = "该事件不是财报跟踪事件，已拒绝删除。"
_HINT_NOT_FOUND = "日历里没有这个事件。"
_HINT_QUERY = "query 只能是单个 ticker 或短串。"
_HINT_LIMIT = "limit 只能是 1 到 250 的整数。"
_HINT_SUMMARY = "summary 只能是 1 到 300 个字符的字符串。"
_HINT_SUMMARY_FORMAT = "summary 必须是「名称 (代码) Earnings — 」这种财报跟踪标题。"
_HINT_DESCRIPTION = "description 只能是不超过 8000 个字符的字符串。"
_HINT_EVENT_ID = "event_id 只能是 5 到 128 位字母、数字、下划线或连字符。"
_HINT_CALENDAR_UNCONFIGURED = "财报跟踪子日历未配置。"

LEGACY_GWS_BRIDGE_PATH = "/internal/tracker-calendar/legacy-gws"
LEGACY_GWS_MAX_TIMEOUT_S = 25.0


# ---------------------------------------------------------------------------
# Shared gcal contract helpers (parity with researchdata_bridge._gcal_*)
# ---------------------------------------------------------------------------


def _gcal_failure(
    status: str,
    error_code: str,
    hint: str,
    category: str,
    *,
    error: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "error_code": error_code,
        "error_category": category,
        "error": error or hint,
        "hint": hint,
        "_gcal_hint": hint,
        "data": data,
    }


def _gcal_param_error(hint: str, *, error_code: str = "invalid_arguments") -> dict[str, Any]:
    return _gcal_failure("invalid_arguments", error_code, hint, "参数错误")


def with_tracker_marker(description: str) -> str:
    """Append the canonical Dalton earnings tracker marker line if absent."""
    text = description.rstrip()
    lines = text.splitlines()
    if lines and lines[-1].strip() == TRACKER_GCAL_MARKER:
        return text
    if not text:
        return TRACKER_GCAL_MARKER
    return f"{text}\n{TRACKER_GCAL_MARKER}"


def is_tracker_event(event: Mapping[str, Any]) -> bool:
    """Return True iff ``event`` matches Dalton's managed earnings tracker signature (§5.3)."""
    summary = event.get("summary") if isinstance(event.get("summary"), str) else ""
    description = event.get("description") if isinstance(event.get("description"), str) else ""
    if TRACKER_GCAL_TRACKER_SUMMARY_RE.match(summary) is None:
        return False
    if TRACKER_GCAL_MARKER in description:
        return True
    return any(marker in description for marker in TRACKER_GCAL_OLD_MARKERS)


def _copy_when(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    if isinstance(value.get("date"), str):
        return {"date": value["date"]}
    copied: dict[str, str] = {}
    if isinstance(value.get("dateTime"), str):
        copied["dateTime"] = value["dateTime"]
    if isinstance(value.get("timeZone"), str):
        copied["timeZone"] = value["timeZone"]
    return copied or None


def normalize_tracker_event(raw: Any) -> dict[str, Any] | None:
    """Normalize an official Calendar MCP event to the ``route='gcal'`` 5-field contract."""
    if not isinstance(raw, Mapping):
        return None
    event_id = raw.get("id") or raw.get("eventId")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    summary = raw.get("summary") if isinstance(raw.get("summary"), str) else ""
    description = raw.get("description") if isinstance(raw.get("description"), str) else ""
    start = _copy_when(raw.get("start"))
    if start is None and isinstance(raw.get("startTime"), str):
        st = raw["startTime"]
        if TRACKER_GCAL_DATE_RE.fullmatch(st):
            start = {"date": st}
        else:
            start = {"dateTime": st}
            if isinstance(raw.get("timeZone"), str) and raw["timeZone"].strip():
                start["timeZone"] = raw["timeZone"].strip()
    end = _copy_when(raw.get("end"))
    if end is None and isinstance(raw.get("endTime"), str):
        et = raw["endTime"]
        if TRACKER_GCAL_DATE_RE.fullmatch(et):
            end = {"date": et}
        else:
            end = {"dateTime": et}
            if isinstance(raw.get("timeZone"), str) and raw["timeZone"].strip():
                end["timeZone"] = raw["timeZone"].strip()
    return {
        "id": event_id.strip(),
        "summary": summary,
        "description": description,
        "start": start,
        "end": end,
    }


def parse_scope_tokens(scopes: str | Collection[str] | None) -> list[str]:
    if not scopes:
        return []
    if isinstance(scopes, str):
        raw_items = scopes.replace(",", " ").split()
    else:
        raw_items = [str(s) for s in scopes]
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def has_tracker_write_scope(scopes: str | Collection[str] | None) -> bool:
    """Return True if granted scopes include Google Calendar write permission."""
    tokens = parse_scope_tokens(scopes)
    return any(
        s in GOOGLE_CALENDAR_WRITE_SCOPES or is_google_calendar_write_scope(s)
        for s in tokens
    )


def has_tracker_read_scope(scopes: str | Collection[str] | None) -> bool:
    """Return True if granted scopes satisfy minimum read-only Google Calendar access."""
    tokens = parse_scope_tokens(scopes)
    return all(scope_satisfied(req, tokens) for req in GOOGLE_CALENDAR_MIN_SCOPES)


def _reject_extras(operation: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    allowed = _COMMON_KEYS | _OP_KEYS.get(operation, frozenset())
    unknown = sorted(
        key for key, value in arguments.items() if value is not None and key not in allowed
    )
    if not unknown:
        return None
    return _gcal_param_error(f"本操作不接受参数 {unknown}。", error_code="unknown_parameters")


def _parse_bound(value: Any) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise ValueError("bad time")
    text = value.strip()
    if TRACKER_GCAL_DATE_RE.fullmatch(text):
        parsed = datetime.fromisoformat(text).replace(tzinfo=UTC)
        return parsed, f"{text}T00:00:00Z"
    probe = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(probe)
    except ValueError as exc:
        raise ValueError("bad time") from exc
    if parsed.tzinfo is None:
        raise ValueError("naive")
    return parsed.astimezone(UTC), text


def _parse_limit(value: Any) -> int:
    if value is None:
        return TRACKER_GCAL_DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("bad limit")
    if value < 1 or value > TRACKER_GCAL_MAX_LIMIT:
        raise ValueError("bad limit")
    return value


def _parse_when(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("shape")
    keys = set(value.keys())
    if keys == {"date"}:
        day = value.get("date")
        if not isinstance(day, str) or TRACKER_GCAL_DATE_RE.fullmatch(day) is None:
            raise ValueError("shape")
        date.fromisoformat(day)
        return {"date": day}
    if keys == {"dateTime", "timeZone"}:
        stamp = value.get("dateTime")
        zone = value.get("timeZone")
        if not isinstance(stamp, str) or not stamp.strip():
            raise ValueError("shape")
        if not isinstance(zone, str) or not zone.strip():
            raise ValueError("shape")
        probe = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
        try:
            datetime.fromisoformat(probe)
            ZoneInfo(zone)
        except (KeyError, ValueError) as exc:
            raise ValueError("shape") from exc
        return {"dateTime": stamp, "timeZone": zone}
    raise ValueError("shape")


def _when_stamp(value: Mapping[str, str]) -> datetime:
    stamp = value["dateTime"]
    probe = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    parsed = datetime.fromisoformat(probe)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(value["timeZone"]))
    return parsed


def _when_before(start: Mapping[str, str], end: Mapping[str, str]) -> bool:
    if "date" in start or "date" in end:
        if "date" not in start or "date" not in end:
            raise ValueError("shape")
        return date.fromisoformat(start["date"]) < date.fromisoformat(end["date"])
    return _when_stamp(start) < _when_stamp(end)


# ---------------------------------------------------------------------------
# PostgreSQL automation execution authorization (§5.3(3))
# ---------------------------------------------------------------------------


async def authorize_tracker_calendar_execution(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    expected_automation_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Verify the 6-tuple caller identity matches a live ``running`` tracker automation execution."""
    identity = _clean_identity(
        user_id=user_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        automation_id=automation_id,
        execution_id=execution_id,
    )
    early = _early_identity_code(identity)
    if early:
        return {"ok": False, "code": early}
    if not _UUID_RE.fullmatch(identity["automation_id"]):
        return {"ok": False, "code": "not_automation_run"}

    configured_auto_id = (
        expected_automation_id
        if expected_automation_id is not None
        else get_tracker_calendar_automation_id()
    )
    if configured_auto_id and identity["automation_id"].lower() != configured_auto_id.lower():
        return {"ok": False, "code": "automation_mismatch"}

    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                row = await lock_report_execution(
                    cur,
                    identity["execution_id"],
                    for_update=False,
                )
                code = _identity_code(row, identity)
                if code:
                    return {"ok": False, "code": code}
                assert row is not None
                row_auto = str(row.get("automation_id") or "").strip().lower()
                if configured_auto_id and row_auto != configured_auto_id.lower():
                    return {"ok": False, "code": "automation_mismatch"}
                return {"ok": True, "code": "ok"}


# ---------------------------------------------------------------------------
# Official Google Calendar MCP executor (§5.3)
# ---------------------------------------------------------------------------


def _extract_mcp_events(raw: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str | None]:
    events = raw.get("events")
    if not isinstance(events, list):
        events = raw.get("items")
    if not isinstance(events, list):
        events = []
    next_token = raw.get("nextPageToken")
    next_str = str(next_token).strip() if isinstance(next_token, str) and next_token.strip() else None
    return [ev for ev in events if isinstance(ev, Mapping)], next_str


def _extract_mcp_single_event(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if isinstance(raw.get("event"), Mapping):
        return raw["event"]
    if isinstance(raw.get("data"), Mapping) and (
        raw["data"].get("id") or raw["data"].get("eventId")
    ):
        return raw["data"]
    if raw.get("id") or raw.get("eventId"):
        return raw
    return None


async def execute_tracker_calendar_official_mcp(
    *,
    owner_id: str,
    operation: str,
    arguments: Mapping[str, Any],
    google_client: GoogleCalendarClientProtocol | None = None,
    oauth_client: GoogleOAuthClientProtocol | None = None,
    now_et: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Execute one tracker calendar operation via Google official Calendar MCP.

    Returns ``(payload, calendar_id)`` where ``payload`` matches ``_gcal_run``'s contract.
    """
    if operation not in TRACKER_ALLOWED_OPERATIONS:
        return (
            _gcal_param_error(
                "gcal 只提供 list_events、insert_event、delete_event、health_check。",
                error_code="invalid_operation",
            ),
            "",
        )

    extra_err = _reject_extras(operation, arguments)
    if extra_err is not None:
        return extra_err, ""

    # Pre-check owner connection & granted scopes BEFORE any token refresh or upstream call.
    conn_row = await integrations_db.get_connection(
        owner_id, "google", "calendar", decrypt_tokens=False
    )
    if conn_row is None:
        if operation == "health_check":
            return (
                _gcal_failure(
                    "not_connected",
                    "tracker_calendar_not_connected",
                    "财报属主尚未连接 Google 日历。",
                    "权限或额度",
                    data={"calendar_ok": False},
                ),
                "",
            )
        return (
            _gcal_failure(
                "not_connected",
                "tracker_calendar_not_connected",
                "财报属主尚未连接 Google 日历。",
                "权限或额度",
            ),
            "",
        )

    prefs = dict(conn_row.preferences or {})
    calendar_id = (
        str(prefs.get("tracker_calendar_id") or "").strip()
        or os.getenv("DALTON_GCAL_CALENDAR_ID", "").strip()
    )
    tz_name = str(prefs.get("tracker_calendar_time_zone") or "").strip() or TRACKER_DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except Exception:
        tz_name = TRACKER_DEFAULT_TIMEZONE

    # Decision #2: write operations under official_mcp MUST be rejected at entry when
    # the owner's granted scopes do not include Google Calendar write permission.
    if operation in TRACKER_WRITE_OPERATIONS and not has_tracker_write_scope(
        conn_row.granted_scopes
    ):
        return (
            _gcal_failure(
                TRACKER_WRITE_SCOPE_MISSING_CODE,
                TRACKER_WRITE_SCOPE_MISSING_CODE,
                TRACKER_WRITE_SCOPE_MISSING_MESSAGE,
                "权限或额度",
                error=TRACKER_WRITE_SCOPE_MISSING_MESSAGE,
            ),
            calendar_id,
        )

    if not calendar_id:
        if operation == "health_check":
            return (
                _gcal_failure(
                    "source_missing",
                    "source_missing",
                    _HINT_CALENDAR_UNCONFIGURED,
                    "上游不可用",
                    data={"calendar_ok": False},
                ),
                "",
            )
        return (
            _gcal_failure("source_missing", "source_missing", _HINT_CALENDAR_UNCONFIGURED, "上游不可用"),
            "",
        )

    # Validate operation arguments before acquiring/refreshing tokens.
    validated = _validate_operation_arguments(operation, arguments)
    if validated.get("ok") is not True:
        return validated["error_payload"], calendar_id

    try:
        fresh = await ensure_fresh_access_token(
            owner_id,
            "google",
            "calendar",
            required_scopes=GOOGLE_CALENDAR_MIN_SCOPES,
            google_client=oauth_client,
        )
    except PersonalIntegrationError as exc:
        if operation == "health_check":
            return (
                _gcal_failure(
                    exc.code,
                    exc.code,
                    str(exc),
                    "权限或额度",
                    data={"calendar_ok": False},
                ),
                calendar_id,
            )
        return _gcal_failure(exc.code, exc.code, str(exc), "权限或额度"), calendar_id

    # Re-verify write scope after potential refresh (in case scope narrowed on refresh).
    if operation in TRACKER_WRITE_OPERATIONS and not has_tracker_write_scope(
        fresh.granted_scopes
    ):
        return (
            _gcal_failure(
                TRACKER_WRITE_SCOPE_MISSING_CODE,
                TRACKER_WRITE_SCOPE_MISSING_CODE,
                TRACKER_WRITE_SCOPE_MISSING_MESSAGE,
                "权限或额度",
                error=TRACKER_WRITE_SCOPE_MISSING_MESSAGE,
            ),
            calendar_id,
        )

    client = google_client or get_google_calendar_client()

    try:
        if operation == "health_check":
            ref_now = now_et or datetime.now(ZoneInfo(tz_name))
            if ref_now.tzinfo is None:
                ref_now = ref_now.replace(tzinfo=ZoneInfo(tz_name))
            else:
                ref_now = ref_now.astimezone(ZoneInfo(tz_name))
            window_end = ref_now + timedelta(hours=24)
            mcp_args = build_google_list_events_args(
                calendar_id=calendar_id,
                time_min=ref_now.isoformat(),
                time_max=window_end.isoformat(),
                tz_name=tz_name,
                limit=1,
            )
            await client.call_mcp_tool(
                access_token=fresh.access_token,
                tool_name="list_events",
                arguments=mcp_args,
            )
            return {"ok": True, "status": "ok", "data": {"calendar_ok": True}}, calendar_id

        if operation == "list_events":
            limit = int(validated["limit"])
            query = validated["query"]
            collected: list[dict[str, Any]] = []
            page_token: str | None = None
            truncated = False
            max_pages = 10
            for _ in range(max_pages):
                remaining = max(1, limit - len(collected))
                mcp_args = build_google_list_events_args(
                    calendar_id=calendar_id,
                    time_min=validated["time_min_raw"],
                    time_max=validated["time_max_raw"],
                    tz_name=tz_name,
                    query=query,
                    limit=remaining,
                    page_token=page_token,
                )
                raw_res = await client.call_mcp_tool(
                    access_token=fresh.access_token,
                    tool_name="list_events",
                    arguments=mcp_args,
                )
                raw_events, next_token = _extract_mcp_events(raw_res)
                for item in raw_events:
                    norm = normalize_tracker_event(item)
                    if norm is not None:
                        collected.append(norm)
                        if len(collected) >= limit:
                            break
                if len(collected) >= limit:
                    if next_token or len(raw_events) > remaining:
                        truncated = True
                    break
                if not next_token:
                    break
                page_token = next_token
            else:
                if page_token:
                    truncated = True

            data: dict[str, Any] = {
                "events": collected[:limit],
                "event_count": len(collected[:limit]),
            }
            if truncated:
                data["truncated"] = True
            return {"ok": True, "status": "ok", "data": data}, calendar_id

        if operation == "insert_event":
            start_obj: dict[str, str] = validated["start"]
            end_obj: dict[str, str] = validated["end"]
            marked_desc = with_tracker_marker(validated["description"])
            create_args: dict[str, Any] = {
                "calendarId": calendar_id,
                "summary": validated["summary"],
                "description": marked_desc,
            }
            if "date" in start_obj:
                create_args["startTime"] = start_obj["date"]
                create_args["endTime"] = end_obj["date"]
                create_args["allDay"] = True
            else:
                ev_tz = start_obj["timeZone"]
                create_args["startTime"] = format_local_datetime_for_tz(
                    start_obj["dateTime"], ev_tz
                )
                create_args["endTime"] = format_local_datetime_for_tz(
                    end_obj["dateTime"], ev_tz
                )
                create_args["timeZone"] = ev_tz
                create_args["allDay"] = False

            raw_created = await client.call_mcp_tool(
                access_token=fresh.access_token,
                tool_name="create_event",
                arguments=create_args,
            )
            single = _extract_mcp_single_event(raw_created)
            event = normalize_tracker_event(single)
            if event is None:
                return (
                    _gcal_failure(
                        "invalid_response",
                        "invalid_response",
                        "Google Calendar MCP 返回了无效事件结构。",
                        "上游不可用",
                    ),
                    calendar_id,
                )
            if TRACKER_GCAL_MARKER not in event["description"]:
                event["description"] = with_tracker_marker(event["description"])
            return {"ok": True, "status": "ok", "data": event}, calendar_id

        # operation == "delete_event"
        event_id: str = validated["event_id"]
        try:
            raw_existing = await client.call_mcp_tool(
                access_token=fresh.access_token,
                tool_name="get_event",
                arguments={"calendarId": calendar_id, "eventId": event_id},
            )
        except GoogleCalendarAPIError as exc:
            if exc.status_code in (404, 410) or str(exc.error_code) in ("404", "410", "not_found"):
                return (
                    _gcal_failure(
                        "event_not_found",
                        "event_not_found",
                        _HINT_NOT_FOUND,
                        "没有匹配",
                    ),
                    calendar_id,
                )
            raise

        existing = _extract_mcp_single_event(raw_existing)
        if existing is None or not is_tracker_event(existing):
            return (
                _gcal_failure(
                    "refused_not_tracker_event",
                    "refused_not_tracker_event",
                    _HINT_REFUSED,
                    "没有匹配",
                ),
                calendar_id,
            )

        await client.call_mcp_tool(
            access_token=fresh.access_token,
            tool_name="delete_event",
            arguments={
                "calendarId": calendar_id,
                "eventId": event_id,
                "notificationLevel": "NONE",
            },
        )
        return {"ok": True, "status": "ok", "data": {"deleted": True, "id": event_id}}, calendar_id

    except GoogleMCPElicitationRequired:
        return (
            _gcal_failure(
                "elicitation_required",
                "elicitation_required",
                "Google Calendar MCP 要求交互确认，无人值守通道无法完成。",
                "权限或额度",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            calendar_id,
        )
    except GoogleCalendarAPIError as exc:
        if exc.status_code in (404, 410):
            status = "event_not_found" if operation == "delete_event" else "upstream_error"
            hint = _HINT_NOT_FOUND if operation == "delete_event" else "Google Calendar MCP 调用失败。"
            return (
                _gcal_failure(
                    status,
                    status,
                    hint,
                    "没有匹配" if operation == "delete_event" else "上游不可用",
                    data={"calendar_ok": False} if operation == "health_check" else None,
                ),
                calendar_id,
            )
        if exc.status_code in (401, 403):
            return (
                _gcal_failure(
                    "not_authorized",
                    "not_authorized",
                    "Google Calendar MCP 鉴权或权限不足。",
                    "权限或额度",
                    data={"calendar_ok": False} if operation == "health_check" else None,
                ),
                calendar_id,
            )
        return (
            _gcal_failure(
                "upstream_error",
                "upstream_error",
                "Google Calendar MCP 调用失败。",
                "上游不可用",
                error=type(exc).__name__,
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            calendar_id,
        )


def _validate_operation_arguments(
    operation: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    if operation == "health_check":
        return {"ok": True}

    if operation == "list_events":
        if arguments.get("time_min") is None or arguments.get("time_max") is None:
            return {"ok": False, "error_payload": _gcal_param_error("list_events 需要 time_min、time_max。")}
        try:
            start_dt, start_text = _parse_bound(arguments.get("time_min"))
            end_dt, end_text = _parse_bound(arguments.get("time_max"))
        except ValueError:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_TIME)}
        if end_dt <= start_dt:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_ORDER)}
        if end_dt - start_dt > TRACKER_GCAL_MAX_WINDOW:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_WINDOW)}
        query = arguments.get("query")
        if query is not None and (
            not isinstance(query, str) or TRACKER_GCAL_QUERY_RE.fullmatch(query) is None
        ):
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_QUERY)}
        try:
            limit = _parse_limit(arguments.get("limit"))
        except ValueError:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_LIMIT)}
        return {
            "ok": True,
            "time_min_raw": start_text,
            "time_max_raw": end_text,
            "query": query if isinstance(query, str) else None,
            "limit": limit,
        }

    if operation == "insert_event":
        for req in ("summary", "description", "start", "end"):
            if arguments.get(req) is None:
                return {
                    "ok": False,
                    "error_payload": _gcal_param_error("insert_event 需要 summary、description、start、end。"),
                }
        summary = arguments.get("summary")
        description = arguments.get("description")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > TRACKER_GCAL_SUMMARY_MAX
        ):
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_SUMMARY)}
        if TRACKER_GCAL_TRACKER_SUMMARY_RE.match(summary.strip()) is None:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_SUMMARY_FORMAT)}
        if not isinstance(description, str) or len(description) > TRACKER_GCAL_DESCRIPTION_MAX:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_DESCRIPTION)}
        try:
            start = _parse_when(arguments.get("start"))
            end = _parse_when(arguments.get("end"))
            ordered = _when_before(start, end)
        except ValueError:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_SHAPE)}
        if not ordered:
            return {"ok": False, "error_payload": _gcal_param_error(_HINT_WHEN_ORDER)}
        return {
            "ok": True,
            "summary": summary.strip(),
            "description": description,
            "start": start,
            "end": end,
        }

    # delete_event
    event_id = arguments.get("event_id")
    if event_id is None:
        return {"ok": False, "error_payload": _gcal_param_error("delete_event 需要 event_id。")}
    if not isinstance(event_id, str) or TRACKER_GCAL_EVENT_ID_RE.fullmatch(event_id) is None:
        return {"ok": False, "error_payload": _gcal_param_error(_HINT_EVENT_ID)}
    return {"ok": True, "event_id": event_id}


# ---------------------------------------------------------------------------
# Legacy GWS bridge callback executor (§5.3)
# ---------------------------------------------------------------------------


async def execute_tracker_calendar_legacy_gws(
    *,
    operation: str,
    arguments: Mapping[str, Any],
    eff_timeout: float = 25.0,
    http_client: Any = None,
) -> tuple[dict[str, Any], str]:
    """Forward a tracker calendar call back to the host bridge's ``legacy-gws`` endpoint."""
    assert_no_advisory_lock_held("tracker_calendar.legacy_gws")
    if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
        return (
            _gcal_failure(
                "capability_disabled",
                "capability_disabled",
                "研究数据桥能力未启用。",
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )
    base_url = get_dalton_bridge_base_url().rstrip("/")
    token = get_dalton_bridge_admin_token()
    if not base_url or not token:
        return (
            _gcal_failure(
                "bridge_not_configured",
                "bridge_not_configured",
                "DALTON_BRIDGE_URL 或 DALTON_BRIDGE_ADMIN_TOKEN 未配置。",
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )

    clamped_timeout = min(max(float(eff_timeout or 25.0), 1.0), LEGACY_GWS_MAX_TIMEOUT_S)
    url = f"{base_url}{LEGACY_GWS_BRIDGE_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Dalton-Bridge-Token": token,
    }
    body = {
        "operation": operation,
        "arguments": dict(arguments),
        "timeout_s": clamped_timeout,
    }
    timeout_cfg = httpx.Timeout(
        connect=min(5.0, clamped_timeout),
        read=clamped_timeout,
        write=clamped_timeout,
        pool=min(5.0, clamped_timeout),
    )

    try:
        if http_client is not None:
            resp = await http_client.post(url, json=body, headers=headers, timeout=timeout_cfg)
        else:
            async with httpx.AsyncClient(timeout=timeout_cfg, trust_env=False) as owned:
                resp = await owned.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        return (
            _gcal_failure(
                "timeout",
                "timeout",
                "宿主机 legacy_gws 桥接调用超时。",
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )
    except httpx.HTTPError as exc:
        return (
            _gcal_failure(
                "upstream_error",
                "upstream_error",
                "无法连接宿主机 legacy_gws 桥接路由。",
                "上游不可用",
                error=type(exc).__name__,
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )

    try:
        parsed = resp.json()
    except ValueError:
        return (
            _gcal_failure(
                "invalid_response",
                "invalid_response",
                "宿主机 legacy_gws 桥接返回了非 JSON 响应。",
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )

    if resp.status_code >= 400 or not isinstance(parsed, Mapping):
        err_msg = (
            str(parsed.get("error") or "legacy_gws 桥接返回错误。")
            if isinstance(parsed, Mapping)
            else "legacy_gws 桥接返回错误。"
        )
        return (
            _gcal_failure(
                "upstream_error",
                "upstream_error",
                err_msg,
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            "",
        )

    payload = parsed.get("payload")
    calendar_id = str(parsed.get("calendar_id") or "").strip()
    if not isinstance(payload, Mapping):
        return (
            _gcal_failure(
                "invalid_response",
                "invalid_response",
                "宿主机 legacy_gws 桥接缺少 payload。",
                "上游不可用",
                data={"calendar_ok": False} if operation == "health_check" else None,
            ),
            calendar_id,
        )
    return dict(payload), calendar_id


# ---------------------------------------------------------------------------
# Layer 6b Disconnect Purge Handler (Item 6, §4.5(2) item 6)
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: Any) -> None:
    if path.is_symlink():
        return
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".tracker-purge-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _discover_tracker_workspace_dirs(
    explicit_dirs: Sequence[Path] | None = None,
) -> list[Path]:
    if explicit_dirs is not None:
        return [Path(p) for p in explicit_dirs]
    found: list[Path] = []
    env_dir = os.getenv("DALTON_TRACKER_WORKSPACE_DIR", "").strip()
    if env_dir:
        candidate = Path(env_dir).expanduser()
        if candidate.is_dir() and not candidate.is_symlink():
            found.append(candidate)
    return found


def clean_local_tracker_event_ids(
    workspace_dirs: Sequence[Path],
    *,
    mark_reconcile_needed: bool = False,
) -> int:
    """Clear local calendar event IDs from ``watchlist.json``, ``apply.json``, and ``roll.json``.

    Never follows symlinks and returns the count of modified JSON files.
    """
    modified = 0
    for ws in workspace_dirs:
        if not ws.is_dir() or ws.is_symlink():
            continue
        state_dir = ws / "state" / "richard-earnings"
        if not state_dir.is_dir() or state_dir.is_symlink():
            continue

        watchlist_path = state_dir / "watchlist.json"
        if watchlist_path.is_file() and not watchlist_path.is_symlink():
            try:
                wl = json.loads(watchlist_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                wl = None
            if isinstance(wl, dict):
                changed = False
                for section in ("confirmed", "pending", "reported"):
                    rows = wl.get(section)
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict) and row.get("calendar_event_id") is not None:
                            row["calendar_event_id"] = None
                            changed = True
                if wl.get("pending_delete_event_ids"):
                    wl["pending_delete_event_ids"] = []
                    changed = True
                if mark_reconcile_needed:
                    if wl.get("tracker_reconcile_needed") is not True:
                        wl["tracker_reconcile_needed"] = True
                        changed = True
                else:
                    if "tracker_reconcile_needed" in wl:
                        wl.pop("tracker_reconcile_needed", None)
                        changed = True
                if changed:
                    _atomic_write_json(watchlist_path, wl)
                    modified += 1

        scans_dir = state_dir / "scans"
        if scans_dir.is_dir() and not scans_dir.is_symlink():
            for day_dir in sorted(scans_dir.iterdir()):
                if not day_dir.is_dir() or day_dir.is_symlink():
                    continue
                apply_path = day_dir / "apply.json"
                if apply_path.is_file() and not apply_path.is_symlink():
                    try:
                        doc = json.loads(apply_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        doc = None
                    if isinstance(doc, dict):
                        changed = False
                        for key in ("planned", "applied"):
                            rows = doc.get(key)
                            if not isinstance(rows, list):
                                continue
                            for item in rows:
                                if not isinstance(item, dict):
                                    continue
                                for field_name in ("event_id", "old_event_id", "calendar_event_id"):
                                    if item.get(field_name) is not None:
                                        item[field_name] = None
                                        changed = True
                        if changed:
                            _atomic_write_json(apply_path, doc)
                            modified += 1

                roll_path = day_dir / "roll.json"
                if roll_path.is_file() and not roll_path.is_symlink():
                    try:
                        doc = json.loads(roll_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        doc = None
                    if isinstance(doc, dict):
                        changed = False
                        moved = doc.get("moved")
                        if isinstance(moved, list):
                            for item in moved:
                                if not isinstance(item, dict):
                                    continue
                                for field_name in ("calendar_event_id", "event_id"):
                                    if item.get(field_name) is not None:
                                        item[field_name] = None
                                        changed = True
                        if changed:
                            _atomic_write_json(roll_path, doc)
                            modified += 1

    return modified


async def _resolve_purge_remote_credentials(
    ledger: PurgeLedgerRow,
    *,
    oauth_client: GoogleOAuthClientProtocol | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(access_token, granted_scopes, calendar_id)`` if available for remote cleanup."""
    env_calendar_id = os.getenv("DALTON_GCAL_CALENDAR_ID", "").strip() or None

    # 1. If connection row for this epoch still exists (e.g. direct call / pre-delete), inspect it.
    conn_row = await integrations_db.get_connection(
        ledger.user_id, "google", "calendar", decrypt_tokens=True
    )
    if (
        conn_row is not None
        and conn_row.auth_epoch <= ledger.auth_epoch_upto
        and conn_row.provider_account_id == ledger.provider_account_id
    ):
        prefs = dict(conn_row.preferences or {})
        cal_id = str(prefs.get("tracker_calendar_id") or "").strip() or env_calendar_id
        scopes = conn_row.granted_scopes
        if not has_tracker_write_scope(scopes):
            return None, scopes, cal_id
        now = datetime.now(UTC)
        if conn_row.access_token and (
            conn_row.expires_at is None
            or (
                conn_row.expires_at.astimezone(UTC)
                if conn_row.expires_at.tzinfo
                else conn_row.expires_at.replace(tzinfo=UTC)
            )
            > now + timedelta(seconds=30)
        ):
            return conn_row.access_token, scopes, cal_id
        if conn_row.refresh_token:
            token, refreshed_scopes = await _refresh_for_purge(
                conn_row.refresh_token, oauth_client=oauth_client
            )
            return token, (refreshed_scopes or scopes), cal_id
        return None, scopes, cal_id

    # 2. Connection row was already removed by finish_disconnect_locked; use ledger's revoke token.
    decrypted_ledger = (
        ledger
        if ledger.revoke_token
        else await integrations_db.get_purge_ledger(
            ledger.purge_id, decrypt_revoke_token=True
        )
    )
    revoke_tok = decrypted_ledger.revoke_token if decrypted_ledger is not None else None
    if not revoke_tok:
        return None, None, env_calendar_id
    token, refreshed_scopes = await _refresh_for_purge(
        revoke_tok, oauth_client=oauth_client
    )
    return token, refreshed_scopes, env_calendar_id


async def _refresh_for_purge(
    refresh_token: str,
    *,
    oauth_client: GoogleOAuthClientProtocol | None = None,
) -> tuple[str | None, str | None]:
    group = get_credential_group("google_oauth")
    if not group.ready:
        return None, None
    client_id = group.values.get(GOOGLE_OAUTH_CLIENT_ID_KEY, "")
    client_secret = group.values.get(GOOGLE_OAUTH_CLIENT_SECRET_KEY, "")
    if not client_id or not client_secret:
        return None, None
    client = oauth_client or get_google_oauth_client()
    try:
        refreshed = await client.refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
    except Exception:
        return None, None
    return refreshed.access_token, refreshed.scope


async def handle_tracker_calendar_purge(
    ledger: PurgeLedgerRow,
    *,
    workspace_dirs: Sequence[Path] | None = None,
    google_client: GoogleCalendarClientProtocol | None = None,
    oauth_client: GoogleOAuthClientProtocol | None = None,
    now_et: datetime | None = None,
) -> bool:
    """Purge handler registered at ``register_tracker_purge_handler`` (§4.5(2) item 6).

    Returns:
        ``True`` when no further reconciliation is needed (either this ledger is not
        for the tracker owner's Google calendar, or both local event IDs and remote
        ``_gcal_is_tracker`` events were deleted cleanly).
        ``False`` when local cleanup succeeded but remote deletion could not complete
        (e.g. read-only OAuth scope, external revocation, missing token, or remote
        transient failure), causing the purger to record
        ``tracker_reconcile_needed = TRUE`` without failing the disconnect flow.
    """
    owner_id = get_tracker_calendar_owner_id()
    if (
        not owner_id
        or ledger.user_id.strip().lower() != owner_id.lower()
        or ledger.provider != "google"
        or ledger.service not in ("calendar", None)
    ):
        return True

    dirs = _discover_tracker_workspace_dirs(workspace_dirs)
    # Step 1: Always clear local event IDs first (marking reconcile pending until remote succeeds).
    clean_local_tracker_event_ids(dirs, mark_reconcile_needed=True)

    # External revocations have already invalidated remote tokens -> reconcile on next reconnect.
    if ledger.trigger_reason in (
        TriggerReason.WEBHOOK_REVOKED,
        TriggerReason.ERROR_CODE_REVOKED,
    ):
        return False

    access_token, granted_scopes, calendar_id = await _resolve_purge_remote_credentials(
        ledger, oauth_client=oauth_client
    )
    if not access_token or not calendar_id:
        return False
    if granted_scopes is not None and not has_tracker_write_scope(granted_scopes):
        return False

    tz = ZoneInfo(TRACKER_DEFAULT_TIMEZONE)
    ref_now = now_et or datetime.now(tz)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=tz)
    else:
        ref_now = ref_now.astimezone(tz)

    time_min = (ref_now - timedelta(days=30)).isoformat()
    time_max = (ref_now + timedelta(days=365)).isoformat()
    client = google_client or get_google_calendar_client()

    try:
        page_token: str | None = None
        tracker_ids: list[str] = []
        seen_ids: set[str] = set()
        for _ in range(20):
            list_args = build_google_list_events_args(
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                tz_name=TRACKER_DEFAULT_TIMEZONE,
                limit=TRACKER_GCAL_MAX_LIMIT,
                page_token=page_token,
            )
            raw_list = await client.call_mcp_tool(
                access_token=access_token,
                tool_name="list_events",
                arguments=list_args,
            )
            raw_events, next_token = _extract_mcp_events(raw_list)
            for ev in raw_events:
                norm = normalize_tracker_event(ev)
                if norm is None or not is_tracker_event(norm):
                    continue
                ev_id = norm["id"]
                if ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    tracker_ids.append(ev_id)
            if not next_token:
                break
            page_token = next_token

        for ev_id in tracker_ids:
            try:
                await client.call_mcp_tool(
                    access_token=access_token,
                    tool_name="delete_event",
                    arguments={
                        "calendarId": calendar_id,
                        "eventId": ev_id,
                        "notificationLevel": "NONE",
                    },
                )
            except GoogleCalendarAPIError as exc:
                if exc.status_code in (404, 410):
                    continue
                raise
    except (GoogleCalendarAPIError, GoogleMCPElicitationRequired) as exc:
        logger.warning(
            "[tracker-purge] remote managed event cleanup incomplete for purge_id=%s (%s); marking tracker_reconcile_needed",
            ledger.purge_id,
            type(exc).__name__,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[tracker-purge] unexpected remote cleanup error for purge_id=%s (%s); marking tracker_reconcile_needed",
            ledger.purge_id,
            type(exc).__name__,
        )
        return False

    # Remote cleanup completed cleanly: clear the local reconcile marker.
    clean_local_tracker_event_ids(dirs, mark_reconcile_needed=False)
    return True


def register_default_tracker_purge_handler() -> None:
    """Register the production tracker calendar purge handler at Step 1's extension point."""
    register_tracker_purge_handler(handle_tracker_calendar_purge)


__all__ = [
    "LEGACY_GWS_BRIDGE_PATH",
    "LEGACY_GWS_MAX_TIMEOUT_S",
    "TRACKER_ALLOWED_OPERATIONS",
    "TRACKER_DEFAULT_TIMEZONE",
    "TRACKER_GCAL_MARKER",
    "TRACKER_GCAL_OLD_MARKERS",
    "TRACKER_GCAL_TRACKER_SUMMARY_RE",
    "TRACKER_WRITE_OPERATIONS",
    "TRACKER_WRITE_SCOPE_MISSING_CODE",
    "TRACKER_WRITE_SCOPE_MISSING_MESSAGE",
    "authorize_tracker_calendar_execution",
    "clean_local_tracker_event_ids",
    "execute_tracker_calendar_legacy_gws",
    "execute_tracker_calendar_official_mcp",
    "handle_tracker_calendar_purge",
    "has_tracker_read_scope",
    "has_tracker_write_scope",
    "is_tracker_event",
    "normalize_tracker_event",
    "parse_scope_tokens",
    "register_default_tracker_purge_handler",
    "with_tracker_marker",
]
