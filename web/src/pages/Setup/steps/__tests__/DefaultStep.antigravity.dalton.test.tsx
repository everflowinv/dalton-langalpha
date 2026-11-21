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
      series_id: 'gemini-4-neon',
      series_prefix: 'gemini-4-neon-',
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
      series_id: 'gemini-4-hydrogen',
      series_prefix: 'gemini-4-hydrogen-',
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
  ];
  const antigravityModels = [
    'gemini-4-neon-low-oauth',
    'gemini-4-neon-medium-oauth',
    'gemini-4-neon-high-oauth',
    'gemini-4-neon-xhigh-oauth',
    'gemini-4-hydrogen-low-oauth',
    'gemini-4-hydrogen-medium-oauth',
    'gemini-4-hydrogen-high-oauth',
  ];
  const metadata = {
    'gemini-4-neon-low-oauth': { display_name: 'Gemini 4 Neon (Low)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, reasoning_efforts: ['low'] },
    'gemini-4-neon-medium-oauth': { display_name: 'Gemini 4 Neon (Medium)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, reasoning_efforts: ['medium'] },
    'gemini-4-neon-high-oauth': { display_name: 'Gemini 4 Neon (High)', provider: 'antigravity-oauth', intelligence: 5, speed: 3, reasoning_efforts: ['high'] },
    'gemini-4-neon-xhigh-oauth': { display_name: 'Gemini 4 Neon (XHigh)', provider: 'antigravity-oauth', intelligence: 5, speed: 2, reasoning_efforts: ['xhigh'] },
    'gemini-4-hydrogen-low-oauth': { display_name: 'Gemini 4 Hydrogen (Low)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['low'] },
    'gemini-4-hydrogen-medium-oauth': { display_name: 'Gemini 4 Hydrogen (Medium)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['medium'] },
    'gemini-4-hydrogen-high-oauth': { display_name: 'Gemini 4 Hydrogen (High)', provider: 'antigravity-oauth', intelligence: 4, speed: 5, reasoning_efforts: ['high'] },
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
  };
});

vi.mock('@/config/brand', () => ({
  isDaltonBuild: true,
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
      payers: {},
      metadata: h.metadata,
      customModels: [],
      seriesCatalog,
      selectableSeries,
      selectableSlotSeries: selectableSeries,
      rawApiResponse: {
        model_metadata: h.metadata,
        antigravity_series: h.seriesCatalogList,
      },
      cannotRun: () => false,
      systemDefaults: null,
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
    client.clear();
  });

  it('shows pure Gemini 4 Neon / Hydrogen series names with zero effort pickers and saves high default IDs', async () => {
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

    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(2);
    expect(screen.getAllByText('Gemini 4 Neon').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Gemini 4 Hydrogen').length).toBeGreaterThanOrEqual(1);

    // Zero effort suffixes or effort controls on onboarding.
    expect(screen.queryByText(/Gemini 4 Neon \(/)).not.toBeInTheDocument();
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
      preferred_background_model: 'gemini-4-hydrogen-high-oauth',
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
});
