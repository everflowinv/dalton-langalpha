# 完整覆盖 Excel 模型规格（`model-spec.md`）

本文件定义完整覆盖工作流（`full-coverage`）阶段 2 产出的 Excel 财务与估值模型（`model/<TICKER>-model-v<NN>.xlsx`）的起点判定（在原模型上滚动维护或按 15 页规格新建）、Dalton 专属增补工作表规格、提示性扫描与版本登记流程。通用建模要求、预测公式规则、格式手册、一致预期对照与版本登记统一以 `.agents/skills/dalton-model/SKILL.md` 为准，本文件只说明完整覆盖特有的工作表与交付约定。

> **建模前先读**：
> - **核心必读**：`.agents/skills/dalton-model/SKILL.md`（四条核心要求、操作步骤与版本登记），并按需读取其参考文件：
>   - `.agents/skills/dalton-model/references/rollforward.md`（在原模型上滚动维护、插列与部件核对）
>   - `.agents/skills/dalton-model/references/forecast-rules.md`（预测期跨度、版面样板、公式写法与各模块判断依据）
>   - `.agents/skills/dalton-model/references/format-spec.md`（基金模型格式手册与 `.agents/skills/dalton-model/scripts/model_style.py` 格式脚本）
>   - `.agents/skills/dalton-model/references/sanity-check.md`（一致预期与卖方预测取数、口径对齐与差异分析）
> - **新建估值页补充参考**：`.agents/skills/dcf-model/SKILL.md`（`WACC`〔加权平均资本成本〕、`DCF`〔自由现金流折现〕、终值与股权价值桥接参考）与 `.agents/skills/comps-analysis/SKILL.md`（可比公司估值表与统计分位参考）。

---

## 一、起点模式与版本保存

先按 `.agents/skills/dalton-model/SKILL.md` 第 1 步确认工作区起点（核对 `sources/` 下用户上传的模型、`model/` 下已有版本与 `data/models.json`）：

1. **工作区已有用户模型（或已有 `model/` 版本）**：
   - 原件只读：先将原模型复制到 `model/<TICKER>-model-v<NN>.xlsx`（尚无 `model/` 版本时存为 `v01`；已有版本时另存为下一版，如已有 `v01` 则另存为 `v02`），后续修改均在新版本上完成。
   - 沿用原模型的工作表结构、预测方法与格式（先运行 `python3 .agents/skills/dalton-model/scripts/model_style.py profile <原模型路径> --out research/<D>-fc/style_profile.json` 提取格式画像，按原表样式补充新增列与行），补齐预测期，并在保持原计算链完整的前提下增补或更新完整覆盖所需的四张专属工作表：**`Facts`**、**`Guidance vs Actual`**、**`Street vs House`**、**`Checks`**（原模型已有对应表或对照区时就地更新）。
2. **工作区尚无模型**：
   - 按下方第二节的 **15 页规格**从零新建 `model/<TICKER>-model-v01.xlsx`，字体、颜色、边框、数字格式与最右备注列遵循 `.agents/skills/dalton-model/references/format-spec.md`，调用 `.agents/skills/dalton-model/scripts/model_style.py` 的 `apply_cell_style` 写入基金默认格式。
3. **后续审稿修订与财报更新**：
   - 统一按 `.agents/skills/dalton-model/SKILL.md` 与 `.agents/skills/dalton-model/references/rollforward.md` 保持原件只读并另存为下一版（如 `v02`、`v03`），按 `.agents/skills/dalton-model/SKILL.md` 第 7 步在工作簿最前面的 `README & Changelog` 工作表（下称「变更记录」）写明全部改动。

---

## 二、新建模型的 15 页工作簿清单与 Dalton 专属页规格

> 说明：常规单公司建模一般不设情景分析表；仅在按下方 15 页规格新建完整覆盖模型时设 `Scenarios`（情景分析）工作表，供备忘录「财务预测与估值」节三情景赔率表取数。在原模型上滚动维护时不新建情景工作表，备忘录三情景从原模型估值区域或分析师假设取数；原模型未做情景测算的，在备忘录写明情景假设并说明模型未含该项计算。

从零新建时，工作簿包含 **15 张业务工作表（12 张核心工作表 + 3 张 Dalton 专属工作表；若有并购可比交易可按需增设 `Precedent Transactions`〔先例交易〕），另在工作簿最前面放 1 张 `README & Changelog`（变更记录）工作表**。15 张业务工作表按以下顺序排列：

1. **`Cover`（封面）**：公司名、代码、基准日、当前股价、市值、评级/倾向、基准/乐观/悲观（`Base` / `Bull` / `Bear`）目标价、模型版本与工作簿目录导航。
2. **`Facts`（Dalton 专属表 1：数字底表镜像）**：将 `research/<D>-fc/facts.md` 中 `FACTS-LEDGER`（机器可读数字账本）的全部条目（市值、股本、严格净现金与基础净现金双口径、隐含资产、股东回报三层次 `R1`/`R2`/`R3`〔见 `facts-ledger.md`〕、盈利质量、8 季损益、3 年 `FCF`〔自由现金流〕与 `SBC`〔股权激励费用〕等）结构化写入本表，附 `key（键名）| 指标 | 数值 | 单位 | 期间 | 一手来源与 accession（监管归档编号）`。**工作簿内其他工作表引用目标公司历史基准数据时，统一用跨表公式引用 `Facts` 表**。
3. **`Revenue Model`（收入拆分模型）**：分部与业务驱动树（Driver Tree）收入拆分表（历史年度 + 最近 8 个单季 + 未来预测期，期间跨度与「由季度汇总的年度列」规则遵循 `.agents/skills/dalton-model/references/forecast-rules.md`；按量 × 价、用户 × `ARPU`〔单用户平均收入〕或 `ARR`〔年度经常性收入〕× `NRR`〔净收入留存率〕等核心驱动写公式）。
4. **`Income Statement`（利润表）**：完整利润表（`GAAP`〔公认会计准则〕与 `Non-GAAP`〔非公认会计准则〕核心项、毛利率、研发/销售/管理费用率、`SBC` 单列、有效税率、加权平均稀释股数与 `EPS`〔每股收益〕）。
5. **`Cash Flow Statement`（现金流量表）**：现金流量表（`OCF`〔经营活动现金流〕、营运资本变动、`CapEx`〔资本开支〕、`FCF`、股份回购与现金分红流出）。
6. **`Balance Sheet`（资产负债表）**：资产负债表（现金及短期投资、应收账款、存货、`PP&E`〔物业、厂房及设备〕、商誉与无形资产、应付账款、递延收入/合同负债、有息债务、股东权益；资产总计用公式核对等于负债及股东权益总计）。
7. **`Scenarios`（情景分析，仅新建 15 页模型时包含）**：乐观 / 基准 / 悲观（`Bull` / `Base` / `Bear`）三情景开关与驱动参数表（设置三套独立的核心驱动参数——收入增速、毛利率、费用率、`CapEx` 强度、终值假设，汇总三情景下的收入、`EPS`、`FCF` 与每股估值）。
8. **`Guidance vs Actual`（Dalton 专属表 2：12 季指引兑现表）**：落地支线 C（`ec.md` 第 C4 节）整理的**过去 12 个季度管理层指引与实际结果逐季对比表**（列：季度｜指引指标｜指引区间下限｜指引区间上限｜指引中点｜实际结果｜超预期/低于预期金额与百分比｜结果判定｜管理层事后解释），底部用公式统计过去 12 季超预期次数、超预期率（%）、平均超预期幅度与指引风格结论（与 `.agents/skills/dalton-model/references/forecast-rules.md`「管理层指引」衔接）。
9. **`Street vs House`（Dalton 专属表 3：本方与市场预期逐驱动对比表）**：备忘录「财务预测与估值」节本方与市场预期对比表的唯一数字出处，口径对齐与差异分析遵循 `.agents/skills/dalton-model/references/sanity-check.md`。按未来两年（`0y`〔本财年〕、`+1y`〔下一财年〕）及中期（`+2y`〔后一财年〕）逐行列出核心业务分部收入、集团总收入、毛利率、各项经营费用/`CapEx`、营业利润（率）、有效税率、稀释股数、`GAAP`/`Non-GAAP EPS`、`FCF`，包含列：
   - `指标与期间 | 市场一致预期（Consensus）| 本方基准（House Base）| 本方乐观/悲观（House Bull / Bear）| 差异（% 或 bps〔基点〕）| 来源类型与日期 | 差异背后的核心分歧与证据依据（[引用N]）`
   - **公式引用分两种情况**：新建 15 页模型时，`House Base / Bull / Bear` 列用跨表公式引用 `Scenarios`、`Revenue Model` 与 `Income Statement` 对应单元格；在原模型上滚动维护时，`House Base` 列用跨表公式引用原模型主预测表或分部表对应单元格，乐观/悲观列引用原模型已有的估值区域，原模型未含对应计算时留空，并在差异说明列注明情景假设见备忘录正文。
10. **`DCF Inputs`（折现参数）**：`WACC` 测算（无风险利率、`ERP`〔股权风险溢价〕、`Beta`〔贝塔系数〕、税后债务成本、目标资本结构）与终值参数（永续增长率 $g$ 与退出倍数 `Exit Multiple`）。
11. **`DCF`（自由现金流折现）**：自由现金流折现模型与股权价值桥接（`EV`〔企业价值〕→ 股权价值 → 隐含每股价值）。
    - **净现金桥接口径**：净现金/（净债务）调整项**默认引用 `Facts` 表的「严格净现金（`net_cash_strict`）」**；旁边单列一行以「基础净现金（`net_cash_basic`）」计算的每股价值作敏感度对照；存在重大折后隐含资产（`hidden_asset_net`）时，写明是否计入股权价值桥接。
12. **`Sensitivity Analysis`（敏感度分析）**：二维敏感度矩阵（`WACC` × 永续增长率 $g$、`WACC` × 退出倍数、核心业务增速 × 稳态营业利润率对每股价值与隐含 `IRR`〔内部收益率〕的敏感度）。
13. **`Comparable Companies`（可比公司）**：同业可比估值表（至少 5–8 家可比公司，含市值、`EV`、未来两年收入与盈利增速、利润率、`P/E`〔市盈率〕、`EV/EBITDA`〔企业价值倍数，即企业价值与息税折旧摊销前利润之比〕、`EV/Sales`〔企业价值与收入之比〕、`FCF Yield`〔自由现金流收益率〕，底部用公式计算最大值、75 分位、中位数、均值、25 分位与最小值）。
14. **`Valuation Summary`（估值汇总）**：估值汇总与区间对照数据，承载估值三角（`DCF` 等内在价值〔严格净现金口径与基础净现金口径〕、可比公司倍数区间、历史估值区间至少三种互相验证；公司有可分拆业务、大额净现金或投资时加做 `SOTP`〔分部加总估值〕；汇总各方法结论的差异说明，以及三情景目标价与隐含上行/下行空间）。
15. **`Checks`（勾稽检查）**：模型勾稽与完整性检查表（资产负债表平衡检查 `Assets - Liabilities - Equity = 0`〔资产 − 负债 − 股东权益 = 0〕、现金流量表期末现金与资产负债表一致、分部收入加总等于总收入、由季度汇总的年度列等于四季之和、`Street vs House` 引用一致与公式错误核查）；个别检查项未显示 `OK` 时，结合公司实际核查原因并在变更记录写明说明后继续推进。

---

## 三、构建脚本归档、提示性扫描与版本登记

1. **保存可重跑构建/更新脚本**
   - 将生成或滚动更新工作簿的 Python 脚本保存至 `research/<D>-fc/build_model.py`（维护原模型时同步保存 `research/<D>-fc/style_profile.json`），便于修改假设后重跑复现。
2. **重算、部件对比与提示性扫描（`model_scan.py --recalc`）**
   - 构建或滚动保存完成后，依次运行公式重算与提示性扫描（在原模型上滚动维护时加跑 `parts-diff` 核对部件变动）：
     ```bash
     python3 .agents/skills/xlsx/scripts/recalc.py model/<TICKER>-model-v<NN>.xlsx
     python3 .agents/skills/dalton-model/scripts/model_scan.py --recalc model/<TICKER>-model-v<NN>.xlsx
     # 在原模型上滚动维护时，额外核对 OOXML（Office 文件的 XML 格式）部件变动：
     python3 .agents/skills/dalton-model/scripts/model_style.py parts-diff <原模型路径> model/<TICKER>-model-v<NN>.xlsx
     ```
   - `model_scan.py --recalc` 为只读提示性扫描工具，结果记入 `Checks` 表与变更记录：逐条查看输出的四类提示（预测期数字常数、可能的期间错配、外部链接与插件函数/`#REF!`〔断裂引用错误〕/循环引用、重算错误值），属问题的修改模型并重算；结合公司实际属合理建模处理、扫描超时或执行失败、以及 `Checks` 表个别条目未显示 `OK` 的情况，均在变更记录写明原因后继续推进。
3. **记录模型输入输出文件（`model_lineage.json`）**
   - 每次产出新版本后，运行以下命令将输入与输出文件路径及 `SHA-256` 哈希记入 `research/<D>-fc/model_lineage.json`（仅作文件与哈希记录留存，知识层仍须按下方第 4 条登记）：
     ```bash
     python3 .agents/skills/dalton-model/scripts/model_style.py lineage \
       --file research/<D>-fc/model_lineage.json \
       --output model/<TICKER>-model-v<NN>.xlsx \
       [--baseline <原模型路径>] [--profile research/<D>-fc/style_profile.json]
     ```
4. **关键输出单元格定位与知识层登记（供主智能体按 `dalton-model` 第 7 步执行）**
   - 在返回摘要中记录基准目标价（及核心收入、`EPS` 输出）所在的准确工作表名与单元格坐标（新建 15 页模型时一般位于 `Valuation Summary` 或 `Scenarios` 表；在原模型上滚动维护时填写原模型实际的估值工作表与单元格），供主智能体更新 `thesis` 的 `house_view.target` 时按实际工作表名填入 `model_ref: {"kind": "model_cell", "path": "model/<TICKER>-model-v<NN>.xlsx", "sheet": "<实际估值页名>", "cell": "<单元格坐标>"}`，并按 `.agents/skills/dalton-model/SKILL.md` 第 7 步调用 `knowledge_update(doc="models", ...)` 登记本次最终版本。
