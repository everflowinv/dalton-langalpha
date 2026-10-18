# 试跑记录：`<样本编号>-<公司>-<YYYY-MM-DD>`

## 1. 运行元数据

| 字段 | 记录值 |
|---|---|
| **样本编号** | `<如 P2-1 / B1 / P3-1 / P4-2 / P5-1>` |
| **交付物（Skill / Workflow）** | `<如 initial-coverage / dalton-bullbear / full-coverage>` |
| **目标公司 / 场景** | `<TICKER（公司名）或专题名称>` |
| **触发方式** | `<斜杠命令手动触发 / once 自动任务 / cron 自动任务 / price 触发 / 研究总监派单>` |
| **工作区 ID** | `<workspace_id 或 home>` |
| **线程 ID（Thread ID）** | `<thread_id>` |
| **Workflow Run ID** | `<run_task_id（若调用了 RunWorkflow，否则填 N/A）>` |
| **使用模型（Manifest 键）** | `<如 claude-opus-5-5-oauth / ocb-openai-gpt-6.1-sol>` |
| **草稿期审稿模型** | `<如 ocb-openai-gpt-6.1-sol（不同家族）或 N/A>` |
| **开始 / 结束时间（耗时）** | `<YYYY-MM-DD HH:MM:SS → HH:MM:SS（共 X 分 Y 秒）>` |
| **子智能体派发状态** | `<共 N 次派发，成功 M 次，失败列表：无>` |
| **本轮花费（USD）** | `$<get_run_charge_usd(run_ref) 返回值>` |
| **事件结论 ID 与五词** | `<VD-YYYYMMDD-NN｜NO_CHANGE / THESIS_STRENGTHENED / THESIS_WEAKENED / THESIS_BROKEN / NEW_THESIS>` |

## 2. 产物路径与确定性检查

- **主草稿 / 报告路径**：`<如 drafts/2026-10-18-initial-coverage.md>`
- **支线与中间材料目录**：`<如 research/2026-10-18-ic/>`
- **模型路径（如有）**：`<如 model/DASH-model-v01.xlsx>`
- **正式发布路径（如有）**：`<如 published/2026-10-18-full-coverage-dash/>`
- **自动检查结果**：
  - [ ] 抽查 5 个核心数字 100% 回指原件或工具结果（无编造一致预期或目标价）
  - [ ] 脚本检查通过（如 `bb_check.py 0 WARN` / `validate_memo.py 0 FAIL` / `validate_topic.py PASS` / `recalc.py & audit.py --strict OK`）
  - [ ] 知识层更新完成且与报告一致（`thesis.md`、`debate.md`、`drivers.md`、`management.md`、`agenda.md`、`verdicts.md`）
  - [ ] 收尾三步执行完毕（`knowledge_append_verdict` 含五词 + `next_validation` + `progress`）

## 3. PM / Lead 质量评估（PS.18 六维评分表）

> 硬门槛：抽查 5 个数字全部对得上原件或工具结果；无编造的一致预期、目标价；`S >= 2`。
> “可用”标准：该交付物适用的维度全部 `>= 2`。
> “优秀”标准：适用维度平均 `>= 2.5`，且至少一个维度为 `3`。

| 代号 | 维度 | 是否适用 | 得分（0–3） | 评分依据与具体摘录 |
|---|---|---|---|---|
| **M** | 业务机制（怎么赚钱、增量从哪来、什么在变） | 是 / 否 | | |
| **D** | 分歧（真分歧、我们站哪边、置信度） | 是 / 否 | | |
| **E** | 预期差（与市场定价差异量化、错误定价原因） | 是 / 否 | | |
| **C** | 驱动链（变量 → 财务 → 估值链条与敏感度） | 是 / 否 | | |
| **F** | 证伪条件（可观察、带阈值或检查日期） | 是 / 否 | | |
| **S** | 来源（数字可回指、层级恰当、同源去重） | **必选** | | |
| **综合** | **平均分 / 评级结论** | — | **`X.XX`（不通过 / 可用 / 优秀）** | **盲评对照结果：`<优于对照 / 持平 / 逊于对照 / 单份评分>`** |

## 4. 抽查的 5 个数字对账明细

| # | 报告中出现的句子与数字 | 报告标注来源 | 原件 / 工具返回核实值 | 结果（PASS/FAIL） |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |

## 5. 发现的问题与迭代闭环

1. **本次发现的问题**：
   - 问题描述：
   - 根因定位（提示词 / references / 脚本 / 数据源）：
   - 已落地修改（具体文件路径与 diff 摘要）：
2. **上次试跑问题是否已解决**：
   - `<首次试跑填 N/A；复跑逐条核对上次问题是否消除>`
