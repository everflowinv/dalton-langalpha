---
name: dalton-ai-app-weekly-monitor
description: "AI 应用周度监控：补库与变化周报，四表筛选、原话与专家口径入库，飞书成稿并提交文字摘要。"
---

# AI 应用行业周度监控

无人值守。不提问。不派子任务。不调用 `message`。本轮只做自动化指令写明的那一种模式。

两种模式由自动化指令写明，技能不自行选择。接收方在 `metadata.report_profile`，配置名是 `ai-app-weekly`。技能正文和工具参数都不写接收方。

安装路径是 `.agents/skills/dalton-ai-app-weekly-monitor/`。命令用 `scripts/ai_application_weekly_monitor_cli.py`。每条命令都带 `--workspace-dir <workspace>` 和 `--json`。`<workspace>` 是本轮工作区根目录。沙箱 Python 已有 `yfinance` 与 `pandas`，直接运行，不要自建虚拟环境，不要安装包。

`state/ai_app_db/` 中的事实库已预先就位，直接读写，不要运行 `migrate-db --apply`。

自行撰写的标题、正文、摘要、日志和最终回复遵守文末撰写硬规则。逐字引文、原始转写、链接、代码、命令和固定协议字段保持原样。

## 模式

- **补库**（每周二 16:00 Asia/Hong_Kong，模型 `gpt-6.1-sol-oauth`）：只入库，不写周报。绑定的报告配置 `ai-app-weekly` 只用于失败告警。成功时不调用 `automation_report`，最终回复只写 `NO_REPLY`。`self-test` 失败或流程中断时，调用 `automation_report(failure_text=...)`，`report_run_id` 为 `ai-app-db-refresh-<tuesday>`（`<tuesday>` 为 Asia/Hong_Kong 当周周二日期，格式 `YYYY-MM-DD`）。
- **周报**（每周五 15:00 Asia/Hong_Kong，模型 `gpt-6-astra-oauth`）：按下面的周报步骤生成变化周报。`report_run_id` 为 `ai-app-weekly-<friday>`（`<friday>` 为 Asia/Hong_Kong 当周周五日期，格式 `YYYY-MM-DD`）。

时区 Asia/Hong_Kong。周报窗口是上周五 15:00 至本周五 15:00。周四美股盘后（等于周五 04:00）在窗口内。周五美股盘前与盘后滚入下一周。补库窗口是上周五 15:00 至本周二运行时刻。周二 16:00 已经入库的公司，周五只补周二 16:00 之后新出业绩的公司。

## 状态路径

都相对工作区。不要写绝对路径，不要用上级目录，不要把这些路径做成符号链接。下文路径、命令和参数中的 `2026-11-10`（周二）、`2026-11-13`（周五）及 `2026-11-06`（上周五）均为示例日期，实际运行时替换为当周对应日期（`YYYY-MM-DD`）。

- 事实库：`state/ai_app_db/`
- 本周应更新清单：`state/ai-app-weekly/temp/ai-app-weekly/due-2026-11-13.json`
- 周报日志：`state/ai-app-weekly/temp/ai-app-weekly/log-2026-11-13.md`
- 补库日志：`state/ai-app-weekly/temp/ai-app-db-refresh/log-2026-11-10.md`
- 更正前备份：`state/ai-app-weekly/temp/ai-app-backfill/`
- 周报草稿：`state/ai-app-weekly/temp/ai-app-weekly/report-2026-11-13.md`
- 飞书工具返回：`state/ai-app-weekly/runs/2026-11-13/feishu-return.json`
- 飞书文档记录：`state/ai-app-weekly/runs/2026-11-13/doc.json`
- 飞书失败记录：`state/ai-app-weekly/runs/2026-11-13/doc.failed.json`
- 归档：`research-outputs/ai-application-weekly/2026-11-13.md`
- 快照：`state/ai_app_db/snapshots/2026-11-13.json`

已有记录只追加。发现口径归类错误、重复记录或来源不是一手件时，先把原文件备份到 `state/ai-app-weekly/temp/ai-app-backfill/`，再在新记录的 note 里写明修正理由。不要改技能目录里的文件。不要调用 `retier --apply`。

## 研究口径

每周回答两件事。

**发现**：这一周有没有新的 AI 变现方式、新的 AI 产品形态，或某家公司的 AI 使用与销售趋势明显起量。

**筛选**：哪些公司的 AI 收入或 AI 使用量增速已经高到市场必须重新定价，并且市场还没反应过来。

两条通道，缺一条会漏掉一整类公司：

- **通道一 披露型**：公司把 AI 拆出来单列，直接比 AI 分项数字的增速。
- **通道二 内嵌型**：公司不单列 AI 分项，增长明显被 AI 推动。判据是公司级总收入同比增速的变化、管理层归因原话，以及可跟踪的 AI 代理量。

两条通道分开排序。内嵌型公司的总收入增速到不了三位数，和通道一放在同一张名次表里会被压到看不见。

只跑通道一会得到一张有系统性偏差的榜：能进榜的是愿意也方便把 AI 拆出来的公司，通常是小市值、AI 占收入比重高的标的。Cloudflare 这类把 AI 织进主产品、靠智能体（agent）流量推高主业用量的公司不会出现在这张榜上。两张榜一起看，表不混排。

三条赛道：

- `toc_internet` ToC（面向消费者）互联网：面向消费者的 AI 产品与变现
- `tob_software` ToB（面向企业）软件：面向企业的 AI 产品、智能体与订阅
- `ai_for_science` AI for Science（科学智能）：制药、材料、生命科学、EDA（电子设计自动化）与科研工具

覆盖池在 `.agents/skills/dalton-ai-app-weekly-monitor/references/universe.json`，共 290 家。加减公司只改这个文件，不动代码。每家标有业务纯度 `ai_purity`（`native` 为核心产品即 AI 交付，`embedded` 为 AI 内嵌于主产品，`adjacent` 为 AI 仅属边缘驱动）。`scan_tier` 1 是核心池，每周完整跑五条信源。`scan_tier` 2 是观察池，本周有财报、重大公告或 AI 关键词命中才进流程，否则在报告里记一行「本周无事件」。`scan_tier` 是扫描频率，和 `screen` 输出里的增速分档 `tier` 是两件事。

`scan_tier` 由 `retier` 按五条规则计算：通道一已有 A/B 级 AI 分项、`ai_purity` 为 native、有 A/B 级归因原话、公司级总收入同比达到门槛且同比增速较对比期未回落、显式钉住的行业读数设定者。命中任一条进核心池。两池家数每次重算都会变，本文不写死。要当期数字跑 `universe`。不带 `--apply` 的 `retier` 只试算。`retier --apply` 会被拒绝；覆盖池调整改仓库里的 `references/universe.json`。

四表不混排。表一 AI Monetization（AI 分项变现）只排 AI 分项收入、ARR（年化经常性收入）、bookings（新签订单额）、ACV（年度合同价值）。表二按 AI 占总收入比重的水平排，不按增速排。表三 Headline Monetization（公司级整体收入增速）看公司级总收入，分档视图和绝对增速榜都要写。表四 Usage（个性化用量）只分组，不排名次。门槛、证据等级和口径纪律见 `.agents/skills/dalton-ai-app-weekly-monitor/references/signal-taxonomy.md` 与 `.agents/skills/dalton-ai-app-weekly-monitor/references/thresholds.md`。数据库分片、原话收录判据和专家层见 `.agents/skills/dalton-ai-app-weekly-monitor/references/database-contract.md`。

### 引擎 A 发现层

输入是新闻、公告、产品页、定价页、专家访谈、研报。按信号分类的七类记录：新变现方式、新产品形态、使用与渗透趋势、单位经济、客户侧证据、组织与资源信号、反向信号。

### 引擎 B 筛选层

输入是 `state/ai_app_db/companies/<TICKER>/metrics.jsonl`。脚本不联网取基本面，不推算未披露数字。先 `add-metric` 入库，再 `screen`。事实库跨周累积，第 N 周可以算出第 N-1 周还算不出的环比和增速变化。

### 引擎 C 内嵌层

输入是 `state/ai_app_db/companies/<TICKER>/fundamentals.jsonl`。

- `kind: fundamental`（公司基本面）：公司级总收入、公司披露的总收入同比、cRPO（当前剩余履约义务）/ RPO（剩余履约义务）同比、净留存率（NRR）、大客户数。
- `kind: attribution`（归因原话）：管理层把增长或下滑明确归到 AI / 智能体上的原话，带证据等级和一手链接。

总收入同比直接抄业绩新闻稿里的 “up X% year-over-year”。XBRL（财报结构化数据）里 Q2、Q3 常常是累计口径，脚本自己拆季度相减容易算错，抄公司写明的那个百分比。

重大并表或剥离时打 `--includes-ma`，`--ma-note` 写事由和完成日期。公司给了 pro-forma（备考合并口径）或剔除并表口径时再补 `--organic-yoy-pct`，只抄披露，不自己估。判据是本期收入里含上年同期没有的实体，且影响大到能改变分档。并表完成当季起连续四期都要打标，直到上年同期基数也含这块业务。

打了标又没有同口径同比的，表三放进口径不可比档：不判加速，不进绝对增速榜，`retier` 不据此升池。对比基期跨并表导致增速变化不可比时，只锁增速变化那一格；本期总收入同比 ≥ 50% 仍进高增档和绝对增速榜，否则归入口径不可比档。没打标的旧记录按 as-reported（财报原始列报口径）走。

Coursera 2026-08-31 的教训：2026Q2 as-reported 同比 +59.6%、较上期抬升 50.6 个百分点，机械结果会进加速档和绝对增速榜，管理层还有 GenAI 归因原话。那 50 个百分点来自 2026-05-11 完成的 Udemy 合并，同口径接近零增长。

`add-attribution` 的 `--quote` 必填。没有管理层原话时，公司级加速只记加速；AI 信号等原话入库后再判。

### 证据等级与 AI 归属

- **A**：财报、公告或申报文件里的分项数字，有会计口径。
- **B**：业绩会或投资者会口头给出，有基数、期间和定义。
- **C**：专家、渠道或第三方估算。可进报告，必须标估算。
- **D**：无口径转述。丢弃。

只有 A 和 B 进筛选主表。C 可以进报告，只放拐点观察。

财务与比率指标必填 `--ai-basis`，四选一：

- `company_ai_line`：公司在披露里把这条收入直接命名为 AI 业务或 AI 分项。
- `company_ai_related`：公司用「AI 相关 / AI 驱动 / AI+X / 与他项合并」这类措辞，边界由公司划。
- `analyst_ai_product`：公司披露的是一条具名产品线且该产品本身即 AI 交付，公司未称其为 AI 收入。
- `analyst_related`：公司只披露业务分部或泛口径，相关性由本系统判断后挂到 AI 收入。

后两档 `judged_by=system`。判定看公司口径原文有没有自称 AI。业务实质有多依赖 AI 由 `ai_purity` 单独记，两者不互相替代。

### 增速、规模与币种

- 爆发档：同比 ≥ 300%，且通过规模闸门。
- 高增档：同比 ≥ 100% 或环比 ≥ 30%，且通过规模闸门。
- 拐点观察：财务未验证，非财务代理指标同比 ≥ 200%。
- 小基数：增速达标但规模未过闸门，单独列，不进主表。
- 口径存疑：口径变更、关联方、一次性项目制、C 级估算。
- 降速：环比连续两季下滑。

环比 ≥ 200% 标「垂直起飞」。最新一期环比比上一期高 5 个百分点以上标「加速」，低 5 个百分点以上标「减速」。环比主门槛是 30%。环比 200% 连续四季等于一年 81 倍，只在从零起步的第一年出现，那时会卡在规模闸门上，所以 200% 是附加标记，入档看 30%。

规模闸门任一条即过：单季 AI 收入 ≥ 2500 万美元；AI ARR ≥ 1 亿美元；AI 收入占总收入 ≥ 5%；AI 月活 ≥ 1000 万，或付费 AI 用户 ≥ 50 万，或付费企业客户 ≥ 300 家，或日调用 ≥ 1 亿次。ARR 只走 1 亿美元那条，不借单季 2500 万过闸。`ai_arr` 记录的金额本身是年化值。财务指标入库缺规模字段时，CLI 拒绝写入。

排序、规模闸门和规模列只用美元字段 `value_usd_mn` / `arr_usd_mn` / `total_revenue_usd_mn`。原币留在 `value` 加 `unit`，前缀写成 `CNY_mn` / `HKD_mn` / `USD_mn`。固定汇率 CNY 7.10、HKD 7.80，只为规模可比，不还原期间汇率。币种不在汇率表内的记 `unconvertible`，不猜、不参与美元排序。通道二原币从 `--currency` 和 `--total-revenue-orig-mn` 取。「—」只留给确认以美元列报的公司。

细则在 `.agents/skills/dalton-ai-app-weekly-monitor/references/thresholds.md` 与 `.agents/skills/dalton-ai-app-weekly-monitor/references/signal-taxonomy.md`。

### 原话与 Guidepoint

原话按五类收录判据（`--anchor`）入库：`product`（具体 AI 产品或功能）、`workflow`（嵌入的业务环节或客户群）、`result`（定量或定性业务结果）、`customer`（具体客户或部署案例）、`commitment`（带时间表、规模或定价细节的承诺）。只有立场、没有对象的句子不入库。结果为负或为零同样入库。

Guidepoint（专家访谈库）一周只做三次原子检索，词不堆进一条 query：

1. 企业 AI 预算与采购：`seat expansion`（席位扩充）、`usage-based pricing`（按用量计费）、`renewal`（续约）、`budget reallocation`（预算重分配）。
2. 消费级 AI 订阅：`conversion`（付费转化）、`retention`（留存）、`willingness to pay`（付费意愿）、`churn`（流失）。
3. AI for Science：`computational chemistry`（计算化学）、`drug discovery platform`（药物发现平台）、`customer spend`（客户支出）、`validation rate`（验证通过率）。

只收本周完成并发布的访谈。以 transcript id 去重。剔除只提到 AI、与三条赛道无关的结果。每份访谈最多引用一段、不超过 20 个英文词，紧跟 `source_attribution.markdown`。专家观点与已核实事实分开写。能对应到覆盖池公司的，用 `add-expert` 入库并在 `--alignment` 中标明与同期管理层表述的关系（`confirms` 印证 / `contradicts` 矛盾 / `extends` 补充 / `unrelated` 无关 / `no_management_claim` 无可比管理层表述）。判为 `contradicts`（矛盾）时必须填 `--management-ref`，指明对应的管理层原话。如果不入库、只写进当周周报正文，后续导出深度报告时将无法检索到该条记录。泛行业、对不到具体公司的留在正文。没有新增时写「本周 Guidepoint 无相关新增访谈」。

Guidepoint 只返回摘录，没有全文，也没有翻页游标。`ok: false` 是调用失败（`error` 为参数错误、上游不可用、权限或额度、没有匹配这四类之一），把它当成「没有数据」会漏记故障。真正没有命中是 `ok: true` 且结果为空，并带 `empty_reason`。`params.recency_bias` 是排序权重，从 30 起，不是日期窗口。`start` / `end` 才是 UTC 日期过滤。

### 信源顺序

第一层：财报、业绩快报、业绩会、港交所披露易、巨潮、公司 IR（投资者关系网站）、SEC 8-K。SEC 用 `finance`，不用 yfinance 的季度财务替代一手文件。yfinance 只给周报里的 3 个月和 12 个月涨跌幅。

第二层：AlphaEngine 先 `search_library` 再 `get_document`。业绩会纪要走 `research(route="transcript")`。`complete` 为 false 时，续查回到 `research(route="alphaengine", operation="search_library")`，不要再传回 transcript。

第三层：Sensor Tower、七麦、QuestMobile、Similarweb、应用商店榜。X 用直接工具 `x_search`：先用 `handles`（最多 20 个，可带或不带 `@`）建固定账号底表，再用一次主题与媒体检索补充。日期放在 `from_date` 和 `to_date`（`YYYY-MM-DD`），账号放在 `handles`，不要写进 `query`。按原帖 URL 去重。只有带 URL 的帖子可以引用；摘要没有来源帖子时记 `no_evidence`。Reddit 当前未覆盖。未检索到的社区内容按未覆盖处理，不要把未检索写成不存在。不要调用 `xreach`。不要使用 firecrawl。时间不够可以跳过，报告末尾写明跳过了哪一层。第三层只作发现和拐点观察，不作为收入、用户数、增速的一手来源。

### 本周结论前先回答

写「本周结论」之前先回答这六件，答案融进正文：有没有此前没有的变现方式；有没有公司第一次披露 AI 分项；上期拐点观察本期被验证还是被证伪；有没有公司口径发生变化；有没有反向证据说明 AI 在侵蚀原有收入；下周哪些财报和发布会会给出新数字。

正文最前面 3 句以内给出本周判断。每条先写发生了什么，再写为什么重要，最后写证据与限制。数字紧跟口径、证据等级、来源和日期。关键数字至少两个独立来源交叉验证，优先公司侧一手来源。演示标「演示级证据」，订单标「订单，尚非收入」，累计数标「累计口径」。未披露写「未披露」，不推算。不用「大幅增长」「显著提升」。不用 Markdown 表格。事实与判断分开。没有高质量增量时报告可以很短。

全量回填与覆盖池维护见 `.agents/skills/dalton-ai-app-weekly-monitor/references/backfill-protocol.md` 和 `.agents/skills/dalton-ai-app-weekly-monitor/references/universe-maintenance.md`。窗口是 2025Q1 至最近一个已经过了 45 天的完整季度。

## 取数

`research` 与 `finance` 只在 ExecuteCode 里调用：`from tools.researchdata import research`，`from tools.researchdata import finance`。`web_search`、`x_search`、`feishu_create_doc`、`automation_report` 是直接工具。

`web_search` 只传 `query`、`start_date`、`end_date`。日期用 `YYYY-MM-DD` 的具体日子。不要传 `date_after`、`date_before`、`freshness`、`country`、`language`、`domain_filter`、`count`、`time_range`、`topic`。

```python
web_search(
    query="Cloudflare earnings 8-K",
    start_date="2026-11-07",
    end_date="2026-11-13",
)
```

```python
x_search(
    query="Cloudflare AI revenue",
    from_date="2026-11-07",
    to_date="2026-11-13",
    handles=["Cloudflare"],
)
```

```python
from tools.researchdata import finance

finance(
    route="sec",
    operation="list-filings",
    ticker="NET",
    form="8-K",
)
```

`form` 必须写在顶层。不写时网关默认 10-Q。

```python
from tools.researchdata import research

research(
    route="alphaengine",
    operation="search_library",
    query="Cloudflare AI revenue",
    start="2026-11-07",
    end="2026-11-13",
    limit=20,
    params={"search_in": "title", "sort": "relevance"},
)
research(
    route="alphaengine",
    operation="get_document",
    params={
        "doc_id": "290001790954279_inst_134249",
        "content_field": "original_text",
        "offset": 0,
        "max_chars": 30000,
    },
)
research(
    route="guidepoint",
    operation="search_library",
    query="Enterprise adoption and budget for AI agents seat expansion usage-based pricing renewal",
    start="2026-11-07",
    end="2026-11-13",
    limit=10,
    params={"recency_bias": 30},
)
research(
    route="transcript",
    operation="transcript",
    ticker="NET",
    query="Cloudflare",
    start="2026-11-07",
    end="2026-11-13",
    limit=4,
)
```

另外两次 Guidepoint 检索分别使用「原话与 Guidepoint」一节的第 2、3 组英文检索词，日期窗口同上。

## 补库步骤

0. 先读本文件的研究口径和命令，再跑 `self-test`。`self-test` 失败或流程中途中断时立即停止，调用 `automation_report` 提交 `failure_text`（不超过 2000 字），最终回复只写 `NO_REPLY`。成功时不调用 `automation_report`。

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json self-test --workspace-dir <workspace>
```

1. 窗口：上周五 15:00 Asia/Hong_Kong 至现在。读覆盖池，对照每家最新 `fundamentals.jsonl` 的 `period` 与 `source_date`，列出窗口内已发布业绩、库里还没有该期记录的公司。发布证据用 `web_search`，港股看披露易，A 股看巨潮，美股看公司 IR（投资者关系网站）或 SEC 8-K。找不到发布证据的公司不入库、不 `mark-gap`，日志写「未出业绩」。不要使用 firecrawl。
2. 期间标签：美股按财季截止日所在的日历季度，7 月至 9 月截止写 `2026Q3`，4 月至 6 月截止写 `2026Q2`。港股中期写 `2026H1`，全年写 `2026FY`。A 股半年报先看该公司 2025 年记录用的是 `2025Q2` 那种 Q2 还是 H1，保持一致。
3. 每家至少一条 `add-fundamental`：非美元列报同时给 `--currency`、`--total-revenue-orig-mn`、`--total-revenue-usd-mn`。同比抄公告。有则加 cRPO（当前剩余履约义务）、RPO（剩余履约义务）、净留存率（NRR）、大客户数。`--quote` 抄含该数字的原句。并表打 `--includes-ma` 与 `--ma-note`。公告里单列的 AI 收入、ARR（年化经常性收入）、用户或调用量，每个数字一条 `add-metric`，财务与比率指标必填 `--ai-basis`。管理层把增长或下滑归到 AI 上的原话用 `add-attribution`。没有原话不推定。
4. 每处理完 10 家，把股票代码（ticker）和记录数追加写入 `state/ai-app-weekly/temp/ai-app-db-refresh/log-2026-11-10.md`。
5. `reconcile --expected` 使用本次应更新的股票代码（ticker）。未交代的要么补数据，要么 `mark-gap`，reason 用真实原因，并写 `--checked-source`。然后 `coverage-ledger --write`。结果只写日志：窗口、应更新家数、入库家数与记录数、mark-gap 名单、未出业绩家数。
6. 不派子任务，不写周报，不改技能文件，不改已有记录。成功则最终回复只写 `NO_REPLY`。

自检失败或流程中断时，用报告配置 `ai-app-weekly` 提交失败告警：

```python
automation_report(
    action="submit",
    report_run_id="ai-app-db-refresh-2026-11-10",
    material="yes",
    failure_text="AI 应用补库失败：self-test 未通过",
)
```

流程中断时把 `failure_text` 换成具体中断原因，例如 `AI 应用补库失败：流程中断`。返回确认发出后，最终回复只写 `NO_REPLY`。

## 周报步骤

0. self-test。失败则停止。不建文档，不写快照。按补库同样的失败规则提交 `failure_text`，但 `report_run_id` 用 `ai-app-weekly-2026-11-13`。
1. `plan` 取窗口和覆盖池。本周应更新清单写入 `state/ai-app-weekly/temp/ai-app-weekly/due-2026-11-13.json`，内容是 JSON 数组。
2. 只补周二 16:00 之后新出业绩的公司。入库规则与补库相同。AlphaEngine 原话按收录判据 `add-quote`。Guidepoint 三次检索的命中用 `add-expert`。第三层（含 `x_search`）可以跳过，报告末尾写明跳过了哪一层。不要使用 firecrawl。每处理完 10 家，把股票代码（ticker）和记录数追加写入 `state/ai-app-weekly/temp/ai-app-weekly/log-2026-11-13.md`。
3. `reconcile --expected-file` 对账。未交代清零之前不发报告。然后 `coverage-ledger --write`。
4. `diff`、`screen --with-price`、`ai-monetization --by yoy --with-price`、`headline-monetization --with-price`、`usage`。数字只来自本轮工具输出或事实库。
5. 关键数字至少两个独立来源交叉验证，优先公司侧一手来源。按 `.agents/skills/dalton-ai-app-weekly-monitor/templates/weekly-report.md` 写变化周报，落到 `state/ai-app-weekly/temp/ai-app-weekly/report-2026-11-13.md`。本周结论和本周变化写全。四表只保留有变化的行，没有变化就一行「本期无变化」。发现层只写本周发生的事。不用「大幅增长」「显著提升」。不用 Markdown 表格。时间不够而跳过的层，写在报告末尾。
6. 若 diff 无变化，且本周没有新入库：只发 5 行以内短消息（窗口、应更新家数、入库家数、本周无变化、下周催化剂）。不建飞书文档，不归档。短消息仍用 `automation_report` 的 `text` 提交。确认提交成功（`ok` 为 true，`delivery_confirmed` 为 true，`status` 为 `sent`，`content_kind` 为 `text`，`report_run_id` 相同）后，运行 `snapshot --write --no-doc`。这个参数只用于这条路径，并且只在该期既没有 `doc.json` 也没有 `doc.failed.json`（检查结果为 `doc_missing`）时写入。已有未完成正文、损坏记录或失败记录时，命令照样拒绝。不运行 `archive-report`。
7. 有变化时 `validate`。通过后才建文档。
8. `doc-status` 的 `create` 为 true 时调用一次 `feishu_create_doc`。返回原样写入 `state/ai-app-weekly/runs/2026-11-13/feishu-return.json`，再 `save-doc`。`doc.json` 已存在就不再建，也不覆盖。
   - 返回 `ok` 为 true，且有可用 `document_id` 或 `node_token`：写入 `doc.json`，之后 `create` 为 false。有 `node_token` 时链接是 `https://feishu.cn/wiki/<node_token>`，否则是 `https://feishu.cn/docx/<document_id>`。
   - 返回 `ok` 为 false，但带可用 `document_id` 或 `node_token`（例如 `content_insert_failed`）：同样写入 `doc.json`，并标记正文未完成。`doc-status` 的 `create` 为 false，`body_incomplete` 为 true。
   - 返回 `ok` 为 false 且没有可用 id（空字符串也不算）：`save-doc` 不写 `doc.json`，把返回累计进 `state/ai-app-weekly/runs/2026-11-13/doc.failed.json` 并计次。`ok` 为 true 但拼不出链接的返回同样按无可用 ID 计次。同一份返回内容只计一次。同一期未满两次时 `create` 仍为 true。满两次后 `create` 为 false，`reason` 为 `create_failed_twice`，停止再建。摘要说明文档未建成，不放链接，不归档、不写快照。
   - 记录 JSON 读不出来时 `create` 为 false，`reason` 为 `doc_record_unreadable`，不要再建，也不要覆盖。
9. 摘要用 `automation_report` 提交：本周结论、本周变化要点。正文已完成时附上文档链接。不用 Markdown 表格，不写附件，不写本地路径。长度不超过 40000 字。`body_incomplete` 为 true 时，摘要标「全文文档正文未完成」，不放该链接，说明未完整交付，到此停止：不归档、不写快照。正文已完成，且确认提交成功（`ok` 为 true，`delivery_confirmed` 为 true，`status` 为 `sent`，`content_kind` 为 `text`，`report_run_id` 相同）之后，才 `archive-report`，再 `snapshot --write`。不带 `--no-doc` 时，文档记录不存在、正文未完成、记录不可读或创建失败未完成，这两条命令返回 `ok` 为 false 并带 `reason`，不写文件。`content_conflict` 或 `not_automation_run` 时不归档、不写快照，不换第二个 `report_run_id`，也不改用 `failure_text`。最终回复只写 `NO_REPLY`。

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json plan --workspace-dir <workspace> \
  --as-of 2026-11-13T15:00:00+08:00
```

美股日历季度、港股中期与全年、A 股与本公司 2025 年记录保持一致，见补库步骤 2。

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json add-fundamental --workspace-dir <workspace> \
  --company Cloudflare \
  --ticker NET \
  --track tob_software \
  --period 2026Q4 \
  --currency USD \
  --total-revenue-orig-mn 500 \
  --total-revenue-usd-mn 500 \
  --revenue-yoy-pct 30 \
  --evidence-grade A \
  --source-url https://www.cloudflare.com/ \
  --source-date 2026-11-13 \
  --quote "Fourth quarter revenue totaled 500 million."
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json add-metric --workspace-dir <workspace> \
  --company Cloudflare \
  --ticker NET \
  --track tob_software \
  --metric ai_revenue \
  --period 2026Q4 \
  --value 100 \
  --unit USD_mn \
  --value-usd-mn 100 \
  --definition "AI revenue disclosed as a separate line" \
  --ai-basis company_ai_line \
  --evidence-grade A \
  --source-url https://www.cloudflare.com/ \
  --source-date 2026-11-13 \
  --quote "AI revenue was 100 million."
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json reconcile --workspace-dir <workspace> \
  --expected NET
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json reconcile --workspace-dir <workspace> \
  --expected-file state/ai-app-weekly/temp/ai-app-weekly/due-2026-11-13.json
```

`mark-gap` 的 `--reason` 用 `not_disclosed`、`source_unavailable`、`not_reported_yet` 等真实原因，并写 `--checked-source`。

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json coverage-ledger --workspace-dir <workspace> \
  --write --since 2025Q1 --until 2026Q3
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json diff --workspace-dir <workspace> \
  --against 2026-11-06
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json screen --workspace-dir <workspace> \
  --with-price
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json ai-monetization --workspace-dir <workspace> \
  --by yoy --with-price
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json headline-monetization --workspace-dir <workspace> \
  --with-price
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json usage --workspace-dir <workspace>
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json validate --workspace-dir <workspace> \
  --file state/ai-app-weekly/temp/ai-app-weekly/report-2026-11-13.md
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json doc-status --workspace-dir <workspace> \
  --date 2026-11-13
```

`create` 为 true 时：

```python
feishu_create_doc(
    title="AI 应用周报 2026-11-13",
    markdown="# AI 应用周报 2026-11-13\n\n## 本周结论\n本周样例。\n",
)
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json save-doc --workspace-dir <workspace> \
  --date 2026-11-13 \
  --file state/ai-app-weekly/runs/2026-11-13/feishu-return.json
```

有变化时的摘要：

```python
automation_report(
    action="submit",
    report_run_id="ai-app-weekly-2026-11-13",
    material="yes",
    text="AI 应用周报 2026-11-13\n本周结论：样例判断。\n本周变化：样例要点。\n文档：https://feishu.cn/docx/DoxExampleDocId20261113\n",
)
```

```python
automation_report(
    action="status",
    report_run_id="ai-app-weekly-2026-11-13",
)
```

正文已保存但发送未确认时，用同一个 `report_run_id` 和同一份 `text` 再提交一次。仍未确认就停止，最终回复只写 `NO_REPLY`。`content_conflict` 时最终回复说明已保存的正文与本次摘要不一致，不附摘要正文。

正文已完成，且确认提交成功后才运行：

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json archive-report --workspace-dir <workspace> \
  --date 2026-11-13 \
  --file state/ai-app-weekly/temp/ai-app-weekly/report-2026-11-13.md
```

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json snapshot --workspace-dir <workspace> \
  --write \
  --as-of 2026-11-13T15:00:00+08:00
```

无变化时的短消息同样用 `text`，五行列出窗口、应更新家数、入库家数、本周无变化、下周催化剂。不建文档。确认提交成功后才运行下面这条。`--no-doc` 只用于这条短消息路径，并且只在该期既没有 `doc.json` 也没有 `doc.failed.json`（检查结果为 `doc_missing`）时写入。已有未完成正文、损坏记录或失败记录时，命令照样拒绝。

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py \
  --json snapshot --workspace-dir <workspace> \
  --write --no-doc \
  --as-of 2026-11-13T15:00:00+08:00
```

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
