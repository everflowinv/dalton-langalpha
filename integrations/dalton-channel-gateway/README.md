# Dalton Channel Gateway (`@dalton/channel-gateway`)

Dalton 多渠道接入网关（监听 `127.0.0.1:8962`）与平台原生命令菜单注册工具。后端接口对照表见 [`PROTOCOL.md`](./PROTOCOL.md)，第三方代码归属见 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。

## 构建与检查

在 `integrations/dalton-channel-gateway` 目录下执行：

```bash
pnpm typecheck   # TypeScript 严格类型检查
pnpm build       # 生成 dist/index.mjs（网关服务）与 dist/native-commands.mjs（原生命令注册 CLI）
pnpm test        # 运行 Vitest 单元测试
```

`build.mjs` 同时构建两个独立入口并做无副作用导入校验，构建或导入模块时不会发起网络请求或注册命令。

---

## 平台原生命令菜单注册（`dalton-native-commands`）

> **部署顺序说明**：当前仅提供共享固定命令清单、飞书/Discord 平台菜单注册库与独立 CLI。原生命令入站事件解析与会话接入尚未合并部署，真实应用的菜单注册尚未执行；须待入站原生命令处理器合并并部署就绪后，再由主助手在目标环境执行菜单预览与 `--apply` 注册，并进行真实平台联调验证。

### 固定原生命令清单

共享命令清单（`src/native-commands/manifest.ts`）固定包含以下 4 条命令，不包含任何私人工作区名称：

| 命令名 | 中文说明 | 参数结构 |
|---|---|---|
| `main` | 返回 Chief of Staff | 无参数 |
| `workspace` | 查看可访问的工作区 | Discord：1 个可选 `STRING` 参数 `workspace`（`autocomplete: true`，无静态选项）；飞书：使用平台支持的命令描述结构 |
| `new` | 在当前工作区新建会话 | 无参数 |
| `stop` | 停止当前任务 | 无参数 |

### 凭据与平台权限要求

注册工具仅读取 Dalton 网关专用环境变量，不读取 OpenClaw 配置或环境文件，且默认连接固定可信官方 API 域名：

1. **飞书（`https://open.feishu.cn`）**
   - 环境变量：`DALTON_FEISHU_APP_ID`、`DALTON_FEISHU_APP_SECRET`（获取 `tenant_access_token` 时严格绑定该 `DALTON_FEISHU_APP_ID`）。
   - 应用权限：
     - 预览读取：`application:app_slash_command:read`
     - 执行写入（`--apply`）：`application:app_slash_command:write`
   - 客户端缓存：飞书服务端列表在写入后立即更新，飞书客户端命令面板约有 **5 分钟**本地缓存。

2. **Discord（`https://discord.com/api/v10`）**
   - 环境变量：`DALTON_DISCORD_BOT_TOKEN`、`DALTON_DISCORD_APPLICATION_ID`。
   - 身份核验：每次运行前先调用 `GET /api/v10/oauth2/applications/@me` 校验当前 Bot Token 所属应用 ID 与 `DALTON_DISCORD_APPLICATION_ID` 完全一致，不通过解码 Token 推断应用 ID。
   - 权限与上下文：
     - 应用需具备 `applications.commands` 权限（指定测试服务器时机器人须已加入该服务器）。
     - 默认注册**全局命令**，显式启用服务器频道（`GUILD = 0`）与机器人私聊（`BOT_DM = 1`），使用既有机器人安装方式（`GUILD_INSTALL = 0`）。
     - 指定 `--guild-id <SNOWFLAKE>` 时仅针对该测试服务器路径同步，不包含机器人私聊全局菜单。

### 操作步骤

1. **只读预览（默认，不产生任何写入）**

   ```bash
   # 飞书预览
   node dist/native-commands.mjs --platform feishu

   # Discord 全局预览（服务器 + 机器人私聊）
   node dist/native-commands.mjs --platform discord

   # Discord 指定测试服务器预览
   node dist/native-commands.mjs --platform discord --guild-id <GUILD_ID>
   ```

2. **执行注册（显式传入 `--apply`）**

   确认入站原生命令处理器已在目标环境部署就绪、且预览变更符合预期后执行：

   ```bash
   # 飞书应用注册
   node dist/native-commands.mjs --platform feishu --apply

   # Discord 全局注册（支持服务器与机器人私聊）
   node dist/native-commands.mjs --platform discord --apply

   # Discord 指定测试服务器注册
   node dist/native-commands.mjs --platform discord --guild-id <GUILD_ID> --apply
   ```

   需要机器可读输出时可附加 `--json`。也可通过 `pnpm native-commands -- --platform <feishu|discord> ...` 调用。

### 幂等、安全与异常语义

- **只增改托管命令**：按固定名称（Discord 同时核对 `CHAT_INPUT` 类型 `type=1`，并在全局与服务器列表查询及全部分页中固定携带 `with_localizations=true`）精确定位，仅对新增或变更的托管字段执行 `POST` 或 `PATCH`，绝不使用批量覆盖（`PUT`）或删除其他未管理命令，并保留未管理的本地化翻译与图标字段。
- **分页与冲突保护**：若列表接口返回 `has_more` / `page_token`，必须完整拉取全部分页后才计算差异；若出现重复分页游标、超页数上限、同名同类型重复命令或缺失有效命令 ID，立即报错中止且不执行写入。
- **状态核实与部分执行可观测**：`POST` / `PATCH` 发生超时、连接中断或响应缺少平台确认结构时不会盲目重发创建请求，而是回查平台当前命令列表确认实际状态；若无法确认最终状态，返回 `unconfirmed` 结果与退出码 `4`。若批量执行中途遇到权限、额度或预算等明确失败，输出会完整保留此前已完成的命令结果，并明确标注当前失败项与后续跳过项。
- **流隔离与退出码**：CLI 运行日志统一输出至 `stderr`，`--json` 模式下 `stdout` 仅输出单个可解析 JSON 文档。退出码：`0` 成功；`1` 网络/限流/配额/分页/冲突错误；`2` 参数错误；`3` 配置缺失/认证失败/身份不匹配/权限不足；`4` 写入状态未确认。所有输出、错误对象与日志均脱敏处理，不含 `App Secret`、`Bot Token` 或动态 `tenant_access_token`。
