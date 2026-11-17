# 自动化与报告投递（Automations）

本文说明 Dalton 自动化任务在个人级与公司级之间的管理机制、系统任务全局视图、报告 profile 配置与 `automation_report` 提交工具、渠道投递流转以及卡片彻底失败后的补充文本告警机制；新增自动化简报类型、配置报告发送群组或排查自动化推送缺失时应阅读本文。

## 1. 自动化分级与版本控制

- **公司级与个人级自动化服务（`src/dalton/services/company_automations.py`）**：统一封装个人（Home / 工作区）与公司共享自动化的增删改停及配置审计历史。每次配置更新支持乐观锁版本号校验（版本冲突抛出 `AutomationVersionConflictError`）与审计快照落库；当智能体通过虚拟文件方式执行两步写入时，`FileCoalesceState` 会将同次保存合并为单一配置版本，避免产生碎片化历史记录。
- **公司自动化迁出批次**：迁移 `d035` 新增 `dalton_company_migration_batches` 与扩展 `automations` 表结构，支撑批量把旧调度迁移为统一格式，并按属主或管理权限追踪。
- **系统任务聚合视图（`src/dalton/autonomy/system_jobs.py`）**：汇聚公司任务、Home 例行任务（`HOME_JOB_TITLES` 含财报日历扫描 `dalton-earnings-calendar`、每日晨会 `dalton-director-daily`、每周复盘 `dalton-director-weekly`、数据地图体检 `dalton-data-health`）、宿主机任务与运维任务（`DEFAULT_OPS_JOBS` 含 `ops-db-backup`、`ops-backend-watchdog`），供 Settings 与监控面板统一读取状态。

## 2. 报告配置（`REPORT_PROFILES`）与白名单目标校验

为确保自动化智能体产出的报告只流向经过管理员配置授权的目标会话，`src/dalton/database/automation_reports.py:parse_report_profile` 对每个自动化的 `metadata` 实施严格校验：

1. **受控 Profile 白名单**：`profile` 字段必须属于 `REPORT_PROFILES` 冻结集合（共 **24** 个已登记业务场景，包括 `msft-tracker` / `meta-tracker` / `amzn-tracker` / `googl-tracker`、`market-digest-am` / `market-digest-pm`、`ai-finance-daily`、`us-weekly-movers`、`x-accounts-daily`、`humanoid-robot-weekly`、`weekly-trend-impact-scan`、`ct-semi-sentiment`、`china-consumer-weekly`、`global-consumer-weekly`、`daily-filing-alert`、`tob-industry-weekly`、`vip-news-alert`、`mark-weekly-newenergy`、`tmeet-weekly-minutes`、`ai-app-weekly`、`richard-earnings`、`richard-coverage-weekly`、`wiki-auto-ingest`、`podcast-weekly`）。
2. **结构化目标列表与通知路由前提**：要求必须声明 `report_targets`（卡片目标）与 `failure_targets`（失败兜底告警目标），可选 `text_targets`（纯文本长报目标），每类目标上限 `MAX_PROFILE_TARGETS = 5`，格式限定为 `feishu:chat:oc_*`、`feishu:user:ou_*`、`discord:user:*` 或 `discord:channel:*`。若仅配置 `failure_targets` 而没有任何有效输出目标，则校验失败（`profile_invalid`）。
   - **仅填写 `metadata.report_profile` 并不能保证投递**：在 `submit_automation_report` 提交阶段（`_authorize_all_targets` → `src/dalton/notify/dispatch.py:evaluate_user_notify_eligibility`）还会强制校验属主的通知偏好：
     1. 属主的通知总开关 `dalton_notify_enabled`（`src/dalton/llm/model_prefs.py:resolve_dalton_notify_enabled`，**默认关闭 `False`**）必须开启，且单任务覆盖 `dalton_notify_overrides` 未将其关闭；
     2. 必须通过 `dalton_notify_routes` 路由与目标匹配检查（个人目标需在 `dalton_notify_targets` 中，群组目标 `feishu:chat:*` / `discord:channel:*` 需属主为 `internal` / `admin` 且未撤回/解绑）；
     3. 对于未命中内置分类的自定义自动化（`classification is None`），必须配置显式键 **`automation:<automation_id>`** 的路由，否则 `evaluate_user_notify_eligibility` 会直接返回 `toggle_off` 并拒绝投递。
   - **配置与排查方法**：上线新自动化前，需在设置页或用户偏好中核对属主的 `dalton_notify_enabled`、`dalton_notify_targets`、`dalton_notify_routes`（含未分类任务的 `automation:<automation_id>`）与 `dalton_notify_overrides`，并检查 `automation_report` 返回错误码（`toggle_off`、`no_authorized_target`、`target_revoked`）及 `dalton_notify_deliveries` 状态。
3. **身份服务端注入与卡片载荷过滤**：`automation_report` 工具参数本身不暴露用户/任务/执行/轮次等身份字段；身份（`user_id`、`workspace_id`、`thread_id`、`run_id`、`turn_origin_automation_id`、`automation_execution_id`）完全由 `src/dalton/tools/automation_report_tools.py:_server_identity` 从服务端 `RunnableConfig.configurable` 读取，并在 `submit_automation_report` 内按 `automations` 与 `automation_executions` 表行锁核验归属。另外，提交的卡片 JSON 会由 `src/dalton/database/automation_reports.py:_FORBIDDEN_KEYS` 递归拦截接收人标识、本地路径与附件字段（`chat_id`、`open_id`、`receive_id`、`union_id`、`user_id`、`email`、`download_url`、`file_path`、`local_path`、`attachment`、`attachments`、`artifact`、`artifacts`、`images`、`path`）。

## 3. `automation_report` 工具与内容限制

智能体在执行周期报告任务末尾，调用专属工具 `automation_report`（`src/dalton/tools/automation_report_tools.py`）提交结构化交付物，支持三类结局：

- **无新增变化静默结束**：素材参数属于 `QUIET_MATERIALS`（`no`、`no_change`、`no-change`），不发出打扰消息。
- **富文本卡片批次**：单批最多 **`MAX_CARDS = 20`** 张卡片、单卡 JSON 不超过 **`MAX_CARD_JSON_BYTES = 262144`**（256 KiB）。
- **Markdown 纯文本长报**：最长 **`MAX_TEXT_CHARS = 40000`** 字符，适合周报/长摘要在群聊或私聊直接可读展开。

提交前，`submit` 会按“先锁 `automations` 行、再锁 `automation_executions` 行”的固定锁序获取行锁，在同一个事务内复用用户偏好与团队分级检查；即便自动化因转入后台续跑而跨越了原始轮次上下文，通过执行行归属校验后仍可完成合法报告提交。

## 4. 投递渠道、总开关与失败告警机制

- **投递执行（`src/dalton/notify/dispatch.py`）**：经由 `dalton_notify_deliveries` 台账统一调度，并调用宿主机 `dalton-channel-gateway`（`127.0.0.1:8962`）投递至飞书群、飞书私聊、Discord 频道或 Discord 私信。投递前再次核查通知总开关、目标未撤回与用户路由权限。
- **确定性失败告警（`automation_report_failure_alert`）**：
  - 当已落库的卡片批次在重试耗尽后达到确定性终端失败状态时（`src/dalton/database/automation_reports.py:card_delivery_is_terminal` 判定：`attempts_exhausted` 或 `status="failed"` 且尝试次数 ≥ 上限，同时排除 `UNCERTAIN_NOTIFY_ERROR_CODES` 等不确定中间态或开关关闭跳过类错误），系统会依据提交时**冻结在原批载荷中的 `failure_target_snapshot`** 插入一条独立补充告警行（`source = FAILURE_ALERT_SOURCE`，正文长度上限 **`MAX_FAILURE_TEXT_CHARS = 2000`**）。
  - 插入告警前会回查原生发送子台账：若实际上卡片已成功发出或尚有未决状态（`in_flight` / `unknown` / `partial` / `gateway_restarted`），则清除陈旧外层失败并取消误发告警。
