# 本机看板采集与推送（Monitor Feeds）

这页讲清已经迁入 Dalton 监控面板的三个本机看板采集源、宿主机实时推送守护进程 `FeedDaemon`、交易所日历与交易时段控制、东方财富限流策略，以及上线前怎么校验。

## 1. 三个已迁入的看板与数据源

三个看板在 `src/dalton/data_jobs/manifest.yaml` 里分别对应任务 `pm-market-state`、`cn-intraday`、`hk-momentum`（合入时默认 **`enabled: false`**，由管理员按上线步骤验完再手动打开）：

| 看板 / 数据任务 ID | 目标面板 Slug | 数据源与采集实现 | 静态与实时产出 |
|---|---|---|---|
| **PM 驾驶舱 & 板块轮动** (`pm-market-state`) | `pm-market-state` (`us`)、`cn-sector-rotation` (`cn`) | `scripts/dalton/monitor_feeds/pm_cockpit/collect.py`：`fetch_us` 用 `yfinance`（`auto_adjust=False`）抓美股 ETF 与基准；`fetch_cn` 用 `akshare` 抓申万一/二级行业与沪深 300 基准（新浪主线 + 东财回退） | 每天构建静态面板（`us` 和 `cn` 两份） |
| **A 股盘中监测** (`cn-intraday`) | `cn-intraday` (`cn`) | `scripts/dalton/monitor_feeds/ashare_monitor/collect.py`：核心指数（`s_sh000001` 等）、两市成交（用上证综指 + 深证综指 `s_sz399106` 对齐情绪看板口径）、涨跌家数广度、申万三级资金流与 ETF 雷达扫描（东方财富/腾讯） | 每天收盘由容器任务构建静态面板和 30 天历史；盘中由宿主机守护进程推 `snapshot` 键 |
| **港股动量指数** (`hk-momentum`) | `hk-momentum` (`hk`) | `scripts/dalton/monitor_feeds/hk_momentum/fetch_data.py`：`fetch_hkex_universe` 从港交所 `HKEX_XLSX_URL` 拉主板股池；`fetch_ggt_list` 分页核对深交所港股通名单；`fetch_spot_batch` 批量拉腾讯行情（`qt.gtimg.cn/q=r_hk*`） | 盘中推 `intraday` 键实时刷新动量排名 |

## 2. 宿主机守护进程与交易时段节奏

实时推送进程 `scripts/dalton/monitor_feeds/run_feeds.py` 里的 `FeedDaemon` 靠 macOS LaunchAgent（`scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist`）在宿主机上常驻运行：

1. **交易所日历与午休感知**：`scripts/dalton/monitor_feeds/common.py:market_session_state` 用 `exchange_calendars`（`XSHG` / `XHKG`）判断交易日和开闭市窗口，能准确认出节假日和 A 股、港股午休时段，不会只靠周末来粗判。
2. **轮询与收敛写盘节奏**：
   - 交易窗口内默认每 **`interval_seconds = 60.0`** 秒抓一次数据并算快照，带着 `X-Service-Token` 调 `PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}` 推最新值。
   - 非交易窗口最多休眠 **`max_idle_sleep_seconds = 300.0`** 秒，并在每个交易日收盘后补写一次当日终值，保证收盘定格数据落库。
3. **显式过期状态保留**：任何一路子源临时报错时，保留上一份有效数据并明确标上 `stale=True`、`stale_reason` 和 `last_success_at`（`scripts/dalton/monitor_feeds/ashare_monitor/collect.py`），让面板准确提示数据为什么停滞。

## 3. 东方财富限流原因与采集节流配置

早期用 30s/120s 高频全市场扫描加 6 并发（还和原 OpenClaw 本地端共用一个出口 IP），每次 A 股全 ETF 和全股票列表扫描都要打大约 70 个东方财富分页请求，结果把出口 IP 打进了 502 限流。现在 `scripts/dalton/monitor_feeds/ashare_monitor/collect.py` 已经收紧成下面这套节流参数：

- **分模块差异刷新缓存**：指数和涨跌广度保持 1 分钟粒度实时刷新；重扫描改成按档 TTL 缓存——ETF 扫描 `ETF_SCAN_TTL_TRADING = 120`（交易时段 120s）/ `ETF_SCAN_TTL_CLOSED = 300`，全市场股票扫描 `STOCK_SCAN_TTL_TRADING = 300` / `STOCK_SCAN_TTL_CLOSED = 600`，历史序列 `HISTORY_TTL = 900`（15 分钟）。
- **并发与域名降级**：分页并发从 6 降到 **`EASTMONEY_PAGE_WORKERS = 2`**；每页最多试 **`EASTMONEY_PAGE_ATTEMPTS = 3`** 次，前两次打 `push2delay.eastmoney.com`，最后一次才切去主域 `push2.eastmoney.com`（定义在 `EASTMONEY_HOSTS`）。
- 启动 Dalton 的 `com.dalton.monitor-feeds` 前，一定要先确认宿主机已经停掉旧的 OpenClaw 和本地同类抓包定时脚本，免得同一出口 IP 被双份流量打封。

## 4. 上线切换步骤

1. 从模板 `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` 替换四个占位符（`__PROJECT_DIR__`、`__HOME__`、`__LOG_DIR__` 和 `__INTERNAL_SERVICE_TOKEN__`），写入 `~/Library/LaunchAgents/com.dalton.monitor-feeds.plist`。设好权限 `chmod 600 ~/Library/LaunchAgents/com.dalton.monitor-feeds.plist`，再运行 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.monitor-feeds.plist` 加载。同时确认状态目录 `var/dalton/monitor-feeds/` 可写，旧本机抓包服务已经停掉。注意这个守护进程启动时不读 `.env`，只要 `.env` 里轮换了 `INTERNAL_SERVICE_TOKEN`，就必须同步更新已安装的 plist 并重新加载（`bootout` + `bootstrap`）。
2. 确认 `.env` 里的 `DALTON_ARTIFACT_PUBLISHING_ENABLED=true`，而且 `cn-intraday`、`hk-momentum` 已经在团队环境通过静态构建注册。
3. 跑一次试跑确认没有 `stale` 异常和 502 报错，再去运维或后台数据任务页把 `pm-market-state`、`cn-intraday`、`hk-momentum` 逐一开启。
