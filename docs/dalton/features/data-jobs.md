# 确定性数据任务层（Data Jobs）

这页讲清 Dalton 确定性数据任务层 `manifest.yaml` 的字段契约、容器（`container`）和宿主机（`host`）双执行器、内存准入保护，以及任务跑完后的监控面板自动发布链路。

## 1. 总开关与架构边界

- **总开关**：`src/dalton/dalton.yaml` 下的 `data_jobs.enabled` 控制调度循环要不要启动，目前是 `true`，最低空闲内存保护阈值 `min_free_mem_mb: 1280`。
- **单任务默认安全阀**：登记在 `src/dalton/data_jobs/manifest.yaml` 里的任务（当前共 42 项）默认都以 `enabled: false` 合入。真正在不在生产跑，看数据库 `dalton_data_task_defs` 表的运行期开关，免得合一次代码就把新任务意外拉起来。
- **表结构**：迁移 `d029_data_tasks` 建了三张表——`dalton_data_task_defs`（任务定义同步）、`dalton_data_task_slot`（调度槽位防重）和 `dalton_data_task_runs`（执行历史与输出摘要）。

## 2. Manifest 字段契约（`DataTaskManifestEntry`）

每条任务都在 `src/dalton/data_jobs/manifest.yaml` 里定义，由 `src/dalton/data_jobs/manifest.py:DataTaskManifestEntry` 校验加载：

| 字段 | 含义与类型 | 用途说明 |
|---|---|---|
| `task_code` / `job_key` | 唯一编号（如 `T5`、`T85`）与短键名（如 `cn-intraday`） | 关联监控面板绑定与调度台账主键 |
| `title` | 任务中文说明 | 展示在 Settings / System Jobs 视图 |
| `batch_no` | 迁移批次号（`1`–`4`） | 记下这条任务属于哪个迁出批次 |
| `runner` | `"container"` \| `"host"` | 指定走独立临时 Docker 容器还是宿主机桥接 |
| `host_mode` | `"mirror"` \| `"run"` \| `null` | `runner="host"` 时的运行模式 |
| `cron` / `timezone` | Cron 表达式与 IANA 时区 | 比如 `"45 17 * * 1-5"` 和 `"Asia/Shanghai"`；可选 `cutover_cron` / `cutover_timezone` |
| `timeout_seconds` | 超时秒数 | 没填时用 `DEFAULT_TIMEOUT_SECONDS = 1800`；还支持 `no_output_timeout_seconds`（无输出超时）和 `output_max_bytes` |
| `cwd` | 执行工作目录（默认 `/Users/everflow/.openclaw/workspace`） | 容器里会映射到受管工作副本目录 |
| `command` / `command_argv` | Shell 命令串与显式 `argv` 列表 | 容器里用 `/app/.venv/bin/python`（`CONTAINER_PYTHON_BIN`）跑 Python 脚本 |
| `secret_names` / `env` | 要注入的 Vault 密钥名列表与明文环境字典 | URL 等非敏感环境声明在 `env` 里 |
| `freeze_paths` | 产物冻结路径 glob 列表 | 任务跑完后核对关键数据和 HTML 输出 |
| `notify_enabled` / `enabled` | 默认通知开关与初始启用状态 | 控制跑完要不要发通知、要不要自动排期 |

## 3. 容器执行器与宿主机执行器

- **容器执行器（`src/dalton/data_jobs/container_runner.py`）**：
  - 调度器通过 `aiodocker`（`docker.sock`）为每次运行拉起**独立的临时 Docker 容器**（`dalton-dt-<job_key>-<run_id8>`，镜像默认 `dalton-langalpha-backend`），并加上资源与挂载硬限：内存硬限 `1 GiB`（`Memory = MemorySwap = 1 GiB`）、CPU 限额 `2.0` 核（`NanoCpus`）、进程上限 `PidsLimit = 256`、读写挂载 `var/dalton/openclaw-data` 到 `/Users/everflow/.openclaw/workspace`、只读挂载 `var/dalton/run-secrets/<run_id>` 和宿主机 `src/`（`/app/src`），再加上 `16 MiB` `tmpfs`（`/tmp/dalton-outbox`）。排查任务日志和容器资源占用时，直接看对应的临时任务容器。
  - **并发与内存保护**：`src/dalton/data_jobs/scheduler.py` 在派发前检查宿主机或容器可用空闲内存不少于 `min_free_mem_mb`（`1280` MiB），防止多个大内存行情计算或绘图脚本同时拉起来把机器打爆。
- **宿主机执行器（`src/dalton/data_jobs/host_runner.py`）**：
  - 用来跑必须在宿主机执行的任务，比如通过 SSH 触发 Windows 端刷新的 `T1`（`bbg-data-pull-asia`）/ `T29`（`bbg-data-pull-us`）BBG 数据拉取、`T34`（`richard-relative-dashboard-refresh`）、`T58`（`gs-pb-weekly-rundown`）、`T60`（`podcast-weekly-pipeline`）、`T61`（`weekly-token-usage`）用量统计、`T87`（`model-availability-watch`）模型可用性监控等。注意 `T22`（`us-top-monitor`）和 `T83`（`semi-bottom-checklist-1030et`）都是容器任务。
  - 请求走回环 `researchdata_bridge.py`（`/internal/data-tasks/host-run`、`/internal/data-tasks/host-kill`）下发，用 **`DALTON_BRIDGE_ADMIN_TOKEN`** 鉴权（同时通过 `Authorization: Bearer` 和 `X-Dalton-Bridge-Token` 发送，桥接端由 `_require_admin_token` 严格校验，普通 `DALTON_BRIDGE_TOKEN` 会被直接拒成 403）。桥接端只按任务 `secret_names` 从解密通道按需注入指定密钥，其余宿主机凭据一律剥掉。
  - **宿主机任务通知投递**：宿主机定时任务运行结束后，调度器同样通过 `read_run_artifacts` 读取桥接留在 `.dalton-runs/<run_id>/outbox.json` 中的投递载荷再发通知；无通知产物时正常跳过投递，严格读取或校验运行元数据（`outbox.json`、`frozen_manifest.json`）报出 `SafeFsError` 时（包括符号链接、文件打开失败、内容解析或校验失败），与容器任务一致按 `invalid_run_metadata:<错误码>` 记失败。

## 4. 任务调度与面板自动发布联动

`src/dalton/data_jobs/scheduler.py` 按 `cron` 和 `timezone` 生成时间槽（`dalton_data_task_slot`），确保同一分钟槽只触发一次：

1. 任务跑完成功后，把状态写进 `dalton_data_task_runs`。
2. 调度器立刻调用 `maybe_publish_data_job_monitor(job_key)`，查这个 `job_key` 有没有在 `src/dalton/monitors/data_job_registry.py:MONITOR_DATA_JOB_REGISTRY` 登记面板绑定。
3. 只要有绑定且 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 开着，就自动校验目标 HTML 和静态文件，把新版本发到对应监控面板（见 [monitors.md](./monitors.md)），一条龙完成发布。
