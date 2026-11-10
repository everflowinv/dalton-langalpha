import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import { ApiError, type ApiResponse } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import {
  CHANNEL_BINDINGS_MAX_WAIT_MS,
  CHANNEL_BINDINGS_POLL_MS,
  DaltonChannelBindingsCard,
  issuedBindCode,
} from '../DaltonChannelBindingsCard';
import { DaltonSection } from '../DaltonSection';
import type {
  DaltonChannelBindCodeCreateResponse,
  DaltonChannelBindingState,
  DaltonChannelBindingsResponse,
  DaltonChannelPlatform,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  fetchBindings: vi.fn<() => Promise<DaltonChannelBindingsResponse>>(),
  createCode: vi.fn<(platform: DaltonChannelPlatform) => Promise<DaltonChannelBindCodeCreateResponse>>(),
  copyText: vi.fn<(text: string) => Promise<void>>(async () => undefined),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: () => h.fetchBindings(),
  createDaltonChannelBindCode: (platform: DaltonChannelPlatform) => h.createCode(platform),
}));

vi.mock('@/lib/clipboard', () => ({
  copyText: (text: string) => h.copyText(text),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isSuccess: true }),
}));

vi.mock('../DaltonCoverageCard', () => ({ DaltonCoverageCard: () => null }));
vi.mock('../DaltonCosSharingCard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../DaltonCosSharingCard')>();
  return {
    ...actual,
    DaltonCosSharingCard: () => null,
  };
});
vi.mock('../DaltonHomeTeamMemoryCard', async (importOriginal) => ({
  ...await importOriginal<typeof import('../DaltonHomeTeamMemoryCard')>(),
  DaltonHomeTeamMemoryCard: () => null,
}));
vi.mock('../DaltonInvitesCard', async (importOriginal) => ({
  ...await importOriginal<typeof import('../DaltonInvitesCard')>(),
  DaltonInvitesCard: () => null,
}));
vi.mock('../FeishuReadScopesCard', async (importOriginal) => ({
  ...await importOriginal<typeof import('../FeishuReadScopesCard')>(),
  FeishuReadScopesCard: () => null,
}));
vi.mock('../ProviderStatusCard', () => ({ ProviderStatusCard: () => null }));
vi.mock('../DaltonBudgetCard', () => ({ DaltonBudgetCard: () => null }));
vi.mock('../DaltonNotifyTargetsCard', () => ({ DaltonNotifyTargetsCard: () => null }));
vi.mock('../DaltonFeishuGroupsCard', () => ({ DaltonFeishuGroupsCard: () => null }));
vi.mock('../DaltonSystemJobsCard', () => ({ DaltonSystemJobsCard: () => null }));

const SECRETS = {
  tenant: 'tenant-secret-SHOULD-NOT-SHOW',
  token: 'vault-token-SHOULD-NOT-SHOW',
  chat: 'chat-binding-secret-SHOULD-NOT-SHOW',
  home: 'home-workspace-SHOULD-NOT-SHOW',
  user: 'other-user-SHOULD-NOT-SHOW',
  bot: 'weixin-bot-secret-SHOULD-NOT-SHOW',
  identityName: 'identity-name-SHOULD-NOT-SHOW',
};

function state(
  platform: DaltonChannelPlatform,
  linked: boolean,
  name: string | null = null,
  id: string | null = null,
): DaltonChannelBindingState {
  return {
    linked,
    platform,
    platform_user_id: id,
    platform_username: name,
    updated_at: linked ? '2026-11-10T00:00:00Z' : null,
  };
}

function bindings(options: {
  tier?: string;
  feishu?: DaltonChannelBindingState;
  discord?: DaltonChannelBindingState;
} = {}): DaltonChannelBindingsResponse {
  return {
    user_id: SECRETS.user,
    user_tier: options.tier ?? 'external',
    home_workspace_id: SECRETS.home,
    bindings: {
      feishu: options.feishu ?? state('feishu', false),
      discord: options.discord ?? state('discord', false),
      weixin: state('weixin', false),
    },
    identities: {
      feishu: {
        platform: 'feishu',
        platform_user_id: 'identity-id-SHOULD-NOT-SHOW',
        platform_username: SECRETS.identityName,
        platform_tenant_key: SECRETS.tenant,
        created_at: null,
        updated_at: null,
      },
    },
    settings: { version: '1', apps: { feishu: { token: SECRETS.token } } },
    chat_bindings: [{ secret: SECRETS.chat }],
    weixin_bot: {
      configured: true,
      status: 'active',
      ilink_bot_id: SECRETS.bot,
      ilink_user_id: 'wx-secret-user',
    },
  };
}

function created(
  platform: 'feishu' | 'discord',
  code = 'DLT-AB12-CD34',
  expiresAt = new Date(Date.now() + 600_000).toISOString(),
): DaltonChannelBindCodeCreateResponse {
  return {
    code,
    platform,
    expires_at: expiresAt,
    expires_in_seconds: 600,
    command: `/bind ${code}`,
  };
}

function apiError(status: number, detail?: string): ApiError {
  const response: ApiResponse = {
    data: detail ? { detail } : {},
    status,
    statusText: 'Error',
    headers: {},
  };
  const code = status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST';
  return new ApiError(`Request failed with status code ${status}`, code, response);
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <DaltonChannelBindingsCard />
    </QueryClientProvider>,
  );
  return { client, ...view };
}

function expectNoSecrets() {
  const text = document.body.textContent ?? '';
  for (const secret of Object.values(SECRETS)) {
    expect(text).not.toContain(secret);
  }
  expect(text).not.toContain('ttl_seconds');
  expect(text).not.toContain('instruction');
}

async function flushFake() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe('DaltonChannelBindingsCard', () => {
  beforeEach(() => {
    vi.useRealTimers();
    focusManager.setFocused(undefined);
    h.fetchBindings.mockReset();
    h.createCode.mockReset();
    h.copyText.mockReset();
    h.copyText.mockResolvedValue(undefined);
    h.fetchBindings.mockResolvedValue(bindings());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows loading instead of an unlinked state, then an unusable shape without a generate button', async () => {
    let resolveFetch: (value: DaltonChannelBindingsResponse) => void = () => {};
    h.fetchBindings.mockImplementation(() => new Promise<DaltonChannelBindingsResponse>((resolve) => {
      resolveFetch = (value) => resolve(value);
    }));
    renderCard();
    expect(screen.getByText('Loading your Feishu and Discord links…')).toBeInTheDocument();
    expect(screen.queryByText('Not linked')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate/ })).not.toBeInTheDocument();
    expect(h.createCode).not.toHaveBeenCalled();

    const payload = bindings();
    const broken: DaltonChannelBindingsResponse = JSON.parse(JSON.stringify(payload));
    Object.assign(broken.bindings.feishu, { linked: 'no' });
    await act(async () => {
      resolveFetch(broken);
    });
    expect(await screen.findByText('Status unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('channel-bind-feishu')).not.toHaveTextContent('Not linked');
    expect(screen.queryByRole('button', { name: 'Generate Feishu code' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate Discord code' })).toBeInTheDocument();
    expectNoSecrets();
  });

  it('shows a forbidden or network failure without a generate button, and retries the same query', async () => {
    h.fetchBindings.mockRejectedValue(apiError(403, 'forbidden'));
    const { unmount } = renderCard();
    expect(await screen.findByRole('alert')).toHaveTextContent('This account cannot view Feishu and Discord links.');
    expect(screen.queryByText('Not linked')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate/ })).not.toBeInTheDocument();
    unmount();

    h.fetchBindings.mockRejectedValue(new Error('Network Error'));
    renderCard();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load your Feishu and Discord links. Your links were not changed.',
    );
    expect(screen.queryByText('Not linked')).not.toBeInTheDocument();
    h.fetchBindings.mockResolvedValue(bindings());
    fireEvent.click(screen.getByRole('button', { name: 'Try loading Feishu and Discord links again' }));
    expect(await screen.findByRole('button', { name: 'Generate Feishu code' })).toBeInTheDocument();
  });

  it('keeps the last good status when a later refresh fails', async () => {
    h.fetchBindings.mockResolvedValueOnce(bindings()).mockRejectedValueOnce(new Error('Network Error'));
    renderCard();
    expect(await screen.findAllByText('Not linked')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Feishu and Discord accounts' }));
    await waitFor(() => expect(h.fetchBindings).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText('Not linked')).toHaveLength(2);
    expect(screen.queryByText('Could not load your Feishu and Discord links. Your links were not changed.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate Feishu code' })).toBeEnabled();
  });

  it.each(['admin', 'internal', 'external'])('lets a %s user generate their own code', async (tier) => {
    h.fetchBindings.mockResolvedValue(bindings({ tier }));
    renderCard();
    expect(await screen.findByRole('button', { name: 'Generate Feishu code' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Generate Discord code' })).toBeEnabled();
    expectNoSecrets();
  });

  it('posts each platform once while that request is in flight and does not mix the codes', async () => {
    const pending = new Map<string, (value: DaltonChannelBindCodeCreateResponse) => void>();
    h.createCode.mockImplementation((platform) => new Promise<DaltonChannelBindCodeCreateResponse>((resolve) => {
      pending.set(platform, (value) => resolve(value));
    }));
    renderCard();
    const feishu = await screen.findByRole('button', { name: 'Generate Feishu code' });
    const discord = screen.getByRole('button', { name: 'Generate Discord code' });
    expect(feishu).toHaveAttribute('type', 'button');
    await act(async () => {
      fireEvent.click(feishu);
      fireEvent.click(feishu);
    });
    expect(h.createCode).toHaveBeenCalledTimes(1);
    expect(h.createCode).toHaveBeenCalledWith('feishu');
    expect(feishu).toBeDisabled();
    expect(feishu).toHaveTextContent('Generating Feishu code…');
    expect(discord).toBeEnabled();

    await act(async () => {
      fireEvent.click(discord);
    });
    expect(h.createCode).toHaveBeenCalledTimes(2);
    expect(h.createCode).toHaveBeenNthCalledWith(2, 'discord');
    expect(discord).toBeDisabled();

    await act(async () => {
      pending.get('feishu')?.(created('feishu', 'DLT-FE11-SHU1'));
    });
    expect(await screen.findByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-FE11-SHU1');
    expect(screen.queryByTestId('channel-bind-command-discord')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate Feishu code' })).toBeEnabled();
    expect(discord).toBeDisabled();

    await act(async () => {
      pending.get('discord')?.(created('discord', 'DLT-DI22-SCD2'));
    });
    expect(screen.getByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-FE11-SHU1');
    expect(screen.getByTestId('channel-bind-command-discord')).toHaveTextContent('/bind DLT-DI22-SCD2');
    expect(screen.getAllByText('Send this command in a private message to the Dalton bot. Do not send it in a group chat.')).toHaveLength(2);
    expect(JSON.stringify(localStorage)).not.toContain('DLT-');
    expect(window.location.href).not.toContain('DLT-');
    expectNoSecrets();
  });

  it('releases a later rejected call before the earlier call finishes', async () => {
    const pending = new Map<string, {
      resolve: (value: DaltonChannelBindCodeCreateResponse) => void;
      reject: (reason: unknown) => void;
    }>();
    h.createCode.mockImplementation((platform) => new Promise<DaltonChannelBindCodeCreateResponse>((resolve, reject) => {
      pending.set(platform, { resolve, reject });
    }));
    renderCard();
    const feishu = await screen.findByRole('button', { name: 'Generate Feishu code' });
    const discord = screen.getByRole('button', { name: 'Generate Discord code' });
    await act(async () => {
      fireEvent.click(feishu);
      fireEvent.click(discord);
    });
    expect(feishu).toBeDisabled();
    expect(discord).toBeDisabled();

    await act(async () => {
      pending.get('discord')?.reject(apiError(500, 'Bind service unavailable'));
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not generate a Discord code. Try again.');
    expect(screen.getByRole('alert')).toHaveTextContent('Bind service unavailable');
    expect(screen.getByRole('button', { name: 'Generate Discord code' })).toBeEnabled();
    expect(feishu).toBeDisabled();
    expect(feishu).toHaveTextContent('Generating Feishu code…');
    expect(screen.queryByTestId('channel-bind-command-discord')).not.toBeInTheDocument();

    await act(async () => {
      pending.get('feishu')?.resolve(created('feishu', 'DLT-FE11-SHU1'));
    });
    expect(await screen.findByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-FE11-SHU1');
    expect(screen.getByRole('button', { name: 'Generate Feishu code' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Generate Discord code' })).toBeEnabled();
  });

  it('does not generate a code on mount or remount', async () => {
    const view = renderCard();
    expect(await screen.findByRole('button', { name: 'Generate Feishu code' })).toBeInTheDocument();
    view.unmount();
    renderCard();
    expect(await screen.findByRole('button', { name: 'Generate Discord code' })).toBeInTheDocument();
    expect(h.createCode).not.toHaveBeenCalled();
  });

  it('copies the server command and says when the copy fails', async () => {
    h.createCode.mockResolvedValue(created('feishu', 'DLT-FE11-SHU1'));
    renderCard();
    fireEvent.click(await screen.findByRole('button', { name: 'Generate Feishu code' }));
    expect(await screen.findByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-FE11-SHU1');
    fireEvent.click(screen.getByRole('button', { name: 'Copy Feishu command' }));
    expect(await screen.findByText('Feishu command copied')).toBeInTheDocument();
    expect(h.copyText).toHaveBeenCalledWith('/bind DLT-FE11-SHU1');

    h.copyText.mockRejectedValueOnce(new Error('refused'));
    fireEvent.click(screen.getByRole('button', { name: 'Copy Feishu command' }));
    expect(await screen.findByText('Could not copy the Feishu command. Select the command and copy it yourself.')).toBeInTheDocument();
  });

  it('starts a code from the keyboard and shows a readable generate error', async () => {
    const user = userEvent.setup();
    h.createCode.mockRejectedValueOnce(apiError(500, 'Bind service unavailable'));
    renderCard();
    const discord = await screen.findByRole('button', { name: 'Generate Discord code' });
    discord.focus();
    await user.keyboard('{Enter}');
    expect(h.createCode).toHaveBeenCalledWith('discord');
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not generate a Discord code. Try again.');
    expect(screen.getByRole('alert')).toHaveTextContent('Bind service unavailable');
    expect(screen.queryByTestId('channel-bind-command-discord')).not.toBeInTheDocument();
    expect(discord).toBeEnabled();

    h.createCode.mockRejectedValueOnce(new Error('Request failed with status code 500'));
    await user.click(discord);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not generate a Discord code. Try again.');
    expect(screen.getByRole('alert')).not.toHaveTextContent('status code');
  });

  it('rejects a code for the other platform and keeps the command out of the page', async () => {
    h.createCode.mockResolvedValue(created('discord', 'DLT-DI22-SCD2'));
    renderCard();
    fireEvent.click(await screen.findByRole('button', { name: 'Generate Feishu code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not generate a Feishu code. Try again.');
    expect(screen.queryByText('/bind DLT-DI22-SCD2')).not.toBeInTheDocument();
  });

  it('shows a linked account from the binding row and clears only that platform after a refresh', async () => {
    h.fetchBindings.mockResolvedValue(bindings());
    h.createCode.mockImplementation(async (platform) => (
      platform === 'feishu' ? created('feishu', 'DLT-FE11-SHU1') : created('discord', 'DLT-DI22-SCD2')
    ));
    renderCard();
    fireEvent.click(await screen.findByRole('button', { name: 'Generate Feishu code' }));
    expect(await screen.findByText('/bind DLT-FE11-SHU1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Generate Discord code' }));
    expect(await screen.findByText('/bind DLT-DI22-SCD2')).toBeInTheDocument();

    h.fetchBindings.mockResolvedValue(bindings({
      feishu: state('feishu', true, 'Ada', 'ou_ada'),
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Feishu and Discord accounts' }));
    await waitFor(() => {
      expect(screen.queryByText('/bind DLT-FE11-SHU1')).not.toBeInTheDocument();
    });
    expect(screen.getByText('/bind DLT-DI22-SCD2')).toBeInTheDocument();
    const feishu = screen.getByTestId('channel-bind-feishu');
    expect(feishu).toHaveTextContent('Linked');
    expect(feishu).toHaveTextContent('Account name: Ada');
    expect(feishu).toHaveTextContent('Account ID: ou_ada');
    expect(feishu).not.toHaveTextContent('Not linked');
    expect(screen.queryByRole('button', { name: 'Generate Feishu code' })).not.toBeInTheDocument();
    expect(screen.getByTestId('channel-bind-discord')).toHaveTextContent('Not linked');
    expectNoSecrets();
  });

  it('refetches when the window regains focus and shows the linked account', async () => {
    focusManager.setFocused(false);
    h.fetchBindings
      .mockResolvedValueOnce(bindings())
      .mockResolvedValue(bindings({ feishu: state('feishu', true, 'Ada', 'ou_ada') }));
    renderCard();
    expect(await screen.findByRole('button', { name: 'Generate Feishu code' })).toBeInTheDocument();
    const calls = h.fetchBindings.mock.calls.length;
    focusManager.setFocused(true);
    await waitFor(() => expect(h.fetchBindings.mock.calls.length).toBeGreaterThan(calls));
    await waitFor(() => {
      const feishu = screen.getByTestId('channel-bind-feishu');
      expect(feishu).toHaveTextContent('Linked');
      expect(feishu).toHaveTextContent('Account name: Ada');
    });
    expect(screen.queryByRole('button', { name: 'Generate Feishu code' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate Discord code' })).toBeInTheDocument();
  });

  it('stops showing an expired code, allows an explicit replacement, and does not poll before a code exists', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-10T12:00:00.000Z'));
    try {
      h.createCode
        .mockImplementationOnce(() => Promise.resolve(created(
          'feishu',
          'DLT-OLD1-CODE',
          new Date(Date.now() + 3_000).toISOString(),
        )))
        .mockImplementationOnce(() => Promise.resolve(created(
          'feishu',
          'DLT-NEW2-CODE',
          new Date(Date.now() + 600_000).toISOString(),
        )));
      renderCard();
      await flushFake();
      expect(screen.getByRole('button', { name: 'Generate Feishu code' })).toBeInTheDocument();
      const before = h.fetchBindings.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CHANNEL_BINDINGS_POLL_MS * 3);
      });
      expect(h.fetchBindings.mock.calls.length).toBe(before);

      fireEvent.click(screen.getByRole('button', { name: 'Generate Feishu code' }));
      await flushFake();
      expect(screen.getByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-OLD1-CODE');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(screen.queryByText('/bind DLT-OLD1-CODE')).not.toBeInTheDocument();
      expect(screen.getByText('This Feishu code has expired. Generate a new one to continue.')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Generate Feishu code' })).toBeEnabled();

      fireEvent.click(screen.getByRole('button', { name: 'Generate Feishu code' }));
      await flushFake();
      expect(screen.getByTestId('channel-bind-command-feishu')).toHaveTextContent('/bind DLT-NEW2-CODE');
      expect(screen.queryByText('/bind DLT-OLD1-CODE')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('polls only while a code can still be used, then stops after the account links or the card unmounts', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-10T12:00:00.000Z'));
    try {
      h.createCode.mockResolvedValue(created('feishu', 'DLT-FE11-SHU1', '2026-11-10T12:10:00.000Z'));
      const view = renderCard();
      await flushFake();
      fireEvent.click(screen.getByRole('button', { name: 'Generate Feishu code' }));
      await flushFake();
      expect(screen.getByText('/bind DLT-FE11-SHU1')).toBeInTheDocument();
      const afterIssue = h.fetchBindings.mock.calls.length;

      await act(async () => {
        await vi.advanceTimersByTimeAsync(CHANNEL_BINDINGS_POLL_MS);
      });
      expect(h.fetchBindings.mock.calls.length).toBeGreaterThan(afterIssue);

      h.fetchBindings.mockResolvedValue(bindings({
        feishu: state('feishu', true, 'Ada', 'ou_ada'),
      }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CHANNEL_BINDINGS_POLL_MS);
      });
      expect(screen.queryByText('/bind DLT-FE11-SHU1')).not.toBeInTheDocument();
      expect(screen.getByTestId('channel-bind-feishu')).toHaveTextContent('Linked');
      const afterLink = h.fetchBindings.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CHANNEL_BINDINGS_POLL_MS * 3);
      });
      expect(h.fetchBindings.mock.calls.length).toBe(afterLink);

      h.createCode.mockResolvedValue(created('discord', 'DLT-DI22-SCD2', '2026-11-10T12:10:00.000Z'));
      fireEvent.click(screen.getByRole('button', { name: 'Generate Discord code' }));
      await flushFake();
      expect(screen.getByText('/bind DLT-DI22-SCD2')).toBeInTheDocument();
      const whileLive = h.fetchBindings.mock.calls.length;
      view.unmount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CHANNEL_BINDINGS_POLL_MS * 3);
      });
      expect(h.fetchBindings.mock.calls.length).toBe(whileLive);
    } finally {
      vi.useRealTimers();
    }
  });

  it('uses the Chinese catalog and the shared bindings query on the Models section', async () => {
    const { default: i18n } = await import('@/i18n');
    await act(() => i18n.changeLanguage('zh-CN'));
    try {
      const { client } = renderCard();
      expect(await screen.findByRole('button', { name: '生成飞书绑定码' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '生成Discord绑定码' })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: '飞书与 Discord 账号' })).toBeInTheDocument();
      expect(document.getElementById('dalton-channel-bindings')).toBeTruthy();
      expect(client.getQueryCache().getAll().map((query) => query.queryKey)).toEqual([
        queryKeys.dalton.channelBindings(),
      ]);
    } finally {
      await act(() => i18n.changeLanguage('en-US'));
    }
  });

  it('mounts beside the Weixin card and stays off the integrations panel', async () => {
    h.fetchBindings.mockClear();
    h.fetchBindings.mockResolvedValue(bindings());
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <DaltonSection />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole('heading', { name: 'Feishu and Discord accounts' })).toBeInTheDocument();
    expect(await screen.findByTestId('dalton-weixin-channel-card')).toBeInTheDocument();
    expect(client.getQueryCache().getAll().map((query) => query.queryKey)).toEqual([
      queryKeys.dalton.channelBindings(),
    ]);

    const integrations = readFileSync('src/pages/Settings/panels/IntegrationsPanel.tsx', 'utf8');
    expect(integrations).toContain('DaltonWeixinChannelCard');
    expect(integrations).not.toContain('DaltonChannelBindingsCard');
    const section = readFileSync('src/pages/Settings/panels/dalton/DaltonSection.tsx', 'utf8');
    expect(section).toContain('<DaltonChannelBindingsCard />');
    expect(section).toContain('<DaltonWeixinChannelCard />');
    const card = readFileSync('src/pages/Settings/panels/dalton/DaltonChannelBindingsCard.tsx', 'utf8');
    expect(card).not.toContain('ttl_seconds');
    expect(card).not.toContain('instruction');
    expect(card).not.toContain('localStorage');
  });
});

describe('issuedBindCode', () => {
  const now = Date.parse('2026-11-10T12:00:00.000Z');

  it('keeps the server command and caps a far-future expiry', () => {
    const issued = issuedBindCode(
      created('feishu', 'DLT-AB12-CD34', '2027-11-10T12:00:00.000Z'),
      'feishu',
      now,
    );
    expect(issued).toEqual({
      platform: 'feishu',
      command: '/bind DLT-AB12-CD34',
      expiresAtMs: now + CHANNEL_BINDINGS_MAX_WAIT_MS,
    });
    expect(issuedBindCode(created('discord', 'DLT-DI22-SCD2'), 'feishu', now)).toBeNull();
    expect(issuedBindCode({ ...created('feishu'), command: '   ', code: '   ' }, 'feishu', now)).toBeNull();
    expect(issuedBindCode({
      ...created('discord', 'DLT-ZZ99-YY88', 'not-a-date'),
      expires_in_seconds: 600,
    }, 'discord', now)?.expiresAtMs).toBe(now + 600_000);
  });
});
