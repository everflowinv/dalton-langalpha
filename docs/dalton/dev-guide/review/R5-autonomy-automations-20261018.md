# R5 自主议程与自动任务 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

审查范围：`src/dalton/autonomy/**`、`src/dalton/tools/director_tools.py`、`scripts/dalton/{bootstrap_watch.py, autonomy_week_report.py}`、迁移 d006、上游挂钩（executor / settlement / models / database / setup）、前端（Main.tsx、navItems.ts、useNavItems.ts、Automations/utils/status.ts、types/automation.ts、types/sse.ts、文案）。对照 README（O11、L10/L11/L17）、PK.14–PK.20、PK-UI.1–PK-UI.6、P0F.3。

## 结论

**骨架搭起来了，但自主闭环的几条主干在真实环境下不工作。** 挂钩位置（预检在 pending 阶段、PRECHECK_SKIPPED 三端处理、前端隐藏）做得很干净、与上游兼容；但（1）L17 的 `_count_strike` 豁免完全没有实施，P0F.3 原样存在；（2）六只探针里五只读的 `watch_state` 字段全仓库没有任何写入者，SEC/价格/预期/内部库/财报窗口监测在生产上是死代码，单测用 override 喂数据所以全绿；（3）信号永远不 resolve，研究总监看板每天把同一批旧信号当新候选反复派单；（4）`dalton` router 三个端点因 import/签名不符必 500，而财报链 skill 硬依赖其中那个 PUT；（5）`dalton_agenda_note` 写 `/questions`，schema 里是 `open_questions`，每次都被校验拒绝；（6）议程探针读的 `validation_queue`/`catalysts`/`next_actions` 都不是 schema 字段，到期验证这条核心信号检不出来。P0F.3 认定的问题在此只做精确盘点，不重复论证。

测试：`tests/unit/dalton/test_autonomy_preflight.py` 5 项通过；`./scripts/dalton/check_hooks.sh` 仅因 F2（models/providers 清单）失败，R5 的 6 个挂钩 id 都已登记且在位。

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | `src/server/database/automation_executions.py:33-53`；写入点见下方盘点 | P0F.3/L17 未实施：`_count_strike` 无 `dalton-*` 豁免、无 DALTON-HOOK 标注；schedule.py 10 处 + director_tools.py:398 + bootstrap_watch.py:82 仍在写 `max_failures`；不写字段时上游默认 3 生效（`models/automation.py:499`，`file.py:282` 的 None 落到 AutomationCreate 默认值） | `bootstrap_watch.py` 建出的 `dalton-meta-watch`（max_failures=10）连续失败 10 次（例如 instruction 引用了不存在的 skill 路径，或问题 10 的 sweep 误记失败）→ `status='disabled'`、`disable_reason='max_failures'`，覆盖池静默停摆。最小复现：对 dalton-* 自动任务连点 10 次"立即运行"并让 turn 失败 | 按 P0F.3 的 SQL 改停用条件为 `(%(fuse)s OR (failure_count + 1 >= max_failures AND name NOT LIKE 'dalton-%')) AND status IN (...)`（provider_auth fuse 保留），删 12 处字段写入，补 `tests/unit/dalton/test_automation_no_strike.py`（现不存在） |
| 2 | 高 | `probes/sec.py:24`（`cik`）、`probes/price.py:21`（`latest_quote`）、`probes/consensus.py:21`（`consensus_current`）、`probes/internal.py:18`（`pending_internal_docs`）、`probes/earnings.py:22-23,61`（`next_earnings_date`/`latest_earnings_release_id`） | 五只探针的数据输入字段全仓库无写入者：`dalton_watch_state` 唯一写入方是预检自身（`preflight.py:276`，只回写探针改过的游标），`bootstrap_watch.py` 只建自动任务不初始化 state。SEC/价格/一致预期/内部库/财报窗口监测在生产上恒为空 | bootstrap 后等一份真实 8-K 或 5% 异动 → `dalton-meta-watch` 永远 `skip(no_new_signals)`，分析师永不唤醒；PK.16 验证 1/2/3b 无法复现。单测因传 `submissions_override`/`agenda_doc_override` 而通过 | 每只探针接真实数据源：sec 从 `company.identifiers.cik`（知识层）取 CIK 直连 EDGAR；price 走 `market_data_provider.get_snapshots`；consensus/internal 走 researchdata 桥；earnings 读 agenda `watch.events`（confirmed）。或在知识层保存/公司入职时播种 watch state |
| 3 | 高 | `src/dalton/database/watch.py:193`（`resolve_signals` 无任何调用方）；`board.py:309-324` | 信号生命周期只有"检测→唤醒"，没有消费与过期：verdict 不回写信号，`consumed_at`/`expired_at` 列压根没建（d006 无此列），7 天过期未实现 | 一次 filing 信号唤醒分析师、结论写完后，信号仍是 `resolved_at IS NULL` → 研究总监看板每天把同一条旧信号列为 `event_note` 候选（每公司取前 3 条）→ 每个工作日重复派单重复花钱；PK.20 验收第 4 条（waiting/已处理事项不再产生新 run）无法满足 | `knowledge_append_verdict` 按 `trigger.ref` 匹配 signal_id 并 resolve；board 候选只取"delivered 后超 24h 未消费"的信号；d006 补 `expired_at` 并让预检/看板跳过已过期 |
| 4 | 高 | `src/dalton/app/router.py:313`、`:332`、`:345` | 三个端点必 500：PUT `/automations/{name}` import 不存在的 `schedule_dalton_automation_helper`；GET `/coverage/board` 以 `(user_id, tier, stale_only)` 调无参的 `build_coverage_board`；GET `/autonomy/report` import 不存在的 `build_weekly_autonomy_report`（实际叫 `generate_weekly_autonomy_report`） | `curl -X PUT /api/v1/dalton/automations/dalton-meta-earn-fy26q3-t0` → 500。财报链 skill（`dalton-earnings-calendar/SKILL.md:19,130,150`）和模板都指明用这个 PUT 建/改 tm3/t0/tp1 → 整条财报排期链走不通 | 在 director_tools.py 里补 `schedule_dalton_automation_helper(user_id, workspace_id, name, entry)`（复用工具内部逻辑），或让 router 调 `build_director_tools` 内的同源实现；`build_coverage_board` 加 user 过滤参数或 router 去掉传参；report 改名对齐 |
| 5 | 高 | `src/dalton/tools/director_tools.py:240`（`"path": "/questions"`） | `dalton_agenda_note` 写的路径不是 agenda schema 的字段（根属性是 `open_questions`，`additionalProperties:false`），`apply_update` 的 schema 校验每次拒绝（NOT SAVED） | chief_of_staff 调 `dalton_agenda_note(workspace_id=…, question=…)` → 返回 `ok:false, Validation failed for agenda`；PM 经研究总监调整优先级的机制（PK.19 第 6 点、skill 明令必须走它）100% 失效 | path 改为 `/open_questions`；顺带核对写入的 value 字典与 `#/$defs/question` 的属性一致（目前一致） |
| 6 | 高 | `probes/agenda.py:48`（`catalysts`）、`:76`（`validation_queue`）、`:132`（`next_actions`） | 探针读的三个字段都不在 `agenda.schema.json` 里（schema 是 `watch.events`、`next_validations`、`wake_conditions`）；board.py:282 同样读了 `catalysts` | 议程 `next_validations` 里有一条 `due` 已过 3 天的 V- 项 → watch 预检和 director 看板都看不见 → "到期验证"这条自主议程的核心信号（PK.14 收尾三步的产物）永远不触发唤醒/派单 | probe 与 board 改读 `next_validations`（字段 id/what/due/status 已兼容）与 `wake_conditions`；删掉 `catalysts` 分支或明确兼容映射 |
| 7 | 中 | `probes/director.py:33-41` | 研究总监预检不判断"有没有事做"：daily/weekly 恒发信号必唤醒 | 议程全空、无信号、无待批 → `dalton-director-daily` 照样每个工作日唤醒花钱；PK.19(2)"空闲日 $0"与验证 2（清空后应被跳过）不成立 | 按 PK.19(2) 的五个条件（到期 V-、high 价值开放问题、超 24h 未消费信号、临近 TTL 待批、待确认财报日）实现 director 探针 |
| 8 | 中 | `preflight.py:221-230`；`schedule.py:41,68,250` | （a）`weekly_delta`/`earnings_window`/`custom_dalton` 恒唤醒，delta"无重要变化不推送"（PK.16）未实现；（b）schedule.py 与模板/设计的时间表全面不一致：delta 每周日 21:00（设计/模板为每晚 21:30 ET）、watch 每日 2 次（模板为 `*/30 6-20 * * 1-5` ET）、watch 时区默认 Asia/Shanghai（PK.19 说美股按 America/New_York）、earnings-calendar 周一 08:00（L10 与模板为周六 09:00，`0 9 * * 6`） | 用 bootstrap 建出的覆盖池：财报日历周一而非周六刷新（L10 决定被违背）；delta 一周只跑一次且无论有无变化都花钱 | schedule.py 的 cron/时区对齐模板；delta 走 consensus/sec/internal/price 探针、无变化 skip |
| 9 | 中 | `knowledge/ops.py:365-368`；`probes/agenda.py`（无退避过滤）；`autonomy/backoff.py`（仅探针 2m–2h） | PK.18 退避三处不符规格：（a）问题退避用 `base_hours × 2^(连续 none 次数−1)` 指数增长，规格是"停滞时长一半"（0.5×(now−stalled_since)，下限 base_hours，上限 30 天）——卡 10 天时间隔 5 天 vs 实现 30 天封顶；（b）预检不读 `next_attempt_after`，退避期的问题照常发信号，也无"新证据清零退避直接唤醒"；（c）P0F.3 的执行失败退避（连续失败总时长一半、1h–7d、成功清零）完全没做（未停用这一点合规，公式不合规） | 同一问题反复无进展：实现 8 次后到 30 天上限，规格应为线性 ~12 天；dalton 任务执行失败后不推迟下次运行，只会按原 cron 再失败并数 strike（叠加问题 1） | `_maintain_question_backoff` 改按 stalled_since 时长计算；agenda 探针过滤 `next_attempt_after`，sec/internal 新证据时清除对应键；`backoff.py` 增加 `failure:<automation>` 一类执行失败退避并接进 executor 结算路径 |
| 10 | 中 | `preflight.py:250-271`；`settings.py:80`（`preflight_timeout_s` 定义未用） | 预检没有总超时：探针顺序 await、无 `wait_for`。唯一外呼 sec 探针自身 15s 超时，但 DB 池饥饿等阻塞会让 firing 停在 pending；心跳任务在 transition running 之后才启动，sweep 300s 后按 `INTERRUPTED` 结算并计 strike（`failure_count+1`） | DB 连接池耗尽 6 分钟 → 一次 watch 触发被记为失败；叠加问题 1，几轮即停用 | `decide()` 内用 `asyncio.wait_for(..., settings.autonomy.preflight_timeout_s)` 包裹，超时按 wake 兜底（永不因预检让任务失败） |
| 11 | 中 | `probes/agenda.py:30`、`probes/director.py:18`、`board.py:74-75`（UTC 日期）；`director_tools.py:357-358`（naive `next_run_at`）；`schedule.py:168-169`（naive→UTC） | 时区（决定：Asia/Shanghai）三处不符：到期/窗口判定用 UTC 日期，上海 00:00–08:00 期间晚一天（watch 07:00 CST 那次看不见"今日到期"项，19:00 才看见）；`dalton_schedule_automation` 传 naive ISO 时间按会话时区（UTC）入库，"排到 10-30 早 7:30"实际 15:30 CST 才触发——对比上游文件路径 `file.py:540-542` 会按 automation 时区补 tz | 分析师按 PK.15 样例写 `next_run_at=2026-10-30T07:30:00`（意图上海时间）→ 晚 8 小时触发 | 探针/board 的 today 用 `ZoneInfo(settings.autonomy.timezone)`；工具里 naive 时间 `replace(tzinfo=ZoneInfo(tz))` 再入库；文档写明必须带时区偏移 |
| 12 | 中 | `database/watch.py:78-136`；`preflight.py:207-211, 274-278` | 并发与容错：（a）`upsert_signal` 是 SELECT-then-INSERT，两个并发 firing（如 watch 与 price 重叠）在标记窗口内都得到 `should_wake=True` → 同一信号双重唤醒双份花费；（b）`UNIQUE(workspace_id, probe, signal_key)` 对 NULL workspace_id（Home 信号）不去重，并发可插重复行；（c）`get_watch_state` 读失败后仍会 `upsert_watch_state` 回写近空 state，清掉已有游标/水位 | 手动触发 watch 的同时 price 触发命中同一事件；或 DB 瞬断一次 → SEC 水位丢失（sec 探针对 None 有防洪水兜底，损害有限但模式危险） | 读失败时跳过回写；`upsert_signal`/`mark_signals_woken` 合并为原子 `UPDATE ... WHERE woken_execution_id IS NULL RETURNING` 判定；Home 信号用哨兵 UUID 或部分唯一索引 |
| 13 | 中 | `board.py:404-415` 对 `director_tools.py:128` | 看板贪心上限写的是 `max(allocatable_usd, est)`：allocatable=2.0 时一条 est=3.5 的候选照样入选；而 `dalton_record_plan` 按 `planned > allocatable` 拒绝保存 → 看板推荐的第一条就被记录工具打回，总监被迫返工 | 低额度日：candidates=[3.5 美元任务]，`dalton_record_plan` 返回 `plan_exceeds_budget` | 两侧统一：要么都允许"当日仅一件超限任务"，要么看板严格 `running+est <= allocatable`；另 `cap=0`（未设上限）时 board 假设 remaining=50 会误导（preflight 视为无限、board 视为 50） |
| 14 | 中 | `plugins/dalton/skills/dalton-research-director/SKILL.md:53`；`schedule.py:276` | 派单契约错误：skill 指示 `delegate_to_analyst(ticker=, task_type=, prompt=, auto_approve_handoffs=True)`，实际签名是 `(question, workspace_id, thread_id, report_back)`（`src/tools/secretary/chief_of_staff.py:87-95`）；`dalton-director-daily` 的 instruction 引用不存在的 `delegate_to_workspace` 工具 | 总监晨会第 5 步照 skill 调用 → 工具参数报错/模型自行猜参数；instruction 里的工具名找不到 | skill 与 instruction 改为 `delegate_to_analyst(question=…, workspace_id=…, report_back=True)`；`auto_approve_handoffs` 是用户设置（`approvals.py:20`），从调用参数里删掉 |
| 15 | 中 | `preflight.py:126-139`；`automation_settlement.py:195-200`+`_schedule_action` | 预检预算口径与设计不符：只判 `spent+reserved >= cap`，`min_wake_usd`（settings 有 0.50）和公司级 `max_usd_per_day` 都没用；且 once 任务被 budget 跳过时 `schedule="close"` 直接完结——财报 tm3/t0/tp1 在额度耗尽日触发即**静默永久丢失**（周历下周才可能重建） | 当日额度已花完时 T0 快评触发 → 执行记录 skipped(precheck)，任务变 completed，无任何提醒 | 补 `remaining < min_wake_usd` 判断；once+budget 跳过改为推迟（或至少在看板/周报里列出"因额度跳过而关闭的 once 任务"） |
| 16 | 低 | `plugins/dalton/skills/dalton-agenda/SKILL.md:57` | 称 `max_failures` 是"未知字段"——实际是 `file.py:98` 的合法可编辑字段；P0F.3 要求改写为解释豁免机制（现豁免还不存在） | 分析师照 skill 理解会误以为字段本身非法 | 待问题 1 的挂钩落地后，改写为"不要写 max_failures；dalton-* 不会因连续失败停用，退避由系统按时间处理" |
| 17 | 低 | `probes/agenda.py:39-40`（`except Exception: doc = None` 无日志）；`autonomy/report.py:112-113`、`board.py:400-401`（异常后返回空结构） | 吞错无日志：知识层 DB 故障时 agenda 探针静默当作"无议程"→ 永久 skip；周报/看板查询失败返回全 0 也算成功，验收指标失真 | `dalton_knowledge_versions` 不可用 → 所有 watch 静默停跳，无任何 warning | agenda 探针异常至少 logger.warning 并计入 probe_backoff；report/board 失败时在输出里带 `"partial": true` 与错误说明 |
| 18 | 低 | `d006_autonomy.py`（`dalton_daily_plans` PK 无 user_id、`dalton_preflight_log` 无 user_id/reason_code/duration_ms 列）；`board.py:110-114`、`report.py`、`preflight` 均不按 user 过滤 | 与 PK.16 的表定义和"多用户"前提不符：看板/周报/预检日志全库混读。OSS 单用户可用，托管多用户即数据串号 | 第二个用户建工作区后，总监看板把两家公司的议程混在一起 | 表补 user_id 并过滤；或在此明确写下"单用户部署假设"的决定 |
| 19 | 低 | `preflight.py:163-178` | price 触发走早退分支：不写 `dalton_signals`、signal_key 是 `trigger:<exec8>`（每次唯一）——PK.14 要求结论 `trigger.ref` 填信号 id，价格异动这条无 id 可填；PK.16"move 与 watch 共用 (kind,ref)"的去重约定未实现 | 5% 异动唤醒后，结论的 trigger.ref 只能填自造字符串，周报的信号闭环统计对不上 | price 分支也 upsert 信号（`price_move`/`<交易日>:price`），把 signal_id 放进 `<dalton_wake>` |
| 20 | 低 | `autonomy/report.py`（整文件） | 周报是简化版：没有 PK.20 的每公司 `autonomous_verdicts` 表、每日花费 vs 额度、空闲日（全 skip 日）判定，验收 1/2/3 无法从周报直接核对 | 跑 `autonomy_week_report.py` 只得到全局计数与五词分布 | 按 PK.20 的 SQL 补 per-company 聚合与每日预算对照 |
| 21 | 低 | `schedule.py:155-236` | `build_earnings_window_specs` 无任何调用方（bootstrap 不用、skill 走 REST），且 naive `report_at` 一律按 UTC 解释，与工具路径（问题 11）口径不一致 | 死代码 + 误导后来者 | 要么在财报链实现里真正用起来并对齐时区，要么删除 |

## max_failures 全量盘点（grep 全仓库核实，2026-10-18/20232e5f）

**Dalton 侧写入（P0F.3 要求删除，共 12 处）：**

| 文件 | 行号 | 值 |
|---|---|---|
| `src/dalton/autonomy/schedule.py` | 52, 72, 91, 125（company watch ×4） | 10 |
| `src/dalton/autonomy/schedule.py` | 184, 204, 224（earnings ×3） | 5 |
| `src/dalton/autonomy/schedule.py` | 254, 272, 292（calendar/director ×3） | 10 |
| `src/dalton/tools/director_tools.py` | 398 | 10 |
| `scripts/dalton/bootstrap_watch.py` | 82 | `spec.get("max_failures", 10)` |

注：P0F.3 说 schedule.py 11 处，实测 10 处（每处一行一值）。`docs/dalton/dev-guide/automation-templates/` 下 20 个模板 JSON **均未写** max_failures（仅 README.md:3 说明性提及）；`plugins/dalton*/`、`workflows/` 无。

**上游路径（行为核实，不改）：**
- `src/server/models/automation.py:499-502`：`AutomationCreate.max_failures` 默认 **3**（ge=1, le=100）——不写字段即 3 次停用；`:552` Update 可改。
- `src/server/services/automations/file.py:98`（`_EDITABLE` 含该字段）、`:282`（`_Entry` 默认 None）、`:530`（文件保存路径透传）——agent 写自动任务文件不写该字段 → 落到 Create 默认 3。
- `src/server/services/automations/lifecycle.py:362`、`src/server/database/automation.py:155,184,207`（DB 层默认 3）。
- `src/server/database/automation_executions.py:22-63` `_count_strike`：停用条件 `(%(fuse)s OR failure_count + 1 >= max_failures) AND status IN ('active','executing')`——**无 `name NOT LIKE 'dalton-%'` 豁免、无 `# DALTON-HOOK` 标注**；`fuse`（provider_auth）照常立即停用（这部分正确）。
- `migrations/versions/001_initial_schema.py:441`（列默认 3）；前端 `web/src/pages/Automations/utils/form.ts:65`（表单默认 3）、`MoreOptions.tsx`/`AutomationInspector.tsx`（展示）——上游 UI 原样，非 Dalton 专属。
- 上游测试若干（`test_automation_models.py`、`test_automation_settlement_db.py` 等）锁定默认值/停用行为，未因 Dalton 改动。

**其他计数类门槛排查：** 无 `max_retries`/attempt 上限作为停用条件；`usage_limit` 结局不计 strike（上游，正确）；`backoff.py:13` 的 attempt 序号只用于选等待时长（2/10/30/120 分钟封顶），不停用任何东西；`internal.py:44` 的 `[-200:]` 是 seen-id 内存修剪；`_FILE_NAME_ATTEMPTS=5` 是上游文件名重试。**唯一"次数→停用"的机制就是 `max_failures`/`_count_strike`。**

## 已确认没问题的关键点

- **挂点位置正确（PK.16 §2）**：`automation_executor.py:818-831` 预检在 `transition_execution(pending→running)` 之前、信用闸门/建线程/投递/心跳之前；跳过时除一行执行记录外零副作用。`_Firing.wake_context`（:89）注入 `additional_context` 的 directive（:724-728）与上游提醒同型。
- **PRECHECK_SKIPPED 三端处理正确**：结算策略 `_Policy(status="skipped", from_statuses=("pending",), metric="precheck_skipped", schedule="close")` 无 strike、无 webhook（`automation_settlement.py:194-200`）；`_schedule_action` 对 cron 返回 None（下次运行在 claim 时已排好，不卡）、once 走 close_once、price 按一次性/循环正确 close 或 rearm（:285-297）；`SkipReason` 加 `"precheck"`（`models/automation.py:591`）；`not_a_server_skip` 把 precheck 与 thread_busy/interrupted 同列（`database/automation.py:56-62`）——预检跳过不会顶掉上一条真实运行的展示，也不进 streak 判定。
- **前端 precheck 文案**：`types/automation.ts:33`、`status.ts:308`、zh-CN/en-US 均有 `automation.skippedPrecheck`；Feed（`list_executions`）保留预检跳过记录并显示"预检跳过"，满足 PK-UI.6 第 6 条。
- **零成本成立（跳过路径）**：跳过时不建线程、不过 `enforce_credit_limit`、不启沙箱/心跳；只写 `automation_executions` + `dalton_preflight_log` 两行与几次零模型成本调用。预算检查异常时放行唤醒（fail-open，不会因预算查询故障一直跳过）。
- **预检状态在 Postgres、无进程内状态**：`dalton_watch_state`/`dalton_signals`/`dalton_preflight_log`/`dalton_daily_plans` 全在库；同一条 firing 只有一个 worker 认领（上游行锁）；多 worker 下除问题 12 的毫秒级竞态外不重复唤醒。
- **同事件去重（单任务内）**：`dalton_signals` 唯一约束 + `woken_execution_id` 判定，同一 (workspace, probe, signal_key) 只唤醒一次；watch 与 move 的跨任务共享 ref 约定未实现（问题 19）。
- **探针出错不永久跳过**：探针异常 → `record_probe_backoff`（2m→10m→30m→2h 封顶，纯时间）后继续，其余探针照常；`decide()` 整体 try/except，异常时按 wake 兜底返回 None，不让自动任务因 Dalton 代码进 FAILED。
- **O11 权限边界**：`build_dalton_tools` 按角色分发——analyst 只多拿 `dalton_schedule_automation`（自建 dalton-* 任务，O11 第二项）；chief_of_staff 拿 4 个总监工具 + 上游协调工具（自动交接 = O11 第一项，凭 `auto_approve_handoffs` 用户设置放行，未扩大到建工作区/删除）。没有第三项自主权限。
- **上游行为不受影响**：非 `dalton-` 自动任务在 `decide()` 返回 None 后与上游完全同路径；结算/模型/DB 改动全部是加法且仅 Dalton 写入的值会走到；前端表单默认 3、Manage UI 的 max_failures 编辑原样保留；`check_hooks.sh` 中 R5 六个挂钩 id 均已登记在位（现存 2 个报错属 F2/P0N 的 manifest 问题）。
- **前端隐藏（PK-UI.2/L13）**：`navItems.ts` `requires: 'market'|'dashboard'`、`useNavItems.ts` 读 `VITE_DALTON_MODE` 过滤、`Main.tsx:99` 兜底路由改 `/chat`；路由本身保留（URL 直达 /market、/dashboard 不报错，页面为上游原样，符合"门槛不是路由守卫"的约定）；`useNavItems.test.tsx:75-79` 有 dalton 模式用例；三处导航共用同一列表。
- **日志无令牌泄露**：autonomy 路径日志只含 ticker/execution_id/异常文本；`last_error` 截 240 字符；SEC UA 来自环境变量配置。
- **测试**：`tests/unit/dalton/test_autonomy_preflight.py` 5 项通过（但其覆盖依赖 override 注入，恰好掩盖了问题 2，见结论）。

## 疑似问题（未能确认）

- **once 任务因预算跳过即永久关闭**（问题 15 后半）：PK.16 §2 明确选了 `schedule="close"`，属设计内；但"额度耗尽日恰逢 T0 快评"的现实后果是静默丢失财报链节点，是否可接受待 owner 表态。
- **watch 与 price 并发双唤醒**（问题 12a）：竞态窗口毫秒级，未构造多 worker 实测；在探针数据源接通前实际触发不了。
- **agenda 探针对 waiting_pm 的屏蔽依赖 status 字符串**：`#/$defs/question.status` 无枚举约束，若分析师把受阻问题写成 `blocked` 之类的自造值，PM 等待过滤（PK.20 第 1 条）会漏——取决于实际数据，未验证。
- **executor 内 `from src.dalton.autonomy import preflight` 位于 `decide()` 的 try 之外**：若 dalton 包 import 失败（如依赖损坏），`execute()` 直接抛异常，可能波及非 Dalton 任务；scheduler 对 execute 异常的结算路径未细查，概率低。
- **`docs/dalton/dev-guide/automation-templates/` 里 `t0.json`/`tp1.json` 与 `dalton-<t>-earn-<period>-*.json` 内容重复**：疑似历史残留，未确认是否有引用方。
