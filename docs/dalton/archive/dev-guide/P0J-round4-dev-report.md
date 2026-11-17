# Dalton on LangAlpha 第四轮整改开发报告（P0J）

日期：2026-10-21

---

## 1. 结论

P0J 第四轮整改的六个问题族（J1–J6）已全部完成。本轮全部真实运行证据均在同一个已合并到 `dalton/main` 的干净提交 `ad1b9544` 上重新生成并通过环境与身份闸门校验（`valid=true`）；唯一未完成的子项是 G4 场景②的“当日新增内部文献”子场景（运行当日无新入库的内部资料，按 `docs/dalton/dev-guide/P0J-round4-fixes.md` 第 5 节待有真实新资料的交易日补跑）。本轮未进入 P0.9 与 P0.10，现提请第五轮审查。

---

## 2. 各族交付

### 2.1 J1：真实运行证据底座（V1、V11、V12、B2）

- **PR #25** `fix(P0J.J1): real-run evidence base, Docker build SHA/dirty verification, and pr_baseline.sh (r3)`（合并提交 `cd27b9b9`）：让 `/api/v1/dalton/openclaw/health`（`src/dalton/app/router.py` 传入调用方 `user_id`，字段由 `src/dalton/services/openclaw_admin.py` 的 `probe_health` 构造）返回后端 `git_sha`、`git_dirty` 与调用方 `user_id`，在 `src/dalton/settings.py` 中统一 `pm_user_ids` 配置并支持 `DALTON_PM_USER_IDS` 覆盖；新增 `scripts/dalton/realrun/check_env.py`、`scripts/dalton/realrun/render_report_tables.py`、`scripts/dalton/pr_baseline.sh` 与 `scripts/dalton/docker_build_backend.sh`，升级 `scripts/dalton/realrun/_common.py` 的证据信封以强制执行 `L40`（禁止生产路径测试用户后门、收窄合成入口为六种合法形式、校验真实用户令牌与邮箱脱敏）和 `L41`（`--record` 模式下强制校验已合并干净提交及容器 `git_sha` 一致）；升级 `scripts/dalton/check_report_claims.py` 支持证据 `sha256` 与生成区块校验，并将 G6 报表输入抽离为静态夹具 `tests/fixtures/dalton/g6/lenses_inputs.json`。

### 2.2 J2：发布核验删除按词与按时长排除数字规则并重标金标准

- **PR #13** `test(dalton): P0J J2a gold standard v2 and colloquial counterexample`（合并提交 `1155a4e3`）：将 `tests/fixtures/dalton/g3/gold_standard.json` 升级至第二版结构，新增 `is_verification_target` 与九类 `incidental_category` 标注，把带可核实事实的句子改标为核验目标，用 `value_as_written` 和 `unit_as_written` 替代口语词的人工换算数值，并新增口语反例夹具 `tests/fixtures/dalton/g3/colloquial_only_draft.md` 与 `tests/fixtures/dalton/g3/colloquial_only_gold.json`。
- **PR #18** `fix(P0J.J2): 删除发布核验中按词和按时长排除数字的规则并统一 assist 后备模型`（合并提交 `6d8843b5`）：在 `src/dalton/publish/numbers.py`、`src/dalton/publish/audit.py`、`src/dalton/llm/assist.py` 等文件中删除 `_COLLOQUIAL_HINT_RE` 口语词门控、时长判 `incidental` 正则、`_can_be_incidental` 中的财务词黑名单与版本词白名单，以及写死在发布核验里的 `deepseek-flash` 重试；按 `L43` 限定非核验对象为九类并交由 Step B LLM 归类，统一定义 `EXEMPT_MARKERS` 常量并与 `plugins/dalton/skills/dalton-publish/SKILL.md` 同步，在 `normalize_period` 中支持中文 `LTM` 表述，在 `src/dalton/llm/assist.py` 与 `src/dalton/llm/role_policy.py` 中实现统一的 `assist_fallback` 后备模型配置，并在 `PublishApprovalCard.tsx` 中展示非核验对象的原句、九类归属及失败批次明细 `failed_batch_details`。
- **PR #26** `fix(P0J.J2b): remove disclaimer section heading skip in numbers.py`（合并提交 `53bfb990`）：删除 `src/dalton/publish/numbers.py` 的 `_split_markdown_sections_and_chunks` 中按免责声明标题跳过整节的列表，确保草稿每一节都送入 Step B 抽取。

### 2.3 J3：夹带清理与小修复

- **PR #17** `fix(P0J.J3): 夹带清理与小修复（去 local-dev-user 特殊分支、补齐 PR #7 测试、启动门窄化与 UI 走查键清单）`（合并提交 `8bbd1557`）：删除 `src/dalton/autonomy/board.py`、`src/dalton/autonomy/probes/director.py` 与 `src/dalton/llm/assist.py` 中按 `local-dev-user` 字面量豁免隔离的分支（系统级上下文统一用 `user_id is None` 表示，研究总监每日维护 `_run_daily_system_maintenance` 改在系统上下文下执行）；在 `src/server/app/setup.py` 中将启动阻断范围收窄为 `DaltonStartupConfigError`（含 `RolePolicyError` 与 Dalton 迁移缺失）；从 `src/dalton/knowledge/schemas/common.schema.json` 中移除未约定的 `"guidance"` 枚举别名；在 `src/dalton/knowledge/service.py` 的 `append_verdict` 中将被剥离的瞬态字段记入返回值的 `warnings`；扩展 `tests/unit/dalton/test_no_test_user_backdoors.py` 与 `tests/unit/dalton/test_p0j_j3_smuggled_cleanup.py`，并新增 `docs/dalton/dev-guide/P0.9-ui-walkthrough-keys.md`。
- **PR #23** `fix(P0J.J3b): _verify_workspace_access 工作区归属检查改为失败即拒绝（404 / 503 / 403）`（合并提交 `ca822e62`）：将 `src/dalton/app/router.py` 中的 `_verify_workspace_access` 改为失败即拒绝（工作区不存在返回 404、数据库查询异常返回 503、非工作区所有者返回 403），消除查询异常或空行时放行请求的隐患。

### 2.4 J4：PR 模板与纪律（L40–L43）

- **PR #14** `docs(dalton): P0J decisions, nine incidental categories, task brief`（合并提交 `96804a0e`）：在 `docs/dalton/dev-guide/P0J-round4-fixes.md` 中落地九类非核验对象定义（`L43`）、任务书要求与第 8.1 节初步决定记录。
- **PR #15** `docs(dalton): P0J.J4 PR template with diff line mapping and principle self-check`（合并提交 `69c14504`）：更新 `.github/pull_request_template.md`，要求每个 PR 逐文件逐行段映射到任务书小节，并填写 7 条核心原则自检表与证据清单。
- **PR #19 / #21 / #22 / #24 / #27 / #28 / #30 / #31** `docs(dalton): P0J 决定（§8.1）`（合并提交 `bf9c39ef`、`3bc89ce2`、`94a8d368`、`5e0ff56a`、`a57b4a34`、`a4cb0be9`、`24846dd0`、`8ec04a95`）：在 `docs/dalton/dev-guide/P0J-round4-fixes.md` 第 8.1 节中记录本轮实施与真实运行前的第 1–20 条决定。

### 2.5 J5：真实运行重做与运行期问题修复

- **PR #32** `fix(dalton): P0J J5a real-run script fixes (G1-G5, check_env) — replaces #29`（合并提交 `4dfef0c6`）：按 `docs/dalton/dev-guide/P0J-round4-fixes.md` 第 8.1 节第 19–20 条决定调整 `scripts/dalton/realrun/` 下的预检与实跑脚本（G1 使用相对增量日预算上限并恢复测试前设置、G3 只读查询迁移工作区、G4 场景⑤使用零工作区闲置用户 `realrun-idle-c`、清理逻辑增加迁移工作区归属保护）。
- **PR #33 – #43**（合并提交 `1bf645ee` 至 `a432909c`）：修复真实运行过程中暴露的脚本问题与产品缺陷（逐项见第 3 节）。
- **PR #44** `docs(P0J.J5): real-run evidence summaries for 2026-10-21 (all families on ad1b9544)`（合并提交 `cd880f52`）：在合并后的干净提交 `ad1b9544` 上完成全部真实运行，提交渲染后的 8 份证据摘要 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/` 与财务镜头对比表 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-2026-10-21.md`。

### 2.6 J6：文档改正与第四轮开发报告

- **当前分支（J6 文档改正）**：按 P0J 第 6 节与第 7 节要求更新 `docs/dalton/dev-guide/PROGRESS.md` 与 `docs/dalton/dev-guide/README.md`（追加 `L39`–`L43`），在 `docs/dalton/dev-guide/P0I-round3-dev-report.md`、`docs/dalton/dev-guide/review/SUMMARY-R3-20261018.md`、`docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md`、`docs/dalton/dev-guide/P0I-round3-fixes.md` 追加勘误，并撰写本报告 `docs/dalton/dev-guide/P0J-round4-dev-report.md`。

---

## 3. J5 真实运行中发现并修复的问题

1. **G5.2 `--record` 回调交换前被预检拦截（PR #33）**
   - **现象**：运行 `scripts/dalton/realrun/g5_broker_and_antigravity.py --mode g5.2 --record` 粘贴回调地址交换令牌时，在调用 `/api/v1/oauth/antigravity/callback` 之前即报错退出。
   - **原因**：`record=True` 客户端在构造时立即执行完整 `check_env` 预检，其中包含 `oauth_token_provenance` 检查，而在回调交换完成前数据库尚无新令牌。
   - **修复 PR**：PR #33（`fix(P0J.J5b): G5.2 --record exchanges the callback with a record=False client`，`1bf645ee`）先执行 `L41` 代码与容器干净度校验并用 `record=False` 客户端完成回调交换，待新令牌落库后再执行完整预检。

2. **G1 花费循环在准入门槛处提前收到 429（PR #34）**
   - **现象**：G1 子智能体花费循环在当日已结算花费尚未达到名义上限时即收到 HTTP 429 并报错。
   - **原因**：`src/dalton/budget/gate.py` 在剩余日预算低于准入门槛 `admission_floor_usd`（`$0.25`）时即拒绝新回合准入，而脚本原先一直循环到 `settled_usd` 超过名义上限才停止。
   - **修复 PR**：PR #34（`fix(P0J.J5c): G1 stops spending where admission refuses, not at the nominal cap`，`65d32f76`）在 `scripts/dalton/realrun/g1_budget.py` 中改为当剩余预算低于准入门槛（`_gate_would_refuse`）时结束铺垫循环，随后断言下一轮对话返回 429 `dalton_daily_budget`。

3. **产品资金漏洞：子智能体预算预留结算竞态导致漏计现金花费（PR #35）**
   - **现象**：G1 真实运行中，OAuth 主模型派出的现金子智能体已完成调用并写入 `conversation_usages`，但用户的当日预算 `settled_usd` 漏计了子智能体现金花费。
   - **原因**：这是真实的产品资金结算竞态漏洞——在 `src/dalton/database/budget.py` 中，对账步骤原先只要看到关联线程下没有 `in_progress` 状态的子智能体运行记录，就立即将预算预留（reservation）标为 `settled`；然而上游子智能体编排器在将后台任务标为终态之后（毫秒到分钟不等）才由用量收集器写入 `conversation_usages` 并更新 `usage_settled_at`，若对账落在该时间窗内，预算预留就会按主模型 `$0` 的 OAuth 记录提前结清，而晚到的子智能体现金记录的 `conversation_response_id` 与该预约的 `run_ref` 相同，又被排除在未预留花费之外，导致现金花费被永久漏计。
   - **修复 PR**：PR #35（`fix(P0J.J5d): budget reservations settle only after subagent usage is recorded`，`c4d10b94`）修改 `src/dalton/database/budget.py`，要求所有子智能体运行记录的 `usage_settled_at IS NOT NULL` 后才允许结清预留，并新增对账棘轮步骤将晚到用量向上追补进已结算的 `settled_usd`。

4. **G1 截图等待 `networkidle` 超时与 `publications.py` 缺少 `logger` 定义（PR #36）**
   - **现象**：G1 在完成 429 验证后用 Playwright 截取前端横幅时因等待 `networkidle` 超时失败；同时后端日志暴露出 `src/dalton/database/publications.py` 存在未定义 `logger` 的 `NameError`。
   - **原因**：前端页面保持 `/api/v1/users/me/thread-events` 长连接使 `networkidle` 永不触发；`src/dalton/database/publications.py` 调用了 `logger.warning` 却漏写模块级 `logger = logging.getLogger(__name__)`。
   - **修复 PR**：PR #36（`fix(P0J.J5e): G1 screenshot waits for load; publications.py defines its logger`，`304b6924`）将 `scripts/dalton/realrun/g1_budget.py` 的页面加载等待条件改为 `wait_until="load"`，并在 `src/dalton/database/publications.py` 中补齐模块级 `logger` 定义。

5. **G1 截图被首次登录引导弹窗拦截（PR #37）**
   - **现象**：G1 Playwright 脚本在对话输入框按回车发送消息后超时，未触发请求。
   - **原因**：真实测试用户 `realrun-pm-a` 首次打开前端页面时弹出了模态引导对话框（onboarding dialog），遮挡输入框并吞掉了回车事件。
   - **修复 PR**：PR #37（`fix(P0J.J5f): G1 screenshot closes the onboarding dialog before sending`，`50ecb12d`）在 `scripts/dalton/realrun/g1_budget.py` 发送消息前检测并关闭可见的模态对话框。

6. **G1 截图中浮动新手清单遮挡拒绝横幅（PR #38）**
   - **现象**：G1 截出的 PNG 图片右下角浮动“快速上手”面板遮挡了底部的预算拒绝横幅文字。
   - **原因**：前端默认展开新手引导清单浮层，位于对话输入框与错误横幅上方。
   - **修复 PR**：PR #38（`fix(P0J.J5g): G1 screenshot hides the getting-started checklist`，`15ef5fc3`）在截图前点击“隐藏引导”按钮收起浮层。

7. **G2 缺少 DeepSeek BYOK 密钥与 G3 口语数字丢失前邻修饰词（PR #39）**
   - **现象**：G2 探测 `deepseek` 供应商时因测试用户与平台均未配置密钥而失败；G3 口语反例草稿中 `"近一半"` 被抽成 `"一半"`。
   - **原因**：服务令牌用户 `realrun-pm-a` 未预置 DeepSeek BYOK 密钥；`src/dalton/publish/numbers.py` 合并 Step B 补充的 `extra_figures` 时直接采用模型剥离修饰词后的短串，未保留原文紧邻的约数或否定前缀。
   - **修复 PR**：PR #39（`fix(P0J.J5h): G2 gets a temporary DeepSeek BYOK key; colloquial figures keep their qualifiers`，`89b9a7e5`）在 `scripts/dalton/realrun/g2_providers.py` 中为测试用户临时写入并在结束后恢复 DeepSeek BYOK 密钥，同时在 `src/dalton/publish/numbers.py` 中扩展 `extra_figures` 原文匹配以保留紧邻的约数与否定修饰词。

8. **G4 场景②重跑时命中同一用户的历史分诊缓存（PR #40）**
   - **现象**：G4 场景②在重跑时报错未记录新的 `assist.preflight.triage` 调用行。
   - **原因**：`assist.preflight.triage` 按 `assist:{user}:preflight.triage:{payload_sha}` 永久回放同一用户对同一份 Form 4 的分诊结果，此前运行已将该样本缓存。
   - **修复 PR**：PR #40（`fix(P0J.J5i): G4 scenario 2 triages a Form 4 the user has not had triaged`，`0781c0e8`）在 `scripts/dalton/realrun/g4_autonomy.py` 中只读查询 `dalton_llm_journal`，挑选当前测试用户尚未分诊过的最新真实 Form 4。

9. **清理阶段删除尚在收尾的工作区触发 HTTP 409（PR #41）**
   - **现象**：G1 等脚本在运行结束清理测试工作区时偶发收到 `DELETE /api/v1/workspaces/{id}` 返回的 HTTP 409（`WorkspaceBusyError`）。
   - **原因**：后台子智能体完成任务回报主线程时会触发一轮简短的汇总回合，脚本在主回合刚返回时立即删除工作区会撞上尚未结束的后台沙箱会话。
   - **修复 PR**：PR #41（`fix(P0J.J5j): realrun cleanup waits for a busy workspace before deleting it`，`9ba4bedf`）在 `scripts/dalton/realrun/_common.py` 中新增 `delete_workspace_when_idle` 并在 `check_env.py` 与 G1–G4 脚本中统一调用，遇到 409 时等待工作区空闲后再删除。

10. **回放行缺少重放时间戳导致窗口内回放漏计与历史花费误计（PR #42）**
    - **现象**：G3 重跑时因前序运行已填充缓存，窗口内新调用数为零且回放未被计入本次时间窗；同时 G1 的 `$0.50` 花费校验把命中历史缓存行的初次创建花费算进了本次运行。
    - **原因**：`src/dalton/database/llm_journal.py` 的 `record_journal_replay` 仅递增 `usage.replay_count` 而未记录重放发生时间，`scripts/dalton/realrun/journal_dump.py` 仅按 `created_at` 过滤时间窗，且 G3 第 6 步每次重跑写入相同的微调文本。
    - **修复 PR**：PR #42（`fix(P0J.J5k): journal records replay time; evidence counts in-run replays; G3 step 6 edit is unique per run`，`ad1b9544`）在 `record_journal_replay` 中写入 `usage.last_replayed_at`，在 `journal_dump.py` 中将时间窗内发生重放的记录纳入统计（对窗口前创建、窗口内重放的行标记 `replayed_from_before_window` 并按零增量现金计入本窗口花费），并在 `scripts/dalton/realrun/g3_publish.py` 第 6 步中使用每次运行唯一的不含数字后缀。

11. **单独运行 G5.1/G5.2 时摘要渲染被跳过（PR #43）**
    - **现象**：运行 `scripts/dalton/realrun/render_report_tables.py` 或摘要生成函数时，`var/dalton/trial-runs/realrun-2026-10-21/g5_1_broker.json` 和 `var/dalton/trial-runs/realrun-2026-10-21/g5_2_antigravity.json` 未生成对应的 `G5.1.md` 与 `G5.2.md`。
    - **原因**：`scripts/dalton/realrun/_common.py` 的 `render_summaries_from_var_to_docs` 原先无条件跳过 `G5.1` 和 `G5.2` 子项，假定总是存在合并的 G5 证据文件，而 G5.2 实际是单独以 `--mode g5.2` 录制的。
    - **修复 PR**：PR #43（`fix(P0J.J5l): render G5.1/G5.2 summaries when there is no combined G5 file`，`a432909c`）修改 `render_summaries_from_var_to_docs`，仅当存在合并的 `.all` 证据文件时才跳过子项文件。

---

## 4. 证据表

本轮全部证据摘要位于 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/`（`G1.md`、`G2.md`、`G3.md`、`G4.md`、`G5.1.md`、`G5.2.md`、`G6.md`、`G7.md`）及 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-2026-10-21.md`。下表由 `scripts/dalton/realrun/render_report_tables.py` 从 `var/dalton/trial-runs/realrun-2026-10-21` 自动生成：

<!-- generated:evidence-table -->
| 族 | 入库脚本 | 原始证据路径 | 原始证据 `sha256` | `git_sha` | `test_user_id` | 新调用 / 回放 (`fresh` / `replayed`) | 对话轮次 (`turns`) | 合计现金花费 (`combined_cash_usd`) | 名义花费 (`notional_usd`) | `valid` |
|---|---|---|---|---|---|---|---:|---:|---:|---|
| **G1 预算** | `scripts/dalton/realrun/g1_budget.py` | `var/dalton/trial-runs/realrun-2026-10-21/g1_budget.json` | `1e9ca01821936b1b76dd89920e670aaf64ff1385ce69dd6100e8c730043cbd18` | `ad1b9544` | `realrun-pm-a,realrun-user-b` | `2` / `3` | `21` | `$0.058583` | `$0.016554` | `true` |
| **G2 接口与探测** | `scripts/dalton/realrun/g2_providers.py` | `var/dalton/trial-runs/realrun-2026-10-21/g2_providers.json` | `2206b5a0a2f714b6ebc5077784ba09f86bc6668d56b07511b0f432dff31737e7` | `ad1b9544` | `realrun-pm-a,realrun-user-b` | `3` / `0` | `0` | `$0.000051` | `$0.000107` | `true` |
| **G3 发布核验** | `scripts/dalton/realrun/g3_publish.py` | `var/dalton/trial-runs/realrun-2026-10-21/g3_publish.json` | `3592f19902a593f72cfe55d4c76dc849cc5b0314eed006401158a6888b75ad74` | `ad1b9544` | `realrun-pm-a,realrun-user-b` | `5` / `145` | `6` | `$0.000000` | `$0.057986` | `true` |
| **G4 自主议程** | `scripts/dalton/realrun/g4_autonomy.py` | `var/dalton/trial-runs/realrun-2026-10-21/g4_autonomy.json` | `dff800544166e97b13eccc9e474aea8c2ecc7277a6fea7267c8ddab43f7064f4` | `ad1b9544` | `realrun-pm-a,realrun-user-b,realrun-idle-c` | `1` / `1` | `3` | `$0.000000` | `$0.001098` | `true` |
| **G5.1 Broker** | `scripts/dalton/realrun/g5_broker_and_antigravity.py` | `var/dalton/trial-runs/realrun-2026-10-21/g5_1_broker.json` | `6e46e5d23bb71f280b0adfcb8cbae4ca0d57c057f1cfa7059f84baa04e397ab6` | `ad1b9544` | `realrun-pm-a` | `0` / `0` | `0` | `$0.000000` | `$0.000000` | `true` |
| **G5.2 Antigravity** | `scripts/dalton/realrun/g5_broker_and_antigravity.py` | `var/dalton/trial-runs/realrun-2026-10-21/g5_2_antigravity.json` | `9af895219f20a4bc7f3733e366d282e1f5bbf7240dce6afa0b27ff9e80c79674` | `ad1b9544` | `realrun-pm-a` | `1` / `0` | `1` | `$0.000000` | `$0.000013` | `true` |
| **G6 财务镜头** | `scripts/dalton/realrun/g6_lenses.py` | `var/dalton/trial-runs/realrun-2026-10-21/g6_lenses.json` | `b273bf140be362ce1f7277f535f84a037071e0446cc555e1d5da163ffc210766` | `ad1b9544` | `realrun-pm-a` | `0` / `0` | `0` | `$0.000000` | `$0.000000` | `true` |
| **G7 宿主机桥** | `scripts/dalton/realrun/g7_bridge.py` | `var/dalton/trial-runs/realrun-2026-10-21/g7_bridge.json` | `6241544b38077aa63dd20449c6311e1fe1fbbf79316c3719694eb69edc0a9117` | `ad1b9544` | `realrun-pm-a` | `0` / `0` | `1` | `$0.000000` | `$0.000000` | `true` |

| G3 关键指标 | 证据实测值 |
|---|---|
| 金标准有来源数字总数 (`gold_sourced_total`) | `37` |
| 金标准真阳核实数 (`true_verified_in_gold_count`) | `31` |
| 金标准错放数 (`false_verified_in_gold_count`) | `0` |
| 金标准误判无来源/矛盾 (`false_unverified_in_gold_count`) | `1` |
| 金标准有来源判 `uncertain` (`sourced_uncertain_in_gold_count`) | `5` |
| `dalton_llm_journal` 新调用 / 回放 (`fresh_calls` / `replayed_calls`) | `5` / `145` |
| `dalton_llm_journal` 名义花费 (`notional_usd`) | `$0.057986` |
<!-- /generated -->

---

## 5. 与计划的偏差和已知限制

- **证据目录命名**：本轮证据目录名是 `realrun-2026-10-21`（带连字符），而 10-19 那轮使用的是 `realrun-20261019`；实跑脚本只把 `--date` 参数当作目录与文件标签使用，不影响校验逻辑。
- **G5.2 单独录制**：G5.2 因为 OAuth 回调需要人在 10 分钟内完成浏览器授权与粘贴，单独以 `--mode g5.2` 记录为 `var/dalton/trial-runs/realrun-2026-10-21/g5_2_antigravity.json`，没有合并的 G5 文件（PR #43 修了渲染）。
- **G3 第 1–5 步回放与第 6 步新调用**：G3 第 1–5 步的 LLM 调用回放了 `89b9a7e5` 上那次运行写入 `dalton_llm_journal` 的结果；`src/` 在 `89b9a7e5` 和 `ad1b9544` 之间关于发布核验提取与判定的逻辑一致，消费这些结果并产出核验报告的判定代码运行在 `ad1b9544` 上；证据摘要已分列窗口内新调用和回放，且第 6 步单节微调窗口内同时包含新调用和回放（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G3.md`）。
- **G4 场景②内部文献子场景待跑**：G4 场景②的“当日新增内部文献”子场景未完成，因运行当日没有新入库的内部资料，按任务书要求待有真实新资料的交易日再跑。
- **assist 缓存作用域对调试样本的消耗**：同一用户、同一输入的 assist 调用按设计永久回放（缓存键作用域为 `assist:{user}:{task}:{payload_sha}`）；因此正式录制证据前的调试运行会“用掉”对应的分诊样本（PR #40 已让 G4 脚本自动挑选当前用户未分诊过的样本避开）。
- **上游 Market Insight 浏览器偶发崩溃**：上游 Market Insight 服务在后端启动时自动补跑 `pre_market` 任务，其进程内无头浏览器崩溃曾导致后端无响应一次（遵循最小侵入原则未修改上游代码，在此如实记录）。
- **G1 花费统计中的历史回放行修正**：G1 的 `$0.50` 花费上限判断里，本轮修正了 `scripts/dalton/realrun/journal_dump.py` 把命中历史缓存的回放行初次花费算进本次运行窗口的问题（PR #42）。

---

## 6. 流程

J5 起的代码改动由 Antigravity 开发（少数小修由 lead 直接编写），grok 与 Muse 辅助审查；真实运行由 lead 在沙箱外执行；本轮 PR 由 lead 审查后合并。
