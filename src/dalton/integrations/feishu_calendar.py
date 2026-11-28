"""Feishu Calendar v4 personal OAuth client (user_access_token per call, §3.2, §5.1).

Every external HTTP call enforces ``assert_no_advisory_lock_held`` and takes a
short-lived ``access_token`` obtained via ``ensure_fresh_access_token``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import logging
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx

from src.dalton.integrations.lock_guard import assert_no_advisory_lock_held

logger = logging.getLogger(__name__)

FEISHU_CALENDAR_V4_BASE = "https://open.feishu.cn/open-apis/calendar/v4"
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class FeishuCalendarAPIError(RuntimeError):
    """Error returned by Feishu Calendar v4 API (never includes access tokens)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _iso_to_unix_str(value: str, tz_name: str = "UTC") -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("datetime value is required")
    if cleaned.isdigit():
        return cleaned
    tz = ZoneInfo(tz_name or "UTC")
    if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
        dt = datetime(
            int(cleaned[0:4]), int(cleaned[5:7]), int(cleaned[8:10]), tzinfo=tz
        )
        return str(int(dt.timestamp()))
    norm = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return str(int(dt.timestamp()))


def _feishu_time_to_iso(time_obj: Any, *, tz_name: str = "UTC") -> dict[str, Any]:
    if not isinstance(time_obj, Mapping):
        return {}
    if time_obj.get("date"):
        return {"date": str(time_obj["date"])}
    raw_ts = time_obj.get("timestamp")
    if raw_ts is not None:
        try:
            tz = ZoneInfo(str(time_obj.get("timezone") or tz_name or "UTC"))
            dt = datetime.fromtimestamp(int(str(raw_ts)), tz=UTC).astimezone(tz)
            return {"dateTime": dt.isoformat(), "timeZone": str(tz)}
        except (ValueError, KeyError):
            return {"timestamp": str(raw_ts)}
    return {}


def normalize_feishu_event(
    raw: Mapping[str, Any],
    *,
    calendar_id: str,
    tz_name: str = "UTC",
) -> dict[str, Any]:
    """Normalize a Feishu Calendar v4 event object into the unified tool representation."""
    normalized: dict[str, Any] = {
        "provider": "feishu",
        "provider_label": "[飞书日历]",
        "calendar_id": calendar_id,
        "event_id": str(raw.get("event_id") or ""),
        "summary": str(raw.get("summary") or ""),
        "description": str(raw.get("description") or ""),
        "location": (
            str(raw["location"].get("name") or "")
            if isinstance(raw.get("location"), Mapping)
            else str(raw.get("location") or "")
        ),
        "start": _feishu_time_to_iso(raw.get("start_time"), tz_name=tz_name),
        "end": _feishu_time_to_iso(raw.get("end_time"), tz_name=tz_name),
        "status": str(raw.get("status") or "confirmed"),
        "updated": raw.get("update_time") or raw.get("create_time"),
        "attendees": list(raw.get("attendees") or []),
        "reminders": list(raw.get("reminders") or []),
        "html_link": raw.get("app_link"),
    }
    idem_key = str(raw.get("idempotency_key") or "").strip()
    if idem_key:
        normalized["idempotency_key"] = idem_key
    return normalized


def build_feishu_event_body(
    *,
    summary: str | None = None,
    description: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    tz_name: str = "UTC",
    all_day: bool = False,
    location: str | None = None,
    reminders: Sequence[Mapping[str, Any]] | None = None,
    need_notification: bool | None = None,
) -> dict[str, Any]:
    """Build a Feishu Calendar v4 event create/patch payload."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary.strip()
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = {"name": location.strip()}
    if start_time is not None:
        if all_day:
            body["start_time"] = {"date": start_time[:10]}
        else:
            body["start_time"] = {
                "timestamp": _iso_to_unix_str(start_time, tz_name),
                "timezone": tz_name or "UTC",
            }
    if end_time is not None:
        if all_day:
            body["end_time"] = {"date": end_time[:10]}
        else:
            body["end_time"] = {
                "timestamp": _iso_to_unix_str(end_time, tz_name),
                "timezone": tz_name or "UTC",
            }
    if reminders is not None:
        body["reminders"] = [
            {"minutes": max(0, int(r.get("minutes", 10)))}
            for r in reminders
            if isinstance(r, Mapping)
        ]
    if need_notification is not None:
        body["need_notification"] = bool(need_notification)
    return body


@runtime_checkable
class FeishuCalendarClientProtocol(Protocol):
    """Injectable protocol for Feishu Calendar v4 API operations."""

    async def list_calendars(
        self,
        *,
        access_token: str,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        start_time: str,
        end_time: str,
        tz_name: str = "UTC",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def list_events_page(
        self,
        *,
        access_token: str,
        calendar_id: str,
        start_time: str,
        end_time: str,
        tz_name: str = "UTC",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        tz_name: str = "UTC",
    ) -> dict[str, Any]: ...

    async def create_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        body: Mapping[str, Any],
        attendees: Sequence[str] | None = None,
        tz_name: str = "UTC",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def update_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        body: Mapping[str, Any],
        tz_name: str = "UTC",
    ) -> dict[str, Any]: ...

    async def delete_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        need_notification: bool = True,
    ) -> dict[str, Any]: ...


class FeishuCalendarClient:
    """Async HTTP client for Feishu Calendar v4 personal calendar endpoints."""

    def __init__(
        self,
        *,
        base_url: str = FEISHU_CALENDAR_V4_BASE,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        operation: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_no_advisory_lock_held(f"feishu_calendar.{operation}")
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise FeishuCalendarAPIError(
                f"Failed to contact Feishu Calendar API ({type(exc).__name__})."
            ) from None

        try:
            payload = resp.json()
        except ValueError:
            raise FeishuCalendarAPIError(
                f"Feishu Calendar API returned non-JSON HTTP {resp.status_code}.",
                status_code=resp.status_code,
            ) from None

        code = payload.get("code") if isinstance(payload, Mapping) else None
        if resp.status_code >= 400 or (code is not None and int(code) != 0):
            msg = str(
                payload.get("msg") if isinstance(payload, Mapping) else ""
            ) or f"Feishu Calendar API error (HTTP {resp.status_code})"
            raise FeishuCalendarAPIError(
                msg,
                status_code=resp.status_code,
                code=int(code) if code is not None else None,
            )
        data = payload.get("data") if isinstance(payload, Mapping) else {}
        return dict(data) if isinstance(data, Mapping) else {}

    async def list_calendars(
        self,
        *,
        access_token: str,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page_size": max(50, min(int(page_size), 1000))}
        if page_token:
            params["page_token"] = page_token
        data = await self._request(
            "GET",
            "/calendars",
            access_token=access_token,
            operation="list_calendars",
            params=params,
        )
        items = data.get("calendar_list") or []
        calendars: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                calendars.append(
                    {
                        "provider": "feishu",
                        "provider_label": "[飞书日历]",
                        "calendar_id": str(item.get("calendar_id") or ""),
                        "summary": str(item.get("summary") or ""),
                        "primary": str(item.get("type") or "") == "primary",
                        "role": item.get("role"),
                    }
                )
        return calendars

    async def list_events_page(
        self,
        *,
        access_token: str,
        calendar_id: str,
        start_time: str,
        end_time: str,
        tz_name: str = "UTC",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "start_time": _iso_to_unix_str(start_time, tz_name),
            "end_time": _iso_to_unix_str(end_time, tz_name),
            "page_size": max(50, min(int(page_size), 500)),
        }
        if page_token:
            params["page_token"] = page_token
        data = await self._request(
            "GET",
            f"/calendars/{calendar_id}/events",
            access_token=access_token,
            operation="list_events",
            params=params,
        )
        items = data.get("items") or []
        normalized = [
            normalize_feishu_event(item, calendar_id=calendar_id, tz_name=tz_name)
            for item in items
            if isinstance(item, Mapping)
        ]
        next_token = str(data.get("page_token") or "").strip() or None
        has_more = bool(data.get("has_more")) or bool(next_token)
        return {
            "items": normalized,
            "has_more": has_more,
            "page_token": next_token,
        }

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        start_time: str,
        end_time: str,
        tz_name: str = "UTC",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]:
        page = await self.list_events_page(
            access_token=access_token,
            calendar_id=calendar_id,
            start_time=start_time,
            end_time=end_time,
            tz_name=tz_name,
            page_size=page_size,
            page_token=page_token,
        )
        return list(page.get("items") or [])

    async def get_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        tz_name: str = "UTC",
    ) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/calendars/{calendar_id}/events/{event_id}",
            access_token=access_token,
            operation="get_event",
        )
        ev = data.get("event") if isinstance(data.get("event"), Mapping) else data
        return normalize_feishu_event(ev, calendar_id=calendar_id, tz_name=tz_name)

    async def create_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        body: Mapping[str, Any],
        attendees: Sequence[str] | None = None,
        tz_name: str = "UTC",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        params = (
            {"idempotency_key": str(idempotency_key).strip()}
            if idempotency_key and str(idempotency_key).strip()
            else None
        )
        data = await self._request(
            "POST",
            f"/calendars/{calendar_id}/events",
            access_token=access_token,
            operation="create_event",
            params=params,
            json_body=body,
        )
        ev = data.get("event") if isinstance(data.get("event"), Mapping) else data
        normalized = normalize_feishu_event(ev, calendar_id=calendar_id, tz_name=tz_name)
        cleaned_attendees = [str(a).strip() for a in (attendees or []) if str(a).strip()]
        if cleaned_attendees and normalized.get("event_id"):
            attendee_items = [
                {"type": "third_party", "third_party_email": email}
                if "@" in email
                else {"type": "user", "user_id": email}
                for email in cleaned_attendees
            ]
            await self._request(
                "POST",
                f"/calendars/{calendar_id}/events/{normalized['event_id']}/attendees",
                access_token=access_token,
                operation="create_event_attendees",
                json_body={
                    "attendees": attendee_items,
                    "need_notification": bool(body.get("need_notification", True)),
                },
            )
            normalized["attendees"] = [{"email": a} for a in cleaned_attendees]
        return normalized

    async def update_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        body: Mapping[str, Any],
        tz_name: str = "UTC",
    ) -> dict[str, Any]:
        data = await self._request(
            "PATCH",
            f"/calendars/{calendar_id}/events/{event_id}",
            access_token=access_token,
            operation="update_event",
            json_body=body,
        )
        ev = data.get("event") if isinstance(data.get("event"), Mapping) else data
        return normalize_feishu_event(ev, calendar_id=calendar_id, tz_name=tz_name)

    async def delete_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        event_id: str,
        need_notification: bool = True,
    ) -> dict[str, Any]:
        await self._request(
            "DELETE",
            f"/calendars/{calendar_id}/events/{event_id}",
            access_token=access_token,
            operation="delete_event",
            params={"need_notification": "true" if need_notification else "false"},
        )
        return {
            "provider": "feishu",
            "calendar_id": calendar_id,
            "event_id": event_id,
            "deleted": True,
        }


_default_feishu_calendar_client: FeishuCalendarClientProtocol | None = None


def get_feishu_calendar_client() -> FeishuCalendarClientProtocol:
    global _default_feishu_calendar_client
    if _default_feishu_calendar_client is None:
        _default_feishu_calendar_client = FeishuCalendarClient()
    return _default_feishu_calendar_client


__all__ = [
    "FEISHU_CALENDAR_V4_BASE",
    "FeishuCalendarAPIError",
    "FeishuCalendarClient",
    "FeishuCalendarClientProtocol",
    "build_feishu_event_body",
    "get_feishu_calendar_client",
    "normalize_feishu_event",
]
