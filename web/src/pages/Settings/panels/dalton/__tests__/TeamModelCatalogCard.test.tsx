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
        provider_sdk: 'antigravity',
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
        parameters: { max_output_tokens: 65536 },
        pricing: null,
        pricing_meta: null,
        dalton_meta: {
          family: 'google-gemini',
          billing: 'subscription',
          tool_calling: true,
          notional_pricing: {
            input: 1.5,
            cached_input: 0.15,
            output: 6,
            source_kind: 'estimated',
            estimated: true,
          },
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

    // Clone opens full-screen bottom drawer with data-sheet attribute and visual-viewport keyboard offset
    fireEvent.click(screen.getByTestId('mobile-action-clone'));
    const sheet = await screen.findByTestId('team-model-edit-sheet');
    expect(sheet).toHaveAttribute('data-sheet', '');
    expect(sheet.className).toContain('--visual-viewport-bottom-offset');
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

    // Update max_tokens and save -> sends ONLY the changed parameters field (never echoes unchanged fields or non-UI parameters)
    fireEvent.change(screen.getByTestId('edit-model-max-tokens-input'), { target: { value: '32768' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    const editedPayload = h.updateTeamCatalogModel.mock.calls[0][1];
    expect(editedPayload).toEqual({
      parameters: { max_tokens: 32768 },
      expected_revision: 7,
      comment: undefined,
    });
    expect(editedPayload.prompt_guidance).toBeUndefined();
    expect(editedPayload.task_costs_bound).toBeUndefined();
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

  it('clears API key when switching provider variant or editing URL and submits selected variant full config atomically', async () => {
    h.fetchTeamModelCatalog.mockResolvedValue(
      makeOverview({
        models: [
          {
            ...makeOverview().models[0],
            model_key: 'acme-chat',
            model_id: 'acme-chat-v1',
            provider: 'acme-gw',
            provider_display_name: 'Acme Gateway',
            catalog_defined_provider: true,
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
            base_url: 'https://gw-a.acme.example.com/v1',
            credential_configured: true,
            key_fingerprint: 'sk-...aaaa',
            bound_base_url: 'https://gw-a.acme.example.com/v1',
            updated_at: '2026-11-24T10:00:00Z',
          },
          {
            provider_slug: 'z-coding-gw',
            display_name: 'Z Coding Gateway',
            sdk: 'glm',
            parent_provider: 'z-ai',
            access_type: 'coding_plan',
            billing: 'subscription',
            base_url: 'https://gw-b.z.example.com/v1',
            credential_configured: true,
            key_fingerprint: 'sk-...bbbb',
            bound_base_url: 'https://gw-b.z.example.com/v1',
            updated_at: '2026-11-24T10:00:00Z',
          },
        ],
      }),
    );

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-acme-chat-btn')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('edit-model-acme-chat-btn'));
    expect(await screen.findByTestId('inline-variant-api-key-input')).toBeInTheDocument();

    // Enter a key for variant A
    const keyInput = screen.getByTestId('inline-variant-api-key-input') as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: 'sk-variant-a-secret' } });
    expect(keyInput.value).toBe('sk-variant-a-secret');

    // Switch provider to variant B -> API key MUST be cleared
    fireEvent.change(screen.getByTestId('edit-model-provider-select'), {
      target: { value: 'z-coding-gw' },
    });
    expect((screen.getByTestId('inline-variant-api-key-input') as HTMLInputElement).value).toBe('');
    expect((screen.getByTestId('inline-variant-base-url-input') as HTMLInputElement).value).toBe(
      'https://gw-b.z.example.com/v1',
    );

    // Enter a key, then modify base URL -> API key MUST be cleared again
    fireEvent.change(screen.getByTestId('inline-variant-api-key-input'), {
      target: { value: 'sk-before-url-change' },
    });
    fireEvent.change(screen.getByTestId('inline-variant-base-url-input'), {
      target: { value: 'https://gw-b2.z.example.com/v1' },
    });
    expect((screen.getByTestId('inline-variant-api-key-input') as HTMLInputElement).value).toBe('');

    // Re-enter new key for variant B and save -> single atomic update carrying variant B's full config
    fireEvent.change(screen.getByTestId('inline-variant-api-key-input'), {
      target: { value: 'sk-variant-b2-fresh' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogProviderVariant).not.toHaveBeenCalled();
    expect(h.updateTeamCatalogModel.mock.calls[0][1].provider_variant_update).toEqual({
      provider_slug: 'z-coding-gw',
      display_name: 'Z Coding Gateway',
      sdk: 'glm',
      parent_provider: 'z-ai',
      access_type: 'coding_plan',
      billing: 'subscription',
      base_url: 'https://gw-b2.z.example.com/v1',
      api_key: 'sk-variant-b2-fresh',
    });
  });

  it('waits for target catalog revision refresh on 409 conflict, shows conflicted fields, preserves draft, and submits only user-changed fields', async () => {
    const initial = makeOverview({ revision: 7 });
    const updatedByOther = makeOverview({
      revision: 9,
      models: [
        {
          ...initial.models[0],
          display_name: 'GPT-5 Edited By Bob',
          context: 512000,
          visible: false,
        },
        initial.models[1],
      ],
    });
    let resolveConflictRefetch!: (value: TeamCatalogOverviewResponse) => void;
    const deferredRefetch = new Promise<TeamCatalogOverviewResponse>((resolve) => {
      resolveConflictRefetch = resolve;
    });

    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(deferredRefetch)
      .mockResolvedValue(updatedByOther);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    // Open edit sheet at revision 7 and edit ONLY display_name
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    const nameInput = (await screen.findByTestId('edit-model-display-name-input')) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: 'GPT-5 My Draft' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1].expected_revision).toBe(7);

    // Conflict banner appears while refetch to v9 is still pending: retry MUST remain disabled
    expect(await screen.findByTestId('edit-sheet-revision-conflict')).toBeInTheDocument();
    expect(screen.getByTestId('edit-sheet-conflict-syncing')).toBeInTheDocument();
    expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeDisabled();
    expect((screen.getByTestId('edit-model-display-name-input') as HTMLInputElement).value).toBe('GPT-5 My Draft');

    // Complete catalog refresh at revision 9 -> conflicted fields are shown and retry enables
    resolveConflictRefetch(updatedByOther);
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    expect(screen.getByTestId('edit-sheet-conflict-latest-summary').textContent).toContain('GPT-5 Edited By Bob');
    const conflictFields = screen.getByTestId('edit-sheet-conflict-fields');
    expect(conflictFields.textContent).toContain('GPT-5 Edited By Bob');
    expect(conflictFields.textContent).toContain('GPT-5 My Draft');
    expect(conflictFields.textContent).toContain('512000');

    // Resubmit draft against revision 9 -> sends ONLY the user's changed field (display_name), preserving Bob's context & visibility
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      display_name: 'GPT-5 My Draft',
      expected_revision: 9,
      comment: undefined,
    });
  });

  it('retries restore-default (not normal save) after 409 revision_conflict once target revision finishes loading', async () => {
    const initial = makeOverview({ revision: 7 });
    const updatedByOther = makeOverview({
      revision: 9,
      models: [
        {
          ...initial.models[0],
          display_name: 'GPT-5 Bob Override',
        },
        initial.models[1],
      ],
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(updatedByOther)
      .mockResolvedValue(updatedByOther);

    h.restoreTeamCatalogModelDefault
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    fireEvent.click(await screen.findByTestId('edit-sheet-restore-default-btn'));
    fireEvent.click(await screen.findByTestId('restore-default-confirm-btn'));

    await waitFor(() => expect(h.restoreTeamCatalogModelDefault).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('edit-sheet-revision-conflict')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());

    // Clicking conflict retry MUST execute restore-default again (never updateTeamCatalogModel)
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));
    await waitFor(() => expect(h.restoreTeamCatalogModelDefault).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel).not.toHaveBeenCalled();
    expect(h.restoreTeamCatalogModelDefault.mock.calls[1]).toEqual([
      'gpt-5',
      { expected_revision: 9, comment: undefined },
    ]);
  });

  it('updates Antigravity max output using max_output_tokens without swapping to max_tokens and sends null when clearing notional cached_input', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');

    // 1. Update max output on Antigravity model -> writes max_output_tokens directly
    expect((screen.getByTestId('edit-model-max-tokens-input') as HTMLInputElement).value).toBe('65536');
    fireEvent.change(screen.getByTestId('edit-model-max-tokens-input'), { target: { value: '131072' } });
    // 2. Clear ONLY notional cached_input -> sends explicit cached_input: null while keeping input & output
    expect((screen.getByTestId('edit-model-notional-cached') as HTMLInputElement).value).toBe('0.15');
    fireEvent.change(screen.getByTestId('edit-model-notional-cached'), { target: { value: '' } });

    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    const sentPayload = h.updateTeamCatalogModel.mock.calls[0][1];
    expect(sentPayload.parameters).toEqual({ max_output_tokens: 131072 });
    expect(sentPayload.dalton_meta?.notional_pricing).toEqual({
      cached_input: null,
      cache_read: null,
    });
  });

  it('keeps tri-state tool_calling unchanged when editing only display name and submits explicit nulls when clearing fields', async () => {
    const overview = makeOverview();
    overview.models[0] = {
      ...overview.models[0],
      dalton_meta: {
        family: 'openai-gpt',
        billing: 'cash',
        // tool_calling intentionally undefined (unprobed)
      },
    };
    h.fetchTeamModelCatalog.mockResolvedValue(overview);

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    // 1. Edit only display_name on unprobed model -> tool_calling MUST NOT be submitted
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    const tcSelect = (await screen.findByTestId('edit-model-tool-calling-select')) as HTMLSelectElement;
    expect(tcSelect.value).toBe('unknown');

    fireEvent.change(screen.getByTestId('edit-model-display-name-input'), {
      target: { value: 'GPT-5 Renamed' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    const firstPayload = h.updateTeamCatalogModel.mock.calls[0][1];
    expect(firstPayload.display_name).toBe('GPT-5 Renamed');
    expect(firstPayload.dalton_meta?.tool_calling).toBeUndefined();
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Clear prompt_guidance, compaction_profile, max_tokens, task_costs_bound, and disable reasoning
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-prompt-guidance-select'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-compaction-select'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-max-tokens-input'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-task-cost-ref-input'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-task-cost-times-input'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-model-reasoning-toggle'));
    fireEvent.change(screen.getByTestId('edit-model-tool-calling-select'), { target: { value: 'false' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toMatchObject({
      prompt_guidance: null,
      compaction_profile: null,
      parameters: { max_tokens: null, max_output_tokens: null },
      reasoning: null,
      task_costs_bound: null,
      dalton_meta: { tool_calling: false },
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 3. Keep reasoning enabled but clear reasoning.write -> submits ONLY reasoning.write: null
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-reasoning-write-input'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(3));
    expect(h.updateTeamCatalogModel.mock.calls[2][1].reasoning).toEqual({
      write: null,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 4. Clear individual subscription notional pricing rates -> submits per-field nulls (preserving sibling fields)
    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-notional-input'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-notional-cached'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-notional-output'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(4));
    expect(h.updateTeamCatalogModel.mock.calls[3][1].dalton_meta).toEqual({
      notional_pricing: {
        input: null,
        cached_input: null,
        cache_read: null,
        output: null,
      },
    });
  });

  it('requires confirmation in EditSheet before hiding or un-curating the last visible curated model in Everflow', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');

    // Toggle curated off on the sole visible curated model in Everflow
    fireEvent.click(screen.getByTestId('edit-model-curated-toggle'));
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    // Must pause for risk confirmation instead of saving immediately
    expect(await screen.findByTestId('edit-sheet-last-curated-confirm')).toBeInTheDocument();
    expect(h.updateTeamCatalogModel).not.toHaveBeenCalled();

    // Confirming submits single atomic update with curated_setup_member: false
    fireEvent.click(screen.getByTestId('edit-sheet-last-curated-confirm-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1]).toMatchObject({
      curated_setup_member: false,
      expected_revision: 7,
    });
  });

  it('submits both variant_credentials and successor_for_dropped on combined rollback retry and builds options from target revision', async () => {
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
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'model_in_use',
          message: "Model 'gpt-5-new' is still referenced by active automations.",
          dropped_model: 'gpt-5-new',
          target_revision_models: [
            { model_key: 'gpt-5-v5-only', display_name: 'GPT-5 v5', provider: 'openai' },
          ],
        }),
      )
      .mockResolvedValueOnce({
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

    renderCard();
    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('team-catalog-history-btn'));
    fireEvent.click(await screen.findByTestId('rollback-to-v5-btn'));

    // Step 1: Enter missing variant credential and submit
    fireEvent.change(await screen.findByTestId('rollback-cred-input-acme-gw'), {
      target: { value: 'sk-old-url-key' },
    });
    fireEvent.click(screen.getByTestId('rollback-creds-submit-btn'));

    // Step 2: Backend now asks for replacement for dropped model using target_revision_models
    const select = (await screen.findByTestId('rollback-dropped-successor-select')) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value).filter(Boolean);
    expect(optionValues).toEqual(['gpt-5-v5-only']);

    fireEvent.change(select, { target: { value: 'gpt-5-v5-only' } });
    fireEvent.click(screen.getByTestId('rollback-dropped-submit-btn'));

    await waitFor(() => expect(h.rollbackTeamCatalog).toHaveBeenCalledTimes(3));
    expect(h.rollbackTeamCatalog.mock.calls[2][0]).toMatchObject({
      target_revision: 5,
      variant_credentials: {
        'acme-gw': {
          base_url: 'https://old-gw.acme.example.com/v1',
          api_key: 'sk-old-url-key',
        },
      },
      successor_for_dropped: {
        'gpt-5-new': 'gpt-5-v5-only',
      },
    });
  });

  it('prunes *-oauth history fields in Core profile while preserving revision rows and rollback buttons', async () => {
    h.profile = 'core';
    h.oauthSyncEnabled = false;
    h.fetchTeamCatalogRevisions.mockResolvedValue({
      revision: 7,
      revisions: [
        {
          revision: 7,
          action: 'update_model',
          target_keys: ['gpt-5', 'gemini-4-neon-high-oauth'],
          payload_sha256: 'sha7',
          diff_summary: { updated: 'gemini-4-neon-high-oauth', provider: 'openai' },
          side_effects: [
            { kind: 'automation', id: 'a1', before: { llm_model: 'gemini-4-neon-high-oauth' } },
          ],
          comment: 'History row',
          restored_from: null,
          actor_user_id: 'admin-1',
          actor_label: 'Alice',
          created_at: '2026-11-25T10:00:00Z',
        },
        {
          revision: 5,
          action: 'update_model',
          target_keys: ['claude-opus-5-5-oauth'],
          payload_sha256: 'sha5',
          diff_summary: { updated: 'claude-opus-5-5-oauth' },
          side_effects: [],
          comment: 'Older version',
          restored_from: null,
          actor_user_id: 'admin-1',
          actor_label: 'Alice',
          created_at: '2026-11-24T10:00:00Z',
        },
      ],
    });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('catalog-model-row-gpt-5')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('team-catalog-history-btn'));
    const dialog = await screen.findByTestId('team-catalog-history-dialog');
    expect(await screen.findByTestId('revision-row-v5')).toBeInTheDocument();
    expect(screen.getByTestId('rollback-to-v5-btn')).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/-oauth/i);
  });

  it('submits only changed nested pricing, pricing_meta, notional_pricing, and reasoning subfields on 409 conflict retry', async () => {
    const initial = makeOverview({ revision: 7 });
    const afterBobCashEdit = makeOverview({
      revision: 9,
      models: [
        {
          ...initial.models[0],
          pricing: { input: 3.0, cached_input: 0.25, output: 10, unit: 'per_1m_tokens' },
          pricing_meta: { source_kind: 'official', estimated: false, verified_at: '2026-11-26' },
          reasoning: { efforts: ['low', 'medium', 'high'], default: 'medium', write: 'extra_body.reasoning_effort' },
        },
        initial.models[1],
      ],
    });
    const afterBobNotionalEdit = makeOverview({
      revision: 11,
      models: [
        afterBobCashEdit.models[0],
        {
          ...initial.models[1],
          dalton_meta: {
            ...initial.models[1].dalton_meta,
            notional_pricing: {
              input: 2.0,
              cached_input: 0.15,
              output: 6,
              source_kind: 'estimated',
              estimated: true,
              note: 'Updated by Bob',
            },
          },
        },
      ],
    });

    let currentOverview = initial;
    h.fetchTeamModelCatalog.mockImplementation(async () => currentOverview);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' })
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' })
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 9, current is 11.',
          expected_revision: 9,
          current_revision: 11,
        }),
      )
      .mockResolvedValueOnce({ revision: 12, model_key: 'gemini-4-neon-high-oauth' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    // 1. Cash model: edit ONLY output rate and reasoning.default; Bob changed input rate, verified_at, and reasoning.write
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-cash-output'), { target: { value: '12' } });
    fireEvent.change(screen.getByTestId('edit-model-reasoning-default-select'), { target: { value: 'high' } });
    currentOverview = afterBobCashEdit;
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      pricing: { output: 12 },
      reasoning: { default: 'high' },
      expected_revision: 9,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Clear ONLY pricing_meta.verified_at -> sends ONLY pricing_meta: { verified_at: null } with no pricing object
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-price-verified-at'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(3));
    expect(h.updateTeamCatalogModel.mock.calls[2][1]).toEqual({
      pricing_meta: { verified_at: null },
      expected_revision: 9,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 3. Subscription model: edit ONLY notional output and clear notional cached_input while Bob changed notional input & note
    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-notional-output'), { target: { value: '7.5' } });
    fireEvent.change(screen.getByTestId('edit-model-notional-cached'), { target: { value: '' } });
    currentOverview = afterBobNotionalEdit;
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(5));
    expect(h.updateTeamCatalogModel.mock.calls[4][1]).toEqual({
      dalton_meta: {
        notional_pricing: {
          output: 7.5,
          cached_input: null,
          cache_read: null,
        },
      },
      expected_revision: 11,
      comment: undefined,
    });
  });

  it('compares catalog variant base_url against opened baseline so renaming a model on a concurrently updated variant omits provider_variant_update', async () => {
    const baseOverview = makeOverview({
      revision: 7,
      models: [
        {
          model_key: 'acme-chat',
          model_id: 'acme-chat-v1',
          provider: 'acme-gw',
          provider_sdk: 'openai',
          provider_display_name: 'Acme Gateway',
          provider_connected: true,
          catalog_defined_provider: true,
          display_name: 'Acme Chat',
          visible: true,
          source: 'team_added',
          status: 'visible',
          intelligence: 4,
          speed: 4,
          plain_language: 4,
          context: 128000,
          compaction_profile: 'moderate',
          input_modalities: ['text'],
          reasoning: null,
          pricing: { input: 1, output: 2, unit: 'per_1m_tokens' },
          pricing_meta: { source_kind: 'official', estimated: false },
          dalton_meta: { family: 'acme', billing: 'cash', tool_calling: true },
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
          base_url: 'https://gw-v1.acme.example.com/v1',
          credential_configured: true,
          key_fingerprint: 'sk-...1111',
          bound_base_url: 'https://gw-v1.acme.example.com/v1',
          updated_at: '2026-11-24T10:00:00Z',
        },
      ],
    });
    const bobUpdatedVariantUrl = makeOverview({
      ...baseOverview,
      revision: 8,
      provider_variants: [
        {
          ...baseOverview.provider_variants[0],
          base_url: 'https://gw-v2.acme.example.com/v1',
          bound_base_url: 'https://gw-v2.acme.example.com/v1',
          key_fingerprint: 'sk-...2222',
        },
      ],
    });

    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(baseOverview)
      .mockResolvedValueOnce(bobUpdatedVariantUrl)
      .mockResolvedValue(bobUpdatedVariantUrl);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 8.',
          expected_revision: 7,
          current_revision: 8,
        }),
      )
      .mockResolvedValueOnce({ revision: 9, model_key: 'acme-chat' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-acme-chat-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-acme-chat-btn'));
    const nameInput = (await screen.findByTestId('edit-model-display-name-input')) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: 'Acme Chat Renamed' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    expect(screen.getByTestId('edit-sheet-conflict-field-variant_base_url').textContent).toContain(
      'https://gw-v2.acme.example.com/v1',
    );

    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      display_name: 'Acme Chat Renamed',
      expected_revision: 8,
      comment: undefined,
    });
    expect(h.updateTeamCatalogModel.mock.calls[1][1].provider_variant_update).toBeUndefined();
  });

  it('requires reviewing an updated variant base_url before submitting a key-only rotation after 409 conflict', async () => {
    const baseOverview = makeOverview({
      revision: 7,
      models: [
        {
          model_key: 'acme-chat',
          model_id: 'acme-chat-v1',
          provider: 'acme-gw',
          provider_sdk: 'openai',
          provider_display_name: 'Acme Gateway',
          provider_connected: true,
          catalog_defined_provider: true,
          display_name: 'Acme Chat',
          visible: true,
          source: 'team_added',
          status: 'visible',
          intelligence: 4,
          speed: 4,
          plain_language: 4,
          context: 128000,
          compaction_profile: 'moderate',
          input_modalities: ['text'],
          reasoning: null,
          pricing: { input: 1, output: 2, unit: 'per_1m_tokens' },
          pricing_meta: { source_kind: 'official', estimated: false },
          dalton_meta: { family: 'acme', billing: 'cash', tool_calling: true },
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
          base_url: 'https://gw-v1.acme.example.com/v1',
          credential_configured: true,
          key_fingerprint: 'sk-...1111',
          bound_base_url: 'https://gw-v1.acme.example.com/v1',
          updated_at: '2026-11-24T10:00:00Z',
        },
      ],
    });
    const bobUpdatedVariantUrl = makeOverview({
      ...baseOverview,
      revision: 8,
      provider_variants: [
        {
          ...baseOverview.provider_variants[0],
          base_url: 'https://gw-v2.acme.example.com/v1',
          bound_base_url: 'https://gw-v2.acme.example.com/v1',
          key_fingerprint: 'sk-...2222',
        },
      ],
    });

    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(baseOverview)
      .mockResolvedValueOnce(bobUpdatedVariantUrl)
      .mockResolvedValue(bobUpdatedVariantUrl);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 8.',
          expected_revision: 7,
          current_revision: 8,
        }),
      )
      .mockResolvedValueOnce({ revision: 9, model_key: 'acme-chat' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-acme-chat-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-acme-chat-btn'));
    fireEvent.change(await screen.findByTestId('inline-variant-api-key-input'), {
      target: { value: 'sk-key-only-rotation' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());

    // Clicking retry MUST NOT silently overwrite v2 URL with stale v1 URL
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));
    expect(await screen.findByTestId('edit-sheet-error')).toBeInTheDocument();
    expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1);
    expect((screen.getByTestId('inline-variant-base-url-input') as HTMLInputElement).value).toBe(
      'https://gw-v2.acme.example.com/v1',
    );
    expect((screen.getByTestId('inline-variant-api-key-input') as HTMLInputElement).value).toBe('');

    // After admin reviews the updated v2 URL and re-confirms key, saving submits against v8 with v2 URL
    fireEvent.change(screen.getByTestId('inline-variant-api-key-input'), {
      target: { value: 'sk-confirmed-for-v2' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      expected_revision: 8,
      comment: undefined,
      provider_variant_update: {
        provider_slug: 'acme-gw',
        display_name: 'Acme Gateway',
        sdk: 'openai',
        parent_provider: 'openai',
        access_type: 'api_key',
        billing: 'cash',
        base_url: 'https://gw-v2.acme.example.com/v1',
        api_key: 'sk-confirmed-for-v2',
      },
    });
  });

  it('preserves last-curated confirmation and refreshed revision across 409 without looping and disables restore-default confirm during catalog reread', async () => {
    // Part A: Already-confirmed last curated removal hits 409 (Bob renamed the model) -> retry submits on v9 without conflict loop
    const initial = makeOverview({ revision: 7 });
    const bobRenamedAtV9 = makeOverview({
      revision: 9,
      models: [
        initial.models[0],
        {
          ...initial.models[1],
          display_name: 'Gemini 4 Neon Renamed By Bob',
        },
      ],
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(bobRenamedAtV9)
      .mockResolvedValue(bobRenamedAtV9);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gemini-4-neon-high-oauth' });

    const { unmount } = renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.click(screen.getByTestId('edit-model-curated-toggle'));
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    fireEvent.click(await screen.findByTestId('edit-sheet-last-curated-confirm-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1].expected_revision).toBe(7);

    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      curated_setup_member: false,
      expected_revision: 9,
      comment: undefined,
    });
    unmount();

    // Part B: Model newly becomes last curated during conflict -> confirmation retains refreshed revision (v8, not v7)
    h.fetchTeamModelCatalog.mockReset();
    h.updateTeamCatalogModel.mockReset();
    const twoCuratedV7 = makeOverview({
      revision: 7,
      curated_setup: ['gpt-5', 'gemini-4-neon-high-oauth'],
      dalton_setup_candidates: ['gpt-5', 'gemini-4-neon-high-oauth'],
    });
    const soleCuratedV8 = makeOverview({
      revision: 8,
      curated_setup: ['gemini-4-neon-high-oauth'],
      dalton_setup_candidates: ['gemini-4-neon-high-oauth'],
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(twoCuratedV7)
      .mockResolvedValueOnce(soleCuratedV8)
      .mockResolvedValue(soleCuratedV8);
    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 8.',
          expected_revision: 7,
          current_revision: 8,
        }),
      )
      .mockResolvedValueOnce({ revision: 9, model_key: 'gemini-4-neon-high-oauth' });

    const { unmount: unmountB } = renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.click(screen.getByTestId('edit-model-curated-toggle'));
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    // Now it asks for last-curated confirmation AND confirming MUST submit with refreshed revision 8
    fireEvent.click(await screen.findByTestId('edit-sheet-last-curated-confirm-btn'));
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      curated_setup_member: false,
      expected_revision: 8,
      comment: undefined,
    });
    unmountB();

    // Part C: Restore-default confirm button and trigger are disabled while catalog reread is pending
    h.fetchTeamModelCatalog.mockReset();
    h.updateTeamCatalogModel.mockReset();
    let resolveReread!: (value: TeamCatalogOverviewResponse) => void;
    const deferredReread = new Promise<TeamCatalogOverviewResponse>((resolve) => {
      resolveReread = resolve;
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(deferredReread)
      .mockResolvedValue(bobRenamedAtV9);
    h.updateTeamCatalogModel.mockRejectedValueOnce(
      makeConflictError({
        code: 'revision_conflict',
        message: 'Expected revision 7, current is 9.',
        expected_revision: 7,
        current_revision: 9,
      }),
    );

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');

    // Open restore-default confirm box, then trigger a 409 save while reread is still loading
    fireEvent.click(screen.getByTestId('edit-sheet-restore-default-btn'));
    expect(await screen.findByTestId('restore-default-confirm-btn')).toBeEnabled();
    fireEvent.change(screen.getByTestId('edit-model-display-name-input'), {
      target: { value: 'GPT-5 Draft' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    expect(await screen.findByTestId('edit-sheet-conflict-syncing')).toBeInTheDocument();
    expect(screen.getByTestId('restore-default-confirm-btn')).toBeDisabled();
    expect(screen.getByTestId('edit-sheet-restore-default-btn')).toBeDisabled();
    expect(screen.getByTestId('edit-sheet-save-btn')).toBeDisabled();
    expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeDisabled();

    resolveReread(bobRenamedAtV9);
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    expect(screen.getByTestId('restore-default-confirm-btn')).toBeEnabled();
  });

  it('chooses max_tokens over max_output_tokens for a google-named variant using the openai SDK without overwriting concurrent edits', async () => {
    const initial = makeOverview({
      revision: 7,
      models: [
        {
          model_key: 'google-proxy-chat',
          model_id: 'gemini-3-pro',
          provider: 'google',
          provider_sdk: 'openai',
          provider_display_name: 'Google OpenAI Proxy',
          provider_connected: true,
          catalog_defined_provider: true,
          display_name: 'Google Proxy Chat',
          visible: true,
          source: 'team_added',
          status: 'visible',
          intelligence: 4,
          speed: 4,
          plain_language: 4,
          context: 200000,
          compaction_profile: 'moderate',
          input_modalities: ['text'],
          reasoning: null,
          parameters: { max_tokens: 8192 },
          pricing: { input: 1, output: 2, unit: 'per_1m_tokens' },
          pricing_meta: { source_kind: 'official', estimated: false },
          dalton_meta: { family: 'google-gemini', billing: 'cash', tool_calling: true },
        },
      ],
      provider_variants: [
        {
          provider_slug: 'google',
          display_name: 'Google OpenAI Proxy',
          sdk: 'openai',
          parent_provider: 'openai',
          access_type: 'api_key',
          billing: 'cash',
          base_url: 'https://google-openai-proxy.example.com/v1',
          credential_configured: true,
          key_fingerprint: 'sk-...goog',
          bound_base_url: 'https://google-openai-proxy.example.com/v1',
          updated_at: '2026-11-24T10:00:00Z',
        },
      ],
    });
    const bobRenamed = makeOverview({
      ...initial,
      revision: 9,
      models: [
        {
          ...initial.models[0],
          display_name: 'Google Proxy Renamed By Bob',
        },
      ],
    });

    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(bobRenamed)
      .mockResolvedValue(bobRenamed);
    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'google-proxy-chat' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-google-proxy-chat-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-google-proxy-chat-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-max-tokens-input'), { target: { value: '16384' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      parameters: { max_tokens: 16384 },
      expected_revision: 9,
      comment: undefined,
    });
  });

  it('diffs task_costs_bound.model and task_costs_bound.times separately so concurrent updates do not overwrite each other', async () => {
    const initial = makeOverview({ revision: 7 });
    const bobUpdatedRef = makeOverview({
      revision: 8,
      models: [
        {
          ...initial.models[0],
          task_costs_bound: { model: 'claude-opus-5-5', times: 0.8 },
        },
        initial.models[1],
      ],
    });

    let currentOverview = initial;
    h.fetchTeamModelCatalog.mockImplementation(async () => currentOverview);
    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 8.',
          expected_revision: 7,
          current_revision: 8,
        }),
      )
      .mockResolvedValueOnce({ revision: 9, model_key: 'gpt-5' })
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    // 1. Alice edits ONLY task_costs_bound.times while Bob changed task_costs_bound.model -> retry sends ONLY { times }
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-task-cost-times-input'), {
      target: { value: '1.25' },
    });
    currentOverview = bobUpdatedRef;
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      task_costs_bound: { times: 1.25 },
      expected_revision: 8,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Edit ONLY task_costs_bound.model -> sends ONLY { model }
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-task-cost-ref-input'), {
      target: { value: 'gpt-6-astra' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(3));
    expect(h.updateTeamCatalogModel.mock.calls[2][1]).toEqual({
      task_costs_bound: { model: 'gpt-6-astra' },
      expected_revision: 8,
      comment: undefined,
    });
  });

  it('sends per-field nulls when clearing notional rates and sends notional_pricing null only on explicit whole-table delete', async () => {
    const initial = makeOverview({
      revision: 7,
      models: [
        makeOverview().models[0],
        {
          ...makeOverview().models[1],
          dalton_meta: {
            family: 'google-gemini',
            billing: 'subscription',
            tool_calling: true,
            notional_pricing: {
              input: 1.5,
              output: 6,
              source_kind: 'official',
              estimated: false,
            },
          },
        },
      ],
    });
    const bobAddedCachedAndNote = makeOverview({
      revision: 9,
      models: [
        initial.models[0],
        {
          ...initial.models[1],
          dalton_meta: {
            ...initial.models[1].dalton_meta,
            notional_pricing: {
              input: 1.5,
              cached_input: 0.2,
              output: 6,
              source_kind: 'third_party',
              estimated: false,
              note: 'Added by Bob',
            },
          },
        },
      ],
    });

    let currentOverview = initial;
    h.fetchTeamModelCatalog.mockImplementation(async () => currentOverview);
    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gemini-4-neon-high-oauth' })
      .mockResolvedValueOnce({ revision: 11, model_key: 'gemini-4-neon-high-oauth' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());

    // 1. Alice clears input & output inputs while Bob added cached_input and note -> retry sends per-field nulls only
    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-notional-input'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('edit-model-notional-output'), { target: { value: '' } });
    currentOverview = bobAddedCachedAndNote;
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      dalton_meta: {
        notional_pricing: {
          input: null,
          output: null,
        },
      },
      expected_revision: 9,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Explicitly clicking delete-whole-table submits notional_pricing: null
    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.click(screen.getByTestId('edit-model-delete-notional-table-btn'));
    expect(screen.getByTestId('edit-model-delete-notional-table-notice')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(3));
    expect(h.updateTeamCatalogModel.mock.calls[2][1]).toEqual({
      dalton_meta: {
        notional_pricing: null,
      },
      expected_revision: 9,
      comment: undefined,
    });
  });

  it('submits both cached_input null and cache_read null when clearing factory cache_read notional rate', async () => {
    const museOverview = makeOverview({
      revision: 7,
      models: [
        {
          model_key: 'muse-spark-1.3-oauth',
          model_id: 'muse-spark-1.3',
          provider: 'muse-oauth',
          provider_display_name: 'Muse OAuth',
          provider_connected: true,
          catalog_defined_provider: false,
          display_name: 'Muse Spark 1.3',
          visible: true,
          source: 'factory',
          status: 'visible',
          intelligence: 3,
          speed: 5,
          plain_language: null,
          context: 1007997,
          compaction_profile: null,
          input_modalities: ['text', 'image'],
          reasoning: null,
          pricing: null,
          pricing_meta: null,
          dalton_meta: {
            family: 'meta-muse',
            billing: 'subscription',
            tool_calling: true,
            notional_ref: 'grok-4.7',
            notional_pricing: {
              input: 2.0,
              output: 6.0,
              cache_read: 0.5,
              estimated: true,
            },
          },
        },
      ],
    });
    h.fetchTeamModelCatalog.mockResolvedValue(museOverview);
    h.updateTeamCatalogModel
      .mockResolvedValueOnce({ revision: 8, model_key: 'muse-spark-1.3-oauth' })
      .mockResolvedValueOnce({ revision: 9, model_key: 'muse-spark-1.3-oauth' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-muse-spark-1.3-oauth-btn')).toBeInTheDocument());

    // 1. Clearing factory cache_read rate sends both cached_input: null and cache_read: null
    fireEvent.click(screen.getByTestId('edit-model-muse-spark-1.3-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    expect((screen.getByTestId('edit-model-notional-cached') as HTMLInputElement).value).toBe('0.5');

    fireEvent.change(screen.getByTestId('edit-model-notional-cached'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1]).toEqual({
      dalton_meta: {
        notional_pricing: {
          cached_input: null,
          cache_read: null,
        },
      },
      expected_revision: 7,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Updating factory cache_read rate to a new number sends only cached_input
    fireEvent.click(screen.getByTestId('edit-model-muse-spark-1.3-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-notional-cached'), { target: { value: '0.25' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      dalton_meta: {
        notional_pricing: {
          cached_input: 0.25,
        },
      },
      expected_revision: 7,
      comment: undefined,
    });
  });

  it('treats tiered-only cash pricing tables like grok-4.7 as existing for price-source-only and output-only edits', async () => {
    const tieredOverview = makeOverview({
      revision: 7,
      models: [
        {
          model_key: 'grok-4.7',
          model_id: 'grok-4.7',
          provider: 'xai',
          provider_display_name: 'xAI',
          provider_connected: true,
          catalog_defined_provider: false,
          display_name: 'Grok 4.7',
          visible: true,
          source: 'factory',
          status: 'visible',
          intelligence: 5,
          speed: 4,
          plain_language: 4,
          context: 2000000,
          compaction_profile: 'maximal',
          input_modalities: ['text', 'image'],
          reasoning: null,
          pricing: {
            input_tiers: [
              { max_tokens: 200000, rate: 2.0, cached_input: 0.5 },
              { max_tokens: null, rate: 4.0, cached_input: 1.0 },
            ],
            output_tiers: [
              { max_tokens: 200000, rate: 6.0 },
              { max_tokens: null, rate: 12.0 },
            ],
          },
          pricing_meta: null,
          dalton_meta: { family: 'xai-grok', billing: 'cash', tool_calling: true },
        },
      ],
    });
    h.fetchTeamModelCatalog.mockResolvedValue(tieredOverview);
    h.updateTeamCatalogModel
      .mockResolvedValueOnce({ revision: 8, model_key: 'grok-4.7' })
      .mockResolvedValueOnce({ revision: 9, model_key: 'grok-4.7' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-grok-4.7-btn')).toBeInTheDocument());

    // 1. Price-source-only edit saves pricing_meta without dropping the change or emitting pricing
    fireEvent.click(screen.getByTestId('edit-model-grok-4.7-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-price-source-select'), {
      target: { value: 'third_party' },
    });
    fireEvent.change(screen.getByTestId('edit-model-price-verified-at'), {
      target: { value: '2026-11-26' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1]).toEqual({
      pricing_meta: {
        source_kind: 'third_party',
        verified_at: '2026-11-26',
      },
      expected_revision: 7,
      comment: undefined,
    });
    await waitFor(() => expect(screen.queryByTestId('team-model-edit-sheet')).toBeNull());

    // 2. Output-only edit submits ONLY { pricing: { output: 14 } } with no input: 0 and no pricing_meta
    fireEvent.click(screen.getByTestId('edit-model-grok-4.7-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.change(screen.getByTestId('edit-model-cash-output'), { target: { value: '14' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      pricing: { output: 14 },
      expected_revision: 7,
      comment: undefined,
    });
  });

  it('submits explicit context null when clearing context window in edit mode', async () => {
    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    expect((screen.getByTestId('edit-model-context-input') as HTMLInputElement).value).toBe('400000');

    fireEvent.change(screen.getByTestId('edit-model-context-input'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(h.updateTeamCatalogModel.mock.calls[0][1]).toEqual({
      context: null,
      expected_revision: 7,
      comment: undefined,
    });
  });

  it('preserves confirmedLastCurated across a failed catalog reread retry after 409 conflict', async () => {
    const initial = makeOverview({ revision: 7 });
    const bobRenamedAtV9 = makeOverview({
      revision: 9,
      models: [
        initial.models[0],
        {
          ...initial.models[1],
          display_name: 'Gemini 4 Neon Renamed By Bob',
        },
      ],
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockRejectedValueOnce(new Error('Transient network failure'))
      .mockResolvedValueOnce(bobRenamedAtV9)
      .mockResolvedValue(bobRenamedAtV9);

    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gemini-4-neon-high-oauth' });

    renderCard();
    await waitFor(() => expect(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('edit-model-gemini-4-neon-high-oauth-btn'));
    await screen.findByTestId('team-model-edit-sheet');
    fireEvent.click(screen.getByTestId('edit-model-curated-toggle'));
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));
    fireEvent.click(await screen.findByTestId('edit-sheet-last-curated-confirm-btn'));

    // First reread fails -> retry refresh button appears
    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('edit-sheet-conflict-refresh-failed')).toBeInTheDocument();

    // Retry reread succeeds -> clicking conflict resubmit MUST NOT ask for last-curated confirmation again
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-refresh-btn'));
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId('edit-sheet-last-curated-confirm')).toBeNull();
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      curated_setup_member: false,
      expected_revision: 9,
      comment: undefined,
    });
  });

  it('preserves draft, revision, and pending conflict reread state when crossing the mobile layout breakpoint', async () => {
    const initial = makeOverview({ revision: 7 });
    const bobUpdated = makeOverview({
      revision: 9,
      models: [
        {
          ...initial.models[0],
          pricing: { input: 3.5, cached_input: 0.25, output: 10, unit: 'per_1m_tokens' },
        },
        initial.models[1],
      ],
    });
    let resolveReread!: (value: TeamCatalogOverviewResponse) => void;
    const deferredReread = new Promise<TeamCatalogOverviewResponse>((resolve) => {
      resolveReread = resolve;
    });
    h.fetchTeamModelCatalog
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(deferredReread)
      .mockResolvedValue(bobUpdated);
    h.updateTeamCatalogModel
      .mockRejectedValueOnce(
        makeConflictError({
          code: 'revision_conflict',
          message: 'Expected revision 7, current is 9.',
          expected_revision: 7,
          current_revision: 9,
        }),
      )
      .mockResolvedValueOnce({ revision: 10, model_key: 'gpt-5' });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
    });
    activeQueryClients.push(qc);
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <TeamModelCatalogCard />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('edit-model-gpt-5-btn')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('edit-model-gpt-5-btn'));
    await screen.findByTestId('team-model-edit-sheet');

    fireEvent.change(screen.getByTestId('edit-model-display-name-input'), {
      target: { value: 'GPT-5 Draft Across Breakpoint' },
    });
    fireEvent.click(screen.getByTestId('edit-sheet-save-btn'));

    expect(await screen.findByTestId('edit-sheet-conflict-syncing')).toBeInTheDocument();
    expect(screen.getByTestId('edit-sheet-save-btn')).toBeDisabled();

    // Cross desktop -> mobile breakpoint while catalog reread is still pending
    h.isMobile = true;
    rerender(
      <QueryClientProvider client={qc}>
        <TeamModelCatalogCard />
      </QueryClientProvider>,
    );

    // Draft, conflict reread notice, and disabled save state MUST all survive layout switch
    expect(screen.getByTestId('edit-sheet-conflict-syncing')).toBeInTheDocument();
    expect(screen.getByTestId('edit-sheet-save-btn')).toBeDisabled();
    expect((screen.getByTestId('edit-model-display-name-input') as HTMLInputElement).value).toBe(
      'GPT-5 Draft Across Breakpoint',
    );

    // Resolve reread and retry -> submits preserved draft against refreshed revision 9
    resolveReread(bobUpdated);
    await waitFor(() => expect(screen.getByTestId('edit-sheet-conflict-resubmit-btn')).toBeEnabled());
    fireEvent.click(screen.getByTestId('edit-sheet-conflict-resubmit-btn'));

    await waitFor(() => expect(h.updateTeamCatalogModel).toHaveBeenCalledTimes(2));
    expect(h.updateTeamCatalogModel.mock.calls[1][1]).toEqual({
      display_name: 'GPT-5 Draft Across Breakpoint',
      expected_revision: 9,
      comment: undefined,
    });
  });
});
