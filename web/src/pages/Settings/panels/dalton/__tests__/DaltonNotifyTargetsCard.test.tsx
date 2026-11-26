import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DaltonNotifyTargetsCard,
  countDistinctWeixinRouteTasks,
} from '../DaltonNotifyTargetsCard';
import type { Automation } from '@/types/automation';
import type {
  DaltonChannelBindingsResponse,
  DaltonNotifyTestResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  prefs: {
    model_preference: {
      dalton_notify_enabled: true as boolean | null | undefined,
      dalton_notify_targets: [] as string[],
      dalton_notify_target_labels: undefined as Record<string, string> | undefined,
    },
    other_preference: {},
  },
  mutateAsync: vi.fn<
    (payload: unknown) => Promise<{ model_preference?: Record<string, unknown> }>
  >(),
  bindings: {
    user_id: 'u-1',
    user_tier: 'internal',
    home_workspace_id: 'home-ws',
    bindings: {
      feishu: {
        linked: true,
        platform: 'feishu',
        platform_user_id: 'ou_ada',
        platform_username: 'Ada Feishu',
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
        linked: false,
        platform: 'weixin',
        platform_user_id: null,
        platform_username: null,
        updated_at: null,
      },
    },
    identities: {},
    settings: { version: '1', apps: {} },
    chat_bindings: [],
    weixin_bot: null,
  } as DaltonChannelBindingsResponse,
  sendTest: vi.fn<(target: string) => Promise<DaltonNotifyTestResponse>>(),
  capabilities: new Set<string>(['channel_feishu', 'channel_discord', 'channel_weixin']),
  automations: null as Automation[] | null,
}));

vi.mock('@/hooks/useCapabilities', () => ({
  useCapabilities: () => ({
    hasCapability: (cap: string) => h.capabilities.has(cap),
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: h.prefs,
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({
    isPending: false,
    mutateAsync: (payload: unknown) => h.mutateAsync(payload),
  }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonChannelBindings: async () => h.bindings,
  sendDaltonNotifyTest: (target: string) => h.sendTest(target),
}));

vi.mock('@/pages/Automations/utils/api', () => ({
  listAutomations: async () => {
    if (h.automations === null) {
      throw new Error('automations unavailable');
    }
    return { data: { automations: h.automations, total: h.automations.length } };
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts && typeof opts.count === 'number' ? `${key}:${opts.count}` : key,
  }),
}));

function renderCard(allowManualInput = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonNotifyTargetsCard allowManualInput={allowManualInput} />
    </QueryClientProvider>,
  );
}

describe('DaltonNotifyTargetsCard', () => {
  beforeEach(() => {
    h.prefs = {
      model_preference: {
        dalton_notify_enabled: true,
        dalton_notify_targets: [],
        dalton_notify_target_labels: undefined,
      },
      other_preference: {},
    };
    h.mutateAsync.mockReset().mockResolvedValue({});
    h.sendTest.mockReset().mockResolvedValue({ ok: true, detail: null, error_code: null });
    h.capabilities = new Set(['channel_feishu', 'channel_discord', 'channel_weixin']);
    h.bindings.weixin_account = null;
    h.automations = null;
  });

  it('derives bound-account switches from linked bindings without manual target input by default', async () => {
    renderCard(false);

    expect(screen.queryByTestId('dalton-notify-manual-input')).not.toBeInTheDocument();
    const feishuRow = await screen.findByTestId('dalton-notify-bound-feishu');
    expect(feishuRow).toHaveTextContent('Ada Feishu');

    const accountToggle = screen.getByRole('switch', { name: 'Ada Feishu' });
    expect(accountToggle).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(accountToggle);
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_targets: ['feishu:user:ou_ada'],
        },
      });
    });
  });

  it('requires confirmation before disabling an enabled bound account switch', async () => {
    h.prefs.model_preference.dalton_notify_targets = ['feishu:user:ou_ada'];
    renderCard(false);

    const accountToggle = await screen.findByRole('switch', { name: 'Ada Feishu' });
    expect(accountToggle).toHaveAttribute('aria-checked', 'true');

    fireEvent.click(accountToggle);
    expect(h.mutateAsync).not.toHaveBeenCalled();

    const confirmBtn = await screen.findByRole('button', { name: 'dalton.common.confirm' });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: { dalton_notify_targets: [] },
      });
    });
  });

  it('requires confirmation before removing custom target in admin manual mode and supports test push', async () => {
    h.prefs.model_preference.dalton_notify_targets = ['feishu:chat:oc_team1'];
    renderCard(true);

    const input = await screen.findByTestId('dalton-notify-manual-input');
    expect(input).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dalton-notify-remove-feishu:chat:oc_team1'));
    expect(h.mutateAsync).not.toHaveBeenCalled();

    const confirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.notifyTargets.removeLabel',
    });
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: { dalton_notify_targets: [] },
      });
    });
  });

  it('defaults master toggle to off when dalton_notify_enabled is unset or null', async () => {
    delete h.prefs.model_preference.dalton_notify_enabled;
    const { unmount } = renderCard(false);
    expect(
      await screen.findByRole('switch', { name: 'dalton.notifyTargets.masterSwitchLabel' }),
    ).toHaveAttribute('aria-checked', 'false');
    unmount();

    h.prefs.model_preference.dalton_notify_enabled = null;
    renderCard(false);
    expect(
      await screen.findByRole('switch', { name: 'dalton.notifyTargets.masterSwitchLabel' }),
    ).toHaveAttribute('aria-checked', 'false');
  });

  it('rejects group/channel targets with task-routing hint and accepts valid personal DM targets', async () => {
    renderCard(true);

    const input = await screen.findByTestId('dalton-notify-manual-input');
    const addBtn = screen.getByTestId('dalton-notify-manual-add');

    fireEvent.change(input, { target: { value: 'feishu:chat:oc_group1' } });
    fireEvent.click(addBtn);
    expect(await screen.findByRole('alert')).toHaveTextContent('dalton.notifyTargets.groupTargetHint');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'discord:channel:123456' } });
    fireEvent.click(addBtn);
    expect(await screen.findByRole('alert')).toHaveTextContent('dalton.notifyTargets.groupTargetHint');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'feishu:user:ou_newuser' } });
    fireEvent.click(addBtn);
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: { dalton_notify_targets: ['feishu:user:ou_newuser'] },
      });
    });
  });

  it('displays, edits, and saves dalton_notify_target_labels on blur', async () => {
    h.prefs.model_preference.dalton_notify_targets = [
      'feishu:user:ou_ada',
      'discord:user:998877',
    ];
    h.prefs.model_preference.dalton_notify_target_labels = {
      'feishu:user:ou_ada': 'Ada Primary',
      'discord:user:998877': 'Night Desk',
    };
    renderCard(false);

    const boundRow = await screen.findByTestId('dalton-notify-bound-feishu');
    expect(boundRow).toHaveTextContent('Ada Primary');

    const boundInput = screen.getByTestId(
      'dalton-notify-label-feishu:user:ou_ada',
    ) as HTMLInputElement;
    expect(boundInput.value).toBe('Ada Primary');

    const customInput = screen.getByTestId(
      'dalton-notify-label-discord:user:998877',
    ) as HTMLInputElement;
    expect(customInput.value).toBe('Night Desk');

    fireEvent.change(customInput, { target: { value: '  Morning Brief  ' } });
    fireEvent.blur(customInput);

    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_target_labels: {
            'feishu:user:ou_ada': 'Ada Primary',
            'discord:user:998877': 'Morning Brief',
          },
        },
      });
    });
  });

  it('offers connected personal WeChat as a selectable push target and hides pending/unlinked WeChat', async () => {
    h.capabilities = new Set(['channel_weixin']);
    h.bindings.weixin_account = {
      linked: true,
      platform: 'weixin',
      status: 'active',
      channel_enabled: true,
      allow_external: false,
      member_allowed: true,
      deny_reason: null,
      account_id: 'acc_wx_1',
      platform_user_id: 'wx_alice_01',
      masked_ilink_user_id: '****e_01',
      platform_username: 'Alice WX',
      ilink_username: 'Alice WX',
      active_push_ready: true,
      last_inbound_at: '2026-11-26T00:00:00Z',
      updated_at: '2026-11-26T00:00:00Z',
    };

    const { unmount } = renderCard(false);
    const wxRow = await screen.findByTestId('dalton-notify-bound-weixin');
    expect(wxRow).toHaveTextContent('Alice WX');
    expect(screen.queryByTestId('dalton-notify-bound-feishu')).not.toBeInTheDocument();

    const wxToggle = screen.getByRole('switch', { name: 'Alice WX' });
    fireEvent.click(wxToggle);
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_targets: ['weixin:user:wx_alice_01'],
        },
      });
    });

    unmount();

    h.bindings.weixin_account = {
      ...h.bindings.weixin_account,
      status: 'pending_verification',
    };
    renderCard(false);
    await waitFor(() => {
      expect(screen.queryByTestId('dalton-notify-bound-weixin')).not.toBeInTheDocument();
    });
  });

  it('gates manual WeChat target copy and validation on channel_weixin capability', async () => {
    // channel_weixin disabled: omits WithWeixin copy and rejects weixin:user: input without saving
    h.capabilities = new Set(['channel_feishu', 'channel_discord']);
    const { unmount } = renderCard(true);

    const inputOff = screen.getByTestId('dalton-notify-manual-input');
    expect(inputOff).toHaveAttribute('placeholder', 'dalton.notifyTargets.manualAddPlaceholder');
    fireEvent.change(inputOff, { target: { value: 'weixin:user:wx_unallowed' } });
    fireEvent.click(screen.getByTestId('dalton-notify-manual-add'));
    expect(await screen.findByText('dalton.notifyTargets.manualAddError')).toBeInTheDocument();
    expect(h.mutateAsync).not.toHaveBeenCalled();
    unmount();

    // channel_weixin enabled: shows WithWeixin placeholder and submits to backend (which verifies ownership)
    h.capabilities = new Set(['channel_feishu', 'channel_discord', 'channel_weixin']);
    renderCard(true);
    const inputOn = screen.getByTestId('dalton-notify-manual-input');
    expect(inputOn).toHaveAttribute(
      'placeholder',
      'dalton.notifyTargets.manualAddPlaceholderWithWeixin',
    );
    fireEvent.change(inputOn, { target: { value: 'weixin:user:wx_me_1' } });
    fireEvent.click(screen.getByTestId('dalton-notify-manual-add'));
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: { dalton_notify_targets: ['weixin:user:wx_me_1'] },
      });
    });
  });

  it('clarifies explicit WeChat task routes in disable confirmation dialog', async () => {
    h.capabilities = new Set(['channel_weixin']);
    h.prefs.model_preference.dalton_notify_targets = ['weixin:user:wx_alice_01'];
    (h.prefs.model_preference as Record<string, unknown>).dalton_notify_routes = {
      'automation:a-1': ['weixin:user:wx_alice_01'],
      'automation:a-2': ['feishu:user:ou_ada', 'weixin:user:wx_alice_01'],
    };
    h.bindings.weixin_account = {
      linked: true,
      platform: 'weixin',
      status: 'active',
      channel_enabled: true,
      allow_external: false,
      member_allowed: true,
      deny_reason: null,
      account_id: 'acc_wx_1',
      platform_user_id: 'wx_alice_01',
      masked_ilink_user_id: '****e_01',
      platform_username: 'Alice WX',
      ilink_username: 'Alice WX',
      active_push_ready: true,
      last_inbound_at: null,
      updated_at: null,
    };

    const { unmount } = renderCard(false);
    await screen.findByTestId('dalton-notify-bound-weixin');
    fireEvent.click(screen.getByRole('switch', { name: 'Alice WX' }));
    expect(
      await screen.findByText('dalton.notifyTargets.disableBoundWeixinConfirmWithCount:2'),
    ).toBeInTheDocument();
    unmount();

    (h.prefs.model_preference as Record<string, unknown>).dalton_notify_routes = {};
    renderCard(false);
    await screen.findByTestId('dalton-notify-bound-weixin');
    fireEvent.click(screen.getByRole('switch', { name: 'Alice WX' }));
    expect(
      await screen.findByText('dalton.notifyTargets.disableBoundWeixinConfirm'),
    ).toBeInTheDocument();
  });

  it('deduplicates explicit WeChat route task count across ID, legacy display-name, and category keys', async () => {
    const wx = 'weixin:user:wx_alice_01';
    const taskA = {
      automation_id: 'a-1',
      name: 'dalton-googl-earn-2026q1-t0',
      dalton_notify_category: 'earnings_t0',
    } as Automation;
    const taskB = {
      automation_id: 'a-2',
      name: 'dalton-msft-earn-2026q1-t0',
      dalton_notify_category: 'earnings_t0',
    } as Automation;

    // Task A has BOTH automation:a-1 and legacy name key with wx -> counted once (1, not 2).
    // Task B has legacy name key with wx, overridden by automation:a-2 without wx -> counted 0.
    const routes = {
      'automation:a-1': [wx],
      'dalton-googl-earn-2026q1-t0': [wx],
      'automation:a-2': ['feishu:user:ou_ada'],
      'dalton-msft-earn-2026q1-t0': [wx],
    };
    expect(countDistinctWeixinRouteTasks(routes, wx, [taskA, taskB])).toBe(1);
    // Without loaded automations, category fallback keys return null (non-numeric copy)
    expect(countDistinctWeixinRouteTasks({ 'category:earnings_t0': [wx] }, wx, null)).toBeNull();

    h.capabilities = new Set(['channel_weixin']);
    h.prefs.model_preference.dalton_notify_targets = [wx];
    (h.prefs.model_preference as Record<string, unknown>).dalton_notify_routes = routes;
    h.automations = [taskA, taskB];
    h.bindings.weixin_account = {
      linked: true,
      platform: 'weixin',
      status: 'active',
      channel_enabled: true,
      allow_external: false,
      member_allowed: true,
      deny_reason: null,
      account_id: 'acc_wx_1',
      platform_user_id: 'wx_alice_01',
      masked_ilink_user_id: '****e_01',
      platform_username: 'Alice WX',
      ilink_username: 'Alice WX',
      active_push_ready: true,
      last_inbound_at: null,
      updated_at: null,
    };

    renderCard(false);
    await screen.findByTestId('dalton-notify-bound-weixin');
    fireEvent.click(screen.getByRole('switch', { name: 'Alice WX' }));
    expect(
      await screen.findByText('dalton.notifyTargets.disableBoundWeixinConfirmWithCount:1'),
    ).toBeInTheDocument();
  });

  it('deduplicates data_task entries against data: route keys and falls back to non-numeric copy on ambiguous cold cache', () => {
    const wx = 'weixin:user:wx_alice_01';
    const dataTask = {
      automation_id: 'data-auto-1',
      name: 'cn-leverage-daily',
      dalton_notify_category: 'data_task',
    } as Automation;
    const routes = {
      'data:cn-leverage-daily': [wx],
      'data:jp-leverage-daily': [wx],
    };
    // cn-leverage-daily is in automations AND in routes['data:cn-leverage-daily'] -> counted once;
    // jp-leverage-daily is only in routes['data:jp-leverage-daily'] -> counted once; total = 2 (not 3).
    expect(countDistinctWeixinRouteTasks(routes, wx, [dataTask])).toBe(2);

    // Cold cache (automations = null) with both automation:<id> and legacy display-name key -> null (non-numeric copy)
    expect(
      countDistinctWeixinRouteTasks(
        {
          'automation:a-1': [wx],
          'dalton-googl-earn-2026q1-t0': [wx],
        },
        wx,
        null,
      ),
    ).toBeNull();
  });
});
