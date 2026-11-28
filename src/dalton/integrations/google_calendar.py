"""Google Calendar official hosted MCP client with stateless ``_meta`` & form elicitation (§3.1, §5.2).

Every external HTTP call enforces ``assert_no_advisory_lock_held`` and accepts a
short-lived ``access_token`` obtained via ``ensure_fresh_access_token`` — tokens are
never stored on the client instance or echoed in exceptions/logs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx

from src.dalton.integrations.lock_guard import assert_no_advisory_lock_held

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_MCP_ENDPOINT = "https://calendarmcp.googleapis.com/mcp/v1"
GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GOOGLE_MCP_PROTOCOL_VERSION = "2026-07-28"
DALTON_MCP_CLIENT_VERSION = "1.0.0"

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class GoogleCalendarAPIError(RuntimeError):
    """Error returned by Google Calendar MCP or CalendarList API (never contains tokens)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class GoogleMCPElicitationRequired(Exception):
    """Raised when Google hosted ``/mcp/v1`` returns ``resultType == 'input_required'`` (§3.1(2))."""

    def __init__(
        self,
        *,
        mcp_endpoint: str,
        mcp_tool_name: str,
        mcp_arguments: Mapping[str, Any],
        request_state: str,
        input_requests: Mapping[str, Any],
    ) -> None:
        super().__init__(
            f"Google MCP elicitation required for tool '{mcp_tool_name}' "
            f"(keys={sorted(input_requests.keys())})"
        )
        self.mcp_endpoint = mcp_endpoint
        self.mcp_tool_name = mcp_tool_name
        self.mcp_arguments = dict(mcp_arguments)
        self._request_state = str(request_state)
        self.input_requests: dict[str, dict[str, Any]] = {
            str(k): dict(v) if isinstance(v, Mapping) else {}
            for k, v in input_requests.items()
        }

    @property
    def request_state(self) -> str:
        return self._request_state

    def __repr__(self) -> str:
        return (
            f"GoogleMCPElicitationRequired(mcp_tool_name={self.mcp_tool_name!r}, "
            f"keys={sorted(self.input_requests.keys())!r})"
        )


def build_google_mcp_meta(
    *,
    protocol_version: str = GOOGLE_MCP_PROTOCOL_VERSION,
    client_version: str = DALTON_MCP_CLIENT_VERSION,
) -> dict[str, Any]:
    """Build the stateless ``params._meta`` header required on every ``/mcp/v1`` call (§3.1(2))."""
    return {
        "io.modelcontextprotocol/protocolVersion": protocol_version,
        "io.modelcontextprotocol/clientInfo": {
            "name": "dalton",
            "version": client_version,
        },
        "io.modelcontextprotocol/clientCapabilities": {
            "elicitation": {
                "form": {},
            }
        },
    }


def build_mcp_tools_call_payload(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    request_state: str | None = None,
    input_responses: Mapping[str, Any] | None = None,
    request_id: str = "1",
) -> dict[str, Any]:
    """Construct the JSON-RPC 2.0 ``tools/call`` body for Google Workspace ``/mcp/v1`` (§3.1(2))."""
    params: dict[str, Any] = {
        "name": tool_name,
        "arguments": dict(arguments),
        "_meta": build_google_mcp_meta(),
    }
    if request_state is not None:
        params["requestState"] = request_state
    if input_responses is not None:
        params["inputResponses"] = dict(input_responses)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


def parse_google_mcp_response(
    payload: Mapping[str, Any],
    *,
    mcp_endpoint: str,
    mcp_tool_name: str,
    mcp_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse a JSON-RPC 2.0 response from Google ``/mcp/v1``, raising on errors or elicitation."""
    if not isinstance(payload, Mapping):
        raise GoogleCalendarAPIError("Google Calendar MCP returned a non-object response.")

    if "error" in payload and payload["error"] is not None:
        err = payload["error"]
        if isinstance(err, Mapping):
            msg = str(err.get("message") or "Google Calendar MCP error")
            code = err.get("code")
        else:
            msg = str(err)
            code = None
        raise GoogleCalendarAPIError(msg, error_code=code)

    result = payload.get("result")
    if not isinstance(result, Mapping):
        return {"raw": result}

    # Direct InputRequiredResult at result root (§3.1(2))
    if result.get("resultType") == "input_required":
        _raise_elicitation(
            result,
            mcp_endpoint=mcp_endpoint,
            mcp_tool_name=mcp_tool_name,
            mcp_arguments=mcp_arguments,
        )

    # Structured content object
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        if structured.get("resultType") == "input_required":
            _raise_elicitation(
                structured,
                mcp_endpoint=mcp_endpoint,
                mcp_tool_name=mcp_tool_name,
                mcp_arguments=mcp_arguments,
            )
        return dict(structured)

    # Standard MCP content array with JSON text block
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, Mapping):
                    if parsed.get("resultType") == "input_required":
                        _raise_elicitation(
                            parsed,
                            mcp_endpoint=mcp_endpoint,
                            mcp_tool_name=mcp_tool_name,
                            mcp_arguments=mcp_arguments,
                        )
                    return dict(parsed)
                return {"items": parsed}

    if result.get("isError"):
        raise GoogleCalendarAPIError(
            "Google Calendar MCP tool returned an error result.",
            error_code="mcp_tool_error",
        )
    return dict(result)


def _raise_elicitation(
    obj: Mapping[str, Any],
    *,
    mcp_endpoint: str,
    mcp_tool_name: str,
    mcp_arguments: Mapping[str, Any],
) -> None:
    req_state = str(obj.get("requestState") or "")
    raw_requests = obj.get("inputRequests")
    if not req_state or not isinstance(raw_requests, Mapping) or not raw_requests:
        raise GoogleCalendarAPIError(
            "Google Calendar MCP returned input_required without valid requestState/inputRequests.",
            error_code="malformed_elicitation",
        )
    raise GoogleMCPElicitationRequired(
        mcp_endpoint=mcp_endpoint,
        mcp_tool_name=mcp_tool_name,
        mcp_arguments=mcp_arguments,
        request_state=req_state,
        input_requests=raw_requests,
    )


def format_local_datetime_for_tz(value: str, tz_name: str) -> str:
    """Convert an ISO datetime (with/without offset) or YYYY-MM-DD date to offset-free local time."""
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("datetime value is required")
    tz = ZoneInfo(tz_name or "UTC")
    if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
        dt = datetime(
            int(cleaned[0:4]),
            int(cleaned[5:7]),
            int(cleaned[8:10]),
            0,
            0,
            0,
            tzinfo=UTC,
        )
        return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")
    iso_norm = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    dt = datetime.fromisoformat(iso_norm)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_google_event(raw: Mapping[str, Any], *, calendar_id: str) -> dict[str, Any]:
    """Normalize a Google Calendar MCP event payload into the unified tool representation."""
    start = raw.get("start")
    end = raw.get("end")
    attendees_raw = raw.get("attendees") or []
    attendees: list[dict[str, Any]] = []
    if isinstance(attendees_raw, list):
        for att in attendees_raw:
            if isinstance(att, Mapping):
                email = str(att.get("email") or "").strip()
                if email:
                    attendees.append(
                        {
                            "email": email,
                            "display_name": att.get("displayName"),
                            "response_status": att.get("responseStatus"),
                            "self": bool(att.get("self")),
                        }
                    )
            elif isinstance(att, str) and att.strip():
                attendees.append({"email": att.strip()})

    ext_props = raw.get("extendedProperties")
    ext_dict = dict(ext_props) if isinstance(ext_props, Mapping) else None
    idem_key = str(raw.get("idempotency_key") or "").strip() or None
    if not idem_key and isinstance(ext_dict, Mapping):
        priv = ext_dict.get("private")
        if isinstance(priv, Mapping):
            idem_key = str(priv.get("dalton_idempotency_key") or "").strip() or None

    normalized: dict[str, Any] = {
        "provider": "google",
        "provider_label": "[Google 日历]",
        "calendar_id": calendar_id,
        "event_id": str(raw.get("id") or raw.get("eventId") or ""),
        "summary": str(raw.get("summary") or ""),
        "description": str(raw.get("description") or ""),
        "location": str(raw.get("location") or ""),
        "start": start if isinstance(start, Mapping) else {"dateTime": raw.get("startTime")},
        "end": end if isinstance(end, Mapping) else {"dateTime": raw.get("endTime")},
        "status": str(raw.get("status") or "confirmed"),
        "updated": raw.get("updated") or raw.get("etag"),
        "attendees": attendees,
        "reminders": raw.get("reminders") or raw.get("overrideReminders"),
        "html_link": raw.get("htmlLink"),
    }
    if ext_dict is not None:
        normalized["extendedProperties"] = ext_dict
    if idem_key:
        normalized["idempotency_key"] = idem_key
    return normalized


def compute_free_busy_slots(
    events: Sequence[Mapping[str, Any]],
    *,
    time_min: str,
    time_max: str,
    tz_name: str = "UTC",
    min_slot_minutes: int = 30,
) -> dict[str, Any]:
    """Compute sorted busy intervals and free windows across normalized events."""
    tz = ZoneInfo(tz_name or "UTC")

    def _parse_ts(val: str) -> datetime:
        cleaned = str(val or "").strip()
        if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
            return datetime(
                int(cleaned[0:4]), int(cleaned[5:7]), int(cleaned[8:10]), tzinfo=tz
            )
        norm = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        dt = datetime.fromisoformat(norm)
        return dt.astimezone(tz) if dt.tzinfo is not None else dt.replace(tzinfo=tz)

    window_start = _parse_ts(time_min)
    window_end = _parse_ts(time_max)
    if window_end <= window_start:
        return {"busy": [], "free": [], "timezone": tz_name}

    raw_busy: list[tuple[datetime, datetime, str]] = []
    for ev in events:
        if str(ev.get("status") or "").lower() == "cancelled":
            continue
        start_obj = ev.get("start") or {}
        end_obj = ev.get("end") or {}
        s_str = (
            start_obj.get("dateTime") or start_obj.get("date")
            if isinstance(start_obj, Mapping)
            else None
        )
        e_str = (
            end_obj.get("dateTime") or end_obj.get("date")
            if isinstance(end_obj, Mapping)
            else None
        )
        if not s_str or not e_str:
            continue
        try:
            s_dt = max(window_start, _parse_ts(str(s_str)))
            e_dt = min(window_end, _parse_ts(str(e_str)))
        except ValueError:
            continue
        if e_dt > s_dt:
            label = f"{ev.get('provider_label', '')} {ev.get('summary', '')}".strip()
            raw_busy.append((s_dt, e_dt, label))

    raw_busy.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = []
    for s_dt, e_dt, _ in raw_busy:
        if not merged or s_dt > merged[-1][1]:
            merged.append((s_dt, e_dt))
        elif e_dt > merged[-1][1]:
            merged[-1] = (merged[-1][0], e_dt)

    min_delta = timedelta(minutes=max(1, int(min_slot_minutes)))
    free_slots: list[dict[str, Any]] = []
    cursor = window_start
    for b_start, b_end in merged:
        if b_start - cursor >= min_delta:
            free_slots.append(
                {
                    "start": cursor.isoformat(),
                    "end": b_start.isoformat(),
                    "duration_minutes": int((b_start - cursor).total_seconds() // 60),
                }
            )
        if b_end > cursor:
            cursor = b_end
    if window_end - cursor >= min_delta:
        free_slots.append(
            {
                "start": cursor.isoformat(),
                "end": window_end.isoformat(),
                "duration_minutes": int((window_end - cursor).total_seconds() // 60),
            }
        )

    return {
        "timezone": tz_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "busy": [
            {"start": s.isoformat(), "end": e.isoformat(), "summary": lbl}
            for s, e, lbl in raw_busy
        ],
        "free": free_slots,
    }


@runtime_checkable
class GoogleCalendarClientProtocol(Protocol):
    """Injectable protocol for Google Calendar MCP & CalendarList operations."""

    async def list_calendars(self, *, access_token: str) -> list[dict[str, Any]]: ...

    async def call_mcp_tool(
        self,
        *,
        access_token: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        request_state: str | None = None,
        input_responses: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class GoogleCalendarMCPClient:
    """Stateless Streamable HTTP client for Google official Calendar MCP ``/mcp/v1``."""

    def __init__(
        self,
        *,
        mcp_endpoint: str = GOOGLE_CALENDAR_MCP_ENDPOINT,
        calendar_list_url: str = GOOGLE_CALENDAR_LIST_URL,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._mcp_endpoint = mcp_endpoint
        self._calendar_list_url = calendar_list_url
        self._timeout = timeout

    async def list_calendars(self, *, access_token: str) -> list[dict[str, Any]]:
        assert_no_advisory_lock_held("google_calendar.list_calendars")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._calendar_list_url, headers=headers)
        except httpx.HTTPError as exc:
            raise GoogleCalendarAPIError(
                f"Failed to contact Google Calendar API ({type(exc).__name__}).",
                error_code="network_error",
            ) from None

        if resp.status_code >= 400:
            raise GoogleCalendarAPIError(
                f"Google Calendar list request failed with HTTP {resp.status_code}.",
                status_code=resp.status_code,
                error_code="http_error",
            )
        data = resp.json()
        items = data.get("items") if isinstance(data, Mapping) else []
        calendars: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                calendars.append(
                    {
                        "provider": "google",
                        "provider_label": "[Google 日历]",
                        "calendar_id": str(item.get("id") or ""),
                        "summary": str(item.get("summary") or ""),
                        "primary": bool(item.get("primary")),
                        "time_zone": item.get("timeZone"),
                        "access_role": item.get("accessRole"),
                    }
                )
        return calendars

    async def call_mcp_tool(
        self,
        *,
        access_token: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        request_state: str | None = None,
        input_responses: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_no_advisory_lock_held(f"google_calendar.mcp.{tool_name}")
        body = build_mcp_tools_call_payload(
            tool_name,
            arguments,
            request_state=request_state,
            input_responses=input_responses,
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._mcp_endpoint,
                    headers=headers,
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise GoogleCalendarAPIError(
                f"Failed to contact Google Calendar MCP ({type(exc).__name__}).",
                error_code="network_error",
            ) from None

        if resp.status_code >= 400:
            raise GoogleCalendarAPIError(
                f"Google Calendar MCP returned HTTP {resp.status_code}.",
                status_code=resp.status_code,
                error_code="http_error",
            )
        try:
            payload = resp.json()
        except ValueError:
            raise GoogleCalendarAPIError(
                "Google Calendar MCP returned malformed JSON.",
                status_code=resp.status_code,
                error_code="invalid_json",
            ) from None

        return parse_google_mcp_response(
            payload,
            mcp_endpoint=self._mcp_endpoint,
            mcp_tool_name=tool_name,
            mcp_arguments=arguments,
        )


def build_google_list_events_args(
    *,
    calendar_id: str,
    time_min: str,
    time_max: str,
    tz_name: str = "UTC",
    query: str | None = None,
    limit: int = 50,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Build official Google Calendar MCP ``list_events`` arguments (§5.3)."""
    args: dict[str, Any] = {
        "calendarId": calendar_id,
        "startTime": format_local_datetime_for_tz(time_min, tz_name),
        "endTime": format_local_datetime_for_tz(time_max, tz_name),
        "timeZone": tz_name or "UTC",
        "pageSize": max(1, min(int(limit), 250)),
        "orderBy": "startTime",
    }
    if query and query.strip():
        args["fullText"] = query.strip()
    if page_token and page_token.strip():
        args["pageToken"] = page_token.strip()
    return args


def build_google_event_write_args(
    *,
    calendar_id: str,
    summary: str | None = None,
    description: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    tz_name: str = "UTC",
    all_day: bool = False,
    location: str | None = None,
    attendees: Sequence[str] | None = None,
    reminders: Sequence[Mapping[str, Any]] | None = None,
    use_default_reminders: bool | None = None,
    event_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build official Google Calendar MCP ``create_event`` / ``update_event`` arguments (§3.1(3), §5.1)."""
    args: dict[str, Any] = {"calendarId": calendar_id}
    if event_id is not None:
        args["eventId"] = event_id
    if summary is not None:
        args["summary"] = summary.strip()
    if description is not None:
        args["description"] = description
    if location is not None:
        args["location"] = location.strip()
    if start_time is not None:
        if all_day:
            args["startTime"] = start_time[:10]
            args["allDay"] = True
        else:
            args["startTime"] = format_local_datetime_for_tz(start_time, tz_name)
            args["timeZone"] = tz_name or "UTC"
            args["allDay"] = False
    if end_time is not None:
        if all_day:
            args["endTime"] = end_time[:10]
            args["allDay"] = True
        else:
            args["endTime"] = format_local_datetime_for_tz(end_time, tz_name)
            args["timeZone"] = tz_name or "UTC"
            args["allDay"] = False
    if attendees is not None:
        cleaned = [str(e).strip() for e in attendees if str(e).strip()]
        args["attendees"] = cleaned
    if reminders is not None:
        formatted_reminders: list[dict[str, Any]] = []
        for r in reminders:
            if not isinstance(r, Mapping):
                continue
            method = str(r.get("method") or "popup").strip().lower()
            if method not in ("popup", "email"):
                method = "popup"
            minutes = int(r.get("minutes", 10))
            formatted_reminders.append({"method": method, "minutes": max(0, minutes)})
        args["overrideReminders"] = formatted_reminders
        args["useDefaultReminders"] = False
    elif use_default_reminders is not None:
        args["useDefaultReminders"] = bool(use_default_reminders)
    if idempotency_key and str(idempotency_key).strip():
        args["extendedProperties"] = {
            "private": {"dalton_idempotency_key": str(idempotency_key).strip()}
        }
    return args


_default_google_calendar_client: GoogleCalendarClientProtocol | None = None


def get_google_calendar_client() -> GoogleCalendarClientProtocol:
    global _default_google_calendar_client
    if _default_google_calendar_client is None:
        _default_google_calendar_client = GoogleCalendarMCPClient()
    return _default_google_calendar_client


__all__ = [
    "DALTON_MCP_CLIENT_VERSION",
    "GOOGLE_CALENDAR_LIST_URL",
    "GOOGLE_CALENDAR_MCP_ENDPOINT",
    "GOOGLE_MCP_PROTOCOL_VERSION",
    "GoogleCalendarAPIError",
    "GoogleCalendarClientProtocol",
    "GoogleCalendarMCPClient",
    "GoogleMCPElicitationRequired",
    "build_google_event_write_args",
    "build_google_list_events_args",
    "build_google_mcp_meta",
    "build_mcp_tools_call_payload",
    "compute_free_busy_slots",
    "format_local_datetime_for_tz",
    "get_google_calendar_client",
    "normalize_google_event",
    "parse_google_mcp_response",
]
