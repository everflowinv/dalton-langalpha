# 数字底表规则与必填键清单（`facts-ledger.md`）

本文件定义 `full-coverage` workflow 阶段 0 产出的数字底表（`research/<D>-fc/facts.md`）的机器可读格式、必填键（Required Keys）、取数规范与全文数字纪律。

---

## 一、为什么需要数字底表（硬规则）

在多支线并行取证与三段式长文写作中，不同子任务各自临时拉数极易造成“同一指标在不同章节数值或口径打架”。因此完整覆盖执行以下**铁律**：

1. **阶段 0 统一锁表**：由阶段 0 子任务一次性提取一手 SEC filing（最近 4 份定期报告、最近 4 次电话会、招股书、内部人交易）、运行 `dalton-financial-lenses` 三把镜头、计算盈利质量指标，并用 `scripts/facts.py set` 写入 `research/<D>-fc/facts.md`。
2. **后续阶段只读底表**：阶段 1（A/B/C/D 四条支线）、阶段 2（Excel 建模）、阶段 3（memo 三段写作）、阶段 4（独立审稿）**引用目标公司的任何财务与市场数字，只能使用 `facts.md` 中的数字**。
3. **缺数回补机制**：若后续阶段发现某个必须使用的目标公司数字不在 `facts.md` 中，正文中显式标注 `底表缺 X`，并调用 `scripts/facts.py set` 补入 `facts.md`（附一手来源与期间），严禁在正文里私自临时拉数或从二手研报抄数字。
4. **唯一例外（同业与竞对数字）**：竞争对手和可比公司的财务数字不在目标公司底表内，由 A 支线与建模支线通过实时工具（`dalton_sec`、`fundamentals`、`yfinance`）现取，每个同业数字后必须标注 `（期间，来源）`。
5. **正式发布对账基准**：发起 `publish_report` 时，`research/<D>-fc/facts.md` 作为 `{"path": "research/<D>-fc/facts.md", "role": "facts"}` 附件传入，发布数字核验器直接读取其中的 `FACTS-LEDGER` 块进行确定性对账。

---

## 二、`FACTS-LEDGER` 块格式与 CLI 操作

`facts.md` 中必须包含一个 HTML 注释包裹的 JSON 账本块，并在其下方同步渲染为 Markdown 表格：

```markdown
<!-- FACTS-LEDGER
{
  "ledger": [
    {
      "key": "price",
      "label": "当前股价",
      "value": "590.12 USD",
      "period": "2026-10-18",
      "source": "yfinance (as_of 2026-10-18)"
    }
  ]
}
-->

| key | 指标 | 数值 | 期间 | 来源 |
|---|---|---|---|---|
| price | 当前股价 | 590.12 USD | 2026-10-18 | yfinance (as_of 2026-10-18) |
```

### 使用 `scripts/facts.py` 读写底表

一律使用 `scripts/facts.py` 写入或更新条目（自动维护 JSON 账本块与 Markdown 表格同步，按 `key` 幂等 upsert）：

```bash
# 写入或更新单条记录
python .agents/skills/dalton-full-coverage/scripts/facts.py set \
  --file research/2026-10-18-fc/facts.md \
  --key net_cash_strict \
  --label "严格净现金" \
  --value "41,200 百万美元" \
  --period "2Q26" \
  --source "SEC 10-Q (accession 0001326801-26-000012)"

# 读取全部或指定 key
python .agents/skills/dalton-full-coverage/scripts/facts.py get \
  --file research/2026-10-18-fc/facts.md [--key net_cash_strict]

# 检查必填键是否齐全
python .agents/skills/dalton-full-coverage/scripts/facts.py check \
  --file research/2026-10-18-fc/facts.md
```

---

## 三、必填键清单（Required Keys）

阶段 0 结束前，`facts.md` 的 `FACTS-LEDGER` 必须包含以下全部必填键（若某指标因公司属性不适用，如无存货的纯软件公司 CCC，仍须写入该键并将 `value` 设为 `"N/A（纯软件无存货）"`）：

### 1. 市场与股本锚点（来自 `anchors.py` 与最新 10-Q/10-K 封面）
| `key` | `label` 示例 | 口径与来源要求 |
|---|---|---|
| `price` | 当前股价 | 收盘价与币种，标明交易日与数据源（yfinance） |
| `market_cap_usd` | 总市值（美元） | 当前美元市值（百万或十亿美元），与三把镜头和 DCF 共用同一基准 |
| `shares_diluted` | 稀释总股本 | 最新一期稀释平均股数（及封面基本股数核对），标明 filing 期间与 accession |

### 2. 三把财务镜头（来自 `dalton-financial-lenses`）
| `key` | `label` 示例 | 口径与来源要求 |
|---|---|---|
| `net_cash_basic` | 基础净现金 | 基础现金端 − 基础有息债务，含金额与占市值比例 |
| `net_cash_strict` | 严格净现金 | 基础净现金 − 类债务项目 − 不自由现金（DCF 股权桥默认采用此口径） |
| `hidden_asset_net` | 隐含资产折后净值 | 四类非核心可变现资产折后合计及占市值比例（无则写 `0 USD (none)`） |
| `return_r1` | 股东回报 R1（过去12个月） | 过去 12 个月已实现分红率 + 可计分真实回购率（%） |
| `return_r2` | 股东回报 R2（三年平均） | 最近三个完整财年总股东回报率算术平均（%） |
| `return_r3` | 股东回报 R3（未来12个月加权） | 未来 12 个月名义总股东回报率 × 政策可信度（%） |
| `return_score` | 股东回报最终评分 | `x.x / 20`（由 `return_score.py` 算出，含持续性调整） |

### 3. 盈利质量三项（Quality of Earnings）
| `key` | `label` 示例 | 口径与来源要求 |
|---|---|---|
| `qoe_sloan` | Sloan 应计比率 | $(\text{Net Income} - \text{OCF} - \text{ICF}) / \text{Average Total Assets}$，最近财年与 TTM |
| `qoe_cfo_ni` | 经营现金流/净利润（CFO/NI） | 最近三个财年及 TTM 的 $\text{OCF} / \text{GAAP Net Income}$ 比率 |
| `qoe_ccc` | 现金转换周期（CCC） | $\text{DSO} + \text{DIO} - \text{DPO}$（天数及近三年趋势；不适用时写 `"N/A（原因）"`） |

### 4. 最近 8 个报告季度的核心损益序列（`q1` 为最早一季，`q8` 为最新一季；或额外并列具体财季键）
使用 `dalton_sec.get_company_facts`（按 R1–R10 规则，单季必须为 3m，Q4 由 FY − 9M 派生并标 `derived_filing_actual`）：
- **营业收入（Revenue）**：`rev_q1` 至 `rev_q8`（`period` 写清具体财季，如 `1Q25`…`4Q26`）
- **毛利或毛利率（Gross Profit）**：`gp_q1` 至 `gp_q8`
- **营业利润（Operating Income）**：`opinc_q1` 至 `opinc_q8`
- **稀释每股收益（Diluted EPS）**：`eps_q1` 至 `eps_q8`

### 5. 最近 3 个完整财年的自由现金流与股权激励（`fy1`、`fy2`、`fy3`，对应 FY-3、FY-2、FY-1）
- **自由现金流（FCF = OCF − Capex）**：`fcf_fy1`、`fcf_fy2`、`fcf_fy3`
- **股权激励费用（SBC）**：`sbc_fy1`、`sbc_fy2`、`sbc_fy3`（含金额及占当年收入比例）

---

## 四、`facts.md` 完整章节结构

除了第一节的 `FACTS-LEDGER` 账本与表格外，`facts.md` 还可包含以下辅助小节供后续支线查阅：
1. `## 一、机器可读账本（FACTS-LEDGER）`（含 HTML 注释块与渲染表格）
2. `## 二、一手材料清单（最近 4 份定期报告、4 次电话会、招股书、内部人交易摘要）`
3. `## 三、三把财务镜头与盈利质量计算明细`
4. `## 四、分部收入与核心经营指标历史序列（附 filing 表名与页码）`
