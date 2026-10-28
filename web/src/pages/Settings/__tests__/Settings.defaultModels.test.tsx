import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const h = vi.hoisted(() => ({
  platformMode: false,
  payers: {} as Record<string, string>,
  mutate: vi.fn((_payload: unknown) => undefined),
  mutateAsync: vi.fn(async (_payload: unknown) => ({})),
  toast: vi.fn(),
  user: {
    id: 'u-1',
    email: 'tester@example.com',
    name: 'Tester',
    access_tier: 1,
    onboarding_completed: true,
  } as Record<string, unknown> | null,
  // Nothing chosen yet: both defaults resolve to the deployment's.
  preferences: { model_preference: {}, other_preference: {} } as Record<string, unknown> | null,
  catalog: {
    openai: { models: ['gpt-sol', 'gpt-terra', 'gpt-luna'] },
    anthropic: { models: ['claude-big', 'claude-small'] },
    deepseek: { models: ['deepseek-flash'] },
  } as Record<string, { models?: string[] }>,
  // DALTON-HOOK(settings-section): Dalton role defaults ride the system_defaults payload
  systemDefaults: {
    default_model: 'deepseek-flash',
    background_model: 'deepseek-flash',
    fallback_models: [] as string[],
    dalton_audit_model: 'gemini-4-argon-medium-oauth',
    dalton_language_model: 'gemini-4-argon-medium-oauth',
    dalton_audit_fallback_models: ['gpt-6.1-sol-oauth', 'claude-opus-5-5-oauth'],
    dalton_audit_timeout_s: 300,
    dalton_extract_model: 'deepseek-flash',
  } as Record<string, unknown>,
  validModelNames: new Set(['gpt-sol', 'gpt-terra', 'gpt-luna', 'claude-big', 'claude-small', 'deepseek-flash']),
  rawModels: {} as Record<string, { models?: string[] }>,
  rawApiResponse: null as Record<string, unknown> | null,
  searchProviderCatalog: null,
}));

vi.mock('@/config/hostMode', () => ({
  get isPlatformMode() {
    return h.platformMode;
  },
}));

// Auth — Settings only needs logout() from useAuth.
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ logout: vi.fn() }),
}));

// Current user — access_tier drives the paid-tier gating. Return the stable
// reference so the authUser effect doesn't fire on every render.
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: h.user, isLoading: false }),
}));

// Preferences — search_provider is loaded from other_preference. Stable ref so
// the prefs-sync effect (setPreferences(prefsData)) doesn't loop.
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoading: false, isLoaded: true }),
}));

// Update mutation. Assert the saved payload here: every control on the tab
// writes a patch of the keys it owns through this one hook, so the payload is
// the whole assertion surface. Stable object so the write callback's identity
// stays put across renders.
const mutationStub = { mutate: h.mutate, mutateAsync: h.mutateAsync };
vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => mutationStub,
}));

// Theme — Settings reads preference + setTheme.
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', preference: 'dark', setTheme: vi.fn() }),
}));

// Models hook — supply the minimal shape Settings + the (stubbed) tier config read.
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: h.catalog,
    // Per-model tuning metadata — the Advanced section reads it for the
    // reasoning-effort ladder. Always an object from the real hook.
    metadata: {},
    payers: h.payers,
    systemDefaults: h.systemDefaults,
    // DALTON-HOOK(settings-section): ModelTab reads customModels for Dalton selectors
    customModels: [],
    offPlanModels: new Set(),
    // Stable Set ref — the stale-model cleanup effect keys on its identity.
    validModelNames: h.validModelNames,
    rawModels: h.rawModels,
    rawApiResponse: h.rawApiResponse,
    compactionProfiles: null,
    searchProviders: h.searchProviderCatalog,
    isLoading: false,
  }),
}));

// Toast.
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: h.toast }),
  toast: h.toast,
}));

// Debounced save. The model tab has no text input and writes on the change
// event, but UserInfoTab (mounted by the same Settings shell) still debounces
// its text fields.
vi.mock('@/hooks/useDebouncedSave', () => ({
  useDebouncedSave: (saveFn: () => Promise<void>) => ({
    trigger: () => { setTimeout(() => { void saveFn(); }, 0); },
    flush: () => { setTimeout(() => { void saveFn(); }, 0); },
    status: 'idle',
  }),
}));

// The real ModelTierConfig runs; only its dropdowns are native selects, so a
// test can pick a model by value and read the empty slot's placeholder.
vi.mock('@/components/model/ModelSelector', () => ({
  ModelSelector: (props: {
    label: string;
    value: string;
    onChange: (v: string) => void;
    models: Record<string, { models?: string[] }>;
    placeholder?: string;
    autoOption?: { value: string; label: string };
  }) => (
    <select aria-label={props.label} value={props.value} onChange={(e) => props.onChange(e.target.value)}>
      <option value="">{props.placeholder}</option>
      {props.autoOption && <option value={props.autoOption.value}>{props.autoOption.label}</option>}
      {Object.values(props.models).flatMap((p) => p.models ?? []).map((m) => (
        <option key={m} value={m}>{m}</option>
      ))}
    </select>
  ),
}));

// The fallback list moved out of ModelTierConfig into the Advanced section's
// own picker. It still maps `selected` with no catalog filter, so what renders
// is exactly the panel's state — which is what the cleanup effect must prune.
// DALTON-HOOK(settings-section): the picker renders its description so Dalton fallback hints are testable
vi.mock('@/components/model/FallbackModelsPicker', () => ({
  FallbackModelsPicker: (props: { selected?: string[]; label?: string; description?: string }) => (
    <div data-testid="fallback-picker" aria-label={props.label}>
      {props.description && <p>{props.description}</p>}
      <ul data-testid="fallback-chips">
        {(props.selected ?? []).map((m) => <li key={m}>{m}</li>)}
      </ul>
    </div>
  ),
}));

// Dashboard API surface Settings imports — model tab load calls three of these.
vi.mock('@/pages/Dashboard/utils/api', () => ({
  updateCurrentUser: vi.fn(async () => ({})),
  clearPreferences: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ avatar_url: '' })),
  getUserApiKeys: vi.fn(async () => ({ providers: [] })),
}));

// Home workspace, used by preference-modify navigation, never in these tests.
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getHomeWorkspace: vi.fn(async () => ({ workspace_id: 'ws-home' })),
}));

// Onboarding — Settings renders replay/reset buttons; no provider in this harness.
vi.mock('@/pages/Onboarding', () => ({
  useOnboarding: () => ({ replayGuides: vi.fn(), resetOnboarding: vi.fn() }),
}));

// DALTON-HOOK(settings-section): stub DaltonSection card in Settings shell harness
vi.mock('@/pages/Settings/panels/dalton/DaltonSection', () => ({
  DaltonSection: () => null,
}));

// DALTON-HOOK(team-credentials): the model-access panel reads the shared-credential hint
vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({ isShared: false, canDisconnect: true, isPm: true, canInvite: false }),
}));

// Import after mocks are registered.
import Settings from '../Settings';

function renderModelTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings?tab=model']}>
        <Settings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const primary = () => screen.getByRole('combobox', { name: 'Default model' }) as HTMLSelectElement;
const background = () => screen.getByRole('combobox', { name: 'Background model' }) as HTMLSelectElement;

/** Whether any preferences write so far named a default model. */
function wroteADefault(): boolean {
  return [...h.mutate.mock.calls, ...h.mutateAsync.mock.calls].some(([payload]) => {
    const prefs = (payload as { model_preference?: Record<string, unknown> } | undefined)?.model_preference ?? {};
    return 'preferred_model' in prefs || 'preferred_background_model' in prefs;
  });
}

beforeEach(() => {
  localStorage.removeItem('settings:modelMode');
  h.preferences = { model_preference: {}, other_preference: {} };
  h.payers = {};
  // DALTON-HOOK(settings-section): restore Dalton role defaults before each test
  h.systemDefaults = {
    default_model: 'deepseek-flash',
    background_model: 'deepseek-flash',
    fallback_models: [],
    dalton_audit_model: 'gemini-4-argon-medium-oauth',
    dalton_language_model: 'gemini-4-argon-medium-oauth',
    dalton_audit_fallback_models: ['gpt-6.1-sol-oauth', 'claude-opus-5-5-oauth'],
    dalton_audit_timeout_s: 300,
    dalton_extract_model: 'deepseek-flash',
  };
  h.mutate.mockClear();
  h.mutateAsync.mockClear();
  h.mutateAsync.mockResolvedValue({});
  h.toast.mockClear();
});

/**
 * A primary pick never touches the Background slot: an empty one follows the
 * primary and says so. Both defaults change under one question, and a Background
 * pick made while it is open joins it instead of dropping the primary.
 */
describe('Settings: default model question', () => {
  it('leaves an empty Background slot following the primary, whichever provider it is from', async () => {
    renderModelTab();
    fireEvent.change(await screen.findByRole('combobox', { name: 'Default model' }), { target: { value: 'gpt-sol' } });
    expect(background().value).toBe('');
    expect(background().selectedOptions[0].textContent).toBe('Same as Default model');
    expect(screen.getByText(/your default model\?/)).toBeInTheDocument();

    fireEvent.change(primary(), { target: { value: 'claude-big' } });
    expect(background().value).toBe('');
    expect(wroteADefault()).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(primary().value).toBe('');
    expect(wroteADefault()).toBe(false);
  });

  it('keeps both picks under one question and writes them together', async () => {
    renderModelTab();
    fireEvent.change(await screen.findByRole('combobox', { name: 'Default model' }), { target: { value: 'gpt-sol' } });
    fireEvent.change(background(), { target: { value: 'gpt-luna' } });
    fireEvent.change(primary(), { target: { value: 'claude-big' } });

    expect(primary().value).toBe('claude-big');
    expect(background().value).toBe('gpt-luna');
    expect(screen.getByText(/your default models\?/)).toBeInTheDocument();
    expect(wroteADefault()).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Make default' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(h.mutateAsync).toHaveBeenCalledWith({
      model_preference: { preferred_model: 'claude-big', preferred_background_model: 'gpt-luna', background_follows: null },
      apply_default_to: 'new_threads',
    });
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('takes a cleared default out of the open question, and closes it once nothing is left', async () => {
    renderModelTab();
    fireEvent.change(await screen.findByRole('combobox', { name: 'Default model' }), { target: { value: 'gpt-sol' } });
    fireEvent.change(background(), { target: { value: 'gpt-luna' } });
    expect(screen.getByText(/your default models\?/)).toBeInTheDocument();

    fireEvent.change(background(), { target: { value: '' } });
    expect(background().value).toBe('');
    expect(primary().value).toBe('gpt-sol');
    expect(screen.getByText(/your default model\?/)).toBeInTheDocument();
    expect(h.mutate).toHaveBeenLastCalledWith({ model_preference: { preferred_background_model: null, background_follows: null } });

    fireEvent.change(primary(), { target: { value: '' } });
    expect(primary().value).toBe('');
    expect(screen.queryByText(/your default models?\?/)).toBeNull();
    expect(h.mutate).toHaveBeenLastCalledWith({ model_preference: { preferred_model: null } });
    expect(h.mutateAsync).not.toHaveBeenCalled();
  });
});

/**
 * An unset default is a choice: Auto runs the deployment's model. The background
 * slot offers Auto beside "Same as Default" only once a default is saved, since
 * before that the two run the same model.
 */
describe('Settings: Auto defaults', () => {
  const options = (select: HTMLSelectElement) => [...select.options].map((o) => o.textContent);

  it('shows both unset defaults as Auto with the model each runs', async () => {
    h.systemDefaults = { default_model: 'claude-big', background_model: 'deepseek-flash', fallback_models: [] };
    try {
      renderModelTab();
      await screen.findByRole('combobox', { name: 'Default model' });
      expect(primary().selectedOptions[0].textContent).toBe('Auto (claude-big)');
      expect(background().selectedOptions[0].textContent).toBe('Auto (deepseek-flash)');
      expect(options(background())).not.toContain('Same as Default model');
    } finally {
      // DALTON-HOOK(settings-section): restore the Dalton role defaults too
      h.systemDefaults = {
        default_model: 'deepseek-flash',
        background_model: 'deepseek-flash',
        fallback_models: [],
        dalton_audit_model: 'gemini-4-argon-medium-oauth',
        dalton_language_model: 'gemini-4-argon-medium-oauth',
        dalton_audit_fallback_models: ['gpt-6.1-sol-oauth', 'claude-opus-5-5-oauth'],
        dalton_audit_timeout_s: 300,
        dalton_extract_model: 'deepseek-flash',
      };
    }
  });

  it('offers Auto beside a saved primary, and writes it without asking', async () => {
    h.preferences = { model_preference: { preferred_model: 'gpt-sol' }, other_preference: {} };
    renderModelTab();
    await screen.findByRole('combobox', { name: 'Background model' });
    expect(background().selectedOptions[0].textContent).toBe('Same as Default model');

    fireEvent.change(background(), { target: { value: '__auto__' } });
    expect(h.mutate).toHaveBeenLastCalledWith({
      model_preference: { preferred_background_model: null, background_follows: 'deployment' },
    });
    expect(screen.queryByText(/your default models?\?/)).toBeNull();
  });

  it('shows a saved Auto for the Background model as Auto', async () => {
    h.preferences = {
      model_preference: { preferred_model: 'gpt-sol', background_follows: 'deployment' },
      other_preference: {},
    };
    renderModelTab();
    await screen.findByRole('combobox', { name: 'Background model' });
    expect(background().selectedOptions[0].textContent).toBe('Auto (deepseek-flash)');
  });
});

// DALTON-HOOK(settings-section): test Dalton timeout draft editing and system-only catalog filtering
describe('Settings: Dalton routing controls', () => {
  it('allows typing 300 digit-by-digit from empty in the audit timeout input and saves on blur or Enter', async () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    renderModelTab();

    const timeoutInput = (await screen.findByLabelText(/Figure audit timeout/i)) as HTMLInputElement;
    expect(timeoutInput.value).toBe('');
    expect(timeoutInput.placeholder).toContain('300');

    const auditSelect = screen.getByRole('combobox', { name: 'Figure audit model' }) as HTMLSelectElement;
    expect(auditSelect.options[0].textContent).toContain('gemini-4-argon-medium-oauth');
    const extractSelect = screen.getByRole('combobox', { name: 'Conversation archive extraction model' }) as HTMLSelectElement;
    expect(extractSelect.options[0].textContent).toContain('deepseek-flash');
    expect(
      screen.getByText(/gpt-6\.1-sol-oauth, claude-opus-5-5-oauth/),
    ).toBeInTheDocument();

    // Type '3', '30', '300' digit-by-digit without triggering intermediate writes or clamping
    fireEvent.change(timeoutInput, { target: { value: '3' } });
    expect(timeoutInput.value).toBe('3');
    expect(h.mutate).not.toHaveBeenCalled();

    fireEvent.change(timeoutInput, { target: { value: '30' } });
    expect(timeoutInput.value).toBe('30');
    expect(h.mutate).not.toHaveBeenCalled();

    fireEvent.change(timeoutInput, { target: { value: '300' } });
    expect(timeoutInput.value).toBe('300');
    expect(h.mutate).not.toHaveBeenCalled();

    // Commit on blur -> saves 300
    fireEvent.blur(timeoutInput);
    expect(h.mutate).toHaveBeenLastCalledWith({
      model_preference: { dalton_audit_timeout_s: 300 },
    });

    // Out-of-range value on Enter shows Chinese error message and does not save
    h.mutate.mockClear();
    fireEvent.change(timeoutInput, { target: { value: '45' } });
    fireEvent.keyDown(timeoutInput, { key: 'Enter' });
    expect(h.mutate).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('数字核验超时必须在 60–1800 秒之间');
  });

  // DALTON-HOOK(settings-section): verify Dalton dropdowns exclude custom_models
  it('excludes user custom_models from Dalton model selectors while keeping system catalog models', async () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    h.catalog = {
      openai: { models: ['gpt-sol', 'my-custom-model'] },
      anthropic: { models: ['claude-big'] },
      deepseek: { models: ['deepseek-flash'] },
    };
    h.preferences = {
      model_preference: {
        custom_models: [{ name: 'my-custom-model', model_id: 'gpt-x', provider: 'openai' }],
      },
      other_preference: {},
    };
    try {
      renderModelTab();
      const auditSelect = (await screen.findByRole('combobox', { name: 'Figure audit model' })) as HTMLSelectElement;
      const optionValues = [...auditSelect.options].map((o) => o.value);
      expect(optionValues).toContain('gpt-sol');
      expect(optionValues).not.toContain('my-custom-model');
    } finally {
      h.catalog = {
        openai: { models: ['gpt-sol', 'gpt-terra', 'gpt-luna'] },
        anthropic: { models: ['claude-big', 'claude-small'] },
        deepseek: { models: ['deepseek-flash'] },
      };
    }
  });

  // DALTON-HOOK(settings-section): the language-review selector defaults from
  // system_defaults (audit fallback) and writes its own key.
  it('shows the language review default and saves the choice under dalton_language_model', async () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    renderModelTab();

    const langSelect = (await screen.findByRole('combobox', { name: 'Language review model' })) as HTMLSelectElement;
    // The empty option carries the effective system default (audit fallback).
    expect(langSelect.options[0].textContent).toContain('gemini-4-argon-medium-oauth');

    fireEvent.change(langSelect, { target: { value: 'gpt-sol' } });
    expect(h.mutate).toHaveBeenLastCalledWith({
      model_preference: { dalton_language_model: 'gpt-sol' },
    });
  });

  it('reads back a saved dalton_language_model preference', async () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    h.preferences = {
      model_preference: { dalton_language_model: 'claude-big' },
      other_preference: {},
    };
    try {
      renderModelTab();
      const langSelect = (await screen.findByRole('combobox', { name: 'Language review model' })) as HTMLSelectElement;
      expect(langSelect.value).toBe('claude-big');
    } finally {
      h.preferences = { model_preference: {}, other_preference: {} };
    }
  });
});

describe('saved fallbacks that cross payers', () => {
  const NOTE = 'Saved fallbacks run on every turn, including ones you pay for yourself.';
  const renderAdvanced = () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    return renderModelTab();
  };

  // Saving the list made the deployment's models the user's choice, which
  // the faded look no longer says.
  it("says a saved list with the deployment's models runs on turns the user pays for", () => {
    h.payers = { 'deepseek-flash': 'server_key', 'claude-big': 'own_key' };
    h.preferences = { model_preference: { fallback_models: ['deepseek-flash'] }, other_preference: {} };
    renderAdvanced();
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });

  it('says nothing to a user who pays for no turn themselves', () => {
    h.payers = { 'deepseek-flash': 'server_key' };
    h.preferences = { model_preference: { fallback_models: ['deepseek-flash'] }, other_preference: {} };
    renderAdvanced();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it('says nothing while the list is still the deployment\'s', () => {
    h.payers = { 'deepseek-flash': 'server_key', 'claude-big': 'own_key' };
    h.systemDefaults = { default_model: 'deepseek-flash', background_model: 'deepseek-flash', fallback_models: ['deepseek-flash'] };
    renderAdvanced();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });
});

describe('payers in the default fallbacks and favorites', () => {
  const FADED = 'Faded ones skip turns you pay for yourself.';
  const renderAdvanced = () => {
    localStorage.setItem('settings:modelMode', 'advanced');
    return renderModelTab();
  };

  // One above the plan, or on a local server, has no payer here, and an
  // own-key turn skips it as it skips one the deployment pays for.
  it("fades a default fallback that no key or sign-in of the user's runs", () => {
    h.payers = { 'claude-big': 'own_key' };
    h.systemDefaults = { default_model: 'deepseek-flash', background_model: 'deepseek-flash', fallback_models: ['claude-small'] };
    renderAdvanced();
    expect(screen.getByText(FADED, { exact: false })).toBeInTheDocument();
  });

  it("does not fade a default fallback the user's own key runs", () => {
    h.payers = { 'claude-big': 'own_key' };
    h.systemDefaults = { default_model: 'deepseek-flash', background_model: 'deepseek-flash', fallback_models: ['claude-big'] };
    renderAdvanced();
    expect(screen.queryByText(FADED, { exact: false })).not.toBeInTheDocument();
  });

  // Twins share a name, one on a plan and one on credits.
  it('tags each favorite with who pays for it', () => {
    h.payers = { 'claude-big': 'own_key' };
    h.preferences = { model_preference: {}, other_preference: { starred_models: ['claude-big'], favorites_starred_only: true } };
    renderModelTab();
    const chip = screen.getByRole('button', { name: /claude-big/i }).parentElement!;
    expect(chip).toHaveTextContent('Your key');
  });
});
