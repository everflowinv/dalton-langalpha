import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from '@/i18n';
import type { CapabilityId } from '@/hooks/useCapabilities';
import type {
  DeploymentSettingsResponse,
  fetchDeploymentSettings,
} from '@/lib/dalton/deploymentCredentials';

const state = vi.hoisted(() => ({
  callerTier: 'admin' as 'admin' | 'internal' | 'external' | 'unknown',
  callerStatus: 'ready' as 'ready' | 'loading' | 'error',
  authMode: 'local' as 'local' | 'supabase' | 'oss',
  isSingleUserOss: false,
  isMultiUserAuth: true,
  isDaltonAuthFlag: true,
  fetchDeploymentSettingsMock: vi.fn<typeof fetchDeploymentSettings>(),
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
}));

vi.mock('@/config/brand', () => ({
  BRAND_NAME: 'Dalton',
  BRAND_MARK: 'langalpha',
  DALTON_PROFILE: 'core',
  isCoreProfileBuild: true,
  isDaltonBuild: true,
}));

vi.mock('@/config/daltonAuth', () => ({
  get DALTON_AUTH_MODE() {
    return state.authMode;
  },
  get isDaltonAuth() {
    return state.isDaltonAuthFlag;
  },
  get isDaltonLocalAuth() {
    return state.authMode === 'local';
  },
  get isDaltonSupabaseAuth() {
    return state.authMode === 'supabase';
  },
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: 'u-1', logout: vi.fn() }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { id: 'u-1', email: 'admin@example.com', name: 'Admin', onboarding_completed: true },
    isLoading: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: {}, isLoading: false, isLoaded: true }),
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  const coreCaps = new Set<CapabilityId>(['company_wiki']);
  return {
    ...actual,
    useCapabilities: () => ({
      profile: 'core' as const,
      capabilities: coreCaps,
      hasCapability: (req: Parameters<typeof actual.hasRequiredCapability>[1]) =>
        actual.hasRequiredCapability(coreCaps, req),
      brandName: 'Dalton',
      fingerprint: 'fp-core',
      tickerLogosEnabled: false,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: true,
      isEverflowBuild: false,
    }),
  };
});

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const ready = state.callerStatus === 'ready';
    const isAdmin = ready && state.callerTier === 'admin';
    const isInternal = ready && (state.callerTier === 'admin' || state.callerTier === 'internal');
    const isExternal = ready && state.callerTier === 'external';
    const snap = {
      tier: ready ? state.callerTier : ('unknown' as const),
      status: state.callerStatus,
      authMode: state.authMode,
      isPlatform: false,
      isKnown: ready,
      isConfirming: state.callerStatus === 'loading',
      isMultiUserAuth: state.isMultiUserAuth,
      isSingleUserOss: state.isSingleUserOss,
      isAdmin,
      isInternal,
      isExternal,
      canActAsOwner: isInternal,
      canPublishPublic: isAdmin,
      canInvite: isInternal,
      isTeamCredentialsShared: false,
      canManageCredentials: isAdmin,
      isPm: isAdmin,
      colleagueSharingEnabled: false,
    };
    return {
      ...snap,
      getLatestIdentity: () => snap,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isLoading: false }),
}));

vi.mock('@/pages/Settings/panels/dalton/DaltonCosSharingCard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/panels/dalton/DaltonCosSharingCard')>();
  return {
    ...actual,
    useUserCosSharingQuery: () => ({ data: undefined, error: undefined }),
  };
});

vi.mock('@/lib/dalton/deploymentCredentials', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/deploymentCredentials')>();
  return {
    ...actual,
    fetchDeploymentSettings: (...args: Parameters<typeof fetchDeploymentSettings>) =>
      state.fetchDeploymentSettingsMock(...args),
  };
});

vi.mock('@/pages/ChatAgent/components/ComputersDialogHost', () => ({
  default: () => null,
}));

vi.mock('@/pages/ChatAgent/hooks/computerPanelStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/hooks/computerPanelStore')>();
  return {
    ...actual,
    useCloseComputersOnLeave: () => undefined,
  };
});

import Settings from '../Settings';

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location-search">{loc.search}</div>;
}

const SAMPLE_RESPONSE: DeploymentSettingsResponse = {
  profile: 'core',
  revision: 3,
  encryption_configured: true,
  fields: [],
  redirect_uris: {
    google: 'https://research.example.com/api/v1/dalton/integrations/google/callback',
    feishu: 'https://research.example.com/api/v1/dalton/integrations/feishu/callback',
  },
  feishu_events_url: 'https://research.example.com/api/v1/dalton/integrations/feishu/events',
  google_redirect_uri_eligible: true,
  google_redirect_uri_reason: null,
};

function renderSettings(initialUrl: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Settings />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(async () => {
  state.callerTier = 'admin';
  state.callerStatus = 'ready';
  state.authMode = 'local';
  state.isSingleUserOss = false;
  state.isMultiUserAuth = true;
  state.isDaltonAuthFlag = true;
  state.fetchDeploymentSettingsMock.mockReset();
  await i18n.changeLanguage('en-US');
});

describe('Settings: deploymentCredentials route and auth gates', () => {
  it('renders static login-mode notice in OSS mode on ?tab=deploymentCredentials without calling credentials API', async () => {
    await i18n.changeLanguage('zh-CN');
    state.authMode = 'oss';
    state.isSingleUserOss = true;
    state.isMultiUserAuth = false;
    state.isDaltonAuthFlag = false;
    state.callerTier = 'admin';

    renderSettings('/settings?tab=deploymentCredentials');

    const notice = await screen.findByTestId('deployment-credentials-oss-notice');
    expect(notice).toBeInTheDocument();
    expect(notice.textContent).toContain('请先启用管理员登录模式');
    expect(screen.getByTestId('location-search')).toHaveTextContent('?tab=deploymentCredentials');
    expect(state.fetchDeploymentSettingsMock).not.toHaveBeenCalled();
  });

  it('hides sidebar link and redirects non-admin member from ?tab=deploymentCredentials back to ?tab=account in local/supabase modes', async () => {
    await i18n.changeLanguage('zh-CN');
    for (const mode of ['local', 'supabase'] as const) {
      state.authMode = mode;
      state.isSingleUserOss = false;
      state.isMultiUserAuth = true;
      state.isDaltonAuthFlag = true;
      state.callerTier = 'internal';

      const { unmount } = renderSettings('/settings?tab=deploymentCredentials');

      await waitFor(() => {
        expect(screen.getByTestId('location-search')).toHaveTextContent('?tab=account');
      });
      expect(screen.getByTestId('settings-permission-denied')).toBeInTheDocument();
      expect(screen.queryByRole('link', { name: '集成凭据' })).toBeNull();
      expect(state.fetchDeploymentSettingsMock).not.toHaveBeenCalled();
      unmount();
    }
  });

  it('shows sidebar link and loads lazy DeploymentCredentialsPanel for local admin', async () => {
    await i18n.changeLanguage('zh-CN');
    state.authMode = 'local';
    state.isSingleUserOss = false;
    state.isMultiUserAuth = true;
    state.isDaltonAuthFlag = true;
    state.callerTier = 'admin';
    state.fetchDeploymentSettingsMock.mockResolvedValueOnce(SAMPLE_RESPONSE);

    renderSettings('/settings?tab=deploymentCredentials');

    expect(screen.getByRole('link', { name: '集成凭据' })).toBeInTheDocument();
    const panel = await screen.findByTestId('deployment-credentials-panel');
    expect(panel).toBeInTheDocument();
    expect(state.fetchDeploymentSettingsMock).toHaveBeenCalledTimes(1);
  });
});
