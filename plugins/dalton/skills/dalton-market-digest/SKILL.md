---
name: dalton-market-digest
description: "为已运行的晨报或晚报准备并读完本轮输入，按固定栏目写完整中文稿，再由 market_digest_output 保存、发送并完成索引。"
---

# 市场晨报 / 晚报

安装路径是 `.agents/skills/dalton-market-digest/`。写正文前先读 [`references/report-rules.md`](references/report-rules.md)。本文件只说明这一轮怎么取数、怎么交稿、什么算结束。

这一轮是服务器已经启动的晨报或晚报任务。时段只用任务对应的 `AM` 或 `PM`。不传 execution id、路径、模型、收件人或 cron。手写卡片、从旧路径读批次、新建 cron、以及旧的命令行都不属于这一步。

## 1. 准备

调用 `market_digest_inputs(action="prepare", period="AM")` 或 `period="PM"`。日期以返回的 `authoritative_date` 为准，格式 `YYYY-MM-DD`。准备阶段不使用调用方传入的日期，也不使用主机时钟。晨报在香港时间 09:00 触发，那时美国东部仍是前一个日历日，主机上的当地日期会把稿件标错一天。

`preparation_status` 为 `ready` 或 `ready_degraded` 且 `usable` 为 true 时才继续读。`code` 为 `prepare_unconfirmed` 时，对同一次运行再调用 prepare；这次重试会复用原来的批次。其他未就绪状态用第 5 节的 FAIL 结束，不编造正文。

准备结果里出现的这些字段按原值使用，缺哪个就不写哪个，不补默认数：`authoritative_date`、`batch_id`、`preparation_status`、`started_hkt_date`、`emails_count`、`priority_senders`、`funda`、`image_backlog`、`tmtb`、`hashes`、`digest_outcome`、`reason`、`reused`、`digest_invoked`、`tmtb_invoked`。`hashes.input_sha256` 要保留。`execution_id` 是准备尝试的 id，与服务器上的 automation execution 不是同一个值。

## 2. 读完每一页

从 `market_digest_inputs(period=..., offset=0)` 开始。`action` 默认就是 read，不传 date。用返回的 `next_offset` 继续，直到 `next_offset` 为 null。各页的 `authoritative_date` 和 `hashes.input_sha256` 应与准备结果一致，`content_sha256` 在各页保持相同。某一页不一致就 FAIL。读完全部页面后再撰写正文。

把各页 `text` 按顺序拼成一份文本。它是一个 JSON 对象，键为 `emails`、`pending_snippets`、`tmtb_docs`。页上同时给出的计数和状态只作核对：`emails_count`、`tmtb_count`、`pending_snippets_count`、`degraded`、`reason`、`priority_senders`、`funda`、`image_backlog`、`tmtb`、`hashes`、`preparation_status`、`authoritative_date`。拼好的全文放在当前工作区，供写稿使用。不从旧输出目录或调用方路径另找一批。

某一页 `ok` 不是 true，或 `truncated` 不是 false，就停在读这一步并 FAIL。

## 3. 撰写正文

按 `references/report-rules.md` 写一份完整中文 Markdown。标题里的日期就是 `authoritative_date`。优先级发件人核对在写栏目之前做完，核对清单不进入正文。

输入按该参考里的定义全空时，不写日报，也不调用 `market_digest_output`。结束回复只有这一行：

`SKIP: no new emails for <authoritative_date> <period>`

有邮件、有 TMTB 全文，或有 pending snippet 时，都写完整栏目。没有邮件但仍有 TMTB 时，只用 TMTB 写完整栏目。

交稿前核对标题、栏目顺序、空行、列表和最后一条来源。提交出去的字符串就是这一份，不再改写第二版。

## 4. 提交和恢复

第一次提交：

`market_digest_output(action="submit", period="AM" 或 "PM", markdown=正文)`

这个工具自己保存、渲染、发送并完成归档和索引。提交可能要几分钟。不要因为没有立刻返回就再交一份不同的正文。

恢复时先调用 `market_digest_output(action="status", period=...)`。以下恢复操作在当前仍运行的自动化执行中调用。status 只查询进度。按它说明的当前步骤继续：

- 原正文还没保存：写完后 `submit`，带上 `markdown`。
- 原正文已保存、报告还没发送：`submit`，不带 `markdown`，沿用已保存的那一份。
- 报告已发送，归档或索引还没完成：`market_digest_output(action="finalize", period=...)`。
- `report_delivery_confirmed` 为 true 且 `finalized` 为 true：进入第 5 节的 DONE。

`finalize` 同样可能要几分钟。再次调用仍针对同一次运行和同一份正文。

## 5. 结束行

回复最后一行只能是下面三种之一，不能是空回复。

DONE 只在工具结果同时给出 `report_delivery_confirmed=true` 和 `finalized=true` 时使用。计数只用准备或读取结果里实际出现的字段；某个字段没有就不写该段，不填 0 冒充。

`DONE: <authoritative_date> <period> · <emails_count>封 · TMTB <tmtb.new_count>篇 · FUNDA AI <funda.research_eligible_count>封／正文索引<完成或待补>／图片<该来源的 pending 或完成> · 卡片已发 · market-memory 已写 · 已索引`

`tmtb.status` 为 `alphaengine_unavailable`、`tmtb.degraded` 为 true，或 TMTB 这一步没有成功时，把 TMTB 那段写成 `TMTB 未同步：<tmtb.reason>`。`reason` 不在结果里就只写 `TMTB 未同步`。`funda.index_pending` 为 true 时正文索引写“待补”，否则在该字段存在且不为 true 时写“完成”。图片用 `image_backlog.sources` 里实际出现的 `funda-ai` 或 `sales-notes` 的 `pending`；没有图片字段就不写图片段。`ready_degraded` 或读取结果 `degraded=true` 时，把已有的 `reason` 附在 DONE 行末尾。

当前步骤还没完成时用 FAIL，不用 DONE：

`FAIL: <当前步骤> · <工具返回的 code、preparation_status 或 reason>`

SKIP 只用第 3 节那一行。待发送、待归档、待索引都是 FAIL 或停在当前步骤，不是 DONE。
