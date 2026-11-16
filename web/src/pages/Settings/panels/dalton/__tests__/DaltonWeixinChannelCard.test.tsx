import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { DaltonWeixinBindCard } from '../DaltonWeixinBindCard';
import { DaltonWeixinBotCard } from '../DaltonWeixinBotCard';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingsResponse,
  DaltonChannelUnbindResponse,
  DaltonWeixinQrPublicResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  callerTier: 'internal' as string | null,
  fetchBindings: vi.fn<() => Promise<DaltonChannelBindingsResponse>>(),
  createCode: vi.fn<(platform: string) => Promise<DaltonChannelBindCodeCreateResponse>>(),
  unbind: vi.fn<(platform: string) => Promise<DaltonChannelUnbindResponse>>(),
  startQr: vi.fn<() => Promise<DaltonWeixinQrPublicResponse>>(),
  waitQr: vi.fn<(sessionKey: string) => Promise<DaltonWeixinQrPublicResponse>>(),
  verifyQr: vi.fn<
    (sessionKey: string, code: string) => Promise<DaltonWeixinQrPublicResponse>
  >(),
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    callerTier: h.callerTier,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

vi.mock('@/lib/clipboard', () => ({
  copyText: (text: string) => h.copyText(text),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: () => h.fetchBindings(),
  createDaltonChannelBindCode: (platform: string) => h.createCode(platform),
  unbindDaltonChannel: (platform: string) => h.unbind(platform),
  startDaltonWeixinQrLogin: () => h.startQr(),
  waitDaltonWeixinQrLogin: (sessionKey: string) => h.waitQr(sessionKey),
  verifyDaltonWeixinQrCode: (sessionKey: string, code: string) =>
    h.verifyQr(sessionKey, code),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeBindings(options: {
  linked?: boolean;
  botConnected?: boolean;
  botState?: DaltonChannelBindingsResponse['weixin_bot'] | 'omit';
} = {}): DaltonChannelBindingsResponse {
  const weixin_bot =
    options.botState === 'omit'
      ? null
      : options.botState !== undefined
        ? options.botState
        : options.botConnected
          ? {
              configured: true,
              status: 'active',
              ilink_bot_id: 'bot-1',
              ilink_user_id: 'u-bot',
              ilink_username: 'TeamBot',
            }
          : {
              configured: false,
              status: 'unconfigured',
              ilink_bot_id: null,
              ilink_user_id: null,
            };
  return {
    user_id: 'u-1',
    user_tier: h.callerTier ?? 'internal',
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
        linked: options.linked ?? false,
        platform: 'weixin',
        platform_user_id: options.linked ? 'wx_123' : null,
        platform_username: options.linked ? 'Alice WX' : null,
        updated_at: options.linked ? '2026-11-15T10:00:00Z' : null,
      },
    },
    identities: {},
    settings: { version: '1', apps: {} },
    chat_bindings: [],
    weixin_bot,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('DaltonWeixinBindCard (personal) & DaltonWeixinBotCard (admin)', () => {
  beforeEach(() => {
    h.callerTier = 'internal';
    h.fetchBindings.mockReset();
    h.createCode.mockReset();
    h.unbind.mockReset();
    h.startQr.mockReset();
    h.waitQr.mockReset();
    h.verifyQr.mockReset();
    h.copyText.mockReset().mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('personal bind card generates command without showing admin QR login controls, and requires ConfirmDialog on unbind', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings({ linked: true, botConnected: false }));
    h.createCode.mockResolvedValueOnce({
      code: 'WX111',
      platform: 'weixin',
      expires_at: '2026-11-16T12:00:00Z',
      expires_in_seconds: 300,
      command: '/bind WX111',
    });
    h.unbind.mockResolvedValueOnce({ unbound: true, platform: 'weixin' });

    renderWithClient(<DaltonWeixinBindCard />);

    expect(await screen.findByTestId('dalton-weixin-bot-not-ready')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-bot-start-qr')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dalton-weixin-generate-code'));
    expect(await screen.findByText('/bind WX111')).toBeInTheDocument();
    expect(h.createCode).toHaveBeenCalledWith('weixin');

    fireEvent.click(screen.getByTestId('dalton-weixin-copy-command'));
    await waitFor(() => expect(h.copyText).toHaveBeenCalledWith('/bind WX111'));

    // Unbind requires confirmation dialog and passes 'weixin'
    fireEvent.click(screen.getByTestId('dalton-weixin-unbind'));
    expect(h.unbind).not.toHaveBeenCalled();

    const btns = await screen.findAllByRole('button', { name: 'dalton.weixinBind.unbind' });
    fireEvent.click(btns[btns.length - 1]);
    await waitFor(() => expect(h.unbind).toHaveBeenCalledWith('weixin'));
  });

  it('distinguishes unknown bot status (null) from unconfigured and expired bot states', async () => {
    h.fetchBindings.mockResolvedValueOnce(makeBindings({ botState: 'omit' }));
    const { unmount } = renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bot-unknown')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-bot-not-ready')).not.toBeInTheDocument();
    unmount();

    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        botState: {
          configured: true,
          status: 'expired',
          ilink_bot_id: 'bot-1',
          ilink_user_id: 'u-bot',
        },
      }),
    );
    renderWithClient(<DaltonWeixinBindCard />);
    expect(await screen.findByTestId('dalton-weixin-bot-expired')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-bot-not-ready')).not.toBeInTheDocument();
  });

  it('admin WeChat bot card hides QR controls for non-admin and polls scan -> confirmed for admin', async () => {
    h.callerTier = 'internal';
    h.fetchBindings.mockResolvedValue(makeBindings({ botConnected: false }));
    const { unmount } = renderWithClient(<DaltonWeixinBotCard />);
    expect(await screen.findByText('dalton.weixinBot.adminOnlyNotice')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-bot-start-qr')).not.toBeInTheDocument();
    unmount();

    vi.useFakeTimers();
    h.callerTier = 'admin';
    h.fetchBindings.mockResolvedValue(makeBindings({ botConnected: true }));
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-1',
      status: 'wait',
      qrcode_url: 'data:image/png;base64,AAA',
    });
    h.waitQr
      .mockResolvedValueOnce({ session_key: 'sess-1', status: 'scaned' })
      .mockResolvedValueOnce({ session_key: 'sess-1', status: 'confirmed', connected: true });

    renderWithClient(<DaltonWeixinBotCard />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    fireEvent.click(screen.getByTestId('dalton-weixin-bot-start-qr'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(screen.getByText('dalton.weixinBot.connectedSuccess')).toBeInTheDocument();
  });

  it.each([401, 403, 404, 409])('stops QR polling immediately on HTTP %s', async (status) => {
    h.callerTier = 'admin';
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-err',
      status: 'wait',
      qrcode_url: 'data:image/png;base64,AAA',
    });
    h.waitQr.mockRejectedValueOnce(
      new ApiError('Error', 'ERR_BAD_REQUEST', { status, statusText: 'Error', headers: {}, data: {} }),
    );

    renderWithClient(<DaltonWeixinBotCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-bot-start-qr'));

    expect(await screen.findByRole('alert')).toHaveTextContent('dalton.weixinBot.loginFailed');
    expect(h.waitQr).toHaveBeenCalledTimes(1);
  });

  it('treats expired and errored team bot configs accurately instead of connected, and cancels QR polling on unmount', async () => {
    h.callerTier = 'admin';
    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        botState: { configured: true, status: 'expired', ilink_bot_id: 'bot-1', ilink_user_id: 'u-bot' },
      }),
    );
    const { unmount: unmountExpired } = renderWithClient(<DaltonWeixinBotCard />);
    expect(await screen.findByTestId('dalton-weixin-bot-status-badge')).toHaveTextContent(
      'dalton.weixinBot.expiredBadge',
    );
    expect(screen.getByTestId('dalton-weixin-bot-expired-notice')).toBeInTheDocument();
    unmountExpired();

    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({
        botState: { configured: true, status: 'error', ilink_bot_id: 'bot-1', ilink_user_id: 'u-bot' },
      }),
    );
    const { unmount: unmountError } = renderWithClient(<DaltonWeixinBotCard />);
    expect(await screen.findByTestId('dalton-weixin-bot-status-badge')).toHaveTextContent(
      'dalton.weixinBot.errorBadge',
    );
    expect(screen.getByTestId('dalton-weixin-bot-error-notice')).toBeInTheDocument();
    unmountError();

    // Unmount before startDaltonWeixinQrLogin resolves must never start pollQr
    h.fetchBindings.mockResolvedValueOnce(makeBindings());
    let resolveStart!: (resp: DaltonWeixinQrPublicResponse) => void;
    h.startQr.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve;
        }),
    );
    const { unmount: unmountInFlight } = renderWithClient(<DaltonWeixinBotCard />);
    fireEvent.click(await screen.findByTestId('dalton-weixin-bot-start-qr'));
    await waitFor(() => expect(h.startQr).toHaveBeenCalledTimes(1));
    unmountInFlight();
    await act(async () => {
      resolveStart({ session_key: 'late-sess', status: 'wait' });
      await Promise.resolve();
    });
    expect(h.waitQr).not.toHaveBeenCalled();
  });
});
