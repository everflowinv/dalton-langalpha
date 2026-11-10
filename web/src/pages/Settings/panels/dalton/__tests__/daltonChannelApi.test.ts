import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingsResponse,
} from '@/pages/Settings/utils/daltonApi';

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('@/api/client', () => ({ api: apiMock }));

import { createDaltonChannelBindCode, fetchDaltonChannelBindings } from '@/pages/Settings/utils/daltonApi';

const created = (platform: 'feishu' | 'discord'): DaltonChannelBindCodeCreateResponse => ({
  code: 'DLT-AB12-CD34',
  platform,
  expires_at: '2026-11-10T12:10:00.000Z',
  expires_in_seconds: 600,
  command: '/bind DLT-AB12-CD34',
});

describe('personal channel bind API', () => {
  beforeEach(() => {
    apiMock.get.mockReset();
    apiMock.post.mockReset();
    apiMock.delete.mockReset();
  });

  it('reads bindings for the signed-in user and posts only that platform', async () => {
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
      weixin_bot: null,
    };
    apiMock.get.mockResolvedValue({ data: payload });
    await expect(fetchDaltonChannelBindings()).resolves.toBe(payload);
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/dalton/channels/bindings');

    apiMock.post.mockResolvedValueOnce({ data: created('feishu') });
    await expect(createDaltonChannelBindCode('feishu')).resolves.toEqual(created('feishu'));
    apiMock.post.mockResolvedValueOnce({ data: created('discord') });
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
});
