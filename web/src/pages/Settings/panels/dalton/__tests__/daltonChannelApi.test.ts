import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApiResponse, RequestConfig } from '@/api/client';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingsResponse,
} from '@/pages/Settings/utils/daltonApi';

type ApiGet = <T = unknown>(url: string, config?: RequestConfig) => Promise<ApiResponse<T>>;
type ApiPost = <T = unknown>(url: string, data?: unknown, config?: RequestConfig) => Promise<ApiResponse<T>>;
type ApiPut = <T = unknown>(url: string, data?: unknown, config?: RequestConfig) => Promise<ApiResponse<T>>;
type ApiDelete = <T = unknown>(url: string, config?: RequestConfig) => Promise<ApiResponse<T>>;

const apiMock = vi.hoisted(() => ({
  get: vi.fn<ApiGet>(),
  post: vi.fn<ApiPost>(),
  put: vi.fn<ApiPut>(),
  delete: vi.fn<ApiDelete>(),
}));

vi.mock('@/api/client', () => ({ api: apiMock }));

import {
  adminRevokeDaltonWeixinUser,
  adminUnbindDaltonWeixinAccount,
  createDaltonChannelBindCode,
  fetchDaltonChannelBindings,
  fetchDaltonWeixinAdminOverview,
  startDaltonWeixinQrLogin,
  updateDaltonWeixinAdminConfig,
  verifyDaltonWeixinQrCode,
  waitDaltonWeixinQrLogin,
} from '@/pages/Settings/utils/daltonApi';

function okResponse<T>(data: T): ApiResponse<T> {
  return { data, status: 200, statusText: 'OK', headers: {} };
}

const created = (platform: 'feishu' | 'discord'): DaltonChannelBindCodeCreateResponse => ({
  code: 'DLT-AB12-CD34',
  platform,
  expires_at: '2026-11-10T12:10:00.000Z',
  expires_in_seconds: 600,
  command: '/bind DLT-AB12-CD34',
});

describe('personal channel & WeChat multi-account API', () => {
  beforeEach(() => {
    apiMock.get.mockReset();
    apiMock.post.mockReset();
    apiMock.put.mockReset();
    apiMock.delete.mockReset();
  });

  it('reads bindings for the signed-in user and posts only Feishu/Discord bind codes', async () => {
    const payload: DaltonChannelBindingsResponse = {
      user_id: 'user-1',
      user_tier: 'external',
      home_workspace_id: 'home-1',
      bindings: {
        feishu: { linked: false, platform: 'feishu', platform_user_id: null, platform_username: null, updated_at: null },
        discord: { linked: false, platform: 'discord', platform_user_id: null, platform_username: null, updated_at: null },
        weixin: { linked: false, platform: 'weixin', platform_user_id: null, platform_username: null, updated_at: null },
      },
      identities: {},
      settings: { version: '1', apps: {} },
      chat_bindings: [],
      weixin_account: {
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
      },
    };
    apiMock.get.mockResolvedValue(okResponse(payload));
    await expect(fetchDaltonChannelBindings()).resolves.toBe(payload);
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/dalton/channels/bindings');

    apiMock.post.mockResolvedValueOnce(okResponse(created('feishu')));
    await expect(createDaltonChannelBindCode('feishu')).resolves.toEqual(created('feishu'));
    apiMock.post.mockResolvedValueOnce(okResponse(created('discord')));
    await expect(createDaltonChannelBindCode('discord')).resolves.toEqual(created('discord'));

    expect(apiMock.post.mock.calls).toEqual([
      ['/api/v1/dalton/channels/bind-codes', { platform: 'feishu' }],
      ['/api/v1/dalton/channels/bind-codes', { platform: 'discord' }],
    ]);
    for (const call of apiMock.post.mock.calls) {
      expect(call).toHaveLength(2);
      expect(call[1]).not.toHaveProperty('user_id');
      expect(call[1]).not.toHaveProperty('instruction');
      expect(call[1]).not.toHaveProperty('ttl_seconds');
    }
  });

  it('forwards AbortSignal on QR start/wait/verify (50s timeout on wait) and sends preset reasons on admin endpoints', async () => {
    const ctrl = new AbortController();

    apiMock.post.mockResolvedValueOnce(okResponse({ session_key: 's-1', status: 'wait' }));
    await startDaltonWeixinQrLogin({ signal: ctrl.signal });
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/v1/dalton/channels/weixin/qrcode/start',
      {},
      { signal: ctrl.signal },
    );

    apiMock.get.mockResolvedValueOnce(okResponse({ session_key: 's-1', status: 'wait' }));
    await waitDaltonWeixinQrLogin('s-1', { signal: ctrl.signal });
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/dalton/channels/weixin/qrcode/wait', {
      params: { session_key: 's-1' },
      timeout: 50_000,
      signal: ctrl.signal,
    });

    apiMock.post.mockResolvedValueOnce(okResponse({ session_key: 's-1', status: 'confirmed' }));
    await verifyDaltonWeixinQrCode('s-1', '654321', { signal: ctrl.signal });
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/v1/dalton/channels/weixin/qrcode/verify-code',
      { session_key: 's-1', verify_code: '654321' },
      { signal: ctrl.signal },
    );

    apiMock.get.mockResolvedValueOnce(okResponse({ enabled: true, accounts: [] }));
    await fetchDaltonWeixinAdminOverview();
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/dalton/channels/weixin/admin/overview');

    apiMock.put.mockResolvedValueOnce(
      okResponse({ ok: true, enabled: true, allow_external: false, accounts_revision: 2, disabled_count: 1, restored_count: 0 }),
    );
    await updateDaltonWeixinAdminConfig({ allow_external: false });
    expect(apiMock.put).toHaveBeenCalledWith('/api/v1/dalton/channels/weixin/admin/config', {
      allow_external: false,
    });

    apiMock.delete.mockResolvedValueOnce(okResponse({ ok: true, platform: 'weixin' }));
    await adminUnbindDaltonWeixinAccount('acc-1', '离职交接');
    expect(apiMock.delete).toHaveBeenCalledWith('/api/v1/dalton/channels/weixin/admin/accounts/acc-1', {
      data: { reason: '离职交接' },
    });

    apiMock.delete.mockResolvedValueOnce(okResponse({ ok: true, platform: 'weixin' }));
    await adminRevokeDaltonWeixinUser('user-2', '误绑微信号');
    expect(apiMock.delete).toHaveBeenCalledWith('/api/v1/dalton/channels/weixin/admin/users/user-2/weixin', {
      data: { reason: '误绑微信号' },
    });
  });
});
