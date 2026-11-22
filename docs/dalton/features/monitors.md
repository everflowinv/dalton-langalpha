# 监控面板平台（Monitors）

这页讲清 Dalton 监控面板平台的数据库结构、四档可见权限、数据任务绑定关系、实时数据内部写入与短时签名授权（grant），以及发布总开关怎么控制读写行为。面板平台既支持发布工作区网页，也支持由确定性数据任务（`data_job`）自动构建静态面板并写入动态快照。

## 1. 核心数据模型与目录分类

面板平台的持久化表结构在迁移 `d038_monitors` 和 `d039_monitor_publish_runs`，代码模块在 `src/dalton/monitors/`：

- **`dalton_monitors`**：面板主表，存着 `slug`、`category`、`visibility`、`source_kind`（工作区网页或 `data_job`）、`acl_revision` 和 `published_seq`（注：`approved_by_admin_user_id` 定义在代码绑定对象 `MonitorDataJobBinding` 里，作为代码属性存在，表 `dalton_monitors` 里没有这一列）。
- **`dalton_monitor_members`**：指定成员清单，用于 `members` 档权限控制。
- **`dalton_monitor_data`**：实时键值数据表，按 `(monitor_id, data_key)` 存动态 JSON 快照，比如 A 股盘中快照 `snapshot`、港股动量 `intraday`。
- **`dalton_monitor_versions` & `dalton_monitor_publish_runs`**：保存每次发布的不可变版本快照（上限 `MAX_MONITOR_VERSIONS = 100`）和发布运行审计日志。
- **分类枚举（`src/dalton/monitors/models.py:MONITOR_CATEGORIES`）**：`overview`、`us`、`cn`、`hk`、`asia`、`stocks`、`portfolio`、`other`。

## 2. 权限四档控制与实时校验

`src/dalton/monitors/models.py:MONITOR_VISIBILITIES` 定义了四档可见性，校验逻辑在 `src/dalton/monitors/permissions.py`。列表、详情和资源读取每次都会实时重算公司等级（`get_user_tier`）：

| 档位 | 允许查看对象 | 设立/修改条件与边界 |
|---|---|---|
| `own` | 只有面板属主本人 | `external` 级用户只能创建和保持 `own` 面板 |
| `members` | 属主 + `dalton_monitor_members` | 要求公司内部等级为 `admin` 或 `internal`；成员事后被降级时，读取当场被拒 |
| `company` | 全体 `admin` 与 `internal` 成员 | 只有内部成员及以上能看，外部账号看不了 |
| `public` | 匿名公开只读 | 只有 `admin` 能设或重发；`data_job` 公开面板必须记 `approved_by_admin_user_id` 并在每次重发时复核；非属主管理员也能管理、降级或删掉公开面板 |

## 3. 数据任务绑定登记（`MONITOR_DATA_JOB_REGISTRY`）

确定性数据任务生成的静态 HTML 面板，必须在 `src/dalton/monitors/data_job_registry.py:MONITOR_DATA_JOB_REGISTRY` 显式登记绑定，调度器才会在任务跑完后调 `src/dalton/data_jobs/scheduler.py:maybe_publish_data_job_monitor` 自动发布更新：

| 数据任务 ID | 绑定 Slug | 分类与排序权重 | 静态目录 / 共享目录 | 实时 `data_keys` |
|---|---|---|---|---|
| `monitor-live-demo` | `live-demo` | `overview`(0)（示例验证绑定） | `research-outputs/monitor-demo` | `("metrics",)` |
| `sentiment-dashboard-refresh` | 6 项面板（`SENTIMENT_DASHBOARD_PANELS`） | `overview`(10) / `us`(20) / `cn`(30) / `hk`(40) / `asia`(50) / `stocks`(60) | `research-outputs/sentiment-dashboard/panels/<slug>` + `_shared` | 无（静态重建） |
| `richard-relative-dashboard-refresh` | `relative-strength` | `stocks`(65) | `research-outputs/kweb-dashboard` | 无 |
| `pm-market-state` | `pm-market-state`、`cn-sector-rotation` | `us`(25)、`cn`(35) | `research-outputs/pm-market-state/pm-market-state`、`research-outputs/pm-market-state/cn-sector-rotation` | 无 |
| `cn-intraday` | `cn-intraday` | `cn`(32) | `research-outputs/cn-intraday` | `("snapshot",)` |
| `hk-momentum` | `hk-momentum` | `hk`(45) | `research-outputs/hk-momentum` | `("intraday",)` |

文件读取器 `HostDirFileSource` 和 `ScopedMountFileSource` 及其单文件上限 **`MAX_HOST_FILE_BYTES = 25 MiB`** 都定义在 `src/dalton/monitors/data_job_registry.py`（由 `src/dalton/monitors/service.py` 导入），打开目录时带 `O_DIRECTORY | O_NOFOLLOW`，拒绝任何符号链接逃逸。

## 4. 实时数据写入与签名授权（Grant）

- **实时数据内部写入接口**：`PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}`（`src/dalton/monitors/router.py`），靠 `require_monitor_service_token` 校验请求头 `X-Service-Token` 必须等于 `INTERNAL_SERVICE_TOKEN`。带 `x-user-id` 的伪造调用会被直接拒绝，同时校验目标面板必须满足 `source_kind == "data_job"` 且 `key` 属于绑定声明的 `data_keys`。
- **短时签名授权（`src/dalton/monitors/grants.py:mint_monitor_grant`）**：前端加载或刷新面板内容时，后端会签一张有效期 **12 小时**（`DEFAULT_TTL_SECONDS = 43200`）的 HMAC 令牌，载荷绑着 `(monitor_id, user_id, acl_revision, exp)`，签名密钥存在数据库 `server_keys` 表的 `'monitor_grant'` 行。
- **沙箱静态与数据托管路由**：
  - `/api/v1/dalton/monitors/g/{grant}/v/{seq}/{path}`：返回静态页面和共享脚本，响应头强制带上 `Content-Security-Policy: sandbox` 隔离脚本权限。
  - `/api/v1/dalton/monitors/g/{grant}/data/{key}`：返回实时 JSON 快照，支持 `ETag` / `304 Not Modified`，每次请求都会重查 `acl_revision` 和用户实时访问权。

## 5. 发布总开关（`DALTON_ARTIFACT_PUBLISHING_ENABLED`）

面板和产物的对外发布受总开关控制（环境变量 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 会盖过 `src/dalton/dalton.yaml` 里的 `artifact_publishing.enabled`，代码默认 `false`）：

- 开关关着时：
  - **HTTP 发布与重发接口**（`publish_workspace_monitor` / `republish_workspace_monitor`，把已有 `public` 面板降级成非公开的情况除外）直接返回 **`403`**（`{"code": "artifact_publishing_disabled"}`）。
  - **后台自动更新钩子**（数据任务跑完后的 `maybe_publish_data_job_monitor` 和对话回合结束后的 `republish_workspace_monitors_after_turn`）直接提前返回跳过发布，静默结束而不抛 `403`。
- 作为止血退路，即使总开关关着，管理员和用户依然可以只读看已有公开面板、把已有 `public` 面板降成非公开，或者删掉风险面板。
