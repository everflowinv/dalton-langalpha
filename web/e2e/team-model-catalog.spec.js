/**
 * Team Model Catalog Batch 2 Playwright E2E acceptance suite:
 * 1. Admin saves a new model in Settings -> immediately selectable in the current session;
 *    an idle foreground member session (zero interaction, controllable clock) proactively
 *    refetches `/api/v1/models` within the 5-minute `refetchInterval` ceiling and picks up
 *    the new model.
 * 2. Hiding or retiring default curated models aligns `/setup/model` admission and
 *    `/setup/default` recommendations via server `dalton_setup_candidates` /
 *    `dalton_curated_providers`, and empty curated sets honor Everflow vs Core contracts.
 * 3. Core capability gating hides all `*-oauth` models/providers, and the `375x812` mobile
 *    viewport renders the single-column card stream + full-screen drawer with zero horizontal
 *    overflow or clipped sticky footer.
 */
import { mockAPI, resetMockServer, test, expect } from './fixtures.js';

function makeAdminOverrides({
  capabilities = ['native_oauth_sync'],
  profile = 'everflow',
  catalogState,
  modelsState,
}) {
  return {
    'GET /dalton/capabilities': {
      profile,
      capabilities,
      brand_name: profile === 'core' ? 'Dalton' : 'Everflow',
      ticker_logos: false,
    },
    'GET /dalton/credentials/status': {
      sharing: 'team',
      enabled: true,
      holder_user_id: 'local-dev-user',
      credential_user_id: 'local-dev-user',
      is_pm: true,
      can_disconnect: true,
      can_invite: true,
      caller_tier: 'admin',
      auth_mode: 'oss',
    },
    'GET /dalton/providers/status': {
      generated_at: '2026-11-25T10:00:00Z',
      providers: [],
      role_warnings: [],
    },
    'GET /dalton/native-models/status': {
      providers: [],
      diff: { added: [], deprecated: [], unchanged: 0 },
      retired: [],
      overlay: { exists: false, model_count: 0, path: '' },
    },
    'GET /users/me/api-keys': {
      byok_enabled: true,
      providers: [
        {
          provider: 'openai',
          display_name: 'OpenAI',
          access_type: 'api_key',
          has_key: true,
          masked_key: 'sk-...1234',
        },
      ],
    },
    'GET /models': async (route) => {
      modelsState.fetchCount += 1;
      const snapshot = modelsState.current();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: {
          'X-Dalton-Catalog-Revision': String(snapshot.catalog_revision ?? 1),
        },
        body: JSON.stringify(snapshot),
      });
    },
    'GET /dalton/team-models': async (route) => {
      const snapshot = catalogState.current();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: {
          'X-Dalton-Catalog-Revision': String(snapshot.revision),
        },
        body: JSON.stringify(snapshot),
      });
    },
    'POST /dalton/team-models': async (route) => {
      const body = route.request().postDataJSON();
      const created = catalogState.onCreate(body);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: {
          'X-Dalton-Catalog-Revision': String(created.revision),
        },
        body: JSON.stringify(created),
      });
    },
  };
}

test.describe('Team Model Catalog Batch 2 E2E', () => {
  test.beforeEach(async () => {
    await resetMockServer();
  });

  test('admin adds model -> immediately visible; idle foreground member refetches within 5-minute refetchInterval', async ({
    page,
    context,
  }) => {
    let rev = 1;
    const extraModels = [];
    const catalogState = {
      current: () => ({
        revision: rev,
        payload_sha256: `sha-${rev}`,
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
            source: 'factory',
            status: 'visible',
            intelligence: 5,
            speed: 4,
            plain_language: 4,
            context: 400000,
            compaction_profile: 'spacious',
            input_modalities: ['text', 'image'],
            reasoning: null,
            pricing: { input: 2.5, output: 10, unit: 'per_1m_tokens' },
            pricing_meta: { source_kind: 'official', estimated: false },
            dalton_meta: { family: 'openai-gpt', billing: 'cash', tool_calling: true },
          },
          ...extraModels,
        ],
        retired: [],
        provider_variants: [],
        curated_setup: null,
        dalton_setup_candidates: ['gpt-5', ...extraModels.map((m) => m.model_key)],
        dalton_curated_providers: ['openai'],
      }),
      onCreate: (body) => {
        rev += 1;
        extraModels.push({
          model_key: body.model_key,
          model_id: body.model_id,
          provider: body.provider,
          provider_display_name: 'OpenAI',
          provider_connected: true,
          catalog_defined_provider: false,
          display_name: body.display_name || body.model_key,
          visible: body.visible !== false,
          source: 'team_added',
          status: 'visible',
          intelligence: body.intelligence ?? 5,
          speed: body.speed ?? 4,
          plain_language: body.plain_language ?? 4,
          context: body.context ?? 400000,
          compaction_profile: body.compaction_profile ?? 'spacious',
          input_modalities: body.input_modalities ?? ['text'],
          reasoning: body.reasoning ?? null,
          pricing: body.pricing ?? { input: 1.4, output: 4.4, unit: 'per_1m_tokens' },
          pricing_meta: body.pricing_meta ?? { source_kind: 'official', estimated: false },
          dalton_meta: body.dalton_meta ?? { billing: 'cash', tool_calling: true },
        });
        return { revision: rev, model_key: body.model_key };
      },
    };

    const adminModelsState = {
      fetchCount: 0,
      current: () => ({
        team_catalog_enabled: true,
        catalog_revision: rev,
        dalton_profile: 'core',
        dalton_setup_candidates: ['gpt-5', ...extraModels.map((m) => m.model_key)],
        dalton_curated_providers: ['openai'],
        models: {
          openai: {
            display_name: 'OpenAI',
            models: ['gpt-5', ...extraModels.map((m) => m.model_key)],
          },
        },
        model_metadata: Object.fromEntries(
          catalogState.current().models.map((m) => [
            m.model_key,
            {
              display_name: m.display_name,
              provider: m.provider,
              model_id: m.model_id,
              tool_calling: true,
              intelligence: m.intelligence,
              speed: m.speed,
            },
          ]),
        ),
        system_defaults: { default_model: 'gpt-5' },
        compaction_profiles: {},
        search_providers: {},
        provider_catalog: [
          { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key', sdk: 'openai', byok_eligible: true },
        ],
        key_candidates: {},
      }),
    };

    // 1. Open an idle foreground member page on Settings -> Model (with model selector visible)
    // using a controllable clock BEFORE the admin adds a model.
    const memberPage = await context.newPage();
    await memberPage.clock.install();
    const memberModelsState = {
      fetchCount: 0,
      current: () => adminModelsState.current(),
    };
    await mockAPI(
      memberPage,
      makeAdminOverrides({
        profile: 'core',
        capabilities: [],
        catalogState,
        modelsState: memberModelsState,
      }),
    );
    await memberPage.goto('/settings?tab=models');
    await expect.poll(() => memberModelsState.fetchCount).toBeGreaterThanOrEqual(1);
    await expect(memberPage.locator('select option', { hasText: 'GPT-5' }).first()).toBeAttached();
    await expect(memberPage.locator('select option', { hasText: 'GPT-5 Research Preview' })).toHaveCount(0);
    const initialMemberFetches = memberModelsState.fetchCount;

    // 2. Admin saves a new model in Settings -> immediately visible in admin catalog AND selectable in model picker
    await mockAPI(
      page,
      makeAdminOverrides({
        profile: 'core',
        capabilities: [],
        catalogState,
        modelsState: adminModelsState,
      }),
    );
    await page.goto('/settings?tab=teamModels#dalton-team-model-catalog');
    await expect(page.getByTestId('dalton-team-model-catalog-card')).toBeVisible();
    await page.getByTestId('team-catalog-add-model-btn').click();
    await expect(page.getByTestId('team-model-edit-sheet')).toBeVisible();

    await page.getByTestId('edit-model-provider-select').selectOption('openai');
    await page.getByTestId('edit-model-key-input').fill('gpt-5-research-preview');
    await page.getByTestId('edit-model-display-name-input').fill('GPT-5 Research Preview');
    await page.getByTestId('edit-model-id-input').fill('gpt-5-research-preview');
    await page.getByTestId('edit-model-cash-input').fill('2.0');
    await page.getByTestId('edit-model-cash-output').fill('8.0');
    await page.getByTestId('edit-sheet-save-btn').click();

    await expect(page.getByTestId('catalog-model-row-gpt-5-research-preview')).toBeVisible();

    // Admin switches to Settings -> Model tab and confirms the new model is immediately selectable
    await page.goto('/settings?tab=models');
    await expect(page.locator('select option', { hasText: 'GPT-5 Research Preview' }).first()).toBeAttached();

    // 3. Keep memberPage completely idle in the foreground (zero clicks/keys/focus) and fast-forward 5 minutes
    await memberPage.clock.fastForward(5 * 60_000 + 1_000);
    await expect.poll(() => memberModelsState.fetchCount).toBeGreaterThan(initialMemberFetches);
    await expect(
      memberPage.locator('select option', { hasText: 'GPT-5 Research Preview' }).first(),
    ).toBeAttached();
    await memberPage.close();
  });

  test('setup entrypoints respect server dalton_setup_candidates and empty curated fallback in Everflow vs Core', async ({
    page,
  }) => {
    const setupBaseMocks = (profile, daltonSetupCandidates, daltonCuratedProviders) => ({
      'GET /users/me': {
        user_id: 'local-dev-user',
        name: 'Member',
        email: 'member@example.com',
        onboarding_completed: false,
        has_api_key: true,
        has_oauth_token: false,
      },
      'GET /users/me/onboarding': {
        models: 'langalpha_models',
        runs_on: 'cloud',
        setup_step: 'model',
        setup_completed_at: null,
      },
      'GET /dalton/capabilities': {
        profile,
        capabilities: profile === 'everflow' ? ['native_oauth_sync'] : [],
        brand_name: profile === 'core' ? 'Dalton' : 'Everflow',
      },
      'GET /dalton/credentials/status': {
        sharing: 'team',
        enabled: true,
        holder_user_id: 'admin-1',
        credential_user_id: 'admin-1',
        is_pm: false,
        can_disconnect: false,
        caller_tier: 'internal',
        auth_mode: 'local',
      },
      'GET /users/me/api-keys': {
        byok_enabled: true,
        providers: [
          {
            provider: 'openai',
            display_name: 'OpenAI',
            access_type: 'api_key',
            has_key: true,
            masked_key: 'sk-...1234',
          },
        ],
      },
      'GET /providers/openai/available-models': {
        provider: 'openai',
        status: 'ok',
        fetched_at: null,
        ready: ['gpt-5'],
        missing: [],
        context_windows: {},
        others: [],
        total: 1,
        cached: false,
      },
      'GET /models': {
        team_catalog_enabled: true,
        catalog_revision: 12,
        dalton_profile: profile,
        dalton_setup_candidates: daltonSetupCandidates,
        dalton_curated_providers: daltonCuratedProviders,
        models: {
          openai: { display_name: 'OpenAI', models: ['gpt-5'] },
        },
        model_metadata: {
          'gpt-5': {
            display_name: 'GPT-5',
            provider: 'openai',
            model_id: 'gpt-5',
            tool_calling: true,
            intelligence: 5,
            speed: 4,
          },
        },
        system_defaults: {},
        compaction_profiles: {},
        search_providers: {},
        provider_catalog: [
          { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key', sdk: 'openai', byok_eligible: true },
        ],
        key_candidates: {},
      },
    });

    // 1) Everflow with narrowed server curated list (gemini-4-neon-high-oauth hidden -> only gpt-6.2-sol-oauth remains)
    await mockAPI(page, {
      ...setupBaseMocks('everflow', ['gpt-6.2-sol-oauth'], ['codex-oauth']),
      'GET /oauth/codex/status': { connected: true, state: 'connected' },
      'GET /oauth/antigravity/status': { connected: true, state: 'connected' },
      'GET /providers/codex-oauth/available-models': {
        provider: 'codex-oauth',
        status: 'ok',
        fetched_at: null,
        ready: ['gpt-6.2-sol-oauth'],
        missing: [],
        context_windows: {},
        others: [],
        total: 1,
        cached: false,
      },
      'GET /providers/antigravity-oauth/available-models': {
        provider: 'antigravity-oauth',
        status: 'ok',
        fetched_at: null,
        ready: ['gemini-4-neon-high-oauth'],
        missing: [],
        context_windows: {},
        others: [],
        total: 1,
        cached: false,
      },
      'GET /models': {
        team_catalog_enabled: true,
        catalog_revision: 11,
        dalton_profile: 'everflow',
        dalton_setup_candidates: ['gpt-6.2-sol-oauth'],
        dalton_curated_providers: ['codex-oauth'],
        models: {
          'codex-oauth': { display_name: 'Codex', models: ['gpt-6.2-sol-oauth'] },
          'antigravity-oauth': { display_name: 'Antigravity', models: ['gemini-4-neon-high-oauth'] },
        },
        model_metadata: {
          'gpt-6.2-sol-oauth': {
            display_name: 'GPT-6.2 Sol',
            provider: 'codex-oauth',
            model_id: 'gpt-6.2-sol',
            tool_calling: true,
            intelligence: 5,
            speed: 3,
          },
          'gemini-4-neon-high-oauth': {
            display_name: 'Gemini 4 Neon',
            provider: 'antigravity-oauth',
            model_id: 'gemini-4-neon',
            tool_calling: true,
            intelligence: 5,
            speed: 4,
          },
        },
        system_defaults: {},
        compaction_profiles: {},
        search_providers: {},
        provider_catalog: [
          { provider: 'codex-oauth', display_name: 'Codex', access_type: 'oauth', sdk: 'openai', byok_eligible: false },
          { provider: 'antigravity-oauth', display_name: 'Antigravity', access_type: 'oauth', sdk: 'gemini', byok_eligible: false },
        ],
        key_candidates: {},
      },
    });
    await page.goto('/setup/default');
    await expect(page.getByText('GPT-6.2 Sol').first()).toBeVisible();
    await expect(page.getByText('Gemini 4 Neon')).toHaveCount(0);

    // 2) Everflow with empty curated list -> stays blocked on /setup/model with team-models-empty notice
    await mockAPI(page, setupBaseMocks('everflow', [], []));
    await page.goto('/setup/model');
    await expect(page.getByTestId('setup-model-team-models-empty')).toBeVisible();

    // 3) Core with empty curated list -> falls back to connected tool-calling model (GPT-5) on /setup/default
    await mockAPI(page, setupBaseMocks('core', [], []));
    await page.goto('/setup/default');
    await expect(page.getByText('GPT-5').first()).toBeVisible();
  });

  test('Core profile exposes zero *-oauth items and 375x812 mobile layout has zero overflow or clipped controls', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 375, height: 812 });

    const catalogState = {
      current: () => ({
        revision: 4,
        payload_sha256: 'sha-4',
        legacy_sync_import_status: 'not_applicable',
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
            source: 'factory',
            status: 'visible',
            intelligence: 5,
            speed: 4,
            plain_language: 4,
            context: 400000,
            compaction_profile: 'spacious',
            input_modalities: ['text', 'image', 'pdf'],
            reasoning: { efforts: ['low', 'medium', 'high'], default: 'medium' },
            pricing: { input: 2.5, output: 10, unit: 'per_1m_tokens' },
            pricing_meta: { source_kind: 'official', estimated: false },
            dalton_meta: { family: 'openai-gpt', billing: 'cash', tool_calling: true },
          },
          {
            model_key: 'gemini-4-neon-high-oauth',
            model_id: 'gemini-4-neon',
            provider: 'antigravity-oauth',
            provider_display_name: 'Antigravity',
            provider_connected: false,
            catalog_defined_provider: false,
            display_name: 'Gemini 4 Neon OAuth',
            visible: true,
            source: 'factory',
            status: 'visible',
            intelligence: 5,
            speed: 4,
            plain_language: 5,
            context: 1000000,
            compaction_profile: 'expansive',
            input_modalities: ['text'],
            reasoning: null,
            pricing: null,
            pricing_meta: null,
            dalton_meta: { billing: 'subscription', tool_calling: true },
          },
        ],
        retired: [],
        provider_variants: [],
        curated_setup: null,
        dalton_setup_candidates: ['gpt-5'],
        dalton_curated_providers: ['openai'],
      }),
      onCreate: (b) => ({ revision: 5, model_key: b.model_key }),
    };

    const modelsState = {
      fetchCount: 0,
      current: () => ({
        team_catalog_enabled: true,
        catalog_revision: 4,
        dalton_profile: 'core',
        dalton_setup_candidates: ['gpt-5'],
        dalton_curated_providers: ['openai'],
        models: { openai: { display_name: 'OpenAI', models: ['gpt-5'] } },
        model_metadata: {
          'gpt-5': { display_name: 'GPT-5', provider: 'openai', tool_calling: true },
        },
        system_defaults: {},
        compaction_profiles: {},
        search_providers: {},
        provider_catalog: [
          { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key', sdk: 'openai', byok_eligible: true },
        ],
        key_candidates: {},
      }),
    };

    await mockAPI(
      page,
      makeAdminOverrides({
        profile: 'core',
        capabilities: [],
        catalogState,
        modelsState,
      }),
    );

    await page.goto('/settings?tab=teamModels#dalton-team-model-catalog');
    await expect(page.getByTestId('team-catalog-mobile-cards')).toBeVisible();
    await expect(page.getByTestId('catalog-model-card-gpt-5')).toBeVisible();

    // Zero *-oauth exposure in Core
    const cardText = await page.getByTestId('dalton-team-model-catalog-card').innerText();
    expect(cardText).not.toMatch(/-oauth/i);

    // Zero horizontal overflow at 375x812
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);

    // Open full-screen mobile edit sheet and verify sticky footer fits within 812px viewport
    await page.getByTestId('mobile-edit-gpt-5-btn').click();
    const sheet = page.getByTestId('team-model-edit-sheet');
    await expect(sheet).toBeVisible();
    await expect(sheet).toHaveAttribute('data-sheet', '');

    const footerBox = await page.getByTestId('edit-sheet-footer').boundingBox();
    expect(footerBox).not.toBeNull();
    expect(footerBox.y + footerBox.height).toBeLessThanOrEqual(813);
    expect(footerBox.x + footerBox.width).toBeLessThanOrEqual(376);

    // Focus an input and simulate soft keyboard shrinking visual viewport to 500px
    await page.getByTestId('edit-model-display-name-input').focus();
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--visual-viewport-height', '500px');
      document.documentElement.style.setProperty('--visual-viewport-bottom-offset', '312px');
    });
    const footerKeyboardBox = await page.getByTestId('edit-sheet-footer').boundingBox();
    expect(footerKeyboardBox).not.toBeNull();
    expect(footerKeyboardBox.y + footerKeyboardBox.height).toBeLessThanOrEqual(501);
  });
});
