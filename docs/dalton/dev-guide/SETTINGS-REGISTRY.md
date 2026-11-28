# 用户可调设置登记指南

本指南说明新增用户可调设置时如何在 `src/dalton/settings_registry.yaml` 中完成登记，并通过守卫测试 `tests/unit/dalton/test_settings_registry.py` 确认研究总监（Chief of Staff）对每项设置的处理方式已经明确拍板。

## 1. 适用范围

凡在以下位置新增或调整用户可调设置时，同步在 `src/dalton/settings_registry.yaml` 的 `settings` 列表中登记：

- 前端偏好与线程字段定义（`web/src/lib/modelPreferences.ts`、`web/src/lib/modelTuning.ts`、`web/src/pages/Onboarding/welcome/profile.ts`、`web/src/lib/threadFieldMutations.ts` 及 `other_preference` 读写模块）；
- 后端 `users` 与 `user_preferences` 字段（`src/server/models/user.py`、`src/llms/preferences.py`、`src/dalton/llm/model_prefs.py`、`src/server/database/user.py` 等）；
- 对话线程可写字段（`ThreadUpdateRequest`、`ThreadVisibilityRequest`、`ThreadShareRequest`）；
- 自动化任务可写字段（`AutomationCreate`、`AutomationUpdate`、`CompanyScriptUpdate`）；
- 工作区、电脑与自部署配置项（`WorkspaceUpdate`、`BrowserChoicesUpdate`、`src/server/services/setup_settings/catalog.py`）；
- Dalton 侧 `PUT` / `PATCH` 设置路由（`src/dalton/app/` 与 `src/dalton/monitors/router.py`）。

## 2. 登记步骤

在 `src/dalton/settings_registry.yaml` 中追加一条记录，按顺序填写以下字段：

```yaml
- key: user_preferences.model_preference.example_key
  stored_in: user_preferences.model_preference.example_key
  web_entry: 设置 → 模型 → 示例区块
  cos: tool:example_tool   # 或 missing，或 excluded:<原因>
  profiles: [everflow, core]
  capability: channel_gateway   # 仅在依赖可选能力时填写
  routes:                       # 仅在对应 Dalton PUT/PATCH 路由时填写
    - PUT /api/v1/dalton/example
```

各字段说明如下：

1. **`key`**：全仓唯一的设置键名，按所属域采用点分命名（例如 `user.*`、`user_preferences.<列>.<键>`、`thread.*`、`automation.*`、`workspace.*`、`dalton.*`、`channels.*`、`deployment_settings.*`）。
2. **`stored_in`**：写明真实落库表与列（或 JSON 路径、Dalton 配置文件项）。
3. **`web_entry`**：写明用户在网页端修改该设置的页面路径与区块名称。
4. **`cos`**：在合入代码时即拍板研究总监的处理方式：
   - `tool:<工具名>`（或 `tool:edit_file(.agents/user/...)`）：研究总监已经具备对应工具或虚拟数据文件编辑能力；
   - `missing`：适合由研究总监在对话中代改、计划后续补齐工具的设置项；
   - `excluded:<原因>`：明确由网页端、服务端或管理员维护的设置项（例如凭据与密钥、账单与预算上限、安全授权开关、管理员专用配置、纯前端外观偏好），直接写明归属原因。
5. **`profiles`**：使用 `src.dalton.profile.DeploymentProfile` 枚举值（`everflow`、`core`）标明该设置出现的部署形态；仅限 Everflow 形态的设置照常登记并标注 `[everflow]`。
6. **`capability`**（可选）：依赖可选能力门控时，填写 `src.dalton.capabilities.CapabilityId` 中对应的枚举值。
7. **`routes`**（可选）：对应 Dalton `PUT` / `PATCH` 设置接口时，按 `METHOD /api/v1/dalton/...` 格式列出完整路由路径。

## 3. 本地校验

登记完成后，运行守卫测试验证所有设置来源与路由均已覆盖、字段格式与工具引用均有效：

```bash
uv run pytest -q tests/unit/dalton/test_settings_registry.py
```

若需要检查新建或修改文件的 Dalton 路径归属与叠加层规则，运行：

```bash
bash scripts/dalton/check_hooks.sh --strict
```
