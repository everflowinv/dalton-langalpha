---
name: dalton-humanoid-robot-weekly
description: "无人值守的人形机器人行业周报：按证据门槛扫描模型、部署、经济性与社区信号，并用 automation_report 提交卡片。"
---

# 人形机器人行业周报

无人值守。不提问，不创建子任务。来源按顺序扫描，避免漏掉引用。

安装路径是 `.agents/skills/dalton-humanoid-robot-weekly/`。命令用 `scripts/humanoid_robot_weekly.py`，写文件时带 `--workspace-dir <workspace>`。卡片结构用 [`templates/weekly-card.md`](templates/weekly-card.md)。来源名单在 [`references/source-universe.yaml`](references/source-universe.yaml)。

接收方在 `metadata.report_profile`，配置名是 `humanoid-robot-weekly`。卡片和工具参数都不写接收方。

状态文件是 `state/humanoid-robot-weekly/state.json`。草稿在 `temp/humanoid-robot-weekly/`。确认发送之后，正文写入 `research-outputs/humanoid-robot-weekly/YYYY-MM-DD.md`，再记入状态。导入历史状态只由主助手执行一次，本轮不要运行导入脚本。

正文先给判断。不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。栏目、证据等级、订单和演示标签保持原样。

监控优先级：模型与控制的可验证进展，单客户或单工厂的规模化部署，经济性与可靠性，重要产品、融资、并购和量产节点，本周新增 Guidepoint 访谈，专业社区信号，安全、监管、劳工和供应链约束。

## 1. 自检

自检失败就停止，不发周报卡片。按第 8 节提交一行失败说明。

```bash
python3 .agents/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py \
  --json self-test --workspace-dir <workspace>
```

通过条件：Python 不低于 3.12，模板和来源名单都在，状态目录可写，共享卡片构造可以加载。

## 2. 窗口和计划

```bash
python3 .agents/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py \
  --json plan --as-of "<香港现在的 ISO 时间>"
```

默认窗口是运行时刻向前 7 天，时区 Asia/Hong_Kong。计划里有门槛、网页 query、三次 Guidepoint query、15 个账号和工具调用形状。`missing_params` 里的参数不要传。

## 3. 模型能力

只有 A 或 B 进入「模型与控制」。

- A：论文或技术报告，加上可复现的代码或权重，加上标准或公开评测。最好有第三方复现。
- B：论文或技术报告，评测协议、基线、样本量和实机结果清楚，代码或权重未公开。
- C：公司视频、演示、采访或营销声明，没有可审计评测。只放产品新闻，必须写「演示级证据」。
- D：二手转述、无原始链接、剪辑视频、匿名爆料。丢掉。

每项写明：谁发布了什么，解决哪类任务，相对什么基线，是否闭环实机，是否包含未见物体、场景、任务或本体，时长、成功率、样本量、延迟和人工介入有没有披露，代码、权重、数据、模型卡、视频和论文是否公开，最大限制是什么。不做单一综合能力分数。

每周覆盖 arXiv cs.RO 以及相关 cs.AI、cs.LG，OpenReview 上的 CoRL、ICRA、IROS、RSS、NeurIPS、ICML 机器人方向，DeepMind Robotics、NVIDIA Isaac、Physical Intelligence、Skild AI、TRI，以及 Stanford、Berkeley、CMU、MIT、NYU、UT Austin 相关实验室。关键词包括 vision-language-action、robot foundation model、whole-body control、dexterous manipulation、long-horizon、failure recovery、cross-embodiment、sim-to-real。候选按本周问题排序，不按热度排序。

网页检索用 `web_search`。`start_date` 和 `end_date` 用计划窗口。`topic` 可传 `news`，当前实现会忽略它，结果还要再核对发布日期。

```python
web_search(query=QUERY, start_date=START_DATE, end_date=END_DATE, topic="news")
```

计划里的 `web_search` 列表逐条执行，包括中文、日文、韩文和德文部署查询。

## 4. 规模化部署

主栏目只收已运行案例，同时满足：

- 单一具名工厂或站点实际运行不少于 250 台，或单一具名客户跨站点实际运行不少于 500 台。
- 客户、地点、机器人型号、生产场景、运行状态至少四项可核实。
- 已运行和订单分开写。

这些不进主栏目：几十台或一两百台试点，LOI、MOU、框架协议和产能目标，只有机器人公司自述，展会、实验室和剪辑视频，数量、场景或是否付费没有披露的合作。

规模化订单观察只收单一具名客户不少于 1000 台的有约束订单或采购合同。必须写「订单，尚非部署」，并写清交付计划和取消条款或付款证据是否可见。不与已运行部署合并计数。

每个部署条目写：客户和工厂、国家或城市、机器人和供应商、已运行数量、另列的订单数量、工序、班次或日运行小时、开始时间、生产状态、远程操作和人工介入、产出、良率、利用率、停机、维护，以及证据等级和至少两个独立来源。优先客户或工厂一侧的原文。

本周没有达到门槛的案例，写「本周无符合门槛的规模化部署」。不要用小试点填版面。

## 5. 经济性、产品和融资

经济性单独成栏。优先追踪售价、RaaS 单价、每小时综合成本、利用率、连续运行、故障间隔、维修时间、远程操作占比、监督人数、吞吐和良率、集成周期、回本期、重复订单、退货或暂停、保险和安全事故。没有披露的数字写「未披露」，不要推算。

产品新闻只收：新机器人或重大代际更新，价格、量产时间、产能、关键硬件或模型栈的实质变化，新代码、权重、数据集或 benchmark，大型客户、并购、破产、停产、召回和安全事故。

融资只收已披露的机构融资、债务、并购或战略投资。金额、轮次、投资方、估值未披露就写「未披露」。融资事实和媒体传闻分开。资金用途指向研发、产能、现场部署或并购时直接写，不把融资金额写成技术领先。

另外固定看这些维度：真实数据小时和失败样本回流，遥操作和现场工程师这些人工成本，同一客户是否进入第二工厂或第二工序，MTBF 和 MTTR，执行器、丝杠、减速器、灵巧手和电池，安全与监管，每小时成本和回本期。

## 6. Guidepoint

分三次原子检索，不要把所有词堆进一条 query。`limit` 用 30。`start` 和 `end` 是窗口。`recency_bias` 只是排序权重，不代替日期窗口。`keywords` 用公司、产品或主题词，最多 10 个。query 写研究问题。

```python
research(
    route="guidepoint",
    operation="search_library",
    query=QUERY,
    start=START_DATE,
    end=END_DATE,
    limit=30,
    params={"keywords": ["humanoid", "factory deployment"], "recency_bias": 50},
)
```

三次问题用计划里的 `guidepoint_queries`。只收窗口内完成并发布的访谈。按 transcript id 去重。只是提到自动化或 AI、主题不是人形或具身机器人的结果剔除。每份访谈最多引用一段，少于 20 个英文词。引用后如果结果里有 `source_attribution.markdown` 就紧跟它；没有就用对应 `_sources` 的 publisher、as_of、title 和 url。publisher 是角色和公司。不写专家姓名。专家观点和已核实事实分开。没有相关新增就写「本周 Guidepoint 无相关新增访谈」。

已经知道完整 `doc_id` 的卖方全文，可以用 `research(route="alphaengine", operation="get_document", params={"doc_id": DOC_ID})` 阅读。这不是本周额外必做的一条扫描。

## 7. 社区

时间线用 `community`，不要传嵌套 `params`。`since` 和 `until` 是日历日，取回后还会再按窗口过滤。15 个 handle 用计划里的列表，`count` 用 20。

```python
community(
    route="x-timeline",
    operation="tweets",
    handle="physical_int",
    since=START_DATE,
    until=END_DATE,
    count=20,
)
```

主题补充用一次 `x_search`。`handles` 就是这 15 个账号。不要传 `allowed_x_handles`、`enable_image_understanding` 或 `enable_video_understanding`。这些参数不存在。返回的是摘录。图片和视频没有被读到时，在来源栏写明可读文本是正文或摘录。

```python
x_search(
    query="humanoid robot deployment OR foundation model OR factory",
    from_date=START_DATE,
    to_date=END_DATE,
    handles=HANDLES,
)
```

精确帖用 `community(route="x-timeline", operation="tweet", url=URL)`，长线程用 `operation="thread"`。按 URL 去重。

Reddit 当前没有对应数据工具。名单只作来源记录，不要用代理、浏览器 cookie 或其他抓取路径去绕。社区内容只作发现、争议和开发者反馈，不作为部署量、收入、价格或产能的一手来源。

同一论文、同一融资、同一客户部署只留一条，以原始来源为主。关键数字至少两个独立来源。

## 8. 成稿、校验、发送

按模板写 `temp/humanoid-robot-weekly/<report_date>.md`。标题用 `🤖 人形机器人行业周报 — <report_date>`。副标题写监控窗口，日期用「至」。没有内容的栏目仍留模板里的那句空栏说明。新稿的模型栏目标题用 `## 🧠 模型与控制`。不用 Markdown 表格。公司演示写「演示级证据」。订单写「订单，尚非部署」。部署写「已运行」。每个数字紧跟来源和日期。

`report_run_id` 用 `humanoid-robot-weekly-<report_date>`。

```bash
python3 .agents/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/humanoid-robot-weekly/<report_date>.md

python3 .agents/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py \
  --json render-card --workspace-dir <workspace> --validate \
  --file temp/humanoid-robot-weekly/<report_date>.md \
  --output temp/humanoid-robot-weekly/<report_date>-card.json
```

校验失败就不渲染、不提交卡片，按失败行处理。

```python
automation_report(action="submit", report_run_id=RUN_ID,
                  material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

只有 `report_run_id` 一致、`content_kind` 为 `cards`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才把正文复制到 `research-outputs/humanoid-robot-weekly/<report_date>.md`，然后：

```bash
python3 .agents/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py \
  --json mark-sent --workspace-dir <workspace> \
  --report-date <report_date> \
  --archive research-outputs/humanoid-robot-weekly/<report_date>.md \
  --window-start "<start_iso>" \
  --window-end "<end_iso>" \
  --validation passed
```

未确认时不记状态，不用第二个 `report_run_id`。同一日期已有不同正文哈希时，脚本保留原状态。同一 `report_run_id` 已保存不同正文时，工具返回 `content_conflict`，不要改卡片，也不要换第二个标识。`not_automation_run` 时停止，不归档，最终回复说明本次不是自动化运行。

确认发送后，最终回复只写 `NO_REPLY`。

失败时 `cards` 省略，一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="人形机器人周报第 N 步失败：原因",
)
```

失败文本确认后最终回复只写 `NO_REPLY`。没能保存时，最终回复只留这一行。
