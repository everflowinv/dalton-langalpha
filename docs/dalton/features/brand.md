# 品牌命名规则（Brand）

这页讲清 Dalton 对外产品和内部助手的双品牌命名约定、前端构建环境变量注入、i18n 插值规范，以及后端提示词和分享失效页的品牌适配。

## 1. 核心命名契约：助手叫 Dalton，产品叫 Everflow

全平台严格区分“AI 助手角色”和“产品平台外壳”：

- **产品平台名**：对外统一叫 **Everflow**，统一展示 Everflow 品牌标识，不出现上游品牌名 `LangAlpha` 或 `Ginlix`。
- **研究助手名**：智能体本身统一自称 **Dalton**。

## 2. 前端品牌注入与 `{{brand}}` 插值

1. **构建期环境配置（`web/.env.dalton`）**：
   - 声明 `VITE_BRAND_NAME=Everflow` 和 `VITE_BRAND_MARK=everflow`。
   - 跑 `bash scripts/dalton/build_frontend_prod.sh` 时，先读 `web/.env.dalton` 当 `VITE_*` 默认值，再用仓库 `.env` 里的 `VITE_*` 覆盖并导出，接着进 `web/` 跑 `pnpm build`（脚本没设 `APP_ENV=dalton`），并同步构建 `docs-site/` 静态帮助站点输出到 `web/dist/help/`。
2. **运行时品牌常量（`web/src/config/brand.ts`）**：
   - 导出 `BRAND_NAME`（由 `VITE_BRAND_NAME` 注入，Dalton 构建下是 `Everflow`）、`BRAND_MARK`（`everflow`）和 `isDaltonBuild` 开关，供组件、页脚和品牌 Logo（`web/src/components/brand/EverflowMark.tsx`、`BrandMark.tsx`）统一读取。侧栏“文档 / 更新日志 / 新功能”及支持链接统一指向站内 `/help/*`，不指向 `langalpha.ai`。
3. **HTML 元信息与链接预览（`web/vite.config.js:brandIndexHtml`）**：
   - Vite 插件 `brandIndexHtml` 在打出 `index.html` 时注入 `<title>`、`/everflow/favicon.svg` 图标、`<meta property="og:site_name">`、`<meta property="og:title">` 和 `<meta name="application-name">` 为 `Everflow`，让飞书、Discord 和浏览器收藏夹卡片显示一致。
4. **前端文案插值**：
   - 前端 i18n 文案统一用 `{{brand}}` 占位符，严禁在 UI 文案里写死产品名 `LangAlpha` 或 `Ginlix`。

## 3. 后端提示词与分享失效降级页

- **智能体系统提示词（`src/ptc_agent/agent/prompts/templates/system.md.j2`）**：
  - 通过 `{# DALTON-HOOK(brand-name) ... #}` 把身份定成 `You are Dalton, Everflow's investment research agent`，让助手分清自己（Dalton）和所在平台（Everflow）。
- **分享报告失效降级 HTML（`src/server/app/share_pages.py`）**：
  - 浏览器访问已撤回（`404`）或无权（`403`）的分享报告链接且 `wants_html` 为真时，`unavailable_page` 会按浏览器首选语言返回中英文自包含 HTML 提示页，操作按钮指向 **Everflow**（`# DALTON-HOOK(brand-name)`），隐藏上游与内部报错细节。
