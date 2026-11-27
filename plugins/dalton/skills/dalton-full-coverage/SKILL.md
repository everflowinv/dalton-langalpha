---
name: dalton-full-coverage
description: "完成一手数字底表、四支线并行取证、15 页动态公式 Excel 三表与 DCF 模型、深度投资备忘录（一位写手成稿）及双审稿发布。对重点标的开展立项级深度建仓研究、需要完整估值模型与正式备忘录时使用；属于最重度完整深度研究。"
---

# Dalton 买方完整覆盖（`dalton-full-coverage`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 是 Dalton 买方完整覆盖（P3 深度覆盖阶段）的启动与发布控制器。目标是针对已完成初次覆盖（或 PM 直接指定深度覆盖）的标的，一次运行产出：
1. **数字底表（`research/<D>-fc/facts.md`）**：含 `<!-- FACTS-LEDGER ... -->` 机器可读账本，作为全文唯一目标公司财务数字来源与发布核验基准；
2. **公司专属 12 问研究提纲（`research/<D>-fc/questions.md`）** 与 **A/B/C/D 四条取证支线底稿（`ea.md`、`eb.md`、`ec.md`、`ed.md`）**；
3. **带动态公式的 Excel 财务与估值模型（`model/<TICKER>-model-v<NN>.xlsx`）**：通过共享建模流水线产出；工作区已有用户模型或历史版本时，按 `.agents/skills/dalton-model/SKILL.md` 保持原件只读，在原模型上受控原地滚动维护并另存为下一版，补齐预测期及 `Facts`（数字底表镜像）、`Guidance vs Actual`（12 季指引兑现表）、`Street vs House`（本方与市场预期对比表）、`Checks`（勾稽检查表）；尚无模型时按默认模板与 `references/model-spec.md` 的 15 页规格新建，完成一致预期对照、本轮范围检查与最多两轮修改回路，在 `research/<D>-fc/model_dump/` 落盘终态账本与检查报告；
4. **深度投资备忘录（`drafts/<D>-memo.md`）**：写给基金经理看（摘要 → 公司与商业模式 → 行业与竞争〔可并入上一节〕→ 业务质量 → 核心投资逻辑与争议 → 财务预测与估值 → 风险 → 管理层 → 跟踪 → 附录 → 引用），12 问研究提纲（含每问 `A / B / C / D` 自评档位）留在 `questions.md` 工作底稿、不进正文；
5. **独立审稿（`dalton-reviewer`）与共享报告改模修订**：修订备忘录时若需联动改模，统一走共享报告改模入口并追加 `report_revision` 账本轮次，完成后更新公司知识层并经 `publish_report(kind="full_coverage", updates_thesis=True)` 提交正式发布审批。

---

## 一、输入参数确认

启动前确认以下参数（可从当前工作区 `agent.md`、`company.md` 及 `../Home/dalton/coverage.yaml` 读取默认值）：

- `ticker`：规范股票代码（如 `META`、`MSFT`）
- `company`：公司名称
- `market`：市场（支持 `US`、`HK`、`CN`；<!-- DALTON_CAP:researchdata_bridge -->港股与 A 股的一手公告与财务初值通过 `researchdata.finance(route="cn-hk", operation="filings"|"ask", ...)` 获取，平台财务表仅作二手初值线索，核心财务数字必须回法定公告原文核实，港股披露易未覆盖时用 `web_search` 查 `site:hkexnews.hk` 原文链接<!-- DALTON_CAP_ELSE -->港股与 A 股的一手公告与核心财务数字通过 `web_search` 查港交所披露易（`site:hkexnews.hk`）、巨潮资讯及交易所官网法定公告原文核实<!-- /DALTON_CAP -->；回报与估值核对复权、分红、拆股与币种）
- `workspace_dir`：当前公司工作区路径
- `as_of`：基准日期（`YYYY-MM-DD`；可不传或传 `"latest"`，`RunWorkflow` 会自动取最近一个美股交易日，`resume` 续跑时自动沿用上一 run 的值）
- `profile`：`"tob"`（面向企业）或 `"toc"`（面向消费者），决定 D 支线做从业者信号表还是多平台产品体验表
- `form`：`"10-K"`（美国本土发行人）、`"20-F"`（外国私人发行人）或 `"annual_report"`（港股/A 股年度报告）
- `pm_brief`（可选）：PM 的具体研究指示或假设
- `focus_questions`（可选）：PM 要求重点深挖的问题列表

### 开始前：检查未完成的 run（中断续跑）

- 读 `research/<D>-fc/.progress.json`（及 `research/` 下其他 `*/.progress.json`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="full-coverage", params={...原参数..., "progress_path": "research/<D>-fc/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。若前一次 run 因审稿 `block` 等原因提前停止，补齐问题后同样用**原参数**加 `resume` 重跑（workflow 返回值里已提供 `resume_hint`）。
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
       "as_of": "<YYYY-MM-DD 或 latest，可不传>",
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
   - **阶段 0.8（问题清单 frame）**：按 `references/frame.md` 撰写 `research/<D>-fc/questions.md`（含每问 `A / B / C / D` 初始自评档位），并跑 `python .agents/skills/dalton-full-coverage/scripts/research_structure.py validate-agenda research/<D>-fc/questions.md` 直至通过。
   - **阶段 1（四条取证支线并行）**：
     - `ea.md`：按 `references/branch-a-industry.md`（行业定义与双法 TAM、5–8 家竞对现取财务与份额、周期高频指标、全球对标、政策、行业专用框架）；
     - `eb.md`：按 `references/branch-b-sellside-expert.md`（<!-- DALTON_CAP:alphaengine|researchdata_bridge|guidepoint -->AlphaEngine 分类检索与全文精读、卖方预期地图、内部库与 company-wiki、Guidepoint 英文专家纪要、卖方盲点<!-- DALTON_CAP_ELSE -->公开卖方研报摘要与预期地图、公开行业专家与访谈纪要、卖方盲点<!-- /DALTON_CAP -->）；
     - `ec.md`：按 `references/branch-c-primary.md`（4 期报告 MD&A/Risk Factors diff、4 期电话会措辞漂移、招股书承诺对照、**C4 过去 12 个季度指引 vs 实际可信度表**、8-K 与内部人交易）；
     - `ed.md`：按 `references/branch-d-offstreet.md`（X、雪球、员工评价、社区情绪、招聘信号、13F、ToC 产品体验表或 ToB 从业者信号表）。
   - **阶段 2（建模流水线，串行在最前）**：调用共享建模流水线（`.agents/skills/dalton-model/SKILL.md` 与 `references/model-spec.md`）产出模型至 `model/<TICKER>-model-v<NN>.xlsx`：先经 `model_fingerprint.py` 提取指纹，再由 `model_edit.py` 受控原地编辑、落盘前原子恢复母本缓存并由 LibreOffice 临时副本复算写回；先独立预测再用 `model_consensus.py` 写独立快照区并生成差异表；经 `model_check.py check` 对本轮改动范围做四分类检查，仅「本轮可修」交回修改（最多两轮），机器报告与终态账本 `model-verification.json` 统一落盘免语言审查目录 `research/<D>-fc/model_dump/`。凡产出可读候选（`pass` 或 `delivered_with_open_issues`）均顺畅进入写作，仅无可读候选时停在成稿之前。
   - **阶段 3（单一写手成稿与第 1 轮语言审查）**：一位写手通读 `questions.md`（12 问研究提纲）、四条支线底稿（含文末逐题证据小结）、`facts.md` 与模型输出，同步更新 `questions.md` 每问 `A / B / C / D` 自评档位（只写在底稿、不进备忘录正文），并按 `references/memo-writing.md` 一次写成整篇备忘录（成稿后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处） → 跑 `validate_memo.py --mode full` 到 0 FAIL → 第 1 轮语言审查（`check_language` round=1，按清单修改）→ 复跑校验。
   - **阶段 4（独立审稿）**：派 `dalton-reviewer` 审阅备忘录（连同底表、支线底稿、最终模型与 `model_dump/` 下已生成的检查报告）。
   - **阶段 5（共享改模、一轮修订、终稿语言审查与知识层修订草案）**：审稿 `revise` 时，先经共享报告改模入口处理联动改模（`model_edit.py` 另存下一版并保缓存复算、`model_consensus.py diff` 刷新差异表、`model_check.py check --kind report_revision --append-revision` 单遍检查并追加账本轮次；超时则由短子任务 `model_check.py write-ledger` 写降级账本，同步更新 `.memo-model.json`），再结合实存模型报告修订备忘录并复审；随后对给人读的底稿笔记（`facts.md`、`questions.md`、`ea.md`–`ed.md`，跳过 `model_dump/`）与终稿备忘录完成第 2 轮语言审查，并生成 `research/<D>-fc/knowledge-revision.json`。

3. **Workflow 不可用时的手动降级路径**
   - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按阶段 0 → 0.8 → 1 → 2 → 3 → 4 → 5 的顺序手动完成各阶段产物与脚本校验（语言审查两轮、修订一轮，顺序同阶段 3–5；成稿和每次修订后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处）。建模阶段依次运行 `model_fingerprint.py`、`model_edit.py`、`model_consensus.py`，首查运行 `python3 .agents/skills/dalton-model/scripts/model_check.py check --workspace . --ticker <TICKER> --research-dir research/<D>-fc --candidate <当前候选路径> --baseline <起点模型或默认模板> --fingerprint research/<D>-fc/model_dump/<TICKER>-fingerprint.json --coord-map research/<D>-fc/model_dump/coord_map.json --workflow full-coverage --mode <build|update> --fix-round 0` 落盘 `research/<D>-fc/model_dump/model-verification.json`；定点修复后用同一组完整参数执行复查（递增 `--fix-round`）：`python3 .agents/skills/dalton-model/scripts/model_check.py check --workspace . --ticker <TICKER> --research-dir research/<D>-fc --candidate <当前候选路径> --baseline <起点模型或默认模板> --fingerprint research/<D>-fc/model_dump/<TICKER>-fingerprint.json --coord-map research/<D>-fc/model_dump/coord_map.json --workflow full-coverage --mode <build|update> --fix-round <1|2>`；审稿修订联动改模后运行 `python3 .agents/skills/dalton-model/scripts/model_check.py check --workspace . --ticker <TICKER> --research-dir research/<D>-fc --candidate <新候选路径> --baseline <上一版本路径> --fingerprint research/<D>-fc/model_dump/<TICKER>-rev-fingerprint.json --coord-map research/<D>-fc/model_dump/coord_map.json --workflow full-coverage --mode <build|update> --kind report_revision --append-revision`；若检查或复算异常，运行 `python3 .agents/skills/dalton-model/scripts/model_check.py write-ledger --workspace . --ticker <TICKER> --research-dir research/<D>-fc --candidate <当前候选路径> --baseline <起点模型或默认模板> --kind <modeling|report_revision> --failed-stages <失败阶段列表，如 check,consensus> --workflow full-coverage --mode <build|update>` 补写降级账本（手动路径省略 `--run-id`）。派 `dalton-reviewer` 审稿（含修订后复审）时：（1）先算 `drafts/<D>-memo.md` 的 sha256；（2）把该哈希写入审稿提示词；（3）将返回的 JSON（`verdict` / `summary` / `items` 逐项覆盖 1–9 项检查 / `must_fix` / `nice_to_have`，审稿人不回填任何哈希）保存为 `research/<D>-fc/review.json`，并写入 `draft_sha256` 字段（复审时覆盖为最新一轮；改稿后必须重新计算哈希、重新审稿）。备忘录主稿语言审查两轮：成稿并跑 `validate_memo.py --mode full` 到 0 FAIL 后调用 `check_language(draft_path=<草稿>, round=1)` 按清单修改（只改语言，不动事实、数字、术语、引用编号与判断强度），复跑校验，再派 `dalton-reviewer` 首次审稿；一轮修订结束后对终稿调用 `check_language(round=2)` 完成终稿语言修改，草稿有改动就再跑一次 round=2 复核（记录追加在同一 json），然后做最后一次审稿（pass 的 JSON 存 review.json）。`research/<D>-fc/` 下给人读的底稿笔记（`facts.md`、`questions.md`、`ea.md`–`ed.md`）各做两轮 `check_language`（第 1 轮按清单改，第 2 轮复核，有改动则再跑一次 round=2 确认）；跳过 `model_dump/` 下的机器报告、json/xlsx 文件与 `.audit/.annotated/.language` 侧车。收尾按第三节第 5 步以 `manual: true` 调用 `dalton_check_done`。**在途的旧结构运行**（备忘录还是「零、12 问 + 一至十章」的旧版双轨稿）：按旧规则完成并收尾——`validate_memo.py` 会自动识别旧结构并按旧规则校验（`--part` 仍可用），不要把旧稿改写成新结构。
   - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

## 三、主智能体收尾：知识层更新、数字核验与正式发布

Workflow 返回后，主智能体按以下 6 步完成收尾（完成契约在完成判定与向 PM 汇报"完成"之前）：

### 第 1 步：检查审稿结论与机械门禁
- 读取 workflow 返回状态、审稿意见与 `validate_memo.py --mode full` 结果。
- 若审稿结论为“不得发布”或仍有未解决的 P0 缺陷，**停留在草稿状态**，向 PM 汇报具体原因与补证计划，不发起 `publish_report`。

### 第 2 步：展示并写入知识层修订（`knowledge_update` + `knowledge_append_verdict`）
- 读取 `research/<D>-fc/knowledge-revision.json`，将核心认知改动（`旧值 → 新值 → 原因与证据编号`）展示给 PM（无人值守运行时直接写入工作版本），然后逐个 doc 调用 `knowledge_update`：
  - **`thesis`**：先写证据台账再定稿——更新支柱、确认/警告/证伪阈值、追加证据台账行（`source.type` 与 `source.tier` 对应；每条 `evidence` 必填 `figures` 数组，每项含 `metric`〔中文或英文指标名〕、`value`、`unit`〔枚举值 `"USD"` / `"USD_mn"` / `"USD_bn"` / `"HKD"` / `"HKD_mn"` / `"HKD_bn"` / `"CNY"` / `"CNY_mn"` / `"CNY_bn"` / `"CNY_100mn"` / `"pct"` / `"bp"` / `"x"` / `"count"` / `"count_mn"` / `"per_share"` / `"other"`，百分比用 `"pct"`〕、`period`、`label`〔仅限 `"reported"` / `"company_guidance"` / `"consensus"` / `"estimate"` / `"assumption"` / `"research_judgement"`〕，且 `source.ref` 指向一手文件或 `prov`，严禁指向本次草稿 `drafts/<D>-memo.md`）、更新 `house_view`（**硬约束**：若给出目标价，`house_view.target` 必须带 `model_ref.kind="model_cell"` 指向 Excel 模型单元格）；
  - **`debate`**：更新完整覆盖后的核心多空分歧、倾向、置信度与改判条件；
  - **`drivers`**：更新核心业务驱动树变量、模型敏感度与高频跟踪指标；
  - **`management`**：写入 C4 支线整理的 12 季指引兑现历史（`guidance_history`）与指引风格定性结论（`guidance_style`：保守/中性/激进、收入还是利润更保守、全年修正节奏）；
  - **`lessons`**：若完整覆盖推翻了初次覆盖中的某项早期判断，记录一条经验教训（错误类型、适用条件、反例与证据）；
  - **`models`**：按 `.agents/skills/dalton-model/SKILL.md` 先调用 `knowledge_read(doc="models")` 取 `latest_version_no`，再调用 `knowledge_update(doc="models", ...)` 登记本次最终模型路径（取 workflow 返回的 `model` 字段，与 `research/<D>-fc/model_dump/model-verification.json` 终态一致）、`created_at`、`reason`、`recalculated` 与关键输出单元格（`key_outputs`），并将 `current` 指向最新版本号；若模型终态为「带问题交付」，在向 PM 汇报的回复最顶部原样列出 `model_dump/remaining-issues.md` 内容；
  - **`company`**：将 `coverage_stage` 更新为 `"full"`。
- 调用 **`knowledge_append_verdict`**：
  - 对照初次覆盖的 thesis 给出正式五词结论（通常为 `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `NEW_THESIS` / `THESIS_BROKEN` / `NO_CHANGE`）；
  - 在 `agenda_ops` 中写入完整覆盖后的持续跟踪议程（跟踪节的验证点与失效条件、高频指标与催化剂日历）。

### 第 3 步：开启自动跟踪（`dalton_coverage_tracking`）
- 调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 开启（或补齐）该公司的标准自动跟踪任务（`watch`、`delta`、`move-up`、`move-down`）。若用户之前手动关闭过（带 `paused_by_user` 标记），`source="coverage_finish"` 会自动保持关闭状态，不重新打开。

### 第 4 步：`check_draft` 预检与 `publish_report` 正式发布
1. 确认报告末尾 `## 引用` 表存在且正文中每个 `[引用N]` 都能在表里找到（指向 `f:`、`evidence:`、`model:` 或 `prov:`），调用 `check_draft(draft_path="drafts/<D>-memo.md")` 查看数字核验反馈（生成 `drafts/<D>-memo.audit.md`），修复所有未匹配或无来源的数字。
2. 调用 `publish_report` 发起正式发布：
   ```json
   {
     "draft_path": "drafts/<D>-memo.md",
     "title": "<公司名>（<TICKER>）深度投资备忘录",
     "kind": "full_coverage",
     "slug": "<ticker-lower>-full-coverage-<D>",
     "attachments": [
       {"path": "<本次最终模型路径，如 model/<TICKER>-model-v<NN>.xlsx>", "role": "model"},
       {"path": "research/<D>-fc/facts.md", "role": "facts"}
     ],
     "updates_thesis": true
   }
   ```
   - **注意**：`attachments` 同时附带本次最终模型文件（`role: "model"`，取 workflow 返回的 `model` 字段或 `model/` 下最新版本）和数字底表 `research/<D>-fc/facts.md`（`role: "facts"`，作为发布核验的对账基准）；其余支线文件与草稿审稿意见留在 `research/`，不进发布包。复用同一基准日（`as_of`）已有备忘录前，先核对正文引用的模型路径及哈希与本次最终模型一致；若模型已更新，按新模型同步更新受影响的预测、估值与引用并重审后再调用 `publish_report`。
   - 若核验退回或服务端独立审稿返回 `revise`，逐条修订后再调 `publish_report`，直至进入 PM 审批卡（`awaiting_pm`）。

### 第 5 步：汇报完成前必须过完成契约（`dalton_check_done`）

- 向 PM 汇报"完成"前，必须调用 `dalton_check_done(workflow="full-coverage", run_id=<`research/<D>-fc/.progress.json` 里的 run_id>, params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>"})`（与启动 `RunWorkflow` 时的参数一致）。
- 走手动降级路径（`workflow.enabled` 已关闭、没有 run）时不传 `run_id`，调用 `dalton_check_done(workflow="full-coverage", params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>", "manual": true})`，审稿结论按 `research/<D>-fc/review.json` 核对。
- `done=false` 时按 `missing` 逐条补齐（缺文件补文件、`facts.py check`/`validate_memo`/模型文件可读性核对未通过时修到通过、知识层缺 source=tool 新版本就 `knowledge_update`、check_draft 用了旧草稿就对最终稿重跑 `check_draft`），补完再查一次。
- 补不了的缺项只能汇报"未完成"，列出缺项与原因；没有 `dalton_check_done` 通过，不得汇报"完成"。

### 第 6 步：完成判定（必查清单）
- `drafts/<D>-memo.md`、本次最终模型文件（`model/<TICKER>-model-v<NN>.xlsx`）及 `research/<D>-fc/facts.md` 均已通过校验；
- 知识层（含 `company.coverage_stage="full"`）与 `knowledge_append_verdict` 已落库；
- 已调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 并检查确认自动跟踪已开启（`status="enabled"`，或因 `paused_by_user=true` 保持关闭）；
- `publish_report` 已提交进入 PM 审批卡（`awaiting_pm`），PM 在审批卡片上批准后，发布管线自动将 `thesis.official`（含正式 house view）写入知识层，并在 `published/` 下生成不可变正式副本。

---

## 四、本 Skill 目录文件

- `references/facts-ledger.md`：阶段 0 数字底表（`FACTS-LEDGER`）规则与必填键清单。
- `references/frame.md`：阶段 0.8 公司专属 12 问问题清单（`questions.md`，内部工作底稿）规则。
- `references/branch-common.md`：A/B/C/D 四条取证支线通用规则。
- `references/branch-a-industry.md`：支线 A（行业与竞争）取证规则。
- `references/branch-b-sellside-expert.md`：支线 B（卖方<!-- DALTON_CAP:researchdata_bridge -->、内部库<!-- /DALTON_CAP -->与专家访谈）取证规则。
- `references/branch-c-primary.md`：支线 C（一手文件精读与 C4 12 季指引可信度表）取证规则。
- `references/branch-d-offstreet.md`：支线 D（场外信号、产品体验与从业者反馈）取证规则。
- `references/memo-writing.md`：备忘录写作目标、章节结构与各节要求、引用与数字纪律、写作与校验流程。
- `references/model-spec.md`：完整覆盖 Excel 模型规格（在原模型上滚动维护与 15 页新建规格，统一遵循 `dalton-model` 规范）。
- `scripts/research_structure.py`：12 问映射表、问题清单校验（`validate-agenda`）与备忘录结构校验（新稿按章节结构；旧版十二章在途稿按旧规则）。
- `scripts/facts.py`：`facts.md` 中 `FACTS-LEDGER` 的命令行读写与必填键检查工具。
- `scripts/validate_memo.py`：备忘录机械校验脚本（新稿查章节结构与引用闭环；旧版十二章在途稿按旧规则校验、`--part` 仍可用）。
- `scripts/test_research_structure.py`：12 问结构与备忘录章节离线回归测试套件。
- `templates/memo.md`：投资备忘录中文模板。
- `templates/questions.md`：公司专属 12 问研究提纲中文模板（内部工作底稿）。
