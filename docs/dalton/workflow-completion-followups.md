# Workflow 完成保障：移出本 PR 的后续项

本 PR（`feat/workflow-completion`）只覆盖 6 个 workflow 的完成契约
（initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、
weekly-coverage-review、topic-research）及共用的 `dalton_check_done`、journal、
resume。以下 7 个多步 skill 的契约与对应 `check_done` 能力已从本 PR 移出，
`dalton_check_done` 对这些名字一律返回
`{"done": false, "missing": ["<name> 尚未定义完成契约"]}`。

后续 PR 逐份补齐时，按三轮审查的意见实现：

## 待补契约（按 skill）

- **dalton-earnings-preview**（`plugins/dalton/skills/dalton-earnings-preview/contract.json`）
  - 交付物：`drafts/<D>-preview-<季度>.md`、`research/<D>-preview-<季度>/predictions.json`
    （`predictions.py validate --strict` 过）；
  - 收尾：drivers/debate 知识版本（source=tool、本轮执行日）、
    verdict（trigger_kind=earnings）、check_draft 侧车哈希一致。
- **dalton-earnings-flash**
  - 交付物：`research/<D>-flash-<季度>.md`（不进 drafts/、不发布）；
  - 收尾：verdict（trigger_kind=earnings，五词初判）。无 check_draft。
- **dalton-stock-move** / **dalton-filing-review**
  - 知识更新是**有条件的**：本轮 verdict 的 `word`（真实列）不是 `NO_CHANGE`
    时，至少一份 thesis/debate/drivers/management 须有本轮 source=tool 新版本
    （第三轮实现的 `knowledge_docs_any_unless_no_change` 语义，schema 字段已随
    范围缩减移除，随本清单一起恢复）；
  - filing-review 的 verdict trigger 允许 `filing` 或 `manual`，不能只强制 filing。
- **dalton-thesis-delta**
  - 读 `research/tracker/last_run.json` 的 `material` 与 `run_id`
    （`collect.py` 的 `cmd_commit` 写入）：`material=yes` 时要求
    `research/<D>-delta.md` 笔记与至少一份受影响知识文档的更新；
  - `last_run.json` 须为本轮写入（mtime 或 run_id 核对）。
- **dalton-call**
  - 门禁两分支分开表达：门禁通过（草稿存在）要求正式草稿 + check_draft；
    门禁未过要求 word=`NO_CHANGE` 且无正式草稿；两个签名都读不到＝无法判定
    门禁结果，记 missing。
- **dalton-publish**
  - `drafts/<slug>.annotated.md` 必需；
  - 发布状态二选一：`list_pending(user_id, "awaiting_pm")` 里存在匹配
    `draft_path` 的请求，或 `published/` 下已有 `<as_of>-<slug>` 副本；
    都没有记 missing（查询失败也记 missing，不降 warning）。

## 恢复方式

- 契约文件与 SKILL.md 收尾段可从本分支第三轮 WIP（`09ccb6ec`）取回：
  `git show 09ccb6ec:plugins/dalton/skills/<name>/contract.json`；
- `check_done_tools` 需要随契约恢复的能力：`published_or_awaiting_pm`、
  `knowledge_docs_any_unless_no_change`、artifact 级 `requires_json`
  （实现见同一提交的 `src/dalton/tools/check_done_tools.py`）；
- 对应测试：`git show 09ccb6ec:tests/unit/dalton/test_check_done.py` 中
  publish/thesis-delta/条件知识等用例。

## 审查意见中对这些契约的其余要求

- verdict 与知识版本判定一律绑定**本轮**（今天 UTC 起、真实列
  `word`/`trigger_kind`），数据库查询失败记 missing；
- 可选项（SKILL 原文写明可选的步骤）失败只进 warnings，且各失败方式一致。
