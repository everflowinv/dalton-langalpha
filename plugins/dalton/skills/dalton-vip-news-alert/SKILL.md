---
name: dalton-vip-news-alert
description: "无人值守的 VIP 动态速递：汇总七位人物过去 24 小时的采访、新闻与社交动态，并用 automation_report 提交卡片。"
---

# VIP 动态速递

无人值守。不提问，不创建子任务。不要把中间过程发到任何聊天。本轮只做这一份速递。

安装路径是 `.agents/skills/dalton-vip-news-alert/`。命令用 `scripts/vip_news_alert.py`。每条命令都带 `--workspace-dir <workspace>`。`<workspace>` 是本轮工作区根目录。

接收方在 `metadata.report_profile`，配置名是 `vip-news-alert`。卡片、工具参数和草稿都不写接收方。

脚本只做确定性的部分：香港时间窗口、各工具原始结果的合并、严格 24 小时过滤、URL / 标题 / status id 去重、关键词归人、卡片 JSON、以及确认发送后的 seen 写入。合并同一事件、匹配视频、严格过滤、中文摘要、采访记录整理由你完成。脚本不编摘要。

人物、关键词、wire 账号和 YouTube 查询以脚本为准。`list-vips` 和 `plan` 会打印它们。不要另写一份名单，也不要改查询词。

自行撰写的标题、摘要、卡片和最终回复遵守文末撰写硬规则。逐字引文和原始转写保持原样。脚本不扫描这些措辞，也不会因此拒绝渲染。

## 分工

- 你按 `plan` 调用时间线、网页检索、YouTube 检索和一次 `x_search`，把原始结果写入 `raw.json`。
- `filter` 按精确 24 小时和去重规则筛出候选人。
- 你做合并、匹配、严格过滤、中文摘要，并在有字幕时写采访记录文件。
- `render` 生成卡片。你提交 `automation_report`。
- 只有卡片确认发出之后，才运行 `commit-seen`。

草稿在 `temp/vip-news-alert/<summary_date>/`：`raw.json`、`filtered.json`、`events.json`、`card.md`、`card.json`、`manifest.json`、`confirm.json`。采访全文在 `research-outputs/vip-news-alert/<summary_date>-transcripts.md`。去重状态在 `state/vip-news-alert/seen.json`。路径都相对工作区。不要写绝对路径，不要用上级目录。

## 1. 窗口、自检、名单、计划

窗口是亚洲/香港时间的严格 24 小时，两端都包含：`[结束时刻减 24 小时, 结束时刻]`。自动化进程的时区即使不是香港，也按香港计算。`summary_date` 是结束时刻的香港公历日。`report_run_id` 是 `vip-news-alert-<summary_date>`。后面的 `<end_iso>`、`<summary_date>`、`RUN_ID` 都用这一步的字段。

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json window --workspace-dir <workspace>
```

记下 `start_iso`、`end_iso`、`start_date`、`end_date`、`summary_date`、`search_start_date`、`search_end_date`、`x_from_date`、`x_to_date`、`report_run_id`。

自检失败就按文末失败节处理，不继续采集。

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json self-test --workspace-dir <workspace>
```

名单用来核对七位人物和 YouTube 查询词。顺序是 Trump、Bessent、Warsh、Demis、Elon、Sam、Dario。显示名是 Donald Trump、Scott Bessent、Kevin Warsh、Demis Hassabis、Elon Musk、Sam Altman、Dario Amodei。

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json list-vips --workspace-dir <workspace>
```

计划命令的 `--as-of` 用窗口的 `end_iso`。筛选、渲染、写入 seen 也用同一个 `end_iso`。脚本按这个时刻重算窗口，不读 raw 里自带的窗口。

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json plan --workspace-dir <workspace> \
  --as-of <end_iso>
```

`plan` 不写文件。它给出四组请求：

- `wires`：8 个账号，顺序为 DeItaone、CNN、WSJ、Bloomberg、SkyNews、FT、FoxNews、TrumpDailyPosts。每项有 `handle`、`since`、`until`、`count`。`since` 和 `until` 是窗口两端的香港公历日。`count` 为 50。
- `web_search`：每位人物一条。`query` 形如 `"Donald Trump" (interview OR speech OR says OR remarks)`。`start_date` 是窗口起点的 UTC 公历日，`end_date` 是窗口结束时刻再加一天之后的 UTC 公历日。
- `youtube`：每位人物的每条原查询各一行，查询词末尾已加 `site:youtube.com`。日期字段与 `web_search` 相同。
- `x_search`：只有一条。`from_date` 和 `to_date` 是严格 24 小时窗口覆盖到的 UTC 公历日。两个理解开关都为 true。没有 `handles`。
- `fetch_prompt`：稍后取字幕时原样使用。

## 2. 采集

按这个顺序调用。调用工具，不从 Python 导入，也不用 Bash 调用这些工具。每一跳失败后只重试这一次。仍然失败就把该路标成 `ok: false`，继续其余各路。不要让单路失败停掉整份速递。

### 2a. 八个 wire 时间线

按 `wires` 的顺序逐个调用，不要并行。不要传嵌套的 `params`。`tweets` 不要带 `url`、`tweet_id`、`query` 或 `include_replies`。`handle` 不要加 `@`。`*DeItaone` 的字母是 I。

```python
community(
    route="x-timeline",
    operation="tweets",
    handle="DeItaone",
    since=START_DATE,
    until=END_DATE,
    count=50,
)
```

另外七个 handle 用各自的 `since`、`until`、`count` 同样调用。`START_DATE` 和 `END_DATE` 用该项的 `since` 和 `until`。返回里的 `has_more` 为 true 时照写。不要翻页，也不要编造未返回的帖。转发也保留，脚本按关键词归人。

### 2b. YouTube 检索

`youtube` 的每一行调用一次 `web_search`。工具名是 `web_search`。签名还有 `time_range` 和 `topic`，本任务都不传。也不传 `count`、`country`、`language`、`domain_filter`、`search_lang`、`ui_lang`。不要用 `freshness` 或 `time_range` 代替精确 24 小时。同一人物的多条结果以后合并进该人物的 `videos`。

```python
web_search(
    query=YOUTUBE_QUERY,
    start_date=SEARCH_START_DATE,
    end_date=SEARCH_END_DATE,
)
```

`YOUTUBE_QUERY`、`SEARCH_START_DATE`、`SEARCH_END_DATE` 用该行自己的 `query`、`start_date`、`end_date`。

### 2c. 网页新闻

`web_search` 的每一行调用一次，参数规则与 YouTube 相同。查询词用该行的 `query`，不要改成按人物拆开的别的句子。

```python
web_search(
    query=WEB_QUERY,
    start_date=SEARCH_START_DATE,
    end_date=SEARCH_END_DATE,
)
```

### 2d. 一次 X 补充

网页新闻都处理完之后，只调用一次 `x_search`。不要拆成七次。它只补充 wire 和网页，不替代这两个来源。签名是 `query`、`from_date`、`to_date`、`handles`、`enable_image_understanding`、`enable_video_understanding`。本任务打开两个理解开关，不传 `handles`。

```python
x_search(
    query=X_QUERY,
    from_date=X_FROM_DATE,
    to_date=X_TO_DATE,
    enable_image_understanding=True,
    enable_video_understanding=True,
)
```

四个字段都用 `plan` 里 `x_search` 的值，不要改写 query。返回的总摘要不要放进 `items`，也不要写进卡片。`items` 只收作者、原帖 URL 和时间都能对上的帖。补充失败时 `x_search.ok` 设为 false，继续写 raw，筛选备注会写明。

### 2e. 写成 raw.json

把结果写成 `temp/vip-news-alert/<summary_date>/raw.json`。`items` 用工具返回的列表。工具放在 `data` 里时照原样留下，脚本两种都认。时间字段保持工具原文，不要编造。网页和视频如果带 `date`、`published_date` 或 `publishedDate`，原样留下。没有这些字段时也要写入：`web_search` 的引用通常只有 `url` 和 `title`。视频若有 `duration`（秒）就原样留下；没有就省略该字段。

```json
{
  "wires": {
    "DeItaone": {"ok": true, "has_more": false, "items": []},
    "CNN": {"ok": false, "items": []}
  },
  "web": {
    "Trump": {"ok": true, "items": []}
  },
  "videos": {
    "Trump": {"ok": true, "items": []}
  },
  "x_search": {"ok": true, "items": []}
}
```

八个 wire、七个 `web`、七个 `videos` 都要有键。没跑成的键写成 `{"ok": false, "items": []}`。缺键会被当成这一路没有返回。

## 3. 筛选

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json filter --workspace-dir <workspace> \
  --input temp/vip-news-alert/<summary_date>/raw.json \
  --output temp/vip-news-alert/<summary_date>/filtered.json \
  --as-of <end_iso>
```

`--as-of` 用第 1 步的 `end_iso`。这一步不写 `seen.json`。

筛选规则：

- wire 和 `x_search` 只保留 `start_iso` 到 `end_iso` 之间的条目，两端都包含。刚好 24 小时前的留下，再早一秒的丢掉。没有时间、时间无法解析的丢掉。纯日期按 UTC 零点理解。没有时区的时钟时间按 UTC 理解。正文为空的 wire 推文，只要时间在窗口内，仍参加关键词归人。
- 网页和视频：有可解析时间且落在窗口外的丢掉。没有可解析时间的保留，并标 `time_unverified: true`。`web_search` 已用 `start_date` 和 `end_date` 限定检索日期。对 `time_unverified` 的条目，取字幕或读页面时看页面上的发布时间。能确认早于窗口的，不要写进卡片。确认不了的可以写进卡片，这时不要写成“今天发生”。
- 去重顺序是 wire、视频、网页、`x_search`。先出现的留下。同一 status id、同一规范 URL、或同一标题哈希只留一条。X 和 YouTube 链接会先收成规范形式。网页结果里的 youtube.com 和 youtu.be 链接归入该人物的视频组，标题保留，随后走取字幕流程。
- `state/vip-news-alert/seen.json` 里未过期的 URL 哈希和标题哈希也会丢掉。超过 7 天的记录不挡新条目。这一步只读不写。
- wire 用关键词归人，名单顺序先命中先归入。一条同时含多个人物关键词时，只出现在更靠前的那一位下面。匹配用正文前 500 个字符。网页留在检索的那位人物下。视频留在检索的那位人物下。这两类都不因为缺少关键词被丢掉。
- `x_search` 命中关键词的帖进入该人物的 `x_posts`。作者、URL、时间都有、但正文没有关键词的帖进入 `unmatched_x`，留给你判断是不是本人或机构原帖。缺作者、缺 status URL 或缺时间的帖丢掉。
- `duration` 是大于 0 且小于 120 的数字时，这条视频丢掉。缺时长、时长为 0 或不是数字时保留。
- 八个时间线、七组网页、七组视频和 `x_search` 全部失败时，`sources_all_failed` 为 true。只跑其中几位人物时，这个字段保持 false。某一路 `ok` 为 true 但列表是空的，算安静的一天，这个字段为 false。

`notes` 里会出现这些句子，渲染时原样放进 `source_notes`：某账号「时间线没有返回」，某账号「时间线已返回，窗口内可能还有未取回的帖子」，某人「的网页检索没有返回」，某人「的视频检索没有返回」，以及「补充检索没有返回」。

## 4. 字幕、合并和严格过滤

筛选之后，只对仍留在 `videos` 里的链接调用 `WebFetch`。工具名是 `WebFetch`，只传 `url` 和 `prompt`。`prompt` 用 `plan` 的 `fetch_prompt`。

```python
WebFetch(url=VIDEO_URL, prompt=FETCH_PROMPT)
```

抽取器把字幕放在页面 markdown 的 Transcript 章节。`WebFetch` 返回的是按 prompt 抽出的文本，没有单独的字幕字段。返回 `NO_TRANSCRIPT`，或正文写明 No transcript available，或没有字幕正文时，这条视频没有 transcript，照常继续，不要编字幕。

带 `time_unverified` 的网页和视频，取字幕或读页面时看页面上的发布时间。能确认早于窗口的，不要写进卡片。确认不了的可以写进卡片，这时不要写成“今天发生”。`web_search` 已经用 `start_date` 和 `end_date` 限定了检索日期。

然后对筛选结果做这些判断：

- 同一采访或同一事件被拆成多条 wire 时，合并成一条。例如同一次 Trump 采访被 *DeItaone 拆成多条，合并为一个事件。
- 为每个事件找完整视频：先看该人物的 `videos`。网页结果里的 YouTube 链接已经归入这里。再看 `web_articles` 里的 CNBC、Sky News 等非 YouTube 视频页面。找到完整视频就用视频链接；有字幕再整理采访记录。没有视频但有完整网页报道就用网页链接。两样都没有时，只保留中文摘要和来源标注。
- 只保留该人物亲自说了什么、亲自做了什么。第三方分析、评论员讨论、Analysts debate、以及其他人评论该人物的内容都删掉。
- 每条写一两句中文摘要。先给判断，再写事实、数字和原话里的关键信息。
- `unmatched_x` 里只有作者、URL、时间都核验过、并且能判断是本人或机构原帖的，才归入对应人物。第三方评论丢掉。同一事件只写一次。

有任何 transcript 时，把完整采访记录写到 `research-outputs/vip-news-alert/<summary_date>-transcripts.md`。不要创建外部文档。文件放在工作区内，内容非空，不要把这个路径做成符号链接。每位有字幕的采访按下面的结构写，核心要点写 5 到 8 条。修正字幕里明显的错词。主持人和嘉宾的问答分开。

```markdown
# VIP 采访记录 — YYYY-MM-DD

## Donald Trump: 采访标题
频道: 频道名 | 时长: 12分钟 | 链接: https://www.youtube.com/watch?v=VIDEO

### 核心要点
- 要点

### 完整记录
**主持人**: 问题
**Donald Trump**: 回答
```

然后写 `temp/vip-news-alert/<summary_date>/events.json`。`title` 照抄筛选结果里的 `title`，中文写在 `summary`。去重和 seen 用的是 `title`，不要用摘要替换它。`url` 用筛选结果里的 URL。`source` 用筛选结果里的 `source`。有字幕的采访才填 `points`，而且必须是恰好 3 条非空中文要点，从全文那 5 到 8 条里选。没有字幕就省略 `points`。`source_notes` 抄筛选 `notes`；没有备注就省略或用空数组。人物键只用 Trump、Bessent、Warsh、Demis、Elon、Sam、Dario。没有内容的人物省略。同一键不要写两次。

```json
{
  "summary_date": "YYYY-MM-DD",
  "source_notes": ["CNN 时间线没有返回"],
  "people": [
    {
      "key": "Trump",
      "interviews": [
        {
          "summary": "一两句中文",
          "url": "https://www.youtube.com/watch?v=VIDEO",
          "title": "筛选结果里的标题",
          "points": ["要点一", "要点二", "要点三"]
        }
      ],
      "articles": [
        {
          "summary": "一两句中文",
          "url": "https://example.com/story",
          "title": "筛选结果里的标题"
        }
      ],
      "headlines": [
        {
          "summary": "一两句中文",
          "source": "*DeItaone",
          "title": "筛选结果里的原文",
          "url": "https://x.com/DeItaone/status/1"
        }
      ]
    }
  ]
}
```

`summary_date` 必须等于窗口的 `summary_date`。链接只用 http 或 https。摘要、标题、来源都要有正文。动态没有 URL 时可以省略 `url`，有 URL 就写上，卡片正文不展示这行 URL，seen 会用它。

## 5. 卡片

卡片由脚本渲染。不要手写卡片 Markdown，也不要在人物栏目里使用三级或四级标题。

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json render --workspace-dir <workspace> \
  --events temp/vip-news-alert/<summary_date>/events.json \
  --markdown temp/vip-news-alert/<summary_date>/card.md \
  --output temp/vip-news-alert/<summary_date>/card.json \
  --as-of <end_iso>
```

有 `points` 但采访记录文件缺失、为空或是符号链接时，命令失败，不会写出新卡片。`empty` 为 true 时表示过滤后没有内容：不写 `card.md`、`card.json` 和 `manifest.json`，已有的卡片文件保持原样。

非空渲染成功后，同时写 `temp/vip-news-alert/<summary_date>/manifest.json`。`seen_keys` 只含这张卡片实际写出的条目，每项有 `key`、`kind`、`url`、`title`。`content_sha256` 是这张卡片作为 `cards` 提交时的 SHA-256。`commit-seen` 不读取采访记录文件。

非空卡片的栏目和顺序固定：

1. 标题 `📡 VIP 动态速递 — <summary_date>`，破折号是长破折号。
2. 人物按名单顺序。每位一个二级标题 `## ◎ Donald Trump（3条动态）`，括号是全角，条数是采访、新闻和动态的合计。没有内容的人物省略。
3. 人物内部按这个顺序，空栏目省略：`**🎥 采访/演讲:**`，摘要下一行是视频链接；`**📰 新闻报道:**`，摘要下一行是报道链接；`**📰 动态:**`，一行是 `中文摘要 [source: *DeItaone]`。来源名里的 `*DeItaone` 保持原样。
4. 有采访要点时，最后之前是 `## 完整采访记录`。每条是 `**Donald Trump：标题**`、恰好 3 条要点、视频链接。全部采访共用一句 `完整记录已存工作区 research-outputs/vip-news-alert/<summary_date>-transcripts.md`。
5. `source_notes` 非空时，`## 来源` 放在整张卡片的最后。

`ok` 为 true 且 `empty` 为 false 时，读 `card.json` 的对象作为 `CARD`。`card_conflict` 表示磁盘上的卡片和这次渲染不同：保留原文件，不要改 events 后再渲第二张，也不要换第二个 `report_run_id`。

## 6. 提交

调用 `automation_report`，不从 Python 导入，也不用 Bash 调用。`cards` 只含这一张卡片。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

`RUN_ID` 用窗口的 `report_run_id`。只有返回里的 `report_run_id` 与 `RUN_ID` 一致，`content_kind` 为 `cards`，并且 `ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才算发送成功。

把 `ok`、`delivery_confirmed`、`status`、`content_kind`、`report_run_id` 原样写到 `temp/vip-news-alert/<summary_date>/confirm.json`。返回里如果有 `content_sha256`，一并写入。然后写入 seen：

```bash
python3 .agents/skills/dalton-vip-news-alert/scripts/vip_news_alert.py \
  --json commit-seen --workspace-dir <workspace> \
  --confirm temp/vip-news-alert/<summary_date>/confirm.json \
  --as-of <end_iso>
```

`commit-seen` 只读 `manifest.json`，把清单里的 seen 键写入 `seen.json`。它不读 `events.json`，也不读采访记录文件。清单里的 `content_sha256` 必须和脚本对已保存卡片算出的值一致。确认结果里如果带 `content_sha256`，也必须和这个值一致。对不上就拒绝写入，并且不改 `seen.json`。每条同时记下 URL 哈希和标题哈希。已有记录里早于 7 天的会在同一次写入里清掉。发送未确认时，这个命令拒绝写入，也不会改动已有的 `seen.json`。不要手改这个文件，也不要导入以前的 seen。

卡片已经保存（提交返回 `ok` 为 true）而发送还未确认时，只用同一 `RUN_ID` 和同一张卡片再提交一次，用来刷新状态。仍然未确认就停止：最终回复只写 `NO_REPLY`，不要运行 `commit-seen`，不要提交 `failure_text`。平台在确定投递失败后会自行通知失败接收方。

`content_conflict` 表示这个 `report_run_id` 已经保存了不同正文。不要改卡片，不要换第二个标识，不要提交 `failure_text`。最终回复说明已保存的正文和这次卡片不一致，不附卡片正文。`code` 为 `not_automation_run` 时停止：最终回复说明本次不是自动化运行，不附卡片正文。

发送成功后，最终回复只写 `NO_REPLY`。

## 7. 没有内容，以及失败

严格过滤之后，采访、新闻和动态都是空的，并且 `sources_all_failed` 为 false：这是安静的一天。不要提交卡片。可以运行 `render` 确认 `empty` 为 true。然后只提交一次静默结果，不带 `cards`、`text` 或 `failure_text`：

```python
automation_report(action="submit", report_run_id=RUN_ID, material="no")
```

然后最终回复只写 `NO_REPLY`。不要运行 `commit-seen`。这次返回的 `delivery_confirmed` 为 false，seen 不会更新。

`sources_all_failed` 为 true，自检失败，筛选或渲染失败，或提交返回 `ok` 为 false 且 `code` 不是 `content_conflict`：这时卡片还没保存成功。不要编造内容，不要提交空卡片冒充成功。提交一行失败说明，`cards` 省略：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="VIP 动态速递失败：原因",
)
```

`RUN_ID` 用窗口的 `report_run_id`。窗口命令本身失败时，用当天的香港公历日拼 `vip-news-alert-<YYYY-MM-DD>`。失败文本确认发出后，最终回复只写 `NO_REPLY`。工具没能保存这行时，最终回复只留这一行。不要发第二份正文。

部分来源没返回、其余来源成功时，仍然提交卡片，并把筛选 `notes` 放进 `source_notes`。

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
