import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import zhSettings from '@/locales/settings/zh-CN.json';
import {
  CHANNEL_BINDINGS_MAX_WAIT_MS,
  CHANNEL_BINDINGS_POLL_MS,
  DaltonChannelBindingsCard,
  issuedBindCode,
} from '../DaltonChannelBindingsCard';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingsResponse,
  DaltonChannelBindingState,
  DaltonChannelPlatform,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  fetchBindings: vi.fn<() => Promise<DaltonChannelBindingsResponse>>(),
  createCode: vi.fn<
    (platform: DaltonChannelPlatform) => Promise<DaltonChannelBindCodeCreateResponse>
  >(),
  copyText: vi.fn<(text: string) => Promise<void>>(),
  locale: 'en-US',
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: () => h.fetchBindings(),
  createDaltonChannelBindCode: (platform: DaltonChannelPlatform) => h.createCode(platform),
}));

vi.mock('@/lib/clipboard', () => ({
  copyText: (text: string) => h.copyText(text),
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => h.locale,
}));

function makeState(
  platform: DaltonChannelPlatform,
  linked = false,
  username: string | null = null,
  uid: string | null = null,
): DaltonChannelBindingState {
  return {
    linked,
    platform,
    platform_user_id: uid,
    platform_username: username,
    updated_at: null,
  };
}

function makeBindings(options: {
  tier?: string;
  feishu?: DaltonChannelBindingState;
  discord?: DaltonChannelBindingState;
} = {}): DaltonChannelBindingsResponse {
  return {
    user_id: 'u-1',
    user_tier: options.tier ?? 'internal',
    home_workspace_id: 'home-ws',
    bindings: {
      feishu: options.feishu ?? makeState('feishu', false),
      discord: options.discord ?? makeState('discord', false),
      weixin: makeState('weixin', false),
    },
    identities: {
      feishu: {
        platform: 'feishu',
        platform_user_id: 'SHOULD-NOT-LEAK',
        platform_username: 'Secret Identity Name',
        platform_tenant_key: null,
        created_at: null,
        updated_at: null,
      },
    },
    settings: { version: '1', apps: {} },
    chat_bindings: [{ secret: 'CHAT-SECRET' }],
    weixin_bot: null,
  };
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonChannelBindingsCard />
    </QueryClientProvider>,
  );
}

describe('issuedBindCode', () => {
  it('caps far-future expiry to max wait', () => {
    const now = 1_700_000_000_000;
    const res = issuedBindCode(
      'feishu',
      {
        code: 'C1',
        platform: 'feishu',
        expires_at: new Date(now + 60 * 60 * 1000).toISOString(),
        expires_in_seconds: 3600,
        command: '/bind C1',
      },
      now,
    );
    expect(res.command).toBe('/bind C1');
    expect(res.expiresAtMs).toBe(now + CHANNEL_BINDINGS_MAX_WAIT_MS);
  });
});

describe('DaltonChannelBindingsCard', () => {
  beforeEach(() => {
    h.locale = 'en-US';
    h.fetchBindings.mockReset();
    h.createCode.mockReset();
    h.copyText.mockReset().mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows loading first, renders unavailable state without generate button on malformed row, and never leaks identities/chat_bindings', async () => {
    let resolveLoad!: (data: DaltonChannelBindingsResponse) => void;
    h.fetchBindings.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveLoad = resolve;
        }),
    );

    const { container } = renderCard();
    expect(await screen.findByText(/Reading your Feishu and Discord|Loading your Feishu and Discord/)).toBeInTheDocument();

    const broken = makeBindings();
    Object.assign(broken.bindings.feishu, { linked: 'bad' });
    await act(async () => {
      resolveLoad(broken);
    });

    expect(await screen.findByText(/Status unavailable/)).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-channel-generate-feishu')).not.toBeInTheDocument();
    expect(screen.getByTestId('dalton-channel-generate-discord')).toBeInTheDocument();
    expect(container.textContent).not.toContain('SHOULD-NOT-LEAK');
    expect(container.textContent).not.toContain('CHAT-SECRET');
  });

  it('prevents duplicate requests per platform while in flight and rejects mismatched platform payload', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    let resolveFeishu!: (res: DaltonChannelBindCodeCreateResponse) => void;
    h.createCode.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFeishu = resolve;
        }),
    );

    renderCard();
    const feishuBtn = await screen.findByTestId('dalton-channel-generate-feishu');
    fireEvent.click(feishuBtn);
    fireEvent.click(feishuBtn);
    expect(h.createCode).toHaveBeenCalledTimes(1);

    // Mismatched platform payload must not render command
    await act(async () => {
      resolveFeishu({
        code: 'WRONG',
        platform: 'discord',
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        expires_in_seconds: 60,
        command: '/bind WRONG',
      });
    });
    expect(screen.queryByText('/bind WRONG')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('copies the generated command and surfaces copy failure safely', async () => {
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.createCode.mockResolvedValueOnce({
      code: 'ABC123',
      platform: 'feishu',
      expires_at: new Date(Date.now() + 120_000).toISOString(),
      expires_in_seconds: 120,
      command: '/bind ABC123',
    });

    renderCard();
    fireEvent.click(await screen.findByTestId('dalton-channel-generate-feishu'));
    expect(await screen.findByText('/bind ABC123')).toBeInTheDocument();

    h.copyText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByTestId('dalton-channel-copy-feishu'));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not copy/);

    h.copyText.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByTestId('dalton-channel-copy-feishu'));
    await waitFor(() => expect(h.copyText).toHaveBeenCalledWith('/bind ABC123'));
  });

  it('refetches on window focus and clears issued command once linked', async () => {
    h.fetchBindings
      .mockResolvedValueOnce(makeBindings())
      .mockResolvedValueOnce(makeBindings());
    h.createCode.mockResolvedValueOnce({
      code: 'C999',
      platform: 'feishu',
      expires_at: new Date(Date.now() + 120_000).toISOString(),
      expires_in_seconds: 120,
      command: '/bind C999',
    });

    renderCard();
    fireEvent.click(await screen.findByTestId('dalton-channel-generate-feishu'));
    expect(await screen.findByText('/bind C999')).toBeInTheDocument();

    h.fetchBindings.mockResolvedValueOnce(
      makeBindings({ feishu: makeState('feishu', true, 'Ada', 'ou_ada') }),
    );
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });
    expect(await screen.findByText(/Ada/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('/bind C999')).not.toBeInTheDocument());
  });

  it('does not start a rapid interval loop when no bind code is active', async () => {
    vi.useFakeTimers();
    const setIntervalSpy = vi.spyOn(window, 'setInterval');
    h.fetchBindings.mockResolvedValue(makeBindings());

    renderCard();
    await act(async () => {
      await Promise.resolve();
    });

    const zeroOrTinyIntervals = setIntervalSpy.mock.calls.filter(
      ([, ms]) => typeof ms === 'number' && ms <= 0,
    );
    expect(zeroOrTinyIntervals).toHaveLength(0);
    setIntervalSpy.mockRestore();
  });

  it('polls on CHANNEL_BINDINGS_POLL_MS only while an unlinked code is active', async () => {
    vi.useFakeTimers();
    h.fetchBindings.mockResolvedValue(makeBindings());
    h.createCode.mockResolvedValueOnce({
      code: 'POLL1',
      platform: 'feishu',
      expires_at: new Date(Date.now() + 120_000).toISOString(),
      expires_in_seconds: 120,
      command: '/bind POLL1',
    });

    renderCard();
    await act(async () => {
      await Promise.resolve();
    });
    expect(h.fetchBindings).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(CHANNEL_BINDINGS_POLL_MS * 2);
    });
    expect(h.fetchBindings).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('dalton-channel-generate-feishu'));
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      vi.advanceTimersByTime(CHANNEL_BINDINGS_POLL_MS);
    });
    expect(h.fetchBindings.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('shows forbidden error when 403 is returned', async () => {
    h.fetchBindings.mockRejectedValueOnce(
      new ApiError('Forbidden', 'ERR_BAD_REQUEST', { status: 403, statusText: 'Forbidden', headers: {}, data: {} }),
    );
    renderCard();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /cannot view Feishu and Discord links/,
    );
  });

  it('places channel bindings & personal WeChat on IntegrationsPanel and removes DaltonSection from ModelsPanel', () => {
    const integrations = readFileSync(
      resolve(__dirname, '../../IntegrationsPanel.tsx'),
      'utf8',
    );
    const models = readFileSync(resolve(__dirname, '../../ModelsPanel.tsx'), 'utf8');
    expect(integrations).toContain('DaltonChannelBindingsCard');
    expect(integrations).toContain('DaltonWeixinBindCard');
    expect(integrations).toContain('DaltonNotifyTargetsCard');
    expect(models).not.toContain('DaltonSection');
    expect(zhSettings.dalton.channelBindingsLabel).toBe('飞书与 Discord 账号绑定');
  });
});
