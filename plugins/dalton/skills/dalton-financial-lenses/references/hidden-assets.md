# 隐含高价值资产计算规范（Hidden Assets）

本参考文件定义 Dalton 财务镜头之二：**非核心隐含资产（Hidden Assets）**的四类纳入范围、默认排除项、估值与折扣率表、与净现金去重规则、重要性评级以及隐含经营资产（Implied Operating Assets）扣除计算。

---

## 1. 纳入范围（A / B / C / D 四类）

只纳入可独立于核心主业之外估值或变现的非核心资产：

| 类别 | JSON `asset_type` | 定义与说明 | 财报与附注线索 |
|---|---|---|---|
| **A. 联营/合营权益** | `associate_jv` | 未并表的联营公司（Associates）或合营企业（Joint Ventures）股权 | `Investments in Associates`, `Equity Method Investments`, `Long Term Equity Investment` 及长期股权投资附注 |
| **B. 上市参股股权** | `listed_equity` | 直接持有的已上市公司股份（战略参股、可供出售金融资产或权益证券投资） | `Marketable Equity Securities`, `Available-for-Sale Securities`, 金融资产附注及 13F/13D/13G 披露 |
| **C. 投资性房地产** | `investment_property` | 非自用的投资性物业、出租楼宇或可独立分割变现的土地储备 | `Investment Properties`, `Land and Improvements`（仅限与日常经营完全可分离、可独立出租或出售的部分） |
| **D. 其他可变现非核心资产** | `other` | 具有公开市场参照价格或独立可观察估值锚的非核心资产 | 独立采矿权/频谱牌照、未计入净现金端的长期 FVPL/FVOCI 基金或私募份额、可交易税收抵免（Transferable tax credits）等 |

### 默认排除项（严禁纳入隐含资产）
- 经营性厂房、机器设备与自用办公楼（Operating PP&E）
- 使用权资产（Right-of-Use / ROU Assets）
- 商誉（Goodwill）
- 普通品牌价值、客户关系、自研软件等无独立变现市场的无形资产
- 缺乏可靠独立估值依据的经营性资源

### 港股与 A 股科目映射及取数核实（`route="cn-hk"`）
1. **初值线索（`operation="ask"`）**：
   - 调用 `researchdata.finance(route="cn-hk", operation="ask", query=f"{code_or_name} 港股资产负债表", limit=50)`（港股）或 `query=f"{code_or_name} 资产负债表"`（A 股），提取长期股权投资（`LONG_EQUITY_INVEST` / 于联营公司及合营企业之投资）、其他权益工具投资、其他非流动金融资产、按公允价值计入损益/其他全面收益之非流动金融资产、投资性房地产（`INVEST_REALESTATE`）等科目初值。
2. **回法定公告附注核实（`operation="filings"` + 披露易退路，必做）**：
   - 必须调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=code_5_or_6, start=..., end=..., limit=20)` 查阅最新年报/中报附注（港股未覆盖时用 `web_search` 查 `site:hkexnews.hk` 原文链接），确认重要的被投上市联营公司名称、持股比例、账面价值及公允价值披露，并与第一把镜头已纳入净现金端的短期银行理财/货币基金严格去重。
3. **跨市场被投股权估值与币种换算**：
   - 当被投上市公司在 A 股或港股上市且账面值 ≥ 目标公司美元市值的 0.5% 时，可通过 `researchdata.finance(route="cn-hk", operation="ask", query=f"{被投公司代码或简称} 行情", limit=5)` 或 `yfinance` 获取最新市值（注意核对复权与总股本口径），按被投公司交易币种汇率折算为美元后乘以持股比例作为折前估值（`gross_estimated_value_usd`），再按 §3.2 标准折扣率打折。

---

## 2. 与净现金（第一把镜头）去重硬规则

**严禁双重计入（No Double Counting）**：
- 凡在第一把镜头（`netcash`）基础现金端已经纳入的**短期理财、货币基金、短期国债、高流动性 FVPL/FVOCI 金融资产**，在隐含资产镜头中**一律不得再次计入**。
- 只有**未计入净现金端**的中长期权益投资、联营合营股权、非流动性 FVPL/FVOCI 投资、投资性房地产等，才能进入隐含资产清单，且必须在 `note` 中写明“未计入净现金现金端，已去重”。

---

## 3. 估值方法与折扣率表（Haircut Table）

### 3.1 估值基准（`valuation_basis`）
1. **上市参股股权（Category B）与上市联营公司（Category A 上市部分）**：
   - 必须使用**分析当日被投公司的最新公开市值 × 持股比例（或持股数 × 最新股价）**进行市价穿透。
2. **非上市联营/合营或私募股权**：
   - 若有近期公允融资估值、可比交易或公司在 FVPL 附注中持续更新公允价值，可按该基准作为折前估值并给予较高折扣；
   - **若非上市且无公开市场锚点，严禁强行编造估值**——应在 `valuation_basis` 中写明“无可靠独立估值依据”，或仅按账面值保守列示并注明局限。
3. **0.5% 效率阈值规则（强制）**：
   - 若某项非核心资产的**账面值 < 当前美元市值的 0.5%**，跳过逐项联网查价，直接采用**账面值（Book Value）**作为折前估值，并应用对应类别的标准折扣率。
   - 对**账面值 ≥ 当前美元市值 0.5%** 的项目，必须核查附注及最新市场价格。

### 3.2 标准折扣率参考表（Haircut Guidelines）

| 资产类型 | 典型折扣率（`haircut_pct`） | 折扣考量因素（`haircut_rationale`） | 兑现难度（`monetization_difficulty`） |
|---|---|---|---|
| **上市股权（可自由交易、无锁定）** | **10% – 20%** | 大宗交易折让、潜在资本利得税负 | `low` |
| **上市股权（有锁定期 / 大股东控制权限制）** | **20% – 40%** | 锁定期限制、战略协同绑定、二级市场大额减持冲击与税负 | `medium` |
| **联营/合营（被投资方已上市）** | **20% – 30%** | 间接持有架构、集团控股折价（Conglomerate discount）、处置需董事会配合 | `medium` |
| **投资性房地产（可独立分离）** | **20% – 40%** | 大宗物业流动性差、交易税费高、商业地产景气度折让 | `medium` – `high` |
| **联营/合营（被投资方非上市）** | **30% – 50%** | 缺乏公开价格发现、信息不对称、退出依赖并购或 IPO 审批 | `high` |
| **其他可变现非核心资产（Category D）** | **按具体条款（通常 20% – 50%）** | 视二级流转市场深度、监管审批与变现折价而定 | `medium` – `high` |

### 3.3 单项与汇总计算公式
```text
单项折后估值 (net_estimated_value_usd) = 折前估值 (gross_estimated_value_usd) × (1 − haircut_pct / 100)
单项占市值比 (net_value_to_market_cap_pct) = net_estimated_value_usd / 美元市值基准 × 100%

隐含资产折前合计 (total_gross_usd) = Σ gross_estimated_value_usd
隐含资产折后合计 (total_net_usd) = Σ net_estimated_value_usd
隐含资产折后占市值比 (total_net_to_market_cap_pct) = total_net_usd / 美元市值基准 × 100%
```

---

## 4. 汇总重要性评级（Assessment）

| 评级（`assessment`） | 判定标准（`total_net_to_market_cap_pct`） |
|---|---|
| `significant` | 隐含资产折后合计 > 市值的 **10%** |
| `moderate` | 隐含资产折后合计占市值的 **3% – 10%**（含 3%，不含 10%） |
| `minimal` | 隐含资产折后合计 > 0 且 < 市值的 **3%** |
| `none` | 无明显隐含资产（`hidden_assets: []`，备注“无明显隐含资产”） |

---

## 5. 隐含经营资产（Implied Operating Assets）计算

将净现金与隐含资产折后净值同时从市值中剥离，衡量市场赋予核心主业的隐含估值（用于 DCF Equity Bridge 与安全边际检验）：
```text
Implied Operating Assets (Strict) = Market Cap − Strict Net Cash − Hidden Assets (after haircut)
Implied Operating Assets (Basic)  = Market Cap − Basic Net Cash  − Hidden Assets (after haircut)
```
在 `lenses.json` 和报告中同时列示金额与占市值比例。
