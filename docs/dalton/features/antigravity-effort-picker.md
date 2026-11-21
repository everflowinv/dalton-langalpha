# Antigravity 模型“界面按系列合并 + 思考强度选项”设计文档（第 6 版）

> **文档状态**：第六次修订定稿（仅修改设计文档，不改动代码；已全量落实第 5 版 Codex 审查意见及系列默认思考强度、外部用户引导两项新增决定，可直接拆分为 PR 1 – PR 4 实施）
>
> **已锁定核心决定**：
> 1. **只在界面、配额冷却与周报层按系列合并，底层带强度后缀的模型 ID 保持不变**：数据库、配置文件与上游请求继续使用既有带后缀键（如 `gemini-4-neon-high-oauth`），不做 Antigravity 存量改名迁移，存量锁定记录不报错。
> 2. **每个 Antigravity 系列拥有集中配置的默认思考强度**：暂定 **`Neon = high`、`Hydrogen = high`、`Argon = high`、`Carbon = medium`**（其中 **`Carbon` / `Argon` 待用户确认**；经 Antigravity 的其他系列回退为 `medium`），全仓仅在一处数据中配置，不散落硬编码。选了系列但未选强度时，在**设置、对话、定时任务**中统一使用该系列的默认强度。
> 3. **外部用户引导（Onboarding，PR #435）只按系列推荐与展示，不出现思考强度选择**：推荐与展示合并后的 `Gemini 4 Neon`（默认模型）与 `Gemini 4 Hydrogen`（后台模型），使用系列默认强度，保存到后端 `user_preferences.model_preference` 的值仍为对应带后缀 ID（`gemini-4-neon-high-oauth` / `gemini-4-hydrogen-high-oauth`）。
> 4. **定时任务新增面向所有模型的“思考强度”设置**：新增 Dalton 迁移 `d042`（`automations.reasoning_effort` 可空列），执行器优先于用户偏好传入并在同次执行的续跑、关联唤醒、重试与订阅备用模型中一致继承；存量多档 Opus 任务（含账户默认实际解析为 Opus 的任务）通过受控脚本一次性写入 `'medium'`，写入后用户仍可随时修改或清空。
> 5. **额度冷却与每周用量周报按系列合并**：配额冷却统一进程内与 Redis 归组键（区分 `:series:` 与 `:model:` 键空间）；周报先按原始模型 ID 与 UTC 日期查价并统计本机/生产来源小计，再按稳定系列 ID 合并展示并附档位明细（含无法恢复后缀记录的“档位未知”桶）。

---

## 1. 现状与存量基线

### 1.1 Antigravity 现有目录条目全表（22 行 / 7 个可见系列）

仓库模型目录由上游基础清单 `src/llms/manifest/models.json` 与 Dalton 覆盖层（内置 `src/dalton/llm/native_overlay.json` + 运行时同步文件 `var/dalton/catalog/native_sync_overlay.json`，见 `src/dalton/llm/overlay.py:99-245,342-397`）合并而成。非 Antigravity 上游模型（如 `claude-opus-5-5-oauth` `:1317-1357`、直连 `gemini-4-neon` `:334-362`）均为每个系列一行并通过 `reasoning.efforts` 声明多档强度；而 `provider: "antigravity-oauth"` 因上游 `v1internal:fetchAvailableModels` 按强度后缀拆分模型 ID，在 `src/dalton/llm/native_overlay.json` 中共定义了 **22 行**（21 行可见、1 行隐藏），全部未填 `display_name`，且每行 `reasoning.efforts` 仅含单一档位：

| 序号 | 清单键名（落库与请求使用的模型 ID） | 底层 `model_id` | 系列前缀 | 强度后缀 | 当前 `efforts` | `visible` | `speed` / `intelligence` | 名义计价（`dalton_meta`） | 位置（`native_overlay.json`） |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `gemini-4-argon-low-oauth` | `gemini-4-argon-low` | `gemini-4-argon` | `low` | `["low"]` | `true` | `3` / `5` | `notional_ref: "gemini-4-argon"` | `:4-30`、`:1017-1022` |
| 2 | `gemini-4-argon-medium-oauth` | `gemini-4-argon-medium` | `gemini-4-argon` | `medium` | `["medium"]` | `true` | `3` / `5` | `notional_ref: "gemini-4-argon"` | `:31-57`、`:1023-1028` |
| 3 | `gemini-4-argon-high-oauth` | `gemini-4-argon-high` | `gemini-4-argon` | `high` | `["high"]` | `true` | `2` / `5` | `notional_ref: "gemini-4-argon"` | `:58-84`、`:1029-1034` |
| 4 | `gemini-4-carbon-low-oauth` | `gemini-4-carbon-low` | `gemini-4-carbon` | `low` | `["low"]` | `true` | 未填 | `$2 / $10 / $0.1` | `:85-109`、`:1035-1048` |
| 5 | `gemini-4-carbon-medium-oauth` | `gemini-4-carbon-medium` | `gemini-4-carbon` | `medium` | `["medium"]` | `true` | 未填 | `$2 / $10 / $0.1` | `:110-134`、`:1049-1062` |
| 6 | `gemini-4-carbon-high-oauth` | `gemini-4-carbon-high` | `gemini-4-carbon` | `high` | `["high"]` | `true` | 未填 | `$2 / $10 / $0.1` | `:135-159`、`:1063-1076` |
| 7 | `gemini-4-hydrogen-low-oauth` | `gemini-4-hydrogen-low` | `gemini-4-hydrogen` | `low` | `["low"]` | `true` | 未填 | `$0.75 / $3.75 / $0.0375` | `:160-184`、`:1077-1090` |
| 8 | `gemini-4-hydrogen-medium-oauth` | `gemini-4-hydrogen-medium` | `gemini-4-hydrogen` | `medium` | `["medium"]` | `true` | 未填 | `$0.75 / $3.75 / $0.0375` | `:185-209`、`:1091-1104` |
| 9 | `gemini-4-hydrogen-high-oauth` | `gemini-4-hydrogen-high` | `gemini-4-hydrogen` | `high` | `["high"]` | `true` | 未填 | `$0.75 / $3.75 / $0.0375` | `:210-234`、`:1105-1118` |
| 10 | `gemini-4-hydrogen-xhigh-oauth` | `gemini-4-hydrogen-xhigh` | `gemini-4-hydrogen` | `xhigh` | `["xhigh"]` | `true` | 未填 | `$0.75 / $3.75 / $0.0375` | `:235-259`、`:1119-1132` |
| 11 | `gemini-4-neon-low-oauth` | `gemini-4-neon-low` | `gemini-4-neon` | `low` | `["low"]` | `true` | 未填 | `$3 / $15 / $0.15` | `:260-284`、`:1133-1146` |
| 12 | `gemini-4-neon-medium-oauth` | `gemini-4-neon-medium` | `gemini-4-neon` | `medium` | `["medium"]` | `true` | 未填 | `$3 / $15 / $0.15` | `:285-309`、`:1147-1160` |
| 13 | `gemini-4-neon-high-oauth` | `gemini-4-neon-high` | `gemini-4-neon` | `high` | `["high"]` | `true` | 未填 | `$3 / $15 / $0.15` | `:310-334`、`:1161-1174` |
| 14 | `gemini-4-neon-xhigh-oauth` | `gemini-4-neon-xhigh` | `gemini-4-neon` | `xhigh` | `["xhigh"]` | `true` | 未填 | `$3 / $15 / $0.15` | `:845-869`、`:1555-1568` |
| 15 | `gemini-2.5-pro-oauth` | `gemini-2.5-pro` | `gemini-2.5-pro` | 无后缀 | `["medium"]` | `false` | `2` / `4` | `notional_ref: "gemini-3.1-pro-preview"` | `:335-360`、`:1175-1181` |
| 16 | `claude-opus-5-5-low-oauth` | `claude-opus-5-5-low` | `claude-opus-5-5` | `low` | `["low"]` | `true` | `3` / `5` | `notional_ref: "claude-opus-5-5"` | `:361-384`、`:1182-1187` |
| 17 | `claude-opus-5-5-medium-oauth` | `claude-opus-5-5-medium` | `claude-opus-5-5` | `medium` | `["medium"]` | `true` | `3` / `5` | `notional_ref: "claude-opus-5-5"` | `:385-408`、`:1188-1193` |
| 18 | `claude-opus-5-5-high-oauth` | `claude-opus-5-5-high` | `claude-opus-5-5` | `high` | `["high"]` | `true` | `2` / `5` | `notional_ref: "claude-opus-5-5"` | `:409-432`、`:1194-1199` |
| 19 | `claude-sonnet-5-5-low-oauth` | `claude-sonnet-5-5-low` | `claude-sonnet-5-5` | `low` | `["low"]` | `true` | `4` / `4` | `notional_ref: "claude-sonnet-5-5"` | `:433-456`、`:1200-1205` |
| 20 | `claude-sonnet-5-5-medium-oauth` | `claude-sonnet-5-5-medium` | `claude-sonnet-5-5` | `medium` | `["medium"]` | `true` | `4` / `4` | `notional_ref: "claude-sonnet-5-5"` | `:457-480`、`:1206-1211` |
| 21 | `claude-sonnet-5-5-high-oauth` | `claude-sonnet-5-5-high` | `claude-sonnet-5-5` | `high` | `["high"]` | `true` | `3` / `4` | `notional_ref: "claude-sonnet-5-5"` | `:481-504`、`:1212-1217` |
| 22 | `gpt-oss-120b-medium-oauth` | `gpt-oss-120b-medium` | `gpt-oss-120b` | `medium` | `["medium"]` | `true` | `4` / `3` | `notional_ref: "gpt-oss-120b-groq"` | `:505-526`、`:1218-1223` |

直接平铺这 21 行可见模型会导致：`Google Antigravity` 在聊天模型面板、设置页和定时任务下拉框中列出 21 行；且选中某行后，虽然 `hasTuningMenu`（`web/src/lib/modelTuning.ts:44-46`，只要有至少 1 个可展示档位即返回 `true`）会渲染思考强度胶囊，但点开菜单仅包含该条目自身的单一档位，无法在思考强度菜单内切档，必须在模型列表中重新找另一行带后缀模型。

### 1.2 底层请求构造与零改动边界

由于底层模型名保持不变，以下模块**完全保持现状零改动**（注意：修改非 `scripts/dalton/dalton_paths.txt` 白名单内的上游文件时须附带已登记的 `DALTON-HOOK` 注释并通过 `scripts/dalton/check_hooks.sh`）：
- **对话强度解析与单档裁剪**：`thread_resolver`（`src/server/services/thread_agent.py:57-58`）与 `resolve_turn_tuning`（调用处在 `src/server/services/llm/clients.py:1143-1146`，定义在 `src/llms/preferences.py:230-249`）解析出请求强度后，`LLM.__init__`（`src/llms/llm.py:702-728`）通过 `clamp_reasoning_effort`（`src/llms/model_spec.py:115-135`）将任意传入值裁剪到单条目 `efforts`（如 `gemini-4-neon-high-oauth` 恒为 `"high"`）。
- **上游报文与令牌刷新重建**：`LLM.get_llm`（`src/llms/llm.py:894-954`）与 `ChatAntigravity._build_payload`（`src/dalton/llm/providers/antigravity/client.py:600-643`）发送原带后缀 `model` 与大写 `thinkingLevel`（`:180-200`）；HTTP 401 时 `rebuild_oauth_client`（定义在 `src/dalton/llm/oauth_refresh.py:124-148`，内部调用 `_resolve` `:90-100`）按原 `OAuthRebuildSpec.model_name` 重建客户端。
- **静态配置与后台设施**：`native_overlay.json`、`src/dalton/dalton.yaml`、`agent_config.dalton.yaml`、播客任务 `T60`（`src/dalton/data_jobs/manifest.yaml:909-923`，`--video-model gemini-4-neon` 走直连、`--text-model gemini-4-hydrogen-high-oauth` 走订阅）、公司百科本机 `agy` 调用、推理血统 `reasoning_lineage.py`、名义计价 `notional_pricing.py`、幂等账本 `dalton_llm_journal`（`src/dalton/llm/complete.py`）、组织策略 `Policy.allows_model`、原生目录同步 `native_catalog.py` 与可用性巡检 `served_models()` 均无需改动。

### 1.3 定时任务强度现状与生产存量基线

1. **定时任务缺乏独立思考强度设置**：`automations` 表仅有 `llm_model` 列，无 `reasoning_effort` 列；`AutomationExecutor._run_turn`（`src/server/services/automation_executor.py:978-989`）发出的 `ChatRequest` 不带 `reasoning_effort`，续跑轮（`src/dalton/services/automation_thread_model.py:318-495`）也只继承 `llm_model`。非 Antigravity 后缀锁档的定时任务（如 Claude Opus 或账户默认模型）实际强度完全随所属用户在设置页的聊天偏好浮动。
2. **生产存量只读统计基线**（统计时间：`2026-11-21`，生产 PostgreSQL 库只读查询快照；离线代码仓不含生产库数据，运维脚本执行前以实时只读预演结果为准）：
   - **启用中定时任务分布（共 177 个）**——查询语句：`SELECT COALESCE(llm_model, '<NULL>') AS model, COUNT(*) FROM automations WHERE deleted_at IS NULL AND status = 'active' GROUP BY 1 ORDER BY 2 DESC;`
     - `llm_model IS NULL`（账户默认模型）：**145 个**；
     - `'claude-opus-5-5-oauth'`：**5 个**；
     - 显式 Antigravity 带后缀模型：**27 个**（`gemini-4-neon-high-oauth` 23、`gemini-4-carbon-high-oauth` 3、`gemini-4-neon-medium-oauth` 1，后缀已锁档，不迁移）。
   - **锁定在 Antigravity 带后缀模型上的存量未归档对话（共 28 个）**——查询语句：`SELECT llm_model, COUNT(*) FROM conversation_threads WHERE archived_at IS NULL AND llm_model ~ '-(low|medium|high|xhigh)-oauth$' GROUP BY 1 ORDER BY 2 DESC;`（`neon-high` 12、`neon-medium` 9、`carbon-medium` 3、`carbon-high` 2、`hydrogen-high` 1、`hydrogen-medium` 1，上线后无需改库直接回显为对应“系列 + 强度”）。

---

## 2. Antigravity 界面按系列合并、系列默认强度与工具适配

### 2.1 单一数据源与各系列默认思考强度配置

新增零第三方依赖的纯标准库模块 `src/dalton/llm/antigravity_series.py`，直接从合并后的模型目录自动推导系列分组，并在该模块集中定义各系列的默认思考强度，供 `/api/v1/models`、`switch_thread_model`、定时任务校验、配额冷却与周报统计统一调用。

#### 1. 各系列默认思考强度集中配置（单一配置点）

在 `src/dalton/llm/antigravity_series.py` 中集中定义默认强度映射表 `ANTIGRAVITY_SERIES_DEFAULT_EFFORTS`（支持在 `src/dalton/dalton.yaml` 新增可选键 `llm.antigravity_series_default_efforts` 覆盖同名 `series_prefix`，代码中绝不在前端或其他服务重复硬编码）：

```python
# 单一默认强度数据源（全仓唯一配置处，经 /api/v1/models 下发至前端）
# 注：Carbon 与 Argon 的默认档位待用户最终确认，实施前若调整只需改此一处字典（或 dalton.yaml 覆盖项）
ANTIGRAVITY_SERIES_DEFAULT_EFFORTS: dict[str, str] = {
    "gemini-4-neon": "high",
    "gemini-4-hydrogen": "high",
    "gemini-4-argon": "high",      # 待用户确认（Carbon/Argon 待用户确认）
    "gemini-4-carbon": "medium",   # 待用户确认（Carbon/Argon 待用户确认）
}
FALLBACK_SERIES_DEFAULT_EFFORT = "medium"
```

- **默认档位解析函数 `resolve_series_default_effort(series_prefix: str, efforts: Sequence[str]) -> str`**：
  1. 先查 `dalton.yaml` 覆盖项与 `ANTIGRAVITY_SERIES_DEFAULT_EFFORTS.get(series_prefix, FALLBACK_SERIES_DEFAULT_EFFORT)` 得到目标默认档 `target`；
  2. 若 `target in efforts`，直接返回 `target`；
  3. 若因目录增删或组织策略过滤导致 `target` 不在当前 `efforts` 中，调用与 `clamp_reasoning_effort`（`src/llms/model_spec.py:115-135`）完全一致的阶梯向下裁剪规则（低于下限时取最低可用档；`efforts` 非空时必返回合法档位）。

#### 2. 目录自动拆解与聚合规则

- **渠道与系统模型身份前置判定（防止误伤同名自定义模型）**：
  - 后端调用方凡涉及用户上下文（对话、定时任务、冷却），均先通过 `user_models.classify_model(user_id, model_name, _pref_cache=pref)`（`src/server/services/llm/user_models.py:259-288`）确认 `source == ModelSource.SYSTEM` 且目录条目 `config.get("provider") == "antigravity-oauth"`；若用户 `custom_models` 中存在同名自定义模型（`classify_model` 返回 `ModelSource.CUSTOM`），一律按用户自定义模型处理，绝不归入 Antigravity 系列。
- **后缀拆解（`parse_antigravity_series(key: str)`）**：
  - 校验 `key.endswith("-oauth")`，剥去 `"-oauth"` 得到 `stem`；
  - 对照上游标准档位元组 `REASONING_LEVELS`（`src/llms/reasoning.py:25-27`：`("none", "minimal", "low", "medium", "high", "xhigh", "max")`），若 `stem` 以 `f"-{effort}"` 结尾，拆出 `(series_prefix, effort)`；无后缀条目（如隐藏的 `gemini-2.5-pro-oauth`）拆出 `(stem, None)`。
- **目录聚合（`build_antigravity_series_catalog(model_config)`）**：
  - 遍历 `ModelConfig.llm_config` 中 `provider == "antigravity-oauth"` 且 `visible is not False` 的系统内置条目（`visible: false` 的 `gemini-2.5-pro-oauth` 及 `retired` 条目自动排除；未来 `native_catalog.py:515-518` 同步进 `native_sync_overlay.json` 的新档位或新系列自动吸纳）；
  - 同一 `series_prefix` 下按 `REASONING_LEVELS` 顺序生成 `variants: dict[effort, model_key]`、`efforts: list[str]`、`default_effort: str`、`default_model: str`（即 `variants[default_effort]`）、组内展示名 `display_name` 与跨渠道消歧名 `qualified_display_name`。

当前 `native_overlay.json` 自动生成的 **7 个可见系列**如下：

| 系列标识（`series_id`，仅用于分组，不落库） | 组内展示名（`display_name`） | 跨渠道消歧展示名 | 目录可选强度（`efforts`） | 系列默认强度（`default_effort`） | 未选强度时默认写入的模型 ID（`default_model`） |
|---|---|---|---|---|---|
| `antigravity-oauth:gemini-4-neon` | `Gemini 4 Neon` | `Gemini 4 Neon (Antigravity)` | `["low", "medium", "high", "xhigh"]` | **`"high"`** | `gemini-4-neon-high-oauth` |
| `antigravity-oauth:gemini-4-hydrogen` | `Gemini 4 Hydrogen` | `Gemini 4 Hydrogen (Antigravity)` | `["low", "medium", "high", "xhigh"]` | **`"high"`** | `gemini-4-hydrogen-high-oauth` |
| `antigravity-oauth:gemini-4-argon` | `Gemini 4 Argon` | `Gemini 4 Argon (Antigravity)` | `["low", "medium", "high"]` | **`"high"`** *(待用户确认)* | `gemini-4-argon-high-oauth` |
| `antigravity-oauth:gemini-4-carbon` | `Gemini 4 Carbon` | `Gemini 4 Carbon (Antigravity)` | `["low", "medium", "high"]` | **`"medium"`** *(待用户确认)* | `gemini-4-carbon-medium-oauth` |
| `antigravity-oauth:claude-opus-5-5` | `Claude Opus 5.5` | `Claude Opus 5.5 (Antigravity)` | `["low", "medium", "high"]` | `"medium"` | `claude-opus-5-5-medium-oauth` |
| `antigravity-oauth:claude-sonnet-5-5` | `Claude Sonnet 5.5` | `Claude Sonnet 5.5 (Antigravity)` | `["low", "medium", "high"]` | `"medium"` | `claude-sonnet-5-5-medium-oauth` |
| `antigravity-oauth:gpt-oss-120b` | `GPT-OSS 120B` | `GPT-OSS 120B (Antigravity)` | `["medium"]`（单档） | `"medium"` | `gpt-oss-120b-medium-oauth` |

#### 3. 公开目录接口与前端类型契约

- `GET /api/v1/models`（`src/server/app/api_keys.py:666-753`）：保留 `models` 与 `model_metadata` 全部带后缀键不变，新增顶层字段 `antigravity_series: list[AntigravitySeriesEntry]`（含 `series_id`、`series_prefix`、`provider`、`display_name`、`qualified_display_name`、`default_effort`、`default_model`、`efforts`、`variants`）。
- `web/src/api/model.ts:12-25`：在 `ModelsResponse` 接口新增可选属性 `antigravity_series?: AntigravitySeriesEntry[]`（放入 PR 1，供后续所有前端 PR 共享）。

### 2.2 前端公用层与各界面入口改动清单

新增 `web/src/lib/antigravitySeries.ts`，并在 `web/src/hooks/useAllModels.ts:240-250,371-409` 中输出**两套职责严格分离的索引**（解决旧档位暂时不可选时的回显丢失及同名自定义模型遮蔽问题）：

1. **已存模型显示反查索引 `seriesCatalog`（用于回显，不依赖当前可选过滤）**：
   - 基于原始 `modelsData.antigravity_series` 与原始系统 `modelsData.model_metadata` 构建，但**前置排除用户自定义模型**：调用 `inspectSavedAntigravityModel(modelKey, { seriesCatalog, rawMetadata, customModels })` 时，若 `customModels` 存在 `cm.name === modelKey`（即该键被用户自定义模型遮蔽，见 `web/src/hooks/useFilteredModels.ts:417-446,529-544`）或 `rawMetadata[modelKey]?.provider !== 'antigravity-oauth'`，直接返回 `null`（按自定义模型或其他渠道模型显示）；
   - 若确认为内置 `antigravity-oauth` 模型，即使该档位当前因组织策略收紧、套餐变化、订阅待重连（`needs_reauth`）或 `missingByProvider` 暂时不在可选列表中，仍准确返回 `{ series, effort, displayName }`，使旧对话、旧定时任务、旧设置和旧子智能体稳定回显为“系列名 + 已存强度”（配合 `effortOptions(offeredEfforts, storedEffort)` `web/src/lib/modelTuning.ts:68-70` 与 `ModelSelector` `missingLabel` 保留展示当前已存档位，不退回裸英文 ID、也不静默篡改）。
2. **当前可选系列索引 `selectableSeries`（用于选择器渲染与切入系列）**：
   - 对给定模型池（对话用 `offered` `:371-374`，后台/定时任务用 `offeredSlots` `:375-378`）过滤掉被 `customPairs` / `shadowOwner`（`useFilteredModels.ts:500-544`）遮蔽的键后，与每个系列的 `variants` 求交集；
   - 系列在当前池中的默认档位取 `resolveEffort(series.default_effort, availableEfforts)`（即优先取服务端配置的系列默认强度，若该档被策略禁用则向下裁剪到最高可用档）；无任何可用档位的系列整体隐藏。
3. **选系列未选强度时的统一规则**：
   - 无论在**设置页（含子智能体侧栏）、对话输入框模型面板、对话内 `switch_thread_model` 工具，还是定时任务编辑页**，当用户从其他模型/其他系列选中某个 Antigravity 系列而未显式指定思考强度时，统一取该系列的默认强度 `series.default_effort` 对应的带后缀模型键（如选 `Gemini 4 Neon` → `gemini-4-neon-high-oauth`，选 `Gemini 4 Carbon` → `gemini-4-carbon-medium-oauth`）。

各界面入口具体改动落点：

- **聊天输入框与对话模型/强度钩子**：
  - `web/src/components/ui/chat-input.modelPicker.tsx:125-230`、`chat-input.modelMenu.tsx:160-370`：`Google Antigravity` 分组、搜索结果、收藏（Favorites）与最近使用（Recent）将同一 `series_id` 折叠为 1 行；当前 `selectedModel` 命中该系列任一后缀时显示勾选态；从其他系列点击该系列行时传入该系列默认强度对应的 `defaultVariantKey`（若已在同系列内点击同一系列行则保持当前后缀键不变）。左侧胶囊显示 `series.display_name`；右侧 `ChatInputEffortMenu` 在系列可选或已存档位数 `> 1` 时列出该系列档位，胶囊显示当前后缀对应档位，“默认”徽标标在该系列的 `default_effort` 上（单档 `GPT-OSS 120B` 仅单档 `medium`，不提供多档切换）。
  - **区分个人工作区对话、共享工作区对话与未创建新对话**（`web/src/pages/ChatAgent/hooks/useThreadModel.ts:180-252,377-383`、`useThreadTuning.ts:51-165`、`web/src/pages/ChatAgent/components/ChatView.tsx:435-445,524-528,2114-2122`、`web/src/components/ui/chat-input.tsx:411-473`）：
    - 后端 `PATCH /api/v1/threads/{thread_id}`（`src/server/app/threads/crud.py:700-711`）对共享工作区（`access.is_shared == True`，前端由 `useSharedEntry(workspaceId)` `web/src/hooks/useSharedWorkspaces.ts:42-47` 判定）拒绝修改 `llm_model` 与 `reasoning_effort` 并返回 `HTTP 403`（`shared_thread_setting`）。
    - **个人工作区已有对话（`sharedEntry === null` 且 `liveThreadId` 非空）**：选系列或在强度菜单切档时，发**单次 `PATCH /api/v1/threads/{id}`** 提交 `{ llm_model: targetVariantKey, ...(thread?.reasoning_effort != null ? { reasoning_effort: null } : {}) }`（`crud.py:727-755` 单次 `update_thread_fields` 原子更新两列；同系列内切档不重复弹出设为账户默认模型横幅）。
    - **共享工作区对话（`sharedEntry !== null`，含加载态 `sharedEntry === undefined`）与未创建新对话（`!liveThreadId`）**：**不调用 `PATCH /api/v1/threads/{id}`**，仅更新当前编辑器本地状态（`model = targetVariantKey`，本地 `reasoningEffort = null`），在发送消息时随 `POST /api/v1/threads/{id}/messages` 请求体携带 `llm_model: targetVariantKey` 按消息生效。
- **设置页与子智能体侧栏**：
  - 单模型槽位（`web/src/components/model/ModelSelector.tsx:50-195`、`ModelTierConfig.tsx:158-190`、`web/src/pages/Settings/panels/ModelsPanel.tsx:372-788`、`web/src/pages/Plugins/components/subagents/SubagentAside.tsx:74-165`）：`ModelSelector` 对 Antigravity 系列折叠渲染 1 项并补齐设置页各调用点的 `missingLabel`；选中某系列时初始取该系列 `default_effort` 对应的后缀键，并在旁侧渲染该系列的“思考强度”下拉框（`SubagentAside` 中当固定模型为 Antigravity 系列时，`EffortBlock` 直接读写同系列对应后缀 `model` 并将独立 `effort` 置 `null`）。
  - 多选列表与按模型微调矩阵（`web/src/components/model/FallbackModelsPicker.tsx:15-241`、`PerModelMatrix.tsx:50-260`）：添加面板按系列折叠，默认选中该系列 `default_effort` 并支持点选具体档位添加对应带后缀 ID；已选芯片与矩阵行名通过 `seriesCatalog` 显示为 `系列名 · 档位`（如 `Gemini 4 Neon · High`）。

### 2.3 外部用户引导（Onboarding）按系列推荐与无强度选择设计

外部新用户引导的推荐模型入口位于 PR #435 引入的 `web/src/config/daltonSetup.ts`（合入前定义为 `DALTON_DEFAULT_MODEL = 'gemini-4-neon-high-oauth'`、`DALTON_BACKGROUND_MODEL = 'gemini-4-hydrogen-high-oauth'`；注：当前分支快照尚未包含 PR #435，在 PR 4 基于含 PR #435 的主干实施时同步修改）及 `web/src/pages/Setup/answers/defaultChoices.ts:99-152`、`web/src/pages/Setup/steps/DefaultStep.tsx:45-430`（在 `credentials.sharing == "team"` 开启时，外部成员共享团队 OAuth 凭据，见 `src/dalton/credentials.py:35-65`）。

具体改动清单：
1. **推荐常量按系列声明**（`web/src/config/daltonSetup.ts`）：
   - 将推荐常量改为系列标识 `DALTON_DEFAULT_SERIES = 'antigravity-oauth:gemini-4-neon'`、`DALTON_BACKGROUND_SERIES = 'antigravity-oauth:gemini-4-hydrogen'`，由辅助函数通过 `antigravity_series` 取该系列的 `default_model`（当系列默认强度为 `Neon=high`、`Hydrogen=high` 时，解析结果即为 `'gemini-4-neon-high-oauth'` 与 `'gemini-4-hydrogen-high-oauth'`）。
2. **候选列表按系列合并去重与存量兼容**（`web/src/pages/Setup/answers/defaultChoices.ts:99-152`）：
   - 在构建候选池 `pool`（`:131-142`）时，将可用的内置 `antigravity-oauth` 模型按系列折叠，每个系列仅保留 1 个代表键（即该系列在可用集合中的默认强度键 `defaultVariantKey`，避免 5 张候选卡被同系列多档占满）；
   - 存量兼容（`:146-148`）：若用户 `saved.preferred_model` 已存有同系列任意带后缀键（如 `'gemini-4-neon-medium-oauth'`），识别其属于该系列并保留该已存键作为 `initial`，不报错、不丢失。
3. **引导页纯系列展示、不出现思考强度选择、落库值明确**（`web/src/pages/Setup/steps/DefaultStep.tsx:188-420`）：
   - 名称格式化函数 `name(m)`（`:188`）对内置 Antigravity 模型优先返回 `series.display_name`：因此“推荐配置”区域（`:293-339`）与候选单选卡片（`:341-355`）仅显示 **`Gemini 4 Neon`**（默认模型）与 **`Gemini 4 Hydrogen`**（后台模型），不再显示 `"Gemini 4 Neon High"`；
   - **引导页全程不渲染思考强度选择器**（包括推荐区、单选卡片及展开“自定义”面板 `:369-418` 内的模型下拉框，传入 `hideEffortPicker` 仅按系列选择并自动取系列默认强度）；
   - **保存到后端的值**（`DefaultStep.tsx:214-221`）：点击“继续”调用 `update.mutateAsync(splitPreferenceWrite(...))` 时，写入后端 `user_preferences.model_preference` 的精确字段值为：
     - `preferred_model`: **`"gemini-4-neon-high-oauth"`**（Neon 系列默认强度 `high` 对应的带后缀 ID）；
     - `preferred_background_model`: **`"gemini-4-hydrogen-high-oauth"`**（Hydrogen 系列默认强度 `high` 对应的带后缀 ID）；
     - `background_follows`: **`null`**。
   - 引导完成后，用户进入对话或设置页仍可随时查看并修改思考强度。

### 2.4 对话内切换模型工具 `switch_thread_model`

修改 `src/dalton/tools/thread_model_tools.py:116-189,234-382` 与 `src/server/services/llm/thread_model.py:218-344,535-624`：

1. **候选展示与同名自定义模型隔离**（`thread_model_tools.py:182-253`）：
   - 仅对 `key not in custom_model_names(pref)` 且 `mc.get_model_config(key).get("provider") == "antigravity-oauth"` 的启用条目按系列折叠为 `Gemini 4 Neon [可选强度: low/medium/high/xhigh, 默认: high]`、`Claude Opus 5.5 (Antigravity) [可选强度: low/medium/high, 默认: medium]`、`GPT-OSS 120B (Antigravity)`；用户同名自定义模型原样单独保留。
2. **新增可选参数 `reasoning_effort: Optional[str] = None`**（`thread_model_tools.py:255-378`）：
   - **传入 Antigravity 系列名**（如 `model="Gemini 4 Neon"`）：若传了 `reasoning_effort` 则校验属于该系列可用 `efforts` 并映射到对应带后缀键（不存在则报错列出可用档位）；**若未传 `reasoning_effort`，统一使用该系列的默认强度 `series.default_effort`**（如 `Gemini 4 Neon` → `"gemini-4-neon-high-oauth"`，`Gemini 4 Carbon` → `"gemini-4-carbon-medium-oauth"`）；若裸传 `"Claude Opus 5.5"` 同时命中多个可用渠道（如 `claude-oauth` 与 `antigravity-oauth`），返回消歧错误列出精确名称；
   - **直接传入带后缀 ID**（如 `model="gemini-4-neon-medium-oauth"`）：原样兼容；若同时传了另一合法 `reasoning_effort="high"`，切换到同系列 `"gemini-4-neon-high-oauth"`；
   - **原子写入与实时通知**（`src/server/services/llm/thread_model.py:218-298,535-624`）：扩展 `prepare_thread_model_switch` 与 `switch_thread_model` 接收可选 `reasoning_effort` 更新标志——切往内置 Antigravity 带后缀模型时在同一 `threads_write.update_thread_fields(...)` 调用中同步设 `reasoning_effort=None`；对非 Antigravity 多档模型支持单次原子更新 `(llm_model, reasoning_effort)`（支持同模型仅改强度不短路跳过），写库后若 `reasoning_effort` 发生变化则调用 `publish_thread_tuning`（对齐 `src/server/app/threads/crud.py:786-793`）。共享工作区对话仍由 `prepare_thread_model_switch`（`:245-256`）拒绝持久化并返回按消息选择提示。

---

## 3. 定时任务“思考强度”设置（适用于所有模型）

### 3.1 数据库迁移 `d042` 与 Antigravity 任务列值规则

1. **迁移文件**：新增 `src/dalton/migrations/versions/d042_automation_reasoning_effort.py`（`down_revision = "d041"`），为 `automations` 表新增可空列及 7 档合法值约束（严格对齐 `src/llms/reasoning.py:25` 的 `ReasoningLevel`）：
   ```sql
   ALTER TABLE automations
     ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)
     CONSTRAINT ck_automations_reasoning_effort
     CHECK (
       reasoning_effort IS NULL
       OR reasoning_effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
     );
   ```
   `downgrade()` 执行 `ALTER TABLE automations DROP COLUMN IF EXISTS reasoning_effort;`。
2. **Antigravity 带后缀任务的列值决策：保持 `automations.reasoning_effort = NULL`**（不冗余双写）：
   - **理由**：(a) Antigravity 模型的强度已由带后缀 `llm_model` 唯一编码并由 `clamp_reasoning_effort` 100% 锁档，保持 `NULL` 可杜绝 CLI、沙箱文件或接口单改 `llm_model` 时出现双字段矛盾；(b) 与生产现有 27 个不迁移的 Antigravity 任务完全同构；(c) 接口与沙箱文件保存内置 Antigravity 带后缀模型时，未传、`null` 或传入与后缀一致的值均归一存 `NULL`，传入与后缀矛盾的值直接报 `HTTP 422` / `AutomationFileError` 拒绝；**当更新请求仅将 `llm_model` 从其他模型改为内置 Antigravity 模型而省略 `reasoning_effort` 时，服务端自动将旧 `reasoning_effort` 清空为 `NULL`**。

### 3.2 共用写入链路、SQL 字段列表、沙箱文件与历史版本快照改动清单

为确保个人任务、公司任务、REST 接口、沙箱 `.json` 文件、回收站与历史版本回滚全部闭环，逐文件改动点如下：

1. **请求与响应模型**（`src/server/models/automation.py`）：
   - `AutomationCreate`（`:506-574`）、`AutomationUpdate`（`:576-647`）新增 `reasoning_effort: Optional[ReasoningLevel] = None`；
   - `AutomationResponse`（`:807-860`）新增 `reasoning_effort: Optional[str] = None`。
2. **REST 路由显式 `null` 保留**（`src/server/app/automations.py:591-595`）：
   - 在 `fields = request.model_dump(exclude_none=True)` 后，与 `:593-594` 的 `llm_model` 对称增加：
     `if "reasoning_effort" in request.model_fields_set and request.reasoning_effort is None: fields["reasoning_effort"] = None`，确保显式传 `"reasoning_effort": null` 能传进个人与公司更新链路清空列值，而省略该键时 `fields` 不含 `"reasoning_effort"`。
3. **REST、沙箱与公司任务共用的生命周期校验与锁内复核**（`src/server/services/automations/lifecycle.py` & `src/dalton/services/company_agent_writes.py`）：
   - 在 `lifecycle.py` 新增共用归一校验函数 `_reconcile_reasoning_effort(user_id, *, target_model, effort_set, raw_effort, stored_effort, is_create, model_pref)`（先经 `user_models.classify_model` `:259-288` 区分系统模型与自定义模型）：
     - **系统内置 `antigravity-oauth` 模型**：显式非空 `raw_effort` 与后缀不一致时抛 `AutomationRefusal(..., field="reasoning_effort")`；其余情况一律归一为 `None`（更新时若 `stored_effort is not None`，即便未显式传 `reasoning_effort` 也强制写入 `reasoning_effort=None` 清空残留值）；
     - **非 Antigravity 模型**：显式非空 `raw_effort` 不在模型支持阶梯内时抛 `AutomationRefusal(field="reasoning_effort")`；新建（`is_create=True`）未传 `reasoning_effort` 时，多档模型默认写入 `'medium'`（不支持 `'medium'` 时取 `default_reasoning_effort`），无推理阶梯模型写 `None`；更新（`is_create=False`）未传 `reasoning_effort` 时，若未改模型则原样保留 `stored_effort`（旧 `NULL` 任务绝不静默补 `'medium'`），若改了新模型且旧非空 `stored_effort` 不在新模型阶梯内，无阶梯模型置 `None`、有阶梯模型按 `clamp_reasoning_effort` 裁剪；显式传 `None` 则置 `None`；
     - **跟随账户默认模型（`target_model is None`）**：新建未传时，若当前有效默认模型为内置 Antigravity 模型则存 `None`，否则默认 `'medium'`；更新未传时保留原值，显式传 `None` 清空。
   - **创建接入**：`lifecycle.create_automation`（`lifecycle.py:540-566`）计算初始 `reasoning_effort` 并传给 `auto_db.create_automation(...)`。
   - **更新预校验接入**：`lifecycle.prepare_owned_automation_update`（`lifecycle.py:645-698`，由个人更新 `:1009`、沙箱文件 `file.py:1036`、公司任务更新 `company_agent_writes.py:305` 共用）按 `"reasoning_effort" in update.model_fields_set` 执行校验并写入 `update_kwargs`。
   - **行锁内复核接入**：`lifecycle.recheck_locked_automation_update`（`lifecycle.py:736-817`，由个人更新 `_reconcile_locked_update` `:1079-1098` 和公司任务更新 `company_agent_writes.py:333-345` 在 `FOR UPDATE` 行锁内共用）按 `locked_row.get("llm_model")` 与 `locked_row.get("reasoning_effort")` 重新执行上述归一复核，防止并发改模型导致锁后状态失配。
   - **公司任务写白名单**：`src/dalton/services/company_agent_writes.py:41-55,298-303` 确认 `"reasoning_effort"` 不在 `_PROTECTED_WRITE_KEYS` 中，随 `writable` 正常进入 `prepare_owned_automation_update` 与 `auto_db.update_automation`。
4. **数据库 CRUD 与 `UpdateQueryBuilder`**（`src/server/database/automation.py`）：
   - `AUTOMATION_COLUMNS`（`:34-45`）加入 `reasoning_effort`；
   - `create_automation`（`:208-408`）函数参数、`build_create_history_columns` 入参字典（`:316-339`）与 `INSERT` 列/值元组（`:352-396`）加入 `reasoning_effort`；
   - `update_automation`：将 `"reasoning_effort"` 同时加入 `nullable_fields`（`:678-681`，确保 `None` 能生成 `SET reasoning_effort = NULL`）、`_config_update_fields`（`:682-699`，确保单改或单清空强度时 `is_config_update=True` 写入审计历史）与 `_build_query_from_kwargs` 标量列列表（`:763-775`）。
5. **版本审计快照、回收站与旧快照回滚兼容**：
   - `src/dalton/services/company_automations.py`：`ALLOWED_AUTOMATION_SNAPSHOT_FIELDS`（`:245-268`）加入 `"reasoning_effort"`；`build_automation_config_snapshot`（`:444-518` 的 `raw_snapshot` `:489-517`）加入 `"reasoning_effort": str(row["reasoning_effort"]) if row.get("reasoning_effort") is not None else None`，使 `sanitize_automation_snapshot` 与 `compute_snapshot_changes` 完整追踪其变更；
   - `src/dalton/services/automation_recycle.py`：`_SNAPSHOT_COLUMNS`（`:67-84`）加入 `"reasoning_effort"`；`_require_complete_snapshot`（`:248-275`）豁免旧快照缺失 `"reasoning_effort"`（缺失时补 `None`，不抛 `HTTP 409`；非空时校验合法性，Antigravity 模型归一为 `None`）；`_base_fields`（`:374-401`）与 `rollback_automation` 更新字典（`:1025-1026,1105-1144`）加入 `"reasoning_effort"`；
   - `src/dalton/database/automation_recycle.py`：`update_automation_recycle`（`:228-305`）的 `UPDATE automations SET ...` 语句加入 `reasoning_effort = %s`。
6. **沙箱文件映射与前端编辑面板**：
   - `src/server/services/automations/file.py`（`_EDITABLE` `:86-101`、`_definition` `:163-194`、`_Entry`、`_plan_create` `:538-540`、`_plan_update` `:577-649`）与 `src/ptc_agent/agent/backends/automations.py`（文档表 `:115-145`）同步支持 `reasoning_effort`；
   - `web/src/pages/Automations/components/MoreOptions.tsx:129-219`（及 `utils/form.ts`、`types/automation.ts`）：紧邻模型选择器新增“思考强度”选择器并在折叠摘要展示。选非 Antigravity 多档模型或非 Antigravity 账户默认时，新建默认 `'medium'`，编辑旧 `null` 任务显示“跟随默认”且未触碰时 `PATCH` 省略该键；**选 Antigravity 系列且未另选强度时，默认取该系列的 `default_effort`**（如选 `Gemini 4 Neon` 默认 `High` → `llm_model = "gemini-4-neon-high-oauth"`，`reasoning_effort = null`），打开旧 Antigravity 任务按后缀回显。

### 3.3 调度执行器首轮记录、唤醒归属与续跑/重试/备用模型继承

针对多工作进程（`--workers N`）下的首轮重试覆盖、同对话多任务唤醒串台、显式带模型重试跳过继承、以及订阅回退模型强度传递四个问题，精确改造如下：

1. **以首轮运行记录 `conversation_responses.metadata` 保存双态强度（防 `conversation_queries` 重试覆盖）**：
   - 原因：`conversation_queries.create_query`（`src/server/database/conversation/queries.py:163-174`）在幂等冲突更新时执行 `SET metadata = EXCLUDED.metadata`，同 `turn_index` 的后续重试可能覆盖首轮查询元数据；而每次运行在 `conversation_responses` 中均有独立行，且 `AutomationExecutor._run_turn`（`src/server/services/automation_executor.py:971-1015`）在 `START` 时即写入 `run_metadata`。
   - 改动：`AutomationExecutor._run_turn`（`automation_executor.py:978-1015`）构造 `ChatRequest(..., llm_model=automation.get("llm_model"), reasoning_effort=automation.get("reasoning_effort"))`，同时在 `run_metadata` 中无条件写入双态字段：
     - `"automation_reasoning_effort_configured": True`（区分 `d042` 上线前已在途的历史无字段旧运行 vs 本次首轮明确未设置 `NULL` 的运行）；
     - `"automation_reasoning_effort": automation.get("reasoning_effort")`（字符串档位或 `None`）。
   - 在 `src/dalton/services/automation_thread_model.py:318-379` 中，将 `_get_execution_first_turn_llm_model` 升级为 `_get_execution_first_turn_settings`，从该 `execution` 最早的首轮 `conversation_responses r`（匹配 `r.metadata->>'automation_execution_id' = exec_id`，按 `r.turn_index ASC, r.attempt_no ASC LIMIT 1`）读取 `(first_model, effort_configured, first_effort)`：
     - 若 `effort_configured` 为 `True`，直接使用 `first_effort`（为 `None` 时即保持无任务级覆盖，**不再反查可能已被用户中途修改过的 `automations` 表**）；
     - 仅当 `effort_configured` 为 `False`（升级瞬间在途旧执行）或首轮尚未落库便发生前置异常时，才回退读取 `automations` 表配置。
2. **唤醒请求（`wakeup`）通过 `wakeups.created_by_run_id` 精确绑定源执行（防同对话多任务串台）**：
   - 原因：唤醒投递请求（`src/server/services/wakeups/delivery.py:427-438`）不携带 `automation_execution_id`，若在 `thread_strategy='continue'` 复用对话上落入 `automation_thread_model.py:312-315` 的 `running[-1]`（`running_thread`）兜底，可能把任务 A 创建的唤醒或用户手动创建的唤醒误绑定到正在运行的任务 B 上。
   - 改动：`delivery.py:431` 发送的 `request.wakeup.notices` 已包含每条唤醒的 `wakeup_id`（`src/server/services/wakeups/message.py:135-151`），且 `wakeups` 表已持久化父运行列 `created_by_run_id`（`src/server/database/wakeups/contract.py:58`）。在 `src/dalton/services/automation_thread_model.py:254-315` 的 `_resolve_source_execution` 中（早于 `:310` 的 `running_thread` 兜底）：
     - 当 `getattr(request, "query_type", None) == "wakeup"` 时，复用 `extract_wakeup_notice_ids(request)`（`src/dalton/sharing.py:1581-1596`）提取 `wakeup_id` 列表，按通知顺序查询 `wakeups` 表（限定 `conversation_thread_id = thread_id`）获取非空 `created_by_run_id`；
     - 若查到 `created_by_run_id`，调用现有的 `_get_execution_for_run_id(created_by_run_id)`（`automation_thread_model.py:152-175`）：命中则返回 `(exec_row, "wakeup_creator_run")`（将 `"wakeup_creator_run"` 加入 `_EXPLICIT_EXECUTION_MATCH_KINDS` `:242-244`，享有订阅回退资格）；若 `created_by_run_id` 非空但不属于任何定时任务执行（即普通用户回合创建的唤醒），直接返回 `(None, "none")` **禁止继续回退 `running_thread`**；仅当历史旧唤醒记录 `created_by_run_id IS NULL` 时才保留原 `allow_running_fallback` 行为。
3. **显式带模型重试、跨工作进程续跑与订阅备用模型共用该强度**：
   - 在 `src/dalton/services/automation_thread_model.py:430-495`（`_resolve_automation_notification_context`）与 `:558-758`（`resolve_turn_config_with_automation_inheritance`）中**解耦模型继承与强度继承**：
     - 当请求显式携带了 `request.llm_model`（例如显式指定模型的重试 `src/server/app/threads/messaging.py:2032-2034`）但未显式携带 `request.reasoning_effort` 时，不再在 `:431` 提前跳过首轮设置读取——模型尊重 `request.llm_model`，但思考强度仍从 `target_exec` 首轮运行记录继承 `inherited_effort`；
     - 扩展 `src/server/services/thread_agent.py:33-76` 的 `thread_resolver`，使返回的 `resolve(model, *, reasoning_effort_override=UNSET)` 支持按回合覆盖 `effort`（当任务级 `inherited_effort` 非空且请求自身未显式传 `request.reasoning_effort` 时，覆盖被复用对话行 `thread.get("reasoning_effort")` 与用户偏好）；
     - 在 `resolve_turn_config_with_automation_inheritance` 中构造绑定了 `inherited_effort` 的 `_effective_resolve(cand_model)`，**同时传给主解析 `thread_model.resolve_turn_config`（`:682,692,718,750`）与订阅备用回退 `resolve_with_subscription_fallback(..., resolve_candidate=_effective_resolve)`（`:629-639`）**：若备用模型是多档模型（如 `gpt-6.2-sol-oauth`），自动继承任务强度并按其阶梯裁剪；若备用模型是单档 Antigravity 模型（如 `gemini-4-neon-high-oauth`），经 `clamp_reasoning_effort(["high"], "high", ...)` 仍稳定保持 `"high"`；
     - 在 `src/server/app/threads/messaging.py:907-923` 组装续跑/重试轮 `run_meta` 时，同步延续 `automation_execution_id`、`"automation_reasoning_effort_configured"` 与 `"automation_reasoning_effort"`，确保后续再次重试或派生唤醒跨工作进程链路不断裂。

### 3.4 存量 Opus 定时任务写入 `medium` 的受控运维脚本

新增 `scripts/dalton/ops/backfill_opus_automation_effort.py`（仅针对存量非 Antigravity 多档 Opus 任务一次性写入 `reasoning_effort = 'medium'`，写入后用户可在界面、CLI 或沙箱文件中正常修改或清空；执行时间由用户确认）：

1. **复用执行器真实模型选择与强度解析规则（杜绝误判与漏判）**：
   - 不使用仅读首选项的 `effective_default_model`（`src/server/services/llm/thread_model.py:627-637`），而是复用与执行器一致的解析规则（`src/server/services/llm/config.py:689-765`：`require_policy` → `builtins_only` → `drop_dead_models` → `gate_models_by_policy` → `gate_models_by_plan` → `load_user_context` → `chief_of_staff_default_runs` → `default_to_first_allowed` → `select_model`，并经 `user_models.classify_model` 排除同名自定义模型）计算每条 `reasoning_effort IS NULL` 任务（含个人、研究跟踪及 `COMPANY_SYSTEM_USER_ID` 公司任务，覆盖活跃/暂停/禁用/回收站未物理删除行）实际运行的系统内置 `effective_model`；
   - 仅当 `effective_model` 为非 `antigravity-oauth` 的系统多档 Claude Opus 模型（如 `claude-opus-5-5-oauth`、`claude-opus-5-5`；Antigravity 带后缀的 `claude-opus-5-5-*-oauth` 保持 `NULL` 不触碰）时纳入候选；
   - 同时调用 `resolve_turn_tuning(model_pref, effective_model)`（`src/llms/preferences.py:230-249`）与 `clamp_reasoning_effort` 计算当前现网实际生效强度：若某任务当前实际生效强度并非 `'medium'`（例如用户在设置页把 Opus 设为了 `'high'`），预演报告显式标红冲突，未指定 `--allow-override-ids` 时 `--apply` 以退出码 `2` 中止。
2. **预演清单锁定、防重复执行标记与单事务锁内复核**：
   - **第一阶段（只读预演生成清单）**：`--dry-run --out var/dalton/operator-records/opus-effort-plan-<UTC>.json` 在 `READ ONLY` 事务中生成确定性预演清单（记录每行的 `automation_id`、`user_id`、`workspace_id`、`llm_model`、`config_version`、`active_version`、相关用户的 `model_preference` 摘要及解析结果，文件权限 `0600`）；
   - **防重复回填保护（不覆盖用户后续清空的任务）**：每次写入在任务 `audit_history` 中记录专用动作名 `action = "backfill_opus_medium"`，并在 `var/dalton/operator-records/opus-automation-effort-applied.json` 写入批次完成记录。脚本运行时若检测到批次记录已存在，或目标行 `audit_history` 中已出现过 `"backfill_opus_medium"`（说明曾回填过、后被用户主动清空回 `NULL`），默认拒绝再次写入并以退出码 `2` 退出；
   - **第二阶段（消费已确认清单单事务写入）**：`--apply --plan <plan.json>` **只处理清单内已确认的 `automation_id`**（不重新盲扫全表 `NULL`）。先落盘 `0600` 归档文件（含 `pre-image` 与预期 `post-image`），再开启单一事务：按 `user_id` 字典序调用 `lock_user_preferences(user_id, conn=db)`（`src/server/database/user.py:839-852`）并对目标任务行按 `automation_id` 字典序执行 `SELECT ... FOR UPDATE`；在锁内重跑模型与强度解析，核对每行的 `config_version`、`llm_model`、`reasoning_effort IS NULL` 及用户偏好与 `plan.json` 完全一致（任一漂移立即整笔 `ROLLBACK` 并报退出码 `2`）；核对通过后写入 `reasoning_effort = 'medium'`，调用 `prepare_update_history_columns` 更新 `config_version`、`active_version` 与 `audit_history`；
   - **回退（`--rollback <archive.json>`）**：单事务锁内核对 `config_version == post_config_version` 且 `reasoning_effort == 'medium'` 的行还原为 `NULL` 并追加回退审计记录；已被用户二次修改过的任务自动跳过并打印保留说明。

---

## 4. 额度冷却按厂商合并（原"按系列"）

> **用户决定（2026-11-21，取代本节下文的"按系列"）**：同一 Antigravity 账号下，同一上游厂商的内置模型共用一个额度池——Gemini 系列（Neon / Hydrogen / Argon / Carbon）任一模型额度耗尽，该账号下全部 Gemini 模型一起冷却；经 Antigravity 的 Claude（Opus / Sonnet）彼此共用、与 Gemini 独立；GPT-OSS 单独。冷却键为 `dalton:quota_exhausted:{cred_user}:antigravity-oauth:vendor:{vendor}`（有 `cred_id` 时追加 `:{cred_id}`），`vendor` 由 `src/dalton/llm/antigravity_series.py` 的 `quota_cooldown_target` 按模型 ID 前缀确定；自定义同名模型与其他提供商仍用 `:model:` 单模型键。订阅调用不自动换模型（上游 `_on_its_payer` 对 OAuth 返回空备用列表）不变：冷却合并的作用是同厂商其他模型直接返回"订阅额度耗尽"卡片（带重置时间），不再发出必然失败的请求。不做旧格式键的过渡读取。


### 4.1 现状问题

Antigravity 返回 HTTP 429 `"individual quota reached"` 时，`antigravity_quota_reset_seconds`（`src/dalton/llm/providers/antigravity/client.py:146-177`）解析重置秒数并交由 `ModelResilienceMiddleware`（`src/ptc_agent/agent/middleware/model_resilience.py`）冷却。现有实现中，Redis 键 `quota_redis_key`（`:95-149`）与本地字典 `self._quota_exhausted`（`:738,742,823,843,975,1090`）均按单档带后缀模型名索引，导致同系列 `high` 配额耗尽后仍会继续尝试同系列的 `medium` / `low`。

### 4.2 独立键格式、全路径客户端透传与团队共享凭据规则

1. **分立键空间格式（杜绝 `-series` 与模型名碰撞）**：
   - 在 `src/dalton/llm/antigravity_series.py` 提供 `quota_cooldown_target(provider: str, model_name: str, *, is_custom: bool = False) -> tuple[Literal["series", "model"], str]`：
     - 当 `provider == "antigravity-oauth"` 且 `not is_custom`（`OAuthRebuildSpec.custom_entry is None`）且 `model_name` 命中系统内置 `antigravity-oauth` 条目时，返回 `("series", series_prefix)`（如 `("series", "gemini-4-neon")`、`("series", "claude-opus-5-5")`）；
     - 其余所有提供商模型或用户自定义模型返回 `("model", model_name)`。
   - 修改 `quota_redis_key`（`src/ptc_agent/agent/middleware/model_resilience.py:95-149`），在键中显式加入 `series:` / `model:` 类型段（模型名不含 `:`，两个命名空间在语法上不可能碰撞）：
     - Antigravity 内置模型（已改为厂商级，见本节开头的用户决定）：`dalton:quota_exhausted:{cred_user}:antigravity-oauth:vendor:{vendor}`（有 `cred_id` 时追加 `:{cred_id}`）；
     - 单模型：`dalton:quota_exhausted:{cred_user}:{provider}:model:{model_id}`（有 `cred_id` 时追加 `:{cred_id}`）。
   - `src/dalton/llm/oauth_refresh.py:20-41,124-148` 的 `OAuthRebuildSpec.model_name` 保持原始带后缀模型 ID 不变，401 刷新令牌重建仍 100% 还原原档位客户端。
2. **补齐全部本地读写与候选客户端透传（含错误汇总与切模型通知）**：
   - 在 `ModelResilienceMiddleware` 中新增 `self._quota_scope_key(name: str, client: Any = None) -> str`：优先返回 `self._quota_redis_key(name, client)`，当无 Redis 键（如纯测试 mock 非 OAuth 客户端）时回退返回 `f"local:model:{name}"`；`self._quota_exhausted` 字典统一以 `scope_key` 为键。
   - **逐处核实并传入有效候选客户端 `effective_client = request.model if cand_client is None else cand_client`**（注意 `_candidates` `:1108` 中主模型的 `cand_client` 为 `None`，必须回退取 `request.model`）：
     - `_local_quota_skipped(self, scope_key: str)`（`:821-825`）与 `_apply_remote_quota_state(self, scope_key: str, name: str, data: Any)`（`:826-845`）：按 `scope_key` 读写 `self._quota_exhausted[scope_key]`；
     - `_quota_skipped(self, name: str, client: Any = None)`（`:916-935`）与 `_aquota_skipped(self, name: str, client: Any = None)`（`:943-964`）：先算 `scope_key = self._quota_scope_key(name, client)` 查本地，未命中再查 Redis 并写回 `self._quota_exhausted[scope_key]`；
     - `_remember_quota_exhausted_local` / `_remember_quota_exhausted` / `_aremember_quota_exhausted`（`:965-1014`）：传入 `req.model`（`:1342,1448-1450` 已传），统一写入 `self._quota_exhausted[scope_key]` 与 Redis；
     - **`_raise_run_exhausted(self, records, candidates, request: ModelRequest)`（`:724-751`，调用处 `:1372,1480`）**：将 `request` 传入，对主模型 `primary_name, primary_client = candidates[0]` 计算 `eff_primary = request.model if primary_client is None else primary_client` 与 `primary_scope = self._quota_scope_key(primary_name, eff_primary)`，用 `self._quota_exhausted.get(primary_scope)` 和 `self._quota_skipped(primary_name, eff_primary)` 构造主模型配额拒绝错误与尝试轨迹；
     - **`_notify_bench_switch(self, request: ModelRequest, candidates, index)`（`:1072-1105`）**：将 `:1085-1091` 的 `for name, _ in candidates[:index]:` 修正为 `for name, cand_client in candidates[:index]:`，计算 `eff_client = request.model if cand_client is None else cand_client`，传入 `self._quota_skipped(name, eff_client)`，并用对应的 `benched_scope = self._quota_scope_key(benched_name, benched_eff_client)` 读取 `self._quota_exhausted.get(benched_scope)`，确保候选链上非主模型的订阅账号与系列冷却也能准确发出一次性 `model_fallback` 通知。
3. **团队共享凭据与多账号隔离语义**：
   - 当部署开启 `credentials.sharing == "team"` 时，`credential_user_id`（`src/dalton/credentials.py:53-65`）将所有团队成员解析到同一凭据持有者 `holder_id`（`DALTON_PM_USER_IDS[0]`），因为全员实际共用同一个 Google 订阅账号 `{cred_id}` 的上游配额池——任一成员触发某系列配额耗尽后，其他成员在同一共享账号上调用该系列各档位均会命中冷却并直接切往其他系列备用模型；
   - 当 `credentials.sharing != "team"`（个人凭据模式）或切换不同 Google 订阅账号（`credential_identity` `src/dalton/llm/oauth_refresh.py:47-72` 生成不同 `{cred_id}`）时，`{cred_user}` 与 `{cred_id}` 完全隔离。

---

## 5. 周报按系列合并

### 5.1 现状与价格一致性核查

宿主机每周任务 `T61`（`src/dalton/data_jobs/manifest.yaml:939-965`）通过 `scripts/dalton/data_assets/skills/model-usage-cost/scripts/run.sh`（`:6-15,112-116`）在独立虚拟环境 `$SKILL_DIR/venv` 中运行：
1. `usage_sources.py` 调用后端只读端点 `GET /api/v1/dalton/internal/usage/by-model?bucket=day`（路由定义在 `src/dalton/app/internal_usage_router.py:257-284`，按日 SQL 聚合在 `fetch_usage_by_model` `:177-254`），并与本机日志在 `reconcile(e)`（`usage_sources.py:678-760`）中对账；
2. `model_usage_cost_cli.py:310-448`（`window_result`）按 `references/prices.json` 计算每条记录的成本并生成按 `(is_dalton, provider, model)` 分组的窗口行；
3. `token_weekly_usage_tracker.py:255-380`（`process_usage_stats`）先拆分 `local_bucket` 与 `dalton_bucket` 来源小计（`:273-276`），再经 `merge_rows_by_model`（`:131-188`）合并模型榜单并由 `build_card`（`:675-760`）生成飞书卡片。

核查 `references/prices.json` 确认：同一系列各档位（含 `2026-12-01` 起 Argon/Carbon 调价分档）单价完全一致；本次保持 `prices.json` 与 `native_overlay.json` 计价配置不变，确保合并前后金额严格零差额。

### 5.2 改动方案：保来源小计、稳定系列归并、“档位未知”兜底与宿主机分发

1. **宿主机独立 `venv` 与离线快照共用同一份系列数据（零手工双写）**：
   - `src/dalton/app/internal_usage_router.py:177-254`：`fetch_usage_by_model` 保持 `models` 列表按原始带后缀 `m.key` 输出不变，同时在响应字典顶层附带由 `antigravity_series.py` 生成的 `"antigravity_series"` 列表；
   - `scripts/dalton/seed_openclaw_data.sh`（`:110-164`）：在同步 `var/dalton/openclaw-data/` 时，将零依赖的 `src/dalton/llm/antigravity_series.py` 及由当前 `native_overlay.json` + `native_sync_overlay.json` 导出的只读 `references/antigravity_series.json` 同步进 `skills/model-usage-cost/` 运行目录（仓库内直跑时亦可通过仓库相对路径直接导入 `src/dalton/llm/antigravity_series.py`），使 `$SKILL_DIR/venv/bin/python` 在离线或无仓库包路径时仍加载同一份模块与目录快照；
   - `model_usage_cost_cli.py` 在 `--json` 输出顶层写入 `"antigravity_series"`，供 `token_weekly_usage_tracker.py --input-json` 离线读取（旧快照无该字段时回退读取随技能包同步的 `references/antigravity_series.json`）。
2. **本机网关与 `agy` 记录保留可恢复档位，无法恢复的归入“档位未知”**：
   - 现状根因：`usage_sources.py:662-675` 的 `base_model` / `normalize_variant_model` 在 `reconcile(e)`（`:700-748`）阶段已将本机 `kind == 'agy'` 与 `kind == 'gateway'`（`provider == 'antigravity-cli-gateway'`）记录的强度后缀剥离（且通过 `agy_enum_names.json` `:735-746` 映射的枚举记录天生无强度后缀）。
   - 改动：在 `usage_sources.py:700-748` 调用 `normalize_variant_model` 覆盖 `r['model']` 之前，先用 `antigravity_series.parse_antigravity_series` 提取原始模型名（或对账网关记录 `g['model']`）中的标准强度后缀存入 `r['meta']['effort']`（不改变原有的 `r['model']` 对账与去重键）；对于源头即无强度后缀的本机记录，`meta['effort']` 保持为空，汇总时统一记入 **`"unknown"`（展示文案：`档位未知`）**。
3. **第一层保持原始计价与来源隔离，第二层先算来源小计再按稳定系列 ID 幂等合并**：
   - **第一层 `model_usage_cost_cli.py:310-448`（`window_result`）**：
     - 保持 `cost_of` 与 `reference_cost_of` 按每条记录原始 `provider`、`model`、`ts` 查价一行不改；
     - 保持分组键包含 `is_dalton`（`:344-349`，若同模型本机存在不同 `effort` 则按 `(is_dalton if by_model else r['source'], provider, model, meta.get('effort'))` 保留明细桶），**绝不在第一层把本机（`dalton_prod=False`）与生产（`dalton_prod=True`）合并到同一行**，防止第二层 `_is_dalton_prod_row`（`token_weekly_usage_tracker.py:121-123`）因 `source` 串入 `'Dalton 生产（订阅）'` 而把本机用量错计入生产小计！
   - **第二层 `token_weekly_usage_tracker.py:131-188,255-366`**：
     - 在 `process_usage_stats`（`:266-291`）中，**先在未跨来源合并的 `raw_rows` 上完成 `local_bucket` 与 `dalton_bucket` 来源小计计算**（对于 `dalton_bucket["models"]` 与 `local_bucket["models"]`，保留原始模型口径并在合并展示层单独按稳定系列聚合榜单，确保本机与生产的词元数、调用数、记录数、费用、无公开价词元与改动前 100% 一致）；
     - 随后在 `merge_rows_by_model(raw_rows, series_index)`（`:131-188,292`）中识别 Antigravity 系列记录（命中 `antigravity_series` 的 Dalton 生产 `*-oauth` 行，以及 `provider == 'antigravity-cli-gateway'` 或 `agy` 来源的同系列行；注意 Claude 官方订阅无强度后缀的 `claude-opus-5-5-oauth` 不命中 Antigravity 系列后缀，保持独立一行 `claude-opus-5-5-oauth (Anthropic)`，而经 Antigravity 的三档合并为 `Claude Opus 5.5 (Antigravity)`）：
       - 合并键采用稳定的 `(norm_provider, series_id)`（非 Antigravity 模型保持 `(norm_provider, model)`），展示名 `model` 设为消歧后的系列展示名（`Gemini 4 Neon`、`Gemini 4 Hydrogen`、`Gemini 4 Argon`、`Gemini 4 Carbon`、`Claude Opus 5.5 (Antigravity)`、`Claude Sonnet 5.5 (Antigravity)`、`GPT-OSS 120B (Antigravity)`）；
       - **完整保留无公开价与参考成本字段**：合并行与每个档位子项同步累加 `fresh`、`cache_write`、`cache_read`、`output`、`total_tokens`、`prompt_tokens`、`records`、`calls`、`runs`、`cost_usd`、`unpriced_tokens`、`no_public_price`、`reference_cost_usd`、`reference_model`、`price_note`，确保 `:351-365` 的 `unpriced_models` 专栏（如无公开价但带 `reference_only` 参考估算的模型）不丢失任何词元或参考金额；
       - **`effort_breakdown` 闭合与幂等合并**：按 `(*REASONING_LEVELS, "unknown")` 顺序维护 `effort_breakdown[bucket]`（无后缀记录归入 `"unknown"`）；若输入行本身已包含 `effort_breakdown`（重复传入已合并结果），直接逐桶累加已有 `effort_breakdown` 而非重复套壳，保证 `merge_rows_by_model(merge_rows_by_model(rows))` 完全幂等，且**任意合并行的 `effort_breakdown` 各桶之和恒等于该合并行的 `total_tokens`、`calls`、`cost_usd`、`unpriced_tokens` 与 `reference_cost_usd`**。
   - **卡片展示**（`token_weekly_usage_tracker.py:675-745`）：在 Top 10 与长尾列表的系列合并行下附加档位明细（例如：`· 档位明细: high 420.0K (300 次, $3.15) ｜ medium 200.0K (150 次, $1.42) ｜ 档位未知 35.0K (20 次, $0.25)`；无公开价档位显示 `无公开价` 或参考估算）。

---

## 6. 测试与上线验证计划

### 6.1 自动化单元测试清单

1. **目录分组与系列默认强度单测**（新增 `tests/unit/dalton/test_antigravity_series.py`）：
   - 验证从当前 `native_overlay.json` 识别 7 个可见系列及各自 `efforts`，排除 `visible: false` 的 `gemini-2.5-pro-oauth`，动态吸纳 `native_sync_overlay.json` 新增档位并排除 `retired` 条目；
   - 验证系列默认强度集中配置生效（`Neon=high`、`Hydrogen=high`、`Argon=high`、`Carbon=medium`、其他 `medium`），且当默认档不在可用子集时正确向下裁剪；
   - 验证 `user_models.classify_model` 前置过滤：同名用户自定义模型（`custom_models`）绝不被识别为 Antigravity 系列；
   - 验证 `GET /api/v1/models` 返回 `antigravity_series`，且 `models`、`model_metadata`、`request_lock.json` 与 `test_reasoning_lineage.py` 不变。
2. **前端公用层、对话/共享空间、设置页与外部引导单测**（新增 `web/src/lib/__tests__/antigravitySeries.test.ts`，扩展 `web/src/pages/ChatAgent/hooks/__tests__/useThreadModel.test.tsx`、`useThreadModel.composer.test.tsx`、`Settings.defaultModels.test.tsx`、`MoreOptions` 单测、`DefaultStep.test.tsx`）：
   - 验证 `seriesCatalog`（已存模型回显）与 `selectableSeries`（当前可选档位）分离：当某旧对话/旧任务/旧偏好存有因权限收紧而暂时不可选的档位（如 `xhigh`）时，仍准确显示为“系列 + 已存强度”而不退回裸 ID；同名自定义模型在所属分组独立显示，不混入 Antigravity 系列；
   - 验证选了系列但未选强度时，设置页、聊天模型面板、定时任务表单均落在该系列的 `default_effort`（如选 Neon 落在 `gemini-4-neon-high-oauth`）；
   - 验证**个人工作区已有对话**切到 Antigravity 或同系列改档时，发出单次 `PATCH /api/v1/threads/{id}` 更新 `llm_model` 并按需附带 `reasoning_effort: null`；**共享工作区已有对话**不发 `PATCH`（避免 403），改为在发送消息时携带 `llm_model`；
   - 验证**外部用户引导（Onboarding）**：`daltonSetup.ts` + `defaultChoices.ts` + `DefaultStep.tsx` 只按系列推荐和展示 `Gemini 4 Neon` 与 `Gemini 4 Hydrogen`，页面不渲染思考强度选择器，提交时写入 `preferred_model: "gemini-4-neon-high-oauth"`、`preferred_background_model: "gemini-4-hydrogen-high-oauth"`，且已存旧带后缀偏好正常保留。
3. **对话内切模型工具单测**（扩展 `tests/unit/dalton/test_switch_thread_model.py`）：
   - 验证传 `"Gemini 4 Neon"` 未传强度时写入系列默认档 `"gemini-4-neon-high-oauth"` 并清空线程 `reasoning_effort`；显式传合法强度写入对应后缀键，非法强度或跨渠道重名返回清晰提示；同名自定义模型不误判；非 Antigravity 多档模型单次原子更新 `(llm_model, reasoning_effort)` 并触发 `publish_thread_tuning`。
4. **定时任务 `d042`、生命周期、快照回滚、唤醒/重试续跑与 Opus 回填脚本单测**（扩展 `tests/unit/server/models/test_automation_models.py`、`tests/unit/server/app/test_automations.py`、`tests/unit/server/services/automations/test_lifecycle_{rules,update_validation}.py`、`tests/unit/ptc_agent/agent/backends/test_automations_backend.py`、`tests/unit/dalton/test_au1_recycle.py`、`tests/unit/dalton/test_automation_locked_recheck.py`、`tests/unit/server/services/test_automation_executor_{lifecycle,credit,route}.py`、`tests/unit/dalton/test_automation_thread_model.py`、`tests/unit/dalton/test_automation_continuation_identity.py`、`tests/unit/dalton/test_automation_subscription_fallback.py`，新增 `tests/unit/dalton/test_backfill_opus_automation_effort.py`）：
   - **写入与快照全覆盖**：覆盖 REST（个人任务与公司任务 `company_agent_writes.py`）、沙箱文件 `.json` 创建/更新/显式 `null` 清空、**仅修改 `llm_model` 切到 Antigravity 时自动清空旧 `reasoning_effort`**、锁内并发复核、`audit_history` 19 字段快照记录，以及回滚 `d042` 前缺字段旧版本快照不报 409；
   - **续跑、唤醒与重试全覆盖**：覆盖**同一复用对话连续运行两项不同定时任务时，任务 A 创建的 `wakeup` 通过 `created_by_run_id` 精确继承任务 A 的模型与强度（不串到正在运行的任务 B，普通用户唤醒也不串到定时任务）**；覆盖**同轮幂等重试不丢失首轮运行记录强度（含首轮明确未设 `NULL` 与历史缺字段区分）**；覆盖**显式传 `llm_model` 但未传 `reasoning_effort` 的重试仍继承任务首轮强度**；覆盖**跨工作进程续跑及触发订阅备用模型回退（多档备用继承任务强度、单档 `neon-high-oauth` 保持 `high`）**；
   - **升降级与旧版编辑恢复**：覆盖 `d042` 升级 → 兼容旧版编辑模型留下陈旧强度 → 再升级自动识别清理，以及 `downgrade d041` 导出 → 再升级按 `config_version` 恢复（跳过降级期间已编辑任务）；
   - **Opus 回填脚本**：覆盖组织策略/套餐回退下的真实有效模型判定、`--apply` 消费预演清单锁内复核、非 `medium` 偏好冲突退出码 `2` 中止、**防重复执行重新覆盖用户已清空任务**、回填后用户可正常改档或清空，以及 `--rollback` 安全还原。
5. **配额冷却与周报单测**（扩展 `tests/unit/middleware/test_model_quota_exhaustion.py`、`tests/unit/dalton/test_weekly_token_usage.py` 及 `model-usage-cost` 技能测试）：
   - **冷却**：验证 `:series:` 与 `:model:` 键格式互不碰撞；同账号同系列共享冷却、不同系列与不同 Google 订阅账号隔离、`credentials.sharing == "team"` 下团队成员共享同一持有者账号冷却、Redis 不可用时本地 `_quota_exhausted` 一致生效，以及主模型/非主模型候选在 `_raise_run_exhausted` 和 `_notify_bench_switch` 中均正确读取各自客户端的冷却状态；
   - **周报**：验证同时含本机（含无后缀 `agy` 记录）与 Dalton 生产记录的新旧快照在合并前后 `local_stats`、`dalton_stats`、全局 `totals`、无公开价模型词元与 `reference_cost_usd`、系列总计与各档明细（含 `档位未知`）之和 100% 一致；验证对已合并结果重复调用 `merge_rows_by_model` 完全幂等。

### 6.2 上线前干跑对账与真机核验

1. **周报固定时间两步离线对账（上线前必做，只生成不发送）**：
   - 第一步（固定 `--as-of` 生成唯一一份计量快照）：
     `bash scripts/dalton/data_assets/skills/model-usage-cost/scripts/run.sh --json summary --days 7 --by-model --as-of <UTC_ISO> > /tmp/weekly_snapshot.json`
   - 第二步（用同一份 `--input-json` 分别跑改动前后脚本核对结构化指标与卡片）：
     分别对旧版与新版 `token_weekly_usage_tracker.py` 执行 `--input-json /tmp/weekly_snapshot.json --json` 与 `--input-json /tmp/weekly_snapshot.json --dry-run`，逐项核对 **`local_stats`、`dalton_stats`、全局 `total_tokens`（含四分项）、`total_calls`、`total_records`、`unpriced_tokens`、`unpriced_models` 参考费用、`total_cost` 100% 相等**，且每个合并系列的 `effort_breakdown`（含 `档位未知`）加总严格等于该系列行总计。
2. **门禁与端到端实测**：
   - 运行 `bash scripts/dalton/check_hooks.sh`、`uv run pytest tests/unit/ -v --tb=short`、`cd web && pnpm test && pnpm typecheck` 全绿；
   - 部署 PR 3 后核验容器内 `docker exec dalton-langalpha-backend-1 uv run alembic -c src/dalton/alembic.ini current` 输出 `d042 (head)`；
   - 部署 PR 4 后核验存量对话回显、个人对话单次 `PATCH` 切档、共享空间对话按消息发送无 403、设置/对话/定时任务选系列默认落在该系列 `default_effort`，以及外部用户引导仅展示 `Gemini 4 Neon` / `Gemini 4 Hydrogen`（无强度选择器）且落库值为对应带后缀 ID。

---

## 7. 分步实施计划（PR 1 – PR 4）

4 个 PR 严格按依赖拓扑排序，每一步均可独立合入、独立部署验证、独立回退：

### PR 1：前后端“系列前缀 + 后缀”分组底座（含系列默认强度） + 系列级配额冷却

- **改动文件**：
  - 后端：`src/dalton/llm/antigravity_series.py`（新增，含集中默认强度配置 `ANTIGRAVITY_SERIES_DEFAULT_EFFORTS`，标注 `Carbon/Argon 待用户确认`）、`src/server/app/api_keys.py:666-753`（`GET /api/v1/models` 增加 `antigravity_series`）、`src/ptc_agent/agent/middleware/model_resilience.py:95-149,724-751,821-845,916-1014,1072-1105,1372,1480`（`:series:`/`:model:` 冷却键与全路径候选客户端透传）；
  - 前端公用底座（提前在 PR 1 落地，供 PR 3 / PR 4 直接消费，本步不改任何现有页面组件，线上界面零变化）：`web/src/api/model.ts:12-25`（`ModelsResponse.antigravity_series`）、`web/src/lib/antigravitySeries.ts`（`seriesCatalog` 与 `selectableSeries` 纯函数）、`web/src/hooks/useAllModels.ts:240-250,371-409`；
  - 测试：`tests/unit/dalton/test_antigravity_series.py`、`tests/unit/middleware/test_model_quota_exhaustion.py`、`web/src/lib/__tests__/antigravitySeries.test.ts`。
- **上线验证与回退**：无数据库变更、无可见 UI 变更；核验 `/api/v1/models` 返回 7 个可见系列及默认强度。若需回退，直接回退 PR 1 代码重新部署前后端即可（Redis 系列冷却键带 TTL 自然过期，与单模型 `:model:` / 旧键互不干扰）。

### PR 2：周报按系列合并（保来源小计、附各档及“档位未知”明细）

- **改动文件**：
  - `src/dalton/app/internal_usage_router.py:177-254`（附带输出 `antigravity_series`）；
  - `scripts/dalton/data_assets/skills/model-usage-cost/scripts/usage_sources.py:662-675,700-748`、`model_usage_cost_cli.py:310-448`、`scripts/dalton/data_assets/scripts/token_weekly_usage_tracker.py:131-188,255-366,675-760`；
  - `scripts/dalton/seed_openclaw_data.sh`（同步 `antigravity_series.py` 与 `references/antigravity_series.json` 到 `var/dalton/openclaw-data/skills/model-usage-cost/`，适配 `run.sh:6-15,112-116` 独立 `venv`）；
  - 测试：`tests/unit/dalton/test_weekly_token_usage.py` 及技能自检。
- **上线验证与回退**：先按 §6.2 第 1 条完成固定 `--as-of` 生成快照 + 新旧脚本 `--input-json` 对账，通过后部署并运行 `scripts/dalton/seed_openclaw_data.sh --apply`。若需回退，检出旧版周报脚本并重新执行 `seed_openclaw_data.sh --apply` 即可立即恢复原逐档周报，无数据库变更。

### PR 3：定时任务新增全模型“思考强度”（`d042` + 续跑/唤醒继承 + Opus 回填脚本）

- **改动文件**：
  - 数据库迁移：`src/dalton/migrations/versions/d042_automation_reasoning_effort.py`（`down_revision = "d041"`）；
  - 后端写入与快照：`src/server/models/automation.py:506-647,807-860`、`src/server/app/automations.py:591-595`、`src/server/services/automations/lifecycle.py:451-580,625-817,1079-1098`、`src/dalton/services/company_agent_writes.py:278-368`、`src/server/database/automation.py:34-45,208-408,678-800`、`src/dalton/services/company_automations.py:245-268,444-518`、`src/dalton/services/automation_recycle.py:67-84,248-275,374-401,1007-1152`、`src/dalton/database/automation_recycle.py:228-305`、`src/server/services/automations/file.py:86-101,163-194,514-650`、`src/ptc_agent/agent/backends/automations.py:115-145`；
  - 后端执行与续跑继承：`src/server/services/automation_executor.py:971-1015`、`src/server/services/thread_agent.py:33-76`、`src/dalton/services/automation_thread_model.py:242-495,558-758`、`src/server/app/threads/messaging.py:767-804,907-925`；
  - 前端定时任务表单（消费 PR 1 已落地的 `antigravitySeries.ts`）：`web/src/pages/Automations/components/MoreOptions.tsx:129-219`、`web/src/pages/Automations/utils/form.ts`、`web/src/types/automation.ts`；
  - 运维脚本（部署不自动运行）：`scripts/dalton/ops/backfill_opus_automation_effort.py`；
  - 测试：§6.1 第 4 条全部单测文件。
- **上线与存量 Opus 脚本执行步骤**：
  1. 执行 `scripts/dalton/ops/prod/deploy_main.sh --expect-migration d042` 上线，核实 `docker exec dalton-langalpha-backend-1 uv run alembic -c src/dalton/alembic.ini current` 为 `d042`；
  2. 运行只读预演 `uv run python scripts/dalton/ops/backfill_opus_automation_effort.py --dry-run --out var/dalton/operator-records/opus-effort-plan.json` 供用户审阅；
  3. **待用户确认执行时间后**，运行 `--apply --plan var/dalton/operator-records/opus-effort-plan.json` 一次性将目标 Opus 任务设为 `'medium'`（之后用户可随时在界面修改）。
- **回退与再升级规程（严格适配 `docker-compose.yml:96-98` 宿主机挂载 `./src:/app/src` 与 `docker_entrypoint_dalton.sh:20` 启动自动 `upgrade head`）**：
  - **若已跑过 Opus 回填脚本且需撤销回填值**：先执行 `uv run python scripts/dalton/ops/backfill_opus_automation_effort.py --rollback <archive.json>`；
  - **路径 A（标准表结构降级回退，严格遵循 `scripts/dalton/ops/prod/deploy_main.sh:618-654` 方案 B）**：
    1. **停写与备份**：卸载 `com.dalton.backend-watchdog` 及桥接/监控守护进程，停止 `edge`、`crawler`、`backend` 容器并确认无活跃数据库连接，触发 `com.dalton.pg-backup` 完成停写后备份；
    2. **导出新列数据**：将非空行导出至 `var/dalton/operator-records/d042-downgrade-export-<UTC>.json`（权限 `0600`，含 `automation_id, llm_model, reasoning_effort, config_version, active_version`）；
    3. **维护容器降级与宿主机复位**：趁宿主机 `./src` 仍含 `d042` 文件时，运行 `docker compose run --rm --no-deps --entrypoint "" backend sh -c "cd /app && uv run alembic -c src/dalton/alembic.ini downgrade d041"`，核实 `alembic_version_dalton = d041` 后，在宿主机执行 `git reset --keep <PR2_SHA>`、重建前端并强制重建容器，恢复守护进程；
    4. **修复后重新升级的数据恢复**：再次升级至 `d042` 后，运行恢复子命令 `--restore-downgrade <export.json>`，在单事务 `FOR UPDATE` 锁内仅对 **`config_version` 与 `llm_model` 均未在降级期间变化** 的任务写回原 `reasoning_effort`（降级期间已被修改的任务保持 `NULL` 并输出清单，绝不套用过时强度）。
  - **路径 B（不降级表结构的快速热修兼容检出回退）**：
    1. 由于容器从宿主机挂载 `./src:/app/src`，**不得仅替换镜像或直接 reset 到无 `d042` 文件的提交**，而是在宿主机检出**保留 `src/dalton/migrations/versions/d042_automation_reasoning_effort.py` 的代码回退提交**并重建前后端，重启后核实容器内 `alembic -c src/dalton/alembic.ini current` 仍正常识别 `d042`；
    2. **防止旧版运行期间改模型留下过时强度**：旧代码编辑任务生成的 `audit_history` 最新激活快照不含 `"reasoning_effort"` 键。再次升级新代码时，服务层与运维脚本 `--reconcile-stale` 自动检查：凡 `reasoning_effort IS NOT NULL` 但当前 `active_version` 快照不含 `"reasoning_effort"` 键且当前 `llm_model` 为内置 Antigravity 模型或与最后一次带 `"reasoning_effort"` 快照的模型不一致的记录，一律将陈旧 `reasoning_effort` 清空为 `NULL` 并写入审计历史。

### PR 4：聊天、设置页、子智能体、`switch_thread_model` 与外部用户引导合并上线

- **改动文件**：
  - 聊天界面：`web/src/components/ui/chat-input.modelPicker.tsx:125-230`、`chat-input.modelMenu.tsx:160-370`、`chat-input.tsx:411-473`、`web/src/pages/ChatAgent/hooks/useThreadModel.ts:180-252,377-383`、`useThreadTuning.ts:51-165`、`web/src/pages/ChatAgent/components/ChatView.tsx:435-445,524-528,2114-2122`；
  - 设置页与子智能体侧栏：`web/src/components/model/ModelSelector.tsx:50-195`、`ModelTierConfig.tsx:158-190`、`FallbackModelsPicker.tsx:15-241`、`PerModelMatrix.tsx:50-260`、`web/src/pages/Settings/panels/ModelsPanel.tsx:372-788`、`web/src/pages/Plugins/components/subagents/SubagentAside.tsx:74-165`；
  - 外部用户引导（基于已合入 PR #435 的主干）：`web/src/config/daltonSetup.ts`、`web/src/pages/Setup/answers/defaultChoices.ts:99-152`、`web/src/pages/Setup/steps/DefaultStep.tsx:70-90,188-420`；
  - 对话内切模型工具：`src/dalton/tools/thread_model_tools.py:116-189,234-382`、`src/server/services/llm/thread_model.py:218-344,535-624`；
  - 测试：§6.1 第 2、3 条全部单测文件。
- **上线验证与回退**：无数据库结构变更，落库模型 ID 100% 保持原带后缀键。若需回退，直接将宿主机检出回退至 PR 3 提交并重新构建部署前后端即可，上线期间新建或修改的全部对话、偏好、子智能体与引导记录在旧版下天然可读可运行，零数据迁移回滚成本。

---

## 8. 审查意见处理记录

### 8.1 旧“底层改名迁移”方案已撤销事项与现方案核心验证项

- **因“底层保留带后缀模型 ID”已整体撤销的旧方案事项**：不再改动 `native_overlay.json` 清单键或引入 `ChatAntigravity` 动态改写模型 ID，因此此前初审/终审针对全库 6 张表改名迁移、上下文压缩 `summary_reasoning_effort` 静默降档、`user_preferences` 多槽位同系列去重冲突、`hook_outbox` `steer` 与 `subagent_runs.replay_facts` 新旧键失配、组织策略 `Policy` 扩权、`dalton_llm_journal` 幂等哈希变化、`notional_pricing.py` 历史账单失联、`native_catalog` 同步器失配以及双轨目录/14 天清理期的全部风险均不再产生。
- **现方案仍须严格把关并已在本文闭环的核心事项**：单一目录数据源与集中系列默认强度、旧不可选档位回显与同名自定义模型隔离、个人对话单次原子 `PATCH` 与共享空间按消息提交、`d042` 共用写入链与旧快照回滚、唤醒 `created_by_run_id` 源执行归属与重试/备用模型强度继承、宿主机 bind-mount 下的迁移升降级恢复、Opus 回填脚本有效模型解析与防重复回填、冷却键空间隔离与全路径客户端透传、周报来源小计防错计与“档位未知”明细闭合。

### 8.2 第 5 版审查意见处理表（Codex 全量逐条对照）

| 问题（严重程度 / 原引位置） | 处理方式（具体落点与测试覆盖） | 文档章节 |
|---|---|---|
| **1. [高] 定时任务改动清单漏列 REST 与沙箱共用写入逻辑、SQL 更新字段列表和快照构造函数**（原引 §3.1、§3.3、§7 PR 3；`automations/lifecycle.py:540–553,690–698,1079–1098`；`database/automation.py:758–775`；`company_automations.py:489–517`） | 补齐完整共用链路：`app/automations.py:591-595` 保留显式 `reasoning_effort: null`；在 `lifecycle.py` 的 `create_automation`（`:540-566`）、`prepare_owned_automation_update`（`:645-698`）及锁内 `recheck_locked_automation_update`（`:736-817,1079-1098`）与 `company_agent_writes.py:278-368` 共用逻辑中区分省略与显式 `null`，并在仅改 `llm_model` 切到 Antigravity 时强制将旧 `reasoning_effort` 清为 `None`；在 `database/automation.py` 同步加入 `nullable_fields`（`:678-681`）、`_config_update_fields`（`:682-699`）与 `_build_query_from_kwargs`（`:763-775`）；在 `company_automations.py:489-517` `build_automation_config_snapshot`、`services/automation_recycle.py:67-84,248-275,374-401,1105-1144` 及 `database/automation_recycle.py:228-305` 补齐快照构造与旧版本恢复；单测覆盖 REST、沙箱、公司任务与旧快照回滚。 | §3.1、§3.2、§6.1.4、§7 PR 3 |
| **2. [高] 唤醒请求无执行 ID 易串任务、显式带模型重试跳过强度继承、同轮幂等重试可覆盖查询元数据**（原引 §3.2；`automation_thread_model.py:254–315,430–441,629–636`；`wakeups/delivery.py:427–438`；`conversation/queries.py:163–174`） | 改为在首轮运行记录 `conversation_responses.metadata`（`automation_executor.py:978-1015`）写入双态 `(automation_reasoning_effort_configured, automation_reasoning_effort)`，避免被 `conversation/queries.py:163-174` 同轮幂等 `DO UPDATE` 覆盖，并严格区分历史缺字段与本次明确未设 `NULL`；唤醒轮在 `automation_thread_model.py:254-315` 用 `sharing.py:1581-1596` `extract_wakeup_notice_ids` 读取 `wakeups.created_by_run_id`（`database/wakeups/contract.py:58`）反查源 `automation_execution`（非定时任务创建的唤醒禁止回退 `running_thread`）；解耦 `:430-441` 使显式带 `llm_model` 的重试仍读取首轮强度，并通过 `thread_agent.py:33-76` 将继承强度统一传给续跑与 `resolve_with_subscription_fallback`（`:629-639`）；单测覆盖同对话连续两项任务唤醒、显式模型重试与跨工作进程续跑。 | §3.3、§6.1.4、§7 PR 3 |
| **3. [高] 按裸模型键和当前可选集合反查会漏掉暂时不可选的旧档位、误判同名自定义模型，且共享空间对话走 `PATCH` 会返回 403**（原引 §2.1–§2.3；`user_models.py:259–285`；`useAllModels.ts:371–390`；`threads/crud.py:688–711`） | 前后端均先验证系统内置渠道身份（后端经 `user_models.classify_model` `:259-288` 排除 `ModelSource.CUSTOM`；前端对照 `customModels` / `customPairs` 与 `rawMetadata[key]?.provider === 'antigravity-oauth'`）；在 `useAllModels.ts` 与 `antigravitySeries.ts` 分离 `seriesCatalog`（全量目录用于已存模型回显，含暂时不可选旧档位）与 `selectableSeries`（当前权限过滤后的可选档位）；聊天页通过 `useSharedEntry(workspaceId)` 区分：个人对话用单次 `PATCH /api/v1/threads/{id}`（`crud.py:688-755`）原子更新 `llm_model` 并按需清空 `reasoning_effort`，共享空间对话不发 `PATCH`（沿用随消息提交 `llm_model` 的规则防 403）；单测覆盖旧对话/旧任务、同名自定义模型、权限收窄与共享空间。 | §2.1、§2.2、§2.4、§6.1.1–6.1.3、§7 PR 1 & PR 4 |
| **4. [高] 周报本机网关和部分 `agy` 记录汇总前已失去强度后缀导致明细加总小于系列总计，且第一层提前跨来源合并会把本机用量错计入生产小计**（原引 §5.2；`usage_sources.py:662–675,735–747`；`model_usage_cost_cli.py:344–349,404–445`；`token_weekly_usage_tracker.py:266–315`） | 在 `usage_sources.py:700-748` 剥离后缀前保留可恢复的 `meta['effort']`，无法恢复强度的本机记录归入显式 `"unknown"`（`档位未知`）桶；第一层 `model_usage_cost_cli.py:344-445` 保持原始计价输入且严格保留 `is_dalton` 来源隔离不跨来源合并；第二层 `token_weekly_usage_tracker.py:266-315` 先算 `local_bucket` 与 `dalton_bucket` 来源小计，再在 `merge_rows_by_model`（`:131-188`）按稳定 `series_id` 幂等合并，完整保留 `unpriced_tokens` 与 `reference_cost_usd`；单测与干跑同时核对来源小计、全局总计、系列总计、各档（含 `档位未知`）加总及重复合并幂等性。 | §5.2、§6.1.5、§6.2.1、§7 PR 2 |
| **5. [高] 生产容器从宿主机挂载 `src` 仅换镜像无法保证读到 `d042`，保留新列跑旧代码改模型会留下过时强度，且降级分支缺再升级恢复步骤**（原引 §7 PR 3；`docker-compose.yml:96–98`；`docker_entrypoint_dalton.sh:20`；`scripts/dalton/ops/prod/deploy_main.sh:625–642`） | 明确生产容器通过 `docker-compose.yml:96-98` bind-mount `./src:/app/src` 且 `docker_entrypoint_dalton.sh:20` 读取宿主机迁移文件：路径 A（标准降级）按 `deploy_main.sh:618-654` 停写 → 导出非空 `reasoning_effort` → 维护容器 `downgrade d041` → 宿主机 `git reset --keep`，并定义重新升级后按 `config_version` 与 `llm_model` 锁内比对恢复（跳过降级期已编辑任务）；路径 B（热修兼容检出）要求宿主机检出含 `d042` 文件的提交并核实容器 `alembic current`，且重新升级时基于 `audit_history` 快照不含 `"reasoning_effort"` 键自动清洗旧版编辑模型后遗留的过时强度；补齐升降级与旧版编辑再升级测试。 | §6.1.4、§6.2.2、§7 PR 3 |
| **6. [中] Opus 回填脚本仅用 `effective_default_model` 未覆盖策略/套餐/可用回退，且仅筛 `NULL` 无法防止重复执行重新覆盖用户已清空的任务**（原引 §3.4；`llm/config.py:698–727,760–765`；`llm/thread_model.py:627–637`；`database/user.py:839–852`） | 脚本改为复用执行器完整模型选择与强度解析规则（`llm/config.py:689-765` + `preferences.py:230-249` + `classify_model`）；拆为 `--dry-run --out <plan.json>` 与消费该确定性清单的 `--apply --plan <plan.json>`，在 `lock_user_preferences`（`database/user.py:839-852`）与行级 `FOR UPDATE` 锁内复核版本与偏好；在 `audit_history` 写入 `action="backfill_opus_medium"` 并记录批次哨兵文件，拒绝重复回填已执行批次或曾回填后被用户清空的任务。 | §3.4、§6.1.4、§7 PR 3 |
| **7. [中] 冷却 `-series` 与模型名共用键空间可能碰撞，漏列 `_raise_run_exhausted` 与 `_notify_bench_switch` 本地读取及候选客户端透传，且未说明团队共享凭据行为**（原引 §4.2；`model_resilience.py:95–148,724–750,1072–1094`；`credentials.py:53–64`） | 将冷却键拆分为独立命名空间 `:series:{series_prefix}` 与 `:model:{model_id}`；全面改造 `_raise_run_exhausted`（`:724-751,1372,1480`）与 `_notify_bench_switch`（`:1072-1105`），对所有候选统一按 `request.model if cand_client is None else cand_client` 传入有效客户端并通过 `_quota_scope_key` 访问 `self._quota_exhausted`；明确说明 `credentials.py:53-65` `credentials.sharing == "team"` 下团队成员共用同一持有者订阅账号时共享冷却、个人凭据及多账号严格隔离，并补齐对应单测。 | §4.2、§6.1.5、§7 PR 1 |
| **8. [中] PR 3 依赖系列选择但前端公用模块排在 PR 4、未说明宿主机独立 `venv` 如何加载共享模块、以及 `--input-json` 下 `--as-of` 不参与统计**（原引 §5.2、§7 PR 2–3；`web/src/api/model.ts:12–25`；`model-usage-cost/scripts/run.sh:6–14,112–115`；`token_weekly_usage_tracker.py:966–981`） | 将 `web/src/api/model.ts:12-25`、`web/src/lib/antigravitySeries.ts` 与 `useAllModels.ts` 提前放入 PR 1；写明 `seed_openclaw_data.sh` 同步零依赖 `antigravity_series.py` 与目录快照至 `skills/model-usage-cost/` 供 `run.sh:6-15,112-116` 独立 `venv` 加载，同时在线接口与计量快照内嵌 `antigravity_series`；将对账流程拆为先用 `run.sh ... --as-of <ISO>` 生成快照文件，再对同一快照文件分别跑新旧 `token_weekly_usage_tracker.py --input-json` 的 `--json` 与 `--dry-run`。 | §2.1、§5.2、§6.2.1、§7 PR 1 & PR 2 |
| **9. [低] 6 处代码行号与单测文件名引用偏差**（原引 §1.1–§1.2、§5.2、§6.1：`modelTuning.ts:44–46`、`clients.py:1143–1146`、`preferences.py:230–249`、`oauth_refresh.py:124`、`internal_usage_router.py:257–284` 及测试文件名） | 已对照当前代码逐一核实修正：`hasTuningMenu` 修正为 `modelTuning.ts:44-46` 并更正单档胶囊可见性描述；`resolve_turn_tuning` 修正为调用处 `clients.py:1143-1146`、定义处 `preferences.py:230-249`；`rebuild_oauth_client` 修正为 `oauth_refresh.py:124-148`（`_resolve` 在 `:90-100`）；内部用量路由修正为 `internal_usage_router.py:257-284`（`fetch_usage_by_model` `:177-254`）；单测文件名修正为实际存在的 `tests/unit/server/services/test_automation_executor_{lifecycle,credit,route}.py` 与 `tests/unit/dalton/test_au1_recycle.py`。 | §1.1、§1.2、§5.1、§5.2、§6.1.4 |
| **10. [疑似] 快照中无法核实外部引导固定推荐入口及生产存量统计查询口径**（原引 §1.4、§2.2、§6.2；`defaultChoices.ts:99–151`） | 明确标注外部引导推荐配置入口为 PR #435 新增的 `web/src/config/daltonSetup.ts` 及 `defaultChoices.ts:99-152`、`DefaultStep.tsx:45-430`（注明当前分支早于 PR #435，PR 4 基于合入 PR #435 的主干修改），并合入“只按系列推荐 Neon / Hydrogen、不出现强度选择、保存系列默认强度带后缀 ID”的新决定；在 §1.3 注明生产数字统计时间（`2026-11-21`）及两条精确只读统计 SQL。 | §1.3、§2.3、§6.1.2、§7 PR 4 |
| **11. [低] 通用术语表、旧改名方案风险与实施细节多处重复**（原引 §术语说明、§1.3、§8） | 删除整节通用常识术语表与 §1 重复边界大表；将 §8.1 历史旧方案说明压缩为“已撤销事项”与“仍须验证事项”两小段，全文聚焦可拆 PR 执行的改动落点、边界处理与测试断言。 | 全文结构、§1、§8.1 |
