# Dalton on LangAlpha 开发进展记录 (PROGRESS.md)

最后更新：2026-10-18

本文件按 `docs/dalton/dev-guide/README.md` 的工作包与步骤实时记录开发进展、分支与提交 SHA、【待验证】项的实测结论、以及已落地的上游 `DALTON-HOOK` 挂钩。

---

## 1. 工作包与步骤进展总表

### WP-A: OpenClaw Broker v2 (`P0.2` – `P0.3`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.2** broker v2 spike | `feat/P0.2-broker-spike` | ⏳ 待执行 | 验证 `openclaw/plugin-sdk/llm` K1–K6，产出 spike 报告 |
| **P0.3** broker v2 完整实现 | `feat/P0.3-broker` | ⏳ 待执行 | `integrations/openclaw/dalton-langalpha-broker/` + `install_broker_v2.sh` |

### WP-B: 平台接入与本地预算 (`P0.1`, `P0.4` – `P0.9`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | 🔄 进行中 | 已安装 `uv 0.13.1`；完成 `docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml`（整栈 colima 容器待 Mac 重启释放内存后启动） |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-llm-overlay` | ⏳ 待执行 | `openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py` + `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `feat/P0.5-openclaw-smoke` | ⏳ 待执行 | `--target broker|gateway|native` (B0–B16) |
| **P0.6** 角色模型配置与切换 | `feat/P0.6-role-policy` | ⏳ 待执行 | `role_policy.py` (`family_of`, `is_text_only`, `check_roles`) + `apply_role_models.py` |
| **P0.7** 设置页同步按钮与健康卡片 | `feat/P0.7-openclaw-sync-ui` | ⏳ 待执行 | `d002_openclaw_sync_runs.py`、`openclaw_admin.py`、`OpenClawCard.tsx`、`SyncDiffDialog.tsx` |
| **P0.8** 本地日美元预算 `LocalBudgetPort` | `feat/P0.8-local-budget` | ⏳ 待执行 | `d001_budget.py`、`src/dalton/budget/`、`DaltonBudgetCard.tsx` + 挂钩 |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | ⏳ 待环境启动 | 待 Mac 重启与 colima 启动后跑端到端走查 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1** researchdata 宿主机桥 | `feat/P0-D.1-researchdata-bridge` | ⏳ 待执行 | `researchdata_bridge.py`、`source_identity.py`、launchd 模板、`install_bridge.sh`、`check_bridge.sh` |
| **P0-D.2** 注册内置 HTTP MCP + 超时补丁 | `feat/P0-D.2-dalton-data-mcp` | ⏳ 待执行 | `plugins/dalton_data/` + `DALTON-HOOK(mcp-call-timeout)` |
| **P0-D.3 / D.3b** 来源面板逐篇展开 | `feat/P0-D.3-provenance` | ⏳ 待执行 | `declared_sources.py`、`ResearchDocSource.tsx`、`d004_provenance_locator.py` |
| **P0-D.4** 基金 wiki 只读覆盖评估 | `feat/P0-D.4-wiki-eval` | ⏳ 待执行 | 实测 `company-wiki` 6 种操作 |
| **P0-D.5** `dalton_sec` 沙箱 stdio MCP | `feat/P0-D.5-dalton-sec` | ⏳ 待执行 | `dalton_sec_mcp_server.py`（R1–R10 规则）+ 真实 EDGAR 对数 + 契约单测 |
| **P0-D.6** 价格与一致预期验证 | `feat/P0-D.6-estimates` | ⏳ 待执行 | 验证 `estimates` 与 `yfinance` |

### WP-D: 方法论与入职 (`P1.1` – `P1.5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P1.1** `dalton-conventions` 方法论 skill | `feat/P1.1-dalton-conventions` | ⏳ 待执行 | `plugins/dalton/skills/dalton-conventions/` + `dalton_registry.py` + `DALTON-HOOK(skills-registry)` |
| **P1.2** 覆盖名单 `coverage.yaml` | `feat/P1.2-coverage-list` | ⏳ 待执行 | `build_coverage.py` + `config/dalton/coverage.yaml` (Core 27 / Watch 78) |
| **P1.3** 公司工作区初始化 | `feat/P1.3-init-company` | ⏳ 待执行 | `dalton-init-company` skill + `init_company.py` + `init_company_workspace.py` |
| **P1.4** 旧认知迁移 (META/AMZN/GOOGL/MSFT) | `feat/P1.4-legacy-migration` | ⏳ 待执行 | `migrate_legacy_knowledge.py`（只读抽取 `$OCW` 与 `$CORE_DB`） |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `feat/PK.1-2-knowledge-schemas` | ⏳ 待执行 | `paths.py` + 10 份 JSON Schema (draft 2020-12) |
| **PK.3 – PK.7** 版本表、写入工具、事后检测、读序、导入接口 | `feat/PK.3-7-knowledge-layer` | ⏳ 待执行 | `d003_knowledge.py`、`ops.py`、`validate.py`、`render.py`、`service.py`、`reconcile.py`、`knowledge_tools.py` |
| **PK.8 – PK.13** 发布治理与数字核验 | `feat/PK.8-13-publish-governance` | ⏳ 待执行 | `d005_publications.py`、`numbers.py`、`match.py`、`audit.py`、`review.py`、`publish_tools.py`、`dalton-reviewer.md` |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3** 脚手架、`dalton-evidence`、财务镜头、公司背景 | `feat/PS.0-3-foundation-skills` | ⏳ 待执行 | `dalton-evidence.md`、`dalton-financial-lenses`（含脚本）、`dalton-company-background` |
| **PS.4 – PS.6** bullbear、初次覆盖、完整覆盖 | `feat/PS.4-6-coverage-workflows` | ⏳ 待执行 | `dalton-bullbear` + `workflows/{bullbear-doc,initial-coverage,full-coverage}/workflow.js` |
| **PS.7 – PS.12** 财报链、异动、thesis 增量、公告 | `feat/PS.7-12-active-coverage` | ⏳ 待执行 | 财报日历/前瞻/快讯/深度点评 (`earnings-deep-update`)、股价异动、thesis delta、公告核查 + 自动任务模板 |
| **PS.13 – PS.18** 周会、专题、conviction call、PM 问答、评分表 | `feat/PS.13-18-weekly-and-call` | ⏳ 待执行 | `weekly-coverage-review`、`topic-research`、`dalton-call`、`dalton-pm-qa`、评分样本模板 |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20** 议程、预检、退避、研究总监 | `feat/PK.14-20-autonomy` | ⏳ 待执行 | `d006_autonomy.py`、`preflight.py`、7 个探针、`backoff.py`、`board.py`、`director_tools.py` |
| **PK-UI.1 – PK-UI.5** 界面小改与中文 | `feat/PK-UI-frontend` | ⏳ 待执行 | `VITE_DALTON_MODE` 隐藏行情/仪表盘、`PublishApprovalCard`、中英文文案 |

---

## 2. 【待验证】项实测记录

1. **宿主机 `uv` 安装状态（P0.1）**：实测宿主机初始无 `uv`，已通过 `brew install uv` 安装 `uv 0.13.1`（`/opt/homebrew/bin/uv`）。
2. **本机内存与网关状态（P0.1）**：宿主机已连续运行 31 天，`fseventsd` 占用较高内存，`colima` 当前未启动；OpenClaw 网关（`127.0.0.1:18789/v1/models` 返回 401）和 AlphaEngine MCP（`127.0.0.1:8950/health` 返回 `status: ready`）均正常运行，`$CORE_DB`（6.2 GB）在外置卷 `/Volumes/EveSSD` 上已挂载可读。
3. **`subagents.definitions.<name>.model` 部分字段覆盖（P0.6 附录 B #8）**：已核实 `src/ptc_agent/config/agent.py` L200–215 与 `src/ptc_agent/agent/subagents/merge.py` L103–108：当 `<name>` 属于 `BUILTIN_SUBAGENTS`（如 `research`、`data-prep`、`general-purpose`）时，`SubagentConfig` 不强制要求 `description`，`SubagentRegistry` 按字段合并覆盖内置定义的 `model`。
4. **`security.max_execution_time` 在 `agent_config.yaml` 中的加载路径（P0-D.2）**：已核实 `src/ptc_agent/config/loaders.py` L206、L284：`load_from_dict` 当前直接调用 `create_default_security_config()`（默认 `max_execution_time=300` 秒），未读取 YAML 顶层 `security:` 段。在默认 `call_timeout_s: 300`（桥端上限 `300 - 25 = 275` 秒）下，300 秒已够用；若后续需将 `call_timeout_s` 提升至 660 秒，可在 `loaders.py` 增加读取 `config_data.get("security")`。
