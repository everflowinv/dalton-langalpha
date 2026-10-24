# Workflow 完成保障：7 个多步 skill 的完成契约

PR #110（`feat/workflow-completion`）只覆盖 6 个 workflow 的完成契约
（initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、
weekly-coverage-review、topic-research）及共用的 `dalton_check_done`、journal、
resume。7 个多步 skill 的契约由后续分支 `feat/skill-completion-contracts` 补齐，
契约文件在 `plugins/dalton/skills/<name>/contract.json`，各 SKILL.md 收尾段要求
汇报“完成”前调用 `dalton_check_done(workflow="<name>", params=...)`。

## 各 skill 契约（已实现）

- **dalton-earnings-preview**：`drafts/<D>-preview-<季度>.md`、
  `research/<D>-preview-<季度>/predictions.json`（重跑 `predictions.py validate --strict`）；
  收尾 drivers/debate 本轮 source=tool 版本、verdict（trigger_kind=earnings）、
  check_draft 侧车哈希与当前稿一致。
- **dalton-earnings-flash**：`research/<D>-flash-<季度>.md`（不进 drafts/、无 check_draft）；
  verdict（trigger_kind=earnings）。
- **dalton-stock-move** / **dalton-filing-review**：研究笔记 + 五词 verdict；本轮
  verdict 的 `word` 不是 `NO_CHANGE` 时，thesis/debate/drivers/management 至少一份
  须有本轮 source=tool 新版本（`knowledge_docs_any_unless_no_change`）。
  stock-move 的 trigger 为 `price_move`；filing-review 允许 `filing` 或 `manual`。
- **dalton-thesis-delta**：`research/tracker/last_run.json` 与 `updates.log` 须为今天
  （执行日 UTC）写入；`last_run.json` 的 `material=yes` 时要求 `research/<D>-delta.md`
  （artifact 级 `requires_json`）与至少一份受影响知识文档的本轮更新；verdict 必写。
- **dalton-call**：门禁两分支（`gated_draft`）：草稿存在＝门禁通过，要求草稿足量
  + check_draft 侧车哈希一致；草稿不存在＝门禁未过，要求本轮 word=`NO_CHANGE`；
  两者都没有、草稿读取出错或 verdict 查询失败＝无法判定，记 missing。
- **dalton-publish**：`drafts/<slug>.md`、`.annotated.md`、`.audit.md`（哈希一致）；
  发布状态二选一（`published_or_awaiting_pm`）：`published/<as_of>-<slug>.md` 已存在，
  或本工作区有 `draft_path` 匹配的 `awaiting_pm` 请求；查询失败记 missing。

## 共同约定

- verdict 与知识版本判定一律绑定**本轮**（今天 UTC 起、真实列 `word`/`trigger_kind`），
  数据库查询失败记 missing；
- 可选项（SKILL 原文写明可选的步骤）失败只进 warnings，且各失败方式一致。

## 未做（有意）

- thesis-delta 的“本轮写入”用 `last_run.json` 的 mtime（今天 UTC 起）核对，没有再核
  `run_id`：工具的 `run_id` 参数指 workflow run，复用会混淆两种身份；mtime 已满足
  “mtime 或 run_id”的要求。执行日跨 UTC 午夜的轮次会被判为非本轮（与 verdict/知识
  版本的“今天 UTC 起”口径一致，是全局约定的已知边界）。
- dalton-call 门禁通过分支不要求 `publish_report` 的发布状态，也不要求 verdict
  （SKILL 写明 verdict 在 PM 批准后才写）；发布状态由 dalton-publish 的契约核对。
- dalton-earnings-flash Step 11（确认/补建 T+1 深度链自动任务）没有可核对的工作区
  产物，未纳入契约。
