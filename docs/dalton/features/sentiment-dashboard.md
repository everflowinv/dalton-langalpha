# 市场情绪看板（Sentiment Dashboard）拆分与数据资产

市场情绪看板通过 `sentiment-dashboard-refresh` 拆分为 6 个全公司可见的独立面板，共享样式与交互脚本由 `_shared/` 目录托管，仓库内的数据任务与看板脚本通过 `seed_openclaw_data.sh` 安装到运行期数据目录 `var/dalton/openclaw-data/`。

## 1. 拆分为 6 个全公司可见面板

通过 `src/dalton/monitors/data_job_registry.py` 里的 `SENTIMENT_DASHBOARD_PANELS` 和生成器 `scripts/dalton/data_assets/skills/sentiment-dashboard/scripts/sentiment_dashboard_cli.py:write_split_panels`，数据任务 `sentiment-dashboard-refresh` 会把产物拆成 6 个独立面板发布，全部默认 `visibility="company"`（仅公司内部和管理员能看）：

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

## 3. 数据资产脚本如何装入运行期数据卷

市场情绪面板脚本沿用 `var/dalton/openclaw-data/` 下的目录树，全部托管脚本与技能定义已收拢在仓库 **`scripts/dalton/data_assets/`** 中，通过 `scripts/dalton/seed_openclaw_data.sh` 直接拷贝到目标数据目录：

1. **干跑预览差异**（默认，不改写磁盘）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh
   ```
2. **正式应用安装**（传 `--apply`）：
   ```bash
   bash scripts/dalton/seed_openclaw_data.sh --apply
   ```
- **唯一来源**：脚本直接从仓库 `scripts/dalton/data_assets/`（含 `scripts/` 与 6 个技能目录）读取源文件并同步到 `--target`（默认 `var/dalton/openclaw-data/`），不再读取宿主机外部目录。
- **每次 `--apply` 的写入行为**：将 `scripts/dalton/data_assets/` 中的脚本与技能同步进 `var/dalton/openclaw-data/`，同步 `src/dalton/llm/antigravity_series.py` 并导出 `skills/model-usage-cost/references/antigravity_series.json`，确保 `state/a-share-volume-watch` 目录就绪。
- **保护既有业务状态与凭据隔离**：`seed_openclaw_data.sh` 默认保留运行期既有 `state/`、`data/`、`temp/` 等业务目录，并自动跳过凭据文件名模式（`.env`、`*secret*`、`*token*.json` 等）以及 `__pycache__`/`*.pyc`。
- 修改 `scripts/dalton/data_assets/` 下的脚本并在生产重建或发布后，在宿主机数据卷上执行一次 `bash scripts/dalton/seed_openclaw_data.sh --apply` 以刷新容器挂载副本。
