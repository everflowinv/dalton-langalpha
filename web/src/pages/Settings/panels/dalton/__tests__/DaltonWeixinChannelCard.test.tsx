import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
import { ApiError } from '@/api/client';
import { DaltonWeixinChannelCard } from '../DaltonWeixinChannelCard';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingsResponse,
  DaltonChannelUnbindResponse,
  DaltonWeixinQrPublicResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  bindings: {
    user_id: 'user-1',
    user_tier: 'internal',
    identities: { feishu: null, weixin: null },
    active_bind_codes: [],
    weixin_bot: null,
    feishu_bot_configured: true,
  } as DaltonChannelBindingsResponse,
  createBindCode: vi.fn<() => Promise<DaltonChannelBindCodeCreateResponse>>(async () => ({
    code: 'WX123456',
    platform: 'weixin',
    expires_at: '2026-12-01T00:00:00Z',
    ttl_seconds: 600,
    instruction: 'Send `/bind WX123456` to the Weixin bot within 10 minutes.',
  })),
  unbindChannel: vi.fn<() => Promise<DaltonChannelUnbindResponse>>(async () => ({
    unbound: true,
    platform: 'weixin',
  })),
  startQr: vi.fn<() => Promise<DaltonWeixinQrPublicResponse>>(async () => ({
    session_key: 'sess-1',
    status: 'wait',
    qrcode_url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
    expires_at: '2026-12-01T00:05:00Z',
  })),
  waitQr: vi.fn<(sessionKey: string) => Promise<DaltonWeixinQrPublicResponse>>(async (sessionKey) => ({
    session_key: sessionKey,
    status: 'confirmed',
    connected: true,
    ilink_bot_id: 'bot-wx-1',
    ilink_user_id: 'wx-admin-1',
    bot_generation: 2,
  })),
  verifyQr: vi.fn<(sessionKey: string, code: string) => Promise<DaltonWeixinQrPublicResponse>>(
    async (sessionKey) => ({
      session_key: sessionKey,
      status: 'confirmed',
      connected: true,
      ilink_bot_id: 'bot-wx-1',
      bot_generation: 2,
    }),
  ),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: async (): Promise<DaltonChannelBindingsResponse> => h.bindings,
  createDaltonChannelBindCode: () => h.createBindCode(),
  unbindDaltonChannel: () => h.unbindChannel(),
  startDaltonWeixinQrLogin: () => h.startQr(),
  waitDaltonWeixinQrLogin: (sessionKey: string) => h.waitQr(sessionKey),
  verifyDaltonWeixinQrCode: (sessionKey: string, code: string) => h.verifyQr(sessionKey, code),
}));

const QR_IMAGE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

function adminBindings(bot: DaltonChannelBindingsResponse['weixin_bot']): DaltonChannelBindingsResponse {
  return {
    user_id: 'admin-1',
    user_tier: 'admin',
    identities: {
      feishu: null,
      weixin: {
        platform_user_id: 'wx-admin-1',
        platform_display_name: 'Admin WX',
        bound_at: '2026-11-01T00:00:00Z',
        last_active_at: null,
      },
    },
    active_bind_codes: [],
    weixin_bot: bot,
    feishu_bot_configured: true,
  };
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonWeixinChannelCard />
    </QueryClientProvider>,
  );
}

const QR_LINK = 'https://ilinkai.weixin.qq.com/qr/sess-keep';
const NEW_QR_IMAGE = 'data:image/png;base64,NEWQR';

function apiError(message: string, status?: number): ApiError {
  if (status === undefined) {
    return new ApiError(message, 'ERR_NETWORK');
  }
  return new ApiError(message, status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST', {
    data: {},
    status,
    statusText: message,
    headers: {},
  });
}

function unconfiguredAdmin(): DaltonChannelBindingsResponse {
  return adminBindings({
    configured: false,
    status: 'unconfigured',
    ilink_bot_id: null,
    ilink_user_id: null,
  });
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('DaltonWeixinChannelCard', () => {
  beforeEach(() => {
    vi.useRealTimers();
    h.bindings = {
      user_id: 'user-1',
      user_tier: 'internal',
      identities: { feishu: null, weixin: null },
      active_bind_codes: [],
      weixin_bot: null,
      feishu_bot_configured: true,
    };
    h.createBindCode.mockClear();
    h.unbindChannel.mockClear();
    h.startQr.mockClear();
    h.waitQr.mockClear();
    h.verifyQr.mockClear();
  });

  it('allows non-admin member to generate a bind code without showing admin QR login controls', async () => {
    renderCard();
    expect(await screen.findByTestId('dalton-weixin-channel-card')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-qr-start-btn')).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-bind-code-btn'));
    });

    expect(h.createBindCode).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('/bind WX123456')).toBeInTheDocument();
  });

  it('shows 已连接 only when the bot is configured and active', async () => {
    h.bindings = adminBindings({
      configured: true,
      status: 'active',
      ilink_bot_id: 'bot-wx-1',
      ilink_user_id: 'wx-admin-1',
      bot_generation: 3,
    });

    renderCard();
    expect(await screen.findByText('微信机器人连接状态：已连接')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-start-btn')).toHaveTextContent('重新扫码连接');
  });

  it('polls scaned then confirmed and refreshes the active bot', async () => {
    h.bindings = adminBindings({
      configured: false,
      status: 'unconfigured',
      ilink_bot_id: null,
      ilink_user_id: null,
    });
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-scan',
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    let polls = 0;
    h.waitQr.mockImplementation(async (sessionKey) => {
      polls += 1;
      if (polls === 1) {
        return {
          session_key: sessionKey,
          status: 'scaned',
          connected: false,
        };
      }
      h.bindings = adminBindings({
        configured: true,
        status: 'active',
        ilink_bot_id: 'bot-wx-1',
        ilink_user_id: 'wx-admin-1',
        bot_generation: 2,
      });
      return {
        session_key: sessionKey,
        status: 'confirmed',
        connected: true,
        ilink_bot_id: 'bot-wx-1',
        ilink_user_id: 'wx-admin-1',
        bot_generation: 2,
      };
    });

    renderCard();
    expect(await screen.findByText('微信机器人连接状态：未连接')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
    });
    expect(await screen.findByTestId('dalton-weixin-qr-image')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-status')).toHaveTextContent('请使用个人微信扫描二维码');

    expect(await screen.findByText('已扫码，请在手机微信上确认登录', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(await screen.findByText('微信扫码连接成功', {}, { timeout: 4000 })).toBeInTheDocument();
    expect(h.waitQr).toHaveBeenCalledTimes(2);
    expect(await screen.findByText('微信机器人连接状态：已连接', {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it('allows admin to submit a verify_code and complete confirmation', async () => {
    h.bindings = adminBindings({
      configured: false,
      status: 'unconfigured',
      ilink_bot_id: null,
      ilink_user_id: null,
    });
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-verify',
      status: 'verify_code',
      connected: false,
      message: 'Please enter the verification code shown in WeChat.',
    });

    renderCard();
    expect(await screen.findByTestId('dalton-weixin-qr-start-btn')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
    });

    expect(await screen.findByTestId('dalton-weixin-verify-form')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-status')).toHaveTextContent(
      '手机端要求输入验证码，请在下方输入后提交',
    );
    expect(h.waitQr).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId('dalton-weixin-verify-input'), {
      target: { value: '654321' },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-verify-submit'));
    });

    await waitFor(() => {
      expect(h.verifyQr).toHaveBeenCalledWith('sess-verify', '654321');
    });
    expect(await screen.findByText('微信扫码连接成功')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-weixin-verify-form')).not.toBeInTheDocument();
  });

  it('stops polling when the QR expires and lets the admin start a new scan', async () => {
    h.bindings = adminBindings({
      configured: true,
      status: 'expired',
      ilink_bot_id: 'bot-wx-1',
      ilink_user_id: 'wx-admin-1',
      bot_generation: 1,
    });
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-exp',
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    h.waitQr.mockImplementation(async (sessionKey) => {
      if (sessionKey === 'sess-exp') {
        return { session_key: sessionKey, status: 'expired', connected: false };
      }
      return { session_key: sessionKey, status: 'wait', connected: false, qrcode_url: QR_IMAGE };
    });

    renderCard();
    expect(await screen.findByText('微信机器人连接状态：登录已过期（请重新扫码）')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-weixin-qr-start-btn')).toHaveTextContent('管理员扫码连接');

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
    });
    expect(await screen.findByText('二维码已过期，请重新生成二维码', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(h.waitQr).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dalton-weixin-qr-start-btn')).toHaveTextContent('重新扫码连接');

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
    });
    expect(h.startQr).toHaveBeenCalledTimes(2);
    expect(await screen.findByText('请使用个人微信扫描二维码', {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it('keeps the same session QR when a wait response omits it, and a new session does not inherit it', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-keep',
      status: 'wait',
      qrcode_url: QR_IMAGE,
      qrcode_img_content: QR_IMAGE,
    });
    h.waitQr.mockImplementation(async (sessionKey) => {
      if (sessionKey === 'sess-keep') {
        return { session_key: sessionKey, status: 'scaned', connected: false };
      }
      return { session_key: sessionKey, status: 'wait', connected: false, qrcode_url: QR_LINK };
    });
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', QR_IMAGE);
      // The first poll is scheduled for 800ms. 799ms is still before it.
      await advance(799);
      expect(h.waitQr).not.toHaveBeenCalled();
      await advance(1);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      expect(h.waitQr).toHaveBeenCalledWith('sess-keep');
      expect(screen.getByTestId('dalton-weixin-qr-status')).toHaveTextContent('已扫码，请在手机微信上确认登录');
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', QR_IMAGE);

      h.startQr.mockResolvedValueOnce({
        session_key: 'sess-next',
        status: 'wait',
        qrcode_url: QR_LINK,
      });
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(1);
      expect(screen.queryByTestId('dalton-weixin-qr-image')).not.toBeInTheDocument();
      expect(screen.getByTestId('dalton-weixin-qr-link')).toHaveAttribute('href', QR_LINK);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('stops showing an expired QR and does not take a late result from the replaced session', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    let releaseOld: (value: DaltonWeixinQrPublicResponse) => void = () => {};
    h.startQr
      .mockResolvedValueOnce({
        session_key: 'sess-old',
        status: 'wait',
        qrcode_url: QR_IMAGE,
      })
      .mockResolvedValueOnce({
        session_key: 'sess-new',
        status: 'wait',
        qrcode_url: NEW_QR_IMAGE,
      });
    h.waitQr.mockImplementation((sessionKey) => {
      if (sessionKey === 'sess-old') {
        return new Promise<DaltonWeixinQrPublicResponse>((resolve) => {
          releaseOld = resolve;
        });
      }
      return Promise.resolve({
        session_key: sessionKey,
        status: 'expired',
        connected: false,
      });
    });
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      expect(h.waitQr).toHaveBeenCalledWith('sess-old');
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(1);
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', NEW_QR_IMAGE);
      releaseOld({
        session_key: 'sess-old',
        status: 'scaned',
        connected: false,
        qrcode_url: QR_IMAGE,
      });
      await advance(1);
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', NEW_QR_IMAGE);
      await advance(800);
      expect(h.waitQr).toHaveBeenCalledWith('sess-new');
      expect(screen.queryByTestId('dalton-weixin-qr-image')).not.toBeInTheDocument();
      expect(screen.getByTestId('dalton-weixin-qr-status')).toHaveTextContent('二维码已过期，请重新生成二维码');
      await advance(20_000);
      expect(h.waitQr).toHaveBeenCalledTimes(2);
      expect(h.startQr).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('retries a temporary poll failure on a bounded schedule and then accepts the same session', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-retry',
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    let waits = 0;
    h.waitQr.mockImplementation(async (sessionKey) => {
      waits += 1;
      if (waits === 1) throw apiError('network down');
      if (waits === 2) throw apiError('gateway down', 500);
      return { session_key: sessionKey, status: 'wait', connected: false };
    });
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', QR_IMAGE);
      expect(screen.getByTestId('dalton-weixin-error')).toHaveTextContent('network down');
      await advance(399);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      await advance(1);
      expect(h.waitQr).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('dalton-weixin-error')).toHaveTextContent('gateway down');
      await advance(799);
      expect(h.waitQr).toHaveBeenCalledTimes(2);
      await advance(1);
      expect(h.waitQr).toHaveBeenCalledTimes(3);
      expect(screen.queryByTestId('dalton-weixin-error')).not.toBeInTheDocument();
      expect(screen.getByTestId('dalton-weixin-qr-image')).toHaveAttribute('src', QR_IMAGE);
      expect(h.startQr).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it.each([401, 403, 404, 409])('does not retry QR polling after HTTP %s', async (status) => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: `sess-${status}`,
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    h.waitQr.mockRejectedValue(apiError(`status ${status}`, status));
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('dalton-weixin-qr-image')).toBeInTheDocument();
      expect(screen.getByTestId('dalton-weixin-error')).toHaveTextContent(`status ${status}`);
      await advance(20_000);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      expect(h.startQr).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('stops after the fifth retry delay is exhausted', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-give-up',
      status: 'wait',
      qrcode_url: QR_LINK,
    });
    h.waitQr.mockRejectedValue(apiError('still down', 429));
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      const delays = [400, 800, 1600, 3200, 6400];
      for (let i = 0; i < delays.length; i += 1) {
        expect(h.waitQr).toHaveBeenCalledTimes(i + 1);
        await advance(delays[i]! - 1);
        expect(h.waitQr).toHaveBeenCalledTimes(i + 1);
        await advance(1);
        expect(h.waitQr).toHaveBeenCalledTimes(i + 2);
      }
      expect(h.waitQr).toHaveBeenCalledTimes(6);
      expect(screen.getByTestId('dalton-weixin-qr-link')).toHaveAttribute('href', QR_LINK);
      await advance(20_000);
      expect(h.waitQr).toHaveBeenCalledTimes(6);
      expect(h.startQr).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('does not poll again after unmount', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-unmount',
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    h.waitQr.mockRejectedValue(apiError('network down'));
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      view.unmount();
      await advance(20_000);
      expect(h.waitQr).toHaveBeenCalledTimes(1);
      expect(h.startQr).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('recovers a confirmed login on the same session after a lost poll reply', async () => {
    vi.useFakeTimers();
    h.bindings = unconfiguredAdmin();
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-confirm',
      status: 'wait',
      qrcode_url: QR_IMAGE,
    });
    let waits = 0;
    h.waitQr.mockImplementation(async (sessionKey) => {
      waits += 1;
      if (waits === 1) throw apiError('response lost');
      h.bindings = adminBindings({
        configured: true,
        status: 'active',
        ilink_bot_id: 'bot-wx-1',
        ilink_user_id: 'wx-admin-1',
        bot_generation: 2,
      });
      return {
        session_key: sessionKey,
        status: 'confirmed',
        connected: true,
        ilink_bot_id: 'bot-wx-1',
        ilink_user_id: 'wx-admin-1',
        bot_generation: 2,
      };
    });
    const view = renderCard();
    try {
      await advance(1);
      await act(async () => {
        fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
      });
      await advance(800);
      expect(screen.getByTestId('dalton-weixin-error')).toHaveTextContent('response lost');
      await advance(400);
      expect(screen.getByTestId('dalton-weixin-qr-status')).toHaveTextContent('微信扫码连接成功');
      expect(screen.queryByTestId('dalton-weixin-qr-image')).not.toBeInTheDocument();
      expect(h.waitQr).toHaveBeenCalledTimes(2);
      expect(h.startQr).toHaveBeenCalledTimes(1);
      await advance(5_000);
      expect(h.waitQr).toHaveBeenCalledTimes(2);
      expect(h.startQr).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
