"""Direct LangChain tool `dalton_report_delivery` for self-service `report_profile` config."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.services.report_delivery import (
    ReportDeliveryError,
    clear_report_delivery_config,
    get_report_delivery_config,
    set_report_delivery_config,
)
from src.dalton.tools.knowledge_tools import _extract_run_identity

logger = logging.getLogger(__name__)

DeliveryActionLiteral = Literal["get", "set", "clear"]


def _json_error(error: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "error": error, "message": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


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
        user_confirmed: bool = False,
        config: RunnableConfig = None,  # type: ignore[assignment]
    ) -> str:
        """读取、配置或撤销定时报告任务（调用 `automation_report` 的任务）的发送对象（`metadata.report_profile` 与通知路由）。

        适用范围（仅内部成员和管理员可用，且只能修改自己的任务；管理员可代改）：
        - 与 `dalton_notify_settings` 的区别：凡是用报告技能（调用 `automation_report` 提交卡片/文字）的定时任务，投递目标由任务的 `metadata.report_profile` 决定，并用本工具配置；本工具在写入 `report_profile` 时会同步打开对应的 `automation:<id>` 通知路由。

        动作说明：
        - `action="get"`：查看 `automation_id` 当前的发送配置（`report_profile`、成功/失败目标、是否被属主推送总开关或 `automation:<id>` 路由拦截），并列出可选的发送对象（已绑定私信渠道 + 已开通报告接收且非管理员调用者在指定 `automation_id` 时会核对为群成员的群/频道）。省略 `automation_id` 时列出你的报告类任务与可选目标（群列表此时不逐群核对成员资格，`membership_status="unchecked_in_list"`）。
        - `action="set"`：配置 `report_profile` 并打开 `automation:<id>` 路由。
          - `automation_id` 必填。
          - `profile`：需在 `REPORT_PROFILES` 中且与任务指令引用的技能匹配；若任务指令可唯一推断（如 `dalton-daily-filing-alert` -> `daily-filing-alert`）可省略，推断不出时必须显式传入。
          - `report_targets`：报告卡片目标列表（仅支持已授权 `feishu:chat:oc_*` 或本人 `feishu:user:ou_*`）。
          - `text_targets`：可选纯文字摘要目标列表（支持飞书或 Discord）。
          - `failure_targets`：可选失败告警目标列表（仅支持 `discord:user:*` 或 `[]`；省略时默认使用任务属主的私信（已绑定的 Discord 私信），无绑定则为 `[]`）。
          - 群目标确认要求：**设置任何群/频道目标（`feishu:chat:*` / `discord:channel:*`）时，必须用户在本次对话里明确确认要发到该群，才可传 `user_confirmed=true`**；仅设置调用者本人私信目标无需确认。未在群报告授权名单里的群或非群成员会被拒绝。
        - `action="clear"`：撤销指定任务的 `report_profile` 并清理其 `automation:<id>` 路由（审计记录同步写入任务审计历史 `audit_history`）。

        Returns:
            JSON string with `ok` status, current/updated `report_profile`, notify eligibility state, selectable targets, audit version, or rejection `error`/`message`.
        """
        caller_user_id, _, _, _ = _extract_run_identity(config)
        if not caller_user_id:
            return _json_error(
                "missing_identity",
                "无法识别当前用户身份，不能操作定时报告发送配置。",
            )

        act = str(action or "").strip().lower()
        try:
            if act == "get":
                res = await get_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=automation_id,
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "set":
                if not automation_id or not str(automation_id).strip():
                    return _json_error("invalid_request", "action='set' 必须提供 automation_id。")
                if report_targets is None and text_targets is None:
                    return _json_error(
                        "invalid_request",
                        "action='set' 必须至少提供 report_targets 或 text_targets。",
                    )
                res = await set_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=str(automation_id).strip(),
                    profile=profile,
                    report_targets=list(report_targets or []),
                    failure_targets=failure_targets,
                    text_targets=text_targets,
                    user_confirmed=bool(user_confirmed),
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "clear":
                if not automation_id or not str(automation_id).strip():
                    return _json_error("invalid_request", "action='clear' 必须提供 automation_id。")
                res = await clear_report_delivery_config(
                    caller_user_id=caller_user_id,
                    automation_id=str(automation_id).strip(),
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            return _json_error(
                "invalid_action",
                f"不支持的 action={action!r}，请使用 get / set / clear。",
            )
        except ReportDeliveryError as exc:
            return _json_error(exc.code, exc.message)
        except Exception as exc:
            logger.exception("[DALTON_REPORT_DELIVERY] Unexpected failure for action=%s", act)
            return _json_error("internal_error", f"配置定时报告发送对象时发生内部错误：{type(exc).__name__}")

    return [dalton_report_delivery]
