# Dalton Offsite Backup (`scripts/dalton/ops/`)

Dalton 配置的 GitHub 私有仓库备份（B2）、每日数据库流式加密上传（B3b）与每周数据包纯流式加密上传（B3：`<产生数据> | openssl | rclone rcat` 直传 Google Drive，临时与缓存目录均验证在外置盘上，不落内置盘）。完整系统运维拓扑与故障处置另见 [OPERATIONS.md](../../../docs/dalton/OPERATIONS.md)。

> 注：`var/dalton/openclaw-data` 当前是 Dalton 数据任务、市场情绪看板与工作区技能资产的活跃卷目录（OpenClaw 守护进程本身已退役，仅保留数据结构）；每周包会通过 `sqlite3` 在线备份 API 对其中 SQLite 库生成一致副本后再打包。

## 1. 重要安全提示：保存加密口令到密码管理器

两套备份的加密均使用同一个口令文件（默认 `~/.config/dalton/backup.key`，权限 `0600`）：
- 首次运行脚本时若不存在会自动生成 32 字节随机口令（`openssl rand -base64 32`）。
- **`backup.key` 绝不会进入 Git 仓库或 Google Drive。**
- 生成后请立即把 `~/.config/dalton/backup.key` 的内容另存到密码管理器（1Password / Bitwarden 等）；若宿主机损毁且没有保存此口令，GitHub 中的 `.enc` 文件与 Google Drive 上的每周加密包均无法解开。

手动预生成口令（可选）：
```bash
mkdir -p ~/.config/dalton && chmod 700 ~/.config/dalton
(umask 077 && openssl rand -base64 32 > ~/.config/dalton/backup.key)
chmod 600 ~/.config/dalton/backup.key
```

## 2. 安装 rclone 与 Google Drive 一次性浏览器授权（B3）

由于宿主机内置盘剩余空间紧张（约 18 GB），Google Drive 上传**禁止使用 Google Drive 桌面版同步目录**（桌面版会在内置盘缓存上传文件），必须使用 `rclone` 直传，且将 `RCLONE_CACHE_DIR`、`TMPDIR`、日志全部指向外置盘 `/Volumes/EveSSD`。由于代理流量额度有限且本机直连 `https://www.googleapis.com` 可达，所有 `rclone` 调用前均会通过 `env -u` 清空代理环境变量（GitHub 配置备份不受此限）。

1. 由主助手安装 `rclone`：
   ```bash
   brew install rclone
   ```
2. 由用户本人在本机完成一次性的 Google Drive OAuth 浏览器授权：
   ```bash
   rclone config
   ```
   按交互提示操作：
   - 选择 `n` (New remote)
   - `name>` 填写 `gdrive`（需与 `offsite-backup.env` 中的 `DALTON_RCLONE_REMOTE` 一致）
   - `Storage>` 选择 `drive`（Google Drive）
   - `client_id` / `client_secret` 留空回车（或使用自有 OAuth Client）
   - `scope>` 选择 `1`（Full access）或 `3`（drive.file，若需读写由 rclone 创建的 `DaltonBackup/` 目录并修剪旧备份，建议 `1` 或 `drive.appfolder` 以外可管理 `DaltonBackup/` 的权限）
   - `Use web browser to automatically authenticate rclone with remote?` 选择 `y`，在弹出的浏览器窗口登录目标 Google 账号并确认授权
   - 完成后确认：
     ```bash
     rclone listremotes
     # 应看到 gdrive:
     ```

未配置 `rclone` 或远端时，`dalton_data_backup.sh` 与 `dalton_db_offsite.sh` 会直接失败并发送 `RCLONE_NOT_CONFIGURED` 告警（每周任务采用流式上传，不在本机或外置盘保留归档副本）。外置盘未挂载时两个上传脚本均直接失败并发送 `EXTERNAL_DRIVE_UNMOUNTED` 告警，不回退到内置 `/tmp`。

## 3. 初始化配置文件与 GitHub 配置备份仓库（B1 / B2）

1. 复制配置模板并按宿主机真实路径修改（权限 `0600`）：
   ```bash
   mkdir -p ~/.config/dalton && chmod 700 ~/.config/dalton
   cp scripts/dalton/ops/offsite-backup.env.example ~/.config/dalton/offsite-backup.env
   chmod 600 ~/.config/dalton/offsite-backup.env
   ```
2. 准备本地 Git 配置备份仓库并绑定由用户/主助手建好的 GitHub 私有仓库（脚本绝不自动建远端、绝不修改远端、绝不 `--force`）：
   ```bash
   mkdir -p ~/Backups/dalton-config-repo
   git -C ~/Backups/dalton-config-repo init -b main
   git -C ~/Backups/dalton-config-repo remote add origin git@github.com:<owner>/<private-repo>.git
   ```

## 4. 安装与启用 launchd 定时任务（由主助手在宿主机执行）

替换模板占位符 `__REPO_ROOT__`、`__HOME__`、`__PYTHON_BIN__` 后安装到 `~/Library/LaunchAgents/`：
- `com.dalton.config-git-backup.plist`：每 6 小时（00:15 / 06:15 / 12:15 / 18:15）执行 `dalton_config_git_backup.sh`（**配置备份只管小而常变的配置与轻量状态**；`DALTON_CONFIG_VAR_EXCLUDES` 默认排除 `venv`/`.venv`、`__pycache__`、`.dalton-freeze`、`openclaw-data/skills/*/{data,output,temp,state,events}`、`asr_jobs`、`reextract` 等大目录与产物，大数据由每周数据包负责）
- `com.dalton.db-offsite.plist`：每天 03:40 执行 `dalton_db_offsite.sh`（将最新完整 `dalton-pg-*.dump` 流式加密上传至 `<remote>:DaltonBackup/db/<日期>/`，先上传 `.partial-<随机>` 并带 `--size`，核对远端大小与 MD5 后原子 `moveto` 为正式 `.enc`，失败自动清理临时对象；保留 `DALTON_DB_RETAIN_COUNT` 默认 7 份；上传前检查 `rclone about` 剩余空间不少于 2 倍）
- `com.dalton.data-backup.plist`：每周日 05:00 执行 `dalton_data_backup.sh`（MinIO 卷、沙箱 `/home/workspace`、Redis 快照以及宿主机 `DALTON_PROD_VAR_DIR` 即 `var/dalton` 经管道流式加密与 `rclone rcat` 上传 `.partial-<随机>`，核对远端大小与 MD5 后 `moveto` 为正式 `.enc`，保留 4 周，不再重复包含 Postgres；其中 `var-dalton-<日期>.tar.gz.enc` 由 `DALTON_DATA_VAR_EXCLUDES` 排除可重建的 `venv`/`.venv`/`__pycache__`/`node_modules`/临时文件与 `run-secrets`，保留 `.dalton-freeze`、技能 `data/state/output/events`、`asr_jobs`、`reextract` 等大数据）。
  - `var-dalton` 打包前按文件头 `SQLite format 3\0`（不限扩展名）识别未排除的 SQLite 库，通过 `sqlite3` 在线备份 API 在外置盘临时 staging 目录生成一致副本，归档时排除原库及 `-wal`/`-shm`/`-journal` 并把副本按相同相对路径合入同一归档，用完即删；若 `tar` 因打包期间文件消失失败，会清理 `.partial` 后整项重试一次（重新快照并打包上传），第二次仍失败才告警。
  - 清单生成后、`tar` 读到之前才新建的 SQLite 库不会做一致副本，会按原样打包，下周备份会正常纳入快照处理。
  - 只读打开带残留 `-wal` 的库时，SQLite 需要能创建或写入 `-shm`；库所在目录不可写、`-shm` 无法创建或写入时，快照会失败并每周告警。处理办法：确认没有进程使用后，用 `sqlite3 <库> "PRAGMA wal_checkpoint(TRUNCATE);"` 打开一次。
  - **只有 SQLite 做一致副本，DuckDB / neo4j 依赖备份时段没有写入**（其他格式不处理，原样打包）。

首次安装后可手动运行一次并查看日志 `~/Library/Logs/dalton-offsite-backup.log` 与 `~/Library/Logs/dalton-offsite-backup-alerts.log`：
```bash
bash scripts/dalton/ops/dalton_config_git_backup.sh
bash scripts/dalton/ops/dalton_db_offsite.sh
bash scripts/dalton/ops/dalton_data_backup.sh
```

## 5. 告警文件与 Gateway 通知说明（B5）

1. **本地告警日志**：`DALTON_OPS_ALERTS_FILE` 在 `dalton-channel-gateway` 中指向严格 JSON 配置（`ops-alerts.json`），脚本不会向 `.json` 文件追加文本行，而是写入 `DALTON_BACKUP_ALERTS_FILE`（默认 `~/Library/Logs/dalton-offsite-backup-alerts.log`）。
2. **Gateway 飞书告警（`backup_failed`）**：`dalton-channel-gateway` 的 `POST /internal/ops-alerts` 支持 `{"code":"backup_failed","detail":"<一行说明>"}`，复用 `ops-alerts.json` 中的 `targets`，默认模板为 `Dalton 备份失败：{detail}`（老配置缺少 `messages.backup_failed` 时自动使用该缺省模板）。在 `~/.config/dalton/offsite-backup.env`（`0600`）中配置 `DALTON_CHANNEL_GATEWAY_URL` 与 `INTERNAL_SERVICE_TOKEN` 后，任何显式失败或未捕获的非零退出（`trap ERR/EXIT`）都会**先发送告警**再写日志（令牌通过标准输入传给 `curl -K -`，不打印、不进命令行参数；日志写入失败不会阻断告警，且原退出码保持不变）。

## 6. 生产部署与宿主机运维脚本（`prod/`）

`scripts/dalton/ops/prod/` 集中管理生产宿主机部署、空闲等待、每日数据库备份、看门狗与 Colima 启动脚本，所有路径均从脚本所在仓库根目录自动推导，支持通过环境变量覆盖默认值（完整部署流程与参数说明见 [OPERATIONS.md](../../../docs/dalton/OPERATIONS.md#14-生产部署)）：

| 文件 | 用途 | 关键环境变量（默认值） |
|---|---|---|
| `prod/deploy_main.sh` | 生产发布主脚本：空闲检查、触发备份、快进 `origin/dalton/main`、构建前端、重启/重建 `backend`/`crawler`、按变更重建 `edge`、健康与公开入口检查 | `DALTON_REPO_ROOT`（脚本推导）、`DALTON_PG_BACKUP_LOG`（`~/Library/Logs/dalton-pg-backup.log`）、`DALTON_EDGE_PORT`（`8088`） |
| `prod/wait_backend_idle.sh` | 轮询 Postgres `conversation_threads` 与 `subagent_runs`，连续两次间隔 60 秒均无进行中任务后返回 `0` | — |
| `prod/backup.sh` | 每日全量 `pg_dump -Fc` 并调用 `prune_backups.py --apply` 按策略修剪旧备份 | `DALTON_REPO_ROOT`（脚本推导）、`DALTON_BACKUP_DIR`（`~/Backups/dalton`）、`DALTON_PRUNE_SCRIPT`（`${DALTON_BACKUP_DIR}/prune_backups.py`） |
| `prod/watchdog.sh` | 每分钟探测 `127.0.0.1:8000/health`，连续失败 3 次重启后端容器，中断满 5 分钟及恢复时通过 Gateway `/internal/ops-alerts` 发送告警（网关不可用时改发 macOS 通知） | `DALTON_REPO_ROOT`、`DALTON_BACKUP_DIR`、`DALTON_OPS_STATE_DIR`（`~/.dalton-ops`）、`DALTON_CHANNEL_GATEWAY_ENV_FILE`（`~/.config/dalton/channel-gateway.env`） |
| `prod/colima_launch.sh` | 等待外置盘 Colima 目录就绪后在前台启动 `colima start -f`，供 `launchd` 托管 | `DALTON_COLIMA_WAIT_DIR`（`/Volumes/EveSSD/Dalton/colima/_lima`） |

`scripts/dalton/ops/prod/launchd/` 提供对应的三份 `LaunchAgent` 模板（安装到 `~/Library/LaunchAgents/` 时替换 `__REPO__`、`__HOME__`、`__LOG_DIR__` 占位符）：
- `com.dalton.backend-watchdog.plist.template`：每 60 秒执行 `prod/watchdog.sh`。
- `com.dalton.colima.plist.template`：登录后前台保活运行 `prod/colima_launch.sh`。
- `com.dalton.pg-backup.plist.template`：每天 03:00 执行 `prod/backup.sh`。
- `com.dalton.portwatch.plist.template`：每 300 秒执行 `prod/portwatch.sh`，记录临时端口（49152–65535）占用和占用最多的进程，写入 `~/Library/Logs/dalton-portwatch/portwatch.jsonl`；占用超过 60% 时另存一份完整快照，便于事后找出泄漏连接的进程（起因是 2026-09-07 端口耗尽事故）。

## 7. 关闭主桶前必跑引用核验（`storage_refs`）

后端启动时的主桶引用检查仅记录 `WARNING`、不会拒绝启动。由于 `check --strict` 按表与主键分批只读扫描，**严禁在主桶仍在接收写入时边写边扫**（否则已扫过的表或主键范围仍可能新增主桶引用，导致命令退出 `0` 后关桶丢失访问）。关闭主桶必须按以下顺序操作，且**从步骤 1 暂停写入起一直保持到步骤 3 配置切换完成**：

### 步骤 1：暂停并排空所有主桶写入（切勿先停 PostgreSQL）

1. **排空在途任务并暂停所有写入主桶的入口**：
   - 等待正在进行的对话轮次与子代理任务结束：
     ```bash
     bash scripts/dalton/ops/prod/wait_backend_idle.sh
     ```
   - 先卸载后端看门狗，否则它每 60 秒探测一次 `/health`，连续 3 次失败就会把刚停下的后端拉起来（Everflow 生产）：
     ```bash
     launchctl bootout gui/$(id -u)/com.dalton.backend-watchdog
     ```
     配置切换完成、`/health` 恢复正常后再装回：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.backend-watchdog.plist`。
   - 暂停会向主桶写入数据的定时/后台任务与外部写流量，并停止后端写入进程（例如 `docker compose stop backend`，或停止运行中的 `server.py` 进程；**保持 PostgreSQL 运行**供只读核验连接）。
2. **关于 `DALTON_WSFILES_WRITE_INLINE_ONLY=1` 的作用边界**：
   - 若只想提前将新的普通工作区文件（非 Git、且 `<= 100 MiB`）改写回数据库内联而不停服，可在环境变量中设置 `DALTON_WSFILES_WRITE_INLINE_ONLY=1`（或在 `src/dalton/dalton.yaml` 中设置 `workspace_files.write_inline_only: true`）并重启/重建后端。
   - **该开关不能替代停写排空**：`> 100 MiB` 工作区文件、Git 仓库文件，以及头像、聊天附件、回复图片、组件数据、技能归档、备忘录二进制等其他主桶消费者在主桶仍开启时仍会写入主桶；因此执行步骤 2 核验前仍必须按第 1 步暂停后端写入并确认排空。

### 步骤 2：在保持暂停写入状态下执行只读全量核验

确认后端写入已暂停且在途任务已排空后，执行只读全量核验并检查退出码：

```bash
python -m src.dalton.storage_refs check --strict
```

- **退出码 `0`**：全库 17 类主桶消费者（含检查点 `captured_images` 与旧 `image_capture` 记录）均未检出确认引用，方可进入步骤 3（若日志中出现头像 `avatar_url` 本站形状疑似告警，请人工复核确认无主桶头像后再切换）。
- **退出码 `1`**：已确认存在主桶对象引用（命中即停）；此时**禁止关闭主桶**，保持 `STORAGE_PROVIDER=s3` 并按需恢复服务（如仅需让新普通工作区文件改写内联，可开启 `DALTON_WSFILES_WRITE_INLINE_ONLY=1`）。
- **退出码 `2`**：检查本身失败或被中断（连接、查询、反序列化异常或 `Ctrl+C`）；保持暂停状态排障并重跑，直到退出码为 `0`（或放弃本次停桶并恢复 `STORAGE_PROVIDER=s3`）。
- **核验范围与已知局限**：`check --strict` 覆盖全库 17 类主桶消费者（精确指针表、附件与回放记录、检查点组件与图片引用、用户头像对象键与本站形状疑似告警）；缺本站或主桶地址配置时按路径形状判断回复图片，HTML 图片按 `src` 属性提取。执行时保留原存储地址环境变量，对结果有疑问时保持主桶开启。

### 步骤 3：仅在退出码 `0` 后切换配置，切换完成前保持暂停

1. 在**继续保持暂停写入**的状态下修改配置：将 `STORAGE_PROVIDER` 设为 `none`（或清空 `STORAGE_BUCKET_NAME`），并用以下命令核对最终配置中 `is_storage_enabled` 为 `false`：
   ```bash
   python -m src.dalton.storage_config show
   ```
2. 重建/启动后端容器（例如 `docker compose up -d backend`），确认 `/health` 正常且启动日志中 `storage_refs` 无命中告警后，再恢复外部流量与定时任务。
3. **禁止在核验返回 `0` 后先恢复写入再修改配置**，否则恢复期间产生的新引用会在关桶后无法读取。
