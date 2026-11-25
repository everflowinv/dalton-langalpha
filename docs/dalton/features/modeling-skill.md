# 公司财务建模技能设计方案（`dalton-model`）

本文定义 Dalton 公司与行业财务建模技能（`skill`，供智能体按需加载的执行规程与配套脚本）的职责边界、目录结构、建模与判断规则、单元格样式规范、工作簿安全写入方式、检查脚本、一致预期对账流程、版本管理、注册门控及分批落地方案。方案同时兼容 **Everflow 生产形态**（`DALTON_PROFILE=everflow`）与 **独立核心安装形态**（`DALTON_PROFILE=core`，架构规范见 `docs/dalton/features/standalone-core.md`）。

---

## 1. 目标、技能边界与协作分工

### 1.1 技能名称与定位

- **技能目录与注册标识**：`plugins/dalton/skills/dalton-model/`（注册名 `dalton-model`，斜杠命令 `/dalton-model`）。
- **核心定位**：Dalton 建立与维护 Excel（电子表格工作簿，扩展名 `.xlsx`）财务预测模型的统一入口。当用户提供自有模型或工作区已有模型时，直接在既有模型基础上滚动维护，沿用原模型的版面、样式与公式链；当工作区尚无模型时，按本技能默认格式与建模指引从零搭建模型。
- **设计原则（指引优先，避免僵化代码裁决）**：不同公司的经营指标、报表正负号习惯、会计口径与财务年度结账日各不相同。建模技能的定位是**实践指引与辅助工具**，应给智能体留出结合公司实际自行判断与调节的空间，不尝试用硬编码脚本穷举所有边缘情况或阻断交付。技能仅保留四条核心硬性要求（预测期金额由驱动假设通过公式计算、季度加总年度列不放假设且比率由年度金额反算、与一致预期或卖方有重大差异时查明原因并写明理由、有用户模型时在其基础上滚动维护并保留原件），其余内容均以指引和判断依据呈现；配套脚本仅提供机械式样式辅助（`model_style.py`）与提示性只读扫描（`model_scan.py`）。

### 1.2 四类适用任务

1. **存量模型滚动维护**：工作区 `model/` 或用户上传目录 `sources/` 已有单公司模型或行业多公司模型，需要向后扩展预测期间（如新增下一财年四个季度及更远年度列）、增补新覆盖公司模块、随公司重述调整分部结构或更新核心驱动假设，输出新版本文件至 `model/`。
2. **财报披露后更新实际值**：公司发布最新季度或年度法定财报后，读取一手公告将原模型中刚披露期间的预测公式切换为实际披露值，重算现金、有息债务、利息收支与股本滚存，校准远期驱动假设并输出新旧预测变动归因。
3. **从零新建财务模型**：工作区尚无模型文件，读取工作区知识层与法定财报后，按默认样式与全公式链规范新建涵盖关键经营指标、业务分部拆解、三张财务报表、估值与汇总表的工作簿，保存至 `model/`。
4. **只读合理性与勾稽核查**：在保持目标模型字节级只读的前提下，先对原件运行重算前静态检查，报告外部链接、插件函数、断裂引用与循环引用；对含外部链接的原件直接采用静态检查结果，对不含外部链接的原件复制临时副本运行重算并检查副本缓存值，核查预测值相对市场一致预期（`consensus`，多位卖方分析师预测的统计均值）或卖方报告的差异来源，排查预测金额常数、季度加总年残留假设、分部与集团脱节或格式偏差，输出问题清单。

### 1.3 与现有技能及组件的分工关系

| 相关技能 | 所在路径 | 职责分工与调用方式 |
|---|---|---|
| `dalton-conventions` | `plugins/dalton/skills/dalton-conventions/` | 投研方法论总纲（含 `references/model-standards.md` 的期间跨度下限、一手财报来源台账、五层数据分类、未预测项留空规则及 `scripts/xlsx_repair.py` 损坏命名区域修复脚本）。`dalton-model` 开头先读该技能并继承其数据与空值标准，在此基础上落地具体的模型公式规则、样式画像与检查脚本。 |
| `dalton-financial-lenses` | `plugins/dalton/skills/dalton-financial-lenses/` | 提供狭义与广义净现金、隐蔽资产、股东回报三层次指标（现金分红率 R1、总现金回报率 R2、扣除股份支付稀释后的净回报率 R3）及盈利质量计算脚本。`dalton-model` 在构建净现金计息、股份回购与股本桥接、估值净现金调整时直接采用同一口径。 |
| `dalton-knowledge` | `plugins/dalton/skills/dalton-knowledge/` | 管理工作区结构化知识文档。`dalton-model` 执行前读取 `thesis.md`（核心论点）、`debate.md`（多空分歧）、`drivers.md`（业务驱动）与 `data/models.json`（模型版本台账），交付新版本模型后调用 `knowledge_update(doc="models", ...)` 登记模型路径与核心输出单元格。 |
| `dalton-earnings-deep` | `plugins/dalton/skills/dalton-earnings-deep/`（及 `workflows/earnings-deep-update/`） | 财报后八节深度点评与模型更新工作流。第 2 批接入后，其模型更新支线与审稿后模型修订统一调用 `dalton-model` 执行滚动更新与 `model_scan.py --recalc` 提示性扫描，继续产出原有的映射表 `mapping.md`、`Change Log` 与盈利变动拆解 `model-delta.md`。 |
| `dalton-full-coverage` | `plugins/dalton/skills/dalton-full-coverage/`（及 `workflows/full-coverage/`） | 买方完整覆盖工作流。第 2 批接入后，工作区已有用户模型时由 `dalton-model` 在原模型基础上滚动补齐期间及 `Facts`、`Guidance vs Actual`、`Street vs House`、`Checks` 表；尚无模型时按 15 页规格新建并统一采用 `dalton-model` 的样式库与 `model_scan.py --recalc` 提示性扫描。 |
| 上游 `xlsx` | `plugins/langalpha_deliverables/skills/xlsx/` | 底层电子表格操作组件。`dalton-model` 复用其 `scripts/recalc.py`（公式重算与缓存值回写）、`scripts/insert.py`（在输出副本上插入或删除行列并重指公式、命名区域与图表引用）、`scripts/render.py`（导出图片核对版面）和 `scripts/profile.py`（原始数据表探查）；样式辅助与提示性扫描使用 `dalton-model` 自带的 `model_style.py` 与 `model_scan.py`。 |
| 上游 `model-update` / `3-statements` / `dcf-model` / `check-model` | `plugins/langalpha_research/skills/` | 注册在上游 `src/ptc_agent/agent/middleware/skills/registry.py`，不在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 的 `SUPERSEDED_UPSTREAM_SKILLS` 集合中（保留在上游注册表且不加入 `SUPERSEDED_UPSTREAM_SKILLS`，供三表勾稽、自由现金流折现估值 `DCF`〔Discounted Cash Flow〕及只读核查的来源台账与报告格式按路径读取）；Dalton 工作区内的公司与行业建模、滚动更新和修订统一路由至 `dalton-model`。 |

---

## 2. 目录结构与各文件职责

技能目录位于 `plugins/dalton/skills/dalton-model/`，包含 1 份主入口文件、4 份按需加载的细则参考文档（`references/`）与 2 个确定性 Python 脚本（`scripts/`）：

```text
plugins/dalton/skills/dalton-model/
├── SKILL.md
├── references/
│   ├── format-spec.md
│   ├── forecast-rules.md
│   ├── rollforward.md
│   └── sanity-check.md
└── scripts/
    ├── model_style.py
    └── model_scan.py
```

### 2.1 `SKILL.md`（入口指引、四条硬性要求与常见步骤）

- **YAML（配置文件标记格式）头部元数据**：`name: dalton-model`；中文 `description` 写明支持在用户现有 Excel 模型上滚动维护、财报披露后更新实际值、新建单公司或行业财务预测模型及只读核查，提供建模指引、样式读取辅助与提示性扫描工具。
- **前置读取顺序**：首段固定写明 `Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.`（一致预期取数工具统一按 `references/sanity-check.md` 执行）；公司工作区依次读取 `agent.md` → `thesis.md` → `debate.md` → `drivers.md` → `data/models.json`，行业工作区先读 `sector.md` 与 `data/models.json`。
- **四条硬性要求与指引定位**：显式列出四条硬性要求及其原因（预测金额由驱动假设通过公式计算；季度加总年不放假设、比率由年度金额反算；与一致预期或卖方重大差异查明原因并写明理由；有用户模型时在其基础上滚动维护、沿用格式并保留原件），说明其余建模与格式内容作为指引由智能体结合公司实际灵活判断。
- **常见执行步骤**：确定起点与副本路径（损坏命名区域先用 `xlsx_repair.py` 修复副本）→ 运行 `model_style.py profile` 提取原表样式画像（失败时退回默认）并按需用 `model_scan.py` 预扫原件 → 在输出副本上使用上游 `xlsx/scripts/insert.py` 扩列、替换外链与插件公式 → 运行 `xlsx/scripts/recalc.py` 重算并运行 `model_scan.py --recalc` 获取核对提示，由智能体逐条判断（确认是问题就改，认为不是问题的在变更记录里写一句原因）→ 用 `model_style.py lineage` 留存普通的 `model_lineage.json` 记录并调用 `knowledge_update(doc="models", ...)` 交付。

### 2.2 `references/` 四份细则指引文档职责

| 文件路径 | 按需读取时机 | 承载内容 |
|---|---|---|
| `references/format-spec.md` | 新建模型或读取存量模型样式时 | 新建模型的常用默认样式常量、存量模型样式画像提取与沿用方式（失败退回默认）、常见版面与数字格式习惯。 |
| `references/forecast-rules.md` | 设计预测逻辑、分部拆解与报表勾稽时 | 好模型的特征与常见例子、期间与非自然年财季处理、加总年常见写法、驱动假设示例、分部/利息/股本/三表/现金流/汇总表的判断依据。 |
| `references/rollforward.md` | 在存量模型上扩列、填季报或处理外链时 | 原件只读与副本操作、双目录选择、上游 `insert.py` 与 `recalc.py` 使用注意点、外链与插件函数替换、普通 `model_lineage.json` 记录。 |
| `references/sanity-check.md` | 执行一致预期对账、内部勾稽与扫描复核时 | 双形态能力门控下的取数指引、重大差异归因与理由写入位置、常见内部勾稽核对点及 `model_scan.py` 提示项的处理方式。 |

### 2.3 `scripts/` 两个确定性脚本职责

- **`scripts/model_style.py`**：纯机械辅助工具，提供默认样式常量、主题色加色调还原器、存量模型按表与区块样式画像提取（失败退回默认）、单元格默认样式写入 `apply_cell_style`、相邻期间列样式克隆 `clone_period_column_style`、知识层期间归一化 `normalize_period_for_knowledge` 及普通的 `model_lineage.json` 台账记录器（不做强制校验）。结构扩列与公式重算直接使用上游 `xlsx` 技能的 `insert.py` 与 `recalc.py`。
- **`scripts/model_scan.py`**：只读提示性扫描器，列出四类值得核对的单元格及原因，输出 JSON 与简短摘要，退出码仅区分扫描成功（`0`）与扫描本身出错（`1`），不做对错裁决也不阻断交付（详见第 5 章）。

---

## 3. 财务建模规则与分析师判断依据

本章将建模指南草案第 2–9 节转化为面向智能体的具体执行规则，并明确分析师判断事项及参考依据。

### 3.1 期间跨度、共用期间映射与季度加总年规则

- **期间跨度标准**：
  1. **逐季与年度跨度**：本财年尚未披露的剩余季度与下一完整财年的四个季度全部做逐季预测（例如最新已披露季为 `3Q26` 时，逐季预测覆盖 `4Q26E` 及 `1Q27E`–`4Q27E`）；年度预测至少覆盖本财年与下两个完整财年（如 `2026E`、`2027E`、`2028E`）。历史年度（自 `FY2018` 或招股书最早完整财年起）与历史单季（至少自 `2024Q1` 起）完整保留，只向后新增期间列。
  2. **工作表列头与知识层期间转换**：存量模型滚动维护时，**列头直接沿用原模型的期间写法**（如历史写 `2025`、`3Q26`、`2026Q2`，预测写 `2026E`、`4Q26E`、`2026Q3E`），保持全书一致，无需改成带 `FY` 或 `A` 的格式；非 12 月底结账的公司在表头旁或 `A2` 注明财年截止月（如 3 月底结账）。调用 `knowledge_update(doc="models", ...)` 登记 `key_outputs` 时，按 `src/dalton/knowledge/schemas/common.schema.json` 的 `period` 正则转换：将 `2026` / `2026E` / `2026F` 转为 `FY2026`，将 `4Q26E` / `2026Q4E` 转为 `4Q26`；对复合年增长率（`CAGR`〔Compound Annual Growth Rate〕）列等无法匹配该正则的列，省略非必填的 `period` 字段。
- **期间映射与非自然年财季指引**：
  阅读与维护工作表时，结合表头与子区块表头行（如主表第 1 行、子业务表第 60 行子表头）区分四类常见情形：
  1. **财季归属与财年绑定**：每个财年对应的四个财季由公司财年截止日、主副表头文本和业务期间归属确定。例如 3 月底结账公司在 `Model - BABA` 中，财年列 `2027E` 对应 `2Q26E`–`1Q27E` 四个财季列；跨自然年的财季求和（`2027E = SUM(2Q26E:1Q27E)`）属于标准同财年对应，不算错期。
  2. **重复期间标签消歧**：同一表头行出现重复标签时（如 `Subs - JD!R1/S1` 两个 `2026E`，分别承接不同口径或明细列），结合子标题、口径说明、所属业务区块或跨表引用关系确认各自口径。
  3. **实际列与预测列统一判定**：同时识别不带后缀的历史列（`2025`、`3Q26`、`2026Q2`）、带 `A` 后缀的历史列（`2025A`、`FY25A`、`3Q26A`），以及带 `E`、`F`、`P` 后缀的预测列（`2026E`、`2026F`、`2026P`、`4Q26E`、`2026Q4F`）。
  4. **`CAGR` 列与差值列单独归类**：表头含 `CAGR`、`YoY`（Year-over-Year，同比增速）、`QoQ`（Quarter-over-Quarter，环比增速）、`vs`、`Chg`、`Δ` 的列（如 `CAGR 25-28E`、`CAGR 19-24`），即使文本末尾带 `E`，也与预测期间列分开处理。
- **季度加总年逐行计算指引**：
  当某预测财年列在同表已有对应的四个财季列时，该年度列全部由季度公式算出，驱动假设只放在预测季度列和无季度拆分的远期年度列；按五类行分别采用以下写法：
  1. **流量金额行**（营业收入、营业成本、各项期间费用、分部与集团营业利润、利息收支、所得税、净利润、经营现金流、资本开支、自由现金流、分红与回购支出）：写对应四个财季的求和公式 `=SUM(Q1:Q4)` 或四格相加公式（四格相加与 `SUM` 等价）；
  2. **时点余额行**（期末现金及短期投资、期末有息债务、资产负债表各时点科目、期末客户资产规模 `AUM`〔Assets Under Management〕、期末总股数）：写引用该财年第四季度的公式 `=Q4`；
  3. **加权股本行**：加权平均稀释股本按公司财报披露口径写四季度均值 `=AVERAGE(Q1:Q4)` 或第四季度 `=Q4`；
  4. **年度每股收益行（`EPS`〔Earnings Per Share〕/ `EPADS`，对应 `ADS`〔American Depositary Share，美国存托股份〕）**：「年度净利润（或归母净利润）÷ 年度加权股数」和「四个财季 `EPS`/`EPADS` 之和（`SUM` 或四格相加）」两种写法都合法（如 `Model - BABA!S202`），同表保持一致；
  5. **衍生比率行**（`YoY` 增速、毛利率、各项费用率、营业利润率 `OPM`〔Operating Profit Margin〕、净利率、货币化率 `Take rate`〔平台收入占交易额比例〕、分部收入占比）：统一用**该年度列自身的金额单元格反算**（如 `=S9/R9-1`、`=S19/S9`），设为斜体且无假设底色。
- **需要分析师判断的事项**：是否将年度预测延伸至下三个财年及更远期（如 5–10 年显式预测）。
- **判断参考依据**（出现以下情形时可向远期延伸至增速与利润率进入稳态的财年）：公司处于亏损或新业务减亏爬坡期，近两年盈利尚未体现稳态水平；市场主流估值采用 3 年后远期市盈率（`P/E`〔Price-to-Earnings Ratio〕）或多年期 `DCF`；公司处于跨度超过 3 年的大型资本开支周期，折旧高峰与产能释放落在第 3 年之后。

### 3.2 存量结构继承与分部改口径对账

- **执行规则**：
  1. **沿用存量模型结构**：工作区已有模型或用户上传了历史模型时，先梳理工作簿的页签计算链路（如同行数据页 `Peers` → 关键经营指标页 `Key Metrics` → 子业务页 `Subs` → 主财务表 `Model` → 估值页 `Valuation` / 分部加总估值页 `SOTP`〔Sum-of-the-Parts〕）及工作簿命名区域（如汇率 `hkd`、`brl`、`ex`），在原预测结构上扩展期间、修补断链和更新分部，保留原工作簿内的全部历史专题对比页。
  2. **分部改口径保留与可比期间对账**：公司中途调整分部披露口径（重组业务线、合并分部或变更分部利润定义）时，在同一分部区域同时保留「旧披露口径区块」（填至最后一次披露该口径的期间，后续期间留空）与「新披露口径区块」（自公司提供重述数据的最早期间起填入并向后预测）。在下方建立「分部口径对账表（`Reconciliation`）」：**仅在公司同时披露可比数据或可对齐集团总数的期间写出差额检验公式**（`=新口径分部合计 + 未分配总部项 + 分部间抵销 - 集团合计`）；因公司未重述早期季度而缺少数据的不可比期间，对账格留空（避免把空白当作零算出失真差额），并在最右侧来源备注列用红字写明缺失的数据项、不可比原因及财报附注出处。
- **需要分析师判断的事项**：新增业务或口径调整时，在 `Subs` 页或主表下方增补子业务块，还是向用户提议调整主表行结构。
- **判断参考依据**：集团合并利润表主结构未变时，统一在 `Subs` 页或主表下方增设业务子模型块并将汇总行链回主表；当公司发生重大并表剥离、更换记账本位币或变更收入确认准则导致主表核心行整体变化时，先列出新旧行映射清单交用户确认后再调整主表。

### 3.3 收入表与成本费用建模规则

- **执行规则**：
  1. **关键经营指标（`KPI`〔Key Performance Indicator〕）与收入成本挂钩**：将公司按披露口径给出的用户数、每用户平均收入（`ARPU`〔Average Revenue Per User〕）、商品交易总额（`GMV`〔Gross Merchandise Volume〕）、货币化率（`Take rate`）、订单量、平均客单价（`ASP`〔Average Selling Price〕）、客户资产规模（`AUM`）等置于 `Key Metrics` 页或主表上方经营区，收入与直接变动成本行用公式关联上述指标（如 `佣金及广告收入 = GMV × Take rate`、`履约成本 = 订单量 × 单均履约成本`），可参考卖方通行拆法补建中间桥接行。
  2. **关键驱动独立拆分预测**：对股价和盈利弹性影响显著的核心驱动单独拆行或单建业务子表预测。历史财报披露不足时，参考公司电话会定量线索、卖方报告拆算方式或第三方高频数据填入历史估算值，套用历史估算样式（见第 4.1 节），并在最右侧备注列用红字注明估算方法与来源（如 `<Yipit`、`<<GS 1117`〔高盛 11 月 17 日报告〕）。
  3. **分部（`Segment`）优先预测**：公司披露分部收入与分部营业利润（或经调整息税及摊销前利润 `EBITA`〔Earnings Before Interest, Taxes, and Amortization〕）时，优先在分部区块逐个分部预测收入、利润率与营业利润。当集团营业利润等于各分部营业利润之和（加未分配总部项与分部间抵销）时，集团主表营业利润直接写汇总公式引用分部区块；集团层面的细分期间费用行只回填历史披露数，预测期若无集团级独立驱动则留空（或由分部费用汇总得出），保持单一利润计算源。
  4. **成本与费用按披露依据拆分**：存量模型已有拆分维度（如将股份支付费用 `SBC`〔Share-Based Compensation〕、折旧与摊销 `D&A`〔Depreciation and Amortization〕、人工、履约物流与其他经营费用分开）的直接沿用；新建模型且法定定期报告（美股 `10-K`/`10-Q`/`20-F`〔美国证券交易委员会年度、季度与外国私人发行人年报〕或港股、A 股定期报告）持续披露明细的，拆出独立行预测以衔接公认会计准则（`GAAP`〔Generally Accepted Accounting Principles〕）与非公认会计准则（`Non-GAAP`）调节项；缺乏持续披露依据的科目保持合并行按占收入比例预测。
- **需要分析师判断的事项**：哪些业务或变量单列为关键驱动；哪些成本费用具备拆分依据。
- **判断参考依据**：
  - **关键驱动识别**：优先依据工作区知识层 `thesis.md` 的核心支柱、`debate.md` 的多空分歧变量，以及卖方报告聚焦的估值与盈利主线；定量筛查可参考「预计贡献未来两年集团增量收入或增量营业利润绝对值约 **20%** 以上，或稳态营业利润率与现有主业相差约 **10 个百分点**以上」（该比例为**建议参考值**，可按公司业务结构与估值敏感度调整）。
  - **成本费用拆分**：法定财报附注或现金流量表连续披露历史金额，且变化节奏与收入不同步（如重资本开支期折旧跟随资产投产、期权费用跟随授予节奏）时拆出独立行。

### 3.4 无循环利息滚存、期末股数滚存与加权稀释股数

- **执行规则**：
  1. **无循环引用的利息收支计算**：
     - 只要公司的净现金储备或有息债务规模对当期或远期利润、每股收益有可见影响，即将利息收入、利息支出与资产负债表（或现金滚存表）的现金及短期投资（含定期存款与短期理财）、有息债务余额建立公式关联；现金与债务规模很小、利息对利润影响微乎其微时，可按近期历史水平或占收入小比例设定简单假设。
     - **统一采用无循环的期初余额计息方式**，保持工作簿迭代计算关闭（`wb.calculation.iterate = False`）：预测期单季列写 `=期初生息现金（或期初计息债务）× 假设年化利率 × 0.25`，纯年度列写 `=期初余额 × 假设年化利率`（存量模型若原本使用期初期末均值并开启了迭代计算，滚动维护时统一改为期初余额无循环公式并关闭迭代开关）。
     - 建表时在历史期间用同口径公式反算公司各期历史隐含年化存款收益率与借款利率，以此检验预测期利率假设处于该公司历史合理带并反映当前市场利率环境；债务笔数多、期限与票息差异大且影响显著的公司，可单建逐笔债务明细表（本金、票息、到期日、转股条款）汇总利息支出。
  2. **期末股数滚存与加权平均稀释股数分层建模**：
     - **股数滚存恒等式使用期末总股数（`Ending Shares`）**：预测期每期按 `期末股数 = 期初股数 + 本期股权激励归属/行权增发股数 − 本期回购注销股数` 动态滚存。其中股权激励增发参考历史自然稀释率或按 `=预计 SBC 费用 ÷ 假设授予/归属股价 × 归属系数` 估算；`本期回购注销股数 = 本期股份回购现金支出（折算为股票交易币种）÷ 假设回购均价`，且**回购现金支出用公式直接关联现金流量表的股份回购流出项**，使现金减少（降低下期期初生息现金）与股数缩减同步联动。
     - **每股收益分母使用加权平均稀释股数（`Weighted Average Diluted Shares`）**：与公司法定财报计算 `EPS` / `EPADS` 的口径严格对齐，预测期用 `=AVERAGE(期初股数, 期末股数) + 稀释性期权与限制性股票单位（RSU〔Restricted Stock Units〕）及可转债的增量稀释股数`（或沿用原模型与财报一致的稀释衔接公式）；发行美国存托凭证（`ADR`〔American Depositary Receipt〕）的公司用显式换算比绑定普通股与 `ADS` 数量。
- **需要分析师判断的事项**：利息与债务是否需要建立滚存或逐笔债务表；未来各期股份回购规模的设定。
- **判断参考依据**：
  - **利息与债务重要性参考值**（**建议参考值**，可按公司利润基数与估值敏感度调整）：利息收入、利息支出或净利息占税前利润（`EBT`〔Earnings Before Taxes〕）或 `Non-GAAP` 净利润绝对值约 **5%** 以上，或狭义净现金 / 总有息债务占总市值约 **10%** 以上时建立余额联动（中概互联网等大额净现金公司即使利润率高导致利息占比低于 5%，只要净现金变动对利润有可见贡献即挂钩现金滚存）；总有息债务与息税折旧摊销前利润（`EBITDA`〔Earnings Before Interest, Taxes, Depreciation and Amortization〕）之比约 **2.0 倍**以上或近两年集中到期债务占比较高时，可拆建逐笔债务明细表。
  - **股份回购取值依据**：综合董事会剩余回购授权额度、管理层公开股东回报计划、近 4–8 季实际回购节奏，以及账面存量非受限净现金与预计自由现金流（`FCF`〔Free Cash Flow〕）设定。**拥有充裕存量净现金的公司动用账面净现金实施超过当期自由现金流的回购属于正常合理的资本配置方式**，在现金滚存中如实扣减并核对期末非受限现金余额充足即可。

### 3.5 资产负债表建模规则

- **执行规则**：
  1. **常规非金融公司**：至少通过现金流量表动态滚存推出**期末现金及短期投资余额**；有息债务影响明显时同步滚存**短期与长期有息债务余额**。存量用户模型若仅维护现金与债务核心滚存行，沿用该精简结构；新建完整三表模型或原模型已含完整资产负债表时，完整滚存营运资本、固定资产净值与股东权益，使每期 `总资产 = 总负债 + 股东权益`。
  2. **资产负债表驱动型公司**：银行、券商、保险、消费金融/信贷科技、资产管理、房地产投资信托（`REITs`〔Real Estate Investment Trusts〕）及重资产租赁公司，完整预测资产负债表各生息与计息科目，并依据 `10-Q`/`10-K`/`20-F` 或定期报告附注，将客户备付金、融出资金、贷款余额、投资资产、计息负债与利润表的利息收入、经纪与资管收入、利息支出、信用减值损失逐项用公式挂钩。
- **需要分析师判断的事项**：目标公司采用精简现金债务滚存还是完整资产负债表驱动建模。
- **判断参考依据**：净利息收入、投资收益、保费收入或基于表内资金规模的融资融券收入占营业总收入约 **20%** 以上（**建议参考值**），或业务规模直接受监管净资本与杠杆率约束时，采用完整资产负债表驱动建模。

### 3.6 现金流量表与未预测项留空规则

- **执行规则**：
  1. **覆盖范围**：所有模型至少预测经营活动现金流（`OCF`〔Operating Cash Flow〕）与自由现金流（`FCF = OCF − CapEx`，`CapEx` 为资本开支〔Capital Expenditures〕），并向下计入现金分红与股份回购现金流出以滚存期末现金。
  2. **营运资本变动（`ΔNWC`〔Change in Net Working Capital〕）**：复盘近 3–5 个财年与近 8 个季度的营运资本变动占收入或收入增量的历史规律。呈现稳定方向与周转特征时（如平台递延收入与应付商家款随交易扩张持续带来净流入，或自营零售应收/存货/应付周转天数 `DSO`/`DIO`/`DPO`〔Days Sales / Inventory / Payable Outstanding〕稳定），按占收入增量比例或周转天数建立预测公式（精简模型无需逐行反推资产负债表细项）；季度或年度波动剧烈、缺乏稳定趋势时**选择不单独预测，对应预测期单元格留空**，并在右侧备注列说明「历史营运资本波动大，预测期未单列预测」。
  3. **未预测项留空与显式零假设规则**（与 `plugins/dalton/skills/dalton-conventions/references/model-standards.md` 第 5 节完全一致）：
     - 凡属**未单独预测或不适用的科目**（包括波动大未预测的营运资本变动、预测期汇兑损益、公允价值变动损益、已停用旧分部行等），**预测期单元格一律保持空白（`Blank`）**，求和公式 `SUM` 自动忽略空白格；存量模型滚动维护时，若将原模型中此类科目的既有公式改为未预测留空，在 `mapping.md` 或 `README & Changelog` 登记工作表、行项、期间和原因后作为合法转换放行，未登记的空白仍算公式退化；
     - **仅当有明确一手证据表明该期实际发生额或预测值确实为零**（如公司公告某财年暂停派息、法定附注显示并购无形资产已摊销完毕、或有明确依据设定零增长/零变动假设）时，才在**套用假设样式的输入单元格**中填入 `0`（结果行通过公式引用该输入格），并在最右侧备注列写明证据来源。
  4. **资本开支、折旧摊销与股东回报**：每本模型均预测 `CapEx`（重资本业务按投向或指引拆分，轻资产业务按占收入比例预测）；`CapEx` 重要时将折旧与期初固定资产及新增 `CapEx` 建立公式关联，否则按占收入比例延续；并购无形资产摊销重要时按年报附注披露的未来 5 年预计摊销时间表放入外部输入格引用；分红与回购结合历史派息率与最新回报计划建立公式，重大回购同步勾稽至第 3.4 节期末股数滚存。
- **需要分析师判断的事项**：营运资本是否具备可预测规律；`CapEx`、并购摊销与回购是否需要细化拆解。
- **判断参考依据**（以下数值均为**建议参考值**，可结合公司历史与估值弹性调整）：近 3–5 年 `ΔNWC / 收入` 方向一致且波动标准差小于收入约 **3%** 时视为规律明显；`CapEx / 收入 ≥ 5%`、`CapEx / OCF ≥ 20%` 或处于扩产周期时拆细并与折旧联动；并购无形资产摊销占 `GAAP` 营业利润绝对值约 **5%** 以上时按附注时间表建模；年度回购占总市值约 **1%** 以上或占自由现金流约 **15%** 以上时必做股数联动。

### 3.7 单元格用途分类、合法假设公式与硬编码核对指引

工作簿内单元格按用途常见归入以下**四类**，由智能体结合公司与区块语境判断（仅预测期财务结果金额必须由驱动假设通过公式计算；`model_scan.py` 只列出预测期数字常数供人工判断，不做硬性裁决）：

| 单元格用途 | 涵盖范围（先按行项用途与期间确定） | 预测期写法与样式指引 |
|---|---|---|
| **1. 财务结果（`formula` / `financial_output`）** | 存在上层量价/分部拆解的收入成本行、三张财务报表主表金额行、小计与合计行、期末现金/债务/股数滚存行、`EPS`/`EPADS`、估值输出格、**由季度汇总的年度列全部行**，以及由金额反算的衍生比率行（`YoY`、利润率、占比、加总年 `Take rate`）。 | **预测期全部写动态公式**。同表金额公式用黑字正体，跨表引用公式用绿字（合计行加粗），衍生比率公式用斜体（跨表衍生比率可同时为绿字斜体，如 `Model - Tiger!G95`），无假设底色。 |
| **2. 驱动假设（`driver_assumption`）** | 非加总预测列（预测季度列与纯年度预测列）中的增速、`Take rate`、毛利率、费用率、有效税率、隐含利率、`CapEx` 占比、客单价等驱动参数行，以及管理层指引等直接输入金额；历史期缺失披露时按线索回填的为历史估算值（`historical_estimate`）。 | 填入**套用假设样式的数值常数**（含经证据确认并写明备注的 `0`），或写入下述**四类驱动假设公式（可引用可比的历史或同期反算指标）**。同一驱动行在历史期为反算公式、预测期为蓝字假设底色常数属于标准建模结构。 |
| **3. 外部静态参考输入（`external_input`）** | 对照表或参考区中的可核验静态值：市场一致预期（`Consensus` 列）、从外部插件抽离出的参考股价/汇率/同行数据输入格。 | 填入静态数值（蓝字无底色输入样式；直接作为预测驱动参数或管理层指引金额的改用 `driver_assumption` 套浅米黄底色与边框），在最右备注列注明一手出处与截至日期；主表财务结果行通过公式引用该输入格。 |
| **4. 标题与留空（标题 / 备注 / 未预测项）** | 报表与业务分节标题行、子区块期间表头（如 `Subs - JD!M60` 浅蓝底年份子表头）、空白分隔行、最右侧红字来源备注列、旧分部停用后的空白格及未单独预测科目的空白格。 | 保持标题文本、备注文本或留空。 |

- **四类常见驱动假设（及历史估算）公式形态**：
  按行项用途与非加总期间列（或历史估算位置）确定为驱动假设或历史估算，套用假设样式（默认蓝字 `0000FF` + `FFFFC8` 底色 + 四边 `hair` 细虚线框；历史估算亦可用浅灰 `EDEDED` 底色，维护原模型时以原表样式为准）。以下四类写法均属正常驱动假设公式：
  1. **单期基准格加减常数增量**：如历史估算 `Model - JD!Q272` 的 `=P272+0.05%`、预测期费用率变动 `=R138+0.3%`；
  2. **单期基准格乘以常数因子或 `(1 ± 常数增量)`**：如单价或费率按固定幅度调整 `=P40*(1+2%)`、`=P40*1.02`；
  3. **直接引用可比历史期间指标（含历史反算比率）或同列子项**：如跨年同季沿用历史反算比率 `Model - JD!BI157` 的 `=BE157`、历史估算引用 `Model - PDD!N16` 的 `=N69`；
  4. **引用同期可比指标（含同期反算比率或同期驱动格）并做固定加减差**：如在线券商模型 `Model - Futu!P35` 的 `=P9-8%`（其中 `P9 =P8/P6` 为同期金额相除得出的反算比率）。
  > **边界区分**：两个金额单元格相除再减 `1`（如 `=S9/R9-1`）或相减除以基期（`=(S9-R9)/R9`）属于衍生比率计算公式，在由季度汇总的年度列中正常使用。
- **结果公式中的常数自查指引**：
  复核预测金额公式时，区分结构性换算常数（`A/B - 1` 与 `1 - 税率` 中的 `1`、季/月/日年化折算 `*4` / `/4` / `*12` / `*365`、量纲换算 `100` / `1000` / `10000`、存托股换算比、勾稽容差）与业务假设常数；若金额结果公式中直接写死了业务参数或固定金额（如 `=E10*1.15` 或固定金额 `500`），将其抽到驱动假设格，由公式引用。
- **存量模型原表硬编码修复**：维护原模型时，若发现原表预测期金额行填了固定数字，能恢复计算关系的改为公式，属管理层指引等有依据直接输入的移到单独输入格引用，并在 `README & Changelog` 工作表写明改动。

### 3.8 汇总表规则

- **执行规则**：
  1. 每本工作簿配备汇总表：单公司模型设于 `Summary` 页或 `Valuation` 页顶部总览区；行业多公司模型设于行业总览页（`Peers` / `Global Comps` 或首页 `README & Changelog` 的各公司核心预测汇总表），同时每家公司保留各自的 `Valuation - <TICKER>` 页。
  2. **期间覆盖**：同时列出关键指标的**年度序列**（最近 1–2 个历史财年 + 全部预测财年）与**季度序列**（最近已披露季度 + 未来逐季预测列）。
  3. **必备指标与全公式链接**：**营业总收入（及同比增速）**与**稀释每股收益 `EPS` / `EPADS`（公司主流定价口径及同比增速）**为必备行；汇总表所有历史与预测单元格均通过跨表绿字公式或同表黑字公式直接指向底层主计算页的对应结果单元格。
- **需要分析师判断的事项**：汇总表除收入与 `EPS` 外补充哪些核心经营与盈利指标。
- **判断参考依据**：结合行业定价核心与卖方跟踪主线选取 3–6 项（如电商纳入核心 `GMV`、货币化率、核心分部 `EBITA`、集团 `Non-GAAP` 营业利润及利润率、`FCF` 与狭义净现金；券商纳入净新增入金客户、期末客户总资产 `AUM`、交易额与营业利润率；硬件与云计算纳入毛利率、研发费用率、`CapEx` 与 `FCF`）。

---

## 4. 格式规范、存量画像提取与工作簿安全写入

### 4.1 默认样式常量与实测版面参数（`scripts/model_style.py`）

4.1 节的默认样式常量提炼自两本用户实测模型（中国电商 40 页工作簿 `ecom_20261008.xlsx`、在线券商 7 页工作簿 `brokerage_20260820.xlsx`），**仅用于从零新建模型，以及存量模型中完全缺少同类参照的新增元素**；存量模型已有同类元素时一律以第 4.2 节提取的原模型画像为准。

| 样式元素 | 常量名（`model_style.py`） | 字体与字形 | 字色 `RGB`（红绿蓝十六进制色值） | 填充底色 `RGB` | 边框与对齐 | 数字格式 / 适用规则 |
|---|---|---|---|---|---|---|
| 基础字体 | `FONT_EN = "Arial"`<br/>`FONT_ZH = "等线"`<br/>`FONT_SIZE = 8` | 英文与数字用 `Arial` 8pt（磅）；含中文字符用 `等线` 8pt | 按角色设定 | — | 文本左对齐，数值与期间列头右对齐 | 新建计算表数据区统一 8pt |
| 第 1 行期间表头 | `HEADER_FILL = "3366FF"`<br/>`HEADER_FONT = "FFFFFF"` | 8pt 加粗 | 白字 `FFFFFF` | 亮蓝底 `3366FF` | 无边框；期间列右对齐，`A1` 左对齐 | `A1` 写单位，如 `(RMB MM)`、`(US$ MM)`、`(US$'000)`、`(HK$'000)` |
| 历史实际值 | `ACTUAL_FONT = "0000FF"` | 8pt 正体 | 蓝字 `0000FF` | 无底色 | 无边框 | 法定财报直接披露或纯算术差分得出的历史值 |
| 历史估算值 | `HIST_EST_FONT = "0000FF"`<br/>`HIST_EST_FILL_GRAY = "EDEDED"`<br/>`HIST_EST_FILL_YELLOW = "FFFFC8"` | 8pt（百分比用斜体，金额/数量用正体） | 蓝字 `0000FF` | 浅灰底 `EDEDED`（对应 Excel 主题 `accent3` 约 `+80%` 色调 `tint ≈ 0.80`）或浅米黄底 `FFFFC8` | 浅米黄底配四边 `hair` 细虚线框；浅灰底按原表无边框或细框 | 与预测假设分开建档，专用于标记历史未披露的推算/估算值（如 `Model - PDD!N16`） |
| 同表计算公式 | `FORMULA_FONT = "000000"` | 8pt 正体 | 黑字 `000000` | 无底色 | 无边框 | 显式设置金额、百分比或倍数数字格式 |
| 小计 / 合计行 | `TOTAL_BOLD = True` | 8pt **加粗**（标签与数值均加粗） | 同表黑字 / 跨表绿字 / 历史蓝字 | 无底色 | **无边框** | 用于总收入、毛利、营业利润、净利润等汇总行 |
| 跨表链接公式 | `LINK_FONT = "008000"` | 8pt 正体（合计行加粗；**若该行为衍生比率则同时设为斜体**） | 绿字 `008000` | 无底色 | 无边框 | 引用本工作簿其他工作表单元格（如 `Model - Tiger!G95` 为绿字斜体） |
| 预测驱动假设 | `ASSUMP_FONT = "0000FF"`<br/>`ASSUMP_FILL = "FFFFC8"`<br/>`ASSUMP_BORDER = "hair"` | 8pt；**百分比类用斜体**，金额/数量类用正体 | 蓝字 `0000FF` | 浅米黄底 `FFFFC8` | **四边 `hair` 细虚线框** | 用于非加总预测列的常数假设与四类合法驱动假设公式 |
| 衍生比率公式 | `RATIO_ITALIC = True` | 8pt **斜体** | 同表黑字 `000000` / 跨表绿字 `008000` | 无底色 | 无边框 | 增速、占比、利润率、加总年反算 `Take rate`，默认 `0.0%`（精细费率 `0.00%`） |
| 报表级分节条 | `SEC_REPORT_FILL = "99CCFF"` | 8pt 加粗 | 黑字 `000000` | 浅蓝底 `99CCFF` | **仅涂 A 列单个单元格**（子区块表头行如 `Subs - JD` 第 60 行可按原表跨期间列） | 用于 `Income statement`、`Balance sheet`、`Cash flow statement`、估值分组栏 |
| 分部 / Non-GAAP 分节条 | `SEC_SEGMENT_FILL = "993300"`<br/>`SEC_SEGMENT_FONT = "FFFFFF"` | 8pt 加粗 | 白字 `FFFFFF` | 棕红底 `993300` | **仅涂 A 列单个单元格** | 用于 `Non-GAAP measures`、`Segment Reporting`、主要子公司块 |
| 业务子模型分节条 | `SEC_SUBMODEL_FILL = "FFCC99"` | 8pt 加粗 | 黑字 `000000` | 浅桃底 `FFCC99` | **仅涂 A 列单个单元格** | 用于具体业务拆解子块（如 `Temu`、`1P Business`）及估值页目标价标题行 |
| 来源说明列 | `SOURCE_NOTE_FONT = "FF0000"` | 8pt 正体（或斜体） | 红字 `FF0000` | 无底色 | 置于最后一个期间列右侧首列 | 文本以 `<` 或 `<<` 开头（如 `<Yipit`、`<<GS 1117`）；不使用单元格批注 |
| 目标价结果格 | `TARGET_PRICE_FONT = "C00000"` | 8pt 加粗 | 深红 `C00000` | 无底色（或按标题行 `FFCC99`） | 无边框 | 用于 `Valuation` / `SOTP` 页核心每股目标价输出格 |

**标准数字格式常量**：
- `FMT_INT = "_(#,##0_);\\(#,##0\\);_(?\\-?_);@"`（整数金额主力格式：负数括号、零值显示为 `-`）
- `FMT_DEC1 = "_(#,##0.0_);\\(#,##0.0\\);_(?\\-?_);@"`（1 位小数金额）
- `FMT_DEC2 = "_(#,##0.00_);\\(#,##0.00\\);_(?\\-?_);@"`（2 位小数，用于 `EPS`、`EPADS`、股价）
- `FMT_PCT = "0.0%"` / `FMT_PCT2 = "0.00%"`（百分比与精细货币化率）
- `FMT_PP = '#,##0.0" pp"'`（百分点 `pp`〔percentage points〕变动）
- `FMT_MULT = '#,##0.0" x"'`（估值倍数，存量模型兼容 `#,##0.0\x`）
- `FMT_YM = 'yy"年"m"月"'`（月度跟踪表日期）

**版面与视图实测默认参数**：
- **列序与实测列宽区间**：`A–C` 窄层级缩进列（宽 `1.7–2.3`）+ `D` 主行标签列（宽 `22–37`） → **年度列在左** → 窄空隔列（宽 `0.7–1.8`） → `CAGR` 列 → 窄空隔列（宽 `0.7–1.8`） → **季度列在右** → **最右侧红字来源说明列**；期间数据列宽设为 `7.2–9.2`。
- **大纲折叠与冻结窗格**：距今超过 3 年的早年年度列与超过 8 个季度的早年季度列设置列大纲分组并折叠隐藏（`outlineLevel = 1, hidden = True`）；关闭网格线（`showGridLines = False`）。**新建模型**默认将冻结窗格（`freeze_panes`）设在第 2–3 行与最近几个季度列附近（如 `AZ3`）；**存量模型实测冻结位置常指向该页核心分析区块行**（如 `Peers!BB22`、`Model - JD!AZ102`、`Model - Futu!J159`），维护存量模型时按页保留原冻结行号，仅将冻结列随新增季度平移。
- **多公司工作簿页签组织**：行业总览页（同业可比表 `Global Comps`、潜在市场总规模 `TAM`〔Total Addressable Market〕、`Monthly Tracking`、`Peers`）在前，之后每家公司前可放一张空白分隔页 `{TICKER}>>`（公司代码后接两个右尖括号，如 `PDD>>`、`JD>>`、`Tiger>>`），同公司下属页签使用同一组页签颜色（`tabColor`）。

### 4.2 存量模型样式画像提取与机械样式辅助（`model_style.py`）

维护存量模型前，运行 `python3 .agents/skills/dalton-model/scripts/model_style.py profile <原模型.xlsx> --out style_profile.json` 提取原模型实测样式摘要（文件缺失或损坏时平滑退回 `default_style_profile()` 并在 `fallback_reason` 注明原因）：

1. **按实际内容识别工作表类型（保留非空 `>>` 页签）**：
   只遍历工作簿中实际存储的单元格（`_iter_stored_cells`，避免远端稀疏单元格引起矩形全表展开），统计每页的非空单元格数、公式数与数值数：
   - **空白分隔页（`blank_divider`）**：可见工作表无公式且无数值、非空单元格不超过 2 个（如 `PDD>>`、`Tiger>>`）；
   - **内容页（`content_sheet`）**：含实际公式或数值的可见工作表（包括名称以 `>>` 结尾但承载估值公式的 `Futu>>`）；
   - **隐藏页（`hidden_sheet`）**：`sheet_state` 非 `visible` 的工作表（`hidden` / `veryHidden`）。
2. **解析工作簿主题调色板（`theme + tint -> RGB`）**：
   读取压缩包内 `xl/theme/theme1.xml` 的 `<a:clrScheme>` 12 个主题色槽，结合单元格 `tint` 浮点值换算为 6 位十六进制 `RGB` 色值（如 `theme=1, tint=0` 还原为 `000000`，`theme=6, tint≈0.80` 还原为约 `EDEDED`）。
3. **两层实测摘要输出（`workbook_defaults` + `sheet_overrides`）**：
   - `workbook_defaults` 记录全工作簿观察到的主英文字体 `dominant_font_en`、主中文字体 `dominant_font_zh`、主字号 `dominant_font_size`、表头底色/字色 `header_fill`/`header_font_color`、实际数与跨表链接字色、假设样式 `assumption_style`、分节色 `section_fills`/`section_styles` 与数字格式集合（未观察到的要素回退采用 4.1 节默认值）；
   - `sheet_overrides[工作表名]` 记录该页的 `sheet_type`、`sheet_state`、`dominant_font_size`、`dominant_font_en`、`dominant_font_zh`、`header_fill`、`header_font_color`、`fonts_observed`、`fills_observed`、`section_fills`、`section_styles`、`number_formats_observed`、`freeze_panes`、`gridlines_hidden`、`sample_col_widths` 与前 40 个 A 列分节标题 `blocks`，供智能体挑选参照样式或传入 `apply_cell_style`。
4. **单元格写入与列样式克隆**：
   - `apply_cell_style(cell, role, ..., profile=None, sheet_name=None)` 按角色（`"header"`、`"historical_actual"`、`"historical_estimate"`、`"driver_assumption"`〔含管理层指引直接输入金额〕、`"external_input"`、`"source_note"`、`"target_price"`、`"sec_report"`、`"sec_segment"`、`"sec_submodel"`、`"formula"` 配合 `is_link`/`is_ratio`/`is_total`）写入样式；传入 `profile` 时优先采用画像中的字体、字号、表头色、分节色与假设样式，未传画像时采用基金默认样式；
   - `clone_period_column_style(ws, src_col, dst_col)` 复制同用途期间列的完整单元格样式（含 `font`、`fill`、`border`、`alignment`、`protection` 与 `number_format`）及列宽，遇到合并区域内非左上角 `MergedCell` 时自动跳过。

### 4.3 工作簿安全扩列、外部依赖处理与保存方式

1. **在输出副本上运行 `insert.py --out` 并更新边界汇总区间**：
   - 原件保持只读；先复制到 `model/` 下的新版本路径（或使用 `.agents/skills/xlsx/scripts/insert.py ... --out <新版本路径>` 直接生成新版本）；
   - 扩列或插行使用 `.agents/skills/xlsx/scripts/insert.py`（带 `--dry-run` 预检、`--copy-style-from <相邻同类列>` 复制格式），核查返回 JSON 的 `status`、`self_check.ok` 与 `warnings`；
   - **显式更新边界扩列涉及的汇总公式**：`insert.py` 仅当插入点严格位于原 `SUM(起始:结束)` 区间内部时才自动扩展区间；在末尾季度或年度右侧追加新列后，显式写入新季度加总年的 `SUM` / `=Q4` / `AVERAGE` 公式、顺延 `CAGR` 起止年份，并更新横向汇总区间。
2. **外部插件公式与外部链接的标准抽离替换**：
   - 原模型常含无插件环境下无法求值的第三方插件函数（如 Capital IQ `_xll.ciqfunctions`、Bloomberg `BQL`/`BDH`）或外部工作簿链接部件（`xl/externalLinks/*.xml`，存在外链时 `recalc.py` 默认拒绝重算）。
   - **替换规则**：原件保持只读；在新版本工作簿中，影响核心结果的外部插件或外链数值抽离到单独输入单元格（最右备注列注明来源与取数日期），原单元格改为引用该输入格并清理外链后再运行 `recalc.py`；不影响核心结果的参考表可保留并在备注列写「需插件刷新」。
3. **存量工作簿特殊部件的处理**：
   - 部分用户工作簿包含 `openpyxl` 保存时会丢弃的部件（如 WMF 图像、绘图、`<extLst>` 条件格式扩展）。`insert.py` 仅重写变动部件、原样保留未改动部件；若后续还需编辑单元格，按 `.agents/skills/xlsx/SKILL.md` 的存量编辑说明处理（必要时用 `lxml` 定点补丁工作表 XML，或在交付说明中如实告知用户会被丢弃的部件）。

---

## 5. 模型提示性扫描脚本设计（`model_scan.py`）

`dalton-model` 配备只读提示性扫描脚本 `plugins/dalton/skills/dalton-model/scripts/model_scan.py`，保持上游 `plugins/langalpha_deliverables/skills/xlsx/scripts/audit.py` 不改动。鉴于不同公司的指标口径、正负号习惯和财年结账日各异，本脚本不试图用硬代码替分析师作对错裁决，也不设 `--strict`、`fail/block` 或 `delivery_decision` 阻断门禁，而是把「值得核对的单元格与原因」整理成结构化提示，交由智能体逐条判断。

### 5.1 命令行接口与退出码

```bash
python3 .agents/skills/dalton-model/scripts/model_scan.py <workbook.xlsx> \
  [--sheet "<工作表名>"] \
  [--recalc] \
  [--max-items 50]
```

- **只读、稀疏遍历与副本重算**：默认仅遍历工作簿中实际存储的单元格（避免远端稀疏格引起内存膨胀），读取公式（含 `ArrayFormula`）、样式与现有缓存值，原件保持字节级只读；未指定 `--sheet` 时扫描全部可见工作表，显式指定 `--sheet` 时即使该表为隐藏状态也执行扫描并在摘要注明。传入 `--recalc` 时，依次按 `.agents/skills/xlsx/scripts/recalc.py` 与仓库内 `plugins/langalpha_deliverables/skills/xlsx/scripts/recalc.py` 定位重算脚本，复制临时副本运行重算并捕获超时（`TimeoutExpired`）及异常；若因外链跳过、未找到脚本、执行失败或超时，自动回退报告原文件现有缓存错误并在 `recalc_note` 说明状态，退出码仍为 `0`。
- **退出码规则**：
  - `0`：扫描正常完成（无论检出多少条核对提示，均不因发现提示项而返回非零退出码）；
  - `1`：仅当扫描本身出错（文件不存在、参数错误、工作簿损坏无法打开或指定的 `--sheet` 不存在）。

### 5.2 四类提示项与防占满汇总规则

每条提示均附带 `sheet`（工作表）、`cell`（单元格坐标或汇总代表项）与 `reason`（提示原因），供智能体逐条判断；同类重复项按来源或工作表汇总计数并给出代表性坐标，防止默认前 50 条被单一类别占满：

| 提示类别（`category`） | 扫描与汇总内容 | 处理方式 |
|---|---|---|
| `forecast_constants` | 预测期列（按第 1–5 行列头识别 `E`/`F`/`P` 后缀，排除 `CAGR`/`YoY` 等列）中出现的全部数字常数；按工作表分组输出，**无底色金额常数排在带底色假设格前面**，并附 `forecast_constants_by_sheet` 各表统计。未在前 5 行识别到期间表头的可见非空工作表会在 `sheets_without_period_header` 与摘要中列出，由模型自行查看。 | 智能体逐条核对：属驱动假设或外部静态输入的保留，属预测金额结果硬编码的改用公式。 |
| `possible_period_mismatch` | 按列头粗略比对表头下方公式引用列的年份与所在列年份，标出「可能存在期间不一致」的单元格（均明确标注为「可能」）。 | 结合公司财年结账月（如 3 月结账跨自然年求和）与跨期逻辑核对，确认无误则保留。 |
| `external_or_broken_or_circular` | 全表扫描（含表头行及表头上方）：工作簿压缩包外链部件汇总为 1 条计数项、外部与 `#REF!` 命名区域分别汇总计数并列代表项、公式断裂引用 `#REF!`（含 `IFERROR`/`IFNA` 内）单列、按 `Tokenizer` 函数词元识别的第三方插件公式（忽略字符串字面量，覆盖 `ArrayFormula`，按工作表+插件汇总计数并列代表格）、外链公式按表汇总，以及迭代开关与依赖循环引用。 | 交付新版本前通常清理外链与插件函数、修复 `#REF!` 并消除循环引用。 |
| `recalc_errors` | 重算后（或重算未执行/回退时的原文件缓存）Excel 错误值（`#DIV/0!`、`#REF!`、`#NAME?`、`#VALUE!`、`#N/A`、`#NUM!`、`#NULL!`、`#SPILL!`、`#CALC!`、`#ERROR!`），按 `(sheet, error_code)` 汇总计数并列代表单元格，**非 `#NAME?` 错误排在 `#NAME?` 前面**。 | 逐项定位并修复错误公式。 |

### 5.3 输出 JSON 与处理原则

`model_scan.py` 向标准输出打印单个 UTF-8 JSON 对象，包含 `status`（`"ok"` 或 `"error"`）、`sheets_scanned`、`sheets_without_period_header`、`recalc_ran`、`recalc_note`、`plugin_formula_counts`、`forecast_constants_by_sheet`、简短中文摘要 `summary`、各类别原始计数 `counts`、截断标记 `truncated` 与提示明细 `items`。

**智能体处理原则**：逐条查看提示清单，**确认是问题就修改模型并重新核对；结合公司实际判断不属于问题的，在变更记录（如 `README & Changelog` 或交付说明）里写一句原因即可**。

---

## 6. 合理性检查、一致预期对账与理由写入工作簿

### 6.1 市场一致预期与卖方预测取数规则（按指标处理状态与双能力门控）

1. **按指标与期间读取返回状态、通用来源及 `implied_model` 口径**：
   - **覆盖范围与双部署共用来源**：公开 MCP（Model Context Protocol，模型上下文协议）工具 `yf_analysis.get_revenue_estimates`、`yf_analysis.get_earnings_estimates`（返回 `0q` 当季、`+1q` 下季、`0y` 本财年、`+1y` 下一财年四个期间的营业收入与每股收益一致预期均值、高低区间、覆盖分析师人数、期末日期 `period_end` 与 `methodology`）以及可选 `FMP`（Financial Modeling Prep，金融数据服务商）工具 `fundamentals.get_analyst_estimates`（配置密钥后返回按财年或财季展开的多期收入、`EBITDA`、息税前利润 `EBIT`、净利润与 `EPS` 一致预期）**在 Everflow 生产形态与独立核心安装形态中均可使用**；生产形态下还可调用 `researchdata.finance(route="estimates", operation="consensus", ticker=...)`（支持美股与港股代码）。
   - **按指标和期间读取 `field_status` 与有效数值**：桥接接口顶层 `status == "partial"` 可能由行情、目标价或 `implied_model` 缺项引起，调用方按指标（`revenue_estimate`、`earnings_estimate`）和具体期间读取 `field_status` 与有效数值，`partial` 只影响实际缺项的指标或期间，已返回的收入与 `EPS` 照常采用；需要确认财季归属时结合公开 `yf_analysis` 的 `period_end` 或公司财报日历核实。
   - **`implied_model` 反推性质与利润预期来源**：桥接返回的 `implied_model`（含隐含税前利润、总费用与经营利润率）是由一致预期收入、`EPS`、稀释股数与税率反推的利润，引用时标注 **「由一致预期 `EPS` 折算」**，不当作营业利润或净利润的一致预期。营业利润、净利润、各分部拆解及 `+2y`（如 `2028E`）等远期预测查阅卖方研究报告（或已配置密钥时的 `fundamentals.get_analyst_estimates`）；A 股公司在桥接 `estimates` 无返回时同样查阅卖方研究报告或公开一致预期汇总。
   - **区分三类异常与缺失情况**：工作簿对照表与文字说明明确区分三种情况：（1）**取数失败**（接口返回 `error` 或连接超时，如实写明取数失败原因）；（2）**覆盖不足**（接口正常返回但该指标或期间为空、分析师人数为 `0`，或卖方与公开渠道均无预测，外部预期格留空并标「无公开一致预期」）；（3）**期间或会计口径无法核实**（外部数据的财季截止日 `period_end`、币种、每股/`ADS` 单位或 `GAAP`/`Non-GAAP` 口径无法确认，仅列原始参考值并标注「期间或会计口径无法核实」，不计算偏离幅度）。
2. **双形态独立能力门控组装（分别按 `researchdata_bridge` 与 `alphaengine` 门控）**：
   `dalton-model` 正文与 `references/sanity-check.md` 将专有桥接调用及说明全部置于条件注释块内，使核心安装组装后零专有词且两种部署形态均可调用公开 `yf_analysis` 与可选 `FMP`：

```markdown
<!-- DALTON_CAP:researchdata_bridge -->
1. **市场一致预期（按指标与期间提取有效值）**：美股与港股调用 `researchdata.finance(route="estimates", operation="consensus", ticker=...)`，按 `field_status` 逐指标（`revenue_estimate`、`earnings_estimate`）和期间（`0q`、`+1q`、`0y`、`+1y`）提取有效均值、高低区间、币种与分析师人数（`status == "partial"` 仅影响缺项指标；返回中的 `implied_model` 为由一致预期 `EPS` 反推的利润，引用时标注「由一致预期 `EPS` 折算」，不当作营业利润或净利润的一致预期）；同步可调用公开 `yf_analysis.get_revenue_estimates` / `get_earnings_estimates` 核实 `period_end` 与 `methodology`，已配置 `FMP` 密钥时可调用 `fundamentals.get_analyst_estimates(symbol=..., period="annual"|"quarter")` 补充多期收入、`EBITDA`、`EBIT`、净利润与 `EPS` 一致预期；A 股标的转查下方卖方报告或公开一致预期汇总。原始返回保存快照至 `data/consensus/<YYYY-MM-DD_HHMM>.json`，并区分取数失败、覆盖不足、期间或会计口径无法核实三种情况。
<!-- DALTON_CAP_ELSE -->
1. **市场一致预期（公开市场数据与可选 `FMP` 接口）**：调用公开工具 `yf_analysis.get_revenue_estimates(ticker=...)` 与 `yf_analysis.get_earnings_estimates(ticker=...)`，按指标与期间（`0q`、`+1q`、`0y`、`+1y`）获取收入与 `EPS` 一致预期均值、高低区间、分析师人数、期末日期 `period_end` 与计算口径 `methodology`（`gaap` 或 `normalized`，留意 `year_ago_stale` 标记）；当环境已配置 `FMP` 密钥使 `fundamentals.get_analyst_estimates(symbol=..., period="annual"|"quarter")` 可用时，同步调用补充多期收入、`EBITDA`、`EBIT`、净利润与 `EPS` 一致预期；将取数结果保存快照至 `data/consensus/<YYYY-MM-DD_HHMM>.json`，并区分取数失败、覆盖不足、期间或会计口径无法核实三种情况。
<!-- /DALTON_CAP -->

<!-- DALTON_CAP:researchdata_bridge,alphaengine -->
2. **卖方报告（营业利润、净利润、分部拆解与 `2028E` 等远期预测）**：调用 `researchdata.research(route="alphaengine", ...)` 检索最近一个财报季后 3–5 家覆盖机构的深度与业绩点评报告，提取各分部收入与利润率、集团营业利润、净利润及 `+2y`（如 `2028E`）远期预测数字与核心假设；报告未覆盖的远期或细分项标注「无公开一致预期」。
<!-- DALTON_CAP_ELSE -->
2. **卖方与公开预期参照（营业利润、净利润、分部拆解与远期预测）**：优先读取用户放入工作区 `sources/` 的参考报告，结合公司官方投资者关系（`IR`〔Investor Relations〕）网站披露的一致预期汇总、公开财报电话会问答纪要，以及通过 `web_search` / `web_fetch` 检索到的公开机构业绩点评摘要，提取分部、营业利润、净利润与远期预测参考值；公开渠道未披露的期间或分部标注「无公开一致预期」。
<!-- /DALTON_CAP -->
```

> **快照与来源标注纪律**：每次取数均落盘带时间戳的快照文件，并在工作簿对照表注明数据源、提取日期与样本数（市场一致预期标如 `<Yahoo Finance 2026-11-25, N=34`，单家卖方标如 `<Morgan Stanley 2026-11-18`），严格区分市场一致预期（`consensus`）、单家卖方预测（`broker estimate`）、由一致预期 `EPS` 折算值、公司法定披露（`fact`）与本方假设（`assumption`）。

### 6.2 可比期间重大差异拆解四步法

对**每一个已完成口径核实的预测期间**（包括 `0q`、`+1q` 预测季度，以及 `0y`、`+1y`、`+2y` 等已获取市场一致预期或单家卖方预测的预测年度）和**每一个可比核心指标**（收入、分部收入、营业利润、净利润、`EPS`/`EPADS`）逐一评估差异幅度。定量筛查可参考：**收入偏离约 ±3% 以上，或营业利润 / 净利润 / `EPS` 偏离约 ±5% 以上，或微利转型公司营业利润率偏离约 ±150 bps（basis points，基点，1 个百分点 = 100 基点）以上**（上述幅度均为**建议参考值**，可按公司历史业绩波动率与估值敏感度收紧或放宽）。按以下四步执行：

1. **第一步：先核实五项基础口径，再计算偏离**
   **核实财季归属、币种、会计准则、每股单位与并表范围并换算至同一基准后，才计算偏离百分比**（任一项无法确认时按第 6.1 节标注「期间或会计口径无法核实」，留空偏离列）：
   - **财年截止日与财季归属**：核对外部预期的 `period_end` 与模型财年截止月及财季区间完全一致（如 3 月底或 6 月底结账公司）；
   - **报表币种与交易币种**：核对外部数字采用报表本位币（如 `RMB`）还是股票交易币种（如 `USD`、`HKD`）及折算汇率；
   - **会计准则与调整项口径**：核对外部预期采用 `GAAP` / 国际财务报告准则（`IFRS`〔International Financial Reporting Standards〕）还是 `Non-GAAP` / `Normalized`（逐项核对 `SBC`、并购无形资产摊销、投资公允价值变动等调增调减范围）；
   - **每股与存托股单位**：核对每股收益对应普通股还是每份 `ADS`（如 `1 ADS = 8 普通股` 或 `2 普通股`）；
   - **持续经营与并表范围**：确认双方同口径包含或剥离近期终止经营业务。
2. **第二步：有来源支撑的逐层归因与未解释差额单列**
   - **分别标明对比基准类型**：在表格中明确区分该行对比的是「市场一致预期均值（`Consensus`）」还是「单家或数家卖方机构预测（`Broker Estimate`，写明机构名）」。
   - **只量化有外部数据支撑的归因项**：当具备卖方分部模型或多指标一致预期时，按计算链路量化各已知环节的贡献（分部收入规模差、分部利润率与未分配总部开支差、净现金/债务滚存带来的净利息差、有效税率差、回购与 `SBC` 带来的稀释股数差）；当公开渠道仅有集团总收入与 `EPS` 两项一致预期、缺少市场对分部或费用的拆分数字时，**只量化收入差及有据可查的项目，将无法由公开数据拆细的剩余部分如实列为「未解释差额（`Unexplained Variance`：公开一致预期未披露分部/费用明细）」**，不反推捏造外部未披露的分部共识数字。
3. **第三步：排查建模疏漏与确认独立观点（`contrarian view`）**
   - 若拆解发现差异源于我方分部汇总漏计总部未分配费用、单季偶发补贴或汇兑收益被线性外推、期初生息现金口径重复、回购扣了股数漏扣现金流、或季度加总年残留旧假设，立即修正模型公式；
   - 若非核心科目的偏离缺少一手证据支撑，将假设回调至公司历史稳态中枢；
   - 若偏离来自核心业务争议点，且有法定财报先行指标（如合同负债/递延收入）、高频跟踪数据、管理层最新资本开支或股东回报计划支撑，确认为合理的主动独立观点，保留我方预测。
4. **第四步：工作簿内记录具体理由与复核 `Checks` 页**
   对所有保留下来的重大偏离逐条写明具体业务驱动、量化差值与证据出处（如 `JD 2028E Non-GAAP 净利润低于卖方中位预期（GS/MS 2026-11 报告）7%：我方对新业务按 3Q26 电话会扩招指引多计 35 亿投入，同时按回购扣减现金后下调利息收入 8 亿`），并确认 `Checks` 页符合第 5.3 节交付规则。

### 6.3 判断依据（`rationale`）在工作簿内的写入位置

| 模型形态 | 工作簿内写入位置 | 内容与格式要求 |
|---|---|---|
| **存量模型（单公司或行业多公司模型）** | **位置 1（主表位）：各公司已有 `Valuation - <TICKER>` 或 `Summary` 页下方的 `Consensus / Broker vs House` 对照表**（原模型无独立估值或汇总页时置于主表下方或独立对照表，A 列涂 `99CCFF` 分节条）<br/>**位置 2（驱动行旁注）：`Model - <TICKER>` / `Key Metrics - <TICKER>` 对应驱动假设行的最右侧红字备注列** | 1. 对照表列结构：`指标 | 期间（含可比季度与年度）| House 预测 | 外部预期值 | 基准类型（Consensus / 单家卖方）| 差异（% 或 bps）| 已证实归因拆解与未解释差额 | 坚持独立观点的具体理由与证据出处 | 外部来源、日期与状态说明`。<br/>2. `House 预测` 列用跨表绿字公式（或同表黑字公式）引用计算页；`外部预期值` 列用蓝字输入（异常或缺失时按第 6.1 节区分标注取数失败、无公开一致预期、期间或会计口径无法核实）；核实口径可比的期间在 `差异` 列写斜体公式。<br/>3. 行业多公司模型同步在首页 `README & Changelog` 总览表旁附「相对外部预期核心差异摘要」。 |
| **从零新建的单公司模型** | **独立工作表 `Street vs House`**（置于情景表之后，与 `dalton-full-coverage` 的同名工作表结构兼容） | 按可比季度与年度列出集团总收入、关键分部收入与营业利润、毛利率、经营费用、`Non-GAAP` 营业利润、净利息、有效税率、加权稀释股数、`Non-GAAP EPS` 与 `FCF`；`House` 列全部用绿字跨表公式引用计算页，逐项标明外部预期类型、有据归因、未解释差额、独立判断理由与跟踪验证条件。 |

---

## 7. 模型存放路径、双目录判定与版本管理

### 7.1 统一 `model/` 目录与 `model/`、`models/` 并存判定规则

- **统一正式目录**：单公司工作区与行业多公司工作区统一使用工作区根目录下的 `model/` 存放正式 `.xlsx` 模型文件，直接匹配 `src/dalton/knowledge/schemas/models.schema.json` 的路径校验规则（`^model/.+\.xlsx$`）。调用 `knowledge_update(doc="models", ...)` 登记版本时，客户端可先计算并核对文件 `sha256`；当服务端（`src/dalton/knowledge/service.py` 的 `_populate_model_sha256`）能够读取工作区文件后端时，也会为未填 `sha256` 的版本记录自动补算哈希值。
- **`model/` 与历史 `models/` 并存时的维护起点判定**：
  若存量工作区同时存在 `model/` 与历史遗留的 `models/` 目录：
  1. 先计算两边 `.xlsx` 文件的 `SHA-256` 哈希：**哈希完全相同的文件直接认定为同一产物的重复副本**，以 `model/` 下的路径作为基准；
  2. 若两边存在哈希不同的文件，先读取 `data/models.json` 的 `current` 版本号（如 `"v12"`），在 `versions[]` 数组中找到 `id` 与 `current` 相同的条目并取该条目的 `path` 字段，结合用户在当前会话中明确指定的底稿文件确定唯一维护起点；
  3. 若 `data/models.json` 尚未登记（或 `current` 为空）且用户未指定具体底稿，而两目录存在哈希不同的独立修改版本，先列出两份文件的路径、修改时间、版本号与核心页差异交用户确认起点，后续新版本统一写入 `model/` 并在 `data/models.json` 登记（原 `models/` 下的旧文件保持只读留存）。
- **构建与更新脚本归档**：当次编写的 Python 更新脚本、`style_profile.json` 与 `model_lineage.json` 统一存放在 `research/<YYYY-MM-DD>-model-<动作>/` 下，使 `model/` 目录只保留历次 `.xlsx` 模型版本文件。

### 7.2 原件只读留存、新版本命名与普通版本记录

1. **原件只读留存**：用户上传至 `sources/` 的原始模型文件以及 `model/` 下所有既有历史版本（`v01`、`v02` 等）均保持字节级只读不动；操作前记录基准文件 `SHA-256` 并写入新模型 `README & Changelog` 页，交付前复核原文件哈希一致。
2. **新版本文件命名**：
   - **单公司工作区**：命名为 `model/<TICKER>-model-v<NN>.xlsx`（两位递增版本号，如首次采用 `sources/` 用户底稿滚动更新产出 `model/FUTU-model-v01.xlsx`，后续季报更新或审稿修订另存为递增版本号）。
   - **行业多公司工作区**：命名为 `model/<Industry_Slug>_Model_v<NN>_<YYYYMMDD>.xlsx`（空格替换为下划线，如 `sources/China eCommerce_Industry Model_20261008.xlsx` 原件不动，首次滚动更新输出 `model/China_eCommerce_Industry_Model_v01_20261125.xlsx`，后续修订或下季更新递增 `v02`、`v03`）。
3. **知识层登记与附加的普通记录（`model_lineage.json`）**：
   - 正式交付版本统一调用 `knowledge_update(doc="models", ...)` 写入 `data/models.json`（必填 `id`〔形如 `v03`〕、`path`〔形如 `model/....xlsx`〕、`created_at`、`reason`，并将 `current` 指向新版本 `id`）。
   - 可选调用 `python3 .agents/skills/dalton-model/scripts/model_style.py lineage --file <model_lineage.json> --output <新版本.xlsx> [--baseline <原模型.xlsx>] [--profile <style_profile.json>] [--note "..."]` 在当次研究目录留存普通记录（字段为 `input_file`、`input_sha256`、`output_file`、`output_sha256`、`profile_file`、`recorded_at`、`note`），方便回溯输入输出哈希，不代替知识层登记。

---

## 8. 技能注册、能力门控与分批修改清单

### 8.1 注册与能力映射（`CORE_CAPABILITY`，核心安装与生产形态共用的基础能力标记）

`dalton-model` 依托本地 `.xlsx` 读写、公式重算与公开市场数据工具即可完整运行，在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 中注册为 **`CORE_CAPABILITY`**（即 `None`）：

- `_DALTON_SKILL_SPECS` 新增条目：`("dalton-model", "dalton-model", ("dalton-conventions", "research-conventions"))`；
- `DALTON_SKILL_CAPABILITY_MAP` 核心投资研究骨架分组新增 `"dalton-model": CORE_CAPABILITY`，并将该分组上方注释中的 `# --- Core investment research skeleton (29 skills) ---` 更新为 `30 skills`（Dalton 内置技能总数由 **51** 增至 **52**）。

### 8.2 第 1 批（新技能本体与注册）涉及文件

| 类别 | 文件路径 | 修改内容 |
|---|---|---|
| 新技能目录 | `plugins/dalton/skills/dalton-model/**` | 新增 `SKILL.md`、`references/{format-spec,forecast-rules,rollforward,sanity-check}.md`、`scripts/{model_style,model_scan}.py`（按 `researchdata_bridge` 与 `alphaengine` 分别门控专有数据源说明，零内部域名硬编码）。 |
| 方法论索引与一致预期入口 | `plugins/dalton/skills/dalton-conventions/SKILL.md` | 1. 在第 4 节按任务查参考表格的「财务建模 / 历史对表 / 财务镜头」行增列 `.agents/skills/dalton-model/SKILL.md`；<br/>2. 将第 6 节「卖方一致预期（`estimates`）」核心安装分支中的 `yf_fundamentals` 改为 `yf_analysis.get_revenue_estimates` / `get_earnings_estimates`（及可选 `fundamentals.get_analyst_estimates`）。 |
| 技能注册表 | `src/ptc_agent/agent/middleware/skills/dalton_registry.py` | `_DALTON_SKILL_SPECS` 与 `DALTON_SKILL_CAPABILITY_MAP` 新增 `dalton-model`，分组注释 `29 skills` 改为 `30 skills`。 |
| 能力、注册与独立核心检查 | `tests/unit/dalton/test_core_capabilities_gate.py`<br/>`tests/unit/dalton/test_b3_skills_data_map_prompts_migration.py`<br/>`tests/standalone/checks/b3_skills_prompts_ui.py` | `test_core_capabilities_gate.py` 与 `tests/standalone/checks/b3_skills_prompts_ui.py`（`len(enabled_skills) == 29`）的核心可见技能数断言更新为 `30`；磁盘技能总数断言由 `51` 更新为 `52`。 |
| 快照采集与技能总数断言 | `tests/unit/dalton/_everflow_parity_capture.py` 及 8 个含技能总数断言的单测文件（`test_agentturn_ai_app.py`、`test_agentturn_mark_weekly.py`、`test_agentturn_richard_earnings.py`、`test_agentturn_tmeet.py`、`test_agentturn_wiki_ingest.py`、`test_declared_sources.py`、`test_podcast_weekly_migration.py`、`test_richard_coverage_weekly.py`） | `_everflow_parity_capture.py` 两处磁盘技能数断言更新为 `52`；8 个单测文件中所有 `len(_DALTON_SKILL_SPECS) == 51` 与 `test_declared_sources.py` 中的 `len(DALTON_SKILL_DEFINITIONS) == 51` 全部更新为 `52`。 |
| Everflow 快照 | `tests/unit/dalton/snapshots/everflow/**` | 运行 `pytest tests/unit/dalton/test_everflow_profile_parity.py --update-everflow-snapshots` 更新技能清单、提示词与组装全文快照。 |
| 新增专项单测 | `tests/unit/dalton/test_modeling_skill.py` | 覆盖 `model_style.py`、`model_scan.py`、多组合能力门控装配与合成工作簿测试（见第 9.1 节）。 |
| 路径登记检查 | `scripts/dalton/dalton_paths.txt`（零改动，运行 `scripts/dalton/check_hooks.sh --strict` 验证） | 全部新增与修改文件均位于已登记的 Dalton 前缀内，零上游文件修改。 |

### 8.3 第 2 批（`dalton-earnings-deep` 与 `dalton-full-coverage` 切换接入）涉及文件

第 2 批将两条工作流与手动执行路径中的建模与季报更新切换至 `dalton-model` 指引与 `model_scan.py --recalc` 提示性扫描，并用 `model_lineage.json` 记录实际输入与输出文件：

| 类别 | 文件路径 | 修改内容 |
|---|---|---|
| 财报深度链技能与工作流 | `plugins/dalton/skills/dalton-earnings-deep/SKILL.md`<br/>`workflows/earnings-deep-update/{workflow.js,contract.json}` | 阶段 1 支线 B 与修订循环改读 `.agents/skills/dalton-model/SKILL.md`，使用 `model_scan.py --recalc` 扫描并逐条核对提示，用 `model_lineage.json` 记录输入输出路径与哈希（保留 `mapping.md`、`Change Log` 与 `model-delta.md`）。 |
| 完整覆盖技能与工作流 | `plugins/dalton/skills/dalton-full-coverage/{SKILL.md,references/model-spec.md,references/memo-writing.md}`<br/>`workflows/full-coverage/{workflow.js,contract.json}` | 阶段 2 建模、阶段 4 只读审模型（改用 `model_scan.py`，不再跑上游 `xlsx/scripts/audit.py --strict`）与阶段 5 修订统一切换至 `dalton-model` 指引与 `model_scan.py`。 |
| 工作流单测与快照 | `tests/unit/dalton/test_workflows_js.py`<br/>`tests/unit/dalton/snapshots/everflow/**` | 更新工作流单测断言并刷新 Everflow 快照。 |

---

## 9. 测试方案与验收标准

### 9.1 自动化单元测试（`tests/unit/dalton/test_modeling_skill.py`）

不依赖外部网络、数据库或未入库的私有用户大文件，测试固件均用 `openpyxl` 在测试内现场构造：

1. **技能注册与三组能力组合装配测试**：
   - 验证 `dalton-model` 在 `DALTON_PROFILE=core` 与 `DALTON_PROFILE=everflow` 下均可由 `get_skill("dalton-model")` 正常解析，依赖包含 `dalton-conventions` 与 `research-conventions`；
   - 对 `plugins/dalton/skills/dalton-model/` 下全部 `.md` 文件及 `dalton-conventions/SKILL.md` 测试三组能力组合的 `read_assembled_text` 输出：核心全关组合零专有词命中；桥接开启、卖方关闭组合零 `alphaengine` 命中；桥接与卖方全开组合无残留 `DALTON_CAP` 标记。
2. **`model_style.py` 默认格式写入、画像沿用与谱系记录测试**：
   - 验证 `apply_cell_style` 与 `clone_period_column_style` 正确写入默认样式与克隆相邻期间列样式，`normalize_period_for_knowledge` 正确转换年度、季度与 `CAGR` 标签；
   - 验证 `profile_workbook_style` 提取主题色+色调还原、各页字号（含非空 11pt 页签）、分节条与常见数字格式，并在文件缺失或无法读取时平滑退回默认样式配置；
   - 验证 `record_model_lineage` 作为普通记录保存输入文件、输出文件、哈希与时间戳。
3. **`model_scan.py` 提示性扫描测试**：
   - 现场构造含四类提示情形的合成工作簿，验证 `model_scan.py` 列出预测期数字常数（金额行与假设行一并列出）、可能期间不一致（标为「可能」）、外链/插件/`#REF!`（含 `IFERROR` 内）/循环引用及公式错误值，每条均含工作表、单元格与原因，输出合法 JSON 与简短摘要且退出码为 `0`；
   - 验证 `model_scan.py --recalc` 在临时副本上调用上游 `recalc.py` 检出重算错误值且不修改源文件哈希，退出码仍为 `0`；
   - 验证仅当文件缺失、损坏或指定工作表不存在时返回退出码 `1`。

### 9.2 两本真实样例的本地只读扫描验收

宿主机对 `.agy/modeling_samples/ecom_20261008.xlsx` 与 `brokerage_20260820.xlsx` 运行只读扫描（样本文件不入仓库，原件哈希不变）：
- 两本样例扫描均不崩溃、返回退出码 `0`、输出合法 JSON 与简短摘要；
- `ecom_20261008.xlsx` 正确提示 Capital IQ、`BQL`、`BDH` 插件公式与 `Peers!I36` 隐藏 `#REF!` 等；
- `brokerage_20260820.xlsx` 正确提示外部链接部件与插件公式等；
- 原文件运行前后 `SHA-256` 一致。

---

## 10. 分批实施计划

| 批次 | 交付范围 | 验收标准 |
|---|---|---|
| **第 1 批：`dalton-model` 技能指引、机械样式与提示性扫描脚本及核心注册** | 1. 完成 `plugins/dalton/skills/dalton-model/` 下 `SKILL.md`、4 份 `references/*.md`、`scripts/model_style.py` 与 `scripts/model_scan.py`；<br/>2. 按第 8.2 节更新 `dalton_registry.py`（含 `30 skills` 注释）、`dalton-conventions/SKILL.md`（方法论索引及第 6 节 `yf_analysis` 一致预期工具更正）、技能数断言及 `tests/unit/dalton/snapshots/everflow/` 快照；<br/>3. 交付 `tests/unit/dalton/test_modeling_skill.py`。 | 1. 单测覆盖默认格式写入与画像沿用、四类扫描提示、副本重算、三组能力组合全文装配及注册断言全部通过；<br/>2. 两本真实样本模型运行 `model_scan.py` 不崩溃且退出码为 `0`；<br/>3. `check_hooks.sh --strict` 与 `pr_baseline.sh` 通过。 |
| **第 2 批：`dalton-earnings-deep` 与 `dalton-full-coverage` 切换接入** | 1. 按第 8.3 节更新 `dalton-earnings-deep` 与 `dalton-full-coverage` 技能文档及两条工作流，改用 `dalton-model` 建模指引与 `model_scan.py` 提示性扫描；<br/>2. 更新 `tests/unit/dalton/test_workflows_js.py` 与 Everflow 快照。 | 1. 工作流单测与核心态/Everflow 态全文装配检查通过；<br/>2. `pr_baseline.sh` 通过。 |

---

## 11. 待用户决定的问题

无。以下两项决定按用户 2026-11-25 指示按 lead 推荐执行：

1. 技能目录名、注册名与斜杠命令统一为 `dalton-model`（`/dalton-model`）。
2. 工作区没有用户模型、需要从零新建时，默认参考第 4 章样式常量，并用 `model_scan.py --recalc` 做提示性扫描复核。
