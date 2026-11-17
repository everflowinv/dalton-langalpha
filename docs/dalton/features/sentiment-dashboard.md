# 市场情绪看板（Sentiment Dashboard）拆分与覆盖层

这页讲清怎么把单页市场情绪看板拆成 6 个全公司可见面板、共享静态资源怎么托管，以及覆盖层脚本怎么同步到运行期数据目录。

## 1. 拆分为 6 个全公司可见面板

通过 `src/dalton/monitors/data_job_registry.py` 里的 `SENTIMENT_DASHBOARD_PANELS` 和生成器 `scripts/dalton/openclaw_overlays/skills/sentiment-dashboard/scripts/sentiment_dashboard_cli.py:write_split_panels`，数据任务 `sentiment-dashboard-refresh` 会把产物拆成 6 个独立面板发布，全部默认 `visibility="company"`（仅公司内部和管理员能看）：

| 面板 Slug | 分类（`category`） | 排序权重 | 定位与对照单页差异 |
|---|---|---|---|
| `market-overview` | `overview` | `10` | 全市场概览首页（情绪温度、核心宽基信号摘要） |
| `us-market` | `us` | `20` | 美股市场情绪与风格指标 |
| `cn-market` | `cn` | `30` | A 股拥挤度、两融与情绪拆分面板 |
| `hk-market` | `hk` | `40` | 港股市场情绪与南向指标 |
| `asia-leverage` | `asia` | `50` | 亚洲三地（日本 / 韩国 / 台湾）融资杠杆汇总，对应 `sentiment_dashboard_cli.py` 过滤 `{"日本", "韩国", "台湾"}`，不含中国 |
| `stocks` | `stocks` | `60` | 个股监控面板——把原单页 `single_tabs` 里的美股个股（`us_stocks`）和中概/A 股个股（`china_stocks`）合并成同一面板（原单页 7 个 tab，拆分面板共 6 个） |

## 2. 面板与共享静态目录布局

- 跑完 `write_split_panels` 后，所有拆分 HTML 写进 `research-outputs/sentiment-dashboard/panels/<slug>/index.html`。
- 六个面板共用的样式和前端交互脚本抽到了 `research-outputs/sentiment-dashboard/panels/_shared/`（包括 `panel.css` 和 `panel.js`）。
- 在 `MONITOR_DATA_JOB_REGISTRY` 里，六个绑定条目的 `shared_rel_dir` 都指向这个 `_shared` 目录。前端通过签名 `grant` 路由加载各面板时，`monitors` 服务会一并安全托管同一个快照版本下的 `_shared` 资产，拦住越界和符号链接读取。

## 3. 覆盖层脚本如何装入运行期数据副本

市场情绪面板脚本还要沿用 `var/dalton/openclaw-data/` 下的既有目录树，所以 Dalton 在代码仓里用**覆盖层目录 `scripts/dalton/openclaw_overlays/`** 维护所有改造（比如拆分面板生成逻辑、拥挤度口径修复补丁），再通过 `scripts/dalton/seed_openclaw_data.sh` 同步进数据副本：

1. **干跑预览差异**（默认不改写磁盘）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh
   ```
2. **正式应用覆盖层**（传 `--apply`）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh --apply
   ```
- **前置依赖**：脚本默认从 `SOURCE_DIR = ~/.openclaw/workspace` 读源文件（可用 `--source <dir>` 覆盖）。如果源目录不存在，脚本会报 `ERROR: Source directory does not exist` 并以退出码 `1` 退出。
- **每次 `--apply` 的完整写入行为**：除了把仓库里的 `scripts/dalton/openclaw_overlays/` 覆盖拷贝进 `var/dalton/openclaw-data/`，脚本还会从 `SOURCE_DIR` 同步白名单里的 25 个脚本（`SEED_SCRIPTS`）、4 个配置（`SEED_CONFIGS`）和 13 个技能代码（`SEED_SKILLS`，不含内部 `state/`、`data/`、`temp/`、`events/`、`model_cards/`），并用 `rsync --ignore-existing` 补齐缺失的 `SEED_STATE_PATHS` 和技能状态文件，最后确保 `state/a-share-volume-watch` 目录存在并建好 `state/sentiment-dashboard/cos-venv/bin/python` 启动器。
- **保留既有数据与排除凭据**：已有的业务状态和数据文件不会被 `--ignore-existing` 覆盖，凭据文件名模式（`.env`、`*secret*`、`*token*.json` 等）以及 `venv`、`.venv`、`cos-venv/`、`.dalton-freeze/` 都会被排除。
- 改过 `scripts/dalton/openclaw_overlays/` 下的脚本并在生产重建或发布后，要记得在宿主机或数据卷上再跑一次 `--apply`，免得容器数据任务继续执行旧副本。
