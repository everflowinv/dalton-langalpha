# 用户可调设置登记指南

本指南说明如何在 `src/dalton/settings_registry.yaml` 中登记用户可调设置，并通过守卫测试 `tests/unit/dalton/test_settings_registry.py` 持续记录研究总监（Chief of Staff）对每项设置的支持状态。

`src/dalton/settings_registry.yaml` 专供开发期维护与单元测试校验使用；运行时代码、提示词模板与工具描述各自直接从代码定义读取配置与能力状态。

## 1. 守卫测试检查范围与同步登记范围

守卫测试自动扫描以下设置来源，并在发现未登记字段或路由时报错：

1. **前端偏好与线程设置**（`web/src/`）：
   - 所有涉及偏好或线程存储的 Zod 模式文件（含递归展开的 `z.object` / `z.looseObject` / `z.strictObject`、`.extend` / `.merge`、对象展开与计算属性）；
   - `ModelPreferences` / `AccountTuning` / `ModelProfile` 接口与 `MOVED_KEYS`、`MODEL_PREF_KEYS`、`TUNING_FIELDS`；
   - `PROFILE_FIELDS`（`web/src/pages/Onboarding/welcome/profile.ts`）与 `ThreadField`（`web/src/lib/threadFieldMutations.ts`）；
   - `web/src/` 全量非测试代码中的 `usePreferenceKey`、`readOtherPreference`、`otherPreferencePatch` 调用点，以及 `{ other_preference: { ... } }`、`{ model_preference: { ... } }` 对象字面量。
2. **后端用户与偏好字段**（`src/`）：
   - `UserUpdate.model_fields` 与 `src.server.database.user.update_user` 参数；
   - `UserPreferencesUpdate` 下全部 Pydantic 偏好模型字段（含 `RiskPreference`、`InvestmentPreference`、`AgentPreference`、`OtherPreference` 与 `ModelPreference`）；
   - 运行时键集（`STRUCTURED_PREFERENCES`、`MOVED_MODEL_KEYS`、`TUNING_FIELDS`、`DALTON_MODEL_PREF_KEYS`、`DEFAULT_OTHER_PREFERENCE`、`_NEVER_MOVED`）；
   - `src/` 中对 `user_preferences` 五个 JSON 列的 Python 字典读写路径与 SQL JSON 路径。
3. **对话线程与自动化任务可写字段**：
   - `ThreadUpdateRequest`、`ThreadVisibilityRequest`、`ThreadShareRequest` 与 `SharePermissions` 全部可写字段；
   - `AutomationCreate`、`AutomationUpdate`、`CompanyScriptUpdate`、`.agents/user/automations/*.json` 虚拟文件 `_EDITABLE` 字段，以及暂停与恢复入口（`POST /api/v1/automations/{automation_id}/pause`、`/resume`）。
4. **工作区、电脑与自部署配置项**：
   - `WorkspaceUpdate`、`AUTO_INGEST_CONFIG_KEY`、`BrowserChoicesUpdate` 与 `src/server/services/setup_settings/catalog.py`。
5. **Dalton 挂载路由与请求体字段**：
   - 按每种部署形态（`everflow`、`core`）实际组装 `src.server.app.setup.app`，提取最终挂载的 Dalton `PUT` / `PATCH` 路径及其 Pydantic 请求体可写字段。

除上述自动扫描来源外，在浏览器 `localStorage`、开放字典载荷（`dict[str, Any]`）或非 `PUT`/`PATCH` 入口新增用户可调设置时，由开发者在同一变更中同步补登到 `src/dalton/settings_registry.yaml`。

## 2. 登记步骤

在 `src/dalton/settings_registry.yaml` 的 `settings` 列表中追加一条记录，按顺序填写以下字段：

```yaml
- key: user_preferences.model_preference.example_key
  stored_in: user_preferences.model_preference.example_key
  web_entry: 设置 → 模型 → 示例区块
  cos: tool:example_tool   # 或 missing，或 excluded:<原因>
  profiles: [everflow, core]
  capability: channel_gateway   # 依赖可选能力时填写
  routes:                       # 对应 Dalton PUT/PATCH 路由时填写
    - PUT /api/v1/dalton/example
  route_fields:                 # 对应接口使用 Pydantic 请求体时填写
    - example_field
  note: 可选补充说明（如成员层级限制或部分动作范围）
```

各字段要求如下：

1. **`key`**：全仓唯一的点分键名（如 `user.*`、`user_preferences.<列>.<键>`、`thread.*`、`automation.*`、`workspace.*`、`dalton.*`、`channels.*`、`deployment_settings.*`）。
2. **`stored_in`**：对照真实数据库迁移与写入 SQL，写明实际落库表名、列名或 JSON 路径（纯前端本地设置写明 `localStorage` 键名）。
3. **`web_entry`**：写明用户在网页端修改该设置的页面路径与区块名称。
4. **`cos`**：在代码合入前明确研究总监对该设置的处理方式：
   - `tool:<工具名>`（或 `tool:edit_file(.agents/user/...)`）：研究总监已经具备对应工具或虚拟数据文件编辑能力；
   - `missing`：计划由研究总监在对话中代改、后续补齐工具的设置项；
   - `excluded:<原因>`：由网页端、服务端或管理员维护的设置项（如凭据与密钥、账单与预算上限、安全授权开关、管理员配置、设备本地与前端外观偏好），写明归属原因。
5. **`profiles`**：使用 `src.dalton.profile.DeploymentProfile` 枚举值（`everflow`、`core`）标明该设置默认出现的部署形态；仅在 Everflow 形态默认启用的设置标注 `[everflow]`。
6. **`capability`**（可选）：依赖可选能力门控时，填写 `src.dalton.capabilities.CapabilityId` 中对应的枚举值。
7. **`routes`** 与 **`route_fields`**（可选）：对应 Dalton `PUT` / `PATCH` 接口时，按 `METHOD /api/v1/dalton/...` 列出最终挂载路径，并在接口使用 Pydantic 请求体时通过 `route_fields` 列出该条目覆盖的可写字段；同一接口上工具支持状态不同的字段分别登记条目并各自填写 `route_fields`。
8. **`note`**（可选）：记录成员层级门控（如 `min_tier=non_external`）、配置前置开关或部分动作范围。

## 3. 核实 `tool:` 条目能够修改对应设置

填写 `cos: tool:<工具名>` 或 `cos: tool:edit_file(...)` 前，按以下步骤核对实现代码：

1. **确认挂载角色与门控**：检查 `src/dalton/tools/__init__.py` 的 `DALTON_TOOL_REGISTRY`（或 `src/tools/secretary/chief_of_staff.py` 的 `CHIEF_OF_STAFF_TOOLS`），确认工具挂在 `chief_of_staff` 角色下，并将注册项中的 `capability` 同步写入登记条目的 `capability` 字段、将 `min_tier` 成员范围与 `setting_gate` 开关写入 `note`。
2. **确认参数与写入路径**：阅读工具函数签名与处理分支，确认该工具接受目标字段作为输入，并实际写入对应数据库列、JSON 键或虚拟文件字段；若工具仅支持读取或仅在特定分支间接修改该字段，将条目登记为 `missing` 并在 `note` 说明现状。
3. **确认虚拟文件可写字段**：填写 `tool:edit_file(.agents/user/...)` 时，对照对应虚拟文件后端的序列化与白名单定义（如 `src/server/services/user_data_io.py`、`src/server/services/automations/file.py`），确认目标字段在可写字段集合内。

## 4. 本地校验

登记完成后，运行守卫测试验证登记格式、工具与能力一致性以及上述扫描范围内的设置项与路由字段：

```bash
uv run pytest -q tests/unit/dalton/test_settings_registry.py
```

检查新增或修改文件的 Dalton 路径归属与叠加层规则：

```bash
bash scripts/dalton/check_hooks.sh --strict
```
