import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import i18n from '@/i18n';
import type { CapabilityId } from '@/hooks/useCapabilities';
import {
  type DeploymentSettingField,
  type DeploymentSettingsResponse,
  type fetchDeploymentSettings,
  type testDeploymentCredentials,
  type updateDeploymentSettings,
} from '@/lib/dalton/deploymentCredentials';
import { queryKeys } from '@/lib/queryKeys';

const mocks = vi.hoisted(() => ({
  profile: 'core' as 'core' | 'everflow',
  authMode: 'local' as 'local' | 'supabase' | 'oss',
  isSingleUserOss: false,
  isMultiUserAuth: true,
  isDaltonAuth: true,
  fetchMock: vi.fn<typeof fetchDeploymentSettings>(),
  updateMock: vi.fn<typeof updateDeploymentSettings>(),
  testMock: vi.fn<typeof testDeploymentCredentials>(),
}));

vi.mock('@/config/daltonAuth', () => ({
  get DALTON_AUTH_MODE() {
    return mocks.authMode;
  },
  get isDaltonAuth() {
    return mocks.isDaltonAuth;
  },
  get isDaltonLocalAuth() {
    return mocks.authMode === 'local';
  },
  get isDaltonSupabaseAuth() {
    return mocks.authMode === 'supabase';
  },
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  const caps = new Set<CapabilityId>(['company_wiki']);
  return {
    ...actual,
    useCapabilities: () => ({
      profile: mocks.profile,
      capabilities: caps,
      hasCapability: (req: Parameters<typeof actual.hasRequiredCapability>[1]) =>
        actual.hasRequiredCapability(caps, req),
      brandName: 'Dalton',
      fingerprint: 'fp-test',
      tickerLogosEnabled: false,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: mocks.profile === 'core',
      isEverflowBuild: mocks.profile === 'everflow',
    }),
  };
});

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const snap = {
      tier: 'admin' as const,
      status: 'ready' as const,
      authMode: mocks.authMode,
      isPlatform: false,
      isKnown: true,
      isConfirming: false,
      isMultiUserAuth: mocks.isMultiUserAuth,
      isSingleUserOss: mocks.isSingleUserOss,
      isAdmin: true,
      isInternal: true,
      isExternal: false,
      canActAsOwner: true,
      canPublishPublic: true,
      canInvite: true,
      isTeamCredentialsShared: false,
      canManageCredentials: true,
      isPm: true,
      colleagueSharingEnabled: false,
    };
    return {
      ...snap,
      getLatestIdentity: () => snap,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

vi.mock('@/lib/dalton/deploymentCredentials', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/deploymentCredentials')>();
  return {
    ...actual,
    fetchDeploymentSettings: (...args: Parameters<typeof fetchDeploymentSettings>) =>
      mocks.fetchMock(...args),
    updateDeploymentSettings: (...args: Parameters<typeof updateDeploymentSettings>) =>
      mocks.updateMock(...args),
    testDeploymentCredentials: (...args: Parameters<typeof testDeploymentCredentials>) =>
      mocks.testMock(...args),
  };
});

import DaltonDeploymentCredentialsPanel from '../DaltonDeploymentCredentialsPanel';

function makeBaseFields(overrides: Partial<Record<string, Partial<DeploymentSettingField>>> = {}): DeploymentSettingField[] {
  const defaults: DeploymentSettingField[] = [
    {
      name: 'DALTON_GOOGLE_OAUTH_CLIENT_ID',
      section: 'calendar_mail',
      label_zh: 'Google OAuth 客户端 ID',
      label_en: 'Google OAuth Client ID',
      secret: false,
      credential_group: 'google_oauth',
      pair_with: 'DALTON_GOOGLE_OAUTH_CLIENT_SECRET',
      input_kind: 'text',
      requires_restart: 'none',
      test_kind: 'google_oauth',
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
      value: null,
    },
    {
      name: 'DALTON_GOOGLE_OAUTH_CLIENT_SECRET',
      section: 'calendar_mail',
      label_zh: 'Google OAuth 客户端密钥',
      label_en: 'Google OAuth Client Secret',
      secret: true,
      credential_group: 'google_oauth',
      pair_with: 'DALTON_GOOGLE_OAUTH_CLIENT_ID',
      input_kind: 'secret',
      requires_restart: 'none',
      test_kind: 'google_oauth',
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
    },
    {
      name: 'DALTON_FEISHU_APP_ID',
      section: 'feishu_app',
      label_zh: '飞书应用 App ID',
      label_en: 'Feishu App ID',
      secret: false,
      credential_group: 'feishu_app',
      pair_with: 'DALTON_FEISHU_APP_SECRET',
      input_kind: 'text',
      requires_restart: 'gateway_hint',
      test_kind: 'feishu_app',
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
      value: null,
    },
    {
      name: 'DALTON_FEISHU_APP_SECRET',
      section: 'feishu_app',
      label_zh: '飞书应用 App Secret',
      label_en: 'Feishu App Secret',
      secret: true,
      credential_group: 'feishu_app',
      pair_with: 'DALTON_FEISHU_APP_ID',
      input_kind: 'secret',
      requires_restart: 'gateway_hint',
      test_kind: 'feishu_app',
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
    },
    {
      name: 'DALTON_FEISHU_ENCRYPT_KEY',
      section: 'feishu_app',
      label_zh: '飞书事件加密密钥（Encrypt Key）',
      label_en: 'Feishu Event Encrypt Key',
      secret: true,
      credential_group: 'feishu_event',
      pair_with: 'DALTON_FEISHU_VERIFICATION_TOKEN',
      input_kind: 'secret',
      requires_restart: 'none',
      test_kind: null,
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
    },
    {
      name: 'DALTON_FEISHU_VERIFICATION_TOKEN',
      section: 'feishu_app',
      label_zh: '飞书事件验证令牌（Verification Token）',
      label_en: 'Feishu Event Verification Token',
      secret: true,
      credential_group: 'feishu_event',
      pair_with: 'DALTON_FEISHU_ENCRYPT_KEY',
      input_kind: 'secret',
      requires_restart: 'none',
      test_kind: null,
      batch: 1,
      configured: false,
      source: 'unset',
      readonly: false,
      updated_at: null,
    },
  ];

  return defaults.map((f) => (overrides[f.name] ? { ...f, ...overrides[f.name] } : f));
}

function makeResponse(partial: Partial<DeploymentSettingsResponse> = {}): DeploymentSettingsResponse {
  return {
    profile: 'core',
    revision: 4,
    encryption_configured: true,
    fields: makeBaseFields(),
    redirect_uris: {
      google: 'https://research.example.com/api/v1/dalton/integrations/google/callback',
      feishu: 'https://research.example.com/api/v1/dalton/integrations/feishu/callback',
    },
    feishu_events_url: 'https://research.example.com/api/v1/dalton/integrations/feishu/events',
    google_redirect_uri_eligible: true,
    google_redirect_uri_reason: null,
    ...partial,
  };
}

function makeApiError(status: number, data: Record<string, unknown>) {
  return new ApiError(`Request failed with status code ${status}`, 'ERR_BAD_REQUEST', {
    status,
    statusText: String(status),
    headers: {},
    data,
  });
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
  const view = render(
    <QueryClientProvider client={client}>
      <DaltonDeploymentCredentialsPanel />
    </QueryClientProvider>,
  );
  return { ...view, client, invalidateSpy };
}

afterEach(async () => {
  mocks.profile = 'core';
  mocks.authMode = 'local';
  mocks.isSingleUserOss = false;
  mocks.isMultiUserAuth = true;
  mocks.isDaltonAuth = true;
  mocks.fetchMock.mockReset();
  mocks.updateMock.mockReset();
  mocks.testMock.mockReset();
  await i18n.changeLanguage('en-US');
});

describe('DaltonDeploymentCredentialsPanel', () => {
  it('shows static notice in OSS mode without calling deployment-settings API', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.authMode = 'oss';
    mocks.isSingleUserOss = true;
    mocks.isMultiUserAuth = false;
    mocks.isDaltonAuth = false;

    renderPanel();

    expect(screen.getByTestId('deployment-credentials-oss-notice')).toBeInTheDocument();
    expect(screen.getByTestId('deployment-credentials-oss-banner')).toHaveTextContent(
      '请先启用管理员登录模式（DALTON_AUTH_MODE=local）',
    );
    expect(mocks.fetchMock).not.toHaveBeenCalled();
  });

  it('displays copyable callback URLs, shows banner and disables Google switches when google_redirect_uri_eligible is false', async () => {
    await i18n.changeLanguage('zh-CN');
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    const fieldsWithGoogleToggle: DeploymentSettingField[] = [
      ...makeBaseFields(),
      {
        name: 'DALTON_CAP_GOOGLE_WORKSPACE',
        section: 'calendar_mail',
        label_zh: 'Google 个人连接器',
        label_en: 'Google Personal Connector',
        secret: false,
        credential_group: null,
        pair_with: null,
        input_kind: 'boolean',
        requires_restart: 'none',
        test_kind: null,
        batch: 1,
        configured: false,
        source: 'unset',
        readonly: false,
        updated_at: null,
        value: 'false',
      },
    ];
    mocks.fetchMock.mockResolvedValueOnce(
      makeResponse({
        fields: fieldsWithGoogleToggle,
        redirect_uris: {
          google: 'https://192.168.1.10/api/v1/dalton/integrations/google/callback',
          feishu: 'https://192.168.1.10/api/v1/dalton/integrations/feishu/callback',
        },
        google_redirect_uri_eligible: false,
        google_redirect_uri_reason: 'raw_ip_not_allowed',
      }),
    );

    renderPanel();

    const banner = await screen.findByTestId('google-redirect-uri-ineligible-banner');
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toContain('公共顶级域名后缀');

    // Copyable Google callback URI works
    expect(screen.getByTestId('google-redirect-uri')).toHaveTextContent(
      'https://192.168.1.10/api/v1/dalton/integrations/google/callback',
    );
    fireEvent.click(screen.getByTestId('google-redirect-uri-copy'));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        'https://192.168.1.10/api/v1/dalton/integrations/google/callback',
      );
    });

    // Google switch is disabled when redirect URI is ineligible
    const switchRow = screen.getByTestId('switch-row-DALTON_CAP_GOOGLE_WORKSPACE');
    const toggleBtn = switchRow.querySelector('button[role="switch"]');
    expect(toggleBtn).toBeDisabled();

    // Core UI must not contain proprietary Everflow branding
    expect(document.body.textContent?.toLowerCase()).not.toContain('everflow');
  });

  it('distinguishes feishu vault_readonly vs vault_error badges while keeping feishu_event keys independently editable', async () => {
    await i18n.changeLanguage('zh-CN');

    // 1) vault_readonly: echoes App ID, locks feishu_app, keeps feishu_event editable & saveable
    const readonlyResp = makeResponse({
      revision: 7,
      fields: makeBaseFields({
        DALTON_FEISHU_APP_ID: {
          source: 'vault_readonly',
          readonly: true,
          configured: true,
          value: 'cli_vault_readonly_001',
        },
        DALTON_FEISHU_APP_SECRET: {
          source: 'vault_readonly',
          readonly: true,
          configured: true,
        },
      }),
    });
    mocks.fetchMock.mockResolvedValue(readonlyResp);
    mocks.updateMock.mockResolvedValueOnce({
      revision: 8,
      changed: ['DALTON_FEISHU_ENCRYPT_KEY'],
      pending_apply: false,
      warnings: [],
    });

    const first = renderPanel();
    const readonlyBadge = await screen.findByTestId('feishu-app-source-badge');
    expect(readonlyBadge).toHaveTextContent('现网飞书机器人使用中（只读 · 第 3 批迁移后可在此修改）');
    expect(screen.getByTestId('feishu-vault-readonly-notice')).toBeInTheDocument();
    expect(screen.getByTestId('input-feishu-app-id')).toHaveValue('cli_vault_readonly_001');
    expect(screen.getByTestId('input-feishu-app-id')).toBeDisabled();
    expect(screen.getByTestId('save-feishu-app-btn')).toBeDisabled();

    // Event keys remain editable
    const encInput = screen.getByTestId('input-feishu-encrypt-key');
    expect(encInput).not.toBeDisabled();
    fireEvent.change(encInput, { target: { value: 'test-secret-not-real-enc-0001' } });
    fireEvent.click(screen.getByTestId('save-feishu-event-btn'));

    await waitFor(() => {
      expect(mocks.updateMock).toHaveBeenCalledWith({
        expected_revision: 7,
        values: { DALTON_FEISHU_ENCRYPT_KEY: 'test-secret-not-real-enc-0001' },
      });
    });
    expect(first.invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.dalton.capabilities(),
    });
    first.unmount();

    // 2) vault_error: distinct danger badge, hides App ID echo, locks feishu_app, keeps feishu_event editable
    mocks.fetchMock.mockReset();
    mocks.fetchMock.mockResolvedValue(
      makeResponse({
        revision: 8,
        fields: makeBaseFields({
          DALTON_FEISHU_APP_ID: {
            source: 'vault_error',
            readonly: true,
            configured: false,
            value: null,
          },
          DALTON_FEISHU_APP_SECRET: {
            source: 'vault_error',
            readonly: true,
            configured: false,
          },
        }),
      }),
    );

    renderPanel();
    const errorBadge = await screen.findByTestId('feishu-app-source-badge');
    expect(errorBadge).toHaveTextContent('主管理员 Vault 读取异常（只读锁定 · 请检查数据库或加密密钥）');
    expect(screen.getByTestId('feishu-vault-error-notice')).toBeInTheDocument();
    expect(screen.getByTestId('input-feishu-app-id')).toHaveValue('');
    expect(screen.getByTestId('input-feishu-app-id')).toBeDisabled();
    expect(screen.getByTestId('test-feishu-app-btn')).toBeDisabled();
    expect(screen.getByTestId('input-feishu-verify-token')).not.toBeDisabled();
  });

  it('shows plain Chinese errors for 409 revision_conflict, 409 env_locked, and 422 incomplete_credential_group, and displays /test verdicts', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.fetchMock.mockResolvedValue(makeResponse({ revision: 5 }));

    renderPanel();
    await screen.findByTestId('google-oauth-card');

    // 1) 422 incomplete_credential_group
    mocks.updateMock.mockRejectedValueOnce(
      makeApiError(422, {
        code: 'validation_error',
        errors: [
          {
            setting_name: 'DALTON_GOOGLE_OAUTH_CLIENT_SECRET',
            reason: 'incomplete_credential_group',
          },
        ],
      }),
    );
    fireEvent.change(screen.getByTestId('input-google-client-id'), {
      target: { value: 'google-client-id-1' },
    });
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const err422 = await screen.findByTestId('deployment-credentials-notice');
    expect(err422.textContent).toContain('成对填写完整');

    // 2) 409 revision_conflict
    mocks.updateMock.mockRejectedValueOnce(
      makeApiError(409, { code: 'revision_conflict', current_revision: 9 }),
    );
    fireEvent.change(screen.getByTestId('input-google-client-secret'), {
      target: { value: 'test-secret-not-real-google-0001' },
    });
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const errConflict = await screen.findByTestId('deployment-credentials-notice');
    expect(errConflict.textContent).toContain('v9');

    // 3) 409 env_locked
    mocks.updateMock.mockRejectedValueOnce(
      makeApiError(409, { code: 'env_locked', setting_name: 'DALTON_GOOGLE_OAUTH_CLIENT_ID' }),
    );
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const errEnv = await screen.findByTestId('deployment-credentials-notice');
    expect(errEnv.textContent).toContain('环境变量');

    // 4) /test probe button calls POST /test and displays structured result
    mocks.testMock.mockResolvedValueOnce({
      ok: true,
      network_reachable: true,
      credentials_valid: true,
      verdict: 'valid',
      detail_code: 'invalid_grant_verified',
    });
    fireEvent.click(screen.getByTestId('test-google-oauth-btn'));
    const probeOk = await screen.findByTestId('google-oauth-probe-result');
    expect(probeOk.textContent).toContain('凭据有效');
    expect(mocks.testMock).toHaveBeenCalledWith('google_oauth', {
      DALTON_GOOGLE_OAUTH_CLIENT_ID: 'google-client-id-1',
      DALTON_GOOGLE_OAUTH_CLIENT_SECRET: 'test-secret-not-real-google-0001',
    });
  });
});
