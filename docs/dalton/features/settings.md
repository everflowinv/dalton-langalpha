# 设置页重组（Settings）

这页讲清设置页（`#360`）重组后的导航分组结构、按用户层级（`minTier`）的页面准入控制、历史路由兼容重映射，以及只读和管理员专属卡片的权限边界。

## 1. 两大导航分组与分段列表

`web/src/pages/Settings/sections.ts` 把所有设置页签收拢成个人和团队两大分组（`GROUPS`），原来空着的 `trading` 和 `experiments` 占位在代码层永久隐藏：

| 分组 | Section ID | 中文定位与内容 | 准入条件 |
|---|---|---|---|
| **Personal（个人设置）** | `account` | 个人资料、账号基础设置 | 所有登录用户 |
| | `preferences` | 偏好设置 | 所有登录用户 |
| | `general` | 通用设置与预算入口、工作区基础偏好 | 所有登录用户 |
| | `chat` | 对话行为与草稿/提示选项 | 所有登录用户 |
| | `model` | 个人默认模型与回退偏好（`ModelAccessSection`） | 所有登录用户（部分管理员控制项受限） |
| | `budget` | 每日美元预算限额与当前消费水位（`DaltonBudgetPanel` / `DaltonBudgetCard`） | 所有登录用户 |
| | `integrations` | 外部工具绑定、个人飞书/Discord 账号绑定、个人 Vault 凭据（`IntegrationsPanel`） | 所有登录用户 |
| | `coverage` | 个人与行业覆盖单元配置（`DaltonCoveragePanel` / `DaltonCoverageCard`） | 仅在后端下发 `coverageUnitsEnabled` 为真时显示 |
| **Team Management（团队管理）** | `teamModels` | 团队共享模型接入状态、原生提供商检查与全局模型开关（`DaltonTeamModelsPanel`） | `minTier: 'admin'` |
| | `teamMembers` | 团队成员分级、邀请链接管理与工作区/记忆共享治理（`DaltonTeamMembersPanel`） | `minTier: 'internal'` 且 `daltonAuthOnly: true`（`external` 和非 Dalton 认证模式不可见） |
| | `teamChannels` | 团队渠道群组管理、飞书可读范围、通知推送开关与运维/系统任务状态（`DaltonTeamOpsPanel`） | `minTier: 'internal'` |

## 2. 路由兼容映射与越权回退

- **历史路由与锚点兼容**：`LEGACY_SECTIONS` 和 `LEGACY_ANCHORS`（`web/src/pages/Settings/sections.ts`）把旧版 `?tab=` 参数（比如老版本拆分前的零散 tab 名）和 `#hash` 锚点无缝重映射到新分组的对应 section 和子卡片位置，老引导链接和帮助文档不会 404。
- **越权深链防御**：低权限成员或外部账号直接访问高权限深链（比如 `?tab=teamModels` 或未开放的 `coverage`）时，`web/src/pages/Settings/Settings.tsx` 会自动拦住并跳回 `?tab=account`，同时弹出无权限提示条，不渲染受限面板 DOM。

## 3. 只读卡片与受限控件清单

为了防止多个入口同时改状态打架、守住团队管理边界，下面这些卡片刻意设成只读，或只对高权限角色开放编辑：

| 卡片 / 组件 | 所在位置 | 约束策略与原因 |
|---|---|---|
| `DaltonSystemJobsCard` | `web/src/pages/Settings/panels/dalton/DaltonSystemJobsCard.tsx`（`teamChannels`） | **严格只读，设置页不能启停或手跑**。只集中展示系统任务运行态和上次结果摘要；启停与重跑全部收口到 Automations 入口。其中 `host` 和 `ops` 作用域任务只给 `admin` 看（前后端双重裁剪），非管理员看不到宿主机运维项。 |
| `ProviderStatusCard` / `NativeModelsCard` | `teamModels` | **仅 `admin` 可见和操作**；`NativeModelsCard` 里的同步、下线等操作按钮在 `!isAdmin` 时强制禁用。 |
| `ProviderRow` | `ModelAccessSection` (`model`) | 非 `admin` 成员看不到管理员运维提示和内部故障诊断细节，避免暴露后台凭据或路由结构。 |
| `DaltonNotifyTargetsCard` | `DaltonTeamOpsPanel` / `IntegrationsPanel` | 只有团队运维面板里 `isAdmin` 为真时，才开放 `allowManualInput` 手填内部目标 ID；普通成员只能选既有绑定或个人绑定通道。 |
| `DaltonCosSharingCard` & `DaltonHomeTeamMemoryCard` | `teamMembers` (`DaltonTeamMembersPanel`) | 严格跟着后端下发的 `can_edit` 权限标志走：`!canEdit` 时只显示只读共享状态，禁用修改开关。 |
| `DaltonFeishuGroupsCard` | `teamChannels` | 查看内部登记飞书群组名单和投递范围（受 `internal`/`admin` 可见边界约束）。 |
