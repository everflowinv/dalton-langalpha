# R2 N7 skill、财务镜头、桥接、挂钩检查 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：4fd04531（源码=0e68b9a8）
本轮 diff：b42da6a7..0e68b9a8。对照材料：REVIEW-CHECKLIST-R2.md §4 N7、review/R7-financial-lenses-20261018.md、review/R3-data-sec-provenance-20261018.md、P0G-review-fixes.md G6/G7 族。全程只读：未改任何代码/配置/文档/数据库，未登录供应商，未打印令牌（桥 env 只 grep 非 TOKEN 行）。对 EDGAR 未发请求（本轮未用到）；yfinance 联网路径未执行（标 ❓）。对本地桥 127.0.0.1:8960/healthz 做了一次只读探测。

## 结论

N7 范围内**没有严重/高危问题**。第一轮本模块的问题（R7 财务镜头 3 高 + 4 中、R3 桥接 2 中、G6/G7 的文案与挂钩项）全部修到位，离线可验证的部分逐一实跑通过。遗留 1 个中低（check_skill_refs 的清单是写死的 denylist，不是对照真实注册表，新增 ghost 名漏报）和 2 个低危（桥接超时表与 P0-D.1 §274 长文清单的残留差异；桥 caller_budget 代码默认值与规格默认值分叉）。第一波 H15 的"grep 无结果"表述不精确（实际有 2 条文档命中，都是"不要写 max_failures"的反向说明），实质结论（无次数门）成立。

## 逐项（1–7）

### 1. check_skill_refs.py — ✅（退出码）/ ❌（清单来源）

- **只报告不阻止 ✅**：`main()` 恒返回 0，docstring 明写 "Always exits 0 (report-only, non-blocking)"。实跑 `uv run python scripts/dalton/check_skill_refs.py` → `Scanned 22 skills: ghost_hits=0, broken_refs=0, header_notes=0`，exit=0。未接进 check_hooks.sh（P0G 允许"手动运行，或者放进 check_hooks.sh"二选一）。
- **工具清单来源 ❌（中低，问题 #1）**：`GHOST_PATTERNS`（`check_skill_refs.py:23-34`）是写死的 11 条 denylist（x_search、firecrawl__firecrawl_scrape、dalton_sec 的 4 个旧名、knowledge_update(target=、RunWorkflow(args=、2 个 ghost skill 名），**不是**从真实注册表抽名比对。P0G G6.1 的设计是"抽出工具名、MCP 函数名、斜杠命令，和真实注册的工具清单比对"。第一轮出现过的 `research_alphaengine`、`guidepoint__search_events`、`alphaengine__download_document` 都不在 denylist 里——本轮已人工改净（见下），但将来再写进去脚本不会报。
- **人工抽查 3 个 skill ✅**：dalton-topic-research、dalton-earnings-preview、dalton-stock-move 全文扫（代码块、表格、正文）里出现的每个工具名都对照真实注册表核实：
  - `researchdata.research/finance/catalog/community` ✅（data_sources.json categories：finance/macro/news/research/community + catalog，`researchdata_mcp.py` tool_catalog 按分类生成）；
  - `dalton_sec.get_company_facts / list_filings` ✅（`plugins/dalton_data/dalton_sec_mcp_server.py:967-1030`，仅这两个工具）；
  - `x_api.search_posts` ✅（`plugins/alternative_data/x_mcp_server.py:310`，server 名 x_api）；`web_search`/`web_fetch` ✅ 原生；
  - `knowledge_update`/`knowledge_append_verdict`/`check_draft`/`publish_report` ✅（`src/dalton/tools/knowledge_tools.py:156,195`、`publish_tools.py:34,58`）；`dalton_coverage_board`/`dalton_record_plan`/`dalton_agenda_note`/`dalton_schedule_automation` ✅（`director_tools.py`）。
  - 另对全 22 个 skill 做了 `工具__工具` 双下划线 MCP 风格全库 grep：0 命中（第一轮的 `guidepoint__search_events`、`alphaengine__download_document` 类写法已清零）。

### 2. 财务镜头中危 4–7 逐一核对 — 全部 ✅（离线实跑）

脚本目录 `plugins/dalton/skills/dalton-financial-lenses/scripts/`，单测 `tests/unit/dalton/test_financial_lenses.py` 41 项全过。

- **#4 CCC 缺输入返回 null ✅**：复现第一轮场景 `earnings_quality.py --ni 100 --cfo 90 --avg-assets 1000 --revenue 500 --ar 100` → `{"dio_days": null, "dso_days": 73.0, "dpo_days": null, "ccc_days": null, "applicable": false, ...}`，不再编造 73 天总额。三项齐全时正常计算（`--cogs 300 --inventory 60 --ap 40` → ccc_days 97.33）。`compute_ccc` 的实现（`earnings_quality.py:114-168`）对缺失分项返回 None，docstring 与 output-schema.md §6 的说明一致。
- **#5 受限现金始终扣除 ✅**：`netcash.py --cash 1000 --restricted-cash 500 --non-free-cash trapped:200 --st-debt 300` → basic 1200、strict 500、`non_free_cash_deductions_usd: 700.0`（restricted 500 + trapped 200，严格口径共扣 700，第一轮只扣 200 的问题已修）。`netcash.py:160-179` 在传入自定义 non-free 项时仍自动插入 restricted_cash 扣减；显式豁免必须带理由：`--include-restricted-cash-in-free` 不带 `--restricted-cash-free-reason` 直接报错（exit 2），带理由则 include=false、strict=1300，与"除非明确标注不扣并写明理由"的口径一致。
- **#6 扣除项保留 value_local ✅**：输出项结构 `{item, value_local, value_usd, amount_usd, include, reason}`（`_normalize_deduction_item`，`netcash.py:35-68`），实跑确认两项都带 `value_local`（500.0 / 200.0）。
- **#7 output-schema.md 与实际输出一致 ✅**：
  - anchors：顶层 11 键、fx/listing_fx 各 8 键、market_cap 12 键与文档 §2 逐键一致（实跑 `--ticker 0700.HK --financial-currency CNY --listing-currency HKD --fx-rate 0.149772 --listing-fx-rate 0.127427 --price-local 417.8 --shares 9005060400 --shares-unit raw`）。fx=CNYUSD=X、listing_fx=HKDUSD=X（PS.2 港币 bug 的正确双汇率），price_usd=417.8×0.127427=53.239。
  - netcash：文档 §3 示例用相同输入实跑**逐数字复现**（basic 42063.0、strict 40363.0、cash_quality moderate、各 breakdown 一致），说明文档确实由真实输出重新生成。
  - return_score：文档 §5 score 块的 inputs 8 键 + R1/R2/R3/R/driver/base_score/adjustment/adjustment_raw/adjustment_clamped/adjustments/final_score/max_score/rationale_zh 与实跑完全一致；rationale_zh 文本逐字一致。
  - **market_cap 单位口径 ✅**：主键 `market_cap_usd_mn`（百万），`value_usd` 美元原值，`market_cap_usd` 明确标注为百万兼容别名；netcash 侧新增 `--market-cap-usd-mn` 参数 + 单位错配自动纠偏（`netcash.py:189-209`：市值与资产负债表量级差 100 倍以上时自动 ÷1e6 或 ×1e6），第一轮"差 1e6"的陷阱有防护。
  - 残留（记录，非本轮承诺范围）：return_score 的 rationale_zh 单条调整时仍有 "+0.5（+0.5（理由））" 嵌套写法（`return_score.py:106-121`，外层是钳制后合计、内层是分项，多条调整时有意义）；这是第一轮 #8（中低）的遗留，G6.2 只承诺修 #4–#7。#8 的主体（可信度档位表 + `--credibility-reason`）已修。

### 3. 桥接（scripts/dalton/researchdata_bridge.py）— ✅

- **stderr 不再落日志 ✅**：`_run_stdio_rpc` 里 stderr 捕获后直接弃用（`stdout_bytes, _stderr_bytes = await ...`，line 240）；无 JSON 响应时只记 `exit_code=%s`（line 271-275）并抛 `SubprocessExitError`——其消息只含 exit_code、docstring 明写 "never carries stderr"。两处旧泄漏点（原 564-566 healthz、723-725 启动探测）现输出 `type(exc).__name__ + exit_code`（line 609-614、772-777）。diff 确认旧的 `err_tail = stderr_bytes.decode(...)[-400:]` 已删除；另有 2 处 `logger.warning(..., exc)` 收窄为 `type(exc).__name__`（line 185、380）。外层兜底（line 744-745）也只记异常类型名，500 响应体是固定文案。
- **读全文 600 秒 + 调用方预算钳制 ✅**：`LONG_READ_TIMEOUT_S=600`，`LONG_READ_OPERATIONS`（read/read-item/get_document/download_document 等）+ `ROUTE_TIMEOUTS` 的 route:op 键（company-wiki:read、alphaengine:get_document、sec:read-item 等）给 600；`compute_timeout` 的 `ceiling = max(10, caller_budget_s − 25)` 把一切超时钳在调用方预算内（line 198-221）。部署实例核实：`~/.config/dalton/researchdata-bridge.env` 权限 600、`DALTON_BRIDGE_CALLER_BUDGET_S=300`（未打印 TOKEN 行），`GET /healthz` → `{"ok": true, server_version 1.3.0, max_inflight 2, caller_budget_s 300, alphaengine_get_document_today {calls:0}}`——即默认配置下实际上限 275s，与 P0-D.1 §276 "默认都在 275 秒以内；要跑满 600 需把 call_timeout_s 与本值一起调到 660" 的口径一致。
- **残留（低危，问题 #2/#3）**：P0-D.1 §274 长文清单里的 guidepoint 全部操作、transcript 整条路由、sec 的 search-text/attachments/financials 在实现里仍是 240 默认；`transcript:get_transcript`/`transcript:read` 是死键（transcript 路由真实 op 值是 `transcript` 或缺省）。代码默认 `CALLER_BUDGET_S=625` 与 P0-D.1 规格默认 300 分叉（部署 env 已显式设 300，线上行为正确）。

### 4. check_hooks.sh --strict --audit — ✅

- **基线自动计算 ✅**：`MERGE_BASE="$(git merge-base HEAD upstream/main 2>/dev/null || echo 8e0dce94)"`（line 88-89）。本机实测 `git merge-base HEAD upstream/main` = `8e0dce94...`（upstream 指向 ginlix-ai/LangAlpha），与回退常量恰好同值——常量只在无 upstream ref 时兜底，不是写死基线。
- **实跑 ✅**：`--strict --audit` exit=0，输出 `NEW_IN_UPSTREAM_DIR (0)`、`UNHOOKED_EDIT (0)`、`HOOKED_EDIT (37)`，全部 DALTON-HOOK id 都在注册表，13 个 P0 必需 hook 齐全，4 个受保护上游配置文件未被改动，docstring lock 只增 dalton_* 键。与第一波结论一致（我独立复跑）。
- **dalton_paths.txt 逐行审（22 行）✅**：plugins/dalton/、plugins/dalton_data/、src/dalton/、scripts/dalton/、config/dalton/、docs/dalton/、integrations/openclaw/、tests/unit/dalton/、两个 dalton_sec 测试路径、agent_config.dalton.yaml、docker-compose.dalton.yml、dalton_registry.py、web 的 6 个 dalton 专属路径——全部是 dalton 自有内容。三个可疑点逐一排除：`workflows/` 在 upstream/main 只有 `.gitkeep`（现内容为 6 个 dalton workflow，不含任何上游文件）；`web/src/pages/Settings/hooks/` 整个目录 upstream 不存在（3 个文件全是 dalton 新增）；`useOpenClaw.ts`/`useProviderStatus.ts` 文件名带上游色彩但均为本轮新建、只被 dalton 面板引用。**没有上游路径被错误豁免。**
- 附带：脚本内 `AUDIT_EXEMPT_FILES`（7 个文件）也核过——对 base 的 diff 全是 dalton 的小改动（测试字典加 dalton_sec 注册行、i18n 加键、nav 测试），且 docstring lock 另有 additive-only 专项检查兜底，不构成豁免漏洞。

### 5. wiki 覆盖报告 — ✅

- **脚本只读 ✅**：`wiki_coverage_report.py` 对 vectors.db 用 `mode=ro&immutable=1` + `PRAGMA query_only=ON`（line 231-232）；config.json 只读 `aliases` 映射（`_load_wiki_aliases_only`，docstring 明写 never reads any keys/tokens）；唯一写点是报告自身路径。
- **抽查 5 家公司 ✅**（报告 §3 明细 vs `~/.openclaw/workspace/wiki/companies` 实际目录）：

  | 公司 | 报告（正文页/总md/最近日期） | 实测 | 一致 |
  |---|---|---|---|
  | GOOGL | 7 / 8 / 2026-10-11 | 7 / 8 / 2026-10-11 | ✅ |
  | BABA | 34 / 35 / 2026-09-11 | 34 / 35 / 2026-09-11 | ✅ |
  | TENCENT（0700.HK） | 1 / 2 / 2026-07-16 | 1 / 2 / 2026-07-16 | ✅ |
  | PDD | 38 / 39 / 2025-08-25 | 38 / 39 / 2025-08-25 | ✅ |
  | ATAT | 23 / 24 / 2026-09-02 | 23 / 24 / 2026-09-02 | ✅ |

  缺失 3 家（MCO/NDAQ/NTES）实测确无目录；总量 174 公司目录 / 27 行业目录实测一致。

### 6. H15/H16/H17–H19 人工复核

- **H15 实质 ✅，第一波表述需修正（问题 #4）**：三条 grep 我逐一复跑。(a) `max_failures`：**有 2 条命中**，均在文档且都是反向说明（dalton-agenda/SKILL.md:57 "不要写 max_failures"、automation-templates/README.md:3 "不设置 max_failures"），不是门逻辑——第一波"grep 无结果"的字面表述不准确。(b) `最多委派|每日最多|max_delegations`：0 命中 ✅（研究总监 SKILL.md 的 Step 3 已改为"受当天的现金额度约束"）。(c) 迁移产物 `2026-07-29` 命中均带真实 accession（META 8-K 0001628280-26-050596），属 S5 范围的已溯源日期。新式"计数后停止"扫描：`max_retries: 5`（muse client）是 HTTP 传输层重试，`compute_failure_backoff_seconds(failure_count)` 是按时间退避（H9 允许的形态），无新增次数门。
- **H16 ✅**：自己复跑脚本（ghost=0 broken=0 header=0 exit 0）+ 第 1 项的 3 skill 人工抽查，双重确认。
- **H17 ✅（离线口径）/ ❓（联网口径）**：`anchors.py --ticker 0700.HK --json` → "Non-US ticker requires explicit --financial-currency" exit 2；`--ticker BABA` → "ADR / foreign issuer requires..." exit 2；写死汇率表已删（`fetch_fx_rate_to_usd` 只认显式传入或 `--allow-network` 实时取，yfinance 异常不再 `except: pass` 而是 ValueError 带类型名）。`--allow-network` 的真实汇率获取路径未执行（Yahoo 不在允许的低频公开只读范围内）→ ❓。META 离线路径（USD identity）✅。
- **H18 ✅**：`netcash.py --input netcash_input.json --json` 按新参数实跑成功（支持文件路径/stdin/内联 JSON；anchors 块里的 fx.rate 与 market_cap.usd_mn 可直接喂入）。
- **H19 ✅**：SKILL.md 第 4 步命令的每个 flag（--ni、--cfo、--cfi、--avg-assets、--assets-begin、--assets-end、--revenue、--cogs、--inventory、--ar、--ap）都在 argparse 里，实跑通过，期初期末资产自动求平均。

### 7. skill 文案本轮改动 — ✅

对 diff b42da6a7..0e68b9a8 里 plugins/dalton 的全部 SKILL.md/references/agents 增行逐类扫描：

- **写死模型名 ✅ 无新增**：唯二的 `model:` 增行在 agents frontmatter（dalton-evidence → grok-4.7、dalton-reviewer → gpt-6.1-sol-oauth），两个 id 都在 `src/dalton/dalton.yaml` 的 `publish.reviewer_models` 与 `native_overlay.json` 里注册；reviewer 的模型由 `scripts/dalton/apply_role_models.py` 按跨家族策略（`select_independent_reviewer`）维护——是 H5 修复的产物而非违规写死。
- **编造诱导 ✅ 无**：增行里没有任何"缺数就估/编一个/自行填"类指令；出现的都是反向纪律（如"严禁使用写死的旧汇率""缺任一分项时返回 null"）。
- **L22/L23/L26 矛盾 ✅ 无**：增行零 PDF 承诺（宽窄两种模式 grep 均空），与 L26（删除 PDF 发布承诺）一致；研究总监改为预算驱动排程，与 L22/L24（额度用完整轮拒绝）方向一致；发布文案未引入"代码一刀切"式表述（L23 口径未动）。
- 其余增行全是 G6.1 承诺的修复，且与真实接口对齐（我逐一核实）：ghost 工具名 → 真实路由；`RunWorkflow(args=` → `params=`；`knowledge_update(target=` → `doc=`；ghost skill 名（dalton-initiate-full→dalton-full-coverage、dalton-earnings-review→dalton-earnings-deep）；日文假名"第ス三节"、繁体"針對/與"修为简体；branch-industry.md 的相对引用改回真实路径；agenda 的自动化 JSON 示例改用平台真实字段（name/description/status/trigger_type/next_run_at/timezone/instruction/workspace_id/thread，无 max_failures、无 feishu）；`delegate_to_analyst(question=, workspace_id=, report_back=True)` 与 `src/tools/secretary/chief_of_staff.py:87-105` 的真实签名一致。

## 问题（按严重程度）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 中低 | `scripts/dalton/check_skill_refs.py:23-34` | ghost 清单是写死的 11 条 denylist，不是从真实注册表抽名比对（P0G G6.1 的设计是 allowlist 比对） | 将来有人在 SKILL.md 写 `alphaengine__download_document` 或新的未注册工具名，脚本仍报 ghost_hits=0（本轮已人工清零的旧名也多不在 denylist 里，防复发能力弱） | 生成清单时从 `~/.openclaw/workspace/config/data_sources.json` 的 categories、`src/dalton/tools` 的 @tool 注册、`plugins/*/mcp.json` 抽出真名，对 `` `工具名(` `` 模式做 allowlist 差集；保持 report-only、exit 0 不变 |
| 2 | 低 | `scripts/dalton/researchdata_bridge.py:43-57, 71-82` | P0-D.1 §274 长文清单未完全落地：guidepoint 全部操作、transcript 整条路由、sec 的 search-text/attachments/financials 仍按 240s 缺省；`transcript:get_transcript`/`transcript:read`（:73-74）是死键——transcript 路由的 operation 枚举只有 `transcript`，600 档永远触发不了 | 一份 transcript 全文读取超过 240 秒时在 240s 被杀，返回 `status: timeout`（默认 275s 调用方预算下余量仅 35s，触发概率低但与决议不一致） | transcript 路由缺省改 600 或补 `transcript:transcript` 键；guidepoint/sec 三个长文 op 按决议补 600；或请 owner 修订 P0-D.1 §274 的口径，二选一 |
| 3 | 低 | `scripts/dalton/researchdata_bridge.py:148-150` | 代码默认 `DALTON_BRIDGE_CALLER_BUDGET_S=625`，P0-D.1 §229 规格写默认 300 且"必须等于 call_timeout_s"（当前 300） | 部署时若漏设该 env（如换机器重装），桥会按 625 放行到 600s，而调用方 300s 先断，子进程空跑浪费并发槽位 | 代码默认改回 300，或注释注明 625 仅在 call_timeout_s=660 的部署下正确 |
| 4 | 低（记录） | 第一波结论表述 | H15 报告"grep 无结果"不精确：`max_failures` grep 实际 2 条命中，均为"不要写 max_failures"的反向文档说明 | 仅报告准确性；实质判定（无次数门逻辑）不受影响 | SUMMARY-R2 汇总时把 H15 表述改为"无次数门逻辑（grep 仅命中反向说明）" |

## 已确认没问题的关键点

- 桥接：stderr 全链路（启动/healthz/tools/call/外层兜底）只记异常类型名与 exit_code；令牌 hmac.compare_digest、404 不回显；只绑 127.0.0.1；env 文件 600；桥上 AlphaEngine get_document 当日用量记账正常。
- 财务镜头：netcash 双口径与自动插补受限现金、豁免需理由；市值单位错配自动纠偏（1e6 量级差检测）；anchors 双汇率方向（fx=报表币种、listing_fx=交易币种）与 price_usd 换算正确；CCC 三分项缺一即 null。
- check_hooks.sh：merge-base 自动计算并实测解析；--strict --audit 通过；dalton_paths.txt 22 行无一上游路径；AUDIT_EXEMPT_FILES 7 个文件的改动均琐碎且另有专项检查兜底。
- wiki 覆盖报告：脚本对 vectors.db 与 config.json 严格只读；5 家抽查全对；174/27 总量属实。
- skill 文案：无新增写死模型名、无编造诱导、无 PDF 承诺；所有接口写法与真实注册表/真实签名一致；agents frontmatter 模型 id 均在目录内且 reviewer 由跨家族策略脚本维护。
- 单测：test_financial_lenses.py 41 项通过（本模块范围）。

## 疑似问题（未能确认）

- ❓ anchors.py `--allow-network` 的 yfinance 实时汇率/metadata 路径未真跑（规则限制不访问 Yahoo）；离线路径与异常路径已全验。
- ❓ 桥接对真实上游（Guidepoint 长访谈、company-wiki 大全文）在 240s 档位下的实际耗时分布未测（需消耗全文配额，R3 审查时的同样保留项）——问题 #2 的实际影响取决于此。
- ❓ G6.2 要求的"BABA、0700.HK、META 按 SKILL.md 命令跑通 + 与基金原脚本对数"只完成了离线与结构验证（参数可跑、键名/单位正确、0700.HK 双汇率显式传入正确）；带真实行情与真实 filing 数值的端到端对数属第 5 节真实运行证据，未在本轮执行。
- ❓ `agent_docstring_lock.json` 与 skills 文案的一致性由 --strict 的 additive-only 检查覆盖，未逐 docstring 人工比对（R3 已覆盖）。
