# AMZN 跟踪研究与写作规则

每轮先读 `references/common-rules.md`，再读本文件。共用的工作底稿、新闻线索、X、待核实线索、卡片章节和语言步骤在共用规则里。下面是 Amazon 专属的数字、期间和停止条件。

导入档案是 `research/tracker/imported-amzn/thesis.md`，保持原字节。

## 每轮必读的材料

### 第 3 条（AlphaEngine）

AlphaEngine 新文档先看摘要。只挑带新数字的，最多 2 篇读全文。新数字包括盈利预测、AWS 增速与利润率、零售利润率、广告、资本开支（capex）、评级、目标价、买方调查。返回额度、`E_DESKTOP` 或 `E_UPSTREAM_API` 时停止读全文，只用摘要，并在本轮更新里注明。

### 第 4 条（SEC 新公告）

- 8-K 先看条目号（item），`finance(route="sec", operation="eight-k", ticker="AMZN", limit=2)`。
- 财报 8-K 用 `finance(route="sec", operation="attachments", ticker="AMZN", form="8-K")` 列出最新一份的附件，再读 99.1 新闻稿。
- 10-Q 用 `finance(route="sec", operation="search-text", ticker="AMZN", form="10-Q", ...)` 查关键词：`Anthropic`、`OpenAI`、`remaining performance obligations`（剩余履约义务）、`Other income`、`finance leases`（融资租赁）、`severance`（遣散费）。
- 融资文件 S-3ASR、424B2、424B5、FWP，以及 8-K Item 8.01 发债，同样读附件，记下金额、币种、期限和利率。单笔 100 亿美元以上，或新品种（可转债、售后回租、表外特殊目的实体（SPV）），算重要。

### 第 7 条（专家访谈）

专家访谈和管理层交流按 2026-10-15 已确认的合规范围使用，包括自称亚马逊在职人员的专家。例子：「10/8 AWS 顶级咨询伙伴 Caylent 的 CTO」。财报出来后，把专家关于 AWS、广告、资本开支（capex）的说法逐条对一遍，更新「专家来源可靠性」。

## 新闻与 X

本轮两条固定网页查询，以数据包里的记录为准，就是这两条：

- `Amazon analyst estimates price target downgrade upgrade AWS`
- `Amazon AWS Trainium Anthropic capex Kuiper retail news`

账号池里，同时覆盖多家时可以写多家股票代码，适用范围保留 AMZN 或 ALL。

## 是否重要

满足任一条就是 `material=yes`。共用的多空分歧（Debate）、买方仓位、催化剂、X 和股价条款在共用规则里。Amazon 专属的几条是：

- 一致预期：数据包自动触发项里有 FY2026 或 FY2027 的每股收益（EPS）、收入，或平均目标价的阈值触发。FY2026 每股收益（EPS）的跳动先查是不是 Anthropic 重估收益被计入，只是口径变化的不算重要。自动阈值见共用规则。或者主流券商改评级、目标价调整 10% 以上、改 FY2027 或 FY2028 的每股收益（EPS）、AWS 增速或利润率、零售利润率、广告或资本开支（capex）预测，且改动幅度或理由是观点池里没有的。主流券商包括 GS、MS、JPM、BofA、Citi、UBS、Barclays、Jefferies、Evercore、Bernstein、Wells Fargo、Deutsche Bank、Piper、TD Cowen、Oppenheimer、Wolfe、Truist。
- 公司事实：财报、指引、8-K、融资（大额发债、可转债、GPU 售后回租或表外融资）、管理层对资本开支（capex）、AWS、Trainium（自研 AI 芯片）外销、零售利润率的新表态、OpenAI 或 Anthropic 合同条款变化（算力承诺、未完成订单（backlog）、融资额度）、Anthropic 上市文件或定价（决定亚马逊 Q4 投资重估收益）、并购（Decart、Globalstar 进展）、裁员或重组、AWS 定价调整（GPU 预留价、Trainium 价格）、亚马逊与 Meta、OpenAI、Google 的代理式购物接入协议、美国联邦贸易委员会（FTC）等重大法律或监管进展（金额 10 亿美元以上，或改变时间表）。
- 关键经营指标（KPI）越过工作底稿里写的阈值，包括 Q3 AWS 第三方跟踪或买方私下预期（whisper）、AWS 利润率、FY2027 资本开支（capex）新数字、北美利润率、广告增速、未完成订单（backlog）。

### 财报特别轮

下列数字是原底稿里这一轮财报要对照的比较值，保留期间和来源，不要改写成永久的当前数值：

- 先把其他收入（Anthropic 重估等）拆出来，再看经营利润。
- Q3 收入对一致预期 2,021 亿和指引 1,970 亿至 2,020 亿。经营利润对指引 225 亿至 265 亿，以及卖方约 260 亿。
- AWS 增速对卖方约 37%、买方 40%、TD Cowen 41.2%。AWS 利润率对一致预期 37.5%。
- 北美和国际增速要计入 Prime Day 从本季挪到上一季、约 4 个百分点的错位，利润率剔除一次性项目后再比。
- 广告增速对 26%。未完成订单（backlog）对 4,960 亿。
- Q4 收入指引对 2,443 亿，经营利润指引对约 330 亿。
- FY2027 资本开支（capex）说法对一致预期约 2,780 亿和 TD Cowen 约 3,400 亿。Trainium 外销单独写。
- 然后给出 FY2027、FY2028 盈利预测的修正方向。

## 盈利预测怎么拆

亚马逊财年即日历年。数据包里 `0y` 是 FY2026，`+1y` 是 FY2027。自动预期比较看 FY2026 和 FY2027。券商预测和财报特别轮的修正方向还看 FY2027 和 FY2028。

一致每股收益（EPS）是 GAAP。FY2026 含上半年 690.6 亿美元投资重估收益。Q3、Q4、FY2027 的一致预期基本不含。每股收益（EPS）变动先查是否来自 Anthropic 重估收益、关税退款、能源衍生品公允价值或裁员费用，再判断经营面。这四项先分项，不并进经营利润。

盈利预测修正按三层写：收入（AWS、零售、广告）、成本（零售利润率、资本开支（capex）与折旧、利息）、投资收益（Anthropic 重估）。没有变化就写「一致预期未动」。

跟踪模型沿用的常数是：稀释后股本 10.95 十亿股（Q2 稀释股数 109.0 亿股），税率 0.2，其他收入 `0y` 为 69.1 十亿美元、`+1y` 为 0。来源是 2Q26 10-Q（SEC 档案号 accession `0001018724-26-000026`）：上半年其他收入 690.6 亿美元，其中 Anthropic 优先股重估 628 亿美元等；FY2025 10-K 有效税率 19.6%。数据包（Yahoo 口径）的 `0y` 是 FY2026（含上半年 Anthropic 重估收益），`+1y` 是 FY2027。这些常数有上述来源；财报特别轮的比较数字仍按上一节的原底稿期间使用，不互相覆盖。

## 卡片正文

股票代码直接写 AMZN。
