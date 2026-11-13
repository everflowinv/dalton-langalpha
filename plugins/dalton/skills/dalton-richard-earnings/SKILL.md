---
name: dalton-richard-earnings
description: "无人值守的 Richard 财报日期周扫：按 105 家名单核查官方披露日期，写入日历并提交纯文本汇总。"
---

# Richard 财报日期周扫

无人值守。不提问，不创建子任务。不调用 `message`。本轮只做这一次财报日期核查。

模型用 `muse-spark-1.3-oauth`。主任务是每周六 07:00（America/New_York）。Dalton 没有单任务超时。75 分钟门槛用 `progress` 记下的开始时间判断。`report_run_id` 直接用脚本返回值，格式 `richard-earnings-<美东日期>`。失败文案只用「失败文案」一节里的原句。

接收方在自动化配置的 `metadata.report_profile`（`richard-earnings`）中定义。技能正文和 `automation_report` 参数都不写接收方。

安装路径是 `.agents/skills/dalton-richard-earnings/`。命令用 `scripts/richard_earnings.py`。每条命令都带 `--workspace-dir <workspace>` 和 `--json`。`<workspace>` 是本轮工作区根目录。

日历写入走 `research(route="gcal")`。列事件、建事件、改期和删除都由脚本完成。模型不直接调用 `research(route="gcal")`，也不在宿主机上运行 `gws`。

名单共 105 家：A 组 Core（核心关注）27 家，B/C/D 组 Watch（观察名单）78 家，含美股、港股和 A 股。这份名单和日历与 `dalton-earnings-calendar` 分开维护。

## 状态路径

都相对工作区。不要写绝对路径，不要用上级目录，不要把这些路径做成符号链接。

- `state/richard-earnings/watchlist.json`：`confirmed`（已确认待发）/ `pending`（待披露）/ `reported`（已发布历史）台账。台账已预先存在，本技能只读写、不初始化导入。
- `state/richard-earnings/scans/<美东日期>/<组>.jsonl`：当日扫描记录，一组一个文件。
- `state/richard-earnings/scans/<美东日期>/inbox/<ticker>.json`：单家公司待传给 `record` 的输入文件。由模型写入。
- `state/richard-earnings/scans/<美东日期>/roll.json`：滚动结果。
- `state/richard-earnings/scans/<美东日期>/started.json`：本轮开始时间。
- `state/richard-earnings/scans/<美东日期>/apply.json`：日历写入结果。

`<美东日期>` 是 America/New_York 的公历日，格式 `YYYY-MM-DD`。

## 1. 自检

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json self-test --workspace-dir <workspace>
```

- `ok` 为 false 且 `calendar_ok` 为 false：停止。提交「失败文案」里的「日历自检未通过」。
- `ok` 为 false 且 `calendar_ok` 为 true：停止。提交「失败文案」里的「脚本没有完成扫描或写入」。
- `ok` 为 true 且 `watchlist_exists` 为 false：停止。提交「失败文案」里的「台账还没有导入」。
- 上面三项都通过后才继续。`report_run_id` 用返回值。

## 2. 滚动

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json roll --workspace-dir <workspace>
```

先做这一步。`confirmed` 里的 `date` 以业绩发布日为准，电话会日期不参与。脚本把日期早于美东今天的条目移入 `reported`，保留 `date`、`timing`、`calendar_event_id` 和原对象，并在 `pending` 登记或更新 `note`。该代码已在 `pending` 时只更新 `note`，不另加一条。没有过期条目时不改 `watchlist.json` 的 `updated`。

`note` 写成「下一期 <下一期标签>」。脚本能从原标签推出下一期时用推出的标签；对不上时用原标签。两种都带「下一期 」前缀。

`ok` 为 false：停止，提交「失败文案」里的「脚本没有完成扫描或写入」。

## 3. 顺序

运行 `list` 后、核查第 1 家之前，先运行一次 `progress`。这次调用写入 `started.json`，并取得 `remaining` 和 `next`。

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json list --workspace-dir <workspace>
```

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json progress --workspace-dir <workspace>
```

`list` 返回 105 家名单、分组和 `note`（财年结账月、披露频率）。它不返回 `next`，也不按待披露、已确认、其余来排序，也不写 `started.json`。

核查按 `progress` 的 `remaining`：先做完 A 组，再做 B、C、D。每一组里先是 `pending`，再是 `confirmed` 里日期未过的公司，然后是该组其余还没记录的公司。从 `next` 开始，按这个队列逐家做第 4 节。不创建子任务。

之后每完成 10 家，或要检查 `gate_open` 时，再用第 5 节的同一条命令运行 `progress`。`gate_open` 为 true 时停止检索，进入第 6 节。

## 4. 逐家核查

对 `next` 指向的一家公司做检索。每家最多 2 次 `web_search`。`start_date` 是美东今天减 180 天，`end_date` 是美东今天。只传 `query`、`start_date`、`end_date`。一个 `query` 只写一家公司。

```python
web_search(
    query="Affirm AFRM investor relations next earnings date",
    start_date="2026-05-18",
    end_date="2026-11-14",
)
```

日期范围外的结果不采用。

判定：

- 官方 IR（投资者关系，Investor Relations）新闻稿、SEC（美国证监会）文件、HKEX（香港交易所）公告或上交所预约披露、正式公告写明一个不早于今天的日期：`announced`，`confidence` 为 `high`。
- 至少 2 个独立权威聚合站给出同一个具体未来日期：`announced`，`confidence` 为 `medium`。只有聚合站、彼此对不上时，记 `not_announced`。
- 本季业绩已经发布：`already_reported`。
- 其余情况：`not_announced`。

`web_search` 返回 `ok` 为 false、`error` 或权限拒绝时，`record` 的 `status` 写 `scan_failed`，失败原因写在 `notes`。`scan_failed` 与 `not_announced` 分开记。`summary` 的「仍待官宣」只数 `not_announced`，「扫描失败」另列公司。

`earnings_date` 只记业绩发布日（earnings release date），不记电话会日期（earnings call date）。不早于美东今天。材料里没有日期时写 `null`，不补日期。

`timing`：`BMO` 是盘前（Before Market Open），`AMC` 是盘后（After Market Close）。`status` 为 `announced` 时，`timing` 必须是 `BMO`、`AMC` 或 `unknown`。公告只写了日期、没写盘前或盘后时填 `unknown`，A 股只有预约披露日时同样填 `unknown`。这两种情况都不要填 `null`。`not_announced`、`already_reported`、`scan_failed` 没有时段时，`timing` 写 `null`。

港股官方来源是 HKEX 董事会会议通告或公司公告。多数港股只披露中期和全年业绩。季度经营数据、季度业务更新不记为财报业绩。A 股官方来源是公司公告和上交所预约披露或正式公告。

填写 `quarter_label` 时对照两处备注：`list` 返回的 `note`（财年结账月和披露频率），以及 `watchlist.json` 里该公司 `pending` 条目的 `note`（待查的下一期标签）。`Q` 是日历季度，`FQ` 是财季（Fiscal Quarter），`FY` 是财年（Fiscal Year）。港股的中期、全年业绩按公告原文写。

官方文件核实不计入上面的 2 次 `web_search`。美股调用 `finance(route="sec", ...)`，港股和 A 股调用 `finance(route="cn-hk", ...)`。只在 `cn-hk` 的 `ticker` 参数里去掉 `.HK` / `.SH`：港股补足 5 位数字（`0700.HK` 传 `"00700"`），A 股写 6 位数字（`601100.SH` 传 `"601100"`）。`start` 用今天减 180 天，`end` 用今天。写入 `inbox/<ticker>.json` 和调用 `record` 时，`ticker` 用 `list` 名单里的原始代码，例如 `0700.HK`、`601100.SH`。

```python
finance(route="sec", operation="list-filings", ticker="AFRM", form="8-K")
finance(
    route="cn-hk",
    operation="filings",
    ticker="00700",
    start="2026-05-18",
    end="2026-11-14",
)
```

每家查完立刻 `record`。先把这一家的 JSON 对象写到工作区里的相对路径，再调用：

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json record --workspace-dir <workspace> \
  --input state/richard-earnings/scans/<美东日期>/inbox/<ticker>.json
```

字段必须齐全：`ticker`、`name`、`status`、`earnings_date`、`timing`、`quarter_label`、`confidence`、`source_url`、`evidence`、`notes`、`group`。除了 `announced` 的 `timing`，没有值的字段写 `null`。`status` 只取 `announced`、`already_reported`、`not_announced`、`scan_failed`。`announced` 的 `timing` 只取 `BMO`、`AMC`、`unknown`；其他状态的 `timing` 可以是这三个值或 `null`。`ticker` 必须在名单里，`group` 必须和名单一致。`announced` 要有不早于今天的日期，且 `confidence` 为 `high` 或 `medium`。返回 `already_recorded` 时不要再写一遍。`status` 必须是字符串；`timing` 和 `confidence` 是字符串，或在允许时为 `null`。写成数组或对象时 `record` 返回校验错误，不写入 jsonl。

## 5. 75 分钟门槛

命令与第 3 节的 `progress` 相同。第 3 节那一次已经把开始时间写入 `started.json`，之后不覆盖。每完成 10 家，或要检查 `gate_open` 时再运行：

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json progress --workspace-dir <workspace>
```

`gate_open` 为 true 只在两种情况出现：`unique` 等于 105，或距离 `started_at` 已满 75 分钟。

两者都还没到：继续下一家，不输出汇总，不轮询，不 sleep。`invalid_lines` 是当日没通过 `record` 校验的行，这里报全量。这些行不计入 `unique`，该公司仍留在 `remaining`，可以重新 `record`。`scan_failed` 也不计入 `unique`，仍留在 `remaining`，之后可以用有效结果再次 `record`。

`unique` 到 105，或满 75 分钟仍未查完：停止检索，执行第 6 节 `apply`，再执行第 7 节 `summary`。`progress` 的 `remaining` 含有 `scan_failed`，用来继续核查；不要把这份名单抄进汇总。`summary` 的「未完成」只列当日 jsonl 里还没有任何记录的公司，并且只在满 75 分钟后出现。已经记成 `scan_failed` 的公司在「扫描失败」里，不在「未完成」里。

`apply` 不受这 75 分钟限制。查完或到达门槛后都必须执行 `apply`。

## 6. 写入日历

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json apply --workspace-dir <workspace>
```

查完，或 `gate_open` 为 true，都执行这一次 `apply`。脚本读取当日 jsonl，写入日历并更新台账。模型不改日历。

然后执行第 7 节 `summary`。跳过项、扫描失败、未完成名单和 `errors` 由 `summary` 写入返回的 `text`。

## 7. 汇总

```bash
python3 .agents/skills/dalton-richard-earnings/scripts/richard_earnings.py \
  --json summary --workspace-dir <workspace>
```

`text` 用返回的 `text`，原样提交，不改数字和栏目。`material` 为 `yes`。`report_run_id` 用返回值。`apply` 的 `ok` 为 false 或 `errors` 非空时，仍然提交这段 `text`，不要改用 `failure_text`。

```python
automation_report(
    action="submit",
    report_run_id="richard-earnings-2026-11-14",
    material="yes",
    text="<summary 返回的 text>",
)
```

返回的 `text` 由脚本生成。无变化、没有扫描失败、且 75 分钟门槛尚未留下未完成名单时，是一行「N 家已确认 / M 家未披露」。其余情况第一行是「已核查 N/105」或「日历未写入：…」，后面是日历变动、跳过、滚动、新增或改期、`already_reported`、仍待官宣、扫描失败、未完成和 `errors`。

还没运行 `apply` 时，`text` 是「日历未写入：apply 没有运行」。提交这段 `text`，不另编数字。

`summary` 没有返回 `text`：提交「失败文案」里的「脚本没有完成扫描或写入」。

## 失败文案

下列情况调用 `automation_report`，省略 `text`（`text` 与 `failure_text` 互斥）。`failure_text` 只用对应原句。`material` 为 `yes`。`report_run_id` 直接用脚本返回的完整值。

```python
automation_report(
    action="submit",
    report_run_id="richard-earnings-2026-11-14",
    material="yes",
    failure_text="<失败文案里的对应原句>",
)
```

- 日历自检未通过：「Richard 财报日期周扫失败：日历自检没有通过」
- 台账还没有导入：「Richard 财报日期周扫失败：watchlist 还没有导入」
- 脚本没有完成扫描或写入：「Richard 财报日期周扫失败：扫描或写入没有完成」

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
