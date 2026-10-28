import type { ComponentType } from 'react';
import {
  Bot,
  CandlestickChart,
  CircleUser,
  Cpu,
  CreditCard,
  FlaskConical,
  MessageSquare,
  Palette,
  PieChart,
  Plug,
  SlidersHorizontal,
} from 'lucide-react';
import { isPlatformMode } from '@/config/hostMode';
import { TRADING_PERMISSION_ANCHOR } from '@/lib/tradingPermission';

interface SectionMeta {
  labelKey: string;
  /** What the section holds, in a line under its title. */
  descKey: string;
  icon: ComponentType<{ className?: string }>;
  /** Points into the hosted account console, which a self-hosted build has none of. */
  platformOnly?: boolean;
}

/**
 * The Settings page's sections, keyed by the `?tab=` values links already
 * carry: `model` and `preferences` (the investing profile) kept their names
 * when the page was regrouped, so every link written before still lands.
 */
export const SECTIONS = {
  account: { labelKey: 'settings.section.account', descKey: 'settings.definition.account', icon: CircleUser },
  preferences: { labelKey: 'settings.section.preferences', descKey: 'settings.definition.preferences', icon: PieChart },
  plan: { labelKey: 'settings.section.plan', descKey: 'settings.definition.plan', icon: CreditCard, platformOnly: true },
  general: { labelKey: 'settings.section.general', descKey: 'settings.definition.general', icon: SlidersHorizontal },
  appearance: { labelKey: 'settings.section.appearance', descKey: 'settings.definition.appearance', icon: Palette },
  chat: { labelKey: 'settings.section.chat', descKey: 'settings.definition.chat', icon: MessageSquare },
  agent: { labelKey: 'settings.section.agent', descKey: 'settings.definition.agent', icon: Bot },
  model: { labelKey: 'settings.section.model', descKey: 'settings.definition.model', icon: Cpu },
  integrations: { labelKey: 'settings.section.integrations', descKey: 'settings.definition.integrations', icon: Plug, platformOnly: true },
  trading: { labelKey: 'settings.section.trading', descKey: 'settings.definition.trading', icon: CandlestickChart },
  experiments: { labelKey: 'settings.section.experiments', descKey: 'settings.definition.experiments', icon: FlaskConical },
} satisfies Record<string, SectionMeta>;

export type Section = keyof typeof SECTIONS;

/** Whether this build draws the section. */
export function sectionShown(section: Section): boolean {
  // DALTON-HOOK(hide-trading): Dalton does not expose live brokerage trading in Settings
  // DALTON-HOOK(hide-experiments): Dalton does not expose experimental labs in Settings
  if (section === 'trading' || section === 'experiments') return false;
  const meta: SectionMeta = SECTIONS[section];
  return isPlatformMode || !meta.platformOnly;
}

export const GROUPS: readonly { labelKey: string; sections: readonly Section[] }[] = [
  { labelKey: 'settings.group.account', sections: ['account', 'preferences', 'plan'] },
  { labelKey: 'settings.group.app', sections: ['general', 'appearance', 'chat'] },
  { labelKey: 'settings.group.agent', sections: ['agent', 'model', 'integrations', 'trading'] },
  { labelKey: 'settings.group.labs', sections: ['experiments'] },
];

// DALTON-HOOK(settings-section): shared visibility context and predicates from Dalton card modules
import {
  canShowDaltonCosSharingCard,
  type DaltonSettingVisibilityContext,
} from './panels/dalton/DaltonCosSharingCard';
import { canShowDaltonInviteAdminCard } from './panels/dalton/DaltonInvitesCard';

export {
  canShowDaltonCosSharingCard,
  canShowDaltonInviteAdminCard,
  type DaltonSettingVisibilityContext,
};

/**
 * One setting: the section it lives in, the anchor its row carries, and the
 * name and line it is drawn with. The rows take their copy from here, so the
 * search finds a setting by exactly the words its row shows.
 */
export interface SettingEntry {
  section: Section;
  anchor: string;
  labelKey: string;
  descKey?: string;
  /** Drawn only in the desktop app, so only found there. */
  desktopOnly?: boolean;
  /** Drawn only in the Models section's advanced view, which opening it turns on. */
  advanced?: boolean;
  // DALTON-HOOK(settings-section): bilingual search terms and card-level visibility predicate
  keywords?: string;
  visibleWhen?: (ctx?: DaltonSettingVisibilityContext | null) => boolean;
}

export const SETTINGS = {
  photo: { section: 'account', anchor: 'photo', labelKey: 'settings.photo', descKey: 'settings.photoDesc' },
  name: { section: 'account', anchor: 'name', labelKey: 'common.name', descKey: 'settings.nameDesc' },
  email: { section: 'account', anchor: 'email', labelKey: 'common.email', descKey: 'settings.emailCannotBeChanged' },
  logout: { section: 'account', anchor: 'sign-out', labelKey: 'settings.logout', descKey: 'settings.logoutDesc' },
  investingAnswers: { section: 'preferences', anchor: 'investing-profile', labelKey: 'settings.investingAnswers', descKey: 'settings.preferencesDesc' },
  resetPreferences: { section: 'preferences', anchor: 'reset-preferences', labelKey: 'settings.resetPreferences', descKey: 'settings.resetPreferencesDesc' },
  plan: { section: 'plan', anchor: 'plan', labelKey: 'settings.planUsage.plan', descKey: 'settings.planUsage.planDesc' },
  usage: { section: 'plan', anchor: 'usage', labelKey: 'settings.planUsage.usage', descKey: 'settings.planUsage.usageDesc' },
  billing: { section: 'plan', anchor: 'billing', labelKey: 'settings.planUsage.billing', descKey: 'settings.planUsage.billingDesc' },
  language: { section: 'general', anchor: 'language', labelKey: 'settings.locale', descKey: 'settings.localeDesc' },
  timezone: { section: 'general', anchor: 'timezone', labelKey: 'settings.timezone', descKey: 'settings.timezoneDesc' },
  linkTarget: { section: 'general', anchor: 'link-target', labelKey: 'settings.shareLinkOpen', descKey: 'settings.shareLinkOpenDesc', desktopOnly: true },
  guides: { section: 'general', anchor: 'guides', labelKey: 'onboarding.settings.sectionTitle', descKey: 'onboarding.settings.description' },
  theme: { section: 'appearance', anchor: 'theme', labelKey: 'settings.theme', descKey: 'settings.themeDesc' },
  fontSize: { section: 'appearance', anchor: 'font-size', labelKey: 'settings.fontSize', descKey: 'settings.fontSizeDesc' },
  cardStyle: { section: 'appearance', anchor: 'card-style', labelKey: 'settings.cardStyle', descKey: 'settings.cardStyleDesc', keywords: '卡片样式 card style' },
  turnDisplay: { section: 'chat', anchor: 'stream-reasoning', labelKey: 'settings.turnDisplay', descKey: 'settings.turnDisplayDesc' },
  streamingMode: { section: 'chat', anchor: 'response-delivery', labelKey: 'settings.streamingMode', descKey: 'settings.streamingModeDesc' },
  toolCallLayout: { section: 'chat', anchor: 'tool-calls', labelKey: 'settings.toolCallLayout', descKey: 'settings.toolCallLayoutDesc' },
  turnEndScroll: { section: 'chat', anchor: 'turn-end', labelKey: 'settings.turnEndScroll', descKey: 'settings.turnEndScrollDesc' },
  voiceInput: { section: 'chat', anchor: 'voice-input', labelKey: 'settings.voiceInput', descKey: 'settings.voiceInputDesc' },
  outputFormat: { section: 'agent', anchor: 'output-format', labelKey: 'settings.outputFormat', descKey: 'settings.outputFormatDesc', keywords: '输出格式 output format' },
  subagents: { section: 'agent', anchor: 'subagents', labelKey: 'settings.subagentsDefault', descKey: 'settings.subagentsDefaultDesc' },
  approveHandoffs: { section: 'agent', anchor: 'approve-handoffs', labelKey: 'settings.autoApproveHandoffs', descKey: 'settings.autoApproveHandoffsDesc' },
  approveWorkspaces: { section: 'agent', anchor: 'approve-workspaces', labelKey: 'settings.autoApproveWorkspaceCreation', descKey: 'settings.autoApproveWorkspaceCreationDesc' },
  searchProvider: { section: 'agent', anchor: 'search-provider', labelKey: 'settings.searchProvider', descKey: 'settings.searchProviderDesc' },
  modelAccess: { section: 'model', anchor: 'model-access', labelKey: 'modelAccess.title', descKey: 'modelAccess.description' },
  defaultModels: { section: 'model', anchor: 'default-models', labelKey: 'settings.defaultModels', descKey: 'settings.defaultModelsDesc', keywords: '系统默认模型 默认模型 default models' },
  defaultModelScope: { section: 'model', anchor: 'default-model-scope', labelKey: 'settings.defaultModelChange.scopeLabel', descKey: 'settings.defaultModelChange.scopeDesc' },
  quickAccess: { section: 'model', anchor: 'quick-access', labelKey: 'settings.starredModels', descKey: 'settings.starredModelsDesc' },
  modelDetail: { section: 'model', anchor: 'model-detail', labelKey: 'settings.modelTuning.showAdvanced', descKey: 'settings.modelTuning.showAdvancedDesc' },
  accountDefaults: { section: 'model', anchor: 'account-defaults', labelKey: 'settings.modelTuning.sectionDefaults', advanced: true },
  perModel: { section: 'model', anchor: 'per-model', labelKey: 'settings.modelTuning.perModel', descKey: 'settings.modelTuning.perModelDesc', advanced: true },
  routing: { section: 'model', anchor: 'routing', labelKey: 'settings.modelTuning.sectionRouting', descKey: 'settings.fallbackModels', advanced: true, keywords: '模型路由 备选模型 fallback routing' },
  // DALTON-HOOK(settings-section): register Dalton model routing, budget, team credentials, and Chief of Staff sharing in central registry
  daltonRouting: { section: 'model', anchor: 'dalton-routing', labelKey: 'settings.modelTuning.daltonHeading', descKey: 'settings.dalton.routingDesc', advanced: true, keywords: '模型路由 审计 核验 审稿 辅助 摘取 model routing audit reviewer assist extract' },
  daltonAuditModel: { section: 'model', anchor: 'dalton-audit-model', labelKey: 'settings.modelTuning.daltonAuditModel', descKey: 'settings.modelTuning.daltonAuditModelDesc', advanced: true, keywords: '审计模型 数字核验模型 终局事实核验 figure audit model fact check' },
  daltonLanguageModel: { section: 'model', anchor: 'dalton-language-model', labelKey: 'settings.modelTuning.daltonLanguageModel', descKey: 'settings.modelTuning.daltonLanguageModelDesc', advanced: true, keywords: '语言审查模型 文字检查模型 语病 AI 腔 language review model' },
  daltonAuditFallbackModels: { section: 'model', anchor: 'dalton-audit-fallback-models', labelKey: 'settings.modelTuning.daltonAuditFallbackModels', descKey: 'settings.modelTuning.daltonAuditFallbackModelsDesc', advanced: true, keywords: '数字核验备选 审计备选模型 figure audit fallback models' },
  daltonAuditTimeout: { section: 'model', anchor: 'dalton-audit-timeout', labelKey: 'settings.modelTuning.daltonAuditTimeoutS', descKey: 'settings.modelTuning.daltonAuditTimeoutSDesc', advanced: true, keywords: '数字核验超时 核验超时 审计超时 figure audit timeout' },
  daltonAssistModel: { section: 'model', anchor: 'dalton-assist-model', labelKey: 'settings.modelTuning.daltonAssistModel', descKey: 'settings.modelTuning.daltonAssistModelDesc', advanced: true, keywords: '辅助任务模型 辅助模型 压缩 标题 摘要 分类 知识对账 auxiliary task model assist' },
  daltonAssistFallbackModels: { section: 'model', anchor: 'dalton-assist-fallback-models', labelKey: 'settings.modelTuning.daltonAssistFallbackModels', descKey: 'settings.modelTuning.daltonAssistFallbackModelsDesc', advanced: true, keywords: '辅助任务备选 辅助备选模型 auxiliary task fallback models' },
  daltonReviewerModels: { section: 'model', anchor: 'dalton-reviewer-models', labelKey: 'settings.modelTuning.daltonReviewerModels', descKey: 'settings.modelTuning.daltonReviewerModelsDesc', advanced: true, keywords: '审稿 审稿模型候选 独立审稿模型 逻辑审稿 盲审 independent reviewer candidates' },
  daltonExtractModel: { section: 'model', anchor: 'dalton-extract-model', labelKey: 'settings.modelTuning.daltonExtractModel', descKey: 'settings.modelTuning.daltonExtractModelDesc', advanced: true, keywords: '对话归档抽卡模型 网页摘取模型 摘取 conversation archive extraction model' },
  daltonCosSharing: { section: 'model', anchor: 'dalton-cos-sharing', labelKey: 'settings.dalton.cosSharingLabel', descKey: 'settings.dalton.cosSharingDesc', keywords: '研究总监 研究总监共享开关 允许同事的研究总监向我的工作区派活 chief of staff sharing colleague dispatch', visibleWhen: canShowDaltonCosSharingCard },
  daltonInvites: { section: 'model', anchor: 'dalton-invites', labelKey: 'settings.dalton.invitesLabel', descKey: 'settings.dalton.invitesDesc', keywords: '团队邀请 成员权限 邀请链接 team invites allowlist', visibleWhen: canShowDaltonInviteAdminCard },
  daltonFeishuScopes: { section: 'model', anchor: 'dalton-feishu-scopes', labelKey: 'settings.dalton.feishuScopesLabel', descKey: 'settings.dalton.feishuScopesDesc', keywords: '飞书读取权限 飞书授权 文档 知识库 多维表格 feishu read scopes', visibleWhen: canShowDaltonInviteAdminCard },
  daltonProviders: { section: 'model', anchor: 'dalton-providers', labelKey: 'settings.dalton.providersLabel', descKey: 'settings.dalton.providersDesc', keywords: '团队凭据 模型与数据源状态 团队共享凭据 team credentials provider status openclaw' },
  daltonBudget: { section: 'model', anchor: 'dalton-budget', labelKey: 'settings.dalton.budgetLabel', descKey: 'settings.dalton.budgetDesc', keywords: '预算 每日预算 团队模型限额 USD 额度 调额 daily budget spending limit' },
  daltonNotifyTargets: { section: 'model', anchor: 'dalton-notify-targets', labelKey: 'settings.dalton.notifyTargetsLabel', descKey: 'settings.dalton.notifyTargetsDesc', keywords: '通知目标 主动推送 消息提醒 notify targets proactive alerts' },
  daltonSystemJobs: { section: 'model', anchor: 'dalton-system-jobs', labelKey: 'settings.dalton.systemJobsLabel', descKey: 'settings.dalton.systemJobsDesc', advanced: true, keywords: '自动任务 知识库同步 研报增量扫描 公司近况巡检 system jobs cron' },
  chatApps: { section: 'integrations', anchor: 'chat-apps', labelKey: 'settings.integrations.chatApps', descKey: 'settings.integrations.chatAppsDesc' },
  connectedApps: { section: 'integrations', anchor: 'connected-apps', labelKey: 'settings.integrations.connectedApps', descKey: 'settings.integrations.connectedAppsDesc' },
  tradingPermission: { section: 'trading', anchor: TRADING_PERMISSION_ANCHOR, labelKey: 'settings.tradingPermission.title', descKey: 'settings.tradingPermission.desc' },
  experiments: { section: 'experiments', anchor: 'experiments', labelKey: 'settings.experiments', descKey: 'settings.experimentsIntro' },
} satisfies Record<string, SettingEntry>;

const ENTRIES: readonly SettingEntry[] = Object.values(SETTINGS);

/** The setting a URL hash names, if any. */
export function settingAt(hash: string): SettingEntry | undefined {
  const anchor = hash.replace(/^#/, '');
  return anchor ? ENTRIES.find((entry) => entry.anchor === anchor) : undefined;
}

/** Every setting the search can find on this surface. */
export function searchableSettings(
  desktop: boolean,
  ctx?: DaltonSettingVisibilityContext | null,
): SettingEntry[] {
  return ENTRIES.filter(
    (entry) =>
      (desktop || !entry.desktopOnly) &&
      sectionShown(entry.section) &&
      (!entry.visibleWhen || entry.visibleWhen(ctx)),
  );
}

// DALTON-HOOK(settings-section): map legacy tab query params and hashes to their new sections
const LEGACY_SECTIONS: Record<string, Section> = {
  userInfo: 'account',
  'user-info': 'account',
  profile: 'account',
  models: 'model',
  llm: 'model',
  dalton: 'model',
};

function asSection(param: string | null): Section | null {
  if (!param) return null;
  if (Object.hasOwn(LEGACY_SECTIONS, param)) return LEGACY_SECTIONS[param];
  return Object.hasOwn(SECTIONS, param) ? (param as Section) : null;
}

/**
 * The section the URL names. A hash naming a setting wins over the tab, so a
 * link written when that setting lived elsewhere (Trading permission under
 * `?tab=preferences`) still opens on it.
 */
export function readSection(params: URLSearchParams, hash: string): Section {
  const cleanHash = hash.replace(/^#/, '');
  return (
    [settingAt(hash)?.section, asSection(cleanHash), asSection(params.get('tab'))].find(
      (s): s is Section => Boolean(s && sectionShown(s)),
    ) ?? 'account'
  );
}
