# 过去 24 小时 AI 与金融要闻

事实窗口是 prepare 返回的 `window`。`CURRENT_YEAR` 和 `SEARCH_START_DATE` 的取法见 `SKILL.md`。

## 1. 十四条查询

每一轮十四条全部执行。下面两条英文和中文查询里的 `CURRENT_YEAR` 换成四位年份，其余文字保持原样。

英文：

1. `AI startup funding round announced today Series A Series B Series C Series D CURRENT_YEAR`
2. `new AI model release LLM`
3. `AI finance fintech deal acquisition`
4. `artificial intelligence enterprise partnership`
5. `AI regulation policy government`
6. `AI chip semiconductor infrastructure`
7. `AI product launch consumer enterprise deployment`
8. `AI productivity enterprise workforce layoff efficiency impact`

中文：

9. `AI 大模型 融资 发布 CURRENT_YEAR`
10. `人工智能 投资 金融科技`
11. `AI 芯片 算力 数据中心`
12. `人工智能 监管 政策`
13. `AI 产品发布 消费级 企业级`
14. `人工智能 效率 就业 裁员 企业应用`

使用本轮注册的 `WebSearch`。查询文本放进这次 schema 的 `query`。schema 的查询字段是 `objective` 时，放进 `objective`。只传这次 schema 列出的参数。schema 中有日期或时效字段时，发现范围覆盖 `SEARCH_START_DATE`。不传 country、language、domain、search_lang、ui_lang，也不传 schema 没有列出的 count。

调用完成、返回内容非空、且没有工具错误时记成功。内容写明没有相关结果，或相关结果列表为空，仍然记成功。工具报错或抛出异常记失败，该条最多再执行一次。十四条都结束后数成功条数 X。来源说明的固定句是 `新闻搜索成功 X/14 条 query`。十四条全部失败时，另写“搜索工具异常”，并引用工具返回的错误。

日期过滤只负责发现。它不是发布日期。

## 2. 严格 24 小时与原文

采用的每一条都打开公司公告、监管机构原文或权威媒体的直接文章，核对页面 `datePublished`、明确发布时间或等价元数据。发布时间落在 `window` 内才写入今日要闻。只有日期、没有具体时间、也无法核验是否在窗口内的内容，不写成严格 24 小时事实。

搜索结果里的年度综述、融资榜单和聚合摘要只作线索。融资新闻优先核对公司公告，以及 Business Wire、Reuters、TechCrunch 的直接报道。原始公告早于窗口时，即使搜索结果在窗口内再次提到，也剔除。

打开页面先用 `WebFetch`。`prompt` 要求抽出发布时间、`datePublished` 或等价元数据，以及进入摘要所需的事实句。公开 URL 返回 403 或 429、机器人页、导航空壳，或正文要等脚本才出现时，再用本轮已经注册的公开网页抓取，只取主内容 markdown，并核对状态码、最终 URL 和正文是否完整。不传 cookies、Authorization 或登录态，不打开内网和签名链接，不绕过付费墙或 CAPTCHA。引用底层原始页面。抓取工具不是事实来源。原文仍然打不开时，这条不进入今日要闻。

优先采用 Reuters、FT、WSJ、Bloomberg、The Information、公司或监管机构原文、The Verge、Ars Technica、Wired、TechCrunch、36氪、虎嗅。软文、没有新增事实的观点和预测不采用。

## 3. 同一事件与已播报

同一核心事件跨媒体只保留最权威的一篇。

事件键是标准化英文核心事件的 SHA256，输入是这句英文的 UTF-8 字节，输出是 64 位小写十六进制。比较时用完整的 64 位。键不使用 URL、中文标题、搜索摘要、截断哈希或其他摘要函数。媒体名、栏目标题和转述措辞不进入这句核心事件。

`news_seen` 保存最近 7 天的这些十六进制键和数字时间戳。映射里是哈希和时间戳。本轮为当前事件写出标准化英文核心事件并计算哈希，再与映射中的完整十六进制比较。已经出现的键不写入今日要闻。窗口内新的金额、条款、监管动作或时间是另一条核心事件，使用新的哈希。读取方式见 `SKILL.md`。

## 4. 今日要闻怎么写

选 5 到 8 条。不足 3 条时写明“今日相关重大新闻较少”。

每条一行，编号从 1 开始：

`1. **标题** — 一句话摘要。（[来源](链接)）`

标题和摘要遵守 `report.md` 的成稿语言。链接指向核对过的原始页面。
