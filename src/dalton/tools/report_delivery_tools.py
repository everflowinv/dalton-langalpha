"""Direct LangChain tool `dalton_report_delivery` for self-service `report_profile` config."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from src.dalton.services.report_delivery import (
    ReportDeliveryError,
    clear_report_delivery_config,
    compute_report_delivery_confirm_token,
    delete_report_delivery_hitl_snapshot,
    get_report_delivery_config,
    load_report_delivery_hitl_snapshot,
    precheck_report_delivery_set,
    save_report_delivery_hitl_snapshot,
    set_report_delivery_config,
)
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.tools.secretary._commands import (
    InjectedToolCallId,
    hitl_confirm,
)

logger = logging.getLogger(__name__)

DeliveryActionLiteral = Literal["get", "set", "clear"]

AUTOMATION_TURN_CONFIRMATION_REFUSED_MESSAGE = (
    "无人值守自动化轮次无法弹出用户确认卡片，禁止直接配置群/频道或非本人私信目标；"
    "请由用户在交互对话中手动确认配置。"
)
CONFIRM_TOKEN_MISMATCH_MESSAGE = (
    "确认期间任务配置或发送目标发生变化（或确认校验未通过），已取消本次设置，请重新发起。"
)


def _tool_command(payload: Mapping[str, Any], tool_call_id: str) -> Command:
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(dict(payload), ensure_ascii=False, default=str),
                    tool_call_id=tool_call_id,
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
    """True when invoked inside an unattended background automation turn."""
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


def _extract_confirm_token(response: Any) -> str | None:
    """Extract ``confirm_token`` from ``decisions[0]`` of a ``hitl_confirm`` response."""
    if not isinstance(response, Mapping):
        return None
    decisions = response.get("decisions")
    if (
        not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
        or not decisions
    ):
        return None
    first = decisions[0]
    if not isinstance(first, Mapping):
        return None
    raw_token = first.get("confirm_token")
    if isinstance(raw_token, str) and raw_token.strip():
        return raw_token.strip()
    return None


def _format_card_targets(
    targets: Sequence[str] | None,
    labels: Mapping[str, str],
    *,
    empty_text: str = "无",
    none_text: str = "未设置",
) -> list[str] | str:
    if targets is None:
        return none_text
    if not targets:
        return empty_text
    return [labels.get(t, t) for t in targets]


def _format_text_card_targets(
    text_targets: Sequence[str] | None,
    report_targets: Sequence[str],
    labels: Mapping[str, str],
) -> list[str] | str:
    if text_targets is None:
        if report_targets:
            inherited = [labels.get(t, t) for t in report_targets]
            return f"沿用报告卡片目标：{'、'.join(inherited)}"
        return "不发送纯文本（无目标）"
    if not text_targets:
        return "不发送纯文本"
    return [labels.get(t, t) for t in text_targets]


def _build_confirm_card_payload(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    report_targets = list(snapshot.get("report_targets") or [])
    raw_text = snapshot.get("text_targets")
    text_targets = list(raw_text) if isinstance(raw_text, list) else None
    failure_targets = list(snapshot.get("failure_targets") or [])
    group_targets = list(snapshot.get("group_targets") or [])
    non_caller_dm_targets = list(snapshot.get("non_caller_dm_targets") or [])
    raw_labels = snapshot.get("target_labels")
    target_labels = raw_labels if isinstance(raw_labels, Mapping) else {}
    automation_id = str(snapshot.get("automation_id") or "")
    automation_name = str(snapshot.get("automation_name") or automation_id)
    config_version = snapshot.get("config_version")
    profile = str(snapshot.get("profile") or "")
    confirm_token = str(snapshot.get("confirm_token") or "")
    tool_call_id = str(snapshot.get("tool_call_id") or "")

    report_display = _format_card_targets(
        report_targets, target_labels, empty_text="无"
    )
    text_display = _format_text_card_targets(
        text_targets, report_targets, target_labels
    )
    failure_display = _format_card_targets(
        failure_targets, target_labels, empty_text="无"
    )
    card_args: dict[str, Any] = {
        "任务": automation_name,
        "报告类型": profile,
        "报告卡片目标": report_display,
        "纯文本目标": text_display,
        "失败告警目标": failure_display,
    }
    if config_version is not None:
        card_args["配置版本"] = f"v{config_version}"
    summary_parts: list[str] = []
    if group_targets:
        summary_parts.append(f"含群/频道目标 {len(group_targets)} 个")
    if non_caller_dm_targets:
        summary_parts.append(f"含非本人私信目标 {len(non_caller_dm_targets)} 个")
    reason_note = "、".join(summary_parts) or "含需确认的外发目标"
    description = (
        f"确认更新定时报告任务「{automation_name}」的发送对象"
        f"（{reason_note}，报告类型：{profile}）。"
    )
    payload: dict[str, Any] = {
        "name": "确认定时报告发送对象",
        "args": card_args,
        "action": "set",
        "automation_id": automation_id,
        "automation_name": automation_name,
        "config_version": config_version,
        "profile": profile,
        "report_targets": report_targets,
        "text_targets": text_targets,
        "failure_targets": failure_targets,
        "group_targets": group_targets,
        "non_caller_dm_targets": non_caller_dm_targets,
        "description": description,
        "confirm_token": confirm_token,
    }
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload


def create_report_delivery_tools() -> list[BaseTool]:
    """Build `dalton_report_delivery` for internal/admin Dalton runtimes."""

    @tool("dalton_report_delivery")
    async def dalton_report_delivery(
        action: DeliveryActionLiteral,
        automation_id: str | None = None,
        profile: str | None = None,
        report_targets: list[str] | None = None,
        failure_targets: list[str] | None = None,
        text_targets: list[str] | None = None,
        user_confirmed: bool | None = None,  # Deprecated & ignored; server uses HITL card confirmation.
        config: RunnableConfig = None,  # type: ignore[assignment]
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """读取、配置或撤销定时报告任务（调用 `automation_report` 的任务）的发送对象（`metadata.report_profile` 与通知路由）。

        适用范围（仅内部成员和管理员可用，且只能修改自己的任务；管理员可代改）：
        - 与 `dalton_notify_settings` 的区别：凡是用报告技能（调用 `automation_report` 提交卡片/文字）的定时任务，投递目标由任务的 `metadata.report_profile` 决定，并用本工具配置；本工具在写入 `report_profile` 时会同步打开对应的 `automation:<id>` 通知路由。

        动作说明：
        - `action="get"`：查看 `automation_id` 当前的发送配置（`report_profile`、成功/失败目标、是否被属主推送总开关或 `automation:<id>` 路由拦截），并列出可选的发送对象（已绑定私信渠道 + 已开通报告接收且非管理员调用者在指定 `automation_id` 时会核对为群成员的群/频道）。省略 `automation_id` 时列出你的报告类任务与可选目标（群列表此时不逐群核对成员资格，`membership_status="unchecked_in_list"`）。
        - `action="set"`：配置 `report_profile` 并打开 `automation:<id>` 路由。
          - `automation_id` 必填。
          - `profile`：需在 `REPORT_PROFILES` 中且与任务指令引用的技能匹配；若任务指令可唯一推断（如 `dalton-daily-filing-alert` -> `daily-filing-alert`）可省略，推断不出时必须显式传入。
          - `report_targets`：报告卡片目标列表（仅支持已授权 `feishu:chat:oc_*` 或已绑定 `feishu:user:ou_*`）。
          - `text_targets`：可选纯文字摘要目标列表（支持飞书、Discord 或个人微信 `weixin:user:*`）。
          - `failure_targets`：可选失败告警目标列表（仅支持 `discord:user:*` 或 `[]`；省略时默认使用任务属主的私信（已绑定的 Discord 私信），无绑定则为 `[]`）。
          - 真实用户确认机制：若目标包含任何群/频道（`feishu:chat:*` / `discord:channel:*`）或非调用者本人的私信目标，服务端会自动弹出交互确认卡片，待用户在界面核对并批准后生效；仅设置调用者本人已绑定私信目标无需确认。未在群报告授权名单里的群或非群成员会被拒绝；后台无人值守自动化轮次禁止配置群/频道或非本人私信目标。
        - `action="clear"`：撤销指定任务的 `report_profile` 并清理其 `automation:<id>` 路由（无需额外确认，审计记录同步写入任务审计历史 `audit_history`）。

        Returns:
            Command with a JSON ToolMessage carrying `ok` status, current/updated `report_profile`, notify eligibility state, selectable targets, audit version, or rejection `error`/`message`.
        """
        # Explicitly ignore deprecated model-supplied boolean `user_confirmed`.
        _ = user_confirmed

        caller_user_id, _, thread_id, _ = _extract_run_identity(config)
        if not caller_user_id:
            return _error_command(
                "missing_identity",
                "无法识别当前用户身份，不能操作定时报告发送配置。",
                tool_call_id,
            )

        act = str(action or "").strip().lower()
        if act in ("set", "clear"):
            from src.dalton.settings import is_dalton_sharing_auth_enabled

            if is_dalton_sharing_auth_enabled():
                from src.dalton.colleague_outbound_guard import (
                    OUTBOUND_GUARD_REFUSAL_MESSAGE,
                    is_run_outbound_blocked,
                )

                if await is_run_outbound_blocked(config):
                    return _error_command(
                        "colleague_outbound_blocked",
                        OUTBOUND_GUARD_REFUSAL_MESSAGE,
                        tool_call_id,
                    )
        try:
            if act == "get":
                res = await get_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=automation_id,
                )
                return _tool_command(res, tool_call_id)

            if act == "set":
                if not automation_id or not str(automation_id).strip():
                    return _error_command(
                        "invalid_request",
                        "action='set' 必须提供 automation_id。",
                        tool_call_id,
                    )
                if report_targets is None and text_targets is None:
                    return _error_command(
                        "invalid_request",
                        "action='set' 必须至少提供 report_targets 或 text_targets。",
                        tool_call_id,
                    )
                aid = str(automation_id).strip()
                raw_report_targets = list(report_targets or [])
                eff_thread_id = str(thread_id or "")

                # Resume path: load any persisted pending confirmation snapshot by
                # tool_call_id BEFORE running precheck so state changes while the
                # card was pending (e.g. owner unbinding Discord) can never skip
                # hitl_confirm or bypass an explicit user rejection.
                pending_snapshot = await load_report_delivery_hitl_snapshot(
                    thread_id=eff_thread_id,
                    tool_call_id=tool_call_id,
                    caller_user_id=caller_user_id,
                )

                hitl_verified = False
                verified_profile: str | None = None
                verified_report_targets: list[str] = []
                verified_text_targets: list[str] | None = None
                verified_failure_targets: list[str] = []
                write_expected_cv: int | None = None

                if pending_snapshot is None:
                    precheck = await precheck_report_delivery_set(
                        caller_user_id=caller_user_id,
                        automation_id=aid,
                        profile=profile,
                        report_targets=raw_report_targets,
                        failure_targets=failure_targets,
                        text_targets=text_targets,
                    )
                    verified_profile = precheck.profile
                    verified_report_targets = list(precheck.report_targets)
                    verified_text_targets = (
                        list(precheck.text_targets)
                        if precheck.text_targets is not None
                        else None
                    )
                    verified_failure_targets = list(precheck.failure_targets)
                    write_expected_cv = precheck.config_version

                    if precheck.needs_confirmation:
                        if _is_unattended_automation_turn(config):
                            return _error_command(
                                "automation_turn_confirmation_forbidden",
                                AUTOMATION_TURN_CONFIRMATION_REFUSED_MESSAGE,
                                tool_call_id,
                            )
                        confirm_token = compute_report_delivery_confirm_token(
                            action="set",
                            caller_user_id=caller_user_id,
                            automation_id=precheck.automation_id,
                            config_version=precheck.config_version,
                            profile=str(verified_profile or ""),
                            report_targets=verified_report_targets,
                            text_targets=verified_text_targets,
                            failure_targets=verified_failure_targets,
                        )
                        pending_snapshot = {
                            "thread_id": eff_thread_id,
                            "tool_call_id": tool_call_id,
                            "caller_user_id": caller_user_id,
                            "automation_id": precheck.automation_id,
                            "automation_name": precheck.automation_name,
                            "config_version": precheck.config_version,
                            "profile": str(verified_profile or ""),
                            "report_targets": list(verified_report_targets),
                            "text_targets": (
                                list(verified_text_targets)
                                if verified_text_targets is not None
                                else None
                            ),
                            "failure_targets": list(verified_failure_targets),
                            "group_targets": list(precheck.group_targets),
                            "non_caller_dm_targets": list(precheck.non_caller_dm_targets),
                            "target_labels": dict(precheck.target_labels),
                            "needs_confirmation": True,
                            "confirm_token": confirm_token,
                        }
                        await save_report_delivery_hitl_snapshot(pending_snapshot)

                if pending_snapshot is not None and pending_snapshot.get("needs_confirmation"):
                    card_payload = _build_confirm_card_payload(pending_snapshot)
                    approved, hitl_response = hitl_confirm(
                        "dalton_report_delivery_set",
                        card_payload,
                    )
                    await delete_report_delivery_hitl_snapshot(
                        thread_id=eff_thread_id,
                        tool_call_id=tool_call_id,
                        caller_user_id=caller_user_id,
                    )
                    if not approved:
                        return _error_command(
                            "user_declined",
                            f"用户取消了定时报告任务「{pending_snapshot['automation_name']}」的发送对象设置。",
                            tool_call_id,
                        )

                    snap_token = str(pending_snapshot.get("confirm_token") or "")
                    returned_token = _extract_confirm_token(hitl_response)
                    if not returned_token or returned_token != snap_token:
                        return _error_command(
                            "confirm_token_mismatch",
                            CONFIRM_TOKEN_MISMATCH_MESSAGE,
                            tool_call_id,
                        )

                    snap_aid = str(pending_snapshot["automation_id"])
                    snap_profile = str(pending_snapshot["profile"])
                    snap_report = list(pending_snapshot["report_targets"])
                    snap_text = (
                        list(pending_snapshot["text_targets"])
                        if pending_snapshot.get("text_targets") is not None
                        else None
                    )
                    snap_failure = list(pending_snapshot["failure_targets"])
                    snap_cv = pending_snapshot.get("config_version")

                    # Re-check authorization and config_version against the exact
                    # frozen snapshot targets rather than re-running initial precheck.
                    precheck_now = await precheck_report_delivery_set(
                        caller_user_id=caller_user_id,
                        automation_id=snap_aid,
                        profile=snap_profile,
                        report_targets=snap_report,
                        failure_targets=snap_failure,
                        text_targets=snap_text,
                    )
                    current_token = compute_report_delivery_confirm_token(
                        action="set",
                        caller_user_id=caller_user_id,
                        automation_id=precheck_now.automation_id,
                        config_version=precheck_now.config_version,
                        profile=str(precheck_now.profile or ""),
                        report_targets=precheck_now.report_targets,
                        text_targets=precheck_now.text_targets,
                        failure_targets=precheck_now.failure_targets,
                    )
                    if (
                        precheck_now.config_version != snap_cv
                        or returned_token != current_token
                        or snap_token != current_token
                    ):
                        return _error_command(
                            "confirm_token_mismatch",
                            CONFIRM_TOKEN_MISMATCH_MESSAGE,
                            tool_call_id,
                        )
                    hitl_verified = True
                    aid = snap_aid
                    verified_profile = precheck_now.profile
                    verified_report_targets = list(precheck_now.report_targets)
                    verified_text_targets = (
                        list(precheck_now.text_targets)
                        if precheck_now.text_targets is not None
                        else None
                    )
                    verified_failure_targets = list(precheck_now.failure_targets)
                    write_expected_cv = precheck_now.config_version

                res = await set_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=aid,
                    profile=verified_profile,
                    report_targets=verified_report_targets,
                    failure_targets=verified_failure_targets,
                    text_targets=verified_text_targets,
                    hitl_confirmed=hitl_verified,
                    expected_config_version=write_expected_cv,
                )
                return _tool_command(res, tool_call_id)

            if act == "clear":
                if not automation_id or not str(automation_id).strip():
                    return _error_command(
                        "invalid_request",
                        "action='clear' 必须提供 automation_id。",
                        tool_call_id,
                    )
                res = await clear_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=str(automation_id).strip(),
                )
                return _tool_command(res, tool_call_id)

            return _error_command(
                "invalid_action",
                f"不支持的 action={action!r}，请使用 get / set / clear。",
                tool_call_id,
            )
        except GraphInterrupt:
            raise
        except ReportDeliveryError as exc:
            return _error_command(exc.code, exc.message, tool_call_id)
        except Exception as exc:
            logger.exception("[DALTON_REPORT_DELIVERY] Unexpected failure for action=%s", act)
            return _error_command(
                "internal_error",
                f"配置定时报告发送对象时发生内部错误：{type(exc).__name__}",
                tool_call_id,
            )

    return [dalton_report_delivery]
