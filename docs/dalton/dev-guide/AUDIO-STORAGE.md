# 大文件与音频直传存储指南（MinIO / S3 兼容）

本文档说明 Dalton 大文件（含长录音音频）存储与分片直传的架构：第 0 节是生产实际采用的转写独立存储方案；第 1–6 节是上游全局存储（生产未启用）与浏览器直传的机制说明、环境变量与 YAML 配置优先级、Caddy 反代签名约束、MinIO 幂等初始化及磁盘容量估算。

---

## 0. 生产方案：转写独立存储

生产**不配置**全局 `STORAGE_*` 对象存储并保持关闭：上游 20 多处功能（截图、工作区备份 / 恢复、头像、技能归档、备忘录、对话记录、下载链接、多模态上下文……）都以它为开关，一旦打开会同时改变行为，且给浏览器的链接会用到不适合浏览器的地址。录音转写（A1）因此使用一套独立的存储配置 `DALTON_ASR_STORAGE_*`，全局存储保持关闭、上游功能零影响。

三个角色经不同地址访问同一个 MinIO（MinIO 只绑定宿主机 `127.0.0.1:9000`，见 `docker-compose.dalton.yml`；不需要公网域名、不需要 Caddy 反代、不需要沙箱接入 Compose 网络）：

| 角色 | 访问地址 | 配置 |
|---|---|---|
| 后端容器（上传 / 组装 / 校验） | `http://host.docker.internal:9000` | `DALTON_ASR_STORAGE_ENDPOINT_URL=http://host.docker.internal:9000` |
| 沙箱（分片 PUT 的目标；`dalton_asr.py`） | `http://host.docker.internal:9000` | `DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL=http://host.docker.internal:9000` —— 预签名分片 URL 按此地址签名；未设置时回退 `DALTON_ASR_STORAGE_ENDPOINT_URL` |
| 桥（宿主机上流式下载音频；`asr_jobs.py`） | `http://127.0.0.1:9000` | `DALTON_ASR_STORAGE_ENDPOINT_URL=http://127.0.0.1:9000`（在桥自己的环境文件里单独设置，不与后端共用） |

其余变量：`DALTON_ASR_STORAGE_BUCKET_NAME`、`DALTON_ASR_STORAGE_ACCESS_KEY_ID`、`DALTON_ASR_STORAGE_SECRET_ACCESS_KEY` 必填（这四项——含端点——齐备，`is_transcription_storage_configured()` 才为真，总开关 `DALTON_TRANSCRIPTION_ENABLED` 开启还要求它）；`DALTON_ASR_STORAGE_REGION` 默认 `us-east-1`；`DALTON_ASR_STORAGE_ADDRESSING_STYLE` 默认 `path`（MinIO 必须用 path 寻址）；`DALTON_ASR_STORAGE_KEY_PREFIX` 为可选的对象键前缀。

实现与一致性约束：

- 后端实现是 `src/dalton/transcription_storage.py`（与全局 `src/utils/storage/` 完全独立）；桥侧由 `scripts/dalton/asr_jobs.py` 的 `SourceObjectStore` 读取同一组变量。
- 对象键前缀规则两侧**必须完全一致**（后端 `resolve_full_key` 与桥 `SourceObjectStore.resolve_full_key`），否则后端上传的对象桥会找不到；该一致性由 `tests/unit/dalton/test_transcription_storage.py` 的 `test_resolve_full_key_matches_bridge_source_object_store` 钉死。
- 分片上传的行为与全局实现一致：每个分片的字节长度签入预签名 URL（`sign_upload_part` 的 `ContentLength` 绑定），`list_uploaded_parts` 返回 `{"part_number", "size", "etag"}` 且以存储侧清单为准。
- 沙箱向 `host.docker.internal` 的分片 PUT 走沙箱普通出网，不经出口代理；部署时确认 Docker 沙箱内能解析 `host.docker.internal`（Docker Desktop 内建；原生 Linux 不会自动解析，本仓库由 Compose 与 Docker 沙箱 provider 添加的 `host.docker.internal:host-gateway` 映射提供，Docker Engine 需 20.10+）。

---

> **以下第 1–6 节为上游全局存储（生产未启用）的既有说明**，保留供非生产部署与浏览器 / 工作区直传参考。生产按第 0 节配置，`STORAGE_*` 全部保持未配置；文中 A1 转写使用 `STORAGE_BROWSER_ENDPOINT_URL` 的历史方案已由第 0 节取代。

---

## 1. 三种网络视角与 Endpoint 职责

在 `docker-compose.dalton.yml` 中，`minio` 服务仅绑定在宿主机回环地址 `127.0.0.1:9000`。**在 Linux 宿主机上，容器内的 `host.docker.internal` 解析为 Docker 网桥网关 IP（如 `172.17.0.1`），无法连通宿主机 `127.0.0.1:9000`**。因此后端必须通过 Docker Compose 网络以服务名 `http://minio:9000` 通信，浏览器通过 HTTPS 反向代理域名访问（实现见 `src/utils/storage/s3_compatible.py:99-137`）。沙箱分两条路径：**录音转写（A1）的分片上传已改用独立的 `DALTON_ASR_STORAGE_*` 存储并经 `host.docker.internal` 访问 MinIO（生产方案见第 0 节；本节的历史方案曾与浏览器 2 GiB 直传共用 `STORAGE_BROWSER_ENDPOINT_URL` 公网 HTTPS 反代），沙箱不需要也不应接入 Compose 网络**（接入会让沙箱内的代理代码直连后端的 Postgres / Redis）；仅工作区直传拉取（`workspace_upload_pull.py`）在 `STORAGE_BLOB_TRANSFER=direct` 时才需要加入同一 Compose 网络，该项在生产保持关闭：

| 网络视角 | 使用的环境变量 | 调用阶段与职责 | 必须配置的值 |
|---|---|---|---|
| **后端容器**（`backend`） | `STORAGE_ENDPOINT_URL` | `create_multipart_upload`、`complete_multipart_upload`、`abort_multipart_upload`、`head_object`、`delete_object`，以及生成供沙箱拉取的预签名 GET URL（`sign_get_url`） | `http://minio:9000`（Compose 容器内网直连） |
| **沙箱容器**（录音转写 A1 分片上传，`dalton_asr.py`） | `DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL`（转写独立存储） | 后端 `sign_transcription_upload_part` 用转写独立存储的签名客户端（`src/dalton/transcription_storage.py`）签发预签名 PUT URL，分片字节长度签入 URL；沙箱内的 `dalton_asr.py` 直接向该地址 `PUT` 音频分片，**不需要也不应接入 Compose 网络**（未配置该地址时回退签 `DALTON_ASR_STORAGE_ENDPOINT_URL`） | 生产：`http://host.docker.internal:9000`（见第 0 节） |
| **沙箱容器**（工作区直传拉取，`workspace_upload_pull.py`；仅 `STORAGE_BLOB_TRANSFER=direct` 时，生产保持关闭） | `STORAGE_ENDPOINT_URL`（使用后端签发的 `get_url`） | `sandbox.pull_direct(url=get_url, ...)`，在沙箱内流式 `GET` 临时对象到 `.wsfiles-upload-<upload_id>` 并原子重命名到目标路径；此路径需要沙箱加入同一 Compose 网络（在 `agent_config.yaml` / `agent_config.dalton.yaml` 中配置 `sandbox.docker.network_mode`） | `http://minio:9000`（Compose 容器内网直连） |
| **浏览器前端** | `STORAGE_BROWSER_ENDPOINT_URL` | 后端用 `_browser_client` 为每个分片签发预签名 PUT URL（`sign_part_url`）；浏览器直接 `PUT` 分片并读取响应头 `ETag` | 生产：`https://storage.<domain>`（经 Caddy HTTPS 反代到 `127.0.0.1:9000`）；仅本机同机调试可用 `http://127.0.0.1:9000` |

### 关键网络与反代注意事项

1. **Caddy 2.9 反代必须显式保留 `Host` 头（防止 `SignatureDoesNotMatch`）**：
   - S3 Signature V4 将 `Host` 签入 `X-Amz-SignedHeaders=host`。后端 `_browser_client` 使用 `STORAGE_BROWSER_ENDPOINT_URL` 的 host:port 计算签名。
   - **Caddy 2.9 在反代时若改写上游 `Host` 头，会导致 MinIO 校验签名与请求 `Host` 不一致而返回 `403 SignatureDoesNotMatch`**。
   - 在 `deploy/dalton/Caddyfile`（或宿主 Caddy 配置）反代 MinIO 时，**必须显式配置 `header_up Host {http.request.host}`**：
     ```caddyfile
     storage.example.com {
         reverse_proxy 127.0.0.1:9000 {
             header_up Host {http.request.host}
         }
     }
     ```
2. **HTTPS 页面 Mixed Content 限制**：
   若前端通过 `https://` 访问，`STORAGE_BROWSER_ENDPOINT_URL` 必须同样为 `https://`，否则浏览器会拦截向 `http://` 发起的分片 `PUT`。
3. **Docker 沙箱网络配置（`sandbox.docker.network_mode`）**：
   - 仅工作区直传拉取（`workspace_upload_pull.py`）在 `STORAGE_BLOB_TRANSFER=direct` 时需要沙箱加入 Compose 网络：因为后端 `sign_get_url` 使用 `STORAGE_ENDPOINT_URL=http://minio:9000` 签名，Docker 沙箱容器必须与 `backend`、`minio` 处于同一 Compose 网络，才能解析 `minio` 并通过签名校验；该项在生产保持关闭。
   - 录音转写（A1）的沙箱分片上传使用独立的 `DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL`（见第 0 节生产方案），沙箱不需要也不应为此接入 Compose 网络。
   - 实际配置字段位于 `agent_config.yaml` / `agent_config.dalton.yaml` 的 `sandbox.docker.network_mode`（见 `src/ptc_agent/config/core.py:461`、`src/ptc_agent/core/sandbox/providers/docker.py:704`；Dalton 默认值为 `"bridge"`，见 `agent_config.yaml:125`；**注意不是 `config.dalton.yaml` 的 `sandbox.docker.network`**）。
   - **仅非生产方案使用**：工作区直传拉取（`STORAGE_BLOB_TRANSFER=direct` + 沙箱接入 Compose 网络）是非生产方案，生产保持关闭（`STORAGE_BLOB_TRANSFER` 不设置或保持默认，`network_mode` 维持默认 `"bridge"`，沙箱不接入 Compose 网络）；只有启用该方案时，才在 `agent_config.dalton.yaml`（或 `agent_config.yaml`）中将其设为 Compose 网络名（可通过 `docker network ls` 查看，通常为 `<project>_default`，如 `repo_default`）：
     ```yaml
     sandbox:
       docker:
         network_mode: "repo_default" # 替换为实际的 docker compose 网络名（仅上述非生产方案使用；生产保持默认 "bridge"）
     ```
4. **沙箱访问存储地址走普通出网**：`dalton_asr.py` 向 `DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL`（生产 `http://host.docker.internal:9000`）发出的分片 `PUT` 不经出口代理，走沙箱的普通出网；部署时确认沙箱能解析 `host.docker.internal` 并连通宿主机的 `127.0.0.1:9000`。

---

## 2. 全部环境变量清单

以下变量用于启用 MinIO / S3 分片直传（参见 `.agy/backlog/backlog_codex_result.md` 第二节第 1 小节、`src/utils/storage/__init__.py`、`src/utils/storage/s3_compatible.py:99-137`）：

| 变量名 | 代码默认值 | 直传推荐值 / 示例 | 说明 |
|---|---|---|---|
| `STORAGE_PROVIDER` | `"s3"`（`agent_config.yaml:206` 已固定） | `s3` | S3 兼容存储；若未配置 `STORAGE_BUCKET_NAME`，运行时自动降级为 `"none"`（`__init__.py:65-70`） |
| `STORAGE_BUCKET_NAME` | 无（必填） | `langalpha-uploads` | S3 / MinIO 桶名（桶必须为私有） |
| `STORAGE_ACCESS_KEY_ID` / `MINIO_ROOT_USER` | 无（必填，**无默认凭据**） | `<随机高强度用户名>` | MinIO 启动必填凭据，未设置时 `minio` 容器拒绝启动 |
| `STORAGE_SECRET_ACCESS_KEY` / `MINIO_ROOT_PASSWORD` | 无（必填，**无默认凭据**） | `<随机高强度密钥>` | MinIO 启动必填密钥，未设置时 `minio` 容器拒绝启动 |
| `STORAGE_REGION` | `us-east-1` | `us-east-1` | MinIO 默认区域 |
| `STORAGE_ADDRESSING_STYLE` | `virtual`（`s3_compatible.py:89`） | **`path`**（必填） | MinIO 必须显式设为 `path`，否则默认 `virtual` 会拼接 `<bucket>.minio:9000` 导致 DNS 失败 |
| `STORAGE_ENDPOINT_URL` | 无（必填） | `http://minio:9000` | 后端容器在 Compose 网络内访问 MinIO 的地址；沙箱仅在非生产的工作区直传拉取方案（`STORAGE_BLOB_TRANSFER=direct`，生产保持关闭）下使用它，A1 转写与浏览器直传都不经过该地址 |
| `STORAGE_BROWSER_ENDPOINT_URL` | 回退到 `STORAGE_ENDPOINT_URL` | `https://storage.example.com` | 浏览器直传 `PUT` 分片的公网 HTTPS 反代地址 |
| `STORAGE_BLOB_TRANSFER` | **`auto`**（`__init__.py:78`） | **生产：不设置（保持默认 `auto`）** | 工作区直传拉取的路径开关：`direct` 要求 Docker 沙箱接入 Compose 网络（见第 1 节），是**非生产方案**，生产不设置或保持默认——`auto` 仅对远程沙箱（Daytona/E2B）启用 `direct`，对 Docker 沙箱回退为 `relay`（`__init__.py:90-106`）。生产不开 `direct` 时，文件面板 / 工作区上传全部走后端内存中继（`POST /{workspace_id}/files/upload`，`crud.py:1364`），单文件上限即中继上限：`RELAY_MAX_BYTES` = 256 MiB（`blob_keys.py:39`；`MAX_UPLOAD_BYTES = RELAY_MAX_BYTES`，`_shared.py:202`；生效值为 `min(RELAY_MAX_BYTES, scan_cap)`，见 `uploads.py:183`、`crud.py:1417-1419`），超过直接返回 `413`（见第 6 节）。**只有 A1 转写的沙箱分片上传**不依赖本变量也不依赖全局存储——它使用独立的 `DALTON_ASR_STORAGE_*` 存储（第 0 节），由沙箱直接 `PUT` 到 `DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL`（不检查传输模式与 `browser_uploads_enabled`）；但录音需先放进工作区，单个录音文件因此也受中继上限约束（常见 2 小时 mp3 约 115 MB，在 256 MiB 上限内；超大的无损 WAV 需先转码或压缩） |
| `STORAGE_BROWSER_UPLOADS` | **`false`**（`__init__.py:85-87`） | **生产：不设置（默认 `false`）**；仅与非生产的 `direct` 方案一起设为 `true` | 浏览器分片直传的开关，但**单独设 `true` 并不能开启直传**：`direct_available()`（`uploads.py:108-118`）还要求传输模式已解析为 `direct`（即 `scan_cap_bytes` 返回 `None`，`transfer.py:224-238`）且沙箱非 SSH——`relay` 模式下浏览器分片直传不可用（`browser_uploads_enabled` 仅在这一处被检查，`uploads.py:115`），上传一律走后端中继单请求路径。生产（`auto` → Docker 回退 `relay`）不设置本变量；A1 转写的沙箱分片上传同样不依赖它 |
| `STORAGE_KEY_PREFIX` | 空字符串 `""` | `dalton/`（或保持空） | 对象键全局前缀；**一旦上线切勿修改**（见第 4 节） |
| `DALTON_CORS_ALLOWED_ORIGINS` | 无（必填） | `https://dalton.example.com` | 逗号分隔的允许浏览器源（禁止 `*`）；在 `docker-compose.dalton.yml` 中注入为 `MINIO_API_CORS_ALLOW_ORIGIN` |
| `DALTON_UPLOADS_EXPIRE_DAYS` | `2` | `2`（仅允许 `1` 或 `2`） | `configure_minio.sh` 为 `<STORAGE_KEY_PREFIX>uploads/` 配置的对象过期天数 |
| `MINIO_API_STALE_UPLOADS_EXPIRY` | `24h` | `24h` | MinIO 自动清理未完成分片上传（Incomplete Multipart Uploads）的过期时间 |
| `MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL` | `6h` | `6h` | MinIO 扫描并清理未完成分片上传的周期 |
| `STORAGE_PUBLIC_URL_BASE` | 无 | **留空 / 不设置** | 桶为私有，所有拉取均走短期预签名 URL |
| `STORAGE_MAX_UPLOAD_SIZE` | `10485760`（代码默认 10 MiB，`.env.example` 30 MiB） | **保持现有值不改** | 仅控制后端内存中转接口与聊天附件上限；工作区直传有独立的 2 GiB 上限（`MAX_DIRECT_UPLOAD_BYTES`），**切勿调大 `STORAGE_MAX_UPLOAD_SIZE`** |
| `DALTON_TRANSCRIPTION_ENABLED` | 未设置（**默认关闭**） | `1`（仅 `1`/`true`/`yes`/`on`，大小写不敏感，算开启） | 录音转写总开关（A1）：关闭时 `prepare_transcription` 工具不注册、`dalton-transcript-polish` 技能隐藏、`/api/v1/dalton/transcriptions*` 返回 404、对账循环不跑；**必须先配好第 0 节的转写独立存储（`DALTON_ASR_STORAGE_*`）与火山 ASR / TOS 凭据、并用真实凭据端到端验证后再开启**（开启还要求 `is_transcription_storage_configured()` 为真，见 `src/dalton/transcription_storage.py`；与全局 `STORAGE_*` 是否配置无关）；开启前还需在**桥所用的 Python**（launchd 里配置的那个，通常为 `/opt/homebrew/bin/python3`）安装 `boto3`——桥现在没有 boto3 也能启动，但转写任务会以不可重试的 `asr_boto3_missing` 失败 |
| `DALTON_TRANSCRIPTION_RECONCILE_ENABLED` | `1`（默认开启） | `1` | 5 分钟后台对账循环的独立关闭开关；受 `DALTON_TRANSCRIPTION_ENABLED` 总开关约束 |

---

## 3. 配置来源：`agent_config.yaml` 与环境变量的分工

请对照 `src/utils/storage/__init__.py:36-87` 与 `src/utils/storage/s3_compatible.py:99-137` 注意两类配置的不同读取来源：

1. **由 `_load_storage_setting` 读取的 3 个开关项（`agent_config.yaml` 优先于环境变量）**：
   - `src/utils/storage/__init__.py:36` 中的 `_load_storage_setting(name, env_var, default)` 会先读取 `agent_config.yaml`（及其环境覆盖文件 `agent_config.dalton.yaml`）中的 `storage.<name>`，未设置时才读取环境变量：
     - `storage.provider` → `STORAGE_PROVIDER`（`agent_config.yaml:202-206` 已固定 `provider: "s3"`）
     - `storage.blob_transfer` → `STORAGE_BLOB_TRANSFER`
     - `storage.browser_uploads` → `STORAGE_BROWSER_UPLOADS`
   - 注意：这 3 个开关项在 YAML 中的显式值（包括 `false` / `relay` 这类假值）会直接覆盖 `.env` 中的同名环境变量——`_load_storage_setting` 先读 YAML、未设置时才回退环境变量（`__init__.py:36-48`）。生产不开启 `direct`：`STORAGE_BLOB_TRANSFER` 不设置即保持默认 `auto`（Docker 沙箱解析为 `relay`）；浏览器分片直传是否开启以 `STORAGE_BROWSER_UPLOADS` 为准（见第 2 节——仅与非生产的 `direct` 方案一起设置）。
2. **仅从环境变量读取的 S3 连接与桶参数（不读 YAML）**：
   - `StorageConfig`（`src/utils/storage/s3_compatible.py:99-137`）与 `src/utils/storage/key_prefix.py` **只从环境变量**读取 `STORAGE_BUCKET_NAME`、`STORAGE_ACCESS_KEY_ID`、`STORAGE_SECRET_ACCESS_KEY`、`STORAGE_REGION`、`STORAGE_ENDPOINT_URL`、`STORAGE_BROWSER_ENDPOINT_URL`、`STORAGE_ADDRESSING_STYLE`、`STORAGE_KEY_PREFIX`、`STORAGE_MAX_UPLOAD_SIZE`，在任何 YAML 中配置这些字段均不会生效。

---

## 4. `STORAGE_KEY_PREFIX` 上线后不可修改

如 `.env.example:133` 与 `src/utils/storage/key_prefix.py` 所示：

- 数据库（如 `workspace_file_uploads.object_key`、工作区文件归档、笔记附件等）持久化的是**不带 prefix 的相对 key**（例如 `uploads/<user_id>/<upload_id>`）。
- 每次调用 `head_object`、`sign_get_url`、`delete_object` 时，后端都会通过 `full_key()` 动态拼接当前的 `STORAGE_KEY_PREFIX`。
- **一旦上线产生数据后修改 `STORAGE_KEY_PREFIX`，所有已入库的对象都将因路径不匹配而报 404 Not Found**。上线前确定好值后请勿再改动。

---

## 5. 桶安全、CORS 与生命周期清理（`configure_minio.sh`）

### 桶必须私有 & 严禁默认凭据 & 镜像说明
- 存储桶包含用户的原始录音、工作区文件与内部资料，**严禁设为 `public` 或 `download`（匿名可读）**，必须保持 `mc anonymous set none`（完全私有）。
- `docker-compose.dalton.yml` 中的 `minio` 服务**不提供 `minioadmin` 默认凭据**；启用 `--profile storage` 时必须提供 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`（或 `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY`）及 `DALTON_CORS_ALLOWED_ORIGINS`。
- MinIO 官方已不再发布社区镜像（Docker Hub / Quay 已停更或拒拉），`docker-compose.dalton.yml` 使用 Chainguard 镜像按 digest 固定（`cgr.dev/chainguard/minio@sha256:e70b2b1e74d79457f808710483bd726ff364529ca30736eaa15a6ab547d3a5a7`），离线镜像备份见 `~/Backups/dalton/images/`。

### 启动与一键初始化校验
1. 启动 `minio` 容器（CORS 与未完成分片清理参数在容器启动时通过环境变量生效，无需重启服务）：
   ```bash
   export STORAGE_BUCKET_NAME="langalpha-uploads"
   export STORAGE_ACCESS_KEY_ID="<your-access-key>"
   export STORAGE_SECRET_ACCESS_KEY="<your-secret-key>"
   export DALTON_CORS_ALLOWED_ORIGINS="https://dalton.example.com"
   export DALTON_UPLOADS_EXPIRE_DAYS=2

   docker compose -f docker-compose.yml -f docker-compose.dalton.yml --profile storage up -d minio
   ```
2. 运行幂等配置与校验脚本（需安装 MinIO Client `mc`）：
   ```bash
   export STORAGE_ENDPOINT_URL="http://127.0.0.1:9000"
   bash scripts/dalton/configure_minio.sh
   ```
   该脚本幂等地完成以下配置与严格校验（任一步失败或 `mc` 缺失均以非零状态码退出，绝不静默跳过；且不执行 `mc admin service restart`，避免中断进行中的分片上传）：
   - **创建 Bucket 并强制私有**：执行 `mc mb --ignore-existing` 与 `mc anonymous set none`，并读回校验匿名权限为 `none`、清空桶策略。
   - **校验 CORS 真实生效**：对 `DALTON_CORS_ALLOWED_ORIGINS` 中的每个源发起真实 HTTP `OPTIONS` 预检请求（`Access-Control-Request-Method: PUT`），校验响应头允许该源、允许 `PUT`/`GET`/`HEAD`、未放行未授权源。
   - **配置并读回校验 `uploads/` 过期规则**：通过 S3 Lifecycle 配置 `<STORAGE_KEY_PREFIX>uploads/` 前缀 `Expiration (Days: DALTON_UPLOADS_EXPIRE_DAYS)`（1–2 天自动删除残留在中转前缀下的完整对象），并读回校验规则已启用。
   - **校验未完成分片清理**：MinIO 的 S3 ILM XML 不支持 `AbortIncompleteMultipartUpload` 动作，未完成分片由 MinIO 内置 `api` 子系统的 `stale_uploads_expiry`（默认 `24h`，通过 `MINIO_API_STALE_UPLOADS_EXPIRY` 注入）与 `stale_uploads_cleanup_interval`（默认 `6h`，通过 `MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL` 注入）自动回收；脚本通过 `mc admin config get dalton api` 校验该机制处于激活状态。

---

## 6. 磁盘容量与资源估算

在规划宿主机磁盘与工作区可用空间时，请按**"原音频 3–5 份 + 分段"**估算峰值占用：

1. **流转过程中的多份副本（原音频 3–5 份 + 分段）**：
   - **第 1 份（MinIO 临时对象）**：浏览器分片直传合并前的各分片，以及合并完成至沙箱拉取删除前的完整对象（位于 `dalton-minio-data` 卷）。
   - **第 2 份（沙箱 Staging 临时文件）**：`workspace_upload_pull.py` 拉取期间写入工作区根目录 `.wsfiles-upload-<upload_id>`，校验通过后原子重命名为工作区目标文件。
   - **第 3 份（工作区原音频文件）**：落盘后的原始音频文件（如 `recordings/interview.wav`）。
   - **第 4–5 份及分段文件（后续转写流水线与归档）**：后续录音转文字（A1b）进行切分生成的音频分段（chunks）、重采样中间文件以及沙箱工作区备份归档。
   - **容量公式建议**：若单次长录音为 `500 MB`，单任务峰值磁盘消耗按 `500 MB × (3 ~ 5) ≈ 1.5 GB ~ 2.5 GB` 预留；支持并发上传时按同一电脑上所有上传者的预留总和叠加，并保留至少 `50 MiB`（`CRITICAL_FREE_BYTES`，见 `src/server/services/computer_disk.py:29`）的电脑级安全余量。
2. **内存边界保护**：
   - `backend` 容器内存限制为 `4g`（见 `docker-compose.dalton.yml`）。
   - 当 `direct_available()` 为假（对象存储未启用或沙箱不支持直拉）时，超过内存中转上限（`relay_cap` = `min(RELAY_MAX_BYTES, scan_cap)`：对象存储启用、走中继时为 `RELAY_MAX_BYTES` 256 MiB（`blob_keys.py:39`），对象存储未启用时收紧为 `INLINE_MAX_BYTES` 100 MiB（`blob_keys.py:44`））的大文件会直接返回 `413 Payload Too Large`（提示*"大文件需要对象存储直传，当前未启用"*），**绝不降级为后端整文件读入内存**。
