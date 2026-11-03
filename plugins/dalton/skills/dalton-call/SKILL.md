---
name: dalton-call
description: "经六条证据门禁硬检后产出高确信度投资建议：含驱动级预期差三问、可证伪主张、三情景赔率卡与胜率裁决、仓位与对冲及退出条件（未达门禁则附赔率卡如实汇报缺口）。用户需要将研究转化为建仓/加仓/做空建议时使用；几分钟内可完成。"
---

# 高确信度投资建议（Conviction Call）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 0. 核心纪律：宁缺毋滥，不达门禁绝不硬凑

Conviction Call（高确信度投资建议）是 Dalton 研究体系的最高级别输出，直接服务于 PM 的建仓/加仓/做空决策。**任何时候只要 6 条证据门禁中有 1 条未满足，必须停止高确信推荐，改用《门禁未通过声明模板》向 PM 如实汇报缺哪块证据、正在等待什么验证信号，并附上已完成的赔率卡与三层保护，同时把降级后的 `actionability`（行动分类）与完成的赔率卡写入 `thesis`（`underwriting` + `house_view`）；没有高确信不等于不交付赔率分析，严禁为了交差而强行拼凑推荐。**

---

## 1. 九步标准流程（9-Step Execution Flow）

### Step 1：通读知识层与近期底稿
按顺序读取公司工作区：
1. `agent.md`（受管区块）、`thesis.md`、`debate.md`、`drivers.md`、`management.md`、近 30 天 `verdicts.md`。
2. `model/` 下最新财务模型（重点读 `Street vs House`、`Drivers`、`DCF` / `Valuation` 表）。
3. `published/` 下最近一次首次覆盖报告、多空辩论（Bull/Bear）及最新季报复盘（Earnings Review）。

### Step 2：六条证据门禁硬检（6 Hard Evidence Gates）
先把三情景赔率卡、三层保护、方向与行动分类拟入 `thesis`（完整结构化字段与 JSON 样例见 `.agents/skills/dalton-knowledge/references/structured-write-example.md`），调用 `knowledge_update(doc="thesis", dry_run=True, ...)` 查看赔率计算摘要（含三情景价格区间、达标终值、两行测试权重所需概率、全部权重组合是否都过最低回报要求、最不利情景相对基准股价的不利变动幅度、估值倍数变动占目标回报比例与基准股价是否过期），再逐条对照 [references/evidence-gate.md](references/evidence-gate.md) 检查 Gate 1 至 Gate 6：
- **Gate 3 双轨检查**：
  - **路径 A（盈利预期差）**：在至少 1 个核心经营驱动变量（Driver）上，House Case 与 Street Consensus 存在明确的数字化差异，并能解释错误定价存续原因；
  - **路径 B（倍数 regime 切换，五条条件必须全部满足）**：① 点名是**增长持续期、资本强度 / 现金转化、资本配置、尾部折价**四类中的哪一类发生变化，并给出可观测的领先指标；② 给出**同一份盈利预测下**新旧估值倍数的价格桥，盈利变动贡献与估值倍数变动贡献分开列示；③ 给出**历史或同业估值倍数锚，并说明为什么适用于当前环境**；④ 存在 **1–4 个季度内**可观测的证伪或去折价催化剂节点与量化阈值；⑤ **同一原因不能在 EPS 和估值倍数里各算一次**（此外当估值倍数变动对目标回报贡献超过 50% 时，除非重估机制已在当期或近期现金流中实际兑现〔`underwriting.rerating_observed=true` 且附 `rerating_sources`〕，否则 `conviction` 最高只能标 `medium`）。**“估值终将回到历史均值”仍判 `FAIL`**。
- **Gate 5 赔率与高确信路径检查（直接取 `knowledge_update(doc="thesis", dry_run=True)` 返回的赔率计算摘要，严禁心算）**：
  - 所主张方向的 `underwriting.breakeven_verdict` 中每条 `verdict` 必须为 `"clearly_above"`（显著高于门槛），且不利方向价带端点相对基准股价的亏损幅度在上限之内（`act_now` 默认 ≤ 25%，空头须同时具备逼空压力情景 `underwriting.stress` 且借券成本 `underwriting.borrow_cost_annual` 已知）；
  - 若拟给 `conviction="high"`（高确信），必须满足两条路径之一：① **有来源支撑的权重区间整体达标**（`weight_bounds` 有历史基率或二元事件来源支撑、`rationale` 不得为风险中性期权口径，且赔率计算摘要显示全部权重组合都过最低回报要求，包括对所主张方向最不利的那组权重）；或 ② **不需要乐观情景兑现即可达标**（在 `b=25%` 与 `b=50%` 两行测试权重下，赔率计算摘要显示价格区间下端〔多头〕或上端〔空头〕也无需 bull/bear 即可达标，且满足立即行动的不利波动上限）；并且若估值倍数变动占目标回报比例 > 50%，重估机制必须已在现金流中兑现（`underwriting.rerating_observed=true` 且附 `rerating_sources`）。
- **Gate 6 支柱健康度检查**：
  - 近 30 天的 `verdicts.md` 中无未处理的 `THESIS_BROKEN`；
  - **主驱动支柱不得为 `impaired`（受损）或 `broken`（证伪）**；其他非主驱动支柱若处于 `impaired` 状态，仅在**已在三情景估值中全额扣减其受损影响、且在过去 30 天内已经重估过**时允许通过。
- **若任意一条 Gate 为 `FAIL`**：
  1. 停止生成正式高确信推荐草稿，改用 [templates/call.md](templates/call.md) 底部的 **《门禁未通过声明模板》**，写明未通过的具体 Gate（门禁）、缺失证据及预计补齐时间/事件，并**附上已完成的赔率卡与三层保护**（三情景价格带、所需胜率与裁决、三层保护与失效条件、降级后的行动分类与限制变量、双向改判条件，数字与 `thesis` 完全一致）。
  2. 写**门禁结果文件** `research/<YYYY-MM-DD>-call.gates.json`：`{"passed": false, "failed_gates": [{"gate": <1–6>, "reason": "<缺哪块证据、为什么不过>"}], "actionability_after": "<wait_evidence | wait_price | pass>", "as_of": "<YYYY-MM-DD>"}`——至少一条失败项、每条都有原因，并用 `actionability_after` 记录降级后的行动建议（取值限 `thesis` 的 `actionability` 降级枚举 `wait_evidence` / `wait_price` / `pass`）；这是完成契约核对门禁未过分支的依据，没有它或 `failed_gates` 为空都不算完成。
  3. **先写 `agenda` 与 `verdict`（门禁失败的写入顺序不可颠倒）**：调用 `knowledge_append_verdict` 记录五词结论（**verdict 按论点的实际变化从五词中选**——支柱与预期差确实未动填 `NO_CHANGE`；本轮证据削弱/强化了论点就如实写 `THESIS_WEAKENED` / `THESIS_STRENGTHENED`，触发证伪或立新论点则写 `THESIS_BROKEN` / `NEW_THESIS`），并在 `event` / `rationale` 里写明是哪条门禁未通过与缺失的证据；同时通过 `agenda_ops`（或先调用 `knowledge_update(doc="agenda", ...)`）将缺失证据对应的带日期验证项写入 `agenda` 根级的 `next_validations` 或 `wake_conditions`（拿到条目 ID，如 `"V-001"` 或 `"C1"`）。
  4. **再更新 `thesis`（v2 工作区写完整结构化降级，v1 旧档兼容不强升 v2）**：
     - 若工作区的 `thesis` 已是 v2（或新建 v2），在 `agenda` 带日期条目落库后调用 `knowledge_update(doc="thesis", ...)`（无论五词结论是否为 `NO_CHANGE`）：把已完成的赔率卡与三层保护写进 `thesis.underwriting`，把降级后的行动分类（`house_view.actionability` 设为 `wait_evidence` / `wait_price` / `pass`；`wait_evidence` 时 `trigger_refs` 指向刚写入 `agenda` 的带日期条目 ID）、限制变量（`binding_constraint`）、确信度（`conviction`）与双向改判条件（`flip_conditions.upgrade_if` / `downgrade_if`）写进 `thesis.house_view`。先 `dry_run=True` 核对赔率计算摘要，再传 `dry_run=False` 正式提交。
     - **v1 旧档兼容**：若工作区数据库里的有效 `thesis` 还是纯 v1 旧档（`schema_version == 1`，不含任何 v2 字段），一旦往 `thesis` 写任何一个 v2 字段就会触发整份 `thesis` 的 v2 全量校验；因此门禁未过时**不要往 v1 的 `thesis` 里单独加 v2 字段**——在门禁结果文件 `research/<YYYY-MM-DD>-call.gates.json` 中写明 `"actionability_after"`（`wait_evidence` / `wait_price` / `pass`），并把降级结论与赔率卡完整写在《门禁未通过声明》报告正文里（若五词结论非 `NO_CHANGE`，只更新 `thesis` 的 v1 字段如 `pillars`、`evidence`、`house_view.summary`）。
  5. 到此结束，不写正式推荐草稿。
- **仅当 Gate 1–6 全部 `PASS` 时**，把 `research/<YYYY-MM-DD>-call.gates.json` 写成 `{"passed": true, "failed_gates": [], "as_of": "<YYYY-MM-DD>"}`，进入 Step 3。

### Step 3：预期差三问（Variant Perception — 3 Questions）
必须用**经营驱动变量（Driver 单位，而非笼统的“市场低估了增长”）**或**倍数 regime 切换机制**回答三问：
1. **市场相信什么？**（当前股价与一致预期隐含的具体 Driver 水平或估值定价假设，如 `2027E 云业务增速 18%、营业利润率 32%`，同步写入 `thesis.market_view`）
2. **实际情况是什么、为什么？**（我们模型中的 Driver 水平或定价规则切换机制及背后的微观机制/产业证据，如 `实际可达 25% / 36%，因为高毛利推理工作负载占比在 2026Q2 已跨过 30% 拐点`）
3. **为什么错误定价至今仍存、何时因何催化剂纠正？**（从惯性外推 / 拥挤交易 / 旧叙事锚定 / 近因偏差 / 卖方覆盖盲区中指明原因，并给出带具体日期窗口与置信度的重估催化剂）

### Step 4：可证伪主张（Falsifiable Claims，2–5 条）
列出支撑本次 Call 的 2–5 条核心可证伪主张，每条必填 7 列：
`主张｜今日证据｜证据标签（事实/公司表态/一致预期/模型推导/判断/假设）｜若为真的财务/估值含义｜检验指标｜证伪条件（量化阈值）｜揭晓时间窗口`

### Step 5：三档情景与 `thesis.underwriting` 赔率卡计算
1. 构建 **Bear / Base / Bull** 三档情景并写入 `thesis.underwriting`（字段结构见 `.agents/skills/dalton-knowledge/references/structured-write-example.md`）：
   - 每档 `states[]` 必须写明：情景名称 `name`（`bear` / `base` / `bull`）、成立与失效条件 `condition`、计算定义 `calc`（`method="eps_pe"` 时填目标期 `period`、盈利口径 `basis`、EPS 区间 `eps`、估值倍数区间 `multiple` 与来源引用 `sources`；完整覆盖有已登记模型时优先用 `method="reference"`，填 `price_range` 与指向 `data/models.json` 已登记模型工作表单元格的 `ref`，如 `"model/META-model-v01.xlsx#Valuation!G18"`，或指向 `thesis.evidence` 中计算类证据的 `ref`），以及期限内每股现金回报 `cash_return_per_share` 与关联分歧 `debate_ids`。
   - **情景权重区间可选**（`weight_bounds`）：仅在有客观依据（历史基率或二元事件，填 `bear`、`base`、`bull` 区间与 `rationale`、`sources`；若为风险中性期权隐含概率，必须在 `rationale` 中写明“风险中性”且仅作展示参考，不能支撑 `high` 确信）时填写，严禁编造无来源的概率点值。
2. **先调用 `knowledge_update(doc="thesis", dry_run=True, ...)` 查看赔率计算摘要**（严禁心算）：
   - 直接读取返回摘要中的：三情景价格区间与含现金终值、达标终值、两行固定测试权重（`base` 权重占 25% 与 50%）下的盈亏平衡所需概率（中点与自洽端点展示区间）、全部权重组合是否都过最低回报要求、最不利情景相对基准股价的不利变动幅度、估值倍数变动占目标回报比例及标签、基准股价是否过期。
   - 将上述计算结果填入 `thesis.underwriting.breakeven_verdict` 与报告正文的赔率卡，确认无误后在收尾传 `dry_run=False` 正式写入 `thesis`（正文数字与 `thesis.underwriting` 严格一致，不另算一套）。

### Step 6：仓位级别建议（Sizing Tier）
对照 [references/sizing.md](references/sizing.md) 给出 **`Core` / `Tactical` / `Small`** 三档之一的级别建议与理由：
- **越权红线**：分析师只输出仓位级别（Tier）与证据匹配度理由，**严禁输出具体买入股数、美元金额或组合精确百分比**（除非 PM 已在基金 `sizing.md` 中预填了各档百分比映射规则）。具体组合权重与硬止损线属于 PM 决策权。

### Step 7：对冲方案（Hedging）
明确区分“我们要赚的 Alpha”与“不想承担的敞口”，给出可执行对冲设计：
- **拟对冲风险**（板块 Beta / 大盘利率敏感度 / 单一原材料或汇率 / 竞争对手配对）
- **对冲工具 / 配对标的**（行业 ETF、同组空头配对 Peer Pair、或期权结构）
- **对冲成本、基差风险（Basis Risk）、残余风险与对冲失效情景**
- **替代方案**：显式对比“直接缩小仓位（Down-sizing）”与“上对冲”的优劣。

### Step 8：退出与监控机制（Exit & Monitoring）
1. **三类退出边界**：止损复核线（Stop-loss review level）、基本面重估线（Reassessment level）、时间止损（Time stop：若至 `<YYYY-MM-DD>` 催化剂仍未兑现则无条件复盘退出）。
2. **监控表（6 列齐全）**：`触发事件｜监控指标｜量化阈值｜数据来源｜日期或窗口｜正反两边各怎么做（若达标如何加仓/持有 vs 若破线如何减仓/止损）`（同步写入 `thesis.house_view.flip_conditions`）。
3. **“什么会让我们改变主意”**：用一段话清晰定义空头（或反方）获胜的标志。

### Step 9：撰写草稿、质检、更新知识层与发布审批
1. 按 [templates/call.md](templates/call.md) 写入 `drafts/<YYYY-MM-DD>-call-<TICKER>.md`（赔率卡数字直接取 `knowledge_update(doc="thesis", dry_run=True)` 的计算摘要）。
2. **第 1 轮语言审查**：调用 `check_language(draft_path="drafts/<YYYY-MM-DD>-call-<TICKER>.md", round=1)`（只标问题），读 `<草稿>.language.md` 清单并逐条修改草稿（只改语言表达，不动事实、数字、术语、引用编号与判断强度）。工具报错不算通过，不得跳过。
3. **第 2 轮语言审查（终稿）**：内容定稿后、`check_draft` 之前，调用 `check_language(round=2)` 并按清单完成终稿语言修改；草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核（记录追加在同一 json），使最后一条第 2 轮记录绑定终稿哈希。本次产出中其他给人读的 .md 笔记（research/ 检索与对账笔记等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
4. 确认报告末尾 `## 引用` 表存在且每个 `[引用N]` 都能在表里找到（指向 `evidence:`、`model:`、`prov:` 或 `f:`），调用 `check_draft` 检查格式与禁用词。
5. 先按需调用 `knowledge_update` 更新 `drivers`、`agenda`（及 `models`、`debate`，确保 `thesis` 引用的 ID 已落库），再调用 `knowledge_update(doc="thesis", dry_run=False, ...)` 正式写入 `thesis`（含 `underwriting`、`house_view`、`market_view`），并调用 `knowledge_append_verdict`（`verdict="NEW_THESIS"` 或 `"THESIS_STRENGTHENED"`）。
6. 调用 `publish_report(draft_path="drafts/<YYYY-MM-DD>-call-<TICKER>.md", title="<公司名>（<TICKER>）Conviction Call", kind="conviction_call", slug="call-<TICKER>", updates_thesis=False)` 提交 PM 审批。

### 收尾门禁：汇报完成前必须过完成契约（`dalton_check_done`）

- 向 PM 汇报“完成”前（两个分支都要），必须调用 `dalton_check_done(workflow="dalton-call", params={"ticker": ..., "as_of": "<本稿日期 YYYY-MM-DD，与草稿文件名一致>"})`。
- 契约按本轮的门禁结果分两支核对：
  - 本轮的 `research/<日期>-call.gates.json` 为 `passed=false`＝**门禁未过分支**，要求：`failed_gates` 至少一条且各带原因、本轮调用 `knowledge_append_verdict` 追加了五词结论之一（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`，按论点实际变化选择，上一轮的记录不算数）、且当工作区 `thesis` 为 v2（或结论非 `NO_CHANGE`）时本轮已用 `knowledge_update(doc="thesis", ...)` 更新 `thesis`（v2 工作区写入降级后的 `house_view.actionability` 与已完成的 `underwriting` 赔率卡；若工作区 `thesis` 仍为 v1，不单独加 v2 字段，在门禁结果文件写明 `actionability_after` 并把降级结论与赔率卡写在报告正文里，结论非 `NO_CHANGE` 时只更新 `thesis` 的 v1 字段）；这一支不要求草稿和语言审查。
  - 门禁结果为 `passed=true` 或本轮没有门禁未过的记录＝**门禁全过分支**，要求：`drafts/<日期>-call-<TICKER>.md` 是本轮新写的完整草稿、`check_draft` 侧车哈希与当前稿一致、且 `<草稿>.language.json` 含两轮语言审查（第 2 轮哈希与终稿一致）。
  - 两支的条件都不满足＝无法判定门禁结果，记为 `missing`。
- `done=false` 时按 `missing` 先补齐再查（有草稿但 check_draft 用了旧草稿就对最终稿重跑 `check_draft`；语言审查缺轮次或第 2 轮哈希对不上就补跑对应轮次的 `check_language`；门禁未过就补写 `research/<日期>-call.gates.json`〔含 `actionability_after`〕、先补记本轮的五词结论 `verdict` 与 `agenda` 待验证项，再按 v2/v1 规则用 `knowledge_update` 更新 `thesis`）；补不了的缺项只能汇报“未完成”并列出缺项与原因，不得汇报“完成”。
