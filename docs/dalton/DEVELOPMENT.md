# Dalton 开发规范与流程

这页定下向 Dalton 分支合入代码的工程规范，覆盖上游同步、叠加层和 hook 守卫、基线测试门禁、前端首屏体积预算、品牌用语约束、迁移命名规则和 PR 审查红线。

## 1. 仓库与上游同步方式

Dalton 开发分支是 `origin/dalton/main`，定期用 `git merge upstream/main` 跟上游 `upstream/main` 同步：

1. 所有同步 PR 都拿 `git merge-base HEAD upstream/main` 当叠加层比较基线。
2. 解完三方合并冲突后，**必须对冲突和改过的 Python 文件跑一遍 `ruff check --select F821`**，查出合并丢掉 `import` 导致的运行期未定义名称报错。
3. 严禁修改下面四个上游受保护基准文件（要改就走 `*.dalton.yaml` 叠加或在 `src/dalton/` 里扩展）：
   - `src/llms/manifest/models.json`
   - `src/llms/manifest/providers.json`
   - `agent_config.yaml`
   - `config.yaml`

## 2. 叠加层规则（`DALTON-HOOK` 与 `dalton_paths.txt`）

### 2.1 什么时候要标 `DALTON-HOOK`

- 凡是列在 `scripts/dalton/dalton_paths.txt` 里的文件和目录，都归 Dalton 独占，不用加 hook 标记。
- 改到非独占的上游文件时，必须在每个变更 hunk 上下 **±25 行**内标上合法的 hook 注释，格式是 `DALTON-HOOK(<hook_id>)`。Python 用 `# DALTON-HOOK(...)`，TypeScript/JS 用 `// DALTON-HOOK(...)` 或块注释，Jinja2 用 `{# DALTON-HOOK(...) #}`。
- `<hook_id>` 必须在 `scripts/dalton/check_hooks.sh` 的 `ALLOWED_HOOKS` 列表里登记；删掉核心防御 hook（`P0_REQUIRED_HOOKS`）会被直接拦住。

### 2.2 工具 Docstring 与代码生成锁

- 上游受保护工具清单锁在 `tests/unit/mcp_servers/agent_docstring_lock.json`。
- Dalton 只允许以**纯增量方式**加 `dalton_*` 或 `mcp:dalton_*` 键。历史上改过的 7 个键锁在 `scripts/dalton/check_hooks.sh` 的 `pinned_modified_sha256` 里，没有专题决定严禁再改上游原有工具的 docstring。

### 2.3 运行校验

```bash
./scripts/dalton/check_hooks.sh --strict --audit
```

## 3. 测试与基线门禁（`pr_baseline.sh`）

向 `dalton/main` 提任何 PR 前，工作树必须干净（`pr_baseline.sh` 会拒掉脏工作区），而且必须全量跑过基线脚本：

```bash
bash scripts/dalton/pr_baseline.sh --baseline-only
# 关联 PR 并在基线后自动拉取 PR 描述执行声明核验（--pr 须传入 PR 编号；本地报告核验请用下方 check_report_claims.py）：
bash scripts/dalton/pr_baseline.sh --pr <PR_NUMBER> --baseline-only
# 独立核验本地报告 Markdown 时直接调用 check_report_claims.py：
uv run python scripts/dalton/check_report_claims.py --strict <path/to/report.md>
```

`scripts/dalton/pr_baseline.sh` 按顺序强制跑这三步（不传 `--baseline-only` 时还会串行跑 9 项 J1 验收和调试探测，其中 J1 Debug 6/9 对照归档报告 `docs/dalton/archive/dev-guide/P0I-round3-dev-report.md`）：

1. `./scripts/dalton/check_hooks.sh --strict --audit`（叠加层与受保护文件守卫）。
2. `uv run --with pytest-xdist ... pytest tests/unit -q -n ${DALTON_BASELINE_WORKERS:-4} --dist loadgroup --durations=25`（macOS 下按约定跳过少数 Linux 专用的 `xattr`/`lchown`/`setuid` 用例）。
3. `pnpm --dir web typecheck && pnpm --dir web test -- --run`（前端 TypeScript 类型检查和 Vitest 单测）。

传入 `--pr <PR_NUMBER>` 时，脚本还会核对远端 `headRefOid` 和本地 `HEAD` 是否一致，拉取 PR 描述跑 `scripts/dalton/check_report_claims.py --strict` 核验报告里引的标识符、路径和口径；不加 `--no-comment` 时，结果会自动评论到对应 PR。

## 4. 前端首屏体积预算与按需导入规则

生产前端包会用 `web/scripts/check-critical-path.mjs` 强制卡住两项指标：首屏 JS 块集合必须等于 `EXPECTED = ['index', 'rolldown-runtime', 'vendor-react']`，首屏总负载上限是 **`MAX_EAGER_KB = 490`**（单位为 **KiB**，即 `bytes / 1024`）。统计范围包括 **gzip** 压缩后的入口 `index.html`、所有被引用的首屏 JS/CSS 资源，以及体积最大的语言包（当前为默认中文包 `zh-CN`）字节总和。

- **严禁在 Settings 以外的页面从 `@/components/dalton/primitives`（即 `web/src/components/dalton/primitives/index.ts` 汇总导出入口）导入组件**。
- **原因**：`primitives/index.ts` 汇总导出了 `DaltonActionConfirmButton`，它里面引了 `ConfirmDialog` 和 `framer-motion`。只要首屏同步路由误引这个入口，整套 `framer-motion` 就会被拖进首屏同步块，直接爆预算。
- **正确做法**：Settings 页以外的组件必须直接按文件精确导入子模块，比如 `@/components/dalton/primitives/DaltonStatusBadge`、`@/components/dalton/primitives/DaltonToolbarPill` 或 `@/components/dalton/primitives/DaltonSettingsCard`。每次前端构建（`pnpm --dir web build` 或 `build_frontend_prod.sh`）后都会跑首屏体积校验。

## 5. 品牌开发规则

系统对内外用户用明确的双命名契约（详见 [features/brand.md](./features/brand.md)）：

- **助手叫 Dalton，产品平台叫 Everflow**：前端所有面向用户的产品文案都用国际化（i18n）插值占位符 `{{brand}}`（Dalton 构建下解析成 `Everflow`），统一由品牌配置注入产品名。
- **提示词自我介绍**：智能体基础提示词 `src/ptc_agent/agent/prompts/templates/system.md.j2` 固定用 `Dalton, Everflow's investment research agent` 身份运行。
- **HTML 与分享降级页**：通过 `web/vite.config.js` 的 `brandIndexHtml` 和 `src/server/app/share_pages.py`，保证浏览器标签页、OpenGraph 链接预览和失效分享页统一下发 Everflow 品牌和 `/everflow/favicon.svg`。

## 6. 数据库迁移编号规则

- Dalton 迁移独立于上游 `migrations/`，放在 `src/dalton/migrations/versions/`，配置文件是 `src/dalton/alembic.ini`，数据库里用独立版本表 `alembic_version_dalton`。
- 迁移脚本统一写原生异步友好 SQL（`op.execute(...)`），不用 ORM 模型自动生成。
- `d031` 之后由于多线并发合入，当前真实依赖链路是（共 43 个迁移，无 `d034`）：**`d031 -> d035 -> d036 -> d032 -> d033 -> d037 -> d038 -> d039 -> d040 -> d041 -> d042 -> d043 -> d044`**。
- 新增迁移前，一定要先跑下面这条命令确认唯一线性头节点，再沿着它往下接 `down_revision`：

```bash
uv run alembic -c src/dalton/alembic.ini heads
```

## 7. 代码与 PR 审查红线（从历史审查清单提炼）

1. **禁止生产代码特例后门**：严禁针对特定测试用户 ID、工作区名称、固定股票或环境标识写“放行、跳过权限或绕过预算”的特殊分支。
2. **禁止无签名的松散 Mock**：后端单测模拟对象方法时，要用 `create_autospec(<Class>, instance=True)` 或受协议约束的真实假实现，禁止用随意的 `MagicMock` / `SimpleNamespace` 盖住缺失字段或同步异步签名不匹配。
3. **Fail-Closed 安全边界**：所有认证（Supabase JWT）、内部服务令牌（`X-Service-Token`）、外部数据访问和目录 Containment（比如防符号链接越权 `O_NOFOLLOW`）校验失败时必须明确拒绝，绝不静默退回无鉴权分支，也绝不打印明文密钥。
4. **测试数据库安全**：删表或重建表结构（`DROP SCHEMA public` 等）的集成测试，只能跑在以 `_test` 结尾的隔离测试库上，严禁指向生产或本地开发库。
5. **变更可验证、不伪造**：文档、发版说明和报告里的数字及声明，必须能在当前代码或当次真实工具输出里复核。尚未现场核实的外部环境事项要明确标出待确认。
