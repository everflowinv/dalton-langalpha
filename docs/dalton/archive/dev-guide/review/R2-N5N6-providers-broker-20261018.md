# R2 N5 原生供应商与模型清单 / N6 broker 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：4fd04531（源码=0e68b9a8，diff 范围 b42da6a7..0e68b9a8）

审查方式：只读。未改代码/配置/文档/数据库，未登录供应商，未打印任何令牌。离线复现用 `uv run python`（真实仓库代码）；broker 侧跑了 `node --test`（13 pass）并只读检查了 `~/.openclaw/managed-plugins/` 与 `~/.openclaw/state/` 的实际文件。后端（colima）未运行；OpenClaw 网关（127.0.0.1:18789）在运行，只做了只读 `/health` GET。

## 结论

**清单搬家（P0N.2）干净：合并目录前后数据级一致，上游文件恢复到位，第一轮 R6 的 8 个代码级问题全部修好并有单测锁定。但新写的"模型供应商状态"服务有一个高危缺陷：「测试连接」探测导入了一个不存在的函数，运行时必失败**——这正是清单警告的"单测全绿、接口一调就死"的形状（探测的 ImportError 在函数体内被 except 吃掉，返回 ok=False 而不是 500，连接口冒烟都拦不住）。另有两处中危：BYOK 供应商检测读错字典形状（黄色警告永远不出现、BYOK 模型被误报不可用），以及探测调用不经 `dalton_complete`（修好 ImportError 后现金探测花费将不入预算）。N6 broker：H1/H2/H20 代码级与证据级全部确认，已安装且在运行的网关版本与仓库逐文件 hash 一致；真实重启验证仍缺。第一轮 R1 的 4 个低危残留未修。

问题计数：严重 0 / 高 1 / 中 4 / 低 10；❓ 3 类。

## 清单搬家前后对比（附脚本与结果摘要）

**脚本**（`/tmp/r2n5/compare_catalog.py`，本次审查用，未入库）：从 git 提取四组文件——A 侧（搬家前）`7670a03f` 的 `src/llms/manifest/{models,providers}.json` + 当时的 `openclaw_overlay.json`；B 侧（搬家后）`8e0dce94` 的两个 manifest + `0e68b9a8` 的 `native_overlay.json` + 现行 `openclaw_overlay.json`。两侧用**同一套合并语义**（照抄 `src/dalton/llm/overlay.py::_merge_single_overlay`：键/组冲突跳过、价格组按 id 追加去重）合并后逐键对比，隔离出"数据搬家"本身的差异。

**结果**：合并后 A=B=92 个模型键、19 个 provider 组、18 个价格组。差异共 **4 处**：

| 差异 | 位置 | 定性 |
|---|---|---|
| `muse-spark-1.3-contributor-oauth` visible true→false | 模型条目 | P0N.2 明示允许的**唯一**语义变化（L21） |
| `muse-wonder-full-1.0-contributor-oauth` visible true→false | 模型条目 | 同上 |
| `openclaw-broker` 组少 `operator_channel:"system"` | overlay 重生 | 非搬家路径：`openclaw_overlay.json` 在两次 dump 之间被同步按钮重新生成，对比两个 overlay 文件确认差异源头在此 |
| `antigravity-cli-gateway/gpt-oss-120b` 的 `dalton.family` `openai-gpt-oss`→`openai-gpt` | overlay 重生 | 同上，L20 家族口径变化经同步脚本带入 |

搬家数据本身（23 个模型键、`provider_config.{antigravity,meta,xai}` 三组、价格组 `models.xai`）**逐字节相同**（脚本断言 full-equality）；7670a03f 相对 8e0dce94 的 manifest 恰好只加这 23 键、三组、一组价，无删除、无对既有键的改动，`infrastructure_pricing`/`credit_conversion` 也未动。

**开发者证据文件的口径**：`var/dalton/catalog_before_p0n1.json`、`catalog_7670a03f.json`、`catalog_after_p0n2.json` 均为 `scripts/dalton/dump_effective_catalog.py` 的输出（构造 `ModelConfig()` 后导出 llm_config/flat_providers/pricing_models；前后两个 7670 时点文件内容完全相同）。`diff(before_p0n1, after_p0n2)` 与我的独立脚本结论一致：同样只有上述 4 处。两法互证。

## N5 逐项

### 1. 清单搬家前后一致（P0N.2 验收） — ✅
见上节。合并目录一致，差异仅 contributor visible×2（明示允许）+ overlay 重生×2（与搬家无关）。

### 2. 上游文件恢复 — ✅
`git diff 8e0dce94 0e68b9a8 -- src/llms/`：**只剩 `src/llms/llm.py`**（+22/-2），diff 里恰好 5 个挂钩块，逐一对上 P0N §7 清单：`llm-overlay`×2（P0.4 既有）、`llm-broker-client`（P0.3 既有）、`provider-kill-switch`（P0N.2 新增，`get_llm` 开头）、`llm-sdk-dispatch`（P0N.2 新增，else 分支派发 `build_dalton_client(self, cache_key=cache_key)`，传**原始** cache_key——R6-10 的修复点）。`src/llms/manifest/`、`src/llms/extension/`、`src/server/app/oauth.py`、`tests/unit/llms/test_manifest_integrity.py`、`web/src/pages/Dashboard/utils/api.ts` 对 8e0dce94 全部零差异。`clients.py` 的 `oauth-dispatch` 挂钩两处齐全（token getter 分派 + 请求头分派，防止给 Google 请求加 `ChatGPT-Account-Id`）。

### 3. R6-2 ~ R6-13 逐项
| 项 | 结论 | 依据 |
|---|---|---|
| R6-2 订阅失效 403 不可重试 | ✅ | `antigravity/client.py:73-80` 异常类同时带 `.status`/`.status_code`；`_error_from_status`（:365-390）把 SUBSCRIPTION_REQUIRED 映射为 `status or 403`。离线复现：无外层状态码的 SUBSCRIPTION_REQUIRED → `status_code=403` → `extract_status_code`=403 ∈ `NON_RETRYABLE_STATUSES`（`src/llms/error_classification.py:17`），不再进重试梯。有单测（test_native_overlay.py:201） |
| R6-3 functionResponse 名称回退 | ✅ | `_messages_to_gemini`（:151-186）先从前序 AIMessage 的 tool_calls / tool_use 块建 id→name 映射，ToolMessage 无 name 时用映射，最后才退 id。离线复现：ask_user 的 ToolMessage 无 name → functionResponse.name=`ask_user` |
| R6-4 同帧 thought+text 翻倍 | ✅ | `_chunks_from_frame`（:460-500）thought 只进 reasoning chunk，text chunk 不再携带 `reasoning_content`。离线复现：reasoning_content=`'THOUGHT'`（一次）、content=`' ANSWER'` |
| R6-5 / N5-4 Docker 登录粘贴兜底 | ✅ | `oauth_native.py:200-209` `POST /api/v1/oauth/antigravity/callback`；`antigravity/oauth.py:256-328` 解析完整回调 URL/查询串/`code#state`，从 Redis 取 verifier/state 换 token。前端两处都有粘贴框：Setup `AntigravityConnect.tsx:217-234`（active 轮询阶段显示）、Settings `AntigravityAccountRow.tsx`（:230 附近）。跨 worker 成立（verifier 在 Redis 600s） |
| R6-6 测试缺失 | ✅（探测路径除外） | `test_native_overlay.py` 新增 9 个测试锁签名清零/不翻倍/名称回退/status_code/回调解析/muse 会话头/kill switch/碰撞跳过/双客户端构造，15 项全过。**但 probe 服务路径无任何测试**——高危缺陷 #1 因此漏网 |
| R6-7 state 校验 fail-closed | ✅ | loopback（oauth.py:380-383）与粘贴路径（:311-314）都要求 state 非空且相等，否则 400/ValueError |
| R6-8 Redis 死写 | ✅（转为有意设计） | verifier/state/port 仍写 Redis，但现在被粘贴兜底跨进程读取（P0N.2 §7 设计如此），TTL 600s，不再是死写 |
| R6-9 二次登录命中旧标记 | ⚠️ 部分 | `start_login` 先删 `done` 键（oauth.py:235）✅；但 `check_login_complete` 的 Redis 不可用 DB 回退（:415-417）仍只看 token 存在、不比 `initiated_at`——Redis 宕机时二次登录仍可能立刻假成功。残留低危 #7 |
| R6-10 Muse 会话亲和头 | ✅ | `llm.py` 挂钩传原始 cache_key → `build_muse_client`（muse/client.py:46-51）设 `x-tbh-session-id`/`x-meta-ai-gateway-session-id`（uuid5 稳定派生），有单测锁定 |
| R6-11 slow_down 退避 | ❌ 未修 | `MuseAccountRow.tsx:72`、`MuseConnect.tsx:46` 仍固定 interval；后端 poll 把 slow_down 当 pending 返回，前端无从区分。低危 #8 |
| R6-12 brandArt | ⚠️ 部分 | `brandArt.ts:48-49` 加了 `antigravity-oauth`（带 DALTON-HOOK）；`muse-oauth`、`xai` 仍缺。低危 #9 |
| R6-13 usage 口径 | ✅ | `_usage_metadata`（:540-547）output = candidates + thoughts，测试断言 5+7=12；total 对上 in+out |
| H14 思考签名（R6-1，第一波已确认） | ✅ | `_stream`/`_astream` 入口 `self._signatures = []`（:606-607, 622-623）；回放改从消息 `additional_kwargs` 取签名；单测锁第二轮不携带上轮签名 |
| 令牌加密与刷新锁与上游一致 | ✅ | 两家 oauth 均走共享 `upsert_oauth_tokens`（pgp_sym）与 `oauth_refresh_wait`（SETNX + owner-token 释放 + TTL + 等待方轮询 DB），与 codex/claude 同构；刷新失败记日志返回 None。antigravity/muse 另写 `refresh_ok/refresh_fail` 事件进 `dalton_provider_events`（P0N.8） |

### 4. Docker 部署下 Antigravity 登录 — ✅（代码与 UI 齐备，真实 Docker 登录未实测 ❓）
见 R6-5 行。粘贴兜底覆盖 loopback 不可达的场景；Redis 不可用时粘贴路径 fail-closed（RuntimeError）。真实容器内走一遍仍无证据（后端没起）。

### 5. 模型供应商状态卡片与接口 — ❌（一个高危 + 两个中危）
- **高危 #1**：`provider_status.py:402` `from src.server.services.llm.clients import resolve_llm_client` —— 该函数**不存在**（全仓库唯此一处引用；实际名为 `resolve_model_client`，clients.py:503）。离线证实 `ImportError`。它在 try 块内被 `except Exception` 吃掉，按 probe_fail 返回 `error_kind="ImportError"`——「测试连接」对任何供应商都必失败。`test_router_wiring.py` 只测路由不 500，且探测端点返回 200（ok=False），所以测试全绿。
- **中危 #2**：BYOK 检测读错字典形状。`get_user_api_keys` 返回 `{byok_enabled, keys:{provider:key}, base_urls}`，而 `provider_status.py:196-197` 与 `role_policy.py:204-206` 都写 `{k for k,v in rows.items() if v}`——得到的是 `{'byok_enabled','keys','base_urls'}` 而非供应商名。后果：`byok_cash_key` 黄色警告永远不触发；BYOK 配置的现金供应商显示"未配置"；`explain_model_availability` 对仅 BYOK 的模型误报 `api_key_missing` 不可用（进而影响 `available_model_keys` 与审稿人候选，方向保守但不正确）。未泄露密钥值（只做了真值判断）。
- **中危 #3**：`probe_provider`（:399-409）直接 `resolve_llm_client(...)`+`ainvoke`，不经 `dalton_complete`——无准入、无调用日志、花费不入预算。订阅模型现金为 0 无碍；现金供应商（xai grok-4.3、deepseek-flash）每次点击都是无记账的真实花费。当前被 #1 掩盖，修 #1 后即暴露。
- 低危 #6：`ok = "PONG" in text.upper() or bool(text)`（:413）——任何非空回复都算成功，与设计"回复是否为 PONG"不符。
- 不泄露令牌 ✅：状态接口只回 email/plan_type/expires_at/seconds_left（OAuth）或 env_key 名 + 布尔（API key）；错误文本过 `sanitize_error_text`（正则 5 类凭据 + broker token 替换 + 截断 200）；前端 ProviderRow/StatusCard 不渲染任何凭据值；`staleTime 30s` 无自动轮询，符合设计。
- 多 worker ✅：三接口无进程内状态，全部查 Postgres/Redis/env/配置文件。
- 吞错（低危 #10）：`_record_provider_event_safe`（antigravity/muse oauth 两处）`except Exception: pass` 零日志；`_fetch_usage_stats_24h` 内层 journal 查询 `except: pass`。事件缺失时无迹可查。

### 6. Muse contributor 变体隐藏与禁用（L21） — ✅（清单与 lead/subagent/reviewer 路径）/ ❌（background/assist 缺口，见 #4）
两处都查了：`native_overlay.json` 两个 contributor 键 `visible:false` + `dalton_meta.forbidden_for_roles:true`；`role_policy.is_forbidden_for_roles`（:156-167）meta 标记 + 键名含 "contributor" 双保险；`explain_model_availability` 给出 `forbidden_for_roles`+`hidden` 两个不可用理由；`select_independent_reviewer` 跳过；`check_roles` 对 lead 与全部子智能体给出红色 `role_forbidden_model` 警告。有单测（test_role_policy_native.py:74）。
**缺口**：`check_roles`（:317-431）只校验 lead、subagents、reviewer——`llm.background`、`llm.assist`、`llm.compaction`、`llm.fetch` 四个槽位完全不校验（forbidden/text-only/可用性都不查）；`resolve_assist_model`（assist.py:56-97）接受 agent_config.dalton.yaml 里写的任何键。手工把 `llm.background` 或 `llm.assist` 设成 contributor 变体会静默生效——基金材料（专家访谈）可能进训练贡献模型，L21 对这几个角色是空的。provider_status 的 `_load_role_model_assignments` 会把它们列进 `used_by_roles` 展示，但告警链路不通。

### 7. role_policy 对 assist 角色的校验 — ❌（坐实第一波发现）
坐实：`check_roles` 全函数（role_policy.py:317-431）无 assist/background/compaction/fetch 任何字样；R2 清单 §3.2 要求"确认 role_policy 校验了 assist 角色"不成立。影响同上条：assist 默认 `gemini-3.8-flash-low-oauth`（订阅、现金 0）本身没问题，但该角色对禁用变体/仅文本/不可用模型零防护，且状态卡片的 `used_by_roles` 会展示 assist 却不给它的任何告警。

## N6 逐项（broker，可选路径）

### 1. 落盘权限、保留期限、不存请求原文 — ✅（权限与脱敏实证）/ ⚠️（JSONL 无清理，低危 #11）
- **权限**：`journal.mjs` 目录 `mkdir 0o700`；`invocations.json` 原子写（tmp(0600)+chmod+rename）；JSONL `appendFile mode 0o600`。**实测** `~/.openclaw/state/plugins/dalton-langalpha-broker/`：`broker.token`、`invocations.json`、`journal/broker-2026-10-18.jsonl` 全部 `-rw-------`，目录 `drwx------`。
- **不存请求原文**：`invocations.json` 条目字段 = `{invocationId, reqHash(sha256), model, status, startedAt/completedAt/failedAt, updatedAtMs, responsePayload}`——请求只存哈希；responsePayload 是完整响应（幂等回放所需，设计允许，0600 落盘 24h TTL）。JSONL 实测 94 行，全部顶层键为 `ts/schema/request_id/endpoint/stream/model_ref/provider/api/auth_mode/status/finish_reason/usage/cost_usd/latency_ms/prompt_sha256/response_sha256/error_code/error_message(截500)/caller_meta`——无 headers、无 token、无消息原文；`append` 前过 `scrubSecretsDeep`（13 类键剔除）。
- **保留**：`invocations.json` 有 TTL 24h + 500 条 LRU 剪枝（completed/failed_no_output 先淘汰）；**但 `journal/broker-<day>.jsonl` 按天文件从不删除**（全源码无 unlink/prune 调用）——单行只有状态元数据、增长慢，仍属无界。低危 #11。

### 2. 内存缓存上限与过期 — ✅
`invocationCache`：默认 `maxCacheEntries=500` LRU（get 触发换位）+ completed/failed 24h TTL + 超限先淘汰终态再按插入序；pending/indeterminate 不被 TTL 淘汰（正确），仅在总量超 500 时按序淘汰（maxConcurrent=8，pending 数量天然很小，可接受）。`replayStore`：512 条 + 30min TTL。models.mjs auth 缓存 60s TTL（键数=目录模型数，天然有界）。invoke.mjs 的模块级 `idempotencyCache` Map 只在 journal 未注入时兜底（生产 plugin-definition 总是建 journal）。

### 3. 已安装版本与仓库一致性 — ✅（active 一致；另有两个陈旧目录）
`~/.openclaw/managed-plugins/` 下有三个 dalton-langalpha-broker 目录，逐文件 sha256 对比 `git show 0e68b9a8:...` 与 `b42da6a7:...`：
- **`dalton-langalpha-broker-b42da6a72b58`（openclaw.json load.paths 里唯一加载的）**：14 个文件**全部与 0e68b9a8 一致**，且无多余/缺失文件（find 对比为空）。目录名虽是修复前提交的哈希，内容已更新为修复版。
- `dalton-langalpha-broker-3e9cc996920e`（未加载）：invoke/journal/plugin-definition 三个文件停在 b42da6a7（修复前）。
- `dalton-langalpha-broker-c81151b30132`（未加载）：invoke/openai-map/plugin-definition 与两个提交都不匹配（中间开发态）。
- 运行中的网关（127.0.0.1:18789，started 2026-10-18T17:39:55Z）`/health` 已暴露新版契约（`journal:{pending,indeterminate,...,write_failures}`、`inflight/queued/limits/brokerVersion/openclawVersion`），`journal_entries=14` 与磁盘 invocations.json 条数一致，`write_failures=0`——**运行的就是修复版代码**。陈旧目录建议择机清理（本次未动）。

### 4. 第一轮 R1 低危项抽查（≥一半，R1 共 5 低，抽了 5 中的全部相关项）
| R1 项 | 结论 |
|---|---|
| R1-8 journal append 吞错 | ✅ 已修：`writeFailures` 计数器 + `/health.journal.write_failures` 暴露（实测 0） |
| R1-12 回放不写 journal | ✅ 已修：invoke.mjs:336-343 回放路径 append `status:"replayed"` |
| R1-9 无排队（maxQueued） | ❌ 未修：limits.mjs 仍无队列，超限立即 429 `broker_overloaded`，`queued:0` 写死（低危 #12） |
| R1-10 make_model_key 折叠碰撞 | ❌ 未修：openclaw_catalog.py:57-61 折叠后仍无查重/告警（低危 #13） |
| R1-11 冒烟缺项 | ⚠️ 部分：B8 已补第二判据且转 pass；B9/B15/B16 仍未实现，`--max-spend-usd`（openclaw_smoke.py:747）仍只定义未使用（低危 #14） |
| R1-3 请求哈希覆盖面 | ✅ 基本修：reqHash 现含 temperature/max_tokens/reasoning_effort(三来源)/response_format/stop；残余：top_p/seed 等仍不进哈希（低危 #15） |
| 调用 ID 生成（确认无退化） | ✅ broker_http.py 仍为 `la1:` sha256(scope+body)[:40]，同字节同 ID、无 gate 时随机 uuid4 |
| 白名单 / 推理映射 / 家族判定（无退化） | ✅ checkModelAllowlist 三重校验仍在（invoke.mjs:155-178）；REASONING_EFFORT_MAP+clampThinkingLevel 未动且 B8 实测 low/high 回显不同；FAMILY_PREFIX_TABLE 按 L20 更新（gpt-oss→openai-gpt），gpt-oss 家族改动经 overlay 同步落地与设计一致 |

### 5. H1/H2/H20 — ✅ 代码级 / ✅ 证据级（H20）/ ❓ 真实重启
- H1（journal 落盘 + 409）：发送前写 `pending` 并同步落盘（invoke.mjs:481-488）；`completed` 带 responsePayload 落盘；重启遗留 `pending` 在 journal 加载时转 `indeterminate`（journal.mjs:143-151）；同 ID 重试遇 pending/indeterminate → 409 `idempotency_indeterminate` 不重发（:371-378）；`failed_no_output` 放行重发；同 ID 不同 reqHash → 409 `idempotency_conflict`（14:21 冒烟实测 B13：replay 200 + conflict 409 双 pass）。并发同 ID：第二个请求读到 pending 即 409，无双飞。
- H2（断开续传）：`onClientClose` 只置 `clientDisconnected`，**不 abort** 上游；事件流消费到完成，completed 照常落盘（流式分支 :740-763），同 ID 重试拿回放；超时兜底 `defaultTimeoutMs 180s / maxTimeoutMs 600s`。✅ 代码级。
- **真实重启/断开实测仍缺**：网关在跑，但重启网关超出只读审查范围；磁盘 14 条 invocation 全是 completed，没有 indeterminate 样本可佐证重启转换路径。❓
- H20（B8）：`var/dalton/smoke/latest_broker.json`（2026-10-18T17:23:00Z，13 pass/0 warn/0 fail）B8=**pass**，`dalton_meta_low="low"` ≠ `dalton_meta_high="high"`，`low_reasoning/high_reasoning` 同样回显——回显契约统一到 `dalton_meta.reasoning_effort` + `openclaw.reasoning`，第一轮"恒 warn"已修。同日 14:21 的旧记录 B8 还是 warn(null,null)、B13 pass(409)，时间线与修复吻合。注意 17:23 这轮是缩减矩阵（6 模型×B1/B2 + B8），B13 未在其中，引用证据时以 14:21 轮的 B13 为准。
- broker 单测：`node --test` 13 pass / 0 fail（与声称一致）。

## 问题（按严重程度）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | `src/dalton/services/provider_status.py:402-404` | 「测试连接」探测导入不存在的 `resolve_llm_client`（实际函数名 `resolve_model_client`，clients.py:503） | 任何用户点任何供应商的"测试连接"：函数体内 ImportError 被 except 吃掉 → 返回 `ok:false, error_kind:"ImportError"`，永远失败。`uv run python -c "from src.server.services.llm.clients import resolve_llm_client"` 实测抛 ImportError；test_router_wiring 只测路由≠500，探测 200(ok=False) 拦不住 | 改 import `resolve_model_client` 并按其真实签名 `(user_id, model_name, *, is_byok, ...)` 调用；补一个断言 probe ok=True（stub 客户端）的单测 |
| 2 | 中 | `src/dalton/services/provider_status.py:196-197`；`src/dalton/llm/role_policy.py:204-206` | BYOK 检测把 `get_user_api_keys` 的返回 `{byok_enabled, keys:{...}, base_urls}` 当 provider→值 遍历，得到 `{'byok_enabled','keys','base_urls'}` 而非供应商名 | 设置页配了 BYOK key 的现金供应商：状态卡显示"未配置"、`byok_cash_key` 黄色警告永不出现；`explain_model_availability` 误报 `api_key_missing` 不可用 → `available_model_keys` 错误排除该模型（审稿人候选受影响） | 两处都改为 `byok_providers = {p for p,v in (rows.get("keys") or {}).items() if v}`（仅取键名，不碰值） |
| 3 | 中 | `src/dalton/services/provider_status.py:399-409` | 探测调用不经 `dalton_complete`：无预算准入、无调用日志、花费不入账 | 修好 #1 后：每次点击"测试连接"对 xai(grok-4.3)/deepseek 等现金供应商产生真实计费调用，日额度与对账均看不见（违反"服务端 LLM 调用一律 dalton_complete"的 S3 口径） | probe 改走 `dalton_complete`（purpose="provider_probe"，scope_ref 带 provider+时间或随机避免回放）或至少把 usage/cost 写一条 `dalton_llm_journal` |
| 4 | 中 | `src/dalton/llm/role_policy.py:317-431`；`src/dalton/llm/assist.py:56-97` | `check_roles` 不校验 `llm.background/assist/compaction/fetch`（forbidden/text-only/可用性都不查）；`resolve_assist_model` 接受配置里的任意键 | 手工把 `agent_config.dalton.yaml` 的 `llm.assist` 或 `llm.background` 设为 `muse-spark-1.3-contributor-oauth`：无任何告警，assist_json（发布数字标注、预检分诊都会用到）把基金研究材料发给训练贡献模型，L21 对这些角色形同虚设 | check_roles 补四个槽位的 forbidden/text_only/unavailable 检查；`resolve_assist_model` 解析结果过 `is_forbidden_for_roles` 时抛错或回退默认 |
| 5 | 中 | `integrations/openclaw/dalton-langalpha-broker/src/replay-state.mjs:9` | R1-7 未修：回放状态 TTL 30 分钟（设计 24h）、仅内存不持久化 | 网关重启或两轮工具调用间隔 >30min：Gemini thoughtSignature 丢失，多轮工具回传可能被上游拒。broker 已降为可选路径、原生路径不受影响，故维持中危偏保守 | TTL 提到 24h 并随 state 目录持久化（0600、有上限），或明确写进决定记录接受现状 |
| 6 | 低 | `src/dalton/services/provider_status.py:413` | `ok = "PONG" in text.upper() or bool(text)`——任何非空回复都算探测成功 | 模型回复任意内容（如错误文案）时测试连接显示成功 | 拆开：PONG 命中为 ok，非 PONG 单独标 `unexpected_reply` |
| 7 | 低 | `src/dalton/llm/providers/antigravity/oauth.py:415-417` | R6-9 残留：Redis 不可用时 `check_login_complete` 的 DB 回退只看 token 存在，不比本次 `initiated_at` | Redis 宕机 + 10 分钟内二次连接：首轮询立刻假成功 | DB 回退要求 `updated_at` 晚于 initiate 时间（initiate 时把时间写进 Redis/请求上下文） |
| 8 | 低 | `web/src/pages/Settings/panels/dalton/oauth/MuseAccountRow.tsx:72`；`MuseConnect.tsx:46` | R6-11 未修：设备流 slow_down 不加大轮询间隔，固定 5s 一直到 10 分钟超时 | Meta 限流时持续高频轮询 | poll 响应携带 slow_down 信号，前端间隔 +5s（RFC 8628） |
| 9 | 低 | `web/src/lib/brandArt.ts` | R6-12 部分：`muse-oauth`、`xai` 仍无 brandArt 条目，UI 回退默认图标 | 纯外观 | 补两个条目（muse 可暂用通用 mark） |
| 10 | 低 | `antigravity/oauth.py:58-69`、`muse/oauth.py:39-50`、`provider_status.py:157-158` | 吞错：`_record_provider_event_safe` 裸 `except: pass`（零日志）；usage 统计里 journal 查询 `except: pass` | 事件/统计静默丢失，状态卡数据缺也无迹可查 | 至少 `logger.debug/warning` 一次 |
| 11 | 低 | `integrations/.../broker/src/journal.mjs:172-173` | `journal/broker-<day>.jsonl` 按天文件从不清理（invocations.json 有 TTL/上限，JSONL 无） | 长期运行的网关 journal 目录线性膨胀（单行仅元数据，慢） | 启动或写入时删除超过保留期（如 30 天）的日文件 |
| 12 | 低 | `integrations/.../broker/src/limits.mjs`；`invoke.mjs:197-204` | R1-9 未修：无排队，超 maxConcurrent 立即 429 `broker_overloaded` | 并行子智能体瞬时 >8 时第 9 个起直接 429 | 简单 FIFO + 等待超时；或写进决定记录接受 |
| 13 | 低 | `src/dalton/llm/openclaw_catalog.py:57-61, 396` | R1-10 未修：`make_model_key` 折叠后无查重，不同 ref 可映射同一 `ocb-` 键静默覆盖 | 目录同时出现 `openai/gpt_6` 与 `openai/gpt-6` 时少一个模型 | 折叠后查重，重键加后缀或写 `meta.warnings` |
| 14 | 低 | `scripts/dalton/openclaw_smoke.py:747` | R1-11 残留：`--max-spend-usd` 接受但从未使用；B9/B15/B16 仍未实现（B0-B8/B10-B14 已实现） | 全量冒烟花费无护栏；缓存计费、对账、回退三项检查从未跑 | 累计 usage.cost_usd 超限即终止；补三项或登记放弃 |
| 15 | 低 | `integrations/.../broker/src/invoke.mjs:288-303` | R1-3 残余：reqHash 仍不含 `top_p/seed/frequency_penalty/presence_penalty` 等 | 同 ID 复用且仅这些字段不同时返回旧缓存而非 409（LangAlpha 主路径按完整请求体派生 ID 不受影响） | 哈希覆盖除 `stream/stream_options/X-Dalton-*` 外全部字段 |

## 已确认没问题的关键点

- **清单搬家数据级一致**：23 键/3 组/1 价组逐字节相同；贡献者 visible×2 是 P0N 明示的唯一允许变化；开发者三份 catalog 证据与独立脚本互证。
- **上游恢复**：`src/llms` 仅剩 llm.py 的 5 个已登记 DALTON-HOOK；manifest、extension、oauth.py、manifest 单测、Dashboard api.ts 全部回到 8e0dce94 零差异。
- **R6 修复族**（2/3/4/7/8/10/13 + H14）：全部修复、全部离线复现、全部有单测（15 passed）。
- **令牌安全**：pgp 加密入库、共享 Redis 刷新锁与上游同构；状态接口/卡片/日志/事件表无任何令牌值（`sanitize_error_text` 5 类正则 + broker token 替换 + 截断；`dalton_provider_events` 写入前递归剔除 token/secret/api_key 类键）。
- **粘贴回退**：后端路由 + Setup + Settings 三处齐全，state fail-closed，verifier 跨 worker 经 Redis（600s TTL）。
- **contributor 双保险**：清单 visible:false + dalton_meta.forbidden_for_roles + 键名兜底；lead/subagent/reviewer/available_model_keys/审稿选择五处全部排除。
- **broker 落盘安全（实测）**：0600/0700 权限、原子写、请求只存哈希、scrubSecretsDeep、94 行 JSONL 无凭据键；内存缓存三处均有界带 TTL。
- **安装一致性**：active 插件目录 14 文件全部 == 0e68b9a8；运行中网关 /health 暴露新 journal 契约且 journal_entries 与磁盘一致、write_failures=0。
- **H1/H2 代码级 + B13 实测**（replay 200 / conflict 409）；**H20 B8 pass**（low≠high 回显）；调用 ID/白名单/推理映射/家族判定无退化。
- **多 worker**：三个新接口无进程内状态；OAuth 完成标志在 Redis/DB；`ModelConfig` 单例只是配置缓存，stamp 失效即重建。

## 疑似问题（未能确认）

- **H1/H2 真实重启/断开续传无实测**：网关在运行（且已确认运行的是修复版代码），但只读审查不能重启它制造 pending 遗留；磁盘 14 条 invocation 全为 completed，没有 indeterminate 实例佐证。进入 P0.9/P0.10 前应补一次真实的"长调用中途重启 + 同 ID 重试"和"流式中途断开 + 同 ID 重试"。
- **三个原生供应商真实调用零证据**：`scripts/dalton/model_smoke.py`（P0N.4）从未创建，`var/dalton/smoke/latest_native.json` 不存在——antigravity 4 轮工具调用、muse 流式、grok-4.7 对话、Docker 内登录均无真实运行记录（后端没起）。P0N.12 验收项 N6-N8 目前无从谈起。
- **探测/状态接口的真实运行行为**：后端未运行，#1/#2/#3 只做了静态与离线 import 验证；起后端后应各点一次复核。
- **端点对多余/错误 thoughtSignature 的行为**（R6 遗留）：仍未知，需一次真实多轮工具调用。
- `~/.openclaw/managed-plugins/` 下两个未加载的陈旧 broker 目录（3e9cc996 含 3 个修复前文件、c81151b3 含 3 个中间态文件）：不影响运行（load.paths 未引用），建议下次运维时清理，避免日后误启用。
