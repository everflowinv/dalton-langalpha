# 大文件与音频直传存储指南（MinIO / S3 兼容）

本文档说明 Dalton 大文件（含长录音音频）浏览器分片直传的架构、三种网络视角、环境变量与 YAML 配置优先级、Caddy 反代签名约束、MinIO 幂等初始化及磁盘容量估算。

---

## 1. 三种网络视角与 Endpoint 职责

在 `docker-compose.dalton.yml` 中，`minio` 服务仅绑定在宿主机回环地址 `127.0.0.1:9000`。**在 Linux 宿主机上，容器内的 `host.docker.internal` 解析为 Docker 网桥网关 IP（如 `172.17.0.1`），无法连通宿主机 `127.0.0.1:9000`**。因此后端与沙箱必须通过同一个 Docker Compose 网络以服务名 `http://minio:9000` 通信，而浏览器通过 HTTPS 反向代理域名访问（实现见 `src/utils/storage/s3_compatible.py:99-137`）：

| 网络视角 | 使用的环境变量 | 调用阶段与职责 | 必须配置的值 |
|---|---|---|---|
| **后端容器**（`backend`） | `STORAGE_ENDPOINT_URL` | `create_multipart_upload`、`complete_multipart_upload`、`abort_multipart_upload`、`head_object`、`delete_object`，以及生成供沙箱拉取的预签名 GET URL（`sign_get_url`） | `http://minio:9000`（Compose 容器内网直连） |
| **沙箱容器**（Docker Sandbox） | `STORAGE_ENDPOINT_URL`（使用后端签发的 `get_url`） | `workspace_upload_pull.py` 调用 `sandbox.pull_direct(url=get_url, ...)`，在沙箱内流式 `GET` 临时对象到 `.wsfiles-upload-<upload_id>` 并原子重命名到目标路径 | 沙箱必须加入同一 Compose 网络（在 `agent_config.yaml` / `agent_config.dalton.yaml` 中配置 `sandbox.docker.network_mode`），使用同一个 `http://minio:9000` |
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
   - 因为后端 `sign_get_url` 使用 `STORAGE_ENDPOINT_URL=http://minio:9000` 签名，Docker 沙箱容器必须与 `backend`、`minio` 处于同一 Compose 网络，才能解析 `minio` 并通过签名校验。
   - 实际配置字段位于 `agent_config.yaml` / `agent_config.dalton.yaml` 的 `sandbox.docker.network_mode`（见 `src/ptc_agent/config/core.py:461`、`src/ptc_agent/core/sandbox/providers/docker.py:704`；Dalton 默认值为 `"bridge"`，见 `agent_config.yaml:125`；**注意不是 `config.dalton.yaml` 的 `sandbox.docker.network`**）。
   - 在 `agent_config.dalton.yaml`（或 `agent_config.yaml`）中将其设为 Compose 网络名（可通过 `docker network ls` 查看，通常为 `<project>_default`，如 `repo_default`）：
     ```yaml
     sandbox:
       docker:
         network_mode: "repo_default" # 替换为实际的 docker compose 网络名
     ```

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
| `STORAGE_ENDPOINT_URL` | 无（必填） | `http://minio:9000` | 后端与沙箱在 Compose 网络内访问 MinIO 的地址 |
| `STORAGE_BROWSER_ENDPOINT_URL` | 回退到 `STORAGE_ENDPOINT_URL` | `https://storage.example.com` | 浏览器直传 `PUT` 分片的公网 HTTPS 反代地址 |
| `STORAGE_BLOB_TRANSFER` | **`auto`**（`__init__.py:78`） | **`direct`**（必填） | 默认 `auto` 仅对远程沙箱（Daytona/E2B）启用 `direct`，对 Docker 沙箱会回退为 `relay`（`__init__.py:90-106`）；**Docker 沙箱必须显式设为 `direct`** |
| `STORAGE_BROWSER_UPLOADS` | **`false`**（`__init__.py:85`） | **`true`**（必填） | 默认关闭浏览器分片直传；**必须显式设为 `true`** |
| `STORAGE_KEY_PREFIX` | 空字符串 `""` | `dalton/`（或保持空） | 对象键全局前缀；**一旦上线切勿修改**（见第 4 节） |
| `DALTON_CORS_ALLOWED_ORIGINS` | 无（必填） | `https://dalton.example.com` | 逗号分隔的允许浏览器源（禁止 `*`）；在 `docker-compose.dalton.yml` 中注入为 `MINIO_API_CORS_ALLOW_ORIGIN` |
| `DALTON_UPLOADS_EXPIRE_DAYS` | `2` | `2`（仅允许 `1` 或 `2`） | `configure_minio.sh` 为 `<STORAGE_KEY_PREFIX>uploads/` 配置的对象过期天数 |
| `MINIO_API_STALE_UPLOADS_EXPIRY` | `24h` | `24h` | MinIO 自动清理未完成分片上传（Incomplete Multipart Uploads）的过期时间 |
| `MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL` | `6h` | `6h` | MinIO 扫描并清理未完成分片上传的周期 |
| `STORAGE_PUBLIC_URL_BASE` | 无 | **留空 / 不设置** | 桶为私有，所有拉取均走短期预签名 URL |
| `STORAGE_MAX_UPLOAD_SIZE` | `10485760`（代码默认 10 MiB，`.env.example` 30 MiB） | **保持现有值不改** | 仅控制后端内存中转接口与聊天附件上限；工作区直传有独立的 2 GiB 上限（`MAX_DIRECT_UPLOAD_BYTES`），**切勿调大 `STORAGE_MAX_UPLOAD_SIZE`** |

---

## 3. 配置来源：`agent_config.yaml` 与环境变量的分工

请对照 `src/utils/storage/__init__.py:36-87` 与 `src/utils/storage/s3_compatible.py:99-137` 注意两类配置的不同读取来源：

1. **由 `_load_storage_setting` 读取的 3 个开关项（`agent_config.yaml` 优先于环境变量）**：
   - `src/utils/storage/__init__.py:36` 中的 `_load_storage_setting(name, env_var, default)` 会先读取 `agent_config.yaml`（及其环境覆盖文件 `agent_config.dalton.yaml`）中的 `storage.<name>`，未设置时才读取环境变量：
     - `storage.provider` → `STORAGE_PROVIDER`（`agent_config.yaml:202-206` 已固定 `provider: "s3"`）
     - `storage.blob_transfer` → `STORAGE_BLOB_TRANSFER`
     - `storage.browser_uploads` → `STORAGE_BROWSER_UPLOADS`
   - 注意：若在 `agent_config.yaml` 或 `agent_config.dalton.yaml` 中显式写了 `browser_uploads: false` 或 `blob_transfer: relay`，YAML 会直接覆盖 `.env` 中的 `STORAGE_BROWSER_UPLOADS=true` / `STORAGE_BLOB_TRANSFER=direct`（`__init__.py:39-45`）。因此请不要在 YAML 中写死 `false`/`relay`，保持通过环境变量开启。
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
   - 当 `direct_available()` 为假（对象存储未启用或沙箱不支持直拉）时，超过内存中转上限（`relay_cap`，默认 100 MiB）的大文件会直接返回 `413 Payload Too Large`（提示*"大文件需要对象存储直传，当前未启用"*），**绝不降级为后端整文件读入内存**。
