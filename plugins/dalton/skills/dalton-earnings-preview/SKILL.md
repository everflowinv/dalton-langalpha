---
name: dalton-earnings-preview
description: "产出财报前瞻报告与可对账的结构化预测清单：厘清五类预期门槛（含买方 bogey）与高频数据、EPS 质量与指引隐含门槛、核心分歧及三情景反应框架。公司临近发榜、用户想明确预期门槛与看点时使用；需多源检索，几分钟内可完成。"
---

# Dalton 财报前瞻（`dalton-earnings-preview`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

---

## 目标

在公司财报发布前三个交易日（由 PS.7 的 `tm3` 自动任务触发，或由分析师手动触发），依次回答三个核心问题：
1. **投资者真正的预期门槛（bar / bogey）是多少，高频数据指向什么方向？**
2. **本次财报对当季业绩与下季/全年指引看什么，EPS 质量与管理层指引隐含门槛在哪？**
3. **当前多空核心分歧（debate）共享哪些前提与因果机制，本次财报能验证什么？**

并将本次要验证的判断用 `scripts/predictions.py` 写成结构化 `predictions.json`，供财报发布后快讯（PS.9 `dalton-earnings-flash`）与次日深度链（PS.9 `dalton-earnings-deep`）逐条对账。

---

## 8 步执行流程

### Step 1：锁定标的与时点（冻结时钟 Freeze the Clock）

1. **确认基础身份**：公司名称、股票代码（ticker）、业务别名、目标财季（严格区分 `FY` 与 `CY`，沿用公司自身财季命名如 `FY26Q3`）。
2. **冻结研究截止时间（`as-of`）**：
   - 实时前瞻：取自动任务触发时刻（或当前会话开始时刻）为唯一 `as-of`；
   - 历史回测前瞻：严格以指定的历史截止时点（如财报发布前 3 天）为 `as-of`，**所有检索必须显式带上截止日期（`end` / `to_date` / `end_date`），截止时间之后的信息一律禁止使用**。
3. **确认发布日期与窗口**：
   - 从 `../Home/dalton/earnings-calendar.json` 或 `agenda.watch.events` 读取官方发布日期、BMO/AMC 及官方来源链接；未经官方确认的写“未确认”，绝不自行编造。
   - **检索时间窗**：预期材料取近 **30 天**（材料不足时可扩展至上一次财报后）；核心 debate 取近 **90 天**。
4. **基线快照**：
   - 读取工作区 `thesis.md`、`debate.md`、`drivers.md`、`management.md`；
   - 取公司上一季财报给出的最新官方指引作为比较基线；
   - 调用 `researchdata.finance(route="estimates", ticker=T)` 获取一致预期快照，并按 P0-D.6 规范落盘到 `data/consensus/<YYYY-MM-DD_HHMM>.json`。

### Step 2：优先级 1 —— Buyside Bogey 与高频数字 Color（投入最大检索精力）

按 [`references/retrieval.md`](references/retrieval.md) 必须分别检索内部研究库四个入口：**AlphaEngine、sales-notes、TMTB、FUNDA AI**（每个入口按公司名和 ticker 分别查）：

- **检索关键词**：`bogey`、`whisper`、`hurdle`、`investor expectations`、`into the print`、`买方预期`、`业绩门槛`、`渠道跟踪`，以及与该公司业务直接相关的高频指标（`sell-through`、`bookings`、`shipments`、`traffic`、`价格`、`排产`）。
- **真实门槛五分（严禁混同）**：
  1. **公司指引（Company Guide）**：管理层生效中的官方指引区间、发布日期与场合；
  2. **卖方一致预期（Street Consensus）**：均值、分析师人数与快照日期（`street_estimate`）；
  3. **具名机构预测（Named Broker Forecast）**：单家券商分析师的具体预测与日期；
  4. **明确的买方门槛（Buyside Bogey / Whisper）**：**只有来源明确引用投资者调研或买方预期时才可标为 buyside bogey**；sales 本人的预测不是买方共识；sales 转述必须写“某机构 sales 转述买方预期”；“大家都想看 beat”只作定性描述，不补造数字；若四处均未查到有来源的买方数字门槛，必须明确写“**本轮未找到有来源的买方数字门槛**”；
  5. **本稿判断（Our Expectation / House View）**：基于驱动树与高频数据给出的本稿预期，说明达到一致预期是否足够、什么构成预期差。
- **数字与高频数据记录规范**：
  - 每个数字必须记录：`指标、期间、币种/单位、GAAP/non-GAAP 或业务口径、原始日期、说话人、原文短摘`。
  - 每个高频数据必须记录：`最新值、较上次变化、样本覆盖、传导到收入还是利润、为什么对这次财报重要`。
  - **禁止不当对比**：不直接比较不同天数的 MTD 窗口、毛额（Gross）与净额（Net）、累计（YTD）与单季（Q）。数字冲突时先对齐期间与口径；确属不同观点的真分歧并排列出，**绝不取简单平均充当“市场共识”**。

### Step 3：优先级 2 —— 业绩看法与关注点

复用已读材料并补查**单公司前瞻、行业/板块前瞻（不要求标题含公司名，进正文/表格/附图查公司与别名）、sales 邮件、TMTB、FUNDA AI**：

- **拆分短期与中期门槛**：严格区分“本季业绩 beat 不 beat”与“下季/全年指引及长期叙事的门槛”。
- **区分市场焦点与本稿重点**：分别写清“市场最关注什么”和“本稿认为最决定预期差的变量是什么”，不把本稿判断伪装成市场共识。
- **提问口径隔离**：电话会或研报中分析师提问里的数字，未经管理层确认的必须标注“分析师提问口径，管理层未确认”，严禁当成管理层指引。
- **同业/上下游先发财报传导（Read-through）**：若同业、客户或供应商近期已发财报，仅在能指明具体业务传导机制时列入，注明传导路径与置信度。

### Step 4：EPS 质量与指引隐含门槛桥（Guidance Bridge）

1. **EPS 质量观察表**：排查一次性项目、有效税率、稀释股本（回购节奏、可转债稀释）、利息与投资公允价值变动、外汇折算、重组/诉讼费用等会扭曲表面 EPS 的项目，输出五列表：
   `| 项目 | 为什么扭曲 | 财报前风险 | 财报后怎么判断 | 影响模型哪一行 |`
   并明确说明卖方一致预期口径是否与公司实际核心经营口径一致。
2. **指引隐含门槛（Guidance Bridge）**：
   - 调用 `get_earnings_history`（或 `estimates` 快照中的近 8 期记录）统计过去 8 个季度收入与核心利润指标相对于自身指引及一致预期的 beat 频率与中位幅度；
   - 读取工作区 `management.md` 中关于管理层指引保守程度的历史记录，判断保守程度是稳定、扩大还是收窄；
   - 推导出本次“**指引隐含的真实门槛**”（注明样本季度数及管理层/指引政策是否发生过变化）。

### Step 5：优先级 3 —— 核心 Debate 与 Narrative 共性 + 一轮 X

1. **先合并内部观点，再补一轮公开 X**：
   - 先将内部四入口中的同义观点去重归并，再按 [`references/retrieval.md`](references/retrieval.md) 调用 `x_api.search_posts` 做一轮公开主题检索（时间窗 90 天且截至 `as-of`；严禁把内部邮件或私有 bogey 数字放进公开查询）。
2. **按因果机制聚类（禁止按表面关键词合并）**：
   - 沿着“**共同前提 → 因果机制 → 关键变量 → 对盈利/估值的含义**”聚类。例如两位作者都谈订单，一个讲需求加速、一个讲供给受限，必须分成两条机制，不能因为都出现“订单”而合并。
3. **独立性与来源链去重**：
   - 称为“共享 narrative”至少需要 **≥2 个独立原始来源**支持；同一份卖方报告被邮件、TMTB、FUNDA AI、X 转述只算 **1 条来源链**；
   - 仅单一来源支持的标“**单源观点**”；点赞量、转发量不证明机构共识。
4. **可观察验证点与电话会必听问题**：
   - 每个核心 debate 必须绑定一个**本次财报可观察的验证点**（及管理层电话会最需关注的 3–4 个核心问题：为什么重要、什么回答证实、什么回答证伪、需要警惕的回避措辞）；
   - 只能长期验证、本季财报无法证伪的条件单独列出；
   - 对照工作区 `debate.md`，写明本轮分歧与知识层现有 debate 的对应关系。

### Step 6：情景与反应框架

1. **Bull / Base / Bear 三情景表**：以“真实预期门槛（bar）”为基准（而非去年同期），列出三情景下的：`收入｜EPS/核心利润｜关键驱动｜管理层会怎么说（Management tell）｜证伪点｜可能的股价反应`，并指出哪类弱业绩可买（weak print buyable）、哪类强业绩易冲高回落（strong print fadeable）。
2. **历史财报反应**：列出过去 4–8 次财报相对于门槛的结果、次日涨跌幅及驱动原因。
3. **期权隐含波动（Options Implied Move）**：
   - 调用 `get_options_chain`（筛选跨过财报日的最近到期日与平值附近行权价，分别取 `call` 和 `put`）与 `get_options_snapshot`；
   - 计算平值跨式组合中价除以现价（`straddle mid / spot`），必须写明**到期日与剩余期限**；
   - 若最近期权到期日远晚于财报日，必须重标为“到期期限波动率背景（expiry-tenor volatility context）”，说明其包含非财报时间价值。

### Step 7：生成结构化预测（`predictions.json`）

调用 `scripts/predictions.py` 生成并校验 `research/<D>-preview-<季度>/predictions.json`：

```bash
python3 .agents/skills/dalton-earnings-preview/scripts/predictions.py write \
  --file research/<D>-preview-<季度>/predictions.json \
  --input <准备好的预测 JSON 文件或通过 --stdin 传入>

python3 .agents/skills/dalton-earnings-preview/scripts/predictions.py validate \
  --file research/<D>-preview-<季度>/predictions.json --strict
```

- **11 个必填字段**：`id, metric, period, basis, our_expectation, street, bogey, why, verify_how, linked_debate, linked_pillar`。
- **最低覆盖硬门槛**：
  1. 营业收入（Revenue）；
  2. EPS（或公司最核心的利润指标，如 Adjusted Operating Income / EBITDA）；
  3. 下季或全年指引（Guidance）；
  4. 2–4 个关键业务运营 KPI；
  5. 第三节中的每个核心 debate 至少对应一条可验证预测。

### Step 8：成稿、自检与收尾三步

1. **成稿落盘**：按 [`templates/preview.md`](templates/preview.md) 将中文前瞻写到 `drafts/<D>-preview-<季度>.md`。开头不超过 3 句话直接给核心判断（真实门槛、关键预期差、最需验证的判断）；每个数字首次出现带期间、单位/口径与来源身份（`（机构/身份 [引用N]）`）。
2. **第 1 轮语言审查**：成稿后先调用 `check_language(draft_path="drafts/<D>-preview-<季度>.md", round=1)`（只标问题），读 `drafts/<D>-preview-<季度>.md.language.md` 清单并逐条修改草稿（只改语言表达，不动事实、数字、术语、引用编号与判断强度）。工具报错不算通过：重试或如实汇报未完成，不得跳过。
3. **交付前自检清单**：
   - [ ] AlphaEngine、sales-notes、TMTB、FUNDA AI 四个内部入口及行业前瞻均已按公司名和 ticker 检索；
   - [ ] 关键引用均已读过正文全文（未仅凭搜索摘要引用）；
   - [ ] 图片表格中的数字已回原图核对正负号、小数点、单位和期间（`description_verified=false` 的自动识图描述未直接用作数字证据）；
   - [ ] 真实门槛五分清晰，buyside bogey 有明确来源或写明“本轮未找到有来源的买方数字门槛”；
   - [ ] 信息身份、期间、口径完整保留，同源转述已去重不重复计票；
   - [ ] 历史回测模式下确认零未来信息泄漏（所有来源日期 `<= as_of`）；
   - [ ] “本轮未取得”与“通路/工具失败”分开写明；
   - [ ] `predictions.py validate --strict` 通过，正文第五节“核对清单”与 `predictions.json` 一一对应；
   - [ ] 末尾必须附引用表（在报告末尾 `## 引用` 一节按每行一条写出 `[引用1]: evidence:E-20261023-01` 或 `[引用3]: model:model/META-model-v01.xlsx!DCF!C12` 等核验来源对应关系）。
4. **第 2 轮语言审查（终稿）**：自检与内容修订全部完成后、调用 `check_draft` 之前，调用 `check_language(round=2)` 并按清单完成终稿语言修改；草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核（记录追加在同一 `<草稿>.language.json`），使最后一条第 2 轮记录绑定终稿哈希。本次产出中其他给人读的 .md 笔记（research/ 检索与对账笔记等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
5. **知识层写入与 `dalton-agenda` 收尾三步**：
   - 调用 `knowledge_update(doc="drivers", ...)` 追加本轮取得的高频指标最新读数；
   - 调用 `knowledge_update(doc="debate", ...)` 标注本次财报可检验的 debate；
   - 调用 `knowledge_append_verdict` 记录事件五词结论（`trigger.kind="earnings"`，`trigger.ref` 填 `<dalton_wake>` 的 `tm3` 信号 id 或 `"manual"`；通常为 `NO_CHANGE`，若前瞻研究已改变判断则如实填写），并在同次调用的 `agenda_ops` 中加入“财报发布当天对照 `research/<D>-preview-<季度>/predictions.json` 逐条核对”的验证项；
   - 调用 `check_draft(draft_path="drafts/<D>-preview-<季度>.md")` 生成数字核验反馈；PM 要求正式发布时调用 `publish_report(draft_path="drafts/<D>-preview-<季度>.md", title="<公司名>（<TICKER>）<季度> 财报前瞻", kind="earnings_preview", slug="<ticker-lower>-preview-<季度小写>-<D>")`；
   - **推送稿两轮语言审查与原样回复**：按 `.agents/skills/dalton-conventions/SKILL.md` §8 将推送正文写入 `research/push/<D>-earnings-preview-<季度>.md`，完成两轮 `check_language` 后以该文件全文逐字作为最后回复（外部通知由 Dalton 通知模块按用户的飞书开关统一处理）。

### 收尾门禁：汇报完成前必须过完成契约（`dalton_check_done`）

- 第 5 项全部做完后、向 PM 汇报“完成”前，必须调用 `dalton_check_done(workflow="dalton-earnings-preview", params={"ticker": ..., "quarter": ..., "as_of": "<本稿 D>"})`（`<D>` 统一用财报发布的美东业务日，与草稿文件名一致）。
- `done=false` 时按 `missing` 先补齐再查（缺草稿或 `predictions.json` 就补写、校验未过就按输出修到 `predictions.py validate --strict` 通过、`drivers`/`debate` 缺本轮 source=tool 新版本就 `knowledge_update`、verdict 未写或 `trigger.kind` 不是 `earnings` 就 `knowledge_append_verdict`、check_draft 用了旧草稿就对最终稿重跑 `check_draft`、语言审查缺轮次或第 2 轮哈希对不上就补跑对应轮次的 `check_language`）；补不了的缺项只能汇报“未完成”并列出缺项与原因，不得汇报“完成”。
