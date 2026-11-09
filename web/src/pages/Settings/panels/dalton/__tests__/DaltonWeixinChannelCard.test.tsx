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
      verify_code_accepted: true,
      ilink_bot_id: 'bot-wx-1',
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

  it('allows admin to start QR login, submit verification code, and complete confirmation', async () => {
    h.bindings = {
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
      weixin_bot: {
        connected: false,
        status: 'unconfigured',
        ilink_bot_id: null,
        ilink_user_id: null,
        base_url: null,
      },
      feishu_bot_configured: true,
    };
    h.startQr.mockResolvedValueOnce({
      session_key: 'sess-verify',
      status: 'need_verifycode',
      verify_code_accepted: false,
      message: 'Please enter verification code',
    });

    renderCard();
    expect(await screen.findByTestId('dalton-weixin-qr-start-btn')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('dalton-weixin-qr-start-btn'));
    });

    expect(await screen.findByTestId('dalton-weixin-verify-form')).toBeInTheDocument();
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
  });
});
