# AlphaEngine 检索全系统缓存

这页讲清宿主机 `researchdata_bridge.py` 里 AlphaEngine 团队级检索缓存的设计，包括 SQLite 存储格式、v3 缓存键、四档有效期、并发单飞去重、`params.fresh` 强制刷新、个人会议跳过规则以及命中率统计命令。

## 1. 存储位置与容量上限

缓存模块在 `scripts/dalton/alphaengine_search_cache.py`，用 SQLite WAL（Write-Ahead Logging）模式落盘：

- **默认文件路径**：`{cache_dir}/alphaengine_search_cache.sqlite3`，可以通过环境变量 `DALTON_ALPHAENGINE_CACHE_DB` 覆盖；建库时文件权限锁成 `0600`。
- **行上限与清理**：表上限是 **`DEFAULT_MAX_ROWS = 10_000`** 行，每执行 **`CLEANUP_EVERY_WRITES = 50`** 次写入触发一次过期清理和容量修剪。过期不超过 `STALE_RETENTION_S`（7 天）的历史行会留下来，供配额耗尽时兜底读取（`get_stale_sync`）。

## 2. 缓存键规则（v3）与条数切片复用

缓存键由 `scripts/dalton/alphaengine_search_cache.py` 生成，当前版本常量为 **`CACHE_KEY_VERSION = 3`**，全系统共享，载荷里的 `scope` 固定是 `"team"`：

1. **规范化键结构**：对下面有序 JSON 对象算 SHA-256：
   - `v`: `3`
   - `op`: 检索操作名
   - `scope`: `"team"`
   - `params`: `{mode, ticker, query, date_from, date_to, sort, filters}`（`query` 先做 Unicode NFKC 标准化并折叠连续空白；`filters` 内部排序并去重）
   - `limit`: 单独放在完整 `cache_key` 里面。
2. **大条数向下切片复用（`base_key`）**：`base_key` 去掉了 `limit` 字段。如果这次要的 `limit` 较小，而缓存里已经存过同一个 `base_key` 且条数够用，系统就用 `slice_cached_payload` 直接截取返回，并附上 `SLICED_CACHE_NOTE` 提示，省下一次上游调用。

## 3. 四档动态有效期（TTL）

有效期计算函数 `compute_ttl_seconds` 按**北京时间当天（`today_str`）**对照检索区间终点日期判断：

| 条件 | 常量名 | 有效期 | 用意 |
|---|---|---|---|
| 空结果 且 结束日 ≥ 今天 | `TTL_TODAY_EMPTY_S` | **1800 秒（30 分钟）** | 防止当天刚上线的研究或纪要长时间被空结果占坑 |
| 非空结果 且 结束日 ≥ 今天 | `TTL_TODAY_NON_EMPTY_S` | **7200 秒（2 小时）** | 兼顾盘中高频调用和当日内容及时更新 |
| 结束日在最近 7 天内 | `TTL_RECENT_7D_S` | **43200 秒（12 小时）** | 近一周历史资料做短周期缓存 |
| 更早历史区间 | `TTL_OLDER_S` | **604800 秒（7 天）** | 历史固定档案做长周期复用 |

## 4. 并发单飞（Singleflight）、强制刷新与个人内容豁免

### 4.1 并发单飞去重

在 `scripts/dalton/researchdata_bridge.py` 里，内存字典 `self._inflight_searches` 对相同查询做单飞合并：

- 查本地 SQLite 缓存**之前**先登记 `_inflight_searches`，把「读缓存 → 调上游 → 写缓存」合并成一次领跑任务。键名用 `cache_key`，强制刷新时加上 `#fresh` 后缀（`f"{plan.cache_key}#fresh"`）。
- 同时到达的完全相同检索请求直接挂起等 `asyncio.shield(result_future)`。领跑协程拿到结果写盘后一起返回，被合并的请求记成 `status="coalesced"`，只打一次上游、只扣一次费。

### 4.2 `params.fresh` 与跳过缓存条件

以下三类请求在缓存层显式绕开或不做跨人复用：

1. **强制刷新（`params.fresh`）**：只要参数 `params.fresh` 为真值，就跳过本地缓存读取，直接向 AlphaEngine 发起实时检索，并把新结果写回刷新缓存。
2. **个人会议不缓存（`_is_personal_scope_request`）**：为了防止个人私密纪要和预约信息通过团队缓存泄给他人，命中下面任一条件时**完全跳过团队缓存读写**：
   - `meeting_scope == "personal"`
   - `own_reservations_only` 为真
   - `document_categories`、`document_codes` 或 `type_ids` 包含 `personal_meeting` 或 `personal_meetings`
3. **只规划与翻页续读**：`mode == "plan"` 以及带翻页游标 `cursor` 的续读请求也绕过缓存直通上游。

## 5. 用量日志与命中率查看方法

每次 AlphaEngine 检索请求都会往宿主机本地 JSONL 台账追加一条结构化审计日志：

- **日志路径**：`~/Library/Logs/alphaengine_usage.jsonl`（单文件满 32 MiB 自动轮转为 `.1`，可用环境变量 `DALTON_ALPHAENGINE_USAGE_FILE` 覆盖）。
- **查看命中率与调用统计**：

```bash
# 查看文本汇总报告（总调用、缓存命中、单飞合并、上游实际消耗及估算节省率）
python3 scripts/dalton/alphaengine_usage_report.py

# 指定特定日志文件或输出机器可读 JSON
python3 scripts/dalton/alphaengine_usage_report.py ~/Library/Logs/alphaengine_usage.jsonl --json
```
