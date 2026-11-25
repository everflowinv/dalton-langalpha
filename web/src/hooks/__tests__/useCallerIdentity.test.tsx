import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import type { TeamCredentialsStatus } from '@/hooks/useTeamCredentials';

const envState = vi.hoisted(() => ({
  isDaltonBuild: true,
  isPlatformMode: false,
  daltonAuthMode: 'local' as '' | 'local' | 'supabase',
  isDaltonAuth: true,
  hasAuthEnv: true,
  userId: 'u-1' as string | null,
}));

const mockApiGet = vi.hoisted(() =>
  vi.fn<(url: string, config?: { signal?: AbortSignal }) => Promise<{ data: TeamCredentialsStatus }>>(),
);

vi.mock('@/config/brand', () => ({
  get isDaltonBuild() {
    return envState.isDaltonBuild;
  },
}));

vi.mock('@/config/hostMode', () => ({
  get isPlatformMode() {
    return envState.isPlatformMode;
  },
}));

vi.mock('@/config/daltonAuth', () => ({
  get DALTON_AUTH_MODE() {
    return envState.daltonAuthMode;
  },
  get isDaltonAuth() {
    return envState.isDaltonAuth;
  },
}));

vi.mock('@/config/localUser', () => ({
  LOCAL_USER_ID: 'local-user',
  get hasAuthEnv() {
    return envState.hasAuthEnv;
  },
}));

vi.mock('@/contexts/AuthContext', async () => {
  const ReactMod = await import('react');
  const AuthContext = ReactMod.createContext<{ userId: string | null } | null>(null);
  return {
    AuthContext,
    useAuth: () => ({ userId: envState.userId }),
  };
});

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (url: string, config?: { signal?: AbortSignal }) => mockApiGet(url, config),
    },
  };
});

import { AuthContext } from '@/contexts/AuthContext';
import { useDaltonTeamModelsAvailable } from '@/pages/Setup/answers/useTeamModelsAvailable';
import { useCallerIdentity } from '../useCallerIdentity';
import { useTeamCredentials } from '../useTeamCredentials';

function makeStatus(overrides: Partial<TeamCredentialsStatus> = {}): TeamCredentialsStatus {
  return {
    sharing: 'team',
    enabled: true,
    holder_user_id: null,
    credential_user_id: 'u-1',
    is_pm: false,
    can_disconnect: false,
    can_invite: true,
    caller_tier: 'internal',
    colleague_sharing_enabled: true,
    auth_mode: 'local',
    ...overrides,
  };
}

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Infinity,
      },
    },
  });
}

function wrapperOf(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <AuthContext.Provider value={{ userId: envState.userId } as never}>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </AuthContext.Provider>
    );
  };
}

describe('useCallerIdentity', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    envState.isDaltonBuild = true;
    envState.isPlatformMode = false;
    envState.daltonAuthMode = 'local';
    envState.isDaltonAuth = true;
    envState.hasAuthEnv = true;
    envState.userId = 'u-1';
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('short-circuits in platform mode without calling /credentials/status', () => {
    envState.isPlatformMode = true;
    const client = makeClient();
    const { result } = renderHook(() => useCallerIdentity({ revalidateOnOpen: true, open: true }), {
      wrapper: wrapperOf(client),
    });

    expect(mockApiGet).not.toHaveBeenCalled();
    expect(result.current.status).toBe('ready');
    expect(result.current.tier).toBe('admin');
    expect(result.current.isPlatform).toBe(true);
    expect(result.current.isKnown).toBe(true);
    expect(result.current.isConfirming).toBe(false);
    expect(result.current.canActAsOwner).toBe(true);
    expect(result.current.canPublishPublic).toBe(true);
    expect(result.current.canInvite).toBe(false);
    expect(result.current.getLatestIdentity().isPlatform).toBe(true);
  });

  it('recognizes single-user OSS mode from server auth_mode="oss" with canInvite=false', async () => {
    envState.daltonAuthMode = '';
    envState.isDaltonAuth = false;
    envState.userId = 'local-user';
    mockApiGet.mockResolvedValueOnce({
      data: makeStatus({
        auth_mode: 'oss',
        caller_tier: 'admin',
        can_invite: false,
        is_pm: true,
      }),
    });

    const client = makeClient();
    const { result } = renderHook(() => useCallerIdentity(), { wrapper: wrapperOf(client) });

    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.tier).toBe('admin');
    expect(result.current.authMode).toBe('oss');
    expect(result.current.isSingleUserOss).toBe(true);
    expect(result.current.isMultiUserAuth).toBe(false);
    expect(result.current.isAdmin).toBe(true);
    expect(result.current.canActAsOwner).toBe(true);
    expect(result.current.canPublishPublic).toBe(true);
    expect(result.current.canInvite).toBe(false);
  });

  it('isolates admin, internal, and external tiers in local auth mode', async () => {
    for (const tier of ['admin', 'internal', 'external'] as const) {
      mockApiGet.mockResolvedValueOnce({
        data: makeStatus({
          auth_mode: 'local',
          caller_tier: tier,
          can_invite: tier !== 'external',
        }),
      });
      const client = makeClient();
      const { result, unmount } = renderHook(() => useCallerIdentity(), {
        wrapper: wrapperOf(client),
      });
      await waitFor(() => expect(result.current.status).toBe('ready'));
      expect(result.current.tier).toBe(tier);
      expect(result.current.isAdmin).toBe(tier === 'admin');
      expect(result.current.isInternal).toBe(tier !== 'external');
      expect(result.current.isExternal).toBe(tier === 'external');
      expect(result.current.canActAsOwner).toBe(tier !== 'external');
      expect(result.current.canPublishPublic).toBe(tier === 'admin');
      expect(result.current.canInvite).toBe(tier !== 'external');
      unmount();
    }
  });

  it('fails closed to status="error", tier="unknown" when VITE_DALTON_AUTH is missing and core/local returns 401', async () => {
    envState.isDaltonBuild = true;
    envState.daltonAuthMode = '';
    envState.isDaltonAuth = false;
    envState.userId = 'local-user';
    mockApiGet.mockRejectedValueOnce(
      new ApiError('Request failed with status code 401', 'ERR_BAD_REQUEST', {
        status: 401,
        statusText: 'Unauthorized',
        headers: {},
        data: { detail: 'Authentication required' },
      }),
    );

    const client = makeClient();
    const { result } = renderHook(() => useCallerIdentity(), { wrapper: wrapperOf(client) });

    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(result.current.tier).toBe('unknown');
    expect(result.current.isKnown).toBe(false);
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isInternal).toBe(false);
    expect(result.current.isExternal).toBe(false);
    expect(result.current.canActAsOwner).toBe(false);
    expect(result.current.canPublishPublic).toBe(false);
    expect(result.current.canInvite).toBe(false);
  });

  it('fails closed without network calls when supabase auth env is missing', () => {
    envState.daltonAuthMode = 'supabase';
    envState.isDaltonAuth = true;
    envState.hasAuthEnv = false;

    const client = makeClient();
    const { result } = renderHook(() => useCallerIdentity(), { wrapper: wrapperOf(client) });

    expect(mockApiGet).not.toHaveBeenCalled();
    expect(result.current.status).toBe('error');
    expect(result.current.tier).toBe('unknown');
    expect(result.current.canActAsOwner).toBe(false);
  });

  it('aborts an in-flight useTeamCredentials cold-start HTTP request via cancelQueries and issues a fresh request', async () => {
    let signalA: AbortSignal | undefined;
    let resolveB!: (v: { data: TeamCredentialsStatus }) => void;
    let callCount = 0;

    mockApiGet.mockImplementation((_url, config) => {
      callCount += 1;
      if (callCount === 1) {
        signalA = config?.signal;
        return new Promise((_resolve, reject) => {
          config?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
      }
      return new Promise((resolve) => {
        resolveB = resolve;
      });
    });

    const client = makeClient();
    const wrapper = wrapperOf(client);

    // 1. useTeamCredentials initiates cold-start request A while cache is empty.
    renderHook(() => useTeamCredentials(), { wrapper });
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
    expect(signalA).toBeDefined();
    expect(signalA?.aborted).toBe(false);

    // 2. Sensitive surface mounts and triggers useCallerIdentity().revalidate().
    const { result } = renderHook(() => useCallerIdentity(), { wrapper });
    let revalidatePromise!: Promise<void>;
    act(() => {
      revalidatePromise = result.current.revalidate();
    });

    // Same-tick synchronous interception before React re-renders.
    expect(result.current.getLatestIdentity().isConfirming).toBe(true);
    expect(result.current.isConfirmingNow()).toBe(true);

    // Request A's AbortSignal is genuinely aborted and request B is issued.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));
    expect(signalA?.aborted).toBe(true);

    resolveB({ data: makeStatus({ caller_tier: 'external', can_invite: false }) });
    await act(async () => {
      await revalidatePromise;
    });

    expect(result.current.getLatestIdentity().tier).toBe('external');
    expect(result.current.getLatestIdentity().isConfirming).toBe(false);
    await waitFor(() => expect(result.current.tier).toBe('external'));
  });

  it('shares activeSeq and isConfirming across instances and synchronously blocks stale admin closures upon downgrade to external', async () => {
    const client = makeClient();
    client.setQueryData(
      queryKeys.dalton.credentialsStatus('u-1'),
      makeStatus({ caller_tier: 'admin', is_pm: true, can_invite: true }),
    );
    const wrapper = wrapperOf(client);

    const hookA = renderHook(() => useCallerIdentity(), { wrapper });
    const hookB = renderHook(() => useCallerIdentity(), { wrapper });
    expect(hookA.result.current.tier).toBe('admin');
    expect(hookB.result.current.tier).toBe('admin');

    // Capture a stale admin closure from instance A BEFORE any downgrade or re-render.
    const capturedIdentityA = hookA.result.current;
    const staleAdminSubmitClosure = () => {
      const latest = capturedIdentityA.getLatestIdentity();
      if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) {
        return 'blocked';
      }
      return 'submitted';
    };
    expect(staleAdminSubmitClosure()).toBe('submitted');

    let signal1: AbortSignal | undefined;
    let rejectReq1!: (err: unknown) => void;
    let resolveReq2!: (v: { data: TeamCredentialsStatus }) => void;
    let call = 0;

    mockApiGet.mockImplementation((_url, config) => {
      call += 1;
      if (call === 1) {
        signal1 = config?.signal;
        return new Promise((_resolve, reject) => {
          rejectReq1 = reject;
          config?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
      }
      return new Promise((resolve) => {
        resolveReq2 = resolve;
      });
    });

    // Instance A starts revalidation #1.
    let pA!: Promise<void>;
    act(() => {
      pA = hookA.result.current.revalidate();
    });
    expect(hookA.result.current.isConfirmingNow()).toBe(true);
    expect(hookB.result.current.isConfirmingNow()).toBe(true);
    expect(staleAdminSubmitClosure()).toBe('blocked');

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));

    // While req #1 is still in flight, instance B starts revalidation #2.
    let pB!: Promise<void>;
    act(() => {
      pB = hookB.result.current.revalidate();
    });

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));
    expect(signal1?.aborted).toBe(true);

    // Wait for A's promise & finally to complete while req #2 is STILL in flight.
    rejectReq1(new DOMException('Aborted', 'AbortError'));
    await act(async () => {
      await pA;
    });

    // Both A and B MUST keep isConfirming === true while req #2 is still in flight.
    expect(hookA.result.current.isConfirming).toBe(true);
    expect(hookB.result.current.isConfirming).toBe(true);
    expect(hookA.result.current.isConfirmingNow()).toBe(true);
    expect(hookB.result.current.isConfirmingNow()).toBe(true);
    expect(hookA.result.current.getLatestIdentity().isConfirming).toBe(true);
    expect(hookB.result.current.getLatestIdentity().isConfirming).toBe(true);
    expect(staleAdminSubmitClosure()).toBe('blocked');

    // Now req #2 resolves with downgraded external tier.
    // Call the stale admin closure immediately after pB resolves, WITHOUT waiting for React re-render.
    resolveReq2({
      data: makeStatus({ caller_tier: 'external', is_pm: false, can_invite: false }),
    });
    await pB;

    const syncSnap = capturedIdentityA.getLatestIdentity();
    expect(syncSnap.isConfirming).toBe(false);
    expect(syncSnap.status).toBe('ready');
    expect(syncSnap.tier).toBe('external');
    expect(syncSnap.isExternal).toBe(true);
    expect(syncSnap.canActAsOwner).toBe(false);
    expect(staleAdminSubmitClosure()).toBe('blocked');

    await waitFor(() => expect(hookA.result.current.tier).toBe('external'));
  });

  it('forces fresh network requests past a fresh 5-minute cache on open, visibilitychange/focus, and explicit revalidate', async () => {
    const client = makeClient();
    client.setQueryData(
      queryKeys.dalton.credentialsStatus('u-1'),
      makeStatus({ caller_tier: 'admin' }),
    );
    let resolveNext!: (v: { data: TeamCredentialsStatus }) => void;
    mockApiGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveNext = resolve;
        }),
    );

    const { result } = renderHook(
      () => useCallerIdentity({ revalidateOnOpen: true, open: true }),
      { wrapper: wrapperOf(client) },
    );

    // 1. Open triggers fresh fetch despite fresh cache.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
    expect(result.current.isConfirming).toBe(true);
    await act(async () => {
      resolveNext({ data: makeStatus({ caller_tier: 'admin' }) });
    });
    await waitFor(() => expect(result.current.isConfirming).toBe(false));

    // 2. Returning to foreground triggers fresh fetch.
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'visible',
    });
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));
    expect(result.current.isConfirming).toBe(true);
    await act(async () => {
      resolveNext({ data: makeStatus({ caller_tier: 'internal' }) });
    });
    await waitFor(() => expect(result.current.tier).toBe('internal'));
    expect(result.current.isConfirming).toBe(false);

    // 3. Explicit 403 revalidate triggers fresh fetch.
    act(() => {
      void result.current.revalidate();
    });
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(3));
    expect(result.current.isConfirming).toBe(true);
    await act(async () => {
      resolveNext({ data: makeStatus({ caller_tier: 'external', can_invite: false }) });
    });
    await waitFor(() => expect(result.current.tier).toBe('external'));
  });

  it('keeps isConfirming=false during 60s silent background poll and cancels in-flight poll when 403 revalidate overlaps', async () => {
    vi.useFakeTimers();
    const client = makeClient();
    client.setQueryData(
      queryKeys.dalton.credentialsStatus('u-1'),
      makeStatus({ caller_tier: 'admin' }),
    );

    const signals: Array<AbortSignal | undefined> = [];
    const resolvers: Array<(v: { data: TeamCredentialsStatus }) => void> = [];
    mockApiGet.mockImplementation((_url, config) => {
      signals.push(config?.signal);
      return new Promise((resolve, reject) => {
        resolvers.push(resolve);
        config?.signal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        });
      });
    });

    const { result } = renderHook(
      () => useCallerIdentity({ revalidateOnOpen: true, open: true }),
      { wrapper: wrapperOf(client) },
    );

    // Settle initial mount revalidate (#1).
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolvers[0]({ data: makeStatus({ caller_tier: 'admin' }) });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isConfirming).toBe(false);

    // Advance 60s to trigger silent background poll (#2).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    // Silent poll MUST NOT set isConfirming.
    expect(result.current.isConfirming).toBe(false);
    expect(result.current.isConfirmingNow()).toBe(false);

    // While 60s poll (#2) is still in flight, simulate a 403 calling revalidate() (#3).
    let p403!: Promise<void>;
    await act(async () => {
      p403 = result.current.revalidate();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Request #2 (the 60s poll) must be aborted, request #3 issued, and isConfirming stays true.
    expect(signals[1]?.aborted).toBe(true);
    expect(mockApiGet).toHaveBeenCalledTimes(3);
    expect(result.current.isConfirming).toBe(true);
    expect(result.current.getLatestIdentity().isConfirming).toBe(true);

    // Settle request #3 with downgraded external tier.
    await act(async () => {
      resolvers[2]({ data: makeStatus({ caller_tier: 'external', can_invite: false }) });
      await p403;
    });

    expect(result.current.getLatestIdentity().isConfirming).toBe(false);
    expect(result.current.getLatestIdentity().tier).toBe('external');
    expect(result.current.tier).toBe('external');
  });

  it('defaults canInvite to false when multi-user credentials/status omits can_invite', async () => {
    for (const authMode of ['local', 'supabase'] as const) {
      envState.daltonAuthMode = authMode;
      envState.isDaltonAuth = true;
      const statusWithoutCanInvite = makeStatus({ auth_mode: authMode, caller_tier: 'internal' });
      delete statusWithoutCanInvite.can_invite;
      mockApiGet.mockResolvedValueOnce({ data: statusWithoutCanInvite });

      const client = makeClient();
      const { result, unmount } = renderHook(() => useCallerIdentity(), {
        wrapper: wrapperOf(client),
      });
      await waitFor(() => expect(result.current.status).toBe('ready'));
      expect(result.current.isInternal).toBe(true);
      expect(result.current.canInvite).toBe(false);
      unmount();
    }
  });

  it('aborts an in-flight useDaltonTeamModelsAvailable credentials/status request via shared fetchTeamCredentialsStatus signal on revalidate()', async () => {
    let signalA: AbortSignal | undefined;
    let resolveB!: (v: { data: TeamCredentialsStatus }) => void;
    let statusCalls = 0;

    mockApiGet.mockImplementation((url, config) => {
      if (url === '/api/v1/models') {
        return Promise.resolve({ data: { dalton_profile: 'everflow' } as unknown as TeamCredentialsStatus });
      }
      statusCalls += 1;
      if (statusCalls === 1) {
        signalA = config?.signal;
        return new Promise((_resolve, reject) => {
          config?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
      }
      return new Promise((resolve) => {
        resolveB = resolve;
      });
    });

    const client = makeClient();
    const wrapper = wrapperOf(client);

    // 1. useDaltonTeamModelsAvailable initiates cold-start /credentials/status request A.
    renderHook(() => useDaltonTeamModelsAvailable(true, [], false, false, false, null), { wrapper });
    await waitFor(() => expect(statusCalls).toBe(1));
    expect(signalA).toBeDefined();
    expect(signalA?.aborted).toBe(false);

    // 2. useCallerIdentity().revalidate() cancels request A and issues fresh request B.
    const { result } = renderHook(() => useCallerIdentity(), { wrapper });
    let p!: Promise<void>;
    act(() => {
      p = result.current.revalidate();
    });

    await waitFor(() => expect(statusCalls).toBe(2));
    expect(signalA?.aborted).toBe(true);

    resolveB({ data: makeStatus({ caller_tier: 'external', can_invite: false }) });
    await act(async () => {
      await p;
    });
    expect(result.current.getLatestIdentity().tier).toBe('external');
  });

  it('skips network fetches and revalidation when enabled is false while still reading cached identity', async () => {
    const emptyClient = makeClient();
    const emptyView = renderHook(
      () => useCallerIdentity({ enabled: false, revalidateOnOpen: true, open: true }),
      { wrapper: wrapperOf(emptyClient) },
    );

    expect(mockApiGet).not.toHaveBeenCalled();
    expect(emptyClient.isFetching()).toBe(0);
    expect(emptyView.result.current.status).toBe('loading');
    expect(emptyView.result.current.isInternal).toBe(false);

    await act(async () => {
      await emptyView.result.current.revalidate();
    });
    expect(mockApiGet).not.toHaveBeenCalled();
    expect(emptyClient.isFetching()).toBe(0);
    emptyView.unmount();

    const cachedClient = makeClient();
    cachedClient.setQueryData(
      queryKeys.dalton.credentialsStatus('u-1'),
      makeStatus({ caller_tier: 'admin', is_pm: true, can_invite: true }),
    );
    const cachedView = renderHook(() => useCallerIdentity({ enabled: false }), {
      wrapper: wrapperOf(cachedClient),
    });

    expect(mockApiGet).not.toHaveBeenCalled();
    expect(cachedClient.isFetching()).toBe(0);
    expect(cachedView.result.current.status).toBe('ready');
    expect(cachedView.result.current.tier).toBe('admin');
    expect(cachedView.result.current.isAdmin).toBe(true);
  });
});
