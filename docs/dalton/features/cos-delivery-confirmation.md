# 研究总监外发配置真实用户确认方案

> **背景**：`dalton_report_delivery(action="set")` 当前依赖模型传入布尔参数 `user_confirmed`（`src/dalton/tools/report_delivery_tools.py:42`、`src/dalton/services/report_delivery.py:2118`），服务端无法辨别是否真的经过真人确认，存在模型自行传 `user_confirmed=true` 或在提示注入下绕过确认的风险；同时私信目标检查（`report_delivery.py:2085-2096`）对管理员代发给任务属主等“非调用者本人”的私信未要求任何确认。本方案用服务端真实交互确认替代模型布尔自证。

## 1. 需要真实确认的外发范围与免除边界

| 场景 | 是否需要真实确认 | 说明 |
|---|---|---|
| **群发 / 频道目标**（`feishu:chat:*`、`discord:channel:*` 等 `kind != "dm"`） | **必须确认** | 无论调用者是普通内部成员还是管理员，在对话中向多人可见群/频道新增或变更投递目标均需真人批准。 |
| **私信给发消息的人以外的人**（`kind == "dm"` 且目标不在 `caller_dm_set`） | **必须确认** | 例如管理员代改他人任务，将报告、纯文本或失败告警私信投递给任务属主（`owner_dm_set`）或其他同事账号。 |
| **仅设置调用者本人已绑定私信**（`canonical in caller_dm_set`） | 无需确认 | 仅影响调用者本人接收，不扩大外部受众。 |
| `action="get"` 查询、`action="clear"` 撤销配置 | 无需确认 | 只读或收紧外发范围。 |
| **网页端直接提交创建或编辑（`POST /api/v1/automations` 与 `PATCH /api/v1/automations/{automation_id}`）** | **必须确认（需一次性确认凭据）** | **仅凭 `principal.auth == "session"` 与 `via_rest=True` 均不算确认**。用户（含管理员）本人的登录令牌在外部脚本、curl 或沙箱脚本中调用接口时，`principal.auth` 同样解析为 `"session"`（`src/server/dependencies/principal.py:215-217`），服务端无法仅凭登录态区分浏览器真人点击与脚本调用。因此 `POST /api/v1/automations`（`src/server/app/automations.py:172-194`）与 `PATCH /api/v1/automations/{automation_id}`（`automations.py:562-606`）执行同一套规则：配置群发或非本人私信时，必须由网页上的确认卡片先向服务端申请并由真人点击生成一次性确认凭据（绑定操作人、目标载荷、任务版本与短时效），随请求回传并由服务端核对且消费该一次性凭据后方可放行；未携带有效一次性凭据的 `"session"` 请求一律拒绝。 |
| **受信任的服务端内部迁移路径（`principal.auth == "service"` / 显式内部迁移调用）** | 可免除二次确认 | 仅限运维/系统迁移脚本或受信任服务间调用，不可由外部用户令牌触发。 |
| **非交互令牌请求（计算机令牌 `principal.auth == "computer"`、OAuth、PAT）** | **不可免除（不算用户确认）** | **`via_rest=True`、计算机令牌以及未附带网页确认卡片一次性凭据的 `"session"` 均不算用户确认**。`POST /api/v1/automations`（`src/server/app/automations.py:157-208`）与 `PATCH /api/v1/automations/{automation_id}`（`automations.py:562-606`）均接入了 `WorkspaceSendPrincipal` 且传入 `via_rest=True`；开启计算机令牌时，同机 CLI 编辑可跳过现有 `cli_approval`（`src/server/dependencies/cli_approval.py:790-793`）。因此 REST 层不能只凭 `via_rest=True` 或 `principal.auth == "session"` 豁免确认：除受信任 `"service"` 内部迁移外，所有 `POST`/`PATCH` 变更群目标或他人私信均必须携带网页确认卡片产生并经服务端核对的一次性确认凭据，`computer` / `oauth` / `pat` 令牌一律拒绝或走显式审批。 |

后续如将即时消息外发（如 `send_message`）开放至非调用者本人目标，亦复用同一“群发或私信他人必经真实确认”基线。

## 2. 复用的现有机制：HITL 中断 + `confirm_token` 确认卡片

对比仓库内三类现有确认机制：

- **`cli_approval.py`**（`src/server/dependencies/cli_approval.py` + `services/cli_approvals.py`）：专为沙箱内 `la` CLI 携带 `computer_id` 令牌调 HTTP API 设计（Redis 挂起 + 长轮询），不适用于后端直调的 LangChain 工具。
- **`publish_report` 审阅中断**（`src/dalton/publish/service.py:2333`）：绑定发布审阅单与 PM 审批队列，形态过重。
- **推荐复用：`hitl_confirm` 直调 + 确认卡片 + `confirm_token` 绑定**（与 `dalton_notify_settings` 完全同源）：
  - **后端实际接线**：工具内直接调用 `src/tools/secretary/_commands.py:42` 的 `hitl_confirm("dalton_report_delivery_set", {...})` 发起 LangGraph `interrupt()`，复用 `src/dalton/tools/notify_tools.py:65-108, 537-615` 的 SHA-256 `confirm_token` 生成与恢复后重算校验。**无需也不应在 `_commands.py:70` 的 `_CARDS` 中注册**（`_CARDS` 仅列研究总监自有工具且 `puts_card` 在 `src/` 内无调用点，`dalton_notify_settings` 同样直接调 `hitl_confirm` 而不在 `_CARDS` 中）。
  - **协议**：复用 `src/server/models/chat.py:120` 的 `HITLDecision.confirm_token`。
  - **前端实际接线**：在 `web/src/pages/ChatAgent/session/interrupts/toolApprovalCard.ts:12-22, 44-52, 70-95` 的 `DALTON_NOTIFY_APPROVAL_TYPES`（或同级 Dalton 确认类型集合）加入 `"dalton_report_delivery_set"`，由 `isToolApprovalRequest` 与 `buildToolApprovalState` 直接渲染确认卡片并缓存令牌，再经 `answerBoard.ts:50-55` 原样回传 `confirm_token`（同时兼容历史回放与渠道网关 `pending_interrupt`）。

**执行闭环与令牌绑定**：
1. 工具先完成全部格式、技能匹配、群授权名单与群成员资格预检，并读取目标任务当前的 `config_version`。
2. 若命中需确认目标，服务端分别规范化并排序三类目标——**报告目标（`report_targets`）**、**纯文本目标（`text_targets`，区分 `None` 与 `[]`）**、**失败通知目标（`failure_targets`）**——连同任务配置版本一起纳入 SHA-256 哈希，计算 `confirm_token`：
   ```text
   confirm_token = sha256(canonical_json({
       "action": "set",
       "caller_user_id": caller_user_id,
       "automation_id": automation_id,
       "config_version": config_version,
       "profile": profile,
       "report_targets": sorted(norm_report_targets),
       "text_targets": sorted(norm_text_targets) if norm_text_targets is not None else None,
       "failure_targets": sorted(norm_failure_targets),
   }))
   ```
   三类目标各自独立绑定（不合并成单一列表），避免将同一接收对象在失败告警、纯文本与报告卡片之间挪动却复用同一批准；同时绑定 `config_version`，防止确认期间任务指令、技能或归属被并发修改。
3. 发出 `hitl_confirm("dalton_report_delivery_set", {...})`；用户在确认卡片分别核对任务名、报告类型以及报告/纯文本/失败通知三类接收对象后点击批准，前端原样回传 `confirm_token`。
4. 工具恢复后重新拉取任务（校验 `config_version` 未变）并重跑预检与 `confirm_token` 计算，仅当回传令牌与重算令牌完全一致才以 `expected_config_version=config_version` 原子落库（防 TOCTOU 与参数漂移，模型全程无法伪造恢复载荷）。

## 3. 改动范围

1. **工具层**（`src/dalton/tools/report_delivery_tools.py`）：废弃并忽略模型入参 `user_confirmed`；工具改为返回 `Command`（注入 `InjectedToolCallId`），需确认时直接调用 `hitl_confirm("dalton_report_delivery_set", ...)` 并完成 `confirm_token`（含三类目标与 `config_version`）重算核验后再落库。
2. **服务层与 REST 鉴权边界**（`src/dalton/services/report_delivery.py`、`src/server/app/automations.py`）：
   - 从 `validate_report_profile_change` 拆出只读预检结果（返回是否需要确认、规范化后的三类目标及任务 `config_version`）；在 `:2085-2122` 将需确认条件补齐为“含群/频道目标 **或** 含非 `caller_dm_set` 的私信目标”。
   - 迁移 `set_report_delivery_config`（`:2821-2891`）与 `clear_report_delivery_config`（`:3065-3073`）：去掉 `set` 路径的模型布尔入参 `user_confirmed`，改为仅接受经 HITL 令牌核验后的内部标志；去掉 `clear` 路径内部硬传的 `user_confirmed=True`，由 `action == "clear"` 显式免确认。
   - **同时覆盖 `POST /api/v1/automations` 创建入口（`automations.py:157-208`，`report_profile` 校验位于 `:172-194`）与 `PATCH /api/v1/automations/{automation_id}` 编辑入口（`automations.py:562-606`）**，两者执行同一套确认规则：不再将 `via_rest=True` 或 `principal.auth == "session"` 等同于真人确认（因为用户本人的登录令牌在脚本里调用接口时同样解析为 `principal.auth == "session"`，`principal.py:215-217`）。除受信任 `"service"` 内部迁移调用外，凡在 `POST`/`PATCH` 中配置群发或非本人私信的请求，均须携带由网页确认卡片交互产生的一次性确认凭据，并由服务端核对且消费该一次性凭据后才放行；未带有效一次性凭据的 `"session"` 请求以及 `computer` / `oauth` / `pat` 令牌请求一律拒绝。
3. **前端确认卡片接线**（`web/src/pages/ChatAgent/session/interrupts/toolApprovalCard.ts` 及定时任务表单确认卡片）：
   - 对话工具路径：在 `DALTON_NOTIFY_APPROVAL_TYPES`（或同级 Dalton 确认集合）加入 `"dalton_report_delivery_set"`，复用现有 `toolApprovalCard.ts` + `answerBoard.ts` 的确认卡片渲染与 `confirm_token` 回传，不改动 `_commands.py:_CARDS`。
   - REST 表单路径（创建与编辑定时任务）：网页提交含群发或非本人私信的 `POST /automations` / `PATCH /automations/{id}` 前弹出确认卡片，由确认卡片交互触发服务端签发并绑定目标载荷的一次性确认凭据，随提交请求回传给服务端核验。
4. **测试与工具说明**：补齐批准/拒绝/令牌不匹配/跨目标类型窜改/恢复期 `config_version` 变更/非本人私信/`POST` 与 `PATCH` 缺少一次性确认凭据（含 `session` 脚本直调与 `computer` 令牌）拦截单测，并移除工具说明中“传 `user_confirmed=true`”的指引。

## 4. 对现有自动化（定时任务）的影响

- **已经保存好配置的定时投递，照原方式执行（零影响、不强制重确认）**。存量已保存的 `metadata.report_profile`（包括早期通过旧参数保存的群发配置）在定时触发时继续由 `automation_report` 按已保存配置直接投递，不调用 `dalton_report_delivery`，不触发 HITL 中断；仅在后续通过对话或 REST 创建/编辑接口新增或变更群目标、他人私信时才触发新版真实确认。
- **后台自动化轮次尝试修改群发或他人私信配置**：无人值守轮次没有前端真人可点击确认卡片，直接 `interrupt()` 会导致后台执行挂起。改动落地时在工具入口识别非交互自动化轮次（如 `configurable` 含 `automation_execution_id` 或 `platform == "automation"`），命中需确认目标时直接返回明确拒绝错误，要求由用户在交互对话中手动确认配置；仅查询、清除或配置调用者本人私信仍可正常执行。
- **网页端提交与系统迁移脚本**：网页端通过 `POST /automations` 创建或 `PATCH /automations/{id}` 编辑群发/非本人私信时，须完成网页确认卡片并携带一次性确认凭据供服务端核验，不能仅凭 `principal.auth == "session"` 跳过确认；仅受信任服务端以 `"service"` 身份执行的内部迁移保持免二次确认，计算机令牌（`la` CLI）与其他非交互 API 令牌同样不再因 `via_rest=True` 获得豁免。
