import { describe, it, expect, vi, afterEach } from 'vitest';
import { zhCN, lookup } from '@/test/catalogs';
import {
  EVERFLOW_DEFAULT_CAPABILITY_SET,
  CORE_DEFAULT_CAPABILITY_SET,
  type CapabilityId,
} from '@/hooks/useCapabilities';
import {
  searchableSettings,
  sectionDescKey,
  sectionShown,
  settingSearchText,
  type DaltonSettingVisibilityContext,
} from '../sections';

const tZh = (key: string) => {
  const val = lookup(zhCN, key);
  return typeof val === 'string' ? val : key;
};

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('Settings sections & search capability gating', () => {
  const coreAdminCtx: DaltonSettingVisibilityContext = {
    callerTier: 'admin',
    canInvite: true,
    isPm: true,
    colleagueDispatchEnabled: true,
    cosSharingLoaded: true,
    coverageUnitsEnabled: true,
    discordChannelsAdmin: true,
    capabilities: CORE_DEFAULT_CAPABILITY_SET,
  };

  const everflowAdminCtx: DaltonSettingVisibilityContext = {
    ...coreAdminCtx,
    capabilities: EVERFLOW_DEFAULT_CAPABILITY_SET,
  };

  it('hides Integrations section and proprietary settings in Core while keeping core sections intact', () => {
    expect(sectionShown('account', coreAdminCtx)).toBe(true);
    expect(sectionShown('chat', coreAdminCtx)).toBe(true);
    expect(sectionShown('model', coreAdminCtx)).toBe(true);
    expect(sectionShown('teamModels', coreAdminCtx)).toBe(true);
    expect(sectionShown('teamChannels', coreAdminCtx)).toBe(false);
    expect(sectionShown('integrations', coreAdminCtx)).toBe(false);

    const coreAnchors = new Set(
      searchableSettings(false, coreAdminCtx).map((entry) => entry.anchor),
    );
    expect(coreAnchors.has('dalton-home-team-memory')).toBe(false);
    expect(coreAnchors.has('dalton-extract-model')).toBe(false);
    expect(coreAnchors.has('dalton-notify-targets')).toBe(false);
    expect(coreAnchors.has('dalton-channel-bindings')).toBe(false);
    expect(coreAnchors.has('dalton-weixin-bind')).toBe(false);
    expect(coreAnchors.has('dalton-native-models')).toBe(false);
    expect(coreAnchors.has('dalton-feishu-scopes')).toBe(false);
    expect(coreAnchors.has('dalton-weixin-bot')).toBe(false);
    expect(coreAnchors.has('dalton-feishu-groups')).toBe(false);
    expect(coreAnchors.has('dalton-discord-channels')).toBe(false);
    expect(coreAnchors.has('dalton-system-jobs')).toBe(false);

    // Core settings remain searchable.
    expect(coreAnchors.has('dalton-providers')).toBe(true);
    expect(coreAnchors.has('dalton-budget')).toBe(true);
  });

  it('returns zero proprietary keyword hits from Core settings search', () => {
    const coreEntries = searchableSettings(false, coreAdminCtx);
    const haystack = coreEntries
      .map((entry) => settingSearchText(entry, tZh, coreAdminCtx))
      .join('\n')
      .toLowerCase();

    for (const term of ['飞书', 'feishu', '微信', 'weixin', 'wechat', 'discord', '模型目录同步', '对话归档抽卡', '知识库同步', '研报增量扫描']) {
      expect(haystack).not.toContain(term);
    }
  });

  it('keeps Everflow sections and settings visible identically to before', () => {
    expect(sectionShown('integrations', everflowAdminCtx)).toBe(true);
    expect(sectionShown('teamChannels', everflowAdminCtx)).toBe(true);
    const efAnchors = new Set(
      searchableSettings(false, everflowAdminCtx).map((entry) => entry.anchor),
    );
    expect(efAnchors.has('dalton-extract-model')).toBe(true);
    expect(efAnchors.has('dalton-notify-targets')).toBe(true);
    expect(efAnchors.has('dalton-channel-bindings')).toBe(true);
    expect(efAnchors.has('dalton-weixin-bind')).toBe(true);
    expect(efAnchors.has('dalton-native-models')).toBe(true);
    expect(efAnchors.has('dalton-weixin-bot')).toBe(true);
    expect(efAnchors.has('dalton-feishu-groups')).toBe(true);
    expect(efAnchors.has('dalton-discord-channels')).toBe(true);
    expect(efAnchors.has('dalton-system-jobs')).toBe(true);
  });

  it('re-enables specific entries and channel keywords when individual capabilities are turned on', () => {
    const partialCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['channel_feishu', 'native_oauth_sync', 'data_jobs']),
    };
    expect(sectionShown('integrations', partialCtx)).toBe(true);
    expect(sectionShown('teamChannels', partialCtx)).toBe(true);
    const anchors = new Set(searchableSettings(false, partialCtx).map((e) => e.anchor));
    expect(anchors.has('dalton-feishu-groups')).toBe(true);
    expect(anchors.has('dalton-notify-targets')).toBe(true);
    expect(anchors.has('dalton-native-models')).toBe(true);
    expect(anchors.has('dalton-system-jobs')).toBe(true);
    expect(anchors.has('dalton-weixin-bind')).toBe(false);
    expect(anchors.has('dalton-discord-channels')).toBe(false);

    // Enabling only Discord omits Feishu keywords from notify/bindings search
    const discordOnlyCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['channel_discord']),
    };
    const discordEntries = searchableSettings(false, discordOnlyCtx);
    const discordHaystack = discordEntries
      .map((entry) => settingSearchText(entry, tZh, discordOnlyCtx))
      .join('\n')
      .toLowerCase();
    expect(discordHaystack).toContain('discord');
    expect(discordHaystack).toContain('discord 账号绑定');
    expect(discordHaystack).not.toContain('飞书');
    expect(discordHaystack).not.toContain('feishu');
  });

  it('uses capability-aware subtitles for teamMembers, teamModels, and teamChannels and hides daltonCosSharing in Core', () => {
    expect(sectionDescKey('teamMembers', coreAdminCtx)).toBe('settings:sectionDesc.teamMembersCore');
    expect(tZh(sectionDescKey('teamMembers', coreAdminCtx))).not.toContain('飞书');
    expect(sectionDescKey('teamMembers', everflowAdminCtx)).toBe('settings:sectionDesc.teamMembers');
    expect(tZh(sectionDescKey('teamMembers', everflowAdminCtx))).toContain('飞书');

    // teamModels omits model catalog sync when native_oauth_sync is disabled
    expect(sectionDescKey('teamModels', coreAdminCtx)).toBe('settings:sectionDesc.teamModelsCore');
    expect(tZh(sectionDescKey('teamModels', coreAdminCtx))).not.toContain('模型目录同步');
    expect(sectionDescKey('teamModels', everflowAdminCtx)).toBe('settings:sectionDesc.teamModels');
    expect(tZh(sectionDescKey('teamModels', everflowAdminCtx))).toContain('模型目录同步');

    // teamChannels mentions only enabled channels
    const dataJobsOnlyCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['data_jobs']),
    };
    const dataJobsDesc = tZh(sectionDescKey('teamChannels', dataJobsOnlyCtx));
    expect(sectionDescKey('teamChannels', dataJobsOnlyCtx)).toBe('settings:sectionDesc.teamChannelsDataJobs');
    expect(dataJobsDesc).toContain('自动化巡检任务');
    expect(dataJobsDesc).not.toContain('微信');
    expect(dataJobsDesc).not.toContain('飞书');
    expect(dataJobsDesc).not.toContain('Discord');

    const discordDataJobsCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['channel_discord', 'data_jobs']),
    };
    const discordDataJobsDesc = tZh(sectionDescKey('teamChannels', discordDataJobsCtx));
    expect(sectionDescKey('teamChannels', discordDataJobsCtx)).toBe('settings:sectionDesc.teamChannelsDiscordDataJobs');
    expect(discordDataJobsDesc).toContain('Discord');
    expect(discordDataJobsDesc).toContain('自动化巡检任务');
    expect(discordDataJobsDesc).not.toContain('微信');
    expect(discordDataJobsDesc).not.toContain('飞书');

    expect(sectionDescKey('teamChannels', everflowAdminCtx)).toBe('settings:sectionDesc.teamChannels');

    const coreAnchors = new Set(
      searchableSettings(false, coreAdminCtx).map((entry) => entry.anchor),
    );
    expect(coreAnchors.has('dalton-cos-sharing')).toBe(false);
  });

  it('hides personal WeChat search entry for external members when weixinAllowExternal is false or unconfirmed while keeping internal/admin visible, and enables notify targets when only channel_weixin is on', () => {
    const extUnconfirmedCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: 'external',
      weixinAllowExternal: undefined,
    };
    const extBlockedCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: 'external',
      weixinAllowExternal: false,
    };
    const extAllowedCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: 'external',
      weixinAllowExternal: true,
    };
    const internalCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: 'internal',
      weixinAllowExternal: false,
    };
    const adminCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: 'admin',
      weixinAllowExternal: false,
    };
    const weixinOnlyCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['channel_weixin']),
    };

    const extUnconfirmedAnchors = new Set(
      searchableSettings(false, extUnconfirmedCtx).map((entry) => entry.anchor),
    );
    const extBlockedAnchors = new Set(
      searchableSettings(false, extBlockedCtx).map((entry) => entry.anchor),
    );
    const extAllowedAnchors = new Set(
      searchableSettings(false, extAllowedCtx).map((entry) => entry.anchor),
    );
    const internalAnchors = new Set(
      searchableSettings(false, internalCtx).map((entry) => entry.anchor),
    );
    const adminAnchors = new Set(
      searchableSettings(false, adminCtx).map((entry) => entry.anchor),
    );
    const weixinOnlyAnchors = new Set(
      searchableSettings(false, weixinOnlyCtx).map((entry) => entry.anchor),
    );

    expect(extUnconfirmedAnchors.has('dalton-weixin-bind')).toBe(false);
    expect(extBlockedAnchors.has('dalton-weixin-bind')).toBe(false);
    expect(extBlockedAnchors.has('dalton-weixin-bot')).toBe(false);
    expect(extAllowedAnchors.has('dalton-weixin-bind')).toBe(true);
    expect(internalAnchors.has('dalton-weixin-bind')).toBe(true);
    expect(internalAnchors.has('dalton-weixin-bot')).toBe(false);
    expect(adminAnchors.has('dalton-weixin-bind')).toBe(true);
    expect(adminAnchors.has('dalton-weixin-bot')).toBe(true);
    expect(weixinOnlyAnchors.has('dalton-notify-targets')).toBe(true);
    expect(weixinOnlyAnchors.has('dalton-weixin-bind')).toBe(true);
    expect(weixinOnlyAnchors.has('dalton-channel-bindings')).toBe(false);

    // Unresolved caller identity (loading, error, or null tier) hides personal WeChat entry and keywords
    const pendingCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: null,
      callerTierPending: true,
      weixinAllowExternal: true,
    };
    const errorCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: null,
      callerTierError: true,
      weixinAllowExternal: true,
    };
    const unknownCtx: DaltonSettingVisibilityContext = {
      ...everflowAdminCtx,
      callerTier: null,
      weixinAllowExternal: true,
    };
    for (const ctx of [pendingCtx, errorCtx, unknownCtx]) {
      const entries = searchableSettings(false, ctx);
      const anchors = new Set(entries.map((entry) => entry.anchor));
      expect(anchors.has('dalton-weixin-bind')).toBe(false);
      const notifyEntry = entries.find((entry) => entry.anchor === 'dalton-notify-targets');
      expect(notifyEntry).toBeDefined();
      expect(settingSearchText(notifyEntry!, tZh, ctx).toLowerCase()).not.toContain('微信');
    }
  });

  // DALTON-HOOK(personal-integrations): verify Integrations section & search visibility for single personal capability or historical connections
  it('shows Integrations section and personal integrations search entry on single personal capability or historical connections', () => {
    // Core with all personal & channel capabilities off and no historical connections -> hidden
    expect(sectionShown('integrations', coreAdminCtx)).toBe(false);
    expect(
      searchableSettings(false, coreAdminCtx).some((e) => e.anchor === 'dalton-personal-integrations'),
    ).toBe(false);

    // Core with single google_calendar capability enabled -> visible
    const gcalOnlyCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      capabilities: new Set<CapabilityId>(['company_wiki', 'google_calendar']),
    };
    expect(sectionShown('integrations', gcalOnlyCtx)).toBe(true);
    expect(
      searchableSettings(false, gcalOnlyCtx).some((e) => e.anchor === 'dalton-personal-integrations'),
    ).toBe(true);

    // Core with all capabilities off but historical personal connections present -> visible for disconnect/cleanup
    const historicalCtx: DaltonSettingVisibilityContext = {
      ...coreAdminCtx,
      hasPersonalIntegrations: true,
    };
    expect(sectionShown('integrations', historicalCtx)).toBe(true);
    expect(
      searchableSettings(false, historicalCtx).some(
        (e) => e.anchor === 'dalton-personal-integrations',
      ),
    ).toBe(true);
  });
});
