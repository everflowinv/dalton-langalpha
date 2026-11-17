# PS.0 预加载、子智能体与 Workflow 挂载验证规范（`PS0-preload`）

本文件记录 PS.0 要求的三项基础设施验证规程与结论：
1. 自动任务文件方式是否通过斜杠命令自动预加载对应 Dalton skill；
2. Workflow `agent()` 的 `agentType` 是否支持随包插件子智能体（`dalton-evidence`、`dalton-reviewer`）；
3. 仓库根目录 `workflows/<name>/workflow.js` 的 QuickJS 编译校验与挂载生效机制。

---

## 1. 静态与单测核实结果（已验证）

| 检查项 | 核实位置 | 结论 |
|---|---|---|
| **仓库级 Workflow 注册表编译校验** | `src/ptc_agent/agent/middleware/background_subagent/workflow/prebuilt.py` | 6 个自带 workflow（`initial-coverage`, `full-coverage`, `bullbear-doc`, `earnings-deep-update`, `weekly-coverage-review`, `topic-research`）全部通过 `PrebuiltWorkflowRegistry` 的 QuickJS `compile_check`，`meta.name` 与目录名完全匹配 |
| **Workflow Schema 嵌套与大小限制** | `src/ptc_agent/agent/middleware/background_subagent/workflow/validation.py` | 所有 workflow 派发的 `opts.schema` 均满足 `schema_max_bytes <= 4096`、`schema_max_depth <= 5`、`schema_max_properties <= 32`，且不含禁用关键字（`pattern`, `uniqueItems`, 外部 `$ref`） |
| **双保险 Skill 预加载指令** | `docs/dalton/dev-guide/automation-templates/*.json` | 所有自动任务模板的 `instruction` 既以斜杠命令（如 `/dalton-earnings-preview ...`）开头，又显式包含 `先读 .agents/skills/<name>/SKILL.md` 兜底，无论走文件写入还是 `PUT /api/v1/dalton/automations/{name}` REST helper 均保证 skill 100% 加载 |

---

## 2. 联调实测步骤（`make up` 全栈环境）

### 2.1 自动任务预加载探针（`dalton-meta-v<YYYYMMDD>-preload-probe.json`）
在 `META` 工作区写入 `.agents/user/automations/dalton-meta-v20261018-preload-probe.json`：
```json
{
  "name": "PS0 预加载探针",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "<当前时间+3分钟，带时区偏移>",
  "timezone": "Asia/Shanghai",
  "instruction": "/dalton-company-background META —— 先读 .agents/skills/dalton-company-background/SKILL.md。只回答：你是否已加载 dalton-company-background skill？列出你读过的 SKILL.md 路径。不要做研究，收尾写一条 NO_CHANGE（trigger.kind=manual）。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new"
}
```
- **检查项**：触发后查看新线程元数据及 Read 工具调用，确认 `.agents/skills/dalton-company-background/SKILL.md` 与 `.agents/skills/dalton-conventions/SKILL.md` 已读取，且 `verdicts.md` 新增一条 `NO_CHANGE`。

### 2.2 插件子智能体 `agentType` 探针
在任意工作区验证 `dalton-evidence` 与 `dalton-reviewer` 出现在 `bundled_agent_plans()` 与 `dispatcher.subagent_types` 中：
- `plugins/dalton/agents/dalton-evidence.md` 由 `src/server/services/plugins/bundled.py: bundled_agent_plans()` 加载并合并进 `known_subagent_types`。
- 修改 `plugins/dalton/agents/*.md` 后执行 `docker compose restart backend` 清除 `lru_cache`。
