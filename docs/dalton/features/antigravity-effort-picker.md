# Antigravity 系列合并、思考强度选择与厂商级额度冷却

本文说明 Dalton 对 Google Antigravity 渠道模型的按系列合并展示、全局（聊天、设置、子智能体、定时任务）思考强度（reasoning effort，模型内部推理深度档位）选择、定时任务级思考强度（`d042`）、厂商级额度冷却与周报按系列合并的现行实现。

## 1. 底层设计与目录聚合

- **底层保留带后缀模型 ID**：数据库、配置文件与上游请求继续使用带强度后缀的键名（如 `gemini-4-neon-high-oauth`），不做存量模型 ID 改名迁移。仅在界面展示、厂商级额度冷却和周报汇总三层进行归并，保证存量对话与任务稳定兼容。
- **单一分组与默认强度数据源（`src/dalton/llm/antigravity_series.py`）**：零第三方依赖的纯标准库模块，供后端路由、定时任务校验、配额冷却与宿主机周报脚本共用。
  - 集中默认强度映射 `ANTIGRAVITY_SERIES_DEFAULT_EFFORTS`（支持在 `src/dalton/dalton.yaml` 下通过 `llm.antigravity_series_default_efforts` 覆盖同名系列）：
    - `gemini-4-neon`: `high`
    - `gemini-4-hydrogen`: `high`（注意：后台模型槽位及新用户引导中的后台模型优先选取低档 `low`，低档不可选时使用该系列最低可用档）
    - `gemini-4-argon`: `high`
    - `gemini-4-carbon`: `medium`
    - 其他经 Antigravity 的系列回退 `FALLBACK_SERIES_DEFAULT_EFFORT = "medium"`。
  - 若配置的默认档不在当前可用档位内，`resolve_series_default_effort` 按 `REASONING_LEVELS` 阶梯向下裁剪（低于下限时取最低可用档）。
- **目录聚合与公开接口**：`build_antigravity_series_catalog` 遍历合并后目录中 `provider == "antigravity-oauth"` 且非隐藏（`visible is not False`）、非退役（`retired` 为假）的条目，当前 `src/dalton/llm/native_overlay.json` 共 22 行（21 行可见，1 行隐藏的 `gemini-2.5-pro-oauth`），聚合成 7 个可见系列：

| 系列标识（`series_id`，仅用于分组） | 展示名（`display_name`） | 可选强度（`efforts`） | 系列默认强度 | 默认模型键（`default_model`） |
|---|---|---|---|---|
| `antigravity-oauth:gemini-4-neon` | `Gemini 4 Neon` | `low` / `medium` / `high` / `xhigh` | `high` | `gemini-4-neon-high-oauth` |
| `antigravity-oauth:gemini-4-hydrogen` | `Gemini 4 Hydrogen` | `low` / `medium` / `high` / `xhigh` | `high` | `gemini-4-hydrogen-high-oauth` |
| `antigravity-oauth:gemini-4-argon` | `Gemini 4 Argon` | `low` / `medium` / `high` | `high` | `gemini-4-argon-high-oauth` |
| `antigravity-oauth:gemini-4-carbon` | `Gemini 4 Carbon` | `low` / `medium` / `high` | `medium` | `gemini-4-carbon-medium-oauth` |
| `antigravity-oauth:claude-opus-5-5` | `Claude Opus 5.5` | `low` / `medium` / `high` | `medium` | `claude-opus-5-5-medium-oauth` |
| `antigravity-oauth:claude-sonnet-5-5` | `Claude Sonnet 5.5` | `low` / `medium` / `high` | `medium` | `claude-sonnet-5-5-medium-oauth` |
| `antigravity-oauth:gpt-oss-120b` | `GPT-OSS 120B` | `medium`（单档） | `medium` | `gpt-oss-120b-medium-oauth` |

- `GET /api/v1/models`（`src/server/app/api_keys.py`）保留全部带后缀的 `models` 与 `model_metadata` 不变，在顶层附带 `antigravity_series` 供前端读取（类型定义见 `web/src/api/model.ts`）。

## 2. 前端选择器、同名自定义模型隔离与对话内工具边界

- **双索引分离（`web/src/lib/antigravitySeries.ts` & `web/src/hooks/useAllModels.ts`）**：
  - `seriesCatalog`（全量目录索引）：用于已存对话、任务和偏好的回显。先排除与用户 `custom_models` 同名的自定义模型且校验 `provider === 'antigravity-oauth'`，即使某个旧档位因策略收窄暂时不可选，仍准确回显为“系列名 + 已存强度”而不退回裸模型 ID。
  - `selectableSeries`（当前可选索引）：仅对当前可用模型池求交集并剔除被自定义模型遮蔽的键，用于各下拉菜单与切档选项。
- **全场景选择器**：
  - **聊天输入框与市场对话面板**（`chat-input.modelPicker.tsx`、`chat-input.modelMenu.tsx`、`useThreadModel.ts`、`useThreadTuning.ts`、`MarketChatPanel.tsx`）：Antigravity 分组、搜索、收藏和最近使用均按系列折叠为 1 行，选中系列后右侧强度胶囊切换同系列带后缀键。个人工作区已有对话通过单次 `PATCH /api/v1/threads/{id}` 原子更新 `llm_model` 并按需清空独立 `reasoning_effort`；共享工作区对话与未创建对话不发 `PATCH`（避免 403），改为在发送消息时随请求体提交。前端在服务端确认前以用户最近一次显式选择的模型与强度为准，防止异步刷新回弹。
  - **设置页、自动化与子智能体侧栏**（`ModelSelector.tsx`、`ModelTierConfig.tsx`、`MoreOptions.tsx`、`FallbackModelsPicker.tsx`、`PerModelMatrix.tsx`、`ModelsPanel.tsx`、`SubagentAside.tsx`）：单模型槽位和自动化表单的系列选择器均按系列折叠，并在旁侧提供该系列的可用思考强度切换；「后台模型」（Background）槽位隐藏强度下拉菜单，优先选取低档（`low`），低档不可用时取最低可用档。
  - **新用户引导（Onboarding）**：仅按系列展示推荐模型且不出现强度选择器，提交时保存带后缀 ID（主模型 `gemini-4-neon-high-oauth`，后台模型 `gemini-4-hydrogen-low-oauth`）；详见 [external-onboarding.md](./external-onboarding.md)。
- **对话内切模型工具（`switch_thread_model`，`src/dalton/tools/thread_model_tools.py`）**：仅支持用户本轮明确要求时调用，参数仅接收单个 `model`（模型 ID 或精确显示名），**不接收独立的 `reasoning_effort` 参数，也不自动把裸系列名展开为默认强度**；若用户只说系列名（如 `Gemini 4 Neon`），工具说明要求先确认具体强度档位后再传入对应带后缀模型 ID。

## 3. 定时任务思考强度（`d042`）与 Opus 回填脚本

- **表结构与写入规则（迁移 `d042_automation_reasoning_effort.py`）**：
  - `automations` 新增可空列 `reasoning_effort VARCHAR(16)`，校验约束限定为 `NULL` 或 `('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')`。
  - REST 接口（`src/server/app/automations.py`）、沙箱虚拟文件（`src/server/services/automations/file.py`）、公司自动化写入（`src/dalton/services/company_agent_writes.py`）与版本快照/回收站（`company_automations.py`、`automation_recycle.py`）共用 `lifecycle.py` 校验与行锁内复核。
  - **Antigravity 带后缀任务列值固定存 `NULL`**：强度已由带后缀 `llm_model` 锁档，避免双字段矛盾；把已有任务从其他模型切到内置 Antigravity 模型而未传 `reasoning_effort` 时，服务端自动清空旧值。
- **执行与续跑继承（`automation_executor.py`、`automation_thread_model.py`）**：
  - 定时任务首轮执行在 `conversation_responses.metadata` 写入任务级强度配置，后续续跑、关联唤醒（经 `wakeups.created_by_run_id` 精确定位源执行，避免同对话多任务串台）、重试及订阅失败回退到备用模型时一致继承该强度（多档备用模型按自身阶梯裁剪，单档 Antigravity 模型保持自身档位）。
- **存量 Opus 定时任务一次性回填脚本（`scripts/dalton/ops/backfill_opus_automation_effort.py`）**：
  - **严格限定范围**：仅处理未删除（`deleted_at IS NULL`）、`llm_model` 严格等于 `claude-opus-5-5-oauth` 或 `claude-opus-5-5`、未被用户同名自定义模型遮蔽、`reasoning_effort IS NULL` 且 `audit_history` 中没有未回滚的 `backfill_opus_medium` 记录的任务（不处理 `llm_model IS NULL` 的账户默认任务，也不重复覆盖已被用户清空的任务）。
  - **用法**：
    ```bash
    # 1. 只读预演并导出计划清单（权限 0600）
    .venv/bin/python scripts/dalton/ops/backfill_opus_automation_effort.py \
      --dry-run --out var/dalton/operator-records/opus-effort-plan.json

    # 2. 按确认后的清单单事务写入（写前落盘归档至 var/dalton/operator-records/opus-effort-backfill-<batch_id>.json）
    .venv/bin/python scripts/dalton/ops/backfill_opus_automation_effort.py \
      --apply --plan var/dalton/operator-records/opus-effort-plan.json

    # 3. 按批次号安全回退（跳过回填后已被用户二次修改的行）
    .venv/bin/python scripts/dalton/ops/backfill_opus_automation_effort.py \
      --rollback <batch_id>
    ```
- **`d042` 降级与恢复规程**：将数据库从 `d042` 降级回 `d041` 会执行 `DROP COLUMN reasoning_effort`，影响所有显式设置过思考强度的自动化任务。降级前按顺序操作：
  1. 若已执行过 Opus 回填脚本且需撤销，先按上文 `--rollback <batch_id>` 回退回填值；
  2. 全量停写并完成备份后，从数据库导出所有 `reasoning_effort IS NOT NULL` 的任务记录（至少包含 `automation_id`、`config_version`、`llm_model`、`reasoning_effort`）保存到 `var/dalton/operator-records/d042-downgrade-export-<UTC时间戳>.json`（权限 `0600`）；
  3. 保留宿主机 `src/dalton/migrations/versions/d042_automation_reasoning_effort.py` 迁移文件，在维护容器中执行 `uv run alembic -c src/dalton/alembic.ini downgrade d041` 并核对 `alembic_version_dalton = d041`，再复位宿主机代码；
  4. 后续重新升级至 `d042` 后，逐条核对导出记录中的 `llm_model` 与 `config_version`，仅对降级期间模型与版本均未变动的任务写回原 `reasoning_effort`（降级期间被编辑过的任务保持 `NULL`，避免套用过时强度）。

## 4. 厂商级额度冷却

同一 Antigravity OAuth 账号下，同一上游厂商的内置模型共用一个额度池（`src/dalton/llm/antigravity_series.py:quota_cooldown_target` 与 `src/ptc_agent/agent/middleware/model_resilience.py`）：

- **厂商分组规则（`ANTIGRAVITY_VENDOR_PREFIXES`）**：
  - `gemini-` → `google`（Neon / Hydrogen / Argon / Carbon 共用）
  - `claude-` → `anthropic`（经 Antigravity 的 Opus / Sonnet 共用，与直连 Claude 订阅独立）
  - `gpt-` → `openai`（GPT-OSS 单独）
- **分立键空间**：
  - Antigravity 内置模型：`dalton:quota_exhausted:{cred_user}:antigravity-oauth:vendor:{vendor}`（存在 `cred_id` 时追加 `:{cred_id}`）
  - 其他提供商或用户自定义同名模型：`dalton:quota_exhausted:{cred_user}:{provider}:model:{model_id}`（存在 `cred_id` 时追加 `:{cred_id}`）
- **行为与团队共享语义**：
  - 任一内置模型触发 Antigravity 配额耗尽（HTTP 429）后，同一账号下同厂商其余模型直接命中冷却并返回带重置时间的“订阅额度耗尽”提示，不再发出必然失败的请求（手动对话不自动换模型，自动任务按 [automations.md](./automations.md) 订阅回退规则处理）。
  - 启用 `credentials.sharing: team` 时，全体成员共享 `DALTON_PM_USER_IDS[0]` 对应的账号冷却；个人凭据或不同 Google 账号间相互隔离。

## 5. 每周用量周报按系列合并

宿主机每周任务 `T61`（`weekly-token-usage`）在 `scripts/dalton/data_assets/skills/model-usage-cost/` 与 `scripts/dalton/data_assets/scripts/token_weekly_usage_tracker.py` 中实现按系列汇总：

1. **共享目录与离线快照**：`GET /api/v1/dalton/internal/usage/by-model` 在保持 `models` 原始带后缀口径不变的同时，于响应顶层输出 `antigravity_series`；`scripts/dalton/seed_openclaw_data.sh --apply` 同步将 `antigravity_series.py` 与导出的 `references/antigravity_series.json` 分发到 `var/dalton/openclaw-data/skills/model-usage-cost/`，供技能独立虚拟环境加载。
2. **计价与来源小计零漂移**：先按每条记录原始模型与日期计价，并保持本机与 Dalton 生产来源隔离计算 `local_bucket` / `dalton_bucket` 小计；再按稳定系列 ID 幂等合并展示行，并在每行下附各强度档位明细（未记录后缀的本机记录归入 `档位未知` 桶，各档明细加总与系列总计严格一致）。
