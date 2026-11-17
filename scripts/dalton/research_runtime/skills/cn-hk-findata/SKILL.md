---
name: "cn-hk-findata"
description: "China/HK/US stock-data analyst skill powered by cninfo + AkShare stock interfaces. Use for NL stock questions needing executable retrieval + structured output."
user-invocable: true
disable-model-invocation: true
---

# CN/HK FinData Skill v2.2 (Registry-Router + Structured Delivery + Multi-Source Fallback)

⚠️ **CRITICAL: NO HALLUCINATIONS**
- Never invent values, dates, links, rankings, or reasons.
- Every number must come from command output.
- If command fails, return `error + hint` and stop.

⚠️ **CRITICAL: EXECUTION RULE**
- Do NOT write ad-hoc Python/curl for user tasks.
- Always run through:
```bash
bash skills/cn-hk-findata/run.sh ...
```

---

## Architecture (v2 — Registry-Based)

- **88 intents** defined in `scripts/intent_registry.yaml` (YAML-driven, no code changes to add new intents)
- **5593 company names** in `scripts/stock_name_cache.json` (O(1) name→ticker lookup)
- **70+ synonyms** for colloquial Chinese → canonical terms
- **Two-phase routing**: keyword exact match → fuzzy/synonym scoring → market-aware fallback
- **Combo queries**: detects "以及/同时/+/还有" → splits into multiple intents → merges results
- **Per-intent vendor fallback**: 12 个 intent 在东财接口不可用时自动改用腾讯/新浪/同花顺/雪球的等价接口（见第 8 节）
- **Auto-retry**: 只对超时类瞬时错误重试 1 次；连接被拒/重置/502/403 立即失败并换源
- **Market filter**: 单项与组合查询均先校验市场，再补股票代码位数；港股 00700 不得补成 A股 000700。无兼容港美股接口时返回缺口，不按名称相似度改查其他市场。
- **Multi-source fallback (v2.1)**: AkShare → Tencent Finance → Sina Finance, with per-capability health tracking; sick sources are demoted, not dropped

---

## 0) Preflight (always first)

```bash
bash skills/cn-hk-findata/run.sh --self-test --json
```

If `ok=false`: stop, report failing checks with `hint`.

Expected checks: `legacy.runtime`, `akshare.import`, `registry.load` (88), `name_cache` (5500+).

---

## 1) Primary Entry: Natural Language Questions

```bash
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "<用户问题>" --max-rows 50
```

This is the **default for all stock questions**. The router handles:
- Extracting company names/tickers from natural language
- Matching to the best AkShare function via registry
- Resolving arguments (dates, market, ticker format)
- Falling back to generic score-based routing if no registry match

### What `--ask` returns
- `ok`: true/false
- `mode`: "ask" (single intent) | "combo" (multi-intent) | fallback
- `plan.module`: matched registry intent ID
- `plan.chosen_function`: AkShare function called
- `plan.kwargs`: resolved arguments
- `plan.score`: match confidence
- `summary`: human-readable summary
- `data.rows`: primary table (list of dicts)
- `data.csv`: CSV string
- `results[]`: (combo mode only) array of per-intent results

---

## 2) Intent Catalog

### 💰 资金流 (5 intents)
| Intent | 触发示例 | AkShare 函数 |
|--------|----------|-------------|
| `fund_flow_individual` | "茅台资金流向"、"谁在买"、"主力在买吗" | `stock_individual_fund_flow` |
| `fund_flow_market` | "大盘资金流"、"全市场资金" | `stock_market_fund_flow` |
| `sector_fund_flow_rank` | "哪个行业资金流入最多" | `stock_sector_fund_flow_rank` |
| `concept_fund_flow` | "概念板块资金流" | `stock_fund_flow_concept` |
| `industry_fund_flow` | "行业板块资金即时" | `stock_fund_flow_industry` |

### 🔄 沪深港通 (2)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `hsgt_hold` | "北向持股排行"、"沪股通持股" | `stock_hsgt_hold_stock_em` |
| `hsgt_fund_flow` | "北向资金"、"外资在买"、"北水流入" | `stock_hsgt_fund_flow_summary_em` |

### 📊 财务报表 (9)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `financial_a_income` | "茅台利润表"、"营收"、"赚了多少钱" | `stock_profit_sheet_by_report_em` |
| `financial_a_balance` | "资产负债表"、"总资产"、"负债率" | `stock_balance_sheet_by_report_em` |
| `financial_a_cashflow` | "现金流"、"经营现金流" | `stock_cash_flow_sheet_by_report_em` |
| `financial_hk_income` | "腾讯财报"、"港股利润表" | `stock_financial_hk_report_em` |
| `financial_hk_balance` | "港股资产负债" | `stock_financial_hk_report_em` |
| `financial_hk_cashflow` | "港股现金流" | `stock_financial_hk_report_em` |
| `financial_indicator` | A股 "ROE"、"杜邦分析"、"财务指标" | `stock_financial_analysis_indicator` |
| `financial_hk_indicator` | 港股 "财务指标"、"ROE"、"财务摘要" | `stock_financial_hk_analysis_indicator_em` |
| `financial_abstract` | "财务摘要"、"快速财务" | `stock_financial_abstract` |

### 📈 业绩 (3)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `yjyg` | "业绩预告"、"预增"、"业绩暴雷" | `stock_yjyg_em` |
| `yjkb` | "业绩快报" | `stock_yjkb_em` |
| `yjbb` | "业绩报表"、"季报" | `stock_yjbb_em` |

### 🔴🟢 涨跌停 & 异动 (6)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `zt_pool` | "今日涨停"、"封板了几个" | `stock_zt_pool_em` |
| `zt_pool_strong` | "强势涨停"、"连板" | `stock_zt_pool_strong_em` |
| `dt_pool` | "跌停池"、"炸板" | `stock_zt_pool_dtgc_em` |
| `zt_pool_zbgc` | "炸板详情"、"开板" | `stock_zt_pool_zbgc_em` |
| `zt_pool_previous` | "昨日涨停今日表现" | `stock_zt_pool_previous_em` |
| `zt_pool_sub_new` | "次新股涨停" | `stock_zt_pool_sub_new_em` |
| `stock_changes` | "盘中异动"、"急涨急跌" | `stock_changes_em` |

### 👥 股东分析 (7)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `top10_holders` | "十大股东"、"谁在持仓" | `stock_gdfx_free_top_10_em` |
| `shareholder_count` | "股东户数"、"散户多不多" | `stock_zh_a_gdhs_detail_em` |
| `circulate_holder` | "流通股东" | `stock_circulate_stock_holder` |
| `fund_holder` | "基金重仓"、"基金持仓明细" | `stock_fund_stock_holder` |
| `main_holder` | "大股东"、"实控人" | `stock_main_stock_holder` |
| `hold_management` | "高管持股明细"、"管理层持股" | `stock_hold_management_detail_em` |
| `hold_num_all` | "全市场股东户数" | `stock_hold_num_cninfo` |

### 📉 行情 (6)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `hist_a` | "A股走势"、"比亚迪走势怎么样" | `stock_zh_a_hist` |
| `hist_hk` | "港股行情" | `stock_hk_hist` |
| `us_hist` | "美股行情"、"纳斯达克" | `stock_us_hist` |
| `minute_a` | "分钟线"、"分时图" | `stock_zh_a_minute` |
| `valuation_a` | "PE"、"估值"、"贵不贵" | `stock_zh_valuation_baidu` |
| `valuation_hk` | "港股估值" | `stock_hk_valuation_baidu` |

### 📊 指数 (4)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `index_spot_a` | "上证指数"、"沪深300"、"大盘指数" | `stock_zh_index_spot_em` |
| `index_spot_hk` | "恒生指数"、"恒生科技" | `stock_hk_index_spot_em` |
| `index_hist_a` | "指数走势"、"大盘走势" | `stock_zh_index_daily_em` |
| `index_hist_hk` | "恒指走势" | `stock_hk_index_daily_em` |

### 🏷️ 板块 (6)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `board_concept` | "概念板块行情" | `stock_board_concept_hist_em` |
| `board_industry` | "行业板块走势"、"属于什么板块" | `stock_board_industry_hist_em` |
| `board_concept_cons` | "AI概念有哪些股票" | `stock_board_concept_cons_em` |
| `board_industry_cons` | "行业有哪些股票" | `stock_board_industry_cons_em` |
| `sector_fund_flow_summary` | "板块资金汇总" | `stock_sector_fund_flow_summary` |
| `sector_fund_flow_hist` | "板块资金历史" | `stock_sector_fund_flow_hist` |

### 🔥 热度 & 情绪 (8)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `hot_rank` | "热榜"、"最火的股票" | `stock_hot_rank_em` |
| `hot_rank_latest` | "个股热度趋势" | `stock_hot_rank_latest_em` |
| `hot_rank_detail` | "热度排名历史" | `stock_hot_rank_detail_em` |
| `comment_jgcyd` | "机构参与度" | `stock_comment_detail_zlkp_jgcyd_em` |
| `comment_focus` | "市场关注度"、"关注热度" | `stock_comment_detail_scrd_focus_em` |
| `comment_desire` | "买入意愿"、"多空意愿" | `stock_comment_detail_scrd_desire_em` |
| `comment_score` | "综合评分"、"个股评分" | `stock_comment_detail_zhpj_lspf_em` |
| `comment_overview` | "千股千评"、"诊股" | `stock_comment_em` |

### 📰 交易 & 监管 (8)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `lhb` | "龙虎榜"、"今日龙虎榜" | `stock_lhb_detail_em` |
| `lhb_stock_stat` | "龙虎榜统计"、"上榜次数" | `stock_lhb_stock_statistic_em` |
| `lhb_yyb` | "营业部排名"、"知名游资排名" | `stock_lh_yyb_most` |
| `dzjy` | "大宗交易" | `stock_dzjy_sctj` |
| `margin_sse` | "两融"、"融资余额" | `stock_margin_sse` |
| `margin_szse` | "深市两融" | `stock_margin_szse` |
| `restricted_release` | "解禁"、"限售" | `stock_restricted_release_summary_em` |
| `pledge_ratio` | "股权质押"、"质押率" | `stock_gpzy_pledge_ratio_em` |

### 📋 公司动态 (7)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `ggcg` | "增减持"、"高管减持" | `stock_ggcg_em` |
| `repurchase` | "回购" | `stock_repurchase_em` |
| `notice` | "公告" | `stock_notice_report` |
| `research_report` | "研报"、"目标价"、"券商怎么看" | `stock_research_report_em` |
| `jgdy` | "机构调研"、"谁来调研了" | `stock_jgdy_detail_em` |
| `fhps` | "分红"、"派息"、"股息率" | `stock_fhps_em` |
| `fhps_detail` | "历史分红记录"、"每股分红" | `stock_fhps_detail_em` |

### 📰 新闻 & 快讯 (6)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `global_news_em` | "股市新闻"、"财经快讯" | `stock_info_global_em` |
| `global_news_cls` | "财联社快讯"、"7x24快讯" | `stock_info_global_cls` |
| `global_news_futu` | "富途快讯" | `stock_info_global_futu` |
| `global_news_sina` | "新浪快讯" | `stock_info_global_sina` |
| `global_news_ths` | "同花顺快讯" | `stock_info_global_ths` |
| `stock_news` | "茅台有什么新闻"、"利好利空" | `stock_news_em` |
| `stock_news_main` | "财经头条"、"要闻" | `stock_news_main_cx` |

### 🌐 其他 (8)
| Intent | 触发示例 | 函数 |
|--------|----------|------|
| `individual_info` | "公司简介"、"做什么的"、"主营业务" | `stock_individual_info_em` |
| `profit_forecast` | "盈利预测"、"一致预期"、"分析师预测" | `stock_profit_forecast_em` |
| `ah_spot` | "AH溢价"、"AH比价" | `stock_zh_ah_spot` |
| `ah_hist` | "AH历史溢价" | `stock_zh_ah_daily` |
| `kcb_spot` | "科创板行情" | `stock_zh_kcb_spot` |
| `analyst_rank` | "分析师排名"、"金牌分析师" | `stock_analyst_rank_em` |
| `ipo_dxsyl` | "打新收益率" | `stock_dxsyl_em` |
| `ipo_xgsglb` | "新股申购"、"IPO" | `stock_xgsglb_em` |
| `esg` | "ESG评级" | `stock_esg_hz_sina` |

### ⚡ Specialized Handlers (code, not registry)
| Handler | 触发示例 | 说明 |
|---------|----------|------|
| `compare_60d` | "茅台和五粮液对比" | 60日收益/波动/成交对比 |
| `xq_hot_rank` | "雪球热股排行榜" | 雪球关注+讨论+交易聚合 |

---

## 3) Colloquial / Synonym Support

The router understands colloquial Chinese. Examples:
- "茅台赚钱吗" → `financial_a_income`
- "宁德时代贵不贵" → `valuation_a`
- "谁在抄底茅台" → `fund_flow_individual`
- "散户割了吗" → `shareholder_count`
- "今天封板了几个" → `zt_pool`
- "券商怎么看" → `research_report`
- "北水流入了多少" → `hsgt_fund_flow`
- "业绩暴雷" → `yjyg`

Full synonym map: 70+ entries in `_SYNONYM_MAP` (adapter source).

---

## 4) Combo Queries

When the question contains connectors (以及/同时/+/还有/并且/加上), the router splits into multiple intents and merges results.

Example: "茅台的业绩预告以及股东户数变化" → returns `yjyg` + `yjbb` + `shareholder_count` in one response.

Output format: `mode: "combo"`, `results: [...]` array with per-intent data.

---

## 5) Deterministic Commands (explicit ticker-based)

### A) Recent filings list (cninfo)
```bash
bash skills/cn-hk-findata/run.sh --json --ticker <TICKER> --limit 120 --max-pages 8 --list-filings
```

### B) Financial extraction (HK via AkShare)
```bash
bash skills/cn-hk-findata/run.sh --json --backend akshare --ticker <TICKER> --financials
```

### C) Filing keyword search (cninfo report text)
```bash
bash skills/cn-hk-findata/run.sh --json --ticker <TICKER> --limit 200 --max-pages 12 --search-text "<KEYWORD>" --report-keyword "年度报告"
```

### D) Filing-based metric facts series
```bash
bash skills/cn-hk-findata/run.sh --json --ticker <TICKER> --max-pages 10 --facts-metric <METRIC> --facts-limit 5
```
Metrics: `revenue net_profit gross_profit operating_profit assets liabilities operating_cashflow`

---

## 6) Output Contract (reply formatting)

1) **结论（1-3行）** — Direct answer first
2) **关键数据（bullet）** — Values from output JSON only, with units/periods
3) **调用与来源（bullet）** — `plan.module` + `plan.chosen_function` + key kwargs
4) **数据质量提示（if needed）** — network issues, fallback used, missing fields

Never output markdown tables on Discord; use bullets.

---

## 7) Failure Handling

- `ok=false` → reply with `error` + `hint` + one concrete next step
- Network/proxy errors → mark as connectivity issue, do not fabricate data
- 维护行情HTML解析器时，对已经下载的正文使用 `from io import StringIO` 后调用 `pd.read_html(StringIO(html_text))`；复用已保存的响应完成解析，核对表中的日期列与收盘价列后再用于价格交叉检查。
- Registry miss → falls back to generic score-based router (399 stock_* functions)
- All candidates fail → suggest narrowing query (market/ticker/date/metric)
- **`degraded=true` 必须如实转述**：命中的 intent 全部取数失败后，通用路由会按函数名相似度另选一个接口，结果经常答非所问（问行业板块返回分红表）。这种回复里带 `degraded=true` 和 `failed_intents`，要说明原问题没拿到数据，不要把近似结果当答案交付
- 回复里出现 `plan.fallback_used=true` 时，注明数据来自哪个厂商（`plan.source`），因为同花顺/新浪的口径与东财不完全一致

---

## 8) Multi-Source Fallback (v2.1)

### Data Source Priority Chain
1. **AkShare** (primary) — 东方财富数据
2. **Tencent Finance** — 腾讯财经 qt.gtimg.cn API
3. **Sina Finance** — 新浪财经 hq.sinajs.cn API

`--fallback-quote` / `--fallback-hist` 走这条链。`--ask` 走的是下面的 **per-intent 回退**。

### Fallback Commands
```bash
# Fetch quote with automatic fallback
bash skills/cn-hk-findata/run.sh --json --fallback-quote 600519 --market a
bash skills/cn-hk-findata/run.sh --json --fallback-quote 00700 --market hk

# Fetch history with automatic fallback
bash skills/cn-hk-findata/run.sh --json --fallback-hist 600519 --market a --days 60

# Check source health status
bash skills/cn-hk-findata/run.sh --json --source-health

# Reset health tracking (after fixing network issues)
bash skills/cn-hk-findata/run.sh --json --reset-source all
bash skills/cn-hk-findata/run.sh --json --reset-source akshare
```

### Health Tracking

健康状态按 **(数据源, capability)** 记账，不是按数据源整体记账。capability 默认取 `context` 里第一个冒号之前的部分，例如 `quote:600519` → `quote`、`hist:600519:60d` → `hist`；也可以给 `fallback_fetch` 显式传 `capability=`。

这样记的原因：AkShare 打的是东方财富好几组互不相干的主机（`push2his` 行情、`datacenter` 榜单、`push2ex` 涨停池）。其中一组不通，不代表别的组不通。老的单一计数器会因为 K 线打不开就把整个 AkShare 判成 down —— 2026 年 4 月到 8 月 `state/source_health.json` 里那条 `akshare: consecutive_failures 29` 就是这么来的，实际龙虎榜、股东户数、涨停池一直是好的。

- 每个 (源, capability) 记录：连续失败次数、累计成功/失败、最近成功/失败时间、最近错误
- 连续失败满 3 次，该组合在 5 分钟内**降到队尾**，不是跳过。全链路都不健康时仍然逐个尝试，不会直接判死
- 状态写在 `state/source_health.json`，键形如 `akshare::hist`；旧的扁平键（`akshare`）仍可读，在报告里显示为 `unscoped`
- 返回值包含 `source`（实际成功的源）、`fallback_used`（是否没用上声明的主源）、`capability`、`attempt_order`（实际尝试顺序）
- `--source-health` 的源级 `healthy` 表示「至少一个 capability 可用」；具体哪个坏了看 `degraded_capabilities` 和 `capabilities`
- `--reset-source akshare` 清掉该源的全部 capability 记录
- `--ask` 路径的 capability 形如 `intent:hist_a`，所以 `akshare::intent:hist_a` 坏掉不会连累 `akshare::intent:lhb`

---

## 8b) Per-Intent Vendor Fallback (v2.2)

### 为什么有这一层

东方财富的行情主机（`push2his` / `push2` / `N.push2`）拒绝本机出口 IP：TLS 握手能完成，请求发完后服务端直接断连，换香港代理节点同样被拒，换一个干净 IP 才通。同一时间东财的 `datacenter`（龙虎榜、股东户数、财报）、`push2ex`（涨停池）、`np-anotice`（公告）全部正常 —— 被封的是行情集群，不是东财整体。

结论：这不是 akshare 版本问题，升级修不好；换出口 IP 目前也做不到。所以把受影响的 intent 改成不依赖单一厂商。

### 声明方式

registry 条目可以加 `fallbacks:`，每个回退带自己的 `function` / `source` / `args` / `post`：

```yaml
- id: hist_a
  function: stock_zh_a_hist          # 东财，主源
  args: {symbol: "{ticker6}", period: daily, start_date: "{start_date}", end_date: "{end_date}", adjust: qfq}
  fallbacks:
    - function: stock_zh_a_hist_tx   # 腾讯
      source: tencent
      args: {symbol: "{ticker_sh_sz_prefix}", start_date: "{start_date}", end_date: "{end_date}", adjust: qfq}
      post:
        date_column: date            # 先按问题里的时间窗过滤
        rename: {date: 日期, open: 开盘, close: 收盘, high: 最高, low: 最低}
        tail: 250                    # 再取最近 N 行
```

`post` 支持的算子，按 `filter_eq → date_column → sort_first → rename → tail/head` 顺序执行：

| 算子 | 作用 | 为什么需要 |
|---|---|---|
| `filter_eq` | 按列值筛行，值可用占位符 | 同花顺资金流返回全市场，要按 `{ticker6}` 取出该股 |
| `date_column` | 按问题里的 `{start_date}`~`{end_date}` 切窗口 | 新浪日线接口不收日期参数，一次返回全部历史 |
| `sort_first` | 把匹配的行提到最前 | 新浪指数快照是全量列表，目标指数可能被行数上限截掉 |
| `rename` | 列名对齐主源 | 各家返回 `date/open/close` 或 `开盘价/收盘价`，下游按东财中文列名读 |
| `tail` / `head` | 限制行数 | `_df_to_records` 取 `head(N)`，不切窗口会返回 1990 年代的行情 |

回退后结果为空视为该源失败，继续下一个源；全部失败才报错。

### 当前覆盖（12 个 intent，均已用真实网络验证）

| intent | 东财主源（当前不通） | 回退 |
|---|---|---|
| `hist_a` | `stock_zh_a_hist` | 腾讯 `stock_zh_a_hist_tx` → 新浪 `stock_zh_a_daily` |
| `hist_hk` | `stock_hk_hist` | 新浪 `stock_hk_daily` |
| `us_hist` | `stock_us_hist` | 新浪 `stock_us_daily` |
| `index_hist_a` | `stock_zh_index_daily_em` | 腾讯 `stock_zh_index_daily_tx` → 新浪 `stock_zh_index_daily` |
| `index_hist_hk` | `stock_hk_index_daily_em` | 新浪 `stock_hk_index_daily_sina` |
| `index_spot_a` | `stock_zh_index_spot_em` | 新浪 `stock_zh_index_spot_sina` |
| `index_spot_hk` | `stock_hk_index_spot_em` | 新浪 `stock_hk_index_spot_sina` |
| `board_concept` | `stock_board_concept_hist_em` | 同花顺 `stock_board_concept_index_ths` |
| `board_industry` | `stock_board_industry_hist_em` | 同花顺 `stock_board_industry_index_ths` |
| `fund_flow_individual` | `stock_individual_fund_flow` | 同花顺 `stock_fund_flow_individual`（即时口径） |
| `sector_fund_flow_rank` | `stock_sector_fund_flow_rank` | 同花顺 `stock_fund_flow_industry` |
| `individual_info` | `stock_individual_info_em` | 雪球 `stock_individual_spot_xq` |

名称解析（公司名→代码）也加了回退：A 股 `stock_zh_a_spot_tx`、港股 `stock_hk_spot`，东财 spot 列表打不开时仍能认名字。

### 口径差异（必须在回复里说明）

- 同花顺的概念/行业划分与东财不同，板块名先经 `stock_board_concept_name_ths` / `stock_board_industry_name_ths` 对齐；对不上就报错，不做模糊猜测（猜错会返回另一个板块的真实数据，比报错更危险）
- 同花顺个股资金流只有当日净额，没有东财的多日主力/散户/大单拆分
- 同花顺行业资金流是「即时」口径，与东财「今日」不等价
- 雪球个股快照有市值和估值，没有东财的所属行业和上市日期
- 新浪港股/美股日线是全历史，靠 `date_column` + `tail` 切窗口

### 仍然没有替代源的

`board_concept_cons` / `board_industry_cons`（板块成分股）只有东财接口，akshare 没有同花顺或新浪的等价函数。这两个 intent 在封禁解除前拿不到数据，会如实报错。

### 封禁是动态的

2026-08-21 排查时，连续压 `push2his` 十几次之后，原本正常的 `fflow/daykline` 也被断开，安静数分钟未恢复。**不要对东财行情主机做批量重试探测。** `_retry_call` 已经把连接被拒/重置/502/403 归为不可重试，健康记账连续失败 3 次后会把东财降到该 intent 的队尾（5 分钟），封禁解除后自动恢复。

## 9) Quick Commands

```bash
# self-test
bash skills/cn-hk-findata/run.sh --self-test --json

# list all 399 stock_* capabilities
bash skills/cn-hk-findata/run.sh --json --backend akshare --list-capabilities

# NL examples
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "茅台资金流向"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "今日涨停"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "腾讯财报怎么样"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "业绩预告"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "600519股东户数"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "茅台前十大股东"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "AI概念板块有哪些股票"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "哪个行业资金流入最多"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "恒生指数怎么样"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "科创板行情"
bash skills/cn-hk-findata/run.sh --json --backend akshare --ask "茅台的业绩预告以及股东户数变化"

# deterministic HK financials
bash skills/cn-hk-findata/run.sh --json --backend akshare --ticker 00700 --financials
```

---

## 9) Adding New Intents (for developers)

Edit `scripts/intent_registry.yaml` — add one entry:

```yaml
- id: my_new_intent
  keywords: [关键词1, 关键词2, 口语表达]
  function: stock_xxx_em
  needs_ticker: true          # or false
  ticker_format: cn6          # cn6 | hk5 | cn_prefix | hk5 | (omit for auto)
  args:
    symbol: "{ticker6}"       # use placeholders
  description: 中文描述
```

Available placeholders: `{ticker}`, `{ticker6}`, `{ticker5}`, `{ticker_cn_prefix}` (SH/SZ), `{ticker_sh_sz_prefix}` (sh/sz), `{index_sh_sz_prefix}` (指数前缀，000xxx→sh、399xxx→sz), `{hk_index_symbol}` (由问题关键词判定 HSI/HSCEI/HSTECH), `{start_date}`, `{end_date}`, `{start_date_dash}`, `{end_date_dash}` (YYYY-MM-DD，新浪接口用), `{today}`, `{latest_quarter}`, `{market_sh_sz}`, `{hsgt_market}`, `{board_name}`, `{board_name_ths_concept}`, `{board_name_ths_industry}` (对齐到同花顺板块名，对不上会报错), `{indicator_annual_quarterly}`.

需要跨厂商兜底时加 `fallbacks:`，格式见第 8b 节。No code changes needed. Run `--self-test` to verify.
