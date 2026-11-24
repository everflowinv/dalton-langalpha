# 研究总监（Chief of Staff）设置类工具设计方案

> **文档路径**：`docs/dalton/features/cos-settings-tools.md`  
> **覆盖范围**：① 通过研究总监归档工作区（整机影响确认、锁内复核、沙箱错误脱敏、实时与历史回放）；② 在指定电脑上新建工作区（电脑名称/ID 解析、目标电脑确认展示）；③ 托管自动化的 `llm_model`（取锁前加载偏好、复用锁内模型与工作区校验、跨人隔离自定义模型列表）；④ `.agy/cos_settings_audit.md` 遗漏项第 2–6 条完成度核对。

---

## 摘要

上游第 11 次同步引入了沙箱内命令行 `la`（`cli/src/commands/*.ts`）配合应用内全局弹窗确认（`src/server/dependencies/cli_approval.py:177-205`、`web/src/components/CliApproval/CliApprovalHost.tsx:32-94`）的工作区与线程操作路径。Dalton 侧为隔离跨人派活场景下的同机沙箱身份并保留对话流内持久化卡片与渠道深链通知，默认将电脑 `lac1.*` 令牌保持关闭态（`src/dalton/dalton.yaml:16-17`、`src/dalton/dalton.core.yaml:13-14`），挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`）。

本方案遵循**“底层服务规则与上游 `la`/REST 同源复用，智能体入口保持服务端工具最小挂载（`DALTON-HOOK`）”**的原则：

1. **通过研究总监归档工作区**：在 `manage_workspaces` 开放 `action="archive"`，弹卡前校验本人所有权、已停止状态及沙箱归档支持度，查询同机全部存活工作区（复用 `src/server/dependencies/cli_approval.py:463` 同源的 `get_live_workspace_ids_for_computer`）生成整机影响快照，经 `hitl_confirm("archive_workspace", ...)` 落入 LangGraph 检查点实现实时流与历史回放一致展示；将整机工作区集合比对下沉至 `ComputerManager._archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1876`）的 `_observed_lock` 锁内复核，并复用 `sandbox_unreachable_detail` 与 `sanitize_error_text`（`src/server/utils/error_sanitization.py:32-40,75-98`）清洗沙箱异常；同步在归档 HTTP 路由补齐 `cli_approval` 门控兜底。
2. **在指定电脑上新建工作区**：在 `manage_workspaces(action="create")` 增加可选参数 `computer`，直接调用上游 `pick_computer`（`src/server/services/computer_reach.py:120-152`）解析 `<activity>` 中的电脑名称、完整 UUID 或 8 位以上唯一前缀，在 `manage_workspaces(action="list")` 多电脑场景下同步输出 `computer_id`；确认前解析并冻结目标 `computer_id` 与 `computer_name`，传入 `WorkspaceManager.create_workspace(..., computer_id=...)`（`src/server/services/workspace_manager.py:140-221`），并在 `CreateWorkspaceCard` 与 `WorkspaceCreatedSlip` 展示目标电脑。
3. **托管自动化的 `llm_model`**：受覆盖单元归属、元数据写保护与 `Home` 作用域约束，托管自动化继续通过专用工具 `dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1599`）承载。在 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:568-1668`）进入 `_user_automations_lock_for_write`（`src/dalton/autonomy/schedule.py:934`）之前预加载任务所有者模型偏好，新建、更新及行锁内复核统一复用 `lifecycle.check_model`（含自定义服务商父级 `get_custom_provider_config`）与 `lifecycle.check_claude_code_hosted`（含新建缺省模型时的默认 Claude Code 规则，`src/server/services/automations/lifecycle.py:296-354`），并在跨人代管（`actor_user_id != task_owner`）时将候选提示收敛为系统目录模型，隔离所有者的自定义模型列表。

三项能力均归属投研底座核心能力（`CORE_CAPABILITY`），在 `everflow` 与 `core` 两种安装形态下默认开放，可零数据库迁移平滑上线。

---

## 1. 现状核对与设置审计评估

### 1.1 上游 `la` 机制与 Dalton 现行运行状态

- **上游 `la` + `cli_approval` 链路**：每个电脑沙箱在启动或重连时由 `cli_tokens.keep()`（`src/server/services/cli_tokens.py:121-174`）写入 `lac1.<computer_id>.<secret>` 令牌文件（`LANGALPHA_TOKEN_FILE`，`cli/README.md:28-33`）。研究总监在 `Bash` 中调用 `la` 访问带有 `require_scopes` 声明的接口（`src/server/dependencies/principal.py:360-460`）；涉及删除、新建工作区、移除电脑或跨电脑启动运行时，`cli_approval.gate`（`src/server/dependencies/cli_approval.py:177-205`）将审批请求写入 Redis（`src/server/services/cli_approvals.py:192-408`，有效期 30 分钟，结案后保留 10 分钟），挂起当前 HTTP 请求并通过用户事件流唤醒前端全局弹窗 `CliApprovalHost` / `CliApprovalDialog`（`web/src/components/CliApproval/CliApprovalHost.tsx:32-94`、`web/src/components/CliApproval/CliApprovalDialog.tsx:45-168`）。
- **Dalton 现行令牌开关与工具挂载**：`src/dalton/dalton.yaml:16-17` 与 `src/dalton/dalton.core.yaml:13-14` 均将 `computer_cli_tokens.enabled` 设为 `false`（读取入口 `src/dalton/settings.py:622-627`）。由 `DALTON-HOOK(computer-cli-tokens-off)` 在三处静态收口：`cli_tokens.keep()` 直接返回 `False`（`src/server/services/cli_tokens.py:131-138`）、`CliTokenSweeper` 跳过巡检（`src/server/services/cli_token_sweep.py:57-62,103-110`）、`get_principal()` 拦截 `lac1.*` 凭证（`src/server/dependencies/principal.py:197-205`）。研究总监在主智能体构建阶段挂载服务端直连工具集 `CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:311-316`、`src/ptc_agent/agent/agent.py:1679-1682`），需用户确认的动作通过 `hitl_confirm`（`src/tools/secretary/_commands.py:42-65`）触发 LangGraph `interrupt()`，在对话流内渲染 `ProposalCard`（`web/src/pages/ChatAgent/components/ProposalCard.tsx:44-121`）。

### 1.2 三项待设计能力的三端现状对照

| 能力项 | 上游 `la` 命令行现状 | 网页端与 REST 接口现状 | Dalton 智能体工具现状 |
|---|---|---|---|
| **1. 归档工作区** | `la workspaces archive <ws>`（`cli/src/commands/workspaces.ts:147-156`）调用 `POST /api/v1/workspaces/{id}/archive`（`src/server/app/workspaces.py:945-974`）。**现存缺口**：① `cli/README.md:151-153` 将归档归入直通动作，路由直接进入执行分支，缺少 `cli_approval` 门控；② 路由依赖 `WorkspaceWriteUserId`（`src/server/dependencies/principal.py:429,452`，`own_computer=True`），与 `POST /computers/{id}/archive` 的 `ComputerEndUserId`（`src/server/app/computers.py:1108`、`src/server/dependencies/principal.py:434`）存在差异；③ Redis 审批记录仅有 10 分钟结案期（`src/server/services/cli_approvals.py:69`），缺少历史对话卡片回放。 | 沙箱面板概览页提供归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`），调用 `WorkspaceManager.archive_workspace`（`src/server/services/workspace_manager.py:452-457`）→ `_archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1872-1908`）与 `_claim_machine_for_archive`（`src/server/services/computer_manager/_machines.py:655-672`）；HTTP 异常经 `_workspace_action_errors`（`src/server/app/workspaces.py:132-171`）与 `sandbox_failure_http`（`src/server/utils/error_sanitization.py:100-117`）清洗。锁内当前仅校验活跃任务，缺少同机工作区列表变更复核。 | `manage_workspaces` 当前包含 `list`、`create`、`update`、`delete` 与 4 项跨区文件动作（`src/tools/secretary/chief_of_staff.py:98,121-166`），`archive` 动作待接入；前端 `SecretaryConfirmCard` 当前处理 `delete_workspace`、`stop_workspace`、`delete_thread` 三类提案（`web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx:5`）。 |
| **2. 指定电脑新建工作区** | `la workspaces create <name> [--description D] [--computer C]`（`cli/src/commands/workspaces.ts:113-146`）调用 `POST /api/v1/workspaces`。`cli_approval.workspace_create`（`src/server/dependencies/cli_approval.py:385-429`）解析主电脑或指定 `computer_id` 写入意图摘要，批准后锁定该电脑创建（`src/server/app/workspaces.py:278-281`），`CliApprovalDialog` 通过 `workspaceCreateOn` 显示目标电脑名（`web/src/lib/cliApprovals/copy.ts:129-134`）。**现存缺口**：① `--computer` 与 `WorkspaceCreate.computer_id`（`src/server/models/workspace.py:60-66`）仅接受 UUID，而 `<activity>` 输出电脑显示名（`src/tools/secretary/activity.py:261-274`）；② 常驻免批分支（`src/server/dependencies/cli_approval.py:399-400`）缺少同名/同代码去重扣留；③ 聊天内联条 `laWorkspaceCard` 仅显示工作区名称（`web/src/pages/ChatAgent/utils/preapprovedCards.ts:50-68`）。 | 新建弹窗通过 `useCreateWorkspace`（`web/src/pages/ChatAgent/hooks/useCreateWorkspace.ts:106-124`）传 `computerId` 至 `POST /api/v1/workspaces`（`src/server/app/workspaces.py:249-424`）→ `WorkspaceManager.create_workspace(..., computer_id=...)`（`src/server/services/workspace_manager.py:140-237`），含 `_chosen_computer` 归属与待退役校验（`src/server/services/workspace_manager.py:224-237`）及首次准备态校验（`src/server/services/workspace_manager.py:186-190`）。 | `<activity>`（`src/tools/secretary/activity.py:238-274`）与 `manage_workspaces(action="list")`（`src/tools/secretary/tools.py:88-93`、`src/tools/secretary/utils.py:79-108`）仅展示电脑名称；`manage_workspaces(action="create")` 参数表固定为主电脑落位（`src/tools/secretary/chief_of_staff.py:55-77`、`src/tools/secretary/tools.py:286-300,439-443`）；`CreateWorkspaceCard` 仅展示名称与描述（`web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx:6-14,67-86`）。 |
| **3. 托管自动化的 `llm_model`** | `la automations create/edit -m MODEL`（`cli/src/commands/automations.ts:55-100`）调用通用 `/api/v1/automations`，经 `cli_approval.automation_runs`（`src/server/dependencies/cli_approval.py:701-823`）预检 `check_model` 与 `check_claude_code_hosted`（`src/server/dependencies/cli_approval.py:758-764`）。**作用域边界**：通用接口限定为调用者本人常规工作区（`src/server/database/automation.py:595-602`）并将覆盖单元受管元数据设为只读保护（`src/server/services/automations/lifecycle.py:95-152`），主分析师代管任务、`Home` 全局任务及公司任务需走专用服务层。 | 网页通用保存路径（`src/server/app/automations.py:157-231,538-700` → `src/server/services/automations/lifecycle.py:618-760,805-1036,1172-1512` 及 `src/dalton/services/company_agent_writes.py:267-382`）已在取行锁前加载 `pref_for_lock`（`src/server/services/automations/lifecycle.py:1295-1303`），并在创建、更新前与行锁内（`recheck_locked_automation_update`，`src/server/services/automations/lifecycle.py:933-1022`）执行 `check_model`（含自定义服务商父级 `:345`）和 `check_claude_code_hosted`（含缺省模型继承 `:296-335`）。Dalton 专属保存路由 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）复用下述 `schedule_dalton_automation_helper`。 | 个人任务可通过 `.agents/user/automations/*.json` 修改 `llm_model`（`src/ptc_agent/agent/backends/automations.py:148`），受管任务由 `src/server/services/automations/file.py:1027` 分流至专用工具。`dalton_schedule_automation`（`src/dalton/tools/director_tools.py:1514-1588`）参数表尚待加入 `llm_model`；底层 `schedule_dalton_automation_helper`（`src/dalton/autonomy/schedule.py:934,1260-1267,1350-1462,1555-1609`）虽读取 `merged_spec["llm_model"]`，但偏好读取位于锁内（`:1267`），且缺少 `check_model`、`check_claude_code_hosted`、锁内工作区/模型复核与跨人自定义模型名脱敏。 |

### 1.3 `.agy/cos_settings_audit.md` 遗漏优先级第 2–6 条完成情况评估

审计文档「遗漏优先级」共列 6 条（高优先级 2 条、中优先级 3 条、低优先级 1 条）。其中第 1 条（高-1：保留专用 `upsert` 更新时的原时区）已在 `src/dalton/tools/director_tools.py:1543,1569` 与 `src/dalton/autonomy/schedule.py:743-744,1215,1336-1337` 完成修复。第 2–6 条逐项核对结果如下：

| 审计序号（原优先级） | 审计要求摘要 | 当前完成状态 | 源码核对位置（`文件:行号`） |
|---|---|---|---|
| **第 2 条（高-2）** | 修正通用自动化说明（区分个人文件与受管专用工具），修正回收站删除与保留期说明。 | **已完成** | `<user_profile>` 与 `<role>` 已写明个人文件与 `dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs` 的边界并按能力门控（`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2:90-93`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:54-58`）；自动化 `README.md` 已补齐受管分流说明及回收站 30 天保留与网页恢复说明（`src/ptc_agent/agent/backends/automations.py:52-56,69-73,264-296`；回执文案 `src/server/services/automations/file.py:1086-1089`）。 |
| **第 3 条（中-1）** | 补齐普通投递、完成通知、定时报告与总开关的关系，修正总开关页面路径与 Dalton 渠道示例。 | **已完成** | 总开关关闭提示已指向「消息与渠道 → 自动推送与接收账号 → 自动推送总开关」（`src/dalton/tools/notify_tools.py:56,668-675`）；交付规范模板已明确三类投递入口与总开关核对要求（`src/ptc_agent/agent/prompts/templates/components/dalton_deliverable.md.j2:13-16`）；`dalton_report_delivery` 工具说明已区分 `automation_report` 任务（`src/dalton/tools/report_delivery_tools.py:45-60`）；自动化与渠道 `README` 示例已更新为飞书、Discord、个人微信并要求先查 `available.json`（`src/ptc_agent/agent/backends/automations.py:42-43,108,153,217-348`、`src/ptc_agent/agent/backends/channels.py:54-62,68,82-90`）。 |
| **第 4 条（中-2）** | 补工作区元数据更新（`name`、`description`）和新建时选择计算机。 | **部分完成（`update` 已完成，新建选电脑归入本文 §2.3）** | `manage_workspaces(action="update")` 已上线并校验管理权限、保护 `Home` 保留行、提示目录改名影响（`src/tools/secretary/chief_of_staff.py:98-101,134-139`、`src/tools/secretary/tools.py:517-616`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:103`）；新建工作区指定电脑随前次 PR 撤回，按本文 §2.3 落地。 |
| **第 5 条（中-3）** | 补研究总监工作区归档入口及整台电脑受影响范围确认说明。 | **待完成（归入本文 §2.2）** | 网页端已有沙箱面板归档按钮（`web/src/pages/ChatAgent/components/sandbox/OverviewTab.tsx:198-209`），研究总监侧入口随前次 PR 撤回，按本文 §2.2 落地。 |
| **第 6 条（低-1）** | 角色提示补工作区删除及确认要求，模型区块补当前对话切换，自动化说明补 `workspace_id` 定位与迁移差异。 | **已完成** | 角色模板已写明 `manage_workspaces(action="delete")` 仅在用户明确要求时调用、先核对名称并等待确认（`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:103`）；`<model>` 区块已补充 `switch_thread_model` 用法与下条消息生效规则（`src/ptc_agent/agent/prompts/templates/envelope/baseline_model.md.j2:15-16`）；文件 `README` 与 `dalton_schedule_automation` 参数说明已分别写清迁移解绑固定线程与仅作定位的差异（`src/ptc_agent/agent/backends/automations.py:146`、`src/dalton/tools/director_tools.py:1544`）。 |

---

## 2. 推荐方案

### 2.1 通道选择：复用上游共享服务层，智能体交互采用服务端工具 + `hitl_confirm`

评估研究总监工作区操作走 `Bash` + `la` CLI 与走服务端直连工具两条路径时，代码库存在四项关键架构约束：

1. **跨人派活同机令牌隔离要求**：`WorkspaceManager.get_session_for_workspace`（`src/server/services/workspace_manager.py:306`）在获取工作区会话时调用 `cli_tokens.keep()`，将 `lac1.*` 写入电脑共享系统用户的 `LANGALPHA_TOKEN_FILE`，且 `core.due`（`src/server/services/cli_tokens.py:152`）仅按 `computer_id` 与 30 分钟剩余有效期判断是否刷新。在同事派活或跨人对话轮次（`A != B`）下，同机共享 OS 用户会使 A 的轮次读到 B 仍在有效期内的令牌，或将 A 的令牌落盘到 B 的电脑供同机其他进程读取（参见 `AGENTS.md:78` 与 `docs/dalton/features/colleague-agent-access.md:142`）。
2. **历史回放持久性要求**：`src/server/services/cli_approvals.py:3-22,69` 采用纯 Redis 瞬态存储（结案后 10 分钟清理），对话检查点中仅保留普通 `Bash` 命令文本，隔天打开历史对话时缺少可展开的已结确认卡片与确认当时的整机工作区快照。服务端工具调用 `hitl_confirm` 走 LangGraph `interrupt()`（`src/tools/secretary/_commands.py:55-64`），提案载荷持久保存在 PostgreSQL 检查点中，实时 SSE 流（`web/src/pages/ChatAgent/session/interrupts/fromLiveEvent.ts`）与历史回放（`web/src/pages/ChatAgent/session/interrupts/fromHistoryEvent.ts`）共用同一套渲染逻辑。
3. **外部聊天渠道通知链路**：`src/server/services/cli_approvals.py:305-307` 仅向网页全局生命周期流广播 `cli_approval`，对话流本身保持运行态直至 `Bash` 满 5 分钟转入后台任务（`AGENTS.md:64`）；渠道网关（`integrations/dalton-channel-gateway/src/orchestrator.ts:6206-6319`）监听对话 SSE 流中的 `interrupt` 事件，使用 `hitl_confirm` 可即时触发 `web_only_approval` 渠道深链提醒。
4. **Dalton 工作区与受管自动化扩展字段**：`manage_workspaces(action="create")` 承载覆盖单元自动登记（`coverage_kind` / `ticker` / `display_name`，`src/tools/secretary/chief_of_staff.py:72-75`）和目录就绪探测 `_folder_ready_result`（`src/tools/secretary/tools.py:161-283`），`dalton_schedule_automation` 承载主分析师归属与报告路由事务同步。

因此推荐架构为：**保持 `computer_cli_tokens.enabled = false` 默认配置，智能体侧统一通过服务端直连工具（`manage_workspaces` 与 `dalton_schedule_automation`）提供能力；将电脑解析、整机工作区快照校验、沙箱错误清洗、自动化锁内模型复核全部下沉或直接复用上游共享服务层，同时为 HTTP 归档路由补齐 `cli_approval` 门控**，使网页、服务端工具与可选开启的 `la` 三条路径行为保持一致。

```mermaid
flowchart TD
    CoS["研究总监 (Chief of Staff)"] -->|"归档 / 指定电脑新建工作区"| MW["服务端工具 manage_workspaces<br/>(src/tools/secretary/chief_of_staff.py)"]
    CoS -->|"托管自动化 llm_model"| DSA["专用工具 dalton_schedule_automation<br/>(src/dalton/tools/director_tools.py)"]

    MW -->|"复用 pick_computer + 同机工作区查询"| HITL["LangGraph hitl_confirm interrupt()<br/>写入检查点 (实时流 + 历史回放 + 渠道深链)"]
    HITL -->|"用户真实会话点击批准"| WM["WorkspaceManager / ComputerManager<br/>_observed_lock 锁内复核 + 错误脱敏"]

    LA["可选开启 la CLI 请求"] -->|"补齐 cli_approval.computer_archive<br/>复用 cli_approval.workspace_create"| WM

    DSA -->|"锁前预读 _safe_model_pref"| SCH["schedule_dalton_automation_helper<br/>(src/dalton/autonomy/schedule.py)"]
    SCH -->|"复用 check_model + check_claude_code_hosted<br/>+ 行锁内 _reconcile_locked_upsert + 跨人脱敏"| DB[("PostgreSQL automations")]
```

---

### 2.2 能力一：通过研究总监归档工作区

#### （1）复用上游 `la workspaces archive` + 归档服务层后仍待补齐的缺口

1. **归档路由门控缺口**：`POST /api/v1/workspaces/{workspace_id}/archive`（`src/server/app/workspaces.py:945-974`）与 `POST /api/v1/computers/{computer_id}/archive`（`src/server/app/computers.py:1107-1118`）直接执行归档，尚待接入 `src/server/dependencies/cli_approval.py`，且 `workspaces/{id}/archive` 当前采用 `WorkspaceWriteUserId`（`own_computer=True`）。
2. **整机影响快照与锁内复核缺口**：`ComputerManager._archive_machine`（`src/server/services/computer_manager/_lifecycle.py:1872-1908`）在 `_observed_lock(computer_id, "computer.archive")` 锁内仅检查活跃运行任务（`:1878`），同机工作区集合在等待确认期间的变更复核有待补齐。
3. **历史对话回放卡片缺口**：`CliApprovalDialog` 的审批状态仅保存在短期 Redis 键中，历史对话气泡缺少持久回放卡片。

#### （2）最小改动补齐方案（`DALTON-HOOK(cos-settings-tools)`）

1. **工具入口与弹卡前预检（`src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py`）**：
   - `chief_of_staff_workspaces` 的 `action` 增加 `"archive"`，在 `_CARDS["manage_workspaces"]`（`src/tools/secretary/_commands.py:72`）加入 `"archive"`（每次逐次确认，跳过 `standing_approval` 自动放行）。
   - 新增 `workspaces_archive(user_id, workspace_id, tool_call_id)`，在发起 `hitl_confirm` 前完成只读预检，将前置条件冲突拦截在弹卡前：
     - 校验 `workspace_id` 格式合法且 `get_workspace(workspace_id)` 存在、`str(workspace.get("user_id")) == user_id`（仅限本人工作区，其余情形统一返回 `"workspace not found"`）；
     - 仅接受常规用户工作区（命中 `is_reserved_row(workspace)` 时返回 `"Home cannot be archived."`）；
     - 通过 `manager.resolve_binding(workspace_id)` 与 `get_computer(binding.computer_id)` 读取目标电脑：当该电脑为当前研究总监轮次所在的 `Home` 电脑、`computer["status"] != "stopped"`（`src/server/services/computer_manager/_machines.py:660`）、缺少 `provider_ref`（`src/server/services/computer_manager/_machines.py:665`）、属于自有 SSH 机器或无冷归档能力的本地容器（对照 `src/server/services/computer_manager/_lifecycle.py:1892`），或 `await manager._machine_has_active_tasks(computer_id)` 为真（`src/server/services/computer_manager/_lifecycle.py:1878`）时，直接返回说明具体原因的 `error_command`。
   - **采集整机受影响范围**：复用 `src/server/dependencies/cli_approval.py:463` 同款查询 `get_live_workspace_ids_for_computer(computer_id, exclude_stand_in=True)` 获取同机全部存活工作区（含同机可能绑定的 `Home` 行），读取各自名称并按稳定顺序排列，构造确认载荷：
     ```json
     {
       "type": "archive_workspace",
       "workspace_id": "<target_ws_id>",
       "workspace_name": "<target_ws_name>",
       "computer_id": "<computer_id>",
       "computer_name": "<spoken_computer_name>",
       "affected_workspace_ids": ["<ws_id_1>", "<ws_id_2>"],
       "affected_workspace_names": ["NVDA", "Semiconductors"]
     }
     ```
   - 调用 `approved, _ = hitl_confirm("archive_workspace", payload)`；用户选择取消时返回 `decline_command("User declined workspace archive.", tool_call_id)`。

2. **锁内复核下沉（`src/server/services/workspace_manager.py:452`、`src/server/services/computer_manager/_lifecycle.py:1872`）**：
   - 为 `archive_workspace` 与 `_archive_machine` 增加可选参数 `expected_computer_id: str | None = None` 与 `expected_workspace_ids: Collection[str] | None = None`。
   - 在 `async with self._observed_lock(computer_id, "computer.archive"):` 锁内、调用 `_claim_machine_for_archive(computer_id)` 修改数据库状态**之前**：
     - 当传入 `expected_computer_id` 时，重新读取 `workspace_id` 行，核验该工作区处于存活态且 `str(row.get("computer_id")) == expected_computer_id`，确保绑定关系与确认时刻一致；
     - 当传入 `expected_workspace_ids` 时，重新查询 `get_live_workspace_ids_for_computer(computer_id, exclude_stand_in=True)` 并检查迁入该电脑的活跃迁移记录；若锁内实际工作区集合相对确认快照存在新增差集（`set(live_ids) - set(expected_workspace_ids)`），立即抛出专用异常 `ArchiveScopeChangedError(added_names)`，中止归档并提示研究总监按最新工作区列表重新发起确认。

3. **沙箱错误信息脱敏（`src/tools/secretary/tools.py`）**：
   - 调用 `archive_workspace` 时按异常类型分层转化为安全文本：
     - `ArchiveScopeChangedError`：返回同机新增工作区提示，请模型重新发起归档确认；
     - 已知业务状态冲突（电脑处于运行/过渡态、存在活跃任务或提供商属于本地/自有机器）：返回固定白名单说明；
     - `SandboxGoneError`、`SandboxTransientError` 或 `connect_failure(e) is not None`：调用 `sandbox_unreachable_detail(e)`（`src/server/utils/error_sanitization.py:75-98`），剥离底层请求 URL、沙箱 ID 与 SDK 响应体；
     - 其余异常：服务端日志记录 `single_line(sanitize_error_text(str(e)))`（`src/server/utils/error_sanitization.py:32-40,56-62`），工具消息返回固定文案 `"failed to archive workspace computer"`。

4. **路由侧 `cli_approval` 同步兜底（`src/server/dependencies/cli_approval.py`、`src/server/app/workspaces.py:945`、`src/server/app/computers.py:1107`）**：
   - 将 `POST /workspaces/{workspace_id}/archive` 的依赖切换为 `WorkspaceEndPrincipal`（`require_scopes("workspace:write", own_computer=False)`），与 `computers/{id}/archive` 接入同一个 `cli_approval.computer_archive` 构建器（在 `details` 与 `acts_on` 中记录 `affected_workspace_ids` 与 `affected_workspace_names`），并将确认快照传入 `archive_workspace(..., expected_workspace_ids=...)`，使 HTTP 路由与服务端工具共享同一套整机确认与锁内复核规则。

---

### 2.3 能力二：在指定电脑上新建工作区

#### （1）复用上游 `la workspaces create --computer C` + 创建服务层后仍待补齐的缺口

1. **电脑名称解析与列表 ID 缺口**：`<activity>`（`src/tools/secretary/activity.py:261-274`）输出电脑显示名，而 `la workspaces create --computer C` 与 `WorkspaceCreate.computer_id`（`src/server/models/workspace.py:60-66`）仅接受 UUID 格式，需补齐显示名与前缀解析。
2. **重名扣留与覆盖单元扩展参数衔接**：`cli_approval.workspace_create`（`src/server/dependencies/cli_approval.py:399-400`）需衔接 Dalton 的同名/同股票代码工作区去重扣留 `_standing_create_withheld`（`src/tools/secretary/approvals.py:88-104`）及 `coverage_kind` / `ticker` / `display_name` 选项。
3. **聊天流内联条的目标电脑展示缺口**：`laWorkspaceCard`（`web/src/pages/ChatAgent/utils/preapprovedCards.ts:50-68`）与 `WorkspaceCreatedSlip`（`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx:494-503`）尚待补充工作区落位的电脑名称展示。

#### （2）最小改动补齐方案（`DALTON-HOOK(cos-settings-tools)`）

1. **复用上游 `pick_computer` 支持电脑名称与 ID，并在列表补齐 `computer_id`**：
   - `manage_workspaces`（`src/tools/secretary/chief_of_staff.py:55-133`）新增可选参数 `computer: str | None = None`，文档说明：可传 `<activity>` 或 `manage_workspaces(action="list")` 列出的电脑名称、电脑 UUID 或 8 位以上前缀；省略时创建在用户主电脑上。
   - `workspaces_list`（`src/tools/secretary/tools.py:88-93`）在用户拥有多台电脑（`len(placement.computers) > 0`）时，除返回 `computer`（显示名）外同步返回 `computer_id`（`str(ws["computer_id"])`），便于同名多机场景消歧；`<activity>` 维持现有精简格式。
   - 在 `workspaces_create`（`src/tools/secretary/tools.py:286`）弹卡前解析目标电脑：
     - 当传入有效 `computer` 字符串时：读取 `roster = await get_computer_roster(user_id)`，调用 **`pick_computer(roster, computer)`**（`src/server/services/computer_reach.py:120-152`）解析出唯一电脑行；遇到同名歧义或目标缺失时，直接将 `ComputerUnreachable` 的可操作提示（含用户名下全部电脑名与 `computer_id`）经 `error_command` 返回模型，在弹卡前完成拦截；随后调用 `WorkspaceManager._chosen_computer(user_id, target_computer_id)`（`src/server/services/workspace_manager.py:224-237`）及就绪态检查（要求电脑处于可用状态，拦截 `status == "error"` 及首次沙箱尚在初始化的 `creating`/`starting` 电脑，对齐 `src/server/services/workspace_manager.py:186-190`）。
     - 省略 `computer` 时：读取 `await get_primary_computer(user_id)`（同 `src/server/dependencies/cli_approval.py:404`）获取当前主电脑的 `computer_id` 与 `spoken_computer_name`（新用户首次分配电脑前传 `target_computer_id = None` 交由 `ensure_primary_computer` 自动初始化）。

2. **冻结目标电脑并在确认卡片及免批回执中展示**：
   - 将解析得到的 `computer_id`、`computer_name`（以及是否为用户自有电脑标记 `computer_is_own_machine`）写入 `hitl_confirm("create_workspace", payload)` 载荷。
   - 用户点击批准恢复执行后，`workspaces_create` 将冻结的 `computer_id` 显式传给 `workspace_manager.create_workspace(user_id=user_id, name=name, description=description, computer_id=target_computer_id)`（`src/tools/secretary/tools.py:439-443`），与 `cli_approval.workspace_create`（`src/server/dependencies/cli_approval.py:390-391,428`）保持同一契约：即使等待确认期间用户切换了主电脑，新建工作区仍准确落在用户确认过的那台电脑上。
   - 在 `StandingApprovalMiddleware`（`src/tools/secretary/approvals.py:88-104`）中：当 `computer` 存在解析歧义、状态受限或目标属于用户自有机器（`kind in MACHINE_KINDS`，见 §7 待定 2）时，扣留自动放行并弹卡展示目标电脑；自动放行直接创建成功时，在返回结果（`src/tools/secretary/tools.py:478-481`）补入 `computer_name`，由 `web/src/pages/ChatAgent/utils/preapprovedCards.ts:81-85` 与 `WorkspaceCreatedSlip` 同步展示。
   - 新建完成后，`_folder_ready_result`（`src/tools/secretary/tools.py:161-283`）已原生按 `workspace["computer_id"]` 判断是否与当前 `Home` 同机：同机且会话就绪时直接建好目录返回 `folder_ready: True`，跨电脑时返回目标电脑上的预期 `folder_path` 与 `folder_ready: False` 提示，可直接复用。

---

### 2.4 能力三：托管自动化的 `llm_model`

#### （1）托管自动化继续采用专用服务端工具的原因

1. **跨人归属与权限模型差异**：`la automations create/edit` 调用通用 `/api/v1/automations`（`cli/src/commands/automations.ts:55-100`），该接口固定按 `principal.user_id` 查询和修改记录；在 Dalton 覆盖单元体系下，管理员（PM）通过研究总监代管覆盖单元任务时，任务的数据库属主 `task_owner` 为该单元的主分析师 `lead_analyst_user_id`（`src/dalton/autonomy/schedule.py:868-915`），通用接口因 `user_id` 归属差异返回 404。
2. **受管元数据写保护与派生规则**：通用更新接口将 `_MANAGED_UNIT_PROTECTED_METADATA_KEYS`（`unit_id`、`ticker`、`dalton_kind`、`dalton_managed`、`subject_kind`、`replaced_by_tracker`，见 `src/server/services/automations/lifecycle.py:95-152`）设为受保护只读键，而新建受管任务依赖 `schedule_dalton_automation_helper` 自动绑定 `unit_id`、推导 `research_tracking` 作用域并维护 `report_profile` 通知路由事务同步（`src/dalton/autonomy/schedule.py:1242-1246,1505-1554`）。
3. **`Home` 全局任务作用域**：电脑令牌的作用域限定在常规工作区自动化（`src/server/database/automation.py:578-602`），而 `dalton-earnings-calendar` 等全局受管任务固定运行在 `Home`（`src/dalton/autonomy/schedule.py:633,796-811`）。
4. **双角色与网页接口同源复用**：`dalton_schedule_automation` 同时供研究总监、公司工作区分析师以及网页端 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1330-1398`）调用，在 `schedule_dalton_automation_helper` 一处补齐锁前偏好加载与锁内校验即可全入口生效。

#### （2）替代方案与锁内校验补齐设计

1. **工具层暴露 `llm_model` 参数（`src/dalton/tools/director_tools.py:1514-1588`）**：
   - 在 `dalton_schedule_automation` 参数签名新增 `llm_model: str | None = None`：
     - `llm_model is None`（省略）：新建时继承任务所有者默认模型，更新时保持已有 `llm_model`（省略 `spec["llm_model"]` 键）；
     - `llm_model.strip().lower() in ("", "default", "clear", "null")`：放入 `spec["llm_model"] = None`，清除任务级模型覆盖并回退至账户默认模型；
     - 其他有效字符串：放入 `spec["llm_model"] = llm_model.strip()`。

2. **进入写锁前预加载偏好（`src/dalton/autonomy/schedule.py:934,1265-1267`）**：
   - 将 `_safe_model_pref(task_owner, None)` 从 `_user_automations_lock_for_write` 内部（现位于 `src/dalton/autonomy/schedule.py:1267`）**前移至 `:934` 进入事务与咨询锁之前**：
     - 当 `resolved_action == "upsert"` 时，先在锁外读取 `pref_for_lock = await _safe_model_pref(task_owner, None)`（命中 Redis 缓存或单次短连接查询，减少持有咨询锁与行锁期间的连接池占用，对齐 `src/server/services/automations/lifecycle.py:629-631,1295-1303`）。
     - 进入锁内后，若覆盖单元刚发生主分析师变更（`locked_lead != task_owner`，`src/dalton/autonomy/schedule.py:959-966`），现有分支直接返回 `not_unit_lead_analyst` 终止写入，因此锁外按 `task_owner` 预读的偏好在全部放行写入路径上均准确对应实际任务属主。
     - 同步在 `active_conn` 同一事务连接内按既有顺序在锁行前读取快照所需的 `prelocked_pref_row` 并传入 `automation_history_context`（`src/dalton/autonomy/schedule.py:1442,1584`），保持单连接事务闭环。

3. **封装跨人脱敏的模型检查函数，完整复用网页保存路径检查（`src/dalton/autonomy/schedule.py` 复用 `src/server/services/automations/lifecycle.py`）**：
   - 新增内部辅助函数 `_check_managed_automation_model(task_owner, actor_user_id, model_name, pref_for_lock)`：
     - 当 `actor_user_id != task_owner`（例如管理员 PM 代改主分析师的覆盖单元任务）时：按推荐策略（见 §7 待定 3）仅接受系统目录模型（`get_configured_llm_models()`），或调用 `lifecycle.check_model(task_owner, model_name, pref_for_lock)` 并在捕获 `AutomationRefusal` 时**重写错误消息，仅拼接系统已配置模型列表 `sorted(n for group in get_configured_llm_models().values() for n in group)`，剥离 `custom_model_names(pref_for_lock)`**，保护任务所有者的私有自定义模型名称列表；
     - 当 `actor_user_id == task_owner`（本人操作）时：直接委托 `lifecycle.check_model(task_owner, model_name, pref_for_lock)`（`src/server/services/automations/lifecycle.py:337-354`），完整支持系统目录模型、本人 `custom_models` 以及自定义服务商父级配置 `user_models.get_custom_provider_config(task_owner, model_name, _pref_cache=pref_for_lock)`（`src/server/services/automations/lifecycle.py:345`），并将返回的 `(source, entry)` 分类结果透传给 `_reconcile_reasoning_effort(..., classified=classified)`。
   - **新建任务（`existing is None`，`src/dalton/autonomy/schedule.py:1555-1609`）**：
     - 当 `create_llm_model` 有值时，先调用 `_check_managed_automation_model` 获取 `classified`；
     - **无论 `create_llm_model` 取值为字符串或 `None`**，统一调用 `await lifecycle.check_claude_code_hosted(task_owner, create_llm_model, ws_id, active_conn, pref_for_lock)`（`src/server/services/automations/lifecycle.py:296-318,663`）：当 `create_llm_model is None` 时，内部自动通过 `_claude_code_default_for`（`:320-335`）检查 `task_owner` 继承的账户默认或部署默认模型，在受限云端电脑的常规工作区上及时拦截 Claude Code 默认模型。
   - **更新任务与行锁内复核（`existing is not None`，`src/dalton/autonomy/schedule.py:1350-1462`）**：
     - 锁前初检：当 `model_in_spec and spec_llm_model and spec_llm_model != existing.get("llm_model")` 时执行 `_check_managed_automation_model`；当目标模型相对 `existing.get("llm_model")` 变化时执行 `check_claude_code_hosted`。
     - **行锁内复核（升级 `_reconcile_locked_effort` 为 `_reconcile_locked_upsert`，在 `auto_db.update_automation` 获取 `FOR UPDATE` 行锁后执行，对齐 `lifecycle.recheck_locked_automation_update` `src/server/services/automations/lifecycle.py:933-1022`）**：
       1. 核验工作区保持原位：若 `_ws_key(locked_row.get("workspace_id")) != _ws_key(existing.get("workspace_id"))`，抛出 `AutomationRefusal(f"任务 `{resolved_name}` 已移到其他工作区，请刷新后重试。", field="workspace_id")`（与 `pause`/`resume` 在 `src/dalton/autonomy/schedule.py:1155-1160` 的锁内守卫对齐）；
       2. 取锁内实际工作区 `locked_ws_id` 与最终模型 `final_model`：当 `model_in_spec and final_model and final_model != locked_row.get("llm_model")` 时，在锁内复核 `_check_managed_automation_model`；
       3. 当 `final_model != locked_row.get("llm_model")`（含清除为 `None` 后继承默认 Claude Code 模型的情形）时，在 `locked_conn` 上复核 `await lifecycle.check_claude_code_hosted(task_owner, final_model, locked_ws_id, locked_conn, pref_for_lock)`；
       4. 在锁内复核 `_reconcile_reasoning_effort`（当模型变更且调用方省略 `reasoning_effort` 时，自动清理或钳制已存强度值，`src/server/services/automations/lifecycle.py:512-521`）。
   - **错误字段精准映射**：将 `src/dalton/autonomy/schedule.py:1374,1459,1570` 中固定写死的 `"invalid_reasoning_effort"` 改为按 `exc.field` 分发（`exc.field == "llm_model"` 返回 `"invalid_llm_model"`，`exc.field == "workspace_id"` 返回 `"workspace_changed"`，其余返回 `"invalid_reasoning_effort"`）；REST 入口 `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py:1388-1391`）保持 `reasoning_effort` 返回 422、`llm_model` 冲突返回 409，与 `/api/v1/automations` 口径一致。

---

### 2.5 涉及的 `DALTON-HOOK` 清单

全部上游文件改动复用已在 `scripts/dalton/check_hooks.sh:485` 登记白名单的既有钩子标识 **`DALTON-HOOK(cos-settings-tools)`**：

| 钩子 ID | 涉及的上游文件 | 挂载内容摘要 |
|---|---|---|
| `cos-settings-tools` | `src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/_commands.py`、`src/tools/secretary/approvals.py` | `manage_workspaces` 增加 `archive` 动作与 `create(computer=...)` 参数、`list` 多电脑输出 `computer_id`、弹卡前校验与沙箱错误清洗、常驻自动放行扣留规则。 |
| `cos-settings-tools` | `src/server/services/workspace_manager.py`、`src/server/services/computer_manager/_lifecycle.py`、`src/server/dependencies/cli_approval.py`、`src/server/app/workspaces.py`、`src/server/app/computers.py` | `_archive_machine` 在 `_observed_lock` 锁内复核同机受影响工作区集合与绑定电脑；归档路由同步接入 `cli_approval.computer_archive` 兜底。 |
| `cos-settings-tools` | `src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2` | 补充研究总监新建工作区选电脑、归档工作区先核对整机影响并等待确认的提示词说明。 |
| `cos-settings-tools` | `web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx`、`web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx`、`web/src/pages/ChatAgent/components/ProposalCard.tsx`、`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx`、`web/src/pages/ChatAgent/session/interrupts/{buckets,fromLiveEvent,fromHistoryEvent}.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`web/src/locales/{zh-CN,en-US}.json` | 新建工作区卡片显示目标电脑；归档工作区卡片在实时流与历史回放中展示目标电脑与同机受影响工作区列表，优化移动端折行布局。 |

> `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py` 与 `integrations/dalton-channel-gateway/src/orchestrator.ts` 位于 Dalton 自有目录内，按自有模块直接维护。

---

## 3. 权限与安全

### 3.1 同事访问与跨人场景下的权限边界

设发消息人为 **A**，目标工作区所有者为 **B**：

| 能力项 | 本人操作（`A == B`） | 普通内部同事跨人访问（`A != B`，已启用 `colleague_sharing`） | 管理员（PM）跨人操作（`A != B`） | 外部成员（`external`） |
|---|---|---|---|---|
| **1. 归档工作区** `manage_workspaces(action="archive")` | 允许归档本人名下已停止、常规工作区所在且支持冷归档的电脑（须经本人点击确认）。 | **仅限所有者本人操作**（归档作用于整台电脑，即便目标工作区开启 `cos_access="rw"` 或 `colleague_dispatch` 仍拦截，统一返回 `"workspace not found"`）。 | **仅限所有者本人操作**（他人个人电脑上的工作区统一返回 `"workspace not found"`）。 | 范围限于外部成员本人名下工作区。 |
| **2. 指定电脑新建工作区** `manage_workspaces(action="create", computer=...)` | 允许在本人名下任意存活且就绪的电脑上新建工作区；覆盖单元自动登记对内部成员与管理员生效。 | 新工作区固定创建在调用者 **A 本人**名下，且 `computer` 仅在 `get_computer_roster(A)` 中解析；传入 B 的电脑标识返回目标缺失提示。 | 同左，仅在管理员本人名下电脑创建本人工作区。 | 允许在外部成员本人电脑新建常规工作区，自动跳过团队覆盖单元登记（`src/tools/secretary/tools.py:319`）。 |
| **3. 托管自动化的 `llm_model`** `dalton_schedule_automation` | 允许修改本人工作区受管任务及本人 `dalton-earnings-calendar` 的 `llm_model`，支持本人系统模型、自定义模型与自定义服务商。 | ① 绑定活跃覆盖单元的工作区：限定当前主分析师（`lead_analyst_user_id == A`）可改，其余同事返回 `not_unit_lead_analyst`（`src/dalton/autonomy/schedule.py:1051-1068`）；② 常规内部同事工作区：要求具备 `web` 通道 `write` 权限，且任务归属调用者 A 本人。 | 在内部成员活跃覆盖单元工作区允许代管主分析师任务（`task_owner = lead_analyst`，审计历史记录 `actor_user_id = PM`）；`llm_model` 限定为系统目录模型或 `null`（隔离主分析师私有自定义模型）；外部成员工作区限定其本人维护（`src/dalton/autonomy/schedule.py:727-731`）。 | 范围限于本人常规工作区受管任务（`src/dalton/autonomy/schedule.py:799-811`）。 |

此外，在工作区内分析师（`role == "analyst"`）跨人轮次中：`manage_workspaces` 仅对研究总监角色注册（`src/ptc_agent/agent/agent.py:1679`），`colleague_workspace` 工具限定为只读与拷入当前工作区动作（`docs/dalton/features/colleague-agent-access.md:24-30`），且 `dalton_schedule_automation` 限定分析师仅操作当前所在工作区（`src/dalton/autonomy/schedule.py:635-663`）。

### 3.2 确认动作的真实性保障

1. **工具签名保持纯业务字段**：`manage_workspaces` 的 `archive`、`create`、`delete` 参数表仅接收业务标识与元数据，将确认状态交由服务端检查点状态机唯一决定。
2. **服务端 `interrupt()` 状态机与登录态绑定**：
   - `hitl_confirm`（`src/tools/secretary/_commands.py:42-65`）调用 LangGraph `interrupt()` 挂起当前图执行，仅接受用户本人有效登录会话（`session`）向 `POST /api/v1/threads/{id}/messages` 提交带匹配 `interrupt_id` 的 `hitl_response` 恢复运行。
   - 电脑令牌（`lac1.*`）在 `check_turn_body`（`src/server/dependencies/principal.py:515-516`）阶段直接拦截携带 `hitl_response` 的请求。
   - 聊天渠道网关将 `create_workspace` 与 `archive_workspace` 归入 `web_only_approval`（`integrations/dalton-channel-gateway/src/orchestrator.ts:1989-2009,6229-6233`），确保批准动作全部经网页登录态完成。
   - 共享对话只读观察者视角（`readOnly` 或 `othersPause`，`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx:511-512,539-540`）前端隐藏操作按钮，后端 `require_thread_access` 仅放行会话所有者的恢复请求。
3. **意图与目标锁定**：新建工作区在弹卡时解析并固化 `target_computer_id`，归档工作区在弹卡时固化 `expected_computer_id` 与 `expected_workspace_ids`，批准恢复后按固化快照在锁内核验执行，消除等待窗口内的并发漂移。

### 3.3 日志与错误信息脱敏

1. **沙箱异常脱敏**：底层容器/云沙箱 SDK 异常可能包含内部请求地址、连接串凭据（`postgresql://user:pw@...`）、`Bearer` 令牌、`sk-*` 密钥或响应体。`workspaces_archive` 与 `workspaces_create` 对外返回给模型的 `ToolMessage`（持久化写入检查点与 `thread_transcripts`）仅使用固定业务提示或 `sandbox_unreachable_detail(exc)`（`src/server/utils/error_sanitization.py:75-98`）；服务端日志统一调用 `single_line(sanitize_error_text(str(exc)))`（`src/server/utils/error_sanitization.py:32-40,56-62`），隐去凭据并转义回车换行符。
2. **所有者自定义模型列表隔离**：当 `effective_actor_id != task_owner` 时，模型校验失败消息仅输出系统级已配置模型（`get_configured_llm_models()`），将 `task_owner` 在 `user_preferences.model_preference["custom_models"]` 中登记的私有模型名称、自定义端点与服务商配置严格保留在所有者本人视角内；服务端日志仅记录 `automation_id`、`actor_user_id` 与提交的 `llm_model` 标识，略去 `model_preference` 明细映射。
3. **自动化版本快照清洗**：`llm_model` 已列入 `ALLOWED_AUTOMATION_SNAPSHOT_FIELDS`（`src/dalton/services/company_automations.py:256`），写入 `audit_history` 时继续经 `sanitize_automation_snapshot`（`src/dalton/services/company_automations.py:360-377`）仅保留白名单字段。

---

## 4. 能力与安装形态

严格遵循独立核心架构的分层约定（部署形态 `src.dalton.profile`、可选能力 `src.dalton.capabilities` 与前端 `useCapabilities()`、上游钩子 `DALTON-HOOK`、运维脚本四者分离）：

### 4.1 两种安装形态（`DeploymentProfile`）下的开放范围

| 工具 / 动作 | `DeploymentProfile.EVERFLOW`（`APP_ENV=dalton` / 缺省） | `DeploymentProfile.CORE`（`DALTON_PROFILE=core` / `APP_ENV=core`） | 形态差异处理方式 |
|---|---|---|---|
| `manage_workspaces(action="archive")` | **默认提供** | **默认提供** | 归属研究总监基础工具集（`src/ptc_agent/agent/agent.py:1679-1682`）。运行时按目标电脑沙箱提供商的冷归档能力判定：Daytona / E2B 等支持归档的电脑正常弹卡归档；默认本地 Docker 或自有 SSH 电脑在弹卡前返回友好说明（提示停止电脑即可释放计算开销）。 |
| `manage_workspaces(action="create", computer=...)` | **默认提供** | **默认提供** | 两种形态均支持用户名下多电脑管理与指定电脑新建工作区；覆盖单元自动登记跟随 `coverage_units.enabled`（`src/dalton/dalton.yaml:36` 与 `src/dalton/dalton.core.yaml:25` 均为 `true`）。 |
| `dalton_schedule_automation(llm_model=...)` | **默认提供** | **默认提供** | 在研究总监与分析师工具集（`src/dalton/tools/__init__.py:220-244,340-356`）均注册为 `capability=CORE_CAPABILITY`（`src/dalton/capabilities.py:38`），随投研核心骨架在两种形态下统一挂载。 |

### 4.2 涉及的能力开关（`CapabilityId` 与 `useCapabilities()`）

本设计三项能力均归属核心通用能力（`CORE_CAPABILITY`），直接复用现有 `CapabilityId` 枚举体系；与周边可选能力的联动全部通过 `has_capability(...)`（后端 `src/dalton/capabilities.py:437-446`）和 `useCapabilities()`（前端 `web/src/hooks/useCapabilities.ts:105-120`）控制：

1. **`CapabilityId.COLLEAGUE_SHARING`（`colleague_sharing`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:65,228-233`）**：开启时启用同事工作区共享读取与 PM 跨人代管覆盖单元任务；Core 默认关闭时仅限本人访问，跨人自定义模型列表脱敏逻辑作为基础安全守卫常驻生效。
2. **`CapabilityId.NATIVE_OAUTH_SYNC`（`native_oauth_sync`，Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:64,220-226`）**：控制 `dalton_schedule_automation` 工具说明及自动化 `README`（`src/ptc_agent/agent/backends/automations.py:309-314`）是否渲染内置 Antigravity `*-oauth` 模型的后缀强度提示，确保 Core 形态保持纯公共模型配置。
3. **`CapabilityId.CHANNEL_GATEWAY` 及 `CHANNEL_FEISHU` / `CHANNEL_DISCORD` / `CHANNEL_WEIXIN`（Everflow 默认开、Core 默认关，`src/dalton/capabilities.py:56-59`）**：开启时由渠道网关推送 `web_only_approval` 确认深链；Core 默认关闭时运行桌面与手机网页端内联确认卡片。
4. **运维脚本边界清晰**：三项能力集中在应用层工具、服务与前端组件，与 `scripts/dalton/ops/` 及 `scripts/standalone/` 运维脚本相互独立。

---

## 5. 多端确认界面与渠道交互

### 5.1 桌面网页端呈现（实时流 + 历史回放）

1. **归档工作区确认卡片（扩展 `web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx`）**：
   - 在 `PROPOSAL_INTERRUPT_TYPES` 与 `PROPOSAL_DATA_KEY_MAP`（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:14-27`）、实时流投影 `web/src/pages/ChatAgent/session/interrupts/fromLiveEvent.ts:177-208`、历史回放投影 `web/src/pages/ChatAgent/session/interrupts/fromHistoryEvent.ts:271-304` 及 `web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx:531-543` 注册 `'archive_workspace'`，映射到 `secretaryActionProposals` 桶。
   - **待确认态（`pending`）**：使用 `Archive` 图标，标题显示「归档电脑及同机工作区」，详情框分两栏展示：
     - **目标电脑**：`<computer_name>`；
     - **同机受影响工作区（共 N 个）**：以标签或列表完整展示 `affected_workspace_names`（目标工作区排在首位，其余同机工作区并列展示），附一行说明：「归档会将整台已停止电脑移至冷存储，同机全部工作区下次启动预计需 1–5 分钟恢复，期间零计算资源占用。」
     - 操作栏仅展示「确认归档」与「取消」两个按钮（每次归档逐次确认）。
   - **已结态与历史回放（`approved` / `rejected`）**：由 `settleProposalFromResult`（`web/src/pages/ChatAgent/session/interrupts/buckets.ts:176-210`）根据后续 `ToolMessage` 自动结算（`success: false` 结算为未执行态），折叠行 `SettledStep` 显示「已归档电脑 `<computer_name>`（含 N 个工作区）」或「已取消归档」，点击展开后原样呈现确认当时的电脑名与受影响工作区快照。

2. **指定电脑新建工作区确认卡片（扩展 `CreateWorkspaceCard.tsx` 与 `WorkspaceCreatedSlip`）**：
   - `web/src/pages/ChatAgent/session/interrupts/fromLiveEvent.ts:90-99` 与 `web/src/pages/ChatAgent/session/interrupts/fromHistoryEvent.ts:181-190` 透传 `computer_id` 与 `computer_name`。
   - **待确认态（`pending`）**：在 `web/src/pages/ChatAgent/components/CreateWorkspaceCard.tsx:67-75` 的工作区名称与描述下方展示「目标电脑：`<computer_name>`」；若命中同名去重提示则继续在下方显示黄色警告条；底部提供「创建」「始终允许」「取消」按钮。
   - **已结态与常驻免批条**：已批准折叠摘要显示「已在 `<computer_name>` 创建工作区：`<workspace_name>`」；常驻自动放行直接创建时，`web/src/pages/ChatAgent/utils/preapprovedCards.ts:81-85` 将 `computer_name` 传入 `WorkspaceCreatedSlip`（`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx:495-502`），在内联创建回执条中同步显示所在电脑名称。

### 5.2 手机网页端适配

手机浏览器与桌面端共用同一套响应式 `ProposalCard` 组件，针对窄屏视口（320px–430px）做三项排版加固：

1. **按钮栏自动折行**：将 `web/src/pages/ChatAgent/components/ProposalCard.tsx:84` 的 `<div className="pt-3 flex items-center gap-2">` 升级为 `pt-3 flex flex-wrap items-center gap-2`，使「创建 / 始终允许 / 取消」三个按钮在窄屏下自然换行且保持 `px-4 py-2`（高度 ≥ 36px）的独立触控热区。
2. **长名称换行保护**：`computer_name`、`workspace_name` 与同机工作区名称统一加 `break-words min-w-0`，保证窄屏横向排版整洁。
3. **多工作区折叠展示**：当同机 `affected_workspace_names` 超过 5 个时，默认展示前 5 个并提供「展开查看全部 N 个工作区」内联切换按钮，保证移动端首屏即可触达操作按钮。

### 5.3 飞书、Discord 与个人微信渠道交互（纯文本渠道的深链确认闭环）

1. **自动归类为网页专属确认（`web_only_approval`）**：
   - 渠道网关 `integrations/dalton-channel-gateway/src/orchestrator.ts:6229-6233` 对携带 `action_requests` 动作对象的 `interrupt` 统一归类为 `kind = "web_only_approval"`。
2. **推送含动作摘要与直达深链的渠道通知**：
   - 升级 `integrations/dalton-channel-gateway/src/orchestrator.ts:6259-6260,6312-6319` 的 `formatInterruptPrompt`：优先读取 `firstAction.type`，针对 `archive_workspace` 与 `create_workspace` 输出具体中文摘要与对话直达链接 `${webBaseUrl}/chat/t/${threadId}`：
     - 归档示例：`「归档确认」研究总监请求归档电脑「Cloud-2」（同机受影响工作区：NVDA、TSLA）。请打开网页确认后继续：https://<domain>/chat/t/<thread_id>`
     - 新建示例：`「新建工作区确认」研究总监请求在电脑「MacBook Pro」上新建工作区「AVGO」。请打开网页确认后继续：https://<domain>/chat/t/<thread_id>`
3. **渠道端闭环恢复流程**：
   - 用户在手机飞书、Discord 或微信中点击上述深链，在手机网页或桌面网页打开该对话即可点击卡片上的「确认」或「取消」，研究总监随即继续执行当前轮次。
   - 用户在飞书/Discord 聊天窗直接发送文字（如“确认”）时，`handlePendingInterruptReply`（`integrations/dalton-channel-gateway/src/orchestrator.ts:1989-2009`）保持挂起并重发该网页确认链接，确保批准动作始终来自本人网页点击。
   - 用户在网页端完成点击后，下次回到飞书/Discord 继续发消息时，网关先调 `GET /api/v1/threads/{id}/status`（`integrations/dalton-channel-gateway/src/orchestrator.ts:1966-1983`），检测到线程已进入非中断态后自动清空渠道会话的 `pending_interrupt` 记录，平滑恢复对话。

---

## 6. 分批计划与测试

### 6.1 数据库迁移评估与编号衔接约定

- **本次迁移需求**：**零数据库迁移**。
  - 工作区指定电脑复用既有 `workspaces.computer_id` 列（`src/server/services/workspace_manager.py:195-198`）；
  - 工作区归档复用既有 `computers.status` / `workspaces.status` 状态转换与 LangGraph PostgreSQL 检查点表；
  - 托管自动化模型复用既有 `automations.llm_model` 列、`d042` 的 `reasoning_effort` 列（`src/dalton/migrations/versions/d042_automation_reasoning_effort.py:43-50`）及 `d035` 的 `audit_history` 列（`src/dalton/services/company_automations.py:256`）。
- **后续演进的迁移编号规则**：当前仓库 Dalton 迁移链头节点为 `d048_invite_admin_tier.py`；按全局排期，微信多账号先行占用 `d049`，研究桥拆分自 `d050` 起顺序占用。后续若衍生出需要新增表或列的扩展需求，统一在合入当下检出仓库唯一的 Dalton 迁移头节点并接在其后单线顺延，满足 Expand-Contract 在线平滑升级兼容要求。

### 6.2 分批开发范围

将全部改动拆分为 **3 个独立可合入、可随时现网发布的开发批次**：

| 批次 | 范围与涉及文件 | 交付目标 |
|---|---|---|
| **Batch 1：托管自动化 `llm_model` 与锁内复核** | `src/dalton/tools/director_tools.py`、`src/dalton/autonomy/schedule.py`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2`（按需补一句字段说明） | 1) `dalton_schedule_automation` 开放 `llm_model`（支持新建继承、更新保留、传空/null 清除）；<br>2) `schedule.py` 将 `_safe_model_pref` 移至 `_user_automations_lock_for_write` 锁前执行；<br>3) 新建、更新及行锁内 `_reconcile_locked_upsert` 复用 `check_model`（含自定义服务商父级）、`check_claude_code_hosted`（含新建缺省模型）与工作区原位复核；<br>4) 跨人操作时隔离 `task_owner` 私有自定义模型列表，按 `exc.field` 精准返回错误码。 |
| **Batch 2：指定电脑新建工作区与目标电脑确认展示** | `src/tools/secretary/chief_of_staff.py`、`src/tools/secretary/tools.py`、`src/tools/secretary/approvals.py`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/pages/ChatAgent/components/{CreateWorkspaceCard,ProposalCard}.tsx`、`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx`、`web/src/pages/ChatAgent/session/interrupts/{fromLiveEvent,fromHistoryEvent}.ts`、`web/src/pages/ChatAgent/utils/preapprovedCards.ts`、`integrations/dalton-channel-gateway/src/orchestrator.ts`、`web/src/locales/{zh-CN,en-US}.json` | 1) `manage_workspaces(action="create")` 新增 `computer` 参数，复用 `computer_reach.pick_computer` 支持电脑名称、UUID 与前缀解析；`list` 多电脑时返回 `computer_id`；<br>2) 弹卡前校验电脑可用性并冻结 `computer_id` / `computer_name`；<br>3) 实时流、历史回放卡片与自动放行回执条显示目标电脑，完成移动端按钮换行与渠道深链文案升级。 |
| **Batch 3：通过研究总监归档工作区与整机锁内复核** | `src/tools/secretary/{chief_of_staff,tools,_commands}.py`、`src/server/services/workspace_manager.py`、`src/server/services/computer_manager/_lifecycle.py`、`src/server/dependencies/cli_approval.py`、`src/server/app/{workspaces,computers}.py`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2`、`web/src/pages/ChatAgent/components/SecretaryConfirmCard.tsx`、`web/src/pages/ChatAgent/session/interrupts/{buckets,fromLiveEvent,fromHistoryEvent}.ts`、`web/src/pages/ChatAgent/components/messageList/MessageContentSegments.tsx`、`web/src/locales/{zh-CN,en-US}.json` | 1) `manage_workspaces(action="archive")` 上线，弹卡前校验本人归属、常规工作区、已停止与提供商归档支持度，采集同机全部工作区发起 `hitl_confirm("archive_workspace", ...)`；<br>2) `_archive_machine` 在 `_observed_lock` 锁内复核同机存活工作区集合保持一致；<br>3) 沙箱错误经 `sandbox_unreachable_detail` 与 `sanitize_error_text` 清洗；<br>4) 桌面/手机网页实时流与历史回放渲染整机受影响工作区卡片，归档 HTTP 路由同步补齐 `cli_approval` 兜底。 |

### 6.3 验收方式与测试矩阵

遵循仓库「先做端到端真实调用验证，再补契约回归测试锁死边界」的原则（`AGENTS.md:109`）：

1. **端到端实机验证**：
   - **Batch 1**：在运行后端上分别以主分析师本人、管理员 PM 身份调用 `dalton_schedule_automation(action="upsert")` 与 `PUT /api/v1/dalton/automations/{name}`，验证设置系统模型、设置自定义服务商模型、云端工作区设置 Claude Code 模型（及新建省略 `llm_model` 而账户默认是 Claude Code 模型）被 409/`invalid_llm_model` 拦截、传 `"default"` 清空回 `None`、PM 传入未知模型名时返回列表仅含系统目录模型。
   - **Batch 2**：在用户名下配置 2 台以上电脑，通过研究总监对话传入 `<activity>` 中的电脑名创建工作区，核对实时确认卡片显示目标电脑、批准后 `workspaces.computer_id` 准确绑定、刷新浏览器后历史卡片仍显示目标电脑；在飞书/Discord 触发新建，核对收到含电脑名的深链提示且网页批准后渠道后续对话自动恢复。
   - **Batch 3**：在同一台已停止且支持归档的电脑上放置 2 个工作区，通过研究总监请求归档其中一个，核对确认卡片完整列出同机 2 个工作区；验证刷新页面后历史回放卡片完整保留列表；验证对运行中电脑、`Home` 工作区、同事共享工作区发起归档在弹卡前即返回清晰提示。

2. **单元与前端组件回归测试**：
   - **后端单测（`tests/unit/`）**：
     - `schedule.py`：断言锁外完成偏好预读、`_user_automations_lock_for_write` 上下文内零额外调用 `get_user_preferences`；模拟行锁等待期间并发修改工作区或模型，断言 `_reconcile_locked_upsert` 准确复核拦截及重算 `reasoning_effort`；断言 `actor_user_id != task_owner` 时未知模型的候选提示仅含系统目录模型，零泄露 `task_owner` 的 `custom_models`。
     - `tools.py` & `_lifecycle.py`：断言 `workspaces_create` 复用 `pick_computer` 处理大小写、转义字符、重名电脑与待退役电脑；断言 `_archive_machine` 在锁内检测到新增同机工作区时中止归档并保持 `stopped` 原状；注入含 `https://user:secret@host`、`Bearer tok_xxx` 与 `sk-xxx` 的模拟沙箱异常，断言返回给模型的 `ToolMessage` 与日志输出均完成脱敏。
     - 钩子与代码规范门禁：通过 `bash scripts/dalton/check_hooks.sh` 与 `make lint`。
   - **前端测试（`cd web && pnpm test && pnpm typecheck`）**：
     - `CreateWorkspaceCard` / `SecretaryConfirmCard` / `replayHistory`：覆盖 `create_workspace`（含 `computer_name` 及 `preapproved` 条）与 `archive_workspace`（含整机多工作区列表、超过 5 项折叠、批准/取消/执行失败结算、共享只读模式隐藏按钮）在实时 SSE 与历史回放两条链路下的一致渲染。

---

## 7. 待用户决定的事项

以下 4 项设计细节请确认（每项均给出推荐选项，默认可按推荐项进入开发）：

1. **研究总监工作区操作的默认通道选择**
   - **（推荐）选项 A：保持 `computer_cli_tokens.enabled = false`，采用服务端直连工具 `manage_workspaces` + `hitl_confirm` 内联持久化卡片，同时将锁内校验下沉至共享服务层并为归档路由补齐 `cli_approval` 兜底**。兼顾跨人派活同机令牌隔离、对话历史卡片持久化回放、飞书/Discord 渠道深链通知与覆盖单元扩展参数。
   - **选项 B：开启 `computer_cli_tokens.enabled = true`，改由 `la` CLI + `CliApprovalHost` 全局弹窗承载工作区新建与归档**。需额外改造 `cli_tokens` 按轮次调用者隔离令牌文件、为 `cli_approvals` 增加数据库持久化与历史卡片回放，并打通渠道网关对 Redis 审批事件的订阅推送。

2. **开启「始终允许新建工作区」（`auto_approve_workspace_creation`）时，指定用户自有电脑（SSH / 本地机器）的确认策略**
   - **（推荐）选项 A：当目标电脑属于用户自有电脑（`kind in MACHINE_KINDS`）时扣留自动放行，弹出确认卡片明确展示目标个人电脑；在默认主电脑或托管云端电脑（且名称唯一）时按设置自动创建**。保护用户个人 Mac / SSH 主机工作根目录免受静默建夹影响。
   - **选项 B：目标电脑归属用户本人且名称唯一时，云端电脑与自有 SSH 电脑统一遵循 `auto_approve_workspace_creation` 自动创建**（在内联回执条 `WorkspaceCreatedSlip` 中显示所在电脑名称）。

3. **管理员（PM）跨人修改主分析师受管自动化时，`llm_model` 的可选模型范围**
   - **（推荐）选项 A：跨人操作（`actor_user_id != task_owner`）时，`llm_model` 限定选用系统目录模型（`get_configured_llm_models()`）或清除为 `null`（跟随主分析师默认模型）**。确保绑定端点具备团队级配额与可用性，并收敛跨人试探私有自定义模型名的侧信道；主分析师本人操作时完整支持本人自定义模型与自定义服务商。
   - **选项 B：跨人操作时支持填写主分析师已配置的私有自定义模型，在未知模型提示消息中仅展示系统目录模型列表**。

4. **受管自动化专用工具 `dalton_schedule_automation` 的其余个人任务字段开放范围（对应 `.agy/cos_settings_audit.md:74` 待确认项）**
   - **（推荐）选项 A：本次聚焦开放 `llm_model`（与已支持的 `reasoning_effort` 配套），`thread_strategy` 与 `max_failures` 继续维持受管任务统一策略（每次新建独立线程 `thread_strategy="new"`、连续失败上限 `3`）**。保证财报与覆盖跟踪每次运行上下文干净独立、失败熔断口径全团队一致。
   - **选项 B：同步开放 `max_failures`（1–100）可选参数，将 `thread_strategy` 保持为 `"new"`**。
