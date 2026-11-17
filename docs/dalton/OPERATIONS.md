# Dalton 运维手册

本文提供 Dalton 生产与准生产环境的构建部署、环境变量生效规则、宿主机 `launchd` 服务运维、会话归档运行与续跑、异地备份恢复、Tailscale Funnel 对外入口管理及常见故障处置；日常发版、修改 `.env` 或排障时按对应章节操作。正文仅列环境变量名，绝不记录密钥值或私有 URL 查询参数；所有容器端口严格绑定在 `127.0.0.1`。

## 1. 部署与构建（仓库现有脚本）

Dalton 使用上游编排叠加 `docker-compose.dalton.yml`，并配合专用后端构建与前端生产构建脚本：

### 1.1 后端重建与启动

每次修改 `src/`、`scripts/`、`plugins/`、`agent_config.dalton.yaml`、`config.dalton.yaml`、`config/dalton/` 或 `migrations/` 后，运行：

```bash
bash scripts/dalton/docker_build_backend.sh
```

- `scripts/dalton/docker_build_backend.sh` 自动注入当前 `BUILD_GIT_SHA` 与 `BUILD_GIT_DIRTY` 并执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --build backend`；仅构建镜像不重启时加 `--build-only`。
- 容器启动脚本 `scripts/dalton/docker_entrypoint_dalton.sh` 会自动依次执行上游与 Dalton 双链路迁移：
  1. `uv run alembic upgrade head`
  2. `uv run alembic -c src/dalton/alembic.ini upgrade head`

### 1.2 全栈与可选服务启动

```bash
# 启动基础全栈（启用 infra profile 以包含 postgres, redis，再叠加 backend, crawler, frontend, edge）
docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra up -d

# 启用语音转写/大文件对象存储 profile 时追加 --profile storage
docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra --profile storage up -d
```

- **Profile 说明**：`docker-compose.yml` 将 `postgres` 与 `redis` 放入 `profiles: ["infra"]`。若 `.env` 已配置 `COMPOSE_PROFILES=infra`，或部署使用外部 PostgreSQL 与 Redis，可省略 `--profile infra`。外部 PostgreSQL 在 `.env` 配置 `DB_HOST` 即可；外部 Redis 只改 `.env` 的 `REDIS_URL` 不够，`docker-compose.dalton.yml` 的 `backend` 服务把 `REDIS_URL` 固定为 `redis://…@redis:6379/0`，需要另加一个 compose 覆盖文件改写该值；否则必须显式传入 `--profile infra`，否则数据库与 Redis 不会启动。
- **镜像注意事项**：`docker-compose.dalton.yml` 将 `cli` 服务放入 `profiles: ["cli-disabled"]` 以保持默认关闭；`edge`（Caddy）挂载 `./web/dist:/srv:ro` 与 `./deploy/dalton/Caddyfile`；`crawler` 运行在隔离网络 `crawl`。

### 1.3 前端生产构建

Caddy `edge` 容器直接静态托管 `./web/dist`。每次发布前端代码或品牌资源调整后，必须在宿主机重跑生产构建并校对门禁：

```bash
bash scripts/dalton/build_frontend_prod.sh --require-supabase
```

- 该脚本先读取 `web/.env.dalton` 作为 `VITE_*` 默认值，再用仓库 `.env` 中的 `VITE_*`（及 `DALTON_AUTH_MODE` / `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` 回退）覆盖并导出环境变量，随后进入 `web/` 执行 `pnpm build`（脚本未设置 `APP_ENV=dalton`），并强制检查首屏预算（`web/scripts/check-critical-path.mjs`）与认证配置完整性（`--require-supabase` 防止构建出无 Supabase 认证的裸包）。
- 默认构建入口已带 `web/.env.dalton`（`VITE_BRAND_NAME=Everflow`、`VITE_BRAND_MARK=everflow`）。

### 1.4 生产部署

部署脚本将迁入 `scripts/dalton/ops/`，迁入后本节补全。

## 2. `.env` 与叠加配置文件改动生效规则

- **修改 `.env` 后必须重建容器**：Docker Compose 仅在创建容器阶段读取 `.env`（`docker compose restart backend` 不会重新注入变更后的环境变量！）。任何涉及 `INTERNAL_SERVICE_TOKEN`、`DALTON_ALLOWED_EMAILS`、`DALTON_PM_USER_IDS`、`DALTON_ARTIFACT_PUBLISHING_ENABLED`、`GEMINI_API_KEY`、`XAI_API_KEY`、`DALTON_WEB_SEARCH_BRIDGE_TOKEN` 等 `.env` 变量修改，必须执行：

```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --force-recreate backend
```

  若 `crawler` 或 `edge` 依赖的环境变量（如 `INTERNAL_SERVICE_TOKEN`）发生变更，需同时对该服务执行 `--force-recreate`。
- **修改 `agent_config.dalton.yaml` / `config.dalton.yaml` / `src/dalton/dalton.yaml`**：宿主机目录已挂入 `backend` 容器（`./agent_config.dalton.yaml`、`./config.dalton.yaml`、`./config/dalton` 等），但因为后端镜像启动会核验 `/app/.build_files.sha256` 清单指纹，**生产变更应统一通过 `scripts/dalton/docker_build_backend.sh` 重建并重建容器**。

## 3. 宿主机 `launchd` 服务运维

### 3.1 服务安装与常用管理命令

| 服务 Label | 安装/模板来源 | 重启命令 | 日志 / 状态位置 |
|---|---|---|---|
| `com.dalton.researchdata-bridge` | `bash scripts/dalton/install_bridge.sh` | `launchctl kickstart -k gui/$(id -u)/com.dalton.researchdata-bridge` | 模板内指定宿主机 Logs（见 `install_bridge.sh`） |
| `com.dalton.channel-gateway` | `scripts/dalton/launchd/com.dalton.channel-gateway.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway` | `~/.config/dalton/channel-gateway.env` (`0600`) 与 gateway 日志 |
| `com.dalton.monitor-feeds` | `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` | `launchctl kickstart -k gui/$(id -u)/com.dalton.monitor-feeds` | `var/dalton/monitor-feeds/` 状态文件与标准日志 |
| `com.dalton.conversation-archive` | `bash scripts/dalton/install_conversation_archive.sh` | `launchctl kickstart -k gui/$(id -u)/com.dalton.conversation-archive` | 归档脚本日志与 `var/dalton/conversations/inbox/` |
| `com.dalton.web-search-bridge` | `bash scripts/dalton/install_web_search_bridge.sh` | `launchctl kickstart -k gui/$(id -u)/com.dalton.web-search-bridge` | `~/.config/dalton/web-search-bridge.env` |
| `com.dalton.config-git-backup` | `scripts/dalton/ops/launchd/com.dalton.config-git-backup.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.config-git-backup` | `~/Library/Logs/dalton-offsite-backup.log` |
| `com.dalton.db-offsite` | `scripts/dalton/ops/launchd/com.dalton.db-offsite.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.db-offsite` | `~/Library/Logs/dalton-offsite-backup.log` & `*-alerts.log` |
| `com.dalton.data-backup` | `scripts/dalton/ops/launchd/com.dalton.data-backup.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.data-backup` | `~/Library/Logs/dalton-offsite-backup.log` & `*-alerts.log` |

查看某项服务实时状态：

```bash
launchctl print gui/$(id -u)/com.dalton.researchdata-bridge | head -n 40
```

> 安全提醒：修改 Vault 中的飞书应用凭据（`DALTON_FEISHU_APP_ID` / `DALTON_FEISHU_APP_SECRET`）后，网关不会热替换内存凭据；请在确认无在途重要推送后，通过 `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway` 重启刷新内存。

### 3.2 会话归档日常运行、回填与续跑（原 `CONVERSATION-ARCHIVE.md` 合并内容）

`com.dalton.conversation-archive` 的 `StartCalendarInterval` 配置在每日 `21:15`（按宿主机本地日历时区触发，当宿主机系统时区为 `Asia/Shanghai` 时即北京时间 `21:15`）使用 `src/dalton/dalton.yaml` 下 `conversations` 默认值运行（单并发 `extract_concurrency: 1`、每小时额度 `hourly_token_budget: 5000000`）：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py run
```

历史大批量回填（例如从某日期起回填，不修改 `dalton.yaml` 每日默认值）：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py backfill \
  --since 2026-04-30 \
  --concurrency 4 \
  --hourly-token-budget 1500000
```

- **并发与限流保护**：采用 `asyncio.Semaphore` 限并发，`HourlyTokenBudgetLimiter` 先按估算 token 预占预算并在完成后按实际用量校准；遇到瞬时 `429` 时共享退避窗口，遇到不可恢复配额错误（`PermanentQuotaError`）则取消在途任务并将已完成连续前缀落盘后退出。
- **逐会话有序落盘**：按 `(agent, session_id)` 维度的已完成连续前缀调用 embedding 并在同一事务内写入 `dalton_conversation_cards` 与 `dalton_conversation_processed`，同时实时更新 `dalton_conversation_runs` 进度（`segments_processed`、`cards_extracted`、`cards_inserted`、`total_tokens`）。某一会话单段失败仅阻塞该会话后续段，不影响其他会话推进。
- **运行锁（Advisory Lock）与中断续跑**：每次非 `--dry-run` 启动时抢占 Postgres 会话锁 `pg_try_advisory_lock(hashtext('dalton_conversation_archive'))`；若已有进程运行则输出 `"已有归档在运行"` 并以退出码 `2` 退出；抢占成功后将前次残留的 `status='running'` 行标为 `status='abandoned'`。进程被中断后直接重跑同一 `backfill` 命令即可跳过已入库片段；若宿主机导出的 `.jsonl` 已留在 `var/dalton/conversations/inbox/`，可追加 `--skip-host-export` 跳过重复导出。

## 4. 备份与恢复

完整备份配置与首次 `rclone` 授权步骤见 [scripts/dalton/ops/README.md](../../scripts/dalton/ops/README.md)。生产关键规则如下：

1. **三层异地容灾**：
   - `dalton_config_git_backup.sh`：每 6 小时备份小体积高变化配置（`.env` 等经 `~/.config/dalton/backup.key` AES 加密为 `.enc` 后提交私人 Git 仓库）。
   - `dalton_db_offsite.sh`：每日 03:40 将最新的完整 `dalton-pg-*.dump` 流式加密上传至 Google Drive `DaltonBackup/db/<日期>/`（先写 `.partial-<随机>` 并校验大小与 MD5 再原子 `moveto`；默认保留 7 份）。
   - `dalton_data_backup.sh`：每周日 05:00 流式加密上传 MinIO 卷、沙箱 `/home/workspace`、Redis 快照与宿主机 `var/dalton`（保留 4 周）。对未排除的 SQLite 文件先经外置盘 `/Volumes/EveSSD` 生成在线一致副本后再归档，不写宿主机内置盘临时目录。
2. **必须外部保管密钥**：`~/.config/dalton/backup.key`（默认对应 `DALTON_BACKUP_KEY_FILE`，权限 `0600`）**绝不会进入 GitHub 或 Google Drive**，务必存入团队密码管理器。
3. **恢复步骤（按实际备份内容逐项执行）**：
   - **解密参数**：`dalton_config_git_backup.sh`、`dalton_db_offsite.sh` 与 `dalton_data_backup.sh` 统一采用 `openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}"` 加密；解密命令参数为：
     ```bash
     openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" -in <加密文件> -out <目标文件>
     ```
     （或将远端流通过管道传入 `openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}"` 直接解包；`rclone` 拉取时建议使用 `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY` 绕过本地代理）。
   - **恢复前先停写入**：
     1. 停止/摘除宿主机写入 `var/dalton`、MinIO、Redis 与 Postgres 的 `launchd` 服务：`com.dalton.monitor-feeds`、`com.dalton.conversation-archive`、`com.dalton.researchdata-bridge`、`com.dalton.channel-gateway`（以及避免并发冲突的备份 `launchd` 任务）。
     2. 停止应用与边缘容器：`docker compose -f docker-compose.yml -f docker-compose.dalton.yml stop backend crawler edge frontend`；若宿主机有运行中的沙箱容器（`${DALTON_SANDBOX_CONTAINER_PREFIX}`，默认 `langalpha-sandbox-`），先停止相关写入进程或容器。
     3. 停止数据任务容器：调度器随后端退出只会取消协程，已启动的 `dalton-dt-*` 容器仍会继续写入 `var/dalton/openclaw-data`。用 `docker ps --filter name=dalton-dt- -q | xargs -r docker stop` 停掉，并再次执行 `docker ps --filter name=dalton-dt-` 确认没有残留后再恢复数据。
   - **按以下顺序恢复各备份项**：
     1. **密钥与配置备份（`DALTON_CONFIG_BACKUP_REPO_DIR`，默认 `~/Backups/dalton-config-repo`）**：从密码管理器还原 `DALTON_BACKUP_KEY_FILE`（`~/.config/dalton/backup.key`，`chmod 600`）；拉取私有配置 Git 仓库并参考 `.backup-enc-manifest.json`，使用上述 `openssl enc -d` 参数解密 `prod_overrides/`（`.env`、`agent_config.dalton*.yaml`、`config.dalton*.yaml` 等）、`dot_config_dalton/`（`~/.config/dalton/` 下的 `.env` 等配置）、`var_dalton/`（轻量配置），以及 `launch_agents/` 下的 `com.dalton.*.plist`（明文）与 `com.dalton.*.plist.enc`（含凭据的 plist，例如写入了 `INTERNAL_SERVICE_TOKEN` 的监控面板采集服务，备份时自动加密；按 `.backup-enc-manifest.json` 解密）、`backup_scripts/`，分别恢复至 `DALTON_PROD_CHECKOUT_DIR`、`DALTON_USER_CONFIG_DIR`、`DALTON_PROD_VAR_DIR` 与 `DALTON_LAUNCHAGENTS_DIR`，敏感 `.env` 与 plist 保持 `0600`。
     2. **PostgreSQL（`gdrive:DaltonBackup/db/<YYYY-MM-DD>/dalton-pg-*.dump.enc`）**：仅先启动 `postgres`（`docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra up -d postgres`）；从 Google Drive 拉取目标日期最新的 `dalton-pg-*.dump.enc`，用 `openssl enc -d` 解密为 `.dump` 后通过 `pg_restore` 恢复至目标库。
     3. **Redis 快照（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/redis-<YYYY-MM-DD>.rdb.enc`）**：确保 `DALTON_REDIS_CONTAINER`（默认 `dalton-langalpha-redis-1`）处于停止状态；解密 `redis-*.rdb.enc` 得到 RDB 文件并写回 `langalpha-redis-data` 卷的 `/data/dump.rdb`（若启用 AOF 需先清理同目录遗留 `appendonlydir` 以免覆盖 RDB 加载），随后启动 `redis`（检查 `REDIS_PASSWORD` 与 `.env` 一致）。
     4. **MinIO 对象存储（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/minio-<YYYY-MM-DD>.tar.gz.enc`）**：确保 `minio` 容器停止；通过辅助镜像（`DALTON_DOCKER_HELPER_IMAGE`，默认 `alpine:3.20`）挂载 `DALTON_MINIO_VOLUME`（默认 `dalton-langalpha_dalton-minio-data`）至 `/data`，将 `minio-*.tar.gz.enc` 解密并 `tar xzf - -C /data` 恢复桶与对象（如同时恢复了数据库，建议选择与 Postgres 时间点一致的周包），然后通过 `--profile storage up -d minio` 启动。
     5. **宿主机 `var/dalton`（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/var-dalton-<YYYY-MM-DD>.tar.gz.enc`）**：清空或移走旧的 `DALTON_PROD_VAR_DIR`（默认 `var/dalton`）后，解密并 `tar xzf - -C <PROD_VAR_DIR>` 解压恢复（该包已由 `backup_lib.py:snapshot-sqlite` 合入在线一致的 SQLite 库副本并排除 `-wal`/`-shm`/`-journal` 及 `DALTON_DATA_VAR_EXCLUDES` 中可重建目录；若目录内存在 `DuckDB`/`neo4j`，其一致性依赖备份窗口无写入）。
     6. **沙箱工作区（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/sandbox-<容器名>-<YYYY-MM-DD>.tar.gz.enc`）**：若目标 `langalpha-sandbox-*` 容器仍在宿主机且需还原 `/home/workspace`，可将对应 `.tar.gz.enc` 解密后在容器停止或无写入时 `docker cp` 解压回 `/home/workspace`（注意超过 `DALTON_SANDBOX_MAX_SIZE_GB` 默认 `5` GiB 的沙箱在备份时会被跳过；常规工作区与聊天附件亦可通过已恢复的 Postgres/MinIO 重放冷重建）。
     7. **启动应用与宿主机服务**：启动 `backend`、`crawler`、`edge`（确认 `docker_entrypoint_dalton.sh` 双链 Alembic 版本一致），最后重新加载并启动第 1 步停掉的宿主机 `launchd` 服务。
   - **恢复后检查**：
     1. 双链迁移版本校验：在 `backend` 容器内确认 `uv run alembic current` 与 `uv run alembic -c src/dalton/alembic.ini current` 均处于 head。
     2. 健康与端到端检查：依次执行 `GET /health`、带认证的 `GET /api/v1/dalton/openclaw/health`、`bash scripts/dalton/check_bridge.sh`、`bash scripts/dalton/check_public_edge.sh`。
     3. 持久数据校验：对 `var/dalton` 下关键 SQLite 库执行 `sqlite3 <数据库路径> "PRAGMA integrity_check;"`，并在 MinIO / 应用内抽检近期音频转写与工作区聊天附件可读。
   - **SQLite `-wal` 告警处置**：遇到 SQLite `-wal` 残留且目录不可写导致备份脚本告警时，确认无写入进程后执行一次 `sqlite3 <数据库路径> "PRAGMA wal_checkpoint(TRUNCATE);"`。

## 5. 公开入口与网络边界

Dalton 宿主机对外服务**严格通过 Tailscale Funnel `:8443` -> Caddy `edge`（`127.0.0.1:8088`）** 暴露（细节见 [dev-guide/PUBLIC-DEPLOY.md](./dev-guide/PUBLIC-DEPLOY.md)）：

- **严禁关闭 Tailscale Funnel `:8443 -> 127.0.0.1:8088` 映射**：这是 Dalton 对外唯一入口；同时严禁改动宿主机上原有 `:443` 或 `:10000` 等既有服务映射。
- **校验公网入口健康**：

```bash
bash scripts/dalton/check_public_edge.sh
# 需要测试 WebSocket 鉴权通道时传入有效用户 JWT：
# DALTON_WS_TEST_TOKEN="<jwt>" bash scripts/dalton/check_public_edge.sh
```

- 每次改写前端构建或重启 `edge` 容器后运行一次 `check_public_edge.sh`，确认仅允许回环与 Caddy 反代路径，且 `/`、`/version.json`、`/assets/` 与 `/ws/v1/market-data/status` 正常返回，`/health`、`/docs`、`/openapi.json` 等边缘屏蔽路径返回 `404`，未鉴权或伪造服务头的 `/api/v1/users/me` 返回 `401`。

## 6. 健康检查清单

```bash
# 1. 后端健康与部署指纹检查（后者需 CurrentUserId 鉴权，可用 Bearer JWT 或 X-Service-Token + X-User-Id）
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H "X-Service-Token: ${INTERNAL_SERVICE_TOKEN}" -H "X-User-Id: <user_id>" \
  http://127.0.0.1:8000/api/v1/dalton/openclaw/health

# 2. 宿主机 researchdata 桥接与声明来源校验
bash scripts/dalton/check_bridge.sh

# 3. 公网入口与 Caddy 静态托管校验
bash scripts/dalton/check_public_edge.sh

# 4. 可选：Web Search 桥接健康检查（未启用可略过）
bash scripts/dalton/check_web_search_bridge.sh

# 5. AlphaEngine 搜索缓存命中与额度日报
python3 scripts/dalton/alphaengine_usage_report.py
```

## 7. 常见故障与处理

| 现象 | 常见原因 | 处理方法 |
|---|---|---|
| 改了 `.env` 后后端行为未变 / 新变量未生效 | 只用了 `restart` 而非重建容器 | 执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --force-recreate backend` |
| `/api/v1/dalton/openclaw/health` 提示镜像或清单漂移 | 容器运行代码或配置与镜像构建指纹不一致 | 执行 `bash scripts/dalton/docker_build_backend.sh` 重新打包构建 |
| 前端打开空白页或旧 UI / 登录异常 | 未重跑生产构建或漏了 `--require-supabase` | 执行 `bash scripts/dalton/build_frontend_prod.sh --require-supabase` 并检查 `web/dist` 权限及 `edge` 状态 |
| 飞书文档授权或通知报 401/启动退出 | Vault 凭据缺失或改后未重启网关 | 在设置页确认 `DALTON_FEISHU_APP_ID` / `DALTON_FEISHU_APP_SECRET` 已保存后，重启 `com.dalton.channel-gateway` |
| A 股盘中监测分时或 ETF 板块偶发延迟/502 | 东方财富全市场扫描触发上游 IP 限流 | 确认使用限流后默认参数（`ETF_SCAN_TTL_TRADING=120`、`EASTMONEY_PAGE_WORKERS=2`）并关闭任何残留旧脚本；检查 `com.dalton.monitor-feeds` 状态 |
| 语音转写或大文件上传报存储未配置 | 启动 Compose 时漏加 `--profile storage`（或漏加 `--profile infra`）或 MinIO 未初始化 | 使用 `--profile infra --profile storage up -d`（使用外部 DB/Redis 时 `--profile storage up -d minio`）启动 `minio` 并按 `AUDIO-STORAGE.md` 核验 `DALTON_ASR_STORAGE_*` 变量 |
| 外置盘未挂载导致夜间备份失败 | `/Volumes/EveSSD` 掉线，脚本按规定拒绝回退内置盘 | 恢复外置盘挂载后手动补跑 `bash scripts/dalton/ops/dalton_db_offsite.sh` |
| 监控面板点击“发布/更新”返回 403 `artifact_publishing_disabled` | 当前环境未启用发布总开关 | 检查 `.env` 中 `DALTON_ARTIFACT_PUBLISHING_ENABLED=true` 并重建 `backend`（见 [monitors.md](./features/monitors.md)） |
