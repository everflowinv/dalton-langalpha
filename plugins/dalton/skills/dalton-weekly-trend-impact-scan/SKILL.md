---
name: dalton-weekly-trend-impact-scan
description: "无人值守的趋势影响周扫：找出过去一周三到五项趋势，判断谁承压、谁受益，并用 automation_report 提交纯文本。"
---

# 趋势影响周扫

无人值守。不提问，不创建子任务。不要把中间过程发到任何聊天。

安装路径是 `.agents/skills/dalton-weekly-trend-impact-scan/`。命令用 `scripts/weekly_scan.py`。写文件或校验时带 `--workspace-dir <workspace>`。`<workspace>` 用本轮工作区根目录。

接收方在 `metadata.report_profile`，配置名是 `weekly-trend-impact-scan`。正文和工具参数都不写接收方。

这个任务没有长期状态。草稿放在 `temp/weekly-trend-impact-scan/<report_date>.txt`。发送失败也保留草稿。

窗口和 `report_run_id` 用亚洲/香港时间（Asia/Hong_Kong），即使自动化时区不是香港。`report_run_id` 是 `weekly-trend-impact-scan-<report_date>`。日期、检索窗口和五条 query 都用第 1 步的返回，不要自己另算。

正文先给判断。自己写的句子不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。同一意思的防御性否定句也不用。需要说明边界时，写清证据范围、置信度和会改判的条件。逐字引文、原始报错和固定栏目名保持原样。

## 1. 窗口

```bash
python3 .agents/skills/dalton-weekly-trend-impact-scan/scripts/weekly_scan.py \
  --json window
```

记下 `start_iso`、`end_iso`、`start_date`、`end_date`、`report_date`、`report_run_id` 和 `queries`。窗口是运行时刻向前 7 天。`web_search` 只按日历日过滤，所以每条结果还要用 `start_iso` 到 `end_iso` 再核对一次。落在开始日、但早于 `start_iso` 的文章不用。

## 2. 检索过去一周

按 `queries` 的顺序逐条调用。五条的范围是 AI与科技产品、政策监管、中美贸易、行业竞争与需求、利率商品与地缘。重点是中概股、港股和 A 股。某一项趋势还要核对原文或公司影响时，可以再搜，日期仍用这一组。不要把日期写进 query。

```python
web_search(query=QUERY, start_date=START_DATE, end_date=END_DATE)
```

不要传 `freshness`、`count`、`time_range`、`topic`、`country`、`language`、`domain_filter`、`search_lang` 或 `ui_lang`。这个工具不接受 `count`，返回条数有固定上限。某一条太宽、结果不够时，把 query 写得更具体再搜一次，日期不变。

逐条核对发布日期。日期缺失，或日期不在 `[start_iso, end_iso]` 里的，不用来支持趋势。五次范围检索全部失败时，不编造趋势，按第 5 节提交一行失败说明。

## 3. 价值链和行情

从核对过日期的材料里选变化最大的 3 到 5 项。证据只够支撑少于 3 项时，不要用弱证据凑数，按第 5 节提交一行失败说明。优先中小盘，证据门槛不因此降低。

每项趋势都看收入、定价权、份额、成本和商业模式里实际会动的环节。没有依据的环节不要写成已经受到影响。影响不明确就写「不明确」，不要配成一边承压、一边受益。

相关股票用 `yf_price` 核对近期表现。港股用数字加 `.HK`（如 `0700.HK`），A 股用 `.SH` 或 `.SZ`，美股用不带交易所后缀的代码。两地上市分开写，不把两地涨跌幅合成一个数。

```python
yf_price.get_stock_history(ticker=TICKER, period="1mo", interval="1d", adjusted=False)
yf_price.get_stock_history(ticker=TICKER, period="1mo", interval="1d", adjusted=True)
yf_price.get_dividends_and_splits(ticker=TICKER)
```

多只股票的日线可以改用一次 `yf_price.get_multiple_stocks_history`，未复权和复权各一次。分红和拆股仍逐只调用。行情口径写在「近期股价」里：上市地常规交易时段的收盘价、起止日期、币种，以及未复权价和复权价是否一致。复权、拆股或交易日缺失有疑问时，不下「是否已经反映」的结论，写明疑问。工具报错或没有该股票的记录时，写「行情缺失」，不用新闻页面上的价格顶上。

因该趋势下跌达到或超过 20% 时，写明「可能已部分反映」，不要写成做空建议。

## 4. 成稿

用纯文本，不用 Markdown 标题。公司名用中文，ticker 保留英文。区分事实、传导判断和交易观察，句子里写出「事实」和「传导判断」。近期股价这一行是交易观察。不用「受害者」「赢家」「最佳机会」，不写确定性买卖指令。中文按主语、动作、结果来写。不写中英双标题，不写空话，不硬凑三点。来源媒体和发布日期写在对应的事实句里，不另起模板外的栏目。

某一侧没有明确公司就写「暂无」，近期股价也写「暂无」。这一项仍然保留，报告照常提交。

```text
📊 本周趋势影响扫描 — YYYY-MM-DD

趋势 1：[一句话写清发生了什么]
承压方向：[公司]（[Ticker]）— [证据和传导路径，1—2 句；没有明确标的就写「暂无」]
近期股价：[窗口和涨跌幅；是否可能已反映]
受益方向：[公司]（[Ticker]）— [证据和传导路径，1—2 句；没有明确标的就写「暂无」]
近期股价：[窗口和涨跌幅；是否可能已反映]
主要不确定性：[一条]

本周更值得跟踪的方向：
- 偏空观察：[ticker 或「暂无」] — [成立条件]
- 偏多观察：[ticker 或「暂无」] — [成立条件]
```

标题日期用 `report_date`。同样的六行再写 2 到 4 遍，总共 3 到 5 项，编号从 1 连续排到最后一项。栏目和顺序保持上面的样子。写成 `temp/weekly-trend-impact-scan/<report_date>.txt` 后校验：

```bash
python3 .agents/skills/dalton-weekly-trend-impact-scan/scripts/weekly_scan.py \
  --json check --workspace-dir <workspace> \
  --file temp/weekly-trend-impact-scan/<report_date>.txt \
  --date <report_date>
```

校验失败就改草稿再校验。改完后仍失败，不提交这份正文，改提交一行失败说明。

## 5. 提交

`BODY` 是校验通过的草稿全文。

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    text=BODY,
)
automation_report(action="status", report_run_id=RUN_ID)
```

返回里的 `report_run_id` 一致、`content_kind` 为 `text`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent` 时，最终回复只写 `NO_REPLY`。未确认就用同一标识和同一份正文再提交。同一 `report_run_id` 已保存不同正文时返回 `content_conflict`，不要改正文，也不要换第二个标识。`not_automation_run` 时停止，最终回复说明本次不是自动化运行，不附扫描正文。

失败时不传 `text`，一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="趋势影响周扫第 N 步失败：原因",
)
```

失败说明确认发送后，最终回复只写 `NO_REPLY`。没能保存时，最终回复只留这一行。不要发第二份正文。

最后一遍只校正语言和标点，不得改动上面规定的栏目、顺序、条数、20% 阈值、行情口径、校验步骤、失败处理和投递方式。
