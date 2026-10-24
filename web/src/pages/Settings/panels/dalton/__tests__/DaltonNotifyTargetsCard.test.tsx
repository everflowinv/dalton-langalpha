import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { DaltonNotifyTargetsCard } from '../DaltonNotifyTargetsCard';

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

describe('DaltonNotifyTargetsCard', () => {
  beforeEach(() => {
    h.preferences = { model_preference: {}, other_preference: {} };
    h.isLoading = false;
    h.isLoaded = true;
    h.mutateAsync.mockClear();
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

  it('validates target format, adds and removes targets, and reads back saved targets', async () => {
    const { unmount } = render(<DaltonNotifyTargetsCard />);

    const input = screen.getByLabelText('推送到');
    const addButton = screen.getByRole('button', { name: /添加/ });

    // 1. Format validation rejects invalid strings
    fireEvent.change(input, { target: { value: 'discord:user:not_numeric' } });
    fireEvent.click(addButton);
    expect(await screen.findByRole('alert')).toHaveTextContent('格式无效');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'feishu:chat:oc_123' } });
    fireEvent.click(addButton);
    expect(await screen.findByRole('alert')).toHaveTextContent('格式无效');
    expect(h.mutateAsync).not.toHaveBeenCalled();

    // 2. Add valid Feishu and Discord targets
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
    expect(screen.getByText('feishu:user:ou_analyst_01')).toBeInTheDocument();

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
    expect(screen.getByText('discord:user:123456789')).toBeInTheDocument();

    // 3. Unmount and remount to verify saved preferences are read back
    unmount();
    render(<DaltonNotifyTargetsCard />);
    expect(screen.getByText('feishu:user:ou_analyst_01')).toBeInTheDocument();
    expect(screen.getByText('discord:user:123456789')).toBeInTheDocument();

    // 4. Remove a target and verify update
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
    expect(screen.getByText('discord:user:123456789')).toBeInTheDocument();
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

    // Unmount and remount to verify saved state is read back
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
