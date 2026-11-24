# 研究总监（Chief of Staff）设置类工具设计方案

> **文档路径**：`docs/dalton/features/cos-settings-tools.md`  
> **修订轮次**：第 2 轮定案版  
> **覆盖范围**：① 通过研究总监与网页归档工作区（服务端快照持久化、数据库栅栏与单事务复核、网页与研究总监一致的整机确认、源头日志与响应脱敏）；② 在指定电脑上新建工作区（电脑名称/ID 解析、快照绑定、自有电脑逐次确认、实时与回放卡片展示）；③ 托管自动化的 `llm_model`（取锁前预读偏好、事务内复核工作区/权限/电脑绑定与运行位置、跨人系统目录限制与私有模型名隐藏）；④ `.agy/cos_settings_audit.md` 遗漏项第 2–6 条完成度核对。

---

## 摘要

上游第 11 次同步引入了沙箱内命令行 `la`（`cli/src/commands/*.ts`）配合应用内全局弹窗确认（`src/server/dependencies/cli_approval.py:177-205`、`web/src/components/CliApproval/CliApprovalHost.tsx:32-94`）的工作区与线程操作路径。Dalton 侧为隔离跨人派活场景下的同机沙箱身份并保留对话流内持久化卡片与渠道通知，默认将电脑 `lac1.*` 令牌保持关闭态（`src/dalton/dalton.yaml:16-17`、`src/dalton/dalton.core.yaml:13-14`），挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`）。

结合两轮架构定案，本方案确立以下核心设计：

1. **确认快照持久化与第一方会话核验**：弹卡前在 `StandingApprovalMiddleware.aafter_model`（`src/tools/secretary/approvals.py:159-201`）按 `tool_call_id` 将解析结果写入线程私有状态 `approval_snapshots`，卡片展示该快照，`interrupt()` 恢复后仅读取该快照；常驻自动放行与后续建库共用通过自有电脑检查的同一 `computer_id`。后端在恢复入口（`src/server/handlers/checkpoint_handler.py:275-303`、`src/server/app/threads/messaging.py:266-315,633-656`）对归档与新建工作区两类中断强制核对第一方网页会话（`principal.auth == "session"`）、发起人本人、当前待确认 `interrupt_id` 及对应快照；执行前在数据库事务内复核快照，出现任何偏差即中止并在卡片展示「情况已变化，请重新发起」。
2. **归档采用数据库行锁栅栏，网页与研究总监双入口对齐**：研究总监通过只读查询校验本人归属、已停止态与提供商冷归档支持度（仅 Daytona 支持 `archives = True`，`src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`；Core 默认 Docker、E2B 与自有 SSH 电脑均在弹卡前明确告知不可用）；研究总监运行所在电脑正处于运行态，直接引导用户前往网页操作，网页端沙箱面板归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`）同步升级为列出同机工作区并提交快照复核的整机确认弹窗。数据库层沿用退役栅栏模式（`src/server/database/sql_fences.py:25-49`），在单事务内对 `computers` 行执行 `FOR UPDATE`、清点全部存活工作区（新增、移出、迁走、改名、`Home` 替代工作区变动均视为变化）并与快照精确比对，一致后原子写入 `status = 'stopping'` 与 `config.archiving = true`，由 `FENCE_TAKES_WORKSPACES` 阻断归档期间的新建与迁入，零数据库迁移落地。
3. **在指定电脑上新建工作区**：`manage_workspaces(action="create")` 增加可选参数 `computer`，复用 `pick_computer`（`src/server/services/computer_reach.py:120-152`）解析 `<activity>` 电脑名称、完整 UUID 或 8 位以上前缀；`manage_workspaces(action="list")` 在用户名下存活电脑达到 2 台及以上时同步输出 `computer_id`。目标电脑为用户自有电脑（`kind in MACHINE_KINDS`）时扣留常驻自动放行并逐次确认，在实时流与刷新回放中按 `tool_call_id` 准确呈现待执行、执行成功、用户取消与执行失败四类状态。
4. **托管自动化的 `llm_model` 与源头脱敏**：继续通过受 `autonomy` 设置门控（`src/dalton/tools/__init__.py:239,242,351,354,436-437`）的专用工具 `dalton_schedule_automation` 承载。在 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:934`）取锁前预读 `user_models.get_model_preference` 与工作区电脑快照；事务内复核工作区存活、操作者权限及实际绑定的电脑一致，显式保存相同模型时同样复核运行位置；跨人显式赋值一律按服务端 `effective_actor_id` 限定系统目录模型并强制按系统目录端点解析，在跨人 `list`、`llm_model` 拒绝消息与 `reasoning_effort` 错误中全程隐藏私有自定义模型名（展示为「所有者自定义模型」）；在 `_archive_machine`、`_folder_ready_result` 及两个 HTTP 归档入口源头改为仅记录与返回白名单字段；飞书、Discord、微信统一仅发送中文通用确认提示与登录保护链接。

---

## 1. 现状核对与设置审计评估

### 1.1 上游 `la` 机制与 Dalton 现行运行状态

- **上游 `la` + `cli_approval` 链路**：每个电脑沙箱在启动或重连时由 `cli_tokens.keep()`（`src/server/services/cli_tokens.py:121-174`）写入 `lac1.<computer_id>.<secret>` 令牌文件（`LANGALPHA_TOKEN_FILE`，`cli/README.md:28-33`）。研究总监在 `Bash` 中调用 `la` 访问带有 `require_scopes` 声明的接口（`src/server/dependencies/principal.py:360-460`）；涉及删除、新建工作区、移除电脑或跨电脑启动运行时，`cli_approval.gate`（`src/server/dependencies/cli_approval.py:177-205`）仅对携带 `computer_id` 的电脑令牌生效（网页 `session` 请求在 `:179` 直接放行），将审批请求写入 Redis（`src/server/services/cli_approvals.py:192-408`，有效期 30 分钟，结案后保留 10 分钟），并在批准后重新计算请求意图摘要比对一致性（`src/server/dependencies/cli_approval.py:200-204`）。
- **Dalton 现行令牌开关与工具挂载**：`src/dalton/dalton.yaml:16-17` 与 `src/dalton/dalton.core.yaml:13-14` 均将 `computer_cli_tokens.enabled` 设为 `false`（读取入口 `src/dalton/settings.py:622-627`）。由 `DALTON-HOOK(computer-cli-tokens-off)` 在三处静态收口：`cli_tokens.keep()` 直接返回 `False`（`src/server/services/cli_tokens.py:131-138`）、`CliTokenSweeper` 跳过巡检（`src/server/services/cli_token_sweep.py:57-62,103-110`）、`get_principal()` 拦截 `lac1.*` 凭证（`src/server/dependencies/principal.py:197-205`）。研究总监在主智能体构建阶段挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`），需用户确认的动作通过 `hitl_confirm`（`src/tools/secretary/_commands.py:42-65`）触发 LangGraph `interrupt()`，在对话流内渲染 `ProposalCard`（`web/src/pages/ChatAgent/components/ProposalCard.tsx:44-121`）。

### 1.2 三项待设计能力的三端现状对照

| 能力项 | 上游 `la` 命令行现状 | 网页端与 REST 接口现状 | Dalton 智能体工具现状 |
|---|---|---|---|
| **1. 归档工作区** | `la workspaces archive <ws>`（`cli/src/commands/workspaces.ts:147-156`）调用 `POST /api/v1/workspaces/{id}/archive`（`src/server/app/workspaces.py:945-974`）。**现存缺口**：① `cli/README.md:151-153` 将归档列为直通动作，路由缺少 `cli_approval` 门控；② 路由依赖 `WorkspaceWriteUserId`（`src/server/dependencies/principal.py:429,452`），且路径参数只有 `workspace_id`，`_computer_limits`（`src/server/dependencies/principal.py:343-348`）仅在路径含 `computer_id` 时拦截本机自归档；③ Redis 审批记录结案保留 10 分钟（`src/server/services/cli_approvals.py:69`），缺少对话流持久化回放。 | 沙箱面板概览页归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`、`web/src/pages/ChatAgent/components/SandboxSettingsPanel.tsx:96-114`）直接调用 `POST /api/v1/workspaces/{id}/archive`，缺少同机工作区影响确认与快照提交；后端 `_archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1872-1908`）仅使用单进程 `asyncio.Lock`（`_observed_lock`，`src/server/services/computer_manager/_sessions.py:252-275`），而新建工作区（`src/server/database/workspace.py:906-914`）与重绑定（`:533-545`）走独立数据库事务且 `FENCE_TAKES_WORKSPACES`（`src/server/database/sql_fences.py:25-38`）缺少归档栅栏；失败恢复日志（`_lifecycle.py:1900-1904`）与路由 `RuntimeError` 分支（`workspaces.py:164-168`、`src/server/app/computers.py:267-270`）会记录或返回原始异常文本。 | `manage_workspaces` 当前包含 `list`、`create`、`update`、`delete` 与 4 项跨区文件动作（`src/tools/secretary/chief_of_staff.py:98,121-166`），`archive` 动作待接入；前端卡片渲染链（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:14-27`、`web/src/pages/ChatAgent/components/messageList/buildRenderBlocks.ts:454`、`contentProjection.ts:53-58`、`SecretaryConfirmCard.tsx:5`）当前仅登记 `delete_workspace`、`stop_workspace`、`delete_thread`。 |
| **2. 指定电脑新建工作区** | `la workspaces create <name> [--description D] [--computer C]`（`cli/src/commands/workspaces.ts:113-146`）调用 `POST /api/v1/workspaces`。`cli_approval.workspace_create`（`src/server/dependencies/cli_approval.py:385-429`）解析主电脑或指定 `computer_id` 写入意图摘要，批准后锁定该电脑创建（`src/server/app/workspaces.py:278-281`），`CliApprovalDialog` 通过 `workspaceCreateOn` 显示目标电脑名（`web/src/lib/cliApprovals/copy.ts:129-134`）。**现存缺口**：① `--computer` 与 `WorkspaceCreate.computer_id`（`src/server/models/workspace.py:60-66`）仅接受 UUID，而 `<activity>` 输出电脑显示名（`src/tools/secretary/activity.py:261-274`）；② 常驻免批分支（`src/server/dependencies/cli_approval.py:399-400`）缺少同名/同代码去重与自有电脑扣留；③ 聊天内联条 `laWorkspaceCard` 仅展示工作区名称（`web/src/pages/ChatAgent/utils/preapprovedCards.ts:50-68`）。 | 新建弹窗通过 `useCreateWorkspace`（`web/src/pages/ChatAgent/hooks/useCreateWorkspace.ts:106-124`）传 `computerId` 至 `POST /api/v1/workspaces`（`src/server/app/workspaces.py:249-424`）→ `WorkspaceManager.create_workspace(..., computer_id=...)`（`src/server/services/workspace_manager.py:140-237`），含 `_chosen_computer` 归属与待退役校验（`:224-237`）及首次准备态校验（`:186-190`）。 | `<activity>`（`src/tools/secretary/activity.py:238-274`）与 `manage_workspaces(action="list")`（`src/tools/secretary/tools.py:88-93`、`src/tools/secretary/utils.py:17-25,91-108`）在多电脑下仅展示电脑名称；`manage_workspaces(action="create")` 参数表固定落在主电脑（`src/tools/secretary/chief_of_staff.py:55-77`、`src/tools/secretary/tools.py:286-305,439-443`）；`CreateWorkspaceCard` 仅展示名称与描述（`web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx:6-14,67-86`）；`_folder_ready_result` 日志直接拼接异常对象（`src/tools/secretary/tools.py:199-200,212-214,229-230,250-252,280-281`）。 |
| **3. 托管自动化的 `llm_model`** | `la automations create/edit -m MODEL`（`cli/src/commands/automations.ts:55-100`）调用通用 `/api/v1/automations`，经 `cli_approval.automation_runs`（`src/server/dependencies/cli_approval.py:701-823`）预检 `check_model` 与 `check_claude_code_hosted`（`:758-764`）。**作用域边界**：通用接口限定为调用者本人常规工作区（`src/server/database/automation.py:595-602`）并将覆盖单元受管元数据设为只读保护（`src/server/services/automations/lifecycle.py:95-152`），主分析师代管任务、`Home` 全局任务及公司任务走专用服务层。 | 网页通用保存路径（`src/server/app/automations.py:157-231,538-700` → `src/server/services/automations/lifecycle.py:618-760,865-1025,1295-1303` 及 `src/dalton/services/company_agent_writes.py:267-382`）在取行锁前调用 `_safe_model_pref`（底层为 `user_models.get_model_preference`，`lifecycle.py:358-363,1295-1303`）加载 `pref_for_lock`；更新初检阶段（`prepare_automation_update`，`:872-895`）在新模型异于当前值时调用 `check_model`（含自定义服务商父级 `:345`），在模型或工作区变化时调用 `check_claude_code_hosted`（`:878-879`），新建阶段对缺省模型通过 `_claude_code_default_for` 检查默认 Claude Code 规则（`:296-335,663`）；行锁回调（`recheck_locked_automation_update`，`:933-1022`）仅在锁内行与初检行发生工作区或模型漂移且目标位置/模型变化时重查 `check_claude_code_hosted`（`:989-999`），并重算 `_reconcile_reasoning_effort`（`:1002-1021`）。Dalton 专属保存路由 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）复用 `schedule_dalton_automation_helper`。 | 个人任务可通过 `.agents/user/automations/*.json` 修改 `llm_model`（`src/ptc_agent/agent/backends/automations.py:148`），受管任务由 `src/server/services/automations/file.py:1027` 分流至专用工具。`dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1588`）参数表尚待加入 `llm_model`；底层 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:771-791,934,1260-1267,1350-1462,1555-1609`）在 `_user_automations_lock_for_write` 锁内才读取偏好（`:1267`），缺少模型校验、Claude Code 运行位置校验、事务内工作区存活/权限/电脑绑定复核，且跨人 `list`（`:784`）直接返回所有者私有 `llm_model` 原文。 |

### 1.3 `.agy/cos_settings_audit.md` 遗漏优先级第 2–6 条完成情况评估

审计文档「遗漏优先级」共列 6 条（高优先级 2 条、中优先级 3 条、低优先级 1 条）。其中第 1 条（高-1：保留专用 `upsert` 更新时的原时区）已在 `src/dalton/tools/director_tools.py:1543,1569` 与 `src/dalton/autonomy/schedule.py:743-744,1215,1336-1337` 完成修复。第 2–6 条逐项核对结果如下：

| 审计序号（原优先级） | 审计要求摘要 | 当前完成状态 | 源码核对位置（`文件:行号`） |
|---|---|---|---|
| **第 2 条（高-2）** | 修正通用自动化说明（区分个人文件与受管专用工具），修正回收站删除与保留期说明。 | **已完成** | `<user_profile>` 与 `<role>` 已写明个人文件与 `dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs` 的边界并按能力门控（`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2:90-93`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:54-58`）；自动化 `README.md` 已补齐受管分流说明及回收站 30 天保留与网页恢复说明（`src/ptc_agent/agent/backends/automations.py:52-56,69-73,264-296`；回执文案 `src/server/services/automations/file.py:1086-1089`）。 |
| **第 3 条（中-1）** | 补齐普通投递、完成通知、定时报告与总开关的关系，修正总开关页面路径与 Dalton 渠道示例。 | **已完成** | 总开关关闭提示已指向「消息与渠道 → 自动推送与接收账号 → 自动推送总开关」（`src/dalton/tools/notify_tools.py:56,668-675`）；交付规范模板已明确三类投递入口与总开关核对要求（`src/ptc_agent/agent/prompts/templates/components/dalton_deliverable.md.j2:13-16`）；`dalton_report_delivery` 工具说明已区分 `automation_report` 任务（`src/dalton/tools/report_delivery_tools.py:45-60`）；自动化与渠道 `README` 示例已更新为飞书、Discord、个人微信并要求先查 `available.json`（`src/ptc_agent/agent/backends/automations.py:42-43,108,153,217-348`、`src/ptc_agent/agent/backends/channels.py:54-62,68,82-90`）。 |
| **第 4 条（中-2）** | 补工作区元数据更新（`name`、`description`）和新建时选择计算机。 | **部分完成（`update` 已完成，新建选电脑归入本文 §2.3）** | `manage_workspaces(action="update")` 已上线并校验管理权限、保护 `Home` 保留行、提示目录改名影响（`src/tools/secretary/chief_of_staff.py:98-101,134-139`、`src/tools/secretary/tools.py:517-616`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:103`）；新建工作区指定电脑随前次 PR 撤回，按本文 §2.3 落地。 |
| **第 5 条（中-3）** | 补研究总监工作区归档入口及整台电脑受影响范围确认说明。 | **待完成（归入本文 §2.2）** | 网页端现有沙箱面板归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`）与研究总监侧入口按本文 §2.2 统一落地整机确认与数据库栅栏复核。 |
| **第 6 条（低-1）** | 角色提示补工作区删除及确认要求，模型区块补当前对话切换，自动化说明补 `workspace_id` 定位与迁移差异。 | **已完成** | 角色模板已写明 `manage_workspaces(action="delete")` 仅在用户明确要求时调用、先核对名称并等待确认（`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:103`）；`<model>` 区块已补充 `switch_thread_model` 用法与下条消息生效规则（`src/ptc_agent/agent/prompts/templates/envelope/baseline_model.md.j2:15-16`）；文件 `README` 与 `dalton_schedule_automation` 参数说明已分别写清迁移解绑固定线程与仅作定位的差异（`src/ptc_agent/agent/backends/automations.py:146`、`src/dalton/tools/director_tools.py:1544`）。 |

---

## 2. 定案方案与详细设计

### 2.1 通道定案与服务端确认快照持久化机制

#### （1）总体通道与边界定案

为隔离跨人派活场景下的共享系统用户令牌（`src/server/services/workspace_manager.py:306`、`src/server/services/cli_tokens.py:152`）、保留 PostgreSQL 检查点内的历史对话确认卡片回放（`src/tools/secretary/_commands.py:54-64`）并打通聊天渠道的 `interrupt` 事件监听（`integrations/dalton-channel-gateway/src/orchestrator.ts:6228-6233`），本设计执行以下四项定案：

1. **保持 `computer_cli_tokens.enabled = false`**，研究总监工作区操作统一通过服务端直连工具 `manage_workspaces` + 应用内 `hitl_confirm` 确认卡片承载，受管自动化通过专用工具 `dalton_schedule_automation` 承载；底层电脑解析、数据库归档栅栏、模型与运行位置复核与网页端同源复用。
2. **目标为用户自有电脑（`kind in MACHINE_KINDS`）的新建工作区逐次确认**：即使开启了 `auto_approve_workspace_creation`，目标为自有 SSH / 本地机器时一律扣留自动放行并弹出确认卡片（卡片隐藏「始终允许」按钮）。
3. **跨人修改受管自动化仅允许系统目录模型**（清除覆盖回退所有者默认值单独保留），并在写入校验、跨人列表查询与错误提示中全程隐藏所有者的私有自定义模型名称。
4. **`dalton_schedule_automation` 本次仅新增开放 `llm_model`**，`thread_strategy="new"` 与 `max_failures=3` 维持受管任务统一策略。

#### （2）确认快照持久化机制（恢复只读快照）

本仓库工具从 LangGraph `interrupt()` 恢复时会从函数首行重新执行（见 `src/tools/secretary/approvals.py:6-9` 注释）。若仅在工具局部变量中解析目标电脑和同机工作区，等待确认期间主电脑切换、工作区迁移或同机新增工作区会导致恢复执行偏离卡片所示内容。为此与现有 `preapproved_calls` 采用同一机制持久化服务端快照：

1. **线程私有状态扩展（`src/tools/secretary/approvals.py:159-205`）**：
   - 在 `StandingApprovalState` 中新增按 `tool_call_id` 索引的私有状态字段：
     ```python
     class StandingApprovalState(AgentState):
         preapproved_calls: NotRequired[Annotated[list[str], PrivateStateAttr]]
         approval_snapshots: NotRequired[Annotated[dict[str, dict[str, Any]], PrivateStateAttr]]
     ```
   - `StandingApprovalMiddleware.aafter_model` 在模型提出 `manage_workspaces(action="create")` 或 `manage_workspaces(action="archive")` 调用后、进入工具节点**之前**运行一次，完成目标电脑与整机工作区集合解析，并将结构化快照写入 `approval_snapshots[tool_call_id]` 落入检查点。
2. **常驻自动放行与快照绑定同一 `computer_id`**：
   - 对于 `manage_workspaces(action="create")`，`aafter_model` 先解析出唯一目标电脑并生成 `snapshot["computer_id"]` 与 `snapshot["computer_is_own_machine"]`，基于同一份快照判定是否满足 `auto_approve_workspace_creation`（非重名、校验成功、电脑就绪且 `computer_is_own_machine == False`）。放行加入 `preapproved_calls` 后，工具执行直接读取 `approval_snapshots[tool_call_id]["computer_id"]` 创建工作区，杜绝检查与执行两次解析之间主电脑切换到自有机器的竞态。
3. **弹卡与恢复只读快照**：
   - 工具首次运行进入 `hitl_confirm` 时，直接以 `approval_snapshots[tool_call_id]` 构造卡片载荷（携带 `tool_call_id`）；
   - 用户在第一方网页会话点击确认、`interrupt()` 恢复重入工具函数时，工具**仅读取 `state["approval_snapshots"][tool_call_id]`**，跳过一切重新解析；
   - 执行阶段将该快照传入数据库事务复核，发现任何状态或集合偏差即中止执行，返回 `error_command("情况已变化，请重新发起", tool_call_id, code="scope_changed")`，前端卡片按 `tool_call_id` 关联结算并展示「情况已变化，请重新发起」。
4. **自动化快照复核**：
   - `dalton_schedule_automation` 底层 `schedule_dalton_automation_helper` 在取锁前采集目标任务的工作区与实际绑定电脑快照 `(ws_id, snapshot_computer_id, target_model, pref_for_lock)`，并在数据库写事务内复核一致后才落库（详见 §2.4）。

```mermaid
flowchart TD
    M["模型输出工具调用 (tool_call_id)"] --> MW["StandingApprovalMiddleware.aafter_model<br/>解析电脑/整机工作区并持久化 approval_snapshots[tool_call_id]"]
    MW -->|"满足常驻自动放行 (云端主/指定电脑且唯一名称)"| EXEC_CREATE["读取快照同一 computer_id<br/>数据库事务内复核并创建"]
    MW -->|"归档 或 需确认的新建 (含自有电脑)"| INT["hitl_confirm interrupt()<br/>卡片展示快照 + 渠道仅发中文通用提示与登录链接"]
    INT -->|"第一方网页 session + 发起人本人 + 匹配 interrupt_id/快照"| RESUME["恢复重入工具函数<br/>只读 approval_snapshots[tool_call_id]"]
    RESUME --> TX["PostgreSQL 单事务 FOR UPDATE 复核快照<br/>(归档打 config.archiving=true 栅栏)"]
    TX -->|"完全一致"| DONE["执行动作并结算卡片为『执行成功』"]
    TX -->|"任意偏差"| ABORT["回滚事务，返回 code=scope_changed<br/>卡片显示『情况已变化，请重新发起』"]
```

---

### 2.2 能力一：通过研究总监与网页归档工作区

#### （1）弹卡前只读快照采集与运行中电脑引导（`src/tools/secretary/approvals.py`、`src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py`）

1. **工具入口与卡片登记**：
   - `manage_workspaces` 的 `action` 增加 `"archive"`，`_CARDS["manage_workspaces"]`（`src/tools/secretary/_commands.py:72`）登记 `"archive"`（每次均需本人网页确认）。
2. **纯只读预检（避免产生副作用的绑定操作）**：
   - `ComputerManager.resolve_binding()` 在遇到旧版未绑定工作区时会隐式领养或新建电脑记录（`src/server/services/computer_manager/_sessions.py:73-89`），预检阶段改为调用纯只读查询 `get_workspace(workspace_id)`（`src/server/database/workspace.py:294`）与 `get_computer_for_workspace(workspace_id)`（`src/server/database/computer.py:215-248`）：
     - 校验工作区存在且归属当前用户本人（`str(ws.get("user_id")) == user_id`，跨人或缺失统一记录快照错误 `"workspace not found"`）；
     - 校验目标为常规用户工作区（命中 `is_reserved_row(ws)` 记录 `"Home cannot be archived."`）；
     - **研究总监当前运行电脑引导**：若目标工作区绑定的电脑正是当前研究总监轮次正在运行的电脑（含 `Home` 主电脑运行态或 `Home` 替代工作区所在云端电脑运行态，本身处于 `running` 且持有当前活跃轮次），在弹卡前直接返回明确引导文案，说明当前对话正在该电脑上运行，请前往网页端沙箱面板（概览页）停止电脑后执行整机归档。若研究总监当前运行在云端替代工作区（`home_stand_in`），而用户请求归档的另一台已停止电脑上绑定有 `Home` 行，则将该 `Home` 行一并纳入整机快照正常弹卡确认；
     - **提供商冷归档能力与停止态预检**：读取 `provider_class(computer["kind"])` 对应的运行时类 `archives` 属性（基础默认值为 `False`，见 `src/ptc_agent/core/sandbox/runtime.py:549`；仅 Daytona 为 `True`，见 `src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`；Core 默认 Docker `agent_config.core.yaml:16`、E2B `src/ptc_agent/core/sandbox/providers/e2b_runtime.py:448-449` 及自有 SSH 机器均为 `False`）。对 `archives == False` 的电脑在弹卡前明确提示当前电脑类型未提供冷归档（停止电脑即可释放计算资源）；对 `computer["status"] != "stopped"`、缺少 `provider_ref`、存在活跃运行/后台命令或活跃迁移（`workspace_move_fences`，`src/server/database/sql_fences.py:105-114`）的电脑同样在弹卡前拦截说明。
3. **采集整机快照并写入 `approval_snapshots[tool_call_id]`**：
   - 查询目标 `computer_id` 上全部存活（`status <> 'deleted'`）的工作区行（含普通工作区、`Home` 行及 `home_stand_in` 替代工作区），按 `workspace_id` 排序生成完整快照项列表：
     ```json
     {
       "action": "archive_workspace",
       "tool_call_id": "<tool_call_id>",
       "workspace_id": "<target_ws_id>",
       "workspace_name": "<target_ws_name>",
       "computer_id": "<computer_id>",
       "computer_name": "<spoken_computer_name>",
       "expected_workspaces": [
         {
           "workspace_id": "<ws_id_1>",
           "name": "NVDA",
           "dir_name": "NVDA",
           "status": "stopped",
           "is_stand_in": false
         }
       ]
     }
     ```
   - 确认卡片向用户展示目标电脑名及所有非替代存活工作区名称与各自状态；底层复核使用完整的 `expected_workspaces` 快照（含 `is_stand_in` 行）。

#### （2）数据库行锁栅栏与单事务快照复核（`src/server/database/sql_fences.py`、`src/server/database/computer.py`、`src/server/services/computer_manager/_machines.py`、`src/server/services/computer_manager/_lifecycle.py`）

弃用仅在单进程内生效的 `_observed_lock` 作为范围复核边界，沿用退役路径的数据库栅栏设计（`src/server/database/sql_fences.py:25-49`、`src/server/database/computer.py:484-499`）：

1. **数据库栅栏扩展（`src/server/database/sql_fences.py:25-49`）**：
   - 在 `computers.config`（JSONB）使用服务端受管键 `{"archiving": true}` 标记「归档中」（复用既有 `config` JSONB 列与 `COMPUTER_STATUSES` 中的 `'stopping'` 状态，`src/server/database/computer.py:55-63`，零数据库迁移）。
   - 更新 `_takes_workspaces(c)`，在主电脑与非主电脑上统一排除归档中标记：
     ```python
     def _takes_workspaces(c: str) -> str:
         return (
             f"({c}is_primary OR NOT COALESCE({c}config, '{{}}'::jsonb)"
             " @> '{\"retire_when_empty\": true}'::jsonb)"
             f" AND NOT COALESCE({c}config, '{{}}'::jsonb)"
             " @> '{\"archiving\": true}'::jsonb"
         )
     ```
   - 在用于工作区迁入目标与主电脑候选筛选的 `takes_a_workspace_sql`（`src/server/database/sql_fences.py:41-49`）中同步追加 `AND {c}status <> 'stopping'`；而 `FENCE_TAKES_WORKSPACES` 保留普通空闲停止（`stopping` 且无 `archiving`）的主电脑可挂载新工作区的既有语义（对齐 `WorkspaceManager.create_workspace` 在 `src/server/services/workspace_manager.py:181-212` 中允许向停止过渡态主电脑新建工作区的设计），同时在归档期间凭借 `{"archiving": true}` 严格挡住所有 `create_workspace_on_computer`（`src/server/database/workspace.py:911`）、`bind_workspace_to_computer`（`:538`）、`claim_workspace_move`（`src/server/database/workspace_move.py:74`）及 `home_stand_in` 落位（`src/server/database/home_stand_in.py:86,100,216`）。
   - 在 `WorkspaceManager._chosen_computer`（`src/server/services/workspace_manager.py:234`）与 `create_workspace` 主电脑分支同步检查 `(computer.get("config") or {}).get("archiving")`，归档期间新建工作区返回清晰的 `ComputerArchivingError` 提示。
2. **单事务加锁清点与打标 `claim_computer_for_archive`（`src/server/database/computer.py`）**：
   - 在同一个 PostgreSQL 事务（`async with get_db_connection() as conn, conn.transaction():`）内按固定锁序执行：
     1) **锁定电脑行**：`SELECT {COMPUTER_COLS} FROM computers WHERE computer_id = %(computer_id)s AND status <> 'deleted' FOR UPDATE`（先锁 `computers` 再锁 `workspaces`，与 `shadowed_write` 锁序 `src/server/database/sql_fences.py:177-180` 保持一致，并与新建/迁入事务中的 `SELECT ... FROM computers ... FOR SHARE` 严格串行化）；
     2) **核验电脑状态与活跃工作**：确认 `status == 'stopped'`、`provider_ref IS NOT NULL`，且 `retire_when_empty`、`archiving` 与进行中的 `spec_change` 均为空置态，并在同一事务内查询该电脑零活跃运行（`computer_run_in_progress`）、零运行中命令（`computer_command_running`）及零进出该电脑的活跃迁移行（`workspace_moves.phase IN ('preparing', 'backing_up', 'staged', 'committed')` 且仍在有效租期内，`src/server/database/sql_fences.py:90-114`）；
     3) **清点同机工作区并精确比对快照**：在同一事务内执行 `SELECT workspace_id, name, dir_name, status, COALESCE(config, '{}'::jsonb) ? 'home_stand_in' AS is_stand_in FROM workspaces WHERE computer_id = %(computer_id)s AND status <> 'deleted' ORDER BY workspace_id FOR UPDATE`。将查询得到的完整元组序列与确认快照 `expected_workspaces` 做全量相等比对（目标工作区解绑或迁走、同机新增工作区、已有工作区删除移出、工作区改名或目录名变化、工作区状态变化、`Home` 替代工作区增减变化，**一律判定为变化**）；
     4) **提交或中止**：比对出现任何差异时立即抛出 `ArchiveScopeChangedError`，回滚事务（电脑行保持原状，跳过沙箱提供商调用）；完全一致时在同一事务内通过 `update_computer_status(computer_id, ComputerStatus.STOPPING, expected=ComputerStatus.STOPPED, merge_config={"archiving": True}, conn=conn)`（`src/server/database/computer.py:1039-1095`）原子更新电脑与同机工作区影子状态并打上 `{"archiving": true}` 标记后提交事务。
3. **归档结算与崩溃恢复清理（`src/server/services/computer_manager/_machines.py:628-642`、`src/server/services/computer_manager/_lifecycle.py:516-536,1889-1906`）**：
   - `_settle_machine_stop` 调用 `update_computer_status(computer_id, status, expected="stopping", drop_config_key="archiving")`，使正常归档完成、异常后的 `_reconcile_provider_stop` 以及跨进程心跳超时恢复均在同一条 SQL 更新中自动清除 `"archiving"` 标记。

#### （3）网页端归档按钮与 HTTP 归档路由一致性对齐（`OverviewTab.tsx`、`SandboxSettingsPanel.tsx`、`workspaces.py`、`computers.py`）

1. **网页端整机归档确认弹窗（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`、`web/src/pages/ChatAgent/components/SandboxSettingsPanel.tsx:96-114`）**：
   - 仅当目标电脑提供商支持冷归档（由沙箱统计或电脑信息返回 `archivable: boolean`）且 `state === 'stopped'` 时展示归档按钮。
   - 点击归档按钮时，先拉取该电脑当前同机存活工作区列表，弹出整机归档确认对话框（列出目标电脑名称与同机全部工作区名称及状态）；用户点击确认后，将快照 `expected_computer_id` 与 `expected_workspaces` 作为请求体提交至归档接口。若后端返回 `409`（`code="scope_changed"`，「情况已变化，请重新发起」），弹窗自动刷新最新同机工作区列表供用户重新核对。
2. **HTTP 归档路由门控与权限边界明确（`src/server/app/workspaces.py:945-974`、`src/server/app/computers.py:1107-1118`）**：
   - **请求载荷与快照复核**：两个路由均接收可选快照体（网页端与研究总监必传；未携带快照的外部直调请求也在路由内先行只读采集当前快照传入 `claim_computer_for_archive`，确保全部归档请求均走数据库栅栏）。
   - **身份依赖与本机令牌自归档拦截**：`POST /api/v1/workspaces/{workspace_id}/archive` 使用实际存在的 `WorkspaceWritePrincipal`（`src/server/dependencies/principal.py:429,440`）。因该路由路径参数只有 `workspace_id`，`_computer_limits`（`:343-348`）仅匹配路径中的 `computer_id` 参数；路由内先经只读 `get_computer_for_workspace(workspace_id)` 解析 `computer_id`，显式校验 `principal.computer_id == computer_id` 时抛出 `forbidden("A computer cannot stop, archive or remove itself")` 拦截本机自归档，并在电脑令牌开启的场景下与 `POST /api/v1/computers/{computer_id}/archive`（依赖 `ComputerEndUserId` / `require_scopes("computer:write", own_computer=False)`，`:434,456`）共用 `cli_approval.computer_archive` 整机审批构建器。
   - **HTTP 管理权限与研究总监本人权限的区分**：研究总监 `manage_workspaces(action="archive")` 面向个人 `Home` 助手场景，严格限定 `workspace["user_id"] == user_id` 本人工作区；HTTP 路由 `workspace_access(workspace, user_id, manage=True)`（`src/server/services/shared_workspaces/access.py:272-291`）对个人电脑同样限定所有者本人（非所有者在 `:287-288` 返回 403），仅在组织托管账户（`is_holder(owner)`）名下的共享工作区允许具备组织管理权的管理员通过网页端归档该共享电脑。

---

### 2.3 能力二：在指定电脑上新建工作区

1. **工具参数与多电脑列表消歧（`src/tools/secretary/chief_of_staff.py:55-133`、`src/tools/secretary/tools.py:65-95`）**：
   - `manage_workspaces` 新增可选参数 `computer: str | None = None`，支持传入 `<activity>` 或 `manage_workspaces(action="list")` 列出的电脑显示名、完整 UUID 或 8 位以上唯一前缀；省略时默认使用当前主电脑。
   - 现有 `ComputerPlacement.from_rows(rows)`（`src/tools/secretary/utils.py:17-25,91-108`）通过 `computer_label(row["name"], len(rows))` 控制：仅当用户名下存活电脑数量 `len(rows) >= 2` 时 `placement.computers` 非空并输出 `row["computer"]`。`workspaces_list` 在同一条件 `if placement.computers and ws.get("computer_id"):`（`src/tools/secretary/tools.py:90-91`）下同步写入 `row["computer_id"] = str(ws["computer_id"])`，实现单电脑精简、多电脑可消歧。
2. **快照生成与自有电脑逐次确认（`src/tools/secretary/approvals.py:88-205`、`src/tools/secretary/tools.py:286-504`）**：
   - 在 `StandingApprovalMiddleware.aafter_model` 中为 `manage_workspaces(action="create")` 构建快照 `approval_snapshots[tool_call_id]`：
     - 传入有效 `computer` 字符串时：读取 `roster = await get_computer_roster(user_id)`，调用 `pick_computer(roster, computer)`（`src/server/services/computer_reach.py:120-152`）解析唯一电脑；若重名或未找到，将 `ComputerUnreachable` 的可操作提示记入 `snapshot["precheck_error"]`（工具执行时直接返回 `error_command`）；
     - 省略 `computer` 时：调用 `await get_primary_computer(user_id)`（`src/server/database/computer.py:387-403`）记录当前主电脑 `computer_id` 与显示名（若新用户尚未分配电脑，记录 `computer_id = None` 标记首次初始化）；
     - 校验目标电脑处于可用就绪态（排除 `error`、`retire_when_empty`、`archiving` 及首次构建中的 `creating`/`starting` 态，对齐 `src/server/services/workspace_manager.py:186-190,231-236`），并记录 `computer_is_own_machine = (computer["kind"] in MACHINE_KINDS)` 与 `duplicate_workspace(user_id, name)` 结果；
     - 当 `computer_is_own_machine` 为真、存在同名/同股票代码工作区、存在 `precheck_error` 或去重读取失败时，一律扣留常驻自动放行，进入 `hitl_confirm("create_workspace", payload)` 弹卡确认；当满足常驻自动放行时，工具直接读取该快照中的 `computer_id` 创建工作区。
3. **恢复执行与事务内电脑一致性复核**：
   - `workspaces_create` 恢复后仅读取 `approval_snapshots[tool_call_id]`，将快照固化的 `computer_id` 传入 `WorkspaceManager.create_workspace(..., computer_id=snapshot_computer_id, expected_own_machine=snapshot_own_machine)`。
   - 在 `create_workspace_on_computer`（`src/server/database/workspace.py:904-938`）事务内 `FOR SHARE` 锁住 `computers` 行时，核验该电脑存活、归属 `user_id`、满足 `FENCE_TAKES_WORKSPACES` 可接收条件且 `status <> 'error'`；若等待确认期间目标电脑已删除、进入归档/退役或状态异常，中止创建并返回 `error_command("情况已变化，请重新发起", tool_call_id, code="scope_changed")`。

---

### 2.4 能力三：托管自动化的 `llm_model`

#### （1）参数语义统一（工具入口与 REST 入口）

受跨人主分析师归属（`src/dalton/autonomy/schedule.py:868-915`）、覆盖单元受管元数据保护（`src/server/services/automations/lifecycle.py:95-152`）及 `Home` 全局任务作用域（`src/server/database/automation.py:578-602`）约束，受管自动化由专用工具 `dalton_schedule_automation` 与 `PUT /api/v1/dalton/automations/{name}` 统一承载，消除省略与清除的歧义：

- **工具入口 `dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1588`）**：
  - 新增参数 `llm_model: str | None = None`；
  - `llm_model is None`（省略或传 `null`）：新建时继承任务所有者默认模型（`llm_model = None`），更新时**保留任务已有 `llm_model`**（在传入 helper 的 `spec` 中省略 `"llm_model"` 键）；
  - `llm_model.strip().lower() in ("default", "clear", "")`：在 `spec` 中显式写入 `spec["llm_model"] = None`，**清除任务级模型覆盖**并回退继承任务所有者默认模型；
  - 其他有效模型字符串：写入 `spec["llm_model"] = llm_model.strip()`。
- **REST 入口 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）**：
  - 沿用 `:1346-1350` 的字段存在性识别：请求体省略 `"llm_model"` 时保留原值；显式传入 `"llm_model": null`（或 `"default"` / `""`）时写入 `spec["llm_model"] = None` 清除覆盖。

#### （2）取锁前预读偏好与工作区电脑快照（`src/dalton/autonomy/schedule.py:934,1265-1267`）

- 在进入 `_user_automations_lock_for_write`（`src/dalton/autonomy/schedule.py:934`）之前：
  1. 当 `resolved_action == "upsert"` 时，先在锁外调用 `pref_for_lock = await _safe_model_pref(task_owner, None)`（底层调用 `user_models.get_model_preference(task_owner)`，`src/server/services/automations/lifecycle.py:358-363`），将锁内 `:1267` 的偏好读取完全移出写锁与行锁范围；
  2. 同时在锁外读取目标工作区快照 `(ws_id, snapshot_computer_id)`：常规工作区经 `get_computer_for_workspace(ws_id)`（`src/server/database/computer.py:215-248`）记录当前绑定的 `computer_id`，`Home` 工作区经 `home_turn_computer(task_owner)`（`src/server/services/claude_code/hosted.py:399-402`）记录运行电脑标识。

#### （3）跨人系统目录模型限制、同名自定义模型解析与全链路隐藏（C5）

以服务端计算的 `effective_actor_id`（`src/dalton/autonomy/schedule.py:1094-1104`）与 `task_owner` 区分本人与跨人操作：

1. **跨人显式赋值仅接受系统目录模型（`effective_actor_id != task_owner`）**：
   - 只要 `model_in_spec` 为真且 `spec_llm_model is not None`（**含显式提交与任务当前已存值相同的模型名**，每次显式赋值必检），一律仅对照系统目录 `get_configured_llm_models()` 与 `user_models.classify_model(task_owner, spec_llm_model, ignore_custom_models=True)`（`src/server/services/llm/user_models.py:264,281`）做校验，**全程跳过 `lifecycle.check_model(task_owner, ...)` 及 `get_custom_provider_config`**，直接拒绝任务所有者已保存或未保存的任何私有自定义模型。
   - **与所有者同名的自定义模型处理策略（选定：按系统目录端点解析，零探测侧信道）**：若按所有者 `custom_models` 是否存在同名项决定拒绝，跨人调用者可通过提交系统模型名观察是否被拒来反推所有者是否配置了同名自定义覆盖。本方案直接复用代码库现成的 `ignore_custom_models=True` 链路（`src/server/services/llm/config.py:685,715-724`、`src/server/services/llm/clients.py:349-1483`）：跨人显式写入系统目录模型时，在受管任务 `metadata` 中记录受保护布尔键 `"llm_model_system_only": True`（本人后续显式设置模型或任何人清除为 `None` 时移除该键）；自动化触发执行时（`src/server/services/automation_executor.py:950-965,1054-1064`），若 `metadata.get("llm_model_system_only")` 为真，向 `classify_model` 与 `resolve_llm_config` 传入 `ignore_custom_models=True`，强制走系统目录端点解析。
   - **拒绝消息零自定义模型**：跨人模型校验失败时，错误消息固定仅列出系统目录已配置模型 `sorted(n for group in get_configured_llm_models().values() for n in group)`，剥离任何人的自定义模型名称。
2. **清除覆盖与继承所有者默认值保留**：
   - 跨人或本人将 `llm_model` 清除为 `None` 时，移除 `metadata["llm_model_system_only"]`，保留运行时继承 `task_owner` 默认模型的行为，并照常执行 `check_claude_code_hosted(task_owner, None, ...)` 检查（其拒绝文案 `claude_code_off_message` 仅描述电脑与托管状态，见 `src/server/services/automations/lifecycle.py:317`）。
3. **跨人 `list` 与 `reasoning_effort` 错误同步隐藏私有模型名**：
   - 在 `schedule_dalton_automation_helper` 的 `list` 分支（`src/dalton/autonomy/schedule.py:771-791`）：当 `list_owner != effective_actor_id` 时，对每条 `llm_model` 非空的记录检查该模型是否为系统目录生效模型（位于 `get_configured_llm_models()` 且当所有者存在同名自定义覆盖时仅 `llm_model_system_only == True` 视为系统模型）；凡属所有者私有自定义模型，统一将返回字段脱敏为 `"llm_model": "所有者自定义模型"`。
   - 在 `_reconcile_reasoning_effort` 调用处（`src/dalton/autonomy/schedule.py:1357-1377,1426-1462,1557-1573`；底层 `src/server/services/automations/lifecycle.py:471-505`）：当 `effective_actor_id != task_owner` 且未改模型、仅改 `reasoning_effort` 时，若存量 `target_model` 为所有者私有自定义模型并触发 `AutomationRefusal(field="reasoning_effort")`，捕获后将其中的私有模型名统一替换为 `"所有者自定义模型"` 再返回或抛出。
4. **本人操作（`effective_actor_id == task_owner`）**：
   - 每次显式提交非空 `spec_llm_model` 时调用 `lifecycle.check_model(task_owner, spec_llm_model, pref_for_lock)`（`src/server/services/automations/lifecycle.py:337-354`，支持系统模型、本人 `custom_models` 与自定义服务商父级 `:345`）。

#### （4）数据库事务内完整复核内容（C6）

在 `schedule_dalton_automation_helper` 的写事务内（新建在 `create_automation` 前使用 `active_conn`，更新在 `auto_db.update_automation` 获取 `FOR UPDATE` 行锁后的 `_reconcile_locked_upsert` 回调中使用 `locked_conn`）统一执行以下四项复核：

1. **工作区存活与未迁移复核**：在 `locked_conn` 上重读 `ws_row = await get_workspace(locked_ws_id, conn=locked_conn)`，确认工作区存在且 `ws_row["status"] != "deleted"`（`Home` 校验保留行有效），更新时额外确认 `_ws_key(locked_row.get("workspace_id")) == _ws_key(existing.get("workspace_id"))`。
2. **操作者权限事务内复核**：在 `locked_conn` 上复核当前操作者权限：覆盖单元工作区重读 `get_active_unit_by_workspace(locked_ws_id, conn=locked_conn)`，确认单元仍处于活跃态、`locked_lead == task_owner` 且 `effective_actor_id` 仍为该单元主分析师或有效 `admin`（PM）；非覆盖单元同事工作区复核 `require_workspace_access(..., channel="web", need="write", conn=locked_conn)`；`Home` 全局任务复核 `PM` 身份。
3. **工作区实际绑定的电脑与快照一致**：在 `locked_conn` 上通过 `get_computer_for_workspace(locked_ws_id, conn=locked_conn)`（`Home` 用 `home_turn_computer(task_owner, conn=locked_conn)`）读取当前实际绑定的 `computer_id` 与电脑状态，核对 `current_computer_id == snapshot_computer_id`、电脑存活且工作区处于静止绑定态（零活跃 `workspace_move_fences`）；不一致时抛出 `AutomationRefusal("情况已变化，请重新发起", field="workspace_id")`。
4. **模型、Claude Code 运行位置与思考强度复核（含显式保存相同模型）**：
   - 只要 `model_in_spec` 为真且 `final_model` 非空，在事务内执行 `_check_managed_automation_model(task_owner, effective_actor_id, final_model, pref_for_lock)`；
   - **当 `model_in_spec` 为真（含显式保存与原记录相同的 `final_model`，或清除为 `None`）、或新建任务（含缺省 `None`）、或锁内模型/电脑相对初检发生变化时**，一律在 `locked_conn` 上调用 `await lifecycle.check_claude_code_hosted(task_owner, final_model, locked_ws_id, locked_conn, pref_for_lock)`，确保工作区迁往受限云端电脑后显式重存同一 Claude Code 模型同样被准确拦截；
   - 执行 `_reconcile_reasoning_effort` 重算思考强度，并将 `schedule.py:1374,1459,1570` 的错误码改为按 `exc.field` 分发（`"llm_model"` → `"invalid_llm_model"`，`"workspace_id"` → `"workspace_changed"`，其余 → `"invalid_reasoning_effort"`）；REST 路由 `src/dalton/app/router.py:1388-1391` 维持 `reasoning_effort` 返回 422、其余 `AutomationRefusal` 返回 409。

---

### 2.5 涉及的 `DALTON-HOOK` 与自有模块清单

全部上游文件改动统一复用已在 `scripts/dalton/check_hooks.sh:485` 登记白名单的钩子标识 **`DALTON-HOOK(cos-settings-tools)`**，通过 `./scripts/dalton/check_hooks.sh --strict --audit`（`scripts/dalton/pr_baseline.sh:187`）门禁校验：

| 钩子 ID / 归属 | 涉及文件 | 改动摘要 |
|---|---|---|
| `DALTON-HOOK(cos-settings-tools)` | `src/tools/secretary/approvals.py`、`src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py` | `StandingApprovalState.approval_snapshots` 快照持久化、自有电脑扣留且共用同一 `computer_id`、`manage_workspaces` 的 `archive` 与 `create(computer=...)`、`list` 多电脑输出 `computer_id`、`_folder_ready_result` 源头白名单脱敏日志。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/database/sql_fences.py`、`src/server/database/computer.py`、`src/server/services/workspace_manager.py`、`src/server/services/computer_manager/{_machines.py,_lifecycle.py}`、`src/server/dependencies/cli_approval.py`、`src/server/app/{workspaces.py,computers.py}` | `FENCE_TAKES_WORKSPACES` 排除 `config.archiving = true`、`takes_a_workspace_sql` 排除 `'stopping'`、单事务 `claim_computer_for_archive` 全量快照比对、`_settle_machine_stop` 清理 `archiving`、`_archive_machine` 与两个 HTTP 归档入口源头白名单脱敏及本机令牌自归档拦截。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/app/threads/messaging.py`、`src/server/handlers/checkpoint_handler.py` | `settle_resume_request` 对 `archive_workspace` 与 `create_workspace` 核对第一方 `session` 会话、发起人本人、待确认 `interrupt_id` 与快照绑定。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/services/automation_executor.py`、`src/ptc_agent/agent/prompts/templates/components/{chief_of_staff.md.j2,user_profile.md.j2}` | 跨人系统模型标记 `llm_model_system_only` 在触发执行时透传 `ignore_custom_models=True`；提示词补充选电脑新建、归档引导与 `llm_model` 参数说明。 |
| `DALTON-HOOK(cos-settings-tools)` | `web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{ProposalCard.tsx,SettledStep.tsx,CreateWorkspaceCard.tsx,SecretaryConfirmCard.tsx,SandboxSettingsPanel.tsx,sandbox/OverviewTab.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts,contentProjection.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`web/src/locales/{zh-CN,en-US}.json` | 对话确认卡片按 `tool_call_id` 关联四态结算与渲染块登记、自有电脑隐藏「始终允许」、网页端沙箱概览归档整机确认弹窗与移动端自适应布局。 |
| Dalton 自有目录（直接维护） | `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py`、`src/dalton/app/router.py`、`integrations/dalton-channel-gateway/src/orchestrator.ts` | `dalton_schedule_automation` 暴露 `llm_model`、锁前偏好加载、事务内四项复核与跨人私有模型隐藏；渠道网关统一发送中文通用确认提示与登录保护链接。 |

---

## 3. 权限与安全

### 3.1 同事访问、跨人操作与共享工作区的权限边界

设发消息人为 **A**，目标工作区所有者为 **B**：

| 能力项 | 本人操作（`A == B`） | 普通内部同事跨人访问（`A != B`，已启用 `colleague_sharing`） | 管理员（PM）跨人操作（`A != B`） | 外部成员（`external`） |
|---|---|---|---|---|
| **1. 归档工作区** `manage_workspaces(action="archive")` 与网页归档 | 允许归档本人名下已停止、常规工作区所在且提供商支持冷归档（Daytona）的电脑（须经本人第一方网页会话点击确认）。当前运行研究总监轮次的电脑引导前往网页停止后归档。 | **研究总监与个人电脑 HTTP 路由均拦截**：归档作用于整台电脑，研究总监统一返回 `"workspace not found"`，个人电脑 HTTP 路由返回 `403`（`src/server/services/shared_workspaces/access.py:287-288`）。 | **他人个人电脑一律拦截**（研究总监返回 `"workspace not found"`，个人电脑 HTTP 路由返回 `403`）。仅组织托管账户（`is_holder(owner)`）名下的组织共享工作区允许具备组织管理权的成员通过网页端执行整机确认归档（`src/server/services/shared_workspaces/access.py:289-291`）。 | 范围限于外部成员本人名下工作区。 |
| **2. 指定电脑新建工作区** `manage_workspaces(action="create", computer=...)` | 允许在本人名下任意存活且就绪的电脑上新建工作区；目标为自有电脑（`MACHINE_KINDS`）时逐次弹卡确认；覆盖单元自动登记对内部成员与管理员生效。 | 新工作区固定创建在调用者 **A 本人**名下，`computer` 仅在 `get_computer_roster(A)` 中解析；传入 B 的电脑标识返回目标缺失提示。 | 同左，仅在管理员本人名下电脑创建本人工作区。 | 允许在外部成员本人电脑新建常规工作区，自动跳过团队覆盖单元登记（`src/tools/secretary/tools.py:319`）。 |
| **3. 托管自动化的 `llm_model`** `dalton_schedule_automation` | 允许修改本人工作区受管任务及本人 `dalton-earnings-calendar` 的 `llm_model`，支持系统模型、本人自定义模型与自定义服务商。 | ① 绑定活跃覆盖单元的工作区：限定当前主分析师（`lead_analyst_user_id == A`）可改，其余同事返回 `not_unit_lead_analyst`（`src/dalton/autonomy/schedule.py:1051-1068`）；② 常规内部同事工作区：要求具备 `web` 通道 `write` 权限，且任务归属调用者 A 本人。 | 在内部成员活跃覆盖单元工作区允许代管主分析师任务（`task_owner = lead_analyst`，审计记录 `actor_user_id = PM`）；显式 `llm_model` 限定系统目录模型并按系统目录端点解析，清除为 `"default"`/`null` 继承主分析师默认模型；跨人 `list` 与报错将主分析师私有自定义模型统一展示为「所有者自定义模型」；外部成员工作区限定其本人维护（`src/dalton/autonomy/schedule.py:727-731`）。 | 范围限于本人常规工作区受管任务（`src/dalton/autonomy/schedule.py:799-811`）。 |

`manage_workspaces` 仅对研究总监角色挂载（`src/ptc_agent/agent/agent.py:1679`）；工作区内分析师（`role == "analyst"`）使用限定为只读与拷入当前工作区动作的 `colleague_workspace` 工具（`docs/dalton/features/colleague-agent-access.md:24-30`），且 `dalton_schedule_automation` 限定分析师仅操作当前所在工作区（`src/dalton/autonomy/schedule.py:635-663`）。

### 3.2 确认动作的真实性与第一方网页会话核对（C4）

现有 `check_turn_body`（`src/server/dependencies/principal.py:480-520`）在 `principal.unlimited`（涵盖 `session` 与 `service`，`:499-500`）及 `principal.computer_id is None`（涵盖 `pat` 与 `oauth`，`:513-514`）时提前返回，使渠道网关（使用 `X-Service-Token`，`integrations/dalton-channel-gateway/src/backend-client.ts:1124`）或持有 `agent:run` 权限的 PAT / OAuth 客户端可通过外层校验。为从后端根源保证工作区归档与指定电脑新建确认来自用户本人网页点击，在 `AdmittedTurn` 与 `settle_resume_request`（`src/server/app/threads/messaging.py:266-315,633-656`、`src/server/handlers/checkpoint_handler.py:275-303`）增加针对性核对：

1. **工具签名纯业务化**：`manage_workspaces` 参数表仅含业务字段，确认结果唯一来源于检查点中断恢复载荷。
2. **按待确认中断类型执行后端四重核对**：
   - 当 `request.hitl_response` 非空时，`settle_resume_request` 读取当前分支最新检查点 `newest` 中的挂起中断任务（`interrupt_id -> action_requests[0]["type"]`）与 `channel_values.get("approval_snapshots")`；
   - 一旦待恢复中断包含 `archive_workspace` 或 `create_workspace`，强制核验：
     1) **第一方网页会话身份**：要求 `principal.auth == "session"` 且非内部服务令牌代发请求（`principal.auth in ("service", "pat", "oauth", "cli")` 或携带 `X-Service-Token` 一律返回 `403 Forbidden`，错误码 `"web_session_required"`）；
     2) **发起人本人一致**：要求当前登录 `principal.user_id` 与发起该挂起轮次的用户本人完全一致（个人线程核对 `thread_meta["user_id"]`，共享/跨人线程核对 `latest_turn_sender(thread_id)`，`src/server/services/shared_workspaces/turns.py:275`）；
     3) **当前待确认项精确匹配**：提交的 `hitl_response` 键必须命中最新检查点当前挂起的 `interrupt_id`（过期分支或历史检查点一律返回 `409 Conflict`）；
     4) **服务端快照完整匹配**：核对 `approval_snapshots[tool_call_id]` 存在且其 `action`、`computer_id`、`workspace_id` / `expected_workspaces` 与该挂起中断载荷完全一致。
3. **其余中断保持既有恢复体验**：当挂起中断为 `ask_user_question` 或 `credit_pause` 时，保留渠道网关通过 `service` 身份代用户回复文字恢复执行的原有流程（`integrations/dalton-channel-gateway/src/orchestrator.ts:2011-2025`）。

### 3.3 源头日志与错误信息脱敏（C7、C5）

在异常产生的第一现场直接改为白名单输出与凭据擦除，避免敏感信息先落入日志或 HTTP 响应再做外层补救：

1. **`ComputerManager._archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1896-1905`）**：
   - 将失败恢复分支的 `logger.warning(..., exc_info=True)` 及外层归档异常日志改为去掉原始堆栈 `exc_info=True`，仅输出结构化白名单字段：`computer_id`、`workspace_id`、`exc_type=type(exc).__name__` 以及经 `single_line(sanitize_error_text(str(exc)))`（`src/server/utils/error_sanitization.py:32-40,56-62`）擦除凭据并转义换行后的单行摘要。
2. **`_folder_ready_result` 与研究总监工作区工具（`src/tools/secretary/tools.py:199-200,212-214,229-230,250-252,280-281,502,564,614`）**：
   - 将 `_folder_ready_result` 全部 5 处 `logger.warning(f"...: {e}")` 及 `workspaces_create` / `workspaces_update` / `workspaces_archive` 的异常日志统一改为记录白名单字段（`workspace_id`、`computer_id`、`stage`、`exc_type`）与 `single_line(sanitize_error_text(str(e)))`；返回给模型的 `ToolMessage` 仅输出固定业务错误码文案或 `sandbox_unreachable_detail(e)`（`src/server/utils/error_sanitization.py:75-98`）。
3. **两个 HTTP 归档入口（`src/server/app/workspaces.py:164-170`、`src/server/app/computers.py:267-273`）**：
   - 将 `_workspace_action_errors` 与 `_computer_action_errors` 在 `action == "archive"` 时捕获未知 `RuntimeError` 原样返回 `str(e)` 及 `logger.exception` 的逻辑收紧：领域拒绝异常（`ArchiveScopeChangedError`、`ArchiveUnsupportedError`、`ComputerNotStoppedError`、`ComputerBusyError`）映射为固定白名单文案（409 或 400），连接故障走 `sandbox_failure_http(e)`（`:100-117`），其余 `RuntimeError` 与 `Exception` 服务端仅按白名单字段 + `single_line(sanitize_error_text(str(e)))` 记日志，向客户端固定返回 `"Failed to archive workspace"` / `"Failed to archive computer"`。
4. **所有者自定义模型名隐藏**：跨人操作受管自动化时，写入拒绝提示、跨人 `list` 返回值与 `reasoning_effort` 报错全部隐藏 `task_owner` 的私有自定义模型名（见 §2.4），版本审计快照继续经 `sanitize_automation_snapshot`（`src/dalton/services/company_automations.py:256,360-377`）按白名单字段落库。

---

## 4. 能力与安装形态

严格执行新部署架构的三层职责切分：

1. **安装形态 `src.dalton.profile`（`DeploymentProfile.EVERFLOW` / `DeploymentProfile.CORE`）**：仅决定部署默认覆盖配置（如加载 `dalton.core.yaml` 与 `agent_config.core.yaml`）及各项能力的默认预设值。
2. **后端能力 `src.dalton.capabilities` 与运行时设置 `src.dalton.settings` / 沙箱配置**：决定服务端工具、路由与底层动作的实际启用状态。
3. **前端 `useCapabilities()`（`web/src/hooks/useCapabilities.ts:105-120`）**：决定可选界面入口与文案的呈现；运维脚本（`scripts/dalton/ops/`、`scripts/standalone/`）保持完全独立。

### 4.1 两种安装形态下的默认配置与实际可用性

| 工具 / 动作 | `DeploymentProfile.EVERFLOW` 默认表现 | `DeploymentProfile.CORE` 默认表现 | 实际启用判定规则（后端能力与设置） |
|---|---|---|---|
| `manage_workspaces(action="archive")` 与网页整机归档 | 工具默认挂载；仅在 **Daytona** 电脑（`src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`，`archives = True`）上可执行冷归档；E2B（`src/ptc_agent/core/sandbox/providers/e2b_runtime.py:448`）与自有 SSH 电脑在弹卡前明确告知冷归档不可用。 | 工具默认挂载；Core 默认沙箱提供商为 **Docker**（`agent_config.core.yaml:16`，`archives = False`），在弹卡前即明确告知当前电脑冷归档不可用（停止即可释放资源），网页端归档按钮同步按 `archivable` 隐藏；若运维显式接入 Daytona 则自动可用。 | 研究总监角色固定挂载 `CHIEF_OF_STAFF_TOOLS`（`src/ptc_agent/agent/agent.py:1679-1682`）；实际能否发起冷归档由目标电脑提供商运行时 `archives` 布尔属性决定。 |
| `manage_workspaces(action="create", computer=...)` | 默认提供 | 默认提供 | 两种形态均开放多电脑管理与指定电脑新建工作区；覆盖单元自动登记由 `settings.coverage_units.enabled` 控制（`src/dalton/dalton.yaml:36` 与 `src/dalton/dalton.core.yaml:25` 默认均为 `true`）。 |
| `dalton_schedule_automation(llm_model=...)` | 默认提供（`autonomy.enabled` 默认开启） | 默认提供（`autonomy.enabled` 默认开启） | 登记为 `capability=CORE_CAPABILITY` 且受 **`setting_gate="autonomy"`** 控制（`src/dalton/tools/__init__.py:239,242,351,354,436-437`）：当 `settings.autonomy.enabled == True` 时向研究总监与分析师挂载，若部署显式关闭 `autonomy.enabled` 则整体卸载。 |

### 4.2 涉及的周边能力开关

1. **`CapabilityId.COLLEAGUE_SHARING`（`colleague_sharing`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:65,228-233`）**：控制同事工作区共享访问与 PM 跨人代管覆盖单元任务；跨人系统目录模型限制与私有模型隐藏逻辑作为基础安全守卫常驻生效。
2. **`CapabilityId.NATIVE_OAUTH_SYNC`（`native_oauth_sync`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:64,220-226`）**：控制工具说明与自动化 `README`（`src/ptc_agent/agent/backends/automations.py:309-314`）是否展示内置 Antigravity `*-oauth` 模型后缀提示，保持 Core 形态纯公共模型呈现。
3. **`CapabilityId.CHANNEL_GATEWAY` 及 `CHANNEL_FEISHU` / `CHANNEL_DISCORD` / `CHANNEL_WEIXIN`（Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:56-59`）**：开启时由渠道网关发送中文通用确认提示与网页链接；前端相关渠道管理入口由 `useCapabilities()` 控制呈现。

---

## 5. 多端确认界面与渠道交互

### 5.1 桌面网页端呈现：按 `tool_call_id` 关联四态与面板归档确认

1. **卡片生命周期四态划分与 `tool_call_id` 精确关联（C9）**：
   - 现有 `decidePendingProposal`（`web/src/pages/ChatAgent/session/interrupts/useInterruptAnswers.ts:69-74`）在点击确认瞬间即将卡片标为 `'approved'`，且 `settleProposalFromResult`（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:191-198`）将 `success: false` 混入 `'rejected'`（显示为“已取消”）。本次统一为 `create_workspace` 与 `archive_workspace`（及秘书动作卡片）携带 `tool_call_id`，区分四种明确状态：
     - **`pending` / `submitting`（待执行）**：初始展示待确认操作按钮；用户点击「确认」后置为提交执行中（`busy=true`，按钮禁用并显示加载态），等待后端该 `tool_call_id` 对应的 `tool_call_result` 到达后再结算最终结果；
     - **`approved`（执行成功）**：`tool_call_result` 返回 JSON `success: true`，折叠行 `SettledStep` 显示勾选图标与「已归档电脑 `<computer_name>`（含 N 个工作区）」或「已在 `<computer_name>` 创建工作区：`<workspace_name>`」；
     - **`rejected`（用户取消）**：用户点击「取消」或 `tool_call_result` 为 `"User declined..."`，显示取消图标与「已取消归档」/「已取消创建工作区」；
     - **`failed`（执行失败，含快照变化）**：`tool_call_result` 返回 JSON `success: false`，独立渲染失败态图标与原因摘要——当 `code === "scope_changed"` 时固定显示 **「情况已变化，请重新发起」**，其他业务冲突显示对应白名单说明（与用户主动取消严格区分）。
   - 实时 SSE 流（`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts:1220-1228`）与刷新历史回放（`web/src/pages/ChatAgent/session/history/resumeSettlement.ts:213`、`web/src/pages/ChatAgent/session/interrupts/fromHistoryEvent.ts:175-202,271-305`）统一按 `event.tool_call_id === card.tool_call_id` 结算卡片状态，并在以下全部卡片枚举文件中同步注册 `'archive_workspace'`：
     - `web/src/types/chat.ts`（`ContentSegment`、`SecretaryActionProposalState`、`WorkspaceProposalState` 扩展 `status`、`tool_call_id`、`computer_id`、`computer_name`、`computer_is_own_machine`、`expected_workspaces`、`errorMessage`）；
     - `web/src/pages/ChatAgent/session/interrupts/buckets.ts:14-27,176-210`（`PROPOSAL_INTERRUPT_TYPES`、`PROPOSAL_DATA_KEY_MAP`、`settleProposalFromResult`）；
     - `web/src/pages/ChatAgent/components/messageList/buildRenderBlocks.ts:445-457`（构建 `archive_workspace` 渲染块）；
     - `web/src/pages/ChatAgent/components/messageList/contentProjection.ts:53-58`（加入 `RETAINED_TYPES` 保证轮次折叠后仍保留该结果卡片）；
     - `web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx:494-543`。
2. **对话内确认卡片细节**：
   - **归档工作区卡片（`web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx`）**：展示目标电脑名称及快照中同机全部常规工作区名称与当前状态，说明整机冷归档影响；仅提供「确认归档」与「取消」按钮。
   - **指定电脑新建工作区卡片（`web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx` 与 `WorkspaceCreatedSlip`）**：展示工作区名称、描述与「目标电脑：`<computer_name>`」；当 `computer_is_own_machine == true` 时隐藏「始终允许」按钮（仅展示「创建」与「取消」），确保自有电脑遵循逐次确认规则；常驻自动放行创建成功时，`web/src/pages/ChatAgent/utils/preapprovedCards.ts:81-85` 与 `WorkspaceCreatedSlip` 同步展示目标电脑名。
3. **沙箱面板概览页归档确认弹窗（`OverviewTab.tsx`、`SandboxSettingsPanel.tsx`）**：
   - 与研究总监卡片保持同一信息结构：展示电脑名称与同机工作区列表，确认时提交 `expected_computer_id` + `expected_workspaces` 快照，遇 `409` 快照变化时在弹窗内提示「情况已变化，请重新发起」并刷新列表。

### 5.2 手机网页端适配

针对 320px–430px 窄屏视口加固三处交互体验：
1. `web/src/pages/ChatAgent/components/ProposalCard.tsx:84` 操作按钮容器改为 `pt-3 flex flex-wrap items-center gap-2`，保证多按钮自然折行与 ≥ 36px 触控高度；
2. 电脑名与工作区名添加 `break-words min-w-0` 保持窄屏纵向排版整洁；
3. 同机工作区超过 5 个时默认展示前 5 个并提供「展开查看全部 N 个工作区」切换按钮。

### 5.3 飞书、Discord 与个人微信渠道交互：通用中文提示与清理闭环（C8）

为保护私人电脑名称与同机工作区清单在群聊或跨端推送中的隐私，渠道网关 `integrations/dalton-channel-gateway/src/orchestrator.ts` 对飞书、Discord、个人微信（**群聊与私聊完全一致**）统一执行以下规则：

1. **仅发送中文通用确认提示与受登录保护链接**：
   - 在 `formatInterruptPrompt`（`orchestrator.ts:6312-6319`）与 `handlePendingInterruptReply`（`orchestrator.ts:1989-2009`）中，当 `kind === "web_only_approval"` 时统一发送中文通用文案：`该操作需要在网页端确认后继续：${this.webBaseUrl}/chat/t/${threadId}`，**消息正文省略电脑名称、工作区名称及同机工作区清单**，完整影响范围仅在通过身份认证的本人网页端卡片中展示。
2. **文字回复仅重发通用提示**：
   - 用户在飞书、Discord 或微信会话中直接发送任何文字（如“确认”“同意”“取消”）时，网关命中 `handlePendingInterruptReply` 的 `web_only_approval` 分支，仅重发上述中文通用确认提示与网页链接，结合后端 §3.2 的 `session` 身份强校验实现前后端双保险。
3. **网页确认或取消后的待确认项清理闭环**：
   - 用户通过链接在手机网页或桌面网页完成点击「确认」或「取消」后，线程脱离 `interrupted` 状态；当用户随后在飞书、Discord 或微信发送下一条消息时，`orchestrator.ts:1966-1983` 调用 `GET /api/v1/threads/{id}/status` 检测到 `status !== "interrupted"`，立即将渠道会话的 `pending_interrupt` 置为 `null` 并进入正常对话轮次。

---

## 6. 分批计划与测试

### 6.1 数据库迁移评估与编号衔接约定（C2、C12）

- **本次迁移结论**：**零数据库迁移**。
  - 归档栅栏标记 `{"archiving": true}` 写入既有 `computers.config`（JSONB）列，过渡态复用既有 `COMPUTER_STATUSES` 中的 `'stopping'`（`src/server/database/computer.py:55-63`）；
  - 指定电脑新建工作区复用既有 `workspaces.computer_id` 列；
  - 确认快照持久化写入既有 LangGraph PostgreSQL 检查点状态通道（`PrivateStateAttr`）；
  - 托管自动化模型与跨人系统模型标记复用既有 `automations.llm_model` 列、`automations.metadata`（JSONB）列、`d042` 的 `reasoning_effort` 列及 `d035` 的 `audit_history` 列。
- **后续演进迁移头核实与衔接约定**：当前仓库唯一的 Dalton Alembic 迁移头节点为 **`d046`**（`src/dalton/migrations/versions/d046_scoped_data_map_cache.py:23-24`，其 `down_revision` 为 `"d048"`）。按全局排期，微信多账号先行占用 `d049`，研究桥拆分自 `d050` 起顺序占用；未来若衍生新表或新列需求，须在合入当下检出仓库唯一的 Dalton 迁移头节点单线顺延。

### 6.2 分批开发范围与发布步骤

拆分为 **3 个独立可合入、可随时现网发布的开发批次**，每批合入前均执行 `./scripts/dalton/check_hooks.sh --strict --audit` 与 `make lint`：

| 批次 | 涉及文件清单 | 交付范围与发布步骤 |
|---|---|---|
| **Batch 1：托管自动化 `llm_model`、事务内复核与跨人模型隔离** | `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py`、`src/dalton/app/router.py`、`src/server/services/automations/lifecycle.py`、`src/server/services/automation_executor.py`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2` | 1) `dalton_schedule_automation` 暴露 `llm_model`（省略保留、`"default"`/`"clear"`/`""` 清除，REST `null` 清除）；<br>2) `schedule.py` 在锁外预读 `user_models.get_model_preference` 与工作区电脑快照；<br>3) 事务内复核工作区存活、操作者权限、电脑绑定快照一致，显式保存相同模型亦复核 `check_claude_code_hosted`；<br>4) 跨人显式赋值按 `effective_actor_id` 限定系统目录模型并打标 `llm_model_system_only=True` 走 `ignore_custom_models=True` 解析，跨人 `list`、`llm_model` 与 `reasoning_effort` 错误统一隐藏私有模型名；<br>5) 发布后端进程即可生效。 |
| **Batch 2：确认快照持久化、第一方会话核验、指定电脑新建工作区与渠道通用提示** | `src/tools/secretary/{approvals.py,chief_of_staff.py,tools.py,_commands.py}`、`src/server/services/workspace_manager.py`、`src/server/app/threads/messaging.py`、`src/server/handlers/checkpoint_handler.py`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{ProposalCard.tsx,SettledStep.tsx,CreateWorkspaceCard.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`web/src/locales/{zh-CN,en-US}.json`、`integrations/dalton-channel-gateway/src/orchestrator.ts` | 1) 落地 `approval_snapshots[tool_call_id]` 快照持久化与恢复只读机制，`_folder_ready_result` 源头白名单脱敏；<br>2) `settle_resume_request` 对工作区确认强制核验第一方 `session`、发起人本人、待确认 `interrupt_id` 与快照；<br>3) `manage_workspaces(action="create", computer=...)` 接入 `pick_computer`、自有电脑扣留确认且共用同一 `computer_id`、`list` 多电脑返回 `computer_id`；<br>4) 前端卡片按 `tool_call_id` 四态结算，渠道网关切换为中文通用确认提示；<br>5) 发布步骤：构建前端、后端及 `integrations/dalton-channel-gateway`（`pnpm build` 并重启网关服务）同步上线。 |
| **Batch 3：数据库归档栅栏、研究总监与网页整机归档确认及源头脱敏** | `src/server/database/{sql_fences.py,computer.py}`、`src/server/services/workspace_manager.py`、`src/server/services/computer_manager/{_machines.py,_lifecycle.py}`、`src/server/dependencies/cli_approval.py`、`src/server/app/{workspaces.py,computers.py}`、`src/tools/secretary/{approvals.py,chief_of_staff.py,tools.py,_commands.py}`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{SecretaryConfirmCard.tsx,SandboxSettingsPanel.tsx,sandbox/OverviewTab.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts,contentProjection.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/locales/{zh-CN,en-US}.json`、`integrations/dalton-channel-gateway/src/orchestrator.ts` | 1) 落地 `FENCE_TAKES_WORKSPACES` 排除 `config.archiving = true`、`takes_a_workspace_sql` 排除 `'stopping'` 及单事务 `claim_computer_for_archive` 全量快照复核；<br>2) `_archive_machine` 与两个 HTTP 归档入口改为白名单日志与响应，补齐工作区归档路由自归档拦截与 `cli_approval` 兜底；<br>3) 上线 `manage_workspaces(action="archive")`（仅 Daytona 可归档，运行中电脑引导至网页，Docker/E2B/SSH 弹卡前明确告知不可用）与网页 `OverviewTab` 整机确认弹窗；<br>4) 登记 `archive_workspace` 至全部前端渲染与投影文件及渠道网关 `web_only_approval` 中文通用提示，构建发布前端、后端及 `integrations/dalton-channel-gateway`（`pnpm build` 并重启网关服务）。 |

### 6.3 验收方式与测试矩阵（C11）

遵循先实机端到端验证、再以回归测试锁死契约边界的原则（`AGENTS.md:109`）：

1. **实机端到端验证**：
   - **隔离 Daytona 电脑与 Docker / E2B 对照验证**：在隔离的已停止 Daytona 电脑（绑定 2 个以上工作区）上分别通过研究总监与网页 `OverviewTab` 发起归档，验证整机工作区清单展示、批准后冷归档成功及刷新回放一致；在 Core 默认 Docker 与 E2B 电脑上请求归档，验证弹卡前即明确告知该提供商不支持冷归档；在研究总监运行所在电脑请求归档，验证返回前往网页端操作的引导提示。
   - **指定电脑新建与多渠道验证**：在多电脑账号（含云端电脑与自有 SSH 电脑）验证按 `<activity>` 电脑名新建、自有电脑扣留常驻放行并隐藏「始终允许」按钮；在飞书、Discord、个人微信（群聊与私聊）触发归档与新建中断，验证仅收到中文通用提示与登录链接、文字回复仅重发提示、网页确认或取消后渠道下一条消息正常恢复对话。
   - **受管自动化 `llm_model` 验证**：分别以主分析师与管理员 PM 调用工具及 `PUT /api/v1/dalton/automations/{name}`，验证系统模型设置、清除覆盖、Claude Code 运行位置拦截及跨人私有模型隔离。

2. **确定时序并发测试与单元/前端回归测试**：
   - **确定时序双连接并发测试（PostgreSQL 实库连接）**：
     - 使用两个独立数据库连接 `conn_a` 与 `conn_b` 配合协程栅栏（`asyncio.Event`）：① 在快照采集后、`claim_computer_for_archive` 提交前，由 `conn_b` 插入同机新工作区、迁入/迁出工作区、改名或写入 `home_stand_in` 行，断言 `claim_computer_for_archive` 在事务内检测到快照不一致立即回滚中止，且 `runtime.archive()` 调用次数为 `0`；② 在 `conn_a` 持 `computers` 行 `FOR UPDATE` 或已提交 `config.archiving = true` 期间，由 `conn_b` 执行 `create_workspace_on_computer` 或 `bind_workspace_to_computer`，断言在行锁释放后被 `FENCE_TAKES_WORKSPACES` 挡住返回 `None` / `ComputerArchivingError`。
   - **快照只读与自动放行竞态单测**：
     - 断言 `interrupt()` 挂起期间用户切换主电脑（`set_primary_computer`）后点击批准，恢复执行仍严格沿用第一次写入 `approval_snapshots[tool_call_id]` 的快照 `computer_id`（目标电脑失效时返回 `code="scope_changed"`，「情况已变化，请重新发起」）；
     - 断言开启 `auto_approve_workspace_creation=True` 时，目标为自有电脑（`MACHINE_KINDS`）仍扣留弹卡，目标为云端主电脑时即使中间件检查后主电脑被并发切换为自有电脑，工具仍使用快照锁定的原云端 `computer_id`。
   - **后端确认身份拒绝单测**：
     - 针对挂起的 `archive_workspace` 与 `create_workspace` 中断，分别以 `auth="service"`（`X-Service-Token`）、`auth="pat"`、`auth="oauth"`、`auth="cli"` 以及非发起人 `session` 提交 `hitl_response`，断言后端全部返回 `403`；同时断言 `ask_user_question` 中断经 `auth="service"` 正常恢复。
   - **托管自动化锁前预读、事务复核与跨人隔离单测**：
     - 断言 `_user_automations_lock_for_write` 上下文内部对 **`user_models.get_model_preference`**（`src/server/services/automations/lifecycle.py:362`）的调用次数为 `0`；
     - 模拟事务前与行锁间工作区被删除、权限被撤回或工作区重绑定到受限云端电脑（含显式提交与原值相同的 Claude Code 模型），断言事务内复核全部拦截；
     - 断言 PM 跨人显式提交主分析师已存的私有自定义模型名直接被拒、提交与所有者自定义模型同名的系统目录模型打标 `llm_model_system_only=True` 并以 `ignore_custom_models=True` 解析、跨人 `list` 将私有模型显示为 `"所有者自定义模型"`、跨人更新 `reasoning_effort` 报错将私有模型名替换为 `"所有者自定义模型"` 且候选提示仅含系统目录模型。
   - **源头脱敏正则全匹配单测**：
     - 向 `_archive_machine` 恢复失败分支、`_folder_ready_result` 及两个 HTTP 归档路由注入完整匹配 `src/server/utils/error_sanitization.py:16-30` 正则长度要求的模拟敏感串（如 `Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9`、`sk-proj_1234567890abcdef`、`AIzaSyA1234567890abcdefXYZ`、`postgresql://dbuser:s3cr3tP4ss@10.0.0.8:5432/app`），断言日志记录与 HTTP / `ToolMessage` 响应仅含白名单字段与 `[REDACTED]`。
   - **前端组件与投影测试（`cd web && pnpm test && pnpm typecheck`）**：
     - 覆盖 `buildRenderBlocks`、`contentProjection.foldRole.test.ts`、`useInterruptAnswers`、`processStreamEvent` 与 `replayHistory`，验证 `create_workspace` 和 `archive_workspace` 按 `tool_call_id` 在实时流与刷新回放中准确呈现待执行、执行成功、用户取消、执行失败（含「情况已变化，请重新发起」）四态，以及 `OverviewTab` 整机归档确认弹窗。
   - **门禁检查**：执行 `./scripts/dalton/check_hooks.sh --strict --audit` 与 `make lint` 全绿通过。

---

## 7. 已定案事项汇总与待决定事项

### 7.1 已定案设计决策汇总

1. **通道与令牌**：保持 `computer_cli_tokens.enabled = false`，采用服务端工具 `manage_workspaces` / `dalton_schedule_automation` + 应用内持久化确认卡片。
2. **确认快照持久化（C1）**：弹卡前按 `tool_call_id` 将解析快照存入线程私有状态 `approval_snapshots`，恢复只读快照；常驻自动放行共用通过自有电脑检查的同一 `computer_id`；数据库事务复核不一致统一中止并显示「情况已变化，请重新发起」。
3. **归档数据库栅栏与双端一致（C2、C3）**：`computers` 行 `FOR UPDATE` 单事务清点全量工作区（新增、移出、迁走、改名、`Home` 替代工作区均算变化）并打 `config.archiving = true` 栅栏，`FENCE_TAKES_WORKSPACES` 阻断归档期新建与迁入，零数据库迁移；研究总监对当前运行所在电脑直接引导至网页操作，网页归档按钮同步改为带快照复核的整机确认。
4. **第一方会话核验（C4）**：后端对归档与新建工作区恢复请求强制校验第一方 `session`、发起人本人、当前待确认项及快照；渠道问答恢复维持原状。
5. **跨人模型与自动化复核（C5、C6）**：跨人显式赋值按 `effective_actor_id` 仅接受系统目录模型并强制按系统目录端点解析（`ignore_custom_models=True`），清除覆盖继承所有者默认值保留，跨人 `list` 与 `llm_model`/`reasoning_effort` 报错全程隐藏私有模型名；事务内复核工作区存活、操作者权限、电脑绑定快照一致及相同模型的运行位置；本次仅开放 `llm_model`。
6. **源头脱敏、渠道通用提示与四态卡片（C7、C8、C9、C10）**：在 `_archive_machine`、`_folder_ready_result` 及两个 HTTP 归档入口源头白名单脱敏；飞书、Discord、微信统一仅发中文通用提示与登录链接；卡片按 `tool_call_id` 区分待执行、执行成功、用户取消、执行失败四态；明确 Daytona 冷归档支持边界与 `autonomy` 设置门控。

### 7.2 仍需用户决定的事项

无。
