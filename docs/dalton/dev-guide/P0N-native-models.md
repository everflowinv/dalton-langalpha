# P0N 模型主路径切到 LangAlpha 原生供应商（broker v2 保留为并行路径）

版本 1.0，2026-10-18。写给接手开发的工程师。验收由 lead 负责。

本分册的代码现状基于 `dalton/main` 的 HEAD `7670a03f`。上游基线是 `8e0dce94`（`feat(auth): verify outside apps' OAuth access tokens…`，即合入 Dalton 之前最后一个上游提交）。

**和其他分册的关系：** 本分册修订 P0 的 P0.4–P0.8 的**默认值**，不推翻它们的设计。broker v2（P0.2–P0.3）、模型同步与同步按钮（P0.7）继续保留、继续可用，只是不再是默认模型路径。冲突时以本分册和 README §6 新增的决定为准（见 §0.3）。

---

## 0. 结论与路线（先读）

### 0.1 owner 已确认的方向（含 2026-10-18 的更新）

1. **默认模型路径改为 LangAlpha 原生供应商**：claude-oauth、codex-oauth（上游原生），antigravity-oauth、muse-oauth（owner 新加的订阅登录），xai（owner 新加，API key 现金计费），以及 providers.json 里已有的 API key 供应商（deepseek、gemini、z-ai、minimax、openrouter……）。
2. **broker v2 不退役。** 它继续安装在 OpenClaw 网关里，`ocb-*` 模型继续出现在模型目录里、可以被选为任何角色的模型；健康检查继续跑；OpenClaw "重新同步模型" 按钮继续可用，放进新的"模型供应商状态"卡片里，作为其中一节。现在**不做**任何卸载、删除动作。退役的前提条件见 §6，届时另行决定。
3. 模型目录的唯一来源 = **LangAlpha 上游 manifest + Dalton overlay**。owner 直接改上游 `models.json` / `providers.json` 的做法要改为 overlay，上游文件回到 `8e0dce94` 的原样。
4. 订阅登录（claude-oauth、codex-oauth、antigravity-oauth、muse-oauth）冒用官方 CLI 的身份，条款和封号风险比 OpenClaw 的 CLI 网关更高。owner 已知情并接受，缓解措施见 P0N.8 和 §5。

### 0.2 现状核对（读代码得到的事实，工程师不用重复调查）

| 事实 | 证据 | 影响 |
|---|---|---|
| owner 的三个提交直接改了上游 manifest：`3e9cc996`（antigravity，models.json +278 行、providers.json +13 行）、`9a03d3ac`（muse，+108 / +14）、`7670a03f`（xai，+39 / +120）。合计新增 23 个模型键（15 个 antigravity、5 个 muse、3 个 xai），3 个 `provider_config` 组（`antigravity`、`meta`、`xai`），1 个价格组 `models.xai` | `git diff 8e0dce94 HEAD -- src/llms/manifest/` | 违反 README §5"不改上游提交的默认配置文件" |
| `check_hooks.sh` 的比对基线写死成 `9a03d3ac`，**这个提交本身就是 owner 改 manifest 的提交**。所以 antigravity 和 muse 的改动被当成"基线"漏检了；只有 `7670a03f`（xai）会让 `--strict` 失败 | `scripts/dalton/check_hooks.sh` 第 125–129 行 | P0N.0 要把基线改为 `8e0dce94`；PROGRESS.md §3 里"相对基线 `9a03d3ac` 零改动"的结论不成立 |
| owner 的提交还改了这些上游文件，**都没有 `DALTON-HOOK` 标注**：`src/llms/llm.py`（两个新 SDK 分支 + `_get_muse_llm`、`_get_antigravity_llm` 两个方法，约 80 行）、`src/llms/extension/__init__.py`、`src/server/services/llm/clients.py`（OAuth 分派）、`src/server/app/oauth.py`（+222 行路由）、`tests/unit/llms/test_manifest_integrity.py`（+22 行豁免名单），以及前端 `ConnectedAccounts.tsx`（+564）、`OAuthConnect.tsx`（+218）、`Dashboard/utils/api.ts`（+88）、`useConfiguredProviders.ts`、`MethodStep.tsx`、`connectStep/shared.tsx`、`brandArt.ts`、`queryKeys.ts`（oauth 两个键）、两个 locale 文件 | `git diff --stat 8e0dce94 HEAD` | P0N.2、P0N.3 要把它们搬到 Dalton 目录，上游只留挂钩 |
| owner 新增的上游目录下的新文件：`src/llms/extension/antigravity.py`（630 行）、`src/server/services/antigravity_oauth.py`（422 行）、`src/server/services/muse_oauth.py`（230 行） | 同上 | 新文件不会和上游冲突，但放在上游目录里，以后合并上游时容易误判归属。P0N.2 迁到 `src/dalton/llm/providers/` |
| **muse 的会话粘性头从来没生效。** `get_llm` 里 `elif self.sdk == "muse": client = self._get_muse_llm(cache_key=effective_cache_key)`，而 `effective_cache_key` 只在 `sdk == "codex"` 或 `openai` 且开启 `prompt_cache_key` 时才有值，所以 muse 收到的永远是 `None`，`x-tbh-session-id` 头不会设置 | `src/llms/llm.py` 第 564–586 行 | P0N.2 的挂钩改为传原始 `cache_key`。这是一处行为变化，要在 P0N.4 的冒烟测试里确认没有副作用 |
| antigravity 的登录是 RFC 8252 loopback：后端进程在 `127.0.0.1:<随机端口>` 上开监听，Google 回跳到 `http://localhost:<端口>/callback`。**在 `docker-compose.dalton.yml` 的容器部署里，宿主机浏览器访问不到容器内的随机端口** | `src/server/services/antigravity_oauth.py` `start_login()` | 【待验证】owner 的端到端验证可能是在宿主机直接跑 `server.py` 做的。P0N.2 要么实测容器内可用，要么加"粘贴回跳地址"的兜底（同 claude-oauth 的 `code#state` 粘贴流程） |
| antigravity 的 usage 只读了 `promptTokenCount`、`candidatesTokenCount`、`thoughtsTokenCount`，**没有读缓存命中的 token 数**；`bind_tools` 丢弃了 `tool_choice` 等参数 | `src/llms/extension/antigravity.py` `_usage_metadata()`、`bind_tools()` | 缓存检查（N9）预期为 warn；结构化输出（N13）【待验证】，决定 antigravity 模型能否做服务端审稿 |
| claude-oauth 每次请求都在 system 最前面插入 `You are Claude Code, Anthropic's official CLI for Claude.` | `src/llms/extension/anthropic_oauth.py` 第 11、59–67 行 | N6 检查要允许这一行存在，但必须证明我们的 system prompt 仍然起作用 |
| 本地预算在 `is_byok=True` 时**整轮放行**（`LocalBudgetPort.acquire` 和 `enforce_local_admission` 都直接返回）。主模型走 OAuth 时整轮被视为 byok，所以**同一轮里的现金子智能体（grok-4.7）和 background（deepseek-flash）都不受日额度约束** | `src/dalton/budget/port.py` 第 66–73 行；`src/dalton/budget/gate.py` `enforce_local_admission` | 现在 lead 已经是 `claude-opus-5-5-oauth`，这个漏洞已经存在。P0N.6 必须修 |
| 预算只累加 `conversation_usages.total_credits`，它只包含 `billing_type=platform` 的调用费用。通过 Settings 按 BYOK 配置的现金 key（例如 xAI）**不计入**日额度 | `src/server/services/persistence/usage.py` 第 143–147 行；`src/dalton/database/budget.py` | P0N.6：现金 key 一律放 `.env`（platform 计费），结算 SQL 改为按单次调用的 `billing_type` 计算现金 |
| `LLMService.complete`（发布审稿 `review.py`、数字标注 `audit.py`）**不写** `conversation_usages`，它的花费目前对预算不可见 | `src/server/services/llm/service.py` 第 54–120 行 | P0N.7 的调用日志表同时承担记账 |
| antigravity 和 muse 的 20 个模型在 manifest 里没有价格（`_UNPRICED_KEYS` 豁免），`total_cost=0` | `tests/unit/llms/test_manifest_integrity.py` diff | P0N.6 用 `notional_ref` 把它们映射到同一底层模型的公开价，用于名义成本 |
| `dalton-evidence.md` 没有 `model:` 字段，**会继承主模型**（现在是 Claude 订阅）；`dalton-reviewer.md` 写死了 `model: ocb-openai-gpt-6.1-sol` | `plugins/dalton/agents/*.md` | P0N.5 修正 |
| `ocb-` 键出现在这些默认值里：`agent_config.dalton.yaml`（background、三个子智能体）、`src/dalton/dalton.yaml` 和 `src/dalton/settings.py`（`publish.reviewer_models` 前三项）、`plugins/dalton/agents/dalton-reviewer.md`、`scripts/dalton/openclaw_smoke.py`（`--langalpha-key` 默认值）、`tests/unit/dalton/test_budget_port.py` | `git grep -ln "ocb-"` | P0N.5 只改默认值；`ocb-*` 模型本身保留在目录里 |
| researchdata 桥直接用子进程跑 `~/.openclaw/workspace/scripts/researchdata_mcp.py`，**不经过 OpenClaw 网关**（`127.0.0.1:18789`）。`data_gateway.py` 只直连 AlphaEngine MCP（`127.0.0.1:8950`）和 Guidepoint 代理（`127.0.0.1:8943`），`config/data_sources.json` 里没有 18789 | `scripts/dalton/researchdata_bridge.py` 第 90–110 行；`~/.openclaw/workspace/scripts/data_gateway.py` 第 20 行 | 切换后 LangAlpha 的模型调用不再需要网关；数据侧仍依赖 OpenClaw 的工作区文件和宿主机服务（P0N.11） |

### 0.3 新增与修订的决定（P0N.11 写进 README §6）

| 编号 | 决定 |
|---|---|
| O3（修订，owner 2026-10-18） | 模型列表以 LangAlpha manifest 加 Dalton overlay 为准。默认走原生供应商；broker v2 保留，作为并行、可选的路径 |
| L1（修订） | 主模型可切换。默认 `claude-opus-5-5-oauth`；备选 codex-oauth 的 GPT、xai 的 `grok-4.7`（现金）、antigravity 托管的 Claude 或 Gemini，以及 `ocb-*` |
| L2（收窄） | "CLI 网关模型只做纯文本角色" 只适用于 broker 路径的 `*-cli-gateway/*`。原生 antigravity、muse 支持工具调用，以 P0N.4 冒烟结果为准 |
| L17 | 每个供应商都有开关（`dalton.yaml` 的 `models.disabled_providers`，或环境变量 `DALTON_DISABLED_PROVIDERS`）。被关掉的供应商在任何路径上都直接报错，不静默换模型 |
| L18 | 服务端一次性长调用（`LLMService.complete`）走 Dalton 调用日志：可回放，崩溃后不确定的调用不自动重发。智能体流式轮次不做调用级幂等，罕见重试造成的重复花费可以接受 |
| L19 | 现金模型的 key 只放 `.env`（平台计费）。本地预算按单次调用的 `billing_type` 计现金：`oauth` 为 0（只记名义成本），其余都算现金。OAuth 主模型的轮次不再整轮放行 |
| L20 | 审稿家族按**底层模型**判断，与供应商无关：antigravity 托管的 Claude 算 `anthropic-claude`；GPT-OSS 算 `openai-gpt`（同一厂商的开放权重模型，审稿独立性按保守口径处理） |
| L21 | muse 的 contributor 变体（`*-contributor-oauth`，按目录说明内容可能被用于训练）默认隐藏，并且禁止用于任何 Dalton 角色。基金的研究材料（专家访谈、Guidepoint）不能进入这类模型 |

### 0.4 迁移顺序（每一步结束时系统都能正常运行）

```
P0N.0 基线修正与审计（只读，不改行为）
  └─ P0N.1 overlay 扩展：多来源 + provider/variant 合并 + dalton_meta（行为不变，native_overlay.json 先为空）
       └─ P0N.2 后端迁移：owner 的供应商代码搬进 src/dalton/llm/providers/，manifest 条目搬进 native_overlay.json，
       │        上游文件还原；"有效目录前后一致"是硬性验收
       │    └─ P0N.3 前端迁移：登录 UI 抽到 Dalton 目录，上游只留挂钩
       └─ P0N.4 通用冒烟 model_smoke.py，跑完整矩阵（必须在切默认值之前）
            └─ P0N.5 角色默认值切到原生模型 + family 映射 + 审稿人
                 ├─ P0N.6 预算：现金/名义口径、修 OAuth 整轮放行
                 ├─ P0N.7 LLMService 调用日志 d007（幂等 + 记账）
                 └─ P0N.8 供应商开关 + 供应商状态后端接口
                      └─ P0N.9 设置页"模型供应商状态"卡片（内含 OpenClaw broker 一节）
P0N.10 双登录并发测试（P0N.4 之后任何时候都可以做，P0N.12 之前必须完成）
P0N.11 OpenClaw 依赖清单 + 文档与决定记录更新
P0N.12 P0N 验收（lead 照 §4 的清单跑）
```

P0N.2 是唯一有"改完之后不能用"风险的一步，所以它必须一个 PR 合入、合入前跑完"有效目录前后一致"的检查。P0N.5 之前，默认值一直是现在的值（混用 `claude-opus-5-5-oauth` 和 `ocb-*`），系统照常运行。

### 0.5 回滚

broker 一直在运行，所以回滚只改配置，不动 OpenClaw：

1. **默认模型回滚**（P0N.5 之后出问题）：把 `agent_config.dalton.yaml`、`src/dalton/dalton.yaml` 的 `publish.reviewer_models`、两个子智能体 `.md` 的 `model:` 恢复到 P0N.5 之前的版本（`git show <P0N.5 之前的提交>:agent_config.dalton.yaml > agent_config.dalton.yaml`，其余同理），然后 `docker compose restart backend`。按用户的设置（Settings 里选的默认模型）需要用户自己在界面上切回 `ocb-*`。
2. **单个供应商出问题**（被限流、被封、token 失效）：把它加进 `DALTON_DISABLED_PROVIDERS`，重启 backend；用到它的角色在 P0N.9 的卡片上显示红色告警，按 L1 切到替补。
3. **P0N.2 迁移本身出问题**：`git revert` P0N.2 的合并提交，回到 owner 直接改上游文件的版本，功能等价。
4. **broker 开关**：`dalton.yaml` 的 `models.broker.enabled` 默认 `true`，回滚时不需要动。只有在 broker 本身出故障、想让 `ocb-*` 从目录里消失时才设成 `false`。

---

## P0N.0 基线修正与审计

**目标：** 让 `check_hooks.sh` 以真正的上游基线 `8e0dce94` 做比对，并能列出"改了上游文件却没有挂钩"的情况。本步只报告，不改任何运行行为。

**前置依赖：** 无。

**改动/新增文件：**
- `scripts/dalton/check_hooks.sh`：把基线改为 `8e0dce94`（仍允许 `DALTON_BASE_REF` 覆盖）；新增 `--audit` 模式；`ALLOWED_HOOKS` 增加 `llm-sdk-dispatch`、`oauth-dispatch`、`provider-kill-switch`、`native-oauth-ui`（在 P0N.2、P0N.3 落地）。
- `scripts/dalton/dalton_paths.txt`【新建】：Dalton 自有路径清单，供 `--audit` 判断哪些文件是上游文件。

**具体做法：**

1. 基线：
   ```bash
   BASE_REF="${DALTON_BASE_REF:-8e0dce94}"
   ```
   以后每次合并上游，lead 把基线更新为合并进来的上游提交（在 PR 描述里写明）。
2. `--audit`：对 `git diff --name-only "$BASE_REF"...HEAD` 里每个**不在** `dalton_paths.txt` 前缀下的文件：
   - 如果是新文件，列为 `NEW_IN_UPSTREAM_DIR`（提醒，不报错；例外清单写在 `dalton_paths.txt` 里，例如 `src/ptc_agent/agent/middleware/skills/dalton_registry.py`、`web/src/pages/Settings/panels/dalton/`、`web/src/pages/Settings/hooks/useOpenClaw.ts`、`web/src/pages/Settings/utils/daltonApi.ts`、`web/src/pages/ChatAgent/components/dalton/`）；
   - 如果是修改，检查 diff 的每个新增行块附近（±3 行）是否有 `DALTON-HOOK(`，没有就列为 `UNHOOKED_EDIT`。
   `dalton_paths.txt` 的初始内容：
   ```
   plugins/dalton/
   plugins/dalton_data/
   src/dalton/
   workflows/
   scripts/dalton/
   config/dalton/
   docs/dalton/
   integrations/openclaw/
   tests/unit/dalton/
   agent_config.dalton.yaml
   docker-compose.dalton.yml
   ```
3. P0N.2 和 P0N.3 合入后，把 `UNHOOKED_EDIT` 计入 `--strict` 的错误。在那之前 `--strict` 只保留原有检查加新基线，所以本步合入后 `--strict` **会**失败（因为 xai 和其他 owner 改动）。这是预期的，P0N.2 修复；在 PR 描述里写明。

**验证方式：**
```bash
./scripts/dalton/check_hooks.sh --audit | tee var/dalton/hook_audit_before.txt
```
输出里要能看到 §0.2 表中列出的全部上游文件。

**验收标准：**
- `--audit` 的输出至少包含：两个 manifest、`src/llms/llm.py` 的未标注新增块、`src/llms/extension/__init__.py`、`src/server/services/llm/clients.py`、`src/server/app/oauth.py`、`tests/unit/llms/test_manifest_integrity.py`，以及 §0.2 列出的前端文件；
- 三个 `NEW_IN_UPSTREAM_DIR`：`antigravity.py`、`antigravity_oauth.py`、`muse_oauth.py`；
- `var/dalton/hook_audit_before.txt` 附在 PR 里。

**风险与注意：** `--audit` 用行块邻近判断，可能误报（例如挂钩注释写在块上方 4 行）。误报时调整挂钩位置，不要放宽规则。

**预估工时：** 0.3 天。

---

## P0N.1 overlay 扩展：多来源合并、provider/variant 合并、`dalton_meta`

**目标：** 让 Dalton overlay 能承载 owner 新增的全部供应商定义（模型键、provider 组、variant、价格），并能和 OpenClaw 同步生成的 overlay 共存。本步结束时 `native_overlay.json` 为空，行为与之前完全一致。

**前置依赖：** P0N.0。

**改动/新增文件：**
- `src/dalton/llm/overlay.py`：改为多来源合并；stamp 改为组合 stamp。
- `src/dalton/llm/native_overlay.json`【新建】：人工维护、提交入库的原生供应商 overlay（P0N.2 填内容）。
- `src/dalton/settings.py`：新增 `DaltonModelsConfig`（`broker.enabled`、`disabled_providers`），挂到 `DaltonConfig.models`。
- `src/dalton/dalton.yaml`：新增 `models:` 段。
- `scripts/dalton/dump_effective_catalog.py`【新建】：输出合并后的有效目录，用于 P0N.2 的前后对比。
- 上游 `src/llms/llm.py`：**不需要新挂钩**。现有 `DALTON-HOOK(llm-overlay)` 两处调用的函数名不变（`apply_model_overlay`、`overlay_changed`），签名不变。

**具体做法：**

1. 来源与顺序（后者不能覆盖前者已有的键，只能新增；冲突时记 error 日志并跳过）：
   1. 上游 `models.json` / `providers.json`（原样读取）；
   2. `src/dalton/llm/native_overlay.json`（人工维护，入库）；
   3. `src/dalton/llm/openclaw_overlay.json`（同步按钮生成；`models.broker.enabled=false` 时跳过）。

2. `native_overlay.json` 的结构（与 `openclaw_overlay.json` 同形，多一个 `dalton_meta`）：
   ```json
   {
     "schema": "dalton-native-overlay/1",
     "models": {
       "grok-4.7": { "model_id": "grok-4.7", "provider": "xai", "visible": true, "...": "从 7670a03f 原样搬入" }
     },
     "providers": {
       "provider_config": {
         "xai": { "sdk": "openai", "base_url": "https://api.x.ai/v1", "env_key": "XAI_API_KEY", "...": "..." },
         "antigravity": { "sdk": "antigravity", "variants": { "antigravity-oauth": { "...": "..." } } }
       },
       "provider_variants": {
         "<已有的上游组名>": { "<新 variant 名>": { "...": "..." } }
       },
       "models": {
         "xai": [ { "id": "grok-4.7", "pricing": { "...": "..." } } ]
       }
     },
     "dalton_meta": {
       "claude-opus-5-5-high-oauth": { "family": "anthropic-claude", "billing": "subscription", "notional_ref": "claude-opus-5-5", "tool_calling": true },
       "grok-4.7": { "family": "xai-grok", "billing": "cash", "tool_calling": true }
     }
   }
   ```
   - `providers.provider_config`：只允许**新的组名**。和上游已有的组同名时报错跳过（这是现在 `overlay.py` 没做的检查：它会直接整组覆盖）。
   - `providers.provider_variants`【新增能力】：给上游**已有的组**追加新 variant（例如以后想在 `anthropic` 下加一个 Dalton 自己的 variant）。只允许新增 variant 键，已有 variant 不能改。合并发生在 `_flatten_providers` 之前（现有挂钩的位置已经满足：`apply_model_overlay` 在 `ModelConfig.__init__` 里先于 `_flatten_providers` 调用）。owner 现在的三个供应商都是新组，暂时用不到这一项，但 D2 要求具备。
   - `providers.models`：新的价格组直接加入；已有的价格组（例如 `gemini`）只追加 `id` 不重复的条目。
   - `dalton_meta`：**不合并进 manifest**，只给 Dalton 自己读（`role_policy`、预算、状态卡片）。键可以是任何模型键，包括上游的键（例如 `claude-opus-5-5-oauth`、`deepseek-flash`），这样不需要改上游就能给上游模型标注家族、计费类型、是否支持工具。

3. `overlay.py` 的接口：
   ```python
   OVERLAY_SOURCES: list[tuple[str, Path]]  # [("native", NATIVE_OVERLAY_PATH), ("openclaw", OVERLAY_PATH)]

   def current_overlay_stamp(*, force: bool = False) -> str | None:
       """'native:<mtime_ns>:<size>|openclaw:<mtime_ns>:<size>'，缺的文件写 '-'；仍然每 2 秒最多 stat 一次。"""

   def overlay_changed(previous_stamp: str | None, path: Path | None = None) -> bool: ...   # 签名不变

   def apply_model_overlay(llm_config: dict, manifest: dict, path: Path | None = None) -> str | None:
       """按 §1 的顺序合并全部来源；path 参数只给单测用。返回组合 stamp。"""

   def load_overlay_data(path: Path | None = None) -> dict | None: ...   # 不变，默认仍读 openclaw_overlay.json（同步按钮和 role_policy 的旧调用方继续可用）

   def load_dalton_meta() -> dict[str, dict]:
       """合并 native_overlay.json 的 dalton_meta 和 openclaw_overlay.json 里每个 ocb 条目的 dalton 字段，键为模型键。"""
   ```
   注意多 worker 约定：stamp 缓存只是性能优化，不是状态真相；每个 worker 各自 stat 文件，结果一致。

4. `dalton.yaml` 新增：
   ```yaml
   models:
     broker:
       enabled: true            # OpenClaw broker v2 并行路径；false 时 ocb-* 从目录消失、健康检查停止
     disabled_providers: []     # 供应商开关（P0N.8）。例：["muse-oauth"]
   ```

5. `dump_effective_catalog.py`：在 backend 容器里构造 `ModelConfig()`，输出 `{"llm_config": ..., "flat_providers": ..., "pricing_models": manifest["models"]}`，键排序，`--exclude-prefix ocb-` 可选。

**验证方式：**
```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml exec backend \
  uv run python scripts/dalton/dump_effective_catalog.py > var/dalton/catalog_before_p0n1.json
# 合入本步代码、native_overlay.json 为空 {} 后再跑一次
diff <(jq -S . var/dalton/catalog_before_p0n1.json) <(jq -S . var/dalton/catalog_after_p0n1.json)   # 必须为空
```
再手动做一次冲突演练：在 `native_overlay.json` 里放一个 `claude-opus-5-5` 键、一个 `anthropic` 组，启动后日志各有一条 error，目录里的上游条目不变。演练完删除。

**验收标准：**
- 空 overlay 时前后目录 diff 为空；
- 冲突演练两条 error 日志都出现，且上游条目没有被覆盖；
- 点一次 OpenClaw 同步按钮，`/api/v1/models` 里的 `ocb-*` 仍然能刷新（组合 stamp 起作用）；
- `models.broker.enabled=false` 时 `ocb-*` 消失，改回 `true` 后恢复。

**风险与注意：** 目前 `apply_model_overlay` 对 `provider_config` 是整组覆盖，对 `providers.models` 是整列表替换。改成"只新增"之后，如果 `openclaw_overlay.json` 里有和上游同名的价格组（例如 `gemini`），以前会把上游的价格列表整个换掉。合入前先 `jq '.providers.models | keys' src/dalton/llm/openclaw_overlay.json` 确认只有 `openclaw-broker`。

**预估工时：** 1 天。

---

## P0N.2 后端迁移：owner 的供应商搬进 Dalton 目录，上游文件还原

**目标：** 上游 manifest、`src/llms/extension/__init__.py`、`src/server/app/oauth.py`、`tests/unit/llms/test_manifest_integrity.py` 回到 `8e0dce94` 的原样；`src/llms/llm.py`、`src/server/services/llm/clients.py` 只保留带 `DALTON-HOOK` 的几行挂钩；owner 的供应商代码成为 Dalton 自有文件。**迁移前后有效目录完全一致，三个供应商的真实调用照常工作。**

**前置依赖：** P0N.1。

**改动/新增文件：**

| 动作 | 源 | 目标 |
|---|---|---|
| `git mv` | `src/llms/extension/antigravity.py` | `src/dalton/llm/providers/antigravity/client.py`【新建】 |
| `git mv` | `src/server/services/antigravity_oauth.py` | `src/dalton/llm/providers/antigravity/oauth.py`【新建】 |
| `git mv` | `src/server/services/muse_oauth.py` | `src/dalton/llm/providers/muse/oauth.py`【新建】 |
| 剪切 | `src/llms/llm.py` 的 `_get_muse_llm` 方法体 | `src/dalton/llm/providers/muse/client.py`【新建】 `build_muse_client(llm, cache_key)` |
| 剪切 | `src/llms/llm.py` 的 `_get_antigravity_llm` 方法体 | `src/dalton/llm/providers/antigravity/__init__.py`【新建】 `build_antigravity_client(llm)` |
| 剪切 | `src/server/app/oauth.py` 中 antigravity、muse 的 8 个路由（+ 头部 docstring 和 import） | `src/dalton/app/oauth_native.py`【新建】，`APIRouter(prefix="/api/v1/oauth")`，路径不变 |
| 剪切 | `models.json` 的 23 个键；`providers.json` 的 `provider_config.{antigravity,meta,xai}` 和 `models.xai` | `src/dalton/llm/native_overlay.json` |
| 新建 | — | `src/dalton/llm/providers/__init__.py`【新建】：注册表（SDK 构造器、OAuth token 取值、开关） |
| 新建 | — | `src/dalton/llm/providers/muse/__init__.py`【新建】 |
| 还原 | `src/llms/extension/__init__.py`、`src/server/app/oauth.py`、`src/llms/manifest/models.json`、`src/llms/manifest/providers.json`、`tests/unit/llms/test_manifest_integrity.py` | `git checkout 8e0dce94 -- <这些文件>` |
| 改 | `src/dalton/app/__init__.py` | 导出的 `dalton_router` 改为一个不带前缀的父路由，同时包含 `router`（`/api/v1/dalton`）和 `oauth_native.router`（`/api/v1/oauth`）。上游 `setup.py` 的 `DALTON-HOOK(router)` 两行不用动 |
| 挂钩 | `src/llms/llm.py` | `DALTON-HOOK(llm-sdk-dispatch)`、`DALTON-HOOK(provider-kill-switch)`（见下） |
| 挂钩 | `src/server/services/llm/clients.py` | `DALTON-HOOK(oauth-dispatch)`（见下） |
| 新建 | — | `tests/unit/dalton/test_native_overlay.py`【新建】：替代被还原的 `_UNPRICED_KEYS` 豁免（见第 6 点） |

**为什么放在 `src/dalton/llm/providers/`（而不是留在 `src/llms/extension/` 和 `src/server/services/`）：**
- README §5 规定 Dalton 代码只放 Dalton 目录。留在上游目录，每次合并上游都要人工判断归属；`check_hooks --audit` 也会一直报 `NEW_IN_UPSTREAM_DIR`；
- 改动量小：三个文件 `git mv`（保留历史），只需要改它们的 import 路径和少数几个调用方（`oauth_native.py`、`clients.py` 挂钩、注册表）；
- 上游的 `ChatCodexOpenAI`（muse 依赖它）、`oauth_tokens`、`oauth_refresh_wait`、`invalidate_oauth_active_cache` 仍然从上游 import，不复制；
- 将来上游自己加了 antigravity 或 muse 的支持，Dalton 这边删掉注册表里的一项、删掉 overlay 条目即可。

**具体做法：**

1. **注册表** `src/dalton/llm/providers/__init__.py`：
   ```python
   """Dalton-owned native LLM providers: SDK builders, OAuth token getters, kill switch (P0N.2, P0N.8)."""

   from typing import Any, Awaitable, Callable

   SdkBuilder = Callable[[Any, str | None], Any]          # (LLM 实例, 原始 cache_key) -> LangChain chat model
   TokenGetter = Callable[[str], Awaitable[dict | None]]   # user_id -> {"access_token", "plan_type", ...}

   def _sdk_builders() -> dict[str, SdkBuilder]:
       from .antigravity import build_antigravity_client
       from .muse.client import build_muse_client
       return {"antigravity": lambda llm, ck: build_antigravity_client(llm),
               "muse": build_muse_client}

   def _oauth_getters() -> dict[str, TokenGetter]:
       from .antigravity.oauth import get_valid_token as ag
       from .muse.oauth import get_valid_token as mu
       return {"antigravity-oauth": ag, "muse-oauth": mu}

   def build_dalton_client(llm: Any, *, cache_key: str | None) -> Any | None:
       builder = _sdk_builders().get(llm.sdk)
       return builder(llm, cache_key) if builder else None

   def dalton_oauth_token_getter(provider: str) -> TokenGetter | None:
       return _oauth_getters().get(provider)

   def is_dalton_oauth_provider(provider: str) -> bool:
       return provider in ("antigravity-oauth", "muse-oauth")

   def ensure_provider_enabled(provider: str) -> None:
       """P0N.8：被关掉的供应商直接抛 ProviderDisabledError，消息里写供应商名和开关位置。"""
   ```
   用函数内 import，避免 `src.llms.llm` 导入时出现循环依赖。注册表本身没有模块级可变状态。

2. **`llm.py` 挂钩**（删掉 owner 加的两个 `elif` 分支和两个方法，替换为下面几行）：
   ```python
       def get_llm(self, cache_key: str | None = None):
           ...
           # DALTON-HOOK(provider-kill-switch): refuse providers disabled in Dalton config (L17)
           from src.dalton.llm.providers import ensure_provider_enabled
           ensure_provider_enabled(self.provider)
           effective_cache_key: str | None = None
           ...
           elif self.sdk == "gemini":
               client = self._get_gemini_llm()
           else:
               # DALTON-HOOK(llm-sdk-dispatch): Dalton-owned native SDKs (antigravity, muse)
               from src.dalton.llm.providers import build_dalton_client
               client = build_dalton_client(self, cache_key=cache_key)
               if client is None:
                   raise ValueError(f"Unsupported SDK: {self.sdk} for provider {self.provider}")
   ```
   - 注意 `build_dalton_client` 收到的是**原始** `cache_key`，修复 §0.2 的 muse 会话头问题。
   - `build_muse_client(llm, cache_key)` 是 `_get_muse_llm` 原样搬过去，把 `self.` 换成 `llm.`。它用到 `llm._resolve_api_key()`、`llm._resolve_base_url()`、`llm.parameters`、`llm.use_response_api`、`llm.default_headers`、`llm.extra_body`，以及模块级的 `_merged_default_headers`、`STREAM_CHUNK_TIMEOUT_S`（从 `src.llms.llm` import）。这些是上游的私有成员，上游改名时这里会坏——所以 `test_native_overlay.py` 里加一个只构造、不发请求的测试锁住它（这是"锁定稳定契约"，符合测试原则）。
   - `ensure_provider_enabled` 放在 `get_llm` 开头，因为所有路径（对话、子智能体、background、`LLMService.complete`、OAuth、BYOK、平台 key）最终都经过这里。

3. **`clients.py` 挂钩**（`resolve_oauth_llm_client` 内两处，共约 5 行）：
   ```python
       # DALTON-HOOK(oauth-dispatch): token getters for Dalton-owned OAuth providers
       from src.dalton.llm.providers import dalton_oauth_token_getter, is_dalton_oauth_provider
       get_valid_token = dalton_oauth_token_getter(provider)
       if get_valid_token is not None:
           pass
       elif provider == "claude-oauth":          # 上游原来的 if 改成 elif，其余不变
           from src.server.services.claude_oauth import get_valid_token
       else:
           from src.server.services.codex_oauth import get_valid_token
       ...
       headers = {}
       if provider == "claude-oauth":
           logger.debug(...)
       elif is_dalton_oauth_provider(provider):  # DALTON-HOOK(oauth-dispatch): bearer rides api_key
           pass
       else:
           # Codex: set ChatGPT-Account-Id header
   ```
   第二处不能省：antigravity 的 `account_id` 存的是 Google 的 `sub`（非空，见 `antigravity_oauth.py` 第 312 行），如果落到 Codex 分支，会给 Google 的请求加上 `ChatGPT-Account-Id` 头。

4. **路由迁移**：`src/dalton/app/oauth_native.py` 的 8 个路由路径和请求/响应体与现在完全一致（`/api/v1/oauth/antigravity/{initiate,poll,status}`、`DELETE /api/v1/oauth/antigravity`、`/api/v1/oauth/muse/device/{initiate,poll}`、`/api/v1/oauth/muse/status`、`DELETE /api/v1/oauth/muse`），前端不用改。确认 FastAPI 没有重复注册（上游 `oauth.py` 还原后不再有这些路径）。

5. **manifest 条目迁移**：写一个一次性脚本（放在 PR 里，不入库；或者放 `scripts/dalton/` 下并在 PR 合入后删除），从 `git show 7670a03f:src/llms/manifest/{models,providers}.json` 减去 `git show 8e0dce94:...`，得到差集写入 `native_overlay.json`。**不要手抄。** 同时生成 `dalton_meta`：

   | 模型键（前缀） | family | billing | notional_ref | tool_calling |
   |---|---|---|---|---|
   | `gemini-4-argon-{low,medium,high}-oauth` | `google-gemini` | subscription | `gemini-4-argon` | true【P0N.4 确认】 |
   | `gemini-3.8-flash-{low,medium,high}-oauth` | `google-gemini` | subscription | `gemini-3.8-flash` | true【同上】 |
   | `gemini-2.5-pro-oauth`、`gemini-3-flash-oauth` | `google-gemini` | subscription | `gemini-3.1-pro-preview`、`gemini-3.8-flash`（上游没有同名价卡，取同档）【待验证：上游是否有更贴近的价卡】 | true【同上】 |
   | `claude-opus-5-5-{low,medium,high}-oauth` | `anthropic-claude` | subscription | `claude-opus-5-5` | true【同上】 |
   | `claude-sonnet-5-5-{low,medium,high}-oauth` | `anthropic-claude` | subscription | `claude-sonnet-5-5` | true【同上】 |
   | `gpt-oss-120b-medium-oauth` | `openai-gpt`（L20） | subscription | `gpt-oss-120b-groq` | 【P0N.4 确认】 |
   | `muse-wonder-full-1.0-oauth`、`muse-wonder-1.0-oauth`、`muse-spark-1.3-oauth` | `meta-muse` | subscription | 无公开价，`notional_pricing` 按 grok-4.7 的价格估（标注"估"）【待 owner 确认】 | true【同上】 |
   | `*-contributor-oauth`（2 个） | `meta-muse` | subscription | 同上 | 同上；**`visible:false`，并在 `dalton_meta` 里加 `"forbidden_for_roles": true`**（L21） |
   | `grok-4.7`、`grok-build-0.1`、`grok-4.3` | `xai-grok` | cash | — | true【同上】 |
   | 上游键：`claude-*-oauth`、`claude-*` | `anthropic-claude` | `-oauth` 为 subscription，其余 cash | `-oauth` 的取同名去后缀 | true |
   | 上游键：`gpt-*-oauth`、`gpt-*` | `openai-gpt` | 同上 | 同上 | true |
   | 上游键：`deepseek-flash`、`glm-*`、`minimax-m3`、`qwen*`、`kimi-*` | `deepseek`、`zai-glm`、`minimax`、`alibaba-qwen`、`moonshot-kimi` | cash | — | true【同上】 |

   contributor 变体在迁移时改成 `visible:false`，这是迁移中**唯一**允许的语义变化，要在 PR 描述里单独写明；"有效目录前后一致"的比对要把这两个键的 `visible` 字段排除。

6. **测试**：上游的 `test_manifest_integrity.py` 还原后只校验上游 manifest，会通过。新的 `tests/unit/dalton/test_native_overlay.py` 锁定：
   - overlay 的每个模型键都能在合并后的 `flat_providers` 里找到 provider，且 provider 有 `sdk`；
   - 每个模型要么有价格条目，要么在 `dalton_meta` 里 `billing == "subscription"` 且有 `notional_ref` 或 `notional_pricing`；
   - overlay 键与上游键不重复；
   - `build_muse_client` 和 `build_antigravity_client` 能用假 token 构造出对象（不发请求）。

7. **antigravity 在容器里的登录**【待验证】：在 `docker-compose.dalton.yml` 的部署下，从宿主机浏览器完成一次 antigravity 登录。
   - 如果 Google 回跳到 `localhost:<随机端口>` 失败：在 `oauth_native.py` 增加 `POST /api/v1/oauth/antigravity/callback`，接收用户从浏览器地址栏粘贴的完整回跳地址，从 Redis 取 `verifier`/`state` 完成换 token（复用 `oauth.py` 里 `_handle_google_callback` 的解析逻辑）。前端在 P0N.3 加一个粘贴框。不要为此在 compose 里开放端口范围。
   - 多 worker：loopback 监听只存在于处理 initiate 的那个 worker 进程里，但完成状态写在 Postgres（token 行）和 Redis，poll 落到任何 worker 都能读到，符合约定。粘贴兜底同样只依赖 Redis 和 Postgres。

**验证方式（真实调用）：**
1. 迁移前在 `7670a03f` 上跑 `dump_effective_catalog.py > var/dalton/catalog_7670a03f.json`；迁移后再跑一次，`jq -S` 后 diff，只允许两个 contributor 键的 `visible` 不同。
2. `git diff 8e0dce94 -- src/llms/manifest/ src/llms/extension/__init__.py src/server/app/oauth.py tests/unit/llms/test_manifest_integrity.py` 为空。
3. `git diff 8e0dce94 -- src/llms/llm.py src/server/services/llm/clients.py` 只剩带 `DALTON-HOOK` 的块（原有的 `llm-overlay`、`llm-broker-client` 加本步新增的三个 ID）。
4. 容器内用已登录的账号各跑一次（P0N.4 的脚本还没好时，用一段 `create_llm` / `resolve_oauth_llm_client` 的临时脚本即可）：`gemini-3.8-flash-low-oauth` 普通对话 + 工具往返；`muse-spark-1.3-oauth` 流式；`grok-4.7` 普通对话（`.env` 里有 `XAI_API_KEY`）。
5. muse 会话头：在 `build_muse_client` 构造出的对象上检查 `default_headers` 里有 `x-tbh-session-id`（传入 `cache_key` 时）。
6. `./scripts/dalton/check_hooks.sh --audit`：三个 `NEW_IN_UPSTREAM_DIR` 消失，后端相关的 `UNHOOKED_EDIT` 清零（前端的留给 P0N.3）。
7. `uv run pytest tests/unit/llms/ tests/unit/dalton/ -q` 全绿。

**验收标准：**
- 第 1、2、3、6 条的输出附在 PR 里，lead 逐条复核；
- 第 4 条三个供应商的真实调用记录（回复片段、usage、耗时；不含 token）；
- antigravity 容器内登录的结论（可以直接用，或者已加粘贴兜底并实测成功）；
- 断开、重新连接 antigravity 和 muse 各一次，前端行为与迁移前相同。

**风险与注意：**
- 这一步必须一个 PR 合入。如果拆成"先还原上游、再加 overlay"，中间状态会丢模型，正在用 antigravity 的线程会报错。
- `git mv` 之后，Python 的旧 import 路径（`src.llms.extension.ChatAntigravity`、`src.server.services.antigravity_oauth`）不再存在。`git grep -n "antigravity_oauth\|muse_oauth\|ChatAntigravity\|AntigravityApiError"` 清理全部调用方。
- `antigravity/oauth.py` 里有 agy CLI 的 OAuth client id 和 client secret 常量。这是安装版应用的公开客户端标识，不是我们的凭据，但仍然不要打印到日志、不要写进 PR 描述正文。
- 迁移不改变任何模型键名。用户在 Settings 里已经选过的模型（例如 `gemini-3.8-flash-low-oauth`）继续有效。

**预估工时：** 1.5 天（含 antigravity 容器登录的验证；如果需要加粘贴兜底，另加 0.5 天）。

---

## P0N.3 前端迁移：原生登录 UI 抽到 Dalton 目录

**目标：** owner 在上游前端文件里加的 antigravity、muse 登录代码（约 1,000 行）移到 Dalton 目录，上游文件只剩带 `// DALTON-HOOK(native-oauth-ui)` 的几行挂钩。界面行为不变。

**前置依赖：** P0N.2（路由路径不变，所以可以和 P0N.2 并行开发，但在它之后合入）。

**改动/新增文件：**
- `web/src/pages/Settings/utils/daltonOAuthApi.ts`【新建】：从 `web/src/pages/Dashboard/utils/api.ts` 剪切 8 个函数（`initiateAntigravityOAuth`、`pollAntigravityOAuth`、`getAntigravityOAuthStatus`、`disconnectAntigravityOAuth`、`initiateMuseDevice`、`pollMuseDevice`、`getMuseOAuthStatus`、`disconnectMuseOAuth`），P0N.2 如加了粘贴兜底，再加 `submitAntigravityCallback`。
- `web/src/pages/Settings/panels/dalton/oauth/AntigravityAccountRow.tsx`【新建】、`MuseAccountRow.tsx`【新建】：从 `ConnectedAccounts.tsx` 剪切两段卡片（状态、免责声明、连接中状态、轮询）。
- `web/src/pages/Setup/steps/connectStep/dalton/AntigravityConnect.tsx`【新建】、`MuseConnect.tsx`【新建】：从 `OAuthConnect.tsx` 剪切。
- `web/src/pages/Settings/hooks/useDaltonOAuthProviders.ts`【新建】：从 `useConfiguredProviders.ts` 剪切两个 `useQuery` 和两段 `result.push`，导出 `useDaltonOAuthProviders(): { providers: ConfiguredProvider[]; isLoading: boolean }`。
- 上游挂钩文件（每处 1–4 行）：`web/src/pages/Settings/panels/ConnectedAccounts.tsx`、`web/src/pages/Setup/steps/connectStep/OAuthConnect.tsx`、`web/src/hooks/useConfiguredProviders.ts`、`web/src/pages/Setup/steps/MethodStep.tsx`、`web/src/pages/Setup/steps/connectStep/shared.tsx`、`web/src/lib/brandArt.ts`、`web/src/lib/queryKeys.ts`（`oauth.antigravity`、`oauth.muse` 两个键，标 `DALTON-HOOK(query-keys)`）。
- 还原：`web/src/pages/Dashboard/utils/api.ts`（`git checkout 8e0dce94 --`）。
- locale：owner 加的键挪到 `dalton.oauth.*` 命名空间下（沿用已登记的 `DALTON-HOOK(i18n)`）。

**具体做法：**
1. 挂钩写法示例（`useConfiguredProviders.ts`）：
   ```ts
   import { useDaltonOAuthProviders } from '@/pages/Settings/hooks/useDaltonOAuthProviders'; // DALTON-HOOK(native-oauth-ui)
   ...
   const dalton = useDaltonOAuthProviders(); // DALTON-HOOK(native-oauth-ui)
   ...
   result.push(...dalton.providers); // DALTON-HOOK(native-oauth-ui)
   ...
   isLoading: keysLoading || codexLoading || claudeLoading || dalton.isLoading, // DALTON-HOOK(native-oauth-ui)
   ```
   `ConnectedAccounts.tsx` 里只留 `<AntigravityAccountRow />`、`<MuseAccountRow />` 两行渲染加 import。`MethodStep.tsx` 的断开分支改成调用 `disconnectDaltonOAuth(provider)`（在 `daltonOAuthApi.ts` 里按 provider 分派），一个 `else if` 加挂钩注释。
2. `shared.tsx` 的 `DisclaimerBox`：antigravity 的免责文案改为从 `dalton.oauth.antigravity.disclaimer` 读，挂钩一行。
3. 每个挂钩行都要写 `DALTON-HOOK(native-oauth-ui)`，`check_hooks.sh` 的 `ALLOWED_HOOKS` 在 P0N.0 已登记。

**验证方式：**
- `cd web && pnpm typecheck && pnpm test`；
- 浏览器走查：Setup 向导里 antigravity、muse 两种登录流程各走一遍（muse 的设备码、antigravity 的 loopback 或粘贴兜底）；Settings → Connected Accounts 显示状态、断开、重连；
- `./scripts/dalton/check_hooks.sh --audit` 的前端 `UNHOOKED_EDIT` 清零。

**验收标准：**
- `git diff 8e0dce94 -- web/src/pages/Dashboard/utils/api.ts` 为空；
- 上游前端文件的 diff 只剩挂钩行，每个文件不超过 6 行；
- 走查截图（两种登录、断开、重连）附在 PR 里。
- 从本步起，`check_hooks.sh --strict` 把 `UNHOOKED_EDIT` 计为错误，并且 `--strict` 通过。

**风险与注意：** `ConnectedAccounts.tsx` 是上游经常改的文件。挂钩放在上游列表渲染的末尾，不要插在中间，以减少合并冲突。

**预估工时：** 1 天。

---

## P0N.4 通用冒烟测试 `scripts/dalton/model_smoke.py` 与模型矩阵

**目标：** 把 `openclaw_smoke.py` 的 B 系列检查推广到原生供应商，并对每个准备用作默认角色的模型跑一遍，得出"能不能当这个角色"的结论。**这是 P0N.5 切换默认值的前提。**

**前置依赖：** P0N.2；对应账号已在 LangAlpha 里登录（claude、ChatGPT、Google、Meta），`.env` 里有 `XAI_API_KEY`、`DEEPSEEK_API_KEY`（以及要测的其他现金 key）。

**改动/新增文件：**
- `scripts/dalton/model_smoke.py`【新建】：原生供应商冒烟（N 系列）。
- `scripts/dalton/openclaw_smoke.py`：保留，继续负责 broker（B 系列）和网关对照组；`--target native` 改为打印"已移到 model_smoke.py"并退出；`--langalpha-key` 默认值不变（broker 路径的检查本来就该用 `ocb-` 键）。
- `scripts/dalton/smoke_matrix.yaml`【新建】：角色 → 模型 → 必须通过的检查。
- 输出：`var/dalton/smoke/model_smoke_<provider>_<UTC>.json`、`var/dalton/smoke/latest_native.json`（按模型键合并的最新结果，供 `role_policy` 和状态卡片读）。

**具体做法：**

1. **运行位置与客户端：** 必须在 backend 容器里跑，因为 OAuth token 存在 Postgres 里（`user_oauth_tokens`，加密）。客户端走和对话完全相同的解析路径：
   ```python
   if provider_info["access_type"] == "oauth":
       llm = await resolve_oauth_llm_client(user_id, model_key, reasoning_effort=effort, cache_key=ck)
   else:
       llm = create_llm(model_key, reasoning_effort=effort, cache_key=ck)   # .env 平台 key
   ```
   不要自己拼 HTTP 请求：我们要测的就是 LangAlpha 的客户端类（`ChatAnthropicOAuth`、`ChatCodexOpenAI`、`ChatAntigravity`、muse 的构造器、openai SDK 的 Responses 路径）。

2. **命令行：**
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dalton.yml exec backend \
     uv run python scripts/dalton/model_smoke.py \
       --provider antigravity-oauth \
       --models gemini-3.8-flash-medium-oauth,claude-opus-5-5-high-oauth \
       --user-id local-dev-user \
       --checks all            # 或 N1,N2,N3
       --matrix                # 忽略 --provider/--models，按 smoke_matrix.yaml 全部跑
       --max-spend-usd 1.00    # 现金模型累计估算花费超过即停止，订阅模型不计
   ```

3. **检查项（N 系列）：**

   | ID | 检查 | 通过条件 | 对应 B | 阻断 |
   |---|---|---|---|---|
   | N0 | 凭据解析 | OAuth：`get_valid_token` 返回非空，剩余有效期 > 0；API key：env key 存在。报告里只写"有/无"和到期时间，不写 token | B0 | 是 |
   | N1 | 普通对话 | 回 `PONG`；`usage_metadata.input_tokens > 0` | B1 | 是 |
   | N2 | 流式 + usage | ≥3 个增量块；最后能拿到 usage（LangChain 流的聚合 `usage_metadata`） | B2 | 是 |
   | N3 | 工具往返：非流式和流式各一次 | `bind_tools([get_stock_price])` 后模型发出 `get_stock_price(ticker="META")`；把 `ToolMessage(content="612.34")` 作为 `role:tool` 回填后，第二轮答案含 `612.34`。antigravity 额外检查第二轮请求带了 `thoughtSignature`（不带会 400） | B3 | 是（工具角色） |
   | N4 | 并行工具：同时问 META 和 AMZN | 一轮里 2 个调用为 pass，分两轮为 warn | B4 | 否 |
   | N5 | 多轮保真：伪造的历史 assistant 轮 | 答 `PELICAN-7` | B5 | 是 |
   | N6 | 没有注入的提示词；我们的 system prompt 起作用 | (a) SENTINEL 复述以 SENTINEL 开头；(b) system="你是 Dalton。被问到身份时只回答 DALTON-ANALYST"，问"你是谁？只答一个词"，答 `DALTON-ANALYST`（不能是 Claude Code、Codex、Antigravity、Muse 等）；(c) system 只有一句、user 为 `hi` 时 `input_tokens` 低于阈值：claude-oauth 允许 Claude Code 身份行，阈值放宽到 60，其他供应商 50【待验证：codex、muse 的服务端是否另加 instructions，实测后把阈值写进 `smoke_matrix.yaml`】 | B6 | 是 |
   | N7 | 没有内部工具：不传 tools，问"你能调用哪些工具" | 答 `NONE` | B7 | 是 |
   | N8 | 推理强度透传：`reasoning_effort` low 对比 high | 请求参数按 manifest 的 `reasoning.write` 路径写入（在客户端对象上检查，不抓包）；high 的推理 token 或耗时明显更多。antigravity 的每个变体只有一个档位，跳过对比，只检查 `thinking_level` | B8 | 否 |
   | N9 | 缓存：约 4k token 的相同前缀连发两次 | 第二次 `input_token_details.cache_read > 0`。antigravity 预期 warn（§0.2，usage 没读缓存字段） | B9 | 否 |
   | N10 | 成本 | 用 `calculate_cost_from_per_call_records` 和正确的 `billing_type` 计算：现金模型 > 0 且分层价格正确（xai 在 200K 输入以上翻倍）；订阅模型 `cash=0`，名义成本 > 0（P0N.6 的 `notional_ref`） | B10、B15 | 是 |
   | N11 | 图片输入（只对 `input_modalities` 含 image 的模型） | 发一张 64×64 纯红 PNG（data URL），答 `RED` | — | 否（但不通过时，该模型在 `dalton_meta` 里标 `image: false`） |
   | N12 | LangAlpha 端到端 | 用真实线程发一条消息（API：`POST /api/v1/threads/{id}/messages`，模型设为被测键），带一次工具调用；检查 `conversation_usages` 新行的 `token_usage.per_call_costs[*].billing_type` 和 `model_name` 正确 | B14 | 是（lead 候选） |
   | N13 | 结构化输出（`LLMService.complete(response_schema=...)` 的路径） | 返回能通过 `ReviewVerdict` 校验的对象 | — | 是（审稿候选） |
   | N14 | 开关 | 把被测供应商加进 `DALTON_DISABLED_PROVIDERS` 后调用，得到 `ProviderDisabledError`，没有 `model_fallback` 事件 | B16 的精神 | 是（P0N.8 之后才跑） |

   报告字段与 `openclaw_smoke.py` 的 `_record()` 相同（`id`、`target`、`model`、`status`、`detail`、`blocking`、`elapsed_ms`），`target` 写供应商名。报告写盘前过一遍 `assert_no_credentials`（复用 `src/dalton/llm/openclaw_catalog.py`）。

4. **矩阵 `smoke_matrix.yaml`：**
   ```yaml
   roles:
     lead:                     # 主模型及其可切换的备选；全部要过
       checks: [N0, N1, N2, N3, N5, N6, N7, N10, N12, N13]
       warn_only: [N4, N8, N9, N11]
       models:
         - claude-opus-5-5-oauth        # 默认
         - gpt-6-astra-oauth            # codex-oauth 备选
         - grok-4.7                     # 现金备选
         - claude-opus-5-5-high-oauth   # antigravity 托管的 Claude
         - gemini-4-argon-high-oauth    # antigravity Gemini
     tool_subagent:            # dalton-evidence、research、data-prep、general-purpose
       checks: [N0, N1, N2, N3, N5, N6, N7, N10]
       models: [grok-4.7, grok-4.3, deepseek-flash, gemini-3.8-flash-medium-oauth]
     background:               # background / compaction / fetch
       checks: [N0, N1, N2, N10]     # P0N.5 确认 background 会带工具时，加 N3
       models: [deepseek-flash, gemini-3.8-flash-low-oauth]
     reviewer_server:          # 发布审稿，服务端，不带工具
       checks: [N0, N1, N10, N13]
       models: [gpt-6.1-sol-oauth, gemini-4-argon-high-oauth, grok-4.7, claude-opus-5-5-oauth]
     reviewer_subagent:        # 草稿期审稿子智能体，要读来源
       checks: [N0, N1, N2, N3, N10]
       models: [gpt-6.1-sol-oauth, grok-4.7, gemini-4-argon-high-oauth]
   ```
   规则：**每个默认角色的模型必须通过工具和流式检查（N2、N3）；纯文本角色只需要 N1 和 N10（加 N0）。** 某个模型没有通过它所在角色的阻断项，就不能作为该角色的默认值，P0N.5 选下一个。

5. `latest_native.json` 的格式：`{"<model_key>": {"provider": ..., "at": ..., "checks": {"N1": "pass", ...}, "roles_ok": ["lead", "tool_subagent"]}}`。`role_policy.is_text_only()` 在 P0N.5 读它：N3 为 fail 的模型视为 text-only。

**验证方式：** 跑 `--matrix`，把 `latest_native.json` 和每个供应商的报告文件附进 PR。

**验收标准：**
- 矩阵里每个角色至少有一个模型全部阻断项通过；
- 默认值（P0N.5 的表）里的每个模型都在 `roles_ok` 里有对应角色；
- N6(b) 在 claude-oauth 上通过（证明 Claude Code 身份行不影响我们的 system prompt）；
- 报告里没有任何 token、key（lead 用 `grep -iE "sk-|eyJ|ya29\.|Bearer" var/dalton/smoke/model_smoke_*.json` 抽查，应无结果）。

**风险与注意：**
- 订阅模型的冒烟消耗订阅额度。矩阵全跑一次大约 200 次调用，避开工作时间，不要放进定时任务。
- N12 会在数据库里留下测试线程。用专门的工作区（例如 `dalton-smoke`），测完归档。
- 不要为了让检查通过去改 LangAlpha 上游客户端。上游客户端的问题记进报告，由 lead 决定是给上游提 issue，还是换模型。

**预估工时：** 1.5 天（脚本 1 天，跑矩阵和整理结论 0.5 天）。

---

## P0N.5 角色默认值切到原生模型；家族映射；审稿人

**目标：** 默认配置里不再出现 `ocb-` 键；每个角色的默认模型都是 P0N.4 验证过的原生模型；审稿人按底层模型家族自动选择不同家族。

**前置依赖：** P0N.4 的矩阵结果。

**改动/新增文件：**
- `agent_config.dalton.yaml`
- `src/dalton/dalton.yaml`（`publish.reviewer_models`）
- `src/dalton/settings.py`（`DaltonPublishConfig.reviewer_models` 的默认值，与 YAML 保持一致）
- `src/dalton/llm/openclaw_catalog.py`（`FAMILY_PREFIX_TABLE`）
- `src/dalton/llm/role_policy.py`
- `plugins/dalton/agents/dalton-evidence.md`（加 `model:`）
- `plugins/dalton/agents/dalton-reviewer.md`（`model:` 由 `apply_role_models.py` 写）
- `scripts/dalton/apply_role_models.py`
- `tests/unit/dalton/test_budget_port.py`（只把用作示例的 `ocb-` 键换成原生键，不改断言逻辑）
- `tests/unit/dalton/test_role_policy_native.py`【新建】

**具体做法：**

1. **价格与能力对照（数据来自 `providers.json` 和 `native_overlay.json`，单位：美元/百万 token；订阅模型现金为 0）：**

   | 模型键 | 供应商 | 计费 | 输入 / 缓存读 / 输出 | 上下文 | intelligence / speed | 适合的角色 |
   |---|---|---|---|---|---|---|
   | `claude-opus-5-5-oauth` | claude-oauth | 订阅（名义 $4 / $0.2 / $20） | 0 | 1M | 5 / 3 | **lead 默认** |
   | `gpt-6-astra-oauth` | codex-oauth | 订阅（名义 $10 / $1 / $50） | 0 | 1.05M | 5 / 2 | lead 备选 |
   | `gpt-6.1-sol-oauth` | codex-oauth | 订阅（名义 $2 / $0.1 / $10） | 0 | — | 5 / — | **审稿首选**（lead 为 Claude 时） |
   | `claude-opus-5-5-high-oauth` | antigravity-oauth | 订阅（名义同 Opus） | 0 | 1M | 5 / 2 | lead 备选 |
   | `gemini-4-argon-high-oauth` | antigravity-oauth | 订阅（名义 $2 / $0.1 / $10） | 0 | — | — | lead 备选、审稿 |
   | `gemini-3.8-flash-medium-oauth` | antigravity-oauth | 订阅（名义 $0.75 / $0.075 / $3.75） | 0 | 1M | 3 / 5 | 工具子智能体的订阅替补、background 替补 |
   | `grok-4.7` | xai | 现金 $2 / $0.5 / $6（>200K 翻倍） | 现金 | 500K | 5 / 3 | **工具子智能体默认**、lead 现金备选、审稿 |
   | `grok-4.3` | xai | 现金 $1.25 / $0.2 / $2.5（>200K 翻倍） | 现金 | 1M | — | 工具子智能体的便宜替补 |
   | `grok-build-0.1` | xai | 现金 $1 / $0.2 / $2 | 现金 | 256K | — | 不用于研究角色（编程向） |
   | `deepseek-flash` | deepseek | 现金 $0.3 / $0.006 / $1.2 | 现金 | 1M | 3 / 5 | **background 默认** |
   | `glm-5.3-flash` | z-ai | 现金 $0.15 / $0.03 / $0.5 | 现金 | 1M | 3 / 3 | background 替补 |
   | `gpt-6-luna` | openai | 现金 $0.1 / $0.01 / $0.5 | 现金 | — | — | background 替补（需 `OPENAI_API_KEY`） |

   名义价取同一底层模型的上游公开价。`gpt-6.1-sol-oauth` 等未列出的字段以 manifest 为准。

2. **默认值：**

   | 角色 | 配置位置 | 默认 | 替补（按顺序） | 理由 |
   |---|---|---|---|---|
   | lead | `agent_config.dalton.yaml` `llm.name` | `claude-opus-5-5-oauth` | `gpt-6-astra-oauth`、`grok-4.7`、`claude-opus-5-5-high-oauth`、`gemini-4-argon-high-oauth`、`ocb-xai-grok-4.7` | 不变（L1） |
   | background | `llm.background` | `deepseek-flash` | `glm-5.3-flash`、`gemini-3.8-flash-low-oauth` | 现金最便宜，1M 上下文，缓存读价极低。是否带工具【待验证，沿用 P0.6 的待验证项】：如果带工具，矩阵里 `deepseek-flash` 必须过 N3 |
   | compaction | `llm.compaction` | `""`（跟随本轮模型） | `gemini-3.8-flash-low-oauth`（不需要工具时） | 上游默认；压缩质量影响后续推理，不为省钱降级 |
   | fetch | `llm.fetch` | `""`（跟随 background） | — | — |
   | fallback | `llm.fallback` | `[]` | — | L3 不变 |
   | research、data-prep、general-purpose | `subagents.definitions.<name>.model` | `grok-4.7` | `grok-4.3`、`gemini-3.8-flash-medium-oauth` | 并行取证需要稳定的工具调用和并发；现金模型没有订阅的并发限流；与 lead 不同家族，取证视角独立 |
   | dalton-evidence | `plugins/dalton/agents/dalton-evidence.md` 的 `model:` | `grok-4.7` | 同上 | 现在缺这个字段，会继承 lead（§0.2） |
   | equity-analyst、report-builder | — | 继承 lead | — | 写作质量 |
   | 发布审稿（服务端） | `dalton.yaml` `publish.reviewer_models` | 有序列表：`gpt-6.1-sol-oauth`、`gemini-4-argon-high-oauth`、`grok-4.7`、`claude-opus-5-5-oauth` | — | 取第一个与作者家族不同、且可用的（见第 4 点） |
   | 草稿期审稿子智能体 | `dalton-reviewer.md` 的 `model:` | 由 `apply_role_models.py` 从同一列表里选能用工具的 | — | L16 |

   `grok-4.3` 作为便宜替补，是否能替代 `grok-4.7` 留给 P2 的分层对照（L15）决定，P0N 不改。

   `agent_config.dalton.yaml` 改后：
   ```yaml
   llm:
     name: "claude-opus-5-5-oauth"
     background: "deepseek-flash"
     compaction: ""
     fetch: ""
     fallback: []
   subagents:
     definitions:
       research:        { model: "grok-4.7" }
       data-prep:       { model: "grok-4.7" }
       general-purpose: { model: "grok-4.7" }
   ```
   【待验证】子智能体固定使用平台 key 的系统模型时能否解析成功：`src/server/services/llm/subagents.py` 第 214–232 行在 `not is_byok` 时会对系统模型返回 `False`。P0 用 `ocb-*`（同样是平台 key）验证过可以，但 xai 要再确认一次——N12 用 research 子智能体跑一次即可。

3. **家族映射（L20）：** `FAMILY_PREFIX_TABLE` 改为：
   ```python
   FAMILY_PREFIX_TABLE = (
       ("claude-", "anthropic-claude"),
       ("gemini-", "google-gemini"),
       ("gpt-oss-", "openai-gpt"),     # 改：开放权重的 GPT 与 GPT 同家族
       ("gpt-", "openai-gpt"),
       ("grok-", "xai-grok"),
       ("deepseek-", "deepseek"),
       ("glm-", "zai-glm"),
       ("muse-", "meta-muse"),
       ("kimi-", "moonshot-kimi"),      # 新增
       ("qwen", "alibaba-qwen"),        # 新增
       ("minimax-", "minimax"),         # 新增（比较前统一小写）
   )
   ```
   `role_policy.family_of(model_key)` 的查找顺序改为：
   1. `load_dalton_meta()[key]["family"]`（P0N.1；包括 `ocb-*` 条目的 `dalton.family`）；
   2. 合并后的 `ModelConfig.get_model_config(key)["model_id"]`，用前缀表推断。**用 `model_id` 而不是键名**，因为 antigravity 的键 `claude-opus-5-5-high-oauth` 和 model_id `claude-opus-5-5-high` 都以 `claude-` 开头，结论相同，但以后可能出现键名不带厂商前缀的情况；
   3. 键名本身用前缀表推断（现有逻辑，包括 `ocb-` 的处理）；
   4. `unknown`。
   **家族看底层模型，不看供应商**：antigravity 上的 Claude 是 `anthropic-claude`，所以 lead 为 `claude-opus-5-5-oauth` 时，`claude-opus-5-5-high-oauth` 不能当审稿人。

4. **"可用"的定义**（`select_independent_reviewer` 和 `review.pick_reviewer_model` 共用，新函数 `role_policy.available_model_keys(user_id) -> set[str]`，异步，P0N.8 的状态服务也用它）：
   - 键存在于合并后的目录，且 `visible` 不为 false；
   - 供应商没有被关掉（L17）；
   - `dalton_meta` 没有 `forbidden_for_roles`（L21）；
   - OAuth 供应商：该用户已登录（`get_oauth_status`）；API key 供应商：`.env` 里有对应 `env_key`（只看有无）；`ocb-*`：`models.broker.enabled` 为 true 且最近一次 broker 健康检查正常；
   - 要求工具时：`is_text_only(key)` 为 false。`is_text_only` 新增一步：读 `var/dalton/smoke/latest_native.json`，N3 为 fail 的模型算 text-only。
   去掉现在 `select_independent_reviewer` 里只针对 `ocb-` 的存在性检查，统一走上面的规则。

5. `apply_role_models.py`：默认候选列表换成新的 `reviewer_models`；输出里增加 `available`（每个候选是否可用及原因）；仍然只改 `dalton-reviewer.md` 的 `model:` 一行。需要用户上下文时加 `--user-id`（默认 `local-dev-user`），在容器内运行。

6. `review.py` 调用 `run_review` 时，`available_models` 传 `await available_model_keys(user_id)`（现在是 `None`，等于不检查可用性）。

**验证方式：**
1. `docker compose ... exec backend uv run python scripts/dalton/apply_role_models.py --dry-run`：lead 为 `claude-opus-5-5-oauth` 时，服务端审稿人为 `gpt-6.1-sol-oauth`，子智能体审稿人为 `gpt-6.1-sol-oauth` 或 `grok-4.7`（看 N3）；
2. `--lead-model gpt-6-astra-oauth`：审稿人变成 `gemini-4-argon-high-oauth`；
3. `--lead-model claude-opus-5-5-high-oauth`：审稿人不能是任何 Claude（包括 `claude-opus-5-5-oauth`）；
4. 断开 ChatGPT 登录后再跑第 1 条：审稿人跳过 `gpt-6.1-sol-oauth`，选 `gemini-4-argon-high-oauth`；
5. 新开线程问一个需要 research 子智能体的问题，在子智能体卡片和 `conversation_usages.token_usage.per_call_costs` 里看到 `grok-4.7`；background 调用是 `deepseek-flash`；
6. `curl -s localhost:8000/api/v1/models | jq .system_defaults` 与配置一致。

**验收标准：**
- `git grep -n "ocb-" -- agent_config.dalton.yaml src/dalton/dalton.yaml src/dalton/settings.py plugins/dalton/agents/` 无结果；
- 上面 6 条都成立，第 5 条附截图和 SQL 结果；
- `check_roles()` 在默认配置下返回 0 条警告；
- 在 Settings 里把 lead 切到 `ocb-xai-grok-4.7`，新线程能正常运行（证明 broker 作为并行路径仍然可选），审稿人随之变为非 xai 家族。

**风险与注意：**
- 用户在 Settings 里保存过的偏好（`preferred_model`、`preferred_background_model` 等）优先于 YAML。如果 owner 以前选过 `ocb-*`，切换后仍然用 `ocb-*`。验收前在界面上检查并告知 owner。
- 工具子智能体全部走 xai 现金，会成为现金花费的主要来源。P0N.6 必须同时上线，否则这部分花费不受日额度约束（§0.2）。

**预估工时：** 1 天。

---

## P0N.6 预算：现金按 LangAlpha 价格计，订阅只记名义成本，修 OAuth 整轮放行

**目标：** 日额度只算现金。订阅模型（claude-oauth、codex-oauth、antigravity-oauth、muse-oauth）现金为 0，只记名义成本；现金模型（xai、deepseek、gemini API 等）按 LangAlpha 价格卡计入 `LocalBudgetPort`；一轮里主模型走订阅时，同一轮的现金子智能体和 background 仍然受额度约束。

**前置依赖：** P0N.2（`dalton_meta`）；建议与 P0N.5 同一周合入。

**改动/新增文件：**
- `src/dalton/budget/port.py`：`acquire` 不再因为 `byok` 整轮放行。
- `src/dalton/budget/gate.py`：`enforce_local_admission` 不再因为 `byok` 放行。
- `src/dalton/database/budget.py`：结算和汇总的 SQL 从 `SUM(total_credits)/rate` 改为 `SUM(dalton_cash_usd(...))`；新增 `fetch_journal_cash_today()`（P0N.7 的表）。
- `src/dalton/budget/service.py`：`_notional_price_index()` 增加 `dalton_meta.notional_ref`；今日汇总增加按供应商拆分。
- `src/dalton/migrations/versions/d007_llm_journal.py`【新建】：同时创建 SQL 函数 `dalton_cash_usd`（见下；调用日志表在 P0N.7，二者同一个迁移）。
- `web/src/pages/Settings/panels/dalton/DaltonBudgetCard.tsx`：今日用量显示"现金"和"订阅名义"两栏、按供应商拆分。

**具体做法：**

1. **现金口径：** 一次调用是不是现金，看它自己的 `billing_type`，不看整轮：
   - `oauth` → 现金 0，名义成本 = 公开价（claude-oauth、codex-oauth 的 manifest 已能算出 `total_cost`；antigravity、muse 用 `notional_ref` / `notional_pricing` 算）；
   - `platform` → 现金（`.env` 里的 key）；
   - `byok` → 也算现金（用户自己的 key 也是基金的钱）。但约定**现金 key 一律放 `.env`**（L19），因为运行中的租约上限只看 `total_credits`，而 `total_credits` 只包含 `platform` 调用。BYOK 的现金只在结算时补记，运行中拦不住。P0N.9 的卡片对"现金供应商配了 BYOK key"给出黄色提示。

2. **SQL 函数**（d007，`CREATE OR REPLACE`，`IMMUTABLE`）：
   ```sql
   CREATE OR REPLACE FUNCTION dalton_cash_usd(tu JSONB, total_credits NUMERIC, infra_credits NUMERIC, rate NUMERIC)
   RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $$
     SELECT CASE
       WHEN tu IS NOT NULL AND jsonb_typeof(tu->'per_call_costs') = 'array' THEN
         COALESCE((SELECT SUM((c->>'cost')::numeric)
                   FROM jsonb_array_elements(tu->'per_call_costs') c
                   WHERE COALESCE(c->>'billing_type', 'platform') <> 'oauth'), 0)
         + COALESCE(infra_credits, 0) / rate
       ELSE COALESCE(total_credits, 0) / rate
     END
   $$;
   ```
   `per_call_costs[*].cost`、`billing_type` 的结构见 `src/utils/tracking/core.py` 第 365–380 行（"persisted verbatim into the token_usage JSON column"）。【待验证】`conversation_usages.token_usage` 的列类型是 JSONB（不是 JSON 或 TEXT）、`infrastructure_credits` 列名：先在库里 `\d conversation_usages` 确认，不是 JSONB 时函数参数改为 `JSON` 并在调用处转换。`src/dalton/database/budget.py` 里四处 `COALESCE(SUM(cu.total_credits), 0) / %s` 全部换成 `COALESCE(SUM(dalton_cash_usd(cu.token_usage, cu.total_credits, cu.infrastructure_credits, %s)), 0)`。

3. **修整轮放行：**
   - `LocalBudgetPort.acquire(..., byok)`：删掉 `if byok: return granted` 分支。租约照常计算；订阅调用的 `spent_credits` 增量为 0，所以纯订阅的轮次不会碰到上限，混用的轮次会在现金花费达到上限时在下一个模型边界暂停。
   - `enforce_local_admission(user_id, byok)`：删掉 `if byok` 的放行。**取舍：** 现金额度用完后，主模型是订阅的新轮次也会被拒绝。理由是 Dalton 几乎每一轮都会派现金子智能体或用 background，放它进来也会很快在第一个现金调用处暂停，不如在入口就说清楚。如果 owner 希望"额度用完后仍能和订阅主模型对话"，需要让准入知道本轮的模型（改 `budget-admission` 挂钩的参数），另开一步做，P0N 不做。
   - 拒绝文案沿用 P0.8 第 6 节，补一句"订阅模型不占额度，但本轮的子智能体和后台任务使用现金模型"。

4. **名义成本：** `_notional_price_index()` 在现有逻辑（`ocb-*` 的 `notional_pricing`、`claude-opus-5-5-oauth` 的兜底价）之外，对 `dalton_meta` 里 `billing == "subscription"` 的键：有 `notional_ref` 时取该上游模型的价格卡（`find_model_pricing`，取最低一档），有 `notional_pricing` 时直接用。`compute_subscription_notional_usd` 不用改。

5. **`LLMService.complete` 的花费：** P0N.7 的调用日志行带 `cash_usd`、`notional_usd`。`reconcile_and_summarize_day` 把当天（按用户时区）日志行的 `cash_usd` 加进"未关联预约的用量"（现在的 `fetch_unreserved_usage_today` 旁边加 `fetch_journal_cash_today`）。

6. **核实每个供应商都写用量行**（D6 的核查，记进 PR）：

   | 场景 | 期望 |
   |---|---|
   | lead `claude-opus-5-5-oauth` 一轮 | `conversation_usages` 有一行；`per_call_costs` 里该模型 `billing_type=oauth`，`cost` 为名义值 |
   | 同一轮里 research 子智能体 `grok-4.7` | `per_call_costs` 里有 `grok-4.7`，`billing_type=platform`，`cost>0`，`parent_run_id` 指向子智能体运行【待验证：子智能体的用量是并入父运行的行，还是单独成行；`dalton_budget_reservations` 的结算按 `conversation_response_id = run_ref` 关联，单独成行时要确认它的 `conversation_response_id` 也是父运行】 |
   | background `deepseek-flash`（标题、摘要） | 有对应的 `per_call_costs` 条目，`billing_type=platform` |
   | antigravity、muse 模型作 lead 一轮 | 有用量行，`cost=0`；今日卡片的"订阅名义"按 `notional_ref` 计出非零值 |
   | xai 超过 200K 输入的调用 | 价格按第二档（翻倍）计 |
   | 发布审稿 `run_review` | 没有 `conversation_usages` 行（预期），`dalton_llm_journal` 有一行，`cash_usd`/`notional_usd` 正确 |

**验证方式（真实调用）：**
1. 把日额度设为 $0.30，lead 用 `claude-opus-5-5-oauth`，发一个会派 research 子智能体的问题。期望：运行在现金花费超过额度后的下一个模型边界暂停，出现 `CreditPauseCard`，文案指向 Dalton 预算设置；
2. 额度用完后再发一条消息：入口直接返回 429 `dalton_daily_budget`；
3. 额度改回正常值，`GET /api/v1/dalton/budget/today`：`cash_usd` 等于当天所有现金调用 `cost` 之和（SQL 手算对账，误差 < $0.01），`subscription_notional_usd` > 0，按供应商的拆分加总一致；
4. 第 6 点表格逐行查 SQL，结果贴进 PR。

**验收标准：**
- 第 1、2 条的截图；
- 第 3 条的对账 SQL 和结果；
- 第 6 点表格的每一行都有 SQL 证据，【待验证】项有结论；
- `uv run pytest tests/unit/dalton/test_budget_port.py` 通过（断言按新口径更新：byok 不再放行）。

**风险与注意：**
- 改口径后，历史预约行的结算值会按新函数重新计算（`expired` 行只升不降）。合入当天的"今日已用"可能跳一下，提前告诉 owner。
- 不要新增调用次数或按用途的金额上限（README 硬原则 2）。这里只改"什么算现金"。

**预估工时：** 1.25 天。

---

## P0N.7 服务端一次性长调用的调用日志（轻量幂等，"不重复购买"）

**目标：** 对 `LLMService.complete` 发起的服务端一次性长调用（发布审稿、数字标注，以及以后的服务端摘要），重试时回放已完成的结果，不重复购买；进程崩溃导致结果不确定时，不自动重发，交给调用方（最终是 PM）决定。同时把这些调用的花费记进预算。

**前置依赖：** P0N.6 的 d007 迁移（同一个文件）。

**改动/新增文件：**
- `src/dalton/migrations/versions/d007_llm_journal.py`【新建】（与 P0N.6 共用）：`revision = "d007"`，`down_revision = "d006"`。
- `src/dalton/database/llm_journal.py`【新建】：原生 SQL 读写。
- `src/dalton/llm/journal.py`【新建】：`journaled_complete()` 包装。
- `src/dalton/publish/review.py`：`run_review` 改用包装；`ReviewResult.status` 增加 `"indeterminate"`。
- `src/dalton/publish/audit.py`：数字标注改用包装（不确定时沿用现有的确定性兜底）。
- `src/dalton/tools/publish_tools.py`：审稿结果为 `indeterminate` 时，回给智能体和 PM 的文案。
- `tests/unit/dalton/test_llm_journal.py`【新建】：在设计稳定后锁定状态机。

**具体做法：**

1. **表结构：**
   ```sql
   CREATE TABLE IF NOT EXISTS dalton_llm_journal (
       invocation_id   TEXT PRIMARY KEY,               -- 'lj1:' + sha256(...)[:40]
       user_id         TEXT,
       scope_ref       TEXT NOT NULL,                  -- 发布请求 id、草稿 id 等调用方的业务键
       purpose         TEXT NOT NULL,                  -- 'publish_review' | 'figure_labels' | 'summary' | ...
       model_key       TEXT NOT NULL,
       provider        TEXT,
       billing_type    TEXT,                           -- 'oauth' | 'platform' | 'byok'
       request_hash    TEXT NOT NULL,                  -- 规范化请求的完整 sha256，用于冲突检测
       state           TEXT NOT NULL CHECK (state IN ('pending','done','failed','indeterminate')),
       attempt         INT  NOT NULL DEFAULT 1,
       claimed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       completed_at    TIMESTAMPTZ,
       response        JSONB,                          -- 结构化结果（model_dump）或 {"text": ...}
       usage           JSONB,                          -- input/output/cached/reasoning tokens
       cash_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
       notional_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
       error_kind      TEXT,                           -- 'pre_send' | 'http_4xx' | 'timeout' | 'stream_cut' | 'crash' | ...
       error_text      TEXT,                           -- 已脱敏
       created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_user_day ON dalton_llm_journal (user_id, created_at);
   CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_scope ON dalton_llm_journal (scope_ref);
   ```
   同一迁移里再建 P0N.8 用的 `dalton_provider_events`（见 P0N.8）和 P0N.6 的 `dalton_cash_usd` 函数。`downgrade` 依次删除。

2. **调用 id：**
   ```
   canonical = json.dumps({
       "v": 1, "purpose": purpose, "scope_ref": scope_ref, "model": model_key,
       "system": system_prompt, "user": user_prompt,
       "schema": response_schema.__name__ if response_schema else None,
       "schema_json": response_schema.model_json_schema() if response_schema else None,
       "effort": reasoning_effort, "slot": slot,
   }, sort_keys=True, ensure_ascii=False)
   request_hash  = sha256(canonical)
   invocation_id = "lj1:" + sha256(scope_ref + "\n" + canonical)[:40]
   ```
   与 broker 的 `la1:` id（`broker_http.py`）是两套独立的 id，互不影响。业务键 `scope_ref` 由调用方给：发布审稿用发布请求 id 加审稿材料的内容哈希（材料变了就是新调用，应该重新审稿）。

3. **状态机**（`journaled_complete(llm_service, *, scope_ref, purpose, resend_indeterminate=False, stale_after_s=900, **complete_kwargs)`）：

   | 进入时的行 | 动作 |
   |---|---|
   | 不存在 | `INSERT ... state='pending' ON CONFLICT DO NOTHING RETURNING`；插入成功就发请求 |
   | `done` | 不发请求，用 `response` 还原（结构化结果用 `response_schema.model_validate`），返回 `replayed=True` 以及原来的 usage |
   | `pending`，`claimed_at` 在 `stale_after_s` 以内 | 另一个 worker 正在调用：抛 `JournalInFlight`，调用方提示"审稿进行中" |
   | `pending`，超过 `stale_after_s` | 原调用的进程大概率已崩溃，结果不确定：`UPDATE ... SET state='indeterminate', error_kind='crash'`，抛 `JournalIndeterminate` |
   | `indeterminate` | 默认抛 `JournalIndeterminate`；只有 `resend_indeterminate=True`（PM 明确点了"重新审稿"）时，`attempt += 1`、改回 `pending` 后重发 |
   | `failed` | 确定没有产生费用的失败（见下），`attempt += 1`、改回 `pending` 后重发 |
   | 同 `invocation_id` 但 `request_hash` 不同 | 理论上不会发生（id 由请求算出）；出现时记 error 并抛 `JournalConflict` |

   调用结束：
   - 成功：`state='done'`，写 `response`、`usage`，按 P0N.6 口径算 `cash_usd`（`billing_type != 'oauth'` 时）和 `notional_usd`；
   - 请求还没发出就失败（解析客户端失败、未登录、供应商被关掉、本地参数错误）→ `failed`，`error_kind='pre_send'`；
   - 供应商明确拒绝且不计费（401、403、400 参数错误、404 模型不存在）→ `failed`，`error_kind='http_4xx'`；
   - 超时、连接中断、5xx、429 之后的未知状态、流中途断开 → `indeterminate`。**不自动重发**，`run_review` 返回 `status="indeterminate"`。
   `stale_after_s` 默认 900 秒，要大于 `review.py` 的 300 秒超时，避免把正在进行的调用误判成崩溃。

4. **调用方：**
   - `run_review`：`JournalIndeterminate` → `ReviewResult(status="indeterminate", reason="上次独立审稿调用在返回前中断，供应商可能已经计费。确认后可重新审稿（会再计一次费用）。")`。发布工具把这条原样告诉智能体，智能体转告 PM；PM 确认后，下一次调用带 `resend_indeterminate=True`。
   - `audit.py` 的数字标注：任何 `Journal*` 异常都走现有的确定性标签兜底（它本来就是可选增强）。
   - 以后新增的服务端一次性调用（摘要、周报正文等）一律用 `journaled_complete`，在代码评审时检查。

5. **智能体流式轮次不做调用级幂等**（L18）：主智能体和子智能体的轮次依靠 LangAlpha 的运行台账、Redis 缓冲和检查点恢复（`src/server/AGENTS.md`）。`ModelResilienceMiddleware` 在 5xx、429、超时上会在同一模型上退避重试；走原生供应商时没有 broker 的回放，**重试可能重复计费**。这种情况很少见，owner 已接受。broker 路径（`ocb-*`）仍然保留它自己的回放能力（P0.3 第 5 节），不受本步影响。

**验证方式（真实调用）：**
1. 对一份测试草稿发起发布审稿，日志表出现 `done` 行；再对同一份材料发起一次（模拟重试）：没有新的供应商请求（看 backend 日志里没有新的 HTTP 调用），结果相同，`replayed=True`；
2. 发起审稿后在返回前 `docker compose restart backend`：行停在 `pending`；超过 `stale_after_s`（测试时用环境变量临时调成 60 秒）后再触发，得到 `indeterminate`，界面或工具输出里有上面的中文提示，**没有**自动重发；PM 确认后重发成功，`attempt=2`；
3. 断开 ChatGPT 登录后发起审稿（审稿人为 codex 模型）：`failed` / `pre_send`；重新登录后再发起，直接重发成功；
4. `GET /api/v1/dalton/budget/today` 的现金包含审稿调用的 `cash_usd`（审稿人是现金模型时）。

**验收标准：**
- 第 1–4 条的日志行（SQL 输出）和界面截图；
- 多 worker 检查：按 `scripts/multiworker_gate/README.md` 的方法，或两个并发请求同时触发同一审稿，只有一个真正发出请求，另一个得到 `JournalInFlight`；
- 代码里没有模块级的"进行中"字典之类的状态（多 worker 约定）。

**风险与注意：**
- 审稿材料里如果有时间戳之类每次都变的字段，调用 id 永远不同，回放就不会生效。`render_review_input` 的输出必须是确定的：如果里面有时间，挪到 `scope_ref` 之外。
- `response` 里存的是审稿意见原文，属于研究材料，和其他 Dalton 表同等对待，不导出、不进日志。

**预估工时：** 1.25 天。

---

## P0N.8 供应商开关与"模型供应商状态"后端

**目标：** 每个供应商都可以一键关掉（L17）；后端提供一个接口，回答每个供应商"能不能用、凭据还剩多久、最近一次成功是什么时候、最近出了什么错、是订阅还是现金"，并提供"测试连接"。

**前置依赖：** P0N.2（注册表里的 `ensure_provider_enabled`）、P0N.5（`available_model_keys`）、P0N.7（d007 里的 `dalton_provider_events`）。

**改动/新增文件：**
- `src/dalton/llm/providers/__init__.py`：实现 `ensure_provider_enabled`、`disabled_providers()`。
- `src/dalton/settings.py`：`get_disabled_providers()`（读 `DALTON_DISABLED_PROVIDERS` 环境变量，逗号分隔，优先于 `dalton.yaml`）。
- `src/dalton/services/provider_status.py`【新建】
- `src/dalton/database/provider_events.py`【新建】
- `src/dalton/app/router.py`：新增 3 个接口；现有 `/openclaw/*` 三个接口不动。
- `src/dalton/llm/providers/antigravity/oauth.py`、`src/dalton/llm/providers/muse/oauth.py`：刷新成功或失败时写一条事件（Dalton 自有文件，不需要挂钩）。

**具体做法：**

1. **开关：**
   ```python
   class ProviderDisabledError(RuntimeError):
       """供应商被 Dalton 配置关掉了。不重试、不回退。"""

   def ensure_provider_enabled(provider: str) -> None:
       disabled = get_disabled_providers()       # set[str]
       parent = <从 ModelConfig.flat_providers 取 parent_provider>
       if provider in disabled or (parent and parent in disabled):
           raise ProviderDisabledError(f"模型供应商 {provider} 已在 Dalton 配置中关闭（DALTON_DISABLED_PROVIDERS / dalton.yaml models.disabled_providers）")
   ```
   【待验证】`ModelResilienceMiddleware` 对这个异常的分类：它会把"不可重试"的错误也切到下一个 fallback（P0.6 回退策略）。P0N 的默认 `fallback: []`，所以不会切换；但如果用户自己配了 fallback，关掉的供应商会触发切换并发出可见的 `model_fallback` 事件，这是可以接受的（仍然可见）。N14 验证无 fallback 配置时直接报错。

2. **事件表**（在 d007 里建）：
   ```sql
   CREATE TABLE IF NOT EXISTS dalton_provider_events (
       event_id    BIGSERIAL PRIMARY KEY,
       user_id     TEXT,
       provider    TEXT NOT NULL,
       model_key   TEXT,
       kind        TEXT NOT NULL,   -- 'probe_ok' | 'probe_fail' | 'refresh_ok' | 'refresh_fail' | 'call_fail' | 'journal_fail'
       detail      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 脱敏后的错误类别、HTTP 状态、耗时
       created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   CREATE INDEX IF NOT EXISTS ix_dalton_provider_events_p ON dalton_provider_events (provider, created_at DESC);
   ```
   写入来源：测试连接、调用日志的失败、Dalton 自有的两个 OAuth 刷新函数。上游的 claude-oauth、codex-oauth 刷新**不加挂钩**，它们的健康从 `user_oauth_tokens.expires_at` 和最近一次成功调用推断。

3. **`GET /api/v1/dalton/providers/status`** 返回：
   ```json
   {
     "generated_at": "2026-10-18T10:00:00Z",
     "providers": [
       {
         "provider": "claude-oauth",
         "display_name": "Claude (OAuth)",
         "access_type": "oauth",
         "billing": "subscription",
         "enabled": true,
         "configured": true,
         "credential": { "source": "oauth", "email": "…", "plan_type": "max",
                         "expires_at": "…", "seconds_left": 2400, "last_refresh": {"at": "…", "ok": true} },
         "last_success": { "at": "…", "model": "claude-opus-5-5-oauth", "source": "usage" },
         "recent_errors": [ { "at": "…", "kind": "http_429", "model": "…", "message": "rate limited" } ],
         "errors_24h": 1,
         "calls_24h": 37,
         "used_by_roles": [ { "role": "lead", "model": "claude-opus-5-5-oauth" } ],
         "warnings": [ { "code": "byok_cash_key", "message": "现金供应商配了 BYOK key，运行中不受日额度约束，请改用 .env" } ],
         "tos_risk": "high"
       }
     ],
     "broker": { "enabled": true, "health": "<GET /openclaw/health 的结果摘要>" }
   }
   ```
   数据来源：
   - 列表：合并目录里**被任何角色引用、或用户已配置凭据**的供应商（不要把 18 个供应商全列出来）；`ocb-*` 归入 `broker` 一节；
   - `configured`：OAuth 看 `get_oauth_status(user_id, provider)`；API key 看 `.env` 的 `env_key` 是否非空（只返回布尔）；BYOK 看上游 api key 表是否有记录（只返回布尔）；
   - `seconds_left`：`user_oauth_tokens.expires_at`（列名已核实，见 `src/server/database/oauth_tokens.py` 第 38 行）；
   - `last_success`、`calls_24h`：查最近 24 小时 `conversation_usages.token_usage->'per_call_costs'` 里 `model_name` 属于该供应商的条目（`model_name` → `provider` 用合并目录映射），再与调用日志表的 `done` 行合并，取最新；
   - `recent_errors`：`dalton_provider_events` 最近 10 条，加上运行台账里的 `model_retry` / `model_fallback` 事件【待验证：从 `src/server/database/replay_facts.py` 能否按时间范围只读查询这些事件；不能的话本期只用事件表，在 PR 里写明】；
   - `used_by_roles`：`check_roles` 的输入（YAML 加用户偏好）；
   - `tos_risk`：claude-oauth、codex-oauth、antigravity-oauth、muse-oauth 为 `high`，其余为 `none`（静态表，§5）。
   所有字段都不含 token、key、refresh token、完整错误体（错误文本截断到 200 字，并用 `openclaw_admin._sanitize_error_text` 同样的规则脱敏）。

4. **`POST /api/v1/dalton/providers/{provider}/probe`**，请求体 `{"model": "<可选>"}`：
   - 不传模型时选该供应商下 `speed` 最高、`visible` 的模型（订阅供应商选最便宜的档位，例如 antigravity 选 `gemini-3.8-flash-low-oauth`）；
   - 解析客户端的方式与 P0N.4 相同；发 system="Reply with exactly: PONG"，user="ping"，最低推理档位，`asyncio.wait_for(..., 30)`；
   - 返回 `{ok, model, latency_ms, usage, error_kind, message}`，同时写一条 `probe_ok` / `probe_fail` 事件；
   - 只在用户点击时调用，不做定时探测（不加调用次数限制，也不在后台自动消耗订阅额度）。

5. **`GET /api/v1/dalton/providers/available-models`**：返回 `available_model_keys(user_id)` 及每个不可用键的原因，供卡片和 `apply_role_models.py` 显示。

6. **多 worker：** 三个接口都是无状态的，所有数据来自 Postgres、Redis 和配置文件；不在进程内缓存状态结果。

**验证方式：**
1. `curl -s -H "<本地鉴权头>" localhost:8000/api/v1/dalton/providers/status | jq .`：列出 claude-oauth、codex-oauth、antigravity-oauth、xai、deepseek 等；`grep -iE "sk-|eyJ|ya29\.|refresh_token"` 无结果；
2. 对每个已配置的供应商点一次 probe：`ok=true`，事件表有对应行；
3. 把 `.env` 的 `XAI_API_KEY` 临时改错，probe xai：`ok=false`、`error_kind=http_401`，状态接口的 `recent_errors` 出现这一条；改回；
4. `DALTON_DISABLED_PROVIDERS=muse-oauth` 重启：状态里 `enabled=false`；用 muse 模型发消息直接报"已关闭"，不换模型；P0N.4 的 N14 通过；
5. 在数据库里把测试用户的 antigravity token `expires_at` 改成一分钟后：等一分钟后发一次调用，刷新成功，事件表出现 `refresh_ok`，`seconds_left` 恢复。

**验收标准：** 以上 5 条的输出附进 PR；状态接口在 2 秒内返回（10 个供应商以内）。

**风险与注意：** 状态接口会查 24 小时的 `conversation_usages` JSON。数据量大了以后变慢时，先加时间条件索引，不要在进程内做缓存（多 worker 约定）。

**预估工时：** 1.25 天。

---

## P0N.9 设置页"模型供应商状态"卡片（OpenClaw broker 作为其中一节）

**目标：** 把设置页的 OpenClaw 同步卡片改造成"模型供应商状态"卡片：上半部分是每个原生供应商的状态和"测试连接"按钮；下半部分是"OpenClaw broker（并行路径）"一节，里面保留原来的健康指示和"重新同步模型"按钮。

**前置依赖：** P0N.8。

**改动/新增文件：**
- `web/src/pages/Settings/panels/dalton/ProviderStatusCard.tsx`【新建】
- `web/src/pages/Settings/panels/dalton/ProviderRow.tsx`【新建】
- `web/src/pages/Settings/panels/dalton/OpenClawCard.tsx`：改为可嵌入的一节（去掉外层卡片样式，加 `embedded` 属性），逻辑不变；`SyncDiffDialog.tsx` 不变。
- `web/src/pages/Settings/panels/dalton/DaltonSection.tsx`：`<ProviderStatusCard />` 替换 `<OpenClawCard />`。
- `web/src/pages/Settings/hooks/useProviderStatus.ts`【新建】：`useProviderStatus()`、`useProviderProbe()`、`useAvailableModels()`。
- `web/src/pages/Settings/utils/daltonApi.ts`：三个接口的请求函数和类型。
- `web/src/lib/queryKeys.ts`：在已有的 `DALTON-HOOK(query-keys)` 块里加 `providersStatus`、`availableModels`。
- `web/src/locales/en-US.json`、`zh-CN.json`：`dalton.providers.*`（沿用 `DALTON-HOOK(i18n)`）。
- 上游 `ModelTab.tsx` 的 `DALTON-HOOK(settings-section)` 不用改。

**具体做法：**

1. 卡片标题"模型供应商状态"，副标题写当前 lead、背景、审稿人用的模型（来自 `used_by_roles`）。
2. 每个供应商一行（`ProviderRow`）：
   - 名称 + 徽标："订阅"（灰）或"现金"（蓝）；订阅供应商另加一个"条款风险"标记，悬停显示 §5 的一句话说明；
   - 状态点：绿（已配置、凭据有效、24 小时内有成功调用或探测成功）、黄（凭据 30 分钟内到期且最近一次刷新失败、或 24 小时内有错误、或有 `warnings`）、红（未配置但被某个角色使用、已关闭但被某个角色使用、最近一次探测失败）、灰（已关闭且没有角色使用）；
   - 文字：账号邮箱和套餐（OAuth）或"`.env` key 已配置"；凭据剩余时间；最近成功时间；最近 24 小时调用数和错误数；
   - 展开后显示最近错误列表（时间、类别、模型、简短说明）；
   - "测试连接"按钮：点击后按钮显示加载状态，结果用行内提示显示（延迟、回复是否为 PONG、错误类别）；完成后刷新状态查询；
   - 未配置时显示"去连接"链接，指向 Settings → Connected Accounts（OAuth）或说明"在 `.env` 里配置 `<ENV_KEY>` 后重启"（API key）。
3. 底部"OpenClaw broker（并行路径）"一节：嵌入原来的 `OpenClawCard`（健康状态、上次同步、"重新同步模型"按钮、差异对话框），默认折叠，标题旁显示健康状态点。`models.broker.enabled=false` 时整节不显示。
4. 角色告警：`check_roles` 的警告和"某角色的模型不可用"显示在卡片顶部，带"去设置"链接（沿用 `role_policy` 警告里的 `link`）。
5. 文案用中文为主，en-US 提供对应英文。

**验证方式：** 浏览器走查并截图：全绿状态；断开一个 OAuth 账号后对应行变红并出现顶部告警；测试连接成功和失败各一次；展开错误列表；broker 一节里点"重新同步模型"，差异对话框正常、同步成功；`pnpm typecheck && pnpm test` 通过。

**验收标准：**
- 截图齐全（上面 6 种状态）；
- 默认界面里能看到 OpenClaw 同步按钮（在 broker 一节内，展开即见），并且能用；
- 界面任何地方都看不到 token 或 key；
- `ModelTab.tsx` 没有新的上游改动。

**风险与注意：** 卡片每次打开都会查状态接口。`staleTime` 设 30 秒，不要设自动轮询（避免多标签页时频繁查 24 小时的用量 JSON）。

**预估工时：** 1.25 天。

---

## P0N.10 双登录并发测试（OpenClaw 与 LangAlpha 登录同一账号）

**目标：** 证明 OpenClaw（CLI 网关、broker）和 LangAlpha（原生 OAuth）同时登录同一个 Claude、ChatGPT、Google 账号，并发使用、各自经历至少一次 token 刷新后，**双方的 token 都没有失效**。不通过的账号，要改为双方用不同账号，或者只保留一方。

**前置依赖：** P0N.2；OpenClaw 网关和 broker 正常运行；LangAlpha 已登录同一账号。

**改动/新增文件：**
- `scripts/dalton/dual_login_test.py`【新建】
- 输出：`var/dalton/smoke/dual_login_<account>_<UTC>.json`

**具体做法：**

1. 测试对：

   | 账号 | LangAlpha 一侧 | OpenClaw 一侧 |
   |---|---|---|
   | Claude | `claude-opus-5-5-oauth`（claude-oauth） | broker 的 `ocb-claude-cli-gateway-claude-opus-5-5`（`claude` CLI 自己的登录）【待验证：OpenClaw 是否还有使用同一 Claude 账号的其他认证配置，有的话一并纳入】 |
   | ChatGPT | `gpt-6.1-sol-oauth`（codex-oauth） | broker 里走 ChatGPT 订阅的模型【待验证：OpenClaw 的 openai 订阅认证配置名，查 `GET /dalton-broker/v1/models?detail=1` 的 provider 字段】 |
   | Google | `gemini-3.8-flash-low-oauth`（antigravity-oauth） | broker 的 `ocb-antigravity-cli-gateway-*`（`agy` CLI 的登录） |

   Meta（muse）也有 `muse-cli-gateway`，同样纳入，作为第四对。

2. 流程（每个账号独立跑，可以并行）：
   1. 记录双方的初始状态：LangAlpha 一侧 `user_oauth_tokens` 的 `expires_at`、`updated_at`（不读 token 值）；OpenClaw 一侧只记录探测是否成功；
   2. 两侧各起一个循环，每 60 秒发一次 N1（PONG），持续时间至少覆盖**双方各两次刷新**；
   3. 为了不必等满 token 的自然有效期（Google 约 1 小时，其他供应商可能数小时），LangAlpha 一侧通过把测试用户的 `expires_at` 改到当前时间来提前触发刷新（每 20 分钟一次）；OpenClaw 一侧用该 CLI 自己的方式触发刷新【待验证：各 CLI 是否有强制刷新的命令；没有的话就等自然到期，测试时长相应延长，并在报告里写明实际观察到的刷新次数】；
   4. 全程记录每次调用的成功或失败、HTTP 状态、错误类别（如 `invalid_grant`、401、`refresh_token_reused`）；LangAlpha 一侧的刷新结果从 P0N.8 的事件表和 `updated_at` 变化读取；
   5. 结束后再各发一次调用，确认双方都还能用。
3. 判定：
   - **通过**：双方都完成至少两次刷新，全程没有因认证失败而中断的调用（偶发 429、5xx 不算）；
   - **不通过**：任一方出现 `invalid_grant`、刷新令牌被吊销、或必须重新登录。结论写进报告，并按下表处理。

   | 不通过的账号 | 处理 |
   |---|---|
   | Claude | LangAlpha 和 OpenClaw 换成不同的 Claude 账号；或者 lead 改用 `gpt-6-astra-oauth` / `grok-4.7`，并在 owner 确认前不切 |
   | ChatGPT、Google、Meta | 同上，或在 Dalton 一侧关掉该供应商（L17），审稿和子智能体用其他家族 |

**验证方式：** 跑脚本，报告附进 PR；报告里不出现任何 token。

**验收标准：** 四个账号都有报告；每份报告里有双方的刷新次数、失败调用数、最终结论；不通过的账号有处理决定，并已同步到 P0N.5 的默认值和 P0N.8 的开关。

**风险与注意：**
- 改 `expires_at` 只能改测试用户、只在测试期间改，测完确认 `get_valid_token` 已把它写回正常值。
- 订阅供应商可能对同一账号的并发会话数有限制，这本身就是要观察的结论之一；被限流时记录下来，不要绕过。
- 测试会消耗双方的订阅额度，挑非工作时间跑。

**预估工时：** 0.75 天（含等待刷新的时间，大部分是挂机）。

---

## P0N.11 OpenClaw 依赖清单与文档更新

**目标：** 写清楚切换后 LangAlpha 还依赖 OpenClaw 的哪些部分、哪些已经不依赖；更新 README 决定记录、P0 附录、PROGRESS。

**前置依赖：** P0N.2–P0N.9 基本完成。

**改动/新增文件：**
- `docs/dalton/dev-guide/README.md`：§2 架构图的"模型"一行改为"LangAlpha 原生供应商（默认）+ openclaw-broker（`ocb-*`，并行可选）"；§6 写入 §0.3 的决定；§9 的第 2 条"新项目通过它调用模型"改为"broker 并行路径和数据侧仍需要它"；§10 风险表补 §5 的条款风险。
- `docs/dalton/dev-guide/P0-platform.md`：附录 A 增加 `llm-sdk-dispatch`、`oauth-dispatch`、`provider-kill-switch`、`native-oauth-ui`；P0.6 的默认选型表加一行"已被 P0N.5 取代"的说明。
- `docs/dalton/dev-guide/PROGRESS.md`：修正 §3 里"相对基线 `9a03d3ac` 零改动"的结论；登记 P0N 各步。
- `scripts/dalton/check_hooks.sh`：`P0_REQUIRED_HOOKS` 增加三个后端挂钩 ID。

**具体做法：** 依赖清单写进 README §2 下方（或本分册附录，二选一，保持一处），内容如下，并逐项给出核实命令的结果：

| 依赖项 | 切换后是否还依赖 | 说明 / 核实方法 |
|---|---|---|
| OpenClaw 网关 `127.0.0.1:18789` | **模型调用不再需要**（默认路径）；broker 并行路径需要 | 停掉网关后，用默认角色跑一轮完整对话（含子智能体、background、发布审稿）应全部成功；`ocb-*` 报错、状态卡片 broker 一节变红 |
| broker v2 插件 `integrations/openclaw/dalton-langalpha-broker` | 保留，作为并行路径 | 不卸载 |
| `openclaw.json` | LangAlpha 不读（P0 起就不挂载进容器） | 不变 |
| researchdata 桥 `scripts/dalton/researchdata_bridge.py` | 需要 OpenClaw **工作区文件**，不需要网关 | 桥用子进程直接运行 `~/.openclaw/workspace/scripts/researchdata_mcp.py`，读 `~/.openclaw/workspace/config/data_sources.json`。核实：网关停止状态下跑 `scripts/dalton/check_bridge.sh`，6 项全绿；`grep -rn "18789" ~/.openclaw/workspace/scripts/researchdata_mcp.py ~/.openclaw/workspace/scripts/data_gateway.py` 无结果【待验证：`data_sources.json` 各路由调用的脚本里是否有调用 `openclaw` CLI 或网关的，逐个 grep】 |
| AlphaEngine MCP `127.0.0.1:8950`、Guidepoint 代理 `127.0.0.1:8943` | 需要 | launchd 服务 `ai.openclaw.alphaengine-mcp`、`ai.openclaw.guidepoint-mcp-proxy`，由 OpenClaw 的运维管理，独立于网关进程；凭据只在宿主机 |
| 基金 wiki `~/.openclaw/workspace/wiki` | 需要（只读，O8） | researchdata 的 `company-wiki` 源 |
| 旧认知迁移的输入（P1.4） | 一次性，已完成 | 不再依赖 |
| CLI 网关服务（`ai.openclaw.claude-cli-gateway`、`antigravity-cli-gateway`、`muse-cli-gateway`） | 只有 broker 路径需要 | LangAlpha 原生路径直接调用供应商接口，不经过这些网关 |
| OpenClaw 的模型同步（P0.7） | 只影响 `ocb-*` 的目录 | 原生模型目录来自 manifest + `native_overlay.json` |

**验证方式：** 停网关测试（上表第一行）和网关停止状态下的 `check_bridge.sh`，结果附进 PR；测完立即恢复网关（它还服务 OpenClaw 本身）。

**验收标准：** 文档 diff 经 lead 审阅；`check_hooks.sh --strict` 通过；停网关测试的证据齐全。

**风险与注意：** 停网关会影响 OpenClaw 本身的其他任务。和 owner 约定时间窗口，测完马上恢复。

**预估工时：** 0.5 天。

---

## P0N.12 P0N 验收

**目标：** lead 按 §4 的清单逐条验收。

**前置依赖：** P0N.0–P0N.11 全部合入。

**改动/新增文件：** 无（只产出验收记录，写进 PROGRESS.md）。

**具体做法：** 在全新重启的栈上（`docker compose down && up`），用默认配置、不改任何用户偏好，按 §4 顺序执行。

**验证方式：** 见 §4 每一条。

**验收标准：** §4 全部通过；不通过的条目要么修复，要么由 lead 写明接受理由。

**风险与注意：** 验收时用户偏好里不能残留 `ocb-*`（P0N.5 的风险项）。

**预估工时：** 0.5 天（lead）。

---

## 4. 验收清单（lead 照着跑）

| 编号 | 检查 | 怎么查 | 通过条件 |
|---|---|---|---|
| N1 | 上游 manifest 回到原样 | `git diff 8e0dce94 -- src/llms/manifest/` | 空 |
| N2 | 其他被 owner 改过的上游文件 | `git diff 8e0dce94 -- src/llms/extension/__init__.py src/server/app/oauth.py tests/unit/llms/test_manifest_integrity.py web/src/pages/Dashboard/utils/api.ts` | 空 |
| N3 | 挂钩检查 | `./scripts/dalton/check_hooks.sh --strict`（基线 `8e0dce94`，含 `UNHOOKED_EDIT`） | `OK` |
| N4 | 有效目录前后一致 | P0N.2 的两份 `dump_effective_catalog.py` 输出 diff | 只有两个 contributor 键的 `visible` 不同 |
| N5 | owner 的供应商代码位置 | `ls src/dalton/llm/providers/{antigravity,muse}/`；`git grep -n "antigravity_oauth\|muse_oauth" -- src/server src/llms` | 文件在 Dalton 目录；上游目录无引用 |
| N6 | 三个 owner 供应商真实可用 | `model_smoke.py --provider antigravity-oauth`、`muse-oauth`、`xai` | N1、N2、N3 通过 |
| N7 | 冒烟矩阵 | `latest_native.json` | 默认值里每个模型在其角色的阻断项全部通过 |
| N8 | claude-oauth 身份行不影响我们的 system prompt | `latest_native.json` 的 `claude-opus-5-5-oauth` → N6 | pass |
| N9 | 默认配置里没有 `ocb-` | `git grep -n "ocb-" -- agent_config.dalton.yaml src/dalton/dalton.yaml src/dalton/settings.py plugins/dalton/agents/` | 无结果 |
| N10 | broker 仍是可选并行路径 | Settings 里把 lead 切到 `ocb-xai-grok-4.7`，新线程跑通；状态卡片 broker 一节健康、同步按钮可用 | 都成立 |
| N11 | 审稿人家族独立 | `apply_role_models.py --dry-run` 分别以 `claude-opus-5-5-oauth`、`gpt-6-astra-oauth`、`claude-opus-5-5-high-oauth` 为 lead | 审稿人家族都与 lead 不同；第三种情况下不选任何 Claude |
| N12 | 子智能体和 background 实际用的模型 | 一轮带 research 子智能体的对话后查 `conversation_usages` | 有 `grok-4.7`（platform）、`deepseek-flash`（platform）、`claude-opus-5-5-oauth`（oauth） |
| N13 | OAuth 主模型的轮次受现金额度约束 | P0N.6 验证第 1、2 条 | 暂停卡片和 429 都出现 |
| N14 | 现金对账 | `GET /api/v1/dalton/budget/today` 对比 SQL 手算 | 误差 < $0.01；订阅名义 > 0 |
| N15 | 审稿调用日志回放 | P0N.7 验证第 1 条 | 第二次没有新的供应商请求 |
| N16 | 崩溃后不自动重发 | P0N.7 验证第 2 条 | 得到 `indeterminate` 和中文提示；确认后才重发 |
| N17 | 审稿花费计入预算 | P0N.7 验证第 4 条 | 计入 |
| N18 | 供应商开关 | `DALTON_DISABLED_PROVIDERS=antigravity-oauth` 重启后用该供应商模型发消息 | 直接报"已关闭"，没有换模型 |
| N19 | 状态卡片 | P0N.9 的截图和走查 | 6 种状态齐全，测试连接成功和失败都正确显示，无凭据泄漏 |
| N20 | 双登录 | 四份 `dual_login_*.json` | 每个账号有结论；不通过的有处理决定并已落到配置 |
| N21 | 网关停止时默认路径可用 | P0N.11 停网关测试 | 默认角色一轮完整对话成功；`check_bridge.sh` 全绿 |
| N22 | 凭据不外泄 | `grep -rniE "sk-[a-z0-9]|eyJ[a-zA-Z0-9]|ya29\.|refresh_token\"\s*:\s*\"[^\"]" var/dalton/ docs/dalton/` 以及 PR 描述 | 无结果 |
| N23 | 【回归】P0.10 里标为【回归】的项目 | 按 P0.10 | 全部通过 |
| N24 | contributor 变体不可用于任何角色 | 把 `research` 子智能体临时设成 `muse-spark-1.3-contributor-oauth` 后看 `check_roles()` 和状态卡片 | 有红色告警；`available_model_keys` 不含该键 |

---

## 5. 条款与封号风险（owner 已接受）

**风险：** 四个订阅登录都冒用官方客户端的身份与供应商通信：

| 供应商 | 冒用的客户端标识 | 代码位置 |
|---|---|---|
| claude-oauth | `user-agent: claude-cli/2.1.77`、`x-app: cli`、`anthropic-beta: claude-code-…,oauth-…`，并在 system 前插入 Claude Code 身份行 | 上游 `providers.json` 的 `claude-oauth` variant；`src/llms/extension/anthropic_oauth.py` |
| codex-oauth | `originator: codex_cli_rs`、`User-Agent: codex_cli_rs/0.46.0`、`ChatGPT-Account-Id` | 上游 `src/llms/llm.py` `_get_codex_llm`（约第 790–796 行）、`src/server/services/codex_oauth.py` |
| antigravity-oauth | `antigravity/cli/1.4.2 (aidev_client; …)` User-Agent、agy 的 OAuth 客户端 | `src/dalton/llm/providers/antigravity/client.py`（迁移后） |
| muse-oauth | `muse-build/1.4.10 …` User-Agent、`x-client-id: tbh:exec` | `src/dalton/llm/providers/muse/client.py`（迁移后） |

OpenClaw 的 CLI 网关是真的启动官方 CLI 进程，而原生路径是用我们的代码模仿 CLI 的请求，因此被识别、被限流或被封号的风险**更高**。另外，模仿的版本号是写死的，官方 CLI 升级、服务端收紧校验后，可能突然全部失败。

**缓解措施（都已落在上面的步骤里）：**
1. **主模型可切换**（L1）：任何一个订阅出问题，在 Settings 里或 `agent_config.dalton.yaml` 里切到另一家订阅、`grok-4.7`（现金），或者 `ocb-*`。
2. **现金 API 兜底**：`.env` 里常备 `XAI_API_KEY`、`DEEPSEEK_API_KEY`；需要 Claude 或 GPT 的现金路径时配 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`（上游 `anthropic`、`openai` 供应商，键名不带 `-oauth`，例如 `claude-opus-5-5`、`gpt-6-astra`），计入日额度。
3. **注意用量节奏**：工具子智能体默认用现金模型（`grok-4.7`），不让订阅账号承担大量并行调用；冒烟和双登录测试避开工作时间；不做定时探测。
4. **每个供应商有开关**（L17）：一个环境变量即可让某个供应商在所有路径上立刻停用，不会静默换成别的模型。
5. **状态卡片的可见性**：429、401、刷新失败会在卡片上标黄或标红，第一时间发现。
6. **contributor 变体禁用**（L21）：基金研究材料不进入可能用于训练的模型。

---

## 6. 以后如需退役 broker 的前提条件（现在不做任何动作）

只有同时满足以下条件，lead 才会向 owner 提出退役 broker v2 的建议，届时另写步骤：

1. P0N.4 的冒烟矩阵全部通过，并且在之后每次合并上游后重跑仍然通过；
2. P0N.10 的双登录测试四个账号都通过（或者已经确定双方使用不同账号）；
3. 原生路径作为默认路径连续稳定运行两周：期间没有因供应商认证问题导致的研究中断，没有需要切回 `ocb-*` 的情况；
4. owner 明确同意。

在那之前，broker、`ocb-*` 模型、健康检查、同步按钮全部保留并维护。

---

## 7. 新增挂钩汇总（并入 P0 附录 A）

| ID | 文件 | 位置 | 行数 | 步骤 |
|---|---|---|---|---|
| provider-kill-switch | `src/llms/llm.py` | `LLM.get_llm` 开头 | 3 | P0N.2 / P0N.8 |
| llm-sdk-dispatch | `src/llms/llm.py` | `LLM.get_llm` 的 SDK 分派 `else` 分支 | 5 | P0N.2 |
| oauth-dispatch | `src/server/services/llm/clients.py` | `resolve_oauth_llm_client` 的 token 取值分派和请求头分派 | 约 6 | P0N.2 |
| native-oauth-ui | `web/src/hooks/useConfiguredProviders.ts`、`web/src/pages/Settings/panels/ConnectedAccounts.tsx`、`web/src/pages/Setup/steps/connectStep/OAuthConnect.tsx`、`web/src/pages/Setup/steps/MethodStep.tsx`、`web/src/pages/Setup/steps/connectStep/shared.tsx`、`web/src/lib/brandArt.ts` | 见 P0N.3 | 每个文件 ≤ 6 | P0N.3 |
| query-keys（已有 ID，扩充） | `web/src/lib/queryKeys.ts` | `oauth.antigravity`、`oauth.muse`；`dalton.providersStatus`、`dalton.availableModels` | +4 | P0N.3、P0N.9 |
| i18n（已有 ID，扩充） | `web/src/locales/en-US.json`、`zh-CN.json` | `dalton.oauth.*`、`dalton.providers.*` | 若干键 | P0N.3、P0N.9 |

被**删除**的未标注上游改动：`src/llms/llm.py` 的 `_get_muse_llm`、`_get_antigravity_llm` 和两个 `elif` 分支；`src/llms/extension/__init__.py`、`src/server/app/oauth.py`、两个 manifest、`tests/unit/llms/test_manifest_integrity.py`、`web/src/pages/Dashboard/utils/api.ts` 的全部 owner 改动（内容迁到 Dalton 目录，见 P0N.2、P0N.3）。

---

## 8. 工时汇总

| 步骤 | 工时（人天） |
|---|---|
| P0N.0 基线修正与审计 | 0.3 |
| P0N.1 overlay 扩展 | 1 |
| P0N.2 后端迁移 | 1.5（加粘贴兜底时 +0.5） |
| P0N.3 前端迁移 | 1 |
| P0N.4 通用冒烟与矩阵 | 1.5 |
| P0N.5 角色默认值与家族 | 1 |
| P0N.6 预算口径 | 1.25 |
| P0N.7 调用日志 | 1.25 |
| P0N.8 开关与状态后端 | 1.25 |
| P0N.9 状态卡片 | 1.25 |
| P0N.10 双登录测试 | 0.75 |
| P0N.11 依赖清单与文档 | 0.5 |
| P0N.12 验收（lead） | 0.5 |
| **合计** | **约 13–13.5** |

两人并行（一人做 P0N.1、P0N.2、P0N.4、P0N.5、P0N.10；一人做 P0N.3、P0N.6、P0N.7、P0N.8、P0N.9），约 1.5 周。

## 附录：【待验证】汇总

| # | 内容 | 所在步骤 |
|---|---|---|
| 1 | antigravity 的 loopback 登录在容器部署下是否可用；不可用时加粘贴回跳地址的兜底 | P0N.2 |
| 2 | `gemini-2.5-pro-oauth`、`gemini-3-flash-oauth`、muse 三个模型的名义价参照；muse 的估价需 owner 确认 | P0N.2 |
| 3 | antigravity、muse、xai 各模型的工具调用、结构化输出、图片输入（`dalton_meta` 的 `tool_calling`、`image`） | P0N.4 |
| 4 | codex、muse 的服务端是否另加 instructions（N6 的 token 阈值） | P0N.4 |
| 5 | background 槽位是否会带工具调用 | P0N.5 |
| 6 | 子智能体固定使用平台 key 的系统模型（`grok-4.7`）时能否解析（`subagents.py` 第 214–232 行） | P0N.5 |
| 7 | `conversation_usages.token_usage` 是否为 JSONB；`infrastructure_credits` 列名 | P0N.6 |
| 8 | 子智能体的用量是并入父运行的行还是单独成行，及其 `conversation_response_id` | P0N.6 |
| 9 | `ModelResilienceMiddleware` 对 `ProviderDisabledError` 的分类 | P0N.8 |
| 10 | 能否从运行台账只读查询 `model_retry` / `model_fallback` 事件 | P0N.8 |
| 11 | OpenClaw 侧 ChatGPT 订阅的认证配置名；各 CLI 能否强制刷新 token；OpenClaw 是否还有其他配置使用同一 Claude 账号 | P0N.10 |
| 12 | `data_sources.json` 各路由的脚本是否调用 `openclaw` CLI 或网关 | P0N.11 |
