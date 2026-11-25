import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonSystemJobsCard } from '../DaltonSystemJobsCard';
import type {
  DaltonSystemJobItem,
  DaltonSystemJobRunResponse,
  DaltonSystemJobsResponse,
  DaltonSystemJobToggleResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  callerTier: 'internal' as string | null,
  jobsResponse: {
    ok: true,
    jobs: [],
  } as DaltonSystemJobsResponse,
  toggleJob: vi.fn<
    (key: string, enabled: boolean) => Promise<DaltonSystemJobToggleResponse>
  >(),
  runJob: vi.fn<(key: string) => Promise<DaltonSystemJobRunResponse>>(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: 'team',
    enabled: true,
    holder_user_id: null,
    credential_user_id: 'u-1',
    is_pm: h.callerTier === 'admin',
    can_disconnect: h.callerTier === 'admin',
    can_invite: h.callerTier === 'admin' || h.callerTier === 'internal',
    caller_tier: h.callerTier,
    colleague_sharing_enabled: true,
    auth_mode: 'local',
  })),
  useTeamCredentials: () => ({
    callerTier: h.callerTier,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const tier = h.callerTier ?? 'unknown';
    const ready = tier === 'admin' || tier === 'internal' || tier === 'external';
    const snapshot = {
      tier: ready ? tier : 'unknown',
      status: ready ? 'ready' : 'error',
      authMode: ready ? 'local' : null,
      isPlatform: false,
      isKnown: ready,
      isConfirming: false,
      isMultiUserAuth: ready,
      isSingleUserOss: false,
      isAdmin: ready && tier === 'admin',
      isInternal: ready && (tier === 'admin' || tier === 'internal'),
      isExternal: ready && tier === 'external',
      canActAsOwner: ready && (tier === 'admin' || tier === 'internal'),
      canPublishPublic: ready && tier === 'admin',
      canInvite: ready && (tier === 'admin' || tier === 'internal'),
      isTeamCredentialsShared: true,
      canManageCredentials: ready && tier === 'admin',
      isPm: ready && tier === 'admin',
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

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonSystemJobs: async () => h.jobsResponse,
  toggleDaltonSystemJob: (key: string, enabled: boolean) => h.toggleJob(key, enabled),
  runDaltonSystemJobNow: (key: string) => h.runJob(key),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeJob(overrides: Partial<DaltonSystemJobItem>): DaltonSystemJobItem {
  return {
    key: 'job-1',
    kind: 'sync',
    scope: 'company',
    title: 'Company Filings Sync',
    status: 'on',
    enabled: true,
    last_run_at: '2026-11-16T08:00:00Z',
    last_result: 'ok',
    next_run_at: null,
    can_toggle: true,
    can_run_now: true,
    ...overrides,
  };
}

function renderCard(scopes?: string[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DaltonSystemJobsCard scopes={scopes} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DaltonSystemJobsCard', () => {
  beforeEach(() => {
    localStorage.removeItem('settings:modelMode');
    h.callerTier = 'internal';
    h.toggleJob.mockReset().mockResolvedValue({ ok: true });
    h.runJob.mockReset().mockResolvedValue({
      ok: true,
      key: 'job-1',
      scope: 'company',
      async: true,
      status: 'running',
    });
    h.jobsResponse = {
      ok: true,
      jobs: [
        makeJob({ key: 'data-1', scope: 'data', title: 'Public Data Sync', can_toggle: true, can_run_now: true }),
        makeJob({ key: 'company-1', scope: 'company', title: 'Company Track', can_toggle: false, can_run_now: false }),
        makeJob({ key: 'sector-1', scope: 'sector', title: 'Sector Track', log_tail: 'ok line 1', can_toggle: false, can_run_now: false }),
        makeJob({ key: 'ops-1', scope: 'ops', title: 'Bridge Maintenance', can_toggle: true, can_run_now: true }),
        makeJob({ key: 'host-1', scope: 'host', title: 'Host Disk GC', can_toggle: true, can_run_now: true }),
      ],
    };
  });

  it('strictly hides host and ops jobs from non-admin members while honoring backend capabilities on public/data scopes', async () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    h.callerTier = 'internal';
    renderCard();

    expect(await screen.findByText('Public Data Sync')).toBeInTheDocument();
    expect(screen.getByText('Company Track')).toBeInTheDocument();
    expect(screen.getByText('Sector Track')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-system-jobs-ops-section')).not.toBeInTheDocument();
    expect(screen.queryByText('Bridge Maintenance')).not.toBeInTheDocument();
    expect(screen.queryByText('Host Disk GC')).not.toBeInTheDocument();

    // Read-only card: running or pausing jobs lives on Automations, never here.
    expect(screen.queryByTestId('dalton-system-job-run-data-1')).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('shows the host and ops section to admins, still without run or toggle controls', async () => {
    h.callerTier = 'admin';
    renderCard();

    expect(await screen.findByTestId('dalton-system-jobs-ops-section')).toBeInTheDocument();
    expect(screen.getByText('Bridge Maintenance')).toBeInTheDocument();
    expect(screen.getByText('Host Disk GC')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-system-job-run-ops-1')).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('supports scopes prop filter and expands log_tail disclosure cleanly', async () => {
    h.callerTier = 'admin';
    renderCard(['sector']);

    expect(await screen.findByText('Sector Track')).toBeInTheDocument();
    expect(screen.queryByText('Company Track')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'dalton.systemJobs.showLogs' }));
    expect(await screen.findByText('ok line 1')).toBeInTheDocument();
  });
});
