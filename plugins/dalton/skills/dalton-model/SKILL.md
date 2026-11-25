---
name: dalton-model
description: "建立或维护公司财务模型（Excel）：在用户模型上滚动更新或按基金格式新建，并对照一致预期（consensus）。用户要求建模型、更新模型、加季度、改预测、核对模型、对比一致预期时使用。"
---

# 公司财务建模（`dalton-model`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

每家公司的指标、正负号习惯、财年和业务结构不同：下面给出默认做法和示例，遇到不适用时按公司实际调整，并在工作簿最前面的 `README & Changelog` 工作表（下称「变更记录」）写明原因。与基金通用标准 `.agents/skills/dalton-conventions/references/model-standards.md` 的关系：维护原模型时，期间跨度、表头和最右备注列写法沿用原模型；新建模型时，表头和最右备注列按 `.agents/skills/dalton-model/references/format-spec.md`，历史覆盖跨度、空值留空和一手披露取数按 `model-standards.md`。

## 一、四条必须做到的要求

1. **预测期的金额由公式算出。** 收入、成本、费用、利润这些金额行，在预测期写公式，例如 `=GMV（商品交易总额）× take rate（货币化率，即平台收入占交易额比例）`、`=去年同期×(1+增速)`、`=收入×利润率`、`=各分部之和`。
   - 调整**预测数值**：改驱动假设（增速、take rate、利润率、费用率、税率），让公式自己算出新结果。
   - 调整**建模方法**（重新分部、补一条勾稽关系、换一个驱动）：可以改公式结构，在变更记录写明。
   - 管理层直接给了金额指引（例如明年 CapEx〔资本开支〕指引 100 亿）：在对应区块或表下方单设输入格填入该金额（套驱动假设格式，在最右备注列写来源），预测行用公式引用该输入格。
   - 原模型里是公式的预测行，更新后仍是公式。
2. **由季度汇总的年度列全部用公式算出，驱动假设只放在预测季度列和无季度拆分的远期年度列。** 该类年度列按指标性质计算：
   - 流量（收入、成本、利润、现金流）：四个季度相加；
   - 时点余额（现金、债务、期末股本）：取财年最后一个季度；
   - 比率（增速、利润率、take rate、占比）：用本年度列的分子和分母重新计算；
   - 年度 EPS（每股收益）：年度利润 ÷ 年度加权平均稀释股数（与公司披露口径一致）；
   - 用户数、活跃商家等 KPI（关键经营指标）：按公司披露口径计算（取第四季度期末数、四季度均值或披露的年度去重数）。
3. **与一致预期或卖方预测的重大差异要查明原因。** 我们的预测与一致预期（consensus）或主要卖方预测差距较大时，拆到分部、驱动假设、费用、利息、股本找出差异来源：有可核实的新信息支撑就保留判断并写明理由；发现自身驱动假设有遗漏或口径错误就修正假设。是否调整仅取决于是否有可核实的新信息或具体的假设问题。
4. **用户提供了原模型，就在原模型基础上维护。** 沿用原模型的结构、预测方法和格式；原模型文件保持只读，修改版另存为 `model/` 下的新版本文件。

## 二、操作步骤

**第 1 步　确定原模型和任务范围**
- 看用户这次给了什么文件、要求做什么（补实际值？加预测期？改驱动假设？从头建？）。
- 已有模型：优先用用户本次指定的文件；其次取 `data/models.json` 中 `current` 对应 `versions[]` 同 `id` 条目的 `path`，并结合工作区此前交付版本核对内容。多份文件无法判断哪份是最新原模型且影响交付时，询问用户。随后读取 `.agents/skills/dalton-model/references/rollforward.md`。
- 没有模型：读取 `.agents/skills/dalton-model/references/forecast-rules.md` 的「版面样板」，按基金默认格式新建。
- 单公司和行业工作区的模型均放在工作区 `model/` 目录：单公司命名为 `model/<TICKER>-model-v<NN>.xlsx`；行业整本模型保留多公司整本结构，沿用原文件名格式递增版本号或日期。
- `model/` 和 `models/` 各有一份时：SHA-256 哈希相同视为同一副本（以 `model/` 为准）；哈希不同时按文件内容和交付记录判断最新原模型，无法判断时询问用户。

**第 2 步　读材料**
- 公司最近的年报和季报（美股年报 10-K、季报 10-Q、外国发行人年报 20-F，或港股、A 股定期报告）：分部披露、KPI、管理层指引、股东回报计划。
- 主要卖方报告或模型：他们拆哪些驱动、市场在争论什么、一致预期大概在哪里。
- 维护原模型时：读懂原模型的工作表结构、哪些行是公式、用什么驱动预测、哪些地方相互勾稽、格式习惯。

**第 3 步　搭建或更新预测**（读取 `.agents/skills/dalton-model/references/forecast-rules.md`）
- 顺序：收入（重点）→ 成本和费用 → 营业利润 → 利息、税、少数股东至归母净利润 → 股本与 EPS → 现金流 → 资产负债表（现金、债务）→ 汇总表。
- 每新增一行先确定单元格类型（历史实际值、历史估算、驱动假设、直接输入的金额、同表公式、跨表引用公式、衍生比率）。

**第 4 步　写格式**（读取 `.agents/skills/dalton-model/references/format-spec.md`）
- 有原模型：运行 `python3 .agents/skills/dalton-model/scripts/model_style.py profile <原模型路径>` 提取格式画像，照原模型同一区块、同一用途单元格写格式。
- 新建模型：使用基金默认格式（可调用 `.agents/skills/dalton-model/scripts/model_style.py` 的 `apply_cell_style`）。

**第 5 步　重算并自查**
- 重算公式：运行 `python3 .agents/skills/xlsx/scripts/recalc.py <新版本模型路径>` 重算并回写缓存值（只读核查原模型时复制到临时文件重算）。
- 扫描核对：运行 `python3 .agents/skills/dalton-model/scripts/model_scan.py <新版本模型路径>`，逐条检查输出的预测期数字常数、可能的期间错配、外部链接与插件函数、`#REF!`（断裂引用错误）、循环引用及重算错误。属问题的修改模型并重新重算；属合理建模处理的在变更记录写明原因。
- 自己再核对几件事：年度 = 季度汇总是否成立；集团利润与分部之和能否对上（对不上的差额是什么）；现金和股本的滚存是否连贯；EPS 与净利润、股数是否一致。

**第 6 步　对照一致预期与卖方预测**（读取 `.agents/skills/dalton-model/references/sanity-check.md`）

**第 7 步　交付**
- 保存新版本至 `model/`（如 `model/PDD-model-v03.xlsx`；行业整本按原文件名递增版本或日期）。
- 登记新版本：先调用 `knowledge_read(doc="models")` 取 `latest_version_no` 作为 `base_version`（无记录时传 `null`），再调用 `knowledge_update(doc="models", ...)` 登记新版本（字段遵循 `dalton-knowledge` 的模型登记 schema：`versions[]` 追加条目，必填 `id`〔形如 `v03`〕、`path`〔形如 `model/PDD-model-v03.xlsx`〕、`created_at`、`reason`，可附 `recalculated` 与 `key_outputs`，并将 `current` 指向新版本 `id`）。`python3 .agents/skills/dalton-model/scripts/model_style.py lineage ...` 写出的 `model_lineage.json` 只是附加的普通记录（记录输入输出路径与哈希），不代替知识层登记。
- 更新变更记录（无 `README & Changelog` 工作表时新建并放在最前面）：写明版本号、日期、所基于的原模型文件、补充的实际值、新增期间、修改的关键驱动假设及理由、结构调整、与一致预期的主要差异及理由、扫描提示中保留不改的重要条目及原因。
- 回复用户时用几句话说清：关键预测（收入、利润、EPS）、和上一版相比变了什么、和一致预期差在哪里。

## 三、常见错误与正确做法

| 错误 | 看起来是什么样 | 正确做法 |
|---|---|---|
| 把公式改成了数字 | 原来 `=GMV×take rate` 的收入行，更新后变成一串固定数字 | 保留公式，只改 GMV 增速和 take rate 假设 |
| 由季度汇总的年度列留着假设 | 2027E 年度收入是四季之和，下面的 2027E 增速却是一个手填的 15% | 年度增速写成 `=2027E收入/2026E收入-1`，用衍生比率格式 |
| 套用了别的格式 | 表头深蓝 `1F4E79`、字号 10、浅灰底，和原模型不一样 | 照原模型或基金默认格式（Arial 8、亮蓝表头 `3366FF`、驱动假设格浅米黄 `FFFFC8`） |
| 偏离一致预期没检查 | 2028E 净利润明显高于一致预期，没有说明来源 | 拆差异来源，确认有依据再保留，并写理由 |
| 集团和分部脱节 | 集团营业利润单独预测，与分部营业利润之和对不上 | 集团营业利润 = 分部之和 + 总部及抵销（单列），或在对不上时写明差额 |
| 现金和股本不联动 | 净现金很多却没有利息收入增长；回购了但股数不变 | 利息随现金滚存；回购和股权激励改变股数（见 `.agents/skills/dalton-model/references/forecast-rules.md` 净现金公司示例） |
| 外部插件公式留在模型里 | Capital IQ、Bloomberg（第三方金融终端插件）公式在无插件环境下显示 `#NAME?`（函数名无法识别错误） | 在单独输入格填入带来源和日期的数值，原单元格用公式引用该输入格 |

## 参考文件

- `.agents/skills/dalton-model/references/forecast-rules.md`：版面样板、公式写法、各部分怎么预测、净现金公司示例、判断依据。
- `.agents/skills/dalton-model/references/format-spec.md`：基金格式手册（每类单元格怎么写，附样例与 `apply_cell_style` 角色说明）。
- `.agents/skills/dalton-model/references/rollforward.md`：在原模型上滚动维护的详细步骤。
- `.agents/skills/dalton-model/references/sanity-check.md`：一致预期和卖方预测的取数、对照和差异分析。
