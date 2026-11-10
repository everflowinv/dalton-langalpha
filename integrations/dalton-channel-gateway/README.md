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

## 网关服务启动、Vault 凭据引导与数据任务群组报告推送

### 1. 启动模式与无明文飞书凭据机制

网关监听本地回环地址 `127.0.0.1:8962`（拒绝任何非 `127.0.0.1` 绑定），支持两种运行模式：

- `DALTON_GATEWAY_MODE=outgoing`（或 CLI 参数 `--outgoing-only` / `--mode=outgoing`）：专用于出站通知与数据任务报告投递，不启动飞书 WebSocket 长连接、不加载微信状态、不执行入站会话恢复。
- `DALTON_GATEWAY_MODE=incoming`（默认）：同时启动入站消息监听、恢复扫描与出站投递。

生产环境下飞书专用应用凭据（`DALTON_FEISHU_APP_ID`、`DALTON_FEISHU_APP_SECRET`）不在 LaunchAgent plist、环境变量文件、仓库配置或命令行参数中落盘明文：

1. **后端受控内部接口**：`POST /api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials` 要求 `X-Service-Token` 鉴权，拒绝任何调用方传入的查询参数、`X-User-Id` 请求头或额外 JSON 字段，仅按服务端配置的唯一主管理员账号（`DALTON_PM_USER_IDS[0]`）从 Dalton Vault（`user_vault_secrets`）解密读取固定的两项键名，并设置 `Cache-Control: no-store`。
2. **启动期内存加载（Fail-Closed）**：当 `DALTON_FEISHU_CREDENTIAL_SOURCE=vault` 时，`prepareGatewayRuntime()` 在构造适配器与监听端口（输出就绪日志）**之前**以有界超时、`redirect: "error"` 与 `cache: "no-store"` 拉取并校验这两项非空凭据，仅存于当前进程内存中的 `FeishuChannelAdapter` 实例。若 Vault 不可达、鉴权失败、重定向、JSON 异常或任一凭据缺失，启动立即失败退出，绝不静默回退到环境变量或 OpenClaw 凭据，且日志与错误消息至多包含凭据键名，绝不输出凭据值或原始响应体。
3. **服务鉴权令牌文件**：LaunchAgent 模板（`scripts/dalton/launchd/com.dalton.channel-gateway.plist.template`）通过 `DALTON_CHANNEL_GATEWAY_ENV_FILE` 指向权限严格为 `0600` 的普通文件 `~/.config/dalton/channel-gateway.env`（拒绝符号链接或组/其他用户可读权限）提供 `INTERNAL_SERVICE_TOKEN`；在 `vault` 模式下，该文件若出现明文飞书凭据键将被直接拒绝。

### 2. 飞书专用凭据安全刷新与重启流程

网关运行期间不会在投递中途热替换内存凭据，以保证正在进行的卡片或图表上传原子一致。更新飞书凭据的标准步骤：

1. 主管理员在 Dalton Web 设置的 Vault 中更新 `DALTON_FEISHU_APP_ID` 与/或 `DALTON_FEISHU_APP_SECRET`。
2. 由运维/主助手在确认无在途关键投递窗口后执行受控重启（例如 `launchctl kickstart -k gui/$(id -u)/com.dalton.channel-gateway`）。
3. 新进程在对外就绪（`server.listen`）前重新向后端 Vault 接口加载最新凭据；若新凭据不完整或后端校验未通过，新进程拒绝就绪并不会生成半可用适配器。

### 3. 管理员数据任务群组报告投递授权

- **私聊（`dm`）**：继续严格要求属主在 `dalton_channel_identities` 中绑定 Dalton 专用平台个人身份，并保留跨用户与跨应用 `open_id` 拦截。
- **群组（`group`）**：主管理员无需绑定个人聊天账号或事先在群内触发入站消息即可向已授权群组投递生产数据任务报告，但必须同时满足：
  1. 生产运行已成功（拒绝 `preview_only` 试跑）、任务启用且通知开关开启；
  2. 当前通知路由（`evaluate_user_notify_eligibility`）显式包含该群组目标；
  3. 群组已在 Dalton 渠道设置（`dalton_channel_user_settings` 与 `dalton_channel_chat_bindings`）中显式登记，且属主具备绑定工作区写权限（绝不以“机器人在群列表中可见”替代 Dalton 渠道授权）；
  4. 网关发送首条消息前通过适配器实时核验机器人群组可达性（飞书调用完整分页的 `im.chat.list` 与 `im.chat.get`，若分页未完成返回 `unavailable`，若机器人不在目标群返回 `not_allowed`，均在写入发送台账或调用发送接口前中止）。

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
