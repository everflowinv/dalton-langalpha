"""Direct LangChain tool ``dalton_calendar`` for personal Google & Feishu calendars (§5.1, §5.2).

Security invariants:
- Mounted in-process on the backend main agent only (zero sandbox or subagent exposure).
- Gated behind ``CapabilityId.GOOGLE_CALENDAR`` and ``CapabilityId.FEISHU_CALENDAR`` via
  ``DALTON_TOOL_REGISTRY.any_capabilities``; provider branches are clamped to active capabilities.
- Access tokens are NEVER cached or held by the tool; every upstream call acquires a
  short-lived token through Step 2's ``ensure_fresh_access_token(...)`` outside DB locks.
- Private surface is pre-checked before token acquisition, and touch recording runs
  atomically right before external calendar calls with the actual token's
  ``(auth_epoch, provider_account_id)`` verified under ``FOR UPDATE``.
- First-touch privacy notice is persisted and deduplicated per thread, claimed only when
  a call succeeds so failures/interrupts never lose the notice.
- Write operations (create/update/delete) require L1 or L2 (external attendees) HITL
  confirmation via ``hitl_confirm(action_type, payload)``. On LangGraph resume, the tool
  restores and consumes ONLY the original approval row, executes the frozen normalized
  payload, re-verifies account generation and event version, and resumes Google MCP
  multi-round ``input_required`` elicitations using persisted ``requestState`` without
  re-running completed stages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Annotated, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from pydantic import WithJsonSchema

from src.dalton.capabilities import (
    CapabilityId,
    effective_capabilities,
    get_connector_providers_summary,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    ActionApprovalRow,
    IntegrationRow,
    IntegrationStatus,
)
from src.dalton.integrations.constants import (
    FEISHU_CALENDAR_SCOPES,
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
)
from src.dalton.integrations.feishu_calendar import (
    FeishuCalendarAPIError,
    FeishuCalendarClientProtocol,
    build_feishu_event_body,
    get_feishu_calendar_client,
)
from src.dalton.integrations.google_calendar import (
    GoogleCalendarAPIError,
    GoogleCalendarClientProtocol,
    GoogleMCPElicitationRequired,
    build_google_event_write_args,
    build_google_list_events_args,
    compute_free_busy_slots,
    get_google_calendar_client,
    normalize_google_event,
)
from src.dalton.services.personal_integration_approval import (
    ELICITATION_APPROVAL_TTL_SECONDS,
    L1_APPROVAL_TTL_SECONDS,
    L2_APPROVAL_TTL_SECONDS,
    build_private_thread_web_deep_link,
    canonical_payload_sha256,
    compute_calendar_confirm_token,
    compute_elicitation_confirm_token,
    validate_elicitation_responses,
)
from src.dalton.services.personal_integration_guard import (
    PersonalSurfaceDeniedError,
    PersonalSurfaceVerificationResult,
    check_thread_surface_still_private,
    claim_first_touch_notice_if_needed,
    extract_surface_context,
    verify_personal_integration_surface,
)
from src.dalton.services.user_integrations_oauth import (
    FreshAccessToken,
    IntegrationNeedsReauthError,
    IntegrationNotConnectedError,
    MissingRequiredScopesError,
    PersonalIntegrationError,
    ensure_fresh_access_token,
)
from src.tools.secretary._commands import InjectedToolCallId, hitl_confirm

logger = logging.getLogger(__name__)

CalendarReadActionLiteral = Literal[
    "list_calendars",
    "list_events",
    "get_event",
    "check_availability",
]
CalendarReadActionArg = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "enum": [
                "list_calendars",
                "list_events",
                "get_event",
                "check_availability",
            ],
        }
    ),
]
CalendarActionLiteral = Literal[
    "list_calendars",
    "list_events",
    "get_event",
    "check_availability",
    "create_event",
    "update_event",
    "delete_event",
]
CalendarProviderLiteral = Literal["auto", "google", "feishu"]

_READ_ACTIONS: frozenset[str] = frozenset(
    {"list_calendars", "list_events", "get_event", "check_availability"}
)
_WRITE_ACTIONS: frozenset[str] = frozenset(
    {"create_event", "update_event", "delete_event"}
)
CALENDAR_WRITE_DISABLED_MESSAGE = "当前版本只支持查看日程和空闲时间"
_SETTINGS_INTEGRATIONS_PATH = "/settings?tab=integrations#dalton-personal-integrations"
_MAX_ELICITATION_ROUNDS = 4
_MAX_AVAILABILITY_PAGES = 20


def _tool_command(payload: Mapping[str, Any], tool_call_id: str) -> Command:
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(dict(payload), ensure_ascii=False, default=str),
                    tool_call_id=tool_call_id,
                    name="dalton_calendar",
                ),
            ],
        }
    )


def _error_command(
    error: str,
    message: str,
    tool_call_id: str,
    **extra: Any,
) -> Command:
    payload: dict[str, Any] = {
        "ok": False,
        "success": False,
        "error": error,
        "message": message,
    }
    payload.update(extra)
    return _tool_command(payload, tool_call_id)


def _auth_error_command(
    exc: Exception,
    *,
    provider: str,
    tool_call_id: str,
) -> Command:
    """Format a user-facing auth/scope guidance error (especially insufficient scopes)."""
    pname = _provider_display_name(provider)
    if isinstance(exc, MissingRequiredScopesError) or (
        isinstance(exc, IntegrationNeedsReauthError)
        and getattr(exc, "code", None) == "missing_required_scopes"
    ):
        return _error_command(
            "needs_reauth",
            f"你的{pname}当前授权权限不足，请前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）"
            f"重新授权并勾选所需日历权限后再试。",
            tool_call_id,
            code="missing_required_scopes",
            provider=provider,
            settings_path=_SETTINGS_INTEGRATIONS_PATH,
        )
    if isinstance(exc, IntegrationNeedsReauthError):
        return _error_command(
            "needs_reauth",
            f"你的{pname}授权已失效，请前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）重新授权后再试。",
            tool_call_id,
            code=getattr(exc, "code", "needs_reauth"),
            provider=provider,
            settings_path=_SETTINGS_INTEGRATIONS_PATH,
        )
    if isinstance(exc, IntegrationNotConnectedError):
        return _error_command(
            "not_connected",
            f"你尚未连接{pname}，请前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）完成连接。",
            tool_call_id,
            provider=provider,
            settings_path=_SETTINGS_INTEGRATIONS_PATH,
        )
    return _error_command(
        "calendar_auth_failed",
        str(exc),
        tool_call_id,
        provider=provider,
        settings_path=_SETTINGS_INTEGRATIONS_PATH,
    )


_UNATTENDED_PLATFORMS: frozenset[str] = frozenset(
    {"automation", "system", "wakeup", "market_target"}
)
_UNATTENDED_ORIGINS: frozenset[str] = frozenset(
    {"automation", "agent", "system", "wakeup", "market_target", "report"}
)
_UNATTENDED_QUERY_TYPES: frozenset[str] = frozenset({"system", "market_target", "wakeup"})
UNATTENDED_CALENDAR_REFUSAL_MESSAGE = (
    "个人日历只能在你本人的对话里使用，自动任务暂不支持。"
)


def _is_unattended_automation_turn(config: RunnableConfig | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    cfg = config.get("configurable")
    if isinstance(cfg, Mapping):
        if (
            str(cfg.get("automation_execution_id") or "").strip()
            or str(cfg.get("turn_origin_automation_id") or "").strip()
            or str(cfg.get("automation_id") or "").strip()
            or str(cfg.get("report_command_id") or "").strip()
        ):
            return True
        if (
            cfg.get("wake_turn") is True
            or cfg.get("unwatched") is True
            or cfg.get("unattended") is True
            or bool(cfg.get("automation_delivery"))
        ):
            return True
        if str(cfg.get("platform") or "").strip().lower() in _UNATTENDED_PLATFORMS:
            return True
        if str(cfg.get("query_type") or "").strip().lower() in _UNATTENDED_QUERY_TYPES:
            return True
        if str(cfg.get("origin") or "").strip().lower() in _UNATTENDED_ORIGINS:
            return True
        if str(cfg.get("run_origin") or "").strip().lower() in _UNATTENDED_ORIGINS:
            return True
        surface = cfg.get("surface")
        if (
            isinstance(surface, Mapping)
            and str(surface.get("platform") or "").strip().lower() in _UNATTENDED_PLATFORMS
        ):
            return True
    meta = config.get("metadata")
    if isinstance(meta, Mapping):
        if (
            str(meta.get("automation_id") or "").strip()
            or str(meta.get("automation_execution_id") or "").strip()
            or meta.get("unwatched") is True
        ):
            return True
        if str(meta.get("origin") or "").strip().lower() in _UNATTENDED_ORIGINS:
            return True
        if str(meta.get("query_type") or "").strip().lower() in _UNATTENDED_QUERY_TYPES:
            return True
    return False


def _enabled_calendar_providers() -> list[str]:
    caps = effective_capabilities()
    providers: list[str] = []
    if CapabilityId.GOOGLE_CALENDAR in caps:
        providers.append("google")
    if CapabilityId.FEISHU_CALENDAR in caps:
        providers.append("feishu")
    return providers


def _provider_display_name(provider: str) -> str:
    return "Google 日历" if provider == "google" else "飞书日历"


def _provider_not_ready_command(
    providers: Sequence[str],
    tool_call_id: str,
) -> Command | None:
    """Reuse backend ``ready`` summary to give admin-setup guidance when connectors are not ready."""
    summary = get_connector_providers_summary()
    not_ready = [p for p in providers if not bool((summary.get(p) or {}).get("ready"))]
    if not not_ready or len(not_ready) < len(providers):
        return None
    names = "或".join(_provider_display_name(p) for p in not_ready)
    missing_creds = any(
        not bool((summary.get(p) or {}).get("credentials_ready")) for p in not_ready
    )
    if missing_creds:
        msg = (
            f"当前部署已开启{names}功能，但管理员尚未在「部署设置 → 集成凭据」完成客户端凭据配置，"
            f"暂无法自助连接，请联系管理员完成配置后再试。"
        )
    else:
        msg = (
            f"当前部署已开启{names}功能，但管理员尚未完成公开回调地址或服务端加密配置，"
            f"暂无法自助连接，请联系管理员完成配置后再试。"
        )
    return _error_command(
        "provider_not_ready",
        msg,
        tool_call_id,
        providers=list(not_ready),
        ready=False,
    )


def _required_scopes_for(provider: str, action: str) -> tuple[str, ...]:
    if provider == "google":
        if action == "list_calendars":
            return (GOOGLE_SCOPE_CALENDAR_LIST_RO,)
        if action in _WRITE_ACTIONS:
            return GOOGLE_CALENDAR_MIN_SCOPES
        return (GOOGLE_SCOPE_CALENDAR_EVENTS,)
    if provider == "feishu":
        if action == "list_calendars":
            return ("calendar:calendar:readonly",)
        if action == "create_event":
            return ("calendar:calendar.event:create",)
        if action == "update_event":
            return ("calendar:calendar.event:update",)
        if action == "delete_event":
            return ("calendar:calendar.event:delete",)
        return ("calendar:calendar.event:read",)
    return FEISHU_CALENDAR_SCOPES


def _default_calendar_id(conn_row: IntegrationRow, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    prefs = conn_row.preferences or {}
    pref_id = str(prefs.get("primary_calendar_id") or "").strip()
    if pref_id:
        return pref_id
    return "primary"


def _extract_external_attendees(
    attendees: Sequence[Any] | None,
    *,
    owner_email: str | None,
) -> list[str]:
    """Return external attendee emails (excluding the calendar owner's own email)."""
    owner_norm = str(owner_email or "").strip().lower()
    external: list[str] = []
    seen: set[str] = set()
    for item in attendees or []:
        if isinstance(item, Mapping):
            if item.get("self"):
                continue
            email = str(item.get("email") or item.get("third_party_email") or "").strip()
        else:
            email = str(item or "").strip()
        if not email:
            continue
        low = email.lower()
        if owner_norm and low == owner_norm:
            continue
        if low not in seen:
            seen.add(low)
            external.append(email)
    return external


def _extract_first_decision(response: Any) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    decisions = response.get("decisions")
    if (
        not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
        or not decisions
    ):
        return {}
    first = decisions[0]
    return dict(first) if isinstance(first, Mapping) else {}


def _event_bound_value(event: Mapping[str, Any] | None, field: str) -> tuple[str | None, bool]:
    """Extract ISO/date string and ``is_all_day`` flag from a normalized event's ``start``/``end``."""
    if not isinstance(event, Mapping):
        return None, False
    obj = event.get(field)
    if not isinstance(obj, Mapping):
        return None, False
    if obj.get("date"):
        return str(obj["date"]), True
    if obj.get("dateTime"):
        return str(obj["dateTime"]), False
    return None, False


def _normalize_calendar_write_request(
    *,
    provider: str,
    action: str,
    calendar_id: str,
    event_id: str | None,
    summary: str | None,
    description: str | None,
    start_time: str | None,
    end_time: str | None,
    tz_name: str,
    all_day: bool,
    location: str | None,
    attendees: Sequence[str] | None,
    reminders: Sequence[Mapping[str, Any]] | None,
    use_default_reminders: bool | None,
    existing_event: Mapping[str, Any] | None,
    owner_email: str | None,
    account_email: str | None,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """Build ONE canonical normalized write specification used for BOTH card rendering and execution.

    Rejects provider-unsupported fields before any confirmation card is shown.
    Returns ``(spec, None)`` or ``(None, (error_code, error_message))``.
    """
    if provider == "feishu" and action == "update_event" and attendees is not None:
        return None, (
            "unsupported_field_for_provider",
            "飞书日历不支持在修改日程时变更参会人；如需调整参会人，请在飞书客户端修改或新建日程。",
        )
    if (
        provider == "feishu"
        and action in ("create_event", "update_event")
        and use_default_reminders is not None
        and reminders is None
    ):
        return None, (
            "unsupported_field_for_provider",
            "飞书日历不支持单独设置 use_default_reminders，请通过 reminders 指定具体提醒分钟数。",
        )

    eff_start = start_time.strip() if isinstance(start_time, str) and start_time.strip() else None
    eff_end = end_time.strip() if isinstance(end_time, str) and end_time.strip() else None
    eff_all_day = bool(all_day)

    # Single-bound time updates fill the omitted bound from existing_event so both
    # providers receive a complete, consistent interval and the card shows exact bounds.
    if action == "update_event" and (eff_start is not None) != (eff_end is not None):
        ex_start, ex_start_all_day = _event_bound_value(existing_event, "start")
        ex_end, ex_end_all_day = _event_bound_value(existing_event, "end")
        if eff_start is None and ex_start is not None:
            eff_start = ex_start
            if not eff_all_day and ex_start_all_day and ex_end_all_day:
                eff_all_day = True
        elif eff_end is None and ex_end is not None:
            eff_end = ex_end
            if not eff_all_day and ex_start_all_day and ex_end_all_day:
                eff_all_day = True

    cleaned_attendees: list[str] | None = (
        [str(a).strip() for a in attendees if str(a).strip()]
        if attendees is not None
        else None
    )

    ext_attendees = _extract_external_attendees(
        cleaned_attendees, owner_email=owner_email
    )
    if existing_event is not None:
        for email in _extract_external_attendees(
            existing_event.get("attendees"), owner_email=owner_email
        ):
            if email.lower() not in {e.lower() for e in ext_attendees}:
                ext_attendees.append(email)

    requires_second_confirm = len(ext_attendees) > 0
    ev_id_clean = event_id.strip() if event_id else None

    if provider == "google":
        if action == "delete_event":
            assert ev_id_clean is not None
            write_payload: dict[str, Any] = {
                "calendarId": calendar_id,
                "eventId": ev_id_clean,
                "notificationLevel": "ALL" if requires_second_confirm else "NONE",
            }
        else:
            write_payload = build_google_event_write_args(
                calendar_id=calendar_id,
                event_id=ev_id_clean,
                summary=summary,
                description=description,
                start_time=eff_start,
                end_time=eff_end,
                tz_name=tz_name,
                all_day=eff_all_day,
                location=location,
                attendees=cleaned_attendees,
                reminders=reminders,
                use_default_reminders=use_default_reminders,
                idempotency_key=idempotency_key if action == "create_event" else None,
            )
            if action == "update_event":
                mutable_keys = set(write_payload.keys()) - {"calendarId", "eventId"}
                if not mutable_keys:
                    return None, (
                        "empty_update",
                        "未指定任何可修改的日程字段，请至少提供一个待更新字段。",
                    )
    else:
        if action == "delete_event":
            assert ev_id_clean is not None
            write_payload = {
                "calendar_id": calendar_id,
                "event_id": ev_id_clean,
                "need_notification": requires_second_confirm,
            }
        else:
            write_payload = build_feishu_event_body(
                summary=summary,
                description=description,
                start_time=eff_start,
                end_time=eff_end,
                tz_name=tz_name,
                all_day=eff_all_day,
                location=location,
                reminders=reminders,
                need_notification=requires_second_confirm,
            )
            if action == "update_event":
                mutable_keys = set(write_payload.keys()) - {"need_notification"}
                if not mutable_keys:
                    return None, (
                        "empty_update",
                        "未指定任何可修改的日程字段，请至少提供一个待更新字段。",
                    )

    # Derive card display fields STRICTLY from the normalized write payload that will execute
    if provider == "google":
        card_summary = write_payload.get("summary")
        card_description = write_payload.get("description")
        card_location = write_payload.get("location")
        card_start = write_payload.get("startTime")
        card_end = write_payload.get("endTime")
        card_reminders = write_payload.get("overrideReminders")
        card_use_default_rem = write_payload.get("useDefaultReminders")
        card_attendees = write_payload.get("attendees")
        attendees_set = "attendees" in write_payload
        description_set = "description" in write_payload
        location_set = "location" in write_payload
        reminders_set = (
            "overrideReminders" in write_payload or "useDefaultReminders" in write_payload
        )
    else:
        card_summary = write_payload.get("summary")
        card_description = write_payload.get("description")
        loc_obj = write_payload.get("location")
        card_location = loc_obj.get("name") if isinstance(loc_obj, Mapping) else None
        card_start = eff_start if "start_time" in write_payload else None
        card_end = eff_end if "end_time" in write_payload else None
        card_reminders = write_payload.get("reminders")
        card_use_default_rem = None
        card_attendees = cleaned_attendees if action == "create_event" else None
        attendees_set = action == "create_event" and cleaned_attendees is not None
        description_set = "description" in write_payload
        location_set = "location" in write_payload
        reminders_set = "reminders" in write_payload

    before_attendees = (
        _extract_external_attendees(existing_event.get("attendees"), owner_email=owner_email)
        if existing_event is not None
        else []
    )
    target_attendees_clean = (
        [str(e).strip() for e in (card_attendees or []) if str(e).strip()]
        if attendees_set
        else []
    )
    if attendees_set:
        if action == "create_event":
            attendees_added = list(target_attendees_clean)
            attendees_removed: list[str] = []
            attendees_cleared = False
        else:
            before_low = {e.lower() for e in before_attendees}
            target_low = {e.lower() for e in target_attendees_clean}
            attendees_added = [
                e for e in target_attendees_clean if e.lower() not in before_low
            ]
            attendees_removed = [
                e for e in before_attendees if e.lower() not in target_low
            ]
            attendees_cleared = len(target_attendees_clean) == 0
    else:
        attendees_added = []
        attendees_removed = []
        attendees_cleared = False

    if action == "delete_event" and existing_event is not None:
        card_summary = existing_event.get("summary") or ""
        ex_start, ex_start_all_day = _event_bound_value(existing_event, "start")
        ex_end, ex_end_all_day = _event_bound_value(existing_event, "end")
        card_start = ex_start
        card_end = ex_end
        if ex_start_all_day and ex_end_all_day:
            eff_all_day = True
        if existing_event.get("location") is not None:
            card_location = str(existing_event.get("location") or "")
        if existing_event.get("description") is not None:
            card_description = str(existing_event.get("description") or "")

    card_args: dict[str, Any] = {
        "provider": provider,
        "provider_label": _provider_display_name(provider),
        "account_email": account_email,
        "calendar_id": calendar_id,
        "action": action,
        "event_id": ev_id_clean,
        "summary": card_summary
        if card_summary is not None
        else (existing_event.get("summary") if existing_event else ""),
        "start_time": card_start,
        "end_time": card_end,
        "timezone": tz_name,
        "all_day": eff_all_day,
        "location": card_location,
        "location_set": location_set,
        "description": card_description,
        "description_set": description_set,
        "reminders": card_reminders,
        "use_default_reminders": card_use_default_rem,
        "reminders_set": reminders_set,
        "attendees": card_attendees,
        "attendees_set": attendees_set,
        "attendees_added": attendees_added,
        "attendees_removed": attendees_removed,
        "attendees_cleared": attendees_cleared,
        "before_attendees": before_attendees,
        "external_attendees": ext_attendees,
        "before": dict(existing_event) if isinstance(existing_event, Mapping) else None,
        "normalized_payload": dict(write_payload),
    }

    return (
        {
            "write_payload": write_payload,
            "card_args": card_args,
            "attendees": cleaned_attendees if action == "create_event" else None,
            "external_attendees": ext_attendees,
            "requires_second_confirm": requires_second_confirm,
        },
        None,
    )


async def _notify_dm_for_web_approval(
    *,
    surface: PersonalSurfaceVerificationResult,
    summary_text: str,
) -> dict[str, Any]:
    """Build desensitized 1:1 DM notice metadata with web login deep link (§4.4 item 6)."""
    deep_link = build_private_thread_web_deep_link(surface.thread_id)
    return {
        "web_approval_url": deep_link,
        "dm_notice": (
            f"请前往网页端登录会话核对并确认日历操作（{summary_text}）：{deep_link}"
        ),
    }


def _build_elicitation_hitl_payload(
    *,
    approval_id: str,
    caller_user_id: str,
    tool_call_id: str,
    confirm_token: str,
    payload_sha: str,
    input_requests: Mapping[str, Any],
    mcp_tool_name: str,
    mcp_arguments: Mapping[str, Any],
    dm_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "description": "Google 日历需要你在网页端完成确认表单。",
        "name": "dalton_calendar",
        "tool_call_id": tool_call_id,
        "approval_id": approval_id,
        "caller_user_id": caller_user_id,
        "provider": "google",
        "service": "calendar",
        "confirm_token": confirm_token,
        "payload_sha256": payload_sha,
        "risk_level": "elicitation",
        "input_requests": dict(input_requests),
        "args": {
            "provider": "google",
            "tool": mcp_tool_name,
            "arguments": dict(mcp_arguments),
        },
    }
    if dm_meta:
        payload.update(dm_meta)
    return payload


def _build_write_hitl_payload(
    *,
    approval_id: str,
    caller_user_id: str,
    provider: str,
    tool_call_id: str,
    confirm_token: str,
    payload_sha: str,
    risk_level: str,
    requires_second_confirm: bool,
    description: str,
    card_args: Mapping[str, Any],
    dm_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "description": description,
        "name": "dalton_calendar",
        "tool_call_id": tool_call_id,
        "approval_id": approval_id,
        "caller_user_id": caller_user_id,
        "provider": provider,
        "service": "calendar",
        "confirm_token": confirm_token,
        "payload_sha256": payload_sha,
        "risk_level": risk_level,
        "requires_second_confirm": requires_second_confirm,
        "args": dict(card_args),
    }
    if dm_meta:
        payload.update(dm_meta)
    return payload


async def _call_google_mcp_with_elicitation(
    *,
    google_client: GoogleCalendarClientProtocol,
    caller_user_id: str,
    surface: PersonalSurfaceVerificationResult,
    token: FreshAccessToken,
    tool_name: str,
    arguments: Mapping[str, Any],
    required_scopes: Sequence[str],
    tool_call_id: str,
    config: RunnableConfig | None,
    stage_prefix: str,
    before_dispatch: Any = None,
    on_elicitation_pause: Any = None,
    before_resumed_call: Any = None,
    replay_resumed_elics: Any = None,
) -> tuple[dict[str, Any] | None, Command | None]:
    """Invoke Google Calendar MCP with durable multi-round ``input_required`` elicitation (§3.1, §5.2).

    Each elicitation round persists its own stage row keyed by ``f"{stage_prefix}:elic:{round_idx}"``
    in ``dalton_integration_action_approvals``. On LangGraph resume, completed rounds replay their
    ``hitl_confirm`` cursor without re-executing Google MCP, and pending rounds consume ONLY the
    original approval row and resume Google MCP with the persisted ``requestState``.
    """
    active_token = token
    request_state: str | None = None
    merged_responses: dict[str, dict[str, Any]] | None = None

    for round_idx in range(_MAX_ELICITATION_ROUNDS):
        stage_key = f"{stage_prefix}:elic:{round_idx}"
        existing: ActionApprovalRow | None = await db.get_action_approval_by_stage_key(
            caller_user_id,
            surface.thread_id,
            stage_key,
            decrypt_state=True,
        )

        if existing is not None and existing.elicitation_state:
            state = dict(existing.elicitation_state)
            input_reqs = dict(existing.input_requests_map or {})
            action_payload = _build_elicitation_hitl_payload(
                approval_id=existing.approval_id,
                caller_user_id=caller_user_id,
                tool_call_id=tool_call_id,
                confirm_token=str(state.get("confirm_token") or ""),
                payload_sha=existing.payload_sha256,
                input_requests=input_reqs,
                mcp_tool_name=str(state.get("mcp_tool_name") or tool_name),
                mcp_arguments=state.get("mcp_arguments") or arguments,
                dm_meta=state.get("dm_meta"),
            )
            approved, resume = hitl_confirm("dalton_google_mcp_elicitation", action_payload)

            # Already completed in a prior resume before a later round or interrupted before dispatch:
            # if no next elicitation round exists yet, we are about to dispatch the resumed call,
            # so ALWAYS run before_resumed_call (event version check) before continuing!
            if existing.status == "succeeded" and isinstance(
                state.get("submitted_responses"), Mapping
            ):
                request_state = str(state["request_state"])
                merged_responses = {
                    str(k): dict(v)
                    for k, v in state["submitted_responses"].items()
                    if isinstance(v, Mapping)
                }
                next_stage_key = f"{stage_prefix}:elic:{round_idx + 1}"
                next_existing = await db.get_action_approval_by_stage_key(
                    caller_user_id,
                    surface.thread_id,
                    next_stage_key,
                    decrypt_state=False,
                )
                if next_existing is None:
                    if before_resumed_call is not None:
                        recheck_err = await before_resumed_call(round_idx, active_token)
                        if recheck_err is not None:
                            return None, recheck_err
                elif replay_resumed_elics is not None:
                    await replay_resumed_elics(round_idx)
                continue

            err_cmd, next_state, next_merged, next_token = await _settle_elicitation_resume(
                approval_id=existing.approval_id,
                caller_user_id=caller_user_id,
                surface=surface,
                google_client=google_client,
                input_requests=input_reqs,
                saved_state=state,
                required_scopes=required_scopes,
                tool_call_id=tool_call_id,
                approved=approved,
                resume=resume,
            )
            if err_cmd is not None:
                return None, err_cmd
            assert next_state is not None and next_merged is not None and next_token is not None
            request_state = next_state
            merged_responses = next_merged
            active_token = next_token
            if before_resumed_call is not None:
                recheck_err = await before_resumed_call(round_idx, active_token)
                if recheck_err is not None:
                    return None, recheck_err
            continue

        # No persisted stage row for this round -> call Google MCP
        if before_dispatch is not None:
            await before_dispatch()
        try:
            result = await google_client.call_mcp_tool(
                access_token=active_token.access_token,
                tool_name=tool_name,
                arguments=arguments,
                request_state=request_state,
                input_responses=merged_responses,
            )
            return result, None
        except GoogleMCPElicitationRequired as elic:
            if _is_unattended_automation_turn(config):
                return None, _error_command(
                    "automation_turn_confirmation_forbidden",
                    "后台自动任务无法进行交互表单确认，已中止本次 Google 日历操作。",
                    tool_call_id,
                )
            if on_elicitation_pause is not None:
                await on_elicitation_pause()
            approval_id = str(uuid4())
            payload_sha = canonical_payload_sha256(
                {
                    "tool_name": elic.mcp_tool_name,
                    "arguments": elic.mcp_arguments,
                    "input_requests": elic.input_requests,
                }
            )
            confirm_token = compute_elicitation_confirm_token(
                approval_id=approval_id,
                caller_user_id=caller_user_id,
                provider="google",
                service="calendar",
                provider_account_id=active_token.provider_account_id,
                auth_epoch=active_token.auth_epoch,
                payload_sha256=payload_sha,
            )
            expires_at = datetime.now(UTC) + timedelta(
                seconds=ELICITATION_APPROVAL_TTL_SECONDS
            )
            dm_meta = (
                await _notify_dm_for_web_approval(
                    surface=surface, summary_text="Google 日历安全确认"
                )
                if surface.is_dm_channel
                else None
            )
            saved_state: dict[str, Any] = {
                "mcp_endpoint": elic.mcp_endpoint,
                "mcp_tool_name": elic.mcp_tool_name,
                "mcp_arguments": dict(elic.mcp_arguments),
                "request_state": elic.request_state,
                "prior_responses": dict(merged_responses or {}),
                "confirm_token": confirm_token,
                "dm_meta": dm_meta,
            }
            await db.create_action_approval(
                approval_id=approval_id,
                caller_user_id=caller_user_id,
                thread_id=surface.thread_id,
                run_id=surface.response_id or None,
                action_type="dalton_google_mcp_elicitation",
                provider="google",
                service="calendar",
                provider_account_id=active_token.provider_account_id,
                auth_epoch=active_token.auth_epoch,
                token_generation=active_token.token_generation,
                payload_sha256=payload_sha,
                input_requests_map=elic.input_requests,
                elicitation_state=saved_state,
                idempotent_message_id=stage_key,
                expires_at=expires_at,
            )

            action_payload = _build_elicitation_hitl_payload(
                approval_id=approval_id,
                caller_user_id=caller_user_id,
                tool_call_id=tool_call_id,
                confirm_token=confirm_token,
                payload_sha=payload_sha,
                input_requests=elic.input_requests,
                mcp_tool_name=elic.mcp_tool_name,
                mcp_arguments=elic.mcp_arguments,
                dm_meta=dm_meta,
            )
            approved, resume = hitl_confirm("dalton_google_mcp_elicitation", action_payload)

            err_cmd, next_state, next_merged, next_token = await _settle_elicitation_resume(
                approval_id=approval_id,
                caller_user_id=caller_user_id,
                surface=surface,
                google_client=google_client,
                input_requests=elic.input_requests,
                saved_state=saved_state,
                required_scopes=required_scopes,
                tool_call_id=tool_call_id,
                approved=approved,
                resume=resume,
            )
            if err_cmd is not None:
                return None, err_cmd
            assert next_state is not None and next_merged is not None and next_token is not None
            request_state = next_state
            merged_responses = next_merged
            active_token = next_token
            if before_resumed_call is not None:
                recheck_err = await before_resumed_call(round_idx, active_token)
                if recheck_err is not None:
                    return None, recheck_err

    return None, _error_command(
        "elicitation_round_limit",
        "Google 日历连续确认次数超过上限，请稍后重试。",
        tool_call_id,
    )


async def _settle_elicitation_resume(
    *,
    approval_id: str,
    caller_user_id: str,
    surface: PersonalSurfaceVerificationResult,
    google_client: GoogleCalendarClientProtocol,
    input_requests: Mapping[str, Any],
    saved_state: Mapping[str, Any],
    required_scopes: Sequence[str],
    tool_call_id: str,
    approved: bool,
    resume: Mapping[str, Any],
) -> tuple[Command | None, str | None, dict[str, dict[str, Any]] | None, FreshAccessToken | None]:
    """Validate and atomically consume ONE original elicitation approval row."""
    if isinstance(resume, Mapping) and resume.get("scope_changed"):
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return (
            _error_command(
                "scope_changed",
                "确认期间对话私密状态或日历连接已发生变化，已作废本次确认，请重新发起。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    decision = _extract_first_decision(resume)
    dec_type = str(decision.get("type") or "").strip().lower()
    provided_elic = decision.get("elicitation_responses")
    mcp_tool_name = str(saved_state.get("mcp_tool_name") or "")
    mcp_arguments = saved_state.get("mcp_arguments") or {}
    saved_request_state = str(saved_state.get("request_state") or "")

    if not approved or dec_type == "reject":
        await db.mark_action_approval_status(
            approval_id, "rejected", caller_user_id=caller_user_id
        )
        cancel_responses = (
            dict(provided_elic)
            if isinstance(provided_elic, Mapping) and provided_elic
            else {k: {"action": "decline"} for k in input_requests}
        )
        try:
            refreshed = await ensure_fresh_access_token(
                caller_user_id,
                "google",
                "calendar",
                min_remaining_seconds=600,
                required_scopes=required_scopes,
            )
            await google_client.call_mcp_tool(
                access_token=refreshed.access_token,
                tool_name=mcp_tool_name,
                arguments=mcp_arguments,
                request_state=saved_request_state,
                input_responses=cancel_responses,
            )
        except Exception:
            logger.debug(
                "[dalton_calendar] Google MCP decline cleanup notice failed (ignored)"
            )
        return (
            _error_command(
                "user_cancelled",
                "你已取消本次 Google 日历操作。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    if not isinstance(provided_elic, Mapping):
        await db.mark_action_approval_status(
            approval_id, "rejected", caller_user_id=caller_user_id
        )
        return (
            _error_command(
                "invalid_elicitation_response",
                "缺少有效的表单确认响应，已取消本次操作。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    ok, err, is_decline = validate_elicitation_responses(input_requests, provided_elic)
    if not ok:
        await db.mark_action_approval_status(
            approval_id, "rejected", caller_user_id=caller_user_id
        )
        return (
            _error_command(
                "invalid_elicitation_response",
                err or "表单确认响应无效。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    if is_decline:
        await db.mark_action_approval_status(
            approval_id, "rejected", caller_user_id=caller_user_id
        )
        try:
            refreshed = await ensure_fresh_access_token(
                caller_user_id,
                "google",
                "calendar",
                min_remaining_seconds=600,
                required_scopes=required_scopes,
            )
            await google_client.call_mcp_tool(
                access_token=refreshed.access_token,
                tool_name=mcp_tool_name,
                arguments=mcp_arguments,
                request_state=saved_request_state,
                input_responses=dict(provided_elic),
            )
        except Exception:
            logger.debug(
                "[dalton_calendar] Google MCP cancel/decline cleanup failed (ignored)"
            )
        return (
            _error_command(
                "user_cancelled",
                "你已取消本次 Google 日历操作。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    consumed = await db.consume_action_approval(
        approval_id, caller_user_id, decrypt_state=True
    )
    if consumed is None or not consumed.elicitation_state:
        return (
            _error_command(
                "approval_expired_or_consumed",
                "确认已过期或已使用，请重新发起。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    if not await check_thread_surface_still_private(surface.thread_id, caller_user_id):
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return (
            _error_command(
                "scope_changed",
                "确认期间对话私密状态已变更，已中止本次操作。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    active_token = await ensure_fresh_access_token(
        caller_user_id,
        "google",
        "calendar",
        min_remaining_seconds=600,
        required_scopes=required_scopes,
    )
    if (
        active_token.auth_epoch != consumed.auth_epoch
        or active_token.provider_account_id != consumed.provider_account_id
    ):
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return (
            _error_command(
                "scope_changed",
                "确认期间 Google 日历连接已更新，请重新发起操作。",
                tool_call_id,
            ),
            None,
            None,
            None,
        )

    state = dict(consumed.elicitation_state)
    next_request_state = str(state["request_state"])
    prior = state.get("prior_responses")
    next_responses: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in prior.items() if isinstance(v, Mapping)}
        if isinstance(prior, Mapping)
        else {}
    )
    for k, v in provided_elic.items():
        next_responses[str(k)] = dict(v)

    updated_state = {**state, "submitted_responses": next_responses}
    await db.update_action_approval_state(
        approval_id,
        caller_user_id=caller_user_id,
        status="succeeded",
        elicitation_state=updated_state,
    )
    return None, next_request_state, next_responses, active_token


def create_calendar_tools(
    *,
    google_client: GoogleCalendarClientProtocol | None = None,
    feishu_client: FeishuCalendarClientProtocol | None = None,
) -> list[BaseTool]:
    """Build the ``dalton_calendar`` direct tool for the main agent (§5.1)."""

    def _google_cli() -> GoogleCalendarClientProtocol:
        return google_client if google_client is not None else get_google_calendar_client()

    def _feishu_cli() -> FeishuCalendarClientProtocol:
        return feishu_client if feishu_client is not None else get_feishu_calendar_client()

    async def _attach_persisted_notice(thread_id: str, res: dict[str, Any]) -> dict[str, Any]:
        notice = await claim_first_touch_notice_if_needed(thread_id, service="calendar")
        if notice:
            res["first_touch_notice"] = notice
        return res

    @tool("dalton_calendar")
    async def dalton_calendar(
        action: CalendarReadActionArg,
        provider: CalendarProviderLiteral = "auto",
        calendar_id: str | None = None,
        event_id: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        timezone: str | None = None,
        query: str | None = None,
        limit: int = 50,
        duration_minutes: int = 30,
        config: RunnableConfig = None,  # type: ignore[assignment]
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """查看当前用户本人已连接的个人日历（支持 Google 日历与飞书日历，当前版本为只读查询）。

        使用说明：
        - 适用场景：仅在当前用户本人的私人 Home 会话、一对一私聊，或仅本人可见的私密工作区中使用。首次在某个对话里访问个人日历时，系统会将私密提示保存为本轮会话事件并在界面展示与回放。
        - 平台选择（`provider`）：默认 `"auto"`。仅连接一个日历平台时自动选用；同时连接 Google 日历与飞书日历时，列出日历、列出日程和查询空闲会自动合并两边结果并标注来源；按事件编号查询单个日程时，先向用户确认目标平台再指定 `"google"` 或 `"feishu"`。
        - 支持的只读动作（`action`）：
          - `"list_calendars"`：列出当前用户有权限查看的日历列表。
          - `"list_events"`：查询指定时间范围（`time_min` 至 `time_max`，ISO 8601 国际标准时间格式，如 `2026-12-01T00:00:00+08:00`）内的日程，可附带 `query` 关键词筛选和 `timezone`（IANA 互联网号码分配局时区名，如 `Asia/Shanghai`、`America/New_York`）。若返回 `truncated=true`，说明时间窗内日程超出翻页上限未列全，需明确告知用户并建议缩小时间范围或加上关键词。
          - `"get_event"`：按 `event_id` 读取单个日程详情。
          - `"check_availability"`：查询 `time_min` 至 `time_max` 之间的忙碌时段与长度不少于 `duration_minutes` 分钟的空闲时段。当返回的 `availability.status` 为 `"unknown"` 时，需明确告知用户当前无法确认空闲（存在需重新授权、读取失败或分页截断的日历），并将空的 `free` 数组说明为状态未知而非没有空档。
        """
        act = str(action or "").strip().lower()
        if act not in _READ_ACTIONS and act not in _WRITE_ACTIONS:
            return _error_command(
                "invalid_action",
                f"不支持的日历动作 '{action}'。",
                tool_call_id,
            )

        # Batch 1 entry gate: personal calendar is interactive-conversation only.
        if _is_unattended_automation_turn(config):
            return _error_command(
                "unattended_turn_unsupported",
                UNATTENDED_CALENDAR_REFUSAL_MESSAGE,
                tool_call_id,
            )

        # Batch 1 scope: read-only personal calendar; reject all write actions at entry
        # before querying connections, refreshing tokens, or touching external APIs.
        if act in _WRITE_ACTIONS:
            return _error_command(
                "write_actions_disabled",
                CALENDAR_WRITE_DISABLED_MESSAGE,
                tool_call_id,
            )

        enabled_providers = _enabled_calendar_providers()
        if not enabled_providers:
            return _error_command(
                "capability_disabled",
                "当前部署尚未开启个人日历功能，请联系管理员在部署设置中启用 Google 日历或飞书日历。",
                tool_call_id,
            )

        req_provider = str(provider or "auto").strip().lower()
        if req_provider not in ("auto", "google", "feishu"):
            return _error_command(
                "invalid_provider",
                "provider 仅支持 'auto'、'google' 或 'feishu'。",
                tool_call_id,
            )
        if req_provider != "auto" and req_provider not in enabled_providers:
            return _error_command(
                "provider_capability_disabled",
                f"当前部署尚未开启{_provider_display_name(req_provider)}功能。",
                tool_call_id,
            )

        surface_ctx = extract_surface_context(config)
        caller_user_id = surface_ctx["caller_user_id"]
        thread_id = surface_ctx["thread_id"]
        if not caller_user_id:
            return _error_command(
                "missing_identity",
                "无法识别当前用户身份，请在本人登录会话中使用个人日历。",
                tool_call_id,
            )

        cfg = (config or {}).get("configurable") if isinstance(config, Mapping) else {}
        tz_name = str(
            timezone
            or (cfg.get("timezone") if isinstance(cfg, Mapping) else None)
            or "Asia/Shanghai"
        ).strip()

        # Resolve user's calendar connections (metadata only; tokens fetched via ensure_fresh_access_token)
        all_conns = await db.list_user_connections(caller_user_id)
        cal_conns: dict[str, IntegrationRow] = {
            c.provider: c
            for c in all_conns
            if c.service == "calendar" and c.provider in enabled_providers
        }
        connected_rows: dict[str, IntegrationRow] = {
            p: c
            for p, c in cal_conns.items()
            if c.status in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
        }
        reauth_rows: dict[str, IntegrationRow] = {
            p: c
            for p, c in cal_conns.items()
            if c.status == IntegrationStatus.NEEDS_REAUTH
        }

        target_providers: list[str]
        if req_provider == "auto":
            if not connected_rows:
                if reauth_rows:
                    names = "、".join(_provider_display_name(p) for p in sorted(reauth_rows))
                    return _error_command(
                        "needs_reauth",
                        f"你的{names}授权需要重新连接，请前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）完成重新授权后再试。",
                        tool_call_id,
                        settings_path=_SETTINGS_INTEGRATIONS_PATH,
                        providers=sorted(reauth_rows),
                    )
                not_ready_cmd = _provider_not_ready_command(enabled_providers, tool_call_id)
                if not_ready_cmd is not None:
                    return not_ready_cmd
                summary_map = get_connector_providers_summary()
                ready_providers = [
                    p for p in enabled_providers if bool((summary_map.get(p) or {}).get("ready"))
                ] or enabled_providers
                avail_names = "或".join(_provider_display_name(p) for p in ready_providers)
                return _error_command(
                    "not_connected",
                    f"你尚未连接个人日历。前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）连接{avail_names}后，即可在私密对话中查询和管理日程。",
                    tool_call_id,
                    settings_path=_SETTINGS_INTEGRATIONS_PATH,
                    available_providers=ready_providers,
                )
            if len(connected_rows) == 1:
                target_providers = [next(iter(connected_rows.keys()))]
            elif act in ("list_calendars", "list_events", "check_availability"):
                target_providers = [p for p in ("google", "feishu") if p in connected_rows]
            else:
                return _tool_command(
                    {
                        "ok": False,
                        "needs_provider_choice": True,
                        "available_providers": sorted(connected_rows.keys()),
                        "message": (
                            "你同时连接了 Google 日历和飞书日历，请先确认要在哪个日历平台执行此操作"
                            "（google 或 feishu），再重新调用。"
                        ),
                    },
                    tool_call_id,
                )
        else:
            if req_provider not in connected_rows:
                if req_provider in reauth_rows:
                    return _error_command(
                        "needs_reauth",
                        f"你的{_provider_display_name(req_provider)}授权需要重新连接，请前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）完成重新授权后再试。",
                        tool_call_id,
                        settings_path=_SETTINGS_INTEGRATIONS_PATH,
                        provider=req_provider,
                    )
                not_ready_cmd = _provider_not_ready_command([req_provider], tool_call_id)
                if not_ready_cmd is not None:
                    return not_ready_cmd
                return _error_command(
                    "not_connected",
                    f"你尚未连接{_provider_display_name(req_provider)}。前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）完成连接后即可使用。",
                    tool_call_id,
                    settings_path=_SETTINGS_INTEGRATIONS_PATH,
                    provider=req_provider,
                )
            target_providers = [req_provider]

        # Validate action-specific arguments before touching DB/upstream
        if act in ("list_events", "check_availability"):
            if not time_min or not time_max:
                now_utc = datetime.now(UTC)
                time_min = time_min or now_utc.isoformat()
                time_max = time_max or (now_utc + timedelta(days=7)).isoformat()
        elif act == "get_event":
            if not event_id or not event_id.strip():
                return _error_command(
                    "missing_event_id",
                    f"执行 {act} 需要提供 event_id。",
                    tool_call_id,
                )

        # STEP 1: Pre-check private surface WITHOUT recording touch yet (§4.4, Row 9)
        pre_surfaces: dict[str, PersonalSurfaceVerificationResult] = {}
        for p in target_providers:
            try:
                pre_surfaces[p] = await verify_personal_integration_surface(
                    config,
                    provider=p,
                    service="calendar",
                    record_touch_now=False,
                )
            except PersonalSurfaceDeniedError as exc:
                return _error_command(
                    exc.code,
                    exc.message,
                    tool_call_id,
                    retry_later=exc.is_purge_wait,
                )

        # ------------------------------------------------------------------
        # READ ACTIONS
        # Sequence per provider: token fetch -> atomic re-verify & record_touch
        # using actual token's (auth_epoch, provider_account_id) -> calendar API
        # ------------------------------------------------------------------
        if act == "list_calendars":
            calendars: list[dict[str, Any]] = []
            errors_by_provider: dict[str, str] = {}
            auth_err_cmd: Command | None = None
            for p in target_providers:
                try:
                    tok = await ensure_fresh_access_token(
                        caller_user_id,
                        p,
                        "calendar",
                        min_remaining_seconds=600,
                        required_scopes=_required_scopes_for(p, act),
                    )
                    await verify_personal_integration_surface(
                        config,
                        provider=p,
                        service="calendar",
                        auth_epoch=tok.auth_epoch,
                        provider_account_id=tok.provider_account_id,
                        expected_auth_epoch=tok.auth_epoch,
                        expected_provider_account_id=tok.provider_account_id,
                        record_touch_now=True,
                    )
                    if p == "google":
                        calendars.extend(
                            await _google_cli().list_calendars(access_token=tok.access_token)
                        )
                    else:
                        calendars.extend(
                            await _feishu_cli().list_calendars(access_token=tok.access_token)
                        )
                except PersonalSurfaceDeniedError as exc:
                    return _error_command(
                        exc.code,
                        exc.message,
                        tool_call_id,
                        retry_later=exc.is_purge_wait,
                    )
                except (
                    MissingRequiredScopesError,
                    IntegrationNeedsReauthError,
                    IntegrationNotConnectedError,
                ) as exc:
                    errors_by_provider[p] = str(exc)
                    if len(target_providers) == 1:
                        auth_err_cmd = _auth_error_command(
                            exc, provider=p, tool_call_id=tool_call_id
                        )
                except (
                    PersonalIntegrationError,
                    GoogleCalendarAPIError,
                    FeishuCalendarAPIError,
                ) as exc:
                    errors_by_provider[p] = str(exc)

            if auth_err_cmd is not None:
                return auth_err_cmd
            if not calendars and errors_by_provider:
                return _error_command(
                    "calendar_read_failed",
                    "读取日历列表失败：" + "；".join(errors_by_provider.values()),
                    tool_call_id,
                    errors=errors_by_provider,
                )
            return _tool_command(
                await _attach_persisted_notice(
                    thread_id,
                    {
                        "ok": True,
                        "action": "list_calendars",
                        "providers": target_providers,
                        "calendars": calendars,
                        "count": len(calendars),
                        **({"partial_errors": errors_by_provider} if errors_by_provider else {}),
                    },
                ),
                tool_call_id,
            )

        if act in ("list_events", "check_availability"):
            assert time_min is not None and time_max is not None
            merged_events: list[dict[str, Any]] = []
            errors_by_provider = {}
            incomplete_providers: dict[str, str] = {}
            truncated_providers: dict[str, str] = {}
            reauth_gap_providers: list[str] = []
            auth_err_cmd = None
            is_availability = act == "check_availability"

            # When querying with provider="auto", any connection in ``needs_reauth`` is
            # a real coverage gap: never report ``check_availability`` as complete/free.
            if req_provider == "auto" and reauth_rows:
                for rp in sorted(reauth_rows):
                    reauth_gap_providers.append(rp)
                    incomplete_providers[rp] = "needs_reauth"
                    errors_by_provider[rp] = (
                        f"{_provider_display_name(rp)}授权需要重新连接，请前往「设置 → 集成」"
                        f"（{_SETTINGS_INTEGRATIONS_PATH}）完成重新授权。"
                    )

            for p in target_providers:
                conn_row = connected_rows[p]
                cal_id = _default_calendar_id(conn_row, calendar_id)
                req_scopes = _required_scopes_for(p, act)
                try:
                    tok = await ensure_fresh_access_token(
                        caller_user_id,
                        p,
                        "calendar",
                        min_remaining_seconds=600,
                        required_scopes=req_scopes,
                    )
                    surf = await verify_personal_integration_surface(
                        config,
                        provider=p,
                        service="calendar",
                        auth_epoch=tok.auth_epoch,
                        provider_account_id=tok.provider_account_id,
                        expected_auth_epoch=tok.auth_epoch,
                        expected_provider_account_id=tok.provider_account_id,
                        record_touch_now=True,
                    )
                    if p == "google":
                        next_page_token: str | None = None
                        max_pages = _MAX_AVAILABILITY_PAGES
                        page_size = 250 if is_availability else max(1, min(int(limit), 250))
                        for page_idx in range(max_pages):
                            mcp_args = build_google_list_events_args(
                                calendar_id=cal_id,
                                time_min=time_min,
                                time_max=time_max,
                                tz_name=tz_name,
                                query=query if act == "list_events" else None,
                                limit=page_size,
                                page_token=next_page_token,
                            )
                            mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                                google_client=_google_cli(),
                                caller_user_id=caller_user_id,
                                surface=surf,
                                token=tok,
                                tool_name="list_events",
                                arguments=mcp_args,
                                required_scopes=req_scopes,
                                tool_call_id=tool_call_id,
                                config=config,
                                stage_prefix=f"tc:{tool_call_id}:read_p{page_idx}",
                            )
                            if err_cmd is not None:
                                return err_cmd
                            res_map = mcp_res or {}
                            raw_items = res_map.get("events") or res_map.get("items") or []
                            if isinstance(raw_items, list):
                                for item in raw_items:
                                    if isinstance(item, Mapping):
                                        merged_events.append(
                                            normalize_google_event(item, calendar_id=cal_id)
                                        )
                            next_tok_val = str(
                                res_map.get("nextPageToken")
                                or res_map.get("next_page_token")
                                or ""
                            ).strip()
                            if not next_tok_val:
                                next_page_token = None
                                break
                            next_page_token = next_tok_val
                        if next_page_token:
                            truncated_providers[p] = "pagination_truncated"
                            if is_availability:
                                incomplete_providers[p] = "pagination_truncated"
                    else:
                        f_page_token: str | None = None
                        f_truncated = False
                        f_page_size = 500 if is_availability else max(50, min(int(limit), 500))
                        for _page_idx in range(_MAX_AVAILABILITY_PAGES):
                            page_res = await _feishu_cli().list_events_page(
                                access_token=tok.access_token,
                                calendar_id=cal_id,
                                start_time=time_min,
                                end_time=time_max,
                                tz_name=tz_name,
                                page_size=f_page_size,
                                page_token=f_page_token,
                            )
                            items = list(page_res.get("items") or [])
                            if not is_availability and query and query.strip():
                                q_low = query.strip().lower()
                                items = [
                                    ev
                                    for ev in items
                                    if q_low in str(ev.get("summary") or "").lower()
                                    or q_low in str(ev.get("description") or "").lower()
                                ]
                            merged_events.extend(items)
                            has_more = bool(page_res.get("has_more"))
                            next_f_tok = str(page_res.get("page_token") or "").strip() or None
                            if not has_more:
                                f_truncated = False
                                break
                            if not next_f_tok:
                                f_truncated = True
                                break
                            f_page_token = next_f_tok
                            f_truncated = True
                        if f_truncated:
                            truncated_providers[p] = "pagination_truncated"
                            if is_availability:
                                incomplete_providers[p] = "pagination_truncated"
                except GraphInterrupt:
                    raise
                except PersonalSurfaceDeniedError as exc:
                    return _error_command(
                        exc.code,
                        exc.message,
                        tool_call_id,
                        retry_later=exc.is_purge_wait,
                    )
                except (
                    MissingRequiredScopesError,
                    IntegrationNeedsReauthError,
                    IntegrationNotConnectedError,
                ) as exc:
                    errors_by_provider[p] = str(exc)
                    incomplete_providers[p] = getattr(exc, "code", "auth_failed")
                    if len(target_providers) == 1 and not reauth_gap_providers:
                        auth_err_cmd = _auth_error_command(
                            exc, provider=p, tool_call_id=tool_call_id
                        )
                except (
                    PersonalIntegrationError,
                    GoogleCalendarAPIError,
                    FeishuCalendarAPIError,
                ) as exc:
                    errors_by_provider[p] = str(exc)
                    incomplete_providers[p] = "provider_error"

            if auth_err_cmd is not None:
                return auth_err_cmd
            if not merged_events and errors_by_provider and not reauth_gap_providers:
                return _error_command(
                    "calendar_read_failed",
                    "读取日程失败：" + "；".join(errors_by_provider.values()),
                    tool_call_id,
                    errors=errors_by_provider,
                )

            merged_events.sort(
                key=lambda ev: str(
                    (ev.get("start") or {}).get("dateTime")
                    or (ev.get("start") or {}).get("date")
                    or ""
                )
            )
            if is_availability:
                availability = compute_free_busy_slots(
                    merged_events,
                    time_min=time_min,
                    time_max=time_max,
                    tz_name=tz_name,
                    min_slot_minutes=duration_minutes,
                )
                if incomplete_providers or errors_by_provider:
                    reasons = dict(incomplete_providers)
                    for ep in errors_by_provider:
                        reasons.setdefault(ep, "provider_error")
                    availability["status"] = "unknown"
                    availability["coverage_complete"] = False
                    availability["free"] = []
                    availability["incomplete_providers"] = reasons
                    if reauth_gap_providers:
                        reauth_names = "、".join(
                            _provider_display_name(rp) for rp in reauth_gap_providers
                        )
                        availability["reauth_providers"] = reauth_gap_providers
                        availability["settings_path"] = _SETTINGS_INTEGRATIONS_PATH
                        availability["unknown_reason"] = (
                            f"{reauth_names}需要重新授权（请前往「设置 → 集成」完成重新授权），"
                            f"空闲时段覆盖不完整，无法确认空闲。"
                        )
                    else:
                        availability["unknown_reason"] = (
                            "部分日历平台读取失败或分页未覆盖完整时间窗，空闲时段状态未知，不能确认空闲。"
                        )
                else:
                    availability["status"] = "complete"
                    availability["coverage_complete"] = True
                return _tool_command(
                    await _attach_persisted_notice(
                        thread_id,
                        {
                            "ok": True,
                            "action": "check_availability",
                            "providers": target_providers,
                            "availability": availability,
                            "event_count": len(merged_events),
                            **(
                                {"partial_errors": errors_by_provider}
                                if errors_by_provider
                                else {}
                            ),
                        },
                    ),
                    tool_call_id,
                )
            return _tool_command(
                await _attach_persisted_notice(
                    thread_id,
                    {
                        "ok": True,
                        "action": "list_events",
                        "providers": target_providers,
                        "timezone": tz_name,
                        "events": merged_events,
                        "count": len(merged_events),
                        "truncated": bool(truncated_providers),
                        **(
                            {
                                "truncated_providers": truncated_providers,
                                "truncation_notice": (
                                    "时间窗内日程超出翻页上限，当前结果未列全；"
                                    "请缩小时间范围或补充关键词后重试。"
                                ),
                            }
                            if truncated_providers
                            else {}
                        ),
                        **({"partial_errors": errors_by_provider} if errors_by_provider else {}),
                    },
                ),
                tool_call_id,
            )

        if act == "get_event":
            p = target_providers[0]
            conn_row = connected_rows[p]
            cal_id = _default_calendar_id(conn_row, calendar_id)
            req_scopes = _required_scopes_for(p, act)
            assert event_id is not None
            try:
                tok = await ensure_fresh_access_token(
                    caller_user_id,
                    p,
                    "calendar",
                    min_remaining_seconds=600,
                    required_scopes=req_scopes,
                )
                surf = await verify_personal_integration_surface(
                    config,
                    provider=p,
                    service="calendar",
                    auth_epoch=tok.auth_epoch,
                    provider_account_id=tok.provider_account_id,
                    expected_auth_epoch=tok.auth_epoch,
                    expected_provider_account_id=tok.provider_account_id,
                    record_touch_now=True,
                )
                if p == "google":
                    mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                        google_client=_google_cli(),
                        caller_user_id=caller_user_id,
                        surface=surf,
                        token=tok,
                        tool_name="get_event",
                        arguments={"calendarId": cal_id, "eventId": event_id.strip()},
                        required_scopes=req_scopes,
                        tool_call_id=tool_call_id,
                        config=config,
                        stage_prefix=f"tc:{tool_call_id}:get",
                    )
                    if err_cmd is not None:
                        return err_cmd
                    raw_ev = (mcp_res or {}).get("event") or mcp_res or {}
                    ev = normalize_google_event(raw_ev, calendar_id=cal_id)
                else:
                    ev = await _feishu_cli().get_event(
                        access_token=tok.access_token,
                        calendar_id=cal_id,
                        event_id=event_id.strip(),
                        tz_name=tz_name,
                    )
                return _tool_command(
                    await _attach_persisted_notice(
                        thread_id, {"ok": True, "action": "get_event", "event": ev}
                    ),
                    tool_call_id,
                )
            except GraphInterrupt:
                raise
            except PersonalSurfaceDeniedError as exc:
                return _error_command(
                    exc.code,
                    exc.message,
                    tool_call_id,
                    retry_later=exc.is_purge_wait,
                )
            except (
                MissingRequiredScopesError,
                IntegrationNeedsReauthError,
                IntegrationNotConnectedError,
            ) as exc:
                return _auth_error_command(exc, provider=p, tool_call_id=tool_call_id)
            except (
                PersonalIntegrationError,
                GoogleCalendarAPIError,
                FeishuCalendarAPIError,
            ) as exc:
                return _error_command(
                    "calendar_read_failed",
                    str(exc),
                    tool_call_id,
                )

        return _error_command(
            "write_actions_disabled",
            CALENDAR_WRITE_DISABLED_MESSAGE,
            tool_call_id,
        )

    # Retained unreachable write-path scaffold for the next batch (closed in Batch 1 read-only).
    async def _retained_unreachable_calendar_write(  # pragma: no cover
        *,
        act: str,
        target_providers: list[str],
        connected_rows: dict[str, IntegrationRow],
        pre_surfaces: dict[str, PersonalSurfaceVerificationResult],
        caller_user_id: str,
        thread_id: str,
        tz_name: str,
        calendar_id: str | None,
        event_id: str | None,
        summary: str | None,
        description: str | None,
        start_time: str | None,
        end_time: str | None,
        all_day: bool,
        location: str | None,
        attendees: list[str] | None,
        reminders: list[dict[str, Any]] | None,
        use_default_reminders: bool | None,
        config: RunnableConfig,
        tool_call_id: str,
    ) -> Command:
        if thread_id and tool_call_id:
            write_stage_key = f"tc:{tool_call_id}:write"
            existing_write = await db.get_action_approval_by_stage_key(
                caller_user_id,
                thread_id,
                write_stage_key,
                decrypt_state=True,
            )
            if existing_write is not None and existing_write.elicitation_state:
                return await _resume_frozen_calendar_write(
                    existing_write=existing_write,
                    caller_user_id=caller_user_id,
                    thread_id=thread_id,
                    tool_call_id=tool_call_id,
                    config=config,
                    google_cli=_google_cli(),
                    feishu_cli=_feishu_cli(),
                    attach_notice=_attach_persisted_notice,
                )

        if _is_unattended_automation_turn(config):
            return _error_command(
                "automation_turn_confirmation_forbidden",
                "后台自动任务无法弹出人工确认卡片，请在本人私密对话中操作个人日历写入。",
                tool_call_id,
            )

        p = target_providers[0]
        conn_row = connected_rows[p]
        surf = pre_surfaces[p]
        cal_id = _default_calendar_id(conn_row, calendar_id)
        req_scopes = _required_scopes_for(p, act)

        # Early reject unsupported provider fields BEFORE acquiring tokens or pre-reading
        if p == "feishu" and act == "update_event" and attendees is not None:
            return _error_command(
                "unsupported_field_for_provider",
                "飞书日历不支持在修改日程时变更参会人；如需调整参会人，请在飞书客户端修改或新建日程。",
                tool_call_id,
            )
        if (
            p == "feishu"
            and act in ("create_event", "update_event")
            and use_default_reminders is not None
            and reminders is None
        ):
            return _error_command(
                "unsupported_field_for_provider",
                "飞书日历不支持单独设置 use_default_reminders，请通过 reminders 指定具体提醒分钟数。",
                tool_call_id,
            )

        try:
            tok = await ensure_fresh_access_token(
                caller_user_id,
                p,
                "calendar",
                min_remaining_seconds=600,
                required_scopes=req_scopes,
            )
        except (
            MissingRequiredScopesError,
            IntegrationNeedsReauthError,
            IntegrationNotConnectedError,
            PersonalIntegrationError,
        ) as exc:
            return _auth_error_command(exc, provider=p, tool_call_id=tool_call_id)

        # Pre-read existing event on update/delete for diff, version marker & attendee detection (§5.2)
        existing_event: dict[str, Any] | None = None
        pre_read_marker: str | None = None
        if act in ("update_event", "delete_event"):
            assert event_id is not None
            try:
                # Atomically re-verify private surface + actual token generation & record touch before reading existing event
                surf = await verify_personal_integration_surface(
                    config,
                    provider=p,
                    service="calendar",
                    auth_epoch=tok.auth_epoch,
                    provider_account_id=tok.provider_account_id,
                    expected_auth_epoch=tok.auth_epoch,
                    expected_provider_account_id=tok.provider_account_id,
                    record_touch_now=True,
                )
                if p == "google":
                    mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                        google_client=_google_cli(),
                        caller_user_id=caller_user_id,
                        surface=surf,
                        token=tok,
                        tool_name="get_event",
                        arguments={"calendarId": cal_id, "eventId": event_id.strip()},
                        required_scopes=_required_scopes_for(p, "get_event"),
                        tool_call_id=tool_call_id,
                        config=config,
                        stage_prefix=f"tc:{tool_call_id}:preread",
                    )
                    if err_cmd is not None:
                        return err_cmd
                    raw_ev = (mcp_res or {}).get("event") or mcp_res or {}
                    existing_event = normalize_google_event(raw_ev, calendar_id=cal_id)
                else:
                    existing_event = await _feishu_cli().get_event(
                        access_token=tok.access_token,
                        calendar_id=cal_id,
                        event_id=event_id.strip(),
                        tz_name=tz_name,
                    )
                pre_read_marker = str(existing_event.get("updated") or "")
            except GraphInterrupt:
                raise
            except PersonalSurfaceDeniedError as exc:
                return _error_command(
                    exc.code,
                    exc.message,
                    tool_call_id,
                    retry_later=exc.is_purge_wait,
                )
            except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
                return _error_command(
                    "event_pre_read_failed",
                    f"读取待操作日程失败：{exc}",
                    tool_call_id,
                )

        approval_id = str(uuid4())
        idempotency_key = f"dalton-cal:{approval_id}"

        # Build ONE canonical normalized write spec used for BOTH confirmation card & execution (Row 6)
        spec, norm_err = _normalize_calendar_write_request(
            provider=p,
            action=act,
            calendar_id=cal_id,
            event_id=event_id,
            summary=summary,
            description=description,
            start_time=start_time,
            end_time=end_time,
            tz_name=tz_name,
            all_day=all_day,
            location=location,
            attendees=attendees,
            reminders=reminders,
            use_default_reminders=use_default_reminders,
            existing_event=existing_event,
            owner_email=conn_row.provider_email,
            account_email=conn_row.provider_email,
            idempotency_key=idempotency_key if act == "create_event" else None,
        )
        if norm_err is not None or spec is None:
            code, msg = norm_err or ("invalid_payload", "日程写入参数无效。")
            return _error_command(code, msg, tool_call_id)

        write_payload = spec["write_payload"]
        card_args = spec["card_args"]
        frozen_attendees = spec["attendees"]
        ext_attendees = spec["external_attendees"]
        requires_second_confirm = bool(spec["requires_second_confirm"])
        risk_level = "L2" if requires_second_confirm else "L1"
        ttl_seconds = (
            L2_APPROVAL_TTL_SECONDS if requires_second_confirm else L1_APPROVAL_TTL_SECONDS
        )

        action_type = f"dalton_calendar_{act}"
        event_id_clean = event_id.strip() if event_id else None
        payload_sha = canonical_payload_sha256(
            {
                "provider": p,
                "calendar_id": cal_id,
                "action": act,
                "event_id": event_id_clean or "",
                "payload": write_payload,
                "attendees": ext_attendees,
            }
        )
        confirm_token = compute_calendar_confirm_token(
            approval_id=approval_id,
            caller_user_id=caller_user_id,
            provider=p,
            provider_account_id=tok.provider_account_id,
            auth_epoch=tok.auth_epoch,
            calendar_id=cal_id,
            action=act,
            event_id=event_id_clean,
            pre_read_updated_marker=pre_read_marker,
            normalized_payload=write_payload,
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        desc_action = {
            "create_event": "新建日程",
            "update_event": "修改日程",
            "delete_event": "删除日程",
        }[act]
        desc_text = f"确认在{_provider_display_name(p)}中{desc_action}" + (
            "（含其他参会人，将发送会议通知）" if requires_second_confirm else ""
        )
        dm_meta = (
            await _notify_dm_for_web_approval(
                surface=surf,
                summary_text=f"{_provider_display_name(p)} {act}",
            )
            if surf.is_dm_channel
            else None
        )

        frozen_state: dict[str, Any] = {
            "kind": "calendar_write",
            "write_phase": "pending_approval",
            "idempotency_key": idempotency_key,
            "provider": p,
            "calendar_id": cal_id,
            "action": act,
            "event_id": event_id_clean,
            "tz_name": tz_name,
            "write_payload": write_payload,
            "attendees": frozen_attendees,
            "external_attendees": ext_attendees,
            "requires_second_confirm": requires_second_confirm,
            "risk_level": risk_level,
            "pre_read_updated_marker": pre_read_marker,
            "card_args": card_args,
            "confirm_token": confirm_token,
            "description": desc_text,
            "dm_meta": dm_meta,
            "is_dm_channel": surf.is_dm_channel,
        }
        write_stage_key = f"tc:{tool_call_id}:write" if tool_call_id else None
        created_row = await db.create_action_approval(
            approval_id=approval_id,
            caller_user_id=caller_user_id,
            thread_id=surf.thread_id,
            run_id=surf.response_id or None,
            action_type=action_type,
            provider=p,
            service="calendar",
            provider_account_id=tok.provider_account_id,
            auth_epoch=tok.auth_epoch,
            token_generation=tok.token_generation,
            payload_sha256=payload_sha,
            elicitation_state=frozen_state,
            idempotent_message_id=write_stage_key,
            expires_at=expires_at,
        )

        action_payload = _build_write_hitl_payload(
            approval_id=approval_id,
            caller_user_id=caller_user_id,
            provider=p,
            tool_call_id=tool_call_id,
            confirm_token=confirm_token,
            payload_sha=payload_sha,
            risk_level=risk_level,
            requires_second_confirm=requires_second_confirm,
            description=desc_text,
            card_args=card_args,
            dm_meta=dm_meta,
        )
        approved, resume = hitl_confirm(action_type, action_payload)

        return await _execute_confirmed_calendar_write(
            approval_row=created_row,
            frozen_state=frozen_state,
            caller_user_id=caller_user_id,
            thread_id=surf.thread_id,
            tool_call_id=tool_call_id,
            config=config,
            approved=approved,
            resume=resume,
            write_phase="pending_approval",
            google_cli=_google_cli(),
            feishu_cli=_feishu_cli(),
            attach_notice=_attach_persisted_notice,
        )

    return [dalton_calendar]


def _normalize_iso_day_window(
    start_val: str | None,
    end_val: str | None,
) -> tuple[str, str]:
    """Return a slightly widened ISO window around ``[start_val, end_val]`` for remote reconciliation."""
    now = datetime.now(UTC)
    parsed_start: datetime | None = None
    parsed_end: datetime | None = None
    for raw, is_end in ((start_val, False), (end_val, True)):
        if not raw:
            continue
        s = str(raw).strip()
        try:
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                dt = datetime.fromisoformat(f"{s}T00:00:00+00:00")
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            if is_end:
                parsed_end = dt
            else:
                parsed_start = dt
        except ValueError:
            continue
    lo = (parsed_start or now) - timedelta(days=1)
    hi = (parsed_end or (parsed_start or now) + timedelta(hours=2)) + timedelta(days=1)
    if hi <= lo:
        hi = lo + timedelta(days=2)
    return lo.isoformat(), hi.isoformat()


def _event_matches_idempotency_key(
    ev: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> bool:
    """Match a remote created event STRICTLY by platform unique identifier (never fuzzy title/time)."""
    key = str(idempotency_key or "").strip()
    if not key:
        return False
    ev_idem = str(ev.get("idempotency_key") or "").strip()
    if ev_idem and ev_idem == key:
        return True
    ext = ev.get("extendedProperties")
    if isinstance(ext, Mapping):
        priv = ext.get("private")
        if isinstance(priv, Mapping):
            if str(priv.get("dalton_idempotency_key") or "").strip() == key:
                return True
    return False


def _parse_comparable_epoch(value: str | None, *, tz_name: str) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = UTC
    norm = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def _remote_bound_matches(
    remote_ev: Mapping[str, Any],
    field: str,
    *,
    expected_str: str,
    tz_name: str,
    all_day: bool,
) -> bool:
    rem_val, rem_all_day = _event_bound_value(remote_ev, field)
    if rem_val is None:
        # Handle raw Feishu timestamp fallback
        bound = remote_ev.get(field)
        if isinstance(bound, Mapping) and bound.get("timestamp") is not None:
            rem_val = str(bound["timestamp"])
        else:
            return False
    if all_day:
        return rem_all_day and str(rem_val)[:10] == str(expected_str)[:10]
    if rem_all_day:
        return False
    rem_ts = _parse_comparable_epoch(rem_val, tz_name=tz_name)
    exp_ts = _parse_comparable_epoch(expected_str, tz_name=tz_name)
    return rem_ts is not None and exp_ts is not None and rem_ts == exp_ts


def _remote_event_matches_approved_update(
    remote_ev: Mapping[str, Any],
    *,
    provider: str,
    write_payload: Mapping[str, Any],
    tz_name: str,
) -> bool:
    """Confirm an unconfirmed update_event ONLY when EVERY approved field matches ``remote_ev``."""
    checked = 0

    if "summary" in write_payload:
        checked += 1
        if str(remote_ev.get("summary") or "").strip() != str(write_payload["summary"] or "").strip():
            return False

    if "description" in write_payload:
        checked += 1
        if str(remote_ev.get("description") or "") != str(write_payload["description"] or ""):
            return False

    if "location" in write_payload:
        checked += 1
        raw_loc = write_payload["location"]
        exp_loc = (
            str(raw_loc.get("name") or "").strip()
            if isinstance(raw_loc, Mapping)
            else str(raw_loc or "").strip()
        )
        if str(remote_ev.get("location") or "").strip() != exp_loc:
            return False

    if provider == "google":
        eff_tz = str(write_payload.get("timeZone") or tz_name or "UTC")
        is_all_day = bool(write_payload.get("allDay"))
        if "startTime" in write_payload:
            checked += 1
            if not _remote_bound_matches(
                remote_ev,
                "start",
                expected_str=str(write_payload["startTime"]),
                tz_name=eff_tz,
                all_day=is_all_day,
            ):
                return False
        if "endTime" in write_payload:
            checked += 1
            if not _remote_bound_matches(
                remote_ev,
                "end",
                expected_str=str(write_payload["endTime"]),
                tz_name=eff_tz,
                all_day=is_all_day,
            ):
                return False
        if "attendees" in write_payload:
            checked += 1
            exp_emails = {
                str(e).strip().lower()
                for e in (write_payload.get("attendees") or [])
                if str(e).strip()
            }
            rem_raw = remote_ev.get("attendees")
            if not isinstance(rem_raw, Sequence) or isinstance(rem_raw, (str, bytes)):
                return False
            all_rem: set[str] = set()
            non_self_rem: set[str] = set()
            for item in rem_raw:
                if isinstance(item, Mapping):
                    email = str(item.get("email") or "").strip().lower()
                    if not email:
                        continue
                    all_rem.add(email)
                    if not item.get("self"):
                        non_self_rem.add(email)
                elif isinstance(item, str) and item.strip():
                    email = item.strip().lower()
                    all_rem.add(email)
                    non_self_rem.add(email)
            if all_rem != exp_emails and non_self_rem != exp_emails:
                return False
        if "overrideReminders" in write_payload:
            checked += 1
            exp_rems = sorted(
                (
                    str(r.get("method") or "popup").strip().lower(),
                    int(r.get("minutes", 10)),
                )
                for r in (write_payload.get("overrideReminders") or [])
                if isinstance(r, Mapping)
            )
            rem_obj = remote_ev.get("reminders")
            if isinstance(rem_obj, Mapping):
                if rem_obj.get("useDefault") is True:
                    return False
                raw_ovr = rem_obj.get("overrides")
                if not isinstance(raw_ovr, Sequence) or isinstance(raw_ovr, (str, bytes)):
                    raw_ovr = []
                got_rems = sorted(
                    (
                        str(r.get("method") or "popup").strip().lower(),
                        int(r.get("minutes", 10)),
                    )
                    for r in raw_ovr
                    if isinstance(r, Mapping)
                )
            elif isinstance(rem_obj, Sequence) and not isinstance(rem_obj, (str, bytes)):
                got_rems = sorted(
                    (
                        str(r.get("method") or "popup").strip().lower(),
                        int(r.get("minutes", 10)),
                    )
                    for r in rem_obj
                    if isinstance(r, Mapping)
                )
            else:
                return False
            if got_rems != exp_rems:
                return False
        elif "useDefaultReminders" in write_payload:
            checked += 1
            exp_default = bool(write_payload["useDefaultReminders"])
            rem_obj = remote_ev.get("reminders")
            if not isinstance(rem_obj, Mapping) or "useDefault" not in rem_obj:
                return False
            if bool(rem_obj["useDefault"]) != exp_default:
                return False
    else:
        for body_key, ev_field in (("start_time", "start"), ("end_time", "end")):
            if body_key in write_payload:
                checked += 1
                bound_val = write_payload[body_key]
                if not isinstance(bound_val, Mapping):
                    return False
                if bound_val.get("date"):
                    if not _remote_bound_matches(
                        remote_ev,
                        ev_field,
                        expected_str=str(bound_val["date"]),
                        tz_name=tz_name,
                        all_day=True,
                    ):
                        return False
                elif bound_val.get("timestamp") is not None:
                    if not _remote_bound_matches(
                        remote_ev,
                        ev_field,
                        expected_str=str(bound_val["timestamp"]),
                        tz_name=str(bound_val.get("timezone") or tz_name or "UTC"),
                        all_day=False,
                    ):
                        return False
                else:
                    return False
        if "reminders" in write_payload:
            checked += 1
            exp_mins = sorted(
                int(r.get("minutes", 10))
                for r in (write_payload.get("reminders") or [])
                if isinstance(r, Mapping)
            )
            rem_list = remote_ev.get("reminders")
            if not isinstance(rem_list, Sequence) or isinstance(rem_list, (str, bytes)):
                return False
            got_mins = sorted(
                int(r["minutes"])
                for r in rem_list
                if isinstance(r, Mapping) and "minutes" in r
            )
            if got_mins != exp_mins:
                return False

    return checked > 0


async def _reconcile_unconfirmed_calendar_write(
    *,
    provider: str,
    action: str,
    calendar_id: str,
    event_id: str | None,
    tz_name: str,
    write_payload: Mapping[str, Any],
    card_args: Mapping[str, Any],
    pre_read_marker: str | None,
    idempotency_key: str,
    approval_id: str,
    caller_user_id: str,
    thread_id: str,
    tool_call_id: str,
    fresh_tok: FreshAccessToken,
    frozen_state: Mapping[str, Any],
    google_cli: GoogleCalendarClientProtocol,
    feishu_cli: FeishuCalendarClientProtocol,
    attach_notice: Any,
) -> Command:
    """Query remote calendar state for a ``write_dispatched_unconfirmed`` approval without re-writing."""
    try:
        if action == "create_event":
            start_hint = str(card_args.get("start_time") or write_payload.get("startTime") or "")
            end_hint = str(card_args.get("end_time") or write_payload.get("endTime") or "")
            summary_hint = str(card_args.get("summary") or write_payload.get("summary") or "")
            win_min, win_max = _normalize_iso_day_window(start_hint, end_hint)
            remote_events: list[dict[str, Any]] = []
            if provider == "google":
                mcp_res = await google_cli.call_mcp_tool(
                    access_token=fresh_tok.access_token,
                    tool_name="list_events",
                    arguments=build_google_list_events_args(
                        calendar_id=calendar_id,
                        time_min=win_min,
                        time_max=win_max,
                        tz_name=tz_name,
                        query=summary_hint or None,
                        limit=50,
                    ),
                )
                raw_items = (mcp_res or {}).get("events") or (mcp_res or {}).get("items") or []
                for item in raw_items:
                    if isinstance(item, Mapping):
                        norm = normalize_google_event(item, calendar_id=calendar_id)
                        if item.get("idempotency_key"):
                            norm["idempotency_key"] = item["idempotency_key"]
                        if isinstance(item.get("extendedProperties"), Mapping):
                            norm["extendedProperties"] = dict(item["extendedProperties"])
                        remote_events.append(norm)
            else:
                page = await feishu_cli.list_events_page(
                    access_token=fresh_tok.access_token,
                    calendar_id=calendar_id,
                    start_time=win_min,
                    end_time=win_max,
                    tz_name=tz_name,
                    page_size=200,
                )
                remote_events = list(page.get("items") or [])

            matched = next(
                (
                    ev
                    for ev in remote_events
                    if _event_matches_idempotency_key(
                        ev,
                        idempotency_key=idempotency_key,
                    )
                ),
                None,
            )
            if matched is not None:
                result_payload = {
                    "ok": True,
                    "action": "create_event",
                    "provider": provider,
                    "event": matched,
                    "reconciled": True,
                    "idempotency_key": idempotency_key,
                }
                await db.update_action_approval_state(
                    approval_id,
                    caller_user_id=caller_user_id,
                    status="succeeded",
                    elicitation_state={
                        **dict(frozen_state),
                        "write_phase": "succeeded",
                        "remote_result": result_payload,
                    },
                )
                return _tool_command(
                    await attach_notice(thread_id, result_payload),
                    tool_call_id,
                )
            return _error_command(
                "write_result_unconfirmed",
                "新建日程请求已发出但未能核实是否生效，为避免重复创建未自动重发；请前往你的日历中确认后再决定是否重试。",
                tool_call_id,
                status="unconfirmed",
                check_calendar=True,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )

        if action == "update_event" and event_id:
            if provider == "google":
                raw = await google_cli.call_mcp_tool(
                    access_token=fresh_tok.access_token,
                    tool_name="get_event",
                    arguments={"calendarId": calendar_id, "eventId": str(event_id)},
                )
                remote_ev = normalize_google_event(
                    (raw or {}).get("event") or raw or {},
                    calendar_id=calendar_id,
                )
            else:
                remote_ev = await feishu_cli.get_event(
                    access_token=fresh_tok.access_token,
                    calendar_id=calendar_id,
                    event_id=str(event_id),
                    tz_name=tz_name,
                )
            if _remote_event_matches_approved_update(
                remote_ev,
                provider=provider,
                write_payload=write_payload,
                tz_name=tz_name,
            ):
                result_payload = {
                    "ok": True,
                    "action": "update_event",
                    "provider": provider,
                    "event": remote_ev,
                    "reconciled": True,
                    "idempotency_key": idempotency_key,
                }
                await db.update_action_approval_state(
                    approval_id,
                    caller_user_id=caller_user_id,
                    status="succeeded",
                    elicitation_state={
                        **dict(frozen_state),
                        "write_phase": "succeeded",
                        "remote_result": result_payload,
                    },
                )
                return _tool_command(
                    await attach_notice(thread_id, result_payload),
                    tool_call_id,
                )
            return _error_command(
                "write_result_unconfirmed",
                "修改日程请求已发出但未能核实全部变更是否生效，为避免重复写入未自动重发；请前往你的日历中确认后再决定是否重试。",
                tool_call_id,
                status="unconfirmed",
                check_calendar=True,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )

        if action == "delete_event" and event_id:
            deleted_confirmed = False
            try:
                if provider == "google":
                    raw = await google_cli.call_mcp_tool(
                        access_token=fresh_tok.access_token,
                        tool_name="get_event",
                        arguments={"calendarId": calendar_id, "eventId": str(event_id)},
                    )
                    remote_ev = normalize_google_event(
                        (raw or {}).get("event") or raw or {},
                        calendar_id=calendar_id,
                    )
                else:
                    remote_ev = await feishu_cli.get_event(
                        access_token=fresh_tok.access_token,
                        calendar_id=calendar_id,
                        event_id=str(event_id),
                        tz_name=tz_name,
                    )
                if str(remote_ev.get("status") or "").strip().lower() == "cancelled":
                    deleted_confirmed = True
            except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
                if getattr(exc, "status_code", None) in (404, 410):
                    deleted_confirmed = True
                else:
                    raise
            if deleted_confirmed:
                result_payload = {
                    "ok": True,
                    "action": "delete_event",
                    "provider": provider,
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "deleted": True,
                    "reconciled": True,
                    "idempotency_key": idempotency_key,
                }
                await db.update_action_approval_state(
                    approval_id,
                    caller_user_id=caller_user_id,
                    status="succeeded",
                    elicitation_state={
                        **dict(frozen_state),
                        "write_phase": "succeeded",
                        "remote_result": result_payload,
                    },
                )
                return _tool_command(
                    await attach_notice(thread_id, result_payload),
                    tool_call_id,
                )
            return _error_command(
                "write_result_unconfirmed",
                "上次删除日程请求已发出但结果未确认，为避免重复操作未自动重发；请前往你的日历中确认后再决定是否重试。",
                tool_call_id,
                status="unconfirmed",
                check_calendar=True,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
    except (GoogleCalendarAPIError, FeishuCalendarAPIError, GoogleMCPElicitationRequired) as exc:
        return _error_command(
            "write_result_unconfirmed",
            f"上次日程写入结果未确认，远端核实失败（{exc}）；为避免重复写入未自动重发，请前往你的日历中确认后再试。",
            tool_call_id,
            status="unconfirmed",
            check_calendar=True,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
        )

    return _error_command(
        "write_result_unconfirmed",
        "上次日程写入请求结果未确认，为避免重复写入已中止；请前往你的日历中确认后再试。",
        tool_call_id,
        status="unconfirmed",
        check_calendar=True,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
    )


async def _resume_frozen_calendar_write(
    *,
    existing_write: ActionApprovalRow,
    caller_user_id: str,
    thread_id: str,
    tool_call_id: str,
    config: RunnableConfig | None,
    google_cli: GoogleCalendarClientProtocol,
    feishu_cli: FeishuCalendarClientProtocol,
    attach_notice: Any,
) -> Command:
    """Resume an interrupted write approval without creating a new approval ID or re-pre-reading."""
    # Replay any completed pre-read elicitation interrupts so LangGraph's interrupt cursor stays aligned
    preread_rows = await db.list_action_approvals_by_stage_prefix(
        caller_user_id,
        thread_id,
        f"tc:{tool_call_id}:preread:elic:",
        decrypt_state=True,
    )
    for pre_row in preread_rows:
        pre_state = pre_row.elicitation_state or {}
        pre_payload = _build_elicitation_hitl_payload(
            approval_id=pre_row.approval_id,
            caller_user_id=caller_user_id,
            tool_call_id=tool_call_id,
            confirm_token=str(pre_state.get("confirm_token") or ""),
            payload_sha=pre_row.payload_sha256,
            input_requests=pre_row.input_requests_map or {},
            mcp_tool_name=str(pre_state.get("mcp_tool_name") or "get_event"),
            mcp_arguments=pre_state.get("mcp_arguments") or {},
            dm_meta=pre_state.get("dm_meta"),
        )
        hitl_confirm("dalton_google_mcp_elicitation", pre_payload)

    frozen = dict(existing_write.elicitation_state or {})
    action_payload = _build_write_hitl_payload(
        approval_id=existing_write.approval_id,
        caller_user_id=caller_user_id,
        provider=existing_write.provider,
        tool_call_id=tool_call_id,
        confirm_token=str(frozen.get("confirm_token") or ""),
        payload_sha=existing_write.payload_sha256,
        risk_level=str(frozen.get("risk_level") or "L1"),
        requires_second_confirm=bool(frozen.get("requires_second_confirm")),
        description=str(frozen.get("description") or ""),
        card_args=frozen.get("card_args") or {},
        dm_meta=frozen.get("dm_meta"),
    )
    approved, resume = hitl_confirm(existing_write.action_type, action_payload)

    if existing_write.status == "succeeded" and isinstance(frozen.get("remote_result"), Mapping):
        return _tool_command(
            await attach_notice(thread_id, dict(frozen["remote_result"])),
            tool_call_id,
        )

    persisted_phase = str(frozen.get("write_phase") or "").strip()
    if (
        existing_write.status == "send_ambiguous"
        or persisted_phase == "write_dispatched_unconfirmed"
    ):
        # Replay any completed elicitation interrupts before reconciling so LangGraph's cursor stays aligned
        for prefix in (
            f"tc:{tool_call_id}:vercheck:elic:",
            f"tc:{tool_call_id}:write:elic:",
            f"tc:{tool_call_id}:vercheck_r",
        ):
            prior_elics = await db.list_action_approvals_by_stage_prefix(
                caller_user_id, thread_id, prefix, decrypt_state=True
            )
            for erow in prior_elics:
                estate = erow.elicitation_state or {}
                epayload = _build_elicitation_hitl_payload(
                    approval_id=erow.approval_id,
                    caller_user_id=caller_user_id,
                    tool_call_id=tool_call_id,
                    confirm_token=str(estate.get("confirm_token") or ""),
                    payload_sha=erow.payload_sha256,
                    input_requests=erow.input_requests_map or {},
                    mcp_tool_name=str(estate.get("mcp_tool_name") or existing_write.action_type),
                    mcp_arguments=estate.get("mcp_arguments") or {},
                    dm_meta=estate.get("dm_meta"),
                )
                hitl_confirm("dalton_google_mcp_elicitation", epayload)
        resolved_phase = "write_dispatched_unconfirmed"
    elif existing_write.status == "executing":
        resolved_phase = (
            persisted_phase if persisted_phase in ("awaiting_google_form", "write_dispatched_unconfirmed")
            else "awaiting_google_form"
        )
    else:
        resolved_phase = "pending_approval"

    return await _execute_confirmed_calendar_write(
        approval_row=existing_write,
        frozen_state=frozen,
        caller_user_id=caller_user_id,
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        config=config,
        approved=approved,
        resume=resume,
        write_phase=resolved_phase,
        google_cli=google_cli,
        feishu_cli=feishu_cli,
        attach_notice=attach_notice,
    )


async def _execute_confirmed_calendar_write(
    *,
    approval_row: ActionApprovalRow,
    frozen_state: Mapping[str, Any],
    caller_user_id: str,
    thread_id: str,
    tool_call_id: str,
    config: RunnableConfig | None,
    approved: bool,
    resume: Mapping[str, Any],
    write_phase: str,
    google_cli: GoogleCalendarClientProtocol,
    feishu_cli: FeishuCalendarClientProtocol,
    attach_notice: Any,
) -> Command:
    """Consume ONLY the original approval row, verify capability/version/epoch, and execute frozen payload."""
    approval_id = approval_row.approval_id
    p = str(frozen_state.get("provider") or approval_row.provider).strip().lower()
    cal_id = str(frozen_state["calendar_id"])
    act = str(frozen_state["action"])
    event_id = frozen_state.get("event_id")
    tz_name = str(frozen_state.get("tz_name") or "Asia/Shanghai")
    write_payload = dict(frozen_state["write_payload"])
    card_args = frozen_state.get("card_args") if isinstance(frozen_state.get("card_args"), Mapping) else {}
    frozen_attendees = frozen_state.get("attendees")
    requires_second_confirm = bool(frozen_state.get("requires_second_confirm"))
    pre_read_marker = frozen_state.get("pre_read_updated_marker")
    idempotency_key = str(frozen_state.get("idempotency_key") or f"dalton-cal:{approval_id}")
    req_scopes = _required_scopes_for(p, act)
    desc_action = {
        "create_event": "新建日程",
        "update_event": "修改日程",
        "delete_event": "删除日程",
    }[act]

    # Re-verify platform capability against the approval's saved provider BEFORE consuming or calling APIs (Item 6)
    if p not in _enabled_calendar_providers():
        if approval_row.status in ("pending", "executing"):
            await db.mark_action_approval_status(
                approval_id, "expired", caller_user_id=caller_user_id
            )
        return _error_command(
            "provider_capability_disabled",
            f"当前部署已关闭{_provider_display_name(p)}功能，已中止本次{desc_action}操作。",
            tool_call_id,
            provider=p,
        )

    if write_phase == "pending_approval":
        if isinstance(resume, Mapping) and resume.get("scope_changed"):
            await db.mark_action_approval_status(
                approval_id, "expired", caller_user_id=caller_user_id
            )
            return _error_command(
                "scope_changed",
                "确认期间对话私密状态或日历连接已发生变化，已作废本次确认，请重新发起。",
                tool_call_id,
            )

        decision = _extract_first_decision(resume)
        if not approved or str(decision.get("type") or "").strip().lower() != "approve":
            await db.mark_action_approval_status(
                approval_id, "rejected", caller_user_id=caller_user_id
            )
            return _error_command(
                "user_rejected",
                f"你已取消本次{desc_action}操作。",
                tool_call_id,
                reason=decision.get("message"),
            )

        if requires_second_confirm:
            if (
                decision.get("second_confirm_acknowledged") is not True
                or str(decision.get("confirmed_payload_sha256") or "").strip()
                != approval_row.payload_sha256
            ):
                await db.mark_action_approval_status(
                    approval_id, "rejected", caller_user_id=caller_user_id
                )
                return _error_command(
                    "second_confirmation_required",
                    "包含其他参会人的日历写入需要勾选二次确认后再执行。",
                    tool_call_id,
                )

        # Atomically consume the ORIGINAL approval row (one-time use + original expires_at enforced!)
        consumed = await db.consume_action_approval(
            approval_id, caller_user_id, decrypt_state=True
        )
        if consumed is None:
            return _error_command(
                "approval_expired_or_consumed",
                "该确认卡片已过期或已被使用，请重新发起操作。",
                tool_call_id,
            )
        expected_auth_epoch = consumed.auth_epoch
        expected_account_id = consumed.provider_account_id
    else:
        expected_auth_epoch = approval_row.auth_epoch
        expected_account_id = approval_row.provider_account_id

    if not await check_thread_surface_still_private(thread_id, caller_user_id):
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return _error_command(
            "scope_changed",
            "确认期间对话私密状态已变更，已中止本次写入。",
            tool_call_id,
        )

    # Acquire fresh token right before external write/reconcile HTTP (outside any lock)
    try:
        fresh_tok = await ensure_fresh_access_token(
            caller_user_id,
            p,
            "calendar",
            min_remaining_seconds=600,
            required_scopes=req_scopes,
        )
    except (
        MissingRequiredScopesError,
        IntegrationNeedsReauthError,
        IntegrationNotConnectedError,
        PersonalIntegrationError,
    ) as exc:
        if write_phase != "write_dispatched_unconfirmed":
            await db.mark_action_approval_status(
                approval_id, "failed", caller_user_id=caller_user_id
            )
        return _auth_error_command(exc, provider=p, tool_call_id=tool_call_id)

    if (
        fresh_tok.auth_epoch != expected_auth_epoch
        or fresh_tok.provider_account_id != expected_account_id
    ):
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return _error_command(
            "scope_changed",
            "确认期间日历授权已变更，请重新发起操作。",
            tool_call_id,
        )

    # Atomically re-verify private surface & record touch with actual token generation before any remote call
    try:
        surf = await verify_personal_integration_surface(
            config,
            provider=p,
            service="calendar",
            auth_epoch=fresh_tok.auth_epoch,
            provider_account_id=fresh_tok.provider_account_id,
            expected_auth_epoch=fresh_tok.auth_epoch,
            expected_provider_account_id=fresh_tok.provider_account_id,
            record_touch_now=True,
        )
    except PersonalSurfaceDeniedError as exc:
        await db.mark_action_approval_status(
            approval_id, "expired", caller_user_id=caller_user_id
        )
        return _error_command(
            exc.code,
            exc.message,
            tool_call_id,
            retry_later=exc.is_purge_wait,
        )

    # State 1: Write was already dispatched, local confirmation failed -> reconcile remotely, NEVER re-write!
    if write_phase == "write_dispatched_unconfirmed":
        return await _reconcile_unconfirmed_calendar_write(
            provider=p,
            action=act,
            calendar_id=cal_id,
            event_id=str(event_id) if event_id else None,
            tz_name=tz_name,
            write_payload=write_payload,
            card_args=card_args,
            pre_read_marker=str(pre_read_marker) if pre_read_marker else None,
            idempotency_key=idempotency_key,
            approval_id=approval_id,
            caller_user_id=caller_user_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fresh_tok=fresh_tok,
            frozen_state=frozen_state,
            google_cli=google_cli,
            feishu_cli=feishu_cli,
            attach_notice=attach_notice,
        )

    mutable_frozen = dict(frozen_state)
    mutable_frozen["idempotency_key"] = idempotency_key

    async def _mark_awaiting_google_form() -> None:
        mutable_frozen["write_phase"] = "awaiting_google_form"
        await db.update_action_approval_state(
            approval_id,
            caller_user_id=caller_user_id,
            status="executing",
            elicitation_state=mutable_frozen,
        )

    async def _mark_write_dispatched_unconfirmed() -> None:
        mutable_frozen["write_phase"] = "write_dispatched_unconfirmed"
        await db.update_action_approval_state(
            approval_id,
            caller_user_id=caller_user_id,
            status="send_ambiguous",
            elicitation_state=mutable_frozen,
        )

    async def _verify_event_version(
        vercheck_prefix: str,
        token_for_check: FreshAccessToken,
    ) -> Command | None:
        if act not in ("update_event", "delete_event") or not pre_read_marker:
            return None
        assert event_id is not None
        try:
            if p == "google":
                latest_raw, vc_err = await _call_google_mcp_with_elicitation(
                    google_client=google_cli,
                    caller_user_id=caller_user_id,
                    surface=surf,
                    token=token_for_check,
                    tool_name="get_event",
                    arguments={"calendarId": cal_id, "eventId": str(event_id)},
                    required_scopes=_required_scopes_for(p, "get_event"),
                    tool_call_id=tool_call_id,
                    config=config,
                    stage_prefix=vercheck_prefix,
                    on_elicitation_pause=_mark_awaiting_google_form,
                )
                if vc_err is not None:
                    await db.mark_action_approval_status(
                        approval_id, "failed", caller_user_id=caller_user_id
                    )
                    return vc_err
                latest_ev = normalize_google_event(
                    (latest_raw or {}).get("event") or latest_raw or {},
                    calendar_id=cal_id,
                )
            else:
                latest_ev = await feishu_cli.get_event(
                    access_token=token_for_check.access_token,
                    calendar_id=cal_id,
                    event_id=str(event_id),
                    tz_name=tz_name,
                )
            current_marker = str(latest_ev.get("updated") or "")
            if current_marker and current_marker != str(pre_read_marker):
                await db.mark_action_approval_status(
                    approval_id, "expired", caller_user_id=caller_user_id
                )
                return _error_command(
                    "event_version_changed",
                    "该日程在确认期间已被修改，已中止操作，请重新查看日程后再发起。",
                    tool_call_id,
                )
        except GraphInterrupt:
            raise
        except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
            await db.mark_action_approval_status(
                approval_id, "failed", caller_user_id=caller_user_id
            )
            return _error_command(
                "event_version_check_failed",
                f"执行前核对日程版本失败：{exc}",
                tool_call_id,
            )
        return None

    # Check if we already entered a write elicitation round in a previous run.
    # If so, replay any completed initial vercheck elicitations for LangGraph cursor alignment,
    # and run a fresh version check inside ``before_resumed_call`` right after settling the write form!
    existing_write_elics = (
        await db.list_action_approvals_by_stage_prefix(
            caller_user_id,
            thread_id,
            f"tc:{tool_call_id}:write:elic:",
            decrypt_state=True,
        )
        if p == "google" and write_phase == "awaiting_google_form"
        else []
    )
    if existing_write_elics:
        prior_vc_elics = await db.list_action_approvals_by_stage_prefix(
            caller_user_id,
            thread_id,
            f"tc:{tool_call_id}:vercheck:elic:",
            decrypt_state=True,
        )
        for vc_row in prior_vc_elics:
            vc_state = vc_row.elicitation_state or {}
            vc_payload = _build_elicitation_hitl_payload(
                approval_id=vc_row.approval_id,
                caller_user_id=caller_user_id,
                tool_call_id=tool_call_id,
                confirm_token=str(vc_state.get("confirm_token") or ""),
                payload_sha=vc_row.payload_sha256,
                input_requests=vc_row.input_requests_map or {},
                mcp_tool_name=str(vc_state.get("mcp_tool_name") or "get_event"),
                mcp_arguments=vc_state.get("mcp_arguments") or {},
                dm_meta=vc_state.get("dm_meta"),
            )
            hitl_confirm("dalton_google_mcp_elicitation", vc_payload)
    else:
        ver_err = await _verify_event_version(f"tc:{tool_call_id}:vercheck", fresh_tok)
        if ver_err is not None:
            return ver_err

    # Execute upstream write using ONLY the frozen normalized payload
    try:
        if p == "google":
            async def _recheck_before_resumed_write(
                round_idx: int, resumed_tok: FreshAccessToken
            ) -> Command | None:
                return await _verify_event_version(
                    f"tc:{tool_call_id}:vercheck_r{round_idx}", resumed_tok
                )

            async def _replay_resumed_vercheck_elics(round_idx: int) -> None:
                prior_r_elics = await db.list_action_approvals_by_stage_prefix(
                    caller_user_id,
                    thread_id,
                    f"tc:{tool_call_id}:vercheck_r{round_idx}:elic:",
                    decrypt_state=True,
                )
                for r_row in prior_r_elics:
                    r_state = r_row.elicitation_state or {}
                    r_payload = _build_elicitation_hitl_payload(
                        approval_id=r_row.approval_id,
                        caller_user_id=caller_user_id,
                        tool_call_id=tool_call_id,
                        confirm_token=str(r_state.get("confirm_token") or ""),
                        payload_sha=r_row.payload_sha256,
                        input_requests=r_row.input_requests_map or {},
                        mcp_tool_name=str(r_state.get("mcp_tool_name") or "get_event"),
                        mcp_arguments=r_state.get("mcp_arguments") or {},
                        dm_meta=r_state.get("dm_meta"),
                    )
                    hitl_confirm("dalton_google_mcp_elicitation", r_payload)

            mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                google_client=google_cli,
                caller_user_id=caller_user_id,
                surface=surf,
                token=fresh_tok,
                tool_name=act,
                arguments=write_payload,
                required_scopes=req_scopes,
                tool_call_id=tool_call_id,
                config=config,
                stage_prefix=f"tc:{tool_call_id}:write",
                before_dispatch=_mark_write_dispatched_unconfirmed,
                on_elicitation_pause=_mark_awaiting_google_form,
                before_resumed_call=_recheck_before_resumed_write,
                replay_resumed_elics=_replay_resumed_vercheck_elics,
            )
            if err_cmd is not None:
                if mutable_frozen.get("write_phase") != "write_dispatched_unconfirmed":
                    await db.mark_action_approval_status(
                        approval_id, "failed", caller_user_id=caller_user_id
                    )
                return err_cmd
            if act == "delete_event":
                result_payload: dict[str, Any] = {
                    "ok": True,
                    "action": "delete_event",
                    "provider": "google",
                    "calendar_id": cal_id,
                    "event_id": event_id,
                    "deleted": True,
                }
            else:
                raw_ev = (mcp_res or {}).get("event") or mcp_res or {}
                result_payload = {
                    "ok": True,
                    "action": act,
                    "provider": "google",
                    "event": normalize_google_event(raw_ev, calendar_id=cal_id),
                }
            mutable_frozen["write_phase"] = "succeeded"
            mutable_frozen["remote_result"] = result_payload
            await db.update_action_approval_state(
                approval_id,
                caller_user_id=caller_user_id,
                status="succeeded",
                elicitation_state=mutable_frozen,
            )
            return _tool_command(
                await attach_notice(thread_id, result_payload),
                tool_call_id,
            )
        else:
            await _mark_write_dispatched_unconfirmed()
            if act == "create_event":
                ev = await feishu_cli.create_event(
                    access_token=fresh_tok.access_token,
                    calendar_id=cal_id,
                    body=write_payload,
                    attendees=frozen_attendees,
                    tz_name=tz_name,
                    idempotency_key=idempotency_key,
                )
                result_payload = {
                    "ok": True,
                    "action": "create_event",
                    "provider": "feishu",
                    "event": ev,
                }
            elif act == "update_event":
                assert event_id is not None
                ev = await feishu_cli.update_event(
                    access_token=fresh_tok.access_token,
                    calendar_id=cal_id,
                    event_id=str(event_id),
                    body=write_payload,
                    tz_name=tz_name,
                )
                result_payload = {
                    "ok": True,
                    "action": "update_event",
                    "provider": "feishu",
                    "event": ev,
                }
            else:
                assert event_id is not None
                del_res = await feishu_cli.delete_event(
                    access_token=fresh_tok.access_token,
                    calendar_id=cal_id,
                    event_id=str(event_id),
                    need_notification=requires_second_confirm,
                )
                result_payload = {"ok": True, "action": "delete_event", **del_res}
            mutable_frozen["write_phase"] = "succeeded"
            mutable_frozen["remote_result"] = result_payload
            await db.update_action_approval_state(
                approval_id,
                caller_user_id=caller_user_id,
                status="succeeded",
                elicitation_state=mutable_frozen,
            )
            return _tool_command(
                await attach_notice(thread_id, result_payload),
                tool_call_id,
            )
    except GraphInterrupt:
        raise
    except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
        # Definite 4xx client error proves upstream did not apply; network/5xx keeps send_ambiguous!
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and 400 <= status_code < 500:
            await db.mark_action_approval_status(
                approval_id, "failed", caller_user_id=caller_user_id
            )
        return _error_command(
            "calendar_write_failed",
            f"执行{desc_action}失败：{exc}",
            tool_call_id,
        )


__all__ = ["create_calendar_tools"]
