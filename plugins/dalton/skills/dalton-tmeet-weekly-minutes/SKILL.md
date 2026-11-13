---
name: dalton-tmeet-weekly-minutes
description: "无人值守的腾讯会议周会纪要：只读拉取录制与转写，写成纪要并提交纯文本。"
---

# 腾讯会议周会纪要

无人值守。不提问，不创建子任务。不调用 `message`。本轮只做这一份周会纪要。

模型用 `gpt-6.1-sol-oauth`。主任务是每周一 15:00（Asia/Shanghai），时限 60 分钟。兜底是每周一 21:00（Asia/Shanghai）。两种模式由自动化指令写明，技能不自行选择。两条自动化共用一个 `report_run_id`：`tmeet-weekly-minutes-<会议日期>`。有会议时 `<会议日期>` 是 `fetch` 返回的 `meeting_date`，格式 `YYYY-MM-DD`。未登录、无录制等拿不到会议日期时，用 `tmeet-weekly-minutes-<上海当天日期>`，直接采用脚本返回的 `report_run_id`。失败文案只用文末「失败文案」清单里的原句。

接收方在自动化配置的 `metadata.report_profile`（`tmeet-weekly-minutes`）中定义：成功摘要投递到 `text_targets`，失败通知投递到 `failure_targets`。技能正文、`config.json` 和 `automation_report` 参数都不写接收方。

安装路径是 `.agents/skills/dalton-tmeet-weekly-minutes/`。命令用 `scripts/tmeet_minutes.py`。每条命令都带 `--workspace-dir <workspace>` 和 `--json`。`<workspace>` 是本轮工作区根目录。

数据源只有腾讯会议云录制。本技能不登录、不建会、不发起录制权限申请。脚本通过只读路由 `tmeet` 使用宿主机上的 `tmeet` 登录凭证。未登录时只报告「需要在宿主机重新登录 tmeet」。

自行撰写的标题、正文、摘要和最终回复遵守文末撰写硬规则。逐字引文、原始转写、命令和固定协议字段保持原样。

## 配置

`config.json` 只有三项：`meeting_subject_keyword`（当前是 `预定的会议`）、`lookback_days`（当前是 7）、`lang`（当前是 `zh`）。主题按子串匹配，大小写不敏感。回看窗口是当前上海时间（Asia/Shanghai）减去 `lookback_days` 天到现在。周一 15:00 的 7 天窗口包含上周五，不另加周一到周日的日历周限制。

## 状态路径

都相对工作区。不要写绝对路径，不要用上级目录，不要把这些路径做成符号链接。

- `state/tmeet-weekly-minutes/pending.flag`：待重拉标记（`pending.flag`）。它在日期目录外面。
- `state/tmeet-weekly-minutes/runs/<会议日期>/search.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/selected.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/minutes.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/todos.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/transcript.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/manifest.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/doc.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/doc.failed.json`
- `state/tmeet-weekly-minutes/runs/<会议日期>/feishu-return.json`

会议日期是 `meeting_start_time` 的上海公历日；没有开始时间时用 `record_start_time`。同一场会的转写和视频按 `(subject, meeting_start_time)` 分组。`meeting_start_time` 最新的一组胜出。组内优先 `file_type=transcript`，没有转写文件再用该组的视频文件。

## 1. 自检

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json self-test --workspace-dir <workspace>
```

`ok` 为 false，或 `logged_in` 不是 true：停止。`report_run_id` 用返回值 `tmeet-weekly-minutes-<上海当天日期>`。提交失败清单里的未登录句。不发到群。不在对话里贴登录链接，也不运行登录命令。

## 2. 拉取

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json fetch --workspace-dir <workspace>
```

`fetch` 调用 `record_search`，再拉取纪要、待办和全文转写。转写从段落编号 `pid=0` 起，每页 `limit` 100，下一页用返回的 `next_pid`，直到 `more` 为 false。脚本检查 pid 从 0 连续、无重复，最多翻 50 页。`fetch` 不写待重拉标记（`pending.flag`）。兜底重拉用 `fetch --from-pending`，见第 7 节。

- `ok` 为 false：停止。`failure_text` 只用失败清单里的对应原句，不用脚本 `error` 的原文。`report_run_id` 用返回值。`not_logged_in`、`cli_contract_changed`、`transcript_too_long`、`transcript_pid_invalid`、`pending_unreadable` 各用清单中的那一句。清单没有单列的其他脚本失败，用「拉取或保存没有完成」那句。这些失败都不要把转写写成已完整获取。
- `found` 为 false：`report_run_id` 是 `tmeet-weekly-minutes-<上海当天日期>`。提交失败清单里的无录制句。不发到群。
- `found` 为 true 且 `truncated` 为 true：照常使用已选出的这场会，并在纪要末尾注明录制列表在页数或条数上限处截断。
- 记下 `meeting_date`、`report_run_id`、`record_file_id`、`file_type`、`run_dir`、`minutes_empty`、`transcript_empty`、`pending_needed`。

`minutes_empty` 表示智能纪要正文是空白。`transcript_empty` 表示转写里没有句子或词文本。两者都为 true 时 `pending_needed` 为 true。

## 3. 纪要还没生成

主任务里 `pending_needed` 为 true：

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json pending --workspace-dir <workspace> \
  --op write --meeting-date <会议日期> --record-file-id <record_file_id>
```

然后最终回复只写 `NO_REPLY`。不建文档，不提交 `automation_report`。21:00 的兜底会处理这份待重拉标记（`pending.flag`）。

智能纪要是空的、转写有文本：用转写写纪要，并在群摘要和飞书文档里写「官方智能纪要未生成，基于转写整理」，继续按第 4–6 节执行。

## 4. 撰写

主助手自己写，不交给子任务。栏目顺序固定为：

1. 要点：只写会议里有依据的判断，标出发言人。不凑条数。
2. 已确认事项：写清谁确认了什么。没有明确拍板的内容不放进本节。
3. 待办：每条写事项、负责人、截止时间。转写没有负责人或截止时间时写「未明确」，不要补。
4. 业绩与仓位：每次都保留。按 `.agents/skills/dalton-tmeet-weekly-minutes/references/positions-performance.md` 检索全份转写、核对上下文并按其分类撰写。

反幻觉：只写转写里有依据的内容。智能纪要只作索引。其中每个数字、范围（全球还是局部）和并列公司，都先在 `transcript.json` 里找到原句再用。对不上时按转写写。智能纪要可能改数字、把局部写成整体、漏掉并列的公司。

转写里的公司名、股价和市值按上下文核对。语音识别可能把「宁德」听成「明德」、把「算力」听成「3m」。按上下文改正，并在纪要末尾注明改了哪几处。

多人共用一个账号时，按上下文区分发言人，并在纪要里注明。

群摘要和飞书文档都用自然中文标题，不要用 Insight、Todo、中英双标题、空泛总结或「会议达成重要共识」等套话。投递前核对姓名、数字、日期、责任归属和完成状态。最后一遍只校正语言和标点，不得改动上面规定的栏目、顺序、条数、阈值、数据口径、验证步骤、失败处理和投递方式。

## 5. 飞书文档

先看是否已经有文档：

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json save-doc --workspace-dir <workspace> \
  --meeting-date <会议日期>
```

`create` 为 false 时不要再调用 `feishu_create_doc`。`reason` 为 `doc_record_unreadable` 表示记录在但 JSON 读不了，不要覆盖，也不要另建。`reason` 为 `create_failed_twice` 时提交失败清单里的「飞书文档未建成」，不发到群。`reason` 为 `content_incomplete`（`content_complete` 为 false）表示文档已创建、正文没写进去：`doc.json` 里保留 `ok: false` 和链接，不要另建，不发群摘要，提交失败清单里的「纪要文档已创建但正文没写进去」，同一条 `failure_text` 末尾附 `url`。

`create` 为 true 时调用 `feishu_create_doc`（成功拿到链接后不再重复调用）。标题是 `周会纪要 YYYY-MM-DD`。`markdown` 是完整纪要。

```python
feishu_create_doc(
    title="周会纪要 2026-11-13",
    markdown="# 周会纪要 2026-11-13\n\n## 要点\n样例一句。\n",
)
```

把返回的 JSON 原样写到 `state/tmeet-weekly-minutes/runs/<会议日期>/feishu-return.json`。调用没有返回 JSON 时写入 `{"ok": false, "error": "call_failed"}`。

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json save-doc --workspace-dir <workspace> \
  --meeting-date <会议日期> \
  --file state/tmeet-weekly-minutes/runs/<会议日期>/feishu-return.json
```

已有 `https://` 飞书链接时用该链接。否则有 `node_token` 时用 `https://feishu.cn/wiki/<node_token>`，再否则用 `https://feishu.cn/docx/<document_id>`。返回 `ok: false` 且带 `document_id` 或 `url`（例如 `error` 为 `content_insert_failed`）时，记入 `doc.json`，`content_complete` 为 false，`create` 为 false，不要另建。没有可用链接的失败（例如 `call_failed`）记入 `doc.failed.json`；若返回 `create` 仍为 true，可再试一次。累计两次失败后 `create` 为 false（`reason` 为 `create_failed_twice`），停止并提交失败清单里的「飞书文档未建成」。

`feishu_create_doc` 会把文档分享给调用者和已配置的团队群。不要再写授权命令。返回里有 `warning` 时，把 `warning` 转述进群摘要。

## 6. 提交群摘要

先回读飞书文档，核对通过后再发群。`feishu_read` 的 `max_chars` 上限是 50000，并且只从开头读。用 `max_chars=50000` 读一次：

```python
feishu_read(token="https://feishu.cn/docx/Abcd1234efgh", max_chars=50000)
```

核对标题和开头。`total_chars` 与本地全文长度相差不超过 10% 时，视为正文已经写完整。不要反复加大 `max_chars` 重读。超过 50000 字的纪要只核对开头和 `total_chars`。要点、已确认事项、待办三个栏目在本地全文里核对，不要求出现在回读的开头里。回读的标题或开头与本地全文对不上，或 `save-doc` 的 `content_complete` 为 false，或 `total_chars` 与本地全文长度相差超过 10%：不发群摘要，提交失败清单里的「纪要文档已创建但正文没写进去」，同一条 `failure_text` 末尾附文档链接。没有按块补写飞书正文的步骤；正文不完整时不另建文档。

群摘要写日期、参会人、要点、已确认事项和待办。参会人取转写里的 `speaker.user_name`。控制在一屏以内。末尾附核对过的文档链接。排版用粗体，不用大标题。金额写“美元/USD”，不用 `$`。

成功时提交纯文本。`text` 是群摘要。`material` 为 `yes`。`report_run_id` 用 `fetch` 的值。

```python
automation_report(
    action="submit",
    report_run_id="tmeet-weekly-minutes-2026-11-13",
    material="yes",
    text="**周会纪要 2026-11-13**\n参会人：甲。\n**要点** 一句。\n**已确认事项** 甲确认了一项。\n**待办** 甲，未明确。\nhttps://feishu.cn/docx/Abcd1234efgh\n",
)
```

标题、开头和 `total_chars` 经 `feishu_read` 核对，并且群摘要已提交后，最终回复只写一行状态：会议日期，以及文档已创建（不贴纪要正文）。中断恢复时复用 `doc.json` 里的已有文档，只补还没完成的步骤（回读核对、提交群摘要），不另建文档。若恢复时发现群摘要已提交但尚未用 `max_chars=50000` 回读核对，先补做回读核对；仍未核对时，最终回复写明「群摘要已提交，文档正文尚未核对」。这份 `report_run_id` 已经提交过群摘要时，保持该次提交，不再另交 `failure_text`。

## 失败文案

下列情况调用 `automation_report`，省略 `text`（`text` 与 `failure_text` 互斥）。`failure_text` 只用对应原句，不发到群。`material` 为 `yes`。`report_run_id` 直接用脚本返回的完整值，不要改成裸日期：有合法会议日期时为 `tmeet-weekly-minutes-<会议日期>`；自检未登录、回看窗口内无录制，以及待重拉标记损坏且其中日期缺失或校验失败时，为 `tmeet-weekly-minutes-<上海当天日期>`。同一 `report_run_id` 已经保存过另一份正文时，再提交会返回 `content_conflict`。

```python
automation_report(
    action="submit",
    report_run_id="tmeet-weekly-minutes-2026-11-13",
    material="yes",
    failure_text="<失败文案清单里的对应原句>",
)
```

- 未登录：「腾讯会议周会纪要失败：需要在宿主机重新登录 tmeet」
- 无录制：「腾讯会议周会纪要失败：回看窗口内没有匹配的周会录制」
- 兜底时纪要和转写仍都是空的：「腾讯会议周会纪要失败：智能纪要和转写仍未生成」
- 输出不再是约定结构：「腾讯会议周会纪要失败：宿主机 tmeet 输出不再是约定结构」
- 转写超过 50 页：「腾讯会议周会纪要失败：转写分页超过 50 页，已停止」
- 转写 pid 不连续或有重复：「腾讯会议周会纪要失败：转写 pid 不连续或有重复」
- 待重拉标记损坏：「腾讯会议周会纪要失败：待重拉的会议标记已损坏」
- 飞书文档两次都没建成：「腾讯会议周会纪要失败：飞书文档未建成」
- 文档已创建但正文没写进去：「腾讯会议周会纪要失败：纪要文档已创建但正文没写进去」，同一条末尾附文档链接
- 其他脚本失败：「腾讯会议周会纪要失败：拉取或保存没有完成」

## 7. 兜底（周一 21:00）

先查待重拉标记（`pending.flag`）：

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json pending --workspace-dir <workspace> \
  --op check
```

`exists` 为 false：最终回复只写 `NO_REPLY`，什么都不发送。

`exists` 为 true 时，先运行一次 `fetch --from-pending`。`pending --op check` 在 `readable` 为 false 时只返回 `ok`、`exists`、`readable` 和 `path`，不返回 `report_run_id` 或 `meeting_date`。这次拉取不重新搜索，也不改选会议。标记可读时，按其中的 `record_file_id` 重拉内容；即使两次运行之间出现了更新的匹配会议，仍处理这份待重拉标记上的这一场。`report_run_id` 用这次命令的返回值，格式是 `tmeet-weekly-minutes-<会议日期>`。

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json fetch --workspace-dir <workspace> \
  --from-pending
```

- `readable` 为 false，或这次返回的 `status` 为 `pending_unreadable`：`report_run_id` 只用上面这次 `fetch --from-pending` 的返回值。标记内日期能通过校验时，返回值是 `tmeet-weekly-minutes-<该日期>`；日期缺失或校验失败时，返回值是 `tmeet-weekly-minutes-<上海当天日期>`。然后运行下面的 `pending --op delete`，再提交失败清单里的「待重拉的会议标记已损坏」。先删除再拉取会得到 `status` 为 `pending_missing`，这次返回里没有 `report_run_id`。
- 返回的 `ok` 为 false，且 `status` 不是 `pending_unreadable`：按第 2 节提交失败清单里的对应原句，`report_run_id` 用返回值。
- 拉取成功（`ok` 为 true）时，覆盖同一会议日期的运行目录是允许的。然后：
  - 智能纪要已经有正文：按第 4 到第 6 节交付，和主任务一样。成功后删除待重拉标记。
  - 智能纪要仍是空的、转写已有文本：按转写写纪要，写上「官方智能纪要未生成，基于转写整理」，继续按第 4–6 节执行。成功后删除待重拉标记。
  - 两者仍都是空的：删除待重拉标记，提交失败清单里的「智能纪要和转写仍未生成」，不发到群。`report_run_id` 用这次 `fetch --from-pending` 的返回值。

删除待重拉标记：

```bash
python3 .agents/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py \
  --json pending --workspace-dir <workspace> \
  --op delete
```

兜底和主任务使用同一个 `report_run_id`。

## 8. 按需问答

用户在当前对话问本周周会时，先读 `state/tmeet-weekly-minutes/runs/<会议日期>/` 里已有的纪要和转写。资料缺失或转写分页不完整时，再跑自检和 `fetch`。按第 4 节核对后在当前对话回复。复用 `doc.json` 里的链接。不另建文档，不发群。

用户同时问纪要、持仓和业绩时，按 `references/positions-performance.md` 第 5 节执行：开头写会议日期、主要已确认仓位及是否披露量化业绩，再按「持仓及操作」「基金业绩与回撤」「周会主要判断」「待办」顺序撰写。只问会议观点时沿用第 4 节的栏目顺序。

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
