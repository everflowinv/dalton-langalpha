# 完整覆盖 Excel 模型规格（`model-spec.md`）

本文件定义 `full-coverage` workflow 阶段 2 产出的 Excel 财务与估值模型（`model/<TICKER>-model-v01.xlsx`）的工作簿页签清单（15 页）、Dalton 专属增补页规格、构建与公式纪律及审计门禁。

> **前置必读 Skill**：动手建模前，必须先读取并严格遵循以下四个基础建模 skill 的规范：
> - `.agents/skills/xlsx/SKILL.md`（工作簿排版、颜色编码、公式规范、`recalc.py` 与 `audit.py --strict` 质量契约）
> - `.agents/skills/3-statements/SKILL.md`（三表勾稽、历史期与预测期构建）
> - `.agents/skills/dcf-model/SKILL.md`（WACC、FCF 折现、终值与股权价值桥接）
> - `.agents/skills/comps-analysis/SKILL.md`（可比公司估值表与统计分位）

---

## 一、工作簿 15 个页签清单与顺序（Workbook Manifest）

工作簿保存路径固定为 `model/<TICKER>-model-v01.xlsx`（后续在审稿修订或财报更新中若需修改模型，必须按 `.agents/skills/model-update/SKILL.md` 另存为 `v02`、`v03`，并用 `knowledge_update(doc="models")` 登记，**严禁覆盖旧版本**）。

工作簿包含 **标准 `initiating-coverage` 的 12 个核心页签 + Dalton 增补的 3 个专属页签（共 15 页，若有并购可比交易可加选 `Precedent Transactions`）**，按以下顺序排列：

1. **`Cover`**：封面页（公司名、代码、基准日、当前股价、市值、评级/倾向、Base/Bull/Bear 目标价、模型版本、工作簿目录导航与颜色图例）。
2. **`Facts`（Dalton 增补页 1：数字底表镜像）**：将 `research/<D>-fc/facts.md` 中 `FACTS-LEDGER` 的全部条目（市值、股本、净现金双口径、隐含资产、股东回报 R1/R2/R3、盈利质量、8 季损益、3 年 FCF/SBC 等）结构化落入本页，附 `key | 指标 | 数值 | 单位 | 期间 | 一手来源与 accession`。**工作簿内其他页签引用目标公司历史基准锚点时，统一用跨表公式从 `Facts` 页引用**。
3. **`Revenue Model`**：分部与业务驱动树（Driver Tree）收入拆分表（历史年度 + 最近 8 个季度单季 + 未来 3–5 年预测，按量 × 价 / 用户 × ARPU / ARR × NRR 等核心驱动建动态公式）。
4. **`Income Statement`**：完整利润表（GAAP 与 Non-GAAP 核心项、毛利率、研发/销售/管理费用率、SBC 单独列示、有效税率、稀释股数与 EPS）。
5. **`Cash Flow Statement`**：现金流量表（经营活动现金流 OCF、营运资本变动明细、Capex、自由现金流 FCF、回购与分红现金支出）。
6. **`Balance Sheet`**：资产负债表（现金与短投、应收、存货、PP&E、商誉与无形资产、应付、递延收入/合同负债、有息债务、股东权益；资产总计必须动态等于负债及股东权益总计）。
7. **`Scenarios`**：多/中/空三情景开关与驱动参数页（至少包含 **Bear / Base / Bull** 三套独立的核心驱动参数——收入增速、毛利率、费用率、Capex 强度、终值假设，严禁只改标签不改底层驱动参数；汇总三情景下的收入、EPS、FCF 与每股估值）。
8. **`Guidance vs Actual`（Dalton 增补页 2：C4 12 季指引可信度表）**：完整落地 C 支线 C4 整理的**过去 12 个季度管理层指引 vs 实际结果逐季对比表**（列：季度｜指引指标｜指引区间下限｜指引区间上限｜指引中点｜实际结果｜Beat/Miss 金额与百分比｜结果判定｜管理层事后解释），并在底部用公式统计：过去 12 季 Beat 次数、Beat 率（%）、平均 Beat 幅度、指引风格结论。
9. **`Street vs House`（Dalton 增补页 3：我们与卖方逐驱动对比表）**：作为备忘录第八章 `8.3 我们与卖方逐驱动对比` 的唯一数字出处。按未来两年（`0y`、`+1y`）及中期（`+2y`）逐行列出：**核心业务分部收入、集团总收入、毛利率、各项经营费用/Capex、营业利润（率）、有效税率、稀释股数、GAAP/Non-GAAP EPS、FCF**，每行包含以下列：
   - `指标与期间 | 卖方一致预期（Street Consensus） | 我们的基准预测（House Base） | 我们的乐观/悲观预测（House Bull / Bear） | House Base vs Street 差异（% 或 bps） | 差异背后的核心 Debate 与证据依据（引用 [A/B/C/D-n]）`
   - 其中 `House Base / Bull / Bear` 列必须用公式直接引用 `Scenarios`、`Revenue Model` 与 `Income Statement` 页的对应单元格，严禁手填硬编码。
10. **`DCF Inputs`**：WACC 测算（无风险利率、ERP、Beta、税后债务成本、目标资本结构）与终值参数（永续增长率 $g$ 与退出倍数 Exit Multiple）。
11. **`DCF`**：自由现金流折现模型与股权价值桥接（Enterprise Value → Equity Value → Implied Share Price）。
    - **Dalton 净现金桥接硬规则**：DCF 的净现金/（净债务）调整项**默认引用 `Facts` 页的「严格净现金（`net_cash_strict`）」**；同时在旁边单列一行以「基础净现金（`net_cash_basic`）」计算的每股价值作为敏感度对照；若有重大折后隐含资产（`hidden_asset_net`），显式说明是否纳入股权桥。
12. **`Sensitivity Analysis`**：二维敏感度矩阵（WACC × 永续增长率 $g$、WACC × 退出倍数、核心业务增速 × 稳态营业利润率对每股价值与隐含 IRR 的敏感度）。
13. **`Comparable Companies`**：同业可比估值表（至少 5–8 家可比公司，含市值、EV、未来两年收入与盈利增速、利润率、P/E、EV/EBITDA、EV/Sales、FCF Yield，底部用公式计算 `Max / 75th Percentile / Median / Mean / 25th Percentile / Min`）。
14. **`Valuation Summary`**：估值汇总与足球场区间图数据（汇总 DCF 严格净现金口径、DCF 基础净现金口径、Comps 估值区间、历史估值倍数区间、三情景目标价与隐含上行/下行空间）。
15. **`Checks`**：模型勾稽与完整性检查页（资产负债表平衡检查 `Assets - Liabilities - Equity = 0`、现金流期末现金与资产负债表一致、分部收入加总等于总收入、`Street vs House` 引用一致性、无错误值，所有检查项状态必须为 `OK`）。

---

## 二、建模执行与质量门禁

1. **保存可重跑构建脚本**
   - 将生成工作簿的 Python 脚本保存为 `research/<D>-fc/build_model.py`，确保修改参数后可一键重新生成。
2. **输入溯源批注与颜色编码**
   - 遵循 `.agents/skills/xlsx/SKILL.md`：历史与外部硬编码输入用蓝色字体，且关键历史输入与一致预期单元格附带出处说明（如 `Source: 10-K FY2025, Item 8, p.64`）；同页公式用黑色字体；跨页引用用绿色字体。
   - 派生单季数据（如 `Q4 = FY - 9M`）必须用公式或明确标注 `Derived filing actual`。
   - 假设为空时留空，绝不填 `0` 冒充实际数。
3. **重算与严格审计（必过门禁）**
   - 构建完成后，必须依次运行 `.agents/skills/xlsx/SKILL.md` 规定的公式重算与严格审计脚本：
     ```bash
     python .agents/skills/xlsx/scripts/recalc.py model/<TICKER>-model-v01.xlsx
     python .agents/skills/xlsx/scripts/audit.py --strict model/<TICKER>-model-v01.xlsx
     ```
   - 确保 **0 公式错误（无 `#REF!`、`#DIV/0!`、`#VALUE!`、`#NAME?`）**，且 `Checks` 页全部行显示 `OK`。
4. **Target Price 单元格定位（供 `knowledge_update` 使用）**
   - 在返回摘要中明确记录 `Valuation Summary` 或 `Scenarios` 页中 Base Case 目标价所在的精确页签名与单元格坐标（如 `Valuation Summary!E14`），以便主智能体在更新 `thesis` 的 `house_view.target` 时填入 `model_ref: {"kind": "model_cell", "path": "model/<TICKER>-model-v01.xlsx", "sheet": "Valuation Summary", "cell": "E14"}`。
