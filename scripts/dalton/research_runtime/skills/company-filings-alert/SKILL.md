---
name: "company-filings-alert"
description: "监控美股 SEC 与中港公告，分类、去重并推送覆盖公司最新 filing。"
user-invocable: true
---

# Company Filings Alert

对覆盖公司列表每日监控SEC EDGAR新filing，自动分类并推送告警。

## 监控的Filing类型

| 类别 | Filing类型 | 含义 |
|------|-----------|------|
| 📊 earnings | 8-K, 6-K | 财报发布、重大事件（美股8-K，外国私人发行人6-K） |
| 👤 insider | Form 4 | 内部人交易（买入/卖出/授予） |
| 📋 annual_quarterly | 10-K, 10-Q, 20-F | 年报/季报 |
| 🗳️ proxy | DEF 14A | 代理投票声明 |
| 💰 offering | S-1, F-1, 424B | 新股发行/IPO |
| 🏛️ ownership | SC 13D, SC 13G | 大额持仓变动（5%+） |
| 📄 other | 144 | 限售股出售意向 |

## 前置依赖

- **findata-analyst** skill（必须在 `skills/findata-analyst/` 路径下）
- **cn-hk-findata** skill（必须在 `skills/cn-hk-findata/` 路径下，用于A股公告）
- **EDGAR_IDENTITY** 环境变量（SEC要求的身份标识）

## 数据源

| 市场 | 数据源 | 可靠性 | 说明 |
|------|--------|--------|------|
| 美股SEC | EDGAR官方API (edgartools + sec.gov/data.sec.gov enrichment) | ★★★★★ | 权威一手源；补充标题、Item、正文摘录和结构化持股/减持字段 |
| A股 | 东财AkShare API | ★★★★☆ | 官方后端，偶有延迟 |
| 港股 | Gemini grounded search → HKEXnews/公司IR | ★★★★★ | CLI 输出官方检索请求，agent 用 `web_search` 的明确日期范围完成 |

## 严格时间窗口

遇到“过去 N 小时”类扫描时，优先用公告平台提供的发布时间判定是否落窗；页面只给文件日期时，读取官方文件的 HTTP `Last-Modified` 作为发布时点证据，同时保留文件日期与该响应头原值。两者跨日或冲突时不得互相覆盖，也不得把文件日期推断成精确发布时间；完成标准是输出同时标明用于落窗的字段及其来源。

## 港股官方公告检索规则

`scan-cn-hk` 对 A 股直接返回公告；对港股返回每家公司的 `web_search_query/date_after/date_before`。Agent 必须逐家调用：

```
web_search(query=result.web_search_query, date_after=result.date_after, date_before=result.date_before, count=10)
```

只接受 HKEXnews 或公司官方 IR/公告；第三方转载只能用于定位，不得作为最终来源。仅传 `query/count/date_after/date_before`，禁止 `country/language/domain_filter/search_lang/ui_lang`。查询中的域名限定用 `site:hkexnews.hk` 写入 query。

## Commands

### A股单家公司精确日期窗口

使用 `cn-hk-findata` 的长选项形式查询，并以返回 `ok=true`、`rows[]` 含 `date/title/pdf_link/source` 为完成：

```bash
bash skills/cn-hk-findata/run.sh --json --ticker 600309 --list-filings \
  --date-from 2026-09-07 --date-to 2026-09-07 --limit 20
```

此分支只使用示例中的长选项；按需替换股票代码、日期和条数。

### scan — 批量扫描覆盖列表

```bash
# 使用逗号分隔的ticker列表
bash skills/company-filings-alert/run.sh scan --tickers NVDA,AAPL,TCOM --days 3

# 使用watchlist文件
bash skills/company-filings-alert/run.sh scan --tickers skills/company-filings-alert/watchlist.txt --days 3

# 使用默认watchlist（skills/company-filings-alert/watchlist.txt）
bash skills/company-filings-alert/run.sh scan --days 3

# JSON模式
bash skills/company-filings-alert/run.sh --json scan --tickers NVDA,AAPL --days 3
```

默认扫描会按 accession 从 SEC 官方原文补充 `headline`、`item_labels`、`body_excerpt`、`report_date`，并对 13D/G、144 提取申报人、持股比例、股数和金额。补充失败只在该 filing 写入 `enrich_error`，不会中断整批扫描；结果按 accession 缓存，空结果不缓存。

如需排障或只取表单列表，可关闭补充层：

```bash
bash skills/company-filings-alert/run.sh --json scan --days 3 --no-enrich
```

### check — 单个ticker查询

```bash
bash skills/company-filings-alert/run.sh check NVDA --days 7
bash skills/company-filings-alert/run.sh --json check NVDA --days 3
bash skills/company-filings-alert/run.sh --json check NVDA --days 3 --no-enrich
```

### 定向 A 股查询

任务按动态 coverage 或用户点名公司执行时，先核对 `scan-cn-hk` 返回的 ticker 是否覆盖全部目标；默认 watchlist 缺少目标时，不得据此判断“无公告”。对缺失的 A 股逐一运行显式 ticker 和日期窗口查询：

```bash
bash skills/cn-hk-findata/run.sh --json --ticker 600309 --list-filings \
  --date-from 2026-09-06 --date-to 2026-09-07 --limit 50
```

将示例 ticker 和日期替换为本次目标；确认结果中的 `ticker`、`date` 和目标一致后，再判断是否有新公告。

### self-test

```bash
bash skills/company-filings-alert/run.sh self-test
bash skills/company-filings-alert/run.sh --json self-test
```

## Watchlist 文件格式

创建 `skills/company-filings-alert/watchlist.txt`，每行一个ticker：
```
# 美股科技
NVDA
AAPL
GOOGL
# 中概股
TCOM
BABA
JD
PDD
```

以 `#` 开头的行是注释，空行会被忽略。

## 输出格式

推送采用以下固定格式：

```
**每日公告提醒 | {日期}（周一加“覆盖上周五至今”）**

**{TICKER}**
{一句话摘要：自然语言概括该公司所有filing的整体情况}
- {具体条目1}
- {具体条目2}

**{下一个TICKER}**
{一句话摘要}
- {具体条目}

X 家公司有新动态。
```

格式规则：
- 不用emoji或警示符号
- 公司名（ticker）粗体，单独一行
- 摘要另起一行，自然语言
- 具体条目用bullet list
- Form 4必须包含：内部人姓名、职位、交易类型、股数、金额（从parsed字段提取）
  例：`Arash Adam Foroughi (CEO & Chairperson) — 卖出 40,666 股，约 $18,699,807 | 3/13`
- Derivative Acquisition：写明获授内容及金额
- 8-K/6-K：先用 `headline` / `item_labels` 写具体事项；需要补充时只从 `cover_summary` / `body_excerpt` 摘要，不根据 accession 猜测
- 13D/G：优先写 `reporting_persons`、`shares`、`percent_of_class`、`amendment_no`
- 144：优先写 `seller`、`position`、`shares`、`market_value`、`sale_date`
- 144：说明是限售股出售意向申报
- 多位高管同时卖出：摘要里提及合计金额，不补“值得留意”等空判断
- 公司之间必须有空行隔开
- 无新动态时：`过去 24 小时无新动态。`
- 摘要直接写哪个公司发生了什么。Form 4、8-K、6-K、HKEXnews 等正式名称保持原样；未取得正文、金额或日期时明确写缺什么，不根据标题补事实。

## Cron 设置

定时公告日报已由 Dalton 技能 `dalton-daily-filing-alert` 负责。

## State

检查状态保存在 `$DALTON_RESEARCH_HOME/state/company-filings-alert/last_check.json`（旧 `DALTON_OCW_ROOT` 模式下为 `skills/company-filings-alert/state/last_check.json`），记录每个 ticker 的最后检查时间。

补充层另用两个可重建缓存：
- `cik_map.json`：ticker → CIK，最长缓存 7 天
- `enrich_cache.json`：按 accession 缓存已取得的官方标题和正文摘录；URL-only/空结果不缓存

`filings.json` 同时保存 `headline`、`item_labels`、`body_excerpt` 等字段。
