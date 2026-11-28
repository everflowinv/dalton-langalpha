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
- **镜像注意事项**：`docker-compose.dalton.yml` 把 `cli` 服务放进 `profiles: ["cli-disabled"]` 让它默认关闭；`edge`（Caddy）挂载 `./web/releases:/srv-releases:ro`（站点根目录为 `/srv-releases/current`）和 `./deploy/dalton/Caddyfile`；`crawler` 跑在隔离网络 `crawl`。

### 1.3 前端生产构建

生产 `edge`（Caddy）通过 `./web/releases:/srv-releases:ro` 与相对符号链接 `web/releases/current` 静态托管前端版本目录；日常本地构建或独立核心安装默认仍写入 `web/dist`。每次发布前端代码或调过品牌资源后，在宿主机执行生产构建并过门禁：

```bash
# 默认构建到 web/dist（独立核心安装与常规手工构建）
bash scripts/dalton/build_frontend_prod.sh --require-supabase
# 生产版本目录构建并原子切换：通过持锁入口执行（共用 FD 9 部署锁）
DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && manual_build_frontend_release'
```

- 直接调用 `bash scripts/dalton/build_frontend_prod.sh --require-supabase --out-dir web/releases/<build-id>` 也会获取同一把部署锁（并排他 `mkdir` 占用目标目录），与自动部署和清理互斥。

- 脚本先读 `web/.env.dalton` 作为 `VITE_*` 默认值，再用仓库 `.env` 里的 `VITE_*`（以及 `DALTON_AUTH_MODE`、`SUPABASE_URL`、`SUPABASE_PUBLISHABLE_KEY` 回退）覆盖并导出环境变量，接着进 `web/` 通过单一共享入口 `pnpm build` 完成类型检查、Vite 生产打包与首屏预算校验（`web/scripts/check-critical-path.mjs`），并在 `--require-supabase` 下校验 Supabase 认证配置完整，最后构建静态帮助站点 `docs-site/`（含前端 `/help` 链接校验）并把产物复制到输出目录的 `help/` 下。传入 `--out-dir` 时通过 `DALTON_VITE_OUT_DIR` 将构建产物与帮助站写入 `web/releases/<build-id>`，原版本目录保持原样。
- 默认构建入口已经带了 `web/.env.dalton`（`VITE_BRAND_NAME=Everflow`、`VITE_BRAND_MARK=everflow`）。

### 1.4 生产部署

生产发布通过 `scripts/dalton/ops/prod/deploy_main.sh` 执行：

```bash
bash scripts/dalton/ops/prod/deploy_main.sh
# 变更了 docker-compose*.yml 容器配置（如 mem_limit、环境变量注入或挂载）时：
bash scripts/dalton/ops/prod/deploy_main.sh --recreate
# 同步改动了 researchdata_bridge.py 或其依赖时：
bash scripts/dalton/ops/prod/deploy_main.sh --bridge
# 包含 Dalton Alembic 迁移时校对目标版本号（自动先做新备份）：
bash scripts/dalton/ops/prod/deploy_main.sh --expect-migration d044
# 当数据库双链版本与目标提交一致时，复用 N 分钟内的成功备份以跳过重新 dump：
bash scripts/dalton/ops/prod/deploy_main.sh --reuse-backup-minutes 60
```

- **发布流程**：脚本先在检出外的固定锁文件（默认 `~/Library/Logs/Dalton/deploy_main.lock`，可用 `DALTON_DEPLOY_LOCK_FILE` 覆盖）上获取排他 `flock`（并发启动的第二个部署会直接提示退出，父子接续全程共享同一把锁），再调用 `wait_backend_idle.sh` 连续两次确认后端空闲，按单次 10 秒超时（`perl` 进程超时与 PostgreSQL `statement_timeout`）读取并记录当前数据库两条 Alembic 迁移链版本（`alembic_version_dalton` 与 `alembic_version`；读取失败或超时时按有迁移处理并继续流程）。接着只执行一次 `git fetch origin` 并锁定目标提交 `TARGET=origin/dalton/main`，依次完成三项预检：（1）校验当前检出 `HEAD` 是 `TARGET` 的祖先（存在本地额外或分叉提交时直接报错退出）；（2）用 `git status --porcelain --untracked-files=all --ignored -- migrations/versions src/dalton/migrations/versions` 检查两条迁移目录保持干净（只排除 `__pycache__/` 与 `*.pyc`，存在未跟踪、被忽略、已修改或已暂存文件时列出文件并退出，先清理或提交后再部署）；（3）按 Alembic 规则（`versions` 目录下所有 `*.py`，排除 `__init__.py`）直接从 `TARGET` 的 Git 树解析 Dalton 与上游两条迁移链的唯一 head，校验每个迁移文件都显式赋值 `revision` 与 `down_revision`、全部父修订存在且从唯一 head 可遍历整条迁移链（发现缺字段、缺父修订、环或多余 head 时直接报错退出）。随后把部署前读取的数据库双链版本与 `TARGET` 的双链 head 比对：传入 `--reuse-backup-minutes N`、双链版本全部一致且 `dalton-pg-backup.log` 末行为 N 分钟内 ` ok ` 记录时复用已有备份；其余情况触发 `com.dalton.pg-backup` 完成一次新备份并核对备份日志。备份完成后记录 `PRE_FF`，快进到锁定的 `TARGET` 并断言 `HEAD` 等于 `TARGET`。
  - **快进后协议握手与新版接续**：快进完成后，脚本对比当前执行中的脚本与目标提交里的 `scripts/dalton/ops/prod/deploy_main.sh` 和 `wait_backend_idle.sh`。任一文件有更新时，将新版脚本复制到新的临时目录并依次完成三项握手校验：（1）`bash -n` 语法检查；（2）调用新版 `--resume-protocol` 核对协议版本（当前为 `2`）一致；（3）写入包含协议版本、`PRE_FF`、`TARGET`、双链版本、备份结果与网关预检状态的临时状态文件，再调用新版 `--validate-resume <state-file> -- <原参数>` 核对状态格式与原参数全部兼容。任一握手项通过后，携带成对的 `DALTON_DEPLOY_REEXECED=<TARGET>` 与 `DALTON_DEPLOY_STATE_FILE`、沿用原排他锁文件描述符启动新版脚本，从快进之后直接继续，跳过重复备份与快进；握手未通过时停止并输出手工复位指引（保留服务与前端原样，不启动新版脚本）。`DALTON_DEPLOY_REEXECED` 与 `DALTON_DEPLOY_STATE_FILE` 必须成对出现，单独传入时脚本立即拒绝运行。
  - **首次从 `web/dist` 迁移到 `web/releases/`（专用入口）**：首次上线该变更由值班人员按专用入口执行：（1）在生产检出根目录先快进到包含标准 `web/releases` 配置的目标提交（此时运行中的 `edge` 仍挂载 `web/dist`）；（2）直接执行 `bash scripts/dalton/ops/prod/deploy_main.sh --first-releases-rollout`（必要时可附 `--pre-ff <旧提交>` 指定回退基线）。部署与首次入口仅接受两种标准 bind 挂载（旧 `./web/dist:/srv:ro` 或新 `./web/releases:/srv-releases:ro`，及 `./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro`，短/长语法均可），遇到覆盖文件指向外部目录、`tmpfs` 或非 bind 挂载时在重建前拒绝并提示支持的配置。脚本核对运行中的 `dalton-langalpha-edge-1` 处于运行态且实际 bind 挂载本检出 `web/dist -> /srv` 后，先在重建前准备、冻结并经 `docker compose config` 与旧 `edge` 镜像 `caddy validate` 校验 `PRE_FF` 的旧 Compose 与 Caddyfile 快照，再将现有 `web/dist` 复制成 `web/releases/legacy-<UTC时间戳>-<PID>`（保留兼容旧前端 JS 的 `build` 字段并写入 `entry` 与含 `legacy-<时间>` 的 `release` 标识）、原子设置 `web/releases/current` 并校验 `index.html`、`assets/`、`help/index.html` 与 `version.json` 齐全，随后使用显式 Compose 文件与项目名重建 `edge`、校验 `/__edge_health` 专用响应（`dalton-edge-ok releases-v1`）并执行公网检查；重建开始后遇到失败或 `SIGINT`/`SIGTERM` 中断时共用同一回退流程，回退开始到旧站点 HTTP 核验结束期间延迟处理后续信号，确认旧站点 HTTP 200 正常后再放锁退出（若回退本身失败则在 FD 9 锁文件与 `.rollback-failed` 中双写故障状态、保留快照目录并输出基于冻结 `compose.args` 的人工恢复命令；全程保留 `web/dist` 原样）。若运行中的 `edge` 仍挂载旧 `web/dist` 而目标提交需要 `/srv-releases`，常规 `deploy_main.sh` 会在 `git merge --ff-only` 之前直接以退出码 `3` 退出，`HEAD`、Compose、Caddyfile 与所有服务保持原样，并提示快进后运行 `--first-releases-rollout`（仅当当前 `HEAD` 确为旧 `web/dist` 提交时附带 `--pre-ff`）。每次首次迁移尝试均使用独立回退快照目录，仅在确认无容器继续绑定旧快照后才清理；首次重建与回退的 `docker compose up` 均在独立进程组内运行并设截止时间（`DALTON_EDGE_COMPOSE_TIMEOUT`，默认 120 秒），超时或中断先终止整组再回退或记录故障。
  - **前端版本目录构建与单步原子切换**：脚本拿快进后的 `HEAD` 双链 head 与部署前数据库版本比较得出 `MIGRATING_DEPLOY`，同步宿主机 `.venv`（`uv sync --frozen`）与前端依赖（`pnpm install --frozen-lockfile`），随后以独立进程组通过 `scripts/dalton/build_frontend_prod.sh --require-supabase --out-dir web/releases/<target短哈希>-<UTC时间戳>-<PID>`（构建前通过排他 `mkdir` 占用目标目录）将新版前端与帮助站构建到独立新版本目录，写入包含应用入口 `entry`、帮助站内容哈希 `help_hash` 与组合构建标识 `build` / `release` 的 `version.json`（从而在应用入口相同、仅帮助站变化时也会更新 `build`），记录本版自有资源清单 `.deploy-own-assets`，并从上一版本目录补拷（优先同盘硬链接）上一版自有带哈希 `assets/`、`help/_astro/` 与 Pagefind 内容寻址索引分片（`*.pf_meta`/`*.pf_index`/`*.pf_fragment`/`*.pf_filter`），确保切换瞬间仍在旧页或旧帮助页搜索的浏览器继续取得到旧资源且仅保留相邻一代。涉及渠道网关变更时同步在独立 release 目录构建网关。重启前再次确认后端空闲与迁移目录/head 一致，随后重建或重启 `backend`、`crawler`（以及存在配置差异或运行中容器实际挂载 `Type`/`Source`/`Destination` 与目标不一致时的 `edge`；重建 `edge` 前先核验 `web/releases/current` 存在且 `index.html`、`assets/`、`help/index.html`、`version.json` 齐全），校验后端 `/health`、Dalton 迁移版本、渠道网关切换与 `/health`、`crawler:8100/health` 及按当前生效 Caddyfile 匹配的 `edge` 就绪探测（新版要求 `GET /__edge_health` 返回 `200` 且正文为 `dalton-edge-ok releases-v1`，回退到旧 Caddyfile 时按旧文件匹配）。全部服务健康后，用临时符号链接加 `os.replace` 单步原子将相对符号链接 `web/releases/current` 指向新版本目录；随后运行 `scripts/dalton/check_public_edge.sh --expected-build <build> --expected-release <release> --dist-dir web/releases/current` 核对 HTTP 返回的 `/version.json` 与本次发布的 `build`、`help_hash`、`release` 完全一致（同时验证 Caddy 按新符号链接即时提供新版本；手工切换后亦可执行 `curl -fsS http://127.0.0.1:8088/version.json` 复核 `build` / `release` 字段），通过后在持有同一把 FD 9 部署锁的状态下清理旧版本目录（保留最近 3 个版本，始终保留当前版本与上一版本）。人工切换（`manual_switch_frontend_release`，补拷失败时中止切换）、人工构建发布（`manual_build_frontend_release`，与部署共用中断陷阱，先终止整组构建进程再清目录放锁）与自动清理（`prune_old_frontend_releases`）均复用同一把 FD 9 部署锁，从目录检查持续持锁到切换或删除结束。
- **常用参数**：
  - `--recreate`：在 `docker-compose.yml` 或 `docker-compose.dalton.yml` 中调整了容器编排字段（如资源上限、环境变量、挂载路径）时传入，使用 `docker compose up -d --no-deps` 重建 `backend` 与 `crawler` 使配置生效；日常纯代码发布保持默认 `docker restart` 即可。每次部署开始时都会按文件名模式清理 `~/Library/Logs/Dalton/containers/<service>-<UTC时间戳>.log.gz` 中超过 14 天的旧归档；重建 `backend`、`crawler` 或 `edge` 容器前，脚本会自动将旧容器的 `docker logs --timestamps` 压缩保存至该目录（保存失败仅告警继续部署）。
  - **数据任务运行副本同步（ETF 月度资金流 / Token 用量周报 / Antigravity 系列快照）**：`scripts/dalton/data_assets/` 中的脚本与技能（含 `token_weekly_usage_tracker.py`、`cron_command_tasks.py`、`feishu_send_card.py` 等），以及 `src/dalton/llm/antigravity_series.py` 和导出的 `references/antigravity_series.json`，均由 `scripts/dalton/seed_openclaw_data.sh` 同步到运行目录（默认 `var/dalton/openclaw-data/`）。改过 `data_assets/` 或 Antigravity 模型系列配置后，需在常规 `deploy_main.sh` 之外单独执行：`bash scripts/dalton/seed_openclaw_data.sh --apply`（先可用 `--dry-run` 预览）。
  - `--bridge`：本次发布包含 `scripts/dalton/researchdata_bridge.py` 或宿主机桥接逻辑改动时传入，在后端就绪后执行 `launchctl kickstart -k` 重启 `com.dalton.researchdata-bridge` 并查看服务列表状态。
  - `--expect-migration dNNN`：本次发布附带 Dalton 数据库迁移时传入，解析参数时即校验 `dNNN` 格式（无效值以状态码 `2` 退出），并在后端就绪后核对 `alembic -c src/dalton/alembic.ini current` 输出的迁移版本前缀。
  - `--reuse-backup-minutes N`（正整数分钟）：脚本锁定本次 `TARGET` 后，核对部署前读取的数据库双链版本与 `TARGET` 的双链 head 完全一致、且 `dalton-pg-backup.log` 末行是 N 分钟内带 ` ok ` 的成功备份时，直接复用该备份跳过重新 dump；存在待执行迁移、数据库版本未能读取或缺少近期成功备份时，自动执行新备份。
- **失败回滚与迁移保护**：脚本在快进前记录 `PRE_FF`。若快进后的 `HEAD` 校验或迁移判定、`pnpm install`、前端版本目录构建、重启前二次空闲等待、容器重启/重建（`backend`、`crawler`、`edge`）、后端 `/health`、Dalton `crawler:8100/health`、`edge` 就绪探测、前端符号链接切换或 `check_public_edge.sh` 任一步失败，会调用 `revert_deploy`：
  - **前端独立回退与版本保留**：若失败发生在切换 `web/releases/current` 之前，脚本仅清理未切换的新版本目录，`current` 始终指向完整旧版本；若失败发生在切换之后（如 `check_public_edge.sh` 失败），脚本在迁移判定与 `git reset --keep` 之前先将新版自有带哈希 `assets/` 与 `help/_astro/` 补拷进旧版本目录（确保公网检查期间拿到新版 HTML 的浏览器在回退后仍可正常获取新版资源），再把 `web/releases/current` 单步原子指回上一版本目录，并保留上一版本目录（暂停自动复位代码时同时保留新旧版本目录供人工排查与切换）。收到 `SIGINT`/`SIGTERM` 时，脚本按进程组终止正在运行的构建或检查并等待整组退出后，再清理未切换的新目录并释放锁；已切换后的信号保留当前指向的完整版本与旧版本目录。
  - **自动回退代码**：同时满足本次部署无待执行迁移（`MIGRATING_DEPLOY=0`）且回退目标 `PRE_FF` 的两条迁移链 head 都等于部署前从数据库读到的版本时，按流程将代码复位到 `PRE_FF`、重新同步 `.venv` 与 `web/node_modules`（前端直接复用已保留的上一版本目录，跳过重建）。若已触动容器，同步重建/重启 `backend`、`crawler` 与 `edge`，并重跑健康检查与按旧版构建标识核对 `check_public_edge.sh`，最终以非零状态码退出。
  - **保留现场并交由人工处理**：当 `MIGRATING_DEPLOY=1`、数据库版本未能读取，或 `PRE_FF` 的双链 head 与数据库版本不一致（例如上次构建失败后本地停在含新迁移的提交、远端随后撤销该迁移）时，脚本保持当前代码与各服务现状，按实际情况说明中止原因，并打印当前与部署前的数据库双链版本以及 `PRE_FF`、`TARGET` 各自的双链 head。脚本把两条链 head 均等于当前数据库版本的提交（`PRE_FF` 或 `TARGET`）标为推荐复位目标；当两者都与数据库不一致或版本读不到时，提示先保持后端停止，给出数据库版本查询与候选提交核对命令，由值班人员先确认要部署的提交。手工处理时在部署检出根目录按输出的 1–9 步顺序执行：先只读排查（`docker ps -a`、后端日志），选择手工恢复后按第 4 节停写并暂停看门狗（`launchctl bootout` 停掉 `com.dalton.backend-watchdog`、`com.dalton.monitor-feeds`、`com.dalton.researchdata-bridge`、`com.dalton.channel-gateway`，并停止 `backend`、`crawler`、`edge`）、核对 `DB_(HOST|PORT|NAME)`、触发 `com.dalton.pg-backup` 确认 `~/Library/Logs/dalton-pg-backup.log` 末行出现新的 ` ok ` 记录并在停写状态重读双链版本（**必须同时核对上游 `alembic_version` 与 Dalton `alembic_version_dalton` 两条迁移链的实际执行结果**）：
    - **仅 Dalton 链推进（上游 `alembic_version` 与部署前完全一致，且仅执行了可逆 Dalton 迁移如 `d046 -> d049`）且新版尚未产生业务写入**：先按需导出受影响新表/列（如 `dalton_channel_weixin_audit_logs` 导出为 JSONL，或 `d042 -> d041` 参见 [antigravity-effort-picker.md](./features/antigravity-effort-picker.md) §3），再在维护容器执行 `alembic -c src/dalton/alembic.ini downgrade d046` 回到部署前版本，核对双链版本与 `PRE_FF` 一致后复位代码并拉起旧服务；
    - **上游链已推进（无论 Dalton 迁移成功或中途失败，如上游迁移已提交而 `d049` 失败，属于已发生 schema 变更而非无变更状态）**，或**两条链均已推进且新版已产生业务写入**：不得仅执行 Dalton `downgrade d046`（否则上游已提交的变更仍留在库中，与旧版代码不匹配），应使用部署前落盘的全量 `pg_dump -Fc` 备份执行 `pg_restore` 整库恢复，或同步核对并回退两条迁移链至与目标 `RESET_SHA` 双链 head 完全一致（仅在明确决定保留运行期新增对话、且上游版本与旧版兼容时才可单独执行 `downgrade d046`；若原管理员曾在多账号期间重扫过码，旧版恢复后需在管理页重新扫码一次）。
    - **复位代码前先设定 `RESET_SHA` 并运行 `target_migration_heads "$RESET_SHA"` 打印所选提交的双链 head，核对与数据库双链版本完全一致后再执行 `git reset --keep "$RESET_SHA"`**、通过 `manual_switch_frontend_release` 切回对应 `web/releases/` 版本（或用 `manual_build_frontend_release` 新建版本目录并原子切换；若目标提交为旧版 `web/dist` 挂载则跑 `build_frontend_prod.sh --require-supabase`）、强制重建容器、恢复宿主机服务（含 `--bridge` 桥接）与健康/公网验证，全部通过后用 `launchctl bootstrap` 恢复看门狗。脚本以非零状态码退出。
  - **多账号微信迁移（`d049`）部署后外键校验**：`d049` 热表外键以 `NOT VALID` 建约束避免停机长锁；部署恢复服务后，执行 `bash scripts/dalton/ops/prod/validate_d049_fks.sh` 在独立短事务中逐条执行 `VALIDATE CONSTRAINT` 并验证 `pg_constraint.convalidated = true`。
- **边缘代理（`edge`）重载**：快进合并后通过 `deploy/dalton/Caddyfile` 差异、`docker compose config` 解析后的 `edge` 服务配置对比以及 `docker inspect` 运行中容器实际挂载比对，检测到任一变化或挂载漂移时，先确认 `web/releases/current` 存在且可服务，再在健康检查前执行 `docker compose up -d --no-deps --force-recreate edge`，随后以 60 秒统一截止时间循环探测独立于前端静态文件的 `http://127.0.0.1:8088/__edge_health`（端口由 `DALTON_EDGE_PORT` 控制，校验 HTTP `200` 与专用响应正文 `dalton-edge-ok releases-v1`）；超时直接触发回滚并以非零退出，就绪并原子切换 `web/releases/current` 后再运行 `check_public_edge.sh` 核对 HTTP `/version.json` 的 `build`、`help_hash` 与 `release` 标识。
- **边缘代理（`edge`）重载**：快进合并后通过 `deploy/dalton/Caddyfile` 差异及 `docker compose config` 解析后的 `edge` 服务配置对比，检测到任一变化时会在健康检查前执行 `docker compose up -d --no-deps --force-recreate edge`，随后以 60 秒统一截止时间循环探测 `http://127.0.0.1:8088/`（端口由 `DALTON_EDGE_PORT` 控制，要求 `curl` 成功且返回 `200`）；超时直接触发回滚并以非零退出，就绪后再运行 `check_public_edge.sh`。
- **启用渠道网关部署的同步发布（含研究总监归档确认卡片）**：本批归档与栅栏改动无新增数据库迁移；启用了 `channel_gateway` 能力的部署（如 Everflow）发布时，须通过 `scripts/dalton/ops/prod/deploy_main.sh` 同步更新后端、前端静态包与 `integrations/dalton-channel-gateway`（确保新增的 `archive_workspace` 仅网页确认文案与前后端快照协议一致）；后端启动钩子会在重启时自动将遗留在归档过渡态的电脑写回 `stopped`，待提供商确认终态后再清除 `config.archiving` 栅栏。独立核心安装（无渠道网关）按 [STANDALONE-DEPLOY.md](./dev-guide/STANDALONE-DEPLOY.md) 通过 `scripts/standalone/upgrade.sh` 同步更新 `backend` 与 `edge` 即可。
- **配套宿主机脚本与 `launchd` 模板**：`scripts/dalton/ops/prod/` 下同时收拢了 `wait_backend_idle.sh`、`backup.sh`、`watchdog.sh`、`colima_launch.sh` 以及 `launchd/` 下的三份模板（`com.dalton.backend-watchdog.plist.template`、`com.dalton.colima.plist.template`、`com.dalton.pg-backup.plist.template`）。

## 2. `.env` 与叠加配置文件改动生效规则

- **改完 `.env` 必须重建容器**：Docker Compose 只在创建容器时读 `.env`；修改 `INTERNAL_SERVICE_TOKEN`、`DALTON_ALLOWED_EMAILS`、`DALTON_PM_USER_IDS`、`DALTON_ARTIFACT_PUBLISHING_ENABLED`、`GEMINI_API_KEY`、`XAI_API_KEY` 等 `.env` 变量后，执行 `--force-recreate` 使新变量生效：

```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --force-recreate backend
```

  如果 `crawler` 或 `edge` 依赖的变量（比如 `INTERNAL_SERVICE_TOKEN`）也改了，对应服务也要一起 `--force-recreate`。
- **修改 `agent_config.dalton.yaml` / `config.dalton.yaml` / `src/dalton/dalton.yaml`**：虽然宿主机目录挂进了 `backend` 容器（`./agent_config.dalton.yaml`、`./config.dalton.yaml`、`./config/dalton` 等），但后端镜像启动时会核对 `/app/.build_files.sha256` 清单指纹，所以**生产改动要统一走 `scripts/dalton/docker_build_backend.sh` 重建镜像和容器**。

### 2.1 Firecrawl 多 Key 主备配置与冷却排查

- **添加第二个（及更多）Key**：在 `.env` 中按顺序配置 `FIRECRAWL_API_KEY`（主 Key），再追加 `FIRECRAWL_API_KEY_2`、`FIRECRAWL_API_KEY_3`……（从 `_2` 起按连续数字递增，遇到空缺即停止读取；只写变量名、不要把任何真实密钥写入文档或日志）。
  - 调度策略为主备切换：请求优先使用顺序最靠前且未冷却的 Key；某个 Key 返回 402 账户额度用完时标记冷却并立即用下一个可用 Key 重试同一个请求（若响应带有余额大于零，或无余额字段但文义为“本次请求所需额度不足”，多 Key 模式下直接返回提供方错误，保持其余 Key 正常）。冷却时长规则：有未来的 `resets_at` 就冷却到那时为止；`resets_at` 已过期则短冷却 5 分钟；缺少或难以解析 `resets_at` 时，若响应明确余额为 0（`<= 0`）则冷却 24 小时，若缺少余额字段则冷却 1 小时。仅当所有 Key 都处于冷却时才返回 `budget_exceeded` 并快速失败（提示最早的实际冷却截止时间 `cooldown_until`），任一 Key 冷却到期后自动恢复优先使用。
  - 改完 `.env` 后执行 `--force-recreate` 重建 `backend` 容器以重读环境变量：

```bash
docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps --force-recreate backend
```

- **查看当前哪个 Key 在冷却**：冷却状态保存在 Redis 的 `firecrawl:key_cooldown:<fp>` 键中（`<fp>` 为对应 Key 的 SHA-256 前 12 位十六进制指纹；日志与 Redis 均不记录 Key 原文）：

```bash
# 列出当前所有处于冷却中的 Firecrawl Key 指纹
docker compose -f docker-compose.yml -f docker-compose.dalton.yml exec -T \
  -e REDISCLI_AUTH="${REDIS_PASSWORD:-redis}" redis \
  redis-cli --scan --pattern 'firecrawl:key_cooldown:*'

# 查看具体冷却记录（cooldown_until / resets_at / marked_at）与剩余冷却秒数
docker compose -f docker-compose.yml -f docker-compose.dalton.yml exec -T \
  -e REDISCLI_AUTH="${REDIS_PASSWORD:-redis}" redis \
  redis-cli GET 'firecrawl:key_cooldown:<fp>'
docker compose -f docker-compose.yml -f docker-compose.dalton.yml exec -T \
  -e REDISCLI_AUTH="${REDIS_PASSWORD:-redis}" redis \
  redis-cli TTL 'firecrawl:key_cooldown:<fp>'
```

  也可以在后端日志中检索 `Firecrawl key fp=` 对照指纹定位是哪一个 Key 触发了 402 切换或冷却；宿主机 `researchdata_bridge.py` 面向 `employee-reviews` 子进程仍仅透传单个 `FIRECRAWL_API_KEY`。

### 2.2 内置浏览器功能（默认关闭与开启条件）

- **为什么关**：合并上游 `#831` 后，Dalton 默认关闭云计算机内置浏览器功能，原因有三项：
  1. **缺少远程预览通道**：Dalton 通过单域名 Tailscale Funnel + Caddy 暴露服务，且 `DALTON-HOOK(sandbox-port-bind)` 将沙箱端口绑定到宿主机 `127.0.0.1`，需先配置独立泛解析 HTTPS 预览域（`PREVIEW_PROXY_BASE_URL`）才可向远程用户提供直播流；
  2. **网络隔离与权限边界不足**：内置出站代理 `browser_runtime/proxy.py` 未拦截私网/兄弟容器网段且会被宿主机 Fake-IP（`198.18.0.0/15`）绕过；每台计算机共用一个 Chrome 实例且 `relay.py` 无服务端控制租约校验，存在共享串屏、越权操控及权限撤回后未断开存量连接的风险；
  3. **宿主机内存与容器资源受限**：Mac Colima VM 内存余量有限、容器未配置 `/dev/shm` 扩容且缺少跨容器浏览器并发总量限制。
  - 当前通过三层总闸保持关闭：`config.dalton.yaml` 设置 `features.browser.enabled: false`（隐藏设置页开关、模型 `browser` 技能与前端 Browser 入口，使 `browser-*` 接口返回 `unavailable`），`DALTON-HOOK(browser-kill-switch)` 同步跳过沙箱侧 `browser_runtime` / `agent-browser` 包装器上传、画质同步与画质接口，且 `DockerProvider._ensure_image` 在 `APP_ENV=dalton` 下会自动传入 `INSTALL_AGENT_BROWSER=0`。`Dockerfile.sandbox` 保持上游默认 `ARG INSTALL_AGENT_BROWSER=1`；Dalton 常规部署（`deploy_main.sh`、`docker_build_backend.sh`、`docker-compose.dalton.yml`）不重建沙箱镜像，**如后续手工重建 `langalpha-sandbox:latest` 且浏览器仍保持关闭，必须显式传入 `--build-arg INSTALL_AGENT_BROWSER=0`**（`docker build --build-arg INSTALL_AGENT_BROWSER=0 -f Dockerfile.sandbox -t langalpha-sandbox:latest .`）。现有 Playwright、Scrapling 与普通应用预览保持不受影响。
- **开之前要补哪些事**：
  1. **出站网络隔离与 Fake-IP 防护**：在实际出站连接处拦截 Mac 宿主机、Docker 网段及内网地址，并补齐 Fake-IP 真实 IP 复查；
  2. **独立授权与访问控制**：首轮试用仅限指定内部用户在个人计算机上使用；将观看与控制权限绑定到用户、计算机和连接，并在服务端校验键鼠输入的控制租约；浏览器端口改由专用接口管理，避免普通应用预览转发；
  3. **连接撤销与独立预览代理**：直播流强制走独立泛解析 HTTPS 预览代理（配好 `PREVIEW_PROXY_BASE_URL` 与边缘路由）；撤回试用权限或关闭开关时断开已建 WebSocket 并停止对应浏览器；
  4. **并发压测与资源限额**：在生产同规格隔离环境实测 Chrome（含 `/dev/shm` 评估）与分析任务并发时的内存和 CPU 占用，按实测结果限制同时运行的浏览器数量，试用先从 `light` 画质起步。
- **怎么开**：
  1. 完成上面四项前置改造并验证通过；
  2. 将 `config.dalton.yaml` 中的 `features.browser.enabled` 改为 `true`；
  3. 按第 1.1 节运行 `bash scripts/dalton/docker_build_backend.sh` 重建并重启后端；
  4. 按上游默认（`INSTALL_AGENT_BROWSER=1`）重建 `langalpha-sandbox:latest`（如 `docker build -f Dockerfile.sandbox -t langalpha-sandbox:latest .` 或显式加 `--build-arg INSTALL_AGENT_BROWSER=1`），并重建替换现有沙箱容器。

### 2.3 Drive 功能（默认关闭）与对象存储生命周期规则

- **默认关闭与启动硬门禁**：合并上游 `#872`（Drive）后，Everflow 生产（`config.dalton.yaml`）与独立核心安装（`config.core.yaml`）均默认设置 `features.drive.enabled: false`。关闭状态下，设置页实验开关、侧栏 `/drive` 入口、前端 `/drive` 路由、计算机 `.agents/drive` 挂载与智能体文件工具中的 Drive 路由均保持隐藏或关闭，后端 `/api/v1/drive/*` 与 `/api/v1/livefs/drive/*` 在入口最先校验部署级开关并统一返回 `404`。
  - 当部署同时配置了 `features.drive.enabled: true` 且 Dalton 同事访问（`colleague_sharing` / `colleague_dispatch`）或电脑 CLI 凭据（`computer_cli_tokens`）任一处于开启状态时，后端会在启动时记录明确错误日志并强制将 Drive 视为关闭（`DALTON-HOOK(drive-kill-switch)`）；已有的同事隔离防护（不挂载、`mount_guard`、挂载接口拦截）继续保留作为第二层。
- **在 Dalton 打开 Drive 前必须完成的三项前置改造**：
  1. 跨用户回合在交回会话前 `await` 卸挂载（覆盖温路径、慢路径与初始化中提前返回，确保跨用户命令开始前主机上不再挂载属主的 Drive）；
  2. `converge_soon`（及 `_load_livefs` / `_keep_drive` / `_drive_soon` 等后台收敛调用）携带真实调用者身份（`caller_user_id`），防止异步收敛任务将属主 Drive 重新挂回；
  3. 电脑 CLI 凭据（`computer_cli_tokens`）的 Drive 权限与真实调用者绑定（或在允许跨用户执行的计算机上隔离其 Drive API 权限），防止跨用户回合经公开 Drive API 读写属主 Drive。
- **对象存储分片清理与启动告警说明**：后端启动时会调用 `has_multipart_cleanup_rule(min_days=7, prefix="uploads/")` 检查主存储桶（`STORAGE_BUCKET_NAME`）是否配置了「中止未完成分片上传且 `DaysAfterInitiation >= 7`」的清理规则。
  - 当存储桶缺少该规则或天数为 6 天及以下时，后端会输出一条 `WARNING` 日志提醒运维配置，该检查仅作告警提示，正常放行启动。
  - 独立核心安装自带的 `scripts/standalone/minio_init.py` 仅负责创建私有桶并清理匿名桶策略；后续启用 Drive 前，S3 / R2 环境配置 `AbortIncompleteMultipartUpload`（`DaysAfterInitiation >= 7`），内置 MinIO 环境通过 MinIO 原生的 `stale_uploads_expiry` 配置分片清理（环境变量 `MINIO_API_STALE_UPLOADS_EXPIRY`，`docker-compose.core.yml` 默认 `24h`；启用 Drive 时设为 `168h` 及以上，因为 Drive 分片上传最长保持打开 6 天）。MinIO 经 S3 生命周期接口查不到这项设置，所以在内置 MinIO 上该启动告警每次都会出现，启动日志同时写明这两种配置方式。

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

> 安全提醒：改了 Vault 里的飞书应用凭据（`DALTON_FEISHU_APP_ID` / `DALTON_FEISHU_APP_SECRET`）后，需在确认无进行中推送时执行 `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway` 重启网关以刷新内存凭据。

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
- **范围边界**：仅替换系统模型引用，用户自定义模型（`model_preference.custom_models`）保持原样；`automations.instruction` 文本中的旧模型名仅列出原文片段与建议改法，由人工按需修订。
- **并发与中断恢复**：线程只更新计划内的 `thread_id`（事务内核对行数不符即回滚）；用户偏好在事务内加锁重读并通过 CAS 保护，冲突时跳过该用户并报告；任一阶段失败会打印已完成与未完成进度摘要并以非零退出码退出，修复后可直接重跑（幂等，已切换行自动跳过）。

### 3.5 专项运维脚本（Opus 思考强度回填与重复公司工作区合并）

- **存量 Opus 定时任务思考强度一次性回填（`scripts/dalton/ops/backfill_opus_automation_effort.py`）**：仅针对未删除且 `llm_model` 严格等于 `claude-opus-5-5-oauth` 或 `claude-opus-5-5`、`reasoning_effort IS NULL` 且未被用户自定义模型遮蔽的任务，写入 `reasoning_effort = 'medium'`。默认只读预演（`--dry-run --out <plan.json>`），确认后执行 `--apply --plan <plan.json>`（写前落盘 `0600` 归档文件至 `var/dalton/operator-records/opus-effort-backfill-<batch_id>.json` 并在单事务行锁内复核，保留用户已主动清空的任务），支持 `--rollback <batch_id>` 安全还原。详见 [antigravity-effort-picker.md](./features/antigravity-effort-picker.md)。
- **历史重复公司工作区合并（`scripts/dalton/ops/merge_duplicate_company_workspace.py`）**：
  - **权限变更副作用（执行前必核对）**：执行 `--apply` 除了将重复工作区（`--dup`）的对话与知识版本合并到规范工作区（`--canon`）、标记重复项（不复制工作区磁盘文件）外，**还会把规范工作区 `canon` 的网页共享直接设为可读写（`web_access='rw'`）**，并同时写入旧的工作区级协同字段（`cos_access='rw'`）。在当前生产已开启跨人协同（`DALTON_COLLEAGUE_DISPATCH_ENABLED=true`）的环境下，研究总监协同实际权限取决于工作区所有者的账户级开关。执行前须先在预演输出中核对共享设置变更。
  - **执行与归档路径**：必传 `--dup <重复工作区UUID> --canon <规范工作区UUID>`；在宿主机直接执行时默认归档写到 `~/Backups/dalton/merge-ws-<UTC时间戳>/before.jsonl`（容器内默认 `var/dalton/operator-records/`，可用 `--archive-out` 指定路径）。在两个工作区无人使用时加 `--apply` 单事务执行并落盘 `0600` 归档，支持 `--revert <before.jsonl>` 撤销：
    ```bash
    # 1. 只读预演并核对预演中的共享设置变更
    .venv/bin/python scripts/dalton/ops/merge_duplicate_company_workspace.py \
      --dup <重复工作区UUID> --canon <规范工作区UUID>

    # 2. 确认后执行写入（按需通过 --archive-out 指定归档文件）
    .venv/bin/python scripts/dalton/ops/merge_duplicate_company_workspace.py \
      --dup <重复工作区UUID> --canon <规范工作区UUID> --apply
    ```
  - **提交后核查**：合并完成后，抽查已迁移对话链接是否正常打开，并核对规范工作区的网页共享权限及所有者的账户级研究总监协同开关是否符合预期。

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
2. **密钥必须离线保管**：`~/.config/dalton/backup.key`（默认对应 `DALTON_BACKUP_KEY_FILE`，权限 `0600`）**仅保存在本地与团队密码管理器中，隔离在 GitHub 与 Google Drive 之外**。
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
     2. **PostgreSQL（`gdrive:DaltonBackup/db/<YYYY-MM-DD>/dalton-pg-*.dump.enc`）**：先只启动 `postgres`（`docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile infra up -d postgres`）。从 Google Drive 拉取目标日期最新的 `dalton-pg-*.dump.enc`，用 `openssl enc -d` 解出 `.dump`，再用 `pg_restore` 灌进目标库；随后执行双链 Alembic `upgrade head`，在启动 `backend`、`research-host` 与任何定时调度前，对恢复库执行共用前置清空脚本 `psql ... -v ON_ERROR_STOP=1 < scripts/dalton/sql/personal_integrations_restore_reset.sql`（将存量个人连接令牌清空并置为 `needs_reauth`，同时清空待吊销令牌与 OAuth 暂存状态）。
     3. **Redis 快照（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/redis-<YYYY-MM-DD>.rdb.enc`）**：确认 `DALTON_REDIS_CONTAINER`（默认 `dalton-langalpha-redis-1`）处于停止状态。解密 `redis-*.rdb.enc` 拿到 RDB 文件，写回 `langalpha-redis-data` 卷的 `/data/dump.rdb`（开了 AOF 的话先清掉同目录下残留的 `appendonlydir`，免得盖过 RDB 加载），再启动 `redis` 并核对 `REDIS_PASSWORD` 和 `.env` 一致。
     4. **MinIO 对象存储（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/minio-<YYYY-MM-DD>.tar.gz.enc`）**：确认 `minio` 容器已停。用辅助镜像（`DALTON_DOCKER_HELPER_IMAGE`，默认 `alpine:3.20`）把 `DALTON_MINIO_VOLUME`（默认 `dalton-langalpha_dalton-minio-data`）挂到 `/data`，把 `minio-*.tar.gz.enc` 解密并用 `tar xzf - -C /data` 恢复桶和对象（同时恢复数据库时，挑跟 Postgres 时间点一致的周包），再用 `--profile storage up -d minio` 启动。
     5. **宿主机 `var/dalton`（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/var-dalton-<YYYY-MM-DD>.tar.gz.enc`）**：清空或移走旧的 `DALTON_PROD_VAR_DIR`（默认 `var/dalton`），解密后用 `tar xzf - -C <PROD_VAR_DIR>` 解压恢复。这个包里已经通过 `backup_lib.py:snapshot-sqlite` 合入了在线一致的 SQLite 副本，并排除了 `-wal`/`-shm`/`-journal` 以及 `DALTON_DATA_VAR_EXCLUDES` 里可重建的目录。如果里面有 `DuckDB` 或 `neo4j`，它们的一致性依赖备份窗口期间没有写入。
     6. **沙箱工作区（`gdrive:DaltonBackup/weekly/<YYYY-MM-DD>/sandbox-<容器名>-<YYYY-MM-DD>.tar.gz.enc`）**：如果目标 `langalpha-sandbox-*` 容器还在宿主机上且需要还原 `/home/workspace`，可以把对应 `.tar.gz.enc` 解密后在容器停机或无写入时 `docker cp` 解压回 `/home/workspace`。超过 `DALTON_SANDBOX_MAX_SIZE_GB`（默认 `5` GiB）的沙箱在备份时会被跳过；常规工作区和聊天附件也能靠恢复好的 Postgres 和 MinIO 重放冷重建。
     7. **拉起应用与宿主机服务**：依次启动 `backend`、`crawler`、`edge`（确认 `docker_entrypoint_dalton.sh` 双链 Alembic 版本一致），最后重新加载并拉起第 1 步停掉的宿主机 `launchd` 服务。
   - **恢复后核查**：
     1. 双链迁移版本校验：进 `backend` 容器确认 `uv run alembic current` 和 `uv run alembic -c src/dalton/alembic.ini current` 都停在 head。
     2. 健康与端到端检查：依次跑 `GET /health`、带认证的 `GET /api/v1/dalton/openclaw/health`、`bash scripts/dalton/check_bridge.sh`、`bash scripts/dalton/check_public_edge.sh`。
     3. 持久数据校验：对 `var/dalton` 下的关键 SQLite 库跑 `sqlite3 <数据库路径> "PRAGMA integrity_check;"`，再去 MinIO 或应用里抽查近期音频转写和工作区聊天附件是否正常打开。
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
| 覆盖单元绑定/解绑/归档返回 409 | 单元已绑定其他工作区，或当前绑定工作区仍有活动受管任务/未开始执行 | 切换工作区须先解绑再重新绑定；解绑或归档前须先关闭跟踪（会暂停相关标准与财报任务并跳过未开始执行），再执行解绑或归档 |

### 覆盖单元（绑定 / 解绑 / 归档规则）

- 同事自己创建的同名或同代码任务属于他们的个人任务，不挡住解绑或归档。
- 解绑或归档后，原工作区里剩下的任务按普通个人任务处理。
