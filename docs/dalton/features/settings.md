# 设置页重组（Settings）

本文说明设置页（`#360`）重组后的导航分组结构、按用户层级（`minTier`）的页面与功能准入控制、历史 URL/锚点兼容重定向，以及只读或管理员专属卡片的设计边界；增删设置面板或调整权限可见范围时应阅读本文。

## 1. 两大导航分组与分段列表

`web/src/pages/Settings/sections.ts` 将所有设置页签整合为个人与团队两大分组（`GROUPS`），原先空置的 `trading` 与 `experiments` 占位于代码层永久隐藏：

| 分组 | Section ID | 中文定位与内容 | 准入条件 |
|---|---|---|---|
| **Personal（个人设置）** | `account` | 个人资料、账号基础设置 | 所有登录用户 |
| | `preferences` | 偏好设置 | 所有登录用户 |
| | `general` | 通用设置与预算入口、工作区基础偏好 | 所有登录用户 |
| | `chat` | 对话行为与草稿/提示选项 | 所有登录用户 |
| | `model` | 个人默认模型与回退偏好（`ModelAccessSection`） | 所有登录用户（部分管理员控制项受限） |
| | `budget` | 每日美元预算限额与当前消费水位（`DaltonBudgetPanel` / `DaltonBudgetCard`） | 所有登录用户 |
| | `integrations` | 外部工具绑定、个人飞书/Discord 账号绑定、个人 Vault 凭据（`IntegrationsPanel`） | 所有登录用户 |
| | `coverage` | 个人与行业覆盖单元配置（`DaltonCoveragePanel` / `DaltonCoverageCard`） | 仅当后端下发 `coverageUnitsEnabled` 为真时显示 |
| **Team Management（团队管理）** | `teamModels` | 团队共享模型接入状态、原生提供商检查与全局模型开关（`DaltonTeamModelsPanel`） | `minTier: 'admin'` |
| | `teamMembers` | 团队成员分级、邀请链接管理与工作区/记忆共享治理（`DaltonTeamMembersPanel`） | `minTier: 'internal'` 且 `daltonAuthOnly: true`（`external` 与非 Dalton 认证模式不可见） |
| | `teamChannels` | 团队渠道群组管理、飞书可读范围、通知推送开关与运维/系统任务状态（`DaltonTeamOpsPanel`） | `minTier: 'internal'` |

## 2. 路由兼容映射与越权回退

- **历史路由与锚点兼容**：`LEGACY_SECTIONS` 与 `LEGACY_ANCHORS`（`web/src/pages/Settings/sections.ts`）将旧版 `?tab=` 参数（例如老版本拆分前的零散 tab 名）及 `#hash` 锚点无缝重映射至新分组下的对应 section 与子卡片位置，避免既有引导链接或帮助文档 404。
- **越权深链防御**：当低权限成员或外部账号直接访问高权限深链（如 `?tab=teamModels` 或未开放的 `coverage`）时，`web/src/pages/Settings/Settings.tsx` 自动拦截并跳转回 `?tab=account`，同时弹出无权限提示条，不渲染受限面板 DOM。

## 3. 只读卡片与受限控件清单

为消除多入口修改状态造成的数据竞争并严守团队管理边界，以下卡片在界面上刻意设为只读或仅对高权限角色开放编辑：

| 卡片 / 组件 | 所在位置 | 约束策略与原因 |
|---|---|---|
| `DaltonSystemJobsCard` | `web/src/pages/Settings/panels/dalton/DaltonSystemJobsCard.tsx`（`teamChannels`） | **严格只读，不可在设置页启停或手跑**。统一展示系统任务运行态与上次结果摘要；启停与重跑全部收口至专门的 Automations 入口。其中 `host` 与 `ops` 作用域任务仅对 `admin` 显示（由后端与前端双重裁剪），非管理员不会看到宿主机运维项。 |
| `ProviderStatusCard` / `NativeModelsCard` | `teamModels` | **仅 `admin` 可见及操作**；`NativeModelsCard` 中的同步或下线等操作按钮在 `!isAdmin` 时强制禁用。 |
| `ProviderRow` | `ModelAccessSection` (`model`) | 非 `admin` 成员隐藏管理员运维提示与内部故障诊断细节，避免暴露后台凭据或路由结构。 |
| `DaltonNotifyTargetsCard` | `DaltonTeamOpsPanel` / `IntegrationsPanel` | 仅当团队运维面板中 `isAdmin` 为真时开放 `allowManualInput` 手动填写内部目标 ID；普通成员仅可选择既有绑定或个人绑定通道。 |
| `DaltonCosSharingCard` & `DaltonHomeTeamMemoryCard` | `teamMembers` (`DaltonTeamMembersPanel`) | 严格遵循后端下发的 `can_edit` 权限标志：`!canEdit` 时仅展示共享状态为只读视图，禁用修改开关。 |
| `DaltonFeishuGroupsCard` | `teamChannels` | 查看内部登记飞书群组名单与投递范围（受 `internal`/`admin` 级可见边界约束）。 |
