# Dalton P0I 第三轮审查修正与真实运行证据重做开发报告 (`P0I-round3-dev-report.md`)

**日期：** 2026-10-19  
**基线提交：** `925fd0f9`（第三轮审查基线）  
**对应指南：** `docs/dalton/dev-guide/P0I-round3-fixes.md`、`docs/dalton/dev-guide/REVIEW-CHECKLIST-R3.md`、`docs/dalton/dev-guide/review/SUMMARY-R3-20261018.md`  
**开发流程：** 按 `L32` 与 `L38` 执行——一族一个分支（`fix/P0I.<族>-<短名>`）、一个 GitHub PR，全部在 GitHub 上合并入 `dalton/main`。

---

## 1. 八个修正族（`I4` → `I2` → `I3` → `I5` → `I6` → `I1` → `I7` → `I8`）PR 与行数统计总览

所有行数统计均由 `git diff --shortstat <基线提交> <合并提交> -- <目录>` 实测得出：

| 顺序 | 族 | 分支名 | GitHub PR | 基线提交 → 合并提交 | `git diff --shortstat <基线> <合并>` 实测输出 | 核心交付内容 |
|---|---|---|---|---|---|---|
| 1 | **I4** | `fix/P0I.I4-frontend-green` | PR #1 | `925fd0f9` → `46f42833` | `3 files changed, 57 insertions(+), 18 deletions(-)` | 修复 `PublishApprovalCard.test.tsx` 类型错误、补齐 `en-US.json`/`zh-CN.json` 双语键、新增真实 i18n `en-US` 渲染测试 |
| 2 | **I2** | `fix/P0I.I2-evidence-tools` | PR #2 | `46f42833` → `487497bc` | `5 files changed, 1574 insertions(+)` | 新增 `scripts/dalton/realrun/journal_dump.py`、`scripts/dalton/realrun/_common.py`、`scripts/dalton/check_report_claims.py` 及单测 `tests/unit/dalton/test_realrun_evidence_tools.py` |
| 3 | **I3** | `fix/P0I.I3-providers-startup-budget` | PR #3 | `487497bc` → `1a04cb71` | `7 files changed, 305 insertions(+), 19 deletions(-)` | 修复 `provider_status.py` 扁平 `api_keys` 读取、`enforce_role_policy_on_startup` 启动失败抛 `RolePolicyError`、启动调用 `prune_expired_journal_responses`、OAuth 冷却持久化至 `dalton_provider_events`、`complete.py` 补齐 `try/except`、`test_router_wiring.py` 补齐 `create_autospec` |
| 4 | **I5** | `fix/P0I.I5-autonomy-low-risk` | PR #4 | `1a04cb71` → `dc1fd7a4` | `8 files changed, 320 insertions(+), 18 deletions(-)` | 自主议程 7 项低危修复（`director_tools.py`、`probes/internal.py`、`preflight.py`、`probes/sec.py`、`schedule.py`、`dalton-agenda/SKILL.md`） |
| 5 | **I6** | `fix/P0I.I6-lenses-anchors-flag` | PR #5 | `dc1fd7a4` → `b2463c54` | `6 files changed, 465 insertions(+), 28 deletions(-)` | `netcash.py`、`return_score.py`、`earnings_quality.py` 新增 `--anchors` 参数并与手填参数互斥，重算 `BABA`/`0700.HK`/`META` 对比表 |
| 6 | **I1** | `fix/P0I.I1-publish-audit` | PR #6 | `b2463c54` → `58e85d4e` | `23 files changed, 7535 insertions(+), 821 deletions(-)` | 发布核验堵住错放（`L34`–`L36`）、全量按章节分批标注、引文量纲与期间锚定、`legacy_thesis` 隔离、章节级缓存、G3 金标准（`159` 条）与测试集入库 |
| 7 | **I7** | `fix/P0I.I7-realrun-evidence` | PR #7 | `58e85d4e` → `7464c4a6` | `39 files changed, 5874 insertions(+), 144 deletions(-)` | 入库 `scripts/dalton/realrun/g1_budget.py` 至 `g7_bridge.py` 共 7 个真实运行脚本，生成 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G1.md`–`G7.md` 与 `var/dalton/trial-runs/realrun-20261019/g*.json` |
| 8 | **I8** | `fix/P0I.I8-docs-and-report` | PR #8 | `7464c4a6` → 当前分支 | 本 PR | 更新 `PROGRESS.md`、`P0H-round2-dev-report.md` 勘误、本报告 `P0I-round3-dev-report.md`、`README.md` 合并 `L34`–`L38` |

按目录汇总的 `git diff --shortstat 925fd0f9 7464c4a6 -- <目录>` 实测输出：
- 全仓（`git diff --shortstat 925fd0f9 7464c4a6`）：`75 files changed, 16069 insertions(+), 987 deletions(-)`
- 发布核验模块（`git diff --shortstat 925fd0f9 7464c4a6 -- src/dalton/publish/`）：`6 files changed, 1583 insertions(+), 734 deletions(-)`
- 后端 `src/dalton/`（`git diff --shortstat 925fd0f9 7464c4a6 -- src/dalton/`）：`29 files changed, 2201 insertions(+), 846 deletions(-)`
- 真实运行与检查脚本（`git diff --shortstat 925fd0f9 7464c4a6 -- scripts/dalton/`）：`14 files changed, 5864 insertions(+), 8 deletions(-)`
- 前端 `web/`（`git diff --shortstat 925fd0f9 7464c4a6 -- web/`）：`4 files changed, 444 insertions(+), 26 deletions(-)`
- 插件 `plugins/dalton/`（`git diff --shortstat 925fd0f9 7464c4a6 -- plugins/dalton/`）：`5 files changed, 282 insertions(+), 19 deletions(-)`
- 单元测试 `tests/unit/dalton/`（`git diff --shortstat 925fd0f9 7464c4a6 -- tests/unit/dalton/`）：`5 files changed, 1339 insertions(+), 77 deletions(-)`

---

## 2. 各修正族详细说明

### 2.1 I4：前端回绿与国际化补齐（PR #1，`46f42833`）

1. **修复 `PublishApprovalCard.test.tsx` 类型错误**：补齐测试数据中的必填字段（不放宽 `PublishApprovalCard.tsx` 的组件 Props 类型定义）。
2. **双语键补齐**：在 `web/src/locales/en-US.json` 与 `web/src/locales/zh-CN.json` 中补齐 `dalton.publish.labelingIncompleteWarning`、`dalton.budget.byokCashNote` 以及第 1.7 节审批卡新增的 `dalton.publish.*` 双语键。
3. **真实 i18n 测试**：在 `web/src/pages/ChatAgent/components/dalton/__tests__/PublishApprovalCard.test.tsx` 中新增使用真实 `i18next` 实例加载 `en-US.json` 的渲染测试，断言英文界面下显示英文文案而非中文兜底值。

### 2.2 I2：真实运行证据规范与三项工具（PR #2，`487497bc`）

1. **`scripts/dalton/realrun/journal_dump.py`**：连接真实 Postgres，按 `--since`、`--until`、`--user-id`、`--purpose` 导出时间窗内的 `dalton_llm_journal` 记录及 `conversation_usages` 会话/子智能体调用明细，统计 `total_calls`、`fresh_calls` (`replayed=false`)、`replayed_calls` (`replayed=true`)、`cash_usd`、`notional_usd` 与 `combined_cash_usd`。
2. **`scripts/dalton/realrun/_common.py`**：提供统一证据信封构造器 `build_evidence_envelope`、非真实成分白名单校验 `validate_synthetic_declarations`（仅允许 `settings_api_budget`、`watch_state_unsee_filing`、`knowledge_api_agenda`、`bridge_delay_hook` 四类，非法类别直接抛 `ValueError`）以及摘要渲染器 `write_evidence_and_summary`。
3. **`scripts/dalton/check_report_claims.py`**：扫描开发报告与 `PROGRESS.md` 中反引号包裹的仓库文件路径、`var/` 证据路径以及代码标识符（函数名、类名、异常名、正则/常量名），核验其真实存在性。

### 2.3 I3：供应商探测、启动失败阻断与日志/冷却修复（PR #3，`1a04cb71`）

1. **供应商状态与探测读取用户 Key（`src/dalton/services/provider_status.py`）**：同时支持用户偏好表中的扁平结构 `{"api_keys": {"deepseek": "..."}}` 与嵌套结构 `{"byok": {"providers": [...]}}`；本机未配置的现金供应商返回明确原因。
2. **启动角色校验阻断（`src/dalton/llm/role_policy.py`、`src/server/app/setup.py`）**：`enforce_role_policy_on_startup` 在角色配置存在错误（如把 `assist` 配为 `*-contributor-oauth`）时直接抛出 `RolePolicyError` 阻断服务启动。
3. **30 天日志清理接入启动（`src/server/app/setup.py`）**：启动生命周期中调用 `prune_expired_journal_responses(retention_days=30)`，异常时打 warning 不影响启动。
4. **OAuth 登录启动冷却持久化（`src/dalton/llm/providers/antigravity/oauth.py`）**：删除进程内 `_LAST_INITIATED_AT` 字典，改为查询最近 60 秒内 `dalton_provider_events` 表中 `event_type = "oauth_initiated"` 的记录，并在发起登录时写入该事件。
5. **模型调用与路由测试补齐**：`src/dalton/llm/assist.py` 在 `assist_json` 失败降级时记 `logger.debug`；`src/dalton/llm/complete.py` 在 `complete_journal_done` 周围包裹 `try/except` 并在落盘异常时调用 `complete_journal_failed` 记为 `indeterminate`；`tests/unit/dalton/test_router_wiring.py` 的 `test_every_dalton_route_resolves_without_500` 对 `aaudit_draft` 改用 `create_autospec`。

### 2.4 I5：自主议程 7 项低危修复（PR #4，`dc1fd7a4`）

1. `src/dalton/tools/director_tools.py`：调用 `build_coverage_board` 时从 `config["configurable"]["user_id"]` 透传 `user_id`。
2. `src/dalton/autonomy/probes/internal.py`：桥接地址与令牌均未配置时按进程每小时最多打一次 `logger.warning`；将 `research`、`news`、`community` 三个路由改为 `asyncio.gather` 并行调用，整步超时 `10.0` 秒。
3. `src/dalton/autonomy/preflight.py`：`claim_signals_woken` 抛异常时返回 `decision="skip", reason="claim_failed"`，留待下一次预检重试；预算查询异常维持放行但日志级别改为 `logger.error` 并写明由 `dalton_complete` 准入层 fail-closed 兜底的注释。
4. `src/dalton/autonomy/probes/sec.py`：CIK 解析链全部失败时打 `logger.warning` 并在 `watch_state` 中记录 `sec_probe_degraded: True`，由 `src/dalton/autonomy/board.py` 在覆盖看板上透出。
5. `src/dalton/autonomy/schedule.py` 与 `plugins/dalton/skills/dalton-agenda/SKILL.md`：模板与代码统一 `cooldown_seconds: 86400`；财报前 3 个交易日（`08:00`）与财报后 1 个交易日（`20:00`）改用 `_offset_trading_days` 跳过周末计算。

### 2.5 I6：财务镜头结构化统一锚点（PR #5，`b2463c54`）

1. `plugins/dalton/skills/dalton-financial-lenses/scripts/netcash.py`、`return_score.py`、`earnings_quality.py` 新增 `--anchors <anchors JSON 路径>` 参数，自动读取 `anchors.py` 输出的市值、汇率与期间，并在输出中标注 `anchors_source: "anchors_file"`。
2. 同时传入 `--anchors` 与手填 `--market-cap-usd-mn` 或 `--fx-rate` 时立即报错退出（exit code 2）；手填模式保留但标注 `anchors_source: "manual"`。
3. 更新 `plugins/dalton/skills/dalton-financial-lenses/SKILL.md` 与 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md`：统一锚点后 `BABA` 严格净现金占市值比由 `19.80%` 更新为 `19.13%`。

### 2.6 I1：发布核验——堵住错放、全量标注与引文确定性锚定（PR #6，`58e85d4e`）

1. **无脚注数字核实路径收紧（`L34`、`src/dalton/publish/match.py`）**：
   - 移除 C 环节中无脚注数字的自动放行：Order 0b（表格行匹配）、Order 4（数值单位期间匹配）以及无脚注目标价不再设置 `matched=True`，仅将命中条目挂载为 `candidate_sources` 交由 D 环节（LLM 判断 + 代码引文锚定）裁决。
   - 删除 `SF-CN` 跨币种特例与死函数。
   - 收紧 Order 5（`apply_derived_figure_matching`）：操作数必须已核实、非比率量纲兼容、比值运算要求操作数期间与目标数字一致、容差收紧至半个末位（`0.5 * 10**(-decimals)`）。
2. **旧认知隔离（`L34`、`src/dalton/knowledge/schemas/thesis.schema.json`、`scripts/dalton/migrate_legacy_knowledge.py`）**：
   - `thesis.schema.json` 的 `evidence[].source.type` 新增 `"legacy_thesis"`；迁移脚本将 `sources/legacy/thesis.md` 下的旧条目标为 `source.type = "legacy_thesis"`，C/D 环节跳过 `legacy_thesis` 作为核实依据，仅作为候选展示（`"旧认知中的说法，尚未用原始来源核实"`）。
3. **B 环节全量按章节分批标注与 LLM 优先（`L35`、`src/dalton/publish/numbers.py`）**：
   - `label_figures_with_status` 按 Markdown 章节切分、每批最多 `40` 个数字调用 `assist.publish.label_figures`，覆盖草稿全部数字与全文。
   - LLM 返回的 `metric`、`period`、`kind` 优先于代码启发式（启发式仅在 LLM 未返回对应字段时兜底）。
   - 新增 `"incidental"`（非核验对象）类型及确定性守卫 `_can_be_incidental`：凡带货币、`%`、`bps`、`x`、量级 `> 1`、`per_share` 或表格 provenance 的数字禁止被排除为 `incidental`。
4. **D 环节引文确定性锚定与消除越权判矛盾（`L28`、`L36`、`src/dalton/publish/audit.py`）**：
   - `retrieve_candidate_sources` 在候选数 `<= 30` 时全量送入，超出时按 `(footnote_match, value_match, period_match, metric_match, raw_hit)` 排序取前 8 条。
   - `verify_llm_judge_verdict` 强制执行四项确定性检查：① `quote` 逐字存在于候选文本；② 引文内数字与草稿数字数值一致；③ 量纲一致（区分 `currency`、`percent`、`bps`、`multiple`、`per_share`）；④ 若引文含期间标记则归一化后必须与草稿期间一致。任一检查不通过则降为 `uncertain`。
   - 脚注路径上只有数值确实不同才判 `contradicted`，期间或量纲不一致判 `unverified`。
5. **章节级核验缓存（`src/dalton/publish/numbers.py`、`src/dalton/publish/audit.py`）**：
   - 按章节切分 `label_figures_with_status` 与 `judge_figures_batch_with_llm` 请求并经 `assist_json` 写入 `dalton_llm_journal`（`inspect_journal_state` 按 `(user_id, purpose, model_key, request_hash)` 命中缓存），改写单节时未改动章节零重复调用。
6. **`src/dalton/publish/numbers.py` 正则清单（`L36` / `L38`）**：
   - 通过 `grep -n "^_[A-Z0-9_]*_RE\b" src/dalton/publish/numbers.py` 实测，文件中共有 26 个模块级正则常量，本轮**新增 0 个正则**，仅按 `P0I §1.4` 修改了 `_PERIOD_TOKEN_RE`（支持两位年份前缀 `26Q4`/`26H1` 与中文期间 `2026 上半年`/`下半年`/`全年`/`财年`）和 `_CN_DATE_RE`（支持不带日的中文月份 `4月`/`10月份`）：
     `_PERIOD_TOKEN_RE`、`_SEC_ACCESSION_RE`、`_HYPHEN_DOC_CODE_RE`、`_ISO_DATE_RE`、`_CN_DATE_RE`、`_SHORT_DATE_RE`、`_SLASH_DATE_RE`、`_CLOCK_TIME_RE`、`_LONG_ID_INT_RE`、`_BATCH_CODE_RE`、`_YEAR_RANGE_RE`、`_STANDALONE_YEAR_RE`、`_FILING_FORM_RE`、`_TICKER_CODE_RE`、`_ENTITY_ID_RE`、`_FOOTNOTE_DEF_RE`、`_FOOTNOTE_REF_RE`、`_MARKDOWN_LINK_RE`、`_INLINE_CODE_RE`、`_HEADING_PREFIX_RE`、`_LIST_PREFIX_RE`、`_STRUCTURAL_COUNT_RE`、`_EXEMPT_MARKER_RE`、`_NUMBER_RE`、`_UNIT_PAREN_STRIP_RE`、`_COLLOQUIAL_HINT_RE`。

---

## 3. I7：按 L37 规范重做 G1–G7 真实运行证据汇总（PR #7，`7464c4a6`）

全部 7 个脚本均已入库于 `scripts/dalton/realrun/`，针对真实运行中的后端服务（`http://127.0.0.1:8000`）、OpenClaw Broker（`http://127.0.0.1:18881`）与 Researchdata 宿主机桥（`http://127.0.0.1:8960`）执行，生成的摘要入库于 `docs/dalton/dev-guide/trial-runs/realrun-20261019/`，原始 JSON 与截图保存在 `var/dalton/trial-runs/realrun-20261019/`：

| 族 | 入库脚本 | 摘要路径 (`docs/dalton/dev-guide/trial-runs/realrun-20261019/`) | 原始证据路径 (`var/dalton/trial-runs/realrun-20261019/`) | 原始证据 `sha256` | 新调用 / 回放 (`fresh` / `replayed`) | 合计现金花费 (`combined_cash_usd`) | 状态 |
|---|---|---|---|---|---|---|---|
| **G1 预算** | `scripts/dalton/realrun/g1_budget.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G1.md` | `var/dalton/trial-runs/realrun-20261019/g1_budget.json`、`var/dalton/trial-runs/realrun-20261019/g1_budget_exhausted_banner.png` | `a70ea63b51f9bef4c2f48f45a44ab9faa40d3d5a1d66a03a59f037ecd162138d` | `2` / `1`（另含 `5` 轮对话，子智能体 `deepseek-flash` 用量 `> 0`） | `$0.022249` | 🟨 |
| **G2 接口与探测** | `scripts/dalton/realrun/g2_providers.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G2.md` | `var/dalton/trial-runs/realrun-20261019/g2_providers.json` | `dc736110e589b37d60448e91068a7b4f5ba2978529682e3900b89fd76f3f6c3c` | `3` / `0`（含现金供应商 `deepseek` 探测成功） | `$0.000015` | 🟨 |
| **G3 发布核验** | `scripts/dalton/realrun/g3_publish.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G3.md` | `var/dalton/trial-runs/realrun-20261019/g3_publish.json` | `5c9c2249cf0592512d504e493ee39f6dafe97af23dc81ebd211f50920a3bfac7` | `78` / `1` | `$0.000000`（名义 `$0.214313`） | 🟨 |
| **G4 自主议程** | `scripts/dalton/realrun/g4_autonomy.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G4.md` | `var/dalton/trial-runs/realrun-20261019/g4_autonomy.json` | `62a2a3349ad8d3dde0fd45aa817f3847e8a1925f6481b63025fa9cc1345861c5` | `2` / `1`（另含 `9` 轮自动任务与对话，`assist.preflight.triage` `replayed=false`） | `$0.010144` | 🟨（场景②内部文献子项如实标 `🟦`） |
| **G5.1 Broker** | `scripts/dalton/realrun/g5_broker_and_antigravity.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.1.md`、`docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.md` | `var/dalton/trial-runs/realrun-20261019/g5_broker_and_antigravity.json` | `a2e155583294f7c9af97d5474e2862d2161d125700f06a58209b33d2716ada30` | `8` / `0` | `$0.000531` | 🟨 |
| **G5.2 Antigravity** | `scripts/dalton/realrun/g5_broker_and_antigravity.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.2.md`、`docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.md` | `var/dalton/trial-runs/realrun-20261019/g5_broker_and_antigravity.json` | `a2e155583294f7c9af97d5474e2862d2161d125700f06a58209b33d2716ada30` | `8` / `0` | `$0.000531`（Antigravity OAuth 现金 `$0.00`） | 🟨 |
| **G6 财务镜头** | `scripts/dalton/realrun/g6_lenses.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G6.md` | `var/dalton/trial-runs/realrun-20261019/g6_lenses.json` | `0dc12374f13d4851bfcb0b0612be7f9a3d749f666c19853b16d22bf5d23f3c42` | `0` / `0`（纯确定性 Python 财务脚本） | `$0.000000` | 🟨 |
| **G7 宿主机桥** | `scripts/dalton/realrun/g7_bridge.py` | `docs/dalton/dev-guide/trial-runs/realrun-20261019/G7.md` | `var/dalton/trial-runs/realrun-20261019/g7_bridge.json` | `1266a7d8b256759e612b8582668b88e6310e4c7fd89cecaa1606626f45460db3` | `0` / `0`（桥接 MCP HTTP 实测） | `$0.000000` | 🟨 |

### 3.1 G3 验收指标逐项对照（`P0I §1.8`）

证据来源：`docs/dalton/dev-guide/trial-runs/realrun-20261019/G3.md` 与 `var/dalton/trial-runs/realrun-20261019/g3_publish.json`（`sha256: 5c9c2249cf0592512d504e493ee39f6dafe97af23dc81ebd211f50920a3bfac7`）：
1. **GOOGL 金标准范围（`tests/fixtures/dalton/g3/gold_standard.json` 共 `159` 个数字，覆盖 `6` 个章节）**：
   - 错放（金标准为 `unverified`/`contradicted`/`exempt`，系统判为 `verified`）：**`0` 个**（标准要求 `0`）。
   - 真阳召回（金标准为 `verified` 共 `61` 个，系统判为 `verified`）：**`61 / 61 = 100.0%`**（标准要求 `>= 75%`）。
   - 错判有矛盾（金标准非 `contradicted`，系统判为 `contradicted`）：**`0` 个**（标准要求 `0`）。
   - 第三轮审查 30 个抽样中的 10 个错放与 2 个锚定盲区：**`0` 个重现**（全部回归锁定）。
2. **GOOGL 金标准范围外（其余章节，共 `355` 个数字）**：
   - `labeled_count == total_figures`（`514 / 514 = 100%`）。
   - 排除为 `incidental` 的数字：**`0` 个带货币、百分比、bp、倍数或每股单位**。
   - 随机固定种子（`seed=20261019`）抽样 `25` 个判为 `verified` 的数字人工逐条核对：**错放 `0` 个**（明细见 `G3.md` 第 3.2 节）。
3. **AMZN 全文（`74` 个数字，`7` 个章节）**：
   - `labeled_count == total_figures`（`74 / 74 = 100%`），排除为 `incidental` 的数字中带财务单位的为 `0` 个，判为 `verified` 的 `24` 个数字人工核对错放为 **`0` 个**。
4. **交叉对照（GOOGL 草稿 × META 证据包 `tests/fixtures/dalton/g3/meta_cross_control_evidence.json`）**：
   - 判为 `verified` 的数字：**`0` 个**（标准要求 `0`）。
5. **S1–S5 合成注入**：
   - `S1`（编造引文）：被 `verify_llm_judge_verdict` 拦截并降为 `uncertain`。
   - `S2`（真正数值矛盾 `$96,469M` vs `$84,742M`）：判为 `contradicted`。
   - `S3`（口语中文数字 `"增长约两成"`、`"毛利率过半"`）：抽出并判为 `unverified`。
   - `S4`（数字仅存在于 `legacy_thesis`）：判为 `unverified`，候选列表展示 `"旧认知中的说法，尚未用原始来源核实"`。
   - `S5`（显式豁免标记 `[常识豁免]`）：判为 `exempt`。
6. **单节改写缓存复用**：
   - 仅改写 GOOGL 草稿第 1 节并二次调用 `POST /api/v1/dalton/publish/{workspace_id}/check`：未改动章节命中 `dalton_llm_journal` 缓存，新调用数从首次 `42` 次降至 `1` 次，现金花费 `$0.00`。

---

## 4. 四条基线验证命令与 `check_report_claims.py` 实测输出

### 4.1 挂钩检查（`./scripts/dalton/check_hooks.sh --strict --audit`）
```text
OK: DALTON-HOOK check passed.
```

### 4.2 Python 单元测试（`uv run pytest tests/unit/dalton/ -q --disable-socket --allow-unix-socket`）
```text
160 passed, 2 warnings in 29.57s
```
（此外：`uv run pytest tests/unit/dalton/test_router_wiring.py -q --disable-socket --allow-unix-socket` 实测 `28 passed`；`uv run pytest tests/unit/dalton/ tests/unit/llms/ tests/unit/middleware/skills/ tests/unit/mcp_servers/ -q --disable-socket --allow-unix-socket` 实测 `1698 passed`。）

### 4.3 Broker v2 单元测试（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs`）
```text
ℹ tests 13
ℹ suites 0
ℹ pass 13
ℹ fail 0
```

### 4.4 Web 前端类型检查与单元测试（`pnpm --dir web typecheck && pnpm --dir web test -- --run`）
```text
$ tsc -b
$ vitest run -- --run
Test Files  677 passed (677)
     Tests  7228 passed (7228)
```

### 4.5 报告与进度表声明核验（`uv run python scripts/dalton/check_report_claims.py --strict docs/dalton/dev-guide/PROGRESS.md docs/dalton/dev-guide/P0I-round3-dev-report.md`）
```text
=== Dalton Report & PROGRESS Claim Check ===

File: docs/dalton/dev-guide/PROGRESS.md (verified=184, missing_identifiers=0, missing_repo_paths=0, missing_var_paths=0)

File: docs/dalton/dev-guide/P0I-round3-dev-report.md (verified=197, missing_identifiers=0, missing_repo_paths=0, missing_var_paths=0)

Summary: files=2 missing_identifiers=0 missing_repo_paths=0 missing_var_paths=0 all_clean=True
```
