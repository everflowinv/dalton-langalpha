---
name: dalton-call
description: "生成高确信度投资建议（Conviction Call）：先做 6 条证据门禁硬检（不满足则诚实输出“暂无高 conviction”而绝不硬凑），通过门禁后完成预期差三问、2–5 条可证伪主张、三档情景与 scenario_irr.py 概率加权 IRR 计算、仓位级别（Core/Tactical/Small）、对冲方案及退出与监控条件，经 PM 审批后更新知识层。触发词：conviction call、推荐报告、高确信度建议、买入建议、做空建议、/call、/dalton-call。"
---

# 高确信度投资建议（Conviction Call）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 0. 核心纪律：宁缺毋滥，不达门禁绝不硬凑

Conviction Call 是 Dalton 研究体系的最高级别输出，直接服务于 PM 的建仓/加仓/做空决策。**任何时候只要 6 条证据门禁中有 1 条未满足，必须输出“暂无高 conviction”模板，明确说明缺哪块证据、正在等待什么验证信号，严禁为了交差而强行拼凑推荐。**

---

## 1. 九步标准流程（9-Step Execution Flow）

### Step 1：通读知识层与近期底稿
按顺序读取公司工作区：
1. `agent.md`（受管区块）、`thesis.md`、`debate.md`、`drivers.md`、`management.md`、近 30 天 `verdicts.md`。
2. `models/` 下最新财务模型（重点读 `Street vs House`、`Drivers`、`DCF` / `Valuation` 表）。
3. `published/` 下最近一次首次覆盖报告、多空辩论（Bull/Bear）及最新季报复盘（Earnings Review）。

### Step 2：六条证据门禁硬检（6 Hard Evidence Gates）
逐条对照 [references/evidence-gate.md](references/evidence-gate.md) 检查 Gate 1 至 Gate 6：
- **若任意一条 Gate 为 `FAIL`**：
  1. 停止生成正式推荐，改用 [templates/call.md](templates/call.md) 底部的 **《暂无高 conviction 声明模板》**，写明未通过的具体 Gate、缺失证据及预计补齐时间/事件。
  2. 调用 `knowledge_append_verdict`（`verdict="NO_CHANGE"`），通过 `agenda_ops` 将缺失证据登记入 `agenda.md` 的验证计划（若需 PM 拍板研究优先级，填 `pm_handoffs`）。
  3. 到此结束，不写正式推荐草稿。
- **仅当 Gate 1–6 全部 `PASS` 时**，进入 Step 3。

### Step 3：预期差三问（Variant Perception — 3 Questions）
必须用**经营驱动变量（Driver 单位，而非笼统的“市场低估了增长”）**回答三问：
1. **市场相信什么？**（当前股价与一致预期隐含的具体 Driver 水平，如 `2027E 云业务增速 18%、营业利润率 32%`）
2. **实际情况是什么、为什么？**（我们模型中的 Driver 水平及背后的微观机制/产业证据，如 `实际可达 25% / 36%，因为高毛利推理工作负载占比在 2026Q2 已跨过 30% 拐点`）
3. **为什么错误定价至今仍存、何时因何催化剂纠正？**（从惯性外推 / 拥挤交易 / 旧叙事锚定 / 近因偏差 / 卖方覆盖盲区中指明原因，并给出带具体日期窗口与置信度的重估催化剂）

### Step 4：可证伪主张（Falsifiable Claims，2–5 条）
列出支撑本次 Call 的 2–5 条核心可证伪主张，每条必填 7 列：
`主张｜今日证据｜证据标签（事实/公司表态/一致预期/模型推导/判断/假设）｜若为真的财务/估值含义｜检验指标｜证伪条件（量化阈值）｜揭晓时间窗口`

### Step 5：三档情景与 `scenario_irr.py` 概率加权收益计算
1. 构建 **Bear / Base / Bull** 三档情景：
   - 每档必须写明：**核心 Driver 水平 → 传导至目标年 EPS / FCF → 目标估值倍数（或 DCF 单元格引用，如 `DCF!G28`）→ 目标股价**。
   - 给出三档概率（总和必须等于 `1.0`；若因事件呈二元分布等原因确实无法诚实赋概率，必须显式标注 **`示意性偏斜`**，且不计算概率加权 IRR）。
2. **必须运行 `scenario_irr.py` 计算 IRR 与风险收益比**（严禁心算）：
   ```bash
   python3 .agents/skills/dalton-call/scripts/scenario_irr.py \
     --current <当前股价> \
     --years <持有年限，如 2.0> \
     --prices <Bear价>/<Base价>/<Bull价> \
     --probs <Bear概率>/<Base概率>/<Bull概率> \
     --yield <年化股息+净回购收益率，默认 0> \
     --hurdle 0.15 \
     --json
   ```
   将脚本输出的各情景年化 IRR、概率加权 IRR、Bear 情景最大回撤（Max Drawdown）及相对门槛收益率（Hurdle Rate，默认 15%）超额值填入报告。

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
2. **监控表（6 列齐全）**：`触发事件｜监控指标｜量化阈值｜数据来源｜日期或窗口｜正反两边各怎么做（若达标如何加仓/持有 vs 若破线如何减仓/止损）`。
3. **“什么会让我们改变主意”**：用一段话清晰定义空头（或反方）获胜的标志。

### Step 9：撰写草稿、质检与发布审批
1. 按 [templates/call.md](templates/call.md) 写入 `drafts/<YYYY-MM-DD>-call-<TICKER>.md`。
2. 调用 `check_draft` 检查格式与禁用词。
3. 调用 `publish_report(draft_path="drafts/<YYYY-MM-DD>-call-<TICKER>.md", kind="conviction_call", slug="call-<TICKER>", updates_thesis=False)` 提交 PM 审批。
4. **PM 批准后**：调用 `knowledge_update` 更新 `thesis`、`drivers`、`agenda`，并调用 `knowledge_append_verdict`（`verdict="NEW_THESIS"` 或 `"THESIS_STRENGTHENED"`）。
