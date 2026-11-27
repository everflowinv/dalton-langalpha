---
name: dalton-model
description: "建立或维护公司财务模型（Excel）：在用户模型上滚动更新或按基金标准格式新建，并对照一致预期（consensus）。用户要求建模型、更新模型、加季度、改预测、核对模型、对比一致预期时使用。"
---

# 公司财务建模（`dalton-model`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

每家公司的指标、正负号习惯、财年和业务结构各有不同：下面给出标准做法和示例，遇到差异时按公司实际调整，并在工作簿最前面的 `README & Changelog` 工作表（下称「变更记录」）写明原因。与基金通用标准 `.agents/skills/dalton-conventions/references/model-standards.md` 的分工：维护原模型时，期间跨度、表头和最右备注列沿用原模型；新建模型时，基于内置默认模板 `.agents/skills/dalton-model/assets/default_model_template.xlsx` 与 `.agents/skills/dalton-model/references/format-spec.md` 搭建，历史覆盖跨度、空值留空和一手披露取数按 `model-standards.md` 执行。

## 一、必须做到的建模约定

1. **预测期金额全部由公式算出。** 收入、成本、费用、利润等金额行在预测期使用公式，例如 `=GMV（商品交易总额）× take rate（货币化率）`、`=去年同期×(1+增速)`、`=收入×利润率`、`=各分部之和`。
   - 调整**预测数值**：修改驱动假设格（增速、take rate、利润率、费用率、税率），由公式重新算出结果。
   - 调整**建模方法**（重新分部、补一条勾稽关系、换一个驱动）：修改公式结构并在变更记录写明原因。
   - 管理层直接给出金额指引（例如明年 CapEx〔资本开支〕指引 100 亿）：在对应区块或表下方单设输入格填入该金额（套驱动假设格式，在最右备注列写明来源），预测行用公式引用该输入格。
   - 原模型中原先为公式的预测行，更新后保持为公式。
2. **预测期间跨度与季度汇总年度列规则。**
   - 预测跨度：季度预测逐季覆盖本财年剩余季度及下一完整财年全部四个季度（如当前 `FY26` 覆盖至 `4Q27E`）；年度预测在下一财年之后再覆盖至少两个完整财年（如 `2026E–2028E`），单向向后扩展并完整保留原模型历史列。
   - 同表已有对应四财季列的年度列全部由公式算出，驱动假设放在预测季度列和无季度拆分的远期年度列：
     - 流量（收入、成本、利润、现金流）：四个季度相加；
     - 时点余额（现金、债务、期末股本）：取财年最后一个季度；
     - 比率（增速、利润率、take rate、占比）：用本年度列的分子和分母重新计算；
     - 年度 EPS（每股收益）：年度利润 ÷ 年度加权平均稀释股数（与公司披露口径一致）；
     - 用户数、活跃商家等 KPI（关键经营指标）：按公司披露口径计算（第四季度期末数、四季度均值或披露的年度去重数）。
3. **先独立建模，再对照一致预期。**
   - 先依据一手财报、管理层指引与研究判断独立设定分部和费用驱动假设，由公式算出本方预测。
   - 外部一致预期与卖方预测可以写入工作簿单独标注的参考快照区（如 `vs Consensus` 市场快照列），本方预测公式保持独立，与外部快照区隔离。
   - 本方预测与一致预期差距较大（默认总收入 `±5%`、利润与稀释 EPS `±10%`，近盈亏平衡改按利润率 `±150bp`）时，拆到分部、驱动假设、费用、利息和股本查明原因：发现自身假设遗漏或口径有误时，返回调整驱动假设并重算；有可核实研究依据支撑时，保留判断并在对照表与变更记录写明一句具体理由。
4. **原模型保持只读，按版本链递增保存。**
   - 用户提供了原模型或工作区已有模型时，在原模型基础上原地维护，原件保持只读，修改版另存至 `model/`：
     - 单公司模型命名为 `model/<TICKER>-model-v<NN>.xlsx`（首次为 `v01`，后续取最大版本号加一）；
     - 多公司行业整本模型保留整本结构，沿用原文件名格式递增版本号或日期（如 `model/China_Ecommerce_v03.xlsx` → `model/China_Ecommerce_v04.xlsx`），一轮只更新目标公司所属工作表与变更记录，保持其他公司工作表原样。

## 二、标准执行流程（`company-model` 工作流）

单次建模或滚动更新默认通过 `company-model` 工作流执行，串联「准备与基线指纹 → 受控原地编辑与一致预期对照 → 本轮范围检查与最多两轮修改 → 终态账本与交付」。

### 启动前准备

1. **解锁 `RunWorkflow`**：先用 Read 工具读取 `.agents/skills/run-workflow/SKILL.md`。
2. **确认参数与断点续跑**：
   - 检查 `research/<as_of>-model-<TICKER>/.progress.json`，若存在未结束的运行记录，用原参数加 `resume="<.progress.json 里的 run_id>"` 续跑。
   - 全新启动时调用 `RunWorkflow(workflow="company-model", params={...})`，参数如下：

| 参数名 | 说明 |
|---|---|
| `ticker` | 本轮唯一目标公司股票代码（如 `"PDD"`、`"JD"`、`"META"`），必填 |
| `company` | 公司名称（可选） |
| `workspace_dir` | 当前工作区路径（一般传 `"."`），必填 |
| `as_of` | 基准日期 `YYYY-MM-DD`（如 `"2026-11-27"`），必填 |
| `progress_path` | 固定传 `"research/{as_of}-model-{ticker}/.progress.json"`，启动必填，供账本写入 `run_id` 与断点续跑 |
| `mode` | 可选：`"auto"`（默认，有起点模型则更新，无则新建）、`"build"` 或 `"update"` |
| `model_path` | 可选：起点模型路径；行业工作区维护多公司整本时必传本工作区整本路径，单公司也可指定上传或已登记文件 |
| `expected_baseline_sha256` | 可选：多公司整本按家串行更新时，传入上一家完成后的 64 位 `SHA-256` |
| `consensus_thresholds` | 可选：按公司特征调整的一致预期差异阈值 |

3. **多公司行业整本串行更新**：在行业工作区需要更新多家公司时，按一家公司一轮串行调用 `company-model`；前一家完成后，将返回的 `candidate_path` 与 `model_sha256` 传给下一轮的 `model_path` 与 `expected_baseline_sha256`，逐家累进整本版本号。

### 阶段说明与脚本工具（工作流内部与手动降级共用）

1. **确定起点与提取指纹**：
   - 优先采用用户本次指定或上传的文件，其次取 `data/models.json` 中 `current` 对应的已登记路径；`model/` 与 `models/` 并存时先比对 `SHA-256`，一致则以 `model/` 为准，不同时按内容与交付记录确认。
   - 运行 `python3 .agents/skills/dalton-model/scripts/model_fingerprint.py <起点模型或默认模板> --ticker <TICKER> --out research/<as_of>-model-<TICKER>/model_dump/<TICKER>-fingerprint.json` 提取基线指纹、行项类型映射与样式画像。
   - 收集一手财报、历次指引与市场一致预期原始数据（落盘 `data/consensus/<YYYY-MM-DD_HHMM>.json`）。
2. **受控原地编辑与母本缓存保护**：
   - 读取 `.agents/skills/dalton-model/references/forecast-rules.md`、`rollforward.md` 与 `format-spec.md`。
   - 首次落盘前先运行 `python3 .agents/skills/dalton-model/scripts/model_edit.py next-path --workspace-dir . --ticker <TICKER>`（有起点模型时补 `--baseline <起点模型>`）绑定本轮输出路径 `<新候选路径>`，再统一通过 `python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline <起点模型> --ticker <TICKER> --plan <edit_plan.json> --out <新候选路径> --workspace-dir . --fingerprint <fingerprint.json> --coord-map-out <R>/model_dump/coord_map.json --mode <build|update>`（有基线哈希约束时补 `--expected-baseline-sha256 <起点64位SHA-256>`）执行扩列（保持原 `<calcPr>`）、只改目标单元格、在候选落盘前先从母本恢复非目标公司表与外链/插件/未改公式缓存，再在临时副本用 LibreOffice 复算并仅合并非错误计算值。
3. **一致预期快照与差异对照**（详见 `.agents/skills/dalton-model/references/sanity-check.md`）：
   - 运行 `python3 .agents/skills/dalton-model/scripts/model_consensus.py write-snapshot --model <当前候选路径> --consensus <consensus.json> --coord-map <R>/model_dump/coord_map.json` 将外部参考值写入独立快照区，运行 `python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model <当前候选路径> --ticker <TICKER> --research-dir <R> --consensus <consensus.json>` 在 `<R>/model_dump/` 生成 `consensus-diff.md` 与 `consensus-diff.json`。
   - 超阈值项拆源复核一次：若需调整自身驱动假设，先对 `<当前候选路径>` 重提指纹并取其当前 64 位 SHA-256，再用下述第 4 步相同的增量 `model_edit.py apply` 命令（`--baseline <当前候选路径> --out <当前候选路径> --in-place --prior-coord-map <R>/model_dump/coord_map.json --coord-map-out <R>/model_dump/coord_map.json --expected-baseline-sha256 <当前候选64位SHA-256>`）只改驱动假设并合并坐标映射，随后按最新模型哈希重跑 `model_consensus.py diff` 刷新差异表；有可核实独立依据则保留并在对照表与变更记录写明一句具体理由。
4. **本轮范围检查与最多两轮修改回路**：
   - 运行 `python3 .agents/skills/dalton-model/scripts/model_check.py check --workspace . --ticker <TICKER> --research-dir <R> --candidate <当前候选路径> --baseline <起点模型> --fingerprint <fingerprint.json> --coord-map <R>/model_dump/coord_map.json --workflow company-model --mode <build|update> --fix-round <0|1|2>`，在免语言审查目录 `<R>/model_dump/` 输出 `model-check.md`、`model-review-pack.md`、`model-verification.json` 与（存在未决项时）`remaining-issues.md`。
   - 检查只对本轮改动单元格、转实际值列、新增期间列与预测区做判定，结果分为「本轮可修 / 基线遗留 / 缺证据 / 不适用」四类，仅「本轮可修」交回建模方定点修改，最多两轮。定点修改必须以当前候选为基础应用增量（切勿把增量计划重新应用到起点原模型）：
     1) 对当前候选重提指纹：`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py <当前候选路径> --ticker <TICKER> --out <R>/model_dump/<TICKER>-fix<N>-fingerprint.json`；
     2) 以当前候选同时作为 `--baseline` 与 `--out` 执行增量修改：`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline <当前候选路径> --ticker <TICKER> --plan <fix_plan.json> --out <当前候选路径> --in-place --workspace-dir . --fingerprint <R>/model_dump/<TICKER>-fix<N>-fingerprint.json --prior-coord-map <R>/model_dump/coord_map.json --coord-map-out <R>/model_dump/coord_map.json --expected-baseline-sha256 <当前候选64位SHA-256> --mode <build|update>`；
     3) 按最新模型哈希重跑 `model_consensus.py diff` 刷新 `consensus-diff.md/.json`，再用 `model_check.py check --mode <build|update> --fix-round <1|2>` 复查。
   - 凡盘上存在可读候选模型，无论零问题通过（`pass`）还是两轮后留有未决项或检查/复算超时降级（`delivered_with_open_issues`），均生成终态账本并顺畅交付。

### 工作流关闭时的手动降级路径

若当前环境关闭了 `workflow.enabled`（工具列表原本无 `RunWorkflow`），主智能体按上述 1–4 步顺序手动执行各脚本（第 2 步首次落盘用起点模型生成 `<当前候选路径>`；第 3 步一致预期返工与第 4 步最多两轮定点修改均使用第 4 步的增量 `model_edit.py apply` 命令对 `<当前候选路径>` 重提指纹、`--in-place`、`--prior-coord-map` 与 `--expected-baseline-sha256` 原地叠加并刷新差异表；若检查或复算异常，运行 `python3 .agents/skills/dalton-model/scripts/model_check.py write-ledger --workspace . --ticker <TICKER> --research-dir <R> --candidate <候选路径> --kind modeling --failed-stages <失败阶段列表，如 check,consensus> --workflow company-model --mode <build|update>`〔可按需补充 `--baseline <起点模型>`、`--reason "<原因>"`；手动路径省略 `--run-id` 使账本 `run_id` 保持 `null`〕落盘降级账本与未决清单），并在收尾调用 `dalton_check_done` 时传 `"manual": true`。若 `workflow.enabled` 开启但工具列表暂未出现 `RunWorkflow`，先用 Read 工具读取 `.agents/skills/run-workflow/SKILL.md` 后重试。

## 三、收尾登记、完成核对与回复格式

1. **知识层模型登记**：
   - 先调用 `knowledge_read(doc="models")` 取 `latest_version_no` 作为 `base_version`（尚无记录时传 `null`），再调用 `knowledge_update(doc="models", ...)` 登记本次最终模型路径、`id`（如 `v03`）、`created_at`、`reason`、`recalculated` 与关键输出单元格 `key_outputs`，并将 `current` 指向新版本。
   - `knowledge_update(doc="models", base_version=..., ops=[...])` 的常规追加形状：`[{"op": "append", "path": "/versions", "value": {"id": "v03", "path": "model/PDD-model-v03.xlsx", "created_at": "...", "reason": "...", "recalculated": true}}, {"op": "set", "path": "/current", "value": "v03"}]`；首次建档时用 `base_version=null` 与 `op="create"` 写入 `{"schema_version": 1, "versions": [...], "current": "v01"}`（完整语法见 `.agents/skills/dalton-knowledge/SKILL.md`）。
2. **完成契约核对（汇报完成前必做）**：
   - 工作流路径：调用 `dalton_check_done(workflow="company-model", run_id=<.progress.json 里的 run_id>, params={"ticker": "<TICKER>", "workspace_dir": ".", "as_of": "<YYYY-MM-DD>", "model_path": "<如有起点模型则传入>"})`。
   - 手动降级路径：不传 `run_id`，在 `params` 中加 `"manual": true`。
   - `<R>/model_dump/` 下的机器报告免语言审查，核对器直接按终态账本核验模型可读性、哈希与阶段证据。
3. **回复用户的固定顺序**：
   - **当终态为「带问题交付」（`delivered_with_open_issues`）时，在回复最顶部原样列出 `<R>/model_dump/remaining-issues.md` 的全部条目**（未消的本轮可修项，或降级阶段名称与候选模型哈希前 12 位）。
   - **模型文件链接 + 约 10 行关键数字变更摘要**（基于最新模型与 `consensus-diff.json`）：新版本路径与基线哈希前 8 位、核心指标新旧版本与一致预期对比简表、本轮调整的关键驱动假设及每项明显差异的一句理由。
   - **折叠附于回复末尾的检查报告摘要**：四类问题统计表、复算引擎说明（注明单引擎复算）与扩展条件格式变动提示（如有）。

## 四、常见错误与正确做法

| 错误 | 看起来是什么样 | 正确做法 |
|---|---|---|
| 把公式改成了数字 | 原来 `=GMV×take rate` 的收入行变成固定数字 | 保留公式，只改 GMV 增速和 take rate 假设 |
| 由季度汇总的年度列留着假设 | 2027E 收入是四季之和，增速却手填 15% | 年度增速写成 `=2027E收入/2026E收入-1`，套衍生比率格式 |
| 把一致预期直接写进结果格 | 预测利润行直接填入市场均值或引用外部快照格 | 独立驱动算出本方预测，外部快照放独立对照区并保持公式隔离 |
| 套用了别的格式 | 表头深蓝、字号 10，与原模型不一致 | 照原模型或内置默认模板（Arial 8、亮蓝表头 `3366FF`、假设格浅米黄 `FFFFC8`） |
| 整本重写破坏缓存与窗格 | 用脚本从头写出整本 `.xlsx`，冻结窗格错位、非目标公司缓存清空 | 经 `model_edit.py` 原地只改目标格，落盘前恢复母本缓存 |
| 集团和分部脱节 | 集团营业利润与分部之和差额巨大且无调节项 | 集团营业利润 = 分部之和 + 总部及抵销（单列），差额有据可查 |
| 现金和股本不联动 | 净现金很多却无利息增长，回购后股数保持原值 | 利息随现金滚存，回购与股权激励同步反映在股数滚存中 |

## 参考文件

- `.agents/skills/dalton-model/references/forecast-rules.md`：版面样板、公式写法、各部分预测方法与净现金公司示例。
- `.agents/skills/dalton-model/references/format-spec.md`：基金格式手册、内置默认模板与 `apply_cell_style` 角色说明。
- `.agents/skills/dalton-model/references/rollforward.md`：在原模型上滚动维护与受控原地编辑的详细步骤。
- `.agents/skills/dalton-model/references/sanity-check.md`：一致预期取数、独立快照区、差异表与返工复核说明。
