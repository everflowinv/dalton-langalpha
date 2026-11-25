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
});
