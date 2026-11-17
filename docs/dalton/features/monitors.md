# 监控面板平台（Monitors）

本文说明 Dalton 监控面板平台的数据库结构、四档可见性权限、数据任务注册与绑定关系、实时数据内部写入与短时签名授权（grant）机制，以及发布总开关的行为约束；开发新的数据面板、调整面板授权策略或排查发布 403 错误时应查阅本文。结论上，面板平台既支持把工作区网页发布成团队面板，也支持由确定性数据任务（`data_job`）自动构建静态面板并通过内部 HTTP 写入动态快照数据。

## 1. 核心数据模型与目录分类

面板平台的持久化schema位于迁移 `d038_monitors` 与 `d039_monitor_publish_runs`（模块位于 `src/dalton/monitors/`）：

- **`dalton_monitors`**：面板主表，包含 `slug`、`category`、`visibility`、`source_kind`（工作区网页或 `data_job`）、`acl_revision` 与 `published_seq`（注：`approved_by_admin_user_id` 定义在代码绑定对象 `MonitorDataJobBinding` 中，不作为列存于 `dalton_monitors`）。
- **`dalton_monitor_members`**：指定成员清单（用于 `members` 档权限控制）。
- **`dalton_monitor_data`**：实时键值数据表，按 `(monitor_id, data_key)` 存储动态 JSON 快照（例如 A 股盘中快照 `snapshot`、港股动量 `intraday`）。
- **`dalton_monitor_versions` & `dalton_monitor_publish_runs`**：保存每次发布的不可变版本快照（上限 `MAX_MONITOR_VERSIONS = 100`）及发布运行审计日志。
- **分类枚举（`src/dalton/monitors/models.py:MONITOR_CATEGORIES`）**：`overview`、`us`、`cn`、`hk`、`asia`、`stocks`、`portfolio`、`other`。

## 2. 权限四档控制与实时校验

`src/dalton/monitors/models.py:MONITOR_VISIBILITIES` 定义四档可见性（权限校验逻辑位于 `src/dalton/monitors/permissions.py`），所有访问在列表、详情及资源读取时实时重算公司等级（`get_user_tier`）：

| 档位 | 允许查看对象 | 设立/修改条件与边界 |
|---|---|---|
| `own` | 仅面板属主本人 | `external` 级用户只允许创建和保持 `own` 面板 |
| `members` | 属主 + `dalton_monitor_members` | 要求公司内部等级为 `admin` 或 `internal`；成员若事后被降级，在读取时实时被拒 |
| `company` | 全体 `admin` 与 `internal` 成员 | 仅内部成员及以上可见；外部账号无法访问 |
| `public` | 匿名公开只读 | 仅 `admin` 可设置或重新发布；`data_job` 公开面板必须记录 `approved_by_admin_user_id` 并在每次重发时复核；非属主管理员也可管理、降级或删除公开面板 |

## 3. 数据任务绑定登记（`MONITOR_DATA_JOB_REGISTRY`）

确定性数据任务生成的静态HTML面板必须在 `src/dalton/monitors/data_job_registry.py:MONITOR_DATA_JOB_REGISTRY` 显式登记绑定关系，才会在任务完成后自动发布更新（由调度器在任务成功后调用 `src/dalton/data_jobs/scheduler.py:maybe_publish_data_job_monitor`）：

| 数据任务 ID | 绑定 Slug | 分类与排序权重 | 静态目录 / 共享目录 | 实时 `data_keys` |
|---|---|---|---|---|
| `monitor-live-demo` | `live-demo` | 示例验证绑定 | `research-outputs/monitor-demo` | `("metrics",)` |
| `sentiment-dashboard-refresh` | 6 项面板（`SENTIMENT_DASHBOARD_PANELS`） | `overview`(10) / `us`(20) / `cn`(30) / `hk`(40) / `asia`(50) / `stocks`(60) | `research-outputs/sentiment-dashboard/panels/<slug>` + `_shared` | 无（静态重build） |
| `pm-market-state` | `pm-market-state`、`cn-sector-rotation` | `us`(25)、`cn`(35) | 对应输出目录 | 无 |
| `cn-intraday` | `cn-intraday` | `cn`(32) | 对应静态目录 | `("snapshot",)` |
| `hk-momentum` | `hk-momentum` | `hk`(45) | 对应静态目录 | `("intraday",)` |

文件读取器 `HostDirFileSource` 与 `ScopedMountFileSource` 及其单文件大小上限 **`MAX_HOST_FILE_BYTES = 25 MiB`** 定义在 `src/dalton/monitors/data_job_registry.py`（由 `src/dalton/monitors/service.py` 导入调用），使用系统调用标志 `O_DIRECTORY | O_NOFOLLOW` 打开目录并拒绝任何符号链接逃逸。

## 4. 实时数据写入与签名授权（Grant）

- **实时数据内部写入接口**：`PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}`（`src/dalton/monitors/router.py`），通过 `require_monitor_service_token` 验证请求头 `X-Service-Token` 必须等于 `INTERNAL_SERVICE_TOKEN`；显式拒绝携带 `x-user-id` 的伪造调用，并校验目标面板必须为 `source_kind == "data_job"` 且 `key` 属于绑定声明的 `data_keys`。
- **短时签名授权（`src/dalton/monitors/grants.py:mint_monitor_grant`）**：前端加载或刷新面板内容时，后端签发有效期 **12 小时**（`DEFAULT_TTL_SECONDS = 43200`）的 HMAC 令牌，载荷绑定 `(monitor_id, user_id, acl_revision, exp)`，签名密钥存放于数据库 `server_keys` 表的 `'monitor_grant'` 行。
- **沙箱静态与数据托管路由**：
  - `/api/v1/dalton/monitors/g/{grant}/v/{seq}/{path}`：返回静态页面与共享脚本，响应头强制带 `Content-Security-Policy: sandbox` 隔离脚本权限。
  - `/api/v1/dalton/monitors/g/{grant}/data/{key}`：返回实时 JSON 快照，支持 `ETag` / `304 Not Modified`，并在每次请求时重新核查 `acl_revision` 与用户实时访问权。

## 5. 发布总开关（`DALTON_ARTIFACT_PUBLISHING_ENABLED`）

面板与产物的对外发布受总开关控制（环境变量 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 覆盖 `src/dalton/dalton.yaml` 中的 `artifact_publishing.enabled`，代码默认 `false`）：

- 开关为关闭时：
  - **HTTP 发布与重发接口**（`publish_workspace_monitor` / `republish_workspace_monitor`，除对已有 `public` 面板降级为非公开外）返回 **`403`**（`{"code": "artifact_publishing_disabled"}`）。
  - **后台自动更新钩子**（数据任务完成后的 `maybe_publish_data_job_monitor` 与对话轮次结束后的 `republish_workspace_monitors_after_turn`）直接提前返回并跳过发布，不抛出 `403`。
- 作为止血退路，即使开关关闭，仍保留已有公开面板的只读查看、已有 `public` 面板降级为非公开以及删除操作，不锁死管理员清理风险入口的能力。
