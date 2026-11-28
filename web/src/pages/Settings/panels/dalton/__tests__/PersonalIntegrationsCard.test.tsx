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
} from '@/lib/dalton/personalIntegrationsApi';
import { PersonalIntegrationsCard } from '../PersonalIntegrationsCard';

const h = vi.hoisted(() => ({
  profile: 'everflow' as 'everflow' | 'core',
  capabilities: new Set<CapabilityId>(['google_calendar', 'feishu_calendar']),
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
  apiPatch: vi.fn(),
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

// Mock strictly at the HTTP client boundary so real personalIntegrationsApi functions,
// real usePersonalIntegrationsQuery React Query hook, rendering, and disconnect flows all run together.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: unknown[]) => h.apiGet(...args),
      post: (...args: unknown[]) => h.apiPost(...args),
      delete: (...args: unknown[]) => h.apiDelete(...args),
      patch: (...args: unknown[]) => h.apiPatch(...args),
    },
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
    h.apiGet.mockResolvedValue({
      data: {
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
      },
    });
    h.apiPost.mockResolvedValue({
      data: { purge: { ...purge, remote_revoke_status: 'succeeded' } },
    });

    renderCard();

    expect(await screen.findByText('Google 日历')).toBeInTheDocument();
    expect(screen.getByText('需重新授权')).toBeInTheDocument();
    expect(screen.getByText(/勾选「日历事件」权限/)).toBeInTheDocument();
    expect(screen.getByText('待配置凭据')).toBeInTheDocument();
    expect(screen.getByText('隐私权政策')).toHaveAttribute('href', '/help/everflow/privacy/');

    fireEvent.click(screen.getByRole('button', { name: /重试远端撤销/ }));
    await waitFor(() =>
      expect(h.apiPost).toHaveBeenCalledWith(
        '/api/v1/dalton/integrations/purges/purge-1/retry-revoke',
      ),
    );
  });

  it('handles pre-redirect and post-callback 409 account switch confirmation dialog', async () => {
    window.history.replaceState(
      null,
      '',
      '/settings?tab=integrations&integration_status=account_switch_confirmation_required&provider=google&service=calendar#confirmation_token=ctok-99',
    );
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('google', 'calendar')],
        connections: [makeConnection('google', 'calendar')],
        active_purges: [],
        has_personal_integrations: true,
      },
    });
    h.apiPost
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
        data: {
          status: 'connected',
          provider: 'google',
          service: 'calendar',
          connection: makeConnection('google', 'calendar', { provider_email: 'new@gmail.com' }),
        },
      });

    renderCard();

    expect(await screen.findByTestId('confirm-account-switch-dialog')).toBeInTheDocument();
    expect(screen.getByText(/old@gmail.com/)).toBeInTheDocument();
    expect(screen.getByText(/new@gmail.com/)).toBeInTheDocument();
    expect(window.location.hash).not.toContain('ctok-99');

    fireEvent.click(screen.getByRole('button', { name: '确认换账号' }));
    await waitFor(() =>
      expect(h.apiPost).toHaveBeenLastCalledWith('/api/v1/dalton/integrations/confirm', {
        confirmation_token: 'ctok-99',
        confirm_account_switch: true,
        cancel: false,
      }),
    );
  });

  it('shows disconnect options for historical connections after capability is disabled in Core', async () => {
    h.profile = 'core';
    h.capabilities = new Set<CapabilityId>(['company_wiki']);
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('google', 'calendar', { enabled: false, ready: false })],
        connections: [makeConnection('google', 'calendar')],
        active_purges: [],
        has_personal_integrations: true,
      },
    });
    h.apiDelete.mockResolvedValue({
      data: {
        local_disconnected: true,
        local_tokens_destroyed: true,
        local_purge_status: 'completed',
        provider: 'google',
        service: 'calendar',
      },
    });

    renderCard();

    expect(await screen.findByText('历史连接')).toBeInTheDocument();
    expect(screen.queryByText('隐私权政策')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '断开连接' }));
    expect(await screen.findByTestId('disconnect-integration-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /断开并在 Google 撤销授权/ }));
    await waitFor(() =>
      expect(h.apiDelete).toHaveBeenCalledWith(
        '/api/v1/dalton/integrations/google/calendar?mode=remote_if_sole',
      ),
    );
    expect(await screen.findByTestId('personal-integrations-banner')).toHaveTextContent(
      '关联个人数据已清理完成',
    );
  });

  it('shows Gmail partial grant badge and pre-switch warning modal', async () => {
    h.capabilities = new Set<CapabilityId>(['google_calendar', 'google_gmail']);
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
        connections: [
          makeConnection('google', 'calendar'),
          makeConnection('google', 'mail', {
            granted_scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
          }),
        ],
        active_purges: [],
        has_personal_integrations: true,
      },
    });
    h.apiPost.mockResolvedValue({
      data: {
        authorize_url: '',
        provider: 'google',
        service: 'calendar',
        intent: 'switch_account',
        expires_in: 600,
      },
    });

    renderCard();

    expect(await screen.findByText('只读授权')).toBeInTheDocument();
    const switchBtns = screen.getAllByRole('button', { name: '换账号' });
    fireEvent.click(switchBtns[0]);
    expect(await screen.findByTestId('pre-account-switch-dialog')).toBeInTheDocument();
    expect(screen.getByText(/换账号会清除旧账号在对话中留下的个人数据/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '继续选择新账号' }));
    await waitFor(() =>
      expect(h.apiPost).toHaveBeenCalledWith('/api/v1/dalton/integrations/google/calendar/authorize', {
        switch_account: true,
        return_to: '/settings?tab=integrations',
      }),
    );
  });

  it('disables connect button using backend ready (including callback/encryption readiness)', async () => {
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [
          makeConnector('google', 'calendar', { credentials_ready: true, ready: false }),
          makeConnector('feishu', 'calendar', { credentials_ready: true, ready: true }),
        ],
        connections: [],
        active_purges: [],
        has_personal_integrations: false,
      },
    });

    renderCard();

    const googleRow = await screen.findByTestId('personal-integration-row-google-calendar');
    expect(googleRow).toHaveTextContent('待完成环境配置');
    expect(googleRow).toHaveTextContent('公开回调地址与服务端加密配置');
    const googleConnectBtn = googleRow.querySelector('button');
    expect(googleConnectBtn).toBeDisabled();

    const feishuRow = screen.getByTestId('personal-integration-row-feishu-calendar');
    const feishuConnectBtn = feishuRow.querySelector('button');
    expect(feishuConnectBtn).not.toBeDisabled();
  });

  it('reports running purge progress accurately when Feishu disconnect returns local_purge_status=running', async () => {
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('feishu', 'calendar')],
        connections: [makeConnection('feishu', 'calendar')],
        active_purges: [],
        has_personal_integrations: true,
      },
    });
    h.apiDelete.mockResolvedValue({
      data: {
        local_disconnected: true,
        local_tokens_destroyed: true,
        local_purge_status: 'running',
        provider: 'feishu',
        service: 'calendar',
      },
    });

    renderCard();

    expect(await screen.findByText('飞书日历')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '断开连接' }));
    expect(await screen.findByTestId('disconnect-integration-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认断开' }));

    const banner = await screen.findByTestId('personal-integrations-banner');
    expect(banner).toHaveTextContent('正在后台清理相关对话个人数据');
    expect(banner).not.toHaveTextContent('已清理完成');
  });

  it('uses purge.provider label (飞书 vs Google) for purge progress and polls while purge is active', async () => {
    const feishuPurge: PersonalIntegrationPurgeLedger = {
      purge_id: 'purge-feishu-1',
      provider: 'feishu',
      service: 'calendar',
      provider_account_id: 'ou_feishu_1',
      trigger_reason: 'user_disconnect',
      local_purge_status: 'running',
      remote_revoke_status: 'not_applicable',
      retry_count: 0,
      next_retry_at: null,
      last_remote_error: null,
      disconnected_at: '2026-11-27T10:00:00Z',
      local_completed_at: null,
      remote_completed_at: null,
    };
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('feishu', 'calendar')],
        connections: [],
        active_purges: [feishuPurge],
        has_personal_integrations: true,
      },
    });

    renderCard();

    const progress = await screen.findByTestId('personal-integration-purge-purge-feishu-1');
    expect(progress).toHaveTextContent('飞书 · 日历 — 正在清理本地飞书对话个人数据');
    expect(progress).not.toHaveTextContent('Google');
  });

  it('disconnectAllGoogleMutation reflects backend purges running/retryable status instead of hardcoding completed', async () => {
    h.capabilities = new Set<CapabilityId>(['google_calendar', 'google_gmail']);
    h.apiGet.mockResolvedValue({
      data: {
        connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
        connections: [makeConnection('google', 'calendar'), makeConnection('google', 'mail')],
        active_purges: [],
        has_personal_integrations: true,
      },
    });
    h.apiDelete.mockResolvedValue({
      data: {
        provider: 'google',
        services: ['calendar', 'mail'],
        purges: [
          {
            purge_id: 'p-1',
            provider: 'google',
            service: 'calendar',
            local_purge_status: 'running',
            remote_revoke_status: 'failed_retryable',
          },
          {
            purge_id: 'p-2',
            provider: 'google',
            service: 'mail',
            local_purge_status: 'completed',
            remote_revoke_status: 'succeeded',
          },
        ],
      },
    });

    renderCard();

    expect(await screen.findByText('Google 日历')).toBeInTheDocument();
    const disconnectBtns = screen.getAllByRole('button', { name: '断开连接' });
    fireEvent.click(disconnectBtns[0]);
    expect(await screen.findByTestId('disconnect-integration-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /断开全部 Google 服务并在 Google 撤销授权/ }));

    await waitFor(() =>
      expect(h.apiDelete).toHaveBeenCalledWith(
        '/api/v1/dalton/integrations/google?mode=remote_if_sole',
      ),
    );
    const banner = await screen.findByTestId('personal-integrations-banner');
    expect(banner).toHaveTextContent('正在后台清理关联本地个人数据');
    expect(banner).not.toHaveTextContent('已清理完成');
  });

  it('auto-refreshes open Settings page while active purge is running and stops once completed', async () => {
    const runningPurge: PersonalIntegrationPurgeLedger = {
      purge_id: 'purge-poll-1',
      provider: 'feishu',
      service: 'calendar',
      provider_account_id: 'ou_feishu_2',
      trigger_reason: 'user_disconnect',
      local_purge_status: 'running',
      remote_revoke_status: 'not_applicable',
      retry_count: 0,
      next_retry_at: null,
      last_remote_error: null,
      disconnected_at: '2026-11-27T10:00:00Z',
      local_completed_at: null,
      remote_completed_at: null,
    };

    const { hasPendingPersonalIntegrationWork } = await import(
      '@/lib/dalton/personalIntegrationsApi'
    );
    expect(
      hasPendingPersonalIntegrationWork({
        connectors: [],
        connections: [],
        active_purges: [runningPurge],
        has_personal_integrations: true,
      }),
    ).toBe(true);
    expect(
      hasPendingPersonalIntegrationWork({
        connectors: [],
        connections: [],
        active_purges: [{ ...runningPurge, local_purge_status: 'completed' }],
        has_personal_integrations: true,
      }),
    ).toBe(false);
    expect(
      hasPendingPersonalIntegrationWork({
        connectors: [],
        connections: [makeConnection('google', 'calendar', { status: 'revoked_purging' })],
        active_purges: [],
        has_personal_integrations: true,
      }),
    ).toBe(true);
  });

  it('updates top banner automatically from running/revoking to completed as polled purge records finish', async () => {
    h.capabilities = new Set<CapabilityId>(['google_calendar', 'google_gmail']);
    const runningPurge: PersonalIntegrationPurgeLedger = {
      purge_id: 'p-all-1',
      provider: 'google',
      service: 'calendar',
      provider_account_id: 'acct-google',
      trigger_reason: 'user_disconnect',
      local_purge_status: 'running',
      remote_revoke_status: 'pending',
      retry_count: 0,
      next_retry_at: null,
      last_remote_error: null,
      disconnected_at: '2026-11-27T10:00:00Z',
      local_completed_at: null,
      remote_completed_at: null,
    };
    const revokingPurge: PersonalIntegrationPurgeLedger = {
      ...runningPurge,
      local_purge_status: 'completed',
      remote_revoke_status: 'pending',
      local_completed_at: '2026-11-27T10:00:05Z',
    };

    h.apiGet
      // Initial mount
      .mockResolvedValueOnce({
        data: {
          connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
          connections: [makeConnection('google', 'calendar'), makeConnection('google', 'mail')],
          active_purges: [],
          has_personal_integrations: true,
        },
      })
      // Post-disconnect refetch #1: local purge still running
      .mockResolvedValueOnce({
        data: {
          connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
          connections: [],
          active_purges: [runningPurge],
          has_personal_integrations: true,
        },
      })
      // Refetch #2: local purge completed, remote revoke pending
      .mockResolvedValueOnce({
        data: {
          connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
          connections: [],
          active_purges: [revokingPurge],
          has_personal_integrations: true,
        },
      })
      // Refetch #3: all purges and remote revokes finished
      .mockResolvedValueOnce({
        data: {
          connectors: [makeConnector('google', 'calendar'), makeConnector('google', 'mail')],
          connections: [],
          active_purges: [],
          has_personal_integrations: false,
        },
      });

    h.apiDelete.mockResolvedValueOnce({
      data: {
        provider: 'google',
        services: ['calendar', 'mail'],
        purges: [
          {
            purge_id: 'p-all-1',
            provider: 'google',
            service: 'calendar',
            local_purge_status: 'running',
            remote_revoke_status: 'pending',
          },
        ],
      },
    });

    renderCard();

    expect(await screen.findByText('Google 日历')).toBeInTheDocument();
    const disconnectBtns = screen.getAllByRole('button', { name: '断开连接' });
    fireEvent.click(disconnectBtns[0]);
    expect(await screen.findByTestId('disconnect-integration-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /断开全部 Google 服务并在 Google 撤销授权/ }));

    await waitFor(() =>
      expect(screen.getByTestId('personal-integrations-banner')).toHaveTextContent(
        '正在后台清理关联本地个人数据',
      ),
    );
    expect(await screen.findByTestId('personal-integration-purge-p-all-1')).toBeInTheDocument();

    // Trigger next poll/refresh: local purge done, remote revoke pending
    fireEvent.click(screen.getByRole('button', { name: '刷新连接状态' }));
    await waitFor(() =>
      expect(screen.getByTestId('personal-integrations-banner')).toHaveTextContent(
        '关联本地个人数据已清理完成，正在后台撤销 Google 授权',
      ),
    );

    // Trigger next poll/refresh: all background purge & revoke done -> banner flips to completed!
    fireEvent.click(screen.getByRole('button', { name: '刷新连接状态' }));
    await waitFor(() =>
      expect(screen.getByTestId('personal-integrations-banner')).toHaveTextContent(
        '已断开全部 Google 服务，关联本地个人数据已清理完成，Google 授权已撤销',
      ),
    );
    expect(screen.getByTestId('personal-integrations-banner')).not.toHaveTextContent('正在后台');
  });
});
