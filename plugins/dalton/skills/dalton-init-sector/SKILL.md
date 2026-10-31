---
name: dalton-init-sector
description: "为已绑定行业覆盖单元的工作区初始化行业知识层：创建 sector 文档（定义、边界、产业链、成员清单）与行业版 thesis / agenda / drivers，全部经 knowledge_update 落库。开始覆盖一个行业、主题或产业链时使用；要求工作区已绑定 sector 覆盖单元。"
---

# Dalton 行业覆盖初始化（`dalton-init-sector`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
知识层读写规范：.agents/skills/dalton-knowledge/SKILL.md（含"行业工作区"一节）。

## 1. 适用场景与前置条件

- 开始覆盖一个**行业 / 主题 / 产业链**（同一套覆盖单元模型）时使用本技能，为一个**已绑定 sector 覆盖单元**的工作区建立行业知识层。
- **前置条件（缺一不跑，先回 Home 补齐）**：
  1. `coverage_units.enabled` 开启（sector 单元已登记，见 `.agents/skills/dalton-knowledge/SKILL.md` 行业一节）；
  2. 本工作区绑定的 active 覆盖单元 `kind=sector`。判断方式：`knowledge_read(doc="sector")` 能读（首次为 null 不报错），而写 `management` / `company` 被拒并返回"行业覆盖单元（sector profile）"字样，即为行业工作区；
  3. `data/sector.json` 尚不存在（`knowledge_read(doc="sector")` 的 latest_version_no 为 null 即未初始化；已有则走日常维护，不重复 create）。
- **行业工作区不得有 ticker**：不写公司文档 `company` / `management`；成员公司的身份只登记在 `sector.members`。

## 2. 初始化顺序（四步，全部经 `knowledge_update`，禁止手写 data/*.json）

每一步：先 `dry_run=True` 校验，通过后 `base_version=null`（首次 create）正式提交。`reason` 写一两句为什么。

### 第 1 步：`sector` 文档（对象画像，唯一权威成员清单）

字段见 [references/sector-fields.md](references/sector-fields.md)，要点：

- `name` / `name_zh` / `kind`（`sector` | `theme` | `value_chain`）/ `definition` / `boundaries`（明确纳入与排除，防止与相邻单元重叠）/ `coverage_stage: "onboarding"` / `one_liner`（≤200 字一句话）必写；
- `scope.regions` 与 `scope.markets`、`value_chain`（产业链环节，按上下游顺序）、`wiki_slug`（共享 wiki 的行业目录，只读映射）按实际填写；`research_tz`（IANA 时区）**P1 只保存，不参与任何到期计算**——议程到期调度按它执行属于后续阶段（P3），现有 agenda 探针仍按公司/交易所规则，别为了"让时区生效"而填它；
- `members`：成员公司清单——每项 `name` 必填，`ticker` / `unit_id` 可选（没有就省略，**不得为行业或未确认的成员编造 ticker**），`workspace_created` 默认 false。**登记成员不自动创建公司工作区**：是否建工作区由 Home 决定，建好后把对应成员 `workspace_created` 改 true。
- 示例 create 值：

```json
{
  "name": "AI Infrastructure",
  "name_zh": "AI 基础设施",
  "kind": "sector",
  "definition": "面向大规模训练与推理的算力、网络与平台层供应链",
  "boundaries": "纳入 GPU/ASIC、先进封装、组网与 IDC 电力；排除终端消费电子",
  "scope": {"regions": ["全球"], "markets": ["US equities", "A股", "港股"]},
  "value_chain": [
    {"stage": "上游芯片与先进封装"},
    {"stage": "服务器与网络设备"},
    {"stage": "云与 IDC 平台"}
  ],
  "research_tz": "America/New_York",
  "coverage_stage": "onboarding",
  "members": [
    {"name": "某综合科技公司", "ticker": "EXAMPLE", "workspace_created": false},
    {"name": "未覆盖的封装厂商"}
  ],
  "one_liner": "算力供给的节奏与瓶颈决定行业盈利周期"
}
```

（id / added_at / workspace_created 由服务端补齐，不用传。）

### 第 2 步：行业版 `thesis`

结构同公司 thesis（支柱、阈值、只追加证据台账、`official` 由发布写入），但语义是**行业**：

- 支柱写行业核心假设、周期位置、预期差；阈值用行业级指标（量价、产能利用率、资本开支增速），不用公司目标价；
- **不强制公司专属字段**：`house_view.target`（目标价）不写——首期不做行业 House View，`house_view` 写 `stance`/`conviction`（没有预期差就 `no_view`/`none`）与 `summary` 即可；
- 证据引用行业来源（协会统计 `official_data`、监管原文、卖方行业报告），figures 的口径与期间照实拆。

### 第 3 步：行业版 `agenda`

`open_questions` 写行业问题；`next_validations` 绑行业指标与政策/发布节点；`watch.events` 的 type 用 `macro`/`regulatory`/`product` 等行业语义，公司专属的 `earnings`/`guidance_update` 留给公司工作区。

### 第 4 步：行业版 `drivers`

`drivers.kind` 用 `volume`/`price`/`capex`/`macro` 等行业驱动；`leading_indicators` 登记行业指标（频率、来源、good_direction、probe），读数只追加。行业指标看板后续从 drivers 生成，不另建指标账本。

## 3. 初始化后的核对

1. `knowledge_read(doc="all")`：sector / thesis / agenda / drivers 四份各有版本；
2. `sector.md` / `thesis.md` / `agenda.md` / `drivers.md` 已渲染，`agent.md` 托管块标题为"（行业 · …）"而非 ticker；
3. 尝试 `knowledge_update(doc="management", …)` 应被拒（"公司专属文档"）——这是行业工作区身份的负向确认；
4. 完成后按 Home 的委派要求回报（沿用 report_back 机制），并在回报里给出 unit_id 与四份文档的版本号。
