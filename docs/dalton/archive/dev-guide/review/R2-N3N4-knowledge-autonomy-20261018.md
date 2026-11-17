# R2 N3 知识层 / N4 自主议程 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：4fd04531（源码=0e68b9a8）  本轮 diff：b42da6a7..0e68b9a8

## 结论

N3 知识层**基本通过**：三条硬规则与字段修复的隔离经离线构造验证成立，direct_edit 检测无退化；扣分项是版本记录不保留原始 ops、别名表对旧字段的条目级覆盖不完整（都属低危）。

N4 自主议程**主干仍然不工作**，与第一轮 R5 的结论同构：三个已知问题全部坐实（H4 键名、H13 字段、triage 签名），且 G4 承诺清单里至少六项未实施或未完成——研究总监空闲日恒唤醒、预检 45 秒总超时缺失、once 任务因额度跳过仍直接关闭、内部库探针查一张不存在的表、`dalton_agenda_note` 修复后仍因缺 `owner` 字段 100% 被拒、时区三处仍未按 Asia/Shanghai。骨架类修复（次数门豁免、信号 resolve/过期、CIK 链、日历周六 09:00、路由 500、委派签名）是真实完成的。136 项 dalton 单测全绿——但 triage 的错误签名恰被 `mock(**kwargs)` 吸收，是"单测通过≠能用"的又一例。

离线复现均在本机完成（无 DB/网络/LLM 花费，$0）。

## N3 知识层（逐项）

### N3.1 别名映射（✅，附两处低危备注）

两张静态表（`src/dalton/knowledge/ops.py`）：

**文档级路径别名 `_DOC_PATH_ALIASES`（ops.py:144-177）**

| doc | 别名 → 目标 |
|---|---|
| agenda | questions / open_question → open_questions；validations / **validation_queue** → next_validations；catalysts → watch/events；events → watch/events；next_actions → wake_conditions；handoffs → pm_handoffs |
| thesis | evidence_log / evidences → evidence；thesis_pillars → pillars；summary → one_liner |
| debate | debate_list / items → debates |
| drivers | key_drivers / driver_list → drivers |
| lessons | lesson_list / items → lessons |
| management | guidance → guidance_history；notes → credibility_notes |

**集合条目字段别名 `_COLLECTION_FIELD_ALIASES`（ops.py:179-252）**：pillars(statement/title/description/summary→claim)；evidence(statement/claim/summary/text→fact)；debates(title/topic→variable, claim/statement→question, importance/why→why_it_matters)；bull/bear(statement/claim/text/summary→case)；open_questions(statement/claim/text/title/question_text→question, reason/why_it_matters→why)；next_validations(due_date/date→due, description/title/statement/claim→what, expectation→expected)；events(event_date/due/due_date→date, action/description/what→plan)；lessons(claim/lesson/text/summary→statement, when→applies_when)；drivers(title/metric→name, statement/claim/summary→description)。

另有 difflib 模糊匹配（ops.py:778-780，cutoff 0.78）兜底顶层段，仅在键不在文档且不在别名表时触发，且显式排除 `official`。

**不同义字段会不会映射到一起：不会撞真实字段。** 逐一对照 schema 根属性验证：thesis 根没有自己的 `summary`（只有 one_liner），agenda 根没有 `catalysts`/`validation_queue`/`next_actions`——所有别名目标都是 schema 真实字段，不存在两个真实字段被合并。两处语义拉伸：`next_actions→wake_conditions`、`catalysts→watch/events` 是旧概念到新 schema 的近似迁移（G4.5 认可的修法）。模糊匹配语义盲但触发条件苛刻（键必须不存在），且失败方向是保守的（修不进 schema 就 NOT SAVED，离线 T4 验证）。

**低危缺口：** 条目级别名对旧 catalyst 形状不完整——`title`/`expected_date`/`status`/`importance` 不在 events 映射里，迁移形状的 create 修复后仍过不了 schema（离线复现：`expected_date/status/title not allowed` + `id required`）。保守失败，无数据风险，只是修复不彻底。

### N3.2 修复后的写入（⚠️ 部分满足）

`apply_update` 存的是**修复后**的内容与**修复后**的 ops（service.py:475 `ops=repaired_ops`），版本行的 `warnings` JSONB 列（d003 建列，database/knowledge.py:172 写入）记录 `{"code":"field_repaired","path":...,"message":"repaired field 'X' -> 'Y'"}`——**能看出经过了自动修复、能看出原字段名**；字段修复只改键名不改值，原值随新键保留，原貌可由 warning+内容重建。**但逐字的原始 ops 没有存进版本行**（存的是 repaired_ops），审计无法看到代理当初提交的原文。LLM 修复（service.py:98-150）同样只留 warning。建议 insert_version 增加 `original_ops` 或在 warning 里带原值片段。

### N3.3 direct_edit 检测无退化（✅）

本轮 diff 不含 `knowledge/reconcile.py`（未改动）；`apply_update` Step 1 的"DB 无版本但沙箱已有 data/<doc>.json → 记为 direct_edit v1"逻辑原样（service.py:306-332）；`write_version_files_to_backend` 对 `source==direct_edit 且 edited_path==data_path` 跳过覆写（service.py:216-219）。`tests/unit/dalton` 136 项全绿（含 knowledge/reconcile 用例）。回归确认通过。

### N3.4 字段修复不能绕过三条硬规则（✅，离线构造复核）

反审计已验证"repair 写 official_version 被重验拒绝"，本轮独立复核结论一致，并补查了证据台账：

1. **硬规则错误直接跳过 LLM 修复**：service.py:424-428——errors 含 `append_only_violation`/`official_is_publish_only`/`target_needs_model_cell` 之一时不进 `_repair_doc_schema_with_assist`。
2. **修复后全量重验**：service.py:444-451 对修复后的文档重新跑 `validate_update`（含 I1/I2/I3）。
3. **映射过滤**：service.py:132 丢弃 `v=="official"` 的映射；ops.py:774-779 顶层别名与 difflib 匹配均显式排除 `official`（离线验证 `/officia` 模糊拼写不会被吸到 `/official`）。
4. **离线构造证据**：(a) 通过别名把 `/evidence/E-…/statement` 修成 `/fact` 后，`apply_ops` 的 I1 检查照样抛 `append_only_violation`（T2）；(b) 工具路径写 `official` 报 `official_is_publish_only`（T3）。
5. **证据台账条目不可被修复改动**：对 append-only 数组内条目的任何 set/upsert/remove（含经别名改写后的路径）都被 `_check_set_append_only_violation` 拒绝（ops.py:897-930，guidance_history 的 actual/outcome 回填是唯一白名单，与证据台账无关）。`resolve_signals` 只动 `dalton_signals` 表，不触知识层版本。

残余观察（低）：根级 LLM 映射可以把一个写错名字的根字段改名进**除 official 外**的任意根属性（如 `house_view`），类型不符会被重验拦下——失败方向保守，不构成硬规则绕过。

### N3.5 ops.py 新增 517 行的构成（已逐块列出）

| 块 | 行区间 | 约行数 | 性质 |
|---|---|---|---|
| `_install_nullable_schema_patch` | 19-116 | ~98 | G3.4 去编造字段的 schema 兼容垫片（可空化 + evidence_ids 补充），非新校验规则 |
| `_DOC_PATH_ALIASES` + `_COLLECTION_FIELD_ALIASES` | 144-252 | ~109 | **静态同义词规则表** |
| `_repair_item_dict`/`_repair_doc_collections`/`repair_knowledge_ops` | 675-821 | ~147 | 确定性别名修复引擎（含 difflib 模糊匹配） |
| `_maintain_question_backoff` 及辅助 | 550-664 | ~115 | G4.5 R5-9 的新退避公式（0.5×停滞时长，24h–30d 夹取，新证据清零） |
| `next_item_id`/`_ensure_nested_ids_and_defaults` 扩展 | 285-505 | ~50 | wake_conditions/pm_handoffs/versions 的 ID 与默认值基础设施 |
| `apply_ops` 接入修复 | 947-949 等 | ~10 | 接线 |

**结论：ops.py 的 517 行里没有一行是 LLM 修复接线**——LLM 接线在 service.py 约 55 行（`_repair_doc_schema_with_assist`，签名正确）。新增全部是规则（别名表+退避+schema 垫片）。别名表是有界的、与 fail-closed 重验配对，尚不属"规则越补越多"的失控形态，但 109 行手工同义词表 + difflib 启发式是下一步膨胀的候选点，建议观察。

## N4 自主议程（逐项）

### N4.1 三个已知问题坐实

**H4 预算键名（❌，高危，离线复现）**
- `preflight.py:354-356` 读 `daily_cap_usd`/`spent_today_usd`/`reserved_usd`；`get_budget_today`（budget/service.py:424-442）实际返回 `cap_usd`/`spent_usd`/`remaining_usd`/`open_reserved_usd`。三个 `.get()` 全部落空 → `cap=0` → `if cap > 0` 恒假 → **额度用尽时预检永不跳过**。离线复现：patch `get_budget_today` 返回已耗尽的真形狀（cap_usd=1, spent_usd=1），`_check_budget_available()` 返回 `(True, None)`。且 `preflight.py:353` 调 `get_budget_today()` **仍不传 user_id**（H4 的另一半也没修）。
- `board.py:88-90` 同样三个键 → `cap_usd=0` → `remaining=50.0` 幻影额度（board.py:91 的 else 分支）；连坐 `director_tools.py:131` `dalton_record_plan` 的额度守卫 `if cap_usd > 0 and planned > allocatable` **恒不触发**（R5-13 的两端口径不一致因此被"冻结"在坏的一侧）。
- `report.py:125` 传了 user_id 且原样透传 budget dict（真键名），**已修** ✅。

**H13 议程探针字段（❌，高危，离线复现）**
- agenda.schema.json 根属性为 `open_questions/next_validations/watch/wake_conditions/...`（additionalProperties:false）。`probes/agenda.py:48` 读 `catalysts`、**:76 读 `validation_queue`**、:132 读 `next_actions`——三者都不在 schema 里；`next_validations`/`wake_conditions` **全文件未出现**。
- 离线复现（真实 `var/dalton/migration/.../GOOGL/data/agenda.json`，11 条 next_validations）：原样 → 0 信号；向 `next_validations` 注入 `due=2026-10-15`（已过期 3 天）的到期项 → **仍 0 信号**；只有写入 schema 外的 `validation_queue` 才出信号——而该字段经 `apply_update` 永远写不进去。"验证到期"这条核心信号（G4.1 表的第 3 行、PK.14 收尾产物）依旧检不出来。
- 讽刺的互锁：ops.py 的写侧别名 `validation_queue→next_validations` 会把写错的代理"修"进 next_validations，读侧探针却还在读 validation_queue——自愈写路径也喂不进探针。`board.py:287-289` 同样读 `catalysts`（但并读了 `watch.events` ✅）。

**triage 签名（❌，高危，离线复现）+ 45 秒超时（❌）**
- 真实签名 `assist_json(task, instructions, payload, schema, *, user_id, role, scope_ref, timeout_s, model, llm_service, workspace_id, thread_id)`（assist.py:126-140）。`preflight.py:179-186` 用 `system_prompt=`/`user_input=`/`output_schema=`/`ticker=` 调用 → `TypeError: assist_json() got an unexpected keyword argument 'system_prompt'` → 被 :187-189 的 `except Exception` 吞成 warning，落入 conservative wake fallback。离线实跑确认：`kept=1, reason=triage_fallback_wake`。**生产上 LLM 分诊从未生效**：所有本应分诊的信号（非 2.02 的 8-K、Form 4、13D/G、6-K、DEF 14A、内部文献）一律唤醒、一律花强模型的钱；G4.2 的"零噪音"目标整体落空（副作用：也从未为分诊花过一分钱）。
- 单测 `test_autonomy_preflight.py:275` `async def mock_triage_skip(**kwargs)` 吸收任意 kwargs——错误签名被测试替身掩盖（12 项全绿）。
- 其余三个 assist_json 调用方（audit.py:278、numbers.py:1049、knowledge/service.py:113）签名**全部正确**，只有 preflight 错。
- **45 秒预检总超时缺失**：`dalton.yaml:44`/`settings.py:98` 定义了 `preflight_timeout_s: 45`，但全仓库无任何引用（grep 证实）；`preflight_check`/`decide` 无 `asyncio.wait_for` 包裹（G4.5 R5-10 承诺未实施）。探针异常有 try/except 兜底、探针网络各有 10-15s 超时，但 DB 池饥饿等阻塞仍可让 firing 挂起（第一轮 R5-10 场景）。

### N4.2 分诊类别（✅ 代码正确；❌ LLM 腿实际死了）

`is_direct_wake_signal`（preflight.py:68-106）逐类确认：
- **10-Q/10-K**（及 20-F）：`DIRECT_WAKE_SEC_FORMS`（:33,93）直接唤醒 ✅
- **8-K item 2.02**：:95-100（items 列表或 summary 含 "2.02"）直接唤醒 ✅
- **议程到期**：probe ∈ {agenda, earnings, calendar, director, schedule, price} 一律直接唤醒（:88）✅——但因 H13，"议程到期"实际检不出。
- **财报窗口**：earnings probe 直接唤醒 ✅（窗口数据来自 agenda `watch.events`，见 N4.6）。
- **其余 SEC（8-K 无 2.02、Form 4、13D/G、6-K、DEF 14A）与 internal**：进 `_triage_signals_with_llm` ✅（:91-104 返回 False）。
- **LLM 失败 → 唤醒**：:187-189（异常）、:194-196（ok=False）、:207-209（结构无效）三处保守回退 ✅，decide() 整体再兜一层（:699-708）✅；唤醒不计失败（skip 才走 PRECHECK_SKIPPED 结算）✅。
- **但整条 LLM 腿因签名错误从未执行**（见上），实际行为退化为"全部直接唤醒"。

### N4.3 研究总监空闲日（❌，未实施）

`probes/director.py:33-41`：`director_daily` 无条件返回 `daily:<date>` 信号，`director_weekly` 无条件返回 `weekly:<W>`；**没有任何"有事可做"判断**（到期 V-、高价值开放问题、超 24h 未消费信号、临近 TTL 待批、待确认财报日五条件一个都没实现）。G4.3 第 3 条与 PK.19(2)"空闲日 $0"验收直接不成立——叠加 triage 已死与 H4 预算门失效，自主层的花钱控制三重失效。判定条件：不存在。

### N4.4 G4.5 清单逐项核对

| 项 | 结论 | 依据 |
|---|---|---|
| 时区 Asia/Shanghai | ❌ 部分 | board.py:78-79 `datetime.now(timezone.utc).date()` 仍是 UTC 日期；probes/agenda.py:30、director.py:18、earnings.py:20、calendar.py:17 同；`schedule_dalton_automation_helper`（schedule.py:379-384）对 naive `next_run_at` 不补 tz，`create_automation` 原样入库按 UTC 解释（automation.py:148-215）——"排 07:30"实际 15:30 CST 触发（R5-11 原样）。修正项：`dalton.yaml` 的 autonomy.timezone 默认确为 Asia/Shanghai，全局任务 cron 的 tz 字段用对了 |
| 财报日历周六 09:00 | ✅ | schedule.py:244 `0 9 * * 6` + 默认 Asia/Shanghai，与模板 `dalton-earnings-calendar.json` 一致（R5-8 该项已修）；美股公司监测 tz=America/New_York ✅（:41） |
| 额度用完 once 任务推迟而非关闭 | ❌ | executor 预检跳过统一走 `Outcome.PRECHECK_SKIPPED`（automation_executor.py:821-831），策略 `schedule="close"`（automation_settlement.py:194-200），`_schedule_action` 对 once 映射 `close_once`（:302-306）→ `status='completed', next_run_at=NULL`。预算耗尽日触发的 tm3/t0/tp1 仍然静默永久丢失；且因 H4，目前这个分支根本不会因额度触发（两个 bug 互相遮蔽） |
| 并发去重（(kind,ref)+Postgres） | ⚠️ 部分 | `claim_signals_woken`（watch.py:203-233）是原子 `UPDATE…WHERE woken_execution_id IS NULL RETURNING` ✅（G4.5 承诺的原子化已做）；**但 preflight.py:659 调用后丢弃返回值**——两个并发 firing 各自 upsert 同一信号（SELECT-then-INSERT 竞态仍在，watch.py:101-167）、各自跑 triage（窗口被 60s 级 LLM 调用拉宽）、只有一个 claim 成功，**两个都返回 wake**，双份唤醒双份花费。NULL workspace 信号在 UNIQUE 语义下仍可插重复行（R5-12b 原样） |

### N4.5（并入上表并发行）

### N4.6 H10/H11 深查

**四探针数据来源：**
- `sec` ✅：真实 EDGAR submissions JSON（sec.py:176-183，httpx+UA env，15s 超时），CIK 解析链 watch_state → 公司文档 `company.cik`/`identifiers.cik` → 内置 22 ticker 表（含四家）→ coverage.yaml → SEC company_tickers.json；首启只记水位不发洪水（:214-216）。
- `agenda` ✅：`latest_valid_version(workspace_id, "agenda")` 读知识库（agenda.py:32-40）——但读字段错（H13）。异常静默吞掉无日志（:39-40，R5-17a 原样）。
- `earnings` ⚠️：窗口检测走 agenda `watch.events` type=earnings ✅（earnings.py:27-39，真实现已接线）；但 `latest_earnings_release_id` 新材料检测仍读无人写入的 state 字段（全仓库无写入者，grep 证实）——那条腿是死的。也仍读 schema 外的 `catalysts`（:28）。
- `internal` ❌：查询 `dalton_internal_documents` 表（internal.py:29-50）——**该表全仓库无任何迁移建表、无任何写入者**（grep 全库仅此一处引用），生产上必抛 UndefinedTable → 被 :51-53 以 **debug 级**吞掉 → 探针恒空。G4.1 承诺的 researchdata 桥接未实现（文件内无桥接调用）。这是 R5-2 同类问题的残留：四探针里一只仍是死代码，且伪装成"有 DB 数据源"。

**CIK 覆盖：** ✅ 迁移证据 `var/dalton/migration/Home/dalton/companies/{GOOGL,META,MSFT,AMZN}/data/company.json` 均有 `identifiers.cik`（0001652044/0001326801/0000789019/0001018724），sec.py 两种取法都认；公司初始化 `plugins/dalton/skills/dalton-init-company/scripts/init_company.py:90-166` 写 `"cik"`+`"identifiers":{"cik":…}`；内置表兜底同样覆盖四家。

**信号 resolve / 7 天过期接线：** ✅ 真实存在调用方。
- resolve：`knowledge_append_verdict` 成功后 → `resolve_verdict_signals`（knowledge_tools.py:226-231）→ `watch_db.resolve_signals`（refs 支持 signal_id/signal_key/payload id/accession，另按 woken_execution_id 兜底全量 resolve）；唤醒指令（preflight.py:291）明示把 signal_key 放进 `trigger_signal(s)`。
- 过期：preflight.py:430-434 每次预检调 `expire_old_signals(max_age_days=7)`；`expired_at` 列由 d007:94 补上（位置埋在"llm_call_journal"迁移里，建议以后单列）；board 查询过滤 `resolved_at IS NULL AND expired_at IS NULL`（board.py:186-187）✅。R5-3 的重复派单根源已除。
- 小瑕：resolve 失败仅 debug 级日志（knowledge_tools.py:132-134）。

### N4.7 PRECHECK_SKIPPED 三端（✅ 无退化）

- 结算：`_Policy(status="skipped", from_statuses=("pending",), metric="precheck_skipped", schedule="close")`，无 strike、无 webhook（automation_settlement.py:194-200，DALTON-HOOK 标注在位）。
- 调度：`_schedule_action` cron→None（claim 时已排下次）、once→close_once、price 按 one-shot/recurring 正确 close 或 rearm（:285-306）。
- 展示：`SkipReason` 含 "precheck"（models/automation.py:591）；`not_a_server_skip` 把 precheck 与 thread_busy/interrupted 同列（database/automation.py:56-62）；前端 types/automation.ts:33 + status.ts:308 + zh-CN/en-US `automation.skippedPrecheck` 文案俱全。全部原样。

### R5 中低项抽查（>一半，11/21）

| R5# | 结论 |
|---|---|
| 7 总监恒唤醒 | ❌ 未修（见 N4.3，本轮升级为高） |
| 8 时间表 | ⚠️ 财报日历周六 09:00 ✅、美股 tz ✅；delta 仍周日一次 `0 21 * * 0`（模板 30 21 * * 1-5 每晚）、watch `0 7,19` vs 模板 `*/30 6-20`、director-daily `0 8` vs 模板 `30 7`、director-weekly `0 20 * * 0` vs 模板 `0 10 * * 6` |
| 9 退避公式 | ⚠️ 问题退避已改 0.5×停滞时长、24h-30d 夹取、新证据清零（ops.py:575-664）✅；执行失败退避已接 `_count_strike` 的指数 1h×2^n 封顶 7d（≈总时长一半，成功 reset）✅；**但预检仍不读 `next_attempt_after`**——退避期的问题照常发信号唤醒（G4.5"预检要跳过退避期的问题"未实施） |
| 10 总超时 | ❌ 未实施（见 N4.1） |
| 11 时区 | ❌ 未修（见 N4.4） |
| 12 并发 | ⚠️ 原子 claim 已做、结果被弃（见 N4.4） |
| 13 看板/记录口径 | ❌ 未修且被 H4 冻结在坏侧（board.py:413 `max(allocatable, est)` vs record_plan 严格拒绝） |
| 14 派单契约 | ✅ schedule.py:268 改为 `delegate_to_analyst(question=…, workspace_id=…, report_back=True)`；instruction 无 `delegate_to_workspace` 残留 |
| 15 once 预算关闭 | ❌ 未修（见 N4.4） |
| 17 吞错 | ⚠️ preflight/board/report 主路径有 warning 日志 ✅；agenda 探针 :39-40 仍静默、internal :52 仍 debug 级、resolve 失败 debug 级 |
| 19 price 信号无 id | ❌ 原样（preflight.py:387-389 `trigger:<exec8>`，不落 dalton_signals） |

## 问题（按严重程度）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | preflight.py:354-356；board.py:88-90 | H4 坐实：预算键名读错（daily_cap_usd/spent_today_usd/reserved_usd vs 真实 cap_usd/spent_usd/remaining_usd），且 preflight.py:353 仍不传 user_id | 离线复现：cap_usd=1/spent_usd=1（已耗尽）→ `_check_budget_available` 返回 True；board remaining=50.0 幻影；`dalton_record_plan` 守卫（director_tools.py:131）因 cap_usd=0 恒不触发 | 改读 cap_usd/spent_usd（或 remaining_usd 直用）；`get_budget_today(user_id)` 从 automation 行取 user_id 传入；补一条锁定真键名的契约测试 |
| 2 | 高 | preflight.py:179-186 | triage 坐实：assist_json 传参签名全错（system_prompt/user_input/output_schema/ticker），生产必 TypeError→被吞成 wake fallback，LLM 分诊从未生效；单测 mock(**kwargs)（test_autonomy_preflight.py:275）掩盖 | 离线实跑：`reason=triage_fallback_wake`，Form 4 等本应分诊的信号全唤醒，G4.2 降噪目标整体落空 | 改为 `assist_json(task=…, instructions=…, payload=…, schema=…, workspace_id=…)`；mock 改成真签名的 StrictMock/`inspect.signature` 校验 |
| 3 | 高 | probes/agenda.py:48,76,132；board.py:287 | H13 坐实：读 schema 外的 validation_queue/next_actions/catalysts，不读 next_validations/wake_conditions；"验证到期"核心信号检不出 | 离线复现：真实 GOOGL 议程 0 信号；向 next_validations 注入 due=已过期项仍 0 信号；仅 schema 外的 validation_queue 出信号（而它永远写不进文档） | probe/board 改读 next_validations（id/what/due/status 已兼容）与 wake_conditions；删 catalysts/validation_queue/next_actions 分支（别名表已负责写入侧归一） |
| 4 | 高 | director_tools.py:241-253（`/open_questions` 的 value） | H12 修复不完整：`dalton_agenda_note` 追加的问题缺必填 `owner`（`#/$defs/question` required），且无服务端默认；每次都被 schema 拒绝（NOT SAVED）——PM 调优先级机制仍 100% 失效 | 离线复现：按工具实际 payload 走 apply_ops+validate → `'owner' is a required property`；LLM 字段修复救不了"缺字段"类错误 | value 补 `"owner": "pm"`（或 `_ensure_nested_ids_and_defaults` 给 open_questions 默认 owner）；给 dalton_agenda_note 补一条走真 schema 的单测 |
| 5 | 高 | probes/internal.py:29-53 | 内部库探针查询 `dalton_internal_documents`——该表无任何迁移建表、无写入者（全仓库唯一引用即此文件）；生产必 UndefinedTable 被 debug 级吞掉，探针恒空。G4.1 的 researchdata 桥接未实现 | 任何一次 company_watch 预检：SELECT 报 relation 不存在 → logger.debug → 返回 [] → internal 信号永远为空 | 建表+接桥（按 G4.1），或明确删掉该探针并在文档记录；异常至少 warning 级 |
| 6 | 高 | probes/director.py:33-41 | 总监探针无"有事可做"判断，daily/weekly 恒发信号必唤醒；G4.3 第 3 条与 PK.19(2)"空闲日 $0"不成立 | 议程全空、无信号、无待批的工作日，`dalton-director-daily` 照常开晨会花强模型钱 | 按 G4.3 五条件实现（到期 V-/高价值开放问题/超 24h 未消费信号/临近 TTL 待批/待确认财报日），全无则 skip |
| 7 | 中 | src/server/services/automation_settlement.py:194-200；automation_executor.py:821-831 | G4.5"额度用完 once 改推迟"未实施：预检跳过统一 close，once 任务 completed、next_run_at=NULL，财报链节点静默丢失（当前被 H4 遮蔽不会触发，修 H4 后即暴露） | 额度耗尽日 T0 快评触发 → skipped(precheck) → 任务 completed 无提醒 | 预检返回带 reason；settle 对 `once + reason=budget_exhausted` 改为 `next_run_at=次日`（推迟），其余 precheck skip 维持 close |
| 8 | 中 | preflight.py（decide/preflight_check 整体）；settings.py:98；dalton.yaml:44 | G4.5"预检 45 秒总超时"未实施：配置项定义了但零引用，无 wait_for 包裹 | DB 池饥饿 6 分钟 → firing 挂起被 sweep 按 INTERRUPTED 结算计 strike（dalton 虽不再停用，但白记失败） | `decide()` 用 `asyncio.wait_for(preflight_check(...), settings.autonomy.preflight_timeout_s)`，超时按 wake 兜底 |
| 9 | 中 | board.py:78-79；probes/{agenda,director,earnings,calendar}.py；schedule.py:379-384 | 时区未按 Asia/Shanghai：today 判定全用 UTC 日期（上海 00:00-08:00 晚一天）；helper 对 naive next_run_at 不补 tz，按 UTC 入库（07:30 意图 → 15:30 CST 触发） | 分析师写 `next_run_at=2026-10-30T07:30:00` → 晚 8 小时 | 探针/board 用 `ZoneInfo(settings.autonomy.timezone)`；helper 里 naive 时间 `replace(tzinfo=ZoneInfo(tz))` 再入库 |
| 10 | 中 | preflight.py:659；watch.py:101-167 | 并发双唤醒残留：upsert_signal 仍 SELECT-then-INSERT，claim_signals_woken 原子但**返回值被丢弃**，未用它过滤 woke_signals；triage 等待把竞态窗口拉宽到分钟级 | watch 与手动触发并发命中同一 filing → 双双 wake、双份花费（信号行本身只 claim 一次，账面看不出） | `claimed = await mark_signals_woken(...)` 后用 claimed 的 signal_id 集合过滤 woke_signals，空则按 skip 结算 |
| 11 | 中 | board.py:413 vs director_tools.py:131 | R5-13 原样：看板贪心允许首条超限候选（`running_est+est <= max(allocatable, est)`），`dalton_record_plan` 严格拒绝 `planned > allocatable`；叠加 H4 时 cap=0 使两侧判断全部失真 | 低额度日看板第一条推荐即被记录工具打回 | 两侧统一（都允许"当日仅一件超限"或都严格）；修 H4 后补 cap=0（未设上限）语义一致化 |
| 12 | 低 | knowledge/service.py:475 | 版本行存 repaired_ops，不存逐字原始 ops；自动修复的"修复前原文"只能由 warning 的字段名映射+内容重建 | 审计回放无法看到代理提交的原始请求 | insert_version 增存 original_ops（或 warning 带原值摘要） |
| 13 | 低 | ops.py:179-252（events 条目别名） | 别名条目覆盖不完整：旧 catalyst 形状的 title/expected_date/status/importance 无映射，修复后仍过不了 schema（fail-closed） | 迁移形状 create → NOT SAVED，代理被迫手工重写字段名 | events 别名补 expected_date→date、title→plan/note 等；或统一由迁移工具预归一 |
| 14 | 低 | earnings.py:78-90 | `latest_earnings_release_id` 无任何写入者（grep 全仓库），新材料检测是死腿 | 永不触发 release 信号（窗口信号不受影响） | 接 SEC 探针的 8-K 2.02 结果回填，或删除该分支 |
| 15 | 低 | schedule.py:42,68 vs docs/.../automation-templates | 时间表与模板不一致残留：watch `0 7,19` vs `*/30 6-20`；delta 周日 21:00 vs 每晚 21:30；director-daily 0 8 vs 30 7；director-weekly 周日 20:00 vs 周六 10:00 | bootstrap 建出的任务与设计文档节奏不同 | 对齐或改模板，二选一并记录决定 |
| 16 | 低 | probes/agenda.py:39-40；internal.py:51-53；knowledge_tools.py:132-134 | 吞错日志级别：agenda 探针静默吞 DB 异常（无日志）；internal 对死表 debug 级；verdict resolve 失败 debug 级 | 知识库故障 → 所有 watch 静默 skip 无 warning | 统一 warning 级并带 probe 名 |
| 17 | 低 | director_tools.py:234,300 vs assist.py:132、report.py:125 | 默认 user_id 双写法并存：`local_dev_user`（下划线）与 `local-dev-user`（连字符）分属两套默认；预算/日志按 user 隔离时会读到两套账 | 单用户 OSS 无感；多用户即数据串号 | 统一常量（建议跟随 CurrentUserId 的真实 user） |
| 18 | 低 | preflight.py:454-463 | weekly_delta/earnings_window/custom_dalton 恒发合成 schedule 信号唤醒；"delta 无重要变化不推送"未实施（R5-8a） | 每周日 delta 无变化也花一次强模型钱 | delta 走探针比对（一致预期/内部库水位），无变化 skip |

## 已确认没问题的关键点

- **三条硬规则与字段修复隔离**（N3.4 全部离线构造通过）：硬规则错误跳过 LLM 修复、修复后全量重验、official 双重过滤、证据台账条目不可经修复改动。
- **direct_edit 检测无退化**：reconcile.py 未动，service.py 导入逻辑与文件覆写豁免原样；136 项 dalton 单测全绿。
- **G4.4 次数门真做了**：`_count_strike` 带 `DALTON-HOOK(no-strike)`，`name NOT LIKE 'dalton-%'` 豁免 + provider_auth fuse 保留 + dalton 指数退避（1h×2^min(n,8) 封顶 7d，近似"总时长一半"）+ 成功 reset；schedule.py/director_tools.py/bootstrap_watch.py 的 max_failures 写入全删（grep 仅剩 skill 文案里的"不要写"）；`test_automation_no_strike.py` 在库并通过。
- **信号生命周期闭环**（H11 修复）：verdict→resolve_signals、7 天过期、board 过滤 resolved/expired，全部有真实调用方。
- **CIK 链与四家覆盖**：迁移产物、初始化脚本、内置表三路齐备。
- **PRECHECK_SKIPPED 三端**（结算/调度/前端+文案）原样无退化。
- **路由 500 修复**（R5-4）：PUT /automations/{name}、GET /coverage/board、GET /autonomy/report 均接上存在且签名匹配的函数。
- **assist_json 家族**：audit.judge/label_figures/knowledge.repair_field 三处签名正确、走 dalton_complete、never-raises、结果经代码复核（knowledge 侧：硬规则重验）。
- **日志无凭据**：autonomy 路径只含 ticker/exec id/异常文本；SEC UA 取自环境变量。
- **状态全在 Postgres**：watch_state/signals/preflight_log/plans 均库表；sec.py 的 `_SEC_TICKERS_CACHE` 只是进程内只读缓存，不作真值。
- **探针故障不致停**：时间退避（2m-2h）后继续，其余探针照常；decide() 永不抛。

## 疑似问题（未能确认）

- **EDGAR 真实连通性与水位推进**：未做真实网络调用（离线审查），sec 探针逻辑经 override 单测与代码走读确认，真实 submissions 拉取行为（限流、UA 要求）需 G4 整族三天真实运行验证。
- **并发双唤醒实测**：竞态窗口经代码推证（毫秒级到分钟级），未构造双 worker 实际复现；需 DB 环境。
- **multi-user 串号**：board/report/preflight 的查询仍不按 user 过滤（R5-18 原样），单用户 OSS 无影响；另 `expire_old_signals` 对 Home 级任务（workspace_id=None）会跨 user 过期全部信号——多用户部署前需决策。
- **`test_preflight_direct_wake_vs_llm_triage_and_fallback` 是否还锁着错误签名**：mock 为 `**kwargs`，修复调用方后该测试不会失败——修 triage 时应同步收紧 mock，否则回归无门。
