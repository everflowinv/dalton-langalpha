import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import type { ModelsResponse } from '@/api/model';
import { resetCatalogRevisionTracker } from '@/lib/dalton/catalogRevision';
import type {
  TeamCatalogLegacyResolvePayload,
  TeamCatalogModelUpsertPayload,
  TeamCatalogMutationResult,
  TeamCatalogOverviewResponse,
  TeamCatalogProviderVariantUpsertPayload,
  TeamCatalogRetirePayload,
  TeamCatalogRevisionsResponse,
  TeamCatalogRollbackPayload,
  TeamCatalogRollbackResponse,
  TeamCatalogTestCallPayload,
  TeamCatalogTestCallResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  isMobile: false,
  isAdmin: true,
  oauthSyncEnabled: true,
  profile: 'everflow' as 'everflow' | 'core',
  fetchTeamModelCatalog: vi.fn<() => Promise<TeamCatalogOverviewResponse>>(),
  createTeamCatalogModel: vi.fn<(payload: TeamCatalogModelUpsertPayload) => Promise<TeamCatalogMutationResult>>(),
  updateTeamCatalogModel: vi.fn<
    (modelKey: string, payload: TeamCatalogModelUpsertPayload) => Promise<TeamCatalogMutationResult>
  >(),
  setTeamCatalogModelVisibility: vi.fn<
    (
      modelKey: string,
      payload: { visible: boolean; expected_revision?: number; comment?: string | null },
    ) => Promise<TeamCatalogMutationResult>
  >(),
  retireTeamCatalogModel: vi.fn<
    (modelKey: string, payload: TeamCatalogRetirePayload) => Promise<TeamCatalogMutationResult>
  >(),
  restoreTeamCatalogModel: vi.fn<
    (
      modelKey: string,
      payload: { expected_revision?: number; comment?: string | null },
    ) => Promise<TeamCatalogMutationResult>
  >(),
  restoreTeamCatalogModelDefault: vi.fn<
    (
      modelKey: string,
      payload: { expected_revision?: number; comment?: string | null },
    ) => Promise<TeamCatalogMutationResult>
  >(),
  createTeamCatalogProviderVariant: vi.fn<
    (payload: TeamCatalogProviderVariantUpsertPayload) => Promise<TeamCatalogMutationResult>
  >(),
  updateTeamCatalogProviderVariant: vi.fn<
    (
      providerSlug: string,
      payload: TeamCatalogProviderVariantUpsertPayload,
    ) => Promise<TeamCatalogMutationResult>
  >(),
  updateTeamCatalogCuratedSetup: vi.fn<
    (payload: {
      curated_setup: string[] | null;
      expected_revision?: number;
      comment?: string | null;
    }) => Promise<TeamCatalogMutationResult>
  >(),
  fetchTeamCatalogRevisions: vi.fn<
    (limit?: number, offset?: number) => Promise<TeamCatalogRevisionsResponse>
  >(),
  fetchTeamCatalogRevisionDetail: vi.fn(),
  rollbackTeamCatalog: vi.fn<
    (payload: TeamCatalogRollbackPayload) => Promise<TeamCatalogRollbackResponse>
  >(),
  testCallTeamCatalogModel: vi.fn<
    (payload: TeamCatalogTestCallPayload) => Promise<TeamCatalogTestCallResponse>
  >(),
  resolveTeamCatalogLegacyImport: vi.fn<
    (payload: TeamCatalogLegacyResolvePayload) => Promise<TeamCatalogMutationResult>
  >(),
  getAvailableModels: vi.fn<() => Promise<ModelsResponse>>(),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => h.isMobile,
  getIsMobileSnapshot: () => h.isMobile,
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => ({
    isAdmin: h.isAdmin,
    tier: h.isAdmin ? 'admin' : 'internal',
    status: 'ready',
  }),
}));

vi.mock('@/hooks/useCapabilities', () => ({
  useCapabilities: () => ({
    profile: h.profile,
    capabilities: new Set(h.oauthSyncEnabled ? ['native_oauth_sync'] : []),
    hasCapability: (req: string | readonly string[] | null | undefined) => {
      if (!req) return true;
      const list = typeof req === 'string' ? [req] : req;
      return list.some((c) => c === 'native_oauth_sync' && h.oauthSyncEnabled);
    },
    brandName: h.profile === 'core' ? 'Dalton' : 'Everflow',
    fingerprint: null,
    tickerLogosEnabled: false,
    isLoading: false,
    isLoaded: true,
    isCoreBuild: h.profile === 'core',
    isEverflowBuild: h.profile !== 'core',
  }),
}));

vi.mock('@/api/model', () => ({
  getAvailableModels: h.getAvailableModels,
}));

vi.mock('@/pages/Settings/utils/daltonApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/utils/daltonApi')>();
  return {
    ...actual,
    fetchTeamModelCatalog: h.fetchTeamModelCatalog,
    createTeamCatalogModel: h.createTeamCatalogModel,
    updateTeamCatalogModel: h.updateTeamCatalogModel,
    setTeamCatalogModelVisibility: h.setTeamCatalogModelVisibility,
    retireTeamCatalogModel: h.retireTeamCatalogModel,
    restoreTeamCatalogModel: h.restoreTeamCatalogModel,
    restoreTeamCatalogModelDefault: h.restoreTeamCatalogModelDefault,
    createTeamCatalogProviderVariant: h.createTeamCatalogProviderVariant,
    updateTeamCatalogProviderVariant: h.updateTeamCatalogProviderVariant,
    updateTeamCatalogCuratedSetup: h.updateTeamCatalogCuratedSetup,
    fetchTeamCatalogRevisions: h.fetchTeamCatalogRevisions,
    fetchTeamCatalogRevisionDetail: h.fetchTeamCatalogRevisionDetail,
    rollbackTeamCatalog: h.rollbackTeamCatalog,
    testCallTeamCatalogModel: h.testCallTeamCatalogModel,
    resolveTeamCatalogLegacyImport: h.resolveTeamCatalogLegacyImport,
  };
});

import { TeamModelCatalogCard } from '../TeamModelCatalogCard';

function makeOverview(
  overrides: Partial<TeamCatalogOverviewResponse> = {},
): TeamCatalogOverviewResponse {
  return {
    revision: 7,
    payload_sha256: 'abc123',
    legacy_sync_import_status: 'imported',
    legacy_sync_import_error: null,
    models: [
      {
        model_key: 'gpt-5',
        model_id: 'gpt-5',
        provider: 'openai',
        provider_display_name: 'OpenAI',
        provider_connected: true,
        catalog_defined_provider: false,
        display_name: 'GPT-5',
        visible: true,
        source: 'team_overridden',
        status: 'visible',
        intelligence: 5,
        speed: 4,
        plain_language: 4,
        context: 400000,
        compaction_profile: 'spacious',
        input_modalities: ['text', 'image', 'pdf'],
        reasoning: { efforts: ['low', 'medium', 'high'], default: 'medium', write: 'parameters.reasoning.effort' },
        parameters: { max_tokens: 16384, reasoning: { effort: 'medium' } },
        prompt_guidance: 'detailed',
        task_costs_bound: { model: 'claude-sonnet-5-5', times: 0.8 },
        alias: ['gpt-5-latest'],
        pricing: { input: 2.5, cached_input: 0.25, output: 10, unit: 'per_1m_tokens' },
        pricing_meta: { source_kind: 'official', estimated: false, verified_at: '2026-11-24' },
        dalton_meta: { family: 'openai-gpt', billing: 'cash', tool_calling: true },
      },
      {
        model_key: 'gemini-4-neon-high-oauth',
        model_id: 'gemini-4-neon',
        provider: 'antigravity-oauth',
        provider_display_name: 'Antigravity',
        provider_connected: true,
        catalog_defined_provider: false,
        display_name: 'Gemini 4 Neon',
        visible: true,
        source: 'factory',
        status: 'visible',
        intelligence: 5,
        speed: 4,
        plain_language: 5,
        context: 1000000,
        compaction_profile: 'expansive',
        input_modalities: ['text', 'image', 'pdf'],
        reasoning: null,
        pricing: null,
        pricing_meta: null,
        dalton_meta: {
          family: 'google-gemini',
          billing: 'subscription',
          tool_calling: true,
          notional_pricing: { input: 1.5, output: 6, source_kind: 'estimated', estimated: true },
        },
      },
    ],
    retired: [
      {
        key: 'old-legacy-model',
        provider: 'openai',
        retired_at: '2026-11-20T10:00:00Z',
        retired_by: 'admin-1',
        scope: 'team',
        restorable: true,
      },
    ],
    provider_variants: [
      {
        provider_slug: 'acme-gw',
        display_name: 'Acme Gateway',
        sdk: 'openai',
        parent_provider: 'openai',
        access_type: 'api_key',
        billing: 'cash',
        base_url: 'https://gw.acme.example.com/v1',
        credential_configured: true,
        key_fingerprint: 'sk-...9a2f',
        bound_base_url: 'https://gw.acme.example.com/v1',
        updated_at: '2026-11-24T10:00:00Z',
      },
    ],
    parent_provider_groups: ['anthropic', 'antigravity-oauth', 'openai', 'z-ai'],
    supported_variant_sdks: ['anthropic', 'dashscope', 'deepseek', 'gemini', 'glm', 'openai', 'openrouter', 'qwq'],
    curated_setup: ['gemini-4-neon-high-oauth'],
    dalton_setup_candidates: ['gemini-4-neon-high-oauth'],
    dalton_curated_providers: ['antigravity-oauth'],
    ...overrides,
  };
}

function makeConflictError(detail: Record<string, unknown>) {
  return new ApiError('Request failed with status code 409', 'ERR_BAD_REQUEST', {
    status: 409,
    statusText: 'Conflict',
    headers: {},
    data: { detail },
  });
}

const activeQueryClients: QueryClient[] = [];

function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  activeQueryClients.push(qc);
  return render(
    <QueryClientProvider client={qc}>
      <TeamModelCatalogCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetCatalogRevisionTracker({ clearClients: true });
  h.isMobile = false;
  h.isAdmin = true;
  h.oauthSyncEnabled = true;
  h.profile = 'everflow';
  h.fetchTeamModelCatalog.mockResolvedValue(makeOverview());
  h.getAvailableModels.mockResolvedValue({
    models: { openai: { display_name: 'OpenAI', models: ['gpt-5'] } },
    model_metadata: {},
    system_defaults: {},
    compaction_profiles: {} as ModelsResponse['compaction_profiles'],
    search_providers: {} as ModelsResponse['search_providers'],
    provider_catalog: [
      {
        provider: 'openai',
        display_name: 'OpenAI',
        access_type: 'api_key',
        brand_key: 'openai',
        sdk: 'openai',
        byok_eligible: true,
      },
      {
        provider: 'antigravity-oauth',
        display_name: 'Antigravity',
        access_type: 'oauth',
        brand_key: 'antigravity',
        sdk: 'gemini',
        byok_eligible: false,
      },
    ],
    key_candidates: {},
  });
  h.createTeamCatalogModel.mockResolvedValue({ revision: 8, model_key: 'gpt-5-turbo' });
  h.updateTeamCatalogModel.mockResolvedValue({ revision: 8, model_key: 'gpt-5' });
  h.restoreTeamCatalogModelDefault.mockResolvedValue({ revision: 8, model_key: 'gpt-5' });
  h.createTeamCatalogProviderVariant.mockResolvedValue({ revision: 8, provider_slug: 'corp-gw' });
  h.testCallTeamCatalogModel.mockResolvedValue({ ok: true, tool_calling_verified: true });
  h.resolveTeamCatalogLegacyImport.mockResolvedValue({ revision: 8, legacy_sync_import_status: 'imported' });
  h.fetchTeamCatalogRevisions.mockResolvedValue({
    revision: 7,
    revisions: [
      {
        revision: 7,
        action: 'update_model',
        target_keys: ['gpt-5'],
        payload_sha256: 'sha7',
        diff_summary: { updated: 'gpt-5' },
        side_effects: [],
        comment: 'Latest rate tweak',
        restored_from: null,
        actor_user_id: 'admin-1',
        actor_label: 'Alice',
        created_at: '2026-11-25T10:00:00Z',
      },
      {
        revision: 5,
        action: 'upsert_provider_variant',
        target_keys: ['acme-gw'],
        payload_sha256: 'sha5',
        diff_summary: { variant: 'acme-gw' },
        side_effects: [],
        comment: 'Old v1 URL',
        restored_from: null,
        actor_user_id: 'admin-1',
        actor_label: 'Alice',
        created_at: '2026-11-24T10:00:00Z',
      },
    ],
  });
  h.rollbackTeamCatalog.mockResolvedValue({
    revision: 8,
    restored_from: 5,
    restored_tasks: [],
    skipped_user_modified_tasks: [],
    revision_record: {
      revision: 8,
      action: 'rollback',
      target_keys: [],
      payload_sha256: 'sha8',
      diff_summary: {},
      side_effects: [],
      comment: null,
      restored_from: 5,
      actor_user_id: 'admin-1',
      actor_label: 'Alice',
      created_at: '2026-11-25T11:00:00Z',
    },
  });
});

afterEach(() => {
  while (activeQueryClients.length > 0) {
    activeQueryClients.pop()?.clear();
  }
  resetCatalogRevisionTracker({ clearClients: true });
});

describe('TeamModelCatalogCard', () => {
  it('renders models, filters by status, and hides all *-oauth entries in Core profile', async () => {
    h.profile = 'core';
    h.oauthSyncEnabled = false;
    renderCard();

    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());
    expect(screen.queryByTestId('catalog-model-row-gemini-4-neon-high-oauth')).toBeNull();
    expect(screen.queryByText(/antigravity-oauth/i)).toBeNull();

    // Switch to retired filter tab
    fireEvent.click(screen.getByTestId('team-catalog-filter-retired'));
    expect(await screen.findByTestId('retired-row-old-legacy-model')).toBeInTheDocument();
  });

  it('copies defaults from sibling model on provider select, runs test call, and creates a new model', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('team-catalog-add-model-btn'));
    expect(await screen.findByTestId('team-model-edit-sheet')).toBeInTheDocument();

    // Pick openai -> copies sibling defaults from gpt-5
    fireEvent.change(screen.getByTestId('edit-model-provider-select'), { target: { value: 'openai' } });
    expect(await screen.findByTestId('edit-sheet-sibling-copied')).toBeInTheDocument();
    expect((screen.getByTestId('edit-model-context-input') as HTMLInputElement).value).toBe('400000');

    fireEvent.change(screen.getByTestId('edit-model-key-input'), { target: { value: 'gpt-5-turbo' } });
    fireEvent.change(screen.getByTestId('edit-model-display-name-input'), { target: { value: 'GPT-5 Turbo' } });
    fireEvent.change(screen.getByTestId('edit-model-id-input'), { target: { value: 'gpt-5-turbo' } });
    fireEvent.change(screen.getByTestId('edit-model-cash-input'), { target: { value: '1.5' } });
    fireEvent.change(screen.getByTestId('edit-model-cash-output'), { target: { value: '6' } });

    // Diagnostic test call
    fireEvent.click(screen.getByTestId('edit-sheet-test-call-btn'));
    await waitFor(() => expect(h.testCallTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('edit-sheet-test-result')).toBeInTheDocument();

    // Save
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.createTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.createTeamCatalogModel.mock.calls[0][0]).toMatchObject({
      model_key: 'gpt-5-turbo',
      provider: 'openai',
      model_id: 'gpt-5-turbo',
      expected_revision: 7,
    });
  });

  it('creates a catalog provider variant with HTTPS URL and write-only API key', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('team-catalog-add-variant-btn'));
    expect(await screen.findByTestId('variant-form-fields')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('variant-slug-input'), { target: { value: 'corp-gw' } });
    fireEvent.change(screen.getByTestId('variant-display-name-input'), { target: { value: 'Corp Gateway' } });
    fireEvent.change(screen.getByTestId('variant-base-url-input'), { target: { value: 'https://llm.corp.example.com/v1' } });
    fireEvent.change(screen.getByTestId('variant-api-key-input'), { target: { value: 'sk-secret-variant-key' } });

    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.createTeamCatalogProviderVariant).toHaveBeenCalledTimes(1));
    expect(h.createTeamCatalogProviderVariant.mock.calls[0][0]).toMatchObject({
      provider_slug: 'corp-gw',
      base_url: 'https://llm.corp.example.com/v1',
      api_key: 'sk-secret-variant-key',
      expected_revision: 7,
    });
  });

  it('supports restore-factory-default on team-overridden built-in model', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    fireEvent.click(await screen.findByTestId('edit-sheet-restore-default-btn'));
    fireEvent.click(await screen.findByTestId('restore-default-confirm-btn'));

    await waitFor(() =>
      expect(h.restoreTeamCatalogModelDefault).toHaveBeenCalledWith('gpt-5', {
        expected_revision: 7,
        comment: undefined,
      }),
    );
  });

  it('handles 409 model_in_use in retire dialog and completes transactional batch replacement', async () => {
    h.retireTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'model_in_use',
          message: 'Model is referenced by active automations.',
          automations: [{ automation_id: 'a1', name: 'Morning Digest', user_id: 'u1' }],
          user_subagents: [],
          plugin_subagents: [],
          user_preferences: [{ user_id: 'u1', slots: ['model_preference.preferred_model'] }],
        }),
      )
      .mockResolvedValueOnce({ revision: 8, model_key: 'gpt-5', side_effects: [{ kind: 'automation', id: 'a1' }] });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('retire-model-gpt-5-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('retire-model-gpt-5-btn'));
    expect(await screen.findByTestId('team-catalog-retire-dialog')).toBeInTheDocument();

    // First submit without successor -> 409 model_in_use
    fireEvent.click(screen.getByTestId('retire-dialog-confirm-btn'));
    expect(await screen.findByTestId('retire-in-use-notice')).toBeInTheDocument();

    // Choose successor and re-submit
    fireEvent.change(screen.getByTestId('retire-successor-select'), {
      target: { value: 'gemini-4-neon-high-oauth' },
    });
    fireEvent.click(screen.getByTestId('retire-dialog-confirm-btn'));

    await waitFor(() => expect(h.retireTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.retireTeamCatalogModel.mock.calls[1]).toEqual([
      'gpt-5',
      {
        successor_model: 'gemini-4-neon-high-oauth',
        migrate_user_defaults: true,
        clear_subagent_model: false,
        expected_revision: 7,
        comment: undefined,
      },
    ]);
  });

  it('handles 409 variant_credential_required during rollback and submits replacement key for old URL', async () => {
    h.rollbackTeamCatalog
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'variant_credential_required',
          message: 'Missing matching credential for acme-gw.',
          missing_variant_credentials: [
            {
              provider_slug: 'acme-gw',
              required_base_url: 'https://old-gw.acme.example.com/v1',
              current_bound_base_url: 'https://gw.acme.example.com/v2',
            },
          ],
        }),
      )
      .mockResolvedValueOnce({
        revision: 8,
        restored_from: 5,
        restored_tasks: [{ kind: 'automation', id: 'a1' }],
        skipped_user_modified_tasks: [],
        revision_record: {
          revision: 8,
          action: 'rollback',
          target_keys: [],
          payload_sha256: 'sha8',
          diff_summary: {},
          side_effects: [],
          comment: null,
          restored_from: 5,
          actor_user_id: 'admin-1',
          actor_label: 'Alice',
          created_at: '2026-11-25T11:00:00Z',
        },
      });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('team-catalog-history-btn'));
    expect(await screen.findByTestId('rollback-to-v5-btn')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('rollback-to-v5-btn'));
    expect(await screen.findByTestId('rollback-variant-credentials-panel')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('rollback-cred-input-acme-gw'), {
      target: { value: 'sk-old-url-key' },
    });
    fireEvent.click(screen.getByTestId('rollback-creds-submit-btn'));

    await waitFor(() => expect(h.rollbackTeamCatalog).toHaveBeenCalledTimes(2));
    expect(h.rollbackTeamCatalog.mock.calls[1][0]).toMatchObject({
      target_revision: 5,
      expected_revision: 7,
      variant_credentials: {
        'acme-gw': {
          base_url: 'https://old-gw.acme.example.com/v1',
          api_key: 'sk-old-url-key',
        },
      },
    });
    expect(await screen.findByTestId('rollback-success-notice')).toBeInTheDocument();
  });

  it('shows failed legacy import banner, disables readonly legacy restore, and resolves via repair dialog', async () => {
    h.fetchTeamModelCatalog.mockResolvedValue(
      makeOverview({
        legacy_sync_import_status: 'failed',
        legacy_sync_import_error: 'Truncated JSON before retired section',
        retired: [
          {
            key: 'locked-legacy-model',
            provider: 'antigravity-oauth',
            retired_at: null,
            retired_by: null,
            scope: 'readonly_legacy',
            restorable: false,
          },
        ],
      }),
    );

    renderCard();
    expect(await screen.findByTestId('team-catalog-legacy-failed-banner')).toBeInTheDocument();

    // Readonly legacy retired model has disabled Restore button
    fireEvent.click(screen.getByTestId('team-catalog-filter-retired'));
    const restoreBtn = await screen.findByTestId('restore-model-locked-legacy-model-btn');
    expect(restoreBtn).toBeDisabled();

    // Open repair dialog and click retry_import
    fireEvent.click(screen.getByTestId('team-catalog-open-legacy-repair-btn'));
    expect(await screen.findByTestId('team-catalog-legacy-import-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('legacy-import-retry-btn'));

    await waitFor(() =>
      expect(h.resolveTeamCatalogLegacyImport).toHaveBeenCalledWith({
        action: 'retry_import',
        persist_known_retired: true,
        expected_revision: 7,
        comment: undefined,
      }),
    );
  });

  it('renders single-column mobile card stream, action sheet, and full-screen drawer under < 768px', async () => {
    h.isMobile = true;
    renderCard();

    expect(await screen.findByTestId('team-catalog-mobile-cards')).toBeInTheDocument();
    expect(screen.queryByTestId('team-catalog-desktop-list')).toBeNull();

    // Open mobile Action Sheet for secondary actions
    fireEvent.click(screen.getByTestId('mobile-more-gpt-5-btn'));
    expect(await screen.findByTestId('mobile-model-action-sheet')).toBeInTheDocument();
    expect(screen.getByTestId('mobile-action-clone')).toBeInTheDocument();

    // Clone opens full-screen bottom drawer with data-sheet attribute
    fireEvent.click(screen.getByTestId('mobile-action-clone'));
    const sheet = await screen.findByTestId('team-model-edit-sheet');
    expect(sheet).toHaveAttribute('data-sheet', '');
  });

  it('echoes parameters, prompt_guidance, and task_costs_bound in edit sheet and uses server-provided variant SDKs and parent groups', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    // Edit gpt-5 -> verify edit-mode echo fields returned by GET /api/v1/dalton/team-models
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    expect(await screen.findByTestId('team-model-edit-sheet')).toBeInTheDocument();
    expect((screen.getByTestId('edit-model-prompt-guidance-select') as HTMLSelectElement).value).toBe('detailed');
    expect((screen.getByTestId('edit-model-max-tokens-input') as HTMLInputElement).value).toBe('16384');
    expect((screen.getByTestId('edit-model-task-cost-ref-input') as HTMLInputElement).value).toBe('claude-sonnet-5-5');
    expect((screen.getByTestId('edit-model-task-cost-times-input') as HTMLInputElement).value).toBe('0.8');

    // Update max_tokens and save -> preserves existing parameters keys from server echo
    fireEvent.change(screen.getByTestId('edit-model-max-tokens-input'), { target: { value: '32768' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1]).toMatchObject({
      prompt_guidance: 'detailed',
      parameters: { max_tokens: 32768, reasoning: { effort: 'medium' } },
      task_costs_bound: { model: 'claude-sonnet-5-5', times: 0.8 },
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // Open Add Variant -> options come directly from server parent_provider_groups & supported_variant_sdks
    fireEvent.click(screen.getByTestId('team-catalog-add-variant-btn'));
    const parentSelect = (await screen.findByTestId('variant-parent-select')) as HTMLSelectElement;
    const sdkSelect = screen.getByTestId('variant-sdk-select') as HTMLSelectElement;
    expect(Array.from(parentSelect.options).map((o) => o.value)).toEqual([
      'anthropic',
      'antigravity-oauth',
      'openai',
      'z-ai',
    ]);
    expect(Array.from(sdkSelect.options).map((o) => o.value)).toEqual([
      'anthropic',
      'dashscope',
      'deepseek',
      'gemini',
      'glm',
      'openai',
      'openrouter',
      'qwq',
    ]);
  });
});
