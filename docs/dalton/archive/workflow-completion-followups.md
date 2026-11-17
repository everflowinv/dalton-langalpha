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
- 正式产物的完成契约带 `language_review` 关闭项（feat/language-review）：按
  `is_reviewable_deliverable`（`src/dalton/publish/language.py`，唯一范围规则）认定本次的正式产物：
  契约声明的可审产物（按 requires_param/requires_json/derive/glob 展开，不看 mtime）∪ run 的
  journal 里各派发回报的文件 ∪ 时间窗内（run 开始时间；手动路径用 as_of 当日，as_of 缺失或为
  latest 时用线程开始时间，再取不到用过去 24 小时；mtime 用 stat，同 modified_on）新写或修改过的
  全部可审 .md，逐个要求
  `<文件>.language.json` 含第 1、2 轮 `check_language` 记录，且最后一条第 2 轮的
  `draft_sha256` 与当前文件一致（声明的终稿还接受与最后一次独立审稿通过的哈希一致）；
  缺轮次、哈希对不上或读不到一律记 missing（dalton-call 门禁未过分支跳过）。复用的旧笔记
  不要求重审：同一哈希已有两轮记录即通过。`published/` 归档稿不作为语言目标（发布横幅使
  哈希不同且归档不可改，其审查以对应草稿为准）；`research/topics/conversations/` 为机器渲染，
  同样不审。适用：initial-coverage、full-coverage、
  earnings-deep-update、topic-research、bullbear-doc 五个 workflow（其 research/ 笔记
  与 drafts/ 背景稿同查），dalton-earnings-preview、dalton-call（门禁通过分支）、
  dalton-publish 的草稿。
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
