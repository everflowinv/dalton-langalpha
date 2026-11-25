# 外部成员权限统一与收敛设计方案

> **基线快照**：分支 `feat/external-member-perms`（基线提交 `64b2857f9`，已合入独立核心与手机网页第一、二批）

> **文档状态**：第 8 轮终稿（已整合七轮 Codex / Grok 审查意见与全部 13 项定案裁决，第 5 节待决问题为 0 项，基线提交 `64b2857f9`，可直接进入 Batch 1 编码）

> **关联文档**：[standalone-core.md](./standalone-core.md)、[external-onboarding.md](./external-onboarding.md)

---

## 0. 背景、目标与已决事项

### 0.1 背景与目标

手机网页第二批代码审查暴露了三类跨越手机端、桌面端与服务端的权限和视图一致性问题：

1. **身份来源分散**：设置页、命令面板、研究总监共享卡、团队记忆卡、侧边栏、文件面板、分享按钮与发布弹窗各自读取不同接口字段（`/dalton/credentials/status`、`/dalton/workspaces/team`、`/dalton/sharing/user-settings`、`/dalton/user-settings`、`/dalton/channels/bindings`）。核心安装关闭 `colleague_sharing` 时，依赖 `/workspaces/team` 或 `/sharing/user-settings` 的入口会收到 404，导致 `callerTier` 落空。
2. **发布与分享写路径存在缺口**：发布弹窗在打开后身份变化或刷新期间仍可提交；停止分享弹窗（`unshareOnly`）渲染了受众选择器且仅调文件 `unshare`，使公开监控面板继续匿名开放；服务端 `PUT /api/v1/artifacts/{artifact_id}/audience` 缺少外部成员与管理员校验；会话停止分享请求附带 `permissions`；外部成员在会话分享面板与普通发布弹窗仍可见复制链接入口；会话读取、会话状态、会话分享、研究总监共享、团队记忆以及 `require_thread_owner()` / `_dalton_external_outside()` 仍存在仅判断 `is_dalton_supabase_auth_enabled()` 的分支，使核心安装 `local` 模式绕过隔离。
3. **预览框文档路径识别不全**：`src/server/preview_proxy/bridge/core.js:123` 中的 `docPath()` 仅匹配 `/v/<seq>/` 和 `/wsfiles/g/<grant>/`，遗漏分享会话的 `/api/v1/public/shared/<token>/files/serve/`；同时预览框在非桥接跳转、无路径监控根入口、非法/冲突路径、跨标签切换与桌面端工具栏联动场景下存在陈旧文档路径残留。

本方案确立四项落地目标：

- 前端通过单一 Hook `useCallerIdentity()`（与 `useTeamCredentials()` 共用透传 `AbortSignal` 的单一查询函数 `fetchTeamCredentialsStatus({ signal })`）统一向 `/api/v1/dalton/credentials/status` 读取服务端确认的 `caller_tier`、`can_invite` 与 `auth_mode`，在上游平台模式直接放行平台权限，并在打开敏感面板、窗口回前台、收到 403 时通过 `cancelQueries` + `fetchQuery({ staleTime: 0 })` 中止在途 HTTP 请求并强制重验（按 `QueryClient + queryKey` 跨实例共享重验状态，并通过 `getLatestIdentity()` 供所有提交与复制回调同步核对最新确认态、就绪态与角色权限），持续打开期间每 60 秒后台静默保鲜。
- 服务端全部属主、内部成员与管理员专属写接口统一校验多用户身份，仅在 `is_dalton_multiuser_auth_enabled()` 下启用 Dalton 角色守卫，在 Everflow（`supabase`）与核心安装（`local`）下一致执行隔离，以 `{"detail": {"code": "...", "message": "..."}}` 返回 403。
- 完整收敛发布弹窗、停止分享弹窗（在 `is_dalton_enabled()` 下后端按 `share_links → dalton_monitors` 一致锁序同事务原子降级公开监控，`list_shared` 复用 `{_COLUMNS}` 左连接纳入历史公开监控文件，前端 `PublishDialog` 与 `SharedInWorkspace` 文件条目统一走 `POST /api/v1/artifacts/{artifact_id}/unshare`）与会话分享面板（含旧前端过渡兼容）。
- 通过左锚定完整前缀匹配扩展 `docLocation()`（区分 `path` / `root` / `invalid`），移除父端 `page:ready` 的 `lenient(docPath)` 并在原始消息对象上按自有属性存在性前置拒绝冲突/非法字段，在桌面、手机与属主预览帧建立统一文档跟踪状态机。

### 0.2 已决事项（共 13 项，全部定案，开发直接照此执行）

| 编号 | 裁决主题 | 执行口径 |
|---|---|---|
| **已决 1** | 监控面板、独立降级与事务内 `FOR SHARE` 复核 | 外部成员保留 `visibility="own"` 监控面板发布能力。**新发布（`POST /monitors`）、HTTP 手动重新发布、回合后自动更新、代理工具**四条路径执行同一可见性规则。属主降为外部成员后，其已有高于 `own`（`members`/`company`/`public`）的面板：① 已发布版本按原可见性保留访问；② 回合后自动更新暂停并写入 `last_publish_error`；③ `POST /monitors` 与 `POST /republish` 均拒绝在降级前切新版本（即使请求体传 `visibility="own"` 也返回 `403`，要求先走独立降级接口 `PATCH /dalton/monitors/{slug}` 只降可见性、不切新版本）；④ `commit_monitor_publish_tx`（`db.py:697-780`）在锁定 `share_links` 与 `dalton_monitors FOR UPDATE` 后，于同一事务内同时复核**实时角色**（`local` 模式同游标读 **`SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE`**，真正阻塞 `set_user_role` 对非键列 `tier` 的 `FOR NO KEY UPDATE`）、**原可见性 `existing["visibility"]`**（外部成员原非 `own` 直接抛 `403`）与**拟写 `visibility`**；`commit_monitor_version_tx` / `_cut_and_finalize_monitor_in_cur`（`db.py:906-1055`）按同游标 `FOR SHARE` 实时角色复核工作区监控 `live_visibility`，并**保留数据任务 `publish_data_job_monitor`（`service.py:1084-1094`）既有 `_verify_job_public_in_tx` 公开审批校验与 `COMPANY_SYSTEM_USER_ID` 语义不变**；⑤ `get_monitor_by_artifact` 放行属主本人读取自身面板元数据并返回 `can_manage=True`。 |
| **已决 2** | 停止分享 `is_dalton_enabled()` 分支、一致锁序、`SELECT {_COLUMNS}` 历史列表与统一文件撤销入口 | **Batch 1 后端**：`POST /api/v1/artifacts/{artifact_id}/unshare`（`records.py:671`）与 `PATCH /api/v1/workspaces/{id}/share-links/{code}`（`share_links.py:203`）以 `is_dalton_enabled()` 分支——当 `not is_dalton_enabled()` 时直接走上游原 `artifacts_db.unshare()`（零访问 `dalton_monitors`）；当 `is_dalton_enabled()` 为真时共用同一原子事务辅助函数，严格按与监控发布/设置事务（`db.py:547,728,1010`）一致的加锁顺序执行：① 先调用 `artifacts_records_db.lock_owned(cur, user_id=owner_id, artifact_id=artifact_id)` 锁 `share_links` 行；② 再 `SELECT monitor_id::text, visibility FROM dalton_monitors WHERE artifact_id = %s::uuid FOR UPDATE`，若存在且 `visibility == "public"` 则同事务更新 `visibility = 'own', acl_revision = acl_revision + 1, updated_at = NOW()`；③ 最后执行 `artifacts_records_db.unshare_in_cur`（递增 `share_links.revision`），使并发迟到的发布/重发在 `read_basis.check(locked)`（`versions.py:125`）以 `409 SharingChanged` 失败。同时在 `src/server/database/share_links.py:218-227` 的 `list_shared` 中，当 `is_dalton_enabled()` 为真时**直接复用 `SELECT {_COLUMNS}`**（严禁手写列子集，确保 `_link()` 与 `artifact_from_row()` 所需列完整），按 `LEFT JOIN dalton_monitors m ON m.artifact_id = l.artifact_id AND m.visibility = 'public'` 纳入 `shared_at IS NULL` 但 `m.monitor_id IS NOT NULL` 的历史公开监控文件（私人 `app` 链接受 `share_links_app_shape` 约束 `shared_at IS NULL` 且无监控记录，绝不进入该列表；`not is_dalton_enabled()` 保持原 SQL）。**Batch 2 前端**：`PublishDialog`（`unshareOnly`）停止按钮与 `SharedInWorkspace` 所有文件条目（`link.kind === 'file'`，含 `shared_at IS NULL` 的历史公开监控）**唯一调用 `POST /api/v1/artifacts/{artifact_id}/unshare`**（`PATCH /workspaces/{id}/share-links/{code}` 仅用于非文件 `app` 链接），成功后同步失效 `artifacts`、`shareLinks` 与 `daltonMonitors` 三组缓存。 |
| **已决 3** | 文件评论与绑定权限 | 外部成员在 Dalton 多用户模式下禁止新建评论、追加评论消息、编辑评论、绑定线程、解绑线程（服务端均返回 `403 external_member_forbidden`）；保留读评论（`200`）、标记已读（`200`）、删除自己的评论（`204`）。`/a/<code>` 属主页（`OwnerArtifactView`）对外部成员及未知态隐藏 `CommentTools` 并以只读帧渲染。上游平台模式保持原平台权限。 |
| **已决 4** | 身份未知态、跨实例共享 `cancelQueries`（含 `AbortSignal` 中止）+ `fetchQuery` 强制重验、同步 `getLatestIdentity()`、60s 静默保鲜与 `admin→internal` 降级 | 未知态（`loading` / `error` / 强制重验 `isConfirming` 中）停用开启分享、发布/重发、修改受众、修改权限、保存设置与复制链接；当 `is_shared === true` 或已有公开分享时，**「停止分享」在未知态与重验中保持可点**。**共享 `AbortSignal` 查询函数与跨实例共享强制重验**（打开敏感面板、窗口回到前台、收到 `403`）：`useTeamCredentials()`、`useCallerIdentity()` 与 `revalidate()` 统一复用透传 `{ signal }` 的 **`fetchTeamCredentialsStatus({ signal })`**（调用 `api.get('/api/v1/dalton/credentials/status', { signal })`），并以 **`WeakMap<QueryClient, Map<string, RevalidationState>>`** 按 `(queryClient, queryKey)` 在所有挂载实例间共享 `{ activeSeq, isConfirming, listeners }`（经 `useSyncExternalStore` 订阅）：① 同步递增共享 `const seq = ++entry.activeSeq`、置 `entry.isConfirming = true` 并广播；② 先调用 **`await queryClient.cancelQueries({ queryKey })`**（无论哪个 Hook 先发起请求或缓存是否已有数据，均取消在途请求并通过 `signal` 真实中止底层 HTTP 请求），再调用 **`await queryClient.fetchQuery({ queryKey, queryFn: fetchTeamCredentialsStatus, staleTime: 0 })`** 发出全新网络请求；③ `finally` 中仅当 `entry.activeSeq === seq` 时才置 `entry.isConfirming = false` 并广播。**同步最新身份快照读取 `getLatestIdentity()`**：同步读取 `queryClient.getQueryState(queryKey)` 最新缓存与共享 `entry.isConfirming`，实时计算最新 `{ status, tier, isConfirming, isAdmin, isInternal, isExternal, canActAsOwner, canPublishPublic, canInvite }`（`isConfirmingNow()` 为 `getLatestIdentity().isConfirming` 别名）；**所有提交、保存与复制回调首行必须先调 `const latest = identity.getLatestIdentity()`，同步同时核对 `!latest.isConfirming && latest.status === 'ready'` 及对应动作所需的最新角色权限谓词（如 `latest.canActAsOwner` / `!latest.isExternal` / `latest.canPublishPublic`），绝不依赖旧渲染闭包捕获的角色标志**。**60 秒持续打开保鲜**（`refetchInterval: 60_000, refetchIntervalInBackground: false`）：作为**后台静默刷新，不修改共享 `entry.isConfirming`**，响应返回角色降级或报错立即生效。从 `admin` 降为 `internal` 时清空已选 `public` 并阻断回弹至 `existingMonitor?.visibility === 'public'`，要求用户显式重选后才允许提交。 |
| **已决 5** | 403 响应结构、前端解析与既有状态码 | 线上完整 JSON 为 `{"detail": {"code": "...", "message": "..."}}`（沿用 FastAPI `HTTPException.detail` 包装），前端统一用 `apiErrorCoded()` / `formatApiErrorDetail()` 展示错误并在 `403` 时触发强制身份重验（覆盖 `PublishDialog`、`ThreadSharePanel` 与 `WorkspaceSharingModal`）。保持 `401`、隐藏资源 `404`、能力关闭 `404` 及既有 `detail.type` 字段原样。 |
| **已决 6** | 工作区设置分模式两阶段校验、属主 `unknown` 整体 403 与 404 隔离 | 严格按 `router.py:461-464` 两阶段顺序执行：① **阶段一（调用者成员资格校验失败）**：`local` 模式下 `is_admin_user` 只查 `dalton_user_roles`（不认配置默认 `pm_user_ids`），缺行或 `is_team_member_user` 查库异常在 `require_team_reader(uid)` 统一返回 **`403`**；`supabase` 模式下非 `is_pm` 调用者非成员返回 **`403`**，无 `_verified_token_identity` 时 `user_db.get_user(uid)` 查库异常返回 **`503 Team reader verification unavailable`**（`sharing.py:905-917`），GET 与 PUT 均在阶段一终止；② **阶段二（通过阶段一成员资格校验后，`classify_owner_tier(owner_id)` 单独失败返回 `"unknown"`）**：非属主访问在 GET/PUT 统一返回 `404`；**属主本人访问自己工作区时，GET 返回 `200` 且 `can_edit: false`（全部子项编辑标识均为 `false`），`PUT /workspaces/{id}/sharing`（无论提交 `cos_access` 还是仅提交 `web_access`）与 `PUT /workspaces/{id}/settings` 均整体返回 `403 external_member_forbidden` 且不改写数据库**；③ 非属主外部成员及外部 Home 在 GET/PUT 统一返回 `404`；外部属主访问自己非 Home 工作区时 GET 返回分字段编辑标识（`can_edit_web_access: true`、`can_edit_cos_access: false`、`can_edit_team_memory: false`），前端仅开放 `web_access`。 |
| **已决 7** | 会话停止分享校验顺序、改造 `_guard_thread_not_foreign_home_stand_in`、重写 `:1392-1412` 早退分支、保留 `:1477-1571` 写保护与双表准入 | `POST /threads/{id}/share` 在资源鉴权之后、调用 `require_members_audience()`（`:197-199`）之前先确定 `effective_req_permissions`：当 `is_dalton_multiuser_auth_enabled()` 且调用者为外部成员且 `request.is_shared is False` 时置 `effective_req_permissions = None`，跳过受众前置校验与权限合并，仅关 `is_shared` 返回 `200`。将会话读取与状态（`crud.py:317`、`messaging.py:1453,1613`、`src/dalton/sharing.py:require_thread_access`）、归属校验（`api.py:487`、`access.py:302`）与会话分享（`threads/sharing.py:61,88,160,204,272`）共 **10 处**全部切至 `is_dalton_multiuser_auth_enabled()`。**同步改造 `_guard_thread_not_foreign_home_stand_in`（`src/dalton/sharing.py:1043-1052`）**：对他人 Home 替身维持抛 **`404 Thread not found`**；对他人个人 Home 先在多用户模式下调 `classify_owner_tier(ws_owner_for_check, conn=conn)`——外部或 `unknown` 属主个人 Home 抛 **`404 Thread not found`**（与 `require_workspace_access:1280-1290` 及 `router.py:485-488` 一致），内部/管理员属主个人 Home 抛 **`403 Forbidden`**（与 `require_workspace_access:1304-1309` 及 `api.py:525` 一致）。从 `require_thread_access` 顶部起重写前两步并调整 `:1451` 发起人 `read`/`web` 快捷路径，**同时原样保留 `:1477-1571` 全部既有逻辑（含 `:1533-1545` 工作区属主对同事发起会话 `need=="write"` 抛 `403` 与 `:1565-1569` 非会话创建者 `need=="write"` 抛 `403`）**，使表 A 与表 B 在 `supabase` 和 `local` 下完全一致。 |
| **已决 8** | 上游平台兼容、单人 `oss` 贯通与漏配 `VITE_DALTON_AUTH` 边界 | **上游平台模式**（`!isDaltonBuild \|\| isPlatformMode`）：`useCallerIdentity()` 跳过 `/credentials/status`，直接返回 `ready` 且 `isPlatform: true, isExternal: false, canActAsOwner: true, canPublishPublic: true, canInvite: false`；后端 Dalton 角色守卫仅在 `is_dalton_multiuser_auth_enabled()` 为真时启用。**Dalton 单人 `oss`**：后端 `/credentials/status` 新增 `auth_mode: get_dalton_auth_mode()`；单人 `oss`（`is_dalton_single_user_oss_mode()`）下 `get_user_tier()` 与 `classify_owner_tier()` 均短路返回 `"admin"`，`/credentials/status` 返回 `caller_tier: "admin", can_invite: false, auth_mode: "oss"`（邀请写接口返回 `403 invites_disabled_in_oss`）。**漏配 `VITE_DALTON_AUTH` 边界**：`AuthContext.tsx` 与 `authToken.ts` 的运行时认证切换不在本 PR 范围内；当 Dalton 构建漏配 `VITE_DALTON_AUTH` 却指向 `core/local` 后端时，`useCallerIdentity()` 不凭构建变量盲判管理员，照常请求 `/credentials/status`，因无令牌收到 `401` 后落入 **`status: 'error', tier: 'unknown'`** 并停用全部特权操作（fail-closed）；仅当服务端返回 `200` 且 `auth_mode: "oss"` 时才赋予单人管理员资格。 |
| **已决 9** | 复制链接限制覆盖普通与停止弹窗 | 本轮在前端对外部成员及未知态隐藏/停用全部复制入口与分享地址框（覆盖 `ThreadSharePanel`、`SharedInWorkspace`、`PublishDialog` 普通态与 `unshareOnly` 态的链接框、`copyCurrentLink` 回调及底栏监控外链）；后端查询接口仍返回链接标识，脱敏下发记入第 6 节后续事项。 |
| **已决 10** | 预览桥接三态 `docLocation()`、原始属性存在性互斥校验与双端联动 | 桥端 `core.js` 将 `docPath()` 扩展为 `docLocation()`，显式区分带路径文档（`{ path }`）、左锚定无路径版本根入口（`{ root: true }`，如 `MonitorViewer.tsx:147`）与非法/未识别路径（`{ invalidPath: true }`）。父端 `pageProtocol.ts:407` **移除 `page:ready` 上的 `lenient(docPath)`，并在 Zod 剥离可选键之前对原始消息对象 `raw` 用 `Object.prototype.hasOwnProperty.call(raw, key)` 检查 `hasPath`/`hasRoot`/`hasInvalidPath`**（显式拦截 **`{ path: undefined, root: true }`**、`{ path: null, root: true }`、`{ path: 'a.html', root: true }`、`{ path: undefined }` 等全部冲突或非法字段为 `pathState: 'invalid'`），仅当 `!hasPath && !hasInvalidPath && raw.root === true` 时得 `pathState: 'root'` 进入 `bridged_root`；`invalid` 与 `unrecognized` 一律清零旧 `bridge.path` 并设为 **`{ kind: 'unbridged', currentUrl: null }`**（双端隐藏原文件动作并隐藏「在新标签打开」）。`FileViewer`、`HtmlViewer` 与属主 `ArtifactFrames`（实时帧与版本帧）全部经 `onShownDocChange` 上报。 |
| **已决 11** | 精确能力注册表、邀请/渠道与检查门禁 | 与主分支 #504 对齐：`src/dalton/capabilities.py` 中 `get_workspace_settings_endpoint`、`update_workspace_settings_endpoint` 与只读共享接口（`list_team_workspaces_endpoint`、`get_workspace_sharing_endpoint`、`get_user_sharing_settings_endpoint`）为 `CORE_CAPABILITY`；`update_workspace_sharing_endpoint`、`update_user_sharing_settings_endpoint` 保持 `CapabilityId.COLLEAGUE_SHARING`；`get_user_settings_endpoint`、`update_user_settings_endpoint` 为 `CapabilityId.CONVERSATION_ARCHIVE`。工作区设置的外部成员与成员资格两阶段校验在接口函数体中执行。邀请、Discord、微信管理统一 403 结构；组织库限于上游平台模式；`SettingsRail` 门控 `/sharing/user-settings`；新增 Dalton 前端与测试文件登记 `scripts/dalton/dalton_paths.txt`，每批执行 `check_hooks.sh --strict --audit`。 |
| **已决 12** | `PUT /artifacts/{id}/kept` 校验顺序与 `unknown` 细分 | 区分 `kept`（属主版本固定/个人仪表盘，访客仍看 `shared_seq`）与 `keep_updated`（访客自动更新）：先过 `reached()`；`kept=False` 跳过成员等级守卫直接返回 `200` 允许清理；`kept=True` **先调用 `require_internal_member(x_user_id)`**（外部成员无论私有或已分享一律返回 `403 external_member_forbidden`），**再**在 `artifact.shared` 为真时调用 `require_admin_member(x_user_id)`（内部成员返回 `403 admin_required`）。前端固定按钮位于 `useFileComments.tsx:148`（`FilePanel.tsx:371` 控制评论工具可用性）。 |
| **已决 13** | `WorkspaceSharingModal` 能力彻底解耦 | 桌面与手机入口（`ChatView.tsx:1906-1934`、`ThreadGallery.tsx:797-815`）只要开启 `colleague_sharing` 或 `conversation_archive` 任一能力即允许打开弹窗。弹窗内共享区与团队记忆区各自独立管理查询、加载/错误态与编辑权限；关闭 `colleague_sharing` 时跳过 `/sharing` 请求且跳过渲染研究总监控件（摒弃 `cos_access ?? 'ro'` 兜底）；`handleSave()` 仅提交已修改且有权限保存的区块。 |

---

## 1. 现状清单（代码核实）

### 1.1 前端身份读取与权限判断现状

| 入口模块 | 代码位置（`文件:行号`） | 当前读取字段与来源接口 | 当前判断逻辑与存在问题 |
|---|---|---|---|
| 设置页可见性上下文 | `web/src/pages/Settings/components/SettingsRail.tsx:29-78` | 主读 `/api/v1/dalton/credentials/status`（`caller_tier`、`is_pm`、`can_invite`）；失败或为空时回退读 `/api/v1/dalton/channels/bindings`（`user_tier`）；另对所有 `isDaltonAuth` 用户无条件请求 `/api/v1/dalton/sharing/user-settings`（`:33`） | ① 混用两个接口作为身份源；② 通道能力关闭时回退失效；③ 核心安装或外部成员访问设置页时仍发出 `/sharing/user-settings` 请求 |
| 设置页分区与深链守卫 | `web/src/pages/Settings/sections.ts:37-46,110-162`<br>`web/src/pages/Settings/Settings.tsx:103-158` | 消费 `useDaltonSettingsVisibility()` 返回的 `callerTier`、`callerTierPending`、`discordChannelsAdmin` | 依赖 `SettingsRail` 混合上下文，需统一切换到单一身份源 |
| 设置页团队运维与 Discord 卡 | `web/src/pages/Settings/panels/dalton/DaltonTeamOpsPanel.tsx:22-42`<br>`web/src/pages/Settings/panels/dalton/DaltonDiscordChannelsCard.tsx:195-203` | 混读 `/api/v1/dalton/credentials/status`（`caller_tier`、`is_pm`）与 `/api/v1/dalton/channels/bindings`（`user_tier`） | 卡片内部再次解析 `bindingsQuery.data.user_tier`，与顶层身份判断重复 |
| 研究总监共享卡 | `web/src/pages/Settings/panels/dalton/DaltonCosSharingCard.tsx:69-76,89-107` | 外部可见性读 `ctx.callerTier`；卡片内部读 `/api/v1/dalton/sharing/user-settings`（`caller_tier`、`can_edit`、`colleague_dispatch_enabled`） | 脱离 `ctx` 挂载时直接请求 `/sharing/user-settings`，并用其响应里的 `caller_tier` 二次裁决 |
| Home 团队记忆卡 | `web/src/pages/Settings/panels/dalton/DaltonHomeTeamMemoryCard.tsx:18-27,68-101` | 外部可见性读 `ctx.callerTier`；卡片内部读 `/api/v1/dalton/user-settings`（`caller_tier`、`can_edit`） | 脱离 `ctx` 挂载时直接请求 `/user-settings`，并用其响应里的 `caller_tier` 二次裁决 |
| 命令面板（工作区权限与设置搜索） | `web/src/components/CommandPalette/useVerbs.ts:39-83`<br>`web/src/components/CommandPalette/useLocalSources.ts:48-51` | `usePaletteAccess()` 读 `/api/v1/dalton/workspaces/team`（`caller_tier`）；`useLocalSources()` 同时调用 `useDaltonSettingsVisibility()` 读 `/credentials/status` 与 `/channels/bindings` | 同一面板两套身份来源并存；核心安装关闭 `colleague_sharing` 时 `/workspaces/team` 返回 404，`callerTier` 落空为 `null` |
| 侧边栏团队区与只读态 Hook | `web/src/pages/ChatAgent/components/dalton/TeamSharing.tsx:89-106,215-229,267-292` | `useTeamReadOnly()` 读 `/api/v1/dalton/workspaces/team`（`caller_tier`），缓存兜底读 `/api/v1/dalton/sharing/user-settings`（`caller_tier`） | 工作区属主确认后若 `colleague_sharing` 关闭，两个接口均 404 使 `callerTier` 恒为 `null`，下游按非外部成员放行 |
| 侧边栏导航树与会话右键菜单 | `web/src/pages/ChatAgent/components/NavigationPanel.tsx:180-185`<br>`web/src/pages/ChatAgent/components/NavigationRows.tsx:357,438,522,528-550`<br>`web/src/pages/ChatAgent/components/ThreadGallery.tsx:140,290,797-815,989,1086` | 各自调用 `useTeamWorkspaces()` 读 `/api/v1/dalton/workspaces/team`（`caller_tier`）及其缓存；`ThreadGallery.tsx:804` 移动端仅凭 `colleagueSharingReady` 挂载 `WorkspaceSharingModal` | `canShareThread` 在 `callerTier === null` 时返回 `true`（`:522`）；多处各自发起 `/workspaces/team` 请求；手机端仅开 `conversation_archive` 时缺失工作区团队记忆入口 |
| 对话顶栏、消息卡片与行情对话 | `web/src/pages/ChatAgent/components/ChatView.tsx:329,1449-1452,1906-1942`<br>`web/src/pages/ChatAgent/components/chatView/usePanelFiles.ts:66`<br>`web/src/pages/MarketView/components/MarketChatPanel.tsx:775-776` | 读 `teamReadOnly.callerTier`（源头 `/workspaces/team`）；`ChatView.tsx:1914` 手机端仅凭 `colleagueSharingReady` 挂载 `WorkspaceSharingModal` | 以 `callerTier !== 'external'` 放行消息卡片发布与文件面板分享，身份加载中、失败或核心安装 404 时按内部属主放行；手机端仅开 `conversation_archive` 时缺少弹窗入口 |
| 文件面板、固定按钮与文件头操作栏 | `web/src/pages/ChatAgent/components/FilePanel.tsx:340-354,371,1002`<br>`web/src/pages/ChatAgent/components/filePanel/useFileComments.tsx:148`<br>`web/src/pages/ChatAgent/components/FileHeaderActions.tsx:171-177,236-248,467-481,528-604,1128-1162` | 读父级传入的 `callerTier`（源头 `/workspaces/team`）；`FilePanel.tsx:371` 控制评论工具可用性，`PinPageButton` 实际渲染在 `useFileComments.tsx:148` | `access` 仅在 `callerTier === 'external'` 时降为 `readOnly`，身份未知时按 `owner` 处理；`FileHeaderActions.tsx:467-478` 仅凭 `unshareRecord?.shared` 显示停止入口，遗漏“文件链接已关、监控仍公开”的历史态；桌面端工具栏（`:528-604`）仍对 `selectedFile` 操作 |
| 发布 / 停止分享弹窗 | `web/src/pages/ChatAgent/components/PublishDialog.tsx:451-486,610-675,769-818,867-895,1084-1092,1214-1259` | 读 `/api/v1/dalton/credentials/status`（经 `useTeamCredentials`），仅取 `callerTier`，5 分钟缓存期间无重验；`:460` 用 `callerTier === 'admin' \|\| !isDaltonAuth` 判定管理员 | ① 身份加载中、出错或打开后降级仍可提交（含公开确认回调 `:1251-1259`）；② `admin→internal` 降级把 `public` 静默映射为 `company`（`:613-615`），且 `:610` 的 `visibilityDraft ?? existingMonitor?.visibility` 会回弹选中 `public`；③ `unshareOnly` 的 `stop()`（`:769-773`）只调文件 `unshare`（服务端尚未联动降级公开监控），若前端改为调监控 `PATCH` 又会在管理员接管面板后对原文件属主报 `403`（`permissions.py:43-59`）；④ 普通态与 `unshareOnly` 态的链接框、复制回调（`:801`）、`AudiencePicker`（`:1084-1092`）、`LibraryPublishRow`（`:1214-1220`）与底栏监控外链（`:1224-1234`）均未按已确认非外部身份收口 |
| 独立文件属主页（`/a/<code>`） | `web/src/pages/SharedChat/OwnerArtifactView.tsx:87,133,138,173,181-194` | 读 `useTeamReadOnly(workspaceId).callerTier`（源头 `/workspaces/team`） | ① `callerTier !== 'external'` 放行发布入口；② 无条件渲染 `CommentTools`（`:133`）与属主 `ArtifactFrames`（`:183-194`），外部成员仍可操作评论 UI；③ `<PublishDialog>` 无条件挂载 |
| 会话分享按钮与面板 | `web/src/pages/ChatAgent/components/ShareButton.tsx:35-47`<br>`web/src/pages/ChatAgent/components/ThreadSharePanel.tsx:28-95,123-128,144-172,240-349` | `ShareButton` 优先读 `callerTierProp`，再静态读 `/workspaces/team` 或 `/sharing/user-settings` 缓存；`ThreadSharePanel` 仅静态读缓存；`SharedInWorkspace`（`:44-46`）对文件调 `unshare.mutate(link.artifact_id)`，非文件调 `patch.mutate` | ① 静态读缓存无法响应后到与变更；② `handleToggleShare(false)` 停止分享仍提交完整 `permissions`（`:154-158`）；③ 外部成员可见分享地址与复制按钮（`:272-294`、`SharedInWorkspace:71-80`）、`AudiencePicker`（`:297`）、文件权限复选框（`:301-331`）及 `LibraryPublishRow`（`:340-344`）；④ `save()` 仅 `console.error`（`:148-150`），未展示服务端 403 原因；`unshare` 成功后尚未同步失效 `daltonMonitors` 缓存 |
| 工作区共享与团队记忆弹窗 | `web/src/pages/ChatAgent/components/dalton/WorkspaceSharingModal.tsx:124-223` | 读 `/dalton/workspaces/{id}/sharing`（`owner_external`、`can_edit`）与 `/dalton/workspaces/{id}/settings`（`can_edit`） | ① `useWorkspaceSharing` 返回 404 时整个弹窗返回 `null`（`:138`），缺数据时用 `cos_access ?? 'ro'` 渲染研究总监控件（`:142`）；② `handleSave()`（`:178-179`）先受共享 `canEdit` 门控；③ 保存失败（`:215-220`）只展示通用文案，未提取 403 原因或重验身份 |
| `useTeamCredentials` 与其余已接入的页面/卡片 | `web/src/hooks/useTeamCredentials.ts:46-56`、`Automations.tsx:196,271`、`DaltonInvitesCard.tsx:62-65`、`FeishuReadScopesCard.tsx:47-55`、`DaltonBudgetCard.tsx:46-47`、`DaltonSystemJobsCard.tsx:61-63`、`DaltonWeixinBotCard.tsx:42-43`、`NativeModelsCard.tsx:31-32`、`ProviderStatusCard.tsx:19-21` | 读 `/api/v1/dalton/credentials/status`（`caller_tier`、`is_pm`、`can_invite`）；`useTeamCredentials.ts:48-52` 的 `queryFn` 未接收 `{ signal }` 也未向 `api.get` 传递 `{ signal }` | 数据源正确；需抽出透传 `{ signal }` 的共享 `fetchTeamCredentialsStatus({ signal })` 供 `useTeamCredentials` 与 `useCallerIdentity` 共同复用，并将成员等级判断统一迁至 `useCallerIdentity()` |

### 1.2 后端写接口与状态接口权限校验现状

| 接口路径 | 代码位置（`文件:行号`） | 当前身份校验规则 | 存在的缺口 |
|---|---|---|---|
| `GET /api/v1/dalton/credentials/status` | `src/dalton/app/router.py:1088-1117`<br>`src/dalton/invites.py:218-226`<br>`src/dalton/access_tiers.py:395-521`<br>`src/dalton/credentials.py:291-304` | 经 `invites.get_user_tier` 转调 `access_tiers.get_user_tier`，并调 `is_credential_admin` | ① 响应缺少服务端确认的 `auth_mode` 字段；② 单人 `oss` 下 `get_user_tier()` 仍查名单并返回 `"external"`、`classify_owner_tier()` 缺少单人短路，同时 `is_credential_admin` 在共享关闭时返回 `True`，字段自相矛盾 |
| `PUT /api/v1/artifacts/{artifact_id}/audience` | `src/server/app/artifacts/records.py:152-178,632-668`<br>`src/server/services/share_audience.py:91-126`<br>`src/server/services/features.py:201-212`<br>`src/server/services/membership.py:40-44` | 仅校验 `reached()` 与 `refuse_link_change()`，再调 `require_members_audience`（`features.py:211` 调 `membership.teams_door_open()`）或 `refuse_public_link_of` | 缺少发布总开关校验、外部成员校验与公开受众管理员校验；外部成员可直接修改受众 |
| `POST /api/v1/artifacts/{artifact_id}/publish` | `src/server/app/artifacts/records.py:232-275,554-629` | 顺序执行 `_refuse_publishing_disabled()`（`:573`）→ `_require_non_external_for_publish`（`:575`）→ `_require_admin_for_public_publish`（`:577`）→ `reached()`（`:578`） | 外部成员与管理员拒绝响应 `detail` 为纯字符串，缺少统一 `code`（低等级调用者请求他人文件在 `:575-577` 先返回 `403`，管理员请求他人文件在 `:578` 返回 `404`） |
| `PATCH /api/v1/artifacts/{artifact_id}`（`keep_updated`） | `src/server/app/artifacts/records.py:329-353` | `keep_updated=True` 校验发布开关与非外部成员（已公开时校验管理员）；`False` 允许关闭清理 | 拒绝响应 `detail` 缺少统一 `code` |
| `PUT /api/v1/artifacts/{artifact_id}/kept` | `src/server/app/artifacts/records.py:356-387` | 校验 `reached()`；`kept=True and artifact.shared` 校验管理员；私有文件 `kept=True` 未限外部成员 | 私有文件 `kept=True` 与前端隐藏 `PinPageButton`（`useFileComments.tsx:148`）存在口径差；已分享文件 `kept=True` 需先验 `require_internal_member` 再验 `require_admin_member` |
| `POST /api/v1/artifacts/{artifact_id}/left-out` | `src/server/app/artifacts/records.py:390-413` | 校验 `_refuse_publishing_disabled()` 与 `_require_non_external_for_publish` | 拒绝响应 `detail` 缺少统一 `code` |
| `POST /api/v1/artifacts/{artifact_id}/unshare`<br>`PATCH /api/v1/workspaces/{id}/share-links/{code}`<br>`GET /api/v1/workspaces/{id}/share-links`<br>`GET /api/v1/share-links/{code}` | `src/server/app/artifacts/records.py:671-684`<br>`src/server/app/share_links.py:195-256`<br>`src/server/database/artifacts/records.py:113-154,271,462-473`<br>`src/server/database/share_links.py:34-37,82-109,218-227`<br>`migrations/versions/049_share_links.py:29-47`<br>`src/dalton/migrations/versions/d038_monitors.py:24-46` | 停止接口仅调 `artifacts_db.unshare()` 清 `share_links.shared_at`；列表查询 `list_shared`（`database/share_links.py:218-227`）使用 `SELECT {_COLUMNS} FROM share_links l WHERE l.workspace_id = %s AND l.shared_at IS NOT NULL`（`_COLUMNS` 含 `ARTIFACT_COLUMNS` 与 `app_command` 子查询；`share_links_app_shape` 约束强制 `kind = 'app'` 时 `shared_at IS NULL`，`app` 链接仅通过单条查询 `GET /api/v1/share-links/{code}` `:239` 解析） | ① 停止接口未在 `is_dalton_enabled()` 下同事务降级关联的公开监控面板；② `list_shared` 过滤掉 `shared_at IS NULL` 但 `dalton_monitors.visibility = 'public'` 的历史文件记录，扩展时须直接复用 `SELECT {_COLUMNS}` 并以 `is_dalton_enabled()` 保护未跑 Dalton 迁移链的上游部署 |
| `PUT/DELETE /api/v1/artifacts/{artifact_id}/binding`<br>`POST/PATCH/DELETE /api/v1/artifacts/{artifact_id}/comments*` | `src/server/app/artifacts/records.py:419-448`<br>`src/server/app/artifacts/comments.py:110-289` | 写评论与绑定校验发布总开关 + `reached()`；解绑、已读、删除仅校验 `reached()`；创建/追加评论成功返回 `201`（`:143,172`），删除返回 `204`（`:276`） | 未在 Dalton 多用户模式下对新建评论、追加消息、编辑评论、绑定与解绑校验内部成员身份 |
| `GET /api/v1/dalton/monitors/by-artifact/{id}`<br>`POST /api/v1/dalton/monitors`<br>`POST /api/v1/dalton/monitors/{slug}/republish`<br>回合后自动更新 & 数据任务发布 | `src/dalton/monitors/router.py:101,299,309,338`<br>`src/dalton/monitors/permissions.py:43-152`<br>`src/dalton/monitors/service.py:382-391,411-535,631-699,740-839,1084-1094`<br>`src/dalton/monitors/db.py:547-694,697-780,906-1055`<br>`src/dalton/auth/roles.py:149-180`<br>`src/dalton/migrations/versions/d047_local_auth_and_roles.py:49-55`<br>`src/dalton/tools/monitor_tools.py:653-695` | 代理工具 `monitor_tools.py:665-670` 已在调用服务前拦截 `public` 与非公司成员的非 `own` 重发；但 `service.py:411`（`POST /monitors`）与写入事务 `commit_monitor_publish_tx`（`db.py:697-780`）仅按请求 `visibility` 校验且事务内 `:757` 仅复核 `public`；`service.py:649`、`service.py:796-798` 与 `commit_monitor_version_tx` / `_cut_and_finalize_monitor_in_cur`（`db.py:926-931`）仅对 `public` 复核管理员；`dalton_user_roles.tier` 为非键列（`set_user_role` 将 `internal` 降为 `external` 时取 `FOR NO KEY UPDATE`，需 `FOR SHARE` 方可阻塞）；`get_monitor_by_artifact`（`service.py:388`）调 `require_monitor_view` 拒绝已降为外部成员的 `company` 面板属主 | ① `POST /monitors` 在外部属主（或读快照期间降为外部成员的属主）传 `visibility="own"` 时可一步降级并切新版本；② HTTP 手动重发、回合后自动更新与写入事务放行外部属主的 `members`/`company` 面板；③ 降级后的外部属主无法经 `by-artifact` 查到自己的 `company` 面板以触发降级 |
| `GET /api/v1/threads/{thread_id}`<br>会话消息回放 `messaging.py:1453`<br>会话运行状态 `GET /threads/{id}/status` `messaging.py:1613`<br>`POST/GET /api/v1/threads/{thread_id}/share`<br>`_guard_thread_not_foreign_home_stand_in()`<br>`require_thread_access()`<br>`require_thread_owner()`<br>`_dalton_external_outside()` | `src/server/app/threads/crud.py:309-335`<br>`src/server/app/threads/messaging.py:1449-1470,1609-1627`<br>`src/server/app/threads/sharing.py:48-95,153-289`<br>`src/dalton/sharing.py:1043-1052,1250-1310,1360-1571`<br>`src/server/utils/api.py:473-525`<br>`src/server/services/shared_workspaces/access.py:294-377` | `crud.py:317`、`messaging.py:1453,1613`、`threads/sharing.py:61,88,160,204,272`、`api.py:487`、`access.py:302` 共 **10 处**仅判断 `is_dalton_supabase_auth_enabled()`；`src/dalton/sharing.py:1392-1412` 在 `not _sharing_auth_enabled()` 时于 `:1430` 之前提前返回；`_guard_thread_not_foreign_home_stand_in`（`:1043-1052`）对他人 Home 替身和个人 Home 一律抛 `404`（未区分外部/`unknown` 属主个人 Home `404` 与内部/管理员属主个人 Home `403`）；`threads/sharing.py:191-201` 在合并前校验 `require_members_audience()` | ① 核心安装 `local` 模式下 10 处分支全部失效（含 `GET /threads/{id}/status`）；② `require_thread_access` 的 `:1392-1412` 早退分支在 `colleague_sharing=off` 时仍放行内部成员读取本人历史跨区会话（`200`），且 `_guard_thread_not_foreign_home_stand_in` 把内部/管理员属主的个人 Home 也抛成 `404`；③ 外部成员以旧前端载荷（含 `audience: "organization"`）调 `is_shared: false` 会在 `threads/sharing.py:197-199` 撞上受众校验 |
| `GET/PUT /api/v1/dalton/sharing/user-settings`（研究总监共享） | `src/dalton/app/router.py:924-1009`<br>`src/dalton/capabilities.py:871-872` | 依赖 `CapabilityId.COLLEAGUE_SHARING`（注册表 `:871-872` 保持 `COLLEAGUE_SHARING`）；内部 `:944,990` 仅判断 `is_dalton_supabase_auth_enabled()` | 核心安装开启 `colleague_sharing` + `local` 模式时跳过校验并硬编码 `caller_tier="internal"` |
| `GET/PUT /api/v1/dalton/user-settings`（Home 团队记忆） | `src/dalton/app/router.py:1012-1085`<br>`src/dalton/capabilities.py:873-874` | `:1028,1067` 仅判断 `is_dalton_supabase_auth_enabled()`；能力注册表 `:873-874` 标为 `CORE_CAPABILITY` | ① 核心安装 `local` 模式下放行外部成员写操作；② 缺少 `CapabilityId.CONVERSATION_ARCHIVE` 路由守卫与注册表声明 |
| `GET/PUT /api/v1/dalton/workspaces/{workspace_id}/sharing`<br>`GET/PUT /api/v1/dalton/workspaces/{workspace_id}/settings` | `src/dalton/app/router.py:433-921`<br>`src/dalton/access_tiers.py:165-196,337-360,464-521`<br>`src/dalton/sharing.py:158-182,866-929,1257-1290`<br>`src/dalton/capabilities.py:869-870` | 路由在 `:461-464` 先执行 `is_pm = await is_admin_user(uid, include_config_defaults=True)`（`local` 下只查 `dalton_user_roles`，`supabase` 下查 `pm_user_ids`）并对 `not is_pm` 执行 `require_team_reader(uid)`（`local` 缺行/查库异常抛 `403`；`supabase` 非成员抛 `403`、`user_db.get_user` 异常抛 `503`），随后在 `:507-514,639-648,775-782` 调 `classify_owner_tier(owner_id)` 且仅判 `owner_external = (effective_owner_tier == "external")`；`/workspaces/{id}/settings` 在注册表 `:869-870` 标为 `CORE_CAPABILITY` | ① 已通过阶段一成员校验后若阶段二 `classify_owner_tier(owner_id) == "unknown"`，GET 返回 `can_edit: true` 且 PUT 放行写入（含仅传 `web_access`）；② 缺少外部属主分字段可编辑标识（`can_edit_web_access` 等）；③ `/workspaces/{id}/settings` 缺少 `CONVERSATION_ARCHIVE` 守卫与注册表声明 |
| `POST/DELETE /api/v1/dalton/invites*` | `src/dalton/app/router.py:2362-2401`<br>`src/dalton/invites.py:229-246,467-530` | `require_pm_for_invites` 校验 `admin`/`internal`；`local` 下管理员可邀三档、内部仅邀 `external`（限 20）；`supabase` 下固定 `external` | 403 缺少统一 `code`；`oss` 单人模式下落入 `_require_invite_supabase_config()` 报 503 而非明确 403 拒绝 |
| `POST /api/v1/dalton/channels/discord-channels`<br>微信机器人管理接口 | `src/dalton/app/channels_router.py:977-1039` | 实时调 `get_user_tier(user_id) != "admin"` 返回 403 | 403 缺少统一 `code: "admin_required"` |
| `POST /api/v1/org-library`（组织库发布） | `src/server/app/org_library.py:271-295`<br>`src/server/services/platform_client.py:59-61` | 走平台组织策略（`HOST_MODE != "oss"` 且配 `AUTH_SERVICE_URL`） | 仅适用于上游平台模式，Everflow 与核心安装均运行在 `HOST_MODE=oss` 下（受限面板保持隐藏） |

### 1.3 HTML 预览桥接与框内跳转文档识别现状

| 环节 | 代码位置（`文件:行号`） | 现状问题 |
|---|---|---|
| 桥接脚本路径提取 `docPath()` 与 `hello()` | `src/server/preview_proxy/bridge/core.js:24-35,123-135` | ① `DOC_PATH = /\/(?:v\/[0-9]+|wsfiles\/g\/[^\/]+)\/(.+)$/` 遗漏 `/api/v1/public/shared/<token>/files/serve/` 且未从行首左锚定完整前缀；② `hello()` 在 `docPath()` 返回 `null` 时一律省略 `message.path`，未区分“合法的无路径版本根入口（如 `MonitorViewer.tsx:147`）”与“路径非法 / URL 不受识别” |
| 父端协议 `lenient(docPath)` 与冲突字段（含显式 `undefined`）处理 | `web/src/pages/ChatAgent/components/viewers/html/pageProtocol.ts:265-267,406-409`<br>`web/src/pages/Monitors/MonitorViewer.tsx:140-150` | `pageProtocol.ts:265-267` 缺少反斜线与 `..` 段拦截；`:407` 对 `page:ready` 使用 `path: lenient(docPath)`，会将被 schema 拒绝的非法路径静默转为 `path: null`；且结构化克隆会保留显式传入的 `{ path: undefined, root: true }` 自有属性，若仅在 Zod 解析后用 `m.path !== undefined` 检查会漏放该冲突消息进入 `bridged_root` |
| 非桥接页面跳转状态与属主帧上报 | `web/src/pages/ChatAgent/components/viewers/html/usePageBridge.ts:64-108`<br>`web/src/pages/ChatAgent/components/viewers/html/BridgedFrame.tsx:189-218`<br>`web/src/pages/ChatAgent/components/viewers/html/ArtifactFrames.tsx:132-170`<br>`web/src/pages/ChatAgent/components/viewers/HtmlViewer.tsx:148-156`<br>`web/src/pages/ChatAgent/components/filePanel/FileViewer.tsx:225-227` | `ViewFrame` 未传入 `onUnbridged`，且仅只读 `ViewFrame` 经 `FileViewer.tsx:226` 上报路径；属主的 `ArtifactFrames.tsx:138,164`（实时帧与版本帧）均未通过同一 `onShownDocChange` 上报文档状态；跳到图片、PDF、404 或脚本丢失 `?inject=view` 的页面后，旧 `bridge.path` 未清零 |
| 跨标签切换串写与过期定时器 | `web/src/pages/ChatAgent/components/viewers/html/liveFrame.ts:79-100`<br>`web/src/pages/ChatAgent/components/FilePanel.tsx:399-412` | `onBridge` 缺少帧标识，切换文件标签时同一渲染周期内旧 `live.bridge.path` 串写到新标签；`UNBRIDGED_GRACE_MS` 超时回调缺少导航序号核对 |
| 桌面端工具栏联动与 `openUrl` 来源 | `web/src/pages/ChatAgent/components/FileHeaderActions.tsx:236-248,467-481,528-604` | `openUrl` 从原标签 A 的 `selectedFile` 计算（`:236-240`），若在 `unbridged` 页直接复用会打开文档 A 而非当前页；`isLinkedHtmlDoc` 仅控制移动端菜单，桌面端工具栏（`:528-604`）仍绑定原标签 `selectedFile` |

---

## 2. 目标设计

### 2.1 前端统一身份读取：`useCallerIdentity()`、多部署兼容与刷新策略

新建 `web/src/hooks/useCallerIdentity.ts`（登记于 `scripts/dalton/dalton_paths.txt`），作为全前端唯一的调用者成员等级读取入口。

#### 多部署模式判定与漏配 `VITE_DALTON_AUTH` 边界（已决 8）

1. **上游平台模式（`isPlatformIdentityMode = !isDaltonBuild || isPlatformMode`，分别来自 `web/src/config/brand.ts:41` 与 `web/src/config/hostMode.ts:10`）**：
   - 跳过 `/api/v1/dalton/credentials/status` 请求；
   - 立即返回 `status: 'ready'`、`tier: 'admin'`、`authMode: 'platform'`、`isPlatform: true`、`isKnown: true`、`isConfirming: false`、`isConfirmingNow: () => false`、`getLatestIdentity: () => ({ ...platformReadySnapshot })`、`isExternal: false`、`canActAsOwner: true`、`canPublishPublic: true`、`canInvite: false`；
   - `PublishDialog`、评论、绑定、会话分享与共享工作区继续按上游平台既有资源权限与组织策略运行。
2. **Dalton 构建（`isDaltonBuild && !isPlatformMode`）**：
   - **`supabase` 缺环境变量兜底**：当 `DALTON_AUTH_MODE === 'supabase'` 但缺失 Supabase 环境变量（`!hasAuthEnv`）时，直接返回 `status: 'error'`、`tier: 'unknown'`（fail-closed）；
   - **一律向服务端 `/api/v1/dalton/credentials/status` 确认身份与运行模式**：在所有其余 Dalton 构建场景（含 `VITE_DALTON_AUTH=supabase`、`VITE_DALTON_AUTH=local` 及未设置 `VITE_DALTON_AUTH` 的构建）中，前端绝不凭本地 `DALTON_AUTH_MODE === 'oss'` 盲判单人管理员，一律通过透传 `AbortSignal` 的**单一共享查询函数 `fetchTeamCredentialsStatus({ signal })`** 发起 `GET /api/v1/dalton/credentials/status`（与 `useTeamCredentials()` 共用 `queryKeys.dalton.credentialsStatus(userId)` 及同一个 `queryFn`）：
     - 服务端返回 `200` 且 `auth_mode: "oss"` → `isSingleUserOss: true`、`isMultiUserAuth: false`、`tier: "admin"`、`canActAsOwner: true`、`canPublishPublic: true`、`canInvite: false`；
     - 服务端返回 `200` 且 `auth_mode: "supabase" | "local"` → `isSingleUserOss: false`、`isMultiUserAuth: true`，严格采用服务端返回的 `caller_tier` 与 `can_invite`；
     - **漏配 `VITE_DALTON_AUTH` 指向 `core/local` 后端的真实边界**：`AuthContext.tsx:100-125` 与 `authToken.ts:330-339` 的运行时认证切换不在本 PR 范围内（正常 `core/local` 部署按 `standalone-core.md:190` 配备 `VITE_DALTON_AUTH=local` 以挂载 `LocalAuthProvider`）。若前端漏配 `VITE_DALTON_AUTH` 却指向 `core/local` 后端，`/api/v1/dalton/credentials/status` 因无本地会话令牌收到 **`401 Unauthorized`**，`useCallerIdentity()` 据此进入 **`status: 'error'`、`tier: 'unknown'`**，全部特权操作 fail-closed 停用，彻底杜绝漏配构建变量时误显管理员入口。

#### 共享 `AbortSignal` 查询函数与 Hook 签名

在 `web/src/hooks/useTeamCredentials.ts` 中导出透传 `AbortSignal` 的唯一查询函数，供 `useTeamCredentials()`、`useCallerIdentity()` 与 `revalidate()` 三处严格共用：

```ts
export async function fetchTeamCredentialsStatus(
  context?: { signal?: AbortSignal },
): Promise<TeamCredentialsStatus> {
  const { api } = await import('@/api/client');
  const res = await api.get<TeamCredentialsStatus>(
    '/api/v1/dalton/credentials/status',
    { signal: context?.signal },
  );
  return res.data;
}
```

`useCallerIdentity.ts` 类型签名：

```ts
export type CallerTier = 'admin' | 'internal' | 'external' | 'unknown';
export type CallerIdentityStatus = 'loading' | 'ready' | 'error';
export type ServerDaltonAuthMode = 'supabase' | 'local' | 'oss' | 'platform';

export interface CallerIdentitySnapshot {
  tier: CallerTier;
  status: CallerIdentityStatus;
  authMode: ServerDaltonAuthMode | null;
  /** 上游平台模式（!isDaltonBuild || isPlatformMode） */
  isPlatform: boolean;
  /** 首次确认完成且得到合法等级 */
  isKnown: boolean;
  /** 是否正处于首次加载或任一实例触发的强制重验中（按 QueryClient + queryKey 跨实例共享） */
  isConfirming: boolean;
  isMultiUserAuth: boolean;
  isSingleUserOss: boolean;
  /** 语义化成员等级布尔值（仅反映成员等级，资源归属由各入口另行校验） */
  isAdmin: boolean;
  isInternal: boolean;          // admin 或 internal
  isExternal: boolean;          // 确认为 external
  canActAsOwner: boolean;       // 平台模式 / 单人 oss / 多用户 isInternal 为 true
  canPublishPublic: boolean;    // 平台模式 / 单人 oss / 多用户 isAdmin 为 true
  canInvite: boolean;           // 多用户下按服务端 can_invite；平台模式与单人 oss 恒为 false
  /** 团队凭据字段保留原有语义（is_pm 与凭据共享开关解耦于 isAdmin） */
  isTeamCredentialsShared: boolean;
  canManageCredentials: boolean;
  isPm: boolean;
  colleagueSharingEnabled: boolean;
}

export interface CallerIdentity extends CallerIdentitySnapshot {
  /**
   * 同步读取 queryClient.getQueryState(queryKey) 最新缓存数据与共享 revalidationRegistry，
   * 按同一确定规则实时计算最新身份快照（不等待 React 下一次重渲染），
   * 供所有提交、保存与复制回调在首行同时核对最新 isConfirming、status 与角色权限谓词。
   */
  getLatestIdentity: () => CallerIdentitySnapshot;
  /** getLatestIdentity().isConfirming 的同步快捷别名 */
  isConfirmingNow: () => boolean;
  /** 先同步置共享 entry.isConfirming = true 并广播，再通过 cancelQueries + fetchQuery({ queryFn: fetchTeamCredentialsStatus, staleTime: 0 }) 中止在途 HTTP 请求并发出全新请求 */
  revalidate: () => Promise<void>;
}

export function useCallerIdentity(options?: {
  /** 敏感面板打开时设为 true：挂载/打开、窗口回前台时调用 revalidate() 取消在途请求强制发网，且打开期间启用 60s 静默轮询 */
  revalidateOnOpen?: boolean;
  /** 配合弹窗 open 属性使用；open 为 true 时触发打开强制重验并激活 60s 后台静默刷新 */
  open?: boolean;
}): CallerIdentity;
```

#### 各状态与部署模式下的确定取值（`CallerIdentitySnapshot` 与 `getLatestIdentity()` 共用同一纯函数计算）

| 场景 / 状态 | `tier` | `status` | `isPlatform` | `isKnown` | `isConfirming` | `isAdmin` | `isInternal` | `isExternal` | `canActAsOwner` | `canPublishPublic` | `canInvite` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 上游平台模式（`!isDaltonBuild \|\| isPlatformMode`） | `'admin'` | `'ready'` | `true` | `true` | `false` | `true` | `true` | `false` | `true` | `true` | `false` |
| Dalton 构建，首次加载中 | `'unknown'` | `'loading'` | `false` | `false` | `true` | `false` | `false` | `false` | `false` | `false` | `false` |
| Dalton 构建，请求失败（含漏配 `VITE_DALTON_AUTH` 收到 `401` 或查库 `503`）或等级非法 | `'unknown'` | `'error'` | `false` | `false` | `false` | `false` | `false` | `false` | `false` | `false` | `false` |
| Dalton 构建，已确认后任一实例正在执行强制重验（打开/回前台/403） | 上次已确认值 | `'ready'` | `false` | `true` | `true` | 同上次值 | 同上次值 | 同上次值 | 同上次值 | 同上次值 | 同上次值 |
| Dalton 构建，已确认后正在执行 60s 后台静默刷新 | 上次已确认值 | `'ready'` | `false` | `true` | **`false`** | 同上次值 | 同上次值 | 同上次值 | 同上次值 | 同上次值 | 同上次值 |
| Dalton 多用户，`caller_tier: 'admin'` | `'admin'` | `'ready'` | `false` | `true` | `false` | `true` | `true` | `false` | `true` | `true` | `true` |
| Dalton 多用户，`caller_tier: 'internal'` | `'internal'` | `'ready'` | `false` | `true` | `false` | `false` | `true` | `false` | `true` | `false` | `true` |
| Dalton 多用户，`caller_tier: 'external'` | `'external'` | `'ready'` | `false` | `true` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
| Dalton 单人回环（服务端 `200` 且 `auth_mode: 'oss'`） | `'admin'` | `'ready'` | `false` | `true` | `false` | `true` | `true` | `false` | `true` | `true` | **`false`** |
| Dalton 认证误配（`VITE_DALTON_AUTH=supabase` 缺环境变量） | `'unknown'` | `'error'` | `false` | `false` | `false` | `false` | `false` | `false` | `false` | `false` | `false` |

#### 跨实例共享强制重验、共享 `AbortSignal` 查询函数、同步 `getLatestIdentity()` 与 60 秒静默保鲜（已决 4）

1. **按 `QueryClient + queryKey` 跨 Hook 实例共享重验状态、共用 `fetchTeamCredentialsStatus({ signal })` 与同步 `getLatestIdentity()` 的唯一规范实现**：
   - **为什么必须跨实例共享重验状态 + 同步读取最新身份快照**：
     1. 同一页面常有多个组件同时挂载 `useCallerIdentity()` 或 `useTeamCredentials()` 并共享同一个 `queryKey`（`queryKeys.dalton.credentialsStatus(userId)`）。当实例 B 调用 `revalidate()` 通过 `queryClient.cancelQueries({ queryKey })` 取消实例 A（或 `useTeamCredentials()`）正在进行的请求时，只有两个 Hook 共用同一个向 `api.get` 透传 `{ signal }` 的 `fetchTeamCredentialsStatus`，`cancelQueries` 才能真正触发 `AbortSignal` 中止底层 HTTP 请求；且只有 `activeSeq` 与 `isConfirming` 按 `(queryClient, queryKey)` 共享，被取消的实例 A 走完 `finally` 时才不会提前清除实例 B 仍在进行中的重验拦截；
     2. 当重验请求 2 返回降级后的新身份（如 `external` 或 `internal`）并走完 `finally` 将共享 `entry.isConfirming` 置回 `false` 的瞬间，在 React 尚未完成下一次重渲染之前，组件内先前已绑定的事件回调闭包所持有的 `identity.isAdmin` / `identity.canActAsOwner` / `identity.isExternal` 仍是旧的 `admin` 值。若回调仅检查 `!identity.isConfirmingNow()`，此时 `isConfirmingNow()` 已变为 `false`，旧闭包就会按旧 `admin` 身份误放行提交或复制。因此所有回调必须通过 **`identity.getLatestIdentity()`** 直接从 `queryClient.getQueryState(queryKey)` 与共享 `entry` 同步计算最新快照，同时校验确认态、就绪态与最新角色权限。
   - **共享状态容器、`useSyncExternalStore` 订阅与 `getLatestIdentity()` 计算**：
     - 在 `useCallerIdentity.ts` 模块内定义 **`const revalidationRegistry = new WeakMap<QueryClient, Map<string, { activeSeq: number; isConfirming: boolean; listeners: Set<() => void> }>>()`**（以 `QueryClient` 为弱引用根键，天然隔离不同 Vitest 测试用例；当 `listeners.size === 0 && !entry.isConfirming` 时从内层 `Map` 移除该键防内存泄漏）；
     - 抽出纯函数 `resolveCallerIdentitySnapshot(queryState, isConfirmingFromRegistry)`：`useCallerIdentity()` 的渲染返回值与 `getLatestIdentity()` 均调用同一纯函数——`getLatestIdentity()` 内部直接读取 `const queryState = queryClient.getQueryState<TeamCredentialsStatus>(queryKey)` 与 `const entry = getEntry(queryClient, queryKey)`，实时返回最新 `CallerIdentitySnapshot`（`isConfirmingNow()` 直接定义为 `() => getLatestIdentity().isConfirming`）。
   - **`revalidate()` 严格三步实现**：
     1. **同步递增共享序号并置位广播**：在共享 `entry` 上执行 `const seq = ++entry.activeSeq; entry.isConfirming = true; notifyListeners(entry)`（同 `queryKey` 下所有挂载实例的 `getLatestIdentity().isConfirming` 与 `isConfirmingNow()` 在同一事件循环 tick 内立即变为 `true`，且所有实例经 `useSyncExternalStore` 同步重渲染置 `isConfirming = true` 停用按钮）；
     2. **先 `cancelQueries` 中止在途旧 HTTP 请求，再 `fetchQuery` 发出全新请求**：先执行 **`await queryClient.cancelQueries({ queryKey })`**（无论在途请求是由 `useTeamCredentials()` 还是 `useCallerIdentity()` 发起、无论缓存是否已有数据，均无条件取消并触发 `fetchTeamCredentialsStatus` 的 `signal.aborted = true` 中止底层 HTTP 请求），再执行 **`await queryClient.fetchQuery({ queryKey, queryFn: fetchTeamCredentialsStatus, staleTime: 0 })`**，强制绕过 5 分钟 `staleTime` 发出全新网络请求并写入 Query 缓存；
     3. **共享序号核对后复位广播**：在 `finally` 中**仅当 `entry.activeSeq === seq` 时**才执行 `entry.isConfirming = false; notifyListeners(entry)`（即便此时组件尚未完成下一帧重渲染，任何旧闭包回调调用 `getLatestIdentity()` 也会直接读到 `queryClient.getQueryState(queryKey)` 中刚写入的最新降级 `data` 或 `error` 状态，彻底杜绝旧闭包放行）。
   - **三个强制重验触发时机**：
     - **打开敏感面板时**：`PublishDialog`、`ThreadSharePanel`、`WorkspaceSharingModal`、设置页团队管理标签在 `open` 由 `false` 变为 `true`（或组件首次以 `open=true` 挂载）时立即调用 `revalidate()`；
     - **窗口回到前台时**：敏感面板处于打开状态时，监听 `document` 的 `visibilitychange`（`document.visibilityState === 'visible'`）与 `window` 的 `focus` 事件并调用 `revalidate()`（同时将查询的 `refetchOnWindowFocus` 设为 `'always'`）；
     - **收到 `403` 时**：写操作统一错误处理工具检测到 `apiErrorStatus(err) === 403` 后，立即调用 `revalidate()` 取消在途请求并强制发网重验。
2. **60 秒持续打开后台静默保鲜（不置共享 `entry.isConfirming`）**：
   - 敏感面板处于 `open === true` 期间启用 `refetchInterval: 60_000` 与 `refetchIntervalInBackground: false`；
   - 该定时轮询作为**后台静默刷新，不修改共享 `entry.isConfirming`（`isConfirming` 与 `isConfirmingNow()` 保持 `false`）**，避免持续打开弹窗的用户每分钟遭遇一次按钮闪烁停用；
   - 一旦 60 秒轮询返回新的 `caller_tier`（如角色降级）或请求报错，React Query 立即更新 `queryState`，`getLatestIdentity()` 在同一时刻即反映新角色，下游 `useEffect` 与渲染逻辑同步应用降级阻断或错误态停用；若强验在此轮询在途时触发，强验的 `cancelQueries` 会直接中止该轮询的 HTTP 请求并发起带 `entry.isConfirming = true` 的新请求。
3. **未知态与强制重验期间的操作规则**：
   - 当 `status !== 'ready' || isConfirming` 时：**停用**开启分享、发布/重新发布、修改受众、修改权限、保存共享/记忆设置与**复制链接**按钮，顶部展示状态条（「正在确认账号权限…」或「权限确认失败，请重试」+ 重试按钮）；
   - **所有提交、保存与复制回调首行必须通过 `const latest = identity.getLatestIdentity()` 同步核对最新确认态、就绪态与角色权限谓词**（例如：`if (latest.isConfirming || latest.status !== 'ready') return;` 以及复制/开启分享要求 `if (!latest.canActAsOwner || latest.isExternal) return;`，公开发布要求 `if (!latest.canPublishPublic) return;`，外部成员监控发布要求 `if (latest.isExternal && visibility !== 'own') return;`）；
   - **停止已有分享例外**：当资源处于已分享态（会话 `is_shared === true` 或文件 `artifact.shared === true` / `existingMonitor?.visibility === 'public'`）时，**「停止分享」按钮在未知态与强制重验期间仍保持可点**；
   - **导航与团队区块**：设置页团队管理分组、研究总监共享卡、Home 团队记忆卡、侧边栏「团队」区与命令面板同事工作区在未知态保持隐藏（设置深链在 `loading` 时展示骨架屏）。
4. **成员等级与资源归属正交校验**：
   - `canActAsOwner` 仅表达成员等级资格；每个界面继续组合校验工作区/会话归属（`wsOwnerId === currentUserId`）、同事只读态（`teamReadOnly.isColleague`、`isThreadReadOnly`）、服务端返回的 `can_edit` / 分字段编辑标识以及访客分享态（`access.kind === 'share'`）。

#### 全前端入口收口与请求门控

- **`SettingsRail.tsx`（`useDaltonSettingsVisibility`）**：改用 `useCallerIdentity()` 获取 `tier`/`status`/`canInvite`/`isAdmin`/`isPm`；移除 `/channels/bindings.user_tier` 回退；`useUserCosSharingQuery` 门控为 `identity.isInternal && hasCapability('colleague_sharing')`。
- **`DaltonTeamOpsPanel.tsx`、`DaltonDiscordChannelsCard.tsx`**：统一改为读 `useCallerIdentity().isAdmin`，移除 `bindingsQuery.data?.user_tier` 判断。
- **`DaltonCosSharingCard.tsx`、`DaltonHomeTeamMemoryCard.tsx`**：卡片内部直接订阅 `useCallerIdentity()` 与 `useCapabilities()`；研究总监卡保留 `colleague_dispatch_enabled` 业务判断，团队记忆卡门控为 `identity.isInternal && hasCapability('conversation_archive')`。
- **`useTeamWorkspaces()`（`TeamSharing.tsx:63-71`）**：在共用 Hook 内统一门控 `enabled: enabled && identity.isInternal && hasCapability('colleague_sharing')`，侧边栏、画廊、命令面板共享同一门控。
- **`TeamSharing.tsx`、`CommandPalette/useVerbs.ts`、`NavigationPanel.tsx`、`NavigationRows.tsx`、`ThreadGallery.tsx`、`ChatView.tsx`、`MarketChatPanel.tsx`、`FilePanel.tsx`、`ShareButton.tsx`、`ThreadSharePanel.tsx`、`OwnerArtifactView.tsx`、`Automations.tsx`**：全部订阅 `useCallerIdentity()`，移除 `/workspaces/team.caller_tier` 与 `queryClient.getQueryData` 静态快照读取。
- **`OwnerArtifactView.tsx`（`/a/<code>` 属主页）**：订阅 `useCallerIdentity()`；对外部成员及身份未知态隐藏 `CommentTools`（`:133`）并改用只读 `ViewFrame`；发布按钮遵循与 `FilePanel` 一致的外部成员 `own` 监控规则（未知态停用）。
- **`WorkspaceSharingModal.tsx` 能力解耦与错误重验（已决 13 & 9）**：
  - **入口触发**：桌面与手机端（`ChatView.tsx:1906-1934`、`ThreadGallery.tsx:797-815`）只要满足 `hasCapability('colleague_sharing') || hasCapability('conversation_archive')` 均允许渲染弹窗入口（移除手机端仅认 `colleagueSharingReady` 的限制）；
  - **独立查询与独立呈现**：弹窗内按能力独立启用两路查询——共享设置（`useWorkspaceSharing`）仅在 `hasCapability('colleague_sharing')` 时启用，团队记忆设置（`useWorkspaceSettings`）仅在 `hasCapability('conversation_archive') && identity.isInternal` 时启用。关闭 `colleague_sharing` 时跳过 `/sharing` 请求，且跳过渲染研究总监控件（移除 `:142` 的 `cos_access ?? 'ro'` 默认值渲染）；外部属主仅渲染被服务端标记为可编辑的 `web_access`（`can_edit_web_access === true`），隐藏研究总监与团队记忆区；
  - **独立保存与 403 处理**：`handleSave()`（`:178-223`）首行调 `const latest = identity.getLatestIdentity()` 同步核对 `!latest.isConfirming && latest.status === 'ready'` 及最新角色对各区块的保存资格（`cos_access` 与 `team_memory` 额外要求 `latest.isInternal`），分别检查共享区与团队记忆区各自的加载态、编辑权与变更态，仅提交有变更且被允许的请求；失败时（`:215-221`）使用 `formatApiErrorDetail(err)` 展示后端明确原因，收到 `403` 同步调用 `identity.revalidate()`。

---

### 2.2 服务端写接口统一权限校验、错误码与分批兼容

#### 统一 403 线上响应格式、适用边界与错误码

所有因 Dalton 成员身份或角色拒绝的写接口统一返回 **HTTP `403 Forbidden`**，通过 FastAPI `HTTPException(status_code=403, detail={"code": ..., "message": ...})` 产生如下完整线上 JSON：

```json
{
  "detail": {
    "code": "external_member_forbidden",
    "message": "外部成员无权执行此操作"
  }
}
```

- **适用范围（平台模式零回归，已决 8）**：新建 `src/dalton/auth_guards.py` 提供 `require_internal_member(user_id)` 与 `require_admin_member(user_id)`，两个守卫**仅在 `is_dalton_multiuser_auth_enabled()` 为真时执行角色检查**；在上游平台模式（`HOST_MODE != "oss"`）及单人 `oss` 模式下直接放行，继续由上游资源权限（`reached()`、`refuse_link_change()`、平台组织策略等）裁决。
- **保留边界**：`401 Unauthorized`（未登录或令牌失效）、隐藏资源的 `404 Not Found`（不可见的他人工作区/线程）、能力关闭的 `404`、`503 Service Unavailable`（如 `sharing.py:916` 的 `Team reader verification unavailable`）以及既有 `detail.type` 字段（如 `share_out.py:73` 的 `{"type": "managers_only", "message": ...}`、`share_audience.py:107,123` 的 `shared_members_only` / `audience_unavailable` 与 `credentials.py:39` 的 `base_url_blocked`）全部保持原样。
- **前端错误解析复用**：前端统一通过 `web/src/api/errors.ts:12-96` 的 `apiErrorCoded(err, code)`、`apiErrorDetailMessage(err)` 与 `formatApiErrorDetail(err)` 读取错误码和中文提示（`ApiError` 构造函数 `web/src/api/client.ts:85-92` 已自动提取 `detail.message`）。在 `PublishDialog.tsx`、`ThreadSharePanel.tsx` 与 `WorkspaceSharingModal.tsx` 统一接入 `formatApiErrorDetail(err)`，并在捕获 `403` 时调用 `identity.revalidate()`。
- **DB 故障语义区分**：`get_user_tier()` 查询失败时 fail-closed 返回 `'external'`（写接口返回 `403 external_member_forbidden`）；`credentials.py:53-114` 在 `local` 模式查表失败时返回 `503`（使 `/credentials/status` 返回 `503`，前端 `useCallerIdentity()` 进入 `status: 'error'`、`tier: 'unknown'` 并停用提交）。

标准错误码定义（`src/dalton/auth_guards.py`）：

| `detail.code` | 适用场景 |
|---|---|
| `external_member_forbidden` | 外部成员执行仅限内部/管理员的写操作（开启会话分享、修改文件受众、新建/追加/编辑评论、绑定/解绑线程、私有或已分享文件 `kept=True`、修改研究总监共享或团队记忆、未先降级直接发布/重发非 `own` 监控面板、通过阶段一成员校验后属主 `owner_tier == "unknown"` 时提交工作区设置/共享、创建/撤销邀请等） |
| `admin_required` | 内部成员（非管理员）执行仅限管理员的写操作（开启含工作区文件的会话分享、将文件受众或监控面板设为 `public`、直接调用文件裸发布、已分享文件 `kept=True`、管理 Discord 频道或微信机器人、在 `local` 模式创建 `internal`/`admin` 邀请等） |
| `artifact_publishing_disabled` | 沿用 `records.py:238` 既有错误码（发布总开关关闭） |
| `invites_disabled_in_oss` | 单人 `oss` 模式下调用团队邀请写接口 |

#### 后端单人回环模式贯通与 `/credentials/status` 扩展（已决 8）

在 `src/dalton/settings.py` 新增：

```python
def is_dalton_single_user_oss_mode() -> bool:
    """显式单人回环模式：HOST_MODE == 'oss' 且 DALTON_AUTH_MODE == 'oss'。"""
    from src.config import settings as app_settings

    return app_settings.HOST_MODE == "oss" and get_dalton_auth_mode() == "oss"
```

- **`src/dalton/access_tiers.py` 两个分级函数同步短路**：`get_user_tier()` 与 `classify_owner_tier()` 在 `is_dalton_single_user_oss_mode()` 为真时均直接返回 `"admin"`，贯通 `/credentials/status`、工作区设置/共享、监控面板、文件发布/受众、会话分享及渠道管理。
- **`GET /api/v1/dalton/credentials/status`（`src/dalton/app/router.py:1088-1117`）**：
  - 响应新增服务端确认字段 `"auth_mode": get_dalton_auth_mode()`（`"supabase" | "local" | "oss"`）；
  - 当 `is_dalton_single_user_oss_mode()` 为真时，返回 `caller_tier: "admin"`、`can_invite: False`、`auth_mode: "oss"`；
  - `is_credential_admin()`（`credentials.py:291-304`）保持既有语义。
- **`src/dalton/invites.py`**：单人 `oss` 模式下直接拒绝邀请写接口，返回 `403` `{"detail": {"code": "invites_disabled_in_oss", "message": "单人模式下无需使用团队邀请"}}`。

#### 逐接口改造规范

1. **监控面板四条路径、独立降级、事务内 `FOR SHARE` 三要素复核、`is_dalton_enabled()` 一致锁序停止与 `SELECT {_COLUMNS}` 历史公开列表（已决 1 & 2）**
   - 在 `src/dalton/monitors/permissions.py` 新增统一校验函数 `require_monitor_publish_visibility(tier, visibility)`：
     - `tier == "external"` 且 `visibility != "own"` → 抛出 `403` `{"code": "external_member_forbidden", "message": "外部成员发布或更新监控面板前须先将可见性降为「仅自己」"}`；
     - `visibility == "public"` 且 `tier != "admin"` → 抛出 `403` `{"code": "admin_required", "message": "只有管理员可以将监控面板设为公开链接"}`。
   - 四条写入路径与事务内 `FOR SHARE` 严格复核：
     - **`POST /api/v1/dalton/monitors`（`service.py:411-535` → `commit_monitor_publish_tx` `db.py:697-780`）**：
       1. 服务层（`service.py:418,467-470`）：校验请求 `payload.visibility`；读到已有监控 `existing` 后，若调用者为 `external` 且 `existing.visibility != "own"`，即使请求传 `visibility="own"` 也立即拒绝切版（返回 `403 external_member_forbidden`，提示先经 `PATCH /api/v1/dalton/monitors/{slug}` 独立降级）；
       2. 写入事务 `commit_monitor_publish_tx`（`db.py:728-760`）：在 `lock_owned`（锁 `share_links`）与 `SELECT ... FROM dalton_monitors WHERE artifact_id = %s::uuid FOR UPDATE`（锁 `existing`）之后、写库切版本之前，在**同一事务游标 `cur`** 内同时核对三要素：
          - 读取 `monitor_owner_id` 的实时角色 `live_tier`：`local` 模式下于同一事务游标执行 **`SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE`**（`dalton_user_roles.tier` 为非键列，`src/dalton/auth/roles.py:170-179` 的 `set_user_role` 在将 `internal` 降为 `external` 时执行 `ON CONFLICT DO UPDATE SET tier = ...` 获取 `FOR NO KEY UPDATE`，使用 **`FOR SHARE`** 可与 `FOR NO KEY UPDATE` 直接互斥阻塞，而 `roles.py` 事务不触及 `share_links` 或 `dalton_monitors`，既无死锁环又保证角色降级与版本写入严格串行；缺行按 `"external"` 处理），`supabase` 模式读取实时名单；
          - 若 `existing is not None` 且 `live_tier == "external"` 且 `existing["visibility"] != "own"`，即使本次参数 `visibility == "own"` 也直接抛出 `403 external_member_forbidden`（彻底堵住读取文件快照期间发生角色降级时一步降级并切版本的窗口）；
          - 对拟写入的 `visibility` 执行 `require_monitor_publish_visibility(live_tier, visibility)`。
     - **`POST /api/v1/dalton/monitors/{slug}/republish`（`service.py:631-699`）**：调用 `require_monitor_publish_visibility(tier, monitor.visibility)`（替代仅判 `public` 的 `:649`），拒绝前写 `record_monitor_publish_failure()`。
     - **回合后自动更新 `republish_workspace_monitors_after_turn`（`service.py:740-839`）**：循环内实时查 `owner_tier = await get_user_tier(monitor.owner_user_id)`，执行 `require_monitor_publish_visibility(owner_tier, monitor.visibility)`；不满足时抛出暂停异常并写入 `last_publish_error`（原已发布版本保留访问）。
     - **代理工具 `src/dalton/tools/monitor_tools.py:653-710`**：保留 `:665-670` 前置校验，并复用服务层守卫透传清晰指引（提示先将可见性降为 `own`）。
     - **版本切割事务 `commit_monitor_version_tx` → `_cut_and_finalize_monitor_in_cur`（`src/dalton/monitors/db.py:906-1055`）与数据任务保留边界**：
       - 工作区监控（`service.py:689,825`）传入工作区可见性复核回调：在行锁 `share_links` 与 `dalton_monitors FOR UPDATE` 之后，用同一事务游标 `cur` 读取 `effective_owner` 的实时角色（`local` 模式下 **`SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE`**）并调用 `require_monitor_publish_visibility(live_tier, live_visibility)`，复核 `public`（`admin`）与 `company`/`members`（`admin`/`internal`）；
       - **数据任务监控 `publish_data_job_monitor`（`src/dalton/monitors/service.py:1084-1094`）保持既有 `_verify_job_public_in_tx` 公开发布审批校验与 `COMPANY_SYSTEM_USER_ID` 语义不变**。
   - **属主管理查询放行（`service.py:382-391`）**：`get_monitor_by_artifact(artifact_id, viewer_id)` 在 `monitor.owner_user_id == viewer_id` 时直接放行读取该属主自己的面板元数据，并返回 `can_manage=True`（即使属主已降为 `external` 且面板为 `company`）；非属主与匿名访问继续走 `require_monitor_view`。
   - **公开监控停止分享后端 `is_dalton_enabled()` 分支与一致锁序原子收口 + `SELECT {_COLUMNS}` 历史公开监控列表查询（已决 2）**：
     - **`POST /api/v1/artifacts/{artifact_id}/unshare`（`records.py:671-684`）与 `PATCH /api/v1/workspaces/{id}/share-links/{code}`（`share_links.py:203-231`）**：
       - 先判断 `from src.dalton.settings import is_dalton_enabled`；
       - 当 **`not is_dalton_enabled()`**（仅运行上游迁移、不存在 `dalton_monitors` 表的部署）时：**原样调用 `await artifacts_db.unshare(user_id=owner_id, artifact_id=...)`**，完全不查询 `dalton_monitors`；
       - 当 **`is_dalton_enabled()` 为真**时：统一调用原子事务辅助函数 `unshare_artifact_and_downgrade_public_monitor(user_id=owner_id, artifact_id=artifact_id)`，严格遵循与 `update_monitor_settings_tx`（`db.py:547`）、`commit_monitor_publish_tx`（`db.py:728`）、`commit_monitor_version_tx`（`db.py:1010`）**完全一致的加锁顺序**：
         1. **第一步（先锁文件记录）**：调用 `locked = await artifacts_records_db.lock_owned(cur, user_id=owner_id, artifact_id=artifact_id)` 锁定 `share_links` 行；
         2. **第二步（再锁关联监控）**：在同一游标执行 `SELECT monitor_id::text AS monitor_id, visibility FROM dalton_monitors WHERE artifact_id = %s::uuid FOR UPDATE`；若存在记录且 `visibility == "public"`，同事务执行 `UPDATE dalton_monitors SET visibility = 'own', acl_revision = acl_revision + 1, updated_at = NOW() WHERE monitor_id = %s::uuid`；
         3. **第三步（撤销文件分享并递增 revision）**：调用 `await artifacts_records_db.unshare_in_cur(cur, user_id=owner_id, artifact_id=artifact_id)`（清除 `shared_at`/`files`/`shared_seq` 并将 `share_links.revision` 加 1）。
     - **并发安全证明**：锁序统一为 `share_links → dalton_monitors`，消除与发布/重发/设置更新之间的死锁环；若停止分享先提交，`share_links.revision` 已递增，任何在撤销前已读取快照的并发首次 `POST /monitors` 或 `republish` 随后进入 `lock_owned` 时，都会在 `read_basis.check(locked)`（`src/server/database/artifacts/versions.py:125`）因 `locked.revision != self.revision` 抛出 `SharingChanged`（`409`），绝不会用旧快照恢复匿名访问；若发布先提交，停止分享后入锁即查到新监控并将其降为 `own`。降级后 `get_public_monitor_detail`（`service.py:284`）、`serve_public_monitor_file`（`:1347`）与 `read_public_monitor_data`（`:1442`）均返回 `404`。
     - **工作区分享列表 SQL 扩展（`src/server/database/share_links.py:218-227` `list_shared`，标注 `DALTON-HOOK(monitors)`）**：
       - 先判断 `from src.dalton.settings import is_dalton_enabled`；
       - 当 **`not is_dalton_enabled()`** 时：**原样执行上游 SQL**（`SELECT {_COLUMNS} FROM share_links l WHERE l.workspace_id = %s AND l.shared_at IS NOT NULL ORDER BY l.shared_at DESC`），完全不引用 `dalton_monitors`；
       - 当 **`is_dalton_enabled()` 为真**时：**直接复用模块既有的 `{_COLUMNS}` 常量**（`share_links.py:34-37`，内含 `ARTIFACT_COLUMNS` 的 `created_thread_id`、`bound_thread_id`、`legacy_refused`、`loaded_files`、`l.artifact_id::text` 等全部列及 `app_command` 子查询，保持与 `_link()` 列契约完全一致，严禁手写列子集），仅扩展 `LEFT JOIN`、`WHERE` 与 `ORDER BY`：
         ```sql
         SELECT {_COLUMNS}
         FROM share_links l
         LEFT JOIN dalton_monitors m
           ON m.artifact_id = l.artifact_id AND m.visibility = 'public'
         WHERE l.workspace_id = %s
           AND (l.shared_at IS NOT NULL OR m.monitor_id IS NOT NULL)
         ORDER BY COALESCE(l.shared_at, m.updated_at, l.created_at) DESC
         ```
         该 `WHERE` 条件仅匹配两类已公开文件记录：① 普通已分享文件（`l.shared_at IS NOT NULL`）与 ② 文件分享链接已关闭但关联公开监控的历史文件（`l.shared_at IS NULL AND m.monitor_id IS NOT NULL`），两者经 `_link()`（`:92-108`）与 `artifact_from_row()`（`database/artifacts/records.py:136`）零报错组装返回；私人 `app` 链接受 `049_share_links.py:44-47` 的 `share_links_app_shape` 约束强制 `shared_at IS NULL` 且监控只接受 `kind = 'file'`（`records.py:271`），因此**私人 `app` 链接绝不进入 `list_shared` 列表**（`app` 链接继续由单条接口 `GET /api/v1/share-links/{code}` `share_links.py:239` 经 `get_link()` / `_link()` 返回）。

2. **文件受众、保留、评论与绑定（已决 3 & 12）**
   - `PUT /api/v1/artifacts/{artifact_id}/audience`（`src/server/app/artifacts/records.py:632-668`）：增加 `_refuse_publishing_disabled()` 与 `require_internal_member(x_user_id)`；当 `body.audience == PUBLIC` 时额外调用 `require_admin_member(x_user_id)`。
   - `PUT /api/v1/artifacts/{artifact_id}/kept`（`src/server/app/artifacts/records.py:356-387`）：`kept` 控制属主版本固定（访客仍看 `shared_seq`）。**精确执行顺序**：
     1. 先执行 `artifact, access = await reached(artifact_id, x_user_id)`；
     2. 当 `body.kept is False` 时：跳过成员等级守卫，直接执行清理并返回 `200`；
     3. 当 `body.kept is True` 时：**先调用 `await require_internal_member(x_user_id)`**（外部成员无论操作私有文件还是已分享文件，一律在此返回 `403 external_member_forbidden`），**再**当 `artifact.shared` 为真时调用 `await require_admin_member(x_user_id)`（内部成员操作已分享文件在此返回 `403 admin_required`）。
   - `PUT /api/v1/artifacts/{artifact_id}/binding`（`:419-436`）与 `DELETE /api/v1/artifacts/{artifact_id}/binding`（`:439-448`）：两者均接入 `require_internal_member(x_user_id)`。
   - `src/server/app/artifacts/comments.py`：
     - `POST /{artifact_id}/comments`（`:143`，成功返回 `201`）、`POST /{artifact_id}/comments/{comment_id}/messages`（`:172`，成功返回 `201`）、`PATCH /{artifact_id}/comments/{comment_id}`（`:202`，成功返回 `200`）全部接入 `require_internal_member(x_user_id)`；
     - `GET /threads/{thread_id}/queued-comments`（`:110`）、`GET /{artifact_id}/comments`（`:131`）、`POST /{artifact_id}/comments/{comment_id}/read`（`:232`）、`POST /{artifact_id}/comments/read`（`:258`）、`DELETE /{artifact_id}/comments/{comment_id}`（`:276`，成功返回 `204`）保持对外部成员开放（仅限其经 `reached()` 可达的自有制品）。

3. **会话读取/状态 vs 会话分享管理：改造 `_guard_thread_not_foreign_home_stand_in`、重写 `require_thread_access`（保留 `:1477-1571` 写保护）、10 处多用户开关、双表状态码与停止分享校验顺序（已决 7）**
   - **全部 10 处站点统一改用 `is_dalton_multiuser_auth_enabled()`**（同时覆盖 `supabase` 与 `local`）：
     1. 会话读取与运行状态：`src/server/app/threads/crud.py:317`（`GET /api/v1/threads/{thread_id}`）、`src/server/app/threads/messaging.py:1453`（会话消息与流式回放读取）、**`src/server/app/threads/messaging.py:1613`（`GET /api/v1/threads/{thread_id}/status`）**；
     2. 线程访问与归属：`src/dalton/sharing.py`（`require_thread_access` 与 `_guard_thread_not_foreign_home_stand_in`）、`src/server/utils/api.py:487`（`require_thread_owner`）、`src/server/services/shared_workspaces/access.py:302`（`_dalton_external_outside`）；
     3. 会话分享管理：`src/server/app/threads/sharing.py:61,88,160,204,272`（`_require_non_external_for_share`、`_require_admin_for_share_files`、`_authorize_thread_share`、`POST /threads/{id}/share`、`GET /threads/{id}/share`）。
   - **改造 `src/dalton/sharing.py:_guard_thread_not_foreign_home_stand_in`（`:1043-1052`）以对齐个人 Home 的属主等级层级**：
     - 当 `ws_owner_for_check != uid` 时：
       1. **先检查他人 Home 替身**（`(ws_obj and is_home_stand_in_row(ws_obj)) or is_home_stand_in_id(ws_owner_for_check, ws_id_for_check)`）：维持抛出 **`404 Thread not found`**（替身永不向非属主暴露存在性）；
       2. **再检查他人个人 Home**（`(ws_obj and (is_home_row(ws_obj) or bool(ws_obj.get("is_home")))) or is_home_id(ws_owner_for_check, ws_id_for_check)`）：
          - 在 `is_dalton_multiuser_auth_enabled()` 下调用 `owner_tier = await classify_owner_tier(ws_owner_for_check, conn=conn)`：若 `owner_tier in ("external", "unknown")`，抛出 **`404 Thread not found`**（与 `require_workspace_access:1280-1290` 及 `router.py:485-488` 一致，外部或 `unknown` 属主的个人 Home 对含管理员在内的所有非属主隐藏为 `404`）；
          - 其余情形（属主为 `internal` / `admin` / `company` 或单人模式）：抛出 **`403 Forbidden`**（与 `require_workspace_access:1304-1309` 及 `api.py:525` 完全一致，不再误抛 `404`）。
   - **从函数顶部 `:1392` 起调整 `src/dalton/sharing.py:require_thread_access`（`:1383-1571`），并完整保留 `:1477-1571` 既有写权限拦截**：
     1. **第一步（最顶层解析元数据并拦截外部成员跨区会话，位于 `if not _sharing_auth_enabled():` 之前）**：
        - 解析 `meta`、`resolved_workspace_id`、`thread_owner` 与 `ws_owner`（当 `resolved_workspace_id is None` 或 `owner_id is None` 或 `workspace_owner_id is None` 时先查 `conv_db.get_thread_auth_meta(thread_id, conn=conn)`；会话不存在或无 `workspace_id` 时抛 `404 Thread not found`）；
        - 在 `is_dalton_multiuser_auth_enabled()` 下，若 `uid != thread_owner or (ws_owner and ws_owner != uid)` 且 `await get_user_tier(uid, conn=conn) == "external"`，**立即抛出 `404 Thread not found`**（覆盖他人普通工作区、他人个人 Home 与他人 Home 替身，且包含外部成员本人历史发起的跨区会话，无论 `colleague_sharing` 与 `colleague_dispatch` 开关状态如何）。
     2. **第二步（重写 `if not _sharing_auth_enabled():` 早退分支，替换原 `:1392-1412`）**：
        - 若 `uid == thread_owner and (not ws_owner or ws_owner == uid)`（自有工作区 + 自有会话）：**直接 `return thread_owner`（`200`）**；
        - 否则（任意跨工作区或非会话属主请求）：先调用改造后的 `await _guard_thread_not_foreign_home_stand_in(thread_id, uid, workspace_id=resolved_workspace_id, workspace_owner_id=ws_owner, meta=meta, conn=conn)`（他人 Home 替身 → **`404`**；外部/`unknown` 属主个人 Home → **`404`**；内部/管理员属主个人 Home → **`403`**），其余跨区非双属主请求（含他人普通工作区以及内部成员读取本人历史在他人工作区发起的派发会话）统一抛出 **`403 Forbidden`**。
     3. **第三步（`_sharing_auth_enabled()` 为真分支，`:1414-1571`）——仅调整 `:1451-1476` 发起人 `read`/`web` 快捷路径，原样保留 `:1477-1571` 全部既有分支**：
        - 执行 `require_team_reader(uid)`；
        - `:1451` 的发起人快捷路径 `is_colleague_thread` 同时要求 **`_sharing_auth_enabled() and is_colleague_dispatch_enabled()`**：当 `is_colleague_thread and uid == thread_owner` 且 `cleaned_need == "read" and cleaned_channel == "web"` 时，调用改造后的 `_guard_thread_not_foreign_home_stand_in(...)`（他人 Home 替身 → **`404`**；外部/`unknown` 属主个人 Home → **`404`**；内部/管理员属主个人 Home → **`403`**），普通工作区直接 `return thread_owner`（**`200`**）；
        - **原样保留 `:1477-1571` 既有代码不变**：
          - `:1477-1531`：同事会话发起者 `uid == thread_owner` 的 `is_tail_turn`、`cos` 渠道读取及 `cleaned_need == "write"` 写权限与 `COLLEAGUE_ACCESS_REVOKED_CODE` 校验保持原样；
          - `:1533-1545`（分支 2）：**工作区属主 B 访问同事在 B 工作区内发起的会话（`uid == ws_owner and thread_owner != uid`）**——仅在 `cleaned_need == "read"` 时 `return thread_owner`，**当 `cleaned_need == "write"` 时继续在进入 `require_workspace_access`（`:1250-1255` 对属主读写通放）之前直接抛出 `403`（`"该对话由同事发起，工作区所有者仅可查看"`）**；
          - `:1547-1571`（分支 3）：其余请求进入 `require_workspace_access`（内部按 `owner_tier == "unknown"`/`"external"` 个人 Home `404` → 他人 Home 替身 `404` → 内部/管理员个人 Home `403` → 普通工作区按权限 `200`/`403`/`404` 裁决，`:1556-1559` 将工作区 `404` 转译为 `404 Thread not found`），且 **`:1565-1569` 在工作区写权限通过后，对 `cleaned_need == "write" and uid != resolved_thread_owner` 继续抛出 `403`（`"该对话由同事发起，仅创建者可继续提问"`）**。
   - **表 A：会话读取与状态（`GET /api/v1/threads/{id}` `crud.py:309`、消息回放 `messaging.py:1453`、运行状态 `GET /api/v1/threads/{id}/status` `messaging.py:1613`，`supabase` 与 `local` 完全一致）**：

     | 调用者与目标会话/工作区关系 | `colleague_sharing = on` | `colleague_sharing = off` |
     |---|---|---|
     | **外部成员 → 他人任意工作区会话**（普通 / 个人 Home / Home 替身，含本人历史发起） | **`404`**（`require_thread_access` 第一步最顶层拦截） | **`404`**（`require_thread_access` 第一步最顶层拦截） |
     | **内部/管理员 → 他人 Home 替身（stand-in）任意会话** | **`404`**（`_guard_thread_not_foreign_home_stand_in` / `_is_home_stand_in_workspace` `:1298-1301`） | **`404`**（第二步 `_guard_thread_not_foreign_home_stand_in` 替身分支） |
     | **内部/管理员 → 外部属主（或 `unknown` 属主）的个人 Home 任意会话** | **`404`**（`_guard_thread_not_foreign_home_stand_in` / `require_workspace_access` `:1280-1290`） | **`404`**（第二步 `_guard_thread_not_foreign_home_stand_in` 外部/`unknown` 个人 Home 分支） |
     | **内部/管理员 → 内部/管理员属主的个人 Home 任意会话** | **`403`**（`_guard_thread_not_foreign_home_stand_in` / `require_workspace_access` `:1304-1309`） | **`403`**（第二步 `_guard_thread_not_foreign_home_stand_in` 非外部个人 Home 分支，对齐 `api.py:525`） |
     | **内部/管理员 → 他人普通工作区、他人发起的会话** | 按共享工作区读权限：可见（`ro`/`rw`）返回 **`200`**，不可见（`hidden` / 外部隐藏）返回 **`404`** | **`403`**（第二步非双属主拒绝） |
     | **内部/管理员 → 他人普通工作区、本人历史发起的派发会话** | `colleague_dispatch = on` 时走发起人快捷路径返回 **`200`**；`colleague_dispatch = off` 时按共享工作区读权限返回 **`200`** / **`404`** | **`403`**（第二步非双属主拒绝） |
     | **任意成员 → 自有工作区 + 自有会话** | **`200`** | **`200`** |

   - **表 B：会话分享管理（`GET /api/v1/threads/{id}/share` 与 `POST /api/v1/threads/{id}/share`，`supabase` 与 `local` 完全一致）**：

     | 调用者与会话关系 | `colleague_sharing = on` | `colleague_sharing = off` |
     |---|---|---|
     | **外部成员 → 他人工作区任意会话**（含本人历史发起的会话） | GET **`404`**；POST **`404`**（`require_thread_owner` `api.py:487` 隐藏） | GET **`404`**；POST **`404`**（`require_thread_owner` `api.py:487` 隐藏） |
     | **内部/管理员 → 他人工作区、他人发起的会话** | GET **`403`**；POST **`403`**（`require_thread_owner` `api.py:507` 拒绝非会话属主） | GET **`403`**；POST **`403`**（`require_thread_owner` `api.py:507` 拒绝非会话属主） |
     | **内部/管理员 → 他人工作区、本人历史发起的派发会话** | GET **`403`**；POST **`403`**（公开分享要求同时拥有会话与工作区，由 `threads/sharing.py:204,272` 拒绝） | GET **`403`**；POST **`403`**（由 `threads/sharing.py:204,272` 拒绝） |
     | **任意成员 → 自有工作区 + 自有会话** | GET **`200`**；POST 按下方精确顺序校验 | GET **`200`**；POST 按下方精确顺序校验 |

   - **`POST /api/v1/threads/{thread_id}/share` 的精确校验顺序（`threads/sharing.py:184-239`）**：
     1. 执行 `_authorize_thread_share`（`:184`）、`refuse_link_change`（`:186`）、`get_thread_by_id`（`:188`）与工作区属主一致性校验（`:204`，`is_dalton_multiuser_auth_enabled()`）；
     2. **先确定本次生效的 `effective_req_permissions`**（位于 `:191-201` 受众校验之前）：
        - 若 `is_dalton_multiuser_auth_enabled()` 且调用者为 `external` 且 `request.is_shared is False`：置 **`effective_req_permissions = None`**；
        - 其余情形：`effective_req_permissions = request.permissions`。
     3. 后续 `:191-201`（`chosen` 与 `require_members_audience()`）和 `:222-233`（权限合并与写库）一律消费 `effective_req_permissions`：
        - 外部成员调 `is_shared: false`（无论 `permissions` 省略、显式 `null`、空对象 `{}` 或旧前端附带 `audience: "organization"` 的完整对象）→ 跳过受众校验与权限合并，仅写 `is_shared = False` 并保留数据库原有 `share_permissions` 返回 `200`；
        - 外部成员调 `is_shared: true` → 返回 `403 external_member_forbidden`；
        - 内部/管理员调 `is_shared: false`：若 `effective_req_permissions is None` 或 `not effective_req_permissions.model_fields_set`（空 `{}`）则仅更新 `is_shared = False`；若携带显式字段（如关闭态预设受众）则执行受众校验并仅合并 `model_fields_set`；
        - 任何成员调 `is_shared: true` 且有效权限含 `allow_files` 或 `allow_download` → 调用 `require_admin_member(x_user_id)`。

4. **工作区设置、个人研究总监共享与 Home 团队记忆（已决 6 & 11，与 #504 对齐）**
   - `GET/PUT /api/v1/dalton/sharing/user-settings`（`router.py`）：`GET` 为 `CORE_CAPABILITY`（关闭 `colleague_sharing` 时返回 200 中性结果并经 `is_dalton_auth_enabled()` 校验成员资格与返回真实 `caller_tier`、`can_edit: false`）；`PUT` 保持 `CapabilityId.COLLEAGUE_SHARING`（关闭时返回 404），开启时外部成员写请求返回 `403 external_member_forbidden`；两接口认证判定统一使用 `is_dalton_auth_enabled()`。
   - `GET/PUT /api/v1/dalton/user-settings`（Home 团队记忆，`router.py`）：挂载 `Depends(require_capability_dep(CapabilityId.CONVERSATION_ARCHIVE))` 与 `CapabilityId.CONVERSATION_ARCHIVE` 注册表映射（关闭归档时 GET/PUT 返回 404），并将认证判定从 `is_dalton_supabase_auth_enabled()` 改为 `is_dalton_auth_enabled()`。
   - `GET/PUT /api/v1/dalton/workspaces/{workspace_id}/settings`（工作区设置，`router.py`）：在 `DALTON_ROUTE_CAPABILITY_REGISTRY` 中保持核心接口 `CORE_CAPABILITY`；关闭 `conversation_archive` 时，符合权限的属主仍可 GET 读取现值与 PUT 保存设置，归档运行时继续停用。外部成员与成员资格两阶段校验在接口函数体中执行；当 `colleague_sharing` 关闭时，非属主访问外部属主工作区的 GET 与 PUT 均返回 `404 Workspace not found`（与 `GET /workspaces/{id}/sharing` 一致）。
   - **工作区设置/共享分模式两阶段校验顺序与 GET / PUT 对齐矩阵**（`GET/PUT /workspaces/{id}/sharing` `router.py:433-710` 与 `GET/PUT /workspaces/{id}/settings` `router.py:713-921`）：
     - **阶段一：调用者成员资格校验（`router.py:461-464` 先执行 `is_pm = await is_admin_user(uid, include_config_defaults=True)`，当 `not is_pm` 时执行 `require_team_reader(uid)` `sharing.py:866-929`）**：
       - **`local` 模式**：`is_admin_user`（`access_tiers.py:181-192`）**只查 `dalton_user_roles` 表**（不读取配置默认 `pm_user_ids`，且查库异常捕获为 `False`）；当调用者在 `dalton_user_roles` 缺行或 `require_team_reader(uid)` 调用的 `is_team_member_user(uid)`（`sharing.py:878-888` / `access_tiers.py:340-350`）遇到查库异常时，统一在阶段一抛出 **`403 Forbidden`**，`GET` 与 `PUT` 均在阶段一终止；
       - **`supabase` 模式**：非 `is_pm` 调用者若非团队成员，在 `require_team_reader(uid)` 抛出 **`403 Forbidden`**；若无 `_verified_token_identity` 且 `user_db.get_user(uid)` 发生数据库查询异常，按 `sharing.py:905-917` 抛出 **`503 Team reader verification unavailable`**，`GET` 与 `PUT` 均在阶段一终止；
     - **阶段二：已通过阶段一成员资格校验后（`local` 合法成员或 `supabase` 合法成员/`is_pm`），按目标工作区属主分级 `classify_owner_tier(owner_id)` 与归属裁决**：
       - **不可见资源 404 隐藏**（GET 与 PUT 一致）：非属主外部成员访问他人工作区、非管理员访问隐藏的外部工作区、任意非属主访问 `owner_tier == "unknown"` 或外部 Home 工作区，全部返回 `404 Workspace not found`；
       - **属主自身 `owner_tier == "unknown"`**（调用者已通过阶段一校验，但 `classify_owner_tier(owner_id)` 单独因查库异常或 `supabase` 用户资料缺失返回 `"unknown"`；单人 `oss` 已短路为 `"admin"` 不会进入此分支）：`GET /sharing` 与 `GET /settings` 均返回 `200` 且 `can_edit: false` 及全部子项编辑标识（`can_edit_web_access`、`can_edit_cos_access`、`can_edit_team_memory`）为 `false`；**`PUT /workspaces/{id}/sharing`（无论提交 `cos_access` 还是仅提交 `web_access`）与 `PUT /workspaces/{id}/settings` 均整体返回 `403 external_member_forbidden` 且不改写数据库记录**；
       - **外部属主管理自己非 Home 工作区**：`/sharing` GET 返回 `can_edit: true`、`can_edit_web_access: true`、`can_edit_cos_access: false`（`cos_access` 锁定 `"hidden"`），`/settings` GET 返回 `can_edit: false`、`can_edit_team_memory: false`；`PUT /sharing` 允许仅修改 `web_access`（返回 `200`），携带 `cos_access` 返回 `403 external_member_forbidden`；`PUT /settings` 返回 `403 external_member_forbidden`；
       - **管理员管理他人工作区**：目标属主为 `internal`/`admin` 时，`PUT /sharing` 允许修改 `web_access` 与 `cos_access`，`PUT /settings` 维持仅属主可改（返回 `403`）；目标属主为 `external` 时，隐藏或 Home 返回 `404`，可见非 Home 返回 `403`。

5. **邀请与团队渠道管理写接口（已决 11）**
   - `src/dalton/invites.py:229-246,467-530`：
     - `local` 模式：管理员可创建 `admin`/`internal`/`external` 三档邀请；内部成员仅可创建 `external` 邀请（限 20 条）；外部成员返回 `403 external_member_forbidden`；内部成员请求创建非 `external` 邀请返回 `403 admin_required`；
     - `supabase` 模式：管理员与内部成员固定创建 `external` 邀请；外部成员返回 `403 external_member_forbidden`；
     - 单人 `oss` 模式：返回 `403 invites_disabled_in_oss`。
   - `src/dalton/app/channels_router.py:977-1039`：Discord 频道与微信机器人管理接口统一返回 `403 admin_required`。

---

### 2.3 发布弹窗、停止分享弹窗与会话分享面板改造

#### 1. 发布与停止分享弹窗（`PublishDialog.tsx`）

- **统一身份订阅、跨实例共享 `cancelQueries` + `fetchQuery` 强制重验与 60s 静默保鲜**：调用 `useCallerIdentity({ revalidateOnOpen: true, open })`。打开弹窗、窗口回前台、写接口返回 `403` 时调用 `revalidate()`（先同步置共享 `entry.isConfirming = true` 并通知同 `queryKey` 全部实例，再执行 `cancelQueries` + `fetchQuery({ queryFn: fetchTeamCredentialsStatus, staleTime: 0 })` 中止在途 HTTP 请求并绕过 5 分钟 `staleTime` 强制发新请求）；弹窗持续打开期间每 60 秒后台静默刷新（不修改共享 `entry.isConfirming`）。
- **未知态与强制重验期间**：
  - `status !== 'ready' || isConfirming` 时：停用发布提交、受众选择、成员选择、自动更新开关与复制链接按钮，顶部显示权限确认状态条（错误态带「重试」按钮调用 `revalidate()`）；
  - 若当前文件已有公开分享（`shared === true` 或 `existingMonitor?.visibility === 'public'`），**「停止分享」按钮在未知态与强制重验期间仍保持可点**。
- **普通态（非 `unshareOnly`）的链接与复制门控（已决 9 & 4）**：
  - 链接展示框、`copyCurrentLink()`（`:801`）与底栏监控外链（`:1224`）统一门控为 `identity.canActAsOwner && !identity.isConfirming`（平台模式 `isPlatform === true` 照常放行）；对外部成员与未知态隐藏链接框与监控外链，并在 `copyCurrentLink()` 首行同步调用 **`const latest = identity.getLatestIdentity(); if (latest.isConfirming || latest.status !== 'ready' || !latest.canActAsOwner || latest.isExternal) return;`**；
  - 全部写请求异常统一通过 `formatApiErrorDetail(err)` 弹出具体原因，并在 `403` 时调用 `identity.revalidate()`。
- **打开后身份降级与防回弹处理（已决 4 & 1）**：
  - 监听 `identity.tier` 与 `identity.status`：
    - 变为 `'external'` 或 `'unknown'`：立即关闭 `publicConfirmOpen`，清除 `membersDraft` 与非 `'own'` 的 `visibilityDraft`；若原面板存在且 `existingMonitor.visibility !== 'own'`（降为外部成员），展示提示「当前身份为外部成员，重新发布前请先将可见性降为『仅自己』」，并提供独立的「降为仅自己」操作（调用 `PATCH /dalton/monitors/{slug}`，不切版本），未降级前停用重新发布按钮；
    - 从 `'admin'` 降为 `'internal'`：立即关闭 `publicConfirmOpen`，置 `publicReselectRequired = true` 且清空 `visibilityDraft`，使 `effectiveVisibility` 解析为 `null`（**阻断 `:610` 的 `visibilityDraft ?? existingMonitor?.visibility` 回弹选中 `'public'`**），展示「管理员权限已变更，请重新选择可见范围」，在用户显式点击有效可见性前停用提交按钮。
  - `send()`、`executeMonitorPublish()`、`chooseAudience()`、`toggleKeep()` 首行同步调用 **`const latest = identity.getLatestIdentity()`**，同时核对 **`!latest.isConfirming && latest.status === 'ready'`** 以及对应动作所需的最新角色权限（如 `public` 可见性或裸发布要求 `latest.canPublishPublic`，非 `own` 监控发布、受众修改、自动更新开启要求 `!latest.isExternal && latest.canActAsOwner`，外部成员已有非 `own` 面板未独立降级前阻断 `executeMonitorPublish()`）。
- **`unshareOnly` 模式统一走 Batch 1 文件撤销接口 + 历史公开监控入口（已决 2）**：
  - **停止动作唯一调用 `POST /api/v1/artifacts/{artifact_id}/unshare`**（`unshare.mutate(artifact.artifact_id)`）：
    - 由 Batch 1 后端在同一数据库事务内（`is_dalton_enabled()` 为真时）按 `lock_owned(share_links) → dalton_monitors FOR UPDATE → unshare_in_cur` 原子完成公开监控降级为 `own` 与文件撤销；
    - **彻底解决管理员接管面板后的撤销权限问题**：当管理员通过重发接管了公开监控（`monitor.owner_user_id` 变为管理员，`service.py:662-666`）后，原文件属主点击「停止分享」走 `POST /artifacts/{id}/unshare`（校验工作区/文件归属 `reached()`）照常成功撤销并将监控降为 `own`，不会撞上 `PATCH /monitors/{slug}` 的 `can_manage_monitor_fields`（`permissions.py:43-59`）`403`；
    - `unshareOnly` 仍启用 `existingMonitorQuery` 用于状态说明展示，但停止按钮不再因监控归属或 `existingMonitorQuery` 失败而阻断撤销；撤销成功后同时失效 `artifacts`、`shareLinks` 与 `daltonMonitors` 三组缓存。
  - **历史状态入口补全**：文件头（`FileHeaderActions.tsx:467-478`）除 `unshareRecord?.shared` 外，同步识别 `existingMonitor?.visibility === 'public'`；工作区分享列表经 `database/share_links.py:218-227` 的 `list_shared`（复用 `SELECT {_COLUMNS}`）自动包含 `shared_at IS NULL` 但监控仍为 `public` 的文件记录，使文件头与分享列表均能一键停止历史残留的公开监控。
  - **`unshareOnly` 界面收敛**：保留文件标题、当前分享状态、「停止分享」按钮与关闭按钮；隐藏分享地址与复制按钮（`:867-895`）、`AudiencePicker`（`:1084-1092`）、`LibraryPublishRow`（`:1214-1220`）、底栏监控外链（`:1224-1234`）、自动更新开关、版本列表与发布表单。

#### 2. 会话分享按钮与面板（`ShareButton.tsx` / `ThreadSharePanel.tsx`）

- **订阅统一身份与打开重验**：`ShareButton` 订阅 `useCallerIdentity()`；`ThreadSharePanel` 调用 `useCallerIdentity({ revalidateOnOpen: true, open })`。
- **停止分享载荷与最新身份快照同步核对**：放宽 `saveThreadSharing`（`useThreadShareStatus.ts:33`）参数类型为 `{ is_shared: boolean; permissions?: ThreadSharePermissions }`；`handleToggleShare(false)` **仅发送 `{ is_shared: false }`**；`handleToggleShare(true)`、受众/权限修改与复制回调（含 `SharedInWorkspace` 复制回调）首行同步调用 **`const latest = identity.getLatestIdentity()`** 并核对 **`!latest.isConfirming && latest.status === 'ready' && latest.canActAsOwner && !latest.isExternal`**（修改 `allow_files`/`allow_download` 额外核对 `latest.isAdmin`）。
- **未知态与外部成员视图**：
  - `ShareButton`：未知态下若 `isShared === true` 或已有 `sharedLinks`（含 `list_shared` 返回的历史公开监控项）保持可点以供停止分享，否则置灰提示；`identity.isExternal` 仅在 `isShared || sharedLinks.length > 0` 时渲染。
  - `ThreadSharePanel`（`identity.isExternal`）：
    - `isShared === true`：显示状态标题、说明文案与**「停止分享」控件**（点击仅发 `{ is_shared: false }`）；隐藏链接框、复制按钮（`:272-294`）、`AudiencePicker`（`:297`）、`allow_files`/`allow_download` 复选框（`:301-331`）与 `LibraryPublishRow`（`:340-344`）。
    - `isShared === false`：显示只读说明文案，隐藏开启开关与受众选择器。
  - `ThreadSharePanel`（`status !== 'ready' || isConfirming`）：停用开启开关、受众选择器、权限复选框与复制链接按钮（错误态附「重试」按钮）；当 `isShared === true` 时保持停止分享可点。
  - `SharedInWorkspace`（`:28-95`）：对外部成员与未知态隐藏复制链接按钮（`:71-80`），保留文件名、分享日期与「停止」按钮；**停止动作保持与 `:44-46` 及 `PublishDialog`（`unshareOnly`）完全一致的单一契约**：
    - 对所有文件条目（`link.kind === 'file' && link.artifact_id`，含 `shared_at IS NULL` 的历史公开监控项）：**唯一调用 `POST /api/v1/artifacts/{artifact_id}/unshare`**（`unshare.mutate(link.artifact_id)`），并在 `unshare` 成功后同步失效 **`artifacts`、`shareLinks` 与 `daltonMonitors` 三组缓存**；
    - `PATCH /api/v1/workspaces/{id}/share-links/{code}`（`patch.mutate`）在前端仅用于非文件（`app`）条目（后端其文件分支仍保留同事务监控降级逻辑以兜底直接 API 调用）。
  - 错误提示：`save()` 与 `stopSharing()` 捕获异常时通过 `formatApiErrorDetail(err)` 弹出 toast，收到 `403` 同步触发 `identity.revalidate()`。

---

### 2.4 `docLocation()` 三态识别规则、原始属性存在性互斥校验与预览框内跳转状态机

#### 1. 桥端 `docLocation()` 左锚定完整前缀与三态上报（`src/server/preview_proxy/bridge/core.js:24-35,119-136`）

枚举服务端全部六条带路径文件托管路由（`DOC_PATH`）与无路径版本根入口路由（`DOC_ROOT`），从 `location.pathname` 行首（`^`）按完整前缀匹配，显式区分**合法文档路径**、**确认的无路径版本根入口**与**非法/无法识别的路径**：

```js
// A. 6 条带工作区相对路径的服务端托管前缀（左锚定）：
// 1. /api/v1/wsfiles/g/<grant>/<path>                         属主实时预览
// 2. /api/v1/public/shared/<token>/files/serve/<path>         分享会话实时预览
// 3. /api/v1/artifacts/g/<grant>/v/<seq>/<path>               API 源已发布制品版本
// 4. /api/v1/dalton/monitors/(g|public)/<id>/v/<seq>/<path>   监控面板授权/公开版本
// 5. /g/<grant>/v/<seq>/<path>                                独立制品主机授权版本
// 6. /v/<seq>/<path>                                          独立制品主机公开版本
var DOC_PATH = /^\/(?:api\/v1\/(?:wsfiles\/g\/[^\/]+|public\/shared\/[^\/]+\/files\/serve|artifacts\/g\/[^\/]+\/v\/[0-9]+|dalton\/monitors\/(?:g|public)\/[^\/]+\/v\/[0-9]+)|g\/[^\/]+\/v\/[0-9]+|v\/[0-9]+)\/(.+)$/;

// B. 确认的无路径版本根入口（左锚定，例如 MonitorViewer.tsx:147 的 `${prefix}/`）：
var DOC_ROOT = /^\/(?:(?:api\/v1\/(?:artifacts\/g\/[^\/]+|dalton\/monitors\/(?:g|public)\/[^\/]+)|g\/[^\/]+)\/)?v\/[0-9]+\/?$/;
```

桥端 `docLocation()` 与 `hello()`（`core.js:24-35,125-135`）以及父端 `pageProtocol.ts:265-267,406-409` 的对称校验规则（已决 10）：

1. **桥端 `core.js:docLocation()` 返回三态**：
   - **命中 `DOC_PATH`**：对 `match[1]` 执行 `decodeURIComponent`；与服务端 `serve.py:139`（`_has_traversal`）对齐，检查不含反斜线 `\`、不含 ASCII 控制字符 `[\u0000-\u001f\u007f]`、按 `/` 分割后任意段均非 `..`、剥离前导 `/` 后非空且长度 `<= MAX_DOC_PATH`（1024）：
     - 校验全部通过 → 返回 `{ kind: 'path', path }` → `hello()` 发送 `{ type: 'page:ready', v: VERSION, path }`；
     - 解码抛错或任一安全/长度校验失败 → 返回 `{ kind: 'invalid' }` → `hello()` 发送 `{ type: 'page:ready', v: VERSION, invalidPath: true }`（**绝不发送 `root: true`，也不静默省略为无路径**）。
   - **未命中 `DOC_PATH`，但命中 `DOC_ROOT`**（无路径版本根入口）：
     - 返回 `{ kind: 'root' }` → `hello()` 发送 `{ type: 'page:ready', v: VERSION, root: true }`。
   - **既未命中 `DOC_PATH` 也未命中 `DOC_ROOT`**（无法识别的 URL）：
     - 返回 `{ kind: 'invalid' }` → `hello()` 发送 `{ type: 'page:ready', v: VERSION, invalidPath: true }`。
2. **父端 `pageProtocol.ts:265-267,406-409` 移除 `lenient(docPath)` 并在原始消息对象上检查自有属性存在性**：
   - `docPath` schema（`:265-267`）补齐反斜线 `\`、控制字符、`..` 路径段与剥离前导 `/` 后非空校验；
   - **`page:ready` 解析移除 `lenient(docPath)`，并在 Zod 剥离 `undefined` 可选属性之前直接对原始消息对象 `raw` 检查自有属性**：因为 `postMessage` 的结构化克隆会保留显式传入的 `{ path: undefined, root: true }` 自有键，若仅用 `m.path !== undefined` 检查会将该冲突消息漏放为 `'root'`。因此在 `page:ready` 分支中对原始对象 `raw` 执行 `const hasOwn = (k: string) => Object.prototype.hasOwnProperty.call(raw, k); const hasPath = hasOwn('path'); const hasRoot = hasOwn('root'); const hasInvalidPath = hasOwn('invalidPath');`，并**严格按以下四步前置互斥顺序**产出 `{ type: 'page:ready', path: string | null, pathState: 'path' | 'root' | 'invalid' | 'unrecognized' }`：
     1. **第一步（基于自有属性存在性前置拒绝非法布尔类型、显式 `invalidPath` 与多字段冲突）**：
        - 若 `(hasRoot && typeof raw.root !== 'boolean')` 或 `(hasInvalidPath && typeof raw.invalidPath !== 'boolean')`；
        - 或 `raw.invalidPath === true`；
        - 或同时存在多于一种位置自有属性（即 `[hasPath, hasRoot, hasInvalidPath].filter(Boolean).length > 1`，显式覆盖 **`{ path: undefined, root: true }`**、`{ path: null, root: true }`、`{ path: 'a.html', root: true }`、`{ path: 'a.html', invalidPath: true }`、`{ root: true, invalidPath: true }` 等任何组合）：
        - **一律直接产出 `{ type: 'page:ready', path: null, pathState: 'invalid' }`**；
     2. **第二步（仅存在 `path` 自有属性时严格校验 `docPath`）**：
        - 当 `hasPath && !hasRoot && !hasInvalidPath` 时：使用严格 `docPath.safeParse(raw.path)` 校验——仅当 `raw.path` 为合法非空相对路径字符串时产出 `{ type: 'page:ready', path: parsed.data, pathState: 'path' }`；若校验失败（含 **`{ path: undefined }`**、`{ path: null }`、`{ path: '' }`、含 `..`/`\`/控制字符/超长）则产出 **`{ type: 'page:ready', path: null, pathState: 'invalid' }`**；
     3. **第三步（仅存在 `root: true` 自有属性时确认为无路径版本根入口）**：
        - 当 `!hasPath && !hasInvalidPath && hasRoot && raw.root === true` 时：产出 **`{ type: 'page:ready', path: null, pathState: 'root' }`**；
     4. **第四步（其余未携带 `path` 且 `root !== true`、`invalidPath !== true` 的情形）**：
        - 例如 `{ type: 'page:ready', v: 1 }` 或 `{ type: 'page:ready', v: 1, root: false }`：产出 **`{ type: 'page:ready', path: null, pathState: 'unrecognized' }`**。
3. **资源加载上报隔离**：`loads.js:73` 继续由 `ownerView` 门控，分享会话下仅在 `page:ready` 上报位置，不上报 `page:loads`。

#### 2. 预览框内跳转统一文档状态机、属主帧透传与 `bridged_root`（已决 10）

在 `BridgedFrame.tsx` / `ArtifactFrames.tsx` / `HtmlViewer.tsx` / `FileViewer.tsx` / `FilePanel.tsx` / `FileHeaderActions.tsx` 建立统一的文档状态模型：

```ts
export type HtmlFrameDocState =
  | { kind: 'pending' }                                // 等待首帧或跳转后的 page:ready
  | { kind: 'current'; path: string }                  // pathState === 'path' 且匹配原标签文档 A
  | { kind: 'bridged_root' }                           // 仅当 pathState === 'root'（确认的无路径监控/制品版本根入口）
  | { kind: 'linked'; path: string }                   // pathState === 'path' 且跳转到另一文档 B（isLinkedHtmlDoc = true）
  | { kind: 'unbridged'; currentUrl: string | null };  // 超时未收到 page:ready，或收到 pathState === 'invalid' / 'unrecognized'（currentUrl 置为 null）
```

| 状态 | 触发条件 | 桌面端与移动端工具栏统一行为 |
|---|---|---|
| **`pending`** | 标签刚切换或触发新一次 `iframe onLoad`（在 `UNBRIDGED_GRACE_MS` 宽限期内） | 停用依赖当前文档身份的写/导出动作，等待确认后切换为终态 |
| **`current`（文档 A）** | 收到 `page:ready`、`pathState === 'path'` 且 `artifactPathKey(path, root) === tabKey` | 正常展示文档 A 的全部允许操作（属主/外部/分享各按自身权限） |
| **`bridged_root`** | 收到 `page:ready` 且 **`pathState === 'root'`**（仅限无冲突自有属性的无路径版本根入口，如 `MonitorViewer.tsx:147`） | 判定为合法根页（`isLinkedHtmlDoc = false`），保留该根视图原有主操作 |
| **`linked`（文档 B）** | 收到 `page:ready`、`pathState === 'path'` 且 `artifactPathKey(path, root) !== tabKey`（`isLinkedHtmlDoc = true`） | 双端统一隐藏文档 A 的文件级操作（源码切换、下载、PDF、编辑、停止分享、发布/更新/版本），提供**「在新标签打开此文档（B）」**（调用 `onOpenDocAsTab(path)`）；属主面板保留对文档 B 的评论操作 |
| **`unbridged`（非桥接页 / 非法路径 / 冲突字段 / 未识别路径）** | ① `onLoad` 后超过 `UNBRIDGED_GRACE_MS` 未收到 `page:ready`；或 ② 收到 `page:ready` 但 **`pathState === 'invalid' \| 'unrecognized'`** | **立即清零旧 `bridge.path`，并将文档状态设为 `{ kind: 'unbridged', currentUrl: null }`**；双端统一隐藏文档 A 的全部文件级操作，且因 `currentUrl === null` **同步隐藏「在新标签打开」按钮**（绝不回退使用原标签 A `selectedFile` 的 `openUrl`） |

关键调用链打通与竞态防护：

1. **全帧透传 `onShownDocChange`**：将 `FileViewer.tsx:225`、`HtmlViewer.tsx` 与属主视图 `ArtifactFrames.tsx:132-170`（同时覆盖实时 `BridgedFrame` `:138` 与版本 `BridgedFrame` `:164`，按当前展示层 `version === null` 选择活跃帧上报）统一接入 `onShownDocChange(state: HtmlFrameDocState)`，使属主实时/版本预览与只读预览享有完全一致的工具栏联动。
2. **`onBridge` 携带帧标识**：`BridgedFrame` 回调改为 `onBridge(bridge, frameKey)`（并在 `PageBridge` 携带只读 `frameKey: string | null`）；`useLiveFrame` 仅当 `frameKey === source?.key` 时更新活跃桥接态，标签切换瞬间即返回 `{ kind: 'pending' }`，消除旧帧路径串写新标签。
3. **超时回调与 `invalid`/`unrecognized` 同步清零**：`BridgedFrame` 为每次 `onFrameLoad` 递增 `navSeqRef.current`；超时触发或收到 `page:ready` 的 `pathState === 'invalid' | 'unrecognized'` 时，均清零 `bridge.path = null` 并上报 `{ kind: 'unbridged', currentUrl: null }`。
4. **脚本跳转（`location.href`）口径**：普通 `<a href>` 与 GET 表单由 `links.js:53-69,197-211` 自动携带 `?inject=view`；分享会话中页面脚本直接改写 `location.href` 丢掉 `?inject=view` 时，本轮保持 `share_files.py` 不变，前端按上述超时机制平稳进入 `{ kind: 'unbridged', currentUrl: null }` 并隐藏 A 的全部文件级操作与新标签入口（补注入方案记入第 6 节后续事项）。

---

### 2.5 四种部署形态对照（平台 / Everflow / 核心安装 / 单人回环）

严格遵循 [standalone-core.md](./standalone-core.md) 的架构边界：部署形态由 `src.dalton.profile` / `brand.ts` 判定，可选能力由 `src.dalton.capabilities` / `useCapabilities()` 判定，成员等级由 `get_user_tier` / `useCallerIdentity()` 判定。

| 维度 | 上游平台模式（`HOST_MODE=platform`） | Everflow（`DALTON_PROFILE=everflow`） | 核心安装（`DALTON_PROFILE=core`） | 单人回环（`DALTON_AUTH_MODE=oss`） |
|---|---|---|---|---|
| **运行条件识别** | 前端 `!isDaltonBuild \|\| isPlatformMode`；后端 `HOST_MODE != "oss"`（当 `DALTON_ENABLED=false` 时不访问 `dalton_monitors`） | `HOST_MODE=oss` + `DALTON_AUTH_MODE=supabase` | `HOST_MODE=oss` + `DALTON_AUTH_MODE=local` | 后端 `is_dalton_single_user_oss_mode()`；前端收到 `/credentials/status` `200` 且 `auth_mode: "oss"` |
| **`caller_tier` 与身份读取** | 跳过 `/credentials/status`；直接 `ready`（`isPlatform: true`, `canActAsOwner: true`, `canPublishPublic: true`） | `DALTON_PM_USER_IDS` → `admin`；`DALTON_ALLOWED_EMAILS` → `internal`；其余 → `external` | 实时查 `dalton_user_roles(user_id, tier)`（监控写入事务同游标 `FOR SHARE`）；缺行或异常 fail-closed 为 `external`；前端漏配 `VITE_DALTON_AUTH` 时请求收 `401` 进入 `error`/`unknown` 停用特权 | 后端 `get_user_tier()` 与 `classify_owner_tier()` 同步返回 `"admin"`；前端凭服务端 `200` + `auth_mode: "oss"` 确认 |
| **团队邀请（`canInvite`）** | `false`（走平台组织体系） | `admin`/`internal` 邀 `external`；`external` 返回 403 | `admin` 邀三档；`internal` 仅邀 `external`（限 20）；`external` 返回 403 | **`canInvite: false`**；前端隐藏入口，后端返回 `403 invites_disabled_in_oss` |
| **同事工作区与研究总监共享** | 平台组织共享工作区（走 `shared_workspaces` 体系） | `colleague_sharing` 默认开启；能力 + `isInternal` 双门控 | 默认关闭（共享设置读接口返回 200 中性结果；`GET /workspaces/{id}/sharing` 仅对属主返回 200 中性结果，非属主返回 404；写接口 PUT 返回 404）；显式开启后按 `dalton_user_roles` 隔离 | 能力开启时单人可读写；关闭时只读 GET 返回 200 中性结果，写接口 PUT 返回 404 |
| **团队记忆（Home / 工作区）** | 不适用 | `conversation_archive` 默认开启；能力 + `isInternal` 双门控 | 归档默认关闭：`GET/PUT /user-settings` 归属 `CONVERSATION_ARCHIVE`（关闭时返回 404）；`GET/PUT /workspaces/{id}/settings` 为核心接口（`CORE_CAPABILITY`），关闭归档时符合权限的属主仍可读取与保存，归档运行时保持停用；开启归档后即便 `colleague_sharing` 关闭，桌面与手机端均可独立打开弹窗并保存 | 工作区设置作为核心接口单人可读写，归档运行时按能力开关启停；Home `/user-settings` 能力开启时单人可读写、关闭时 404 |
| **监控面板、文件受众、保留、评论与会话分享** | Dalton 角色守卫全部跳过，完整保留平台共享工作区、组织受众与评论行为 | 按 `supabase` 名单三层隔离 | 按 `dalton_user_roles` 执行完全相同的三层隔离 | 单人视为 `admin`（仍受 `artifact_publishing.enabled` 约束） |
| **组织受众（`organization`）与组织库** | 满足平台 `org_member()` 前置条件时 `organization` 返回 `200`；个人工作区具备 `PUBLIC_LINKS` 策略时切 `public` 返回 `200`，共享工作区（`is_holder`）切 `public` 返回 `403 shared_members_only`（`share_audience.py:98-110`） | `HOST_MODE=oss` 下 `teams_door_open()`（调用 `features.py:211`，定义 `membership.py:40`）为假，选 `organization` 返回 `403 audience_unavailable`（`share_audience.py:119-126`），组织库不启用 | 同左（选 `organization` 返回 `403 audience_unavailable`，组织库不启用） | 不可用（选 `organization` 返回 `403 audience_unavailable`） |

---

## 3. 改动清单与分批实施计划

> **上游标注与检查门禁**：上游源码改动均标注已登记白名单的 `DALTON-HOOK(<id>)` 注释（复用 `external-share-guard`、`colleague-dispatch`、`workspace-sharing-settings`、`team-sharing`、`settings-section`、`settings-reorg`、`mobile-web`、`monitors`、`company-automations`）。`src/dalton/`、`tests/unit/dalton/`、`tests/integration/dalton/`、`tests/standalone/` 已被 `scripts/dalton/dalton_paths.txt` 目录前缀覆盖（若新增目录不在前缀内则同步追加至 `dalton_paths.txt`）；**所有新增 Dalton 前端源码及新增测试文件均须同步登记到 `scripts/dalton/dalton_paths.txt`**。每批合入前执行 `bash scripts/dalton/check_hooks.sh --strict --audit` 校验新增上游路径与未标注改动。

### 3.1 Batch 1：服务端权限闭环、能力注册表、一致锁序原子停止分享与单人 `oss` 贯通（约 3 人日）

| 文件路径 | 归属与登记 | `DALTON-HOOK` 要求 | 主要改动点 |
|---|---|---|---|
| `src/dalton/auth_guards.py` | 新建（`src/dalton/` 已覆盖） | 无需 Hook | 定义统一 403 错误码与 `require_internal_member` / `require_admin_member`（仅在 `is_dalton_multiuser_auth_enabled()` 下生效，平台模式自动放行） |
| `src/dalton/settings.py`<br>`src/dalton/access_tiers.py` | Dalton 独占 | 无需 Hook | 新增 `is_dalton_single_user_oss_mode()`；`get_user_tier()` 与 `classify_owner_tier()` 在单人 `oss` 下均短路返回 `"admin"` |
| `src/dalton/capabilities.py` | Dalton 独占 | 无需 Hook | 与 #504 对齐：`get_workspace_settings_endpoint`、`update_workspace_settings_endpoint` 与只读共享 GET 保持 `CORE_CAPABILITY`；`update_workspace_sharing_endpoint`、`update_user_sharing_settings_endpoint` 保持 `CapabilityId.COLLEAGUE_SHARING`；`get_user_settings_endpoint`、`update_user_settings_endpoint` 设为 `CapabilityId.CONVERSATION_ARCHIVE` |
| `src/dalton/app/router.py` | Dalton 独占 | 无需 Hook | ① `/credentials/status` 新增 `auth_mode: get_dalton_auth_mode()`，单人 `oss` 返回 `caller_tier: "admin"` 与 `can_invite: False`；② `/workspaces/team`、`/sharing/user-settings` 与 `/user-settings` 统一改用 `is_dalton_auth_enabled()`（共享关闭时只读 GET 返回 200 中性结果并保留真实 `caller_tier`）；③ `/user-settings` 挂 `CONVERSATION_ARCHIVE` 守卫，`/workspaces/{id}/settings` 保持核心接口（关闭归档时符合权限的属主仍可读写，归档运行时停用）；④ `/workspaces/{id}/sharing` 与 `/settings` 按分模式两阶段校验（阶段一：`local` 缺行或 `is_team_member_user` 查库异常在 `require_team_reader(uid)` 返 `403`；`supabase` 非成员返 `403`、`user_db.get_user` 异常返 `503`；阶段二：通过成员校验后属主 `owner_tier == "unknown"` 时 GET 返回 `200` + `can_edit: false` 及全子项 `false`，**`PUT /sharing`（含仅传 `web_access`）与 `PUT /settings` 均整体返回 `403 external_member_forbidden` 且不写库**；外部属主 GET 返回分字段编辑标识 `can_edit_web_access: true`、`can_edit_cos_access: false`、`can_edit_team_memory: false`；非属主不可见及共享关闭时访问外部属主工作区均 `404`）；⑤ 统一 403 错误体 |
| `src/dalton/invites.py`<br>`src/dalton/app/channels_router.py` | Dalton 独占 | 无需 Hook | 邀请在单人 `oss` 下返回 `403 invites_disabled_in_oss`；邀请、Discord 与微信管理 403 统一为 `{ detail: { code, message } }` |
| `src/dalton/monitors/permissions.py`<br>`src/dalton/monitors/service.py`<br>`src/dalton/monitors/db.py`<br>`src/dalton/tools/monitor_tools.py` | Dalton 独占 | 无需 Hook | ① 新增 `require_monitor_publish_visibility()` 覆盖 `POST /monitors`、HTTP 手动重发与回合后自动更新；② `publish_workspace_monitor` 与 **`commit_monitor_publish_tx`**（`db.py:697-780`）在锁定 `share_links` 与 `dalton_monitors FOR UPDATE` 后，于同一事务游标内同时核对实时角色（`local` 下 **`SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE`**，阻塞 `set_user_role` 的 `FOR NO KEY UPDATE`）、原可见性 `existing["visibility"]`（外部属主已有非 `own` 面板即使传 `own` 也抛 `403`，要求先经 `PATCH` 独立降级）与拟写 `visibility`；③ **`commit_monitor_version_tx` → `_cut_and_finalize_monitor_in_cur`**（`db.py:906-1055`）对工作区监控按同游标 `FOR SHARE` 实时角色复核 `live_visibility`，**保留数据任务 `publish_data_job_monitor`（`service.py:1084-1094`）既有 `_verify_job_public_in_tx` 公开审批校验与 `COMPANY_SYSTEM_USER_ID` 语义不变**；④ `get_monitor_by_artifact` 放行属主本人读取元数据并返回 `can_manage=True`；⑤ 外部属主非 `own` 面板暂停自动更新并写 `last_publish_error`；⑥ 统一 403 错误体 |
| `src/server/app/artifacts/records.py`<br>`src/server/app/artifacts/comments.py`<br>`src/server/app/share_links.py`<br>`src/server/database/artifacts/records.py`<br>`src/server/database/share_links.py` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)` / `DALTON-HOOK(monitors)` | ① `POST /artifacts/{id}/unshare`（`records.py:671`）与 `PATCH /workspaces/{id}/share-links/{code}`（`share_links.py:203`）以 `is_dalton_enabled()` 门控：未启用时走原 `artifacts_db.unshare()`，启用时统一按 **`lock_owned(share_links) → SELECT dalton_monitors FOR UPDATE → unshare_in_cur`** 一致锁序同事务原子降级关联 `visibility == "public"` 监控为 `own`、递增 `acl_revision` 并撤销文件分享；② `src/server/database/share_links.py:218-227` 的 `list_shared` 在 `is_dalton_enabled()` 为真时**直接复用 `SELECT {_COLUMNS}`**，配合 `LEFT JOIN dalton_monitors m ON m.artifact_id = l.artifact_id AND m.visibility = 'public'` 纳入 `shared_at IS NULL` 的历史公开监控文件并按 `COALESCE(l.shared_at, m.updated_at, l.created_at) DESC` 排序（私人 `app` 链接不进入该列表；未启用时保持原 SQL）；③ `PUT /audience` 补齐发布总开关、内部成员与公开管理员校验；④ `PUT /kept` 在 `kept=True` 时**先调 `require_internal_member` 再在 `artifact.shared` 时调 `require_admin_member`**，`kept=False` 保持开放清理；⑤ `POST/PATCH /comments*` 与 `PUT/DELETE /binding` 接入 `require_internal_member`；⑥ 统一 403 错误体 |
| `src/server/app/threads/crud.py`<br>`src/server/app/threads/messaging.py`<br>`src/server/app/threads/sharing.py`<br>`src/dalton/sharing.py`<br>`src/server/utils/api.py`<br>`src/server/services/shared_workspaces/access.py` | **上游文件**（`src/dalton/sharing.py` 为 Dalton 独占） | 需 `DALTON-HOOK(workspace-sharing-settings)` / `DALTON-HOOK(team-sharing)` / `DALTON-HOOK(external-share-guard)` / `DALTON-HOOK(colleague-dispatch)` | ① `crud.py:317`、`messaging.py:1453`、**`messaging.py:1613`（`GET /threads/{id}/status`）**、`threads/sharing.py:61,88,160,204,272`、`api.py:487`、`access.py:302` 共 **10 处**全部统一改用 `is_dalton_multiuser_auth_enabled()`；② **改造 `src/dalton/sharing.py:_guard_thread_not_foreign_home_stand_in`（`:1043-1052`）**：对他人 Home 替身抛 `404`；对他人个人 Home 先调 `classify_owner_tier`——外部/`unknown` 属主抛 `404`，内部/管理员属主抛 `403`；③ **从函数顶部 `:1392` 起调整 `require_thread_access`**：第一步最顶层拦截外部成员跨区会话抛 `404`；第二步重写 `if not _sharing_auth_enabled():` 早退分支（自有双属主返 `200`，否则先调 `_guard_thread_not_foreign_home_stand_in` 区分替身 `404`/外部个人 Home `404`/内部个人 Home `403`，其余跨区非双属主统一抛 `403`）；第三步开启分支仅将 `:1451` 发起人 `read`/`web` 快捷路径改为双开关门控并复用改造后的守卫，**完整保留 `:1477-1571` 既有写权限拦截（含 `:1533-1545` 工作区属主对同事发起会话 `need=="write"` 抛 `403` 与 `:1565-1569` 非会话创建者 `need=="write"` 抛 `403`）**；④ 在 `threads/sharing.py:191` 之前确定 `effective_req_permissions`，外部成员 `is_shared: false` 置为 `None`；⑤ 统一 403 错误体 |
| `tests/unit/dalton/test_external_member_write_guards.py`<br>`tests/integration/dalton/test_external_member_permissions_db.py`<br>`tests/standalone/checks/b4_local_auth.py` | 新建/已有（登记 `scripts/dalton/dalton_paths.txt`） | 登记 `dalton_paths.txt` | 服务端单测矩阵、**分别针对 `commit_monitor_publish_tx` 与 `commit_monitor_version_tx` 两条写入口的真实 PostgreSQL 双连接 `FOR SHARE` 双向阻塞集成测试（`-m integration`）**、以及核心安装真实容器验收 |

### 3.2 Batch 2：前端统一身份 Hook、错误解析、工作区弹窗解耦与发布/分享面板收敛（约 3–4 人日）

| 文件路径 | 归属与登记 | `DALTON-HOOK` 要求 | 主要改动点 |
|---|---|---|---|
| `web/src/hooks/useCallerIdentity.ts`<br>`web/src/hooks/__tests__/useCallerIdentity.test.tsx` | **新建（均登记 `scripts/dalton/dalton_paths.txt`）** | 登记 `dalton_paths.txt` | 统一身份 Hook、平台模式短路、服务端 `auth_mode` 采信（漏配 `VITE_DALTON_AUTH` 收 `401` 进入 `error`/`unknown` 停用特权）、**复用 `fetchTeamCredentialsStatus({ signal })` 共享 `queryFn`**、**以 `WeakMap<QueryClient, Map<string, RevalidationState>>` + `useSyncExternalStore` 按 `(queryClient, queryKey)` 跨实例共享 `activeSeq` 与 `isConfirming`**、**暴露同步方法 `getLatestIdentity()`（实时读取 `queryClient.getQueryState(queryKey)` 与共享 `entry.isConfirming` 计算最新身份与角色权限快照，`isConfirmingNow()` 为其别名）**、强制重验 `revalidate()`（`cancelQueries` 中止在途 HTTP 请求 + `fetchQuery({ queryFn: fetchTeamCredentialsStatus, staleTime: 0 })` 发新请求）、60s 后台静默保鲜与完整单测 |
| `web/src/hooks/useTeamCredentials.ts` | Dalton 独占（已在 `dalton_paths.txt`） | 无需 Hook | 扩展类型读取服务端 `auth_mode`；**抽出并导出透传 `AbortSignal` 的共享查询函数 `fetchTeamCredentialsStatus({ signal })`（调用 `api.get('/api/v1/dalton/credentials/status', { signal })`）**，与 `useCallerIdentity()` 严格共用同一 `queryKey` 与同一 `queryFn` |
| `web/src/api/errors.ts` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)`（若有新增辅助函数） | 复用 `apiErrorCoded()` / `apiErrorDetailMessage()` / `formatApiErrorDetail()`，按需补充 `isForbiddenError()` 触发身份重验 |
| `web/src/pages/Settings/components/SettingsRail.tsx`<br>`web/src/pages/Settings/sections.ts`<br>`web/src/pages/Settings/Settings.tsx` | **上游文件** | 需 `DALTON-HOOK(settings-section)` / `DALTON-HOOK(settings-reorg)` | 统一改用 `useCallerIdentity()`；移除 `/channels/bindings.user_tier` 回退；`useUserCosSharingQuery` 门控为 `identity.isInternal && hasCapability('colleague_sharing')` |
| `web/src/pages/Settings/panels/dalton/*.tsx` | Dalton 独占 | 无需 Hook | 研究总监卡、团队记忆卡、团队运维/Discord/邀请等全部迁至 `useCallerIdentity()` |
| `web/src/pages/Automations/Automations.tsx` | **上游文件** | 需 `DALTON-HOOK(company-automations)` | 将 `:196` 的 `useTeamCredentials()` 迁至 `useCallerIdentity()` |
| `web/src/components/CommandPalette/useVerbs.ts`<br>`web/src/components/CommandPalette/useLocalSources.ts` | **上游文件** | 需 `DALTON-HOOK(colleague-dispatch)` / `DALTON-HOOK(external-share-guard)` | 改用 `useCallerIdentity()` + `colleague_sharing` 门控 |
| `web/src/pages/ChatAgent/components/dalton/TeamSharing.tsx`<br>`web/src/pages/ChatAgent/components/dalton/WorkspaceSharingModal.tsx` | Dalton 独占 | 无需 Hook | ① `useTeamWorkspaces()` 统一门控能力与内部身份；② `WorkspaceSharingModal` 彻底解耦 `colleague_sharing` 与 `conversation_archive` 两区查询/加载/错误/编辑与保存逻辑（关闭 `colleague_sharing` 时跳过 `/sharing` 且跳过渲染研究总监区），`handleSave()` 首行调 `const latest = identity.getLatestIdentity()` 同步核对 `!latest.isConfirming && latest.status === 'ready'` 及最新角色权限，按服务端分字段编辑标识呈现外部属主 `web_access`，保存失败展示 `formatApiErrorDetail` 并在 403 强制重验身份 |
| `web/src/pages/ChatAgent/components/ChatView.tsx`<br>`web/src/pages/ChatAgent/components/ThreadGallery.tsx` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)` / `DALTON-HOOK(colleague-dispatch)` / `DALTON-HOOK(mobile-web)` | 统一订阅 `useCallerIdentity()`；桌面与手机端 `WorkspaceSharingModal` 入口（`ChatView.tsx:1906-1934`、`ThreadGallery.tsx:797-815`）放开为任一能力（`colleague_sharing` 或 `conversation_archive`）启用即可挂载 |
| `web/src/pages/ChatAgent/components/NavigationPanel.tsx`<br>`NavigationRows.tsx`<br>`chatView/usePanelFiles.ts`<br>`FilePanel.tsx`（身份部分）<br>`FileHeaderActions.tsx`（公开监控历史停止入口部分）<br>`web/src/pages/MarketView/components/MarketChatPanel.tsx`<br>`web/src/pages/SharedChat/OwnerArtifactView.tsx` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)` / `DALTON-HOOK(colleague-dispatch)` | 统一订阅 `useCallerIdentity()`；未知态停用属主写入口；`FileHeaderActions.tsx:467` 补齐 `existingMonitor?.visibility === 'public'` 历史停止入口；`OwnerArtifactView` 对外部成员与未知态隐藏 `CommentTools` 并切为只读帧 |
| `web/src/pages/ChatAgent/components/PublishDialog.tsx` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)` / `DALTON-HOOK(monitors)` | 打开/回前台/403 强制重验（`cancelQueries` + `fetchQuery`）+ 60s 后台静默刷新；**提交、受众/自动更新修改与 `copyCurrentLink` 回调首行同步调用 `const latest = identity.getLatestIdentity()` 核对 `!latest.isConfirming && latest.status === 'ready'` 及最新角色权限（如 `latest.canActAsOwner` / `!latest.isExternal` / `latest.canPublishPublic`）**；`admin→internal` 清空 `public` 并阻断回弹要求重选；外部属主非 `own` 面板阻断重发并提供独立降级操作；普通态链接框、`copyCurrentLink`（`:801`）与底栏监控外链（`:1224`）按已确认非外部身份门控；**`unshareOnly` 停止分享唯一调用 `POST /api/v1/artifacts/{artifact_id}/unshare` 并同步失效 `artifacts`、`shareLinks`、`daltonMonitors` 三组缓存**，隐藏复制与受众区；接入 `formatApiErrorDetail` |
| `web/src/pages/ChatAgent/components/ShareButton.tsx`<br>`ThreadSharePanel.tsx`<br>`web/src/pages/ChatAgent/hooks/useThreadShareStatus.ts` | **上游文件** | 需 `DALTON-HOOK(external-share-guard)` | 订阅 `useCallerIdentity({ revalidateOnOpen: true, open })`；**开启分享、修改受众/文件权限与复制回调首行调 `const latest = identity.getLatestIdentity()` 同步核对 `!latest.isConfirming && latest.status === 'ready' && latest.canActAsOwner && !latest.isExternal`（文件权限复选框额外核对 `latest.isAdmin`）**；停止分享仅发 `{ is_shared: false }`；未知态下 `is_shared === true` 保留停止分享可点；外部成员隐藏链接框、复制按钮、受众选择器、文件权限复选框、组织库行与 `SharedInWorkspace` 复制按钮；**`SharedInWorkspace` 对所有文件条目（含 `shared_at IS NULL` 历史公开监控项）唯一调用 `POST /api/v1/artifacts/{artifact_id}/unshare` 并同步失效 `artifacts`、`shareLinks`、`daltonMonitors` 三组缓存**；接入 `formatApiErrorDetail` 与 403 强制重验 |
| 新增前端权限单测（如 `WorkspaceSharingModal.test.tsx`、`ThreadSharePanel.test.tsx`、`OwnerArtifactView.test.tsx` 等） | **新建（均登记 `scripts/dalton/dalton_paths.txt`）** | 登记 `dalton_paths.txt` | 覆盖平台模式、漏配 `VITE_DALTON_AUTH` 收 `401` 进入 `error`/`unknown`、**`useTeamCredentials()` 先发起请求后 `useCallerIdentity().revalidate()` 触发 `cancelQueries` 真实中止 `signal.aborted === true` 并发新请求**、同 tick 拦截、**多实例重叠重验及重验返回降级 `external` 后立即调用旧 `admin` 闭包回调被 `getLatestIdentity()` 同步拦截**、60s 在途轮询与 403 强验重叠取消旧请求、`PublishDialog(unshareOnly)` 与 `SharedInWorkspace` 文件条目唯一调 `POST /artifacts/{id}/unshare` 并失效三组缓存、仅开 `conversation_archive` 桌面/手机弹窗 |

### 3.3 Batch 3：预览桥接三态 `docLocation()`、原始属性存在性互斥校验与全帧统一文档状态机（约 2 人日）

| 文件路径 | 归属与登记 | `DALTON-HOOK` 要求 | 主要改动点 |
|---|---|---|---|
| `src/server/preview_proxy/bridge/core.js`<br>`src/server/preview_proxy/bridge/bridge.test.ts` | **上游文件** | 需 `DALTON-HOOK(mobile-web)` | `docPath()` 扩展为三态 `docLocation()`：`DOC_PATH` 左锚定 6 条带路径前缀（过滤反斜线、控制字符、`..` 段与超长），`DOC_ROOT` 左锚定无路径版本根入口（发 `root: true`），非法或未识别路径显式发 `invalidPath: true`；补全单测 |
| `web/src/pages/ChatAgent/components/viewers/html/pageProtocol.ts` | **上游文件** | 需 `DALTON-HOOK(mobile-web)` | 父端 `docPath` 补齐反斜线与 `..` 段拦截；**移除 `page:ready` 上的 `lenient(docPath)`，在 Zod 剥离可选键之前对原始消息对象 `raw` 用 `Object.prototype.hasOwnProperty.call(raw, key)` 执行四步前置互斥校验**（显式拦截 `{ path: undefined, root: true }`、`{ path: null, root: true }`、`{ path: 'a.html', root: true }`、`{ path: undefined }` 等为 `pathState: 'invalid'`），精确产出 `pathState: 'path' \| 'root' \| 'invalid' \| 'unrecognized'` |
| `web/src/pages/ChatAgent/components/viewers/html/usePageBridge.ts`<br>`web/src/pages/ChatAgent/components/viewers/html/BridgedFrame.tsx`<br>`web/src/pages/ChatAgent/components/viewers/html/liveFrame.ts`<br>`web/src/pages/ChatAgent/components/viewers/html/ArtifactFrames.tsx`<br>`web/src/pages/ChatAgent/components/viewers/HtmlViewer.tsx`<br>`web/src/pages/ChatAgent/components/filePanel/FileViewer.tsx`<br>`web/src/pages/ChatAgent/components/FilePanel.tsx`（文档状态机部分）<br>`web/src/pages/ChatAgent/components/FileHeaderActions.tsx`<br>`web/src/pages/Monitors/MonitorViewer.tsx` | **上游文件** | 需 `DALTON-HOOK(mobile-web)` / `DALTON-HOOK(monitors)` | 实现 `HtmlFrameDocState`（`pending`/`current`/`bridged_root`/`linked`/`unbridged`）；仅 `pathState === 'root'` 进入 `bridged_root`，`invalid`/`unrecognized` 立即清零 `bridge.path` 并设为 `{ kind: 'unbridged', currentUrl: null }`（隐藏原文件动作与新标签入口）；`onBridge` 带帧标识；超时回调核对导航序号；`FileViewer`、`HtmlViewer` 与属主 `ArtifactFrames`（实时帧与版本帧）全部经 `onShownDocChange` 上报 |
| 新增桥接/状态机单测与 Playwright 端到端用例（如 `web/e2e/mobile-p1-layout.spec.js` 或新文件） | Dalton 独占（新文件登记 `scripts/dalton/dalton_paths.txt`） | 登记 `dalton_paths.txt` | 验证无路径监控入口进入 `bridged_root`，非法路径、冲突字段（含 **`{ path: undefined, root: true }`**、`{ path: null, root: true }`、`{ path: 'a.html', root: true }`、`{ path: undefined }`）与缺 `root: true` 的消息均落入 `{ kind: 'unbridged', currentUrl: null }` 并隐藏原动作与新标签入口；真实浏览器验证分享会话内跳转及漏配 `VITE_DALTON_AUTH` 前端收到真实 `401` 后停用特权控件 |

---

## 4. 测试矩阵

### 4.1 权限行为测试矩阵（身份 × 操作 × 资源归属）

| 操作 | `admin` | `internal` | `external` | `unknown`（loading / error / 强制重验中） |
|---|---|---|---|---|
| **普通文件裸发布**（`POST /artifacts/{id}/publish`） | 自有：`200`<br>他人：`404` | 自有与他人均先命中等级守卫：`403 admin_required`（`records.py:577` 先于 `:578 reached()`） | 自有与他人均先命中等级守卫：`403 external_member_forbidden`（`records.py:575` 先于 `:578`） | 前端停用并提示；后端 DB 异常 fail-closed 为 `403` |
| **监控面板发布 / 重发 / 自动更新 / 代理工具** | 自有全可见性含 `public`：`200`；可接管/降级他人 `public` 面板 | 自有 `own`/`members`/`company`：`200`；`public`：`403 admin_required`；从 `admin` 降级后清空 `public` 且阻断回弹，重选前停用提交 | 自有新 `own`：`200`；非 `own`：`403 external_member_forbidden`；已有非 `own` 面板自动更新暂停并提示，经 `by-artifact` 可见降级入口，`POST /monitors` 传 `own`（含快照期并发降级）在 `commit_monitor_publish_tx` 内被 **`FOR SHARE`** + `existing["visibility"]` 拦截（`403`），须先 `PATCH` 独立降级再重发 | 前端停用提交与可见性选择（含跨实例共享 `getLatestIdentity()` 同 tick 拦截）；强制重验完成前拦截提交与公开确认弹窗 |
| **修改文件受众**（`PUT /artifacts/{id}/audience`） | **平台模式**（`HOST_MODE=platform`）：满足 `org_member()` 时切 `organization` `200`；具备 `PUBLIC_LINKS` 策略的**个人工作区**切 `public` `200`，**平台共享工作区（`is_holder`）**从 `organization` 切 `public` 返回 **`403`**（`detail.type == "shared_members_only"`，`share_audience.py:98-110`）<br>**Dalton 部署**（`HOST_MODE=oss`）：切 `public` `200`；`teams_door_open()`（调用 `features.py:211`，定义 `membership.py:40`）为假，选 `organization` 返回 **`403`**（`detail.type == "audience_unavailable"`，`share_audience.py:119-126`） | **平台模式**：满足 `org_member()` 时切 `organization` `200`；个人工作区切 `public`（按平台策略）；共享工作区切 `public` 返回 **`403`**（`detail.type == "shared_members_only"`）<br>**Dalton 部署**：切 `public` 返回 `403 admin_required`；选 `organization` 返回 **`403`**（`detail.type == "audience_unavailable"`） | 前端隐藏；服务端任意受众修改返回 `403 external_member_forbidden` | 前端停用并提示；后端 fail-closed 返回 `403` |
| **文件版本固定**（`PUT /artifacts/{id}/kept`） | 私有 `kept=True`：`200`；已分享 `kept=True`：`200`；`kept=False`：`200` | 私有 `kept=True`：`200`；已分享 `kept=True`：`403 admin_required`；`kept=False`：`200` | 私有 `kept=True`：`403 external_member_forbidden`；已分享 `kept=True`（先命中 `require_internal_member`）：`403 external_member_forbidden`；自有 `kept=False`：`200`（清理开放） | **`kept=True`**：前端隐藏固定入口，后端 fail-closed 返回 `403`<br>**`kept=False`**：在已通过 `reached()` 资源鉴权的前提下保持返回 **`200`** 允许清理 |
| **停止分享（一致锁序原子降级 + 统一 `POST /unshare` + `SELECT {_COLUMNS}` 历史列表）** | `is_dalton_enabled()` 下 `POST /artifacts/{id}/unshare` 与 `PATCH /workspaces/{id}/share-links/{code}` 均按 `lock_owned(share_links) → dalton_monitors FOR UPDATE → unshare_in_cur` 原子降级公开监控并递增 `revision`；前端 `PublishDialog(unshareOnly)` 与 `SharedInWorkspace` 文件条目**唯一调用 `POST /artifacts/{id}/unshare` 并失效三组缓存**；降级后三条匿名监控路由全 `404` | 同左（自有资源，含**管理员接管公开监控后原文件属主经 `unshareOnly` / `SharedInWorkspace` 调 `POST /artifacts/{id}/unshare` 撤销成功 `200`**） | 自有已分享文件及 `shared_at IS NULL` 的历史残留公开监控在文件头与 `SharedInWorkspace` 均通过 `POST /artifacts/{id}/unshare` 停止（`200`）；降为 `own` 后三条匿名路由全 `404`；他人资源 `404` | **`is_shared === true` / 公开监控已开启时仍允许点击停止**（文件条目唯一调 `POST /artifacts/{id}/unshare`）；未分享态无停止操作 |
| **会话读取/状态 vs 会话分享管理（表 A & 表 B）** | 自有工作区+自有会话：读取、状态与分享均 `200`<br>他人工作区会话：读取与 `GET /threads/{id}/status` 严格按 §2.2 **表 A**（他人 Home 替身 `404`；**外部或 `unknown` 属主的个人 Home `404`**；**内部/管理员属主的个人 Home `403`**；普通工作区在 `colleague_sharing` 开时 `200`/`404`、关时 `403`）；**对他人发起会话执行 `need="write"` 时：在 `colleague_sharing=on` 且工作区可见（工作区属主本人 `:1533-1545` 或具备 `rw` 权限的同事 `:1565-1569`）下保持返回 `403`，若工作区为 `hidden`/不可见则先在工作区准入阶段返回 `404`（`sharing.py:1342`）**；分享 `GET/POST /share` 严格按 §2.2 **表 B**（他人发起与本人历史发起均 `403`） | 自有可开启纯对话 `200`，开 `allow_files` 返回 `403 admin_required`，关闭态显式预设受众允许<br>他人工作区会话：读取与状态按 **表 A**（他人 Home 替身 `404`、外部/`unknown` 个人 Home `404`、内部/管理员个人 Home `403`、普通工作区开 `200`/`404` 关 `403`）；**对他人发起会话执行 `need="write"` 在 `colleague_sharing=on` 且工作区可见（工作区属主本人或具备 `rw` 权限同事）时保持返回 `403`（`hidden` 工作区先返回 `404`）**；分享 `GET/POST /share` 按 **表 B**（均 `403`） | 自有会话开启：`403 external_member_forbidden`；停止仅发 `{ is_shared: false }` 返回 `200`（旧前端带 `audience: "organization"` 载荷调 `is_shared: false` 跳过受众校验与合并返回 `200`）<br>**他人工作区任意会话（普通 / 个人 Home / Home 替身，含本人历史发起）**：无论 `colleague_sharing` 开关，**表 A 会话读取/状态与表 B 分享 GET/POST 全部返回 `404`** | 开启与改权限停用；`is_shared === true` 时强制重验期间与错误态仍允许点击停止分享 |
| **复制链接（会话面板 / 工作区列表 / 普通与 `unshareOnly` 发布弹窗）** | 普通面板显示并可复制；`unshareOnly` 隐藏 | 普通面板显示并可复制；`unshareOnly` 隐藏 | 全部面板与列表（含普通 `PublishDialog` 链接框、`copyCurrentLink`、底栏监控外链、`ThreadSharePanel`、`SharedInWorkspace`）均隐藏并由 `getLatestIdentity()` 同步拦截 | 普通面板停用复制按钮并通过跨实例共享 `getLatestIdentity()` 同步拦截回调；`unshareOnly` 隐藏 |
| **文件评论与绑定** | 自有（含平台共享工作区成员）：创建/追加评论 `201`，编辑/读/已读/绑定/解绑 `200`，删除评论 `204` | 自有：创建/追加评论 `201`，编辑/读/已读/绑定/解绑 `200`，删除评论 `204` | 新建/追加/编辑评论及绑定/解绑：`403 external_member_forbidden`；读/已读：`200`；删自己评论：`204`；`/a/<code>` 隐藏 `CommentTools` 并渲染只读帧 | 前端隐藏/停用写评论入口；后端 fail-closed 返回 `403` |
| **工作区与个人团队记忆 / 研究总监共享** | 能力开启时自有可写 `200`；管理员可改非外部属主工作区 `web_access`/`cos_access`；不可见外部工作区与外部个人 Home GET/PUT 均 `404` | 能力开启时自有可写 `200`；他人工作区不可改（可见 `403` / 不可见 `404`） | 个人两项与工作区 `cos_access`/`team_memory`：`403 external_member_forbidden`；自有工作区 GET 返回 `can_edit_web_access: true`、其余编辑标识 `false`，`PUT /sharing` 仅 `web_access` 返回 `200`；他人工作区与外部 Home GET/PUT 均 `404` | 前端隐藏卡片/停用保存；**阶段一调用者成员校验失败：`local` 缺行或 `is_team_member_user` 查库异常 GET/PUT 均 `403`，`supabase` 非成员 `403`、`user_db.get_user` 查库异常 `503`；阶段二通过成员校验后属主 `owner_tier == "unknown"` 时：属主本人 GET 返回 `200` + `can_edit: false`（全子项 `false`），`PUT /sharing`（含仅传 `web_access`）与 `PUT /settings` 均返回 `403 external_member_forbidden` 且不改库；非属主 GET/PUT 均 `404`** |
| **成员邀请** | `local` 邀三档；`supabase` 邀 `external`；`oss` 返回 `403 invites_disabled_in_oss`（`canInvite: false`） | `local` 与 `supabase` 仅邀 `external`（`local` 邀高权限 `403 admin_required`） | `403 external_member_forbidden` | 前端隐藏团队成员分组；后端 fail-closed 返回 `403` |

### 4.2 部署组合与专项测试清单

1. **后端参数化单测与真实 PostgreSQL 并发集成测试（`tests/unit/dalton/test_external_member_write_guards.py` & `tests/integration/dalton/test_external_member_permissions_db.py`）**：
   - **上游平台模式与 `DALTON_ENABLED=false` 回归**：
     - `HOST_MODE=platform` 下：满足 `org_member()` 夹具时 `PUT /artifacts/{id}/audience` 切 `organization` 返回 `200`；具备 `PUBLIC_LINKS` 策略的个人工作区切 `public` 返回 `200`；平台共享工作区（`is_holder(owner_id)`）从 `organization` 切 `public` 返回 **`403`** 且 `detail.type == "shared_members_only"`（`share_audience.py:98-110`）；共享工作区成员执行文件发布、`PUT /kept`、评论创建（`201`）/追加（`201`）/编辑（`200`）/删除（`204`）、绑定/解绑与会话分享，断言 Dalton 角色守卫零误杀；
     - **`DALTON_ENABLED=false`（仅运行上游迁移、无 `dalton_monitors` 表语义）**：验证 `GET /api/v1/workspaces/{id}/share-links`（`list_shared`）、`POST /api/v1/artifacts/{id}/unshare` 与 `PATCH /api/v1/workspaces/{id}/share-links/{code}` 均走上游原 SQL 与 `artifacts_db.unshare()` 正常返回 `200`，零访问 `dalton_monitors` 表。
   - **Dalton 多部署组合与 `audience_unavailable` 全模式断言**：
     - Everflow（`supabase`）、核心安装 `local` × `{colleague_sharing: on/off}` × `{conversation_archive: on/off}`（4 组）、单人回环 `oss`（实调 `/credentials/status` 校验 `auth_mode: "oss"` / `caller_tier: "admin"` / `can_invite: false`，验证工作区设置 `classify_owner_tier` 短路、监控发布、会话分享与邀请 `403 invites_disabled_in_oss`）；
     - **`supabase`、`local` 与 `oss` 三种模式下已授权内部/管理员请求调用 `PUT /artifacts/{id}/audience` 选择 `organization`**：分别显式断言全部返回 **`403`** 且 **`detail.type == "audience_unavailable"`**（`features.py:211` → `membership.py:40` `teams_door_open()` 为假 → `share_audience.py:119-126`）。
   - **关键边界、锁序与并发用例**：
     - **停止分享与发布/重发并发锁序闭环**：并发执行首次 `POST /monitors`（`visibility="public"`）或 `POST /monitors/{slug}/republish` 与 `POST /artifacts/{id}/unshare`（及 `PATCH /workspaces/{id}/share-links/{code}`），验证两边均按 `lock_owned(share_links) → dalton_monitors FOR UPDATE` 顺序加锁无死锁；当停止分享先提交递增 `share_links.revision` 后，持旧 `ReadBasis` 的发布/重发请求在 `read_basis.check(locked)`（`versions.py:125`）抛出 `SharingChanged`（`409`），绝不恢复匿名访问；三条匿名监控路由全部返回 `404`；
     - **管理员接管公开监控后原属主撤销分享**：管理员重发接管他人 `public` 监控（`owner_user_id` 变为管理员）后，原工作区/文件属主调用 `POST /artifacts/{id}/unshare` 返回 `200`，同事务将监控降为 `own` 并关闭文件分享；
     - **历史公开监控列表查询（`list_shared` 复用 `SELECT {_COLUMNS}` 端到端序列化验证 + `app` 链接缺席与单条查询断言）**：在同一工作区下构造三类记录：① 普通已分享文件（`shared_at IS NOT NULL`）、② `share_links.shared_at IS NULL` 且关联 `dalton_monitors.visibility = 'public'` 的历史公开监控文件、③ 私人非文件 `app` 链接（含 `port` 与 `preview_servers` 启动命令）；**首先调用真实 `GET /api/v1/workspaces/{id}/share-links` 接口（完整执行 `_link()` 与 `artifact_from_row()` 序列化），断言 ① 与 ② 两类文件记录按 `COALESCE(l.shared_at, m.updated_at, l.created_at) DESC` 正常返回且无 `KeyError`，同时断言 ③ 私人 `app` 链接不在列表返回中；接着单独调用真实 `GET /api/v1/share-links/{code}` 接口（`src/server/app/share_links.py:239`，经 `get_link()` / `_link()` 消费 `{_COLUMNS}` 的 `app_command`），断言 ③ `app` 链接正常返回且 `command_key` 序列化正确**；最后调用 `POST /artifacts/{id}/unshare` 停止 ② 历史公开监控后，断言监控降为 `own`、再次请求 `GET /api/v1/workspaces/{id}/share-links` 不再返回 ②；
     - **两条监控写入口的真实双连接 PostgreSQL `FOR SHARE` 双向阻塞集成测试（`tests/integration/dalton/test_external_member_permissions_db.py`，`-m integration`）**：使用两个真实 PostgreSQL 数据库连接，**分别**对两条写入口各做双向并发验证（不得只测其一）：
       1. **`commit_monitor_publish_tx`（`POST /monitors` 入口）**：(a) 连接 A 在事务内持有 `SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE` 时，连接 B 并发调用 `set_user_role(uid, "external")`（`FOR NO KEY UPDATE`）被阻塞直到连接 A 提交；(b) 连接 B 先持有 `set_user_role(internal → external)` 更新锁未提交时，连接 A 执行 `commit_monitor_publish_tx`（内部属主传 `visibility="own"` 更新已有 `company` 面板）在 `FOR SHARE` 处被阻塞，待连接 B 提交后连接 A 读到实时 `live_tier == "external"` 与 `existing["visibility"] == "company"`，立即抛出 `403 external_member_forbidden`；
       2. **`commit_monitor_version_tx` / `_cut_and_finalize_monitor_in_cur`（`republish` 与回合后自动更新入口）**：(a) 连接 A 在事务内持有 `FOR SHARE` 时，连接 B 并发调用 `set_user_role(uid, "external")` 被阻塞直到连接 A 提交；(b) 连接 B 先持有 `set_user_role(internal → external)` 更新锁未提交时，连接 A 对已有 `company` 工作区监控执行 `commit_monitor_version_tx` 在 `FOR SHARE` 处被阻塞，待连接 B 提交后连接 A 读到实时 `live_tier == "external"` 与 `live_visibility == "company"`，立即抛出 `403 external_member_forbidden`；
       3. 同时验证数据任务 `publish_data_job_monitor`（`service.py:1084`）继续走 `_verify_job_public_in_tx` 正常发布；降级后的外部属主调 `GET /monitors/by-artifact/{id}` 返回 `200` 且 `can_manage=True`；
     - **会话读取/状态（表 A）、写保护保留（`:1477-1571`）与会话分享管理（表 B）双模式全覆盖**：在 `supabase` 与 `local` 两种多用户模式下，分别针对 `GET /threads/{id}`（`crud.py:317`）、消息回放（`messaging.py:1453`）、**`GET /threads/{id}/status`（`messaging.py:1613`）**、`GET /threads/{id}/share`、`POST /threads/{id}/share` 逐格断言表 A 与表 B 的状态码（重点覆盖：外部成员跨区在全部 5 个端点含 `/status` 均返回 `404`；内部/管理员访问**他人 Home 替身**在 `on/off` 下均返回 **`404`**、访问**外部或 `unknown` 属主的个人 Home** 在 `on/off` 下均返回 **`404`**、访问**内部/管理员属主的个人 Home** 在 `on/off` 下均返回 **`403`**；`colleague_sharing=off` 时内部成员读取他人普通工作区会话及**本人历史在他人工作区发起的派发会话**均经重写后的 `:1392` 分支返回 `403`；**以及 `colleague_sharing=on` 时工作区属主与具备 `rw` 权限的同事对他人发起会话调用 `require_thread_access(..., need="write")` 仍分别在 `:1533-1545` 与 `:1565-1569` 返回 `403`，而 `hidden` 工作区先在 `:1342` 返回 `404`**）；
     - **旧前端载荷停止会话分享**：外部成员携带含 `audience: "organization"` 的完整 `permissions` 调 `POST /threads/{id}/share`（`is_shared: false`），验证在 `require_members_audience()` 之前置 `effective_req_permissions = None`，返回 `200` 且原 `share_permissions` 不变；
     - **`PUT /artifacts/{id}/kept` 四行用例**：外部私有 `kept=True` → `403 external_member_forbidden`、外部已分享 `kept=True` → `403 external_member_forbidden`、内部已分享 `kept=True` → `403 admin_required`、外部/未知态已过 `reached()` 调 `kept=False` → `200`；
     - **工作区设置分模式两阶段校验独立用例**：
       1. **阶段一用例**：(a) `local` 模式下：① 调用者在 `dalton_user_roles` 缺行（含其 `user_id` 位于配置 `pm_user_ids` 中的情形，验证 `local` 不豁免缺行），访问 `GET/PUT /workspaces/{id}/sharing` 与 `/settings` 均在 `require_team_reader(uid)` 返回 **`403`**；② **单独向 `require_team_reader(uid)` 所调用的 `is_team_member_user(uid)`（`sharing.py:878-888` / `access_tiers.py:340-350`）数据库查询注入异常**（注意 `is_admin_user()` 首次查库异常被捕获转为 `False` 后仍会进入 `require_team_reader` 独立查询成员资格），断言 `/workspaces/{id}/sharing` 与 `/settings` 的 GET 与 PUT 均返回 **`403`**；(b) `supabase` 模式下：非 `is_pm` 调用者非成员返回 **`403`**，模拟无 `_verified_token_identity` 且 `user_db.get_user(uid)` 抛数据库异常时 `GET/PUT` 均返回 **`503`**；
       2. **阶段二用例**：先让调用者通过阶段一成员资格校验（`local` 在 `dalton_user_roles` 中有合法 `internal` 行，或 `supabase` 合法成员），再单独模拟 `classify_owner_tier(owner_id)` 失败返回 `"unknown"`，断言属主本人访问自己工作区时 `GET /sharing` 与 `GET /settings` 返回 `200` 且 `can_edit: false`（全部子项 `false`），**仅提交 `PUT /workspaces/{id}/sharing` `{"web_access": "ro"}` 与 `PUT /settings` 均返回 `403 external_member_forbidden` 且数据库记录未修改**；非属主访问该工作区时 `GET/PUT` 均返回 `404`；
     - **能力注册表断言**：与主分支 #504 对齐，`capabilities.py` 中 `get_workspace_settings_endpoint`、`update_workspace_settings_endpoint`、`get_user_sharing_settings_endpoint` 为 `CORE_CAPABILITY`、`update_user_sharing_settings_endpoint` 保持 `COLLEAGUE_SHARING`、`get_user_settings_endpoint` 与 `update_user_settings_endpoint` 为 `CONVERSATION_ARCHIVE`（原因：#504 已上线，核心安装关闭共享时这些读取接口返回 200 中性结果，外部成员与成员资格两阶段校验在接口函数体中执行）。
2. **核心安装真实容器验收（`tests/standalone/checks/b4_local_auth.py`）**：
   - 在 `auth: local` 栈中以受邀 `external` 账号调用文件受众、会话读取、会话状态（`GET /threads/{id}/status`）、会话分享、用户设置、工作区设置与监控发布接口，校验 403/404 响应与错误码；
   - **漏配 `VITE_DALTON_AUTH` 真实容器 `401` 与前端停用特权联动验收**：在 `b4_local_auth.py` 中模拟漏配 `VITE_DALTON_AUTH` 的前端（无本地会话令牌）直接请求真实 `core/local` 容器的 `GET /api/v1/dalton/credentials/status` 断言返回 `401 Unauthorized`，并配合第 4 项浏览器端到端测试验证前端收到该真实 `401` 后 `useCallerIdentity()` 进入 `status: 'error', tier: 'unknown'` 且发布、开启分享、改受众、复制链接等特权操作全部停用。
3. **前端与桥接单测（`cd web && pnpm test`、`make test-bridge`）**：
   - `useCallerIdentity.test.tsx`：
     - 覆盖上游平台模式直通、服务端 `200` + `auth_mode: "oss"`（`canInvite: false`）、正常 `VITE_DALTON_AUTH=local` 下按服务端 `caller_tier` 隔离；
     - **漏配 `VITE_DALTON_AUTH` 指向 `core/local` 后端收到 `401` 时进入 `status: 'error', tier: 'unknown'` 并停用全部特权操作**；
     - **跨 Hook 冷启动取消在途 HTTP 请求（`useTeamCredentials` 先起网 → `useCallerIdentity().revalidate()` 中止 `AbortSignal`）**：当缓存尚无数据（`data === undefined`）且由 **`useTeamCredentials()`**（复用共享 `fetchTeamCredentialsStatus({ signal })`）率先发起的首次 HTTP 请求 A 仍在飞行中时，敏感面板挂载并调用 **`useCallerIdentity().revalidate()`**，**明确断言请求 A 收到的 `signal.aborted === true`（底层 HTTP 请求被真实中止）**，并实际发出全新网络请求 B（绝不复用请求 A 的旧响应）；
     - **调用 `revalidate()` 后同 tick 同步拦截**：在调用 `revalidate()` 后的同一事件循环 tick（React 尚未重渲染前）立即调用提交或复制回调，断言被 `identity.getLatestIdentity().isConfirming === true`（及 `identity.isConfirmingNow() === true`）同步拦截；
     - **跨 Hook 实例共享同一 `queryKey` 重叠调用 `revalidate()` 及降级完成后旧闭包同步拦截**：组件实例 A 与实例 B 同时挂载并共享同一 `(queryClient, queryKey)`（初始缓存为 `admin`，此时在实例 A 上捕获了提交与复制回调闭包）；实例 A 先调用 `revalidate()` 发出请求 1，在请求 1 飞行中实例 B 调用 `revalidate()`（通过 `cancelQueries` 取消请求 1 并发出请求 2，返回降级后的 `external`）→ **① 断言当请求 1 被取消并执行完 A 的 `finally` 后、请求 2 仍在飞行期间，实例 A 与实例 B 的 `isConfirming`、`isConfirmingNow()` 与 `getLatestIdentity().isConfirming` 均持续保持 `true`，两实例中的提交与复制回调均被同步拦截；② 断言当请求 2 返回 `external` 并执行完 `finally` 将 `isConfirming` 置回 `false` 的瞬间，立即调用此前在实例 A 上捕获的旧 `admin` 提交/复制回调闭包（不等待 React 重渲染），`identity.getLatestIdentity()` 同步返回 `tier: 'external', isExternal: true, canActAsOwner: false` 并立即拦截该操作（不发请求、不写剪贴板）**；
     - **预置未过期的 5 分钟新鲜缓存**：分别断言「打开敏感面板」「窗口回到前台（`visibilitychange` / `focus`）」「写操作收到 `403`」三次触发均**实际发出新的网络请求**，且在请求飞行期间 `isConfirming === true`（提交与复制按钮停用，而已分享资源的停止按钮仍可点击）；
     - **60 秒持续打开定时轮询与 403 强制重验重叠竞态**：① 单独 60 秒轮询发出新请求时 `isConfirming === false` 且 `isConfirmingNow() === false`（不闪烁停用按钮），响应降级或报错立即生效；② **当 60 秒轮询请求 A（返回旧 `admin`）尚在飞行中时，触发写接口 `403` 调用 `revalidate()`（`cancelQueries` 中止请求 A 的 `signal` + `fetchQuery`）发出新请求 B（返回 `external`），断言请求 A 的 `signal.aborted === true`、在请求 B 完成前 `isConfirming` 与 `getLatestIdentity().isConfirming` 持续保持 `true` 不被 A 提前清零，且请求 B 返回后立即由 `getLatestIdentity()` 反映 `external`**；
   - `PublishDialog.monitors.test.tsx`：覆盖 `admin→internal` 清空 `public` 且阻断回弹至 `existingMonitor.visibility`、外部降级阻断重发与独立降级入口、普通态外部/未知隐藏链接框并通过 `getLatestIdentity()` 同步拦截 `copyCurrentLink` 与底栏外链、**`unshareOnly` 唯一调用 `POST /artifacts/{id}/unshare`（含管理员接管公开监控后原属主成功撤销并同步失效 `artifacts`、`shareLinks`、`daltonMonitors` 三组缓存）**、强制重验期间已分享仍可点击停止分享与 `403` 错误文案展示；
   - `WorkspaceSharingModal` + `ChatView` + `ThreadGallery` 单测：覆盖桌面与手机端在**仅开启 `conversation_archive`（关闭 `colleague_sharing`）**时正常显示入口、打开弹窗、保存团队记忆且完全跳过 `/sharing` 请求（不渲染研究总监控件）；覆盖保存 `403` 展示 `formatApiErrorDetail` 并触发强制身份重验；
   - `ThreadSharePanel` / `ShareButton` / `OwnerArtifactView` / `SettingsRail` / `CommandPalette` / `Automations` 单测：覆盖 `is_shared: false` 载荷不含 `permissions`、未知态与强制重验期 `is_shared === true` 允许停止、**文件头 `FileHeaderActions` 与 `SharedInWorkspace` 均能展示 `shared_at IS NULL` 的历史公开监控，且点击停止均唯一发出 `POST /api/v1/artifacts/{artifact_id}/unshare` 并失效 `artifacts`、`shareLinks`、`daltonMonitors` 三组缓存**、`/a/<code>` 外部成员无 `CommentTools`、`SettingsRail` 关闭 `colleague_sharing` 时跳过 `/sharing/user-settings`；
   - `bridge.test.ts`（`make test-bridge`）与 `pageProtocol` / `HtmlViewer` / `BridgedFrame` / `ArtifactFrames` / `FileViewer` / `FileHeaderActions` / `MonitorViewer` 单测：
     - 验证 `DOC_PATH` 全部 6 条左锚定完整前缀、工作区路径内含 `/v/2/` 与 `files/serve/` 防误截断；
     - 验证 `DOC_ROOT` 无路径版本根入口（如 `MonitorViewer.tsx:147`）发出 `root: true` 并在父端进入 `bridged_root`；
     - **验证非法路径（`../`、`\`、控制字符、超长、非托管前缀）发出 `invalidPath: true`，以及父端基于自有属性存在性（`hasOwnProperty`）对冲突字段组合（含 `{ path: undefined, root: true }`、`{ path: null, root: true }`、`{ path: 'a.html', root: true }`、`{ path: 'a.html', invalidPath: true }`、`{ root: true, invalidPath: true }`）、非法 `path`（含 `{ path: undefined }`、`{ path: null }`）、`invalidPath: true` 和缺少 `root: true` 的无路径消息一律清除旧 `bridge.path` 并映射为 `{ kind: 'unbridged', currentUrl: null }`，在桌面与移动端同时隐藏原文件 A 全部动作与「在新标签打开」入口**；
     - 验证属主实时帧与版本帧 `onShownDocChange` 上报、`onBridge` 帧标识防跨标签串写、超时导航序号核对。
4. **真实浏览器端到端用例（Playwright）**：
   - 在分享会话与外部成员预览中实测 `<a href>` 相对链接跳转 A → B、连续跳转 B → C、脚本 `location.href` 跳转进入 `unbridged`（确认隐藏 A 的全部操作且隐藏「在新标签打开」）、无路径公开监控 `bridged_root`，并在桌面（1280px）与手机（390px）两个视口下断言工具栏动作一致；
   - **漏配 `VITE_DALTON_AUTH` 前端收到真实 `401` 端到端验证**：验证未携带本地认证会话的前端在收到 `/api/v1/dalton/credentials/status` 的 `401` 响应后，`useCallerIdentity` 处于 `status: 'error', tier: 'unknown'`，页面上发布、开启分享、修改受众与复制链接等特权操作全部处于停用状态。

---

## 5. 待决问题

**当前无剩余待决问题（0 项）**。全部 13 项设计裁决及七轮复审细则均已定案并归入第 0.2 节与第 2、3、4 节，可直接按第 3 节启动 Batch 1 编码。

---

## 6. 后续事项（记入 Backlog，本轮范围之外）

1. **停止向外部成员下发公开链接标识**：待前端隐藏复制入口稳定后，评估将 `GET /api/v1/threads/{id}/share` 与 `GET /api/v1/workspaces/{id}/share-links` 在外部成员视角下改为脱敏响应（仅返回 `is_shared`、标题与用于执行停止操作的句柄，不下发可拼接公开 URL 的 `share_token` / `share_url` / `code`）。
2. **分享会话中脚本 `location.href` 跳转补注入桥接**：当前 `src/server/app/share_files.py:455-459` 仅在携带 `?inject=view` 时注入桥接脚本，脚本改写 `location.href` 会进入 `unbridged` 非桥接态（已安全隐藏文档 A 的误导操作）。后续如需支持分享会话内的脚本跳转仍自动识别文档 B，可参考属主路由 `src/server/app/workspace_files/serve.py:464-480` 按 `Sec-Fetch-Dest` 补齐 `nested` 注入与 `Vary: Sec-Fetch-Dest` 缓存隔离。
