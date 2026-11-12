---
name: dalton-bullbear-tracker
description: "无人值守的公司日常多空跟踪（MSFT、META、AMZN、GOOGL 共用）：读工作底稿和本轮材料，判断重要变化；有重要变化时生成卡片，并在接收方确认发送后本地提交（commit）；无重要变化时本地提交（commit）并静默。"
---

# 公司日常多空跟踪（`dalton-bullbear-tracker`）

无人值守。不提问，不创建子任务，20 分钟内结束。股票代码（ticker）从任务指令取得，只接受 MSFT、META、AMZN、GOOGL。每轮先读 [共用规则](references/common-rules.md) 和本公司的 `references/<ticker小写>-rules.md`。研究判断、工作底稿章节和卡片写法以这两份规则为准。

工作底稿只有 `research/tracker/thesis.md`。导入档案 `research/tracker/imported-<ticker小写>/thesis.md` 只读。工作区根目录的财报 `thesis.md` 和 `data/thesis.json` 保持原样。工作底稿不存在时，说明导入尚未完成，停止本轮；不要改用财报底稿继续研究。

接收方配置在任务的 `metadata.report_profile` 里，配置名（profile）是 `<ticker小写>-tracker`。卡片和工具参数都不写接收方或目标地址。

下面的命令使用沙箱里的技能目录 `.agents/skills/dalton-bullbear-tracker/scripts/`。本轮上下文如果给出了该技能目录的绝对路径，就用那个目录下的同名脚本。采集只用这个目录里的 `collect.py`。每条命令都带 `--ticker <TICKER>`。

## 1. 先恢复未完成的运行

读工作底稿，确认文件存在。然后列出尚未写入 `research/tracker/updates.log` 的运行：

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/state.py open-runs \
  --ticker <TICKER> \
  --workspace-dir <workspace>
```

返回按 `run_id` 从新到旧排列，已写入 `updates.log` 的运行不在其中。选择时跳过已经确认发出的失败通知记录（`content_kind` 为 `failure_text`）。其余取最新一条，本轮不再采集第二个数据包。

选中的一条 `research_pending`（待研究）为 true 时，采集已经完成，还没有评审恢复记录（`review.json`）。沿用该条的 `run_id` 和 `packet_json`，从第 3 节继续读这个数据包并做研究。不要编造 `material`（是否有重要变化），不要调用报告工具，也不要把它当成已经确认发送。

已有 `review.json` 时，沿用原来的 `run_id` 和 `report_run_id`。

`review.json` 可以先记下 `card_json`（卡片相对路径），再构造卡片。恢复时先看这个相对路径是不是工作区内的普通文件。文件还不存在，或该路径是符号链接时，继续这一轮的研究和卡片构造，沿用同一个 `run_id` 和 `report_run_id`。不要读取这张还不存在的卡片，也不要提交报告（submit）。

`material` 为 `no`：不调用报告工具，直接按第 6 节本地提交（`commit --material no`）。

review 的 `content_kind` 为 `failure_text` 时，不要本地提交（`commit --material yes`）。这条记录可以没有 `packet.json`；没有数据包时不要补造。失败文本的确认同样要求返回里的 `report_run_id` 与 review 完全一致，并且 `content_kind` 明确为 `failure_text`，同时 `ok`、`delivery_confirmed` 和 `status` 为 `sent`。对上之后，`seen.json` 仍保持原状，这条记录不挡住新的采集，也不表示本轮研究已经处理完成。同一份报告还没确认时，用同一个 `report_run_id` 再提交一次失败文本。标识不一致或缺字段时保持原状，不把失败文本当成已结束。

卡片文件已经存在时，不重写已有卡片。调用 `automation_report` 的 `status` 读取这一份 `report_run_id`，再按 review 和返回只补投递：

- 卡片文件已在，但这一份报告的 `delivery_confirmed` 还不是 `true`，或 `status` 为 `pending`、`unknown`、`failed`：保留数据包、工作底稿和原卡片字节。用同一个 `report_run_id` 和完全相同的 `cards` 再提交报告（submit）一次，并用 `status` 核查。
- 接受卡片确认前，返回里的 `report_run_id` 必须与 review 的 `report_run_id` 完全一致，并且 `content_kind` 明确为 `cards`。同时 `ok` 为 `true`、`delivery_confirmed` 为 `true`、`status` 为 `sent` 时，卡片保持原字节，只本地提交（`commit --material yes`）。缺 `report_run_id`、标识不一致，或 `content_kind` 不是 `cards` 时，保持原状，不本地提交（commit），`seen.json` 不变。
- 本地把发送记成 `sent`，但还没有上述同一份报告的结果时，只调用 `status`，先不要本地提交（commit）。
- 工具返回的 `code` 为 `not_automation_run`：这不是自动化运行。不要本地提交（commit），不要另找发送办法。最终回复说明本次不是自动化运行，不附卡片正文。

已确认的 `failure_text` 不恢复成卡片，也不本地提交（commit）。跳过它之后若还有未完成运行，恢复其中最新的一条。一条都没有时，进入采集。

## 2. 采集

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/collect.py collect \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --since <YYYY-MM-DD> \
  --run-id <YYYYMMDD-HHMM>
```

`--since` 用上一份 `research/tracker/last_run.json` 的 `until`；没有上一份时可以省略。`--run-id` 用美东时间 `YYYYMMDD-HHMM`。记下返回的 `run_id`、相对路径 `packet_json` 和 `packet_md`。运行目录是 `research/tracker/<run_id>/`。

完整数据包已存在时，采集程序会恢复该数据包。恢复结果不是失败，不要再采集一次。采集失败可以重试一次。两次都失败时，不要本地提交（commit），`seen.json` 保持原状，按第 7 节发失败报告并结束。

## 3. 读材料并判断

按研究规则读完工作底稿、`packet.md` 和其中的摘录文件（excerpts）。执行数据包里的固定网页查询，用 WebSearch 和 WebFetch。即使内部研究材料没有新增，也要完成新闻、X 和待核实线索。核查写到 `research/tracker/<run_id>/news.md`，保留标题、来源、日期、URL 和检索/原文状态。页面打不开就换来源；仍只有一个来源时写清归属。

AlphaEngine 先看摘要，全文篇数和停止条件按本公司规则，`doc_id` 保持完整。SEC 新公告核对 8-K 条目号（item）、99.1 新闻稿附件和本公司规则里的 10-Q 关键词。X 帖子先判断作者身份，再更新专家记录、观点池和待核实线索。

用编辑工具修改工作底稿里有变化的章节。然后判断 `material`。

保存本轮评审恢复记录（`review.json`）。记录只含运行标识、判断、工作底稿路径和哈希、卡片路径和发送状态，不写凭据、工具完整返回或第二份正文。有重要变化时，可以先记下即将写入的卡片路径，再构造卡片。

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/state.py save-review \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --material yes \
  --summary "<一句话>" \
  --report-run-id <run_id> \
  --card-json research/tracker/<run_id>/card.json \
  --send-status pending \
  --content-kind cards
```

`material` 为 `no` 时用 `--material no --send-status not_required`，可以不传 `--card-json`。工作底稿哈希由脚本按 `research/tracker/thesis.md` 计算。

同一条 `review.json` 再次保存时，`run_id`、`report_run_id`、已写下的 `card_json` 和已写下的 `content_kind` 保持不变。可以只把 `send_status` 改成当前发送状态。传入另一份 `report_run_id` 或另一张卡片路径时，命令失败，原文件字节不变。这样改发送状态不会换成另一份报告。

本轮新作者用以下命令写入账号池，最多新增 3 位：

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/state.py x-pool add \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --handle <handle> \
  --tickers <TICKER> \
  --reason "<身份与依据>"

python3 .agents/skills/dalton-bullbear-tracker/scripts/state.py x-pool remove \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --handle <handle> \
  --reason "<移出原因>"
```

没有 `run_id` 或没有 `packet.json` 时不要改账号池。

## 4. 无重要变化

不调用报告工具。`material=no` 不会形成发送确认，不要等投递。第 3 节保存恢复记录时已经把 `send_status` 记为 `not_required`。确认这一点后，按第 6 节本地提交（`commit --material no`）。最终回复只写 `NO_REPLY`。

## 5. 有重要变化

按研究规则写 `research/tracker/<run_id>/card.md`。构造卡片：

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/render_card.py \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --file research/tracker/<run_id>/card.md \
  --output research/tracker/<run_id>/card.json
```

脚本只写卡片文件，不联网。正文超过 3,000 字、含图片或表格时会拒绝且不截断。同一运行已有卡片时，只有内容一致才复用；内容不同则保留原字节。已保存或已发送的卡片不要改写。

读出 `card.json`，得到一个对象 `CARD`。直接调用 `automation_report` 工具，不要从 Python 脚本导入，也不要用 Bash 调用这个工具：

```python
automation_report(action="submit", report_run_id=RUN_ID,
                  material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

`config` 以及用户、工作区、线程、运行和自动化身份由服务器注入，调用时不传。`cards` 是只含这一张卡片的列表。

返回是 JSON 字符串。只有返回里的 `report_run_id` 与 review 的 `report_run_id` 完全一致，`content_kind` 明确为 `cards`，并且 `ok` 为 `true`、`delivery_confirmed` 为 `true`、`status` 为 `sent`，才可以本地提交（`commit --material yes`）。缺字段或标识不一致时保持原状，不本地提交（commit），`seen.json` 不变。`ok` 或 `created` 只说明报告已保存。`status` 只读取，不发送。

同一 `report_run_id` 的已保存正文不能更换。不同正文返回 `report_conflict`。同一次执行只有一份报告。卡片已经保存后，不要改传 `failure_text`，也不要换第二个 `report_run_id` 充当失败通知。发送未确认时，用同一标识和完全相同的 `cards` 再提交报告（submit），并复查 `status`。若复查后状态仍为 `pending`、`unknown` 或 `failed`，保留数据包、工作底稿和原卡片，下一轮先恢复。不要另找临时发送渠道，也不要把尚未确认的发送写成已经发送。

报告已经 `sent` 时，只复查 `status`，保留原卡片。全部接收方确认后，用同一个 `run_id`、`report_run_id` 和 `card_json` 把 `review.json` 的 `send_status` 更新为 `sent`，`content_kind` 仍为 `cards`，再本地提交（commit）。最终回复只写 `NO_REPLY`，不重复卡片正文。

## 6. 提交

`seen.json` 只在本地提交（commit）成功时更新。

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/collect.py commit \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --material yes \
  --summary "<一句话>"
```

无重要变化时把 `yes` 换成 `no`。summary 用工作底稿变更记录里的那一句。

## 7. 采集失败

两次采集都失败，或采集还没有写出 `packet.json` 时，不要本地提交（commit），不要补造 `packet.json`。若本轮还没有保存过卡片，先写下失败恢复记录，再提交失败报告（submit）。`cards` 省略：

```bash
python3 .agents/skills/dalton-bullbear-tracker/scripts/state.py save-review \
  --ticker <TICKER> \
  --workspace-dir <workspace> \
  --run-id <RUN_ID> \
  --material yes \
  --summary "<一句话>" \
  --report-run-id <RUN_ID> \
  --send-status pending \
  --content-kind failure_text
```

这里的 `--material yes` 只标记失败报告，不授权本地提交（`commit --material yes`）。失败文本里的股票代码用本轮 `<TICKER>`：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="<TICKER> 跟踪本轮采集失败：原因，下一轮自动重试",
)
```

`RUN_ID` 用已经产生的 `run_id`；采集还没有 `run_id` 时，用当天美东日期组成稳定标识 `<ticker小写>-collect-YYYYMMDD`。失败文本的确认要求返回里的 `report_run_id` 与这条记录完全一致，并且 `content_kind` 明确为 `failure_text`。对上之后，用同一个 `run_id`、`report_run_id` 和 `--content-kind failure_text` 把 `send_status` 写成 `sent`。这不表示本轮研究已经处理完成，不要因此本地提交（`commit --material yes`），`seen.json` 保持原状。标识不一致或缺字段时保持原状。失败文本已确认发出后，最终回复只写 `NO_REPLY`。失败文本未能保存时，最终回复只留这一行失败说明，不附卡片正文。工具返回的 `code` 为 `not_automation_run` 时，按第 1 节处理，不要把 `status` 字段当成这个结果。
