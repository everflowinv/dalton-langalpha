# Everflow 生产转写服务切换与回退操作手册（第 2 批 `dalton-transcription`）

本手册对应设计定稿 [researchdata-connectors.md](../../../../docs/dalton/features/researchdata-connectors.md) §6.3，用于将 Everflow 生产转写由旧桥 `com.dalton.researchdata-bridge`（`:8960`）切换至独立服务 `com.dalton.transcription`（`:8965`），并提供完整回退顺序。

自动化辅助脚本：`scripts/dalton/ops/prod/switch_transcription_service.sh`（支持 `--check`、`--drain-bridge`、`--drain-service`、`--switch --dry-run`、`--switch`、`--rollback`）。

---

## 0. 切换前准备

1. **先部署含本批改造的版本（部署本身保持旧桥转写连续可用）**：
   - Everflow 形态在仓库 `.env` 尚未显式设置 `DALTON_TRANSCRIPTION_SERVICE_URL` 时，后端继续请求旧桥 `http://host.docker.internal:8960` 并使用现有 `DALTON_BRIDGE_ADMIN_TOKEN`，部署后即可无缝保持线上转写与历史查询可用。
   - 待旧桥已更新至支持 `DALTON_BRIDGE_ASR_DISABLED`、`.owner.lock` 与 `/transcriptions/drain` 的版本后，再按以下步骤准备并由切换脚本完成切流。
2. **确认路径一致**：生产 `.env` 未设置 `DALTON_ASR_JOBS_DIR` / `DALTON_ASR_USAGE_FILE` 时，默认路径为：
   - 作业目录：`<repo>/var/dalton/asr_jobs`
   - 用量账本：`<repo>/var/dalton/asr_jobs/asr_usage.jsonl`
   `scripts/dalton/launchd/com.dalton.transcription.plist.template` 已显式写入上述完全一致的路径。
3. **创建独立服务环境文件** `~/.config/dalton/transcription.env`（权限 `0600`）：
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
   # 可选（若生产旧桥环境覆盖了非默认值时同步填写，留空使用代码默认值）
   # VOLC_TOS_REGION=cn-beijing
   # VOLC_TOS_ENDPOINT=tos-s3-cn-beijing.volces.com
   EOF
   chmod 600 ~/.config/dalton/transcription.env
   ```
4. **安装 LaunchAgent plist**：
   - 从 `scripts/dalton/launchd/com.dalton.transcription.plist.template` 替换 `__PYTHON_BIN__`、`__REPO__`、`__HOME__`、`__LOG_DIR__` 后写入 `~/Library/LaunchAgents/com.dalton.transcription.plist`（此时先保留未加载状态，待旧桥释放 `.owner.lock` 后再由切换步骤启动）。
   - `__PYTHON_BIN__` **必须**指向仓库虚拟环境 `<repo>/.venv/bin/python`（已安装 `fastapi`、`uvicorn`、`boto3`、`httpx`）。
5. **执行切换前预检与演练**：
   ```bash
   bash scripts/dalton/ops/prod/switch_transcription_service.sh --check
   bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch --dry-run
   ```
   `--check` 会检查路径、锁状态、两端 `/healthz`，并使用 plist 指定的 Python 解释器执行 `import fastapi, uvicorn, boto3, httpx` 预检。

---

## 1. 切换步骤顺序（§6.3）

可直接运行：

```bash
bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch --dry-run
bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch
```

脚本会在更新 `.env` 和重建后端之前强制完成三项核验：新服务 `/healthz` 显示已持有 `.owner.lock`、新令牌调用 `/transcriptions/drain` 鉴权通过、并成功从新服务读出作业目录内一个已完成的历史任务。任一项未通过时脚本以非 0 退出，保持 `.env` 与后端容器原状，并打印当前状态与恢复旧桥接单命令。

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
   - 验证旧桥 `http://127.0.0.1:8960/healthz` 显示 `asr.disabled: true`、`asr.owner_lock_held: false`，且转写路由返回 `503`：
     ```bash
     curl -i -H "Authorization: Bearer ${DALTON_BRIDGE_ADMIN_TOKEN}" \
       http://127.0.0.1:8960/transcriptions/health-probe
     ```
3. **启动 `com.dalton.transcription`（`:8965`）并完成三项切流前核验**：
   ```bash
   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.dalton.transcription.plist
   # (a) 健康检查确认持锁与就绪
   curl -sf http://127.0.0.1:8965/healthz | python3 -m json.tool
   # (b) 使用新令牌确认鉴权通过
   curl -sf -H "Authorization: Bearer ${DALTON_TRANSCRIPTION_TOKEN}" \
     http://127.0.0.1:8965/transcriptions/drain | python3 -m json.tool
   # (c) 使用新令牌查询作业目录内任一已完成历史任务 <sample_job_id>
   curl -sf -H "Authorization: Bearer ${DALTON_TRANSCRIPTION_TOKEN}" \
     "http://127.0.0.1:8965/transcriptions/${sample_job_id}" | python3 -m json.tool
   ```
   确认返回 `status: "ok"`、`owner_lock_held: true`、`ffmpeg_available: true`、`ffprobe_available: true`，新令牌鉴权返回 `200`，且历史已完成任务可读。
4. **更新仓库 `.env` 并重建后端容器（E16，通过重建加载新 `.env`）**：
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

脚本会在修改 `.env` 和重建后端之前确认：`launchctl bootout` 成功执行、新服务进程已停（或已停止接单、在途任务为 0 且 `.owner.lock` 已释放），以及旧桥 `/healthz` 显示 `asr.disabled: false` 且 `asr.owner_lock_held: true` 并通过旧桥令牌鉴权。任一条件未满足时脚本以非 0 退出，保持 `.env` 与后端容器原状。

或按以下 3 步手工执行：

1. **停止新服务接单（drain）、等待落盘并停止服务释放 `.owner.lock`**：
   ```bash
   curl -sf -X POST -H "Authorization: Bearer ${DALTON_TRANSCRIPTION_TOKEN}" \
     -H "Content-Type: application/json" -d '{"drain":true}' \
     http://127.0.0.1:8965/transcriptions/drain
   # 也可发送 SIGUSR1 触发排水：kill -USR1 <pid>
   # 确认 active_jobs == 0 后停止服务（bootout 须成功返回 0）
   launchctl bootout "gui/$(id -u)/com.dalton.transcription"
   # 确认新服务已停且 .owner.lock 已释放
   ```
2. **恢复旧桥转写、重启并确认旧桥已持锁**：
   - 在 `~/.config/dalton/researchdata-bridge.env` 写入 `DALTON_BRIDGE_ASR_DISABLED=false`；
   - 重启旧桥：`launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge"`；
   - 核对 `curl -sf http://127.0.0.1:8960/healthz` 中 `asr.disabled` 为 `false` **且** `asr.owner_lock_held` 为 `true`，并确认旧桥令牌调用 `GET http://127.0.0.1:8960/transcriptions/drain` 返回 `200`。
3. **恢复 `.env` 转写配置并重建后端容器（E16）**：
   - 将仓库 `.env` 的 `DALTON_TRANSCRIPTION_SERVICE_URL` 改回 `http://host.docker.internal:8960`、`DALTON_TRANSCRIPTION_TOKEN` 设为旧桥 `DALTON_BRIDGE_ADMIN_TOKEN`（或移除 `.env` 中的 `DALTON_TRANSCRIPTION_SERVICE_URL` 与 `DALTON_TRANSCRIPTION_TOKEN`，让 Everflow 后端自动回退到旧桥 `:8960` 与 `DALTON_BRIDGE_ADMIN_TOKEN`）；
   - 执行重建：
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend
     ```
