# Dalton on LangAlpha 开发进展记录 (PROGRESS.md)

最后更新：2026-10-18

本文件按 `docs/dalton/dev-guide/README.md` 的工作包与步骤实时记录开发进展、分支与提交 SHA、【待验证】项的实测结论、以及已落地的上游 `DALTON-HOOK` 挂钩。

---

## 1. 工作包与步骤进展总表

### WP-A: OpenClaw Broker v2 (`P0.2` – `P0.3`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.2** broker v2 spike | `feat/P0.2-3-broker-v2` | ✅ 已完成 | `0360262a` (merge `b1c40b43`)：验证 `openclaw/plugin-sdk/llm` K1–K6 全部通过（见 `var/dalton/smoke/broker_spike_20261018_093702.json`） |
| **P0.3** broker v2 完整实现 | `feat/P0.2-3-broker-v2` | ✅ 已完成 | `0360262a` (merge `b1c40b43`)：`integrations/openclaw/dalton-langalpha-broker/`（10/10 Node 单测通过）+ `install_broker_v2.sh` 已部署至 `~/.openclaw/managed-plugins/` |

### WP-B: 平台接入与本地预算 (`P0.1`, `P0.4` – `P0.9`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | ✅ 已完成 | `7641d792` (merge `c81151b3`)：`docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml` |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-8-llm-overlay-and-budget` | ✅ 已完成 | `7061b70e` (merge `0a7f5b99`)：`openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py`（实测同步 27 个模型，0 凭据落盘）+ `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `feat/P0.4-8-llm-overlay-and-budget` | ✅ 已完成 | `7061b70e` (merge `0a7f5b99`)：`openclaw_smoke.py --target broker` 实测 B0–B14 全部通过（13 pass, 1 warn, 0 fail；含 `B14` `create_llm()` 端到端调用） |
| **P0.6** 角色模型配置与切换 | `feat/P0.4-8-llm-overlay-and-budget` | ✅ 已完成 | `7061b70e` (merge `0a7f5b99`)：`role_policy.py` (`family_of`, `is_text_only`, `check_roles`) + `apply_role_models.py`（`anthropic-claude` vs `openai-gpt` 家族隔离，0 warning） |
| **P0.7** 设置页同步按钮与健康卡片 | `feat/P0.4-8-llm-overlay-and-budget` | ✅ 已完成 | `7061b70e` (merge `0a7f5b99`)：`d002_openclaw_sync_runs.py`、`openclaw_admin.py`、`OpenClawCard.tsx`、`SyncDiffDialog.tsx` |
| **P0.8** 本地日美元预算 `LocalBudgetPort` | `feat/P0.4-8-llm-overlay-and-budget` | ✅ 已完成 | `7061b70e` (merge `0a7f5b99`)：`d001_budget.py`、`src/dalton/budget/{service,port,gate}.py`、`DaltonBudgetCard.tsx` + `test_budget_port.py`（8/8 单测通过） |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | ⏳ 待容器启动 | 代码、单测与宿主机网关/桥接全部就绪；待 Mac 重启释放内存并启动 `colima` 后跑浏览器端到端走查 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1** researchdata 宿主机桥 | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：`researchdata_bridge.py`、`source_identity.py`、launchd 服务 `127.0.0.1:8960` 实测 `check_bridge.sh` 6 项全绿 |
| **P0-D.2** 注册内置 HTTP MCP + 超时补丁 | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：`plugins/dalton_data/` + `DALTON-HOOK(mcp-call-timeout)` |
| **P0-D.3 / D.3b** 来源面板逐篇展开 | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：`declared_sources.py`、`ResearchDocSource.tsx`、`d004_provenance_locator.py` + `test_declared_sources.py`（13/13 单测通过） |
| **P0-D.4** 基金 wiki 只读覆盖评估 | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：实测 `company-wiki` 6 种操作与 G1 反向提及查询 |
| **P0-D.5** `dalton_sec` 沙箱 stdio MCP | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：`dalton_sec_mcp_server.py`（R1–R10 规则）+ META/MSFT/AMZN 真实 EDGAR 对数 + 契约与期间单测（12/12 通过） |
| **P0-D.6** 价格与一致预期验证 | `feat/P0-D.1-6-data-and-sec` | ✅ 已完成 | `4d4b5b48` (merge `46e742d1`)：实测 `estimates` (Yahoo Finance) 与 `yfinance` 对 META/MSFT/AMZN 的字段与非日历财年映射 |

### WP-D: 方法论与入职 (`P1.1` – `P1.5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P1.1** `dalton-conventions` 方法论 skill | `feat/P1.1-4-onboarding-and-migration` | ✅ 已完成 | `e2bd1396` (merge `3da37d9e`)：`plugins/dalton/skills/dalton-conventions/`（SKILL.md + 8 份 references 均 ≤200 行）+ `dalton_registry.py` |
| **P1.2** 覆盖名单 `coverage.yaml` | `feat/P1.1-4-onboarding-and-migration` | ✅ 已完成 | `e2bd1396` (merge `3da37d9e`)：`build_coverage.py --check` 通过（Core 27 / Watch 78，共 105 家，5 项已知冲突显式化解） |
| **P1.3** 公司工作区初始化 | `feat/P1.1-4-onboarding-and-migration` | ✅ 已完成 | `e2bd1396` (merge `3da37d9e`)：`dalton-init-company` skill + `init_company.py` + `init_company_workspace.py`（三态幂等验证通过） |
| **P1.4** 旧认知迁移 (META/AMZN/GOOGL/MSFT) | `feat/P1.1-4-onboarding-and-migration` | ✅ 已完成 | `e2bd1396` (merge `3da37d9e`)：`migrate_legacy_knowledge.py` 实测 4 家 × 8 份 JSON 全部通过 PK.2 Schema 校验，27/27 条 OpenClaw D1..Dn 争点完整迁移，外部源零改动 |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `feat/PK.1-13-knowledge-and-publish` | ✅ 已完成 | `0a5bbdce` (merge `48ec4667`)：`paths.py` + 10 份 JSON Schema (draft 2020-12，`additionalProperties: false`) |
| **PK.3 – PK.7** 版本表、写入工具、事后检测、读序、导入接口 | `feat/PK.1-13-knowledge-and-publish` | ✅ 已完成 | `0a5bbdce` (merge `48ec4667`)：`d003_knowledge.py`、`ops.py`、`validate.py`、`render.py`、`service.py`、`reconcile.py`、`knowledge_tools.py`（12/12 单测通过） |
| **PK.8 – PK.13** 发布治理与数字核验 | `feat/PK.1-13-knowledge-and-publish` | ✅ 已完成 | `0a5bbdce` (merge `48ec4667`)：`d005_publications.py`、`numbers.py`、`match.py`、`audit.py`、`review.py`、`publish_tools.py`、`dalton-reviewer.md`（17/17 单测通过） |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3** 脚手架、`dalton-evidence`、财务镜头、公司背景 | `feat/PS.0-18-skills-and-workflows` | ✅ 已完成 | `ed3d902c` (merge `1ed687cb`)：`dalton-evidence.md`、`dalton-financial-lenses`（含四组确定性脚本与 12 项单测）、`dalton-company-background` |
| **PS.4 – PS.6** bullbear、初次覆盖、完整覆盖 | `feat/PS.0-18-skills-and-workflows` | ✅ 已完成 | `ed3d902c` (merge `1ed687cb`)：`dalton-bullbear`、`dalton-initial-coverage`、`dalton-full-coverage` + `workflows/{bullbear-doc,initial-coverage,full-coverage}/workflow.js` |
| **PS.7 – PS.12** 财报链、异动、thesis 增量、公告 | `feat/PS.0-18-skills-and-workflows` | ✅ 已完成 | `ed3d902c` (merge `1ed687cb`)：财报日历/前瞻/快讯/深度点评 (`earnings-deep-update`)、股价异动、thesis delta、公告核查 + 10 份自动任务模板 |
| **PS.13 – PS.18** 周会、专题、conviction call、PM 问答、评分表 | `feat/PS.0-18-skills-and-workflows` | ✅ 已完成 | `ed3d902c` (merge `1ed687cb`)：`weekly-coverage-review`、`topic-research`、`dalton-call`、`dalton-pm-qa`、`dalton-agenda`、`dalton-research-director`、评分样本模板 |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20** 议程、预检、退避、研究总监 | `feat/PK.14-20-autonomy-and-ui` | ✅ 已完成 | `94552207` (merge `95becbf1`)：`d006_autonomy.py`、`preflight.py`、8 个探针、`backoff.py`、`board.py`、`director_tools.py` + `test_autonomy_preflight.py`（10/10 单测通过） |
| **PK-UI.1 – PK-UI.5** 界面小改与中文 | `feat/PK.14-20-autonomy-and-ui` | ✅ 已完成 | `94552207` (merge `95becbf1`)：`VITE_DALTON_MODE` 隐藏行情/仪表盘、`PublishApprovalCard`、中英文文案（`pnpm typecheck` 0 error，前端 90/90 单测通过） |

---

## 2. 【待验证】项实测记录

1. **宿主机 `uv` 安装状态（P0.1）**：实测宿主机初始无 `uv`，已通过 `brew install uv` 安装 `uv 0.13.1`（`/opt/homebrew/bin/uv`）。
2. **本机内存与网关状态（P0.1）**：宿主机已连续运行 31 天，`fseventsd` 占用较高内存，`colima` 当前未启动；OpenClaw 网关（`127.0.0.1:18789`）和 AlphaEngine MCP（`127.0.0.1:8950/health` 返回 `status: ready`）均正常运行，`$CORE_DB`（6.2 GB）在外置卷 `/Volumes/EveSSD` 上已挂载可读。
3. **OpenClaw `plugin-sdk/llm` 运行时契约（P0 附录 B #1–#4, #12）**：
   - `prepareSimpleCompletionModelForAgent` 返回的 `prepared.model` 挂载了 `Symbol("openclaw.modelLlmRuntime")`，其内部 `completionTransport` 绑有 `streamSimple` 闭包；Broker v2 通过 `resolveStreamExecutionModel(model)` 提取该绑定对象后，所有传输层（`openai-responses`、`openai-completions`、`google-generative-ai`、`anthropic-messages`）均正常工作。
   - `toolChoice` 在不同传输层存在命名差异（`openai-responses` 读取 `options.toolChoice`，而 `openai-completions` 未直接消费 `options.toolChoice`）；Broker v2 在 `invoke.mjs` 中通过 `buildToolChoiceStreamOptions` 同时传入 `toolChoice` 与 `onPayload` 钩子，实测 `xai/grok-4.7`、`google/gemini-3.8-flash`、`openai/gpt-6-astra` 的 K1–K6 全部通过。
   - 经 Broker v2 调用时不会触发 OpenClaw workspace prompt 注入：实测 `B6` 的未缓存 `prompt_tokens` 为 91（远低于 100 阈值），且 `B7` 实测无任何内部工具泄漏。
4. **`subagents.definitions.<name>.model` 部分字段覆盖（P0 附录 B #8）**：已核实 `src/ptc_agent/config/agent.py` L200–215 与 `src/ptc_agent/agent/subagents/merge.py` L103–108：当 `<name>` 属于 `BUILTIN_SUBAGENTS`（如 `research`、`data-prep`、`general-purpose`）时，`SubagentConfig` 不强制要求 `description`，`SubagentRegistry` 按字段合并覆盖内置定义的 `model`。
5. **`security.max_execution_time` 在 `agent_config.yaml` 中的加载路径（P0-D.2）**：已核实 `src/ptc_agent/config/loaders.py` L206、L284：`load_from_dict` 当前直接调用 `create_default_security_config()`（默认 `max_execution_time=300` 秒），未读取 YAML 顶层 `security:` 段。在默认 `call_timeout_s: 300`（桥端上限 `300 - 25 = 275` 秒）下，300 秒已够用。
6. **基金 Wiki 只读覆盖与 G1 反向提及实测（P0-D.4）**：
   - 实测 `~/.openclaw/workspace/wiki` 共含 26 家公司与 8 篇专题（如 `ai_cloud_Hyperscaler_CapEx_ROI`、`ai_semis_CoWoS_HBM_Supply_Chain` 等）。
   - G1 反向提及查询：通过 `source="company-wiki", action="search", query="Google TPU Meta MTIA Marvell Broadcom custom ASIC"`（或 `source="all"` 跨公司 + 专题全文检索）可直接命中 `companies/MRVL/industry.md`、`companies/AVGO/relationships.md`、`companies/META/overview.md` 等跨公司供应链段落。
7. **SEC EDGAR 期间语义与真实对数（P0-D.5）**：
   - **META 2025Q2 vs H1 2025**：10-Q 同时披露 ~91 天单季（Revenue `$47,516M`）与 ~181 天半年累计（`$89,830M`），`dalton_sec` 按天数桶精确区分 `Q2` 与 `YTD_6M`，无混用。
   - **MSFT FY2026Q1**：period end `2025-09-30` 在 `fy_end_month=6` 下正确归一为 `FY2026Q1`（Revenue `$77,673M`），`calendar_equivalent="CY2025Q3"` 显式标注。
   - **AMZN 2025Q4 派生单季**：10-K 仅披露 FY2025 全年（`$716,924M`），`dalton_sec` 自动由 `FY2025 - (Q1+Q2+Q3)` 派生单季 Q4（`$213,386M`），标注 `derived=True`、`derivation_formula="FY2025 - (2025Q1 + 2025Q2 + 2025Q3)"` 及全部 4 份源 10-K/10-Q 的 accession number。
8. **价格与一致预期非日历财年映射（P0-D.6）**：
   - `researchdata(source="estimates")` 底层调用 Yahoo Finance（免费源），返回 `eps_estimate`、`revenue_estimate`、`eps_trend`、`eps_revisions`、`earnings_history`、`growth_estimates`。
   - 对 MSFT（6 月财年），Yahoo Finance 将 `0q`/`+1q` 的 `endDate` 标为日历季末（`2026-09-30` / `2026-12-31`），需结合 `dalton_sec` 的 `fy_end_month=6` 映射为 `FY2027Q1` / `FY2027Q2`。

---

## 3. 测试与挂钩检查汇总

- **`./scripts/dalton/check_hooks.sh --strict`**：`OK: DALTON-HOOK check passed.`（上游 `models.json`、`providers.json`、`agent_config.yaml`、`config.yaml` 相对基线 `9a03d3ac` 零改动）。
- **Broker v2 Node 单测**：`10 pass, 0 fail`。
- **Python 后端单测**：`tests/unit/dalton/`（72 passed）、`tests/unit/middleware/skills/`（237 passed）、`tests/unit/mcp_servers/`（108 passed）、`tests/unit/server/services/test_bundled_plugins.py`（1 passed），共 **418 passed, 0 failed**。
- **Web 前端类型检查与单测**：`pnpm typecheck` 0 errors；`vitest` 4 个测试集共 **90 passed, 0 failed**。
