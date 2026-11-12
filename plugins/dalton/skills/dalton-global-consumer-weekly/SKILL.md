---
name: dalton-global-consumer-weekly
description: "无人值守的美国与日本消费周报：按本币超额、跨市场背离与证据门槛采集并成稿，并用 automation_report 提交纯文本。"
---

# 美国与日本消费周报

无人值守。不提问，不创建子任务。安装路径是 `.agents/skills/dalton-global-consumer-weekly/`。命令用 `scripts/global_consumer_weekly.py`，写文件时带 `--workspace-dir <workspace>`。

接收方在 `metadata.report_profile`，配置名是 `global-consumer-weekly`。技能正文和工具参数都不写接收方。

状态目录是 `state/global-consumer/`：`notes.jsonl`、`snapshots/`、`checkpoints.jsonl`，行情缓存 `prices/`，财报缓存 `earnings/`。归档是 `research-outputs/global-consumer-weekly/YYYY-MM-DD.md`。日期用当周周五（Asia/Hong_Kong）。周日跑、文件名用周五。导入历史状态只由主助手执行一次，本轮不要运行导入脚本。

两种模式由自动化指令写明。采集只入库，不成稿、不提交、不写快照、不归档。成稿提交卡片，确认发送后再归档全文，然后写快照。

正文先给判断。报告正文不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。

## 口径

209 家（美国 133、日本 76），22 个细分，名单在 `references/universe.json`。汽车整车不在池内。相对强弱用本币对本地基准：美国 `^GSPC`，日本 `^N225`。美元收益单独一列，不混进相对强弱。中位数先按市场取。窗口是日历日 7 / 28 / 91 / 364，对齐到当周周五。各市场用自己最后一个不晚于该周五的交易日。

两地读数差 = 美国 alpha 减日本 alpha。alpha = 细分超额中位数减本地消费池超额中位数。正文写「相对本地消费股」，不用 `divergence_raw_w1`。一侧少于 3 家不作方向（`sufficient`）。一侧少于 4 家不出两地读数（`thin`），只作观察。`cross_market` 为 false 的细分只出单市场。

深度归因只做 `plan --focus` 选出的 3 到 5 个细分，默认 5。到期的待验证条目不受这个限制。

技术面七组，一家只进一组：股价创新高且放量；股价与跑赢幅度同时创新高；跑赢大盘创新高但股价没创新高；股价创新高但跑赢幅度没创新高；股价创新低；跌破 200 日均线；跑输大盘创新低但股价没创新低。这一层看 `tech-survival` 的 `forward`。`survival_pct` 只描述信号是否延续。

行情用 yfinance，`auto_adjust=False`。汇率：USDJPY、EURUSD、USDCNY、DXY。16 个美元计价大宗。缺口：PET 与树脂、瓦楞纸、运价。大宗只作成本方向。

## 证据

与中国消费周报同一套 A/B/C/D。B 和 C 要 `--cross-check`，找不到传 `none`。逐字引用不超过 20 词，专家访谈要 source attribution。D 级不入库。

点名公司的 AlphaEngine 只用 `names --subsector` 给出的中文名，不传 `industry_names`，也不用纯英文 query 配 `foreign_report`。需要外文研报类别时，中文名和 `params.document_categories=["foreign_report"]` 一起用。没有公司名的主题检索才用 `industry_names`。带 query 时 sort 只用 `relevance`。查不到写「该库未检索到」。AlphaEngine 撞额度上限时不重试超过两轮，照实记录。

```python
research(route="alphaengine", operation="search_library", query=NAME_ZH, start=START, end=END,
         limit=20, params={"sort": "relevance", "document_categories": ["foreign_report"]})
research(route="guidepoint", operation="search_library", query=QUERY, start=START, end=END,
         limit=30, params={"keywords": ["running shoe"], "recency_bias": 50})
web_search(query=QUERY, start_date=START, end_date=END)
community(route="last30days", operation="search", query=QUERY, days=7,
          plan={"subqueries": [{"search_query": QUERY, "ranking_query": QUERY}]})
finance(route="sec", operation="list-filings", ticker=TICKER, form="8-K")
```

last30days 只在实物消费品细分上跑。不用 cashtag，不查 StockTwits。Reddit 未覆盖，不绕道。社区证据最高 C 级，只作品类旁证，不作数字来源。社区内容只作发现，销量、价格和份额用一手来源。

Guidepoint 优先本周，否则最近 2 周并标「背景，非本周增量」。再没有就写「本周无相关新增访谈」。三个角度：份额与价格、单店与渠道经济性、竞争与新品。`recency_bias` 只是排序权重。返回的是问答摘录。

日股披露用 `jp-disclosures`。TDnet 大约保留 31 天。`guidance` 和 `monthly_sales` 的标题没有数字，用 `WebFetch(url=PDF_URL, prompt="提取业绩预想或月次销售的数字、口径和期间")` 打开 PDF。日股财报日期来自 Yahoo 时标「指示性」。

## 1. 自检

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json self-test --workspace-dir <workspace> --network
```

自检失败就停止。采集失败不提交报告。成稿失败按第 5 节提交一行失败说明。

## 2. 周五

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json anchor --workspace-dir <workspace> --as-of <as-of>
```

`report_run_id` 是 `global-consumer-weekly-<friday>`。行情命令的 `--as-of` 都传这个周五。日股披露不传 `--as-of`，截止日期是香港时间当天。

## 3. 采集

不写报告，不提交，不跑 `snapshot`，不归档。

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json sync-prices --workspace-dir <workspace> --force

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json perf --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json index --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json tech --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json fx --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json commodities --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json plan --workspace-dir <workspace> --as-of <friday> --focus 5

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json names --workspace-dir <workspace> --subsector sportswear

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json earnings --workspace-dir <workspace> --as-of <friday> --lookback 10 --days 21

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json jp-disclosures --workspace-dir <workspace> --days 7

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json add-note --workspace-dir <workspace> \
  --subsector sportswear --tier A --type company_disclosure \
  --claim "CLAIM" --source "10-Q" --source-date <friday> --as-of <friday>
```

B 或 C 级加 `--cross-check CROSS` 或 `--cross-check none`。然后看到期项，采集阶段只列出，结案放在成稿：

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json checkpoints --workspace-dir <workspace> --as-of <friday>
```

## 4. 成稿

先结案到期的待验证，再取当周周五收盘。`due_now` 每一条都要 `resolve-checkpoint`。`confirmed` 和 `falsified` 给 `--evidence`。`unresolved` 给 `--note`。

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json resolve-checkpoint --workspace-dir <workspace> --as-of <friday> \
  --id <id> --result unresolved --note "NOTE"

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json perf --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json index --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json tech --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json tech-survival --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json fx --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json commodities --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json plan --workspace-dir <workspace> --as-of <friday> --focus 5

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json notes --workspace-dir <workspace> --week <friday> --full

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json diff --workspace-dir <workspace> --as-of <friday>
```

`plan` 分开给出 `price_window` 和 `source_window`。信源窗口从上周日到本次采集时刻（香港时间）。成稿使用上面 `notes --week <friday> --full` 读出的采集记录；省略 `--full` 时不返回正文。

`perf` 的 `price_as_of` 里两个市场的日期都要等于本周周五。`requested_as_of` 和 `as_of_anchor` 只是请求的周五。不等就再跑一次 `sync-prices --force` 和 `perf`。仍然不等，看 `holiday_note` 和 `missing_count`，第一段写实际日期。

补扫采集之后的增量：`jp-disclosures --days 1`。不传 `--as-of`，截止日期是香港时间当天，不折成周五。美股 8-K。命中的用 `add-note` 入库。行情命令仍传周五。

卡片八节按 `templates/card.md`，顺序固定，最后一节是待验证，不设细分指数节。全文按 `templates/full-report.md`。专家、成本和观察写不出时分别写「本周无相关新增访谈」「本周大宗成本无显著变动」「本期无降级事项」。正文不出现 `$`，不用 Markdown 表格。

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/global-consumer-weekly/<friday>-card.md --profile card

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/global-consumer-weekly/<friday>.md --profile full
```

阻断项为 0 才提交。新的待验证用 `add-checkpoint`，`--due` 要晚于本期周五：

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json add-checkpoint --workspace-dir <workspace> --as-of <friday> \
  --subsector sportswear --company "NAME" --market US \
  --claim "CLAIM" --metric "METRIC" --threshold "THRESHOLD" \
  --due <due> --source "SOURCE"
```

## 5. 提交

提交物是卡片，原样放进 `text`。全文在确认发送后归档。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", text=CARD_MARKDOWN)
automation_report(action="status", report_run_id=RUN_ID)
```

确认条件：`report_run_id` 一致，`content_kind` 为 `text`，`ok` 为 true，`delivery_confirmed` 为 true，`status` 为 `sent`。先归档全文，再写快照。未确认时两条都不写。

```bash
python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json archive --workspace-dir <workspace> \
  --file temp/global-consumer-weekly/<friday>.md --as-of <friday> \
  --content-kind text --delivery-confirmed --status sent

python3 .agents/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py \
  --json snapshot --workspace-dir <workspace> --as-of <friday> --write \
  --content-kind text --delivery-confirmed --status sent
```

`content_conflict` 时不改正文，也不换第二个标识。`not_automation_run` 时停止，不归档。确认发送后，最终回复只写 `NO_REPLY`。

正文还没保存时，省略 `text`，提交一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="全球消费周报第 N 步失败：原因",
)
```

正文已经保存、投递确定失败时，不再提交 `failure_text`。同一 `report_run_id` 会得到 `content_conflict`。失败通知由平台发给 profile 的 `failure_targets`：确定失败才发，只发一次，已经发送的条目不触发。
