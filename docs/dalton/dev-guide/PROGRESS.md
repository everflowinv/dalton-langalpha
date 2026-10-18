# Dalton on LangAlpha 开发进展记录 (PROGRESS.md)

最后更新：2026-10-18

本文件按 `docs/dalton/dev-guide/README.md`、`P0G-review-fixes.md`、`P0N-native-models.md`、`P0F-acceptance-fixes.md` 的工作包与步骤实时记录开发进展、分支与提交 SHA、【待验证】项的实测结论、以及已落地的上游 `DALTON-HOOK` 挂钩。

状态三级标记（按 `P0F.5` 规范）：
- `🟦 代码完成`：代码、数据库迁移、前端组件与单元测试全部通过。
- `🟨 真实运行验证`：已跑真实服务/脚本/网络对数/冒烟验证并留下可复查报告（待 PM/Lead 最终验收或容器全栈回归后升为 `🟩`）。
- `🟩 lead 验收通过`：仅由 Lead / PM 验收签字后标记。

---

## 1. 工作包与步骤进展总表

### WP-A: OpenClaw Broker v2 (`P0.2` – `P0.3`, `G5.1`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.2** broker v2 spike | `feat/P0.2-3-broker-v2` | 🟨 真实运行验证 | `0360262a` (merge `b1c40b43`)：验证 `openclaw/plugin-sdk/llm` K1–K6 全部通过（见 `var/dalton/smoke/broker_spike_20261018_093702.json`） |
| **P0.3 / G5.1** broker v2 完整实现与落盘防重 | `dalton/main` | 🟨 真实运行验证 | `integrations/openclaw/dalton-langalpha-broker/`（13/13 Node 单测通过）：磁盘幂等落盘 (`0600`、`pending` 重启转 `indeterminate` 返 `409 idempotency_indeterminate`、`failed_no_output` 可重试)、客户端断开不中止上游、`/health` 字段对齐、`B8` `dalton_meta.reasoning_effort` 回显；已部署至 `~/.openclaw/managed-plugins/` 并通过 `openclaw_smoke.py --target broker --checks B1,B2,B8`（13 pass, 0 fail） |

### WP-B: 平台接入、原生模型与本地预算 (`P0.1`, `P0.4` – `P0.9`, `P0N`, `G0`, `G1`, `G2`, `G5.2`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | 🟨 真实运行验证 | `7641d792` (merge `c81151b3`)：`docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml` |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-8-llm-overlay-and-budget` | 🟨 真实运行验证 | `7061b70e` (merge `0a7f5b99`)：`openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py`（实测同步 27 个模型，0 凭据落盘）+ `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `dalton/main` | 🟨 真实运行验证 | `openclaw_smoke.py --target broker` 实测 B0–B14 全部通过（含 `B8` 推理强度回显与 `B14` `create_llm()` 端到端调用，0 warn, 0 fail） |
| **P0.6** 角色模型配置与切换 | `dalton/main` | 🟨 真实运行验证 | `role_policy.py` (`family_of`, `is_text_only`, `check_roles`, `select_independent_reviewer`) + `apply_role_models.py`（含 `assist` 角色默认 `gemini-3.8-flash-low-oauth`、备选 `deepseek-flash`；`*-contributor-oauth` 禁止做角色模型） |
| **P0.7 / P0N.8** 设置页同步按钮与模型供应商状态卡 | `dalton/main` | 🟦 代码完成 | `d002_openclaw_sync_runs.py`、`openclaw_admin.py`、`provider_status.py`、`OpenClawCard.tsx`、`ProviderStatusCard.tsx`、`ProviderRow.tsx` |
| **P0.8 / G1** 本地日美元预算 `LocalBudgetPort` 与 `d007` 账本 | `dalton/main` | 🟦 代码完成 | `d001_budget.py`、`d007_llm_call_journal.py`（`dalton_cash_usd` SQL 函数按单次调用 `billing_type != 'oauth'` 计现金、BYOK 现金模型同样计入；修复主模型为 OAuth 时绕过现金门禁；`release()` 校验 `generation`）、`dalton_complete` 服务端准入+记账+防重、`assist_json` 统一便宜 LLM 辅助入口（`test_budget_port.py` 13/13 通过） |
| **P0N.0 – P0N.5 / G5.2** 原生模型迁入 `src/dalton/` 与 Antigravity/Muse 修复 | `dalton/main` | 🟨 真实运行验证 | 还原上游 `models.json`/`providers.json`/`extension/__init__.py`/`oauth.py`/`test_manifest_integrity.py`/`Dashboard/utils/api.ts` 至 `8e0dce94` 零 diff；迁入 `src/dalton/llm/native_overlay.json`（23 模型）与 `src/dalton/llm/providers/{antigravity,muse}`；完成 `G5.2` R6-1~R6-13 修复（思考签名只取末条、Gemini 3 `thinkingConfig` vs 2.5 `thinkingBudget`、30s 单端点连接超时、凭证刷新锁、禁 `verify=False`） |
| **G2** 路由接线修复与守门测试 | `dalton/main` | 🟦 代码完成 | 修复 `/api/v1/dalton/*` 全部 7 个死接口 + 3 个供应商探测接口 + 工作区归属校验；新增 `tests/unit/dalton/test_router_wiring.py`（26/26 通过） |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | 🟦 代码完成 | 待容器环境启动后执行浏览器端到端走查 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`, `G7.1`, `P0F.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1 / G7.1** researchdata 宿主机桥 | `dalton/main` | 🟨 真实运行验证 | `researchdata_bridge.py`、`source_identity.py`、launchd 服务 `127.0.0.1:8960` 实测 `check_bridge.sh` 6 项全绿；`G7.1` 修复启动失败仅记异常类名与退出码（不打 `stderr`），全文读取动作超时对齐为 600s |
| **P0-D.2** 注册内置 HTTP MCP + 超时补丁 | `feat/P0-D.1-6-data-and-sec` | 🟦 代码完成 | `plugins/dalton_data/` + `DALTON-HOOK(mcp-call-timeout)` |
| **P0-D.3 / D.3b** 来源面板逐篇展开 | `feat/P0-D.1-6-data-and-sec` | 🟦 代码完成 | `declared_sources.py`、`ResearchDocSource.tsx`、`d004_provenance_locator.py` + `test_declared_sources.py`（13/13 单测通过） |
| **P0-D.4 / P0F.4** 基金 wiki 只读覆盖评估 | `dalton/main` | 🟨 真实运行验证 | `scripts/dalton/wiki_coverage_report.py` 实测 `~/.openclaw/workspace/wiki` 共 **174 个公司目录、27 个行业/主题目录、`vectors.db` 1,086 篇文档**；覆盖池 105 家中 **51 家有 wiki 目录**（Core `24/27`，仅缺 `MCO`/`NDAQ`/`NTES`；Watch `27/78`），检索命中 `51/51`（见 `var/dalton/reports/wiki_coverage_20261018.md`） |
| **P0-D.5** `dalton_sec` 沙箱 stdio MCP | `feat/P0-D.1-6-data-and-sec` | 🟨 真实运行验证 | `dalton_sec_mcp_server.py`（R1–R10 规则）+ META/MSFT/AMZN 真实 EDGAR 对数 + 契约与期间单测（12/12 通过） |
| **P0-D.6** 价格与一致预期验证 | `feat/P0-D.1-6-data-and-sec` | 🟨 真实运行验证 | 实测 `estimates` (Yahoo Finance) 与 `yfinance` 对 META/MSFT/AMZN 的字段与非日历财年映射 |

### WP-D: 方法论与入职 (`P1.1` – `P1.5`, `G3.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P1.1** `dalton-conventions` 方法论 skill | `feat/P1.1-4-onboarding-and-migration` | 🟦 代码完成 | `plugins/dalton/skills/dalton-conventions/`（SKILL.md + 8 份 references 均 ≤200 行）+ `dalton_registry.py` |
| **P1.2** 覆盖名单 `coverage.yaml` | `feat/P1.1-4-onboarding-and-migration` | 🟨 真实运行验证 | `build_coverage.py --check` 通过（Core 27 / Watch 78，共 105 家，5 项已知冲突显式化解） |
| **P1.3** 公司工作区初始化 | `dalton/main` | 🟨 真实运行验证 | `dalton-init-company` skill + `init_company.py`（自动从 `coverage.yaml` 写入 `cik` 到 `data/company.json`）+ `init_company_workspace.py`（三态幂等验证通过） |
| **P1.4 / G3.4** 旧认知迁移与清除编造数值 | `dalton/main` | 🟨 真实运行验证 | `migrate_legacy_knowledge.py` 清除全部硬编码编造值（`65/35`、`2026-07-29`、`15.0`、`2026-11-15` 等全部改为 `null` 并标明 `"迁移时未计算，待首次研究补充"`），重新生成 `var/dalton/migration`（32/32 Schema 通过，27/27 条争点完整迁移，编造字面量 grep 0 命中），并提供 `remediate_migrated_workspace` 修正函数 |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`, `G3.1` – `G3.3`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `dalton/main` | 🟦 代码完成 | `paths.py` + 10 份 JSON Schema (draft 2020-12，`additionalProperties: false`，支持迁移待补字段 `null`) |
| **PK.3 – PK.7** 版本表、写入工具、字段自动修复、读序 | `dalton/main` | 🟦 代码完成 | `d003_knowledge.py`、`ops.py`（支持常见路径别名与 `assist_json("knowledge.repair_field")` 自动修复字段名并记 warning；卡住问题退避改为 `stalled_duration / 2` 夹在 `[24h, 30d]`，新证据立即清除退避）、`validate.py`、`render.py`、`service.py`、`reconcile.py`、`knowledge_tools.py` |
| **PK.8 – PK.13 / G3.1 – G3.3** 发布治理与“代码抽取 + LLM 判断 + 代码复核” | `dalton/main` | 🟦 代码完成 | `numbers.py`（屏蔽 SEC accession number、表头行列表头单位继承、`assist_json("audit.label_figures")` 抽取中文口语数字如“三成”）、`match.py` & `audit.py`（统一数值换算对比 + `assist_json("audit.judge")` + 代码复核原文引用存在性与数值一致；四类结论 `verified`/`contradicted`/`unverified`/`uncertain`，`uncertain` 不阻断发布交 PM 审批卡决定）、`review.py`（经 `dalton_complete` 准入记账，拒绝未知作者模型盲选）、`files.py`（删除断裂 PDF 承诺）、`workspaces.py`（`workspace-delete-guard` 禁止删除有正式发布成果的工作区） |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`, `G6.1`, `G6.2`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3 / G6.2** 脚手架、`dalton-evidence`、财务镜头、公司背景 | `dalton/main` | 🟨 真实运行验证 | 修复财务镜头 3 项高危 + 4 项中危问题：删除写死兜底汇率表（非美股必须显式 `--fx-rate` 或 `--allow-network`）、支持 `--input` JSON 与 `--beginning-assets`/`--ending-assets`、CCC 任一分量缺失返回 `null`、始终扣除受限现金、保留原币种金额、统一 `market_cap_usd_mn`；实测 `BABA`、`0700.HK`、`META` 真实跑通，`test_financial_lenses.py` 23/23 通过 |
| **PS.4 – PS.18 / G6.1** 分析师 Skill、Workflow 与引用比对脚本 | `dalton/main` | 🟦 代码完成 | 清理残留 OpenClaw 旧工具名（统一为 `researchdata.research`、`x_api.search_posts`、`web_fetch`、`get_company_facts`、`list_filings`）、`RunWorkflow` 参数名统一为 `params`、重写 `dalton-agenda` 自动任务模板与 `dalton-research-director` 去掉次数门、补齐 `dalton_registry.py` `requires`；新增 `scripts/dalton/check_skill_refs.py`（扫描 22 个 skill：`ghost_hits=0, broken_refs=0`） |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`, `G4`, `P0F.3`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20 / G4 / P0F.3** 4 信号探针、LLM 分诊、信号生命周期、去掉次数门 | `dalton/main` | 🟦 代码完成 | 删除 `price.py` 与 `consensus.py` 探针，保留 `sec`/`internal`/`agenda`/`earnings` 4 探针；`preflight.py` 45s 总超时保护，10-Q/10-K/8-K 2.02 直接唤醒，其余新公告与新内部资料经 `assist_json("preflight.triage")` 批量分诊（LLM 失败保守唤醒）；`knowledge_append_verdict` 自动标记信号已处理，超 7 天未处理信号自动过期；总监空转日 `wake=False` 零消耗；`DALTON-HOOK(no-strike)` 豁免 `dalton-*` 连续失败停用并按时间退避 |
| **PK-UI.1 – PK-UI.5 / G3.2** 界面小改、审批卡对齐与中英文文案 | `dalton/main` | 🟦 代码完成 | `PublishApprovalCard.tsx` 前后端英文键名对齐、展示四类核验计数与 `uncertain_items` 逐条交 PM 裁决列表；`pnpm --dir web typecheck` 0 error，前端全量 `676/676` 测试文件（`7224/7224` 测试）全部通过 |

---

## 2. G3.4 迁移脚本编造数值清理对照表

| 位置 (`scripts/dalton/migrate_legacy_knowledge.py`) | 原先硬编码值 | 清理后做法 |
|---|---|---|
| `build_drivers_doc` (`decomposition.eps_growth_pct` / `multiple_rerating_pct`) | `65` / `35` | 设为 `None` (`null`)，备注 `"迁移时未计算，待首次研究补充"` |
| `build_management_doc` (`guidance_ledger[].date`) | `f"{current_year}-07-29"` | 仅从真实源抽取日期；缺失时设为 `None` (`null`) |
| `build_agenda_doc` (`budget.daily_usd_cap` / `per_run_max_usd`) | `15.0` / `2.5` | 设为 `None` (`null`)，统一交由平台预算设置控制 |
| `build_agenda_doc` (`watch.events[].date` / `next_validations[].target_date`) | `f"{current_year}-11-15"` | 设为 `None` (`null`)，待首次研究或财报日历探针填入 |

---

## 3. 本轮明确删掉或不做的内容（`P0G §9`）

1. **删除价格探针与一致预期探针**：已从仓库删除 `src/dalton/autonomy/probes/price.py` 和 `src/dalton/autonomy/probes/consensus.py`；股价异动改用 LangAlpha 自带 `price` 触发器，一致预期对比放入每晚 `thesis-delta` 任务。
2. **删除发布时生成 PDF 的承诺**：已在 `src/dalton/publish/files.py` 中移除断裂的 PDF 导出逻辑，正式发布归档 `.md` 与 `.xlsx`。
3. **不做 Broker 备用模型自动切换**：模型切换统一在 LangAlpha 侧处理，不在 broker 中维护跨模型切换链。
4. **不做 Broker 排队等待**：超出并发上限立即返回 `429`，由调用方处理。

---

## 4. 【待验证】项实测记录

1. **宿主机 `uv` 安装状态（P0.1）**：实测宿主机初始无 `uv`，已通过 `brew install uv` 安装 `uv 0.13.1`（`/opt/homebrew/bin/uv`）。
2. **本机内存与网关状态（P0.1）**：OpenClaw 网关（`127.0.0.1:18789`）、Researchdata 宿主机桥（`127.0.0.1:8960`）和 AlphaEngine MCP（`127.0.0.1:8950/health` 返回 `status: ready`）均正常运行，`$CORE_DB`（6.2 GB）在外置卷 `/Volumes/EveSSD` 上已挂载可读。
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

## 5. 测试与挂钩检查汇总

- **`./scripts/dalton/check_hooks.sh --strict --audit`**：`OK: DALTON-HOOK check passed.`（`NEW_IN_UPSTREAM_DIR: 0`，`UNHOOKED_EDIT: 0`；上游 `models.json`、`providers.json`、`extension/__init__.py`、`oauth.py`、`test_manifest_integrity.py`、`Dashboard/utils/api.ts`、`agent_config.yaml`、`config.yaml` 相对基线 `8e0dce94` 零改动，见 `var/dalton/hook_audit_after.txt`）。
- **Skill 引用检查 (`./scripts/dalton/check_skill_refs.py`)**：`Scanned 22 skills: ghost_hits=0, broken_refs=0, header_notes=0`。
- **Broker v2 Node 单测**：`13 pass, 0 fail`。
- **Python 后端单测**：`tests/unit/dalton/`、`tests/unit/llms/`、`tests/unit/middleware/skills/`、`tests/unit/mcp_servers/` 共 **1,674 passed, 0 failed**。
- **Web 前端类型检查与全量单测**：`pnpm --dir web typecheck` **0 errors**；`vitest` **676/676 个测试文件（7,224/7,224 项测试）全部通过**。
