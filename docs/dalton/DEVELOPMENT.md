# Dalton 开发规范与流程

本文定义向 Dalton 分支合入代码的工程流程、与上游 LangAlpha 同步的方法、叠加层与 hook 守卫规则、提交前必跑的基线检查、前端首屏体积预算、品牌用语约束、迁移命名规则及 PR 审查红线；编写 Dalton 代码、审查 PR 或执行上游合并前应阅读本文。

## 1. 仓库与上游同步方式

Dalton 开发分支为 `origin/dalton/main`，与上游 `upstream/main` 通过常规 `git merge upstream/main` 保持定期同步：

1. 所有同步 PR 以 `git merge-base HEAD upstream/main` 作为叠加层比较基线。
2. 解决三方合并冲突后，**必须对冲突与修改过的 Python 文件运行 `ruff check --select F821`**，排查合并丢失 `import` 导致的运行期未定义名称报错。
3. 严禁修改以下上游受保护基准文件（应通过 `*.dalton.yaml` 叠加或在 `src/dalton/` 内扩展）：
   - `src/llms/manifest/models.json`
   - `src/llms/manifest/providers.json`
   - `agent_config.yaml`
   - `config.yaml`

## 2. 叠加层规则（`DALTON-HOOK` 与 `dalton_paths.txt`）

### 2.1 何时需要标记 `DALTON-HOOK`

- 凡属 `scripts/dalton/dalton_paths.txt` 列明的文件与目录，属于 Dalton 独占资产，无需加 hook 标记。
- 凡修改非独占的上游文件，必须在每个变更 hunk 上下 **±25 行**内标注合法的 hook 注释，格式为 `DALTON-HOOK(<hook_id>)`（Python 使用 `# DALTON-HOOK(...)`，TypeScript/JS 使用 `// DALTON-HOOK(...)` 或块注释，Jinja2 使用 `{# DALTON-HOOK(...) #}`）。
- `<hook_id>` 必须在 `scripts/dalton/check_hooks.sh` 中的 `ALLOWED_HOOKS` 列表登记；删减核心防御 hook（`P0_REQUIRED_HOOKS`）会被直接阻断。

### 2.2 工具 Docstring 与代码生成锁

- 上游受保护工具清单锁位于 `tests/unit/mcp_servers/agent_docstring_lock.json`。
- Dalton 仅允许以**纯增量方式**新增 `dalton_*` 或 `mcp:dalton_*` 键；历史修改过的 7 个键锁定在 `scripts/dalton/check_hooks.sh` 的 `pinned_modified_sha256` 中，非专题决定严禁再改写上游既有工具的 docstring。

### 2.3 运行校验

```bash
./scripts/dalton/check_hooks.sh --strict --audit
```

## 3. 测试与基线门禁（`pr_baseline.sh`）

向 `dalton/main` 提交任何 PR 前，工作树必须干净（`pr_baseline.sh` 会拒绝脏工作区提交），且必须全量通过基线脚本：

```bash
bash scripts/dalton/pr_baseline.sh --baseline-only
# 关联 PR 并在基线后自动拉取 PR 描述执行声明核验（脚本不支持 --report，--pr 必须传入 PR 编号）：
bash scripts/dalton/pr_baseline.sh --pr <PR_NUMBER> --baseline-only
# 独立核验本地报告 Markdown 时直接调用 check_report_claims.py：
uv run python scripts/dalton/check_report_claims.py --strict <path/to/report.md>
```

`scripts/dalton/pr_baseline.sh` 按顺序强制执行四步（不传 `--baseline-only` 时还会串行执行 9 项 J1 验收与调试探测，其中 J1 Debug 6/9 比对归档报告 `docs/dalton/archive/dev-guide/P0I-round3-dev-report.md`）：

1. `./scripts/dalton/check_hooks.sh --strict --audit`（叠加层与受保护文件守卫）。
2. `uv run --with pytest-xdist ... pytest tests/unit -q -n ${DALTON_BASELINE_WORKERS:-4} --dist loadgroup --durations=25`（macOS 下按约定跳过少数 Linux 专用 `xattr`/`lchown`/`setuid` 用例）。
3. `node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs`（历史接口兼容性合约测试，保障现有测试基线不破坏）。
4. `pnpm --dir web typecheck && pnpm --dir web test -- --run`（前端 TypeScript 编译检查与 Vitest 单测）。

传入 `--pr <PR_NUMBER>` 时还会校验远端 `headRefOid` 与本地 `HEAD` 一致，拉取该 PR 描述后执行 `scripts/dalton/check_report_claims.py --strict` 核验报告引用的标识符、路径与口径（不加 `--no-comment` 时会将报告自动评论到对应 PR）。

## 4. 前端首屏体积预算与按需导入规则

生产前端包通过 `web/scripts/check-critical-path.mjs` 强制核对两项约束：首屏 JS 块集合必须等于 `EXPECTED = ['index', 'rolldown-runtime', 'vendor-react']`，且首屏总负载上限 **`MAX_EAGER_KB = 525`**（单位为 **KiB**，即 `bytes / 1024`；统计对象为经 **gzip** 压缩后的入口 `index.html`、所有被引用首屏 JS/CSS 资源以及体积最大的语言包 catalog 字节总和；因默认中文语言包 `zh-CN` 纳入首屏后从 500 KiB 调高至 525 KiB）：

- **严格禁止在非 Settings 页面从 `@/components/dalton/primitives`（即 `web/src/components/dalton/primitives/index.ts` barrel 入口）导入组件**。
- 原因：该 barrel 导出了 `DaltonActionConfirmButton`，其内部引用 `ConfirmDialog` 与 `framer-motion`；一旦首屏同步路由误引该 barrel，会把整套 `framer-motion` 拖入首屏同步块，直接触发超预算失败（+45.6 kB 回归）。
- **正确做法**：Settings 页以外的组件必须直接按文件精确导入子模块，例如 `@/components/dalton/primitives/DaltonStatusPill` 或 `@/components/dalton/primitives/DaltonSectionCard`。每次前端构建（`pnpm --dir web build` 或 `build_frontend_prod.sh`）后均会校验首屏体积。

## 5. 品牌开发规则

系统对内外部用户采用明确的双命名契约（详见 [features/brand.md](./features/brand.md)）：

- **助手称呼 Dalton，产品平台称 Everflow**：前端所有面向用户的产品文案使用 i18n 插值占位符 `{{brand}}`（Dalton 构建下解析为 `Everflow`），不要硬编码 `LangAlpha` 或 `Ginlix`。
- **提示词自我介绍**：智能体基础提示词 `src/ptc_agent/agent/prompts/templates/system.md.j2` 固定以 `Dalton, Everflow's investment research agent` 身份运行。
- **HTML 与分享降级页**：通过 `web/vite.config.js` 的 `brandIndexHtml` 与 `src/server/app/share_pages.py` 保障浏览器标签页、OpenGraph 链接预览和失效分享页统一下发 Everflow 品牌与 `/everflow/favicon.svg`。

## 6. 数据库迁移编号规则

- Dalton 迁移独立于上游 `migrations/`，存放在 `src/dalton/migrations/versions/`，配置文件为 `src/dalton/alembic.ini`，数据库内使用独立版本表 `alembic_version_dalton`。
- 迁移文件统一采用原生异步友好 SQL（`op.execute(...)`），不使用 ORM 模型自动生成。
- 历史区间 `d031` 之后由于多线并发合入，当前真实依赖链路为：**`d031 -> d035 -> d036 -> d032 -> d033 -> d037 -> d038 -> d039`**（不存在 `d034`）。
- 新增迁移前，务必先执行以下命令确认唯一线性头节点并沿其向下衔接 `down_revision`：

```bash
uv run alembic -c src/dalton/alembic.ini heads
```

## 7. 代码与 PR 审查红线（从历史审查清单提炼）

1. **禁止生产代码特例后门**：绝对禁止针对特定测试用户 ID、工作区名称、固定股票或环境标识编写“放行/跳过权限/绕过预算”的特殊分支。
2. **禁止使用无签名的松散 Mock**：后端单元测试模拟对象方法时，应使用 `create_autospec(<Class>, instance=True)` 或受协议约束的真实假实现，禁止用随意的 `MagicMock` / `SimpleNamespace` 掩盖缺失字段或同步/异步签名不匹配。
3. **Fail-Closed 安全边界**：所有认证（Supabase JWT）、内部服务令牌（`X-Service-Token`）、外部数据访问及目录 Containment（如禁止符号链接越权 `O_NOFOLLOW`）校验失败时必须明确拒绝，绝不静默回退到无鉴权分支或明文打印密钥。
4. **测试数据库安全**：涉及删除/重建表结构（`DROP SCHEMA public` 等）的集成测试，只能针对以 `_test` 结尾的隔离测试库执行，严禁指向生产或本地开发库。
5. **变更可验证、不伪造**：文档、发版说明与报告中的数字和声明必须能在当前代码或当次真实工具输出中复核；无法确认的外部环境事项必须明确标出待确认。
