# 外部新用户注册引导全链路盘点与 Dalton 适配方案

本文档盘点 Dalton（产品名 **Everflow**，智能助手 **Dalton**）中**外部新成员**（External Member，权限等级 `tier = "external"`，即不在内部名单 `DALTON_ALLOWED_EMAILS` 中、通过邀请链接注册的用户）与**内部新成员**（Internal Member，`tier = "internal"` / `"admin"`）首次登录到能够正常提问的完整页面、路由判断与约束核对，并给出下一轮步骤裁剪与文案改写建议。

> **状态说明**：本轮已完成第一部分盘点、第三部分建议清单以及**第二部分品牌修正**（将文案与图标中的 `LangAlpha` / `Ginlix` 改为 `{{brand}}` 插值与 `BrandMark`）；第三部分的步骤裁剪与权限改写代码改动待确认后在下一轮实施。

---

## 一、外部新用户注册到可用全链路盘点

在当前 Dalton 部署形态下（前端为自托管开源模式 `VITE_HOST_MODE=oss`、Dalton Supabase 登录 `VITE_DALTON_AUTH=supabase`、品牌构建 `VITE_BRAND_NAME=Everflow` / `VITE_BRAND_MARK=everflow`），外部新用户从注册到发起对话的完整代码走向如下：

```
/invite#<token> (InviteRedeemPage)
  └─ 注册成功 & 自动登录 ─→ /dashboard (AuthenticatedShell)
        └─ useOnboardingRoute + shellTarget
              ├─ 若尚未完成向导 (answered=false & setup_completed=false) ─→ /setup/use (UseStep)
              │     ├─ [!providerConnected] ─→ /setup/model (ModelStep) ─→ /setup/connect (ConnectStep)
              │     │                              └─ ⚠ 当前 teamManagedByAdmin + 后端 403 会在此卡死
              │     └─ [providerConnected]  ─→ /setup/default (DefaultStep)
              │                                    └─ 保存偏好 & setup_completed ─→ /welcome
              └─ 若已跳过/完成 ─→ /welcome (DaltonOnboardingRedirect)
                                      └─ 创建/定位 Home 工作区 ─→ /chat/t/__default__ (dalton-onboarding)
```

### 1.1 逐步流程表（含跳转条件、适用性与处理建议）

| 阶段 / 路由 | 组件与判定位置（文件:行号） | 页面显示与路由条件 | 外部用户是否适用 | 存在问题分类 | 本轮/下轮建议处理 |
|---|---|---|---|---|---|
| **0. 邀请兑换页**<br>`/invite#<token>` | `web/src/lib/dalton/InviteRedeemPage.tsx`<br>`src/dalton/invites.py:689-826` | 从 URL fragment（片段标识符）读取邀请令牌，展示邮箱与角色提示，由用户设置用户名和密码完成创建账号；成功后自动调用 `loginWithEmail` 并跳转 `/dashboard`。已使用 `BrandMark` 与 `BRAND_NAME`。 | **适用** | 无（正常通行；账号写入 `dalton_members` 表，`tier` 为 `external`） | **保留** |
| **1. 根壳路由门禁**<br>`AuthenticatedShell` | `web/src/App.tsx:374`<br>`web/src/hooks/useOnboardingRoute.ts:36-86`<br>`web/src/hooks/useSetupGate.ts:22-77`<br>`web/src/pages/Setup/answers/gate.ts:48-99` | `onboardingRoute()` 读取 `platform`（`isPlatformMode=false`）、`onboarded`（`setup_completed`）、`hasAnswers`（`models` 与 `runs_on`）、`needsSetup`（由 `useSetupGate` 读取已配凭据、`access_tier >= 0`、`deployment_serves`、`nothingOwnToConnect`）。新号 `hasAnswers=false` 时返回 `{ kind: 'questions', unpaired: true }`；`shellTarget()` 因浏览器端 `desktop=false` 且未点跳过（`SKIP_KEY='ginlix_setup_exited'`），重定向至 `/setup/use`。 | **适用**（入口正确，但分支计算依赖的后端数据有缺陷） | **越权与功能隐患**：`src/dalton/credentials.py:53` 的 `credential_user_id()` 未区分 `tier`，把 `external` 也映射到 `DALTON_PM_USER_IDS[0]`。若 PM（管理员）已配置团队凭据，外部用户会被判为 `has_oauth_token=true` / `providerConnected=true`，直接跳过模型连接步骤并使用团队共享凭据。 | **改逻辑**：下轮修复 `credentials.py` 对 `external` 成员的凭据隔离（详见第 2 节与第 4 节）。 |
| **2. 使用方式问答**<br>`/setup/use` | `web/src/pages/Setup/steps/UseStep.tsx`<br>`web/src/pages/Setup/answers/useSetupFacts.ts` | 展示两类问题：<br>①“你打算怎么使用 {{brand}}？”（选项池：`claude_code` / `langalpha_models` / `subscription_or_key`）<br>②“任务在哪里运行？”（`cloud` / `own_computer`）。<br>当前 Dalton 配置下：`claude_code` 因 `config.dalton.yaml:66` 关闭而隐藏；`langalpha_models` 因默认模型为 OAuth 模型（`agent_config.dalton.yaml:10`）使 `deployment_serves_default()` 返回 `false` 而隐藏；`offered_providers: []`（`agent_config.dalton.yaml:29`）使 `own_computer` 隐藏。最终实际只剩 `subscription_or_key` + `cloud` 唯一组合。 | **部分适用**（需要引导定位模型与算力，但当前表单无真实选项） | **品牌**（已修复 `setup.answers.use.title` 等）：原写死 `LangAlpha`。<br>**说法不符**：`cloud` 在文案中叫“云端计算机”，实际后端由部署方本地 Docker 容器（Container Sandbox，沙箱容器）提供；且只剩唯一选项仍显示单选提问页体验冗余。 | **本轮改品牌**；**下轮改写**：在没有可选分支时改为“外部成员启用说明”或清晰告知“计算由部署方提供，模型需使用个人账户”，或隐藏多余选项。 |
| **3. 计算机选择与 SSH 配对**<br>`/setup/computer`<br>`/setup/ssh` | `web/src/pages/Setup/steps/ComputerStep.tsx`<br>`web/src/pages/Setup/steps/SshStep.tsx`<br>`web/src/pages/Setup/answers/setupSteps.ts` | 仅当 `runsOn === 'own_computer'` 时出现在流程条上；供用户选择通过 Mac 客户端远程控制或 SSH 脚本把自己的机器接入平台。由于 `offered_providers: []` 且无桌面端，正常引导不会进入。 | **不适用** | **品牌**（已修复 `setup.answers.computer.*`、`setup.answers.ssh.lead`）。<br>**功能不可用**：若用户手动访问 URL 进入，页面仍保留“更新 LangAlpha 应用以配对 Mac”（已替换为 `{{brand}}`）及 SSH 配置入口，但 Dalton 外部成员无桌面应用且未开放自添 SSH 机器。 | **本轮改品牌**；**下轮隐藏**：在 Dalton 构建或 `!ownOffered` 时保护路由直接跳回下一步，不暴露 Mac 应用更新提示。 |
| **4. Mac 客户端配对系列**<br>`/setup/mac`<br>`/setup/this-mac`<br>`/setup/continue`<br>`ThisMacFirstRun` | `web/src/pages/Setup/mac/MacHandoffStep.tsx`<br>`web/src/pages/Setup/mac/ThisMacStep.tsx`<br>`web/src/pages/Setup/mac/ContinueStep.tsx`<br>`web/src/pages/ThisMac/ThisMacFirstRun.tsx` | 浏览器引导下载 Mac 应用并等待接管（`DESKTOP_DOWNLOAD_URL` 在 Dalton 构建下为 `null`，见 `web/src/config/supportLinks.ts:40`），或桌面端首次运行时询问是否用本机作为工作机（通过 Login Items 登录项配对）。 | **不适用** | **品牌**（已修复 `setup.mac.*`、`setup.thisMac.*`、`thisMacFirstRun.setupHelper` 及 `ThisMacPairing.tsx:75` 硬编码的 `name="LangAlpha"`）。<br>**功能不可用**：Dalton 不发布 Mac 桌面客户端（`DESKTOP_DOWNLOAD_URL = null`），直接访问 `/setup/mac` 下载按钮无有效地址。 | **本轮改品牌**；**下轮隐藏**：当 `DESKTOP_DOWNLOAD_URL === null`（非桌面分发构建）时直接禁用/重定向 `/setup/mac` 等 Mac 专属路由。 |
| **5. 模型连接步骤**<br>`/setup/model`<br>`/setup/connect`<br>`/setup/method`<br>`/setup/provider`<br>`/setup/claude-code` | `web/src/pages/Setup/steps/ModelStep.tsx`<br>`web/src/pages/Setup/steps/ConnectStep.tsx`<br>`web/src/pages/Setup/steps/connectStep/ApiKeyConnect.tsx`<br>`SubscriptionConnect.tsx`<br>`CustomProviderConnect.tsx`<br>`MethodStep.tsx` / `ProviderStep.tsx` / `ClaudeCodeStep.tsx` | 当 `providerConnected === false` 时进入 `/setup/model`，提供连接 OpenAI/Anthropic 等订阅（OAuth）或 API Key；选好后进 `/setup/connect` 填写密钥或授权。遗留路线 `/setup/method` → `/setup/provider` → `/setup/connect` / `/setup/claude-code` 仍在 `SetupWizard.tsx` 注册。 | **核心适用，但当前机制冲突** | **会卡住（严重阻塞）**：<br>① 后端 `src/dalton/credentials.py:85` 的 `require_credential_write_admin()` 在 `team` 模式下对所有非 PM 用户返回 `403`；<br>② 前端 `web/src/hooks/useTeamCredentials.ts:68` 对非 PM 用户算出 `canManage = false`（即 `teamManagedByAdmin = true`），导致 `ApiKeyConnect`、`SubscriptionConnect`、`CustomProviderConnect`、`MethodStep` 隐藏全部填写表单，只展示“由管理员统一配置”；若外部用户无凭据进入 `/setup/model` → `/setup/connect`，将**没有任何可操作表单，永久卡死**。<br>**说法不符 & 品牌**（已修复 token 保存文案）：包含 `setup token`、Claude Code（已关闭）和 `credits`（积分）提示。 | **本轮改品牌**；**下轮必须修复**：解除 `external` 用户的共享凭据读写绑定，允许 `external` 自主添加个人 API Key / OAuth，同时隐藏 `claude-code` 分支与 `credits` 字样。 |
| **6. 默认模型选择**<br>`/setup/default`<br>（及遗留 `/setup/defaults`、`/setup/ready`） | `web/src/pages/Setup/steps/DefaultStep.tsx`<br>`web/src/pages/Setup/answers/defaultChoices.ts`<br>`DefaultsStep.tsx` / `DoneStep.tsx` | 选定主对话模型与后台任务模型（Background Model），保存用户偏好并标记 `setup_completed: true`，随后调用 `toWelcome()` 跳转 `/welcome`。在 OSS 模式下，`useFilteredModels` 依据已连接个人凭据或部署方默认模型过滤可选列表。 | **适用** | **品牌**（已修复 `setup.answers.default.why.deployment` 等）。<br>**说法不符**：当涉及部署方模型或后台模型时，文案提及 `credits`（积分）计费；在无任何个人模型可用时页面会提示无可运行模型。 | **本轮改品牌**；**下轮改写**：删除 `credits` 消耗提示，补充“需先至少连接一个个人模型或使用部署方开放的模型”。 |
| **7. 欢迎与首次访谈**<br>`/welcome` → `/chat/t/__default__` | `web/src/App.tsx:399-400`<br>`web/src/pages/Onboarding/dalton/interview.tsx:50-87`<br>`web/src/pages/Onboarding/welcome/Welcome.tsx`、`WelcomeBanner.tsx` | `isDaltonBuild` 下 `/welcome` 渲染 `DaltonOnboardingRedirect`，自动获取/创建 Home 工作区并跳转到 `/chat/t/__default__`，触发 `dalton-onboarding` 首次对话。上游 3 步投资问卷（`Welcome.tsx`）不再打开，但部分组件（`WelcomeBanner`、`introScenes/chatParts.tsx`）仍在构建和引导中复用。 | **适用** | **品牌**（本轮已将 `chatParts.tsx:16,128` 的 `LangAlphaMark` 替换为 `BrandMark`）。<br>注意：`WelcomeDraft` 使用的浏览器存储键不重命名（见 1.3 节）。 | **保留 Dalton 跳转**；品牌已修正。 |
| **8. 应用内新手任务与页面导览**<br>`GettingStarted`<br>`PageIntroModal`<br>`ChatAppsPromptCard` | `web/src/pages/Onboarding/OnboardingProvider.tsx`<br>`web/src/pages/Onboarding/registry/index.ts:18`<br>`web/src/pages/Onboarding/engine/GettingStarted.tsx`<br>`web/src/pages/Onboarding/engine/PageIntroModal.tsx`<br>`web/src/pages/Onboarding/ChatAppsPromptCard.tsx` | `registry/index.ts:18` 已通过 `DALTON-HOOK(onboarding-tasks)` 过滤掉 `desktopApp` 与 `platformOnly` 任务（`integrations`），浏览器端自动隐藏 `remoteControl`，最终展现 6 项任务（仪表盘、行情、完善个人画像→`/welcome`、新建工作区、首次对话、配置模型）。初次访问对应页面时弹出 `PageIntroModal` 动效导览。 | **适用** | 无明显品牌泄露（`intros/*.json` 已清零）。但“配置模型”任务对于外部用户依赖前述个人凭据可用性；部分任务导览涉及工作区与模型能力，需确保文案符合外部用户权限边界。 | **保留并在下轮联调** `models` 任务跳转体验。 |

### 1.2 外部成员实际约束专项核对

结合代码与 Dalton 部署配置，逐项核对外部成员（`tier = "external"`）的真实运行时约束：

1. **团队共享凭据与外部成员隔离（`src/dalton/credentials.py`）**：
   - **目标规则**：外部成员**不得使用团队共享凭据**，只能使用自己连接的模型凭据或部署方显式开放给外部用户的平台模型。
   - **代码现状（存在缺口）**：`credential_user_id(user_id)`（`src/dalton/credentials.py:53-65`）是同步函数，只判断了 `cfg.enabled`、`cfg.credentials.sharing == "team"`、`is_dalton_supabase_auth_enabled()` 与 `DALTON_PM_USER_IDS`，**没有查询用户层级 `get_user_tier`**；因此它当前同样把外部成员的凭据读写请求指向了 `DALTON_PM_USER_IDS[0]`（主管理员账号）。同时 `require_credential_write_admin()`（`src/dalton/credentials.py:85-100`）拦截所有非 PM 用户写入 API Key / OAuth。若不修改此处，外部用户要么“意外白嫖管理员的团队凭据跳过连接页”，要么在开启隔离后因 `403` 与前端 `canManage=false` **完全无法配置自己的模型**。
2. **计算环境由部署方 Docker 提供（`agent_config.dalton.yaml` / `docker-compose.dalton.yml`）**：
   - `agent_config.dalton.yaml:29` 将计算机提供商限制为仅容器沙箱，且 `offered_providers: []`（不提供个人电脑/SSH 接入），所以沙箱计算资源由服务端部署的 Docker 容器统一提供（即向导里对应的 `cloud` 选项）。
3. **无 Mac 桌面应用（`web/src/config/supportLinks.ts:40`、`web/src/pages/Onboarding/registry/index.ts:18`）**：
   - `DESKTOP_DOWNLOAD_URL = isDaltonBuild ? null : 'https://langalpha.ai/download'`，Dalton 不打包或分发桌面应用，`GettingStarted` 也已隐藏桌面任务。
4. **Claude Code 运行时关闭（`config.dalton.yaml:66-67`）**：
   - `features.claude_code_runtime.enabled` 为 `false`，因此 Claude Code 模式选项及对应的 setup token 流程对所有 Dalton 用户（含外部用户）均关闭。
5. **模型权限与可选模型范围（`src/dalton/access_tiers.py`、`src/server/services/llm/config.py:202-216`、`web/src/hooks/useFilteredModels.ts`）**：
   - `get_user_tier()`（`src/dalton/access_tiers.py:47-105`）对未知或查询失败的成员默认“fail-closed（安全降级）”为 `external`。
   - 当前 `agent_config.dalton.yaml:10` 默认模型为 `claude-opus-5-5-oauth`（OAuth 模型，服务端没有环境变量 API Key），导致 `deployment_serves_default()` 返回 `False`，后端环境变量密钥也未对普通用户作为平台模型开放。
   - **结论**：在“不给外部用户共享团队凭据、且后端不兜底提供免费平台模型”的设定下，**外部成员必须连接至少一个自己的模型账户（API Key 或个人 OAuth 订阅）才能开始对话**。
   - **引导文案应该怎么说**：
     - 在 `/setup/use` 与 `/setup/model` 页明确告知：**“计算环境（沙箱）由 Everflow 部署方提供；作为外部受邀成员，您的对话不共享团队内部模型凭据，需要连接您自己的模型账户（API Key 或个人订阅授权）后方可继续。您的密钥仅加密保存在个人账户下，不会与团队或其他成员共享。”**
     - 如果部署方未来配置了允许外部使用的环境级平台模型（使 `deployment_serves_default()` 为真并开放给 external tier），则需补充第二个选项：“使用平台提供的默认模型，或连接您自己的模型账户”。

### 1.3 本地存储键名与机器路径保留说明（不改）

为避免已有用户的引导进度、草稿与桌面状态丢失，以及保持容器/宿主机目录结构兼容，以下键名与路径名**刻意保留原值，不做品牌替换**：
- 浏览器 `localStorage` 进度镜像键：`langalpha-onboarding-v${ONBOARDING_PREFS_VERSION}`（`web/src/pages/Onboarding/mirror.ts:18`）
- 浏览器 `localStorage` 欢迎流草稿键：`langalpha-welcome-draft:`（`web/src/pages/Onboarding/welcome/draft.ts:33`）
- 浏览器 `localStorage` 跳过向导标记：`ginlix_setup_exited`（`web/src/hooks/useSetupGate.ts`）
- 远程主机/容器运行时目录文本：`~/.langalpha`（如 `computer.alsoDeleteFilesHint`、`computer.filesRuntimeKept`）

---

## 二、内部新成员首登流程差异对比

内部成员（邮箱匹配 `DALTON_ALLOWED_EMAILS`，`tier = "internal"`；若同时在 `DALTON_PM_USER_IDS` 中则为 `"admin"`）仅在以下环节与外部成员不同，其余路由架构与引导框架一致：

| 对比维度 | 外部新成员（`external`） | 内部新成员（`internal` / `admin`） | 涉及代码位置 |
|---|---|---|---|
| **1. 注册与登录入口** | 必须凭 PM 发送的一次性邀请链接 `/invite#<token>` 注册后登录。 | 直接在 `/login` 通过内部邮箱名单校验登录/首登，无需邀请码。 | `web/src/lib/dalton/InviteRedeemPage.tsx`<br>`src/dalton/auth.py` |
| **2. 团队共享凭据继承** | **不可使用**团队共享凭据（修复隔离后 `has_oauth_token`/`has_api_key` 初始为 `false`），必须进入 `/setup/model` → `/setup/connect` 绑定个人凭据。 | **合法共享** PM（管理员）预先配置好的团队共享 API Key / OAuth 凭据。只要团队已配好凭据，首登时 `facts.providerConnected === true`，系统直接**跳过 `/setup/model`**，从 `/setup/use` 直接去往 `/setup/default`。 | `src/dalton/credentials.py:35-100`<br>`web/src/hooks/useTeamCredentials.ts`<br>`web/src/pages/Setup/answers/useSetupFacts.ts` |
| **3. 模型连接表单权限** | 非管理员，但**应当允许管理自己的个人凭据**（下轮需放开个人写入权限，仅禁用团队共享区）。 | 普通内部成员使用团队共享凭据时只读展示“由管理员统一配置”；仅 PM（`admin`）可编辑团队共享凭据。 | `web/src/hooks/useTeamCredentials.ts:68-78`<br>`src/dalton/credentials.py:85` |
| **4. 设置页可见 Tab** | 仅可见**个人设置**标签页，隐藏团队管理模块。 | 可见**团队管理**标签页（团队成员 `teamMembers`、团队频道 `teamChannels`；`admin` 额外可见团队模型 `teamModels`）。 | `docs/dalton/features/settings.md` |
| **5. 记忆、工作区与分享能力** | 仅个人工作区，不接入团队级记忆检索（`conversation_search`），对外发布/分享报告受外部守卫限制。 | 可参与团队工作区协作、使用团队跨线程记忆检索与完整报告分享能力。 | `src/dalton/access_tiers.py` |

---

## 三、下一步步骤裁剪与改写方案清单（待确认，本轮不改代码）

以下清单汇总了确认外部新用户能顺利完成注册引导并开始提问所需的功能与文案改造方案，请确认优先级与细节后在下一轮统一实施：

| 编号 | 改造项 | 改动理由 | 涉及文件与行号 | 潜在风险与注意事项 |
|---|---|---|---|---|
| **P0-1** | **隔离外部成员的共享凭据并放开其个人凭据写入权限** | 当前 `credential_user_id` 把所有人都指到 PM，且 `require_credential_write_admin` 与前端 `canManage` 把所有非 PM 用户都挡在表单外。结果外部用户要么越权消耗团队凭据，要么在隔离后被 `/setup/connect` 彻底卡死。 | 后端：<br>- `src/dalton/credentials.py:35-100`<br>- `src/server/app/api_keys.py`、`oauth.py`、`oauth_native.py`（调用点传入异步 `tier` 或拆分团队凭据读取函数）<br>前端：<br>- `web/src/hooks/useTeamCredentials.ts:22-88`<br>- `src/dalton/app/router.py`（credentials status 接口按 `tier` 返回 `is_shared: false, can_manage: true` 给 `external`） | 1. `credential_user_id` 当前为同步函数且在大量路由/服务层调用，改造成按 `tier` 分流时需逐一梳理后台任务与 SSE 对话时的 `user_id` 传递，避免后台总结或标题生成任务拿不到外部用户的 Key。<br>2. 需同步更新 `external` 成员删除自己凭据的权限，确保不影响团队凭据。 |
| **P0-2** | **在 Dalton 构建 / `claude_code_runtime` 关闭时彻底拦截 `/setup/claude-code` 与 Claude Code 引导分支** | Dalton 已通过 `config.dalton.yaml:66` 关闭 Claude Code，但 `SetupWizard` 仍注册了 `/setup/claude-code` 路由，`ModelStep` 等仍包含其状态切换与 setup token 同意书分支，外部用户若误入或旧进度恢复会看到不可用功能。 | - `web/src/pages/Setup/SetupWizard.tsx`<br>- `web/src/pages/Setup/steps/UseStep.tsx`<br>- `web/src/pages/Setup/steps/ModelStep.tsx`<br>- `web/src/pages/Setup/steps/ClaudeCodeStep.tsx` | 需确认老用户若历史 `onboarding_progress.models === 'claude_code'` 时，前端路由能安全兜底重置为 `'subscription_or_key'`，防止重定向死循环；上游代码需标注 `DALTON-HOOK`。 |
| **P0-3** | **在 Dalton 无桌面端构建下隐藏 Mac 配对与 SSH 引导步骤** | `DESKTOP_DOWNLOAD_URL` 为 `null` 且 `offered_providers: []`，外部成员在网页端没有 Mac 客户端也没有自添 SSH 主机的权限，保留 `/setup/mac`、`/setup/this-mac`、`/setup/continue`、`/setup/computer`、`/setup/ssh` 存在死链接和误导风险。 | - `web/src/pages/Setup/SetupWizard.tsx`<br>- `web/src/pages/Setup/steps/ComputerStep.tsx`<br>- `web/src/pages/Setup/mac/MacHandoffStep.tsx`、`ThisMacStep.tsx`、`ContinueStep.tsx`<br>- `web/src/pages/Setup/answers/setupSteps.ts` | 如果未来 Dalton 重新支持“自连 SSH 电脑”，需将电脑步骤门控挂在 `offered_providers.length > 0` 与 `DESKTOP_DOWNLOAD_URL !== null` 等运行时能力标识上，而不是简单写死 `isDaltonBuild`。 |
| **P1-1** | **改写外部用户在 `/setup/use` 的展示形态与“云端计算机”文案** | 当前 `UseStep` 对 Dalton 外部用户实质上只有 `subscription_or_key` + `cloud` 两个唯一选项，却仍展示成双问题表单，且 “云端计算机” 易与托管平台混淆。 | - `web/src/pages/Setup/steps/UseStep.tsx`<br>- `web/src/locales/zh-CN.json`、`en-US.json`（新增 Dalton 专有提示键或条件文案） | 修改 `UseStep` 行为可能影响上游单测 `UseStep.test.tsx`，建议通过 `isDaltonBuild` / `tier` 开关控制并保留上游默认分支测试通过，加上 `DALTON-HOOK`。 |
| **P1-2** | **清除向导中不适用的 `credits`（积分）与“平台模型”措辞，明确外部成员凭据规则** | 自部署 Dalton 没有上游 LangAlpha 的 `credits` 计费系统；“后台任务在模型上运行会消耗你的 credits”以及“Everflow 的模型”说法与外部成员“只能用个人凭据（或特定平台模型）”的事实不符。 | - `web/src/locales/zh-CN.json`、`en-US.json`：`setup.answers.default.why.*`、`setup.answers.tag.langalpha_models` 等<br>- `web/src/pages/Setup/steps/DefaultStep.tsx`、`UseStep.tsx`、`ModelStep.tsx` | 修改 `en-US.json` / `zh-CN.json` 文案时需同步检查依赖准确英文字符串的 Setup 单测（如 `DefaultStep.test.tsx`、`ModelStep.test.tsx`），并在 `keys.test.ts` 中保持占位符和术语规范一致。 |
| **P2-1** | **校验引导流终点与 `GettingStarted` 任务联动** | 对外部用户而言，若初次跳过 `/setup` 直接进入仪表盘，右侧/侧边栏 `GettingStarted` 的第 6 项 `models` 任务应能把用户带往“个人模型设置”页并允许其配置个人 API Key / OAuth，而不是看到只读的“团队托管”提示。 | - `web/src/pages/Onboarding/registry/gettingStarted.ts`<br>- 设置页模型接入相关组件 | 依赖 P0-1 的 `useTeamCredentials` 权限分流先落地；否则即使跳转到设置页同样无法添加个人模型。 |
