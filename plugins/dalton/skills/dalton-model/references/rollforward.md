# 在原模型上滚动维护

起点原模型按 `.agents/skills/dalton-model/SKILL.md` 确定；原模型文件保持只读，所有修改统一经由 `model_edit.py` 在 `model/` 下的新版本路径完成（单公司如 `model/PDD-model-v03.xlsx`，多公司行业整本沿用原文件名递增版本或日期）。

## 步骤

**1. 提取基线指纹并读懂原模型**
- 运行 `python3 .agents/skills/dalton-model/scripts/model_fingerprint.py <原模型路径> --ticker <TICKER> --out <R>/model_dump/<TICKER>-fingerprint.json`，提取工作表顺序、期间轴、财年截止月、语义优先的行项类型映射、冻结窗格、样式画像、既有外链与插件格。
- 检查起点健康度：确认原模型预测期保持公式链、冻结窗格正常；遇到原模型本身已被整本重写损坏的情况时，及时告警并回退至上一未损坏版本。
- 梳理目标公司工作表的驱动公式、跨表引用与三表勾稽关系；多公司行业整本中只改目标公司所属工作表与变更记录，其余公司工作表保持原状。

**2. 补实际值（新披露的季度）**
- 将新披露季度的列头去掉预测后缀（例如 `3Q26E` 改为 `3Q26`）；参照前几个历史季度直接填数的明细行（各收入分项、成本、费用、KPI〔关键经营指标〕），通过 `model_edit.py` 将该季原预测公式替换为公司披露的实际值（套历史实际值格式），编辑器自动登记至 `coord_map.json` 的 `rollover_actual_cells` 以在检查时自动放行。
- 该季原有的驱动假设行（浅米黄底的增速、take rate〔货币化率〕等）改为由当季实际金额反算的衍生比率公式（斜体、无底色、无边框）；需留存对比时，把原假设值记入最右备注列。
- **保留全部小计、合计、比率、跨表引用与勾稽公式**：收入合计保持 `=SUM(...)`，毛利率保持公式，跨表引用保持引用。
- 公司只披露累计数（半年、前三季、全年）时，单季流量按累计数差分计算（如 `Q4 = 全年 − 前三季`），在最右备注列注明「披露衍生值（如 `FY − 9M`）」及两期财报出处；单季 EPS（每股收益）、加权平均稀释股数、去重用户数按各自定义取得或重新计算。
- 未披露的细分项填入估算值（套历史估算格式，最右备注列写明来源），或留空并在最右备注列写明原因。
- 实际值与上一版预测差距大的重要项目，在变更记录（`README & Changelog` 或原表既有 `Change Log` 工作表）写一句原因。
- 指引记录：补上本次新披露的实际值与兑现情况，并把本季业绩新闻稿或业绩会给出的新指引各记一行。

**3. 扩展预测期与受控原地编辑**
- 期间跨度：季度预测逐季覆盖至下一完整财年 `Q4`（如当前 `FY26` 覆盖至 `4Q27E`），年度预测覆盖至下下财年（至少 3 个完整财年，如 `2026E–2028E`），单向向后扩展并完整保留存量历史列。
- 新增季度列按公式用途选参照列：同比驱动参照去年同季列，环比驱动参照上一季列；新增季度使某年成为季度汇总年度列时，该年金额改写为季度汇总公式、比率用本年度分子分母重算，驱动假设移到对应季度列。
- 无季度拆分的远期年度列及新增远期年度列，直接在年度列放驱动假设（套驱动假设格式）。
- 新增季度和已有季度之间隔着只有年度的年份时，先把中间年份按指标性质拆成季度（流量四季之和等于原年度、余额按季滚存对齐年末、日均量按天数加权、利润率与 take rate 由各季金额算出并保留季节性），再向后扩展，并在回复中说明。
- 顺延已有 CAGR（复合年增长率）列、估值页（Valuation、SOTP〔分部加总估值〕）的目标年份与预测列引用，以及末尾扩列后的横向 `SUM` 与条件格式区间。
- 全部扩列与改格操作写入编辑计划 JSON，运行 `python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline <原模型路径> --ticker <TICKER> --plan <edit_plan.json> --workspace-dir . --fingerprint <fingerprint.json> --coord-map-out <R>/model_dump/coord_map.json --mode update`：脚本先以保持 `<calcPr>` 模式调用 `insert.py` 扩行列并留存缓存母本，再由受控 `openpyxl` 只改目标格，候选落盘前先调用 `restore_donor_cache` 原子恢复非目标公司表、外链、插件、既有循环格与未改公式格的原始缓存，随后在临时副本用 LibreOffice 复算并仅合并非错误计算值。

**4. 调整预测与外部依赖处理**
- 发现原模型预测期金额格为常数时：能按拆解区恢复计算关系的改写为公式；属管理层直接金额指引的，单设输入格（套驱动假设格式、最右备注列写来源）并由预测行公式引用，在变更记录写明。
- 外部终端插件（Capital IQ `_xll.ciqfunctions...`、Bloomberg `BQL`/`BDH`）与既有外部工作簿链接：
  - 影响本轮核心预测结果的，在独立输入格填入带来源和日期的数值，原格用公式引用该输入格；
  - 其余不涉及本轮目标预测的外链与插件格，由 `model_edit.py` 与 `recalc.py` 的母本缓存保护机制原样保留原始缓存值，避免错误值覆盖。
- 同一工作簿内的跨表引用和命名区域照常保留。

**5. 对照一致预期与本轮范围检查**
- 先独立完成本方预测，再按 `.agents/skills/dalton-model/references/sanity-check.md` 运行 `model_consensus.py write-snapshot` 与 `model_consensus.py diff` 生成 `<R>/model_dump/consensus-diff.md/.json`；超阈值项拆源复核一次并补充理由。
- 运行 `python3 .agents/skills/dalton-model/scripts/model_check.py check --workspace . --ticker <TICKER> --research-dir <R> --candidate <新版本路径> --baseline <原模型路径> --fingerprint <fingerprint.json> --coord-map <R>/model_dump/coord_map.json --workflow company-model --fix-round 0`，检查仅针对本轮改动范围做判定：
  - 仅「本轮可修」条目交回修改，最多两轮，每轮修改后按最新哈希刷新 `consensus-diff.md/.json` 并复查；
  - 基线遗留、缺证据与不适用条目如实记入报告备查；
  - 扩展条件格式（`<extLst>` / `x14`）变动在检查报告末尾如实提示。

**6. 记录与交付**
- 在 `README & Changelog`（或原 `Change Log`）写明版本号、日期、基线文件、补充的实际值、新增期间、修改的驱动假设及理由、结构调整与一致预期差异理由，再按 `.agents/skills/dalton-model/SKILL.md` 完成 `knowledge_update(doc="models")` 登记与 `dalton_check_done` 核对。

## 注意

- 保持原模型全部工作表及相对顺序稳定；确需调整顺序时，先核查跨表区间公式影响并在变更记录写明。
- 任务范围内可确认的原模型计算错误（如合计漏行）在新版本修正并说明影响范围；任务范围外的工作表或原作者定义保持原状，记录发现并请用户确认。
