# Dalton on LangAlpha 开发进展记录 (PROGRESS.md)

最后更新：2026-10-21

本文件按 `docs/dalton/dev-guide/README.md`、`P0J-round4-fixes.md`、`P0I-round3-fixes.md`、`P0H-round2-fixes.md`、`P0G-review-fixes.md`、`P0N-native-models.md`、`P0F-acceptance-fixes.md` 的工作包与步骤实时记录开发进展、分支与提交 SHA、【待验证】项的实测结论、以及已落地的上游 `DALTON-HOOK` 挂钩。

状态三级标记（按 `P0F.5` 与 `L37`、`L38`、`L39`–`L43` 规范）：
- `🟦 代码完成`：代码、数据库迁移、前端组件与单元测试全部通过。
- `🟨 真实运行验证`：已用入库脚本跑真实服务/接口/网络对数/冒烟验证并留下带固定头部与调用日志对账的可复查证据摘要（待 PM/Lead 最终验收或容器全栈回归后升为 `🟩`）。
- `🟩 lead 验收通过`：仅由 Lead / PM 验收签字后标记。

---

## 1. 工作包与步骤进展总表

### WP-A: OpenClaw Broker v2 (`P0.2` – `P0.3`, `G5.1`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.2** broker v2 spike | `feat/P0.2-3-broker-v2` | 🟨 真实运行验证 | `0360262a` (merge `b1c40b43`)：验证 `openclaw/plugin-sdk/llm` K1–K6 全部通过（见 `var/dalton/smoke/broker_spike_20261018_093702.json`） |
| **P0.3 / G5.1** broker v2 完整实现与落盘防重 | `dalton/main` | 🟨 真实运行验证 | 第四轮因 V1 整体退回后，在已合并干净提交 `ad1b9544` 上重做（PR #44 `cd880f52`）：`integrations/openclaw/dalton-langalpha-broker/`（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs` `13/13 pass`）：2026-10-21 使用 `scripts/dalton/realrun/g5_broker_and_antigravity.py --mode g5.1 --date 2026-10-21` 针对宿主机 OpenClaw 桥接器验证：① 流中途客户端断开后同 `X-Dalton-Invocation-Id` 重连返回 `HTTP 200`（`X-Dalton-Replayed=1`，`openclaw_meta.replayed=True`）；② 重启承载桥接器的 OpenClaw 网关进程（`openclaw gateway restart`，`/dalton-broker/v1/health` 的 `started_at` 从 `2026-10-21T08:41:53.805Z` 变为 `2026-10-21T11:20:43.339Z`）后同 ID 重连返回 `HTTP 409`（`disk_status=indeterminate`，`idempotency_indeterminate`），`valid=true`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G5.1.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g5_1_broker.json`） |

### WP-B: 平台接入、原生模型与本地预算 (`P0.1`, `P0.4` – `P0.9`, `P0N`, `G1`, `G2`, `G5.2`, `I3`, `I4`, `I7`, `J1`, `J3`, `J5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | 🟨 真实运行验证 | `7641d792` (merge `c81151b3`)：`docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml`（见 `var/dalton/hook_audit_after.txt`） |
| **P0H §2.2 / P0I §7.1 / P0J §1.4** 整套环境启动与连通性预检 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 `ad1b9544`（Docker 部署）上重做（PR #25 `cd27b9b9`、PR #44 `cd880f52`）：通过 `scripts/dalton/realrun/check_env.py` 与各族实跑验证：① 宿主机桥 `http://127.0.0.1:8960` 真实读取 `company-wiki:read` 与 `sec:financials` 返回 `HTTP 200`、`_declared_sources=3`、`_sources=3`、`zero_stderr_leakage=True`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G7.md`）；② OpenClaw Broker 断流重放与网关重启 409 验证通过（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G5.1.md`）；③ 启动严格校验 `enforce_role_policy_on_startup` 在 `src/server/app/setup.py:663-669` 仅对 `DaltonStartupConfigError`（含 `RolePolicyError` 和迁移缺失）阻断启动（真实子进程验证 `failed_fast=True (exit_code=3)`，见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G2.md`） |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-8-llm-overlay-and-budget` | 🟨 真实运行验证 | `7061b70e` (merge `0a7f5b99`)：`openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py`（同步模型写入入库配置 `src/dalton/llm/openclaw_overlay.json`，0 凭据落盘）+ `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `dalton/main` | 🟨 真实运行验证 | 按每次运行分别记录：① `var/dalton/smoke/openclaw_smoke_broker_20261018T142137Z.json`（覆盖 `B0`–`B8`、`B10`–`B14` 共 14 项：`13 pass, 1 warn (B8), 0 fail`）；② 修复 `B8` `dalton_meta.reasoning_effort` 后运行 `--checks B1,B2,B8` 子集 `var/dalton/smoke/openclaw_smoke_broker_20261018T172300Z.json`（共 13 项：`13 pass, 0 warn, 0 fail`） |
| **P0.6 / H4 / I3 / J3** 角色模型配置与启动失败阻断 | `fix/P0J.J3-smuggled-cleanup` | 🟨 真实运行验证 | 第四轮退回后在 J3（`8bbd1557` PR #17）与 J5（`ad1b9544` PR #44 `cd880f52`）完成：`src/dalton/llm/role_policy.py` 的 `check_roles` 校验全部角色（禁止 `*-contributor-oauth` 进入任何角色，`assist` 必须是不需要工具的模型，`assist_fallback` 备选模型不得与审稿人同家族）；`src/server/app/setup.py:663-669` 捕获 `DaltonStartupConfigError`（含 `RolePolicyError` 与迁移缺失）阻断服务启动，其他异常维持 warning；在 `ad1b9544` 上通过真实 `uvicorn` 子进程（`PTC_CONFIG_FILE` 设 `assist=muse-spark-1.3-contributor`）验证 `failed_fast=True (exit_code=3)`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G2.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g2_providers.json`） |
| **P0.7 / P0N.8 / I3 / J5** 设置页同步按钮与模型供应商状态卡 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J5（`ad1b9544` PR #44 `cd880f52`）重做并验证：`src/dalton/services/provider_status.py:223-228` 在读取用户 BYOK Key 异常时记录 `logger.debug`；`src/dalton/llm/providers/antigravity/oauth.py` 在发起登录 `start_login`（`src/dalton/llm/providers/antigravity/oauth.py:239-243`）时写入 `oauth_login_start` 事件，回退路径 `check_login_complete`（`src/dalton/llm/providers/antigravity/oauth.py:451-468`，经辅助函数 `_get_latest_oauth_login_start`（第 73-106 行））比较令牌的更新时间 `updated_at` 和最近一次发起登录的时间 `initiated_at`。2026-10-21 在 `ad1b9544` 上运行 `scripts/dalton/realrun/g2_providers.py` 实测供应商探测：已配置现金供应商 `deepseek` 探测成功（`ok=True`）并计入 `dalton_llm_journal`（`calls=3, fresh=3, cash_usd=$0.000051`），订阅供应商 `antigravity-oauth` 探测成功（`ok=True`），未配置供应商 `zhipu` 返回 `ok=False, error_kind=model_not_found`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G2.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g2_providers.json`） |
| **P0.8 / G1 / I3 / J3 / J5** 本地日美元预算 `LocalBudgetPort` 与 `d007` 账本 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J3（`8bbd1557` PR #17）、J5d（`c4d10b94` PR #35）与 J5（`ad1b9544` PR #44 `cd880f52`）完成：① 30 天日志响应清理 `prune_expired_journal_responses(retention_days=30)` 接入研究总监的每日预检（`probes/director.py`），在 J3 之后作为系统上下文（`user_id is None`）的每日任务执行（定义于 `src/dalton/autonomy/probes/director.py:101-130` 的 `_run_daily_system_maintenance`，在 `src/dalton/autonomy/probes/director.py:152-156, 323-324` 以 `user_id=None` 调用）；② `src/dalton/llm/complete.py:733-750` 在 `complete_journal_failed` 外包 `try/except` 防止覆盖原始异常；③ `src/dalton/database/budget.py` 修复预算预留结算竞态，仅在子智能体用量落库后结算；④ 2026-10-21 在 `ad1b9544` 上运行 `scripts/dalton/realrun/g1_budget.py --cap-usd 0.3`（服务令牌双用户 `realrun-pm-a` / `realrun-user-b`）：用户 A 日额度设为当日已花现金 `$0.6580 + $0.30 = $0.9580`，主模型 `claude-sonnet-5-5-low-oauth` 派出现金子智能体 `deepseek-flash`（会话用量记录数 `13`，总花费计入 `settled_usd=$0.715753`），设置页临时 BYOK Key `deepseek` 探测花费 `$0.000040` 计入 `GET /api/v1/dalton/budget/today`，用户 A 触顶时 `spent_usd=$0.716431`（剩余额度 `$0.241569`），下一轮对话 `POST /api/v1/threads/{id}/messages` 返回 `HTTP 429`（`detail.type = "dalton_daily_budget"`），同时用户 B（日额度 `$0.31`，`deepseek-flash`）对话返回 `HTTP 200`（剩余额度 `$0.2998`）；`publish.review` 第二次调用命中缓存（`replayed=True, cached=True, cash_usd=$0.000000`），本轮总现金花费 `combined_cash_usd=$0.058583`（`<= $0.50`），附前端拒绝横幅截图 `var/dalton/trial-runs/realrun-2026-10-21/g1_budget_exhausted_banner.png`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G1.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g1_budget.json`） |
| **P0N.0 – P0N.5 / G5.2 / I3 / J5** 原生模型迁入 `src/dalton/` 与 Antigravity/Muse 修复 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J5（`ad1b9544` PR #44 `cd880f52`）完成：迁入 `src/dalton/llm/native_overlay.json` 与 `src/dalton/llm/providers/{antigravity,muse}`；`src/dalton/llm/providers/antigravity/oauth.py` 删除进程内 `_LAST_INITIATED_AT` 字典，在发起登录 `start_login`（`src/dalton/llm/providers/antigravity/oauth.py:239-243`）时写入 `oauth_login_start` 事件，回退路径 `check_login_complete`（`src/dalton/llm/providers/antigravity/oauth.py:451-468`，经辅助函数 `_get_latest_oauth_login_start`（第 73-106 行））比较令牌的更新时间 `updated_at` 和最近一次发起登录的时间 `initiated_at`。2026-10-21 在 `ad1b9544`（`deployment=docker`）上运行 `scripts/dalton/realrun/g5_broker_and_antigravity.py --mode g5.2 --date 2026-10-21`：测试用户 `realrun-pm-a` 通过 `POST /api/v1/oauth/antigravity/initiate` 与 `POST /api/v1/oauth/antigravity/callback` 完成真实回调地址粘贴与令牌交换（`missing_state=400, wrong_state=400, real_paste=200`），验证 `realrun-pm-a.connected=True` 且 `realrun-user-b.connected=False`，并用 `gemini-3.8-flash-low-oauth` 真实完成 4 轮工具调用（`tool_calls=4, tool_call_results=4, model_turns=5`，见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G5.2.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g5_2_antigravity.json`） |
| **G2 / I3 / J3b / J5** 路由接线修复与守门测试 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J3b（`ca822e62` PR #23）与 J5（`ad1b9544` PR #44 `cd880f52`）重做并验证：`src/dalton/app/router.py` 的 `_verify_workspace_access` 改为失败即拒绝（查不到返回 `404`、数据库异常返回 `503`、非所有者返回 `403`）；`tests/unit/dalton/test_router_wiring.py` 使用 `create_autospec` 替身；2026-10-21 在 `ad1b9544` 上运行 `scripts/dalton/realrun/g2_providers.py` 从 `GET /openapi.json` 动态枚举全部 `22` 条 `/api/v1/dalton` 路由并在 `realrun-pm-a` / `realrun-user-b` 下逐条真实调用通过（`uncalled_routes=0`），验证 PM 专属路由 `POST /api/v1/dalton/knowledge/{ws}/thesis/import` 对 `realrun-pm-a` 返回 `HTTP 200`、对 `realrun-user-b` 返回 `HTTP 403`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G2.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g2_providers.json`） |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | 🟦 代码完成 | 第四轮审查结论：**不进入 P0.9/P0.10**（见 `docs/dalton/dev-guide/review/SUMMARY-R4-20261019.md` 第 7 行）。本轮已完成 J1–J6，全部 8 份真实运行证据已在同一个合并后的干净提交 `ad1b9544` 上重新生成且 `valid=true`（其中 G4 除场景②内部文献子场景外完成，该子场景待有真实新入库资料的交易日再跑）。第五轮审查之后再决定。 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`, `G7`, `P0F.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1 / G7 / J5** researchdata 宿主机桥 | `dalton/main` | 🟨 真实运行验证 | 第四轮因 V1 退回后，在 J3（`8bbd1557` PR #17）与 J5（`ad1b9544` PR #44 `cd880f52`）重做并验证：2026-10-21 运行 `scripts/dalton/realrun/g7_bridge.py --backend http://127.0.0.1:8000 --bridge http://127.0.0.1:8960 --delay-s 245 --date 2026-10-21`：① 调用 ① 使用已入库测试钩子 `scripts/dalton/researchdata_bridge.py:168-190`（`DALTON_BRIDGE_TEST_MODE=1`、`DALTON_BRIDGE_TEST_DELAY_S=245`，在 `synthetic` 中声明 `committed_test_hook`）完成 `> 240s` 长读（`company-wiki:read`，`eff_timeout=600s`，实测 `elapsed_s=248.512s`，`HTTP 200`，`timed_out=False`），返回 `top_keys=['_declared_sources', '_sources']`、`_declared_sources=3`、`_sources=3`；② 调用 ② 针对常驻桥接服务 `http://127.0.0.1:8960`（关掉延迟开关）真实读取 `company-wiki:read` GOOGL（`elapsed_s=6.324s`）与 `sec:financials` GOOGL（`elapsed_s=2.218s`），均返回 `_declared_sources=3, _sources=3`；③ `zero_stderr_leakage=True`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G7.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g7_bridge.json`） |
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
| **P1.4 / G3.4 / I1 / J5** 旧认知迁移、清除编造数值与 `legacy_thesis` 隔离 | `dalton/main` | 🟨 真实运行验证 | `58e85d4e` (PR #6) & `ad1b9544` (PR #44 `cd880f52`)：`scripts/dalton/migrate_legacy_knowledge.py` 清除全部硬编码编造值（见 `var/dalton/migration/migration_report.json`），并按 `L34` 将从 `sources/legacy/thesis.md` 迁入的条目统一标记为 `source.type = "legacy_thesis"`（原因 `"迁移的旧认知不作为核实依据"`），在发布核验 C/D 环节中不作为核实依据、仅作为候选展示；2026-10-21 在 `ad1b9544` 上通过 `GET /api/v1/dalton/knowledge/{ws}/thesis/versions` 验证四家迁移公司（`META`/`AMZN`/`GOOGL`/`MSFT`）均保留 v1 初始迁移与 v2 `legacy_thesis` 降级版本（证据条数：`META=92, AMZN=119, GOOGL=116, MSFT=112`，见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G3.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g3_publish.json`） |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`, `G3`, `I1`, `I7`, `J2`, `J3`, `J5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `fix/P0J.J3-smuggled-cleanup` | 🟦 代码完成 | `58e85d4e` (PR #6) & `8bbd1557` (PR #17)：`paths.py` + 10 份 JSON Schema (draft 2020-12，`additionalProperties: false`，`thesis.schema.json` 新增 `legacy_thesis` 枚举值，`common.schema.json` 收敛 `company_guidance` 标签并保留 `LTM`/`filing`/`EV-`) |
| **PK.3 – PK.7 / H3 §3.3 / J3** 版本表、写入工具、字段建议+LLM确认、读序 | `dalton/main` | 🟨 真实运行验证 | `8bbd1557` (PR #17) & `ad1b9544` (PR #44 `cd880f52`)：`ops.py` 冻结确定性顶层路径别名表（永不改 `/official`），`difflib.get_close_matches` 产出候选建议交由 `assist_json("knowledge.repair_field")` 确认后改写并记 warning；`src/dalton/database/verdicts.py` 的 `next_verdict_id` 按日全局唯一序号分配；`src/dalton/knowledge/service.py` 的 `append_verdict` 在 Schema 校验前剥离瞬态字段并将被剥离字段名记入返回 `warnings[]`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G4.md`） |
| **PK.8 – PK.13 / G3 / I1 / J2 / J5** 发布核验堵住错放、删语义规则、九类非核验对象与按节缓存 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J2a（`1155a4e3` PR #13）、J2（`6d8843b5` PR #18）、J2b（`53bfb990` PR #26）、J5h（`89b9a7e5` PR #39）、J5k（`ad1b9544` PR #42）与 J5（PR #44 `cd880f52`）完成：① 删掉 `_COLLOQUIAL_HINT_RE` 门控、时长判 `incidental` 正则、`_can_be_incidental` 中的指标黑名单与版本白名单、免责声明标题整节跳过以及 publish 内写死的 `deepseek-flash` 重试；② 按 `L43` 限定非核验对象为九类并交由 LLM 判断，确定性守卫不留绕过通道，定义唯一 `EXEMPT_MARKERS` 常量，`normalize_period` 识别中文 `过去 12 个月`/`近 12 个月` 为 `LTM`；③ 2026-10-21 在 `ad1b9544` 上运行 `scripts/dalton/realrun/g3_publish.py`（`acceptance_pass=True`，金标准 `version=2` 共 `159` 条、有来源 `37` 条）：`false_verified_in_gold_count=0`，`cross_control_verified_count=0`（`0 / 505`），`false_unverified_in_gold_count=1 / 37 (2.70%)`，`misclassified_financial_incidental_count=0`（两份报告共排除 `incidental_total_combined=37` 个并全量逐条列出），`uncertain_ratio=13 / 876 (1.48%)`，标注覆盖 GOOGL `515/515`（`failed_batches=0`）、AMZN `398/398`（`failed_batches=0`），口语反例草稿抽出 `["近一半", "大半", "砍掉一半", "倍增"]`，`exempt_marker_count=1`（`〔假设〕`），金标准范围外已核实抽样 `0 / 17` 错放，单节微调重跑窗口内审计新调用 `5` 条、回放 `70` 条（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G3.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g3_publish.json`） |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`, `G6`, `I6`, `J5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3 / G6 / I6 / J5** 脚手架、`dalton-evidence`、财务镜头统一锚点与公司背景 | `dalton/main` | 🟨 真实运行验证 | 第四轮退回后在 J1（`cd27b9b9` PR #25）与 J5（`ad1b9544` PR #44 `cd880f52`）重做并验证：`netcash.py`、`return_score.py`、`earnings_quality.py` 通过 `--anchors` 读取 `anchors.py --allow-network` 联网（`yfinance`）获取的真实市值与汇率，同时传入 `--anchors` 与手填 `--market-cap-usd-mn`/`--fx-rate` 时报错退出（`rejected=True (exit_code=2)`）；报表科目输入来自 `tests/fixtures/dalton/g6/lenses_inputs.json`（声明为 `committed_fixture_input`，`illustrative=True`），验证受限现金单次扣除 `(basic_net_cash_usd - restricted_cash_usd_equiv) == strict_net_cash_usd`（`BABA`：`mcap=$270193.98M, fx=0.150053, basic=$64342.73M, restricted=$6182.18M, strict=$58160.55M (21.53%), return_score=5.6, quality=strong`；`0700.HK`：`mcap=$451919.29M, fx=0.150053, basic=$10938.86M, restricted=$510.18M, strict=$10428.68M (2.31%), return_score=3.3, quality=strong`；`META`：`mcap=$1768606.14M, fx=1.0, basic=$49229.0M, restricted=$120.0M, strict=$49109.0M (2.78%), return_score=2.2, quality=strong`，见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G6.md`、`var/dalton/trial-runs/realrun-2026-10-21/g6_lenses.json` 与 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-2026-10-21.md`） |
| **PS.4 – PS.18 / G6.1** 分析师 Skill、Workflow 与引用比对脚本 | `dalton/main` | 🟦 代码完成 | 清理残留 OpenClaw 旧工具名、`RunWorkflow` 参数名统一为 `params`、重写 `dalton-agenda` 自动任务模板与 `dalton-research-director` 去掉次数门、补齐 `dalton_registry.py` `requires`；`scripts/dalton/check_skill_refs.py` 扫描 22 个 skill：`ghost_hits=0, broken_refs=0` |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`, `G4`, `I4`, `I5`, `J3`, `J5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20 / G4 / I5 / J3 / J5** 自主议程低危项修复与自动任务触发真实验证 | `dalton/main` | 🟨（除场景②内部文献子场景外完成，该子场景待跑） | 第四轮退回后在 J3（`8bbd1557` PR #17）、J5i（`0781c0e8` PR #40）与 J5（`ad1b9544` PR #44 `cd880f52`）重做：**除场景②内部文献子场景外完成，该子场景待跑**（当日无新增内部文献，按 P0J §5 G4 待有真实新资料的交易日再跑）。① J3 删除 `board.py`、`probes/director.py`、`assist.py` 中按 `local-dev-user` 字面量特判的分支，所有非 `None` 的 `user_id` 一律按工作区所有者过滤；② 2026-10-21 在 `ad1b9544` 上运行 `scripts/dalton/realrun/g4_autonomy.py`（`valid=true`）：场景①真实 SEC 8-K（`0001628280-26-050596`）唤醒并写入结论 `VD-20261021-05 (NO_CHANGE)`；场景②真实 SEC Form 4（`0000950103-26-015324`）经 `assist.preflight.triage`（`triage_fresh_calls=1`）分诊跳过，场景②内部文献子场景未完成（当日无新增内部文献，待跑）；场景③a `dalton_agenda_note` 写入 `agenda v2 (source=tool, questions=1)`；场景③b 到期验证项 `V-201` 唤醒并写入 `VD-20261021-06 (THESIS_STRENGTHENED)`；场景④覆盖看板 `open_signals_by_ticker={"GOOGL": 0, "HOME": 0, "META": 0}`，且验证 `realrun-pm-a` 与 `realrun-user-b` 两用户隔离（`other_user_visible_pm_workspaces_on_board=0, other_user_visible_pm_verdicts=0, other_user_visible_pm_publish_requests=0`）；场景⑤ `realrun-idle-c` 每日研究总监零信号跳过（`status=skipped, reason=no_new_signals, cash_spend_usd=$0.00`）；场景⑥共享预算熔断（`watch_reason=budget_exhausted`）且一次性任务保持 `tm3_status=active`、顺延至 `next_run_at=2026-10-23T12:00:00Z`（见 `docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/G4.md` 与 `var/dalton/trial-runs/realrun-2026-10-21/g4_autonomy.json`） |
| **PK-UI.1 – PK-UI.5 / I4 / I1 / J2 / J3** 前端回绿、审批卡升级与中英文国际化 | `fix/P0J.J2-publish-delete-rules` | 🟦 代码完成 | `46f42833` (PR #1)、`58e85d4e` (PR #6)、`8bbd1557` (PR #17) & `6d8843b5` (PR #18)：修复 `PublishApprovalCard.test.tsx` 类型错误，补齐 `en-US.json` 与 `zh-CN.json` 中 `dalton.publish.*` / `dalton.budget.*` 双语键并新增真实 i18n `en-US` 渲染单测；`PublishApprovalCard.tsx` 展示标注覆盖率（`labeled/total`）、可展开 `incidental` 列表（显示原句与九类归属）、`failed_batch_details`（“第 N 批（第 X–Y 个数字）标注失败”）、按来源拆分的已核实计数、LLM 核实项草稿与引文并排对照、无来源项候选列表；其余 Dalton 组件待国际化键清单已整理入 `docs/dalton/dev-guide/P0.9-ui-walkthrough-keys.md` |

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
