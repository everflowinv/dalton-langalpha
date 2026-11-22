# 设置页结构与权限边界（Settings）

这页讲清设置页重组后的导航分组结构、按用户层级（`minTier`）的页面准入控制、历史路由兼容重映射，以及只读和管理员专属卡片的权限边界。

## 1. 两大导航分组与分段列表

`web/src/pages/Settings/sections.ts` 把所有设置页签收拢成**个人设置**和**团队管理**两大分组（`GROUPS`），上游的 `trading` 和 `experiments` 占位在代码层永久隐藏：

| 分组 | Section ID | 界面名称与内容 | 准入条件 |
|---|---|---|---|
| **个人设置（Personal）** | `account` | **个人资料**：头像、姓名、邮箱、退出登录 | 所有登录用户 |
| | `preferences` | **投资画像**：投资偏好问答与重置 | 所有登录用户 |
| | `general` | **通用与外观**：语言、时区、主题、字号、卡片样式、使用引导（及桌面端专属选项） | 所有登录用户 |
| | `chat` | **对话与助手**：对话流式/展示行为、输出格式、自动交接审批、研究总监协同共享（`DaltonCosSharingCard`，由 `colleague_dispatch.enabled`（代码默认 `false`）/ `DALTON_COLLEAGUE_DISPATCH_ENABLED` 控制，本生产部署已开启）与 Home 团队记忆个人开关（`DaltonHomeTeamMemoryCard`，默认关闭） | 所有登录用户（部分卡片按权限显隐） |
| | `model` | **模型**：个人默认模型、常用模型、备用列表与高级后台模型路由（`ModelAccessSection`） | 所有登录用户（团队共享模式下凭据读写受限） |
| | `budget` | **用量与预算**：每日美元预算限额与当前消费水位（`DaltonBudgetPanel` / `DaltonBudgetCard`） | 所有登录用户 |
| | `integrations` | **消息与渠道**：个人推送总开关与目标（`DaltonNotifyTargetsCard`）、飞书/Discord 账号绑定（`DaltonChannelBindingsCard`）、个人微信绑定（`DaltonWeixinBindCard`） | 所有登录用户 |
| | `coverage` | **覆盖结构**：个人与行业覆盖单元、未归类工作区管理（`DaltonCoveragePanel` / `DaltonCoverageCard`） | 仅在后端下发 `coverageUnitsEnabled` 为真时显示 |
| **团队管理（Team Management）** | `teamModels` | **模型与供应商**：团队共享模型接入（`ModelAccessSection scope="team"`）、供应商状态、原生模型目录同步（`DaltonTeamModelsPanel` / `ProviderStatusCard` / `NativeModelsCard`） | `minTier: 'admin'` |
| | `teamMembers` | **成员与权限**：团队邀请链接（`DaltonInvitesCard`）、飞书读取范围授权（`FeishuReadScopesCard`，仅 `admin` 可见） | `minTier: 'internal'` 且 `daltonAuthOnly: true`（`external` 和非 Dalton 认证模式不可见） |
| | `teamChannels` | **团队渠道与运维**：团队微信机器人（`admin`）、飞书团队群（`DaltonFeishuGroupsCard`）、Discord 团队频道（`admin`）、团队通知目标手填（`admin`）与系统任务只读看板（`DaltonSystemJobsCard`） | `minTier: 'internal'` |

## 2. 路由兼容映射与越权回退

- **历史路由与锚点兼容**：`LEGACY_SECTIONS` 和 `LEGACY_ANCHORS`（`web/src/pages/Settings/sections.ts`）把旧版 `?tab=` 参数（如 `appearance`、`desktop`、`agent`、`artifacts`、`plan`、`dalton`、`models`、`team-models` 等）和 `#hash` 锚点无缝重映射到新分组的对应 section 和子卡片位置，确保老引导链接正常跳转。
- **越权深链防御**：低权限成员或外部账号直接访问高权限深链（比如 `?tab=teamModels` 或未开放的 `coverage`）时，`web/src/pages/Settings/Settings.tsx` 会自动拦住并跳回 `?tab=account`，同时弹出无权限提示条，不渲染受限面板 DOM。

## 3. 只读卡片与受限控件清单

为了防止多个入口同时改状态打架、守住团队管理边界，下面这些卡片刻意设成只读，或只对高权限角色开放编辑：

| 卡片 / 组件 | 所在位置 | 约束策略与原因 |
|---|---|---|
| `DaltonSystemJobsCard` | `teamChannels`（`DaltonTeamOpsPanel`） | **严格只读**，集中展示系统任务运行态和上次结果摘要；启停与重跑全部收口到 Automations 入口。其中 `host` 和 `ops` 作用域任务仅对 `admin` 展示（前后端双重裁剪）。 |
| `ProviderStatusCard` / `NativeModelsCard` | `teamModels`（`DaltonTeamModelsPanel`） | **仅 `admin` 可见和操作**；`NativeModelsCard` 里的同步、下线等操作按钮在 `!isAdmin` 时强制禁用。 |
| `ProviderRow` | `teamModels`（`ProviderStatusCard`） | 运维提示（`credential.hint`）在组件内按 `isAdmin` 展示；最近错误由组件直接渲染，整页访问由 `minTier: 'admin'` 限定为管理员。 |
| `DaltonNotifyTargetsCard` | `teamChannels`（`DaltonTeamOpsPanel`）/ `integrations`（`IntegrationsPanel`） | 只有团队运维面板里 `isAdmin` 为真时，才开放 `allowManualInput` 手填内部目标 ID；普通成员在个人“消息与渠道”面板只能选既有绑定或个人绑定通道。 |
| `DaltonCosSharingCard` & `DaltonHomeTeamMemoryCard` | `chat`（`ChatPanel`） | 按后端下发的权限标志控制显隐与可编辑态：`!canEdit` 时只显示只读状态，禁用修改开关。 |
| `FeishuReadScopesCard` | `teamMembers`（`DaltonTeamMembersPanel`） | 仅 `admin` 可见并管理飞书知识库/文档读取范围授权。 |
| `DaltonFeishuGroupsCard` / `DaltonDiscordChannelsCard` / `DaltonWeixinBotCard` | `teamChannels`（`DaltonTeamOpsPanel`） | `DaltonFeishuGroupsCard` 对 `internal`/`admin` 可见；`DaltonDiscordChannelsCard` 与 `DaltonWeixinBotCard` 仅 `admin` 可见。 |
