# 品牌命名规则（Brand）

本文说明 Dalton 对外产品与内部助手的双品牌命名约定、前端环境变量与构建插件注入机制、国际化插值规范以及后端提示词/分享失效页面的品牌适配；新增面向用户的页面、调整分享落地页或构建生产前端包前应查阅本文。

## 1. 核心命名契约：助手叫 Dalton，产品叫 Everflow

全平台严格区分“AI 助手角色”与“产品平台外壳”：

- **产品平台名**：统一称为 **Everflow**（不再向终端用户展示上游品牌名 `LangAlpha` 或 `Ginlix`）。
- **研究助手名**：智能体本身统一自称为 **Dalton**。

## 2. 前端品牌注入与 `{{brand}}` 插值

1. **构建期环境配置（`web/.env.dalton`）**：
   - 声明 `VITE_BRAND_NAME=Everflow` 与 `VITE_BRAND_MARK=everflow`。
   - 执行 `bash scripts/dalton/build_frontend_prod.sh` 时会先读取 `web/.env.dalton` 作为 `VITE_*` 默认值（再以仓库 `.env` 中的 `VITE_*` 覆盖并导出），随后进入 `web/` 执行 `pnpm build`（脚本未设置 `APP_ENV=dalton`）。
2. **运行时品牌常量（`web/src/config/brand.ts`）**：
   - 导出 `BRAND_NAME`（由 `VITE_BRAND_NAME` 注入，Dalton 构建为 `Everflow`）、`BRAND_MARK`（`everflow`）与 `isDaltonBuild` 开关，供组件、页脚与品牌 Logo（`web/src/components/brand/EverflowMark.tsx`、`BrandMark.tsx`）统一读取。
3. **HTML 元信息与链接预览（`web/vite.config.js:brandIndexHtml`）**：
   - Vite 构建插件 `brandIndexHtml` 在构建产出 `index.html` 时注入 `<title>`、`/everflow/favicon.svg` 图标、`<meta property="og:site_name">`、`<meta property="og:title">` 与 `<meta name="application-name">` 为 `Everflow`，保障飞书、Discord 与浏览器收藏夹链接卡片显示一致。
4. **前端文案插值**：
   - 前端 i18n 文案统一使用 `{{brand}}` 占位符，严禁在 UI 文案中写死产品名称 `LangAlpha` 或 `Ginlix`。

## 3. 后端提示词与分享失效降级页

- **智能体系统提示词（`src/ptc_agent/agent/prompts/templates/system.md.j2`）**：
  - 通过 `{# DALTON-HOOK(brand-name) ... #}` 明确约定身份为 `You are Dalton, Everflow's investment research agent`，使其既不对外自称 LangAlpha，也分清自身（Dalton）与所在平台（Everflow）。
- **分享报告失效降级 HTML（`src/server/app/share_pages.py`）**：
  - 当浏览器访问已撤回（`404`）或无权（`403`）的分享报告链接时（`wants_html` 为真），`unavailable_page` 按浏览器首选语言返回中/英文自包含 HTML 提示页，操作按钮锚定前往 **Everflow**（`# DALTON-HOOK(brand-name)`），不泄露上游或内部错误信息。
