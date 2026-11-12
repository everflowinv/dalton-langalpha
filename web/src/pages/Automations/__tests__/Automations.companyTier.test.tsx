// DALTON-HOOK(company-automations): a failed credentials read hides the company group.
// This file runs the real useTeamCredentials hook. isDaltonAuth is true only here;
// the other automation tests stay on the OSS path, where an unset tier still requests company.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import type { QueryClient } from '@tanstack/react-query';
import i18n from '@/i18n';
import { queryKeys } from '@/lib/queryKeys';
import { renderWithProviders } from '@/test/utils';
import type { AgentAutomationRead } from '@/types/automation';
import * as api from '../utils/api';
import Automations from '../Automations';

const STATUS_URL = '/api/v1/dalton/credentials/status';
const TIER_WAIT_MS = 8_000;

const auth = vi.hoisted(() => ({ userId: 'user-1' }));
const home = vi.hoisted(() => ({ getHomeWorkspace: vi.fn() }));
const daltonAuthState = vi.hoisted(() => ({ isDaltonAuth: true }));
const statusApi = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useHomeTimezone', () => ({ useHomeTimezone: () => 'America/New_York' }));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] }, isPending: false, isLoading: false }),
}));
vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));
vi.mock('@/pages/ChatAgent/utils/api/workspaces', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHomeWorkspace: home.getHomeWorkspace,
}));
vi.mock('@/config/daltonAuth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config/daltonAuth')>();
  return {
    ...actual,
    get isDaltonAuth() {
      return daltonAuthState.isDaltonAuth;
    },
  };
});
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: ((url: string, config?: unknown) => statusApi.get(url, config)) as unknown as typeof actual.api.get,
    },
  };
});
vi.mock('@/contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ userId: auth.userId, isLoggedIn: true, isInitialized: true }),
  };
});
vi.mock('../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/api')>();
  return {
    ...actual,
    listAutomations: vi.fn(),
    listExecutions: vi.fn(),
    listRecentRuns: vi.fn(),
    listAutomationReads: vi.fn(),
    listAutomationExecutionReads: vi.fn(),
    getAutomationRead: vi.fn(),
    getDeliveryOptions: vi.fn(),
    listAutomationHistory: vi.fn(),
  };
});

const label = (key: string) => i18n.t(key);
const tierKey = () => queryKeys.dalton.credentialsStatus('user-1');

function httpError(status: number, detail: string): Error {
  const error = new Error(detail);
  return Object.assign(error, { response: { status, data: { detail } } });
}

function emptyPage() {
  return { data: { automations: [], total: 0 } } as never;
}

function agent(over: Partial<AgentAutomationRead> & Pick<AgentAutomationRead, 'automation_id' | 'name' | 'scope'>): AgentAutomationRead {
  return {
    user_id: 'user-1',
    description: null,
    trigger_type: 'cron',
    cron_expression: '0 9 * * 1-5',
    timezone: 'America/New_York',
    trigger_config: null,
    next_run_at: null,
    last_run_at: null,
    instruction: 'Summarize the desk.',
    workspace_id: 'home-1',
    llm_model: null,
    additional_context: null,
    thread_strategy: 'new',
    conversation_thread_id: null,
    status: 'active',
    max_failures: 3,
    failure_count: 0,
    delivery_config: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-02T00:00:00Z',
    last_execution: null,
    engine_kind: 'agent',
    notify_storage_mode: 'default',
    config_version: 2,
    active_version: 2,
    created_by: 'user-1',
    updated_by: null,
    ...over,
  };
}

function credentialsBody(tier: 'external' | 'internal') {
  return {
    data: {
      sharing: 'off' as const,
      enabled: false,
      holder_user_id: null,
      credential_user_id: 'user-1',
      is_pm: false,
      can_disconnect: false,
      can_invite: false,
      caller_tier: tier,
    },
  };
}

function rejectStatus(url: string) {
  if (url !== STATUS_URL) throw new Error(`unexpected GET ${url}`);
  throw new Error('credentials status failed');
}

function companyReads(includeDeleted: boolean) {
  return vi.mocked(api.listAutomationReads).mock.calls
    .map(([params]) => params)
    .filter((params) => {
      if (!params || params.scope !== 'company') return false;
      return Boolean(params.includeDeleted) === includeDeleted;
    });
}

function expectNoCompanyReads() {
  expect(companyReads(false)).toEqual([]);
  expect(companyReads(true)).toEqual([]);
}

/** The credentials query retries once. Wait until that settles, then let the company effect run. */
async function settledTier(queryClient: QueryClient, status: 'error' | 'success') {
  await waitFor(() => {
    expect(queryClient.getQueryState(tierKey())?.status).toBe(status);
  }, { timeout: TIER_WAIT_MS });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

beforeEach(() => {
  auth.userId = 'user-1';
  daltonAuthState.isDaltonAuth = true;
  localStorage.clear();
  home.getHomeWorkspace.mockResolvedValue({ workspace_id: 'home-1', is_home: true });
  statusApi.get.mockReset();
  statusApi.get.mockImplementation(rejectStatus);
  vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [], total: 0 } } as never);
  vi.mocked(api.listExecutions).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationExecutionReads).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
    if (params?.includeDeleted) return emptyPage();
    if (params?.scope === 'personal') {
      return {
        data: { automations: [agent({ automation_id: 'me-1', name: 'Morning note', scope: 'personal' })], total: 1 },
      } as never;
    }
    if (params?.scope === 'company') {
      return {
        data: { automations: [agent({ automation_id: 'co-1', name: 'Desk brief', scope: 'company' })], total: 1 },
      } as never;
    }
    return emptyPage();
  });
  vi.mocked(api.getAutomationRead).mockRejectedValue(httpError(404, 'missing'));
  vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: null } } as never);
  vi.mocked(api.listAutomationHistory).mockResolvedValue({
    data: {
      automation_id: 'none',
      scope: 'personal',
      engine_kind: 'agent',
      config_version: 1,
      active_version: 1,
      entries: [],
      total: 0,
      limit: 50,
      offset: 0,
    },
  } as never);
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('company group when Dalton credentials/status fails', () => {
  it('hides the company group and skips its list and recycle bin', async () => {
    const { queryClient } = renderWithProviders(<Automations />, { route: '/automations?view=manage' });

    expect(await screen.findByRole('button', { name: /Morning note/ }, { timeout: TIER_WAIT_MS })).toBeInTheDocument();
    await settledTier(queryClient, 'error');

    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: label('automation.groupMine') })).toBeInTheDocument();
    expectNoCompanyReads();
    expect(api.listAutomationReads).toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'personal', includeDeleted: true, limit: 50, offset: 0 }),
    );
  }, 15_000);

  it('keeps skipping company reads after a cached external caller fails a refetch', async () => {
    statusApi.get.mockImplementation(async (url: string) => {
      if (url !== STATUS_URL) throw new Error(`unexpected GET ${url}`);
      return credentialsBody('external');
    });
    const { queryClient } = renderWithProviders(<Automations />, { route: '/automations?view=manage' });

    expect(await screen.findByRole('button', { name: /Morning note/ })).toBeInTheDocument();
    await settledTier(queryClient, 'success');
    expect(queryClient.getQueryData(tierKey())).toMatchObject({ caller_tier: 'external' });
    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
    expectNoCompanyReads();

    statusApi.get.mockImplementation(rejectStatus);
    await queryClient.invalidateQueries({ queryKey: tierKey() });
    await settledTier(queryClient, 'error');

    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Morning note/ })).toBeInTheDocument();
    expectNoCompanyReads();
  }, 15_000);

  it('requests the company list and recycle bin once the read says internal', async () => {
    statusApi.get.mockImplementation(async (url: string) => {
      if (url !== STATUS_URL) throw new Error(`unexpected GET ${url}`);
      return credentialsBody('internal');
    });
    const { queryClient } = renderWithProviders(<Automations />, { route: '/automations?view=manage' });

    expect(await screen.findByRole('region', { name: label('automation.groupCompany') }, { timeout: TIER_WAIT_MS })).toBeInTheDocument();
    await settledTier(queryClient, 'success');
    expect(screen.getByRole('button', { name: /Desk brief/ })).toBeInTheDocument();
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'company', limit: 50, offset: 0 });
    expect(api.listAutomationReads).toHaveBeenCalledWith({
      scope: 'company',
      limit: 50,
      offset: 0,
      includeDeleted: true,
    });
  }, 15_000);
});
