# P0 接通：平台层开发指南

版本 0.3，2026-10-18。基座 LangAlpha main `8e0dce94`，分支 `dalton/main`。对应蓝图 §4.3（模型接入）、§4.9（本地预算）、§5 P0。

**修订记录 v0.3（2026-10-18，按 owner 与 lead 的决定统一修订）：**
- 本地运行时改为 **colima**（4 CPU、5 GiB 内存，VM 磁盘和数据放 `/Volumes/EveSSD`），不再写 Docker Desktop；所有公司工作区共用一台 computer（一个沙箱，内存 3 GiB，空闲自动停）。Daytona 不用。可达性测试以 colima 为准（P0.1）。
- compose overlay 增加挂载 `workflows/`（改 workflow 不用重建镜像）和 `config/dalton/`；backend 内存上限按 5 GiB VM 下调（P0.1）。
- 主分析师模型**可切换**：默认原生 claude-oauth Opus，替补 broker 上的 grok-4.7 或现金 API key；补一句 claude-oauth 的订阅条款风险。CLI gateway 类（antigravity、muse、claude-cli）只做纯文本角色：compaction、后台摘要、审稿（P0.6）。
- P0.4 同步输出增加 `dalton.openclaw_ref`、`dalton.upstream_provider`、`dalton.family`，作为审稿家族判定的唯一依据；`role_policy.family_of()` 供 PK、PS 使用（P0.4、P0.6）。
- P0.8 增加供其他模块调用的预算服务函数 `get_budget_today()`、`get_run_charge_usd()`，字段名全文统一为 `remaining_usd`、`charge_usd`；日额度与旧 Dalton 独立，时区 Asia/Shanghai，额度数额由 owner 定。供应商配额只在 80% 告警，不设闸门。
- 迁移：所有 Dalton 表走同一条独立链 `alembic_version_dalton`，编号统一为 `d001`–`d006`（本篇 d001、d002；PK 的 d003、d005、d006；P3 的来源定位 d004）。
- OpenClaw 配置修改和网关重启已获授权（由 lead/运维执行），去掉相应的同意问题；附录 C 改为"已决事项 + 剩余 owner 问题"。
- 附录 A 补上数据篇（P0D）的钩子：`mcp-call-timeout`（MCP HTTP 调用超时可配置）和 `skills-registry`。

**0.2 版的主要变化：** 按 owner 的要求，模型接入的主路径改为新的 OpenClaw 插件 **`dalton-langalpha-broker`（broker v2）**。原方案 A（经 OpenClaw 网关的专用 `langalpha` agent）降为对照组和备选。LangAlpha 原生的 OAuth 登录和 API key 是另一条备选。

本篇只管平台层：本地环境、broker v2、LangAlpha 接入、设置页的同步按钮和健康指示、本地日预算，以及原生界面的端到端可见性。

不在本篇范围内的：
- 数据接入：复用 OpenClaw 的 researchdata MCP，经宿主机侧 HTTP 桥接，由另一篇草稿负责，这里只引用。
- "网关页面"（owner 看新项目在做什么、读成果的页面）：由知识层、治理、自主性草稿（PK）和 lead 补写的 UI 章节负责。

标注约定：
- 【待验证】：读代码推断的结论。必须先用真实调用确认，再往下写代码。
- 【新建】：路径目前不存在。
- 其余路径都已在仓库或本机确认存在（检查于 2026-10-18）。以下新文件即使行文中没有逐个标注，也都是【新建】：
  - `integrations/openclaw/dalton-langalpha-broker/**`
  - `src/dalton/**`、`scripts/dalton/**`、`tests/unit/dalton/**`
  - `web/src/pages/Settings/panels/dalton/**`、`web/src/pages/Settings/{hooks,utils}/`
  - `docker-compose.dalton.yml`、`agent_config.dalton.yaml`

---

## 0. 结论与路线（先读）

### 0.1 三条模型路径

| 路径 | 是什么 | 定位 |
|---|---|---|
| **主路径：broker v2** | 新的 OpenClaw 插件 `dalton-langalpha-broker`，在网关端口上注册 HTTP 路由 `/dalton-broker/v1/*`，对外提供 OpenAI Chat Completions 兼容接口。插件内部直接调用 OpenClaw 底层 SDK（`openclaw/plugin-sdk/llm` 的 `streamSimple`），使用 OpenClaw 已配好的模型和凭证。**不经过 OpenClaw agent 循环**：不注入系统提示，没有 bootstrap 文件，没有 OpenClaw 自带工具 | P0 主路径。先做 1–2 天 spike（P0.2），再做完整实现（P0.3，1–2 周） |
| 备选 1：LangAlpha 原生 | `claude-oauth`（Claude 订阅，manifest 已有 `claude-opus-5-5-oauth` 等）、`codex-oauth`（ChatGPT 订阅，`gpt-6.1-sol-oauth` 等），以及 `deepseek`、`z-ai`、`gemini` 等的 API key | **主分析师的默认模型 `claude-opus-5-5-oauth` 走这条**（CLI gateway 类模型不能做工具调用，见 0.3）。主分析师模型可切换，见 P0.6。broker v2 不可用时，也作为整体备选 |
| 备选 2：网关 agent（原方案 A） | `POST 18789/v1/chat/completions`，`model=openclaw/langalpha`，用 `x-openclaw-model` 选后端模型 | 只作对照组：冒烟脚本的 `--target gateway`。不进生产配置 |

### 0.2 为什么不用网关 agent（原方案 A）：读 OpenClaw 2026.10.4 源码得到的证据

源码位置：`~/.openclaw/tools/node/lib/node_modules/openclaw/dist/openai-http-C1OgQgtd.mjs`；文档 `~/.openclaw/tools/node/lib/node_modules/openclaw/docs/gateway/openai-http-api.md`。

- 每次请求都是一次完整的 OpenClaw agent 运行。`messages[]` 被压平成一条文本 prompt，历史渲染成 `User: / Assistant: / Tool:<id>:` 文本行（`buildAgentPrompt`）。我们的 system 消息接在 OpenClaw 自己的系统提示后面（作为 `extraSystemPrompt`）。
- 请求 schema 里没有 `reasoning_effort`，推理强度无法透传。
- 用量里没有 cost，也没有 cache write（`toOpenAiChatCompletionsUsage`）。
- 工具结果里的图片会丢失。

这些问题都出在 agent 这一层。broker v2 绕过 agent 层、直接调用 SDK，可以从根上避开。

### 0.3 broker v2 可行性的证据（读 SDK 得到，spike 负责实测）

- **`openclaw/plugin-sdk/llm`**（`dist/plugin-sdk/llm.js`）导出：
  - 调用：`stream`、`streamSimple`、`complete`、`completeSimple`；
  - 工具校验：`validateToolCall`、`validateToolArguments`；
  - 流与消息：`AssistantMessageEventStream`、`transformMessages`；
  - 成本与推理：`calculateCost`、`clampThinkingLevel`、`resolveOpenAIReasoningEffortMapping` 等。
- **类型定义**在 `node_modules/@openclaw/ai/dist/types-DvzmWW8J.d.mts`：
  - `Context = {systemPrompt?, messages: Message[], tools?: Tool[]}`；
  - `Message` = `UserMessage` | `AssistantMessage`（content 由 `text`、`thinking`、`toolCall{id,name,arguments,thoughtSignature?}` 组成）| `ToolResultMessage{toolCallId, toolName, content, isError}`；
  - `Tool = {name, description, parameters(JSON schema)}`；
  - `SimpleStreamOptions` 含 `maxTokens`、`reasoning`（`off|minimal|low|medium|high|xhigh|adaptive|max`）、`signal` 等；
  - 流事件有 `text_*`、`thinking_*`、`toolcall_start/delta/end`、`done`、`error`；
  - `Usage` 含 `input`、`output`、`cacheRead`、`cacheWrite`、`cost.{input,output,cacheRead,cacheWrite,total}`。
- **按指定模型取 model 和 auth：** `openclaw/plugin-sdk/simple-completion-runtime` 的 `prepareSimpleCompletionModelForAgent({cfg, agentId, modelRef: "xai/grok-4.7"})` 返回 `{model, auth, selection}`。`modelRef` 参数见 `dist/simple-completion-runtime-BYHNOuzH.mjs` 的 `resolveSimpleCompletionSelectionRequest`。
- **模型调用的权限：** `docs/plugins/sdk-runtime/models.md` 规定，插件必须由 operator 在 `plugins.entries.<id>.llm` 里显式授权（`allowModelOverride`、`allowedModels`、`allowedCompletionModels`、`allowAuthProfileOverride`）。
- **HTTP 路由：** 插件可以用 `api.registerHttpRoute({path, auth:"plugin", match:"prefix", handler})` 在网关端口上注册路由（`docs/plugins/architecture-internals/gateway-routes.md`）。
- **broker v1 可复用的部分**（`/Users/everflow/Projects/dalton-batch-cnhk/integrations/openclaw-model-broker/src/{broker,journal,auth,protocol,server}.mjs`）：
  - 认证（HMAC 和 owner-only 的 key 文件）；
  - 持久幂等日志（发出前先记 pending；崩溃后返回 `IDEMPOTENCY_INDETERMINATE`；`replayOnly`；0600 权限、原子替换）；
  - 每个 profile 的上限（`maxTokens`、`timeoutMs`、`maxConcurrent`、`maxQueued`）；
  - 服务生命周期（`api.registerService`）。

  v1 只用了 `api.runtime.llm.complete`（文本进、文本出），没有流式，也没有工具调用。

**CLI gateway 类供应商排除在工具调用之外**：`claude-cli-gateway`、`antigravity-cli-gateway`、`muse-cli-gateway` 会把工具调用压平。broker v2 把它们标为 **text-only**：只用于 compaction、后台摘要、审稿这类纯文本角色；带 `tools` 的请求直接返回 400，不做降级。订阅 Claude 当主分析师走 LangAlpha 原生 `claude-oauth`。

### 0.4 模型清单的事实（只读公开字段）

- `models.providers` 一共 8 家、27 个模型。`agents.defaults.models` 的允许列表是 25 个（缺 `xai/grok-4.3` 和 `xai/grok-build-0.1`）。v1 broker 的 `plugins.entries["dalton-openclaw-model-broker"].llm.allowedModels` 是全部 27 个。
- broker v2 的 `allowedModels` 已定：采用 P0.3 第 7 节的 16 个（允许列表内能用工具的 12 个，加 claude-cli 4 个做纯文本）；其他 text-only 模型先不开。
- 按工具能力和计费分类：

| 类别 | 模型 | 数量 |
|---|---|---|
| 可用工具、现金计费 | deepseek×1、google×4、xai×1（允许列表内）、zai×2 | 8 |
| 可用工具、订阅计费 | openai×4（ChatGPT 登录，原生 Responses） | 4 |
| text-only、订阅计费 | claude-cli×4、antigravity×5、muse×4 | 13 |

### 0.5 本地预算的事实

OSS 模式下额度检查完全不生效（`usage_limits.platform_gating_active()` 为 False）。现成的 gate 是"租约 + 心跳"模型，拒绝时发 `credit_pause` interrupt。本地预算实现成同接口的本地 port，见 P0.8。

### 0.6 时间线

- **P0.2 spike（1–2 天）是闸门。** go 之后再进入 P0.3。
- P0.3（broker v2，Node）和 P0.4/P0.7/P0.8（LangAlpha 侧，Python 和 TS）可以由两人并行：
  - LangAlpha 侧在 broker 完成前，先用原生 `claude-opus-5-5-oauth` 和网关对照组联调；
  - broker v2 一可用，切换 base_url 即可。
- 日历时间约 2–2.5 周，合计约 16–18.5 人日（见文末汇总）。

---

## P0.1 本地环境与仓库卫生

**目标：** 在本机（Mac mini，16 GB 内存、10 核）用 **colima**（本机已装 colima 0.10.3）提供的 Docker 跑起 LangAlpha 全栈（postgres、redis、backend、frontend、docker 沙箱），`HOST_MODE=oss`。沙箱只用本地 Docker，**不用 Daytona**（Daytona 在云端，访问不到宿主机上的 broker 和 researchdata 桥）。所有公司工作区共用**一台 computer**（一个沙箱）。同时定下"Dalton 代码放哪、怎么和上游合并"的规矩。

**前置依赖：**
- colima 已按下文第 0 步启动（4 CPU、5 GiB 内存，VM 磁盘和数据在 `/Volumes/EveSSD`）。2026-10-18 检查时 daemon 未运行。外置卷 `/Volumes/EveSSD` 必须已挂载。
- 已安装 `uv` 和 `pnpm`（宿主机跑脚本用）。2026-10-18 检查时宿主机没有 `uv`，需要先装（`brew install uv`）。
- OpenClaw 网关在跑（`curl 127.0.0.1:18789/v1/models` 返回 401 即说明在跑）。broker v2 装好后（P0.3），它挂在同一个端口上：`/dalton-broker/v1/*`。

**改动/新增文件：**

| 路径 | 状态 | 用途 |
|---|---|---|
| `.env` | 新建（gitignored） | 凭证和开关 |
| `~/.zprofile`（或 owner 的 shell profile） | 改【owner】 | `export COLIMA_HOME=/Volumes/EveSSD/colima`，让 colima 的 VM 磁盘和 Docker 数据落在外置卷 |
| `docker-compose.dalton.yml` | 【新建】，提交 | Dalton 的 compose 叠加：只读挂载（overlay 配置、`workflows/`、`config/dalton/`）、入口脚本、内存上限 |
| `agent_config.dalton.yaml` | 【新建】，用 `git add -f` 提交 | Dalton 的 agent 配置 overlay（`APP_ENV=dalton`），见 P0.6 |
| `scripts/dalton/docker_entrypoint_dalton.sh` | 【新建】 | 上游入口逻辑 + Dalton 迁移 |
| `scripts/dalton/check_hooks.sh` | 【新建】 | 列出并校验所有 `DALTON-HOOK` 标记 |
| `src/dalton/__init__.py`、`src/dalton/settings.py`、`src/dalton/dalton.yaml` | 【新建】 | Dalton 后端包的根、配置读取器、行为默认值 |

### 具体做法

#### 0. 启动 colima（VM 和数据放 EveSSD）

```bash
export COLIMA_HOME=/Volumes/EveSSD/colima          # 同时写进 shell profile；VM 磁盘、Docker 镜像和卷都在这里
mkdir -p "$COLIMA_HOME"
colima start --cpu 4 --memory 5 --disk 120 --vm-type vz --mount-type virtiofs
docker context use colima
docker info --format '{{.Name}} {{.OperatingSystem}} cpus={{.NCPU}} mem={{.MemTotal}}'
```

- 磁盘 120 GiB 是建议值（沙箱镜像、postgres 数据、构建缓存）；`--vm-type vz` 和 `virtiofs` 需要 macOS 13 以上【待验证本机版本，不支持就去掉这两个参数】。
- 已有默认 profile 的 VM 不在 EveSSD 上时，先 `colima delete` 再按上面重建（旧 VM 里没有需要保留的数据【待验证】）。
- `host.docker.internal` 在 colima 下是否指向 Mac 回环，由数据篇 P0-D.0 实测；需要时在 `colima start --edit` 里给 docker daemon 设 `"host-gateway-ip": "192.168.5.2"`，然后重启 colima（已获授权，由 lead/运维执行）。

#### 1. 生成 `.env`

```bash
cd /Users/everflow/Projects/dalton-langalpha
cp .env.example .env
make config        # 交互向导
```

向导里的选择：
- LLM 选"跳过"：OpenClaw 不在向导列表里，模型在 P0.3 和 P0.4 配。
- Sandbox 选 docker（不用 Daytona）。
- Search 选 tavily，并填 key；或改 `search_api`，见下文。
- 向导会自动生成 `BYOK_ENCRYPTION_KEY`。

**注意：** 向导会改写 `agent_config.yaml`（上游文件）。跑完后执行 `git diff agent_config.yaml`。如果有改动，**一律还原**（`git checkout agent_config.yaml`），把需要的值写进 `agent_config.dalton.yaml`。

然后手工确认或补全 `.env`：

```dotenv
HOST_MODE=oss
AUTH_USER_ID=local-dev-user
SANDBOX_PROVIDER=docker
DOCKER_HOST_SOCKET=/var/run/docker.sock   # compose 里的 socket 挂载按 colima VM 内的路径解析【待验证】
COMPOSE_PROFILES=infra
COMPOSE_FILE=docker-compose.yml:docker-compose.dalton.yml   # 让 make up 自动叠加 Dalton 配置
APP_ENV=dalton                                              # 读 agent_config.dalton.yaml
BYOK_ENCRYPTION_KEY=<make config 生成>
TAVILY_API_KEY=<owner 提供>          # agent_config.yaml 的 search_api 默认是 tavily
# FMP_API_KEY=                       # 本阶段不配：价格默认用 yfinance（已决定）；FMP 以后可选
DALTON_RESEARCHDATA_URL=<见数据篇 P0-D.1；值不入库>
DALTON_BROKER_TOKEN=<broker v2 首次启动时生成在它的状态目录里，owner 复制过来；只能调模型，不是网关 operator 令牌>
DALTON_BUDGET_GATE=1                 # 本地预算开关，见 P0.8
REDIS_PASSWORD=redis                 # 与 Redis 容器 --requirepass 一致
REDIS_URL=redis://:redis@localhost:6379/0  # 宿主机模式（uv run python server.py）必填带密码 URL，防止 [RB_WAKE] Authentication required 导致 /threads/messages 503；Docker 容器内由 docker-compose.dalton.yml 覆盖为 @redis:6379/0
# 下面这个只给"网关 agent 对照组"（P0.5 --target gateway）用，主路径不需要：
# OPENCLAW_GATEWAY_TOKEN=<owner 从 openclaw.json 的 gateway.auth.token 复制；我们的脚本绝不读这个字段>
```

#### 2. 新建 `docker-compose.dalton.yml`

上游的 `docker-compose.override.yml` 已在 `.gitignore` 里，被 `/setup-worktree` 占用，所以我们用独立文件名，通过 `COMPOSE_FILE` 叠加。

```yaml
services:
  backend:
    entrypoint: ["/usr/bin/tini", "-s", "--", "/app/scripts/dalton/docker_entrypoint_dalton.sh"]
    command: ["uv", "run", "python", "server.py", "--host", "0.0.0.0", "--port", "8000", "--reload"]
    mem_limit: 2g          # 上游是 8g；colima VM 只有 5 GiB，见第 5 步【待验证实际占用】
    memswap_limit: 2g
    volumes:
      # 不再挂载 openclaw.json：模型目录改由 broker v2 的 /dalton-broker/v1/models 提供（P0.3、P0.4），
      # 凭证和配置都留在宿主机的 OpenClaw 进程里
      # 上游 compose 只挂基础 YAML，不挂 overlay；不挂的话改 overlay 要重建镜像
      - ./agent_config.dalton.yaml:/app/agent_config.dalton.yaml:ro
      # 仓库根的 workflows/<name>/workflow.js（PS 的 workflow）。注册表按 <cwd>/workflows 读取
      # （src/ptc_agent/agent/middleware/background_subagent/workflow/prebuilt.py），进程内缓存，
      # 所以改 workflow 只要重启 backend（docker compose restart backend），不用重建镜像
      - ./workflows:/app/workflows:ro
      # 覆盖名单 config/dalton/coverage.yaml（P0D 的 P1.2），后端的研究总监等从这里读
      - ./config/dalton:/app/config/dalton:ro
      # 冒烟报告目录（var/ 已 gitignore），供健康接口读取
      - ./var/dalton:/app/var/dalton
```

`workflows/` 和 `config/dalton/` 要先建目录（可以只放一个 `.gitkeep`），否则 Docker 会以 root 身份建一个空目录。

#### 3. 新建 `scripts/dalton/docker_entrypoint_dalton.sh`

内容 = 上游 `scripts/docker_entrypoint.sh` 的三步（等 PG、`uv run alembic upgrade head`），再加一行：

```bash
uv run alembic -c src/dalton/alembic.ini upgrade head
```

最后 `exec "$@"`。Dalton 的迁移走独立链，原因见 P0.8。

#### 4. 构建沙箱镜像并启动

```bash
colima status || colima start      # 参数已记在 $COLIMA_HOME 的 profile 里
until docker info >/dev/null 2>&1; do sleep 2; done
make up PROVIDER=docker
```

`make up PROVIDER=docker` 会先执行 `docker build -f Dockerfile.sandbox -t langalpha-sandbox:latest .`，再 `docker compose up --build`。首次构建较慢（ubuntu 24.04 + apt + Python 依赖，【待验证】约 10–20 分钟）。

#### 5. 资源

colima VM 固定为 4 CPU、5 GiB 内存（owner 决定；本机 16 GB 还要跑 OpenClaw 和各个 CLI gateway）。VM 内的分配：

| 容器 | 上限 | 来源 |
|---|---|---|
| 沙箱（全部公司工作区共用 1 个） | `memory_limit: "3g"`、`cpu_count: 2.0` | `agent_config.dalton.yaml` 的 `sandbox.docker` |
| backend | `mem_limit: 2g`（上游 8g） | `docker-compose.dalton.yml` |
| postgres + redis | 约 0.5 GB | — |
| frontend（vite dev） | 约 0.5–1 GB | — |

上限之和超过 5 GiB，但这些是上限不是预留；实测 `docker stats` 常驻占用。内存吃紧时，先把 frontend 改到宿主机跑（`make dev-web`），再考虑压 backend。

**一台 computer、空闲自动停：**
- 所有公司工作区绑定同一台 computer（同一个沙箱）。新建工作区时选已有的 computer；脚本建工作区（P0D 的 P1.3）时传同一个 computer【待验证：创建接口是否默认复用用户已有的 computer，以及传参方式】。同一 computer 上的工作区互相可见，这符合预期（覆盖研究需要跨公司读）。
- 空闲回收由后端的 `WorkspaceManager` 做，它对 docker 沙箱同样生效。空闲阈值 = `max(sandbox.daytona.auto_stop_interval − 600, 300)` 秒（`src/server/app/setup.py` 约第 392 行，变量名带 daytona 但与 provider 无关）。在 overlay 里设 `sandbox.daytona.auto_stop_interval: 2400`，即空闲 30 分钟自动停【待验证：docker provider 的 stop 实际停掉容器】。有自动任务在跑时不会被停（`_machine_has_active_tasks`）。

`agent_config.dalton.yaml` 里与本节相关的片段（完整样例见 P0.6）：

```yaml
sandbox:
  provider: "docker"
  docker: { memory_limit: "3g", cpu_count: 2.0 }
  daytona: { auto_stop_interval: 2400 }   # 只用来推导空闲回收阈值（30 分钟）；不启用 Daytona
```

#### 6. 仓库卫生（硬性规矩）

**Dalton 代码只放在这几处：**
- `plugins/dalton*/`：skills、subagents、MCP
- `src/dalton/`：后端包
- `scripts/dalton/`
- `web/src/pages/**/dalton/`：前端新组件
- `workflows/<name>/workflow.js`：PS 的 workflow（上游目前没有这个目录；如果上游以后也放 workflow，目录名用 `dalton-` 前缀避免冲突）
- `config/dalton/`：覆盖名单等
- `integrations/openclaw/dalton-langalpha-broker/`：broker v2
- `docs/dalton/`
- 上面新建的几个根目录文件
- 迁移只放在 `src/dalton/migrations/`（独立链，见 P0.8 第 3 节），**不在上游 `migrations/versions/` 里加文件**

**上游文件只允许"钩子级"改动：**
- 每处不超过约 10 行，并带标记注释：Python 用 `# DALTON-HOOK(<id>): <一句话原因>`，TS 用 `// DALTON-HOOK(<id>)`。
- `scripts/dalton/check_hooks.sh` 用 `git grep -n "DALTON-HOOK("` 列出所有钩子，并和本文附录 A 的表比对。多出或缺少都失败退出。
- 合并上游前后各跑一次。

**分支策略：**
- `dalton/main` 是集成分支，推 `origin`（github.com/everflowinv/dalton-langalpha，私有）。
- 开发分支 `dalton/feat/<topic>`，经 PR 合入 `dalton/main`，不直接推 `dalton/main`。
- 上游远端 `upstream` = ginlix-ai/LangAlpha，push URL 已是 `DISABLED`（已确认），**不要改回来**。
- 每周（或上游有我们需要的修复时）同步一次：

```bash
git fetch upstream
git push origin upstream/main:upstream-main      # 维护 origin 上的上游镜像分支（已存在 origin/upstream-main）
git checkout -b dalton/sync-$(date +%Y%m%d) dalton/main
git merge upstream/main                           # 用 merge，不用 rebase：dalton/main 是共享分支
scripts/dalton/check_hooks.sh
uv run python scripts/dalton/sync_openclaw_models.py --check   # 上游改了 manifest 时，确认 overlay 键不冲突
make test && (cd web && pnpm typecheck)
# 然后做 P0.8 里标 [回归] 的真实调用项，开 PR 合回 dalton/main
```

### 验证方式

```bash
docker compose ps                                   # postgres/redis/backend/frontend 都是 healthy/running
curl -sf http://localhost:8000/health               # 200
open http://localhost:5173                          # OSS 模式免登录，用户为 local-dev-user
docker compose exec backend uv run alembic current  # 显示 073 (head)
docker compose exec backend uv run alembic -c src/dalton/alembic.ini current   # 显示 Dalton 链的 head（P0.7/P0.8 之后）
docker compose exec backend curl -s -o /dev/null -w "%{http_code}\n" http://host.docker.internal:18789/v1/models   # 期望 401（colima 下网络可达）【待验证】
# broker v2 装好后：
docker compose exec backend sh -c 'curl -s -H "Authorization: Bearer $DALTON_BROKER_TOKEN" http://host.docker.internal:18789/dalton-broker/v1/health'   # 期望 {"status":"ok",...}
docker compose exec backend ls /app/workflows /app/config/dalton   # 两个挂载都在
colima list                                                         # CPUS 4、MEMORY 5GiB
du -sh "$COLIMA_HOME"                                               # VM 磁盘在 /Volumes/EveSSD 下
```

然后在 UI 里新建两个 workspace（都选同一台 computer），确认只起了一个沙箱容器（`docker ps --filter name=langalpha-sandbox`），`docker inspect` 里内存上限是 3 GiB。闲置 30 分钟后容器被停，再发消息能自动拉起。后端日志里**不应**出现 `Docker host has no /dev/fuse; sandbox runs without the file mount`（`src/ptc_agent/core/sandbox/providers/docker.py:1083`）。如果出现，记下来：文件挂载不可用会影响 memory/automation 文件在代码里的读写（colima 的 VM 是否提供 `/dev/fuse`【待验证】）。

### 验收标准

- 上面的命令全部符合预期。
- `git status` 只出现 Dalton 新文件，`agent_config.yaml` 和 `config.yaml` 没有改动。
- `scripts/dalton/check_hooks.sh` 通过，P0 结束时钩子数和附录 A 一致。

### 风险与注意

- 后端容器里只有 `DALTON_BROKER_TOKEN`（只能调模型）。各家供应商的 apiKey 和 OAuth 凭证都留在宿主机的 OpenClaw 进程里，不进容器，也不进沙箱。
- **宿主机与容器共跑时的 Redis 唤醒监听池与对话传输层 503 根因（`REDIS_URL` 说明，P0J §1.4）：**
  - **审查人实例缺失的变量：** 审查人当时那台实例的 `.env`（备份于 `.env.bak-20261019`）已配置 `REDIS_PASSWORD=redis`，**缺失的变量是 `REDIS_URL`**。
  - **为什么需要密码及连接串解析路径：** `docker-compose.yml:35` 启动 `redis` 容器时使用 `redis-server --requirepass ${REDIS_PASSWORD:-redis}` 开启了密码鉴权。后端在宿主机进程启动时，`RedisCacheClient.__init__`（`src/utils/cache/redis_cache.py:125-129`）通过 `get_nested_config('redis.url') or os.getenv("REDIS_URL", "redis://localhost:6379/0")` 读取 `config.yaml` 中的 `redis.url: "${REDIS_URL:-redis://localhost:6379/0}"`；因 `.env` 缺失 `REDIS_URL`，`self.url` 解析为不带密码的 `redis://localhost:6379/0`。
  - **启动握手失败与 `self.enabled = False`：** 后端启动执行 `src/server/app/setup.py:259-268` 的 `await init_cache()` 时，`RedisCacheClient.connect()`（`src/utils/cache/redis_cache.py:154-196`）在第 `183` 行执行 `await self.client.ping()`，被要求密码的 Redis 拒绝并抛出 `AuthenticationError: Authentication required.`。`redis_cache.py:193-196` 捕获异常后打印 `Failed to connect to Redis: Authentication required.`，**将 `self.enabled` 置为 `False` 并重新抛出异常（`raise`）**；`src/server/app/setup.py:266-268` 捕获该异常后打印 `Server will continue without caching`，使后端在 `cache.enabled == False` 状态下继续运行。
  - **为什么唤醒监听循环持续报错且 `POST /api/v1/threads/messages` 返回 HTTP 503：**
    1. 唤醒监听循环 `WakeListener._run()`（`src/server/services/report_back/handoff/wake_listener.py:314-332`）调用 `await get_shared_pubsub_client()`（`src/server/services/workspace_status_pubsub.py:100-109`）；该函数在第 `107-108` 行检查 `if not cache.enabled or not cache.client: return None`，因 `cache.enabled` 已被置为 `False` 而返回 `None`，导致 `wake_listener.py:315-321` 抛出 `ConnectionError("pub/sub pool unavailable")` 并在第 `332` 行反复输出 `[RB_WAKE] Wake listener error, resubscribing: pub/sub pool unavailable`。
    2. 对话接口 `POST /api/v1/threads/messages` 在 `_handle_send_message()`（`src/server/app/threads/messaging.py:552`）调用预检 `_assert_stream_transport_ready()`（`src/server/app/threads/messaging.py:61-104`）；第 `89-91` 行检查 `if cache.enabled and cache.client and await cache.client.ping(): return` 因 `cache.enabled` 为 `False` 不满足，直接在第 `94-104` 行抛出 `HTTPException(status_code=503, detail={"code": "transport_unavailable", "message": "The event-stream transport is temporarily unreachable; retry shortly."})`（同时若走到底层事件流写入 `RedisCacheClient.pipelined_event_buffer`，`src/utils/cache/redis_cache.py:583-586` 也会因 `if not self.enabled or not self.client:` 抛出 `EventBufferUnavailableError`，经 `src/utils/cache/stream_append.py:222-225` 转为 `StreamAppendError`）。
  - **固定配置方式：** `.env` 中补齐宿主机使用的 `REDIS_URL=redis://:redis@localhost:6379/0`，并在 `docker-compose.dalton.yml:8` 中为 `backend` 容器显式覆盖 `REDIS_URL: "redis://:${REDIS_PASSWORD:-redis}@redis:6379/0"`（容器内访问 `redis` 服务名，宿主机访问 `localhost`），同时配置 `INTERNAL_SERVICE_TOKEN` 与 `DALTON_PM_USER_IDS=local-dev-user,realrun-pm-a`。
  - **修复前后 `check_env.py` 对话传输层（`checks.conversation_transport`）实测输出结构（完整实测运行输出见 `scripts/dalton/pr_baseline.sh` 自动生成发布的 PR 评论）：**
    - 修复前（缺失 `REDIS_URL`，`cache.enabled` 在启动 `ping()` 鉴权失败后被置为 `False`，`check_conversation_transport` 返回 `checks.conversation_transport`）：
      ```json
      {
        "status": "FAIL",
        "model": "antigravity/gemini-3-flash-preview",
        "http_status": 503,
        "sse_events_count": 0,
        "conversation_usages_count": 0,
        "user_id_verified": false,
        "error": {
          "detail": {
            "code": "transport_unavailable",
            "message": "The event-stream transport is temporarily unreachable; retry shortly."
          }
        }
      }
      ```
    - 修复后（`.env` 补齐 `REDIS_URL=redis://:redis@localhost:6379/0` 且容器内由 `docker-compose.dalton.yml` 覆盖为 `redis://:${REDIS_PASSWORD:-redis}@redis:6379/0`，`check_conversation_transport` 返回 `checks.conversation_transport`，实跑原始输出由 `scripts/dalton/pr_baseline.sh` 直接生成）：
      ```json
      {
        "status": "PASS",
        "model": "antigravity/gemini-3-flash-preview",
        "http_status": 200,
        "sse_events_count": 9,
        "conversation_usages_count": 1,
        "user_id_verified": true,
        "error": null
      }
      ```
- 如果 colima 下 `host.docker.internal` 访问不到只绑 loopback 的网关（broker v2 也挂在这个端口上，所以结论一样）：
  - 先按 P0-D.0 的排查顺序处理（`host.lima.internal` / `192.168.5.2`、daemon 的 `host-gateway-ip`）；
  - 备选一：后端改在宿主机跑（`make setup-db` 起 PG/Redis，`make dev` 起后端，`make dev-web` 起前端）。这时 `{HOST_IP}` 自动解析为 `localhost`（`src/config/env.py:127`）。沙箱仍在 colima 里，仍要能访问宿主机桥；
  - 备选二：把网关改成 `bind: lan` 并配合防火墙（已获授权，由 lead/运维执行）。
- `/Volumes/EveSSD` 没挂载时 colima 起不来。这是预期行为：不要退回到内置盘上的默认 VM，否则数据会分裂成两份。
- `make config` 会改上游文件，必须按上文还原。

**预估工时：** 0.75 天（含 colima 重建；不含首次镜像构建的等待）。

---

## P0.2 broker v2 spike（1–2 天，go/no-go 闸门）

**目标：** 用最少的代码证明 `openclaw/plugin-sdk/llm` 能在 OpenClaw 插件里完成以下几件事：
- 在**指定的后端模型**上，用**我们自己的完整多轮上下文**完成**原生工具调用往返**；
- **流式**输出，并拿到**用量（含缓存 token）和成本**；
- 全程不经过 OpenClaw agent 循环。

spike 用 `xai/grok-4.7` 做主测；时间允许时加测 `deepseek/deepseek-flash` 和 `google/gemini-3.8-flash`。Gemini 3 的工具调用要回传 thoughtSignature，是最难的一项。

**前置依赖：**
- 在 `openclaw.json` 里加一个**仅用于 spike 的**插件条目（spike 结束后设为 `enabled:false`），并重启网关（launchd label `ai.openclaw.gateway`，见旧脚本 `dalton-batch-cnhk/scripts/openclaw_broker_stopped_window.py`）。owner 已授权，由 lead/运维执行；改前先备份 `openclaw.json`。
- 本机 Node 用 OpenClaw 自带的 `~/.openclaw/tools/node/bin/node`（broker v1 的 `package.json` 要求 `engines.node >= 24`）。

**新增文件：**
- `integrations/openclaw/dalton-langalpha-broker/`【新建】，spike 阶段只放：
  - `openclaw.plugin.json`
  - `package.json`（`openclaw.extensions: ["./index.mjs"]`，`peerDependencies.openclaw >= 2026.10.4`）
  - `index.mjs`
  - `src/spike.mjs`
- `scripts/dalton/broker_spike_client.py`【新建】，Python 客户端，用 httpx 发请求。

### 具体做法

#### 1. 插件骨架

照抄 v1 的 `index.mjs` 和 `src/plugin-definition.mjs` 的结构（`definePluginEntry`）。在 `register(api)` 里：

```js
api.registerHttpRoute({
  path: "/dalton-broker/v1/",
  match: "prefix",
  auth: "plugin",
  handler: (req, res) => spike.handle(req, res),
});
```

spike 阶段的鉴权只校验 `Authorization: Bearer <token>`，token 从插件 `config.spikeToken` 读取（lead/运维写入）。

#### 2. 取模型

```js
const r = await prepareSimpleCompletionModelForAgent({
  cfg: <运行时配置>,
  agentId: <专用 agent，如 "main">,
  modelRef: "xai/grok-4.7",
});
```

- 要先确认三件事【待验证】：插件里拿运行时完整配置的入口（例如 `api.config` 或 `api.runtime.config`）；`prepareSimpleCompletionModelForAgent` 是否受 `plugins.entries.<id>.llm.allowedModels` 约束；agentId 是否必需。
- 如果 `prepare*` 只返回 agent 默认模型、拿不到指定模型，退路是 `api.runtime.modelAuth.getRuntimeAuthForModel({model, cfg})`，再配合 `api.runtime.modelConfig.resolveAllowedModelRef`。

#### 3. 调用

```js
streamSimple(r.model, context, {
  apiKey: r.auth.apiKey,
  maxTokens,
  reasoning,
  signal,
});
```

- 先直接用 `r.model` 试。如果报 transport 未准备好之类的错误，参照 `simple-completion-execution-BsqqTm99.mjs`：它在 `completeSimple` 前做了 `prepareModelForSimpleCompletion` 和 `prepareHeadersForSimpleCompletion`，但这两个来自 `@openclaw/ai/transports`，**不是**插件 SDK 的公开面【待验证：插件能否使用】。
- 两种方式都不行时，降级为 `completeWithPreparedSimpleCompletionModel`（非流式）。这算 spike 的"部分 go"，见第 5 节判定。

#### 4. 映射（spike 只做最小子集）

OpenAI messages 到 SDK Context：
- system/developer 合并为 `systemPrompt`；
- user 映射为 `UserMessage`（字符串，或 `text`/`image` 块）；
- assistant 映射为 `AssistantMessage`：
  - `content` = 文本块 + `toolCall{id, name, arguments: JSON.parse(...)}`；
  - `api`/`provider`/`model` 填目标模型；
  - `usage` 填零；`stopReason` 有 tool_calls 时为 `"toolUse"`，否则为 `"stop"`；
- tool 映射为 `ToolResultMessage{toolCallId, toolName（从前面的 assistant tool_call 里查）, content:[{type:"text", text}], isError:false}`；
- `tools[]` 映射为 `Tool{name, description, parameters}`。

SDK 事件到 SSE：
- `text_delta` → `choices[0].delta.content`；
- `toolcall_start`/`toolcall_delta`/`toolcall_end` → `delta.tool_calls[{index, id, type:"function", function:{name, arguments}}]`；
- `done` → `finish_reason`（`toolUse`→`tool_calls`，`stop`→`stop`，`length`→`length`）；
- 收尾时如果请求了 `stream_options.include_usage`，再发一个 usage 块，然后 `[DONE]`。

#### 5. 用 `broker_spike_client.py` 跑 6 项测试

都发往 `http://127.0.0.1:18789/dalton-broker/v1/chat/completions`。

| # | 测试 | go 条件 |
|---|---|---|
| K1 | 非流式普通对话：`"Reply with exactly: PONG"` | 返回 PONG；`usage.prompt_tokens>0` |
| K2 | 流式：数到 30 | ≥3 个增量块，最后有 usage 块 |
| K3 | 工具往返（流式）：`get_stock_price` 查 META，回填 `{"price":612.34}` | 第一次：`finish_reason=tool_calls`，参数能解析出 `ticker=META`。第二次：回答含 612.34 |
| K4 | 多轮保真：伪造的 assistant 轮说 "The codeword is PELICAN-7"，再问 codeword | 答 PELICAN-7 |
| K5 | 无注入：`system:"SENTINEL-ALPHA-42…"`，让模型逐字复述系统指令的第一行；另发只有 `"hi"` 的请求看 `prompt_tokens` | 复述以 SENTINEL 开头；`"hi"` 的 prompt_tokens 小于 50（证明没有 OpenClaw 系统提示） |
| K6 | 用量与成本：同一个约 4k token 前缀连发两次 | 两次都有 `usage.cost.total>0`；第二次 `cacheRead>0` 为加分项，不作为 go 条件 |

#### 6. go/no-go 判定（spike 报告写进 `var/dalton/smoke/broker_spike_<ts>.json` 和 PR 描述）

| 结论 | 条件 | 下一步 |
|---|---|---|
| **GO** | K1–K5 全过，K6 有 cost | 进入 P0.3 |
| **部分 GO** | 只能非流式（第 3 步降级），或工具调用只在部分模型上可用 | 已决定接受：非流式 broker 只服务子智能体和后台调用；主分析师必须流式，走原生 OAuth |
| **NO-GO** | K3 或 K4 不过，或者插件拿不到指定模型或凭证 | 主路径改为 LangAlpha 原生（备选 1），broker v2 搁置；P0.4 及之后的 LangAlpha 侧工作不受影响 |

### 验收标准

- spike 报告覆盖 K1–K6，每项有原始证据（请求和响应摘要，**不含任何凭证**）。
- 明确给出 GO、部分 GO 或 NO-GO，由 lead 签字确认。

### 风险与注意

- `@openclaw/ai/internal/*` 和 `transports` 的一部分没有 semver 保证（`@openclaw/ai` 的 README 原话）。spike 要记录用到的每一个导入路径。P0.3 只允许使用 `openclaw/plugin-sdk/*` 和 `@openclaw/ai` 的公开子路径；万不得已用了非公开路径时，要在插件里做版本闸门（版本不匹配就拒绝启动）。
- spike 的插件条目在 spike 结束后**禁用**（`enabled:false`），不留在生产配置里。

**预估工时：** 1.5 天（骨架和映射 1 天，测试和报告 0.5 天）。

---

## P0.3 broker v2 完整实现：`dalton-langalpha-broker`（1–2 周）

**目标：** 一个可长期运行的 OpenClaw 托管插件，提供 OpenAI Chat Completions 兼容接口。它要做到：
- 完整的消息和工具语义；
- 流式；
- 推理强度透传；
- 用量含缓存 token 和成本；
- 持久幂等日志（发出前先记录，状态未知的调用只能 replay，不重新购买）；
- 允许列表、上限、鉴权；
- 公开模型目录接口和健康接口。

凭证始终留在宿主机的 OpenClaw 进程里。

**前置依赖：** P0.2 GO。

### 1. 目录与文件（全部【新建】，在本仓库，不放 `plugins/`，那里是 LangAlpha 的 Agent Plugins 格式）

```
integrations/openclaw/dalton-langalpha-broker/
  openclaw.plugin.json     # id/name/version/activation.onStartup/configSchema（closed schema，见第 7 节）
  package.json             # "type":"module"，engines.node>=24，peerDependencies.openclaw，openclaw.extensions
  index.mjs                # definePluginEntry(createPluginDefinition())
  README.md                # 边界、安装、配置、错误码（照 v1 README 的结构写）
  src/
    plugin-definition.mjs  # registerService（启动和停止，打开 journal、加载 token）+ registerHttpRoute
    http.mjs               # 路由分发、请求体上限、SSE 写出、错误统一出口
    auth.mjs               # Bearer token（默认）/ 可选 HMAC（从 v1 auth.mjs 移植）
    openai-map.mjs         # OpenAI ⇄ SDK Context/事件/usage 的映射（纯函数，可单测）
    models.mjs             # 允许列表、模型准备缓存、text-only 判定、公开目录
    invoke.mjs             # 一次调用：准入 → journal pending → streamSimple → 收尾 → journal completed/failed
    journal.mjs            # 从 v1 journal.mjs 移植：pending/completed/failed，replay-only，0600，原子替换，上限与剪枝
    replay-state.mjs       # 按 tool_call_id、助手消息哈希缓存 provider 回放状态（thoughtSignature、thinking 签名、providerReplay）
    limits.mjs             # 并发、排队、超时、请求和输出字节上限（从 v1 broker.mjs 的上限部分移植）
  test/                    # node --test，用 fake runtime（v1 的做法）；等真实调用验证之后再补
```

### 2. HTTP 接口（全部挂在网关端口 `127.0.0.1:18789` 的 `/dalton-broker/v1/` 下，`auth:"plugin"`）

**为什么选网关路由，不选 Unix socket 或独立端口：**
- 后端在 Docker 容器里。Unix socket 跨 macOS 和 colima 虚拟机的边界不可用。
- 独立端口要多维护一个监听器和生命周期。
- 网关路由复用已有端口，生命周期由网关管理。网关重启时自动返回 503 加 `Retry-After`，LangAlpha 的错误漏斗按可重试处理。
- 可达性和网关一样，经 `host.docker.internal`【待验证，P0.1 同一条】。
- **备选**：如果网关路由不支持长时间的 SSE，或者有请求体上限【待验证】，就改在插件服务里自己 `listen("127.0.0.1", 18790)`，接口完全不变。

| 方法与路径 | 作用 |
|---|---|
| `POST /dalton-broker/v1/chat/completions` | 主接口，见第 3 节 |
| `GET /dalton-broker/v1/models` | OpenAI 风格的 `{object:"list", data:[{id:"xai/grok-4.7", object:"model", owned_by:"openclaw"}]}`。只列 broker 允许列表内的模型 |
| `GET /dalton-broker/v1/models?detail=1` | 公开目录，供 LangAlpha 同步（P0.4）。每个模型：`{ref, provider, name, api, contextWindow, maxTokens（取模型和 broker 上限的较小值）, reasoning, reasoningLevels[], input[], toolCalling（true，或 false 表示 text-only）, billing（"cash" 或 "subscription"）, cost（OpenClaw 原始公开 cost，含 tieredPricing）}`，外加 `meta{openclawVersion, brokerVersion, generatedAt}`。**只读公开字段**，不读 apiKey、headers、auth 等 |
| `GET /dalton-broker/v1/health` | `{status:"ok", brokerVersion, openclawVersion, journal:{pending, completed, capacityLeft}, inflight, queued}`。不调用模型 |
| `GET /dalton-broker/v1/invocations/{id}` | 只读回放（replay-only）：`{state:"completed", response}`、`{state:"pending"}`（不确定状态）、`{state:"failed", error}`，或 404 `IDEMPOTENCY_MISS` |

所有接口都需要 `Authorization: Bearer <DALTON_BROKER_TOKEN>`。

### 3. `POST /chat/completions` 协议

**请求字段**（closed 校验，未知字段返回 400，避免静默忽略）：
- 必需：`model`、`messages`；
- 可选：`tools`、`tool_choice`（`auto|none|required|{type:function,function:{name}}`）、`stream`、`stream_options.include_usage`、`max_completion_tokens`/`max_tokens`、`reasoning_effort`、`temperature`、`top_p`、`stop`、`parallel_tool_calls`（记录，按 SDK 能力处理）、`user`（忽略，不建会话）。

`model` 字段：
- 值是 OpenClaw 的 `provider/model`，例如 `xai/grok-4.7`。不在 broker 允许列表里就返回 404 `model_not_allowed`。**绝不替换成别的模型。**
- 写成 `model@profile`（指定 auth profile）时，必须配置 `allowAuthProfileOverride`，否则 400。

请求头：
- `X-Dalton-Invocation-Id`（可选，`[A-Za-z0-9:._-]{8,128}`）：幂等键，见第 5 节。
- `X-Dalton-Replay-Only: 1`：只读回放，不发起调用。
- `X-Dalton-Fallback: provider/model[,…]`：**调用方显式要求**时才做供应商故障回退，见第 6 节。

**消息映射**（`openai-map.mjs`，在 spike 的基础上补全）：
- `system`/`developer` 按顺序拼成 `systemPrompt`。LangAlpha 的 `operator_channel` 为 `system`，会把 operator 消息放在 system 里。
- `user`：字符串，或 `[{type:"text"}, {type:"image_url", image_url:{url:"data:…;base64,…"}}]`，映射为 `TextContent`、`ImageContent`。远程 URL 图片拒绝（400），避免 SSRF。
- `assistant`：`content` 加上 `tool_calls`，映射为 `AssistantMessage`。`provider`/`api`/`model` 填**本次目标模型**，跨模型历史由 SDK 的 `transformMessages` 处理【待验证】。如果 `replay-state.mjs` 里能按 `tool_call_id` 或消息哈希查到回放状态（thinking 块及签名、`thoughtSignature`、`providerReplay`），**并且模型相同**，就还原进去。
- `tool`：映射为 `ToolResultMessage`，`toolName` 由前面 assistant 消息里同 id 的调用确定。找不到对应调用时返回 400，不做猜测。`content` 是字符串或 text/image 数组。
- `tools`：映射为 `Tool{name, description, parameters}`。用 SDK 的 `validateToolArguments` 校验模型返回的参数；参数非法时按 SDK 的 `malformed_tool_call_arguments` 语义报错，不传给调用方。
- `tool_choice`：映射到 SDK 的对应选项【待验证 SDK 字段名】。SDK 不支持 `required` 或指定函数时返回 400，不降级。

推理强度：`reasoning_effort` 的 `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max` 映射为 SDK 的 `reasoning`（`none`→`off`），再用 `clampThinkingLevel` 和模型能力夹到支持的档位。实际生效的档位放在响应扩展字段 `openclaw.reasoning` 里。未传时用 broker 配置里该模型的 `defaultReasoning`（可空，空则由 SDK 决定）。

text-only 模型：模型所属的 provider 在配置 `textOnlyProviders` 里（默认 `["claude-cli-gateway","antigravity-cli-gateway","muse-cli-gateway"]`）时，**请求带非空 `tools` 就返回 400 `tools_unsupported`**，绝不压平。

**非流式响应：**

```json
{"id":"chatcmpl-<invocation>","object":"chat.completion","created":…,"model":"xai/grok-4.7",
 "choices":[{"index":0,"message":{"role":"assistant","content":"…",
   "tool_calls":[{"id":"call_…","type":"function","function":{"name":"…","arguments":"{…}"}}]},
   "finish_reason":"tool_calls|stop|length"}],
 "usage":{"prompt_tokens":I+CR+CW,"completion_tokens":O,"total_tokens":…,
   "prompt_tokens_details":{"cached_tokens":CR,"cache_write_tokens":CW},
   "completion_tokens_details":{"reasoning_tokens":R?},
   "cost_usd":cost.total,
   "openclaw":{"input":I,"cacheRead":CR,"cacheWrite":CW,"output":O,"cost":{…},"responseModel":"…","reasoning":"high","invocationId":"…","replayed":false}}}
```

**流式响应：**
- SSE，`data: {chat.completion.chunk}`。首块带 `delta.role="assistant"`。
- `text_delta` → `delta.content`。
- `thinking_delta` → `delta.reasoning_content`。是否输出由配置 `emitReasoning` 决定（默认 true）；LangChain `ChatOpenAI` 会忽略这个字段，UI 是否展示另议【待验证】。
- `toolcall_start` → `delta.tool_calls[{index, id, type, function:{name, arguments:""}}]`；`toolcall_delta` → `delta.tool_calls[{index, function:{arguments:<片段>}}]`。
- 最后一块带 `finish_reason`；有 `include_usage` 时再发一个 `choices:[]` 加 `usage` 的块；然后 `data: [DONE]`。
- 出错时：如果还没发出任何内容，返回对应的 HTTP 状态码和 JSON 错误；如果已经开始流式，发 `data: {"error":{…}}` 再发 `[DONE]`。
- 每 15 秒写一次 `: keepalive` 注释，避免 LangAlpha 的 stream chunk timeout 误判流停滞（`STREAM_CHUNK_TIMEOUT_S`，`src/llms/llm.py`）。

**错误码**（`{"error":{"type","code","message"}}`，`message` 不含 prompt 和凭证）：

| HTTP | code | 说明 |
|---|---|---|
| 400 | `invalid_request` | 请求不合法 |
| 400 | `tools_unsupported` | text-only 模型收到 tools |
| 400 | `unknown_tool_call_id` | tool 消息找不到对应调用 |
| 401 | `unauthorized` | |
| 404 | `model_not_allowed` | |
| 409 | `idempotency_conflict` | 同一 id 但请求哈希不同 |
| 409 | `idempotency_indeterminate` | 同一 id 仍是 pending |
| 413 | `request_too_large` | |
| 429 | `broker_busy` | 带 `Retry-After` |
| 502 | `provider_error` | 带 `retryable: bool` 和上游状态码 |
| 503 | `provider_unavailable` | |
| 504 | `timeout` | |

LangAlpha 的 `classify_error` 会把 5xx 和 429 当作可重试，见 `src/server/AGENTS.md` 的错误漏斗。

### 4. 鉴权

- **默认：Bearer token。** 插件首次启动时，在自己的状态目录（`ctx.stateDir`）生成 32 字节随机 token，写入 `broker.token`（0600），日志只打印文件路径，不打印 token。lead/运维把它复制到 LangAlpha `.env` 的 `DALTON_BROKER_TOKEN`，manifest 里作为 openclaw-broker provider 的 `env_key`，由 openai SDK 以 `Authorization: Bearer` 发出。
  - 这个 token **只能调模型**，权限远小于网关的 operator token。
  - 轮换：删掉文件并重启插件，然后更新 `.env`。
- **可选：HMAC**，移植 v1 的 `auth.mjs`（`clientId`、`timestampMs`、`nonce`、`mac`，时钟偏差上限，nonce 防重放）。需要 LangAlpha 侧的 httpx 钩子负责签名（P0.4 第 6 节）。已决定 P0 用 Bearer，不开 HMAC；以后需要再升级。

### 5. 幂等日志与回放（语义同 v1，扩展到流式）

**幂等键：**
- 来自 `X-Dalton-Invocation-Id`。没有这个头时，生成随机 id，照常记日志，但不提供去重。
- 请求哈希 = 规范化 JSON 的 sha256。计算前去掉 `stream`、`stream_options`，以及 `X-Dalton-*` 回放、回退类的头；这些字段只影响输出形式，不影响模型调用。

**调用前：** 原子写入 `{id, requestHash, state:"pending", createdAt}`，然后才 dispatch。

**收尾：**

| 结果 | 记录 | 同一 id 再次请求 |
|---|---|---|
| 成功 | `completed`：保存完整响应（assistant 消息、tool calls、usage、cost）。这是敏感输出缓存，0600 | 原样回放：非流式直接返回；流式按保存的消息重新生成 SSE。`openclaw.replayed=true`，`usage` 和成本照常返回，但 `cost_usd` 标为已付费，见下 |
| 明确失败，且没有产生输出（上游 4xx/5xx 发生在任何 token 之前） | `failed{code, retryable}` | 允许再次 dispatch；日志记下 dispatch 序号，只用于审计，不构成任何次数上限 |
| 流式中途上游失败 | `failed_with_output`（对应 SDK 的 `PROVIDER_FAILURE_WITH_OUTPUT`） | 允许重新 dispatch（调用方明确重试，属于新的一次购买），日志记录这次重买 |
| 进程崩溃，没有结果 | 保持 `pending` | 返回 409 `idempotency_indeterminate`。**不重新调用**（"状态未知的调用不靠换编号重新购买"）。pending 记录不会自动删除；只能由 lead/运维用插件命令显式处理（P0 只提供只读查看，处理工具留到以后） |

**客户端在流式中途断开：** broker **继续消费上游流直到完成**（受 `timeoutMs` 约束），写入 `completed`。LangAlpha 用同一个 id 重试时直接拿到回放，不重新付费。

**replay-only**（`X-Dalton-Replay-Only: 1` 或 `GET /invocations/{id}`）：只读，不占用日志槽，不发起调用。

**容量：** `journalTtlMs`、`journalMaxRecords`、`journalMaxBytes` 同 v1。剪枝只删 `completed`。pending 占满容量时，新请求 fail-closed（503 `journal_full`）。

**回放状态缓存（`replay-state.mjs`）：** 每次成功调用后，把 thinking 块签名、`toolCall.thoughtSignature`、`providerReplay` 按 `tool_call_id` 和助手消息哈希保存（TTL 24 小时，同样 0600、有上限）。下一次请求带回这些 tool_call 时自动还原。这样 Gemini 3 一类要求回传签名的模型也能多轮工具调用，LangAlpha 不需要任何改动。

### 6. 回退：只针对供应商故障，并且只在调用方要求时

- 默认**不回退**：上游失败就按第 3 节的错误码返回，由 LangAlpha 决定（P0.6 的策略是 `fallback: []`）。
- 请求头带 `X-Dalton-Fallback: deepseek/deepseek-flash` 时，只在 `provider_unavailable`、`timeout`，或 `provider_error` 且 `retryable=true` 的情况下，**并且还没有输出任何 token 时**，才改用列出的模型。回退目标也必须在允许列表内，且具备相同的工具能力。
- 响应的 `model` 字段和 `openclaw.fallbackFrom` 如实写明实际使用的模型，日志记录回退事件。**绝不静默换模型。**

### 7. 配置（`openclaw.plugin.json` 的 `configSchema`，closed）和 `openclaw.json` 条目

```json5
// ~/.openclaw/openclaw.json（已获授权，lead/运维操作）
plugins: {
  load: { paths: [ /* 现有项…, */ "/Users/everflow/.openclaw/managed-plugins/dalton-langalpha-broker-<git sha>" ] },
  allow: [ /* 现有项…, */ "dalton-langalpha-broker" ],
  entries: {
    "dalton-langalpha-broker": {
      enabled: true,
      llm: {
        allowModelOverride: true,
        allowedModels: [            // 建议：允许列表内能用工具的 12 个，加上做纯文本用的 claude-cli 4 个；其他 text-only 先不开
          "xai/grok-4.7", "deepseek/deepseek-flash",
          "google/gemini-3.8-flash", "google/gemini-3.1-pro-preview", "google/gemini-3.5-flash-lite", "google/gemini-4-argon",
          "zai/glm-5.3", "zai/glm-5.3-flash",
          "openai/gpt-6.1-sol", "openai/gpt-6-astra", "openai/gpt-5.6-terra", "openai/gpt-6-luna",
          "claude-cli-gateway/claude-opus-5-5", "claude-cli-gateway/claude-sonnet-5-5",
          "claude-cli-gateway/claude-haiku-5-5", "claude-cli-gateway/claude-fable-5-1"
        ],
        allowedCompletionModels: [ /* 同上，把每次调用都限制在这个范围内 */ ],
        allowAuthProfileOverride: false,
        allowAgentIdOverride: false
      },
      config: {
        agentId: "main",                       // 用于 prepare 的 agent；只借用它的模型和凭证解析，不运行 agent【待验证 spike 结论】
        routePrefix: "/dalton-broker/v1/",
        textOnlyProviders: ["claude-cli-gateway", "antigravity-cli-gateway", "muse-cli-gateway"],
        subscriptionProviders: ["claude-cli-gateway", "antigravity-cli-gateway", "muse-cli-gateway", "openai"],
        models: {                              // 按模型的上限（来自 v1 profiles 的 maxTokens 和 timeoutMs）
          "xai/grok-4.7":            { maxTokens: 128000, timeoutMs: 1800000, defaultReasoning: "high" },
          "deepseek/deepseek-flash": { maxTokens: 128000, timeoutMs: 1800000 },
          "openai/gpt-6.1-sol":      { maxTokens: 128000, timeoutMs: 1800000 }
          // 没列出的模型用 defaults
        },
        defaults: { maxTokens: 64000, timeoutMs: 1800000 },
        maxConcurrent: 8, maxQueued: 32, maxQueueWaitMs: 600000,
        maxRequestBytes: 16777216, maxOutputBytes: 4194304,
        emitReasoning: true,
        journalTtlMs: 86400000, journalMaxRecords: 5000, journalMaxBytes: 268435456,
        auth: { scheme: "bearer" }             // 或 { scheme: "hmac", clientId: "client:dalton-langalpha" }
      }
    }
  }
}
```

说明：
- `subscriptionProviders` 里的 `openai` 是 ChatGPT 登录的订阅（`api: openai-chatgpt-responses`）。
- `allowedModels` 一律写 `provider/model`，broker 内部再做一次交集校验：同时在 broker 配置、`llm.allowedModels` 和 OpenClaw 模型目录里的模型才可用。

### 8. 安装为 OpenClaw 托管插件（沿用 v1 的方式）

v1 的做法：
- 插件代码按 git sha 内容寻址，复制到 `managed-plugins/<包名>-<sha>/`。现有目录如 `~/.openclaw/managed-plugins/openclaw-model-broker-3855c1e3…`，当前生效的 v1 路径在 `/Volumes/EveSSD/Dalton/…/managed-plugins/openclaw-model-broker-1d1faf86…`，见 `plugins.load.paths`。
- 把路径写进 `plugins.load.paths`，id 加进 `plugins.allow`，配置 `plugins.entries.<id>`。
- 在停机窗口里重启网关（`/Users/everflow/Projects/dalton-batch-cnhk/scripts/openclaw_broker_stopped_window.py` 用 launchd label `ai.openclaw.gateway` 做 stop / 配置 CAS / start）。

v2 新写一个更简单的脚本 `scripts/dalton/install_broker_v2.sh`【新建】，**lead/运维执行**（owner 已授权修改 OpenClaw 配置和重启网关），不由 LangAlpha 进程调用：

1. `git -C <repo> rev-parse HEAD` 得到 sha。用 `git archive HEAD integrations/openclaw/dalton-langalpha-broker` 解包到 `~/.openclaw/managed-plugins/dalton-langalpha-broker-<sha>/`。目录已存在就跳过（内容寻址）。
2. 在安装目录里执行 `~/.openclaw/tools/node/bin/node --check index.mjs`；有测试时运行 `node --test test/*.test.mjs`。
3. **不自动改 `openclaw.json`**。打印需要粘贴的 JSON 片段：`load.paths` 里的新路径和 `entries` 条目。由 lead/运维修改，并先备份成 `openclaw.json.bak-dalton-broker-v2-<日期>`（沿用本机已有的备份命名习惯）。
4. 打印重启命令，`launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway`【待验证 label 和域】；重启后 `curl` 健康接口。
5. 升级：换新 sha 目录，改 `load.paths`，重启网关。journal 在插件状态目录里，跨版本保留，所以不要改插件 id。

### 9. 验证方式（真实调用优先）

- 把 spike 的 K1–K6 扩成 P0.5 冒烟脚本的 `--target broker` 全集（B0–B16），对 6 个主力模型都跑一遍。
- 幂等专项：
  - 同 id 重放，期望 `replayed=true`，且上游没有新的计费（对比上游控制台或 broker 日志的 dispatch 计数）；
  - 同 id、不同请求体，期望 409 conflict；
  - 流式中途 `kill -9` 网关后再重放同 id，期望 409 indeterminate，且没有发起新调用；
  - 流式中途客户端断开后重放，期望拿到完整回放。
- text-only：给 `claude-cli-gateway/claude-opus-5-5` 发带 tools 的请求，期望 400 `tools_unsupported`；不带 tools 时正常返回。
- 回退：用一个不可达的配置模拟上游故障（或临时让 deepseek 的 key 失效，由 lead/运维操作后恢复）。不带 `X-Dalton-Fallback` 时直接报错；带上时切换，并在响应里写明 `fallbackFrom`。
- 并发：同时发 20 个请求，`maxConcurrent=8` 和排队生效，超出的返回 429 `broker_busy`。

### 10. 验收标准

- 上述验证全部通过；P0.5 的 broker 冒烟全部通过。
- 插件日志和 journal 里没有 prompt 原文（journal 只存响应）和凭证：`grep` 抽查。
- 安装目录内容寻址，journal 文件权限是 0600。
- README 写明边界、配置、错误码和安装方法。
- 测试在真实调用验证之后才补，覆盖映射纯函数、journal 状态机和鉴权。

### 11. 风险与注意

- **SDK 的非公开路径：** 见 P0.2。OpenClaw 升级后要先跑 B 系列冒烟再放量。插件启动时检查 `openclaw` 版本，不在白名单里就拒绝启动（fail closed），日志说明原因。
- **网关路由的 admission：** 网关重启时返回 503，正在进行的 SSE 会被中断。重启前先让 LangAlpha 那边空闲，或者接受一次自动重试（journal 会把这次重试变成回放或 409）。
- **订阅模型（openai 系）的配额和并发**由 ChatGPT 账号决定。broker 的 `maxConcurrent` 只是本地保护上限。供应商配额的原则：只告警，不设闸门；能读到配额用量时，达到 80% 在 OpenClaw 卡片上告警，P0 读不到就不做。

**预估工时：** 7–9 人日（协议和映射 2 天，流式和工具 2 天，journal 和回放状态 2 天，鉴权、上限和目录 1 天，安装、联调和文档 1–2 天）。

---

## P0.4 LangAlpha 接入 broker v2：模型同步、manifest overlay、调用 id

**目标：** 把 broker v2 公开目录里的模型，生成成 LangAlpha 的模型条目（上下文、最大输出、推理档位、价格、家族、订阅、是否 text-only），以 overlay 的形式合并进 manifest，不改上游的 `models.json` 和 `providers.json`。每次模型调用带上稳定的 `X-Dalton-Invocation-Id`，让 broker 的幂等日志生效。

**前置依赖：** P0.2 GO。在 broker v2 完成前，可以先对着 spike 版本或者 fixture JSON 开发。

**改动/新增文件：**

| 路径 | 状态 | 说明 |
|---|---|---|
| `src/dalton/llm/openclaw_catalog.py` | 【新建】 | 纯函数：broker 目录 → 规范化 → 生成 overlay；脚本和接口共用 |
| `src/dalton/llm/overlay.py` | 【新建】 | `apply_model_overlay()`、`overlay_stamp()`、`overlay_changed()` |
| `src/dalton/llm/openclaw_overrides.yaml` | 【新建】，人工维护 | 按模型覆盖可见性、家族、显示名、上下文上限、speed/intelligence |
| `src/dalton/llm/openclaw_overlay.json` | 【新建】，生成，提交 | overlay 正文 |
| `src/dalton/llm/broker_http.py` | 【新建】 | 注入 `X-Dalton-Invocation-Id` 的 httpx 客户端 |
| `scripts/dalton/sync_openclaw_models.py` | 【新建】 | CLI 包装 |
| `src/llms/llm.py` | 钩子 `DALTON-HOOK(llm-overlay)` | `ModelConfig.__init__` 合并 overlay；`LLM.get_model_config()` 检查 overlay 戳 |
| `src/llms/llm.py` | 钩子 `DALTON-HOOK(llm-broker-client)` | `_build_openai_params` 里，provider 带 `dalton_broker: true` 时注入 httpx 客户端 |
| `src/server/app/api_keys.py` | 钩子 `DALTON-HOOK(catalog-cache)` | `_build_provider_catalog`（第 721 行，`lru_cache`）在 overlay 戳变化时 `cache_clear()` |

### 1. 数据来源

- **主来源：** `GET {broker}/models?detail=1`（P0.3 第 2 节）。容器里可以直接访问，**不需要挂载 `openclaw.json`**。
- **补充来源（只在宿主机 CLI 可用）：** `--with-openclaw-config ~/.openclaw/openclaw.json`。按字段白名单读取 v1 broker profile 的 `providerControls.rateCard`（已核实、带过期时间的价格卡）和 `maxTokens`。只读这些字段：`plugins.entries["dalton-openclaw-model-broker"].config.profiles[*].{id, model, maxTokens, thinkingLevel, providerControls.rateCard}`。**禁止读** `apiKey`、`headers`、`auth`、`token`、`secret`、`password`、`gateway.*`、`credentials`。
- **可选：** 旧 Dalton 的价格卡导出（`--dalton-price-cards path.json`，格式 `{ "provider/model": {"input_per_million_usd": x, "output_per_million_usd": y} }`），字段来自 `dalton-batch-cnhk/src/dalton_core/model_router.py` 的 `_profile_wire`。不 import 旧代码。
- **兜底：** LangAlpha `providers.json` 里同一个裸 id 的价格。

### 2. 价格规范化：多来源取最大值（偏保守）

- 同一个价格分量（input、output、cached_input、cache_5m）在多个来源之间不一致时取最大值，差异写进 `meta.price_discrepancies`。例如 `google/gemini-3.8-flash`：rateCard 是 $1.50/$7.50，cost 是 $0.75/$3.75，取 rateCard 的价格。
- 平价卡：`{"input", "output", "cached_input"←cacheRead, "cache_5m"←cacheWrite（>0 时）, "unit":"per_1m_tokens"}`。字段名同 `src/llms/pricing_utils.py`。
- 分段价（`tieredPricing`）转成 `input_tiers`/`output_tiers` + `input_pricing_mode`/`output_pricing_mode="input_dependent"`：
  - 每段上界 = 下一段 `range[0]` 向下取整到 1000，最后一段为 `null`；
  - 写法参照 `providers.json` 里现有的 `gpt-6.1-sol`。
- `billing=="subscription"`：`{"pricing_type":"subscription","unit":"per_1m_tokens"}`，`src/utils/tracking/core.py` 按 0 成本计、不进现金额度。名义价格另存为同一条目的 `notional_pricing`。
- 现金模型完全没有价格 → `visible:false`，并写入 `meta.warnings`（无法如实预留预算）。

### 3. 家族（审稿独立性用）

| 模型 id 前缀 | 家族 |
|---|---|
| `claude-` | `anthropic-claude` |
| `gemini-` | `google-gemini` |
| `gpt-oss-` | `openai-gpt-oss` |
| `gpt-` | `openai-gpt` |
| `grok-` | `xai-grok` |
| `deepseek-` | `deepseek` |
| `glm-` | `zai-glm` |
| `muse-` | `meta-muse`【待验证】 |

同一模型经不同网关接入，仍算同一家族（旧 Dalton `canonical_vendor_family` 也是这样处理）。LangAlpha 原生的 `claude-opus-5-5-oauth` 同样属于 `anthropic-claude`。

**家族字段是同步输出的一部分，也是审稿独立性判定的唯一依据。** 每个生成的 provider 模型条目都带 `dalton` 块（第 4 节样例）：
- `dalton.openclaw_ref`：broker 目录里的 `ref`，即 `provider/model`，例如 `claude-cli-gateway/claude-opus-5-5`；
- `dalton.upstream_provider`：broker 目录里的 `provider`（OpenClaw 的上游 provider id），例如 `claude-cli-gateway`、`google`、`xai`；
- `dalton.family`：按上表从模型 id 推导；`openclaw_overrides.yaml` 可以按模型覆盖（用于 muse 这类前缀不明的模型）。推不出来时写 `unknown`，并进 `meta.warnings`。

`src/dalton/llm/role_policy.py` 提供 `family_of(model_key) -> str`（P0.6）：`ocb-` 键读 overlay 的 `dalton.family`；原生键（如 `claude-opus-5-5-oauth`、`gpt-6.1-sol-oauth`）去掉 `-oauth` 等后缀后用同一张前缀表。PK 的发布审稿、PS 的草稿期审稿、P0.7 卡片的角色检查都只调这一个函数，不各自解析模型名。`unknown` 家族一律视为"和任何模型都可能同家族"，不能当审稿人。

### 4. 生成的条目（键名前缀 `ocb-`，避免和上游的 `claude-opus-5-5` 等键冲突）

键名规则：`ocb-<provider>-<model>`，字符集 `[a-z0-9.-]`，例如 `ocb-xai-grok-4.7`。

`models.json` 条目：

```json
"ocb-xai-grok-4.7": {
  "model_id": "xai/grok-4.7",
  "provider": "openclaw-broker",
  "display_name": "Grok 4.7 · OpenClaw",
  "visible": true,
  "speed": 3, "intelligence": 4,
  "context": 500000,
  "input_modalities": ["text", "image"],
  "prompt_guidance": "lean",
  "parameters": { "max_tokens": 128000 },
  "reasoning": { "efforts": ["low", "medium", "high", "xhigh"], "default": "high", "write": "parameters.reasoning_effort" }
}
```

- `model_id` 就是 broker 认的 `provider/model`，线上 `model` 字段直接发这个值。计费戳 `pricing_model_id` 也是它，所以价格按模型区分。**不需要网关方案里那种 `parameters.model` 覆盖。**
- `reasoning.efforts` 来自目录的 `reasoningLevels`，换算成 LangAlpha 的档位名；`write` 写法同 manifest 里 `vllm`、`openrouter` 的现有用法。模型 `reasoning:false` 时不写这个块。
- `input_modalities` 按目录的 `input` 填。broker 支持 base64 图片，包括工具结果里的图片【待验证：LangAlpha 把多模态工具结果按 OpenAI 格式发送的形状】。
- **text-only 模型**（`toolCalling:false`）：
  - `display_name` 后缀加"（仅文本）"；
  - 加 `"profile": {"tool_calling": false}`【待验证：`ModelSpec` 会把 manifest 的 `profile` 合进 `client.profile`（`src/llms/llm.py` 的 `get_llm`），但 LangAlpha 是否据此禁用工具未知】；
  - P0.6 的角色配置和 P0.7 的卡片会对"主模型或子智能体用了 text-only 模型"给出警告；
  - 最后一道保护在 broker：带 tools 的请求返回 400。

`providers.json` 的 overlay：

```json
{
  "provider_config": {
    "openclaw-broker": {
      "sdk": "openai",
      "base_url": "http://{HOST_IP}:18789/dalton-broker/v1",
      "env_key": "DALTON_BROKER_TOKEN",
      "access_type": "local",
      "use_response_api": false,
      "dynamic_models": false,
      "byok_eligible": false,
      "prompt_cache_key": false,
      "operator_channel": "system",
      "display_name": "OpenClaw (broker)",
      "dalton_broker": true
    }
  },
  "models": { "openclaw-broker": [
    { "id": "xai/grok-4.7", "name": "Grok 4.7", "is_reasoning": true, "description": "Grok 4.7 via Dalton broker v2",
      "context_window": 500000, "max_output": 128000, "pricing": { "...": "按第 2 节" }, "notional_pricing": null,
      "dalton": { "openclaw_ref": "xai/grok-4.7", "upstream_provider": "xai", "family": "xai-grok",
                  "billing": "cash", "tool_calling": true, "price_sources": ["broker.cost"] } } ] }
}
```

`{HOST_IP}` 在容器里替换为 `host.docker.internal`，在宿主机上替换为 `localhost`（`src/config/env.py:127`）。

overlay 文件结构：

```json
{ "meta": { "generated_at", "source": "broker|broker+openclaw_config", "broker_version", "openclaw_version",
            "counts": {"catalog","visible","hidden","text_only"}, "hidden": [], "price_discrepancies": [], "warnings": [] },
  "models": { }, "providers": { "provider_config": { }, "models": { } } }
```

### 5. `openclaw_overrides.yaml` 初始建议

```yaml
claude-cli-gateway/claude-opus-5-5:  { visible: true }    # 仅文本：compaction、后台摘要、审稿；主分析师走原生 claude-oauth
antigravity-cli-gateway/gemini-3.8-flash: { visible: false }  # 旧 Dalton 实测传输上限约 170KB（model_profile_bounds.py）
antigravity-cli-gateway/gemini-4-argon:   { visible: false }  # "190KB prompt cap"
muse-cli-gateway/muse-wonder-full-1.0:    { visible: false }  # 价格未公开
xai/grok-4.7:            { speed: 3, intelligence: 4 }
openai/gpt-6.1-sol:      { speed: 3, intelligence: 5 }
deepseek/deepseek-flash: { speed: 5, intelligence: 3 }
```

不在 broker `allowedModels` 里的模型，本来就不会出现在目录里。

### 6. overlay 钩子和调用 id 钩子

**`src/llms/llm.py` 的 `ModelConfig.__init__`**，在读完两个 JSON、执行 `_flatten_providers` 之前加：

```python
# DALTON-HOOK(llm-overlay): merge the generated OpenClaw catalog without editing upstream manifests
from src.dalton.llm.overlay import apply_model_overlay
self.overlay_stamp = apply_model_overlay(self.llm_config, self.manifest)
```

**`LLM.get_model_config()`**：

```python
# DALTON-HOOK(llm-overlay): rebuild when the overlay file changed (re-sync button)
from src.dalton.llm.overlay import overlay_changed
if cls._model_config is None or overlay_changed(getattr(cls._model_config, "overlay_stamp", None)):
    cls._model_config = ModelConfig()
```

`apply_model_overlay` 的规则：
- 文件不存在时返回 `None`，什么都不做。
- models 的键和上游冲突时，**跳过并记 error 日志**，绝不覆盖上游条目。
- 返回的戳是 `f"{st_mtime_ns}:{st_size}"`。

`overlay_changed` 的规则：每个进程每 2 秒最多 `os.stat` 一次。这是从共享磁盘文件推导出的执行上下文缓存，不是状态真相；各 worker 各自重建，符合 multi-worker 约束。

**`src/server/app/api_keys.py` 的 `list_models()`**：开头检查 overlay 戳，变化时调用 `_build_provider_catalog.cache_clear()`。

**`_build_openai_params`**（约 4 行）：

```python
# DALTON-HOOK(llm-broker-client): stable invocation ids for the Dalton broker's idempotency journal
if self.provider_info.get("dalton_broker"):
    from src.dalton.llm.broker_http import broker_http_clients
    params.setdefault("http_async_client", broker_http_clients().async_client)
    params.setdefault("http_client", broker_http_clients().sync_client)
```

`broker_http.py` 的实现：
- httpx `event_hooks["request"]` 在每个 POST `/chat/completions` 上设置请求头：

  ```
  X-Dalton-Invocation-Id = "la1:" + sha256(scope + "\n" + request.content)[:40]
  ```

  其中 `scope` 取 `current_credit_gate.get().run_ref`（`src/ptc_agent/agent/middleware/credit_gate.py:572` 的 ContextVar），子智能体再拼上 `current_background_agent_id`（`agent/middleware/background_subagent/context.py`）。没有 gate 时 scope 用 `"nogate:" + uuid4()`，即不去重。
- 同一次逻辑调用的 SDK 重试和 `ModelResilienceMiddleware` 重试，请求体逐字节相同，所以 id 相同，broker 会回放或返回 409。不同调用的请求体不同，所以 id 不同。
- 例外：同一运行内出现逐字节相同的两次请求时，第二次会拿到回放。结果相同，没有副作用。
- 选这个方案而不是加中间件，是因为不需要改智能体中间件栈【待验证：openai SDK 重试时是否复用同一个 body 字节；`current_credit_gate` 在模型调用的 task 里可见】。
- 客户端是单例，连接池复用。它只依赖 contextvar，没有请求路径会读的模块级状态。
- 以后若升级到 HMAC，同一个钩子负责签名（P0 不做）。

**收到 409 `idempotency_indeterminate` 时：** LangAlpha 当作不可重试错误，经错误漏斗发出 `error`，由用户决定是否重新发起一轮。新的一轮会产生新的 run_ref 和新的 id，这是"人决定重新购买"。

### 7. 同步脚本

```
uv run python scripts/dalton/sync_openclaw_models.py
     [--broker-url http://127.0.0.1:18789/dalton-broker/v1]   # 默认取 DALTON_BROKER_URL，没有就用这个
     [--with-openclaw-config ~/.openclaw/openclaw.json]       # 宿主机专用：加入 v1 rateCard
     [--dalton-price-cards PATH] [--dry-run] [--check]
```

token 从 `DALTON_BROKER_TOKEN` 读取。写入时先写 tmp，再 `os.replace`。`--check` 在 overlay 与目录不一致、或键与上游冲突时退出码为 1，合并上游后使用。

### 验证方式（真实调用优先）

1. 先 `--dry-run` 看 diff：可见数、隐藏数、text-only 数和第 0.4 节的分类表一致（按 broker 的 `allowedModels`）。
2. 正式同步后：`curl -s localhost:8000/api/v1/models | jq '.models["openclaw-broker"], .model_metadata["ocb-xai-grok-4.7"]'`，应有分组、推理档位和 `price` 档。
3. 在 UI 里把默认模型切到 `Grok 4.7 · OpenClaw`，问一句话。检查：
   - 流式正常；
   - 推理强度下拉可用，切到 low 和 high 时，broker 响应的 `openclaw.reasoning` 跟着变；
   - `conversation_usages` 最新一行 `platform_cost>0`；
   - broker 日志里能看到 `X-Dalton-Invocation-Id`。
4. 跑 P0.5 的 B14（LangAlpha 路径）。

### 验收标准

- 上游 `src/llms/manifest/*.json` 零改动。
- `--check` 退出码 0。
- overlay 里没有任何凭证字段。
- 每个 openclaw-broker 模型条目都有 `dalton.openclaw_ref`、`dalton.upstream_provider`、`dalton.family`；`family=unknown` 的条目在 `meta.warnings` 里。
- 现金模型的对话按 overlay 价格入账；订阅模型 `platform_cost==0`。
- 同一次调用的重试在 broker 侧是同一个 invocation id（看 broker 日志）。

### 风险与注意

- broker 目录不可达时，同步失败，但保留旧 overlay，LangAlpha 照常运行。
- 上游重构 `_build_openai_params` 时，`llm-broker-client` 钩子可能失效。B14 列为合并上游后的回归项。
- 上游还有一个 `reasoning_lineage` 的 `lru_cache`（`src/llms/reasoning_lineage.py:68`），它只和 provider 有关。openclaw-broker 这个 provider 是固定的，不受重新同步影响【待验证】。

**预估工时：** 1.35 天（含 `dalton.*` 家族字段）。

---

## P0.5 冒烟测试 `scripts/dalton/openclaw_smoke.py`（broker 为主，网关和原生作对照）

**目标：** 用真实调用回答四个问题，每项给出通过/失败和对方案的影响：
1. 我们发出的完整对话是否被原样处理；
2. 是否被注入了别人的提示词；
3. 推理强度、缓存 token、成本能否拿到；
4. 幂等和"不静默换模型"是否成立。

**前置依赖：** P0.3（broker v2 已装好）。`--target gateway` 只需要网关，`--target native` 需要 LangAlpha 在跑并已登录 OAuth。

**新增文件：** `scripts/dalton/openclaw_smoke.py`【新建】。报告写到 `var/dalton/smoke/openclaw_smoke_<target>_<UTC>.json`，并更新 `latest_<target>.json`。

### 命令行

```
uv run python scripts/dalton/openclaw_smoke.py
  --target broker|gateway|native      # 默认 broker
  [--base-url ...]                    # broker 默认 http://127.0.0.1:18789/dalton-broker/v1
  [--models xai/grok-4.7,deepseek/deepseek-flash,openai/gpt-6.1-sol,google/gemini-3.8-flash,zai/glm-5.3,claude-cli-gateway/claude-opus-5-5]
  [--checks all|B0,B3,...] [--langalpha-key ocb-xai-grok-4.7] [--max-spend-usd 0.50]
```

实现约束：
- 用 httpx 直连；token 只从环境变量读，绝不打印，日志里写 `***`。
- 不发 `user`。
- 每条记录 `{id, target, model, status, evidence, consequence}`，同时保存耗时、首 token 时间、`usage` 原文和 HTTP 状态码。
- 阻断项失败时退出码为 2。

### broker 检查项（B）

除非注明，B1–B4、B9、B10 对每个模型都跑。

| ID | 检查 | 通过条件 | 失败的后果 |
|---|---|---|---|
| B0 | `GET /health`、`GET /models`、`GET /models?detail=1` | 200；目录与 broker 的 `allowedModels` 一致；没有任何凭证字段 | 阻断 |
| B1 | 普通对话 | 返回 PONG，`usage.prompt_tokens>0` | 该模型阻断（overrides 里 `visible:false`） |
| B2 | 流式 + `include_usage` | ≥3 个增量块，最后有 usage 块；空闲时有 keepalive | 阻断 |
| B3 | 工具往返（非流式和流式两种） | 工具名和参数正确；回填结果后答案含 612.34 | 阻断（text-only 模型改测 B11） |
| B4 | 并行工具：同时查 META 和 AMZN | 2 个调用为 pass，1 个为 warn | warn：记录 |
| B5 | 多轮保真：伪造的 assistant 轮 | 答 PELICAN-7 | 阻断 |
| B6 | 无注入：SENTINEL 复述；`"hi"` 的 prompt_tokens | 复述以 SENTINEL 开头；prompt_tokens 小于 50 | 阻断（说明有东西在我们前面加了提示词） |
| B7 | 无内部工具：不传 tools 时问"你能调用哪些工具" | 答 NONE | 阻断 |
| B8 | 推理强度：low 对比 high | `openclaw.reasoning` 跟着变；high 的推理 token 或耗时明显更多 | 不阻断；推理档位从 manifest 去掉 |
| B9 | 缓存：约 4k 前缀连发两次 | 第二次 `cached_tokens>0` | warn：按全价估算，偏保守 |
| B10 | 成本：`usage.cost_usd` | 现金模型 >0；订阅模型如实（可以是 0，或名义值） | 不阻断，以本地价格卡为准；但要记录偏差（B15） |
| B11 | text-only：`claude-cli-gateway/*` 带 tools | 400 `tools_unsupported`；不带 tools 时正常 | 阻断：防止压平 |
| B12 | 允许列表：`xai/grok-4.3`、`nosuch/model` | 404 `model_not_allowed`，不能返回 200 | 阻断：防止静默换模型 |
| B13 | 幂等：同 id 重放；同 id 不同请求体；流式中途断开后重放 | 第一种 `replayed=true` 且没有新的 dispatch；第二种 409 conflict；第三种拿到完整回放 | 阻断 |
| B14 | LangAlpha 路径（容器内）：`create_llm(<key>)` 流式加工具 | 线上 `model` 等于 `xai/grok-4.7`；带有 `X-Dalton-Invocation-Id`；`pricing_model_id` 正确；有 usage | 阻断 P0.4 验收 |
| B15 | 成本对账：本地价格卡算出的成本，对比 broker 的 `cost_usd` | 相差 5% 以内为 pass | warn：价格卡与 OpenClaw 计价不一致，查 `price_discrepancies` |
| B16 | 回退：只在显式要求时 | 不带 `X-Dalton-Fallback` 时直接报错；带上时切换，并写明 `fallbackFrom` | 阻断 |

### 对照组

- **G（`--target gateway`，原方案 A）：** 跑原草稿的 S1、S5、S7、S8、S9、S11 子集，只为留证据，说明为什么不用它。结果预期是 S7、S9、S11 fail（§0.2）。**不阻断任何东西。**
- **N（`--target native`）：** 对 `claude-opus-5-5-oauth` 跑 B1、B2、B3、B5、B6。走 LangAlpha 自己的客户端（容器内 `create_llm`）。验证主分析师的备选路径可用。

### 验收标准

- `latest_broker.json` 里 B 系列阻断项全部 pass；`latest_native.json` 里 N 系列 pass。
- `latest_gateway.json` 存在，作为对照证据。
- 所有报告里都没有 token：用 `grep` 检查。

**预估工时：** 0.75 天。

---

## P0.6 角色模型配置、子智能体模型与回退策略

**目标：** 用 `agent_config.dalton.yaml`（APP_ENV overlay）给出各角色的默认模型，满足四条约束：
- 审稿人和主模型来自不同家族；
- 用工具的角色不能配 text-only 模型；
- 回退只针对供应商故障；
- 回退必须可见。

**前置依赖：** P0.4，以及 P0.5 的模型结果。

**改动/新增文件：**
- `agent_config.dalton.yaml`【新建，`git add -f`】
- `src/dalton/llm/role_policy.py`【新建】：`family_of(model_key)`（P0.4 第 3 节）、`is_text_only(model_key)`、`check_roles(config) -> list[warning]`。校验家族独立性和 text-only 约束，供 P0.7 的卡片、PK 的发布审稿（PK.10）和 PS 的草稿期审稿复用。
- `scripts/dalton/apply_role_models.py`【新建】：切换主模型后运行一次。用 `role_policy.family_of()` 从 `publish.reviewer_models` 里挑第一个与主模型家族不同、支持工具的模型，写入 `plugins/dalton/agents/dalton-reviewer.md` 的 `model:` 字段（PK.10），并打印 `check_roles` 的警告。验收：把主模型切到 Gemini 后运行，审稿子智能体不再是 Gemini 家族。

### 主分析师模型可切换（不写死）

主分析师模型是一个配置值，不是代码里的常量。切换方式有两种，任选其一，**不需要改代码、不需要重启**：
- 全局默认：改 `agent_config.dalton.yaml` 的 `llm.name`（需重启 backend，因为 overlay 在启动时读取）；
- 按用户：在 Settings → 模型 → 默认模型（原生 `ModelTierConfig`）里选。用户设置优先于 YAML（见下文"优先级"）。

| 选项 | 模型键 | 计费 | 说明 |
|---|---|---|---|
| **默认** | `claude-opus-5-5-oauth` | Claude 订阅（不占现金额度） | LangAlpha 原生 claude-oauth；在 Settings → Providers & accounts → Connected Accounts 里登录 Claude。**风险：** 用订阅 OAuth 驱动第三方 agent 可能不符合 Claude 订阅的使用条款，存在被限制或封号的可能；出现问题就切到下面两项之一 |
| 替补一 | `ocb-xai-grok-4.7` | 现金（$2/$6 每百万 token） | broker v2；N 系列不通过、或不想用订阅 OAuth 时用 |
| 替补二 | 现金 API key 的原生模型，例如 `deepseek`、`gemini`、`z-ai`（或其他在 `providers.json` 里有 `env_key` 的供应商） | 现金，计入 Dalton 日额度 | 供应商自己的 API，不经过 OpenClaw。key 写进 `.env`（平台 key，`billing_type=platform`，计入日额度）；**不要**在 Settings 里按 BYOK 配，BYOK 调用不计入日额度（P0.8 第 7 节）【待验证：该供应商的 env key 在 OSS 模式下按 platform 计费】 |

切换后，审稿人按 `family_of()` 自动换成另一家族（见下表"审稿人"）。CLI gateway 类模型（antigravity、muse、claude-cli）**不能**当主分析师或任何用工具的子智能体，只做纯文本角色：compaction、后台摘要、审稿。

### 默认选型（P0.5 之后修订）

| 角色 | 配置键 | 建议 | 理由 | 替补 |
|---|---|---|---|---|
| 主分析师（lead） | `llm.name` | **`claude-opus-5-5-oauth`**（见上一节） | 最强；订阅不占现金额度；原生工具调用和流式 | `ocb-xai-grok-4.7`，或现金 API key 的原生模型 |
| background | `llm.background` | `ocb-deepseek-deepseek-flash` | 现金最便宜，1M 上下文 | `ocb-zai-glm-5.3-flash`；纯文本的后台摘要也可以用 `ocb-claude-cli-gateway-claude-haiku-5-5`【待验证：background slot 是否会带工具调用】 |
| compaction | `llm.compaction` | `""`（使用本轮模型） | LangAlpha 默认 | 订阅想省额度时可以用 `ocb-claude-cli-gateway-claude-sonnet-5-5`（仅文本，compaction 不需要工具【待验证】） |
| fetch | `llm.fetch` | `""`（跟随 background） | — | — |
| fallback | `llm.fallback` | `[]` | 见下文 | — |
| 子智能体 research、data-prep、general-purpose | `subagents.definitions.<name>.model`【待验证：只覆盖 model 字段是否可行，见 `src/ptc_agent/agent/subagents/merge.py`】，或在 Plugins → Subagents 里按用户设置 | `ocb-xai-grok-4.7` | 并行取证；现金模型并发稳定 | `ocb-deepseek-deepseek-flash` |
| 子智能体 equity-analyst、report-builder | — | 继承 lead | 写作质量 | — |
| 发布审稿（PK.10，服务端 `LLMService.complete`，不带工具） | `src/dalton/dalton.yaml` 的 `publish.reviewer_models`（有序列表） | 依次为 `ocb-openai-gpt-6.1-sol`、`ocb-google-gemini-4-argon`、`ocb-claude-cli-gateway-claude-opus-5-5`、`claude-opus-5-5-oauth`，取第一个 `family_of()` 不同于作者模型且可用的 | **家族必须不同于作者**；服务端审稿不带工具，所以 text-only 模型也可以 | — |
| 草稿期审稿子智能体（PS.17，自愿） | 子智能体 `.md` 的 `model` | 同上列表，但只取能用工具的模型（要读来源） | 同上 | — |

`agent_config.dalton.yaml` 样例：

```yaml
llm:
  name: "claude-opus-5-5-oauth"
  background: "ocb-deepseek-deepseek-flash"
  compaction: ""
  fetch: ""
  fallback: []
sandbox:
  provider: "docker"
  docker: { memory_limit: "3g", cpu_count: 2.0 }
  daytona: { auto_stop_interval: 2400 }   # 只用于推导空闲回收阈值（P0.1 第 5 步）
subagents:
  definitions:
    research:        { model: "ocb-xai-grok-4.7" }
    data-prep:       { model: "ocb-xai-grok-4.7" }
    general-purpose: { model: "ocb-xai-grok-4.7" }
# 数据篇 P0-D.2 的超时设置也写在这个文件里（researchdata 的 call_timeout_s、security.max_execution_time），见那一节
```

注意：`claude-opus-5-5-oauth` 只有在当前用户完成 Claude OAuth 登录后才可用。没有登录时，LangAlpha 的可用性检查会把它判为不可用【待验证：表现为报错，还是回落到系统默认模型】。所以 P0 验收前必须先登录，或先把 `llm.name` 设为 `ocb-xai-grok-4.7`。

### 优先级

- 用户设置优先于 YAML：`preferred_model`、`preferred_background_model`、`compaction_model`、`fetch_model`、`fallback_models`（`src/server/services/llm/config.py` 的 `resolve_llm_config` → `select_model`）。
- 子智能体还可以在 Plugins → Subagents 里按用户设置（`web/src/pages/Plugins/components/subagents/SubagentAside.tsx`）。

### 回退策略

- LangAlpha 的 `ModelResilienceMiddleware`（`src/ptc_agent/agent/middleware/model_resilience.py`）在 400、认证失败、未知模型这类不可重试的错误上**也会**切换到下一个 fallback。切换会发出 `model_fallback` 记录，可见，但触发面比我们的底线宽。所以 **P0 设 `fallback: []`**。
- 供应商的 5xx、429、超时：中间件在同一个模型上退避重试。因为 invocation id 稳定（P0.4 第 6 节），重试在 broker 侧会变成回放或 409，**不会重复购买**。
- broker 层的回退只在请求带 `X-Dalton-Fallback` 时发生。P0 不带这个头。
- 审稿人永远不配回退。审稿缺席时，草稿仍然可读，但不能升级为正式版本。

### 验证方式

1. `curl -s localhost:8000/api/v1/models | jq .system_defaults` 符合配置。
2. 派出 research 子智能体的问题，检查子智能体实际用的是 grok-4.7（看子智能体卡片，或 broker 日志的 model 字段）。
3. 故障演练：把 `DALTON_BROKER_TOKEN` 改错，然后用 broker 模型发消息。期望直接报 401，没有 `model_fallback` 记录。演练完改回。
4. `role_policy.py`：故意把审稿列表的第一项设成和 lead 同家族，确认 `family_of()` 跳过它；把 research 设成 text-only 模型，P0.7 的卡片应该给出警告。
5. 切换主分析师：在 Settings 里把默认模型改成 `ocb-xai-grok-4.7`，新开线程确认生效（broker 日志的 model 字段），审稿人选择随之变化（`check_roles` 的输出）；再切回。

### 验收标准

- 以上 5 条全部成立。

**预估工时：** 0.6 天。

---

## P0.7 设置页：OpenClaw "重新同步模型" 按钮 + broker 健康指示（owner 已确认纳入 P0）

> 范围按 owner 和 lead 的决定：只在现有 Settings → 模型页（`web/src/pages/Settings/panels/ModelTab.tsx`）加一张 OpenClaw 卡片。
>
> 原生 ModelTab 已有的功能**不重做**：默认和 background 模型（`ModelTierConfig`）、fetch/compaction（`ModelSelector`）、回退（`FallbackModelsPicker`）、按模型调参（`PerModelMatrix`）。子智能体模型仍然在 Plugins → Subagents 设置。
>
> 同步之后，新模型自动出现在这些原生控件里，因为它们都读 `/api/v1/models`。

**目标：**
- 点一下按钮，就执行和 `scripts/dalton/sync_openclaw_models.py` 完全相同的同步逻辑。按钮返回新增、删除、变更的模型清单，并且不重启后端就生效。
- 卡片上能看到：上次同步时间、模型数、错误信息、网关健康状态。

**前置依赖：** P0.4。同步逻辑在 `src/dalton/llm/openclaw_catalog.py`，overlay 钩子已就位。数据来源是 broker v2 的 `GET /dalton-broker/v1/models?detail=1`，在容器里直接可达，不需要挂载 `openclaw.json`。

### 1. 存储决策：生成的条目放哪里

**先看现状。** 前端的 `useAllModels`（`web/src/hooks/useAllModels.ts`）只依赖一个接口：`GET /api/v1/models`（`useModels` → `web/src/api/model.ts` 的 `getAvailableModels`，query key `queryKeys.models.all`，staleTime 5 分钟）。这个接口的数据来源如下：

- `src/server/app/api_keys.py` 的 `list_models()`（约第 809 行）调用 `get_configured_llm_models()`、`LLM.get_model_config()`，以及带 `lru_cache` 的 `_build_provider_catalog()`（第 721 行）；
- 这几个函数最终都读 `ModelConfig`。它是进程级单例，在构造时读两个 manifest 文件；
- 运行时的 `create_llm`、计价（`pricing_utils`）、BYOK 判断也都走同一个单例。

**三个方案的比较：**

| 方案 | 重启后保留 | 合并上游 | 多 worker | 侵入性 | 结论 |
|---|---|---|---|---|---|
| a. 直接改提交的 `models.json` / `providers.json` | 是 | **差**：上游频繁改这两个文件，每次合并都会冲突 | 需重启 | 不需要钩子，但上游文件被改 | 否 |
| b. DB 表（例如 `dalton_model_catalog`），由 catalog 读取 | 是 | 好 | 好 | **高**：`ModelConfig` 是同步构造，被大量同步代码调用（`LLM.__init__`、`pricing_utils` 等），无法在里面做 async DB 读取，只能另起一层缓存 | 否 |
| **c. 生成的 overlay 文件 `src/dalton/llm/openclaw_overlay.json`（提交到仓库），在 `ModelConfig.__init__` 里合并（P0.4 的 `llm-overlay` 钩子）** | 是 | 好：独立文件，不会冲突 | 每个 worker 从同一个磁盘文件重建（下文第 3 点） | 低：两处钩子，约 10 行 | **采用** |

**补充：** 同步**运行记录**（时间、结果、错误）写 Postgres 表 `dalton_openclaw_sync_runs`。状态真相在 Postgres，目录内容的真相在 overlay 文件。

部署上的注意：
- dev compose 把 `./src` 挂进容器，所以容器里点按钮写的 overlay 会落到宿主机仓库，`git status` 能看到变化。这是有意的：按钮同步过的结果，可以经正常 PR 提交下来。
- 如果以后在**不挂载源码**的镜像里运行（如 `docker-compose.prod.yml`），需要把 `src/dalton/llm/` 改成挂载的 volume。否则重建镜像后会退回提交时的版本【P0 不涉及，写入已知限制】。

### 2. 改动/新增文件

| 路径 | 状态 | 说明 |
|---|---|---|
| `src/dalton/app/__init__.py`、`src/dalton/app/router.py` | 【新建】 | `APIRouter(prefix="/api/v1/dalton")`，只做编排 |
| `src/dalton/services/openclaw_admin.py` | 【新建】 | `run_sync(user_id, dry_run, force)`、`probe_health()`、`last_sync()` |
| `src/dalton/database/openclaw_sync.py` | 【新建】 | 原生 SQL：同步运行记录 |
| `src/dalton/migrations/versions/d002_openclaw_sync_runs.py` | 【新建】 | Dalton 独立迁移链（见 P0.8 的说明），`down_revision="d001"` |
| `src/server/app/setup.py` | 钩子 `DALTON-HOOK(router)` | 在 `app.include_router(subagents_router)`（约第 1324 行）旁加 `app.include_router(dalton_router)` |
| `web/src/pages/Settings/panels/dalton/OpenClawCard.tsx` | 【新建】 | 卡片本身 |
| `web/src/pages/Settings/panels/dalton/SyncDiffDialog.tsx` | 【新建】 | dry-run 的 diff 确认框 |
| `web/src/pages/Settings/utils/daltonApi.ts` | 【新建】 | 页面自己的 api 层，走 `api/client.ts` |
| `web/src/pages/Settings/hooks/useOpenClaw.ts` | 【新建】 | React Query：`useOpenClawStatus`、`useOpenClawHealth`、`useOpenClawSync` |
| `web/src/pages/Settings/panels/ModelTab.tsx` | 钩子 `DALTON-HOOK(settings-section)` | import 加 `<DaltonSection />`（内含本卡片和 P0.8 的预算卡），放在 Providers & accounts 小节（`ConnectedAccounts`，约第 515 行）之后，共 2 行 |
| `web/src/pages/Settings/panels/dalton/DaltonSection.tsx` | 【新建】 | 小节容器 |
| `web/src/lib/queryKeys.ts` | 钩子 `DALTON-HOOK(query-keys)` | `dalton: { all: ['dalton'], openclawStatus: () => ['dalton','openclaw','status'], openclawHealth: () => ['dalton','openclaw','health'], budget: ..., budgetToday: ... }` |
| `web/src/locales/en-US.json`、`zh-CN.json` | 钩子 `DALTON-HOOK(i18n)` | `dalton.openclaw.*` 键，两边都要加 |

### 3. 后端接口

鉴权统一用 `CurrentUserId`（`src/server/utils/api.py`）。

#### 3.1 `POST /api/v1/dalton/openclaw/models/sync`

请求体：`{"dry_run": bool = false, "force": bool = false}`。

流程：
1. 在 app 连接池上执行 `SELECT pg_try_advisory_lock(hashtext('dalton:openclaw_sync'))`。拿不到锁就返回 **409** `{"code":"sync_in_progress"}`。锁在 Postgres 里，跨 worker 生效；`finally` 里 `pg_advisory_unlock`。
2. 插入一行运行记录：`status='running'`，`dry_run`，`started_by=user_id`。
3. 用 `DALTON_BROKER_TOKEN` 拉取 `GET {broker}/models?detail=1`（超时 10 秒），再调用 `openclaw_catalog.build_overlay(catalog=..., overrides=..., price_cards=None)`。这和 CLI 用的是同一个函数，CLI 只是包装；容器内不使用 `--with-openclaw-config`。然后执行 `diff_overlay(current, new)`。如果当前 overlay 是宿主机 CLI 带着 v1 rateCard 生成的（`meta.source=broker+openclaw_config`），容器内同步时**保留原有的 rateCard 价格来源**：已有价格只能上调，不能下调，并在 warnings 里注明。
4. **引用保护：** 新 overlay 会删除或隐藏的模型键，如果正被以下任一处引用，且 `force=false`，就把它列进 `blocked`，不写入，返回 **409** `{"code":"in_use", "blocked":[{"key","referenced_by"}]}`：
   - `setup.agent_config.llm` 的 `name` / `background` / `compaction` / `fetch` / `fallback`；
   - YAML 里的子智能体 `definitions.*.model`；
   - 任一用户的 model 偏好：`preferred_model`、`preferred_background_model`、`fetch_model`、`compaction_model`、`fallback_models`；
   - 用户子智能体设置。

   原因：`agent_config` 指向不存在的模型时，下次启动会失败（`src/ptc_agent/config/agent.py` 里有"not found in models.json"校验）；用户偏好里的模型被删掉后，会触发 `model_removed`。用户偏好的查询方式参照 `src/server/services/llm/user_models.py` 的 `get_model_preference`，SQL 写在 `src/dalton/database/` 里，不复用上游的私有函数【待验证：读取所有用户偏好的现成入口】。
5. 不是 `dry_run` 时，先写 tmp 文件，再 `os.replace` 到 `src/dalton/llm/openclaw_overlay.json`（原子替换），然后在**当前 worker** 上执行 `LLM._model_config = None` 和 `_build_provider_catalog.cache_clear()`。其他 worker 在 2 秒内通过 overlay 戳（P0.4）自行重建，不需要任何进程间通信。
6. 更新运行记录：`status='succeeded'|'failed'|'blocked'`，填入 `finished_at`、`added`、`removed`、`changed` 的数量和明细（JSONB）、`warnings`、`error`、`overlay_stamp`、`public_extract_sha256`。
7. 返回体：

```json
{ "run_id": "...", "dry_run": false, "applied": true,
  "added":   [{"key":"ocb-xai-grok-4.7","openclaw_ref":"xai/grok-4.7","display_name":"..."}],
  "removed": [{"key":"...","openclaw_ref":"...","reason":"not_in_allowlist|gone_from_openclaw|hidden_by_override|unpriced"}],
  "changed": [{"key":"...","fields":{"context":[500000,480000],"pricing.output":[6,7.5],"visible":[true,false]}}],
  "unchanged": 18, "warnings": ["..."], "price_discrepancies": [{"ref":"google/gemini-3.8-flash","field":"input","values":{"rateCard":1.5,"openclaw.cost":0.75},"chosen":1.5}],
  "blocked": [], "synced_at": "2026-10-18T09:12:03Z", "counts": {"allowlist":25,"visible":21,"hidden":4,"skipped_not_allowed":2} }
```

**错误码：**

| 状态码 | code | 含义 |
|---|---|---|
| 409 | `sync_in_progress` | 另一个同步正在进行 |
| 409 | `in_use` | 要删除或隐藏的模型正被引用 |
| 502 | `broker_unreachable` | broker 连不上、401，或超时；返回体带健康状态 |
| 422 | `catalog_invalid` | 目录结构不合法，或为空；为空时拒绝写入，避免误删全部模型 |
| 500 | — | 其他错误；运行记录仍写 `failed` |

错误文本要先脱敏再上线：只给出路径和字段名，不回显文件内容。

#### 3.2 `GET /api/v1/dalton/openclaw/models/status`

只读。返回：
- 当前 overlay 的 `meta`：`generated_at`、`counts`、`warnings`；
- 最近一次同步运行，以及最近一次成功同步（从 `dalton_openclaw_sync_runs` 读取）；
- 模型表：`[{key, openclaw_ref, display_name, family, subscription, visible, context, max_output, pricing_summary:{input,output,cached_input}|null, notional_summary|null, warnings[]}]`。

#### 3.3 `GET /api/v1/dalton/openclaw/health`

流程：
1. 用 `httpx.AsyncClient(timeout=3.0)` 请求 `GET {base}/models`。
   - 实际请求 `GET {broker}/health`。`broker` 取 openclaw-broker provider 的 `base_url` 做 `{HOST_IP}` 替换后的值，和运行时同一个解析（`src.config.env.HOST_IP`）。
   - 请求头 `Authorization: Bearer <DALTON_BROKER_TOKEN>`，只从环境变量读，经 `src/dalton/settings.py` 的 accessor 取得。
2. 判定状态：

| 状态 | 条件 |
|---|---|
| `ok` | 200，且 `status=="ok"` |
| `degraded` | 200，但 journal 的 pending 数大于 0，或剩余容量低于 10%（提示"有未决调用"） |
| `not_installed` | 网关可达，但该路径返回 404（插件没装或没启用） |
| `gateway_restarting` | 503 |
| `auth_error` | 401 或 403 |
| `token_missing` | 环境变量为空 |
| `unreachable` | 连接错误或超时 |
| `error` | 其他状态码 |

返回：

```json
{"status", "http_status", "latency_ms", "checked_at", "base_url": "<只给 host:port，不含凭证>",
 "broker": {"brokerVersion", "openclawVersion", "journal", "inflight", "queued"},
 "smoke": {"generated_at", "blocking_failures": ["B6", ...]} | null}
```

`smoke` 读自 `var/dalton/smoke/latest_broker.json`，P0.1 已挂载这个目录。

约束：
- **不在进程内缓存**，每次实时探测。
- 这是免费调用：不跑模型。
- 前端每 60 秒轮询一次，只在卡片可见时轮询。
- 任何响应和日志都不回显 token。

#### 3.4 迁移 `d002_openclaw_sync_runs.py`

```sql
CREATE TABLE IF NOT EXISTS dalton_openclaw_sync_runs (
  run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_by    VARCHAR(255) NOT NULL,
  dry_run       BOOLEAN NOT NULL,
  forced        BOOLEAN NOT NULL DEFAULT FALSE,
  status        TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','blocked')),
  started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at   TIMESTAMPTZ NULL,
  counts        JSONB NULL,          -- {"added":n,"removed":n,"changed":n,"visible":n,...}
  diff          JSONB NULL,          -- added/removed/changed 明细（不含任何凭证）
  warnings      JSONB NULL,
  error         TEXT NULL,
  overlay_stamp TEXT NULL,
  public_extract_sha256 TEXT NULL
);
CREATE INDEX IF NOT EXISTS ix_dalton_sync_runs_started ON dalton_openclaw_sync_runs (started_at DESC);
```

崩溃后可能残留 `running` 的记录：`status` 接口把 `running` 且超过 5 分钟的记录显示为"中断"。不用后台任务清理，读取时判定即可。

CLI（`scripts/dalton/sync_openclaw_models.py`）也写这张表：能连上 DB 时写，连不上时只打印。这样宿主机同步也能在卡片上显示出来。

### 4. 多 worker 安全（逐条对照 `src/server/AGENTS.md` 的检查清单）

- **没有请求路径会读的模块级状态。** overlay 戳和 `_build_provider_catalog` 的缓存都是"从共享文件推导出的执行上下文"，任何 worker 都能各自重建，不把它当状态真相。
- **并发写**由 Postgres advisory lock 串行化；写文件用原子替换，读的一方要么看到旧版本，要么看到新版本。
- **同步状态**以 `dalton_openclaw_sync_runs` 为准，不依赖哪个 worker 处理了请求。
- **没有 Redis 或 pub/sub 依赖。** 其他 worker 错过"通知"也没关系：下一次 `get_model_config()` 看到戳变化就会重建（最多 2 秒）。
- **验证：** 用 `--workers 2` 启动（在 `docker-compose.dalton.yml` 里临时覆盖 `command`，加 `--workers 2`，并去掉 `--reload`，两者互斥），按第 6 节的 A9 重复 5 次。还要连续打 20 次 `/api/v1/models`，确认所有响应都已切换到新版本。

### 5. UI：位置与状态

卡片在 ModelTab 的 Providers & accounts 小节之后，标题"OpenClaw 模型网关"。组件全部复用 `components/ui/` 的原语，样式遵守 `DESIGN.md`、`styles/tokens.css`。

**头部一行：**
- 健康胶囊：复用 `StatusPill`（`web/src/components/mcp/McpPrimitives.tsx`），配色参照 `web/src/pages/ChatAgent/components/mcp/McpStatusPill.tsx`。
- 文案：

| 状态 | 胶囊文案 | 颜色 |
|---|---|---|
| ok | "broker 正常 · 23ms" | profit 色 |
| degraded | "有 N 个未决调用" | warning 色 |
| not_installed | "broker 未安装" | warning 色 |
| gateway_restarting | "网关重启中" | warning 色 |
| auth_error | "令牌无效" | warning 色 |
| token_missing | "未配置令牌" | warning 色 |
| unreachable | "不可达" | loss 色 |

  悬停时显示 `checked_at` 和 `base_url`。
- 右侧有一个"立即检查"小按钮，用于手动刷新健康状态。

**状态行：**
- "上次同步：2026-10-18 17:12（成功）"，时间用 `lib/format.ts` 按本地时区和 locale 格式化，相对时间用 `useNow()`。
- "可见 21 · 隐藏 4 · 不在允许列表 2"。
- 最近一次失败或被阻断时，显示红色或黄色提示条，内容为错误摘要和"查看详情"（展开 `error`、`blocked`、`warnings`）。
- 最近一次冒烟："2026-10-18 · 阻断失败 0"。没有报告时显示"尚未运行冒烟测试"，并给出命令提示。

**主按钮"从 OpenClaw 重新同步"：**

| 状态 | 表现 |
|---|---|
| 空闲 | 可点 |
| 预览中 | 调 `dry_run:true`，按钮转圈 |
| 预览完成 | 弹出 `SyncDiffDialog`：分三栏列出新增、删除（带原因）、变更（字段新旧值），价格差异单独一节。没有变化时显示"已是最新"，只给"关闭" |
| `in_use` 阻断 | 对话框列出被引用的模型和引用位置，提供两个出口："去修改引用"（链接到上方默认模型控件或 `/plugins?tab=subagents`），以及"强制应用"（二次确认，`force:true`） |
| 应用中 | 确认后调 `dry_run:false`，按钮转圈，禁用 |
| 成功 | toast "已同步：+2 −1 ~3"。执行 `invalidateQueries(queryKeys.models.all)` 和 `queryKeys.dalton.all`，原生的默认模型、回退等下拉立即刷新 |
| 失败 | 提示条显示 `code` 对应的中文说明（`sync_in_progress` / `broker_unreachable` / `catalog_invalid` / 其他），保留最后一次成功的结果 |

**角色检查行（只读）：** 调用 `role_policy.py`（P0.6）。主模型或子智能体用了 text-only 模型，或者审稿人和主模型同家族时，显示 warning 提示条，并链接到上方的默认模型控件或 `/plugins?tab=subagents`。不在这里重新做角色配置。

**折叠区"模型清单"：** 表格列为显示名、家族、上下文、最大输出、价格（`$in / $out`，每百万 token）或"订阅（名义 $x / $y）"、可见性（隐藏的写原因）、工具能力（"仅文本"标记）。只读；需要修改时，引导去编辑 `src/dalton/llm/openclaw_overrides.yaml`。

**约束：**
- API 调用只放在 `utils/daltonApi.ts` 和 hooks 里，组件里不直接 fetch。
- query key 一律来自 `queryKeys.ts`。
- 不在本地镜像服务端状态。

### 6. 验证方式（真实调用）

1. 健康检查：
   - 正常时显示 `ok`；
   - 把 `.env` 的 token 改错并重启，显示 `auth_error`；
   - 停掉 OpenClaw 网关（lead/运维操作，或临时把 `base_url` 端口改错），显示 `unreachable`；
   - 全部恢复。
2. 无变化同步：点按钮，预览显示"已是最新"。运行记录里多一行 `dry_run=true, succeeded`。
3. 有变化同步（不需要动 OpenClaw 配置）：在 `src/dalton/llm/openclaw_overrides.yaml` 里给 `zai/glm-5.3` 加 `visible: false`，点按钮：
   - 预览显示 `removed: ocb-zai-glm-5.3 (hidden_by_override)`；
   - 确认后**不重启**，原生默认模型下拉里 GLM-5.3 消失；
   - `curl -s localhost:8000/api/v1/models | jq '.model_metadata["ocb-zai-glm-5.3"]'` 返回 `null`；
   - 恢复后再同步，模型重新出现。
4. 引用保护：把默认模型设为 GLM-5.3 后重复第 3 步，返回 409 `in_use`，对话框列出 `preferred_model`。"强制应用"后，原生的 `model_removed` 处理会清理偏好（上游行为）。
5. 并发：两个浏览器标签同时点"应用"，一个成功，另一个返回 409 `sync_in_progress`。
6. 多 worker：按第 4 节执行。
7. 宿主机 CLI 同步后，卡片的"上次同步"更新为 CLI 运行的那次（DB 可达时）。
8. 错误路径：临时把 `.env` 的 `DALTON_BROKER_TOKEN` 改错，同步返回 502 `broker_unreachable`，旧 overlay 保持不变，卡片显示错误。

### 7. 验收标准（lead 检查）

- 上面 8 条全部符合预期。
- 卡片显示上次同步时间、数量、错误或阻断、健康状态和冒烟摘要。
- 同步结果不重启就生效，在两个 worker 下也成立。
- 上游的 `src/llms/manifest/*.json` 零改动。
- 新增的上游钩子只有 `router`、`settings-section`、`query-keys`、`i18n` 四处。`llm-overlay`、`llm-broker-client`、`catalog-cache` 已在 P0.4 加入。
- 所有接口响应和日志里都没有 token。
- `pnpm typecheck`、`pnpm lint`（0 error）、`web/src/locales/__tests__/keys.test.ts` 通过。

### 8. 风险与注意

- 容器内同步依赖 broker v2 可用。broker 不可用时（例如 NO-GO 或网关重启），按钮报错，但旧 overlay 继续生效。
- `useModels` 的 5 分钟 staleTime：成功后必须主动 invalidate，已写进第 5 节。
- 价格只在本卡片展示。不改上游的 `get_model_metadata`，它只下发 1–5 档的 `price`。

**预估工时：** 1 天（后端 0.5 天，前端 0.4 天，多 worker 验证 0.1 天）。

---

## P0.8 本地日美元预算（LocalBudgetPort）

**目标：** OSS 模式下实现一个和 `PlatformCreditGatePort` 同接口的本地 port，语义按旧 Dalton：
- 共享日美元额度和可选 task cap 是**唯一**的硬约束，不加任何次数限制；
- 发送前预留，按实际用量结算；
- 未结算的预留按全额占用；
- 进程崩溃或 TTL 过期时保守入账，不释放。

额度用尽时，运行以现成的 `credit_pause` 卡片暂停。

**前置依赖：** P0.1（数据库）、P0.4（价格卡，成本才能算对）。

**改动/新增文件：**

| 路径 | 状态 |
|---|---|
| `src/dalton/settings.py`、`src/dalton/dalton.yaml` | 【新建】，开关和行为默认值 |
| `src/dalton/budget/__init__.py`、`port.py`、`gate.py`、`service.py` | 【新建】 |
| `src/dalton/database/budget.py` | 【新建】，原生 SQL（psycopg3，用 `src.server.database.pool.get_db_connection`） |
| `src/dalton/alembic.ini`、`src/dalton/migrations/env.py`、`src/dalton/migrations/script.py.mako`、`src/dalton/migrations/versions/d001_budget.py` | 【新建】，独立迁移链 |
| `src/server/services/credit_gate_port.py` | 钩子 `DALTON-HOOK(budget-gate)`：`build_run_credit_gate` 和 `verify_credit_gate_wiring` 开头各 3 行 |
| `src/server/dependencies/usage_limits.py` | 钩子 `DALTON-HOOK(budget-admission)`：`enforce_credit_limit` 开头 3 行 |
| `src/dalton/app/router.py` | 加预算接口 |
| `web/src/pages/Settings/panels/dalton/DaltonBudgetCard.tsx` | 【新建】 |
| `web/src/pages/ChatAgent/session/interrupts/creditPauseCard.ts` | 钩子 `DALTON-HOOK(pause-links)`：OSS 下把链接换成"调整日预算" |

### 1. 现有接口（必须兼容）

接口是鸭子类型，没有 Protocol 类，实际契约由 `tests/unit/middleware/test_credit_gate.py` 的 `FakePort` 固定：

```python
# src/server/services/credit_gate_port.py: PlatformCreditGatePort 的方法签名
async def acquire(self, user_id: str, run_ref: str, spent_credits: float,
                  rate_multiplier: float = 1.0, byok: bool = False) -> Optional[LeaseVerdict]
async def release(self, user_id: str, run_ref: str, generation: Optional[int] = None) -> None
async def capability(self) -> Optional[dict]
async def heartbeat(self, kind: credit_ledger.RunKind, run_ref: str, credits: float) -> Optional[bool]

# src/ptc_agent/agent/middleware/credit_gate.py
@dataclass(frozen=True)
class LeaseVerdict:
    granted: bool
    ceiling_credits: float = 0.0
    ttl_seconds: float = 0.0
    generation: Optional[int] = None
    quota: Optional[dict] = None
```

运行机制：
- 一个主运行对应一个 `CreditLease`，子智能体通过 `spawn_child` 共享同一个租约。
- 后台每 2 秒执行一次 `_tick`，用整轮的累计花费调用 `acquire`。花费单位是 credits，等于 `platform_usd_total() * USD_TO_CREDITS_RATE(1000)` 加基础设施 credits。
- `CreditGateMiddleware.abefore_model` 在每次模型调用前检查 `denial is not None and spent >= ceiling_credits`：主运行发 `interrupt({"action_requests":[{"type":"credit_pause","message":...}]})`，子智能体抛 `CreditStopError`。
- 拒绝文案来自 `LeaseVerdict.quota["message"]`。

### 2. 钩子（每处 3 行左右）

```python
# credit_gate_port.py，build_run_credit_gate 开头：
# DALTON-HOOK(budget-gate): local daily-USD gate in OSS mode
from src.dalton.budget.gate import local_gate_enabled, build_local_run_gate
if local_gate_enabled():
    return build_local_run_gate(user_id, run_id, token_callback, tool_tracker, model_name, is_byok)

# verify_credit_gate_wiring 开头：
if local_gate_enabled():
    from src.dalton.budget.gate import verify_local_wiring
    return await verify_local_wiring()

# usage_limits.py，enforce_credit_limit 开头：
# DALTON-HOOK(budget-admission)
from src.dalton.budget.gate import local_gate_enabled, enforce_local_admission
if local_gate_enabled():
    return await enforce_local_admission(user_id, byok=byok)
```

`local_gate_enabled()` = `settings.HOST_MODE == "oss"` 且 `DALTON_BUDGET_GATE == "1"`（由 `src/dalton/settings.py` 读取，遵守"配置走 accessor"的约定）。

`build_local_run_gate`：
- 构造方式和上游一致：`CreditGateState(run_ref=run_id, kind="run", port=LOCAL_PORT, lease=CreditLease(user_id=..., run_ref=run_id, port=LOCAL_PORT, is_byok=is_byok), tracker=token_callback, tool_tracker=tool_tracker, rate_multiplier=1.0)`。
- `user_id` 为空时返回 `None`（同上游）。
- `LOCAL_PORT` 是无状态对象，所有真相都在 Postgres 里。

### 3. 存储（独立的 Alembic 链，原生 SQL）

**为什么不接在上游链上：** 上游迁移是线性编号（当前 head 是 `073`）。如果我们加 `074`，上游以后也加 `074`，就会出现双 head，入口脚本的 `alembic upgrade head` 直接失败。如果事后把我们的迁移改接到上游后面，已经执行过的库会跳过上游的迁移。所以 Dalton 用独立的 `version_table = "alembic_version_dalton"`：`src/dalton/migrations/env.py` 复制 `migrations/env.py` 的 URL 构造逻辑，并在 `context.configure(..., version_table="alembic_version_dalton")`。表之间**不建外键**指向上游表，只存 id。

**全部 Dalton 迁移只用这一条链**（各篇不得在上游 `migrations/versions/` 里加文件，也不得另起链）。编号登记如下，新增迁移时在这里追加一行：

| revision | 文件 | 归属 | down_revision |
|---|---|---|---|
| d001 | `d001_budget.py` | P0.8 本地预算 | None |
| d002 | `d002_openclaw_sync_runs.py` | P0.7 同步运行记录 | d001 |
| d003 | `d003_knowledge.py` | PK.3 知识版本与事件结论 | d002 |
| d004 | `d004_provenance_locator.py` | P0D 的 P0-D.3b（P3 执行）：给上游 `provenance_records` 加 `dalton_source_url`、`dalton_as_of` 两列（`ADD COLUMN IF NOT EXISTS`，可空） | d003 |
| d005 | `d005_publications.py` | PK.8 发布治理 | d004 |
| d006 | `d006_autonomy.py` | PK.16 预检与自主议程 | d005 |

d004 是唯一一处改上游表结构的迁移：列名带 `dalton_` 前缀，避免和上游以后加的列撞名；它在入口脚本里排在上游 `alembic upgrade head` 之后执行，所以表一定已经存在。

`d001_budget.py`：

```python
revision = "d001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
    CREATE TABLE IF NOT EXISTS dalton_budget_config (
        user_id            VARCHAR(255) PRIMARY KEY,
        daily_cap_usd      NUMERIC(12,6) NOT NULL CHECK (daily_cap_usd >= 0),
        default_task_cap_usd NUMERIC(12,6) NULL CHECK (default_task_cap_usd IS NULL OR default_task_cap_usd > 0),
        timezone           TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS dalton_budget_task_caps (
        user_id     VARCHAR(255) NOT NULL,
        scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('workspace','thread')),
        scope_id    UUID NOT NULL,
        cap_usd     NUMERIC(12,6) NOT NULL CHECK (cap_usd > 0),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (user_id, scope_kind, scope_id)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS dalton_budget_reservations (
        reservation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id          VARCHAR(255) NOT NULL,
        budget_day       DATE NOT NULL,            -- 按 config.timezone，取运行首次 acquire 的日期
        run_ref          UUID NOT NULL UNIQUE,     -- = conversation_responses.conversation_response_id（主运行 run_id）
        thread_id        UUID NULL,
        workspace_id     UUID NULL,
        reserved_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),   -- 累计授予的上限
        observed_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (observed_usd >= 0),   -- acquire 时上报的累计花费，取最大
        task_cap_usd     NUMERIC(12,6) NULL,       -- 生效的 task cap 快照
        state            TEXT NOT NULL DEFAULT 'open'
                         CHECK (state IN ('open','released','settled','expired')),
        generation       INTEGER NOT NULL DEFAULT 1,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        renewed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at       TIMESTAMPTZ NOT NULL,
        released_at      TIMESTAMPTZ NULL,
        settled_at       TIMESTAMPTZ NULL,
        settled_usd      NUMERIC(12,6) NULL,       -- 结算后的最终入账额
        settle_source    TEXT NULL CHECK (settle_source IN ('usage','conservative'))
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dalton_resv_user_day ON dalton_budget_reservations (user_id, budget_day)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dalton_resv_unsettled ON dalton_budget_reservations (user_id) WHERE state <> 'settled'")

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_budget_reservations")
    op.execute("DROP TABLE IF EXISTS dalton_budget_task_caps")
    op.execute("DROP TABLE IF EXISTS dalton_budget_config")
```

**"结算"的真相是上游的 `conversation_usages`：**
- 主运行在 finalize 时 `persist_usage(response_id=handle.run_id)`（`src/server/services/runs/coordinator.py` 约第 705 行）；
- 子智能体 `persist_usage(response_id=..., msg_type="task", settle_task_run_id=...)`（`src/server/services/runs/subagent_usage.py` 约第 115 行），其 `response_id` 是否等于父运行的 run_id【待验证】。
- USD 计算：`total_credits / USD_TO_CREDITS_RATE`。`total_credits` 只计 `billing_type=="platform"` 的成本（BYOK、OAuth、订阅都是 0），再加基础设施 credits。

### 4. 计算口径（`service.py`，全部在一个事务里，带按用户/日的 advisory lock）

```
usage_for_run(r) = SUM(cu.total_credits)/rate  WHERE cu.conversation_response_id = r.run_ref   -- 主运行 + 子任务【待验证 task 行的 response_id】

charge(r) =
  state='settled'                 → r.settled_usd                              -- = usage_for_run（结算时写入）
  state IN ('open','released')    → GREATEST(r.reserved_usd, r.observed_usd, usage_for_run(r))   -- 未结算按全额预留占用
  state='expired'                 → r.settled_usd                              -- 保守入账 = 过期时的 GREATEST(...)，不释放

spent_today(user, day) =
    Σ_{r: r.budget_day = day} charge(r)
  + Σ  cu.total_credits/rate  over 今日 conversation_usages，且 conversation_response_id 不属于任何预留
    -- 覆盖没有经过 gate 的服务器侧调用（LLMService.complete 的标题、insights 等）【待验证：这些调用是否写 conversation_usages】
```

`reconcile(user_id, day)` 是惰性对账，在每次 acquire、准入检查、预算查询时执行，不需要常驻进程：

1. `state IN ('open','released')`，且 `conversation_responses.usage_settled_at IS NOT NULL`，且存在 usage 行，且该运行下没有 `status='in_progress'` 的 `subagent_runs`（`parent_run_id = run_ref`）→ `state='settled'`，`settled_usd = usage_for_run`，`settle_source='usage'`。
2. `state='open'` 且 `expires_at < now()`（进程崩溃、租约没有续期）→ `state='expired'`，`settled_usd = GREATEST(reserved_usd, observed_usd, usage_for_run)`，`settle_source='conservative'`。**不释放**。
3. `state='expired'` 的预留，之后如果恢复扫描补写了 usage 行，`settled_usd` 只能上调到 `GREATEST(settled_usd, usage_for_run)`，绝不下调。

SQL 用带条件的 `UPDATE … WHERE state = <旧值>`，符合 multi-worker 规范。

### 5. LocalBudgetPort 语义

```python
class LocalBudgetPort:
    async def acquire(self, user_id, run_ref, spent_credits, rate_multiplier=1.0, byok=False) -> Optional[LeaseVerdict]
    async def release(self, user_id, run_ref, generation=None) -> None
    async def capability(self) -> Optional[dict]     # {"enabled": True, "lease_ttl_seconds": ..., "mode": "dalton-local"}
    async def heartbeat(self, kind, run_ref, credits) -> Optional[bool]
```

**acquire**：一个事务，先拿 `pg_advisory_xact_lock(hashtext('dalton_budget:' || user_id || ':' || day))`。

1. `reconcile(user_id, day)`。
2. 读 config；没有记录时用 `dalton.yaml` 的 `budget.default_daily_cap_usd`。**没有任何配置时按 0 处理，即拒绝**（fail-closed），文案写"未设置日预算"。日额度的具体数额由 owner 定（附录 C），在定下之前 `dalton.yaml` 里写占位 `default_daily_cap_usd: null`，因此 P0 验收前必须在设置页填一个值。时区固定默认 `Asia/Shanghai`。
3. UPSERT 本运行的预留行：首次 acquire 时 `budget_day=今天`，并从 `conversation_responses` / `conversation_threads` 回填 thread_id 和 workspace_id【待验证列名】；然后 `observed_usd = GREATEST(observed_usd, spent_credits/rate)`。
4. 计算：

```
this_floor = GREATEST(observed_usd, usage_for_run)
others     = spent_today − charge(this)
available  = daily_cap − others − this_floor
task_left  = (task_cap − this_floor)    if task_cap else +∞
              -- task_cap 取值顺序：thread 级 > workspace 级 > default_task_cap_usd
grant      = MIN(chunk_usd, available, task_left)
              -- chunk_usd 来自 dalton.yaml 的 budget.reserve_chunk_usd，默认 1.00
```

5. 根据 `grant` 返回裁决：
   - `grant > 0`：`reserved_usd = GREATEST(reserved_usd, this_floor + grant)`，`expires_at = now() + lease_ttl`（默认 300 秒），`renewed_at = now()`，返回：

   ```python
   LeaseVerdict(granted=True, ceiling_credits=reserved_usd*rate, ttl_seconds=lease_ttl, generation=generation)
   ```

   - `grant <= 0`：返回：

   ```python
   LeaseVerdict(granted=False, ceiling_credits=this_floor*rate, ttl_seconds=lease_ttl, generation=generation,
                quota={"type": "dalton_daily_budget" | "dalton_task_cap", "message": <中文文案，见下>,
                       "spent_usd": ..., "cap_usd": ..., "open_reservations_usd": ...})
   ```

6. 出现任何异常时返回 `None`，上游语义是保留上一个裁决。但为了 fail-closed，**准入检查（第 7 点）对 DB 异常返回 503**，和上游 `enforce_credit_limit` 的 fail-closed 一致。

**release**：`UPDATE … SET state='released', released_at=now() WHERE run_ref=%s AND state='open'`；结算留给 reconcile 处理。

**heartbeat**：直接委托 `src/server/database/runs/credit_ledger.heartbeat(kind, run_ref, credits)`，写上游的 `in_flight_credits`，让上游的 `settle_abandoned` 和恢复扫描照常工作。预留的 `observed_usd` 由 acquire 维护，不在这里写。

**超额的上界（如实写进文档）：**
- 上游设计是"慢一个裁决"：中间件只在被拒绝**之后**才会停下。最坏的超额约等于 `chunk_usd` 加上单次模型调用成本乘以同时在跑的成员数（主运行加子智能体）。
- 用 `chunk_usd=1.00`、`admission_floor_usd=0.25` 时，正常情况下超额在 $1 量级。
- 如果 owner 要求严格的"单次调用前预留"，要另写一个 Dalton 中间件，在每次调用前按"输入 token 估算 + max_tokens"预扣。这要改智能体中间件栈。**已决定 P0 不做**。
- 服务端单次调用（`dalton_complete` / `assist_json`）在发送现金模型请求前执行准入检查：未传 `thread_id` / `workspace_id` 时仅检查日美元上限（含 `admission_floor_usd`）；传入 `thread_id` 或 `workspace_id` 时还会汇总该会话/工作区累计现金花费并与生效的 `task_cap` 比较（`task_spent >= task_cap` 时返回 `task_cap_exhausted`，P0H §5 B3）。

### 6. 拒绝文案（中文，进入 `CreditPauseCard`）

- 日额度用尽：`今日预算 $20.00 已用完（已结算 $18.70，未结算预留 $1.30）。可在 设置 → 模型 → Dalton 预算 调高额度，或等北京时间 00:00 重置。`
- task cap 用尽：`本任务预算 $5.00 已用完（已用 $5.02）。确认继续请点"继续"，将按新任务重新计额。`
  - 恢复运行是一次新的 POST，会生成新的 run，task cap 重新计额；日额度仍然生效。

### 7. 准入检查 `enforce_local_admission(user_id, byok)`

所有入口都会调用 `enforce_credit_limit`：
- `app/threads/messaging.py:545`
- `app/threads/create.py:114`
- `app/insights.py:66`
- `services/automation_executor.py:790,858`
- 恢复（resume）也会再走一遍准入。

逻辑：
- 执行 `reconcile`，然后计算 `available = cap − spent_today`。
- `available < admission_floor_usd`（默认 $0.25）时，抛出：

```python
HTTPException(429, detail={"message", "type": "dalton_daily_budget", "used_credits", "credit_limit",
              "remaining_credits", "outstanding_debt": 0, "retry_after": <到本地次日 0 点的秒数>})
```

  并带上 headers `Retry-After`、`X-RateLimit-Limit`、`X-RateLimit-Remaining`。字段和上游一致，前端的 429 读取器可以直接用。
- `byok=True`（用户自带 key 或 OAuth）时放行：这些调用不进现金额度。
- DB 异常时返回 503 `service_unavailable`。

### 8. 启动校验 `verify_local_wiring()`

日志检查：
- `alembic_version_dalton` 存在且是 head；
- 三张表可读；
- 至少有一条 config，或 `dalton.yaml` 里有默认额度；
- `USD_TO_CREDITS_RATE` 是 1000。

只记日志，不抛异常，和上游一致。

### 9. 接口与设置页

`src/dalton/app/router.py`：
- `GET /api/v1/dalton/budget` → `{daily_cap_usd, default_task_cap_usd, timezone, task_caps:[{scope_kind, scope_id, scope_name, cap_usd}]}`
- `PUT /api/v1/dalton/budget` → 同样的结构，做校验：金额 ≥ 0，timezone 合法（`zoneinfo`）。
- `PUT/DELETE /api/v1/dalton/budget/task-caps/{scope_kind}/{scope_id}`
- `GET /api/v1/dalton/budget/today` → 先 `reconcile`，再返回：

```json
{"day", "cap_usd", "settled_usd", "open_reserved_usd", "expired_conservative_usd",
 "unreserved_usage_usd", "spent_usd", "remaining_usd",
 "subscription_notional_usd",
 "runs": [{"run_ref", "thread_id", "state", "reserved_usd", "observed_usd", "charge_usd", "created_at"}]}
```

  - `runs` 取最近 20 条。
  - `subscription_notional_usd` = 今日 `conversation_usages.token_usage` 里订阅模型的 token 数乘以 overlay 的 `notional_pricing`，读取时计算【待验证 `token_usage` 里按模型拆分的 JSON 结构，见 `src/utils/tracking/core.py` 的 `by_model`】。

`DaltonBudgetCard.tsx`（放在 `DaltonSection` 里）：
- 进度条：已用（已结算 + 未结算预留 + 保守入账）/ 日额度。
- 数字行：剩余、未结算预留、订阅名义用量（灰色，注明"不计入现金额度"）。
- 输入框：日额度（USD）、默认 task cap（可空）、时区。
- 按 workspace 覆盖 task cap 的小表格：workspace 下拉来自现有 workspaces 接口。P0 只做 workspace 级，thread 级只提供 API。
- 最近运行的预留明细（可折叠）。
- 30 秒轮询 `budget/today`，只在卡片可见时轮询。

`creditPauseCard.ts` 钩子：

```ts
// DALTON-HOOK(pause-links)
if (!isPlatformMode) links = [{ label: t('dalton.budget.adjust'), href: '/settings?tab=model#dalton-budget' }]
```

`isPlatformMode` 来自 `web/src/config/hostMode.ts`。链接字段要符合 `ErrorLinkSpec` 的结构【待验证 `web/src/utils/rateLimitError.ts` 里 `ErrorLinkSpec` 的字段名】。另外检查发送消息被 429 拒绝时，由 `buildRateLimitError` 生成的链接在 OSS 下是否指向不存在的 `/account/*`，做同样的替换。

### 10. 供其他模块调用的服务函数（PK、PS 只用这两个，字段名全文统一）

`src/dalton/budget/service.py` 对外只暴露两个只读函数，名称和字段与第 9 节的接口一致，PK 的预检、研究总监和 PS 的自动任务都用它们，**不另起名字**：

```python
async def get_budget_today(user_id: str) -> dict
    # 先 reconcile，再返回与 GET /api/v1/dalton/budget/today 完全相同的结构：
    # {"day", "cap_usd", "settled_usd", "open_reserved_usd", "expired_conservative_usd",
    #  "unreserved_usage_usd", "spent_usd", "remaining_usd", "subscription_notional_usd", "runs": [...]}
async def get_run_charge_usd(run_ref: str) -> Decimal | None
    # 一次运行当前的 charge（第 4 节的 charge(r)，即 runs[].charge_usd）；没有预留行时返回 None
```

调用方读 `get_budget_today(user_id)["remaining_usd"]` 判断"今天还剩多少"，读 `get_run_charge_usd(run_ref)` 判断"这一轮花了多少"。旧草稿里的 `remaining_today_usd`、`run_cost_usd` 一律改成这两个名字。

### 验证方式（真实调用优先，单元测试最后补）

1. `docker compose exec backend uv run alembic -c src/dalton/alembic.ini upgrade head`，然后执行 `\dt dalton_*`，三张表都在。
2. 设置页把日额度设为 $0.30。用现金模型（如 `ocb-xai-grok-4.7`）跑一个长任务，例如"写一份 META 的 3000 字业务概览，并派两个子智能体分别查广告和 Reality Labs"。期望：
   - 运行中，`budget/today` 的 `open_reserved_usd` 是 $0.30 以内的整块预留；
   - 花费达到上限后，出现 `CreditPauseCard`，文案是日额度用尽，子智能体卡片显示 `credit_stop`；
   - 总花费 ≤ $0.30 + 本节第 5 点的超额上界；
   - 运行结束后，预留变为 `settled`，`settled_usd` 等于 `conversation_usages` 合计。
3. 在额度用尽状态下再发一条消息，返回 429，界面显示中文文案和"调整日预算"链接。把额度调到 $2，点卡片上的"继续"，可以继续。
4. 崩溃演练：任务进行中执行 `docker compose kill backend && docker compose up -d backend`。等租约 TTL（300 秒）过后查询 `budget/today`：该预留为 `expired`，`settled_usd = GREATEST(reserved, observed)`，`settle_source='conservative'`，剩余额度**没有恢复**。
5. 订阅模型：用 `ocb-openai-gpt-6.1-sol`（broker，订阅）和 `claude-opus-5-5-oauth`（原生 OAuth，`billing_type=oauth`，上游本来就不计入 `platform_cost`）各对话一次。`spent_usd` 不变；broker 那次的 `subscription_notional_usd` 增加。OAuth 那次的名义用量按 `claude-opus-5-5` 的上游价格估算【待验证 `token_usage` 里能否区分 oauth】。
6. 并发（`--workers 2`）：额度剩 $0.50 时同时发两个任务，两个预留合计 ≤ $0.50（advisory lock 生效）。重复 3 次。
7. task cap：给当前 workspace 设 $0.20 的 task cap，任务在 $0.20 附近暂停，文案是 task cap 用尽；点"继续"可以续跑。
8. 以上都稳定后，补单元测试，用来固定契约：
   - `LocalBudgetPort` 满足 `FakePort` 的契约；
   - charge 和 reconcile 的状态转换；
   - 准入检查的 429 结构。
   - 测试放在 `tests/unit/dalton/`【新建】。
   - 上游测试 `tests/unit/server/dependencies/test_usage_limits.py::test_oss_mode_never_blocks` 会在 `DALTON_BUDGET_GATE` 未设置时继续通过。测试环境不要设置这个变量。

### 验收标准

- 第 2–7 条全部符合预期。
- 预算只有"日额度 + 可选 task cap"两种，代码里没有任何次数限制。
- 拒绝时显示中文 pause 卡片，并有可点的"调整日预算"链接。
- 崩溃后保守入账，不释放。
- 上游的 3 处钩子各约 3 行，带 `DALTON-HOOK` 标记。

### 风险与注意

- **cache write 的计量：** broker v2 在 `prompt_tokens_details.cache_write_tokens` 里报告 cache write，但 LangChain 和 LangAlpha 的用量解析很可能不读这个非标准字段，于是 LangAlpha 侧成本会少算 cache write【待验证】。当前的现金模型 cacheWrite 价格都是 0，所以没有影响。同步脚本要对"现金模型且 cacheWrite>0"发出 warning。
- **成本对账：** broker 的 `usage.cost_usd` 是 OpenClaw 自己算的成本。P0 以本地价格卡为准（偏保守：两者取大的那个价格卡），用 B15 定期对账，偏差超过 5% 时查原因。以后如果要让预算直接用 broker 的成本，需要在 `per_call_token_tracker` 里读这个扩展字段。这要改上游，P0 不做。
- "未预留的 usage"这一项依赖 `LLMService.complete` 是否写 usage 行。如果不写，这类调用就不受额度约束，需要另想办法。P0 先查清楚并记录。
- 和旧 Dalton 的日额度：**已决定独立**（旧 Dalton 独立部署已停）。新项目只认自己的 `dalton_budget_*` 表。

**预估工时：** 2–2.5 天（后端 1.5 天，前端 0.5 天，演练 0.5 天）。

---

## P0.9 原生界面端到端可见性验证（owner 看的基线）

**目标：** 用 P0.6 的默认模型（主分析师用原生 Claude OAuth，子智能体用 broker v2 上的 grok-4.7）确认 LangAlpha 原生界面能实时展示研究过程。这是 owner 判断"新项目在做什么"的基线，后续的"网关页面"和覆盖视图都在它之上搭建。

**前置依赖：**
- P0.4、P0.6；
- 数据篇（P0D）的 researchdata 桥（含只读的基金 wiki 路由）至少接通。没接通时，先用 LangAlpha 自带的 SEC 和网页搜索验证。

**改动/新增文件：** 无代码改动。产出是走查截图，放在 `docs/dalton/dev-guide/assets/p0-walkthrough/`【新建】，以及 PR 里的走查记录。

### 具体做法（逐项截图）

1. **流式文本与步骤：** 在 workspace "META" 里问"概括 META 最近一个季度的业绩要点，并列出数据来源"。
   - 期望：逐字流式输出，过程中显示活动块（`web/src/pages/ChatAgent/components/ActivityBlock.tsx`）。
   - 注意：原生 claude-oauth 下应该能看到思考块。broker 模型的思考内容经 `delta.reasoning_content` 发出，但 LangChain `ChatOpenAI` 可能丢弃它【待验证】，看不到时记为已知限制。
2. **工具调用卡片：** 同一轮里应出现 `execute_code`、web/SEC 工具的调用卡片，点开能看到参数和结果（`ToolCallDetailView.tsx`）。
3. **子智能体卡片：** 问"并行调查：A 查广告业务驱动，B 查 Reality Labs 亏损趋势，汇总给我"。
   - 期望：出现两个子智能体任务卡片（`TaskCardShell.tsx`、`SubagentTaskMessageContent.tsx`），各自独立流式，完成后回报主线程。
4. **来源面板：** 回答完成后打开 SourcesPanel（`SourcesPanel.tsx`），列出本轮访问的来源（来源记录中间件）。P0 用不改核心的降级显示（P0-D.3）：researchdata 调用显示为一条 `mcp:researchdata` 记录，逐篇文档的来源身份、as-of 和链接在 P3 才有（P0-D.3b）。
5. **文件面板：** 智能体写入的工作区文件出现在文件面板里，可以打开。
6. **自动任务：** 让智能体"每天北京时间 8:30 汇总 META 前一日新闻，写入 `research/`"（自动任务文件名用 `dalton-` 前缀，分析师可以直接建，不用再问，见 PK.15），然后在 Automations 页（`web/src/pages/Automations/Automations.tsx`）看到该任务，点"立即运行"，执行记录出现在列表里。
7. **重连：** 流式进行中刷新页面，能接着看到后续输出（Redis 流缓冲加续传光标）。
8. **成本：** 每轮结束后，`budget/today` 的 `spent_usd` 增加，和 `conversation_usages` 一致。

### 验收标准

8 项都有截图，并且行为符合预期。不符合的项要写清现象和归因：是 broker v2、overlay 配置、原生 OAuth，还是上游本身的问题。

### 风险与注意

- 子智能体卡片里，如果出现"有回答但没有工具调用"的情况，先查是不是误配了 text-only 模型（broker 应该已经返回 400）。
- CLI gateway 类模型只做纯文本角色，不会驱动主线程；如果 compaction 或后台摘要配了它们，首 token 慢会让 compaction 等得更久。出现时看 broker 日志的首 token 时间，必要时把 compaction 换回本轮模型。

**预估工时：** 0.4 天。

---

## P0.10 P0 验收清单（lead 照着跑）

标 [回归] 的项在每次合并上游，或 OpenClaw 升级后都要重跑。SQL 用 `docker compose exec postgres psql -U postgres -d postgres -c "<SQL>"`。

| # | 动作 | 期望观察 |
|---|---|---|
| A1 [回归] | `docker compose ps`；`curl -sf localhost:8000/health`；打开 `http://localhost:5173` | 4 个服务 healthy；免登录进入 |
| A2 | `git diff --stat upstream/main -- src/llms/manifest agent_config.yaml config.yaml` | 零改动 |
| A3 [回归] | `scripts/dalton/check_hooks.sh` | 钩子清单与附录 A 一致 |
| A4 | 看 P0.2 spike 报告 | GO（或部分 GO），lead 签字 |
| A5 | `ls ~/.openclaw/managed-plugins/ \| grep dalton-langalpha-broker`；`curl -s -H "Authorization: Bearer $DALTON_BROKER_TOKEN" 127.0.0.1:18789/dalton-broker/v1/health` | 安装目录按 sha 命名；`status:"ok"` |
| A6 [回归] | `uv run python scripts/dalton/openclaw_smoke.py --target broker` | B 系列阻断项全过；报告里没有 token |
| A7 | `--target native`、`--target gateway` | N 系列通过；G 报告存在（作为对照证据，不要求通过） |
| A8 [回归] | `uv run python scripts/dalton/sync_openclaw_models.py --check` | 退出码 0 |
| A9 [回归] | `curl -s localhost:8000/api/v1/models \| jq '.models["openclaw-broker"].models \| length, .system_defaults'` | 数量与 overlay 的 `meta.counts.visible` 一致；默认模型为 `claude-opus-5-5-oauth`（或切换后的主分析师模型，见 P0.6），`fallback_models=[]` |
| A10 [回归] | 在容器内跑 `openclaw_smoke.py --checks B14 --langalpha-key ocb-xai-grok-4.7` | pass：线上 model 字段、invocation id、计费戳都正确 |
| A11 | 幂等现场：用 broker 模型跑一轮，期间 `docker compose restart backend` | 前端自动重连或重试；broker 日志里同一个 invocation id 只 dispatch 了一次（回放或 409），没有重复计费 |
| A12 | 设置 → 模型 → OpenClaw 卡片 | 显示"broker 正常"、上次同步时间、数量、冒烟摘要；有角色检查行 |
| A13 | 在 overrides 里隐藏 `zai/glm-5.3`，点"重新同步"并应用；然后恢复 | 不重启后端即生效，恢复后重新出现 |
| A14 | 把默认模型设为 GLM-5.3 后再做 A13；两个标签同时点"应用"；`--workers 2` 下重复 5 次 | 依次返回 409 `in_use`、409 `sync_in_progress`，多 worker 下每次都在 2–3 秒内生效；`dalton_openclaw_sync_runs` 里的记录对得上 |
| A15 | 在 META workspace 提问（P0.9 第 1–4 条），默认模型配置 | 流式、工具卡片、子智能体卡片（grok-4.7 via broker）、SourcesPanel 都正常；能看到调用内部研究库和（只读）wiki 的工具卡片（依赖 P0D） |
| A16 | `SELECT conversation_response_id, msg_type, total_credits/1000.0 AS usd, token_usage->>'platform_cost' FROM conversation_usages ORDER BY created_at DESC LIMIT 5;` | 现金模型的轮次 `usd>0`；OAuth 和订阅模型为 0 |
| A17 | `GET /api/v1/dalton/budget/today` 或看预算卡 | 已用金额等于 A16 合计加上未结算预留；剩余额度相应减少；有订阅名义用量 |
| A18 | 日额度设为 $0.30，跑 P0.8 验证第 2 条的长任务 | 中文 pause 卡片出现；子智能体显示 credit_stop；超额在上界以内 |
| A19 | 额度用尽时发消息；调高额度后点"继续" | 返回 429，有"调整日预算"链接；调高后能继续 |
| A20 | 崩溃演练（P0.8 验证第 4 条） | 预留显示 `expired/conservative`，额度没有恢复 |
| A21 | 双 worker 并发预算（P0.8 验证第 6 条） | 合计预留 ≤ 剩余额度 |
| A22 | 自动任务（P0.9 第 6 条） | 有执行记录；额度用尽时在准入阶段被拒，记录里有原因 |
| A23 [回归] | `make test`；`cd web && pnpm typecheck && pnpm lint`；`(cd integrations/openclaw/dalton-langalpha-broker && ~/.openclaw/tools/node/bin/node --test test/*.test.mjs)` | 全部通过 |
| A24 | `colima list`；`du -sh $COLIMA_HOME`；两个 workspace 各发一句话后 `docker ps --filter name=langalpha-sandbox`；闲置 30 分钟后再看 | 4 CPU / 5 GiB，VM 在 `/Volumes/EveSSD`；只有 1 个沙箱容器，内存上限 3 GiB；闲置后被停，再发消息自动拉起 |
| A25 | 改 `workflows/<name>/workflow.js` 的一处文字，`docker compose restart backend`，再触发该 workflow | 新内容生效，没有重建镜像 |
| A26 | 切换主分析师模型（P0.6 验证第 5 条） | 切换生效；审稿人选择随 `family_of()` 变化 |

---

## 附录 A：钩子清单（P0 结束时的全集）

**LangAlpha 上游文件：**

| ID | 文件 | 位置 | 行数 |
|---|---|---|---|
| llm-overlay | `src/llms/llm.py` | `ModelConfig.__init__`、`LLM.get_model_config` | 约 6 |
| llm-broker-client | `src/llms/llm.py` | `_build_openai_params` | 约 4 |
| catalog-cache | `src/server/app/api_keys.py` | `list_models()` 开头 | 约 4 |
| router | `src/server/app/setup.py` | 约第 1324 行 | 2 |
| budget-gate | `src/server/services/credit_gate_port.py` | `build_run_credit_gate`、`verify_credit_gate_wiring` | 约 6 |
| budget-gate | `src/server/app/setup.py` | `lifespan` 启动时 `verify_credit_gate_wiring` 仅在 `DaltonStartupConfigError` 时阻断启动 | 约 5 |
| budget-admission | `src/server/dependencies/usage_limits.py` | `enforce_credit_limit` | 3 |
| settings-section | `web/src/pages/Settings/panels/ModelTab.tsx` | Providers & accounts 小节之后 | 2 |
| query-keys | `web/src/lib/queryKeys.ts` | `dalton` 键组 | 约 8 |
| i18n | `web/src/locales/en-US.json`、`zh-CN.json` | `dalton.*` | 若干键 |
| pause-links | `web/src/pages/ChatAgent/session/interrupts/creditPauseCard.ts` | `buildCreditPauseState` | 约 3 |
| provider-kill-switch | `src/llms/llm.py` | `LLM.get_llm` 开头检查 `ensure_provider_enabled` | 3 |
| llm-sdk-dispatch | `src/llms/llm.py` | `LLM.get_llm` SDK 分派调用 `dispatch_native_sdk` | 约 6 |
| oauth-dispatch | `src/server/services/llm/clients.py` | `_resolve_oauth_token` 调用 `resolve_native_oauth_token` | 约 6 |
| native-oauth-ui | `web/src/hooks/useConfiguredProviders.ts`、`web/src/pages/Settings/panels/ConnectedAccounts.tsx`、`web/src/pages/Setup/steps/connectStep/OAuthConnect.tsx`、`web/src/pages/Setup/steps/MethodStep.tsx`、`web/src/pages/Setup/steps/connectStep/shared.tsx`、`web/src/lib/brandArt.ts` | Antigravity / Muse OAuth UI 委托至 Dalton 子模块 | 各 1–4 |
| workspace-delete-guard | `src/server/app/workspaces.py` | `delete_workspace` 检查正式发布报告（G3.3 / L13） | 约 5 |
| no-strike | `src/server/database/automation_executions.py` | `_count_strike` 豁免 `dalton-*` 自动停用并按时间退避（G4.4 / P0F.3） | 约 8 |

**数据篇（P0D）引入的钩子**（也在 P0 阶段落地，`check_hooks.sh` 一并核对）：

| ID | 文件 | 位置 | 行数 |
|---|---|---|---|
| mcp-call-timeout | `src/ptc_agent/config/core.py` | `MCPServerConfig` 加可选字段 `call_timeout_s` | 2 |
| mcp-call-timeout | `src/ptc_agent/core/tool_generator.py` | `_client_entry` 的 HTTP 分支：有值时写进 entry | 2 |
| mcp-call-timeout | `src/ptc_agent/core/sandbox/mcp_client_runtime.py` | `_ServerCfg`、`_normalize`、`tools/call` 的预算取值 | 约 6 |
| mcp-call-timeout | `src/ptc_agent/core/sandbox/tool_overlay.py` | `executable_keys` 加 `call_timeout_s` | 1 |
| skills-registry | `src/ptc_agent/agent/middleware/skills/registry.py` | 文件末尾合并 `DALTON_SKILL_DEFINITIONS` | 2–3 |
| provenance-declared | `src/ptc_agent/agent/middleware/provenance/middleware.py` | `_extract_execute_code`、`_extract_direct_mcp` 产出 `mcp_tool` 后展开 `_sources` | 约 4 |
| provenance-type | `web/src/types/sse.ts`、`web/src/pages/ChatAgent/components/SourcesPanel.tsx` | `research_doc` 类型与渲染分派 | 约 3 |

细节见 P0D 的 P0-D.2（超时）、P0-D.3（来源显示降级版）和 P1.1（skill 注册）。PK 篇的钩子列在 PK §5；`check_hooks.sh` 比对的是"本附录 + PK §5"的并集。P3 的来源定位改动（P0-D.3b）落地时再并入本表。

**OpenClaw 侧（已获授权，lead/运维操作，不在本仓库）：** 在 `~/.openclaw/openclaw.json` 的 `plugins.load.paths`、`plugins.allow`、`plugins.entries["dalton-langalpha-broker"]` 里各加一项（P0.3 第 7、8 节）。备份文件命名为 `openclaw.json.bak-dalton-broker-v2-<日期>`。

## 附录 B：【待验证】汇总

1. 插件里拿运行时完整配置的入口；`prepareSimpleCompletionModelForAgent({modelRef})` 能否取到指定模型，是否受 `llm.allowedModels` 约束（P0.2）。
2. `streamSimple(prepared.model, …)` 能否直接用，还是需要 `@openclaw/ai/transports` 的准备步骤（非公开面）（P0.2）。
3. 网关插件路由（`auth:"plugin"`）对长时间 SSE 和大请求体的支持（P0.3 第 2 节）；colima 下 `host.docker.internal` 能否访问只绑 loopback 的端口（P0.1，实测按 P0-D.0）。
4. SDK 里 `tool_choice` 的对应字段；`transformMessages` 如何处理跨模型历史；Gemini 3 的 `thoughtSignature` 回放（P0.3）。
5. openai SDK 重试时 body 是否逐字节相同；`current_credit_gate` 在模型调用的上下文里是否可见（P0.4 第 6 节）。
6. LangChain `ChatOpenAI` 对 `delta.reasoning_content` 和 `cache_write_tokens` 的处理（P0.8、P0.9）。
7. manifest 里的 `profile.tool_calling=false` 能否被 LangAlpha 识别（P0.4）。
8. 内置子智能体能否只在 `subagents.definitions` 里覆盖 `model`（P0.6）。
9. 子智能体 usage 行的 `conversation_response_id` 是否等于父运行 id；`LLMService.complete` 是否写 usage 行；`token_usage` 按模型拆分的结构（P0.8）。
10. `conversation_responses` 到 thread、workspace 的列名；`ErrorLinkSpec` 的字段（P0.8）。
11. 没有登录 Claude OAuth 时，`claude-opus-5-5-oauth` 作为 YAML 默认模型的表现（P0.6）。
12. 网关的 launchd label 和 `launchctl kickstart` 的域（P0.3 第 8 节）。
13. 读取所有用户 model 偏好的入口（P0.7 的引用保护）。
14. muse 系列模型的家族归属。
15. colima 相关：VM 是否提供 `/dev/fuse`；compose 的 docker socket 挂载路径；`--vm-type vz`/`virtiofs` 在本机是否可用（P0.1）。
16. 新建工作区默认是否复用用户已有的 computer，脚本如何指定 computer；docker provider 下空闲回收是否真的停掉容器（P0.1 第 5 步）。
17. backend 在 2 GiB 上限下的实际占用（P0.1 第 5 步）。
18. 现金 API key 写在 `.env` 时，OSS 模式下是否按 `billing_type=platform` 计费、计入日额度（P0.6）。

## 附录 C：已决事项与剩余 owner 问题

**已决事项**（v0.2 的 Q1–Q10 已全部定下，实施时直接照做）：

| 事项 | 决定 |
|---|---|
| 主分析师模型 | 可切换，不写死。默认原生 `claude-opus-5-5-oauth`；替补 `ocb-xai-grok-4.7` 或现金 API key 的原生模型（P0.6）。claude-oauth 有订阅条款风险，出问题就切 |
| CLI gateway 类模型 | antigravity、muse、claude-cli 只做纯文本角色：compaction、后台摘要、审稿 |
| spike 只拿到"部分 GO" | 接受：broker 只服务子智能体和后台调用，主分析师走原生 |
| broker v2 的 `allowedModels` | P0.3 第 7 节的 16 个；其他 text-only 模型先不开 |
| 修改 `openclaw.json`、重启网关 | 已授权，由 lead/运维执行，改前备份 |
| broker 鉴权 | Bearer token（只能调模型）；HMAC 以后再说 |
| 网关 agent `langalpha` | 只作对照组保留，不进生产配置 |
| 严格的"每次调用前预留" | P0 不做 |
| 日额度 | 与旧 Dalton 独立；时区 `Asia/Shanghai`；只有日额度和可选 task cap 两种限制，没有次数限制；供应商配额只在 80% 告警 |
| v1 broker | 不动；v2 用新的插件 id 和新的 journal，两者互不影响 |
| 运行时与沙箱 | colima（4 CPU、5 GiB，VM 和数据在 `/Volumes/EveSSD`），本地 Docker 沙箱，一台 computer 共用，沙箱 3 GiB，空闲 30 分钟自动停；不用 Daytona |
| 迁移 | 所有 Dalton 表走同一条独立链 `alembic_version_dalton`，编号 d001–d006（P0.8 第 3 节） |
| 模型键 | `ocb-<provider>-<model>`，provider `openclaw-broker`；审稿家族只看 overlay 的 `dalton.family` |

**剩余 owner 问题**（全套指南只剩这几项；与本篇直接相关的是第 1 项）：

| # | 问题 | 默认 / 建议 |
|---|---|---|
| 1 | 日额度的具体数额（USD/天） | 未定；P0 验收前必须在设置页填一个值，未填时 fail-closed |
| 2 | STRL 的正式定义 | PS 暂用 M/D/E/C/F/S 的替代定义 |
| 3 | P2 的样本公司 | 建议 DASH，备选 AFRM |
| 4 | 仓位建议的百分比档位和 hurdle rate | hurdle rate 默认 15% |
| 5 | 每周复盘放在哪一天 | — |
| 6 | 净现金口径确认 | 采用 SKILL.md 里的版本 |

## 工时汇总

| 步骤 | 工时 |
|---|---|
| P0.1 本地环境（colima） | 0.75 天 |
| P0.2 broker spike | 1.5 天 |
| P0.3 broker v2 完整实现 | 7–9 天 |
| P0.4 LangAlpha 接入 | 1.35 天 |
| P0.5 冒烟测试 | 0.75 天 |
| P0.6 角色、切换与回退 | 0.6 天 |
| P0.7 设置页同步与健康 | 1 天 |
| P0.8 本地预算 | 2–2.5 天 |
| P0.9 界面走查 | 0.4 天 |
| P0.10 验收 | 0.4 天 |
| **合计** | **约 16–18.5 人日** |

两人并行（一人负责 broker，用 Node；一人负责 LangAlpha 侧，用 Python 和 TS）时，日历时间约 2–2.5 周。
