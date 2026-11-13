---
name: dalton-mark-weekly-newenergy
description: "无人值守的新能源与中概股周报：按35家公司取数、校验并发布飞书文档，改写模式再提交纯文本摘要。"
---

# 新能源与中概股周度动态

无人值守。不提问，不创建子任务。不使用 `x_search`，不调用 `message`。本轮只做这一份周报。

两种运行模式由自动化指令写明，技能不自行选择。两条自动化共用一个 `report_run_id`（本次周报的运行标识）：`mark-weekly-newenergy-<saturday>`（`<saturday>` 是 `window` 命令返回的 `saturday` 字段，即美东周六日期，格式 `YYYY-MM-DD`）。只有改写模式提交 `automation_report`（自动化报告）。

- **原版**：`collect` 抓取 AlphaEngine 和内部库结果并写入 `state/mark-weekly-news/runs/<saturday>/raw.json`，对话里补 `web_search`，写稿，`check` 到 35/35，发布原版飞书文档。不提交 `automation_report`。结果只写入状态文件。最终回复只写 `NO_REPLY`。
- **改写**：读本周原版定稿，按 `.agents/skills/dalton-mark-weekly-newenergy/references/restyle_style.md` 逐段改写，脚本逐段校验。没通过的段保留原文。整体校验通过后发布改写版飞书文档，然后提交纯文本摘要。原版未完成或失败时，不改写，改由改写模式提交含失败原因的 `text`。

接收方在 `metadata.report_profile`，配置名是 `mark-weekly-newenergy`。技能正文和工具参数都不写接收方。

安装路径是 `.agents/skills/dalton-mark-weekly-newenergy/`。命令用 `scripts/mark_weekly.py`。每条命令都带 `--workspace-dir <workspace>` 和 `--json`。`<workspace>` 是本轮工作区根目录。

名单、分组和行业查询以 `.agents/skills/dalton-mark-weekly-newenergy/references/targets.json` 为准，共 35 家。不要另写一份名单，也不要改查询词。`plan` 会打印窗口、网页检索参数和全文参数。AlphaEngine 与内部库由 `collect` 调用，不要在对话里逐条再调。网页检索按 `plan` 里的 `params` 原样传入，不要增删字段。

旧历史由主助手导入，本轮不要导入，也不要改写已有的 `state/mark-weekly-news/history.json` 里其他周的记录。

自行撰写的标题、正文、摘要和最终回复遵守文末撰写硬规则。逐字引文、原始转写、链接、doc 号（AlphaEngine 文档编号）和【】标签保持原样。脚本的 `style_warnings` 只作提醒，不因此让 `check` 失败；发布前仍要把这些句子改成正面陈述。

## 状态路径

都相对工作区。不要写绝对路径，不要用上级目录，不要把这些路径做成符号链接。

当周运行目录是 `state/mark-weekly-news/runs/<saturday>/`。技能参考文件在 `.agents/skills/dalton-mark-weekly-newenergy/references/`。后文提到这些文件时，都从工作区根目录写完整路径。

- `state/mark-weekly-news/history.json`
- `state/mark-weekly-news/drafts/<saturday>.md`
- `state/mark-weekly-news/drafts/<saturday>.restyled.md`
- `state/mark-weekly-news/runs/<saturday>/manifest.json`
- `state/mark-weekly-news/runs/<saturday>/evidence/`
- `state/mark-weekly-news/runs/<saturday>/draft.md`
- `state/mark-weekly-news/runs/<saturday>/raw.json`
- `state/mark-weekly-news/runs/<saturday>/collect-state.json`
- `state/mark-weekly-news/runs/<saturday>/doc-original.json`
- `state/mark-weekly-news/runs/<saturday>/doc-restyled.json`
- `state/mark-weekly-news/runs/<saturday>/restyle/`

后面的命令都把 `<saturday>` 传给 `--end`。

## 完成门槛

`check` 返回 `ok` 为 true，且 `companies_found` 为 `35/35` 之前，不调用 `feishu_create_doc`，也不写最终回复。

只有本次运行已经超过 100 分钟，才把还没查完的公司写成未完成小节，然后继续发布：

```markdown
- **判断：NA，本周未查完。**
- NA（未查：原因）
```

摘要里用 `unfinished` 列出这些公司，第一行会写明没查完的名字。平时没有重大更新的公司写成：

```markdown
- **判断：NA。**
- NA（已查：AlphaEngine 研报/公告、内部库、web_search 中英文）
```

检索失败要写成检索失败，不要写成无更新。原版按 120 分钟安排。按 `.agents/skills/dalton-mark-weekly-newenergy/references/targets.json` 的公司分组顺序写作，每写完第 N 组，把整篇 `state/mark-weekly-news/runs/<saturday>/draft.md` 写回一次，并在过程说明里写 Batch N（第 N 组已完成）。

## 1. 窗口和自检

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json self-test --workspace-dir <workspace>
```

自检失败就按第 9 节把原因写入 `state/mark-weekly-news/runs/<saturday>/failure.json`，不继续采集。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json window --workspace-dir <workspace>
```

记下 `saturday`、`report_run_id`、`title`、`window`。`window` 里有 `date_from`、`date_to`、`web_date_after`、`web_date_before`、`calendar_until`。时区是 America/New_York（美东）。结束日默认是今天的美东日期。`web_date_after` 和 `web_date_before` 就是后面 `web_search` 的 `start_date` 和 `end_date`。

## 2. 计划

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json plan --workspace-dir <workspace> \
  --end <saturday>
```

`plan` 不写文件。它给出：

- `alphaengine`：35 家各两条，再加 11 条行业查询，共 81 条。公司 `limit` 为 8，行业 `limit` 为 10。卖方类目是 `sell_side_report`、`sell_side_comment`、`meeting_minutes`。新闻类目是 `news`、`announcement`。行业查询不传 `document_categories`。这些调用由下一步的 `collect` 执行。
- `internal`：`handled_by` 为 `collect`。脚本先列 `company-wiki`（企业维基，`limit` 100，`indexed_since` 用窗口的 `date_from`），再对 sales-notes、tmtb、funda-ai（内部邮件与笔记库）按公司检索，读正文时用 `route="all"`、`operation="read"`。
- `web_search` 与 `get_document`：在 `assemble` 之后执行，见第 4 节末。

`researchdata` 只能从 Python 调用。Bash 运行本技能脚本前会带上 `PYTHONPATH` 和 `PTC_WORKSPACE_CONFIG`，所以脚本里可以 `from tools import researchdata`。`web_search`、`feishu_create_doc`、`automation_report` 是直接工具。

## 3. 采集

`collect` 按 `plan` 调用 `research`，把 AlphaEngine 81 条、一条 company-wiki `list`、以及每家公司的内部库结果写入 `state/mark-weekly-news/runs/<saturday>/raw.json`。每写完一个键就原子替换这个文件。旁边的 `state/mark-weekly-news/runs/<saturday>/collect-state.json` 记录每个键已经尝试的次数。已经完成的键会跳过。失败或检索不完整的键记 1 次，累计满 2 次后进入 `exhausted_keys`，不再重试。默认预算 2400 秒。到点就停：还没发出的请求保持未完成，不记成失败，也不增加尝试次数。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json collect --workspace-dir <workspace> \
  --end <saturday>
```

返回里看 `remaining_keys`、`exhausted_keys` 和 `failed`。`remaining_keys` 是还没完成、并且尝试次数未满 2 次的键。`remaining_keys` 大于 0 时，再跑同一条 `collect`。整次运行最多跑 3 次 `collect`。满 3 次之后，无论 `remaining_keys`、`exhausted_keys` 和 `failed` 是多少，都继续 `prices`、`earnings`、`assemble`。失败的来源由 assemble 写进 `errors`，稿件「未核实与检索失败」照写。`exhausted_keys` 里的键不要再重试。不要自己补调 `research`，也不要手改 `state/mark-weekly-news/runs/<saturday>/raw.json` 或 `state/mark-weekly-news/runs/<saturday>/collect-state.json`。传输未配置时会立刻返回 `researchdata_transport_unconfigured`，这时不要按键重试。

内部库（company-wiki 企业维基，以及 sales-notes、tmtb、funda-ai 内部邮件与笔记库）由 `collect` 自动检索并截取摘录，`assemble` 会把摘录和失败信息写进证据与 `state/mark-weekly-news/runs/<saturday>/manifest.json` 的 `errors`。写稿时只用证据里的摘录，不要另编；凡是证据或 `errors` 里标了「内部库检索不完整：…」「company-wiki 新入库列表获取失败：…」或「内部库尚未完成检索（预算用尽或重试已满）」，都在稿件「七、未核实与检索失败」照写，不要写成命中 0 篇。

## 4. 行情、业绩日历和证据

行情和业绩日历由脚本在沙箱里直接读取。依赖已经装好的 `yfinance` 和 `pandas`。不要自己再装包。

`earnings` 命令会直接抓取巨潮预约披露日和 yfinance 业绩日历。查询失败时证据会标明失败，稿件不要编造日期。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json prices --workspace-dir <workspace> \
  --end <saturday>
```

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json earnings --workspace-dir <workspace> \
  --end <saturday>
```

记下 `big_moves_ge_8pct`（近一周涨跌幅绝对值达到或超过 8% 的公司）。行情下载整体失败时，`prices` 仍返回 `ok` 为 true，`error` 是异常类名，涨跌表全部为缺失，然后继续后面的命令。业绩日历某一家查询失败时，证据里写「yfinance 日历查询失败：类名」，其他公司照常。

然后整理证据：

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json assemble --workspace-dir <workspace> \
  --end <saturday> \
  --raw state/mark-weekly-news/runs/<saturday>/raw.json
```

读 `window`、`evidence_files`、`big_moves_ge_8pct`、`prev_run`、`errors`、`errors_summary`、`ae_queries`、`ae_failed`。`errors` 是全部失败，`errors_summary` 是前 30 条。`ae_failed` 超过三分之一时，`hint` 会要求改用 `web_search`，并在「未核实」写明「AlphaEngine 不可用：错误码」。不要把检索失败写成无更新。

`prev_run.draft` 是上周稿。上周已经写过的事件本周不重复。同一事件有新进展时只写新进展，并注明「上周已报」。`state/mark-weekly-news/runs/<saturday>/evidence/prev-earnings.md` 是上一期稿的「五、业绩日历」，业绩日历以它和证据里的业绩日历线索为底。

`assemble` 之后再做网页检索，并按需要读全文。

需要 AlphaEngine 全文时，整次运行最多 6 篇。在 ExecuteCode（Python 代码执行工具）里运行，把 `doc_id` 换成检索命中的编号。下面的 `doc_id` 仅为格式示例：

```python
from tools.researchdata import research

research(
    route="alphaengine",
    operation="get_document",
    params={
        "doc_id": "290001790954279_inst_134249",
        "content_field": "original_text",
        "offset": 0,
        "max_chars": 30000,
    },
)
```

优先读摘要。只在要写进稿件的数字或原话对不上摘要时才读全文。这 6 篇不代替 `collect` 已经做过的 `search_library`。

网页检索在对话里调用。工具名是 `web_search`。只传 `query`、`start_date`、`end_date`。不要传 `count`、`country`、`language`、`domain_filter`、`freshness`、`date_after`、`date_before`、`time_range`、`topic`。日期用 `plan` 里该条的 `start_date` 和 `end_date`。样例窗口是 2026-10-31 至 2026-11-08：

```python
web_search(
    query="宁德时代",
    start_date="2026-10-31",
    end_date="2026-11-08",
)
```

`plan` 里每家的 `query`：`ae_q` 不含汉字时用 `ae_q`，例如松下用 `Panasonic Energy`、三星SDI 用 `Samsung SDI`；`ae_q` 含汉字时用中文名，例如宁德时代。行业查询用 `plan` 里的 `q`，中文和英文都要覆盖。对 `big_moves_ge_8pct` 里的公司，以及未来 7 天有业绩日期的公司，再各做一次同样的 `web_search`。结果直接用于写稿，不要写入 `state/mark-weekly-news/runs/<saturday>/raw.json`。

## 5. 写稿

稿件写到 `state/mark-weekly-news/runs/<saturday>/draft.md`。先读证据，再按下面的结构写。行业动态先读 `state/mark-weekly-news/runs/<saturday>/evidence/industry.md`，六个小节都要有。公司按 `.agents/skills/dalton-mark-weekly-newenergy/references/targets.json` 的分组顺序，每组先读 `state/mark-weekly-news/runs/<saturday>/evidence/<分组 id>.md`（文件名用该分组的 `id`，例如 `battery.md`）。公司标题含名单里的公司名，或含该公司关键词的前两个。

本周发业绩的公司要写收入、利润及同比，对比一致预期，管理层指引，以及盘后或次日的股价反应。`big_moves_ge_8pct` 里的公司要写股价原因。找不到公司事件时，写「未找到对应公司事件」，并给出同期板块或指数走势。

业绩日历以证据里的「业绩日历线索」为底，并对照 `state/mark-weekly-news/runs/<saturday>/evidence/prev-earnings.md`（上一期稿的「五、业绩日历」）。分「本周已发布」「未来两周」（到 `calendar_until`）和「只发中期和年度业绩」三个小节。未来 7 天内的日期必须用公告或公司 IR（投资者关系）页核实。每条标来源等级：【公告】是交易所公告或预约系统，【官网】是公司 IR 页，【第三方日历】是仅有第三方日历来源。地平线、华润万象生活、滨江服务、华润置地、建发国际只发中期和年度业绩，归入「### 只发中期和年度业绩」小节。

第六节只放 `state/mark-weekly-news/runs/<saturday>/evidence/prices.md` 的表，这是全文唯一的表格。表原样贴入，不要改数字。

```markdown
# 新能源与中概股周度动态（M/D至M/D）
窗口与口径说明一段：事件窗口、股价区间、doc 指 AlphaEngine 文档号、来源等级的含义。

## 一、本周结论
3 至 5 条，每条先写判断再写最关键的数字。

## 二、行业动态
### 中国锂电与储能
### 海外锂电与储能
### 中国新能源车
### 海外新能源车
### 燃油车与汽车出口
### AI 电力链

## 三、新能源公司
### 宁德时代（300750.SZ / 3750.HK）近一周 +9.1%
- **判断：偏多、偏空或中性，一句话说清本周最重要的变化。**
- [10/20] 事件：发生了什么，关键数字带期间和单位，来源（机构或媒体、日期、doc 号或链接）
- 卖方：机构、日期、评级、目标价及调整、核心理由
- 看点：日期和事件

## 四、中概股公司

## 五、业绩日历
### 本周已发布
### 未来两周
### 只发中期和年度业绩

## 六、近一周涨跌幅

## 七、未核实与检索失败
```

行业小节先写一行判断，再列 2 至 6 条带日期的事件。覆盖：中国锂电与储能（排产、装车、储能招中标、碳酸锂及材料价格、固态电池、钠电池、政策）；海外锂电与储能（韩日电池厂、欧美储能项目与政策、数据中心配储）；中国新能源车（乘联会周度或月度、出口、价格、政策）；海外新能源车（欧洲注册量、美国政策与关税）；燃油车与汽车出口；AI 电力链（燃机、燃料电池、核电、数据中心电力订单）。

什么算重大：业绩及预告、指引变化、大额订单或合同、投产扩产关停、新产品、月度交付或销量、并购融资配售回购增减持、管理层变动、监管诉讼处罚、直接相关的政策、主要券商评级与目标价调整、股价单日大幅波动及原因、重要合作。软文、没有数字的宣传稿、纯股价复盘不写入正文。

写作时先给判断，再写依据。证据有限时写明当前倾向、置信度和改判条件。每个数字带期间和单位。每条一个来源。只有一个出处的数字标「单一来源」。卖方、管理层、专家分开标注。检索摘要里的数字要能对上引用原文，对不上就丢掉或标「未核实」。非中文信息用原始来源，不用百度百科。金额写「美元」或「亿元」。区间写「至」。不用美元符号，不用波浪号。正文不写研究过程和工具日志。

## 6. 校验和原版文档

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json check --workspace-dir <workspace> \
  --end <saturday>
```

按 `missing_h2`、`missing_companies`、`sections_without_judgement_or_events`、`dollar_signs`、`tildes` 和 `chars`（全文不少于 6000 字）改到 `ok` 为 true。`style_warnings` 里的句子改成正面陈述后再发布。逐字引语可以保留。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json doc-status --workspace-dir <workspace> \
  --end <saturday> \
  --kind original
```

`create` 为 false 时不要调用 `feishu_create_doc`。`reason` 为 `doc_record_unreadable` 表示记录文件在但 JSON 损坏，不要再建。`reason` 为 `create_failed_twice` 表示同一类文档已经失败两次。`create` 为 true 时调用一次。`title` 用窗口命令的 `title`，月和日不补零。`markdown` 用 `state/mark-weekly-news/runs/<saturday>/draft.md` 全文。样例：

```python
feishu_create_doc(
    title="新能源与中概股周度动态（10/31至11/7）",
    markdown="# 新能源与中概股周度动态（10/31至11/7）\n\n## 一、本周结论\n本周样例。\n",
)
```

把返回的 JSON 原样写到 `state/mark-weekly-news/runs/<saturday>/feishu-return.json`。调用没有返回 JSON 时，写入 `{"ok": false, "error": "call_failed"}`。成功和失败都保存：

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json save-doc --workspace-dir <workspace> \
  --end <saturday> \
  --kind original \
  --file state/mark-weekly-news/runs/<saturday>/feishu-return.json
```

保存时先把工具返回原样落盘，再归档草稿、更新 `history.json`。归档失败记 `archive_error`，历史失败记 `history_error`，已经落盘的记录仍然算数。

- 返回成功，且有 `document_id` 或 `node_token`：写入 `state/mark-weekly-news/runs/<saturday>/doc-original.json`。之后 `create` 为 false。
- 返回失败但带有可用的 `document_id` 或 `node_token`（能拼出飞书链接）：文档已建、正文未写完，同样写入 `state/mark-weekly-news/runs/<saturday>/doc-original.json`，`create` 为 false（后续改写模式跑 `summary` 时，脚本会自动标「正文未完成」且不展示该链接）。
- 返回失败且没有可用 id：写入 `state/mark-weekly-news/runs/<saturday>/doc-original.failed.json` 并累计次数，不挡住下一次 `create`。同一类失败满 2 次后 `create` 为 false，`reason` 为 `create_failed_twice`。
- 正式记录文件在，但 JSON 损坏：`create` 为 false，`reason` 为 `doc_record_unreadable`。不要覆盖，也不要再建。

`state/mark-weekly-news/runs/<saturday>/doc-original.json` 已存在时，命令不会覆盖它，也不要换标题再建一篇。链接由 `document_id` 或 `node_token` 拼出。有 `node_token` 时用 `https://feishu.cn/wiki/<node_token>`，否则用 `https://feishu.cn/docx/<document_id>`。

建好后，`feishu_create_doc` 会自动把文档分享给调用者和已配置的团队跟踪群，不要再跑单独的授权命令。改写模式若在 `state/mark-weekly-news/runs/<saturday>/doc-original.json` 或 `state/mark-weekly-news/runs/<saturday>/doc-restyled.json` 里看到 `warning`，把该 `warning` 填入 `state/mark-weekly-news/runs/<saturday>/summary-input.json` 的 `unverified`（未核实条目）。

原版到这里结束。不提交 `automation_report`。最终回复只写 `NO_REPLY`。

原版任一步失败时，按第 9 节运行 `note-failure`，最终回复只写 `NO_REPLY`。`window` 命令本身失败时，`<saturday>` 用当天的美东公历日，格式 `YYYY-MM-DD`。改写模式在 `restyle` 为 false 时提交含失败原因的 `text`。`failure_text`（一行失败说明）只用于第 9 节写明的情形。

## 7. 改写

改写模式先看原版是否已经完成。不要单独读 `state/mark-weekly-news/runs/<saturday>/failure.json` 来决定是否改写。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json restyle-segment --workspace-dir <workspace> \
  --end <saturday>
```

返回 `restyle` 为 false 时，不要改写，不要建改写版文档。读 `reason`：原版草稿还没有写好、原版校验还没有通过，写入 `state/mark-weekly-news/runs/<saturday>/summary-input.json` 的 `restyle_failure`；原版飞书文档还没有记录、`doc_record_unreadable`，以及建文档失败（如 `create_failed_twice`，或 `failure.json` 里记下的同类原因），写入 `publish_failure`。然后直接做第 8 步。`restyle-segment` 在原版 `check` 未通过时，返回的是「原版草稿还没有写好」或「原版校验还没有通过」，不会改读 `failure.json`。这时 `summary` 仍会把「改写版未发布」写成「原版未完成」。原版 `check` 已通过、但没有成功的改写版文档时，「改写版未发布」使用 `restyle_failure`；该字段为空时，脚本改用整篇校验原因，或「还没有改写版文档」。非空的 `publish_failure` 会输出「飞书发布失败：…」，并附上草稿路径。

返回 `restyle` 为 true 时，读 `.agents/skills/dalton-mark-weekly-newenergy/references/restyle_style.md` 和 `state/mark-weekly-news/runs/<saturday>/restyle/chunks.json`。`verbatim` 为 true 的段原样保留，不要改写。其余每一段按该文件改写，输出放在 `===BEGIN===` 和 `===END===` 之间。改写要求以该文件为准。改写出的各段正文和后续填写的摘要，都必须同时遵守文末「撰写硬规则」（不要修改 `.agents/skills/dalton-mark-weekly-newenergy/references/restyle_style.md` 文件）。

把各段改写结果写入 `state/mark-weekly-news/runs/<saturday>/restyle/rewritten.json`：

```json
{"chunks": [{"index": 0, "text": "===BEGIN===\n改写正文\n===END==="}]}
```

`index` 用 `chunks.json` 里的序号。然后：

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json restyle-apply --workspace-dir <workspace> \
  --end <saturday> \
  --rewritten state/mark-weekly-news/runs/<saturday>/restyle/rewritten.json
```

脚本核对标题、条目数、带正负号的数字（`+10%` 与 `-10%` 不同，减号 U+2212 和全角减号 U+FF0D 都与 `-` 相同）、公司名出现集合、`[M/D]` 日期标记集合、doc 号、链接占位符和【】标签。改写后多出来或少掉的公司名、日期标记，这一段失败并保留原文。序号在 `chunks_kept_original`，从 0 起算。第六节行情表始终用原文。

`restyle-apply` 还会做整篇校验：没有残留的 `LINK_` 占位符、标题行与原文一致、链接与原文一致、第六节「近一周涨跌幅」与原稿逐字一致（只去掉整段首尾空白）。任一不过，返回 `ok` 为 false，`reason` 为「整体校验未过，不要发布」。把这个原因写进摘要的 `restyle_failure`。

读 `state/mark-weekly-news/runs/<saturday>/restyle/flags.json`。只在改写把意思改偏时改回原意，例如加强或减弱了判断、丢掉「待核实」或「单一来源」、改了比较对象。意思没变的句子不要再改。用编辑工具改 `state/mark-weekly-news/runs/<saturday>/restyle/draft.md` 里的那一句。改完再跑下面的 `check`，不要重新运行 `restyle-apply`，否则会覆盖手工改回的句子。`check --which restyled` 会再跑一遍公司校验、整篇校验，以及按段落对齐原稿的同一组事实校验（带正负号的数字、公司名出现集合、`[M/D]` 日期标记）。段数对不上就整篇失败，原因是「整体校验未过，不要发布」。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json check --workspace-dir <workspace> \
  --end <saturday> \
  --which restyled
```

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json doc-status --workspace-dir <workspace> \
  --end <saturday> \
  --kind restyled
```

`create` 为 false 时不要发布。整篇校验未过时，`reason` 是「整体校验未过，不要发布」。记录损坏或同一类失败满两次时，原因分别是 `doc_record_unreadable`、`create_failed_twice`。`create` 为 true 时调用一次 `feishu_create_doc`。标题用 `新能源与中概股周度动态（10/31至11/7，改写版）` 这种格式，月和日不补零，改写版三个字在括号里面。markdown 用 `state/mark-weekly-news/runs/<saturday>/restyle/draft.md` 全文。返回原样写入 `state/mark-weekly-news/runs/<saturday>/feishu-return-restyled.json`，失败也写。

```python
feishu_create_doc(
    title="新能源与中概股周度动态（10/31至11/7，改写版）",
    markdown="# 新能源与中概股周度动态（10/31至11/7，改写版）\n\n## 一、本周结论\n本周样例。\n",
)
```

改写版的保存规则与原版相同：先落盘，再归档和写历史。失败但带有可用的 `document_id` 或 `node_token` 也算已建，`create` 为 false（`summary` 脚本会自动标「正文未完成」且不放入推荐链接）。没有 id 的失败累计在 `state/mark-weekly-news/runs/<saturday>/doc-restyled.failed.json`。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json save-doc --workspace-dir <workspace> \
  --end <saturday> \
  --kind restyled \
  --file state/mark-weekly-news/runs/<saturday>/feishu-return-restyled.json
```

`state/mark-weekly-news/runs/<saturday>/doc-restyled.json` 已存在就不要再建。改写版发布失败不影响原版。摘要里如实写明。

## 8. 摘要

写 `state/mark-weekly-news/runs/<saturday>/summary-input.json`。要点、下周业绩和未核实由你填写。覆盖家数和 AlphaEngine 次数由脚本从 `check` 和 `state/mark-weekly-news/runs/<saturday>/manifest.json` 读取，不要在输入里另填。

```json
{
  "highlights": ["一句判断加最关键的数字"],
  "next_earnings": ["11/16 宁德时代"],
  "unverified": ["一条未核实"],
  "unfinished": [],
  "publish_failure": "",
  "restyle_failure": ""
}
```

`highlights` 写 3 至 5 条。`next_earnings` 按日期写。`unverified` 最多 5 条。没有发布失败时，把 `publish_failure` 和 `restyle_failure` 设成空字符串。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json summary --workspace-dir <workspace> \
  --end <saturday> \
  --input state/mark-weekly-news/runs/<saturday>/summary-input.json
```

`summary` 返回的 `text` 就是待提交的纯文本摘要（脚本在对应情况下会自动拼入尖括号链接、失败原因、草稿路径、保留原文的段号以及「整体校验未过，不要发布」）。`summary-input.json` 各字段不要用表格，也不要手改 `summary` 生成的 `text`。

`chars` 超过 1500 时 `ok` 为 false，`over_limit` 为 true。先压缩 `highlights`、`next_earnings`、`unverified`，再跑一次 `summary`。只有 `ok` 为 true 才提交 `text`。

提交时 `report_run_id` 用窗口命令返回的值，样例是 `mark-weekly-newenergy-2026-11-07`。`text` 用摘要命令返回的 `text`。

```python
automation_report(
    action="submit",
    report_run_id="mark-weekly-newenergy-2026-11-07",
    material="yes",
    text="新能源与中概股周度动态（10/31至11/7）\n改写版（推荐读这份）：未发布\n",
)
automation_report(
    action="status",
    report_run_id="mark-weekly-newenergy-2026-11-07",
)
```

只有返回里的 `report_run_id` 与提交时的相同，`content_kind` 为 `text`，并且 `ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才算发送成功。

发送成功后，最终回复只写 `NO_REPLY`。

摘要已经保存（提交返回 `ok` 为 true）而发送还未确认时，只用同一个 `report_run_id` 和同一份 `text` 再提交一次。仍然未确认就停止：最终回复只写 `NO_REPLY`，不要提交 `failure_text`。

`content_conflict` 表示这个 `report_run_id` 已经保存了不同正文。不要改摘要，不要换第二个标识，不要提交 `failure_text`。最终回复说明已保存的正文和这次摘要不一致，不附摘要正文。`code` 为 `not_automation_run` 时停止：最终回复说明本次不是自动化运行，不附摘要正文。

提交返回 `ok` 为 false，且 `code` 不是 `content_conflict` 或 `not_automation_run`，正文还没保存时，提交一行失败说明：

```python
automation_report(
    action="submit",
    report_run_id="mark-weekly-newenergy-2026-11-07",
    material="yes",
    failure_text="新能源与中概股周度动态失败：原版校验还没有通过",
)
```

失败文本确认发出后，最终回复只写 `NO_REPLY`。工具没能保存这行时，最终回复只留这一行。不要发第二份正文。

## 9. 失败记录

原版任一步失败时，运行 `note-failure`，把一句具体原因写入 `state/mark-weekly-news/runs/<saturday>/failure.json`（若 `window` 命令本身失败，`<saturday>` 用当天美东公历日 `YYYY-MM-DD`），不提交 `automation_report`，最终回复只写 `NO_REPLY`。

改写模式由 `restyle-segment` 判断能否改写：先看原版 `check`，再看 `state/mark-weekly-news/runs/<saturday>/doc-original.json`。记录里 `ok` 为 true 且能拼出链接时，忽略旧的 `failure.json`，仍按原版定稿改写。返回 `restyle` 为 false 时不改写，按第 8 步运行 `summary` 并提交 `text`（含失败原因的摘要）。只有 `window` 或 `summary` 命令本身失败，或 `automation_report` 提交 `text` 返回 `ok` 为 false（且 `code` 不是 `content_conflict`、`not_automation_run`）且正文尚未保存时，才用 `failure_text` 提交一行失败说明。`window` 失败时不要跑 `summary`，只提交 `failure_text`。正文已保存但发送未确认时，只用同一个 `report_run_id` 和同一份 `text` 再提交一次；仍未确认就停止，最终回复只写 `NO_REPLY`，不要提交 `failure_text`。`content_conflict` 时，最终回复说明已保存的正文与本次摘要不一致，不附摘要正文。

```bash
python3 .agents/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py \
  --json note-failure --workspace-dir <workspace> \
  --end <saturday> \
  --message <reason>
```

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
