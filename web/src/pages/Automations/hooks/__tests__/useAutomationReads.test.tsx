// DALTON-HOOK(company-automations): paged reads for agent and script tasks.
import { type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { apiErrorStatus } from '@/api/errors';
import { queryKeys } from '@/lib/queryKeys';
import { createTestQueryClient, renderHookWithProviders } from '@/test/utils';
import type {
  AutomationHistory,
  AutomationRead,
  AutomationReadList,
  AutomationReadListQuery,
  AutomationRunRead,
  AutomationRunReadList,
  AutomationScope,
} from '@/types/automation';
import type { AutomationReadFetchResult, AutomationReadState } from '../useAutomationReads';
import {
  useAutomationExecutionReads,
  useAutomationHistory,
  useAutomationRead,
  useAutomationReadList,
  useAutomationRunReads,
} from '../useAutomationReads';
import * as api from '../../utils/api';

vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  listAutomationReads: vi.fn(),
  listAutomationRunReads: vi.fn(),
  getAutomationRead: vi.fn(),
  listAutomationExecutionReads: vi.fn(),
  listAutomationHistory: vi.fn(),
}));

const auth = vi.hoisted(() => ({ userId: 'user-1' as string | null }));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    userId: auth.userId,
    isLoggedIn: auth.userId != null,
    isInitialized: true,
  }),
}));

const USER = 'user-1';
const SCRIPT_ID = 'data:daily-prices';
const AGENT_ID = '550e8400-e29b-41d4-a716-446655440000';
const THREAD_A = '11111111-1111-4111-8111-111111111111';
const THREAD_B = '22222222-2222-4222-8222-222222222222';

function ok<T>(data: T) {
  return { data, status: 200, statusText: 'OK', headers: {} };
}

function httpError(status: number): ApiError {
  const code = status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST';
  return new ApiError(`Request failed with status code ${status}`, code, {
    status,
    statusText: '',
    headers: {},
    data: { detail: 'refused' },
  });
}

function retainingClient() {
  const client = createTestQueryClient();
  client.setDefaultOptions({
    queries: {
      retry: false,
      gcTime: Infinity,
      staleTime: Infinity,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
    },
  });
  return client;
}

/** Props come from `rerender`, so a filter change is a new input. Mutating one
 *  object and rendering again keeps the first value under the React Compiler. */
function renderRead<TProps, TResult>(
  hook: (props: TProps) => TResult,
  initialProps: TProps,
  queryClient: QueryClient = createTestQueryClient(),
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { ...renderHook(hook, { wrapper, initialProps }), queryClient };
}

function listKey(
  scope: AutomationScope | null,
  extra: Partial<AutomationReadListQuery> = {},
) {
  return queryKeys.automations.readList(USER, {
    scope,
    status: null,
    limit: 50,
    offset: 0,
    ...extra,
  });
}

function listPage(total: number): AutomationReadList {
  return { automations: [], total };
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
}

/** The resolver is a property. A local assigned inside the executor stays at
 *  its initial type under control-flow analysis, so the call would see `never`. */
function deferred(): Deferred {
  const gate: { resolve: (value: void | PromiseLike<void>) => void } = {
    resolve: () => {},
  };
  const promise = new Promise<void>((resolve) => {
    gate.resolve = resolve;
  });
  return {
    promise,
    resolve: () => {
      gate.resolve(undefined);
    },
  };
}

/** The promise `refetch` resolves to, including a 403 whose page was removed. */
async function readRefetch<T>(
  refetch: AutomationReadState<T>['refetch'],
): Promise<AutomationReadFetchResult<T>> {
  let settled: AutomationReadFetchResult<T> | undefined;
  await act(async () => {
    settled = await refetch();
  });
  if (!settled) throw new Error('refetch did not settle');
  return settled;
}

beforeEach(() => {
  auth.userId = USER;
  vi.resetAllMocks();
});

describe('useAutomationReads pages', () => {
  it('reads the second list page and keeps the server total', async () => {
    const page = {
      automations: [{ engine_kind: 'script', automation_id: SCRIPT_ID, name: 'Daily prices' } as AutomationRead],
      total: 80,
    };
    vi.mocked(api.listAutomationReads).mockResolvedValue(ok(page));
    const { result, queryClient } = renderHookWithProviders(() =>
      useAutomationReadList({ scope: 'company', limit: 50, offset: 50 }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'company', limit: 50, offset: 50 });
    expect(result.current.data?.total).toBe(80);
    expect(result.current.data?.automations).toHaveLength(1);
    expect(result.current.data?.total).not.toBe(result.current.data?.automations.length);
    expect(queryClient.getQueryData(listKey('company', { offset: 50 }))).toEqual(page);
    expect(queryClient.getQueryData(queryKeys.automations.list({ limit: 100, offset: 0 }))).toBeUndefined();
  });

  it('reads the second history page and keeps the server total, limit, and offset', async () => {
    const page: AutomationHistory = {
      automation_id: SCRIPT_ID,
      scope: 'company',
      engine_kind: 'script',
      config_version: 4,
      active_version: 3,
      entries: [
        {
          version: 4,
          prev_version: 3,
          restored_from_version: null,
          actor_user_id: null,
          changed_at: null,
          action: 'update',
          changes: { cron_expression: { old: '0 6 * * *', new: '0 7 * * *' } },
          snapshot: { cron_expression: '0 7 * * *' },
        },
      ],
      total: 80,
      limit: 50,
      offset: 50,
    };
    vi.mocked(api.listAutomationHistory).mockResolvedValue(ok(page));
    const { result, queryClient } = renderHookWithProviders(() =>
      useAutomationHistory(SCRIPT_ID, { limit: 50, offset: 50 }),
    );
    await waitFor(() => expect(result.current.data?.total).toBe(80));
    expect(api.listAutomationHistory).toHaveBeenCalledWith(SCRIPT_ID, { limit: 50, offset: 50 });
    expect(result.current.data?.entries).toHaveLength(1);
    expect(result.current.data?.limit).toBe(50);
    expect(result.current.data?.offset).toBe(50);
    expect(queryClient.getQueryData(
      queryKeys.automations.readHistory(USER, SCRIPT_ID, { limit: 50, offset: 50 }),
    )).toEqual(page);
  });

  it('keeps server has_more on the feed and on one task runs', async () => {
    const feed: AutomationRunReadList = {
      executions: [{ engine_kind: 'script', automation_id: SCRIPT_ID, status: 'succeeded' } as AutomationRunRead],
      has_more: true,
    };
    vi.mocked(api.listAutomationRunReads).mockResolvedValue(ok(feed));
    const feedHook = renderHookWithProviders(() => useAutomationRunReads({ scope: 'company' }));
    await waitFor(() => expect(feedHook.result.current.isSuccess).toBe(true));
    expect(feedHook.result.current.data?.has_more).toBe(true);
    expect(feedHook.result.current.data?.executions).toHaveLength(1);
    feedHook.unmount();

    const full: AutomationRunReadList = {
      executions: Array.from({ length: 20 }, (_, index) => ({
        engine_kind: 'agent',
        automation_execution_id: `exec-${index}`,
      })) as AutomationRunRead[],
      has_more: false,
    };
    vi.mocked(api.listAutomationExecutionReads).mockResolvedValue(ok(full));
    const { result } = renderHookWithProviders(() =>
      useAutomationExecutionReads(AGENT_ID, { limit: 20, offset: 0 }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.has_more).toBe(false);
    expect(result.current.data?.executions).toHaveLength(20);
  });

  it('puts the same default limit and offset in the query key as the request', async () => {
    vi.mocked(api.listAutomationReads).mockResolvedValue(ok(listPage(1)));
    vi.mocked(api.listAutomationRunReads).mockResolvedValue(ok({ executions: [], has_more: false }));
    vi.mocked(api.listAutomationExecutionReads).mockResolvedValue(ok({ executions: [], has_more: false }));
    vi.mocked(api.listAutomationHistory).mockResolvedValue(ok({
      automation_id: SCRIPT_ID,
      scope: 'company',
      engine_kind: 'script',
      config_version: 1,
      active_version: 1,
      entries: [],
      total: 0,
      limit: 50,
      offset: 0,
    }));
    const listed = renderHookWithProviders(() => useAutomationReadList());
    const feed = renderHookWithProviders(() => useAutomationRunReads());
    const runs = renderHookWithProviders(() => useAutomationExecutionReads(SCRIPT_ID));
    const history = renderHookWithProviders(() => useAutomationHistory(SCRIPT_ID));
    await waitFor(() => expect(listed.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(feed.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(runs.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(history.result.current.isSuccess).toBe(true));

    expect(api.listAutomationReads).toHaveBeenCalledWith({});
    expect(listed.queryClient.getQueryData(listKey(null))).toEqual(listPage(1));
    expect(api.listAutomationRunReads).toHaveBeenCalledWith({});
    expect(feed.queryClient.getQueryData(queryKeys.automations.readRuns(USER, {
      scope: null,
      status: null,
      thread_id: null,
      limit: 20,
      offset: 0,
    }))).toEqual({ executions: [], has_more: false });
    expect(api.listAutomationExecutionReads).toHaveBeenCalledWith(SCRIPT_ID, {});
    expect(runs.queryClient.getQueryData(queryKeys.automations.readExecutions(USER, SCRIPT_ID, {
      limit: 20,
      offset: 0,
    }))).toEqual({ executions: [], has_more: false });
    expect(api.listAutomationHistory).toHaveBeenCalledWith(SCRIPT_ID, {});
    expect(history.queryClient.getQueryData<AutomationHistory>(queryKeys.automations.readHistory(USER, SCRIPT_ID, {
      limit: 50,
      offset: 0,
    }))?.total).toBe(0);
  });

  it('keeps each read kind out of the agent-only cache', async () => {
    vi.mocked(api.listAutomationReads).mockResolvedValue(ok(listPage(4)));
    vi.mocked(api.listAutomationRunReads).mockResolvedValue(ok({ executions: [], has_more: true }));
    const { result, queryClient } = renderHookWithProviders(() => {
      const list = useAutomationReadList({ scope: 'company' });
      const feed = useAutomationRunReads({ scope: 'company' });
      return { list, feed };
    });
    await waitFor(() => expect(result.current.list.data?.total).toBe(4));
    await waitFor(() => expect(result.current.feed.data?.has_more).toBe(true));
    expect(queryClient.getQueryData(queryKeys.automations.list({ limit: 100, offset: 0 }))).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.automations.runs())).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.automations.executions(SCRIPT_ID))).toBeUndefined();
    expect(listKey('company').slice(0, queryKeys.automations.all.length)).toEqual(queryKeys.automations.all);
    expect(result.current.list.data).not.toBe(result.current.feed.data);
  });
});

describe('useAutomationReads cache identity', () => {
  it('does not reuse a list page when scope changes', async () => {
    const totals = { default: 11, personal: 22, research_tracking: 33, company: 44 } as const;
    let hold: Deferred | null = null;
    let holdScope: string | null = null;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params = {}) => {
      const key = params.scope ?? 'default';
      if (hold && holdScope === key) await hold.promise;
      return ok(listPage(totals[key]));
    });
    const { result, rerender, queryClient } = renderRead(
      (props: { scope?: AutomationScope }) => useAutomationReadList(props),
      {},
      retainingClient(),
    );
    await waitFor(() => expect(result.current.data?.total).toBe(11));

    const arm = (scope: string) => {
      holdScope = scope;
      hold = deferred();
      return hold;
    };
    const next = async (scope: AutomationScope | undefined, total: number) => {
      const calls = vi.mocked(api.listAutomationReads).mock.calls.length;
      const gate = arm(scope ?? 'default');
      rerender({ scope });
      await waitFor(() => {
        expect(result.current.isFetching).toBe(true);
        expect(result.current.data).toBeUndefined();
      });
      expect(result.current.isSuccess).toBe(false);
      gate.resolve();
      hold = null;
      await waitFor(() => expect(result.current.data?.total).toBe(total));
      expect(vi.mocked(api.listAutomationReads).mock.calls.length).toBe(calls + 1);
    };

    await next('personal', 22);
    await next('research_tracking', 33);
    await next('company', 44);
    const calls = vi.mocked(api.listAutomationReads).mock.calls.length;
    rerender({});
    await waitFor(() => expect(result.current.data?.total).toBe(11));
    expect(vi.mocked(api.listAutomationReads).mock.calls.length).toBe(calls);
    expect(queryClient.getQueryData(listKey(null))).toEqual(listPage(11));
    expect(queryClient.getQueryData(listKey('personal'))).toEqual(listPage(22));
    expect(queryClient.getQueryData(listKey('research_tracking'))).toEqual(listPage(33));
    expect(queryClient.getQueryData(listKey('company'))).toEqual(listPage(44));
  });

  it('does not reuse another task detail, runs, or history', async () => {
    let hold: Deferred | null = null;
    const detailFor = (id: string): AutomationRead => (
      id === SCRIPT_ID
        ? { engine_kind: 'script', automation_id: id, name: 'Daily prices' }
        : { engine_kind: 'agent', automation_id: id, name: 'Morning note', instruction: 'Summarize.' }
    ) as AutomationRead;
    vi.mocked(api.getAutomationRead).mockImplementation(async (id) => {
      if (hold) await hold.promise;
      return ok(detailFor(id));
    });
    vi.mocked(api.listAutomationExecutionReads).mockImplementation(async (id) => {
      if (hold) await hold.promise;
      return ok({
        executions: [{ engine_kind: 'script', automation_id: id, status: 'succeeded' } as AutomationRunRead],
        has_more: id === SCRIPT_ID,
      });
    });
    vi.mocked(api.listAutomationHistory).mockImplementation(async (id) => {
      if (hold) await hold.promise;
      return ok({
        automation_id: id,
        scope: 'company',
        engine_kind: id === SCRIPT_ID ? 'script' : 'agent',
        config_version: 1,
        active_version: 1,
        entries: [],
        total: id === SCRIPT_ID ? 3 : 9,
        limit: 50,
        offset: 0,
      } as AutomationHistory);
    });
    const args = { id: SCRIPT_ID as string | null | undefined };
    const { result, rerender, queryClient } = renderHookWithProviders(() => ({
      detail: useAutomationRead(args.id),
      runs: useAutomationExecutionReads(args.id),
      history: useAutomationHistory(args.id),
    }), { queryClient: retainingClient() });
    await waitFor(() => expect(result.current.detail.data?.name).toBe('Daily prices'));
    expect(result.current.runs.data?.has_more).toBe(true);
    expect(result.current.history.data?.total).toBe(3);

    const gate = deferred();
    hold = gate;
    args.id = AGENT_ID;
    rerender();
    await waitFor(() => {
      expect(result.current.detail.data).toBeUndefined();
      expect(result.current.runs.data).toBeUndefined();
      expect(result.current.history.data).toBeUndefined();
      expect(result.current.detail.isFetching).toBe(true);
    });
    expect(result.current.detail.isSuccess).toBe(false);
    gate.resolve();
    hold = null;
    await waitFor(() => expect(result.current.detail.data?.name).toBe('Morning note'));
    expect(result.current.runs.data?.has_more).toBe(false);
    expect(result.current.history.data?.total).toBe(9);
    expect(queryClient.getQueryData(queryKeys.automations.readDetail(USER, SCRIPT_ID))).toMatchObject({
      name: 'Daily prices',
    });
    expect(queryClient.getQueryData(queryKeys.automations.readDetail(USER, AGENT_ID))).toMatchObject({
      name: 'Morning note',
    });
    expect(queryClient.getQueryData(queryKeys.automations.executions(SCRIPT_ID))).toBeUndefined();

    const calls = vi.mocked(api.getAutomationRead).mock.calls.length;
    args.id = '';
    rerender();
    await flush();
    expect(vi.mocked(api.getAutomationRead)).toHaveBeenCalledTimes(calls);
    expect(vi.mocked(api.listAutomationExecutionReads)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.listAutomationHistory)).toHaveBeenCalledTimes(2);
    expect(result.current.detail.data).toBeUndefined();
    expect(result.current.runs.data).toBeUndefined();
    expect(result.current.history.data).toBeUndefined();
    expect(result.current.detail.isFetching).toBe(false);
    const dumped = JSON.stringify(queryClient.getQueryCache().getAll().map((query) => query.queryKey));
    expect(dumped).not.toContain('undefined');
  });

  it('does not reuse a page when the user changes', async () => {
    let hold: Deferred | null = null;
    vi.mocked(api.listAutomationReads).mockImplementation(async () => {
      const user = auth.userId;
      if (hold && user === 'user-2') await hold.promise;
      return ok(listPage(user === USER ? 5 : 9));
    });
    const { result, rerender, queryClient } = renderHookWithProviders(
      () => useAutomationReadList({ scope: 'company' }),
      { queryClient: retainingClient() },
    );
    await waitFor(() => expect(result.current.data?.total).toBe(5));
    const gate = deferred();
    hold = gate;
    auth.userId = 'user-2';
    rerender();
    await waitFor(() => {
      expect(result.current.isFetching).toBe(true);
      expect(result.current.data).toBeUndefined();
    });
    gate.resolve();
    await waitFor(() => expect(result.current.data?.total).toBe(9));
    expect(queryClient.getQueryData(queryKeys.automations.readList(USER, {
      scope: 'company',
      status: null,
      limit: 50,
      offset: 0,
    }))).toEqual(listPage(5));
    expect(queryClient.getQueryData(queryKeys.automations.readList('user-2', {
      scope: 'company',
      status: null,
      limit: 50,
      offset: 0,
    }))).toEqual(listPage(9));
  });

  it('does not reuse a list page when status changes', async () => {
    let hold: Deferred | null = null;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params = {}) => {
      if (hold && params.status === 'timed_out') await hold.promise;
      return ok(listPage(params.status === 'succeeded' ? 3 : 8));
    });
    const { result, rerender } = renderRead(
      (props: { status?: string }) => useAutomationReadList(props),
      { status: 'succeeded' },
      retainingClient(),
    );
    await waitFor(() => expect(result.current.data?.total).toBe(3));
    const gate = deferred();
    hold = gate;
    rerender({ status: 'timed_out' });
    await waitFor(() => {
      expect(result.current.data).toBeUndefined();
      expect(result.current.isSuccess).toBe(false);
    });
    gate.resolve();
    await waitFor(() => expect(result.current.data?.total).toBe(8));
    expect(api.listAutomationReads).toHaveBeenLastCalledWith({ status: 'timed_out' });
  });

  it('does not reuse a feed page when thread_id changes', async () => {
    let hold: Deferred | null = null;
    vi.mocked(api.listAutomationRunReads).mockImplementation(async (params = {}) => {
      if (hold && params.thread_id === THREAD_B) await hold.promise;
      return ok({
        executions: [],
        has_more: params.thread_id === THREAD_A,
      });
    });
    const { result, rerender } = renderRead(
      (props: { thread_id?: string }) => useAutomationRunReads(props),
      { thread_id: THREAD_A },
      retainingClient(),
    );
    await waitFor(() => expect(result.current.data?.has_more).toBe(true));
    const gate = deferred();
    hold = gate;
    rerender({ thread_id: THREAD_B });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    gate.resolve();
    await waitFor(() => expect(result.current.data?.has_more).toBe(false));
    expect(api.listAutomationRunReads).toHaveBeenLastCalledWith({ thread_id: THREAD_B });
  });

  it('does not reuse a list page when offset changes', async () => {
    let hold: Deferred | null = null;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params = {}) => {
      if (hold && params.offset === 50) await hold.promise;
      return ok(listPage(params.offset === 50 ? 80 : 4));
    });
    const { result, rerender, queryClient } = renderRead(
      (props: { offset?: number }) => useAutomationReadList(props),
      { offset: 0 },
      retainingClient(),
    );
    await waitFor(() => expect(result.current.data?.total).toBe(4));
    const gate = deferred();
    hold = gate;
    rerender({ offset: 50 });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    gate.resolve();
    await waitFor(() => expect(result.current.data?.total).toBe(80));
    expect(queryClient.getQueryData(listKey(null, { offset: 0 }))).toEqual(listPage(4));
    expect(queryClient.getQueryData(listKey(null, { offset: 50 }))).toEqual(listPage(80));
  });

  it('does not reuse a feed page when limit changes', async () => {
    let hold: Deferred | null = null;
    vi.mocked(api.listAutomationRunReads).mockImplementation(async (params = {}) => {
      if (hold && params.limit === 50) await hold.promise;
      return ok({ executions: [], has_more: params.limit !== 50 });
    });
    const { result, rerender } = renderRead(
      (props: { limit?: number }) => useAutomationRunReads(props),
      { limit: 20 },
      retainingClient(),
    );
    await waitFor(() => expect(result.current.data?.has_more).toBe(true));
    const gate = deferred();
    hold = gate;
    rerender({ limit: 50 });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    gate.resolve();
    await waitFor(() => expect(result.current.data?.has_more).toBe(false));
    expect(api.listAutomationRunReads).toHaveBeenLastCalledWith({ limit: 50 });
  });
});

describe('useAutomationReads requests', () => {
  it('does not request for an empty id, a logged-out user, or enabled false', async () => {
    for (const id of ['', null, undefined]) {
      const detail = renderHookWithProviders(() => useAutomationRead(id));
      const runs = renderHookWithProviders(() => useAutomationExecutionReads(id));
      const history = renderHookWithProviders(() => useAutomationHistory(id));
      await flush();
      detail.unmount();
      runs.unmount();
      history.unmount();
    }
    expect(api.getAutomationRead).not.toHaveBeenCalled();
    expect(api.listAutomationExecutionReads).not.toHaveBeenCalled();
    expect(api.listAutomationHistory).not.toHaveBeenCalled();

    auth.userId = null;
    const loggedOut = renderHookWithProviders(() => ({
      list: useAutomationReadList({ scope: 'company' }),
      detail: useAutomationRead(SCRIPT_ID),
      feed: useAutomationRunReads({ scope: 'company' }),
      runs: useAutomationExecutionReads(SCRIPT_ID),
      history: useAutomationHistory(SCRIPT_ID),
    }));
    await flush();
    expect(api.listAutomationReads).not.toHaveBeenCalled();
    expect(api.getAutomationRead).not.toHaveBeenCalled();
    expect(api.listAutomationRunReads).not.toHaveBeenCalled();
    expect(api.listAutomationExecutionReads).not.toHaveBeenCalled();
    expect(api.listAutomationHistory).not.toHaveBeenCalled();
    loggedOut.unmount();

    auth.userId = USER;
    vi.mocked(api.listAutomationReads).mockResolvedValue(ok(listPage(4)));
    const gated = renderRead(
      (props: { scope: 'company'; enabled: boolean }) => useAutomationReadList(props),
      { scope: 'company', enabled: false },
    );
    await flush();
    expect(api.listAutomationReads).not.toHaveBeenCalled();
    gated.rerender({ scope: 'company', enabled: true });
    await waitFor(() => expect(api.listAutomationReads).toHaveBeenCalledTimes(1));
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'company' });
    await waitFor(() => expect(gated.result.current.data?.total).toBe(4));
  });

  it('does not return a cached company list after a 403', async () => {
    const cached: AutomationReadList = {
      automations: [{ engine_kind: 'script', automation_id: SCRIPT_ID, name: 'Daily prices' } as AutomationRead],
      total: 4,
    };
    const fresh = listPage(1);
    vi.mocked(api.listAutomationReads)
      .mockResolvedValueOnce(ok(cached))
      .mockRejectedValueOnce(httpError(403))
      .mockResolvedValueOnce(ok(fresh));
    const { result } = renderHookWithProviders(() => useAutomationReadList({ scope: 'company' }));
    await waitFor(() => expect(result.current.data?.total).toBe(4));
    const refused = await readRefetch(result.current.refetch);
    expect(refused.data).toBeUndefined();
    expect(refused.isError).toBe(true);
    expect(refused.isSuccess).toBe(false);
    expect(refused.status).toBe('error');
    expect(refused.isRefetchError).toBe(true);
    expect(refused.isLoadingError).toBe(false);
    expect(apiErrorStatus(refused.error)).toBe(403);
    expect(refused.data).not.toEqual(cached);
    await waitFor(() => {
      expect(result.current.data).toBeUndefined();
      expect(result.current.isSuccess).toBe(false);
      expect(result.current.isError).toBe(true);
    });
    expect(apiErrorStatus(result.current.error)).toBe(403);
    expect(result.current.data).not.toEqual({ automations: [], total: 0 });
    const refreshed = await readRefetch(result.current.refetch);
    expect(refreshed.data).toEqual(fresh);
    expect(refreshed.isSuccess).toBe(true);
    expect(refreshed.isError).toBe(false);
    expect(refreshed.error).toBeNull();
    await waitFor(() => {
      expect(result.current.data).toEqual(fresh);
      expect(result.current.isSuccess).toBe(true);
      expect(result.current.isError).toBe(false);
    });
  });

  it('does not return a cached company task after a 403', async () => {
    const cached = { engine_kind: 'script', automation_id: SCRIPT_ID, name: 'Daily prices' } as AutomationRead;
    const fresh = { engine_kind: 'script', automation_id: SCRIPT_ID, name: 'Updated prices' } as AutomationRead;
    vi.mocked(api.getAutomationRead)
      .mockResolvedValueOnce(ok(cached))
      .mockRejectedValueOnce(httpError(403))
      .mockResolvedValueOnce(ok(fresh));
    const { result } = renderHookWithProviders(() => useAutomationRead(SCRIPT_ID));
    await waitFor(() => expect(result.current.data?.name).toBe('Daily prices'));
    const refused = await readRefetch(result.current.refetch);
    expect(refused.data).toBeUndefined();
    expect(refused.isError).toBe(true);
    expect(refused.isSuccess).toBe(false);
    expect(refused.status).toBe('error');
    expect(apiErrorStatus(refused.error)).toBe(403);
    await waitFor(() => {
      expect(result.current.data).toBeUndefined();
      expect(result.current.isSuccess).toBe(false);
      expect(result.current.isError).toBe(true);
    });
    const refreshed = await readRefetch(result.current.refetch);
    expect(refreshed.data).toEqual(fresh);
    expect(refreshed.isSuccess).toBe(true);
    expect(refreshed.isError).toBe(false);
    await waitFor(() => {
      expect(result.current.data?.name).toBe('Updated prices');
      expect(result.current.isSuccess).toBe(true);
    });
  });

  it('does not return cached feed, task runs, or history from a 403 refetch', async () => {
    const feed: AutomationRunReadList = {
      executions: [{ engine_kind: 'script', automation_id: SCRIPT_ID, status: 'succeeded' } as AutomationRunRead],
      has_more: true,
    };
    const runs: AutomationRunReadList = { executions: [], has_more: false };
    const history: AutomationHistory = {
      automation_id: SCRIPT_ID,
      scope: 'company',
      engine_kind: 'script',
      config_version: 4,
      active_version: 3,
      entries: [],
      total: 3,
      limit: 50,
      offset: 0,
    };
    vi.mocked(api.listAutomationRunReads).mockResolvedValueOnce(ok(feed)).mockRejectedValueOnce(httpError(403));
    const feedView = renderHookWithProviders(() => useAutomationRunReads({ scope: 'company' }));
    await waitFor(() => expect(feedView.result.current.data?.has_more).toBe(true));
    const refusedFeed = await readRefetch(feedView.result.current.refetch);
    expect(refusedFeed.data).toBeUndefined();
    expect(refusedFeed.isError).toBe(true);
    expect(refusedFeed.isSuccess).toBe(false);
    expect(apiErrorStatus(refusedFeed.error)).toBe(403);
    await waitFor(() => expect(feedView.result.current.data).toBeUndefined());
    feedView.unmount();

    vi.mocked(api.listAutomationExecutionReads).mockResolvedValueOnce(ok(runs)).mockRejectedValueOnce(httpError(403));
    const runsView = renderHookWithProviders(() => useAutomationExecutionReads(SCRIPT_ID));
    await waitFor(() => expect(runsView.result.current.isSuccess).toBe(true));
    const refusedRuns = await readRefetch(runsView.result.current.refetch);
    expect(refusedRuns.data).toBeUndefined();
    expect(refusedRuns.isSuccess).toBe(false);
    expect(apiErrorStatus(refusedRuns.error)).toBe(403);
    await waitFor(() => expect(runsView.result.current.data).toBeUndefined());
    expect(runsView.result.current.data).not.toEqual({ executions: [], has_more: false });
    runsView.unmount();

    vi.mocked(api.listAutomationHistory).mockResolvedValueOnce(ok(history)).mockRejectedValueOnce(httpError(403));
    const historyView = renderHookWithProviders(() => useAutomationHistory(SCRIPT_ID));
    await waitFor(() => expect(historyView.result.current.data?.total).toBe(3));
    const refusedHistory = await readRefetch(historyView.result.current.refetch);
    expect(refusedHistory.data).toBeUndefined();
    expect(refusedHistory.isError).toBe(true);
    expect(refusedHistory.isSuccess).toBe(false);
    expect(apiErrorStatus(refusedHistory.error)).toBe(403);
    await waitFor(() => {
      expect(historyView.result.current.data).toBeUndefined();
      expect(historyView.result.current.isSuccess).toBe(false);
    });
  });

  it('keeps a 404 or 503 refetch as an error and leaves the last page in place', async () => {
    const cached = listPage(4);
    for (const status of [404, 503]) {
      vi.mocked(api.listAutomationReads)
        .mockResolvedValueOnce(ok(cached))
        .mockRejectedValueOnce(httpError(status));
      const view = renderHookWithProviders(() => useAutomationReadList({ scope: 'company' }));
      await waitFor(() => expect(view.result.current.data?.total).toBe(4));
      const refused = await readRefetch(view.result.current.refetch);
      expect(refused.isError).toBe(true);
      expect(refused.isSuccess).toBe(false);
      expect(refused.status).toBe('error');
      expect(refused.isRefetchError).toBe(true);
      expect(apiErrorStatus(refused.error)).toBe(status);
      expect(refused.data).toEqual(cached);
      expect(refused.data).not.toEqual({ automations: [], total: 0 });
      await waitFor(() => {
        expect(view.result.current.isError).toBe(true);
        expect(view.result.current.isSuccess).toBe(false);
        expect(view.result.current.data).toEqual(cached);
      });
      view.unmount();
    }
  });

  it('keeps 404 and 503 as errors instead of empty pages', async () => {
    const mounts: Array<() => { result: { current: AutomationReadState<unknown> }; unmount: () => void }> = [
      () => renderHookWithProviders(() => useAutomationReadList({ scope: 'company' })),
      () => renderHookWithProviders(() => useAutomationRunReads({ scope: 'company' })),
      () => renderHookWithProviders(() => useAutomationRead(SCRIPT_ID)),
      () => renderHookWithProviders(() => useAutomationExecutionReads(SCRIPT_ID)),
      () => renderHookWithProviders(() => useAutomationHistory(SCRIPT_ID)),
    ];
    for (const status of [404, 503]) {
      const error = httpError(status);
      vi.mocked(api.listAutomationReads).mockRejectedValue(error);
      vi.mocked(api.listAutomationRunReads).mockRejectedValue(error);
      vi.mocked(api.getAutomationRead).mockRejectedValue(error);
      vi.mocked(api.listAutomationExecutionReads).mockRejectedValue(error);
      vi.mocked(api.listAutomationHistory).mockRejectedValue(error);
      for (const mount of mounts) {
        const view = mount();
        await waitFor(() => expect(view.result.current.isError).toBe(true));
        expect(view.result.current.isSuccess).toBe(false);
        expect(view.result.current.data).toBeUndefined();
        expect(apiErrorStatus(view.result.current.error)).toBe(status);
        expect(view.result.current.data).not.toEqual({ automations: [], total: 0 });
        expect(view.result.current.data).not.toEqual({ executions: [], has_more: false });
        view.unmount();
      }
    }
  });
});
