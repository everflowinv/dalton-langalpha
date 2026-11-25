// DALTON-HOOK(standalone-core): Vitest coverage for CustomProviderConnect surfacing Core format hint on save
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { queryKeys } from '@/lib/queryKeys';
import { CustomProviderConnect } from '../steps/connectStep/CustomProviderConnect';

const h = vi.hoisted(() => ({
  connected: vi.fn(),
  testApiKey: vi.fn(),
  updatePreferences: vi.fn(async (_payload: unknown) => ({})),
  updateApiKeys: vi.fn(async (_payload: unknown) => ({})),
  refresh: vi.fn(async () => ({})),
}));

vi.mock('@/config/brand', () => ({
  BRAND_NAME: 'LangAlpha',
  BRAND_MARK: 'langalpha',
  isDaltonBuild: true,
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: false,
}));

// DALTON-HOOK(team-credentials): export shared fetchTeamCredentialsStatus alongside useTeamCredentials mock
vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: 'off',
    enabled: false,
    holder_user_id: null,
    credential_user_id: 'local-user',
    is_pm: true,
    can_disconnect: true,
    can_invite: false,
    caller_tier: 'admin',
    colleague_sharing_enabled: false,
    auth_mode: 'oss',
  })),
  useTeamCredentials: () => ({ isShared: false, canManage: true }),
}));

vi.mock('@/api/model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/model')>();
  return {
    ...actual,
    testApiKey: h.testApiKey,
  };
});

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: { model_preference: {} } }),
}));

vi.mock('@/hooks/useStoredPreferences', () => ({
  useReadStoredPreferences: () => async () => ({ model_preference: {} }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: h.updatePreferences }),
}));

vi.mock('@/hooks/useApiKeys', () => ({
  useUpdateApiKeys: () => ({ mutateAsync: h.updateApiKeys }),
}));

vi.mock('@/hooks/useModelAccessActions', () => ({
  useRefreshAfterAccessChange: () => h.refresh,
}));

vi.mock('@/hooks/useProviderCatalog', () => ({
  useProviderCatalog: () => ({ connectionOf: (p: string) => p }),
}));

vi.mock('@/hooks/useProviderModels', () => ({
  useEndpointModels: () => ({
    data: { status: 'ok', recognized: [{ name: 'resp-only-1', model_id: 'resp-only-1', display_name: 'Resp Only 1' }], others: [] },
    settled: true,
    isFetching: false,
    isError: false,
    error: null,
  }),
}));

vi.mock('@/components/modelAccess/endpointTrust', () => ({
  useEndpointVerdict: () => true,
}));

vi.mock('../steps/connectStep/shared', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../steps/connectStep/shared')>();
  return {
    ...actual,
    useProviderConnected: () => h.connected,
  };
});

describe('CustomProviderConnect Core format hint display', () => {
  beforeEach(() => {
    h.connected.mockReset();
    h.testApiKey.mockReset();
    h.updatePreferences.mockClear();
    h.updateApiKeys.mockClear();
    h.refresh.mockClear();
  });

  it('displays tool_calling_hint returned on Core custom provider save and does not advance past the warning', async () => {
    const hint = '网关只支持 Responses 接口，请在提供商设置里打开 Responses 格式';
    h.testApiKey.mockResolvedValue({
      success: true,
      model: 'resp-only-1',
      tool_calling: null,
      tool_calling_models: { 'resp-only-1': null },
      tool_calling_hint: hint,
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.models.all, {
      dalton_profile: 'core',
      models: {},
      model_metadata: {},
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <CustomProviderConnect state={{ method: 'api_key', answering: true }} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByLabelText(/Provider name|提供商名称/i), { target: { value: 'Resp GW' } });
    fireEvent.change(screen.getByLabelText(/Base URL/i), { target: { value: 'https://resp.acme.test/v1' } });
    fireEvent.change(screen.getByPlaceholderText(/Paste your API key here/i), { target: { value: 'sk-resp-1234' } });

    const continueBtn = screen.getByRole('button', { name: /continue|继续/i });
    fireEvent.click(continueBtn);

    const hintEl = await screen.findByTestId('api-key-test-hint');
    expect(hintEl).toHaveTextContent(hint);
    await waitFor(() => expect(h.testApiKey).toHaveBeenCalledTimes(1));
    expect(h.connected).not.toHaveBeenCalled();
  });

  it('displays rate-limit hint and stays on form when /api/v1/keys/test returns HTTP 429 on retry', async () => {
    h.testApiKey.mockRejectedValue({ response: { status: 429, data: { detail: 'Too many test requests.' } } });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.models.all, {
      dalton_profile: 'core',
      models: {},
      model_metadata: {},
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <CustomProviderConnect state={{ method: 'api_key', answering: true }} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByLabelText(/Provider name|提供商名称/i), { target: { value: 'Resp GW' } });
    fireEvent.change(screen.getByLabelText(/Base URL/i), { target: { value: 'https://resp.acme.test/v1' } });
    fireEvent.change(screen.getByPlaceholderText(/Paste your API key here/i), { target: { value: 'sk-resp-1234' } });

    fireEvent.click(screen.getByRole('button', { name: /continue|继续/i }));

    const hintEl = await screen.findByTestId('api-key-test-hint');
    expect(hintEl).toHaveTextContent('检查过于频繁，请稍后重试');
    await waitFor(() => expect(h.testApiKey).toHaveBeenCalledTimes(1));
    expect(h.connected).not.toHaveBeenCalled();
  });
});
