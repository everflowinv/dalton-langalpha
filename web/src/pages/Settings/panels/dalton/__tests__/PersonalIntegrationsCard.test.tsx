import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import type { CapabilityId } from '@/hooks/useCapabilities';
import type {
  PersonalIntegrationConnection,
  PersonalIntegrationConnectorInfo,
  PersonalIntegrationPurgeLedger,
  PersonalIntegrationsListResponse,
} from '@/lib/dalton/personalIntegrationsApi';
import { PersonalIntegrationsCard } from '../PersonalIntegrationsCard';

const h = vi.hoisted(() => ({
  profile: 'everflow' as 'everflow' | 'core',
  capabilities: new Set<CapabilityId>(['google_calendar', 'feishu_calendar']),
  fetchList: vi.fn<() => Promise<PersonalIntegrationsListResponse>>(),
  authorize: vi.fn(),
  confirm: vi.fn(),
  disconnectService: vi.fn(),
  disconnectProvider: vi.fn(),
  retryRevoke: vi.fn(),
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      profile: h.profile,
      capabilities: h.capabilities,
      hasCapability: (req: CapabilityId | readonly CapabilityId[] | null | undefined) => {
        if (!req) return true;
        if (typeof req === 'string') return h.capabilities.has(req);
        return req.some((c) => h.capabilities.has(c));
      },
      brandName: 'Dalton',
      fingerprint: 'fp-1',
      tickerLogosEnabled: true,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: h.profile === 'core',
      isEverflowBuild: h.profile === 'everflow',
    }),
  };
});

vi.mock('@/lib/dalton/personalIntegrationsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/personalIntegrationsApi')>();
  return {
    ...actual,
    fetchPersonalIntegrations: () => h.fetchList(),
    authorizePersonalIntegration: (...args: unknown[]) => h.authorize(...args),
    confirmPersonalIntegration: (...args: unknown[]) => h.confirm(...args),
    disconnectPersonalIntegrationService: (...args: unknown[]) => h.disconnectService(...args),
    disconnectPersonalIntegrationProvider: (...args: unknown[]) => h.disconnectProvider(...args),
    retryPersonalIntegrationRevoke: (...args: unknown[]) => h.retryRevoke(...args),
  };
});

function makeConnector(
  provider: 'google' | 'feishu',
  service: 'calendar' | 'mail',
  overrides: Partial<PersonalIntegrationConnectorInfo> = {},
): PersonalIntegrationConnectorInfo {
  return {
    provider,
    service,
    capability_id:
      provider === 'google'
        ? service === 'calendar'
          ? 'google_calendar'
          : 'google_gmail'
        : service === 'calendar'
          ? 'feishu_calendar'
          : 'feishu_mail',
    batch: service === 'calendar' ? 1 : provider === 'feishu' ? 2 : 3,
    enabled: true,
    ready: true,
    credentials_ready: true,
    callback_url: `https://dalton.example.com/api/v1/dalton/integrations/${provider}/callback`,
    events_url:
      provider === 'feishu'
        ? 'https://dalton.example.com/api/v1/dalton/integrations/feishu/events'
        : null,
    ...overrides,
  };
}

function makeConnection(
  provider: 'google' | 'feishu',
  service: 'calendar' | 'mail',
  overrides: Partial<PersonalIntegrationConnection> = {},
): PersonalIntegrationConnection {
  return {
    provider,
    service,
    provider_account_id: `acct-${provider}`,
    provider_email: provider === 'google' ? 'alice@gmail.com' : 'alice@feishu.cn',
    granted_scopes:
      provider === 'google'
        ? [
            'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
            'https://www.googleapis.com/auth/calendar.events',
          ]
        : ['offline_access', 'calendar:calendar:readonly'],
    status: 'connected',
    auth_epoch: 1,
    authorized_at: '2026-11-27T10:00:00Z',
    expires_at: '2026-11-27T11:00:00Z',
    last_refresh_at: '2026-11-27T10:30:00Z',
    needs_reauth_at: null,
    last_error_code: null,
    client_rotated: false,
    preferences: {},
    updated_at: '2026-11-27T10:30:00Z',
    ...overrides,
  };
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PersonalIntegrationsCard />
    </QueryClientProvider>,
  );
}

describe('PersonalIntegrationsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.profile = 'everflow';
    h.capabilities = new Set<CapabilityId>(['google_calendar', 'feishu_calendar']);
    window.history.replaceState(null, '', '/settings?tab=integrations');
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/settings?tab=integrations');
  });

  it('renders health statuses, missing-scope reauth hint, and retryable remote revoke button', async () => {
    const purge: PersonalIntegrationPurgeLedger = {
      purge_id: 'purge-1',
      provider: 'google',
      service: 'calendar',
      provider_account_id: 'old-sub',
      trigger_reason: 'user_disconnect',
      local_purge_status: 'completed',
      remote_revoke_status: 'failed_retryable',
      retry_count: 1,
      next_retry_at: null,
      last_remote_error: '503',
      disconnected_at: '2026-11-27T10:00:00Z',
      local_completed_at: '2026-11-27T10:01:00Z',
      remote_completed_at: null,
    };
    h.fetchList.mockResolvedValue({
      connectors: [
        makeConnector('google', 'calendar'),
        makeConnector('feishu', 'calendar', { credentials_ready: false, ready: false }),
      ],
      connections: [
        makeConnection('google', 'calendar', {
          status: 'needs_reauth',
          last_error_code: 'missing_required_scopes',
        }),
      ],
      active_purges: [purge],
      has_personal_integrations: true,
    });
    h.retryRevoke.mockResolvedValue({ purge: { ...purge, remote_revoke_status: 'succeeded' } });

    renderCard();

    expect(await screen.findByText('Google 日历')).toBeInTheDocument();
    expect(screen.getByText('需重新授权')).toBeInTheDocument();
    expect(screen.getByText(/勾选「日历事件」权限/)).toBeInTheDocument();
    expect(screen.getByText('待配置凭据')).toBeInTheDocument();
    expect(screen.getByText('隐私权政策')).toHaveAttribute('href', '/help/everflow/privacy/');

    fireEvent.click(screen.getByRole('button', { name: /重试远端撤销/ }));
    await waitFor(() => expect(h.retryRevoke).toHaveBeenCalledWith('purge-1'));
  });

  it('handles pre-redirect and post-callback 409 account switch confirmation dialog', async () => {
    window.history.replaceState(
      null,
      '',
      '/settings?tab=integrations&integration_status=account_switch_confirmation_required&provider=google&service=calendar#confirmation_token=ctok-99',
    );
    h.fetchList.mockResolvedValue({
      connectors: [makeConnector('google', 'calendar')],
      connections: [makeConnection('google', 'calendar')],
      active_purges: [],
      has_personal_integrations: true,
    });
    h.confirm
      .mockRejectedValueOnce(
        new ApiError('conflict', 'ERR_BAD_REQUEST', {
          status: 409,
          statusText: 'Conflict',
          headers: {},
          data: {
            code: 'account_switch_confirmation_required',
            provider: 'google',
            service: 'calendar',
            previous_account_email: 'old@gmail.com',
            new_account_email: 'new@gmail.com',
          },
        }),
      )
      .mockResolvedValueOnce({
        status: 'connected',
        provider: 'google',
        service: 'calendar',
        connection: makeConnection('google', 'calendar', { provider_email: 'new@gmail.com' }),
      });

    renderCard();

    // Dialog opens with old and new account emails, and fragment token was scrubbed from URL
    expect(await screen.findByTestId('confirm-account-switch-dialog')).toBeInTheDocument();
    expect(screen.getByText(/old@gmail.com/)).toBeInTheDocument();
    expect(screen.getByText(/new@gmail.com/)).toBeInTheDocument();
    expect(window.location.hash).not.toContain('ctok-99');

    fireEvent.click(screen.getByRole('button', { name: '确认换账号' }));
    await waitFor(() =>
      expect(h.confirm).toHaveBeenLastCalledWith({
        confirmation_token: 'ctok-99',
        confirm_account_switch: true,
        cancel: false,
      }),
    );
  });

  it('shows disconnect options for historical connections after capability is disabled in Core', async () => {
    h.profile = 'core';
    h.capabilities = new Set<CapabilityId>(['company_wiki']);
    h.fetchList.mockResolvedValue({
      connectors: [makeConnector('google', 'calendar', { enabled: false })],
      connections: [makeConnection('google', 'calendar')],
      active_purges: [],
      has_personal_integrations: true,
    });
    h.disconnectService.mockResolvedValue({
      local_disconnected: true,
      local_tokens_destroyed: true,
      provider: 'google',
      service: 'calendar',
    });

    renderCard();

    expect(await screen.findByText('历史连接')).toBeInTheDocument();
    // Core profile hides Everflow privacy policy link when VITE_PRIVACY_POLICY_URL is unset
    expect(screen.queryByText('隐私权政策')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '断开连接' }));
    expect(await screen.findByTestId('disconnect-integration-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /断开并在 Google 撤销授权/ }));
    await waitFor(() =>
      expect(h.disconnectService).toHaveBeenCalledWith('google', 'calendar', 'remote_if_sole'),
    );
  });

  it('shows Gmail partial grant badge and pre-switch warning modal', async () => {
    h.capabilities = new Set<CapabilityId>(['google_calendar', 'google_gmail']);
    h.fetchList.mockResolvedValue({
      connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
      connections: [
        makeConnection('google', 'calendar'),
        makeConnection('google', 'mail', {
          granted_scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
        }),
      ],
      active_purges: [],
      has_personal_integrations: true,
    });
    h.authorize.mockResolvedValue({
      authorize_url: '',
      provider: 'google',
      service: 'calendar',
      intent: 'switch_account',
      expires_in: 600,
    });

    renderCard();

    expect(await screen.findByText('只读授权')).toBeInTheDocument();
    const switchBtns = screen.getAllByRole('button', { name: '换账号' });
    fireEvent.click(switchBtns[0]);
    expect(await screen.findByTestId('pre-account-switch-dialog')).toBeInTheDocument();
    expect(screen.getByText(/换账号会清除旧账号在对话中留下的个人数据/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '继续选择新账号' }));
    await waitFor(() =>
      expect(h.authorize).toHaveBeenCalledWith('google', 'calendar', {
        switch_account: true,
        return_to: '/settings?tab=integrations',
      }),
    );
  });
});
