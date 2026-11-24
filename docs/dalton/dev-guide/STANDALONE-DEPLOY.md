# Dalton 独立核心版部署手册

本手册面向在独立 Linux（类 Unix 开源操作系统）服务器上部署与运维 **Dalton 独立核心版（Standalone Core，自包含核心安装形态）** 的管理员，覆盖系统准备、首次安装、HTTPS（超文本传输安全协议）配置、本地账号、模型密钥以及备份、恢复与平滑升级流程。

---

## 1. 系统要求

### 1.1 软硬件基线

| 项目 | 最低 / 推荐要求 | 说明 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04 LTS 或 Debian 12（64 位 Linux） | 使用专用于运行 Dalton 的独立云主机或虚拟机 |
| Docker Engine（容器运行时引擎） | **≥ 28.0** | 保障宿主机 `127.0.0.1` 回环端口发布与同网段主机隔离 |
| Docker Compose（多容器编排工具） | **≥ v2.24.4**（插件版 `docker compose`） | 支持 `!reset` / `!override` 编排覆盖语法 |
| CPU（中央处理器）与内存 | 4 核 CPU / **≥ 8 GiB 内存**（推荐 16 GiB） | 同时运行后端、PostgreSQL（开源关系型数据库）、Valkey（BSD-3-Clause 兼容 Redis 协议的内存键值缓存，服务名保留为 `redis`）、MinIO（兼容 S3 协议的开源对象存储服务）与代码沙箱容器 |
| 可用磁盘 | **≥ 30 GiB**（推荐 ≥ 80 GiB SSD） | 存放镜像、沙箱层、上传文件与本地滚动备份 |
| 基础命令行工具 | `bash`、`openssl`、`curl`、`tar`、`rsync`、`python3`、`make`、`sha256sum` | 供初始化、配置向导、备份、恢复与升级/回滚脚本调用 |

验证主机容器环境版本：

```bash
docker --version
docker compose version
```

### 1.2 网络与端口要求

| 部署模式 | 需放行的入站 TCP（传输控制协议）端口 | Compose 编排文件 |
|---|---|---|
| 本机回环 HTTP 试用（`DALTON_TLS_MODE=loopback`，默认） | 仅本机 `127.0.0.1:8088` | `docker-compose.yml` + `docker-compose.core.yml` |
| 本机回环 HTTPS（`DALTON_TLS_MODE=internal` / `custom`） | 仅本机 `127.0.0.1:18080` 与 `127.0.0.1:18443`（IPv6 回环 `::1` 为 `[::1]:18080` / `[::1]:18443`） | 额外附加 `deploy/standalone/docker-compose.tls.yml`（IPv6 附加 `deploy/standalone/docker-compose.tls.ipv6.yml`） |
| 公网自动 HTTPS（`DALTON_TLS_MODE=auto`） | **80**（ACME 验证与跳转）与 **443**（HTTPS），监听 `0.0.0.0`（IPv6 为 `[::]`） | 额外附加 `deploy/standalone/docker-compose.tls.yml`（IPv6 附加 `deploy/standalone/docker-compose.tls.ipv6.yml`） |
| 企业内网 HTTPS（`DALTON_TLS_MODE=internal` / `custom`） | **80** 与 **443**（内网可达），监听 `0.0.0.0`（IPv6 为 `[::]`） | 额外附加 `deploy/standalone/docker-compose.tls.yml`（IPv6 附加 `deploy/standalone/docker-compose.tls.ipv6.yml`） |

> **对外发布范围说明**：`scripts/standalone/init.sh` 按 `--domain` 自动写入认证模式与监听地址（`DALTON_EDGE_TLS_BIND`）。回环域名（`localhost`、`127.0.0.1`、`::1`）固定监听本机回环地址（`127.0.0.1` 或 `[::1]`），用于单机试用。公网域名、企业内网域名和局域网 IP 在执行 `init.sh` 时会自动启用本地多用户登录（写入 `DALTON_AUTH_MODE=local` 与 `VITE_DALTON_AUTH=local`），并将 `DALTON_EDGE_TLS_BIND` 设为 `0.0.0.0`（IPv6 设为 `[::]`）以通过 `deploy/standalone/docker-compose.tls.yml`（IPv6 使用 `docker-compose.tls.ipv6.yml`）监听所有地址；部署前请在宿主机防火墙和云安全组放行入站 TCP 80 与 443 端口，并带 `VITE_DALTON_AUTH=local` 构建 `edge` 镜像。后端启动时会同步校验：当 `DALTON_EDGE_TLS_BIND` 为非回环地址时，认证模式必须为 `local`。

> **基础镜像版本固定说明**：独立版直接拉取的运行时与边缘网关镜像（`valkey/valkey:8-alpine`、`cgr.dev/chainguard/minio` 以及 `deploy/standalone/Dockerfile.edge` 中的 `node:24-alpine` 与 `caddy:2.9.1-alpine`）均已按不可变 SHA-256 摘要固定版本。PostgreSQL（`scripts/dalton/Dockerfile.postgres` 的 `postgres:18`）、后端（`scripts/dalton/Dockerfile.dalton` 的 `node:24-alpine`、`python:3.13-slim`、`ghcr.io/astral-sh/uv:latest`）与沙箱（`Dockerfile.sandbox` 的 `ubuntu:24.04`）的基础镜像与上游及多架构构建共享，且构建阶段通过 `apt-get` 拉取滚动安全更新，因此仍使用可变主版本标签（详见根目录 `NOTICE`）。如需在严格离线或冻结基线环境中部署，请提前构建并通过私有镜像仓库同步固定摘要后的产物镜像，升级时附加 `--skip-build`。

后端服务（`backend`）、数据库（`postgres`）、缓存（`redis`，运行 Valkey 8）和对象存储（`minio`）均运行在固定子网 `core_internal`（默认 `172.31.250.0/24`，可通过 `DALTON_CORE_INTERNAL_SUBNET` 覆盖）中，所有外部请求统一经边缘网关 `edge`（Caddy）进入。`docker-compose.core.yml` 直接由同一个 `DALTON_CORE_INTERNAL_SUBNET` 变量派生后端的 `DALTON_TRUSTED_PROXIES`（`init.sh` 只写入 `DALTON_CORE_INTERNAL_SUBNET` 和 `DALTON_TRUSTED_PROXY_HOPS=1`），因此调整子网后网络与可信代理始终保持一致，后端按 `X-Forwarded-For` 记录真实客户端 IP 并执行分 IP 限速。代码沙箱容器运行在独立隔离网络 `sandbox-net` 中，仅与后端通信。

### 1.3 Docker 套接字权限边界

核心版采用 DooD（Docker-outside-of-Docker，容器挂载宿主机套接字管理兄弟容器的方式），将 `/var/run/docker.sock` 挂载进 `backend` 容器以按需启停代码分析沙箱。持有该套接字等同于拥有宿主机 Docker 守护进程控制权，因此请在**专用于运行 Dalton 的独立主机或虚拟机**上部署，并将部署账号加入 `docker` 用户组：

```bash
sudo usermod -aG docker "$USER"
# 重新登录 SSH 会话使组权限生效
```

---

## 2. 首次安装

### 第 1 步：解压发行包

将获取到的发行压缩包上传到服务器并解压至部署目录（以下以 `/opt/dalton` 为例）：

```bash
mkdir -p /opt/dalton
tar -xzf dalton-core-<version>.tar.gz -C /opt/dalton
cd /opt/dalton
```

### 第 2 步：运行初始化脚本

执行 `scripts/standalone/init.sh`，脚本会自动检测系统环境、按目标域名写入认证模式与监听地址、配置 `COMPOSE_FILE` 与可信代理参数、生成全套随机密钥（写入权限为 `0600` 的 `.env` 文件），并构建代码沙箱镜像 `langalpha-sandbox:latest`：

```bash
# 公网域名部署（启用本地登录、自动 HTTPS 与 0.0.0.0 监听）
bash scripts/standalone/init.sh --domain dalton.example.com

# 企业内网部署（启用本地登录、内部 CA HTTPS 与 0.0.0.0 监听）
bash scripts/standalone/init.sh --domain dalton.internal

# 本机回环 HTTP 试用（仅发布 127.0.0.1:8088）
bash scripts/standalone/init.sh --domain localhost

# 本机回环内部 CA HTTPS（仅发布 127.0.0.1:18080 与 127.0.0.1:18443）
bash scripts/standalone/init.sh --domain localhost --tls-mode internal
```

首次构建代码沙箱镜像通常需要 20–40 分钟。若希望先完成配置再另行构建沙箱镜像，可附加 `--skip-sandbox-build`，随后手动执行：

```bash
docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .
```

### 第 3 步：配置模型密钥与可选数据源

运行交互式配置向导写入大模型 API Key（应用编程接口密钥）及可选数据源：

```bash
make config
```

也可先启动服务，再在浏览器首次引导页中配置模型密钥（详见第 5 节）。

### 第 4 步：启动核心服务栈并核验状态

为避免被当前 shell 中导出的 `COMPOSE_PROJECT_NAME` / `COMPOSE_FILE` 等变量劫持到其他部署实例，本手册所有 `docker compose` 命令均显式指定 `-p <项目名> -f <文件>... --env-file .env`，其中 `<项目名>` 从当前目录 `.env` 的 `COMPOSE_PROJECT_NAME` 读取（默认由 `init.sh` 写入 `dalton-core`，不读 shell 环境变量）：

```bash
cd /opt/dalton
PROJECT_NAME="$(python3 -c 'import re; m=[re.match(r"^\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*[=:]\s*[\"'"'"']?([^\"'"'"'#\s]+)", l) for l in open(".env")]; vals=[x.group(1) for x in m if x]; print(vals[-1] if vals else "")')"
test -n "$PROJECT_NAME"

# 回环 HTTP 模式
docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env up -d

# 启用 HTTPS 模式（IPv6 回环替换为 deploy/standalone/docker-compose.tls.ipv6.yml）
docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env up -d
```

启动流程按顺序完成：
1. 拉起 `postgres`（含 `pgvector` 向量扩展）、`redis`（Valkey 8）与 `minio`；
2. 运行一次性容器 `minio-init` 建立私有存储桶 `chat-uploads`；
3. 运行一次性容器 `migrate` 通过 PostgreSQL 咨询锁串行执行上游与 Dalton 两条 Alembic（数据库表结构迁移工具）迁移链至最新版本；
4. 启动 `backend`、`crawler` 与 `edge`。

查看所有服务状态与后端健康检查（启用 HTTPS 时一并加上 `-f deploy/standalone/docker-compose.tls.yml`）：

```bash
docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env ps
docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T backend curl -sf http://127.0.0.1:8000/health
```

---

## 3. HTTPS 配置

边缘网关 `edge` 基于 `deploy/standalone/Caddyfile` 与 `deploy/standalone/modes/*.caddy`，通过 `.env` 中的 `DALTON_TLS_MODE` 切换四种运行模式。四种模式共享同一套路由与安全边界（剥离外部 `X-Service-Token` / `X-User-Id` 请求头、屏蔽内部中继接口与 `/health`/`/docs`/`/metrics`、访问日志过滤 `?token` 与响应 `Location` 头）。证书与私钥持久化保存在 Compose 逻辑卷 `dalton-caddy-data` 中；宿主机上实际卷名带有实例前缀，执行以下命令查找：

```bash
docker volume ls --filter name=dalton-caddy-data
```

### 3.1 本机回环 HTTP 试用模式（`loopback`，当前默认）

```bash
bash scripts/standalone/init.sh --domain localhost
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env up -d
```

此模式仅发布 `127.0.0.1:8088`（通过 `http://127.0.0.1:8088` 访问，或从本地电脑通过 `ssh -L 8088:127.0.0.1:8088 user@server` 转发访问），宿主机 80/443 端口保持空闲。

### 3.2 内部 CA HTTPS 模式（`internal`）

适用于企业内网域名（如 `dalton.internal`）、局域网 IP 与本机 HTTPS 验证。传入企业内网域名或局域网 IP 时，`init.sh` 自动启用本地登录（写入 `DALTON_AUTH_MODE=local`、`VITE_DALTON_AUTH=local`），将 `DALTON_EDGE_TLS_BIND` 设为 `0.0.0.0`（IPv6 设为 `[::]`），并监听 80/443 端口：

```bash
# 企业内网域名或局域网 IP（自动启用本地登录，监听 0.0.0.0:80 与 0.0.0.0:443；IPv6 换用 docker-compose.tls.ipv6.yml）
bash scripts/standalone/init.sh --domain dalton.internal
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env up -d --build edge backend

# 本机回环 HTTPS（仅绑定 127.0.0.1:18080 与 127.0.0.1:18443，HTTP 跳转自动携带 18443 端口并启用 HSTS）
bash scripts/standalone/init.sh --domain localhost --tls-mode internal
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env up -d
```

启动 `edge` 后，导出 Caddy 内部 CA（证书颁发机构）根证书并导入客户端或操作系统的受信任根证书库：

```bash
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env exec -T edge \
  cat /data/caddy/pki/authorities/local/root.crt > dalton-local-ca.crt
```

### 3.3 企业自有证书模式（`custom`）

将完整证书链 `cert.pem` 与私钥 `key.pem`（权限 `0600`）放入指定目录（默认为 `./deploy/standalone/certs`）后执行（传入企业域名时，`init.sh` 自动启用本地登录并将 `DALTON_EDGE_TLS_BIND` 设为 `0.0.0.0`）：

```bash
bash scripts/standalone/init.sh \
  --domain dalton.corp.internal \
  --tls-mode custom \
  --tls-cert-dir /etc/dalton/certs
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env up -d --build edge backend
```

> **自有证书备份与健康探测**：`backup.sh` 仅归档 Caddy 的 `/data` 卷，请单独做好 `DALTON_TLS_CERT_DIR`（含 `cert.pem` 与 `key.pem`）的备份与恢复。若证书由企业内部 CA 签发，在执行 `restore.sh` 和 `upgrade.sh` 时传入 `--ca-file <企业CA根证书.pem>` 完成 HTTPS 校验。

### 3.4 公网域名自动 HTTPS（`auto`）

公网域名部署可直接启用自动 HTTPS：`init.sh` 自动写入 `DALTON_AUTH_MODE=local`、`VITE_DALTON_AUTH=local`、`DALTON_TLS_MODE=auto`，并将 `DALTON_EDGE_TLS_BIND` 设为 `0.0.0.0`（监听 80 与 443）：

1. 将目标域名（如 `dalton.example.com`）的 `A` / `AAAA` 记录指向服务器公网 IP。
2. 在云安全组与系统防火墙放行入站 **TCP 80** 与 **TCP 443** 端口，并确认宿主机 80/443 端口空闲。
3. 运行 `bash scripts/standalone/init.sh --domain dalton.example.com`。
4. 使用含 `deploy/standalone/docker-compose.tls.yml` 的编排命令构建并启动服务，Caddy 通过 ACME 自动签发并续期证书：
   ```bash
   docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env up -d --build edge backend
   ```

---

## 4. 首位管理员与账号管理

正式面向多人或外部网络部署时，需在**首次对外启动前**启用本地多用户账号体系（基于 PostgreSQL 存储密码哈希、签发短期 JWT〔JSON 网络签名令牌〕与可轮换刷新会话）：

### 4.1 启用本地多用户账号模式

在 `.env` 中配置以下参数，并确保首次构建 `edge` 时带入 `VITE_DALTON_AUTH=local`：

```dotenv
DALTON_AUTH_MODE=local
VITE_DALTON_AUTH=local
```

修改 `VITE_DALTON_AUTH` 后重新构建并启动 `edge` 与 `backend`（启用 HTTPS 时加上 `-f deploy/standalone/docker-compose.tls.yml`）：

```bash
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env up -d --build edge backend
```

### 4.2 创建首位管理员

1. 在服务器上从权限为 `0600` 的 `.env` 读取一次性自举口令（自后端首次启动写入起 24 小时内有效）：
   ```bash
   grep '^BOOTSTRAP_SETUP_TOKEN=' .env
   ```
2. 浏览器访问 `https://<您的域名>/setup/admin`。
3. 填入自举口令、管理员邮箱、用户名与登录密码（长度至少 10 位）并提交。
4. 创建成功后，系统在同一数据库事务中写入管理员账号、角色与团队凭据持有人记录，并关闭自举入口（后续访问返回 `409 Conflict`）。

### 4.3 管理员运维命令行工具

当自举口令过期、连续输错 5 次触发锁定，或管理员忘记密码时，直接在服务器上通过容器内的运维 CLI（命令行界面）处理（`<项目名>` 从 `.env` 读取，启用 HTTPS 时加上 `-f deploy/standalone/docker-compose.tls.yml`）：

```bash
# 重新签发 24 小时有效的自举口令（仅在尚未完成自举前有效）
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T backend \
  uv run --no-sync python -m src.dalton.auth.cli reissue-bootstrap-token

# 重置指定账号密码并立即撤销该账号全部既有登录会话
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec backend \
  uv run --no-sync python -m src.dalton.auth.cli reset-password --email admin@example.com

# 为已有用户授予管理员角色
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T backend \
  uv run --no-sync python -m src.dalton.auth.cli grant-admin --email member@example.com
```

### 4.4 邀请团队成员

首位管理员登录后，进入网页 **设置 → 团队管理** 生成带有成员等级（内部成员 `internal` / 外部成员 `external`）的一次性邀请链接（默认 7 天有效）。受邀成员打开链接设置密码即可完成注册。

---

## 5. 模型密钥与可选数据源配置

Dalton 核心版采用 BYOK（Bring Your Own Key，自带模型密钥）模式，至少配置 1 个支持工具调用的大模型凭据即可开始研究对话。

### 5.1 配置方式

- **方式 A（网页向导，推荐）**：首次进入应用后在 `/setup` 引导页或 **设置 → 模型与供应商** 中填入主流模型提供商的 API Key（OpenAI、Anthropic、Google Gemini、DeepSeek 等），或添加自定义 OpenAI 兼容模型地址。密钥在数据库中经 `BYOK_ENCRYPTION_KEY` 加密保存。
- **方式 B（命令行向导）**：在服务器上运行 `make config`，按提示填写环境变量与默认模型，然后重启后端生效：
  ```bash
  make config
  docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env restart backend
  ```

### 5.2 企业内网模型网关白名单

为防范 SSRF（服务端请求伪造），多用户本地认证模式下后端默认拦截指向私网或回环地址的自定义模型地址。如需接入企业内网自建模型网关，由运维管理员在 `.env` 中显式登记白名单地址（多个地址以英文逗号分隔）并重启 `backend`：

```dotenv
DALTON_OPERATOR_LLM_ENDPOINTS=https://llm-gateway.corp.internal/v1
```

### 5.3 合规标识与可选增强密钥

在 `.env` 中按需补充以下配置并重启 `backend`：

| 环境变量 | 用途 | 默认行为 |
|---|---|---|
| `SEC_USER_AGENT` | 访问公开 SEC EDGAR（美国证券交易委员会电子数据收集分析检索系统）接口的合规身份，格式如 `Dalton ops@example.com` | 建议所有部署填写 |
| `FMP_API_KEY` | 金融数据提供商 FMP（Financial Modeling Prep）密钥 | 留空时默认使用免密钥 Yahoo Finance 行情与 `dalton_sec` 美股财报工具 |
| `TAVILY_API_KEY` / `SERPER_API_KEY` / `EXA_API_KEY` | 外网实时搜索密钥 | 填写任意一项后自动启用 `WebSearch`；留空时使用内置免密钥网页抓取器 |
| `BRAND_NAME` / `VITE_BRAND_NAME` | 自定义站点与智能体品牌名 | 默认 `Dalton` |
| `DALTON_TIMEZONE` | 部署默认时区与预算日结时区（如 `Asia/Shanghai`） | 默认 `UTC` |

---

## 6. 备份、恢复与平滑升级

> **适用范围（外部数据库说明）**：`backup.sh`、`restore.sh` 与 `upgrade.sh` 仅管理 Compose 编排的 `postgres` 容器（`.env` 中 `DB_HOST=postgres`）。如果部署接入了外部 PostgreSQL 实例，请直接使用 `pg_dump` / `pg_restore` 自行备份与恢复数据库。

### 6.1 正式备份（在线执行）

`scripts/standalone/backup.sh` 在服务在线运行时完成以下四项归档，并执行 `pg_restore --list` 校验、MinIO 流式分块打包与 `SHA256SUMS`（安全散列算法 256 位校验清单）生成：

1. PostgreSQL 逻辑备份 `postgres.dump`（单一事务快照，同时包含上游与 Dalton 两条迁移链）；
2. MinIO 对象归档 `minio-objects.tar.gz`（按块流式读写并增量计算逐文件 SHA-256，保留 `ContentType`，同时在清单中记录有效桶名与键前缀）：v1 仅支持**单一对象存储服务**——脚本按应用实际生效的配置加载逻辑（结合 `agent_config.yaml` 默认值与 `.env`）分别解析聊天上传、ASR 与通用存储，只要指向不同的存储服务（例如聊天上传走 MinIO，而通用存储通过 `S3_BUCKET_NAME` 指向默认 AWS S3 或 OSS），`backup.sh` 与 `restore.sh` 都会直接报错退出；
3. Caddy 证书与内部 CA 数据归档 `caddy-data.tar.gz`；
4. `.env` 密钥文件：传入 `--env-passphrase-file` 时使用 OpenSSL AES-256-CBC + PBKDF2（参数写入 `manifest.json`，整体完整性由 `SHA256SUMS` 校验）加密生成 `env.enc`。

> **一致性与私钥保管边界**：
> - **在线一致性窗口与备份期对象核对**：数据库 `postgres.dump` 是单一时刻的一致快照，MinIO 对象枚举紧随其后执行。枚举完对象后，`backup.sh` 会立即核对数据库快照（`postgres.dump`）里所有**已完成**（非 `uploading`）的聊天上传记录，确认每一条的 `object_key` 都在本次对象归档里；只要有缺失，`backup.sh` 会直接失败、列出缺失的键并提示“可能是备份期间有清理任务运行，请重新备份”（不会发布不完整的备份目录）。对于备份时尚未完成的分片上传（状态为 `uploading` 的记录，本来就还没有最终对象），`backup.sh` 与 `restore.sh` 核对引用时都会跳过，`restore.sh` 会在输出中列出跳过了多少条；其他已完成记录在恢复后若仍有对象缺失，`restore.sh` 会直接失败退出并提示在线备份的时间窗口。
> - **私钥保护**：`caddy-data.tar.gz` 包含 Caddy 的 TLS 私钥与内部 CA 私钥，同步至异地备份存储时请对整个备份目录采取加密与最小权限控制。使用 `custom` 模式时，请同时单独备份 `DALTON_TLS_CERT_DIR`。

#### 准备执行账号、口令文件与备份目录（统一部署身份）

所有备份命令与定时任务均使用同一个部署账号（以下以当前已加入 `docker` 组的 `$USER` 为例）执行。先由 `sudo` 创建目录并赋权给该部署账号：

```bash
sudo mkdir -p /etc/dalton /var/backups/dalton /var/log/dalton
sudo chown "$USER:$USER" /etc/dalton /var/backups/dalton /var/log/dalton
chmod 700 /etc/dalton /var/backups/dalton
chmod 750 /var/log/dalton

# 以部署账号生成加密口令文件（权限 0600）
openssl rand -base64 32 > /etc/dalton/backup.pass
chmod 600 /etc/dalton/backup.pass
# 将 /etc/dalton/backup.pass 的副本单独存入公司密码管理器
```

#### 执行一次完整备份

```bash
cd /opt/dalton
bash scripts/standalone/backup.sh \
  --output-dir /var/backups/dalton \
  --retain 14 \
  --env-passphrase-file /etc/dalton/backup.pass
```

常用参数：
- `--output-dir <目录>`：备份父目录，每次发布加锁生成 `dalton-backup-YYYYMMDD-HHMMSSZ-<随机后缀>` 子目录（默认 `<部署目录>/backups`）；
- `--retain <N>`：成功校验后保留当前实例最近 N 份有效备份（默认 `7`，`0` 表示全部保留，上限 `3650`）；
- `--env-passphrase-file <文件>`：对 `.env` 加密生成 `env.enc`（也可通过环境变量 `DALTON_BACKUP_PASSPHRASE_FILE` 指定）；
- `--project-name <项目名>`：显式指定 Docker Compose 项目名（优先级高于 `.env` 中的 `COMPOSE_PROJECT_NAME` 与部署目录名）。

#### 配置每日定时自动备份（cron，使用同一部署账号）

以同一部署账号编辑定时任务：

```bash
crontab -e
```

加入如下任务（每天凌晨 02:30 执行，并配合异地同步任务将 `/var/backups/dalton` 加密转存至异地存储）：

```cron
30 2 * * * cd /opt/dalton && /usr/bin/env bash scripts/standalone/backup.sh --output-dir /var/backups/dalton --retain 14 --env-passphrase-file /etc/dalton/backup.pass >> /var/log/dalton/backup.log 2>&1
```

### 6.2 正式恢复（仅支持空目标实例）

v1 的 `scripts/standalone/restore.sh` **只恢复到空的目标实例**（不提供原地替换已有数据的路径）：
1. 校验备份目录内的 `SHA256SUMS`、`manifest.json` 与对象归档清单；
2. 准备目标 `.env`（**只读文件内容，不读 shell 环境变量**；解密 `env.enc` 产生的临时文件统一使用 `.env.tmp-*` 前缀，便于下次运行自动清理；传入 `--replace-env` 时对 `COMPOSE_PROJECT_NAME` 做**逐字比较**，不做规范化）：在读取或检查 `.env` 之前先按部署目录真实路径取得目录锁 `/tmp/dalton-restore-dir-<hash>.lock`，随后再取得项目锁 `/tmp/dalton-<项目名>.restore.lock`（固定顺序：先目录锁、后项目锁；优先使用 `flock`，无 `flock` 时退化为 `mkdir` 原子锁；不会按 PID 自动清理遗留锁，如遇残留锁需由管理员确认无并发恢复后手动删除），清理上次异常中断残留的 `.env.tmp-*`，校验外部库拦截、数据库标识符与 Compose 文件。从 `env.enc` 恢复时，在任何依赖 Compose 配置的预检开始之前，脚本会先将原 `.env`（若存在）备份成 `.env.pre-restore-<时间戳>`（`.env` 是普通文件时备份权限为 `600`；`.env` 是符号链接时，备份保留链接本身，权限由链接指向的文件决定），再用 `mv` 原子放入解密后的 `.env`（权限 `600`），后续所有预检、存储配置比对、写入与启动均使用与日常部署完全一致的 `docker compose` 参数（不生成临时 compose 覆盖文件）。随后在写入任何数据之前通过 `docker compose -p <项目名> -f <文件>... --env-file <.env> config --format json` 取出 `backend` 服务**实际生效**的环境变量（`.env` 中的值只作为辅助参考，最终以 `compose config` 的结果为准），校验单一对象存储约束（`--skip-minio` 仅在 `backend` 实际未配置任何对象存储时允许使用）以及备份清单与 `backend` 实际配置在聊天上传、ASR、通用存储的**桶名、端点和键前缀一致性**（不一致即在写入任何数据前停止，并由 `trap` 自动将 `.env` 恢复为恢复前的内容，提示修正 Compose 覆盖文件或先改 `.env` 让两者一致，或使用 `--replace-env`）；
3. 检查**整个目标 PostgreSQL 实例**与对象存储必须为空：使用目标/备份 `.env` 中的超级用户凭据经 TCP 连接 PostgreSQL（连不上或非超级用户直接拒绝并说明原因），通过 `pg_database` 列出全部非模板库（排除 `template0`、`template1`，包含 `postgres` 库在内），逐库检查非系统 schema 下 `pg_class` 全部关系及 `pg_proc` / `pg_type` 用户定义函数与类型：如果只有应用库（`DB_NAME`）非空则拒绝并指向下方“在已有实例上覆盖恢复”步骤；如果应用库以外的数据库里有数据，由于 `backup.sh` 只备份 `DB_NAME`，脚本会列出这些库名并提示先自行备份、确认不再需要后再按手册清空实例（**不会**直接给出 `down -v` 命令）；对象存储内不能有任何桶（或只能有 `minio_init` 刚创建、尚无任何对象的空桶）；同时用 `pg_restore --list` 校验备份文件。在第一次向数据库或对象存储写入数据之前的任何一步失败，`trap` 都会自动把 `.env.pre-restore-<时间戳>` 换回 `.env`（原无 `.env` 则删除新写入的 `.env`）；开始写入数据后再失败则不再自动换回 `.env`，并输出原 `.env` 备份路径供人工按手册处理；恢复成功后会保留 `.env.pre-restore-<时间戳>` 并在结尾输出路径，提示确认无误后自行删除；
4. 调用所有 `docker compose` 命令时均显式指定 `-p <项目名> -f <文件>... --env-file <.env>`，并提前通过 `env -u` 清除 `.env` 与编排文件中引用的键（及 `COMPOSE_PROJECT_NAME`、`COMPOSE_FILE`、`COMPOSE_PROFILES`），防止 shell 里导出的旧变量覆盖 `.env` 文件；
5. 通过 `backend` 服务实际生效的环境直接向正式对象存储的原桶名写入对象（流式上传并读回校验大小、SHA-256 与 `ContentType`），直接向正式数据库恢复 `postgres.dump` 并执行 `migrate`，再逐条核对数据库引用对象（跳过状态为 `uploading` 的未完成上传记录并在输出中列出跳过的条数；若已完成记录仍有对象缺失，则直接失败退出并提示在线备份的时间窗口）；
6. 若 `.env` 来自备份则重建 `edge` 镜像（除非传入 `--skip-edge-build`），恢复 Caddy `/data`，最后启动全套服务并执行健康探测与双链版本核验。

#### 空机恢复步骤

1. 在新服务器上解压对应（或更新）版本的发行包至 `/opt/dalton`，并将校验通过的备份目录拷入本机（例如 `/var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3`）。
2. 执行恢复（直接写进正式库与备份中的原桶名）：
   ```bash
   cd /opt/dalton

   # 方式 A：备份目录含 env.enc，传入口令文件解密并原子放入 .env（写入数据前失败会自动回滚）后恢复全部数据
   bash scripts/standalone/restore.sh \
     --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3 \
     --env-passphrase-file /etc/dalton/backup.pass

   # 方式 B：提前将备份同期保存的原件 .env 放入 /opt/dalton/.env（权限 0600），再执行恢复
   bash scripts/standalone/restore.sh \
     --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3
   ```
   > - 如果目标目录已有一份 `.env` 且目标实例为空，需要用备份内的 `env.enc` 替换该 `.env`，附加 `--replace-env`（要求新旧 `.env` 中的 `COMPOSE_PROJECT_NAME` 逐字一致；从 `env.enc` 恢复时，脚本会先把原 `.env` 备份成 `.env.pre-restore-<时间戳>`——`.env` 是普通文件时备份权限为 `600`，`.env` 是符号链接时备份保留链接本身、权限由链接指向的文件决定——再放入新 `.env`，写入数据前任何一步失败会自动换回原 `.env`，恢复成功后会保留该备份并输出路径，确认无误后自行删除）。
   > - 若使用已有 `.env` 而未用 `env.enc` 覆盖，请确认其中的对象存储桶名、键前缀与 `BYOK_ENCRYPTION_KEY` 均与备份同期原件一致（桶名或键前缀不一致会在写入前被直接拒绝，可先改 `.env` 让两者一致，或使用 `--replace-env`）。使用企业自有证书（`custom`）模式时，提前恢复 `DALTON_TLS_CERT_DIR` 并可附加 `--ca-file <企业CA.pem>`。
   > - 如需显式指定目标实例的 Docker Compose 项目名，可附加 `--project-name <项目名>`（优先级高于 `.env` 中的 `COMPOSE_PROJECT_NAME` 与部署目录名）。
3. 恢复完成后，提前构建代码沙箱镜像以消除首次对话等待：
   ```bash
   docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .
   ```

#### 在已有实例上覆盖恢复（含升级迁移后回滚）

> **危险操作警告**：下列第 3 步的 `docker compose -p "$PROJECT" ... down -v` **会永久删除目标项目的数据库、对象存储、缓存与证书卷中的所有当前数据**，且无法撤回。执行前**必须**先完成第 1–2 步，确认目标项目名无误、手里有一份校验通过且完整可用的备份，且实例里除应用库外没有需要保留的其他数据库！

如果需要用备份覆盖已有数据的实例（包括升级执行数据库迁移后需要回滚到升级前状态），请按以下人工步骤依次操作（以下以部署目录 `/opt/dalton`、备份目录 `/var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3` 为例；启用 HTTPS 时在所有 `docker compose` 命令中一并加上 `-f deploy/standalone/docker-compose.tls.yml`，IPv6 回环加上 `-f deploy/standalone/docker-compose.tls.ipv6.yml`）：

1. **先确定唯一的目标项目名 `PROJECT`**（后面所有检查数据库、列出卷、`down -v` 与恢复命令都必须使用这同一个 `$PROJECT`，切勿中途改用其他项目名）：

   这里填的就是 `upgrade.sh` / `restore.sh` 输出里显示的那个项目名（例如 `label=com.docker.compose.project=<项目名>` 或 `docker compose -p <项目名>` 中的值；若未显式传入 `--project-name`，对应 `.env` 里转小写后的 `COMPOSE_PROJECT_NAME`，`.env` 未设置时由部署目录名推导）：
   ```bash
   cd /opt/dalton
   PROJECT=<项目名>
   test -n "$PROJECT"
   echo "目标项目名: ${PROJECT}"
   ```

2. **确认有一份校验通过的备份，并检查实例里应用库以外的数据库有没有用户对象**：检查 `backup.sh` 输出中是否包含 PostgreSQL、MinIO、Caddy 的 `OK: ... verified` 校验结果与 `== Backup Complete ==`，并在备份目录中重新核验 `SHA256SUMS`：
   ```bash
   cd /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3
   sha256sum -c SHA256SUMS
   cat manifest.json
   ```
   确认全部文件返回 `OK`、`postgres.dump` 与 `minio-objects.tar.gz` 完整存在，且同期 `.env`（或 `env.enc` 与口令文件）可用；若打算保留当前 `.env`，请先核对 `manifest.json` 里的桶名、端点与键前缀是否与实际生效的 `backend` 配置一致。

   **执行 `down -v` 之前，逐个检查该实例所有非模板库中是否存在用户对象**（`backup.sh` 只备份 `.env` 里的 `DB_NAME`，而 `down -v` 会删除整个 PostgreSQL 数据卷；下面循环会输出每个非模板库的用户对象数量，若除应用库 `DB_NAME` 之外还有任意库的结果大于 0，说明该库里有数据，请先自行备份并确认不再需要）：
   ```bash
   cd /opt/dalton
   # 若修改过 DB_USER，请将下方 DB_USER 替换为 .env 中的超级用户名
   DB_USER=postgres
   (
     set -eu
     if ! dbs_raw="$(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T postgres \
       psql -U "$DB_USER" -d template1 -Atqc \
       "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('template0','template1') ORDER BY datname;")"; then
       printf 'ERROR: 数据库列表检查失败，先不要删除卷\n' >&2
       exit 1
     fi
     dbs="$(printf '%s\n' "$dbs_raw" | tr -d '\r')"
     if [ -z "$dbs" ]; then
       printf 'ERROR: 未查到任何非模板数据库，检查失败，先不要删除卷\n' >&2
       exit 1
     fi
     while IFS= read -r db; do
       [ -z "$db" ] && continue
       if ! cnt_raw="$(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T postgres \
         psql -U "$DB_USER" -d "$db" -Atqc \
         "SELECT (SELECT count(*) FROM pg_class WHERE relnamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog','information_schema') OR nspname LIKE 'pg_toast%' OR nspname LIKE 'pg_temp%')) + (SELECT count(*) FROM pg_proc WHERE pronamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog','information_schema') OR nspname LIKE 'pg_toast%' OR nspname LIKE 'pg_temp%')) + (SELECT count(*) FROM pg_type WHERE typnamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog','information_schema') OR nspname LIKE 'pg_toast%' OR nspname LIKE 'pg_temp%'));" </dev/null)"; then
         printf 'ERROR: 数据库 %s 检查失败，先不要删除卷\n' "$db" >&2
         exit 1
       fi
       cnt="${cnt_raw%$'\r'}"
       case "$cnt" in
         ''|*[!0123456789]*)
           printf 'ERROR: 数据库 %s 检查失败，先不要删除卷（输出非纯数字: %s）\n' "$db" "$cnt" >&2
           exit 1
           ;;
       esac
       printf '%s\tuser_objects=%s\n' "$db" "$cnt"
     done <<EOF
   $dbs
   EOF
   )
   ```

3. **列出项目 `"$PROJECT"` 下的完整卷清单并人工确认后，执行 `down -v` 删除本项目的卷**（仅删除当前项目的容器与数据卷，保留 `/opt/dalton/.env` 与发行文件）：
   ```bash
   cd /opt/dalton

   # 按项目标签列出完整卷清单（包含已移除容器残留的数据卷），请逐项人工确认
   docker volume ls --filter "label=com.docker.compose.project=${PROJECT}"
   ```
   **请仔细核对上面输出的完整卷清单**，确认每一个卷均属于当前要重置的项目 `"$PROJECT"` 且第 2 步备份与数据库检查均已通过，然后显式指定 `-p "$PROJECT"` 与 `.env` 删除容器与卷：
   ```bash
   # 回环 HTTP 模式（启用 HTTPS 时额外加上 -f deploy/standalone/docker-compose.tls.yml）
   docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.core.yml --env-file .env down -v
   ```

4. **使用 `restore.sh`（显式指定 `--project-name "$PROJECT"`）恢复到这个空实例**：
   ```bash
   cd /opt/dalton

   # 使用当前目录保留的 .env 恢复数据
   bash scripts/standalone/restore.sh \
     --project-name "$PROJECT" \
     --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3

   # 如需同时用备份中的 env.enc 替换当前 .env，附加 --env-passphrase-file 与 --replace-env
   # bash scripts/standalone/restore.sh \
   #   --project-name "$PROJECT" \
   #   --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3 \
   #   --env-passphrase-file /etc/dalton/backup.pass \
   #   --replace-env
   ```

#### 恢复中途失败的处理

如果 `restore.sh` 在**开始写入对象存储或数据库之后**的任何一步失败（例如对象上传中断、`pg_restore` / `migrate` 报错、DB 对象引用核对发现缺失、容器启动或健康探测失败），目标实例会处于**半成品**状态——此时脚本**不会**自动换回 `.env`（避免配置与已写入数据不一致），会在错误输出中写明原 `.env` 备份路径 `.env.pre-restore-<时间戳>`（若是从 `env.enc` 替换已有 `.env`），且直接重跑 `restore.sh` 会被空实例守卫拒绝。

**备份目录本身只读、完全不受影响。** 请直接按上方 §6.2「在已有实例上覆盖恢复」的第 1 步与第 3–4 步处理（沿用同一个 `PROJECT=<项目名>`）：
1. 用 `docker volume ls --filter "label=com.docker.compose.project=${PROJECT}"` 确认本项目的完整卷清单；
2. 执行 `docker compose -p "$PROJECT" -f <文件>... --env-file .env down -v` 清理半成品实例；
3. 排查并修正导致上次失败的原因后，重新运行 `restore.sh --project-name "$PROJECT" ...` 完成恢复。

#### 恢复后的确认方法

1. 检查容器状态与桶配置（沿用同一个 `PROJECT=<项目名>`，启用 HTTPS 时加上 `-f deploy/standalone/docker-compose.tls.yml`）：
   ```bash
   cd /opt/dalton
   docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.core.yml --env-file .env ps
   grep -E '^DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=' .env
   ```
2. 打开 Web UI，确认历史工作区、对话线程回放与既有上传文件均可正常打开与下载，并尝试上传一个新文件验证对象存储可写。

### 6.3 平滑升级与回滚

`scripts/standalone/upgrade.sh` 按以下安全顺序执行原地升级：

1. 先在临时目录解压并校验新版本发行包（不修改部署目录）；
2. 调用**旧版** `scripts/standalone/backup.sh` 完成全量在线备份（若从早期 Batch 1 / `#480` 首次升级且旧目录尚无 `backup.sh`，则自动调用新发行包内的 `backup.sh` 备份旧栈）；
3. 为旧版 `backend`、`edge`、`crawler` 镜像打上 `pre-upgrade-<时间戳>` 标签，并将旧版发行文件快照保存到 `.upgrade-backup/<时间戳>/`；
4. 替换部署目录中的发行文件（保留 `.env`、`backups/`、`.upgrade-backup/`、`.restore-backup/`、`deploy/standalone/certs/` 与 `deploy/standalone/extra/`），构建新镜像、执行 `migrate` 与 `minio-init`、滚动替换 `backend`（等待 `/health` 通过）、`crawler` 与 `edge`，最后执行一次普通 `docker compose -p <项目名> -f <文件>... --env-file .env up -d` 收敛全部服务，并完成健康探测与双链版本核验。

#### 从 `#480`（Batch 1）首次升级与 Redis 7.4 → Valkey 8 卷迁移

Batch 1（`#480`）的旧部署目录中**尚未包含** `scripts/standalone/backup.sh` 与 `scripts/standalone/upgrade.sh`，因此首次升级时必须**先解压新包，再在新包目录里运行 `scripts/standalone/upgrade.sh --work-dir <旧目录> --release-dir <新包目录>`**。

此外，Batch 1（`#480`）使用 `redis:7-alpine`（Redis 7.4，写出 **RDB v12** 快照格式），本版本起 `redis` 服务已切换为 **Valkey 8**（BSD-3-Clause 协议，仅兼容至 Redis 7.2 / **RDB v11**）。**Valkey 8 无法直接读取 Redis 7.4 写出的 `dump.rdb` 文件**（启动会报 `Can't handle RDB format version 12`）。在 Dalton 独立核心版中，Redis 仅存放瞬态缓存、SSE 流重连缓冲与限流计数，所有权威业务数据均持久化在 PostgreSQL 与 MinIO 中。

按以下顺序直接照抄执行从 `#480` 首次升级（以下以旧目录 `/opt/dalton`、新包解压目录 `/tmp/dalton-core-new` 为例）：

```bash
# 1. 先解压新版本发行包到临时目录
mkdir -p /tmp/dalton-core-new
tar -xzf /tmp/dalton-core-<new-version>.tar.gz -C /tmp/dalton-core-new

# 2. 使用新包里的 backup.sh 对旧目录执行升级前完整备份（PostgreSQL + MinIO + Caddy + .env）
cd /tmp/dalton-core-new
bash scripts/standalone/backup.sh \
  --work-dir /opt/dalton \
  --output-dir /var/backups/dalton \
  --env-passphrase-file /etc/dalton/backup.pass

# 3. 从 .env 读取旧项目名，获取旧项目 redis 容器挂载的 /data 卷名，停机归档并校验成功后，再删除该卷
cd /opt/dalton
PROJECT_NAME="$(python3 -c 'import re; m=[re.match(r"^\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*[=:]\s*[\"'"'"']?([^\"'"'"'#\s]+)", l) for l in open(".env")]; vals=[x.group(1) for x in m if x]; print(vals[-1] if vals else "")')"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g')}"
REDIS_CID="$(docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env ps -aq redis | head -n 1)"
REDIS_VOL="$(docker inspect --format '{{range .Mounts}}{{if and (eq .Type "volume") (eq .Destination "/data")}}{{.Name}}{{end}}{{end}}' "$REDIS_CID")"
REDIS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$REDIS_CID")"
echo "旧项目 (${PROJECT_NAME}) Redis 卷名: ${REDIS_VOL}"

REDIS_ARCHIVE="/var/backups/dalton/redis-pre-valkey-$(date -u +%Y%m%d-%H%M%SZ).tar.gz"
test -n "$REDIS_CID" && test -n "$REDIS_VOL" && \
  docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env stop redis && \
  docker run --rm -v "${REDIS_VOL}:/data:ro" --entrypoint /bin/sh "$REDIS_IMAGE" -c 'tar -czf - -C /data .' > "$REDIS_ARCHIVE" && \
  chmod 600 "$REDIS_ARCHIVE" && \
  test -s "$REDIS_ARCHIVE" && \
  tar -tzf "$REDIS_ARCHIVE" >/dev/null && \
  docker compose -p "${PROJECT_NAME}" -f docker-compose.yml -f docker-compose.core.yml --env-file .env rm -f redis && \
  docker volume rm "$REDIS_VOL"

# 4. 在新包目录里运行 upgrade.sh 完成升级（Valkey 8 会自动创建全新的 RDB v11 数据卷）
cd /tmp/dalton-core-new
bash scripts/standalone/upgrade.sh \
  --work-dir /opt/dalton \
  --release-dir /tmp/dalton-core-new \
  --backup-dir /var/backups/dalton \
  --env-passphrase-file /etc/dalton/backup.pass
```

> 若您在同一个 Redis 实例中额外存放了自定义持久键且需要保留，切勿直接拷贝 `dump.rdb`，请在停机前使用逻辑迁移工具（如 `RedisShake` 或客户端 `SCAN` 导出后导入新 Valkey 8 实例）完成数据迁移。

执行常规升级（已完成首次升级、部署目录已有 `upgrade.sh` 时）：

```bash
cd /opt/dalton
bash scripts/standalone/upgrade.sh \
  --release-tar /tmp/dalton-core-<new-version>.tar.gz \
  --backup-dir /var/backups/dalton \
  --env-passphrase-file /etc/dalton/backup.pass
```

使用企业自有证书（`custom`）模式时，可附加 `--ca-file <企业CA.pem>` 完成升级后 HTTPS 探测。如需显式指定目标实例的 Docker Compose 项目名，可附加 `--project-name <项目名>`（优先级高于 `.env` 中的 `COMPOSE_PROJECT_NAME` 与部署目录名，并会透传给升级前调用的 `backup.sh`）。

#### 升级失败回滚

当任意步骤失败时，脚本输出的所有 `docker compose` 命令均显式携带 `-p <项目名> -f <文件>... --env-file <.env>`，并按以下三种情况给出对应的 shell 转义回滚指引：
- **迁移尚未开始就失败**（备份、解压、文件替换或镜像构建阶段失败）：数据库表结构未被改动，不需要动数据（避免丢弃备份后的新写入）；脚本仅提示使用 `rsync -a --delete`（排除 `.env`、`backups/`、`.upgrade-backup/`、`.restore-backup/`、`certs/`、`extra/`）还原旧发行文件、恢复 `pre-upgrade-<时间戳>` 镜像标签并重启服务；
- **迁移之后失败（迁移前后版本号均成功读到且至少一条迁移链版本已变化）**：依次提示：
  1. 使用 `rsync -a --delete`（带上述排除项）从 `.upgrade-backup/<时间戳>/` 还原旧版发行文件；
  2. 通过 `docker tag <镜像>:pre-upgrade-<时间戳> <镜像>:local` 恢复旧版容器镜像标签；
  3. 按上文 §6.2「在已有实例上覆盖恢复（含升级迁移后回滚）」的人工步骤操作：先核验升级前备份，再用 `docker volume ls --filter label=com.docker.compose.project=<项目名>` 列出并确认本项目完整卷清单后执行 `docker compose -p <项目名> -f <文件>... --env-file <.env> down -v`，最后用 `restore.sh --skip-edge-build` 恢复到空实例；
- **迁移状态不确定（`migrate` 已启动后，迁移前或迁移后未能读到版本号，或者前后版本号相同）**：由于部分迁移会在 `autocommit_block()` 前提交 DDL，版本号未变化也不足以保证表结构完全未改动；脚本**不会**直接认定"未改动"或"已迁移"，而是提示先人工查询当前 `alembic_version` / `alembic_version_dalton` 及表结构并与升级前备份的 `manifest.json` 核对，确认无不兼容改动则只还原文件与镜像，否则按 §6.2 人工步骤恢复。

---

## 7. 常见问题

### 7.1 MinIO 镜像拉取失败（`cgr.dev` 500 或不可达）

核心版默认从 Chainguard 拉取按摘要固定的 MinIO 镜像（`cgr.dev/chainguard/minio@sha256:...`），因为上游官方 `minio/minio` 社区镜像已停止分发并下线。若遇到 `cgr.dev` 临时返回 500 或网络受限，可在 `.env` 中通过 `DALTON_MINIO_IMAGE` 切换备用镜像源（如 AWS ECR Public 上的 Chainguard 镜像或企业内部制品库镜像）：

```dotenv
# 备用源 1：AWS ECR Public 上的 Chainguard MinIO 镜像（按摘要固定版本）
DALTON_MINIO_IMAGE=public.ecr.aws/chainguard/minio@sha256:e70b2b1e74d79457f808710483bd726ff364529ca30736eaa15a6ab547d3a5a7

# 备用源 2：企业内部镜像仓库同步后的镜像（按摘要固定版本）
# DALTON_MINIO_IMAGE=registry.corp.internal/mirror/chainguard/minio@sha256:e70b2b1e74d79457f808710483bd726ff364529ca30736eaa15a6ab547d3a5a7
```

修改 `.env` 后重新拉起 `minio` 服务：

```bash
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env up -d minio
```

### 7.2 公网 HTTPS 证书签发失败

1. 核对域名解析是否已生效：`dig +short <您的域名>` 返回本机公网 IP。
2. 核对宿主机防火墙（`ufw` / `firewalld`）与云安全组均已放行入站 TCP 80 和 443 端口。
3. 核对宿主机 80/443 端口未被其他进程占用：`ss -tlnp | grep -E ':80 |:443 '`。
4. 查看边缘网关实时日志定位具体原因：
   ```bash
   docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml --env-file .env logs --tail=100 edge
   ```

### 7.3 首次对话等待时间较长

首次运行代码分析需要本地存在 `langalpha-sandbox:latest` 镜像。检查镜像是否已提前构建：

```bash
docker image inspect langalpha-sandbox:latest >/dev/null && echo "已就绪"
```

若尚未构建，提前执行 `docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .` 即可消除首次对话的构建等待。

### 7.4 自举口令过期或输错次数超限

在服务器执行 `reissue-bootstrap-token` 重置失败计数并生成新的 24 小时有效口令：

```bash
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec -T backend \
  uv run --no-sync python -m src.dalton.auth.cli reissue-bootstrap-token
```

### 7.5 管理员忘记密码

在服务器执行 `reset-password` 交互式设置新密码（命令同时撤销所有旧会话）：

```bash
docker compose -p <项目名> -f docker-compose.yml -f docker-compose.core.yml --env-file .env exec backend \
  uv run --no-sync python -m src.dalton.auth.cli reset-password --email <管理员邮箱>
```

### 7.6 提示无法访问 `/var/run/docker.sock`

确认当前部署账号已加入 `docker` 组并重新登录终端，或将 Rootless Docker 套接字路径写入 `.env` 中的 `DOCKER_HOST_SOCKET` 后重启 `backend`。

### 7.7 磁盘空间增长较快

定期检查未使用的旧容器镜像与已退出沙箱资源：

```bash
docker system df
docker image prune -f
```

同时通过 `backup.sh --retain <N>` 控制本机保留份数，并将历史备份转存至异地存储。
