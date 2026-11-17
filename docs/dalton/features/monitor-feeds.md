# 本机看板采集与推送（Monitor Feeds）

本文说明由本机旧服务迁入 Dalton 监控面板平台的三个看板采集源、宿主机实时推送守护进程 `FeedDaemon`、交易所日历与交易时段控制、东方财富接口限流调整细节以及上线前校验流程；排查 A 股/港股实时面板数据不更新或调整采集节奏时应阅读本文。

## 1. 三个已迁入的看板与数据源

三个看板在 `src/dalton/data_jobs/manifest.yaml` 中分别对应任务 `pm-market-state`、`cn-intraday`、`hk-momentum`（合入时默认 **`enabled: false`**，由管理员按上线步骤验证后手动启用）：

| 看板 / 数据任务 ID | 目标面板 Slug | 数据源与采集实现 | 静态与实时产出 |
|---|---|---|---|
| **PM 驾驶舱 & 板块轮动** (`pm-market-state`) | `pm-market-state` (`us`)、`cn-sector-rotation` (`cn`) | `scripts/dalton/monitor_feeds/pm_cockpit/collect.py`：`fetch_us` 用 `yfinance`（`auto_adjust=False`）抓美股 ETF 与基准；`fetch_cn` 用 `akshare` 抓申万一/二级行业与沪深 300 基准（新浪主线 + 东财回退） | 每日构建静态面板（`us` 与 `cn` 两份） |
| **A 股盘中监测** (`cn-intraday`) | `cn-intraday` (`cn`) | `scripts/dalton/monitor_feeds/ashare_monitor/collect.py`：核心指数（`s_sh000001` 等）、两市成交（使用上证综指 + 深证综指 `s_sz399106` 对齐情绪看板口径）、涨跌家数广度、申万三级资金流与 ETF 雷达扫描（东方财富/腾讯） | 每日收盘由容器任务构建静态面板与 30 天历史；盘中由宿主机守护进程推 `snapshot` 键 |
| **港股动量指数** (`hk-momentum`) | `hk-momentum` (`hk`) | `scripts/dalton/monitor_feeds/hk_momentum/fetch_data.py`：`fetch_hkex_universe` 从港交所 `HKEX_XLSX_URL` 拉取主板股池；`fetch_ggt_list` 分页校验深交所港股通名单；`fetch_spot_batch` 批量拉取腾讯行情（`qt.gtimg.cn/q=r_hk*`） | 盘中推 `intraday` 键实时刷新动量排名 |

## 2. 宿主机守护进程与交易时段节奏

实时推送进程 `scripts/dalton/monitor_feeds/run_feeds.py` 中的 `FeedDaemon` 通过 macOS LaunchAgent（`scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist`）在宿主机运行：

1. **交易所日历与午休感知**：`scripts/dalton/monitor_feeds/common.py:market_session_state` 基于 `exchange_calendars`（`XSHG` / `XHKG`）判断交易日与开闭市窗口，精确识别节假日与 A 股/港股午间休市时段，不依赖简单的周末判断。
2. **轮询与收敛写盘节奏**：
   - 交易窗口内默认每 **`interval_seconds = 60.0`** 秒采集并计算一次，通过 `PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}` 携带 `X-Service-Token` 推送最新值。
   - 非交易窗口最多休眠 **`max_idle_sleep_seconds = 300.0`** 秒，并在每个交易日收盘后补写一次当日终值，确保收盘定格数据完整落库。
3. **显式过期状态留存**：任一子源临时报错时，保留上一份有效数据并显式打上 `stale=True`、`stale_reason` 与 `last_success_at`（`scripts/dalton/monitor_feeds/ashare_monitor/collect.py`），禁止拿空值或过时值冒充实时正常状态。

## 3. 东方财富限流原因与采集节流配置

早期采用 30s/120s 高频全市场扫描 + 6 并发（且与原 OpenClaw 本地端并发请求同一出口 IP）时，A 股全 ETF 与全股票列表扫描每次产生约 70 个东方财富分页请求，导致目标站对出口 IP 返回 502 限流。现已在 `scripts/dalton/monitor_feeds/ashare_monitor/collect.py` 收紧为以下节流参数：

- **分模块差异刷新缓存**：指数与涨跌广度保持 1 分钟粒度实时刷新；重扫描改用分档 TTL——ETF 扫描 `ETF_SCAN_TTL_TRADING = 120`（交易时段 120s）/ `ETF_SCAN_TTL_CLOSED = 300`，全市场股票扫描 `STOCK_SCAN_TTL_TRADING = 300` / `STOCK_SCAN_TTL_CLOSED = 600`，历史序列 `HISTORY_TTL = 900`（15 分钟）。
- **并发与域名降级**：分页面并发数从 6 降为 **`EASTMONEY_PAGE_WORKERS = 2`**；每页最多重试 **`EASTMONEY_PAGE_ATTEMPTS = 3`** 次，前两次请求 `push2delay.eastmoney.com`，仅末次重试切换主域 `push2.eastmoney.com`（定义在 `EASTMONEY_HOSTS`）。
- 启用 Dalton `com.dalton.monitor-feeds` 前，必须确认宿主机已停止旧的 OpenClaw / 本地同类抓包定时脚本，避免同一 IP 双重冲击触发封禁。

## 4. 上线切换步骤

1. 基于模板 `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` 替换所有占位符（`__PROJECT_DIR__`、`__HOME__`、`__LOG_DIR__` 与 `__INTERNAL_SERVICE_TOKEN__`）后写入 `~/Library/LaunchAgents/com.dalton.monitor-feeds.plist`，设置权限 `chmod 600 ~/Library/LaunchAgents/com.dalton.monitor-feeds.plist`，再通过 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.monitor-feeds.plist` 加载；同时确认状态目录 `var/dalton/monitor-feeds/` 可写且旧本机抓包服务已停机。注意该服务运行时不读取 `.env`，一旦在 `.env` 中轮换 `INTERNAL_SERVICE_TOKEN`，必须同步更新已安装的 plist 并重加载（`bootout` + `bootstrap`）。
2. 确认 `.env` 中 `DALTON_ARTIFACT_PUBLISHING_ENABLED=true` 且面板 `cn-intraday`、`hk-momentum` 已在团队环境通过静态构建注册。
3. 核对单次试跑无 `stale` 异常与 502 报错后，在运维/后台数据任务页将 `pm-market-state`、`cn-intraday`、`hk-momentum` 逐一由关闭切为启用。
