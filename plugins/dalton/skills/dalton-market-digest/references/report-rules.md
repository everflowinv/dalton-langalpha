# 晨报和晚报正文

交出去的 Markdown 只有一份。标题、栏目、归档和卡片用的都是这同一段字符串。校对只改语言和标点，不改栏目、顺序、已有数字、抓取状态和结束规则。

## 标题和栏目

晨报第一行正好是：

`# 📊 市场晨报 — <authoritative_date>`

晚报第一行正好是：

`# 📊 市场晚报 — <authoritative_date>`

破折号是 `—`。日期来自 prepare 的 `authoritative_date`，不用主机时钟，也不用 `date` 命令。

栏目顺序和层级固定如下。`##` 是地区和大组，美股下面的五个子栏是 `###`。不要把子栏压成一长段 `##`，也不要加中英双标题。

```markdown
# 📊 市场晨报 — 2026-11-10

## 🇺🇸 美股

### 大型科技与 AI

### 半导体

### 软件

### 金融

### 其他行业亮点

## 🇨🇳 中国/港股

## 🇰🇷 韩国

## 📌 宏观与地缘政治

## 🎯 会议与活动

## 📝 来源
```

晚报只把第一行换成 `# 📊 市场晚报 — <authoritative_date>`，其余栏目相同。

每个 `##` 和 `###` 单独占一行，前后各留一个空行。内容留在对应标题下面。每个标题下的条目用 `- ` 开始，标题之后重新起一组列表，不把上一段的编号带到下一段。

`## 📝 来源` 是最后一个 `##`。提交前确认每一节的最后一条都还在，来源的最后一行也还在。长段落交给渲染拆分，正文里不删句子，也不为了塞进卡片而追加 `...`。

某一节这一轮没有内容时保留标题，标题下写一条 `- 本轮此栏无新材料`。不要删掉整节，否则栏目顺序会变。

## 怎么写每一条

按主题写，不按邮件顺序写。每条先写发生了什么、为什么重要，再写交易含义。判断须有来源材料支持。

标题和栏目用中文。公司名、ticker、正式指标名和邮件里的原文术语可以保留。同一对象只用一种叫法。普通中文能说清时，不用 Beat、raise、thesis、run-rate 充当中文谓语。

不写“值得注意的是”“核心逻辑是”“市场正在重新定价”这类空壳句，不机械凑满三点。

自行撰写的标题、正文、摘要和最终回复不用这些防御性否定说法，也不用同义改写：不代表、并非、而不是、不能、不构成、不应。先写判断，再写事实、依据、适用条件和待核项。需要写边界时，直接写来源材料的适用范围、置信度和改判条件。逐字引文、原始数字和工具返回的固定字段保持原样。

交稿前再读一遍标题和每条 bullet，确认中文主语清楚、数字和对象配对、来源没有串到别的条目上。所有数字、目标价、百分比和日期都按输入原样保留，不四舍五入成不同的数字。

## 优先级发件人

写栏目之前，在内部核对这三个人，清单不放进正文：

- Jeffrey Favuzza
- Peter Callahan
- Christian DeGrasse

在拼好的 `emails` 里查看每封邮件实际带有的 `from`、`sender`、`from_name` 和 `subject`。每个人写成 `FOUND in email #序号 "主题"` 或 `NOT FOUND`。`priority_senders` 只是名字到布尔值的索引；它不代替主题，也不代替要点。

FOUND 的人先把要点、交易想法和当时的市场氛围写成内部条目，再放进对应主题。来源节写上这些人的名字。未在邮件里出现的人写成 NOT FOUND，不要写成“没有观点”。

## 邮件、TMTB 和 FUNDA

`emails` 里的邮件都要读。`source` 为 `funda-ai` 且 `research_eligible` 为 false 的验证码或订阅管理邮件跳过，不写进栏目。其余邮件按主题进入上面的栏目。

TMTB 用 `tmtb_docs` 的全文。多数落在美股的大型科技与 AI、半导体或软件，不单开 TMTB 栏目。引用 TMTB 的 bullet 末尾标「（TMTB）」。和卖方邮件冲突时两边都写，并写明各自出处。`pending_snippets` 不是全文：这些条只用摘要来写，正文和来源里标「仅摘要」。

来源节单列一行：`TMTB（AlphaEngine）：<标题1>；<标题2>`。标题用文档里实际有的标题字段。`tmtb.status` 为 `no_new_docs` 且没有文档、也没有 pending snippet 时，不写这行。`tmtb.status` 为 `alphaengine_unavailable`，或 `tmtb.degraded` 为 true 时，日报仍用已经拿到的邮件来写，结束行按 SKILL.md 披露 TMTB 未同步和已有的 `tmtb.reason`。不在同一次运行里另跑同步。

FUNDA AI 按主题并入同一套栏目，引用末尾标「（FUNDA AI）」。来源节列出这一轮用到的标题和日期，用邮件对象里实际有的字段。和 TMTB 或卖方邮件有分歧时保留双方。同一篇上游研报被转述多次只算一份来源材料。FUNDA AI 是独立研究，不写成卖方共识，也不写成买方门槛。只用这一轮 `emails` 里的研究内容，不把历史回填再发一遍。

`funda.research_eligible_count`、`funda.research_excluded_count`、`funda.status`、`funda.received`、`funda.pending_documents`、`funda.index_pending` 哪个出现就用哪个。`index_pending` 为 true 时，结束行的正文索引写“待补”。`image_backlog.sources.funda-ai` 或 `sales-notes` 里的 `pending` 是这一来源的待处理图片数，`historical_backlog` 是历史积压，不算这一轮的新稿。

## 什么时候还有正文

`emails` 为空、`tmtb_docs` 为空、`pending_snippets` 也为空，才算这一轮没有输入。这时不写栏目，结束行用 SKILL.md 里的 SKIP 那一行。

没有邮件，但 `tmtb_docs` 或 `pending_snippets` 有内容时，照常写完整栏目并提交。

`preparation_status` 为 `ready_degraded`，或读取结果 `degraded` 为 true 时，仍然写已经拿到的内容。结束行带上结果里已有的 `reason`，不把降级写成失败，也不把它写成未披露。
