# 市场情绪看板（Sentiment Dashboard）拆分与覆盖层

本文说明如何把原有的单页市场情绪看板拆分为 6 个公司全员可见监控面板，共享静态资源的生成与托管结构，以及部署脚本如何把仓库里的覆盖层脚本同步到运行期数据目录；修改情绪面板脚本或排查面板展示异常时应阅读本文。

## 1. 拆分为 6 个全公司可见面板

通过 `src/dalton/monitors/data_job_registry.py` 中的 `SENTIMENT_DASHBOARD_PANELS` 与输出生成器 `scripts/dalton/openclaw_overlays/skills/sentiment-dashboard/scripts/sentiment_dashboard_cli.py:write_split_panels`，数据任务 `sentiment-dashboard-refresh` 的产物不再只作为单一长页发布，而是拆成 6 个独立面板，全部默认 `visibility="company"`（仅公司内部及管理员可见）：

| 面板 Slug | 分类（`category`） | 排序权重 | 说明与对照单页差异 |
|---|---|---|---|
| `market-overview` | `overview` | `10` | 全市场概览首页（情绪温度、核心宽基信号摘要） |
| `us-market` | `us` | `20` | 美股市场情绪与风格指标 |
| `cn-market` | `cn` | `30` | A 股拥挤度、两融与情绪拆分面板 |
| `hk-market` | `hk` | `40` | 港股市场情绪与南向指标 |
| `asia-leverage` | `asia` | `50` | 亚洲三地（日本 / 韩国 / 台湾）融资杠杆汇总（对应 `sentiment_dashboard_cli.py` 过滤 `{"日本", "韩国", "台湾"}`，不含中国） |
| `stocks` | `stocks` | `60` | 个股监控面板——将原单页 `single_tabs` 中的美股个股（`us_stocks`）与中概/A 股个股（`china_stocks`）合并为同一面板展示（原单页为 7 个 tab，拆分面板共 6 个） |

## 2. 面板与共享静态目录布局

- 执行 `write_split_panels` 后，所有拆分 HTML 写入 `research-outputs/sentiment-dashboard/panels/<slug>/index.html`。
- 六个面板共用的样式与前端交互脚本统一抽离至 `research-outputs/sentiment-dashboard/panels/_shared/`（包含 `panel.css` 与 `panel.js`）。
- 在 `MONITOR_DATA_JOB_REGISTRY` 中，六个绑定条目的 `shared_rel_dir` 均指向该 `_shared` 目录；前端通过签名 `grant` 路由加载各面板时，`monitors` 服务会一并安全托管同一快照版本下的 `_shared` 资产，禁止任何越界或符号链接读取。

## 3. 覆盖层脚本如何装入运行期数据副本

由于市场情绪面板脚本仍复用了 `var/dalton/openclaw-data/` 下的既有文件树结构，Dalton 在代码仓内通过**覆盖层目录 `scripts/dalton/openclaw_overlays/`** 维护全部改造（例如拆分面板生成逻辑、以及拥挤度口径修复补丁），并通过 `scripts/dalton/seed_openclaw_data.sh` 同步进入数据副本：

1. **干跑预览差异**（默认不改写磁盘）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh
   ```
2. **正式应用覆盖层**（传 `--apply`）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh --apply
   ```
- **前置依赖**：脚本默认从 `SOURCE_DIR = ~/.openclaw/workspace` 读取源文件（可通过 `--source <dir>` 覆盖）；若源目录不存在，脚本会直接报 `ERROR: Source directory does not exist` 并以退出码 `1` 退出。
- **每次 `--apply` 的完整写入行为**：除将仓库内 `scripts/dalton/openclaw_overlays/` 拷贝覆盖进 `var/dalton/openclaw-data/` 外，脚本还会从 `SOURCE_DIR` 同步白名单中的 25 个脚本（`SEED_SCRIPTS`）、4 个配置（`SEED_CONFIGS`）和 13 个技能代码（`SEED_SKILLS`，不含内部 `state/`、`data/`、`temp/`、`events/`、`model_cards/`），并以 `rsync --ignore-existing` 补充缺失的 `SEED_STATE_PATHS` 与技能状态文件，最后确保 `state/a-share-volume-watch` 目录存在及创建 `state/sentiment-dashboard/cos-venv/bin/python` 启动器。
- **保留既有数据与排除凭据**：已存在的业务状态与数据文件不会被 `--ignore-existing` 覆盖，且凭据文件名模式（`.env`、`*secret*`、`*token*.json` 等）及 `venv`/`.venv`、`cos-venv/`、`.dalton-freeze/` 等均被排除。
- 修改 `scripts/dalton/openclaw_overlays/` 下任何脚本并在生产重建或发布后，应同步在宿主机或数据卷上执行一次 `--apply`，否则容器数据任务仍可能执行旧副本。
