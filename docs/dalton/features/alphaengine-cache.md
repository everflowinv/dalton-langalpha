# AlphaEngine 检索全系统缓存与额度管理

宿主机 `scripts/dalton/researchdata_bridge.py` 为 AlphaEngine 提供独立双额度核算、单家公司一次检索软提醒、团队级 v4 检索缓存、7 天旧缓存兜底与单进程熔断保护：

- **双独立额度前提**：
  1. **新文档阅读额度**：首次打开一篇新文档（`get_document`）计入一次阅读额度；同一 `doc_id` 再次打开或按 `offset` 续读时直接复用上游已解锁文档，消耗零额外阅读额度。
  2. **检索独立额度**：每次上游检索（`search_library` / `transcript`）消耗单独的检索额度。系统通过工具规则引导「单家公司检索一次完成」、内存软提醒与团队级缓存节省检索额度，同时确保不同公司或新主题的检索正常执行。

## 1. 存储位置与容量上限

缓存模块 `scripts/dalton/alphaengine_search_cache.py` 采用 SQLite WAL（Write-Ahead Logging）模式落盘：

- **默认路径与权限**：`{cache_dir}/alphaengine_search_cache.sqlite3`，支持通过环境变量 `DALTON_ALPHAENGINE_CACHE_DB` 覆盖；建库时文件权限设为 `0600`。
- **缓存表容量与保留窗口**：`ae_search_cache` 表上限为 **`DEFAULT_MAX_ROWS = 10_000`** 行，每 **`CLEANUP_EVERY_WRITES = 50`** 次写入触发一次清理。条目过期后继续保留 **`STALE_RETENTION_S`（7 天）**，用于故障或熔断期间的旧缓存兜底（`get_stale_sync`）。
- **已读文档表（`ae_read_docs`）**：同一 SQLite 数据库内的轻量表 `ae_read_docs(doc_id, first_read_at, last_read_at, read_count)` 记录已成功打开过的 `doc_id` 与首次读取时间，作为首次打开与重复打开的判定依据。

## 2. 缓存键 v4 归一化、条数切片与 v3 迁移

缓存键版本号为 **`CACHE_KEY_VERSION = 4`**，由 `prepare_alphaengine_search_request` 生成全系统共享的团队级键（`scope = "team"`）。

### 2.1 v4 归一化规则

系统对有序 JSON 对象计算 SHA-256 生成 `cache_key` 与 `base_key`：

1. **查询词（`query`）**：执行 Unicode NFKC 全角转半角、去除首尾空白、折叠连续空白为单个空格，并将英文统一转为小写。
2. **关键词与筛选列表（数组筛选参数）**：若参数中包含 `type_ids`、`source_ids`、`document_codes`、`document_categories`、`markets`、`stock_group_ids`、`first_regions`、`second_regions`、`company_names`、`company_short_names`、`industry_names`、`source_names`（以及透传的 `keywords`），均按规范化后有序去重，消除传参顺序差异。
3. **日期参数（`date_from` / `date_to`）**：纯日期（如 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD`）统一规范化到天（`YYYY-MM-DD`）；带有具体时间或时区的（例如 `2026-11-01T00:00:00Z`、`2026-11-01T08:00:00+08:00`）保留完整标准化 UTC ISO 字符串（如 `2026-11-01T00:00:00Z`），与上游时间边界语义保持一致。
4. **条数向下切片（`base_key`）**：`base_key` 省略 `limit`。当已有同 `base_key` 且条数充足的缓存条目时，`slice_cached_payload` 直接截取所需条数返回，并附带 `SLICED_CACHE_NOTE`。

### 2.2 v3 到 v4 的迁移与清理时机

- **双读懒迁移（Lazy Dual-Read）**：由于缓存键为单向 SHA-256 哈希，升级后采用在线懒迁移。`AlphaEngineSearchCache.get_sync` 与 `get_stale_sync` 先查询 v4 键；v4 键查无结果时回退查询对应的 v3 键，命中后保留原 `fetched_at`、`expires_at` 与 `retain_until` 立即回写一份 v4 条目。所有新写入统一使用 v4 键。
- **v3 清理时机**：缓存最长生命周期为 `TTL_OLDER_S（7 天）+ STALE_RETENTION_S（7 天）= 14 天`。v4 上线稳定运行满 **14 天** 后，存量 v3 条目会随 `retain_until` 到期由自动清理任务全部回收，届时即可移除 v3 回退读取分支。

## 3. 单家公司一次检索规则与软提醒（放行执行）

### 3.1 工具说明规则

在 `scripts/dalton/research_runtime/config/data_sources.json`（`alphaengine.rules` / `transcript.rules`）、`plugins/dalton_data/plugin.json` 及 bridge 工具描述中统一写入规则：

> 单家公司检索一次完成：检索一家公司时，把中英文公司名和股票代码用空格隔开写在同一个查询里，从返回列表挑选文档；已有结果时从现有列表筛选，报错时切换备用数据源；不同公司或新主题照常各自检索。AlphaEngine 按实际向上游请求的页数计检索额度，命中短时缓存不计；每页至少请求 20 条（`limit` 超过 20 时按 `limit` 请求，去重或筛选后不足 `limit` 条时会自动翻页，单次最多 5 页）。先用 `limit` 20 以内，不够再翻页，不要一次要 50 条再只看前几条。
>
> 查业绩会转写时把 `start` / `end` 收窄到业绩会前后几天，避免日期范围过宽导致连续翻页。

### 3.2 会话级 30 分钟软提醒（`AlphaEngineCompanySearchTracker`）

- **内存滑动窗口与条目上限**：状态仅保存在单进程 bridge 内存中，按会话（优先取 `thread`，缺省回退 `workspace`）维护 30 分钟滑动窗口（`COMPANY_SEARCH_WINDOW_S = 1800` 秒），每次调用（含失败与无会话请求）均先按时间清理并受条目上限约束：同公司签名记录、同词历史、文档清单三类各自跨会话最多 `COMPANY_SEARCH_MAX_ENTRIES = 5000` 条（三类合计最多 15,000 条），超出时淘汰最旧条目，不落盘。同一 `(会话, 公司)` 下每个查询签名仅保留最近一次时间戳，每个会话仅保留提示所需的最少信息（时间、返回条数、每家公司最多 10 条文档的 `doc_id` / 标题 / 日期）。提示计算与查询日志写入相互独立异常隔离：提示计算异常仅记 warning 并降级为不附提示；查询日志写入异常仅记 warning，有意保留已算出的 `advice` / `advice_previous_docs` 正常返回，二者均不影响检索结果返回和台账记录。
- **公司识别口径**：基于 `plugins/dalton/skills/dalton-daily-filing-alert/watchlist*.txt`、各行业 `universe.json` 与核心中英文别名表（`extract_companies_from_search`）匹配 `query` 与 `ticker`：
  - 纯 ASCII 股票代码严格区分大小写（仅认大写，例如 `NVDA` 命中而 `nvda` 不计入）。
  - 与常见英文词重名的代码（如 `APP`、`AI`、`ON`、`NOW`、`ALL`、`CAN`、`IT`、`META`、`UBER`、`SNOW`、`PATH`、`CARS`、`FAST`、`REAL`、`LOVE`、`OPEN`、`PLAY`、`TEAM`、`BOX`、`LIFE`、`ARM`、`FUN`、`COST`、`SHOP`、`SPOT`、`DASH`、`CART` 等）单独裸写时不触发，仅在查询同时包含该公司其他名称（如 `AppLovin`、`Meta Platforms`）或明确代码写法（`$APP`、`APP US`、`APP.O`）时命中。
  - 未匹配到已知公司的纯主题词跳过公司计数，仅受同词重搜提醒覆盖；调用方未写 `query`（仅传 `ticker`）的请求不参与同词判断，纯 `ticker` 的 `transcript` 请求跳过同公司计数但已返回的团队文档仍记入同公司文档清单。
- **同词重搜提醒（只提示、不拦截）**：同一会话 30 分钟内对调用方原始传入的同一规范化查询词（跨 `search_library` 与 `transcript`，忽略日期、条数、排序、筛选差异，且不以 bridge 折叠后的 `ticker` 充当查询词）再次发起非翻页检索（不带 `cursor`）时（`transcript` 忽略主题词、返回带 `query_ignored` 时，这个主题词不算本次查询词，不参与同词提醒），检索照常执行并返回全部结果，在返回对象附加 `advice`：`本会话 {N} 分钟前已用相同查询词检索过（上次返回 {M} 条）；请先从现有结果中挑选，确实需要不同时间段或更多结果时再检索`（不足 1 分钟写“不到 1 分钟前”）。缓存命中同样计入“上次检索”；命中 `_is_personal_scope_request` 的个人会议请求完全不进入同词历史，既不记录也不触发同词提醒。
- **同公司多次检索提醒与已有文档清单**：沿用原有同公司计数口径（含失败、`mode=plan`、个人会议与带查询词的续页刷新时间），同一会话 30 分钟内对同一家公司发起第 3 次及以上且为**新查询词**的检索时，在返回对象附加 `advice`，并将本会话此前该公司检索已返回的文档清单放在独立字段 `advice_previous_docs`（每条含 `doc_id`、`title`、`date`，去重、按最近一次返回排序、最多 10 条），并在 `advice` 中指向 `advice_previous_docs`。个人会议范围的请求（`_is_personal_scope_request`）不记录文档清单。
- **同时触发优先级与 `route="all"` 透传**：同词重搜与同公司多次检索分开判断；两者同时满足时仅附一条 `advice`（同词重搜优先，括号内附“，此前返回的文档见 `advice_previous_docs`”），`advice_previous_docs` 照常附带；`route="all"` 的 AlphaEngine 子腿将 `advice` 与 `advice_previous_docs` 同时附在顶层及 `payload["alphaengine"]`。

## 4. 上游保护（检索熔断）与失败兜底

### 4.1 检索熔断（单进程内状态）

`AlphaEngineSearchCircuitBreaker` 驻留在单进程运行的 `researchdata_bridge.py` 进程内存中（单进程单事件循环架构天然保证状态一致），并在以下规则下工作：

- **严格触发条件**：优先识别明确参数错误（如 `E_BAD_*` 或 `参数错误` 类别，即使消息带数字 `403` 也不触发）；仅当可缓存的 AlphaEngine 检索返回明确的额度或频率受限信号时才打开熔断——包括原文 `无查看权限或额度已达上限`；带 HTTP 语义的 `429`（`HTTP 429`、`status_code=429`、`Too Many Requests`）；`quota_exceeded`、`rate_limited`；以及明确表示耗尽或限流的中文写法（`额度已达上限`、`额度不足`、`超出额度`、`额度已用完`、`额度用尽`、`频率限制`、`频率过高`、`请求过于频繁`）。业绩会转写（`transcript`）翻页时，后续页返回这类错误也会打开熔断，已取得的部分结果照常返回但不写缓存。`auth_expired`、`paid_source_not_allowed` 以及单纯的 `forbidden` / `401` / `403` 不触发熔断，直接返回原错误；个人会议、`mode=plan`、`cursor` 翻页等不可缓存请求既不触发熔断也不受熔断拦截。
- **退避时长与复位**：初始时长 **60 秒**；冷却到期后若连续触发则每次翻倍（60s → 120s → 240s → 480s），上限 **10 分钟（600 秒）**；上游检索成功一次后立即复位。
- **并发名额后二次检查**：请求在排队取得并发槽位之后、发起上游 RPC 之前会再次检查熔断状态；若排队期间熔断已打开，则直接按熔断规则处理并立即释放槽位，避免无谓消耗上游额度。
- **熔断期间行为**：
  - 有可用缓存（含 7 天保留期内的过期条目）时，跳过上游请求并直接返回缓存，响应标注 `stale: true`、`stale_reason: "circuit_open"` 与 `cached_at`。
  - 本地尚缺可用缓存时，跳过上游请求并立即返回携带触发类别与原文摘要的明确错误：「AlphaEngine 检索暂停约 N 秒（上次错误：<类别>：<摘要>）」（`error_category = "权限或额度"`）。
- **read 操作独立性**：`get_document`（read）拥有独立的上游额度，始终直连上游执行，保持独立配额统计与响应路径。
- **健康状态观测**：`GET /healthz` 响应中的 `alphaengine_search_circuit_breaker` 字段实时输出 `open`、`remaining_seconds`（剩余秒数）、`trip_count`（累计触发次数）、`consecutive_trips` 与 `last_reason`；`alphaengine_get_document_today` 实时输出 `calls`（首次打开计额度文档数）、`first_open_calls`、`repeat_calls`（重复打开数）、`total_calls` 与 `chars`。

### 4.2 失败与超时前使用旧缓存

当出现以下任一情况且缓存存在同键的保留期内旧条目时，bridge 直接返回旧缓存，并在响应与 `_bridge_cache` 中标注 `stale: true`、`stale_reason` 与原条目抓取时间 `cached_at`（UTC ISO-8601）：

1. **上游失败兜底**：上游返回 **「上游不可用」** 或 **「权限或额度」** 失败时，返回旧缓存，分别标注 `stale_reason: "upstream_unavailable"` 或 `"quota"`。
2. **调用方截止前兜底**：共享单飞等待期间，若在调用方超时截止前保留余量（如 2 秒）仍未完成且本地存在同键旧缓存，则先向调用方返回旧缓存并标注 `stale_reason: "upstream_timeout"`；共享的上游任务继续在后台完成并在成功时回写最新缓存，其余等待者取消时后台任务继续跑完。读取旧缓存本身也受截止时间限制；`params.fresh` 为真时直接等待上游结果。无可用旧缓存时保持原超时返回行为。

## 5. 四档动态有效期（TTL）

`compute_ttl_seconds` 按**北京时间当天（`today_str`）**对照检索结束日期分四档计算有效期：

| 条件 | 常量名 | 有效期 | 用途 |
|---|---|---|---|
| 空结果 且 结束日 ≥ 今天 | `TTL_TODAY_EMPTY_S` | **1800 秒（30 分钟）** | 缩短当日空结果的缓存周期，方便新内容快速上线 |
| 非空结果 且 结束日 ≥ 今天 | `TTL_TODAY_NON_EMPTY_S` | **7200 秒（2 小时）** | 兼顾盘中高频复用与当日研究更新 |
| 结束日在最近 7 天内 | `TTL_RECENT_7D_S` | **43200 秒（12 小时）** | 近一周历史资料短周期缓存 |
| 更早历史区间 | `TTL_OLDER_S` | **604800 秒（7 天）** | 历史固定档案长周期复用 |

## 6. 并发单飞（Singleflight）与直通策略

### 6.1 并发单飞去重

`ResearchDataBridge._inflight_searches` 对相同 `cache_key`（强制刷新时为 `f"{plan.cache_key}#fresh"`）做单飞合并：

- 请求在查询 SQLite 前先登记 `_inflight_searches`，将「读缓存 → 调上游 → 写缓存」合并为单次领跑任务。
- 并发到达的同键请求共享 `asyncio.shield(result_future)`，领跑任务落盘后一起返回；跟随请求记为 `status="coalesced"`，仅消耗一次上游请求。

### 6.2 直通上游请求

以下三类请求直接访问上游：

1. **强制刷新（`params.fresh`）**：`params.fresh` 为真值时，常规时段跳过本地有效缓存读取，向 AlphaEngine 发起实时检索并写回覆盖缓存；熔断期间优先返回本地可用缓存。
2. **个人会议直通（`_is_personal_scope_request`）**：命中 `meeting_scope == "personal"`、`own_reservations_only` 为真，或 `document_categories`/`document_codes`/`type_ids` 包含 `personal_meeting`/`personal_meetings` 时，跳过团队缓存读写，直通上游。
3. **规划与游标翻页**：`mode == "plan"` 以及携带 `cursor` 的续页请求直通上游执行。由于上游翻页游标仅在约 15 分钟内有效，缓存层规定超过 `CURSOR_SAFE_AGE_S`（12 分钟）的缓存副本会剥离失效游标并附带提示（`STALE_CURSOR_NOTE` / `STALE_CONTINUATION_NOTE`），不再允许跨时段用旧游标续查。
   - **`dalton-thesis-delta` 按日期续采**：每日增量采集脚本（`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py`）跨轮仅保存已覆盖日期边界，游标只在单轮内使用（单次 `AE_PAGE_LIMIT = 20`，每轮上限 `MAX_CURSOR_PAGES = 5` 次请求）。采集时先对含 3 天回看的整窗请求：待扫描日期跨多天时整窗扫描最多使用 4 次请求（留 1 次给逐日推进），待扫描日期仅 1 天时整窗扫描可使用全部 5 次请求；超出后再按最旧日期逐日推进。仅当单日真正用尽本轮预算且尚无进度时才越过该日（越过后该日遗漏的文档仅能在后续 3 天回看窗口内补采）；遇到取数失败、旧缓存兜底或游标过期时，未取得有效进度则保留原覆盖边界，逐日推进中已完成的日期继续计入进度。积压超过 `MAX_AE_BACKLOG_DAYS = 7` 天时截断并上报，且每段首页请求均带 `params.fresh = true`。

## 7. 用量台账、团队检索文本日志与统计报告

### 7.1 调用台账与检索文本日志

- **用量台账**：默认写入 `~/Library/Logs/alphaengine_usage.jsonl`（单文件满 32 MiB 自动轮转为 `.1`，支持环境变量 `DALTON_ALPHAENGINE_USAGE_FILE` 覆盖）。每次成功 `get_document` 记录 `first_open`（区分首次打开计入额度与重复打开），每次熔断记录 `breaker_tripped` 与 `breaker_open`。
- **团队检索文本日志（`AlphaEngineTeamQueryLogger`）**：
  - **记录范围**：仅记录团队范围检索（`search_library`、`transcript` 及 `route="all"` 的 AlphaEngine 子腿）的查询文本与规范化参数；命中 `_is_personal_scope_request` 的个人会议请求完全跳过文本日志。
  - **存储、轮转与保留期**：默认按北京时间日粒度轮转写入 `~/Library/Logs/alphaengine_search_queries/YYYY-MM-DD.jsonl`（支持环境变量 `DALTON_ALPHAENGINE_QUERY_LOG_DIR` 覆盖），每次写入自动清理超过 **7 天**的历史日志文件。
  - **权限与可见范围**：目录权限设为 `0700`，日志文件权限设为 `0600`；仅宿主机管理员与 `alphaengine_usage_report.py` 报告脚本读取，桥接 HTTP 状态接口与沙箱工具均保持不可见。

### 7.2 统计报告口径（`alphaengine_usage_report.py`）

```bash
# 查看用量、额度消耗与重复检索汇总报告
python3 scripts/dalton/alphaengine_usage_report.py

# 指定查询日志目录或输出机器可读 JSON
python3 scripts/dalton/alphaengine_usage_report.py ~/Library/Logs/alphaengine_usage.jsonl --json
```

报告输出以下核心维度：

- **双额度核算（按天）**：
  - **检索（search）**：按天区分「检索次数」（search 调用总数）、「检索消耗额度」（实际请求上游的 search，带 `pages_scanned` 多页扫描时按实际页数累计，未带该字段按 1 计）、「失败次数」（`ok=false` 的调用数）以及命中缓存或单飞节省次数（`search.cache_saved`）。
  - **文档读取（`get_document`）**：按天区分首次打开新文档次数（`read.first_open`，计入阅读额度）与重复打开次数（`read.repeat`，复用已读文档）。
- **可缓存检索命中率**：剔除 `bypass`、个人会议及未进入缓存层的调用，统计 `(hit + coalesced) / 可缓存检索总数`。
- **按天的失败类别分布与熔断统计**：按北京时间汇总每天各类 `error_category` 分布、当日 `breaker_trips` 次数与总拦截次数。
- **按调用方检索与失败率排行**：按工作区（优先）或用户统计检索次数、失败次数与失败率并降序排行。
- **同一公司的多次检索排行与重复查询文本**：基于团队检索文本日志，按会话与公司统计多次检索及对应查询文本列表，并列出重复次数最多的查询文本。
