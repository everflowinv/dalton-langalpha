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
| 基础命令行工具 | `bash`、`openssl`、`curl`、`tar`、`python3`、`make`、`sha256sum` | 供初始化、配置向导、备份、恢复与升级脚本调用 |

验证主机容器环境版本：

```bash
docker --version
docker compose version
```

### 1.2 网络与端口要求

| 部署模式 | 需放行的入站 TCP（传输控制协议）端口 | Compose 编排文件 |
|---|---|---|
| 本机回环 HTTP 试用（`DALTON_TLS_MODE=loopback`，当前默认） | 仅本机 `127.0.0.1:8088` | `docker-compose.yml` + `docker-compose.core.yml` |
| 本机回环 HTTPS（`DALTON_TLS_MODE=internal` / `custom`） | 仅本机 `127.0.0.1:18080` 与 `127.0.0.1:18443` | 额外附加 `deploy/standalone/docker-compose.tls.yml` |
| 公网自动 HTTPS（`DALTON_TLS_MODE=auto`，待 Batch 4b 开放） | **80**（ACME 验证与跳转）与 **443**（HTTPS） | 额外附加 `deploy/standalone/docker-compose.tls.yml` |
| 企业内网 HTTPS（`DALTON_TLS_MODE=internal` / `custom`，待 Batch 4b 开放） | **80** 与 **443**（内网可达） | 额外附加 `deploy/standalone/docker-compose.tls.yml` |

> **对外发布范围说明**：当前核心版处于免密单机试用阶段，`scripts/standalone/init.sh` 仅接受回环地址（`localhost`、`127.0.0.1`、`::1`），支持回环 HTTP（`loopback`）和回环 HTTPS（`internal` / `custom`）。正式面向团队或公网部署将在本地登录（Batch 4b）上线后开放；届时 `init.sh` 会在首次启动前自动写入 `DALTON_AUTH_MODE=local` 与 `VITE_DALTON_AUTH=local`。

后端服务（`backend`）、数据库（`postgres`）、缓存（`redis`，运行 Valkey 8）和对象存储（`minio`）均运行在固定子网 `core_internal`（默认 `172.31.250.0/24`，可通过 `DALTON_CORE_INTERNAL_SUBNET` 覆盖）中，所有外部请求统一经边缘网关 `edge`（Caddy）进入。`init.sh` 会自动将 `DALTON_TRUSTED_PROXIES` 设为该子网并将 `DALTON_TRUSTED_PROXY_HOPS` 设为 `1`，以便后端按 `X-Forwarded-For` 记录真实客户端 IP 并执行分 IP 限速。代码沙箱容器运行在独立隔离网络 `sandbox-net` 中，仅与后端通信。

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

执行 `scripts/standalone/init.sh`，脚本会自动检测系统环境、配置回环访问模式、写入 `COMPOSE_FILE` 与可信代理参数、生成全套随机密钥（写入权限为 `0600` 的 `.env` 文件），并构建代码沙箱镜像 `langalpha-sandbox:latest`：

```bash
# 本机回环 HTTP 试用（仅发布 127.0.0.1:8088）
bash scripts/standalone/init.sh --domain localhost

# 或启用本机回环内部 CA HTTPS（仅发布 127.0.0.1:18080 与 127.0.0.1:18443）
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

`init.sh` 已将所需编排文件写入 `.env` 中的 `COMPOSE_FILE`，直接运行即可（或显式传入对应的 `-f` 参数）：

```bash
# 使用 .env 中的 COMPOSE_FILE 启动
docker compose up -d

# 显式命令（回环 HTTP 模式）
docker compose -f docker-compose.yml -f docker-compose.core.yml up -d

# 显式命令（启用 HTTPS 模式）
docker compose -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml up -d
```

启动流程按顺序完成：
1. 拉起 `postgres`（含 `pgvector` 向量扩展）、`redis`（Valkey 8）与 `minio`；
2. 运行一次性容器 `minio-init` 建立私有存储桶 `chat-uploads`；
3. 运行一次性容器 `migrate` 通过 PostgreSQL 咨询锁串行执行上游与 Dalton 两条 Alembic（数据库表结构迁移工具）迁移链至最新版本；
4. 启动 `backend`、`crawler` 与 `edge`。

查看所有服务状态与后端健康检查：

```bash
docker compose ps
docker compose exec -T backend curl -sf http://127.0.0.1:8000/health
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
docker compose -f docker-compose.yml -f docker-compose.core.yml up -d
```

此模式仅发布 `127.0.0.1:8088`（通过 `http://127.0.0.1:8088` 访问，或从本地电脑通过 `ssh -L 8088:127.0.0.1:8088 user@server` 转发访问），宿主机 80/443 端口保持空闲。

### 3.2 内部 CA HTTPS 模式（`internal`）

适用于本机 HTTPS 验证（当前可用），以及 Batch 4b 上线后的企业内网域名（如 `dalton.internal`）与局域网 IP：

```bash
# 本机回环 HTTPS（仅绑定 127.0.0.1:18080 与 127.0.0.1:18443，HTTP 跳转自动携带 18443 端口并启用 HSTS）
bash scripts/standalone/init.sh --domain localhost --tls-mode internal
docker compose -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml up -d
```

启动 `edge` 后，导出 Caddy 内部 CA（证书颁发机构）根证书并导入客户端或操作系统的受信任根证书库：

```bash
docker compose exec -T edge \
  cat /data/caddy/pki/authorities/local/root.crt > dalton-local-ca.crt
```

### 3.3 企业自有证书模式（`custom`）

将完整证书链 `cert.pem` 与私钥 `key.pem`（权限 `0600`）放入指定目录（默认为 `./deploy/standalone/certs`）后执行：

```bash
bash scripts/standalone/init.sh \
  --domain localhost \
  --tls-mode custom \
  --tls-cert-dir /etc/dalton/certs
docker compose -f docker-compose.yml -f docker-compose.core.yml -f deploy/standalone/docker-compose.tls.yml up -d --no-deps --force-recreate edge
```

> **自有证书备份与健康探测**：`backup.sh` 仅归档 Caddy 的 `/data` 卷，请单独做好 `DALTON_TLS_CERT_DIR`（含 `cert.pem` 与 `key.pem`）的备份与恢复。若证书由企业内部 CA 签发，在执行 `restore.sh` 和 `upgrade.sh` 时传入 `--ca-file <企业CA根证书.pem>` 完成 HTTPS 校验。

### 3.4 公网域名自动 HTTPS（`auto`，待 Batch 4b 上线后启用）

本地登录（Batch 4b）上线后，`init.sh` 将接受公网域名并在首次启动前自动写入 `DALTON_AUTH_MODE=local` 与 `VITE_DALTON_AUTH=local`：

1. 将目标域名（如 `dalton.example.com`）的 `A` / `AAAA` 记录指向服务器公网 IP。
2. 在云安全组与系统防火墙放行入站 **TCP 80** 与 **TCP 443** 端口，并确认宿主机端口空闲。
3. 运行 `bash scripts/standalone/init.sh --domain dalton.example.com`，随后使用含 `deploy/standalone/docker-compose.tls.yml` 的编排命令启动服务，Caddy 将通过 ACME 自动签发并续期证书。

---

## 4. 首位管理员与账号管理（Batch 4b 本地登录上线后）

正式面向多人或外部网络部署时，需在**首次对外启动前**启用本地多用户账号体系（基于 PostgreSQL 存储密码哈希、签发短期 JWT〔JSON 网络签名令牌〕与可轮换刷新会话）：

### 4.1 启用本地多用户账号模式

在 `.env` 中配置以下参数，并确保首次构建 `edge` 时带入 `VITE_DALTON_AUTH=local`：

```dotenv
DALTON_AUTH_MODE=local
VITE_DALTON_AUTH=local
```

修改 `VITE_DALTON_AUTH` 后重新构建并启动 `edge` 与 `backend`：

```bash
docker compose up -d --build edge backend
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

当自举口令过期、连续输错 5 次触发锁定，或管理员忘记密码时，直接在服务器上通过容器内的运维 CLI（命令行界面）处理：

```bash
# 重新签发 24 小时有效的自举口令（仅在尚未完成自举前有效）
docker compose exec -T backend \
  uv run --no-sync python -m src.dalton.auth.cli reissue-bootstrap-token

# 重置指定账号密码并立即撤销该账号全部既有登录会话
docker compose exec backend \
  uv run --no-sync python -m src.dalton.auth.cli reset-password --email admin@example.com

# 为已有用户授予管理员角色
docker compose exec -T backend \
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
  docker compose restart backend
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
2. MinIO 全部存储桶对象归档 `minio-objects.tar.gz`（按块流式读写并增量计算逐文件 SHA-256，保留 `ContentType`）；
3. Caddy 证书与内部 CA 数据归档 `caddy-data.tar.gz`；
4. `.env` 密钥文件：传入 `--env-passphrase-file` 时使用 OpenSSL AES-256-CBC + PBKDF2（参数写入 `manifest.json`，整体完整性由 `SHA256SUMS` 校验）加密生成 `env.enc`。

> **一致性与私钥保管边界**：
> - **在线一致性窗口**：数据库 `postgres.dump` 是单一时刻的一致快照，MinIO 对象枚举紧随其后执行。在此窗口内被删除的对象可能缺失于对象归档中；恢复时 `restore.sh` 会核对并报告数据库中引用了但对象存储缺失的条目。
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
- `--env-passphrase-file <文件>`：对 `.env` 加密生成 `env.enc`（也可通过环境变量 `DALTON_BACKUP_PASSPHRASE_FILE` 指定）。

#### 配置每日定时自动备份（cron，使用同一部署账号）

以同一部署账号编辑定时任务：

```bash
crontab -e
```

加入如下任务（每天凌晨 02:30 执行，并配合异地同步任务将 `/var/backups/dalton` 加密转存至异地存储）：

```cron
30 2 * * * cd /opt/dalton && /usr/bin/env bash scripts/standalone/backup.sh --output-dir /var/backups/dalton --retain 14 --env-passphrase-file /etc/dalton/backup.pass >> /var/log/dalton/backup.log 2>&1
```

### 6.2 在全新主机或已有实例上正式恢复

`scripts/standalone/restore.sh` 依次完成：`SHA256SUMS` 与 `pg_restore --list` 校验 → 解密 `.env`（按 `manifest.json` 记录的 PBKDF2 参数）并重建 `edge` 镜像 → 检查目标数据库全部非系统 schema 与目标 MinIO 存储桶是否为空 → 恢复数据库、MinIO 对象（流式上传、还原 `ContentType` 并读回校验 SHA-256）与 Caddy 证书 → 报告数据库引用但 MinIO 缺失的对象 → 启动全部服务并执行健康探测。

#### 空机恢复步骤（先恢复 `.env`，再构建镜像）

1. 在新服务器上解压对应（或更新）版本的发行包至 `/opt/dalton`，并将备份目录拷入本机（例如 `/var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3`）。
2. 先恢复 `.env` 并完成数据恢复（`restore.sh` 在解密 `.env` 后会自动重建 `edge` 镜像以匹配其中的品牌与认证参数）：
   ```bash
   cd /opt/dalton

   # 方式 A：备份目录含 env.enc，传入口令文件自动还原 .env 并恢复全部数据
   bash scripts/standalone/restore.sh \
     --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3 \
     --env-passphrase-file /etc/dalton/backup.pass

   # 方式 B：提前将备份同期保存的原件 .env 放入 /opt/dalton/.env（权限 0600），再执行恢复
   bash scripts/standalone/restore.sh \
     --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3
   ```
   > 若使用已有 `.env` 而未用备份内的 `env.enc` 覆盖，请确认其中的 `BYOK_ENCRYPTION_KEY` 与备份同期原件一致，否则已保存的模型密钥将无法解密。使用企业自有证书（`custom`）模式时，提前恢复 `DALTON_TLS_CERT_DIR` 并可附加 `--ca-file <企业CA.pem>`。
3. 在 `.env` 就绪并完成恢复后，提前构建代码沙箱镜像以消除首次对话等待：
   ```bash
   docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .
   ```

#### 在已有数据的实例上覆盖恢复

`restore.sh` 将数据替换和配置替换分为两个显式授权参数：
- `--replace-data`：允许替换已有数据库与对象存储。脚本先将备份恢复到临时库 `<库名>_restore_<时间戳>` 并完成迁移，全部成功后才停应用并改名切换，同时将原库改名为 `<库名>_pre_restore_<时间戳>` 完整保留；对象存储端会同步清理备份清单以外的多余对象。
- `--replace-env`：允许使用备份中的 `env.enc` 覆盖现有 `.env`（覆盖后自动重建 `edge`）。

```bash
bash scripts/standalone/restore.sh \
  --backup-dir /var/backups/dalton/dalton-backup-20260101-023000Z-a1b2c3 \
  --replace-data
```

恢复完成并确认业务数据无误后，按脚本末尾打印的命令手动清理保留的原库 `<库名>_pre_restore_<时间戳>`。

### 6.3 平滑升级与回滚

`scripts/standalone/upgrade.sh` 按以下四步安全顺序执行原地升级：

1. 先调用**旧版** `scripts/standalone/backup.sh` 完成全量在线备份；
2. 为旧版 `backend`、`edge`、`crawler` 镜像打上 `pre-upgrade-<时间戳>` 标签，并将旧版发行文件快照保存到 `.upgrade-backup/<时间戳>/`；
3. 将新版本发行包解压至临时目录，校验必需文件齐全后再替换部署目录中的发行文件（保留 `.env`、`backups/`、`.upgrade-backup/`、`deploy/standalone/certs/` 与 `deploy/standalone/extra/`）；
4. 构建新镜像、执行 `migrate` 与 `minio-init`、滚动替换 `backend`（等待 `/health` 通过）、`crawler` 与 `edge`，最后执行一次普通 `docker compose up -d` 收敛全部服务，并完成健康探测与双链版本核验。

执行升级：

```bash
cd /opt/dalton
bash scripts/standalone/upgrade.sh \
  --release-tar /tmp/dalton-core-<new-version>.tar.gz \
  --backup-dir /var/backups/dalton \
  --env-passphrase-file /etc/dalton/backup.pass
```

使用企业自有证书（`custom`）模式时，可附加 `--ca-file <企业CA.pem>` 完成升级后 HTTPS 探测。

#### 升级失败回滚

当任意步骤失败时，脚本会在终端输出可直接复制执行的回滚命令（参数已做 shell 转义并携带实际使用的 `--compose-files`）：
1. 从 `.upgrade-backup/<时间戳>/` 还原旧版发行文件；
2. 通过 `docker tag <镜像>:pre-upgrade-<时间戳> <镜像>:local` 恢复旧版容器镜像标签；
3. 使用升级前备份目录执行 `restore.sh --replace-data --skip-edge-build` 恢复数据库、MinIO 对象与 Caddy 数据。

---

## 7. 常见问题

### 7.1 MinIO 镜像拉取失败（`cgr.dev` 500 或不可达）

核心版默认从 Chainguard 拉取按摘要固定的 MinIO 镜像（`cgr.dev/chainguard/minio@sha256:...`），因为上游官方 `minio/minio` 社区镜像已停止分发并下线。若遇到 `cgr.dev` 临时返回 500 或网络受限，可在 `.env` 中通过 `DALTON_MINIO_IMAGE` 切换备用镜像源（如 AWS ECR Public 上的 Chainguard 镜像或企业内部制品库镜像）：

```dotenv
# 备用源 1：AWS ECR Public 上的 Chainguard MinIO 镜像（建议拉取后改为对应 digest 固定）
DALTON_MINIO_IMAGE=public.ecr.aws/chainguard/minio:latest

# 备用源 2：企业内部镜像仓库同步后的镜像
# DALTON_MINIO_IMAGE=registry.corp.internal/mirror/chainguard/minio@sha256:e70b2b1e74d79457f808710483bd726ff364529ca30736eaa15a6ab547d3a5a7
```

修改 `.env` 后重新拉起 `minio` 服务：

```bash
docker compose up -d minio
```

### 7.2 公网 HTTPS 证书签发失败（Batch 4b 公网部署）

1. 核对域名解析是否已生效：`dig +short <您的域名>` 返回本机公网 IP。
2. 核对宿主机防火墙（`ufw` / `firewalld`）与云安全组均已放行入站 TCP 80 和 443 端口。
3. 核对宿主机 80/443 端口未被其他进程占用：`ss -tlnp | grep -E ':80 |:443 '`。
4. 查看边缘网关实时日志定位具体原因：
   ```bash
   docker compose logs --tail=100 edge
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
docker compose exec -T backend \
  uv run --no-sync python -m src.dalton.auth.cli reissue-bootstrap-token
```

### 7.5 管理员忘记密码

在服务器执行 `reset-password` 交互式设置新密码（命令同时撤销所有旧会话）：

```bash
docker compose exec backend \
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
