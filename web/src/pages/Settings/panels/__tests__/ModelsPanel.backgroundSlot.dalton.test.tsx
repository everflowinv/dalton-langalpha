import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AntigravitySeriesEntry } from '@/api/model';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { buildSelectableAntigravitySeries, buildSeriesCatalog } from '@/lib/antigravitySeries';

const SERIES_LIST: AntigravitySeriesEntry[] = [
  {
    series_id: 'antigravity-oauth:gemini-4-neon',
    series_prefix: 'gemini-4-neon',
    provider: 'antigravity-oauth',
    display_name: 'Gemini 4 Neon',
    qualified_display_name: 'Gemini 4 Neon (Antigravity)',
    default_effort: 'high',
    default_model: 'gemini-4-neon-high-oauth',
    efforts: ['low', 'medium', 'high'],
    variants: {
      low: 'gemini-4-neon-low-oauth',
      medium: 'gemini-4-neon-medium-oauth',
      high: 'gemini-4-neon-high-oauth',
    },
  },
  {
    series_id: 'antigravity-oauth:gemini-4-hydrogen',
    series_prefix: 'gemini-4-hydrogen',
    provider: 'antigravity-oauth',
    display_name: 'Gemini 4 Hydrogen',
    qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
    default_effort: 'high',
    default_model: 'gemini-4-hydrogen-high-oauth',
    efforts: ['low', 'medium', 'high'],
    variants: {
      low: 'gemini-4-hydrogen-low-oauth',
      medium: 'gemini-4-hydrogen-medium-oauth',
      high: 'gemini-4-hydrogen-high-oauth',
    },
  },
];

const RAW_META = {
  'gemini-4-neon-low-oauth': { display_name: 'Gemini 4 Neon (Low)', provider: 'antigravity-oauth' },
  'gemini-4-neon-medium-oauth': { display_name: 'Gemini 4 Neon (Medium)', provider: 'antigravity-oauth' },
  'gemini-4-neon-high-oauth': { display_name: 'Gemini 4 Neon (High)', provider: 'antigravity-oauth' },
  'gemini-4-hydrogen-low-oauth': { display_name: 'Gemini 4 Hydrogen (Low)', provider: 'antigravity-oauth' },
  'gemini-4-hydrogen-medium-oauth': { display_name: 'Gemini 4 Hydrogen (Medium)', provider: 'antigravity-oauth' },
  'gemini-4-hydrogen-high-oauth': { display_name: 'Gemini 4 Hydrogen (High)', provider: 'antigravity-oauth' },
};

const AG_MODELS = {
  'antigravity-oauth': {
    display_name: 'Google Antigravity',
    models: Object.keys(RAW_META),
  },
};

const h = vi.hoisted(() => ({
  preferences: { model_preference: {}, other_preference: {} } as Record<string, unknown>,
  mutate: vi.fn((_payload: unknown) => undefined),
  mutateAsync: vi.fn(async (_payload: unknown) => ({})),
  models: {} as Record<string, ProviderModelsData>,
  metadata: {} as Record<string, ModelMetadataEntry>,
  customModels: [] as CustomModelEntry[],
  favorites: [] as string[],
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  removeManyFavorites: vi.fn(),
  systemDefaults: {
    default_model: 'gemini-4-neon-high-oauth',
    background_model: 'gemini-4-hydrogen-low-oauth',
    fallback_models: [] as string[],
  },
}));

vi.mock('@/config/hostMode', () => ({ isPlatformMode: false }));
vi.mock('@/config/daltonAuth', () => ({ isDaltonAuth: false }));
vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({
    isShared: false,
    canDisconnect: true,
    canManage: true,
    isPm: true,
    canInvite: false,
    callerTier: null,
    callerTierPending: false,
    callerTierError: false,
  }),
}));
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoading: false, isLoaded: true }),
}));
vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({ mutate: h.mutate, mutateAsync: h.mutateAsync, isPending: false }),
}));
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => {
    const seriesCatalog = buildSeriesCatalog(SERIES_LIST);
    const selectableSeries = buildSelectableAntigravitySeries(seriesCatalog, h.models, {
      rawMetadata: RAW_META,
      customModels: h.customModels,
    });
    const allNames = Object.values(h.models).flatMap((p) => p.models ?? []);
    return {
      models: h.models,
      slotModels: h.models,
      metadata: h.metadata,
      payers: {},
      systemDefaults: h.systemDefaults,
      customModels: h.customModels,
      offPlanModels: new Set<string>(),
      platform: null,
      validModelNames: new Set(allNames),
      catalogModelNames: new Set(allNames),
      rawModels: h.models,
      rawApiResponse: { model_metadata: RAW_META, antigravity_series: SERIES_LIST },
      compactionProfiles: null,
      searchProviders: null,
      isLoading: false,
      unread: false,
      seriesCatalog,
      selectableSeries,
      selectableSlotSeries: selectableSeries,
    };
  },
}));
vi.mock('@/hooks/useAccount', () => ({ useAccount: () => ({ policy: null }) }));
vi.mock('@/hooks/useFavoriteModels', () => ({
  useFavoriteModels: () => ({
    models: h.favorites,
    add: h.addFavorite,
    remove: h.removeFavorite,
    removeMany: h.removeManyFavorites,
  }),
}));
vi.mock('@/hooks/usePayerLabel', () => ({ usePayerLabel: () => () => '' }));
vi.mock('@/hooks/useClaudeCodeSteps', () => ({ useClaudeCodeSteps: () => ({ status: 'done' }) }));
vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn(), useToast: () => ({ toast: vi.fn() }) }));
vi.mock('../ModelAccessSection', () => ({ ModelAccessSection: () => null }));
vi.mock('@/components/model/PayerOrder', () => ({ PayerOrder: () => null }));

import { ModelsPanel } from '../ModelsPanel';

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings?tab=model']}>
        <ModelsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ModelsPanel — Background slot fixed-low Antigravity behavior', () => {
  beforeEach(() => {
    localStorage.removeItem('settings:modelMode');
    h.preferences = { model_preference: {}, other_preference: {} };
    h.models = AG_MODELS;
    h.metadata = RAW_META;
    h.customModels = [];
    h.favorites = [];
    h.addFavorite.mockReset();
    h.removeFavorite.mockReset();
    h.removeManyFavorites.mockReset();
    h.mutate.mockClear();
    h.mutateAsync.mockClear();
    h.mutateAsync.mockResolvedValue({});
  });

  it('picks gemini-4-hydrogen-low-oauth for Background slot with no effort selector and no "Low" label', async () => {
    renderPanel();

    const bgSelect = screen.getByLabelText('Background model') as HTMLSelectElement;
    // Auto background placeholder also omits effort suffix
    expect(bgSelect.options[0].textContent).toBe('Auto (Gemini 4 Hydrogen)');
    expect(bgSelect.options[0].textContent).not.toContain('Low');

    const hydrogenOpt = [...bgSelect.options].find((o) => o.textContent === 'Gemini 4 Hydrogen');
    expect(hydrogenOpt).toBeDefined();
    expect(hydrogenOpt?.value).toBe('gemini-4-hydrogen-low-oauth');

    fireEvent.change(bgSelect, { target: { value: 'gemini-4-hydrogen-low-oauth' } });

    // No effort selector for Background model, and no "Low" anywhere in the rendered page
    expect(screen.queryByLabelText(/Background model.*Effort/i)).toBeNull();
    expect(document.body.textContent).not.toMatch(/\bLow\b/);

    // Confirm default model question if shown, or wait for direct save
    const confirmBtn = screen.queryByRole('button', { name: 'Make default' });
    if (confirmBtn) fireEvent.click(confirmBtn);

    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(h.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        model_preference: expect.objectContaining({
          preferred_background_model: 'gemini-4-hydrogen-low-oauth',
        }),
      }),
    );
  });

  it('displays an already-saved non-low Background variant as pure series name without rewriting it, while Primary still offers effort selection', () => {
    h.preferences = {
      model_preference: {
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-high-oauth',
      },
      other_preference: {},
    };
    renderPanel();

    const bgSelect = screen.getByLabelText('Background model') as HTMLSelectElement;
    expect(bgSelect.value).toBe('gemini-4-hydrogen-high-oauth');
    expect(bgSelect.selectedOptions[0].textContent).toBe('Gemini 4 Hydrogen');
    expect(screen.queryByLabelText(/Background model.*Effort/i)).toBeNull();
    expect(h.mutate).not.toHaveBeenCalled();
    expect(h.mutateAsync).not.toHaveBeenCalled();

    // Primary (Default model) slot is unaffected and keeps its companion effort picker
    const primaryEffort = screen.getByLabelText(/Default model.*Effort/i) as HTMLSelectElement;
    expect(primaryEffort.value).toBe('high');
  });

  it('excludes same-name custom models from Quick Access series favorites and preserves them when unstarring the series', () => {
    h.customModels = [{ name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom-neon' }];
    h.models = {
      'antigravity-oauth': {
        display_name: 'Google Antigravity',
        models: ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth'],
      },
      openai: {
        display_name: 'OpenAI',
        models: ['gemini-4-neon-high-oauth'],
      },
    };
    h.metadata = {
      ...RAW_META,
      'gemini-4-neon-high-oauth': { provider: 'openai', is_custom_model: true, display_name: 'Custom Neon' },
    };

    // Case 1: Only the same-name custom model is in favorites -> clicking Gemini 4 Neon adds a built-in variant instead of removing custom
    h.favorites = ['gemini-4-neon-high-oauth'];
    const { unmount } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: '+ Add' }));
    fireEvent.click(screen.getByRole('button', { name: 'Gemini 4 Neon' }));
    expect(h.addFavorite).toHaveBeenCalledWith('gemini-4-neon-medium-oauth');
    expect(h.removeManyFavorites).not.toHaveBeenCalled();
    unmount();

    // Case 2: Built-in Neon low + medium AND same-name custom model are favorited ->
    // clicking Gemini 4 Neon removes ONLY the built-in variants and leaves the custom model favorite untouched
    h.addFavorite.mockReset();
    h.removeManyFavorites.mockReset();
    h.favorites = ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth', 'gemini-4-neon-high-oauth'];
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: '+ Add' }));
    fireEvent.click(screen.getByRole('button', { name: 'Gemini 4 Neon' }));
    expect(h.removeManyFavorites).toHaveBeenCalledTimes(1);
    expect(h.removeManyFavorites).toHaveBeenCalledWith(['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth']);
  });
});
