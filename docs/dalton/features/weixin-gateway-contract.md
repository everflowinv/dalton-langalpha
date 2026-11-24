# 团队个人微信多账号 — 网关与后端内部接口契约

> 本文档为后端（批次 A）与渠道网关（批次 B）之间的**唯一接口真值契约**，严格以后端实现（`src/dalton/app/channels_router.py`、`src/dalton/database/channel_gateway.py`、`src/dalton/database/weixin_multi_account.py`、`src/dalton/database/data_task_notifications.py`、`src/dalton/database/automation_notifications.py`）为准。设计文档见 [weixin-multi-account.md](./weixin-multi-account.md) §3.1。

---

## 0. 公共约定

- **路由前缀**：除特别标明「后端 → 网关」外，全部挂载于 `/api/v1/dalton/channels/internal/*`。
- **服务鉴权**：全部内部接口要求请求头 `X-Service-Token: <INTERNAL_SERVICE_TOKEN>`（不匹配返回 `401`）。所有接口均不接受用户 JWT。
- **能力门控**：总能力 `CapabilityId.CHANNEL_GATEWAY` 关闭时全部返回 `404`；微信专属接口及目标平台解析为 `weixin` 的共享接口额外要求 `CapabilityId.CHANNEL_WEIXIN` 开启（关闭返回 `404`）。
- **错误响应格式**：结构化错误返回 JSON `{"code": "<error_code>", "message": "<human_message>", ...}`（HTTP `400`/`401`/`403`/`404`/`409`/`503`）。
- **`X-Gateway-Epoch` 租约围栏规则（集中于 `verify_gateway_epoch_in_tx`）**：
  - **始终免带（只读 / 租约获取 / 配置读取）**：`POST .../gateway-lease/acquire`、`GET .../weixin/accounts/{account_id}`、`GET .../weixin/login-sessions/{session_key}`、`GET .../weixin/collect-buffers/{buffer_id}`、`GET .../state`（I-4）、`PUT .../settings`（I-5）、`POST .../automation-runs` 与 `POST .../automation-runs/{execution_id}/finish`（I-8）。
  - **微信始终强制（缺头、过期或失配一律 `409 {"code": "stale_gateway_epoch"}`）**：全部微信写入接口、所有目标平台为 `weixin`（含数据库既有行或会话来源解析为 `weixin`）的 I-1 / I-2 / I-3 / I-6 / `preflight-send` / I-7（含 `deliveries/progress`、`inbound/progress`、`steers/reconcile`、`deliveries/deferred-due`）。
  - **飞书 / Discord 区分入站与出站**：
    - **I-1 / I-2 / I-3 / `recover`**：租约表从未出现租约行时允许省略；一旦租约表有记录，省略或过期/失配返回 `409 stale_gateway_epoch`。
    - **I-6 / I-7（`authorize-and-claim`、`deliveries/deferred-due`、`deliveries/progress`、`inbound/progress`、`steers/reconcile`）**：即使租约表已有记录，非微信平台（飞书 / Discord）**省略 `X-Gateway-Epoch` 仍放行**（支撑网关 `outgoing` 无租约模式）；但**只要显式携带了 `X-Gateway-Epoch`**，后端仍按租约行校验，过期/失配返回 `409 stale_gateway_epoch`。
- **旧地址规范化**：微信私聊规范地址为 `weixin:@me`；历史库行遗留的 `weixin:dm:<id>` 在 I-6 当前会话匹配与 `notice` 目标核对时自动规范化为 `weixin:@me` 同等对待。
- **I-9 已移除**：单账号旧接口 `/internal/weixin/state` 已下线，批次 A 与批次 B 同步部署。

---

## 1. 网关单实例租约

### 1.1 `POST /internal/gateway-lease/acquire`

- **请求头**：`X-Service-Token`（不带 `X-Gateway-Epoch`）
- **请求体**：
  - `instance_id: string`（必填，1..255）
  - `ttl_seconds: int`（可选，默认 `45`，范围 `5..300`）
- **响应 `200`**：
  - 获租：`{"granted": true, "epoch": int, "instance_id": string, "expires_at": ISO8601}`
  - 已被他人持有且未过期：`{"granted": false, "epoch": int, "holder_instance_id": string, "expires_at": ISO8601, "retry_after_ms": int}`（默认 `3000`）

### 1.2 `POST /internal/gateway-lease/heartbeat`

- **请求头**：`X-Service-Token`
- **请求体**：`instance_id: string`, `epoch: int (>=1)`, `ttl_seconds: int = 45 (5..300)`
- **响应 `200`**：`{"ok": true, "epoch": int, "expires_at": ISO8601}`
- **错误**：`409 {"code": "stale_gateway_epoch", "message": "..."}`（失租或已过期）

### 1.3 `POST /internal/gateway-lease/release`

- **请求头**：`X-Service-Token`
- **请求体**：`instance_id: string`, `epoch: int (>=1)`
- **响应 `200`**：`{"ok": true, "released": bool}`

---

## 2. 微信账号同步、详情、游标与状态上报

### 2.1 `POST /internal/weixin/accounts/reconcile`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：`since_revision: int | null = null`
- **响应 `200`**：
  - `changed: bool`（`since_revision` 与当前 `accounts_revision` 不同时为 `true`，固定返回全量活跃与待验证快照）
  - `enabled: bool`
  - `allow_external: bool | null`
  - `max_concurrent_accounts: int`
  - `accounts_revision: int`
  - `accounts: Array<AccountSnapshot>`（当 `changed: false` 时为空数组 `[]`）：
    - `account_id: uuid-str`
    - `user_id: string`
    - `ilink_bot_id: string`
    - `ilink_user_id: string | null`
    - `ilink_username: string | null`
    - `bot_token: string`
    - `base_url: string`
    - `cdn_base_url: string`
    - `get_updates_buf: string`
    - `bot_generation: int`
    - `status: "active" | "pending_verification"`
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
  - `session_key: string`, `user_id: string`, `status: "starting" | "wait" | "scaned" | "verify_code" | "confirmed" | "expired" | "superseded"`, `login_version: int`, `qrcode: string | null`, `qrcode_img_content: string | null`, `redirect_base_url: string`, `expires_at: ISO8601`, `connected: bool`, `already_connected: bool`, `account_id: uuid-str | null`, `bot_generation: int | null`, `accounts_revision: int | null`
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
  - `save_qr`：`{"ok": true, "session_key": string, "status": "wait", "login_version": 2, "expires_at": ISO8601}`
  - `update`：`{"ok": true, "revoked": false, "session_key": string, "status": string, "login_version": int, "redirect_base_url": string, "expires_at": ISO8601}`
  - `confirm`：`{"ok": true, "account_id": uuid-str, "connected": true, "already_connected": false, "bot_generation": int, "accounts_revision": int, "status": "active" | "pending_verification"}`
- **错误（三类 `action` 统一）**：
  - **`409 {"code": "login_session_revoked", "message": "扫码会话已撤销或已过期"}`**（会话已撤销 / 被取代 / 过期 / 版本冲突，0 行命中整笔回滚）
  - `409 {"code": "stale_gateway_epoch"}`
  - `409 {"code": "ilink_user_already_bound"}` / `409 {"code": "weixin_capacity_reached"}`（`confirm` 占用或容量已满）
  - `403`（成员不在册或渠道/外部开关关闭）、`503 {"code": "roster_lookup_error"}`

---

## 4. 收集模式（Collect Buffers）

### 4.1 `GET /internal/weixin/collect-buffers/{buffer_id}`

- **请求头**：`X-Service-Token`（只读，无需 `X-Gateway-Epoch`）
- **查询参数**：`message_id?: string`（传入时同时返回该条目的附件上传状态）
- **响应 `200`**：
  - `ok: true`
  - `buffer_id: uuid-str`
  - `status: "collecting" | "flushing" | "done" | "cancelled" | "expired"`
  - `account_id: uuid-str | null`
  - `dalton_user_id: string`
  - `workspace_id: uuid-str`
  - `scope_key: string`
  - `session_generation: int`
  - `bot_generation: int`
  - `item_count: int`
  - `expires_at: ISO8601`
  - `max_expires_at: ISO8601`
  - **`upload_status: "pending_upload" | "ready" | "failed" | null`**（规范统一字段；仅传 `message_id` 且条目存在时非空）
  - `item_upload_status: "pending_upload" | "ready" | "failed" | null`（同 `upload_status`，兼容别名）
- **网关上传前判定规则**：仅当 `status == "collecting"`（或 `status == "flushing"` 且 `upload_status == "pending_upload"`）时才继续下载并上传工作区附件；任何 `cancelled` / `expired` / `done`、或 `upload_status != "pending_upload"`、或本接口请求失败（超时 / 5xx）时必须中止上传。
- **错误**：`400`、`404`

### 4.2 `POST /internal/weixin/collect-buffers/{buffer_id}/items/{message_id}/attachments`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **请求体**：
  - `uploaded_attachments: Array<object>`（默认 `[]`）
  - `upload_failed: bool = false`
  - `error_message: string | null = null`
- **响应 `200`**：
  - 正常写入（`collecting`，或 `flushing` 且条目仍为 `pending_upload`）：
    `{"ok": true, "upload_status": "ready" | "failed", "collect_buffer_status": "collecting" | "flushing"}`
  - 窗口已取消或过期（不写 `collect_items`，同步退役入站行）：
    `{"ok": true, "upload_status": "cancelled", "collect_buffer_status": "cancelled" | "expired", "code": "collect_buffer_cancelled", "message": "收集窗口已取消"}`
  - 窗口已完成（`done`）或 `flushing` 下条目已非 `pending_upload`（不写 `collect_items`，清空入站媒体密文）：
    `{"ok": true, "upload_status": "too_late", "collect_buffer_status": "done" | "flushing", "code": "collect_buffer_too_late", "message": "收集窗口已结束，附件补登已过时"}`
- **错误**：`400`、`404`、`409 stale_gateway_epoch`

### 4.3 `POST /internal/weixin/collect-buffers/{buffer_id}/finalize`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填）
- **响应 `200`**：
  - `ok: true`
  - `status: "done" | "cancelled" | "expired"`
  - `flushed_inbound: SyntheticCollectInbound | null`（仅 `status == "done"` 时非空）：
    - `platform: "weixin"`, `message_id: "collect:<buffer_id>"`, `request_key: uuid-str`, `dalton_user_id: string`, `workspace_id: uuid-str`, `thread_scope_key: string`, `external_thread_id: string`, `generation: int`, `weixin_account_id: uuid-str | null`, `weixin_bot_generation: int | null`, `uploaded_attachments: Array<object>`, `status: string`
    - **`raw_payload`**: `{"platform_user_id": string, "text": string, "generation": int, "collect_buffer_id": uuid-str, "collected_item_count": int, "synthetic_collect": true}`（正文与发件人均在 `raw_payload` 内）
- **错误**：`400`、`404`、`409 stale_gateway_epoch`

---

## 5. 入站、绑定与会话指令（I-1 / I-2 / I-3）

### 5.1 I-1：`POST /internal/inbound/register-and-resolve`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `platform: "feishu" | "discord" | "weixin"`, `platform_user_id: string`, `chat_type: "dm" | "group" = "dm"`, `chat_address: string`, `thread_scope_key: string`, `message_id: string`, `request_key?: uuid-str | null`, `raw_payload: object = {}`
  - 微信附加字段：`weixin_account_id?: uuid-str | null`, `weixin_context_token?: string | null`, `weixin_bot_id?: string | null`, `weixin_bot_generation?: int | null`, `weixin_context_seq?: int | null`, `weixin_media_meta?: Array<object> | null`, `unsupported_item_types?: Array<string> | null`
- **响应 `200`（关键字段）**：
  - `ok: bool`, `linked: bool`, `allowed: bool`, `deny_reason: string | null`, `existing_status: string | null`, `status: string`, `dalton_user_id?: string`, `workspace_id?: uuid-str`, `generation?: int`, `external_thread_id?: string`, `has_usable_weixin_context_token?: bool`
  - 收集模式命中（**首次登记与重复 `message_id` 重放均返回**；重复重放不改写已有 `encrypted_weixin_media_meta`）：
    - `collection_mode: "appended" | "limit_reached"`
    - `collect_buffer_id: uuid-str`
    - `collect_buffer_status: "collecting" | "flushing" | "done" | "cancelled" | "expired"`
  - 待验证冲突自动解绑时：
    - `ok: false`, **`deny_reason: "ilink_user_conflict_auto_unbound"`**
    - `one_time_weixin_reply?: {"bot_token": string, "base_url": string, "ilink_bot_id": string, "recipient_id": string, "context_token": string, "text": string}`
- **常见 `deny_reason` 枚举**：`weixin_inbound_fields_required`, `weixin_group_unsupported`, `invalid_inbound_event`, `unsupported_merged_forward`, `not_linked`, `member_not_in_roster`, `weixin_channel_disabled`, `external_access_disabled`, `weixin_bot_generation_mismatch`, `weixin_message_id_account_mismatch`, `ilink_user_conflict_auto_unbound`, `collect_limit_reached`
- **错误**：`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`（网关保留游标稍后重试）

### 5.2 I-2：`POST /internal/bind-code/consume`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `action: "bind" | "unbind" = "bind"`, `platform: "feishu" | "discord" | "weixin"`, `platform_user_id?: string | null`, `code?: string | null`, `is_group: bool = false`, `user_id?: string | null`, `message_id?: string | null`
  - 微信 `/unbind` 必填字段（任一缺失返回 `400`）：`weixin_account_id`, `weixin_bot_id`, `weixin_bot_generation`, `platform_user_id`；可选 `weixin_context_token`（用于签发一次性告别回执）
- **响应 `200`**：
  - `ok: bool`, `reason?: string`, `user_id?: string`, `one_time_weixin_reply?: OneTimeReplyEnvelope | null`
- **错误**：`400`、`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`

### 5.3 I-3：`POST /internal/session/command`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（微信必填；飞书/Discord 租约出现后必填）
- **请求体**：
  - `cmd: string`（含 `new`, `stop`, `workspace`, `workspace_list`, `collect`, `done`, `cancel`, `help` 等）, `platform`, `scope_key`, `platform_user_id?`, `dalton_user_id?`, `chat_type = "dm"`, `chat_address?`, `message_id?`, `request_key?`, `raw_payload?`, `command_arg?`, `is_internal_query: bool = false`
  - 微信附加字段：`weixin_account_id?`, `weixin_context_token?`, `weixin_bot_id?`, `weixin_bot_generation?`, `weixin_context_source?`, `weixin_context_seq?`
- **响应 `200`**：
  - 公共字段：`ok: bool`, `duplicate?: bool`, `deny_reason?: string | null`（冲突自动解绑统一为 **`"ilink_user_conflict_auto_unbound"`**）, `reply_text?: string`, `command_reply_pending?: bool`, `generation?: int`, `one_time_weixin_reply?: OneTimeReplyEnvelope | null`
  - `/collect`：`collect_status: "collecting"`, `collect_buffer_id: uuid-str`, `workspace_id: uuid-str`
  - `/cancel`：`collect_status: "cancelled" | "not_collecting"`, **`cancelled_buffer_ids: Array<uuid-str>`**（复数数组，网关应只中止本数组列出的窗口）
  - `/done`：`collect_status: "done" | "empty" | "cancelled" | "not_collecting"`, `collect_buffer_id?: uuid-str`, `flushed_inbound?: SyntheticCollectInbound | null`
- **不变量**：既有 `(platform, message_id)` 行存在且发件人不匹配（含请求省略 `platform_user_id` 而查得身份不匹配，或请求同时标为 `invalid_inbound_event`）时，仅返回拒绝，绝不覆盖原记录。
- **错误**：`409 stale_gateway_epoch`、`503 {"code": "roster_lookup_error"}`

---

## 6. 状态查询与用户设置（I-4 / I-5）

### 6.1 I-4：`GET /internal/state`

- **请求头**：`X-Service-Token`（只读，无需 `X-Gateway-Epoch`）
- **查询参数**：`user_id?`, `thread_id?`, `run_id?`, `platform?`, `external_thread_id?`
- **响应 `200`**：`user_tier`, `home_workspace_id`, `identities`, `settings`, `chat_bindings`, `current_conversation`（仅当 `channel_weixin` 启用时才输出微信键）

### 6.2 I-5：`PUT /internal/settings`

- **请求头**：`X-Service-Token`（无需 `X-Gateway-Epoch`）
- **请求体**：`user_id?`, `version: string`（CAS）, `settings?: object`, `automation_output_update?: object`
- **响应 `200`**：更新后的设置与新 `version`；`409`（CAS 版本冲突）；`404`（禁用子能力平台被修改）

---

## 7. 出站认领、预检、暂缓重试与进度结算（I-6 / Preflight / I-7 / I-8）

### 7.1 I-6：`POST /internal/deliveries/authorize-and-claim`

- **请求头**：`X-Service-Token`；微信必填 `X-Gateway-Epoch`；飞书 / Discord 可省略（显式携带仍会校验）
- **请求体**：
  - `delivery_key: string`, `kind: "agent_send" | "fallback" | "interrupt" | "automation" | "notice" | "data_task" | "automation_notify"`, `user_id?`, `thread_id?`, `run_id?`, `execution_id?`, `automation_execution_id?`, `platform?`, `target?`, `inbound_message_id?`, `has_non_empty_text: bool = true`, `total_chunks: int = 0`, `source_scope_key?`, `source_generation?: int`, `activity_origin?: "conversation" | "replay" | "notification" | "command" | "none"`
- **响应 `200`（按 `claim_status` 分支）**：
  1. **`claim_status: "claimed"`**：
     - `recipient_id: string`, `platform: string`, `target_address: string`, `delivery_attempt: int`, `is_current: bool`, `thread_scope_key: string | null`, `home_workspace_id: uuid-str | null`, `confirmed_thread_address: string | null`, `discord_body_phase: string | null`
     - 已送达断点（供 `partial_deferred` 重试续发剩余段/附件）：`sent_chunks: int`, `total_chunks: int`, `platform_message_ids: Array<any>`, `files_status: Array<object>`, `delivered_segments: Array<any>`
     - 微信附加字段：`weixin_account_id: uuid-str`, `weixin_bot_id: string`, `weixin_bot_generation: int`, `weixin_context_token: string`, `weixin_context_token_version: int | null`
  2. **`claim_status: "deferred"`**（未到 `next_retry_at`，或认领时名册查询失败 `roster_lookup_error` 落库暂缓）：
     - `status: "deferred" | "partial_deferred"`, `code: "delivery_deferred" | "roster_lookup_error"`, `retryable: true`, `defer_count: int`, `first_deferred_at: ISO8601`, `next_retry_at: ISO8601`, `retry_after_seconds: int`, **`retry_after_ms: int`**, `delivery_attempt: int`, `platform`, `target_address`, `sent_chunks`, `total_chunks`, `platform_message_ids`, `files_status`, `delivered_segments`, `is_current`
  3. **`claim_status: "already_completed"`**：`status: "sent" | "partial"`, `first_response_json`, `platform`, `target_address`, `delivery_attempt`
  4. **`claim_status: "in_flight_or_unknown"`**：`status: "unknown"`, `code: "in_flight"`, `platform`, `target_address`, `delivery_attempt`
  5. **`claim_status: "denied"`**：`status: "failed" | "unknown"`, `code: string`, `message: string`
     - 常见拒绝码：`deferred_timeout`（已超 24 小时上限，**永久终态，不再允许重新认领为 `in_flight`**）、`session_generation_superseded`（在认领同事务 `FOR SHARE` 核验当前会话代数失配，覆盖 `notice` 与普通回复）、`source_generation_unknown`（标 `unknown` + 审计）、`weixin_bot_generation_superseded`、`weixin_account_rebound`、`weixin_session_expired`、`weixin_context_token_stale`、`missing_weixin_context_token`、`member_not_in_roster`、`weixin_channel_disabled`、`external_access_disabled`、`not_linked`、`not_allowed`
- **错误**：`400`、`404`（平台能力关闭）、`409 stale_gateway_epoch`

### 7.2 发送前实时预检：`POST /internal/deliveries/{delivery_key}/preflight-send`

- **请求头**：`X-Service-Token`, `X-Gateway-Epoch`（必填；先做 `channel_weixin` 能力检查再解析 epoch）
- **请求体**：`delivery_attempt: int`, `chunk_index?: int | null`
- **响应 `200`**：
  - 允许：`{"authorized": true, "decision": "allow", "retryable": false, "account_id": uuid-str, "ilink_bot_id": string, "bot_generation": int, "bot_token": string, "base_url": string, "cdn_base_url": string, "recipient_id": string, "weixin_context_token": string, "weixin_context_token_version": int | null}`
  - 暂缓（名册 `lookup_error`）：`{"authorized": false, "decision": "defer", "retryable": true, "deny_code": "roster_lookup_error"}`
  - 拒绝：`{"authorized": false, "decision": "deny", "retryable": false, "deny_code": string}`
- **错误**：`404`（能力关闭）、`409 stale_gateway_epoch`

### 7.3 到期暂缓投递轮询：`GET /internal/deliveries/deferred-due`

- **职责（自动重试机制）**：由持租网关定时轮询（建议每 10–15 秒，启动恢复亦返回初始清单）。后端在同一短事务内先将全部已超 24 小时上限的 `deferred` / `partial_deferred` 行扫描置为终态 `failed`（`error_code: "deferred_timeout"`）并写微信审计，再返回当前 `next_retry_at <= NOW()` 的到期投递列表，由网关按 `delivery_key` 经 I-6 重新认领并续发。
- **请求头**：`X-Service-Token`；查询 `platform=weixin` 时必填 `X-Gateway-Epoch`；非微信可省略
- **查询参数**：`platform?: "feishu" | "discord" | "weixin"`, `limit: int = 50 (1..200)`
- **响应 `200`**：
  - `ok: true`
  - `deliveries: Array<DeferredDeliveryRow>`（含 `delivery_key`, `kind`, `user_id`, `thread_id`, `run_id`, `execution_id`, `platform`, `target_address`, `status: "deferred" | "partial_deferred"`, `delivery_attempt`, `defer_count`, `first_deferred_at`, `next_retry_at`, `sent_chunks`, `total_chunks`, `platform_message_ids`, `files_status`, `delivered_segments`, `source_scope_key`, `source_generation`, `activity_origin`, `weixin_account_id`, `weixin_bot_id`, `weixin_bot_generation`, `weixin_recipient_id`, `error_code`）
  - `expired_count: int`（本次扫描转为 `failed` 的超 24h 条数）
  - `next_upcoming_retry_at: ISO8601 | null`（下一条未到期投递的最早重试时间）

### 7.4 I-7 (1)：`POST /internal/deliveries/progress`

- **请求头**：`X-Service-Token`；库中投递行 `platform == "weixin"` 时必填 `X-Gateway-Epoch`；飞书 / Discord 可省略（显式携带仍会校验）
- **请求体**：
  - `delivery_key: string`
  - `status?: "in_flight" | "sent" | "partial" | "failed" | "unknown" | "deferred" | "partial_deferred" | null`
  - `outcome?: "deferred" | "partial_deferred" | "sent" | "partial" | "failed" | "unknown" | null`
  - `delivery_attempt?: int | null`（传入时必须匹配当前尝试号，否则 `409`）
  - `sent_chunks?: int | null`, `total_chunks?: int | null`, `platform_message_ids?: Array<any> | null`, `files_status?: Array<object> | null`
  - **`delivered_segments?: Array<any> | null`**（部分送达暂缓 `partial_deferred` 时记录已送达段标识，后端单调合并保存，供重试只发剩余部分）
  - `first_response_json?: object | null`, `error_code?: string | null`, `error_message?: string | null`
  - `confirmed_thread_address?: string | null`, `discord_body_phase?: string | null`
- **暂缓规则（`outcome`/`status` 为 `"deferred"` 或 `"partial_deferred"`）**：
  - **受保护状态优先**：Discord `unknown` 保护先于暂缓分支执行，且同时检查 `status` 与 `outcome`；`sent` / `partial` 终态不可暂缓（返回 `409`）。
  - **部分送达语义**：传 `outcome: "partial_deferred"`，或暂缓时已有 `sent_chunks > 0` / 已送达附件 / 非空 `delivered_segments`，数据库状态写入 `partial_deferred` 并保留全部已送达进度，不再提前结算为终态 `partial`。
  - **同尝试号幂等**：同一 `delivery_attempt` 重复上报暂缓（如响应丢失重送）返回 `idempotent_replay: true`，**不增加 `defer_count`、不延长 `next_retry_at`、不重复写审计**。
  - **指数退避与 24h 上限**：`backoff_seconds = min(600, 30 * 2^(defer_count - 1))`（30s → 60s → 120s → 240s → 480s → 600s 封顶）；距 `first_deferred_at` 满 24 小时后自动转为终态 `status: "failed"`, `error_code: "deferred_timeout"`, `expired_to_failed: true`。
  - **审计原因仅用白名单枚举**：写入 `dalton_channel_weixin_audit_logs.reason` 仅按错误码白名单映射固定中文文案，绝不落盘调用方自由文本 `error_message`。
- **响应 `200`**：
  - 暂缓 / 超限：`{"ok": true, "delivery_key", "status": "deferred" | "partial_deferred" | "failed", "outcome": "deferred" | "partial_deferred" | "failed", "defer_count": int, "first_deferred_at": ISO8601, "next_retry_at": ISO8601 | null, "retry_after_seconds": int | null, "retry_after_ms": int | null, "expired_to_failed": bool, "idempotent_replay": bool, "sent_chunks": int, "total_chunks": int, "platform_message_ids": Array<any>, "files_status": Array<object>, "delivered_segments": Array<any>, "sent_to_current_count": 0, "delivery_attempt": int}`
  - 常规结算：`{"ok": true, "delivery_key", "status", "sent_to_current_count", "confirmed_thread_address", "discord_body_phase", "delivery_attempt"}`
- **错误**：`400`、`404`、`409`（`stale_gateway_epoch` / `delivery_attempt` 失配 / 受保护状态不可改写）

### 7.5 I-7 (2)：`POST /internal/inbound/progress`

- **请求头**：`X-Service-Token`；**平台以数据库实际记录 / 持久化会话来源为准**——库中已有 `thread_watches` / `background_runs` / 源入站行为 `weixin` 时强制要求有效 `X-Gateway-Epoch`；请求体平台与库中平台不一致时直接返回 **`409 {"code": "platform_mismatch"}`** 且不写任何字段。飞书 / Discord 合法请求可省略 `X-Gateway-Epoch`。
- **请求体**：`platform?`, `message_id?`, `status?`, `thread_id?`, `run_id?`, `turn_index?`, `request_key?`, `retry_request_key?`, `resubmitted_request_key?`, `steered_into_run_id?`, `fallback_pending?`, `uploaded_attachments?`, `raw_payload?`, `raw_payload_patch?`, `command_effect_status?`, `command_reply_pending?`, `command_result?`, `session_interrupt_update?`, `background_run?`, `thread_watch?`
- **响应 `200`**：`{"ok": true, ...}`
- **错误**：`409 stale_gateway_epoch`、`409 platform_mismatch`

### 7.6 I-7 (3)：`POST /internal/steers/reconcile`

- **请求头**：`X-Service-Token`；微信必填 `X-Gateway-Epoch`；飞书 / Discord 可省略
- **请求体**：`platform: string`, `message_id: string`, `run_id?: string | null`
- **响应 `200`**：对账结果对象
- **错误**：`404`、`409 stale_gateway_epoch`

### 7.7 I-7 (4)：`GET /internal/recover`

- **请求头**：`X-Service-Token`；`X-Gateway-Epoch`（租约表有记录后必填）
- **查询参数**：`mode: "startup" | "watch_reconnect" = "startup"`, `thread_id?: uuid-str | null`
- **响应 `200`**：
  - `mode == "watch_reconnect"`：`{"mode": "watch_reconnect", "background_runs": [...], "thread_watches": [...]}`
  - `mode == "startup"`：`{"mode": "startup", "inbound_events": [...], "background_runs": [...], "thread_watches": [...], "automation_runs": [...], "flushing_collect_buffers": [...], "deferred_deliveries": [...]}`（启动时自动把超 24h 的暂缓投递转 `failed` 并返回剩余暂缓投递列表；锁序严格为 `weixin_accounts FOR KEY SHARE` → 仅锁已锁账号集合内的 `thread_watches FOR UPDATE` + 新行重检，零死锁空隙）

### 7.8 I-8：自动化运行登记与结束

- `POST /internal/automation-runs`（`X-Service-Token`，无需 `X-Gateway-Epoch`）：请求 `{execution_id, user_id?, workspace_id?, automation_name, entries, thread_id?}` → `200`。
- `POST /internal/automation-runs/{execution_id}/finish`（`X-Service-Token`，无需 `X-Gateway-Epoch`）：请求 `{status?, thread_id?, final_text?, targets?, mark_finished: bool = false}` → `200`。
