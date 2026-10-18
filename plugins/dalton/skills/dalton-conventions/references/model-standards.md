# 财务模型期间、分层与镜头标准（Model Standards）

## 1. 期间标准（Period Coverage Standards）

所有 Dalton 财务模型（`<TICKER>-model-v<NN>.xlsx`）必须满足以下期间跨度要求：
- **年度历史（Annual Actuals）**：至少从 **FY2018A** 起连续覆盖至最近完整财年（若公司上市较晚，则从招股书最早完整财年起）。
- **单季历史（Quarterly Actuals）**：至少从 **2024Q1A** 起连续覆盖至最近已披露季度。
- **年度预测（Annual Forecasts）**：向前滚动预测至少 **3 个完整财年**（如当前为 FY26，须覆盖 FY26E、FY27E、FY28E）。
- **季度预测（Quarterly Forecasts）**：向前滚动预测至少覆盖至**次年 Q4**（如当前在 2026 年内，季度预测须逐季覆盖至 2027Q4E）。
- **单向扩展原则**：随着新财报披露，模型的历史与预测覆盖期**只能向后扩展，严禁缩短或删除早期历史列**。

---

## 2. 一手 Filing 硬编码与单元格级来源台账

- **历史实际数（Actuals）只取一手监管 Filing**：美股取 SEC 10-K / 10-Q / 20-F（经 `dalton_sec.extract_facts` 或 `researchdata.finance(route="sec")`），港股/A 股取法定年报/季报/业绩公告。严禁直接把卖方模型里的历史数抄入底表。
- **单元格级来源台账（Provenance Ledger）**：每个历史硬编码区域必须配套记录或可追溯至以下 6 项元数据：
  1. **法定文件与 Accession**（如 `10-K (0001326801-26-000012)`）
  2. **报告期间与财年结束日**（如 `FY2025 (ended 2025-12-31)`）
  3. **报表名与页码 / Note 编号**（如 `Consolidated Statements of Income, p.68`）
  4. **SEC 归档 URL**
  5. **原始披露单位**（如 `$ in millions`）
  6. **会计准则与口径**（`GAAP` / `IFRS` / `Non-GAAP Reconciled`）

---

## 3. 累计期间拆单季规则（Derived Quarterly Actuals）

- 当公司仅在季报/半年报/年报中披露累计期间（YTD：6M / H1、9M、FY）而未直接列出单季（3M ended）数字时，单季历史值必须通过算术差分推导：
  - `Q2 = H1 (6M) − Q1`
  - `Q3 = 9M − H1 (6M)`
  - `Q4 = FY − 9M`（或 `FY − Q1 − Q2 − Q3`）
- 此类推导值在模型台账与正文中统一标记为 **`Derived filing actual`**（并注明被减项各自的 Accession），不得伪装成直接披露的单季行。

---

## 4. 模型五层分类与证据标签映射

模型中的每一行数据必须归入以下五层之一，并与 `research-conventions` 的七类证据标签严格对应：

| 模型分层（Model Layer） | 定义与纪律 | 对应证据标签 |
|---|---|---|
| **1. Filing Actual（一手披露值）** | 法定财报底表或附注直接披露的原始数字 | `fact` |
| **2. Derived Filing Actual（披露衍生值）** | 由一手披露数纯算术推导的值（如 `FY−9M` 单季值、同比增速、毛利率、`FCF = CFO − Capex`） | `fact` / `model-derived` |
| **3. Imputed / Residual（推算与分摊值）** | 用披露总数减去已知分部得出的残差，或按某种比例分摊的估算值。**严禁把残差或按比例分摊的数冒充公司直接披露数！** | `model-derived` |
| **4. Explicit Assumption（显式预测假设）** | 分析师在预测期设定的核心驱动参数（如增速、Take rate、费用率、税率） | `assumption` / `judgement` |
| **5. Scenario Output（情景输出）** | 在 Bull / Base / Bear 情景下由公式联动算出的预测期财务结果与估值 | `model-derived` |

---

## 5. 空值纪律（Blank vs Zero Rule）

- **假设或历史未披露项为空时一律留空（Blank / `null`），严禁填 `0`**：
  - 在 Excel 与 JSON 台账中，`0` 代表"公司实际发生了 0 元"或"分析师显式预测该项为 0%"；
  - 尚未披露的细项或不适用的假设必须留空（或标 `N/A`），避免拉低均值、扭曲同比增速或触发除零错误掩盖。

---

## 6. 模型修订日志与事后对账（Model Revision & Post-Earnings Reconciliation）

- **每次修改模型版本（`vNN -> vNN+1`）必须记录四项**：
  1. **旧值（Old Value）** vs **新值（New Value）**（含关键假设与 FY/Q 收入、利润、EPS 变化幅度）；
  2. **修改原因（Reason & Evidence）**（哪条新披露或新证据触发了修改，绝不允许"因为卖方一致预期变了所以跟着改"）；
  3. **受影响的 Thesis 支柱或 Debate 编号**（如 `P1`、`D2`）。
- **事后对账（Actual vs Forecast Reconciliation）**：每季财报披露后，必须对比实际数与我们财报前模型的预测数；当实际数推翻原预测（误差超阈值或方向相反）时，必须在模型修订日志与 `lessons.md` 中写一条对账归因记录。

---

## 7. 四大财务镜头（Financial Lenses）标准定义

在深度覆盖与模型分析中，统一使用以下四组买方财务镜头（自动化计算由 P2 的 `dalton-financial-lenses` 负责，此处定义口径）：

### 7.1 净现金（Net Cash）双口径
- **狭义净现金（Strict Net Cash）**：`现金及现金等价物 + 短期定期存款/短期国债 (Short-term investments / Marketable securities) − 总有息债务（短期借款 + 一年内到期长期负债 + 长期借款 + 融资租赁负债）`。
- **广义净现金（Broad Net Cash）**：在狭义口径基础上，加上可随时变现的长期理财/非受限可出售金融资产，并扣除受限现金（Restricted Cash）及明确定性的准债务项（须逐项列明细）。计算净现金占市值比（Net Cash / Market Cap）与 Ex-Cash P/E。

### 7.2 隐蔽资产（Hidden Assets）
- 识别未合并报表但具重大价值的股权投资（Equity Method / Cost Method Investments，按公允市值或最新融资估值折算）、超额土地/不动产、NOL 税务盾或亏损业务分部（SOTP 还原核心主业真实盈利）。

### 7.3 股东回报三层次（Shareholder Return R1 / R2 / R3）
- **R1（现金分红率 Dividend Yield）**：`年度现金股息总额 / 当前市值`。
- **R2（总股东现金回报率 Total Cash Yield）**：`(年度现金股息 + 年度回购注销金额) / 当前市值`。
- **R3（净股东回报率 Net Shareholder Yield，扣除 SBC 稀释）**：`(年度现金股息 + 年度股票回购总额 − 年度股份支付费用 SBC) / 当前市值`（或用期末稀释总股本 YoY 真实缩减率验证回购是否仅用于抵消员工期权稀释）。

### 7.4 盈利质量（Earnings Quality）
- **Sloan Ratio（应计比率）**：`(Net Income − CFO − CFI) / Average Total Assets`（绝对值超过 ±10% 需警惕应计利润操纵或激进资本化）。
- **现金流转化率（Cash Conversion）**：`CFO / Net Income` 与 `FCF / Non-GAAP Net Income`（连续多年显著低于 80% 必须追查原因）。
- **营运资金周期（CCC）**：`DSO（应收账款周转天数）+ DIO（存货周转天数）− DPO（应付账款周转天数）` 的边际变化趋势（警惕塞渠道导致的 DSO 异常拉长）。

---

## 8. 期间命名与单位规则（与 `dalton_sec` R1–R10 对齐）

- **期间键命名**：年度统一写 `FY2024`、`FY2025`（或简写 `FY24A`、`FY26E`）；季度统一按**公司财年（Fiscal Calendar）**写 `2025Q1`、`2025Q2`（正文中可写 `1Q25`、`2Q26`），并在公司非自然年结账（如 MSFT 6 月底结账、NVDA 1 月底结账）时在表头注明财年截止月（如 `FY26 ends June 2026`）。
- **单位一致性**：同一张表内金额单位必须统一（美股大盘股通常统一为 `USD Millions` 或 `USD Billions`，每股指标为 `USD / share`，股数为 `Millions` 或 `Billions`）；跨表对数时注意 SBC、D&A 往往只在现金流量表完整披露，分部收入从附注提取时须核对是否含分部间抵销（Intersegment Eliminations）。
