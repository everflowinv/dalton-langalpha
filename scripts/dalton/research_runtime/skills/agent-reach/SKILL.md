---
name: agent-reach
description: >
  多平台搜索/阅读/互动（Twitter/X, Reddit, YouTube, GitHub, B站, 小红书, 抖音, 微信公众号, LinkedIn, Boss直聘, RSS, Exa 等 13+ 平台）。
  支持搜索、读取、发帖、评论、配置，以及任意网页抓取。
  触发词："搜推特"、"搜小红书"、"看视频"、"搜一下"、"上网搜"、"帮我查"、"全网搜索"、
  "search twitter"、"read tweet"、"youtube transcript"、"search reddit"、
  "read this link"、"看这个链接"、"B站"、"bilibili"、"抖音视频"、
  "微信文章"、"公众号"、"LinkedIn"、"GitHub issue"、"RSS"、
  "search online"、"web search"、"find information"、"research"、
  "帮我配"、"configure twitter"、"configure proxy"、"帮我安装"。
user-invocable: false
---

# Agent Reach — Usage Guide

Upstream tools for 13+ platforms. Call them directly.

Run `agent-reach doctor` to check which channels are available.

## ⚠️ Workspace Rules

**Never create files in the agent workspace.** Use `/tmp/` for temporary output and `~/.agent-reach/` for persistent data.

## Web — Any URL

```bash
curl -s "https://r.jina.ai/URL"
```

## Web Search (Exa)

```bash
mcporter call 'exa.web_search_exa(query: "query", numResults: 5)'
mcporter call 'exa.get_code_context_exa(query: "code question", tokensNum: 3000)'
```

## Twitter/X (xreach)

```bash
xreach search "query" -n 10 --json          # search
xreach tweet URL_OR_ID --json                # read tweet (supports /status/ and /article/ URLs)
xreach tweets @username -n 20 --json         # user timeline
xreach thread URL_OR_ID --json               # full thread
```

## YouTube (yt-dlp)

```bash
yt-dlp --dump-json "URL"                     # video metadata
yt-dlp --write-sub --write-auto-sub --sub-lang "zh-Hans,zh,en" --skip-download -o "/tmp/%(id)s" "URL"
                                             # download subtitles, then read the .vtt file
yt-dlp --dump-json "ytsearch5:query"         # search
```

## Bilibili (yt-dlp)

```bash
yt-dlp --dump-json "https://www.bilibili.com/video/BVxxx"
yt-dlp --write-sub --write-auto-sub --sub-lang "zh-Hans,zh,en" --convert-subs vtt --skip-download -o "/tmp/%(id)s" "URL"
```

> Server IPs may get 412. Use `--cookies-from-browser chrome` or configure proxy.

## Reddit

```bash
curl -s "https://www.reddit.com/r/SUBREDDIT/hot.json?limit=10" -H "User-Agent: agent-reach/1.0"
curl -s "https://www.reddit.com/search.json?q=QUERY&limit=10" -H "User-Agent: agent-reach/1.0"
```

> Server IPs may get 403. Search via Exa instead, or configure proxy.

## GitHub (gh CLI)

```bash
gh search repos "query" --sort stars --limit 10
gh repo view owner/repo
gh search code "query" --language python
gh issue list -R owner/repo --state open
gh issue view 123 -R owner/repo
```

## 小红书 / XiaoHongShu (mcporter)

```bash
mcporter call 'xiaohongshu.search_feeds(keyword: "query")'
mcporter call 'xiaohongshu.get_feed_detail(feed_id: "xxx", xsec_token: "yyy")'
mcporter call 'xiaohongshu.get_feed_detail(feed_id: "xxx", xsec_token: "yyy", load_all_comments: true)'
mcporter call 'xiaohongshu.publish_content(title: "标题", content: "正文", images: ["/path/img.jpg"], tags: ["tag"])'
```

> Requires login. Use Cookie-Editor to import cookies.

## 抖音 / Douyin (mcporter)

```bash
mcporter call 'douyin.parse_douyin_video_info(share_link: "https://v.douyin.com/xxx/")'
mcporter call 'douyin.get_douyin_download_link(share_link: "https://v.douyin.com/xxx/")'
```

> No login needed.

## 微信公众号 / WeChat Articles

**Search** (miku_ai):
```python
python3 -c "
import asyncio
from miku_ai import get_wexin_article
async def s():
    for a in await get_wexin_article('query', 5):
        print(f'{a[\"title\"]} | {a[\"url\"]}')
asyncio.run(s())
"
```

**Read** (Camoufox — bypasses WeChat anti-bot):
```bash
cd ~/.agent-reach/tools/wechat-article-for-ai && python3 main.py "https://mp.weixin.qq.com/s/ARTICLE_ID"
```

> WeChat articles cannot be read with Jina Reader or curl. Must use Camoufox.

## LinkedIn (mcporter)

```bash
mcporter call 'linkedin.get_person_profile(linkedin_url: "https://linkedin.com/in/username")'
mcporter call 'linkedin.search_people(keyword: "AI engineer", limit: 10)'
```

Fallback: `curl -s "https://r.jina.ai/https://linkedin.com/in/username"`

## Boss直聘 (mcporter)

```bash
mcporter call 'bosszhipin.get_recommend_jobs_tool(page: 1)'
mcporter call 'bosszhipin.search_jobs_tool(keyword: "Python", city: "北京")'
```

Fallback: `curl -s "https://r.jina.ai/https://www.zhipin.com/job_detail/xxx"`

## 微博 / Weibo (mcporter)

```bash
mcporter call 'weibo.get_hot_searches()'                          # 热搜榜
mcporter call 'weibo.search_content(keyword: "query", count: 10)' # 搜索内容
mcporter call 'weibo.search_users(keyword: "query", count: 5)'    # 搜索用户
mcporter call 'weibo.search_topics(keyword: "query", count: 5)'   # 搜索话题
mcporter call 'weibo.get_user_timeline(uid: "USER_ID", count: 10)' # 用户动态
mcporter call 'weibo.get_comments(id: "POST_ID", count: 20)'      # 帖子评论
```

> 需要配置 MCP：`pip install git+https://github.com/Panniantong/mcp-server-weibo.git && mcporter config add weibo --command 'mcp-server-weibo'`

## V2EX

```bash
curl -s "https://www.v2ex.com/api/topics/hot.json" | python3 -c "import sys,json; [print(f'{t[\"title\"]} | {t[\"url\"]}') for t in json.load(sys.stdin)[:10]]"
curl -s "https://www.v2ex.com/api/topics/latest.json" | python3 -c "import sys,json; [print(f'{t[\"title\"]} | {t[\"url\"]}') for t in json.load(sys.stdin)[:10]]"
curl -s "https://www.v2ex.com/api/nodes/show.json?name=NODE_NAME"   # 节点信息
curl -s "https://www.v2ex.com/api/topics/show.json?id=TOPIC_ID"     # 帖子详情
curl -s "https://www.v2ex.com/api/replies/show.json?topic_id=TOPIC_ID" # 帖子回复
curl -s "https://www.v2ex.com/api/members/show.json?username=USERNAME" # 用户信息
```

> 无需配置，V2EX API 公开可用。

## 小宇宙播客 / Xiaoyuzhou Podcast

播客音频转文字（火山引擎 Seed-ASR 2.0）：
```bash
bash ~/.agent-reach/tools/xiaoyuzhou/transcribe.sh "小宇宙链接" [/tmp/output.txt]
```

> 复用 transcript-polish skill 的 Seed-ASR 配置，无需额外配置。
> 流程：解析页面 → 下载音频 → 转码 → 切片(>30min时) → TOS 上传 → Seed-ASR 2.0 → 合并输出

## RSS

```python
python3 -c "
import feedparser
for e in feedparser.parse('FEED_URL').entries[:5]:
    print(f'{e.title} — {e.link}')
"
```

## Troubleshooting

- **Channel not working?** Run `agent-reach doctor` — shows status and fix instructions.
- **Twitter fetch failed?** Ensure `undici` is installed: `npm install -g undici`. Configure proxy: `agent-reach configure proxy URL`.

## Setting Up a Channel ("帮我配 XXX")

If a channel needs setup (cookies, Docker, etc.), fetch the install guide:
https://raw.githubusercontent.com/Panniantong/agent-reach/main/docs/install.md

User only provides cookies. Everything else is your job.
