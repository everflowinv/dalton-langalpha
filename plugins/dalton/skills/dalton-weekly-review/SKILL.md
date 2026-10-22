---
name: dalton-weekly-review
description: "在 Home 对全覆盖名单做周度异动筛选及盈利修正与估值归因，对核心覆盖逐家五源深挖、观察名单按命中定向检索，汇总一周观点变化与投资建议。用于每周复盘全组合覆盖标的的边际变化与投资机会；属于跨公司多支线深度工作流，耗时较长。"
---

# Dalton 覆盖周报与周会启动器（`dalton-weekly-review`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 运行位置与职责边界

1. **先读 `run-workflow`**：执行本 skill 第一步必须先读取 `.agents/skills/run-workflow/SKILL.md` 以解锁 `RunWorkflow` 工具。
2. **在 Home 运行**：周会是跨公司的全局复盘，在 `Home` 工作区由研究总监（Chief of Staff）运行（对应每周末自动任务 `dalton-director-weekly.json`）。
3. **只读兄弟工作区、只写周会目录**：
   - 子智能体通过相对路径读取各公司工作区的知识层（如 `../META/thesis.md`、`../META/debate.md`、`../META/verdicts.md`、`../META/data/consensus/`；批量概览可先调 `dalton_coverage_board`）；
   - 所有过程产物只写入 `Home/dalton/research/weekly-<end>/`，最终周会稿写入 `Home/dalton/drafts/<end>-weekly.md`；
   - **严禁直接跨工作区修改各公司的知识层文件**：对各公司知识层（如 `drivers` 的周度股价驱动记录、`debate`、`thesis`）的修改建议，一律写成提案文件 `Home/dalton/research/weekly-<end>/proposals/<TICKER>.json`。

---

## 执行流程

### 第 1 步：确定窗口终点与路径

1. **窗口定义**：
   - `end`：最近一个完整交易日收盘日期（`YYYY-MM-DD`）；
   - `base`：上周同一工作日收盘日期（默认 `end` 往前 7–8 个自然日，对齐 1W 完整交易周）；
   - 港股、A 股或美股休市日在周会稿开头注明，休市市场不参与当周横截面比较；港股与 A 股覆盖标的全量接入深度检索（除内部研究库、AlphaEngine、新闻与 X 外，统一通过 `researchdata.finance(route="cn-hk", operation="filings"|"ask", ...)` 检索巨潮/港股公告、资金流、两融、龙虎榜、回购分红与 AH 溢价，港股披露易未覆盖时用 `web_search` 查 HKEXnews 原文链接；平台财务表仅作二手初值，核心财务数回公告核实，回报核对复权、分红、拆股与币种）。
2. **覆盖名单路径**：`Home/dalton/coverage.yaml`（工作区内相对路径 `dalton/coverage.yaml`）。

### 第 2 步：启动 `weekly-coverage-review` Workflow

调用 `RunWorkflow` 运行预置的 `weekly-coverage-review` 工作流：

```json
{
  "workflow": "weekly-coverage-review",
  "params": {
    "home_dir": "dalton",
    "end": "YYYY-MM-DD",
    "coverage_path": "dalton/coverage.yaml"
  }
}
```

#### Workflow 内部四阶段概览（当 `workflow.enabled` 关闭时按此顺序手动执行）：
1. **阶段 1：筛选与归因（`筛选`）**：
   - 运行 `.agents/skills/dalton-weekly-review/scripts/screen_movers.py` 与 `.agents/skills/dalton-weekly-review/scripts/attribution.py`；
   - 输出 `dalton/research/weekly-<end>/screen.json` 与 `attribution.json`；
   - **数据完备性门禁**：若 `screen.json` 返回 `ok: false`（超过一半美股缺少最新一根日线），立即中止周会并说明“最新交易日行情尚未就绪，不下结论”。
2. **阶段 2：Core 逐家并行五源深挖（`Core 逐家`）**：
   - 持仓 Core 排最前；每家派一个 `dalton-evidence` 子智能体（遵守“一次只查一家公司”纪律，预算受平台单次运行 64 个子智能体上限约束）；
   - 按 `.agents/skills/dalton-weekly-review/references/core-five-sources.md` 检索五类来源，结合 `screen.json` 与 `attribution.json` 写出 `dalton/research/weekly-<end>/core-<TICKER>.md`；
   - 若有知识层更新建议（含本周 EPS vs 倍数归因写入 `drivers`），输出 `dalton/research/weekly-<end>/proposals/<TICKER>.json`。
3. **阶段 3：Watch 命中定向检索（`Watch 命中`）**：
   - 只对阶段 1 命中的 Watch 公司派 `dalton-evidence` 子智能体（超出 64 槽位上限的余下命中公司写进“口径与缺口”，下次优先）；
   - 按 `.agents/skills/dalton-weekly-review/references/watch-rules.md` 检索并写 `dalton/research/weekly-<end>/watch-<TICKER>.md`。
4. **阶段 4：汇总与 Conviction Call（`汇总与 call`）**：
   - 对 Core 中标记 `call_candidate: true` 的公司（最多 3 家）按 `.agents/skills/dalton-call/SKILL.md` 做证据门槛与三情景 IRR 核对，输出 `call-<TICKER>.md`（不过门槛即输出“暂无高 conviction”及缺口）；
   - 按 `.agents/skills/dalton-weekly-review/templates/weekly.md` 汇总全部材料、各公司本周 `verdicts.md` 与 `proposals/`，生成周会稿 `dalton/drafts/<end>-weekly.md`。

### 第 3 步：提案交办（`delegate_to_analyst`）与议程批注（`dalton_agenda_note`）

Workflow 完成后，研究总监在 Home 执行收尾动作：
1. **交办知识层修改提案**：
   - 遍历 `dalton/research/weekly-<end>/proposals/*.json`；
   - 对每家有提案的公司，调用 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)` 派单给该公司工作区分析师（无人值守运行已开启 `auto_approve_handoffs`，不会卡在确认卡上），指令分析师审阅提案并调用 `knowledge_update` / `knowledge_append_verdict` 落库。
2. **追加重点议程备注**：
   - 对本周出现重大异动、证据缺口或需要下周重点跟踪的问题，调用研究总监工具 `dalton_agenda_note` 追加到对应公司的 `agenda` 中。
3. **统计与推送 PM**：
   - 统计全覆盖公司本周事件五词（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）分布；
   - 将周会稿中的**“本周判断（3–6 条）”**与**“Conviction call（或本周暂无高 conviction）”**推送给 PM；
   - 周会稿保留在 `Home/dalton/drafts/<end>-weekly.md` 供 PM 周会阅读（Home 不调 `publish_report`；若某条 conviction call 需作为正式报告发布，由对应公司分析师在公司工作区走 `dalton-call` 的 `publish_report(kind="conviction_call")`）。
