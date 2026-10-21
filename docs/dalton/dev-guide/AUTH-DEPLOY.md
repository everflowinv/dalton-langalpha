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
