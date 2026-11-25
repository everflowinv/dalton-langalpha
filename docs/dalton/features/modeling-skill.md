# 公司财务建模技能设计方案（`dalton-model`）

本文定义 Dalton 公司与行业财务建模技能（`skill`，供智能体按需加载的专业执行规程与配套脚本）的边界、目录结构、建模与判断规则、单元格格式规范、专用检查脚本、一致预期对账流程、版本管理、注册门控及分批落地方案。方案同时兼容 **Everflow 生产形态**（`DALTON_PROFILE=everflow`）与 **独立核心安装形态**（`DALTON_PROFILE=core`，详见 `docs/dalton/features/standalone-core.md`）。

---

## 1. 目标、技能边界与触发路由

### 1.1 技能名称与定位

- **技能目录与注册名**：`plugins/dalton/skills/dalton-model/`（注册名 `dalton-model`，斜杠命令 `/dalton-model`）。
- **核心定位**：作为 Dalton 建立与维护 Excel（电子表格工作簿，扩展名 `.xlsx`）财务预测模型的统一入口。当用户提供自有模型或工作区已有模型时，直接在既有模型基础上滚动维护并沿用其原生格式与公式链；当工作区尚无模型时，按本技能默认格式规范与预测规则从零搭建模型。

### 1.2 四类触发场景

1. **在用户既有模型上滚动维护**：用户上传单公司模型或行业多公司模型（或工作区 `model/`、`sources/` 目录已有底稿），要求延伸预测期（例如增加下一财年逐季预测与更远年度预测）、新增覆盖公司子模块、调整分部口径或修订核心驱动假设。
2. **财报披露后更新实际值**：公司披露最新季度或年度业绩后，将最新历史实际数填入现有模型，重算现金滚存、利息、股份回购与稀释股本，校准远期驱动假设并输出新旧预测变动归因。
3. **从零新建财务模型**：单家公司或行业工作区尚无模型，用户要求建立覆盖业务驱动、收入分拆、三张财务报表、估值与汇总表的动态财务预测模型。
4. **模型合理性与勾稽检查**：用户要求核查现有模型预测是否偏离市场一致预期（`consensus`）、排查某项收入或利润偏高/偏低的原因，或检查工作簿是否存在硬编码覆盖公式、季度加总年残留假设数、分部脱节或格式不一致问题。

### 1.3 与现有技能的分工与协作关系

| 相关技能 | 所在路径 | 职责边界与协作方式 |
|---|---|---|
| `dalton-conventions` | `plugins/dalton/skills/dalton-conventions/` | 投研方法论总纲（含 `references/model-standards.md` 规定的期间跨度下限、一手财报来源台账、五层数据分类、空值留空规则及 `scripts/xlsx_repair.py` 损坏命名区域修复脚本）。`dalton-model` 开头固定先读该技能并继承上述通用标准，在此基础上补齐具体的模型构建规则、格式常量与专属静态检查。 |
| `dalton-financial-lenses` | `plugins/dalton/skills/dalton-financial-lenses/` | 提供统一报告期、汇率与市值锚点下的净现金双口径、隐含资产、股东回报三层次指标（现金分红率 R1 / 总现金回报率 R2 / 扣除股权稀释后的净回报率 R3）及盈利质量确定性计算脚本。`dalton-model` 在构建净现金利息滚存、回购与稀释股本桥接、估值净现金调整时直接复用其口径。 |
| `dalton-knowledge` | `plugins/dalton/skills/dalton-knowledge/` | 管理工作区结构化知识文档。`dalton-model` 在立项前读取 `thesis.md`（核心论点）、`debate.md`（多空焦点）、`drivers.md`（业务驱动）与 `data/models.json`（模型版本台账），交付新版本模型后调用 `knowledge_update(doc="models", ...)` 登记路径与核心输出单元格。 |
| `dalton-earnings-deep` | `plugins/dalton/skills/dalton-earnings-deep/`（及 `workflows/earnings-deep-update/`） | 财报后八节深度点评与模型更新工作流。工作区已有用户模型或 Dalton 模型时，其支线 B 转用 `dalton-model` 执行季报更新与 `style_audit.py --strict` 校验（保留原有的映射表 `mapping.md`、变更日志与盈利变动拆解 `model-delta.md` 交付物）。 |
| `dalton-full-coverage` | `plugins/dalton/skills/dalton-full-coverage/`（及 `workflows/full-coverage/`） | 买方完整覆盖工作流。工作区已有用户模型时，阶段 2 建模转用 `dalton-model` 在原模型基础上滚动补齐期间、驱动与预期差对比表；工作区尚无模型时，按 15 页规格新建模型并统一采用 `dalton-model` 的预测规则、样式库与 `style_audit.py --strict` 验收。 |
| 上游 `xlsx` | `plugins/langalpha_deliverables/skills/xlsx/` | 底层电子表格操作组件。`dalton-model` 复用其 `scripts/recalc.py`（公式重算与缓存值回写）、`scripts/insert.py`（插入或删除行列并自动平移公式、命名区域与图表引用）、`scripts/render.py`（导出图片供版面目视检查）和 `scripts/profile.py`（原始数据表结构画像）；样式写入与合规审计全部使用 `dalton-model` 自带的 `model_style.py` 与 `style_audit.py`。 |
| 上游 `model-update` / `3-statements` / `dcf-model` / `check-model` | `plugins/langalpha_research/skills/` | 保留在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 作为底层通用参考模块（供三表勾稽公式、自由现金流折现估值 `DCF`〔Discounted Cash Flow〕及只读审查报告结构参考）；凡 Dalton 工作区内的公司与行业建模任务，统一路由至 `dalton-model` 执行。 |

---

## 2. 目录结构与各文件提纲

技能目录位于 `plugins/dalton/skills/dalton-model/`，包含 1 份主入口文件、4 份按需读取的细则参考文档（`references/`）与 2 个确定性 Python 脚本（`scripts/`）：

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
    └── style_audit.py
```

### 2.1 `SKILL.md`（主流程与路由入口）

- **YAML（配置文件标记格式）头部元数据**：`name: dalton-model`；中文 `description` 写明支持在用户现有 Excel 模型上滚动维护、财报后更新季度实际值、新建单公司或行业多公司财务预测模型及合理性检查，遵循基金模型格式规范与全公式链纪律。
- **固定前置声明**：首段固定写明 `Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.`，以及知识层读取顺序（公司工作区读 `agent.md` → `thesis.md` → `debate.md` → `drivers.md` → `data/models.json`；行业工作区先读 `sector.md`）。
- **第 1 节：模式判定与前置准备**：检查工作区 `model/`、`data/models.json` 与用户上传附件（`sources/`），分流至「存量模型滚动维护」「新建模型」或「只读合理性/勾稽检查」；当 `openpyxl`（读写 `.xlsx` 文件的 Python 库）因损坏命名区域报错时，先调用 `.agents/skills/dalton-conventions/scripts/xlsx_repair.py` 生成修复副本。
- **第 2 节：八步标准执行闭环**：
  1. 计算原模型 `SHA-256`（安全哈希摘要校验值）并在 `model/` 下确定新版本目标路径；
  2. 运行 `scripts/model_style.py profile` 提取存量模型格式画像（新建模型时直接加载默认样式配置）；
  3. 输出数据映射与结构调整清单（期间扩展范围、新增/调整分部、外部插件公式替换清单）；
  4. 在当次任务目录 `research/<YYYY-MM-DD>-model/` 下保存可重复运行的 Python 构建/更新脚本并生成工作簿；
  5. 调用 `.agents/skills/xlsx/scripts/recalc.py` 完成全表公式重算，直至零公式错误；
  6. 调用 `.agents/skills/dalton-model/scripts/style_audit.py --strict`（存量维护必加 `--baseline <原模型路径>`）完成格式、公式纪律与勾稽门禁检查；
  7. 调用 `.agents/skills/xlsx/scripts/render.py` 抽查核心工作表图片，核对列宽、对齐与数字显示完整；
  8. 调用 `knowledge_update(doc="models", ...)` 登记新模型版本，向用户交付模型路径、关键财务预测汇总、与一致预期差异拆解及原表硬编码修复清单（如有）。
- **第 3 节：细则文档按需加载表**：按当前任务类型指引读取 `references/` 下对应的细则文件。

### 2.2 `references/format-spec.md`（格式规范与存量样式提取细则）

- 完整格式规范表：字体与字号、第 1 行期间表头、历史实际值、同表公式与小计/合计行、跨表链接、预测驱动假设与历史估算值、衍生比率、A 列三色分节条、最右侧红字来源说明列、目标价突出样式、7 类标准数字格式串、版面列序（年度在左、`CAGR`〔Compound Annual Growth Rate，复合年增长率〕居中、季度在右）、早期列大纲折叠与近季冻结窗格。
- 存量模型样式读取方法：如何识别 Excel 主题色（如 `theme=1` 黑色文本、`theme=6` 历史估算灰底）、如何区分历史列/季度加总年列/纯年度预测列，以及存量模型样式优先于默认样式的合并规则。
- `openpyxl` 读写存量工作簿的操作守则：双重加载（公式态与 `data_only=True` 缓存值态分开读取，只在公式态工作簿上保存）、嵌入式图片（如 `WMF`〔Windows Metafile，旧版矢量图格式〕）与条件格式扩展节点的检测及 XML（Extensible Markup Language，可扩展标记语言）级补丁策略。

### 2.3 `references/forecast-rules.md`（预测规范与业务判断细则）

- 逐节落地预测期跨度、季度加总年反算规则、历史模型结构继承与分部改口径双轨对账、经营指标（`KPI`〔Key Performance Indicator，关键经营指标〕）与收入成本公式挂钩、关键驱动识别与单拆、分部优先预测原则、成本费用拆分准则、净现金/有息债利息滚存与稀释股本动态桥接、资产负债表双模式（常规精简滚存 vs 资产负债表驱动型完整预测）、现金流量表五项规则、预测金额全公式纪律、汇总表必备行项与全链接要求。
- 每一节配套列出需要分析师判断的事项与量化/定性判断依据。

### 2.4 `references/rollforward.md`（存量模型滚动维护操作手册）

- 原件只读保护与单公司/行业工作区版本命名规则。
- 期间轴扩展标准动作：使用 `.agents/skills/xlsx/scripts/insert.py columns` 在年度块与季度块内部安全插入新列（自动重指全书跨表公式、命名区域与求和区间），调用 `model_style.py` 复制相邻同类列样式，将已披露季度从预测公式切换为蓝色历史实际值并补齐一手财报出处。
- 配套收尾动作：顺延 `CAGR` 列起止年份公式、后移冻结窗格至最新实际季度附近、将滑出近期窗口的早年列归入折叠分组、把 `Capital IQ`（标普全球旗下的金融数据 Excel 插件，公式前缀 `_xll.ciqfunctions`）等沙箱内无法重算的外部插件断链替换为可核验静态输入加标准公式、更新或新增 `README & Changelog` 与 `Checks` 工作表。

### 2.5 `references/sanity-check.md`（合理性检查与一致预期对账细则）

- 市场一致预期与卖方预测的获取路径：使用 `<!-- DALTON_CAP:researchdata_bridge -->...<!-- DALTON_CAP_ELSE -->...<!-- /DALTON_CAP -->` 条件标记分别写清 Everflow 生产环境与独立核心安装的取数工具、快照保存路径 `data/consensus/<YYYY-MM-DD_HHMM>.json` 及口径对齐检查单。
- 自上而下差异拆解法（收入规模差、分部结构与利润率差、净利息差、有效税率差、回购股本缩减效应）。
- 坚持独立观点（`contrarian view`，与市场一致预期不同的独立判断）的三项成立条件，以及模型内判断理由（`rationale`）在不同工作簿结构下的具体写入表位。
- 五项刚性内部勾稽与合理性区间检查清单。

### 2.6 `scripts/model_style.py`（样式常量、存量画像提取与写入辅助库）

- **模块常量与样式构造器**：定义全套十六进制颜色、字号、边框、数字格式常量；提供每次返回全新不可变样式对象的构造函数（根据单元格文本是否含中文字符自动选用 `等线` 8pt 或 `Arial` 8pt）。
- **存量格式画像器（`profile_workbook_style` 与 CLI 子命令 `profile`）**：扫描给定 `.xlsx` 文件，输出包含工作表分类、期间列映射、字体、色值、数字格式与来源记录方式的 `StyleProfile` JSON（JavaScript Object Notation，结构化数据交换格式）对象。
- **语义化单元格写入与扩列工具**：提供 `write_header_row`、`write_actual`、`write_formula`、`write_cross_link`、`write_assumption`、`write_derived_ratio`、`write_section_bar`、`write_source_note`、`write_target_price`、`clone_period_column_style`、`apply_sheet_view_defaults`，供建模脚本直接导入调用。

### 2.7 `scripts/style_audit.py`（格式、公式纪律与勾稽检查脚本）

- 独立命令行工具兼可导入模块（CLI〔Command-Line Interface，命令行接口〕支持 `<file.xlsx> [--strict] [--sheet NAME] [--baseline <orig.xlsx>] [--profile <style_profile.json>]`）。
- 覆盖基础重算与断链安全、格式规范、公式纪律（含季度加总年专项检查、相对假设公式识别、对比原底稿的公式保留率）和四大业务勾稽检查，输出结构化 JSON 报告并按 `--strict` 控制进程退出码。

---

## 3. 财务建模业务规则与分析师判断依据

本节将建模指南草案第 2–10 节转化为面向智能体的逐条执行规则，并逐节标明需要分析师判断的环节及判断依据。写入技能 `references/` 时保持直接陈述句，便于模型逐条核对执行。

### 3.1 预测期与季度加总年规则（对应草案第 2 节）

- **执行规则**：
  1. **逐季预测跨度**：本财年尚未披露的剩余季度与下一完整财年的四个季度全部做逐季预测（例如当前最新实际季为 `3Q26`，逐季预测须覆盖 `4Q26E` 及 `1Q27E`–`4Q27E`）。历史年度（至少自 `FY2018` 或上市最早完整财年起）与历史单季（至少自 `2024Q1` 起）完整保留，只向后扩展期间列。
  2. **年度预测跨度**：年度列至少覆盖本财年与下两个完整财年（如 `2026E`、`2027E`、`2028E`）。
  3. **季度加总年计算规则**：当某个预测财年（如 `2026E`、`2027E`）在同表右侧已具备对应的四个季度列时：
     - 所有流量型金额行（收入、成本、各项费用、分部与集团营业利润、利息、所得税、净利润、经营现金流、资本开支、自由现金流、分红与回购）在该年度列统一写四季度求和公式 `=SUM(Q1:Q4)`；期末现金、期末债务等时点余额行引用对应 `Q4` 单元格；加权稀释股本按公司既有口径取四季均值 `=AVERAGE(Q1:Q4)` 或 `Q4` 股本；
     - 该年度列下的所有比率行（同比增速 `YoY`〔Year-over-Year〕、毛利率、各项费用率、营业利润率 `OPM`〔Operating Profit Margin〕、净利率、货币化率 `Take rate`〔平台收入占交易额比例〕、分部收入占比）统一用**该年度列自身的金额单元格反算**（例如 `=S9/R9-1`、`=S19/S9`），设为黑色斜体无底色；该年度列保持零驱动假设输入；
     - 仅在真正驱动计算的预测季度列（`4Q26E`、`1Q27E`–`4Q27E`）以及尚未展开季度的纯年度预测列（如 `2028E`）放置驱动假设单元格。
- **需要判断的事项**：是否将年度预测延伸至下三个财年及更远期（如 5–10 年显式预测期）。
- **判断依据**（满足任一条件即向远期延伸至利润率与增速进入稳态的年份）：
  1. 公司当前处于亏损、盈亏平衡切换期或核心新业务减亏爬坡期，近两年利润尚无法代表稳态盈利能力；
  2. 市场主流定价锚点采用 3 年后远期市盈率（`P/E`〔Price-to-Earnings Ratio〕）或多年期自由现金流折现估值（`DCF`）；
  3. 公司正处于跨度超过 3 年的大型资本开支周期（如新建算力基础设施、晶圆厂或海外仓配网络），折旧高峰与产能释放发生在第 3 年之后。

### 3.2 起点继承与分部改口径处理（对应草案第 3 节）

- **执行规则**：
  1. **沿用存量模型结构**：当工作区已有模型或用户提供了历史模型时，先完整梳理原工作簿的页签依赖链（如行业数据页 `Peers` → 经营指标页 `Key Metrics` → 子业务页 `Subs` → 主财务表 `Model` → 估值页 `Valuation` / 分部加总估值 `SOTP`〔Sum-of-the-Parts〕）及已定义的命名区域（如汇率 `hkd`、`usd`），在原预测框架上扩展期间、修补断链和调整分部，完整保留工作簿内全部历史专题对比页。
  2. **分部改口径双轨保留与对账**：当公司在历史期或最新财报调整了分部披露口径（重组业务线、合并分部或改变分部利润指标定义）时：
     - 在分部区域同时保留「旧披露口径块」与「新披露口径块」：旧口径块保留历史实际数至最后一次披露该口径的季度，后续期间留空；新口径块从公司提供重述数据的最早期间起填入历史数并向后展开预测；
     - 在两个口径块下方建立「分部口径对账调节表（`Reconciliation`）」，能对齐集团总收入或总营业利润的期间写出差额检验公式（`=新口径分部合计 + 未分配总部项 + 分部间抵销 - 集团合计`）；因公司未重述早期季度或变更了内部结算规则而无法完全对齐的期间，保留差额公式并在最右侧备注列用红字写明不可比原因及对应财报附注页码。
- **需要判断的事项**：新增业务或口径变化时，是在原表局部增补子块，还是向用户提议重构主表。
- **判断依据**：若集团合并利润表结构不变，仅新增子业务或调整分部归属，统一在 `Subs` 页或主表下方增设「业务子模型块」并将结果汇总回主表；仅当公司发生重大资产剥离并表变更、更换记账本位币或变更收入确认准则导致主表核心行项整体失效时，先向用户提交新旧行项映射方案确认后再重构。

### 3.3 收入表与成本费用建模规则（对应草案第 4 节）

- **执行规则**：
  1. **经营指标（`KPI`）与财务预测公式挂钩**：将公司披露的核心经营指标——用户数、每用户平均收入（`ARPU`〔Average Revenue Per User〕）、商品交易总额（`GMV`〔Gross Merchandise Volume〕）、货币化率（`Take rate`）、订单量、平均客单价（`ASP`〔Average Selling Price〕）、客户资产规模（`AUM`〔Assets Under Management〕）等——置于 `Key Metrics` 页或主表上方经营区，收入与直接变动成本行用公式直接关联这些指标（例如 `佣金及广告收入 = GMV × Take rate`、`履约成本 = 订单量 × 单均履约成本`）。披露链条存在缺环时，参考卖方通行建模公式补建中间桥接行。
  2. **关键驱动独立拆分预测**：对股价和盈利弹性影响大的核心驱动因素单独拆行或单建业务子表预测。当公司历史财报未逐期披露某项关键驱动（如某新业务单季 `GMV` 或独立损益）时，综合公司电话会定性定量线索、卖方报告拆算逻辑或第三方跟踪数据回填历史估算值，单元格统一设为驱动假设样式（蓝字 + `FFFFC8` 底色 + 四边细虚线框），并在最右侧备注列用红字标明估算口径与数据来源（如 `<GS 1117`、`<管理层电话会披露占比推算`）。
  3. **分部披露优先预测**：公司披露分部（`Segment`）收入与分部营业利润（或经调整息税及摊销前利润 `EBITA`）时，优先在分部区块逐个分部预测收入、利润率与分部营业利润。当集团营业利润等于各分部营业利润之和（加上总部未分配费用与分部间抵销项）时，集团主表的营业利润直接写汇总公式引用分部区块；集团层面的各项期间费用行仅回填历史披露实际数，预测期若无集团级独立驱动则留空或由分部费用加总得出，避免集团层与分部层各自独立设费用率导致两套营业利润脱节。
  4. **成本与费用按可靠依据拆分**：存量模型已有成本费用拆分的（例如将股份支付费用 `SBC`〔Share-Based Compensation〕、折旧与摊销 `D&A`〔Depreciation and Amortization〕、人工、仓储物流与其他经营费用分开），直接沿用原拆分维度；新建模型且法定财报附注或现金流量表持续披露明细项的，拆出独立行预测以衔接公认会计准则（`GAAP`〔Generally Accepted Accounting Principles〕）与非公认会计准则（`Non-GAAP`）调节项；缺乏披露数据支撑的科目保持合并行按占收入比例预测。
- **需要判断的事项**：（1）哪些业务或变量属于必须单拆的「关键驱动」；（2）某项成本费用是否有足够依据拆分。
- **判断依据**：
  - **关键驱动判定标准**（符合任一条即单独拆分）：
    1. 已列入工作区知识层 `thesis.md` 的核心论点支柱或 `debate.md` 的多空核心分歧变量；
    2. 最近一个财报季 3 份及以上卖方深度或业绩点评报告将其作为核心估值驱动或盈利拐点主线（例如跨境业务减亏幅度、外卖或社区团购投入节奏、核心广告货币化率提升）；
    3. 该业务预计贡献未来两个财年集团增量收入或增量营业利润绝对值的 **20% 及以上**，或该业务稳态营业利润率与公司现有主业相差 **10 个百分点及以上**。
  - **成本费用拆分标准**：法定定期报告（`10-K`/`10-Q`/`20-F`〔美国证券交易委员会年度、季度与外国私人发行人年报表格〕或港股/A 股年报中报）连续披露该子项历史金额，且该子项的变化驱动与主营收入规模不同步（如重资本开支期的折旧跟随固定资产投入、员工期权费用跟随授予节奏、履约成本跟随包裹单量），即予单拆；其余归入合并费用率预测。

### 3.4 利息收支与稀释股本规则（对应草案第 5 节）

- **执行规则**：
  1. **利息收入与利息支出**：
     - **金额重大时**：与资产负债表或现金流量表的现金及短期投资余额（含定期存款与短期理财）、有息债务余额建立公式关联。为保持工作簿零循环引用，默认采用**期初余额 × 假设年化收益率/利率**计算（单季预测列乘 `0.25`；原模型已采用期初期末平均余额且开启迭代计算的除外）。建表时先在历史期间用公式反算历史隐含年化利率（`=当期利息 / 期初对应余额`，单季乘 `4`），核验预测期设定的利率假设处在历史合理带并反映当前市场基准利率变化。债务结构复杂且影响重大的公司，单独建立逐笔债务明细表（列明每笔债券/可转债/贷款的本金、票息率、到期日与转股条件）逐笔汇总利息支出。
     - **金额不重大时**：按最近历史水平顺延或占收入小比例设定简单假设。
  2. **稀释股本（`Diluted Shares` / `ADS`〔American Depositary Share，美国存托股份〕折算股数）**：
     - 预测期稀释股本采用动态滚存公式：`本期稀释股本 = 上期稀释股本 + 员工股权激励自然增发股数 − 本期回购注销股数`（发行美国存托凭证 `ADR`〔American Depositary Receipt〕的公司同步用公式绑定普通股与 `ADS` 换算比例）。
     - 股权激励自然增发股数参考历史年度剔除回购后的自然稀释率，或按 `=预计 SBC 费用 / 当前基准股价 × 归属系数` 估算。
     - 回购减少股数结合公司剩余回购授权额度、管理层公开股东回报指引及近 4–8 个季度实际回购执行节奏设定：`本期回购股数 = 本期回购现金支出（折算为交易币种） / 假设回购均价`（默认取当前基准股价），且**本期回购现金支出用公式直接勾稽至现金流量表的股份回购流出项**，使现金减少（降低后续期初生息现金）与股本减少（增厚每股收益 `EPS`〔Earnings Per Share〕/ `EPADS`〔每份 ADS 收益〕）同步联动。
- **需要判断的事项**：（1）「利息金额重大」与「债务影响重大」的界定门槛；（2）未来回购节奏的取值。
- **判断依据**：
  - **利息与债务重大性门槛**（满足任一条即按重大处理，建立余额联动公式）：
    1. 利息收入、利息支出或净利息占税前利润（`EBT`〔Earnings Before Taxes〕）或 `Non-GAAP` 净利润绝对值的 **5% 及以上**；
    2. 狭义净现金或总有息债务占当前总市值的 **10% 及以上**；
    3. 总有息债务与息税折旧摊销前利润（`EBITDA`〔Earnings Before Interest, Taxes, Depreciation and Amortization〕）之比达到 **2.0 倍及以上**，或未来 24 个月内到期债务占总有息债务 **20% 及以上**（此情形同步拆建逐笔债务明细表）。
  - **回购节奏取值依据**：年度回购金额取「董事会剩余授权额度」「管理层已宣布年度股东回报目标」与「近 4 季实际回购年化节奏（受预计自由现金流与净现金底垫约束）」三者中的审慎可执行水平，回购额限定在剩余授权与预计自由现金流可支撑范围内。

### 3.5 资产负债表规则（对应草案第 6 节）

- **执行规则**：
  1. **常规行业公司（最小必建集）**：至少通过现金流量表动态滚存推出**期末现金及现金等价物（含短期投资/定存）余额**；触发 3.4 节债务重大门槛时同步滚存**短期与长期有息债务余额**。若原用户模型仅维护核心现金与债务滚存行，保持该精简结构；若新建完整三表模型或原模型包含配平资产负债表，则完整滚存营运资本科目、固定资产净值与留存收益，并在 `Checks` 页逐期校验 `总资产 = 总负债 + 股东权益`。
  2. **资产负债表驱动型公司（完整资产负债表必建）**：银行、券商、保险、金融科技/信贷、资产管理、房地产投资信托（`REITs`〔Real Estate Investment Trusts〕）与重资产租赁公司，完整预测资产负债表各核心科目，并查阅最新 `10-Q`/`10-K`/`20-F` 或法定财报附注，把资产负债表科目（生息客户资金、融出资金、贷款余额、投资资产、计息负债、客户存款/保证金）与利润表科目（利息收入、经纪佣金、利息支出、信用减值拨备）通过平均余额与年化收益率/费率/拨备率建立逐项公式关系。
- **需要判断的事项**：目标公司适用常规精简滚存还是完整资产负债表驱动建模。
- **判断依据**：凡净利息收入、金融资产投资收益、风险保费或基于表内资金规模收取的融资融券收入占营业总收入 **20% 及以上**，或业务扩张直接受监管净资本/杠杆率指标约束的公司，一律归为资产负债表驱动型。

### 3.6 现金流量表规则（对应草案第 7 节）

- **执行规则**：
  1. **覆盖深度**：所有模型至少预测到经营活动现金流（`OCF`〔Operating Cash Flow〕）与自由现金流（`FCF = OCF − CapEx`〔Free Cash Flow，扣除资本开支 `CapEx` 后的自由现金流〕），并向下衔接分红与股份回购现金流出，以推出期末现金余额。
  2. **营运资本变动（`Working Capital`）**：先统计过去 3–5 个财年与近 8 个季度的净营运资本变动（`ΔNWC`〔Net Working Capital〕）占收入或收入增量的比例。历史呈现稳定方向与区间规律时（如平台预收货款/递延收入随交易规模扩张持续带来净现金流入，或自营零售存货应付周转稳定），按占收入增量比例或周转天数（`DSO`/`DIO`/`DPO`〔应收/存货/应付周转天数〕）建立预测公式；若历史季度或年度间正负大幅摆动、无稳定趋势，预测期营运资本变动可设为中性 `0`（或留空不单预测），并在右侧备注列注明「历史营运资本波动大，预测期按中性假设处理」。
  3. **折旧与摊销**：当 `CapEx` 对业务重要时，将折旧与 `CapEx`（或期初固定资产净值加当期新增 `CapEx`）建立公式联动，使新增资本开支按合理折旧年限转化为后续期间折旧费用；`CapEx` 占比很小时，折旧按占收入比例延续历史水平。无形资产摊销视历史及预期并购规模而定：有重大并购无形资产的公司，按财报附注披露的未来 5 年预计摊销时间表填入；无重大并购的公司按历史小额延续或自然递减。
  4. **资本开支（`CapEx`）**：每本模型均预测 `CapEx`。算力基础设施、云计算、半导体制造、自建仓储物流等重资本业务按投向明细或管理层指引单独拆解预测；轻资产业务按 `CapEx / 收入` 的历史稳态比例预测。
  5. **分红与回购**：结合公司历史派息率、股息政策及最新股东回报计划建立预测公式；回购金额重大时与 3.4 节股本表的回购注销股数双向勾稽。
- **需要判断的事项**：营运资本规律性、`CapEx` 重要性、并购摊销重大性与回购重大性的判定。
- **判断依据**：
  - **营运资本规律性**：近 3–5 个完整财年 `ΔNWC / 收入` 符号保持一致且年度波动标准差小于收入的 **3%** 时判为有明确规律；正负交替无趋势时按中性处理；
  - **`CapEx` 重要性**：`CapEx / 收入 ≥ 5%`、`CapEx / OCF ≥ 20%`，或公司正开启新一轮基础设施扩产周期时判为重要，须拆细并与折旧挂钩；
  - **并购摊销重大性**：并购无形资产摊销占 `GAAP` 营业利润绝对值 **5% 及以上** 时，查阅年报附注摊销时间表逐年建模；
  - **回购重大性**：预计年度回购金额占当前市值 **1% 及以上** 或占自由现金流 **15% 及以上** 时，强制勾稽回稀释股本表。

### 3.7 公式纪律与原表硬编码修复（对应草案第 8 节）

- **执行规则**：
  1. **预测期金额全公式化**：预测期内的所有金额单元格（收入、成本、费用、分部利润、集团利润、税、净利润、每股收益、现金流各项、期末现金、期末股本）全部写入 Excel 动态公式。调整模型预测时，只修改标记为驱动假设样式的输入单元格（增速、货币化率、利润率、费用率、税率、隐含利率、资本开支率、回购金额）。
  2. **相对上期的驱动假设公式规则**：驱动假设单元格既可填入常数，也可写成相对上一期或去年同期单元格加减变动的公式（例如 `=R138+0.3%`、`=P272+0.05%`、`=P9-8%`、`=BE157`）。此类单元格统一套用驱动假设格式（蓝字 `0000FF` + 浅米黄底 `FFFFC8` + 四边 `hair` 虚线框；百分比设为斜体），属于合规驱动假设。
  3. **原模型预测硬编码修复与清单披露**：在存量模型上维护时，保持原模型所有既有预测公式 100% 不退化为常数；若扫描发现原模型预测期金额行里存在历史遗留的手工常数硬编码，将其改写为与同行逻辑一致的公式，并在交付说明中单列「原模型硬编码修复表」（注明工作表、单元格坐标、指标名、原常数值、改后公式及数值变动）。
- **需要判断的事项**：某一行在预测期属于「可放常数/相对增量的驱动假设行」还是「必须由公式算出的金额结果行」。
- **判断依据**：比率、费率、单价、税率、利率、周转天数及公司明确按绝对额规划的驱动参数（如年度回购预算额、固定总部开支基数）属于驱动假设行；凡存在上层业务量价拆解的收入成本行、所有财务报表主表金额行、小计与合计行、每股指标行以及季度加总年的全部行，一律属于计算结果行，只写公式。

### 3.8 汇总表规则（对应草案第 9 节）

- **执行规则**：
  1. 每本工作簿均配备一张汇总表（单公司模型置于首页 `Summary` 或 `Valuation` 页顶部总览区；多公司行业模型置于 `Peers` / `Global Comps` 或首页 `README & Changelog` 的公司核心预测总览表，各公司另配 `Valuation - <TICKER>` 页）。
  2. 汇总表同时展示关键指标的**年度序列（最近 1–2 个历史财年 + 全部预测财年）与季度序列（最近已披露季度 + 未来逐季预测）**。
  3. **必备指标**：**营业总收入（及同比增速）**、**稀释每股收益 `EPS` / `EPADS`（公司核心定价口径及同比增速）**。
  4. **全动态引用**：汇总表中的全部历史与预测数字均通过跨表公式（绿字 `008000`）或同表公式引用底层计算页对应单元格，保持单一数据源。
- **需要判断的事项**：除收入与 `EPS` 外，汇总表纳入哪些补充核心指标。
- **判断依据**：根据行业定价核心与卖方重点跟踪口径选取 3–6 项——例如电商平台纳入核心 `GMV`、货币化率、核心分部 `EBITA`、集团 `Non-GAAP` 营业利润及利润率、自由现金流与狭义净现金；在线券商纳入净新增入金客户数、期末客户总资产 `AUM`、交易量与营业利润率；算力与硬件公司纳入毛利率、研发费用率、资本开支与自由现金流。

### 3.9 合理性检查总则（对应草案第 10 节）

- **执行规则**：完成计算后，将模型未来 2 个季度及未来 2–3 个财年的核心预测与市场一致预期、卖方报告对比；偏离超过阈值时逐层拆解到具体分部与驱动假设，排查建模疏漏并评估是否有充分证据坚持独立观点；确认合理后在模型内写明理由（具体取数渠道、拆解公式与落表位置见第 6 章），同时逐项通过五项内部勾稽核查（分部营业利润加总等于集团、季度之和等于年度、利息隐含利率处在历史与市场合理区间、股本变动与回购及 `SBC` 一致、完整资产负债表配平）。

---

## 4. 格式规范、样式常量与存量模型格式提取

### 4.1 默认样式常量表（`scripts/model_style.py`）

当工作区尚无用户模型（新建模型），或用户模型中未包含某类新增元素时，统一使用以下从两本用户实测模型（中国电商 40 页工作簿、在线券商 7 页工作簿）提炼的标准常量：

| 样式元素 | 常量名（`model_style.py`） | 字体与字形 | 字色 `RGB`（红绿蓝十六进制色值） | 填充底色 `RGB` | 边框与对齐 | 数字格式 / 版面规则 |
|---|---|---|---|---|---|---|
| 基础字体 | `FONT_EN = "Arial"`<br/>`FONT_ZH = "等线"`<br/>`FONT_SIZE = 8` | 英文与数字用 `Arial` 8pt；含中文字符用 `等线` 8pt | 按元素类型设定 | — | 文本左对齐，数值与期间列头右对齐 | 全表数据区统一 8pt |
| 第 1 行期间表头 | `HEADER_FILL = "3366FF"`<br/>`HEADER_FONT = "FFFFFF"` | 8pt 加粗 | 白字 `FFFFFF` | 亮蓝底 `3366FF` | 无边框；期间列右对齐，`A1` 左对齐 | `A1` 写单位，如 `(RMB MM)`、`(US$ MM)`、`(HK$'000)` |
| 历史实际值 | `ACTUAL_FONT = "0000FF"` | 8pt 正体 | 蓝字 `0000FF` | 无底色 | 无边框 | 按行属性套金额/百分比/倍数格式 |
| 同表计算公式 | `FORMULA_FONT = "000000"` | 8pt 正体 | 黑字 `000000` | 无底色 | 无边框 | 显式指定金额、百分比或倍数数字格式 |
| 小计 / 合计行 | `TOTAL_BOLD = True` | 8pt **加粗**（标签与数值均加粗） | 同表黑字 / 跨表绿字 / 历史蓝字 | 无底色 | **四边留空（零边框）** | 适用于总收入、毛利、营业利润、净利润等汇总行 |
| 跨表链接公式 | `LINK_FONT = "008000"` | 8pt 正体（合计行加粗） | 绿字 `008000` | 无底色 | 无边框 | 公式引用本工作簿其他工作表单元格 |
| 预测驱动假设与历史估算值 | `ASSUMP_FONT = "0000FF"`<br/>`ASSUMP_FILL = "FFFFC8"`<br/>`ASSUMP_BORDER = "hair"` | 8pt；**百分比类假设用斜体**，金额/数量类假设用正体 | 蓝字 `0000FF` | 浅米黄底 `FFFFC8` | **四边 `hair` 细虚线框** | 覆盖常数假设、相对上期增减假设公式及历史未披露估算值 |
| 衍生比率公式 | `RATIO_ITALIC = True` | 8pt **斜体** | 黑字 `000000` | 无底色 | 无边框 | 增速、占比、利润率、反算货币化率，默认 `0.0%`（精细费率 `0.00%`） |
| 报表级分节条 | `SEC_REPORT_FILL = "99CCFF"` | 8pt 加粗 | 黑字 `000000` | 浅蓝底 `99CCFF` | **仅涂 A 列单个单元格**（B 列起保持独立单元格且无底色） | 用于 `Income statement`、`Balance sheet`、`Cash flow statement`、估值分组栏 |
| 分部 / Non-GAAP 分节条 | `SEC_SEGMENT_FILL = "993300"`<br/>`SEC_SEGMENT_FONT = "FFFFFF"` | 8pt 加粗 | 白字 `FFFFFF` | 棕红底 `993300` | **仅涂 A 列单个单元格** | 用于 `Non-GAAP measures`、`Segment Reporting`、主要控股子公司块 |
| 业务子模型分节条 | `SEC_SUBMODEL_FILL = "FFCC99"` | 8pt 加粗 | 黑字 `000000` | 浅桃底 `FFCC99` | **仅涂 A 列单个单元格** | 用于具体业务拆解子块（如 `Temu`、`1P Business`）及估值页目标价标题格 |
| 来源说明列 | `SOURCE_NOTE_FONT = "FF0000"` | 8pt 正体（或斜体） | 红字 `FF0000` | 无底色 | 置于最后一个期间列右侧首列 | 文本以 `<` 或 `<<` 开头（如 `<Yipit`、`<<GS 1117`）；保持 0 单元格批注 |
| 目标价结果格 | `TARGET_PRICE_FONT = "C00000"` | 8pt 加粗 | 深红 `C00000` | 无底色（或同行标 `FFCC99`） | 无边框 | 用于 `Valuation` / `SOTP` 页核心每股目标价输出格 |

**标准数字格式常量**：
- `FMT_INT = "_(#,##0_);\\(#,##0\\);_(?\\-?_);@"`（整数金额主力格式：负数括号、零值显示为短横线 `-`）
- `FMT_DEC1 = "_(#,##0.0_);\\(#,##0.0\\);_(?\\-?_);@"`（1 位小数金额）
- `FMT_DEC2 = "_(#,##0.00_);\\(#,##0.00\\);_(?\\-?_);@"`（2 位小数，用于 `EPS`、`EPADS`、股价）
- `FMT_PCT = "0.0%"` / `FMT_PCT2 = "0.00%"`（百分比与精细货币化率）
- `FMT_PP = '#,##0.0" pp"'`（百分点 `pp`〔percentage points〕变动）
- `FMT_MULT = '#,##0.0" x"'`（估值倍数）
- `FMT_YM = 'yy"年"m"月"'`（月度高频跟踪表日期）

**版面与视图默认常量**：
- **列序**：`A–C` 窄层级缩进列（列宽 `1.8–2.3`）+ `D` 主行标签列（列宽 `26–36`） → **年度列在左**（历史年份写纯数字 `2025`，预测年份加后缀 `2026E`） → 窄空隔列（宽 `1.2`） → `CAGR` 列（如 `CAGR 25-28E`，公式 `=(末年/首年)^(1/年数)-1`） → 窄空隔列（宽 `1.2`） → **季度列在右**（历史写 `3Q26` 或 `2026Q3`，预测写 `4Q26E` 或 `2026Q4E`） → **最右侧红字来源说明列**。期间列宽设为 `7.5–9.2`。
- **大纲折叠与冻结窗格**：距今超过 3 年的早期年度列与距今超过 8 个季度的早期季度列设置列分组并默认折叠隐藏（`outlineLevel = 1, hidden = True`）；冻结窗格（`freeze_panes`）定在第 2 或第 3 行、最近 2–4 个已披露季度列处（使打开工作簿即同时看见左侧行标签与最新季度数据）；全表设置 `ws.sheet_view.showGridLines = False` 关闭网格线。
- **多公司工作簿页签组织**：行业总览页（同业可比表 `Global Comps`、潜在市场总规模 `TAM`〔Total Addressable Market〕、同行对标页 `Peers`）在前，之后每家公司前放一张空白分隔页 `<TICKER>>>`，同公司下属页签（`Valuation - XX`、`Key Metrics - XX`、`Model - XX`、`Subs - XX`、`SOTP - XX`）使用同一组标签颜色（`tabColor`）。

### 4.2 读取用户模型格式并沿用的实现方法（`profile_workbook_style`）

用户提供了现有模型时，先运行 `python .agents/skills/dalton-model/scripts/model_style.py profile <原模型.xlsx> --out style_profile.json`，按以下五步自动提取原模型的实际格式契约并沿用：

1. **筛选代表性计算页**：遍历工作簿可见页签，跳过空白分隔页（名称以 `>>` 结尾或非空单元格少于 10 个）、隐藏缓存页（如 `_CIQHiddenCacheSheet`）及纯文字说明页，优先选取名称含 `Model`、`Income`、`IS`、`Key Metrics` 或公式单元格数量最多的前 3 张核心计算页作为采样源。
2. **解析期间表头行与四类列集合**：在每张采样页前 10 行定位期间表头行（第 1 行，或含连续年份 `1990–2100` 及季度标签的无公式行），记录表头底色、字色与字号，并识别期间命名风格（如 `3Q26` 风格 vs `2026Q3` 风格、历史期间是否带 `A` 后缀）：
   - 不带 `E`/`F`/`P` 后缀的年份与季度列归入**历史列集合**；
   - 带 `E`（或 `F`/`P`）后缀的年份与季度列归入**预测列集合**；
   - 在预测年度列中，检查同表季度区是否同时存在该财年的完整四个季度列（`1Q`–`4Q`）：存在则归入**季度加总年列集合**，不存在则归入**纯年度预测列集合**；
   - 最后一列期间列右侧若存在非空字符串单元格，定位为**来源备注列**。
3. **按单元格角色分类抽样样式特征**（同时用 `load_workbook(path, data_only=False)` 读公式结构、用 `data_only=True` 读缓存值类型，扫描采样页前 300 行数据区）：
   - **历史实际值角色**：位于历史列、`data_type == "n"`（数值常数）且无底色填充的单元格 → 统计字体名称、字号、字体颜色（识别 `0000FF`）与金额整数/小数格式串（兼容会计格式 `_(* #,##0_)` 与 `_(#,##0_)`）的众数；
   - **历史估算值角色**：位于历史列、带实心填充色（如 `FFFFC8` 或 Excel 主题色 `theme=6` 浅灰底 `EDEDED`）的数值或公式格 → 记录原模型的历史估算标记样式；
   - **预测驱动假设角色**：位于非加总预测列（预测季度列与纯年度预测列），满足「数值常数」或「带实心填充底色」或「蓝字相对增减公式」的单元格 → 统计填充底色（如 `FFFFC8`）、四边边框线型（如四边 `hair`）、字体颜色及百分比格斜体规则；
   - **同表公式与衍生比率角色**：`data_type == "f"`、经 `openpyxl.formula.Tokenizer` 词法解析不含跨表引用 `!` 且无假设底色的单元格 → 按 `number_format` 是否含 `%` 拆分为金额公式与衍生比率公式，统计字体色（将 `color=None`、`rgb="000000"` 与 Excel 标准文本黑色主题槽 `type="theme", theme=1` 统一识别为合规黑色）与斜体比例；同时扫描行标签含 `Total` / `Gross profit` / `Operating income` / `Net income` / `合计` 的行，记录合计行加粗与上下边框习惯；
   - **跨表链接角色**：`data_type == "f"`、词法解析包含本工作簿其他页签引用（含 `!` 且不含外部工作簿 `[`）且无假设底色的单元格 → 统计跨表链接字色（如 `008000`）；
   - **分节条与来源标注方式**：扫描标签列（A–D 列）无期间数据的标题行，提取填充色与列分布；统计全工作簿批注总数 `comment_count` 与右侧备注列红字单元格总数，判定该模型采用「右侧红字备注列」还是「单元格批注」。
4. **合成有效样式配置（原模型优先、默认表补缺）**：任一角色在原工作簿采样数达到 5 个且众数占比不低于 60% 时，锁定原工作簿实测样式；原工作簿未覆盖的元素（如原模型尚无 `Checks` 页）回退采用 4.1 节默认常量。
5. **扩列时直接克隆相邻同类列**：在存量工作表新增期间列时，优先通过 `insert.py columns --copy-style-from <相邻同类列>` 与 `clone_period_column_style` 整列复制行级样式（新历史季复制紧邻的上一历史季列，新预测季复制紧邻的上一预测季列，新纯年度列复制原最后一列纯年度预测列），保留每一行原有的缩进、字体、小计加粗与特殊小数位数，再按单元格角色微调字体颜色和假设框。

---

## 5. 模型检查脚本设计（`style_audit.py`）

按已定决策，`dalton-model` 自带专用检查脚本 `plugins/dalton/skills/dalton-model/scripts/style_audit.py`，保持上游 `plugins/langalpha_deliverables/skills/xlsx/scripts/audit.py` 零改动。这样既避免触碰上游文件，又彻底解决上游脚本将合规相对假设公式（`=R138+0.3%`）判为 `formula_hardcode` 失败、将蓝色假设公式判为颜色异常、强制每个蓝色输入格写单元格批注，以及将用户原表自带的 `_CIQHiddenCacheSheet`（`veryHidden`）直接判死的问题。

### 5.1 命令行接口与参数

```bash
python .agents/skills/dalton-model/scripts/style_audit.py <model.xlsx> \
  [--strict] \
  [--sheet "<工作表名>"] \
  [--baseline <原模型.xlsx>] \
  [--profile <style_profile.json>]
```

- `--strict`：严格门禁模式。所有 `fail` 级检查项导致脚本返回退出码 `1`，并将核心格式项（外来底色/字号、新增假设格样式残缺、计算公式留在 `General` 格式、汇总表静态常数）从 `warn` 提升为 `fail`。
- `--baseline <原模型.xlsx>`：在用户既有模型上滚动维护时传入原模型路径，做两项增量比对：（1）逐行逐期间检查原模型既有预测公式是否被降级覆写为常数；（2）对原模型未改动历史列中既有的历史习惯（例如部分旧表把历史年度 `SUM` 列标成蓝字）豁免样式告警，将严格样式门禁聚焦于本次新增和修改的区域。
- `--profile <style_profile.json>`：传入 `model_style.py profile` 导出的样式画像；未传时脚本自动在内存对目标工作簿（有 `--baseline` 时对原模型）提取画像并与 4.1 节默认值对齐。

### 5.2 完整检查项清单与严重级别

| 类别 | 检查项代号（`check`） | 默认级别（`--strict` 级别） | 检查内容与判定逻辑 |
|---|---|---|---|
| **基础与重算** | `cached_error` | `fail` (`fail`) | 公式单元格重算后的缓存值为 `#REF!`、`#DIV/0!`、`#VALUE!`、`#NAME?`、`#N/A`、`#NUM!` 或 `#ERROR!`。 |
| **基础与重算** | `external_or_plugin_link` | `fail` (`fail`) | 单元格公式或工作簿/工作表命名区域指向外部工作簿（含 `[Book.xlsx]`），或残留 `_xll.ciqfunctions`、`RTD`、`WEBSERVICE` 等沙箱无法求值的外部插件函数。 |
| **基础与重算** | `unrecalculated` | `fail` (`fail`) | 计算结果为数值的公式单元格缓存值为 `None`（提示必须先运行 `recalc.py`）。 |
| **基础与重算** | `hidden_sheet_referenced` | `warn` (`warn`) | 活动可见页公式引用了隐藏或深度隐藏（`veryHidden`）工作表；未被任何活动公式引用的历史遗留缓存页（如 `_CIQHiddenCacheSheet`）仅记 `info`。 |
| **格式规范** | `foreign_template_style` | `warn` (`fail`) | 数据计算页出现外来默认模板填充色（`1F4E79`、`D9E1F2`、`F2F2F2`、`BDD7EE`，除非原模型画像本身使用该色）或非空单元格字号偏离目标字号（如在 8pt 模型中写入 10pt/11pt 数据格）。 |
| **格式规范** | `font_family` | `warn` (`warn`) | 非空单元格字体不在目标字体集合（默认 `Arial`、`等线`，或原模型 `StyleProfile` 提取的主字体集合）内。 |
| **格式规范** | `actual_font_color` | `warn` (`warn`) | 历史期间列中的硬编码实际数值（非表头行、非标注为估算底色的单元格）未使用蓝字 `0000FF`。 |
| **格式规范** | `assumption_cell_style` | `warn` (`fail` 针对新增/修改列) | 非加总预测列中的驱动假设格（含常数假设与相对上期增减假设公式）未完整设置「蓝字 `0000FF` + `FFFFC8` 底色 + 四边 `hair` 虚线框」，或百分比假设（格式含 `%`）未设斜体。 |
| **格式规范** | `formula_font_color` | `warn` (`warn`) | 非假设类的跨表链接公式未使用绿字 `008000`，或同表计算公式误用蓝字/红字（`color=None`、`000000` 与 `theme=1` 均判为合法黑色；`--baseline` 下原表未改动旧列保持原色豁免）。 |
| **格式规范** | `derived_ratio_italic` | `warn` (`warn`) | 无假设底色的衍生比率公式（增速、利润率、占比，`number_format` 含 `%`）未设置斜体。 |
| **格式规范** | `formula_number_format` | `warn` (`fail`) | 计算结果为数值的公式单元格（第 1 行表头除外）仍处于 `General` 未格式化状态。 |
| **格式规范** | `total_row_style` | `warn` (`warn`) | 小计/合计行未加粗，或在无边框风格模型中给合计行加了多余边框。 |
| **格式规范** | `sheet_view_layout` | `info` / `warn` | 计算页未关闭网格线（`showGridLines` 为真，`warn`）、超过 25 行的计算页未设置冻结窗格（`warn`）、多数期间列仍为默认列宽（`warn`）。 |
| **公式纪律** | `forecast_amount_hardcode` | `fail` (`fail`) | 预测期间列（列头带 `E`）的金额计算行（同行已有计算公式，或行标签为收入/毛利/费用/营业利润/净利润/`EPS`/现金流/小计合计）被写入了硬编码数值常数。 |
| **公式纪律** | `formula_regression_vs_baseline` | `fail` (`fail`) | 传入 `--baseline` 时，原模型中原本为公式的单元格在新模型对应页签、行项与期间上退化为常数。 |
| **公式纪律** | `summed_year_violation` | `fail` (`fail`) | **季度加总年专项检查**：当某预测年度列在同表右侧拥有完整四个季度列时：（1）流量金额行不是四季度求和公式；（2）该年度列下的比率行（`YoY`、利润率、占比、`Take rate`）写成了常数、相对假设公式或保留了 `FFFFC8` 假设样式，而非由该年度列自身金额单元格反算。 |
| **公式纪律** | `formula_hidden_literal` | `fail` (`fail`) | 普通黑字/绿字金额计算公式内部直接写死业务假设常数（如 `=E10*1.15`）。**合法放行例外**：位于非加总预测列、显式套用假设样式（蓝字 + `FFFFC8` 底色）且结构为「单期基准格 ± 常数增量」或「基准格 × (1 ± 常数增量)」的相对假设公式（如 `=R138+0.3%`、`=P9-8%`），记入 `stats.relative_assumption_formulas` 并正常通过；结构性常数（`0, 1, -1, 2, 4, 12, 100, 360, 365, 1000, 1000000`、幂指数及 `Checks` 判定阈值）同步放行。 |
| **公式纪律** | `formula_literal` | `fail` (`fail`) | 公式内容仅为一个裸常数（如 `=0`、`=500`）。 |
| **勾稽检查** | `checks_sheet_rollup` | `fail` (`fail`) | 工作簿缺少 `Checks` 页，或 `Checks` 页缺少动态判定公式与 `Overall` 汇总行，或重算后 `Checks` 页存在任一行结果为 `FAIL`。 |
| **勾稽检查** | `annual_quarters_tieout` | `fail` (`fail`) | 对同表同时存在年度列与四个季度列的预测期及近期历史流量核心行（总收入、毛利、营业利润、净利润、经营现金流），直接核验缓存值 `|年度值 − 四季之和| ≤ max(0.5, 1e-4 × |年度值|)`（已在 `Checks` 页显式登记为历史重述口径差异 `WARN` 并附备注的旧历史年除外）。 |
| **勾稽检查** | `segment_op_tieout` | `fail` (`fail`) | 当表内同时存在分部营业利润小计/分部块与集团营业利润行（或 `Checks` 页登记了分部勾稽对）时，核验预测期各期 `集团营业利润 == Σ分部营业利润 + 未分配总部项/抵销项`。 |
| **勾稽检查** | `balance_sheet_tieout` | `fail` (`fail`) | 当工作表包含完整预测期资产负债表（同时存在总资产行与负债及股东权益总计行且预测期非空）时，逐期核验 `|总资产 − 负债及股东权益总计| < 0.05`。 |
| **勾稽检查** | `share_and_rate_diagnostics` | `warn` (`warn`) | （1）现金流量表预测期有重大回购现金流出而稀释股本行在预测期完全恒定不变（疑似漏挂股本滚存公式）；（2）利息收入/期初现金或利息支出/期初有息债的隐含年化利率落在 `[0%, 10%]` 常规区间之外（提示排查单位错配千倍或分母错行）。 |
| **勾稽检查** | `summary_table_links` | `warn` (`fail`) | 工作簿缺少包含收入与 `EPS` 的汇总表（或估值总览表），或汇总表中的预测数值为静态硬编码而非引用计算页的公式。 |

### 5.3 输出格式规范

`style_audit.py` 向标准输出打印单个 UTF-8 JSON 对象（字段与上游 `audit.py` 保持同构，便于自动化测试与工作流探针统一读取，每个检查项最多附 25 个具体单元格示例）：

```json
{
  "status": "pass",
  "strict": true,
  "file": "model/China_eCommerce_Industry_Model_v01_20261125.xlsx",
  "baseline": "sources/China eCommerce_Industry Model_20261008.xlsx",
  "stats": {
    "sheets_audited": 41,
    "formula_cells": 68420,
    "relative_assumption_formulas": 412,
    "baseline_formulas_checked": 59310,
    "baseline_formula_regressions": 0,
    "checks_sheet_fails": 0,
    "checks_sheet_warns": 1
  },
  "findings": [
    {
      "check": "share_and_rate_diagnostics",
      "level": "warn",
      "count": 1,
      "message": "Implied annualized cash yield sits outside [0%, 10%]; verify currency/unit alignment or explain in Checks column D",
      "examples": ["Model - VIPS!AG48 (implied rate 0.4%, note: onshore RMB demand deposit)"]
    }
  ]
}
```

- **退出码规则**：命令行参数错误或文件无法打开时输出 `{"status": "error", "message": "..."}` 并以退出码 `1` 退出；存在任一 `level == "fail"` 项时 `status` 为 `"fail"`，在 `--strict` 下退出码为 `1`；无 `fail` 项时 `status` 为 `"pass"`，退出码为 `0`（所有剩余 `warn` 项须在模型 `Checks` 页 D 列和交付回复中逐条说明原因）。

---

## 6. 合理性检查、一致预期拆解与理由落表流程

### 6.1 市场一致预期与卖方预测的数据来源（双形态能力门控）

为满足 `docs/dalton/features/standalone-core.md` 的双形态架构要求，`dalton-model` 正文与 `references/sanity-check.md` 中涉及数据源调用的段落统一通过 `src/dalton/content_assembly.py` 的条件注释标记组装：

```markdown
<!-- DALTON_CAP:researchdata_bridge -->
1. **市场一致预期（`estimates`）**：调用 `researchdata.finance(route="estimates", ticker=...)` 获取未来两季与未来两至三年的收入、营业利润、净利润及每股收益一致预期均值、最高/最低区间与覆盖分析师人数，并将原始返回保存快照至工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`（附 `_meta{route, fetched_at, source}`）。
2. **卖方分部与驱动预测**：调用 `researchdata.research`（检索已启用的卖方研报与纪要路由）查阅最近一个财报季后 3–5 家主流覆盖机构的分部收入、利润率与关键经营指标拆解，识别市场主线预期。
<!-- DALTON_CAP_ELSE -->
1. **市场一致预期（公开市场数据源）**：调用公开市场数据 MCP（Model Context Protocol，模型上下文协议）工具获取一致预期——优先使用 `yf_fundamentals`、`yf_analysis.get_growth_estimates`（覆盖当季 `0q`、下季 `+1q`、当年 `0y`、次年 `+1y`）及已配置密钥时的 `fundamentals`（`FMP`〔Financial Modeling Prep〕分析师一致预期接口），将取数结果保存快照至工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`（写明数据源如 `"Yahoo Finance via yfinance"`、获取时间与分析师人数）。
2. **卖方与市场预期参照**：优先读取用户放入工作区 `sources/` 的参考报告，结合公司官方投资者关系（`IR`〔Investor Relations〕）网站公布的一致预期汇总表、公开财报电话会分析师问答环节关注焦点，以及通过 `web_search` / `web_fetch` 检索到的公开机构业绩点评摘要，核对市场对核心分部与利润率的预期水平。
<!-- /DALTON_CAP -->
```

> **快照与证据标签要求**：一致预期数据无历史时点回溯能力，每次合理性检查取数时都先落盘时间戳快照，并在模型备注或预期对照表中注明提取日期与分析师人数（如 `<Yahoo Finance 2026-11-25, N=34`），证据类型统一标记为市场预期（`street estimate` / `consensus`），与公司法定披露（`fact`）和我方假设（`assumption`）严格区分。

### 6.2 重大差异拆解四步法

当未来两个财年中任一财年的集团总收入偏离一致预期达到 **±3%**，或营业利润 / `Non-GAAP` 净利润 / `EPS` 偏离达到 **±5%**（微利转型公司按营业利润率偏离达到 **±150 bps**〔basis points，基点，1 个百分点 = 100 基点〕）时，按以下四步执行拆解：

1. **第一步：口径对齐核查**
   先核对双方是否在比较同一个口径：确认一致预期采用的是 `GAAP` 还是 `Non-GAAP`（是否剔除 `SBC`、无形资产摊销及公允价值变动）、报表币种还是交易币种（如人民币 `RMB` 还是美元 `USD`，汇率取数日是否一致）、普通股 `EPS` 还是每份 `ADS` 收益 `EPADS`。因口径错位造成的表面差异先换算到同一口径再比对。
2. **第二步：利润表与股本逐层归因（`Variance Bridge`）**
   按计算链路将我方与一致预期的净利润及 `EPS` 差额拆解为五项之和：
   - **分部收入规模差**：各业务分部 `GMV`/用户/货币化率假设与卖方主流拆法的差异；
   - **分部利润率与总部未分配费用差**：核心主站利润率、新业务减亏幅度，以及集团主表是否完整计入了未分配总部开支；
   - **净利息与非经常损益差**：净现金滚存带来的利息收入（核验期初现金基数与隐含存款收益率是否合理）、联营公司投资收益及汇兑损益（预测期汇兑与公允价值变动默认置 `0`）；
   - **有效税率差**：核查是否因某单季偶发退税或不可抵扣亏损导致预测期有效税率偏离稳态区间；
   - **稀释股数差**：核查股份回购注销节奏与 `SBC` 自然稀释对每股分母的净影响。
3. **第三步：区分「建模疏漏」与「主动独立观点」**
   - 凡属分部加总漏扣未分配项、把单季一次性收益外推至远期、现金重复计息、回购扣了股数却未扣现金流、加总年残留旧假设数等建模缺陷，当场回到模型公式修正；
   - 凡属缺乏一手证据支撑的非核心科目偶然偏离，将假设收敛回历史中枢或行业合理带；
   - 凡属围绕核心争议变量、且具备一手财报先行指标（如递延收入、剩余履约义务）、高频经营跟踪数据或明确股东回报算术支撑的主动判断，确认为有效独立观点（`contrarian view`），保留我方预测数字。
4. **第四步：五项内部合理性复核**
   同步在工作簿 `Checks` 页确认：（1）集团营业利润等于分部之和加未分配项；（2）季度加总等于年度；（3）隐含存款收益率与借款利率处在历史与当前市场合理带；（4）股本增减与现金流回购及 `SBC` 匹配；（5）完整资产负债表逐期平衡。

### 6.3 判断依据（`rationale`）在模型工作簿内的写入位置

与一致预期存在明显差异的判断依据，直接写入 Excel 工作簿内，让阅读者打开模型即可定位差异原因：

| 模型形态 | 模型内具体写入位置 | 内容与格式要求 |
|---|---|---|
| **用户存量模型（单公司或行业多公司模型）** | **位置 1（主定位）：各公司 `Valuation - <TICKER>` 页（或 `Summary` 页）估值表正下方的 `Consensus vs House` 对照小节**（A 列涂 `99CCFF` 报表级分节条）<br/>**位置 2（行级补充）：`Model - <TICKER>` / `Key Metrics - <TICKER>` 对应驱动假设行的最右侧红字备注列** | 1. 对照小节包含列：`指标 | 期间（如 2026E / 2027E / 2028E）| House 预测 | Consensus | 差异（% 或 bps）| 核心差异来源与坚持理由（Rationale）| 共识来源与日期`。<br/>2. `House 预测` 列全部写跨表绿字公式（`008000`）引用 `Model - <TICKER>`，`Consensus` 列写蓝字输入（`0000FF`），`差异` 列写黑字斜体公式。<br/>3. `Rationale` 列用一句清晰中文点明具体分部、具体驱动参数差值与证据（如：`JD 27E Non-GAAP NI 低于一致预期 6%：我们对新业务投入多计 35 亿亏损，依据 3Q26 电话会管理层扩招骑手指引`）。<br/>4. 多公司行业模型同时在首页 `README & Changelog` 的公司总览表旁附一列「相对一致预期核心差异摘要」。 |
| **从零新建的单公司标准模型** | **独立工作表 `Street vs House`**（紧随计算表与情景表之后，与 `dalton-full-coverage` 的同名工作表结构一致） | 完整列出未来两季与未来两至三年的集团总收入、各关键分部收入与营业利润、毛利率、各项费用率、`Non-GAAP` 营业利润、净利息、有效税率、稀释股数、`Non-GAAP EPS` 与 `FCF`；`House` 列全绿字跨表公式引用计算页，每项重大差异同行写明拆解数字、证据来源与改判观察条件。 |

---

## 7. 模型存放路径与版本命名规范

### 7.1 统一存放目录：`model/`

- **单公司工作区与行业工作区统一使用工作区根目录下的 `model/` 目录**存放正式财务模型工作簿。该路径与知识层 `src/dalton/knowledge/schemas/models.schema.json` 的路径校验规则（`^model/.+\.xlsx$`）完全一致，公司工作区与绑定行业覆盖单元（`sector profile`）的行业工作区均可通过 `knowledge_update(doc="models", ...)` 登记模型版本并由服务端自动计算归档 `sha256`。
- **历史双目录归口迁移规则**：若存量工作区同时存在历史遗留的 `models/` 与标准 `model/` 目录（如早期会话在两处各放了一份副本），执行本技能时先对比两处文件的修改时间、公式完整性与重算状态，将较新且完整的一份保留在 `model/`，后续所有新版本只写入 `model/`，并在当次 `README & Changelog` 与交付回复中说明已统一归口至 `model/`。
- **构建与更新脚本位置**：每次新建或滚动维护编写的 Python 脚本统一存放在当次研究任务子目录 `research/<YYYY-MM-DD>-model-<动作>/`（如 `research/2026-11-25-model-rollforward/update_model.py` 及 `style_profile.json`），使 `model/` 目录只保留干净的历次 `.xlsx` 模型版本文件。

### 7.2 原件保留与新版本命名规则

1. **原件只读留存**：
   - 用户通过聊天上传的原始模型文件保留在 `sources/`（或原上传位置）字节级只读不动；`model/` 下已有的全部历史版本（`v01`、`v02` 等）同样只读保留，供随时做跨版本公式保留率检查（`--baseline`）与盈利预测变动对比。
   - 动手修改前先记录基准模型文件的 `SHA-256` 哈希值，写入新模型 `README & Changelog` 页的「基准底稿版本与哈希」栏，并在交付前复核原文件哈希未发生任何改变。
2. **新版本文件命名**：
   - **单公司工作区**：固定命名为 `model/<TICKER>-model-v<NN>.xlsx`（两位数字递增版本号，如 `model/FUTU-model-v01.xlsx` → `model/FUTU-model-v02.xlsx`，直接兼容 `earnings-deep-update` 与 `full-coverage` 工作流通过 `ls model/{ticker}-model-v*.xlsx | sort -V | tail -1` 解析最新版本）。当用户首次在某公司工作区上传自有模板（如 `sources/Futu_Model_202608.xlsx`）做第一次滚动更新时，原件留在 `sources/`，新产物存为 `model/<TICKER>-model-v01.xlsx`。
   - **行业多公司工作区**：行业工作区本身不设单一股票代码，命名保留原行业模型的英文或拼音主题标识（空格统一替换为下划线），附两位递增版本号与更新日期：`model/<Industry_Slug>_Model_v<NN>_<YYYYMMDD>.xlsx`（例如用户原件 `sources/China eCommerce_Industry Model_20261008.xlsx` 保持不动，首次滚动更新产出 `model/China_eCommerce_Industry_Model_v01_20261125.xlsx`，下一季度更新产出 `model/China_eCommerce_Industry_Model_v02_20270320.xlsx`；同一天内多轮修订依次递增 `v01` → `v02`）。

---

## 8. 技能注册、能力门控与同步修改清单

### 8.1 注册与能力映射（`CORE_CAPABILITY`，核心安装与生产环境共用的基础能力标记）

`dalton-model` 依托本地 `.xlsx` 读写、公式重算与公开市场数据工具即可独立运行（启用内部桥接时自动扩展研究库检索），因此在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 中注册为 **`CORE_CAPABILITY`**（即 `None`，核心安装与 Everflow 生产环境均默认开启并同步进沙箱 `.agents/skills/dalton-model/`）：

- `_DALTON_SKILL_SPECS` 新增条目：
  ```python
  ("dalton-model", "dalton-model", ("dalton-conventions", "research-conventions")),
  ```
- `DALTON_SKILL_CAPABILITY_MAP` 核心投资研究骨架分组新增条目：
  ```python
  "dalton-model": CORE_CAPABILITY,
  ```
  （该分组技能数由 **29** 增至 **30**，Dalton 内置技能总数由 **51** 增至 **52**。）

### 8.2 需要修改的代码、配置与测试快照清单

#### （1）第 1 批（新技能本体与注册）涉及文件

| 类别 | 文件路径 | 修改内容 |
|---|---|---|
| 新技能目录 | `plugins/dalton/skills/dalton-model/**` | 新增 `SKILL.md`、`references/{format-spec,forecast-rules,rollforward,sanity-check}.md`、`scripts/{model_style,style_audit}.py`（涉及桥接数据源处使用 `<!-- DALTON_CAP:researchdata_bridge -->...<!-- DALTON_CAP_ELSE -->...<!-- /DALTON_CAP -->` 包裹，零内部域名硬编码）。 |
| 方法论索引 | `plugins/dalton/skills/dalton-conventions/SKILL.md` | 在「§4 按任务找参考」表格的「财务建模 / 历史对表 / 财务镜头」行补充 `.agents/skills/dalton-model/SKILL.md`。 |
| 技能注册表 | `src/ptc_agent/agent/middleware/skills/dalton_registry.py` | `_DALTON_SKILL_SPECS` 与 `DALTON_SKILL_CAPABILITY_MAP` 各加一行 `dalton-model`（`CORE_CAPABILITY`）。 |
| 能力门禁单测 | `tests/unit/dalton/test_core_capabilities_gate.py` | `TestBatch3BackendTextAndSkillGates` 中核心可见技能数断言由 `29` 更新为 `30`（同时自动校验 `dalton-model` 核心态组装文本零专有词泄露）。 |
| 技能注册单测 | `tests/unit/dalton/test_b3_skills_data_map_prompts_migration.py` | `test_every_dalton_skill_on_disk_is_explicitly_registered` 中磁盘技能总数断言由 `51` 更新为 `52`。 |
| 快照采集与既有技能总数断言 | `tests/unit/dalton/_everflow_parity_capture.py` 及 8 个写死 `len(_DALTON_SKILL_SPECS) == 51` 的单测文件（`test_agentturn_ai_app.py`、`test_agentturn_mark_weekly.py`、`test_agentturn_richard_earnings.py`、`test_agentturn_tmeet.py`、`test_agentturn_wiki_ingest.py`、`test_declared_sources.py`、`test_podcast_weekly_migration.py`、`test_richard_coverage_weekly.py`） | `_everflow_parity_capture.py` 两处磁盘技能数断言由 `51` 更新为 `52`；上述 8 个单测中的技能总数断言同步更新为 `52`。 |
| Everflow 全文快照 | `tests/unit/dalton/snapshots/everflow/**` | 运行 `pytest tests/unit/dalton/test_everflow_profile_parity.py --update-everflow-snapshots` 重新生成快照（更新 `app_env_dalton/manifest.json`、`app_env_empty/manifest.json`、主智能体提示词快照及技能组装全文快照）。 |
| 新增专项单测 | `tests/unit/dalton/test_modeling_skill.py` | 覆盖 `model_style.py`、`style_audit.py`、双形态条件装配与合成模型工作簿测试（详见第 9 章）。 |
| 路径登记校验 | `scripts/dalton/dalton_paths.txt`（无需改动，跑 `check_hooks.sh --strict` 验证） | 上述路径均已落在 `plugins/dalton/`、`src/ptc_agent/agent/middleware/skills/dalton_registry.py` 与 `tests/unit/dalton/` 已登记前缀内，零新增上游文件 `DALTON-HOOK`。 |

#### （2）第 2 批（`dalton-earnings-deep` 与 `dalton-full-coverage` 联动接入）涉及文件

> **关键耦合点说明**：`workflows/earnings-deep-update/contract.json`、`workflows/full-coverage/contract.json` 与 `workflows/full-coverage/workflow.js` 的只读探针 `strictAudit` 当前直接写死了 `python .agents/skills/xlsx/scripts/audit.py ... --strict`。第 2 批将这两个技能切换至 `dalton-model` 时，必须**同步切换 `workflow.js` 与 `contract.json` 中的模型验证器命令为 `style_audit.py --strict`**，否则按 Dalton 规范生成的相对假设公式（`=R138+0.3%`）和右侧红字来源（无批注）会被旧验证器拦截。

| 类别 | 文件路径 | 修改内容 |
|---|---|---|
| 财报深度链技能与工作流 | `plugins/dalton/skills/dalton-earnings-deep/SKILL.md`<br/>`workflows/earnings-deep-update/workflow.js`<br/>`workflows/earnings-deep-update/contract.json` | 支线 B 模型更新改为加载 `.agents/skills/dalton-model/SKILL.md` 在现有模型上滚动更新（保留 `mapping.md`、`Change Log` 与 `model-delta.md`），并将 `contract.json` 与子任务提示词中的 `xlsx/scripts/audit.py --strict` 替换为 `dalton-model/scripts/style_audit.py --strict`。 |
| 完整覆盖技能与工作流 | `plugins/dalton/skills/dalton-full-coverage/SKILL.md`<br/>`plugins/dalton/skills/dalton-full-coverage/references/model-spec.md`<br/>`workflows/full-coverage/workflow.js`<br/>`workflows/full-coverage/contract.json` | 阶段 2 建模改为：工作区已有用户模型时调用 `dalton-model` 在原模型上滚动扩展并补齐 `Facts`、`Guidance vs Actual`、`Street vs House`、`Checks` 所需表格与锚点；尚无模型时按 15 页规格新建并套用 `dalton-model` 样式规范；`workflow.js` 的 `strictAudit` 探针与 `contract.json` 的验证器统一改为 `dalton-model/scripts/style_audit.py --strict`。 |
| 工作流单测与快照 | `tests/unit/dalton/test_workflows_js.py`<br/>`tests/unit/dalton/snapshots/everflow/**` | 更新 `test_workflows_js.py` 中 `_contract_audit_pick` 对模型审计脚本路径的匹配断言，并重生成 Everflow 技能与工作流快照。 |

---

## 9. 测试方案与用户验收标准

### 9.1 自动化单元测试（`tests/unit/dalton/test_modeling_skill.py`）

单测全部基于 `openpyxl` 在内存或临时目录离线运行，不依赖外部网络、数据库或未提交至仓库的本地大文件：

1. **技能注册与双形态条件装配测试**：
   - 验证 `dalton-model` 在 `DALTON_PROFILE=core` 与 `DALTON_PROFILE=everflow` 下均可由 `get_skill("dalton-model")` 正常解析，前置依赖包含 `dalton-conventions` 与 `research-conventions`；
   - 对 `plugins/dalton/skills/dalton-model/` 下全部 `.md` 文件运行 `read_assembled_text`，验证核心形态下专有词黑名单（`researchdata`、`alphaengine`、`guidepoint`、`feishu`、`everflow` 等）命中数为 `0`，且无未闭合的 `DALTON_CAP` 标记。
2. **`model_style.py` 样式写入与存量画像提取测试**：
   - **默认样式精确性**：调用辅助函数构建示例工作表，重读断言中英文自动匹配 `等线` 8pt / `Arial` 8pt、第 1 行 `3366FF` 亮蓝底白字加粗、历史值 `0000FF` 无底色、跨表链接 `008000`、假设格 `0000FF` + `FFFFC8` + 四边 `hair` 边框（百分比斜体、金额正体）、衍生比率黑字斜体 `0.0%`、合计行加粗无边框、A 列三色分节条只涂 A 列、右侧红字备注列无批注、目标价 `C00000` 加粗、网格线关闭及冻结窗格位置正确；
   - **存量工作簿画像（`profile_workbook_style`）**：构造模拟真实用户习惯的多页工作簿（含 `theme=1` 黑色字体、`theme=6` 历史估算填充、会计数字格式、`3Q26`/`4Q26E` 与 `2026Q2`/`2026Q3E` 两种期间标签、`_CIQHiddenCacheSheet` 隐藏页），验证画像器准确识别历史/预测分界、季度加总年列、纯年度预测列、假设样式与右侧红字备注模式；
   - **相邻列克隆**：验证 `clone_period_column_style` 在扩列时完整保留行缩进、行高列宽、小计加粗与各行特定数字格式。
3. **`style_audit.py` 正反向门禁测试**：
   - **合规模型零误报放行**：构造包含 `=R138+0.3%` 和 `=P9-8%` 相对假设公式、无单元格批注（采用右侧红字备注列）、带无引用 `_CIQHiddenCacheSheet`、季度加总年比率由年度金额反算、分部营业利润等于集团营业利润、`Checks` 页 `Overall = OK` 的工作簿，断言 `style_audit.py --strict` 返回 `status == "pass"` 且退出码为 `0`；
   - **违规项逐条精准拦截**：分别构造单点缺陷工作簿，断言 `--strict` 精确报出对应检查项与单元格坐标并返回退出码 `1`：
     1. 预测期金额行写入常数 → `forecast_amount_hardcode`；
     2. 相对 `--baseline` 原模型将预测公式改为常数 → `formula_regression_vs_baseline`；
     3. 季度加总年（如 `2027E` 含 `1Q27E`–`4Q27E`）金额行非四季求和，或下面 `YoY` / 利润率行写成常数或保留 `FFFFC8` 假设样式 → `summed_year_violation`；
     4. 黑字金额公式内夹带 `=E10*1.15` 隐藏常数 → `formula_hidden_literal`；
     5. 混入 `1F4E79`/`D9E1F2` 外来底色或 `10pt`/`11pt` 字体 → `foreign_template_style`；
     6. 新增假设格缺失 `FFFFC8` 底色或四边 `hair` 虚线框 → `assumption_cell_style`；
     7. 残留 `_xll.ciqfunctions` 或 `#NAME?`/`#REF!` → `external_or_plugin_link` / `cached_error`；
     8. 季度之和不等于年度、分部营业利润之和不等于集团、完整资产负债表不平、`Checks` 页有 `FAIL` → 对应勾稽 `fail`；
     9. 大额回购现金流出而稀释股本恒定不变，或隐含存款年化收益率高达 `45%` → `share_and_rate_diagnostics` (`warn`)。
4. **两本用户真实样本模型的本地开发对照检查**（开发阶段在宿主机对 `.agy/modeling_samples/` 只读运行验证，样本文件不入库）：
   - 对 `ecom_20261008.xlsx` 运行 `model_style.py profile` 与 `style_audit.py`：确认准确提取出 `Arial`/`等线` 8pt、`3366FF`、`FFFFC8` + 四边 `hair`、A 列三色分节条与右侧红字备注；准确检出原底稿待清理的 `_xll.ciqfunctions` 插件公式、缺失 `Checks` 页及 `Model - JD` `2026E` 个别遗留金额常数，同时对原表数百个合法相对假设公式零误报；
   - 对 `brokerage_20260820.xlsx` 运行同样检查：确认准确识别 `2026Q2`/`2026Q3E` 期间格式、券商生息资产—利息桥接公式与 `=P9-8%` 假设公式。

### 9.2 用户验收标准（中国电商工作区滚动一季实测）

第 1 批合入后，在测试环境或「中国电商」工作区以原底稿 `China eCommerce_Industry Model_20261008.xlsx` 为起点，使用 `dalton-model` 执行一次真实滚动维护验收，对照用户此前提出的全部纠正点逐条核验：

1. **原件与路径合规**：原底稿字节哈希不变；新模型输出至 `model/China_eCommerce_Industry_Model_v01_<YYYYMMDD>.xlsx`（`model/` 单一目录），原 40 张工作表中的历史专题分析页完整保留，全部 `_xll.ciqfunctions` 插件公式已替换为可核验静态输入加动态公式。
2. **原公式链 100% 保留**：传入原底稿作 `--baseline` 运行 `style_audit.py --strict`，PDD、JD、BABA 原有预测公式行零退化为硬编码（`baseline_formula_regressions == 0`），所有预测期金额行均为动态公式。
3. **季度加总年比率干净**：`2026E` 与 `2027E` 年度流量金额全部由四季 `SUM` 得出，其下 `YoY` 增速、毛利率、费用率、营业利润率、`Take rate` 与业务占比全部由该年度列自身金额反算（黑色斜体、无底色、零假设残留）；`2028E` 纯年度列由显式假设驱动；`CAGR` 列起止年份已顺延。
4. **现金—利息—回购—股本与分部勾稽闭环**：各公司集团营业利润等于分部营业利润加总（含未分配项）；净现金公司利息收入挂钩期初现金滚存且隐含收益率合理；股份回购支出扣减现金流并同步减少稀释股本。
5. **一致预期对照与理由入表**：各家公司未来两年预测已对照最新市场一致预期（重点核查 JD `2027E`–`2028E` 净利润水平），重大差异已在各家 `Valuation - <TICKER>` 页的 `Consensus vs House` 小节及计算页最右侧红字备注列写明具体分部/假设拆解与理由。
6. **原生样式与版面一致**：全表保持 `Arial`/`等线` 8pt，零外来 `1F4E79`/`D9E1F2` 配色，新增列与新增页的分节条、页签颜色、早年列分组折叠和近季冻结窗格与原模板一致，`render.py` 渲染核心页无 `###` 溢出或格式错位。
7. **重算与审计全绿**：`recalc.py` 返回 `success`（0 错误、0 外链、0 循环引用），`style_audit.py --strict --baseline <原底稿>` 退出码为 `0`，`Checks` 页 `Overall` 显示 `OK`。

---

## 10. 分批实施计划

全部开发工作拆分为两个独立可合并、可验证的批次推进：

| 批次 | 交付范围 | 验收标准 |
|---|---|---|
| **第 1 批：`dalton-model` 技能本体、样式库、检查脚本与核心注册** | 1. 完成 `plugins/dalton/skills/dalton-model/` 下 `SKILL.md`、4 份 `references/*.md`、`scripts/model_style.py` 与 `scripts/style_audit.py`；<br/>2. 在 `dalton_registry.py` 注册为 `CORE_CAPABILITY`，在 `dalton-conventions/SKILL.md` 补一行参考索引；<br/>3. 交付 `tests/unit/dalton/test_modeling_skill.py` 单测，更新技能计数断言并刷新 `tests/unit/dalton/snapshots/everflow/` 快照。 | 1. 单测覆盖默认样式写入、存量样式提取、全部 `style_audit.py` 正反向规则与双形态专有词门禁全绿；<br/>2. 本地对两本用户样本模型只读运行画像与审计符合预期；<br/>3. `check_hooks.sh --strict` 与 `pr_baseline.sh` 全绿，可直接在中国电商工作区做滚动一季实测验收。 |
| **第 2 批：`dalton-earnings-deep` 与 `dalton-full-coverage` 切换接入本技能** | 1. 更新 `dalton-earnings-deep/SKILL.md`、`workflows/earnings-deep-update/{workflow.js,contract.json}`，使支线 B 模型更新转用 `dalton-model` 并以 `style_audit.py --strict` 作为完成契约验证器；<br/>2. 更新 `dalton-full-coverage/SKILL.md`、`references/model-spec.md`、`workflows/full-coverage/{workflow.js,contract.json}`，在已有用户模型时转用 `dalton-model` 滚动维护，并将 `strictAudit` 探针与契约验证器统一切换为 `style_audit.py --strict`；<br/>3. 更新 `tests/unit/dalton/test_workflows_js.py` 与 Everflow 快照。 | 1. 工作流单测（含契约审计命令解析、模型版本递增与断点续跑测试）全绿；<br/>2. 核心态与 Everflow 态技能及工作流全文门禁、快照比对与 `pr_baseline.sh` 全绿。 |

---

## 11. 待用户决定的问题

无。以下两项已由用户确认（2026-11-25）：

1. 技能目录名、注册名与斜杠命令统一为 `dalton-model`（`/dalton-model`）。
2. 工作区没有用户模型、需要从零新建时，同样使用第 4 章的默认格式，并用 `style_audit.py --strict` 验收。
