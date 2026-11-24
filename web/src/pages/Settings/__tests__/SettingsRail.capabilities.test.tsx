import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { queryOptions } from '@tanstack/react-query';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import type { CapabilityId } from '@/hooks/useCapabilities';

let mockCapabilities: ReadonlySet<CapabilityId> = new Set<CapabilityId>(['channel_discord']);

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

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    canInvite: false,
    isPm: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isLoading: false }),
}));

vi.mock('@/pages/Settings/panels/dalton/DaltonCosSharingCard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/panels/dalton/DaltonCosSharingCard')>();
  return {
    ...actual,
    useUserCosSharingQuery: () => ({ data: undefined }),
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
  fetchGlobalSearch: vi.fn(async () => ({ conversations: [], documents: [] })),
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

import { SettingsRail } from '../components/SettingsRail';
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
    const combobox = await screen.findByRole('combobox');

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
});
