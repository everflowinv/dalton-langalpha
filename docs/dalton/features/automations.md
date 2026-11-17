# 自动化与报告投递（Automations）

这页讲清 Dalton 自动化任务在个人级与公司级之间的管理机制、系统任务聚合视图、`REPORT_PROFILES` 白名单校验、`automation_report` 提交工具，以及投递失败告警怎么触发。

## 1. 自动化分级与版本控制

- **公司级与个人级自动化服务（`src/dalton/services/company_automations.py`）**：统一封装个人（Home / 工作区）和公司共享自动化的增删改停及配置审计历史。每次改配置都做乐观锁版本号校验（版本冲突抛 `AutomationVersionConflictError`）并落审计快照。智能体用虚拟文件做两步写入时，`FileCoalesceState` 会把同一次保存合并成一个配置版本，免得留下一堆碎历史。
- **公司自动化迁出批次**：迁移 `d035` 新增了 `dalton_company_migration_batches` 并扩展了 `automations` 表结构，用来把旧调度批量迁成统一格式，并按属主或管理权限追踪。
- **系统任务聚合视图（`src/dalton/autonomy/system_jobs.py`）**：把公司任务、Home 例行任务（`HOME_JOB_TITLES` 含财报日历扫描 `dalton-earnings-calendar`、每日晨会 `dalton-director-daily`、每周复盘 `dalton-director-weekly`、数据地图体检 `dalton-data-health`）、宿主机任务和运维任务（`DEFAULT_OPS_JOBS` 含 `ops-db-backup`、`ops-backend-watchdog`）汇在一起，供 Settings 和监控面板统一读状态。

## 2. 报告配置（`REPORT_PROFILES`）与白名单目标校验

为了让自动化产出的报告只发去管理员授权过的目标会话，`src/dalton/database/automation_reports.py:parse_report_profile` 会对每个自动化的 `metadata` 做严格校验：

1. **受控 Profile 白名单**：`profile` 字段必须落在 `REPORT_PROFILES` 冻结集合里（共 **24** 个已登记业务场景，包括 `msft-tracker`、`meta-tracker`、`amzn-tracker`、`googl-tracker`、`market-digest-am`、`market-digest-pm`、`ai-finance-daily`、`us-weekly-movers`、`x-accounts-daily`、`humanoid-robot-weekly`、`weekly-trend-impact-scan`、`ct-semi-sentiment`、`china-consumer-weekly`、`global-consumer-weekly`、`daily-filing-alert`、`tob-industry-weekly`、`vip-news-alert`、`mark-weekly-newenergy`、`tmeet-weekly-minutes`、`ai-app-weekly`、`richard-earnings`、`richard-coverage-weekly`、`wiki-auto-ingest`、`podcast-weekly`）。
2. **结构化目标列表与通知路由前提**：必须声明 `report_targets`（卡片目标）和 `failure_targets`（失败告警目标），可选 `text_targets`（纯文本长报目标）。每类目标上限 `MAX_PROFILE_TARGETS = 5`，格式限定为 `feishu:chat:oc_*`、`feishu:user:ou_*`、`discord:user:*` 或 `discord:channel:*`。只配了 `failure_targets` 却没有任何有效输出目标时，校验会报 `profile_invalid` 失败。
   - **填了 `metadata.report_profile` 后，还得满足属主通知条件**：在 `submit_automation_report` 提交阶段（`_authorize_all_targets` → `src/dalton/notify/dispatch.py:evaluate_user_notify_eligibility`），系统还会强制查属主的通知偏好：
     1. 属主的通知总开关 `dalton_notify_enabled`（`src/dalton/llm/model_prefs.py:resolve_dalton_notify_enabled`，**默认关闭 `False`**）必须打开，而且单任务覆盖 `dalton_notify_overrides` 没把它关掉；
     2. 必须通过 `dalton_notify_routes` 的路由和目标匹配检查——个人目标要在 `dalton_notify_targets` 里，群组目标 `feishu:chat:*` / `discord:channel:*` 要求属主是 `internal` / `admin` 且没撤回或解绑；
     3. 没命中内置分类的自定义自动化（`classification is None`），必须配好显式键 **`automation:<automation_id>`** 的路由，不然 `evaluate_user_notify_eligibility` 会直接返回 `toggle_off` 拒绝投递。
   - **配置与排查方法**：上线新自动化前，去设置页或用户偏好里核对属主的 `dalton_notify_enabled`、`dalton_notify_targets`、`dalton_notify_routes`（含未分类任务的 `automation:<automation_id>`）和 `dalton_notify_overrides`，再看 `automation_report` 返回错误码（`toggle_off`、`no_authorized_target`、`target_revoked`）以及 `dalton_notify_deliveries` 状态。
3. **身份服务端注入与卡片载荷过滤**：`automation_report` 工具参数本身不暴露用户、任务、执行或轮次身份字段。身份（`user_id`、`workspace_id`、`thread_id`、`run_id`、`turn_origin_automation_id`、`automation_execution_id`）全由 `src/dalton/tools/automation_report_tools.py:_server_identity` 从服务端 `RunnableConfig.configurable` 读取，并在 `submit_automation_report` 里按 `automations` 和 `automation_executions` 表行锁核对归属。提交的卡片 JSON 还会经 `src/dalton/database/automation_reports.py:_FORBIDDEN_KEYS` 递归拦截接收人标识、本地路径和附件字段（`chat_id`、`open_id`、`receive_id`、`union_id`、`user_id`、`email`、`download_url`、`file_path`、`local_path`、`attachment`、`attachments`、`artifact`、`artifacts`、`images`、`path`）。

## 3. `automation_report` 工具与内容限制

智能体跑完周期报告任务后，调专属工具 `automation_report`（`src/dalton/tools/automation_report_tools.py`）提交结构化结果，支持三种结局：

- **无新增变化静默结束**：素材参数落在 `QUIET_MATERIALS`（`no`、`no_change`、`no-change`），不发消息打扰。
- **富文本卡片批次**：单批最多 **`MAX_CARDS = 20`** 张卡片，单卡 JSON 不超过 **`MAX_CARD_JSON_BYTES = 262144`**（256 KiB）。
- **Markdown 纯文本长报**：最长 **`MAX_TEXT_CHARS = 40000`** 字符，适合周报和长摘要在群聊或私聊直接展开阅读。

提交前，`submit` 会按“先锁 `automations` 行、再锁 `automation_executions` 行”的固定顺序拿行锁，在同一个事务里复用用户偏好和团队分级检查。哪怕自动化因为转入后台续跑跨过了原轮次上下文，只要执行行归属核对通过，照样能合法提交报告。

## 4. 投递渠道、总开关与失败告警机制

- **投递执行（`src/dalton/notify/dispatch.py`）**：通过 `dalton_notify_deliveries` 台账统一调度，调宿主机 `dalton-channel-gateway`（`127.0.0.1:8962`）发去飞书群、飞书私聊、Discord 频道或 Discord 私信。发之前还会再查一遍通知总开关、目标未撤回和用户路由权限。
- **确定性失败告警（`automation_report_failure_alert`）**：
  - 已落库的卡片批次在重试耗尽后进了确定性终端失败状态（`src/dalton/database/automation_reports.py:card_delivery_is_terminal` 判定：`attempts_exhausted` 或 `status="failed"` 且尝试次数 ≥ 上限，同时排除 `UNCERTAIN_NOTIFY_ERROR_CODES` 等不确定中间态和开关关闭跳过类错误）时，系统会拿提交时**冻结在原批载荷里的 `failure_target_snapshot`** 插入一条独立补充告警行（`source = FAILURE_ALERT_SOURCE`，正文上限 **`MAX_FAILURE_TEXT_CHARS = 2000`**）。
  - 插告警前会回查原生发送子台账：要是卡片其实已经发出去了，或者还有未决状态（`in_flight` / `unknown` / `partial` / `gateway_restarted`），就会清掉陈旧的外层失败并取消误发告警。
