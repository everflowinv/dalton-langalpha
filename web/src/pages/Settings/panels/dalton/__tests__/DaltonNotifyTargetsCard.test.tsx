import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonNotifyTargetsCard } from '../DaltonNotifyTargetsCard';
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

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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
});
