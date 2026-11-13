---
name: dalton-x-accounts-daily
description: "无人值守的 X 账号日报：读取四个固定账号过去 24 小时推文，逐条中文摘要，并用 automation_report 提交卡片。"
---

# X 账号日报

无人值守。不提问，不创建子任务。不要把中间过程发到任何聊天。

安装路径是 `.agents/skills/dalton-x-accounts-daily/`。命令用 `scripts/x_accounts_daily.py`，写文件时带 `--workspace-dir <workspace>`。

接收方在 `metadata.report_profile`，配置名是 `x-accounts-daily`。卡片和工具参数都不写接收方。

本任务没有长期状态。文件只放在 `temp/x-accounts-daily/<summary_date>/`：原始 JSON、筛选结果、摘要、Markdown 和 `card.json`。发送失败也保留这些临时文件。

账号顺序固定：`aleabitoreddit`、`jukan05`、`mingchikuo`、`citrini`。窗口是亚洲/香港时间的严格 24 小时 `[NOW-24h, NOW]`，即使自动化时区不是香港。`SUMMARY_DATE` 是这个窗口结束时刻的香港公历日。

摘要和数据备注先给判断。正文不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。

## 1. 取窗口

```bash
python3 .agents/skills/dalton-x-accounts-daily/scripts/x_accounts_daily.py \
  --json window
```

记下 `start_iso`、`end_iso`、`start_date`、`end_date`、`summary_date`。

## 2. 四个账号的时间线

按顺序调用，不要并行。每个账号失败后重试一次。仍然失败就记「抓取失败」并继续，不要让整份日报停住。不要传嵌套的 `params`。`tweets` 不要带 `url`、`tweet_id` 或 `query`。`since` 和 `until` 是日历日，桥接层在取回之后按日历日过滤再截到 `count`。脚本还会按精确 24 小时再滤一次。`count` 用 50。不要传 `include_replies`。

```python
community(
    route="x-timeline",
    operation="tweets",
    handle="aleabitoreddit",
    since=START_DATE,
    until=END_DATE,
    count=50,
)
```

另外三个 handle 同样调用。返回里的 `has_more` 为 true 时，数据备注说明窗口内可能还有未取回的帖子，不要翻页，也不要编造。

把四次结果写成 `temp/x-accounts-daily/<summary_date>/raw.json`：

```json
{
  "accounts": {
    "aleabitoreddit": {"ok": true, "has_more": false, "items": []},
    "jukan05": {"ok": true, "has_more": false, "items": []},
    "mingchikuo": {"ok": true, "has_more": false, "items": []},
    "citrini": {"ok": false, "items": []}
  },
  "x_search": {"ok": false, "items": []}
}
```

`items` 用工具返回的列表。纯转推且没有本人评论会在筛选时丢掉。带本人观点的 quote 保留。置顶旧帖按 `created_at` 排除，不按它还钉在主页上保留。

## 3. 补充检索

四个时间线都处理完之后，先调用一次 `x_search` 做宽检索。它只补充时间线，四个账号的时间线仍是底表。单帖的图片或视频补充在下面，最多 2 条，不算进这一次。

```python
x_search(
    query="posts whose meaning depends on an image, video, or long thread",
    from_date=START_DATE,
    to_date=END_DATE,
    handles=["aleabitoreddit", "jukan05", "mingchikuo", "citrini"],
)
```

这个工具没有 `allowed_x_handles`，用 `handles`。返回的是摘要摘录，不是保证完整的原文。上面这一次补充检索不要传图片或视频开关。

宽检索结果先按精确 24 小时过滤，并按推文 ID（`status id`）和 URL 与时间线去重。只有 URL、作者和时间都核验过、作者是这四个账号之一、且落在窗口内，才补入。宽检索失败时继续用时间线，把 `x_search.ok` 设为 false。

若入选帖依赖未读的图片或视频，最多再对 2 条单帖调用 `x_search`：查询写该帖 URL，含图片传 `enable_image_understanding=True`，含视频传 `enable_video_understanding=True`，两者都有则两个都传。仍读不到时，才在数据备注写明可读文本是正文或摘录，不要猜测或描述未读到的图片、视频内容。URL 查询未召回该帖时按补充失败处理，继续用时间线，不要反复重试。

```python
x_search(
    query="https://x.com/<handle>/status/<id>",
    from_date=START_DATE,
    to_date=END_DATE,
    handles=["<handle>"],
    enable_image_understanding=True,  # 视频帖改传或同传 enable_video_understanding=True
)
```

若入选帖依赖未展开的连帖（thread），最多再对 2 条调用：

```python
community(route="x-timeline", operation="thread", url="https://x.com/<handle>/status/<id>")
```

`x-timeline` 读不到图片和视频。普通纯文字帖不要重复查。

## 4. 筛选

```bash
python3 .agents/skills/dalton-x-accounts-daily/scripts/x_accounts_daily.py \
  --json select --workspace-dir <workspace> \
  --as-of "<end_iso>" \
  --input temp/x-accounts-daily/<summary_date>/raw.json \
  --output temp/x-accounts-daily/<summary_date>/selected.json
```

`--as-of` 用第 1 步的 `end_iso`，保证窗口和取数时一致。脚本丢掉窗口外的帖、纯转推，并把核验过的补充帖并入对应账号。某账号时间线失败且没有核验过的补充帖，栏目写「抓取失败」。时间线成功但窗口内没有帖，写「过去 24 小时内没有新推文」。

## 5. 摘要和 Markdown

每条帖用一句中文概括，保留关键数字、公司或产品名、时间点和方向。不要把不同帖合并成一条。英文只在术语或原句必要时留短语。摘要不要自己写链接。

把摘要写到 `temp/x-accounts-daily/<summary_date>/summaries.json`，键是推文 ID（`status id`）：

```json
{"summaries": {"123": "一句中文"}}
```

缺摘要时脚本报错，不要让脚本编一句。

```bash
python3 .agents/skills/dalton-x-accounts-daily/scripts/x_accounts_daily.py \
  --json compose --workspace-dir <workspace> \
  --selected temp/x-accounts-daily/<summary_date>/selected.json \
  --summaries temp/x-accounts-daily/<summary_date>/summaries.json \
  --output temp/x-accounts-daily/<summary_date>/summary.md
```

Markdown 由脚本生成。四个账号各自从 1 重新编号。链接用筛选结果里的 URL。没有异常时，数据备注里有这一句，不要改写：

`四个账号均抓取成功；SuperGrok X Search 补充成功`

时间窗用「至」，不用成对的 `~`。不要用表格，不要为了长度截断或加 `...`。

## 6. 卡片

`report_run_id` 用 `x-accounts-daily-<summary_date>`。标题用 compose 返回的 `title`。

```bash
python3 .agents/skills/dalton-x-accounts-daily/scripts/x_accounts_daily.py \
  --json render --workspace-dir <workspace> \
  --file temp/x-accounts-daily/<summary_date>/summary.md \
  --output temp/x-accounts-daily/<summary_date>/card.json \
  --title "<title>"
```

读出 `CARD` 后：

```python
automation_report(action="submit", report_run_id=RUN_ID,
                  material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

窗口内没有新帖也提交这张卡片，栏目里写「过去 24 小时内没有新推文」。不要改成静默。

渲染（`render`）返回 `card_conflict`（卡片文件冲突）时，禁止删除或覆盖已有卡片文件（不要调用 `unlink` 删除文件，也不要换路径重写）：先读已保存的正文（`summary.md`）、卡片（`card.json`）和 `automation_report(action="status", report_run_id=RUN_ID)` 的发送状态；若状态已是已发送（`status="sent"` 且 `delivery_confirmed=true`），最终回复直接写 `NO_REPLY`；若尚未确认发送，保持原采集窗口（`start_iso` / `end_iso`），直接用已保存的 `card.json` 和同一 `report_run_id`（`x-accounts-daily-<summary_date>`）调用 `automation_report(action="submit", ...)`。提交返回里的 `report_run_id` 一致、`content_kind` 为 `cards`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent` 时，最终回复只写 `NO_REPLY`；未确认就用同一标识和同一份 `cards` 再提交。同一 `report_run_id` 已保存不同正文时返回 `content_conflict`（正文冲突），不要改卡片，也不要换第二个标识。`not_automation_run`（非自动化运行）时停止，最终回复说明本次不是自动化运行，不附日报正文。

提交失败时保留临时文件。再提交一行 `failure_text`。这行也保存失败时，最终回复只写这行错误。不要发第二份正文。
