# Everflow 生产转写服务切换与回退操作手册（第 2 批 `dalton-transcription`）

本手册对应设计定稿 [researchdata-connectors.md](../../../../docs/dalton/features/researchdata-connectors.md) §6.3，用于将 Everflow 生产转写由旧桥 `com.dalton.researchdata-bridge`（`:8960`）切换至独立服务 `com.dalton.transcription`（`:8965`），并提供完整回退顺序。

自动化辅助脚本：`scripts/dalton/ops/prod/switch_transcription_service.sh`（支持 `--check`、`--drain-bridge`、`--drain-service`、`--switch --dry-run`、`--switch`、`--rollback`）。

---

## 0. 切换前准备

1. **确认路径一致**：生产 `.env` 未设置 `DALTON_ASR_JOBS_DIR` / `DALTON_ASR_USAGE_FILE`，默认路径为：
   - 作业目录：`<repo>/var/dalton/asr_jobs`
   - 用量账本：`<repo>/var/dalton/asr_jobs/asr_usage.jsonl`
   `scripts/dalton/launchd/com.dalton.transcription.plist.template` 已显式写入上述完全一致的路径。
2. **创建独立服务环境文件** `~/.config/dalton/transcription.env`（权限 `0600`）：
   ```bash
   umask 077
   cat > ~/.config/dalton/transcription.env <<'EOF'
   DALTON_TRANSCRIPTION_TOKEN=<openssl rand -hex 32>
   DALTON_ASR_STORAGE_ENDPOINT_URL=http://127.0.0.1:9000
   DALTON_ASR_STORAGE_BUCKET_NAME=<现有转写存储桶名>
   DALTON_ASR_STORAGE_ACCESS_KEY_ID=<Access Key>
   DALTON_ASR_STORAGE_SECRET_ACCESS_KEY=<Secret Key>
   SEED_ASR_APP_ID=<现有 Seed-ASR App ID>
   SEED_ASR_ACCESS_KEY=<现有 Seed-ASR Access Key>
   VOLC_TOS_AK=<现有 TOS AK>
   VOLC_TOS_SK=<现有 TOS SK>
   VOLC_TOS_BUCKET=<现有 TOS Bucket>
   EOF
   chmod 600 ~/.config/dalton/transcription.env
   ```
3. **安装 LaunchAgent plist**（替换 `__PYTHON_BIN__`、`__REPO__`、`__HOME__`、`__LOG_DIR__` 后写入 `~/Library/LaunchAgents/com.dalton.transcription.plist`，先暂不 bootstrap，待旧桥释放 `.owner.lock` 后再启动）。
4. **先部署包含锁前置与新服务代码的版本**（旧桥需包含本次 `DALTON_BRIDGE_ASR_DISABLED` 与 `/transcriptions/drain` 改造），先用 `--check` 与 `--switch --dry-run` 预览。

---

## 1. 切换步骤顺序（§6.3）

可直接运行：

```bash
bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch --dry-run
bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch
```

或按以下 4 步手工执行：

1. **停止旧桥接新单（drain）并等待在途任务落盘**：
   ```bash
   curl -sf -X POST -H "Authorization: Bearer ${DALTON_BRIDGE_ADMIN_TOKEN}" \
     -H "Content-Type: application/json" -d '{"drain":true}' \
     http://127.0.0.1:8960/transcriptions/drain
   # 轮询至 active_jobs == 0（ready_for_handoff: true）
   curl -sf -H "Authorization: Bearer ${DALTON_BRIDGE_ADMIN_TOKEN}" \
     http://127.0.0.1:8960/transcriptions/drain
   ```
2. **禁用旧桥转写并重启旧桥释放 `.owner.lock`**：
   - 在 `~/.config/dalton/researchdata-bridge.env` 写入 `DALTON_BRIDGE_ASR_DISABLED=true`；
   - 重启旧桥：`launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge"`；
   - 验证旧桥不再持锁且返回 `503`：
     ```bash
     curl -i -H "Authorization: Bearer ${DALTON_BRIDGE_ADMIN_TOKEN}" \
       http://127.0.0.1:8960/transcriptions/health-probe
     ```
3. **启动 `com.dalton.transcription`（`:8965`）拿锁恢复**：
   ```bash
   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.dalton.transcription.plist
   curl -sf http://127.0.0.1:8965/healthz | python3 -m json.tool
   ```
   确认返回 `status: "ok"`、`owner_lock_held: true`、`ffmpeg_available: true`、`ffprobe_available: true`。
4. **更新仓库 `.env` 并重建后端容器（E16，必须重建而非 `docker restart`）**：
   - 在仓库 `.env` 写入：
     ```dotenv
     DALTON_TRANSCRIPTION_SERVICE_URL=http://host.docker.internal:8965
     DALTON_TRANSCRIPTION_TOKEN=<与 ~/.config/dalton/transcription.env 一致>
     ```
   - 重建后端容器：
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend
     ```

### 切换后验收

- 查询切换前已完成的历史任务状态、结果与 `/result/raw` 流式下载正常；
- 切换前中断的在途任务已由新服务自动恢复至完成；
- 新提交、取消、重试及 `var/dalton/asr_jobs/asr_usage.jsonl` 用量追加正常。

---

## 2. 回退步骤顺序（§6.3）

可直接运行：

```bash
bash scripts/dalton/ops/prod/switch_transcription_service.sh --rollback --dry-run
bash scripts/dalton/ops/prod/switch_transcription_service.sh --rollback
```

或按以下 3 步手工执行：

1. **停止新服务接单（drain）、等待落盘并停止服务释放 `.owner.lock`**：
   ```bash
   curl -sf -X POST -H "Authorization: Bearer ${DALTON_TRANSCRIPTION_TOKEN}" \
     -H "Content-Type: application/json" -d '{"drain":true}' \
     http://127.0.0.1:8965/transcriptions/drain
   # 也可发送 SIGUSR1 触发排水：kill -USR1 <pid>
   # 确认 active_jobs == 0 后停止服务
   launchctl bootout "gui/$(id -u)/com.dalton.transcription"
   ```
2. **恢复旧桥转写并重启拿锁恢复**：
   - 在 `~/.config/dalton/researchdata-bridge.env` 写入 `DALTON_BRIDGE_ASR_DISABLED=false`；
   - 重启旧桥：`launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge"`；
   - 核对 `curl -sf http://127.0.0.1:8960/healthz` 中 `asr.disabled` 为 `false`。
3. **恢复 `.env` 转写配置并重建后端容器（E16）**：
   - 将仓库 `.env` 的 `DALTON_TRANSCRIPTION_SERVICE_URL` 改回 `http://host.docker.internal:8960`，`DALTON_TRANSCRIPTION_TOKEN` 设为旧桥 `DALTON_BRIDGE_ADMIN_TOKEN`（或回退到切流前 `.env` 与后端版本）；
   - 执行重建：
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend
     ```
