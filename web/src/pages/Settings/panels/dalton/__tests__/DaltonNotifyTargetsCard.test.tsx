import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { DaltonNotifyTargetsCard } from '../DaltonNotifyTargetsCard';
import type { DaltonNotifyTestResponse } from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  preferences: {
    model_preference: {} as Record<string, unknown>,
    other_preference: {} as Record<string, unknown>,
  },
  isLoading: false,
  isLoaded: true,
  listeners: new Set<() => void>(),
  mutateAsync: vi.fn(async (patch: Record<string, unknown>) => {
    const mp = (patch.model_preference ?? {}) as Record<string, unknown>;
    h.preferences = {
      ...h.preferences,
      model_preference: {
        ...h.preferences.model_preference,
        ...mp,
      },
    };
    for (const listener of h.listeners) listener();
    return h.preferences;
  }),
  sendTest: vi.fn<(target: string) => Promise<DaltonNotifyTestResponse>>(
    async () => ({ ok: true, detail: null, error_code: null }),
  ),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => {
    const [, forceRender] = React.useState(0);
    React.useEffect(() => {
      const cb = () => forceRender((n) => n + 1);
      h.listeners.add(cb);
      return () => {
        h.listeners.delete(cb);
      };
    }, []);
    return {
      preferences: h.preferences,
      isLoading: h.isLoading,
      isLoaded: h.isLoaded,
    };
  },
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({
    mutate: vi.fn(),
    mutateAsync: h.mutateAsync,
  }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  sendDaltonNotifyTest: (target: string) => h.sendTest(target),
}));

describe('DaltonNotifyTargetsCard', () => {
  beforeEach(() => {
    h.preferences = { model_preference: {}, other_preference: {} };
    h.isLoading = false;
    h.isLoaded = true;
    h.mutateAsync.mockClear();
    h.sendTest.mockClear();
    h.sendTest.mockImplementation(async () => ({ ok: true, detail: null, error_code: null }));
  });

  it('disables master switch, add button, and delete buttons before preferences finish loading', () => {
    h.isLoading = true;
    h.isLoaded = false;
    h.preferences = {
      model_preference: {
        dalton_notify_targets: ['feishu:user:ou_existing_01'],
      },
      other_preference: {},
    };

    render(<DaltonNotifyTargetsCard />);

    const masterSwitch = screen.getByRole('switch', { name: '飞书提醒' });
    const input = screen.getByLabelText('推送到');
    const addButton = screen.getByRole('button', { name: /添加/ });
    const deleteButton = screen.getByRole('button', { name: '删除 feishu:user:ou_existing_01' });

    expect(masterSwitch).toBeDisabled();
    expect(input).toBeDisabled();
    expect(addButton).toBeDisabled();
    expect(deleteButton).toBeDisabled();

    fireEvent.click(masterSwitch);
    fireEvent.click(addButton);
    fireEvent.click(deleteButton);
    expect(h.mutateAsync).not.toHaveBeenCalled();
  });

  it('rejects group formats, adds and removes personal targets, and reads them back', async () => {
    const { unmount } = render(<DaltonNotifyTargetsCard />);

    const input = screen.getByLabelText('推送到');
    const addButton = screen.getByRole('button', { name: /添加/ });

    // A group target is not a personal list entry (admin-only, per task).
    fireEvent.change(input, { target: { value: 'feishu:chat:oc_research_group' } });
    fireEvent.click(addButton);
    expect(await screen.findByRole('alert')).toHaveTextContent('格式无效');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'discord:user:not_numeric' } });
    fireEvent.click(addButton);
    expect(await screen.findByRole('alert')).toHaveTextContent('格式无效');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.change(input, { target: { value: 'feishu:user:ou_analyst_01' } });
      fireEvent.click(addButton);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_targets: ['feishu:user:ou_analyst_01'],
        },
      });
    });

    await act(async () => {
      fireEvent.change(input, { target: { value: 'discord:user:123456789' } });
      fireEvent.click(addButton);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_targets: ['feishu:user:ou_analyst_01', 'discord:user:123456789'],
        },
      });
    });

    unmount();
    render(<DaltonNotifyTargetsCard />);
    expect(screen.getByText('feishu:user:ou_analyst_01')).toBeInTheDocument();
    expect(screen.getByText('discord:user:123456789')).toBeInTheDocument();

    const deleteFeishuBtn = screen.getByRole('button', { name: '删除 feishu:user:ou_analyst_01' });
    await act(async () => {
      fireEvent.click(deleteFeishuBtn);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_targets: ['discord:user:123456789'],
        },
      });
    });
    expect(screen.queryByText('feishu:user:ou_analyst_01')).not.toBeInTheDocument();
  });

  it('shows the saved label and submits label edits as preference patches', async () => {
    h.preferences = {
      model_preference: {
        dalton_notify_targets: ['feishu:user:ou_me_1'],
        dalton_notify_target_labels: { 'feishu:user:ou_me_1': '我本人' },
      },
      other_preference: {},
    };
    render(<DaltonNotifyTargetsCard />);

    const labelInput = screen.getByLabelText('目标别名 feishu:user:ou_me_1');
    expect(labelInput).toHaveValue('我本人');

    await act(async () => {
      fireEvent.change(labelInput, { target: { value: '本人（新）' } });
      fireEvent.blur(labelInput);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_target_labels: { 'feishu:user:ou_me_1': '本人（新）' },
        },
      });
    });

    await act(async () => {
      fireEvent.change(labelInput, { target: { value: '' } });
      fireEvent.blur(labelInput);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_target_labels: { 'feishu:user:ou_me_1': null },
        },
      });
    });
  });

  it('shows the test-send outcome per target, success and failure', async () => {
    h.preferences = {
      model_preference: {
        dalton_notify_targets: ['feishu:user:ou_me_1', 'discord:user:1'],
      },
      other_preference: {},
    };
    h.sendTest.mockImplementation(async (target: string) =>
      target === 'feishu:user:ou_me_1'
        ? { ok: true, detail: null, error_code: null }
        : {
            ok: false,
            detail: '发送失败（exit_code=1）',
            error_code: 'exit_code=1',
          },
    );
    render(<DaltonNotifyTargetsCard />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '发送测试 feishu:user:ou_me_1' }));
    });
    expect(await screen.findByText('测试已发送')).toBeInTheDocument();
    expect(h.sendTest).toHaveBeenCalledWith('feishu:user:ou_me_1');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '发送测试 discord:user:1' }));
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('发送失败（exit_code=1）');
  });

  it('defaults master switch 飞书提醒 to off, toggles on/off, and reads back saved value', async () => {
    const { unmount } = render(<DaltonNotifyTargetsCard />);

    expect(
      screen.getByText('开启后，财报快讯、深度复盘、财报前瞻、重大股价异动、每周复盘会私信给你。'),
    ).toBeInTheDocument();

    const masterSwitch = screen.getByRole('switch', { name: '飞书提醒' });
    expect(masterSwitch).toHaveAttribute('aria-checked', 'false');

    await act(async () => {
      fireEvent.click(masterSwitch);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_enabled: true,
        },
      });
    });
    expect(masterSwitch).toHaveAttribute('aria-checked', 'true');

    unmount();
    render(<DaltonNotifyTargetsCard />);
    const remountedSwitch = screen.getByRole('switch', { name: '飞书提醒' });
    expect(remountedSwitch).toHaveAttribute('aria-checked', 'true');

    await act(async () => {
      fireEvent.click(remountedSwitch);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_enabled: false,
        },
      });
    });
    expect(remountedSwitch).toHaveAttribute('aria-checked', 'false');
  });
});
