# 净现金双口径计算规范（Net Cash Dual Definitions）

本参考文件定义 Dalton 财务镜头之一：**基础净现金（Basic Net Cash / Definition A）**与**严格净现金（Strict Net Cash / Definition B）**的科目归类、行业扣减、营运资本核查、现金质量评级及隐含经营资产（Implied Operating Assets）计算规则。

---

## 1. 基础净现金（Basic Net Cash / Definition A）

### 1.1 基础现金端（Cash Side — Basic）
纳入资产负债表及附注中明确属于以下类别的项目：
1. **现金及现金等价物（Cash and cash equivalents）**：**含受限资金（Restricted cash）**（受限资金在基础口径先纳入，在严格口径作为“不自由现金”剔除）。
2. **定期存款（Time deposits / Term deposits）**：短期及可随时支取或明确归属司库管理的定期存款。
3. **短期投资 / FVPL / FVOCI 中的准现金部分**：明确属于**银行理财、货币市场基金、国债逆回购、短期国债或其他低风险高流动性金融资产**的部分（须核对附注确认底层性质）。
4. **明确不纳入**：长期股权投资、战略性权益投资、长久期债权投资、缺乏高流动性的非标资产。

### 1.2 基础债务端（Debt Side — Basic）
纳入所有明确有息的融资性负债：
1. **短期有息借款（Short-term borrowings / Current portion of long-term debt）**。
2. **长期有息银行借款（Long-term bank loans）**。
3. **公司债、优先票据、可转换债券（Corporate bonds / Senior notes / Convertible notes）**。
4. **其他明确有息融资工具**。

### 1.3 基础净现金公式
```text
基础现金端合计 (USD) = Σ 纳入的基础现金端科目 (USD)
基础债务端合计 (USD) = Σ 纳入的基础债务端科目 (USD)
基础净现金 (Basic Net Cash / Definition A) = 基础现金端合计 − 基础债务端合计
基础净现金占市值比 (%) = 基础净现金 / 美元市值基准 × 100%
```

---

## 2. 严格净现金（Strict Net Cash / Definition B）

在基础净现金之上，进一步扣减**类债务项目（Quasi-debt items）**和**不自由现金（Non-free cash）**，反映在不影响日常经营履约与监管底线的前提下真正可自由支配的净现金。

### 2.1 按行业与性质的强制扣减项

| 类别 / 行业 | 应扣减项目（从基础净现金中减去） | 经济实质说明 |
|---|---|---|
| **所有行业（不自由现金）** | 受限现金（Restricted cash）、质押存款（Pledged deposits）、监管备付金/客户托管资金 | 产权受限或受监管隔离，公司不能自由用于分红、回购或偿债 |
| **教育行业** | 递延收入（Deferred revenue）、预收学费（Prepaid tuition）、退款负债（Refund liabilities） | 未完成授课时负有履约或退费义务，实质占用预收资金 |
| **医美 / 连锁 / 消费服务** | 会员储值卡余额、合同负债（Contract liabilities）、客户预收款（Advances from customers） | 消费者预付卡/储值未核销余额，属于对客户的刚性履约负债 |
| **制造 / 经销 / 供应链** | 融资性应付票据（Notes payable，特别是银行承兑汇票中具融资性质的部分）、经销商保证金 | 以票据或保证金形式延后付款，实质替代短期有息借款 |
| **电商 / 平台 / 金融科技** | 应付商家结算款、客户备付金（若混入广义现金端）、助贷风险保障金负债 | 属于过手资金或代收代付，非平台自有自由现金 |

### 2.2 逐项列示要求
每一项候选扣减（无论最终 `include` 为 `true` 还是 `false`）都必须逐条列示：
- `item`：财报或附注中的原始科目名称
- `value_local`：报表本币金额
- `value_usd`：按统一锚点汇率换算的美元金额
- `include`：`true`（计入扣减）或 `false`（仅列示不扣减）
- `reason`：一句话说明计入或不计入的商业与会计理由

### 2.3 严格净现金公式
```text
类债务扣减合计 (Quasi-debt Deductions) = Σ (include=true 的类债务项目 USD)
不自由现金扣减合计 (Non-free Cash Deductions) = Σ (受限资金、质押存款等不自由现金 USD)
严格口径总扣减 = 类债务扣减合计 + 不自由现金扣减合计
严格净现金 (Strict Net Cash / Definition B) = 基础净现金 − 严格口径总扣减
严格净现金占市值比 (%) = 严格净现金 / 美元市值基准 × 100%
```

---

## 3. 专项核查与备查规则（强制执行）

### 3.1 “大存大贷”检测（Large Deposit & Large Loan Flag）
- **触发条件**：账面同时存在大额现金/定期存款与大额有息借款（例如有息负债占总现金端比例较高，如 ≥ 30%，且两边绝对金额均占总资产或市值显著比例，或存在存贷利差倒挂）。
- **分析要求**：判断是否为集团内资金归集受限、跨境资金出入境摩擦（境内人民币存款 vs 境外美元债）、质押套利或财务粉饰结构；估算真正可跨主体调用的自由现金，并在输出中显式标注 `large_deposit_loan_flag: true` 及“大存大贷”分析说明。

### 3.2 租赁资产与租赁负债（Lease Disclosure）
- **处理原则**：使用权资产（ROU assets）和租赁负债（Operating / Finance lease liabilities，含流动与非流动部分）**不参与**基础净现金或严格净现金的加减计算。
- **强制列示**：必须在输出 JSON 的 `lease_liabilities_usd` 字段和报告正文中单独列示租赁负债总额（流动 + 非流动），供零售、餐饮、连锁、数据中心等重租赁行业交叉参考。

### 3.3 营运资本与 Other Payable / Other Receivable >20% 预警
1. **营运资本（Working Capital）**：
   ```text
   营运资本 = 流动资产合计 (Total Current Assets) − 流动负债合计 (Total Current Liabilities)
   ```
   必须写明报告期及所包含的主要流动资产/流动负债科目。
2. **20% 聚合科目预警（>20% Threshold Warning）**：
   - 当 **其他应付款（Other Payables / Accrued expenses and other current liabilities）> 流动负债合计的 20%** 时，必须触发预警，提示其中可能隐藏合同负债、融资性应付票据、经销商保证金或递延收入，并核查附注拆细。
   - 当 **其他应收款（Other Receivables / Prepaid expenses and other current assets）> 流动资产合计的 20%** 时，必须触发预警，提示其中可能隐藏大额预付供应商款、押金保证金或关联方资金占用。

---

## 4. 现金质量评级（Cash Quality Rating）

| 评级 | 判定标准 |
|---|---|
| `clean` | 现金头寸清晰透明，受限资金与类债务极少（严格净现金与基础净现金接近），有息杠杆低，无“大存大贷” |
| `moderate` | 存在一定规模的合同负债/递延收入或受限资金，但附注披露充分、分类明确，无异常存贷结构 |
| `complex` | 大量受限/质押资产、显著“大存大贷”、跨境资金池摩擦、大额未拆细 Other Payable/Receivable（>20%）或高杠杆复杂融资结构 |

---

## 5. 隐含经营资产（Implied Operating Assets）勾稽

结合第二把镜头（隐含资产折后净值 `Hidden Assets Net`），计算市场当前对公司核心经营业务的隐含定价：
```text
隐含经营资产（基础口径） = 美元市值 − 基础净现金 − 隐含资产折后净值
隐含经营资产（严格口径） = 美元市值 − 严格净现金 − 隐含资产折后净值
```
当隐含经营资产接近 0 甚至为负时，说明市场给核心主业的估值极低（处于深度净现金/隐含资产折价状态），需结合第三把镜头（股东回报意愿与能力）判断价值能否兑现。
