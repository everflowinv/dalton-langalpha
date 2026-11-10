import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
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

describe('DaltonWeixinChannelCard', () => {
  beforeEach(() => {
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
});
