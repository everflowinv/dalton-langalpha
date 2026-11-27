# Everflow 生产数据任务配置说明

本目录包含 Everflow 生产运维脚本（不随 Standalone Core 发行包分发）。

仓库的通用脚本与核心发布产物中不包含任何 Everflow 专属真实值（飞书群/用户 ID、Discord 用户/频道、宿主机绝对路径、Windows 远端路径、公司邮箱等）。对于原来直接写在 `src/dalton/data_jobs/`（`batch3_messages.py` 与 `manifest.yaml`）里的 5 项默认值（T2/T38/T73/T83 通知目标与 T58 宿主机解释器路径），仓库仅存放在 Everflow 专属配置文件 `src/dalton/data_jobs/everflow_defaults.yaml` 中：

- 该文件**仅在 `src.dalton.profile` 为 Everflow 时加载**，环境变量或 `config/data_tasks.env` 若设置则优先覆盖；Core 形态永不加载，且 `scripts/standalone/package_release.sh` 会剥离该文件（`verify_release_tarball.py` 同步拦截），确保常规部署无需改动生产 `.env` 即可保持 T2/T38/T58/T73/T83 行为不变。
- 当需要执行 `seed_openclaw_data.sh --apply` 同步版本化脚本到生产 `var/dalton/openclaw-data/scripts/` 时，请将下表全部必填键写入：

- **`<var_data_root>/config/data_tasks.env`**（默认 `var/dalton/openclaw-data/config/data_tasks.env`，也可在 `<var_data_root>` 挂载目录内通过 `DALTON_DATA_TASKS_CONFIG` 指定自定义文件）——任务容器（通过数据卷挂载）、宿主机桥接（`researchdata_bridge.py`）与 `seed_openclaw_data.sh --apply` 预检统一以此为持久化配置源，重复键统一取最后一条；
- （可选）后端进程环境 / `.env` 中若同时设置下表键（含 `SEC_USER_AGENT` 别名与 `DALTON_DATA_TASKS_CONFIG`），`container_runner.py` 也会在创建临时任务容器时显式转发给容器内脚本（与 `seed_openclaw_data.sh` 共用 `src/dalton/data_jobs/task_config.py` 中的 `/workspace/` 路径映射函数），但**仅写在 `.env` 或当前 shell 环境、未写入 `<var_data_root>/config/data_tasks.env` 的键会被预检判为缺失**。

> **运行时策略**：容器任务与宿主机任务在运行时继续执行工作区 `var/dalton/openclaw-data/scripts/`（及工作区 `skills/.../run.sh`）下的副本。仓库中的 `scripts/dalton/data_assets/scripts/` 作为版本化来源与审查对象，常规 `deploy_main.sh` 不会改写工作区脚本或切换运行路径；需要同步到生产工作区时，由运维显式执行 `bash scripts/dalton/seed_openclaw_data.sh --apply`（内置预检）。
>
> **预检保护**：在 Everflow 形态下执行 `bash scripts/dalton/seed_openclaw_data.sh --apply` 前，脚本以**运行中后端容器实际持有的环境**（`docker inspect` 读取，与任务容器继承同源）及单个配置文件为准进行校验：未设置 `DALTON_DATA_TASKS_CONFIG` 时只检查 `<TARGET_DIR>/config/data_tasks.env`；若设置了自定义 `DALTON_DATA_TASKS_CONFIG`，若仅写入 `.env` 而运行中后端容器尚未更新（`docker restart` 不会重载 `.env`），预检会拦截并提示「需要重建后端」（`docker compose up -d --no-deps --force-recreate backend`）；路径映射与容器运行器共用 `task_config.resolve_data_tasks_config_relative_path`，并且仅检查该单一文件是否齐全（不与默认 `config/data_tasks.env` 合并，重复键取最后一条）。`seed_openclaw_data.sh` 通过统一形态解析器读取 `<repo>/.env` 中的 `APP_ENV` / `DALTON_PROFILE`，不会误把已初始化的 Core 安装当成 Everflow 拦截。

## 必填配置键（Everflow `seed --apply` 预检项）

| 键名 | 兼容旧键 | 用途 |
|---|---|---|
| `DALTON_DATA_TASK_BBG_HOST` | `DALTON_BBG_HOST` | Windows BBG 机器 SSH 目标（格式 `<user>@<host>`），供 `bbg_remote_refresh.sh` 连接刷新与拉取导出文件 |
| `DALTON_DATA_TASK_BBG_CSV_REMOTE` | `DALTON_BBG_CSV_REMOTE` | Windows 端 BBG 导出 CSV 的远端路径，供 `bbg_remote_refresh.sh` 拉取 |
| `DALTON_DATA_TASK_BBG_XLSX_REMOTE` | `DALTON_BBG_XLSX_REMOTE` | Windows 端 BBG 工作簿的远端路径，供 `bbg_remote_refresh.sh` 拉取 |
| `DALTON_DATA_TASK_BBG_REFRESH_CMD` | `DALTON_BBG_REFRESH_CMD` | Windows 端触发 BBG 刷新的命令（非 `--pull-only` 必填），供 `bbg_remote_refresh.sh` 远端执行 |
| `DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID` | `DALTON_FEISHU_TRACKING_CHAT_ID` | 飞书 Tracking 群 `chat_id`（`us-etf-monthly-flows`、`weekly-token-usage`、`hk-momentum-weekly` 统一读取） |
| `DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID` | `DALTON_FEISHU_SEMI_CHAT_ID` | 飞书 Semi 群 `chat_id`（`softbank-nav-monitor`、`tsmc-adr-premium` 使用） |
| `DALTON_DATA_TASK_FEISHU_ASIA_TARGET` | `DALTON_FEISHU_ASIA_TARGET` | 飞书 Market Alert 投递目标（`chat:<chat_id>`，`pt-watchlist-asia/us`、`china-2c-hk/us` 统一读取） |
| `DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET` | `DALTON_MEGACAP_FEISHU_TARGET` | 六大科技股相对信号（`megacap_relative_signal_monitor.py`）飞书接收群 `chat_id` |
| `DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID` | `DALTON_SENTIMENT_DIGEST_CHAT_ID`, `SENTIMENT_DIGEST_CHAT_ID` | 情绪看板每日速览（`sentiment_dashboard_digest.sh`）飞书接收群 `chat_id` |
| `DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET` | — | A 股底部放量监控（T38，`batch3_messages.py`）飞书私聊目标（`user:<open_id>`） |
| `DALTON_DATA_TASK_DISCORD_DM_TARGET` | — | Discord 个人私聊目标（`user:<id>`，`e_watch_monitor.py`、`cron_command_tasks.py`、`batch3_messages.py` 统一读取） |
| `DALTON_DATA_TASK_DISCORD_DEV_CHANNEL` | — | Discord Dev 频道目标（`channel:<id>`，`ai-chain-daily`、`fde-hiring-weekly-report` 统一读取） |
| `DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL` | — | Discord 半导体底部清单频道目标（`channel:<id>`，`semi-bottom-checklist-1030et` 使用） |
| `DALTON_SEC_USER_AGENT` | `DALTON_DATA_TASK_SEC_USER_AGENT`, `SEC_USER_AGENT` | SEC EDGAR API 请求头 `User-Agent`（供 T16/T36 半导体早晚报查询；支持通过容器环境或 `config/data_tasks.env` 注入） |
| `DALTON_DATA_TASK_GS_PB_PYTHON` | — | 宿主机高盛 PB 周报（T58 `gs-pb-weekly-rundown`）专用 venv Python 绝对路径，由 `researchdata_bridge.py` 展开 |

## 可选配置键

| 键名 | 兼容旧键 | 用途 |
|---|---|---|
| `DALTON_DATA_TASK_WORKSPACE` | `DALTON_WORKSPACE`, `OPENCLAW_WORKSPACE`, `WORKDIR` | 运行时数据任务工作区路径（未设置时以当前运行目录 `$PWD` 为准） |
| `DALTON_DATA_TASKS_CONFIG` | — | 显式覆盖配置文件路径（默认 `<runtime_workspace>/config/data_tasks.env`） |
| `DALTON_DATA_TASK_LEGACY_WORKSPACE_PREFIX` | — | 飞书卡片旧绝对图片路径前缀改写（`feishu_send_card.py` 使用，未设置时不改写） |
| `DALTON_DATA_TASK_OPENCLAW_BACKUP_DIR` | `DALTON_OPENCLAW_BACKUP_DIR` | `openclaw-config-backup` 失败诊断时检查的备份目录 |
| `DALTON_DATA_TASK_OPENCLAW_CONFIG` | `DALTON_OPENCLAW_CONFIG` | OpenClaw 配置文件路径（`etf_monthly_flows_tracker.py` 默认 `~/.openclaw/openclaw.json`） |
| `DALTON_DATA_TASK_CHINA_2C_UNIVERSE_DIR` | — | China 2C 股票池 JSON 目录（`china_2c_monitor.py` 默认 `<runtime_workspace>/state`） |

## 漂移检查（只读）

比较仓库基线（含 `seed_openclaw_data.sh` 正常生成的 Antigravity 序列模块与 JSON）与生产 `var/dalton/openclaw-data/`：

```bash
# 默认比较 scripts/dalton/data_assets/{scripts,skills} 与 var/dalton/openclaw-data/{scripts,skills}
.venv/bin/python scripts/dalton/ops/prod/check_data_assets_drift.py

# JSON 输出 / 自定义目录
.venv/bin/python scripts/dalton/ops/prod/check_data_assets_drift.py --json
.venv/bin/python scripts/dalton/ops/prod/check_data_assets_drift.py --repo-dir <repo_assets> --var-dir <prod_var>
```

退出码：`0` 全部一致；`1` 存在漂移或单侧文件（含生产 `var/` 下有而仓库没有的脚本）；`2` 检查根目录缺失或不可读。
