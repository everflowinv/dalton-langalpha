import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AntigravitySeriesEntry } from '@/api/model';
import { buildSelectableAntigravitySeries, buildSeriesCatalog } from '@/lib/antigravitySeries';

const h = vi.hoisted(() => {
  const seriesCatalogList: AntigravitySeriesEntry[] = [
    {
      series_id: 'antigravity-oauth:gemini-4-neon',
      series_prefix: 'gemini-4-neon',
      display_name: 'Gemini 4 Neon',
      qualified_display_name: 'Gemini 4 Neon (Antigravity)',
      provider: 'antigravity-oauth',
      default_effort: 'high',
      default_model: 'gemini-4-neon-high-oauth',
      efforts: ['low', 'medium', 'high', 'xhigh'],
      variants: {
        low: 'gemini-4-neon-low-oauth',
        medium: 'gemini-4-neon-medium-oauth',
        high: 'gemini-4-neon-high-oauth',
        xhigh: 'gemini-4-neon-xhigh-oauth',
      },
    },
    {
      series_id: 'antigravity-oauth:gemini-4-carbon',
      series_prefix: 'gemini-4-carbon',
      display_name: 'Gemini 4 Carbon',
      qualified_display_name: 'Gemini 4 Carbon (Antigravity)',
      provider: 'antigravity-oauth',
      default_effort: 'high',
      default_model: 'gemini-4-carbon-high-oauth',
      efforts: ['medium', 'high'],
      variants: {
        medium: 'gemini-4-carbon-medium-oauth',
        high: 'gemini-4-carbon-high-oauth',
      },
    },
    {
      series_id: 'antigravity-oauth:gemini-4-hydrogen',
      series_prefix: 'gemini-4-hydrogen',
      display_name: 'Gemini 4 Hydrogen',
      qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
      provider: 'antigravity-oauth',
      default_effort: 'high',
      default_model: 'gemini-4-hydrogen-high-oauth',
      efforts: ['low', 'medium', 'high'],
      variants: {
        low: 'gemini-4-hydrogen-low-oauth',
        medium: 'gemini-4-hydrogen-medium-oauth',
        high: 'gemini-4-hydrogen-high-oauth',
      },
    },
    {
      series_id: 'antigravity-oauth:claude-opus-5-5',
      series_prefix: 'claude-opus-5-5',
      display_name: 'Claude Opus 5.5',
      qualified_display_name: 'Claude Opus 5.5 (Antigravity)',
      provider: 'antigravity-oauth',
      default_effort: 'high',
      default_model: 'claude-opus-5-5-high-oauth',
      efforts: ['medium', 'high'],
      variants: {
        medium: 'claude-opus-5-5-medium-oauth',
        high: 'claude-opus-5-5-high-oauth',
      },
    },
  ];
  const antigravityModels = [
    'gemini-4-neon-low-oauth',
    'gemini-4-neon-medium-oauth',
    'gemini-4-neon-high-oauth',
    'gemini-4-neon-xhigh-oauth',
    'gemini-4-carbon-medium-oauth',
    'gemini-4-carbon-high-oauth',
    'gemini-4-hydrogen-low-oauth',
    'gemini-4-hydrogen-medium-oauth',
    'gemini-4-hydrogen-high-oauth',
    'claude-opus-5-5-medium-oauth',
    'claude-opus-5-5-high-oauth',
  ];
  const metadata: Record<string, { display_name: string; provider: string; intelligence: number; speed: number; plain_language?: number; reasoning_efforts: string[] }> = {
    'gemini-4-neon-low-oauth': { display_name: 'Gemini 4 Neon (Low)', provider: 'antigravity-oauth', intelligence: 4, speed: 4, plain_language: 5, reasoning_efforts: ['low'] },
    'gemini-4-neon-medium-oauth': { display_name: 'Gemini 4 Neon (Medium)', provider: 'antigravity-oauth', intelligence: 4, speed: 4, plain_language: 5, reasoning_efforts: ['medium'] },
    'gemini-4-neon-high-oauth': { display_name: 'Gemini 4 Neon (High)', provider: 'antigravity-oauth', intelligence: 4, speed: 4, plain_language: 5, reasoning_efforts: ['high'] },
    'gemini-4-neon-xhigh-oauth': { display_name: 'Gemini 4 Neon (XHigh)', provider: 'antigravity-oauth', intelligence: 4, speed: 4, plain_language: 5, reasoning_efforts: ['xhigh'] },
    'gemini-4-carbon-medium-oauth': { display_name: 'Gemini 4 Carbon (Medium)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, plain_language: 5, reasoning_efforts: ['medium'] },
    'gemini-4-carbon-high-oauth': { display_name: 'Gemini 4 Carbon (High)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, plain_language: 5, reasoning_efforts: ['high'] },
    'gemini-4-hydrogen-low-oauth': { display_name: 'Gemini 4 Hydrogen (Low)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['low'] },
    'gemini-4-hydrogen-medium-oauth': { display_name: 'Gemini 4 Hydrogen (Medium)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['medium'] },
    'gemini-4-hydrogen-high-oauth': { display_name: 'Gemini 4 Hydrogen (High)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['high'] },
    'claude-opus-5-5-medium-oauth': { display_name: 'Claude Opus 5.5 (Medium)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, reasoning_efforts: ['medium'] },
    'claude-opus-5-5-high-oauth': { display_name: 'Claude Opus 5.5 (High)', provider: 'antigravity-oauth', intelligence: 5, speed: 2, reasoning_efforts: ['high'] },
  };
  return {
    seriesCatalogList,
    antigravityModels,
    metadata,
    mutateAsync: vi.fn(async () => ({})),
    finish: vi.fn(async () => true),
    toWelcome: vi.fn(),
    home: vi.fn(async () => ({ workspace_id: 'home' })),
    preferences: {} as object,
    systemDefaults: null as null | { fallback_models?: string[] },
    payers: {} as Record<string, string>,
    cannotRun: [] as string[],
  };
});

vi.mock('@/config/brand', () => ({
  isDaltonBuild: true,
  DALTON_PROFILE: 'everflow' as const,
  isCoreProfileBuild: false,
  BRAND_NAME: 'Everflow',
  BRAND_MARK: 'everflow',
  APP_TAGLINE: '',
  isBetaBuild: false,
}));
vi.mock('@/pages/Onboarding/welcome/Welcome', () => ({ default: () => null }));
vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () => ({ queryKey: ['home'], queryFn: h.home }),
}));
vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: true,
    canDisconnect: false,
    canManage: false,
    isPm: false,
    canInvite: false,
    callerTier: 'external',
    callerTierPending: false,
    callerTierError: false,
  }),
}));
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => {
    const models = { 'antigravity-oauth': { models: h.antigravityModels } };
    const seriesCatalog = buildSeriesCatalog(h.seriesCatalogList);
    const selectableSeries = buildSelectableAntigravitySeries(seriesCatalog, models, {
      rawMetadata: h.metadata,
      customModels: [],
    });
    return {
      models,
      slotModels: models,
      payers: h.payers,
      metadata: h.metadata,
      customModels: [],
      seriesCatalog,
      selectableSeries,
      selectableSlotSeries: selectableSeries,
      rawApiResponse: {
        model_metadata: h.metadata,
        antigravity_series: h.seriesCatalogList,
      },
      cannotRun: (m: string) => h.cannotRun.includes(m),
      systemDefaults: h.systemDefaults,
      isLoading: false,
      unread: false,
    };
  },
}));
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoaded: true, unread: false, retry: vi.fn() }),
}));
vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: h.mutateAsync, isPending: false, isError: false }),
}));
vi.mock('@/hooks/useConfiguredProviders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useConfiguredProviders')>()),
  SERVER_KEY: '__server_key__',
  useConfiguredProviders: () => ({ providers: [], byokEnabled: true }),
}));
vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ user: { user_id: 'u-ext' } }) }));
vi.mock('@/hooks/useAccount', () => ({ useAccount: () => ({ policy: null }) }));
vi.mock('@/hooks/useProviderCatalog', () => ({ useProviderCatalog: () => ({ connectionOf: (p: string) => p }) }));
vi.mock('@/config/hostMode', () => ({ isPlatformMode: false, HOST_MODE: 'oss', APP_ENTRY_PATH: '/app' }));
vi.mock('../../answers/flow', () => ({
  useAnswerFlow: () => ({
    isLoading: false,
    answers: { models: 'subscription_or_key', runsOn: 'cloud' },
    first: false,
    tokenTarget: null,
    next: 'default',
    progress: null,
    finish: h.finish,
    retryFacts: vi.fn(),
    toWelcome: h.toWelcome,
    finishing: false,
    finishFailed: false,
    back: null,
  }),
}));

import DefaultStep from '../DefaultStep';

describe('DefaultStep Antigravity series onboarding', () => {
  const client = new QueryClient();

  beforeEach(() => {
    h.mutateAsync = vi.fn(async () => ({}));
    h.finish = vi.fn(async () => true);
    h.toWelcome = vi.fn();
    h.preferences = {};
    h.systemDefaults = null;
    h.payers = {};
    h.cannotRun = [];
    client.clear();
  });

  it('shows pure curated series names with zero effort pickers and saves neon-high + hydrogen-low', async () => {
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-medium-oauth',
    ]);
    expect(screen.getAllByText('Gemini 4 Neon').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Gemini 4 Carbon').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Claude Opus 5.5').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Gemini 4 Hydrogen').length).toBeGreaterThanOrEqual(1);

    // Zero effort suffixes or effort controls on onboarding.
    expect(screen.queryByText(/Gemini 4 Neon \(/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Gemini 4 Carbon \(/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Claude Opus 5\.5 \(/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Gemini 4 Hydrogen \(/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/thinking effort|reasoning effort/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Thinking effort|Reasoning effort/i)).not.toBeInTheDocument();

    // Open Customize and confirm no effort selectors appear there either.
    await userEvent.click(screen.getByRole('button', { name: /Customize/i }));
    expect(screen.queryByLabelText(/thinking effort|reasoning effort/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Thinking effort|Reasoning effort/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));

    const savedArg = (h.mutateAsync.mock.calls[0] as unknown as [{ model_preference: Record<string, unknown> }])[0];
    expect(savedArg.model_preference).toMatchObject({
      preferred_model: 'gemini-4-neon-high-oauth',
      preferred_background_model: 'gemini-4-hydrogen-low-oauth',
      background_follows: null,
    });
  });

  it('picking Hydrogen in the onboarding Background dropdown saves gemini-4-hydrogen-low-oauth with no effort picker or "Low" label', async () => {
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /Customize/i }));
    const bgSelect = screen.getByLabelText(/Background model/i) as HTMLSelectElement;

    // Switch away to Neon then pick Hydrogen so Hydrogen uses its fixed-low option value
    await userEvent.selectOptions(bgSelect, 'gemini-4-neon-low-oauth');
    const hydrogenOption = [...bgSelect.options].find((o) => o.textContent === 'Gemini 4 Hydrogen');
    expect(hydrogenOption).toBeDefined();
    expect(hydrogenOption?.value).toBe('gemini-4-hydrogen-low-oauth');
    await userEvent.selectOptions(bgSelect, 'gemini-4-hydrogen-low-oauth');

    expect(screen.queryByLabelText(/thinking effort|reasoning effort|effort/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\bLow\b/);

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));

    const savedArg = (h.mutateAsync.mock.calls[0] as unknown as [{ model_preference: Record<string, unknown> }])[0];
    expect(savedArg.model_preference).toMatchObject({
      preferred_background_model: 'gemini-4-hydrogen-low-oauth',
      background_follows: null,
    });
  });

  it('renders plain_language as the third rating when present and omits it when absent', () => {
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    const neonCard = radios.find((r) => r.value === 'gemini-4-neon-high-oauth')?.closest('label');
    const opusCard = radios.find((r) => r.value === 'claude-opus-5-5-medium-oauth')?.closest('label');
    expect(neonCard).not.toBeNull();
    expect(opusCard).not.toBeNull();

    // Neon carries plain_language: 5 -> shows Intelligence, Speed, Plain language in order.
    const neonImgs = Array.from(neonCard!.querySelectorAll('[role="img"]')).map((el) =>
      el.getAttribute('aria-label'),
    );
    expect(neonImgs).toEqual([
      'Intelligence: 4 of 5',
      'Speed: 4 of 5',
      'Plain language: 5 of 5',
    ]);

    // Opus in this fixture omits plain_language -> shows only Intelligence and Speed.
    const opusImgs = Array.from(opusCard!.querySelectorAll('[role="img"]')).map((el) =>
      el.getAttribute('aria-label'),
    );
    expect(opusImgs).toEqual([
      'Intelligence: 5 of 5',
      'Speed: 3 of 5',
    ]);
  });

  it('omits fallback_models on Continue when unset and untouched even if deployment defaults are filtered', async () => {
    h.preferences = {};
    h.systemDefaults = {
      fallback_models: ['gemini-4-neon-high-oauth', 'grok-4.7-oauth', 'deepseek-flash'],
    };
    h.cannotRun = ['grok-4.7-oauth', 'deepseek-flash'];

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));

    const savedArg = (h.mutateAsync.mock.calls[0] as unknown as [{ model_preference: Record<string, unknown> }])[0];
    expect(savedArg.model_preference).not.toHaveProperty('fallback_models');
  });

  it('saves fallback_models when an unset user explicitly edits fallback models in Customize', async () => {
    h.preferences = {};
    h.systemDefaults = {
      fallback_models: ['gemini-4-neon-high-oauth', 'gemini-4-carbon-medium-oauth'],
    };

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /Customize/i }));
    await userEvent.click(screen.getByRole('button', { name: /Remove Gemini 4 Carbon · Medium/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));

    const savedArg = (h.mutateAsync.mock.calls[0] as unknown as [{ model_preference: Record<string, unknown> }])[0];
    expect(savedArg.model_preference.fallback_models).toEqual(['gemini-4-neon-high-oauth']);
  });

  it('preserves existing behavior for already-saved fallback_models when a saved provider is filtered', async () => {
    h.preferences = {
      model_preference: {
        preferred_model: 'gemini-4-neon-high-oauth',
        fallback_models: ['gemini-4-neon-high-oauth', 'grok-4.7-oauth'],
      },
    };
    h.cannotRun = ['grok-4.7-oauth'];

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/setup/default']}>
          <Routes>
            <Route path="/setup/default" element={<DefaultStep />} />
            <Route path="*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));

    const savedArg = (h.mutateAsync.mock.calls[0] as unknown as [{ model_preference: Record<string, unknown> }])[0];
    expect(savedArg.model_preference.fallback_models).toEqual(['gemini-4-neon-high-oauth']);
  });
});
