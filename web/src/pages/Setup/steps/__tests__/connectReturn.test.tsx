/**
 * A connection made for a page that sent the user into setup goes straight back
 * there, naming the provider by the key it was saved under, instead of walking
 * on through this app's model steps. Without a return the steps are unchanged.
 */
import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { captureSetupReturn, readSetupReturn } from '@/lib/setupReturn';

const h = vi.hoisted(() => ({
  assign: vi.fn(),
  mutateAsync: vi.fn(async (_payload: unknown) => ({})),
  updatePreferences: vi.fn(async (_payload: unknown) => ({})),
  apiGet: vi.fn(),
  preferences: null as Record<string, unknown> | null,
  catalog: [] as unknown[],
  configured: [] as string[],
  submitClaudeCallback: vi.fn(),
  initiateClaudeOAuth: vi.fn(),
  initiateCodexDevice: vi.fn(),
  pollCodexDevice: vi.fn(),
}));

vi.mock('@/config/supportLinks', () => ({ PLATFORM_URL: '/account' }));
vi.mock('@/hooks/useApiKeys', () => ({ useUpdateApiKeys: () => ({ mutateAsync: h.mutateAsync }) }));
vi.mock('@/hooks/usePreferences', () => ({ usePreferences: () => ({ preferences: h.preferences }) }));
vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: h.updatePreferences }),
}));
vi.mock('@/hooks/useModels', () => ({
  useModels: () => ({ models: { provider_catalog: h.catalog }, isLoading: false }),
}));
vi.mock('@/hooks/useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({ configuredSet: new Set(h.configured) }),
}));
vi.mock('@/api/client', () => ({ api: { get: h.apiGet, defaults: { baseURL: '' } } }));
// DALTON-HOOK(team-credentials): the connect steps read the team-credentials hint,
// which needs the auth context these isolated step renders do not mount.
vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({ isShared: false, canDisconnect: true, isPm: true, canInvite: false }),
}));
vi.mock('@/pages/Dashboard/utils/api', () => ({
  initiateCodexDevice: h.initiateCodexDevice,
  pollCodexDevice: h.pollCodexDevice,
  initiateClaudeOAuth: h.initiateClaudeOAuth,
  submitClaudeCallback: h.submitClaudeCallback,
}));

import { ApiKeyConnect } from '../connectStep/ApiKeyConnect';
import { CustomProviderConnect } from '../connectStep/CustomProviderConnect';
import { ExistingCustomConnect } from '../connectStep/ExistingCustomConnect';
import { OAuthConnect } from '../connectStep/OAuthConnect';
import ProviderStep from '../ProviderStep';
import type { LocationState } from '../connectStep/shared';

/**
 * The way back is a full page load, which jsdom will not perform. `assign` is an
 * own non-configurable property of Location, so the whole object is swapped
 * rather than the one method spied.
 */
const realLocation = window.location;
Object.defineProperty(window, 'location', {
  configurable: true,
  value: { href: realLocation.href, origin: realLocation.origin, assign: h.assign },
});
afterAll(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
});

const returnUrl = () => `${window.location.origin}/account/onboarding/models?step=model`;
const keepReturn = () => captureSetupReturn(`?return=${encodeURIComponent(returnUrl())}`);

/** The step at `route` with its state, and the model step it would move on to. */
function renderStep(element: React.ReactElement, route = '/setup/connect') {
  return renderWithProviders(
    <Routes>
      <Route path={route} element={element} />
      <Route path="/setup/models" element={<p>models step</p>} />
    </Routes>,
    { route },
  );
}

const apiKeyState: LocationState = {
  method: 'api_key',
  provider: 'dashscope',
  displayName: 'DashScope',
  sdk: 'openai',
  defaultBaseUrl: 'https://cn.example.com/v1',
  defaultRegion: 'cn',
  regionVariants: [
    { provider: 'dashscope-intl', region: 'intl', base_url: 'https://intl.example.com/v1', sdk: 'openai' },
  ] as LocationState['regionVariants'],
};

afterEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  h.preferences = null;
  h.catalog = [];
  h.configured = [];
});

describe('ApiKeyConnect with a return', () => {
  it('goes back naming the key the provider was saved under', async () => {
    keepReturn();
    const user = userEvent.setup();
    renderStep(<ApiKeyConnect state={apiKeyState} />);

    await user.click(screen.getByRole('button', { name: 'International' }));
    await user.type(screen.getByPlaceholderText(/key/i), 'sk-test');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await vi.waitFor(() => expect(h.assign).toHaveBeenCalledTimes(1));
    expect(h.mutateAsync.mock.calls[0][0]).toMatchObject({ api_keys: { 'dashscope-intl': 'sk-test' } });
    expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=dashscope-intl`);
    expect(screen.queryByText('models step')).toBeNull();
  });

  it('without a return, moves on to the model step', async () => {
    const user = userEvent.setup();
    renderStep(<ApiKeyConnect state={{ ...apiKeyState, regionVariants: null }} />);

    await user.type(screen.getByPlaceholderText(/key/i), 'sk-test');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(h.assign).not.toHaveBeenCalled();
  });

  it('keeps the button usable after a failed save', async () => {
    keepReturn();
    h.mutateAsync.mockRejectedValueOnce(new Error('key rejected'));
    const user = userEvent.setup();
    renderStep(<ApiKeyConnect state={{ ...apiKeyState, regionVariants: null }} />);

    await user.type(screen.getByPlaceholderText(/key/i), 'sk-test');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('key rejected');
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();
    expect(h.assign).not.toHaveBeenCalled();
  });
});

describe('CustomProviderConnect with a return', () => {
  it('goes back naming the new provider by its slug', async () => {
    keepReturn();
    h.apiGet.mockResolvedValue({
      data: { models: [{ name: 'gpt-x', model_id: 'gpt-x', display_name: 'GPT X', input_modalities: [] }] },
    });
    const user = userEvent.setup();
    renderStep(<CustomProviderConnect state={{ method: 'api_key', isCustom: true }} />);

    await user.type(screen.getByLabelText('Provider name'), 'My Server');
    await user.type(screen.getByLabelText('Base URL'), 'https://llm.example.com/v1');
    await user.type(screen.getByLabelText('API key'), 'sk-test');
    // The parent catalog arrives picked; one click imports it.
    expect(await screen.findByRole('checkbox', { name: 'GPT X' })).toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await vi.waitFor(() => expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=my-server`));
    expect(screen.queryByText('models step')).toBeNull();
  });
});

describe('ExistingCustomConnect with a return', () => {
  it('goes back once models are picked from a local server', async () => {
    keepReturn();
    h.apiGet.mockResolvedValue({ data: { models: [{ id: 'qwen-3', name: 'qwen-3' }] } });
    const user = userEvent.setup();
    renderStep(
      <ExistingCustomConnect
        state={{
          method: 'local',
          provider: 'lmstudio',
          displayName: 'LM Studio',
          isExistingCustom: true,
          dynamicModels: true,
        }}
      />,
    );

    await user.click(await screen.findByRole('checkbox', { name: 'qwen-3' }));
    await user.click(screen.getByRole('button', { name: 'Image' }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await vi.waitFor(() => expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=lmstudio`));
    expect(h.updatePreferences.mock.calls[0][0]).toMatchObject({
      model_preference: {
        custom_models: [
          { name: 'qwen-3', model_id: 'qwen-3', provider: 'lmstudio', input_modalities: ['text', 'image'] },
        ],
      },
    });
  });

  it('without a return, moves on to the model step', async () => {
    const user = userEvent.setup();
    renderStep(<ExistingCustomConnect state={{ method: 'api_key', provider: 'my-server', isExistingCustom: true }} />);

    await user.type(screen.getByLabelText('Model'), 'big-model');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(h.assign).not.toHaveBeenCalled();
  });
});

describe('OAuthConnect with a return', () => {
  it('goes back naming the sign-in provider', async () => {
    keepReturn();
    vi.spyOn(window, 'open').mockReturnValue(null);
    h.initiateClaudeOAuth.mockResolvedValue({ authorize_url: 'https://auth.example.com/authorize' });
    h.submitClaudeCallback.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    renderStep(<OAuthConnect state={{ method: 'oauth', provider: 'claude-oauth', displayName: 'Claude' }} />);

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /claude/i }));
    await user.type(await screen.findByRole('textbox'), 'code#state');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    await vi.waitFor(() => expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=claude-oauth`));
    expect(screen.queryByText('models step')).toBeNull();
  });

  it('goes back once the device sign-in completes', async () => {
    keepReturn();
    vi.spyOn(window, 'open').mockReturnValue(null);
    h.initiateCodexDevice.mockResolvedValue({
      user_code: 'ABCD-1234',
      verification_url: 'https://auth.example.com/device',
      interval: 0.001,
    });
    h.pollCodexDevice.mockResolvedValueOnce({ pending: true }).mockResolvedValue({ success: true });
    const user = userEvent.setup();
    renderStep(<OAuthConnect state={{ method: 'oauth', provider: 'codex-oauth', displayName: 'Codex' }} />);

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /verification page/i }));

    await vi.waitFor(() => expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=codex-oauth`));
    expect(h.pollCodexDevice).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('models step')).toBeNull();
  });
});

describe('ProviderStep with a return', () => {
  const catalogEntry = {
    provider: 'deepseek',
    display_name: 'DeepSeek',
    brand_key: 'deepseek',
    access_type: 'api_key',
  };

  it('goes back at once for a provider already connected', async () => {
    keepReturn();
    h.catalog = [catalogEntry];
    h.configured = ['deepseek'];
    const user = userEvent.setup();
    renderStep(<ProviderStep />, '/setup/provider');

    await user.click(screen.getByRole('radio', { name: /DeepSeek/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=deepseek`);
    expect(screen.queryByText('models step')).toBeNull();
  });

  it('goes back at once for a custom provider already set up', async () => {
    keepReturn();
    h.catalog = [catalogEntry];
    h.configured = ['my-server'];
    h.preferences = { model_preference: { custom_providers: [{ name: 'my-server', parent_provider: 'openai' }] } };
    const user = userEvent.setup();
    renderStep(<ProviderStep />, '/setup/provider');

    await user.click(screen.getByRole('radio', { name: /my-server/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(h.assign).toHaveBeenCalledWith(`${returnUrl()}&connected=my-server`);
  });

  it('does not report a custom provider without a key as connected', async () => {
    keepReturn();
    h.catalog = [catalogEntry];
    h.preferences = { model_preference: { custom_providers: [{ name: 'my-server', parent_provider: 'openai' }] } };
    const user = userEvent.setup();
    renderStep(<ProviderStep />, '/setup/provider');

    await user.click(screen.getByRole('radio', { name: /my-server/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(h.assign).not.toHaveBeenCalled();
    expect(readSetupReturn()).toBe(returnUrl());
  });

  it('without a return, moves on to the model step', async () => {
    h.catalog = [catalogEntry];
    h.configured = ['deepseek'];
    const user = userEvent.setup();
    renderStep(<ProviderStep />, '/setup/provider');

    await user.click(screen.getByRole('radio', { name: /DeepSeek/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(h.assign).not.toHaveBeenCalled();
  });

  it('a provider not yet connected still goes to its connect step', async () => {
    keepReturn();
    h.catalog = [catalogEntry];
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/setup/provider" element={<ProviderStep />} />
        <Route path="/setup/connect" element={<p>connect step</p>} />
      </Routes>,
      { route: '/setup/provider' },
    );

    await user.click(screen.getByRole('radio', { name: /DeepSeek/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('connect step')).toBeInTheDocument();
    expect(h.assign).not.toHaveBeenCalled();
  });
});
