# 开发指南 P0-D / P1：数据接通与入职

版本 0.3，2026-10-18。基座：LangAlpha main `8e0dce94`，分支 `dalton/main`。上位文件：`docs/dalton/blueprint.md`（已批准）。

本文的读者是要按步骤实施的工程师，验收人是 lead。全文按"步骤编号 → 目标 / 前置依赖 / 改动文件 / 具体做法 / 验证方式 / 验收标准 / 风险 / 工时"的结构写。

**修订记录 v0.3（2026-10-18，按 owner 与 lead 的决定统一修订）：**
- 合规口径定稿：专家访谈、管理层交流和 Guidepoint 检索结果都可以用，引用时标专家身份（不写姓名）。删掉合规相关的开放问题（P1.1、附录 C）。
- 基金 wiki 对本项目**只读**：删掉全部回写步骤（原 P0-D.4 的 P3 回写接口、写令牌）。研究发现只放在工作区知识层和 `published/`。
- SPCX 留在 Core；美团规范代码 `3690.HK`；价格默认 yfinance，FMP 以后可选（P1.2、P0-D.6）。
- 超时重做：去掉 45 秒裁剪。桥按路由设超时（默认 240 秒，长文读取最高 600 秒，前提是调用方预算够）；LangAlpha 侧把 MCP HTTP 单次调用预算（`_HTTP_CALL_BUDGET = 65.0`）改成按 server 可配置，researchdata 默认 300 秒，给出最小补丁（P0-D.1、P0-D.2）。超时只用来兜住挂死。
- 鉴权定为 R1（URL 路径能力令牌，只读）；R2 列为以后的选项。
- 沙箱定为本地 Docker（colima，配置见 P0.1），可达性测试以 colima 为准；Daytona 不用。
- 来源面板：P0 用不加迁移的降级方案（P0-D.3）；逐篇来源定位的完整核心改动移到 P3（新 P0-D.3b），与发布数字核验一起做，迁移进 Dalton 独立链 `d004`。
- 工作区骨架和迁移对齐 PK v0.3：工作区名就是 ticker；知识文件按 PK.1 布局；迁移脚本经 PK.7 的导入 API 写知识文档（P1.3、P1.4）。
- 事件结论 JSON 用 Playbook 正式词形，界面和 md 用中文短标签（P1.1）。
- 本阶段只做美股；港股和 A 股深度放到 P6。
- 独立 Dalton 部署已停，删掉相关冲突问题；附录 C 只保留剩余 owner 问题。

---

## 0. 读前须知

### 0.1 标记约定

| 标记 | 含义 |
|---|---|
| 【已核实】 | 本文作者在 2026-10-18 读过代码或实测过。文中给了路径和行号 |
| 【新建】 | 文件或目录现在不存在，由本步骤创建 |
| 【待验证】 | 推断或外部行为，实施者必须先实测，再按结果走主方案或备选方案 |
| 【owner】 | 需要 owner 本人在本机操作，例如往 `.env` 或 `/etc/hosts` 写值。需要拍板的事项都已决定，见附录 C |

路径写法：
- `<repo>` 指 `/Users/everflow/Projects/dalton-langalpha`。
- `~/.openclaw/workspace` 简写为 `$OCW`。
- 旧 Dalton 的 Mega Tech 库简写为 `$CORE_DB`，即 `/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core/core.sqlite`。

### 0.2 不变的工作规则

1. **只读外部来源。** 不改 `$OCW` 下任何文件，不改 `$CORE_DB`，不改 `~/.openclaw/openclaw.json`。
2. **凭证不出宿主机进程，也不进文档和日志。** 只记录键名，包括 `GEMINI_API_KEY`、`EDGAR_IDENTITY`、`GUIDEPOINT_CLIENT_ID` 和 `GUIDEPOINT_CLIENT_SECRET`、`FMP_API_KEY`、`DALTON_RESEARCHDATA_URL`、`DALTON_BRIDGE_TOKEN`。
3. **先真实调用，再写测试。** 单元测试只锁定已经稳定的契约，例如期间匹配规则、来源身份映射表。见 `<repo>/AGENTS.md` 的 "Working principles"。
4. **面向 agent 的 docstring 和说明遵守 `<repo>/src/tools/AGENTS.md`**：
   - 每句话都要能改变一次调用决策。
   - MCP 工具的 `Returns:` 写完整的机器结构。
   - 处理建议放在 `Args:` 之前。
   - 目标长度 800 字符以内，硬上限 900。
5. **上游友好。** 新代码尽量放进 `plugins/dalton*/`、`scripts/dalton/` 和独立模块。必须改 LangAlpha 核心时，列出精确文件，并把改动压到最小。

### 0.3 方向调整（owner 2026-10-18 指示）

数据部分**不再新写**一个多路由的 `dalton_research` server，改为：

- **原样复用** OpenClaw 的 `researchdata` MCP server（`$OCW/scripts/researchdata_mcp.py`）。它在宿主机上运行，经一个薄的 stdio→streamable-HTTP 桥暴露给 LangAlpha。
- 基金 wiki 由 researchdata 的 `company-wiki` 路由覆盖，**不单独做 `fund_wiki` server**。wiki 对本项目**只读，永不回写**（owner 决定）；唯一的缺口"反向提及"如有需要再做成只读小工具（P0-D.4）。
- SEC、价格、一致预期仍按原计划处理，见 P0-D.5 和 P0-D.6。

owner 原话是"通过 egress relay、认证头存 vault"。代码核实表明，这条路在不改核心的前提下走不通，见 0.4 第 6 至 8 行。**已决定先用 R1**（URL 路径里的只读能力令牌，P0-D.1）；需要改核心的 R2 列为以后的选项。

### 0.4 关键事实（实施前必须知道）

| # | 事实 | 出处 | 影响 |
|---|---|---|---|
| 1 | researchdata 是只读的 stdio JSON-RPC server，按行分隔（serverInfo `researchdata` 1.3.0，协议 `2024-11-05`）。它不处理 `initialize` 之外的握手，也不要求先 `initialize`。工具共 6 个：`catalog`，以及 `finance`、`macro`、`news`、`research`、`community` 各一个。每个分类工具带 `route` 枚举，参数包括 `mode`、`operation`、`ticker`、`query`、`series`、`concept`、`item`、`document`、`form`、`dataset`、`start`、`end`、`as_of`、`vintage`、`limit`（1–100，默认 3）、`timeout`（1–600，默认 180）和 `params`，`additionalProperties:false` | 【已核实】`$OCW/scripts/researchdata_mcp.py` L20-40, L84-125 | 桥可以按"每个请求起一个子进程"实现，无状态，可以并发 |
| 2 | 每次调用都会派生子进程 `python3 scripts/data_gateway.py <mode> <route> ...`。返回的是 gateway 结果里的 `data` 字段，**gateway 的外层信封被剥掉了**。`ok:false` 时设 `isError:true` | 【已核实】同上 L70-77 | 来源映射必须按各路由底层输出的结构来解析 |
| 3 | OpenClaw 用 `/opt/homebrew/bin/python3 <script>` 启动它，cwd 为 `$OCW`，`requestTimeoutMs` 620000 | 【已核实】`openclaw.json mcp.servers.researchdata`（只读了 command、args、cwd 和超时） | 桥必须用同一个解释器和同一个 cwd |
| 4 | AlphaEngine（`127.0.0.1:8950/mcp`）和 Guidepoint（`127.0.0.1:8943/mcp`）本身都是宿主机上的 streamable-HTTP MCP，**不带认证头**。凭证在它们自己的进程里：AlphaEngine 从桌面 App 进程参数读 JWT；Guidepoint 由代理进程刷新 OAuth，令牌文件不读 | 【已核实】`openclaw.json` 只读 transport 和 url；`lsof` 显示两个端口都在监听；`GET :8950/health` 返回 `status: ready` | 凭证天然只在宿主机，桥不需要接触它们 |
| 5 | LangAlpha 在**沙箱内**启动 stdio MCP。沙箱看不到宿主机文件（`vectors.db`、脚本），也看不到宿主机 localhost | 【已核实】`src/ptc_agent/core/sandbox/assets.py` L577-685，只复制入口文件 | researchdata 不能作为 stdio 放进 bundle |
| 6 | **egress relay 只代理公网 HTTPS**：`pin_public_url(require_https=True)`，`ip.is_global`。它只给用户层 server 发放 grant，内置 bundle 没有 grant。用户层 server 的 URL 也会拒绝 `localhost`、`*.internal` 和非全局 IP | 【已核实】`src/server/services/egress/relay.py`；`src/server/utils/egress_guard.py` L126-173；`grant_scope.py` L89-93；`src/server/models/mcp_server.py` L270-313 | owner 设想的"relay + vault 头"**不能直接用**于宿主机服务 |
| 7 | **内置的 HTTP MCP 在沙箱里调用时不发任何请求头**。URL 里的 `${VAR}` 在后端加载时展开 | 【已核实】`src/ptc_agent/core/tool_generator.py` L688-701；`src/ptc_agent/core/sandbox/mcp_client_runtime.py` L391-406；`src/ptc_agent/config/plugins.py` L184-191 | 访问控制只能放进 URL，做成能力令牌 |
| 8 | 直连路径（模型直接发 JSON 工具调用）也要经 relay，所以对私有地址不可用 | 【已核实】`AGENTS.md` "PTC pattern"；`src/server/services/egress/direct_tools.py`、`src/server/services/tool_binding.py` | researchdata **只能走 PTC**，即在沙箱代码里调用生成的 wrapper |
| 9 | 沙箱调用内置 HTTP server 时，单次 `tools/call` 的预算写死为 **65 秒**（`_HTTP_CALL_BUDGET = 65.0`，注释说要高于 egress relay 的 55 秒墙钟）；握手和 `tools/list` 的预算是 30 秒。researchdata 不走 relay（非 OAuth server，不是 `relay_bound`），所以只受这 65 秒约束。外层 `execute_code` 的默认上限是 300 秒（`security.max_execution_time`，`src/ptc_agent/config/core.py` L121） | 【已核实】`mcp_client_runtime.py` L57-62、L1556-1571；`tool_generator.py` L659-671；`src/server/services/egress/relay.py` L73 | 65 秒对长文读取太短。P0-D.2 把它改成按 server 可配置（researchdata 默认 300 秒），不再让桥把超时压到 45 秒 |
| 10 | 上游默认沙箱是 **Daytona**（云端），根本到不了宿主机。只有 `provider: docker` 能经 `host.docker.internal` 访问宿主机（`ExtraHosts host-gateway`） | 【已核实】`agent_config.yaml` L25 与 L60-62；`src/ptc_agent/core/sandbox/providers/docker.py` L1020-1036；`docker-compose.yml` L50-51 | 已决定用本地 docker 沙箱（colima，P0.1），不用 Daytona |
| 11 | 本机 Docker 运行时是 **colima 0.10.3**，2026-10-18 检查时 daemon 未运行。宿主机上**没有 `uv` 和 `uvx`**，`npx` 在 `~/.openclaw/tools/node/bin/npx` | 【已核实】`colima version`；`which uv` 无结果 | colima 按 P0.1 第 0 步配置（4 CPU、5 GiB、VM 在 EveSSD）；colima 下 `host-gateway` 是否指向 Mac 的回环地址由 P0-D.0 实测；宿主机先装 uv（P0.1） |
| 12 | 来源记录对 MCP 结果只记一条 `source_type="mcp_tool"`，`identifier="server:tool"`，**不解析结果里的标题、URL、文档 id 和日期**。`result_body` 上限 64 KiB，每次执行的总预算 4 MiB；错误结果不入账 | 【已核实】`src/ptc_agent/agent/middleware/provenance/middleware.py` L769-828；`src/ptc_agent/core/tool_generator.py` L67；`mcp_client_runtime.py` L582-650 | 要让来源面板显示来源身份，必须改核心：P0 做不加迁移的降级版（P0-D.3），完整版在 P3（P0-D.3b） |
| 13 | 前端 `ProvenanceSourceType` 是封闭的联合类型（8 种）。`provenance_records` 的 `source_type` 是不带 CHECK 的 TEXT，列集合固定（`identifier`、`title`、`detail`、`provider` 等） | 【已核实】`web/src/types/sse.ts` L126-134；`migrations/versions/013_add_provenance_records.py` L23-39 | 新增 `research_doc` 类型不需要迁移；要存 URL 和 as-of 这两个新字段才需要迁移（P3 的 d004） |
| 14 | `wiki.db` 是 **0 字节的空文件**。真正的索引是 `wiki/vectors.db`：documents 1,086 行、chunks 9,507 行，FTS5 trigram 加 3072 维 Gemini 向量 | 【已核实】`sqlite3 -readonly` | 蓝图里"wiki.db + 向量索引"的说法要更正 |
| 15 | plugin 里的 skill 如果没有 `SKILL_REGISTRY` 条目，只会以"Your skills（用户内容）"的身份出现。`requires` **只能**在 `registry.py` 的 `SkillDefinition` 里声明，SKILL.md 的 frontmatter 不解析这个键 | 【已核实】`src/ptc_agent/agent/middleware/skills/registry.py` L17-66 与 L213-236；`discovery.py` L127-221 | `dalton-conventions` 必须注册，见 P1.1 |
| 16 | LangAlpha 没有工作区模板机制。只有首台机器首次 provision 时会写一份默认 `agent.md`。有 REST 接口可以建工作区、写文件（OSS 模式不需要认证），记忆**没有**写接口 | 【已核实】`src/server/services/computer_manager/_provisioning.py` L412-435；`src/server/app/workspace_files/crud.py` L555-622；`src/server/app/memory.py` | 初始化走脚本加 skill，见 P1.3 |
| 17 | 工作区文件夹名允许 `.`，只替换 `/ \ : $ \` "` 和控制字符；`Home` 是保留名 | 【已核实】`src/server/database/workspace_names.py` L27-29 | 工作区名就是 ticker（与 PK v0.3 一致），`META`、以后的 `3690.HK` 都可以直接用 |

### 0.5 步骤总览与依赖

```
P0-D.0 宿主机可达性验证（colima） ──┬─> P0-D.1 researchdata 宿主机桥 ──> P0-D.2 注册为内置 HTTP MCP（含超时补丁）──┐
                                    │                                                                          ├─> P0-D.3 来源显示（P0 降级版，无迁移）
                                    └─> P0-D.5 SEC（dalton_sec，沙箱 stdio，公网）─────────────────────────────┘
P0-D.4 wiki 覆盖评估（只读，依赖 P0-D.2）    P0-D.6 价格与一致预期（依赖 P0-D.2）    P0-D.7 P6 清单
P0-D.8 P0-D 验收
P1.1 dalton-conventions ─┐
P1.2 覆盖名单 ───────────┼─> P1.3 公司工作区初始化 ──> P1.4 旧认知迁移（经 PK.7 导入 API）──> P1.5 P1 验收
       （P1.3 和 P1.4 的文件结构以 PK-knowledge-governance-autonomy.md v0.3 为准）
P0-D.3b 逐篇来源定位（完整版，P3 执行，和 PK.9 数字核验一起）
```

| 步骤 | 工时（人日） | 能否并行 |
|---|---|---|
| P0-D.0 | 0.5 | 最先做 |
| P0-D.1 | 1.75 | 在 D.0 之后 |
| P0-D.2 | 1.0 | 在 D.1 之后（含 LangAlpha 侧超时补丁） |
| P0-D.3 | 1.0 | 契约设计可以和 D.1 同时做 |
| P0-D.4 | 0.25 | 在 D.2 之后 |
| P0-D.5 | 2.5 | 和 D.1 并行 |
| P0-D.6 | 0.5 | 在 D.2 之后 |
| P0-D.7 | 0.1 | — |
| P0-D.8 | 0.5 | 最后做 |
| **P0-D 合计** | **约 8.1**（两人并行约 5 个工作日） | |
| P1.1 | 2.5 | 可以和 P0-D 并行 |
| P1.2 | 1.0 | 可以和 P0-D 并行 |
| P1.3 | 1.25 | 需要 PK.1/PK.2 定稿（v0.3 已定） |
| P1.4 | 3.0 | 在 P1.3 和 PK.7 之后 |
| P1.5 | 0.5 | 最后做 |
| **P1 合计** | **约 8.25** | |
| P0-D.3b（P3 执行） | 1.25 | 和 PK.9 同期 |

蓝图对每个阶段的估计是 1 周。单人做会超期，建议 P0-D.5 和 P1.1、P1.2 交给第二个人并行。

---

# P0-D 数据接通

## P0-D.0 宿主机可达性验证

**目标：** 用实测回答一个问题："在 colima 下，LangAlpha 的 docker 沙箱容器和后端容器，能否访问 Mac 宿主机 127.0.0.1 上的服务？"再按结果确定桥绑定哪个地址、URL 写什么。两个容器都要通：沙箱里的代码调用 researchdata；后端进程做 MCP 发现，PK.16 的零成本预检也从后端直接调桥。

**前置依赖：**
- P0.1 已完成：colima 按 4 CPU、5 GiB、VM 在 `/Volumes/EveSSD` 启动，`make up` 能跑起整套服务。
- 沙箱使用 `SANDBOX_PROVIDER=docker`（已决定，不用 Daytona，见 0.4 第 10 行）。
- 已构建 `langalpha-sandbox:latest`。

**改动/新增文件：** 无代码改动。实测记录写进本步骤 PR 的描述，作为验收证据。

**具体做法：**

1. 启动运行时，确认 docker 可用（参数见 P0.1 第 0 步）：
   ```bash
   export COLIMA_HOME=/Volumes/EveSSD/colima
   colima status || colima start
   docker info --format '{{.Name}} {{.OperatingSystem}} cpus={{.NCPU}}'
   ```
2. 用一个普通容器探测宿主机回环。AlphaEngine 的 `/health` 不需要凭证【已核实】：
   ```bash
   docker run --rm --add-host host.docker.internal:host-gateway curlimages/curl:8.11.1 \
     -sS -m 5 http://host.docker.internal:8950/health
   ```
   - 期望返回 JSON `{"ok":true,...,"status":"ready"}`。
   - 失败时按下面的顺序排查：
     1. `colima ssh -- getent hosts host.lima.internal`，期望 `192.168.5.2`。
     2. `docker run --rm curlimages/curl:8.11.1 -sS -m 5 http://192.168.5.2:8950/health`。
     3. 第 2 步通、第 1 步不通，说明 docker 的 `host-gateway` 指向 VM 内的 docker0，而不是 Mac。修法是在 colima 的 docker daemon 配置里设 `"host-gateway-ip": "192.168.5.2"`（`colima start --edit`），然后重启【待验证】（已获授权，由 lead/运维执行）。
3. 在 LangAlpha 真实的沙箱容器里探测（先在 UI 里随便发一句话，让沙箱起来）：
   ```bash
   docker ps --format '{{.Names}} {{.Image}}' | grep langalpha-sandbox
   docker exec <sandbox> python3 -c "import urllib.request as u;print(u.urlopen('http://host.docker.internal:8950/health',timeout=5).read()[:120])"
   ```
4. 在后端容器里探测。后端的 MCP 发现注册表在后端进程里连 HTTP server，见 0.4 第 7 行：
   ```bash
   docker compose exec backend python -c "import urllib.request as u;print(u.urlopen('http://host.docker.internal:8950/health',timeout=5).read()[:120])"
   ```
5. 如果开发时后端是在宿主机上用 `uv run python server.py` 直接跑的，Mac 本身无法解析 `host.docker.internal`。这种情况在 `/etc/hosts` 加一行 `127.0.0.1 host.docker.internal`【owner】，让同一个 URL 在宿主机和容器里都能用。
6. 端口占用检查。桥默认用 8960：
   ```bash
   lsof -nP -iTCP:8960 -sTCP:LISTEN
   ```
   2026-10-18 检查时空闲。

**决策表：**

| 结果 | 桥绑定地址 | `DALTON_RESEARCHDATA_URL` 主机部分 |
|---|---|---|
| 第 2 步直接通 | `127.0.0.1` | `host.docker.internal` |
| 只有 `192.168.5.2` 通，且改 daemon 后仍不通 | `127.0.0.1`（lima 的 slirp 会转到 Mac 回环）【待验证】 | `192.168.5.2` |
| 都不通 | 停下，上报 lead | — |

**验证方式：** 以上命令的输出贴进 PR。

**验收标准：**
- 第 2 至 4 步在沙箱容器和后端容器里各成功一次。
- 记录了最终采用的主机名和端口。

**风险与注意：**
- Linux 生产机上 `host-gateway` 是 docker0 的 IP，宿主机服务要绑定 `0.0.0.0` 或网桥 IP 才能被访问。本文只针对本机 Mac 和 colima 的环境；不考虑 Docker Desktop 和 Daytona。
- 重建 colima VM（例如改 CPU 或内存）之后，第 2 至 4 步要重跑一次。
- 本步只用 8950 端口证明这条路径走得通。最终沙箱**只访问桥（8960）**，不直接访问 8950 和 8943。

**工时：** 0.5 人日。

---

## P0-D.1 researchdata 宿主机桥

**目标：** 在宿主机上原样运行 researchdata，经一个能鉴权、限时、可审计的 HTTP 桥暴露成 streamable-HTTP MCP。凭证（AlphaEngine JWT、Guidepoint OAuth、`GEMINI_API_KEY`、`EDGAR_IDENTITY`）全部留在宿主机进程里。

**前置依赖：** P0-D.0 已经确定绑定地址。

### 方案比较

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| A. `mcp-proxy`（sparfenyuk），用 uvx 固定版本 | `uvx --from 'mcp-proxy==<ver>' mcp-proxy --host 127.0.0.1 --port 8960 -- /opt/homebrew/bin/python3 $OCW/scripts/researchdata_mcp.py` | 现成，支持 SSE 和 streamable HTTP | 宿主机没有 uv，要先装；只有一个 stdio 子进程，调用串行，一个长文读取会堵住所有请求；不能按路由设超时，不能加路径令牌和 `_sources`；它和 2024-11-05 协议协商的行为【待验证】 | 备选 |
| B. `supergateway`，用 npx 固定版本 | `npx -y supergateway@<ver> --stdio "<cmd>" --outputTransport streamableHttp --port 8960` | 已有 node | 缺点同 A；会话有状态；没有鉴权【待验证】 | 备选 |
| C. 自写桥 `scripts/dalton/researchdata_bridge.py`，只用标准库 | `ThreadingHTTPServer`；每个 `tools/call` 起一个 researchdata 子进程；桥自己应答握手 | 不依赖第三方包，也不绑定 MCP SDK 版本；能并发；能按路由设超时、加令牌、加 `_sources`、写审计日志；researchdata 本身不需要 `initialize` | 要自己维护 streamable HTTP 的一个子集（JSON 响应、无服务端推送），约 300 行 | **推荐** |

AGENTS.md 的"第三方 MCP 隔离启动"规则针对的是 LangAlpha 自己拉起的 stdio server。桥运行在宿主机、在 LangAlpha 之外，方案 C 是我们自己的代码，不受这条约束。

### 鉴权的决定（已定：R1），以及为什么不走 relay 和 vault

- relay 拒绝私有地址，内置 server 也拿不到 grant；内置 HTTP server 在沙箱里不发请求头。见 0.4 第 6 至 8 行。
- **采用 R1：只读能力令牌放在 URL 路径里。**
  - URL 写成 `http://host.docker.internal:8960/mcp/<DALTON_BRIDGE_TOKEN>`。
  - 整个 URL 写进后端 `.env` 的 `DALTON_RESEARCHDATA_URL`。compose 用 `env_file: .env` 把它带进后端【已核实 `docker-compose.yml` L59】，后端在加载 bundle 时展开 `${DALTON_RESEARCHDATA_URL}`。
  - **安全口径要说清楚：** 这个令牌会出现在沙箱的 MCP 客户端配置里，沙箱代码看得到。但它只授予"调用只读的 researchdata"这一项能力（桥上没有任何写路由，wiki 也只读），而这本来就是 agent 应有的能力。真正的上游凭证从不离开宿主机。令牌的作用是挡住同机其他容器和局域网的访问，并且可以轮换。
- **以后的选项 R2（改核心，本阶段不做）：** 新增"运营方白名单的私有出站"。
  - 在 `egress_guard` 里加一个 `DALTON_PRIVATE_EGRESS_ALLOWLIST=host.docker.internal:8960`。
  - 给内置 HTTP server 一类 backend-env 头来源，在 `credentials.py` 里从后端环境读 `DALTON_BRIDGE_TOKEN`。
  - 在 `grant_scope.py` 里允许这类内置 server 拿 grant，让 `tool_generator` 把它标成 `relay_bound`。
  - 这样令牌不进沙箱，但要改 relay 的 SSRF 防线，需要安全评审。估计 2 至 3 人日。只有当令牌暴露在沙箱里成为实际问题时才考虑。
  - 注意：R2 下 researchdata 会变成 `relay_bound`，单次调用要受 relay 的 55 秒墙钟约束（`relay.py` L73），长文读取会被截断。真要走 R2，relay 的墙钟也要按路由放宽。

### 改动/新增文件

| 路径 | 状态 | 说明 |
|---|---|---|
| `<repo>/scripts/dalton/researchdata_bridge.py` | 【新建】 | 桥本体，只用标准库，运行在 `/opt/homebrew/bin/python3`（3.14） |
| `<repo>/scripts/dalton/source_identity.py` | 【新建】 | 路由结果 → `_sources` 的映射表，见附录 B。桥和 P0-D.5 共用 |
| `<repo>/scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template` | 【新建】 | launchd 模板 |
| `<repo>/scripts/dalton/install_bridge.sh` | 【新建】 | 渲染模板，执行 `launchctl bootstrap` 和 `kickstart` |
| `<repo>/scripts/dalton/check_bridge.sh` | 【新建】 | 健康检查加一次真实调用 |
| `~/.config/dalton/researchdata-bridge.env` | 【新建】【owner】 | 权限 0600。键：`DALTON_BRIDGE_TOKEN`、`DALTON_BRIDGE_BIND`、`DALTON_BRIDGE_PORT`、`RESEARCHDATA_PYTHON`、`RESEARCHDATA_SCRIPT`、`DALTON_BRIDGE_MAX_CONCURRENCY`、`DALTON_BRIDGE_DEFAULT_TIMEOUT_S`（默认 240）、`DALTON_BRIDGE_LONG_TIMEOUT_S`（默认 600）、`DALTON_BRIDGE_CALLER_BUDGET_S`（默认 300，必须等于 P0-D.2 里 researchdata 的 `call_timeout_s`）、`DALTON_ALPHAENGINE_DAILY_QUOTA`（可空，用于 80% 告警） |
| `~/Library/LaunchAgents/com.dalton.researchdata-bridge.plist` | 【新建】【owner】 | 由 `install_bridge.sh` 生成 |
| `<repo>/.env` | 改【owner】 | 加 `DALTON_RESEARCHDATA_URL`，只写键名，值不入库 |

### 具体做法：桥的接口契约

**HTTP 路由**

| 方法与路径 | 行为 |
|---|---|
| `POST /mcp/<token>` | JSON-RPC 入口。令牌用 `hmac.compare_digest` 比较，不匹配一律返回 404，不提示令牌错误 |
| `GET /mcp/<token>`、`DELETE /mcp/<token>` | 405。不提供服务端推送流，客户端遇到 405 应回落到纯 POST【待验证 LangAlpha 客户端的行为】 |
| `GET /healthz` | 不带令牌，只回状态，不回数据，见下面的结构 |
| 其他路径 | 404 |

`GET /healthz` 的返回：
```json
{"ok": true, "bridge_version": "0.1.0", "researchdata": {"script": "present", "server_version": "1.3.0"},
 "alphaengine": {"status": "ready", "fulltext_today": 12, "daily_quota": 200, "quota_alert": false},
 "guidepoint": {"tcp": "open"}, "uptime_s": 1234,
 "inflight": 0, "max_concurrency": 4,
 "timeouts": {"default_s": 240, "long_s": 600, "caller_budget_s": 300}}
```
- `alphaengine.status` 取自 `GET 127.0.0.1:8950/health` 的 `status` 字段，那个接口不需要凭证【已核实】。
- Guidepoint 只做 TCP 连通检查。它没有 health 接口，`self-test` 会带凭证访问上游，所以不调用。
- `fulltext_today` 从当天的审计日志里数 `alphaengine` 的 `get_document` 次数（按 Asia/Shanghai 日期）。配了 `DALTON_ALPHAENGINE_DAILY_QUOTA` 时，达到 80% 就把 `quota_alert` 置为 true，并写一条告警日志。**只告警，不拦截**：配额由 AlphaEngine 自己执行。

**JSON-RPC 方法**

| method | 桥的处理 |
|---|---|
| `server/discover` 等未知方法 | 立刻回 `-32601`。LangAlpha 客户端会先探测 `2026-07-28` 版本，失败后回落到旧式 `initialize`【已核实 `mcp_client_runtime.py` L47-50】 |
| `initialize` | 桥自己应答。`protocolVersion` 规则：客户端请求的版本若在 `{"2025-03-26","2025-06-18","2025-11-25"}` 内就原样回显，否则回 `2025-03-26`。`capabilities:{"tools":{}}`，`serverInfo:{"name":"researchdata","version":"1.3.0+bridge0.1"}`。可以返回一个随机的 `Mcp-Session-Id`，但不依赖它【待验证：握手要能通过 LangAlpha 客户端 `_call_mcp_tool_http`】 |
| 通知（没有 `id`） | 回 202，空 body |
| `ping` | `{}` |
| `tools/list` | 起一个子进程，取 researchdata 的 `tools/list`，缓存 10 分钟。**过滤一处：** 从每个分类工具的 `route` 枚举里去掉 `kind` 为 `agent_tool` 或 `skill` 的路由。这些是 handoff 路由，在 LangAlpha 里不会执行，留着只会浪费调用。路由的 kind 从 `$OCW/config/data_sources.json` 读【已核实，字段名是 `kind`】。工具的 description 不改 |
| `tools/call` | 见下面"调用流程" |

**`tools/call` 的调用流程**

1. **参数检查：**
   - `name` 必须在 6 个工具里。
   - `route` 若属于被过滤的 handoff 路由，直接返回 `isError:true`，内容为 `{"ok":false,"status":"error","error":"route_not_available_in_langalpha","hint":"新闻用 WebSearch/WebFetch；X 用 x_api；雪球与社区数据在 P6"}`。
2. **按路由定超时（只兜住挂死，不用来限制正常调用）：**
   - 路由分两类：
     - **长文读取**：`research` 的 `alphaengine` `get_document`、`guidepoint` 全部操作、`transcript`，`company-wiki`/`sales-notes`/`tmtb`/`funda-ai` 的 `read`，`finance` 的 `sec` 中 `read-item`、`search-text`、`attachments`、`financials`。上限 `DALTON_BRIDGE_LONG_TIMEOUT_S`（600 秒）。
     - **其他**：上限 `DALTON_BRIDGE_DEFAULT_TIMEOUT_S`（240 秒）。
   - 调用方预算封顶：实际上限 = `min(路由上限, DALTON_BRIDGE_CALLER_BUDGET_S − 25)`。LangAlpha 侧 researchdata 的单次预算默认 300 秒（P0-D.2），所以默认配置下所有路由实际都在 275 秒以内；要让长文读取用满 600 秒，把 P0-D.2 的 `call_timeout_s` 和这里的 `DALTON_BRIDGE_CALLER_BUDGET_S` 一起调到 660，并把 `security.max_execution_time` 调到 720 以上。
   - `arguments.timeout` 缺省时设为实际上限；大于实际上限时压到上限，并在结果里加 `_bridge.timeout_clamped:true` 和 `_bridge.timeout_s`。
   - 留 25 秒余量的原因：researchdata 外层会把子进程超时设为 `timeout+10`，桥自己的硬超时再多 10 秒（第 4 步），还要留出回传时间，保证超时由桥给出带类型的错误，而不是 LangAlpha 客户端在半路断开。
3. **并发：** 用信号量控制，默认 4 个并发。排队超过 10 秒就返回 JSON-RPC 错误 `-32000 bridge_busy`（这是并发保护，不是次数限制；agent 稍后重试即可）。
4. **执行：**
   - `subprocess.Popen([RESEARCHDATA_PYTHON, RESEARCHDATA_SCRIPT], cwd=$OCW, stdin=PIPE, stdout=PIPE, stderr=DEVNULL, start_new_session=True)`。
   - 写入一行 `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}`，读回一行。
   - 硬超时 = 本次生效的 `timeout + 20` 秒，到时 `os.killpg` 杀掉进程组，返回 `isError:true`，内容为 `{"ok":false,"status":"timeout","timeout_s":<生效值>,"hint":"上游可能挂死；稍后重试，或换一个文档 id"}`。超时只用来兜住挂死，不用来逼 agent 缩小查询。
   - **stderr 不回传，也不落日志**，因为 gateway 的 stderr 可能含凭证线索。gateway 自己也不回显 stderr【已核实】。
5. **加 `_sources`：**
   - 如果第一个 content 块是 text，且能解析成 dict，就调用 `source_identity.declare(tool, arguments, payload)`，得到最多 50 条来源记录。
   - 把它作为**第一个键** `_sources` 插进 dict，再用 `ensure_ascii=False` 序列化。放在第一个键，是为了让 64 KiB 的 body 截断时它仍然留在开头。
   - 其他键不动。图片块原样保留。
6. **审计日志：** 写到 `~/Library/Logs/dalton/researchdata-bridge.jsonl`，一次调用一行：
   ```json
   {"ts":"...","tool":"research","route":"company-wiki","operation":"search","args_sha256":"...","status":"ok","is_error":false,"timeout_s":240,"duration_ms":2310,"result_bytes":18234,"sources":7}
   ```
   日志**不记** query 原文和结果内容。

**环境变量与 launchd**

- researchdata 依赖宿主机环境里的 `GEMINI_API_KEY`（wiki 和邮件库的查询向量）和 `EDGAR_IDENTITY`（SEC 路由）。company-wiki 的 `run.sh` 会自己从 shell profile 读 key，research_library 路径的情况【待验证】。
- launchd 不加载 shell profile。plist 的 `ProgramArguments` 用 `/bin/zsh -lc 'set -a; source ~/.config/dalton/researchdata-bridge.env; exec /opt/homebrew/bin/python3 <repo>/scripts/dalton/researchdata_bridge.py'`，让登录 shell 带上与 OpenClaw 相同的环境【待验证】。
- 还要设：`KeepAlive=true`、`RunAtLoad=true`，`StandardOutPath` 和 `StandardErrorPath` 指向 `~/Library/Logs/dalton/`。
- 判定办法：在 launchd 下跑一次 `research(route="company-wiki", operation="search")`。结果里**不能**有"embedding 失败，回落到 keyword"的 warning。出现了就说明 key 没进环境。

**`install_bridge.sh` 的步骤**

1. 如果 `~/.config/dalton/researchdata-bridge.env` 不存在，用 `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'` 生成令牌并写入，权限设为 0600。**令牌值不打印。**
2. 渲染 plist，然后执行 `launchctl bootout gui/$UID/com.dalton.researchdata-bridge 2>/dev/null; launchctl bootstrap gui/$UID <plist>; launchctl kickstart -k gui/$UID/com.dalton.researchdata-bridge`。
3. 提示 owner 把 `DALTON_RESEARCHDATA_URL=http://<host>:8960/mcp/<token>` 写进 `<repo>/.env`。脚本可以用 `--write-dotenv` 代写；不加这个参数就只提示键名。

### 验证方式（真实调用优先）

1. 宿主机：`curl -s http://127.0.0.1:8960/healthz`，期望 `alphaengine.status=ready`。
2. 宿主机：用 curl 发 `initialize`，再发 `tools/list`。期望 6 个工具，`news` 和 `community` 的枚举里没有 handoff 路由。
3. `check_bridge.sh` 依次做 6 个真实调用，每个都记录状态、耗时和 `_sources` 条数：
   - `research(route="company-wiki", operation="search", query="META capex 2027", limit=5)`
   - `research(route="company-wiki", operation="read", document="wiki:<上一步返回的 id>")`
   - `research(route="alphaengine", operation="search_library", query="Meta Platforms", start="2026-10-01", end="2026-10-18", limit=5)`
   - `research(route="guidepoint", operation="search_library", query="Meta advertising", limit=3)`
   - `finance(route="sec", operation="list-filings", ticker="META", form="10-Q", limit=3)`
   - `finance(route="estimates", ticker="META")`
4. 并发：同时发 6 个 alphaengine 搜索，确认没有串行堵塞，第 5 和第 6 个按设计排队或返回 busy。
5. 超时：
   - 默认配置下发一个 `timeout=600` 的 `alphaengine get_document`，确认被压到 275，结果里有 `_bridge.timeout_clamped` 和 `_bridge.timeout_s`；
   - 把 `DALTON_BRIDGE_CALLER_BUDGET_S` 临时调到 660 后重发，确认生效值是 600；
   - 用一个故意挂住的假子进程（`RESEARCHDATA_SCRIPT` 指向 `sleep` 脚本，只在测试时）确认到点被杀，返回 `status:"timeout"`。
6. 后端容器可达：`docker compose exec backend python -c "..."` 对 `/healthz` 发请求，返回 200（PK.16 的预检从后端直接调桥）。
7. 令牌：错误令牌返回 404；`/healthz` 不泄露任何数据。
8. 重启：`launchctl kickstart -k` 之后 5 秒内恢复；重启 Mac 后自动拉起。

### 验收标准

- `check_bridge.sh` 的 6 个调用全部成功。每个 `_sources` 都至少有 1 条，且带 `identity`、`document_id` 和 `as_of`。
- 审计日志里没有 query 原文、结果内容或凭证。`grep -iE 'bearer|token|key=' researchdata-bridge.jsonl` 没有输出。
- launchd 管理下 embedding 正常，没有 keyword 回落的 warning。
- 代码里没有任何对 `$OCW` 的写操作（code review 检查 `open(..., 'w')` 和 `subprocess` 的参数）。

### 风险与注意

- **AlphaEngine 全文有配额。** 桥不缓存，researchdata 里也只有 AlphaEngine server 自带 10 分钟缓存。agent 侧的使用规则写进 instruction，见 P0-D.2。配额只做 80% 告警（`/healthz` 的 `quota_alert`），不设闸门。
- AlphaEngine 的令牌依赖桌面 App 在运行。App 没开时返回 `E_DESKTOP_NOT_RUNNING`，属于预期内的失败，按错误信封透传。
- `cursor` 分页依赖 AlphaEngine server 内存里的游标（有效 15 分钟）。桥是无状态的，但 AlphaEngine server 是同一个进程，不受影响。
- researchdata 升级（OpenClaw 侧改动）会直接影响 LangAlpha。桥在 `/healthz` 里报 `server_version`。版本变化时 `tools/list` 缓存失效，并写一条日志。

**工时：** 1.75 人日（桥 1 天，按路由超时和配额统计 0.25 天，launchd、脚本和验证 0.5 天）。

---

## P0-D.2 注册为 LangAlpha 内置 HTTP MCP（`plugins/dalton_data/`）

**目标：** LangAlpha 启动时发现 researchdata 的 6 个工具，为沙箱生成 wrapper，并在提示词里给出正确的使用指引。同时把 LangAlpha 侧写死的 65 秒 MCP HTTP 单次调用预算改成按 server 可配置，researchdata 默认 300 秒。

**前置依赖：** P0-D.1 的桥在跑，`.env` 里已有 `DALTON_RESEARCHDATA_URL`。

### 改动/新增文件

| 路径 | 状态 |
|---|---|
| `<repo>/plugins/dalton_data/plugin.json` | 【新建】 |
| `<repo>/plugins/dalton_data/mcp.json` | 【新建】 |
| `<repo>/plugins/dalton_data/_bootstrap.py` | 【新建】，从 `plugins/langalpha_market_data/_bootstrap.py` 复制，供 P0-D.5 的 stdio server 使用 |
| `<repo>/plugins/dalton_data/README.md` | 【新建】，可选，写运维说明：桥、令牌、超时、故障排查 |
| `<repo>/agent_config.dalton.yaml` | 改（P0.6 建的 overlay）：`mcp.servers` 里给 researchdata 加 `call_timeout_s: 300`；`security.max_execution_time` 视需要调高 |
| `<repo>/src/ptc_agent/config/core.py` | 钩子 `DALTON-HOOK(mcp-call-timeout)`，2 行 |
| `<repo>/src/ptc_agent/core/tool_generator.py` | 钩子 `DALTON-HOOK(mcp-call-timeout)`，2 行 |
| `<repo>/src/ptc_agent/core/sandbox/mcp_client_runtime.py` | 钩子 `DALTON-HOOK(mcp-call-timeout)`，约 6 行 |
| `<repo>/src/ptc_agent/core/sandbox/tool_overlay.py` | 钩子 `DALTON-HOOK(mcp-call-timeout)`，1 行 |

### 具体做法

**`mcp.json`**

mcp.json 是封闭格式，只允许 `type`、`url`、`headers`。传输名必须写 `streamable-http`；写成 `http` 会被当作未知传输跳过【已核实 `plugins.py` L44】。

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "researchdata": { "type": "streamable-http", "url": "${DALTON_RESEARCHDATA_URL}" },
    "dalton_sec": {
      "type": "stdio", "command": "uv",
      "args": ["run", "python", "plugins/dalton_data/dalton_sec_mcp_server.py"],
      "env": { "SEC_USER_AGENT": "${SEC_USER_AGENT}" }
    }
  }
}
```
`dalton_sec` 见 P0-D.5，可以等它完成后再加。server 名用 `researchdata`，与 OpenClaw skill 的写法 `researchdata.research(route=...)` 保持一致，方便迁移方法论。

**`plugin.json`**

`extensions["ai.langalpha"].servers.researchdata` 里的 `ServerMeta` 是 `extra:"forbid"`，只允许 `description`、`instruction`、`tool_exposure_mode`、`vault_blueprints` 和 `icon` 这几个键【已核实 `plugins.py` L47-65】。`instruction` 的草稿如下。我们不拥有这个 server，所有使用指引只能放在这里。

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "dalton-data",
  "version": "0.1.0",
  "description": "Dalton fund data: the fund's internal research library (AlphaEngine broker research, Guidepoint expert calls, sales notes, TMTB, FUNDA AI, company wiki, call transcripts), consensus snapshots, SEC filings and XBRL facts.",
  "author": {"name": "Dalton"},
  "license": "Proprietary",
  "extensions": {"ai.langalpha": {"servers": {
    "researchdata": {
      "description": "Fund internal research library and data gateway (read-only): broker research, expert calls, sales notes, company wiki, transcripts, estimates, SEC, FRED.",
      "tool_exposure_mode": "detailed",
      "instruction": "<见下文>"
    }
  }}}
}
```

`instruction` 的草稿（英文，便于和 LangAlpha 的英文系统提示衔接，术语保留中文；约 1,400 字符，实施时压缩到能用的最短）：

> Fund-internal sources; call from execute_code only (no direct path). Start with `catalog()` once per thread to see routes. Research: `research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=...)`.
> - Routes: `company-wiki` (fund wiki; params.company=TICKER), `all` (wiki+sales-notes+TMTB+FUNDA AI fused), `alphaengine` (broker research; search_library then get_document), `guidepoint` (expert calls), `transcript` (earnings calls), `sales-notes`, `tmtb`, `funda-ai`.
> - Finance: `finance(route="sec"|"estimates"|"cn-hk")`. Macro: `macro(route="fred"|"treasury")`.
> - Read only ids that search returned (`wiki:`, `mail:`, `tmtb:`, `funda:`, `alphaengine` doc_id). Page long text with params.offset/max_chars.
> - Leave `timeout` unset: the bridge sets per-route limits and long reads may take minutes. Keep date windows narrow for relevance. AlphaEngine full text is quota-limited, so read only what you need.
> - Every result has `_sources` (identity, document_id, as_of, url). Cite with identity, publisher and date. Expert calls: identity, not name. Guidepoint verbatim ≤20 words.
> - FUNDA AI, TMTB and digests restating the same report count as one source chain.
> - Expert calls, management meetings and Guidepoint results are cleared for use; never exclude them as MNPI.
> - The fund wiki is read-only; never try to write to it.
> - `status: empty` means "本轮未取得", never "公司未披露".

- `tool_exposure_mode` 选 `detailed`。工具只有 6 个，但参数是通用的大枚举，agent 需要看到签名才能用对。
- `vault_blueprints` 不填：R1 方案下令牌不在 vault 里。

**发现与生成：**
1. 重启后端。`./plugins` 是 bind-mount 进去的，重启就够，不需要重新构建【已核实 `plugins/README.md` L104-108】。
2. 后端日志里应该出现 researchdata 的发现成功。
3. 进一个工作区跑一个 turn，触发 wrapper 生成和同步。

**暴露方式：** 只走 PTC。`direct` 绑定会经 relay，对私有地址不可用（0.4 第 8 行）。上游的 `agent_config.yaml` 不动；唯一的覆盖是下面的超时，写在 `agent_config.dalton.yaml` 里。

### LangAlpha 侧：MCP HTTP 单次调用超时可配置（钩子 `mcp-call-timeout`）

**现状：** `src/ptc_agent/core/sandbox/mcp_client_runtime.py` L62 写死 `_HTTP_CALL_BUDGET = 65.0`，L1567-1571 的 `tools/call` 用它做 httpx 超时和每次发送的截止时间。这个值是为 relay 的 55 秒墙钟定的；researchdata 是直连的内置 HTTP server，不经 relay，却也被它卡在 65 秒。

**最小补丁（4 个文件，约 11 行，全部带 `# DALTON-HOOK(mcp-call-timeout)`）：**

1. `src/ptc_agent/config/core.py` 的 `MCPServerConfig` 加一个可选字段：
   ```python
   # DALTON-HOOK(mcp-call-timeout): per-server tools/call budget for slow direct HTTP servers
   call_timeout_s: float | None = Field(default=None, ge=10, le=900)
   ```
2. `src/ptc_agent/core/tool_generator.py` 的 `_client_entry`，HTTP/SSE 分支建好 `entry` 之后：
   ```python
   if server.call_timeout_s:  # DALTON-HOOK(mcp-call-timeout)
       entry["call_timeout_s"] = float(server.call_timeout_s)
   ```
3. `src/ptc_agent/core/sandbox/mcp_client_runtime.py`：
   - `_ServerCfg` 加字段 `call_timeout_s: float`；
   - `_normalize` 里 `call_timeout_s=float(entry.get("call_timeout_s") or _HTTP_CALL_BUDGET)`；
   - `tools/call`（L1567、L1571）把 `_HTTP_CALL_BUDGET` 换成 `cfg.call_timeout_s`。`relay_bound` 的 server 不会带这个键（`generate_client_config` 对 OAuth server 不走 `_client_entry`），所以仍是 65 秒，relay 的错误类型不受影响。
4. `src/ptc_agent/core/sandbox/tool_overlay.py` L270 的 `executable_keys` 加 `"call_timeout_s"`，这样改了超时会触发沙箱里客户端配置的重新同步。

运行时源码一改，`MCP_CLIENT_CODEGEN_VERSION` 会自动变化，热沙箱会重新生成 wrapper（根 `AGENTS.md`），不需要手动处理。

**配置（`agent_config.dalton.yaml`）：**

```yaml
mcp:
  servers:
    - name: "researchdata"        # bundle 已声明的名字：这里只覆盖写出的键
      call_timeout_s: 300         # 默认 300 秒；要让长文读取用满 600 秒，改成 660，并同步桥的 DALTON_BRIDGE_CALLER_BUDGET_S
security:
  max_execution_time: 360         # execute_code 的上限（SecurityConfig 默认 300）；call_timeout_s 调到 660 时，这里改成 720 以上【待验证 YAML 键路径，见 src/ptc_agent/config/agent.py L497-510】
```

- `mcp.servers` 是列表，overlay 里列表整体替换（根 `AGENTS.md` 的 overlay 规则）。上游 `agent_config.yaml` 的 `mcp.servers` 目前是 operator 自己的空列表，所以整体替换没有副作用；合并上游时要复查【待验证 `servers:` 当前内容】。
- 这个覆盖只对 researchdata 生效；`dalton_sec`（stdio）和其他 server 不受影响。
- 超时只用来兜住挂死。真正的上限链条是：桥的生效超时（≤ 调用方预算 − 25）< LangAlpha 单次调用预算 `call_timeout_s` < `execute_code` 的 `max_execution_time`。三者要一起调。

### 验证方式

1. 后端日志或 `GET /api/v1/mcp/...` 的发现状态显示 researchdata 有 6 个工具【待验证具体接口路径】。
2. 在 UI 的 META 工作区问："用内部研究库查最近两周关于 META 2027 年费用指引的卖方观点和专家访谈，给出处。"期望：
   - 能看到 `execute_code` 调用 `research(route="all"|"alphaengine"|"company-wiki"|"guidepoint", ...)`；
   - 来源面板里出现 `mcp:researchdata` 记录；P0-D.3 完成后，还应出现逐条的文档来源；
   - 回答引用了来源身份和日期。
3. 把桥停掉再问一次，期望 agent 收到明确的连接错误，并如实说明"内部库本轮不可用"，而不是编造内容。
4. 超时补丁：在沙箱里执行 `python3 -c "import json;print(json.load(open('.agents/tools/mcp_client_config.json'))['servers']['researchdata'].get('call_timeout_s'))"`，期望 300【待验证配置文件路径】。再发一次 AlphaEngine 长文 `get_document`（耗时超过 65 秒的文档，或用 P0-D.1 的假子进程把耗时拉到 120 秒），期望正常返回，不再在 65 秒处断开。

### 验收标准

- `uv run pytest tests/unit/server/services/test_bundled_plugins.py -q` 通过。新 bundle 不能和已有组件冲突【已核实：该测试会检查磁盘上所有 bundle】。
- 上面第 2 条的对话截图里能看到工具调用和来源面板。
- 后端日志里搜不到 `DALTON_RESEARCHDATA_URL` 的值，也搜不到令牌片段。
- 超过 65 秒、低于 275 秒的 researchdata 调用能正常返回；relay 类 server 的行为不变（`uv run pytest tests/unit/ -q` 里与 MCP 客户端运行时相关的测试全部通过）。
- 4 处 `mcp-call-timeout` 钩子都在 P0 附录 A 的清单里，`check_hooks.sh` 通过。

### 风险与注意

- **后端启动时如果桥没在跑，工具就发现不了。** 发现注册表是否会自动重试【待验证】。不会的话，运维规则是"先起桥、后起后端"，并把这条写进 README。
- 令牌会出现在沙箱的 `.agents/tools/mcp_client_config.json` 里（R1 的已知取舍）。轮换办法：改 `~/.config/dalton/researchdata-bridge.env` 和 `.env`，然后先重启桥、再重启后端。
- 生成的 wrapper 的模块名和导入方式要实测确认，写进 README【待验证】。

**工时：** 1.0 人日（注册 0.5 天，超时补丁和验证 0.5 天）。

---

## P0-D.3 来源显示（P0 降级版：逐篇展开，不加迁移）

**目标：** 每次 researchdata 或 dalton_sec 调用，除了现有的那条 `mcp_tool` 记录，还按结果里的 `_sources` 展开成逐条文档来源，在 SourcesPanel 里显示来源身份、文档 id 和 as-of。**P0 不加迁移**（已决定）：不新增数据库列，as-of 和 publisher 拼进已有的 `title`/`provider` 列，URL 暂不可点。带 URL 和 as-of 两列的完整版放到 P3（P0-D.3b），和 PK.9 的发布数字核验一起做，因为数字核验需要它。

**前置依赖：** P0-D.1 的 `_sources` 契约。可以和 D.1 同时设计。

### 契约：`_sources`（声明式来源，通用于我们自己的 MCP 结果）

```json
"_sources": [
  {
    "document_id": "alphaengine:150001501899344",
    "route": "alphaengine",
    "identity": "broker_report",
    "tier": 3,
    "publisher": "Morgan Stanley",
    "title": "Meta: 2027 opex framework ...",
    "as_of": "2026-10-14",
    "as_of_basis": "publish_time",
    "url": null,
    "private": true,
    "independent": true
  }
]
```

| 字段 | 规则 |
|---|---|
| `identity` | 枚举：`filing`、`company`、`broker_report`、`official_data`、`expert`、`news`、`internal_note`、`derived`、`street_estimate` |
| `tier` | 按蓝图 3.1 的层级：filing 1、company 2、broker_report 3、official_data 4、expert 5、news 6。其余三类取 `null`，不进入层级：`internal_note` 是基金内部笔记，`derived` 是二手摘要、`independent=false`，`street_estimate` 是一致预期，单独分层 |
| `as_of_basis` | `filed`、`publish_time`、`call_date`、`document_date`、`fetched_at`、`observation_date` 之一 |
| 长度上限 | `document_id` 200、`title` 300、`publisher` 120、`url` 2,000 |
| 条数上限 | 每次调用最多 50 条 |

### 改动/新增文件（P0 降级版）

| 路径 | 状态 | 改动 |
|---|---|---|
| `<repo>/scripts/dalton/source_identity.py` | 【新建】 | 各路由的映射函数，见附录 B |
| `<repo>/src/dalton/provenance/declared_sources.py` | 【新建】 | `expand_declared_sources(server, tool, body, tool_call_id) -> list[ProvenanceSource]`。解析 body 开头的 JSON；只对白名单里的 server（`researchdata`、`dalton_sec`）生效；校验 identity 枚举；截断各字段；body 缺失或 JSON 被截断时返回空列表，不抛异常 |
| `<repo>/src/ptc_agent/agent/middleware/provenance/middleware.py` | 钩子 `DALTON-HOOK(provenance-declared)`，2 处各约 2 行 | 在 `_extract_execute_code`（L769-828）和 `_extract_direct_mcp`（L736 起）里，产出 `mcp_tool` 记录之后，再 `yield from expand_declared_sources(...)`。新记录用**已有列**：`source_type="research_doc"`，`identifier=document_id`，`title=f"{title} · {as_of}"`，`detail=identity`，`provider=f"{route}:{publisher}"`，`tool_call_id` 与父记录相同 |
| `<repo>/web/src/types/sse.ts` | 钩子 `DALTON-HOOK(provenance-type)`，1 行 | `ProvenanceSourceType` 加 `'research_doc'` |
| `<repo>/web/src/pages/ChatAgent/components/SourcesPanel.tsx` | 钩子 `DALTON-HOOK(provenance-type)`，约 2 行 | `research_doc` 交给新组件渲染 |
| `<repo>/web/src/pages/ChatAgent/components/dalton/ResearchDocSource.tsx` | 【新建】 | 图标用 BookOpen；徽章显示 identity 的中文标签（一手披露、公司/管理层、券商研报、官方数据、专家访谈、新闻线索、内部笔记、二手摘要、一致预期）；副标题为 `provider`；document_id 可复制；按 `identifier` 去重显示 |
| `web/src/locales/en-US.json`、`zh-CN.json` | 钩子 `DALTON-HOOK(i18n)`（P0 已有） | 加上述标签的中英文 |

`provenance_records.source_type` 是不带 CHECK 的 TEXT（0.4 第 13 行），所以新类型不需要迁移。这些钩子在 P0 落地，补进 P0 附录 A。

### 具体做法要点

- **不信任输入。** `mcp_trace` 由 agent 一侧产生，沙箱代码理论上可以伪造。和现有 trace 一样，所有字段都要截断，identity 只接受白名单值，只对白名单 server 展开。来源记录只作展示和核验线索，不作为授权依据。
- **截断容错。** body 超过 64 KiB 会被截断。`_sources` 放在第一个键，基本能保留。一次执行的 body 总预算是 4 MiB，耗尽后 `result_body` 为空，这时只保留父记录。instruction 里要提醒 agent："一次 execute_code 不要批量拉几十篇全文。"
- **去重。** 同一 `document_id` 在一个 turn 内出现多次时，前端合并显示，后端不去重，因为同一文档被多次读取本身是有用的信息。
- **URL 在 P0 不可点。** wiki 条目的 `source_url`、SEC 归档链接仍在结果正文的 `_sources` 里，agent 引用时可以写出来；面板里可点要等 P0-D.3b。

### 验证方式

1. 真实对话：在 META 工作区跑 P0-D.2 的第 2 条问题。来源面板应出现 `research_doc` 条目，徽章和 as-of 正确。
2. 刷新页面或回放线程后仍然显示，说明这些记录已经落库。
3. 手工构造一个 `_sources` 里 identity 非法的结果，确认它被丢弃。

### 验收标准

- 附录 B 里每个路由都至少有一次真实调用，在面板里显示正确。截图要覆盖 wiki、alphaengine、guidepoint、sec、estimates。
- 没有新增任何迁移；上游 `migrations/versions/` 零改动。
- 稳定之后再写单元测试，锁定两项契约：`declared_sources` 的白名单、截断和容错；`source_identity` 的映射表（用录制的样本 JSON 作为 fixture）。
- `make lint`、`pnpm typecheck`、`uv run pytest tests/unit/ -q` 全部通过。

### 风险与注意

- 改核心的面很小（中间件 2 处、前端类型 1 处、面板 1 处），合并上游时冲突可能出现在 `middleware.py` 和 `SourcesPanel.tsx`。
- `title` 里拼了 as-of，P3 改成独立列之后，旧记录仍是拼接格式；P0-D.3b 的前端要兼容两种格式。

**工时：** 1 人日（后端 0.5 天，前端和联调 0.5 天）。

---

## P0-D.3b 逐篇来源定位完整版（P3 执行，与 PK.9 发布数字核验同期）

**目标：** 给逐篇来源记录加上独立的 `source_url` 和 `as_of`，面板里可点；PK.9 的数字核验据此把发布稿里的数字对回具体文档和日期。

**前置依赖：** P0-D.3；PK.9 的设计（核验要读哪些字段）。

### 改动/新增文件

| 路径 | 状态 | 改动 |
|---|---|---|
| `<repo>/src/dalton/migrations/versions/d004_provenance_locator.py` | 【新建】，Dalton 独立链（P0.8 第 3 节的登记表），`down_revision="d003"` | `ALTER TABLE provenance_records ADD COLUMN IF NOT EXISTS dalton_source_url TEXT, ADD COLUMN IF NOT EXISTS dalton_as_of TEXT;`，可空，不建索引。列名带 `dalton_` 前缀，不会和上游以后的列撞名；不占用上游的线性编号 |
| `<repo>/src/ptc_agent/agent/provenance/types.py` | 钩子 `DALTON-HOOK(provenance-locator)` | `ProvenanceSource` 加可选字段 `source_url: str \| None`、`as_of: str \| None`；`build_provenance_event` 透传 |
| `<repo>/src/server/database/provenance.py` | 钩子 `DALTON-HOOK(provenance-locator)` | `_INSERT_COLUMNS` 加 `dalton_source_url`、`dalton_as_of`，同时检查 bind 数量的上限 |
| `<repo>/src/dalton/provenance/declared_sources.py` | 改 | 填 `source_url`、`as_of`；`title` 不再拼 as-of |
| `<repo>/web/src/types/chat.ts` | 钩子 | `ProvenanceRecord` 加 `source_url?`、`as_of?` |
| `ResearchDocSource.tsx` | 改 | `source_url` 是 http(s) 时可点；副标题改为"publisher · as_of"；兼容 P0 的拼接格式 |

### 验证方式

1. 迁移在空库和已有数据的库上都能 upgrade 和 downgrade（`alembic -c src/dalton/alembic.ini`）。
2. 真实对话后，wiki 条目（frontmatter 的 `source_url`）和 SEC 条目在面板里可以点开。
3. PK.9 的核验用例能从来源记录读到 `as_of` 和 `source_url`。

### 验收标准

- 上游 `migrations/versions/` 零改动；`alembic_version_dalton` 到 d004 及之后。
- P0 时期的旧记录仍能正常显示。

### 风险与注意

- 这是唯一一处改上游表结构的迁移。上游以后若重建 `provenance_records`，要在合并时复查。

**工时：** 1.25 人日。

---

## P0-D.4 基金 wiki：覆盖评估（只读）

**目标：** 证明 researchdata 的 `company-wiki` 路由能满足 fund_wiki 的读取需求，列出缺口。wiki 对本项目**只读**（owner 决定）：不做任何回写，研究发现只放在工作区知识层和 `published/`。

**前置依赖：** P0-D.2。

### wiki 的事实

| 项目 | 情况 |
|---|---|
| 文档根目录 | `$OCW/wiki/`，其中 `companies/` 174 家、`sectors/` 27 至 29 个目录，Markdown 共 1,291 篇，内容文档 1,086 篇 |
| 索引 | `wiki/vectors.db`，FTS5 trigram 加 3072 维 Gemini 向量，混合检索用 RRF（k=60），并做时间衰减（半衰期 540 天，下限 0.5） |
| 内容类型 | Flomo 笔记 561、专家访谈 135、研究笔记 90、管理层会议纪要 88、券商研报 50 等 |
| frontmatter | 只有 `tags`、`related_companies`、`related_sectors`、`source`、`source_url`。日期写在文件名和正文里 |

参考：`$OCW/skills/company-wiki/SKILL.md`、`$OCW/references/company-wiki-system.md`（其中数字已过时）、`$OCW/scripts/research_library.py` L168-245。

### 需求对照

| fund_wiki 需求 | researchdata 的做法 | 结论 |
|---|---|---|
| `search` | `research(route="company-wiki", operation="search", query, start, end, limit, params={company\|sector, tag, search_mode, no_decay, indexed_since})` | ✓ 混合检索；embedding 失败时自动回落到 keyword 并给出 warning |
| `read_page` | `operation="read", document="wiki:<id>", params={offset, max_chars≤30000}` | ✓ 分页，带 `content_sha256` |
| `list_company` | `operation="list", params={company:"META"}`，按日期倒序 | ✓ |
| `related` | 没有接口。`documents.related` 列和 `index.md` 里的"被其他公司/行业提及"都没有暴露出来 | ✗ 缺口 G1 |
| 主题 | `operation="topic"`，按 tag 取 | ✓ |
| 和邮件库联搜 | `route="all"` | ✓ |

### 缺口与处理

| 编号 | 缺口 | 处理 |
|---|---|---|
| G1 | 反向提及："哪些行业或公司的文档提到了 META" | P0 用检索近似：不带 company 过滤，直接用 ticker 和别名检索。P2 如果真实使用证明需要，就在桥里加一个桥自有的只读工具 `wiki_related(company, limit)`，按 `json_each(documents.related)` 只读查 `vectors.db`，用 `mode=ro`。不改 researchdata |
| G2 | 回写 | **不做**。wiki 对本项目只读；发现留在工作区知识层和 `published/` |
| G3 | `wiki.db` 是空文件 | 忽略。在蓝图和 dev-guide 里更正说法 |
| G4 | 索引不记录向量是哪个 embedding 模型生成的 | 只记录这个风险，不处理（OpenClaw 侧的问题） |
| G5 | 没有合规字段 | 不需要：合规口径已定（专家访谈、管理层交流、Guidepoint 都可用，标专家身份），写进方法论，见 P1.1 |
| G6 | frontmatter 没有日期 | `_sources.as_of` 取 research_library 的 `date` 字段，`as_of_basis=document_date` |

**验证方式：** 在 META 工作区真实跑一遍上表 6 种用法，记录结果条数和耗时。G1 用"不过滤 company、检索 META"做近似，记录召回效果，供 P2 决定是否实现 `wiki_related`。

**验收标准：**
- 上表每一行都有真实调用记录。
- 桥和 dalton_data bundle 里没有任何写 wiki 的路径（code review 检查）。

**风险：** 稠密检索是对约 117 MB 向量做 O(N) 暴力计算，单次延迟【待验证，按秒级估计】。记录实测延迟；它在桥的默认超时（240 秒）以内即可，超时只用来兜住挂死。

**工时：** 0.25 人日。

---

## P0-D.5 SEC：复用自带工具、补齐缺口（新建 `dalton_sec`）

**目标：** 满足基金在美股披露上的三项需求：
1. 任意历史一期的 10-Q 或 10-K 条目正文；
2. XBRL company facts 的时间序列，**每个数据点带单位、口径、期间和 accession**；
3. 财年期间的正确匹配，存在歧义时保留歧义。

### 现状与缺口

| 需求 | LangAlpha `get_sec_filing`（direct） | researchdata `finance/sec`（宿主机 edgartools） | 缺口 |
|---|---|---|---|
| 最新 10-K/10-Q 条目和三表 | ✓ 只取最新一期（`edgartools_parser.py` L83-91）。10-K 默认 item 1、1A、7、8；10-Q 默认 part1 item2、part2 item1A | ✓ | — |
| 8-K（近 90 天，含新闻稿） | ✓ | ✓ `eight-k` | — |
| 历史某一期的条目 | ✗ | `read-item` 加 `as_of`（按 filing_date 过滤）。能否精确指定 period 或 accession【待验证】 | 可能缺"按 accession 指定" |
| XBRL 序列，逐点带单位 | ✗ | `facts`（concept 必填）、`smart-facts`、`search-concepts`。`financials` **不带单位**（旧 Dalton 的教训） | 缺统一的期间解析、时长校验、歧义和重述的保留 |
| 财年标签（如 MSFT FY2026Q1）映射到日期 | ✗ | ✗ | 缺 |
| 在沙箱代码里调用 | ✗（direct 工具） | ✓ 经桥 | — |
| 20-F、6-K、13F、Form 4、DEF 14A | ✗ | `insider` 部分覆盖 | 按需处理，P6 |

### 决定

- **阅读条目正文：** P0 先用 researchdata 的 `read-item` 加 `as_of`，以及 `list-filings`。只有实测证明无法按期间或 accession 精确定位时，才在 `dalton_sec` 里加 `get_filing_item`。
- **XBRL 序列和期间：** 新建 `dalton_sec`，沙箱 stdio MCP。它访问 SEC 的公网 API，不需要凭证，只需要一个 User-Agent 身份，代码完全由我们控制，可以按契约测试。

### 改动/新增文件

| 路径 | 状态 |
|---|---|
| `<repo>/plugins/dalton_data/dalton_sec_mcp_server.py` | 【新建】。**单文件自包含**，因为沙箱只复制入口文件（0.4 第 5 行）。文件名在所有 bundle 中唯一 |
| `<repo>/plugins/dalton_data/mcp.json` | 加 `dalton_sec`，见 P0-D.2 |
| `<repo>/plugins/dalton_data/plugin.json` | 加 `dalton_sec` 的 `description` 和 `instruction`，`tool_exposure_mode: "detailed"` |
| `<repo>/.env` | 加 `SEC_USER_AGENT`【owner】。格式是 "机构名 联系邮箱"，属于公开身份，不是秘密。注意：它会作为沙箱级环境变量注入（0.4 第 7 行的同类机制） |
| `<repo>/tests/unit/mcp_servers/test_dalton_sec_periods.py` | 【新建】，契约稳定后再写 |
| `<repo>/tests/unit/mcp_servers/fixtures/dalton_sec/*.json` | 【新建】，录制的 companyfacts 样本：META、MSFT（FYE 06-30）、AMZN，以及一个有重述的样本 |

### 工具契约

docstring 按 `src/tools/AGENTS.md` 和 `mcp_servers/AGENT_CONTRACT.md` 写：Returns 写完整结构，处理建议放在 Args 之前，800 字符以内。

**1. `get_company_facts(ticker, concepts, periods=None, forms=["10-Q","10-K"], include_other_versions=True) -> _OUT_FACTS`**

- 数据源：
  - `https://www.sec.gov/files/company_tickers.json`，用来把 ticker 映射到 CIK，进程内缓存 24 小时；
  - `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`；
  - `https://data.sec.gov/submissions/CIK##########.json`，取 `fiscalYearEnd` 和 filings 索引。
- 限速：不超过 10 次/秒，同时带 `User-Agent: $SEC_USER_AGENT`。
- 返回结构用 `mcp_servers/_envelope.py` 的 `make_response`：

```json
{
  "symbol": "MSFT", "count": 3, "source": "sec_xbrl_companyfacts",
  "data": [
    {"concept": "us-gaap:Revenues", "basis": "us-gaap", "label": "Revenues",
     "request": "FY2026Q1", "period_start": "2025-07-01", "period_end": "2025-09-30",
     "duration": "3m", "value": 77673000000, "unit": "USD", "unit_source": "issuer_xbrl_fact",
     "accession": "0000950170-25-xxxxx", "form": "10-Q", "filed": "2025-10-29", "frame": "CY2025Q3",
     "layer": "filing_actual", "other_versions": [{"accession": "...", "value": 77600000000, "filed": "..."}],
     "diagnostics": []}
  ],
  "fiscal_calendar": {"fye_mmdd": "06-30", "source": "submissions.fiscalYearEnd", "conflicts": []},
  "unresolved": [{"request": "FY2026Q4", "reason": "not_yet_filed"}],
  "ambiguities": [],
  "as_of": "2026-10-18T10:00:00Z",
  "_sources": [{"document_id": "sec:0000950170-25-xxxxx", "identity": "filing", "tier": 1,
                "publisher": "SEC EDGAR", "title": "MSFT 10-Q 2025-09-30", "as_of": "2025-10-29",
                "as_of_basis": "filed", "url": "https://www.sec.gov/Archives/edgar/data/789019/...", "private": false, "independent": true}]
}
```
- 示例里的数值只用来说明结构，不是事实。

规则全部来自旧 Dalton 的教训：
- `research_sources.py` 的 `_sec_unit_evidence`（L2661-2729）、`_bind_sec_fact_units`（L3007-3138）、`_sec_fact_duration`（L2982）；
- `series_periods.py`（L28-167）；
- `document_periods.py`（L471-530）。

| 规则 | 内容 |
|---|---|
| R1 单位来源 | 单位**只取发行人自己的 fact**。companyfacts 本来就按单位分组，每个点的单位都是显式的。**绝不默认 USD**。没有单位就不返回这个值，记 `unit_evidence_gap` |
| R2 时长判定 | 由 start 和 end 计算：80–100 天为 3m，170–195 天为 6m，260–285 天为 9m，350–380 天为 12m，1 天以内为 instant，其余为 other。没有 start 时用 frame 的正则 `^CY(\d{4})(Q[1-4])?(I)?$` 推断 |
| R3 期间标签 | 解析 `Q1-4`、`H1`、`H2`、`9M`、`FY` 加年份，前后顺序都可以，"FY" 前缀可有可无；也接受 ISO 日期。Q* 只匹配 3m，**绝不拿 YTD 充当单季**；H1 和 9M 匹配 6m 和 9m；FY 匹配 12m |
| R4 派生期间 | Q4 等于 FY 减 9M，H2 等于 FY 减 H1。两者都要同一单位、同一 concept、来自同一组 filing，标 `layer:"derived_filing_actual"`，并带两个 accession。不满足条件就进 `unresolved` |
| R5 财年日历 | 优先用 submissions 的 `fiscalYearEnd`；其次用 FY 点的 end 日期，要求唯一。两者不一致时写进 `fiscal_calendar.conflicts`，**不猜**。财年按其结束时所在的日历年命名；1 月和 2 月结束的财年，命名有歧义，必须显式给 `fiscal_year_end`【规则来自 `document_periods.py` L487-502】 |
| R6 fy 和 fp 字段 | companyfacts 里每个 fact 的 `fy` 和 `fp` 描述的是**所在的那份 filing**，不是这个数据点的期间。比较期的数据点必须按日期判断，不能按 fy 和 fp |
| R7 重述与多版本 | 同一 concept、期间和时长有多个值时，`value` 取最新 filed 的那个，其余放进 `other_versions`，并在 diagnostics 里记 `fact_value_differs_in_other_filing`。如果数据点所在 filing 本身给出的值与采用值不同，记 `fact_version_conflict`，**不合并** |
| R8 单位冲突 | 同一个点有多个候选单位时，不返回值，在 `ambiguities` 里记 `{reason:"unit_ambiguity", units, candidates}`。候选按（unit, accession, start, frame, form, value）排序，保证结果确定 |
| R9 维度 | companyfacts 只含无维度的 fact，所以分部数据拿不到。返回 `diagnostics:["segments_not_in_companyfacts"]`，提示改用条目正文或 `facts` 的维度查询 |
| R10 口径 | `basis` 只从命名空间取，可以是 `us-gaap`、`ifrs-full`、`dei` 或公司扩展前缀。**不推断 GAAP 和 non-GAAP 的属性** |

**2. `list_filings(ticker, forms=None, since=None, until=None, limit=20) -> _OUT_FILINGS`**

基于 submissions API，每条 filing 返回 `{accession, form, filed, period_of_report, primary_doc_url, items (8-K), is_amendment}`，并附带 `_sources`。

**3. 条件性工具 `get_filing_item(ticker, form, period|accession, item, offset=0, max_chars=20000)`**

只在 researchdata 的 `read-item` 被证明不够用时才做。实现时先确认沙箱镜像里是否装了 edgartools【待验证 `Dockerfile.sandbox` 的依赖】。

**错误信封：** 用 `make_error`，错误码取 `invalid_argument`、`not_found`、`rate_limited`、`upstream_error`。错误结果不入来源账（0.4 第 12 行），这符合预期。

### 验证方式（真实调用）

| # | 用例 | 期望 |
|---|---|---|
| 1 | META `us-gaap:Revenues`，`2025Q2` 和 `H1 2025` | Q2 为 3m，H1 为 6m，两者不混；与 10-Q 正面表格手工对数一致 |
| 2 | MSFT（FYE 06-30）的 `FY2026Q1` | period_end 为 2025-09-30；`fiscal_calendar.fye_mmdd="06-30"` |
| 3 | AMZN 的 `2025Q4` | 给出 `derived_filing_actual`，值等于 FY 减 9M，带两个 accession |
| 4 | 选一个有重述的 concept（GOOGL 或其他，实施者自己找） | `other_versions` 非空，diagnostics 正确 |
| 5 | 不存在的 concept | 返回 `not_found` 或 `unresolved`，不报错崩溃 |
| 6 | 通过 LangAlpha 对话问"MSFT 最近 8 个财季的收入和营业利润，标财年期间" | 走 `dalton_sec`，来源面板出现 `filing` 徽章和 SEC 链接 |

### 验收标准

- 上表 6 个用例都有真实调用记录，用例 1 至 3 附人工对数截图。
- 稳定之后：
  - 把 `"dalton_sec": "plugins.dalton_data.dalton_sec_mcp_server"` 加进 `tests/unit/mcp_servers/test_agent_contract.py` 的 `_SERVERS`；
  - 运行 `uv run python scripts/utils/update_agent_docstring_lock.py` 生成锁定，这是锁文件唯一的写入方式；
  - 加进 `test_entrypoint_imports.py` 的 `_ENTRYPOINT_SERVERS`，入口文件模块级不能 import `src.`。
- `test_dalton_sec_periods.py` 用 fixture 锁定 R2 至 R8。

### 风险与注意

- SEC 公平访问政策：必须带 UA，并限速到每秒 10 次以内，否则会被封 IP。
- 沙箱用的是 docker 的 bridge 网络，可以访问公网【待验证 `data.sec.gov` 是否可达】。
- 外国私人发行人（20-F）只有半年或年度数据。遇到时返回 `missing_quarterly_disclosure_gap`，**不能写成"公司未披露"**。

**工时：** 2.5 人日（server 1.5 天，验证 0.5 天，测试和锁定 0.5 天）。

---

## P0-D.6 价格与一致预期

**目标：** 明确价格和一致预期各用哪个工具、按什么口径标注、怎么积累历史。P0 不写新代码，只做验证并定下规则。

### 可用工具

| 用途 | 首选 | 备选 | 说明 |
|---|---|---|---|
| 日线、报价 | **yfinance**：`yf_price.get_stock_history` 等（已决定为默认） | LangAlpha 自带 `get_daily_prices`、`get_quote`（direct），MCP `price_data` | 后者属于 FMP 系列，需要 `FMP_API_KEY`；本阶段不配，FMP 以后可选。港股、A 股价格放到 P6 |
| 一致预期快照（完整包） | researchdata `finance(route="estimates", ticker=T)`，在宿主机跑 `consensus_estimates.py` | — | 包含 EPS 和收入（0q、+1q、0y、+1y）、EPS 趋势（7、30、90 天）、修正次数、目标价、评级、近 8 期财报日期、价格位置、`implied_model`、近 4 次财报后次日涨跌 |
| 一致预期（细项） | `yf_analysis` 下的 `get_earnings_estimates`、`get_revenue_estimates`、`get_growth_estimates`、`get_analyst_price_targets` 等 | `get_company_overview`（FMP 分析师数据） | — |
| 估值历史 | MCP `fundamentals.get_historical_valuation` | `yf_fundamentals.compare_valuations` | 用于"历史上是 EPS 驱动还是倍数驱动"的分析（P2） |

### 口径规则（写进 P1.1 的方法论和 instruction）

1. 一致预期一律标为 `street_estimate`，写明来源（"Yahoo Finance via yfinance"）、`fetched_at`、期间键和分析师人数。
2. **Yahoo 没有历史，也没有 as-of。** 修正历史只能靠自己存快照：每次取数都写到公司工作区的 `data/consensus/<YYYY-MM-DD_HHMM>.json`，内容是原始 JSON 加 `_meta{route, fetched_at, source}`。P4 的夜间自动任务会定时写快照；P1.4 把旧 stock-tracker 的快照迁进来，作为历史的起点。
3. `implied_model` 是**模型推导**，不是一致预期，标 `model-derived`。它的税率默认取最近一季（脚本 help 里写的是全年，与代码不一致），而且 gateway 只透传 ticker，无法覆盖股本和税率。
4. estimates 里的价格是未复权的 3 个月窗口。做收益计算时以 yfinance 的复权价格为准，并统一基准日，见 `research-conventions/references/market-data-rules.md`。
5. 一致预期不能自动覆盖 house case（蓝图 3.1 第 2 条）。

### 验证方式

对 META、MSFT、AMZN 各取一次 estimates 和 `yf_analysis.get_earnings_estimates`，比较两者的期间键和数值是否一致；再用 `yf_price.get_stock_history` 取一次 3 个月日线，确认价格默认走 yfinance。

### 验收标准

- 验证记录完整。
- 快照文件的 schema 写进 P1.3 的模板，即 `data/consensus/README.md`。

### 风险与注意

- yfinance 偶尔失败或限流，consensus_estimates 会逐段返回 `{"error":...}`。agent 必须把它当作"本轮未取得"处理。

**工时：** 0.5 人日。

---

## P0-D.7 推迟到 P6 的数据源（只列清单）

本阶段只做美股（已决定）。港股和 A 股的数据深度、价格和公告监控全部放到 P6。

| 数据 | 现状 | P6 的方向 |
|---|---|---|
| 港交所披露易（HKEX）公告 | researchdata 没有专门的路由。旧 Dalton 有接入经验，位置在 `dalton-batch-cnhk` 的 cn-hk 开发线【待验证具体模块】 | 新建公告 MCP，并接入 P4 的公告监控 |
| 巨潮（cninfo，A 股） | researchdata 的 `cn-hk` 路由（cninfo 和 AkShare，`filings`、`ask`）**P0 已经可以调用，但不做验收** | P6 评估是直接复用，还是新建一个带来源身份的接口 |
| 雪球 | researchdata 的 `xueqiu` 是 handoff 路由，桥已经过滤掉 | 新建接口 |
| X | LangAlpha `alternative_data.x_api`（需要在 vault 配置 `X_BEARER_TOKEN`）。researchdata 的 x 路由都是 handoff | P6 配置 vault 后直接使用 |

---

## P0-D.8 P0-D 验收清单

| # | 检查项 | 证据 |
|---|---|---|
| 1 | 沙箱容器和后端容器都能访问宿主机桥 | P0-D.0 的命令输出 |
| 2 | 桥由 launchd 管理，重启后自动恢复；`/healthz` 正常；令牌错误返回 404 | `check_bridge.sh` 的输出 |
| 3 | LangAlpha 发现了 researchdata 的 6 个工具和 dalton_sec 的 2 个工具 | 日志或接口截图 |
| 4 | 在 META 工作区问一个问题，能看到 agent 调用内部研究库（alphaengine、guidepoint、sales-notes）和 wiki 作答，来源面板逐条显示来源身份和 as-of（可点链接在 P3 的 P0-D.3b） | UI 截图 |
| 4b | 一个耗时超过 65 秒的长文读取正常返回（超时补丁生效） | P0-D.2 验证第 4 条的记录 |
| 5 | SEC 的 6 个用例对数正确 | P0-D.5 的记录 |
| 6 | 一致预期取数与口径标注正确 | P0-D.6 的记录 |
| 7 | 日志、`.env` 以外的文件和 PR 里都没有凭证值 | `git grep` 加日志 grep |
| 8 | 单元测试只覆盖已稳定的契约，并且全部通过 | `uv run pytest tests/unit/ -q` |
| 9 | 费用计入日额度 | 属于另一份指南（本地预算）的验收项，本文不重复 |

---

# P1 入职

## P1.1 `dalton-conventions`：基金方法论 skill

**目标：** 把 Playbook v1、培训手册精粹 4 篇和 OpenClaw skill 里的方法纪律，合并成一个 skill，让所有 Dalton skill 都先读它。它和 LangAlpha 自带的 `research-conventions` 分工明确、不冲突。

**前置依赖：** 无，可以和 P0-D 并行。正文里引用的 researchdata 路由名，以 P0-D.2 为准。

### 改动/新增文件

| 路径 | 状态 | 内容 |
|---|---|---|
| `<repo>/plugins/dalton/plugin.json` | 【新建】 | bundle 元数据，不声明 servers。`mcp.json` 可以没有，`langalpha_research` 就只有 plugin.json 和 skills【已核实】 |
| `<repo>/plugins/dalton/skills/dalton-conventions/SKILL.md` | 【新建】 | 总纲，不超过 200 行 |
| `<repo>/plugins/dalton/skills/dalton-conventions/references/method.md` | 【新建】 | 研究流程与关键问题 |
| `.../references/evidence-and-sources.md` | 【新建】 | 来源层级、身份、去重、合规 |
| `.../references/model-standards.md` | 【新建】 | 模型期间与分层 |
| `.../references/risk-reward.md` | 【新建】 | 风险回报与仓位建议 |
| `.../references/event-verdicts.md` | 【新建】 | 事件五词结论与跟踪 |
| `.../references/writing.md` | 【新建】 | 中文交付规范 |
| `.../references/analyst-levels.md` | 【新建】 | 分析师等级与成长 |
| `.../references/SOURCES.md` | 【新建】 | 每条规则的出处（路径、JSON 路径、行号），给维护者看，agent 不必读 |
| `<repo>/src/ptc_agent/agent/middleware/skills/dalton_registry.py` | 【新建】 | `DALTON_SKILL_DEFINITIONS: dict[str, SkillDefinition]` |
| `<repo>/src/ptc_agent/agent/middleware/skills/registry.py` | 钩子 `DALTON-HOOK(skills-registry)`，2 至 3 行（列入 P0 附录 A） | 在 `SKILL_REGISTRY` 定义之后合并：`from .dalton_registry import DALTON_SKILL_DEFINITIONS; SKILL_REGISTRY.update(DALTON_SKILL_DEFINITIONS)`。要避免循环 import：`dalton_registry` 只 import `SkillDefinition`，合并语句放在文件末尾 |
| `<repo>/tests/unit/middleware/skills/test_shared_skill_layer.py` | 改 | 照 research-conventions 的写法，加 dalton-conventions 的依赖检查。这是已经稳定的契约，可以写测试 |

### SKILL.md 的精确提纲

```markdown
---
name: dalton-conventions
description: "Fund methodology for Dalton (fundamental long-biased hedge fund): research questions by coverage stage, source hierarchy and identity, expert-call compliance, model period standards, risk/reward standards, five-word event verdicts, Chinese delivery. Read before any Dalton deliverable or knowledge-file write."
---
# Dalton 基金方法论

1. 角色与边界（≤10 行）
   - 新入职分析师；fundamental long-biased，但不强迫看多；没有足够预期差就说"暂无高 conviction"
   - Dalton 只提研究观点和仓位建议；正式观点和交易由 PM 决定
2. 与 research-conventions 的关系（下面的优先级表）
3. 八条底线（照抄蓝图 3.1，每条一行）
4. 按任务找参考（表）
   初次覆盖 → method.md §初筛四问 + §深度认知 12 问
   完整覆盖 / memo → method.md §12 Key Questions + §Good Business/People/Price + risk-reward.md
   事件 / 日常跟踪 → event-verdicts.md
   建模 → model-standards.md
   引用与来源 → evidence-and-sources.md
   成稿 → writing.md
5. 合规口径（原文写入，不缩写）
6. 数据入口速查（researchdata 路由、dalton_sec、价格和一致预期各一行；细节在 plugin instruction 里）
7. 公司知识层读写约定（只指向 PK 文档定义的文件和 schema，不在这里重复）；基金 wiki 只读，发现写进知识层
```

**第 2 节的优先级表（写进 SKILL.md）：**

| 主题 | research-conventions（自带） | dalton-conventions | 冲突时 |
|---|---|---|---|
| 证据标签 | 七类：`fact`、`company claim`、`street estimate`、`model-derived`、`judgement`、`assumption`、`needs-source` | 沿用；另外把模型分层（Filing actual、Derived、推算、假设、情景）映射到这七类 | 共用 |
| 来源层级 | 按事项类别排序 | 基金 6 级层级（含专家一级），加来源身份和来源链去重 | **dalton 优先** |
| readiness posture | 五级阶梯 | 沿用。草稿可以是任何一级；正式发布至少要 `review-ready`，并通过 P3 的核验 | 共用；发布门槛由 P3 规定 |
| 行动词汇 | add、press、hold、trim 等 | 仓位建议沿用自带词汇；事件判断改用五词结论 | 事件用 dalton，仓位用自带 |
| 篇幅与深度 | 三档深度，带字数参考 | 去掉机械门槛（蓝图 3.2），深度只作提示 | **dalton 优先** |
| 语言 | 英文 | 中文交付，保留英文术语 | **dalton 优先** |

**第 5 节的合规口径原文**，来自 `bullbear-discussion/references/common_task.md` L139，以及 `$OCW/memory/2026-10-15.md` L102 和 L128 的记录：

> 专家访谈、管理层交流（包括转入基金 wiki 的材料）以及 Guidepoint 的检索结果，都已经过用户的合规流程，可以使用（2026-10-15 确认，2026-10-18 owner 再次确认范围包括 Guidepoint）。这包括以公司在职员工口吻发言的专家，以及高管小范围交流。照常使用，不要自行以"重大非公开信息"为由排除。
>
> 引用时写日期和专家身份（写身份，不写姓名），并注明可靠性（已证实、被反驳、互相矛盾）。专家给出的经营数字，标为"单一来源估计"，与公司披露和卖方数据分开写。
>
> Guidepoint 原文引用不超过 20 个词，并附 `source_attribution`；不得用于 KYC 或合规筛查。

适用范围已定：wiki 里的专家访谈和管理层交流、AlphaEngine 里的专家和调研纪要、Guidepoint 检索结果，一律可用并标专家身份。

### 各 reference 的提纲与合并来源

| 文件 | 精确提纲 | 合并来源（只取方法，剥掉渠道和人名） |
|---|---|---|
| `method.md` | §1 三段式研究流程：Initial Screen、Investment Memo、Active Coverage，不设固定阶段关卡（蓝图 3.2）<br>§2 初筛四问，以及初筛 8 节结构<br>§3 深度认知 12 问，每问要有判断、证据、反方证据、未知项、falsifier 和下一项验证<br>§4 完整覆盖 12 个 Key Questions，以及 memo 的 12 段结构<br>§5 Good Business、Good People、Good Price，商业质量和股票吸引力分开判断<br>§6 Variant Perception 三问（市场认为、实际上、为什么错误定价还在）<br>§7 anti-thesis 是完整的反向观点，不是风险清单；pre-mortem 做 3 至 5 条，不立稻草人<br>§8 零基思考："如果今天重新开仓，还买不买"<br>§9 Driver Tree → 关键变量 → 市场隐含假设 → 差异化观点 → 三情景 → 催化剂与证伪 | Playbook：`stages[0..5]`、`key_questions`、`deliverable_templates`<br>`workspace-chem/references/manual/research-process.md` 和 `memo-template.md`<br>`company-deepdive-mx-0911/SKILL.md` L11-73<br>`writing_guide.md` L78-89 |
| `evidence-and-sources.md` | §1 6 级来源层级（filing、公司公告、署名研报、官方或协会数据、专家（标身份）、新闻（只作线索）），禁用来源：百度百科<br>§2 来源身份与 researchdata 的 `_sources.identity` 对照表（附录 B）<br>§3 来源链去重：同一份研报被邮件、TMTB、FUNDA AI、X 转述，只算一条；只有一家来源时标"单源观点"；点赞和转发不算共识<br>§4 关键数字至少要两个独立来源<br>§5 买方门槛与各种身份的区分：公司指引、卖方一致预期、具体机构预测、明确的买方门槛、作者判断。分歧数字不能平均成"共识"<br>§6 电话会提问口径：分析师说的数字若管理层没有确认，标"分析师提问口径，管理层未确认"<br>§7 "本轮未取得"不等于"公司未披露"；最终的"未找到"只描述本轮已覆盖的范围<br>§8 数字底表：目标公司的数字只取底表；拿不到就写"未取得"，不用相近的数字顶替<br>§9 合规口径（与 SKILL.md 第 5 节相同）<br>§10 原文引用上限：Guidepoint 不超过 20 个词，其他来源不超过 3 句 | Playbook `evidence_discipline`<br>`company-preview/SKILL.md` L33-57，`retrieval.md` L5、L50、L54<br>`bullbear-discussion/SKILL.md` L73、L76，`common_task.md` L33-47、L136-154<br>`deepdive common_rules.md` L13-18、L33<br>`execution.md` L66-68 |
| `model-standards.md` | §1 期间标准：年度历史从 2018A 起，单季历史从 2024Q1A 起，年度预测滚动 3 年，季度预测到次年 Q4；覆盖期只能扩展，不能缩短<br>§2 历史硬编码只取一手 filing，并建单元格级来源台账（文件、期间、页码、URL、单位、口径）<br>§3 Q2、Q3、Q4 由累计数推导（H1−Q1、9M−H1、FY−9M），标"Derived filing actual"<br>§4 分层：Filing actual、Derived、推算、假设、情景；不得把残差或按比例分摊的数冒充披露数<br>§5 假设为空就留空，不填 0<br>§6 每次修订记录旧值、新值、原因和受影响的 thesis；实际数推翻预测时写一条对账记录<br>§7 财务镜头的定义：净现金两种口径、隐含资产、股东回报 R1/R2/R3、盈利质量（Sloan Ratio、CFO/NI、CCC）。计算由 P2 的 `dalton-financial-lenses` 负责<br>§8 期间命名和单位规则，与 dalton_sec 的 R1 至 R10 一致 | Playbook `model_discipline`<br>`bullbear return_task.md` §1.1、§3、§4<br>`deepdive execution.md` L54-64<br>本文 P0-D.5 |
| `risk-reward.md` | §1 回报标准：长期 compounder 3 至 5 年 3 至 5 倍；周期反转加催化剂，看对的上行要能补偿看错的损失和波动；价值股 6 至 12 个月 50% 上行；做空 3 至 6 个月 30% 下行，并写明时间跨度<br>§2 三情景估值与 IRR<br>§3 仓位建议词汇（沿用 research-conventions 的 judgment.md），以及对冲<br>§4 "暂无高 conviction"是合法结论<br>§5 PM 决定交易 | Playbook `risk_reward_standards`<br>`deepdive SKILL.md` L55-59<br>`research-process.md` L25 |
| `event-verdicts.md` | §1 五词结论，**使用 Playbook 的正式词形**：`NO_CHANGE`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`、`NEW_THESIS`，JSON 和数据库里一律写正式词形，md 和界面显示中文短标签：无变化、强化、削弱、证伪、新论点（已决定）。蓝图里的简写 STRENGTHENED、WEAKENED 等不再使用<br>§2 每个事件必须记：事件、对应的 driver 或 thesis 支柱、结论、证据、下一项验证及其日期<br>§3 验证之后要问：是否已经 priced in，催化剂是否用尽<br>§4 判断错误时，归类为事实、时机、传导、估值或执行，并写入 lessons<br>§5 日报只写重要变化；无变化就不推送<br>§6 股价异动先量化：用同组公司和板块 ETF 分清是板块 beta 还是个股 alpha；再按 filing、新闻、卖方、社媒的顺序找原因 | Playbook `stages[5]`、`decision_vocabulary`<br>`stock-move-analyzer/SKILL.md`（`$OCW/skills/`） |
| `writing.md` | §1 判断先行：开头不超过 3 句核心判断；每个要点按"判断 → 证据 → 对收入、利润、现金或估值的影响 → 改判条件"组织<br>§2 把"不是、不能、不代表"这类警告改写成正确口径，再写投资含义；限制条件集中写一次<br>§3 引用格式："（来源身份 [引用N]）"，一句一个来源；每个数字标期间（2Q26、1H26、FY2025、TTM）；`[假设]` 和引用分开<br>§4 中文交付，保留英文术语：主谓清楚，少用被动，不堆黑话，不凑三点，不写奖状式开场；禁用"赋能、闭环、抓手、呈现出、体现了"<br>§5 数字写法："Capex 相当于净利润的 x%"，不写"占净利润"；区间写"至"，不用"~"；金额写 USD 或美元，不用 `$`（LangAlpha 的 Markdown 是否把 `$` 当公式渲染【待验证】）<br>§6 一句话收尾："The stock works if X; it breaks if Y; the key debate is Z" | `bullbear common_task.md` §0.1 L15-24<br>`SKILL.md` L13、L56<br>`deepdive writing_guide.md` L5-13、L103<br>`common_rules.md` L56 |
| `analyst-levels.md` | 从 Basic L1 到 L4，再到 Advanced L1 至 L4 的标准；Dalton 当前处于哪一级、目标是哪一级；Advanced 级的验收看 variant view 命中率 | Playbook `analyst_levels`，`manual/analyst-levels.md` |

**迁移时要剥掉的内容**（对应蓝图 4.5）：
- 飞书分块写入、Discord 排版、25 分钟上限和 12 分钟搜索限制；
- 写死的模型名，例如 `openai/gpt-6-astra`、"Fable 5"；
- 人名和账号，例如"为 Mark 研究"、Richard 的日历；
- 渠道细节：Gmail、xreach、qt.gtimg.cn、东财；
- 行数下限，例如 700 行或 350 行；
- 化工专属内容，例如价差周报。

### 注册与 `requires`

`dalton_registry.py` 的条目示例：

```python
DALTON_SKILL_DEFINITIONS = {
    "dalton-conventions": SkillDefinition(
        name="dalton-conventions",
        description="<必须与 SKILL.md frontmatter 的 description 逐字相同>",
        skill_md_path="skills/dalton-conventions/SKILL.md",
        requires=("research-conventions",),
        # 不设 command：和 research-conventions 一样只供阅读，不做斜杠命令
    ),
    # P2 起的 Dalton skill 都写成下面这样：
    # "dalton-bullbear": SkillDefinition(..., command="bullbear",
    #     requires=("dalton-conventions", "research-conventions")),
}
```

规则：
1. 每个 Dalton skill 的 SKILL.md 正文前 12 行内，必须有这一行：`Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.` 同时它要在 registry 里同时 `requires` dalton-conventions 和 research-conventions。两个都写，是为了不依赖 `resolve_disabled_skills` 是否传递依赖【已核实 L435-453 只处理直接声明的依赖】。
2. `test_shared_skill_layer.py` 增加一项检查：凡是 `.md` 里引用了 `.agents/skills/dalton-conventions/` 的 bundled skill，都必须在 registry 里声明 `requires`。
3. 不注册的 skill 会以"Your skills（用户内容）"出现，失去平台身份和 `requires` 保护。所以**必须注册**。

### 用户层记忆：基金 mandate

记忆没有写接口（0.4 第 16 行），所以用一次"入职对话"写入。在 Home 里请 Chief of Staff 执行下面这段话，并把这段话固定写进 P1 的操作手册：

> 读 `.agents/skills/dalton-conventions/SKILL.md`，把基金的 investor mandate 写成用户层记忆 `.agents/user/memory/fund_mandate.md`（type: user）。内容包括：长仓导向、不强迫看多、PM 决定交易、风险回报标准引用 risk-reward.md、中文交付、覆盖名单的位置（见 P1.2）、基金 wiki 只读、回答 PM 的问题先读 `.agents/skills/dalton-pm-qa/SKILL.md`（PS.16）、分析师可以直接新建和修改自己的 `dalton-*` 自动任务文件，不必先问（PK.15）。只写规则和指针，不写数据和名单。

验证：`GET /api/v1/memory/user` 和 `GET /api/v1/memory/user/read?key=fund_mandate.md` 能读到这个文件，`memory.md` 索引里也有对应的一行。

### 验证方式

1. 重启后，任意工作区的 skill 清单里，dalton-conventions 显示为平台 skill，而不是出现在"Your skills"下。可以用 `uv run python scripts/utils/test_skill_discovery.py` 辅助检查【待验证用法】。
2. 在 META 工作区问："按基金方法论，初次覆盖要回答哪些问题？专家访谈能不能用？"期望：
   - agent 读取了 dalton-conventions，UI 里能看到 Read 动作；
   - 回答里的初筛四问、深度 12 问与 Playbook 一致；
   - 合规口径与原文一致。
3. 关掉 research-conventions 之后，它仍然留在沙箱里，因为被 dalton-conventions 依赖。

### 验收标准

- SKILL.md 不超过 200 行；每个 reference 不超过 200 行。
- `SOURCES.md` 给每条规则写明出处。
- 抽查 10 条规则，原文与来源一致，没有人名、渠道细节或写死的模型名残留。
- `uv run pytest tests/unit/middleware/skills/ -q` 通过。
- 上面验证第 2 条的对话截图。

### 风险与注意

- 改了 `registry.py`。合并上游时这个文件可能冲突，但改动只有 2 至 3 行，冲突面很小。
- registry 的 description 与 frontmatter 不一致时，PTC 发现会和沙箱清单脱节【已核实，代码注释里有说明】。
- 方法论的篇幅要克制：SKILL.md 只放索引和底线，细节放进 references 按需读取，避免每个 turn 都消耗大量上下文。

**工时：** 2.5 人日（提炼和写作 1.5 天，注册、测试和验证 1 天）。

---

## P1.2 覆盖名单（Core 27 / Watch 78）

**目标：** 生成唯一的规范名单 `coverage.yaml`。它要能追溯到 OpenClaw 的原始来源，并且消除来源之间的不一致。Dalton 在 Home 和各公司工作区都能读到它。

**前置依赖：** 无。

### 来源（全部只读）

| 来源 | 路径 | 能提供什么 |
|---|---|---|
| 主映射 | `$OCW/skills/coverage-update-richard/scripts/screen_movers.py` | `TICKERS`（L34-374，324 条）：`name -> (ticker, peer_group, sheet_section, tier)`，tier 为 Core、Watch 或空（只作同组）。`EXTRA_PEERS`（L380-397）给出同组 ETF。原表是 2026-10-09 的 "Weekly Risk Reward (Richard)" |
| Core 检索名 | `$OCW/skills/coverage-update-richard/scripts/coverage_update_richard_cli.py` | `CORE`（L39-67）：27 条 `{name, search_names[]}` |
| 财报跟踪 | `$OCW/scripts/richard_earnings_tracker_cli.py` | `COMPANIES`（L26）：105 条，group A 是 Core，B、C、D 是 Watch |
| 财报日期 | `$OCW/state/earnings_scan/watchlist.json` | confirmed、pending、reported 三组，含 IR 链接。**没有 tier 字段** |

### 已知不一致（脚本必须显式处理，不能静默处理）

| 问题 | 处理 |
|---|---|
| 美团：`TICKERS` 写 `3690.HK`，`watchlist.json` 和 tracker CLI 写 `MPNGY` | 规范代码用 `3690.HK`（owner 已定），`aliases.adr` 记 `MPNGY`。港股深度在 P6，本阶段只进名单 |
| `601100.SS` 和 `601100.SH` | 规范代码用 Yahoo 格式 `601100.SS`，别名记 `.SH` |
| `BRK-B` 和 `BRK.B` | 规范代码用 `BRK-B`，别名记 `BRK.B` |
| SPCX（SpaceX）分在 US Mega Tech 栏，tier 为 Core | 留在 Core（owner 已定）。`note` 写"非上市或特殊标的，数据可得性有限；没有 filing 和行情时按'本轮未取得'处理" |
| `watchlist.json` 的 `reported` 里 TCOM 和 IBKR 各出现两次 | 去重，不影响 tier |

### 改动/新增文件

| 路径 | 状态 |
|---|---|
| `<repo>/scripts/dalton/build_coverage.py` | 【新建】。用 `ast` 解析上面三个 `.py` 文件里的字面量，**不执行它们**；读 `watchlist.json`；输出 YAML 和差异报告 |
| `<repo>/config/dalton/coverage.yaml` | 【新建】，进 git。这是规范名单，名单变更以 PR 的形式经 PM 确认 |
| `<repo>/config/dalton/README.md` | 【新建】，可选，写字段说明 |

### `coverage.yaml` 的 schema

```yaml
schema_version: 1
as_of: "2026-10-09"                # 来自原表日期
generated_at: "2026-10-18T..."
sources:
  - {path: "~/.openclaw/workspace/skills/coverage-update-richard/scripts/screen_movers.py", sha256: "..."}
  - {path: ".../coverage_update_richard_cli.py", sha256: "..."}
  - {path: "~/.openclaw/workspace/scripts/richard_earnings_tracker_cli.py", sha256: "..."}
  - {path: "~/.openclaw/workspace/state/earnings_scan/watchlist.json", sha256: "..."}
tiers:
  core:  {meaning: "每周五源复盘、每日跟踪、财报前瞻和点评全链", count: 27}
  watch: {meaning: "只看异动；财报只做快讯", count: 78}
companies:
  - ticker: META                   # 规范代码，Yahoo 格式
    name: "Meta Platforms"
    tier: core                     # core | watch
    market: US                     # US | HK | CN
    peer_group: us_megatech
    sheet_section: "US Mega Tech"
    peers: [GOOGL, AMZN, MSFT, AAPL]   # TICKERS 里同 peer_group 的其他标的
    etfs: [QQQ]                    # 来自 EXTRA_PEERS；没有就留空列表
    aliases: {search_names: ["Meta", "Facebook"], adr: null, earnings_tracker: META}
    earnings: {group: A, ir_url: "https://investor.atmeta.com/..."}
    workspace: {name: "META", status: active}   # active | planned | none
    coverage_stage: legacy_imported   # none | legacy_imported | initial | full
    note: null
```
- `peers` 和 `etfs` 的示例值只用来说明结构，以脚本的实际输出为准。

### 分发与表示

1. **规范文件**是 `config/dalton/coverage.yaml`，在 git 里有版本。
2. **给 agent 读的副本**是 Home 工作区里的 `Home/dalton/coverage.yaml`，Home 的文件夹名已核实。具体做法：
   - 用 `POST /api/v1/workspaces/home` 拿到 Home 的 id；
   - 用 `PUT /api/v1/workspaces/{id}/files/write?path=dalton/coverage.yaml` 上传；
   - 由 `scripts/dalton/build_coverage.py --sync` 完成，并且做成幂等：内容哈希相同就不写。
   - 公司工作区通过 `../Home/dalton/coverage.yaml` 读取。同一台机器上的工作区可以互相访问【已核实，AGENTS.md】。
3. **用户层记忆**只放指针和规则，不放名单，因为 memory 的模板明确说不要保存详尽的 ticker 列表【已核实 `memory.md.j2`】。文件是 `.agents/user/memory/dalton_coverage.md`（type: project），内容包括：
   - 名单位置；
   - Core 和 Watch 的含义；
   - "名单变更需要 PM 确认"；
   - 当前已建工作区的 4 家。
   
   这个文件在 P1.1 的入职对话里一并写入。

### 验证方式

1. 运行 `uv run python scripts/dalton/build_coverage.py --check`。输出 Core 27、Watch 78、合计 105，以及和各来源的差异表。差异表里只能有上面已登记的 5 类不一致。
2. 运行 `--sync` 两次，第二次应该是 0 写入。
3. 在 Home 问 Chief of Staff："我们的 Core 名单里有哪些中概股？" 期望它读取 `dalton/coverage.yaml` 作答，数量正确。

### 验收标准

- 计数正确。
- 每一项差异都有处理记录。
- YAML 由脚本生成，不手工编辑；需要手工覆盖的项写在 `config/dalton/coverage.overrides.yaml` 里，由脚本合并。
- PM（owner）在 PR 里确认名单。

### 风险与注意

- OpenClaw 的名单还会继续更新。`build_coverage.py` 在报告里写出来源文件的 sha256，P4 起每周比对一次；来源一变就提醒，**不自动覆盖**。

**工时：** 1 人日。

---

## P1.3 公司工作区初始化

**目标：** 给一家被覆盖的公司建出标准的工作区骨架：工作区本身（名字就是 ticker，绑定共用的那台 computer）、`agent.md`、目录和各目录的 README，并且可以重复执行，不会覆盖分析师已经写过的内容。**知识文档（`data/*.json` 和渲染出的 `.md`）不由本步骤写**：4 家迁移公司由 P1.4 经 PK.7 的导入 API 创建；新覆盖的公司由分析师第一次调用 `knowledge_update`（op=`create`）创建。

**前置依赖：**
- P1.2（工作区名和 tier 从 coverage.yaml 读）；
- PK v0.3 的 PK.1（文件布局）和 PK.6（agent.md 托管块）。**布局以 PK.1 为准，本节不另定义。**

### 方案比较

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| a. 后端钩子 | 在 `_ensure_project_attached`（`computer_manager/_provisioning.py` L1004）里，按工作区 config 的标记写入种子文件 | 全自动 | 改核心生命周期代码，多 worker 和恢复路径都要考虑，和上游耦合 | 不采用 |
| b. 宿主机脚本，走 REST | `POST /api/v1/workspaces` → `POST /{id}/start?lazy=false` → `PUT /{id}/files/write` | 不改核心，可以批量处理，幂等 | 需要后端在运行 | **采用**，用于批量建 4 家和后续迁移 |
| c. skill 加沙箱脚本 | `dalton-init-company`：agent 在工作区里运行 `init_company.py` | agent 可以自主为新覆盖的公司建骨架（P2 起用得上） | 依赖 agent 执行 | **采用**，供 P2 以后使用 |

b 和 c **共用同一份模板和渲染代码**，避免两份实现跑偏。

### 改动/新增文件

| 路径 | 状态 |
|---|---|
| `<repo>/plugins/dalton/skills/dalton-init-company/SKILL.md` | 【新建】，什么时候用、怎么用 |
| `<repo>/plugins/dalton/skills/dalton-init-company/scripts/init_company.py` | 【新建】。只用标准库，既能在沙箱里运行，也能被宿主机脚本 import。核心函数是 `render(ticker, coverage_entry, template_version) -> dict[relpath, str]` 和 `apply(root, files, manifest)` |
| `<repo>/plugins/dalton/skills/dalton-init-company/templates/*` | 【新建】，各文件的模板，见下 |
| `<repo>/scripts/dalton/init_company_workspace.py` | 【新建】。宿主机走 REST，用 `importlib` 按路径加载上面的 `init_company.py` |
| `dalton_registry.py` | 加 `dalton-init-company`：`command="dalton-init-company"`，`requires=("dalton-conventions",)` |

### 工作区骨架（初始化之后的样子）

工作区名 = 规范 ticker（例如 `META`），工作区描述写"公司名 · Core/Watch"（之后由 `knowledge_update` 写 `company` 文档时同步，PK.4）。所有公司工作区绑定同一台 computer（P0.1）。布局照 PK.1，本步骤只建下面这些：

```
META/                            # 工作区文件夹 = ticker
├── agent.md                     # 自动注入，32 KB 上限；含 PK.6 的托管块标记，保留 LangAlpha 的种子小节
├── data/consensus/README.md     # 一致预期快照 schema，见 P0-D.6
├── model/README.md              # 模型命名和版本规则（PK.1：<TICKER>-model-v<NN>.xlsx，每版一个新文件）
├── research/README.md           # 事件笔记、专题研究（自由文件）
├── drafts/README.md             # 待发布草稿；草稿随时可改，核验只在发布时卡一次
├── published/README.md          # 只经 publish_report 写入（PK.11），按约定不改
├── sources/README.md            # 迁移和原件副本放这里，只读
└── .dalton/init.json            # 初始化清单
```

`thesis.md`、`debate.md`、`drivers.md`、`management.md`、`lessons.md`、`agenda.md`、`verdicts.md`、`data/<doc>.json`、`history/` 由 PK 的工具在第一次写入时生成，初始化脚本不碰它们。

### 模板要点

**`agent.md`**（不超过 2,000 词，符合 `workspace_context.md.j2` 的要求）：

```markdown
# {name}（{ticker}）· Dalton 覆盖工作区

## 覆盖信息
- Tier：{tier}；同组：{peers}；市场：{market}；覆盖阶段：{coverage_stage}
- 名单来源：../Home/dalton/coverage.yaml（as_of {as_of}）

## 当前判断（一句话）
暂无（{date} 初始化）。正式 thesis 以 data/thesis.json 的 `official`（经 publish_report、PM 批准）为准。

<!-- dalton:summary:start (由 knowledge_update 维护，勿手改) -->
（知识层尚未建立）
<!-- dalton:summary:end -->

## 工作约定
- 先读 .agents/skills/dalton-conventions/SKILL.md
- 回答 PM 的问题：先读 .agents/skills/dalton-pm-qa/SKILL.md
- 改知识层用 knowledge_update / knowledge_append_verdict（PK.4）；直接改文件会被记录为 direct_edit 并提示合入
- 基金 wiki 只读；发现写进本工作区的知识层
- 可以直接新建和修改本公司的 dalton-* 自动任务文件，不必先问（PK.15）
- 每次工作结束：更新议程，并给出下一次唤醒的时间和理由（PK.14）

## 文件索引
| 文件 | 内容 |
|---|---|
| thesis.md / data/thesis.json | thesis 支柱、证据台账、证伪阈值、house view（字段见 PK.2） |
| ... | ... |

## Thread Index
## Key Findings
## File Index
```
最后三个小节沿用 LangAlpha 种子 `agent.md` 的标题（`computer_manager/_types.py` L30），这样 LangAlpha 自带的约定照样适用。

**其他文件：**
- 每个 README 开头放一段 HTML 注释，写清"谁写、何时写、只追加还是可改"，并指向 PK.1 的对应条目。
- 不写任何 `data/*.json` 占位：空文档由 PK 的 `create` 操作生成，避免出现绕过 schema 校验的初始版本。

### 幂等规则（`.dalton/init.json`）

清单结构：
```json
{"template_version": "0.1", "created_at": "...", "files": {"thesis.md": {"sha256": "<写入时的哈希>"}}}
```

逐个文件判断：

| 情况 | 处理 |
|---|---|
| 文件不存在 | 写入 |
| 文件存在，哈希等于清单记录，模板版本升级了 | 覆盖，表示分析师没有动过 |
| 文件存在，哈希不同 | **不写**，表示分析师改过。报告里列为 `skipped_modified` |
| `agent.md` 被改过，但缺少托管块标记 | 只在文件末尾追加一对空的托管块标记，其余不动 |
| `agent.md` 正好是 LangAlpha 的默认种子内容 | 视为未修改，可以覆盖 |

### 宿主机脚本的用法

```bash
uv run python scripts/dalton/init_company_workspace.py --tickers META,AMZN,GOOGL,MSFT \
  [--api http://localhost:8000] [--apply]      # 默认 dry-run，只打印将要创建和写入的内容
```
- OSS 模式下不带认证头，用户是 `AUTH_USER_ID`，默认 `local-dev-user`【已核实 `src/server/utils/api.py` L71-100】。
- 先用 `GET /api/v1/workspaces` 按名字查重，已有工作区就复用，不重复创建。
- 新建时绑定已有的那台 computer（P0.1：全部公司工作区共用一个沙箱）【待验证：创建接口的 computer 参数】。
- 只建美股公司（本阶段只做美股）；港股、A 股的工作区放到 P6。

### 验证方式

1. 先用测试工作区 `ZZTEST` 跑一次 `--apply`，然后用 `GET /{id}/files` 核对目录。
2. 再跑一次，应该是 0 写入。
3. 手工改一下 `model/README.md` 再跑一次，结果应为 `skipped_modified`。
4. 在 ZZTEST 工作区问："这个工作区有哪些知识文件，各自是干什么的？"期望 agent 根据注入的 agent.md 正确回答。
5. 在 skill 路径上：在另一个测试工作区执行 `/dalton-init-company ZZTEST2`，得到同样的骨架。

### 验收标准

- META、AMZN、GOOGL、MSFT 四个工作区都已存在，骨架完整。
- 幂等性的三种情况都有截图或日志。
- 骨架与 PK.1 一致、agent.md 托管块标记与 PK.6 一致，由 PK 的负责人签字确认。
- 4 个工作区在同一台 computer 上（`docker ps` 只有 1 个沙箱容器）。

### 风险与注意

- 写文件会启动沙箱，冷启动比较慢；工作区处于 stopped 或 archived 状态时返回 409【已核实】。脚本要先调用 start 并等待就绪。
- 托管块标记的写法必须和 PK.6 逐字一致，否则 `knowledge_update` 找不到要改写的区域。

**工时：** 1.25 人日。

---

## P1.4 旧认知迁移（META / AMZN / GOOGL / MSFT）

**目标：** 一次性脚本 `scripts/dalton/migrate_legacy_knowledge.py`。它把 OpenClaw 的跟踪状态、基金 wiki 索引和旧 Dalton 的 debate map、forecast model、dossier，转成四个公司工作区里的知识文件。**知识文档一律经 PK.7 的导入 API 写入**（`POST /api/v1/dalton/knowledge/{workspace_id}/{doc}/import`，`source='import'`，走与 `knowledge_update` 相同的校验和版本记录）；原样副本、快照和索引这类普通文件经工作区文件 REST 写入。PK.7 不再单独提供导入脚本，抽取逻辑都在本步骤。要求：
- 所有来源只读；
- 可以重复执行；
- 每一项内容都带出处；
- 迁入的观点标为"迁移观点，待复核"，**不当作 PM 批准的正式 thesis**。蓝图记录旧 Dalton 正式 thesis 为 0 版。

**前置依赖：** P1.3 的骨架已经建好；PK.2 的 schema 和 PK.7 的导入 API 已可用（PK v0.3 已定稿）。

### 来源清单（只读）

| 来源 | 路径 | 用什么 |
|---|---|---|
| OpenClaw 跟踪底稿 | `$OCW/state/stock-tracker/<T>/thesis.md`（每个 30 至 33 KB） | 小节：核心判断；Debates（META 有 D1 至 D6，其余有 D1 至 D7，每条含分歧、Bull、Bear、当前倾向（含置信度）、改判）；Earnings revision 模型；KPI 与阈值；催化剂日历；指引节奏；专家来源可靠性；已知观点池；待核实线索；排除来源；数据备注；变更记录 |
| 一致预期快照 | `$OCW/state/stock-tracker/<T>/consensus/*.json`（META 9 份，AMZN 3 份，GOOGL 4 份，MSFT 3 份） | 作为一致预期历史的起点 |
| 已处理的文档 | `$OCW/state/stock-tracker/<T>/seen.json`、`last_run.json`、`updates.log` | 去重基线，"已知观点池"的依据 |
| 跟踪配置 | `$OCW/config/stock_tracker/<T>.json` | 别名、检索词、阈值、`model{diluted_shares_bn, tax_rate, other_income_bn}`；**丢弃 `feishu{}`** |
| 跟踪提示词 | `$OCW/config/stock_tracker/<T>_cron_prompt.md` | 里面有专家可靠性规则，原样复制到 `sources/legacy/` |
| 基金 wiki | `$OCW/wiki/companies/<T>/`（META 7 篇，AMZN 10 篇，GOOGL 7 篇加 1 个 PDF，MSFT 2 篇），以及 `vectors.db` | **不复制正文**（wiki 仍是权威来源，按需经 researchdata 读取），只生成索引 |
| 旧 Dalton | `$CORE_DB`（6.7 GB，journal_mode=delete【已核实】，344 张表） | 见下 |

**旧 Dalton 各表的用法：**

| 表 | 内容 | 用法 |
|---|---|---|
| `debate_map_versions` | 99 版：googl 27、msft 25、amzn 18、meta 16，另有 industry hyperscaler 13 | 取每条链 `version_number` 最大的一版 |
| `company_dossier_versions` | 25 版 | 取最新版 |
| `forecast_model_versions` | 19 版 | 取最新版，以及 `forecast_model_annual_projections` 和 `forecast_model_filing_proofs` |
| `claim_versions` | 6,574 条 | 把 `claim-version:<id>` 解析成 `normalized_statement`、`value`、`unit`、`period`、`basis` |
| `evidence_versions`、`evidence_relations` | — | 回指原始来源（filing 的 accession、研报 doc id）。字段关系【待验证】 |
| `document_provenance_records` | 6,199 条 | 用 broker、title、published_at 补全来源身份 |
| `thesis_versions` | 0 行 | 没有可迁移的内容 |

### 读库安全

1. 只用 `sqlite3.connect("file:<path>?mode=ro&immutable=1", uri=True)`。`-readonly` 在外置卷上会因为无法创建 `-shm` 而失败【已核实】。
2. 读之前检查没有 `-wal` 和 `-journal` 文件，并记录 `size`、`mtime` 和前 64 MB 的 sha256；读完再检查一次，变了就中止并重试。`immutable=1` 假定库在读取期间不变，如果旧 Dalton 同时在写，读到的可能不一致。如果库在频繁变化，改为先把整个库复制到本地临时目录，再按只读方式打开副本。
3. 脚本里绝不出现任何写语句；在 code review 时 grep `INSERT`、`UPDATE`、`DELETE`、`ATTACH`、`PRAGMA` 中会写库的那些。
4. 不读 `writer-tokens.json`、`credential-grants` 一类文件，也不读 `deliveries.json`，因为里面有飞书 chat_id。

### 映射：来源到目标

字段以 PK.2 的 schema 为准，对象级的映射规则（例如支柱 `provenance='inherited'`、`official=null`）以 PK.7 为准；下表只规定"来源 → 目标"和写入方式。

**A. 知识文档（经 PK.7 导入 API，按 PK.2 schema 生成 JSON，`.md` 由服务端渲染）：**

| 目标文档 | 内容来源 | 规则 |
|---|---|---|
| `thesis` | OpenClaw 的"核心判断"；旧 Dalton dossier 的相关段落 | 支柱 `provenance: "inherited"`，`official: null`（旧系统正式 thesis 为 0 版）；`one_liner` 前缀"迁移观点，待复核"。OpenClaw 的"变更记录"原文放 `research/legacy-import-<date>.md`，不塞进 schema |
| `debate` | ① OpenClaw 的 D1..Dn（主来源，更新、更贴近人工判断）：分歧、Bull、Bear、当前倾向（写成 `my_view`）、改判条件<br>② 旧 Dalton 的最新 debate map：每条 debate 的 question、bull 和 bear（claim 解析后的原文）、market_position、our_position、status、last_shift_reason | **两者不合并。** 按主题对应到同一条 debate：OpenClaw 的内容进 `bull`/`bear`/`my_view`，旧 Dalton 的内容写进该 debate 的 `history` 第一条（"旧 Dalton debate map v{N}（as of …）"）。两者矛盾时，在 `agenda.open_questions` 生成一条复核问题。"已知观点池"放 `research/legacy-import-<date>.md` |
| `drivers` | OpenClaw 的"KPI 与阈值"和"Earnings revision 模型"；config 里的 `thresholds` 和 `model{}`；旧 Dalton forecast model 的 `drivers[]`（concept、method、history 的 accessions） | 每个 driver 的来源写进对应的 `evidence_refs`（`kind:"file"`，指向 `sources/legacy/...`）；阈值标明出自 OpenClaw 配置 |
| `management` | OpenClaw 的"指引节奏"；旧 Dalton dossier 的 `guidance_style` 和 `management_and_capital_allocation` 两节（句子由 claim 解析而来） | 只放有出处的句子 |
| `lessons` | OpenClaw 的"专家来源可靠性"和"排除来源"（附原因） | 每条写 `applies_when`；`confidence: low`，`statement` 前缀"迁移，待复核" |
| `agenda` | OpenClaw 的"催化剂日历"和"待核实线索"；迁移产生的复核任务（每个 Dn 一条，加上来源分歧项） | 第一条问题固定为"复核迁移认知：逐条确认、修订或否决 D1..Dn"（`value: high`、`urgency: now`）。催化剂日期一律 `date_status: estimated`（显示"待确认"），等 PS.7 用官方来源确认 |
| `company` | `coverage.yaml` 的条目 | ticker、tier、peers、benchmarks（ETF） |

PK 的 schema 是封闭的（`additionalProperties: false`），所以**不能**在 JSON 对象上加 `provenance` 字段。出处的写法见下一节。

**B. 普通文件（经工作区文件 REST 写入，按 P1.3 的幂等规则）：**

| 目标文件 | 内容来源 | 规则 |
|---|---|---|
| `agent.md` | 一句话当前判断，取自 OpenClaw 核心判断的第一句，标"迁移"；dossier 的 `business_model` 摘要 | 只改托管块以外的"覆盖信息"一节；托管块由 `knowledge_update` 维护 |
| `model/legacy/forecast_model_v{N}.json` 和 `model/legacy/README.md` | 旧 Dalton forecast model 的原始 `record_json`，以及摘要（drivers、预测期间、assumptions、evidence accessions） | **不转成 xlsx**，P3 再建模型 |
| `data/consensus/<ts>.json` | OpenClaw 的一致预期快照，原样复制，补上 `_meta` | 文件名保留原来的时间戳 |
| `sources/legacy/openclaw/` | `thesis.md`、`<T>.json`（去掉 feishu）、`<T>_cron_prompt.md`、`updates.log`、`seen.json` 的原样副本 | 只读副本，供核对 |
| `sources/legacy/dalton-core/` | debate map、dossier 最新版的原始 `record_json` | 同上 |
| `sources/legacy/provenance.json` | 每条迁入内容的完整出处记录（见下一节） | 机器可读的出处总表 |
| `sources/wiki-index.md` | 只读查询 `vectors.db`：`documents WHERE category_type='company' AND category_name=T`，列出日期、类型、`wiki:<id>`、文件名和 `source`；再列出 `related` 里提到 T 的文档数（META 73、AMZN 115、GOOGL 102、MSFT 74 篇左右） | 不复制正文；wiki 只读 |
| `research/legacy-import-<date>.md` | schema 放不下的旧字段、变更记录、已知观点池 | 人读附件 |
| `Home/dalton/industry/us-hyperscaler/debate.md` | 旧 Dalton 的行业 debate map（hyperscaler 链，13 版）的最新版 | 放在 Home，四家公司的 agent.md 里加链接 |

### 出处格式

**普通 Markdown 文件**（`research/legacy-import-*.md`、`sources/wiki-index.md` 等）：
- 每个迁入的小节末尾加一行脚注：`> 迁移自：OpenClaw stock-tracker thesis.md（state/stock-tracker/META/thesis.md，mtime 2026-10-16，sha256:ab12…），2026-10-18 迁入`。
- 文件头的 HTML 注释里放完整的 `migrated_from` 列表。

**知识文档（PK schema 内）：**
- 导入请求的 `reason` 写 `迁入自 <system>:<table 或文件>@<content_hash 前 12 位>`，进 `dalton_knowledge_versions.reason`；
- 证据条目和 debate 的 `evidence_refs` 用 `{"kind":"file","path":"sources/legacy/dalton-core/debate_map_v16.json"}` 这类引用指回原样副本。

**出处总表 `sources/legacy/provenance.json`：** 每条迁入内容一条记录，用 schema 里的 id（如 `P1`、`D3`、`K2`）作键：
```json
"D3": {"system": "dalton-core", "table": "debate_map_versions", "version_id": "...",
  "version_number": 16, "content_hash": "...", "source_created_at": "2026-10-16T...",
  "path": "/Volumes/EveSSD/.../core.sqlite", "migrated_at": "...",
  "migrator": "scripts/dalton/migrate_legacy_knowledge.py@<git sha>"}
```

**claim 无法解析时**，保留 `claim-version:<id>` 原文，并在报告的 `unresolved_claims` 里列出。**绝不补写内容。**

"迁移"本身不重建旧系统的治理字段（见风险一节）；出处只用于核对，不参与任何校验。

### 脚本结构与用法

```bash
uv run python scripts/dalton/migrate_legacy_knowledge.py \
  --tickers META,AMZN,GOOGL,MSFT \
  --out var/dalton/migration            # 本地暂存区，进 .gitignore
  [--api http://localhost:8000] [--apply] [--only openclaw,dalton-core,wiki]
```

执行步骤：
1. **extract：** 读取各来源，生成中间 JSON，写到 `var/dalton/migration/<run_id>/extract/<T>/*.json`。
2. **render：** 生成 7 份知识文档的 JSON（A 表）和普通文件（B 表），写到 `.../render/<T>/`。用 PK.2 的 JSON Schema 逐个本地校验（`src/dalton/knowledge/schemas/`），不通过就停下并写进报告。
3. **diff：**
   - 知识文档：`GET` 该文档的最新版本（PK.13 的只读 API）。不存在 → 待导入；已存在 → 跳过，报告里写"已存在 vN，跳过"（与 PK.7 的重复执行规则一致）。不覆盖分析师后来的版本。
   - 普通文件：对照工作区里的 `.dalton/migration.json` 清单，规则和 P1.3 的幂等规则相同：没有动过的可以覆盖；分析师改过的不覆盖，新内容放到 `.dalton/migration-pending/<path>`，并写进报告。
4. **apply：** 只有加了 `--apply` 才写：知识文档调 PK.7 导入 API，普通文件调工作区文件 REST，同时写入新的清单。
5. **report：** 生成 `.../report.md`，内容包括：
   - 每家公司每类内容的条数；
   - D1..Dn 的迁移率；
   - claim 的解析率；
   - 来源分歧的条数；
   - 跳过的文件。

**幂等：** 来源不变时，第二次运行应该是 0 写入。`run_id` 取所有来源指纹的 hash，相同来源得到相同的 `run_id`。

### 验证方式

1. 先 dry-run 四家公司，人工抽查：
   - 每家抽 3 条 debate，对照 OpenClaw 原文和 `core.sqlite` 的 `record_json`；
   - 每家抽 2 个 driver 阈值，对照配置文件。
2. 执行 `--apply`，然后在 UI 里打开 META 工作区，查看服务端渲染出的 debate.md 和 agenda.md；`dalton_knowledge_versions` 里每家 7 份文档各有一行 `source='import'`。
3. 再执行一次 `--apply`，应该是 0 写入。
4. 手工改一下 META 的 `sources/wiki-index.md` 再执行，应该进入 `migration-pending`；知识文档显示"已存在，跳过"。
5. 用 `ls -l` 和 sha 校验，确认所有来源文件的 mtime 和 sha 在运行前后没有变化。

### 验收标准

- OpenClaw 的 D1..Dn 迁移率 100%（META 6 条，其余各 7 条），每条都能在 `sources/legacy/provenance.json` 里查到出处。
- 4 家 × 7 份知识文档全部经导入 API 写入并通过 schema；第二次运行显示"已存在，跳过"。
- 旧 Dalton 的 debate map、dossier、forecast 最新版全部有对应的原样副本和摘要。claim 解析率写进报告，未解析的列出清单，不补写。
- 来源分歧全部生成了 agenda 复核问题。
- 没有任何飞书 ID 或凭证进入工作区。
- 来源只读有证据：运行前后的指纹一致。

### 风险与注意

- **观点时效。** OpenClaw 的底稿截至 10-16 前后，旧 Dalton 截至 10-16（debate map）或 10-12（forecast）。迁入的观点必须带 as_of。P1.5 的问答测试会检查 agent 有没有把旧观点当成最新观点。
- **不要把迁移变成"重建旧系统"。** 旧 Dalton 的治理字段（constitution_ref、mission_version_ref、drafted_by、verified_by 等）只保留在原样副本里，不进入新的知识文件。
- 外置卷 `/Volumes/EveSSD` 没挂载时，脚本要明确报错退出，不能静默跳过旧 Dalton 部分。

**工时：** 3 人日（extract 和 render 2 天，验证和抽查 1 天；导入 API 本身在 PK.7）。

---

## P1.4b OpenClaw 对话归档与回填（`scripts/dalton/conversation_archive.py`）

### 日常增量任务（默认值不变）

每日定时任务使用 `src/dalton/dalton.yaml` 中的默认配置（`extract_concurrency: 1`、`hourly_token_budget: 120000`），保持单并发串行平稳运行：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py run
```

### 回填

当需要将历史对话大批量回填（例如回填至 `2026-04-30` 约 1 万个片段）且 Antigravity（agy）OAuth 额度充足时，建议通过 CLI 显式传入并发与每小时 token 预算参数：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py backfill \
  --since 2026-04-30 \
  --concurrency 4 \
  --hourly-token-budget 1500000
```

- **推荐回填参数**：`--concurrency 4 --hourly-token-budget 1500000`（仅对本次回填进程生效；每日增量任务仍用 `dalton.yaml` 默认值 `extract_concurrency: 1`、`hourly_token_budget: 120000`）。
- **逐段有序落盘与进度可见**：并发抽取时，结果按原始片段顺序缓冲，每次将已连续完成的前缀（最多 `N = concurrency` 段）批量向量化并在单个数据库事务中写入 `dalton_conversation_cards`（含 `content_hash` 去重与按时间顺序的 `supersedes`）和 `dalton_conversation_processed`，同时实时更新 `dalton_conversation_runs` 的进度计数（`segments_processed`、`cards_inserted`、`total_tokens` 等），每完成一个片段向 `stderr` 无缓冲输出一行 `INFO` 进度日志。
- **运行锁与防重入**：启动时获取 Postgres 会话级 advisory lock（`pg_try_advisory_lock(hashtext('dalton_conversation_archive'))`）。若已有归档进程在运行，立即打印 `"已有归档在运行"` 并以退出码 `2` 退出；拿到锁后会自动将此前崩溃残留的 `status='running'` 行标记为 `abandoned`。
- **被杀后如何续跑**：若进程因 `SIGTERM`、`SIGKILL`、容器重建或配额耗尽被中断，已连续完成的前缀片段均已事务落盘并记录在 `dalton_conversation_processed` 中（inbox `.jsonl` 文件也会保留至文件内所有分片均处理完毕）。续跑时**直接原样重新执行同一条命令**（或加上 `--skip-host-export` 直接消费已在 `var/dalton/conversations/inbox/` 中的 `.jsonl` 文件），管线会自动跳过已记录在 `dalton_conversation_processed` 中的片段，只重做被杀当时正在进行中及后续未完成的片段。

---

## P1.5 P1 验收清单

蓝图的完成标准：问"你对 META 的看法和主要 debate"，回答引用的是迁入的认知，而且符合方法论的结构。

| # | 检查项 | 方法 | 通过标准 |
|---|---|---|---|
| 1 | dalton-conventions 已注册，并被读取 | 在 META 工作区对话，查看 UI 里的 Read 动作 | 显示为平台 skill；回答前读取了它 |
| 2 | 覆盖名单正确 | `build_coverage.py --check`；在 Home 提问 | Core 27、Watch 78；差异都已登记 |
| 3 | 用户层记忆 | `GET /api/v1/memory/user` | `fund_mandate.md` 和 `dalton_coverage.md` 都在，只含规则和指针 |
| 4 | 四个工作区的骨架 | `GET /{id}/files` | 骨架完整；agent.md 已被注入 |
| 5 | 迁移报告 | `report.md` | 满足 P1.4 的验收标准 |
| 6 | **核心问答（META）** | 在 META 工作区问："你对 META 的看法和主要 debate？" | 见下面的评分表 |
| 7 | 核心问答（另外三家各一次） | 同上 | 同上 |
| 8 | Home 跨工作区问答 | 在 Home 问："四家 Mega Tech 里，哪家的 debate 最近变化最大？" | Chief of Staff 读取各工作区的 debate.md 作答，或者转交给对应的分析师；as_of 写清楚 |
| 9 | 专家材料的使用口径 | 在 GOOGL 工作区问："专家对 TPU 外销怎么看？" | 使用 wiki 里的专家访谈和 Guidepoint 检索结果；写身份不写姓名；标"单一来源估计"；不以 MNPI 为由拒绝 |
| 10 | 补充检索 | 问："这周 META 有没有新的卖方观点改变了 D1？" | 调用 researchdata；给出五词结论之一（界面显示中文短标签，记录里是正式词形），以及下一项验证 |
| 11 | 界面走查 | 用真实数据截图 | owner 签收 |

**第 6 和第 7 项的评分表（每项都满足才算通过）：**
1. 开头不超过 3 句核心判断，并写明这是迁移观点及其 as_of。
2. 列出 top debates（与迁入的 D1..Dn 对应）。每条都有分歧、多方、空方、我的判断和改判条件。
3. 观点引用迁入的知识文件，例如"见 debate.md D1"，或者引用来源身份加日期，并区分 OpenClaw 底稿和旧 Dalton。
4. 每个数字都带期间和来源；凡是一致预期，都标明来源和取数时间；没有编造的目标价或一致预期。
5. 来源分歧明确写出来，不被抹平。
6. 中文交付，保留英文术语，没有黑话和奖状式开场。
7. 结尾给出 1 至 3 条下一项验证，与 agenda.md 一致。

**签收：** owner 以 PM 身份盲评第 6 项，达到"可用"即通过。截图归档到 P1 的 PR。

---

## 附录 A：researchdata 路由速查

依据 `$OCW/config/data_sources.json` 的 23 条路由，以及 `data_gateway.py`、`research_library.py`【已核实】。

| 分类工具 | route | operations | 主要参数 | 后端 | 在 LangAlpha 中 |
|---|---|---|---|---|---|
| research | `company-wiki` | search、read、list、topic | query、start、end、document=`wiki:<id>`、params.company、sector、tag、search_mode、no_decay、indexed_since、offset、max_chars | `wiki/vectors.db`，只读 | ✓ |
| research | `all` | search（联搜） | query、params.sources | wiki、sales-notes、tmtb、funda-ai，用 RRF 融合 | ✓ |
| research | `alphaengine` | search_library、get_document、list_filters、auth_status | query、start 和 end（成对出现）、params（source_names、document_categories、markets、cursor 等）、get_document 的 params.doc_id 和 max_chars | `127.0.0.1:8950/mcp` | ✓，全文有配额 |
| research | `guidepoint` | search_library | query、start、end、limit（不超过 40） | `127.0.0.1:8943/mcp`，经代理 | ✓ |
| research | `transcript` | transcript | ticker、query、start、end | AlphaEngine 的电话会类型；回落源 roic 从 2026-08 起一直返回 403 | ✓ |
| research | `sales-notes`、`tmtb`、`funda-ai` | search、read | query、start、end、document、params.sender、layer | 本地 SQLite | ✓ |
| finance | `sec` | list-filings、financials、ratios、facts、smart-facts、search-concepts、search-text、read-item、attachments、eight-k、insider | ticker（必填）、form、concept、item、query、as_of | 宿主机 edgartools，有 24 小时缓存 | ✓ |
| finance | `estimates` | consensus | ticker | `consensus_estimates.py`（yfinance） | ✓ |
| finance | `cn-hk` | filings、ask | ticker、start、end、query | cninfo、AkShare | 可用，P6 再验收 |
| macro | `fred`、`treasury` | — | series、start、end、vintage；dataset | 公网 | ✓ |
| news、community | `web-news`、`web-page`、`filings`、`last30days`、`agent-reach`、`x-*`、`xueqiu`、`employee` | handoff | — | 不执行 | ✗ 桥会过滤掉 |

## 附录 B：`_sources` 映射表（`scripts/dalton/source_identity.py`）

| route 与 operation | 结果位置【以实测为准】 | document_id | identity | publisher | title | as_of（basis） | url |
|---|---|---|---|---|---|---|---|
| alphaengine search_library | `results[]` | `alphaengine:{doc_id}` | 按 `document_code` 或 type 映射：sell_side*、domestic_report、foreign_report → `broker_report`；announcement → `company`；meeting_minutes → 业绩会或调研纪要记 `company`，专家会记 `expert`（按 `type_name` 细分【待验证】）；news → `news` | `sources[0]` 的名称 | title | publish_time（publish_time） | null（私有） |
| alphaengine get_document | `metadata` | 同上 | 同上 | `metadata.sources` | `metadata.title` | 同上 | null |
| guidepoint search_library | 文本 JSON 里的 `data[]` | `guidepoint:{sha1(reference_url or transcript_name+date)}` | `expert` | `"Guidepoint · {respondent.title}, {respondent.company}"`，**不写姓名** | transcript_name | date（call_date） | reference_url |
| transcript | `items[]` | `alphaengine:{doc_id}` | kind 为 company_call 时记 `company`，为 broker_callback 时记 `broker_report` | `sources` | title | publish_time | null |
| company-wiki search、read、list | `results[]` 或 `document` | `wiki:<id>` | 按 content_type 映射：专家访谈 → `expert`；管理层会议纪要 → `company`；券商研报、卖方交流、Earnings Update → `broker_report`；微信文章 → `news`；Flomo 笔记、研究笔记、季度研究笔记和其他类型 → `internal_note` | `original_source`，即 frontmatter 的 `source`，没有就写"基金 wiki" | title | date（document_date） | url，即 `source_url` |
| sales-notes | `results[]` | `mail:<id>` 或 `digest:<id>` | `broker_report`（sales color）；layer=digest 时记 `derived`，`independent=false` | original_source（发件人） | subject | date（document_date） | null |
| tmtb | `results[]` | `tmtb:<id>` | `news`，表示第三方 newsletter【待验证：是否应归为 broker_report】 | "TMTB" | title | date | null |
| funda-ai | `results[]` | `funda:<id>` | `derived`，`independent=false` | "FUNDA AI" | title | published_at | url |
| all | `results[]` | 各 source 自己的 document_id，另外合并 `alternate_document_ids` | 按各 source 的规则 | — | — | — | — |
| sec（researchdata） | 视 operation 而定 | `sec:{accession}` | `filing` | "SEC EDGAR" | `"{form} {period}"` | filing_date（filed） | SEC 归档 URL |
| estimates | 顶层 | `estimates:yahoo:{ticker}:{fetched_at}` | `street_estimate` | "Yahoo Finance via yfinance" | `"{ticker} consensus"` | fetched_at（fetched_at） | null |
| fred、treasury | 顶层 | `fred:{series}[@vintage]`、`treasury:{dataset}` | `official_data` | "FRED"、"US Treasury" | series | 最新观测日期（observation_date） | 官方 URL |
| cn-hk filings | `results[]` | `cninfo:{id}` | `filing` | "巨潮资讯" | title | 公告日期 | url |
| dalton_sec | 自己产出 | `sec:{accession}` | `filing` | "SEC EDGAR" | — | filed | 归档 URL |

## 附录 C：已决事项与剩余 owner 问题

**已决事项**（v0.2 的开放问题已全部定下）：

| 事项 | 决定 |
|---|---|
| 鉴权 | R1：URL 路径里的只读能力令牌；R2（relay 私有出站白名单）列为以后的选项 |
| 沙箱 | 本地 Docker（colima，P0.1），一台 computer 共用；不用 Daytona。colima 的 `host-gateway` 由 P0-D.0 实测，需要改 daemon 配置时由 lead/运维执行 |
| 超时 | 桥按路由：默认 240 秒，长文最高 600 秒（受调用方预算封顶）；LangAlpha 侧 researchdata 单次预算可配置，默认 300 秒；超时只兜住挂死 |
| 来源面板 | P0 用不加迁移的降级版（P0-D.3）；完整版（d004）在 P3 与发布数字核验一起做（P0-D.3b） |
| 基金 wiki | 只读，不回写；发现留在工作区知识层和 `published/` |
| 专家材料 | 专家访谈、管理层交流、Guidepoint 检索结果都可用；标专家身份，不写姓名；Guidepoint 原文 ≤20 词 |
| 名单 | 美团 `3690.HK`（`MPNGY` 作别名）；SPCX 留在 Core |
| 五词结论 | JSON 用正式词形（`THESIS_STRENGTHENED` 等），md 和界面用中文短标签 |
| 旧 forecast model | 只迁原样 JSON 和摘要，P3 再建 xlsx |
| 价格 | 默认 yfinance；FMP 以后可选 |
| AlphaEngine 全文配额 | 只在 80% 告警（桥 `/healthz`），不设闸门 |
| 独立 Dalton 部署 | 已停，不存在访问冲突 |
| 市场范围 | 本阶段只做美股；港股和 A 股深度放到 P6 |
| PK schema | v0.3 已定稿，P1.3 和 P1.4 按 PK.1、PK.2、PK.7 实施 |

**剩余 owner 问题**（全套指南只剩这几项；本篇不直接依赖其中任何一项）：

| # | 问题 | 默认 / 建议 |
|---|---|---|
| 1 | 日额度的具体数额（USD/天） | 未定；P0 验收前在设置页填写 |
| 2 | STRL 的正式定义 | PS 暂用 M/D/E/C/F/S 的替代定义 |
| 3 | P2 的样本公司 | 建议 DASH，备选 AFRM |
| 4 | 仓位建议的百分比档位和 hurdle rate | hurdle rate 默认 15% |
| 5 | 每周复盘放在哪一天 | — |
| 6 | 净现金口径确认 | 采用 SKILL.md 里的版本 |
