---
name: dalton-msft-tracker
description: "无人值守的 MSFT 日常多空跟踪：读工作底稿和本轮材料，判断重要变化；有重要变化时生成卡片，并在收件方确认发送后提交；无重要变化时本地提交并静默。约 20 分钟。"
---

# MSFT 日常跟踪（`dalton-msft-tracker`）

无人值守。不提问，不创建子任务，20 分钟内结束。研究判断、底稿章节和卡片写法以 [研究规则](references/research-rules.md) 为准，每轮先读完该文件。

跟踪底稿只有 `research/tracker/thesis.md`。导入档案 `research/tracker/imported-msft/thesis.md` 只读。工作区根目录的财报 `thesis.md` 和 `data/thesis.json` 保持原样。底稿不存在时，说明导入尚未完成，停止本轮；不要改用财报底稿继续研究。

收件人在任务的 `metadata.report_profile` 里，profile 名是 `msft-tracker`。卡片、技能和工具参数都不写目标地址。

下面的命令使用沙箱里的技能目录 `.agents/skills/dalton-msft-tracker/scripts/`。本轮上下文如果给出了该技能目录的绝对路径，就用那个目录下的同名脚本。采集只用这个目录里的 `collect.py`。

## 1. 先恢复未完成的运行

读工作底稿，确认文件存在。然后列出尚未写入 `research/tracker/updates.log` 的运行：

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/state.py open-runs \
  --workspace-dir <workspace>
```

有未完成运行时，取最新一条，沿用原来的 `run_id` 和 `report_run_id`。本轮不再采集第二个包。

`review.json` 可以先记下 `card_json`，再构造卡片。恢复时先看这个相对路径是不是工作区内的普通文件。文件还不存在，或该路径是符号链接时，继续这一轮的研究和卡片构造，沿用同一个 `run_id` 和 `report_run_id`。不要读取这张还不存在的卡片，也不要提交报告。

卡片文件已经存在时，不重写已有卡片。调用 `automation_report` 的 `status` 读取这一份 `report_run_id`，再按 review 和返回只补投递：

- `material` 为 `no`：不调用报告工具，直接按第 6 节 `commit --material no`。
- 卡片文件已在，但这一份报告的 `delivery_confirmed` 还不是 `true`，或 `status` 为 `pending`、`unknown`、`failed`：保留数据包、底稿和原卡片字节。用同一个 `report_run_id` 和完全相同的 `cards` 再提交一次，并用 `status` 核查。
- 接受卡片确认前，返回里的 `report_run_id` 必须与 review 的 `report_run_id` 完全一致，并且 `content_kind` 明确为 `cards`。同时 `ok` 为 `true`、`delivery_confirmed` 为 `true`、`status` 为 `sent` 时，卡片保持原字节，只 `commit --material yes`。缺 `report_run_id`、标识不一致，或 `content_kind` 不是 `cards` 时，保持原状，不 commit，`seen.json` 不变。
- 本地把发送记成 `sent`，但还没有上述同一份报告的结果时，只调用 `status`，先不要 commit。
- review 的 `content_kind` 为 `failure_text` 时，不要 `commit --material yes`。失败文本的确认同样要求返回里的 `report_run_id` 与 review 完全一致，并且 `content_kind` 明确为 `failure_text`，同时 `ok`、`delivery_confirmed` 和 `status` 为 `sent`。对上之后，`seen.json` 仍保持原状，这条记录不挡住新的采集。同一份报告还没确认时，用同一个 `report_run_id` 再提交一次失败文本。标识不一致或缺字段时保持原状，不把失败文本当成已结束。
- 工具返回的 `code` 为 `not_automation_run`：这不是自动化运行。不要 commit，不要另找发送办法。最终回复说明本次不是自动化运行，不附卡片正文。

最新一条如果是已确认的 `failure_text`，不要把它恢复成待发送卡片，直接进入采集。其他未完成运行先恢复。没有未完成运行时，进入采集。

## 2. 采集

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/collect.py collect \
  --workspace-dir <workspace> \
  --since <YYYY-MM-DD> \
  --run-id <YYYYMMDD-HHMM>
```

`--since` 用上一份 `research/tracker/last_run.json` 的 `until`；没有上一份时可以省略。`--run-id` 用美东时间 `YYYYMMDD-HHMM`。记下返回的 `run_id`、相对路径 `packet_json` 和 `packet_md`。运行目录是 `research/tracker/<run_id>/`。

完整数据包已存在时，采集程序会恢复该包。恢复结果不是失败，不要再采集一次。采集失败可以重试一次。两次都失败时，不要 commit，`seen.json` 保持原状，按第 7 节发失败报告并结束。

## 3. 读材料并判断

按研究规则读完工作底稿、`packet.md` 和 excerpts。执行数据包里的两条固定网页查询，用 WebSearch 和 WebFetch。即使内部材料没有新增，也要完成新闻、X 和待核实线索。核查写到 `research/tracker/<run_id>/news.md`，保留标题、来源、日期、URL 和检索/原文状态。页面打不开就换来源；仍只有一个来源时写清归属。

AlphaEngine 先看摘要，最多两篇全文，`doc_id` 保持完整。遇到额度、`E_DESKTOP` 或 `E_UPSTREAM_API` 就停止全文。SEC 新公告核对 item、99.1 附件和 10-Q 关键词。X 先判断作者身份，再更新专家记录、观点池和待核实条目。

用编辑工具修改底稿里有变化的章节。然后判断 `material`。

保存本轮恢复记录。记录只含运行标识、判断、底稿路径和哈希、卡片路径和发送状态，不写凭据、工具完整返回或第二份正文。有重要变化时，可以先记下即将写入的卡片路径，再构造卡片。

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/state.py save-review \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --material yes \
  --summary "<一句话>" \
  --report-run-id <run_id> \
  --card-json research/tracker/<run_id>/card.json \
  --send-status pending \
  --content-kind cards
```

`material` 为 `no` 时用 `--material no --send-status not_required`，可以不传 `--card-json`。底稿哈希由脚本按 `research/tracker/thesis.md` 计算。

同一条 `review.json` 再次保存时，`run_id`、`report_run_id`、已写下的 `card_json` 和已写下的 `content_kind` 保持不变。可以只把 `send_status` 改成当前发送状态。传入另一份 `report_run_id` 或另一张卡片路径时，命令失败，原文件字节不变。这样改发送状态不会换成另一份报告。构造被打断、卡片文件还不是普通文件时，按第 1 节继续这一轮的研究和构造。

本轮新作者用同一把工作区锁写入账号池，最多新增 3 位：

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/state.py x-pool add \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --handle <handle> \
  --tickers MSFT \
  --reason "<身份与依据>"

python3 .agents/skills/dalton-msft-tracker/scripts/state.py x-pool remove \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --handle <handle> \
  --reason "<移出原因>"
```

没有 `run_id` 或没有 `packet.json` 时不要改账号池。

## 4. 无重要变化

不调用报告工具。`material=no` 不会形成发送确认，不要等投递。按第 6 节 `commit --material no`。把 review 的 `send_status` 记为 `not_required`。最终回复只写 `NO_REPLY`。

## 5. 有重要变化

按研究规则写 `research/tracker/<run_id>/card.md`。构造卡片：

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/render_card.py \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --file research/tracker/<run_id>/card.md \
  --output research/tracker/<run_id>/card.json
```

脚本只写卡片文件，不联网。正文超过 3,000 字、含图片或表格时会拒绝且不截断。同一运行已有卡片时，只有内容一致才复用；内容不同则保留原字节。已保存或已发送的卡片不要改写。

读出 `card.json`，得到一个对象 `CARD`。由本轮代理直接调用报告工具，不要从脚本导入，也不要用 Bash 调用服务：

```python
automation_report(action="submit", report_run_id=RUN_ID,
                  material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

`config` 以及用户、工作区、线程、运行和自动化身份由服务器注入，调用时不传。`cards` 是只含这一张卡片的列表。

返回是 JSON 字符串。只有返回里的 `report_run_id` 与 review 的 `report_run_id` 完全一致，`content_kind` 明确为 `cards`，并且 `ok` 为 `true`、`delivery_confirmed` 为 `true`、`status` 为 `sent`，才可以 `commit --material yes`。缺字段或标识不一致时保持原状，不 commit，`seen.json` 不变。`ok` 或 `created` 只说明报告已保存。`status` 只读取，不发送。

同一 `report_run_id` 的已保存正文不能更换。不同正文返回 `report_conflict`。同一次执行只有一份报告。卡片已经保存后，不要改传 `failure_text`，也不要换第二个 `report_run_id` 充当失败通知。发送未确认时，用同一标识和完全相同的 `cards` 再提交，并复查 `status`。状态保持 `pending`、`unknown` 或 `failed`，保留数据包、底稿和原卡片，下一轮先恢复。不要另找临时发送渠道，也不要把失败私信写成已经发送。

报告已经 `sent` 时，只复查 `status`，保留原卡片，然后提交。

全部接收方确认后，用同一个 `run_id`、`report_run_id` 和 `card_json` 把 review 的 `send_status` 更新为 `sent`，`content_kind` 仍为 `cards`，再 commit。最终回复只写 `NO_REPLY`，不重复卡片正文。

## 6. 提交

`seen.json` 只在 commit 成功时更新。

```bash
python3 .agents/skills/dalton-msft-tracker/scripts/collect.py commit \
  --workspace-dir <workspace> \
  --run-id <run_id> \
  --material yes \
  --summary "<一句话>"
```

无重要变化时把 `yes` 换成 `no`。summary 用底稿变更记录里的那一句。

## 7. 采集失败

两次采集都失败时，不要 commit。若本轮还没有保存过卡片，发送失败报告，`cards` 省略：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="MSFT 跟踪本轮采集失败：原因，下一轮自动重试",
)
```

`RUN_ID` 用已经产生的 `run_id`；采集还没有 `run_id` 时，用当天美东日期组成稳定标识 `msft-collect-YYYYMMDD`。失败文本的确认要求返回里的 `report_run_id` 与这次提交的标识完全一致，并且 `content_kind` 明确为 `failure_text`。对上之后，review 也记为 `failure_text`。这不构成研究包成功处理，不要因此 `commit --material yes`。标识不一致或缺字段时保持原状，`seen.json` 不变。失败文本已确认发出后，最终回复只写 `NO_REPLY`。失败文本未能保存时，最终回复只留这一行失败说明，不附卡片正文。工具返回的 `code` 为 `not_automation_run` 时，按第 1 节处理，不要把 `status` 字段当成这个结果。
