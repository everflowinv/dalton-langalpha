---
name: "employee-reviews"
description: "抓 Blind / Indeed / Glassdoor 员工评论：六维评分时间序列、pros/cons 全文、公司级聚合指标。"
---

# employee-reviews

抓 Blind、Indeed、Glassdoor 的员工评论，做员工侧情绪证据。

三条通路互相独立，能力边界不一样，先看清楚再选：

| 站点 | 通路 | 能拿到 | 拿不到 |
|---|---|---|---|
| Blind | 直连 HTTP，零成本 | **全库**六维评分 + 标题 + 岗位 + 地点 + 时间戳；最新 30 条的 pros/cons 全文 | 第 31 条往后的正文（Blind 用 lorem ipsum 占位） |
| Indeed | Firecrawl（带 key，1 credit/页） | 正文、职位、地点、日期、五维公司聚合分，20 条/页可翻 | 长评论被 `Show more` 截断，要全文得单抓该条 URL |
| Glassdoor | Firecrawl（带 key，1 credit） | 总分、推荐率、CEO 认可、前景、人口统计切分、FAQ 里的公司级四维分、首页 3 条正文 | 第 2 页起 302 到登录墙，本 skill 不绕 |

输出只作员工侧情绪证据。薪酬、裁员、人数等事实数字回公司披露或 filing 核对。

## 快速开始

### 自检前的额度检查（适用于本文所有 `self-test` 调用）

1. 先通过现有凭据读取函数在进程内请求 `https://api.firecrawl.dev/v2/team/credit-usage`，只记录 `remainingCredits` 和检查结果，不输出凭据或请求头。标准自检含 Indeed、Glassdoor 两次付费抓取；确认余额足够后再运行下方 CLI。
2. 余额不足或无法确认时，保留免费检查、跳过付费检查：用 `importlib` 加载 `scripts/employee_reviews_cli.py`，仅在该进程内把模块的 `firecrawl_scrape` 替换为抛出明确 `SKIPPED` 原因的函数，再调用 `cmd_self_test(argparse.Namespace(json=True))`。这会运行认证与 Blind 检查，不发 Indeed/Glassdoor 抓取请求；不修改 live 脚本，不改走无鉴权端点。保存自检 JSON，确认付费项均为跳过后再采集 Blind。
3. 将认证可用、余额可用、抓取成功分别判断；`firecrawl.auth` 显示成功不代表余额为正。交付写明 Indeed/Glassdoor 未覆盖及跳过原因，不把跳过记为通过；额度不足时不执行本文的抓取重试建议。

```bash
bash skills/employee-reviews/scripts/run.sh self-test              # 三条通路逐条验活
bash skills/employee-reviews/scripts/run.sh blind --company NVIDIA --pages 1
bash skills/employee-reviews/scripts/run.sh summary --company NVIDIA --pages 40
bash skills/employee-reviews/scripts/run.sh indeed --company Nvidia --pages 3
bash skills/employee-reviews/scripts/run.sh glassdoor --url 'https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm'
```

`--json` / `--quiet` / `--verbose` 在子命令前后都认。

真正的 bootstrap wrapper 在 `scripts/run.sh`：Skill Workshop 只接受
`assets/ examples/ references/ scripts/ templates/` 里的支持文件，根目录只能放 `SKILL.md`。
venv 仍然建在 skill 根目录的 `venv/`。

skill 根目录另有一个 `run.sh` 转发脚本，所以 `bash skills/employee-reviews/run.sh ...`
和 `scripts/run.sh` 等价。**不要把它换成软链接**：`scripts/run.sh` 用
`dirname "${BASH_SOURCE[0]}"` 定位自己，软链接会让它把 skill 根目录当成 `scripts/`，
找不到 `employee_reviews_cli.py`。这个转发脚本不在 Workshop 管辖范围内，
重新 apply 本 skill 后如果丢了，手工补回四行 `exec bash .../scripts/run.sh "$@"` 即可。

## Blind

Blind 是三个站里唯一能零成本翻到底的。页面是 Next.js，评论数据藏在
`self.__next_f.push` 的 flight payload 里，反转义后按 `"reviews":{"list":[` 做括号配对切出 JSON。

```bash
bash skills/employee-reviews/scripts/run.sh blind --company NVIDIA --pages 40 --json > temp/nvda_blind.json
bash skills/employee-reviews/scripts/run.sh blind --company Palantir --since 2026-01-01
```

**正文锁（关键）**：Blind 只对最新一页（30 条）放出 `pros` / `cons` 全文，
从第 31 条起正文被替换成 `Lorem ipsum dolor sit amet...`。CLI 已经把这些正文置为
`null` 并打上 `body_locked: true`，不会让占位文本混进词频统计。

被锁的条目里，下面这些**仍然是真实数据**，可以放心做时间序列：

- `overall` / `career` / `balance` / `compensation` / `culture` / `management` 六维评分
- `summary`（一句话标题）
- `jobgroup`、`memberLocation`、`createdAt`

实测：NVIDIA 库内 1167 条、Palantir 235 条、Meta 11876 条，翻页数量对得上（Palantir 拉满 235/235）。

### 解析公司 slug

```bash
bash skills/employee-reviews/scripts/run.sh blind-resolve --query palantir
```

按常见大小写变体逐个探测。首字母缩写型公司（AMD、IBM）探不到，
直接去 teamblind.com 搜一次，把地址栏 `/company/` 后面那段原样传 `--company`。

### 六维时间序列 + 词频

```bash
bash skills/employee-reviews/scripts/run.sh summary --company NVIDIA --pages 40 --json
```

`monthly_ratings` 用全部抓到的评论算（评分不受正文锁影响）；
`top_pros_terms` / `top_cons_terms` 只统计正文可读的那 30 条，样本偏最新，
引用时要说明这一点。

## Indeed

裸 curl 吃 403（Cloudflare），必须走 Firecrawl。CLI 分两条路：

- **有 key**：打 REST `https://api.firecrawl.dev/v2/scrape`。key 依次从环境变量
  `FIRECRAWL_API_KEY` 和 `~/.config/firecrawl/api_key` 读，脚本本身不存 key。
- **没 key**：退回无鉴权 MCP 公开端点 `https://mcp.firecrawl.dev/v2/mcp`。

有 key 时**不走** MCP 端点。带 `Authorization` 头打 `mcp.firecrawl.dev` 会间歇返回
503 `Firecrawl credential validation is temporarily unavailable`（2026-08-26 实测），
同一时刻 REST 正常。REST 遇到 5xx 会自动回退到无鉴权端点并在 stderr 留一行；
401/402/403 直接报错，不回退——那是 key 失效或额度耗尽，换端点没用。

免费档每月 1000 credits，一次 scrape 记 1 credit，`self-test` 的 `firecrawl.auth`
会打印剩余额度。别开并发，也别为了凑页数反复重试——40 页就是 40 credits。

```bash
bash skills/employee-reviews/scripts/run.sh indeed --company Nvidia --pages 3 --json
bash skills/employee-reviews/scripts/run.sh indeed --company Nvidia --fresh    # 绕开 Firecrawl 缓存
```

两个坑，CLI 都处理了但结果里要留意：

- **featured review 每页置顶重复**，已按 review id 去重，标了 `is_featured`
- **正文被 `...Show more` 截断**，标了 `text_truncated: true`；要全文得单独抓那条 URL
- **两个评论数不是一回事**：`filtered_review_count`（页面标题里那个，美国区筛选后）
  和 `global_review_count`（metadata 里的全球数）差很多，别混用。NVIDIA 是 173 对 322。

## Glassdoor

只有首页。`_P2.htm` 会 302 到 `member/profile/login?reason=bot-detection`，
需要登录态才能往下翻，本 skill 不绕登录墙。

2026-08-26 用付费 key 加 `proxy: "stealth"` 复测过 `_P2.htm`，返回的仍是那张
bot-detection 登录页。**换更贵的代理挡位解决不了 Glassdoor 翻页**，别再为此烧 credits。

首页本身信息量不小，值得当聚合指标用：

```bash
bash skills/employee-reviews/scripts/run.sh glassdoor --url 'https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm' --json
```

拿得到总分、评论基数、推荐率、CEO 认可率、正面前景比例、行业均值、
按种族/性别/性取向/退伍军人身份切分的评分，以及 FAQ 正文里的公司级四维分。

**口径提醒**：评论卡片里那些六维分是**单条评论**的评分，不是公司均值。
CLI 只从 FAQ 正文抽公司级分数（`company_dimension_ratings`），抽不到就留空，
不拿 3 条评论的均值冒充公司分。

## 与 agent-reach 的关系

agent-reach 的 `ALL_CHANNELS` 是写死在包里的列表，没有插件入口；
往 pipx venv 里塞模块会在下次重装时被抹掉。所以本 skill 独立存在，
按站点分通路的写法对齐 agent-reach 的 channel 契约，将来要上游发 PR 可以直接改写。

社区信源路由（与 AGENTS.md 的 Tools 本地说明 一致）：

- 员工评论 → 本 skill
- 社区情绪 → [近30天社区研究](../last30days-research/SKILL.md)（固定引擎与来源分流；Reddit 未覆盖）
- 雪球帖子 → `scripts/xueqiu_posts.py`；雪球行情/热股 → agent-reach `XueqiuChannel`
- X/Twitter → `xreach` + `x_search`

## 失败排查

先跑 `self-test`，它会分别打通三条路并给出可读结论。

- Blind 报 404：slug 不对，跑 `blind-resolve` 或去站内搜一次
- Blind 首页正文也被锁：self-test 会 FAIL 并明说，说明 Blind 的免费额度变了，别当成解析 bug
- `firecrawl.auth` FAIL：key 过期或额度用完，先跑那条 credit-usage curl 看余额
- Indeed / Glassdoor 报 Firecrawl 错误：额度用完或目标站临时拦截，降页数、隔一会儿再试。
  Firecrawl 偶发瞬时失败，self-test 挂了先原样重跑一次再判断是不是结构变了
- 任何站点结构变了导致解析出 0 条：报出来，不要拿部分结果当全量

## 边界

- 只抓公开页面，不传 cookie、不绕登录墙、不做账号登录
- 结果是员工侧主观陈述，引用时标平台和日期
- 不用于 KYC、背调或对个人的识别
