# 同组（Peer Group）与板块 ETF 解析规则

在运行 `move_quant.py` 进行股价异动量化分解时，同组公司（Peers）与板块基准 ETF 按以下优先级依次确定：

## 1. 显式参数覆盖（最高优先级）

当用户在提问中明确指定对比标的（例如“对比 META 和 GOOGL、SNAP”），或调用方在命令行显式传入 `--peers A,B,C`、`--etf XLK` 时，直接使用显式传入的同组列表和板块 ETF。

## 2. 覆盖名单 `coverage.yaml`（默认权威来源）

默认读取当前用户的覆盖名单（覆盖名单由系统按用户维护，通过 Chief of Staff 修改；公司工作区内相对路径为 `../Home/dalton/coverage.yaml`，Home 工作区内为 `dalton/coverage.yaml`）：

1. 在 `companies` 列表中找到目标 `ticker` 的条目。
2. **同组列表（Peers）**：
   - 优先取该条目的 `peers` 字段（即同一 `peer_group` 下的其他公司代码，不含目标公司自身）；
   - 若条目只有 `peer_group` 名称而未展开 `peers`，收集 `coverage.yaml` 中所有 `peer_group` 相同、且代码不等于目标公司的上市代码（跳过 `SPCX` 等无公开行情的私有公司）。
3. **板块 ETF**：
   - 取该条目的 `etfs[0]`（若 `etfs` 非空，例如 `QQQ`、`IGV`、`KWEB`、`XHB`）；
   - 若该条目的 `etfs` 为空，按其 `sheet_section` 或 `peer_group` 映射到对应的板块 ETF（见下表）。

| `peer_group` / `sheet_section` | 默认板块 ETF |
|---|---|
| `megacap` / `US Mega Tech` | `QQQ` |
| `app_software` / `infra_software` / `US Software` | `IGV` |
| `it_services` | `XLK` |
| `streaming_media` / `social_apps` / `games_adtech` | `XLC` |
| `ecom` / `delivery` / `online_travel` | `XLY` |
| `banks` / `networks` / `payments` / `fintech_lenders` / `card_lenders` / `bnpl` / `brokers` / `alts` / `bureaus` / `ratings_info` / `insurers` / `ins_brokers` / `exchanges` / `US Financial` | `XLF` |
| `consumer_brands` / `specialty_retail` / `restaurants` / `lodging_cruise` / `leisure` | `XLY` |
| `staples` / `mass_retail` / `food_ag` | `XLP` |
| `homebuilder` | `XHB` |
| `ai_construction` | `PAVE` |
| `space` | `UFO` |
| `cn_mega` / `cn_ecom_vertical` / `cn_travel_local` / `cn_internet_small` / `cn_content` / `cn_games` / `cn_brokers_exch` / `cn_logistics` / `cn_health` / `cn_ai` / `cn_cloud_idc` / `cn_consumer_finance` | `KWEB` |

## 3. 基金 Wiki 行业页（名单外公司第一回退）

若目标公司不在 `coverage.yaml` 中：
- 通过 `researchdata.research(route="company-wiki", operation="search", query=f"{ticker} peers 行业 可比公司")` 查询基金 wiki 是否已有该公司或所在细分行业的同组定义；
- 若找到可比公司，通过 `--peers` 传给 `move_quant.py`。

## 4. 脚本内置同组表与 yfinance 行业回退（兜底）

若以上均未命中，`move_quant.py` 自动执行两层兜底：
1. 查找脚本内置的 `DEFAULT_PEER_MAP`（涵盖 AI 算力/芯片、光通信/网络、半导体设备、大型科技、中概电商/科技、电动车等常见同组）；
2. 调用 `yfinance.Ticker(ticker).info` 读取 `sector` / `industry`，按 GICS/Yahoo 行业映射板块 ETF：
   - `Technology` → `XLK`（若 `industry` 含 `Semiconductor` → `SMH`）
   - `Communication Services` → `XLC`
   - `Consumer Cyclical` → `XLY`
   - `Consumer Defensive` → `XLP`
   - `Healthcare` → `XLV`
   - `Financial Services` → `XLF`
   - `Industrials` → `XLI`
   - `Energy` → `XLE`
   - `Real Estate` → `XLRE`
   - `Utilities` → `XLU`
   - `Basic Materials` → `XLB`
   - 其他或获取失败 → `SPY`
