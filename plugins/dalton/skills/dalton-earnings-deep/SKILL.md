---
name: dalton-earnings-deep
description: "财报后并行产出八节深度点评报告与更新后的 Excel 财务模型，逐条对账财报前瞻预测、校准核心论点支柱并更新管理层指引兑现记录。卖方点评与电话会到齐后需要完整复盘季报并滚动模型与认知时使用；属于完整深度研究工作流。"
---

# Dalton 财报深度链与八节点评（`dalton-earnings-deep`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

---

## 独立八节点评模式（用户明确只要八节点评）

用户明确要求“只写八节深度点评报告、不跑完整 workflow”时，不调用 `RunWorkflow`（与 `RunWorkflow` 是否可用、`workflow.enabled` 是否开启无关）：主智能体在当前会话内按下述 Step 2 的四个阶段顺序直接执行，并逐阶段核对产物。其余情况一律走下面的深度链 workflow。

---

## 第一步：加载 Workflow 工具与确认运行模式

1. **必读 `run-workflow`**：执行任何操作前，先用 Read 工具读 `.agents/skills/run-workflow/SKILL.md` 了解 workflow 规范（workflow 启用时，加载本 skill 会自动解锁 `RunWorkflow`）。
2. **深度链 workflow**：
   - **默认（`earnings-deep-update` workflow）**：当由 PS.7 创建的 `tp1` 自动任务触发（T+1 交易日 20:00，此时卖方财报后报告已基本到齐）或用户要求完整财报后更新时，调用 `RunWorkflow(workflow="earnings-deep-update", params={...})`。
   - **Workflow 不可用时**：
     - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按本 skill 下述四个阶段的顺序手动执行，并逐阶段核对产物。阶段 3 派 `dalton-reviewer` 审稿按三步执行：（1）主智能体先计算草稿的 sha256（`sha256sum drafts/<D>-deep-<quarter>.md` 或等价的 Python `hashlib.sha256`）；（2）把该哈希写进派给 `dalton-reviewer` 的提示词；（3）要求审稿人在返回的 JSON 里原样回填 `draft_sha256`，并将返回的 JSON（含 `verdict` 与 `draft_sha256`）原样保存到 `research/<D>-deep-<quarter>/review.json`（多轮复审时保存最后一轮；改稿后重新计算哈希、重审并覆盖）。收尾调用 `dalton_check_done(workflow="earnings-deep-update", params={"ticker": "<TICKER>", "quarter": "<quarter>", "workspace_dir": "<当前工作区路径>", "release_date": "<D>", "manual": true})`（不传 `run_id`），审稿据此文件核对。语言审查两轮（顺序不可颠倒，与 workflow 相同）：成稿后先调用 `check_language(draft_path=<草稿>, round=1)` 并按 `<草稿>.language.md` 清单修改（只改语言，不动事实、数字、术语、引用编号与判断强度），再派 `dalton-reviewer` 首次审稿；修订循环结束、终稿内容定稿后调用 `check_language(round=2)` 按清单完成终稿语言修改，草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核，然后做最后一次审稿（pass 的 JSON 存 review.json）与 `check_draft`；第一次审稿就 pass 时：第 2 轮照常执行；改了稿就补一次终稿审稿（pass 的 JSON 覆盖存 review.json），哈希未变则无需重审。本次产出中其他给人读的 .md 笔记（research/ 支线、背景、快讯、异动、公告点评、专题章节等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
     - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

## 开始前：检查未完成的 run（中断续跑）

- 读 `research/<D>-deep-<quarter>/.progress.json`（及 `research/` 下其他 `*/.progress.json`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="earnings-deep-update", params={...原参数..., "progress_path": "research/<D>-deep-<quarter>/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。
- 全新启动时也在 `params` 里带上 `"progress_path": "research/<D>-deep-<quarter>/.progress.json"`，让中断后的 run 可被发现与续跑。

## Step 1：组装参数并启动 `earnings-deep-update` Workflow

在公司工作区收集以下参数后调用 `RunWorkflow(workflow="earnings-deep-update", params=...)`：

| 参数名 | 含义与取值规则 |
|---|---|
| `ticker` | 股票代码（如 `"META"`、`"NVDA"`） |
| `company` | 公司名称（如 `"Meta Platforms"`） |
| `quarter` | 公司财季标签（如 `"FY26Q3"`，与 filing、电话会及快讯一致） |
| `workspace_dir` | 当前公司工作区目录路径（如 `"."` 或工作区绝对/相对路径） |
| `release_date` | 官方财报发布日期 `YYYY-MM-DD`（如 `"2026-10-28"`） |
| `model_path` | 从 `data/models.json` 读取当前登记的最新 Excel 模型路径（如 `"model/META-model-v01.xlsx"`）；**若该公司尚无模型，传 `null`**（workflow 会自动跳过模型更新支线，仅执行点评、对账与校准） |
| `preview_predictions` | 前瞻预测文件路径（如 `"research/2026-10-23-preview-FY26Q3/predictions.json"`）；若本次财报前无前瞻预测文件，传 `null` |
| `progress_path` | 固定传 `"research/<D>-deep-<quarter>/.progress.json"`（`<D>` 即 `release_date`），让中断后的 run 可被发现与续跑 |
| `sector_overlay` | 若标的属于半导体/硬件行业（NVDA、AVGO、AMD、MRVL、TSM、ASML、MU、QCOM、ARM、INTC、TXN、LRCX、AMAT、KLAC、ADI、ON、MCHP、SMCI、ANET、CRDO、ALAB 等），传 `"hardware"` 以叠加 [`references/hardware-overlay.md`](references/hardware-overlay.md)；其他行业传 `null` |

---

## Step 2：四阶段执行内容（Workflow 内部支线 / 独立回退顺序）

> 以下阶段由 `earnings-deep-update` workflow 执行。仅当 `workflow.enabled` 已关闭，或处于上面的独立八节点评模式时，才由主智能体按这些阶段手动执行。

### 阶段 1：八节深度点评 ∥ 模型更新（并行）

1. **支线 A —— 八节深度点评（必跑）**：
   - 严格按 [`references/eight-sections.md`](references/eight-sections.md)（硬件股叠加 [`references/hardware-overlay.md`](references/hardware-overlay.md)）与 [`templates/deep.md`](templates/deep.md) 撰写中文深度报告至 `drafts/<D>-deep-<quarter>.md`。
   - **价格反应与真实 52 周高低**：必须运行 `scripts/price_reaction.py` 获取盘后、次日、一周的绝对涨跌与相对 `QQQ`、`SPY`、板块 ETF（硬件股必加 `SOXX`/`SMH`）的超额表现，以及基于**真实 252 个交易日**的 52 周高低位置：
     ```bash
     python3 .agents/skills/dalton-earnings-deep/scripts/price_reaction.py \
       --ticker META --release-date 2026-10-28 --timing AMC \
       --benchmarks QQQ,SPY --sector-etf XLC --markdown
     ```
   - **卖方与独立研究窗口**：检索发布时刻至发布后 3 个交易日内的 AlphaEngine 卖方报告、FUNDA AI 与 TMTB 点评，以及 `[T0, T0+24h)` 的 `sales-notes`。
   - **证据层级与四类信息标注**：美股遵守 `10-Q/10-K > 8-K EX-99.1 > 新闻稿 > 演示稿 > 电话会` 优先级；港股与 A 股通过 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=..., end=..., limit=20)` 调取法定定期报告与业绩公告（港股未覆盖时用 `web_search` 查披露易 `site:hkexnews.hk` 原文链接），`operation="ask"` 平台财务表仅作二手初值，核心数字一律回公告核实，并核对复权、分红、拆股与交易/报表币种；只在电话会口头提及的指引标 `call-only guidance`；所有内容严格区分 **事实 / 管理层指引 / 市场预期 / 研究判断**。

2. **支线 B —— Excel 模型更新（`model_path` 非空时运行）**：
   - 先读 `.agents/skills/model-update/SKILL.md`，触发类型设为 `Results`。
   - 先输出披露行项与模型行项的映射表到 `research/<D>-deep-<quarter>/mapping.md`。
   - 将当季实际值写入并调整远期驱动假设，**另存为新版本 `model/<TICKER>-model-v<NN+1>.xlsx`（严禁覆盖旧版本）**，完整填写 `Change Log`。
   - 运行模型重算（`recalc.py`）与严格审计（`audit.py --strict`），确保 `Checks` 全数 `OK`，并输出新旧版本 EPS walk 与估值对比至 `research/<D>-deep-<quarter>/model-delta.md`。

### 阶段 2：前瞻对账与 Thesis 财报后校准

严格按 [`references/calibration.md`](references/calibration.md) 产出校准文件，并将核心表格合入 `drafts/<D>-deep-<quarter>.md` 末尾的“前瞻对账”与“thesis 校准”章节：

1. **前瞻逐条对账（`reconcile.md` 与 `reconcile.json`）**：
   - 运行 `scripts/reconcile.py` 对 `predictions.json` 的全部条目逐条比对实际结果：
     ```bash
     python3 .agents/skills/dalton-earnings-deep/scripts/reconcile.py \
       --predictions research/<preview_dir>/predictions.json \
       --actuals research/<D>-deep-<quarter>/actuals.json \
       --output research/<D>-deep-<quarter>/reconcile.md
     ```
   - 每条预测明确判定 **`对` / `错` / `部分`**，凡判为“错”或“部分”的写明具体错因（`数据错` / `模型错` / `判断错` / `意外事件`）并生成 `lessons` 候选；同时将错判与部分命中的计数写入 `research/<D>-deep-<quarter>/reconcile.json`（格式为 `{"wrong": <错的条数>, "partial": <部分的条数>}`）。若本次财报前无 `predictions.json`，在 `reconcile.md` 与报告中说明“本次财报前无 predictions.json，跳过前瞻对账”，并将 `research/<D>-deep-<quarter>/reconcile.json` 写为 `{"wrong": 0, "partial": 0}`。
2. **Thesis 财报后校准（`thesis-calibration.md`）**：
   - 先读 `.agents/skills/thesis-tracker/SKILL.md`（`post-earnings` 模式），先拆解盈利质量（税率/回购带来的 EPS beat 不得将经营支柱判为 confirming），再对 `thesis.md` 每个支柱给出信号词（`strongly confirming` … `invalidating` / `untested`）与状态词（`strengthening` / `intact` / `watch` / `impaired` / `broken` / `changed` / `untested` / `retired`），并逐一排查五种漂移模式（有/无）。
3. **管理层指引兑现记录更新（`management-delta.md`）**：
   - 对比本季实际表现与上一季给出的官方指引中点/区间，记录偏差幅度与方向，判断管理层指引保守/激进风格是否延续。

### 阶段 3：草稿期审稿与生成 `knowledge-revision.json`

1. **草稿期审稿（`dalton-reviewer`）**：审阅 `drafts/<D>-deep-<quarter>.md` 及 `research/<D>-deep-<quarter>/` 下的对账与校准文件（核查 beat/miss 口径、“综合判断”是否有独立判断、thesis 信号是否被股价涨跌替代、对账是否诚实面对错判，以及“`## 引用` 表存在且每个 `[引用N]` 都能在表里找到”）。手动路径下先计算草稿 `sha256` 写进审稿提示词，要求审稿人原样回填 `draft_sha256`，将返回的 JSON 存为 `research/<D>-deep-<quarter>/review.json`。
2. **生成知识层修订草案**：汇总全部产出与审稿意见，按 PK.2 Schema 生成 `research/<D>-deep-<quarter>/knowledge-revision.json`。

---

## Step 3：主智能体收尾 —— 知识层落库、终判与推送（必做）

Workflow 结束后（或独立模式完成后），由当前主智能体读取 `research/<D>-deep-<quarter>/knowledge-revision.json`，执行以下收尾动作（深度链通常在无人值守自动运行，**只修改知识层工作版本，不动 `thesis.official`**）：

1. **调用 `knowledge_update` 逐项更新知识层**：
   - `doc="thesis"`：先写证据台账再定稿——更新各支柱信号、状态与新证据行（每条 `evidence` 必填 `figures` 数组，含 `metric`、`period`、`value`、`unit`、`label`，且 `source.ref` 指向一手文件或 `prov`，严禁指向本次草稿；若草稿期审稿缺席 `review_done == false`，非 `NO_CHANGE` 结论注明“待审”，暂不直接下调核心支柱状态）；
   - `doc="debate"`：更新本次财报验证后的多空分歧状态；
   - `doc="drivers"`：追加当季核心财务与经营 KPI 最新实际读数；
   - `doc="management"`：补填 `guidance_history[].actual` 与 `outcome`；若连续两季同类指标同向偏差，更新 `guidance_style` / `credibility_notes`；
   - `doc="lessons"`：将前瞻对账中“错/部分”条目的错因与适用边界追加为经验教训；
   - `doc="models"`：若有新模型版本通过 `Checks`，登记新模型路径与版本说明；**若模型更新失败（`Checks` 未过），严禁静默跳过**，必须在修订说明中写明“模型未更新”并在 `agenda_ops` 排入人工跟进待办。
2. **调用 `knowledge_append_verdict` 写入事件五词终判与议程**：
   - 写入财报终判（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`，`trigger.kind="earnings"`，`trigger.ref` 填 `tp1` 信号 id 或 `"manual"`）；
   - 在 `agenda_ops` 中写入下一季需要跟踪验证的事项；
   - **重大异常升级（PM Escalation）**：若终判为 `THESIS_BROKEN` 或任一核心支柱状态变为 `impaired` / `broken`，必须立即通过通知渠道推送 PM，并在 `agenda_ops` 中写入一条 `pm_handoffs`（PK.14）。
3. **草稿核验（`check_draft`）**：
   - 末尾合并为单一 `## 引用` 节：确认报告末尾的 `## 引用` 表存在且每个 `[引用N]` 都能在表里找到（每行一条写出核验目标与括号内来源说明，形如 `[引用1]: evidence:E-20261023-01 （法定财报｜SEC 10-Q｜3Q26 季报｜2026-10-28｜accession ...）` 或 `[引用5]: model:model/META-model-v01.xlsx!DCF!C12 （本方模型｜DCF 估值页）`）；
   - 调用 `check_draft` 生成数字核验报告 `drafts/<D>-deep-<quarter>.audit.md`；
4. **汇报完成前必须过完成契约（`dalton_check_done`）**：
   - 调用 `dalton_check_done(workflow="earnings-deep-update", run_id=<`research/<D>-deep-<quarter>/.progress.json` 里的 run_id>, params={"ticker": "<TICKER>", "quarter": "<quarter>", "workspace_dir": "<当前工作区路径>", "release_date": "<D>", "model_path": "<模型路径或 null>"})`（与启动时的参数一致）；
   - 走手动降级路径（`workflow.enabled` 已关闭、没有 run）时不传 `run_id`，调用 `dalton_check_done(workflow="earnings-deep-update", params={"ticker": "<TICKER>", "quarter": "<quarter>", "workspace_dir": "<当前工作区路径>", "release_date": "<D>", "model_path": "<模型路径或 null>", "manual": true})`，审稿结论按 `research/<D>-deep-<quarter>/review.json` 核对；
   - `done=false` 时按 `missing` 先补齐再查（缺文件补文件、知识层缺 source=tool 新版本就 `knowledge_update`、check_draft 用了旧草稿就对最终稿重跑），补不了的缺项只能汇报"未完成"并列出缺项与原因，不得汇报"完成"。
5. **发布/推送（完成契约通过后）**：
   - 当 PM 要求正式发布时，调用 `publish_report(draft_path="drafts/<D>-deep-<quarter>.md", title="<公司名>（<TICKER>）<quarter> 财报深度点评", kind="earnings_review", slug="<ticker-lower>-deep-<quarter-lower>-<D>", attachments=[{"path": "model/<TICKER>-model-v<NN>.xlsx", "role": "model"}])`（无模型时 `attachments` 传 `[]`）；
   - 将深度报告核心结论、前瞻对账得分、模型变动及 thesis 校准摘要推送给 PM。
