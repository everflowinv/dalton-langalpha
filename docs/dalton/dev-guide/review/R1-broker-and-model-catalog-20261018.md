# R1 broker v2 与模型清单 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

范围：`integrations/openclaw/dalton-langalpha-broker/**`、`scripts/dalton/{install_broker_v2.sh, broker_spike_client.py, sync_openclaw_models.py, openclaw_smoke.py, apply_role_models.py}`、`src/dalton/llm/*`、`src/dalton/app/router.py`（P0.7 接口）、`src/dalton/services/openclaw_admin.py`、上游挂钩（`src/llms/llm.py`、`src/server/app/api_keys.py`；`clients.py` 无挂钩）。对照 P0-platform.md P0.2–P0.7、README 决定 L1/L2/L3/L16、P0F-acceptance-fixes。

证据：broker Node 测试 10 项本次复跑全部通过（`node --test test/*.test.mjs`）；消息转换、流式工具拼装、overlay 合并、家族判定均用离线片段在真实文件上复核；运行证据取自 `var/dalton/smoke/latest_broker.json`（2026-10-18T14:21:37Z，13 pass / 1 warn / 0 fail）。

## 结论

一句话：**模型目录、消息转换、认证、白名单、text-only 拒绝、overlay 合并、家族判定这些"静态契约"扎实；但 P0.3 §5 设计的幂等 journal 状态机基本未实现（无"进行中"记录、回放缓存只在内存、客户端断开即中止上游），"不重复付费"只在网关进程存活期内成立。进入 P0.9/P0.10 真实运行验收之前，应先修问题 1、2，并解决 B8（P0F.6）的回显缺失。**

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | `integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs:32, 261-324, 491-498, 582-596`（对照 `src/journal.mjs:41-64`） | 幂等回放状态只有进程内存 `idempotencyCache`，P0.3 §5 的 journal 状态机整体缺失：发送前不写 `pending`；`completed` 响应不落盘；崩溃/重启后同 ID 重试会**重新调用供应商（重复付费）**，设计要求的"崩溃遗留 pending → 409 `idempotency_indeterminate` 不重发"不存在；并发同 ID 的两个请求都在飞行时均放行（各自调供应商，后完成者覆盖缓存） | 网关重启（设计明确预期的运维事件，P0.3 §11）后 LangAlpha 用同一 `X-Dalton-Invocation-Id` 重试：`idempotencyCache` 已空 → 全额重新计费。B13 只验证了单进程内存回放 | dispatch 前原子 append `{id, requestHash, state:"pending"}`；完成/失败时更新同一 JSONL 或独立状态文件；启动/请求时读 journal：`completed` 同哈希回放、`pending` 返回 409 不重发；`failed`（无输出）允许重发。响应缓存落盘 0600 并设 TTL |
| 2 | 高 | `integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs:215-221` | 客户端断开即 `abortController.abort()` 中止上游生成，与 P0.3 §5"断开后继续消费上游流直到完成、写入 completed，重试直接拿回放不重复付费"相反 | 流式中途网络抖动断开 → 上游中止（已生成 token 已计费）→ 调用方同 ID 重试 → 重新调用供应商，付两次钱 | 断开时不 abort 上游（保留 `timeoutMs` 兜底），继续消费 `eventStream` 至完成并写入缓存/journal；只有超时和显式取消才 abort |
| 3 | 中 | `integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs:263-270` | 请求哈希只覆盖 `model/messages/tools/tool_choice`，不含 `temperature/max_tokens/reasoning_effort/response_format` 等 | 同 ID 复用 + 仅 `reasoning_effort` 不同（low→high）：reqHash 相同 → 返回低档缓存的旧答案而非 409。LangAlpha 自身按完整请求体派生 ID（`broker_http.py`）所以主路径不受影响，但"同 ID 不同内容必须拒绝"对外部调用方不成立 | reqHash 覆盖除 `stream/stream_options/X-Dalton-*` 外的全部请求体字段（P0.3 §5 原文语义） |
| 4 | 中 | `src/dalton/services/openclaw_admin.py:105-115` 对 `src/plugin-definition.mjs:153-165` | 健康探测读 `/health` 的 `journal.pending/inflight/queued/limits.maxConcurrent/brokerVersion/openclawVersion`，broker 的 `/health` 只返回 `active_requests/max_concurrent/configured_models_count/token_configured/plugin_id/version`——字段全部对不上 | `degraded`（有未决调用、剩余容量<10%）判定永远不触发（pending 恒 0）；`broker` 元数据恒 null。P0.7 §3.3 契约漂移，也是问题 1 的可见层 | 两侧统一：broker `/health` 增加 `journal:{pending}`、`inflight/queued/limits`（修问题 1 后自然有数据）；探测端字段名对齐 |
| 5 | 中 | `integrations/openclaw/dalton-langalpha-broker/src/journal.mjs:41-64`、`src/invoke.mjs:32` | 日志无限增长：无 `journalTtlMs/journalMaxRecords/journalMaxBytes` 剪枝（P0.3 §5 有明确数值），按天 JSONL 只增不减；`idempotencyCache` 保存完整响应负载，无上限、无 TTL | 长期运行的网关进程：journal 目录线性膨胀；内存里缓存所有历史响应（大响应 × 长上下文 = 显著内存） | 实现三参数剪枝（只删 completed）；`idempotencyCache` 改 LRU + TTL（或随 journal 持久化后直接读文件） |
| 6 | 中 | `integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs`（全文件 grep 无 `X-Dalton-Fallback`/`fallbackFrom`）；`scripts/dalton/openclaw_smoke.py` 无 B16 | P0.3 §6 的显式回退完全未实现：请求头 `X-Dalton-Fallback`、`openclaw.fallbackFrom` 回显、回退事件日志、B16 冒烟项都没有。L3 前半句成立（`agent_config.dalton.yaml:11` `fallback: []`，broker 任何失败原样报错不换模型），但"打开时只在供应商故障且未输出内容时回退并告知实际模型"这条路径无处落地 | 调用方想按 P0.3 §6 请求回退时无机制可用；若改用 LangAlpha 上游 `ModelResilienceMiddleware` 的 fallback 列表，语义（是否限制在未输出内容时）是上游行为，Dalton 未验证 | 按 P0.3 §6 补 `X-Dalton-Fallback` 处理（白名单交集 + 无输出才切 + `fallbackFrom` 回显）并补 B16；或明确决定放弃 broker 层回退、写进决定记录 |
| 7 | 中 | `integrations/openclaw/dalton-langalpha-broker/src/replay-state.mjs:9` | 回放状态缓存 TTL 30 分钟（P0.3 §5 写 24 小时），且仅内存、未按设计持久化 0600 | 网关重启或两次请求间隔 >30 分钟后，Gemini 3 系列的 `thoughtSignature`/thinking 签名丢失，多轮工具调用回传无签名可能被上游拒绝 | TTL 改 24h 并随插件状态目录持久化（0600、有上限） |
| 8 | 低 | `integrations/openclaw/dalton-langalpha-broker/src/journal.mjs:54-57` | `append` 失败被 `.catch(() => {})` 完全吞掉：磁盘满/权限错时日志静默丢失，无日志无计数（通用检查"无 except:pass 吞错"） | journal 所在盘写满后所有调用记录静默消失，审计与对账无据可查 | catch 里记入内存错误计数并经 `/health` 暴露（journal_write_failures），或至少 `logger.warn` 一次 |
| 9 | 低 | `integrations/openclaw/dalton-langalpha-broker/src/limits.mjs:3-26`、`src/invoke.mjs:197-204` | 无排队：超过 `maxConcurrent` 立即 429 `broker_overloaded`，P0.3 §7 设计的 `maxQueued:32/maxQueueWaitMs` 未实现（错误码也与设计的 `broker_busy` 不同） | 并行子智能体瞬时并发 >8 时，第 9 个起直接 429，调用方需自行重试 | 加简单 FIFO 队列 + 等待超时；错误码统一 `broker_busy` |
| 10 | 低 | `src/dalton/llm/openclaw_catalog.py:54-58, 541` | `make_model_key` 把 `[^a-z0-9.-]` 折叠为 `-`：不同 ref 可能映射同一 `ocb-` 键，`models_out[key] = model_entry` 静默覆盖 | 上游目录若同时有 `openai/gpt_6` 和 `openai/gpt-6`（或 `xai/grok.beta` 与 `xai/grok-beta`），后处理的条目悄悄替换前一个，目录少一个模型 | 折叠后查重，重键时加后缀或报 warning 进 `meta.warnings` |
| 11 | 低 | `scripts/dalton/openclaw_smoke.py:35-801` | 冒烟矩阵缺项：B9（缓存读）、B15（成本对账）、B16（回退）未实现；B6 只查 `uncached<100`（文档要求 SENTINEL 复述 + `<50`）；B3 只测非流式往返（文档要求两种）；`--max-spend-usd`（740 行）被接受但从未使用，冒烟花费无护栏 | 缓存计费、价格卡对账、回退约束三项设计检查从未跑过；全量 6 模型 × 全检查的花费可能超预算而脚本继续跑 | 补 B9/B15/B16 与 B6 的 SENTINEL 判据；累计 `usage.cost_usd` 超过 `max_spend_usd` 时提前终止 |
| 12 | 低 | `integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs:288-322` | 幂等回放命中不写 journal：回放事件在审计日志中不可见 | 对账时看到上游计费 1 次但 journal 里也只有 1 条，无法区分"没发生调用（回放）"与"记录丢失"；反向也无法统计回放节省 | 回放路径 append 一条 `{status:"replayed", invocation_id}` |

## 已确认没问题的关键点

- **消息转换（检查项 1）✅**：离线构造"system + user + assistant(2 个并行 tool_calls) + tool×2 + assistant(1 个 tool_call) + tool + assistant(纯文本)"请求跑 `normalizeOpenAiMessagesToContext`：systemPrompt 正确拼接；tool_calls 解析成 `toolCall` 块（字符串参数安全 JSON.parse，坏 JSON 落 `_raw` 不抛）；`tool` 消息缺 `name` 时靠 `toolCallNameById` 回填正确的工具名；`tool_call_id` 缺失报 400；stopReason 按是否有 tool_calls 置 toolUse/stop。反向 `buildNonStreamingChatCompletionResponse`：纯工具轮 `content:null`、`reasoning_content`、arguments 序列化，单测断言齐全。多轮 Gemini 签名经 replay-store 还原（`openai-map.test.mjs` 覆盖）。
- **流式工具调用（检查项 2）✅**：离线验证参数分段（`{"tic` + `ker":"META"}`）按 index 拼接得到完整 JSON；两个并行调用按 `contentIndex` 分到 index 0/1 不串号；未流式过参数时 `toolcall_end` 补发完整参数且有 `streamedArgsByContentIndex` 防重发。实际 B4（xai/grok-4.7 双并行工具）pass。缺陷见疑似问题 2。
- **调用 ID 生成（检查项 4）✅**：`broker_http.py` 以 scope（credit_gate run_ref + 可选 background agent id）+ 完整请求体 sha256 前 40 位派生，同一调用重试（同字节）得同一 ID；不同内容哈希不同；无 gate 时 `nogate:uuid4` 随机——放弃幂等但绝不误回放。httpx 事件钩子幂等（已有头不覆盖）。
- **认证（检查项 5）✅**：`crypto.timingSafeEqual`（先长度判等，只泄露长度）；错 token 401 且 message 不含 token；日志无 token；loopback 三地址 + 8 种代理头一律 403 `loop_only`；token 文件 0600 原子生成（tmp+rename）；`auth.test.mjs` 覆盖。
- **模型白名单（检查项 6）✅**：`allowedProviders`/`allowedModels`/openclaw.json 目录三重校验，`model` 必须严格 `provider/model` 格式，prepare 之前拦截；B12 实测两个未知模型均 404 `model_not_allowed`，不存在静默换模型路径。
- **纯文字模型（检查项 8）✅**：CLI 网关三家默认 `textOnlyProviders`，带 tools 请求在 prepare/SSE 开始前返回 400 `tools_unsupported`，不压扁；B11 实测 pass；overlay 同步给这些模型 `profile.tool_calling:false` 与"（仅文本）"显示名（L2）。
- **用量和费用（检查项 9）✅**：`prompt_tokens=input+cacheRead`（与 SDK usage 语义一致，单测 100+20=120 断言）、`completion_tokens=output`、缓存读写在 `prompt_tokens_details`；cost 由 SDK `calculateCost` 算一次，`dalton_meta.cost_usd` 与 journal 单次记录，回放不产生新费用；B10 实测 `cost_usd=0.001218`。overlay 多价格来源取最大值为 P0.4 §2 明文设计（偏保守、discrepancies 记录在案）。
- **推理强度映射（检查项 10）✅（映射正确，端到端未验证＝B8）**：`REASONING_EFFORT_MAP` 覆盖 none/off/minimal/low/medium/high/xhigh/max，经 `clampThinkingLevel` 收敛到模型档位，clamp 异常回退原始档（off 则不传）；manifest 侧 efforts low/medium/high 全在表内。
- **同步脚本（检查项 11）✅**：`extract_v1_rate_cards_from_openclaw_config` 只拷贝 id/model/maxTokens/thinkingLevel/rateCard 数值白名单字段；overlay 输出经 `assert_no_credentials` 三处把关（build、写盘、CLI 主流程）；实际生成的 `openclaw_overlay.json` 复核无任何凭据键；写盘 tmp + `os.replace` 原子；CLI 不写 `openclaw.json`；smoke 报告写盘前有 token 泄露自检（`openclaw_smoke.py:791-792`）。
- **overlay 合并（检查项 12）✅**：实测合并上游 65 → 92 键、27 个 `ocb-*` 无一与上游碰撞；`apply_model_overlay` 对碰撞键报 error 并跳过，sync 脚本对碰撞直接退出 1；多 worker：`mtime_ns:size` 戳 + 2 秒节流懒重建，各 worker 独立判定，sync 在当前 worker 立即清 `LLM._model_config` 与 `_build_provider_catalog` 缓存，同步本体由 Postgres advisory lock 串行化——符合 P0.7 §4 的多 worker 约定（无请求路径读进程内真相）。
- **家族判定（检查项 13）✅（L16 满足）**：离线对 13 个代表键复核（claude/gemini/gpt-oss 先于 gpt/grok/deepseek/glm/muse、`-oauth` 后缀剥离、`ocb-` 前缀回退匹配、overlay `dalton.family` 优先）全部正确；`select_independent_reviewer` 动态选家族：lead=`claude-opus-5-5-oauth` → 审稿人 `ocb-openai-gpt-6.1-sol`，lead=`ocb-xai-grok-4.7` → 同样换到 openai-gpt 家族；`check_roles` 对当前 `agent_config.dalton.yaml` 零警告；`apply_role_models.py` 与服务端共用同一函数，无写死。
- **回退默认关闭（检查项 7 前半）✅**：`agent_config.dalton.yaml` `fallback: []`，broker 无任何自动换模型逻辑（L3 默认面成立）。
- **上游挂钩（通用检查）✅**：`llm.py` 三处（llm-overlay×2、llm-broker-client）、`api_keys.py` 一处（catalog-cache）均带 `DALTON-HOOK` 标注并登记在 `check_hooks.sh` 基线；`clients.py` 无挂钩改动；上游 `models.json` 已无 `ocb-` 键（F2/P0N.2 修复生效）。
- **日志脱敏（通用检查）✅**：journal `scrubSecretsDeep` 递归剔除 13 类凭据键（有单测）；调用方元数据只存 `dalton` 扩展与哈希（`prompt_sha256`/`response_sha256`，不落 prompt 原文）；`_sanitize_error_text` 把 token 替换为 `***`。

## 疑似问题（未能确认）

- **容器 → broker 连通性（P0.9 前必须实测）**：PROGRESS.md 记录 OpenClaw 网关监听 `127.0.0.1:18789`；overlay 的 base_url 是 `http://{HOST_IP}:18789/dalton-broker/v1`（容器内 `HOST_IP` 默认 `host.docker.internal`→host-gateway）。若 colima 的 gvproxy 转发在 macOS 主机侧呈现为 127.0.0.1 来源则一切正常；若不是，容器内所有 `ocb-*` 调用会被 `verifyLoopbackAndOrigin` 403 `loop_only` 拒绝（或在 TCP 层连不上只听回环的网关）。B14 是宿主机进程跑的（HOST_IP 解析为 localhost），容器路径从未验证。
- **`toolcall_delta` 缺 `contentIndex` 时参数串号**：`openai-map.mjs:520` 默认 `cIdx=0`，离线复现两个工具的 delta 都归到 index 0，拼出 `{"a":1}{"b":2}` 无效 JSON。实际传输层（B4 pass 的 openai 系）都带 contentIndex，未见触发；若 OpenClaw 升级后某传输不带此字段，会在并行工具时坏参数。建议对"多工具在流且 delta 无 contentIndex"的情况 fail-fast。
- **订阅模型 `pricing:{pricing_type:"subscription"}`（无数值费率）合入上游 manifest 的 providers 模型表**：上游计价代码对缺 `input/output` 的 pricing 如何处理未验证——预期按 0 计费（符合"订阅不进现金预算"的意图），但未确认不会在上游解析路径抛错。
- **同一 run 内两次字节级完全相同的调用共用一个调用 ID**（`broker_http.py` 内容哈希语义）：第二次会拿到第一次的缓存回放。这是幂等的预期行为，但 temperature>0 的"重复采样"场景会得到相同输出；如需区分，调用侧应在 body 中带 nonce。

## P0F.6：B8 警告的原因与影响（核实结论）

- **B8 检查什么**：`openclaw_smoke.py:541-579`——对主工具模型各发一次 `reasoning_effort:"low"` 和 `"high"` 的非流式请求（同一道算术题），比较两次响应里 `body["openclaw"]["reasoning"]` 字段（564-565 行）是否不同。
- **实际输出**：`var/dalton/smoke/latest_broker.json` B8 = `warn`，`evidence: {"low_reasoning": null, "high_reasoning": null}`，两次请求 HTTP 均 200、合计约 5.0 秒——请求本身成功，只是回显字段读出来都是 null。
- **原因**：契约不匹配。broker 的非流式响应（`openai-map.mjs` `buildNonStreamingChatCompletionResponse` + `invoke.mjs:447-463` 的 `dalton_meta`）从不输出 `openclaw` 键，也不回显实际生效的推理档位；`openclaw` 键只在幂等回放路径被构造（`invoke.mjs:292-295`），而 B13 恰好读的是回放里的 `openclaw.replayed`，所以 B13 pass、B8 恒 warn——两个检查读的是两个从未统一的回显契约。`invoke.mjs:373` 已经算出了 clamp 后的 `reasoning` 值，只是没放进响应。
- **影响**：推理档位向 OpenClaw SDK 的透传**至今没有被端到端验证**（映射代码本身正确，见检查项 10）。按 P0.5 的表，B8 不过的后果是"推理档位从 manifest 去掉"；目前 27 个 overlay 模型全部带 `reasoning.efforts`，而验证悬置为 warn。它不是 OpenClaw 内部工具泄漏一类的问题（B7 pass 佐证），无功能性破坏。
- **修复方向**（本次未改）：`dalton_meta` 增加 `reasoning` 字段回显 clamp 后的实际档位；B8 改读该字段，并按 P0.5 补第二判据（high 的推理 token 数或耗时明显高于 low）。修掉后重跑冒烟，B8 应转 pass。
