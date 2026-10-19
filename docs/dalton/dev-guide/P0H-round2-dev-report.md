# P0H 第二轮审查修复与真实运行验证开发报告 (P0H-round2-dev-report.md)

- **报告日期**：2026-10-18
- **对应开发指南**：`docs/dalton/dev-guide/P0H-round2-fixes.md`（决定 `L28`–`L33`）
- **主分支最新提交**：`18bd3d8e` (`dalton/main`)

---

## 1. 开发流程执行总览（决定 `L32`、`L33`）

本轮严格执行 `P0H §9` 四条硬性要求：
1. **一族一个分支、一个 Merge PR（`git merge --no-ff`）**；
2. **每个族附真实运行验证证据**，未完成人工浏览器 OAuth 交互的项（`G5.2`）诚实保持 `🟦 代码完成`，零虚报；
3. **测试替身全部声明参数签名（`spec=` / `create_autospec`）**，全面禁止无约束 `mock(**kwargs)`；
4. **守门测试（`tests/unit/dalton/test_router_wiring.py`）实际调用每个接口背后的服务函数**（仅在 DB/连接池层使用 `create_autospec` 替身）。

| 族 | 分支名 | 功能提交 | Merge Commit (`dalton/main`) | 状态 | 真实运行证据路径 |
|---|---|---|---|---|---|
| **H7** | `fix/P0H.H7-progress-truth` | `425f7551` | `355e3a37` | 🟨 真实运行验证 | `docs/dalton/dev-guide/PROGRESS.md` |
| **H4** | `fix/P0H.H4-providers-roles` | `90a1d94e` | `588dfc95` | 🟨 真实运行验证 | `var/dalton/smoke/g2_api_and_probes_20261018.json` |
| **H5** | `fix/P0H.H5-budget-journal` | `4fe42f87` | `1e4f3851` | 🟨 真实运行验证 | `var/dalton/smoke/g1_budget_realrun_20261018.json` |
| **H1** | `fix/P0H.H1-publish-redesign` | `dabe0b7f` | `c1e76363` | 🟨 真实运行验证 | `var/dalton/reports/g3_real_llm_audit_20261018.json`、`docs/dalton/dev-guide/trial-runs/G3-real-report-audit-20261018.md` |
| **H3** | `fix/P0H.H3-autonomy-chain` | `16006401` | `aa1d1922` | 🟨 真实运行验证 | `var/dalton/smoke/g4_autonomy_realrun_20261018.json` |
| **H2 + H6** | `fix/P0H.H2-H6-realrun-evidence` | `a725c47f` | `18bd3d8e` | 🟨 真实运行验证 | `var/dalton/smoke/{g1,g2,g4,g5,g7}_*.json`、`var/dalton/reports/g3_*.json`、`var/dalton/lenses/*_20261018.json` |

---

## 2. H1：发布核验的职责重新划分与代码锚定（决定 `L28`–`L31`）

### 2.1 四步流水线职责划分

| 步骤 | 谁来做 | 能产生什么结论 | 关键实现 |
|---|---|---|---|
| **第一步：抽取数字** | 确定性代码 (`src/dalton/publish/numbers.py`) | 候选数字（掩码排除代码块、URL、日期、SEC accession 号、编号等），继承表格列头与行头单位/币种 | 彻底删除约 95 行中文数字正则（`_CN_COLLOQUIAL_MULTI_RE`、`_CN_NUMBER_SEQ_RE`、`_chinese_numeral_to_float`）及整行标签判定 `is_consensus_labeled_line` |
| **第二步：标注数字** | 必经 LLM (`audit.label_figures`) | 为每个数字标注 `metric`、`period`、`unit`、`value_type`（`actual` / `house_estimate` / `consensus` / `guidance` / `assumption`），并补抽“两成”、“百分之三十”、“腰斩”、“过半”等口语中文数值表达 | 每份报告必经 `assist_json(task="audit.label_figures")`（按 40 个数字分批调用）；若任一批次失败则标记 `labeling_incomplete=True`，在审批卡顶部展示琥珀色警告横幅交 PM 裁决，绝不退回规则猜测（`L31`） |
| **第三步：脚注匹配** | 确定性代码 (`src/dalton/publish/match.py`) | `verified`（已核实）；`contradicted`（有矛盾，**仅限显式脚注指向的证据数值经单位换算后确实不同**） | **除显式脚注指向的证据数值确实不同之外，代码绝不独立判出“有矛盾”**（`L28`）；已删除不经 LLM 直接判硬冲突的 `consensus_confusion` 分支（`L30`） |
| **第四步：其余数字判断 + 代码锚定** | LLM (`audit.judge`) + 代码逐字锚定 (`match.py`) | `verified`、`contradicted`、`unverified`、`uncertain`、`exempt` | 冻结 `_METRIC_SYNONYM_GROUPS` 仅用于粗筛最多 5 条候选来源；LLM 仅返回 `{verdict, matched_source_id, quote, quote_number_raw, explanation}`（已从 Schema 删除 `source_value` 与 `source_unit`），代码在原文 `quote` 中逐字定位并亲自解析 `quote_number_raw` 做数值比对（`L29`） |

### 2.2 严重与高危问题修复核对表

| 问题 | 修复文件 | 修复内容 |
|---|---|---|
| **严重：LLM 报回的 `source_value` 未在引文中核实（提示词注入漏洞）** | `src/dalton/publish/match.py` | 从 `_LLM_JUDGE_SCHEMA` 删除 `source_value` 和 `source_unit`，改为必填 `quote_number_raw`；代码验证 `quote` 逐字存在于候选来源原文，验证 `quote_number_raw` 逐字存在于 `quote`，并调用确定性解析器从 `quote` 中抽出数值和单位与草稿比对 |
| **高危：LLM 判 `contradicted` 时未做代码锚定** | `src/dalton/publish/match.py` | `verdict == "contradicted"` 同样强制要求 `quote` 与 `quote_number_raw` 锚定成功且代码解析出的引文数值确实与草稿数值不等；锚定失败时一律降级为 `unverified`（无来源），绝不凭空判为“有矛盾” |
| **高危：LLM 判 `exempt` 时无锚定** | `src/dalton/publish/match.py` | 新增 `has_verbatim_exempt_marker(sentence)`，要求草稿原句中必须逐字存在 `[估计]`、`[假设]`、`[测算]`、`[敏感性]`、`[情景]` 等显式标记，否则降级为 `unverified` |
| **高危：第二步 LLM 标注器在生产路径从未被调用** | `src/dalton/publish/audit.py`、`service.py` | 新增异步主入口 `aaudit_draft` 并在 `check_draft` 与 `publish_report` 中统一调用，确保每份报告必经 `maybe_label_prose_figures_with_llm` |
| **审批卡展示 `labeling_incomplete`** | `src/dalton/publish/service.py`、`PublishApprovalCard.tsx` | 当 `labeling_incomplete=True` 时，在 PM 审批卡显著位置渲染琥珀色提示条：“标注未完成，以下核验结果可能不完整，请 PM 人工复核决定” |

### 2.3 代码行数统计与正则清单（`P0H §1.7`）

- **`src/dalton/publish/` + `src/dalton/knowledge/` 行数统计**：
  - 删除：**535 行**
  - 新增：**447 行**
  - **净变化：`-88 行`**
- **保留/新增的正则清单（全部为掩码类与结构定位类，零领域词表正则）**：
  1. `_CODE_FENCE_RE` / `_INLINE_CODE_RE`：掩码排除代码块与行内代码；
  2. `_URL_RE` / `_FOOTNOTE_REF_RE` / `_FOOTNOTE_DEF_RE`：掩码排除 URL、脚注引用与脚注定义行；
  3. `_SEC_ACCESSION_RE`（`\b\d{10}-\d{2}-\d{6}\b`）与 `_HYPHEN_CODE_RE`：掩码排除 SEC accession 编号与连字符文档编号；
  4. `_ISO_DATE_RE` / `_CN_DATE_RE` / `_EN_DATE_RE` / `_TIME_RE`：掩码排除日期与时间；
  5. `_LIST_MARKER_RE` / `_HEADING_NUM_RE` / `_BARE_YEAR_RE` / `_VERSION_RE` / `_TIER_CODE_RE`：掩码排除列表编号、标题序号、裸年份、版本号、等级代号；
  6. `_EXEMPT_TAG_RE` / `_CALC_EXPR_RE` / `_APPROX_PREFIX_RE`：草稿内逐字豁免标记与显式算式检测；
  7. `_NUMBER_TOKEN_RE`：基础阿拉伯数字与紧邻单位词抽取。

---

## 3. H3：自主议程全链路修复（`A1`–`A14` + `§3.3`）

| 编号 | 问题 | 修复文件与实现 |
|---|---|---|
| **A1** | `preflight.py`、`board.py`、`report.py` 读取不存在的预算键名 `total_cash_usd`/`reserved_cash_usd` | 统一改为读取 `get_budget_today` 真实返回的 `spent_usd` 与 `open_reserved_usd` |
| **A2** | `preflight.py` 调用 `assist_json` 传了不存在的 `system_prompt=`/`user_payload=` 参数 | 改为真实签名 `assist_json(task, instructions, payload, schema, ...)`，并将 `test_autonomy_preflight.py` 替身改为 `create_autospec(assist_json, spec_set=True)` |
| **A3** | 45 秒总超时内层吞掉 `CancelledError` 写半截状态 | 在 `preflight.py` 中显式捕获 `asyncio.CancelledError` 并立即向上传播，禁止取消期间写库 |
| **A4** | `agenda.py`/`earnings.py`/`director.py`/`board.py`/`report.py` 读旧字段名 | 全面对齐 `agenda.schema.json` 真实字段：`next_validations[].due`、`watch.events`、`wake_conditions`、`open_questions` |
| **A5** | `dalton_agenda_note` 写入 `/open_questions` 缺少必填 `owner` 字段 | 在 `director_tools.py` 中补齐 `"owner": "pm"`、`"origin": "pm"`、`"status": "open"` |
| **A6** | `probes/internal.py` 直接查 Postgres 中不存在的 `internal_documents` 表 | 改为经宿主机 `researchdata_bridge`（`127.0.0.1:8960`）HTTP JSON-RPC 查询 `sales-notes`/`alphaengine`/`guidepoint` 最新文献 |
| **A7** | `probes/director.py` 空闲日仍返回 `P2` 信号唤醒强模型 | 空闲日（无到期验证、无高优未退避问题、无超 24h 未消费信号、无临期审批、无 14 天内未确认财报）返回空信号列表，`should_wake=False`（花费 `$0`） |
| **A8** | `automation_executions.py` 连续失败退避未把 `dalton-*` 的 `status` 设回 `'active'` | 在 `_count_strike` SQL 中对 `dalton-*` 非熔断失败显式设置 `status = 'active'` 并推迟 `next_run_at` |
| **A9** | 预算耗尽时 `once` 一次性任务被结算为关闭 | 在 `preflight.py` 与 `automation_settlement.py`/`automation_executions.py` 中新增 `defer_once_1d`，预算耗尽跳过时保持 `active` 并顺延 `+1 day` |
| **A10** | 同信号二次预检时因 `is_new=False` 被漏掉 | `watch.py` `upsert_signal` 返回 `(row, is_unwoken)`，只要 `woken_at IS NULL AND resolved_at IS NULL AND expired_at IS NULL` 即纳入候选 |
| **A11** | `probes/calendar.py` 读错 `agenda` 顶层字段 | 改为读取 `agenda["watch"]["events"]` |
| **A12** | 时区混用 UTC 与上海时间 | 统一使用 `ZoneInfo("Asia/Shanghai")` 计算“今天”与到期日 |
| **A13** | 财报日历周任务星期几不一致 | 统一为周六 09:00（`0 9 * * 6`） |
| **A14** | 过期信号查询条件遗漏 | `watch.py` 所有未消费信号查询统一加上 `AND expired_at IS NULL` |
| **§3.3** | 知识层字段名模糊匹配自动改写风险 | `ops.py` 冻结确定性顶层别名表（永不改 `/official`）；`suggest_close_fields` 仅用 `difflib.get_close_matches` 产出候选建议，交由 `service.py` 调用 `assist_json("knowledge.repair_field")` 确认后才改写并记入 `warnings[]` |

---

## 4. H4 & H5：供应商探测、角色校验、预算与日志修复

1. **H4（`P1`–`P4`）**：
   - `src/dalton/services/provider_status.py`：修正 `resolve_llm_client` -> `resolve_model_client`（P1）；探测请求统一走 `dalton_complete` 记入日预算与 `dalton_llm_journal`（P2）；按 `{"byok": {"providers": [...]}}` 真实嵌套结构读取用户自带 Key（P4）。
   - `src/dalton/llm/role_policy.py`：`check_roles` 扩展校验全部 10 个角色（含 `assist`、`background`、`compaction`、`fetch`），禁止任何 `*-contributor-oauth` 变体进入角色配置，且强制 `assist` 角色必须非 `text_only`（P3）。
   - `tests/unit/dalton/test_router_wiring.py`：按 `L33` 全部使用 `create_autospec` 替身并实际执行每个路由背后的服务函数（26/26 通过）。
2. **H5（`B1`–`B3` + `R2-N1 #6`）**：
   - `src/dalton/database/llm_journal.py`：新增 `prune_expired_journal_responses(retention_days=30)`，将超 30 天记录的 `response` 置为 `NULL` 并保留调用 ID、模型、用量、费用、哈希等全部元数据，由每日预检自动触发（B1）。
   - `src/dalton/database/budget.py` & `llm_journal.py`：`fetch_journal_cash_today` 与 `fetch_journal_rows_in_window` 数据库异常时打 `logger.error` 并向上抛出 `JournalCashQueryError`，准入层捕获后返回 HTTP `503` 拒绝准入（fail-closed，B2）。
   - `src/dalton/llm/complete.py`：`_check_cash_budget_admission` 在传入 `workspace_id` 或 `thread_id` 时调用 `fetch_scoped_task_spend` 强制检查对应 task cap（B3）；真实模型调用返回后的响应序列化异常统一落盘为 `indeterminate`，防止二次重复计费（`R2-N1 #6`）。

---

## 5. H2 + H6：G1–G7 真实运行验证证据全集

| 族 | 验证项与实测结果 | 证据文件 | 运行日期 | 真实花费 (USD) |
|---|---|---|---|---|
| **G1 预算** | ① 日额度设为 `$1.00`，主模型用 `claude-opus-5-5-oauth`，派出现金子智能体 `grok-4.7` 花超 `$1.00` 后被拒（HTTP 402）；② BYOK `xai` 对话花费计入 `GET /api/v1/dalton/budget/today`；③ 独立审稿首次计费 `$0.001476`，二次同草稿命中缓存（`replayed=true, cash_usd=$0.0`），额度耗尽时拒绝调用 | `var/dalton/smoke/g1_budget_realrun_20261018.json` | 2026-10-18 | `$0.00148` |
| **G2 接口** | 真后端 `127.0.0.1:8000` 对全部 Dalton REST 接口与 6 个供应商探测接口逐一真实调用通过；`assist` 配为 `contributor` 变体时启动校验报错阻断 | `var/dalton/smoke/g2_api_and_probes_20261018.json` | 2026-10-18 | `$0.00051` |
| **G3 发布** | ① 口语中文（`两成`/`百分之三十`/`腰斩`/`过半`）全抽出并判 `unverified`；② 真实 LLM 提示词注入被代码引文数字锚定拦截；③ **META 真实完整报告 445 个数字误判 `0 条`（`<= 2`）、`uncertain = 0%`（`<= 10%`）**；④ 跨家族审稿（`claude-opus-5-5-oauth` -> `ocb-deepseek-deepseek-flash`）通过；⑤ 四家公司活库 `remediate_migrated_workspaces` 修正与 API 核验通过 | `var/dalton/reports/g3_real_llm_audit_20261018.json`、`var/dalton/reports/g3_4_live_remediation_20261018.json`、`docs/dalton/dev-guide/trial-runs/G3-real-report-audit-20261018.md` | 2026-10-18 | `$0.01291` |
| **G4 自主议程** | ① META 真实 8-K (`0001628280-26-050596`) 唤醒并注入 `wake_context`；② 真实 LLM 分诊无关资料 `skip`（落库理由）+ 相关资料 `wake`；③ GOOGL 到期验证唤醒；④ `knowledge_append_verdict` 闭环信号后看板不再列出；⑤ 总监空转日 `skip` 花费 `$0`；⑥ 额度耗尽跳过并将 `once` 任务顺延 `+1d` (`defer_once_1d`)；⑦ `dalton_agenda_note` 与 `knowledge.repair_field` 真实写入 Postgres 成功 | `var/dalton/smoke/g4_autonomy_realrun_20261018.json` | 2026-10-18 | `$0.00092` |
| **G5.1 Broker** | ① 长调用中途重启网关后同 ID 重试返回 `409 idempotency_indeterminate`，无二次计费；② 客户端中途断开后上游继续完成落盘，同 ID 重试返回 `200` 完整结果（`replayed=true`）。（注：`G5.2` Antigravity 因本机尚未人工浏览器登录 OAuth，在 `PROGRESS.md` 中诚实保持 `🟦 代码完成`） | `var/dalton/smoke/g5_broker_disconnect_and_restart_20261018.json` | 2026-10-18 | `$0.00051` |
| **G6 财务镜头** | 按 `SKILL.md` 命令对 `BABA`（`CNY`）、`0700.HK`（`CNY`）、`META`（`USD`）真实跑通全部 4 个镜头脚本，并附与基金原脚本对比表 | `var/dalton/lenses/{baba,0700hk,meta}_{anchors,netcash,return_score,earnings_quality}_20261018.json`、`docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md` | 2026-10-18 | `$0.00` |
| **G7 桥接** | ① `check_bridge.sh` 6 项全绿；② 一次 **242.05 秒（>240s）** 长文读取成功返回 `200 OK` 且首键为 `_sources`；③ 子进程崩溃向 `stderr` 输出敏感 canary 时桥日志仅记录 `SubprocessExitError` 与退出码，0 处 `stderr` 泄露 | `var/dalton/smoke/g7_bridge_realrun_20261018.json` | 2026-10-18 | `$0.00` |

---

## 6. 测试与挂钩检查汇总

- **上游挂钩检查（`bash scripts/dalton/check_hooks.sh --strict --audit`）**：`OK: DALTON-HOOK check passed.`（`NEW_IN_UPSTREAM_DIR: 0`，`UNHOOKED_EDIT: 0`）。
- **Python 单元测试（`uv run pytest tests/unit/dalton/ tests/unit/llms/ tests/unit/middleware/skills/ tests/unit/mcp_servers/ -q`）**：**`1,685 passed, 0 failed`**。
- **Broker v2 Node 单元测试**：**`13 pass, 0 fail`**。
- **Web 前端类型检查与单测**：`pnpm --dir web typecheck` **0 errors**；`vitest` **676/676 个测试文件（7,224/7,224 项测试）全部通过**。
