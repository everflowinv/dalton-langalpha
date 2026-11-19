# Dalton 运维手册

本文提供 Dalton 生产与准生产环境的构建部署、环境变量生效规则、宿主机 `launchd` 服务运维、异地备份恢复、Tailscale Funnel 对外入口管理及常见故障处置；正文仅列环境变量名，所有容器端口统一绑定在 `127.0.0.1`。

## 1. 部署与构建（仓库现有脚本）

Dalton 用上游编排叠加 `docker-compose.dalton.yml`，配合专用后端构建和前端生产构建脚本：

### 1.1 后端重建与启动

每次改过 `src/`、`scripts/`、`plugins/`、`agent_config.dalton.yaml`、`config.dalton.yaml`、`config/dalton/` 或 `migrations/` 后，运行：

```bash
bash scripts/dalton/docker_build_backend.sh
```

- `scripts/dalton/docker_build_backend.sh` 会自动注入当前 `BUILD_GIT_SHA` 和 `BUILD_GIT_DIRTY`，并跑 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --build backend`。只想打镜像不重启时加上 `--build-only`。
- 容器启动脚本 `scripts/dalton/docker_entrypoint_dalton.sh` 会依次跑上游和 Dalton 双链路迁移：
  1. `uv run alembic upgrade head`
  2. `uv run alembic -c src/dalton/alembic.ini upgrade head`

### 1.2 全栈与可选服务启动

```bash
# 启动基础全栈（启用 infra profile 以包含 postgres, redis，再叠加 backend, crawler, frontend, edge）
docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra up -d

# 启用语音转写/大文件对象存储 profile 时追加 --profile storage
docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra --profile storage up -d
```

- **Profile 说明**：`docker-compose.yml` 把 `postgres` 和 `redis` 放进了 `profiles: ["infra"]`。如果 `.env` 里已经设了 `COMPOSE_PROFILES=infra`，或者部署连的是外部 PostgreSQL 和 Redis，可以省掉 `--profile infra`。外部 PostgreSQL 在 `.env` 配 `DB_HOST` 就行；外部 Redis 除了改 `.env` 的 `REDIS_URL`，还得再加一个 compose 覆盖文件改写 `docker-compose.dalton.yml` 里 `backend` 写死的 `redis://…@redis:6379/0`。用本地容器库且 `.env` 没配 `COMPOSE_PROFILES=infra` 时，必须显式传 `--profile infra`，数据库和 Redis 才会跟着拉起来。
- **镜像注意事项**：`docker-compose.dalton.yml` 把 `cli` 服务放进 `profiles: ["cli-disabled"]` 让它默认关闭；`edge`（Caddy）挂载 `./web/dist:/srv:ro` 和 `./deploy/dalton/Caddyfile`；`crawler` 跑在隔离网络 `crawl`。

### 1.3 前端生产构建

Caddy `edge` 容器直接静态托管 `./web/dist`。每次发布前端代码或调过品牌资源后，都要在宿主机重跑生产构建并过门禁：

```bash
bash scripts/dalton/build_frontend_prod.sh --require-supabase
```

- 这个脚本先读 `web/.env.dalton` 当 `VITE_*` 默认值，再用仓库 `.env` 里的 `VITE_*`（以及 `DALTON_AUTH_MODE`、`SUPABASE_URL`、`SUPABASE_PUBLISHABLE_KEY` 回退）覆盖并导出环境变量，接着进 `web/` 跑 `pnpm build`（脚本没设 `APP_ENV=dalton`），最后强制检查首屏预算（`web/scripts/check-critical-path.mjs`）和认证配置完整性（`--require-supabase` 防止打出没带 Supabase 认证的裸包）。
- 默认构建入口已经带了 `web/.env.dalton`（`VITE_BRAND_NAME=Everflow`、`VITE_BRAND_MARK=everflow`）。

### 1.4 生产部署

生产发布通过 `scripts/dalton/ops/prod/deploy_main.sh` 执行：

```bash
bash scripts/dalton/ops/prod/deploy_main.sh
# 变更了 docker-compose*.yml 容器配置（如 mem_limit、环境变量注入或挂载）时：
bash scripts/dalton/ops/prod/deploy_main.sh --recreate
# 同步改动了 researchdata_bridge.py 或其依赖时：
bash scripts/dalton/ops/prod/deploy_main.sh --bridge
# 包含 Dalton Alembic 迁移时校对目标版本号：
bash scripts/dalton/ops/prod/deploy_main.sh --expect-migration d039
```

- **发布流程**：脚本先调用 `wait_backend_idle.sh` 连续两次确认没有进行中的会话或子代理任务，随后触发 `com.dalton.pg-backup` 完成一次新的 Postgres 备份并核对备份日志；备份完成后记录 `PRE_FF` 提交并快进合并 `origin/dalton/main`，接着同步宿主机 `.venv`（`uv sync --frozen`）与前端依赖（`pnpm install --frozen-lockfile`）并执行 `scripts/dalton/build_frontend_prod.sh --require-supabase`；重启前再次确认后端空闲，然后重建或重启 `backend` 与 `crawler`（以及有配置变更时的 `edge`），最后依次检查 `/health`、Dalton 迁移版本、`crawler:8100/health` 与 `scripts/dalton/check_public_edge.sh`。
- **常用参数**：
  - `--recreate`：在 `docker-compose.yml` 或 `docker-compose.dalton.yml` 中调整了容器编排字段（如资源上限、环境变量、挂载路径）时传入，使用 `docker compose up -d --no-deps` 重建 `backend` 与 `crawler` 使配置生效；日常纯代码发布保持默认 `docker restart` 即可。
  - `--bridge`：本次发布包含 `scripts/dalton/researchdata_bridge.py` 或宿主机桥接逻辑改动时传入，在健康检查后重启 `com.dalton.researchdata-bridge` 并输出服务状态。
  - `--expect-migration dNNN`：本次发布附带 Dalton 数据库迁移时传入，用于核对 `alembic -c src/dalton/alembic.ini current` 输出的迁移版本前缀。
- **失败回滚**：脚本在快进合并前记录 `PRE_FF`。若 `pnpm install`、前端生产构建、重启前二次空闲等待失败，或容器重启/重建（`backend`、`crawler`、`edge`）、后端 `/health`、Dalton `crawler:8100/health`、`edge` 就绪探测及 `check_public_edge.sh` 任一步失败，都会调用 `revert_deploy` 将代码复位到 `PRE_FF`、重新同步 `.venv` 与 `web/node_modules` 并重打旧版前端静态包；若已触动容器，还会按需重建/重启 `backend`、`crawler` 与 `edge`，并重跑健康检查报告回滚后状态，最终以非零状态码退出。
- **边缘代理（`edge`）重载**：快进合并后通过 `deploy/dalton/Caddyfile` 差异及 `docker compose config` 解析后的 `edge` 服务配置对比，检测到任一变化时会在健康检查前执行 `docker compose up -d --no-deps --force-recreate edge`，随后以 60 秒统一截止时间循环探测 `http://127.0.0.1:8088/`（端口由 `DALTON_EDGE_PORT` 控制，要求 `curl` 成功且返回 `200`）；超时直接触发回滚并以非零退出，就绪后再运行 `check_public_edge.sh`。
- **配套宿主机脚本与 `launchd` 模板**：`scripts/dalton/ops/prod/` 下同时收拢了 `wait_backend_idle.sh`、`backup.sh`、`watchdog.sh`、`colima_launch.sh` 以及 `launchd/` 下的三份模板（`com.dalton.backend-watchdog.plist.template`、`com.dalton.colima.plist.template`、`com.dalton.pg-backup.plist.template`）。

## 2. `.env` 与叠加配置文件改动生效规则

- **改完 `.env` 必须重建容器**：Docker Compose 只在创建容器时读 `.env`，跑 `docker compose restart backend` 不会重新注入新变量！只要改了 `INTERNAL_SERVICE_TOKEN`、`DALTON_ALLOWED_EMAILS`、`DALTON_PM_USER_IDS`、`DALTON_ARTIFACT_PUBLISHING_ENABLED`、`GEMINI_API_KEY`、`XAI_API_KEY` 等 `.env` 变量，就必须跑：

```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --force-recreate backend
```

  如果 `crawler` 或 `edge` 依赖的变量（比如 `INTERNAL_SERVICE_TOKEN`）也改了，对应服务也要一起 `--force-recreate`。
- **修改 `agent_config.dalton.yaml` / `config.dalton.yaml` / `src/dalton/dalton.yaml`**：虽然宿主机目录挂进了 `backend` 容器（`./agent_config.dalton.yaml`、`./config.dalton.yaml`、`./config/dalton` 等），但后端镜像启动时会核对 `/app/.build_files.sha256` 清单指纹，所以**生产改动要统一走 `scripts/dalton/docker_build_backend.sh` 重建镜像和容器**。

## 3. 宿主机 `launchd` 服务运维

### 3.1 服务安装与常用管理命令

| 服务 Label | 安装/模板来源 | 重启命令 | 日志 / 状态位置 |
|---|---|---|---|
| `com.dalton.researchdata-bridge` | `bash scripts/dalton/install_bridge.sh` | `launchctl kickstart -k gui/$(id -u)/com.dalton.researchdata-bridge` | 模板内指定宿主机 Logs（见 `install_bridge.sh`） |
| `com.dalton.channel-gateway` | `scripts/dalton/launchd/com.dalton.channel-gateway.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway` | `~/.config/dalton/channel-gateway.env` (`0600`) 与 gateway 日志 |
| `com.dalton.monitor-feeds` | `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` | `launchctl kickstart -k gui/$(id -u)/com.dalton.monitor-feeds` | `var/dalton/monitor-feeds/` 状态文件与标准日志 |
| `com.dalton.config-git-backup` | `scripts/dalton/ops/launchd/com.dalton.config-git-backup.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.config-git-backup` | `~/Library/Logs/dalton-offsite-backup.log` |
| `com.dalton.db-offsite` | `scripts/dalton/ops/launchd/com.dalton.db-offsite.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.db-offsite` | `~/Library/Logs/dalton-offsite-backup.log` & `*-alerts.log` |
| `com.dalton.data-backup` | `scripts/dalton/ops/launchd/com.dalton.data-backup.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.data-backup` | `~/Library/Logs/dalton-offsite-backup.log` & `*-alerts.log` |
| `com.dalton.backend-watchdog` | `scripts/dalton/ops/prod/launchd/com.dalton.backend-watchdog.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.backend-watchdog` | `~/Backups/dalton/watchdog.log` |
| `com.dalton.colima` | `scripts/dalton/ops/prod/launchd/com.dalton.colima.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.colima` | `~/Library/Logs/dalton-colima.log` |
| `com.dalton.pg-backup` | `scripts/dalton/ops/prod/launchd/com.dalton.pg-backup.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.pg-backup` | `~/Library/Logs/dalton-pg-backup.log` |
| `com.dalton.portwatch` | `scripts/dalton/ops/prod/launchd/com.dalton.portwatch.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.portwatch` | `~/Library/Logs/dalton-portwatch/portwatch.jsonl` |
| `com.dalton.alphaengine-mcp` | `scripts/dalton/launchd/com.dalton.alphaengine-mcp.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.alphaengine-mcp` | `~/Library/Logs/dalton-alphaengine-mcp.{log,err.log}` |
| `com.dalton.alphaengine-desktop-watchdog` | `scripts/dalton/launchd/com.dalton.alphaengine-desktop-watchdog.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.alphaengine-desktop-watchdog` | `~/Library/Logs/dalton-alphaengine-desktop-watchdog.{log,err.log}` |
| `com.dalton.guidepoint-mcp-proxy` | `scripts/dalton/launchd/com.dalton.guidepoint-mcp-proxy.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.guidepoint-mcp-proxy` | `~/Library/Logs/dalton-guidepoint-mcp-proxy.{log,err.log}` |
| `com.dalton.usage-collector` | `scripts/dalton/launchd/com.dalton.usage-collector.plist.template` | `launchctl kickstart -k gui/$(id -u)/com.dalton.usage-collector` | `~/Library/Logs/dalton-usage-collector.{log,err.log}` 与 `~/Library/Application Support/Everflow/usage-accounting/collector-status.json` |

查看某项服务实时状态：

```bash
launchctl print gui/$(id -u)/com.dalton.researchdata-bridge | head -n 40
```

> 安全提醒：改了 Vault 里的飞书应用凭据（`DALTON_FEISHU_APP_ID` / `DALTON_FEISHU_APP_SECRET`）后，网关不会热替换内存凭据。确认没有正在发的重要推送后，用 `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway` 重启刷新内存。

### 3.2 研究数据常驻服务

宿主机上有三项研究数据常驻服务由 Dalton 的 `launchd` 模板托管，端口继续保持本机回环地址，供 `researchdata_bridge.py` 与研究运行时脚本直连：

| 服务与作用 | launchd 标签 | 端口 / 周期 | 日志与状态位置 |
|---|---|---|---|
| **AlphaEngine MCP**：运行 `server.mjs`（`/opt/homebrew/bin/node`），复用宿主机 AlphaEngine Desktop 登录态提供研报与会议纪要查询 | `com.dalton.alphaengine-mcp` | `127.0.0.1:8950`（KeepAlive） | `~/Library/Logs/dalton-alphaengine-mcp.log`、`~/Library/Logs/dalton-alphaengine-mcp.err.log` |
| **AlphaEngine 桌面端看门狗**：运行 `desktop-watchdog.sh`，检测桌面端存活与 MCP 鉴权状态；应用未启动时拉起，登录失效或模块页未打开时通过频道网关 `/internal/ops-alerts`（失败时退回 macOS 通知）提醒 | `com.dalton.alphaengine-desktop-watchdog` | 每 `120` 秒（StartInterval） | `~/Library/Logs/dalton-alphaengine-desktop-watchdog.log`、`~/Library/Logs/dalton-alphaengine-desktop-watchdog.err.log`；状态位于 `$DALTON_RESEARCH_HOME/state/alphaengine-desktop-watchdog.json` |
| **Guidepoint MCP 代理**：运行 `guidepoint_mcp_proxy.py`（`/opt/homebrew/bin/python3.12`），自动刷新短期 OAuth 访问令牌并转发 MCP 请求 | `com.dalton.guidepoint-mcp-proxy` | `127.0.0.1:8943`（KeepAlive） | `~/Library/Logs/dalton-guidepoint-mcp-proxy.log`、`~/Library/Logs/dalton-guidepoint-mcp-proxy.err.log` |

- **唯一运行模式与配置同步**：`researchdata_bridge.py`、`install_bridge.sh` 与三项宿主机常驻服务统一采用 `DALTON_RESEARCH_CODE`（代码与虚拟环境目录）+ `DALTON_RESEARCH_HOME`（运行时数据与配置目录）双目录模式；`host_services_switch.sh` 渲染 plist 时统一注入 `DALTON_RESEARCH_CODE` 与 `DALTON_RESEARCH_HOME`：
  ```bash
  # 1. 只读查看模式、服务数据路径与标签当前状态
  bash scripts/dalton/ops/host_services_switch.sh --plan

  # 2. 按顺序加载并切换到 com.dalton.* 服务并完成健康与 PID 校验
  bash scripts/dalton/ops/host_services_switch.sh --apply

  # 3. 仅重新渲染并重启已加载的 com.dalton.* 服务（适合切换目录后同步路径）
  bash scripts/dalton/ops/host_services_switch.sh --apply --rerender-only
  ```
  也可执行 `.venv/bin/python scripts/dalton/researchdata_bridge.py --check-runtime`，在输出 JSON 的 `host_services` 字段中查看 `8950`、`8943` 端口连通性与当前生效标签。

- **Guidepoint 令牌失效后重新授权**：
  ```bash
  /opt/homebrew/bin/python3.12 scripts/dalton/research_runtime/scripts/guidepoint_mcp_oauth.py
  ```
  脚本会启动 `http://localhost:8942/callback` 并打开浏览器完成 OAuth 授权，默认从环境变量或 `$DALTON_RESEARCH_HOME/config/guidepoint_mcp.env`（可用 `GUIDEPOINT_ENV_FILE` 覆盖）读取客户端凭据，授权完成后把新令牌以 `0600` 权限原子写入 `$DALTON_RESEARCH_HOME/config/guidepoint_mcp_tokens.json`（默认对应 `~/Library/Application Support/Everflow/research/config/guidepoint_mcp_tokens.json`，可用 `GUIDEPOINT_TOKENS_FILE` 覆盖）。更新令牌后执行 `launchctl kickstart -k gui/$(id -u)/com.dalton.guidepoint-mcp-proxy` 让代理立即生效。

- **AlphaEngine 删除废弃账号**：
  AlphaEngine MCP 会把在 Desktop App 中登录过的账号按 `user_id` 保存在 macOS 钥匙串（service 为 `dalton-alphaengine`），并在 `~/Library/Caches/dalton-researchdata-bridge/alphaengine_accounts_state.json`（`0600`，不含令牌）记录额度与状态元数据；过期超过 7 天后会自动停止每日登录提醒。如需彻底移除已弃用的账号：先在桌面 App 中退出该账号（否则 MCP 重启后会从 App 重新读到它的令牌），再停止 AlphaEngine MCP（运行中的进程会把内存中的账号记录写回状态文件），然后删除钥匙串条目、从状态文件 `accounts` 中移除对应 `user_id`，最后重新启动（安装时用 `DALTON_LAUNCH_AGENTS_DIR` 指定过 plist 目录的，把下面的路径换成该目录）：
  ```bash
  launchctl bootout gui/$(id -u)/com.dalton.alphaengine-mcp
  security delete-generic-password -s dalton-alphaengine -a <user_id>
  # 编辑 ~/Library/Caches/dalton-researchdata-bridge/alphaengine_accounts_state.json，删除 accounts 中的 <user_id>
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.alphaengine-mcp.plist
  ```

### 3.3 token 用量采集

- **作用**：`scripts/dalton/ops/usage_collector_run.sh` 选用技能自带 venv 或 `/opt/homebrew/bin/python3.12` 启动 `scripts/dalton/ops/usage_collector.py`，每 60 秒调用 Dalton 技能 `scripts/dalton/data_assets/skills/model-usage-cost` 的 `collect-broker --days 2 --json` 子命令（超时 120 秒，通过 `collector.lock` 互斥锁跳过重叠调度），把 Grok CLI 等易轮转的本地日志及时写入账本，供每周六 12:00 HKT 的宿主机数据任务 `T61`（`weekly-token-usage`）汇总。
- **标签**：`com.dalton.usage-collector`（模板位于 `scripts/dalton/launchd/com.dalton.usage-collector.plist.template`，调度参数为 `StartInterval = 60`、`Nice = 10`）。
- **指针文件与账本位置**：
  - 活跃账本指针文件位于 `~/.config/dalton/usage-state-dir`（权限 `0600`，内容为当前活跃账本目录的绝对路径）。采集器、技能 CLI 与周报脚本优先读取 `DALTON_USAGE_STATE_DIR` 环境变量或 `~/.config/dalton/usage-state-dir` 指针。
  - 当前账本默认位于 `~/Library/Application Support/Everflow/usage-accounting`（写入 plist 的 `DALTON_USAGE_STATE_DIR` 与指针文件），目录下包含：
    - `evidence.sqlite`（权限 `0600`，本地技术计量账本）
    - `collector-status.json`（权限 `0600`，原子写入 `ok`、`collected_at`、`last_ok_at`、`consecutive_failures` 等状态）
    - `collector.lock`（采集互斥锁文件）
- **部署与刷新命令**：
  ```bash
  # 1. 校验采集器 Python，预览指针、账本大小与标签状态
  bash scripts/dalton/ops/usage_collector_switch.sh --plan

  # 2. 写入指针、加载 com.dalton.usage-collector 并等待首次 ok:true（最多 300 秒）
  bash scripts/dalton/ops/usage_collector_switch.sh --apply
  # 若目标目录已存在 evidence.sqlite 且需以指定源覆盖（原文件将重命名为 evidence.sqlite.pre-apply.<时间戳> 保留）：
  bash scripts/dalton/ops/usage_collector_switch.sh --apply --force
  ```
- **排查位置**：
  - `launchd` 服务状态：`launchctl print gui/$(id -u)/com.dalton.usage-collector`
  - 当前指针指向：`cat ~/.config/dalton/usage-state-dir`
  - 采集状态文件：当前活跃账本目录下的 `collector-status.json`（查看 `ok`、`last_ok_at`、`consecutive_failures` 与 `error`）
  - 采集日志：`~/Library/Logs/dalton-usage-collector.log` 与 `~/Library/Logs/dalton-usage-collector.err.log`
  - 手动单次执行验证（显式指向临时目录，保持生产账本目录干净）：
    ```bash
    DALTON_USAGE_STATE_DIR="$(mktemp -d)" bash scripts/dalton/ops/usage_collector_run.sh
    ```

### 3.4 生产模型切换脚本（`switch_model_refs.py`）

升级默认模型后，用 `scripts/dalton/ops/switch_model_refs.py` 批量迁移 `automations.llm_model`（仅 `deleted_at IS NULL`）、`conversation_threads.llm_model` 及 `user_preferences.model_preference` 中的系统模型引用：

```bash
# 1. 必须先执行计划模式（只读，不写库；列出受影响行、指令原文片段及建议改法）
.venv/bin/python scripts/dalton/ops/switch_model_refs.py \
  --from gpt-6.1-sol-oauth --to gpt-6.2-sol-oauth \
  --from gpt-6-astra-oauth --to gpt-6.2-astra-oauth

# 2. 确认计划后加 --apply 执行；计划里含公司自动化（scope='company'）时必须传 --actor-user-id
.venv/bin/python scripts/dalton/ops/switch_model_refs.py \
  --from gpt-6.1-sol-oauth --to gpt-6.2-sol-oauth \
  --from gpt-6-astra-oauth --to gpt-6.2-astra-oauth \
  --actor-user-id <内部成员或管理员 user_id> \
  --apply
```

- **先计划后写入**：默认不加 `--apply` 为只读计划模式；写入前会校验 `scope='company'` 是否提供了合法的 `--actor-user-id`（经 `is_internal_or_admin` 校验为内部成员或管理员），缺失或无权限会在写入任何数据前报错退出。
- **归档文件位置**：执行 `--apply` 前把原始值以 `0600` 权限写入 `var/dalton/operator-records/model-switch-<UTC时间戳>.json`，并在偏好 CAS 写入完成后同步记录实际被替换的原值与跳过的冲突用户。
- **范围边界**：用户自定义模型（`model_preference.custom_models`）**不在切换范围内**，脚本不会改动；`automations.instruction` 文本中的旧模型名只列出原文片段与建议改法，**不会自动改写**。
- **并发与中断恢复**：线程只更新计划内的 `thread_id`（事务内核对行数不符即回滚）；用户偏好在事务内加锁重读并通过 CAS 保护，冲突时跳过该用户并报告；任一阶段失败会打印已完成与未完成进度摘要并以非零退出码退出，修复后可直接重跑（幂等，已切换行不会再次进入计划）。

### 3.6 加入或切换模型的检查清单

每次上线新模型或切换默认模型时，按以下六步逐项核验（命令中的 `<新模型key>`、`<旧模型key>`、`<提供方>`、`<管理员user_id>` 与 `<抽查用户user_id>` 按实际目标替换）：

1. **新模型配置写入 Dalton overlay，上游 manifest 保持原样**：
   对于待正式发布或待订阅开放的新模型，先在 `dalton_meta` 中添加 `"awaiting_release": true`，并把模型设为 `"visible": false`（订阅开放前选择器不显示它）；收到 T87 的 `model_released` 告警后核对实际档位，再设为可见并去掉该标记。模型定义放在 `src/dalton/llm/native_overlay.json`（或运行时同步文件 `var/dalton/catalog/native_sync_overlay.json`），上游 `src/llms/manifest/models.json` 与 `src/llms/manifest/providers.json` 保持只读原样。核对新模型已进入叠加配置并顺利通过钩子校验：

   ```bash
   .venv/bin/python -c 'import json; d=json.load(open("src/dalton/llm/native_overlay.json", encoding="utf-8")); print("<新模型key> in overlay:", "<新模型key>" in d.get("models", {}))'
   bash scripts/dalton/check_hooks.sh --strict --audit
   ```

2. **部署后对每个订阅调用 `available-models`（`refresh=true`），确认新模型在 `ready` 中**：
   部署并重建后端后，用管理员凭据逐个刷新已连接订阅的实时列表，确认新模型落在 `ready` 列表中（当 `codex-oauth` 将新模型归入 `missing` 时，在 `.env` 中调高 `DALTON_CODEX_CLIENT_VERSION` 并用 `--force-recreate` 重建 `backend`）：

   ```bash
   for p in codex-oauth claude-oauth antigravity-oauth xai-oauth; do
     echo "=== ${p} ==="
     curl -fsS -H "X-Service-Token: ${INTERNAL_SERVICE_TOKEN}" -H "X-User-Id: <管理员user_id>" \
       "http://127.0.0.1:8000/api/v1/providers/${p}/available-models?refresh=true" \
       | python3 -c 'import json,sys; d=json.load(sys.stdin); print("status=", d.get("status"), "ready_has_target=", "<新模型key>" in (d.get("ready") or []), "missing=", d.get("missing"))'
   done
   ```

3. **确认 `/api/v1/models` 返回该模型**：
   检查公开目录接口已返回新模型元数据与可见状态：

   ```bash
   curl -fsS http://127.0.0.1:8000/api/v1/models \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); k="<新模型key>"; meta=d.get("model_metadata", {}).get(k); print("in_metadata=", bool(meta), "visible=", (meta or {}).get("visible"), "provider=", (meta or {}).get("provider"))'
   ```

4. **发起一次真实模型请求**：
   通过后端探针接口向新模型发送真实 `PONG` 请求，确认返回 `"ok": true`：

   ```bash
   curl -fsS -X POST \
     -H "Content-Type: application/json" \
     -H "X-Service-Token: ${INTERNAL_SERVICE_TOKEN}" \
     -H "X-User-Id: <管理员user_id>" \
     -d '{"model_key": "<新模型key>"}' \
     "http://127.0.0.1:8000/api/v1/dalton/providers/<提供方>/probe" \
     | python3 -m json.tool
   ```

5. **需要迁移旧模型引用时，先运行计划模式，确认后再执行 `--apply`**：
   先用只读计划模式预览待更新的自动化、线程与用户偏好（含收藏和隐藏列表），确认输出后加上 `--apply`（涉及公司自动化时附带 `--actor-user-id`）：

   ```bash
   .venv/bin/python scripts/dalton/ops/switch_model_refs.py \
     --from <旧模型key> --to <新模型key>

   .venv/bin/python scripts/dalton/ops/switch_model_refs.py \
     --from <旧模型key> --to <新模型key> \
     --actor-user-id <管理员user_id> \
     --apply
   ```

6. **抽查一位用户的模型选择器可见状态**：
   用普通成员的 `user_id` 查询其可用模型接口（确认新模型落在 `ready` 中），再登录前端页面打开模型选择器确认新模型展示正常：

   ```bash
   curl -fsS -H "X-Service-Token: ${INTERNAL_SERVICE_TOKEN}" -H "X-User-Id: <抽查用户user_id>" \
     "http://127.0.0.1:8000/api/v1/providers/<提供方>/available-models" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print("user_ready_has_target=", "<新模型key>" in (d.get("ready") or []), "status=", d.get("status"))'
   ```

## 4. 备份与恢复

完整备份配置和首次 `rclone` 授权步骤见 [scripts/dalton/ops/README.md](../../scripts/dalton/ops/README.md)。生产关键规则如下：

1. **三层异地容灾**：
   - `dalton_config_git_backup.sh`：每 6 小时备份体积小、改得勤的配置（`.env` 等用 `~/.config/dalton/backup.key` AES 加密成 `.enc` 后推到私人 Git 仓库）。
   - `dalton_db_offsite.sh`：每天 03:40 把最新的完整 `dalton-pg-*.dump` 流式加密传到 Google Drive `DaltonBackup/db/<日期>/`（先写 `.partial-<随机>` 并校对大小和 MD5，再原子 `moveto`；默认留 7 份）。
   - `dalton_data_backup.sh`：每周日 05:00 流式加密上传 MinIO 卷、沙箱 `/home/workspace`、Redis 快照和宿主机 `var/dalton`（留 4 周）。对没排除的 SQLite 文件，先在外置盘 `/Volumes/EveSSD` 上生成在线一致副本再打包，不写宿主机内置盘临时目录。
2. **密钥必须离线保管**：`~/.config/dalton/backup.key`（默认对应 `DALTON_BACKUP_KEY_FILE`，权限 `0600`）**绝不会进 GitHub 或 Google Drive**，一定要存进团队密码管理器。
3. **恢复步骤（按实际备份内容逐项来）**：
   - **解密参数**：`dalton_config_git_backup.sh`、`dalton_db_offsite.sh` 和 `dalton_data_backup.sh` 统一用 `openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}"` 加密。解密命令如下：
     ```bash
     openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" -in <加密文件> -out <目标文件>
     ```
     也可以把远端流直接管道喂给 `openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}"` 解包。用 `rclone` 拉取时，建议带上 `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY` 绕开本地代理。
   - **恢复前先停所有写入**：
     1. 停掉或摘掉宿主机上会写 `var/dalton`、MinIO、Redis 和 Postgres 的 `launchd` 服务：`com.dalton.monitor-feeds`、`com.dalton.researchdata-bridge`、`com.dalton.channel-gateway`，以及会冲突的备份 `launchd` 任务。
     2. 停掉应用和边缘容器：`docker compose -f docker-compose.yml -f docker-compose.dalton.yml stop backend crawler edge frontend`。宿主机如果还有跑着的沙箱容器（`${DALTON_SANDBOX_CONTAINER_PREFIX}`，默认 `langalpha-sandbox-`），先停掉里面的写入进程或容器。
     3. 停掉数据任务容器：调度器跟着后端退出只会取消协程，已经拉起的 `dalton-dt-*` 容器还会继续写 `var/dalton/openclaw-data`。执行 `docker ps --filter name=dalton-dt- -q | xargs -r docker stop` 停掉，再跑一次 `docker ps --filter name=dalton-dt-` 确认清干净，然后再动手恢复数据。
   - **按顺序恢复各备份项**：
     1. **密钥与配置备份（`DALTON_CONFIG_BACKUP_REPO_DIR`，默认 `~/Backups/dalton-config-repo`）**：先从密码管理器恢复 `DALTON_BACKUP_KEY_FILE`（`~/.config/dalton/backup.key`，`chmod 600`）。拉下私有配置 Git 仓库并对照 `.backup-enc-manifest.json`，用上面的 `openssl enc -d` 参数解出 `prod_overrides/`（`.env`、`agent_config.dalton*.yaml`、`config.dalton*.yaml` 等）、`dot_config_dalton/`（`~/.config/dalton/` 下的 `.env` 等配置）、`var_dalton/`（轻量配置），以及 `launch_agents/` 里的明文 `com.dalton.*.plist` 和加密的 `com.dalton.*.plist.enc`（含凭据的 plist，比如写了 `INTERNAL_SERVICE_TOKEN` 的监控面板采集服务，按 `.backup-enc-manifest.json` 解密）和 `backup_scripts/`。按目录分别放回 `DALTON_PROD_CHECKOUT_DIR`、`DALTON_USER_CONFIG_DIR`、`DALTON_PROD_VAR_DIR` 和 `DALTON_LAUNCHAGENTS_DIR`，敏感 `.env` 和 plist 保持 `0600`。
     2. **PostgreSQL（`gdrive:DaltonBackup/db/<YYYY-MM-DD>/dalton-pg-*.dump.enc`）**：先只启动 `postgres`（`docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra up -d postgres`）。从 Google Drive 拉取目标日期最新的 `dalton-pg-*.dump.enc`，用 `openssl enc -d` 解出 `.dump`，再用 `pg_restore` 灌进目标库。
     3. **Redis 快照（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/redis-<YYYY-MM-DD>.rdb.enc`）**：确认 `DALTON_REDIS_CONTAINER`（默认 `dalton-langalpha-redis-1`）处于停止状态。解密 `redis-*.rdb.enc` 拿到 RDB 文件，写回 `langalpha-redis-data` 卷的 `/data/dump.rdb`（开了 AOF 的话先清掉同目录下残留的 `appendonlydir`，免得盖过 RDB 加载），再启动 `redis` 并核对 `REDIS_PASSWORD` 和 `.env` 一致。
     4. **MinIO 对象存储（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/minio-<YYYY-MM-DD>.tar.gz.enc`）**：确认 `minio` 容器已停。用辅助镜像（`DALTON_DOCKER_HELPER_IMAGE`，默认 `alpine:3.20`）把 `DALTON_MINIO_VOLUME`（默认 `dalton-langalpha_dalton-minio-data`）挂到 `/data`，把 `minio-*.tar.gz.enc` 解密并用 `tar xzf - -C /data` 恢复桶和对象（同时恢复数据库时，挑跟 Postgres 时间点一致的周包），再用 `--profile storage up -d minio` 启动。
     5. **宿主机 `var/dalton`（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/var-dalton-<YYYY-MM-DD>.tar.gz.enc`）**：清空或移走旧的 `DALTON_PROD_VAR_DIR`（默认 `var/dalton`），解密后用 `tar xzf - -C <PROD_VAR_DIR>` 解压恢复。这个包里已经通过 `backup_lib.py:snapshot-sqlite` 合入了在线一致的 SQLite 副本，并排除了 `-wal`/`-shm`/`-journal` 以及 `DALTON_DATA_VAR_EXCLUDES` 里可重建的目录。如果里面有 `DuckDB` 或 `neo4j`，它们的一致性依赖备份窗口期间没有写入。
     6. **沙箱工作区（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/sandbox-<容器名>-<YYYY-MM-DD>.tar.gz.enc`）**：如果目标 `langalpha-sandbox-*` 容器还在宿主机上且需要还原 `/home/workspace`，可以把对应 `.tar.gz.enc` 解密后在容器停机或无写入时 `docker cp` 解压回 `/home/workspace`。超过 `DALTON_SANDBOX_MAX_SIZE_GB`（默认 `5` GiB）的沙箱在备份时会被跳过；常规工作区和聊天附件也能靠恢复好的 Postgres 和 MinIO 重放冷重建。
     7. **拉起应用与宿主机服务**：依次启动 `backend`、`crawler`、`edge`（确认 `docker_entrypoint_dalton.sh` 双链 Alembic 版本一致），最后重新加载并拉起第 1 步停掉的宿主机 `launchd` 服务。
   - **恢复后核查**：
     1. 双链迁移版本校验：进 `backend` 容器确认 `uv run alembic current` 和 `uv run alembic -c src/dalton/alembic.ini current` 都停在 head。
     2. 健康与端到端检查：依次跑 `GET /health`、带认证的 `GET /api/v1/dalton/openclaw/health`、`bash scripts/dalton/check_bridge.sh`、`bash scripts/dalton/check_public_edge.sh`。
     3. 持久数据校验：对 `var/dalton` 下的关键 SQLite 库跑 `sqlite3 <数据库路径> "PRAGMA integrity_check;"`，再去 MinIO 或应用里抽查近期音频转写和工作区聊天附件能不能正常打开。
   - **SQLite `-wal` 告警处置**：碰上 SQLite `-wal` 残留且目录不可写导致备份脚本告警时，先确认没有进程在写，再跑一次 `sqlite3 <数据库路径> "PRAGMA wal_checkpoint(TRUNCATE);"`。

## 5. 公开入口与网络边界

Dalton 宿主机对外服务**严格只通过 Tailscale Funnel `:8443` -> Caddy `edge`（`127.0.0.1:8088`）** 暴露（细节见 [dev-guide/PUBLIC-DEPLOY.md](./dev-guide/PUBLIC-DEPLOY.md)）：

- **严禁关闭 Tailscale Funnel `:8443 -> 127.0.0.1:8088` 映射**：这是 Dalton 唯一的对外入口；同时严禁改动宿主机上原有的 `:443` 或 `:10000` 等既有服务映射。
- **校验公网入口健康**：

```bash
bash scripts/dalton/check_public_edge.sh
# 需要测试 WebSocket 鉴权通道时传入有效用户 JWT：
# DALTON_WS_TEST_TOKEN="<jwt>" bash scripts/dalton/check_public_edge.sh
```

- 每次重打前端构建或重启 `edge` 容器后跑一次 `check_public_edge.sh`，确认只放行回环和 Caddy 反代路径，`/`、`/version.json`、`/assets/` 和 `/ws/v1/market-data/status` 正常返回，`/health`、`/docs`、`/openapi.json` 等边缘屏蔽路径返回 `404`，未鉴权或伪造服务头的 `/api/v1/users/me` 返回 `401`。

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

# 4. AlphaEngine 搜索缓存命中与额度日报
python3 scripts/dalton/alphaengine_usage_report.py
```

## 7. 常见故障与处理

| 现象 | 常见原因 | 处理方法 |
|---|---|---|
| 改了 `.env` 后后端行为没变 / 新变量没生效 | 只跑了 `restart`，没重建容器 | 跑 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --force-recreate backend` |
| `/api/v1/dalton/openclaw/health` 报镜像或清单漂移 | 容器里的代码或配置和镜像构建指纹对不上 | 跑 `bash scripts/dalton/docker_build_backend.sh` 重新打包构建 |
| 前端打开空白页、旧 UI 或登录报错 | 没重跑生产构建，或漏加 `--require-supabase` | 跑 `bash scripts/dalton/build_frontend_prod.sh --require-supabase`，再查 `web/dist` 权限和 `edge` 状态 |
| 飞书文档授权或通知报 401 / 启动退出 | Vault 凭据缺失，或改完没重启网关 | 在设置页确认 `DALTON_FEISHU_APP_ID` / `DALTON_FEISHU_APP_SECRET` 已保存，然后重启 `com.dalton.channel-gateway` |
| A 股盘中监测分时或 ETF 板块偶发延迟 / 502 | 东方财富全市场扫描触发上游 IP 限流 | 确认在用限流后的默认参数（`ETF_SCAN_TTL_TRADING=120`、`EASTMONEY_PAGE_WORKERS=2`）并关掉残留旧脚本，再查 `com.dalton.monitor-feeds` 状态 |
| 语音转写或大文件上传报存储未配置 | 起 Compose 时漏了 `--profile storage`（或 `--profile infra`），或者 MinIO 没初始化 | 用 `--profile infra --profile storage up -d`（用外部 DB/Redis 时 `--profile storage up -d minio`）拉起 `minio`，并对照 `AUDIO-STORAGE.md` 查 `DALTON_ASR_STORAGE_*` 变量 |
| 外置盘没挂载导致夜间备份失败 | `/Volumes/EveSSD` 掉线，脚本按规定拒绝退回内置盘 | 挂回外置盘后手动补跑 `bash scripts/dalton/ops/dalton_db_offsite.sh` |
| 监控面板点“发布/更新”返回 403 `artifact_publishing_disabled` | 当前环境没开发布总开关 | 检查 `.env` 里的 `DALTON_ARTIFACT_PUBLISHING_ENABLED=true` 并重建 `backend`（见 [monitors.md](./features/monitors.md)） |
