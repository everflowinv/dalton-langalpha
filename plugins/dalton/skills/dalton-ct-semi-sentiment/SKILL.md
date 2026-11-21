---
name: dalton-ct-semi-sentiment
description: "无人值守的 CT 半导体情绪日报：汇总十个固定账号过去二十四小时的观点和温度，并用 automation_report 提交纯文本。"
---

# CT 半导体情绪日报

无人值守。不提问，不创建子任务。不要把中间过程发到任何聊天。

安装路径是 `.agents/skills/dalton-ct-semi-sentiment/`。命令用 `scripts/sentiment_log.py`。写文件、读日志或校验时带 `--workspace-dir <workspace>`。`<workspace>` 用本轮工作区根目录。

接收方在 `metadata.report_profile`，配置名是 `ct-semi-sentiment`。正文和工具参数都不写接收方。

窗口、标题日期、数据备注里的时间和 `report_run_id` 用美洲/纽约时间（America/New_York），即使自动化时区不是纽约。原任务没有写时区，按纽约处理。`report_run_id` 是 `ct-semi-sentiment-<summary_date>`。日期、24 小时窗口、账号名单和补充检索的 query 都用第 1 步的返回，不要自己另算，也不要增减账号。

状态文件是 `state/ct-semi-sentiment/sentiment.log`。草稿放在 `temp/ct-semi-sentiment/<summary_date>/`。发送失败也保留草稿，不要追加日志。

正文先给判断。自己写的句子不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。同一意思的防御性否定句也不用。需要说明边界时，写清证据范围、置信度和会改判的条件。逐字引文、原始报错和固定栏目名保持原样。

账号顺序固定，第一梯队，不要增减：

- `xiaomustock`（川沐）
- `nft_hu`（Fiona）
- `octopusycc`（大老师）
- `bitfool1`（比特傻）
- `blknoiz06`（Ansem）
- `notthreadguy`（ThreadGuy）
- `ShanghaoJin`（Herman Jin）
- `degentradingLSD`
- `qinbafrank`
- `bboczeng`（勃勃OC）

没有中文名的账号只写 `@handle`。

## 1. 窗口和已有温度

```bash
python3 .agents/skills/dalton-ct-semi-sentiment/scripts/sentiment_log.py \
  --json window
```

```bash
python3 .agents/skills/dalton-ct-semi-sentiment/scripts/sentiment_log.py \
  --json tail --workspace-dir <workspace>
```

记下 `start_iso`、`end_iso`、`start_date`、`end_date`、`summary_date`、`report_run_id`、`x_query`、`handles` 和 `x_handles`。`handles` 是带称呼的对象数组，时间线用其中的 `handle`。`x_handles` 是同一顺序的纯字符串数组。`tail` 返回日志最后 3 行；文件不存在时 `exists` 为 false，温度行不写升降。

## 2. 十个账号的时间线

按 `handles` 的顺序逐个调用，不要并行。每个账号失败后重试一次。仍然失败就记下来，继续下一个，不要让整份日报停住。不要传嵌套的 `params`。`tweets` 不要带 `url`、`tweet_id`、`query` 或 `include_replies`。`since` 和 `until` 填窗口两端的 UTC 日历日（即第 1 步返回的 `start_date` 和 `end_date`）。时间线工具在取回之后按 UTC 日历日过滤再截到 `count`，第 4 步还会按精确 24 小时再滤一次。`count` 用 50。

```python
community(
    route="x-timeline",
    operation="tweets",
    handle="xiaomustock",
    since=START_DATE,
    until=END_DATE,
    count=50,
)
```

其余九个账号同样调用。这十次时间线是完整名单，接口不支持翻页，不要尝试翻页、不要编造未返回的帖，也不要用别的搜索代替某一次失败的时间线。把工具返回的 `items`（帖子列表）以及 4 个覆盖度字段原样写入 `raw.json`（直接保留整段返回结构也可以）：`has_more`（是否还有更早的未取回帖子）、`truncated`（是否因达到 `count` 上限被截断）、`oldest_fetched_at`（过滤和截断前抓到的非置顶原始帖最早时间）和 `oldest_returned_at`（过滤和截断后最终返回帖的最早时间）。第 4 步脚本会据此判断：未截断且 `oldest_fetched_at` 早于窗口起点，或已截断且 `oldest_returned_at` 早于窗口起点时视为取全；其余只要 `has_more` 或 `truncated` 为 true，脚本会在 `incomplete_notes` 中自动标出「窗口内可能还有未取回的帖子，最早只取到 <时间>（纽约时间）」（因为有些情况下无法确认被截掉的帖子是否在窗口内）。时间优先使用 `oldest_returned_at`，缺失时回退到 `oldest_fetched_at`；两者均缺失时，若旧版 `raw.json` 里带有帖子时间会改用帖子里最早的时间，否则省略「，最早只取到……」后半句。

把十次结果写成 `temp/ct-semi-sentiment/<summary_date>/raw.json`。`items` 用工具返回的 `data.items`。工具对象本身也可以放进该账号，脚本认 `items` 和 `data.items`。

```json
{
  "accounts": {
    "xiaomustock": {"ok": true, "has_more": false, "truncated": false, "oldest_fetched_at": null, "oldest_returned_at": null, "items": []}
  },
  "x_search": {"ok": false, "items": []}
}
```

十个账号都要有键。失败的写成 `{"ok": false, "items": []}`。

## 3. 一次补充检索

十个时间线都处理完之后，只调用一次 `x_search`。它只补充时间线，不证明名单完整。query 用窗口返回的 `x_query`，不要加宽，也不要换成全市场搜索。`x_search` 的 `handles` 要字符串数组，用窗口里的 `x_handles`，也就是 `[item["handle"] for item in window["handles"]]`，不带 `@`。不要把对象数组直接传进去。不要传 `allowed_x_handles`。

```python
x_search(
    query=X_QUERY,
    from_date=START_DATE,
    to_date=END_DATE,
    handles=[item["handle"] for item in window["handles"]],
    enable_image_understanding=True,
    enable_video_understanding=True,
)
```

`from_date` 和 `to_date` 用窗口返回的 `start_date` 和 `end_date`（窗口覆盖到的 UTC 日历日）。返回的帖子是摘录，不是保证完整的原文。图或视频若有文字说明，放进该帖的 `image_understanding` 或 `video_understanding`。没有这些字段就不要描述图片或视频里的内容。

补充失败，包括工具不接受这两个媒体开关时，继续用时间线。把 `x_search.ok` 设为 false。不要再做一次更宽的 X 搜索，也不要逐帖重复查询。

成功时，`x_search.items` 保留 `author`、`date`、`url`、`summary_excerpt`，以及实际返回的图或视频说明。

## 4. 筛选

```bash
python3 .agents/skills/dalton-ct-semi-sentiment/scripts/sentiment_log.py \
  --json select --workspace-dir <workspace> \
  --as-of "<end_iso>" \
  --input temp/ct-semi-sentiment/<summary_date>/raw.json \
  --output temp/ct-semi-sentiment/<summary_date>/selected.json
```

`--as-of` 用第 1 步的 `end_iso`。脚本丢掉窗口外的帖和纯转推，保留带观点的 quote。同一 status id 或同一原帖 URL 只留一条：补充检索里新出现的图、视频说明和正文上下文并进这条记录，来源写成 `timeline+x_search`。时间线里没有的帖才另加一条，来源是 `x_search`。置顶旧帖按时间排除。补充帖必须带有核验过的原帖 URL、作者和时间，作者还必须是这十个账号之一。

`failed` 是时间线没有取到的账号。`no_posts` 是时间线成功但过去 24 小时没有新帖的账号。`with_posts` 是窗口内有帖的账号。时间线失败但补充检索补到帖时，该账号会同时出现在 `failed` 和 `with_posts`。`incomplete`（兼容旧字段名 `has_more`）列出时间线未能确认取全的账号，`incomplete_notes` 已为这些账号生成现成提示语（格式为「@账号 窗口内可能还有未取回的帖子，最早只取到 <时间>（纽约时间）」；时间线工具未返回 `oldest_returned_at` 和 `oldest_fetched_at` 时，若旧版 `raw.json` 里带有帖子时间会改用帖子里最早的时间，否则自动省略「，最早只取到……」后半句）。`incomplete_notes` 非空时，成稿时逐条原样写入【数据备注】。

## 5. 成稿

用中文写 `temp/ct-semi-sentiment/<summary_date>/report.txt`。去掉首尾空白后不超过 1700 个字符，按 Unicode 字符计，表情和中文都算一个。原任务在超过 1900 个字符时压缩个股信号、不拆成多条。这里先压到 1700 以内，仍然只提交一份。

```text
🧭 CT半导体情绪日报（YYYY-MM-DD）

【共识主线】2-4句：这群人今天在共同交易或讨论什么（存储回调定性、CPO、semicap 设备、仓位动作等）。没有共识就写「无明显共识」，再加一句话概括。

【情绪温度】X/10（1 为极度恐慌，10 为极度贪婪）加一句依据。本轮写出了 N/10，且日志有更早的记录时，和最近一条比，写出 ↑、↓ 或 →。最后一条就是前一公历日时，写较昨日；更早则写较该日期的记录。温度是「本轮无法评分」时不写升降。

【分歧点】1-3条：谁和谁观点相反。没有则写「今日无明显分歧」。

【个股/仓位信号】每人最多 1 条，只列实质新信息（新开仓、加减仓、止盈、新目标价、关键数据），格式「@账号：…」。无新推的不列。

【数据备注】分开列下面四类，没有就写「无」，不要合成一句；`incomplete_notes` 非空时，还须逐条原样写入下面：
时间线抓取失败账号
过去24小时无新推账号
有新推但无半导体相关观点账号
SuperGrok X Search 补充状态
（`incomplete_notes` 非空时，在此逐条原样附入）
```

标题日期用 `summary_date`。温度用半角 `N/10`。四类备注都要出现；第 4 步 `incomplete_notes` 非空时，还必须逐条原样写入【数据备注】（`check` 会逐句核对，缺了就报错）。补充检索成功，且没有新帖、也没有把图、视频或上下文并进已有帖时，写「补充成功，没有新增」。并进已有帖的说明要写进对应的个股或数据备注。补充失败写「补充未完成，日报采用时间线」。

十个时间线都失败时，【共识主线】写「本轮没有取到时间线」，【情绪温度】写「本轮无法评分」。这种日报仍要提交，不要改成静默，也不要追加日志。

有帖但没有共同主题时，写「无明显共识」加一句话，不要为了有共识把不同话题并成一条。

```bash
python3 .agents/skills/dalton-ct-semi-sentiment/scripts/sentiment_log.py \
  --json check --workspace-dir <workspace> \
  --file temp/ct-semi-sentiment/<summary_date>/report.txt \
  --date <summary_date>
```

校验失败就先压缩【个股/仓位信号】，改草稿后再校验。改完后仍失败，不提交这份正文，改提交一行失败说明。

## 6. 提交，确认后再记日志

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

返回里的 `report_run_id` 一致、`content_kind` 为 `text`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent` 时，才处理日志。正文里有 `N/10` 才追加。温度是「本轮无法评分」时不要追加。失败说明确认发送后也不要追加。

日志一行是 `YYYY-MM-DD | N/10 | 一句话主线`。日期用 `summary_date`，分隔符和斜杠用半角。一句话主线用共识主线的那一句，不要把整份日报写进去。`<line>` 就是这一行。

```bash
python3 .agents/skills/dalton-ct-semi-sentiment/scripts/sentiment_log.py \
  --json append --workspace-dir <workspace> \
  --delivery-confirmed true \
  --status sent \
  --content-kind text \
  --date <summary_date> \
  --line "<line>"
```

同一天已经有完全相同的一行时，脚本保留原文件。同一天已有不同的一行时返回 `log_conflict`，不要改日志，也不要换第二个 `report_run_id`。

未确认就用同一标识和同一份正文再提交。同一 `report_run_id` 已保存不同正文时返回 `content_conflict`，不要改正文，也不要换第二个标识。`not_automation_run` 时停止，不追加日志，最终回复说明本次不是自动化运行，不附日报正文。

文本确认发送且该记的日志已经记完，最终回复只写 `NO_REPLY`。

失败时不传 `text`，一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="半导体情绪日报第 N 步失败：原因",
)
```

失败说明确认发送后，最终回复只写 `NO_REPLY`。没能保存时，最终回复只留这一行。不要发第二份正文。
