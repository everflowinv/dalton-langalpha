// DALTON-HOOK(team-model-catalog): verify ModelStep holds both members and admins on /setup/model when Everflow curated setup is empty, while Core uses fallback models
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const h = vi.hoisted(() => ({
  models: 'langalpha_models' as 'langalpha_models' | 'subscription_or_key',
  team: {
    shared: true,
    available: false,
    empty: true,
    isLoading: false,
    unread: false,
    canManage: false,
    callerTierPending: false,
    rejectedProviders: new Set<string>(),
    daltonProfile: 'everflow' as 'everflow' | 'core',
    daltonSetupCandidates: [] as string[] | undefined,
    daltonCuratedProviders: [] as string[] | undefined,
    retry: vi.fn(),
  },
}));

vi.mock('@/config/brand', () => ({
  isDaltonBuild: true,
  isCoreProfileBuild: false,
  DALTON_PROFILE: 'everflow',
  BRAND_NAME: 'Everflow',
  BRAND_MARK: 'everflow',
  faviconHref: () => '/everflow/favicon.svg',
}));

vi.mock('../../answers/useTeamModels', () => ({
  useDaltonTeamModels: () => h.team,
}));

vi.mock('../../answers/flow', () => ({
  useAnswerFlow: () => ({
    isLoading: false,
    answers: { models: h.models, runsOn: 'cloud' },
    first: false,
    tokenTarget: null,
    facts: { providerConnected: false },
    finish: vi.fn(),
    finishing: false,
    finishFailed: false,
    back: null,
  }),
  useChangeAnswers: () => vi.fn(),
}));

vi.mock('../../answers/connectedDefault', () => ({
  useSaveConnectedDefault: () => ({
    modelFor: () => null,
    settled: () => true,
    save: vi.fn(async () => true),
    saving: false,
  }),
}));

vi.mock('@/hooks/useConfiguredProviders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useConfiguredProviders')>()),
  useConfiguredProviders: () => ({ providers: [], byokEnabled: true }),
  useOAuthStatus: () => ({
    data: { connected: false, state: 'disconnected', can_sign_in: true },
    isError: false,
  }),
}));

vi.mock('@/hooks/useProviderCatalog', () => ({
  useProviderCatalog: () => ({ planGroups: [] }),
}));

vi.mock('@/hooks/useAccount', async () => {
  const { allows } = await import('@/lib/accountRules');
  const who = { start: 'Your organization', mid: 'your organization' };
  return {
    useAccount: () => ({ policy: null, setterName: () => who }),
    useRule: (rule: Parameters<typeof allows>[1]) => ({ allowed: allows(null, rule), who }),
  };
});

vi.mock('@/hooks/useClaudeCode', () => ({
  useClaudeCodeSetup: () => ({ setup: null, isLoading: false, unread: false, retry: vi.fn() }),
}));

vi.mock('@/hooks/useClaudeCodeCustody', () => ({
  useClaudeCodeCustody: () => ({ custody: null, isLoading: false, unread: false, retry: vi.fn() }),
}));

vi.mock('@/hooks/useTurnOnClaudeCode', () => ({
  useTurnOnClaudeCode: () => ({ pending: false, failed: false, retry: vi.fn() }),
}));

import ModelStep from '../ModelStep';

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function renderModelStep() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/setup/model']}>
        <Routes>
          <Route path="/setup/model" element={<ModelStep />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.models = 'langalpha_models';
  h.team = {
    shared: true,
    available: false,
    empty: true,
    isLoading: false,
    unread: false,
    canManage: false,
    callerTierPending: false,
    rejectedProviders: new Set<string>(),
    daltonProfile: 'everflow',
    daltonSetupCandidates: [],
    daltonCuratedProviders: [],
    retry: vi.fn(),
  };
});

describe('ModelStep (Dalton curated setup alignment)', () => {
  it('holds non-admin member with saved langalpha_models on /setup/model with contact-admin notice when Everflow curated is empty', () => {
    renderModelStep();
    expect(screen.getByTestId('setup-model-team-models-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('where')).toBeNull();
  });

  it('holds admin (canManage: true) on /setup/model with empty curated notice when server dalton_setup_candidates is [] in Everflow', () => {
    h.team.canManage = true;
    renderModelStep();
    expect(screen.getByTestId('setup-model-team-models-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('where')).toBeNull();
  });

  it('lets admin (canManage: true) reach SubscriptionStep when team credentials are empty but curated candidates exist', () => {
    h.models = 'subscription_or_key';
    h.team.canManage = true;
    h.team.daltonSetupCandidates = ['gemini-4-neon-high-oauth'];
    h.team.daltonCuratedProviders = ['antigravity-oauth', 'claude-oauth', 'codex-oauth'];
    renderModelStep();
    expect(screen.queryByTestId('setup-model-team-models-empty')).toBeNull();
    expect(screen.getByRole('button', { name: /Google Antigravity/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /ChatGPT/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Claude/i })).toBeInTheDocument();
  });

  it('advances Core profile to /setup/default when confirmed tool-calling fallback models are available', () => {
    h.team.daltonProfile = 'core';
    h.team.daltonSetupCandidates = [];
    h.team.available = true;
    h.team.empty = false;
    renderModelStep();
    expect(screen.getByTestId('where')).toHaveTextContent('/setup/default');
  });
});
