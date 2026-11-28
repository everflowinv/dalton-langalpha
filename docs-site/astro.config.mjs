import { defineConfig, passthroughImageService } from 'astro/config';
import starlight from '@astrojs/starlight';
import { everflowSiteArtifacts } from './src/integrations/site-artifacts.mjs';
import {
  daltonDocsProfileVitePlugin,
  mapDocSlug,
  resolveDocsProfile,
} from './src/integrations/profile-filter.mjs';

const { isCore, brandName } = resolveDocsProfile();

const rawSiteOrigin = (
  process.env.DOCS_SITE_ORIGIN ||
  process.env.DALTON_FRONTEND_URL ||
  process.env.PUBLIC_APP_URL ||
  process.env.FRONTEND_URL ||
  ''
)
  .trim()
  .replace(/\/+$/, '');
const siteOrigin = /^https?:\/\//i.test(rawSiteOrigin) ? rawSiteOrigin : '';

const siteTitle = isCore ? `${brandName} 文档` : 'Everflow 文档';
const siteDescription = isCore
  ? `${brandName} 投研研究工作台中文使用文档。`
  : 'Everflow 投研研究平台与 Dalton 研究助手中文使用文档。';
const rssTitle = isCore ? `${brandName} 新功能` : 'Everflow 新功能';

const sidebar = [
  {
    label: '新功能',
    slug: 'whats-new',
  },
  {
    label: '入门',
    items: [
      {
        label: isCore ? `${brandName} 是什么` : 'Everflow 是什么',
        slug: mapDocSlug('get-started/what-is-everflow', { isCore }),
      },
      { label: '第一次使用', slug: 'get-started/first-use' },
      { label: '界面导览', slug: 'get-started/interface-tour' },
      { label: '集成凭据配置', slug: 'get-started/admin-integrations' },
    ],
  },
  {
    label: '核心概念',
    items: [
      { label: '工作区', slug: 'concepts/workspaces' },
      { label: '对话与回合', slug: 'concepts/threads-and-turns' },
      { label: '分析师与研究总监', slug: 'concepts/chief-of-staff-and-analysts' },
      { label: '电脑与沙箱', slug: 'concepts/computers-and-sandboxes' },
      { label: '文件', slug: 'concepts/files' },
      { label: '记忆、资料与草稿本', slug: 'concepts/memory-memos-scratchpad' },
      { label: '技能与插件', slug: 'concepts/skills-and-plugins' },
      { label: '定时任务、唤醒与后台命令', slug: 'concepts/automations-wakeups-commands' },
      { label: '分享与权限边界', slug: 'concepts/sharing' },
      { label: '模型与额度', slug: 'concepts/models-and-budgets' },
      { label: 'Claude Code 模式', slug: 'concepts/claude-code' },
    ],
  },
  ...(!isCore
    ? [
        {
          label: 'Everflow 专有能力',
          items: [
            { label: '监控面板', slug: 'everflow/monitors' },
            { label: '研究数据源', slug: 'everflow/research-data-sources' },
            { label: '定时报告与投递', slug: 'everflow/scheduled-reports-and-delivery' },
            { label: '设置页逐组说明', slug: 'everflow/settings-guide' },
            { label: '隐私政策', slug: 'everflow/privacy' },
          ],
        },
      ]
    : []),
  {
    label: '怎么选',
    items: [
      { label: '研究总监还是分析师？', slug: 'compare/chief-of-staff-or-analyst' },
      { label: '定时任务还是唤醒？', slug: 'compare/automation-or-wakeup' },
      { label: '记忆、资料还是草稿本？', slug: 'compare/memory-memo-or-scratchpad' },
      { label: '技能、插件还是数据源？', slug: 'compare/skill-plugin-or-mcp' },
    ],
  },
  {
    label: '实战指南',
    items: [
      { label: '写首次覆盖报告', slug: 'explore/initiation-report' },
      { label: '财报前准备与财报后更新', slug: 'explore/earnings-preview-and-review' },
      { label: '搭估值模型（Excel）', slug: 'explore/valuation-models' },
      { label: '盯一只股票的价格异动', slug: 'explore/price-move-watch' },
      { label: '组合复盘', slug: 'explore/portfolio-review' },
      { label: '做监控面板与图表', slug: 'explore/dashboards-and-charts' },
      ...(!isCore ? [{ label: '把结果发到飞书群', slug: 'explore/deliver-to-feishu' }] : []),
    ],
  },
  {
    label: '更新日志',
    slug: 'changelog',
  },
  {
    label: '常见问题',
    slug: 'faq',
  },
];

export default defineConfig({
  ...(siteOrigin ? { site: siteOrigin } : {}),
  base: '/help',
  image: {
    service: passthroughImageService(),
  },
  vite: {
    plugins: [daltonDocsProfileVitePlugin({ isCore, brandName })],
  },
  integrations: [
    starlight({
      title: siteTitle,
      description: siteDescription,
      defaultLocale: 'root',
      locales: {
        root: {
          label: '简体中文',
          lang: 'zh-CN',
        },
      },
      ...(!isCore
        ? {
            logo: {
              light: './src/assets/everflow-light.svg',
              dark: './src/assets/everflow-dark.svg',
              alt: 'Everflow 文档',
              replacesTitle: false,
            },
          }
        : {}),
      favicon: isCore ? '/favicon-core.svg' : '/favicon.svg',
      lastUpdated: true,
      customCss: ['./src/styles/everflow.css'],
      components: {
        Hero: './src/components/Hero.astro',
        PageTitle: './src/components/PageTitle.astro',
        ThemeSelect: './src/components/ThemeSelect.astro',
      },
      head: [
        {
          tag: 'link',
          attrs: {
            rel: 'alternate',
            type: 'application/rss+xml',
            title: rssTitle,
            href: '/help/whats-new/rss.xml',
          },
        },
      ],
      sidebar,
    }),
    everflowSiteArtifacts({ basePath: '/help', siteOrigin, isCore, brandName }),
  ],
});
