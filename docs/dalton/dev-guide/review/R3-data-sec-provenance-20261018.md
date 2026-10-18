# R3 researchdata 桥接、SEC 数据、来源记录 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

范围：`scripts/dalton/{researchdata_bridge.py, source_identity.py, install_bridge.sh, check_bridge.sh, launchd/}`、`plugins/dalton_data/**`、`src/dalton/provenance/**`、上游挂钩（`mcp_client_runtime.py` / `config/core.py` / `tool_generator.py` / `tool_overlay.py` / `provenance/middleware.py`）、迁移 d004、前端 SourcesPanel / ResearchDocSource、`tests/unit/mcp_servers/agent_docstring_lock.json`。对照材料：dev-guide README（O6/O8/O10、L5/L6）、P0D-P1 v0.3、P0F。审查全程只读；对 EDGAR 的公开只读请求共约 10 次（带 User-Agent、≤5 req/s、缓存指向临时目录）；OpenClaw 侧仅静态读取与本地 127.0.0.1 tools/list 探测，未消耗 AlphaEngine 全文配额、未触碰任何凭证。

## 结论

桥接的安全基本面（仅绑 127.0.0.1、令牌常量时间比较且不落日志、无 shell 子进程、handoff 路由三处过滤、tools/call 信号量限并发）和上游超时补丁的"按服务名生效"实现都正确；dalton_sec 的期间归一化用 3 家公司 6 个指标对 EDGAR 实测全部命中（含非日历财年、单季与累计区分、Q4 推算），来源映射离线构造 6 组用例（含截断 JSON 回退）逐一正确，基金 wiki 无任何写入路径，docstring 锁文件相对上游只新增且全是 `dalton_sec` 键。**没有严重/高危问题**；3 个中危：桥启动/健康检查路径会把上游 stderr 尾部落进日志（违反 P0-D.1 的凭证卫生决定）、桥的路由超时表与 O10/P0-D.1 决议的 240/600 口径不一致、`_declared_sources` 信任 agent 可写的 mcp_trace（第一期降级设计继承的缺口，建议 P3 收口）。另有一个部署耦合提醒：`call_timeout_s: 300` 只在 `APP_ENV=dalton` 时生效。

测试：`tests/unit/mcp_servers`（含锁）482 项、`tests/unit/dalton` + `test_dalton_sec_periods.py` 80 项、provenance 中间件 1 项全绿；前端 `SourcesPanel.test.tsx` 31 项全绿。`check_hooks.sh` 当前失败于 `src/llms/manifest/`（R6 范围，见 R6 报告）；R3 范围内所有上游改动均带 DALTON-HOOK 标注。

问题计数：严重 0；高 0；中 3；低 6；疑似 5。

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 中 | `scripts/dalton/researchdata_bridge.py:230-232`（泄漏点 564-566、723-725） | 上游子进程 stderr 尾部（≤400 字符）被拼进异常消息，启动探测和 healthz 探测失败时整条消息写入 `~/Library/Logs/dalton-researchdata-bridge.log`。P0-D.1 明确"stderr 不回传也不落日志，因为 gateway 的 stderr 可能含凭证线索" | `researchdata_mcp.py` 崩溃且 stderr 带凭证线索（如代理 URL、cookie 报错行）→ 启动时 `logger.warning("Startup initialize probe warning: %s", exc)` 或 healthz 的 `Healthz init probe failed: %s` 把含 stderr 尾部的消息落盘。tools/call 路径不泄漏（外层只记异常类型名、500 响应体不含消息），仅这两处泄漏；定"中"是因为有条件触发且仅限本机日志 | 日志只记 `type(exc).__name__`；或 RuntimeError 里不携带 stderr，改为单独计数/长度指标；至少对 err_tail 做一次凭证模式过滤 |
| 2 | 中 | `scripts/dalton/researchdata_bridge.py:41-57, 165-181` | 路由超时表与 O10 / P0-D.1 v0.3 决议不一致：决议是"其他路由上限 240 秒、长文类（guidepoint 全部、transcript、库 read、sec read-item/search-text/attachments/financials）上限 600 秒、缺省即上限"；实现里除 `alphaengine:get_document`（240/600）外全部是 60-90 秒默认、120-180 秒上限 | Guidepoint 长访谈或 company-wiki 全文 read 正常耗时 130-200 秒时被桥在 120/180 秒处杀掉，返回 `status: timeout`，而调用方预算（275 秒）本可容纳；违反"超时只兜挂死、不限制正常慢请求"的原则。若实测这些路由普遍 <120 秒则降级为低，需重测后二选一 | 按决议把"read/get_document 类操作"归入长文类（route+operation 判定，现在只有 `alphaengine:get_document` 一个键），其他路由缺省抬到与决议一致；或实测后请 owner 修订 P0-D.1 的口径表 |
| 3 | 中 | `src/ptc_agent/agent/middleware/provenance/middleware.py:771, 834` + `src/dalton/provenance/declared_sources.py:81-196` | `_declared_sources` 从 mcp_trace 的 `result_body` 展开，而 trace 是 agent 可写的（上游注释自认 "agent-authored"）：沙箱代码可以手写 `server="researchdata"` 的 trace 条目 + 伪造 body，制造带"一手披露/专家访谈"徽章的 research_doc 来源卡 | 恶意或错乱的 agent 在 execute_code 里伪造 artifact，Sources 面板显示从未发生过的文档引用，污染证据台账的可信呈现。上游对 mcp_tool 行同样信任 trace（等价风险），但新徽章把伪造收益抬高了；属第一期降级版（P0-D.3）已接受、P3（d004/P0-D.3b）应收口的缺口 | P3 时把声明式来源的提取移到服务端可认证的通道（如桥/服务器签名、或经 execute_code 结果校验哈希）；过渡期可在 expand 时校验 result_sha256 与 body 一致性（伪造成本提高一档） |
| 4 | 低 | `scripts/dalton/researchdata_bridge.py:378`（sem 仅包 tools/call）对 641-678 | `initialize` / `tools/list` 路径绕过并发信号量，可无限并发起子进程（各 ≤15 秒自限） | 持令牌的调用方连续打 initialize → 并发 N 个 python 进程；正常客户端每解释器一次，风险低（localhost+令牌） | `ensure_initialized`/`get_filtered_tools` 套同一信号量或独立小信号量 |
| 5 | 低 | `web/src/types/sse.ts:135`、`web/src/pages/ChatAgent/components/SourcesPanel.tsx:43-53` | `data_api`（estimates/fred/treasury 的 source_type）未加进前端 `ProvenanceSourceType` 联合、GROUP_ORDER 和 i18n；面板按未知类型兜底渲染成 "Data Api" 分组 | researchdata 一致预期/FRED 来源显示为无翻译的兜底组名（功能不丢，观感差） | 联合类型加 `'data_api'`，GROUP_ORDER 排在 `research_doc` 后，补 `chat.sources.groups.data_api` 文案 |
| 6 | 低 | `scripts/dalton/source_identity.py:425`（`_declare_sec` url 恒为 None）、`:388`（transcript `as_of_basis="call_date"` 却取 publish_time） | researchdata sec 路由的来源卡不透传 rows 里的 URL；transcript 的基准标签与实际取值字段不符 | sec 来源卡永远无链接（dalton_sec 侧算了 URL 但第一期也不进 provenance，属 L6 已接受损失）；transcript 若 publish_time≠通话日，as_of 标签误导 | `_add_filing` 透传 `r.get("url")`；transcript basis 改 "publish_time" 或优先取 call date 字段 |
| 7 | 低 | 文档与实现漂移（P0D-P1 v0.3 §P0-D.1 对 `scripts/dalton/researchdata_bridge.py`） | 环境键名不一致（文档 `DALTON_BRIDGE_BIND/RESEARCHDATA_PYTHON/MAX_CONCURRENCY/DEFAULT_TIMEOUT_S/LONG_TIMEOUT_S/DALTON_ALPHAENGINE_DAILY_QUOTA` vs 实现 `DALTON_BRIDGE_HOST/DALTON_OCW_PYTHON/DALTON_BRIDGE_MAX_INFLIGHT/CALLER_BUDGET_S`，且配额告警键未实现）；healthz 载荷比规格少（无 researchdata present/alphaengine health/guidepoint tcp/quota_alert）；`_bridge.timeout_clamped` 标注、排队 10 秒回 `-32000 bridge_busy`、tools/list 缓存 10 分钟均未实现（缓存按 server_version 失效）；initialize 回显集合差异（多 `2024-11-05`、少 `2025-06-18`，与实际客户端 `_LEGACY_OFFER=2025-11-25` 兼容） | 按文档排障的工程师找不到对应配置键；长排队时调用方只会干等到自己的超时 | 更新 P0-D.1 为实现的键名与行为，或补齐告警/忙错；两者取一，避免规格与代码长期分叉 |
| 8 | 低 | `scripts/dalton/check_hooks.sh:121-136` | P0F.2 承诺的"校验 agent_docstring_lock.json 相对上游只有新增、新增键属 dalton_*"未实现；基线仍优先硬编码的 `9a03d3ac` 且接受 `DALTON_BASE_REF` 手工传入，与 P0F.1"一律取 merge-base、不接受手工基线"相悖（当前即因 manifest 误报/漏报失败，属 R6 范围） | 锁文件本身合规（见下"已确认"），但失去自动防回归：将来混入对上游键的改动不会被发现 | 按P0F.1/P0F.2 补脚本：基线取 `git merge-base`；对 lock 文件做 jq/python 只增校验 |
| 9 | 低 | `agent_config.dalton.yaml:26-27`（依赖 `.env` 的 `APP_ENV=dalton`）对 `mcp_client_runtime.py:62` | researchdata 的 `call_timeout_s: 300` 仅在 `APP_ENV=dalton` overlay 生效；漏配时沙箱客户端预算回落 65 秒，而桥允许到 275 秒——超过 65 秒的调用在客户端半路断开（桥还在跑，白耗上游调用） | owner 忘在 `.env` 写 `APP_ENV=dalton`（P0-platform 文档步骤）→ 长文档读取全部表现为客户端传输错误而非桥的类型化超时 | `install_bridge.sh` 末尾校验 `.env` 含 `APP_ENV=dalton`（或后端启动时校验 researchdata 的 call_timeout_s ≥ 桥 CALLER_BUDGET−25），不满足则显式报错 |

## 已确认没问题的关键点

- **桥接安全（检查项 1）**：默认只绑 `127.0.0.1:8960`（`DALTON_BRIDGE_HOST` 可改，安装脚本硬写 127.0.0.1，LAN 不可达，沙箱经 `host.docker.internal` 走宿主回环）；令牌 `hmac.compare_digest` 常量时间比较、不匹配一律 404、任何日志不含路径/令牌（桥自身与 `mcp_registry._expand_url` 的 unexpanded 告警都只记占位符）；`check_bridge.sh` 全程不回显令牌；env 文件 chmod 600、repo `.env` 已 gitignore；子进程 `asyncio.create_subprocess_exec` 列表参数、无 shell、工具参数经 stdin JSON 传入（researchdata_mcp 侧 schema 校验 enum/additionalProperties，参数无法变成 URL 或文件路径）；桥自身不发起任何外呼，无法被参数诱导请求任意网址；handoff 路由三处过滤（tools/list 枚举、tools/call 拒绝并回 `isError`、catalog 输出再滤），名单与 `$OCW/config/data_sources.json` 的 `kind∈{agent_tool,skill}` 逐一吻合（9 条，异常兜底硬编码名单当前与实际一致）；tools/call 有信号量（默认 2）。
- **子进程管理（检查项 2）**：超时 `proc.kill()`+`await proc.wait()`，无僵尸；每请求全新子进程、无跨请求状态，桥重启无残留；被桥 SIGKILL 时 OpenClaw 侧 data_gateway 以自身 `killpg`（`start_new_session`）在 ≤10 秒内自清（见疑似 2）。
- **MCP streamable HTTP 协议（检查项 3）**：POST JSON-RPC、通知回 202 空体、GET/DELETE 405（客户端回落纯 POST）、未知方法 `-32601`（客户端回落旧式 initialize，`_LEGACY_OFFER=2025-11-25` 在回显集合内，握手兼容）、ping 正常；请求体 256 KiB 上限；逐段 15 秒读超时防慢连接常驻。
- **超时（检查项 4）**：上游补丁按服务名生效且只影响配置了的服务——`MCPServerConfig.call_timeout_s`（默认 None，ge10/le900）仅在设置时进沙箱客户端配置（`tool_generator.py:701`），运行时未携带该键一律回落 `_HTTP_CALL_BUDGET=65.0`（`mcp_client_runtime.py:169,192,1569,1573`）；price_data/fundamentals/sec_tools 等其他服务行为不变；`tool_overlay.py:281` 把键列入 executable_keys 保证改超时会触发重同步；`supervisor_runtime/daemon.py` 的同名参数为上游原有（diff 为空）；researchdata=300 经 `agent_config.dalton.yaml` overlay 按名覆盖（`config/utils.py` 单层键覆盖，保留插件 URL 只加键）；桥侧 `实际上限=min(路由上限, CALLER_BUDGET−25)=275 秒 < execute_code 默认 300 秒`，不超过调用方预算。
- **wiki 只读（检查项 5）**：暴露面只有 `fetch|plan` 两 mode，全部工具标注 `readOnlyHint:true`；company-wiki 仅 search/read；data_gateway 只转发"已登记的只读操作"；本仓库侧无任何 wiki 写入路径；插件 instruction 明示只读。
- **来源记录（检查项 6，离线构造 6 组实测）**：company-wiki 三类身份推断（专家访谈→expert/Guidepoint、管理层→company、无关键词→internal_note/Fund Wiki）、alphaengine get_document（`structuredContent.metadata` 的 doc_id/publish_time/sources[0]→broker_report，`IND_SECOND_5FA442AE`→company）与 search_library、guidepoint（`library_ts:` 去前缀、重复行去重、publisher 只含角色绝不含 full_name——合规要求）、sec rows（accession 去重、as_of=filed、title 带报告期）、estimates→street_estimate、fred→official_data 且 as_of 取最新观察日；`expand_declared_sources` 白名单（researchdata/dalton_sec）、身份/类型校验、截断 JSON 的 `_declared_sources` 前缀解析回退全部正确；`_declared_sources` 置于 JSON 首部以保证 64 KiB 截断后存活。
- **dalton_sec 数据正确性（检查项 7）**：单季/累计按天数分类（75-105→单季、165-195→H1、255-285→9M、350-380→FY），季度查询只匹配单季事实，META 2025Q2 返回 90 天单季值而非 H1；非日历财年映射（MSFT fye=6 来自 submissions `fiscalYearEnd`，FY2025Q1=日历 2024Q3；`_effective_year_month` 归一 52/53 周越月期末）；Q4 推算=FY−9M，标 `derived:true`+`derivation_method:"FY_minus_9M"`，components 记录两份申报的 form/filed/accession 且都注册进 `_declared_sources`，per-share 单位不做减法；单位只取公司披露（优先 USD 但不换算，unit 字段透出）；重述双值保留（as_reported 带 `restated_in_later_filing/latest_val/latest_filed/latest_accession_no`，latest 反之）；同 (filed, accession) 去重且保留带 frame 的行；EDGAR 访问带 UA、0.21s 节流、TTL 缓存、缓存写入失败静默不影响结果。
- **工具说明快照（检查项 8）**：`git diff 8e0dce94 -- tests/unit/mcp_servers/agent_docstring_lock.json` 只有 2 个新增键 `mcp:dalton_sec:get_company_facts`、`mcp:dalton_sec:list_filings`，上游既有键零改动，符合 P0F.2 的实体要求（自动校验未实现见问题 8）；482 项 mcp_servers 单测（含锁）通过。
- **迁移 d004**：幂等（ADD/DROP COLUMN IF NOT EXISTS），链 d001→d006 完整；`dalton_source_url/dalton_as_of` 目前无任何读写方，与 L6"第一期不改数据库"一致（列为 P3 预留）。
- **多 worker / 通用**：桥是 launchd 单进程，无跨进程状态；服务端挂钩全是纯函数、无模块级可变状态；usage 文件在 asyncio 锁内 tmp+replace 原子写；无 `except: pass` 吞错（declared_sources 的兜底 `except Exception: return []` 为中间件"永不抛错"契约，建议加 debug 日志但可接受）；R3 范围所有上游改动均带 DALTON-HOOK 标注。

### EDGAR 抽核结果（公开只读 HTTP，UA=DaltonResearch code-review）

| 公司 | 指标（resolved concept） | 期间 | 脚本值 | EDGAR 原文值 | 结论 |
|---|---|---|---|---|---|
| MSFT | revenue（RevenueFromContractWithCustomerExcludingAssessedTax） | FY2025Q1（2024-07-01~09-30，91 天，10-Q 0000950170-24-118967） | 65,585,000,000 USD | 65,585 百万美元 | ✅ 非日历财年（6 月截止）映射正确，fiscal=FY2025Q1/cal=2024Q3 |
| MSFT | net_income（NetIncomeLoss） | FY2025（2024-07-01~2025-06-30，10-K 0000950170-25-100235） | 101,832,000,000 USD | 101,832 百万美元 | ✅ |
| META | revenue | 2025Q2（2025-04-01~06-30，90 天，10-Q 0001628280-25-036791） | 47,516,000,000 USD（单季，非 H1 89,830M） | 47,516 百万美元 | ✅ 单季与累计正确区分 |
| META | eps_diluted（EarningsPerShareDiluted） | 2025Q2 | 7.14 USD/shares | 7.14 美元 | ✅ |
| AAPL | revenue | FY2025Q4（截至 2025-09-27；XBRL 无独立 Q4 事实→推算） | 102,466,000,000 USD = FY 416,161M − 9M 313,695M，`derived:true`，两份申报（10-K 0000320193-25-000079、10-Q 0000320193-25-000073）均入来源 | 102,466 百万美元（与公布 Q4 营收一致） | ✅ Q4 推算与申报文件记录正确 |
| AAPL | operating_income（OperatingIncomeLoss） | FY2025Q4（推算） | 32,427,000,000 USD = 133,050M − 100,623M | 32,427 百万美元 | ✅ |

附带：`_edgar_archive_url` 生成的 META 10-Q index URL 实测 HTTP 200，格式正确。

## 疑似问题（未能确认）

1. `_effective_year_month`（`dalton_sec_mcp_server.py:223-228`）把任何"期末日在 1-6 日"的期间回拨一个月（52/53 周财年归一）——若某公司某期间真实地结束于 1-6 日（罕见，如部分银行 1 月 1 日年结），会被归入上一个月/季度。抽核的 3 家未触发；覆盖名单扩大时留意。
2. 桥 SIGKILL researchdata_mcp 后，其孙进程 data_gateway 靠自身 `killpg` 在 ≤10 秒内自清（代码路径已核对，`start_new_session` + `timeout+10`），未做实际挂死场景的活体实验。
3. `_infer_wiki_identity` 的中文关键词启发式在构造用例上全部正确，但未对真实 wiki 语料（1,086 篇）做批量回放验证误判率。
4. alphaengine `IND_SECOND_5FA442AE` → identity "company" 是硬编码类型 id，未能对 AlphaEngine 分类表核实其语义。
5. researchdata_mcp 对"hidden 路由"（如 `sales-mail`）接受缓存客户端显式传名调用（`researchdata_mcp.py:73-76` 兼容子句）：枚举里不可见但按名可调，均为只读、同一信任域，属信息性。
