---
name: dalton-us-weekly-movers
description: "无人值守的美股周涨跌幅共性周报：按脚本榜单合并主题、核对催化，并用 automation_report 提交卡片。"
---

# 美股周涨跌幅共性周报

无人值守。不提问，不创建子任务。本轮只做这一份周报。

安装路径是 `.agents/skills/dalton-us-weekly-movers/`。下面的命令用这个目录里的 `scripts/us_weekly_movers_cli.py`。每条写文件的命令都带 `--workspace-dir <workspace>`。`<workspace>` 用本轮工作区根目录，不靠向上查找说明文件来猜。

接收方在任务的 `metadata.report_profile` 里，配置名是 `us-weekly-movers`。卡片和工具参数都不写接收方。

状态在 `state/us-weekly-movers/`：`universe.json`、`themes.jsonl`、`last_scan.json`、`scan_<week_end>.json`。草稿在 `temp/us-weekly-movers/`。确认发送之后，投递原文和明细才写入 `research-outputs/us-weekly-movers/<week_end>.md`。导入历史状态只由主助手执行一次，本轮不要运行导入脚本。

周报正文先直接给判断，再写事实和改判条件。正文不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。栏目、数字、来源和失败说明保持原样。

## 三层分工

- 筛选层是脚本。池子、周涨跌幅、量比、财报周、行业聚类都由 `scan` 算出。模型不重算这些数字。
- 核对层对每个入选主题调用一次 `web_search`。
- 判断层合并聚类、去掉 beta 和单票财报、定性催化、写周报。这层的价值是敢删。

## 固定参数

池子是市值不低于 10 亿美元的普通股，保留 ADR，去掉优先股、权证、单位和票据。周窗口由行情日期决定：上一 ISO 周最后一个交易日收盘，到本周最后一个交易日收盘。涨幅榜和跌幅榜各前 40，周涨跌幅绝对值至少 5%。量比只展示，不作门槛。同一行业至少 3 只才成聚类，同一板块至少 4 只作辅助。涨幅主题最多 3 个，跌幅主题最多 3 个。没有至少 3 只共振，就写「无」。

调参只改命令行：`--min-cap`、`--top-n`、`--min-vol-ratio`、`--min-move`、`--cluster-min`。

自检可以单独跑，失败也不阻止本轮。本轮从刷新池子开始。

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json self-test --workspace-dir <workspace>
```

## 1. 刷新池子

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json universe --workspace-dir <workspace>
```

成功时 `ok` 为 true，并写 `state/us-weekly-movers/universe.json`。拉不到时脚本保留已有文件。文件已在就继续第 2 步，周报末尾写「池子沿用 <as_of>」。文件不在，按第 6 节发一行失败说明。

## 2. 算榜单

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json scan --workspace-dir <workspace>
```

这一步大约数分钟。返回 `ok` 为 true 再往下。产物：

- `state/us-weekly-movers/scan_<week_end>.json` 和 `last_scan.json`
- `temp/us-weekly-movers/<week_end>-candidates.md`：两张前 40 表和行业聚类

`priced_n` 要达到 `universe_n` 的 95%。低于这个比例就按第 6 节处理，不补数，也不写「本周无主题」。赶时间可以加 `--no-earnings`，周报要注明未标财报周。

## 3. 定主题

读 candidates，再读 `recent-themes` 里最近一周：

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json recent-themes --workspace-dir <workspace>
```

按这个顺序：

1. 合并。行业切得细，同一逻辑会散在几个行业。用板块聚类和前 40 全表合成主题，合并后仍至少 3 只。
2. 剔除。单票财报（财报周为「是」）单独不成主题，除非至少 3 只同业因同一读数联动。主题中位涨跌幅与全池中位数同向，并且不到全池中位数 2 倍，按 beta 处理，不立主题。同一主题上周已出现且本周没有新催化，判断写「延续」。
3. 排序。按只数乘中位涨跌幅，涨幅取前 3，跌幅取前 3。
4. 单票。主题之外，周涨跌幅绝对值至少 25% 且不是财报周的单票，最多留 1 行，不展开。没有就整块删掉。

## 4. 核对催化

每个入选主题调用一次 `web_search`。英文 query 带 2 到 3 个代码，加上 `stock rally this week` 或 `shares fall this week`。日期用 `start_date` 和 `end_date`，对应 `week_start` 和 `week_end`。`topic` 可以传 `news`，当前检索实现会忽略它，所以结果还要按发布日期再滤到本周。

```python
web_search(
    query="AAA BBB CCC stock rally this week",
    start_date=WEEK_START,
    end_date=WEEK_END,
    topic="news",
)
```

催化只分五类：公司公告、财报、行业会议或数据、卖方动作、政策。都对不上就写「未见明确催化，按轮动处理」。对不上也不放弃主题，也不引用发布日期不在本周的新闻。每一次调用都超时，按第 6 节处理。

## 5. 写周报并提交卡片

先把正文写到 `temp/us-weekly-movers/<week_end>-delivery.md`。不用 Markdown 表格，不用 `#` 标题。金额写「美元」。区间写「至」。跌幅用 Unicode 减号 `−`。主题之间空一行。编号用 ①②③，不用 `1.`。每个主题一个加粗标题行（主题名、中位涨跌幅、只数和最多 4 个代码），下面两条 bullet，分别是「催化」和「判断」，各一句。

```
📈 **美股周涨跌幅共性｜<week_start> 至 <week_end>**
池 <n> 只（市值 ≥10 亿美元），全池中位 <x>%，上涨占比 <y>%

**▲ 涨幅主题**

**① <主题名>　+<x>%**（<N> 只：A、B、C、D 等）
- 催化（<五类之一>）：<一句>
- 判断：**<新主题 / 延续 / 反转>**，<一句理由>

**▼ 跌幅主题**

**① <主题名>　−<x>%**（<N> 只：…）
- 催化（…）：…
- 判断：**…**，…

**单票提示**
<代码 ±x%，一句>

**下周看点**
<一至两句：哪个主题最可能延续或反转，成立条件>
```

某一侧没有主题，写 `**▲ 涨幅主题**：无` 或 `**▼ 跌幅主题**：无`。没有单票提示就删掉那一块。

然后生成卡片。`report_run_id` 用 `us-weekly-movers-<week_end>`。同一周重试沿用这个标识和同一份卡片字节。

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json render-card --workspace-dir <workspace> \
  --file temp/us-weekly-movers/<week_end>-delivery.md \
  --output temp/us-weekly-movers/<week_end>-card.json \
  --title "美股周涨跌幅共性周报"
```

读出 `card.json` 的对象 `CARD`。调用 `automation_report`，不从 Python 导入，也不用 Bash 调用：

```python
automation_report(action="submit", report_run_id=RUN_ID,
                  material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

`config` 以及用户、工作区、线程、运行和自动化身份由服务器注入。`cards` 只含这一张卡片。

只有返回里的 `report_run_id` 与 `RUN_ID` 一致，`content_kind` 为 `cards`，并且 `ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才把草稿复制到 `research-outputs/us-weekly-movers/<week_end>.md`（投递原文，加上入选主题的代码明细和链接），再追加主题：

```bash
python3 .agents/skills/dalton-us-weekly-movers/scripts/us_weekly_movers_cli.py \
  --json append-themes --workspace-dir <workspace> \
  --file temp/us-weekly-movers/<week_end>-themes.json
```

`themes.json` 形如 `{"themes":[...]}`。每行必有 `week_end`、`side`（`gainers` 或 `losers`）、`label`、`symbols`（至少 3 个）、`catalyst_type`。可有 `median_ret_week` 和 `verdict`。同一 `week_end + side + label` 已在则跳过。

未确认发送时，保留草稿和原卡片，用同一标识和同一份 `cards` 再提交一次。不要把未确认写成已发送，也不要换第二个 `report_run_id`。`content_conflict` 表示这个 `report_run_id` 已经保存了不同正文，不要改卡片。`code` 为 `not_automation_run` 时停止：不归档，不追加主题，最终回复说明本次不是自动化运行，不附周报正文。

确认发送后，最终回复只写 `NO_REPLY`。

## 6. 失败

脚本失败、`priced_n` 低于 `universe_n` 的 95%，或本轮每一次 `web_search` 都超时：不要补数，不要写「本周无主题」，不要归档，不要追加主题。提交一行失败说明，`cards` 省略：

```python
automation_report(
    action="submit",
    report_run_id="us-weekly-movers-<week_end 或当天日期>",
    material="yes",
    failure_text="美股周涨跌周报第 N 步失败：原因",
)
```

失败文本确认发出后，最终回复只写 `NO_REPLY`。工具没能保存这行时，最终回复只留这一行。

## 已知边界

- 行业分类颗粒不均。跨行业主题在第 3 步合并，不直接把聚类当成品。
- 周涨跌幅用未复权 Close。拆股已处理，周内除息忽略。
- 财报周走 `earnings_dates`。拿不到的显示 `?`，按未知处理。
- 量比是本周日均量除以前 20 日均量，只展示。要临时恢复过滤，传 `--min-vol-ratio 1.5`。
- 周四、周五跑到的是不完整周。这个任务按完整交易周使用，定时设在周六。
