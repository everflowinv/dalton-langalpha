import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { useDaltonSettingsVisibility } from '@/pages/Settings/components/SettingsRail';
import {
  canShowDaltonDiscordChannelsCard,
  searchableSettings,
  SECTIONS,
  tierSatisfies,
} from '@/pages/Settings/sections';
import {
  DaltonDiscordChannelsCard,
  extractDiscordChannelId,
  isValidDiscordSnowflake,
  MAX_DISCORD_CHANNEL_NAME_LENGTH,
  parseDiscordChannelInput,
} from '../DaltonDiscordChannelsCard';
import { DaltonTeamOpsPanel } from '../DaltonTeamOpsPanel';
import type {
  DaltonChannelBindingsResponse,
  DaltonChannelSettingsUpdateResponse,
  DaltonDiscordChannelEntryRequest,
  DaltonDiscordChannelVerifyResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  fetchBindings: vi.fn<() => Promise<DaltonChannelBindingsResponse>>(),
  verifyChannel: vi.fn<(channelId: string) => Promise<DaltonDiscordChannelVerifyResponse>>(),
  mutateDiscordChannel: vi.fn<
    (payload: DaltonDiscordChannelEntryRequest) => Promise<DaltonChannelSettingsUpdateResponse>
  >(),
  isDaltonAuth: true,
  userId: 'u-admin' as string | null,
  callerTier: 'admin' as string | null,
  callerTierPending: false,
  callerTierError: false,
  isPm: true,
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    userId: h.userId,
    user: h.userId ? { id: h.userId } : null,
  }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/utils/daltonApi')>();
  return {
    ...actual,
    fetchDaltonChannelBindings: () => h.fetchBindings(),
    verifyDaltonDiscordChannel: (channelId: string) => h.verifyChannel(channelId),
    mutateDaltonDiscordChannel: (payload: DaltonDiscordChannelEntryRequest) =>
      h.mutateDiscordChannel(payload),
    fetchDaltonFeishuGroups: async () => ({ ok: true, groups: [] }),
    fetchDaltonSystemJobs: async () => ({ ok: true, jobs: [] }),
    sendDaltonNotifyTest: async () => ({ ok: true, detail: null, error_code: null }),
  };
});

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.isDaltonAuth;
  },
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({
    isShared: h.isDaltonAuth,
    canManage: h.callerTier === 'admin' || (!h.isDaltonAuth && h.isPm),
    canDisconnect: h.callerTier === 'admin' || (!h.isDaltonAuth && h.isPm),
    isPm: h.isPm,
    canInvite: h.callerTier === 'admin' || h.callerTier === 'internal',
    callerTier: h.callerTier,
    callerTierPending: h.callerTierPending,
    callerTierError: h.callerTierError,
  }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, params?: Record<string, unknown>) =>
      params && Object.keys(params).length > 0 ? `${key}:${JSON.stringify(params)}` : key,
  }),
}));

vi.mock('../DaltonWeixinBotCard', () => ({
  DaltonWeixinBotCard: () => <div data-testid="mock-weixin-bot-card" />,
}));

vi.mock('../DaltonFeishuGroupsCard', () => ({
  DaltonFeishuGroupsCard: () => <div data-testid="mock-feishu-groups-card" />,
}));

vi.mock('../DaltonSystemJobsCard', () => ({
  DaltonSystemJobsCard: () => <div data-testid="mock-system-jobs-card" />,
}));

vi.mock('../DaltonNotifyTargetsCard', () => ({
  DaltonNotifyTargetsCard: () => <div data-testid="mock-notify-targets-card" />,
}));

function makeBindings(
  overrides: Partial<DaltonChannelBindingsResponse> = {},
): DaltonChannelBindingsResponse {
  return {
    user_id: 'u-admin',
    user_tier: 'admin',
    home_workspace_id: 'ws-home',
    bindings: {
      feishu: {
        linked: true,
        platform: 'feishu',
        platform_user_id: 'ou_1',
        platform_username: 'Ada',
        updated_at: null,
      },
      discord: {
        linked: true,
        platform: 'discord',
        platform_user_id: '111222333444555666',
        platform_username: 'ada#0001',
        updated_at: null,
      },
      weixin: {
        linked: false,
        platform: 'weixin',
        platform_user_id: null,
        platform_username: null,
        updated_at: null,
      },
    },
    identities: {},
    settings: {
      version: 'v3',
      apps: {
        feishu: {
          preferred: 'feishu:@me',
          chats: { 'feishu:oc_keep': { workspace_id: 'ws-1', name: 'Feishu Keep' } },
          automation_output: {},
          agent_messages: { enabled: true, allowed: ['feishu:@me'] },
        },
        discord: {
          preferred: 'discord:@me',
          chats: {
            'discord:1528743490555740230': {
              workspace_id: 'ws-semi',
              name: 'Semi Desk',
            },
          },
          automation_output: { 'ws-semi': 'discord:1528743490555740230' },
          agent_messages: { enabled: true, allowed: ['discord:@me'] },
        },
      },
    },
    chat_bindings: [
      {
        platform: 'discord',
        chat_address: 'discord:1528743490555740230',
        chat_name: 'Semi Desk',
        workspace_id: 'ws-semi',
        bound_by_user_id: 'u-admin',
        last_used_at: 1763500000,
      },
    ],
    weixin_bot: null,
    ...overrides,
  };
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonDiscordChannelsCard />
    </QueryClientProvider>,
  );
}

describe('extractDiscordChannelId, parseDiscordChannelInput & isValidDiscordSnowflake', () => {
  it('enforces 17-20 digit snowflakes < 2^64, accepts channel/message URLs, and rejects DM links', () => {
    expect(isValidDiscordSnowflake('1481256083589697566')).toBe(true);
    expect(isValidDiscordSnowflake('18446744073709551615')).toBe(true);
    expect(isValidDiscordSnowflake('18446744073709551616')).toBe(false);
    expect(isValidDiscordSnowflake('9'.repeat(32))).toBe(false);
    expect(isValidDiscordSnowflake('1234567890123456')).toBe(false);

    expect(extractDiscordChannelId('1481256083589697566')).toBe('1481256083589697566');
    expect(
      extractDiscordChannelId(
        'https://discord.com/channels/700000001000000001/1481256083589697566',
      ),
    ).toBe('1481256083589697566');
    expect(
      extractDiscordChannelId(
        'https://discord.com/channels/700000001000000001/1481256083589697566/',
      ),
    ).toBe('1481256083589697566');
    expect(
      extractDiscordChannelId(
        'https://discord.com/channels/700000001000000001/1481256083589697566/1481256999999999999',
      ),
    ).toBe('1481256083589697566');
    expect(
      parseDiscordChannelInput('https://discord.com/channels/@me/1481256083589697566'),
    ).toEqual({ channelId: null, reason: 'dm_link' });
    expect(
      parseDiscordChannelInput('https://discord.com/channels/@me/1481256083589697566/999999'),
    ).toEqual({ channelId: null, reason: 'dm_link' });
    expect(extractDiscordChannelId('9'.repeat(32))).toBeNull();
    expect(extractDiscordChannelId('not-a-channel')).toBeNull();
    expect(extractDiscordChannelId('https://example.com/channels/1/2')).toBeNull();
  });
});

describe('DaltonDiscordChannelsCard', () => {
  beforeEach(() => {
    h.fetchBindings.mockReset();
    h.verifyChannel.mockReset();
    h.mutateDiscordChannel.mockReset();
    h.isDaltonAuth = true;
    h.userId = 'u-admin';
    h.callerTier = 'admin';
    h.callerTierPending = false;
    h.callerTierError = false;
    h.isPm = true;
  });

  it('renders registered Discord channels with name and ID only (no last-used or remove button)', async () => {
    h.fetchBindings.mockResolvedValueOnce(makeBindings());

    renderCard();

    expect(await screen.findByText('Semi Desk')).toBeInTheDocument();
    const row = screen.getByTestId('dalton-discord-channel-row-1528743490555740230');
    expect(row).toHaveTextContent('1528743490555740230');
    expect(row).not.toHaveTextContent('lastUsedLabel');
    expect(row).not.toHaveTextContent('neverUsed');
    expect(
      screen.queryByTestId('dalton-discord-channel-remove-1528743490555740230'),
    ).not.toBeInTheDocument();
  });

  it('renders empty state when no Discord channels are registered', async () => {
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({ settings: { version: 'v1', apps: {} }, chat_bindings: [] }),
    );

    renderCard();

    expect(await screen.findByText('dalton.discordChannels.empty')).toBeInTheDocument();
  });

  it('hides legacy guild/channel keys from registered list, shows legacy count note, and deduplicates by channel ID', async () => {
    h.fetchBindings.mockResolvedValue(
      makeBindings({
        settings: {
          version: 'v3',
          apps: {
            discord: {
              preferred: 'discord:@me',
              chats: {
                'discord:700000001000000001/1481256083589697566': {
                  workspace_id: 'ws-semi',
                  name: 'Gateway Thread Format',
                },
                'discord:1528743490555740230': {
                  workspace_id: null,
                  name: 'Semi Desk',
                },
              },
              automation_output: {},
              agent_messages: { enabled: true, allowed: ['discord:@me'] },
            },
          },
        },
      }),
    );
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1481256083589697566',
      channel_name: 'research-reports',
      detail: null,
    });
    h.mutateDiscordChannel.mockResolvedValueOnce({
      version: 'v4',
      settings: {},
      changes: ['Updated discord channel settings'],
    });

    renderCard();

    expect(await screen.findByText('Semi Desk')).toBeInTheDocument();
    expect(screen.queryByText('Gateway Thread Format')).not.toBeInTheDocument();
    expect(screen.getByTestId('dalton-discord-channels-legacy-note')).toHaveTextContent(
      'dalton.discordChannels.legacyBindingsNote:{"count":1}',
    );

    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1528743490555740230' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      'dalton.discordChannels.alreadyAdded',
    );
    expect(h.verifyChannel).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    await waitFor(() => {
      expect(h.verifyChannel).toHaveBeenCalledWith('1481256083589697566');
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'add',
        channel_id: '1481256083589697566',
        version: 'v3',
        name: 'research-reports',
      });
    });
  });

  it('extracts channel ID from Discord link, verifies with backend, and submits single-entry add', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1481256083589697566',
      channel_name: 'research-reports',
      detail: null,
    });
    h.mutateDiscordChannel.mockResolvedValueOnce({
      version: 'v4',
      settings: {},
      changes: ['Updated discord channel settings'],
    });

    renderCard();

    const idInput = await screen.findByTestId('dalton-discord-channel-id-input');
    const nameInput = screen.getByTestId('dalton-discord-channel-name-input');
    const addBtn = screen.getByTestId('dalton-discord-channel-add-btn');

    fireEvent.change(idInput, {
      target: { value: 'https://discord.com/channels/700000001000000001/1481256083589697566' },
    });
    fireEvent.change(nameInput, { target: { value: 'Daily Reports' } });
    fireEvent.click(addBtn);

    await waitFor(() => {
      expect(h.verifyChannel).toHaveBeenCalledWith('1481256083589697566');
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'add',
        channel_id: '1481256083589697566',
        version: 'v3',
        name: 'Daily Reports',
      });
    });
  });

  it('rejects @me DM links, out-of-range IDs, and >255-char names on add and rename', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());

    renderCard();

    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: 'https://discord.com/channels/@me/1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      'dalton.discordChannels.dmLinkNotAllowed',
    );

    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '9'.repeat(32) },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      'dalton.discordChannels.invalidChannelId',
    );

    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.change(screen.getByTestId('dalton-discord-channel-name-input'), {
      target: { value: 'x'.repeat(MAX_DISCORD_CHANNEL_NAME_LENGTH + 1) },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      `dalton.discordChannels.nameTooLong:{"max":${MAX_DISCORD_CHANNEL_NAME_LENGTH}}`,
    );

    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-1528743490555740230'));
    fireEvent.change(
      screen.getByTestId('dalton-discord-channel-rename-input-1528743490555740230'),
      { target: { value: 'y'.repeat(MAX_DISCORD_CHANNEL_NAME_LENGTH + 1) } },
    );
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-save-1528743490555740230'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      `dalton.discordChannels.nameTooLong:{"max":${MAX_DISCORD_CHANNEL_NAME_LENGTH}}`,
    );

    expect(h.verifyChannel).not.toHaveBeenCalled();
    expect(h.mutateDiscordChannel).not.toHaveBeenCalled();
  });

  it('counts Unicode code points for emoji names (accepts 128 and 255 emojis, rejects 256)', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.verifyChannel.mockResolvedValue({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1481256083589697566',
      channel_name: 'reports',
      detail: null,
    });
    h.mutateDiscordChannel.mockResolvedValue({
      version: 'v4',
      settings: {},
      changes: ['Updated discord channel settings'],
    });

    const emoji128 = '📊'.repeat(128); // UTF-16 length = 256, code points = 128
    const emoji255 = '📊'.repeat(255); // UTF-16 length = 510, code points = 255
    const emoji256 = '📊'.repeat(256); // UTF-16 length = 512, code points = 256

    renderCard();

    // 1. Add with 128 emojis (UTF-16 .length === 256) succeeds
    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.change(screen.getByTestId('dalton-discord-channel-name-input'), {
      target: { value: emoji128 },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    await waitFor(() => {
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'add',
        channel_id: '1481256083589697566',
        version: 'v3',
        name: emoji128,
      });
    });

    // 2. Rename to 255 emojis succeeds
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-1528743490555740230'));
    fireEvent.change(
      screen.getByTestId('dalton-discord-channel-rename-input-1528743490555740230'),
      { target: { value: emoji255 } },
    );
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-save-1528743490555740230'));

    await waitFor(() => {
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'rename',
        channel_id: '1528743490555740230',
        version: 'v3',
        name: emoji255,
      });
    });

    // 3. Add with 256 emojis is rejected client-side
    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697567' },
    });
    fireEvent.change(screen.getByTestId('dalton-discord-channel-name-input'), {
      target: { value: emoji256 },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      `dalton.discordChannels.nameTooLong:{"max":${MAX_DISCORD_CHANNEL_NAME_LENGTH}}`,
    );

    // 4. Rename to 256 emojis is rejected client-side
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-1528743490555740230'));
    fireEvent.change(
      screen.getByTestId('dalton-discord-channel-rename-input-1528743490555740230'),
      { target: { value: emoji256 } },
    );
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-save-1528743490555740230'));
    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      `dalton.discordChannels.nameTooLong:{"max":${MAX_DISCORD_CHANNEL_NAME_LENGTH}}`,
    );
    expect(h.mutateDiscordChannel).toHaveBeenCalledTimes(2);
  });

  it('shows missing-binding badge when chat_bindings lacks a user chat and preserves custom name when re-adding with blank name input', async () => {
    h.fetchBindings.mockResolvedValue(
      makeBindings({
        chat_bindings: [],
      }),
    );
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1528743490555740230',
      channel_name: 'discord-server-default-name',
      detail: null,
    });
    h.mutateDiscordChannel.mockResolvedValueOnce({
      version: 'v4',
      settings: {},
      changes: ['Restored discord shared binding'],
    });

    renderCard();

    const row = await screen.findByTestId('dalton-discord-channel-row-1528743490555740230');
    expect(row).toHaveTextContent('dalton.discordChannels.missingBindingBadge');
    expect(row).not.toHaveTextContent('dalton.discordChannels.registeredBadge');

    fireEvent.change(screen.getByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1528743490555740230' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    await waitFor(() => {
      expect(h.verifyChannel).toHaveBeenCalledWith('1528743490555740230');
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'add',
        channel_id: '1528743490555740230',
        version: 'v3',
      });
    });
  });

  it('surfaces backend message and problems when mutation fails with 400', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1481256083589697566',
      channel_name: 'reports',
      detail: null,
    });
    h.mutateDiscordChannel.mockRejectedValueOnce(
      new ApiError('Request failed with status code 400', 'ERR_BAD_REQUEST', {
        status: 400,
        statusText: 'Bad Request',
        headers: {},
        data: {
          code: 'invalid',
          message: 'Settings validation failed',
          problems: [
            {
              field: 'discord.chats["discord:1481256083589697566"]',
              message: 'already registered',
            },
          ],
        },
      }),
    );

    renderCard();

    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    const errBox = await screen.findByTestId('dalton-discord-channels-error');
    expect(errBox).toHaveTextContent('Settings validation failed');
    expect(errBox).toHaveTextContent(
      'discord.chats["discord:1481256083589697566"]: already registered',
    );
    expect(errBox).not.toHaveTextContent('Request failed with status code 400');
  });

  it('allows adding when backend returns no_token and shows online-verify warning', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: false,
      reason: 'no_token',
      channel_id: '1481256083589697566',
      channel_name: null,
      detail: null,
    });
    h.mutateDiscordChannel.mockResolvedValueOnce({
      version: 'v4',
      settings: {},
      changes: [],
    });

    renderCard();

    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    expect(
      await screen.findByTestId('dalton-discord-channels-no-token-notice'),
    ).toHaveTextContent('dalton.discordChannels.noTokenNotice');
    expect(h.mutateDiscordChannel).toHaveBeenCalledTimes(1);
  });

  it('shows verification error and skips mutation when channel fails backend check', async () => {
    h.fetchBindings.mockResolvedValueOnce(makeBindings());
    h.verifyChannel.mockResolvedValueOnce({
      ok: false,
      verified: false,
      reason: 'verification_failed',
      channel_id: '1481256083589697566',
      channel_name: null,
      detail: 'Discord channel target must be a guild text channel.',
    });

    renderCard();

    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    expect(await screen.findByTestId('dalton-discord-channels-error')).toHaveTextContent(
      'Discord channel target must be a guild text channel.',
    );
    expect(h.mutateDiscordChannel).not.toHaveBeenCalled();
  });

  it('shows refresh-and-retry notice on 409 version_conflict without auto-overwriting', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.verifyChannel.mockResolvedValueOnce({
      ok: true,
      verified: true,
      reason: null,
      channel_id: '1481256083589697566',
      channel_name: 'reports',
      detail: null,
    });
    h.mutateDiscordChannel.mockRejectedValueOnce(
      new ApiError('Request failed with status code 409', 'ERR_BAD_REQUEST', {
        status: 409,
        statusText: 'Conflict',
        headers: {},
        data: {
          code: 'version_conflict',
          message: 'Channel settings version conflict',
        },
      }),
    );

    renderCard();

    fireEvent.change(await screen.findByTestId('dalton-discord-channel-id-input'), {
      target: { value: '1481256083589697566' },
    });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-add-btn'));

    expect(
      await screen.findByTestId('dalton-discord-channels-version-conflict'),
    ).toHaveTextContent('dalton.discordChannels.versionConflict');
    expect(h.mutateDiscordChannel).toHaveBeenCalledTimes(1);
  });

  it('supports renaming a channel', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.mutateDiscordChannel.mockResolvedValueOnce({
      version: 'v4',
      settings: {},
      changes: [],
    });

    renderCard();

    fireEvent.click(await screen.findByTestId('dalton-discord-channel-rename-1528743490555740230'));
    const renameInput = screen.getByTestId('dalton-discord-channel-rename-input-1528743490555740230');
    fireEvent.change(renameInput, { target: { value: 'Semi Research' } });
    fireEvent.click(screen.getByTestId('dalton-discord-channel-rename-save-1528743490555740230'));

    await waitFor(() => {
      expect(h.mutateDiscordChannel).toHaveBeenCalledWith({
        action: 'rename',
        channel_id: '1528743490555740230',
        version: 'v3',
        name: 'Semi Research',
      });
    });
  });

  it('shows card and search entry only for verified admin in both Dalton auth and OSS local mode', async () => {
    // 1. Search visibility predicate (sections.ts)
    expect(
      searchableSettings(false, { callerTier: 'admin', discordChannelsAdmin: true }).map(
        (e) => e.anchor,
      ),
    ).toContain('dalton-discord-channels');
    expect(
      searchableSettings(false, { callerTier: 'admin', discordChannelsAdmin: false }).map(
        (e) => e.anchor,
      ),
    ).not.toContain('dalton-discord-channels');
    expect(
      canShowDaltonDiscordChannelsCard({ callerTier: 'internal', discordChannelsAdmin: false }),
    ).toBe(false);
    expect(
      canShowDaltonDiscordChannelsCard({ callerTier: 'external' }),
    ).toBe(false);

    // 2. Dalton auth mode: admin shows, internal/external hide
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'admin' }));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    h.isDaltonAuth = true;
    h.callerTier = 'external';
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId('dalton-discord-channels-card')).not.toBeInTheDocument();

    h.callerTier = 'internal';
    rerender(
      <QueryClientProvider client={client}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId('dalton-discord-channels-card')).not.toBeInTheDocument();

    h.callerTier = 'admin';
    rerender(
      <QueryClientProvider client={client}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId('dalton-discord-channels-card')).toBeInTheDocument();

    // 3. OSS local mode (isDaltonAuth=false, isPm=true, callerTier=null):
    // must follow backend bindings user_tier rather than treating local user as admin
    const ossClientNonAdmin = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    h.isDaltonAuth = false;
    h.callerTier = null;
    h.isPm = true;
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'external' }));
    render(
      <QueryClientProvider client={ossClientNonAdmin}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(h.fetchBindings).toHaveBeenCalled();
    });
    expect(screen.queryAllByTestId('dalton-discord-channels-card')).toHaveLength(1); // only previous Dalton-auth admin render

    const ossClientAdmin = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'admin' }));
    render(
      <QueryClientProvider client={ossClientAdmin}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(screen.getAllByTestId('dalton-discord-channels-card')).toHaveLength(2);
    });
  });

  it('falls back to /channels/bindings user_tier when /credentials/status fails or omits caller_tier, and respects mismatched tiers', async () => {
    const createClient = () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      });

    // 1. /credentials/status failed (callerTierError=true, callerTier=null), while /channels/bindings returns admin
    h.isDaltonAuth = true;
    h.userId = 'u-admin';
    h.callerTier = null;
    h.callerTierError = true;
    h.callerTierPending = false;
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'admin' }));

    const fallbackClient = createClient();
    const { result: errFallbackHook } = renderHook(() => useDaltonSettingsVisibility(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={fallbackClient}>{children}</QueryClientProvider>
      ),
    });

    await waitFor(() => {
      expect(errFallbackHook.current.callerTier).toBe('admin');
      expect(errFallbackHook.current.discordChannelsAdmin).toBe(true);
    });
    expect(tierSatisfies(errFallbackHook.current.callerTier, SECTIONS.teamChannels.minTier)).toBe(true);
    expect(searchableSettings(false, errFallbackHook.current).map((e) => e.anchor)).toContain(
      'dalton-discord-channels',
    );

    render(
      <QueryClientProvider client={fallbackClient}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId('dalton-discord-channels-card')).toBeInTheDocument();

    // 2. /credentials/status omits caller_tier (callerTierError=false, callerTier=null), while /channels/bindings returns admin
    h.callerTier = null;
    h.callerTierError = false;
    const missingTierClient = createClient();
    const { result: missingTierHook } = renderHook(() => useDaltonSettingsVisibility(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={missingTierClient}>{children}</QueryClientProvider>
      ),
    });

    await waitFor(() => {
      expect(missingTierHook.current.callerTier).toBe('admin');
      expect(missingTierHook.current.discordChannelsAdmin).toBe(true);
    });

    // 3. Mismatch: /credentials/status says admin, /channels/bindings says internal -> fail closed for card & search
    h.callerTier = 'admin';
    h.callerTierError = false;
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'internal' }));
    const mismatchAdminClient = createClient();
    const { result: mismatchAdminHook } = renderHook(() => useDaltonSettingsVisibility(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={mismatchAdminClient}>{children}</QueryClientProvider>
      ),
    });

    await waitFor(() => {
      expect(mismatchAdminHook.current.discordChannelsAdmin).toBe(false);
    });
    expect(searchableSettings(false, mismatchAdminHook.current).map((e) => e.anchor)).not.toContain(
      'dalton-discord-channels',
    );

    const { container: mismatchContainer } = render(
      <QueryClientProvider client={mismatchAdminClient}>
        <DaltonTeamOpsPanel />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(
        mismatchContainer.querySelector('[data-testid="dalton-discord-channels-card"]'),
      ).toBeNull();
    });

    // 4. Mismatch: /credentials/status says internal, /channels/bindings says admin -> do not escalate card/search
    h.callerTier = 'internal';
    h.callerTierError = false;
    h.fetchBindings.mockResolvedValue(makeBindings({ user_tier: 'admin' }));
    const mismatchInternalClient = createClient();
    const { result: mismatchInternalHook } = renderHook(() => useDaltonSettingsVisibility(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={mismatchInternalClient}>{children}</QueryClientProvider>
      ),
    });

    await waitFor(() => {
      expect(mismatchInternalHook.current.callerTier).toBe('internal');
      expect(mismatchInternalHook.current.discordChannelsAdmin).toBe(false);
    });
  });
});
