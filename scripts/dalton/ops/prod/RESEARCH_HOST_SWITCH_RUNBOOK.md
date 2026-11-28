# Everflow 生产研究宿主服务停机手动切换与一键回滚手册

本手册面向生产值班负责人，指导在无用户使用的维护时段停机完成 Everflow 宿主机研究服务的手动切换，将 Guidepoint 令牌刷新权与已迁移研究路由交由独立研究宿主服务 `com.dalton.research-host`（监听 `127.0.0.1:8951`）接管，同时保留精简后的旧研究数据桥 `com.dalton.researchdata-bridge`（监听 `127.0.0.1:8960`）继续承载未迁移路由。异常时通过固定恢复目录中的一键回滚脚本恢复切换前状态。

## 术语说明

- **LaunchAgent**（macOS 用户级守护进程配置）：由系统 `launchd`（macOS 后台服务管理器）托管的宿主机后台服务，配置文件为 **plist**（属性列表配置文件），位于 `~/Library/LaunchAgents/`。
- **bootout / bootstrap**（卸载 / 加载服务）：`launchctl`（`launchd` 命令行管理工具）用于停止卸载服务与重新加载启动服务的子命令。
- **Docker Compose**（容器编排工具）：用于管理后端 `backend` 与网关等容器生命周期的命令行工具；编排文件列表由 `COMPOSE_FILE`（Compose 配置文件列表环境变量）及仓库默认文件统一解析。
- **MCP**（Model Context Protocol，模型上下文协议）：沙箱与宿主机研究服务之间调用检索工具的标准通信协议，请求采用 **JSON-RPC**（基于 JSON 的远程过程调用协议），响应兼容 JSON 与 **SSE**（Server-Sent Events，服务器推送事件流）。
- **OAuth**（开放授权协议）：Guidepoint 上游接口使用的访问令牌与刷新令牌认证机制，由宿主机唯一持锁进程串行刷新并原子落盘；**TTL**（Time To Live，剩余有效时长）指访问令牌距过期的剩余秒数。
- **in_flight**（在途请求数）：服务当前正在处理的请求计数；排空前需确认其归零。
- **SHA-256**（安全散列算法摘要）：快照与恢复目录清单中用于逐文件校验完整性的 64 位十六进制指纹。
- **mtime**（文件修改时间）：当令牌文件缺少 `obtained_at` 字段时用于比较新旧的兜底时间戳。
- **准入闸门**：位于仓库 `var/dalton/maintenance-admission.json` 的持久化维护状态文件；关闭后后端对新发起对话、自动化与知识入库统一返回 **HTTP**（超文本传输协议）`503`。
- **dry-run**（只读演练模式）：仅校验并打印计划执行的步骤，保持磁盘与运行态原样。

---

## 一、切换前一天（只读检查与前置准备）

在计划切换的前一天工作时段完成以下检查，全部命令均为只读或面向暂存目录，保持线上服务正常提供访问。以下命令默认在仓库根目录（`<repo>`）执行。

### 1.1 确认数据库备份最新可用

核对 `~/Backups/dalton/` 中存在当日最新的 PostgreSQL（关系型数据库）完整备份；如需立即补做一份手工备份，执行：

```bash
set -euo pipefail
ls -lht ~/Backups/dalton/dalton-pg-*.dump | head -n 5 && \
  bash scripts/dalton/ops/prod/backup.sh
```

**成功判据**：输出包含当日 `dalton-pg-*.dump` 文件且文件大小正常，末尾打印 `ok`。

### 1.2 确认凭据文件与三份 plist 模板渲染就绪

1. 核对 `~/.config/dalton/research-host.env` 权限为 `0600`，且包含以下 5 项独立凭据：`DALTON_RESEARCH_HOST_TOKEN`、`DALTON_RESEARCH_ADMIN_TOKEN`、`DALTON_BRIDGE_TOKEN`、`GUIDEPOINT_CLIENT_ID`、`GUIDEPOINT_CLIENT_SECRET`。
2. 使用统一渲染入口将三份 LaunchAgent 配置文件渲染到暂存目录 `var/dalton/cutover-snapshots/research-host/staged-plists/`（保持线上 `~/Library/LaunchAgents/` 原样）：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/bridge_subprocess_env.py render-stage-a-plists \
  --output-dir var/dalton/cutover-snapshots/research-host/staged-plists \
  --repo-root "$(pwd)" \
  --home "${HOME}"
```

**成功判据**：命令返回退出码 `0` 并输出 JSON，目录中生成 `com.dalton.guidepoint-mcp-proxy.plist`、`com.dalton.researchdata-bridge.plist`、`com.dalton.research-host.plist` 三份文件。

### 1.3 运行只读预检

依次运行基础环境预检与完整切换前预检（跳过双链时间预算文件校验；两项命令均通过 `--research-host-plist` 读取暂存目录中的新服务配置）：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py --stage-a \
  --research-host-plist var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
  --report-json && \
  bash scripts/dalton/ops/prod/switch_research_host.sh --check \
    --skip-budgets \
    --research-host-plist var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
    --report-json
```

**成功判据**：两段输出中顶层 `"ok": true`，所有检查项均为 `"ok"`（或已确认可接受的 `"warn"`），涵盖 `:8943` 进程 **PID**（进程标识符）核对、三份 plist 路径格式、Guidepoint 令牌文件、仓库虚拟环境依赖、前端版本指针 `web/releases/current`、三端能力一致性与环境变量覆盖扫描。

### 1.4 记录切换前一天现网状态快照

将现网完整配置与状态原子写入 `var/dalton/cutover-snapshots/<时间>-precheck/` 并自动校验 SHA-256 清单：

```bash
set -euo pipefail
bash scripts/dalton/ops/prod/snapshot_research_host.sh --label precheck
```

**成功判据**：输出 `SNAPSHOT_OK: .../var/dalton/cutover-snapshots/<时间>-precheck` 与 `(SHA-256 verified)`。

---

## 二、停机切换（维护窗口内逐步执行）

请在无人使用的维护时段按顺序执行以下 10 步。执行前先在当前终端记录用户域标识：

```bash
DOMAIN="gui/$(id -u)"
```

### 第 1 步：发布并校验固定恢复入口（落闸前，在线完成）

在落闸停机之前，先将当前已验证的一键回滚、快照与运维脚本原子发布至固定恢复目录 `var/dalton/cutover-snapshots/recovery-bin/`，并立即核验 SHA-256 清单完整性。本步在线完成，线上服务保持正常运行：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py recovery-publish \
  --repo "$(pwd)" \
  --src-dir "$(pwd)/scripts/dalton/ops/prod" \
  --gp-lock-src "$(pwd)/scripts/dalton/research_runtime/guidepoint_lock.py" \
  --source-commit "$(git rev-parse HEAD)" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py manifest-verify \
    --recovery-dir var/dalton/cutover-snapshots/recovery-bin
```

**成功判据**：两项命令均返回退出码 `0`，`var/dalton/cutover-snapshots/recovery-bin/recovery-manifest.json` 哈希校验通过，且 `var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh` 就绪可执行。校验通过后再进入第 2 步。

**失败处理**：本步期间准入闸门保持开放，线上服务维持正常访问；若本步报错，直接停止流程，排查目录权限、磁盘空间或源文件完整性后重新执行本步，确认通过再进入后续停机步骤。

### 第 2 步：落闸并确认后端 `:8000` 返回 `503`

关闭后端准入闸门及外部宿主机直调开关，再核对容器内文件修订号与 `:8000` 准入探针实际返回 `503`：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused true \
  --pause-host-direct true \
  --operation manual_switch \
  --reason "downtime_research_host_cutover" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
    --repo "$(pwd)" \
    --expected-paused true \
    --backend-url http://127.0.0.1:8000
```

**成功判据**：`gate-set` 输出 `"paused": true` 与 `"pause_host_direct": true`；`gate-verify` 输出 `"status": "verified"` 且 `"probe_status_code": 503`。

**失败回滚命令**：本步尚未修改服务配置，若需中止切换，直接重新开闸恢复线上访问：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused false \
  --pause-host-direct false \
  --operation manual_switch \
  --reason "abort_before_cutover"
```

### 第 3 步：等待后端与数据任务空闲

调用维护模式空闲等待脚本，确认进行中的对话回合、子智能体任务以及已领取的数据任务槽位连续两次归零：

```bash
set -euo pipefail
bash scripts/dalton/ops/prod/wait_backend_idle.sh --max-wait-seconds 600
```

> **说明**：`wait_backend_idle.sh` 仅检查数据库中的对话回合、子智能体任务与数据任务槽位，**不覆盖旧桥 `:8960` 内的知识库/卖方纪要入库（`ingest_inflight`）与桥内排队请求**；桥内入库与排队请求的排空由第 5 步先调用 `:8960/admin/drain`（超时 240 秒，覆盖 180 秒入库上限并等待 `inflight`、`queued`、`ingest_inflight` 全部归零）保证。

**成功判据**：退出码 `0`，输出 `idle (maintenance mode, checks=...) threads=0 subagents=0 data_slots=0 data_runs=0`。

**失败回滚命令**：若在窗口内持续忙碌决定改期，执行第 2 步的开闸命令即可恢复对外服务。

### 第 4 步：保存停机前完整快照并固定前端版本

在修改任何配置文件或停服前，保存本次切换的权威回滚快照（涵盖 `.env`、三份 plist、宿主机服务环境文件、实际解析路径上的 Guidepoint 令牌文件、交接日志、`web/releases/current` 指向，并在 `web/releases/.pinned-releases/` 标记保留切换前的前端发布目录），将快照路径存入 `SNAP_DIR` 并记录到值班日志中供观察期使用：

```bash
set -euo pipefail
SNAP_DIR="$(bash scripts/dalton/ops/prod/snapshot_research_host.sh --label cutover --print-path)" && \
  bash scripts/dalton/ops/prod/snapshot_research_host.sh --verify "${SNAP_DIR}" && \
  echo "SNAP_DIR=${SNAP_DIR}"
```

> **说明**：回滚时，`.env`、plist、交接日志与 `web/releases/current` 从快照还原；Guidepoint 令牌文件在停掉持锁方并确认刷新锁释放后比较 `obtained_at`（缺字段时比较 `mtime`），保留更新的现网令牌，仅在现网令牌缺失或损坏时使用快照兜底。

**成功判据**：输出 `SNAPSHOT_VERIFY_OK: <快照目录>`，终端打印非空的 `SNAP_DIR`。

**失败回滚命令**：若快照写入或校验失败，保持原服务运行，按第 2 步开闸命令恢复访问并排查缺失文件、磁盘空间或目录权限。

### 第 5 步：先排空并停止旧桥 `:8960`，再排空并停止上游 `:8943`

必须**先排空并停止旧桥 `:8960`**，再排空并停止上游 Guidepoint 代理 `:8943`（避免桥内排队请求在拿到执行名额后访问已停止的上游）。先调用 `drain-bridge`（超时设为 240 秒，覆盖 180 秒入库上限并留出余量）向 `:8960/admin/drain` 下发排空指令，等 `draining=true` 且 `inflight == 0`、`queued == 0`、`ingest_inflight == 0` 后卸载 `:8960`；再对当前刷新方 `:8943` 执行停前排空：`drain-refresher` 复用预检有效环境解析（含 `GUIDEPOINT_ENV_FILE`）读取管理员凭据，当访问令牌剩余 TTL ≤ 60 秒时在它仍是唯一持锁方时先通过完整 MCP 会话握手完成一次受控刷新并确认落盘，再向 `/admin/drain` 发送排空指令并持续确认 `/healthz` 同时满足 `draining=true`、`in_flight == 0` 且令牌目录清零 `.guidepoint_mcp_tokens.*.tmp`，随后卸载 `:8943` 并确认刷新锁 `.guidepoint.owner.lock` 已释放：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py drain-bridge \
  --repo "$(pwd)" \
  --home "${HOME}" \
  --url http://127.0.0.1:8960 \
  --timeout 240 && \
  launchctl bootout "${DOMAIN}/com.dalton.researchdata-bridge" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py drain-refresher \
    --repo "$(pwd)" \
    --home "${HOME}" \
    --target legacy \
    --url http://127.0.0.1:8943 && \
  launchctl bootout "${DOMAIN}/com.dalton.guidepoint-mcp-proxy" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py wait-lock-free \
    --repo "$(pwd)" \
    --home "${HOME}"
```

确认两个端口均已释放监听：

```bash
lsof -nP -iTCP:8960 -sTCP:LISTEN || true
lsof -nP -iTCP:8943 -sTCP:LISTEN || true
```

**成功判据**：`drain-bridge`（输出 `"draining": true`、`"inflight": 0`、`"queued": 0`、`"ingest_inflight": 0`）、`drain-refresher`（输出 `"draining": true`、`"in_flight": 0`、`"tmp_files": 0`）与 `wait-lock-free`（输出 `"free": true`）均返回 `0`；两次 `lsof` 均为空输出，表明 `:8960` 与 `:8943` 已完全停止。

**失败说明与回滚命令**：若 `drain-bridge` 超时且 `ingest_inflight > 0`，属于**入库未结束时先等待再重跑**的情形（此时上游 `:8943` 尚未停止），请等待桥内入库结束（上限 180 秒）后再重跑本步或执行一键回滚：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 6 步：迁移 Guidepoint 令牌刷新权并切换 LaunchAgent 配置

先向交接日志写入未完成切换意图，再将 Guidepoint 令牌刷新权 `target_owner` 原子切换为 `research_host_8951`；随后移除旧代理 `plist`，安装已渲染的新版研究宿主 `plist` 与新版旧桥 `plist`：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
  --repo "$(pwd)" \
  operation=manual_switch \
  phase=migrating_token_owner \
  completed=false \
  snapshot_dir="${SNAP_DIR}" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
    --repo "$(pwd)" \
    target_owner=research_host_8951 \
    phase=owner_switched_to_8951 && \
  rm -f "${HOME}/Library/LaunchAgents/com.dalton.guidepoint-mcp-proxy.plist" && \
  cp var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
    "${HOME}/Library/LaunchAgents/com.dalton.research-host.plist" && \
  cp var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.researchdata-bridge.plist \
    "${HOME}/Library/LaunchAgents/com.dalton.researchdata-bridge.plist"
```

**成功判据**：全部命令返回 `0`；执行以下命令核对交接日志返回 `research_host_8951`：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-read \
  --repo "$(pwd)" \
  --key target_owner
```

**失败回滚命令**：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 7 步：启动 `:8951`（新研究宿主服务）并限时轮询验活

加载并启动 `com.dalton.research-host`，随后在 30 秒内轮询 `:8951/healthz`（服务健康检查端点），严格确认 `probes.guidepoint.owner_lock_held` 为 `true`：

```bash
set -euo pipefail
launchctl bootstrap "${DOMAIN}" "${HOME}/Library/LaunchAgents/com.dalton.research-host.plist" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py wait-owner \
    --target research-host \
    --url http://127.0.0.1:8951 \
    --timeout 30
```

**成功判据**：命令返回退出码 `0` 并输出 `:8951/healthz` JSON，其中 `"ok": true` 且 `probes.guidepoint.owner_lock_held` 为 `true`（`probes.guidepoint.standby` 为 `false`）。

**失败回滚命令**：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 8 步：更新 `.env` 与桥配置、拉起精简旧桥、重建后端容器并限时轮询验活

1. 在仓库根目录 `.env` 中写入研究宿主切流配置（与 `~/.config/dalton/research-host.env` 中的令牌保持一致）：
   - `DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951`
   - `DALTON_RESEARCH_HOST_TOKEN=<与 research-host.env 相同的只读 MCP 令牌>`
   - `DALTON_RESEARCH_ADMIN_TOKEN=<与 research-host.env 相同的管理令牌>`
   - `DALTON_EXTRA_PLUGINS_DIR=/app/scripts/dalton/research_runtime/extra_plugins`
2. 在 `~/.config/dalton/researchdata-bridge.env` 中将 `DALTON_GUIDEPOINT_MCP_URL` 更新为 `http://127.0.0.1:8951/mcp/guidepoint`，并写入对应的 `DALTON_RESEARCH_HOST_TOKEN`。
3. 重新加载精简后的旧研究数据桥 `:8960`（旧桥读取新 `.env` 后关闭已迁移路由并统一返回 `route_migrated`）并限时轮询 `:8960/healthz` 至就绪；随后通过 `preflight.py compose-args` 统一解析 `COMPOSE_FILE` 参数强制重建后端容器，限时轮询 `:8000/health` 至就绪后再核验闭闸状态：

```bash
set -euo pipefail
COMPOSE_LINES="$(.venv/bin/python scripts/dalton/ops/prod/preflight.py compose-args --repo "$(pwd)")" && \
  COMPOSE_ARGS=() && \
  while IFS= read -r line; do if [ -n "${line}" ]; then COMPOSE_ARGS+=("${line}"); fi; done <<< "${COMPOSE_LINES}" && \
  launchctl bootstrap "${DOMAIN}" "${HOME}/Library/LaunchAgents/com.dalton.researchdata-bridge.plist" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py wait-health \
    --url http://127.0.0.1:8960/healthz \
    --timeout 30 && \
  docker compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate backend && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py wait-health \
    --url http://127.0.0.1:8000/health \
    --timeout 60 && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
    --repo "$(pwd)" \
    --expected-paused true \
    --backend-url http://127.0.0.1:8000
```

**成功判据**：两处 `wait-health` 在时限内均返回退出码 `0` 并输出 HTTP `200` 健康响应，`gate-verify` 输出 `"status": "verified"` 且 `"probe_status_code": 503`。

**失败回滚命令**：若任一步超时或报错，立即执行回滚：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 9 步：实调验收

1. 运行服务端实调探针，一次性校验 `:8951/healthz` 的 `probes.guidepoint.owner_lock_held`、公司知识库 `company-wiki` 的 `search`、Guidepoint 的 `search_library`、旧桥 `:8960` 已迁移路由关闭返回 `route_migrated` 以及后端健康状态（令牌由脚本直接从环境文件读取，命令行参数保持零凭据）：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py probe-services \
  --repo "$(pwd)" \
  --home "${HOME}" \
  --target research-host
```

2. 对单个受控测试工作区临时放行闸门（将 `<TEST_WORKSPACE_UUID>` 替换为测试工作区的 **UUID**〔通用唯一识别码〕），同时保持 `pause_host_direct=true` 让宿主机后台直调继续处于暂停状态，仅由该测试工作区在前端界面发起包含公司知识库与 Guidepoint 检索的真实研究对话验收：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused true \
  --pause-host-direct true \
  --allow-workspace-id "<TEST_WORKSPACE_UUID>" \
  --operation manual_switch \
  --reason "single_workspace_verification"
```

**成功判据**：`probe-services` 全部探针输出 `[OK]` 且退出码为 `0`；`gate-set` 输出 `"paused": true` 与 `"pause_host_direct": true`；测试工作区研究对话成功返回公司知识库与 Guidepoint 检索结果，工具溯源正常，页面状态正常。

**失败回滚命令**：任一探针或对话验收异常时立即执行恢复目录下的一键回滚（脚本会先收回单工作区豁免并全量闭闸）：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 10 步：核对前端版本、开闸并标记切换完成

全部验收通过后，先核对实际对外提供的前端 `/version.json` 与快照记录的 `web/releases/current/version.json` 一致，再按顺序开放准入闸门、核对 `:8000` 探针恢复 `200`，并在核验通过后将交接日志标记为 `completed=true`（采用 `set -euo pipefail` 与 `&&` 串联，前一步报错时即刻中止）：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py verify-frontend-version \
  --repo "$(pwd)" \
  --snapshot-dir "${SNAP_DIR}" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
    --repo "$(pwd)" \
    --paused false \
    --pause-host-direct false \
    --operation manual_switch \
    --reason "cutover_verified" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
    --repo "$(pwd)" \
    --expected-paused false \
    --backend-url http://127.0.0.1:8000 && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
    --repo "$(pwd)" \
    phase=completed \
    completed=true
```

**成功判据**：`verify-frontend-version` 返回 `"ok": true`；`gate-verify` 输出 `"status": "verified"` 且 `"probe_status_code": 200`；`journal-set` 退出码 `0`。保留 `${SNAP_DIR}` 目录与 `web/releases/.pinned-releases/` 保留标记直至观察期结束（观察期结束后再清理保留标记以恢复常规版本轮转）。

**失败处理与恢复命令**：

- **情形 A：`verify-frontend-version`、`gate-set` 或 `gate-verify` 失败**（交接日志仍处于未完成态）：立即执行恢复目录下的一键回滚命令（脚本第一步先落闸并核验闭闸，再恢复切换前服务）：

```bash
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

- **情形 B：`gate-set` 与 `gate-verify` 已通过（准入闸门已开放），但 `journal-set` 写入失败**：先检查当前准入状态与交接日志内容，排除磁盘空间或目录权限问题后补写交接日志并读回核验 `completed` 为 `true`、`target_owner` 为 `research_host_8951`：

```bash
set -euo pipefail
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-status --repo "$(pwd)" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-read --repo "$(pwd)" && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
    --repo "$(pwd)" \
    phase=completed \
    completed=true && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-read \
    --repo "$(pwd)" \
    --key completed && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py journal-read \
    --repo "$(pwd)" \
    --key target_owner
```

若排查后决定终止切换并退回切换前状态，也可直接执行情形 A 中的一键回滚命令。

---

## 三、回滚

### 3.1 触发回滚的情形

在切换过程中或观察期内出现以下任一情况，立即执行一键回滚：

- `com.dalton.research-host`（`:8951`）启动失败、健康检查失败，或 `probes.guidepoint.owner_lock_held` 为 `false`（未能持有 Guidepoint 令牌刷新锁）。
- 仓库 `.env` 更新或后端容器重建失败，导致 `:8000/health` 在维护窗口内超时仍待就绪。
- 实调验收中公司知识库 `search`、Guidepoint `search_library` 或真实研究对话出现权限拒绝、令牌刷新失败或检索结果异常。
- 精简旧桥 `:8960` 的保留路由在切换后出现异常。

### 3.2 执行一键回滚

从固定恢复目录 `var/dalton/cutover-snapshots/recovery-bin/` 执行一键回滚（即使工作树被回退仍可独立运行）。先运行只读演练模式核对将要恢复的快照路径与步骤，确认无误后执行正式回滚；**观察期内回滚必须显式传入第 4 步记录的 `SNAP_DIR`**（紧急情况下省略目录参数时，脚本会跳过校验失败的目录，自动选取 `var/dalton/cutover-snapshots/` 下最新的有效快照）：

```bash
set -euo pipefail
bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh --dry-run "${SNAP_DIR}" && \
  bash var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh "${SNAP_DIR}"
```

> 固定恢复目录已在停机切换第 1 步落闸前发布并校验就绪，所有停机步骤失败后均可直接从该目录执行回滚；如需应急，也可直接运行仓库内的 `bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"`。

一键回滚脚本按以下顺序幂等执行，支持在修复环境问题后重复运行：

1. 先在宿主机侧落闸并读回确认维护准入闸门已关闭（在快照校验与 Compose 参数解析之前完成；数据库连接失败时自动复用 `--no-db-lock` 应急写入并读回验证），随后校验快照目录 SHA-256 清单并在停服前解析快照指向确认旧前端目标发布目录存在且 `version.json` 匹配（自动选择快照时采用相同检查），再优先使用快照中的 `.env` 令牌校验当前运行的后端闭闸状态（遇 HTTP `401` 时回退使用当前配置 `.env` 核验，两者均失败时停在闭闸），并默认独立执行数据库空闲检查（注：`wait_backend_idle.sh` 不覆盖旧桥入库，仅在显式传入 `--skip-idle-wait` 时跳过）。
2. 先调用旧桥 `:8960/admin/drain`（默认超时 240 秒，覆盖 180 秒入库上限；当前配置管理令牌返回 `401` 时使用已校验快照中的管理令牌重试；仍失败或超时则在停止任何刷新服务之前退出，入库未结束时先等待再重跑），等 `draining=true` 且 `inflight == 0`、`queued == 0`、`ingest_inflight == 0` 后再卸载 `:8960` 并确认端口释放；随后对 `com.dalton.research-host`（`:8951`）执行停前排空（仅在服务已从 `launchd` 卸载且进程表无匹配进程时跳过排空；仍登记于 `launchd` 或进程存活时继续限时等待，存活时在 TTL ≤ 60 秒条件下先受控刷新并确认落盘，再发送 `/admin/drain` 并确认 `/healthz` 同时满足 `draining=true`、`in_flight == 0` 且无 `.guidepoint_mcp_tokens.*.tmp`）并卸载确认端口停止响应。
3. 排空并停止 `:8943`；确认 `.guidepoint.owner.lock` 已释放；从快照原子还原 `.env`、三份 `plist`、宿主机环境文件、交接日志与 `web/releases/current` 符号链接（目标发布目录及 `version.json` 已在停服前核对，还原前再次复核；Guidepoint 令牌文件比较 `obtained_at` 或 `mtime`，保留更新的现网令牌，仅在缺失或损坏时用快照兜底）。
4. 重新加载并启动旧 Guidepoint 代理（`:8943`）与旧研究数据桥（`:8960`），分别限时轮询确认 `:8943` 已持锁、`:8960/healthz` 健康就绪。
5. 通过 `preflight.py compose-args` 按还原后的 `.env`（认 `COMPOSE_FILE`）解析编排参数，强制重建 `backend` 容器，限时轮询 `:8000/health` 健康就绪后再核验闭闸状态。
6. 实调旧桥 Wiki `search`、旧桥 Guidepoint `search_library` 与 `:8943/mcp` 直调（采用完整 MCP 会话握手与 JSON/SSE 响应解码）验证业务可用，并在开闸前核对实际提供的前端 `/version.json` 与还原后的快照版本一致。
7. 全部探针与前端版本核对通过后开放准入闸门并核验；开闸命令报错或开闸校验失败均进入重新落闸并读回验证路径（数据库连接失败时自动通过 `--no-db-lock` 应急写入关闭状态并读回验证），重闭闸本身失败时明确报错提示人工闭闸。

### 3.3 回滚后验收

一键回滚输出 `ROLLBACK_OK` 后，执行以下复核确认线上完全回到切换前状态：

```bash
set -euo pipefail
lsof -nP -iTCP:8951 -sTCP:LISTEN || true
curl -fsS http://127.0.0.1:8943/healthz | .venv/bin/python -m json.tool && \
  curl -fsS http://127.0.0.1:8960/healthz | .venv/bin/python -m json.tool && \
  curl -fsS http://127.0.0.1:8000/health && \
  .venv/bin/python scripts/dalton/ops/prod/preflight.py probe-services \
    --repo "$(pwd)" \
    --home "${HOME}" \
    --target legacy
```

**成功判据**：

- `:8951` 端口已停止监听（`lsof` 输出为空）；
- `:8943/healthz` 返回 HTTP `200` 且 `owner_lock_held` 为 `true`；
- `:8960/healthz` 与 `:8000/health` 均返回 HTTP `200`；
- `probe-services --target legacy` 全部探针输出 `[OK]`；
- 在前端界面发起一次常规研究对话，确认公司知识库与 Guidepoint 检索正常返回结果。
