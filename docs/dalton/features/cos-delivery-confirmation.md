# 研究总监外发配置真实用户确认方案

> **背景**：`dalton_report_delivery(action="set")` 当前依赖模型传入布尔参数 `user_confirmed`（`src/dalton/tools/report_delivery_tools.py:42`、`src/dalton/services/report_delivery.py:2118`），服务端无法辨别是否真的经过真人确认，存在模型自行传 `user_confirmed=true` 或在提示注入下绕过确认的风险；同时私信目标检查（`report_delivery.py:2085-2096`）对管理员代发给任务属主等“非调用者本人”的私信未要求任何确认。本方案用服务端真实交互确认替代模型布尔自证。

## 1. 需要真实确认的外发范围

| 场景 | 是否需要真实确认 | 说明 |
|---|---|---|
| **群发 / 频道目标**（`feishu:chat:*`、`discord:channel:*` 等 `kind != "dm"`） | **必须确认** | 无论调用者是普通内部成员还是管理员，向多人可见群/频道新增或变更投递目标均需真人批准。 |
| **私信给发消息的人以外的人**（`kind == "dm"` 且目标不在 `caller_dm_set`） | **必须确认** | 例如管理员代改他人任务，将报告或失败告警私信投递给任务属主（`owner_dm_set`）或其他同事账号。 |
| **仅设置调用者本人已绑定私信**（`canonical in caller_dm_set`） | 无需确认 | 仅影响调用者本人接收，不扩大外部受众。 |
| `action="get"` 查询、`action="clear"` 撤销配置 | 无需确认 | 只读或收紧外发范围。 |
| 管理员通过已鉴权 Web/REST 接口直接操作（`via_rest=True`） | 无需二次确认 | 请求本身即为真人点击前端界面的显式操作，保持现有语义。 |

后续如将即时消息外发（如 `send_message`）开放至非调用者本人目标，亦复用同一“群发或私信他人必经真实确认”基线。

## 2. 复用的现有机制：HITL 中断 + `confirm_token` 确认卡片

对比仓库内三类现有确认机制：

- **`cli_approval.py`**（`src/server/dependencies/cli_approval.py` + `services/cli_approvals.py`）：专为沙箱内 `la` CLI 携带 `computer_id` 令牌调 HTTP API 设计（Redis 挂起 + 长轮询），不适用于后端直调的 LangChain 工具。
- **`publish_report` 审阅中断**（`src/dalton/publish/service.py:2333`）：绑定发布审阅单与 PM 审批队列，形态过重。
- **推荐复用：`hitl_confirm` + 确认卡片 + `confirm_token` 绑定**（与 `dalton_notify_settings` 同源）：
  - 后端：`src/tools/secretary/_commands.py:42` 的 `hitl_confirm`（基于 LangGraph `interrupt()`）+ `src/dalton/tools/notify_tools.py:65-108, 537-615` 的 SHA-256 `confirm_token` 绑定与恢复后重算校验；
  - 协议：`src/server/models/chat.py:120` 的 `HITLDecision.confirm_token`；
  - 前端：`web/src/pages/ChatAgent/session/interrupts/toolApprovalCard.ts:12-22, 70-90` 与 `answerBoard.ts:50-55` 的确认卡片渲染与令牌回传（同时兼容历史回放与渠道网关 `pending_interrupt`）。

**执行闭环**：工具先完成全部格式、技能匹配、群授权名单与群成员资格预检 → 若命中需确认目标，服务端基于 `(action, caller_user_id, automation_id, profile, sorted_targets)` 计算 `confirm_token` 并发出 `hitl_confirm("dalton_report_delivery_set", ...)` → 用户在确认卡片核对任务名、报告类型与具体接收对象后点击批准，前端原样回传 `confirm_token` → 工具恢复后重新拉取任务与目标校验并重算 `confirm_token`，两者完全一致才落库（防 TOCTOU 与参数漂移，模型全程无法伪造恢复载荷）。

## 3. 改动范围

1. **工具层**（`src/dalton/tools/report_delivery_tools.py`）：废弃并忽略模型入参 `user_confirmed`；工具改为返回 `Command`（注入 `InjectedToolCallId`），需确认时调用 `hitl_confirm` + `confirm_token` 核验后再提交落库。
2. **服务层**（`src/dalton/services/report_delivery.py`）：复用 `validate_report_profile_change` 拆出只读预检结果（返回是否需要确认及规范化目标列表）；在 `:2085-2122` 将需确认条件补齐为“含群/频道目标 **或** 含非 `caller_dm_set` 的私信目标”；服务端不再采信模型传入的布尔标志，仅接受经 HITL 令牌核验后的内部标志或 `via_rest=True`。
3. **卡片与路由登记**（`src/tools/secretary/_commands.py`、`web/src/pages/ChatAgent/session/interrupts/toolApprovalCard.ts`）：在 `_CARDS` 注册 `dalton_report_delivery`（`{"set"}`），在前端 `DALTON_NOTIFY_APPROVAL_TYPES`（或同级 Dalton 确认集合）加入 `"dalton_report_delivery_set"`，复用现有确认卡片与 `confirm_token` 回传。
4. **测试与工具说明**：补齐批准/拒绝/令牌不匹配/恢复期任务变更/非本人私信单测，并移除工具说明中“传 `user_confirmed=true`”的指引。

## 4. 对现有自动化（定时任务）的影响

- **已配置定时任务的日常执行：零影响**。定时任务运行期通过 `automation_report` 按已保存的 `metadata.report_profile` 投递，不调用 `dalton_report_delivery`，不触发 HITL 中断。
- **后台自动化轮次尝试修改群发或他人私信配置**：无人值守轮次没有前端真人可点击确认卡片，直接 `interrupt()` 会导致后台执行挂起。改动落地时在工具入口识别非交互自动化轮次（如 `configurable` 含 `automation_execution_id` 或 `platform == "automation"`），命中需确认目标时直接返回明确拒绝错误，要求由用户在交互对话中手动确认配置；仅查询或配置本人私信仍可正常执行。
- **REST 管理入口与系统迁移脚本**：继续走 `via_rest=True` 或服务端迁移路径，不受影响。
