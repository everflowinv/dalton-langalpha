# R6 原生接入 Antigravity、Muse、xAI 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

范围：提交 3e9cc996（Antigravity）、9a03d3ac（Muse）、7670a03f（xAI）的全部文件；对照上游 claude_oauth.py / codex_oauth.py / anthropic_oauth.py / codex.py。审查为只读：未调用付费模型、未发起真实 OAuth、未刷新令牌。关键结论均用离线脚本在 `.venv` 中复现（langchain 1.4.0 / core 1.6.1），manifest 单测 14 项通过。

## 结论

账号安全基本面（加密存储、Redis 刷新锁、令牌不泄露、多 worker 状态外置）与上游 Claude/Codex 实现完全同构，是三家共用的成熟通道，**没有发现凭据泄露类问题**。Muse 和 xAI 因为复用 codex/openai 的 SDK 通道，风险很小。**Antigravity 的自研 v1internal 客户端有一个高危问题**：思考签名（thoughtSignature）在实例上跨轮累积，从工具循环第 3 轮起回传错误的签名（离线复现确认），这正是审查清单点名"多轮工具调用时思考签名正确回传"的那一项。另有订阅失效错误被误判为可重试、functionResponse 名称回退错误、容器化部署下 loopback 登录不可达三个中危问题，以及测试缺失（登录/刷新/工具往返/错误分类全部无测试）。

- 严重：0；高：1；中：5；低：7；疑似：6（见下）。
- Antigravity：协议实现大体正确（信封、SSE、usage），但思考签名状态管理有实质缺陷，错误分类有一处缺口，登录依赖同机浏览器。
- Muse：最稳的一家——完全骑在 codex 通道上，唯一实际缺陷是会话亲和头永远不会生效（死代码）。
- xAI：纯 manifest 注册，无新代码面，价格条目与 P0N 设计表一致，无发现问题。

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | `src/llms/extension/antigravity.py:260, 461, 543-544` | 思考签名在客户端实例上跨请求累积，不随请求清零 | 同一 agent 图内工具循环复用同一实例（`bind_tools` 的 `model_copy` 浅拷贝共享 `_signatures` 列表，已验证）。离线复现：第 1 轮消息携带 [SIG1]（正确）；第 2 轮消息携带 [SIG1, SIG2]；第 3 轮（纯文本、无签名）仍携带 [SIG1, SIG2]。下一轮 `_messages_to_gemini` 回放时 `pop(0)` 把**上一轮的旧签名配到本轮的 functionCall 上**，自己的签名变成孤立 part，且无界累积（长任务几十轮）。端点对错误/多余签名的行为未知（可能 400，可能静默降质），提交说明只验证了单轮往返，复现不了这个 | 签名改为请求级状态：在 `_build_payload`（或 `_stream`/`_astream` 入口）清空 `self._signatures`，只累积本次响应的签名 |
| 2 | 中 | `src/llms/extension/antigravity.py:73-79, 358-363` | `AntigravityApiError` 用 `.status` 携带状态码，而容错中间件（`error_classification.extract_status_code`、`stream_errors.py`）只读 `.status_code`；SUBSCRIPTION_REQUIRED 分支的消息不含数字，正则也救不回 | 离线复现：401 → 识别为不可重试（消息里有数字，正则兜底）；**403 SUBSCRIPTION_REQUIRED（订阅失效/取消，最常见故障）→ status None → 判为可重试**，容错中间件会把一个永久失败沿重试梯和 fallback 模型全走一遍。`ResponsesStreamFailedError` 用 `status_code` 属性是正确范例 | 异常类加 `status_code` 属性（或改名为 `status_code`），并让 SUBSCRIPTION_REQUIRED 分支映射 403 |
| 3 | 中 | `src/llms/extension/antigravity.py:184` | `functionResponse` 的 `name` 回退到 `tool_call_id`；`ToolMessage.name` 缺失时发给 Gemini 的名称不是工具名 | 主执行路径（langchain_core `tool_result_to_message`）和 direct_mcp/repeat_guard/order_governance 都设了 `name`，但 `src/ptc_agent/agent/middleware/ask_user.py:96` 的 ToolMessage 没有 `name`——用户回答后下一轮请求，functionResponse 名变成 `antigravity_call_N`，与声明的工具名不匹配，端点很可能 400 | 遍历消息时用前一条 AIMessage 的 `tool_calls` 建 id→name 映射做回退，而不是拿 id 当名字 |
| 4 | 中 | `src/llms/extension/antigravity.py:458-470` | 一个 SSE 帧同时含 thought 和 text 部件时，`reasoning_content` 被放进两个 chunk，最终消息和 SSE 流里思考文本翻倍 | 离线复现：帧 `[{"thought":true,"text":"THOUGHT"},{"text":" ANSWER"}]` → `_final_message` 的 `reasoning_content == "THOUGHTTHOUGHT"`（langchain chunk 合并同样翻倍）。SSE producer 把每个 chunk 的 `reasoning_content` 当增量追加，用户看到思考重复；持久化的历史/摘要也翻倍 | text chunk 不携带 `extra_kwargs["reasoning_content"]`（只有纯 reasoning chunk 带） |
| 5 | 中 | `src/server/services/antigravity_oauth.py:219-221` 对 `docker-compose.yml:44-56` | loopback 回调监听在 backend 容器内的 127.0.0.1 临时端口；默认 `make up` 栈没有 host 网络也没有端口发布，宿主浏览器打不开 `http://localhost:<port>/callback` | 标准部署（backend 容器化）下 Antigravity 连接必失败：Google 重定向到宿主 localhost 的未发布端口，连接被拒；前端只能轮询到 10 分钟超时。只有 `uv run python server.py` 本机跑后端才行。Claude 有手动粘贴 URL 的回退，Antigravity 没有 | 文档明示该限制并提供回退：粘贴回调 URL 手动完成交换（复用 claude 的模式），或引导非容器部署 |
| 6 | 中 | `tests/`（缺） | 检查项 11 的关键路径全部无测试：登录（PKCE/state/loopback）、令牌刷新、工具调用往返、错误分类 | 三家提交只加了 `test_manifest_integrity.py` 的豁免名单（16+6 行）。codex 有 `test_codex_extension.py`、`test_codex_client_headers.py`，antigravity 630 行自研协议、muse 230 行 OAuth 服务零测试。问题 1、2、4 都是单测能拦的形状 | 至少补：签名往返（多轮）、错误分类状态码提取、`_messages_to_gemini` 的 name 回退、SSE 帧组装 |
| 7 | 低 | `src/server/services/antigravity_oauth.py:298-301` | `state` 缺失时跳过校验（`if returned_state and returned_state != state`），应 fail-closed | 伪造回调可以不带 state 直接触发交换尝试。实际攻击面被 PKCE 覆盖（伪造 code 在交换时因 verifier/redirect_uri 不匹配被 Google 拒绝），属纵深防御缺口 | 改为 `state` 必须存在且相等 |
| 8 | 低 | `src/server/services/antigravity_oauth.py:236-241` | PKCE verifier + state + port 写入 Redis 但从未被读取（回调闭包用进程内的值），死写且把一次性机密明文留在 Redis 600 秒 | 无功能影响；verifier 属于应当保密的值 | 删除该写入，或改为真正跨进程使用 |
| 9 | 低 | `src/server/services/antigravity_oauth.py:327, 349-358` | `start_login` 不清理上次的 `done` 键；`check_login_complete` 在 Redis 不可用时回退查 DB | 10 分钟内二次连接：首次轮询立刻命中旧 `done` 键假成功；Redis 宕机时旧令牌的存在也会误报成功 | `start_login` 先删 `done` 键；DB 回退要求 `updated_at` 晚于本次 initiate |
| 10 | 低 | `src/llms/llm.py:574-579` 对 `:864-869` | Muse 会话亲和头是死代码：`get_llm` 的 cache_key 门只放行 `sdk == "codex"` 或 `"openai"`，`sdk == "muse"` 永远拿不到 cache_key | `_get_muse_llm` 里 `if cache_key:` 永假，`x-tbh-session-id` / `x-meta-ai-gateway-session-id` 从不上线，提交说明承诺的会话亲和实际不存在；影响缓存命中/配额亲和，不影响正确性 | 门里加 `self.sdk == "muse"`（不给 prompt_cache_key，只传 session 头所需的 key） |
| 11 | 低 | `web/src/pages/Settings/panels/ConnectedAccounts.tsx:287-309`、`OAuthConnect.tsx` muse 分支 | 设备流收到 `slow_down` 不按 RFC 8628 加大轮询间隔（服务端把 slow_down 当 pending，前端用固定 interval） | Meta 限流时持续 5 秒一打，直到 10 分钟超时；不致命 | poll 响应携带 slow_down 信号，前端把间隔 +5s |
| 12 | 低 | `web/src/lib/brandArt.ts:39-58` | `muse-oauth` 和 `xai` 没有 brandArt 条目，UI 回退默认图标 | 纯外观 | 补条目（muse 可暂用通用 mark） |
| 13 | 低 | `src/llms/extension/antigravity.py:499-513` | `output_tokens` 取 `candidatesTokenCount`（不含 thoughts），`total_tokens` 取 `totalTokenCount`（含 thoughts），口径不一致 | 订阅模型无金额影响，但用量展示里 output 偏小、total 对不上 in+out | output 加上 `thoughtsTokenCount`，或 total 用 in+out |

## 已确认没问题的关键点

- **令牌存储**：三家都走共享的 `upsert_oauth_tokens`（`pgp_sym_decrypt`/`pgp_sym_encrypt`，`src/server/database/oauth_tokens.py`），无明文路径。
- **令牌刷新**：与上游 codex/claude 同构——`oauth_refresh_wait.py` 共用（SETNX 锁 + owner-token 释放 + 35s TTL + 等待方 10s 轮询 DB）；刷新失败记日志返回 None → 400 oauth_required（与上游行为一致）；Muse 无 refresh_token 时显式沿用旧 access token 并注释说明。
- **令牌泄露**：日志只打 status/body 片段（token 端点错误体不含令牌）；`/status` 不解密；loopback 回调 HTML 只回显 email；`_identifying_params` 不含 api_key；Muse device_code 只存 Redis 服务端、initiate 响应只回 user_code/verification_url；`authorize_url` 里的 code_challenge/state 是公开值。
- **PKCE**：S256、48 字节 urlsafe verifier、base64url 去填充、交换带 code_verifier，实现正确；loopback 只绑 `127.0.0.1`、成功即关、600s 超时关闭，前端轮询 3s×200 次与之对齐；scopes 与官方 `agy` CLI 的刷新令牌观测集一致（含 aicode/cclog/cloud-platform 等 7 项，未超出官方客户端自身集合）。
- **Muse 登录**：RFC 8628 设备流，denied/expired 终态显式抛错并清理 Redis 键，未知终态错误也上抛而不是永远轮询；令牌加密入库。
- **Muse/xAI 通道**：Muse 骑 `ChatCodexOpenAI`（system→instructions、store=false、Responses 流守卫），models.json 参数形状（`reasoning.summary`、`include reasoning.encrypted_content`、efforts 写 `parameters.reasoning.effort`）与上游 codex 条目逐字段一致；xAI 走标准 openai SDK + `use_response_api` + `XAI_API_KEY`，无新协议代码。
- **推理强度映射**：antigravity 的 `write: parameters.thinking_level` 在 `reasoning.py` 的 WRITE_PATHS 白名单内，`-low/-medium/-high` 后缀派生与 parameters 双路径结果一致（含 EXTRA_LOW→LOW 折叠）；muse 与 codex 同路。
- **用量与费用**：usage 从 finishReason 帧的 usageMetadata 读出（含 reasoning token details）经标准路径入 `conversation_usages`；订阅 20 个模型零现金成本由 `_UNPRICED_KEYS` 锁定（tests/unit/llms/test_manifest_integrity.py，14 passed），且 `find_model_pricing` 的全供应商回退今天不会命中任何 antigravity/muse model_id（逐一核对无碰撞）；预算门对 OAuth 轮放行（`is_byok` 含 OAUTH）。
- **xAI 价格**：与 P0N 设计表逐项一致（grok-4.7 $2/$0.5/$6、grok-build-0.1 $1/$0.2/$2、grok-4.3 $1.25/$0.2/$2.5，>200K 输入翻倍，`input_dependent` 分层），`pricing_utils` 的分层/缓存价读取路径支持该形状；重试不会重复计费（失败尝试不产生 usage 记录，仅成功回合入账）。
- **多 worker**：令牌状态在 Postgres/Redis，无进程内令牌缓存当状态依据；Antigravity 轮询读 Redis/DB 不依赖发起进程；loopback 回调由 Google 直连 OS 端口，不经负载均衡。
- **模型清单内容**：三家 23 个键的 context/max_output/能力标记/visible 与提交说明及 P0N 表一致（清单位置问题属 P0F.2 已知，不重复报告）。
- **前端**：轮询窗口、断开连接路径、locale en/zh 键齐全（25 键无缺失）、`noopener` 打开授权页。

## 疑似问题（未能确认）

- `src/server/services/antigravity_oauth.py:50` 存在疑似令牌（Google 安装型应用 client_secret，`GOCSPX-` 前缀）。属 RFC 8288 公开客户端类别、随官方 CLI 二进制分发的非机密值，文件头有说明；按审查规则不复制内容。判定：可接受，建议在报告归档时知悉。
- 端点对错误/多余 thoughtSignature 的行为未知（问题 1 的实际后果是 400 还是静默降质），需一次真实多轮工具调用验证。
- SSE 解析只认 `"data: "` 前缀（`antigravity.py:384, 402`）；Google `alt=sse` 实测带空格，但规范允许无空格，若上游变更会静默丢帧。
- xAI 三个模型 `is_reasoning: true` 但 models.json 无 `reasoning` 块（无推理强度控制）；是否与 OpenClaw 账号侧用法一致未能确认（P0N 表也未标 efforts）。
- Antigravity 托管的 claude-opus/sonnet-5.5 标 1M 上下文、gemini 系列 1M——无法对公开资料核实（模型版本为账号目录来源）。
- 上游文件改动无 `DALTON-HOOK` 标注、manifest 条目在基础清单、OAuth 主模型时预算整轮放行——均已登记在 P0N.2/P0N.3/P0N.6，非本报告新发现。
