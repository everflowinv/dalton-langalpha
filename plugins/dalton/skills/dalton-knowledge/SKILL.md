---
name: dalton-knowledge
description: "维护覆盖公司或行业工作区的结构化知识层（公司/行业画像、核心论点、多空焦点、经营驱动、经验教训、研究议程、模型登记与判断日志）并保持读写一致。需要查阅既有认知或通过受控工具更新知识文档时使用；按需读写，轻量快速。"
---

# Dalton 知识层维护（Knowledge Layer）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

每家覆盖公司对应一个工作区（文件夹名 = ticker，如 `META`）；每个覆盖行业/主题/产业链对应一个绑定 sector 覆盖单元的工作区（见 §1b）。人读的中文 Markdown 放在工作区根目录，机器读的真值 JSON 放在 `data/` 下并写入 Postgres `dalton_knowledge_versions` 版本表。

## 1. 工作区文件地图

| 路径 | 角色 | 写入方式 |
|---|---|---|
| `agent.md` | 公司目标、一句话判断与文件索引；`<!-- dalton:summary:start ... -->` 至 `<!-- dalton:summary:end -->` 为服务端托管块 | 托管块由 `knowledge_update` 自动刷新；自由区可用 `Edit` 维护 |
| `thesis.md` / `data/thesis.json` | 核心支柱（3–5 条）、confirm/warn/break 阈值、只追加证据台账、house view 与正式版本 | `knowledge_update(doc="thesis", ...)` |
| `debate.md` / `data/debate.json` | 当前 Top Debates（分歧变量、多方、空方、最强反驳、我方判断、改判条件） | `knowledge_update(doc="debate", ...)` |
| `drivers.md` / `data/drivers.json` | 关键业务驱动、领先指标最新读数、EPS/倍数敏感度 | `knowledge_update(doc="drivers", ...)` |
| `management.md` / `data/management.json` | 管理层团队、指引风格（Beat 率/中位 Surprise 由服务端计算）、指引对实际历史 | `knowledge_update(doc="management", ...)` |
| `lessons.md` / `data/lessons.json` | 经验教训与来源纪律（含适用条件、反例；只可 `retract` 撤回，不可删除） | `knowledge_update(doc="lessons", ...)` |
| `agenda.md` / `data/agenda.json` | 待解研究问题、近期验证点、催化剂日历（非官方来源日期标 `estimated`/待确认）、唤醒条件 | `knowledge_update(doc="agenda", ...)` |
| `data/company.json` | 公司基本资料、覆盖层级（core/watch）、覆盖阶段、同业与基准 ETF | `knowledge_update(doc="company", ...)` |
| `data/models.json` | `model/<TICKER>-model-v<NN>.xlsx` 版本登记与关键输出单元格 | `knowledge_update(doc="models", ...)` |
| `verdicts.md` | 最近 60 条事件五词结论日志（全量在 `dalton_verdicts` 表） | `knowledge_append_verdict(...)` |

## 1b. 行业工作区（sector profile，P1）

工作区绑定了 **active 的 sector 覆盖单元**（`coverage_units.enabled` 开启且已登记）时，知识层切换为**行业 profile**；开关关闭或未登记一律按公司处理（§1 的公司文件地图）。判定特征：`knowledge_read(doc="sector")` 可读，写 `management`/`company` 会被工具拒绝并说明"行业覆盖单元（sector profile）"。

| 路径 | 角色 | 与公司的差异 |
|---|---|---|
| `sector.md` / `data/sector.json` | 单元画像：定义、研究边界、地区/市场范围、产业链、wiki 标识、研究时区、覆盖阶段、**成员公司清单** | 替代 `company.json`；字段速查见 `.agents/skills/dalton-init-sector/references/sector-fields.md` |
| `thesis.md` / `data/thesis.json` | 行业核心假设、周期位置、预期差、确认/证伪阈值、只追加证据台账 | 结构与不变量同公司；**不强制公司目标价等公司专属字段**（`house_view.target` 不写，首期不做行业 House View） |
| `drivers.md` / `data/drivers.json` | 行业供需/量价/产能/资本开支驱动与指标读数 | 行业指标看板从 drivers 生成，不另建指标账本 |
| `debate` / `lessons` / `agenda` / `models` / `verdicts.md` | 同公司 | 行业议程用宏观/政策/数据发布语义；结论沿用五词结论 |
| ~~`management.md` / `data/management.json`~~ | 公司专属，**行业工作区不创建**（调用即被拒） | — |
| ~~`data/company.json`~~ | 公司专属，**行业工作区不创建**（调用即被拒） | — |

成员清单维护规则：

- **唯一权威来源是 `data/sector.json` 的 `members`**（P2 的行业—公司关联表届时作为它的投影）；不要在别处另抄一份名单。
- 成员只登记 `name`（必填）、可选 `ticker` / `unit_id` / `note` 与 `workspace_created`；**登记成员不自动创建公司工作区**——由 Home 决定是否建，建好后把该成员 `workspace_created` 改为 true。
- **不得给行业编 ticker**：`ticker` 只用于确有证券代码的成员公司；行业单元本身永远没有 ticker，也不会被证券类跟踪/探针当成公司（身份由覆盖单元注册表决定，不是名字）。
- 初始化一个行业工作区用 `.agents/skills/dalton-init-sector/`。

## 2. 按需读取顺序

1. **回答 PM 问题**：先读 `thesis.md` 和 `debate.md`，再按需读 `drivers.md` 和 `verdicts.md`。
2. **事件或预检唤醒**：先读 `agenda.md`，再读唤醒原因关联的支柱与驱动。
3. **撰写正式报告**：先读 `thesis.md`、`debate.md`、`management.md` 及 `model/` 当前版本。

## 3. 硬性不变量（违反时工具拒写）

1. **证据台账只追加（`append_only_violation`）**：`thesis.evidence` 的已有条目必须原样保留在原位置；更正旧证据时，用 `append` 新增一条并填 `supersedes: "E-YYYYMMDD-NN"`。
2. **正式版本只经发布写入（`official_is_publish_only`）**：`thesis.official` 代表 PM 批准的正式 House View 快照，只能由 `publish_report(..., updates_thesis=True)` 写入，工具路径一律禁止修改。
3. **目标价与模型证据必须指向模型单元格（`target_needs_model_cell` / `model_needs_model_cell` / `model_tier_1_forbidden`）**：`house_view.target` 必须包含 `model_ref: {"kind": "model_cell", "path": "model/<TICKER>-model-v<NN>.xlsx", "cell": "Model!F42"}`，且该模型文件已登记在 `data/models.json` 的 `versions` 中；`source.type="model"` 的证据同样必须引用 `kind="model_cell"`（含 `path` 与 `Sheet!Cell`），且默认 tier 为 2–3，**禁止标 tier 1**。
4. **写证据必填 `figures`，且 `source.ref` 指向一手文件或 `prov`**：写入 `thesis.evidence` 的每条证据都必须填写 `figures` 数组——将该条证据 `fact` 中的每个事实数字逐项拆出，每项必填 `metric`（中英文指标名称，如 `"广告收入"`、`"Family DAP 同比增速"`）、`value`（数值）、`unit`（仅限枚举值 `"USD"` / `"USD_mn"` / `"USD_bn"` / `"HKD"` / `"HKD_mn"` / `"HKD_bn"` / `"CNY"` / `"CNY_mn"` / `"CNY_bn"` / `"CNY_100mn"` / `"pct"` / `"bp"` / `"x"` / `"count"` / `"count_mn"` / `"per_share"` / `"other"`，百分比填 `"pct"` 而非 `"%"`）、`period`（如 `"3Q26"`、`"FY2026"`、`"1H26"`、`"LTM"`、`"NTM"`、`"spot"`）、`label`（仅限六值枚举 `"reported"` / `"company_guidance"` / `"consensus"` / `"estimate"` / `"assumption"` / `"research_judgement"`，中文指标名放进 `metric`），供 `check_draft` / `publish_report` 通过 `evidence:E-YYYYMMDD-NN` 做结构化数值核验；`source.ref` 必须指向一手文件或 `prov` 记录（如 `kind="filing"`、`"provenance"`、`"model_cell"`、`"url"` 或 `research/` 下的支线取证笔记），严禁指向本次的报告初稿或草稿（`drafts/...`）。

## 4. `knowledge_update` 操作语言（Ops）示例

调用前先读 `data/<doc>.json`（或查看 `agent.md` / 历史版本）获取当前版本号 `base_version`（首次创建传 `null`）；**`base_version` 只从读到的当前文档取，不猜、不复用旧值**。对于批量更新或知识层草案（如初次覆盖/深度覆盖的 `knowledge-proposal.json`），**先传 `dry_run=True` 做校验与 diff 预览**（不写版本、不渲染文件，返回会写成什么、哪些地方不通过及期望结构），确认无误后再以 `dry_run=False` 正式提交。**不要用"试一次看报什么错"来探测 schema**（2026-10-30 复盘：NOW 给 `lessons` 根级多传 `as_of` 被拒、GOOGL 直接改 append-only 记录后又猜版本号、DASH 一次 16 条校验错误）：每个 doc 的根级字段是封闭集合（`additionalProperties=false`），校验错误里的 `Expected structure` 就是 schema，按它改，不要换字段名再猜；只追加的记录（如 `thesis.evidence`）不能原地修改，更正走 `append` + `supersedes`。所有列表元素按 `id` 寻址；新增元素省略 `id` 时由服务端自动分配（如 `P1`、`E-20261018-01`、`D1`、`K1`、`G-001`、`L-001`、`Q-001`、`V-001`）。

```json
[
  {
    "op": "append",
    "path": "/evidence",
    "value": {
      "period": "3Q26",
      "source": {
        "type": "filing",
        "identity": "META 3Q26 10-Q",
        "tier": 1,
        "ref": {"kind": "filing", "form": "10-Q", "accession": "0001326801-26-000045"}
      },
      "fact": "3Q26 广告收入达 594 亿美元，Family DAP 同比增长 6%，广告展示量 +10%，平均单价 +11%",
      "figures": [
        {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "3Q26", "label": "reported"},
        {"metric": "Family DAP 同比增速", "value": 6.0, "unit": "pct", "period": "3Q26", "label": "reported"},
        {"metric": "广告展示量同比增速", "value": 10.0, "unit": "pct", "period": "3Q26", "label": "reported"},
        {"metric": "广告平均单价同比增速", "value": 11.0, "unit": "pct", "period": "3Q26", "label": "reported"}
      ],
      "pillar_ids": ["P1"],
      "signal": "confirming",
      "quality": "high"
    }
  },
  {
    "op": "set",
    "path": "/pillars/P2/status",
    "value": "watch"
  },
  {
    "op": "set",
    "path": "/house_view",
    "value": {
      "stance": "long",
      "conviction": "medium",
      "summary": "广告核心变现稳健，但 FY26 Capex 上沿压制短期自由现金流倍数",
      "variant_perception": "市场过度线性外推折旧压力，忽视推荐模型带来的广告单价提升",
      "target": {
        "value": 680.0,
        "currency": "USD",
        "method": "FY26E 24x P/E",
        "model_ref": {
          "kind": "model_cell",
          "path": "model/META-model-v01.xlsx",
          "cell": "Valuation!F18"
        }
      },
      "updated_at": "2026-10-18T12:00:00Z"
    }
  }
]
```

其他常用操作：
- **补填历史指引实际值**：`{"op": "set", "path": "/guidance_history/G-001/actual", "value": {"value": 51.2, "unit": "USD_bn", "reported_at": "2026-10-30", "ref": {"kind": "evidence", "evidence_id": "E-20261030-01"}}}` 及 `{"op": "set", "path": "/guidance_history/G-001/outcome", "value": "above_high"}`。
- **支柱退役**：`{"op": "retire", "path": "/pillars/P4", "value": {"reason": "业务分部已重组，并入 P1"}}`。
- **撤回经验教训**：`{"op": "retract", "path": "/lessons/L-002", "value": {"reason": "后续两个季度反例证明该规律不再适用"}}`。

## 5. 事件五词结论（`knowledge_append_verdict`）

每次查看新证据（财报、公告、异动、卖方新观点、专家访谈）的研究会话结束时，必须调用 `knowledge_append_verdict` 记录五词结论之一，并明确下一项验证点（`next_validation`）：

| 正式词形（写入 JSON） | 中文显示 | 适用情形 |
|---|---|---|
| `NO_CHANGE` | 无变化 | 噪音或已被价格/现有预期充分反映，支柱与预期差不变 |
| `THESIS_STRENGTHENED` | 强化 | 核心支柱得到 1–3 级证据验证，确认阈值命中 |
| `THESIS_WEAKENED` | 削弱 | 支柱出现预警信号或偏离预期路径，但尚未触发证伪阈值 |
| `THESIS_BROKEN` | 证伪 | 触发核心支柱 `break` 阈值，必须同步修订支柱状态与 House View |
| `NEW_THESIS` | 新论点 | 业务驱动或分歧框架发生结构性变化，立新支柱或新 Debate |

## 6. 直接修改提示的合入与来源纪律

- **处理直接修改提示**：若 `agent.md` 托管块提示 `⚠ data/<doc>.json 被直接修改` 或 `ℹ <doc>.md 被直接修改`，先读对应的 `history/<doc>/...-edited.md` 或 `data/<doc>.json`，用 `knowledge_update` 将有效修改正式合入，消除告警。
- **来源层级（Tier 1–6）与 `source.type` 枚举**：
  - Tier 1：`filing`（监管财报：10-K/10-Q/8-K/20-F）
  - Tier 2：`company_release`（公司官方公告/新闻稿/IR 演示文稿）、`transcript`（电话会/业绩会转写，包括 AlphaEngine ASR 稿；若为卖方回访转写可记 tier 3）
  - Tier 3：`broker_report`（署名卖方研报）、`consensus`（一致预期，如 Yahoo Finance / Visible Alpha / 彭博一致预期，直接记 `consensus` 而不必硬塞进 `broker_report`）
  - Tier 4：`official_data`（官方/行业协会统计）
  - Tier 5：`expert`（专家访谈，**只写身份如"前 Meta 广告产品总监"，严禁写个人姓名**）、`internal_note`（团队内部对话或纪要，默认 tier 5，视引述内容可记 3–5）
  - Tier 6：`news`（新闻报道，仅作线索）
  - 特殊类型：`model`（本方模型推导，默认 tier 2–3，**必须引用模型单元格 `kind="model_cell"` 且禁止标 tier 1**）、`legacy_thesis`（历史迁移论点，旧数据兼容，tier 3–5）。
- **严谨表述**：本轮未取到某项数据，只能写"本轮未取得"，严禁推断为"公司未披露"。
