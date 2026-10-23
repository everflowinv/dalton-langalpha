# Everflow 公网入口部署指南 (`PUBLIC-DEPLOY.md`)

本文档说明如何通过 **Tailscale Funnel (`:8443`) → 本机 Caddy 网关 (`127.0.0.1:8088`)** 将 Everflow (Dalton) 安全地对团队开放，同时保持宿主机其他端口与已有 Tailscale 服务隔离。认证与允许名单配置前置要求见 [`AUTH-DEPLOY.md`](AUTH-DEPLOY.md)。

---

## 1. 架构图与端口矩阵

```
公网团队成员浏览器
  https://<主机名>.<tailnet>.ts.net:8443
        │
        │  TLS 终止于 Tailscale Funnel (:8443)
        ▼
  Tailscale Daemon (宿主机，保留 443 与 10000 原有服务不动)
        │
        │  HTTP 仅转发至本机回环 127.0.0.1:8088
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Docker Compose: edge (caddy:2.9.1-alpine, 宿主机 127.0.0.1:8088 -> 8088) │
│   - 访问日志：format filter 将 query 中 token 替换为 REDACTED            │
│   - 响应头：nosniff / strict-origin-when-cross-origin / SAMEORIGIN       │
│                                                                         │
│   ├─ /api/* , /ws/*                                                     │
│   │    • 剥离外来请求头 X-Service-Token 与 X-User-Id                     │
│   │    • flush_interval -1 (SSE 零缓冲 + 原生 WebSocket Upgrade)        │
│   │    └──► Docker 内网 backend:8000 (FastAPI)                          │
│   │                                                                     │
│   ├─ /version.json                                                      │
│   │    └──► 静态只读卷 /srv/version.json (no-store；不存在时真实 404)    │
│   │                                                                     │
│   ├─ /assets/*                                                          │
│   │    └──► 静态只读卷 /srv/assets/* (immutable 长缓存；不存在时 404)    │
│   │                                                                     │
│   ├─ 其余 GET/HEAD（排除 /health, /docs, /openapi.json 等后端根路径）   │
│   │    └──► 静态只读卷 /srv + SPA 回退 /index.html (index.html 不缓存)  │
│   │                                                                     │
│   └─ 其余所有路径 / 方法 ──► 404 Not Found                              │
└─────────────────────────────────────────────────────────────────────────┘
```

### 宿主机监听端口矩阵（全部绑定 `127.0.0.1`，局域网不可直连）

| 服务 | 宿主机绑定地址 | 容器端口 | 说明 |
|---|---|---|---|
| `edge` (Caddy) | `127.0.0.1:8088` | `8088` | Tailscale Funnel `8443` 唯一转发目标 |
| `backend` (FastAPI) | `127.0.0.1:8000` (`!override`) | `8000` | 覆盖 `docker-compose.yml` 的 `0.0.0.0:8000`，仅本机与容器网可达 |
| `frontend` (Vite dev) | `127.0.0.1:5173` (`!override`) | `5173` | 覆盖 `docker-compose.yml` 的 `0.0.0.0:5173`，仅本机开发调试用 |
| `postgres` | `127.0.0.1:5432` | `5432` | 数据库（仅本机可达） |
| `redis` | `127.0.0.1:6379` | `6379` | 缓存与流缓冲（仅本机可达） |

> **说明（为什么改绑 `127.0.0.1` 不会断工作区沙箱）**：工作区沙箱经 `host.docker.internal`（Colima 下解析为 `192.168.5.2`，即宿主回环转发地址）回连后端；经实测，宿主端口改绑 `127.0.0.1` 后容器内通过 `host.docker.internal:8000` 依然可达，沙箱 livefs 与 egress relay 不受影响。

---

## 2. 前置条件与版本要求

### 2.1 Docker Compose 版本要求
- `docker-compose.dalton.yml` 使用 YAML 自定义标签 `ports: !override` 替换（而非追加）`docker-compose.yml` 中 `backend` 和 `frontend` 的 `0.0.0.0` 端口映射。
- **最低版本要求**：**Docker Compose v2.24.4+**（本机已实测为 `Docker Compose version 5.6.0`）。
- 核验命令：
  ```bash
  docker compose version
  docker compose -f docker-compose.yml -f docker-compose.dalton.yml config
  ```
  在展开后的 `config` 输出中确认 `backend`、`frontend`、`edge` 的 `ports` 均只有一条规则且包含 `host_ip: 127.0.0.1`。

### 2.2 `.env` 必填变量
除 [`AUTH-DEPLOY.md`](AUTH-DEPLOY.md) §1 所列的前后端认证变量（`DALTON_AUTH_MODE=supabase`、`DALTON_ALLOWED_EMAILS`、`SUPABASE_URL`、`SUPABASE_PUBLISHABLE_KEY`、`INTERNAL_SERVICE_TOKEN`、`DALTON_PM_USER_IDS`、`VITE_DALTON_AUTH=supabase`、`VITE_SUPABASE_URL`、`VITE_SUPABASE_PUBLISHABLE_KEY`）外，建议在 `.env` 中设置：

```dotenv
PUBLIC_APP_URL=https://<主机名>.<tailnet>.ts.net:8443
```
确保后端生成的分享链接（`/a/<code>`、`/s/<code>`）指向公网入口而非 `http://localhost:5173`。

---

## 3. Supabase 控制台 URL 配置

在 Supabase Dashboard → **Authentication** → **URL Configuration** 中配置：

1. **Site URL**：
   ```text
   https://<主机名>.<tailnet>.ts.net:8443
   ```
2. **Redirect URLs**（添加以下允许跳转地址，供密码重置与邮箱确认跳转使用）：
   ```text
   https://<主机名>.<tailnet>.ts.net:8443/reset-password
   https://<主机名>.<tailnet>.ts.net:8443/auth/confirm
   https://<主机名>.<tailnet>.ts.net:8443/callback
   ```
   - `/reset-password`：密码重置邮件落地页（见 `web/src/contexts/AuthContext.tsx` 中 `passwordResetUrl()`）。
   - `/auth/confirm`：邮箱 OTP / 确认链接落地页（见 `emailConfirmUrl()`）。
   - `/callback`：PKCE / OAuth 通用回调页。

同时确认 [`AUTH-DEPLOY.md`](AUTH-DEPLOY.md) §2 的三项安全基线已开启（禁止公开注册 `disable_signup=true`、开启邮箱确认 `mailer_autoconfirm=false`、关闭匿名登录）。

---

## 4. 构建与启动步骤

### 步骤 1：构建前端生产产物 (`web/dist`)
在仓库根目录执行生产构建脚本（自动读取 `.env` 中的 `VITE_*` 与 `web/.env.dalton` 品牌默认项，输出到 `web/dist` 并校验 `index.html` 与 `version.json`）：

```bash
bash scripts/dalton/build_frontend_prod.sh --require-supabase
```

### 步骤 2：启动 / 更新容器服务
```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d backend edge
```
（如需在宿主机同时保留 `127.0.0.1:5173` 开发前端，可执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d`。）

> **注意**：若此前 `backend` 或 `frontend` 以旧配置运行在 `0.0.0.0:8000` / `0.0.0.0:5173`，执行 `up -d --force-recreate backend frontend edge` 以确保宿主端口绑定立即切换为 `127.0.0.1`。

### 步骤 3：执行本机网关自检
在开启 Tailscale Funnel 前，先对 `127.0.0.1:8088` 执行自检脚本：

```bash
bash scripts/dalton/check_public_edge.sh
```
确认输出 `ALL PUBLIC EDGE CHECKS PASSED.`。

---

## 5. Tailscale Serve / Funnel 命令（由 lead 执行）

> **重要警告**：本机 Tailscale 已在 `443`（`/`、`/d/`、`/dalton/`）和 `10000` 端口开放其他服务。**严禁执行不带端口参数的 `tailscale serve reset` 或 `tailscale funnel reset`**，所有命令必须显式指定 `--https=8443`。

```bash
# 1. 先查看现有 serve / funnel 配置，确认 443 与 10000 状态
tailscale serve status
tailscale funnel status

# 2. 将本机 Caddy 网关 (127.0.0.1:8088) 挂载到 Tailscale HTTPS 8443 端口
tailscale serve --bg --https=8443 http://127.0.0.1:8088

# 3. 开启 8443 端口的公网 Funnel
tailscale funnel --bg --https=8443 http://127.0.0.1:8088
# 注：若本机 Tailscale CLI 为旧版子命令语法，则第 2、3 步对应为：
#   tailscale serve --https=8443 --bg http://127.0.0.1:8088
#   tailscale funnel --https=8443 on

# 4. 核验 8443 已开启 Funnel，且 443 / 10000 原有路由未受影响
tailscale funnel status
```

---

## 6. 安全设计与取舍说明

### 6.1 服务令牌头剥离（`X-Service-Token` / `X-User-Id`）与自检局限说明
- **网关行为**：`deploy/dalton/Caddyfile` 在 `/api/*` 与 `/ws/*` 路由块中同时通过 `request_header -X-Service-Token` / `-X-User-Id` 和 `reverse_proxy` 的 `header_up -X-Service-Token` / `-X-User-Id` 无条件删除外来请求携带的服务令牌头，确保 `INTERNAL_SERVICE_TOKEN` 通道只能从容器内网或本机 `127.0.0.1:8000` 调用。
- **自检方式与局限**：
  - 后端没有回显原始请求头的公开接口。
  - 在 `src/server/utils/api.py` 中，当后端已配置合法的 `INTERNAL_SERVICE_TOKEN`（长度 `>= 32`）时：
    - 若外来伪造的 `X-Service-Token` 未被网关剥离而到达后端，`_service_token_user_id()`（`src/server/utils/api.py:70-71`）会直接抛出 `401 {"detail": "Invalid service token"}`；若携带真令牌匹配成功但缺 `X-User-Id`，则由调用它的 `get_current_user_id()`（`src/server/utils/api.py:88-91`）抛出 `401 {"detail": "Missing X-User-Id"}`。
    - 若网关已剥离 `X-Service-Token`，`_service_token_user_id()` 看到请求无该头而跳过服务认证分支，交由 `verify_dalton_bearer()` 返回 `401 {"detail": "Missing authentication"}`。
  - **局限**：`scripts/dalton/check_public_edge.sh` 通过断言返回状态码为 `401` 且 `detail == "Missing authentication"` 来验证剥离。如果后端未配置 `INTERNAL_SERVICE_TOKEN`（或长度不足 32），即使请求头未被剥离，后端也会跳过服务令牌分支并返回 `401 {"detail": "Missing authentication"}`。因此上线验收时应确保后端 `INTERNAL_SERVICE_TOKEN` 已按要求配置，或用真实 `INTERNAL_SERVICE_TOKEN` + `X-User-Id` 经 `:8088` 请求确认仍返回 `401`。

### 6.2 访问日志 `?token=` 脱敏方案选择
- **选择方案**：采用 Caddy 的 `log` + `format filter`（`request>uri query { replace token REDACTED }`），输出 JSON 结构化日志。
- **取舍原因**：相比直接删除整个查询字符串，按参数名过滤 `token` 既能确保 `/ws/v1/market-data/aggregates/{market}?token=<jwt>` 握手中的 Supabase JWT 被替换为 `token=REDACTED`（绝不以明文进日志），又能保留 `interval=second`、`tier=realtime`、`format=cmdp` 以及 REST 接口的分页筛选参数，兼顾安全性与线上排障可观测性。

### 6.3 站点级 `encode` 与 SSE 流式响应实测结论
- **保留站点级 `encode zstd gzip`**：Caddy 2.9.1 的 `encode` 默认不压缩 `text/event-stream`。经实测（每秒推送一条事件的 SSE 服务通过本 `Caddyfile` 代理，客户端带 `Accept-Encoding: gzip, zstd` 请求），响应不含 `Content-Encoding`，事件按 0s/1s/2s/3s 逐条实时到达，配合 `flush_interval -1` 不会造成 SSE 缓冲延迟。

### 6.4 安全响应头与后续演进项（CSP）
- **已启用响应头**：
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `X-Frame-Options: SAMEORIGIN`
  - `-Server`（隐藏 `Server: Caddy` 标识）
- **不启用 `Strict-Transport-Security` (HSTS)**：TLS 在 Tailscale Funnel 层终止，Caddy 仅在宿主机 `127.0.0.1:8088` 提供明文 HTTP。
- **后续演进项：`Content-Security-Policy` (CSP)**：
  - 本次上线暂不加 CSP，避免阻断前端正常运行。
  - 原因：`web/index.html` 包含内联的 stale-build 恢复 IIFE 脚本、locale 预加载脚本及窗口拖拽样式，且前端包含 Monaco Editor、PDF.js Web Worker / WebAssembly (`wasm`) 与运行时样式注入。后续需盘点完整的内联脚本 SHA-256 hash、`worker-src 'self' blob:`、`script-src 'self' 'wasm-unsafe-eval'` 及 `connect-src 'self' https://*.supabase.co` 后，先以 `Content-Security-Policy-Report-Only` 观察无误再正式开启。

---

## 7. 回滚方法

1. **紧急切断公网流量（秒级生效，不影响本机 443 / 10000 其他服务）**：
   ```bash
   tailscale funnel --https=8443 off
   tailscale serve --https=8443 off
   ```
2. **停止 Caddy 网关容器**：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dalton.yml stop edge
   ```
3. **回退代码与配置**：
   切换回 `dalton/main` 分支并执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d`。

---

## 8. 上线检查清单（Launch Checklist）

- [ ] **前端构建产物体检**：`web/dist/index.html` 与 `web/dist/version.json` 存在，且构建时 `VITE_DALTON_AUTH=supabase`。
- [ ] **无令牌 401**：`curl -i http://127.0.0.1:8088/api/v1/users/me` 返回 `HTTP/1.1 401 Unauthorized`（`{"detail":"Missing authentication"}`）。
- [ ] **服务令牌头被剥**：
  ```bash
  curl -i -H "X-Service-Token: forged-token-0123456789abcdef0123456789abcdef" \
          -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
          http://127.0.0.1:8088/api/v1/users/me
  ```
  返回 `401` 且响应体为 `{"detail":"Missing authentication"}`（若带真实 `INTERNAL_SERVICE_TOKEN` 经 `8088` 访问同样返回 `401`）。
- [ ] **`/version.json` 行为正确**：存在时返回 `200`（`application/json`，`Cache-Control: no-cache, no-store, must-revalidate`），缺失时返回 `404`（不回退到 `index.html`）。
- [ ] **`?token=` 不进访问日志**：执行 `bash scripts/dalton/check_public_edge.sh` 后，运行：
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.dalton.yml logs --tail=50 edge
  ```
  确认 `/ws/v1/market-data/aggregates/stock` 请求日志中查询参数显示为 `token=REDACTED`，无明文 token。
- [ ] **局域网无法直连 8000 / 5173 / 8088**：
  - 在本机执行：
    ```bash
    lsof -nP -iTCP:8000 -iTCP:5173 -iTCP:8088 -sTCP:LISTEN
    ```
    确认监听地址全部为 `127.0.0.1:8000`、`127.0.0.1:5173`、`127.0.0.1:8088`（无 `*:8000` 或 `*:5173`）。
  - 从同一局域网的另一台设备访问 `http://<本机局域网IP>:8000` 与 `http://<本机局域网IP>:5173`，确认连接被拒绝。
- [ ] **公网回归验证**：通过 `https://<主机名>.<tailnet>.ts.net:8443` 访问登录页、白名单账号登录、非白名单账号 403 拦截、密码重置邮件跳转 `/reset-password` 正常。
