import type { ComponentType } from 'react';
import {
  Bot,
  CandlestickChart,
  CircleUser,
  Cpu,
  FlaskConical,
  Layers,
  MessageSquare,
  PieChart,
  Plug,
  Server,
  ServerCog,
  SlidersHorizontal,
  Users,
  Wallet,
} from 'lucide-react';
import { BRAND_NAME, isDaltonBuild } from '@/config/brand'; // DALTON-HOOK(brand-name)
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(settings-reorg)
import { isPlatformMode } from '@/config/hostMode';
// DALTON-HOOK(standalone-core): capability-aware section & search gating
import {
  hasRequiredCapability,
  PERSONAL_INTEGRATION_CAPABILITIES, // DALTON-HOOK(personal-integrations)
  type CapabilityRequirement,
} from '@/hooks/useCapabilities';
import { canDesktopSettings } from '@/lib/desktop';
import { TRADING_PERMISSION_ANCHOR } from '@/lib/tradingPermission';
// DALTON-HOOK(settings-section): shared visibility context and predicates from Dalton card modules
// DALTON-HOOK(team-memory-toggle): Home Team Memory user-level switch setting entry
// DALTON-HOOK(settings-reorg): Personal vs Team Management groups, minTier guards, and legacy anchor resolution
import {
  canShowDaltonCosSharingCard,
  type DaltonSettingVisibilityContext,
} from './panels/dalton/DaltonCosSharingCard';
import { canShowDaltonHomeTeamMemoryCard } from './panels/dalton/DaltonHomeTeamMemoryCard';
import { canShowDaltonInviteAdminCard } from './panels/dalton/DaltonInvitesCard';
import { canShowDaltonFeishuScopesCard } from './panels/dalton/FeishuReadScopesCard';
import { canShowDaltonWeixinBindCard } from './panels/dalton/weixinVisibility'; // DALTON-HOOK(weixin-multi-account)

/** Discord channels card & search visibility: requires verified admin tier and channel_discord capability. */
export function canShowDaltonDiscordChannelsCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!ctx) return false;
  if (!hasRequiredCapability(ctx.capabilities, 'channel_discord')) return false;
  if (ctx.discordChannelsAdmin !== undefined) {
    return ctx.discordChannelsAdmin;
  }
  return ctx.callerTier === 'admin';
}

export {
  canShowDaltonCosSharingCard,
  canShowDaltonHomeTeamMemoryCard,
  canShowDaltonInviteAdminCard,
  canShowDaltonFeishuScopesCard,
  canShowDaltonWeixinBindCard, // DALTON-HOOK(weixin-multi-account)
  type DaltonSettingVisibilityContext,
};

export type SettingsTierRequirement = 'internal' | 'admin';

export interface SectionMeta {
  labelKey: string;
  /** What the section holds, in a line under its title. */
  descKey: string;
  icon: ComponentType<{ className?: string }>;
  /** Points into the hosted account console, which a self-hosted build has none of. */
  platformOnly?: boolean;
  /** Settings the desktop shell keeps, so drawn only in a shell that can read and write them. */
  desktopOnly?: boolean;
  /** Minimum Dalton team tier required (`internal` allows internal+admin; `admin` allows admin only). */
  minTier?: SettingsTierRequirement;
  /** Rendered only when Dalton team auth is active (hidden in OSS single-user mode). */
  daltonAuthOnly?: boolean;
  /** Rendered only when the coverage units feature flag is enabled. */
  coverageUnitsOnly?: boolean;
  /** Optional capability gate (DALTON-HOOK(standalone-core)). */
  capability?: CapabilityRequirement;
  /** What a hosted server's operator decides, so only a self-hosted build shows it. */
  selfHostedOnly?: boolean;
}

// DALTON-HOOK(settings-reorg): Personal vs Team Management groups & minTier section metadata
/**
 * Reorganized Settings sections split into Personal and Team Management groups.
 * Legacy `?tab=` params (`appearance`, `desktop`, `agent`, `artifacts`, `plan`, `dalton`, etc.)
 * are redirected via `LEGACY_SECTIONS`, and legacy `#anchor` targets via `LEGACY_ANCHORS`.
 */
export const SECTIONS = {
  // Personal settings
  account: { labelKey: 'settings:section.account', descKey: 'settings:sectionDesc.account', icon: CircleUser },
  preferences: { labelKey: 'settings:section.preferences', descKey: 'settings:sectionDesc.preferences', icon: PieChart },
  general: { labelKey: 'settings:section.general', descKey: 'settings:sectionDesc.general', icon: SlidersHorizontal },
  chat: { labelKey: 'settings:section.chat', descKey: 'settings:sectionDesc.chat', icon: MessageSquare },
  // The glyph the workspace gallery's computer menu names them by.
  computers: { labelKey: 'settings:section.computers', descKey: 'settings:definition.computers', icon: Server },
  model: { labelKey: 'settings:section.model', descKey: 'settings:sectionDesc.model', icon: Cpu },
  server: { labelKey: 'settings:section.server', descKey: 'settings:definition.server', icon: ServerCog, selfHostedOnly: true },
  budget: { labelKey: 'settings:section.budget', descKey: 'settings:sectionDesc.budget', icon: Wallet },
  integrations: { labelKey: 'settings:section.integrations', descKey: 'settings:sectionDesc.integrations', icon: Plug },
  // DALTON-HOOK(watchlist): Personal tier monitoring policies are always available in Coverage section (Everflow & Core)
  coverage: { labelKey: 'settings:section.coverage', descKey: 'settings:sectionDesc.coverage', icon: Layers },
  // Team Management settings
  teamModels: { labelKey: 'settings:section.teamModels', descKey: 'settings:sectionDesc.teamModels', icon: ServerCog, minTier: 'admin' },
  teamMembers: { labelKey: 'settings:section.teamMembers', descKey: 'settings:sectionDesc.teamMembers', icon: Users, minTier: 'internal', daltonAuthOnly: true },
  teamChannels: { labelKey: 'settings:section.teamChannels', descKey: 'settings:sectionDesc.teamChannels', icon: Bot, minTier: 'internal' },
  // Upstream stubs retained as permanently hidden sections
  trading: { labelKey: 'settings:section.trading', descKey: 'settings:definition.trading', icon: CandlestickChart },
  experiments: { labelKey: 'settings:section.experiments', descKey: 'settings:definition.experiments', icon: FlaskConical },
} satisfies Record<string, SectionMeta>;

export type Section = keyof typeof SECTIONS;

/** Whether `callerTier` satisfies `minTier`. Fails closed on null/undefined/error. */
export function tierSatisfies(
  callerTier: string | null | undefined,
  minTier?: SettingsTierRequirement,
): boolean {
  if (!minTier) return true;
  if (minTier === 'admin') return callerTier === 'admin';
  return callerTier === 'admin' || callerTier === 'internal';
}

/** Whether this user/build draws the section in nav, search, and CommandPalette. */
export function sectionShown(
  section: Section,
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  // DALTON-HOOK(hide-trading): Dalton does not expose live brokerage trading in Settings
  // DALTON-HOOK(hide-experiments): Dalton does not expose experimental labs in Settings
  // DALTON-HOOK(settings-reorg): hide Computers and self-hosted Server sections in Dalton builds; enforce minTier & coverageUnitsOnly across rail, search, and palette
  // DALTON-HOOK(standalone-core): enforce capability gating and hide empty Integrations section in Core
  if (section === 'trading' || section === 'experiments') return false;
  if ((section === 'computers' || section === 'server') && isDaltonBuild) return false;
  const meta: SectionMeta = SECTIONS[section];
  if (meta.desktopOnly && !canDesktopSettings()) return false;
  if (meta.selfHostedOnly && isPlatformMode) return false;
  if (meta.platformOnly && !isPlatformMode) return false;
  if (meta.daltonAuthOnly && !isDaltonAuth) return false;
  if (meta.minTier && !tierSatisfies(ctx?.callerTier, meta.minTier)) return false;
  if (meta.coverageUnitsOnly && ctx?.coverageUnitsEnabled !== true) return false;
  if (meta.capability && !hasRequiredCapability(ctx?.capabilities, meta.capability)) return false;
  // DALTON-HOOK(personal-integrations): also show Integrations when any personal calendar/mail capability is enabled or historical connections exist
  if (
    section === 'integrations' &&
    !isPlatformMode &&
    !hasRequiredCapability(ctx?.capabilities, [
      'channel_gateway',
      'channel_feishu',
      'channel_discord',
      'channel_weixin',
      ...PERSONAL_INTEGRATION_CAPABILITIES,
    ]) &&
    ctx?.hasPersonalIntegrations !== true
  ) {
    return false;
  }
  if (
    section === 'teamChannels' &&
    !hasRequiredCapability(ctx?.capabilities, [
      'channel_feishu',
      'channel_discord',
      'channel_weixin',
      'data_jobs',
    ])
  ) {
    return false;
  }
  return true;
}

export const GROUPS: readonly { labelKey: string; sections: readonly Section[] }[] = [
  {
    labelKey: 'settings:group.personal',
    sections: ['account', 'preferences', 'general', 'chat', 'computers', 'model', 'server', 'budget', 'integrations', 'coverage'],
  },
  {
    labelKey: 'settings:group.team',
    sections: ['teamModels', 'teamMembers', 'teamChannels'],
  },
];

export interface SettingEntry {
  section: Section;
  anchor: string;
  labelKey: string;
  descKey?: string;
  desktopOnly?: boolean;
  advanced?: boolean;
  /** Drawn only while this feature flag is on, so only found then. */
  feature?: string;
  /** Other names the search finds this row by, though the row does not show
   *  them: rows drawn only some of the time beside it, so a search never opens
   *  on a row that is not there, or the name of the group the row sits in. */
  alsoKeys?: readonly string[];
  // DALTON-HOOK(settings-section): bilingual search terms and card-level visibility predicate
  // DALTON-HOOK(standalone-core): optional capability gate on individual setting entries
  keywords?: string;
  capability?: CapabilityRequirement;
  visibleWhen?: (ctx?: DaltonSettingVisibilityContext | null) => boolean;
}

export const SETTINGS = {
  photo: { section: 'account', anchor: 'photo', labelKey: 'settings:photo', descKey: 'settings:photoDesc' },
  name: { section: 'account', anchor: 'name', labelKey: 'common.name', descKey: 'settings:nameDesc' },
  email: { section: 'account', anchor: 'email', labelKey: 'common.email', descKey: 'settings:emailCannotBeChanged' },
  logout: { section: 'account', anchor: 'sign-out', labelKey: 'account.logout', descKey: 'settings:logoutDesc' },

  investingAnswers: { section: 'preferences', anchor: 'investing-profile', labelKey: 'settings:investingAnswers', descKey: 'settings:preferencesDesc' },
  resetPreferences: { section: 'preferences', anchor: 'reset-preferences', labelKey: 'settings:resetPreferences', descKey: 'settings:resetPreferencesDesc' },

  language: { section: 'general', anchor: 'language', labelKey: 'settings:locale', descKey: 'settings:localeDesc' },
  timezone: { section: 'general', anchor: 'timezone', labelKey: 'settings:timezone', descKey: 'settings:timezoneDesc' },
  linkTarget: { section: 'general', anchor: 'link-target', labelKey: 'settings:shareLinkOpen', descKey: 'settings:shareLinkOpenDesc', desktopOnly: true },
  guides: { section: 'general', anchor: 'guides', labelKey: 'onboarding.settings.sectionTitle', descKey: 'onboarding.settings.description' },
  theme: { section: 'general', anchor: 'theme', labelKey: 'settings:themeMode', descKey: 'settings:themeDesc', alsoKeys: ['settings:theme'] },
  fontSize: { section: 'general', anchor: 'font-size', labelKey: 'settings:fontSize', descKey: 'settings:fontSizeDesc' },
  cardStyle: { section: 'general', anchor: 'card-style', labelKey: 'settings:cardStyle', descKey: 'settings:cardStyleDesc', keywords: '卡片样式 card style' },
  notifyTurn: { section: 'general', anchor: 'notify-turn', labelKey: 'settings:desktop.notifyTurn', descKey: 'settings:desktop.notifyTurnDesc', desktopOnly: true, alsoKeys: ['settings:desktop.notificationsGroup'] },
  notifyNeedsInput: { section: 'general', anchor: 'notify-needs-input', labelKey: 'settings:desktop.notifyNeedsInput', descKey: 'settings:desktop.notifyNeedsInputDesc', desktopOnly: true, alsoKeys: ['settings:desktop.notificationsGroup'] },
  notifyWakeup: { section: 'general', anchor: 'notify-wakeup', labelKey: 'settings:desktop.notifyWakeup', descKey: 'settings:desktop.notifyWakeupDesc', desktopOnly: true, alsoKeys: ['settings:desktop.notificationsGroup'] },
  notifyAutomation: { section: 'general', anchor: 'notify-automation', labelKey: 'settings:desktop.notifyAutomation', descKey: 'settings:desktop.notifyAutomationDesc', desktopOnly: true, alsoKeys: ['settings:desktop.notificationsGroup'] },
  notifyStarted: { section: 'general', anchor: 'notify-started', labelKey: 'settings:desktop.notifyStarted', descKey: 'settings:desktop.notifyStartedDesc', desktopOnly: true, alsoKeys: ['settings:desktop.notificationsGroup'] },
  menubarIcon: { section: 'general', anchor: 'menubar-icon', labelKey: 'settings:desktop.menubarIcon', descKey: 'settings:desktop.menubarIconDesc', desktopOnly: true, alsoKeys: ['settings:desktop.shortcut', 'settings:desktop.keepRunning', 'settings:desktop.openAtLogin'] },
  menubarItems: { section: 'general', anchor: 'menubar-items', labelKey: 'settings:desktop.menubarItems', descKey: 'settings:desktop.menubarItemsDesc', desktopOnly: true, alsoKeys: ['settings:desktop.menubarStyle', 'settings:desktop.menubarStyleRotate', 'settings:desktop.menubarStyleSide', 'settings:desktop.addSymbol'] },
  menubarShow: { section: 'general', anchor: 'menubar-show', labelKey: 'settings:desktop.menubarShow', descKey: 'settings:desktop.menubarShowDesc', desktopOnly: true, alsoKeys: ['settings:desktop.menubarGroup'] },
  menubarExtendedHours: { section: 'general', anchor: 'menubar-extended-hours', labelKey: 'settings:desktop.menubarExtended', descKey: 'settings:desktop.menubarExtendedDesc', desktopOnly: true, alsoKeys: ['settings:desktop.menubarGroup'] },
  panelSections: { section: 'general', anchor: 'panel-sections', labelKey: 'settings:desktop.panelSections', descKey: 'settings:desktop.panelSectionsDesc', desktopOnly: true, alsoKeys: ['settings:desktop.panelGroup'] },
  panelTiles: { section: 'general', anchor: 'panel-tiles', labelKey: 'settings:desktop.panelTiles', descKey: 'settings:desktop.panelTilesDesc', desktopOnly: true, alsoKeys: ['settings:desktop.panelGroup'] },
  panelWatchlist: { section: 'general', anchor: 'panel-watchlist', labelKey: 'settings:desktop.panelWatchlist', descKey: 'settings:desktop.panelWatchlistDesc', desktopOnly: true, alsoKeys: ['settings:desktop.panelGroup'] },

  turnDisplay: { section: 'chat', anchor: 'stream-reasoning', labelKey: 'settings:turnDisplay', descKey: 'settings:turnDisplayDesc' },
  streamingMode: { section: 'chat', anchor: 'response-delivery', labelKey: 'settings:streamingMode', descKey: 'settings:streamingModeDesc' },
  toolCallLayout: { section: 'chat', anchor: 'tool-calls', labelKey: 'settings:toolCallLayout', descKey: 'settings:toolCallLayoutDesc' },
  turnEndScroll: { section: 'chat', anchor: 'turn-end', labelKey: 'settings:turnEndScroll', descKey: 'settings:turnEndScrollDesc' },
  composerTray: { section: 'chat', anchor: 'composer-tray', labelKey: 'settings:composerTray', descKey: 'settings:composerTrayDesc' },
  voiceInput: { section: 'chat', anchor: 'voice-input', labelKey: 'settings:voiceInput', descKey: 'settings:voiceInputDesc' },
  // DALTON-HOOK(settings-section): bilingual search keywords for artifacts & output settings
  commentDestination: { section: 'chat', anchor: 'comment-destination', labelKey: 'settings:artifacts.destination', descKey: 'settings:artifacts.destinationDesc', keywords: '评论去向 comment destination' },
  commentEnter: { section: 'chat', anchor: 'comment-enter', labelKey: 'settings:artifacts.enter', descKey: 'settings:artifacts.enterDesc', keywords: '评论 回车 comment enter' },
  artifactAnswers: { section: 'chat', anchor: 'artifact-answers', labelKey: 'settings:artifacts.answers', descKey: 'settings:artifacts.answersDesc', keywords: '评论 回答 artifact answers' },
  outputFormat: { section: 'chat', anchor: 'output-format', labelKey: 'settings:outputFormat', descKey: 'settings:outputFormatDesc', keywords: '输出格式 output format' },
  researchDepth: { section: 'chat', anchor: 'research-depth', labelKey: 'settings:researchDepth', descKey: 'settings:researchDepthDesc' },
  reachingOut: { section: 'chat', anchor: 'reaching-out', labelKey: 'settings:reachingOut', descKey: 'settings:reachingOutDesc' },
  subagents: { section: 'chat', anchor: 'subagents', labelKey: 'settings:subagentsDefault', descKey: 'settings:subagentsDefaultDesc' },
  ownContext: { section: 'chat', anchor: 'own-context', labelKey: 'settings:ownContext', descKey: 'settings:ownContextDesc', feature: 'shared_workspaces_member_context' },
  approveHandoffs: { section: 'chat', anchor: 'approve-handoffs', labelKey: 'settings:autoApproveHandoffs', descKey: 'settings:autoApproveHandoffsDesc' },
  approveWorkspaces: { section: 'chat', anchor: 'approve-workspaces', labelKey: 'settings:autoApproveWorkspaceCreation', descKey: 'settings:autoApproveWorkspaceCreationDesc' },
  searchProvider: { section: 'chat', anchor: 'search-provider', labelKey: 'settings:searchProvider', descKey: 'settings:searchProviderDesc', alsoKeys: ['settings:searchDepth'] },
  daltonCosSharing: { section: 'chat', anchor: 'dalton-cos-sharing', labelKey: 'settings:dalton.cosSharingLabel', descKey: 'settings:dalton.cosSharingDesc', keywords: '研究总监 工作区协同共享 允许同事的研究总监向我的工作区派活 chief of staff sharing colleague dispatch', capability: 'colleague_sharing', visibleWhen: canShowDaltonCosSharingCard },
  // DALTON-HOOK(team-memory-toggle): Home Team Memory user-level setting entry
  // DALTON-HOOK(standalone-core): gate Home Team Memory on conversation_archive
  daltonHomeTeamMemory: { section: 'chat', anchor: 'dalton-home-team-memory', labelKey: 'settings:dalton.homeTeamMemoryLabel', descKey: 'settings:dalton.homeTeamMemoryDesc', keywords: '团队记忆 团队历史研究记录 对话记录 team memory conversation archive', capability: 'conversation_archive', visibleWhen: canShowDaltonHomeTeamMemoryCard },
  // A machine's own settings are in the Computers panel each row opens, so
  // the list is what a search for one of them lands on.
  computers: {
    section: 'computers',
    anchor: 'computers',
    labelKey: 'settings:computers.list',
    descKey: 'computer.computersDesc',
    alsoKeys: [
      'computer.card.spec',
      'computer.card.storage',
      'computer.alwaysOn',
      'computer.filesBackup.label',
      'computer.browserQuality.label',
      'computer.makePrimary.button',
      'computer.rename',
    ],
  },
  newComputer: { section: 'computers', anchor: 'new-computer', labelKey: 'computer.newComputer' },
  modelAccess: { section: 'model', anchor: 'model-access', labelKey: 'modelAccess.title', descKey: 'modelAccess.description' },
  defaultModels: { section: 'model', anchor: 'default-models', labelKey: 'settings:defaultModels', descKey: 'settings:defaultModelsDesc', keywords: '系统默认模型 默认模型 default models' },
  defaultModelScope: { section: 'model', anchor: 'default-model-scope', labelKey: 'settings:defaultModelChange.scopeLabel', descKey: 'settings:defaultModelChange.scopeDesc' },
  quickAccess: { section: 'model', anchor: 'quick-access', labelKey: 'settings:starredModels', descKey: 'settings:starredModelsDesc' },
  modelDetail: { section: 'model', anchor: 'model-detail', labelKey: 'settings:modelTuning.showAdvanced', descKey: 'settings:modelTuning.showAdvancedDesc' },
  accountDefaults: { section: 'model', anchor: 'account-defaults', labelKey: 'settings:modelTuning.sectionDefaults', advanced: true },
  perModel: { section: 'model', anchor: 'per-model', labelKey: 'settings:modelTuning.perModel', descKey: 'settings:modelTuning.perModelDesc', advanced: true },
  routing: { section: 'model', anchor: 'routing', labelKey: 'settings:modelTuning.sectionRouting', descKey: 'settings:fallbackModels', advanced: true, keywords: '模型路由 备选模型 fallback routing' },
  agentLocation: { section: 'server', anchor: 'agent-location', labelKey: 'settings:server.location', descKey: 'settings:server.locationDesc' },
  marketDataKeys: { section: 'server', anchor: 'market-data', labelKey: 'setup:services.marketData', descKey: 'settings:server.marketDataDesc' },
  webSearchKeys: { section: 'server', anchor: 'web-search', labelKey: 'setup:services.webSearch', descKey: 'settings:server.webSearchDesc' },
  // DALTON-HOOK(settings-section): register Dalton model routing under Personal -> Models advanced disclosure
  daltonRouting: { section: 'model', anchor: 'dalton-routing', labelKey: 'settings:modelTuning.daltonHeading', descKey: 'settings:dalton.routingDesc', advanced: true, keywords: '后台任务模型 模型路由 核验 审校 复核 model routing audit reviewer assist extract' },
  daltonAuditModel: { section: 'model', anchor: 'dalton-audit-model', labelKey: 'settings:modelTuning.daltonAuditModel', descKey: 'settings:modelTuning.daltonAuditModelDesc', advanced: true, keywords: '数据核验模型 审计模型 figure audit model' },
  daltonLanguageModel: { section: 'model', anchor: 'dalton-language-model', labelKey: 'settings:modelTuning.daltonLanguageModel', descKey: 'settings:modelTuning.daltonLanguageModelDesc', advanced: true, keywords: '文字审校模型 语言审查模型 language review model' },
  daltonAuditFallbackModels: { section: 'model', anchor: 'dalton-audit-fallback-models', labelKey: 'settings:modelTuning.daltonAuditFallbackModels', descKey: 'settings:modelTuning.daltonAuditFallbackModelsDesc', advanced: true, keywords: '数据核验备用模型 figure audit fallback models' },
  daltonAuditTimeout: { section: 'model', anchor: 'dalton-audit-timeout', labelKey: 'settings:modelTuning.daltonAuditTimeoutS', descKey: 'settings:modelTuning.daltonAuditTimeoutSDesc', advanced: true, keywords: '单次核验超时 核验超时 figure audit timeout' },
  daltonAssistModel: { section: 'model', anchor: 'dalton-assist-model', labelKey: 'settings:modelTuning.daltonAssistModel', descKey: 'settings:modelTuning.daltonAssistModelDesc', advanced: true, keywords: '轻量后台任务模型 辅助模型 auxiliary task model assist' },
  daltonAssistFallbackModels: { section: 'model', anchor: 'dalton-assist-fallback-models', labelKey: 'settings:modelTuning.daltonAssistFallbackModels', descKey: 'settings:modelTuning.daltonAssistFallbackModelsDesc', advanced: true, keywords: '轻量后台任务备用模型 auxiliary task fallback models' },
  daltonReviewerModels: { section: 'model', anchor: 'dalton-reviewer-models', labelKey: 'settings:modelTuning.daltonReviewerModels', descKey: 'settings:modelTuning.daltonReviewerModelsDesc', advanced: true, keywords: '独立复核候选模型 审稿模型 independent reviewer candidates' },
  // DALTON-HOOK(standalone-core): gate archive extraction, channels, native sync, and Feishu scopes on capabilities
  daltonExtractModel: { section: 'model', anchor: 'dalton-extract-model', labelKey: 'settings:modelTuning.daltonExtractModel', descKey: 'settings:modelTuning.daltonExtractModelDesc', advanced: true, keywords: '研究记忆整理模型 对话归档抽卡模型 conversation archive extraction model', capability: 'conversation_archive' },

  plan: { section: 'budget', anchor: 'plan', labelKey: 'settings:planUsage.plan', descKey: 'settings:planUsage.planDesc', visibleWhen: () => isPlatformMode },
  usage: { section: 'budget', anchor: 'usage', labelKey: 'settings:planUsage.usage', descKey: 'settings:planUsage.usageDesc', visibleWhen: () => isPlatformMode },
  billing: { section: 'budget', anchor: 'billing', labelKey: 'settings:planUsage.billing', descKey: 'settings:planUsage.billingDesc', visibleWhen: () => isPlatformMode },
  daltonBudget: { section: 'budget', anchor: 'dalton-budget', labelKey: 'settings:dalton.budgetLabel', descKey: 'settings:dalton.budgetDesc', keywords: '预算 每日预算 花费上限 USD 额度 调额 daily budget spending limit' },

  // DALTON-HOOK(personal-integrations): personal calendar & mail OAuth connections card
  daltonPersonalIntegrations: {
    section: 'integrations',
    anchor: 'dalton-personal-integrations',
    labelKey: 'settings:dalton.personalIntegrationsLabel',
    descKey: 'settings:dalton.personalIntegrationsDesc',
    keywords: '个人日历 个人邮箱 外部账号连接 personal calendar mail',
    visibleWhen: (ctx) =>
      hasRequiredCapability(ctx?.capabilities, PERSONAL_INTEGRATION_CAPABILITIES)
      || ctx?.hasPersonalIntegrations === true,
  },
  // DALTON-HOOK(settings-section): personal messaging channels and push targets live together in Messaging & Channels
  // DALTON-HOOK(weixin-multi-account): include channel_weixin in notify targets capability gate
  daltonNotifyTargets: { section: 'integrations', anchor: 'dalton-notify-targets', labelKey: 'settings:dalton.notifyTargetsLabel', descKey: 'settings:dalton.notifyTargetsDesc', keywords: '通知目标 飞书 Discord 微信 自动推送 消息提醒 notify targets feishu discord weixin wechat 推送总开关 总开关 飞书提醒 推送开关', capability: ['channel_feishu', 'channel_discord', 'channel_weixin'] },
  daltonChannelBindings: { section: 'integrations', anchor: 'dalton-channel-bindings', labelKey: 'settings:dalton.channelBindingsLabel', descKey: 'settings:dalton.channelBindingsDesc', keywords: '飞书 Discord 账号绑定 绑定码 私聊 机器人 bind code feishu discord account', capability: ['channel_feishu', 'channel_discord'] },
  // DALTON-HOOK(weixin-multi-account): personal WeChat QR connection card gated by capability and external access policy
  daltonWeixinBind: { section: 'integrations', anchor: 'dalton-weixin-bind', labelKey: 'settings:dalton.weixinBindLabel', descKey: 'settings:dalton.weixinBindDesc', keywords: '微信 我的微信 扫码 连接 个人微信 wechat weixin qr connect', capability: 'channel_weixin', visibleWhen: canShowDaltonWeixinBindCard },
  chatApps: { section: 'integrations', anchor: 'chat-apps', labelKey: 'settings:integrations.chatApps', descKey: 'settings:integrations.chatAppsDesc', visibleWhen: () => isPlatformMode },
  // DALTON-HOOK(settings-reorg): match runtime platform-only visibility in search & palette
  connectedApps: { section: 'integrations', anchor: 'connected-apps', labelKey: 'settings:integrations.connectedApps', descKey: 'settings:integrations.connectedAppsDesc', visibleWhen: () => isPlatformMode },

  // DALTON-HOOK(watchlist): Personal watchlist tier policies (#dalton-tier-policies) & colleague_sharing-gated team coverage units
  daltonTierPolicies: { section: 'coverage', anchor: 'dalton-tier-policies', labelKey: 'settings:dalton.tierPoliciesLabel', descKey: 'settings:dalton.tierPoliciesDesc', keywords: '各档监控策略 关注列表 核心 跟踪 雷达 盘中盯盘 论点增量 涨跌异动 财报写稿 tier policy watchlist core watch radar cron' },
  daltonCoverage: { section: 'coverage', anchor: 'dalton-coverage', labelKey: 'settings:dalton.coverageLabel', descKey: 'settings:dalton.coverageDesc', keywords: '覆盖结构 行业 独立公司 绑定工作区 未归类工作区 coverage sector company', capability: 'colleague_sharing', visibleWhen: (ctx) => ctx?.coverageUnitsEnabled === true },

  // DALTON-HOOK(team-model-catalog): register team model catalog card in Team Management -> Models & Providers
  daltonTeamModelCatalog: { section: 'teamModels', anchor: 'dalton-team-model-catalog', labelKey: 'settings:dalton.teamCatalogLabel', descKey: 'settings:dalton.teamCatalogDesc', keywords: '团队模型目录 新增模型 模型价格 评分 退役 回滚 供应商变体 team model catalog pricing ratings retire rollback provider variant' },
  daltonProviders: { section: 'teamModels', anchor: 'dalton-providers', labelKey: 'settings:dalton.providersLabel', descKey: 'settings:dalton.providersDesc', keywords: '团队凭据 模型与数据源状态 团队共享凭据 供应商 team credentials provider status' },
  daltonNativeModels: { section: 'teamModels', anchor: 'dalton-native-models', labelKey: 'dalton.nativeModels.title', descKey: 'dalton.nativeModels.footnote', keywords: '模型目录同步 同步模型 退役模型 model catalog sync', capability: 'native_oauth_sync' },

  daltonInvites: { section: 'teamMembers', anchor: 'dalton-invites', labelKey: 'settings:dalton.invitesLabel', descKey: 'settings:dalton.invitesDesc', keywords: '团队邀请 成员权限 邀请链接 team invites allowlist', visibleWhen: canShowDaltonInviteAdminCard },
  daltonFeishuScopes: { section: 'teamMembers', anchor: 'dalton-feishu-scopes', labelKey: 'settings:dalton.feishuScopesLabel', descKey: 'settings:dalton.feishuScopesDesc', keywords: '飞书读取权限 飞书授权 文档 知识库 多维表格 feishu read scopes', capability: 'feishu_docs', visibleWhen: canShowDaltonFeishuScopesCard },

  // DALTON-HOOK(standalone-core): gate team channel cards on respective channel capabilities
  // DALTON-HOOK(weixin-multi-account): admin team WeChat channel management card
  daltonWeixinBot: { section: 'teamChannels', anchor: 'dalton-weixin-bot', labelKey: 'settings:dalton.weixinBotLabel', descKey: 'settings:dalton.weixinBotDesc', keywords: '团队微信渠道管理 个人微信 微信接入 外部成员 槽位 代解除 wechat channel admin', capability: 'channel_weixin', visibleWhen: (ctx) => ctx?.callerTier === 'admin' },
  daltonFeishuGroups: { section: 'teamChannels', anchor: 'dalton-feishu-groups', labelKey: 'settings:dalton.feishuGroupsLabel', descKey: 'settings:dalton.feishuGroupsDesc', keywords: '飞书群 团队群 群聊绑定 feishu groups', capability: 'channel_feishu' },
  daltonDiscordChannels: { section: 'teamChannels', anchor: 'dalton-discord-channels', labelKey: 'settings:dalton.discordChannelsLabel', descKey: 'settings:dalton.discordChannelsDesc', keywords: 'Discord 频道 团队频道 群报告 频道绑定 discord channels', capability: 'channel_discord', visibleWhen: canShowDaltonDiscordChannelsCard },
  daltonSystemJobs: { section: 'teamChannels', anchor: 'dalton-system-jobs', labelKey: 'settings:dalton.systemJobsLabel', descKey: 'settings:dalton.systemJobsDesc', keywords: '自动巡检 团队公共任务 知识库同步 研报增量扫描 公司近况巡检 system jobs cron', capability: 'data_jobs' },

  tradingPermission: { section: 'trading', anchor: TRADING_PERMISSION_ANCHOR, labelKey: 'settings:tradingPermission.title', descKey: 'settings:tradingPermission.desc' },
  experiments: { section: 'experiments', anchor: 'experiments', labelKey: 'settings:experiments', descKey: 'settings:experimentsIntro' },
} satisfies Record<string, SettingEntry>;

const ENTRIES: readonly SettingEntry[] = Object.values(SETTINGS);

// DALTON-HOOK(settings-reorg): redirect removed/renamed anchor hashes such as #connected-accounts and #dalton-weixin-channel
export const LEGACY_ANCHORS: Record<string, { section: Section; targetAnchor: string }> = {
  'connected-accounts': { section: 'model', targetAnchor: SETTINGS.modelAccess.anchor },
  'dalton-weixin': { section: 'integrations', targetAnchor: SETTINGS.daltonWeixinBind.anchor },
  'dalton-weixin-channel': { section: 'integrations', targetAnchor: SETTINGS.daltonWeixinBind.anchor },
  'dalton-coverage-unclassified': { section: 'coverage', targetAnchor: 'dalton-coverage-unclassified' },
};

/** Resolve an anchor hash (including legacy aliases) to a target element ID for scrolling. */
export function resolveScrollAnchor(hash: string): string {
  const clean = hash.replace(/^#/, '');
  if (!clean) return '';
  return LEGACY_ANCHORS[clean]?.targetAnchor ?? clean;
}

/** The setting a URL hash names, if any. */
export function settingAt(hash: string): SettingEntry | undefined {
  const anchor = hash.replace(/^#/, '');
  if (!anchor) return undefined;
  const direct = ENTRIES.find((entry) => entry.anchor === anchor);
  if (direct) return direct;
  const legacy = LEGACY_ANCHORS[anchor];
  if (legacy) {
    return (
      ENTRIES.find((entry) => entry.anchor === legacy.targetAnchor) ?? {
        section: legacy.section,
        anchor: legacy.targetAnchor,
        labelKey: SECTIONS[legacy.section].labelKey,
      }
    );
  }
  return undefined;
}

/** Every setting the search can find on this surface. */
export function searchableSettings(
  desktop: boolean,
  ctx?: DaltonSettingVisibilityContext | null,
  featureOn: (key: string) => boolean = () => true,
): SettingEntry[] {
  // DALTON-HOOK(standalone-core): exclude settings whose required capability is disabled
  return ENTRIES.filter(
    (entry) =>
      (desktop || !entry.desktopOnly) &&
      (!entry.feature || featureOn(entry.feature)) &&
      sectionShown(entry.section, ctx) &&
      hasRequiredCapability(ctx?.capabilities, entry.capability) &&
      (!entry.visibleWhen || entry.visibleWhen(ctx)),
  );
}

// DALTON-HOOK(settings-section): map legacy tab query params and hashes to their new sections
// DALTON-HOOK(settings-reorg): keep compatibility for merged upstream tabs (appearance, desktop, agent, artifacts, plan)
const LEGACY_SECTIONS: Record<string, Section> = {
  userInfo: 'account',
  'user-info': 'account',
  profile: 'account',
  plan: 'budget',
  appearance: 'general',
  desktop: 'general',
  agent: 'chat',
  artifacts: 'chat',
  models: 'model',
  llm: 'model',
  dalton: 'model',
  'team-models': 'teamModels',
  'team-members': 'teamMembers',
  'team-channels': 'teamChannels',
};

// DALTON-HOOK(standalone-core): resolve capability-aware section subtitle so disabled integrations never appear
export function sectionDescKey(
  section: Section,
  ctx?: DaltonSettingVisibilityContext | null,
): string {
  if (section === 'teamMembers' && !hasRequiredCapability(ctx?.capabilities, 'feishu_docs')) {
    return 'settings:sectionDesc.teamMembersCore';
  }
  if (section === 'teamModels' && !hasRequiredCapability(ctx?.capabilities, 'native_oauth_sync')) {
    return 'settings:sectionDesc.teamModelsCore';
  }
  if (section === 'teamChannels') {
    const hasWeixin = hasRequiredCapability(ctx?.capabilities, 'channel_weixin');
    const hasFeishu = hasRequiredCapability(ctx?.capabilities, 'channel_feishu');
    const hasDiscord = hasRequiredCapability(ctx?.capabilities, 'channel_discord');
    const hasDataJobs = hasRequiredCapability(ctx?.capabilities, 'data_jobs');
    if (hasWeixin && hasFeishu && hasDataJobs) {
      return SECTIONS.teamChannels.descKey;
    }
    const suffix = [
      hasWeixin ? 'Weixin' : '',
      hasFeishu ? 'Feishu' : '',
      hasDiscord ? 'Discord' : '',
      hasDataJobs ? 'DataJobs' : '',
    ].join('');
    return suffix ? `settings:sectionDesc.teamChannels${suffix}` : 'settings:sectionDesc.teamChannelsCore';
  }
  return SECTIONS[section].descKey;
}

// DALTON-HOOK(standalone-core): resolve capability-aware setting label so disabled channel names never appear in search results or CommandPalette
export function settingLabelKey(
  entry: SettingEntry,
  ctx?: DaltonSettingVisibilityContext | null,
): string {
  if (entry.anchor === 'dalton-channel-bindings') {
    const canFeishu = hasRequiredCapability(ctx?.capabilities, 'channel_feishu');
    const canDiscord = hasRequiredCapability(ctx?.capabilities, 'channel_discord');
    if (canFeishu && !canDiscord) return 'settings:dalton.channelBindingsLabelFeishu';
    if (!canFeishu && canDiscord) return 'settings:dalton.channelBindingsLabelDiscord';
  }
  return entry.labelKey;
}

export function settingSearchText(
  entry: SettingEntry,
  t: (key: string, opts?: Record<string, unknown>) => string,
  ctx?: DaltonSettingVisibilityContext | null,
): string {
  // DALTON-HOOK(standalone-core): omit disabled channel names from title, description, and search keywords
  const labelKey = settingLabelKey(entry, ctx);
  let descKey = entry.descKey;
  let keywords = entry.keywords ?? '';
  if (entry.anchor === 'dalton-notify-targets' || entry.anchor === 'dalton-channel-bindings') {
    const canFeishu = hasRequiredCapability(ctx?.capabilities, 'channel_feishu');
    const canDiscord = hasRequiredCapability(ctx?.capabilities, 'channel_discord');
    const channelTokens: string[] = [];
    if (canFeishu) channelTokens.push('飞书 feishu 飞书提醒');
    if (canDiscord) channelTokens.push('Discord discord');
    // DALTON-HOOK(weixin-multi-account): delegate WeChat search token gating to Dalton helper
    if (entry.anchor === 'dalton-notify-targets' && canShowDaltonWeixinBindCard(ctx)) {
      channelTokens.push('微信 weixin wechat');
    }
    if (entry.anchor === 'dalton-channel-bindings') {
      if (canFeishu && !canDiscord) {
        descKey = 'settings:dalton.channelBindingsDescFeishu';
      } else if (!canFeishu && canDiscord) {
        descKey = 'settings:dalton.channelBindingsDescDiscord';
      }
    }
    keywords =
      entry.anchor === 'dalton-notify-targets'
        ? `通知目标 自动推送 消息提醒 notify targets 推送总开关 总开关 推送开关 ${channelTokens.join(' ')}`.trim()
        : `账号绑定 绑定码 私聊 机器人 bind code account ${channelTokens.join(' ')}`.trim();
  }
  // DALTON-HOOK(personal-integrations): include provider search tokens only for enabled personal capabilities
  if (entry.anchor === 'dalton-personal-integrations') {
    const tokens = ['个人日历 个人邮箱 外部账号连接 personal calendar mail'];
    if (hasRequiredCapability(ctx?.capabilities, ['google_calendar', 'google_gmail'])) {
      tokens.push('Google google gmail 日历');
    }
    if (hasRequiredCapability(ctx?.capabilities, ['feishu_calendar', 'feishu_mail'])) {
      tokens.push('飞书 feishu 飞书日历 飞书邮箱');
    }
    keywords = tokens.join(' ');
  }
  return [
    t(labelKey, { brand: BRAND_NAME }), // DALTON-HOOK(brand-name)
    descKey ? t(descKey, { brand: BRAND_NAME }) : '', // DALTON-HOOK(brand-name)
    ...(entry.alsoKeys ?? []).map((key) => t(key, { brand: BRAND_NAME })), // DALTON-HOOK(brand-name)
    t(SECTIONS[entry.section].labelKey),
    // DALTON-HOOK(settings-section): match bilingual keywords so Chinese and English queries always hit
    keywords,
  ].join(' ');
}

function asSection(param: string | null): Section | null {
  if (!param) return null;
  if (Object.hasOwn(LEGACY_SECTIONS, param)) return LEGACY_SECTIONS[param];
  return Object.hasOwn(SECTIONS, param) ? (param as Section) : null;
}

/** Resolve the section named by URL without checking tier visibility (used to show pending skeleton vs deny notice). */
export function resolveRequestedSection(params: URLSearchParams, hash: string): Section | null {
  const cleanHash = hash.replace(/^#/, '');
  return settingAt(hash)?.section ?? asSection(cleanHash) ?? asSection(params.get('tab')) ?? null;
}

// DALTON-HOOK(settings-reorg): resolve Settings tab/hash navigation uniformly across Personal & Team sections
/**
 * The section the URL names. A hash naming a setting wins over the tab, so a
 * link written when that setting lived elsewhere still opens on it when shown.
 */
export function readSection(
  params: URLSearchParams,
  hash: string,
  ctx?: DaltonSettingVisibilityContext | null,
): Section {
  const cleanHash = hash.replace(/^#/, '');
  return (
    [settingAt(hash)?.section, asSection(cleanHash), asSection(params.get('tab'))].find(
      (s): s is Section => Boolean(s && sectionShown(s, ctx)),
    ) ?? 'account'
  );
}
