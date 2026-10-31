---
name: dalton-sector-delta
description: "行业覆盖单元的定期资料增量扫描：以行业关键词与成员公司名为检索条件扫内部研究库（不含 SEC/公告/股价/一致预期），对照行业知识层判断是否构成实质性变化，仅在有重要变化时写增量笔记并更新知识层。由行业自动任务 dalton-sector-<slug>-delta 每周唤醒，或手动检查行业基本面有无边际变化时使用；无实质变化时快速短路完成。"
---

# Dalton 行业资料增量跟踪（`dalton-sector-delta`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
行业认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 目标与边界

对行业覆盖单元（sector 单元）每周收一次"资料增量包"：以 `data/sector.json` 的行业名（`name` / `name_zh`）与成员公司名（`members[].name`）为检索词，扫内部研究来源 `sales-notes` / `tmtb` / `funda-ai` / `guidepoint`，判断相对现有行业知识层是否存在**重要变化（material change）**。

**行业不跑证券探针**（与本技能无关的边界，见行业议程任务说明）：SEC filing、港 A 公告、财报窗口、一致预期与股价都不采集——成员公司的这类事件由**公司侧**的 `dalton-<t>-watch` / `dalton-<t>-delta` 发现一次，再经跨单元联动（P2）向本行业产生待复核/更新候选；本技能绝不为成员公司重复采集证券类材料。`alphaengine` 的"覆盖边界"窗口模型按单一公司查询词维护游标，行业多检索词无法复用该状态，首期不扫（packet 的 `skipped` 有说明）。

- **零成本预检与 `<dalton_wake>`**：每周 cron（`dalton-sector-<slug>-delta.json`，北京时间 11:00）唤醒后线程首条上下文包含 `<dalton_wake>`（`probe=schedule`，`kind=sector_delta`），无信号的轮次在预检就被记 `skip`、花费为 0。
- **无重要变化不打扰 PM**：所有来源取数成功且判定不属于重要变化时，只记一条 `NO_CHANGE` 结论并 `commit --material no`，**绝不向 PM 推送消息**。

## 三步执行流程

### 第 1 步：读取 `<dalton_wake>` 并运行 `collect_sector.py`

```bash
python3 .agents/skills/dalton-sector-delta/scripts/collect_sector.py collect \
  --workspace-dir .
```

脚本读取 `data/sector.json` 生成检索词（行业名 + 成员公司名，默认上限 6 个，`--max-terms` 调整），逐词检索四个内部来源，写 `research/tracker/<run_id>/packet.json` 与 `packet.md`。与公司增量脚本共用同一条 researchdata 调用路（沙箱 PTC 包装模块优先，`DALTON_RESEARCHDATA_URL` 只作 HTTP 桥覆盖）；返回 `status=researchdata_transport_unconfigured` 时是配置错误，本轮未采集任何来源：不要重试采集，按"所有来源取数失败"如实汇报并停下，严禁写成"本轮无新文档"。

### 第 2 步：短路判断（要求全部来源 ok 且 0 新增）

- 检查汇总 JSON 的 `internal_status` / `failed_sources` / `all_sources_ok` / `short_circuit_eligible`。**任何来源 `ok: false` 或未完成（`incomplete` / `partial` / `has_more`）都不能当成"没有新文档"**：结论与汇报必须如实写明"`<来源> 本轮取数失败或未完成（原因）`"。
- 仅当 `all_sources_ok == true`、`failed_sources` 为空且 `counts.internal == 0` 时走短路：`commit --material no --summary "无新增"`，再调用 `knowledge_append_verdict` 写一条 `NO_CHANGE`（`progress="none"`，`trigger.kind="agenda"`，`trigger.ref` 填 `<dalton_wake>` 信号 id）。
- `skipped` 里的来源（`sec` / `consensus` / `price` / `alphaengine`）是**行业边界内设计性不采**，不算取数失败，也不参与短路判断的失败计数。
- 提交的窗口纪律由脚本保证：`all_sources_ok=false` 的包 commit 后 `window_advanced=false`，`last_run.until` 保持旧值（未扫到的区间留在下一轮窗口里，seen 去重兜底）——汇报不完整的一轮时要说明"检索窗口未推进，下轮重扫"。

### 第 3 步：有增量时（material=yes）

1. 读 `packet.md`，按证据对行业论点的影响分流：只影响成员公司的证据经 `delegate` 交回 Home 转公司分析师（行业工作区不改公司知识）；影响行业层面的（政策、供需、产业链格局、跨成员共性变化）才进入本工作区的知识更新。
2. 经 `knowledge_update` 更新 `thesis` / `debate` / `agenda`（必要时 `drivers` 指标读数），写增量笔记 `research/<YYYY-MM-DD>-delta.md`（引用 packet 的文档 id，成员公司相关证据注明"已转公司侧"）。
3. `commit --material yes --summary "<一句话结论>"`，并调用 `knowledge_append_verdict` 写五词结论（`trigger.kind="internal_material"`，`trigger.ref` 填信号 id 或 packet run_id）。

## 检索词维护

检索词来自 `sector.json` 的权威成员清单：成员名单变更（`knowledge_update` 到 `sector.members`）后下一次扫描自动生效；成员公司名过长/过泛导致检索噪音大时，在成员条目的 `note` 里注明，人工调 `--terms` 临时覆盖并考虑收敛 `name`。
