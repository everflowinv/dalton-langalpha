# AlphaEngine 检索全系统缓存

本文说明宿主机 `researchdata_bridge.py` 中接入的 AlphaEngine 团队级检索缓存架构，涵盖 SQLite 存储格式、v3 缓存键规则、四档动态有效期、并发单飞（singleflight）去重、`params.fresh` 强制刷新、个人会议不缓存规则及命中率与配额监控日志；调整缓存策略、排查旧数据未更新或统计 AlphaEngine 配额节省率时应阅读本文。

## 1. 存储位置与容量上限

缓存模块位于 `scripts/dalton/alphaengine_search_cache.py`，采用 SQLite WAL（Write-Ahead Logging）模式持久落盘：

- **默认文件路径**：`{cache_dir}/alphaengine_search_cache.sqlite3`，可通过环境变量 `DALTON_ALPHAENGINE_CACHE_DB` 覆盖；建库时文件权限锁定为 `0600`。
- **行上限与清理**：表上限 **`DEFAULT_MAX_ROWS = 10_000`** 行，每执行 **`CLEANUP_EVERY_WRITES = 50`** 次写入触发一次过期记录清理与容量修剪，并保留过期时间不超过 `STALE_RETENTION_S`（7 天）的历史行供配额耗尽兜底（`get_stale_sync`）。

## 2. 缓存键规则（v3）与条数切片复用

缓存键构建由 `scripts/dalton/alphaengine_search_cache.py` 负责，当前版本常量 **`CACHE_KEY_VERSION = 3`**（全系统共享，载荷内 `scope` 固定为 `"team"`）：

1. **规范化键构成**：对以下有序 JSON 对象计算 SHA-256：
   - `v`: `3`
   - `op`: 检索操作名
   - `scope`: `"team"`
   - `params`: `{mode, ticker, query, date_from, date_to, sort, filters}`（其中 `query` 先经 Unicode NFKC 标准化并折叠连续空白；`filters` 内部有序化并去重）
   - `limit`: 单独外置在完整 `cache_key` 之中。
2. **大条数向下切片复用（`base_key`）**：`base_key` 去掉 `limit` 字段；当请求需要较小 `limit` 而缓存中已有同 `base_key` 且包含足够全集结果的条目时，通过 `slice_cached_payload` 直接截取返回并附上 `SLICED_CACHE_NOTE` 提示，进一步减少上游消耗。

## 3. 四档动态有效期（TTL）

有效期计算函数 `compute_ttl_seconds` 基于**北京时间当日（`today_str`）** 与检索区间终点日期判断：

| 条件 | 常量名 | 有效期 | 说明 |
|---|---|---|---|
| 空结果 且 结束日 ≥ 今天 | `TTL_TODAY_EMPTY_S` | **1800 秒（30 分钟）** | 防止当天刚上线的研究/纪要长时间被空结果占位 |
| 非空结果 且 结束日 ≥ 今天 | `TTL_TODAY_NON_EMPTY_S` | **7200 秒（2 小时）** | 兼顾盘中/当日热点高频调用与时效更新 |
| 结束日在最近 7 天内 | `TTL_RECENT_7D_S` | **43200 秒（12 小时）** | 近一周历史资料短周期缓存 |
| 更早历史区间 | `TTL_OLDER_S` | **604800 秒（7 天）** | 历史固定档案长周期复用 |

## 4. 并发单飞（Singleflight）、强制刷新与个人内容豁免

### 4.1 并发单飞去重

在 `scripts/dalton/researchdata_bridge.py` 中，内存字典 `self._inflight_searches` 对相同查询实施单飞合并：

- 在检查本地 SQLite 缓存**之前**先行登记 `_inflight_searches`（将 `读缓存 -> 调上游 -> 写缓存` 合并为单次领跑任务），键为 `cache_key`（若请求强制刷新则后缀 `#fresh`，即 `f"{plan.cache_key}#fresh"`）。
- 并发到达的完全相同检索请求直接挂起等待 `asyncio.shield(result_future)`，领跑协程拿到结果并写盘后统一返回，被合并请求记为 `status="coalesced"`，不重复打上游、也不重复计费。

### 4.2 `params.fresh` 与跳过缓存条件

以下请求在缓存层显式绕过或不做跨用户复用：

1. **强制刷新（`params.fresh`）**：只要调用参数 `params.fresh` 为真值，跳过本地缓存读取，直接向 AlphaEngine 发起实时检索，并将新结果写回刷新缓存。
2. **个人会议不缓存（`_is_personal_scope_request`）**：为避免个人私密纪要与预约信息通过团队缓存泄漏给他人，凡请求命中以下任一条件均**完全跳过团队缓存读写**：
   - `meeting_scope == "personal"`
   - `own_reservations_only` 为真
   - `document_categories`、`document_codes` 或 `type_ids` 包含 `personal_meeting` 或 `personal_meetings`
3. **只规划与翻页续读**：`mode == "plan"` 以及带翻页游标 `cursor` 的续读请求同样绕过缓存直通上游。

## 5. 用量日志与命中率查看方法

每次 AlphaEngine 检索请求均向宿主机本地 JSONL 台账追加一条结构化审计日志：

- **日志路径**：`~/Library/Logs/alphaengine_usage.jsonl`（单文件达 32 MiB 自动轮转 `.1`，可通过环境变量 `DALTON_ALPHAENGINE_USAGE_FILE` 覆盖）。
- **查看命中率与调用统计**：

```bash
# 查看文本汇总报告（总调用、缓存命中、单飞合并、上游实际消耗及估算节省率）
python3 scripts/dalton/alphaengine_usage_report.py

# 指定特定日志文件或输出机器可读 JSON
python3 scripts/dalton/alphaengine_usage_report.py ~/Library/Logs/alphaengine_usage.jsonl --json
```
