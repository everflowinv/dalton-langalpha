---
name: dalton-initial-coverage
description: "并行执行公司背景、行业竞争、三把财务镜头与估值历史四条支线，产出一份买方 12 问初筛报告并建立公司初始知识层。将新标的纳入覆盖、需要在深入建模前全面评估商业质量与多空焦点时使用；属于多支线并行深度研究，耗时较长。"
---

# Dalton 买方初次覆盖（`dalton-initial-coverage`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 是 Dalton 买方初次覆盖（P2 初筛阶段）的启动与收尾控制器。目标是围绕一家新覆盖公司，一次运行产出 **12 问初筛报告**（`drafts/<as_of>-initial-coverage.md`）+ **初始化的公司知识层**（`company`、`thesis`、`debate`、`drivers`、`management`、`agenda` 与首条 `NEW_THESIS` 结论），回答买方初筛最核心的问题：主营靠什么赚钱、行业特性与周期、长短期驱动、竞争位置与壁垒（正在形成还是被破坏）、历史股价是 EPS 驱动还是倍数驱动、当前 bull/bear debate、股价位置、叙事演绎与高 conviction 预期差（若无足够预期差则诚实写“暂无高 conviction”）。

---

## 一、输入参数确认

启动前确认以下参数（若 `../Home/dalton/coverage.yaml` 或当前工作区已提供则直接读取，无需重复追问）：

- `ticker`：规范股票代码（如 `META`、`APP`）
- `company`：公司名称
- `market`：市场（支持 `US`、`HK`、`CN`；港股与 A 股通过 `researchdata.finance(route="cn-hk", operation="filings"|"ask", ...)` 获取法定公告、财务表初值、行情、资金流、两融、股东与 AH 溢价——平台财务表仅作二手初值线索，核心财务数字必须回法定公告原文核实，港股披露易未覆盖时用 `web_search` 查 `site:hkexnews.hk` 原文链接，回报与估值核对复权、分红、拆股与交易/报表币种）
- `workspace_dir`：当前公司工作区根目录（如 `.` 或工作区路径）
- `as_of`：基准日期（`YYYY-MM-DD`，默认今天）
- `peers`（可选）：可比同业代码列表；不提供时由行业支线与估值支线自行选定 5–8 家竞争对手及 ≥3 家一档可比公司，并写明选取理由
- `sector`（可选）：所属行业名称
- `pm_question`（可选）：PM 特别指定的关注问题或假设

---

## 二、执行流程（启动 `initial-coverage` workflow）

1. **加载 `run-workflow` 工具**
   - 用 Read 工具读 `.agents/skills/run-workflow/SKILL.md` 了解 workflow 规范（workflow 启用时，加载本 skill 会自动解锁 `RunWorkflow`）。
   - 读 `.agents/skills/dalton-conventions/SKILL.md` 确认买方初筛标准。

2. **调用 `RunWorkflow`**
   ```json
   {
     "workflow": "initial-coverage",
     "params": {
       "ticker": "<TICKER>",
       "company": "<公司名>",
       "market": "US",
       "workspace_dir": "<workspace_dir>",
       "as_of": "<YYYY-MM-DD>",
       "peers": ["<可选同业>"],
       "sector": "<可选行业>",
       "pm_question": "<可选PM问题>"
     }
   }
   ```
   `initial-coverage` workflow 会自动分 5 个阶段（共 9 次子智能体派发）执行：
   - **阶段 1（并行 4 支线取证）**：
     - **A 公司背景**：按 `.agents/skills/dalton-company-background/SKILL.md` 输出 `drafts/<D>-background.md`；
     - **B 行业支线**：按 `references/branch-industry.md` 输出 `research/<D>-ic/industry.md`；
     - **C 财务镜头**：按 `.agents/skills/dalton-financial-lenses/SKILL.md` 输出 `research/<D>-ic/lenses.json` 与 `drafts/<D>-lenses.md`；
     - **D 估值历史与可比**：按 `references/branch-valuation-history.md` 与 `.agents/skills/comps-analysis/SKILL.md` 输出 `research/<D>-ic/valuation.md` 与 `research/<D>-ic/comps.xlsx`。
   - **阶段 2（bull/bear 快速版）**：综合 A–D 支线产物与内部库/AlphaEngine 检索，按 `.agents/skills/dalton-bullbear/SKILL.md` 快速版方法输出 `research/<D>-ic/bullbear-quick.md`。
   - **阶段 3（12 问初筛成稿）**：按 `references/screen-12q.md` 与 `templates/initial-coverage.md` 撰写 `drafts/<D>-initial-coverage.md`。
   - **阶段 4（知识层初始化草案）**：从初筛成稿抽取结构化知识层操作草案 `research/<D>-ic/knowledge-proposal.json`（只抽取不新增判断）。
   - **阶段 5（独立审稿与修订）**：由 `dalton-reviewer` 审阅重点题（Q1–Q5、Q7、Q8、Q12）、壁垒判断与估值分解表，再由修订子智能体完成 P0 修改并同步更新 `knowledge-proposal.json`。

3. **Workflow 不可用时的手动降级路径**
   - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按上述阶段 1→5 的顺序手动执行（依次派发 `dalton-evidence` / `general-purpose` 子智能体或亲自执行），生成全部产物文件，并逐阶段核对产物。
   - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

## 三、知识层初始化写入（Workflow 结束后主智能体必做）

> 子智能体没有 `knowledge_update` 与 `knowledge_append_verdict` 工具。Workflow 跑完后，必须由主智能体审读 `drafts/<D>-initial-coverage.md` 与 `research/<D>-ic/knowledge-proposal.json` 并落库（有 PM 在场时先向 PM 展示核心判断与草案摘要，无人值守自动运行时直接写入工作版本）。

1. **逐个文档调用 `knowledge_update`**
   - 新工作区文档尚不存在时传 `base_version=null` 并使用 `create` 操作；已有文档时使用 `insert` / `append` / `set`：
     - **`company`**：写入一句话业务判断、所属行业、是否周期股，并将 `coverage_stage` 设为 `"initial"`（自动同步工作区描述）。
     - **`thesis`**：写入报告第七节的 3–5 条初始 thesis 支柱（每条含主张、当前证据，以及 `confirm` / `warn` / `break` 可观测阈值）；首批证据台账条目来自初筛报告引用（确保 `source.type` 与默认 `source.tier` 对应：filing=1、company_release=2、transcript=2、broker_report=3、consensus=3、official_data=4、expert=5、internal_note=5(3–5)、news=6、model=2–3 且须引用 `model_cell` 并禁标 tier 1）；写入 `house_view` 初值（`reason` ≥30 字）。
     - **`debate`**：写入报告第四节的 2–4 个核心多空分歧（分歧变量、Bull、Bear、最强反驳、我的判断、改判条件、来源）。
     - **`drivers`**：写入行业支线识别的长短期驱动变量、2–3 个可跟踪高频指标、宏观敏感度，以及估值支线的“历史上是 EPS 驱动还是倍数驱动”分解结论。
     - **`management`**：写入管理层背景、资本配置与指引风格的初步记录（显式标注“初筛记录，待完整覆盖 C4 12 季表核实”）。
     - **`agenda`**：写入报告第八节的研究议程——完整覆盖需要回答的 3–5 个关键问题、下一项验证事项与日期（`next_validations`）、待观察催化剂事件与日期（`watch.events`）。

2. **写入首条结论 `knowledge_append_verdict`**
   - 调用 `knowledge_append_verdict` 记录一条 `NEW_THESIS`（新论点）：
     - `trigger.kind`：PM 指派填 `"manual"`，研究总监排程填 `"director"`；
     - 附上 `next_validation`（what + due）与 `progress`。

3. **开启公司自动跟踪 `dalton_coverage_tracking`**
   - 知识层与首条结论落库后，必须调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 开启该公司的四项标准自动跟踪任务（`watch`、`delta`、`move-up`、`move-down`）。
   - 若用户之前曾手动关闭过跟踪（任务元数据含 `paused_by_user`），`source="coverage_finish"` 会自动跳过重新开启并保持关闭状态，不要强制覆盖用户选择。

4. **汇报与可选发布**
   - 可调用 `check_draft(draft_path="drafts/<D>-initial-coverage.md")` 检查数字核验反馈。
   - 向 PM 汇报核心判断（≤3 句）、12 问重点题结论、估值分解结果、初始倾向（关注 / 观察 / 暂不覆盖 / 建议推进完整覆盖）、研究议程及自动跟踪状态。
   - 初次覆盖默认保留为草稿；若 PM 明确要求正式发布，调用 `publish_report(draft_path="drafts/<D>-initial-coverage.md", title="...", kind="initial_coverage", slug="...")`。

5. **完成判定（必查清单）**
   - `drafts/<D>-initial-coverage.md` 已完成独立审稿与 P0 修订；
   - `company`、`thesis`、`debate`、`drivers`、`management`、`agenda` 均已通过 `knowledge_update` 写入，且已记录首条 `NEW_THESIS` `knowledge_append_verdict`；
   - 已调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 并检查确认自动跟踪已开启（`status="enabled"`，或因 `paused_by_user=true` 保持用户手动关闭状态）。

---

## 四、本 Skill 目录文件

- `references/branch-industry.md`：阶段 1B 行业与竞争支线的 8 条取证与分析规则。
- `references/branch-valuation-history.md`：阶段 1D 估值历史分解（`ln(P1/P0) = ln(EPS1/EPS0) + ln(PE1/PE0)`）与可比表规则。
- `references/screen-12q.md`：买方初筛 12 问（Q1–Q12）答题标准与重点题要求。
- `templates/initial-coverage.md`：初次覆盖报告中文输出模板。
