# R2 本地日预算与管理接口 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

审查范围：`src/dalton/budget/**`、`src/dalton/database/budget.py`、迁移 d001/d002 与 Dalton 独立 Alembic 链、上游挂钩（`credit_gate_port.py`、`usage_limits.py`、`ptc_run.py`、`automation_settlement.py`）、`src/dalton/settings.py`、`src/dalton/dalton.yaml`、`src/dalton/app/router.py` 全部接口、`web/src/pages/Settings/panels/dalton/*`。对照 `P0-platform.md` §P0.8、`P0N-native-models.md` §0.2/§0.3、`P0F-acceptance-fixes.md`、README（L4/L19）。

测试：`uv run pytest tests/unit/dalton/`（72 通过）、`tests/unit/server/dependencies/test_usage_limits.py`（93 通过）。上海时区午夜边界用 `_local_day_bounds` 离线脚本验证。

## 结论

一句话：**不能进入真实运行验收。预算核心（预留/结算/并发/日界/精度）实现正确且与 P0.8 设计一致，但 P0N.6 的两个已知漏洞原样存在（OAuth 主模型整轮放行 + Settings-BYOK 现金 key 完全不可见），`LLMService.complete` 花钱完全无预算且不入账（P0N.7 未落地），另有 router.py 7 个管理接口调用不存在的函数直接 500、autonomy 三处预算读取全部调错导致预算守卫永不生效。修完 P0N.6/P0N.7 与下表 4-5 号问题后复核。**

## ⚠️ P0N.6 核实结论（单独成节）

**P0N.6 属实，代码提交 20232e5f 上原样存在，且默认配置（lead = `claude-opus-5-5-oauth`）下即处于触发状态。** 完整路径：

入口链（主对话）：
1. `src/server/app/threads/messaging.py:541` — `is_byok = config.credential_source in (CredentialSource.OAUTH, CredentialSource.BYOK)`，**OAuth 被算作 own-key**；`:545` `enforce_credit_limit(user_id, byok=is_byok)`。
2. `src/server/dependencies/usage_limits.py:295-298`（DALTON-HOOK）转入 `enforce_local_admission`。
3. `src/dalton/budget/gate.py:64` — `if byok or not user_id: return`：**OAuth 主模型整轮免准入检查**，即使日额度已耗尽、且本轮将派出现金子智能体。
4. 运行时闸门：`src/server/handlers/chat/ptc_run.py:379` `own_key = is_own_key_turn(config)`（`src/server/services/llm/clients.py:62-77`，OAUTH/BYOK 均 True）→ `:492-499` `build_run_credit_gate(..., is_byok=own_key)` → `src/server/services/credit_gate_port.py:415-418`（DALTON-HOOK）→ `src/dalton/budget/gate.py:41-54` 构造 `CreditLease(is_byok=True)`。
5. 每次模型边界：`src/ptc_agent/agent/middleware/credit_gate.py:301-309` 把 `self.is_byok` 传给 `port.acquire`。
6. **放行点：`src/dalton/budget/port.py:69-76`** — `if byok: return LeaseVerdict(granted=True, ceiling_credits=spent+1_000_000, ...)`：不查库、不建预留、不设上限。子智能体经 `spawn_child`（`credit_gate.py:455-482`）与后台任务（`background_subagent/run_executor.py:148-156`）**共用同一条 lease**，所以现金子智能体（grok-4.7 等平台计费调用，`tracker.platform_usd_total()` 里明明有钱）同样不受任何日额度约束。
7. 该行为被单测锁定为预期：`tests/unit/dalton/test_budget_port.py:48-53` `test_local_budget_port_byok_grants_immediately`。

记账后果（不是执行后果）：被绕过轮次的 `conversation_usages` 行没有对应预留，`fetch_unreserved_usage_today`（`src/dalton/database/budget.py:324-348`）会在事后把它计入当天 `spent_usd`，于是**下一轮现金主模型的准入会被拒**——但每一轮 OAuth 主模型继续绕过，单轮内可无限超支（$1,000,000 credits 天花板形同虚设）。

第二子项同样属实：Settings 里按 BYOK 配的现金 key（如 xAI）在 `src/llms/llm.py:551-563` 被标 `billing_type="byok"`，`src/server/services/persistence/usage.py:146-148` 只把 `platform_cost` 计入 `total_credits`，于是这些**真实现金花费在预算里永远是 $0**——准入不拦（byok 跳过）、日汇总也不计（total_credits=0），账目本身错，不只是执行缺口。

P0N.6 之外的其他绕过路径（逐一排查）：
- **workflow 子智能体**：`workflows/*/workflow.js` 的 `agent()` 经 `background_subagent/workflow/driver.py:477-486` 的标准 dispatcher 走同一 `_child_credit_lane` → 与父轮同 lease → 有闸门（但同受 P0N.6 影响）。✅（除 P0N.6 外无额外绕过）
- **自动任务**：`src/server/services/automation_executor.py:880-881/795-796` 有准入（`enforce_credit_limit`），轮次照常建 gate。⚠️ 变体：`_credentials`（`:241-252`）按**用户级** `has_byok or has_oauth` 传 byok——用户只要存过任意 OAuth token，自动任务准入即被跳过（即使该轮实际用现金模型）；运行时闸门仍按该轮解析出的 credential 生效。属 P0N.6 同族。
- **服务端 `LLMService.complete`**：完全绕过（见问题 3）。
- 恢复/重连轮次走同一 POST 路径重新准入（`credit_gate.py:644-647` 注释所述），无额外绕过。✅

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 严重（P0N.6a，已知） | `src/dalton/budget/port.py:69-76`；`src/dalton/budget/gate.py:64`；`src/server/app/threads/messaging.py:541,545`；`src/server/handlers/chat/ptc_run.py:379,492-499` | OAuth 主模型整轮放行：准入跳过 + lease `is_byok=True` + acquire 无条件 grant（ceiling=spent+1M credits），同轮现金子智能体全部不受日额度约束 | 默认 lead `claude-opus-5-5-oauth` 下每个研究轮：主模型 OAuth、`dalton-evidence`/`research` 等子智能体跑 grok-4.7 现金——日额度 $20 已用完仍照跑，单轮可花掉任意金额；事后仅计入当天 spent，不拦本轮 | 按 P0N.6/L19 实施：按单次调用 `billing_type` 计现金（oauth=0、其余算现金）；去掉整轮 byok 放行，lease 的 byok 只影响 oauth 计费部分；同步改 `test_budget_port.py:48-53` |
| 2 | 严重（P0N.6b，已知） | `src/llms/llm.py:551-563`；`src/server/services/persistence/usage.py:146-148`；`src/dalton/database/budget.py`（结算 SQL 全部用 `total_credits`） | Settings-BYOK 现金 key（billing_type=byok）的 `total_credits=0`：真实现金花费不进日额度也不进任何预算统计 | 用户在 Settings 填 xAI key 跑 grok-4.7 一整天，预算面板 spent=$0.00，额度永远不会被触发 | 现金 key 只放 `.env`（platform 计费）；结算与汇总 SQL 改为按 `per_call_costs[*].billing_type` 区分现金（platform+byok）与名义（oauth）——即 P0N.6 的既定方案 |
| 3 | 严重（P0N.7 未落地，文档已知） | `src/dalton/publish/review.py:241-254`（slot=primary, reasoning=high）；`src/dalton/publish/audit.py:95-103`；另上游 `insight_service.py:195`、`thread_title.py:114`、`memo/metadata.py:297` | `LLMService.complete`（`src/server/services/llm/service.py:54-120`）不写 `conversation_usages`、不建预留、无准入：发布审稿这类单次数美元的调用对预算完全不可见 | 日额度已用满 → 用户点发布 → 独立审稿照跑（每次可多次重试不同审稿模型，`pick_reviewer_model` 轮换），花费无人记账 | 落地 P0N.7（d007 调用日志表，目前 `src/dalton/migrations/versions/` 只到 d006）：complete 前准入检查 + 调用后写入记账，接入日额度汇总 |
| 4 | 高 | `src/dalton/app/router.py:166,186,208,233,313-315,332-336,347` | 7 个接口调用**不存在的函数**，必然 500：knowledge 4 个（`knowledge_service.list_doc_versions`/`get_doc_version`/`diff_doc_versions`/`import_document` 均不存在，实际函数名是 `apply_update`/`import_knowledge_doc`/`diff_versions`）；`PUT /automations/{name}` 的 `schedule_dalton_automation_helper` 不存在；`GET /coverage/board` 传 `user_id/tier/stale_only` 给无参的 `build_coverage_board()`；`GET /autonomy/report` 调 `build_weekly_autonomy_report`（实际叫 `generate_weekly_autonomy_report`） | `curl -X GET .../api/v1/dalton/coverage/board`（登录态）→ TypeError → 500。已用 `uv run python` 导入逐一验证 4 组签名不匹配。另注意：knowledge 版本接口即使修好名字，其底层查询只按 workspace_id 过滤、无 user 归属校验，修的时候必须补 | 对齐函数名并补路由级测试（当前 72 项单测无一覆盖 router）；knowledge 查询加 user/workspace 归属校验 |
| 5 | 高 | `src/dalton/autonomy/preflight.py:126-141`；`src/dalton/autonomy/board.py:78-89`；`src/dalton/autonomy/report.py:116-119` | autonomy 三处调 `get_budget_today()` **漏传必填的 `user_id`**（TypeError 被 `except Exception` 吞掉），且读错键名（`spent_today_usd`/`reserved_usd`，实际是 `spent_usd`/`open_reserved_usd`）：预算耗尽跳过（PK.16 `budget_exhausted`）**永不生效**，且 preflight 异常分支 fail-open；board 读不到时剩余额度默认 $50 | 额度用完的当天：每个 dalton-* 自动任务照常唤醒、走准入被 429 拒（结算为 refused 而非 skipped，白付 sandbox 唤醒）；coverage board 按 $50 可用额度做研究分配 | 传入 user_id（board/report 需先加参数）、改用正确键名；preflight 的异常分支至少 warn 级日志而非 debug 静默 |
| 6 | 中 | `src/dalton/settings.py:137-140`；`src/server/services/credit_gate_port.py:473-476` | `DALTON_BUDGET_GATE` 未设置时整个本地预算**静默关闭**：`verify_local_wiring` 只在开关打开时运行，关闭时无任何启动告警；开关只靠 P0-platform.md 的 .env 清单（部署文档有写，但无防呆） | 运维忘写 `DALTON_BUDGET_GATE=1`（compose 文件不含它）→ 预算完全失效，唯一线索是预算面板数字不动 | 启动时若 `HOST_MODE==oss` 且 `dalton.enabled` 而开关未开，打一条 warning 级"本地预算闸门未启用" |
| 7 | 中 | `src/dalton/budget/port.py:272-294` | `release()` 接受 `generation` 参数但完全不用：与上游 PlatformCreditGatePort 不同，没有世代围栏。交错场景（lane 离开 → 另一 lane rejoin 重开预留 → 旧 teardown 的 release 迟到）会把新预留置为 released，短暂少预留 | 长轮次 + 子智能体跨流存活的重连场景：其他轮次可短暂超额预订；进程若恰好在此窗口崩溃仍按 GREATEST 保守入账，不漏账 | `WHERE run_ref=%s AND state='open' AND generation=%s`（generation 非 None 时），与上游语义对齐 |
| 8 | 低 | `src/dalton/app/router.py:119-141`；`src/dalton/database/budget.py:84-105` | task cap 的 scope_id 不校验归属（任意 workspace/thread UUID 可写入） | 用户 A 给别人的 workspace_id 写 cap 行——无越权效果（查询按 `(user_id, scope_kind, scope_id)` 精确匹配，行永远惰性），仅脏数据 | 写入前校验 scope_id 属于当前用户，或文档注明惰性 |
| 9 | 低 | `src/dalton/settings.py:82`（`vendor_quota_alert_ratio`） | 供应商配额"提醒"无任何消费者：grep 全库仅此一处定义。满足"不拦截"，但提醒功能缺失（PK 设计里的 alert 未实现） | 供应商 429/配额将尽时无提醒 | 实现提醒（preflight 或 board 消费该配置），或先删配置避免假象 |
| 10 | 低 | `src/server/app/setup.py:1326-1327` | dalton router 无条件挂载（仅 DALTON-HOOK 注释）：platform 模式部署也会暴露 `/api/v1/dalton/*`（含 openclaw sync 管理接口，任何登录用户可触发 sync/probe） | 非 Dalton 的 SaaS 部署上任何登录用户可 POST `/openclaw/models/sync`（无 PM 限制，与 knowledge import 的 pm_user_ids 检查不一致） | 挂载前检查 `is_dalton_enabled()`；sync 加 PM 限制 |

## 已确认没问题的关键点

- **检查项 1（fail-closed）✅**：`resolve_user_budget_config`（`service.py:136-184`）无 DB 行且 `default_daily_cap_usd=null` → cap=0、configured=False → 准入必拒（文案"未设置日预算"）；`enforce_local_admission`（`gate.py:71-85`）DB 异常 → 503。运行中 acquire 异常返回 None 沿用上一裁决（`port.py:268-270`），新轮次会被准入拦住——`P0-platform.md:1589` 明示这是设计（准入 fail-closed、运行中沿用上游语义），且额度已耗尽时上一裁决就是 denial，边界可控。
- **检查项 2（未结算预留全额占用）✅**：`fetch_day_reservations`（`database/budget.py:286-321`）对 open/released 一律 `charge_usd = GREATEST(reserved, observed, usage)`；`release()` 只改 state 不减 reserved（`port.py:282-290`）；`spent_usd = settled + open_reserved + expired + unreserved`（`service.py:239`）。
- **检查项 3（过期预留不释放）✅**：reconcile 规则 2/3（`database/budget.py:234-283`）过期按 `GREATEST(reserved, observed, usage)` 保守入账且只升不降；released 无 usage 的预留也按预留额占着当天额度直到日切（保守，符合设计）。
- **检查项 4（结算按实际费用）✅**：规则 1（`database/budget.py:196-231`）在 `conversation_responses.usage_settled_at` 已写、有 usage 行、无 in-progress 子任务时按 `SUM(total_credits)/rate` 精确结算，释放多预留的部分；子智能体费用经 `subagent_usage.py` 落在父 response id 下（`msg_type='task'`），同一 SUM 覆盖，不漏不重。
- **检查项 6（并发）✅**：`reconcile_and_summarize_day` 全程在事务内先取 `pg_advisory_xact_lock(hashtext('dalton_budget:{user}:{day}'))`（`database/budget.py:14-18`），同用户同日所有 acquire/结算串行；预留更新是同事务原子 UPDATE；`run_ref` 有 UNIQUE 约束兜底 ON CONFLICT。无进程内状态（`LOCAL_PORT` 无状态，多 worker 安全）。
- **检查项 7（日期边界）✅**：`_local_day_bounds`（`service.py:37-55`）按用户配置时区（默认 Asia/Shanghai）计算本地日界并换算 UTC；离线脚本验证 23:59/00:01 上海时间正确落到 10-18/10-19；跨午夜运行的预留归属开始日，其 usage 不会被次日 `unreserved` 重复计（NOT EXISTS 按任意日预留排除）；非法时区回退上海。
- **检查项 8（精度）✅**：全程 `Decimal` + SQL `NUMERIC(12,6)`，`Decimal(str(x))` 转换、`quantize(1e-6)`；`remaining_usd = max(0, cap - spent)`（`service.py:241`）不会出现负数；float 只出现在 API 展示边界（与上游 LeaseVerdict 类型一致）。
- **检查项 9（task cap）✅**：与日额度同点检查 `grant = min(chunk, available, task_left)`（`port.py:201-207`），先到先拒；拒绝时区分 `dalton_task_cap` / `dalton_daily_budget` 文案（`port.py:236-253`）；优先级 thread > workspace > default（`database/budget.py:142-175`）。
- **检查项 10（订阅名义成本）✅**：现金额度只含 `total_credits`（仅 platform 计费）；OAuth/订阅调用的名义成本单独算进 `subscription_notional_usd`（`service.py:58-133`，oauth 行直接取 total_cost，未标价模型用 overlay `notional_pricing`），前端明确标注"不计入现金额度"。
- **检查项 11（供应商配额只提醒不拦截）部分✅**：预算代码中无任何按供应商拦截的逻辑（grep 无 429/quota 拦截路径）；"提醒"未实现（问题 9）。
- **检查项 12（无调用次数类限制）✅**：`src/dalton/budget/**` 与 dalton.yaml 无 call count/retry count/子池；退避全部时间基（`DaltonBackoffConfig` base_hours）；`retry_after` 只按到午夜的秒数。
- **检查项 13（迁移链）✅**：`src/dalton/migrations/env.py:42` 独立 `version_table="alembic_version_dalton"`；d001（预算三表 + `ix_dalton_resv_user_day` + 部分索引 `ix_dalton_resv_unsettled` + run_ref UNIQUE）与 d002 齐、`downgrade()` 都可回滚；`docker_entrypoint_dalton.sh:17-19` 在上游迁移后自动执行；入口脚本顺序保证 d004 改上游表时表已存在。
- **检查项 14（接口权限）基本✅**：router.py 全部 21 个接口都要求 `CurrentUserId`（`get_current_user_id`：OSS 单用户或 Bearer JWT，`utils/api.py:71-96`），无匿名可触发的管理接口；修改类接口与上游一致走 Bearer（无 cookie 会话，CSRF 不适用，上游 OAuth 回调另有 state 校验）；budget 读写全部以当前 user_id 为主键/过滤；publications 单条有 `user_id != 当前用户 → 404` 检查。遗留见问题 8/10。
- **上游挂钩最小化 ✅**：`git diff 8e0dce94...HEAD` 显示四个挂钩文件合计 +24 行，全部带 `DALTON-HOOK` 标注（`credit_gate_port.py:415,473`、`usage_limits.py:295`、`ptc_run.py:929`、`automation_settlement.py:92,194`）；`messaging.py`/`create.py`/`insights.py` 零改动（复用上游既有 `enforce_credit_limit` 调用点）。
- **前端面板 ✅**：`DaltonBudgetCard.tsx` 与 `daltonApi.ts` 的字段名/端点与后端完全一致（`spent_usd`/`remaining_usd`/`open_reserved_usd`/`subscription_notional_usd` 等）；客户端校验（日额度 ≥0、task cap >0）与服务端重复；剩余额度直接展示后端 max(0,…) 结果。
- **日志/异常卫生 ✅**：预算路径日志只含 run_ref/用户 id/异常栈，无令牌；`except Exception` 均有日志（唯 autonomy 三处吞错已列为问题 5）；无 `except: pass`。

## 疑似问题（未能确认）

- **运行中 fail-open 的单轮敞口**：准入后 DB 中断，进行中轮次沿用旧裁决继续跑（设计如此，P0-platform.md:1589）。敞口上限 = 单轮花费，无量化测试。需真实运行验收时观察，暂不列为问题。
- **`enforce_local_admission` 对轮内已花金额不设上限的准入语义**：准入只看"剩余 ≥ $0.25"（`admission_floor_usd`）， resumed 轮次若旧预留仍在（占用仍在汇总里），不会重复计入——逻辑正确，但未写单测锁定"恢复轮次不双计"。
- **问题 4 的 knowledge 接口是否有隐藏消费者**：前端 Settings/ChatAgent 目录 grep 未发现调用这些端点的代码，疑为纯死代码 + 拼写漂移；若别处（桌面端、脚本）有调用需一并修。
- **多 worker 下的 advisory lock 等待**：`pg_advisory_xact_lock` 无超时设置，同用户大量并发 acquire 会排队（ acquire 频率由上游 refresher 控制，正常轮次秒级一次，预计无问题）；未在 2+ worker 实测高并发预留竞争（`scripts/multiworker_gate/` 未覆盖预算路径，建议补）。
