import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { ProviderRow } from '../ProviderRow';
import type { DaltonProviderStatusItem } from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  isDaltonAuth: true,
  isShared: false,
  probeState: {
    isPending: false,
    isError: false,
    error: null as Error | null,
    data: undefined as undefined | { provider: string; ok: boolean; message: string; latency_ms: number },
    variables: undefined as undefined | { provider: string },
    mutate: vi.fn<(vars: { provider: string }) => void>(),
  },
}));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.isDaltonAuth;
  },
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: h.isShared ? 'team' : 'off',
    enabled: h.isShared,
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
    isShared: h.isShared,
    canDisconnect: true,
    canManage: true,
    isPm: true,
    canInvite: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('../../../hooks/useProviderStatus', () => ({
  useProviderProbe: () => h.probeState,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts && 'error' in opts ? `${key}:${String(opts.error)}` : key,
    i18n: { language: 'zh-CN' },
  }),
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

function makeMuseItem(overrides: Partial<DaltonProviderStatusItem> = {}): DaltonProviderStatusItem {
  return {
    provider: 'muse-oauth',
    display_name: 'Muse OAuth',
    access_type: 'oauth',
    billing: 'subscription',
    enabled: true,
    configured: true,
    is_subs_active: true,
    subs_tier_name: 'Pro',
    subs_usage: {
      window: { used_percent: 42, resets_at: 1700000000 },
      weekly: { used_percent: 18, resets_at: '2026-11-20T00:00:00Z' },
      tier: 'Pro',
    },
    credential: { source: 'oauth' },
    recent_errors: [],
    errors_24h: 0,
    calls_24h: 12,
    used_by_roles: [{ role: 'lead', model: 'claude-opus' }],
    warnings: [],
    tos_risk: 'none',
    ...overrides,
  };
}

function renderRow(item: DaltonProviderStatusItem) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProviderRow item={item} isAdmin={false} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ProviderRow (muse-oauth subscription & usage)', () => {
  beforeEach(() => {
    h.isDaltonAuth = true;
    h.isShared = false;
    h.probeState.isPending = false;
    h.probeState.isError = false;
    h.probeState.error = null;
    h.probeState.data = undefined;
    h.probeState.variables = undefined;
    h.probeState.mutate.mockReset();
  });

  it('renders subscription status and 5h/weekly usage in local time when present', () => {
    renderRow(makeMuseItem());
    const usage = screen.getByTestId('dalton-muse-subs-usage');
    expect(usage).toHaveTextContent('5h: 42%');
    expect(usage).toHaveTextContent('18%');
    expect(usage.textContent).not.toContain('NaN');
  });

  it('omits the subscription/usage row when fields are missing', () => {
    renderRow(
      makeMuseItem({
        subs_usage: null,
        credential: { source: 'oauth', subs_usage: null },
      }),
    );
    expect(screen.queryByTestId('dalton-muse-subs-usage')).not.toBeInTheDocument();
  });

  it('points bind link to team-model-access in shared mode and model-access in personal mode', () => {
    h.isShared = true;
    const { unmount } = renderRow(makeMuseItem({ configured: false }));
    expect(screen.getByTestId('dalton-provider-connect-muse-oauth')).toHaveAttribute(
      'href',
      '/settings?tab=teamModels#team-model-access',
    );
    unmount();

    h.isShared = false;
    renderRow(makeMuseItem({ configured: false }));
    expect(screen.getByTestId('dalton-provider-connect-muse-oauth')).toHaveAttribute(
      'href',
      '/settings?tab=model#model-access',
    );
  });

  it('displays probe request error message and keeps probe button enabled for retry', () => {
    h.probeState.isError = true;
    h.probeState.error = new Error('Network timeout');
    h.probeState.variables = { provider: 'muse-oauth' };

    renderRow(makeMuseItem());

    expect(screen.getByTestId('dalton-provider-probe-error-muse-oauth')).toHaveTextContent(
      'dalton.providers.probeFailed:Network timeout',
    );
    const probeBtn = screen.getByTestId('dalton-provider-probe-muse-oauth');
    expect(probeBtn).toBeEnabled();
    fireEvent.click(probeBtn);
    expect(h.probeState.mutate).toHaveBeenCalledWith({ provider: 'muse-oauth' });
  });
});
