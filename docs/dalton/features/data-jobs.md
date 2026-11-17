# 确定性数据任务层（Data Jobs）

本文说明 Dalton 确定性数据任务清单 `manifest.yaml` 的字段结构、容器（`container`）与宿主机（`host`）双执行器机制、调度器内存准入保护以及任务完成后自动触发监控面板发布的数据链路；排查定时抓数失败、新增一条数据任务或从关闭状态启用任务时应阅读本文。

## 1. 总开关与架构边界

- **总开关**：`src/dalton/dalton.yaml` 下 `data_jobs.enabled` 控制调度循环是否启动（当前为 `true`，最低空闲内存保护阈值 `min_free_mem_mb: 1280`）。
- **单任务默认安全阀**：所有登记在 `src/dalton/data_jobs/manifest.yaml` 中的任务默认均以 `enabled: false` 合入，启用状态以数据库 `dalton_data_task_defs` 表的运行期开关为准，确保代码合并不会意外在生产拉起新任务。
- **表结构**：迁移 `d029_data_tasks` 建立 `dalton_data_task_defs`（任务定义同步）、`dalton_data_task_slot`（调度槽位防重）与 `dalton_data_task_runs`（执行历史与输出摘要）。

## 2. Manifest 字段契约（`DataTaskManifestEntry`）

每条任务在 `src/dalton/data_jobs/manifest.yaml` 定义，由 `src/dalton/data_jobs/manifest.py:DataTaskManifestEntry` 校验加载：

| 字段 | 含义与类型 | 说明 |
|---|---|---|
| `task_code` / `job_key` | 唯一编号（如 `T5`、`T85`）与短键名（如 `cn-intraday`） | 关联监控面板绑定与调度台账主键 |
| `title` | 任务中文说明 | 展示于 Settings / System Jobs 视图 |
| `batch_no` | 迁移批次号（`1`–`4`） | 记录该任务属于哪个迁出批次 |
| `runner` | `"container"` \| `"host"` | 决定执行器走独立临时 Docker 容器还是宿主机桥接 |
| `host_mode` | `"mirror"` \| `"run"` \| `null` | `runner="host"` 时的运行模式 |
| `cron` / `timezone` | Cron 表达式与 IANA 时区 | 如 `"45 17 * * 1-5"` 与 `"Asia/Shanghai"`；可选 `cutover_cron` / `cutover_timezone` |
| `timeout_seconds` | 超时秒数 | 未填时采用 `DEFAULT_TIMEOUT_SECONDS = 1800`；另支持 `no_output_timeout_seconds`（无输出超时）与 `output_max_bytes` |
| `cwd` | 执行工作目录（默认 `/Users/everflow/.openclaw/workspace`） | 容器内会映射至受管工作副本目录 |
| `command` / `command_argv` | Shell 命令串与显式 `argv` 列表 | 容器采用 `/app/.venv/bin/python`（`CONTAINER_PYTHON_BIN`）运行 Python 脚本 |
| `secret_names` / `env` | 需注入的 Vault 密钥名列表与明文环境字典 | 如 URL 等非敏感环境在 `env` 中声明 |
| `freeze_paths` | 产物冻结路径 glob 列表 | 任务完成后核验关键数据与 HTML 输出 |
| `cos_mode` | `"skip"` \| `"prod"` | 是否在完成后上传腾讯云 COS（兼容旧发布目标） |
| `notify_enabled` / `enabled` | 默认通知开关与初始启用状态 | 控制完成后是否发出通知、是否自动排期 |

## 3. 容器执行器与宿主机执行器

- **容器执行器（`src/dalton/data_jobs/container_runner.py`）**：
  - 调度器通过 `aiodocker`（`docker.sock`）为每次运行创建**独立的临时 Docker 容器**（`dalton-dt-<job_key>-<run_id8>`，镜像默认 `dalton-langalpha-backend`），并施加资源与挂载限制：内存硬限 `1 GiB`（`Memory = MemorySwap = 1 GiB`）、CPU 限额 `2.0` 核（`NanoCpus`）、进程上限 `PidsLimit = 256`、读写挂载 `var/dalton/openclaw-data` 至 `/Users/everflow/.openclaw/workspace`、只读挂载 `var/dalton/run-secrets/<run_id>` 与宿主机 `src/`（`/app/src`），以及 `16 MiB` `tmpfs`（`/tmp/dalton-outbox`）；排查任务日志与容器资源占用来看临时任务容器而非 `backend` 容器本身。
  - **并发与内存保护**：`src/dalton/data_jobs/scheduler.py` 在派发前检查宿主机/容器可用空闲内存不低于 `min_free_mem_mb`（`1280` MiB），防止多个大内存行情计算或绘图脚本同时拉起导致 OOM。
- **宿主机执行器（`src/dalton/data_jobs/host_runner.py`）**：
  - 用于必须在宿主机运行的任务（如通过 SSH 触发 Windows 端刷新的 `T1`（`bbg-data-pull-asia`）/ `T29`（`bbg-data-pull-us`）BBG 数据拉取、`T61 weekly-token-usage` 用量统计、或需访问宿主机专用环境的脚本；注：`T22` 为 `us-top-monitor`、`T83` 为 `semi-bottom-checklist-1030et`，二者均为容器任务）。
  - 请求通过回环 `researchdata_bridge.py`（`/internal/data-tasks/host-run`、`/internal/data-tasks/host-kill`）下发，使用 **`DALTON_BRIDGE_ADMIN_TOKEN`** 鉴权（同时通过 `Authorization: Bearer` 与 `X-Dalton-Bridge-Token` 发送，桥接端由 `_require_admin_token` 严格校验，普通 `DALTON_BRIDGE_TOKEN` 会被拒绝为 403），桥接端按任务 `secret_names` 从解密通道按需注入特定密钥并严格剥离其他无关宿主机凭据。

## 4. 任务调度与面板自动发布联动

`src/dalton/data_jobs/scheduler.py` 按 `cron` 与 `timezone` 生成时间槽（`dalton_data_task_slot`），保证同一分钟槽不会重复并发触发：

1. 任务执行成功后落库 `dalton_data_task_runs` 状态。
2. 调度器立即调用 `maybe_publish_data_job_monitor(job_key)`：查询该 `job_key` 是否在 `src/dalton/monitors/data_job_registry.py:MONITOR_DATA_JOB_REGISTRY` 注册了面板绑定。
3. 若存在绑定且 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 为开启状态，自动校验目标 HTML/静态文件并发布新版本至对应监控面板（见 [monitors.md](./monitors.md)），无需人工再点“发布”。
