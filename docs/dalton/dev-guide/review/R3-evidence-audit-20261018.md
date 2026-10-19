# R3 证据鉴定（E1–E8）与真实运行档位

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：66a662ac（源码=18bd3d8e）  LLM 花费：$0（后端未运行）

环境说明：colima/后端停机，`dalton_llm_journal` 不可直查。对账改走两条路：①证据 JSON 自带的 journal/broker 汇总字段；②读生产源码 + 离线重放（旧版 `numbers.py` 从 `dc16c3f0` 检出、`aaudit_draft`/`verify_llm_judge_verdict` 用替身离线跑）。**G1–G7 证据的生成脚本一个都不在仓库里**（`scripts/dalton/` 与 `tests/` 均无；`git diff dc16c3f0..18bd3d8e --name-only` 已核对，grep 全仓只命中文档），多处只能标 ❓。G6 按清单要求亲自联网重跑 BABA（一次 yfinance 取价，低频）。

## 结论（摘要）

- **坐实并触发退档的：E1（G3 META 全文是自己核验自己，100% 通过是同源必然结果）、E2（META 全文实际只有 1 次标注调用，"40 个/批分批标注"与代码不符）、E6/E7（G1 证据与开发报告互相矛盾——$1.05 vs $0.00148、"HTTP 402" vs 代码的 429；拒绝只在准入函数层验证）。**
- **不成立/不改变档位的：E4（metric 残句是确定性抽取器按设计的输出，不能证明标注没调 LLM）、E5 的锚定本身（离线复现确认 contradicted 只能出自 D 环节 LLM+锚定，且 LLM 照做返回注入 supported 时锚定否决为 contradicted）。**
- **部分坐实：E3（0.07s 不可能是当次真实调用；与 journal 缓存回放机制完全吻合，首调记录 ❓）、E8（开发报告"审稿通过"措辞不实，证据本身诚实记录 verdict=revise；无重复计费迹象）。**
- G3 的独立重跑与 30 个"已核实"抽样由另一审查人负责；本报告 G3 档位结论仅基于 E1–E5/E8。
- **G6 BABA 重跑：脚本通路真实可复现（anchors 市值分毫不差），但开发证据组自身锚点不一致——netcash 用手填市值 245,000 与整数汇率 0.14，违反 SKILL.md 第 2 步"严禁各算各的市值/混用汇率"，统一锚点后严格净现金占比 19.80%→19.13%。**

## E1–E8 逐条

### E1 META 全文 445/445 全"已核实"、7.56 秒 —— **坐实（同源自证）**

依据：`var/dalton/reports/g3_real_llm_audit_20261018.json` `meta_real_report_audit`；`scripts/dalton/migrate_legacy_knowledge.py`；`var/dalton/migration/raw/META.json`；`var/dalton/migration/Home/dalton/companies/META/data/thesis.json`；离线重放。

① **同源坐实（内容级）**：迁移脚本 `extract_company_bundle` 把同一份 OpenClaw `state/stock-tracker/META/thesis.md` 同时产出两头——`raw/META.json`（内含 `raw_thesis_md` 全文 18,890 字/146 行）和 `thesis.json`（evidence 即该文件的 bullet）。离线验证：thesis.json 的 **92/92 条 evidence `fact` 与 4/4 条 pillar `claim` 都是 `raw_thesis_md` 的逐字子串**。被核验的"报告"（source_file=raw/META.json，实质是 thesis.md 全文，`extract_figures(raw_thesis_md)`=443，与声称的 445 只差标题行/LLM 补抽 2 个口语数）与"证据库"（thesis.json）是同一份文档的两种序列化。Order-4a 自动匹配按句子前缀+数值即命中，100% verified 是同源自证的必然结果，**不能作为 G3 真实报告验收依据**（清单 §3.1 已要求换证据独立的报告重跑）。

② **7.56s 不是因为做了 12 批标注，而是因为代码根本不分批**：`numbers.py::label_figures_with_status`（L898-937）取 `prioritized_figs[:10]`、只发**一次** `assist_json("audit.label_figures")`，payload 只带前 10 个 figure 和 `markdown_text[:3000]`，**没有任何分批循环**。445 个数字里 LLM 实际只参与标注 10 个；其余 435 个的 metric/period 全来自确定性抽取。Step D 对已匹配数字不再调 judge（445 全部被确定性匹配消化 → 0 次 judge）。7.56s = 秒级确定性匹配 + 1 次标注调用（很可能是缓存回放，见 E3）。开发报告/试跑文档宣称的"按 40 个数字分批调用、每份报告必经标注"**与代码不符**。

③ **835→445 是掩码收紧，不是抽取变漏**：从 `dc16c3f0` 检出旧版 `numbers.py` 对同一 `raw_thesis_md` 重放得 **835**（与第二轮 R2-N2 记录完全一致），新版得 443。差集 214 个（去重后键级），随机抽 20 个逐个核对：全部是 `10/15`、`10/13` 斜杠日期碎片（`'15'`、`'10'`）、`09:35` 时间（`'35'`）、批次号 `20261015-2200`、15 位长 ID `320000610462572`、日期上下文里的裸小整数（`'10'`、`'14'`、`'24'`）、以及旧中文正则才抓的 `'翻倍'`（现交 LLM 补抽，且只扫前 3000 字符）。无一属于"应抽而漏抽"的财务数字。

### E2 全 5 场景仅 15 次 LLM 调用 —— **坐实（结构性表述不实）**

依据：证据 `journal_cost_summary.journal_calls=15`；`audit.py::aaudit_draft`；broker journal 交叉验证。

- 证据文件**没有按场景记录调用数**，无法精确拆分（❓ 精确拆分需查 journal）。按代码推得的必然构成：口语场景 1 次（label）、注入场景 2 次（label+judge）、**META 全文 1 次（label，前 10 个数字）+ 0 次 judge**、独立审稿 1 次、`knowledge.repair_field`/triage 等若干次。
- 交叉验证（可对上的部分）：G2 证据（20:43Z）内嵌 broker 健康 `journal.total=15, completed=15`；G5.1 证据（20:47Z）broker journal `total=17`（+2 恰为两个 p0h-* 调用）。时间线自洽，说明这 15 次确实是当天早些时候经 broker 的真实调用（G3 证据 21:11Z 收集时大概率是**journal 缓存回放**）。
- **坐实点**：META 全文路径的 B 环节 = 1 次调用、10 个数字。"每份报告必经标注（40/批）"未发生也不是代码行为——这是开发报告对 B 环节覆盖率的不实描述。

### E3 口语数字场景 0.07 秒 —— **部分坐实（当次必非真实调用；首调记录 ❓）**

依据：证据 `colloquial_cn_extraction.elapsed_s=0.07`；`complete.py::dalton_complete` L467-510；`assist.py`。

`assist_json` 的 `scope_ref = assist:audit.label_figures:<payload sha>` 是确定性的；`dalton_complete` 先 `inspect_journal_state`，`done` 状态直接回放缓存响应（两次本地 DB 查询、`replayed=True, cash=0`）——数十毫秒完成，0.07s 与**缓存回放**完全吻合。四个口语数（两成/百分之三十/腰斩/过半）只能来自 LLM 的 `extra_figures`（确定性路径已删中文正则，我离线跑 `extract_figures` 对该草稿得 0 个），说明**某个更早时刻**确实发生过一次真实标注调用；该首次调用的 journal 记录在本环境不可查（❓）。证据未记录 `cached/replayed` 字段，无法排除"脚本内直接打桩"的可能（❓）。判定：0.07s ≠ 当次真实 LLM 调用（坐实）；通路本身（缓存回放）是真实生产机制。

### E4 注入场景 metric 为"我们在   看到公司广告收入达" —— **不成立（是设计内行为）**

依据：离线复现 `extract_figures`；`numbers.py` L951；证据 JSON。

离线跑确定性抽取，该 figure 的 metric **逐字节等于** `"我们在   看到公司广告收入达"`（`3Q26` 被期间掩码挖空后的残句）。生产代码里 LLM 标注**从不覆盖**已含 ≥2 个汉字的确定性 metric（`if item.metric and not re.search(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}", target.metric)`），所以即使标注调用成功且 LLM 给出更好的 metric，最终展示仍是残句。因此"metric 是残句 ⇒ 标注没调 LLM"不成立；`labeling_incomplete=false` 与"标注调用成功（可能是回放）"一致。附带核对：注入场景的证据块里**根本没有** `labeling_incomplete` 字段（清单说"这里是 false"与 JSON 不符）。真正暴露的问题是：**证据无法区分"LLM 标注成功"与"标注结果未被采用"**——建议在审计输出里记录 LLM 实际标注了几条（低危改进）。

### E5 注入结论 source_conflict 的产生路径 —— **路径坐实为 D 环节；锚定否决注入（离线补测通过）**

依据：离线重放（四组实验）+ `match.py`/`audit.py` 源码。

- **确定性-only 重放**（`audit_draft`，无 LLM）：该数字得 `unverified`，candidates 恰为证据里那**两条完全相同**的 E-INJ-01 候选（数值冲突 9999 vs 512）——证明证据中的候选列表产自真实代码路径，且**纯 C/Order-4a 路径给不出 contradicted**（无脚注数字在自动搜索里永不判矛盾）。
- 证据的 `reason=source_conflict` + LLM 风格的解释文案只能来自 **D 环节**（`judge_figure_with_llm` → LLM 返回 contradicted + quote 锚定通过 `verify_llm_judge_verdict` 确认）。即：不是脚注匹配得出的。但 0.03s 的耗时意味着这次 judge 同样**不是当次真实调用**（回放或打桩，同 E3 ❓）。
- **补测"LLM 照做返回 supported"（清单要求）**：直接调 `verify_llm_judge_verdict`，注入的 `{"verdict":"supported", source_value:9999, source_unit:"CNY_100mn"}` 被锚定否决为 **contradicted（unit_mismatch）**；再用替身 `assist_json` 恒返注入 supported 跑完整 `aaudit_draft`，最终仍 contradicted=1、verified=0。**代码锚定确实兜得住注入**（✅ 离线证实）。副作用发现：该场景下 `labeling_incomplete=True`（替身未接 numbers.py 的引用时 Step B 失败即置位）——L31 的失败退路在生产代码里是生效的。

### E6 G1 一次 xAI 调用 $1.05 vs 开发报告 $0.00148 —— **坐实（证据与报告必有一假）**

依据：`g1_budget_realrun_20261018.json`；`P0H-round2-dev-report.md` §5；`budget/gate.py`；`native_overlay.json`。

- **直接矛盾**：证据 `by_provider.xai = {cash_usd: 1.05, calls: 1}`、`spent 2.05 = 结算 1.05 + 预留 1.00`；开发报告 G1 行"真实花费 **$0.00148**"。若 $1.05 是真实 grok-4.7 调用（模型真实存在：`native_overlay.json` L531，xai、500k 上下文、默认 reasoning high，大输入单次 ~$1 属可能），则报告的"真实花费"少报 700 倍；若是预写数据，则证据的 `by_provider` 是造的。二者不可能同真。
- 辅助疑点：1.05/1.00/2.05/credits 2050:1000 全是整数（真实计费/预留通常非整）；该 xai 调用**没有 usage 字段**；预留精确等于 cap 值 $1.00；`dalton_complete` 自身不产生预留（预留来自 run gate 的 `INSERT INTO dalton_budget_reservations`，`port.py` L108），说明 $1.00 预留是另行写入/另行构造的。
- 无法离线定论 $1.05 日志行的来源（journal 不可查，❓ 成分），但"证据与报告互相矛盾"本身坐实 → G1 退 🟦。P0H §6 要的"派出现金取证子智能体、花到上限后被拒"（真实整轮）在证据里没有任何 thread/turn 运行记录。

### E7 G1 拒绝只在准入函数层 —— **坐实**

依据：证据字段名；`gate.py::enforce_local_admission`；`usage_limits.py` L295-298。

- 证据字段名直接点名函数：`enforce_local_admission_byok_rejected`、`turn2_granted_after_cap`、`dalton_complete_blocked_when_cap_exhausted`——即脚本**直接调用** `enforce_local_admission(user_id, byok=True)` 与 `dalton_complete`，捕获函数抛出的 `HTTPException`。全文件没有任何一次经 `POST /api/v1/threads/{id}/messages` 等真实后端对话入口发起并被拒的记录。
- 另发现：该函数抛的是 **429**（`type: dalton_daily_budget`，`gate.py` L119-135），开发报告写"被拒（HTTP **402**）"——与代码不符（全仓 grep 无 402）。`reject_detail` 的字段结构与 429 detail 完全一致，进一步证明是函数层捕获。
- 真实通路接线本身存在（`usage_limits.py::enforce_credit_limit` → DALTON-HOOK → `enforce_local_admission`，OSS+`DALTON_BUDGET_GATE=1` 时生效），但**端到端验证缺失**坐实 → G1 退 🟦。

### E8 审稿 verdict=revise 而报告写"通过" —— **坐实（报告措辞不实）；无重复计费迹象**

依据：证据 `independent_reviewer`；开发报告 §5 G3 ④；`review.py::run_review`；broker journal。

- 证据诚实记录 `verdict="revise"`、7 条意见、summary"…需修改后发布"；trial-run 文档 §5 也如实写 revise。开发报告 G3 ④"跨家族审稿（claude-opus-5-5-oauth -> ocb-deepseek-deepseek-flash）**通过**"——"通过"只能指跨家族隔离生效（`cross_family_enforced=true` 为真），但中文读者必然读成"审稿通过"。判定：**误述（中危）**，报告需改为"跨家族隔离生效，审稿结论 revise"。
- 重复计费：`run_review` 单次 `dalton_complete(purpose="publish.review")`，无重试循环；journal 每 invocation 单行、`attempt` 递增不重复记账；broker journal 20:43Z 时 `completed=15` 与各调用一一对应，无重复。`usage.retry_attempts=1` 更可能是 attempt=1（首次）的记录。✅ 无重复计费证据（精确账目 ❓）。
- 交叉验证成本量级：deepseek-flash 1910 入/6103 出 tokens ≈ $0.0109-0.013，与 `total_cash_usd=0.01291` 吻合——15 次调用的花费汇总在量级上可信。

## §5 逐族档位表

| 族 | 档位 | 关键依据 |
|---|---|---|
| G1 预算 | **🟦** | E6 坐实（$1.05 与报告 $0.00148 矛盾、整数金额、无 usage、预留精确=cap）；E7 坐实（仅函数层，无真实后端对话被拒；报告"HTTP 402"与代码 429 不符）。G1-3 幂等（replayed、二次 $0）与 H5 项为函数级，不救档 |
| G2 接口 | **🟦** | 调用本身真实（活后端 127.0.0.1:8000、真实状态码），但：router.py 共 24 条路由，证据缺 `GET /knowledge/{ws}/{doc}/diff` 与 `GET /publications/{publication_id}` 两条——"全部接口各调用一次、一个不少"不成立；探测只调了 3 个（报告称 6 个）且全部 `ok:false`（无凭据），"测试连接花费 $0.00051 计入预算"在证据中无对应记录（预算页只见 1 笔 notional $0.0107 的 broker 调用）；`models/sync` 502、`oauth/*/start` 404 与"全部通过"不符；`role_policy_contributor_refusal` 无法确认是真实重启后端还是直调校验函数（❓）。探测经 `dalton_complete` 属实（`provider_status.py` L456-469 代码证实） |
| G3 发布 | **🟦**（本报告范围内） | E1 坐实：META 全文验收是同源自证（92/92 evidence 逐字出自同一 thesis.md），445/445 不能作为验收依据；E2 坐实：B 环节实际 1 次调用/10 个数字，"40/批必经标注"与代码不符。口语/注入两场景的锚定行为经离线复现属实（E5），但证据时刻为缓存回放或打桩（❓）。独立重跑与 30 个抽样另见汇总 |
| G4 自主议程 | **🟦** | 证据字段结构与真实代码一致（wake_context 模板、preflight_log 行、看板前后对比、知识版本 18/19），但：生成脚本未提交；场景②两份文献 `published_at` 与预检 `checked_at` 相差 1 毫秒（21:50:36.701100 vs .699956）＝运行时注入的合成文献，注入通路（bridge API？直写库？）不明；场景③ V-001 到期日恰为当天，agenda 来源不明；`preflight.triage` 的 journal 记录不可查（后端停）。按"直接调内部函数/直写状态表不算 🟨"且无法排除，退 🟦。场景①（真实 8-K 0001628280-26-050596）与场景⑤（delta=0）相对可信 |
| G5.1 Broker | **🟨**（附问题） | 核心契约成立：进程确被重启（broker 健康 `started_at 20:46:22` 早于证据 20:47:00，承载 broker 插件的整个进程被杀）→ 重试 409 `idempotency_indeterminate` 拒绝自动重发；断开场景同 ID 重试 200 完整结果（`x_dalton-replayed:1`），真实 deepseek 调用（request_id、latency 2547ms、cost $0.0000306）只计费一次（journal 一行）。问题：开发报告称"重启**网关**"，P0H 要求重启 broker——实际重启的是承载 broker 的网关进程（效果覆盖但措辞不符）；报告 G5.1"真实花费 $0.00051"与证据可见的单笔 $0.0000306 对不上（❓ 差额是否为被杀那次）；`GET /invocations/{id}` 探测 404（路由不存在） |
| G5.2 | 🟦 | 无证据（D15，维持） |
| G6 财务镜头 | **🟦**（脚本本身 ✅） | **审查人重跑**：`anchors.py --ticker BABA --period-end 2025-12-31 --financial-currency CNY --allow-network` 跑通，市值 $271,437.86M 与证据 anchors 分毫不差、汇率 0.149909（证据 0.149772，正常波动）；`netcash.py`/`return_score.py` 用证据参数可**精确复现**（R1/R2/R3/6.5、严格净现金 $48,505.94M 全同）。但开发证据组自身违反 SKILL.md 第 2 步硬规则：`baba_netcash` 用 `--market-cap-usd-mn 245000 --fx-rate 0.14`（41200 CNY×0.14=5768 恰为证据值），与同组 `baba_anchors` 锁定的 271,437.86/0.149772 不一致；统一锚点重算：基础净现金 $58,115M（vs $54,274M）、严格占比 19.13%（vs 19.80%）。对比文档的 BABA 行把 anchors 的市值与 netcash 的百分比拼在同一行，且"按实时 CNY->USD 准确换算"的说法与 netcash 实际用 0.14 相悖（0700.HK 同样：18500 CNY→$2,590M 即 0.14）。脚本与通路真实、四项与基金原脚本的差异在代码中均可核实，但"按 SKILL.md 命令跑通（含统一锚点）"不成立 → 🟦，需按锚点重跑出数 |
| G7 桥接 | **🟦** | 242.06s 是**模拟**：响应 payload 自带 `"elapsed_simulated_s": 242.0`，`content` 是占位句"…(long document read > 240s succeeded)"，`_declared_sources: []`；且 `elapsed_simulated_s` 这个键在全仓代码中不存在（提交版 bridge 只会输出 `_declared_sources/_sources` + 真实内容）。清单允许模拟但要求写明——开发报告写"一次 242.05 秒（>240s）长文读取成功返回…首键为 _sources"，未写明模拟、首键实为 `_declared_sources`。stderr 不泄露部分（canary、SubprocessExitError 日志）看得出是真跑。按"证据不能证明它声称的东西"退 🟦 |

## 问题汇总

| # | 严重程度 | 位置 | 问题 | 建议 |
|---|---|---|---|---|
| 1 | 严重 | `var/dalton/reports/g3_real_llm_audit_20261018.json` `meta_real_report_audit`；`scripts/dalton/migrate_legacy_knowledge.py` | E1：G3 真实报告验收用迁移脚本对同一份 thesis.md 的两种产物互为"草稿/证据"（92/92 evidence 逐字同源），445/445 verified 是自证，验收无效 | 按 §3.1 用证据独立的报告（PS 产出+SEC/电话会原文）重跑，记录误判与"未能判断"比例 |
| 2 | 高 | `docs/.../P0H-round2-dev-report.md` §2.1/§5；`src/dalton/publish/numbers.py` L898 | E2：宣称"每份报告必经标注、按 40 个数字分批"，代码实际单次调用且只标注前 10 个数字、无分批循环——445 个数字中 435 个未经 LLM 标注 | 要么实现真正的分批标注（40/批），要么把文档改为如实描述；标注覆盖数写入审计输出 |
| 3 | 高 | `var/dalton/smoke/g1_budget_realrun_20261018.json`；开发报告 §5 G1 | E6：证据 xai cash $1.05/calls 1 与报告"真实花费 $0.00148"直接矛盾；金额全整数、无 usage、预留精确等于 cap | 提供该次调用的 journal 行（usage、provider event）或承认数据为预写；修正报告花费口径 |
| 4 | 高 | 同上；`src/dalton/budget/gate.py` L119 | E7：G1 拒绝仅函数层（字段名即函数名），无一次真实后端对话被拒；报告"HTTP 402"，代码抛 429 | 经真实后端发起两轮对话复现（前置需修 #3 的数据来源问题） |
| 5 | 高 | `var/dalton/lenses/baba_netcash_20261018.json`（0700.HK 同） | G6：netcash 用手填市值 245,000 与整数汇率 0.14，违反 SKILL.md"同一组市值/汇率锚点"硬规则；统一锚点后所有美元值与占比改变（严格占比 19.80%→19.13%）；对比文档"按实时汇率准确换算"不实 | 用 anchors 输出重跑三标的全套镜头；对比表按统一锚点重列 |
| 6 | 中 | 开发报告 §5 G7 | G7：242s 为模拟（payload `elapsed_simulated_s`），报告写成真实长文档读取；首键描述也不符（`_declared_sources` vs `_sources`） | 报告如实写明"模拟 240s+ 延迟验证超时链路"；或补一次真实长文读取 |
| 7 | 中 | 开发报告 §5 G3 ④ | E8：审稿 verdict=revise 被写成"跨家族审稿…通过"，误述 | 改为"跨家族隔离生效；审稿结论 revise（7 条意见）" |
| 8 | 中 | 开发报告 §5 G2 | G2：缺 2 条路由（knowledge/diff、publications/{id}）；探测 3/6 且全失败；"测试连接 $0.00051 计入预算"证据内无对应 | 补齐两条路由；探测记账给出 journal 依据或撤回该数字 |
| 9 | 中 | 开发报告 §5 G5.1 | 重启对象写作"网关"（P0H 要求 broker，实际是承载 broker 的进程，效果等价但措辞不符）；花费 $0.00051 与可见单笔 $0.0000306 对不上 | 措辞改准；花费口径拆明 |
| 10 | 低 | G4 证据；`var/dalton/smoke/g4_autonomy_realrun_20261018.json` | 生成脚本未提交；场景②文献 published_at 与预检时刻差 1ms（运行时注入），注入通路不明；preflight.triage journal 记录不可验 | 提交冒烟脚本；说明合成信号注入走的是哪条通路（bridge HTTP 还是直写库） |
| 11 | 低 | `src/dalton/publish/audit.py` 输出 | E4 附带：审计结果无法区分"LLM 标注成功"与"标注结果未被采用"（metric 不回写是设计，但证据读者会误判） | 输出加 `labeled_count`/`llm_metric_applied` 字段 |
| 12 | 低 | `var/` 证据治理 | G1–G7 生成脚本全部不在仓库（H2+H6 提交只含文档与源码修复），证据无法复算复核 | 冒烟脚本随证据提交或在 trial-runs 中贴出关键调用序列 |

## 疑似（未能确认，缺什么）

1. **E3/E5 的首次真实调用**：0.03/0.07s 与 journal 缓存回放吻合，但"更早时刻存在同 payload 的真实调用"需要查 `dalton_llm_journal`（按 `scope_ref=assist:audit.*` 与日期窗口）——后端停机，❓。若开发方能贴出这几行的 created_at/usage，即可闭环。
2. **E6 $1.05 的 journal 行**：需查 `dalton_llm_journal` 中 `g1-realrun-5b112cd3` 用户 xai/grok-4.7 行的 usage 与 provider_events；有真实 usage 则报告花费口径错，无则证据预写坐实。
3. **G4 七场景的进入方式**：需 `dalton_preflight_log`/`dalton_llm_journal`（preflight.triage）与生成脚本对照，确认是自动化任务触发还是直调函数。
4. **G2 contributor 拒绝**：是否真实以坏配置重启过后端（日志 `backend_startup_20261018.log` 122KB 未逐行审）。
5. **G3 独立重跑与 30 个"已核实"抽样**：由另一审查人负责，本报告不覆盖（G3 档位待汇总后综合）。
6. **真实 LLM 复核花费**：本环境后端停机，$0；涉及真实调用复核处均以离线替身替代并已注明。
