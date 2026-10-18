# 方法论规则溯源索引（SOURCES — 仅供维护者核对，Agent 不必读取）

本文件记录 `dalton-conventions` 各文件每一节规则对应的上游真源路径、JSON 路径与行号，供维护者审计与上游同步核对。

---

## 1. `SKILL.md`（总纲）

| 章节 | 规则摘要 | 上游真源路径 | JSON 路径 / 行号 |
|---|---|---|---|
| §1 角色与边界 | 基本面多头导向、不强迫看多、"暂无高 conviction"、PM 决定交易 | `/Users/everflow/Projects/dalton-research-agent-os/deploy/phase9/p9a-research-playbook-v1.json`<br>`docs/dalton/blueprint.md` | `$.investor_mandate` (L5–28)<br>§3.1 (L190–215) |
| §2 与 `research-conventions` 的关系 | 6 项主题优先级对照表 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | §P1.1 (L911–921) |
| §3 八条底线 | 数字带来源/期间/口径、模型只由证据改动、五词结论、完整空方、来源身份、先量化异动、只写重要变化、诚实边界 | `docs/dalton/blueprint.md`<br>`p9a-research-playbook-v1.json` | §3.1 (L190–215)<br>`$.non_negotiables` (L29–42) |
| §4 按任务找参考 | 任务到 `references/*.md` 的路由表 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | §P1.1 (L899–905) |
| §5 合规口径 | 专家访谈、管理层交流、Guidepoint 合规可用；标身份不写姓名；单源估计；Guidepoint ≤20 词 | `~/.openclaw/workspace/skills/bullbear-discussion/references/common_task.md`<br>`docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | L136–154 (尤其 L139)<br>§P1.1 (L922–930) |
| §6 数据入口速查 | `researchdata`、`dalton_sec`、`yfinance`、`estimates` 快照规则 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | §P0-D.2, §P0-D.5, §P0-D.6 (L804–811), 附录 A |
| §7 公司知识层读写约定 | PK.1 布局、PK.2 Schema、PK.4 工具、PK.6 托管块、wiki 只读 | `docs/dalton/dev-guide/PK-knowledge-governance-autonomy.md` | §PK.1, §PK.2, §PK.4, §PK.6 |

---

## 2. `references/method.md`（研究流程与关键问题）

| 章节 | 规则摘要 | 上游真源路径 | JSON 路径 / 行号 |
|---|---|---|---|
| §1 三段式研究流程 | Initial Screen、Investment Memo、Active Coverage，去掉机械关卡与字数门槛 | `p9a-research-playbook-v1.json`<br>`docs/dalton/blueprint.md` | `$.stages[0..5]` (L72–195)<br>§3.2 (L217–245) |
| §2 初筛四问与 8 节结构 | 初筛核心四问、`initial_screen_note` 8 节模板 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace-chem/references/manual/research-process.md` | `$.stages[1]`, `$.deliverable_templates.initial_screen_note` (L340–365)<br>L1–45 |
| §3 深度认知 12 问 | 12 问清单及每问必填 6 要素（判断、证据、反方证据、未知项、falsifier、下一项验证） | `p9a-research-playbook-v1.json` | `$.stages[2].deep_cognition_12_questions` (L105–148) |
| §4 完整覆盖 12 Key Questions 与 Memo 12 段 | 12 Key Questions、Investment Memo 12 段结构 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace-chem/references/manual/memo-template.md` | `$.key_questions` (L196–250), `$.deliverable_templates.investment_memo` (L366–395)<br>L1–75 |
| §5 Good Business / People / Price | 商业质量、管理层与股票估值吸引力分开判断 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/SKILL.md` | `$.key_questions` (L196–235)<br>L11–42 |
| §6 Variant Perception 三问 | 市场认为什么、实际上是什么、为什么错误定价还在 | `~/.openclaw/workspace-chem/references/manual/research-process.md`<br>`company-deepdive-mx-0911/SKILL.md` | L18–32<br>L43–60 |
| §7 Anti-Thesis 与 Pre-Mortem | 完整反向叙事（非风险清单）、3–5 条事前验尸、不立稻草人 | `p9a-research-playbook-v1.json`<br>`company-deepdive-mx-0911/references/writing_guide.md` | `$.stages[3]` (L149–168)<br>L78–89 |
| §8 零基思考 | "如果今天重新开仓，还买不买" | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace-chem/references/manual/tracker-checklist.md` | `$.stages[5]` (L178–195)<br>L15–30 |
| §9 标准推导链条 | Driver Tree → 关键变量 → 市场隐含假设 → 差异化观点 → 三情景 → 催化剂与证伪 | `company-deepdive-mx-0911/SKILL.md` | L15–73 |

---

## 3. `references/evidence-and-sources.md`（来源层级、身份、去重与合规）

| 章节 | 规则摘要 | 上游真源路径 | JSON 路径 / 行号 |
|---|---|---|---|
| §1 6 级来源层级与禁用来源 | Filing → 公司公告 → 署名研报/官方统计 → 独立产业数据 → 专家 → 新闻；禁用百度百科 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace/skills/company-preview/SKILL.md`<br>`company-deepdive-mx-0911/references/common_rules.md` | `$.evidence_discipline.source_hierarchy` (L275–298)<br>L33–57<br>L13–18 |
| §2 `_sources.identity` 对照表 | `researchdata` 与 `dalton_sec` 14 类路由的身份映射 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | 附录 B (L1450–1468) |
| §3 来源链去重 | 同源转述只算一条；单一来源标"单源观点"；点赞转发不算共识 | `~/.openclaw/workspace/skills/bullbear-discussion/references/common_task.md`<br>`~/.openclaw/workspace/skills/company-preview/references/retrieval.md` | L33–47<br>L5, L50, L54 |
| §4 关键数字双源验证 | 关键非财报数字至少两个独立来源 | `p9a-research-playbook-v1.json` | `$.evidence_discipline` (L285–305) |
| §5 五类预期身份区分 | 指引、卖方一致预期、具体机构预测、买方门槛、作者判断；分歧不取平均 | `~/.openclaw/workspace/skills/bullbear-discussion/SKILL.md`<br>`bullbear-discussion/references/common_task.md` | L73, L76<br>L33–47 |
| §6 电话会提问口径 | 分析师提问数字未经管理层确认标"分析师提问口径，管理层未确认" | `bullbear-discussion/references/common_task.md` | L40–47 |
| §7 "本轮未取得" vs "未披露" | 检索未命中写"本轮未取得"，限定在已覆盖范围 | `company-deepdive-mx-0911/references/common_rules.md` | L33 |
| §8 数字底表原则 | 目标公司数字只取底表，不用相近数字顶替 | `company-deepdive-mx-0911/references/execution.md` | L66–68 |
| §9–§10 合规口径与引用上限 | 专家合规全文；Guidepoint ≤20 词，其他 ≤3 句 | `bullbear-discussion/references/common_task.md`<br>`docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | L136–154<br>§P0-D.2, §P1.1 (L922–930, L937) |

---

## 4. `references/model-standards.md`（财务模型期间与分层）

| 章节 | 规则摘要 | 上游真源路径 | JSON 路径 / 行号 |
|---|---|---|---|
| §1 期间标准 | 年度 2018A 起、单季 2024Q1A 起、预测 3 年 / 至次年 Q4、只扩不缩 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace/skills/bullbear-discussion/references/return_task.md` | `$.model_discipline.period_standards` (L306–322)<br>§1.1 |
| §2 一手 Filing 与单元格台账 | 硬编码只取一手 filing，6 项单元格级台账 | `p9a-research-playbook-v1.json`<br>`company-deepdive-mx-0911/references/execution.md` | `$.model_discipline` (L310–330)<br>L54–64 |
| §3 累计拆单季 | `Q2=H1-Q1`, `Q3=9M-H1`, `Q4=FY-9M`，标 `Derived filing actual` | `bullbear-discussion/references/return_task.md`<br>`docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | §3<br>§P0-D.5, §P1.1 (L938) |
| §4–§6 五层分类、留空、修订对账 | 5 层分类（严禁残差冒充披露）、空值留空不填 0、修订记录与事后对账 | `p9a-research-playbook-v1.json`<br>`bullbear-discussion/references/return_task.md` | `$.model_discipline` (L320–339)<br>§3, §4 |
| §7 四大财务镜头 | 净现金双口径、隐蔽资产、股东回报 R1/R2/R3、盈利质量（Sloan、CFO/NI、CCC） | `~/.openclaw/workspace/skills/findata-netcash/SKILL.md`<br>`~/.openclaw/workspace/skills/findata-hiddenasset/SKILL.md`<br>`bullbear-discussion/scripts/fincheck.py` | L1–85<br>L1–80<br>L1–120 |
| §8 期间命名与单位规则 | 与 `dalton_sec` R1–R10 对齐 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | §P0-D.5 (L660–750) |

---

## 5. `references/risk-reward.md`、`event-verdicts.md`、`writing.md`、`analyst-levels.md`

| 文件与章节 | 规则摘要 | 上游真源路径 | JSON 路径 / 行号 |
|---|---|---|---|
| `risk-reward.md` §1–§5 | 四类机会回报标准（Compounder 3–5yr 3–5x、周期反转、价值 6–12m +50%、做空 3–6m -30%）、三情景 IRR、仓位词汇、暂无高 conviction、PM 决定交易 | `p9a-research-playbook-v1.json`<br>`company-deepdive-mx-0911/SKILL.md`<br>`manual/research-process.md` | `$.risk_reward_standards` (L251–274)<br>L55–59<br>L25 |
| `event-verdicts.md` §1–§5 | 五词结论正式词形与中文标签、事件 5 要素、Priced in 与催化剂耗尽、5 类错误归因、无变化不推送 | `p9a-research-playbook-v1.json`<br>`docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | `$.stages[5]` (L178–195), `$.decision_vocabulary.event_verdicts` (L396–415)<br>§P1.1 (L940) |
| `event-verdicts.md` §6 | 股价异动先量化同组与 ETF 剥离 Beta/Alpha，再按四级顺序查因 | `~/.openclaw/workspace/skills/stock-move-analyzer/SKILL.md` | L1–95 |
| `writing.md` §1–§6 | 判断先行（≤3 句）、段落四步法、正向改写警告、引用格式与期间、中文交付与禁用词表、"相当于" vs "占"、区间"至"、金额 USD/美元、一句话收尾 | `bullbear-discussion/references/common_task.md`<br>`bullbear-discussion/SKILL.md`<br>`company-deepdive-mx-0911/references/writing_guide.md`<br>`company-deepdive-mx-0911/references/common_rules.md` | §0.1 (L15–24)<br>L13, L56<br>L5–13, L103<br>L56 |
| `analyst-levels.md` §1–§3 | Basic L1–L4 与 Advanced L1–L4 标准、当前定位、Variant View 命中率验收 | `p9a-research-playbook-v1.json`<br>`~/.openclaw/workspace-chem/references/manual/analyst-levels.md` | `$.analyst_levels` (L43–71)<br>L1–65 |
