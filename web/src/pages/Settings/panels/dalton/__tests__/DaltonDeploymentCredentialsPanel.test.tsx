import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import i18n from '@/i18n';
import type { CapabilityId } from '@/hooks/useCapabilities';
import {
  PROBE_REQUEST_TIMEOUT_MS,
  parseDeploymentSettingsError,
  type DeploymentSettingField,
  type DeploymentSettingsResponse,
  type fetchDeploymentSettings,
  type testDeploymentCredentials,
  type updateDeploymentSettings,
} from '@/lib/dalton/deploymentCredentials';
import { queryKeys } from '@/lib/queryKeys';

const mocks = vi.hoisted(() => ({
  profile: 'core' as 'core' | 'everflow',
  brandName: 'Dalton',
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
      brandName: mocks.brandName,
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

function makeBaseFields(
  overrides: Partial<Record<string, Partial<DeploymentSettingField>>> = {},
): DeploymentSettingField[] {
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
  mocks.brandName = 'Dalton';
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

  it('shows coming-soon status for personal calendar switches and callback guidance when no boolean switches are registered', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.fetchMock.mockResolvedValueOnce(makeResponse());

    renderPanel();

    expect(await screen.findByTestId('google-connector-availability-badge')).toHaveTextContent(
      '个人连接器即将推出',
    );
    expect(screen.getByTestId('feishu-connector-availability-badge')).toHaveTextContent(
      '个人连接器即将推出',
    );
    const googleRow = screen.getByTestId('switch-row-coming-soon-google');
    const feishuRow = screen.getByTestId('switch-row-coming-soon-feishu');
    expect(googleRow).toHaveTextContent('即将推出');
    expect(feishuRow).toHaveTextContent('即将推出');
    expect(googleRow.querySelector('button[role="switch"]')).toBeDisabled();
    expect(feishuRow.querySelector('button[role="switch"]')).toBeDisabled();
  });

  it('hides plaintext IDs behind per-field configured/source badges, clears vault_error dots, and keeps feishu_event editable', async () => {
    await i18n.changeLanguage('zh-CN');

    // 1) vault_readonly: shows configured status & source without prefilling plaintext App ID; feishu_event stays editable
    const readonlyResp = makeResponse({
      revision: 7,
      fields: makeBaseFields({
        DALTON_GOOGLE_OAUTH_CLIENT_ID: {
          source: 'saved',
          readonly: false,
          configured: true,
          value: 'test-client-id-not-prefilled.apps.googleusercontent.com',
        },
        DALTON_GOOGLE_OAUTH_CLIENT_SECRET: {
          source: 'saved',
          readonly: false,
          configured: true,
        },
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

    // Neither Google Client ID nor Feishu App ID prefills plaintext values into inputs
    expect(screen.getByTestId('input-google-client-id')).toHaveValue('');
    expect(screen.getByTestId('input-feishu-app-id')).toHaveValue('');
    expect(document.body.textContent).not.toContain('test-client-id-not-prefilled');
    expect(document.body.textContent).not.toContain('cli_vault_readonly_001');

    // Per-field badges show configured + source accurately
    expect(screen.getByTestId('field-status-DALTON_GOOGLE_OAUTH_CLIENT_ID')).toHaveTextContent(
      '已设置 · 网页已保存',
    );
    expect(screen.getByTestId('field-status-DALTON_FEISHU_APP_ID')).toHaveTextContent(
      '已设置 · 现网 Vault 只读',
    );
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

    // 2) vault_error: distinct danger badge, no misleading '••••••••' placeholder on secret, feishu_event stays editable
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
    expect(screen.getByTestId('field-status-DALTON_FEISHU_APP_ID')).toHaveTextContent('读取异常');
    expect(screen.getByTestId('field-status-DALTON_FEISHU_APP_SECRET')).toHaveTextContent('读取异常');
    expect(screen.getByTestId('input-feishu-app-id')).toHaveAttribute('placeholder', '—');
    expect(screen.getByTestId('input-feishu-app-secret')).toHaveAttribute('placeholder', '—');
    expect(screen.getByTestId('input-feishu-app-id')).toBeDisabled();
    expect(screen.getByTestId('test-feishu-app-btn')).toBeDisabled();
    expect(screen.getByTestId('input-feishu-verify-token')).not.toBeDisabled();
  });

  it('binds drafts to their starting revision, blocks silent overwrite after refresh, and reconciles via rebase or discard', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.fetchMock
      .mockResolvedValueOnce(makeResponse({ revision: 5 }))
      .mockResolvedValueOnce(makeResponse({ revision: 9 }));

    renderPanel();
    await screen.findByTestId('google-oauth-card');

    // Begin editing on v5
    fireEvent.change(screen.getByTestId('input-google-client-id'), {
      target: { value: 'draft-client-id-v5' },
    });
    fireEvent.change(screen.getByTestId('input-google-client-secret'), {
      target: { value: 'test-secret-not-real-v5-0001' },
    });

    // Another admin updates server to v9; this admin refreshes
    fireEvent.click(screen.getByTestId('deployment-credentials-refresh'));

    const staleNotice = await screen.findByTestId('google_oauth-stale-draft-notice');
    expect(staleNotice.textContent).toContain('v9');
    expect(staleNotice.textContent).toContain('v5');
    // Save button stays locked until the admin reviews and explicitly reconciles
    expect(screen.getByTestId('save-google-oauth-btn')).toBeDisabled();

    // Explicitly re-base draft after reviewing the group
    fireEvent.click(screen.getByTestId('google_oauth-rebase-draft-btn'));
    expect(screen.queryByTestId('google_oauth-stale-draft-notice')).not.toBeInTheDocument();
    expect(screen.getByTestId('save-google-oauth-btn')).not.toBeDisabled();

    mocks.updateMock.mockResolvedValueOnce({
      revision: 10,
      changed: ['DALTON_GOOGLE_OAUTH_CLIENT_ID', 'DALTON_GOOGLE_OAUTH_CLIENT_SECRET'],
      pending_apply: false,
      warnings: [],
    });
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    await waitFor(() => {
      expect(mocks.updateMock).toHaveBeenCalledWith({
        expected_revision: 9,
        values: {
          DALTON_GOOGLE_OAUTH_CLIENT_ID: 'draft-client-id-v5',
          DALTON_GOOGLE_OAUTH_CLIENT_SECRET: 'test-secret-not-real-v5-0001',
        },
      });
    });
  });

  it('shares Save/Test draft resolution, clears stale probe verdicts on edit or revision change, and shows dedicated probe timeout/error messages', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.fetchMock
      .mockResolvedValueOnce(makeResponse({ revision: 5 }))
      .mockResolvedValueOnce(makeResponse({ revision: 6 }));

    renderPanel();
    await screen.findByTestId('google-oauth-card');

    // 1) Half-filled unset group sends identical payload (with null partner) to Save and Test
    fireEvent.change(screen.getByTestId('input-google-client-id'), {
      target: { value: 'google-client-id-1' },
    });
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
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const err422 = await screen.findByTestId('deployment-credentials-notice');
    expect(err422.textContent).toContain('成对填写完整');
    expect(mocks.updateMock).toHaveBeenCalledWith({
      expected_revision: 5,
      values: {
        DALTON_GOOGLE_OAUTH_CLIENT_ID: 'google-client-id-1',
        DALTON_GOOGLE_OAUTH_CLIENT_SECRET: null,
      },
    });

    mocks.testMock.mockResolvedValueOnce({
      ok: false,
      network_reachable: false,
      credentials_valid: null,
      verdict: 'format_invalid',
      detail_code: 'incomplete_credential_group',
    });
    fireEvent.click(screen.getByTestId('test-google-oauth-btn'));
    await screen.findByTestId('google-oauth-probe-result');
    expect(mocks.testMock).toHaveBeenCalledWith(
      'google_oauth',
      {
        DALTON_GOOGLE_OAUTH_CLIENT_ID: 'google-client-id-1',
        DALTON_GOOGLE_OAUTH_CLIENT_SECRET: null,
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    // 2) 409 revision_conflict & 409 env_locked
    mocks.updateMock.mockRejectedValueOnce(
      makeApiError(409, { code: 'revision_conflict', current_revision: 9 }),
    );
    fireEvent.change(screen.getByTestId('input-google-client-secret'), {
      target: { value: 'test-secret-not-real-google-0001' },
    });
    // Editing the secret immediately cleared the previous probe verdict
    expect(screen.queryByTestId('google-oauth-probe-result')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const errConflict = await screen.findByTestId('deployment-credentials-notice');
    expect(errConflict.textContent).toContain('v9');

    mocks.updateMock.mockRejectedValueOnce(
      makeApiError(409, { code: 'env_locked', setting_name: 'DALTON_GOOGLE_OAUTH_CLIENT_ID' }),
    );
    fireEvent.click(screen.getByTestId('save-google-oauth-btn'));
    const errEnv = await screen.findByTestId('deployment-credentials-notice');
    expect(errEnv.textContent).toContain('环境变量');

    // 3) Successful probe -> editing input clears green result
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
    expect(mocks.testMock).toHaveBeenLastCalledWith(
      'google_oauth',
      {
        DALTON_GOOGLE_OAUTH_CLIENT_ID: 'google-client-id-1',
        DALTON_GOOGLE_OAUTH_CLIENT_SECRET: 'test-secret-not-real-google-0001',
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    fireEvent.change(screen.getByTestId('input-google-client-id'), {
      target: { value: 'google-client-id-2' },
    });
    expect(screen.queryByTestId('google-oauth-probe-result')).not.toBeInTheDocument();

    // 4) Client timeout (ECONNABORTED) renders dedicated probe timeout error (never "保存未完成") & re-enables buttons
    mocks.testMock.mockRejectedValueOnce(
      new ApiError(`timeout of ${PROBE_REQUEST_TIMEOUT_MS}ms exceeded`, 'ECONNABORTED'),
    );
    fireEvent.click(screen.getByTestId('test-google-oauth-btn'));
    const timeoutErr = await screen.findByTestId('google-oauth-probe-error');
    expect(timeoutErr.textContent).toContain('测试请求超时');
    expect(timeoutErr.textContent).not.toContain('保存未完成');
    expect(screen.getByTestId('test-google-oauth-btn')).not.toBeDisabled();
    expect(screen.getByTestId('test-feishu-app-btn')).not.toBeDisabled();

    // 5) Revision refresh also clears stale probe errors/results
    fireEvent.click(screen.getByTestId('deployment-credentials-refresh'));
    await waitFor(() => {
      expect(screen.getByTestId('deployment-credentials-revision-badge')).toHaveTextContent('v6');
    });
    expect(screen.queryByTestId('google-oauth-probe-error')).not.toBeInTheDocument();
  });

  it('uses useCapabilities().profile and brandName for the profile badge and parses client timeout errors', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.profile = 'everflow';
    mocks.brandName = 'Everflow';
    mocks.fetchMock.mockResolvedValueOnce(makeResponse({ profile: 'core', revision: 2 }));

    renderPanel();

    const profileBadge = await screen.findByTestId('deployment-credentials-profile-badge');
    expect(profileBadge).toHaveTextContent('Everflow');
    expect(profileBadge.textContent).not.toContain('标准管理形态');

    const parsedTimeout = parseDeploymentSettingsError(
      new ApiError('timeout of 12000ms exceeded', 'ECONNABORTED'),
    );
    expect(parsedTimeout.isTimeout).toBe(true);
    expect(parsedTimeout.isNetworkError).toBe(false);
    expect(PROBE_REQUEST_TIMEOUT_MS).toBeGreaterThanOrEqual(10_000);
  });

  it('locks group inputs while probing and discards late probe responses after draft change or refresh', async () => {
    await i18n.changeLanguage('zh-CN');
    mocks.fetchMock
      .mockResolvedValueOnce(makeResponse({ revision: 5 }))
      .mockResolvedValueOnce(makeResponse({ revision: 5 }));

    renderPanel();
    await screen.findByTestId('google-oauth-card');

    const googleIdInput = screen.getByTestId('input-google-client-id');
    const googleSecretInput = screen.getByTestId('input-google-client-secret');
    const feishuIdInput = screen.getByTestId('input-feishu-app-id');
    const feishuSecretInput = screen.getByTestId('input-feishu-app-secret');

    fireEvent.change(googleIdInput, { target: { value: 'google-client-id-live' } });
    fireEvent.change(googleSecretInput, { target: { value: 'test-secret-not-real-0001' } });

    let resolveGoogleProbe!: (value: Awaited<ReturnType<typeof testDeploymentCredentials>>) => void;
    let googleProbeSignal: AbortSignal | undefined;
    mocks.testMock.mockImplementationOnce(
      (_target, _values, opts) =>
        new Promise((resolve) => {
          googleProbeSignal = opts?.signal;
          resolveGoogleProbe = resolve;
        }),
    );

    fireEvent.click(screen.getByTestId('test-google-oauth-btn'));

    // While probing Google OAuth, Google inputs & save button are locked; Feishu inputs remain editable
    expect(googleIdInput).toBeDisabled();
    expect(googleSecretInput).toBeDisabled();
    expect(screen.getByTestId('save-google-oauth-btn')).toBeDisabled();
    expect(screen.getByTestId('test-google-oauth-btn')).toBeDisabled();
    expect(screen.getByTestId('test-feishu-app-btn')).toBeDisabled();
    expect(feishuIdInput).not.toBeDisabled();

    // Even if a draft change fires while the request is in flight, the probe epoch advances and aborts the flight
    fireEvent.change(googleSecretInput, { target: { value: 'test-secret-not-real-0002' } });
    expect(googleProbeSignal?.aborted).toBe(true);
    expect(googleIdInput).not.toBeDisabled();

    // Late resolution from the superseded probe is discarded instead of stamping "凭据有效" onto the new draft
    resolveGoogleProbe({
      ok: true,
      network_reachable: true,
      credentials_valid: true,
      verdict: 'valid',
      detail_code: 'invalid_grant_verified',
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByTestId('google-oauth-probe-result')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('test-secret-not-real-0001');
    expect(document.body.textContent).not.toContain('test-secret-not-real-0002');

    // Feishu app probe locks Feishu inputs and discards late response when Refresh is clicked in flight
    fireEvent.change(feishuIdInput, { target: { value: 'cli_probe_lock_001' } });
    fireEvent.change(feishuSecretInput, { target: { value: 'test-secret-not-real-0003' } });

    let resolveFeishuProbe!: (value: Awaited<ReturnType<typeof testDeploymentCredentials>>) => void;
    let feishuProbeSignal: AbortSignal | undefined;
    mocks.testMock.mockImplementationOnce(
      (_target, _values, opts) =>
        new Promise((resolve) => {
          feishuProbeSignal = opts?.signal;
          resolveFeishuProbe = resolve;
        }),
    );

    fireEvent.click(screen.getByTestId('test-feishu-app-btn'));
    expect(feishuIdInput).toBeDisabled();
    expect(feishuSecretInput).toBeDisabled();
    expect(screen.getByTestId('save-feishu-app-btn')).toBeDisabled();

    fireEvent.click(screen.getByTestId('deployment-credentials-refresh'));
    expect(feishuProbeSignal?.aborted).toBe(true);

    resolveFeishuProbe({
      ok: true,
      network_reachable: true,
      credentials_valid: true,
      verdict: 'valid',
      detail_code: 'feishu_token_ok',
    });
    await waitFor(() => {
      expect(mocks.fetchMock).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByTestId('feishu-app-probe-result')).not.toBeInTheDocument();
    expect(feishuIdInput).not.toBeDisabled();
  });

  it('clears prior green probe verdict after same-revision Vault refresh and displays accurate Feishu events URL guidance', async () => {
    await i18n.changeLanguage('zh-CN');
    const vaultResp = makeResponse({
      revision: 7,
      fields: makeBaseFields({
        DALTON_FEISHU_APP_ID: {
          source: 'vault_readonly',
          readonly: true,
          configured: true,
          value: 'cli_vault_same_rev_001',
        },
        DALTON_FEISHU_APP_SECRET: {
          source: 'vault_readonly',
          readonly: true,
          configured: true,
        },
      }),
    });
    // Both initial load and post-Vault-rotation refresh return the same deployment revision v7
    mocks.fetchMock.mockResolvedValueOnce(vaultResp).mockResolvedValueOnce({ ...vaultResp });
    mocks.testMock.mockResolvedValueOnce({
      ok: true,
      network_reachable: true,
      credentials_valid: true,
      verdict: 'valid',
      detail_code: 'feishu_token_ok',
    });

    renderPanel();
    await screen.findByTestId('feishu-credentials-card');

    // Bilingual guidance tells admins to register the Events URL only after event subscriptions launch
    const zhHint = screen.getByTestId('feishu-callback-availability-hint');
    expect(zhHint.textContent).toContain('事件订阅上线后');
    expect(zhHint.textContent).toContain('保存事件验签凭据');

    await i18n.changeLanguage('en-US');
    const enHint = await screen.findByTestId('feishu-callback-availability-hint');
    expect(enHint.textContent).toContain('after event subscriptions go live');

    await i18n.changeLanguage('zh-CN');
    fireEvent.click(screen.getByTestId('test-feishu-app-btn'));
    const greenBadge = await screen.findByTestId('feishu-app-probe-result');
    expect(greenBadge.textContent).toContain('凭据有效');

    // Vault key rotated out-of-band without incrementing deployment revision -> Refresh must clear old green badge
    fireEvent.click(screen.getByTestId('deployment-credentials-refresh'));
    await waitFor(() => {
      expect(mocks.fetchMock).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByTestId('feishu-app-probe-result')).not.toBeInTheDocument();
  });
});
