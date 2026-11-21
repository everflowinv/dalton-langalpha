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
  getPreferences: vi.fn(async () => ({})),
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
});
