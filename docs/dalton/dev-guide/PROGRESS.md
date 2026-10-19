# Dalton on LangAlpha 开发进展记录 (PROGRESS.md)

最后更新：2026-10-19

本文件按 `docs/dalton/dev-guide/README.md`、`P0I-round3-fixes.md`、`P0H-round2-fixes.md`、`P0G-review-fixes.md`、`P0N-native-models.md`、`P0F-acceptance-fixes.md` 的工作包与步骤实时记录开发进展、分支与提交 SHA、【待验证】项的实测结论、以及已落地的上游 `DALTON-HOOK` 挂钩。

状态三级标记（按 `P0F.5` 与 `L37`、`L38` 规范）：
- `🟦 代码完成`：代码、数据库迁移、前端组件与单元测试全部通过。
- `🟨 真实运行验证`：已用入库脚本跑真实服务/接口/网络对数/冒烟验证并留下带固定头部与调用日志对账的可复查证据摘要（待 PM/Lead 最终验收或容器全栈回归后升为 `🟩`）。
- `🟩 lead 验收通过`：仅由 Lead / PM 验收签字后标记。

---

## 1. 工作包与步骤进展总表

### WP-A: OpenClaw Broker v2 (`P0.2` – `P0.3`, `G5.1`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.2** broker v2 spike | `feat/P0.2-3-broker-v2` | 🟨 真实运行验证 | `0360262a` (merge `b1c40b43`)：验证 `openclaw/plugin-sdk/llm` K1–K6 全部通过（见 `var/dalton/smoke/broker_spike_20261018_093702.json`） |
| **P0.3 / G5.1** broker v2 完整实现与落盘防重 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | `7464c4a6` (PR #7)：`integrations/openclaw/dalton-langalpha-broker/`（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs` `13/13 pass`）：① 承载 broker 的宿主机 OpenClaw 网关进程（`127.0.0.1:18881`）在长调用中途重启后同 ID 重试返回 `409 idempotency_indeterminate`（零二次计费），客户端中途断开后同 ID 重试返回 `200` 完整落盘结果（见 `var/dalton/smoke/g5_broker_disconnect_and_restart_20261018.json`）；② 2026-10-19 使用入库脚本 `scripts/dalton/realrun/g5_broker_and_antigravity.py` 对 `http://127.0.0.1:18881` 完成非流式、SSE 流式及 2 轮工具调用闭环验证（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.1.md` 与 `var/dalton/trial-runs/realrun-20261019/g5_broker_and_antigravity.json`） |

### WP-B: 平台接入、原生模型与本地预算 (`P0.1`, `P0.4` – `P0.9`, `P0N`, `G1`, `G2`, `G5.2`, `I3`, `I4`, `I7`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | 🟨 真实运行验证 | `7641d792` (merge `c81151b3`)：`docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml`（见 `var/dalton/hook_audit_after.txt`） |
| **P0H §2.2 / P0I §7.1** 整套环境启动与三项连通性检查 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | `7464c4a6` (PR #7)：2026-10-19 启动整套服务（Postgres `127.0.0.1:5432`、Redis `127.0.0.1:6379`、后端 `http://127.0.0.1:8000`、宿主机桥 `http://127.0.0.1:8960`、OpenClaw 网关 `127.0.0.1:18789` / Broker `http://127.0.0.1:18881`），启动日志落盘于 `var/dalton/smoke/backend_startup_20261019.log`。三项连通性检查结果：① 宿主机桥 `127.0.0.1:8960` 全部 6 个 MCP 工具真实调用返回 `200 OK`、0 条 `502`（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G7.md`）；② OpenClaw Broker `127.0.0.1:18881` 连通且流式/工具调用正常（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.1.md`）；③ 启动严格校验 `enforce_role_policy_on_startup` 与 30 天日志清理 `prune_expired_journal_responses` 执行成功（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G2.md`） |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-8-llm-overlay-and-budget` | 🟨 真实运行验证 | `7061b70e` (merge `0a7f5b99`)：`openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py`（同步模型写入入库配置 `src/dalton/llm/openclaw_overlay.json`，0 凭据落盘）+ `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `dalton/main` | 🟨 真实运行验证 | 按每次运行分别记录：① `var/dalton/smoke/openclaw_smoke_broker_20261018T142137Z.json`（覆盖 `B0`–`B8`、`B10`–`B14` 共 14 项：`13 pass, 1 warn (B8), 0 fail`）；② 修复 `B8` `dalton_meta.reasoning_effort` 后运行 `--checks B1,B2,B8` 子集 `var/dalton/smoke/openclaw_smoke_broker_20261018T172300Z.json`（共 13 项：`13 pass, 0 warn, 0 fail`） |
| **P0.6 / H4 / I3** 角色模型配置与启动失败阻断 | `fix/P0I.I3-providers-startup-budget` | 🟨 真实运行验证 | `1a04cb71` (PR #3) & `7464c4a6` (PR #7)：`src/dalton/llm/role_policy.py` 的 `check_roles` 校验全部 10 个角色（含 `assist`、`background`、`compaction`、`fetch`），禁止 `*-contributor-oauth` 进入任何角色，且 `assist` 角色必须是不需要工具的模型；`enforce_role_policy_on_startup` 在 `src/server/app/setup.py` 启动时遇到错误配置直接抛出 `RolePolicyError` 阻断服务启动（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G2.md` 与 `var/dalton/trial-runs/realrun-20261019/g2_providers.json`） |
| **P0.7 / P0N.8 / I3 / I7** 设置页同步按钮与模型供应商状态卡 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I3` (`1a04cb71` PR #3) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：修正 `src/dalton/services/provider_status.py` 读取 `api_keys` 扁平字典与 `byok` 嵌套结构；2026-10-19 通过 `scripts/dalton/realrun/g2_providers.py` 对 `http://127.0.0.1:8000` 实测 5 个供应商探测，其中现金供应商 `deepseek` 探测成功并计入 `dalton_llm_journal`（`cash_usd=$0.000015`），订阅供应商 `claude-cli-gateway` 与 `antigravity` 探测成功（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G2.md` 与 `var/dalton/trial-runs/realrun-20261019/g2_providers.json`） |
| **P0.8 / G1 / I3 / I7** 本地日美元预算 `LocalBudgetPort` 与 `d007` 账本 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I3` (`1a04cb71` PR #3) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：① `src/server/app/setup.py` 启动时调用 `prune_expired_journal_responses(retention_days=30)` 清理超 30 天响应正文；② `src/dalton/llm/complete.py` 在 `complete_journal_done` 落盘周围补齐 `try/except` 兜底标记 `indeterminate`；③ 2026-10-19 运行 `scripts/dalton/realrun/g1_budget.py --cap-usd 0.02` 通过真实 `POST /api/v1/threads/messages` 驱动 `claude-sonnet-5-5-low-oauth` 主模型派出现金子智能体 `deepseek-flash` 花费 `$0.022204`、设置页 `PUT /api/v1/users/me/api-keys` 触发 BYOK 探测花费 `$0.000045` 计入 `GET /api/v1/dalton/budget/today`，触顶后下一轮对话返回 `HTTP 429`（`detail.type = "dalton_daily_budget"`），附前端拒绝横幅截图 `var/dalton/trial-runs/realrun-20261019/g1_budget_exhausted_banner.png` 与 `publish.review` 二次缓存命中验证（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G1.md` 与 `var/dalton/trial-runs/realrun-20261019/g1_budget.json`） |
| **P0N.0 – P0N.5 / G5.2 / I3 / I7** 原生模型迁入 `src/dalton/` 与 Antigravity/Muse 修复 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | `1a04cb71` (PR #3) & `7464c4a6` (PR #7)：迁入 `src/dalton/llm/native_overlay.json` 与 `src/dalton/llm/providers/{antigravity,muse}`；`src/dalton/llm/providers/antigravity/oauth.py` 将 60s 登录启动冷却持久化至 `dalton_provider_events` 表（删除进程内 `_LAST_INITIATED_AT`）；2026-10-19 运行 `scripts/dalton/realrun/g5_broker_and_antigravity.py` 完成 Antigravity OAuth 状态核验、`gemini-3.8-flash-low-oauth` / `claude-sonnet-5-5-low-oauth` / `claude-opus-4-6-thinking-max-oauth` 思维链与工具调用闭环验证（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G5.2.md` 与 `var/dalton/trial-runs/realrun-20261019/g5_broker_and_antigravity.json`） |
| **G2 / I3 / I7** 路由接线修复与守门测试 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I3` (`1a04cb71` PR #3) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：`tests/unit/dalton/test_router_wiring.py` 全部使用 `create_autospec` 替身（包括 `test_every_dalton_route_resolves_without_500` 对 `aaudit_draft` 使用 `create_autospec`，`uv run pytest tests/unit/dalton/test_router_wiring.py -q --disable-socket --allow-unix-socket` `28 passed`）；`scripts/dalton/realrun/g2_providers.py` 从应用路由表动态枚举全部 33 条 `/api/v1/dalton` 路由并逐条真实调用通过（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G2.md` 与 `var/dalton/trial-runs/realrun-20261019/g2_providers.json`） |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | 🟦 代码完成 | 如实陈述当前状态：I1–I8 代码与单测已合入，G1、G2、G3、G4、G5.1、G5.2、G6、G7 共 8 族已按 `L37` 规范生成入库脚本与真实运行证据并达到 `🟨`（其中 G4 场景②的内部文献子项因当天内部库无新入库文档，按 `P0I §7 G4` 规范如实标为 `🟦`，不合成假文献）；待第四轮审查人用入库脚本独立重跑 G1 与 G3（`P0I §7.2`）及人工浏览器界面走查后进入 P0.9/P0.10 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`, `G7`, `P0F.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1 / G7 / I7** researchdata 宿主机桥 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：`scripts/dalton/researchdata_bridge.py` 新增入库测试延迟钩子 `DALTON_BRIDGE_TEST_DELAY_S`（默认关闭）；2026-10-19 运行 `scripts/dalton/realrun/g7_bridge.py` 对 `http://127.0.0.1:8960` 完成：① 全部 6 个 MCP 工具端到端真实调用返回 `200 OK`；② 真实长文档读取验证 `_sources` 与 `_declared_sources` 通路（首键如实记录为 `_sources` 或文档业务键）；③ 通过 `DALTON_BRIDGE_TEST_DELAY_S` 完成 `>240s`（实测 `242.02s`）长耗时读取并在 `synthetic` 中显式声明 `bridge_delay_hook`；④ 子进程崩溃 `stderr` 零泄露验证（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G7.md` 与 `var/dalton/trial-runs/realrun-20261019/g7_bridge.json`） |
| **P0-D.2** 注册内置 HTTP MCP + 超时补丁 | `feat/P0-D.1-6-data-and-sec` | 🟦 代码完成 | `plugins/dalton_data/` + `DALTON-HOOK(mcp-call-timeout)`（`agent_config.dalton.yaml` `call_timeout_s: 625`） |
| **P0-D.3 / D.3b** 来源面板逐篇展开 | `feat/P0-D.1-6-data-and-sec` | 🟦 代码完成 | `declared_sources.py`、`ResearchDocSource.tsx`、`d004_provenance_locator.py` + `test_declared_sources.py`（13/13 单测通过） |
| **P0-D.4 / P0F.4** 基金 wiki 只读覆盖评估 | `dalton/main` | 🟨 真实运行验证 | `scripts/dalton/wiki_coverage_report.py` 实测 `~/.openclaw/workspace/wiki` 共 **174 个公司目录、27 个行业/主题目录、`vectors.db` 1,086 篇文档**；覆盖池 105 家中 **51 家有 wiki 目录**（Core `24/27`，仅缺 `MCO`/`NDAQ`/`NTES`；Watch `27/78`），检索命中 `51/51`（见 `var/dalton/reports/wiki_coverage_20261018.md`） |
| **P0-D.5** `dalton_sec` 沙箱 stdio MCP | `feat/P0-D.1-6-data-and-sec` | 🟨 真实运行验证 | `dalton_sec_mcp_server.py`（R1–R10 规则）+ META/MSFT/AMZN 真实 EDGAR 对数 + 契约与期间单测（12/12 通过） |
| **P0-D.6** 价格与一致预期验证 | `feat/P0-D.1-6-data-and-sec` | 🟨 真实运行验证 | 实测 `estimates` (Yahoo Finance) 与 `yfinance` 对 META/MSFT/AMZN 的字段与非日历财年映射 |

### WP-D: 方法论与入职 (`P1.1` – `P1.5`, `G3.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P1.1** `dalton-conventions` 方法论 skill | `feat/P1.1-4-onboarding-and-migration` | 🟦 代码完成 | `plugins/dalton/skills/dalton-conventions/`（SKILL.md + 8 份 references 均 ≤200 行）+ `dalton_registry.py` |
| **P1.2** 覆盖名单 `coverage.yaml` | `feat/P1.1-4-onboarding-and-migration` | 🟨 真实运行验证 | `build_coverage.py --check` 通过（Core 27 / Watch 78，共 105 家，5 项已知冲突显式化解，见 `config/dalton/coverage.yaml`） |
| **P1.3** 公司工作区初始化 | `dalton/main` | 🟨 真实运行验证 | `dalton-init-company` skill + `init_company.py`（自动从 `coverage.yaml` 写入 `cik` 到 `data/company.json`）+ `init_company_workspace.py`（三态幂等验证通过） |
| **P1.4 / G3.4 / I1** 旧认知迁移、清除编造数值与 `legacy_thesis` 隔离 | `fix/P0I.I1-publish-audit` | 🟨 真实运行验证 | `58e85d4e` (PR #6)：`scripts/dalton/migrate_legacy_knowledge.py` 清除全部硬编码编造值（见 `var/dalton/migration/migration_report.json`），并按 `L34` 将从 `sources/legacy/thesis.md` 迁入的条目统一标记为 `source.type = "legacy_thesis"`（原因 `"迁移的旧认知不作为核实依据"`），在发布核验 C/D 环节中不作为核实依据、仅作为候选展示（见 `var/dalton/reports/g3_4_live_remediation_20261018.json` 与 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G3.md`） |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`, `G3`, `I1`, `I7`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `fix/P0I.I1-publish-audit` | 🟦 代码完成 | `58e85d4e` (PR #6)：`paths.py` + 10 份 JSON Schema (draft 2020-12，`additionalProperties: false`，`thesis.schema.json` 新增 `legacy_thesis` 枚举值) |
| **PK.3 – PK.7 / H3 §3.3** 版本表、写入工具、字段建议+LLM确认、读序 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | `7464c4a6` (PR #7)：`ops.py` 冻结确定性顶层路径别名表（永不改 `/official`），`difflib.get_close_matches` 产出候选建议交由 `assist_json("knowledge.repair_field")` 确认后改写并记 warning；`src/dalton/database/verdicts.py` 的 `next_verdict_id` 修复为按日全局唯一序号分配，`src/dalton/knowledge/service.py` 的 `append_verdict` 在 Schema 校验前剥离瞬态 `trigger_signal`/`trigger_signals` 字段（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G4.md`） |
| **PK.8 – PK.13 / G3 / I1 / I7** 发布核验堵住错放、全量分批标注、引文锚定与章节级缓存 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I1` (`58e85d4e` PR #6) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`（`git diff --shortstat 925fd0f9 7464c4a6 -- src/dalton/publish/` 为 `6 files changed, 1583 insertions(+), 734 deletions(-)`）：① 按 `L34` 移除无脚注数字在 C 环节的自动放行，收紧 `apply_derived_figure_matching` 派生四则运算；② 按 `L35` 在 `label_figures_with_status` 中按章节分批（每批 `<= 40` 个数字）覆盖全部数字与全文，LLM 标注优先于启发式，新增 `_can_be_incidental` 确定性守卫；③ 按 `L36` 在 `verify_llm_judge_verdict` 中补齐量纲一致性与引文期间一致性检查；④ 按章节分批调用 `assist_json` 命中 `dalton_llm_journal` 缓存；⑤ 2026-10-19 运行 `scripts/dalton/realrun/g3_publish.py` 通过 `POST /api/v1/dalton/publish/{workspace_id}/check` 实测 GOOGL（`514` 个数字，金标准 `159` 个数字中错放 `0`、真阳召回 `100%`、错判有矛盾 `0`，金标准外 25 个抽样错放 `0`）、AMZN（`74` 个数字）、GOOGL × META 交叉对照（`verified = 0`）、S1–S5 合成注入及单节改写缓存复用（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G3.md` 与 `var/dalton/trial-runs/realrun-20261019/g3_publish.json`） |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`, `G6`, `I6`, `I7`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3 / G6 / I6 / I7** 脚手架、`dalton-evidence`、财务镜头统一锚点与公司背景 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I6` (`b2463c54` PR #5) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：`netcash.py`、`return_score.py`、`earnings_quality.py` 新增 `--anchors` 参数从 `anchors.py` 输出读取统一市值、汇率与期间（同时传入 `--anchors` 与手填 `--market-cap-usd-mn`/`--fx-rate` 时报错退出）；统一锚点后 `BABA` 严格净现金占市值比由 `19.80%` 更新为 `19.13%`；2026-10-19 运行 `scripts/dalton/realrun/g6_lenses.py` 完成真实锚点统一验证（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G6.md`、`var/dalton/trial-runs/realrun-20261019/g6_lenses.json` 与 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md`） |
| **PS.4 – PS.18 / G6.1** 分析师 Skill、Workflow 与引用比对脚本 | `dalton/main` | 🟦 代码完成 | 清理残留 OpenClaw 旧工具名、`RunWorkflow` 参数名统一为 `params`、重写 `dalton-agenda` 自动任务模板与 `dalton-research-director` 去掉次数门、补齐 `dalton_registry.py` `requires`；`scripts/dalton/check_skill_refs.py` 扫描 22 个 skill：`ghost_hits=0, broken_refs=0` |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`, `G4`, `I4`, `I5`, `I7`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20 / G4 / I5 / I7** 自主议程低危项修复与自动任务触发真实验证 | `fix/P0I.I7-realrun-evidence` | 🟨 真实运行验证 | 第三轮复核后降为 `🟦`，在 P0I `I5` (`dc1fd7a4` PR #4) 与 `I7` (`7464c4a6` PR #7) 重做并恢复 `🟨`：① 完成 `I5` 7 项低危修复（`director_tools.py` 透传 `user_id`、`probes/internal.py` 节流 warning 与 3 路由 `asyncio.gather` 10s 并行、`preflight.py` 认领异常返回 `claim_failed` 跳过与预算异常 `logger.error` 注释说明、`probes/sec.py` 记录 `sec_probe_degraded`、`schedule.py` 按交易日计算财报节点并与模板对齐 `cooldown_seconds: 86400`）；② 2026-10-19 运行 `scripts/dalton/realrun/g4_autonomy.py` 全部经 `POST /api/v1/automations/{id}/trigger` 真实触发自动任务执行器完成六项场景 + `dalton_agenda_note` 写入验证（其中场景②的内部文献子项因当天内部资料库无新入库文档 `meta_new_docs=0, googl_new_docs=0`，按 `P0I §7 G4` 规范如实标为 `🟦`，不合成假文献；见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G4.md` 与 `var/dalton/trial-runs/realrun-20261019/g4_autonomy.json`） |
| **PK-UI.1 – PK-UI.5 / I4 / I1** 前端回绿、审批卡升级与中英文国际化 | `fix/P0I.I1-publish-audit` | 🟦 代码完成 | `46f42833` (PR #1) & `58e85d4e` (PR #6)：修复 `PublishApprovalCard.test.tsx` 类型错误，在 `en-US.json` 与 `zh-CN.json` 中补齐 `dalton.publish.*` / `dalton.budget.*` 双语键并新增真实 i18n `en-US` 渲染单测；`PublishApprovalCard.tsx` 展示标注覆盖率（`labeled/total`）、可折叠 `incidental` 列表、按来源拆分的已核实计数（`footnote`/`llm`/`derived`）、LLM 核实项草稿与引文并排对照、无来源项候选列表；`pnpm --dir web typecheck` `0` 错误，`pnpm --dir web test -- --run` `677` 个测试文件（`7,228` 项测试）全部通过 |

---

## 2. G3.4 / I1 迁移脚本编造数值清理与旧认知隔离对照表

| 位置 (`scripts/dalton/migrate_legacy_knowledge.py`) | 原先硬编码或旧逻辑 | 清理后做法 (`build_knowledge_documents` / `remediate_doc_payload`) |
|---|---|---|
| `drivers` (`historical_valuation_driver.eps_share_pct` / `multiple_share_pct`) | `65` / `35` | 设为 `None` (`null`)，备注 `"迁移时未计算，待首次研究补充"` |
| `management` (`guidance_history[].issued_at`) | `f"{current_year}-07-29"` | 仅从真实源抽取日期；缺失时设为 `None` (`null`) |
| `company` (`autonomy.max_usd_per_day`) | `15` | 设为 `None` (`null`)，统一交由平台预算设置控制 |
| `agenda` (`watch.events[].date` / `next_validations[].due`) | `f"{current_year}-11-15"` | 设为 `None` (`null`)，待首次研究或财报日历探针填入 |
| `thesis` (`evidence[].source.type`) | 混用 `sec_filing` / `transcript` 等类型 | 凡 `ref.path` 位于 `sources/legacy/` 的迁入条目统一改为 `legacy_thesis`，原因记 `"迁移的旧认知不作为核实依据"`（`L34`） |

---

## 3. 明确删掉或不做的内容（`P0G §9`）

1. **删除价格探针与一致预期探针**：已从仓库删除价格探针与一致预期探针模块；股价异动改用 LangAlpha 自带 `price` 触发器，一致预期对比放入每晚 `thesis-delta` 任务。
2. **删除发布时生成 PDF 的承诺**：已在 `src/dalton/publish/files.py` 中移除断裂的 PDF 导出逻辑，正式发布归档 `.md` 与 `.xlsx`。
3. **不做 Broker 备用模型自动切换**：模型切换统一在 LangAlpha 侧处理，不在 broker 中维护跨模型切换链。
4. **不做 Broker 排队等待**：超出并发上限立即返回 `429`，由调用方处理。

---

## 4. 【待验证】项实测记录

1. **宿主机 `uv` 安装状态（P0.1）**：实测宿主机初始无 `uv`，已通过 `brew install uv` 安装 `uv 0.13.1`（`/opt/homebrew/bin/uv`）。
2. **本机内存与网关状态（P0.1 / P0H §2.2）**：OpenClaw 网关（`127.0.0.1:18789` / Broker `127.0.0.1:18881`）、Researchdata 宿主机桥（`127.0.0.1:8960`）和 AlphaEngine MCP（`127.0.0.1:8950/health` 返回 `status: ready`）均正常运行，`$CORE_DB`（6.2 GB）在外置卷 `/Volumes/EveSSD` 上已挂载可读（见 `docs/dalton/dev-guide/trial-runs/realrun-20261019/G7.md`）。
3. **OpenClaw `plugin-sdk/llm` 运行时契约（P0 附录 B #1–#4, #12）**：
   - `prepareSimpleCompletionModelForAgent` 返回的 `prepared.model` 挂载了 `Symbol("openclaw.modelLlmRuntime")`，其内部 `completionTransport` 绑有 `streamSimple` 闭包；Broker v2 通过 `resolveStreamExecutionModel(model)` 提取该绑定对象后，所有传输层（`openai-responses`、`openai-completions`、`google-generative-ai`、`anthropic-messages`）均正常工作。
   - `toolChoice` 在不同传输层存在命名差异；Broker v2 在 `invoke.mjs` 中通过 `buildToolChoiceStreamOptions` 同时传入 `toolChoice` 与 `onPayload` 钩子，实测 `xai/grok-4.7`、`google/gemini-3.8-flash`、`openai/gpt-6-astra` 的 K1–K6 全部通过。
   - 经 Broker v2 调用时不会触发 OpenClaw workspace prompt 注入：实测 `B6` 的未缓存 `prompt_tokens` 为 91（远低于 100 阈值），且 `B7` 实测无任何内部工具泄漏。
4. **`subagents.definitions.<name>.model` 部分字段覆盖（P0 附录 B #8）**：已核实 `src/ptc_agent/config/agent.py` L200–215 与 `src/ptc_agent/agent/subagents/merge.py` L103–108：当 `<name>` 属于 `BUILTIN_SUBAGENTS`（如 `research`、`data-prep`、`general-purpose`）时，`SubagentConfig` 不强制要求 `description`，`SubagentRegistry` 按字段合并覆盖内置定义的 `model`。
5. **`security.max_execution_time` 在 `agent_config.yaml` 中的加载路径（P0-D.2）**：已核实 `src/ptc_agent/config/loaders.py` L206、L284：`load_from_dict` 当前直接调用 `create_default_security_config()`（默认 `max_execution_time=300` 秒），未读取 YAML 顶层 `security:` 段。在默认 `call_timeout_s: 300`（桥端上限 `300 - 25 = 275` 秒）下，300 秒已够用。
6. **基金 Wiki 只读覆盖与 G1 反向提及实测（P0-D.4 / P0F.4 修正）**：
   - 实测 `~/.openclaw/workspace/wiki` 共含 **174 个公司目录、27 个行业/主题目录，`vectors.db` 共索引 1,086 篇文档**。
   - 对照 `config/dalton/coverage.yaml` 全部 105 家公司（27 Core + 78 Watch）：共 **51/105 家**已有对应公司目录（其中 **Core 24/27**，缺失的 3 家 Core 为 `MCO`、`NDAQ`、`NTES`；**Watch 27/78**），已有目录的 51 家检索命中率 **51/51 (100%)**。完整明细见 `var/dalton/reports/wiki_coverage_20261018.md`。
   - G1 反向提及查询：通过 `source="company-wiki", action="search", query="Google TPU Meta MTIA Marvell Broadcom custom ASIC"`（或 `source="all"` 跨公司 + 专题全文检索）可直接命中 `companies/MRVL/industry.md`、`companies/AVGO/relationships.md`、`companies/META/overview.md` 等跨公司供应链段落。
7. **SEC EDGAR 期间语义与真实对数（P0-D.5）**：
   - **META 2025Q2 vs H1 2025**：10-Q 同时披露 ~91 天单季（Revenue `$47,516M`）与 ~181 天半年累计（`$89,830M`），`dalton_sec` 按天数桶精确区分 `Q2` 与 `YTD_6M`，无混用。
   - **MSFT FY2026Q1**：period end `2025-09-30` 在 `fy_end_month=6` 下正确归一为 `FY2026Q1`（Revenue `$77,673M`），`calendar_equivalent="CY2025Q3"` 显式标注。
   - **AMZN 2025Q4 派生单季**：10-K 仅披露 FY2025 全年（`$716,924M`），`dalton_sec` 自动由 `FY2025 - (Q1+Q2+Q3)` 派生单季 Q4（`$213,386M`），标注 `derived=True`、`derivation_formula="FY2025 - (2025Q1 + 2025Q2 + 2025Q3)"` 及全部 4 份源 10-K/10-Q 的 accession number。
8. **价格与一致预期非日历财年映射（P0-D.6）**：
   - `researchdata(source="estimates")` 底层调用 Yahoo Finance（免费源），返回 `eps_estimate`、`revenue_estimate`、`eps_trend`、`eps_revisions`、`earnings_history`、`growth_estimates`。
   - 对 MSFT（6 月财年），Yahoo Finance 将 `0q`/`+1q` 的 `endDate` 标为日历季末（`2026-09-30` / `2026-12-31`），需结合 `dalton_sec` 的 `fy_end_month=6` 映射为 `FY2027Q1` / `FY2027Q2`。

---

## 5. 测试与挂钩检查汇总（附实测命令，`L38`）

- **上游挂钩检查（`./scripts/dalton/check_hooks.sh --strict --audit`）**：`OK: DALTON-HOOK check passed.`（`NEW_IN_UPSTREAM_DIR: 0`，`UNHOOKED_EDIT: 0`，见 `var/dalton/hook_audit_after.txt`）。
- **Skill 引用检查（`./scripts/dalton/check_skill_refs.py`）**：`Scanned 22 skills: ghost_hits=0, broken_refs=0, header_notes=0`。
- **路由接线守门单测（`uv run pytest tests/unit/dalton/test_router_wiring.py -q --disable-socket --allow-unix-socket`）**：**`28 passed`**（全部使用 `create_autospec` 替身并实际调用路由底层服务函数）。
- **Dalton 专项 Python 单测（`uv run pytest tests/unit/dalton/ -q --disable-socket --allow-unix-socket`）**：**`160 passed`**。
- **Python 组合回归单测（`uv run pytest tests/unit/dalton/ tests/unit/llms/ tests/unit/middleware/skills/ tests/unit/mcp_servers/ -q --disable-socket --allow-unix-socket`）**：**`1,698 passed`**。
- **Broker v2 Node 单测（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs`）**：**`13 pass, 0 fail`**。
- **Web 前端类型检查与全量单测（`pnpm --dir web typecheck && pnpm --dir web test -- --run`）**：`tsc -b` **`0 errors`**；`vitest` **`677 passed (677)` 测试文件、`7,228 passed (7,228)` 项测试全部通过**。
- **开发报告与进度表标识符/证据路径检查（`uv run python scripts/dalton/check_report_claims.py --strict docs/dalton/dev-guide/PROGRESS.md docs/dalton/dev-guide/P0I-round3-dev-report.md`）**：`missing_identifiers=0, missing_repo_paths=0, missing_var_paths=0, all_clean=True`。
