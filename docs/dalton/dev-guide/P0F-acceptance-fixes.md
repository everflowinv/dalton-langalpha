# P0F 验收修正：第一轮代码的问题清单

版本 0.1，2026-10-18。lead 对 `dalton/main` `7670a03f` 第一轮验收的结论和修正步骤。

**结论：** 代码都已写完，复跑结果属实：Python `tests/unit/dalton` 72 项通过，broker Node 测试 10 项通过，broker 健康检查和 researchdata 桥接都正常，最近一次 broker 冒烟测试 13 项通过、1 项警告、0 项失败。但有 6 个问题必须修，修完才能进入 P0.9/P0.10 的真实运行验收。

| 编号 | 问题 | 严重程度 | 工时 |
|---|---|---|---|
| F1 | 20 个提交没有推送到远端 | 高，有丢失风险 | 0.1 |
| F2 | 挂钩检查不通过：上游的模型清单被直接修改 | 高，违反约定 | 并入 P0N.2 |
| F3 | 自动任务里出现次数限制 `max_failures`，而且上游默认就有"连续失败 3 次停用" | 高，违反硬原则 | 1 |
| F4 | PROGRESS.md 的 wiki 数据错误 | 中 | 0.5 |
| F5 | "✅ 已完成"没有区分代码完成、真实运行验证、验收通过 | 中 | 0.5 |
| F6 | 冒烟测试 B8 的警告没有说明 | 低 | 0.25 |

---

## F1 推送到远端

**目标：** 远端 `origin/dalton/main` 与本地一致，以后每次合并后立即推送。

**前置依赖：** 无。

**改动/新增文件：** 无代码改动。

**具体做法：**
1. `git push origin dalton/main`。
2. 把 8 个功能分支也推上去：`git push origin feat/P0.1-platform-env feat/P0.2-3-broker-v2 feat/P0.4-8-llm-overlay-and-budget feat/P0-D.1-6-data-and-sec feat/P1.1-4-onboarding-and-migration feat/PK.1-13-knowledge-and-publish feat/PK.14-20-autonomy-and-ui feat/PS.0-18-skills-and-workflows`。
3. 约定补充到 README 第 5 节：**每次合入 `dalton/main` 后立即推送；不在本机保留未推送的已合并提交。**

**验证方式：** `git fetch origin && git rev-parse dalton/main origin/dalton/main`，两个值相同。

**验收标准：** lead 在 GitHub 上看到 `dalton/main` 的最新提交与本地一致，8 个功能分支都在。

**风险与注意：** 推送前确认没有把 `.env`、令牌、`var/dalton/` 下含业务内容的运行记录加进提交。`var/` 应该在 `.gitignore` 里，请顺便确认。

---

## F2 挂钩检查不通过

**目标：** `./scripts/dalton/check_hooks.sh --strict` 以**上游原版**为基线通过。

**前置依赖：** P0N.2（把原生供应商的清单改动移进 overlay，见 `P0N-native-models.md`）。

**改动/新增文件：**
- `scripts/dalton/check_hooks.sh`：基线改为上游
- `src/llms/manifest/models.json`、`src/llms/manifest/providers.json`：恢复为上游内容（由 P0N.2 完成）

**具体做法：**
1. **基线改为上游，不能用自己的提交。** 现在 PROGRESS.md 写的"检查通过"是以 `9a03d3ac` 为基线的，而那个提交本身就改了 `providers.json`，所以查不出问题。基线一律取 `git merge-base dalton/main upstream/main`；还没合并过上游时，就是 `8e0dce94`。`check_hooks.sh` 自动计算这个值，不接受手动传入基线。
2. **把清单改动移进 overlay：** 按 P0N.2 执行。
3. **工具说明快照的例外：** `tests/unit/mcp_servers/agent_docstring_lock.json` 新增了 `dalton_sec` 的条目。这是上游测试会自动扫描 `plugins/*/*_mcp_server.py` 导致的，允许保留，但要满足两点：
   - 在 P0 附录 A 的挂钩清单里登记为例外；
   - `check_hooks.sh` 校验这个文件相对上游**只有新增**、且新增的键都属于 `dalton_*` 服务。

**验证方式：** 在干净的检出上运行 `./scripts/dalton/check_hooks.sh --strict`。

**验收标准：**
- 输出 `OK`；
- `git diff $(git merge-base dalton/main upstream/main) -- src/llms/manifest/` 为空；
- 故意在 `providers.json` 里加一行，检查会报错。

**风险与注意：** 以后每次合并上游，基线都会自动前移，不需要改脚本。

**预估工时：** 0.25 天，不含 P0N.2。

---

## F3 去掉自动任务的次数限制

**目标：** Dalton 的自动任务不再因"连续失败 N 次"被停用。失败交给按时间的退避（PK.18）处理，并让分析师或研究总监知道。

**前置依赖：** PK.15、PK.18 已合入。

**改动/新增文件：**
- `src/server/database/automation_executions.py`：`_count_strike` 加挂钩 `# DALTON-HOOK: P0F.3 dalton-* automations never auto-disable on consecutive failures`
- `src/dalton/autonomy/schedule.py`：删掉 11 处 `max_failures`
- `src/dalton/tools/director_tools.py`：第 398 行附近，删掉 `max_failures=10`
- `scripts/dalton/bootstrap_watch.py`：第 82 行附近，删掉 `max_failures=spec.get(...)`
- `docs/dalton/dev-guide/automation-templates/` 下的模板：删掉 `max_failures`
- `src/dalton/autonomy/backoff.py`：补"执行失败"这一类退避原因（如果还没有）
- 新建 `tests/unit/dalton/test_automation_no_strike.py`

**具体做法：**

1. **为什么只删字段不够：** 上游 `src/server/models/automation.py` 第 499 行把 `max_failures` 的默认值定为 3（取值 1–100），`src/server/database/automation_executions.py` 的 `_count_strike` 在 `failure_count + 1 >= max_failures` 时把自动任务设为 `disabled`，原因记为 `max_failures`。不写这个字段，就按默认值 3 生效，比现在的 10 还严。

2. **挂钩：** `_count_strike` 的 SQL 里，停用条件改为：
   ```sql
   (%(fuse)s OR (failure_count + 1 >= max_failures AND name NOT LIKE 'dalton-%%'))
   AND status IN ('active', 'executing')
   ```
   - `failure_count` 照常累加，用于展示和退避计算；
   - `fuse`（供应商拒绝了凭据，原因记为 `provider_auth`）照常立即停用。这不是次数门：凭据失效时重试只会一直失败，应该停下来让 owner 处理；
   - 非 `dalton-` 开头的自动任务保持上游行为。

3. **失败后的处理：** `dalton-*` 自动任务执行失败时：
   - 由 PK.18 的退避逻辑推迟下一次运行，按时间计算，例如连续失败的总时长的一半，最短 1 小时、最长 7 天；成功一次即清零。不用次数作为停止条件；
   - 在对应公司的 `agenda.md` 记一条"自动任务 X 失败：原因、下次重试时间"，研究总监每日调度时能看到；
   - 如果失败原因是确定性的，比如配置错误、引用了不存在的 skill，研究总监应该修正或删除这个自动任务，而不是让它一直退避。

4. **清理：** 删掉 `src/dalton/`、`plugins/dalton*/`、`workflows/`、`scripts/dalton/` 和模板里所有的 `max_failures`。`dalton-agenda` skill 里"严禁写入 `max_failures`"的说明保留，改为说明原因：不写也不会被停用，退避由系统处理。

5. **单元测试**（锁定这个契约）：
   - `dalton-x` 自动任务连续失败 20 次，状态仍是 `active`，`next_run_at` 按退避推迟；
   - 同样的情况换成非 `dalton-` 名字，第 3 次失败后被停用（上游行为不变）；
   - `fuse=True` 时，`dalton-x` 也会被立即停用，原因记为 `provider_auth`。

**验证方式：**
- `grep -rn "max_failures" src/dalton plugins/dalton plugins/dalton_data workflows scripts/dalton docs/dalton/dev-guide/automation-templates` 除 skill 说明外没有结果；
- 跑新的单元测试；
- P0 环境起来后，人为制造一个会失败的 `dalton-test` 自动任务，观察连续失败 5 次后仍是 `active`，下次运行时间在推迟，`agenda.md` 里有失败记录。

**验收标准：** 以上三项全部满足，挂钩已登记进 P0 附录 A。

**风险与注意：** 不要改上游 `max_failures` 的默认值和取值范围，只在 `_count_strike` 里按名字前缀豁免，保证合并上游时冲突最小。

**预估工时：** 1 天。

---

## F4 修正 wiki 覆盖评估

**目标：** P0-D.4 的 wiki 覆盖评估基于真实数据，并能说明覆盖名单里每家公司在 wiki 里有没有资料。

**前置依赖：** P1.2 的 `config/dalton/coverage.yaml`。

**改动/新增文件：**
- `docs/dalton/dev-guide/PROGRESS.md`：第 2 节第 6 条
- 新建 `scripts/dalton/wiki_coverage_report.py`：只读

**具体做法：**
1. **实际规模：** `~/.openclaw/workspace/wiki/companies` 下有 174 个公司目录，`sectors` 下有 27 个行业目录（2026-10-18 lead 实测）。PROGRESS.md 写的"26 家公司与 8 篇专题"不对，请改正，并说明当时数的是什么。
2. **写报告脚本：** 对 `coverage.yaml` 里的 105 家公司逐家检查：
   - wiki 里有没有对应的公司目录；
   - 页面数量和最近更新日期；
   - 通过 researchdata 的 `company-wiki` 路由用公司名检索，第一页能不能命中自己的页面。

   输出到 `var/dalton/reports/wiki_coverage_<date>.md`。
3. **在 PROGRESS.md 里记录汇总：** 有多少家有页面、多少家能检索命中、哪些 Core 公司没有任何 wiki 资料。

**验证方式：** 运行脚本，抽查 5 家的结果和实际目录是否一致。

**验收标准：** 报告存在，数字和 lead 抽查一致；Core 27 家里没有 wiki 资料的公司已经单独列出。

**预估工时：** 0.5 天。

---

## F5 进度状态分三档

**目标：** PROGRESS.md 能一眼看出哪些只是代码写完，哪些真实跑过，哪些已经通过验收。

**前置依赖：** 无。

**改动/新增文件：** `docs/dalton/dev-guide/PROGRESS.md`

**具体做法：**
1. **状态改为三档，每一档都要附证据：**

   | 状态 | 含义 | 必须附的证据 |
   |---|---|---|
   | 🟦 代码完成 | 已合入 `dalton/main`，单元测试通过 | 提交 SHA、测试命令和结果 |
   | 🟨 真实运行验证 | 在真实环境里按"验证方式"跑过 | 运行记录的路径（`var/dalton/...`）、截图或对话片段、日期 |
   | 🟩 lead 验收通过 | lead 按"验收标准"复核通过 | 由 lead 填写：日期和结论 |

2. **按这个标准，现在的实际状态：**
   - 🟨：P0.2、P0.3（broker 技术验证和冒烟测试）、P0.5、P0-D.1（桥接）、P0-D.5 和 P0-D.6（真实 EDGAR 和一致预期对数）、P1.2、P1.4（迁移脚本实跑）；
   - 其余全部是 🟦；
   - 没有任何一项是 🟩。
3. **PS 系列的 skill 和 workflow，** 必须按 PS.0 规定的试跑记录格式真实跑一次，才能标 🟨。试跑记录放在 `docs/dalton/dev-guide/trial-runs/`。
4. **`✅ 已完成` 不再使用。**

**验证方式：** lead 逐行核对证据是否存在。

**验收标准：** 每一行都有对应档位的证据；没有证据的一律按 🟦 算。

**预估工时：** 0.5 天。

---

## F6 说明冒烟测试 B8 的警告

**目标：** 最近一次冒烟测试（`var/dalton/smoke/latest_broker.json`）里 B8 是警告，要查清原因并记录。

**具体做法：** 在 PROGRESS.md 第 2 节补一条：B8 检查的是什么、为什么是警告、是否影响使用、要不要处理。如果是 OpenClaw 自身的工具暴露给了模型，要按 P0.3 修掉，不能接受。

**验收标准：** PROGRESS.md 里有结论；如果需要修改，修改已合入，且重跑冒烟测试 B8 通过。

**预估工时：** 0.25 天。

---

## 修完之后

F1–F6 都合入并推送后，在 PROGRESS.md 里通知 lead。lead 复核这 6 项，然后进入：
1. Mac 重启、colima 配置（README 第 9 节）；
2. P0N 原生接入改造（`P0N-native-models.md`）；
3. P0.9 原生界面走查和 P0.10 验收。
