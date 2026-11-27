"""Direct LangChain tool ``dalton_calendar`` for personal Google & Feishu calendars (§5.1, §5.2).

Security invariants:
- Mounted in-process on the backend main agent only (zero sandbox or subagent exposure).
- Gated behind ``CapabilityId.GOOGLE_CALENDAR`` and ``CapabilityId.FEISHU_CALENDAR`` via
  ``DALTON_TOOL_REGISTRY.any_capabilities``; provider branches are clamped to active capabilities.
- Access tokens are NEVER cached or held by the tool; every upstream call acquires a
  short-lived token through Step 2's ``ensure_fresh_access_token(...)`` outside DB locks.
- Every read and write first passes ``verify_personal_integration_surface(...)`` (which
  atomically calls ``record_touch(...)`` and emits the first-touch notice on initial access).
- Write operations (create/update/delete) require L1 or L2 (external attendees) HITL
  confirmation; Google MCP ``input_required`` form elicitation is handled on both reads
  and writes with full multi-round ``inputResponses`` preservation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Annotated, Any, Literal
from uuid import uuid4

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from src.dalton.capabilities import CapabilityId, effective_capabilities
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import IntegrationRow, IntegrationStatus
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
    extract_surface_context,
    verify_personal_integration_surface,
)
from src.dalton.services.user_integrations_oauth import (
    FreshAccessToken,
    IntegrationNeedsReauthError,
    IntegrationNotConnectedError,
    PersonalIntegrationError,
    ensure_fresh_access_token,
)
from src.tools.secretary._commands import InjectedToolCallId, hitl_confirm

logger = logging.getLogger(__name__)

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
_SETTINGS_INTEGRATIONS_PATH = "/settings?tab=integrations#dalton-personal-integrations"
_MAX_ELICITATION_ROUNDS = 4


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


def _is_unattended_automation_turn(config: RunnableConfig | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    cfg = config.get("configurable")
    if not isinstance(cfg, Mapping):
        return False
    if str(cfg.get("automation_execution_id") or "").strip():
        return True
    if str(cfg.get("platform") or "").strip().lower() == "automation":
        return True
    surface = cfg.get("surface")
    if (
        isinstance(surface, Mapping)
        and str(surface.get("platform") or "").strip().lower() == "automation"
    ):
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
) -> tuple[dict[str, Any] | None, Command | None]:
    """Invoke Google Calendar MCP with multi-round ``input_required`` form elicitation handling (§3.1, §5.2)."""
    active_token = token
    request_state: str | None = None
    merged_responses: dict[str, dict[str, Any]] | None = None

    for _round in range(_MAX_ELICITATION_ROUNDS):
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
                elicitation_state={
                    "mcp_endpoint": elic.mcp_endpoint,
                    "mcp_tool_name": elic.mcp_tool_name,
                    "mcp_arguments": elic.mcp_arguments,
                    "request_state": elic.request_state,
                    "prior_responses": dict(merged_responses or {}),
                },
                expires_at=expires_at,
            )

            action_request: dict[str, Any] = {
                "type": "dalton_google_mcp_elicitation",
                "name": "dalton_calendar",
                "tool_call_id": tool_call_id,
                "approval_id": approval_id,
                "caller_user_id": caller_user_id,
                "provider": "google",
                "service": "calendar",
                "confirm_token": confirm_token,
                "payload_sha256": payload_sha,
                "risk_level": "elicitation",
                "input_requests": elic.input_requests,
                "args": {
                    "provider": "google",
                    "tool": elic.mcp_tool_name,
                    "arguments": elic.mcp_arguments,
                },
            }
            if surface.is_dm_channel:
                action_request.update(
                    await _notify_dm_for_web_approval(
                        surface=surface, summary_text="Google 日历安全确认"
                    )
                )

            resume = hitl_confirm(
                description="Google 日历需要你在网页端完成确认表单。",
                action_requests=[action_request],
            )
            if isinstance(resume, Mapping) and resume.get("scope_changed"):
                await db.mark_action_approval_status(
                    approval_id, "expired", caller_user_id=caller_user_id
                )
                return None, _error_command(
                    "scope_changed",
                    "确认期间对话私密状态或日历连接已发生变化，已作废本次确认，请重新发起。",
                    tool_call_id,
                )

            decision = _extract_first_decision(resume)
            dec_type = str(decision.get("type") or "").strip().lower()
            provided_elic = decision.get("elicitation_responses")

            # Decline / cancel branch (§3.1(2), §5.2)
            if dec_type == "reject":
                await db.mark_action_approval_status(
                    approval_id, "rejected", caller_user_id=caller_user_id
                )
                cancel_responses = (
                    dict(provided_elic)
                    if isinstance(provided_elic, Mapping) and provided_elic
                    else {k: {"action": "decline"} for k in elic.input_requests}
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
                        tool_name=elic.mcp_tool_name,
                        arguments=elic.mcp_arguments,
                        request_state=elic.request_state,
                        input_responses=cancel_responses,
                    )
                except Exception:
                    logger.debug(
                        "[dalton_calendar] Google MCP decline cleanup notice failed (ignored)"
                    )
                return None, _error_command(
                    "user_cancelled",
                    "你已取消本次 Google 日历操作。",
                    tool_call_id,
                )

            if not isinstance(provided_elic, Mapping):
                await db.mark_action_approval_status(
                    approval_id, "rejected", caller_user_id=caller_user_id
                )
                return None, _error_command(
                    "invalid_elicitation_response",
                    "缺少有效的表单确认响应，已取消本次操作。",
                    tool_call_id,
                )

            ok, err, is_decline = validate_elicitation_responses(
                elic.input_requests, provided_elic
            )
            if not ok:
                await db.mark_action_approval_status(
                    approval_id, "rejected", caller_user_id=caller_user_id
                )
                return None, _error_command(
                    "invalid_elicitation_response",
                    err or "表单确认响应无效。",
                    tool_call_id,
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
                        tool_name=elic.mcp_tool_name,
                        arguments=elic.mcp_arguments,
                        request_state=elic.request_state,
                        input_responses=dict(provided_elic),
                    )
                except Exception:
                    logger.debug(
                        "[dalton_calendar] Google MCP cancel/decline cleanup failed (ignored)"
                    )
                return None, _error_command(
                    "user_cancelled",
                    "你已取消本次 Google 日历操作。",
                    tool_call_id,
                )

            consumed = await db.consume_action_approval(
                approval_id, caller_user_id, decrypt_state=True
            )
            if consumed is None or not consumed.elicitation_state:
                return None, _error_command(
                    "approval_expired_or_consumed",
                    "确认已过期或已使用，请重新发起。",
                    tool_call_id,
                )
            if not await check_thread_surface_still_private(
                surface.thread_id, caller_user_id
            ):
                await db.mark_action_approval_status(
                    approval_id, "expired", caller_user_id=caller_user_id
                )
                return None, _error_command(
                    "scope_changed",
                    "确认期间对话私密状态已变更，已中止本次操作。",
                    tool_call_id,
                )

            active_token = await ensure_fresh_access_token(
                caller_user_id,
                "google",
                "calendar",
                min_remaining_seconds=600,
                required_scopes=required_scopes,
            )
            if active_token.auth_epoch != consumed.auth_epoch:
                await db.mark_action_approval_status(
                    approval_id, "expired", caller_user_id=caller_user_id
                )
                return None, _error_command(
                    "scope_changed",
                    "确认期间 Google 日历连接已更新，请重新发起操作。",
                    tool_call_id,
                )

            state = consumed.elicitation_state
            request_state = str(state["request_state"])
            prior = state.get("prior_responses")
            next_responses: dict[str, dict[str, Any]] = (
                dict(prior) if isinstance(prior, Mapping) else {}
            )
            for k, v in provided_elic.items():
                next_responses[str(k)] = dict(v)
            merged_responses = next_responses
            await db.mark_action_approval_status(
                approval_id, "succeeded", caller_user_id=caller_user_id
            )

    return None, _error_command(
        "elicitation_round_limit",
        "Google 日历连续确认次数超过上限，请稍后重试。",
        tool_call_id,
    )


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

    @tool("dalton_calendar")
    async def dalton_calendar(
        action: CalendarActionLiteral,
        provider: CalendarProviderLiteral = "auto",
        calendar_id: str | None = None,
        event_id: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        timezone: str | None = None,
        query: str | None = None,
        limit: int = 50,
        duration_minutes: int = 30,
        summary: str | None = None,
        description: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        all_day: bool = False,
        location: str | None = None,
        attendees: list[str] | None = None,
        reminders: list[dict[str, Any]] | None = None,
        use_default_reminders: bool | None = None,
        config: RunnableConfig = None,  # type: ignore[assignment]
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """读取或管理当前用户本人已连接的个人日历（支持 Google 日历与飞书日历）。

        使用说明：
        - 适用场景：仅在当前用户本人的私人 Home 会话、1:1 私聊，或仅本人可见的私密工作区中使用；首次在某个对话里访问个人日历时，需将工具返回的 `first_touch_notice` 原样转告用户。
        - 平台选择（`provider`）：默认 `"auto"`。仅连接一个日历平台时自动选用；同时连接 Google 日历与飞书日历时，只读查询会自动合并两边结果并标注来源，写操作与按事件 ID 查询单个日程时请先向用户确认目标平台再指定 `"google"` 或 `"feishu"`。
        - 支持的动作（`action`）：
          - `"list_calendars"`：列出用户有权限查看的日历列表。
          - `"list_events"`：查询指定时间范围（`time_min` 至 `time_max`，ISO 8601 时间格式，如 `2026-12-01T00:00:00+08:00`）内的日程，可附带 `query` 关键词筛选和 `timezone`（IANA 时区名，如 `Asia/Shanghai`、`America/New_York`）。
          - `"get_event"`：按 `event_id` 读取单个日程详情。
          - `"check_availability"`：查询 `time_min` 至 `time_max` 之间的忙碌时段与长度不少于 `duration_minutes` 分钟的空闲时段。
          - `"create_event"`：新建日程（需提供 `summary`、`start_time`、`end_time`，可选 `description`、`location`、`attendees` 邮箱列表、`reminders` 如 `[{"method": "popup", "minutes": 15}]`、`all_day`）。
          - `"update_event"`：修改已有日程（需提供 `event_id` 及待修改字段）。
          - `"delete_event"`：删除已有日程（需提供 `event_id`）。
        - 人工确认：所有写操作（新建、修改、删除）都会在网页端弹出确认卡片，待用户在界面核对并批准后执行；包含其他参会人时会要求二次确认。
        """
        act = str(action or "").strip().lower()
        if act not in _READ_ACTIONS and act not in _WRITE_ACTIONS:
            return _error_command(
                "invalid_action",
                f"不支持的日历动作 '{action}'。",
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
        if not caller_user_id:
            return _error_command(
                "missing_identity",
                "无法识别当前用户身份，请在本人登录会话中使用个人日历。",
                tool_call_id,
            )

        cfg = (config or {}).get("configurable") if isinstance(config, Mapping) else {}
        tz_name = str(
            timezone or (cfg.get("timezone") if isinstance(cfg, Mapping) else None) or "Asia/Shanghai"
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
                avail_names = "或".join(_provider_display_name(p) for p in enabled_providers)
                return _error_command(
                    "not_connected",
                    f"你尚未连接个人日历。前往「设置 → 集成」（{_SETTINGS_INTEGRATIONS_PATH}）连接{avail_names}后，即可在私密对话中查询和管理日程。",
                    tool_call_id,
                    settings_path=_SETTINGS_INTEGRATIONS_PATH,
                    available_providers=enabled_providers,
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
        elif act in ("get_event", "update_event", "delete_event"):
            if not event_id or not event_id.strip():
                return _error_command(
                    "missing_event_id",
                    f"执行 {act} 需要提供 event_id。",
                    tool_call_id,
                )
        elif act == "create_event":
            if not summary or not summary.strip() or not start_time or not end_time:
                return _error_command(
                    "missing_required_fields",
                    "新建日程需要提供 summary、start_time 和 end_time。",
                    tool_call_id,
                )

        # Verify private surface & record touch per target provider BEFORE external HTTP
        first_touch_notice: str | None = None
        surfaces: dict[str, PersonalSurfaceVerificationResult] = {}
        for p in target_providers:
            conn_row = connected_rows[p]
            try:
                surf = await verify_personal_integration_surface(
                    config,
                    provider=p,
                    service="calendar",
                    auth_epoch=conn_row.auth_epoch,
                    provider_account_id=conn_row.provider_account_id,
                    record_touch_now=True,
                )
            except PersonalSurfaceDeniedError as exc:
                return _error_command(
                    exc.code,
                    exc.message,
                    tool_call_id,
                    retry_later=exc.is_purge_wait,
                )
            surfaces[p] = surf
            if surf.first_touch_notice and first_touch_notice is None:
                first_touch_notice = surf.first_touch_notice

        def _attach_notice(res: dict[str, Any]) -> dict[str, Any]:
            if first_touch_notice:
                res["first_touch_notice"] = first_touch_notice
            return res

        # ------------------------------------------------------------------
        # READ ACTIONS
        # ------------------------------------------------------------------
        if act == "list_calendars":
            calendars: list[dict[str, Any]] = []
            errors_by_provider: dict[str, str] = {}
            for p in target_providers:
                try:
                    tok = await ensure_fresh_access_token(
                        caller_user_id,
                        p,
                        "calendar",
                        min_remaining_seconds=600,
                        required_scopes=_required_scopes_for(p, act),
                    )
                    if p == "google":
                        calendars.extend(
                            await _google_cli().list_calendars(access_token=tok.access_token)
                        )
                    else:
                        calendars.extend(
                            await _feishu_cli().list_calendars(access_token=tok.access_token)
                        )
                except (
                    IntegrationNeedsReauthError,
                    IntegrationNotConnectedError,
                    PersonalIntegrationError,
                    GoogleCalendarAPIError,
                    FeishuCalendarAPIError,
                ) as exc:
                    errors_by_provider[p] = str(exc)
            if not calendars and errors_by_provider:
                return _error_command(
                    "calendar_read_failed",
                    "读取日历列表失败：" + "；".join(errors_by_provider.values()),
                    tool_call_id,
                    errors=errors_by_provider,
                    **({"first_touch_notice": first_touch_notice} if first_touch_notice else {}),
                )
            return _tool_command(
                _attach_notice(
                    {
                        "ok": True,
                        "action": "list_calendars",
                        "providers": target_providers,
                        "calendars": calendars,
                        "count": len(calendars),
                        **({"partial_errors": errors_by_provider} if errors_by_provider else {}),
                    }
                ),
                tool_call_id,
            )

        if act in ("list_events", "check_availability"):
            assert time_min is not None and time_max is not None
            merged_events: list[dict[str, Any]] = []
            errors_by_provider = {}
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
                    if p == "google":
                        mcp_args = build_google_list_events_args(
                            calendar_id=cal_id,
                            time_min=time_min,
                            time_max=time_max,
                            tz_name=tz_name,
                            query=query if act == "list_events" else None,
                            limit=limit,
                        )
                        mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                            google_client=_google_cli(),
                            caller_user_id=caller_user_id,
                            surface=surfaces[p],
                            token=tok,
                            tool_name="list_events",
                            arguments=mcp_args,
                            required_scopes=req_scopes,
                            tool_call_id=tool_call_id,
                            config=config,
                        )
                        if err_cmd is not None:
                            return err_cmd
                        raw_items = (mcp_res or {}).get("events") or (mcp_res or {}).get("items") or []
                        if isinstance(raw_items, list):
                            for item in raw_items:
                                if isinstance(item, Mapping):
                                    merged_events.append(
                                        normalize_google_event(item, calendar_id=cal_id)
                                    )
                    else:
                        feishu_items = await _feishu_cli().list_events(
                            access_token=tok.access_token,
                            calendar_id=cal_id,
                            start_time=time_min,
                            end_time=time_max,
                            tz_name=tz_name,
                            page_size=limit,
                        )
                        if query and query.strip() and act == "list_events":
                            q_low = query.strip().lower()
                            feishu_items = [
                                ev
                                for ev in feishu_items
                                if q_low in str(ev.get("summary") or "").lower()
                                or q_low in str(ev.get("description") or "").lower()
                            ]
                        merged_events.extend(feishu_items)
                except GraphInterrupt:
                    raise
                except (
                    IntegrationNeedsReauthError,
                    IntegrationNotConnectedError,
                    PersonalIntegrationError,
                    GoogleCalendarAPIError,
                    FeishuCalendarAPIError,
                ) as exc:
                    errors_by_provider[p] = str(exc)

            if not merged_events and errors_by_provider:
                return _error_command(
                    "calendar_read_failed",
                    "读取日程失败：" + "；".join(errors_by_provider.values()),
                    tool_call_id,
                    errors=errors_by_provider,
                    **({"first_touch_notice": first_touch_notice} if first_touch_notice else {}),
                )

            merged_events.sort(
                key=lambda ev: str(
                    (ev.get("start") or {}).get("dateTime")
                    or (ev.get("start") or {}).get("date")
                    or ""
                )
            )
            if act == "check_availability":
                availability = compute_free_busy_slots(
                    merged_events,
                    time_min=time_min,
                    time_max=time_max,
                    tz_name=tz_name,
                    min_slot_minutes=duration_minutes,
                )
                return _tool_command(
                    _attach_notice(
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
                        }
                    ),
                    tool_call_id,
                )
            return _tool_command(
                _attach_notice(
                    {
                        "ok": True,
                        "action": "list_events",
                        "providers": target_providers,
                        "timezone": tz_name,
                        "events": merged_events,
                        "count": len(merged_events),
                        **({"partial_errors": errors_by_provider} if errors_by_provider else {}),
                    }
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
                if p == "google":
                    mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                        google_client=_google_cli(),
                        caller_user_id=caller_user_id,
                        surface=surfaces[p],
                        token=tok,
                        tool_name="get_event",
                        arguments={"calendarId": cal_id, "eventId": event_id.strip()},
                        required_scopes=req_scopes,
                        tool_call_id=tool_call_id,
                        config=config,
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
                    _attach_notice({"ok": True, "action": "get_event", "event": ev}),
                    tool_call_id,
                )
            except GraphInterrupt:
                raise
            except (
                IntegrationNeedsReauthError,
                IntegrationNotConnectedError,
                PersonalIntegrationError,
                GoogleCalendarAPIError,
                FeishuCalendarAPIError,
            ) as exc:
                return _error_command(
                    "calendar_read_failed",
                    str(exc),
                    tool_call_id,
                    **({"first_touch_notice": first_touch_notice} if first_touch_notice else {}),
                )

        # ------------------------------------------------------------------
        # WRITE ACTIONS (create_event, update_event, delete_event) -> HITL L1/L2
        # ------------------------------------------------------------------
        if _is_unattended_automation_turn(config):
            return _error_command(
                "automation_turn_confirmation_forbidden",
                "后台自动任务无法弹出人工确认卡片，请在本人私密对话中操作个人日历写入。",
                tool_call_id,
            )

        p = target_providers[0]
        conn_row = connected_rows[p]
        surf = surfaces[p]
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
        except (
            IntegrationNeedsReauthError,
            IntegrationNotConnectedError,
            PersonalIntegrationError,
        ) as exc:
            return _error_command(
                "needs_reauth" if isinstance(exc, IntegrationNeedsReauthError) else "calendar_auth_failed",
                str(exc),
                tool_call_id,
                settings_path=_SETTINGS_INTEGRATIONS_PATH,
            )

        # Pre-read existing event on update/delete for diff & attendee detection (§5.2)
        existing_event: dict[str, Any] | None = None
        pre_read_marker: str | None = None
        if act in ("update_event", "delete_event"):
            assert event_id is not None
            try:
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
            except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
                return _error_command(
                    "event_pre_read_failed",
                    f"读取待操作日程失败：{exc}",
                    tool_call_id,
                )

        # Determine risk level (L2 when external attendees exist on input or existing event)
        ext_attendees = _extract_external_attendees(
            attendees, owner_email=conn_row.provider_email
        )
        if existing_event is not None:
            for email in _extract_external_attendees(
                existing_event.get("attendees"), owner_email=conn_row.provider_email
            ):
                if email.lower() not in {e.lower() for e in ext_attendees}:
                    ext_attendees.append(email)
        requires_second_confirm = len(ext_attendees) > 0
        risk_level = "L2" if requires_second_confirm else "L1"
        ttl_seconds = (
            L2_APPROVAL_TTL_SECONDS if requires_second_confirm else L1_APPROVAL_TTL_SECONDS
        )

        # Build provider-specific normalized write payload
        if p == "google":
            if act == "delete_event":
                assert event_id is not None
                write_payload: dict[str, Any] = {
                    "calendarId": cal_id,
                    "eventId": event_id.strip(),
                    "notificationLevel": "ALL" if requires_second_confirm else "NONE",
                }
            else:
                write_payload = build_google_event_write_args(
                    calendar_id=cal_id,
                    event_id=event_id.strip() if event_id else None,
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
                )
        else:
            if act == "delete_event":
                assert event_id is not None
                write_payload = {
                    "calendar_id": cal_id,
                    "event_id": event_id.strip(),
                    "need_notification": requires_second_confirm,
                }
            else:
                write_payload = build_feishu_event_body(
                    summary=summary,
                    description=description,
                    start_time=start_time,
                    end_time=end_time,
                    tz_name=tz_name,
                    all_day=all_day,
                    location=location,
                    reminders=reminders,
                    need_notification=requires_second_confirm,
                )

        action_type = f"dalton_calendar_{act}"
        approval_id = str(uuid4())
        payload_sha = canonical_payload_sha256(
            {
                "provider": p,
                "calendar_id": cal_id,
                "action": act,
                "event_id": event_id.strip() if event_id else "",
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
            event_id=event_id.strip() if event_id else None,
            pre_read_updated_marker=pre_read_marker,
            normalized_payload=write_payload,
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        await db.create_action_approval(
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
            expires_at=expires_at,
        )

        card_args: dict[str, Any] = {
            "provider": p,
            "provider_label": _provider_display_name(p),
            "account_email": conn_row.provider_email,
            "calendar_id": cal_id,
            "action": act,
            "event_id": event_id.strip() if event_id else None,
            "summary": summary or (existing_event.get("summary") if existing_event else ""),
            "start_time": start_time,
            "end_time": end_time,
            "timezone": tz_name,
            "all_day": all_day,
            "location": location,
            "description": description,
            "reminders": reminders,
            "use_default_reminders": use_default_reminders,
            "external_attendees": ext_attendees,
            "before": existing_event,
        }
        action_request: dict[str, Any] = {
            "type": action_type,
            "name": "dalton_calendar",
            "tool_call_id": tool_call_id,
            "approval_id": approval_id,
            "caller_user_id": caller_user_id,
            "provider": p,
            "service": "calendar",
            "confirm_token": confirm_token,
            "payload_sha256": payload_sha,
            "risk_level": risk_level,
            "requires_second_confirm": requires_second_confirm,
            "args": card_args,
        }
        if surf.is_dm_channel:
            action_request.update(
                await _notify_dm_for_web_approval(
                    surface=surf,
                    summary_text=f"{_provider_display_name(p)} {act}",
                )
            )

        desc_action = {
            "create_event": "新建日程",
            "update_event": "修改日程",
            "delete_event": "删除日程",
        }[act]
        resume = hitl_confirm(
            description=(
                f"确认在{_provider_display_name(p)}中{desc_action}"
                + ("（含其他参会人，将发送会议通知）" if requires_second_confirm else "")
            ),
            action_requests=[action_request],
        )

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
        if str(decision.get("type") or "").strip().lower() != "approve":
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
                != payload_sha
            ):
                await db.mark_action_approval_status(
                    approval_id, "rejected", caller_user_id=caller_user_id
                )
                return _error_command(
                    "second_confirmation_required",
                    "包含其他参会人的日历写入需要勾选二次确认后再执行。",
                    tool_call_id,
                )

        # Atomic one-time consumption + surface/epoch re-verification
        consumed = await db.consume_action_approval(approval_id, caller_user_id)
        if consumed is None:
            return _error_command(
                "approval_expired_or_consumed",
                "该确认卡片已过期或已被使用，请重新发起操作。",
                tool_call_id,
            )

        if not await check_thread_surface_still_private(surf.thread_id, caller_user_id):
            await db.mark_action_approval_status(
                approval_id, "expired", caller_user_id=caller_user_id
            )
            return _error_command(
                "scope_changed",
                "确认期间对话私密状态已变更，已中止本次写入。",
                tool_call_id,
            )

        # Acquire fresh token right before external write HTTP (outside any lock)
        try:
            fresh_tok = await ensure_fresh_access_token(
                caller_user_id,
                p,
                "calendar",
                min_remaining_seconds=600,
                required_scopes=req_scopes,
            )
        except (
            IntegrationNeedsReauthError,
            IntegrationNotConnectedError,
            PersonalIntegrationError,
        ) as exc:
            await db.mark_action_approval_status(
                approval_id, "failed", caller_user_id=caller_user_id
            )
            return _error_command("calendar_auth_failed", str(exc), tool_call_id)

        if (
            fresh_tok.auth_epoch != consumed.auth_epoch
            or fresh_tok.provider_account_id != consumed.provider_account_id
        ):
            await db.mark_action_approval_status(
                approval_id, "expired", caller_user_id=caller_user_id
            )
            return _error_command(
                "scope_changed",
                "确认期间日历授权已变更，请重新发起操作。",
                tool_call_id,
            )

        # Execute upstream write
        try:
            if p == "google":
                mcp_res, err_cmd = await _call_google_mcp_with_elicitation(
                    google_client=_google_cli(),
                    caller_user_id=caller_user_id,
                    surface=surf,
                    token=fresh_tok,
                    tool_name=act,
                    arguments=write_payload,
                    required_scopes=req_scopes,
                    tool_call_id=tool_call_id,
                    config=config,
                )
                if err_cmd is not None:
                    await db.mark_action_approval_status(
                        approval_id, "failed", caller_user_id=caller_user_id
                    )
                    return err_cmd
                await db.mark_action_approval_status(
                    approval_id, "succeeded", caller_user_id=caller_user_id
                )
                if act == "delete_event":
                    return _tool_command(
                        _attach_notice(
                            {
                                "ok": True,
                                "action": "delete_event",
                                "provider": "google",
                                "calendar_id": cal_id,
                                "event_id": event_id,
                                "deleted": True,
                            }
                        ),
                        tool_call_id,
                    )
                raw_ev = (mcp_res or {}).get("event") or mcp_res or {}
                return _tool_command(
                    _attach_notice(
                        {
                            "ok": True,
                            "action": act,
                            "provider": "google",
                            "event": normalize_google_event(raw_ev, calendar_id=cal_id),
                        }
                    ),
                    tool_call_id,
                )
            else:
                if act == "create_event":
                    ev = await _feishu_cli().create_event(
                        access_token=fresh_tok.access_token,
                        calendar_id=cal_id,
                        body=write_payload,
                        attendees=attendees,
                        tz_name=tz_name,
                    )
                    await db.mark_action_approval_status(
                        approval_id, "succeeded", caller_user_id=caller_user_id
                    )
                    return _tool_command(
                        _attach_notice(
                            {"ok": True, "action": "create_event", "provider": "feishu", "event": ev}
                        ),
                        tool_call_id,
                    )
                if act == "update_event":
                    assert event_id is not None
                    ev = await _feishu_cli().update_event(
                        access_token=fresh_tok.access_token,
                        calendar_id=cal_id,
                        event_id=event_id.strip(),
                        body=write_payload,
                        tz_name=tz_name,
                    )
                    await db.mark_action_approval_status(
                        approval_id, "succeeded", caller_user_id=caller_user_id
                    )
                    return _tool_command(
                        _attach_notice(
                            {"ok": True, "action": "update_event", "provider": "feishu", "event": ev}
                        ),
                        tool_call_id,
                    )
                assert event_id is not None
                del_res = await _feishu_cli().delete_event(
                    access_token=fresh_tok.access_token,
                    calendar_id=cal_id,
                    event_id=event_id.strip(),
                    need_notification=requires_second_confirm,
                )
                await db.mark_action_approval_status(
                    approval_id, "succeeded", caller_user_id=caller_user_id
                )
                return _tool_command(
                    _attach_notice({"ok": True, "action": "delete_event", **del_res}),
                    tool_call_id,
                )
        except GraphInterrupt:
            raise
        except (GoogleCalendarAPIError, FeishuCalendarAPIError) as exc:
            await db.mark_action_approval_status(
                approval_id, "failed", caller_user_id=caller_user_id
            )
            return _error_command(
                "calendar_write_failed",
                f"执行{desc_action}失败：{exc}",
                tool_call_id,
            )

    return [dalton_calendar]


__all__ = ["create_calendar_tools"]
