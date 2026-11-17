# R2 第一轮问题修复核对（S1–S5、H1–H20）

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：4fd04531（源码=0e68b9a8）

方法说明：全部结论以代码阅读 + 可离线复现的运行结果为依据。跑过的命令与关键输出在各条"依据"中给出；单测均在本机 `uv run pytest` 实跑，broker 测试用 `node --test` 实跑（真实 HTTP 生命周期），开发者当天真实运行证据取自 `var/dalton/`（smoke/、migration/）。环境限制：后端/colima 未运行，涉及真实 LLM/真实网关重启的项标 ❓。

## 结论

- **S1–S5：5/5 通过。**
- **H1–H20：18/20 通过；H4、H13 未修复（同一缺陷在修复提交前后逐字未变，离线复现失败）。**
- H 项中带残余风险的备注：H6 无契约锁定测试；H1/H2 缺真实网关重启证据（测试级证据充分）。
- **建议：H4、H13 修复并通过回归后再进入下一审查阶段**；其余可放行。两次核对发现的键名/字段名错位（budget 键、agenda 字段）同属"读端与写端契约漂移"，建议下一轮加一个跨模块契约测试（service 返回键 ↔ 消费端读取键）统一堵住。

## 严重问题核对（S1–S5）

### S1 主模型 OAuth 整轮放行、现金子智能体不受日额度（R2-1） — ✅

依据：
- 读 `src/dalton/budget/port.py`（全文 305 行）：`acquire()` 接受 `byok` 参数但**没有任何按 byok 提前放行的分支**；docstring 明示 "`byok` never bypasses the daily USD cap"。配额计算全部走 `reconcile_and_summarize_day` + `dalton_cash_usd`（按调用计费）。
- 读 `src/dalton/budget/gate.py`（全文）：`enforce_local_admission(user_id, byok=False)` 中 `byok` 只出现在签名与注释，不参与任何跳过逻辑。
- 读迁移 `src/dalton/migrations/versions/d007_llm_call_journal.py:17-34`：`dalton_cash_usd` 在 `per_call_costs` 数组存在时逐调用求和，`WHERE billing_type <> 'oauth'` —— 只有 OAuth 订阅调用算 $0，byok 与 platform 现金调用全部计入日额度。
- 离线场景（主模型 OAuth + 同轮现金用量 + 小额度 → 下一次现金调用被拒）：`tests/unit/dalton/test_budget_port.py::test_acquire_byok_enforces_daily_cash_cap`（cap=$1.00、已花 $1.50、`byok=True` → `granted is False`、`quota["type"]=="dalton_daily_budget"`）与 `test_enforce_local_admission_denies_when_below_floor_including_byok`（byok=False/True 都断言抛 429）。实跑 `uv run pytest tests/unit/dalton/test_budget_port.py -q` → **12 passed**。测试为 mock 汇总层，但拒绝路径的断言真实存在，且被拒逻辑落在被 mock 的 `reconcile_and_summarize_day` 与 port 之间，与代码阅读互证。
- 旧放行测试已删：`git show b42da6a7:tests/unit/dalton/test_budget_port.py` 含 `test_local_budget_port_byok_grants_immediately`（断言 `granted is True`、ceiling>500），当前版本 `grep byok_grants_immediately` 无结果（exit=1）。

### S2 设置页现金 key 在预算里恒 $0（R2-2） — ✅

依据：
- `d007` 的 `dalton_cash_usd`：有 `per_call_costs` 时取 `SUM(cost) WHERE billing_type <> 'oauth'`，byok 调用按实际费用计；仅当无 per-call 数组才回退 `total_credits/rate`。
- 写端确认：`src/utils/tracking/core.py:366-387` 每次调用都写 `call_cost_record`（含 `billing_type`、实际 `cost`）进 `per_call_costs`；`:432-434` 说明 total_credits 只统计 platform（byok 行 total_credits=0 是预期）。
- 离线复算（实跑）：构造 `billing_type='byok'`、`total_credits=0`、per-call cost=0.4 的行 → 镜像 SQL 语义的 Python 复算得 `cash_usd=0.4`；再直接调 `src/dalton/budget/service.py::compute_provider_breakdown` → `{'xai': {'cash_usd': 0.4}}`、oauth 0.9 进 `notional_usd` 不进现金。汇总读取路径（`fetch_day_reservations` / `fetch_unreserved_usage_today` / `reconcile_reservations_sql`，`src/dalton/database/budget.py:237-415`）全部经 `dalton_cash_usd`。
- 单测 `test_d007_migration_and_provider_breakdown` 已锁定 byok→cash、oauth→notional。

### S3 服务端 LLMService.complete 无准入不记账（R2-3） — ✅

依据：
- `grep -rn "LLMService\|\.complete(" src/dalton`（排除 dalton_complete 定义处）只有两个直调点：
  1. `src/dalton/publish/review.py:301` —— 仅当调用方显式传入 `llm_service` 时走该分支；生产入口 `src/dalton/tools/publish_tools.py` 调 `run_publish/run_check_draft` 时**不传 `llm_service`**（默认 None），走 `dalton_complete`（review.py:273-289）。
  2. `src/dalton/publish/audit.py:336`（`maybe_label_prose_figures_with_llm`）—— `markdown_text` 非空时优先走 `_label_figures_assist` → `assist_json` → `dalton_complete`（audit.py:309-316）；直调分支同样需要外部注入 `llm_service`，当前无调用方传。
- 其余全部收敛：`src/dalton/llm/assist.py` 文件头声明 "Always routes through `dalton_complete`"（:4, :165）；preflight 三分类 `_triage_signals_with_llm` 用 `assist_json`（preflight.py:117）；dalton 内无 `create_llm(` 直用。
- `src/dalton/llm/complete.py`（全文 702 行）五步齐全：`inspect_journal_state`（缓存/冲突/不确定，:426-502）→ 非_oauth 模型 `_check_cash_budget_admission`（:504-541）→ `claim_journal_invocation` 原子认领（:543-593）→ `llm_service.complete`（:610-624，带超时）→ `complete_journal_done` 记账 cash/notional（:637-645）；异常路径 `complete_journal_failed` + `record_provider_event`（:663-685）。
- 额度用完不调模型：`test_budget_port.py::test_dalton_complete_cache_hit_and_budget_rejection` 第 2 段断言 `status=='budget_exhausted'`、错误文案含"已用完"、`mock_llm.complete.assert_not_awaited()`（实跑通过）；review.py:290-296 将其转为 `ReviewResult(status="unavailable", reason="独立审稿调用失败：…")`，即"额度不足"而非调模型。

### S4 编造数字穿过发布闸门（R4-1） — ✅

依据（离线实跑第一轮反例）：
- 场景：provenance body 含"3Q26 业绩：广告收入 512 亿美元……DAP（日活跃用户）32 亿"，草稿写"3Q26 DAP 达 512 百万[^prov:prov-1]"。`audit_draft(draft, provenance_records=prov)` 输出：`1 figures: 0 matched, 0 exempt, 1 unverified`，该 figure `matched=False, reason=source_conflict`（指标不匹配 DAP≠广告收入 + 数值 512e6 vs 512e8 超容差）——**不是 verified**。
- 读 `src/dalton/publish/match.py`（全文 1231 行）：所有数值比较经 `values_within_tolerance`（:210-276）→ `_to_base`（:202-207）先按 `UNIT_BASE` 换算到基准单位再比；期间/指标/单位不匹配分别归类 `period_mismatch`/`unit_mismatch`/`source_conflict`；无任何字符串相等式数字比较（唯一逐字比较是指标名与引文）。
- 相关锁定测试 `test_publish_numbers.py::test_g31_counterexamples_units_footnotes_tables_and_sec_accession`、`test_g31_provenance_rejects_wrong_metric_or_period_r4_3`、`test_g31_realistic_report_false_positive_rate_at_most_2` 均在（本次随套件实跑通过）。

### S5 迁移脚本写入编造数据（R7-1、8、9、11） — ✅

依据：
- 字面量清查（`grep -nE '"[0-9]+(\.[0-9]+)?"|日期串' scripts/dalton/migrate_legacy_knowledge.py` 后逐条看上下文）：剩余数字字面量仅有 CIK（0001326801 等，公开 SEC 标识符，与 builtin 表一致）、财报日 2026-10-28/29（写入 watch events 时带 `date_status: estimated`，见下）、团队姓名/任职时间（公开事实）、"window": "2025-2026"（区间标签）、"≥1%"（阈值参数）。原先编造的 65/35、07-29、max_usd_per_day=15、11-15 均已改为 `None`：`eps_share_pct: None`+note（:985-988）、`issued_at: None`（:1015）、`autonomy max_usd_per_day: None`（:1289）、watch events `date_status: estimated/unknown`（:1188-1194）。
- 修正为追加新版本而非原地改：`remediate_migrated_workspaces`（:1586-1616）对每 doc `apply_update(ops=[{"op":"create",...}], reason="移除迁移脚本编造的数值")`，`remediate_doc_payload`（:1540-1583）把 65/35、07-29、15、-11-15 置 null。
- 抽查迁移产物（实跑脚本核对四家公司）：`var/dalton/migration/Home/dalton/companies/{META,AMZN,GOOGL,MSFT}/data/drivers.json` 全部 `eps_share_pct=null, multiple_share_pct=null, note=迁移时未计算`，整文件不含 65/35；management `issued_at` 样本为 None；company autonomy 为 `{'enabled': True, 'max_usd_per_day': None, ...}`；agenda watch events 日期为 None/`estimated:2026-10-28|29`，next_validations due 同源。

## 高危问题核对（H1–H20）

### H1 broker 防重复付费只在内存（R1-1） — ✅（真实网关重启证据缺，❓）

- `journal.mjs`：`setInvocation` 写内存缓存后 `await enqueueDiskSync()`（原子 tmp+rename 落盘 `invocations.json`，mode 0600）；启动加载时把遗留 `pending` 转 `indeterminate`（:143-151）。
- `invoke.mjs:479-489`：**发送上游前**（`streamSimpleFn` 在 :492）先 `saveInvocation({status:"pending"})`；同 ID 重试遇到 `pending/indeterminate` → 409 `idempotency_indeterminate`（:371-378，文案明言 "refusing automatic resend to prevent double billing"）；reqHash 不符 → 409 conflict（:315-321）。
- 实跑 `node --test test/invoke.test.mjs test/journal.test.mjs` → **6 pass / 0 fail**，含用例 "G5.1 & B8: gateway restart pending -> 409 indeterminate, client disconnect mid-stream finishes & replays 200"（用真实 HTTP fetch + 第二个 broker 实例同 stateDir 模拟重启，断言 409）与 "persists invocations … converts pending to indeterminate on restart"。
- `var/dalton/smoke/latest_broker.json`（2026-10-18T17:23Z，13/13 pass）无重启/断开场景记录 —— 真实网关崩溃重启+同 ID 重试的现场证据缺失，标 ❓（缺：可重启的线上 broker + 一笔在途请求）。

### H2 客户端断开即中止上游（R1-2） — ✅（同上 ❓ 真跑证据）

- `invoke.mjs:240-246`：`onClientClose` 只置 `clientDisconnected=true`，**不 abort**；两个消费循环只在 `abortController.signal.aborted` 时 break（:641），而 abort 仅由超时触发（:395-397，默认 180s、上限 600s —— 后台消费的超时兜底）。断开后所有 `writeSseData` 被 `!clientDisconnected && !res.writableEnded` 保护，循环继续到 `eventStream.result()`，随后 `saveInvocation(completed, responsePayload)` 落盘（流式分支 :740-763）。
- 同 ID 重试拿完整结果：`completed && responsePayload` → 200 重放（SSE 与非流式两形态，:322-370），测试断言 `x-dalton-replayed: 1` 且 `streamCalls` 不变（"Retry after client disconnect must not re-invoke upstream"）与断言 `opts.signal.aborted === false`。

### H3 7 个管理接口调用不存在的函数（R2-4、R5-4） — ✅（含破坏性验证）

- 实跑 `uv run pytest tests/unit/dalton/test_router_wiring.py -q` → **26 passed**（26 条路由无 500；`test_gatekeeper_catches_broken_service_symbol` 自证删函数必被捕获；`test_workspace_ownership_and_pm_guards` 验证非属主 403）。
- 破坏性验证（唯一改代码处，已恢复）：把 `src/dalton/app/router.py` 的 `get_budget_settings` 导入改名 → 实跑守门测试 **1 failed**（`GET /api/v1/dalton/budget returned 500`，断言消息完整）→ `git checkout -- src/dalton/app/router.py` 恢复 → 重跑 **26 passed**，`git status --porcelain` 为空。
- knowledge 接口属主校验：router.py:87-105 `_verify_workspace_access`，六个 knowledge 路由全部调用（:254,279,301,328,356,377,423）；测试断言 intruder 403。

### H4 预算读取漏传 user_id、读错键名（R2-5） — ❌（preflight/board 未修，report 已修）

- **键名错位仍在**：`get_budget_today`（`src/dalton/budget/service.py:457-467`，即 `reconcile_and_summarize_day` 输出）返回键为 `cap_usd / spent_usd / open_reserved_usd`；而
  - `src/dalton/autonomy/preflight.py:354-356` 读 `daily_cap_usd / spent_today_usd / reserved_usd`；
  - `src/dalton/autonomy/board.py:88-90` 读同样三个错键，且 `:96-98` 用错键结果**覆盖** `budget_raw` 里的真实 `cap_usd/spent_usd`，`:91` 使 remaining 恒显示回退值 50.0。
  - `git show b42da6a7` 证明修复前后这两处逐字未变（R2-5 原缺陷仍在）。
- 离线复现（实跑）：patch `get_budget_today` 返回 `{cap_usd:10.0, spent_usd:9.95, open_reserved_usd:0.5}` 后调 `_check_budget_available()` → `ok=True, reason=None`（应为 budget_exhausted）；cap=0（未配置）也放行。**"额度用完时预检跳过"不成立**；看板预算块恒 0/50。
- 已修部分：`src/dalton/autonomy/report.py:123-127` 传 `user_id`、直接用原始 dict、异常打 warning；`src/dalton/tools/director_tools.py:32-33` 两套键名都兼容。preflight `_check_budget_available()` 调 `get_budget_today()` 未传 user_id（OSS 单用户下影响有限）；异常分支 `logger.debug` 而非 warning（:360）。
- 定级：高（核心自治路径常见情况失效；主现金闸门在 dalton_complete/准入层仍有效，故不升严重）。

### H5 审稿人恒取第一项（R4-2） — ✅

- `src/dalton/publish/review.py:135-174` `pick_reviewer_model`：先经 `role_policy.select_independent_reviewer`，回退循环**跳过同家族与 unknown**；`run_review:246-267` 双保险：作者模型 None/unknown → `unavailable`（拒绝发布），选出的模型与作者同家族 → `unavailable`。
- 离线实跑：author=`gpt-6-astra-oauth`/`ocb-openai-gpt-5.4`/`gpt-6.1-sol-oauth`（openai-gpt 家族，候选表首位是 gpt-6.1-sol-oauth）→ 全部选中 `gemini-4-argon-high-oauth`（跳过第一项）；author=`claude-opus-5-5-oauth` → `gpt-6.1-sol-oauth`；None/`mystery-model-xyz` → None（拒绝）。`family_of` 委托 role_policy（按底层模型判家族，L20 一致）。
- 锁定测试 `test_publish_numbers.py:244-259, 554-559`（同家族唯一候选返回 None 等）。

### H6 审批卡前后端键名不一致（R4-3） — ✅（无契约测试，遗留风险）

- 服务端 `src/dalton/publish/service.py:563-603` 发出的 `args`：`request_id/ticker/title/kind/draft_path/published_path/figure_summary{verified,contradicted,unverified,uncertain,exempt,uncertain_items}/uncertain_items/reviewer{model,family,verdict,summary,points}/updates_thesis/thesis_version/audit_summary/reviewer_summary` + 中文回退键。
- 前端 `web/src/pages/ChatAgent/components/dalton/PublishApprovalCard.tsx:55-91` 逐键读取（含中文回退），`ToolApprovalCard.tsx:61-62` 按 `tool==='publish_report'` 路由。
- 卡片可见五类计数（verified/冲突/未核实/未能判断/豁免，:73-75）与"未能判断—交由 PM 决定"逐条列表（line/raw/metric/period/原文/原因，:158-186）。
- 遗留：全仓未找到锁定该契约的前端组件测试或后端 payload 断言（`grep PublishApprovalCard|figure_summary` 于 `*.test.*` 无命中）——键名再漂移无测试拦截。

### H7 表头单位表格误判（R4-4） — ✅

- 离线实跑第一轮反例：草稿表格 `| 期间 | 广告收入（亿美元） |` + 单元格 `512[^prov:...]`，来源 body"广告收入 512 亿美元" → `audit_draft` 输出 `1 figures: 1 matched, 0 exempt, 0 unverified`；提取的 figure `unit=USD_bn, scale=1e8, metric=广告收入`（表头单位继承生效）。
- `test_publish_numbers.py::test_g31_counterexamples…`（:344-369）同场景已锁定。

### H8 有发布成果的工作区可被删除（R4-5） — ✅

- `src/server/app/workspaces.py` delete_workspace（约 :847-854）：`DALTON-HOOK(workspace-delete-guard)` 调 `has_published_reports` → 409 "该工作区含已发布研报，禁止删除；请归档。"；`src/dalton/database/publications.py:422-431` 实现（EXISTS dalton_publications）。
- 前端 `workspaceActions.tsx` + `DeleteConfirmModal.tsx`：删除需确认弹窗（"This action cannot be undone. All data … permanently deleted."），失败时服务端错误（含 409 详情）显示在弹窗内（:67-73, :171-173）。

### H9 连续失败 N 次停用（R5-1） — ✅

- `src/server/database/automation_executions.py:22-71` `_count_strike`（DALTON-HOOK(no-strike)）：禁用条件 = `fuse OR (达到 max_failures AND name NOT LIKE 'dalton-%')` —— dalton-* 永不因连败停用；失败退避 `next_run_at = max(now, now + 3600<<min(failure_count,8))`（1h 起、7d 封顶，**按时间推迟**）；`disable_reason` 区分 provider_auth/max_failures。
- 凭据失效立即停用：`src/server/services/automation_settlement.py:138-144` `Outcome.KEY_REJECTED → strike="fuse"`。
- 非 Dalton 行为不变 + fuse 立即停用均由 `test_automation_no_strike.py` 三用例锁定，实跑 **4 passed**。

### H10 五个探针输入字段没人写入（R5-2） — ✅

- 价格/一致预期探针已删：`git diff b42da6a7..0e68b9a8 -- src/dalton/autonomy/probes/` 显示 `price.py`（-55）`consensus.py`（-57）删除；目录现状无此二文件。
- 剩余四个公司探针的数据来源（preflight.py:465-504）：
  - `sec`：CIK 解析链 watch_state → company doc `identifiers.cik`（sec.py:96-109）→ `BUILTIN_TICKER_CIKS`/coverage.yaml → SEC 在线 JSON；水位线 `last_sec_accession` 存 watch_state。
  - `earnings`：`next_earnings_date` ← watch_state 或 agenda 的 catalysts/watch.events（earnings.py:24-28）。
  - `internal`：`dalton_internal_documents` 表 / state 内 feed，水位线 last_internal_doc_id/ts（internal.py:13-70）。
  - `agenda`：agenda 文档（见 H13 —— 该探针字段错位）。
- CIK 写入：公司初始化 `plugins/dalton/skills/dalton-init-company/scripts/init_company.py:165-166` 写 `"cik"`+`"identifiers":{"cik":…}`；四家已迁移公司回填核对（实跑）：META/AMZN/GOOGL/MSFT 的 `data/company.json` 均有 `identifiers.cik`（且 BUILTIN_TICKER_CIKS 亦覆盖四家）。

### H11 信号永不标已处理（R5-3） — ✅

- 记录结论时标已处理：`src/dalton/tools/knowledge_tools.py:226-231` `knowledge_append_verdict` 成功后调 `resolve_verdict_signals`（按 verdict refs / execution_id 解析，:116-141）；另有 preflight 三分类跳过即 resolve（preflight.py:619-630）。
- 超 7 天过期：preflight.py:429-434 每次 preflight `expire_old_signals(max_age_days=7)`；d007 加 `expired_at` 列。
- 看板不再列两类：`src/dalton/autonomy/board.py:183-191` 查询条件 `resolved_at IS NULL AND expired_at IS NULL`。

### H12 dalton_agenda_note 写错路径（R5-5） — ✅

- `src/dalton/tools/director_tools.py:241-244`：op 为 `append_item`，`"path": "/open_questions"`；agenda schema（`src/dalton/knowledge/schemas/agenda.schema.json:10`）确有 `open_questions`。（按清单范围，"字段名写错由 LLM 自动修复"机制留给后续审计。）

### H13 议程探针读字段不在结构里（R5-6） — ❌（watch.events 已接，next_validations/wake_conditions 未接）

- schema 的议程结构只有：`open_questions / next_validations / watch{events,…} / wake_conditions / pm_handoffs…`（agenda.schema.json，`additionalProperties:false`）。
- `src/dalton/autonomy/probes/agenda.py` 实读：`catalysts`(schema 外，恒空，与 watch.events 合读尚可) 、**`validation_queue`(:76，schema 外)**、`open_questions`(:96 ✓)、`next_actions`(:132，schema 外)；`next_validations` 与 `wake_conditions` **一次都没读**。R5-6 点名要改的 `next_validations` 未改。
- 离线实跑（真实迁移议程 GOOGL agenda.json + 注入到期验证项）：
  - `next_validations` 里 due=2026-10-15、status=pending 的项 → **0 signals，未检出**；
  - 同一内容放在 schema 外的 `validation_queue` → 检出 `validation:V-001:2026-10-15 (P0)`。
  - 真实 GOOGL 议程（V-001 due 2026-10-28）今天 0 信号 —— 到期日将静默漏报。
- 定级：高（P0 级"支柱验证到期"唤醒在真实数据上永不触发）。

### H14 Antigravity 思考签名跨轮累积（R6-1） — ✅

- `src/dalton/llm/providers/antigravity/client.py`：`_stream`(:606-607) 与 `_astream`(:622-623) 每次请求先 `self._signatures = []`（R6-1 注释）；本次响应捕获的签名只附加到本响应最终消息（:578-579），下一轮 payload 按消息 additional_kwargs 重放（:216-229），不跨轮叠加。
- 测试：`test_native_overlay.py::test_antigravity_signatures_cleared_per_turn_and_reasoning_not_doubled`（第 1 轮带 sig-turn-1 → 消息携带；同实例第 2 轮无签名 → `_THOUGHT_SIGNATURES_KEY not in msg2`），实跑 **11 passed**。注：该测试为 2 轮（1 轮签名），未覆盖 ≥3 轮工具调用链，但累积缺陷正是"轮 2 保留轮 1"这一形态，已锁定。

### H15 研究总监 skill 写死次数上限（R7-2） — ✅

- 实跑 `grep -rnE "最多委派|每日最多|max_delegations" plugins/dalton src/dalton`（限 .md/.py/.yaml）→ **零命中**。预算消费处 `director_tools.py:32-33` 按剩余额度动态判断。

### H16 skill 残留 OpenClaw 工具名（R7-3） — ✅

- 实跑 `uv run python scripts/dalton/check_skill_refs.py` → `Scanned 22 skills: ghost_hits=0, broken_refs=0, header_notes=0`。
- 人工抽查 3 个 skill（dalton-research-director / dalton-earnings-flash / dalton-publish）代码块内的工具调用行：无 x_search/firecrawl/extract_facts/verify_numbers/read_filing_section/get_filing_metadata 等。剩余 "openclaw" 字样仅两处：`dalton-init-company/templates/sources_README.md`（描述 `sources/legacy/openclaw/` 迁移目录，合法路径名）与 `dalton-conventions/references/SOURCES.md`（溯源表指向历史 `~/.openclaw/...` 原始出处，非工具调用）。

### H17–H19 财务镜头汇率取错 + 两条命令跑不通（R7 系列） — ✅（联网取价路径 ❓）

- 写死汇率表已删：`anchors.py` 头部第 3 条即 "No hardcoded fallback FX rate table"；全目录 grep 无 FX 常量表（仅 USD=1.0 恒等）。
- 离线报错退出而非旧汇率（实跑）：`anchors.py --ticker BABA --period-end 2026-06-30 --financial-currency CNY --json`（无 --fx-rate）→ `ERROR: Non-USD currency 'CNY' requires explicit --fx-rate …`，**exit=2**。
- 0700.HK（实跑）：`--ticker 0700.HK` → `ERROR: Non-US ticker '0700.HK' (market=HK) requires explicit --financial-currency …`，exit=2（配合 SKILL.md "港股/A 股深度支持在 P6 提供，不硬算"）。
- 汇率跟随列报币种（实跑）：0700.HK + `--financial-currency CNY --fx-rate 0.1408 --listing-fx-rate 0.1282 --price-local 92.5` → `fx.pair=CNYUSD=X`（不是 HKDUSD）、listing_fx=HKDUSD=X、`price_usd=11.8585`(=92.5×0.1282)、mcap=109098.2 USD mn —— R7 原错（CNY 报表用 HKD 汇率、本地价未换算）均已修正。
- 命令可跑（实跑，按 SKILL.md 原样参数形状）：BABA 离线锚点（market_cap_usd_mn=227000，source=explicit）；`netcash.py --cash … --fx-rate 0.1408 --json` 正常输出；`return_score.py --ttm-div … --adjust "+0.5:持续回购"` 正常输出；`earnings_quality.py --ni … --json` 正常输出。
- 单测 `uv run pytest tests/unit/dalton/test_financial_lenses.py -q` → **41 passed**。真实 yfinance 汇率（--allow-network）路径未跑（无网络目标），标 ❓。

### H20 冒烟 B8 恒警告（P0F.6） — ✅

- 读 `var/dalton/smoke/latest_broker.json`（generated_at 2026-10-18T17:23:00Z，summary `{total:13, pass:13, warn:0, fail:0}`，blocking_failures=[]）：
  B8 记录 `status:"pass", blocking:false`，evidence `{low_reasoning:"low", high_reasoning:"high", dalton_meta_low:"low", dalton_meta_high:"high"}` —— low/high 两次回显不同，读的正是 `dalton_meta.reasoning_effort`。无警告。

## 新发现的问题

| # | 严重程度 | 文件:行号 | 问题 | 建议 |
|---|---|---|---|---|
| N1 | 高 | src/dalton/autonomy/preflight.py:354-357；src/dalton/autonomy/board.py:88-98 | H4 残留：预算键名错位（读 `daily_cap_usd/spent_today_usd/reserved_usd`，实际 `cap_usd/spent_usd/open_reserved_usd`），预检"额度用完跳过"死代码；看板预算块被 0 值覆盖、remaining 恒回退 50.0 | 改读实际键名；补一条用真实 summary dict 断言"耗尽→skip"的单测 |
| N2 | 高 | src/dalton/autonomy/probes/agenda.py:76（另 :48,:132） | H13 残留：验证到期读 schema 外的 `validation_queue`，`next_validations`/`wake_conditions` 未读，真实议程的到期验证（P0）永不唤醒 | 改读 `next_validations`；顺带删 `catalysts`/`next_actions` 死分支或入 schema；用迁移议程做离线回归 |
| N3 | 中 | src/dalton/publish/review.py:299-316；src/dalton/publish/audit.py:336-343 | 保留 `llm_service.complete` 直调分支：一旦未来调用方传入 llm_service 即绕过预算准入与 journal 记账（当前无调用方传，属潜伏） | 删除注入分支或在其内部改走 dalton_complete |
| N4 | 低 | web/src/pages/ChatAgent/components/dalton/PublishApprovalCard.tsx（全件）；src/dalton/publish/service.py:563-603 | H6 契约无测试锁定：审批卡字段键名再漂移无拦截（本轮靠人工比对） | 加一个前端组件渲染测试或后端 payload 键断言 |
| N5 | 低 | src/dalton/tools/director_tools.py:212-252 | `dalton_agenda_note` docstring 的 urgency 枚举（overdue/today/this_week/this_quarter/later）与 agenda schema 枚举（now/this_week/this_quarter/someday）不一致；传 docstring 里的合法值会过不了 schema 校验（依赖自动修复/失败回退） | 对齐 docstring 与 schema，或在工具内做枚举映射 |
| N6 | 低 | scripts/dalton/migrate_legacy_knowledge.py:1048-1061 | `capital_allocation` 模板默认写"维持季度常规分红"到全部四家，对 AMZN（不派息）为定性错误（非数字编造，R7 未列） | 迁移时置 null/按公司来源写 |
| N7 | 低 | src/dalton/app/router.py:87-105 | `_verify_workspace_access` 仅在 ws 存在且 owner 非空时不匹配才 403；ws 查不到时放行给下游（通常 404），属宽松默认 | ws 不存在直接 404 |

## 疑似（未能确认）

- H1/H2 的**真实网关**崩溃重启 + 同 ID 重试、真机断开续传：单测/集测（真实 HTTP + 真实磁盘 stateDir）已覆盖同型场景，但 `var/dalton/smoke/` 无该场景记录；需可重启的线上 broker 才能闭环。
- H17–H19 的 `--allow-network` 实时汇率（yfinance `CNYUSD=X`）与真实 yfinance 行情/股本获取：离线环境无法触网验证，仅核了显式参数路径与报错路径。
- S1 的端到端整轮行为（OAuth 主模型 + 真实现金子智能体在同轮跑）：需后端 + 真实模型；本轮以代码路径 + mock 层单测 + SQL 函数语义复算代替。
- N5 中 `apply_update` 是否服务端补齐 question 必填字段（id/owner/attempts/created_at）：未逐一验证 schema 校验的默认值注入，标记待查。
