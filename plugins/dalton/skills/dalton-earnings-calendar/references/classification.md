# 财报日期分类与官方来源规则（`classification.md`）

本文件定义 `dalton-earnings-calendar` 在扫描与更新台账时的日期分类标准、来源门槛、时点（timing）与财季标签规则。核心原则：**只认公司官方来源；第三方聚合日历一律标“待确认”（`date_status: estimated`），在官方公布前不建研究自动任务。**

---

## 一、五类扫描状态与台账三态映射

| 扫描 `status` | 台账 `status` | `date_status` | 判定条件 | 自动任务动作 |
|---|---|---|---|---|
| `confirmed` | `confirmed` | `confirmed` | **公司官方来源**明确披露未来的具体业绩发布日期 | 为 `workspace.status=active` 的公司创建/改期 `tm3`、`t0`、`tp1`（Watch 只建 `t0`） |
| `pending`（带候选日期） | `pending` | `estimated`（界面与总结标“**待确认**”） | 无公司官方来源，仅有 FMP、yfinance 或第三方聚合网站给出预计日期 | **不建 `tm3/t0/tp1`**；若候选日期在 10 天内，仅建一条 T-5 的 `once` 日期复查任务（只复查日期，不做研究） |
| `already_reported` | 移入 `reported` 并在 `entries` 重置为 `pending` | `unannounced` | 本期财报已发布完毕（发布日在过去），下一期尚未披露 | 走 `roll` 归档，不建新一期任务 |
| `not_announced` | `pending` | `unannounced` | 官方未披露、也无候选日期，或信息存在歧义 | 不建任务；严禁按去年同期外推或猜测日期 |
| `scan_failed` | 保持原状态不变 | 保持原值 | 搜索工具报错、超时、额度耗尽或页面抓取失败 | 不改台账日期，必须列入总结第 6 项“**扫描失败的公司**”，严禁伪装成 `not_announced` |

---

## 二、官方来源与候选来源边界

### 1. 合格的公司官方来源（可标 `confirmed`，`confidence: "high"`）

必须直接读到原文并核实发行人身份、目标财季与具体未来日期（`earnings_date >= today`）：

1. **公司 IR 新闻稿（`source_type: "company_ir"`）**：公司投资者关系官网（IR Newsroom / Press Releases）发布的排期公告，或公司经 BusinessWire、PRNewswire、GlobeNewswire 正式发出的排期通告（例如 *"Meta to Report Third Quarter 2026 Results on October 28, 2026"*）。
2. **公司 IR Events 页（`source_type: "ir_events"`）**：公司官方 IR Calendar / Upcoming Events 页面明确列出的下一次季报/年报发布日及电话会时间。
3. **监管机构与交易所公告（`source_type: "sec_filing"` / `"cnhk_filing"`）**：
   - **美股**：SEC EDGAR 上的 8-K 或 6-K 明确披露未来业绩发布日期。
   - **港股与 A 股（`route="cn-hk"`）**：调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=SEARCH_START_DATE, end=TODAY, limit=20)`（港股传 5 位零填充纯数字如 `"00700"`，A 股传 6 位纯数字如 `"600519"`，不带 `.HK`/`.SS`/`.SZ` 后缀）检索巨潮资讯（cninfo）披露的**董事会会议召期通告（Date of Board Meeting）**、**定期报告预约披露时间表/变更公告**及 `pdf_link` 原文；若港交所披露易（HKEXnews）的董事会会议通告未被 `cn-hk` 的 `filings` 覆盖，必须使用 `web_search` 检索披露易（`site:hkexnews.hk`）并核对官方原文链接。也可辅以 `researchdata.finance(route="cn-hk", operation="ask", query=f"{code_or_name} 公告", limit=20)` 定位线索，但必须核实官方公告原文后方可标为 `confirmed`。

### 2. 第三方聚合与估算来源（只能标 `pending` + `date_status: "estimated"`）

以下来源给出的日期（无论单家还是多家一致）多数基于历史算法推算，**一律不得作为 `confirmed` 依据**：

- 数据接口：`FMP`、`yfinance`、LangAlpha 自带 `get_earnings_calendar`
- 财经聚合站：Nasdaq Earnings Calendar、Zacks、Earnings Whispers、Wall Street Journal (WSJ)、MarketWatch、Yahoo Finance Calendar、Seeking Alpha、Investing.com

**处理规则**：
- 台账 `status` 保持 `"pending"`，写入 `"tentative_date": "YYYY-MM-DD"`、`"tentative_sources": ["..."]`、`"date_status": "estimated"`、`"confidence": "low"`（或多家聚合一致时 `"medium"`）。
- 在 `agenda.watch.events` 与周度总结中统一显示为“**待确认**”。
- **严禁为 `estimated` 日期创建 `tm3`、`t0`、`tp1` 自动任务**。
- **10 天内候选复查**：若 `0 <= (tentative_date - today).days <= 10`，创建一条 T-5（或当距今不足 5 天时设为次日 09:00）的 `once` 自动任务 `dalton-<t>-earn-<period>-recheck.json`，instruction 仅执行单公司官方日期复查，一旦官宣再转正并补建财报链任务。

---

## 三、发布时段（`timing`）与具体时间规则

1. **`timing` 枚举**：
   - `BMO`（Before Market Open，美股盘前，通常 06:00–09:00 ET 发布）
   - `AMC`（After Market Close，美股盘后，通常 16:00–16:30 ET 发布）
   - `unknown`（官方公告仅给出日期，未指明盘前或盘后）
2. **业绩发布时间 vs 电话会时间**：
   - 台账分别记录 `release_time_et`（财报新闻稿发布时间，如 `"16:05"`）与 `call_time_et`（业绩电话会开始时间，如 `"16:30"`）。
   - 若官方通告仅写出 *"after market close, followed by a conference call at 4:30 p.m. ET"* 而未精确到分钟的发布时间，`release_time_et` 取 `"16:00"`（AMC 默认盘后整点）或 `"07:00"`（BMO 默认盘前），并在 `notes` 说明。
   - **日期以业绩发布日（`earnings_date`）为准**，不以次日亚洲时区电话会日期为准。
3. **非财报事件排除**：
   - 季度经营数据简报（如零售/快递/潮玩的月度或季度经营更新、车企月度交付量、Investor Day、股东大会）**不是财报业绩发布**，不得作为 earnings release 写入本台账。

---

## 四、财年与财季标签规范（`quarter`）

- 统一使用公司自身的财年与财季命名，格式为 `FY<YY>Q<1-4>`（如 `FY26Q3`；半年报公司用 `FY26H1` / `FY26FY`），自动任务文件名中的 `<period>` 使用对应全小写（如 `fy26q3`）。
- 特殊财年公司必须核对覆盖名单 `coverage.yaml` 的 `note` / `fiscal_note`：
  - **6 月财年**：MSFT（Microsoft）、AFRM（Affirm）——例如 2026 年 7–9 月自然季为 `FY27Q1`。
  - **9 月财年**：AAPL（Apple）、V（Visa）、DHI（D.R. Horton）——例如 2026 年 7–9 月自然季为 `FY26Q4`。
  - **3 月财年**：BABA（Alibaba）。
  - **1 月财年**：CRM（Salesforce）、WMT（Walmart）、PL（Planet Labs）。
  - **10 月财年**：TOL（Toll Brothers）。
  - **自然年财年（`FY=CY`）**：META、GOOGL、AMZN 等——2026 年 7–9 月为 `FY26Q3`。
