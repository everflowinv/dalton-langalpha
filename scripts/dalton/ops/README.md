# Dalton Offsite Backup (`scripts/dalton/ops/`)

Dalton 配置的 GitHub 私有仓库备份（B2）、每日数据库流式加密上传（B3b）与每周数据包纯流式加密上传（B3：`<产生数据> | openssl | rclone rcat` 直传 Google Drive，临时与缓存目录均验证在外置盘上，不落内置盘）。

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
- `com.dalton.config-git-backup.plist`：每 6 小时（00:15 / 06:15 / 12:15 / 18:15）执行 `dalton_config_git_backup.sh`
- `com.dalton.db-offsite.plist`：每天 03:40 执行 `dalton_db_offsite.sh`（将最新完整 `dalton-pg-*.dump` 流式加密上传至 `<remote>:DaltonBackup/db/<日期>/`，先上传 `.partial-<随机>` 并带 `--size`，核对远端大小与 MD5 后原子 `moveto` 为正式 `.enc`，失败自动清理临时对象；保留 `DALTON_DB_RETAIN_COUNT` 默认 7 份；上传前检查 `rclone about` 剩余空间不少于 2 倍）
- `com.dalton.data-backup.plist`：每周日 05:00 执行 `dalton_data_backup.sh`（MinIO 卷、沙箱 `/home/workspace`、Redis 快照经管道流式加密与 `rclone rcat` 上传 `.partial-<随机>`，核对远端大小与 MD5 后 `moveto` 为正式 `.enc`，保留 4 周，不再重复包含 Postgres）

首次安装后可手动运行一次并查看日志 `~/Library/Logs/dalton-offsite-backup.log` 与 `~/Library/Logs/dalton-offsite-backup-alerts.log`：
```bash
bash scripts/dalton/ops/dalton_config_git_backup.sh
bash scripts/dalton/ops/dalton_db_offsite.sh
bash scripts/dalton/ops/dalton_data_backup.sh
```

## 5. 告警文件与 Gateway 通知说明（B5）

1. **本地告警日志**：`DALTON_OPS_ALERTS_FILE` 在 `dalton-channel-gateway` 中指向严格 JSON 配置（`ops-alerts.json`），脚本不会向 `.json` 文件追加文本行，而是写入 `DALTON_BACKUP_ALERTS_FILE`（默认 `~/Library/Logs/dalton-offsite-backup-alerts.log`）。
2. **Gateway 飞书告警（`backup_failed`）**：`dalton-channel-gateway` 的 `POST /internal/ops-alerts` 支持 `{"code":"backup_failed","detail":"<一行说明>"}`，复用 `ops-alerts.json` 中的 `targets`，默认模板为 `Dalton 备份失败：{detail}`（老配置缺少 `messages.backup_failed` 时自动使用该缺省模板）。在 `~/.config/dalton/offsite-backup.env`（`0600`）中配置 `DALTON_CHANNEL_GATEWAY_URL` 与 `INTERNAL_SERVICE_TOKEN` 后，任何显式失败或未捕获的非零退出（`trap ERR/EXIT`）都会**先发送告警**再写日志（令牌通过标准输入传给 `curl -K -`，不打印、不进命令行参数；日志写入失败不会阻断告警，且原退出码保持不变）。
