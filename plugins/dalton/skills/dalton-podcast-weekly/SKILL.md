---
name: dalton-podcast-weekly
description: Execute podcast weekly secondary proofreading, Feishu doc publishing, and tracking report delivery.
---

# dalton-podcast-weekly

完成播客周报的二次校对、内容提要撰写、飞书建档与周报汇总投递。上游宿主机流水线任务 `podcast-weekly-pipeline` 已在宿主机 `var/dalton/openclaw-data` 的 `research-outputs/podcast-weekly/` 与 `state/podcast-weekly/` 下生成每期的 `.zh.md`、`.en.md`、`.notes.json`、`.merged.md` 及台账 `state/podcast-weekly/episodes.json`（默认覆盖 All-In 与 a16z，频道与飞书目标文件夹配置见 `references/channels.json`）。本技能统一通过 `podcast_weekly_read` 与 `podcast_weekly_write` 读写宿主机产物和状态文件。

## 1. 读取台账与空周判定

调用 `podcast_weekly_read` 检查宿主机台账与未报告期次：

```text
podcast_weekly_read(action="status", pending_only=false)
```

若是在包含完整工作区副本的本地调试环境中核对，也可运行以下脚本（子命令在前、`--json` 置后）：

```bash
python3 .agents/skills/dalton-podcast-weekly/scripts/podcast_publish.py status --workspace-dir . --json
```

- 返回 `ok: false`（如 `ledger_missing`、`ledger_unreadable`、`doc_record_unreadable`、`reported_record_unreadable`、`pending_report_unreadable`）：立即停止并保持现有状态文件原样不变，在最终回复中写明具体报错与缺失或损坏的文件；台账缺失或无法读取时禁止判定为空周，也禁止发送「没有新节目」。
- 按返回值 `next_action` 的唯一分支推进：
  - `next_action == "resubmit_pending"`：存在待确认报告（`pending_report`）。（1）**禁止重写正文或更换 `report_run_id`**，直接使用 `pending_report.report_run_id` 与 `pending_report.text` 原样调用 `automation_report(action="submit", report_run_id=pending_report.report_run_id, material="yes", text=pending_report.text)`。（2）若返回 `content_conflict` 或 `execution_batch_conflict`，立即停止并原样报错，保留 `pending_report` 留待下次运行重提（禁止更换 `report_run_id` 重试）。（3）仅当 `submit`（或随后的 `status` 核查）返回 `ok == true`、`delivery_confirmed == true`、`status == "sent"` 且返回的 `report_run_id` 与提交完全一致时，才视为送达确认；未确认时先调用一次 `automation_report(action="status", report_run_id=pending_report.report_run_id)` 核查。（4）确认送达后，调用 `podcast_weekly_write(action="mark_reported", report_run_id=pending_report.report_run_id, success_video_ids=pending_report.success_video_ids, failed_video_ids=pending_report.failed_video_ids, empty_date=pending_report.empty_date)`（`empty_date` 为空时可省略）完成已报告登记，随后**结束本次运行**（禁止在同一次自动化执行里提交第二份报告），在最终回复中写明确认回执并说明「已补发上次的周报，本周新内容在下次运行时提交」。（5）核查后仍未确认时：若本次重提返回 `created == true`（报告此刻才首次保存、投递刚开始），或 `pending_report.created_at` 的香港日期等于本次 `status.date`，保留 `pending_report` 原样不变、结束本次运行并在最终回复说明未确认状态；只有当本次重提返回 `created == false`（报告早已保存、投递已结束且未送达）且创建日期早于当天时，才调用 `podcast_weekly_write(action="abandon_pending_report", report_run_id=pending_report.report_run_id)` 放弃该过期的待确认报告，随后**重新调用一次 `status`** 按新的 `next_action` 继续推进。
  - `next_action == "publish"`：`to_publish`（`ready` 或 `incomplete_docs`）非空；先执行第 2 至 3 步完成校对、内容提要与飞书建档，第 3 步结束后再重新调用一次 `status`，按刷新后的 `report_success` 与 `report_failed` 进入第 4 步。
  - `next_action == "submit"`：`to_publish` 为空，且 `report_success` 或 `report_failed` 非空；直接跳至第 4 步，保存待确认报告（`pending_report`）、提交周报汇总并确认送达后调用 `mark_reported` 完成已报告登记。
  - `next_action == "empty_week"`：未报告的成功与失败期次均为空，且当天尚无已报告登记或空周登记；先调用 `podcast_weekly_write(action="save_pending_report", report_run_id="<report_run_id>", text="播客周报｜本周 All-In 和 a16z 没有新节目", success_video_ids=[], failed_video_ids=[], empty_date="<date>")`，再调用 `automation_report(action="submit", report_run_id="<report_run_id>", material="yes", text="播客周报｜本周 All-In 和 a16z 没有新节目")`。若返回 `content_conflict` 或 `execution_batch_conflict`，立即停止并原样报错，保留 `pending_report` 留待下次运行重提。仅当返回 `ok == true`、`delivery_confirmed == true`、`status == "sent"` 且返回的 `report_run_id` 与提交一致时，才调用 `podcast_weekly_write(action="mark_reported", report_run_id="<report_run_id>", empty_date="<date>")` 完成已报告登记并结束；若未确认，先调用一次 `automation_report(action="status", report_run_id="<report_run_id>")` 核查：核查确认送达（条件同上）则同样调用上面的 `mark_reported` 并结束；仍未确认则保留 `pending_report` 结束本次运行并在最终回复说明（下次运行按 `resubmit_pending` 重提）。
  - `next_action == "nothing_to_report"`：`pending_report` 不存在，`to_publish`、`report_success` 与 `report_failed` 均为空，且当天已有已报告登记或空周登记；**禁止调用 `automation_report`**，直接结束本次运行，在最终回复中用一句话说明「本周期没有新的变化（今日已有报告或空周登记，已披露失败仍在等待流水线恢复）」。

## 2. 每期二次校对并写内容提要

对 `ready` 中的每一期执行二次校对与内容提要撰写。

- **拉取宿主机产物到本地沙箱临时工作区**：主任务先调用 `podcast_weekly_read(action="read_artifact", video_id="<video_id>", artifact="zh", offset=0, max_chars=16000)`（依次拉取 `notes`、`zh`、`en`、`merged`；若 `has_more == true` 按 `next_offset` 续读），写入沙箱临时目录 `.work/podcast-weekly/<video_id>/` 后再交给子代理处理。禁止未先拉取就直接假定沙箱本地已有宿主机产物。
- **子代理派发规则**：每期派发 1 个 `general-purpose` 子代理（继承当前运行模型，省略 `model` 参数），同时最多运行 2 个子代理。派发指令中明确要求：
  1. 只修改沙箱内本期 `.work/podcast-weekly/<video_id>/` 下的 `.zh.md`、`.en.md`、`.notes.json` 并新建 `.summary.md`，禁止修改其余期次或台账，禁止再派嵌套子代理；
  2. 原文附上本技能末尾的「撰写硬规则」；
  3. 完成后仅返回本期中文标题、3 至 5 条核心看点、输出文件路径及仍留待核对的事项；
  4. 遇到证据不足以推翻原判的数字、专名或说话人疑点时，保留原文并在校正说明标注「待核」，无需额外派发模型复核争议片段。

每期子任务按以下规则处理：

1. **读 `.notes.json` 并逐条处理标记项**：
   - `amount_warnings`（金额数量级警示）：回 `.en.md` 找原句核对 billion（十亿）/ million（百万）数量级；确有译错则改 `.zh.md`，核对后清空该数组（`[]`），并将修改说明写入 `corrections`。
   - `speaker_flags`（说话人疑点标记）：仅在具备确凿证据（直呼其名、自报身份、前后问答严丝合缝）时才调整说话人，在 `.zh.md` 与 `.en.md` 同一位置插入 `**姓名** [时间]` 拆分段落，并把该条改写为 `时间 说话人校正：哪句从谁改给谁（依据）`；证据不足时保留原判，将条目改写为 `未改，引用前回看视频`。
   - `short_blocks`（过短段落标记）：校订稿词数明显少于逐字稿的段落，对照已拉取的 `.merged.md` 补齐漏句。
   - `corrections`（校正记录）中的「待核」及 `unclear`（听不清片段）：保留说话人原话，数字、公司名、人名即使疑似口误也只写进校正说明并标「待核」，保持原文与发言；`unclear` 处留白，不猜测补写。
2. **通读 `.zh.md`**：修正翻译腔、漏译与人名/公司名拼写不统一；数字、说话人、时间戳仅在对照 `.en.md` 确证错误后改动。
3. **写 `.summary.md`**：
   - 读者定位：未听原节目、只想在 3 分钟内掌握重点的投资人。
   - 第一行 `# 中文标题`：译为中文，最多 30 字，保留人名与公司名英文原文；生成飞书文档时以此行作为文档标题（英文原标题会自动附在正文第一行）。
   - 第二段为判断：两三句话先给核心结论，再写依据与重要性。
   - 随后按主题分组（按议题归并，无需按时间线平铺）：每组一行 `**主题名**`（4 至 12 字，点明核心分歧或判断，例如 `席位计价为什么失效`），下配 2 至 4 条 `- ` 要点；全篇 3 至 5 组、共 8 至 14 条。同一议题在节目各处分散提及的合并入同组，并在要点末尾一并标出各处 `[分:秒]`。
   - 每条要点 40 至 80 字，一条只讲一件事，先给结论再列说话人与依据，末尾附 `[分:秒]`；涉及数字须保留数字及其修饰对象，且提要里的每一个数字均可在 `.zh.md` 中查到原处。
   - 最后一组固定为 `**投资看点**`：点明涉及的公司、资产类别与核心判断变化；嘉宾单方陈述写明「嘉宾自述，未独立核实」；主持人与嘉宾存在利益关联时一并标出。
   - 用词规范：优先使用「是、会、要、做、查」，删去「呈现、体现、围绕、赋能」等空话；同一对象全篇保持同一称呼；首次出现的专业术语附一句简明解释；正文只使用 `- ` 列表与 `**加粗**`，金额单位写「美元」，数字区间写「至」。
   - 交稿前逐条复读：每条脱离上下文后仍独立清晰。
4. **回写宿主机产物**：子代理完成后，主任务依次为 `zh`、`en`、`notes` 及新建的 `summary` 调用 `podcast_weekly_write(action="write_artifact", video_id="<video_id>", artifact="summary", content="<更新后全文>")`（对其余产物分别传入 `"zh"`、`"en"`、`"notes"`），将结果写回宿主机 `research-outputs/podcast-weekly/`。

校对没做完或缺失 `.summary.md` 的期次跳过发布，不写入周报汇总正文，只在本次最终回复里列出跳过原因。

## 3. 生成文稿、创建飞书文档与断点续传

对 `incomplete_docs`（未写完的飞书文档）与通过第 2 步校对的 `ready`（待发布）期次，按顺序调用 `podcast_weekly_read` 准备飞书文档数据：

```text
podcast_weekly_read(action="prepare_doc", video_id="<video_id>")
```

- 返回 `ok: false`（如 `doc_record_unreadable`、`doc_create_failed_twice`、`doc_failed_record_unreadable`、`chunks_record_unreadable`）：立即停止该期建档并保持现有状态文件原样，只在本次最终回复里说明原因，不写入周报汇总的失败段；周报汇总的失败段统一以第 3 步完成后刷新 `status` 返回的 `report_failed` 为准。
- 返回 `already_exists: true`：直接复用返回的 `doc_url`，跳过重复建档。
- 返回 `resume_append: true`（即已有 `document_id` 与 `doc_url`，但部分分段尚未追加完成）：禁止重新调用 `feishu_create_doc`。直接对 `remaining_chunks` 中的每一段顺序调用 `feishu_append(token="<document_id>", markdown=<chunk.content>)`，每成功追加一段立即调用：
  `podcast_weekly_write(action="record_chunk", video_id="<video_id>", document_id="<document_id>", doc_url="<doc_url>", part=<chunk.part>, total_parts=<total_parts>)`。
  全部 `1..total_parts` 登记完成后，后台自动将该期标为 `body_status: "complete"` 并写入 `episodes.json`。
- 返回 `create: true`：
  1. 提取输出的 `title`、`folder_token`（对应 `references/channels.json` 中配置的飞书目标文件夹）、`zh_title`、`total_parts` 以及 `chunks`。
  2. 调用 `feishu_create_doc(title=<title>, folder_token=<folder_token>, markdown=<chunks[0].content>)` 创建飞书文档。
  3. **只要返回值包含 `document_id` 或 `url`——包括建档后正文写入报错 `error: "content_insert_failed"` 的情况**——立即登记文档 ID 与链接：
     - 若首段已随建档成功写入：按 `prepare_doc` 返回的 `total_parts` 与 `chunks[0].part` 调用 `podcast_weekly_write(action="save_doc", video_id="<video_id>", document_id="<document_id>", doc_url="<url>", zh_title="<zh_title>", part=<chunks[0].part>, total_parts=<total_parts>, body_complete=<true_if_total_parts_eq_1_else_false>)`；当 `total_parts` 大于 1 时传 `body_complete=false`，并对后续每个 `chunk` 顺序调用 `feishu_append(token="<document_id>", markdown=<chunk.content>)` 并登记 `podcast_weekly_write(action="record_chunk", video_id="<video_id>", document_id="<document_id>", doc_url="<doc_url>", part=<chunk.part>, total_parts=<total_parts>)`。
     - 若返回 `content_insert_failed`（文档已建成但首段正文未写入）：调用 `podcast_weekly_write(action="save_doc", video_id="<video_id>", document_id="<document_id>", doc_url="<url>", zh_title="<zh_title>", part=0, total_parts=<total_parts>, body_complete=false)` 立即保留 `document_id` 与 `doc_url`，禁止再次调用 `feishu_create_doc`，改为对所有 `chunk` 顺序调用 `feishu_append(token="<document_id>", markdown=<chunk.content>)` 并登记 `podcast_weekly_write(action="record_chunk", video_id="<video_id>", document_id="<document_id>", doc_url="<doc_url>", part=<chunk.part>, total_parts=<total_parts>)` 补完全部正文。
  4. 仅当 `feishu_create_doc` 既失败又未返回任何 `document_id`/`url` 时，调用 `podcast_weekly_write(action="record_failure", video_id="<video_id>", error="<报错>")` 记录失败；同一期累计失败 2 次后停止建档，待刷新 `status` 进入 `report_failed` 后写入周报汇总的失败段（单次失败仍跳过本次建档，只在本次最终回复列出原因）。
- **读回抽查**：全部建档完成后，抽取至少 1 篇用 `feishu_read` 读回文档正文，确认「内容提要」「中文全文」「校正说明」「英文校订稿」四个部分完整存在。
- **刷新状态并进入第 4 步**：第 3 步可建档的期次全部处理完成后（跳过的期次按规则记录原因），提交周报汇总前**必须重新调用一次** `podcast_weekly_read(action="status", pending_only=false)`。若存在跳过或未完成的期次而使 `next_action` 仍为 `publish`，**禁止退回第 2 步**，只按刷新后的 `report_success` 与 `report_failed` 直接推进第 4 步汇报已完成部分（跳过与未完成的期次会在下次运行时再进入 `to_publish`）；若刷新后 `report_success` 与 `report_failed` 均为空，只回复说明本次各期跳过原因并结束。

## 4. 提交周报汇总（`automation_report`）并完成已报告登记

禁止在技能内硬编码任何群聊或私信目标，也无须调用外部消息工具；全部投递统一通过 `automation_report` 提交，由后台按 `podcast-weekly` 投递配置发送至目标渠道。

组装周报汇总正文（`automation_report` 的 `text` 上限 40000 字；期次多时压缩每期要点，不得超限）：

- 第一行：`播客周报｜<起始日> 至 <结束日>，共 N 期`
- 每期一组（只列刷新后 `status` 返回的 `report_success` 期次）：
  - 第一行：`**【频道】中文标题**（时长，日期）`（中文标题取 `.summary.md` 首行；若该期 `recovered == true`，注明「此前失败、现已补发」）
  - 下一行：`原标题：<英文原标题>`
  - 3 至 5 条 `- ` 要点（取自内容提要最重要的判断；每条一句话、先结论后依据、标明发言人；涉及具体公司或资产的看点占一条）
  - 单独一行列出飞书文档链接
- 失败段：本段只写入刷新后 `status` 返回的 `report_failed` 期次（含流水线未就绪与连续两次建档失败）；`report_failed` 为空时省略该段。本次跳过的期次（未建档、校对未完成等）不写入周报汇总正文，只在本次最终回复里列出原因；已记入 `reported.json` 的旧失败留待流水线恢复，不重复写入本段。
- 排版兼容规范：面向中文读者采用先给判断的主谓句，删去空话；金额单位写「美元」，区间写「至」；正文不用 `#` 大号标题，不使用 Markdown 表格，文档链接直接单行放置，确保飞书群与同步渠道均可直接阅读。
- 判断严格出自节目内容并写明观点归属；用于研究的事实数字须回公司公告或监管申报文件（filing）核实。

写好周报汇总正文后，按「先保存待确认报告（`pending_report`）、再调用 `automation_report` 提交周报汇总、仅在确认送达后才调用 `mark_reported` 完成已报告登记」的顺序执行（使用第 3 步完成后重新调用 `status` 返回的 `report_run_id`）：

```text
podcast_weekly_write(action="save_pending_report", report_run_id="<report_run_id>", text="<汇总正文>", success_video_ids=["<report_success[].video_id>"], failed_video_ids=["<report_failed[].video_id>"])
automation_report(action="submit", report_run_id="<report_run_id>", material="yes", text="<汇总正文>")
```

仅当 `automation_report`（`submit` 或随后的 `status` 核查）返回 `ok == true`、`delivery_confirmed == true`、`status == "sent"` 且返回的 `report_run_id` 与提交完全一致时，才调用：

```text
podcast_weekly_write(action="mark_reported", report_run_id="<report_run_id>", success_video_ids=["<report_success[].video_id>"], failed_video_ids=["<report_failed[].video_id>"])
```

若 `submit` 返回未确认送达（包括 `ok: true, delivery_confirmed: false` 或 `status != "sent"`），禁止调用 `mark_reported`，调用一次 `automation_report(action="status", report_run_id="<report_run_id>")` 核查：确认送达后补调 `mark_reported`；仍未确认则保留 `pending_report` 原样不变、结束本次运行并在最终回复说明（下次运行按 `resubmit_pending` 重提）。若 `automation_report` 返回 `content_conflict` 或 `execution_batch_conflict`，立即停止并原样报错，保留 `pending_report` 留待下次运行重提（禁止更换 `report_run_id` 重试）。最终回复中写明：本次 `report_run_id`、总期数、各期飞书文档链接、失败或跳过的期次与原因、`automation_report` 提交与投递确认回执，以及实际未验证的环节。

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
