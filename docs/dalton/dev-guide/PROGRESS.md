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
| **P0.3 / G5.1** broker v2 完整实现与落盘防重 | `fix/P0H.H2-H6-realrun-evidence` | 🟨 真实运行验证 | `integrations/openclaw/dalton-langalpha-broker/`（13/13 Node 单测通过）：① `openclaw_smoke.py --target broker --checks B1,B2,B8` 子集 13/13 pass（见 `var/dalton/smoke/openclaw_smoke_broker_20261018T172300Z.json`）；② 2026-10-18 真实复演长调用中途重启网关同 ID 重试返回 `409 idempotency_indeterminate`（零二次计费）及客户端中途断开后同 ID 重试返回 `200` 完整落盘结果（`replayed=true`，花费 `$0.00051`，见 `var/dalton/smoke/g5_broker_disconnect_and_restart_20261018.json`） |

### WP-B: 平台接入、原生模型与本地预算 (`P0.1`, `P0.4` – `P0.9`, `P0N`, `G0`, `G1`, `G2`, `G5.2`, `H4`, `H5`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0.1** 本地环境与仓库卫生 | `feat/P0.1-platform-env` | 🟨 真实运行验证 | `7641d792` (merge `c81151b3`)：`docker-compose.dalton.yml`、`agent_config.dalton.yaml`、`docker_entrypoint_dalton.sh`、`check_hooks.sh`、`src/dalton/settings.py`、`src/dalton/dalton.yaml`（见 `var/dalton/hook_audit_after.txt`） |
| **P0.4** LangAlpha 接入 broker v2 | `feat/P0.4-8-llm-overlay-and-budget` | 🟨 真实运行验证 | `7061b70e` (merge `0a7f5b99`)：`openclaw_catalog.py`、`overlay.py`、`broker_http.py`、`sync_openclaw_models.py`（实测同步 27 个模型，0 凭据落盘，见 `var/dalton/catalog_openclaw.json`）+ `llm.py`/`api_keys.py` 挂钩 |
| **P0.5** 冒烟测试 `openclaw_smoke.py` | `dalton/main` | 🟨 真实运行验证 | 按每次运行分别记录：① `var/dalton/smoke/openclaw_smoke_broker_20261018T142137Z.json`（覆盖 `B0`–`B8`、`B10`–`B14` 共 14 项：`13 pass, 1 warn (B8), 0 fail`）；② 修复 `B8` `dalton_meta.reasoning_effort` 后运行 `--checks B1,B2,B8` 子集 `var/dalton/smoke/openclaw_smoke_broker_20261018T172300Z.json`（共 13 项：`13 pass, 0 warn, 0 fail`，仅覆盖 `B1`、`B2`、`B8`） |
| **P0.6 / H4** 角色模型配置与切换 | `fix/P0H.H4-providers-roles` | 🟨 真实运行验证 | `588dfc95`：`role_policy.py` 校验全部 10 个角色（含 `assist`、`background`、`compaction`、`fetch`），禁止 `*-contributor-oauth` 进入任何角色且 `assist` 必须非 `text_only`；2026-10-18 实测把 `assist` 改配为 `claude-sonnet-4-6-contributor-oauth` 时启动校验立即报错阻断（见 `var/dalton/smoke/g2_api_and_probes_20261018.json`） |
| **P0.7 / P0N.8 / H4** 设置页同步按钮与模型供应商状态卡 | `fix/P0H.H4-providers-roles` | 🟨 真实运行验证 | `588dfc95`：修复 `provider_status.py` `resolve_model_client` 导入（P1）、探测经 `dalton_complete` 记入预算（P2）、自带 key 嵌套字典读取（P4）；2026-10-18 对真后端触发 `openclaw-broker`/`xai`/`deepseek` 测试连接成功并计入 `GET /api/v1/dalton/budget/today`（花费 `$0.00051`，见 `var/dalton/smoke/g2_api_and_probes_20261018.json`） |
| **P0.8 / G1 / H5** 本地日美元预算 `LocalBudgetPort` 与 `d007` 账本 | `fix/P0H.H5-budget-journal` | 🟨 真实运行验证 | `1e4f3851`：修复 `B1`（`prune_expired_journal_responses` 30 天清理响应正文保留元数据）、`B2`（现金汇总异常 fail-closed 返 503）、`B3`（一次性调用按 `workspace_id`/`thread_id` 强制检查 task cap）；2026-10-18 完成 G1 三连真实验证：① `$1` 日额度下 OAuth 主模型 + 现金子智能体 (`grok-4.7`) 花超上限被拒；② BYOK `xai` 对话花费计入预算卡片；③ 发布审稿首次计费 (`$0.001476`)、二次同草稿命中缓存 (`replayed=true, cash_usd=$0.0`)、额度耗尽时拒绝调用（见 `var/dalton/smoke/g1_budget_realrun_20261018.json`） |
| **P0N.0 – P0N.5 / G5.2** 原生模型迁入 `src/dalton/` 与 Antigravity/Muse 修复 | `dalton/main` | 🟦 代码完成 | 还原上游 `models.json`/`providers.json`/`extension/__init__.py`/`oauth.py`/`test_manifest_integrity.py`/`Dashboard/utils/api.ts` 至 `8e0dce94` 零 diff；迁入 `src/dalton/llm/native_overlay.json`（23 模型，见 `var/dalton/catalog_native.json`）与 `src/dalton/llm/providers/{antigravity,muse}`；完成 `G5.2` R6-1~R6-13 修复与单测；因本机尚未手动登录 Antigravity OAuth，≥4 轮工具调用与 Docker 粘贴回调登录保持 `🟦` 待人工浏览器授权后补齐 |
| **G2 / H4** 路由接线修复与守门测试 | `fix/P0H.H4-providers-roles` | 🟨 真实运行验证 | `588dfc95`：`test_router_wiring.py` 按 `L33` 全部改用 `create_autospec` 并实际调用底层服务函数（26/26 通过）；2026-10-18 起真后端 `127.0.0.1:8000` 对全部 Dalton REST 接口与供应商探测接口逐一真实调用通过（见 `var/dalton/smoke/g2_api_and_probes_20261018.json`） |
| **P0.9 / P0.10** 原生界面走查与 P0 验收 | `dalton/main` | 🟦 代码完成 | H1–H7 代码与 G1/G2/G3/G4/G5.1/G6/G7 真实运行证据已齐备，待人工浏览器走查签字 |

### WP-C: 数据接通 (`P0-D.0` – `P0-D.8`, `G7.1`, `P0F.4`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **P0-D.1 / G7.1** researchdata 宿主机桥 | `fix/P0H.H2-H6-realrun-evidence` | 🟨 真实运行验证 | `researchdata_bridge.py`、`install_bridge.sh`（`caller_budget_s=625`）：2026-10-18 实测 `check_bridge.sh` 6 项全绿 + 一次 **242.05 秒（>240s）** 长文读取返回 `200 OK` 且首键为 `_sources` + 子进程崩溃（向 `stderr` 输出敏感 canary 并以 exit_code=17 退出）时桥日志仅记录 `SubprocessExitError` 与退出码、0 处 `stderr` 泄露（见 `var/dalton/smoke/g7_bridge_realrun_20261018.json`） |
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
| **P1.4 / G3.4** 旧认知迁移与清除编造数值 | `fix/P0H.H2-H6-realrun-evidence` | 🟨 真实运行验证 | `migrate_legacy_knowledge.py` 清除全部硬编码编造值（见 `var/dalton/migration/migration_report.json`：32/32 Schema 通过，27/27 条争点完整迁移，编造字面量精确 grep 0 命中）；2026-10-18 在活库 Postgres 对 `META`/`AMZN`/`GOOGL`/`MSFT` 执行 `remediate_migrated_workspaces` 追加修正版本（`reason="移除迁移脚本编造的数值"`）并通过真后端 `GET /api/v1/dalton/knowledge/{ws_id}/{doc}/versions` 全量核验（见 `var/dalton/reports/g3_4_live_remediation_20261018.json`） |

### WP-E: 公司知识层与发布治理 (`PK.1` – `PK.13`, `G3.1` – `G3.3`, `H1`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.1 – PK.2** 文件布局与 JSON Schema | `fix/P0H.H1-publish-redesign` | 🟦 代码完成 | `c1e76363`：`paths.py` + 10 份 JSON Schema (draft 2020-12，`additionalProperties: false`，支持迁移待补字段 `null`) |
| **PK.3 – PK.7 / H3 §3.3** 版本表、写入工具、字段建议+LLM确认、读序 | `fix/P0H.H3-autonomy-chain` | 🟨 真实运行验证 | `aa1d1922`：`ops.py` 冻结确定性顶层路径别名表（永不改 `/official`），`difflib.get_close_matches` 仅产出候选建议交由 `assist_json("knowledge.repair_field")` 确认后改写并记 warning；2026-10-18 真实 LLM 修复 `questn_text -> question` 写入 Postgres v19 验证通过（见 `var/dalton/smoke/g4_autonomy_realrun_20261018.json` 场景 7） |
| **PK.8 – PK.13 / G3.1 – G3.3 / H1** 发布核验四步职责重划与代码锚定 | `fix/P0H.H1-publish-redesign` | 🟨 真实运行验证 | `c1e76363`：按 `L28`–`L31` 重构四步流水线（净减 88 行，删除 95 行中文数字正则、整行标签判定与 `consensus_confusion` 硬冲突；必经 `audit.label_figures`；`audit.judge` 去掉 `source_value`/`source_unit` 并由代码逐字锚定引文数字；豁免逐字校验标记）。2026-10-18 真实 LLM 实测：① 口语中文四词全抽出；② 提示词注入被代码锚定拦截；③ **META 真实完整报告 445 个数字误判 0 条（≤2）、未能判断 0%（≤10%）**；④ 跨家族审稿 (`claude-opus-5-5-oauth` -> `ocb-deepseek-deepseek-flash`) 通过（总花费 `$0.01291 <= $2`，见 `var/dalton/reports/g3_real_llm_audit_20261018.json` 与 `docs/dalton/dev-guide/trial-runs/G3-real-report-audit-20261018.md`） |

### WP-F: 分析师 Skill、Workflow 与自动任务 (`PS.0` – `PS.18`, `G6.1`, `G6.2`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PS.0 – PS.3 / G6.2** 脚手架、`dalton-evidence`、财务镜头、公司背景 | `fix/P0H.H2-H6-realrun-evidence` | 🟨 真实运行验证 | 2026-10-18 按 `SKILL.md` 命令对 `BABA`（`--reporting-currency CNY`，实时 `CNYUSD=X=0.149772`）、`0700.HK`（`--reporting-currency CNY`，实时 `HKDUSD=X=0.127437`）、`META`（`USD`）真实跑通全部 4 个镜头脚本并留档 `var/dalton/lenses/{baba,0700hk,meta}_{anchors,netcash,return_score,earnings_quality}_20261018.json`，附与基金原脚本对比表 `docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md` |
| **PS.4 – PS.18 / G6.1** 分析师 Skill、Workflow 与引用比对脚本 | `dalton/main` | 🟦 代码完成 | 清理残留 OpenClaw 旧工具名、`RunWorkflow` 参数名统一为 `params`、重写 `dalton-agenda` 自动任务模板与 `dalton-research-director` 去掉次数门、补齐 `dalton_registry.py` `requires`；`scripts/dalton/check_skill_refs.py` 扫描 22 个 skill：`ghost_hits=0, broken_refs=0` |

### WP-G & WP-H: 自主议程与界面小改 (`PK.14` – `PK.20`, `PK-UI.1` – `PK-UI.6`, `G4`, `H3`)
| 步骤 | 分支 | 状态 | 提交 / 说明 |
|---|---|---|---|
| **PK.14 – PK.20 / G4 / H3** 自主议程 A1–A14 修复与六点真实验证 | `fix/P0H.H3-autonomy-chain` | 🟨 真实运行验证 | `aa1d1922`：修复 A1–A14 全部 7 高 + 7 中问题（预算键名 `spent_usd`/`open_reserved_usd`、`assist_json` 位置参数签名、45s 总超时任务取消、议程真实字段、`dalton_agenda_note` 补 `owner="pm"`、内部库探针改走桥 HTTP、总监空转日 `wake=False`、`once` 任务预算耗尽顺延 +1 天等）。2026-10-18 完成 §3.4 六点 + `dalton_agenda_note` 真实运行验证（7/7 场景全绿，见 `var/dalton/smoke/g4_autonomy_realrun_20261018.json`）：① META 真实 8-K (`0001628280-26-050596`) 唤醒；② 真实 LLM 分诊不相关资料 `skip` + 相关资料 `wake` 并落库理由；③ GOOGL 到期验证项唤醒；④ `knowledge_append_verdict` 闭环信号后看板不再列出；⑤ 总监空转日 `skip` 花费 `$0`；⑥ 额度耗尽跳过并将 `once` 任务顺延 `+1d` (`defer_once_1d`)；⑦ `dalton_agenda_note` 实际写入 Postgres 成功 |
| **PK-UI.1 – PK-UI.5 / G3.2 / H1** 界面小改、审批卡对齐与中英文文案 | `fix/P0H.H1-publish-redesign` | 🟦 代码完成 | `PublishApprovalCard.tsx` 前后端英文键名对齐、展示四类核验计数、`labeling_incomplete` 琥珀色横幅与 `uncertain_items` 逐条交 PM 裁决列表；`pnpm --dir web typecheck` 0 error，前端全量 `676/676` 测试文件（`7224/7224` 测试）全部通过 |

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
