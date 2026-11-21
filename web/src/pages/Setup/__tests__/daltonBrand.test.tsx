/**
 * Under a Dalton build (`VITE_BRAND_NAME=Everflow`, `VITE_BRAND_MARK=everflow`),
 * rendering the Setup wizard steps, Mac pairing prompts, Onboarding cards, and
 * intro scenes in both English and Chinese must never expose hardcoded
 * "LangAlpha" or "Ginlix" copy or render the upstream LangAlpha SVG mark.
 */
import { Suspense } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

const h = vi.hoisted(() => ({
  models: 'claude_code' as 'claude_code' | 'subscription_or_key' | 'langalpha_models',
  runsOn: 'own_computer' as 'cloud' | 'own_computer',
  connectKind: 'api_key' as 'api_key' | 'oauth' | 'custom',
  platform: false,
  desktop: false,
  canPairMac: false,
  gettingStartedTasks: [] as Array<{ def: Record<string, unknown>; done: boolean }>,
}));

vi.mock('@/config/hostMode', () => ({
  get isPlatformMode() {
    return h.platform;
  },
  get HOST_MODE() {
    return h.platform ? 'platform' : 'oss';
  },
  APP_ENTRY_PATH: '/dashboard',
}));

vi.mock('@/lib/desktop', () => ({
  isDesktopShell: () => h.desktop,
  canRemoteControl: () => h.canPairMac,
  canAddThisMac: () => h.desktop && h.canPairMac,
  get desktop() {
    return h.desktop ? { platform: 'darwin' } : undefined;
  },
  getRemoteControl: async () => ({
    supported: true,
    service: 'off',
    helper: null,
    paired: false,
    fingerprint: null,
    account: 'tester',
  }),
  enableRemoteControl: vi.fn(async () => ({ paired: false, account: 'tester' })),
  disableRemoteControl: vi.fn(async () => undefined),
  openConsolePage: vi.fn(),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: {
      user_id: 'u-ext-1',
      email: 'ext@example.com',
      access_tier: -1,
      deployment_serves: true,
      computer_choice: null,
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

vi.mock('@/hooks/useAccount', async () => {
  const { allows } = await import('@/lib/accountRules');
  const who = { start: 'Your organization', mid: 'your organization' };
  return {
    useAccount: () => ({ policy: null, setterName: () => who }),
    useRule: (rule: Parameters<typeof allows>[1]) => ({ allowed: allows(null, rule), who }),
  };
});

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '团队共用：在这里连接后全队都能使用',
  useTeamCredentials: () => ({
    isShared: false,
    canDisconnect: true,
    canManage: true,
    isPm: false,
    canInvite: false,
  }),
}));

vi.mock('@/hooks/useFeatures', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useFeatures')>()),
  useFeatures: () => ({
    data: [{ key: 'claude_code_runtime', enabled: true }],
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useSetFeatureOverride: () => ({ mutate: vi.fn(), isPending: false, isError: false, isSuccess: false }),
}));

vi.mock('@/hooks/useClaudeCode', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useClaudeCode')>()),
  useClaudeCodeSetup: () => ({
    setup: {
      state: 'no_computer',
      computers: [],
      offered: true,
      enabled: false,
      hosted: { available: false, reason: null, mode: 'stored' },
    },
    isLoading: false,
    unread: false,
    retry: vi.fn(),
  }),
  updateComputers: vi.fn(),
}));

vi.mock('@/hooks/useClaudeCodeCustody', () => ({
  useClaudeCodeCustody: () => ({
    custody: { offered: true, mode: 'stored', token: null, computers: [] },
    choose: vi.fn(),
    choosing: null,
    isLoading: false,
    unread: false,
    retry: vi.fn(),
  }),
}));

vi.mock('@/hooks/useConfiguredProviders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useConfiguredProviders')>()),
  useConfiguredProviders: () => ({ providers: [], hasAny: false }),
}));

vi.mock('@/hooks/useProviderCatalog', () => ({
  useProviderCatalog: () => ({
    catalog: [],
    keyGroups: [],
    planGroups: [],
    localEntries: [],
    entryFor: () => undefined,
    placeOf: () => undefined,
    optionFor: () => null,
    connectionOf: () => undefined,
    isLoading: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: {}, isLoading: false, unread: false, retry: vi.fn() }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: {
      'codex-oauth': { models: ['gpt-5'] },
      'claude-code': { models: ['cc-opus'] },
      anthropic: { models: ['claude-sonnet'] },
    },
    slotModels: {
      'codex-oauth': { models: ['gpt-5'] },
      anthropic: { models: ['claude-sonnet'] },
    },
    payers: { 'gpt-5': 'credits', 'cc-opus': 'subscription:claude-code' },
    metadata: {
      'gpt-5': { display_name: 'GPT 5', intelligence: 5, speed: 3 },
      'cc-opus': {
        display_name: 'Claude Opus',
        sdk: 'claude-code',
        provider: 'claude-code',
        model_id: 'claude-opus',
        intelligence: 5,
        speed: 3,
      },
      'claude-sonnet': { display_name: 'Claude Sonnet', intelligence: 4, speed: 4 },
    },
    customModels: [],
    cannotRun: () => false,
    systemDefaults: { default: 'claude-sonnet', background: 'gpt-5' },
    isLoading: false,
    unread: false,
    retry: vi.fn(),
  }),
}));

vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () => ({
    queryKey: ['home'],
    queryFn: async () => ({ workspace_id: 'ws-home', status: 'running' }),
  }),
}));

vi.mock('@/hooks/useOnboardingProgress', () => ({
  useOnboardingProgress: () => ({
    progress: {
      models: h.models,
      runs_on: h.runsOn,
      plan_step_done_at: '2026-11-20T00:00:00Z',
      this_mac: null,
      setup_on: null,
      setup_step: null,
      setup_completed_at: null,
    },
    isLoading: false,
    isError: false,
    update: vi.fn(),
    isUpdating: false,
  }),
}));

vi.mock('@/pages/ChatAgent/hooks/useComputers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/pages/ChatAgent/hooks/useComputers')>()),
  useComputerProviders: () => ({
    data: { providers: [{ kind: 'ssh', allowed: true }] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useComputers: () => ({ data: { computers: [] }, isLoading: false }),
}));

vi.mock('@/pages/ChatAgent/hooks/useHomePlacement', () => ({
  useHomePlacement: () => ({ data: undefined }),
}));

vi.mock('@/pages/ChatAgent/hooks/useRemoteControl', () => ({
  useThisMacAvailability: () => ({
    offered: h.canPairMac,
    added: false,
    settled: true,
    serverFailed: false,
    retryServer: vi.fn(),
  }),
  serverTakesRemoteControl: () => h.canPairMac,
}));

vi.mock('@/pages/ChatAgent/components/sshSetupState', () => ({
  useSshSetup: () => ({ create: { host: 'lab' }, ready: false, readyStep: { filesBackup: true }, defaultName: null }),
}));

vi.mock('@/pages/ChatAgent/components/sshSetup', () => ({
  SshSetup: () => <div>ssh-setup</div>,
}));

vi.mock('@/pages/Setup/answers/flow', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/pages/Setup/answers/flow')>()),
  useAnswerFlow: () => ({
    isLoading: false,
    answers: { models: h.models, runsOn: h.runsOn },
    first: true,
    ownComputers: [],
    facts: {
      providerConnected: true,
      hasProvider: true,
      claudeCodeReady: false,
      ownComputerCount: 0,
    },
    rail: ['use', 'computer', 'model', 'default'],
    next: 'default',
    nextPath: '/setup/default',
    unread: false,
    retry: vi.fn(),
    finish: vi.fn(),
    finishing: false,
    finishFailed: false,
    back: vi.fn(),
  }),
  useChangeAnswers: () => vi.fn(),
}));

vi.mock('@/pages/Setup/answers/useSetupFacts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/pages/Setup/answers/useSetupFacts')>()),
  useSetupFacts: () => ({
    isLoading: false,
    progress: {
      models: h.models,
      runs_on: h.runsOn,
      plan_step_done_at: '2026-11-20T00:00:00Z',
      this_mac: null,
      setup_on: 'mac_pending',
      setup_step: null,
      setup_completed_at: null,
    },
    answers: { models: h.models, runsOn: h.runsOn },
    ownComputers: [],
    facts: {},
    rail: ['computer', 'model', 'default'],
    next: 'default',
    nextPath: '/setup/default',
  }),
}));

vi.mock('@/pages/Setup/answers/connectedDefault', () => ({
  useSaveConnectedDefault: () => ({
    modelFor: () => null,
    settled: () => true,
    save: vi.fn(async () => true),
    saving: false,
  }),
}));

vi.mock('@/pages/Onboarding/OnboardingProvider', () => ({
  useOnboarding: () => ({
    gettingStarted: {
      visible: true,
      tasks: h.gettingStartedTasks,
      doneCount: 0,
      dismiss: vi.fn(),
      completeTask: vi.fn(),
    },
  }),
  useChatAppsPrompt: () => ({
    offered: true,
    threadId: null,
    pin: vi.fn(),
    dismiss: vi.fn(),
    connect: vi.fn(),
  }),
}));

vi.mock('@/pages/Onboarding/welcome/useWelcomeOffer', () => ({
  useWelcomeOffer: () => ({
    hasDraft: false,
    label: 'Start welcome',
    open: vi.fn(),
  }),
}));

vi.mock('@/hooks/useLinkedChatApps', () => ({
  deliveryOptionsQuery: () => ({
    queryKey: ['delivery-options'],
    queryFn: async () => ({ enabled: true, apps: {} }),
  }),
}));

const FORBIDDEN_BRAND = /langalpha|ginlix/i;

function assertNoUpstreamBrand(container: HTMLElement, label: string, locale: string) {
  const clone = container.cloneNode(true) as HTMLElement;
  // Exclude decorative CLI command inside MachineArt SVG (`$ langalpha setup`, role="img" exposes aria-label).
  clone.querySelectorAll('.ma-term').forEach((el) => el.remove());
  const text = clone.textContent ?? '';
  expect(text, `[${locale}] ${label} leaked upstream brand text: ${text}`).not.toMatch(FORBIDDEN_BRAND);
  container.querySelectorAll('[aria-label]').forEach((el) => {
    const aria = el.getAttribute('aria-label') ?? '';
    expect(aria, `[${locale}] ${label} aria-label leaked upstream brand: ${aria}`).not.toMatch(FORBIDDEN_BRAND);
  });
}

describe('Dalton brand rendering across Setup and Onboarding', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();
  });

  afterEach(async () => {
    cleanup();
    vi.unstubAllEnvs();
    vi.resetModules();
    const [{ initI18n }, { default: enUS }, { default: zhCN }, { default: introsEn }, { default: introsZh }, { default: thisMacEn }, { default: thisMacZh }] =
      await Promise.all([
        import('@/i18n'),
        import('@/locales/en-US.json'),
        import('@/locales/zh-CN.json'),
        import('@/locales/intros/en-US.json'),
        import('@/locales/intros/zh-CN.json'),
        import('@/locales/thisMacFirstRun/en-US.json'),
        import('@/locales/thisMacFirstRun/zh-CN.json'),
      ]);
    await initI18n({
      'en-US': { translation: enUS, intros: introsEn, thisMacFirstRun: thisMacEn },
      'zh-CN': { translation: zhCN, intros: introsZh, thisMacFirstRun: thisMacZh },
    });
  });

  it.each(['en-US', 'zh-CN'] as const)(
    'renders all Setup steps, Mac pairing views, Onboarding cards, and intro scenes without LangAlpha/Ginlix in %s',
    async (locale) => {
      const [
        { initI18n, default: i18n },
        { default: enUS },
        { default: zhCN },
        { default: introsEn },
        { default: introsZh },
        { default: thisMacEn },
        { default: thisMacZh },
        { renderWithProviders, createTestQueryClient },
        { render },
        { MemoryRouter, Route: DynamicRoute, Routes: DynamicRoutes },
        { QueryClientProvider },
        { BRAND_NAME, BRAND_MARK, isDaltonBuild },
        { default: UseStep },
        { default: ComputerStep },
        { default: SshStep },
        { default: ModelStep },
        { default: DefaultStep },
        { default: MethodStep },
        { default: ProviderStep },
        { default: ConnectStep },
        { default: ClaudeCodeStep },
        { default: DefaultsStep },
        { default: DoneStep },
        { default: MacHandoffStep },
        { default: ThisMacStep },
        { default: ContinueStep },
        { ThisMacFirstRunGate },
        { GettingStartedLauncher },
        { DEPLOYED_TASKS },
        { ChatAppsPromptCard },
        { WelcomeBanner },
        { PageIntroModal },
        { PAGE_INTROS },
        { loadIntroScenes, INTRO_VISUALS },
        { HandoffSlip },
        { ThisMacPairingBody },
      ] = await Promise.all([
        import('@/i18n'),
        import('@/locales/en-US.json'),
        import('@/locales/zh-CN.json'),
        import('@/locales/intros/en-US.json'),
        import('@/locales/intros/zh-CN.json'),
        import('@/locales/thisMacFirstRun/en-US.json'),
        import('@/locales/thisMacFirstRun/zh-CN.json'),
        import('@/test/utils'),
        import('@testing-library/react'),
        import('react-router'),
        import('@tanstack/react-query'),
        import('@/config/brand'),
        import('../steps/UseStep'),
        import('../steps/ComputerStep'),
        import('../steps/SshStep'),
        import('../steps/ModelStep'),
        import('../steps/DefaultStep'),
        import('../steps/MethodStep'),
        import('../steps/ProviderStep'),
        import('../steps/ConnectStep'),
        import('../steps/ClaudeCodeStep'),
        import('../steps/DefaultsStep'),
        import('../steps/DoneStep'),
        import('../mac/MacHandoffStep'),
        import('../mac/ThisMacStep'),
        import('../mac/ContinueStep'),
        import('@/pages/ThisMac/ThisMacFirstRunGate'),
        import('@/pages/Onboarding/engine/GettingStarted'),
        import('@/pages/Onboarding/registry'),
        import('@/pages/Onboarding/ChatAppsPromptCard'),
        import('@/pages/Onboarding/welcome/WelcomeBanner'),
        import('@/pages/Onboarding/engine/PageIntroModal'),
        import('@/pages/Onboarding/registry'),
        import('@/pages/Onboarding/engine/introScenes'),
        import('@/pages/Onboarding/engine/introScenes/chatParts'),
        import('../mac/ThisMacPairing'),
      ]);

      await initI18n({
        'en-US': { translation: enUS, intros: introsEn, thisMacFirstRun: thisMacEn },
        'zh-CN': { translation: zhCN, intros: introsZh, thisMacFirstRun: thisMacZh },
      });
      await i18n.changeLanguage(locale);

      expect(BRAND_NAME).toBe('Everflow');
      expect(BRAND_MARK).toBe('everflow');
      expect(isDaltonBuild).toBe(true);

      // 1. Setup Q&A & step routes
      const setupCases: Array<[string, () => { container: HTMLElement; unmount: () => void }]> = [
        [
          'UseStep',
          () => {
            h.models = 'claude_code';
            h.runsOn = 'own_computer';
            return renderWithProviders(<UseStep />);
          },
        ],
        [
          'ComputerStep (browser)',
          () => {
            h.desktop = false;
            h.canPairMac = true;
            return renderWithProviders(<ComputerStep />);
          },
        ],
        [
          'ComputerStep (mac update)',
          () => {
            h.desktop = true;
            h.canPairMac = false;
            return renderWithProviders(<ComputerStep />);
          },
        ],
        ['SshStep', () => renderWithProviders(<SshStep />)],
        [
          'ModelStep (claude_code)',
          () => {
            h.models = 'claude_code';
            return renderWithProviders(<ModelStep />);
          },
        ],
        [
          'ModelStep (subscription_or_key)',
          () => {
            h.models = 'subscription_or_key';
            return renderWithProviders(<ModelStep />);
          },
        ],
        [
          'DefaultStep (claude_code)',
          () => {
            h.models = 'claude_code';
            return renderWithProviders(<DefaultStep />);
          },
        ],
        ['MethodStep', () => renderWithProviders(<MethodStep />)],
        ['ProviderStep', () => renderWithProviders(<ProviderStep />)],
        [
          'ConnectStep (api_key)',
          () =>
            render(
              <QueryClientProvider client={createTestQueryClient()}>
                <MemoryRouter initialEntries={[{ pathname: '/setup/connect', state: { provider: 'anthropic', method: 'api_key' } }]}>
                  <DynamicRoutes>
                    <DynamicRoute path="/setup/connect" element={<ConnectStep />} />
                  </DynamicRoutes>
                </MemoryRouter>
              </QueryClientProvider>,
            ),
        ],
        ['ClaudeCodeStep', () => renderWithProviders(<ClaudeCodeStep />)],
        ['DefaultsStep', () => renderWithProviders(<DefaultsStep />)],
        ['DoneStep', () => renderWithProviders(<DoneStep />)],
        ['MacHandoffStep', () => renderWithProviders(<MacHandoffStep />)],
        [
          'ThisMacStep',
          () => {
            h.desktop = true;
            h.canPairMac = true;
            return renderWithProviders(<ThisMacStep />);
          },
        ],
        [
          'ThisMacPairingBody (approval)',
          () =>
            renderWithProviders(
              <ThisMacPairingBody
                account="tester"
                pairing={{
                  stage: 'approval',
                  elapsedSeconds: 0,
                  error: null,
                  computer: null,
                  home: null,
                  takeOver: null,
                  takeOverFolder: vi.fn(),
                  pair: vi.fn(),
                  retry: vi.fn(),
                }}
              />,
            ),
        ],
        [
          'ContinueStep',
          () => {
            h.desktop = true;
            h.canPairMac = true;
            return renderWithProviders(<ContinueStep />);
          },
        ],
        [
          'ThisMacFirstRunGate (suppressed in Dalton)',
          () => {
            h.desktop = true;
            h.canPairMac = true;
            return renderWithProviders(
              <ThisMacFirstRunGate>
                <div>gate-children</div>
              </ThisMacFirstRunGate>,
            );
          },
        ],
      ];

      for (const [name, mount] of setupCases) {
        const view = mount();
        assertNoUpstreamBrand(view.container, name, locale);
        if (name === 'ThisMacPairingBody (approval)') {
          expect(view.container.textContent).toContain('Everflow');
        }
        if (name === 'ThisMacFirstRunGate (suppressed in Dalton)') {
          expect(view.container.textContent).toContain('gate-children');
        }
        view.unmount();
      }

      // 2. Onboarding cards & intro scenes
      h.gettingStartedTasks = DEPLOYED_TASKS.map((def) => ({ def: def as unknown as Record<string, unknown>, done: false }));
      const launcher = renderWithProviders(<GettingStartedLauncher variant="row" />);
      assertNoUpstreamBrand(launcher.container, 'GettingStartedLauncher', locale);
      launcher.unmount();

      const chatApps = renderWithProviders(<ChatAppsPromptCard threadId="t1" active idle replied />);
      assertNoUpstreamBrand(chatApps.container, 'ChatAppsPromptCard', locale);
      chatApps.unmount();

      const banner = renderWithProviders(<WelcomeBanner onDismiss={vi.fn()} />);
      assertNoUpstreamBrand(banner.container, 'WelcomeBanner', locale);
      banner.unmount();

      // 3. chatParts HandoffSlip renders EverflowMark rather than LangAlphaMark
      const handoff = renderWithProviders(
        <HandoffSlip workspace="Research" title="Task" at={0} started={0} finished={1000} />,
      );
      assertNoUpstreamBrand(handoff.container, 'HandoffSlip', locale);
      expect(handoff.container.querySelector('g[transform="translate(1.5 0)"]')).not.toBeNull();
      expect(handoff.container.innerHTML).not.toContain('M38.0312');
      handoff.unmount();

      // 4. All PAGE_INTROS render cleanly with Everflow brand
      await loadIntroScenes(INTRO_VISUALS);
      for (const intro of PAGE_INTROS) {
        const modal = renderWithProviders(
          <Suspense fallback={null}>
            <PageIntroModal intro={intro} onClose={vi.fn()} onAbandon={vi.fn()} />
          </Suspense>,
        );
        assertNoUpstreamBrand(document.body, `PageIntroModal(${intro.id})`, locale);
        modal.unmount();
      }

      // 5. All interpolated setup / Mac / thisMacFirstRun / legal consent / approval keys resolve without upstream brand
      const namespacesAndPrefixes: Array<[string, Record<string, unknown>, string]> = [
        ['translation', (locale === 'zh-CN' ? zhCN : enUS).setup as Record<string, unknown>, 'setup'],
        ['translation', (locale === 'zh-CN' ? zhCN : enUS).onboarding as Record<string, unknown>, 'onboarding'],
        ['thisMacFirstRun', (locale === 'zh-CN' ? thisMacZh : thisMacEn) as Record<string, unknown>, 'thisMacFirstRun'],
      ];
      const checkTree = (ns: string, obj: Record<string, unknown>, prefix: string) => {
        for (const [k, v] of Object.entries(obj)) {
          const path = `${prefix}.${k}`;
          if (v && typeof v === 'object' && !Array.isArray(v)) {
            checkTree(ns, v as Record<string, unknown>, path);
          } else if (typeof v === 'string') {
            const key = ns === 'thisMacFirstRun' ? `thisMacFirstRun:${path.replace(/^thisMacFirstRun\./, '')}` : path;
            const rendered = i18n.t(key, { name: 'Workspace', model: 'Claude Sonnet', count: 1, n: 1 });
            expect(rendered, `[${locale}] ${key} leaked upstream brand`).not.toMatch(FORBIDDEN_BRAND);
          }
        }
      };
      for (const [ns, tree, prefix] of namespacesAndPrefixes) {
        checkTree(ns, tree, prefix);
      }
      for (const key of [
        'modelAccess.claudeCode.consent.agree',
        'cliApproval.note.computerFiles',
        'vendorTerms.body',
      ]) {
        expect(i18n.t(key, { name: 'Serper' }), `[${locale}] ${key}`).not.toMatch(FORBIDDEN_BRAND);
      }
    },
  );
});
