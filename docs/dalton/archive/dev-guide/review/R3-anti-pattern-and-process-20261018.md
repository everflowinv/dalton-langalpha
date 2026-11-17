# R3 反模式与流程审计（L28–L33、§6 常规）
审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：66a662ac（源码=18bd3d8e）

审查范围：`dc16c3f0..18bd3d8e`，只读。涉及 LLM 的检查为零次真实调用，花费 **$0**。
所有命令在 `/Users/everflow/Projects/dalton-langalpha` 工作副本（`git diff --stat 18bd3d8e 66a662ac -- src/dalton` 为空，源码与 18bd3d8e 一致）上执行。

## 总判断（规则还在长吗；流程离 L32 多远）

- **规则没有在语义意义上继续长。** 本轮 `src/dalton` 新增 `re.compile` 恰好 6 条、内联 `re.search` 2 条，全部是 P0H §1.5 点名要求的掩码/结构类；四张冻结词表与基线**逐字节一致**，零新增条目。唯一"规则形状"的增长不在词表，而在 `a725c47f`（H2+H6 分支）里混入的 match.py 特例分支（见 4.1.4 与问题 #2）。
- **流程离 L32 有两处实质偏离：** ① 6 个族 0 个 PR（`gh pr list --state all --limit 100` 过滤 `P0H`/`dalton` 命中 0），全部本地 `--no-ff` 合并后直推；② H2+H6 合用一个分支本身可接受（H2 仅配置+脚本），但该提交同时携带了**提交说明完全未声明的发布管线功能改动**。
- **开发报告的"净减 88 行"不可从 git 复现**，且提交说明与开发报告给出两组不同的毛数、同一个净数——数字疑似事后编造常数（问题 #1）。
- L33：**0 处中危违规**；新增裸替身 8 处中 6 处为数据库连接/游标类（清单允许），2 处为无 spec 的 LLM 客户端替身（dalton_complete 本体真实运行，判疑似低危，lead 可改判）。

## 4.1 代码量与规则（含正则清单逐条判断表）

### 4.1.1 行数口径

实测命令与输出（`-- src/dalton/publish src/dalton/knowledge`）：

| 口径 | 命令 | 结果 | 净变化 |
|---|---|---|---|
| H1 单族（lead 预跑①） | `git diff --stat dc16c3f0 c1e76363 -- src/dalton/publish src/dalton/knowledge` | 12 files, **+602 / −707** | **−105** |
| 本轮整体（lead 预跑②） | `git diff --stat dc16c3f0 18bd3d8e -- …` | 13 files, **+871 / −741** | **+130** |
| 开发报告 L51–54 声称 | — | +447 / −535 | **−88** |
| H1 提交说明（dabe0b7f 正文）声称 | — | +622 / −710 | **−88** |

**"−88"无法从 git 的任何自然口径复现。** 我试过的口径均对不上：publish 单独至 c1e76363 = +587/−596（净 −9）；knowledge 单独 = +15/−111（净 −96）；numbers+match 两文件 = +278/−420（净 −142）；H1 排除 audit.py = +323/−575；`dabe0b7f^..dabe0b7f`（分支尖对父）= 与 dc16c3f0..c1e76363 相同（dabe0b7f 的父就是 1e4f3851，H2–H5/H7 均未触碰这两个目录）。提交说明与开发报告共用"净 −88"却给出两组互不相同的毛数，这是编造数字的典型特征，**按问题 #1（中危，报告可信度）报告**。

**全轮 +130 的构成**（逐提交 numstat）：

| 提交 | publish+knowledge 变化 | 内容 |
|---|---|---|
| dabe0b7f（H1） | +602/−707（净 −105） | match.py 139/−222、numbers.py 139/−198、review.py 25/−43、knowledge/ops.py 0/−103 全删；audit.py **+279/−132（净 +147，B 环节必经 + 锚定重构加码）** |
| 16006401（H3） | knowledge +156/−8（净 +148） | `knowledge/service.py` +118：§3.3 字段修复（difflib 建议 → `assist_json('knowledge.repair_field')` LLM 确认，service.py:107-137、255-266）+ ops.py +38 |
| a725c47f（H2+H6） | +122/−35（净 +36） | match.py +52/−20（SF-CN 合成数字、目标价流）、numbers.py +9/−5、service/ops 小改 |

即：H1 确实净减 105 行（其中知识层 ops.py 删 103 行），但 H3 的 LLM 字段确认流（+148）和 H6 顺手改的 match/numbers（+36）把全轮翻成**净增 130**。报告若要陈述减码，只能以 H1 单族为准并写明口径。

### 4.1.2 新增 re.compile 逐条判断

`git diff dc16c3f0 18bd3d8e -- src/dalton | grep -E "^\+.*re\.compile"` 共 6 条（2 条为多行写法，模式体在后续行）：

| # | 正则 | 位置 | 用途 | 判定 |
|---|---|---|---|---|
| 1 | `_SLASH_DATE_RE` = `(?<!\d)(?:0?[1-9]|1[0-2])/(?:0?[1-9]\|[12]\d\|3[01])(?:/\d{2,4})?(?!\d)` | numbers.py:45 | 掩码斜杠日期"10/12"（numbers.py:489 `_blank` 调用） | ✅ 掩码，P0H §1.5 点名（D11） |
| 2 | `_CLOCK_TIME_RE` = `\b(?:[01]?\d\|2[0-3]):[0-5]\d(?::[0-5]\d)?\b` | numbers.py:46 | 掩码时间"05:22"（:490） | ✅ 掩码，§1.5 点名 |
| 3 | `_LONG_ID_INT_RE` = `\b\d{12,}\b` | numbers.py:47 | 掩码 12 位以上长整数（Discord ID 1485122334455667788，:491） | ✅ 掩码，§1.5 点名 |
| 4 | `_BATCH_CODE_RE` = `\b\d{8}-\d{4}\b` | numbers.py:48 | 掩码批次号"20261015-2200"（:492） | ✅ 掩码，§1.5 点名 |
| 5 | `_HEADING_PREFIX_RE`（改写，非新增名）加负向前瞻 `(?![1-4]Q\d\|[12]H\d\|20\d{2}\s*[QH][1-4])` | numbers.py:56-59 | 剥标题序号时不再吞"# 2Q26 复盘"的期间词（:553、:571） | ✅ 结构定位，§1.5 点名修复 |
| 6 | `_CANONICAL_UNIT_TOKEN_RE` = `\b(USD_bn\|…\|pct\|bps\|bp\|USD\|HKD\|CNY)\b` | audit.py:184 附近定义 | `_extract_quote_numbers`：确定性解析器从引文抽出数字后，若引文中出现规范单位记号（如 `USD_bn`），把裸 `unit='other', scale=1.0` 的数字升级为该单位 | ✅ 单位格式归一，喂给 `values_within_tolerance` 的确定性换算；不按词义判结论 |

另有 2 条内联（非 compile）：`re.search(r"[A-Za-z]{2,}\|[\u4e00-\u9fff]", fig.metric or "")`（match.py 判 metric 字段是否含字母内容；audit 门控处同款）——检查字符串里有没有字母，格式判断，✅。新增 `_VALID_LABEL_KINDS = frozenset({"reported","estimate","consensus","guidance","derived"})`（numbers.py:877）是 LLM 返回 kind 值的 schema 枚举白名单（:957、:1003），结构性校验，✅。

**越权数：0。** 无一条用规则做指标语义判断或直接产生 verified/contradicted 结论。

**计数对比：** `src/dalton` 的 `re.compile` 总数 dc16c3f0=30（numbers.py 22 + 8 个文件各 1）→ 18bd3d8e=34（numbers.py 25 = 22−删 `_CN_PROSE_FIG_RE`+4 掩码；audit.py +1；其余不变）。净 +4。

### 4.1.3 冻结表

`git diff dc16c3f0 18bd3d8e -- src/dalton/publish | grep "^\+.*(DEFAULT_METRIC_ALIASES\|_METRIC_SYNONYM_GROUPS\|_GROWTH_WORDS\|_MARGIN_WORDS\|_GENERIC_MODIFIER_WORDS)"` 仅命中 2 行，均为 match.py:23-24 新加的 **FROZEN 注释本身**，无条目行。逐表字节比对（`git show <rev>:<file>` 提取定义块）：

| 表 | 基线行数 | 现行数 | 逐字节一致 |
|---|---|---|---|
| `DEFAULT_METRIC_ALIASES`（match.py:25） | 63 | 63 | ✅ |
| `_GROWTH_WORDS`（match.py:73） | 15 | 15 | ✅ |
| `_MARGIN_WORDS`（match.py:88） | 11 | 11 | ✅ |
| `_GENERIC_MODIFIER_WORDS`（numbers.py:313） | 25 | 25 | ✅ |

注意：**`_METRIC_SYNONYM_GROUPS` 在代码中从未存在**（dc16c3f0 与 18bd3d8e 均无）——开发报告冻结的是一张不存在的表（D13 的镜像：指南写错一套名字，报告写错另一套）。实际被删的是 `_CN_PROSE_FIG_RE`、`_CN_DIGIT_MAP`、`_parse_cn_prose_figure`（dc16c3f0 的 numbers.py:62/75/507 存在，现已全删 ✅）；报告声称删掉的 `_CN_COLLOQUIAL_MULTI_RE`/`_CN_NUMBER_SEQ_RE`/`_chinese_numeral_to_float` 在两个版本中都不存在。

使用点全量核对：

- `DEFAULT_METRIC_ALIASES`：唯一使用点 match.py:145（`canonical_metric_group`，把 metric 归并到别名组）。调用方：`metrics_match`（match.py:184-185）与 match.py:458。
- `_GROWTH_WORDS`/`_MARGIN_WORDS`：唯一使用点 match.py:172-180，作为**否定守卫**（"广告收入"不得对"广告收入同比增速"、"营业利润"不得对"营业利润率"）。
- `_GENERIC_MODIFIER_WORDS`：唯一使用点 numbers.py:365，把"同比"等泛修饰从句与前主语从句拼成更完整的 metric 字符串。
- `metrics_match` 全部 13 个调用点落在三个函数：`retrieve_candidate_sources`（match.py:1022-1159，纯候选检索，喂给 LLM）、`match_figures`（:550 结构化 evidence、:793/:814/:860/:882 脚注路径）、`_inspect_text_source`（:343）。

**能否直接决定结论：不能。** 在 match_figures 的脚注路径（match.py:550-578），`metrics_match` 通过只是必要条件，紧随其后的 `values_within_tolerance`（数值换算+容差，:554/:571）才是授予 `matched=True` 的决定者；词表内容只能**否决**一次核实（把数字碰巧相同但指标对不上的情况挡下来 → unverified+候选，正是 P0H 要求的方向），永远不能单凭词表造出 verified/contradicted。保守方向，✅。`source_conflict` 候选也只在"指标已匹配且数值确实不等"时产生（:593），决定者仍是数值比较（L28 判定归 R3-code 报告，此处只回答词表问题）。

### 4.1.4 结论：规则还在长吗？

**词表和正则没有长**：正则净 +4 且全部为 §1.5 点名的掩码/结构类；冻结表零变化且加了 FROZEN 注释；`consensus_confusion`、`_infer_label_hint` 确已删除。**但有一处非正则的"规则式增长"值得点名**：`a725c47f`（H2+H6 分支）在 match.py:312-330 新增 SF-CN 特例——草稿数字 `raw` 不含任何阿拉伯数字（口语中文数字）且逐字出现在来源正文时，**用草稿自己的 value/unit/metric 合成一个来源侧 ExtractedFigure**，随后数值比较恒真。锚定实质是"词逐字出现"，value/unit 是自证拷贝；对无数字词这可能是唯一可行的锚，但它是一条新匹配规则，且写在证据收集分支里、未在提交说明中声明（见问题 #2；语义风险归 R3-code 报告评估）。另外 `llm_service` 参数线仍在 publish 内贯穿 18 处（audit/service/review/numbers，作为可注入依赖传给 `label_figures_with_status`）——是否违反 L30 的删除要求由 guide-conformance 报告按 D13 判定，此处仅记录 grep 结果非空。

## 4.2 测试替身

### 4.2.1 逐条判定

清单 §1 的 grep 在本轮改动的 6 个测试文件中共 9 处命中；其中 `test_autonomy_preflight.py:809` 的 `mock_cur = AsyncMock()` 是**未改动的既有行**（diff 中为上下文行）。本轮**新增**裸替身 8 处，全部在新建文件 `tests/unit/dalton/test_budget_port.py`：

| # | 位置 | 替身 | 用在哪 | 判定 |
|---|---|---|---|---|
| 1-2 | :30, :33（`_make_mock_conn`） | `MagicMock()` ×2 | DB 连接/事务的 `__aenter__/__aexit__` 壳 | ✅ 可接受（数据库连接类） |
| 3 | :73 | `MagicMock()` | DB 游标（execute/fetchone 打桩返回预算行） | ✅ 可接受 |
| 4 | :403 | `MagicMock()` | 同上（BYOK acquire 测试的游标） | ✅ 可接受 |
| 5 | :202 | `mock_llm = MagicMock()` + `.complete = AsyncMock(...)` | `test_dalton_complete_cache_hit_and_budget_rejection`：作为**第一个参数传给真实运行的 `dalton_complete`** | ⚠️ 疑似低危（见下） |
| 6 | :818 | 同上 | 序列化异常 → `indeterminate` 测试，同样传给真实 `dalton_complete` | ⚠️ 疑似低危 |
| 7-8 | :524, :527 | `token_callback=MagicMock()` | `build_local_run_gate` 的令牌回调参数 | ✅ 可接受（平凡回调） |

**#5/#6 不满足"中危"的字面条件但贴边，交 lead 裁量：** 替身替换的是**供应商客户端对象**（仅用到 `.complete(...)` 单方法协议），不是 `assist_json`/`dalton_complete`/服务函数本身——`dalton_complete` 在测试里真实执行，其内部的 `inspect_journal_state`、`_check_cash_budget_admission`、`claim_journal_invocation`、`complete_journal_failed`、`record_provider_event` 等服务依赖全部用 `create_autospec(..., spec_set)` 打桩（:822-857 等）。残留风险是客户端调用签名的漂移（如 kwarg 改名）AsyncMock 不会报错。若有具体客户端类可 `create_autospec`，建议统一；按协调方规则若坚持"传进 dalton_complete 的替身也算"，则此两处升中危。

spec 纪律总览（本轮改动测试文件）：`create_autospec` 计数——test_router_wiring 19、test_autonomy_preflight 18、test_budget_port 16、test_knowledge_layer 6、test_publish_numbers 4；裸 `Mock()` 计数——仅 test_budget_port 8（如上表）。

### 4.2.2 测试质量抽查

**锁的是回归与钱/凭据契约，不是凑覆盖率：**

- `test_publish_numbers.py::test_g31_llm_judge_reverification_and_uncertain_bucket_does_not_block`（:453，约 194 行）：用替身固定 LLM verdict，验证 L28/L29 锚定降级行为——正是修好的回归。
- `test_publish_numbers.py::test_g31_realistic_report_false_positive_rate_at_most_2`（:647）：误报率契约（≤2）。
- `test_publish_numbers.py:337-363`：D11 五例全覆盖（SEC accession、文档号、斜杠日期 10/12、时钟 05:22、12 位 Discord ID、批次号 20261018-0412、"# 2Q26 复盘"期间保持）。
- `test_budget_port.py`：G1.1（BYOK 上限授予 $1 chunk 而非 +1,000,000）、G1.3（缓存命中重放不计费 `cash_usd==0`、`mock_llm.complete.assert_not_awaited()`）、预算拒绝不调 LLM、序列化异常落 `indeterminate` 且重试不重发——全是钱的契约。
- `test_knowledge_layer.py:146-150`：用 `check_all_schemas()` 对 10 张真实 schema 校验（非复制结构）。
- `test_role_policy_native.py`（新文件）：`family_of` 家族解析 + `check_roles` 全角色 + `select_independent_reviewer`。
- 实测复核：`uv run pytest tests/unit/dalton/test_budget_port.py -q` → **15 passed**；`test_publish_numbers.py + test_knowledge_layer.py` → **21 passed**（与 H1 提交说明声称的 21 一致 ✅）。

**无锁定 LLM 具体输出的脆弱测试**：三个大测试文件中未发现对模型生成文本的长字符串断言（命中的长中文串均为固定文案/路径 fixture）；LLM 返回一律是测试自己注入的替身数据。前端 `PublishApprovalCard.test.tsx` 两个用例（英文结构化键 / 中文旧字符串键）断言的是组件渲染而非模型输出。

## 4.3 流程（分支/PR/提交说明核对）

### 4.3.1 一族一分支

6 个功能提交的分支归属（`git log --simplify-by-decoration` + `git branch -a --contains`，本地与 origin 均有同名分支）：H7→`fix/P0H.H7-progress-truth`（425f7551）、H4→`fix/P0H.H4-providers-roles`（90a1d94e）、H5→`fix/P0H.H5-budget-journal`（4fe42f87）、H1→`fix/P0H.H1-publish-redesign`（dabe0b7f）、H3→`fix/P0H.H3-autonomy-chain`（16006401）、H2+H6→`fix/P0H.H2-H6-realrun-evidence`（a725c47f）。**H2+H6 合并一分支**：H2 的实际内容是 `agent_config.dalton.yaml`（call_timeout_s 300→625）、`scripts/dalton/install_bridge.sh`、`scripts/dalton/researchdata_bridge.py` 增改——配置与脚本，几乎无产品代码，**可接受，但按清单要求在此写明**。真正的偏离是该分支还携带了 H1 域的功能改动（见 4.3.3）。

### 4.3.2 一族一 PR

`gh pr list --state all --limit 100 --json number,title,headRefName` 过滤 `P0H`/`dalton`：**命中 0**（最近 100 个 PR 均为平台侧，如 #563 feat/workspaces、#557 e2b-backend）。合并全部为本地 `--no-ff` 后推送（merge 提交 355e3a37/588dfc95/1e4f3851/c1e76363/aa1d1922/18bd3d8e 的 reflog 形态 + 分支直推 origin）。**不符合 L32 的"一族一 PR、PR 描述附证据"**：证据只存在于提交说明、`P0H-round2-dev-report.md` 和 gitignored 的 `var/` 里。是否要求补开 PR 由 lead 决定（清单 §4.3.2 预留此裁量）。

### 4.3.3 提交说明与代码一致性

6 个提交中 5 个只有标题行、无正文（425f7551、90a1d94e、4fe42f87、16006401、a725c47f）；仅 dabe0b7f 有详细正文。逐条核实：

| 提交 | 抽查的说法 | 核实结果 |
|---|---|---|
| 425f7551（H7） | "correct PROGRESS.md … status tiers" | ✅ 14 行改动：G5.1 🟨→🟦（"真实重启返 409 与中途断开重放复演待 H6 补齐"）、P0.1/P0.4/P0.5 补证据指针 |
| 90a1d94e（H4） | "provider probe client resolution" / "spec-checked router gatekeeper" / "journal accounting" / "full role policy enforcement" | ✅ provider_status.py 新增 `from src.server.services.llm.clients import resolve_model_client`；test_router_wiring.py +19 处 `create_autospec`；budget/gate.py + llm/assist.py 计账改动；role_policy.py + 新测试 test_role_policy_native.py。**附带改动**：providers/{antigravity,muse}/oauth.py——muse 是修真 bug（`record_provider_event` 原来漏 await 的同步空调用 → async+best_effort+debug 日志），dev-report 未提（与清单 3.3 的"未提及改动"对上） |
| 4fe42f87（H5） | "30d journal response pruning" / "fail-closed journal cash queries" / "scoped task cap enforcement" | ✅ llm_journal.py `prune_expired_journal_responses(retention_days=30)`；budget.py `raise` + 注释 "fails closed … 503"，测试 `pytest.raises(RuntimeError, match="dalton_llm_journal unavailable")`；complete.py `resolve_effective_task_cap` + `task_spent >= task_cap` 分支 |
| dabe0b7f（H1） | 正文 9 条具体说法 | ✅ 8/9 属实：删 `_CN_PROSE_FIG_RE`/`_CN_DIGIT_MAP`/`_parse_cn_prose_figure`（基线 numbers.py:62/75/507 已不存在）；`_infer_label_hint`/`consensus_confusion` 全库无痕；4 掩码正则 + `_HEADING_PREFIX_RE` 前瞻；`aaudit_draft`（audit.py:562）在 service.py:131/:362 两条路径被调；`Semaphore(4)`+120s（audit.py:576/:606-608）；2 处 `.pdf` 承诺删除；`resolve_thread_author_model` 加 `logger.warning`；PublishApprovalCard.test.tsx 新增。**❌ 1/9 不实**："Diff stats 622(+), 710(−) (net −88)" —— git 实测 602/707（净 −105），622/710 与 447/535（dev-report）互相矛盾且都不可复现（问题 #1） |
| 16006401（H3） | "autonomy signal chain A1-A14 and knowledge field suggestion confirmation" | ✅ 18 文件覆盖 preflight/watch/probes×5/board/report/schedule/director_tools；knowledge/service.py:107-137、255-266 确为 difflib→`assist_json('knowledge.repair_field')` LLM 确认流（写入仍走 schema 校验，"不经 LLM 确认不改写"的判定归 guide-conformance） |
| a725c47f（H2+H6） | 标题 "H2 environment startup and H6 G1-G7 real-run evidence" | ⚠️ **部分不符**：var/ 证据不入库（gitignored），提交实际包含 match.py +52/−20（SF-CN 合成数字特例 + 目标价脚注必须指向 model cell + 未脚注目标价可落到 evidence/prov）、audit.py 门控改 `_RequiresModelCellOnly` 需 `footnote_target`、numbers.py +9/−5、llm/assist.py、budget.py、automation_settlement.py——**发布管线的功能改动未在提交说明任何位置声明**（问题 #2） |

## §6 常规审查

### 6.1 上游文件改动标注

`git diff dc16c3f0 18bd3d8e --name-only -- src/server web/` 恰为清单所列 4 个文件，无遗漏：

- `src/server/database/automation_executions.py`：2 个改动块均有标注——:33 `# DALTON-HOOK(no-strike)`（既有）、:132 `# DALTON-HOOK(precheck-outcome): A9 defer one-time automation by 1 day on budget exhaustion`（新增 `defer_once_1d` SQL 块）。✅
- `src/server/services/automation_settlement.py`：4 处标注——:92（`PRECHECK_SKIPPED` 枚举旁）、:194、:300、:356（`_schedule_action` 调用点），与全部 4 个改动 hunk 对应（含 `ScheduleAction` Literal 扩展 `defer_once_1d`、`_schedule_action` 新增 `outcome/excerpt` 关键字参数、budget_exhausted 分支）。✅
- 前端两处在 `dalton/` 专属目录下（Dalton 自有 UI），无需挂钩标注。✅

### 6.2 probes/internal.py 桥接不可用时的行为

读 `src/dalton/autonomy/probes/internal.py` 全文：

- **桥地址解析**（:104-111）：`DALTON_BRIDGE_MCP_URL` 优先；否则 `DALTON_BRIDGE_TOKEN`+`DALTON_BRIDGE_PORT`（默认 8960）拼 `http://127.0.0.1:{port}/mcp/{token}`；两者皆无 → **直接返回 `[]`，零网络、零日志**（静默无信号，不是错误）。
- **超时**：每路由 `urllib.request.urlopen(req, timeout=8.0)`（:26/:51），跑在 `asyncio.to_thread` 里；3 个路由（sales-notes/alphaengine/guidepoint）**串行**，最坏 24 秒。
- **失败行为**：单路由异常 → `logger.warning("[DALTON_INTERNAL_PROBE] bridge query failed for ticker=%s route=%s: %s", …)`（:125-130，warning 级，符合 A14/D8），继续下一路由；最终返回已收集的 docs（可能为空）。
- **会不会拖住预检**：probe 本身不会挂死（套接字超时兜底），24s < 预检 45s 总时限，单独不致死；但**串行 3×8s 吃掉总预算的一半以上**，与其他探针叠加时是预检超时的主要嫌疑（低危建议：并行化或给整步包总时限，问题 #8）。桥不可用的净效果 = 无信号 + 状态 `last_internal_check_at` 照常推进（:159），不会阻塞也不会误报。
- **日志无凭据**：warning 只打 ticker/route/异常消息；token 只出现在 URL（:109）且从不被记录；urllib 的 URLError/HTTPError 消息不含完整 URL。✅

### 6.3 前端

- **PublishApprovalCard.tsx（+17）**：`labeling_incomplete` 从 `args.labeling_incomplete ?? figSum.labeling_incomplete` 取值；为真时渲染琥珀色横幅（`rgba(245,158,11,…)` 背景+边框，`data-testid="publish-labeling-incomplete-warning"`），文案 `t('dalton.publish.labelingIncompleteWarning', '标注未完成，口语数字和指标可能未核验')`。"未能判断"列表（uncertain_items）与四类计数（已核实/冲突/未核实/未能判断/豁免）此前已有，本轮由组件测试锁死两套键（英文结构化 `figure_summary`/`uncertain_items` 与中文旧字符串键 `数字核验`），测试 2 个用例断言横幅、计数行、列表项、审稿人、版本号齐全——**中文路径显示正确**。
- **i18n 现状**：`web/src/locales/zh-CN.json` 与 `en-US.json` 中 `dalton.publish.*`/`dalton.budget.*` 键计数为 **0**——本轮新键（`labelingIncompleteWarning`、`byokCashNote`）连同整个 Dalton 组件族都走 `defaultValue` 中文兜底，**英文界面显示的也是中文**。这是本轮之前就存在的既成模式，本轮沿用而非引入；且组件测试把 `useTranslation` mock 成永远返回 defaultValue，测试结构上不可能发现该问题。按清单"中英文两套文案下显示正确"的字面要求：英文侧不满足（低危，问题 #7）。
- **Settings 面板 DaltonBudgetCard.tsx（+6）**：仅新增一行静态说明 `t('dalton.budget.byokCashNote', '注：设置页填写的现金类 BYOK Key 同样计入本地日美元现金额度（L24）')`——纯提示文案，无逻辑。

### 6.4 多 worker / 凭据 / 吞错（本轮 diff 新增代码）

- **模块级状态**：全轮 diff 中唯一新增的模块级可变状态是 `src/dalton/llm/providers/antigravity/oauth.py:56` `_LAST_INITIATED_AT: dict[str, datetime] = {}`（H4 提交引入）：`start_login` 写入（:203），`check_login_complete` 在 **Redis 不可用的回退路径**读取（:419-428），用 DB 行 `updated_at` 与本进程记录的发起时间比较，防把登录前的旧 token 行误判为完成。多 worker 下发起登录与轮询可落在不同进程，读侧拿到 `None` → 守卫退化为"任何有 token 的行都算完成"，即 Redis 故障时可能误报登录完成；字典也只增不清。AGENTS.md 明文"请求路径不得查阅模块级状态"，按字面违规（中危，问题 #3；爆发半径限 Antigravity 登录流 + Redis 故障双条件）。
- **日志凭据**：全轮新增 logger 行过滤 `token|key|secret|password|url=` 命中 0；bridge token（URL 内）、OAuth verifier/state 均未出现在日志语句。✅
- **吞错**：全轮新增 `except Exception` 22 处——19 处紧随 logger 或 raise（含 muse/oauth 把原 `except: pass` 改为 debug 日志的净改善）；2 处"handled/continue"为时区解析回退（`ZoneInfo(...) except → utc`，calendar.py/board.py/schedule.py，窄且合理）；1 处 `except Exception: pass` 在 complete.py `_unwrap_content_blocks` 的 `ast.literal_eval` 解析尝试——纯解析回退，失败即返回原字符串，非运维错误吞没。✅（antigravity/oauth.py 另有 3 处写库/关 socket 的 best-effort pass，均在前置日志之后，可接受。）
- **状态以 DB 为准**：本轮核心状态机（watch 信号、`dalton_watch_state`、journal、预算）全部走 Postgres/Redis，未发现新的进程内真相源（除上述 `_LAST_INITIATED_AT` 一处）。

## 问题汇总

| # | 严重程度 | 位置 | 问题 | 建议 |
|---|---|---|---|---|
| 1 | 中 | `P0H-round2-dev-report.md:51-54` + `dabe0b7f` 提交正文 | "净减 88 行"不可从 git 复现：报告称 447/535、提交说明称 622/710、实测 602/707（H1 净 −105）；全轮 publish+knowledge 实为**净增 130**（H3 知识层 +148、H6 顺手改 +36）。两组毛数共享同一净数，疑似编造常数 | 报告改为可复现口径（注明 `dc16c3f0..c1e76363` 与全轮两组数），并解释全轮为何转增 |
| 2 | 中 | `a725c47f`（fix/P0H.H2-H6-realrun-evidence） | H2+H6 证据提交携带未声明的发布管线功能改动：match.py SF-CN 合成来源数字（用草稿自身 value/unit 自证匹配）、目标价脚注强制 model cell、audit.py 门控改条件——H1 域的工作流进了 H6 分支，违反一族一分支的精神，且说明只字未提 | 语义正确性交 R3-code 报告审（SF-CN 自证匹配风险）；流程上要求后续凡功能改动必须落在对应族分支并在说明中声明 |
| 3 | 中 | `src/dalton/llm/providers/antigravity/oauth.py:56,203,423` | 模块级可变状态 `_LAST_INITIATED_AT` 被 `check_login_complete` 在请求路径读取；多 worker 下 start/poll 异进程 + Redis 故障时守卫失效（旧 token 行误判登录完成），字典无清理 | initiated_at 落 DB（provider_events）或 Redis 键（注释里已有 600s TTL 语义），移除进程内 dict |
| 4 | 低 | `tests/unit/dalton/test_budget_port.py:202,818` | 无 spec 的 LLM 客户端替身传给真实 `dalton_complete`（服务内部依赖均 create_autospec）。字面上不属于"替身=assist_json/dalton_complete/服务函数"的中危类，但客户端调用签名漂移不会被测试抓住 | 有具体客户端类则改 create_autospec；lead 可按 L33 严格口径升为中危 |
| 5 | 低 | `P0H-round2-dev-report.md` §2.3 | 正则清单 7 类中 9 个名字在代码里不存在（`_CODE_FENCE_RE`、`_URL_RE`、`_EN_DATE_RE`、`_BARE_YEAR_RE`、`_VERSION_RE`、`_TIER_CODE_RE`、`_CALC_EXPR_RE`、`_APPROX_PREFIX_RE`、`_NUMBER_TOKEN_RE`；代码栅栏是字符串逻辑 numbers.py:526，URL 用 `_MARKDOWN_LINK_RE`，裸年份是 `_STANDALONE_YEAR_RE`），另有 4 个名字与实际不符（`_HYPHEN_CODE_RE`→`_HYPHEN_DOC_CODE_RE` 等）；"冻结的 `_METRIC_SYNONYM_GROUPS`"从未存在 | 按实际标识符重写清单；报告数字类断言（行数、清单）逐项复核后再发布 |
| 6 | 低 | GitHub 流程 | 6 个族 0 个 PR（最近 100 个 PR 无 P0H/dalton 命中），本地 `--no-ff` 合并直推；证据只在提交说明/报告/gitignored `var/` | 是否补 PR 由 lead 决定；至少把各族证据摘要复制进 `docs/…/trial-runs/`（同清单 3.5 建议） |
| 7 | 低 | `web/src/pages/ChatAgent/components/dalton/PublishApprovalCard.tsx`、`Settings/panels/dalton/DaltonBudgetCard.tsx` | 新增 i18n 键 `dalton.publish.labelingIncompleteWarning`、`dalton.budget.byokCashNote` 在 zh-CN.json/en-US.json 均无条目，英文界面显示中文（dalton.* 族既有模式，本轮沿用）；组件测试 mock 了 useTranslation，测不出此问题 | 补 en-US（及 zh-CN）条目，或明确 Dalton UI 中文-only 的决策并让测试覆盖真实 t() |
| 8 | 低 | `src/dalton/autonomy/probes/internal.py:114-131` | 桥查询 3 路由串行、每路 8s 超时，桥不可用时最坏占预检 45s 总时限的一半以上（3 条 warning） | 三路由并行化，或对整步包 8-10s 总时限 |
| 9 | 观察 | `src/dalton/publish/**`（18 处） | `llm_service` 参数线仍在 publish 贯穿（清单 §1 的 grep 非空） | 归 guide-conformance 报告按 L30/D13 判定 |
| 10 | 观察 | `fix/P0H.H2-H6-realrun-evidence` | H2（环境启动）与 H6 合用分支；H2 实际仅 `agent_config.dalton.yaml` call_timeout_s 300→625 与 bridge 安装/服务脚本 | 可接受的偏差，按清单要求已在 §4.3.1 写明 |

**统计：中危 3（#1 报告数字不实、#2 证据提交夹带功能改动、#3 模块级状态），低危 5，观察 2。** 无严重、无高危。

## 疑似

- 问题 #4（裸客户端替身）按协调方规则存在两种读法，lead 裁量后可升中危。
- SF-CN 合成数字（#2 内嵌）是否构成 L28 违规（代码独立判"已核实"的新路径）由 R3-code 报告定性，本报告仅记录其存在与位置（match.py:312-330）。
- 问题 #1 的另一种可能：开发方用了某个未提交的中间工作区状态统计行数。即便如此，报告与提交说明两组毛数互斥这一点仍无法自洽，结论不变。
