---
name: dalton-china-consumer-weekly
description: "无人值守的中国消费周报：按超额中位数与证据门槛采集事实并成稿，并用 automation_report 提交纯文本。"
---

# 中国消费周报

无人值守。不提问，不创建子任务。安装路径是 `.agents/skills/dalton-china-consumer-weekly/`。命令用 `scripts/china_consumer_weekly.py`，写文件时带 `--workspace-dir <workspace>`。

接收方在 `metadata.report_profile`，配置名是 `china-consumer-weekly`。技能正文和工具参数都不写接收方。

状态目录是 `state/china-consumer-db/`，下面有 `prices/`、`snapshots/`、`notes.jsonl`、`commodities/`、`collection-runs/`。归档是 `research-outputs/china-consumer-weekly/YYYY-MM-DD.md`。`YYYY-MM-DD` 用当周周五（Asia/Hong_Kong），公式是运行日减去 `(weekday - 4) % 7`。周日跑、文件名用周五。导入历史状态只由主助手执行一次，本轮不要运行导入脚本。

两种模式由自动化指令写明。采集只入库，不成稿、不提交、不写快照。成稿提交卡片全文，确认发送后再归档，然后写快照。

正文先给判断。报告正文不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。栏目和证据等级字段保持原样。

## 口径

看消费内部谁在变，不看消费总量好不好。四层都用中位数：

1. 市场 beta：沪深300 与恒生指数，只用来扣除。
2. 消费池对各自基准的超额中位数。
3. 细分超额中位数减去消费池超额中位数。
4. 个股超额减去所在细分的超额中位数。

细分指数：每家先除以自己的市场基准（A 股 `sh000300`，港股 `hkHSI`），再取中位数。A 股和港股放进同一细分时不做汇率折算。窗口涨跌用成分股窗口收益的中位数，窗口是日历日 7 / 28 / 91 / 364。样本少于 4 家不参与排名，标「样本不足」。

覆盖池 214 家，26 个细分，名单在 `references/universe.json` 和 `references/subsectors.json`。A+H 同时上市的公司两条都留，报告里不把同一家当成两个独立证据。汽车整车、白酒以外的农业种植、纯医药不在池内。

技术面六组，一家只进一组，只写公司名：股价创新高且放量；跑赢大盘创新高但股价没创新高；股价创新高但跑赢幅度没创新高；股价创新低；跌破 200 日均线；跑输大盘创新低但股价没创新低。

大宗用新浪连续合约，12 个品种。只写周变动达到 2% 的。涨跌按交易日回溯（5 / 20 / 65 / 250）。纸浆是包装用纸代理。缺口：生鲜乳、瓦楞纸现货、白酒批价。

## 证据

按这个顺序扫：公司披露、AlphaEngine、Guidepoint、网页、社区。D 级不入库。

- A：公司披露，有口径和期间。可以直接进归因。
- B：业绩会、路演或调研里的口头数字，有基数和期间。
- C：专家、渠道、第三方估算，进报告时标「估算」。
- D：转述、无口径、无链接。丢掉。

B 和 C 入库要带 `--cross-check`。本周没有印证就传 `none`，这条只进观察项。印证来自下游、渠道、上游或平台，同一家公司换个说法不算。

进归因要同时有：可核对数字、口径与期间、基数或同比基准。

AlphaEngine：`research(route="alphaengine", operation="search_library", query=..., start=..., end=..., limit=20, params={"sort": "relevance", "industry_names": [...]})`，再 `get_document`。带 query 时 sort 只用 `relevance`。纪要和点评只取最近 2 周，更旧的标「背景，非本周增量」。

Guidepoint：`research(route="guidepoint", operation="search_library", query=..., start=..., end=..., limit=30, params={"keywords": [...], "recency_bias": 50})`。三个角度：价格与批价、单店与单位经济性、渠道与竞争。优先本周，否则最近 2 周并标背景，再没有就写「本周无相关新增访谈」。逐字引用不超过 20 个英文词，并附 source attribution。不写专家姓名。

网页：`web_search(query, start_date=START, end_date=END, time_range="week")`。`topic` 可以不传。

公告：`finance(route="cn-hk", operation="filings", ticker=..., start=..., end=...)`。社区：`community(route="xueqiu", operation="search", query=...)` 或 `community(route="last30days", operation="search", query=..., days=7, plan={"subqueries": [{"search_query": "...", "ranking_query": "..."}]})`。社区内容只作发现，销量、价格和份额用一手来源。

行情命令走腾讯前复权：`proxy.finance.qq.com` 的 qfq kline，报价 `qt.gtimg.cn`。

## 1. 自检

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json self-test --workspace-dir <workspace> --network
```

自检失败就停止。采集失败不提交报告。成稿失败按第 5 节提交一行失败说明。

## 2. 周五与计划

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json anchor --workspace-dir <workspace> --as-of <as-of>
```

`<as-of>` 省略时用香港当天。输出的 `friday` 和 `report_run_id`（`china-consumer-weekly-<friday>`）用于稿件名、归档、快照和提交。

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json plan --workspace-dir <workspace> --as-of <friday>
```

`plan` 分开给出 `price_window` 和 `source_window`。行情窗口终点是 `price_as_of`。信源窗口从上周日（`source_window.start`）到本次采集时刻（`source_window.end_at`，香港时间），覆盖周五盘后与周末。

## 3. 采集

先跑行情和事实入库。这一步不写卡片，不调用 `automation_report`，不跑 `snapshot`。

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json sync-prices --workspace-dir <workspace> --force

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json perf --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json index --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json tech --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json commodities --workspace-dir <workspace> --as-of <friday> --refresh
```

对本周跑赢和跑输最多的细分（各 3 到 5 个）以及技术突破的公司，按信源顺序查原因。每条事实：

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json add-note --workspace-dir <workspace> \
  --subsector baijiu --source-type company_disclosure --evidence-grade A \
  --claim "CLAIM" --source-date <friday> --week-of <friday>
```

B 或 C 级加上 `--cross-check CROSS` 或 `--cross-check none`。专家访谈加上 `--source-type expert_call` 和 `--source-attribution TEXT`。

采集结束只记运行，不写快照：

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json record-collection --workspace-dir <workspace> --as-of <friday> \
  --note "采集完成"
```

## 4. 成稿前的读数

先看对比，再写正文。没有基线就整节不写「对比上期」。

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json diff --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json notes --workspace-dir <workspace> --since <source-start> --limit 0
```

`<source-start>` 用 `plan` 的 `source_window.start`（当周周五往前 5 天的周日）。成稿按这个完整信源窗口读采集记录，周一到周四写入的记录也要进正文。`--limit 0` 读出全部记录；省略时只返回最后 50 条。

`perf` 的 `price_as_of` 要等于本周周五。`requested_as_of` 只是请求日。不等就再跑一次 `sync-prices --force` 和 `perf`。仍然不等，看 `holiday_note` 和 `missing_count`，第一段写 `price_as_of`，全文按实际行情日期叙述。

成稿补扫周日 08:00 采集之后的公告：`finance(route="cn-hk", operation="filings", ticker=..., start=采集时刻, end=成稿时刻)`。命中的用 `add-note` 入库。

卡片按 `templates/weekly-card.md` 写到 `temp/china-consumer-weekly/<friday>-card.md`。全文按 `templates/weekly-report.md` 写到 `temp/china-consumer-weekly/<friday>.md`。卡片最后一节是待验证。不用 Markdown 表格，正文不出现 `$`。

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/china-consumer-weekly/<friday>-card.md --profile card

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/china-consumer-weekly/<friday>.md --profile report
```

`vague_phrase`、`missing_section`、`dollar_sign`、`caliber_label` 清零才提交。`number_without_period` 是提醒。

## 5. 提交

提交物是卡片。在第一行标题之后插入一行 `-# 行情窗口 MM-DD 至 MM-DD ｜ 信源窗口 MM-DD 至 MM-DD ｜ 中位数口径，已剔除市场 beta`，其余不动，然后把这份卡片放进 `text`。`report_run_id` 用 `china-consumer-weekly-<friday>`。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", text=CARD_MARKDOWN)
automation_report(action="status", report_run_id=RUN_ID)
```

只有返回的 `report_run_id` 一致、`content_kind` 为 `text`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才归档全文，再写快照。两条命令用同一组确认参数。未确认时两条都不写。

```bash
python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json archive --workspace-dir <workspace> \
  --file temp/china-consumer-weekly/<friday>.md --as-of <friday> \
  --content-kind text --delivery-confirmed --status sent

python3 .agents/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py \
  --json snapshot --workspace-dir <workspace> --as-of <friday> --write \
  --content-kind text --delivery-confirmed --status sent
```

同一 `report_run_id` 已有不同正文时返回 `content_conflict`，不改正文，也不换第二个标识。`not_automation_run` 时停止，不归档。确认发送后，最终回复只写 `NO_REPLY`。

正文还没保存时，省略 `text`，提交一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="中国消费周报第 N 步失败：原因",
)
```

正文已经保存、投递确定失败时，不再提交 `failure_text`。同一 `report_run_id` 会得到 `content_conflict`。失败通知由平台发给 profile 的 `failure_targets`：确定失败才发，只发一次，已经发送的条目不触发。
