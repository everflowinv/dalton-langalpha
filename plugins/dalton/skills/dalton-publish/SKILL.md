---
name: dalton-publish
description: "执行研究草稿的数字与来源脚注核验（check_draft）及正式发布流程（publish_report：数字核验、跨模型家族独立审稿、PM 审批与不可变归档）。研究成稿准备自检或提交正式发布时使用；视审稿轮次需数分钟。"
---

# Dalton 发布治理与数字核验（Publication Governance）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

在 Dalton 工作流中，**草稿永不被阻止，核验只在正式发布时卡一次**。日常研究笔记放 `research/`，准备正式发布的报告先写在 `drafts/<slug>.md`，经 `check_draft` 自检无误后，调用 `publish_report` 走"数字核验 → 跨家族独立审稿 → PM 审批 → 不可变归档（`published/` + 数据库）"。

## 1. 数字写作与脚注约定（确保 100% 可核验）

正文中一律使用阿拉伯数字（不使用"约五百亿"等汉字数字）。每个关键数字后用 Markdown 脚注标注来源，保证正文干净可读：

```markdown
3Q26 广告收入 512 亿美元，同比 +21%[^e1]；我们的 FY26 EPS 预测 28.40 美元[^m1]，高于一致预期 27.10 美元[^c1]。

[^e1]: evidence:E-20261030-01
[^m1]: model:model/META-model-v13.xlsx#Model!H42
[^c1]: prov:3f6c9a12-0000-0000-0000-000000000001（Yahoo Finance 一致预期，2026-10-28）
```

四种合法脚注目标：
1. `evidence:<E-id>`：指向 `data/thesis.json` 证据台账中的条目（推荐先将核心财报/电话会数字入账）。
2. `model:<path>#<Sheet>!<Cell>`：指向 `data/models.json` 已登记且 `recalculated=true` 的模型单元格。**目标价、IRR、三情景收益必须指向模型单元格**。
3. `prov:<provenance_record_id>`：指向本工作区线程的 provenance 来源记录。**凡是标注为"一致预期 / consensus"的数字，必须指向一致预期接口记录（provider 前缀 `estimates:` 或 `yf_analysis`，或 identity `street_estimate`）**。
4. `f:<id>`：指向 `attachments` 中 `role="facts"` 附件（如 `drafts/<slug>.facts.md`）内 `<!-- FACTS-LEDGER -->` 表格的对应行，核验引擎会继续校验该行的 `ref`。

豁免规则：
- 引用块（`> ...`）中管理层原话里的数字自动豁免；
- 显式带 `〔示意〕`、`[示意]`、`（示意）`、`〔假设〕`、`[假设]`、`（假设）`、`〔估计〕`、`[估计]`、`（估计）`、`〔举例〕`、`[举例]`、`（举例）` 标记的数字自动豁免。

## 2. 草稿自检：`check_draft(draft_path)`

写完 `drafts/<slug>.md` 后，先调用 `check_draft(draft_path="drafts/<slug>.md")`：
- 生成侧车清单 `drafts/<slug>.audit.md`（按行号列出所有未匹配或冲突数字及原因：`unverified`、`source_conflict`、`period_mismatch`、`unit_mismatch`）；
- 生成带标注副本 `drafts/<slug>.annotated.md`（未核实数字标为 `**27.10 美元**⟨未核实⟩`）；
- 根据 `.audit.md` 补齐脚注或修正数字，直到 `unverified` 降为 0。

## 3. 正式发布：`publish_report(...)`

仅当草稿定稿且 `check_draft` 通过后调用：
1. **数字核验**：仍有未核实数字时返回 `audit_failed`，按 `.audit.md` 修正后重新调用。
2. **独立审稿（`dalton-reviewer`）**：服务端自动选择与作者不同模型家族的审稿人（按判断先行、证据链、分层、反方、预期差、与知识层一致性、仓位含义 7 项审查）。若返回 `review_revise`，按 `must_fix` 修改草稿后再次调用；若返回 `review_unavailable`，草稿保留在 `drafts/`，并在 `agenda.json` 登记 `pm_handoffs`。
3. **PM 审批**：
   - 在交互线程中弹出审批卡（`ToolApprovalCard`）；
   - 在自动任务（unattended）运行中不弹 interrupt，状态停在 `awaiting_pm` 并写入 `agenda.pm_handoffs`，待 PM 在交互线程中指示批准时传入 `request_id` 完成发布。
4. **不可变归档**：批准后写入 `published/<YYYY-MM-DD>-<slug>.md`（及 `.xlsx`）、写入 `dalton_publications` 与 `dalton_publication_files`，并在 `updates_thesis=True` 时更新 `thesis.official` 与 `published/INDEX.md`。`published/` 下的文件一旦生成绝不改写。
