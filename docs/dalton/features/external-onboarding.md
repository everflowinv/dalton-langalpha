# 外部新用户注册引导全链路盘点与 Dalton 适配方案

本文档记录 Dalton（产品名 **Everflow**，智能助手 **Dalton**）中**外部新成员**（External Member，`tier = "external"`，通过邀请链接注册）与**内部新成员**（Internal Member，`tier = "internal"` / `"admin"`）首次登录到正常提问的完整引导链路与实现规则。

> **状态说明**：已完成品牌替换（`{{brand}}` 插值与 `BrandMark`）、团队共享凭据首登路由裁剪、默认推荐模型预选、旧进度恢复兼容、以及向导中 Claude Code / Mac 配对 / 积分（credits）文案与分支的过滤。

---

## 一、外部新用户注册到可用全链路流程

在当前 Dalton 部署形态下（`VITE_HOST_MODE=oss`、Dalton Supabase 登录 `VITE_DALTON_AUTH=supabase`、品牌构建 `VITE_BRAND_NAME=Everflow` / `VITE_BRAND_MARK=everflow`），外部成员与普通内部成员均可使用团队共享凭据。首次登录的完整代码走向如下：

```
/invite#<token> (InviteRedeemPage)
  └─ 注册成功 & 自动登录 ─→ /dashboard (AuthenticatedShell)
        └─ useOnboardingRoute + onboardingRoute / shellTarget
              ├─ 新号 (hasAnswers=false) ─→ /setup/use (UseStep)
              │     ├─ [团队开启共享且成员有可用模型] ─→ 保存 models='langalpha_models', runs_on='cloud'
              │     │                                      └─ /setup ─→ nextSetupStep='default' ─→ /setup/default (DefaultStep)
              │     │                                            └─ 预选 neon-high + hydrogen-high ─→ 保存并完成 ─→ /welcome (DaltonOnboardingRedirect) ─→ /chat/t/__default__
              │     ├─ [团队开启共享但无可用模型] ─→ 显示「团队暂未配置可用模型，请联系管理员」+「重试」，禁用继续与完成保存
              │     └─ [团队共享关闭] ─→ 显示个人连接选项 (subscription_or_key) ─→ /setup/model ─→ /setup/connect ─→ /setup/default ─→ /welcome
              ├─ 旧进度恢复 (hasAnswers=true & 未完成)
              │     ├─ [团队模型可用 + subscription_or_key / claude_code] ─→ 统一按 langalpha_models 处理 ─→ /setup/default（不循环）
              │     └─ [setup_on='mac_pending'] ─→ 在 Dalton 下转回浏览器流程，允许 DefaultStep 继续并完成
              └─ 已完成或跳过 ─→ /welcome (DaltonOnboardingRedirect)
                                  └─ 创建/定位 Home 工作区 ─→ /chat/t/__default__ (dalton-onboarding)
```

### 1.1 关键路由与组件说明

| 阶段 / 路由 | 核心实现位置 | 实际行为与 Dalton 规则 |
|---|---|---|
| **0. 邀请兑换页**<br>`/invite#<token>` | `web/src/lib/dalton/InviteRedeemPage.tsx`<br>`src/dalton/invites.py` | 从 URL fragment 读取邀请令牌，展示邮箱与角色提示，设置用户名和密码完成创建账号并自动登录跳转 `/dashboard`；使用 `BrandMark` 与 `BRAND_NAME`。 |
| **1. 根壳路由门禁**<br>`AuthenticatedShell` / `/setup` | `web/src/hooks/useOnboardingRoute.ts`<br>`web/src/pages/Setup/answers/gate.ts`<br>`web/src/pages/Setup/answers/setupSteps.ts`<br>`web/src/pages/Setup/answers/useSetupFacts.ts`<br>`web/src/pages/Setup/answers/flow.tsx` | `useSetupFacts` 通过统一的 `useDaltonTeamModels()` 判断成员是否**实际拥有可用团队模型**。Dalton 构建下：`shellTarget` 不再跳转到 `/setup/mac`、`/setup/this-mac` 或 `/setup/continue`；旧进度 `setup_on='mac_pending'` 在 `onboardingRoute`、`flow.tsx` 与 `DefaultStep` 中均按 `'browser'` 处理；团队模型可用时，`effectiveModelsAnswer()` 将旧进度 `subscription_or_key` / `claude_code` 统一视作 `langalpha_models`，确保 `nextSetupStep`、`ModelStep`、`DefaultStep` 判断一致且无跳转循环。 |
| **2. 使用方式问答**<br>`/setup/use` | `web/src/pages/Setup/steps/UseStep.tsx`<br>`web/src/pages/Setup/answers/useTeamModels.ts` | - 团队共享开启且成员实际可用模型非空：隐藏个人订阅/API Key 与 Claude Code 选项，直接显示「团队已配置好模型」，默认选中云端运行环境（若后端提供 SSH 则折叠于「高级」下），点击继续写入 `models: 'langalpha_models', runs_on: 'cloud'` 并直接进入 `/setup/default`。<br>- 团队共享开启但可用模型为空：显示「团队暂未配置可用模型，请联系管理员」并提供「重试」按钮，禁用继续按钮，阻止进入或完成设置。<br>- 团队共享关闭：仅展示个人连接（`subscription_or_key`）和允许的运行环境。 |
| **3. 计算机与 Mac 路由拦截**<br>`/setup/computer`、`/setup/mac`、`/setup/this-mac`、`/setup/continue`、`/setup/claude-code` | `web/src/pages/Setup/SetupWizard.tsx`<br>`web/src/pages/Setup/steps/ComputerStep.tsx` | `SetupWizard` 在 Dalton 下将 `/setup/mac`、`/setup/this-mac`、`/setup/continue`、`/setup/claude-code` 直接重定向回 `/setup`。`ComputerStep` 默认展示托管云端计算并支持切换为 `runs_on: 'cloud'` 前往 `/setup/default`，隐藏 Mac 配对入口（SSH 仅在服务端提供时折叠于「高级」）。 |
| **4. 模型连接与旧版入口**<br>`/setup/model`、`/setup/method`、`/setup/provider`、`/setup/models` | `web/src/pages/Setup/steps/ModelStep.tsx`<br>`web/src/pages/Setup/steps/MethodStep.tsx`<br>`web/src/pages/Setup/steps/ProviderStep.tsx`<br>`web/src/pages/Setup/steps/ModelPickStep.tsx`<br>`web/src/components/modelAccess/useModelPicker.ts` | - `ModelStep` 使用同一个 `useDaltonTeamModels()`：团队模型可用时直接跳 `/setup/default`；团队共享开启但无可用模型且当前用户不可管理凭据时，提示联系管理员并提供「重试」（不跳回 `/setup/default`，避免循环）。<br>- `MethodStep` 在 Dalton 下隐藏 Claude Code 推荐文案（使用 `setup.methodOAuthDescDalton`）；`ProviderStep` 不展示 Claude Code 入口。<br>- 旧版 `/setup/models`（`ModelPickStep` -> `useModelPicker(..., { dalton: isDaltonBuild })`）过滤掉 `claude-code` 与 `credits` 模型，不得选中或保存。 |
| **5. 默认模型选择**<br>`/setup/default`（及旧版 `/setup/defaults`、`/setup/ready`） | `web/src/pages/Setup/steps/DefaultStep.tsx`<br>`web/src/pages/Setup/answers/defaultChoices.ts`<br>`web/src/config/daltonSetup.ts`<br>`web/src/pages/Setup/steps/DefaultsStep.tsx`<br>`web/src/pages/Setup/steps/DoneStep.tsx` | - 通过 `filterDaltonProviderModels`、`filterDaltonPayers` 与 `isDaltonAllowedModel` 过滤所有需要 `credits` 或 `claude-code` 的模型与部署默认后台模型（包括后台模型的「自动」选项与高级路由模型，不得选中/保存）。<br>- 主对话模型默认推荐 `gemini-4-neon-high-oauth`，后台模型默认推荐 `gemini-4-hydrogen-high-oauth`。<br>- 当可用模型池为空时：`held = true` 且 `setUp()` 拒绝保存完成；若团队共享已开启，则展示「团队暂未配置可用模型，请联系管理员」并提供「重试」。<br>- `setup_on='mac_pending'` 不会导致「继续」按钮被禁用。 |
| **6. 欢迎页、首次访谈与页面导览**<br>`/welcome` → `/chat/t/__default__` | `web/src/App.tsx`<br>`web/src/pages/Onboarding/dalton/interview.tsx`<br>`web/src/pages/Onboarding/registry/index.ts`<br>`web/src/pages/Onboarding/registry/pageIntros.ts` | - Dalton 下 `/welcome` 渲染 `DaltonOnboardingRedirect`，自动定位 Home 工作区并进入 `/chat/t/__default__` 发起 `dalton-onboarding` 首次访谈（无券商/交易步骤）。<br>- `DEPLOYED_TASKS` 隐藏 `desktopApp`、`remoteControl` 与 `integrations`；`DEPLOYED_INTROS` 通过 `hideInDalton` 隐藏工作区导览中「通过远程控制添加 Mac」的步骤（`workspace`）。 |

---

## 二、外部与内部成员首登权限与模型能力对比

| 对比维度 | 外部新成员（`external`） | 内部新成员（`internal` / `admin`） | 相关位置 |
|---|---|---|---|
| **1. 注册与登录入口** | 通过 PM 发送的一次性邀请链接 `/invite#<token>` 注册后自动登录。 | 在 `/login` 通过内部邮箱名单登录。 | `web/src/lib/dalton/InviteRedeemPage.tsx`<br>`src/dalton/auth.py` |
| **2. 团队共享凭据使用** | 团队共享开启且已配好可用模型时，**可直接共享使用团队凭据**并跳过连接账户步骤，直接进入默认模型选择；无可用模型时阻止完成设置并提示联系管理员。 | 同样直接使用团队共享凭据（若 `admin` 已配置可用模型则跳过连接；若未配置可前往 Settings → 团队模型 配置）。 | `web/src/pages/Setup/answers/useTeamModels.ts`<br>`web/src/pages/Setup/steps/UseStep.tsx` |
| **3. 推荐默认模型** | 默认主模型 `gemini-4-neon-high-oauth`，后台模型 `gemini-4-hydrogen-high-oauth`；隐藏所有 `credits` 与 `claude-code` 模型。 | 同左。 | `web/src/config/daltonSetup.ts`<br>`web/src/pages/Setup/answers/defaultChoices.ts` |
| **4. 设置页可见 Tab** | 仅可见**个人设置**，隐藏团队管理模块。 | 可见**团队管理**标签页；仅 `admin` 可见并管理团队模型凭据（`teamModels`）。 | `web/src/pages/Settings/sections.ts`<br>`docs/dalton/features/settings.md` |

---

## 三、本地存储键名与机器路径保留说明（不改）

为避免已有用户的引导进度、草稿与桌面状态丢失，以及保持目录结构兼容，以下键名与路径名刻意保留原值，不做品牌替换：
- 浏览器 `localStorage` 进度镜像键：`langalpha-onboarding-v${ONBOARDING_PREFS_VERSION}`（`web/src/pages/Onboarding/mirror.ts:18`）
- 浏览器 `localStorage` 欢迎流草稿键：`langalpha-welcome-draft:`（`web/src/pages/Onboarding/welcome/draft.ts:33`）
- 浏览器 `localStorage` 跳过向导标记：`ginlix_setup_exited`（`web/src/hooks/useSetupGate.ts`）
- 远程主机/容器运行时目录文本：`~/.langalpha`（如 `computer.alsoDeleteFilesHint`、`computer.filesRuntimeKept`）
