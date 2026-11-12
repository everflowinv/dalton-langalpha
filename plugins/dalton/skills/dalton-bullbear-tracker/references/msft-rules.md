# MSFT 跟踪研究与写作规则

每轮先读 `references/common-rules.md`，再读本文件。共用的工作底稿、新闻线索、X、待核实线索、卡片章节和语言步骤在共用规则里。下面是微软专属的数字、期间和停止条件。

导入档案是 `research/tracker/imported-msft/thesis.md`，保持原字节。

## 每轮必读的材料

### 第 3 条（AlphaEngine）

AlphaEngine 新文档先看摘要。只挑带新数字的，最多两篇读全文。新数字包括盈利预测、Azure、M365、Copilot 增速、资本开支（capex）、评级、目标价、买方调查。返回额度、`E_DESKTOP` 或 `E_UPSTREAM_API` 时停止读全文，只用摘要，并在本轮更新里注明。

### 第 4 条（SEC 新公告）

- 8-K 先看条目号（item），`finance(route="sec", operation="eight-k", ticker="MSFT", limit=2)`。
- 财报 8-K 用 `finance(route="sec", operation="attachments", ticker="MSFT", form="8-K")` 列出最新一份的附件，再读 99.1 新闻稿。
- 10-Q 用 `finance(route="sec", operation="search-text", ...)` 查关键词：`Anthropic`、`remaining performance obligation`（剩余履约义务）、`finance leases`（融资租赁）、`severance`（遣散费）。
- 融资文件 S-3ASR、424B2、424B5、FWP 同样读附件，记下金额、品种和利率。微软在本轮周期还没发过债，首次发债算重要。

### 第 7 条（专家访谈）

专家访谈和管理层交流按 2026-10-15 已确认的合规范围使用，包括自称微软在职人员的专家。例子：「10/15 自称微软 Azure AI 业务、负责公司战略和预算的专家」。财报出来后，把专家关于 Azure、Copilot 席位、资本开支（capex）的说法逐条对一遍，更新「专家来源可靠性」。

## 新闻与 X

本轮两条固定网页查询，以数据包里的记录为准，就是这两条：

- `Microsoft analyst estimates price target downgrade upgrade Azure`
- `Microsoft OpenAI Copilot capex datacenter Anthropic news`

账号池里，同时覆盖多家时可以写 `MSFT,META,GOOGL` 这类股票代码，适用范围保留 MSFT 或 ALL。

## 是否重要

满足任一条就是 `material=yes`。共用的多空分歧（Debate）、买方仓位、催化剂、X 和股价条款在共用规则里。微软专属的几条是：

- 一致预期：数据包自动触发项里有 FY27 或 FY28 的每股收益（EPS）、收入，或平均目标价的阈值触发。自动阈值见共用规则。或者主流券商改评级、目标价调整 10% 以上、改 FY27 或 FY28 每股收益（EPS）、Azure 增速、M365、Copilot 或资本开支（capex）预测，且改动幅度或理由是观点池里没有的。主流券商包括 GS、MS、JPM、BofA、Citi、UBS、Barclays、Jefferies、Evercore、Bernstein、Wells Fargo、Deutsche Bank、Piper、TD Cowen、Oppenheimer、Stifel。
- 公司事实：财报、指引、8-K、融资（首次发债、可转债、大额租赁或表外融资）、管理层对资本开支（capex）、Azure、Copilot、Maia（微软自研 AI 芯片）的新表态、OpenAI 或 Anthropic 合同条款变化（分成、独家、剩余履约义务（RPO）、算力承诺）、Anthropic 上市文件或定价、裁员或重组公告、重大法律或监管进展（金额 10 亿美元以上，或改变时间表）。Anthropic 上市文件或定价会决定微软 FQ2 投资收益。
- 关键经营指标（KPI）越过工作底稿里写的阈值，包括 FQ1 Azure 第三方跟踪或买方私下预期（whisper）、Copilot 席位、FY27 资本开支（capex）新数字、剔除 OpenAI 的剩余履约义务（RPO）增速。

### 财报特别轮

下列数字是原底稿里这一轮财报要对照的比较值，保留期间和来源，不要改写成永久的当前数值：

- 实际收入和经营利润对一致预期：FQ1 收入 907.1 亿、每股收益（EPS）4.72；公司指引中值约 904 亿、每股收益（EPS）约 4.70。先拆出 Anthropic 等投资收益和裁员费用，再写超没超。
- Azure 增速对指引 44% 至 45%、买方约 47%、专家约 45%。
- FQ2 Azure 指引对 47%。
- M365 商业云对 17%。Copilot 付费席位对 4,000 万，同时看 M365 E7 套餐档位和按用量计费收入。
- Microsoft Cloud 毛利率对 65%。
- FQ1 资本开支（capex）对 500 亿以上，以及对 FY27 的三档说法：2,200 亿、2,400 亿、2,600 亿。
- 剔除 OpenAI 的剩余履约义务（RPO）增速对 25%。
- 然后给出 FY27、FY28 盈利预测的修正方向。

## 盈利预测怎么拆

微软财年是 7 月至次年 6 月。数据包里 `0y` 是 FY27（止于 2027-06），`+1y` 是 FY28。判断盈利预测修正时两年都看。

一致每股收益（EPS）是 non-GAAP，只剔除 OpenAI。Anthropic 投资收益和裁员费用都在每股收益（EPS）里面。每股收益（EPS）变动时先查是不是来自这两项，两项分开识别。

盈利预测修正按三层写：收入（Azure、M365 Copilot、消费端）、成本（毛利率、资本开支（capex）与折旧、裁员费用）、投资收益（Anthropic 等）。写明哪一层动了，以及隐含费用空间的变化。没有变化就写「一致预期未动」。

财务模型假设来自 FY26 Q4 8-K 附件 99.1（SEC 档案号 accession `0001193125-26-323632`）：non-GAAP 净利润 353 亿美元、每股收益（EPS）4.74，约 74.5 亿股。FY27 Q1 指引税率约 20%，其他收入约每季 -1 亿美元（不含 OpenAI）。跟踪模型沿用的常数是：稀释后股本 7.45 十亿股（74.5 亿股），税率 0.2，其他收入 FY27 与 FY28 各 -4 亿美元。这些常数有上述来源；财报特别轮的比较数字仍按上一节的原底稿期间使用，不互相覆盖。

## 卡片正文

股票代码直接写 MSFT。
