// DALTON-HOOK(brand-name): the team-only denial notice names the build's brand, so a Core build never says "Everflow"
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import Settings from '../../../Settings';

const h = vi.hoisted(() => ({
  brandName: 'Dalton',
  isCore: true,
  isDaltonAuth: false,
}));

vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get BRAND_NAME() {
    return h.brandName;
  },
  get isCoreProfileBuild() {
    return h.isCore;
  },
  get DALTON_PROFILE() {
    return h.isCore ? ('core' as const) : ('everflow' as const);
  },
  isDaltonBuild: true,
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.isDaltonAuth;
  },
  isDaltonLocalAuth: false,
  get isDaltonSupabaseAuth() {
    return h.isDaltonAuth;
  },
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { id: 'u-1', email: 'analyst@dalton.test', name: 'Analyst', timezone: 'Asia/Shanghai' },
    isLoading: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: {
      risk_preference: { risk_level: 'balanced' },
      investment_preference: { horizon: 'medium_term', focus_custom: 'ai' },
      model_preference: {},
      other_preference: {},
    },
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['prefs'],
  useUpdatePreferences: () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }),
  useClearPreferences: () => ({ mutateAsync: vi.fn(async () => undefined), isPending: false }),
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useCapabilities')>()),
  useCapabilities: () => ({
    profile: h.isCore ? ('core' as const) : ('everflow' as const),
    capabilities: new Set(),
    hasCapability: () => false,
    brandName: h.brandName,
    fingerprint: null,
    tickerLogosEnabled: false,
    isLoading: false,
    isLoaded: true,
    isError: false,
    refetch: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => ({
    tier: 'admin',
    status: 'ready',
    authMode: 'oss',
    isPlatform: false,
    isKnown: true,
    isConfirming: false,
    isMultiUserAuth: false,
    isSingleUserOss: !h.isDaltonAuth,
    isAdmin: true,
    isInternal: true,
    isExternal: false,
    canActAsOwner: true,
    canPublishPublic: true,
    canInvite: false,
    isTeamCredentialsShared: true,
    canManageCredentials: true,
    isPm: true,
    colleagueSharingEnabled: false,
    getLatestIdentity: vi.fn(),
    isConfirmingNow: vi.fn(() => false),
    revalidate: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: 'team',
    enabled: true,
    holder_user_id: null,
    credential_user_id: 'u-1',
    is_pm: true,
    can_disconnect: true,
    can_invite: true,
    caller_tier: 'admin',
    colleague_sharing_enabled: true,
    auth_mode: h.isDaltonAuth ? 'local' : 'oss',
  })),
  useTeamCredentials: () => ({
    isShared: true,
    canManage: true,
    canDisconnect: true,
    isPm: true,
    canInvite: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isLoading: false, isError: false, refetch: vi.fn() }),
  useDaltonCoverageUnitDetail: () => ({ data: undefined, isLoading: false, isError: false }),
  splitCoverageUnits: () => ({ sectors: [], standaloneCompanies: [] }),
  coverageUnitErrorCode: () => null,
  coverageUnitBlockingAutomations: () => null,
}));

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] } }),
}));

vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [], total: 0 } }),
}));

vi.mock('../DaltonTeamModelsPanel', () => ({
  DaltonTeamModelsPanel: () => null,
}));
vi.mock('../DaltonWeixinBotCard', () => ({ DaltonWeixinBotCard: () => null }));
vi.mock('../DaltonFeishuGroupsCard', () => ({ DaltonFeishuGroupsCard: () => null }));
vi.mock('../DaltonNotifyTargetsCard', () => ({ DaltonNotifyTargetsCard: () => null }));

vi.mock('@/pages/Settings/panels/AccountPanel', () => ({ AccountPanel: () => null }));
vi.mock('@/pages/ChatAgent/components/ComputersDialogHost', () => ({ default: () => null }));

// The link a member saved or was sent: the old tab name, a setting inside it.
const CORE_DEEP_LINK = '/settings?tab=team-members#dalton-invites';

// The language is set before anything is drawn: switching it under a mounted
// tree re-renders every reader outside act().
async function renderDeepLink(lng: 'zh-CN' | 'en-US') {
  await i18n.changeLanguage(lng);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[CORE_DEEP_LINK]}>
        <Routes>
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('team-only section denial notice names the build brand', () => {
  beforeEach(() => {
    h.brandName = 'Dalton';
    h.isCore = true;
    h.isDaltonAuth = false;
  });

  it('zh-CN: a Core build deep link to team members shows the Core brand and no "Everflow"', async () => {
    await renderDeepLink('zh-CN');

    const notice = await screen.findByTestId('settings-permission-denied');
    expect(notice).toHaveTextContent('此页面仅在 Dalton 团队版提供');
    expect(notice).not.toHaveTextContent('Everflow');
    expect(document.body).not.toHaveTextContent('Everflow');
  });

  it('en-US: a Core build deep link to team members shows the Core brand and no "Everflow"', async () => {
    await renderDeepLink('en-US');

    const notice = await screen.findByTestId('settings-permission-denied');
    expect(notice).toHaveTextContent('This page is only available in Dalton Team Edition.');
    expect(notice).not.toHaveTextContent('Everflow');
    expect(document.body).not.toHaveTextContent('Everflow');
  });

  it('follows the brand the build sets, as a customer renaming it would', async () => {
    h.brandName = 'Acme Research';
    await renderDeepLink('zh-CN');

    const notice = await screen.findByTestId('settings-permission-denied');
    expect(notice).toHaveTextContent('此页面仅在 Acme Research 团队版提供');
  });

  // What Everflow production reads today, word for word: its build sets the
  // brand to "Everflow", which is what the hard-coded copy used to say.
  it('reads exactly as before on an Everflow build', async () => {
    h.brandName = 'Everflow';
    h.isCore = false;
    await renderDeepLink('zh-CN');

    const notice = await screen.findByTestId('settings-permission-denied');
    expect(notice).toHaveTextContent(/^此页面仅在 Everflow 团队版提供$/);
  });
});
