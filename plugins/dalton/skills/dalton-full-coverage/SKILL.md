---
name: dalton-full-coverage
description: "完成一手数字底表、四支线并行取证、15 页动态公式 Excel 三表与 DCF 模型、三段式 12 问深度投资备忘录及双审稿发布。对重点标的开展立项级深度建仓研究、需要完整估值模型与正式备忘录时使用；属于最重度完整深度研究。"
---

# Dalton 买方完整覆盖（`dalton-full-coverage`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 是 Dalton 买方完整覆盖（P3 深度覆盖阶段）的启动与发布控制器。目标是针对已完成初次覆盖（或 PM 直接指定深度覆盖）的标的，一次运行产出：
1. **数字底表（`research/<D>-fc/facts.md`）**：含 `<!-- FACTS-LEDGER ... -->` 机器可读账本，作为全文唯一目标公司财务数字来源与发布核验基准；
2. **公司专属 12 问研究提纲（`research/<D>-fc/questions.md`）** 与 **A/B/C/D 四条取证支线底稿（`ea.md`、`eb.md`、`ec.md`、`ed.md`）**；
3. **带动态公式的 15 页 Excel 财务与估值模型（`model/<TICKER>-model-v01.xlsx`）**：含标准 12 页 + Dalton 专属 `Facts`、`Guidance vs Actual`（12 季表）、`Street vs House` 三页；
4. **12 问前置总纲 + 十章论证深度投资备忘录（`drafts/<D>-memo.md`）**；
5. **模型审计（`check-model`）与独立审稿（`dalton-reviewer`）**，修订后更新公司知识层并经 `publish_report(kind="full_coverage", updates_thesis=True)` 提交正式发布审批。

---

## 一、输入参数确认

启动前确认以下参数（可从当前工作区 `agent.md`、`company.md` 及 `../Home/dalton/coverage.yaml` 读取默认值）：

- `ticker`：规范股票代码（如 `META`、`MSFT`）
- `company`：公司名称
- `market`：市场（支持 `US`、`HK`、`CN`；港股与 A 股的一手公告与财务初值通过 `researchdata.finance(route="cn-hk", operation="filings"|"ask", ...)` 获取，平台财务表仅作二手初值线索，核心财务数字必须回法定公告原文核实，港股披露易未覆盖时用 `web_search` 查 `site:hkexnews.hk` 原文链接；回报与估值核对复权、分红、拆股与币种）
- `workspace_dir`：当前公司工作区路径
- `as_of`：基准日期（`YYYY-MM-DD`）
- `profile`：`"tob"`（面向企业）或 `"toc"`（面向消费者），决定 D 支线做从业者信号表还是多平台产品体验表
- `form`：`"10-K"`（美国本土发行人）、`"20-F"`（外国私人发行人）或 `"annual_report"`（港股/A 股年度报告）
- `pm_brief`（可选）：PM 的具体研究指示或假设
- `focus_questions`（可选）：PM 要求重点深挖的问题列表

### 开始前：检查未完成的 run（中断续跑）

- 读 `research/<D>-fc/.progress.json`（及 `research/` 下其他 `*/.progress.json`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="full-coverage", params={...原参数..., "progress_path": "research/<D>-fc/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。
- 全新启动时也在 `params` 里带上 `"progress_path": "research/<D>-fc/.progress.json"`，让中断后的 run 可被发现与续跑。

---

## 二、执行流程（启动 `full-coverage` workflow）

1. **加载 `run-workflow` 工具与规范**
   - 用 Read 工具读 `.agents/skills/run-workflow/SKILL.md` 了解 workflow 规范（workflow 启用时，加载本 skill 会自动解锁 `RunWorkflow`）。
   - 读 `.agents/skills/dalton-conventions/SKILL.md` 与当前工作区知识层（`thesis.md`、`debate.md`、`drivers.md`、`management.md`、`agenda.md`）。

2. **调用 `RunWorkflow`**
   ```json
   {
     "workflow": "full-coverage",
     "params": {
       "ticker": "<TICKER>",
       "company": "<公司名>",
       "market": "US",
       "workspace_dir": "<workspace_dir>",
       "as_of": "<YYYY-MM-DD>",
       "profile": "tob",
       "form": "10-K",
       "pm_brief": "<可选>",
       "focus_questions": [],
       "progress_path": "research/<D>-fc/.progress.json"
     }
   }
   ```
   `full-coverage` workflow 按以下阶段自动推进（约 15 次子智能体派发）：
   - **阶段 0（一手材料与数字底表）**：精读最近 4 份定期报告、4 次电话会、招股书（如有）、内部人交易；复用 `dalton-financial-lenses` 计算三把镜头；计算盈利质量（Sloan 比率、CFO/NI、CCC）；用 `scripts/facts.py set` 写入 `research/<D>-fc/facts.md` 的 `FACTS-LEDGER`。若阶段 0 失败则中止。
   - **阶段 0.8（问题清单 frame）**：按 `references/frame.md` 撰写 `research/<D>-fc/questions.md`，并跑 `python .agents/skills/dalton-full-coverage/scripts/research_structure.py validate-agenda research/<D>-fc/questions.md` 直至通过。
   - **阶段 1（四条取证支线并行）**：
     - `ea.md`：按 `references/branch-a-industry.md`（行业定义与双法 TAM、5–8 家竞对现取财务与份额、周期高频指标、全球对标、政策、行业专用框架）；
     - `eb.md`：按 `references/branch-b-sellside-expert.md`（AlphaEngine 分类检索与全文精读、卖方预期地图、内部库与 company-wiki、Guidepoint 英文专家纪要、卖方盲点）；
     - `ec.md`：按 `references/branch-c-primary.md`（4 期报告 MD&A/Risk Factors diff、4 期电话会措辞漂移、招股书承诺对照、**C4 过去 12 个季度指引 vs 实际可信度表**、8-K 与内部人交易）；
     - `ed.md`：按 `references/branch-d-offstreet.md`（X、雪球、员工评价、社区情绪、招聘信号、13F、ToC 产品体验表或 ToB 从业者信号表）。
   - **阶段 2 & 3（建模与 memo 三段式写作）**：
     - **建模**：按 `references/model-spec.md` 构建 `model/<TICKER>-model-v01.xlsx`（`recalc.py` 与 `audit.py --strict` 通过，Checks 全 OK），与 memo 第一段并行；
     - **memo 第一段**（基本信息、核心判断、零、一、二）→ 跑 `validate_memo.py --part 1` 到 0 FAIL；
     - **memo 第二段**（三至六，引用模型三情景与敏感度）→ 跑 `validate_memo.py --part 2` 到 0 FAIL；
     - **memo 第三段**（七至十、附录、引用清单，第七章含 C4 指引可信度表，第九章估值与模型一致）→ 跑 `validate_memo.py --mode full` 到 0 FAIL。
   - **阶段 4（审模型与独立审稿并行）**：`check-model` 审模型输出 `research/<D>-fc/model-check.md`（只出报告不改模型）∥ `dalton-reviewer` 审 memo。
   - **阶段 5（修订与知识层修订草案）**：按审稿意见与 `model-check.md` 修订 memo 和模型（若改模型，按 `.agents/skills/model-update/SKILL.md` 另存 `v02`，绝不覆盖 `v01`），并生成 `research/<D>-fc/knowledge-revision.json`。

3. **Workflow 不可用时的手动降级路径**
   - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按阶段 0 → 0.8 → 1 → 2/3 → 4 → 5 的顺序手动完成（依次调用子智能体或亲自执行）各阶段产物与脚本校验，并逐阶段核对产物。独立审稿阶段派 `dalton-reviewer` 审最终稿后，把它返回的 JSON（含 `verdict` 与 `draft_sha256`）原样保存到 `research/<D>-fc/review.json`（多轮复审时保存最后一轮；改稿后必须重审并覆盖）；收尾调用 `dalton_check_done` 时在 `params` 里加 `"manual": true`、不传 `run_id`，审稿据此文件核对。语言审查两轮（顺序不可颠倒，与 workflow 相同）：成稿后先调用 `check_language(draft_path=<草稿>, round=1)` 并按 `<草稿>.language.md` 清单修改（只改语言，不动事实、数字、术语、引用编号与判断强度），再派 `dalton-reviewer` 首次审稿；修订循环结束、终稿内容定稿后调用 `check_language(round=2)` 按清单完成终稿语言修改，草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核，然后做最后一次审稿（pass 的 JSON 存 review.json）与 `check_draft`；第一次审稿就 pass 时：第 2 轮照常执行；改了稿就补一次终稿审稿（pass 的 JSON 覆盖存 review.json），哈希未变则无需重审。本次产出中其他给人读的 .md 笔记（research/ 支线、背景、快讯、异动、公告点评、专题章节等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
   - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

## 三、主智能体收尾：知识层更新、数字核验与正式发布

Workflow 返回后，主智能体按以下 6 步完成收尾（完成契约在完成判定与向 PM 汇报"完成"之前）：

### 第 1 步：检查审稿结论与机械门禁
- 读取 workflow 返回状态、审稿意见与 `validate_memo.py --mode full` 结果。
- 若审稿结论为“不得发布”或仍有未解决的 P0 缺陷，**停留在草稿状态**，向 PM 汇报具体原因与补证计划，不发起 `publish_report`。

### 第 2 步：展示并写入知识层修订（`knowledge_update` + `knowledge_append_verdict`）
- 读取 `research/<D>-fc/knowledge-revision.json`，将核心认知改动（`旧值 → 新值 → 原因与证据编号`）展示给 PM（无人值守运行时直接写入工作版本），然后逐个 doc 调用 `knowledge_update`：
  - **`thesis`**：更新支柱、确认/警告/证伪阈值、追加证据台账行（`source.type` 与 `source.tier` 对应）、更新 `house_view`（**硬约束**：若给出目标价，`house_view.target` 必须带 `model_ref.kind="model_cell"` 指向 Excel 模型单元格）；
  - **`debate`**：更新完整覆盖后的核心多空分歧、倾向、置信度与改判条件；
  - **`drivers`**：更新核心业务驱动树变量、模型敏感度与高频跟踪指标；
  - **`management`**：写入 C4 支线整理的 12 季指引兑现历史（`guidance_history`）与指引风格定性结论（`guidance_style`：保守/中性/激进、收入还是利润更保守、全年修正节奏）；
  - **`lessons`**：若完整覆盖推翻了初次覆盖中的某项早期判断，记录一条经验教训（错误类型、适用条件、反例与证据）；
  - **`models`**：登记模型文件 `model/<TICKER>-model-v01.xlsx`（及修订版 `v02`，如有）；
  - **`company`**：将 `coverage_stage` 更新为 `"full"`。
- 调用 **`knowledge_append_verdict`**：
  - 对照初次覆盖的 thesis 给出正式五词结论（通常为 `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `NEW_THESIS` / `THESIS_BROKEN` / `NO_CHANGE`）；
  - 在 `agenda_ops` 中写入完整覆盖后的持续跟踪议程（第十章的监控清单、高频指标与催化剂日历）。

### 第 3 步：开启自动跟踪（`dalton_coverage_tracking`）
- 调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 开启（或补齐）该公司的标准自动跟踪任务（`watch`、`delta`、`move-up`、`move-down`）。若用户之前手动关闭过（带 `paused_by_user` 标记），`source="coverage_finish"` 会自动保持关闭状态，不重新打开。

### 第 4 步：`check_draft` 预检与 `publish_report` 正式发布
1. 先调用 `check_draft(draft_path="drafts/<D>-memo.md")` 查看数字核验反馈（生成 `drafts/<D>-memo.audit.md`），修复所有未匹配或无来源的数字。
2. 调用 `publish_report` 发起正式发布：
   ```json
   {
     "draft_path": "drafts/<D>-memo.md",
     "title": "<公司名>（<TICKER>）深度投资备忘录",
     "kind": "full_coverage",
     "slug": "<ticker-lower>-full-coverage-<D>",
     "attachments": [
       {"path": "model/<TICKER>-model-v01.xlsx", "role": "model"},
       {"path": "research/<D>-fc/facts.md", "role": "facts"}
     ],
     "updates_thesis": true
   }
   ```
   - **注意**：`attachments` 必须同时附带模型文件（`role: "model"`）和数字底表 `research/<D>-fc/facts.md`（`role: "facts"`，作为发布核验的对账基准）；其余支线文件与草稿审稿意见留在 `research/`，不进发布包。
   - 若核验退回或服务端独立审稿返回 `revise`，逐条修订后再调 `publish_report`，直至进入 PM 审批卡（`awaiting_pm`）。

### 第 5 步：汇报完成前必须过完成契约（`dalton_check_done`）

- 向 PM 汇报"完成"前，必须调用 `dalton_check_done(workflow="full-coverage", run_id=<`research/<D>-fc/.progress.json` 里的 run_id>, params={<启动 RunWorkflow 时的同一组参数，含 as_of>})`。
- 走手动降级路径（`workflow.enabled` 已关闭、没有 run）时不传 `run_id`，`params` 里加 `"manual": true`，审稿结论按 `research/<D>-fc/review.json` 核对。
- `done=false` 时按 `missing` 逐条补齐（缺文件补文件、`facts.py check`/`validate_memo`/`audit.py` 未过修到过、知识层缺 source=tool 新版本就 `knowledge_update`、check_draft 用了旧草稿就对最终稿重跑 `check_draft`），补完再查一次。
- 补不了的缺项只能汇报"未完成"，列出缺项与原因；没有 `dalton_check_done` 通过，不得汇报"完成"。

### 第 6 步：完成判定（必查清单）
- `drafts/<D>-memo.md`、`model/<TICKER>-model-v01.xlsx` 及 `research/<D>-fc/facts.md` 均已通过校验；
- 知识层（含 `company.coverage_stage="full"`）与 `knowledge_append_verdict` 已落库；
- 已调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 并检查确认自动跟踪已开启（`status="enabled"`，或因 `paused_by_user=true` 保持关闭）；
- `publish_report` 已提交进入 PM 审批卡（`awaiting_pm`），PM 在审批卡片上批准后，发布管线自动将 `thesis.official`（含正式 house view）写入知识层，并在 `published/` 下生成不可变正式副本。

---

## 四、本 Skill 目录文件

- `references/facts-ledger.md`：阶段 0 数字底表（`FACTS-LEDGER`）规则与必填键清单。
- `references/frame.md`：阶段 0.8 公司专属 12 问问题清单（`questions.md`）规则。
- `references/branch-common.md`：A/B/C/D 四条取证支线通用规则。
- `references/branch-a-industry.md`：支线 A（行业与竞争）取证规则。
- `references/branch-b-sellside-expert.md`：支线 B（卖方、内部库与专家访谈）取证规则。
- `references/branch-c-primary.md`：支线 C（一手文件精读与 C4 12 季指引可信度表）取证规则。
- `references/branch-d-offstreet.md`：支线 D（场外信号、产品体验与从业者反馈）取证规则。
- `references/memo-writing.md`：12 问前置总纲 + 十章论证写作指南、章节映射与三段式写作流程。
- `references/model-spec.md`：15 页 Excel 模型规格（标准 12 页 + Dalton 增补 3 页）。
- `scripts/research_structure.py`：12 问映射表、问题清单校验（`validate-agenda`）与备忘录结构校验。
- `scripts/facts.py`：`facts.md` 中 `FACTS-LEDGER` 的命令行读写与必填键检查工具。
- `scripts/validate_memo.py`：备忘录机械校验脚本（支持 `--part 1|2|3` 与 `--mode full`）。
- `scripts/test_research_structure.py`：12 问结构与问题清单离线回归测试套件。
- `templates/memo.md`：12 问深度投资备忘录中文模板。
- `templates/questions.md`：公司专属 12 问研究提纲中文模板。
