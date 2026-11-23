# 同事共享权限对工作区分析师生效设计方案（兼容独立核心版本）

> **目标分支**：`design/colleague-agent-access`（定稿版）  
> **核心原则**：把目前同事的 Chief of Staff（研究总监，运行在个人 `Home` 工作区的统筹智能体）对他人工作区的读取与复制权限，原样复制给同事工作区里的 Analyst（工作区分析师，运行在具体公司或专题工作区的投研智能体）；分析师的写入只落在当前工作区，研究总监现有行为保持不变（并与分析师同步获得 UTF-8 文本文件服务端原样复制能力）。

## 摘要

本方案按**“分析师跨区读取 = 研究总监同一套 `cos` 权限 + 跨人双身份交集 + 复制与写入只落当前工作区”**打通工作区分析师跨计算机取用同事共享工作区资料的链路。分析师新增直接工具 `colleague_workspace`（含 `list_workspaces`、`list_files`、`read_file`、`copy_file`、`list_threads`、`read_thread` 6 项动作），直接复用研究总监现有的权限函数（`verify_workspace_owner`、`verify_thread_owner` 与 `require_workspace_access` 的 `cos` 通道）、镜像/沙箱文件读取函数与对话提取函数；**`copy_file` 为研究总监与分析师同步新增 ≤ 5 MB 白名单扩展名严格 UTF-8 文本文件服务端原样复制**（对解码文本与多编码原始字节做已知密钥检查，命中即拒绝复制并说明原因，通过则服务端逐字节落盘）；分析师的 `copy_file` 与知识库更新限定只写当前工作区（按实际 `origin_channel` 核验写权限），跨工作区写文件、写知识库与 `delegate_to_analyst`（派发任务）仍归研究总监专属。跨人轮次逐一检查发消息的人 A 与当前工作区主人 B 的权限返回值并取交集；同时通过基类 `DbJsonRoute._read_only()` 与 `CallContext.caller_user_id` 锁定跨人轮次记忆及全部用户数据路由只读，并为分析师补齐持久化外发与面板自动刷新待确认守卫。工具、文本复制与派发交接提示词同属第 1 批上线；独立核心版本（Standalone Core，自包含部署形态）通过 `CapabilityId.COLLEAGUE_SHARING`（Everflow 默认开、核心安装默认关）兼容。

---

## 1. 现状与问题根因

1. **跨工作区工具仅挂给研究总监，且 `copy_file` 仅支持二进制文件**：`CHIEF_OF_STAFF_TOOLS`（`src/tools/secretary/chief_of_staff.py:306-311`）仅在 `role == "chief_of_staff"` 时挂载（`src/ptc_agent/agent/agent.py:1504-1507`），含 `workspaces_list`（`tools.py:61`）、`workspace_files_list_cos`（`:959`）、`workspace_file_read_cos`（`:1040`）、`workspace_file_write_cos`（`:1145`）、`workspace_file_copy_cos`（`:1757`，`:1922` 目标仅限本人 `Home` 或本人工作区，`:1772` 拒绝文本文件，导致 25 个约 700 KB 文本文件无法直接复制）、`threads_list`（`:659`）与 `agent_output`（`chief_of_staff.py:212`）。权限由 `_commands.py:170,221` 的 `verify_workspace_owner` / `verify_thread_owner` 单人校验（拒绝返回 `Command`，成功返回 `None`）。
2. **既有知识库工具仅单方校验且两角色共用工厂**：`src/dalton/tools/__init__.py:188,279` 均调 `create_knowledge_tools`（`knowledge_tools.py:476,575,700`），仅查提问人 A 的权限且 `:488,584` 用 `already_admitted` 跳过了跨人写通道复核。
3. **现网真实失败与跨人身份错位**：A 从 `Home` 派活或直接在 B 的工作区提问时，沙箱运行在 B 的机器上（`ptc_run.py:885`），分析师缺少跨区工具且交接提示（`chief_of_staff.md.j2:43`）与基线（`baseline.py:844`）未指明可用工具；提示词未区分 `/chat/<workspace_id>` 与 `/chat/t/<thread_id>`（`ChatAgent.tsx:50-51`）；`agent.py:493` 将 `user_id=owner_id`（B）传入 `build_filesystem_backend`，使跨人只读判断恒为假，且模型易把提问人误称为“这台电脑的账户”。

---

## 2. 目标行为

### 2.1 分析师跨工作区动作与写入边界

| 动作 / 工具 | 权限要求 | 复用的研究总监实现 | 行为说明 |
|---|---|---|---|
| `list_workspaces` | `cos` `read`（本人按所有者放行） | `get_workspaces_for_user` + `list_colleague_cos_workspaces` | 对 `{A, B}` 可见工作区取交集，保留已停止可读镜像的工作区，排除 `W_cur`、`Home`、`deleted`、`unbound`。 |
| `list_files` / `read_file` | 目标 `cos` `read` | `workspace_files_list_cos` / `workspace_file_read_cos` | 停止态读持久化镜像，运行态读沙箱，`read_file` 按源所有者密钥脱敏。 |
| `copy_file` | 源 `cos` `read` + `W_cur` 实际 `origin_channel` `write` | 共用源读取、二进制扫描与**新增 ≤ 5 MB 严格 UTF-8 文本复制** | 将源工作区的二进制文件（≤ 50 MB）或白名单 UTF-8 文本文件（≤ 5 MB）服务端逐字节复制进**当前工作区 `W_cur`**（研究总监同步获得文本复制，落点仍为本人 `Home` 或本人工作区）。 |
| `list_threads` / `read_thread` | 目标工作区 / 对话 `cos` `read` | `threads_list`（`verify_workspace_owner`） / `agent_output`（`verify_thread_owner` + `extract_text_from_thread`） | 列出最近 20 条活跃对话；读取对话回复（保留流式读取与 `__wsref__` 链接改写）。 |
| 知识库与文件写入 | 分析师只写 `W_cur`（按实际 `origin_channel` 核验 `write`） | `create_knowledge_tools(..., role=role)`；本地文件工具 | 分析师 `colleague_workspace` 无跨区 `write_file`；`role=="analyst"` 时 `knowledge_update` / `knowledge_append_verdict` 拒绝非 `W_cur` 的 `workspace_id` 并对跨人轮次复核 `W_cur` 在 `origin_channel` 下的写权限（去掉 `knowledge_tools.py:488,584` 跨人跳过），跨区 `knowledge_read` 走双人校验；研究总监保持原样。 |

### 2.2 身份组合判定规则

设 **A** 为发消息的人，**B** 为当前工作区 `W_cur` 所有者，**C** 为目标工作区 `W_target` 所有者：
- **本人轮次（`A == B`，含内部成员、管理员及外部成员本人）**：等同 A 本人的研究总监权限——本人其他非 `Home` 工作区（`C == A`）按所有者放行，同事工作区走 `require_workspace_access(channel="cos")`。
- **跨人轮次（`A != B`）**：直接调用现有 `get_user_tier` / `classify_owner_tier`（`access_tiers.py:53`）确认 A、B 均为内部成员/管理员，且 **A 与 B 两人分别通过研究总监权限检查**；任一方返回“未找到”时整次统一返回“未找到”，仅当两人均至少可读且因写被拒时才返回“只读”提示。
- **目标为 `Home`**：主 `Home`（`is_home_row`）与 `Home` 替代工作区（`is_reserved_row`）一律拒绝（含 `A == B == C`）。

---

## 3. 实现方案

### 3.1 双人校验与 `list_workspaces` 双人交集

1. **上下文与当前工作区复核**：从 `config["configurable"]` 读取 `A = user_id`、`W_cur = turn_admitted_workspace_id or workspace_id`、实际 `origin_channel`（派发轮次为 `"cos"`，网页轮次为 `"web"`，`ptc_run.py:281,1189`）及 `tail_turn_no_new_work`（为真即拒）。`A != B` 时核验 A、B 均为内部/管理员，并调 `require_workspace_access(W_cur, A, channel=origin_channel, need=cur_need)`（捕获 `HTTPException(403/404)` 转为拒绝 `Command`）。
2. **逐目标 `Command | None` 判定**：先查目标工作区（`read_thread` 先由 `thread_id` 反查工作区），排除缺失、`deleted`、`unbound` 及 `is_home_row` / `is_reserved_row` / `is_home`；再对 `{A, B}` 去重后的每个 `u` 逐次调 `verify_workspace_owner`（`read_thread` 调 `verify_thread_owner`），检查返回值 `cmd is None` 为通过；任一方返回非只读拒绝则整次返回未找到。
3. **`list_workspaces` 双人交集步骤**：对 `{A, B}` 去重后的每个身份 `u`，分别取“本人工作区 `get_workspaces_for_user(u)` ∪ 同事可见工作区 `list_colleague_cos_workspaces(u)`”，按 `workspace_id` 取**交集**（`A != B` 时访问级别取 `ro` 与 `rw` 中较严一档），最后统一过滤掉 `W_cur`、主 `Home` 与替代工作区（`is_home_row` / `is_reserved_row` / `is_home`）、`deleted` 及 `unbound`。

### 3.2 文本与二进制服务端 `copy_file` 规则（研究总监与分析师共用源检查）

1. **文本文件接受条件与双重密钥检查**：
   - **白名单扩展名**：仅接受 `.md`、`.markdown`、`.txt`、`.csv`、`.tsv`、`.json`、`.jsonl`、`.yaml`、`.yml`、`.py`、`.sql`、`.html`、`.xml`、`.log`（大小写无关）；运行态沙箱读取后对解析出的实际源路径 `source_client_path`（`tools.py:1877`）再次执行 `_is_non_owner_blocked_path` 与扩展名复核；
   - **严格编码与大小**：单文件 `<= 5 MB`（`5 * 1024 * 1024` 字节），原始字节**不含 `NUL`（`b"\x00"`）**且通过**严格 UTF-8 解码**（`raw_bytes.decode("utf-8-sig")`，允许带 BOM〔Byte Order Mark，字节顺序标记〕）；UTF-16 等其他编码或含空字节文件直接拒绝并说明原因；
   - **解码文本 + 多编码原始字节密钥检查**：加载源工作区所有者 `vault_secrets`，① 对解码后的字符串运行 `get_redactor().redact(text, vault_secrets=vault_secrets) != text`（检查平台密钥、令牌与保险库密钥）；② 复用 `tools.py:1343,1580` 二进制扫描做法，将 `vault_secrets`（长度 `>= MIN_REDACT_SECRET_LEN`）按 `("utf-8", "utf-16-le", "utf-16-be")` 编码后在原始字节中检索。任一命中即拒绝复制并说明原因；均未命中则服务端将原始 `raw_bytes` 逐字节写入目标沙箱。
   - **二进制文件密钥检查与边界**：
     - **带宏的 Office 文件一律拒绝**：`.xlsm`、`.docm`、`.pptm`、`.xlam`（以及伪装为无宏扩展名但内嵌 `vbaProject.bin` 的 OOXML 容器）一律拒绝复制并给出说明，防止宏项目夹带无法核验的凭据或代码；
     - **OOXML 文档（`.docx`、`.xlsx`、`.pptx`）XML 已知密钥检查**：解压后对全部 `.xml` / `.rels` 成员（含 XML 实体反转义）执行与文本复制相同的 `get_redactor().redact(..., vault_secrets=vault_secrets)` 已知密钥检查及多编码保险库凭据检查；
     - **图片与扫描版 PDF 照常允许复制**：图片（`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.bmp`、`.tiff`）及纯图片扫描版 PDF 无法检查像素中的文字内容，照常允许通过 `copy_file` 复制（PDF 仍照常执行文本层、对象流、注释与附件扫描，并受页数/流解压量/子进程超时与内存上限约束）。
   - **撤销共享防落盘（临时文件 + 复核 + 原子改名）**：研究总监与分析师的 `copy_file` 均先将字节写入目标工作区的临时文件（`.wsfiles-copy-*`），落盘完成后在提交前**再复核一次**源端共享权限，复核通过才原子改名为正式目标路径；复核失败或改名异常时立即删除临时文件，目标路径不落盘。
2. **目的地分流与解析后路径校验**：研究总监保持落入本人 `Home` 或本人工作区；分析师固定落入 `W_cur`（默认 `shared/<filename>`），以 `B` 身份 `acquire_sandbox_to_change(W_cur, B)`，经 `normalize_requested_path`、`_is_non_owner_blocked_path`、`_sibling_folder_named`、`contained_target` 解析出 `dest_abs` 后，**对 `to_client_path(dest_sandbox, dest_abs, dest_work_dir)` 再次执行 `_is_non_owner_blocked_path`**（防符号链接覆盖受保护文件），再调 `aupload_file_bytes` 写入。

### 3.3 链接识别与来源标注

- `colleague_workspace` 自动提取 `/chat/<workspace_id>` 与 `/chat/t/<thread_id>` 中的 UUID（Universally Unique Identifier，通用唯一识别码）；在 `ProvenanceMiddleware._extractors`（`middleware.py:366`）显式注册 `"colleague_workspace"` 登记脱敏来源摘要；链接在浏览器打开继续按 `web_access`（`crud.py:245`）判断。

---

## 4. 与独立核心版本的兼容性

1. **能力开关**：登记 **`CapabilityId.COLLEAGUE_SHARING = "colleague_sharing"`**（环境变量 `DALTON_CAP_COLLEAGUE_SHARING`；Batch 2 前由 `colleague_sharing_gate.py` 承接），**`everflow_default = True`、`core_default = False`**（核心安装默认关，由客户管理员开启；需跨人发消息或派活再设 `DALTON_COLLEAGUE_DISPATCH_ENABLED=true`）。关闭时不挂工具、`DALTON_ROUTE_CAPABILITY_REGISTRY` 中 5 个共享路由返回 `404`、提示词与前端隐藏共享入口。
2. **门禁分离与 `sandbox-net`**：同事跨工作区访问走 **`is_dalton_sharing_auth_enabled()`**（Batch 4 接入 `dalton_user_roles` 与 `require_team_reader` 前，`local` 模式在此入口保持 `False` 仅所有者访问）；公开分享与产物发布角色限制（`share_links.py:74`、`share_access.py:117`、`artifacts/records.py:267`、`snapshot.py:941`）走 **`is_dalton_multiuser_auth_enabled()`**。跨区读写与复制全在 `backend` 进程内完成，零跨沙箱直连，文案与提示词零专有词。

---

## 5. 提示词与帮助文档改动点

- **第 1 批同步修改**：`chief_of_staff.md.j2:43-50`（挂 `colleague_sharing_enabled` 条件）与 `tools.py:106-124` 派发交接说明点名分析师调用 `colleague_workspace(action="copy_file")` 复制二进制（≤ 50 MB）或 UTF-8 文本文件（≤ 5 MB）进当前工作区；`baseline.py:844`、`epoch.py:109,968` 与 `workspace_context.md.j2` 按实际已挂载 `colleague_workspace` 条件渲染工具与 `/chat/<workspace_id>` 链接说明；`turn.md.j2`、`user_profile.md.j2`、`user_visible_text.md.j2` 区分跨人轮次提问人 A 与工作区主人 B，不称“这台电脑的账户”。
- **第 2 批文档同步**：更新 `plugins/langalpha_service/skills/langalpha-doc/**` 与 `docs-site/src/content/docs/` 5 篇共享与角色对比文档。

---

## 6. 设置页与共享弹窗文案（中英文，第 2 批）

涉及 `web/src/locales/{zh-CN,en-US}.json`、`web/src/pages/Settings/sections.ts`、`src/dalton/app/router.py:601,921`：

| 位置 / 键名 | 新中文文案 | 新英文文案 |
|---|---|---|
| `settings.dalton.cosSharingLabel` / `dalton.sharing.cosUserToggleTitle` | **同事的智能体访问** | **Colleague agent access** |
| `settings.dalton.cosSharingDesc` | **控制内部同事的智能体（研究总监与工作区分析师）能否跨区访问你的非 Home 工作区或派发任务。** | **Control whether internal teammates' agents (Chief of Staff and workspace analysts) can access or dispatch tasks to your non-Home workspaces.** |
| `dalton.cosSharing.enabledBadge` / `disabledBadge` | **已允许访问** / **已关闭** | **Access enabled** / **Disabled** |
| `dalton.cosSharing.toggleLabel` / `dalton.sharing.cosUserToggleLabel` | **允许内部同事的智能体访问我的工作区** | **Allow internal teammates' agents to access my workspaces** |
| `dalton.cosSharing.toggleDesc` | **开启后，内部同事的研究总监与工作区分析师均可按权限跨区读取或复制你非 Home 工作区中的文件与对话（跨区写入与派发任务仍仅通过研究总监发起）。** | **When enabled, internal teammates' Chief of Staff and workspace analysts can read or copy files and conversations from your non-Home workspaces (cross-workspace writes and task dispatch go through Chief of Staff).** |
| `dalton.sharing.cosLabel` / `cosManagedInSettingsHint` | **同事的智能体（研究总监与分析师）** / **同事的智能体访问权限已在「设置 → 对话与助手」中按账户统一管理。** | **Colleagues' Agents (Chief of Staff & Analysts)** / **Access for colleagues' agents is managed globally in Settings → Chat & Assistant.** |
| `router.py:601,921` 外部成员提示 | **`外部成员工作区不支持开启同事的智能体访问权限`** / **`外部成员的工作区不对其他成员的智能体开放，无需此项设置`** | （后端统一中文提示） |

---

## 7. 安全防护与边界说明

1. **跨人轮次记忆与全部用户数据路由只读**：
   - **生产身份传参**：`src/ptc_agent/agent/agent.py:493` 调 `build_filesystem_backend` 时传 `user_id=str(user_id) if user_id else owner_id`（A）与 `workspace_owner_id=workspace_owner_id or owner_id`（B）；
   - **基类统一只读判断**：将 `cross_user_read_only: bool = False` 放入基类 `DbJsonRoute.__init__`（`db_json_route.py:323`）并写入 **`DbJsonRoute._read_only()`（`:698`：`if self._cross_user_read_only: return True`）**，使 `UserDataBackend`、`AutomationsBackend`、`SubagentsBackend`、`ChannelsBackend` 全部统一生效；`awrite_text`（`:734`）、`awrite_versioned`（`:862`）与 **`aedit_text`（`:741` 入口调 `self._read_only(filename)`）** 统一拦截，**`DbJsonFolderRoute` 的 `is_writable`（`db_json_folder.py:94`）、`adelete_versioned`（`:99`）与 `arename_versioned`（`:143`）** 同样检查 `self._cross_user_read_only` 并拒绝；`filesystem_routes.py:240-349` 在 `A != B` 时将用户记忆、工作区记忆、工作流 `StoreBackend` 及上述全部用户数据路由设为只读；
   - **LiveFS（FUSE〔Filesystem in Userspace，用户态文件系统〕实时挂载）调用者传递**：在 `CallContext`（`livefs_mount.py:50`）新增 `caller_user_id: str | None = None`，本机命令（`agent.py:478`）与**研究总监 `computer=` 远端命令（`agent.py:939` `there_call`）** 均填入 `caller_user_id=str(user_id)`；`outcomes.open_call`（`outcomes.py:96`）保存、**`outcomes.call_context`（`:123`）反序列化读回 `caller_user_id`**；`LivefsTree._build()`（`livefs/tree.py:282`）在上下文存在且 `caller_user_id` 不等于计算机/工作区主人时按跨人只读拒绝变更，查不到上下文时保持现网行为按主人处理，避免误伤主人本人及远端计算机写入。
2. **`Home` 禁作目标**：主 `Home`（`is_home_row`）与替代工作区（`is_reserved_row`）无条件拒绝作为跨区目标。
3. **仅作用于分析师的外发守卫与 `keep_updated` 持久待确认标记**：
   - **角色范围**：外发守卫**仅作用于 `role == "analyst"`**，研究总监现有的“复制到 `Home` 后发送”等行为完全不变；跨区读回文本统一包裹 `<<<EXTERNAL_UNTRUSTED_CONTENT>>>`（`formatting.py:37`）；
   - **同轮与恢复结算拦截**：分析师在跨人轮次（`A != B`）或本人已执行跨区读/拷的轮次，在运行记录 `conversation_responses.metadata` 持久化标记 `"colleague_outbound_blocked": true`，服务端拒绝 `send_message`、飞书写入、`dalton_report_delivery`（含私信）、`automation_report(action="submit")`、`podcast_weekly_write(action="publish_doc")` 与 `dalton_monitor(action in ("publish", "update", "republish"))`，并在正常及恢复结算（`coordinator.py:549,895`）跳过当轮 `keep_updated_after_turn`（`publish_report` 进内部审阅队列，保持可用）；
   - **后续轮次自动刷新待确认标记**：受限分析师轮次若修改了工作区内已开启 `keep_updated` 的监控面板或共享产物源文件，在该面板/产物记录上持久化写入“待确认（`pending_outbound_confirm`）”标记；后续任意轮次的自动刷新（`snapshot.py:895`、`monitors/service.py:672`）发布前检查该标记并暂停自动发布，直至工作区主人在后续轮次明确确认内容与可见范围后清除标记。

---

## 8. 测试方案与分批计划

- **第 1 批（核心工具、文本复制、双人校验、只读与外发防护、交接提示词）**：落地 `colleague_workspace`、白名单严格 UTF-8 文本+二进制 `copy_file`、`knowledge_tools.py` 按角色限制、基类 `DbJsonRoute._read_only()` + `CallContext.caller_user_id`、分析师外发与 `keep_updated` 待确认守卫、`CapabilityId.COLLEAGUE_SHARING`（`core_default=False`）及第 1 批提示词。单测覆盖无 BOM UTF-16 拒绝与多编码密钥拦截、`list_workspaces` 双人交集、`SubagentsBackend` `Edit`/删除/重命名拒绝、研究总监 `computer=` 远端写入正常、后续普通轮次跳过未确认 `keep_updated` 刷新；在双进程 Everflow 环境通过真实 `delegate_to_analyst` 验证 **25 个 UTF-8 文本文件逐字节一致**与二进制文件复制到目标工作区。
- **第 2 批（设置页双语文案、技能参考与帮助站文档）**：落地双语文案、`langalpha-doc`、`docs-site` 文档与核心安装能力关闭零残留扫描（`local` 跨成员验收在 Batch 4 接入角色表后执行）。

---

## 9. 待用户确认的问题

所有设计规则均已拍板定稿，**无剩余待确认项**。

---

## 10. 第四轮审查意见处理记录

| 审查编号 | 问题摘要 | 处理方式 |
|---|---|---|
| **Codex R4 高 1 / Grok R4 高 1** | 文本识别会放行含 `NUL` 或无 BOM UTF-16 文件并漏检其中的已知密钥；运行态需复核实际源路径格式。 | **采纳**：§3.2 限定白名单扩展名、严格 UTF-8（可带 BOM）、禁 `NUL` 字节，运行态复核实际源路径格式，同时检查解码文本与 `utf-8`/`utf-16-le`/`utf-16-be` 原始字节密钥。 |
| **Codex R4 高 2** | 同机本地 `Write`/`Edit` 可写到 B 同机上的其他工作区目录。 | 按同机共享系统用户的既有整机信任边界移入 §11「已知问题」并写明后续隔离方向。 |
| **Codex R4 高 3** | 同机进程可从环境读取 B 本人命令的 LiveFS 调用编号。 | 按同机既有整机信任边界移入 §11「已知问题」并写明后续受限用户/内核凭据绑定方向。 |
| **Codex R4 高 4** | 后续普通轮次会自动刷新受限轮次改过的 `keep_updated` 面板；外发守卫不应影响研究总监。 | **采纳**：§7 明确外发守卫仅作用于 `analyst`（研究总监不变），并对受限轮次改过的 `keep_updated` 产物持久化记“待确认”标记，自动刷新前检查，确认后清除。 |
| **Codex R4 中 1 / Grok R4 高 4** | `list_workspaces` 漏了双人交集步骤。 | **采纳**：§3.1 恢复对 A、B 各自取“本人工作区 ∪ `list_colleague_cos_workspaces`”后按 `workspace_id` 取交集，再排除 `Home`/`deleted`/`unbound`。 |
| **Codex R4 中 2 / Grok R4 高 3** | `CallContext` 缺字段与 `outcomes.py` 反序列化回读，且严拒无上下文会伤及主人与研究总监 `computer=` 写入。 | **采纳**：§7 补齐 `livefs_mount.py:50`、`outcomes.py:123` 与 `agent.py:939` `computer=` 分支；上下文标明跨人时只读，查无上下文按现网现状处理。 |
| **Grok R4 高 2** | `cross_user_read_only` 未进基类 `DbJsonRoute._read_only()`，子类与删除/重命名不生效。 | **采纳**：§7 将判断直接写入 `DbJsonRoute._read_only()`（`:698`），覆盖 `awrite_text`、`aedit_text` 及 `DbJsonFolderRoute` 删除/重命名。 |

---

## 11. 已知问题（研究总监与分析师共有 / 既有同机信任边界）

1. **同机工作区共用操作系统用户的信任边界**（`AGENTS.md`、`file_ops.py:381`、`path_resolution.py:140`、`livefs_runtime/daemon.py:251`）：同一台计算机上的多个工作区与 `Home` 共用同一 Linux 用户，现网同事派活轮次下本地 `Write` / `Edit` / `Bash` 可访问 B 同机上的其他工作区目录，同机进程也可能从 `/proc` 读取到 B 本人仍在运行的命令的 LiveFS 调用编号（共享弹窗已有整机信任提示）。本功能不改变该模型；未来若需同机硬隔离，需在本地文件工具入口限制 `W_cur` 路径，并将跨人轮次的沙箱命令改为受限系统用户运行或由执行守护进程绑定内核进程身份。
2. **非所有者入口对 `Home` 替代工作区识别不全**（`sharing.py:920`、`router.py:415,545`）：现网仅调 `is_home_row` 未调 `is_reserved_row`；本功能已在分析师入口拦截，后续建议在共享基础层统一升级。
3. **沙箱路径分步打开竞态、共享撤销落盘时序、像素文字盲区与运行态 SSH 24 MiB 读取上限**（`crud.py:176`、`tools.py:1346,1563,1936`、`_shared.py:341`、`graph.py:383`）：属现网研究总监共性边界，后续另立任务统一演进。
