# Dalton Supabase 认证部署指南 (`AUTH-DEPLOY.md`)

本文档说明在 `HOST_MODE=oss` 下启用 Dalton Supabase 登录与邮箱允许名单时的公网部署要求、必配环境变量及安全边界。

---

## 1. 前后端必须同时设置的环境变量

前后端认证开关各自读取环境变量，部署时**必须同时启用并对齐配置**（只开一侧会导致前端免登录而后端 401，或前端登录而后端未校验）：

### 后端环境变量
| 变量名 | 必填要求 | 说明 |
|---|---|---|
| `DALTON_AUTH_MODE` | `supabase` | 开启 Dalton Supabase JWT 验签与允许名单（默认 `oss` 免登录） |
| `DALTON_ALLOWED_EMAILS` | 逗号分隔邮箱列表 | 允许访问的邮箱白名单（大小写不敏感；为空时 fail-closed 全部 403） |
| `SUPABASE_URL` | Supabase 项目 URL | 用于拉取 JWKS 验签公钥及 `/auth/v1/settings` 就绪检查（为空时 503） |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase Publishable Key | 用于异步请求 `${SUPABASE_URL}/auth/v1/settings` 校验项目安全配置 |
| `INTERNAL_SERVICE_TOKEN` | 至少 32 个字符的高熵随机串 | 内部服务自派发与受控脚本通道令牌（为空或 `< 32` 字符时启动报错且所有服务令牌路径一律拒绝） |
| `DALTON_PM_USER_IDS` | 逗号分隔的 Supabase 用户 UUID | 拥有 PM 权限的用户 UUID 列表（不能为空、不能含 `local-dev-user` 或非 UUID 值） |

### 前端构建/运行环境变量
| 变量名 | 必填要求 | 说明 |
|---|---|---|
| `VITE_DALTON_AUTH` | `supabase` | 开启前端 Supabase 登录流、403 自动提示登出与禁止前端自助建号 |
| `VITE_SUPABASE_URL` | 与后端 `SUPABASE_URL` 一致 | 前端 Supabase 客户端地址 |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | 与后端 `SUPABASE_PUBLISHABLE_KEY` 一致 | 前端 Supabase 客户端公钥 |

---

## 2. Supabase 项目侧安全基线（就绪检查强制核验）

后端在启动及运行期（成功缓存 TTL 300 秒、失败缓存 TTL 30 秒，单进程 `asyncio.Lock` 防并发拉取）通过 `${SUPABASE_URL}/auth/v1/settings` 强制校验以下三项，任一不满足即 fail-closed 返回 `503`：

1. `disable_signup == true`（禁止公开自助注册）
2. `mailer_autoconfirm == false`（禁止邮箱免确认）
3. `external.anonymous_users` 不为真（禁止匿名用户登录）

---

## 3. 服务令牌（`INTERNAL_SERVICE_TOKEN`）通道与本机调用约束

- **设计用途**：`X-Service-Token`（配合 `X-User-Id`）是有意保留的内部自派发（如后台回报 `notify_turn`、秘书派发）与受控运维脚本通道。
- **不经过邮箱允许名单与 Supabase 503 就绪检查**：命中合法的 `INTERNAL_SERVICE_TOKEN`（长度 `>= 32`）时，请求按 `X-User-Id` 指定的身份执行（或在 `PUT /threads/{id}/external-id` 上执行无归属回填），**不经过 `DALTON_ALLOWED_EMAILS` 名单检查**。
- **公网部署强制要求**：
  1. `INTERNAL_SERVICE_TOKEN` 必须使用至少 32 字节（建议 64 字符 hex）的密码学随机高熵密钥，并定期轮换。
  2. **公网部署时服务令牌只能在本机或受控内网调用**：反向代理（Nginx / Caddy / Cloudflare / ALB）必须在公网入口剥离或拒绝外部客户端携带的 `X-Service-Token` 请求头，确保只有本机后端容器与本机运维脚本可使用该通道。

---

## 4. WebSocket `?token=` 网关日志脱敏

- 浏览器端行情 WebSocket（`/ws/v1/market-data/aggregates/{market}`，见 `src/server/app/market_data_ws.py:402`）在握手阶段通过查询参数 `?token=<access_token>` 传递 Supabase JWT。
- 公网部署时，**必须在反向代理 / 网关层对 `/ws/v1/market-data/aggregates/{market}`（建议覆盖整个 `/ws/` 握手路径）访问日志（access log）中的 `token` 查询参数做脱敏或关闭查询串记录**（例如在 Nginx `log_format` 中使用 `$uri` 替代 `$request_uri`，或用 `map` 将 `token=[^&]+` 替换为 `token=REDACTED`），防止 JWT 写入明文访问日志。

---

## 5. 团队共用凭据（`credentials.sharing: team | "off"`）

- **配置开关**：`src/dalton/dalton.yaml` 中的 `credentials.sharing: team | "off"`（例如关闭时写 `sharing: "off"`，解析器也兼容未加引号被 YAML 1.1 解析为布尔 `false` 的 `sharing: off`；默认 `team`）。仅在 **Dalton Supabase 登录开启**（`DALTON_AUTH_MODE=supabase`）且 **环境变量 `DALTON_PM_USER_IDS` 已配置且非空**时生效；未设置 `DALTON_PM_USER_IDS`、设为空字符串 `""` 或关闭 Supabase 登录时一律等同 `"off"`（保持上游按 `user_id` 隔离的多租户行为）。
- **单一凭据持有人**：启用 `team` 共享后，`src/dalton/credentials.py:credential_user_id(user_id)` 将所有团队成员的模型凭据读写透明解析到 **`DALTON_PM_USER_IDS` 的第一个用户 ID**（持有人）。任何成员连接模型 OAuth 或填写非空 BYOK API Key，均写入持有人名下并立即对全队生效；OAuth 活跃缓存、BYOK 缓存与 Redis OAuth 刷新锁（`oauth:refresh:{holder_id}:{provider}`）均按持有人 ID 计算。
- **共享范围与隔离边界**：
  - **共享（仅限模型凭据）**：`user_oauth_tokens`（模型 OAuth：Codex / Claude / Antigravity / Muse）、`user_api_keys`（BYOK 密钥、`base_url`）以及 `users.byok_enabled`（用户资料表中的 `has_api_key` / `has_oauth_token` 统一只看解析后的持有人）。
  - **仍按本人隔离**：
    1. **MCP 服务器、插件、Vault 密钥与出网授权（egress grants）**：在上游与交易授权级别（`trading_permissions`）、插件复合外键、egress grant 及各工作区 schema/配置版本号强绑定，共享会导致持有人交易权限越权扩散及跨用户目录/工作区状态冲突，因此保持按人隔离。
    2. **用户资料与审计**：用户资料、偏好（`user_preferences`）、工作区、线程、记忆、日预算、`dalton_provider_events` 日志与 `dalton_llm_journal.user_id` 均记实际调用者（共享开启时 `usage.credential_user_id` 记录所借用的持有人 ID）。
- **断开、清空与关闭权限**：所有白名单成员均可连接模型 OAuth、新增或更新为非空 BYOK API Key（可同时附带该 provider 的 `base_url`）、开启 `byok_enabled=true`；**仅 PM 用户（`DALTON_PM_USER_IDS`）可断开模型 OAuth、删除/置空 API Key（含传 `""` 或借 `base_url` 写空）、单独修改/清空已有密钥的 `base_url`（未同时提供非空新密钥）或将 `byok_enabled` 设为 `false`**，非 PM 成员调用返回 `403`（`"团队共用凭据只能由管理员断开"`），前端通过 React Query（`queryKeys.dalton.credentialsStatus`）按当前登录用户获取权限并对非 PM 成员隐藏断开按钮。

## 6. 邀请注册

管理员（`DALTON_PM_USER_IDS`）可在**设置 → 邀请成员**区块生成一次性邀请链接（`{DALTON_FRONTEND_URL}/invite#{token}`，7 天有效，可随时撤销；token 放在 URL 片段中，永不发送至服务器访问日志，前端加载后立即清除 URL 片段并通过 JSON body 调用接口）。受邀人打开链接设置**用户名 + 密码**后，后端通过 Supabase Admin API 创建账户并写入 `dalton_members` 表，自动完成首次登录。

### 环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SUPABASE_SECRET_KEY` | 无（必填以启用邀请） | Supabase 服务端管理员密钥（`service_role` / Secret Key），仅后端持有，用于调用 `POST {SUPABASE_URL}/auth/v1/admin/users` 创建已确认邮箱的账户。缺失时生成邀请与兑换接口返回 `503`（`"邀请注册未配置"`）。 |
| `DALTON_INVITE_EMAIL_DOMAIN` | `team.dalton.local` | 邀请注册账户合成邮箱域名，受邀人设置的 `username` 映射为 `{username}@{DALTON_INVITE_EMAIL_DOMAIN}`。 |
| `VITE_DALTON_INVITE_EMAIL_DOMAIN` | `team.dalton.local` | 前端登录页用户名补全域名（须与后端 `DALTON_INVITE_EMAIL_DOMAIN` 一致）。当登录框输入不含 `@` 时，前端自动补全为 `{username}@{VITE_DALTON_INVITE_EMAIL_DOMAIN}` 再调用 Supabase 密码登录。 |
| `DALTON_TRUSTED_PROXIES` | `127.0.0.1,::1` | 可信反向代理 IP 或 CIDR 列表（逗号分隔，可加入 edge 容器所在 Docker 网段）。公开邀请接口限流默认取直连 `request.client.host`，仅当直连对端属于 `DALTON_TRUSTED_PROXIES` 时才从右往左取 `X-Forwarded-For` 中第一个非可信地址。 |

### Supabase 侧设置要求

- **保持关闭公开注册**：Supabase 控制台必须继续保持 `disable_signup=true`（关闭 Allow new users to sign up）、`mailer_autoconfirm=false`（开启 Confirm email）以及关闭匿名登录（`external.anonymous_users=false`）。
- 邀请注册通过服务端 `SUPABASE_SECRET_KEY` 调用 Admin API（`email_confirm: true`）创建用户，无需开放公开注册，后端启动与鉴权时的 `/auth/v1/settings` fail-closed 安全检查保持不变。
- 团队成员判定为 `DALTON_ALLOWED_EMAILS ∪ dalton_members`，原有环境变量白名单中的邮箱无需迁移即可继续使用。

