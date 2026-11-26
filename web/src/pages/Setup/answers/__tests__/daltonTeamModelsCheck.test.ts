// DALTON-HOOK(dalton-setup): unit tests for daltonTeamModelsCheck retry cancellation and forced refresh tracking
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { AvailableModels, ModelsResponse } from '@/api/model';
import { queryKeys } from '@/lib/queryKeys';

const h = vi.hoisted(() => ({
  getAvailableModels: vi.fn<() => Promise<ModelsResponse>>(),
  getProviderAvailableModels: vi.fn<
    (provider: string, options?: { refresh?: boolean; signal?: AbortSignal }) => Promise<AvailableModels>
  >(),
  getPreferences: vi.fn<() => Promise<Record<string, unknown>>>(async () => ({})),
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
  HOST_MODE: 'oss',
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/api/model', () => ({
  getAvailableModels: h.getAvailableModels,
  getProviderAvailableModels: h.getProviderAvailableModels,
}));

vi.mock('@/pages/Dashboard/utils/api', () => ({
  getPreferences: h.getPreferences,
}));

import {
  bumpDaltonTeamModelsRetry,
  checkDaltonTeamModelsAvailable,
  forceRefreshProviderAvailableModels,
} from '../daltonTeamModelsCheck';

const MODELS_RESPONSE: ModelsResponse = {
  models: {
    'antigravity-oauth': {
      display_name: 'Antigravity',
      models: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth'],
    },
  },
  model_metadata: {
    'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', access_type: 'oauth' },
    'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth', access_type: 'oauth' },
  },
  provider_catalog: [
    {
      provider: 'antigravity-oauth',
      display_name: 'Antigravity',
      access_type: 'oauth',
      brand_key: 'antigravity',
      byok_eligible: false,
    },
  ],
  system_defaults: {},
  compaction_profiles: {} as ModelsResponse['compaction_profiles'],
  search_providers: {} as ModelsResponse['search_providers'],
  key_candidates: {},
};

const PROVIDERS = [
  {
    provider: 'antigravity-oauth',
    display_name: 'Antigravity',
    access_type: 'oauth' as const,
    state: 'connected' as const,
    masked_key: null,
  },
];

const staleMissing = (): AvailableModels => ({
  provider: 'antigravity-oauth',
  status: 'ok',
  fetched_at: null,
  ready: [],
  missing: ['gemini-4-neon-high-oauth'],
  context_windows: {},
  others: [],
  total: 0,
  cached: true,
});

const freshReady = (): AvailableModels => ({
  provider: 'antigravity-oauth',
  status: 'ok',
  fetched_at: null,
  ready: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth'],
  missing: [],
  context_windows: {},
  others: [],
  total: 2,
  cached: false,
});

const fallbackFailed = (): AvailableModels => ({
  provider: 'antigravity-oauth',
  status: 'fallback',
  reason: 'unreachable',
  fetched_at: null,
  ready: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth'],
  missing: [],
  context_windows: {},
  others: [],
  total: 2,
  cached: false,
});

beforeEach(() => {
  vi.clearAllMocks();
  h.getAvailableModels.mockResolvedValue(MODELS_RESPONSE);
  h.getPreferences.mockResolvedValue({});
});

describe('daltonTeamModelsCheck forced retry', () => {
  it('cancels an in-flight normal provider request before issuing refresh=1 so retry never reuses stale cache', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let resolveNormal!: (v: AvailableModels) => void;
    let normalSignal: AbortSignal | undefined;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      const refresh = Boolean(opts?.refresh);
      calls.push({ refresh });
      if (!refresh) {
        normalSignal = opts?.signal;
        return new Promise<AvailableModels>((resolve) => {
          resolveNormal = resolve;
        });
      }
      return Promise.resolve(freshReady());
    });

    // Start a normal (non-refresh) request on the exact same query key (as useAllModels does).
    const normalFetch = qc
      .fetchQuery({
        queryKey: queryKeys.providerModels.available('antigravity-oauth', ''),
        queryFn: ({ signal }) => h.getProviderAvailableModels('antigravity-oauth', { signal }),
        staleTime: 10 * 60_000,
        retry: false,
      })
      .catch(() => null);

    expect(calls).toEqual([{ refresh: false }]);
    expect(normalSignal?.aborted).toBe(false);

    // User clicks Retry while the normal request is still in-flight.
    bumpDaltonTeamModelsRetry(qc);
    const result = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });

    expect(normalSignal?.aborted).toBe(true);
    expect(calls).toEqual([{ refresh: false }, { refresh: true }]);
    expect(result.available).toBe(true);

    // Late resolution of the cancelled normal request does not taint the fresh result.
    resolveNormal(staleMissing());
    await normalFetch;

    // A follow-up check in the same retry generation reuses the fresh result without another refresh=1.
    const followUp = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(followUp.available).toBe(true);
    expect(calls).toEqual([{ refresh: false }, { refresh: true }]);
    qc.clear();
  });

  it('only marks the provider as refreshed after a forced refresh=1 request succeeds', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const calls: Array<{ refresh: boolean }> = [];
    let attempt = 0;

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      const refresh = Boolean(opts?.refresh);
      calls.push({ refresh });
      attempt += 1;
      if (attempt === 1) {
        return Promise.reject(new Error('transient network failure'));
      }
      return Promise.resolve(freshReady());
    });

    bumpDaltonTeamModelsRetry(qc);
    // First forced attempt fails -> provider must NOT be marked refreshed.
    await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(calls).toEqual([{ refresh: true }]);

    // Next check under the same retry generation still sends refresh=1 until it succeeds.
    const recovered = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(recovered.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);

    // Once succeeded, subsequent check in same generation reuses cached fresh data.
    const cached = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(cached.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);
    qc.clear();
  });

  // DALTON-HOOK(dalton-setup): concurrent retries on the same generation share one refresh=1 request and abort non-refresh
  it('deduplicates concurrent retry checks on the same generation while cancelling an in-flight non-refresh request', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let normalSignal: AbortSignal | undefined;
    let resolveForced!: (v: AvailableModels) => void;
    let forcedSignal: AbortSignal | undefined;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      const refresh = Boolean(opts?.refresh);
      calls.push({ refresh });
      if (!refresh) {
        normalSignal = opts?.signal;
        return new Promise<AvailableModels>(() => {});
      }
      forcedSignal = opts?.signal;
      return new Promise<AvailableModels>((resolve) => {
        resolveForced = resolve;
      });
    });

    const normalFetch = qc
      .fetchQuery({
        queryKey: queryKeys.providerModels.available('antigravity-oauth', ''),
        queryFn: ({ signal }) => h.getProviderAvailableModels('antigravity-oauth', { signal }),
        staleTime: 10 * 60_000,
        retry: false,
      })
      .catch(() => null);

    expect(calls).toEqual([{ refresh: false }]);
    expect(normalSignal?.aborted).toBe(false);

    // DALTON-HOOK(dalton-setup): fire two overlapping checks on the same retry generation
    bumpDaltonTeamModelsRetry(qc);
    const first = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    const second = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });

    await vi.waitFor(() => expect(calls).toEqual([{ refresh: false }, { refresh: true }]));
    expect(normalSignal?.aborted).toBe(true);
    expect(forcedSignal?.aborted).toBe(false);

    resolveForced(freshReady());
    const [res1, res2] = await Promise.all([first, second]);
    expect(res1.available).toBe(true);
    expect(res2.available).toBe(true);
    // Exactly one refresh=1 request was sent; the second concurrent caller never cancelled it or reused the non-refresh request.
    expect(calls).toEqual([{ refresh: false }, { refresh: true }]);
    await normalFetch;
    qc.clear();
  });

  // DALTON-HOOK(dalton-setup): consecutive retry clicks (new retry generation) share the in-flight refresh=1 without cancelling it
  it('reuses an in-flight forced refresh across consecutive retry clicks without cancelling it', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let resolveForced!: (v: AvailableModels) => void;
    let forcedSignal: AbortSignal | undefined;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      const refresh = Boolean(opts?.refresh);
      calls.push({ refresh });
      forcedSignal = opts?.signal;
      return new Promise<AvailableModels>((resolve) => {
        resolveForced = resolve;
      });
    });

    // First click starts refresh=1 (generation 1)
    bumpDaltonTeamModelsRetry(qc);
    const first = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));
    expect(forcedSignal?.aborted).toBe(false);

    // DALTON-HOOK(dalton-setup): second rapid click bumps generation 2 while gen 1 refresh=1 is still in-flight
    bumpDaltonTeamModelsRetry(qc);
    const second = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });

    // Must NOT abort the first forced refresh or send a second refresh=1 request
    await Promise.resolve();
    expect(forcedSignal?.aborted).toBe(false);
    expect(calls).toEqual([{ refresh: true }]);

    resolveForced(freshReady());
    const [res1, res2] = await Promise.all([first, second]);
    expect(res1.available).toBe(true);
    expect(res2.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }]);

    // Subsequent check in generation 2 reuses the freshly resolved live result without another refresh=1
    const followUp = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(followUp.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }]);
    qc.clear();
  });

  // DALTON-HOOK(dalton-setup): throttled server response (cached: true) must NOT mark provider as refreshed
  it('does not mark a provider as refreshed when refresh=1 returns a server-throttled cached: true response', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const calls: Array<{ refresh: boolean }> = [];
    let attempt = 0;

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      const refresh = Boolean(opts?.refresh);
      calls.push({ refresh });
      attempt += 1;
      // Attempt 1 hits backend 10s throttle -> server answers stale list with cached: true
      if (attempt === 1) {
        return Promise.resolve(staleMissing());
      }
      return Promise.resolve(freshReady());
    });

    bumpDaltonTeamModelsRetry(qc);
    const throttled = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(throttled.available).toBe(false);
    expect(calls).toEqual([{ refresh: true }]);

    // DALTON-HOOK(dalton-setup): because cached: true was not marked refreshed, next check still sends refresh=1
    const live = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(live.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);

    // Once a live (cached: false) response lands, provider IS marked refreshed and follow-up reuses it
    const cached = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    expect(cached.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);
    qc.clear();
  });

  it('reuses a first retry refresh=1 that was still in-flight when a second retry started, even if it settles before the second retry reaches provider refresh', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let resolveForced!: (v: AvailableModels) => void;
    let resolveSecondModels!: (v: ModelsResponse) => void;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      calls.push({ refresh: Boolean(opts?.refresh) });
      if (calls.length === 1) {
        return new Promise<AvailableModels>((resolve) => {
          resolveForced = resolve;
        });
      }
      return Promise.resolve(freshReady());
    });

    // Retry #1 starts and sends refresh=1 (still in-flight).
    bumpDaltonTeamModelsRetry(qc);
    const first = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });
    await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));

    // Retry #2 starts while Retry #1's refresh=1 is still in-flight, and its
    // pre-refresh /api/v1/models read is still pending when Retry #1 finishes.
    bumpDaltonTeamModelsRetry(qc);
    qc.removeQueries({ queryKey: queryKeys.models.all, exact: true });
    h.getAvailableModels.mockImplementationOnce(
      () => new Promise<ModelsResponse>((resolve) => {
        resolveSecondModels = resolve;
      }),
    );
    const second = checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
    });

    // Retry #1's refresh=1 settles live while Retry #2 is still awaiting /api/v1/models.
    resolveForced(freshReady());
    const res1 = await first;
    expect(res1.available).toBe(true);

    // Retry #2 now finishes /api/v1/models and proceeds — must NOT send a second refresh=1.
    resolveSecondModels(MODELS_RESPONSE);
    const res2 = await second;
    expect(res2.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }]);
    qc.clear();
  });

  it('reuses a first retry refresh=1 recorded at retry start on the direct forceRefreshProviderAvailableModels path (sharing off)', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let resolveForced!: (v: AvailableModels) => void;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      calls.push({ refresh: Boolean(opts?.refresh) });
      if (calls.length === 1) {
        return new Promise<AvailableModels>((resolve) => {
          resolveForced = resolve;
        });
      }
      return Promise.resolve(freshReady());
    });

    // Retry #1 (sharing off) enters forceRefreshProviderAvailableModels -> refresh=1 is in-flight.
    const gen1 = bumpDaltonTeamModelsRetry(qc);
    const first = forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen1);
    await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));

    // Retry #2 clicks while Retry #1's refresh=1 is still in-flight (before Retry #2 finishes invalidateQueries).
    const gen2 = bumpDaltonTeamModelsRetry(qc);

    // Retry #1's refresh=1 resolves live before Retry #2 reaches forceRefreshProviderAvailableModels.
    resolveForced(freshReady());
    const res1 = await first;
    expect(res1.ready).toContain('gemini-4-neon-high-oauth');

    // Retry #2 finishes invalidateQueries and calls forceRefreshProviderAvailableModels(qc, provider, gen2).
    // Must reuse the refresh=1 that was already in-flight when Retry #2 started, not send a second refresh=1.
    const res2 = await forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen2);
    expect(res2.ready).toContain('gemini-4-neon-high-oauth');
    expect(calls).toEqual([{ refresh: true }]);

    // Retry #3 clicked AFTER Retry #1 already finished must still send a fresh refresh=1.
    const gen3 = bumpDaltonTeamModelsRetry(qc);
    const res3 = await forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen3);
    expect(res3.ready).toContain('gemini-4-neon-high-oauth');
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);
    qc.clear();
  });

  // DALTON-HOOK(dalton-setup): R4 r2 — non-live first-round refresh must not satisfy a second retry (sharing on)
  it.each([
    {
      label: 'cached: true',
      settleFirst: (resolve: (v: AvailableModels) => void) => resolve(staleMissing()),
      secondNonLive: () => Promise.resolve(staleMissing()),
    },
    {
      label: 'thrown error',
      settleFirst: (_resolve: (v: AvailableModels) => void, reject: (err: unknown) => void) =>
        reject(new Error('transient upstream failure')),
      secondNonLive: () => Promise.reject(new Error('still failing')),
    },
    {
      label: 'status: fallback with cached: false',
      settleFirst: (resolve: (v: AvailableModels) => void) => resolve(fallbackFailed()),
      secondNonLive: () => Promise.resolve(fallbackFailed()),
    },
  ])(
    'issues exactly one new refresh=1 on second retry (sharing on) when first retry settles with $label, and keeps provider unrefreshed until live ok',
    async ({ settleFirst, secondNonLive }) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      let resolveFirst!: (v: AvailableModels) => void;
      let rejectFirst!: (err: unknown) => void;
      let resolveSecondModels!: (v: ModelsResponse) => void;
      const calls: Array<{ refresh: boolean }> = [];

      h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
        calls.push({ refresh: Boolean(opts?.refresh) });
        if (calls.length === 1) {
          return new Promise<AvailableModels>((resolve, reject) => {
            resolveFirst = resolve;
            rejectFirst = reject;
          });
        }
        if (calls.length === 2) {
          return secondNonLive();
        }
        return Promise.resolve(freshReady());
      });

      // Retry #1 starts and sends refresh=1 (in-flight).
      bumpDaltonTeamModelsRetry(qc);
      const first = checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
      });
      await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));

      // Retry #2 clicks while Retry #1 is still in-flight, and waits on /api/v1/models.
      const gen2 = bumpDaltonTeamModelsRetry(qc);
      qc.removeQueries({ queryKey: queryKeys.models.all, exact: true });
      h.getAvailableModels.mockImplementationOnce(
        () => new Promise<ModelsResponse>((resolve) => {
          resolveSecondModels = resolve;
        }),
      );
      const second = checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
      });

      // Retry #1 settles with a non-live outcome while Retry #2 is still reading /api/v1/models.
      settleFirst(resolveFirst, rejectFirst);
      await first;

      // Retry #2 proceeds: must NOT accept the non-live first result, and must send exactly ONE new refresh=1 (no loop).
      resolveSecondModels(MODELS_RESPONSE);
      await second;
      expect(calls).toEqual([{ refresh: true }, { refresh: true }]);

      // Because Retry #2's own refresh=1 also returned non-live, provider is STILL not marked refreshed:
      // a subsequent check in generation 2 sends refresh=1 and succeeds live.
      const live = await checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
        retryGeneration: gen2,
      });
      expect(live.available).toBe(true);
      expect(calls).toEqual([{ refresh: true }, { refresh: true }, { refresh: true }]);

      // Once a live ok response lands, provider IS marked refreshed and follow-up reuses cache without refresh=1.
      const followUp = await checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
        retryGeneration: gen2,
      });
      expect(followUp.available).toBe(true);
      expect(calls).toEqual([{ refresh: true }, { refresh: true }, { refresh: true }]);
      qc.clear();
    },
  );

  // DALTON-HOOK(dalton-setup): R4 r2 — non-live first-round refresh must not satisfy a second retry (sharing off)
  it.each([
    {
      label: 'cached: true',
      settleFirst: (resolve: (v: AvailableModels) => void) => resolve(staleMissing()),
      secondNonLive: () => Promise.resolve(staleMissing()),
      expectSecondReject: false,
    },
    {
      label: 'thrown error',
      settleFirst: (_resolve: (v: AvailableModels) => void, reject: (err: unknown) => void) =>
        reject(new Error('transient upstream failure')),
      secondNonLive: () => Promise.reject(new Error('second attempt still failed')),
      expectSecondReject: true,
    },
    {
      label: 'status: fallback with cached: false',
      settleFirst: (resolve: (v: AvailableModels) => void) => resolve(fallbackFailed()),
      secondNonLive: () => Promise.resolve(fallbackFailed()),
      expectSecondReject: false,
    },
  ])(
    'issues exactly one new refresh=1 on second retry (sharing off) when first retry settles with $label, and keeps provider unrefreshed until live ok',
    async ({ settleFirst, secondNonLive, expectSecondReject }) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      let resolveFirst!: (v: AvailableModels) => void;
      let rejectFirst!: (err: unknown) => void;
      const calls: Array<{ refresh: boolean }> = [];

      h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
        calls.push({ refresh: Boolean(opts?.refresh) });
        if (calls.length === 1) {
          return new Promise<AvailableModels>((resolve, reject) => {
            resolveFirst = resolve;
            rejectFirst = reject;
          });
        }
        if (calls.length === 2) {
          return secondNonLive();
        }
        return Promise.resolve(freshReady());
      });

      // Retry #1 (sharing off) starts refresh=1.
      const gen1 = bumpDaltonTeamModelsRetry(qc);
      const first = forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen1).catch((err) => err);
      await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));

      // Retry #2 clicks while Retry #1 is still in-flight.
      const gen2 = bumpDaltonTeamModelsRetry(qc);

      // Retry #1 settles with a non-live result before Retry #2 reaches forceRefreshProviderAvailableModels.
      settleFirst(resolveFirst, rejectFirst);
      await first;

      // Retry #2 runs: must immediately issue exactly ONE new refresh=1 (no loop).
      if (expectSecondReject) {
        await expect(forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen2)).rejects.toThrow(
          'second attempt still failed',
        );
      } else {
        const res2 = await forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen2);
        expect(res2.cached || res2.status === 'fallback').toBe(true);
      }
      expect(calls).toEqual([{ refresh: true }, { refresh: true }]);

      // Provider must NOT be marked refreshed yet until a live ok refresh=1 succeeds.
      const liveCheck = await checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
        retryGeneration: gen2,
      });
      expect(liveCheck.available).toBe(true);
      expect(calls).toEqual([{ refresh: true }, { refresh: true }, { refresh: true }]);

      // Once live ok succeeds, provider IS marked refreshed and follow-up reuses cache without another refresh=1.
      const followUp = await checkDaltonTeamModelsAvailable({
        queryClient: qc,
        providers: PROVIDERS,
        byokEnabled: false,
        providersUnread: false,
        policy: null,
        retryGeneration: gen2,
      });
      expect(followUp.available).toBe(true);
      expect(calls).toEqual([{ refresh: true }, { refresh: true }, { refresh: true }]);
      qc.clear();
    },
  );

  // DALTON-HOOK(dalton-setup): R4 r2 — second retry actively awaiting an in-flight first retry recovers in one refresh=1 when first fails, while superseded generation stays silent
  it('immediately issues one new refresh=1 when an actively awaited in-flight first retry fails, and lets only the latest generation retry when superseded', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let resolveFirst!: (v: AvailableModels) => void;
    const calls: Array<{ refresh: boolean }> = [];

    h.getProviderAvailableModels.mockImplementation((_provider, opts) => {
      calls.push({ refresh: Boolean(opts?.refresh) });
      if (calls.length === 1) {
        return new Promise<AvailableModels>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve(freshReady());
    });

    // Retry #1 starts refresh=1 (in-flight).
    const gen1 = bumpDaltonTeamModelsRetry(qc);
    const first = forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen1);
    await vi.waitFor(() => expect(calls).toEqual([{ refresh: true }]));

    // Retry #2 clicks and begins awaiting Retry #1 while it is still in-flight.
    const gen2 = bumpDaltonTeamModelsRetry(qc);
    const second = forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen2);
    await Promise.resolve();
    expect(calls).toEqual([{ refresh: true }]);

    // Retry #3 supersedes Retry #2 while Retry #1 is still in-flight.
    const gen3 = bumpDaltonTeamModelsRetry(qc);
    const third = forceRefreshProviderAvailableModels(qc, 'antigravity-oauth', gen3);
    await Promise.resolve();
    expect(calls).toEqual([{ refresh: true }]);

    // Retry #1 settles with backend fallback failure shape.
    resolveFirst(fallbackFailed());
    const [res1, res2, res3] = await Promise.all([first, second, third]);
    expect(res1.status).toBe('fallback');
    expect(res2.status).toBe('fallback');
    expect(res3.status).toBe('ok');
    expect(res3.cached).toBe(false);
    // Only the latest generation (gen3) issued a new refresh=1 — exactly one additional call total.
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);

    // Provider is now marked refreshed for gen3; follow-up reuses fresh cache without another refresh=1.
    const followUp = await checkDaltonTeamModelsAvailable({
      queryClient: qc,
      providers: PROVIDERS,
      byokEnabled: false,
      providersUnread: false,
      policy: null,
      retryGeneration: gen3,
    });
    expect(followUp.available).toBe(true);
    expect(calls).toEqual([{ refresh: true }, { refresh: true }]);
    qc.clear();
  });
});

// DALTON-HOOK(standalone-core): Core vs Everflow availability gating in daltonTeamModelsCheck
describe('daltonTeamModelsCheck standalone-core profile support', () => {
  const byokAndGatewayResponse = (daltonProfile: 'core' | 'everflow'): ModelsResponse => ({
    models: {
      openai: {
        display_name: 'OpenAI',
        models: ['gpt-5', 'o1-text-only'],
      },
    },
    model_metadata: {
      'gpt-5': { provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3, tool_calling: true },
      'o1-text-only': { provider: 'openai', access_type: 'api_key', tool_calling: false },
    },
    provider_catalog: [
      {
        provider: 'openai',
        display_name: 'OpenAI',
        access_type: 'api_key',
        brand_key: 'openai',
        byok_eligible: true,
      },
    ],
    system_defaults: {},
    compaction_profiles: {} as ModelsResponse['compaction_profiles'],
    search_providers: {} as ModelsResponse['search_providers'],
    key_candidates: {},
    dalton_profile: daltonProfile,
  });

  const byokProviders = [
    {
      provider: 'openai',
      display_name: 'OpenAI',
      access_type: 'api_key' as const,
      state: 'connected' as const,
      masked_key: 'sk-...1234',
    },
  ];

  it('reports available=true in Core profile for BYOK models when no curated -oauth models exist, and false in Everflow', async () => {
    const qcCore = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    h.getAvailableModels.mockResolvedValueOnce(byokAndGatewayResponse('core'));
    h.getProviderAvailableModels.mockResolvedValueOnce({
      provider: 'openai',
      status: 'ok',
      fetched_at: null,
      ready: ['gpt-5'],
      missing: [],
      context_windows: {},
      others: [],
      total: 1,
      cached: false,
    });

    const coreRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcCore,
      providers: byokProviders,
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(coreRes.available).toBe(true);
    qcCore.clear();

    const qcEverflow = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    h.getAvailableModels.mockResolvedValueOnce(byokAndGatewayResponse('everflow'));
    h.getProviderAvailableModels.mockResolvedValueOnce({
      provider: 'openai',
      status: 'ok',
      fetched_at: null,
      ready: ['gpt-5'],
      missing: [],
      context_windows: {},
      others: [],
      total: 1,
      cached: false,
    });

    const efRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcEverflow,
      providers: byokProviders,
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(efRes.available).toBe(false);
    qcEverflow.clear();
  });

  it('requires verified tool-calling evidence in Core: unverified custom gateway is unavailable until probed, verified is available, text-only is rejected', async () => {
    // 1. Unverified custom gateway (no tool_calling verdict on model or provider) -> not confirmed available.
    const qcUnverified = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    h.getAvailableModels.mockResolvedValueOnce({
      ...byokAndGatewayResponse('core'),
      models: {},
      model_metadata: {},
    });
    h.getPreferences.mockResolvedValueOnce({
      model_preference: {
        custom_providers: [
          { name: 'acme-gateway', parent_provider: 'openai', base_url: 'https://llm.acme.test/v1' },
        ],
        custom_models: [
          { name: 'acme-custom-v1', model_id: 'corp-1', provider: 'acme-gateway' },
        ],
      },
    });
    const unverifiedRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcUnverified,
      providers: [
        {
          provider: 'acme-gateway',
          display_name: 'Acme Gateway',
          access_type: 'api_key' as const,
          is_custom: true,
          state: 'connected' as const,
          masked_key: 'sk-...gw',
        },
      ],
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(unverifiedRes.available).toBe(false);
    qcUnverified.clear();

    // 2. Probed custom gateway (provider carries recorded tool_calling: true) -> available.
    const qcCustom = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    h.getAvailableModels.mockResolvedValueOnce({
      ...byokAndGatewayResponse('core'),
      models: {},
      model_metadata: {},
    });
    h.getPreferences.mockResolvedValueOnce({
      model_preference: {
        custom_providers: [
          { name: 'acme-gateway', parent_provider: 'openai', base_url: 'https://llm.acme.test/v1' },
        ],
        custom_models: [
          { name: 'acme-custom-v1', model_id: 'corp-1', provider: 'acme-gateway' },
        ],
      },
    });

    const customRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcCustom,
      providers: [
        {
          provider: 'acme-gateway',
          display_name: 'Acme Gateway',
          access_type: 'api_key' as const,
          is_custom: true,
          state: 'connected' as const,
          masked_key: 'sk-...gw',
          tool_calling_models: { 'corp-1': true },
        },
      ],
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(customRes.available).toBe(true);
    qcCustom.clear();

    const qcTextOnly = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    h.getAvailableModels.mockResolvedValueOnce({
      ...byokAndGatewayResponse('core'),
      models: { openai: { display_name: 'OpenAI', models: ['o1-text-only'] } },
    });
    h.getProviderAvailableModels.mockResolvedValueOnce({
      provider: 'openai',
      status: 'ok',
      fetched_at: null,
      ready: ['o1-text-only'],
      missing: [],
      context_windows: {},
      others: [],
      total: 1,
      cached: false,
    });

    const textOnlyRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcTextOnly,
      providers: byokProviders,
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(textOnlyRes.available).toBe(false);
    qcTextOnly.clear();
  });
});
