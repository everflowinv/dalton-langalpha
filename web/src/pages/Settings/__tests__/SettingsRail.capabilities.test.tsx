import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { queryOptions } from '@tanstack/react-query';
import { ApiError } from '@/api/client'; // DALTON-HOOK(external-share-guard)
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import type { CapabilityId } from '@/hooks/useCapabilities';

let mockCapabilities: ReadonlySet<CapabilityId> = new Set<CapabilityId>(['channel_discord']);
// DALTON-HOOK(weixin-multi-account): allow simulating external tier, unresolved identity status, and channel bindings response for WeChat search gating
let mockCallerTier: 'admin' | 'internal' | 'external' = 'admin';
let mockCallerStatus: 'ready' | 'loading' | 'error' = 'ready';
let mockWeixinDenyReason: string | null = null;
let mockWeixinChannelEnabled = true;
let mockWeixinAllowExternal = true;
// DALTON-HOOK(external-share-guard): allow simulating 200 neutral and 404 responses on /sharing/user-settings
let mockCosSharingQuery: { data: { colleague_dispatch_enabled?: boolean } | undefined; error?: unknown } = {
  data: undefined,
};

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      profile: 'core' as const,
      capabilities: mockCapabilities,
      hasCapability: (req: Parameters<typeof actual.hasRequiredCapability>[1]) =>
        actual.hasRequiredCapability(mockCapabilities, req),
      brandName: 'Dalton',
      fingerprint: 'test-fp',
      tickerLogosEnabled: false,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: true,
      isEverflowBuild: false,
    }),
  };
});

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: null, logout: vi.fn() }),
}));

// DALTON-HOOK(team-credentials): export shared fetchTeamCredentialsStatus alongside useTeamCredentials mock
vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: 'off',
    enabled: false,
    holder_user_id: null,
    credential_user_id: 'local-user',
    is_pm: true,
    can_disconnect: true,
    can_invite: false,
    caller_tier: 'admin',
    colleague_sharing_enabled: false,
    auth_mode: 'oss',
  })),
  useTeamCredentials: () => ({
    canInvite: false,
    isPm: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
  }),
}));
// DALTON-HOOK(settings-section): provide admin identity snapshot for SettingsRail migrated to useCallerIdentity
// DALTON-HOOK(weixin-multi-account): support testing external tier visibility in SettingsRail
vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const ready = mockCallerStatus === 'ready';
    const isAdmin = ready && mockCallerTier === 'admin';
    const isInternal = ready && (mockCallerTier === 'admin' || mockCallerTier === 'internal');
    const isExternal = ready && mockCallerTier === 'external';
    const snapshot = {
      tier: ready ? mockCallerTier : ('unknown' as const),
      status: mockCallerStatus,
      authMode: 'oss' as const,
      isPlatform: false,
      isKnown: ready,
      isConfirming: mockCallerStatus === 'loading',
      isMultiUserAuth: false,
      isSingleUserOss: isAdmin,
      isAdmin,
      isInternal,
      isExternal,
      canActAsOwner: isInternal,
      canPublishPublic: isAdmin,
      canInvite: false,
      isTeamCredentialsShared: false,
      canManageCredentials: isAdmin,
      isPm: isAdmin,
      colleagueSharingEnabled: false,
    };
    return {
      ...snapshot,
      getLatestIdentity: () => snapshot,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

// DALTON-HOOK(weixin-multi-account): mock channel bindings lookup for external WeChat entry gating
vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: vi.fn(async () => ({
    user_id: 'u-test',
    user_tier: mockCallerTier,
    home_workspace_id: 'home',
    bindings: {
      feishu: { linked: false, platform: 'feishu', platform_user_id: null, platform_username: null, updated_at: null },
      discord: { linked: false, platform: 'discord', platform_user_id: null, platform_username: null, updated_at: null },
      weixin: { linked: false, platform: 'weixin', platform_user_id: null, platform_username: null, updated_at: null },
    },
    identities: {},
    settings: { version: '1', apps: {} },
    chat_bindings: [],
    weixin_account: {
      linked: false,
      platform: 'weixin',
      status: mockWeixinDenyReason ? 'paused' : 'unconfigured',
      channel_enabled: mockWeixinChannelEnabled,
      allow_external: mockWeixinAllowExternal,
      member_allowed: !mockWeixinDenyReason,
      deny_reason: mockWeixinDenyReason,
      account_id: null,
      platform_user_id: null,
      masked_ilink_user_id: null,
      platform_username: null,
      ilink_username: null,
      active_push_ready: false,
      last_inbound_at: null,
      updated_at: null,
    },
  })),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isLoading: false }),
}));

vi.mock('@/pages/Settings/panels/dalton/DaltonCosSharingCard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/panels/dalton/DaltonCosSharingCard')>();
  return {
    ...actual,
    useUserCosSharingQuery: () => mockCosSharingQuery,
  };
});

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getRecentThreads: vi.fn(async () => ({ threads: [] })),
  getWorkspaceThreads: vi.fn(async () => ({ threads: [] })),
}));

vi.mock('@/api/search', () => ({
  searchConversations: vi.fn(async () => ({
    threads: [],
    turns: [],
    files: [],
    next_cursor: { threads: null, turns: null, files: null },
    failed: [],
  })),
  getSearchPreview: vi.fn(async () => {
    throw new Error('no preview');
  }),
}));

vi.mock('@/lib/dalton/searchApi', () => ({
  fetchGlobalSearch: vi.fn(async () => ({ documents: [] })),
}));

vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [] } }),
}));

vi.mock('@/hooks/useSkills', () => ({
  useSkills: () => ({ data: [] }),
}));

vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () =>
    queryOptions({ queryKey: ['workspaces', 'home'], queryFn: async () => ({ workspace_id: 'home', is_home: true, name: 'Home' }) }),
  useHomeWorkspaceId: () => 'home',
  resolveChatWorkspace: vi.fn(async () => 'home'),
}));

vi.mock('@/hooks/useSymbolSearch', () => ({
  useSymbolSearch: () => ({ hits: [], loading: false }),
}));

vi.mock('@/lib/quotes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useQuotes: () => ({ quotes: {} }),
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light' }),
}));

vi.mock('@/hooks/useAppearance', () => ({
  useAppearance: () => ({ pickTheme: vi.fn() }),
}));

vi.mock('@/components/nav/useNavItems', () => ({
  useNavItems: () => [],
}));

vi.mock('@/pages/Settings/panels/usePreferenceWrite', () => ({
  useSaveFailureToast: () => vi.fn(),
}));

vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({
  useCreateWorkspace: () => vi.fn(),
}));

import { SettingsRail, useDaltonSettingsVisibility } from '../components/SettingsRail'; // DALTON-HOOK(external-share-guard)
import { CommandPaletteProvider, useCommandPalette } from '@/components/CommandPalette/CommandPaletteProvider';

function OpenPaletteButton() {
  const palette = useCommandPalette();
  return (
    <button type="button" onClick={() => palette?.open()}>
      Open palette
    </button>
  );
}

afterEach(async () => {
  mockCapabilities = new Set<CapabilityId>(['channel_discord']);
  // DALTON-HOOK(weixin-multi-account): reset tier and WeChat external policy mocks between tests
  mockCallerTier = 'admin';
  mockCallerStatus = 'ready';
  mockWeixinDenyReason = null;
  mockWeixinChannelEnabled = true;
  mockWeixinAllowExternal = true;
  mockCosSharingQuery = { data: undefined };
  await i18n.changeLanguage('en-US');
});

describe('Discord-only settings search and CommandPalette omit Feishu text', () => {
  it('renders SettingsRail search results without "飞书" when only Discord is enabled', async () => {
    await i18n.changeLanguage('zh-CN');
    mockCapabilities = new Set<CapabilityId>(['channel_discord']);

    renderWithProviders(<SettingsRail section="general" />);
    const input = screen.getByRole('combobox');

    fireEvent.change(input, { target: { value: 'Discord' } });
    const discordList = screen.getByRole('listbox');
    expect(within(discordList).getByText('Discord 账号绑定')).toBeInTheDocument();
    expect(discordList.textContent).not.toContain('飞书');

    fireEvent.change(input, { target: { value: '飞书' } });
    const feishuList = screen.getByRole('listbox');
    expect(within(feishuList).queryAllByRole('option')).toHaveLength(0);
    expect(feishuList.textContent).not.toContain('飞书');
  });

  it('renders CommandPalette search results without "飞书" and does not match "飞书" when only Discord is enabled', async () => {
    await i18n.changeLanguage('zh-CN');
    mockCapabilities = new Set<CapabilityId>(['channel_discord']);

    renderWithProviders(
      <CommandPaletteProvider>
        <OpenPaletteButton />
      </CommandPaletteProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Open palette' }));
    // DALTON-HOOK(weixin-multi-account): allow lazy CommandPalette import under full-suite concurrency
    const combobox = await screen.findByRole('combobox', undefined, { timeout: 5000 });

    fireEvent.change(combobox, { target: { value: 'Discord' } });
    // The match is highlighted, so the label spans several elements.
    const dialog = screen.getByRole('dialog');
    await waitFor(() => {
      expect(dialog.textContent).toContain('Discord 账号绑定');
    });
    expect(dialog.textContent).not.toContain('飞书');

    fireEvent.change(combobox, { target: { value: '飞书' } });
    await waitFor(() => {
      expect(screen.getByRole('dialog').textContent).not.toContain('Discord 账号绑定');
    });
    expect(screen.queryByText(/账号绑定|消息推送/)).toBeNull();
    const settingOptions = within(dialog)
      .queryAllByRole('option')
      .filter((el) => el.textContent?.includes('设置'));
    expect(settingOptions).toHaveLength(0);
  });

  // DALTON-HOOK(external-share-guard): 404 or 200 neutral on /sharing/user-settings hides CoS sharing cleanly
  it('marks CoS sharing as disabled when /sharing/user-settings returns 404 or 200 neutral payload', () => {
    mockCapabilities = new Set<CapabilityId>(['colleague_sharing']);
    mockCosSharingQuery = {
      data: undefined,
      error: new ApiError('Request failed with status code 404', 'ERR_BAD_REQUEST', {
        status: 404,
        statusText: 'Not Found',
        headers: {},
        data: { detail: 'Not found' },
      }),
    };

    function Probe() {
      const ctx = useDaltonSettingsVisibility();
      return (
        <div
          data-testid="vis-probe"
          data-loaded={String(ctx.cosSharingLoaded)}
          data-dispatch={String(ctx.colleagueDispatchEnabled)}
        />
      );
    }

    const { rerender } = renderWithProviders(<Probe />);
    expect(screen.getByTestId('vis-probe')).toHaveAttribute('data-loaded', 'true');
    expect(screen.getByTestId('vis-probe')).toHaveAttribute('data-dispatch', 'false');

    mockCosSharingQuery = {
      data: { colleague_dispatch_enabled: false },
    };
    rerender(<Probe />);
    expect(screen.getByTestId('vis-probe')).toHaveAttribute('data-loaded', 'true');
    expect(screen.getByTestId('vis-probe')).toHaveAttribute('data-dispatch', 'false');
  });

  // DALTON-HOOK(weixin-multi-account): hide personal WeChat search entry for external members when allow_external is false even if channel_enabled is also false
  it('hides personal WeChat from SettingsRail search for external members when external access is disabled (including when both switches are off)', async () => {
    await i18n.changeLanguage('zh-CN');
    mockCapabilities = new Set<CapabilityId>(['channel_gateway', 'channel_weixin']);
    mockCallerTier = 'external';
    mockWeixinChannelEnabled = false;
    mockWeixinAllowExternal = false;
    mockWeixinDenyReason = 'weixin_channel_disabled';

    renderWithProviders(<SettingsRail section="general" />);
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: '微信' } });

    await waitFor(() => {
      const list = screen.getByRole('listbox');
      expect(within(list).queryAllByRole('option')).toHaveLength(0);
    });
  });

  // DALTON-HOOK(weixin-multi-account): hide personal WeChat from search and CommandPalette while caller identity is loading or errored
  it('hides personal WeChat from SettingsRail search and CommandPalette while caller identity is unresolved', async () => {
    await i18n.changeLanguage('zh-CN');
    mockCapabilities = new Set<CapabilityId>(['channel_gateway', 'channel_weixin']);
    mockCallerTier = 'external';
    mockCallerStatus = 'loading';
    mockWeixinAllowExternal = true;

    const { unmount } = renderWithProviders(<SettingsRail section="general" />);
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: '微信' } });
    await waitFor(() => {
      const list = screen.getByRole('listbox');
      expect(within(list).queryAllByRole('option')).toHaveLength(0);
    });
    unmount();

    mockCallerStatus = 'error';
    renderWithProviders(
      <CommandPaletteProvider>
        <OpenPaletteButton />
      </CommandPaletteProvider>,
    );
    fireEvent.click(screen.getByText('Open palette'));
    const paletteInput = await screen.findByRole('combobox', {}, { timeout: 5000 });
    fireEvent.change(paletteInput, { target: { value: '微信' } });
    const dialog = screen.getByRole('dialog');
    await waitFor(() => {
      const settingOpts = within(dialog)
        .queryAllByRole('option')
        .filter((o) => (o.textContent ?? '').includes('设置') || (o.textContent ?? '').includes('我的微信'));
      expect(settingOpts).toHaveLength(0);
    });
  });
});
