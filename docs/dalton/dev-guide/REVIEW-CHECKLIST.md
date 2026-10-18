# 代码审查清单：第一轮代码（`dalton/main` `7670a03f`）

版本 0.1，2026-10-18。写给负责审查的人。对比的基线是 LangAlpha 上游 `8e0dce94`。

## 0. 怎么用这份清单

**规模：** 相对上游新增约 7.5 万行、364 个文件。业务代码约 4.6 万行，测试约 1.2 万行。按第 2 节分成 7 个模块，可以 7 人并行，也可以按优先级依次审查。

**每一条检查都要给出结论：**
- ✅ 通过：写明怎么确认的，比如读了哪段代码、跑了什么命令；
- ❌ 有问题：写明严重程度、文件和行号、会在什么情况下出错（最好附一个能复现的最小例子）、建议怎么改；
- ❓ 疑似：没能确认，写明还缺什么才能确认。

**严重程度：**

| 级别 | 含义 |
|---|---|
| 严重 | 钱算错、凭据泄露、正式成果里可能出现编造的数字、数据丢失 |
| 高 | 违反三条硬原则；核心功能在常见情况下出错 |
| 中 | 边界情况出错，或者和开发指南的设计不一致 |
| 低 | 可读性、命名、多余代码 |

**规则：**
- 只读审查：不改代码，不写生产数据，不调用付费模型，不登录供应商，不打印任何令牌或密钥。
- 可以跑单元测试、离线脚本，以及对本机服务的只读请求。
- "单元测试通过"不能代替审查：测试和代码是同一批人写的。
- 审查结果写进 `docs/dalton/dev-guide/review/R<模块编号>-<日期>.md`，汇总后由 lead 转成修正任务。

**每个模块都要查的四类问题：**
1. 逻辑错误和 bug；
2. 安全：凭据泄露、未授权访问、注入；
3. 违反三条硬原则：核验只在发布时卡一次；限制只有日美元额度和可选的 task cap，不许有次数门；尽量少改上游，改了要标 `DALTON-HOOK`；
4. 与开发指南的设计不一致（指南见 `README.md` 和各分册）。

**通用检查，所有模块都要过：**
- [ ] **多 worker：** 后端以多个进程运行。不允许把进程内的模块级变量当作状态依据，状态必须以 Postgres 为准（见上游 `AGENTS.md` 的 Multi-worker 一节）。
- [ ] **日志：** 日志、异常信息、SSE 事件、接口响应里不会出现令牌、API key、OAuth 码。
- [ ] **吞掉错误：** 没有 `except: pass` 这类静默吞掉错误的写法，失败要么上报、要么记录。
- [ ] **上游挂钩：** 对上游文件的改动都标了 `DALTON-HOOK`，并登记在 P0 附录 A。

## 1. 先跑的命令

```bash
cd /Users/everflow/Projects/dalton-langalpha
./scripts/dalton/check_hooks.sh --strict                       # 已知不通过（P0F.2）
.venv/bin/python -m pytest tests/unit/dalton -q                 # 72 项
.venv/bin/python -m pytest tests/unit/llms tests/unit/mcp_servers tests/unit/middleware/skills -q
(cd integrations/openclaw/dalton-langalpha-broker && node --test test/*.test.mjs)   # 10 项
(cd web && pnpm typecheck && pnpm test)
for f in workflows/*/workflow.js; do node --check "$f"; done
grep -rn "max_failures" src/dalton plugins/dalton workflows scripts/dalton docs/dalton/dev-guide/automation-templates   # 已知有问题（P0F.3）
git diff --name-only 8e0dce94 HEAD --diff-filter=M | grep -v '^docs/'   # 被改动的上游文件，逐一确认有 DALTON-HOOK 标注
```

## 2. 分模块检查

### R1 broker v2 与模型清单（优先级：高）

**范围：**
- `integrations/openclaw/dalton-langalpha-broker/**`
- `scripts/dalton/install_broker_v2.sh`、`scripts/dalton/broker_spike_client.py`
- `src/dalton/llm/{broker_http.py, openclaw_catalog.py, overlay.py, openclaw_overlay.json, openclaw_overrides.yaml, role_policy.py}`
- `scripts/dalton/{sync_openclaw_models.py, openclaw_smoke.py, apply_role_models.py}`
- `src/dalton/app/router.py` 里的同步和健康检查接口
- `src/llms/llm.py`、`src/server/services/llm/clients.py`、`src/server/app/api_keys.py` 里与 overlay、broker 相关的挂钩

**对照：** P0-platform.md 的 P0.2–P0.7。

**检查项：**
- [ ] **消息转换：** OpenAI 格式的 `system`、`user`、`assistant`（含 `tool_calls`）、`tool`（含 `tool_call_id`）都能正确转换成 OpenClaw SDK 的格式，转回来也正确。构造一个"两轮工具调用加一轮普通回答"的请求实际验证。
- [ ] **流式工具调用：** 工具调用的参数在流式输出里分多段返回时，拼起来是完整的 JSON；多个工具调用并行时编号不串。
- [ ] **调用日志与不重复付费：**
  - 发送给供应商之前，先写入"进行中"的记录；
  - 同一个调用 ID 重复请求时，返回保存的结果，不再调用供应商；
  - 进程崩溃后留下的"进行中"记录，再次请求时返回"不确定"，不重新发送；
  - **同一个调用 ID 配上不同的请求内容时，必须拒绝，不能返回别人的结果**；
  - 客户端中途断开后，broker 仍把调用跑完并记录下来；
  - 日志文件写入是原子的，并发时不会损坏；日志会不会无限增长。
- [ ] **调用 ID 的生成**（`broker_http.py`）：同一次调用的重试得到同一个 ID；不同调用不会撞上同一个 ID。
- [ ] **认证：**
  - token 比较用的是常量时间比较；
  - 错误的 token 返回 401，并且不在日志里留下 token；
  - 接口只监听本机地址（127.0.0.1），不对外开放。
- [ ] **模型白名单：** 不在白名单里的模型被拒绝，不能通过改 `model` 参数绕过。
- [ ] **回退：** 只有调用方明确要求时才回退，只在供应商故障、且还没有输出任何内容时回退，并且响应里写明实际用了哪个模型。
- [ ] **纯文字模型：** CLI 网关类模型收到带工具的请求时返回 400，不会把工具调用压成文字。
- [ ] **用量和费用：** 输入、输出、缓存读、缓存写的 token 数和费用都正确，不重复计算。
- [ ] **推理强度：** 能正确映射到 SDK 的推理级别。
- [ ] **同步脚本：**
  - 只读 `openclaw.json` 里模型的公开信息，不读也不写 `apiKey`、请求头、token；
  - 生成的 overlay 文件里没有任何凭据；
  - 写文件是原子的，中途失败不会留下半个文件。
- [ ] **overlay 合并：** 生成的模型键（`ocb-*`）不和上游的键冲突；overlay 文件更新后，各个 worker 都能加载到新内容。
- [ ] **家族判定**（`role_policy.family_of`）：每个模型的家族判断正确；审稿人不会和主模型同一家族。

### R2 本地日预算与管理接口（优先级：严重，涉及钱）

**范围：**
- `src/dalton/budget/**`
- `src/dalton/database/**` 中和预算有关的部分
- 迁移 `d001`、`d002`，以及 Dalton 独立的 Alembic 迁移链（`src/dalton/alembic.ini`、`src/dalton/migrations/**`）
- 上游挂钩：`src/server/services/credit_gate_port.py`、`src/server/dependencies/usage_limits.py`、`src/server/handlers/chat/ptc_run.py`、`src/server/services/automation_settlement.py`
- `src/dalton/settings.py`、`src/dalton/dalton.yaml`
- `src/dalton/app/router.py` 的全部接口
- `web/src/pages/Settings/panels/dalton/*`

**对照：** P0-platform.md 的 P0.8；README 的 L4。

**检查项：**
- [ ] **额度检查默认拒绝：** 没有设置额度、数据库不可用或出错时，一律不放行，而不是默认放行。
- [ ] **未结算的预留按全额占用：** 预留之后还没结算的，在计算剩余额度时按预留全额计入。
- [ ] **过期预留：** 按"预留金额"和"实际观察到的花费"中较大的一个入账，不会被释放。
- [ ] **结算：** 按 `conversation_usages` 里的实际费用结算，不会多扣，也不会漏扣。
- [ ] **覆盖所有花钱的入口：** 主分析师的对话、子智能体、workflow 派出的子智能体、自动任务、服务端的 `LLMService.complete`（比如审稿）。**逐一确认都计入了日额度。**
- [ ] **并发：** 两个进程同时预留时，不会都以为额度还够，而一起超支。检查 SQL 是否加锁或者是原子更新。
- [ ] **日期边界：** 按 Asia/Shanghai 计算"今天"；午夜前后跨日的调用归属正确。
- [ ] **金额精度：** 用 Decimal 或整数微美元，不用浮点数；剩余额度不会出现负数显示错误。
- [ ] **task cap：** 可选，生效时和日额度同时检查，先触发哪个都会暂停。
- [ ] **订阅类模型：** 现金成本记为 0，名义成本另外记录，不占用现金额度。
- [ ] **供应商配额：** 只提醒，不拦截。
- [ ] **没有次数限制：** 预算模块里没有调用次数、尝试次数之类的限制。
- [ ] **迁移链：** Dalton 的迁移版本表是独立的（`alembic_version_dalton`），和上游互不干扰；需要的索引都在；回滚脚本可用。
- [ ] **接口权限：**
  - `router.py` 的每个接口都要求登录，并且只能读写当前用户自己的数据；
  - 修改类接口有防 CSRF 的措施，做法和上游的其他接口一致；
  - 同步、健康检查这类管理接口不会被未登录的请求触发。

### R3 researchdata 桥接、SEC 数据、来源记录（优先级：高）

**范围：**
- `scripts/dalton/{researchdata_bridge.py, source_identity.py, install_bridge.sh, check_bridge.sh, launchd/*}`
- `plugins/dalton_data/**`，包括 `dalton_sec_mcp_server.py`、`plugin.json`、`mcp.json`
- `src/dalton/provenance/**`
- 上游挂钩：`src/ptc_agent/agent/middleware/provenance/middleware.py`、`src/ptc_agent/core/sandbox/mcp_client_runtime.py`、`src/ptc_agent/core/tool_generator.py`、`src/ptc_agent/core/sandbox/tool_overlay.py`、`src/ptc_agent/config/core.py`
- 迁移 `d004`
- 前端 `SourcesPanel`、`ResearchDocSource`
- `tests/unit/mcp_servers/agent_docstring_lock.json` 的改动

**对照：** P0D-P1-data-and-onboarding.md 的 P0-D.0–P0-D.8；README 的 O6、O8、O10、L5、L6。

**检查项：**
- [ ] **桥接的安全：**
  - 只监听需要的地址，不对局域网开放；
  - 访问 token 不出现在日志和审计记录里；token 比较是常量时间；
  - 启动 `researchdata_mcp.py` 子进程时没有 `shell=True`，参数不会被拼接成命令；
  - 读取类参数里的路径不能用 `../` 访问到不该访问的文件；
  - 不能借参数让桥接去请求任意网址；
  - 交给 OpenClaw 工具处理的路由已经隐藏，不暴露写入能力；
  - 有并发上限，大量请求不会把宿主机的资源耗尽。
- [ ] **子进程管理：** 超时后子进程会被结束，不会留下僵尸进程；桥接重启后没有残留进程。
- [ ] **MCP 协议：** 握手、会话、错误响应、超时，都符合 streamable HTTP 的规范。
- [ ] **超时：**
  - 每条路由的超时符合决定：默认 240 秒，长文档最长 600 秒，并且不超过调用方的时间预算；
  - LangAlpha 一侧的工具调用超时可配置，默认 300 秒；
  - **这个上游补丁只影响配置了的服务**，其他 MCP 服务的行为不变。
- [ ] **wiki 只读：** 没有任何写入 wiki 的路径。
- [ ] **来源记录：** 每份文档的来源类型、编号、日期和链接都映射正确。抽 5 个真实结果核对。
- [ ] **`dalton_sec` 的数据正确性**（这一项错了会直接影响研究结论）：
  - 单季和累计正确区分，按天数判断，例如 Q2 不会被当成上半年累计；
  - 非日历财年的映射正确，例如 MSFT 的财年在 6 月结束；
  - 第四季度推算：全年减前三季度，标注为推算值，并记录所用的申报文件；
  - 单位和数量级只采用公司自己的披露，不默认美元；
  - 有重述时，保留歧义并说明，不随意取一个；
  - 重复的事实条目要去重；
  - 抽 3 家公司、各 2 个指标，和 EDGAR 原文逐一核对。
- [ ] **工具说明快照：** `agent_docstring_lock.json` 相对上游只有新增，新增的键都属于 `dalton_*` 服务（P0F.2）。

### R4 知识层与发布治理（优先级：严重，涉及正式成果的可信度）

**范围：**
- `src/dalton/knowledge/**`、`src/dalton/publish/**`
- `src/dalton/tools/{knowledge_tools.py, publish_tools.py}` 及其在 `src/ptc_agent/agent/agent.py` 里的注册挂钩
- 迁移 `d003`、`d005`
- `plugins/dalton/agents/dalton-reviewer.md`
- 前端 `PublishApprovalCard`、`ToolApprovalCard` 的改动

**对照：** PK-knowledge-governance-autonomy.md 的 PK.1–PK.13；README 的 L7、L8、L9、L13、L16。

**检查项：**
- [ ] **三条硬规则不能被绕过：**
  - 证据台账只能追加，不能通过任何操作或直接改文件删除、修改已有证据；
  - 正式观点和 house view 只能经 `publish_report` 写入；
  - 目标价必须引用模型里的单元格。
- [ ] **直接修改知识文件：** 不拦截、不自动恢复，记录为 `direct_edit` 版本；不符合结构时给分析师提醒。用 Bash 改、在文件面板里改，两种情况都要验证。
- [ ] **草稿不会被卡住：** 草稿阶段的检查只给反馈，任何情况下都不阻止保存和继续研究。
- [ ] **数字核验不放过编造的数字**（最重要）。构造草稿测试以下情况，每一种都要被核验抓出来：
  - 带单位和数量级的数字：亿、billion、bn、百万、%；
  - 范围：例如 "20–25%"；
  - 中文写法：例如 "三成"、"翻倍"；
  - 表格里的数字；
  - 没有标注来源的数字；
  - 数值对、期间不对的数字，例如把 Q2 的数写成 Q3；
  - 数值对、指标不对的数字，例如把营收增速写成利润率。
- [ ] **数字核验不误伤合法的数字：** 日期、文档编号、合理的四舍五入，不应该被判为不通过。
- [ ] **独立审稿：**
  - 审稿模型一定和作者模型属于不同家族；
  - 审稿人不可用时，草稿仍然可读，但不能发布；
  - 审稿结论由服务端产生，不能被作者伪造。
- [ ] **PM 审批：**
  - 同一个批准只能执行一次，重复提交或重放请求不会发布两次；
  - 无人值守的自动任务里，发布停在"等待 PM"，不会因为弹出审批卡而把整个任务判为失败。
- [ ] **已发布内容不可变：** `published/` 里的文件不能被覆盖；文件被删除或修改后，能按数据库里保存的内容和 hash 恢复。
- [ ] **有发布成果的工作区只能归档，不能删除。**
- [ ] **并发：** 两个进程同时写同一份知识文件的版本时，有唯一约束保证顺序，不会丢版本。

### R5 自主议程与自动任务（优先级：高）

**范围：**
- `src/dalton/autonomy/**`
- `src/dalton/tools/director_tools.py`
- `scripts/dalton/{bootstrap_watch.py, autonomy_week_report.py}`
- 迁移 `d006`
- 上游挂钩：`src/server/services/automation_executor.py`、`src/server/services/automation_settlement.py`、`src/server/models/automation.py`、`src/server/database/automation.py`、`src/server/database/automation_executions.py`、`src/server/services/automations/*`、`src/server/app/setup.py`
- 前端：`Main.tsx`、`navItems.ts`、`useNavItems.ts`、`Automations/utils/status.ts`、`types/automation.ts`、`types/sse.ts`、各语言文案

**对照：** PK-knowledge-governance-autonomy.md 的 PK.14–PK.20、PK-UI.1–PK-UI.6；README 的 O11、L10、L11、L17；P0F.3。

**检查项：**
- [ ] **次数限制：**
  - 列出所有出现 `max_failures` 的地方（已知 `schedule.py` 11 处、`director_tools.py`、`bootstrap_watch.py`）；
  - 确认上游默认值 3 在不写字段时会生效（`src/server/models/automation.py` 第 499 行，`automation_executions.py` 的 `_count_strike`）；
  - 查有没有其他计数类的限制。
- [ ] **零成本预检：**
  - 判定为跳过时确实不产生任何费用：不建线程、不调用模型、不经过额度检查；
  - 该唤醒时一定会唤醒，不会因为探测出错而一直跳过；
  - 探测出错时怎么处理，有没有明确的规则；
  - 预检状态存在 Postgres 里，多个进程不会重复唤醒；同一件事按 `(kind, ref)` 去重。
- [ ] **新状态 `PRECHECK_SKIPPED`：** 界面、结算、调度器都能正确处理，不会被当成失败，也不会卡住下一次运行。
- [ ] **退避：** 只按时间计算，即停滞时长的一半，最短 24 小时、最长 30 天；有新证据时清零。核对计算是否正确。
- [ ] **研究总监：**
  - 排序时考虑当天剩余的额度，不会派出超过额度的任务；
  - 任务派给正确的公司工作区；
  - 交给 PM 的事项不会在后台继续花钱。
- [ ] **权限：** 自主权限只限于两项：自动批准交接，分析师自建 `dalton-*` 自动任务。没有扩大到其他操作。
- [ ] **不影响上游：** 非 Dalton 的自动任务行为和上游完全一致。
- [ ] **时区：** 议程的到期日、研究总监的每日调度、财报日期，都按 Asia/Shanghai 处理。
- [ ] **界面隐藏：** `VITE_DALTON_MODE` 隐藏行情和看板后，没有留下死链接，直接访问这些地址也不会报错。

### R6 原生接入的 Antigravity、Muse、xAI（优先级：严重，涉及账号安全）

**范围：**
- 提交 `3e9cc996`（Antigravity）、`9a03d3ac`（Muse）、`7670a03f`（xAI）的全部文件
- 包括 `src/llms/extension/antigravity.py`、`src/server/services/antigravity_oauth.py`，以及 Muse 和 xAI 的对应文件
- 这些提交对 `src/llms/manifest/{models.json, providers.json}`、`src/llms/llm.py`、`src/llms/extension/__init__.py`、`src/server/services/llm/clients.py`、`src/server/app/{oauth.py, api_keys.py, setup.py}` 的改动
- 前端：设置向导、`ConnectedAccounts`、`OAuthConnect`、`useConfiguredProviders`、`brandArt`、各语言文案

**对照：** 上游的 Claude 和 ChatGPT 订阅实现是参照标准：`src/server/services/claude_oauth.py`、`codex_oauth.py`、`src/llms/extension/anthropic_oauth.py`、`codex.py`。README 的 O13、O14、L1、L2；P0N-native-models.md。

**检查项：**
- [ ] **登录流程的安全：**
  - PKCE 实现正确；
  - `state` 参数有校验，能防止 CSRF；
  - 本机回调服务只监听本机地址，用完就关闭；
  - 申请的权限范围没有超出需要。
- [ ] **令牌存储：** 和上游一样加密保存（`src/server/database/encryption.py`），不存明文。
- [ ] **令牌刷新：** 和上游一样用 Redis 锁防止多个进程同时刷新（`oauth_refresh_wait.py`）；刷新失败时有明确的处理。
- [ ] **令牌泄露：** 日志、异常信息、SSE 事件、接口响应里都不会出现令牌。
- [ ] **协议转换：**
  - Antigravity 的 `v1internal` 请求格式、SSE 解析正确；
  - 工具调用在两个方向上的转换正确，包括工具调用 ID；
  - 多轮工具调用时，Gemini 的"思考签名"能正确回传；
  - 流式输出的增量拼接正确；
  - Muse 和 xAI 也按以上各项检查。
- [ ] **用量和费用：** 写入 `conversation_usages`，能被本地日预算读到；订阅类模型的现金成本为 0；xAI 的价格条目正确。
- [ ] **错误分类：** 供应商错误能被上游的容错中间件正确识别，比如限流、额度用尽、凭据失效。重试不会导致重复计费。
- [ ] **推理强度：** 映射正确。
- [ ] **模型清单：** 上下文长度、最大输出、能力标记、是否显示，都正确。这些条目要按 P0N 移进 overlay。
- [ ] **多 worker：** 没有把进程内的令牌缓存当作状态依据。
- [ ] **测试覆盖：** 关键路径有测试，包括登录、刷新、工具调用往返、错误分类。

### R7 skill、workflow、覆盖名单与迁移脚本（优先级：高，决定研究质量）

**范围：**
- `plugins/dalton/**`：22 个 skill，包括 `dalton-conventions` 和它的 references、财务镜头脚本，以及 `agents/*.md`
- `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 和 `registry.py` 的挂钩
- `workflows/*/workflow.js`（6 个）
- `docs/dalton/dev-guide/automation-templates/*`
- `config/dalton/coverage.yaml`、`scripts/dalton/build_coverage.py`
- `scripts/dalton/{init_company_workspace.py, migrate_legacy_knowledge.py}`

**对照：** P0D-P1-data-and-onboarding.md 的 P1.1–P1.5；PS-analyst-skills-and-workflows.md 全部；blueprint 第 3 节。

**检查项：**
- [ ] **workflow 和引擎的真实接口一致：**
  - 引擎代码在 `src/ptc_agent/agent/middleware/background_subagent/workflow/{engine.py, prebuilt.py, prelude.js}`，用法说明在 `plugins/langalpha_service/skills/run-workflow/SKILL.md`；
  - 用到的函数都存在，参数正确；
  - 派出的子智能体次数不超过上限（64 次，并发 8，单个子智能体 30 分钟）；
  - 引用的子智能体类型，比如 `dalton-evidence`，确实存在；
  - 没有写死模型名；
  - 出错时有处理；
  - 每个文件都过 `node --check`。
- [ ] **skill 注册：** 每个 `SKILL.md` 的 frontmatter 都有效；名字不和 LangAlpha 自带的 skill 重复；依赖关系（`requires`）和斜杠命令一致。
- [ ] **skill 内容是否真的写进了基金的方法论**（最重要）。逐个对照 PS 分册，判断是扎实还是空洞的模板：
  - bull-bear：v4.2 的结构是否完整（共识分三类、top debates 五段式、预期修正图、催化剂、财务体检、Variant perception、一句话结论）；
  - deep dive：12 问是否完整，四条取证支线是否齐全；
  - 财报前瞻：三项优先级（真实门槛、高频数据、核心 debate）；
  - 财报快讯：同比用上一季度的增速、指引和上季实际发布的比、beat/miss 用同一 GAAP 口径；
  - 股价异动：六个步骤，以及用同组和板块 ETF 区分 beta 和 alpha；
  - 周度复盘：Core/Watch 分层，"EPS 修正 vs 估值倍数"的归因公式；
  - conviction call：预期差三问、三情景 IRR、仓位级别、允许说"暂无高 conviction"；
  - 方法纪律：来源身份分层、"本轮未取得不等于未披露"、"提问口径，管理层未确认"。
- [ ] **没有 OpenClaw 运行时的残留：** 飞书分块写入、25 分钟上限、写死的模型名、人名和账号。
- [ ] **没有会诱导编造数字的指令**，比如"缺数据时合理估计"却不要求标注为估计。
- [ ] **没有次数限制**，比如模板里的 `max_failures`。
- [ ] **财务镜头脚本：**
  - 净现金（两种口径）、隐含资产、股东回报 R1/R2/R3 的计算，和基金原 skill 的说明一致（原 skill 在 `~/.openclaw/workspace/skills/findata-{netcash,hiddenasset,return}/`，只读）；
  - PS.2 列出的四个已知移植 bug 是否修了：港币汇率对、隐含资产里未换算的价格、评分显示的四舍五入、伪造的 52 周高低点。
- [ ] **覆盖名单：** Core 27 家、Watch 78 家；SPCX 在 Core 里；美团是 `3690.HK`。
- [ ] **迁移和初始化脚本：**
  - 重复运行结果不变；
  - 对来源只读：OpenClaw 的状态文件，以及旧 Dalton 的 `core.sqlite`（要用只读方式打开）；
  - 迁出的文件符合 PK.2 的结构定义；
  - 来源信息有保留。

## 3. 交付物

每个模块交一份 `docs/dalton/dev-guide/review/R<编号>-<日期>.md`：

```markdown
# R<编号> <模块名> 审查结果
审查人：  日期：  代码版本：7670a03f

## 结论
一句话：可以进入真实运行验收 / 需要先修以下问题。

## 问题（按严重程度排序）
| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |

## 已确认没问题的关键点
- …

## 疑似问题（未能确认）
- …
```

lead 汇总 7 份结果，把需要修的问题转成修正任务，放进 `P0F-acceptance-fixes.md`。

## 4. 建议的审查顺序

人手不足时，按这个顺序：

1. **R2 预算**：钱
2. **R4 知识层与发布**：正式成果的可信度
3. **R6 原生供应商**：账号安全
4. **R1 broker**
5. **R3 数据**
6. **R7 skill 和 workflow**
7. **R5 自主议程**
