# 日报正文

一份 Markdown，不用表格。三个业务栏目的 `###` 标题前后各留一个空行。今日要闻、GitHub、Earnings Calls 的编号各自从 1 开始。

```markdown
## 📊 AI + 金融投资日报

### 今日要闻

### GitHub AI 项目动量

### Earnings Calls：AI 的直接业务影响

#### 跨日共性与新趋势

### 来源说明
```

`#### 跨日共性与新趋势` 放在业绩会栏目里、来源说明之前。报告工具会在这个子区块前加分隔线。内容用 `signals.md` 的历史判断，同时使用本轮 GitHub、本轮业绩会和入库前的 matches。

各栏目的条目写法：

- 今日要闻见 `news.md`。5 到 8 条；不足 3 条时保留“今日相关重大新闻较少”。
- GitHub 见 `github.md`。0 到 8 条严格三行的项目，或“今日没有达到阈值的新项目或持续热点”。
- 业绩会公司见 `earnings.md`。最强最多 8 家详细写，其余高质量 signal 逐家一行放在“其他机会雷达”。没有可验证披露时写“本窗口没有找到可验证的 AI 直接影响披露”。

未核实的数字不进入正文。内容较长时保留定量证据和投资含义。

## 来源说明

来源说明按下面的句子写，计数用本批真实字段。某个字段没有就不编数字。

第一行是斜体固定句，日期用本轮 `complete_through`：

`_由 Eve 自动生成；新闻与业绩会为发送前 24 小时，GitHub 总 stars 为抓取时点，日增为 YYYY-MM-DD 的官方完整日期桶_`

然后是这些行：

`新闻搜索成功 X/14 条 query。`

X 是 `news.md` 的成功条数。十四条全部失败时，同一节写“搜索工具异常”和工具返回的错误。

`GitHub：日/周/月榜与持久观察池发现，官方增星历史核验 H/N 个仓库；本轮展示 T/8 个，其中首次 F 个、持续 C 个。`

H 是 `coverage.history_verified`，N 是 `coverage.checked_repositories`，T 是正文编号项目数，F 是其中首次入榜，C 是其中持续热点。同一节写明失败来源、`data_gaps`、待核验或待复核的数量，以及名额外观察数量。不列候补项目名称。没有缺口时写“无”。第三方榜单增量不作为正文数字。

`GitHub 状态：本轮确认降温的已报项目逐个列名；没有则写无。48 小时上游聚类检出 K 组，确认 M 组、未能确认 K-M 组。`

降温名单只来自 `cooled` 里已经报过的仓库。核验失败的仓库留在缺口，不写入这句的降温名单。K 是 `upstream_clusters` 的组数，M 是 README 或官方页面已经核实的组数。未能确认的聚类按 `github.md` 写“cluster X 未能确认上游”。

`业绩会：AlphaEngine Earnings Calls（type_id: IND_SECOND_5FA442AE），过去 24 小时无市场/行业/市值限制的 AI 关键词检索；命中 N 篇，按相关性排序后全文扫描 X 篇，80篇预算跳过 B 篇，失败 Z 篇；AI candidates C 篇中 signal S / watchlist W / reject R。`

N 是 `coverage.keyword_matches_in_exact_window`，X 是 `coverage.full_text_scanned`，B 是 `coverage.skipped_due_to_budget`，Z 是 `coverage.fetch_errors`，C 是 `coverage.ai_candidates`。S、W、R 是三种 disposition 的条数，三者之和等于 C。`coverage.search_query_errors` 大于 0 时，同一节写明失败 query 数。

`历史共性：AI signal memory（post-ingest E/N signals 已用 gemini-embedding-2 向量化；历史检索模式按 compare 输出填写），当前累计 signals N 条。`

这里的 E 是入库后的 `embedded_signals`，两个 N 里，向量化比例用 `embedded_signals` 与 `signals`，累计条数用 `stats.signals`。`embedding_status` 为 `complete` 时写 E/N 已用 `gemini-embedding-2` 向量化，此时 E 等于 N。`partial` 或 `none` 时写实际 E/N、`embedding_coverage_pct`，以及 `embedding_errors` 里的原文。模型名以 `configured_embedding_model` 为准。

`search_modes` 含 `empty_history` 时写“本轮入库前历史库为空”。历史库非空且 `search_modes` 含 `fts` 时写“历史检索 FTS 回退”。其余模式按 compare 的 `search_modes` 原样写。

## 成稿语言

自行撰写的标题、正文、摘要、分析、研究底稿和最终回复，先写判断，再写事实、依据、适用条件和具体待核项。边界和不确定性写成证据范围、置信度，以及什么新事实会改变判断。

这些自行撰写的文字不用下面的说法，也不用它们的同义改写：不代表、并非、而不是、不能、不构成、不应。

交稿前逐句检查。发现上述说法就整句改写，保留事实、数字、责任归属、结论强弱和风险。逐字引文、原始转写、代码、命令、原始报错和固定协议字段保持原样。上面各节的固定栏目、固定短句、计数字段和失败披露保持原样。

本轮若把撰写交给子任务或其他模型，把本节全文放进该任务，汇总时再逐句检查一次。
