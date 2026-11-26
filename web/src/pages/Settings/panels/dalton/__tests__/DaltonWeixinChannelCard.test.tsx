import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import type { CapabilityId } from '@/hooks/useCapabilities';
import { DaltonWeixinBindCard } from '../DaltonWeixinBindCard';
import { DaltonWeixinBotCard } from '../DaltonWeixinBotCard';
import { encodeQrCodeMatrix } from '../qrCodeSvg';
import type {
  DaltonChannelBindingsResponse,
  DaltonChannelUnbindResponse,
  DaltonWeixinAdminConfigUpdatePayload,
  DaltonWeixinAdminConfigUpdateResponse,
  DaltonWeixinAdminOverviewResponse,
  DaltonWeixinAdminUnbindReason,
  DaltonWeixinMemberAccountView,
  DaltonWeixinQrPublicResponse,
  DaltonWeixinQrRequestOptions,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  callerTier: 'internal' as string | null,
  capabilities: new Set<CapabilityId>(['channel_gateway', 'channel_weixin']) as ReadonlySet<CapabilityId>,
  fetchBindings: vi.fn<() => Promise<DaltonChannelBindingsResponse>>(),
  unbind: vi.fn<(platform: string) => Promise<DaltonChannelUnbindResponse>>(),
  startQr: vi.fn<(options?: DaltonWeixinQrRequestOptions) => Promise<DaltonWeixinQrPublicResponse>>(),
  waitQr: vi.fn<
    (sessionKey: string, options?: DaltonWeixinQrRequestOptions) => Promise<DaltonWeixinQrPublicResponse>
  >(),
  verifyQr: vi.fn<
    (
      sessionKey: string,
      code: string,
      options?: DaltonWeixinQrRequestOptions,
    ) => Promise<DaltonWeixinQrPublicResponse>
  >(),
  fetchAdminOverview: vi.fn<() => Promise<DaltonWeixinAdminOverviewResponse>>(),
  updateAdminConfig: vi.fn<
    (payload: DaltonWeixinAdminConfigUpdatePayload) => Promise<DaltonWeixinAdminConfigUpdateResponse>
  >(),
  adminUnbindAccount: vi.fn<
    (accountId: string, reason: DaltonWeixinAdminUnbindReason) => Promise<DaltonChannelUnbindResponse>
  >(),
  adminRevokeUser: vi.fn<
    (targetUserId: string, reason: DaltonWeixinAdminUnbindReason) => Promise<DaltonChannelUnbindResponse>
  >(),
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      profile: 'everflow' as const,
      capabilities: h.capabilities,
      hasCapability: (req: Parameters<typeof actual.hasRequiredCapability>[1]) =>
        actual.hasRequiredCapability(h.capabilities, req),
      brandName: 'Dalton',
      fingerprint: 'test-fp',
      tickerLogosEnabled: true,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: false,
      isEverflowBuild: true,
    }),
  };
});

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const tier = h.callerTier ?? 'unknown';
    const ready = tier === 'admin' || tier === 'internal' || tier === 'external';
    const snapshot = {
      tier: ready ? tier : 'unknown',
      status: ready ? 'ready' : 'error',
      authMode: ready ? 'local' : null,
      isPlatform: false,
      isKnown: ready,
      isConfirming: false,
      isMultiUserAuth: ready,
      isSingleUserOss: false,
      isAdmin: ready && tier === 'admin',
      isInternal: ready && (tier === 'admin' || tier === 'internal'),
      isExternal: ready && tier === 'external',
      canActAsOwner: ready && (tier === 'admin' || tier === 'internal'),
      canPublishPublic: ready && tier === 'admin',
      canInvite: ready && (tier === 'admin' || tier === 'internal'),
      isTeamCredentialsShared: true,
      canManageCredentials: ready && tier === 'admin',
      isPm: ready && tier === 'admin',
      colleagueSharingEnabled: false,
    };
    return {
      ...snapshot,
      getLatestIdentity: () => snapshot,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

vi.mock('@/pages/Settings/utils/daltonApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/utils/daltonApi')>();
  return {
    ...actual,
    fetchDaltonChannelBindings: () => h.fetchBindings(),
    unbindDaltonChannel: (platform: string) => h.unbind(platform),
    startDaltonWeixinQrLogin: (options?: DaltonWeixinQrRequestOptions) => h.startQr(options),
    waitDaltonWeixinQrLogin: (sessionKey: string, options?: DaltonWeixinQrRequestOptions) =>
      h.waitQr(sessionKey, options),
    verifyDaltonWeixinQrCode: (
      sessionKey: string,
      code: string,
      options?: DaltonWeixinQrRequestOptions,
    ) => h.verifyQr(sessionKey, code, options),
    fetchDaltonWeixinAdminOverview: () => h.fetchAdminOverview(),
    updateDaltonWeixinAdminConfig: (payload: DaltonWeixinAdminConfigUpdatePayload) =>
      h.updateAdminConfig(payload),
    adminUnbindDaltonWeixinAccount: (
      accountId: string,
      reason: DaltonWeixinAdminUnbindReason,
    ) => h.adminUnbindAccount(accountId, reason),
    adminRevokeDaltonWeixinUser: (
      targetUserId: string,
      reason: DaltonWeixinAdminUnbindReason,
    ) => h.adminRevokeUser(targetUserId, reason),
  };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeWeixinAccount(
  overrides: Partial<DaltonWeixinMemberAccountView> = {},
): DaltonWeixinMemberAccountView {
  return {
    linked: false,
    platform: 'weixin',
    status: 'unconfigured',
    channel_enabled: true,
    allow_external: true,
    member_allowed: true,
    deny_reason: null,
    account_id: null,
    platform_user_id: null,
    masked_ilink_user_id: null,
    platform_username: null,
    ilink_username: null,
    active_push_ready: false,
    last_inbound_at: null,
    updated_at: null,
    ...overrides,
  };
}

function makeBindings(options: {
  userTier?: string;
  weixinAccount?: Partial<DaltonWeixinMemberAccountView> | null;
} = {}): DaltonChannelBindingsResponse {
  const wx =
    options.weixinAccount === null
      ? null
      : makeWeixinAccount(options.weixinAccount);
  return {
    user_id: 'u-1',
    user_tier: options.userTier ?? h.callerTier ?? 'internal',
    home_workspace_id: 'home-ws',
    bindings: {
      feishu: {
        linked: false,
        platform: 'feishu',
        platform_user_id: null,
        platform_username: null,
        updated_at: null,
      },
      discord: {
        linked: false,
        platform: 'discord',
        platform_user_id: null,
        platform_username: null,
        updated_at: null,
      },
      weixin: {
        linked: wx?.linked === true,
        platform: 'weixin',
        account_id: wx?.account_id ?? null,
        status: wx?.status ?? null,
        platform_user_id: wx?.platform_user_id ?? null,
        masked_ilink_user_id: wx?.masked_ilink_user_id ?? null,
        platform_username: wx?.ilink_username ?? null,
        has_usable_context_token: wx?.active_push_ready ?? false,
        updated_at: wx?.updated_at ?? null,
      },
    },
    identities: {},
    settings: { version: '1', apps: {} },
    chat_bindings: [],
    weixin_account: wx,
  };
}

function makeAdminOverview(
  overrides: Partial<DaltonWeixinAdminOverviewResponse> = {},
): DaltonWeixinAdminOverviewResponse {
  return {
    enabled: true,
    allow_external: true,
    db_allow_external: true,
    accounts_revision: 5,
    max_concurrent_accounts: 200,
    active_poller_count: 3,
    connected_count: 2,
    pending_verification_count: 1,
    expired_count: 1,
    gateway_lease: {
      instance_id: 'gw-1',
      epoch: 4,
      acquired_at: '2026-11-26T00:00:00Z',
      expires_at: '2026-11-26T00:00:45Z',
      active: true,
    },
    accounts: [
      {
        account_id: 'acc-alice',
        user_id: 'u-alice',
        user_email: 'alice@example.com',
        user_name: 'Alice',
        user_tier: 'internal',
        ilink_bot_id: 'bot-a',
        ilink_username: 'AliceWX',
        masked_ilink_user_id: '****a1b2',
        status: 'active',
        prev_status: null,
        disabled_reason: null,
        bot_generation: 2,
        has_pending_login: false,
        pending_login_status: null,
        last_inbound_at: '2026-11-26T04:00:00Z',
        last_error_code: null,
        updated_at: '2026-11-26T04:10:00Z',
      },
      {
        account_id: null,
        user_id: 'u-bob',
        user_email: 'bob@example.com',
        user_name: 'Bob',
        user_tier: 'external',
        ilink_bot_id: null,
        ilink_username: null,
        masked_ilink_user_id: null,
        status: 'login_pending',
        prev_status: null,
        disabled_reason: null,
        bot_generation: null,
        has_pending_login: true,
        pending_login_status: 'starting',
        last_inbound_at: null,
        last_error_code: null,
        updated_at: '2026-11-26T04:12:00Z',
      },
    ],
    audit_logs: [
      {
        audit_id: 'audit-1',
        action: 'admin_unbind',
        operator_user_id: 'u-admin',
        target_user_id: 'u-old',
        weixin_account_id: 'acc-old',
        ilink_bot_id: 'bot-old',
        masked_ilink_user_id: '****0001',
        reason: '离职交接',
        metadata: {},
        created_at: '2026-11-26T03:00:00Z',
      },
    ],
    ...overrides,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

describe('DaltonWeixinBindCard (personal "我的微信") & DaltonWeixinBotCard (admin "团队微信渠道管理")', () => {
  beforeEach(() => {
    h.callerTier = 'internal';
    h.capabilities = new Set<CapabilityId>(['channel_gateway', 'channel_weixin']);
    h.fetchBindings.mockReset();
    h.unbind.mockReset();
    h.startQr.mockReset();
    h.waitQr.mockReset();
    h.verifyQr.mockReset();
    h.fetchAdminOverview.mockReset();
    h.updateAdminConfig.mockReset();
    h.adminUnbindAccount.mockReset();
    h.adminRevokeUser.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('walks personal QR flow wait -> scaned -> verify_code (invalid retry -> confirmed) and invalidates bindings query', async () => {
    vi.useFakeTimers();
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-1',
      status: 'wait',
      qrcode_url: 'data:image/png;base64,AAA',
    });
    h.waitQr
      .mockResolvedValueOnce({ session_key: 'sess-1', status: 'scaned_but_redirect' })
      .mockResolvedValueOnce({ session_key: 'sess-1', status: 'verify_code' });
    // First verification attempt fails with invalid_verify_code -> keeps input open for retry
    h.verifyQr
      .mockRejectedValueOnce(
        new ApiError('Bad Request', 'ERR_BAD_REQUEST', {
          status: 400,
          statusText: 'Bad Request',
          headers: {},
          data: { code: 'invalid_verify_code', message: 'invalid code' },
        }),
      )
      .mockResolvedValueOnce({
        session_key: 'sess-1',
        status: 'confirmed',
        connected: true,
        account_status: 'pending_verification',
      });

    renderWithClient(<DaltonWeixinBindCard />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(h.fetchBindings).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByTestId('dalton-weixin-qr-scaned')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute(
      'src',
      'data:image/png;base64,AAA',
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1300);
    });
    expect(screen.getByTestId('dalton-weixin-verify-code-input')).toBeInTheDocument();

    // Wrong code shows error and keeps input visible for re-entry
    fireEvent.change(screen.getByTestId('dalton-weixin-verify-code-input'), {
      target: { value: '000000' },
    });
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByTestId('dalton-weixin-qr-error')).toHaveTextContent(
      'dalton.weixinBind.errors.invalidVerifyCode',
    );
    expect(screen.getByTestId('dalton-weixin-verify-code-input')).toBeInTheDocument();

    // Retry with valid code -> confirmed with pending_verification status & refreshes bindings
    fireEvent.change(screen.getByTestId('dalton-weixin-verify-code-input'), {
      target: { value: '842190' },
    });
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    expect(h.verifyQr).toHaveBeenLastCalledWith(
      'sess-1',
      '842190',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByTestId('dalton-weixin-qr-confirmed')).toHaveTextContent(
      'dalton.weixinBind.connectedPendingVerify',
    );
    // Bindings query invalidated and refetched after confirmation
    expect(h.fetchBindings).toHaveBeenCalledTimes(2);
  });

  it('ignores stale 409/need_rescan from an old verify_code request after rescanning a new QR session', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-old',
        status: 'wait',
        qrcode_url: 'https://ilinkai.weixin.qq.com/qr/old',
      })
      .mockResolvedValueOnce({
        session_key: 'sess-new',
        status: 'wait',
        qrcode_url: 'https://ilinkai.weixin.qq.com/qr/new',
      });
    h.waitQr
      .mockResolvedValueOnce({ session_key: 'sess-old', status: 'verify_code' })
      .mockImplementationOnce(() => new Promise(() => {}));

    let rejectOldVerify!: (err: unknown) => void;
    let oldVerifySignal: AbortSignal | undefined;
    h.verifyQr.mockImplementationOnce((_key, _code, opts) => {
      oldVerifySignal = opts?.signal;
      return new Promise((_resolve, reject) => {
        rejectOldVerify = reject;
      });
    });

    renderWithClient(<DaltonWeixinBindCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-start-qr'));
    const input = await screen.findByTestId('dalton-weixin-verify-code-input');
    fireEvent.change(input, { target: { value: '123456' } });
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));
    await waitFor(() => expect(h.verifyQr).toHaveBeenCalledTimes(1));

    // User rescans while old verify is still in flight
    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    expect(oldVerifySignal?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute(
        'src',
        'https://ilinkai.weixin.qq.com/qr/new',
      ),
    );

    // Late 409/need_rescan from old verify must NEVER overwrite or stop the new QR session
    await act(async () => {
      rejectOldVerify(
        new ApiError('Conflict', 'ERR_BAD_REQUEST', {
          status: 409,
          statusText: 'Conflict',
          headers: {},
          data: { code: 'session_revoked', revoked: true, need_rescan: true },
        }),
      );
      await Promise.resolve();
    });

    expect(screen.queryByTestId('dalton-weixin-qr-expired')).not.toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-wait')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute(
      'src',
      'https://ilinkai.weixin.qq.com/qr/new',
    );
  });

  it('refreshes bindings when admin revokes session during verify_code stage', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-revoked',
      status: 'wait',
      qrcode_url: 'data:image/png;base64,AAA',
    });
    h.waitQr.mockResolvedValueOnce({ session_key: 'sess-revoked', status: 'verify_code' });
    h.verifyQr.mockRejectedValueOnce(
      new ApiError('Conflict', 'ERR_BAD_REQUEST', {
        status: 409,
        statusText: 'Conflict',
        headers: {},
        data: { code: 'session_revoked', revoked: true, need_rescan: true },
      }),
    );

    renderWithClient(<DaltonWeixinBindCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-start-qr'));
    const input = await screen.findByTestId('dalton-weixin-verify-code-input');
    fireEvent.change(input, { target: { value: '123456' } });
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));

    expect(await screen.findByTestId('dalton-weixin-qr-expired')).toHaveTextContent(
      'dalton.weixinBind.qrRevoked',
    );
    await waitFor(() => expect(h.fetchBindings).toHaveBeenCalledTimes(2));
  });

  it('renders scannable QR graphics for HTTPS image URLs (with SVG fallback on error) and liteapp QR text URLs', async () => {
    expect(encodeQrCodeMatrix('https://liteapp.weixin.qq.com/q/test-123')?.size).toBeGreaterThanOrEqual(29);

    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-https',
        status: 'wait',
        qrcode_img_content: 'https://ilinkai.weixin.qq.com/qr/2',
        qrcode_url: 'https://ilinkai.weixin.qq.com/qr/2',
      })
      .mockResolvedValueOnce({
        session_key: 'sess-liteapp',
        status: 'wait',
        qrcode_img_content: 'https://liteapp.weixin.qq.com/q/abc123',
        qrcode_url: 'https://liteapp.weixin.qq.com/q/abc123',
      });
    h.waitQr.mockImplementation(() => new Promise(() => {}));

    renderWithClient(<DaltonWeixinBindCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-start-qr'));

    const img = await screen.findByTestId('dalton-weixin-qr-image');
    expect(img).toHaveAttribute('src', 'https://ilinkai.weixin.qq.com/qr/2');

    // If an HTTPS QR URL returns non-image content, onError falls back to an inline SVG QR code
    fireEvent.error(img);
    expect(await screen.findByTestId('dalton-weixin-qr-svg')).toBeInTheDocument();

    // Known WeChat liteapp URL renders inline SVG QR code immediately
    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    expect(await screen.findByTestId('dalton-weixin-qr-svg')).toBeInTheDocument();

    // Payload exceeding Version 20 capacity (>666 bytes) shows link fallback without SVG/img
    const longUrl = `https://liteapp.weixin.qq.com/q/${'x'.repeat(680)}`;
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-too-long',
      status: 'wait',
      qrcode_img_content: longUrl,
      qrcode_url: longUrl,
    });
    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    expect(await screen.findByTestId('dalton-weixin-qr-too-long')).toHaveTextContent(
      'dalton.weixinBind.qrTooLong',
    );
    expect(screen.getByTestId('dalton-weixin-qr-open-link')).toHaveAttribute('href', longUrl);
    expect(screen.queryByTestId('dalton-weixin-qr-svg')).not.toBeInTheDocument();
  });

  it('disables verify-code form during rescan/unbind, clears stale QR on unbind failure with retry button, and handles weixin_login_failed', async () => {
    h.fetchBindings.mockResolvedValue(
      makeBindings({
        weixinAccount: {
          linked: true,
          status: 'active',
          account_id: 'acc-existing',
          platform_user_id: 'wx-existing',
          masked_ilink_user_id: '****ting',
        },
      }),
    );
    let resolveRescan: ((v: DaltonWeixinQrPublicResponse) => void) | null = null;
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-vc',
        status: 'verify_code',
        qrcode_url: 'data:image/png;base64,AAA',
      })
      .mockImplementationOnce(
        () =>
          new Promise<DaltonWeixinQrPublicResponse>((resolve) => {
            resolveRescan = resolve;
          }),
      );
    h.waitQr.mockImplementation(() => new Promise(() => {}));

    renderWithClient(<DaltonWeixinBindCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-start-qr'));
    const vcInput = await screen.findByTestId('dalton-weixin-verify-code-input');
    fireEvent.change(vcInput, { target: { value: '654321' } });

    // Start rescan -> old verify-code input & submit button are disabled while startMutation is pending
    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    await waitFor(() => {
      expect(screen.getByTestId('dalton-weixin-verify-code-input')).toBeDisabled();
      expect(screen.getByTestId('dalton-weixin-verify-code-submit')).toBeDisabled();
    });

    // Finish rescan with a wait QR, then trigger unbind failure -> stale QR cleared & retry button shown
    await act(async () => {
      resolveRescan?.({
        session_key: 'sess-new',
        status: 'wait',
        qrcode_url: 'data:image/png;base64,BBB',
      });
    });
    expect(await screen.findByTestId('dalton-weixin-qr-image')).toBeInTheDocument();

    h.unbind.mockRejectedValueOnce(new Error('network_error'));
    fireEvent.click(screen.getByTestId('dalton-weixin-unbind'));
    const unbindDialog = await screen.findByRole('dialog');
    fireEvent.click(within(unbindDialog).getByRole('button', { name: 'dalton.weixinBind.unbind' }));

    expect(await screen.findByTestId('dalton-weixin-qr-failed')).toHaveTextContent(
      'dalton.weixinBind.unbindFailed',
    );
    expect(screen.getByTestId('dalton-weixin-retry-qr')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-qr-panel')).not.toBeInTheDocument();

    // weixin_rate_limited during verify-code keeps input open with retry-later message
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-fail-vc',
      status: 'verify_code',
      qrcode_url: 'data:image/png;base64,CCC',
    });
    h.verifyQr
      .mockRejectedValueOnce(
        new ApiError('Too Many Requests', 'ERR_BAD_REQUEST', {
          status: 429,
          statusText: 'Too Many Requests',
          headers: {},
          data: { code: 'weixin_rate_limited' },
        }),
      )
      .mockRejectedValueOnce(
        new ApiError('Bad Gateway', 'ERR_BAD_RESPONSE', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: {},
          data: { code: 'weixin_login_failed', need_rescan: true },
        }),
      );
    fireEvent.click(screen.getByTestId('dalton-weixin-retry-qr'));
    const vcInput2 = await screen.findByTestId('dalton-weixin-verify-code-input');
    fireEvent.change(vcInput2, { target: { value: '111111' } });
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));
    expect(await screen.findByTestId('dalton-weixin-qr-error')).toHaveTextContent(
      'dalton.weixinBind.errors.rateLimited',
    );
    expect(screen.getByTestId('dalton-weixin-verify-code-input')).toBeInTheDocument();

    // weixin_login_failed during verify-code prompts rescan with retry button
    fireEvent.click(screen.getByTestId('dalton-weixin-verify-code-submit'));
    expect(await screen.findByTestId('dalton-weixin-qr-failed')).toHaveTextContent(
      'dalton.weixinBind.errors.weixinLoginFailed',
    );
    expect(screen.getByTestId('dalton-weixin-retry-qr')).toBeInTheDocument();
  });

  it('aborts in-flight wait request on unmount and on rescan so only one wait runs at a time', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-a',
        status: 'wait',
        qrcode_url: 'data:image/png;base64,AAA',
      })
      .mockResolvedValueOnce({
        session_key: 'sess-b',
        status: 'wait',
        qrcode_url: 'data:image/png;base64,BBB',
      });

    const signals: Array<AbortSignal | undefined> = [];
    h.waitQr.mockImplementation((_sessionKey, opts) => {
      signals.push(opts?.signal);
      return new Promise(() => {});
    });

    const { unmount } = renderWithClient(<DaltonWeixinBindCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-start-qr'));
    await waitFor(() => expect(h.waitQr).toHaveBeenCalledTimes(1));
    expect(signals[0]?.aborted).toBe(false);

    // Rescan aborts the first in-flight wait before starting the second
    fireEvent.click(screen.getByTestId('dalton-weixin-start-qr'));
    expect(signals[0]?.aborted).toBe(true);
    await waitFor(() => expect(h.waitQr).toHaveBeenCalledTimes(2));
    expect(signals[1]?.aborted).toBe(false);

    // Unmount after wait is issued aborts the active in-flight wait
    unmount();
    expect(signals[1]?.aborted).toBe(true);
  });

  it('enters explicit failure state clearing stale QR on conflict/capacity errors, refreshes on expired click, and completes full-capacity rescan replacement', async () => {
    // Already connected account (e.g. slot held at 200/200) -> completes same-account rescan replacement
    h.fetchBindings
      .mockResolvedValueOnce(
        makeBindings({
          weixinAccount: {
            linked: true,
            status: 'active',
            account_id: 'acc-1',
            ilink_username: 'AliceWX',
            masked_ilink_user_id: '****a1b2',
            active_push_ready: true,
            updated_at: '2026-11-26T04:10:00Z',
          },
        }),
      )
      .mockResolvedValue(
        makeBindings({
          weixinAccount: {
            linked: true,
            status: 'active',
            account_id: 'acc-1',
            ilink_username: 'AliceWX-Renewed',
            masked_ilink_user_id: '****a1b2',
            active_push_ready: true,
            bot_generation: 2,
            updated_at: '2026-11-26T05:00:00Z',
          },
        }),
      );

    // 1) Conflict error clears stale QR and offers retry button
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-conflict',
      status: 'wait',
      qrcode_url: 'data:image/png;base64,AAA',
    });
    h.waitQr.mockRejectedValueOnce(
      new ApiError('Conflict', 'ERR_BAD_REQUEST', {
        status: 409,
        statusText: 'Conflict',
        headers: {},
        data: { code: 'ilink_user_already_bound', message: 'already bound' },
      }),
    );

    renderWithClient(<DaltonWeixinBindCard />);
    const startBtn = await screen.findByTestId('dalton-weixin-start-qr');
    expect(startBtn).toHaveTextContent('dalton.weixinBind.rescanConnect');
    fireEvent.click(startBtn);

    const failedNotice = await screen.findByTestId('dalton-weixin-qr-failed');
    expect(failedNotice).toHaveTextContent('dalton.weixinBind.errors.alreadyBound');
    // Stale QR image & polling spinner are cleared
    expect(screen.queryByTestId('dalton-weixin-qr-image')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-qr-panel')).not.toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-retry-qr')).toBeInTheDocument();

    // 2) Clicking retry hits expired QR, then clicking Refresh QR completes full-capacity rescan replacement
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-exp',
        status: 'wait',
        qrcode_url: 'data:image/png;base64,EXP',
      })
      .mockResolvedValueOnce({
        session_key: 'sess-rescan-ok',
        status: 'wait',
        qrcode_url: 'data:image/png;base64,OK',
      });
    h.waitQr
      .mockResolvedValueOnce({ session_key: 'sess-exp', status: 'expired', connected: false })
      .mockResolvedValueOnce({
        session_key: 'sess-rescan-ok',
        status: 'confirmed',
        connected: true,
        account_id: 'acc-1',
        bot_generation: 2,
      });

    fireEvent.click(screen.getByTestId('dalton-weixin-retry-qr'));
    const refreshBtn = await screen.findByTestId('dalton-weixin-refresh-qr');
    fireEvent.click(refreshBtn);

    expect(await screen.findByTestId('dalton-weixin-qr-confirmed')).toHaveTextContent(
      'dalton.weixinBind.connectedSuccess',
    );
    expect(await screen.findByText('AliceWX-Renewed')).toBeInTheDocument();
  });

  it('renders all 5 personal status badges, connected info, push state, always-visible forward guide, and confirm unbind', async () => {
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        weixinAccount: {
          linked: true,
          status: 'active',
          account_id: 'acc-1',
          ilink_username: 'AliceWX',
          masked_ilink_user_id: '****a1b2',
          active_push_ready: true,
          last_inbound_at: '2026-11-26T04:00:00Z',
          updated_at: '2026-11-26T04:10:00Z',
        },
      }),
    );
    h.unbind.mockResolvedValueOnce({ ok: true, platform: 'weixin', deleted_account: true });

    const { unmount: unmountActive } = renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-status-badge')).toHaveTextContent(
      'dalton.weixinBind.statusActive',
    );
    expect(screen.getByText('AliceWX')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-masked-id')).toHaveTextContent('****a1b2');
    expect(screen.getByTestId('dalton-weixin-push-status')).toHaveTextContent(
      'dalton.weixinBind.pushReadyBadge',
    );
    expect(screen.getByTestId('dalton-weixin-forward-guide')).toHaveTextContent(
      'dalton.weixinBind.forwardGuide',
    );

    fireEvent.click(screen.getByTestId('dalton-weixin-unbind'));
    expect(h.unbind).not.toHaveBeenCalled();
    const confirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.weixinBind.unbind',
    });
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);
    await waitFor(() => expect(h.unbind).toHaveBeenCalledWith('weixin'));
    unmountActive();

    // pending_verification
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        weixinAccount: {
          linked: true,
          status: 'pending_verification',
          account_id: 'acc-2',
          ilink_username: 'PendingWX',
          masked_ilink_user_id: null,
          active_push_ready: false,
          updated_at: '2026-11-26T04:10:00Z',
        },
      }),
    );
    const { unmount: unmountPending } = renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-status-badge')).toHaveTextContent(
      'dalton.weixinBind.statusPendingVerification',
    );
    expect(screen.getByTestId('dalton-weixin-pending-verification-notice')).toBeInTheDocument();
    unmountPending();

    // expired
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        weixinAccount: {
          linked: false,
          status: 'expired',
          account_id: 'acc-3',
          ilink_username: 'OldWX',
          masked_ilink_user_id: '****9999',
        },
      }),
    );
    const { unmount: unmountExpired } = renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-status-badge')).toHaveTextContent(
      'dalton.weixinBind.statusExpired',
    );
    unmountExpired();

    // paused (channel disabled for internal member)
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        weixinAccount: {
          linked: false,
          status: 'paused',
          channel_enabled: false,
          allow_external: false,
          member_allowed: false,
          deny_reason: 'weixin_channel_disabled',
        },
      }),
    );
    renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-status-badge')).toHaveTextContent(
      'dalton.weixinBind.statusPaused',
    );
    expect(screen.getByTestId('dalton-weixin-start-qr')).toBeDisabled();
  });

  it('hides personal WeChat card for external members when allow_external is false even when channel_enabled is ALSO false, while keeping admin and internal visible', async () => {
    // BOTH switches off: backend returns deny_reason='weixin_channel_disabled' AND allow_external=false
    h.callerTier = 'external';
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        userTier: 'external',
        weixinAccount: {
          status: 'paused',
          channel_enabled: false,
          allow_external: false,
          member_allowed: false,
          deny_reason: 'weixin_channel_disabled',
        },
      }),
    );
    const { unmount: unmountBothOff } = renderWithClient(<DaltonWeixinBindCard />);
    await waitFor(() => expect(h.fetchBindings).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('dalton-weixin-bind-card')).not.toBeInTheDocument();
    unmountBothOff();

    // Only allow_external off -> hidden for external
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        userTier: 'external',
        weixinAccount: {
          status: 'paused',
          channel_enabled: true,
          allow_external: false,
          member_allowed: false,
          deny_reason: 'external_access_disabled',
        },
      }),
    );
    const { unmount: unmountExtOff } = renderWithClient(<DaltonWeixinBindCard />);
    await waitFor(() => expect(h.fetchBindings).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId('dalton-weixin-bind-card')).not.toBeInTheDocument();
    unmountExtOff();

    // External member with allow_external=true sees card
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        userTier: 'external',
        weixinAccount: {
          status: 'unconfigured',
          channel_enabled: true,
          allow_external: true,
          member_allowed: true,
          deny_reason: null,
        },
      }),
    );
    const { unmount: unmountExtOn } = renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-card')).toBeInTheDocument();
    unmountExtOn();

    // Internal member stays visible even when both switches are off (sees paused state)
    h.callerTier = 'internal';
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        userTier: 'internal',
        weixinAccount: {
          status: 'paused',
          channel_enabled: false,
          allow_external: false,
          member_allowed: false,
          deny_reason: 'weixin_channel_disabled',
        },
      }),
    );
    renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bind-status-badge')).toHaveTextContent(
      'dalton.weixinBind.statusPaused',
    );
  });

  it('renders nothing for both cards when channel_weixin capability is disabled or caller tier is unresolved', () => {
    h.callerTier = 'admin';
    h.capabilities = new Set<CapabilityId>([]);
    const { container: c1, unmount: u1 } = renderWithClient(<DaltonWeixinBindCard />);
    const { container: c2, unmount: u2 } = renderWithClient(<DaltonWeixinBotCard />);
    expect(c1.firstChild).toBeNull();
    expect(c2.firstChild).toBeNull();
    expect(h.fetchBindings).not.toHaveBeenCalled();
    expect(h.fetchAdminOverview).not.toHaveBeenCalled();
    u1();
    u2();

    // Unresolved caller tier (identity loading/error and bindings not yet resolved) hides bind card
    h.capabilities = new Set<CapabilityId>(['channel_gateway', 'channel_weixin']);
    h.callerTier = null;
    h.fetchBindings.mockImplementationOnce(() => new Promise(() => {}));
    const { container: c3 } = renderWithClient(<DaltonWeixinBindCard />);
    expect(c3.firstChild).toBeNull();
  });

  it('admin card returns null with zero admin requests for non-admins, and shows accurate stats, switches, localized stages, audit actors, and preset-reason unbind/revoke for admin', async () => {
    // Non-admin (internal & external): component itself returns null and fires zero overview requests
    h.callerTier = 'internal';
    const { container: intContainer, unmount: unmountInt } = renderWithClient(<DaltonWeixinBotCard />);
    expect(intContainer.firstChild).toBeNull();
    expect(screen.queryByTestId('dalton-weixin-bot-card')).not.toBeInTheDocument();
    expect(h.fetchAdminOverview).not.toHaveBeenCalled();
    unmountInt();

    h.callerTier = 'external';
    const { container: extContainer, unmount: unmountExt } = renderWithClient(<DaltonWeixinBotCard />);
    expect(extContainer.firstChild).toBeNull();
    expect(h.fetchAdminOverview).not.toHaveBeenCalled();
    unmountExt();

    // Admin view
    h.callerTier = 'admin';
    h.fetchAdminOverview.mockResolvedValue(makeAdminOverview());
    h.updateAdminConfig.mockResolvedValue({
      ok: true,
      enabled: true,
      allow_external: false,
      accounts_revision: 6,
      disabled_count: 1,
      restored_count: 0,
    });
    h.adminUnbindAccount.mockResolvedValueOnce({
      ok: true,
      platform: 'weixin',
      account_id: 'acc-alice',
      deleted_account: true,
    });
    h.adminRevokeUser.mockResolvedValueOnce({
      ok: true,
      platform: 'weixin',
      user_id: 'u-bob',
      superseded_login_sessions: 1,
    });

    renderWithClient(<DaltonWeixinBotCard />);

    // Top link to personal WeChat card
    const ownLink = await screen.findByTestId('dalton-weixin-manage-own-link');
    expect(ownLink).toHaveAttribute('href', '/settings?tab=integrations#dalton-weixin-bind');

    // Stats bar: 2 verified / 3 connected total (2 active + 1 pending), 1 / 1 attention, 3 / 200 slots
    expect(screen.getByTestId('dalton-weixin-stat-connected')).toHaveTextContent('2 / 3');
    expect(screen.getByTestId('dalton-weixin-stat-attention')).toHaveTextContent('1 / 1');
    expect(screen.getByTestId('dalton-weixin-stat-slots')).toHaveTextContent('3 / 200');
    expect(screen.getByTestId('dalton-weixin-gateway-lease-badge')).toHaveTextContent(
      'dalton.weixinBot.gatewayLeaseActive',
    );

    // Member rows: Alice (nickname + masked ID) & Bob (pending login with localized stage label)
    const aliceRow = screen.getByTestId('dalton-weixin-member-row-u-alice');
    expect(within(aliceRow).getByText('AliceWX')).toBeInTheDocument();
    expect(within(aliceRow).getByText('****a1b2')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-member-status-u-alice')).toHaveTextContent(
      'dalton.weixinBot.memberStatusActive',
    );
    expect(screen.getByTestId('dalton-weixin-member-status-u-bob')).toHaveTextContent(
      'dalton.weixinBot.memberStatusLoginPending',
    );
    // Audit log includes actor/target line
    expect(screen.getByTestId('dalton-weixin-audit-logs')).toHaveTextContent(
      'dalton.weixinBot.auditActorTarget',
    );

    // Toggle channel switch & external member switch
    fireEvent.click(
      screen.getByRole('switch', { name: 'dalton.weixinBot.channelSwitchLabel' }),
    );
    await waitFor(() =>
      expect(h.updateAdminConfig).toHaveBeenCalledWith({ enabled: false }),
    );

    fireEvent.click(
      screen.getByRole('switch', { name: 'dalton.weixinBot.externalSwitchLabel' }),
    );
    await waitFor(() =>
      expect(h.updateAdminConfig).toHaveBeenCalledWith({ allow_external: false }),
    );

    // Admin unbind bound member (Alice) requires preset reason
    fireEvent.click(screen.getByTestId('dalton-weixin-admin-unbind-u-alice'));
    const confirmBtn1 = await screen.findByTestId('dalton-weixin-admin-unbind-confirm');
    expect(confirmBtn1).toBeDisabled();
    fireEvent.change(screen.getByTestId('dalton-weixin-admin-unbind-reason'), {
      target: { value: '离职交接' },
    });
    expect(confirmBtn1).not.toBeDisabled();
    fireEvent.click(confirmBtn1);
    await waitFor(() =>
      expect(h.adminUnbindAccount).toHaveBeenCalledWith('acc-alice', '离职交接'),
    );

    // Admin revoke pending-scan member (Bob, account_id === null) calls user-level revoke endpoint
    fireEvent.click(screen.getByTestId('dalton-weixin-admin-unbind-u-bob'));
    const confirmBtn2 = await screen.findByTestId('dalton-weixin-admin-unbind-confirm');
    expect(confirmBtn2).toBeDisabled();
    fireEvent.change(screen.getByTestId('dalton-weixin-admin-unbind-reason'), {
      target: { value: '手机遗失应急' },
    });
    fireEvent.click(confirmBtn2);
    await waitFor(() =>
      expect(h.adminRevokeUser).toHaveBeenCalledWith('u-bob', '手机遗失应急'),
    );
  });
});
