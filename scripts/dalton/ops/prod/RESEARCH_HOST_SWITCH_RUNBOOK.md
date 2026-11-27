# Everflow 生产研究宿主服务停机手动切换与一键回滚手册

本手册面向生产值班负责人，指导在无用户使用的维护时段停机完成 Everflow 宿主机研究服务的手动切换，将 Guidepoint 令牌刷新权与已迁移研究路由交由独立研究宿主服务 `com.dalton.research-host`（监听 `127.0.0.1:8951`）接管，同时保留精简后的旧研究数据桥 `com.dalton.researchdata-bridge`（监听 `127.0.0.1:8960`）继续承载未迁移路由。异常时通过一键回滚脚本恢复切换前快照。

## 术语说明

- **LaunchAgent**（macOS 用户级守护进程配置）：由系统 `launchd`（macOS 后台服务管理器）托管的宿主机后台服务，配置文件为 **plist**（属性列表配置文件），位于 `~/Library/LaunchAgents/`。
- **bootout / bootstrap**（卸载 / 加载服务）：`launchctl`（`launchd` 命令行管理工具）用于停止卸载服务与重新加载启动服务的子命令。
- **Docker Compose**（容器编排工具）：用于管理后端 `backend` 与网关等容器生命周期的命令行工具。
- **MCP**（Model Context Protocol，模型上下文协议）：沙箱与宿主机研究服务之间调用检索工具的标准通信协议，请求采用 **JSON-RPC**（基于 JSON 的远程过程调用协议）。
- **OAuth**（开放授权协议）：Guidepoint 上游接口使用的访问令牌与刷新令牌认证机制，由宿主机唯一持锁进程串行刷新并落盘。
- **SHA-256**（安全散列算法摘要）：快照清单中用于逐文件校验完整性的 64 位十六进制指纹。
- **准入闸门**：位于仓库 `var/dalton/maintenance-admission.json` 的持久化维护状态文件；关闭后后端对新发起对话、自动化与知识入库统一返回 **HTTP**（超文本传输协议）`503`。
- **dry-run**（只读演练模式）：仅校验并打印计划执行的步骤，保持磁盘与运行态原样。

---

## 一、切换前一天（只读检查与前置准备）

在计划切换的前一天工作时段完成以下检查，全部命令均为只读或面向暂存目录，保持线上服务正常提供访问。以下命令默认在仓库根目录（`<repo>`）执行。

### 1.1 确认数据库备份最新可用

核对 `~/Backups/dalton/` 中存在当日最新的 PostgreSQL（关系型数据库）完整备份；如需立即补做一份手工备份，执行：

```bash
ls -lht ~/Backups/dalton/dalton-pg-*.dump | head -n 5
bash scripts/dalton/ops/prod/backup.sh
```

**成功判据**：输出包含当日 `dalton-pg-*.dump` 文件且文件大小正常，末尾打印 `ok`。

### 1.2 确认凭据文件与三份 plist 模板渲染就绪

1. 核对 `~/.config/dalton/research-host.env` 权限为 `0600`，且包含以下 5 项独立凭据：`DALTON_RESEARCH_HOST_TOKEN`、`DALTON_RESEARCH_ADMIN_TOKEN`、`DALTON_BRIDGE_TOKEN`、`GUIDEPOINT_CLIENT_ID`、`GUIDEPOINT_CLIENT_SECRET`。
2. 使用统一渲染入口将三份 LaunchAgent 配置文件渲染到暂存目录 `var/dalton/cutover-snapshots/research-host/staged-plists/`（保持线上 `~/Library/LaunchAgents/` 原样）：

```bash
.venv/bin/python scripts/dalton/bridge_subprocess_env.py render-stage-a-plists \
  --output-dir var/dalton/cutover-snapshots/research-host/staged-plists \
  --repo-root "$(pwd)" \
  --home "${HOME}"
```

**成功判据**：命令返回退出码 `0` 并输出 JSON，目录中生成 `com.dalton.guidepoint-mcp-proxy.plist`、`com.dalton.researchdata-bridge.plist`、`com.dalton.research-host.plist` 三份文件。

### 1.3 运行只读预检

依次运行基础环境预检与完整切换前预检（跳过双链时间预算文件校验）：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py --stage-a \
  --research-host-plist var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
  --report-json
bash scripts/dalton/ops/prod/switch_research_host.sh --check \
  --skip-budgets \
  --research-host-plist var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
  --report-json
```

**成功判据**：两段输出中顶层 `"ok": true`，所有检查项均为 `"ok"`（或已确认可接受的 `"warn"`），涵盖 `:8943` 进程 **PID**（进程标识符）核对、三份 plist 路径格式、Guidepoint 令牌文件、仓库虚拟环境依赖、前端版本指针 `web/releases/current`、三端能力一致性与环境变量覆盖扫描。

### 1.4 记录切换前一天现网状态快照

将现网完整配置与状态原子写入 `var/dalton/cutover-snapshots/<时间>-precheck/` 并自动校验 SHA-256 清单：

```bash
bash scripts/dalton/ops/prod/snapshot_research_host.sh --label precheck
```

**成功判据**：输出 `SNAPSHOT_OK: .../var/dalton/cutover-snapshots/<时间>-precheck` 与 `(SHA-256 verified)`。

---

## 二、停机切换（维护窗口内逐步执行）

请在无人使用的维护时段按顺序执行以下 9 步。执行前先在当前终端记录用户域标识：

```bash
DOMAIN="gui/$(id -u)"
```

### 第 1 步：落闸并确认后端 `:8000` 返回 `503`

关闭后端准入闸门及外部宿主机直调开关，再核对容器内文件修订号与 `:8000` 准入探针实际返回 `503`：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused true \
  --pause-host-direct true \
  --operation manual_switch \
  --reason "downtime_research_host_cutover"

.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
  --repo "$(pwd)" \
  --expected-paused true \
  --backend-url http://127.0.0.1:8000
```

**成功判据**：`gate-set` 输出 `"paused": true` 与 `"pause_host_direct": true`；`gate-verify` 输出 `"status": "verified"` 且 `"probe_status_code": 503`。

**失败回滚命令**：若本步失败且尚未改动服务配置，直接重新开闸恢复线上访问：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused false \
  --pause-host-direct false \
  --operation manual_switch \
  --reason "abort_before_cutover"
```

### 第 2 步：等待后端与数据任务空闲

调用维护模式空闲等待脚本，确认进行中的对话回合、子智能体任务以及已领取的数据任务槽位连续两次归零：

```bash
bash scripts/dalton/ops/prod/wait_backend_idle.sh --max-wait-seconds 600
```

**成功判据**：退出码 `0`，输出 `idle (maintenance mode, checks=...) threads=0 subagents=0 data_slots=0 data_runs=0`。

**失败回滚命令**：若在窗口内持续忙碌决定改期，执行第 1 步的开闸命令即可恢复对外服务。

### 第 3 步：保存停机前完整快照

在修改任何配置文件或停服前，保存本次切换的权威回滚快照（涵盖 `.env`、三份 plist、宿主机服务环境文件、Guidepoint 令牌文件、交接日志、`web/releases/current` 指向），并将路径存入当前终端变量 `SNAP_DIR`：

```bash
SNAP_DIR="$(bash scripts/dalton/ops/prod/snapshot_research_host.sh --label cutover --print-path)"
bash scripts/dalton/ops/prod/snapshot_research_host.sh --verify "${SNAP_DIR}"
echo "SNAP_DIR=${SNAP_DIR}"
```

**成功判据**：输出 `SNAPSHOT_VERIFY_OK: <快照目录>`，终端已打印非空的 `SNAP_DIR`。

**失败回滚命令**：若快照写入或校验失败，保持原服务运行，按第 1 步开闸命令恢复访问并排查磁盘空间与目录权限。

### 第 4 步：刷新恢复工具目录并停止 `:8943` 与旧桥 `:8960`

先将当前已验证的运维脚本原子发布至固定恢复目录 `var/dalton/cutover-snapshots/recovery-bin/`，再依次停止旧 Guidepoint 代理（`:8943`）与旧研究数据桥（`:8960`）：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py recovery-publish \
  --repo "$(pwd)" \
  --src-dir "$(pwd)/scripts/dalton/ops/prod" \
  --gp-lock-src "$(pwd)/scripts/dalton/research_runtime/guidepoint_lock.py" \
  --source-commit "$(git rev-parse HEAD)"

launchctl bootout "${DOMAIN}/com.dalton.guidepoint-mcp-proxy"
launchctl bootout "${DOMAIN}/com.dalton.researchdata-bridge"
```

确认两个端口均已释放监听：

```bash
lsof -nP -iTCP:8943 -sTCP:LISTEN || true
lsof -nP -iTCP:8960 -sTCP:LISTEN || true
```

**成功判据**：`recovery-publish` 退出码为 `0`；两次 `lsof` 均为空输出，表明 `:8943` 与 `:8960` 已完全停止。

**失败回滚命令**：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 5 步：迁移 Guidepoint 令牌刷新权并切换 LaunchAgent 配置

先向交接日志写入未完成切换意图，再将 Guidepoint 令牌刷新权 `target_owner` 原子切换为 `research_host_8951`；随后移除旧代理 `plist`，安装已渲染的新版研究宿主 `plist` 与新版旧桥 `plist`：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
  --repo "$(pwd)" \
  operation=manual_switch \
  phase=migrating_token_owner \
  completed=false \
  snapshot_dir="${SNAP_DIR}"

.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
  --repo "$(pwd)" \
  target_owner=research_host_8951 \
  phase=owner_switched_to_8951

rm -f "${HOME}/Library/LaunchAgents/com.dalton.guidepoint-mcp-proxy.plist"
cp var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.research-host.plist \
  "${HOME}/Library/LaunchAgents/com.dalton.research-host.plist"
cp var/dalton/cutover-snapshots/research-host/staged-plists/com.dalton.researchdata-bridge.plist \
  "${HOME}/Library/LaunchAgents/com.dalton.researchdata-bridge.plist"
```

**成功判据**：两次 `journal-set` 均返回 `0`；执行以下命令核对交接日志返回 `research_host_8951`：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-read \
  --repo "$(pwd)" \
  --key target_owner
```

**失败回滚命令**：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 6 步：启动 `:8951`（新研究宿主服务）并验活

加载并启动 `com.dalton.research-host`，随后检查 `:8951/healthz`（服务健康检查端点）：

```bash
launchctl bootstrap "${DOMAIN}" "${HOME}/Library/LaunchAgents/com.dalton.research-host.plist"
curl -fsS http://127.0.0.1:8951/healthz | .venv/bin/python -m json.tool
```

**成功判据**：`:8951/healthz` 返回 HTTP `200`，JSON 中 `"ok": true` 且 `guidepoint.owner_lock_held` 为 `true`。

**失败回滚命令**：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 7 步：更新 `.env` 与桥配置、拉起精简旧桥并重建后端容器

1. 在仓库根目录 `.env` 中写入研究宿主切流配置（与 `~/.config/dalton/research-host.env` 中的令牌保持一致）：
   - `DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951`
   - `DALTON_RESEARCH_HOST_TOKEN=<与 research-host.env 相同的只读 MCP 令牌>`
   - `DALTON_RESEARCH_ADMIN_TOKEN=<与 research-host.env 相同的管理令牌>`
   - `DALTON_EXTRA_PLUGINS_DIR=/app/scripts/dalton/research_runtime/extra_plugins`
2. 在 `~/.config/dalton/researchdata-bridge.env` 中将 `DALTON_GUIDEPOINT_MCP_URL` 更新为 `http://127.0.0.1:8951/mcp/guidepoint`，并写入对应的 `DALTON_RESEARCH_HOST_TOKEN`。
3. 重新加载精简后的旧研究数据桥 `:8960`（此时旧桥读取新 `.env`，自动关闭已迁移路由并统一返回 `route_migrated`），并强制重建后端容器以加载新 `.env`：

```bash
launchctl bootstrap "${DOMAIN}" "${HOME}/Library/LaunchAgents/com.dalton.researchdata-bridge.plist"
curl -fsS http://127.0.0.1:8960/healthz | .venv/bin/python -m json.tool

docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps --force-recreate backend
curl -fsS http://127.0.0.1:8000/health
```

**成功判据**：`:8960/healthz` 与 `:8000/health` 均返回 HTTP `200`；再次确认容器内闸门仍处于关闭态：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
  --repo "$(pwd)" \
  --expected-paused true \
  --backend-url http://127.0.0.1:8000
```

**失败回滚命令**：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 8 步：实调验收

1. 运行服务端实调探针，一次性校验 `:8951/healthz` 持锁、公司知识库 `company-wiki` 的 `search`、Guidepoint 的 `search_library`、旧桥 `:8960` 已迁移路由关闭返回 `route_migrated` 以及后端健康状态（令牌由脚本直接从环境文件读取，零明文命令行参数）：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py probe-services \
  --repo "$(pwd)" \
  --home "${HOME}" \
  --target research-host
```

2. 对单个受控测试工作区临时放行闸门（将 `<TEST_WORKSPACE_UUID>` 替换为测试工作区的 **UUID**〔通用唯一识别码〕），在前端界面发起一次包含公司知识库与 Guidepoint 检索的真实研究对话验收：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused true \
  --pause-host-direct false \
  --allow-workspace-id "<TEST_WORKSPACE_UUID>" \
  --operation manual_switch \
  --reason "single_workspace_verification"
```

**成功判据**：`probe-services` 全部探针输出 `[OK]` 且退出码为 `0`；测试工作区研究对话成功返回公司知识库与 Guidepoint 检索结果，工具溯源正常，页面无报错。

**失败回滚命令**：任一探针或对话验收异常时立即执行一键回滚（脚本会自动先收回单工作区豁免并全量闭闸）：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

### 第 9 步：开闸并标记切换完成

全部验收通过后，开放准入闸门、核对 `:8000` 探针恢复 `200`，并将交接日志标记为完成：

```bash
.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-set \
  --repo "$(pwd)" \
  --paused false \
  --pause-host-direct false \
  --operation manual_switch \
  --reason "cutover_verified"

.venv/bin/python scripts/dalton/ops/prod/preflight.py gate-verify \
  --repo "$(pwd)" \
  --expected-paused false \
  --backend-url http://127.0.0.1:8000

.venv/bin/python scripts/dalton/ops/prod/preflight.py journal-set \
  --repo "$(pwd)" \
  phase=completed \
  completed=true
```

**成功判据**：`gate-verify` 输出 `"status": "verified"` 且 `"probe_status_code": 200`；`journal-set` 退出码 `0`。保留 `${SNAP_DIR}` 目录直至观察期结束。

---

## 三、回滚

### 3.1 触发回滚的情形

在切换过程中或观察期内出现以下任一情况，立即执行一键回滚：

- `com.dalton.research-host`（`:8951`）启动失败、健康检查失败，或 `owner_lock_held` 为 `false`（未能持有 Guidepoint 令牌刷新锁）。
- 仓库 `.env` 更新或后端容器重建失败，导致 `:8000/health` 在维护窗口内超时未恢复正常。
- 实调验收中公司知识库 `search`、Guidepoint `search_library` 或真实研究对话出现权限拒绝、令牌刷新失败或检索结果异常。
- 精简旧桥 `:8960` 的保留路由在切换后出现异常。

### 3.2 执行一键回滚

先运行只读演练模式核对将要恢复的快照路径与步骤，确认无误后执行正式回滚（省略快照目录参数时，脚本自动选取 `var/dalton/cutover-snapshots/` 下最新的有效快照）：

```bash
bash scripts/dalton/ops/prod/rollback_research_host.sh --dry-run
bash scripts/dalton/ops/prod/rollback_research_host.sh "${SNAP_DIR}"
```

一键回滚脚本按以下顺序幂等执行，支持在修复环境问题后重复运行：

1. 核对快照目录 SHA-256 清单完整性。
2. 确认维护准入闸门已关闭（若当前为开放态则先落闸），后端在线时等待在途对话与数据任务空闲。
3. 停止 `com.dalton.research-host`（`:8951`）并确认端口已停止响应。
4. 停止 `:8943` 与 `:8960`，从快照原子还原 `.env`、三份 `plist`、宿主机环境文件、Guidepoint 令牌文件、交接日志与 `web/releases/current` 指向。
5. 重新加载并启动旧 Guidepoint 代理（`:8943`）与旧研究数据桥（`:8960`）。
6. 使用 Docker Compose 强制重建 `backend` 容器并核对健康状态与闭闸状态。
7. 实调旧桥 Wiki `search` 与 Guidepoint `search_library` 验证业务可用。
8. 全部探针通过后自动开放准入闸门；若中途任一步失败，脚本自动停在闸门关闭状态并打印明确的人工排查与重跑指引。

### 3.3 回滚后验收

一键回滚输出 `ROLLBACK_OK` 后，执行以下复核确认线上完全回到切换前状态：

```bash
lsof -nP -iTCP:8951 -sTCP:LISTEN || true
curl -fsS http://127.0.0.1:8943/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8960/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8000/health
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
