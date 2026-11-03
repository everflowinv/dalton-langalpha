---
name: dalton-initial-coverage
description: "并行执行公司背景、行业竞争、三把财务镜头与估值历史四条支线，产出一份买方 12 问初筛报告并建立公司初始知识层。将新标的纳入覆盖、需要在深入建模前全面评估商业质量与多空焦点时使用；属于多支线并行深度研究，耗时较长。"
---

# Dalton 买方初次覆盖（`dalton-initial-coverage`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 是 Dalton 买方初次覆盖（初筛阶段）的启动与收尾控制器。目标是围绕一家新覆盖公司，一次运行产出 **内部 12 问底稿**（`research/<as_of>-ic/questions.md`）+ **面向 PM 阅读的初次覆盖报告**（`drafts/<as_of>-initial-coverage.md`，按章节展开，正文不逐题列 Q1–Q12、每件事只写一遍）+ **初始化的公司知识层**（`company`、`thesis`〔含 `underwriting` 赔率卡〕、`debate`、`drivers`、`management`、`agenda` 与首条 `NEW_THESIS` 结论），回答买方初筛最核心的问题：主营靠什么赚钱、行业特性与周期、长短期驱动、竞争位置与壁垒（正在形成还是正在被破坏）、历史股价是 EPS 驱动还是倍数驱动、当前 bull/bear debate、股价位置、叙事演绎与预期差（知识层用 `stance`〔方向〕+ `conviction`〔确信度〕表达，正文用自然语言写清方向、确信度、行动分类、限制因素与双向改判条件，并交付三情景赔率卡与三层保护）。

---

## 一、输入参数确认

启动前确认以下参数（若 `../Home/dalton/coverage.yaml` 或当前工作区已提供则直接读取，无需重复追问）：

- `ticker`：规范股票代码（如 `META`、`APP`）
- `company`：公司名称
- `market`：市场（支持 `US`、`HK`、`CN`；港股与 A 股通过 `researchdata.finance(route="cn-hk", operation="filings"|"ask", ...)` 获取法定公告、财务表初值、行情、资金流、两融、股东与 AH 溢价——平台财务表仅作二手初值线索，核心财务数字必须回法定公告原文核实，港股披露易未覆盖时用 `web_search` 查 `site:hkexnews.hk` 原文链接，回报与估值核对复权、分红、拆股与交易/报表币种）
- `workspace_dir`：当前公司工作区根目录（如 `.` 或工作区路径）
- `as_of`：基准日期（`YYYY-MM-DD`；可不传或传 `"latest"`，`RunWorkflow` 会自动取最近一个美股交易日，`resume` 续跑时自动沿用上一 run 的值）
- `peers`（可选）：可比同业代码列表；不提供时由行业支线与估值支线自行选定 5–8 家竞争对手及 ≥3 家一档可比公司，并写明选取理由
- `sector`（可选）：所属行业名称
- `pm_question`（可选）：PM 特别指定的关注问题或假设

### 开始前：检查未完成的 run（中断续跑）

- 读 `research/<D>-ic/.progress.json`（及 `research/` 下其他 `*/.progress.json`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="initial-coverage", params={...原参数..., "progress_path": "research/<D>-ic/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。若前一次 run 因审稿 `block` 等原因提前停止，补齐问题后同样用**原参数**加 `resume` 重跑（workflow 返回值里已提供 `resume_hint`）。
- 全新启动时也在 `params` 里带上 `"progress_path": "research/<D>-ic/.progress.json"`，让中断后的 run 可被发现与续跑。

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
       "as_of": "<YYYY-MM-DD 或 latest，可不传>",
       "peers": ["<可选同业>"],
       "sector": "<可选行业>",
       "pm_question": "<可选PM问题>",
       "progress_path": "research/<D>-ic/.progress.json"
     }
   }
   ```
   `initial-coverage` workflow 按以下阶段自动推进（估值支线拆分为取数与写稿两步，取证结束后额外执行一次支线产物核验派发，并包含语言审查、审稿修订与终态产物核验等多次子智能体派发）：
   - **阶段 1（并行 4 支线取证 + 支线核验）**：
     - **A 公司背景**：按 `.agents/skills/dalton-company-background/SKILL.md` 输出 `drafts/<D>-background.md`；
     - **B 行业支线**：按 `references/branch-industry.md` 输出 `research/<D>-ic/industry.md`；
     - **C 财务镜头**：按 `.agents/skills/dalton-financial-lenses/SKILL.md` 输出 `research/<D>-ic/lenses.json` 与 `drafts/<D>-lenses.md`；
     - **D 估值历史与可比（拆成取数 + 写稿两步派发）**：先按 `.agents/skills/comps-analysis/SKILL.md` 取数并生成可比表 `research/<D>-ic/comps.xlsx`，再按 `references/branch-valuation-history.md` 撰写估值历史与对数分解稿 `research/<D>-ic/valuation.md`；
     - **支线核验**：四条支线取证完成后，多一次支线核验派发，核对各支线产物是否存在并计算哈希。
   - **阶段 2（bull/bear 快速版，含叙事收敛）**：综合 A–D 支线产物与内部库/AlphaEngine 检索，按 `.agents/skills/dalton-bullbear/SKILL.md` 快速版方法输出 `research/<D>-ic/bullbear-quick.md`；**首次覆盖时的叙事收敛按该 skill 快速版流程第 1 步执行**（先列全市场叙事、删减到 2–3 个决定价值的驱动变量〔driver〕、每个驱动变量再往下挖一层观察指标与数据来源），收敛结果写入主要分歧、第 3 阶段的知识层 `debate` / `drivers` 草案，以及第 4 阶段的内部 12 问底稿与初次覆盖报告。
   - **阶段 3（知识层初始化草案 + 写手成稿前正式写入并锁定赔率卡版本）**：若本次运行开始时盘上已有在途旧结构草稿（由启动时的 `草稿探测` 最后一条已完成记录的原文匹配判定），则跳过成稿前的本阶段与暂停点（改在成稿后抽取草案）。否则，从阶段 1–2 的支线材料与 `bullbear-quick.md` 抽取结构化知识层操作草案 `research/<D>-ic/knowledge-proposal.json`（`thesis` 含 `underwriting` 三情景赔率卡，字段写法照 `src/dalton/knowledge/schemas/thesis.schema.json` 与 `plugins/dalton/skills/dalton-knowledge/references/structured-write-example.md`；每条 `thesis.evidence` 必填结构化 `figures` 数组，且 `source.ref` 指向一手文件或 `prov`）。因工作流子智能体无 `knowledge_update` 工具，若盘上尚无第一行写有固定格式 `已保存 thesis 版本 N` 的 `research/<D>-ic/underwriting-summary.md`，工作流在此暂停并返回 `resume_hint`，主智能体按以下顺序执行：
     1. **先正式写入依赖文档**：对 `knowledge-proposal.json` 中不依赖报告数字的 `agenda`、`debate`、`company`、`drivers`、`management` 先调用 `knowledge_update(..., dry_run=True)` 校验、通过后以 `dry_run=False` **正式写入**，确保 `thesis` 中 `trigger_refs`（如 `V-001`）与 `debate_ids` / `driver_ids` 等跨文档引用能通过数据库校验；
     2. **再对 `thesis` 先试写检查、通过后正式写入**：先调用 `knowledge_update(doc="thesis", ..., dry_run=True)` 校验（若第一行写着 `没有保存成功` 或 `不能写进正文`，必须先修正 `knowledge-proposal.json` 重试），通过后立即调用 `knowledge_update(doc="thesis", ..., dry_run=False)` **正式写入**数据库；
     3. **按固定格式写赔率摘要文件并续跑**：从 `knowledge_update(doc="thesis", ..., dry_run=False)` 返回中取出版本号 `N`，按固定格式写入 `research/<D>-ic/underwriting-summary.md`——第一行固定写 `已保存 thesis 版本 N`，摘要文件首行之后只贴正式保存后的赔率摘要，不要贴试写的返回；若 `house_view.stance == "no_view"` 且 `underwriting` 为空，第一行固定写 `已保存 thesis 版本 N；暂不表态，不计算赔率`。写完再用原参数加 `resume=<.progress.json 里的 run_id>` 续跑工作流。
   - **阶段 4（一位写手完成内部 12 问底稿与初次覆盖报告成稿）**：
     - **在途旧结构草稿**：若本次运行开始时盘上已有在途旧结构草稿，保留旧结构继续完善 `drafts/<D>-initial-coverage.md`，不补写 `research/<D>-ic/questions.md` 与 `research/<D>-ic/underwriting-summary.md`，返回 JSON 中 `underwriting_version` 固定填 `1`。
     - **新结构成稿顺序与返回字段**：由一位写手先按 `references/screen-12q.md` 与 `templates/questions.md` 写出内部 12 问底稿 `research/<D>-ic/questions.md`（逐题回答 Q1–Q12 并标明归属章节），再直接采用 `research/<D>-ic/underwriting-summary.md` 中已锁定的赔率数字，按 `templates/initial-coverage.md` 与 `plugins/dalton/skills/dalton-conventions/references/writing.md` 一次写成初次覆盖报告 `drafts/<D>-initial-coverage.md`。返回 JSON 中将赔率摘要首行的版本号填入必填字段 `underwriting_version`，并在 `files` 中声明 `["drafts/<D>-initial-coverage.md", "research/<D>-ic/questions.md", "research/<D>-ic/underwriting-summary.md"]`。
     - **正文结构要求**：第一个二级标题必须是 `## 摘要`（或 `## 一页摘要`、`## Summary`）。`## 摘要` 不用粗体标签列清单，表前用四到六段连续论证写清：第一段直接给出结论、方向、行动分类及收盘价、市值与流动性；随后几段依次讲清生意怎么赚钱、利润率为什么在这个水平、长期趋势和短期噪音怎么区分（哪些变量能持续跟踪、哪些短期波动不比市场多预判）、观察价/买点区间为什么由业务质量上限和相对同业估值上限决定；接着放紧凑三情景赔率表、赔率结论与双向改判条件。其余章节覆盖 `## 公司与商业模式`、`## 核心投资逻辑与主要分歧`（每个分歧用两三段连贯的业务机制论证写完，不用“多方 / 空方 / 我们的判断”三段对称清单；单列 `### 交易角度` 小节；`### 核心投资支柱与验证阈值` 小节只写每个支柱的确认、预警、证伪阈值并指明对应哪条分歧，不重复展开论据）、`## 估值与赔率`（写清观察价/买点区间的业务质量上限与相对估值上限依据，且正文退出倍数判断与赔率表一致）、`## 风险`、`## 管理层与资本配置`、`## 研究议程与跟踪`，按需设 `## 附注`，文末为单一 `## 引用`。
     - **正文文风与数字纪律**：正文严禁逐条列 Q1–Q12；每段先用短句给判断，一句话的事实数字一般不超过两个；同一组同业倍数比较全文只在一处完整展开；三情景的价格、倍数、回报统一用紧凑小表呈现；`[假设]` 写在假设数字所在的那一句或那一格里：核验按句（句末标点或换行断开，见 `dalton-conventions` 的 `writing.md` 第 4 节）或按表格单元格豁免，标记不会传到下一句或相邻的格子，所以不要只在段前或表前写一次；口径、来源分歧、未取得的数据放进 `## 附注`；正文不出现英文字段名、枚举值与内部代号，常见买方英文术语直接用且不建术语替换表。
   - **阶段 5（两轮语言审查、独立审稿、一轮修订与三者版本一致核对）**：
     1. **首轮语言审查与独立审稿**：成稿先经第 1 轮 `check_language`，再由 `dalton-reviewer` 审阅 `questions.md` 的重点题（Q1–Q5、Q7、Q8、Q12）及报告正文的章节完整性与不重复、壁垒判断、估值分解表、赔率数字与 `underwriting-summary.md` 一致性，以及“`## 引用` 表存在且每个 `[引用N]` 都能在表里找到”（审稿子任务在 `files` 中完整声明检查过的草稿、`questions.md` 与 `underwriting-summary.md`）。
     2. **一轮修订（当审稿结论为 `"revise"` 时）**：执行一轮修订并同步更新 `questions.md` 与 `knowledge-proposal.json`（在途旧稿豁免同步 `questions.md`）。修订子任务必须在 `files` 中声明读取过的 `underwriting-summary.md`，并在返回 JSON 中填写必填字段 `underwriting_version`（当前摘要首行版本号）与 `needs_reunderwrite`（`true` / `false`）：
        - **无需改赔率**：改好正文与底稿后将 `needs_reunderwrite` 设为 `false`，直接进入复审；
        - **需要改赔率（EPS、倍数、持有期、权重等）**：先将更新后的赔率参数写入 `knowledge-proposal.json`，暂不改动报告正文中的赔率数字，将 `needs_reunderwrite` 设为 `true`。**工作流在修订之后、复审之前立刻停下**，由主智能体先把 `thesis` 引用到的非 `thesis` 依赖文档（如 `agenda`、`debate`）有变化的部分以 `dry_run=False` 正式写入，再对 `thesis` 先 `dry_run=True` 校验、后 `dry_run=False` 正式写入新版本，按固定格式更新 `underwriting-summary.md`（第一行固定写 `已保存 thesis 版本 N`，首行之后只贴正式保存后的赔率摘要，不要贴试写的返回）后用原参数加 `resume` 续跑；续跑时声明了 `underwriting-summary.md` 的成稿与审稿修订阶段自动按新摘要重跑，将正文数字同步到新版本、把 `needs_reunderwrite` 设为 `false` 并记录新的 `underwriting_version` 后进入复审。
     3. **笔记语言审查、第 2 轮正文语言审查与终稿审稿**：复审通过后，依次执行支线与底稿笔记的两轮语言审查、报告正文第 2 轮 `check_language`，再由 `赔率摘要复核` 核对最终稿记录的 `underwriting_version` 等于 `underwriting-summary.md` 首行版本号（不一致则暂停，待同步后 `resume`），通过后执行终稿审稿与产物核验（含 `underwriting-summary.md`）。

3. **Workflow 不可用时的手动降级路径**
   - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按上述阶段 1→5 的顺序手动执行（依次派发 `dalton-evidence` / `general-purpose` 子智能体或亲自执行）：
     1. **阶段 1–3（取证、知识层正式落库与赔率摘要）**：完成阶段 1–2 取证后生成 `research/<D>-ic/knowledge-proposal.json`；先对 `agenda`、`debate`、`company`、`drivers`、`management` 调用 `knowledge_update(..., dry_run=True)` 校验、通过后以 `dry_run=False` 正式写入，再对 `thesis` 先 `dry_run=True` 校验、通过后以 `dry_run=False` 正式写入，按固定格式写入 `research/<D>-ic/underwriting-summary.md`（第一行固定写 `已保存 thesis 版本 N`，`house_view.stance="no_view"` 时写 `已保存 thesis 版本 N；暂不表态，不计算赔率`，首行之后只贴正式保存后的赔率摘要，不要贴试写的返回）。
     2. **阶段 4 与第 1 轮语言审查**：完成 `research/<D>-ic/questions.md` 与 `drafts/<D>-initial-coverage.md` 成稿后，先调用 `check_language(draft_path="drafts/<D>-initial-coverage.md", round=1)` 并按 `<草稿>.language.md` 清单修改语言（只改语言表达，不动事实、数字、术语、引用编号与判断强度）。
     3. **阶段 5 独立审稿与一轮修订**：每次派 `dalton-reviewer` 审稿均按三步执行——（1）先计算当前草稿的 sha256（`sha256sum drafts/<D>-initial-coverage.md` 或等价的 Python `hashlib.sha256`）；（2）把该哈希写进派给 `dalton-reviewer` 的提示词；（3）将返回的 JSON（`verdict`、`summary`、`items` 逐项覆盖 1–9 项检查、`must_fix`、`nice_to_have`，审稿人不回填任何哈希）保存到 `research/<D>-ic/review.json`，保存时把第（1）步算出的被审草稿 sha256 写入 `draft_sha256` 字段。若首次审稿结论为 `"revise"`，执行一轮修订（若需改赔率输入，先把有变化的 `agenda`、`debate`、`company`、`drivers`、`management` 以 `dry_run=False` 正式写入，再对 `thesis` 先 `dry_run=True` 校验、后 `dry_run=False` 正式写入新版本，然后更新 `underwriting-summary.md` 与正文赔率数字），改稿后重新计算草稿哈希并派 `dalton-reviewer` 复审。
     4. **笔记语言审查、第 2 轮正文语言审查与终稿审稿**：本次产出的其他给人读的 `.md` 笔记（`drafts/<D>-background.md`、`drafts/<D>-lenses.md` 及 `research/<D>-ic/` 下的 `industry.md`、`valuation.md`、`bullbear-quick.md`、`questions.md`）各审两轮 `check_language`（第 1 轮审完按清单改，第 2 轮复核，改过就再调一次 `round=2` 确认；`underwriting-summary.md`、`.json`/`.xlsx` 等机器文件、`bg_data/`、`data/`、`lenses_work/`、`model_dump/`、知识层渲染文件及 `.audit`/`.annotated`/`.language` 附属文件不审）。报告正文定稿后调用 `check_language(round=2)` 完成终稿语言修改，若草稿有改动则再调用一次 `check_language(round=2)` 复核，随后补一次终稿审稿（将结论为 `"pass"` 的 JSON 覆盖存入 `research/<D>-ic/review.json`；若第 2 轮语言审查未改动草稿且已有绑定当前哈希的 `"pass"` 记录则无需重审）。
     5. **数字核验与完成检查**：调用 `check_draft(draft_path="drafts/<D>-initial-coverage.md")`，收尾调用 `dalton_check_done(workflow="initial-coverage", params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>", "manual": true})`（不传 `run_id`）。
   - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

## 三、知识层核对与收尾（Workflow 结束后主智能体必做）

> 子智能体没有 `knowledge_update` 与 `knowledge_append_verdict` 工具。新结构运行中，`company`、`thesis`、`debate`、`drivers`、`management`、`agenda` 已在阶段 3 暂停点（及按需在修订阶段 `needs_reunderwrite=true` 暂停点）通过 `knowledge_update(..., dry_run=False)` 正式落库，`research/<D>-ic/underwriting-summary.md` 第一行已按固定格式记录 `已保存 thesis 版本 N`。Workflow 跑完后，主智能体核对数据库里的 `thesis` 最新有效版本与 `underwriting-summary.md` 第一行的版本号 `N` 一致（若修订阶段更新了其他非 `thesis` 文档或在途旧稿尚未落库，则按 `research/<D>-ic/knowledge-proposal.json` 补齐正式写入，但严禁在终稿审稿后再生成新的 `thesis` 版本以免与 `underwriting-summary.md` 版本号脱节）。

1. **知识层六个文档的字段与正式写入标准（阶段 3 暂停点及按需补齐时遵循，先 `dry_run=True` 确认，再 `dry_run=False` 正式提交）**
   - 对 `knowledge-proposal.json` 里的每个文档，先调用 `knowledge_update(..., dry_run=True)` 校验并预览 diff 与期望结构，全部通过后再以 `dry_run=False` 正式写入；新工作区文档尚不存在时传 `base_version=null` 并使用 `create` 操作；已有文档时使用 `insert` / `append` / `set`：
     - **`company`**：写入一句话业务判断、所属行业、是否周期股，并将 `coverage_stage` 设为 `"initial"`（自动同步工作区描述）。
     - **`thesis`**：写入 3–5 条初始投资支柱（与报告「核心投资逻辑与主要分歧」节一致，每条含主张、当前证据，以及 `confirm` / `warn` / `break` 可观测阈值）；写入 `underwriting` 三情景赔率卡与三层保护（字段结构与写法照 `src/dalton/knowledge/schemas/thesis.schema.json` 与 `plugins/dalton/skills/dalton-knowledge/references/structured-write-example.md`，正式写入后将其返回的赔率摘要存入 `research/<D>-ic/underwriting-summary.md`）；写入首批 `evidence` 台账条目，**每条 `evidence` 必填 `figures` 数组**（每个事实数字一项，必填 `metric`〔中文或英文指标名〕、`value`、`unit`〔枚举值 `"USD"` / `"USD_mn"` / `"USD_bn"` / `"HKD"` / `"HKD_mn"` / `"HKD_bn"` / `"CNY"` / `"CNY_mn"` / `"CNY_bn"` / `"CNY_100mn"` / `"pct"` / `"bp"` / `"x"` / `"count"` / `"count_mn"` / `"per_share"` / `"other"`，百分比用 `"pct"`〕、`period`、`label`〔仅限 `"reported"` / `"company_guidance"` / `"consensus"` / `"estimate"` / `"assumption"` / `"research_judgement"`〕），且 **`source.ref` 必须指向一手文件或 `prov` 记录**（`kind="filing"` / `"provenance"` / `"model_cell"` / `"url"` 或 `research/` 下的支线原始文件，严禁指向本次初筛草稿 `drafts/<D>-initial-coverage.md`）；确保 `source.type` 与默认 `source.tier` 对应（`"filing"`=1、`"company_release"`=2、`"transcript"`=2、`"broker_report"`=3、`"consensus"`=3、`"official_data"`=4、`"expert"`=5、`"internal_note"`=5(3–5)、`"news"`=6、`"model"`=2–3 且须引用 `"model_cell"` 并禁标 tier 1）；写入 `house_view` 初值（`reason` ≥30 字）。`dalton_check_done` 完成检查对 `thesis` 只核对版本号，不比较任何时间或来源：读取 `underwriting-summary.md` 第一行的 `已保存 thesis 版本 N`，核对数据库里该工作区 `thesis` 最新的有效版本必须等于 `N`，且新结构下这个版本必须有 `underwriting`，或者 `house_view.stance="no_view"` 且 `underwriting` 为空（按数据库判断）。
     - **`debate`**：写入阶段 2 收敛并与报告「核心投资逻辑与主要分歧」节一致的 2–4 个核心多空分歧（分歧变量、Bull、Bear、最强反驳、我的判断、改判条件、来源）。
     - **`drivers`**：写入行业支线识别的长短期驱动变量、2–3 个可跟踪高频指标、宏观敏感度，以及估值支线的“历史上是 EPS 驱动还是倍数驱动”分解结论。
     - **`management`**：写入管理层背景、资本配置与指引风格的初步记录（显式标注“初筛记录，待后续深入研究核实过去 12 个季度的指引兑现记录”）。
     - **`agenda`**：写入与报告「研究议程与跟踪」节一致的研究议程——后续深入研究需要回答的 3–5 个关键问题、下一项验证事项与日期（`next_validations`）、待观察催化剂事件与日期（`watch.events`）。

2. **写入首条结论 `knowledge_append_verdict`**
   - 调用 `knowledge_append_verdict` 记录一条 `NEW_THESIS`（新论点）：
     - `trigger.kind`：PM 指派填 `"manual"`，研究总监排程填 `"director"`；
     - 附上 `next_validation`（what + due）与 `progress`。

3. **开启公司自动跟踪 `dalton_coverage_tracking`**
   - 知识层与首条结论落库后，必须调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 开启该公司的四项标准自动跟踪任务（`watch`、`delta`、`move-up`、`move-down`）。
   - 若用户之前曾手动关闭过跟踪（任务元数据含 `paused_by_user`），传入 `"coverage_finish"` 会自动跳过重新开启并保持关闭状态，不要强制覆盖用户选择。

4. **汇报完成前必须过完成契约（`dalton_check_done`）**
   - 向 PM 汇报"完成"前，必须调用 `dalton_check_done(workflow="initial-coverage", run_id=<`research/<D>-ic/.progress.json` 里的 run_id>, params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>"})`（与启动 `RunWorkflow` 时的参数一致）。
   - 走手动降级路径（`workflow.enabled` 已关闭、没有 run）时不传 `run_id`，调用 `dalton_check_done(workflow="initial-coverage", params={"ticker": "<TICKER>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>", "manual": true})`，审稿结论按 `research/<D>-ic/review.json` 核对。
   - `done=false` 时按 `missing` 逐条补齐（缺文件补文件、校验未过修到过、知识层缺 source=tool 新版本或 `thesis.underwriting` 未落库就调用 `knowledge_update`、`check_draft` 用了旧草稿就对最终稿重跑 `check_draft`），补完再查一次。
   - 补不了的缺项只能汇报"未完成"，列出缺项与原因；没有 `dalton_check_done` 通过，不得汇报"完成"。

5. **完成判定（必查清单）**
   - `research/<D>-ic/questions.md`（内部 12 问底稿）、`research/<D>-ic/underwriting-summary.md`（正式落库的赔率卡摘要，首行固定为 `已保存 thesis 版本 N` 且与服务端数据库 `thesis` 最新有效版本一致）与 `drafts/<D>-initial-coverage.md`（初次覆盖报告正文）已完成语言审查、独立审稿与一轮修订；
   - `## 引用` 表存在且每个 `[引用N]` 都能在表里找到（每行同时含 `evidence:` / `model:` / `prov:` / `f:` 核验前缀与括号内来源说明）；
   - `company`、`thesis`（含 `underwriting` 赔率卡及带完整 `figures` 且 `source.ref` 指向一手文件或 `prov` 的 `evidence` 台账）、`debate`、`drivers`、`management`、`agenda` 均已通过 `knowledge_update` 写入，且已记录首条 `NEW_THESIS` `knowledge_append_verdict`；
   - 已调用 `dalton_coverage_tracking(action="enable", source="coverage_finish")` 并检查确认自动跟踪已开启（返回状态为 `"enabled"`，或因 `paused_by_user=true` 保持用户手动关闭状态）。

6. **汇报与可选发布**
   - 确认报告末尾合并为单一 `## 引用` 节，每个 `[引用N]` 都能在表里找到（形如 `[引用1]: evidence:E-20261023-01 （法定财报｜Meta Platforms｜FY2025 10-K｜2026-02-01｜accession 0001326801-26-000012）` 或 `[引用3]: model:model/META-model-v01.xlsx!DCF!C12 （本方模型｜DCF 估值页核心假设）`），且终稿已通过 `check_draft(draft_path="drafts/<D>-initial-coverage.md")`。
   - 向 PM 汇报核心判断（≤3 句）、三情景赔率卡要点、主要多空分歧与估值分解结论、初始建议、研究议程及自动跟踪状态。
   - 初次覆盖默认保留为草稿；若 PM 明确要求正式发布，调用 `publish_report(draft_path="drafts/<D>-initial-coverage.md", title="...", kind="initial_coverage", slug="...")`。

---

## 四、本 Skill 目录文件

- `references/branch-industry.md`：阶段 1B 行业与竞争支线的 8 条取证与分析规则。
- `references/branch-valuation-history.md`：阶段 1D 估值历史分解（`ln(P1/P0) = ln(EPS1/EPS0) + ln(PE1/PE0)`）与可比表规则。
- `references/screen-12q.md`：初次覆盖内部 12 问底稿（Q1–Q12）答题标准与报告正文写作规则。
- `templates/questions.md`：内部 12 问底稿（`research/<D>-ic/questions.md`）输出模板。
- `templates/initial-coverage.md`：初次覆盖报告（`drafts/<D>-initial-coverage.md`）中文输出模板。
