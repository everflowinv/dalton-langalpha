---
name: dalton-bullbear
description: "梳理个股市场共识、2–3 个核心多空分歧、盈利预测修正轨迹与隐含费用空间、近期催化剂及改判条件。用户想弄清市场对某只股票在争什么、预期差在哪时使用；对话快速版几分钟可完成，若要求完整 v4.2 长稿则启动多阶段深度工作流。"
---

# Dalton 个股多空讨论（`dalton-bullbear`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 提供两种交付形态，共用同一套买方分析链：**市场共识（price in 了什么）→ 真正的分歧（Top Debates）→ 盈利预测修正地图（含隐含费用空间与敏感度）→ 催化剂与 KPI → 改判条件**：

1. **对话快速版（默认路径）**：PM 问“X 的多空在争什么 / earnings revision / 近期催化剂”且未要求完整文档，或输入 `/bullbear {TICKER}` 时，由主智能体直接在对话中完成取证与判断，不派发 workflow、不跑财务体检和股东回报支线。
2. **v4.2 完整成稿（`bullbear-doc` workflow）**：PM 要求正式多空长稿、或按研究议程需要产出完整 v4.2 文档时，启动 `bullbear-doc` workflow（6 个子任务阶段：准备 → 股东回报支线 → 主稿 → 合并 → 独立审稿 → 修订）。

---

## 一、对话快速版流程（默认，主智能体直接执行）

> **速度原则**：快速版的价值在速度与判断锐度，与 `bullbear-doc` 完整成稿明确拉开详略——正文控制在约 30 行内，每段只给判断与关键数字，论证细节留给完整成稿。不要在快速版里跑 `fincheck.py` 和股东回报支线。

1. **读基金方法论与公司知识层**
   - 读 `.agents/skills/dalton-conventions/SKILL.md`。
   - 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序读取当前工作区的 `agent.md`、`thesis.md`、`debate.md`、`drivers.md`。
   - 若已有历史 `debate.md`，本轮以**增量更新与对比**为主：重点核查哪些 debate 的倾向或置信度发生了变化、原因是什么（新证据 vs 已 price in），禁止忽略旧结论直接覆盖。
   - **首次覆盖时执行叙事收敛（全列 → 删减 → 下挖底层驱动）**：初次覆盖一家公司、知识层尚无 `debate`（分歧档案）时，先把市场上流传的叙事**全部列出**（不加筛选，含互相矛盾的说法）；再**删减到 2–3 个可研究、真正决定价值的驱动变量（driver）**，逐条写明删除理由；每个驱动变量再**往下挖一层底层驱动**，写明观察指标与数据来源。收敛结果作为本轮"主要分歧"段的输入，并写入知识层 `debate` 与 `drivers`（驱动变量档案）初稿；后续季度重估只做增量更新，不再重跑全量收敛。

2. **一致预期与隐含费用空间**
   - 在 PTC（`execute_code`）中先调一次 `researchdata.catalog()`，再调 `researchdata.finance(route="estimates", ticker=T)`，获取本财年（`0y`）与下一财年（`+1y`）EPS、收入一致预期、7/30/90 天 EPS 修正轨迹、近 30 天上调/下调次数，以及近 4 次财报次日涨跌。
   - 每次取数将原始 JSON（附 `_meta{route, fetched_at, source}`）存盘到 `data/consensus/<YYYY-MM-DD_HHMM>.json`。
   - 运行隐含费用测算脚本（或调用同名函数）：
     ```bash
     python .agents/skills/dalton-bullbear/scripts/implied_model.py --estimates-json data/consensus/<latest>.json
     ```
     若最近财年存在一次性税项，传入 `--tax <公司指引有效税率>`，得出一致预期隐含税前利润、隐含总费用空间（`implied_total_expenses_bn`）、每 100 亿美元费用的 EPS 敏感度（`eps_per_10bn_expense`）及每 1% 收入的 EPS 敏感度（`eps_per_1pct_revenue`）。

3. **内部研究库四路由检索**
   - 对 `sales-notes`、`tmtb`、`funda-ai`、`company-wiki` 四个路由，分别用以下三组关键词各查一次（时间窗默认最近 92 天）：
     1. 公司名 / 股票代码
     2. 核心产品或核心业务名
     3. `"capex"` / `"费用"` / `"指引"` / `"预测"`
   - **Sales color 专项规则**：按公司名和代码分别搜，邮件按 `message_id`、研报按 `doc_id` 去重后用 `read` 读正文（多票日报按 `next_offset` 翻页摘出本公司段落）。按正文里的 sales/trading 身份声明归类，不按数据库分类（如 `Sector Specialist Commentary`、`TECH SKETCH` 可能归在 `foreign_report` 下）。

4. **AlphaEngine 卖方研报与机构会议检索**
   - 中英文 query 各搜一次（`researchdata.research(route="alphaengine", operation="search_library", query=..., start=..., end=..., params={"sort": "relevance"})`；若按文档类别过滤，可直接传 `params={"document_categories": [...], "sort": "relevance"}`，或显式调 `researchdata.research(route="alphaengine", operation="list_filters", params={"filter_type": "document_types", "document_code": "summary"})` 获取细粒度 `type_ids`——注意无参 `list_filters()` 只返回友好 `categories`，不会返回 `type_ids`；时间窗默认近 92 天）。
   - 挑 3–6 篇高相关报告调 `operation="get_document"` 读正文，优先外资行（MS、GS、UBS、JPM、Citi、BofA、Jefferies、Bernstein 等）的财报前瞻与预测调整报告。
   - 若涉及 AI 收入占比、大单 TCV/RPO 或 AI 降本提效，遵循 `references/ai-revenue-rules.md` 核对口径。

5. **一手指引与公告核对**
   - 美股用 `dalton_sec.list_filings` 与 `researchdata.finance(route="sec")` 核对最新 8-K / 10-Q / 10-K 的业绩指引原文与电话会纪要，记录 accession 与期间；港股与 A 股调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=..., end=..., limit=20)`（查不到，或者需要仅英文版的公告时，再用 `web_search` 查 `site:hkexnews.hk`）核对最新定期报告与业绩公告原文（`operation="ask"` 平台财务表仅作二手初值，核心财务数字回公告核实，回报核对复权、分红、拆股与币种）。分析师提问中提及但管理层未确认的数字，标为“分析师提问口径，管理层未确认”。

6. **按固定七段结构在对话中回复**（除非 PM 要求写文件，否则直接在对话中输出；收尾同步将赔率卡写入 `thesis.underwriting`、方向与行动分类写入 `thesis.house_view`、市场观点写入 `thesis.market_view`——先 `knowledge_update(doc="thesis", dry_run=True)` 看赔率计算摘要，再正式写入，正文赔率卡数字与 `thesis` 保持一致，不另算一套，见 `dalton-conventions/SKILL.md` §1）：

```markdown
**结论**：`stance`（方向：`long` 多头 / `short` 空头 / `neutral` 中性 / `no_view` 无观点）｜`conviction`（确信度：高 / 中 / 低 / 无〔仅 `no_view`〕）｜行动分类（现在可以动手 / 等价格〔附买点价带〕/ 等证据〔附带期限的待验证事件〕/ 不参与）与限制变量（无〔仅"现在可以动手"〕/ 价格 / 证据 / 时机 / 下行路径 / 实施条件）——一句话给立场与核心依据
**市场叙事与预期门槛**（3–5 行要点）：市场在交易什么；Street（卖方一致预期）/ 买方（bogey〔买方预期门槛〕/ whisper〔财报前的耳语预期〕）/ House（我们的判断）各自的门槛数字与口径（口径一致才比较；拿不到买方 bogey 写明原因，不因此压低 conviction〔确信度〕）；附 7/30/90 天修正轨迹与隐含费用空间（流程第 2 步产出）
**主要分歧**（聚焦 2–3 个 debate，最多 3 个，禁止硬凑，每个一行）：分歧变量｜传导路径（仅 EPS / 仅倍数 / 两者）｜Bull 怎么看（带来源）｜Bear 怎么看（带来源）｜我的倾向与置信度（若与上一版不同，写明变化原因）
**赔率卡**：三情景（bear 悲观 / base 基准 / bull 乐观）EPS × 倍数 = 价格带（单个价带宽度 `(hi−lo)/mid`〔（上沿−下沿）÷中点〕≤ 25%，三档中点严格递增，主判定取价带中点）＋ 所需胜率与裁决——按 `.agents/skills/dalton-conventions/references/risk-reward.md` §2 第 4 条计算（base 测试权重固定 25% / 50% 两行，注明"测试权重，不是预测"；多头算所需 bull 概率 `u_req(b)`、空头算所需 bear 概率 `d_req(b)`），裁决四选一（明显高于 / 接近 / 低于 / 无法判断），并写明方向、所用权重行与依据的分歧；不编造概率点值
**三层保护与失效条件**：生存（资金链 / 融资需求）｜价值（机制与区间）｜路径（跌到价值区之间会发生什么），各一句，并写明保护在什么条件下失效；遵守 `risk-reward.md` §6 的硬规则——不输出单一"底价"、回购授权不算托底（只认实际股本缩减）、EPS × 倍数后不得再加全部净现金、SBC（股票期权报酬）只算一次、历史低谷倍数只作价值层输入并写明为何仍适用、融资需求可写"未知"但写未知不得宣称生存保护充分
**非线性四问**（检查表见 `.agents/skills/dalton-conventions/references/nonlinear.md`）：倍数 regime（市场给这类资产的定价规则）变了吗｜driver（驱动变量）有没有非线性或凸性（斜率随变量增大而加大，或阈值 / 期权性质）机会｜拥挤持仓 + 催化剂怎么组合｜有没有反身性（股价反过来改变公司行为）——无信号时一行写明"本轮没有可承保的非线性机会"
**投委会四问（含双向改判条件）、默认前提自检与来源**：投委会四问（每问 1–2 句）——① 什么在改变客户行为和公司的经济性？② 为什么这个解释比竞争解释更可信？③ 市场在哪个环节判断错了？④ 看到什么我会认错（双向改判条件：看到什么可观测证据或数字会升级 / 加仓或回补，看到什么会降级 / 减仓或加空，各至少一条）；默认前提自检——结论是否仅靠"默认会复苏 / 默认管理层会兑现 / 默认政策会好转 / 默认利润率会回到历史水平 / 默认新业务会成功"支撑，若是则降为低确信或补齐证据；来源——每类来源（内部库四路由 / AlphaEngine / SEC 公告 / 一致预期）命中与实际使用数，本轮未取得的写明原因（无相关结果 / 渠道失败 / 未覆盖）
```

7. **收尾：写知识层并询问是否转成稿**
   - 按下方"三、知识层写入与发布规则"先更新 `debate`、`drivers` 并追加 `verdict`（含 `agenda_ops`），最后更新 `thesis`（快速版必做）。
   - 询问 PM 是否需要跑 `bullbear-doc` 生成包含独立股东回报支线与 XBRL 财务体检的 v4.2 完整成稿。

---

## 二、v4.2 完整成稿流程（`bullbear-doc` workflow）

当 PM 要求完整文档、深度多空成稿，或议程自动任务触发完整成稿时：

### 开始前：检查未完成的 run（中断续跑）

- 读 `research/<as_of>-bullbear/.progress.json`（及 `research/` 下其他 `*/.progress.json`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="bullbear-doc", params={...原参数..., "progress_path": "research/<as_of>-bullbear/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。若前一次 run 因审稿 `block` 等原因提前停止，补齐问题后同样用**原参数**加 `resume` 重跑（workflow 返回值里已提供 `resume_hint`）。
- 全新启动时也在 `params` 里带上 `"progress_path": "research/<as_of>-bullbear/.progress.json"`，让中断后的 run 可被发现与续跑。

1. **加载 workflow 工具**
   - 用 Read 工具读 `.agents/skills/run-workflow/SKILL.md` 了解 workflow 规范（workflow 启用时，加载本 skill 会自动解锁 `RunWorkflow`）。
   - 读 `.agents/skills/dalton-bullbear/references/v42-structure.md` 与 `references/return-branch.md` 了解产物标准。

2. **启动 `bullbear-doc`**
   - 参数说明：`as_of`（基准日期，`YYYY-MM-DD`）可不传或传 `"latest"`（`RunWorkflow` 会自动取最近一个美股交易日，`resume` 续跑时自动沿用上一 run 的值）。
   - 调用 `RunWorkflow`：
     ```json
     {
       "workflow": "bullbear-doc",
       "params": {
         "ticker": "<TICKER>",
         "company": "<公司名>",
         "market": "US",
         "workspace_dir": "<当前工作区路径>",
         "as_of": "<YYYY-MM-DD 或 latest，可不传>",
         "research_days": 92,
         "prior_draft": "<可选：上一版 bullbear 草稿或发布稿路径>",
         "progress_path": "research/<as_of>-bullbear/.progress.json"
       }
     }
     ```
   - Workflow 内部自动执行 6 个派发阶段：
     - **阶段 1（准备）**：跑 `.agents/skills/dalton-financial-lenses/scripts/anchors.py` 锁定市值与汇率（`anchors.json`）；跑 `.agents/skills/dalton-bullbear/scripts/fincheck.py` 生成资产负债表触发项与 Capex 体检（`fincheck.md`）；存一致预期快照并跑 `implied_model.py`（`consensus.md`）；锁定价格锚（`price.md`）。
     - **阶段 2（股东回报支线）**：按 `references/return-branch.md` 与 `return_score.py` 产出第一节（`return.md`，≤40 行）、第九节附录（`return_appendix.md`，≤30 行）与 5 条摘要（`return_summary.md`）。
     - **阶段 3（主稿）**：按 `references/v42-structure.md` 和 `templates/bullbear.md` 撰写 `main.md`，跑 `scripts/bb_check.py` 修到 0 WARN。
     - **阶段 4（合并与审稿）**：把 `return.md` 插入 `<!-- INSERT:return -->`、`return_appendix.md` 插入 `<!-- INSERT:return_appendix -->`，生成 `drafts/<as_of>-bullbear.md`，复跑 `bb_check.py` 到 0 WARN，并派 `dalton-reviewer` 独立审稿（含核查“`## 引用` 表存在且每个 `[引用N]` / `[回报引用N]` 都能在表里找到”）。
     - **阶段 5（修订）**：按审稿意见处理全部 P0 与可改 P1，复跑 `bb_check.py` 确认 0 WARN。
   - **Workflow 不可用时的降级路径**：
     - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按上述阶段 1→5 的顺序手动执行脚本与支线写作，最终输出到 `drafts/<as_of>-bullbear.md`，并逐阶段核对产物。阶段 4 派 `dalton-reviewer` 审稿按三步执行：（1）主智能体先计算草稿的 sha256（`sha256sum drafts/<as_of>-bullbear.md` 或等价的 Python `hashlib.sha256`）；（2）把该哈希写进派给 `dalton-reviewer` 的提示词；（3）将返回的 JSON（`verdict` / `summary` / `items` 逐项覆盖 1–9 项检查 / `must_fix` / `nice_to_have`，审稿人不回填任何哈希）保存到 `research/<as_of>-bullbear/review.json`，保存时把第（1）步算出的被审草稿 sha256 写入该 JSON 的 `draft_sha256` 字段（多轮复审时保存最后一轮；改稿后必须重新计算哈希、重新审稿并覆盖）。收尾调用 `dalton_check_done(workflow="bullbear-doc", params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<as_of>", "manual": true})`（不传 `run_id`），审稿据此文件核对。语言审查两轮（顺序不可颠倒，与 workflow 相同）：成稿后先调用 `check_language(draft_path=<草稿>, round=1)` 并按 `<草稿>.language.md` 清单修改（只改语言，不动事实、数字、术语、引用编号与判断强度），再派 `dalton-reviewer` 首次审稿；修订循环结束、终稿内容定稿后调用 `check_language(round=2)` 按清单完成终稿语言修改，草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核，然后做最后一次审稿（pass 的 JSON 存 review.json）与 `check_draft`；第一次审稿就 pass 时：第 2 轮照常执行；改了稿就补一次终稿审稿（pass 的 JSON 覆盖存 review.json），哈希未变则无需重审。本次产出中其他给人读的 .md 笔记（research/ 支线、背景、快讯、异动、公告点评、专题章节等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
     - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

3. **主智能体核查与收尾**
   - 检查 `drafts/<as_of>-bullbear.md` 是否存在，运行 `python .agents/skills/dalton-bullbear/scripts/bb_check.py drafts/<as_of>-bullbear.md` 确认 0 WARN。确认文末 `## 引用` 表存在且每个 `[引用N]` / `[回报引用N]` 都能在表里找到（每行合并写出核验目标与括号内来源说明，形如 `[引用1]: evidence:E-20261023-01 （行情｜Yahoo Finance｜价格与一致预期快照｜{DATE}｜data/consensus/...）`）。
   - 可选调用 `check_draft(draft_path="drafts/<as_of>-bullbear.md")` 查看数字核验反馈（草稿期不阻断）。
   - 执行下方知识层写入（第三节第 1–4 步）。
   - **汇报完成前必须过完成契约**：调用 `dalton_check_done(workflow="bullbear-doc", run_id=<`research/<as_of>-bullbear/.progress.json` 里的 run_id>, params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<as_of>"})`（走手动降级路径、没有 run 时不传 `run_id`，调用 `dalton_check_done(workflow="bullbear-doc", params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<as_of>", "manual": true})`，审稿结论按 `research/<as_of>-bullbear/review.json` 核对）；`done=false` 时按 `missing` 先补齐再查（缺文件补文件、`bb_check` 修到 0 WARN、知识层缺 source=tool 新版本就 `knowledge_update`、check_draft 用了旧草稿就对最终稿重跑），补不了的缺项只能汇报"未完成"并列出缺项，不得汇报"完成"。
   - 完成契约通过后，向 PM 汇报核心判断、各 Debate 倾向变化及审稿结论；如需正式发布，按第三节第 5 步执行。

---

## 三、知识层写入与发布规则（主智能体收尾必做）

无论快速版还是完整成稿，结束时均由主智能体按 `.agents/skills/dalton-knowledge/SKILL.md` 与 `.agents/skills/dalton-agenda/SKILL.md` 写入知识层（子智能体无 `knowledge_*` 工具）：

1. **`knowledge_update(doc="thesis")`**（快速版与完整成稿收尾均必做；**调用顺序硬约束**：因 `thesis` 需校验跨文档引用 `debate_ids` 与 `trigger_refs`，**必须先执行下方第 2、3、4 步将 `debate`、`drivers` 与 `agenda`（或 `agenda_ops`）落库，最后再写 `thesis`**；写 `thesis` 时先传 `dry_run=True` 查看赔率计算摘要，确认无误后再传 `dry_run=False` 正式提交，对话回复或报告正文的赔率卡数字与 `thesis` 保持一致，不另算一套；完整字段路径、枚举值与过检 JSON 样例统一见 `.agents/skills/dalton-knowledge/references/structured-write-example.md`）
   - **`house_view`（我方观点）**：必填 `stance`（方向：`long` 多头 / `short` 空头 / `neutral` 中性 / `no_view` 无观点四选一；`pass` 不是 `stance` 的取值）、`conviction`（确信度：`high` 高 / `medium` 中 / `low` 低 / `none` 无，`none` 仅用于 `no_view`，上限遵守 `dalton-conventions/references/risk-reward.md` §6）、`actionability`（行动分类：`act_now` / `wait_price` / `wait_evidence` / `pass`）、`binding_constraint`（限制变量：`none`〔仅 `act_now`〕/ `price` / `evidence` / `timing` / `downside_path` / `implementation`）、`horizon_months`（期限月数，1–60）、`flip_conditions`（双向改判条件：`upgrade_if` 与 `downgrade_if` 两个非空字符串，`long`/`short`/`neutral` 时两边均须填写具体指标、数字阈值、观察窗口与动作）、`summary`（一句话方向结论与核心理由）、`updated_at`（更新时间）；`wait_price` 必填 `entry_band`（买点价带，含 `lo`、`hi`），`wait_evidence` 必填 `trigger_refs`（指向 `agenda` 中带明确到期日的待验证事项或唤醒条件 ID，必须先写入 `agenda` 再写 `thesis`）；`no_view` 时 `actionability` 填 `"pass"`，并必填 `missing`（缺失信息数组）与 30 天内的 `review_by`；可选 `variant_perception`、`position_suggestion`、`target`（仅指向已登记模型单元格时使用）。
   - **`underwriting`（赔率卡与三层保护）**：`stance` 为 `long` / `short` / `neutral` 时必填三情景 `states`（`bear` / `base` / `bull` 三项，每项含 `name`、`condition`、`calc`〔`method="eps_pe"` 填 `eps`、`multiple`、`basis`、`period`、`sources`，或 `method="reference"` 填 `price_range`、`ref`〕、每股股东现金回报 `cash_return_per_share`、关联 `debate_ids`）、基准股价 `anchor`（`price`、`currency`、`price_at`、`source`）、可选有来源支撑的 `weight_bounds`（`bear`、`base`、`bull`、`rationale`、`sources`）、三层保护 `protection`（`survival`、`needs_financing`、`value`、`value_range`、`path`、`failure`；空头在 `underwriting` 顶层填 `borrow_cost_annual` 与逼空压力情景 `stress`）、胜率裁决数组 `breakeven_verdict`（每项含 `side`、`b_row`、`verdict`〔`clearly_above` / `around` / `below` / `cannot_judge`〕、`basis` 与 `debate_ids`；`neutral` 时数组须同时包含 `side="long"` 与 `side="short"` 的条目）、`scenario_version`（改动 `anchor`/`states`/`weight_bounds` 时递增）与 `hurdles`（门槛参数快照）。
   - **`market_view`（市场观点）**：写入 `summary`（市场正在交易的主叙事）、`observations`（预期观察列表，每项含主张 `claim`、来源身份 `who`、日期 `as_of`、来源引用 `sources` 与置信度 `confidence`）、可选 `figures`（含 `metric`、`period`、`value`、`unit`、`label`）与 `positioning`（持仓拥挤度：`crowded_long` / `balanced` / `crowded_short` / `unknown`）。

2. **`knowledge_update(doc="debate")`**
   - 每个 Debate 写入一条结构化记录：分歧变量、多方论据（Bull）、空方论据（Bear）、双方最强反驳、我的判断（立场与置信度）、依赖假设、改判条件、来源引用。
   - 若某条 Debate 的倾向或置信度相比旧版发生变化，使用 `set` 操作更新并在 `reason` 中写明变化原因（服务端自动在 `debates[].history` 保留旧值与原因）。

3. **`knowledge_update(doc="drivers")`**
   - 将 §4（Earnings revision map）识别出的核心驱动变量与 EPS/收入敏感度、§5（Catalyst & KPI）的 3–5 个跟踪 KPI 及 bull/bear 区间写入 `drivers`（新读数用 `append`）。

4. **`knowledge_append_verdict`**
   - `trigger.kind`：PM 发起填 `"manual"`，议程触发填 `"agenda"`。
   - `verdict`：对照上一版 debate 与 thesis 给出正式五词结论之一（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）。
   - `agenda_ops`：把 §5 的催化剂写入 `watch.events`，把改判条件写入 `next_validations`。

5. **发布（`publish_report`）**
   - 对话快速版不发布。
   - 完整成稿默认保存在 `drafts/<as_of>-bullbear.md`；当 PM 要求正式发布时，调用 `publish_report(draft_path="drafts/<as_of>-bullbear.md", title="...", kind="bullbear", slug="...")`。

---

## 四、本 Skill 目录文件

- `references/v42-structure.md`：v4.2 完整成稿章节结构、写作与引用规则、信源检索细则。
- `references/return-branch.md`：股东回报支线（第一节 ≤40 行、第九节附录 ≤30 行、5 条摘要）规则。
- `references/ai-revenue-rules.md`：AI 收入口径（已确认收入 vs ARR vs TCV/RPO）与 AI 提效费用核查规则。
- `templates/bullbear.md`：v4.2 中文成稿 Markdown 模板（含 `<!-- INSERT:return -->` 与 `<!-- INSERT:return_appendix -->`）。
- `scripts/bb_check.py`：v4.2 机械格式检查与支线合并脚本。
- `scripts/fincheck.py`：基于 `dalton_sec` / SEC XBRL companyfacts 的资产负债表大额/跳变科目与 Capex 强度体检脚本。
- `scripts/implied_model.py`：一致预期隐含税前利润、隐含总费用空间与 EPS 敏感度测算脚本。
