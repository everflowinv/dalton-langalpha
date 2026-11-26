import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import {
  attachCatalogRevisionQueryClient,
  getObservedCatalogRevision,
  isTeamCatalogEnabled,
  observeCatalogModelError,
  observeCatalogRevisionHeader,
  resetCatalogRevisionTracker,
  setTeamCatalogEnabled,
  syncTeamCatalogCapabilityFromResponse,
} from '../catalogRevision';
import { queryKeys } from '@/lib/queryKeys';
import type { ModelsResponse } from '@/api/model';

const h = vi.hoisted(() => ({
  getAvailableModels: vi.fn<() => Promise<ModelsResponse>>(),
}));

vi.mock('@/api/model', () => ({
  getAvailableModels: h.getAvailableModels,
}));

import { useModels } from '@/hooks/useModels';

const SAMPLE_MODELS: ModelsResponse = {
  models: { openai: { display_name: 'OpenAI', models: ['gpt-5'] } },
  model_metadata: { 'gpt-5': { provider: 'openai', display_name: 'GPT-5' } },
  system_defaults: {},
  compaction_profiles: {} as ModelsResponse['compaction_profiles'],
  search_providers: {} as ModelsResponse['search_providers'],
  provider_catalog: [],
  key_candidates: {},
  team_catalog_enabled: true,
  catalog_revision: 3,
};

const createdClients: QueryClient[] = [];

function makeClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  createdClients.push(qc);
  return qc;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetCatalogRevisionTracker({ clearClients: true });
  h.getAvailableModels.mockResolvedValue(SAMPLE_MODELS);
});

afterEach(() => {
  while (createdClients.length > 0) {
    createdClients.pop()?.clear();
  }
  resetCatalogRevisionTracker({ clearClients: true });
});

describe('catalogRevision tracker & useModels active refresh', () => {
  it('stays inert when team_catalog_enabled is false and activates once enabled', () => {
    const qc = makeClient();
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const detach = attachCatalogRevisionQueryClient(qc);

    expect(isTeamCatalogEnabled()).toBe(false);
    // Headers and errors are ignored until the server explicitly enables team catalog
    expect(observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '3' }, { url: '/api/v1/users/me' })).toBe(false);
    expect(getObservedCatalogRevision()).toBeNull();
    expect(observeCatalogModelError(400, { type: 'model_removed' })).toBe(false);
    expect(invalidateSpy).not.toHaveBeenCalled();

    // Syncing /api/v1/models with team_catalog_enabled: true activates tracking
    syncTeamCatalogCapabilityFromResponse('/api/v1/models', 200, { team_catalog_enabled: true, catalog_revision: 3 });
    expect(isTeamCatalogEnabled()).toBe(true);

    expect(observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '3' }, { url: '/api/v1/users/me' })).toBe(false);
    expect(getObservedCatalogRevision()).toBe(3);
    expect(invalidateSpy).not.toHaveBeenCalled();

    // Same or older revision does not invalidate
    expect(observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '3' }, { url: '/api/v1/users/me' })).toBe(false);
    expect(observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '2' }, { url: '/api/v1/users/me' })).toBe(false);
    expect(invalidateSpy).not.toHaveBeenCalled();

    // Higher revision (including Headers instance from SSE stream) triggers immediate invalidation
    expect(observeCatalogRevisionHeader(new Headers({ 'X-Dalton-Catalog-Revision': '4' }), { url: '/api/v1/threads/t1/messages' })).toBe(true);
    expect(getObservedCatalogRevision()).toBe(4);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.models.all });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.providerModels.all });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.dalton.teamModelCatalog() });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: [...queryKeys.dalton.all, 'team-models-available'],
    });

    detach();
  });

  it('ref-counts QueryClient attachments so unmounting one hook does not drop the app client', () => {
    const qc = makeClient();
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const detachGlobal = attachCatalogRevisionQueryClient(qc);
    const detachHook = attachCatalogRevisionQueryClient(qc);
    setTeamCatalogEnabled(true);

    observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '1' });
    detachHook();

    observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '2' }, { url: '/api/v1/threads' });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.models.all });

    invalidateSpy.mockClear();
    detachGlobal();
    observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '3' }, { url: '/api/v1/threads' });
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it('invalidates models on 400 model_removed, model_retired, and catalog_route_changed, but ignores upstream 400 model_unavailable', () => {
    const qc = makeClient();
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const detach = attachCatalogRevisionQueryClient(qc);
    setTeamCatalogEnabled(true);

    expect(observeCatalogModelError(400, { type: 'model_removed' })).toBe(true);
    expect(observeCatalogModelError(400, { code: 'model_retired' })).toBe(true);
    expect(observeCatalogModelError(400, { code: 'catalog_route_changed' })).toBe(true);
    expect(invalidateSpy).toHaveBeenCalledTimes(12);

    invalidateSpy.mockClear();
    // Upstream 400 model_unavailable must NOT trigger catalog invalidation in generic REST client
    expect(observeCatalogModelError(400, { type: 'model_unavailable' })).toBe(false);
    expect(observeCatalogModelError(400, { code: 'model_unavailable' })).toBe(false);
    expect(observeCatalogModelError(400, { code: 'other_error' })).toBe(false);
    expect(observeCatalogModelError(500, { code: 'model_removed' })).toBe(false);
    expect(invalidateSpy).not.toHaveBeenCalled();
    detach();
  });

  it('useModels enables 5-minute foreground refetchInterval only when team_catalog_enabled is true and reacts to higher revision header', async () => {
    const qc = makeClient();
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children);

    const { result, unmount } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(result.current.models?.catalog_revision).toBe(3));

    const query = qc.getQueryCache().find({ queryKey: queryKeys.models.all });
    const opts = query?.options as {
      staleTime?: number;
      refetchInterval?: number | false | ((q: { state: { data?: ModelsResponse } }) => number | false);
      refetchIntervalInBackground?: boolean;
      refetchOnWindowFocus?: boolean;
    };
    expect(opts.staleTime).toBe(5 * 60_000);
    expect(typeof opts.refetchInterval).toBe('function');
    if (typeof opts.refetchInterval === 'function') {
      expect(opts.refetchInterval({ state: { data: undefined } })).toBe(false);
      // Numeric catalog_revision WITHOUT team_catalog_enabled: true must NOT enable polling
      expect(opts.refetchInterval({ state: { data: { ...SAMPLE_MODELS, team_catalog_enabled: false } } })).toBe(false);
      const { team_catalog_enabled: _omit, ...withoutFlag } = SAMPLE_MODELS;
      expect(opts.refetchInterval({ state: { data: withoutFlag } })).toBe(false);
      expect(opts.refetchInterval({ state: { data: SAMPLE_MODELS } })).toBe(5 * 60_000);
    }
    expect(opts.refetchIntervalInBackground).toBe(false);
    expect(opts.refetchOnWindowFocus).toBe(true);

    // Simulate a higher catalog revision arriving on another API response
    h.getAvailableModels.mockResolvedValueOnce({ ...SAMPLE_MODELS, catalog_revision: 4 });
    observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '3' }, { url: '/api/v1/models' });
    observeCatalogRevisionHeader({ 'x-dalton-catalog-revision': '4' }, { url: '/api/v1/users/me' });

    await waitFor(() => expect(result.current.models?.catalog_revision).toBe(4));
    unmount();
  });

  it('useModels does not schedule an infinite timer under vi.runAllTimers when team_catalog_enabled is absent', () => {
    vi.useFakeTimers();
    try {
      const { team_catalog_enabled: _omit, ...upstreamOnly } = SAMPLE_MODELS;
      h.getAvailableModels.mockResolvedValue(upstreamOnly);
      const qc = makeClient();
      const wrapper = ({ children }: { children: React.ReactNode }) =>
        React.createElement(QueryClientProvider, { client: qc }, children);

      const { unmount } = renderHook(() => useModels(), { wrapper });
      expect(() => vi.runAllTimers()).not.toThrow();
      unmount();
    } finally {
      vi.useRealTimers();
    }
  });
});
