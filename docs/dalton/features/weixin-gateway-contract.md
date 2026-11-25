# 团队个人微信多账号 — 网关与后端内部接口契约

> 本文档为后端（批次 A）与渠道网关（批次 B）之间的**唯一接口真值契约**，严格以后端实现（`src/dalton/app/channels_router.py`、`src/dalton/database/channel_gateway.py`、`src/dalton/database/weixin_multi_account.py`、`src/dalton/database/data_task_notifications.py`、`src/dalton/database/automation_notifications.py`、`src/dalton/notify/channel_transport.py`、`src/tools/messaging/tools.py`）为准。设计文档见 [weixin-multi-account.md](./weixin-multi-account.md) §3.1。

---

## 0. 公共约定

- **路由前缀**：除特别标明「后端 → 网关」外，全部挂载于 `/api/v1/dalton/channels/internal/*`。
- **服务鉴权与网关就绪探活**：全部内部接口要求请求头 `X-Service-Token: <INTERNAL_SERVICE_TOKEN>`（不匹配返回 `401`）。所有接口均不接受用户 JWT。
  - 网关 `GET /health` 同样要求 `X-Service-Token`，返回 `200 {"ok": true, "ready": <bool>}`，区分「进程已监听」与「业务已就绪」：仅当启动快照已应用、写入门禁已打开（即 `/agent/send` 不再返回 `503 gateway_starting`）时 `ready` 才为 `true`，停机过程中为 `false`。若后端尚未挂载微信路由或未开启 `channel_weixin`、`/internal/weixin/accounts/reconcile` 在启动期返回 `404`，非微信部署网关即视作启动快照已就绪并放开写入门禁（`GET /health` 返回 `ready: true`，同时后台每 5 分钟探测一次）。
  - 独立部署升级脚本（`scripts/standalone/upgrade.sh`）的就绪检查**只认** `/health` 返回严格合法 JSON `ok === true && ready === true`（缺少 `ready` 字段、`ready: false` 或含 `NaN`/`Infinity` 等非法 JSON 常量一律按未就绪继续轮询直至超时，不回退请求 `/agent/settings`）。
- **能力门控**：总能力 `CapabilityId.CHANNEL_GATEWAY` 关闭时全部返回 `404`；微信专属接口及目标平台解析为 `weixin` 的共享接口额外要求 `CapabilityId.CHANNEL_WEIXIN` 开启（关闭返回 `404`）。
- **错误响应格式**：结构化错误返回 JSON `{"code": "<error_code>", "message": "<human_message>", ...}`（HTTP `400`/`401`/`403`/`404`/`409`/`422`/`503`）。
- **`X-Gateway-Epoch` 租约围栏规则（集中于 `verify_gateway_epoch_in_tx`）**：
  - **始终免带（只读 / 租约获取 / 配置读取）**：`POST .../gateway-lease/acquire`、`GET .../weixin/accounts/{account_id}`、`GET .../weixin/login-sessions/{session_key}`、`GET .../state`（I-4）、`PUT .../settings`（I-5）、`POST .../automation-runs` 与 `POST .../automation-runs/{execution_id}/finish`（I-8）。
  - **微信始终强制（缺头、过期或失配一律 `409 {"code": "stale_gateway_epoch"}`）**：全部微信写入接口、所有目标平台为 `weixin`（含数据库既有行或 `conversation_threads` / 会话来源解析为 `weixin`）的 I-1 / I-2 / I-3 / I-6 / `preflight-send` / I-7（含 `deliveries/progress`、`inbound/progress`、`steers/reconcile`）。
  - **飞书 / Discord 区分入站与出站**：
    - **I-1 / I-2 / I-3 / `recover`**：租约表从未出现租约行时允许省略；一旦租约表有记录，省略或过期/失配返回 `409 stale_gateway_epoch`。
    - **I-6 / I-7（`authorize-and-claim`、`deliveries/progress`、`inbound/progress`、`steers/reconcile`）**：即使租约表已有记录，非微信平台（飞书 / Discord）**省略 `X-Gateway-Epoch` 仍放行**（支撑网关 `outgoing` 无租约模式）；但**只要显式携带了 `X-Gateway-Epoch`**，后端仍按租约行校验，过期/失配返回 `409 stale_gateway_epoch`。
- **一次性回执信封（`OneTimeReplyEnvelope`）**：用于冲突自动解绑或 `/unbind` 后的单次微信告别回复，**严格包含且仅包含以下 6 个字段**（不包含 `account_id` 或 `bot_generation`）：
  ```json
  {
    "bot_token": "string",
    "base_url": "string",
    "ilink_bot_id": "string",
    "recipient_id": "string",
    "context_token": "string",
    "text": "string"
  }
  ```
- **旧地址规范化**：微信私聊规范地址为 `weixin:@me`；历史库行遗留的 `weixin:dm:<id>` 在 I-6 当前会话匹配与 `notice` 目标核对时自动规范化为 `weixin:@me` 同等对待。
- **I-9 已移除**：单账号旧接口 `/internal/weixin-state` 已下线，批次 A 与批次 B 同步部署。
- **材料收集模式不在本批范围内**：原 `/collect`、`/done`、`/cancel`、`/help` 指令及 `/internal/weixin/collect-buffers/*` 接口已移除，留待后续独立批次实现；微信入站附件按单条消息随回合处理（与飞书一致）。

---

## 1. 网关单实例租约

### 1.1 `POST /internal/gateway-lease/acquire`

- **请求头**：`X-Service-Token`（不带 `X-Gateway-Epoch`）
- **请求体**：
  - `instance_id: string`（必填，去空白后 `1..128` 字符）
  - `ttl_seconds: int`（可选，默认 `45`，校验范围 `5..300`；越界由 FastAPI/Pydantic 返回 HTTP `422`）
- **响应 `200`**：
  - 获租：`{"granted": true, "epoch": int, "expires_at": ISO8601}`
  - 已被他人持有且未过期：`{"granted": false, "epoch": int, "expires_at": ISO8601, "retry_after_ms": int}`（`retry_after_ms` 范围 `1000..3000`）
- **错误**：`400`（`instance_id` 去空白后为空或超过 128 字符）、`422`（字段类型或 `ttl_seconds` 超出 `5..300`）

### 1.2 `POST /internal/gateway-lease/heartbeat`

- **请求头**：`X-Service-Token`
- **请求体**：`instance_id: string`, `epoch: int (>=1)`, `ttl_seconds: int = 45`（校验范围 `5..300`，越界返回 HTTP `422`）
- **响应 `200`**：`{"ok": true, "epoch": int, "expires_at": ISO8601}`
- **错误**：`400`、`422`、`409 {"code": "stale_gateway_epoch", "message": "..."}`（失租或已过期）

### 1.3 `POST /internal/gateway-lease/release`

- **请求头**：`X-Service-Token`
- **请求体**：`instance_id: string`, `epoch: int (>=1)`
- **响应 `200`**：`{"ok": true, "released": bool}`
- **错误**：`422`（字段校验失败）

---

## 2. 微信账号同步、详情、游标与状态上报

### 2.1 `POST /internal/weixin/accounts/reconcile`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：`since_revision: int | null = null`
- **响应 `200`**：
  - `changed: bool`（`since_revision` 与当前 `accounts_revision` 不同时为 `true`）
  - `enabled: bool`
  - `allow_external: bool | null`
  - `max_concurrent_accounts: int`
  - `accounts_revision: int`
  - `accounts: Array<AccountSnapshot>`（当 `changed: false` 或 **`enabled: false`〔即使 `changed: true`〕** 时固定为空数组 `[]`；仅在 `changed: true` 且 `enabled: true` 时返回全量 `active` / `pending_verification` 快照）：
    - `account_id: uuid-str`
    - `user_id: string`
    - `ilink_bot_id: string`
    - `ilink_user_id: string | null`
    - `prev_ilink_user_id: string | null`
    - `ilink_username: string | null`
    - `bot_token: string`
    - `base_url: string`
    - `cdn_base_url: string`
    - `get_updates_buf: string`
    - `bot_generation: int`
    - `status: "active" | "pending_verification"`
    - `last_inbound_at: ISO8601 | null`
    - `updated_at: ISO8601 | null`
- **错误**：`404`（`channel_weixin` 关闭）、`409 stale_gateway_epoch`

### 2.2 `GET /internal/weixin/accounts/{account_id}`

- **请求头**：`X-Service-Token`（只读，无需 `X-Gateway-Epoch`）
- **查询参数**：`recipient_platform_user_id?: string`, `inbound_message_id?: string`
- **响应 `200`**：
  - `account_id: uuid-str`
  - `user_id: string`
  - `ilink_bot_id: string`
  - `ilink_user_id: string | null`
  - `ilink_username: string | null`
  - `bot_token: string`
  - `base_url: string`
  - `cdn_base_url: string`
  - `get_updates_buf: string`（无游标时为 `""`）
  - `bot_generation: int`
  - `status: "active" | "pending_verification" | "expired" | "disabled"`
  - `weixin_context_token: string | null`
  - `weixin_context_token_version: int | null`
- **错误**：`400`（无效 UUID）、`404`（账号不存在或能力关闭）

### 2.3 `POST /internal/weixin/accounts/{account_id}/cursor`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：
  - `expected_bot_generation: int`
  - `expected_ilink_bot_id: string`
  - `expected_get_updates_buf: string | null = null`
  - `get_updates_buf: string`
- **响应 `200`**：
  - `ok: bool`, `updated: bool`, `stale_generation: bool`, `cas_mismatch: bool`, `bot_generation: int`, `get_updates_buf: string`, `status: string`
- **错误**：`400`、`404`、`409 stale_gateway_epoch`

### 2.4 `POST /internal/weixin/accounts/{account_id}/status`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：
  - `action: "mark_session_expired" | "mark_context_token_stale" | "reset_corrupt_cursor"`
  - `expected_bot_generation: int`
  - `expected_ilink_bot_id: string`
  - `error_code: string | null = null`（保持字符串类型，如 `"-14"`；`mark_session_expired` 为空时默认 `"-14"`）
  - `recipient_platform_user_id: string | null = null`（`mark_context_token_stale` 用）
  - `expected_token_version: int | null = null`（`mark_context_token_stale` CAS 用）
- **响应 `200`**：
  - `ok: bool`, `updated: bool`, `stale_generation: bool`, `version_mismatch: bool`, `accounts_revision: int`
- **错误**：`400`、`404`、`409 stale_gateway_epoch`

### 2.5 后端 → 网关：`POST /internal/weixin/accounts/sync`

- **请求头**：`X-Service-Token`
- **请求体**：`{"accounts_revision": int, "reason": string}`
- **期望响应 `200`**：`{"ok": true, "accepted_revision": int}`（后端最佳努力轻推，失败不影响事务提交）

---

## 3. 微信二维码登录会话

### 3.1 `GET /internal/weixin/login-sessions/{session_key}`

- **请求头**：`X-Service-Token`（只读，无需 `X-Gateway-Epoch`）
- **响应 `200`**：
  - `session_key: string`, `user_id: string`, `status: "starting" | "wait" | "scaned" | "verify_code" | "confirmed" | "expired" | "superseded"`, `login_version: int`, `qrcode: string | null`, `qrcode_img_content: string | null`, `redirect_base_url: string`, `expires_at: ISO8601`, `connected: bool`, `already_connected: bool`, `account_id: uuid-str | null`, `account_status?: "active" | "pending_verification" | null`（已确认会话直接返回对应微信账号的当前实时状态，亦同步写入 `confirmed_result.account_status`）, `bot_generation: int | null`, `accounts_revision: int | null`
- **错误**：`404`（会话不存在）

### 3.2 `PATCH /internal/weixin/login-sessions/{session_key}`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：
  - `action: "save_qr" | "update" | "confirm"`
  - `expected_login_version?: int | null`
  - `expected_bot_generation?: int | null`
  - `status?: "wait" | "scaned" | "verify_code" | "expired" | null`（`update` 用）
  - `qrcode?: string | null`, `qrcode_img_content?: string | null`（`save_qr` 用）
  - `redirect_base_url?: string | null`, `expires_in_seconds?: int | null`
  - `ilink_bot_id?: string | null`, `bot_token?: string | null`, `base_url?: string | null`, `cdn_base_url?: string | null`, `ilink_user_id?: string | null`, `ilink_username?: string | null`（`confirm` 用）
- **响应 `200`**：
  - `save_qr`：`{"ok": true, "revoked": false, "session_key": string, "status": "wait", "login_version": 2, "expires_at": ISO8601, "redirect_base_url": string}`
  - `update`：`{"ok": true, "revoked": false, "session_key": string, "status": "wait" | "scaned" | "verify_code" | "expired", "login_version": int, "redirect_base_url": string, "expires_at": ISO8601}`
  - `confirm`：
    ```json
    {
      "ok": true,
      "revoked": false,
      "status": "confirmed",
      "account_status": "active | pending_verification",
      "ilink_bot_id": "string",
      "ilink_user_id": "string | null",
      "ilink_username": "string | null",
      "confirmed_result": {
        "account_id": "uuid-str",
        "connected": true,
        "already_connected": false,
        "bot_generation": 1,
        "accounts_revision": 2
      },
      "account_id": "uuid-str",
      "connected": true,
      "already_connected": false,
      "bot_generation": 1,
      "accounts_revision": 2
    }
    ```
    > **字段语义说明**：`confirm` 响应中 `status` 始终是**登录会话状态**（固定为 `"confirmed"`）；生成的微信账号状态由 **`account_status`**（`"active" | "pending_verification"`）给出。网关应始终读取 `account_status` 判断是否需要等待首包核验。
- **错误（三类 `action` 统一）**：
  - **`400`**：`session_key` 缺失、不支持的 `action`、`save_qr` 缺少 `qrcode`、`update` 传入非法 `status`（仅允许 `wait` / `scaned` / `verify_code` / `expired`）、`confirm` 缺少 `ilink_bot_id` 或 `bot_token`
  - **`409 {"code": "login_session_revoked", "message": "扫码会话已撤销或已过期"}`**（会话已撤销 / 被取代 / 过期 / 版本冲突，0 行命中整笔回滚）
  - `409 {"code": "stale_gateway_epoch"}`
  - `409 {"code": "ilink_user_already_bound"}` / `409 {"code": "weixin_capacity_full"}`（`confirm` 占用或容量已满）
  - `403`（成员不在册或渠道/外部开关关闭）、`503 {"code": "roster_lookup_error"}`

### 3.3 后端 → 网关：三个扫码内部入口

后端收到网页端扫码请求后，先完成登录会话归属校验与状态预写，再携带 `X-Service-Token` 与 `X-User-Id: <user_id>` 转发给网关：

1. **`POST /internal/weixin/qrcode/start`**
   - **请求头**：`X-Service-Token`, `X-User-Id: <user_id>`
   - **请求体**：`{"session_key": string, "user_id": string, "login_version": int, "expected_bot_generation": int | null}`
   - **期望响应 `200`**：含 `session_key`, `status: "wait"`, `qrcode_img_content`, `expires_at` 等（后端按 `_shape_public_weixin_qr_response` 脱敏过滤后返回前端，绝不透出 `qrcode` 原始凭证或 `bot_token`）。
2. **`GET /internal/weixin/qrcode/wait`**
   - **请求头**：`X-Service-Token`, `X-User-Id: <user_id>`
   - **查询参数**：`session_key: string`
   - **期望响应 `200`**：含 `session_key`, `status: "wait" | "scaned" | "verify_code" | "confirmed" | "expired" | "superseded"`；确认成功时可带 `connected`, `account_id`, `account_status`, `bot_generation`, `accounts_revision` 等。
3. **`POST /internal/weixin/qrcode/verify-code`**
   - **请求头**：`X-Service-Token`, `X-User-Id: <user_id>`
   - **请求体**：`{"session_key": string, "verify_code": string}`
   - **期望响应 `200`**：同 `wait` 响应格式，经后端 `_shape_public_weixin_qr_response` 脱敏后返回前端。

---

## 4. 入站、绑定与会话指令（I-1 / I-2 / I-3）

### 4.1 I-1：`POST /internal/inbound/register-and-resolve`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `platform: "feishu" | "discord" | "weixin"`, `platform_user_id: string`, `chat_type: "dm" | "group" = "dm"`, `chat_address: string`, `thread_scope_key: string`, `message_id: string`, `request_key?: uuid-str | null`, `raw_payload: object = {}`
  - 微信附加字段：`weixin_account_id?: uuid-str | null`, `weixin_context_token?: string | null`, `weixin_bot_id?: string | null`, `weixin_bot_generation?: int | null`, `weixin_context_seq?: int | null`, `weixin_media_meta?: Array<object> | null`, **`unsupported_item_types?: Array<int | string> | null`**（网关传 iLink 数字类型码，如 `[5]`；后端兼容数字或字符串列表）
- **响应 `200`（关键字段）**：
  - `ok: bool`, `linked: bool`, `allowed: bool`, `deny_reason: string | null`, `existing_status: string | null`, `status: string`, `dalton_user_id?: string`, `workspace_id?: uuid-str`, `generation?: int`, `external_thread_id?: string`, `has_usable_weixin_context_token?: bool`
  - 待验证冲突自动解绑时：
    - `ok: false`, **`deny_reason: "ilink_user_conflict_auto_unbound"`**
    - `one_time_weixin_reply?: OneTimeReplyEnvelope | null`（字段见 §0）
- **常见 `deny_reason` 枚举**：`weixin_inbound_fields_required`, `weixin_group_unsupported`, `invalid_inbound_event`, `unsupported_merged_forward`, `not_linked`, `member_not_in_roster`, `weixin_channel_disabled`, `external_access_disabled`, `weixin_bot_generation_mismatch`, `weixin_message_id_account_mismatch`, `ilink_user_conflict_auto_unbound`
- **错误**：`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`（网关保留游标稍后重试）

### 4.2 I-2：`POST /internal/bind-code/consume`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `action: "bind" | "unbind" = "bind"`, `platform: "feishu" | "discord" | "weixin"`, `platform_user_id?: string | null`, `code?: string | null`, `is_group: bool = false`, `user_id?: string | null`, `message_id?: string | null`
  - 微信 `/unbind` 必填字段（任一缺失返回 `400`）：`weixin_account_id`, `weixin_bot_id`, `weixin_bot_generation`, `platform_user_id`；可选 `weixin_context_token`（用于签发一次性告别回执）
- **响应 `200`**：
  - `ok: bool`, `reason?: string`, `user_id?: string`, `one_time_weixin_reply?: OneTimeReplyEnvelope | null`
- **错误**：`400`、`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`

### 4.3 I-3：`POST /internal/session/command`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `cmd: string`（支持 `workspace_list`, `workspace_switch`, `new`, `stop`；不支持的指令如已移除的 `collect`/`done`/`cancel`/`help` 返回 `400`）, `platform`, `scope_key`, `platform_user_id?`, `dalton_user_id?`, `chat_type = "dm"`, `chat_address?`, `message_id?`, `request_key?`, `raw_payload?`, `is_internal_query: bool = false`
  - 微信附加字段：`weixin_account_id?`, `weixin_context_token?`, `weixin_bot_id?`, `weixin_bot_generation?`, `weixin_context_source?`, `weixin_context_seq?`
- **响应 `200`**：
  - 公共字段：`ok: bool`, `duplicate?: bool`, `deny_reason?: string | null`（冲突自动解绑统一为 **`"ilink_user_conflict_auto_unbound"`**）, `reply_text?: string`, `command_reply_pending?: bool`, `generation?: int`, `one_time_weixin_reply?: OneTimeReplyEnvelope | null`
- **不变量**：在修改任何既有 `(platform, message_id)` 入站行之前（不论原行是否已有 `command_result`），先核对账号 / 用户 / 发件人（含省略 `platform_user_id` 查得身份不匹配或 `invalid_inbound_event` 场景），不一致仅返回拒绝，绝不覆盖原记录。
- **错误**：`400`、`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`

---

## 5. 状态查询与用户设置（I-4 / I-5）

### 5.1 I-4：`GET /internal/state`

- **请求头**：`X-Service-Token`（只读，无需 `X-Gateway-Epoch`）
- **查询参数**：`user_id?`, `thread_id?`, `run_id?`, `platform?`, `external_thread_id?`
- **响应 `200`**：`user_tier`, `home_workspace_id`, `identities`, `settings`, `chat_bindings`, `current_conversation`（仅当 `channel_weixin` 启用时才输出微信键）

### 5.2 I-5：`PUT /internal/settings`

- **请求头**：`X-Service-Token`（无需 `X-Gateway-Epoch`）
- **请求体**：`user_id?`, `version: string`（CAS）, `settings?: object`, `automation_output_update?: object`
- **响应 `200`**：更新后的设置与新 `version`；`409`（CAS 版本冲突）；`404`（禁用子能力平台被修改）

---

## 6. 出站认领、发送前预检与进度结算（I-6 / Preflight / I-7 / I-8）

### 6.1 I-6：`POST /internal/deliveries/authorize-and-claim`

- **请求头**：`X-Service-Token`；微信必填 `X-Gateway-Epoch`；飞书 / Discord 可省略（显式携带仍会校验）
- **加锁顺序**：全部微信认领路径（常规 I-6、`data_task` 与 `automation_notify`，含失败重认领）严格先按 `dalton_channel_weixin_accounts (FOR KEY SHARE)` →（常规 I-6）`dalton_channel_session_generations (FOR SHARE)` → `dalton_channel_deliveries (FOR UPDATE)` 加锁，与 `unbind_weixin_account`（账号 `FOR UPDATE` → 会话 → 删除触发投递 `ON DELETE SET NULL`）完全同序，零死锁。
- **请求体**：
  - `delivery_key: string`, `kind: "agent_send" | "fallback" | "interrupt" | "automation" | "notice" | "data_task" | "automation_notify"`, `user_id?`, `thread_id?`, `run_id?`, `execution_id?`, `automation_execution_id?`, `platform?`, `target?`, `inbound_message_id?`, `has_non_empty_text: bool = true`, `total_chunks: int = 0`, `source_scope_key?`, `source_generation?: int`, `activity_origin?: "conversation" | "replay" | "notification" | "command" | "none"`
- **响应 `200`（按 `claim_status` 与 `kind` 分支）**：
  1. **`claim_status: "claimed"`（常规 `kind`：`agent_send` / `fallback` / `interrupt` / `automation` / `notice`）**：
     - `recipient_id: string`, `platform: string`, `target_address: string`, `delivery_attempt: int`, `is_current: bool`, `thread_scope_key: string | null`, `home_workspace_id: uuid-str | null`, `confirmed_thread_address: string | null`, `discord_body_phase: string | null`, `weixin_context_token: string | null`
     - 微信附加字段：`weixin_account_id: uuid-str`, `weixin_bot_id: string`, `weixin_bot_generation: int`, **`weixin_context_token_version: int | null`**（允许为 `null`）
  2. **`claim_status: "claimed"`（`kind: "data_task"` 与 `kind: "automation_notify"` 专用精简响应）**：
     - 公共字段：`claim_status: "claimed"`, `recipient_id: string`, `platform: string`, `target_address: string`, `is_current: false`, `home_workspace_id: uuid-str | null`, `weixin_context_token: string | null`（不返回 `delivery_attempt`、`thread_scope_key`、`confirmed_thread_address`、`discord_body_phase`）
     - 微信附加字段：`weixin_account_id: uuid-str`, `weixin_bot_id: string`, `weixin_bot_generation: int`, `weixin_context_token_version: int | null`
  3. **`claim_status: "already_completed"`**：`status: "sent" | "partial"`, `first_response_json`, `platform`, `target_address`（常规 `kind` 另含 `delivery_attempt`；`data_task`/`automation_notify` 另含 `is_current: false`）
  4. **`claim_status: "in_flight_or_unknown"`**：`status: "unknown"`, `code: "in_flight" | "unknown"`, `platform`, `target_address`（常规 `kind` 另含 `delivery_attempt`；`data_task`/`automation_notify` 另含 `is_current: false`）
  5. **`claim_status: "denied"`**：`status: "failed" | "unknown"`, `code: string`, `message: string`
     - 常见拒绝码：`session_generation_superseded`（在认领同事务 `FOR SHARE` 核验当前会话代数失配，覆盖 `notice` 与普通回复）、`source_generation_unknown`（标 `unknown` + 审计）、`weixin_bot_generation_superseded`、`weixin_account_rebound`、`weixin_session_expired`、`weixin_context_token_stale`、**`missing_weixin_context_token`（常规 I-6）** / **`weixin_context_token_missing`（`data_task` 与 `automation_notify`）**、`member_not_in_roster`、`weixin_channel_disabled`、`external_access_disabled`、`not_linked`、`not_allowed`
- **错误**：
  - `400`、`404`（平台能力关闭）、`409 stale_gateway_epoch`
  - **`503 {"code": "roster_lookup_error", "retryable": true, "message": "..."}`**（认领时名册查询失败，不落库暂缓，由调用方/网关按退避重试）

### 6.2 发送前实时预检：`POST /internal/deliveries/{delivery_key}/preflight-send`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填；先做 `channel_weixin` 能力检查再解析 epoch）
- **请求体**：`delivery_attempt: int`, `chunk_index?: int | null`
- **响应 `200`**：
  - 允许：`{"authorized": true, "decision": "allow", "retryable": false, "chunk_index": int, "account_id": uuid-str, "ilink_bot_id": string, "bot_generation": int, "bot_token": string, "base_url": string, "cdn_base_url": string, "recipient_id": string, "weixin_context_token": string, "weixin_context_token_version": int | null}`
  - 临时可重试（名册 `lookup_error`）：`{"authorized": false, "decision": "defer", "retryable": true, "deny_code": "roster_lookup_error"}`
  - 拒绝：`{"authorized": false, "decision": "deny", "retryable": false, "deny_code": string}`
- **网关进程内退避重试上限与后端调用超时协同常量**：
  - **单次投递预检重试总时长上限**：固定为 **`180s`（3 分钟，后端常量 `WEIXIN_PREFLIGHT_RETRY_BUDGET_SECONDS = 180`）**，整笔投递（跨全部分片与附件）共享该总预算（指数退避如 2s → 4s → 8s → 16s → 30s 封顶），后端不落库暂缓状态、投递行保持 `in_flight`；超预算仍未恢复时，网关通过 I-7 上报终态 `status: "failed"`（若已有部分分段送出则按既有分段语义上报 `partial` 或 `unknown`）。
  - **网关实际取值**：网关常量 `WEIXIN_PREFLIGHT_RETRY_MAX_ELAPSED_MS = 35000`（35s），在上述 180s 上限之内；重试用尽时 I-7 的 `error_code` 填最后一次可重试的根因（如 `roster_lookup_error`），并附可选字段 `error_detail: "preflight_deadline_exceeded"`。后端 I-7 请求模型允许额外字段，当前只记录 `error_code`。
  - **后端调用网关读超时（统一引用共享常量 `GATEWAY_DELIVERY_READ_TIMEOUT_S = 240.0`，连接超时 `5.0s`，严格大于 `180s` 契约上限与 `35s` 网关实际期限并留足余量）**：
    - `src/dalton/notify/channel_transport.py`：共享常量 `WEIXIN_PREFLIGHT_RETRY_BUDGET_SECONDS = 180`、`GATEWAY_DELIVERY_READ_TIMEOUT_S = 240.0` 及 `resolve_gateway_delivery_read_timeout_s(timeout_s)`；`dispatch_data_task_target_via_gateway`、`post_data_task_gateway_notify`、`post_automation_gateway_notify` 无论调用方是否显式传入更短的 `timeout_s`，实际读超时一律满足 `>= 240.0s`（未传时为 `max(240.0, get_notify_http_timeout_s() * 2.5)`）；
    - `src/dalton/notify/dispatch.py`：自动化报告发送（`_report_dispatch_timeout_s` / `dispatch_automation_report`）与失败补发扫描（`retry_failed_notify_deliveries`，默认 `max_duration_s = 250.0`）对网关调用一律使用 `resolve_gateway_delivery_read_timeout_s(...)`（`>= 240.0s`）；**认领下一笔网关投递前要求剩余扫描预算 `>= GATEWAY_DELIVERY_READ_TIMEOUT_S + 4.0`（`244.0s`，4s 用于写状态）**，不足则停止本次扫描，留待下一次；
    - `src/dalton/autonomy/preflight.py`：预检触发的补发扫描预算 `PREFLIGHT_RETRY_NOTIFY_MAX_DURATION_S = 250.0`、外层 `PREFLIGHT_RETRY_NOTIFY_TIMEOUT_S = 254.0`（保留 `4.0s` DB 落账余量）；
    - `src/tools/messaging/tools.py`：`_SEND_TIMEOUT = httpx.Timeout(GATEWAY_DELIVERY_READ_TIMEOUT_S, connect=5.0)`；
    - `src/server/services/automation_delivery.py`：`_FINISH_TIMEOUT = httpx.Timeout(connect=5.0, write=10.0, pool=10.0, read=float(GATEWAY_DELIVERY_READ_TIMEOUT_S))`，并由 `asyncio.wait_for(..., timeout=_FINISH_TOTAL_TIMEOUT_S)` 约束单次 `/agent/automation-runs/{execution_id}/finish` 总时长上限 `_FINISH_TOTAL_TIMEOUT_S = float(GATEWAY_DELIVERY_READ_TIMEOUT_S) + 15.0`（`255.0s`），严格小于 `hook_outbox` 中 `automation_finish` 的认领租约期限 `int(GATEWAY_DELIVERY_READ_TIMEOUT_S) + 60`（`300s`）。
- **错误**：`404`（能力关闭）、`409 stale_gateway_epoch`

### 6.3 I-7 (1)：`POST /internal/deliveries/progress`

- **请求头**：`X-Service-Token`；库中投递行 `platform == "weixin"` 时必填 `X-Gateway-Epoch`；飞书 / Discord 可省略（显式携带仍会校验）
- **请求体**：
  - `delivery_key: string`
  - `status?: "in_flight" | "sent" | "partial" | "failed" | "unknown" | null`
  - `outcome?: "in_flight" | "sent" | "partial" | "failed" | "unknown" | null`（与 `status` 同时传入时必须一致，否则返回 `400`）
  - `delivery_attempt?: int | null`（传入时必须匹配当前尝试号，否则 `409`）
  - `sent_chunks?: int | null`, `total_chunks?: int | null`, `platform_message_ids?: Array<any> | null`, `files_status?: Array<object> | null`
  - `first_response_json?: object | null`, `error_code?: string | null`（允许 `preflight_timeout` 等网关预检错误码）, `error_message?: string | null`, `error_detail?: string | null`（可选诊断字段，如 `"preflight_deadline_exceeded"`；后端模型放行，不阻断入库）
  - `confirmed_thread_address?: string | null`, `discord_body_phase?: string | null`
- **状态迁移表（集中于 `_validate_delivery_status_transition`）与 `error_code` 保护**：
  - 合法状态集合：`in_flight`、`sent`、`partial`、`failed`、`unknown`（其余值如 `deferred` / `partial_deferred` 一律返回 `400`）。
  - 允许迁移：
    - `in_flight` → `in_flight | sent | partial | failed | unknown`
    - `partial` → `partial | sent | failed | unknown`
    - `sent` → 仅允许幂等 `sent`（不可回到 `in_flight` 或改为其他终态，违规 `409`）
    - `failed` → 仅允许幂等 `failed`（失败重试必须经 I-6 重新认领，不可经 I-7 回到 `in_flight`，违规 `409`）
    - `unknown` → 仅允许幂等 `unknown`（违规 `409`）；**唯一例外**：Discord 非 `data_task` 投递在 `error_code == "gateway_restarted"`、`discord_body_phase == "dispatching"` 且 `delivery_attempt` 精确匹配时允许收敛到 `sent | failed | partial`。
  - **防 `error_code` 篡改绕过保护**：
    - Discord 投递的 `error_code == "gateway_restarted"` 专供后端 `recover_channel_state(mode="startup")` 写入，I-7 显式写入一律返回 `409`；
    - 既有 `status == "unknown"` 的投递行（含省略 `status` 的进度更新）禁止修改 `error_code`（传入与现值不同的 `error_code` 返回 `409`，同值/省略均不改写 `error_code` 与 `error_message`）。
- **响应 `200`**：
  ```json
  {
    "ok": true,
    "delivery_key": "string",
    "status": "in_flight | sent | partial | failed | unknown",
    "sent_to_current_count": 0,
    "confirmed_thread_address": "string | null",
    "discord_body_phase": "before_body | dispatching | null",
    "delivery_attempt": 1
  }
  ```
- **错误**：`400`（参数缺失、非法状态、`status` 与 `outcome` 冲突）、`404`、`409`（`stale_gateway_epoch` / `delivery_attempt` 失配 / 非法状态迁移 / Discord `unknown` 保护 / `error_code` 保护）

### 6.4 I-7 (2)：`POST /internal/inbound/progress`

- **请求头**：`X-Service-Token`；**平台以数据库实际记录为准，加锁顺序严格为「入站锁 → 线程平台事务咨询锁 → 租约 `FOR SHARE` → 账号 → 会话 → 监听 → 后台运行」**——
  - 事务最先获取入站咨询锁 `dalton_inbound:{platform}:{message_id}` 及按排序后的目标 `thread_id` 获取 `pg_advisory_xact_lock(hashtext('dalton_thread_platform:' || thread_id))`（严格先于显式 `X-Gateway-Epoch` 的租约行 `FOR SHARE`；解绑流程不拿此咨询锁，无死锁环），在咨询锁内为请求涉及的每个 `thread_id`（含库中尚无任何记录的新线程）跨顶层入站事件、`thread_watch`、`session_interrupt_update` 与 `background_run` 统一裁定唯一平台，同一请求内不同实体对同一 `thread_id` 携带冲突平台时直接返回 `409 platform_mismatch` 并整笔回滚（零写入）；
  - 预检查不对 `dalton_channel_thread_watches` / `dalton_channel_background_runs` 取锁，避免与 `unbind_weixin_account`（`weixin_accounts → session_generations → thread_watches → background_runs`）或并发写升锁形成死锁；
  - 请求带有顶层 `thread_id` 时（即使仅更新 `dalton_channel_inbound_events`，不带监听、中断或后台运行），一律在线程平台咨询锁内用同一库内平台查询核对目标线程平台；目标线程为微信时强制校验 `X-Gateway-Epoch`，平台不一致返回 `409 platform_mismatch`，禁止建立跨平台关联；
  - 首次写入按 `dalton_channel_thread_watches (无锁)` → **`conversation_threads.platform (FOR SHARE)`** → `dalton_channel_inbound_events (by thread_id)` → `dalton_channel_background_runs / inbound_events / conversation_responses (仅采用 thread_id 等于请求目标线程的记录)` 顺序推导权威平台，最后仅在 `(platform, message_id)` 已明确绑定到目标 `thread_id`/`run_id` 时才参考（绝不采用另一线程的运行或未绑定入站行）；
  - 写入微信监听行 / 后台运行 / 会话中断前，先通过无锁预读收集本次可能引用的全部微信 `account_id`（含请求显式传入的 `account_id`、目标用户账号、以及已有 `dalton_channel_thread_watches` / `dalton_channel_background_runs` 上的 `weixin_account_id`），按 `account_id` 排序统一取 `FOR KEY SHARE`，再锁 `dalton_channel_session_generations (FOR UPDATE)`，再锁 `dalton_channel_thread_watches (FOR UPDATE)` 与 `dalton_channel_background_runs (FOR UPDATE)` 并核对库内平台、微信 `X-Gateway-Epoch` 及实际引用的 `weixin_account_id` 是否均在已锁集合内（若发现漏锁账号则回滚并带上该账号重试一次，仍不一致返回 `409 weixin_account_lock_conflict`）；
  - 若 `session_interrupt_update` 的会话校验失败（`session_is_current_for_interrupt == false`），且目标 `dalton_channel_thread_watches` 自身代数/用户/范围/平台与请求不一致（例如会话与监听均已推进到代数 2 而请求仍带代数 1），无论单发 `session_interrupt_update` 还是混合携带 `thread_watch` 均绝不改写 `dalton_channel_thread_watches.pending_interrupt`；当会话已推进到新代数而后台旧线程监听仍处于请求代数时，允许按监听自身代数更新监听中断（清理 `pending_interrupt: null` 时同样校验 `interrupt_id` / `run_id` / `thread_id` 与监听上既有中断匹配，防止旧清理误清同代新中断）；
  - 若仅提交 `session_interrupt_update` 且省略 `platform`、预查未命中，则按 `thread_id` 锁监听行读取平台：若发现预查后新提交的监听行，则回滚本次事务并按「预查已知平台」的主路径（账号 → 会话 → 监听，完整执行会话更新与代数/用户/范围校验）重试一次，重试后仍不一致返回 `409 platform_mismatch`；若仍无监听行返回 `400 missing_platform`；
  - 写入在 `ON CONFLICT DO UPDATE` 上附加 `WHERE <table>.platform = EXCLUDED.platform`，微信行并发冲突一律返回 `409 platform_mismatch` 或 `409 stale_gateway_epoch` 并回滚整笔事务。飞书 / Discord 合法请求可省略 `X-Gateway-Epoch`。
- **请求体**：`platform?`, `message_id?`, `status?`, `thread_id?`, `run_id?`, `turn_index?`, `request_key?`, `retry_request_key?`, `resubmitted_request_key?`, `steered_into_run_id?`, `fallback_pending?`, `uploaded_attachments?`, `raw_payload?`, `raw_payload_patch?`, `command_effect_status?`, `command_reply_pending?`, `command_result?`, `session_interrupt_update?`, `background_run?`, `thread_watch?`
- **响应 `200`**：
  ```json
  {
    "ok": true,
    "run_id": "string | null",
    "has_in_flight_current_text": false,
    "has_unknown_current_text": false,
    "has_in_flight_or_unknown_current_text": false,
    "sent_to_current_count": 0,
    "session_is_current_for_interrupt": "bool | null"
  }
  ```
- **错误**：`400`（如仅提交 `session_interrupt_update` 且无法解析 `platform` 返回 `400 missing_platform`）、`409 stale_gateway_epoch`、`409 {"code": "platform_mismatch", "message": "..."}`

### 6.5 I-7 (3)：`POST /internal/steers/reconcile`

- **请求头**：`X-Service-Token`；微信必填 `X-Gateway-Epoch`；飞书 / Discord 可省略
- **请求体**：`platform: string`, `message_id: string`, `run_id?: string | null`
- **响应 `200`**：
  ```json
  {
    "ok": true,
    "reconcile_status": "consumed | pending_resubmit | returned_unresubmitted | still_pending",
    "outcome": "string | null",
    "run_status": "string | null",
    "inbound_status": "string",
    "resubmitted_request_key": "uuid-str | null"
  }
  ```
- **错误**：`404`、`409 stale_gateway_epoch`

### 6.6 I-7 (4)：`GET /internal/recover`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（租约表有记录后必填）
- **查询参数**：`mode: "startup" | "watch_reconnect" = "startup"`, `thread_id?: uuid-str | null`
- **响应 `200`**：
  - `mode == "watch_reconnect"`：`{"mode": "watch_reconnect", "background_runs": Array<object>, "thread_watches": Array<object>}`
  - `mode == "startup"`：`{"mode": "startup", "inbound_events": Array<object>, "background_runs": Array<object>, "thread_watches": Array<object>, "automation_runs": Array<object>}`（锁序严格为 `weixin_accounts FOR KEY SHARE` → 仅锁已锁账号集合内的 `thread_watches FOR UPDATE` + 新行重检，零死锁空隙）

### 6.7 I-8：自动化运行登记与结束

- **`POST /internal/automation-runs`**（`X-Service-Token`，无需 `X-Gateway-Epoch`）
  - **请求体**：`{"execution_id": uuid-str, "user_id"?: string, "workspace_id"?: uuid-str | null, "automation_name": string = "", "entries": Array<string>, "thread_id"?: uuid-str | null}`
  - **响应 `200`**：`{"targets": Array<AutomationRunTarget>}`，其中每个 `AutomationRunTarget` 包含：
    - `entry: string`
    - `address: string | null`
    - `name: string`
    - `ok: bool`
    - `message: string`
  - **错误**：`400`
- **`POST /internal/automation-runs/{execution_id}/finish`**（`X-Service-Token`，无需 `X-Gateway-Epoch`）
  - **请求体**：`{"status"?: string | null, "thread_id"?: uuid-str | null, "final_text"?: string | null, "targets"?: Array<object> | null, "mark_finished": bool = false}`
  - **响应 `200`**：
    ```json
    {
      "execution_id": "uuid-str",
      "status": "finishing | finished",
      "previous_status": "registered | finishing | finished",
      "user_id": "string",
      "workspace_id": "uuid-str | null",
      "thread_id": "uuid-str | null",
      "automation_name": "string",
      "finish_status": "string | null",
      "final_text": "string | null",
      "targets": [
        {
          "entry": "string",
          "address": "string | null",
          "name": "string",
          "ok": true,
          "message": "string",
          "agent_sent": false
        }
      ]
    }
    ```
    （新登记的运行初始状态为 `"registered"`；`mark_finished: false` 首次认领时将状态置为 `"finishing"` 并在每个 `targets[i]` 上标记 `agent_sent: bool`；`mark_finished: true` 时无论是否携带 `targets` 均收敛为 `"finished"`〔省略 `targets` 时保留库内已有 `targets`〕；已处于 `"finished"` 的运行永远不会被改回 `"finishing"`）
  - **错误**：`404`（未知 `execution_id`）

### 6.8 工作区附件上传门禁：`POST /api/v1/workspaces/{workspace_id}/files/upload`

- **适用场景**：网关在微信入站消息携带附件时向工作区沙箱上传文件，由后端在获取沙箱前、写盘前与写盘后三次校验账号代际、机器人身份与租约围栏，避免已解绑或已轮换的旧账号把文件落盘到工作区。
- **微信上传请求头**：
  - `X-Service-Token` + `X-User-Id`（网关服务代调用鉴权，与其余工作区代理请求一致；网页端使用用户 JWT）
  - `X-Weixin-Account-Id: uuid-str`（触发校验的微信账号 ID）
  - `X-Weixin-Bot-Generation: int (>=1)`（触发校验的账号当前代际）
  - `X-Weixin-Bot-Id?: string`（可选；网关在携带微信上传头时一并发送当前入站账号对应的 `ilink_bot_id`，后端核对其与 `dalton_channel_weixin_accounts.ilink_bot_id` 一致，防止换绑不同机器人或旧 bot 实例继续写入工作区）
  - `X-Gateway-Epoch: int (>=1)`（当前网关租约 epoch）
- **兼容不变量**：未携带 `X-Weixin-Account-Id`、`X-Weixin-Bot-Generation` 与 `X-Weixin-Bot-Id` 的上传（飞书 / Discord / 网页端）直接跳过该门禁，行为完全不变。
- **校验逻辑（三次门禁 + 单路径写锁内临时文件原子发布）**：
  1. 获取沙箱前先执行快速校验；
  2. 获取沙箱并读完上传内容后，以与 `aupload_file_bytes` 完全一致的规范化目标路径获取可重入写锁 `sandbox.path_write_lock(lock_path)`，**在同一把写锁内**依次完成：
     - 写入前门禁校验（不通过直接返回 `409`，零写入）；
     - 先将文件字节写入目标路径同父目录下的唯一临时文件 `.dalton-wx-upload-<uuid4>.tmp`；
     - 写入临时文件后、发布到目标路径前再次执行门禁校验；若不通过，**仅清理该临时文件（`rm -f`）并返回 `409`，绝不触碰目标路径文件**（避免覆盖既有文件或误删并发后到的新上传），亦不记录上传字节或触发快照同步；
     - 仅当写后校验通过，才在同一把写锁内执行 `mv -f <staged_path> <normalized>` 原子替换目标路径（若 `mv -f` 失败则清理临时文件并返回 `500`）。
  每次校验均不跨沙箱操作持数据库事务，且均核对：`channel_weixin` 能力开启、`X-Gateway-Epoch` 为当前有效租约、目标 `account_id` 归属当前 `user_id`、状态处于 `active` / `pending_verification`、数据库当前 `bot_generation` 与请求头一致，且若请求携带 `X-Weixin-Bot-Id` 则与数据库 `ilink_bot_id` 完全一致。
- **错误（均为顶层 JSON 字段，不嵌套在 `detail` 内）**：
  - `400 {"code": "invalid_weixin_upload_headers", "message": "..."}`（`X-Weixin-Account-Id` 非合法 UUID 或 `X-Weixin-Bot-Generation` 非正整数）
  - `404`（`channel_weixin` 能力关闭）
  - `409 {"code": "stale_gateway_epoch", "message": "..."}`（`X-Gateway-Epoch` 缺失、非法或已失效）
  - `409 {"code": "weixin_bot_generation_superseded", "message": "..."}`（账号不存在、非本人、已停用/过期、`bot_generation` 与库内当前代际不一致，或 `X-Weixin-Bot-Id` 与库内 `ilink_bot_id` 不一致）
