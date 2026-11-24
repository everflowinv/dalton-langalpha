# 研究总监（Chief of Staff）设置类工具设计方案

> **文档路径**：`docs/dalton/features/cos-settings-tools.md`  
> **修订轮次**：第 3 轮定案版  
> **覆盖范围**：① 通过研究总监与网页归档工作区（服务端快照持久化、迁出与归档同级行锁互斥、归档栅栏仅在提供商已结束时清除、网页与研究总监一致的整机确认、源头结构化日志与本地异常捕获）；② 在指定电脑上新建工作区（电脑名称/ID 解析、首次无主电脑时限定新建托管云端电脑、自有电脑逐次确认、实时与回放四态卡片）；③ 托管自动化的 `llm_model`（锁前预读偏好、事务内锁行至提交复核工作区/财报日历例外权限/电脑绑定与运行位置、跨人系统目录限制贯通轮次解析与订阅回退）；④ `.agy/cos_settings_audit.md` 遗漏项第 2–6 条完成度核对。

---

## 摘要

上游第 11 次同步引入了沙箱内命令行 `la`（`cli/src/commands/*.ts`）配合应用内全局弹窗确认（`src/server/dependencies/cli_approval.py:177-205`、`web/src/components/CliApproval/CliApprovalHost.tsx:32-94`）的工作区与线程操作路径。Dalton 侧为隔离跨人派活场景下的同机沙箱身份并保留对话流内持久化卡片与渠道通知，默认将电脑 `lac1.*` 令牌保持关闭态（`src/dalton/dalton.yaml:16-17`、`src/dalton/dalton.core.yaml:13-14`），挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`）。

结合三轮架构定案，本方案确立以下核心设计：

1. **确认快照持久化与按认证模式分层的确认核验**：弹卡前在 `StandingApprovalMiddleware.aafter_model`（`src/tools/secretary/approvals.py:159-201`）按 `tool_call_id` 将解析结果写入线程私有状态 `approval_snapshots`，卡片展示该快照，`interrupt()` 恢复后仅读取该快照；常驻自动放行与后续建库共用通过自有电脑检查的同一 `computer_id`；新用户无主电脑时，快照 `computer_id=None` 明确定义为新建托管云端主电脑（执行前若已出现主电脑则中止重来）。在 `local` 与 `supabase` 认证模式下，后端恢复入口强制核验第一方网页登录会话、发起人本人、当前 `interrupt_id` 及对应快照；在核心安装回环试用模式（`DALTON_AUTH_MODE=oss`）下，允许本机回环网页点击确认，同时拦截一切渠道与 API 客户端确认请求。
2. **归档与迁出同级行锁互斥，栅栏仅由提供商终态清除**：研究总监通过纯只读查询校验本人归属、已停止态与冷归档支持度（仅 Daytona 支持 `archives = True`，`src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`；Core 默认 Docker、E2B 与自有 SSH 电脑均在弹卡前明确告知不可用）；研究总监运行所在电脑引导用户前往网页操作，网页端归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`）同步升级为带快照复核的整机确认弹窗。数据库层统一迁出（`src/server/database/workspace_move.py:183-210`）与归档对源电脑行的 `FOR UPDATE` 加锁顺序，归档在锁定工作区行后再次核查活跃迁移（`staged`、`committed` 阶段即便心跳过期仍持续阻断归档），快照完全一致后原子写入 `status = 'stopping'` 与 `config.archiving = true`。恢复路径在提供商状态仍为 `archiving` 时仅结算机器状态而保留 `config.archiving` 栅栏，唯有提供商确认归档完成或明确失败后才清除栅栏，零数据库迁移落地。
3. **在指定电脑上新建工作区**：`manage_workspaces(action="create")` 增加可选参数 `computer`，复用 `pick_computer`（`src/server/services/computer_reach.py:120-152`）解析 `<activity>` 电脑名称、完整 UUID 或 8 位以上前缀；`manage_workspaces(action="list")` 在用户名下存活电脑达到 2 台及以上时同步输出 `computer_id`。目标为用户自有电脑（`kind in MACHINE_KINDS`）时扣留常驻自动放行并逐次确认，在实时流与刷新回放中按 `tool_call_id` 区分待执行、执行成功、用户取消与执行失败四态。
4. **托管自动化 `llm_model` 贯通轮次解析与源头结构化脱敏**：继续通过受 `autonomy` 设置门控（`src/dalton/tools/__init__.py:239,242,351,354,436-437`）的专用工具 `dalton_schedule_automation` 承载。在 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:934`）取锁前预读 `user_models.get_model_preference` 与工作区电脑快照；事务内按迁移一致顺序将电脑行与工作区行锁至提交（`Home` 在同一事务内锁定关联电脑与工作区行确定运行位置），复核工作区存活、授权条件（保留本人修改 `dalton-earnings-calendar` 例外，其余 `Home` 全局任务核验 PM 身份）及相同模型的运行位置；跨人显式赋值限定系统目录模型，保存时 `_reconcile_reasoning_effort` 共用同一份系统目录分类；执行行 `automation_executions`（Dalton 迁移 `d050`，升级时为已有执行行按最早一轮运行元数据回填、尚无运行的按 `automations` 当前配置回填，降级删列）在创建执行行的同一事务内从同一次读取的任务配置定格写入完整模型快照（`llm_model`、`reasoning_effort`、`llm_model_system_only`）且之后不再修改，该执行的全部派发与续跑路径均先关联执行再只用该三项快照（人工确认续跑忽略网页输入框与线程当前模型，无法关联执行时按普通所有者轮次处理），传至 `resolve_llm_config` 与订阅回退以忽略同名自定义模型；归档 HTTP 入口本地捕获沙箱连接异常，各异常点统一仅记录固定错误类别与结构化白名单字段；渠道仅发送中文通用提示与登录链接，并按原 `interrupt_id` 精确清理或替换待确认项。

---

## 1. 现状核对与设置审计评估

### 1.1 上游 `la` 机制与 Dalton 现行运行状态

- **上游 `la` + `cli_approval` 链路**：每个电脑沙箱在启动或重连时由 `cli_tokens.keep()`（`src/server/services/cli_tokens.py:121-174`）写入 `lac1.<computer_id>.<secret>` 令牌文件（`LANGALPHA_TOKEN_FILE`，`cli/README.md:28-33`）。研究总监在 `Bash` 中调用 `la` 访问带有 `require_scopes` 声明的接口（`src/server/dependencies/principal.py:360-460`）；涉及删除、新建工作区、移除电脑或跨电脑启动运行时，`cli_approval.gate`（`src/server/dependencies/cli_approval.py:177-205`）仅对携带 `computer_id` 的电脑令牌生效（网页 `session` 请求在 `:179` 直接放行），将审批请求写入 Redis（`src/server/services/cli_approvals.py:192-408`，有效期 30 分钟，结案后保留 10 分钟），并在批准后重新计算请求意图摘要比对一致性（`src/server/dependencies/cli_approval.py:200-204`）。
- **Dalton 现行令牌开关与工具挂载**：`src/dalton/dalton.yaml:16-17` 与 `src/dalton/dalton.core.yaml:13-14` 均将 `computer_cli_tokens.enabled` 设为 `false`（读取入口 `src/dalton/settings.py:622-627`）。由 `DALTON-HOOK(computer-cli-tokens-off)` 在三处静态收口：`cli_tokens.keep()` 直接返回 `False`（`src/server/services/cli_tokens.py:131-138`）、`CliTokenSweeper` 跳过巡检（`src/server/services/cli_token_sweep.py:57-62,103-110`）、`get_principal()` 拦截 `lac1.*` 凭证（`src/server/dependencies/principal.py:197-205`）。研究总监在主智能体构建阶段挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`），需用户确认的动作通过 `hitl_confirm`（`src/tools/secretary/_commands.py:42-65`）触发 LangGraph `interrupt()`，在对话流内渲染 `ProposalCard`（`web/src/pages/ChatAgent/components/ProposalCard.tsx:44-121`）。

### 1.2 三项待设计能力的三端现状对照

| 能力项 | 上游 `la` 命令行现状 | 网页端与 REST 接口现状 | Dalton 智能体工具现状 |
|---|---|---|---|
| **1. 归档工作区** | `la workspaces archive <ws>`（`cli/src/commands/workspaces.ts:147-156`）调用 `POST /api/v1/workspaces/{id}/archive`（`src/server/app/workspaces.py:945-974`）。**现存缺口**：① `cli/README.md:151-153` 将归档列为直通动作，路由缺少 `cli_approval` 门控；② 路由依赖 `WorkspaceWriteUserId`（`src/server/dependencies/principal.py:429,452`），且路径参数只有 `workspace_id`，`_computer_limits`（`src/server/dependencies/principal.py:343-348`）仅在路径含 `computer_id` 时拦截本机自归档；③ Redis 审批记录结案保留 10 分钟（`src/server/services/cli_approvals.py:69`），缺少对话流持久化回放。 | 沙箱面板概览页归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`、`web/src/pages/ChatAgent/components/SandboxSettingsPanel.tsx:96-114`）直接调用 `POST /api/v1/workspaces/{id}/archive`，缺少同机工作区影响确认与快照提交；后端 `_archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1872-1908`）仅使用单进程 `_observed_lock`（`src/server/services/computer_manager/_sessions.py:252-275`），而新建（`src/server/database/workspace.py:906-914`）、重绑定（`:533-545`）与迁出（`src/server/database/workspace_move.py:183-210`）走独立数据库事务且 `FENCE_TAKES_WORKSPACES`（`src/server/database/sql_fences.py:25-38`）缺少归档栅栏；故障恢复 `_reconcile_provider_stop`（`_lifecycle.py:637-644`）在提供商仍处于 `archiving` 时即把机器状态结算回 `stopped`；两个归档路由（`workspaces.py:143-152,164-170`、`src/server/app/computers.py:222-228,267-273`）会将部分沙箱异常上抛至记录原始异常文本的全局处理器 `_sandbox_failure_handler`（`src/server/app/setup.py:1644-1655`）。 | `manage_workspaces` 当前包含 `list`、`create`、`update`、`delete` 与 4 项跨区文件动作（`src/tools/secretary/chief_of_staff.py:98,121-166`），`archive` 动作待接入；前端卡片渲染链（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:14-27`、`web/src/pages/ChatAgent/components/messageList/buildRenderBlocks.ts:454`、`contentProjection.ts:53-58`、`SecretaryConfirmCard.tsx:5`）当前仅登记 `delete_workspace`、`stop_workspace`、`delete_thread`。 |
| **2. 指定电脑新建工作区** | `la workspaces create <name> [--description D] [--computer C]`（`cli/src/commands/workspaces.ts:113-146`）调用 `POST /api/v1/workspaces`。`cli_approval.workspace_create`（`src/server/dependencies/cli_approval.py:385-429`）解析主电脑或指定 `computer_id` 写入意图摘要，批准后锁定该电脑创建（`src/server/app/workspaces.py:278-281`），`CliApprovalDialog` 通过 `workspaceCreateOn` 显示目标电脑名（`web/src/lib/cliApprovals/copy.ts:129-134`）。**现存缺口**：① `--computer` 与 `WorkspaceCreate.computer_id`（`src/server/models/workspace.py:60-66`）仅接受 UUID，而 `<activity>` 输出电脑显示名（`src/tools/secretary/activity.py:261-274`）；② 常驻免批分支（`src/server/dependencies/cli_approval.py:399-400`）缺少同名/同代码去重与自有电脑扣留；③ 聊天内联条 `laWorkspaceCard` 仅展示工作区名称（`web/src/pages/ChatAgent/utils/preapprovedCards.ts:50-68`）。 | 新建弹窗通过 `useCreateWorkspace`（`web/src/pages/ChatAgent/hooks/useCreateWorkspace.ts:106-124`）传 `computerId` 至 `POST /api/v1/workspaces`（`src/server/app/workspaces.py:249-424`）→ `WorkspaceManager.create_workspace(..., computer_id=...)`（`src/server/services/workspace_manager.py:140-237`），其中 `computer_id=None` 分支调用 `ensure_primary_computer`（`src/server/services/computer_manager/_machines.py:213-271`），可能将用户已有的自有电脑提升为主电脑（`:236`）。 | `<activity>`（`src/tools/secretary/activity.py:238-274`）与 `manage_workspaces(action="list")`（`src/tools/secretary/tools.py:88-93`、`src/tools/secretary/utils.py:17-25,91-108`）在多电脑下仅展示电脑名称；`manage_workspaces(action="create")` 参数表固定落在主电脑（`src/tools/secretary/chief_of_staff.py:55-77`、`src/tools/secretary/tools.py:286-305,439-443`）；`CreateWorkspaceCard` 仅展示名称与描述（`web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx:6-14,67-86`）；`_folder_ready_result` 日志直接拼接异常对象（`src/tools/secretary/tools.py:199-200,212-214,229-230,250-252,280-281`）。 |
| **3. 托管自动化的 `llm_model`** | `la automations create/edit -m MODEL`（`cli/src/commands/automations.ts:55-100`）调用通用 `/api/v1/automations`，经 `cli_approval.automation_runs`（`src/server/dependencies/cli_approval.py:701-823`）预检 `check_model` 与 `check_claude_code_hosted`（`:758-764`）。**作用域边界**：通用接口限定为调用者本人常规工作区（`src/server/database/automation.py:595-602`）并将覆盖单元受管元数据设为只读保护（`src/server/services/automations/lifecycle.py:95-152`），主分析师代管任务、`Home` 全局任务及公司任务走专用服务层。 | 网页通用保存路径（`src/server/app/automations.py:157-231,538-700` → `src/server/services/automations/lifecycle.py:618-760,865-1025,1295-1303` 及 `src/dalton/services/company_agent_writes.py:267-382`）在取行锁前调用 `_safe_model_pref`（底层为 `user_models.get_model_preference`，`lifecycle.py:358-363,1295-1303`）加载 `pref_for_lock`；更新初检阶段（`prepare_automation_update`，`:872-895`）在新模型异于当前值时调用 `check_model`（含自定义服务商父级 `:345`），在模型或工作区变化时调用 `check_claude_code_hosted`（`:878-879`），新建阶段对缺省模型通过 `_claude_code_default_for` 检查默认 Claude Code 规则（`:296-335,663`）；行锁回调（`recheck_locked_automation_update`，`:933-1022`）仅在锁内行与初检行发生工作区或模型漂移且目标位置/模型变化时重查 `check_claude_code_hosted`（`:989-999`），并重算 `_reconcile_reasoning_effort`（`:1002-1021`）。Dalton 专属保存路由 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）复用 `schedule_dalton_automation_helper`。 | 个人任务可通过 `.agents/user/automations/*.json` 修改 `llm_model`（`src/ptc_agent/agent/backends/automations.py:148`），受管任务由 `src/server/services/automations/file.py:1027` 分流至专用工具。`dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1588`）参数表尚待加入 `llm_model`；底层 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:771-791,934,1260-1267,1350-1462,1555-1609`）在 `_user_automations_lock_for_write` 锁内才读取偏好（`:1267`），缺少模型校验、Claude Code 运行位置校验与锁行至提交的绑定复核，且跨人 `list`（`:784`）直接返回所有者私有 `llm_model` 原文。 |

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
3. **跨人修改受管自动化仅允许系统目录模型**（清除覆盖回退所有者默认值单独保留），并在保存分类、轮次模型解析、跨人列表查询与错误提示中全程隔离和隐藏所有者的私有自定义模型。
4. **`dalton_schedule_automation` 本次仅新增开放 `llm_model`**，`thread_strategy="new"` 与 `max_failures=3` 维持受管任务统一策略。

#### （2）确认快照持久化机制（恢复只读快照，C1）

本仓库工具从 LangGraph `interrupt()` 恢复时会从函数首行重新执行（见 `src/tools/secretary/approvals.py:6-9` 注释）。为确保恢复执行与卡片展示严格一致，与现有 `preapproved_calls` 采用同一机制持久化服务端快照：

1. **线程私有状态扩展（`src/tools/secretary/approvals.py:159-205`）**：
   - 在 `StandingApprovalState` 中新增按 `tool_call_id` 索引的私有状态字段：
     ```python
     class StandingApprovalState(AgentState):
         preapproved_calls: NotRequired[Annotated[list[str], PrivateStateAttr]]
         approval_snapshots: NotRequired[Annotated[dict[str, dict[str, Any]], PrivateStateAttr]]
     ```
   - `StandingApprovalMiddleware.aafter_model` 在模型提出 `manage_workspaces(action="create")` 或 `manage_workspaces(action="archive")` 调用后、进入工具节点**之前**运行一次，完成目标电脑与整机工作区集合解析，将结构化快照写入 `approval_snapshots[tool_call_id]` 落入检查点。
2. **常驻自动放行与快照绑定同一 `computer_id`**：
   - 对于 `manage_workspaces(action="create")`，`aafter_model` 先生成快照（确定唯一 `computer_id` 或首次建云电脑标记 `bootstrap_cloud_primary=True`，以及 `computer_is_own_machine`），基于同一份快照判定 `auto_approve_workspace_creation`（非重名、校验成功、电脑就绪且 `computer_is_own_machine == False`）。放行加入 `preapproved_calls` 后，工具执行直接读取该快照创建工作区，消除检查与执行两次解析之间的竞态。
3. **弹卡与恢复只读快照**：
   - 工具首次运行进入 `hitl_confirm` 时，直接以 `approval_snapshots[tool_call_id]` 构造卡片载荷（携带 `tool_call_id`）；
   - 用户在网页端点击确认、`interrupt()` 恢复重入工具函数时，工具**仅读取 `state["approval_snapshots"][tool_call_id]`**，跳过一切重新解析；
   - 执行阶段将快照传入数据库事务复核，发现任何状态或集合偏差即中止执行，返回 `error_command("情况已变化，请重新发起", tool_call_id, code="scope_changed")`，前端卡片按 `tool_call_id` 关联结算并展示「情况已变化，请重新发起」。
4. **自动化快照与锁行复核**：
   - `dalton_schedule_automation` 底层 `schedule_dalton_automation_helper` 在取锁前采集目标任务的工作区与实际绑定电脑快照 `(ws_id, snapshot_computer_id, target_model, pref_for_lock)`，并在写事务内锁住工作区与电脑行直至提交（详见 §2.4）。

```mermaid
flowchart TD
    M["模型输出工具调用 (tool_call_id)"] --> MW["StandingApprovalMiddleware.aafter_model<br/>解析电脑/整机工作区并持久化 approval_snapshots[tool_call_id]"]
    MW -->|"满足常驻自动放行 (云端电脑且唯一名称)"| EXEC_CREATE["只读快照 computer_id / bootstrap_cloud_primary<br/>数据库事务内复核并创建"]
    MW -->|"归档 或 需确认的新建 (含自有电脑)"| INT["hitl_confirm interrupt()<br/>卡片展示快照 + 渠道仅发中文通用提示与登录链接"]
    INT -->|"网页会话核验 + 发起人本人 + 匹配 interrupt_id/快照"| RESUME["恢复重入工具函数<br/>只读 approval_snapshots[tool_call_id]"]
    RESUME --> TX["PostgreSQL 单事务按序加锁复核快照<br/>(归档与迁出同级锁源电脑 + 打 config.archiving=true)"]
    TX -->|"完全一致"| DONE["执行动作并结算卡片为『执行成功』"]
    TX -->|"任意偏差"| ABORT["回滚事务，返回 code=scope_changed<br/>卡片显示『情况已变化，请重新发起』"]
```

---

### 2.2 能力一：通过研究总监与网页归档工作区

#### （1）弹卡前只读快照采集与运行中电脑引导（`src/tools/secretary/approvals.py`、`src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py`）

1. **工具入口与卡片登记**：
   - `manage_workspaces` 的 `action` 增加 `"archive"`，`_CARDS["manage_workspaces"]`（`src/tools/secretary/_commands.py:72`）登记 `"archive"`（每次逐次确认）。
2. **纯只读预检（避免绑定副作用）**：
   - `ComputerManager.resolve_binding()` 遇到旧版未绑定工作区时会隐式领养或创建电脑（`src/server/services/computer_manager/_sessions.py:73-89`），预检阶段改为调用纯只读查询 `get_workspace(workspace_id)`（`src/server/database/workspace.py:294`）与 `get_computer_for_workspace(workspace_id)`（`src/server/database/computer.py:215-248`）：
     - 校验工作区存在且归属当前用户本人（`str(ws.get("user_id")) == user_id`，其余情形统一记录快照错误 `"workspace not found"`）；
     - 校验目标为常规用户工作区（命中 `is_reserved_row(ws)` 记录 `"Home cannot be archived."`）；
     - **研究总监当前运行电脑引导（C3）**：当目标工作区绑定的电脑正是当前研究总监轮次正在运行的电脑（含 `Home` 主电脑运行态或 `Home` 替代工作区所在云端电脑运行态，处于 `running` 且持有当前活跃轮次）时，弹卡前直接返回引导回复，提示用户前往网页端沙箱面板（概览页）停止电脑后执行整机归档。若当前研究总监运行在云端替代工作区（`home_stand_in`），而用户请求归档的另一台已停止电脑上绑定有 `Home` 行，则将该 `Home` 行一并纳入整机快照正常弹卡确认；
     - **提供商冷归档能力与停止态预检（C10）**：读取 `provider_class(computer["kind"])` 对应的运行时类 `archives` 属性（基类默认 `False`，见 `src/ptc_agent/core/sandbox/runtime.py:549`；仅 **Daytona** 为 `True`，见 `src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`；Core 默认 **Docker** `agent_config.core.yaml:16`、**E2B** `src/ptc_agent/core/sandbox/providers/e2b_runtime.py:448-449` 及自有 SSH 电脑均为 `False`）。对 `archives == False` 的电脑在弹卡前明确告知当前电脑提供商未提供冷归档（停止电脑即可释放资源）；对 `computer["status"] != "stopped"`、缺少 `provider_ref`、存在活跃运行/后台命令或活跃迁移（含已过期但处于 `staged`/`committed` 的迁移，`src/server/database/sql_fences.py:90-114`）的电脑同样在弹卡前拦截说明。
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
   - 确认卡片向用户展示目标电脑名及所有常规存活工作区名称与各自状态；底层复核使用完整的 `expected_workspaces` 快照（含 `is_stand_in` 行）。

#### （2）数据库栅栏、迁出与归档同级行锁互斥及栅栏终态清除规则（C2、C13、C14）

沿用退役路径的数据库栅栏设计（`src/server/database/sql_fences.py:25-49`、`src/server/database/computer.py:484-499`），将新建、重绑定、迁入与迁出全部纳入统一数据库行锁秩序：

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
   - 在用于工作区迁入目标与主电脑候选筛选的 `takes_a_workspace_sql`（`src/server/database/sql_fences.py:41-49`）中同步追加 `AND {c}status <> 'stopping'`；`FENCE_TAKES_WORKSPACES` 保留普通停止过渡态（`stopping` 且无 `archiving`）主电脑可挂载新工作区的既有语义（对齐 `WorkspaceManager.create_workspace` 在 `src/server/services/workspace_manager.py:181-212` 的行为），同时在归档全周期凭借 `{"archiving": true}` 挡住 `create_workspace_on_computer`（`src/server/database/workspace.py:911`）、`bind_workspace_to_computer`（`:538`）、`claim_workspace_move`（`src/server/database/workspace_move.py:74`）及 `home_stand_in` 落位（`src/server/database/home_stand_in.py:86,100,216`）。
   - 在 `WorkspaceManager._chosen_computer`（`src/server/services/workspace_manager.py:234`）与 `create_workspace` 主电脑分支检查 `(computer.get("config") or {}).get("archiving")`，归档期间新建工作区返回清晰的 `ComputerArchivingError` 提示。

2. **迁出事务与归档事务统一源电脑加锁规则（C13，`src/server/database/workspace_move.py:183-260`）**：
   - 现有 `claim_workspace_move` 在 `:186-197` 仅锁定目标电脑 `target`（与可选 `removing`），随后才锁 `workspaces` 行（`:203-209`），使迁出记录可在归档检查迁移与锁定工作区之间插入提交。
   - **改造方案**：在 `claim_workspace_move` 进入事务加锁前读取待迁工作区当前所在的 `source_computer_id`，在 `:186-197` 的首条 `computers` 加锁语句中按 `ORDER BY computer_id` 顺序同时锁定**源电脑与目标电脑**（源电脑取 `FOR UPDATE` 并校验 `NOT COALESCE(config, '{}'::jsonb) @> '{"archiving": true}'::jsonb`，目标电脑维持 `FOR SHARE` / `FOR UPDATE`），随后锁定 `workspaces` 行（`:203-209`）并核验 `workspace["computer_id"] == source_computer_id`（若并发漂移则按既有 `:227-230` 返回 `MoveRefused("move_in_progress")`）。由此，迁出与归档遵循完全相同的全局加锁拓扑（`computers ORDER BY computer_id` → `workspaces ORDER BY workspace_id`），天然无死锁且严格串行互斥。

3. **单事务加锁清点、二次迁移复查与打标 `claim_computer_for_archive`（C2、C13，`src/server/database/computer.py`）**：
   - 在同一个 PostgreSQL 事务（`async with get_db_connection() as conn, conn.transaction():`）内按固定锁序执行：
     1) **锁定电脑行**：`SELECT {COMPUTER_COLS} FROM computers WHERE computer_id = %(computer_id)s AND status <> 'deleted' FOR UPDATE`，确认 `status == 'stopped'`、`provider_ref IS NOT NULL`，且 `retire_when_empty`、`archiving` 与进行中的 `spec_change` 均为空置态；
     2) **锁定同机全部存活工作区行并精确比对快照**：执行 `SELECT workspace_id, name, dir_name, status, COALESCE(config, '{}'::jsonb) ? 'home_stand_in' AS is_stand_in FROM workspaces WHERE computer_id = %(computer_id)s AND status <> 'deleted' ORDER BY workspace_id FOR UPDATE`，将结果序列与确认快照 `expected_workspaces` 做全量相等比对（目标工作区解绑或迁走、同机新增工作区、已有工作区删除移出、工作区改名或目录名变化、工作区状态变化、`Home` 替代工作区增减变化，**一律视为变化**）；
     3) **锁定工作区行后复查活跃工作与迁移记录**：在已持有 `computers` 与全部同机 `workspaces` `FOR UPDATE` 行锁的状态下，查询该电脑零活跃运行（`computer_run_in_progress`）、零运行中命令（`computer_command_running`），并复查 `workspace_moves` 表中涉及 `source_computer_id = %(computer_id)s OR target_computer_id = %(computer_id)s` 的记录——凡存在 `phase IN ('staged', 'committed')`（`MOVE_SET_ASIDE_PHASES`，`src/server/database/sql_fences.py:92`，此时目录已移置，**即便心跳过期 `move_stale()` 为真也持续阻止归档直至迁移恢复完成**），或存在 `phase IN ('preparing', 'backing_up') AND NOT move_stale()` 的迁移，一律拒绝归档；
     4) **提交或中止**：出现任何快照不一致或活跃迁移时抛出 `ArchiveScopeChangedError` / `ComputerBusyError` 回滚事务（电脑行保持原状，跳过沙箱提供商调用）；全部通过时在同一事务内执行 `update_computer_status(computer_id, ComputerStatus.STOPPING, expected=ComputerStatus.STOPPED, merge_config={"archiving": True}, conn=conn)`（`src/server/database/computer.py:1039-1095`）提交标记。

4. **归档栅栏清除规则：仅由「提供商已结束」状态清除（C14，`src/server/services/computer_manager/_machines.py:628-643`、`src/server/services/computer_manager/_lifecycle.py:516-550,602-655,1889-1906`）**：
   - Daytona 冷归档可在后台持续较长时间（`_lifecycle.py:641-642`），而停机心跳 Redis 键有效期为 20 秒、Redis 读异常超过 180 秒（`STOP_HEARTBEAT_UNAVAILABLE_RECOVERY_S`，`:523-535`）也会触发 `_reconcile_provider_stop`。若恢复路径在提供商仍为 `archiving` 时清除栅栏，将在冷归档进行中放行新建与迁入。
   - 为此将 `_settle_machine_stop` 签名扩展为 `_settle_machine_stop(computer_id, status, *, clear_archiving: bool = False)`（默认 `clear_archiving=False`，仅结算机器 `status` 而保留 `config.archiving`），并新增 `clear_computer_archiving(computer_id)` 供后续终态清理。全部清除点与对应提供商状态严格规定如下：

   | 路径 / 触发点 | 提供商实际返回状态（`runtime.get_state()` / 调用结果） | 机器 `status` 结算 | `config.archiving` 栅栏动作 |
   |---|---|---|---|
   | 1. `_archive_machine` 正常返回（`_lifecycle.py:1894-1895`） | `await runtime.archive()` 正常完成（归档已结束） | `'stopped'` | **清除**（`clear_archiving=True` → `drop_config_key="archiving"`） |
   | 2. `_reconcile_provider_stop`（含异常恢复 `:1898`、心跳超时 `:516-536`、Redis 不可读超 180s `:523-536`） | **`"archiving"`（提供商仍在归档中）** | 结算为 `'stopped'`（沿用现有状态机，`:643-644`） | **保留 `config.archiving = true`**（`clear_archiving=False`，持续阻断新建与迁入） |
   | 3. `_reconcile_provider_stop` 探明提供商归档已结束或明确失败 | `"archived"`、`"stopped"`、`"error"`、`SandboxGoneError`（`:624-630`）或无 `sandbox_id`（`:610-614`） | 结算为 `'stopped'` | **清除**（`clear_archiving=True`） |
   | 4. `_reconcile_provider_stop` 探明沙箱仍在运行 | `"running"`（归档未启动或已在远端恢复运行，`:645-649`） | 结算为 `'running'` | **清除**（`clear_archiving=True`） |
   | 5. `_reconcile_provider_stop` 查询提供商状态自身超时或瞬态网络失败 | 状态未知（抛出异常，未进入结算分支） | 保持 `'stopping'` | **保留 `config.archiving = true`** |
   | 6. 机器已处于 `'stopped'` 且携有残留 `config.archiving = true`，后续触发启动（`start_computer`）或新建/迁入前对账探测（`reconcile_archiving_computer`） | 若 `get_state()` 返回 `"archived"` / `"stopped"` / `"error"` / `SandboxGoneError`，或 `start_computer` 完成远端唤醒绑定；若仍返回 `"archiving"` 则拒绝新建/迁入 | `'stopped'`（探测）或 `'running'`（唤醒后） | 确认非 `"archiving"` 终态时**清除** `config.archiving`；仍为 `"archiving"` 时**保留**并返回 `ComputerArchivingError` |

#### （3）网页端归档按钮与 HTTP 归档路由一致性对齐（C3、`OverviewTab.tsx`、`SandboxSettingsPanel.tsx`、`workspaces.py`、`computers.py`）

1. **网页端整机归档确认弹窗（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`、`web/src/pages/ChatAgent/components/SandboxSettingsPanel.tsx:96-114`）**：
   - 仅当目标电脑提供商支持冷归档（`archivable === true`）且 `state === 'stopped'` 时展示归档按钮。
   - 点击归档按钮时，先拉取该电脑当前同机存活工作区列表，弹出整机归档确认对话框（列出目标电脑名称与同机全部工作区名称及状态）；用户点击确认后，将快照 `expected_computer_id` 与 `expected_workspaces` 作为请求体提交至归档接口。遇后端返回 `409`（`code="scope_changed"`，「情况已变化，请重新发起」），弹窗自动刷新最新同机工作区列表供用户重新核对。
2. **HTTP 归档路由门控与权限边界明确（`src/server/app/workspaces.py:945-974`、`src/server/app/computers.py:1107-1118`）**：
   - **请求载荷与快照复核**：两个路由均接收快照体（网页端与研究总监必传；外部直调请求也在路由内先行只读采集当前快照传入 `claim_computer_for_archive`，确保全部归档请求均走数据库栅栏）。
   - **身份依赖与本机令牌自归档拦截**：`POST /api/v1/workspaces/{workspace_id}/archive` 使用 `WorkspaceWritePrincipal`（`src/server/dependencies/principal.py:429,440`）。因路径参数只有 `workspace_id`，路由内先经只读 `get_computer_for_workspace(workspace_id)` 解析 `computer_id`，显式校验 `principal.computer_id == computer_id` 时抛出 `forbidden("A computer cannot stop, archive or remove itself")` 拦截本机自归档，并在电脑令牌开启的场景下与 `POST /api/v1/computers/{computer_id}/archive` 共用 `cli_approval.computer_archive` 整机审批构建器。
   - **HTTP 管理权限与研究总监本人权限的区分**：研究总监 `manage_workspaces(action="archive")` 限定 `workspace["user_id"] == user_id` 本人工作区；HTTP 路由 `workspace_access(workspace, user_id, manage=True)`（`src/server/services/shared_workspaces/access.py:272-291`）对个人电脑同样限定所有者本人（非所有者在 `:287-288` 返回 403），仅在组织托管账户（`is_holder(owner)`）名下的共享工作区允许组织管理员通过网页端归档该共享电脑。

---

### 2.3 能力二：在指定电脑上新建工作区

1. **工具参数与多电脑列表消歧（`src/tools/secretary/chief_of_staff.py:55-133`、`src/tools/secretary/tools.py:65-95`）**：
   - `manage_workspaces` 新增可选参数 `computer: str | None = None`，支持传入 `<activity>` 或 `manage_workspaces(action="list")` 列出的电脑显示名、完整 UUID 或 8 位以上唯一前缀；省略时默认使用当前主电脑。
   - `ComputerPlacement.from_rows(rows)`（`src/tools/secretary/utils.py:17-25,91-108`）通过 `computer_label(row["name"], len(rows))` 控制：仅当用户名下存活电脑数量 `len(rows) >= 2` 时 `placement.computers` 非空并输出 `row["computer"]`。`workspaces_list` 在同一条件 `if placement.computers and ws.get("computer_id"):`（`src/tools/secretary/tools.py:90-91`）下同步写入 `row["computer_id"] = str(ws["computer_id"])`。
2. **快照生成、自有电脑逐次确认与首次无主电脑初始化规则（C1、C15，`src/tools/secretary/approvals.py:88-205`、`src/tools/secretary/tools.py:286-504`、`src/server/services/workspace_manager.py:175-221`、`src/server/services/computer_manager/_machines.py:213-271`）**：
   - 在 `StandingApprovalMiddleware.aafter_model` 中为 `manage_workspaces(action="create")` 构建快照 `approval_snapshots[tool_call_id]`：
     - **显式传入 `computer` 时**：读取 `roster = await get_computer_roster(user_id)`，调用 `pick_computer(roster, computer)`（`src/server/services/computer_reach.py:120-152`）解析唯一电脑；遇到重名或缺失时将 `ComputerUnreachable` 提示记入 `snapshot["precheck_error"]`（工具执行时直接返回 `error_command`）；
     - **省略 `computer` 且已有主电脑时**：调用 `await get_primary_computer(user_id)`（`src/server/database/computer.py:387-403`）记录当前主电脑 `computer_id` 与显示名；
     - **省略 `computer` 且当前无主电脑时（首次初始化规则，C15）**：现有 `ensure_primary_computer`（`_machines.py:233-238`）会先尝试 `promote_primary_computer(user_id)` 将用户名下已有的自有电脑提升为主电脑。为避免确认后静默落到自有电脑或新出现的主电脑上，快照阶段先检查用户名下现有电脑（`get_computers_for_user`）与部署建机策略（`builds_computers(config.sandbox)` 及 `user.computer_choice != "own_machine"`，`:239-245`）：
       1) 当部署支持创建托管云端电脑且用户未强制限定仅用自有机器时，快照记录 **`computer_id = None`、`bootstrap_cloud_primary = True`、`computer_name = "新建托管云端电脑"`、`computer_is_own_machine = False`**，明确表示「新建一台托管云端主电脑并在其上创建工作区」（即使用户名下存在未设为主电脑的自有机器，执行时也绝不将其提升为主电脑）；
       2) 若部署不创建托管云端电脑或用户设置了 `computer_choice == "own_machine"`，则在弹卡前返回明确提示：用户名下已有自有电脑时提示指定该电脑名称重新发起（从而走自有电脑确认卡片），尚无任何电脑时返回 `NeedsComputer` 连接引导；
     - 对已有目标电脑校验其处于可用就绪态（排除 `error`、`retire_when_empty`、`archiving` 及首次构建中的 `creating`/`starting` 态，对齐 `src/server/services/workspace_manager.py:186-190,231-236`），记录 `computer_is_own_machine = (computer["kind"] in MACHINE_KINDS)` 与 `duplicate_workspace(user_id, name)` 结果；
     - 当 `computer_is_own_machine` 为真、存在同名/同代码工作区、存在 `precheck_error` 或去重读取失败时，一律扣留常驻自动放行并进入 `hitl_confirm("create_workspace", payload)`；满足常驻自动放行时直接按快照执行。
3. **恢复执行与事务内严格按快照落地（C1、C15）**：
   - **快照 `computer_id` 非空时**：传入 `WorkspaceManager._chosen_computer` 与 `create_workspace_on_computer`（`src/server/database/workspace.py:904-938`），在事务内 `FOR SHARE` 锁住该 `computer_id` 行，核验其存活、归属 `user_id`、满足 `FENCE_TAKES_WORKSPACES` 且 `status <> 'error'`；出现任何偏差立即中止并返回 `error_command("情况已变化，请重新发起", tool_call_id, code="scope_changed")`。
   - **快照 `computer_id is None`（`bootstrap_cloud_primary == True`）时**：在 `computer_capacity_lock(user_id)`（`src/server/database/computer.py:89-104`）保护下，先查询 `await get_primary_computer(user_id, conn=conn)`——**若确认后到执行前用户名下已出现任何主电脑，立即中止创建并返回 `error_command("情况已变化，请重新发起", tool_call_id, code="scope_changed")`**；确认仍无主电脑时，直接调用 `create_computer_for_user(user_id, default_name=name, is_primary=True)` 创建托管云端主电脑（**跳过 `promote_primary_computer`，绝不提升任何已有的自有电脑**；若并发竞争导致新插入行 `is_primary` 为假，按 `_machines.py:260-265` 将该竞争行标记 `deleted` 并返回 `code="scope_changed"` 提示重新发起），随后在该新建托管云端电脑上创建工作区。

---

### 2.4 能力三：托管自动化的 `llm_model`

#### （1）参数语义统一（工具入口与 REST 入口）

- **工具入口 `dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1588`）**：
  - 新增参数 `llm_model: str | None = None`；
  - `llm_model is None`（省略或传 `null`）：新建时继承任务所有者默认模型（`llm_model = None`），更新时**保留任务已有 `llm_model`**（在传入 helper 的 `spec` 中省略 `"llm_model"` 键）；
  - `llm_model.strip().lower() in ("default", "clear", "")`：在 `spec` 中写入 `spec["llm_model"] = None`，**清除任务级模型覆盖**并回退继承任务所有者默认模型；
  - 其他有效模型字符串：写入 `spec["llm_model"] = llm_model.strip()`。
- **REST 入口 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）**：
  - 沿用 `:1346-1350` 的字段存在性识别：请求体省略 `"llm_model"` 时保留原值；显式传入 `"llm_model": null`（或 `"default"` / `""`）时写入 `spec["llm_model"] = None` 清除覆盖。

#### （2）取锁前预读偏好与工作区电脑快照（`src/dalton/autonomy/schedule.py:934,1265-1267`）

- 在进入 `_user_automations_lock_for_write`（`src/dalton/autonomy/schedule.py:934`）之前：
  1. 当 `resolved_action == "upsert"` 时，先在锁外调用 `pref_for_lock = await _safe_model_pref(task_owner, None)`（底层调用 `user_models.get_model_preference(task_owner)`，`src/server/services/automations/lifecycle.py:358-363`），消除锁内 `:1267` 的偏好读取；
  2. 同时在锁外读取目标工作区快照 `(ws_id, snapshot_computer_id)`：常规工作区经 `get_computer_for_workspace(ws_id)`（`src/server/database/computer.py:215-248`）记录当前绑定的 `computer_id`，`Home` 工作区经 `home_turn_computer(task_owner)`（`src/server/services/home_placement.py:46-60`）记录当前实际运行电脑标识。

#### （3）跨人系统目录限制贯通保存分类与轮次解析（C5、C17）

以服务端计算的 `effective_actor_id`（`src/dalton/autonomy/schedule.py:1094-1104`）与 `task_owner` 区分本人与跨人操作：

1. **保存阶段校验与 `_reconcile_reasoning_effort` 共用同一份系统目录分类（C5、C17）**：
   - 当 `effective_actor_id != task_owner` 且 `model_in_spec` 为真、`spec_llm_model is not None`（**含显式提交与已存值相同的模型名**）时：仅对照 `get_configured_llm_models()` 与 `classified = await user_models.classify_model(task_owner, spec_llm_model, ignore_custom_models=True)`（`src/server/services/llm/user_models.py:264,281`）校验，跳过 `lifecycle.check_model(task_owner, ...)` 及 `get_custom_provider_config`；未知或非系统目录模型抛出仅含系统目录模型列表的 `AutomationRefusal(field="llm_model")`。
   - 校验通过后，在任务 `metadata` 中写入受保护键 `"llm_model_system_only": True`（本人后续显式设置非空模型或任何人清除为 `None` 时移除该键）；
   - **保存时思考强度使用同一份分类**：在锁前初检与锁内 `_reconcile_locked_upsert` 调用 `_reconcile_reasoning_effort`（`src/server/services/automations/lifecycle.py:450-521`）时，凡目标任务带有 `llm_model_system_only == True`（含本次跨人显式设置，或存量带有该标记且本次仅修改 `reasoning_effort`），一律传入上述 `ignore_custom_models=True` 得到的 `classified=(ModelSource.SYSTEM, system_entry)`，确保保存校验与运行时按同一系统模型阶梯处理。
2. **执行级不可变模型配置快照贯通首轮、全部续跑路径与换模型交接（C17）**：
   - 模型配置快照属于自动化执行行 `automation_executions`（Dalton 迁移 `d050`，新增 `llm_model`、`reasoning_effort` 与 `llm_model_system_only BOOLEAN NOT NULL DEFAULT FALSE` 三列），在全部 5 处创建执行行的数据库事务内（`claim_due_automations`、`claim_price_firing`、`claim_dalton_price_move_firing`、`create_execution_for_live_parent`、`create_execution`）由**同一次读取**的 `automations` 记录一次性写定，**之后不再修改**（父任务不存在插入零行时 `create_execution` 显式报错）。两条价格触发路径均从数据库当前行定格快照，派发与等待线程空闲后的重派均使用执行行快照而非缓存或重读的任务配置；修改任务配置仅影响修改后新建的执行。
   - **首轮与全部续跑路径统一读执行行快照**：首轮及等待线程空闲后的派发由 `AutomationExecutor` 从 `begin_execution_if_not_deleted` 返回的执行行读取三项快照写入 `ChatRequest`；后续全部续跑路径（人工确认续跑、唤醒、命令/子代理/任务汇报、服务端重试、部署后恢复、`running_thread` 及网页重试自动化运行）均在 `automation_thread_model.py` 先关联所属执行再直接使用执行行三项快照（人工确认续跑忽略网页输入框提交的模型/思考强度与线程当前保存的模型；服务端 `/retry` 若失败运行未落执行编号仍回查原请求唤醒通知的 `created_by_run_id`；`running_thread` 命中时不论标记真假均写入 `automation_source_execution_id`；部署后恢复携带被中断运行的 `restart_from_run_id`，普通用户轮次的恢复不经 `running_thread` 借用其他执行）。
   - **所有者切模型与交接 / 手动压缩**：所有者切换线程模型仅影响所有者自己的后续轮次，不影响在途自动化执行；换模型交接（`crud.py`、`thread_model.py`、`thread_model_handoff.py`）与手动压缩（`thread_maintenance.py`）均从**同一个运行**的 `run_metadata`（及其关联的执行行快照）原子读取旧模型名与 `llm_model_system_only`（网页入口直传 `source_system_only`，延迟交接将 `(model, system_only)` 持久化在 `candidate_sources` 中），避免跨运行混用。
3. **清除覆盖、跨人 `list` 与 `reasoning_effort` 错误隐藏（C5）**：
   - 将 `llm_model` 清除为 `None` 时移除 `metadata["llm_model_system_only"]`，保留继承所有者默认模型的行为；
   - 跨人 `list`（`src/dalton/autonomy/schedule.py:771-791`）：当 `list_owner != effective_actor_id` 时，凡任务 `llm_model` 非空且不属于系统目录生效模型（不在 `get_configured_llm_models()` 中，或所有者存在同名自定义覆盖且 `llm_model_system_only` 未置真），统一返回 `"llm_model": "所有者自定义模型"`；
   - 跨人仅修改 `reasoning_effort` 遇到存量私有模型抛出 `AutomationRefusal(field="reasoning_effort")` 时，将错误消息中的私有模型名统一替换为 `"所有者自定义模型"`。

#### （4）事务内锁行至提交与财报日历授权例外复核（C6、C18、C19）

普通 `get_workspace`（`src/server/database/workspace.py:294`）与 `get_computer_for_workspace`（`src/server/database/computer.py:215`）属于无锁快照查询，并发迁移提交（`commit_workspace_move`，`src/server/database/workspace_move.py:715-768`）仍可在复核通过后、自动化事务提交前改变工作区电脑绑定。因此在 `schedule_dalton_automation_helper` 的写事务内（新建在 `create_automation` 前使用 `active_conn`，更新在 `auto_db.update_automation` 获取 `automations` 行锁后的 `_reconcile_locked_upsert` 中使用 `locked_conn`）**按与工作区迁移完全一致的表锁顺序锁行直至事务提交**：

1. **锁到提交的记录与加锁顺序（C18，`computers ORDER BY computer_id` → `workspaces ORDER BY workspace_id`）**：
   - **常规工作区（非 `Home`）**：
     1) 先按快照 `snapshot_computer_id`（若快照为空则先读当前 `computer_id`）锁定电脑行：`SELECT {COMPUTER_COLS} FROM computers WHERE computer_id = %(computer_id)s AND status <> 'deleted' FOR SHARE`（阻断并发电脑删除、退役与归档的 `FOR UPDATE` 直至本事务提交）；
     2) 再锁定工作区行：`SELECT workspace_id, user_id, computer_id, status FROM workspaces WHERE workspace_id = %(locked_ws_id)s AND status <> 'deleted' FOR SHARE`（与 `claim_workspace_move` 的 `FOR UPDATE` `workspace_move.py:206`、`commit_workspace_move` 的 `FOR UPDATE OF w` `:737` 及 `bind_workspace_to_computer` 的 `UPDATE` `workspace.py:546` 互斥，保持绑定稳定直至提交）；
     3) 在持有上述两把 `FOR SHARE` 锁的前提下，核对 `ws_row["computer_id"] == snapshot_computer_id`、更新时 `_ws_key(locked_row.get("workspace_id")) == _ws_key(existing.get("workspace_id"))`，并检查零活跃迁移栅栏 `workspace_move_fences(locked_ws_id)`（`src/server/database/sql_fences.py:105-114`）。
   - **`Home` 工作区（在同一事务内确定稳定的实际运行位置）**：
     1) `Home` 轮次的实际运行电脑由 `home_turn_computer`（`src/server/services/home_placement.py:46-60`）根据主电脑可达性与 `home_stand_in` 决定；
     2) 在同一事务 `locked_conn` 内，先按 `ORDER BY computer_id` 锁定任务所有者名下全部存活电脑行：`SELECT {COMPUTER_COLS} FROM computers WHERE user_id = %(task_owner)s AND status <> 'deleted' ORDER BY computer_id FOR SHARE`（防止提交前发生 `make_primary` 主电脑切换 `workspace_move.py:718` 或电脑退役删除）；
     3) 再按 `ORDER BY workspace_id` 锁定 `Home` 行与 `Home` 替代工作区行：`SELECT workspace_id, computer_id, status, config FROM workspaces WHERE workspace_id IN (%(home_id)s, %(stand_in_id)s) AND status <> 'deleted' ORDER BY workspace_id FOR SHARE`（防止提交前 `Home` 重绑定或替代工作区换机）；
     4) 在上述行锁保护下于同一 `locked_conn` 调用 `home_turn_computer(task_owner, conn=locked_conn)` 计算当前实际运行电脑，核对其 `computer_id` 与锁前快照 `snapshot_computer_id` 一致。任何不一致均抛出 `AutomationRefusal("情况已变化，请重新发起", field="workspace_id")`。
2. **操作者权限复核与财报日历本人例外（C19）**：
   - 在同一事务连接 `locked_conn` 上完整复用入口授权判定条件（对齐 `src/dalton/autonomy/schedule.py:799-811,1051-1086`）：
     - **覆盖单元工作区**：重读 `get_active_unit_by_workspace(locked_ws_id, conn=locked_conn)`，确认单元活跃、`locked_lead == task_owner` 且 `effective_actor_id` 仍为主分析师或活跃 `admin`（PM）；
     - **非覆盖单元内部同事工作区**：复核 `require_workspace_access(..., channel="web", need="write", conn=locked_conn)`；
     - **`Home` 任务授权（含 `dalton-earnings-calendar` 例外）**：工具调用要求 `role == "chief_of_staff"`；当任务名称 `saved_name == "dalton-earnings-calendar"` 时，**允许内部成员与管理员本人（`effective_actor_id == task_owner`）创建或修改自己的财报日历任务**；其余 `Home` 全局任务（`saved_name != "dalton-earnings-calendar"`）强制复核 `effective_actor_id` 具备活跃 `admin`（PM）身份。
3. **模型、Claude Code 运行位置与思考强度复核（含显式保存相同模型，C6）**：
   - 当 `model_in_spec` 为真且 `final_model` 非空时，执行 `_check_managed_automation_model`；
   - 当 `model_in_spec` 为真（**含显式保存与原记录相同的 `final_model`**，或清除为 `None`）、新建任务（含缺省 `None`）或锁内模型/电脑变化时，在已加锁的 `locked_conn` 上调用 `await lifecycle.check_claude_code_hosted(task_owner, final_model, locked_ws_id, locked_conn, pref_for_lock)`；
   - 执行 `_reconcile_reasoning_effort` 重算思考强度，并按 `exc.field` 分发错误码（`"llm_model"` → `"invalid_llm_model"`，`"workspace_id"` → `"workspace_changed"`，其余 → `"invalid_reasoning_effort"`；REST `src/dalton/app/router.py:1388-1391` 保持 `reasoning_effort` 返回 422、其余返回 409）。

---

### 2.5 涉及的 `DALTON-HOOK` 与自有模块清单

全部上游文件改动复用已在 `scripts/dalton/check_hooks.sh:485` 登记白名单的钩子标识 **`DALTON-HOOK(cos-settings-tools)`**，通过 `./scripts/dalton/check_hooks.sh --strict --audit`（`scripts/dalton/pr_baseline.sh:187`）门禁校验：

| 钩子 ID / 归属 | 涉及文件 | 改动摘要 |
|---|---|---|
| `DALTON-HOOK(cos-settings-tools)` | `src/tools/secretary/approvals.py`、`src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py` | `StandingApprovalState.approval_snapshots` 快照持久化、首次无主电脑 `bootstrap_cloud_primary` 规则、自有电脑扣留且共用同一 `computer_id`、`manage_workspaces` 的 `archive` 与 `create(computer=...)`、`list` 多电脑输出 `computer_id`、`_folder_ready_result` 结构化白名单日志。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/database/{sql_fences.py,computer.py,workspace_move.py}`、`src/server/services/workspace_manager.py`、`src/server/services/computer_manager/{_machines.py,_lifecycle.py}`、`src/server/dependencies/cli_approval.py`、`src/server/app/{workspaces.py,computers.py}` | `FENCE_TAKES_WORKSPACES` 排除 `config.archiving = true`、`claim_workspace_move` 源电脑同级加锁、单事务 `claim_computer_for_archive` 锁工作区后复查迁移（含过期 `staged`/`committed`）、仅在提供商终态清除 `archiving` 栅栏、两个 HTTP 归档入口本地捕获沙箱异常并输出白名单日志。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/models/chat.py`、`src/server/app/threads/messaging.py`、`src/server/handlers/checkpoint_handler.py`、`src/server/services/{thread_agent.py,thread_status.py}` | `settle_resume_request` 按认证模式核验第一方 `session`（兼容 `oss` 回环试用并拦截渠道/API 客户端）、发起人、当前 `interrupt_id` 与快照；`ChatRequest.llm_model_system_only` 与 `thread_agent.py` 透传 `ignore_custom_models`；`thread_status.py` 输出当前挂起 `pending_interrupt_ids`。 |
| `DALTON-HOOK(cos-settings-tools)` | `src/server/services/automations/lifecycle.py`、`src/server/services/automation_executor.py`、`src/ptc_agent/agent/prompts/templates/components/{chief_of_staff.md.j2,user_profile.md.j2}` | `automation_executor.py` 携带 `llm_model_system_only` 进入请求与运行元数据；提示词补充选电脑新建、归档引导与 `llm_model` 参数说明。 |
| `DALTON-HOOK(cos-settings-tools)` | `web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{ProposalCard.tsx,SettledStep.tsx,CreateWorkspaceCard.tsx,SecretaryConfirmCard.tsx,SandboxSettingsPanel.tsx,sandbox/OverviewTab.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts,contentProjection.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`web/src/locales/{zh-CN,en-US}.json` | 对话确认卡片按 `tool_call_id` 关联四态结算与渲染块登记、自有电脑隐藏「始终允许」、网页端沙箱概览归档整机确认弹窗与移动端自适应布局。 |
| Dalton 自有目录（直接维护） | `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py`、`src/dalton/app/router.py`、`src/dalton/services/{automation_subscription_fallback.py,automation_thread_model.py}`、`integrations/dalton-channel-gateway/src/{orchestrator.ts,backend-client.ts}` | `dalton_schedule_automation` 暴露 `llm_model`、锁前偏好加载、事务内锁行至提交复核（含财报日历例外）与跨人私有模型隐藏；订阅回退与续跑轮次透传 `ignore_custom_models`；渠道网关通用中文提示及按 `interrupt_id` 清理替换旧待确认项。 |

---

## 3. 权限与安全

### 3.1 同事访问、跨人操作与共享工作区的权限边界

设发消息人为 **A**，目标工作区所有者为 **B**：

| 能力项 | 本人操作（`A == B`） | 普通内部同事跨人访问（`A != B`，已启用 `colleague_sharing`） | 管理员（PM）跨人操作（`A != B`） | 外部成员（`external`） |
|---|---|---|---|---|
| **1. 归档工作区** `manage_workspaces(action="archive")` 与网页归档 | 允许归档本人名下已停止、常规工作区所在且提供商支持冷归档（Daytona）的电脑（须经本人网页点击确认）。当前运行研究总监轮次的电脑引导前往网页停止后归档。 | **研究总监与个人电脑 HTTP 路由均拦截**：研究总监统一返回 `"workspace not found"`，个人电脑 HTTP 路由返回 `403`（`src/server/services/shared_workspaces/access.py:287-288`）。 | **他人个人电脑一律拦截**（研究总监返回 `"workspace not found"`，个人电脑 HTTP 路由返回 `403`）。仅组织托管账户（`is_holder(owner)`）名下的组织共享工作区允许组织管理员通过网页端执行整机确认归档（`src/server/services/shared_workspaces/access.py:289-291`）。 | 范围限于外部成员本人名下工作区。 |
| **2. 指定电脑新建工作区** `manage_workspaces(action="create", computer=...)` | 允许在本人名下任意存活且就绪的电脑上新建工作区（无主电脑时限定新建托管云端电脑）；目标为自有电脑（`MACHINE_KINDS`）时逐次弹卡确认；覆盖单元自动登记对内部成员与管理员生效。 | 新工作区固定创建在调用者 **A 本人**名下，`computer` 仅在 `get_computer_roster(A)` 中解析；传入 B 的电脑标识返回目标缺失提示。 | 同左，仅在管理员本人名下电脑创建本人工作区。 | 允许在外部成员本人电脑新建常规工作区，自动跳过团队覆盖单元登记（`src/tools/secretary/tools.py:319`）。 |
| **3. 托管自动化的 `llm_model`** `dalton_schedule_automation` | 允许修改本人工作区受管任务及本人 `dalton-earnings-calendar`（`src/dalton/autonomy/schedule.py:799-811,1078-1086`）的 `llm_model`（其余 `Home` 全局任务要求本人具备 PM 身份），支持系统模型、本人自定义模型与自定义服务商。 | ① 绑定活跃覆盖单元的工作区：限定当前主分析师（`lead_analyst_user_id == A`）可改，其余同事返回 `not_unit_lead_analyst`（`src/dalton/autonomy/schedule.py:1051-1068`）；② 常规内部同事工作区：要求具备 `web` 通道 `write` 权限，且任务归属调用者 A 本人。 | 在内部成员活跃覆盖单元工作区允许代管主分析师任务（`task_owner = lead_analyst`，审计记录 `actor_user_id = PM`）；显式 `llm_model` 限定系统目录模型并强制按系统目录端点解析，清除为 `"default"`/`null` 继承主分析师默认模型；跨人 `list` 与报错将主分析师私有自定义模型统一展示为「所有者自定义模型」；外部成员工作区限定其本人维护（`src/dalton/autonomy/schedule.py:727-731`）。 | 范围限于本人常规工作区受管任务（`src/dalton/autonomy/schedule.py:799-811`）。 |

`manage_workspaces` 仅对研究总监角色挂载（`src/ptc_agent/agent/agent.py:1679`）；工作区内分析师（`role == "analyst"`）使用限定为只读与拷入当前工作区动作的 `colleague_workspace` 工具（`docs/dalton/features/colleague-agent-access.md:24-30`），且 `dalton_schedule_automation` 限定分析师仅操作当前所在工作区（`src/dalton/autonomy/schedule.py:635-663`）。

### 3.2 确认动作的真实性与认证模式适配（C4、C16）

现有 `check_turn_body`（`src/server/dependencies/principal.py:480-520`）对 `principal.unlimited`（涵盖 `session` 与 `service`）及无 `computer_id` 的 `pat`/`oauth` 主体直接放行。为保证归档（`archive_workspace`）与指定电脑新建（`create_workspace`）两类确认的真实性，同时适配核心安装的本机回环试用场景，在 `AdmittedTurn` 与 `settle_resume_request`（`src/server/app/threads/messaging.py:266-315,633-656`、`src/server/handlers/checkpoint_handler.py:275-303`）执行分层核对：

1. **工具签名纯业务化**：`manage_workspaces` 参数表仅含业务字段，确认结果唯一来源于检查点中断恢复载荷。
2. **`DALTON_AUTH_MODE in ("local", "supabase")`（标准本地账号模式与 Everflow 生产模式）的四重强核验**：
   - 当 `request.hitl_response` 命中的当前最新检查点挂起中断包含 `archive_workspace` 或 `create_workspace` 时，强制核验：
     1) **第一方网页登录会话**：要求 `principal.auth == "session"`（背后分别由验证通过的本地 HS256 会话 JWT 或 Supabase JWT 签发，`src/server/utils/api.py:181-195`），且请求未携带 `X-Service-Token` 头、无 `request.platform` / `external_thread_id` 渠道标记；凡 `principal.auth in ("service", "pat", "oauth", "cli")` 一律返回 `403 Forbidden`（`code="web_session_required"`）；
     2) **发起人本人一致**：要求 `principal.user_id` 与发起该挂起轮次的用户本人完全一致（个人线程核对 `thread_meta["user_id"]`，共享/跨人线程核对 `latest_turn_sender(thread_id)`，`src/server/services/shared_workspaces/turns.py:275`）；
     3) **当前待确认项匹配**：`hitl_response` 键必须精确对应最新检查点当前挂起的 `interrupt_id`；
     4) **服务端快照匹配**：核对 `approval_snapshots[tool_call_id]` 存在且关键字段与挂起中断载荷一致。
3. **`DALTON_AUTH_MODE == "oss"`（核心安装本机回环试用模式）的边界与取舍（C16）**：
   - 在独立核心版的本机回环试用配置（`DOMAIN=localhost`，`scripts/standalone/init.sh:1005-1009`）下，启动校验已将该模式严格限制在回环监听地址并要求显式声明 `DALTON_ALLOW_DEV_OSS_AUTH=1`（`src/dalton/settings.py:896-916`）；未由桌面客户端注入 `LANGALPHA_OWNER_SECRET` 时，`owner_requests.is_owner`（`src/server/auth/owner_requests.py:28-38`）与 `get_user_id_any_state`（`src/server/utils/api.py:188-200`）将无凭据请求映射为单用户 `LOCAL_DEV_USER_ID` 且标记 `auth="session"`，服务端在密码学上无法单独证明请求来自浏览器登录。
   - **明确设计取舍**：在 `DALTON_AUTH_MODE == "oss"` 下，本机回环环境视为所有者本人，保留网页端点击确认上述两类卡片的能力（放行无凭据或携带有效 `x-langalpha-owner` 的 `principal.auth == "session"` 请求，并照常执行上述发起人、`interrupt_id` 与快照一致性核对）；同时**严格拦截一切渠道与 API 客户端确认请求**——凡 `principal.auth in ("service", "pat", "oauth", "cli")`、请求头携带非空 `X-Service-Token` 或非空 `Authorization` 令牌（注意不用 `_is_service_request`，因其在无 `INTERNAL_SERVICE_TOKEN` 的 `oss` 模式下对所有者请求也返回真，见 `src/server/app/threads/messaging.py:360`），或请求体携带 `platform` / `external_thread_id` 的恢复请求，一律返回 `403 Forbidden`。需要多进程凭据隔离或公网访问的部署统一使用默认的 `DALTON_AUTH_MODE=local`。
4. **其余中断保持不变**：当挂起中断为 `ask_user_question` 或 `credit_pause` 时，渠道网关通过 `service` 身份恢复执行的既有行为保持原样（`integrations/dalton-channel-gateway/src/orchestrator.ts:2011-2025`）。

### 3.3 异常路径本地捕获与源头结构化脱敏（C7、C20、C5）

将结构化字段白名单作为第一防线，`sanitize_error_text`（`src/server/utils/error_sanitization.py:32-40`）仅作为辅助兜底，消除把原始提供商响应体写入日志或交给全局处理器的路径：

1. **两个 HTTP 归档入口本地捕获沙箱异常（C20，`src/server/app/workspaces.py:143-170,945-974`、`src/server/app/computers.py:222-273,1107-1118`）**：
   - 现有 `_workspace_action_errors`（`workspaces.py:143-152`）与 `_computer_action_errors`（`computers.py:222-228`）在 `connect_failure(e) is None` 时将 `SandboxGoneError`、`SandboxTransientError` 重新抛出给全局处理器 `_sandbox_failure_handler`（`src/server/app/setup.py:1644-1655`），后者直接执行 `logger.warning(f"...: {single_line(str(exc))}")` 把提供商原始响应体写入日志。
   - **改造方案**：在两个归档 HTTP 入口本地捕获 `SandboxGoneError`、`SandboxTransientError`、`SSHConnectError`、`RuntimeError` 及未知 `Exception`：
     - 服务端日志**仅记录固定错误类别枚举**（如 `"sandbox_gone"`、`"sandbox_transient"`、`"ssh_connect_refused"`、`"archive_scope_changed"`、`"archive_unexpected_error"`）与**允许的结构化字段**（`action="archive"`、`computer_id`、`workspace_id`、`exc_type=type(e).__name__`、`ssh_code=refused.code if refused else None`），**跳过记录 `str(e)` 原文，且本地直接构造并抛出 `sandbox_failure_http(e)` 对应的 `HTTPException`（`503` 固定契约文案或 `422` 固定连接拒绝文案）**，使异常不再流入全局 `_sandbox_failure_handler`；
     - 领域拒绝异常（`ArchiveScopeChangedError`、`ArchiveUnsupportedError`、`ComputerNotStoppedError`、`ComputerBusyError`）直接返回固定白名单 `409` / `400` 响应，未知异常固定返回 `"Failed to archive workspace"` / `"Failed to archive computer"`。
2. **`ComputerManager._archive_machine` 与 `_folder_ready_result` 结构化白名单日志（C7、C20）**：
   - `_archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1896-1905`）失败恢复分支去掉 `exc_info=True` 与异常原文，仅记录固定错误类别（`"archive_reconcile_failed"`）与结构化字段（`computer_id`、`workspace_id`、`exc_type`）；
   - `_folder_ready_result`（`src/tools/secretary/tools.py:199-282`）及 `workspaces_create` / `workspaces_update` / `workspaces_archive` 异常日志同样改为仅记录固定阶段名（`stage`）、错误类别与结构化标识（`workspace_id`、`computer_id`、`exc_type`）；对外返回给模型的 `ToolMessage` 仅使用固定白名单业务提示或 `sandbox_unreachable_detail(e)`（`src/server/utils/error_sanitization.py:75-98`）。
   - 若未来某处诊断辅助字段确需格式化受控短文本，先限制在封闭枚举内并叠加 `single_line(sanitize_error_text(...))` 双重清洗，绝不以正则替换单独承担任意 SDK 异常文本的脱敏职责。
3. **所有者自定义模型名隐藏（C5）**：跨人写入拒绝消息、跨人 `list` 返回值与 `reasoning_effort` 错误统一隐藏私有模型名（见 §2.4），版本审计快照经 `sanitize_automation_snapshot`（`src/dalton/services/company_automations.py:256,360-377`）仅保留白名单字段。

---

## 4. 能力与安装形态

严格执行新部署架构的三层职责切分（C10）：

1. **安装形态 `src.dalton.profile`（`DeploymentProfile.EVERFLOW` / `DeploymentProfile.CORE`）**：决定安装默认覆盖配置（如 `dalton.core.yaml`、`agent_config.core.yaml`）及各项能力的默认开关预设。
2. **后端能力 `src.dalton.capabilities` 与运行时设置 `src.dalton.settings` / 沙箱配置**：决定服务端工具、路由与底层动作的实际启用状态。
3. **前端 `useCapabilities()`（`web/src/hooks/useCapabilities.ts:105-120`）**：决定可选界面入口与文案呈现；运维与独立版升级脚本（`scripts/dalton/ops/`、`scripts/standalone/upgrade.sh`）与业务代码解耦。

### 4.1 两种安装形态下的默认配置与实际可用性

| 工具 / 动作 | `DeploymentProfile.EVERFLOW` 默认表现 | `DeploymentProfile.CORE` 默认表现 | 实际启用判定规则（后端能力与设置） |
|---|---|---|---|
| `manage_workspaces(action="archive")` 与网页整机归档 | 工具默认挂载；仅在 **Daytona** 电脑（`src/ptc_agent/core/sandbox/providers/daytona_runtime.py:346`，`archives = True`）上可执行冷归档；E2B（`src/ptc_agent/core/sandbox/providers/e2b_runtime.py:448`）与自有 SSH 电脑在弹卡前明确告知冷归档不可用。 | 工具默认挂载；Core 默认沙箱提供商为 **Docker**（`agent_config.core.yaml:16`，`archives = False`），在弹卡前明确告知当前电脑冷归档不可用（停止即可释放资源），网页端归档按钮同步按 `archivable` 隐藏；若运维显式配置 Daytona 则自动启用。 | 研究总监角色固定挂载 `CHIEF_OF_STAFF_TOOLS`（`src/ptc_agent/agent/agent.py:1679-1682`）；实际能否发起冷归档由目标电脑提供商运行时 `archives` 布尔属性决定。 |
| `manage_workspaces(action="create", computer=...)` | 默认提供 | 默认提供 | 两种形态均开放多电脑管理与指定电脑新建工作区；覆盖单元自动登记由 `settings.coverage_units.enabled` 控制（`src/dalton/dalton.yaml:36` 与 `src/dalton/dalton.core.yaml:25` 默认均为 `true`）。 |
| `dalton_schedule_automation(llm_model=...)` | 默认提供（`autonomy.enabled` 默认开启） | 默认提供（`autonomy.enabled` 默认开启） | 登记为 `capability=CORE_CAPABILITY` 且受 **`setting_gate="autonomy"`** 控制（`src/dalton/tools/__init__.py:239,242,351,354,436-437`）：当 `settings.autonomy.enabled == True` 时向研究总监与分析师挂载，显式关闭 `autonomy.enabled` 时整体卸载。 |

### 4.2 涉及的周边能力开关与编排差异

1. **`CapabilityId.COLLEAGUE_SHARING`（`colleague_sharing`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:65,228-233`）**：控制同事工作区共享访问与 PM 跨人代管覆盖单元任务；跨人系统目录模型限制与私有模型隐藏作为基础安全守卫常驻生效。
2. **`CapabilityId.NATIVE_OAUTH_SYNC`（`native_oauth_sync`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:64,220-226`）**：控制工具说明与自动化 `README`（`src/ptc_agent/agent/backends/automations.py:309-314`）是否展示内置 Antigravity `*-oauth` 模型后缀提示，保持 Core 形态纯公共模型呈现。
3. **`CapabilityId.CHANNEL_GATEWAY` 及 `CHANNEL_FEISHU` / `CHANNEL_DISCORD` / `CHANNEL_WEIXIN`（Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:56-59`）**：Core 默认编排 `docker-compose.core.yml` 不包含渠道网关容器，仅运行桌面与手机网页确认卡片并通过 `scripts/standalone/upgrade.sh` 升级后端与前端；仅实际启用了 `channel_gateway` 能力的部署（如 Everflow）运行并发布 `dalton-channel-gateway` 服务。

---

## 5. 多端确认界面与渠道交互

### 5.1 桌面网页端呈现：按 `tool_call_id` 关联四态与面板归档确认（C3、C9）

1. **卡片生命周期四态划分与 `tool_call_id` 精确关联（C9）**：
   - 统一为 `create_workspace` 与 `archive_workspace`（及秘书动作卡片）携带 `tool_call_id`，改造 `decidePendingProposal`（`web/src/pages/ChatAgent/session/interrupts/useInterruptAnswers.ts:69-74`）与 `settleProposalFromResult`（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:176-210`），区分四种状态：
     - **`pending` / `submitting`（待执行）**：初始展示待确认操作按钮；用户点击「确认」后置为提交执行中（`busy=true`，按钮禁用并显示加载态），等待后端该 `tool_call_id` 对应的 `tool_call_result` 到达后再结算最终结果；
     - **`approved`（执行成功）**：`tool_call_result` 返回 JSON `success: true`，折叠行 `SettledStep` 显示勾选图标与「已归档电脑 `<computer_name>`（含 N 个工作区）」或「已在 `<computer_name>` 创建工作区：`<workspace_name>`」；
     - **`rejected`（用户取消）**：用户点击「取消」或 `tool_call_result` 为 `"User declined..."`，显示取消图标与「已取消归档」/「已取消创建工作区」；
     - **`failed`（执行失败，含快照变化）**：`tool_call_result` 返回 JSON `success: false`，渲染独立失败态图标与原因摘要——当 `code === "scope_changed"` 时固定显示 **「情况已变化，请重新发起」**，其余业务冲突显示对应白名单文案。
   - 实时 SSE 流（`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts:1220-1228`）与刷新历史回放（`web/src/pages/ChatAgent/session/history/resumeSettlement.ts:213`、`web/src/pages/ChatAgent/session/interrupts/fromHistoryEvent.ts:175-202,271-305`）统一按 `event.tool_call_id === card.tool_call_id` 结算，并在 `web/src/types/chat.ts`、`buckets.ts:14-27`、`web/src/pages/ChatAgent/components/messageList/buildRenderBlocks.ts:445-457`、`web/src/pages/ChatAgent/components/messageList/contentProjection.ts:53-58`（`RETAINED_TYPES`）及 `MessageContentSegments.tsx:494-543` 全量登记 `'archive_workspace'`。
2. **对话内确认卡片细节**：
   - **归档工作区卡片（`web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx`）**：展示目标电脑名称及快照中同机全部常规工作区名称与当前状态，仅提供「确认归档」与「取消」按钮。
   - **指定电脑新建工作区卡片（`web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx` 与 `WorkspaceCreatedSlip`）**：展示工作区名称、描述与「目标电脑：`<computer_name>`」（首次初始化显示「新建托管云端电脑」）；当 `computer_is_own_machine == true` 时隐藏「始终允许」按钮；常驻自动放行创建成功时，`web/src/pages/ChatAgent/utils/preapprovedCards.ts:81-85` 与 `WorkspaceCreatedSlip` 同步展示目标电脑名。
3. **沙箱面板概览页归档确认弹窗（`OverviewTab.tsx`、`SandboxSettingsPanel.tsx`）**：
   - 展示电脑名称与同机工作区列表，确认时提交 `expected_computer_id` + `expected_workspaces` 快照，遇 `409` 快照变化时提示「情况已变化，请重新发起」并刷新列表。

### 5.2 手机网页端适配

针对 320px–430px 窄屏视口加固三处排版：
1. `web/src/pages/ChatAgent/components/ProposalCard.tsx:84` 按钮栏改为 `pt-3 flex flex-wrap items-center gap-2`，保持 ≥ 36px 触控高度与自然换行；
2. 电脑名与工作区名添加 `break-words min-w-0`；
3. 同机工作区超过 5 个时默认展示前 5 个并提供「展开查看全部 N 个工作区」切换按钮。

### 5.3 飞书、Discord 与个人微信渠道交互：通用中文提示与按 `interrupt_id` 清理替换（C8、C21）

在启用了渠道网关的部署中，`integrations/dalton-channel-gateway/src/orchestrator.ts` 对飞书、Discord、个人微信（**群聊与私聊一致**）统一执行以下规则：

1. **仅发送中文通用确认提示与受登录保护链接（C8）**：
   - 在 `formatInterruptPrompt`（`integrations/dalton-channel-gateway/src/orchestrator.ts:6312-6319`）与 `handlePendingInterruptReply`（`integrations/dalton-channel-gateway/src/orchestrator.ts:1989-2009`）中，当 `kind === "web_only_approval"` 时统一发送中文通用文案：`该操作需要在网页端确认后继续：${this.webBaseUrl}/chat/t/${threadId}`，省略电脑名称、工作区名称及同机工作区清单。
2. **文字回复仅重发通用提示（C8）**：
   - 原 `interrupt_id` 仍待处理期间，用户在渠道聊天窗发送任何文字仅重发上述中文通用提示与网页链接。
3. **核对原 `interrupt_id` 是否仍待处理并支持新中断替换（C21，`src/server/services/thread_status.py:158-171,265-287`、`integrations/dalton-channel-gateway/src/orchestrator.ts:1962-1986,6266-6277`）**：
   - 用户在网页确认或取消旧卡片后，恢复运行的轮次可能紧接着触发新的中断（如 `ask_user_question` 或下一张确认卡），此时 `GET /api/v1/threads/{id}/status` 顶层 `status` 仍为 `"interrupted"`，仅凭 `status !== "interrupted"` 会误留旧卡片状态。
   - **后端扩展**：在 `read_thread_runtime_status`（`src/server/services/thread_status.py:158-171`）读取最新检查点时，提取当前挂起中断标识列表写入 `progress["pending_interrupt_ids"]`（及当前首个待处理中断的摘要载荷 `progress["pending_interrupts"]`；非中断态为空列表 `[]`）。
   - **网关精确清理与替换**：
     1) 当新中断 SSE 事件到达网关时（`orchestrator.ts:6266-6277`），`session_interrupt_update` 直接以新 `interrupt_id` 替换会话上的旧 `pending_interrupt`；
     2) 当渠道收到用户新消息进入 `handlePendingInterruptReply`（`orchestrator.ts:1962-1986`）时，调用 `getThreadStatus` 同时核对 `liveStatus.status` 与 `liveStatus.progress?.pending_interrupt_ids`：
        - 若 `liveStatus.status !== "interrupted"` 或 `pending_interrupt_ids` 为空：清空 `pending_interrupt: null`，将当前渠道消息作为正常新轮次派发；
        - 若 `liveStatus.status === "interrupted"` 但原 `pendingInterrupt.interrupt_id` 已不在 `pending_interrupt_ids` 中（旧卡片已在网页处理完毕，且轮次停在了新中断上）：立即用 `liveStatus.progress.pending_interrupts[0]` 替换会话上的旧 `pending_interrupt`，按新中断类型处理（若新中断是 `ask_user_question` 则向用户补发新问题提示，若仍是新 `web_only_approval` 则发送对应新待确认提示），确保渠道文字绝不再被当作对旧卡片的回复。

---

## 6. 分批计划与测试

### 6.1 数据库迁移评估与编号衔接约定（C2、C12）

- **本次迁移结论**：**零数据库迁移**。
  - 归档栅栏标记 `{"archiving": true}` 写入既有 `computers.config`（JSONB）列，过渡态复用既有 `COMPUTER_STATUSES` 中的 `'stopping'`（`src/server/database/computer.py:55-63`）；
  - 指定电脑新建工作区复用既有 `workspaces.computer_id` 列；
  - 确认快照持久化写入既有 LangGraph PostgreSQL 检查点状态通道（`PrivateStateAttr`）；
  - 托管自动化模型与 `llm_model_system_only` 标记复用既有 `automations.llm_model` 列、`automations.metadata`（JSONB）列、`d042` 的 `reasoning_effort` 列及 `d035` 的 `audit_history` 列。
- **后续演进迁移头核实与衔接约定**：当前仓库唯一的 Dalton Alembic 迁移头节点为 **`d046`**（`src/dalton/migrations/versions/d046_scoped_data_map_cache.py:23-24`，其 `down_revision` 为 `"d048"`）。按全局排期，微信多账号占用 `d049`，本功能占用 `d050`（接在 `d046` 之后；两者谁后合入，谁把自己的 `down_revision` 改到合入时的唯一迁移头），研究桥拆分自 `d051` 起顺序占用；未来若衍生新表或新列需求，须在合入当下检出仓库唯一的 Dalton 迁移头节点单线顺延。

### 6.2 分批开发范围与按部署形态区分的发布步骤

拆分为 **3 个独立可合入、可随时现网发布的开发批次**，每批合入前均执行 `./scripts/dalton/check_hooks.sh --strict --audit`（`scripts/dalton/pr_baseline.sh:187`）与 `make lint`：

| 批次 | 涉及文件清单 | 交付范围与发布步骤 |
|---|---|---|
| **Batch 1：托管自动化 `llm_model`、锁行至提交复核与轮次解析系统目录贯通** | `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py`、`src/dalton/app/router.py`、`src/server/models/chat.py`、`src/server/app/threads/messaging.py`、`src/server/services/thread_agent.py`、`src/server/services/automations/lifecycle.py`、`src/server/services/automation_executor.py`、`src/dalton/services/{automation_subscription_fallback.py,automation_thread_model.py}`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2` | 1) `dalton_schedule_automation` 暴露 `llm_model`（省略保留、`"default"`/`"clear"`/`""` 清除，REST `null` 清除）；<br>2) `schedule.py` 在锁外预读 `user_models.get_model_preference` 与工作区电脑快照；<br>3) 事务内按 `computers` → `workspaces` 顺序锁行至提交（含 `Home` 锁定关联电脑与工作区行确定运行位置），复核工作区存活、授权条件（保留本人修改 `dalton-earnings-calendar` 例外）及显式相同模型的运行位置；<br>4) 跨人显式赋值限定系统目录模型，保存时 `_reconcile_reasoning_effort` 共用同一份系统目录分类，执行时经 `ChatRequest` / `run_metadata` 传至 `thread_agent.py` 的 `resolve_llm_config` 及订阅回退路径；<br>5) **发布步骤**：Everflow 重启后端进程；Standalone Core 通过 `scripts/standalone/upgrade.sh`（`docs/dalton/dev-guide/STANDALONE-DEPLOY.md:513-519`）滚动更新 `backend`。 |
| **Batch 2：确认快照持久化、认证核验、指定电脑新建工作区与渠道旧卡片清理** | `src/tools/secretary/{approvals.py,chief_of_staff.py,tools.py,_commands.py}`、`src/server/services/{workspace_manager.py,thread_status.py}`、`src/server/services/computer_manager/_machines.py`、`src/server/app/threads/messaging.py`、`src/server/handlers/checkpoint_handler.py`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{ProposalCard.tsx,SettledStep.tsx,CreateWorkspaceCard.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`web/src/locales/{zh-CN,en-US}.json`、`integrations/dalton-channel-gateway/src/{orchestrator.ts,backend-client.ts}` | 1) 落地 `approval_snapshots[tool_call_id]` 快照持久化与恢复只读机制、首次无主电脑 `bootstrap_cloud_primary` 规则及 `_folder_ready_result` 结构化白名单日志；<br>2) `settle_resume_request` 落地 `local`/`supabase` 四重核验与 `oss` 回环试用边界；<br>3) `manage_workspaces(action="create", computer=...)` 接入 `pick_computer`、自有电脑扣留确认且共用同一 `computer_id`、`list` 多电脑返回 `computer_id`；<br>4) 前端卡片按 `tool_call_id` 四态结算；`thread_status.py` 输出 `pending_interrupt_ids`，渠道网关发送中文通用提示并按原 `interrupt_id` 清理/替换旧项；<br>5) **发布步骤**：Standalone Core 通过 `scripts/standalone/upgrade.sh` 更新 `backend` 与 `edge`（前端，Core 默认编排 `docker-compose.core.yml` 无网关服务）；启用了 `channel_gateway` 能力的部署（如 Everflow）同步构建部署前后端及 `integrations/dalton-channel-gateway`。 |
| **Batch 3：迁出与归档同级行锁栅栏、终态栅栏清除、双端整机归档确认与本地异常捕获** | `src/server/database/{sql_fences.py,computer.py,workspace_move.py}`、`src/server/services/workspace_manager.py`、`src/server/services/computer_manager/{_machines.py,_lifecycle.py}`、`src/server/dependencies/cli_approval.py`、`src/server/app/{workspaces.py,computers.py}`、`src/tools/secretary/{approvals.py,chief_of_staff.py,tools.py,_commands.py}`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/types/chat.ts`、`web/src/pages/ChatAgent/components/{SecretaryConfirmCard.tsx,SandboxSettingsPanel.tsx,sandbox/OverviewTab.tsx}`、`web/src/pages/ChatAgent/components/messageList/{MessageContentSegments.tsx,buildRenderBlocks.ts,contentProjection.ts}`、`web/src/pages/ChatAgent/session/interrupts/{buckets.ts,useInterruptAnswers.ts,fromLiveEvent.ts,fromHistoryEvent.ts}`、`web/src/pages/ChatAgent/session/history/resumeSettlement.ts`、`web/src/pages/ChatAgent/session/stream/processStreamEvent.ts`、`web/src/locales/{zh-CN,en-US}.json`、`integrations/dalton-channel-gateway/src/{orchestrator.ts,backend-client.ts}` | 1) 落地 `claim_workspace_move` 源电脑同级加锁、`claim_computer_for_archive` 锁工作区后复查迁移（含过期 `staged`/`committed`）及仅由提供商终态清除 `config.archiving` 栅栏；<br>2) `_archive_machine` 与两个 HTTP 归档入口改为本地捕获沙箱异常并仅输出固定错误类别与结构化白名单字段，补齐自归档拦截与 `cli_approval` 兜底；<br>3) 上线 `manage_workspaces(action="archive")`（仅 Daytona 可归档，运行中电脑引导至网页，Docker/E2B/SSH 弹卡前明确告知不可用）与网页 `OverviewTab` 整机确认弹窗；<br>4) **发布步骤**：Standalone Core 通过 `scripts/standalone/upgrade.sh` 更新 `backend` 与 `edge`；启用了 `channel_gateway` 能力的部署同步更新前后端及渠道网关（若独立于 Batch 2 合入）。 |

### 6.3 验收方式与测试矩阵（C11、C13–C21）

遵循先实机端到端验证、再以回归测试锁死契约边界的原则（`AGENTS.md:109`）：

1. **实机端到端验证**：
   - **隔离 Daytona 电脑与 Docker / E2B 对照验证**：在隔离的已停止 Daytona 电脑（绑定 2 个以上工作区）上分别通过研究总监与网页 `OverviewTab` 发起归档，验证整机工作区清单展示、批准后冷归档成功及刷新回放一致；在 Core 默认 Docker 与 E2B 电脑上请求归档，验证弹卡前明确告知冷归档不可用；在研究总监运行所在电脑请求归档，验证返回前往网页操作的引导提示。
   - **指定电脑新建、无主电脑初始化与多渠道验证**：验证按 `<activity>` 电脑名新建、自有电脑扣留自动放行并隐藏「始终允许」按钮、新账号无主电脑时创建托管云端主电脑；在启用渠道网关的环境中对飞书、Discord、个人微信（群聊与私聊）验证仅收到中文通用提示、文字回复仅重发提示、网页确认后即便立即进入下一张中断卡也能替换旧 `interrupt_id`。
   - **受管自动化 `llm_model` 验证**：验证主分析师本人与普通成员本人修改 `dalton-earnings-calendar`、PM 跨人设置系统目录模型并在触发轮次与续跑轮次走系统目录端点解析。

2. **确定时序并发测试与单元/前端回归测试**：
   - **确定时序双连接并发测试（PostgreSQL 实库连接）**：
     - 使用两个独立数据库连接 `conn_a` 与 `conn_b` 配合协程栅栏：① 快照采集后、`claim_computer_for_archive` 提交前，由 `conn_b` 分别执行新建同机工作区、迁入工作区、`claim_workspace_move` 发起迁出、改名及写入 `home_stand_in` 行，断言归档事务检测到不一致或活跃迁移立即回滚中止，`runtime.archive()` 调用次数为 `0`；② 构造心跳过期的 `staged` 与 `committed` 迁移记录，断言归档被持续阻断；③ 在 `conn_a` 持锁或已提交 `config.archiving = true` 期间，断言并发新建、重绑定与迁出均被挡住；④ 模拟进程退出触发 `_reconcile_provider_stop`（含心跳超时与 Redis 读失败超 180 秒），当模拟提供商返回 `"archiving"` 时断言机器状态结算为 `'stopped'` 而 **`config.archiving = true` 保持保留**（新建与迁入继续被拒），当提供商返回 `"archived"` / `"stopped"` / `"error"` 时断言栅栏清除。
   - **快照只读、无主电脑初始化与自动放行竞态单测**：
     - 断言 `interrupt()` 挂起期间切换主电脑后恢复执行仍只读 `approval_snapshots[tool_call_id]`；
     - 断言无主电脑快照（`computer_id=None, bootstrap_cloud_primary=True`）在用户名下已有未提升的自有电脑时仅新建托管云端电脑而不提升自有电脑，若确认等待期间出现了主电脑则中止并返回 `"情况已变化，请重新发起"`；
     - 断言 `auto_approve_workspace_creation=True` 遇自有电脑扣留弹卡，云端电脑放行使用同一快照 `computer_id`。
   - **后端确认身份与免登模式边界单测**：
     - 在 `DALTON_AUTH_MODE in ("local", "supabase")` 下，断言 `service`、`pat`、`oauth`、`cli` 及非发起人 `session` 提交两类工作区 `hitl_response` 均返回 `403`，渠道 `ask_user_question` 经 `service` 正常恢复；
     - 在 `DALTON_AUTH_MODE == "oss"` 下，断言回环无凭据网页请求可确认两类卡片，而携带 `X-Service-Token`、`Authorization` 令牌或 `platform` 渠道标记的请求一律返回 `403`。
   - **托管自动化锁行至提交、财报日历例外与轮次解析单测**：
     - 断言 `_user_automations_lock_for_write` 内对 `user_models.get_model_preference`（`src/server/services/automations/lifecycle.py:362`）调用次数为 `0`；
     - 使用双连接验证自动化事务持 `computers` 与 `workspaces` `FOR SHARE` 锁期间，并发 `commit_workspace_move` 被阻塞至自动化事务提交后才可执行；验证普通内部成员本人可保存 `dalton-earnings-calendar` 的 `llm_model`，修改其他 `Home` 全局任务返回 `forbidden_pm`；
     - **断言打在轮次解析入口**：构造任务所有者拥有与系统目录模型同名（如 `"claude-sonnet-4-6"`）但指向私有端点的 `custom_models` 项，由 PM 跨人写入该模型名，断言保存时 `_reconcile_reasoning_effort` 按系统目录分类计算，且触发首轮（`resolve_ptc_turn_llm_config`）、续跑轮次（`src/server/services/thread_agent.py:76` `thread_resolver` → `resolve_llm_config`）及订阅回退时均忽略所有者同名自定义模型而解析到系统目录客户端；断言 PM 显式提交私有模型被拒、跨人 `list` 与 `reasoning_effort` 报错均显示 `"所有者自定义模型"`。
   - **本地异常捕获与结构化脱敏单测**：
     - 向 `_archive_machine`、`_folder_ready_result` 及两个 HTTP 归档入口注入携带完整模拟凭据（`Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9`、`sk-proj_1234567890abcdef`、`AIzaSyA1234567890abcdefXYZ`、`postgresql://dbuser:s3cr3tP4ss@10.0.0.8:5432/app`）与任意提供商响应正文的 `SandboxTransientError` / `SandboxGoneError` / `RuntimeError`，断言归档 HTTP 入口未落入全局 `_sandbox_failure_handler`，且日志与响应仅含固定错误类别及结构化白名单字段，零提供商响应正文残留。
   - **三渠道与旧卡片替换单测 + 前端四态测试**：
     - 覆盖飞书、Discord、个人微信在旧卡片于网页确认/取消后立即进入新中断的场景，断言网关核对 `pending_interrupt_ids` 替换旧 `interrupt_id`，后续渠道文字不再回复旧卡；
     - 执行 `cd web && pnpm test && pnpm typecheck`、前端四态与 `OverviewTab` 测试，以及 `./scripts/dalton/check_hooks.sh --strict --audit` 与 `make lint`。

---

## 7. 已定案事项汇总与待决定事项

### 7.1 已定案设计决策汇总

1. **通道、快照持久化与首次建机（C1、C15）**：保持 `computer_cli_tokens.enabled = false`；弹卡前按 `tool_call_id` 持久化 `approval_snapshots`，恢复只读快照；常驻自动放行共用同一 `computer_id`，自有电脑逐次确认；无主电脑时 `computer_id=None` 限定新建托管云端主电脑，确认后若已出现主电脑则中止重来。
2. **归档与迁出互斥、终态清栅栏与双端一致（C2、C3、C13、C14）**：迁出与归档对源电脑统一按序取 `FOR UPDATE`，归档锁住工作区后复查迁移（过期 `staged`/`committed` 仍阻断归档），零数据库迁移；`config.archiving` 栅栏仅在提供商确认归档完成或明确失败时清除（提供商仍为 `archiving` 时保留栅栏）；研究总监对当前运行所在电脑引导至网页，网页归档同步改为整机快照确认。
3. **确认真实性与免登回环取舍（C4、C16）**：`local` 与 `supabase` 模式强制核验第一方 `session`、发起人、当前 `interrupt_id` 与快照；`oss` 回环试用模式允许本机网页点击确认，拦截一切渠道与 API 客户端确认。
4. **跨人模型贯通轮次解析与锁行复核（C5、C6、C17、C18、C19）**：跨人显式赋值限定系统目录模型，保存时 `_reconcile_reasoning_effort` 共用系统目录分类，执行时将 `llm_model_system_only` 传至 `thread_agent.py` 的 `resolve_llm_config` 及订阅回退路径；事务内锁住电脑与工作区行直至提交（含 `Home` 运行位置判定），保留本人修改 `dalton-earnings-calendar` 例外；本次仅开放 `llm_model`。
5. **本地异常捕获脱敏、渠道 `interrupt_id` 清理、四态卡片与部署分层（C7–C10、C20、C21）**：归档 HTTP 入口本地捕获沙箱异常，全链路源头仅记录固定错误类别与结构化白名单字段；渠道仅发中文通用提示并按原 `interrupt_id` 精确清理或替换旧项；卡片按 `tool_call_id` 区分四态；区分 Core `upgrade.sh` 与启用 `channel_gateway` 部署的发布步骤。

### 7.2 仍需用户决定的事项

无。
