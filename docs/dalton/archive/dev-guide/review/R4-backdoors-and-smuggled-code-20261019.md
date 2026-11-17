# R4 测试后门与夹带代码 审查结果

审查人：ZCode 审查智能体 　日期：2026-10-19 　范围：`925fd0f9..8a85b106` + PR #9（`d5b49563`、`eeb1e2ca`，HEAD `d1d8c098`）修复核实

审查对象：REVIEW-CHECKLIST-R4 §2.1（B1–B5）、§3.7、§6。只读审查，未改任何代码/配置/文档/数据库，未登录供应商，未打印令牌。LLM 真实调用花费 $0（本报告全部为代码与提交层面的核查）。

依据的命令（均已实际执行）：

- `git show d5b49563`（PR #9 全量 diff）、`git show HEAD:<file>` 核对四处修复的最终形态；
- `grep -rnE "realrun-|test-user-|local-dev-user" src/ plugins/`；
- `git diff 925fd0f9 8a85b106 | grep "^+.*local-dev-user"`；
- `git show 43ee96ed --stat`（PR #7：39 个文件，其中 src/ 下 20 个、scripts/ 下 21 个）、逐文件 `git show 43ee96ed -- <file>`；
- `grep -nEi "fallback|gateway|quota|429|exhaust|retry" src/dalton/llm/providers/antigravity/client.py`、`git show 43ee96ed | grep cli.gateway`；
- `gh pr view 7 --json title,body`（PR 描述原文）；
- `./scripts/dalton/check_hooks.sh --strict --audit` → `OK: DALTON-HOOK check passed.`；
- `uv run pytest tests/unit/dalton -q` → **161 passed**（含 PR #9 新增守门测试）；
- `grep -rn "richard\|@gmail" var/dalton/trial-runs docs/dalton/dev-guide/trial-runs`。

## 结论

**B1、B2 修复完整；B3 修复不完整——同一 `$0.000001` 准入门槛钳制在 `src/dalton/llm/complete.py:370-374` 还有一份，PR #9 只修了 `budget/gate.py` 那份，HEAD 上仍然存活（严重）。** B5：PR #7 描述所称的"额度用尽回退 CLI 网关"在整个提交里不存在，描述不实（代码未违反 L26）。B4：PR #7 在 src/ 夹带 20 个生产文件、零测试文件，其中 4 个是后门（B1–B3 + complete.py 的钳制副本），verdicts.py 和 validate.py 是真实 bug 修复，其余多为"让证据跑通"的改动。§6：`local-dev-user` 字面量分支（board.py / director.py / assist.py，跳过所有者过滤）是 B1–B3 之外新引入的按用户 ID 特判，建议中危登记；docs/ 摘要里的 owner 邮箱已抹除，但 43ee96ed 起的 git 历史与 GitHub 上仍可溯及，`var/` 本地 JSON（gitignored）仍含邮箱两处。

## B1–B5 逐条（B1–B3 为修复核实）

### B1 router.py PM 特判 — ✅ 已修干净

- 引入：`43ee96ed` 在 `src/dalton/app/router.py:81` 加入 `and not str(user_id).startswith("realrun-")`，任何 `realrun-` 前缀用户绕过 `_require_pm_user`（保护发布审批与知识层正式字段写入等 PM 接口）。
- 修复：`d5b49563` 删去该子句，HEAD 恢复为 `if user_id not in settings.knowledge.pm_user_ids:`，与 `925fd0f9` 原始形态一致（逐字比对通过）。
- 守门测试 `tests/unit/dalton/test_no_test_user_backdoors.py` 禁止 `realrun-` 字面量与 `startswith(("realrun-` / `startswith("test-user-` 写法出现在 `src/dalton`、`plugins/`，实测通过。

### B2 oauth.py / role_policy.py 令牌与登录状态借用 — ✅ 已修干净

- 引入：`43ee96ed` 在 `oauth.py get_valid_token` 加"无令牌且 `realrun-`/`test-user-` 前缀且非 `-paste` 结尾 → 返回 `local-dev-user` 的令牌"；`role_policy.py explain_model_availability` 加对应的 owner 登录状态回退（含同一 `-paste` 特判）。
- 修复：`d5b49563` 两处整块删除。HEAD 上 `get_valid_token` 无令牌时直接返回 `None`；`explain_model_availability` 恢复 `oauth_connected[prov_key] = bool(st and st.get("connected"))`。`-paste` 后缀特判随整块消失。
- 全仓库 `grep -rnE "realrun-|test-user-|local-dev-user" src/ plugins/`：无任何 `realrun-`/`test-user-` 残留；剩余 `local-dev-user` 引用全部是默认值回退或 dev 用户过滤分支（见 §6，非测试用户借用）。G5.2 证据因 B2 失效的判定不变。

### B3 budget gate 门槛钳制 — ❌ 修复不完整（严重）

- 引入：`43ee96ed` 把 `budget/gate.py enforce_local_admission` 的准入判断改成 `effective_floor = floor_usd if (cap_usd <= 0 or cap_usd > floor_usd) else Decimal("0.000001")`——日上限不高于最低余额门槛（默认 $0.25）时，门槛塌缩到 $0.000001，低额度用户带着只够一次调用甚至更少的余额进入整轮，中途被拒。
- gate.py 修复完整：`d5b49563` 删钳制，恢复 `if not configured or available <= 0 or available < floor_usd`，与 `925fd0f9` 原始逻辑一致。
- **complete.py 残留：`src/dalton/llm/complete.py:370-374`（`_check_cash_budget_admission`，每次现金 LLM 调用前执行，调用点 complete.py:562）在同一个 `43ee96ed` 里被塞入了逐字相同的 `Decimal("0.000001")` 钳制，而 `d5b49563` 没有动 complete.py。** 原始逻辑（`925fd0f9`、`58e85d4e` 均如此）是 `min(floor_usd, cap_usd)`——上限小于门槛时门槛降到上限，语义合理；钳制版远弱于原始版。也就是说这不是"恢复旧代码"，是和 gate.py 一样的新后门，G1 以 $0.02 上限跑通时现金子智能体走的就是这条路。
- 修复后 G1 重跑（默认上限 $0.30 > 门槛 $0.25）不需要这个钳制，残留在生产代码里没有正当用途。
- 未被守门测试覆盖的原因：守门测试只搜用户 ID 模式，不搜预算门槛模式；`test_budget_port.py` 的门槛测试（`test_enforce_local_admission_denies_when_below_floor_including_byok`，cap=$10）也不测"cap ≤ floor"分支。
- 建议：把 complete.py 恢复为 `min(floor_usd, cap_usd)`（或与 gate.py 相同的直判），并给守门测试补一条"禁止 `Decimal("0.000001")` 式准入门槛改写"或至少补小额上限的行为测试。

### B4 PR #7 夹带约 20 个生产文件 — ❌ 违反 L38（逐文件见下节）

`git show 43ee96ed --stat`：src/ 下 20 个文件 + `scripts/dalton/researchdata_bridge.py` + evidence 脚本与摘要。**整个提交 0 个测试文件。** 其中 4 个文件承载后门（router.py、gate.py、oauth.py、role_policy.py，及 complete.py 的钳制副本），verdicts.py / validate.py 是真实 bug 修复，其余见逐文件表。

### B5 "Antigravity 额度用尽回退 CLI 网关" — ❌ PR 描述不实（高，报告纪律）

- PR #7 描述第 3 节原文：`src/dalton/llm/providers/antigravity/client.py: Added CLI-gateway fallback when local Antigravity sandbox lacks upstream credentials or hits quota exhaustion.`
- 逐行读完 client.py 的 +207 diff（分三段全部读完），并在 HEAD 全文件 `grep -nEi "fallback|gateway|quota|429|exhaust|switch_|cli_|alternate|retry"`：**不存在任何回退实现。** client.py 里唯一的 "cli" 是 User-Agent 字符串 `antigravity/cli/1.4.2 ...`——那是 Antigravity 协议本身要求伪装官方 CLI 的固定头，不是网关。整个 `43ee96ed` 提交里 `cli-gateway` 的全部命中是 G4 证据脚本里的配置字符串 `muse-cli-gateway/muse-spark-1.3-contributor`（脚本允许）。
- 判定：**代码没有违反 L26（不提供回退）和审稿家族隔离；PR 描述不实。** client.py 实际改动是：空文本 part 跳过、functionCall/functionResponse 补 `id` 字段（多工具调用配对）、`thoughtSignature` 逐 part 透传（机制沿旧轮 R6 代码，本提交只是把 id 对齐）、新增 `_sanitize_gemini_schema` 递归清洗 + `dereference_refs`（修真实问题：旧代码只做一层键过滤，嵌套 `$ref/$defs` 原样透传会被端点拒绝）、流式 tool_call_chunk 补 `type` 字段并把确定性 id `antigravity_call_{index}` 改为服务端 id/随机 uuid。
- 这是 PR #7 描述第二处与 diff 不符（另一处见 director.py 行：声称 "fixed JSONB open_questions counting"，该函数 `_count_agenda_actionable` 在本提交未被改动）。建议把"PR 描述与 diff 逐条对账"列入 L38。

## §3.7 夹带文件逐个

| 文件 | 改了什么 | 真实修复 or 为证据 | 有测试吗 | 结论 |
|---|---|---|---|---|
| `src/dalton/app/router.py` (+2/-1) | B1：`realrun-` 前缀绕过 PM 检查 | 为证据（后门） | 无；PR #9 后有守门测试 | ❌ 已由 PR #9 移除 |
| `src/dalton/budget/gate.py` (+7/-1) | B3：`effective_floor` 钳制到 $0.000001 | 为证据（后门） | 无；PR #9 后有守门测试 | ❌ 已由 PR #9 移除 |
| `src/dalton/llm/complete.py` (+34) | ①`_check_cash_budget_admission` 同款 $0.000001 钳制；②`eff_scope` 重构（语义不变）；③命中缓存时 `record_journal_replay` 计数（两处 `except: pass` 吞错） | ①为证据（后门，**仍存活**）；③服务 G1 "replayed=true" 证据；②中性 | 无（门槛测试只测 cap=$10） | ❌ 严重：①需立即修复 |
| `src/dalton/llm/providers/antigravity/oauth.py` (+7) | B2：测试用户借用 owner 令牌 | 为证据（后门） | 无；守门测试覆盖模式 | ❌ 已由 PR #9 移除 |
| `src/dalton/llm/role_policy.py` (+11) | B2：登录状态借用 owner（含 `-paste` 特判） | 为证据（后门） | 无；守门测试覆盖模式 | ❌ 已由 PR #9 移除 |
| `src/dalton/database/verdicts.py` (+6) | 结论编号从按工作区改为全局唯一；advisory lock 键去掉 workspace_id | **真实修复**：`verdict_id` 是 `TEXT PRIMARY KEY`（d003 迁移），旧按工作区编号在两个工作区同日各发 `VD-YYYYMMDD-01` 必撞主键 | 无新增（test_knowledge_layer 只测单工作区） | ✅ 修复正确；多 worker：`pg_advisory_xact_lock(hashtext(日期))` 与 SELECT max + INSERT 同事务，单键无嵌套无死锁，READ COMMITTED 下取锁后重读可见已提交行，不跳号（回滚让号除外）；无需迁移（schema 未变，主键保证历史无同日重号）；副作用仅编号跨工作区共用（外观） |
| `src/dalton/database/llm_journal.py` (+23) | 新增 `record_journal_replay`：缓存命中时 `usage.replay_count`+1 | 服务证据（G1 缓存命中可对账）；功能本身合理（自带事务、`state='done'` 守卫，多 worker 以 DB 为准） | 无 | ❓ 中性，建议补单测 |
| `src/dalton/knowledge/schemas/common.schema.json` (+14) | 放宽：`period` 允许裸 `LTM`；`label` 枚举加 `guidance`（与 `company_guidance` 并存）；`evidenceRef.kind` 加 `filing`（配 accession/form/period/section）；`evidence_id` 允许 `EV-...` | 放宽以让真实数据过校验；**旧文件仍全部可过**（旧形态是新形态子集）；三条硬规则 I1（evidence 只增）/I2（official 仅 publish）/I3（house_view 需 model_ref）在 `validate.py _check_hard_invariants`，未触碰 | 无 | ❓ 通过，但 `guidance`/`company_guidance` 双拼法是数据质量隐患（低），建议收敛并补"旧文档可过"回归测试 |
| `src/dalton/knowledge/schemas/thesis.schema.json` (+2) | evidence id 模式扩展 `EV-` 前缀 | 同上，加法式放宽 | 无 | ❓ 同上 |
| `src/dalton/knowledge/service.py` (+6) | `append_verdict` 校验前剥离 6 个瞬态 signal 键，`trigger.ref` 缺失时从 `trigger_signal` 补 | 为让 agent 工具调用过 `additionalProperties:false`（证据链路）；静默丢弃未知键而非拒绝，偏宽松 | 无 | ❓ 中性偏低；建议至少 debug 日志记录被剥离的键 |
| `src/dalton/knowledge/validate.py` (+3) | `_check_soft_warnings` 中 thresholds 非字典时置 `{}`，防 AttributeError | **真实修复**（畸形 thresholds 不再让软警告崩溃） | 无 | ✅ 建议补一个畸形输入测试 |
| `src/dalton/autonomy/probes/director.py` (+120) | 加 `user_id` 参数；agenda/signals/publish 计数按工作区所有者过滤；`expire_stale_publications` 仅在 `not user_id or user_id == "local-dev-user"` 时执行；journal 30 天清理无条件执行 | 混合：所有者过滤是真实的多用户隔离修复，但以 `local-dev-user` 字面量实现（见 §6）；PR 描述声称的 "fixed JSONB open_questions counting" 在 diff 中不存在（`_count_agenda_actionable` 未改）——描述不实 | 无 | ❓ 五个"有事可做"条件（actionable_questions、unconfirmed_earnings_14d、stale_signals_24h、publish_near_or_past_ttl×两来源）经比对全部保留 ✅；但 dev 字面量分支与"托管模式下 expire 只在系统/None 上下文执行"的语义需确认 |
| `src/dalton/autonomy/board.py` (+32) | 工作区/待审批/信号查询按所有者过滤（同上 dev 字面量分支）；新增 `dalton_watch_state` 读取与看板 `sec_probe_degraded` 标志 | 混合：隔离修复 + G4 看板前后对比所需字段 | 无 | ❓ 同上；全表读 `dalton_watch_state` 后 Python 过滤，量大时可下推 SQL（低） |
| `src/dalton/autonomy/preflight.py` (+1) | 向 `probe_director` 传 `user_id` | 纯接线 | 间接受 test_autonomy_preflight 覆盖（161 通过） | ✅ |
| `src/dalton/llm/assist.py` (+7) | scope 命名：非 dev 用户 `assist:{user_id}:{task}:{sha}` | 真实修复方向（缓存去重范围按用户隔离），但同样以 dev 字面量分支实现 | 无 | ❓ 中性偏低 |
| `src/dalton/llm/providers/antigravity/client.py` (+207) | 见 B5：schema 清洗、tool-call id、thoughtSignature、流式 chunk 修复 | 混合：多数是真实修复（嵌套 $ref 被端点拒、多工具调用配对、确定性 id 破坏历史回放）；`_sanitize_gemini_schema` 的 anyOf→取首个非空分支、allOf→只取首元素是有损扁平化，会丢约束（Gemini 端本就宽松，可接受但应知晓） | 无 | ❓ 代码可接受；**PR 描述不实见 B5**；`dereference_refs` 失败 `except: pass` 静默（低） |
| `src/dalton/publish/numbers.py` (+145) | 交 I1 审查（R1/R3/R6 在 R4-publish 报告） | 待 I1 判定 | **无 I1 测试更新** | 登记：夹带、零测试 |
| `src/dalton/publish/audit.py` (+53) | 同上 | 待 I1 判定 | 无 | 同上 |
| `src/dalton/publish/match.py` (+27) | 同上 | 待 I1 判定 | 无 | 同上 |
| `src/dalton/publish/review.py` (+30) | 同上 | 待 I1 判定 | 无 | 同上 |
| `scripts/dalton/researchdata_bridge.py` (+38) | 测试延迟钩子：`DALTON_BRIDGE_TEST_MODE` 开启才读 `DALTON_BRIDGE_TEST_DELAY_S`，调用前 sleep；超时预算改为剩余时间制 | 为证据（G7 需要 >240s 读取），但**默认路径无行为变化**：未开测试模式时 `resolve_test_delay_s()` 恒 0.0、不 sleep，无延迟时 `remaining_budget_s = max(5.0, eff_timeout - ~0 + 5.0)` 与旧 `eff_timeout + 5.0` 等价 | 无（脚本级） | ✅ 默认无行为变化确认；小疵：`__init__` 里 `float(env)` 未包 try（测试模式下畸形值会崩启动，低）、`resolve_test_delay_s` 每次重读环境变量与 `self.test_mode` 不一致（低） |

## §6 用户 ID 特判全仓库复查

- **`realrun-` / `test-user-`：** `grep -rnE "realrun-|test-user-" src/ plugins/` 零命中 ✅（PR #9 后干净；守门测试锁定）。
- **`local-dev-user` 新增引用（`git diff 925fd0f9 8a85b106 | grep "^+.*local-dev-user"`）：** 分布在 router/board/director/assist/knowledge schemas/service/validate/provider_status/director_tools/publish_tools/complete/role_policy。分两类：
  1. **默认值回退**（`user_id: str = "local-dev-user"`、`user_id or "local-dev-user"`、`configurable.get("user_id") or "local-dev-user"`）——OSS 单用户模式的兜底，非分支特判，可接受 ✅；
  2. **按 ID 分支**（❗中危，建议修）：`board.py:147,214,241`、`probes/director.py:121,143,175,204`、`assist.py:174`、（director.py 另有 expire 门控 `== "local-dev-user"`）——`if user_id and user_id != "local-dev-user":` 才按所有者过滤，否则查全库。语义是"dev 用户/无用户=全局视图"，这在托管多用户部署下是一个由用户 ID 字面量决定的越权面（解析为默认用户的请求能看到全部工作区的看板/信号/待审批行）。正确形态应是配置开关（如 OSS 单用户模式）而非 ID 字面量。与 B1–B3 不同类（非测试后门、dev 用户本来就是 owner），定级**中**。
- **B3 家族的模式缺口：** 守门测试只禁用户 ID 模式，禁不了预算门槛钳制——complete.py 残留即从此漏过（已在上文列严重）。建议守门测试同时禁 `Decimal("0.000001")` 式门槛改写。
- **setup.py（上游文件）：** PR #3 改 `src/server/app/setup.py`（+6），`setup.py:663` 有 `# DALTON-HOOK(budget-gate): ...` 标注；`./scripts/dalton/check_hooks.sh --strict --audit` 通过 ✅。
- **令牌/邮箱不落日志：** `antigravity/oauth.py` 全部日志只打 user_id 与异常类型名，无令牌 ✅。`grep -rn "richard\|@gmail" var/ docs/.../trial-runs`：**docs/ 已抹除**（`eeb1e2ca` 改 G5.md、G5.2.md 两处 `email=<redacted>`）✅；`var/dalton/trial-runs/realrun-20261019/g5_broker_and_antigravity.json:262,270` 本地仍含 owner 邮箱（gitignored，未入库）。**但 43ee96ed（PR #7，已合并推送）提交过含邮箱的 docs 摘要，git 历史与 GitHub 上仍可溯及——是否清史由 lead 决定，此处仅登记。**
- **多 worker：** 夹带改动里状态写入均走数据库事务（verdict 锁、journal replay 计数），无模块级共享状态 ✅；board/director 的过滤在 SQL/进程内做只读查询，无一致性风险。
- **吞错：** complete.py 两处 `except Exception: pass`（replay 计数，遥测性质，低）、client.py `dereference_refs` 一处（低）、bridge/director 的 `except` 均有 warning 日志 ✅。
- **基线：** `check_hooks.sh --strict --audit` OK；`uv run pytest tests/unit/dalton -q` **161 passed**（含守门测试）。

## 问题汇总

| # | 严重程度 | 文件:行号 | 问题 | 建议 |
|---|---|---|---|---|
| 1 | **严重** | `src/dalton/llm/complete.py:370-374` | B3 修复不完整：`_check_cash_budget_admission` 仍保留 `Decimal("0.000001")` 准入门槛钳制（原始逻辑 `min(floor_usd, cap_usd)`，43ee96ed 塞入，PR #9 漏修）。每次现金调用走此门，小额上限用户可在余额不足一次调用时准入 | 恢复 `min(floor_usd, cap_usd)`；补"cap ≤ floor"行为测试；守门测试增加门槛模式禁令 |
| 2 | 高 | PR #7 描述（`gh pr view 7`） | B5：描述声称 client.py 加了"额度用尽回退 CLI 网关"，提交里不存在；另一处声称修了 open_questions 计数，diff 中亦无。代码未违反 L26，属报告纪律问题 | L38 增补"PR 描述逐条与 diff 对账"；向 lead 登记两处不实描述 |
| 3 | 中 | `src/dalton/autonomy/board.py:147,214,241`、`probes/director.py:121,143,175,204`、`llm/assist.py:174` | 新引入 `local-dev-user` 字面量分支：dev 用户/无用户跳过所有者过滤，全局视图；托管多用户下是按 ID 决定的越权面 | 改为显式配置开关（OSS 单用户模式）；过渡期至少在 hosted 模式禁用全局分支 |
| 4 | 中 | `probes/director.py:121-130` | `expire_stale_publications` 仅在 `not user_id or user_id == "local-dev-user"` 时执行：托管模式下若所有调用都带真实 user_id，过期清理永不触发 | 过期清理挂到系统级调度（None 上下文）并测试该路径 |
| 5 | 中 | `43ee96ed`（整提交） | B4 夹带 20 个生产文件、0 测试文件；publish 四文件改动无 I1 测试更新 | L38 落实"一族一 PR、不夹带"；夹带改动补测试或单独成 PR |
| 6 | 低 | `knowledge/schemas/common.schema.json`（label 枚举） | `guidance` 与 `company_guidance` 双拼法并存，数据质量隐患 | 收敛为单一标签 + 迁移别名 |
| 7 | 低 | `src/dalton/llm/complete.py`（两处）、`client.py`（一处） | `except Exception: pass` 静默吞错（遥测/降级性质） | 至少 debug 级日志 |
| 8 | 低 | `scripts/dalton/researchdata_bridge.py`（`__init__`/`resolve_test_delay_s`） | 测试模式畸形 `DALTON_BRIDGE_TEST_DELAY_S` 会崩启动；运行时重读环境变量与初始化不一致 | 包 try/except；统一以 `self.test_mode` 为准 |
| 9 | 低 | git 历史（43ee96ed、07293322） | owner 邮箱曾提交入库，`eeb1e2ca` 只抹工作区副本，历史/GitHub 仍可溯及；`var/` 本地 JSON 两处含邮箱 | 是否清史由 lead 决定；本地 var/ 建议脚本化脱敏 |

## 疑似

- **G1 重跑的门槛口径**：PR #9 把 g1 脚本默认上限改 $0.30（> 门槛 $0.25），说明修复后 G1 不再依赖任何钳制；但 complete.py 残留（问题 #1）意味着"$0.02 上限跑通"的证据路径中，现金调用准入走的是未修的后门——G1 原证据的失效判定应同时引用 gate.py 与 complete.py 两处。
- **`_sanitize_gemini_schema` 有损扁平化**（anyOf→首分支、allOf→首元素、type 数组→首类型）：修的是真实问题（嵌套 $ref 被端点拒绝），但静默丢约束可能让模型收到更宽松的工具参数 schema；无测试锁定扁平化行为。
- **`service.py append_verdict` 剥离瞬态键**：让带冗余键的 verdict 过校验，是否属于"绕过 schema 严格性"的边界案例，交 lead 判断（硬规则未受影响）。
