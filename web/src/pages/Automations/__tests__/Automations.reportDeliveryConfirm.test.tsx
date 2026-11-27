import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import type { Automation, DeliveryOptions } from '@/types/automation';
import * as api from '../utils/api';
import Automations from '../Automations';

const confirmMocks = vi.hoisted(() => ({
  precheck: vi.fn(),
  issue: vi.fn(),
}));

const home = vi.hoisted(() => ({
  getHomeWorkspace: vi.fn(),
}));

vi.mock('@/lib/dalton/reportDeliveryConfirmation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/reportDeliveryConfirmation')>();
  return {
    ...actual,
    precheckReportDeliveryConfirmation: confirmMocks.precheck,
    issueReportDeliveryConfirmation: confirmMocks.issue,
  };
});

vi.mock('@/hooks/useCapabilities', () => ({
  useCapabilities: () => ({
    capabilities: { channel_gateway: true },
    hasCapability: (cap: string) => cap === 'channel_gateway',
    isLoading: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: { model_preference: {}, other_preference: {} },
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

vi.mock('@/hooks/useHomeTimezone', () => ({ useHomeTimezone: () => 'America/New_York' }));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws-1', name: 'Research' }] } }),
}));
vi.mock('@/pages/ChatAgent/utils/api/workspaces', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHomeWorkspace: home.getHomeWorkspace,
}));

vi.mock('@/contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/contexts/AuthContext')>();
  return { ...actual, useAuth: () => ({ userId: 'user-1', isLoggedIn: true, isInitialized: true }) };
});

vi.mock('../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/api')>();
  const { personalReadMocks } = await import('./readBridge');
  const listAutomations = vi.fn(async () => ({
    data: { automations: new Array<Automation>(), total: 0 },
  }));
  return {
    ...actual,
    listAutomations,
    listExecutions: vi.fn(async () => ({ data: { executions: [], has_more: false } })),
    listRecentRuns: vi.fn(async () => ({ data: { executions: [], has_more: false } })),
    getDeliveryOptions: vi.fn(async () => ({
      data: { enabled: false, workspace_id: 'ws-1', apps: {} } as DeliveryOptions,
    })),
    createAutomation: vi.fn(async (payload) => ({
      data: {
        automation_id: 'auto-new-1',
        user_id: 'user-1',
        name: payload.name,
        description: null,
        trigger_type: payload.trigger_type,
        cron_expression: payload.cron_expression ?? '0 9 * * 1-5',
        timezone: payload.timezone ?? 'America/New_York',
        trigger_config: null,
        next_run_at: null,
        last_run_at: null,
        instruction: payload.instruction,
        workspace_id: 'ws-1',
        llm_model: null,
        thread_strategy: 'continue',
        conversation_thread_id: null,
        status: 'active',
        max_failures: 3,
        failure_count: 0,
        disable_reason: null,
        delivery_config: null,
        metadata: payload.metadata ?? {},
        created_at: '2026-11-26T00:00:00Z',
        updated_at: '2026-11-26T00:00:00Z',
        last_execution: null,
      } as Automation,
    })),
    updateAutomation: vi.fn(async (id, payload) => ({
      data: {
        automation_id: id,
        user_id: 'user-1',
        name: payload.name ?? 'Updated',
        description: payload.description ?? null,
        trigger_type: 'cron',
        cron_expression: payload.cron_expression ?? '0 9 * * 1-5',
        timezone: payload.timezone ?? 'America/New_York',
        trigger_config: null,
        next_run_at: null,
        last_run_at: null,
        instruction: payload.instruction ?? 'Run report.',
        workspace_id: 'ws-1',
        llm_model: null,
        thread_strategy: 'continue',
        conversation_thread_id: null,
        status: 'active',
        max_failures: 3,
        failure_count: 0,
        disable_reason: null,
        delivery_config: null,
        metadata: payload.metadata ?? {},
        created_at: '2026-11-26T00:00:00Z',
        updated_at: '2026-11-26T00:00:00Z',
        last_execution: null,
      } as Automation,
    })),
    ...personalReadMocks(listAutomations),
  };
});

beforeEach(() => {
  home.getHomeWorkspace.mockResolvedValue({ workspace_id: 'home-1', is_home: true });
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

const label = (key: string, opts?: Record<string, unknown>) => i18n.t(key, opts);

describe('Automations report delivery confirmation dialog (AD5)', () => {
  it('shows confirmation dialog for group targets, issues one-time token on confirm, and aborts on cancel', async () => {
    confirmMocks.precheck.mockResolvedValue({
      data: {
        needs_confirmation: true,
        action: 'create',
        automation_id: null,
        automation_name: 'Daily Filing Alert',
        config_version: null,
        profile: 'daily-filing-alert',
        report_targets: ['feishu:chat:oc_group_auth_1'],
        text_targets: null,
        failure_targets: [],
        group_targets: ['feishu:chat:oc_group_auth_1'],
        non_caller_dm_targets: [],
        target_labels: { 'feishu:chat:oc_group_auth_1': 'Research Group 1' },
      },
    });
    confirmMocks.issue.mockResolvedValue({
      data: {
        needs_confirmation: true,
        confirmation_id: 'conf-1',
        confirmation_token: 'one-time-tok-xyz',
        expires_at: '2026-11-26T01:00:00Z',
        action: 'create',
        automation_id: null,
        automation_name: 'Daily Filing Alert',
        config_version: null,
        profile: 'daily-filing-alert',
        report_targets: ['feishu:chat:oc_group_auth_1'],
        text_targets: null,
        failure_targets: [],
        target_labels: { 'feishu:chat:oc_group_auth_1': 'Research Group 1' },
      },
    });

    renderWithProviders(<Automations />, { route: '/automations' });

    // Start new automation from zero-state blank automation (matching Automations.place.test.tsx)
    fireEvent.click(await screen.findByText(label('automation:blankAutomation')));
    fireEvent.change(await screen.findByLabelText(label('common.name')), {
      target: { value: 'Daily Filing Alert' },
    });
    fireEvent.change(screen.getByLabelText(label('automation:instruction')), {
      target: { value: 'Run daily filing alert report.' },
    });

    // Open More options -> expand report profile fields -> fill profile & group target
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation:moreOptions')) }));
    fireEvent.click(
      await screen.findByRole('button', { name: label('automation:reportDeliveryConfigure') }),
    );
    const profileInput = await screen.findByLabelText(label('automation:reportDeliveryConfirmProfile'));
    fireEvent.change(profileInput, { target: { value: 'daily-filing-alert' } });
    const targetsInput = await screen.findByLabelText(label('automation:reportDeliveryConfirmReportTargets'));
    fireEvent.change(targetsInput, { target: { value: 'feishu:chat:oc_group_auth_1' } });

    // Submit form -> precheck runs, dialog opens, createAutomation NOT called yet
    const submitBtn = screen.getByRole('button', { name: label('common.create') });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(confirmMocks.precheck).toHaveBeenCalledTimes(1);
    });
    // text_targets=null inherits report_targets -> actual recipient appears under both report cards and plain text!
    expect((await screen.findAllByText('Research Group 1')).length).toBe(2);
    expect(screen.getByText(new RegExp(label('automation:reportDeliveryTextInheritedNote')))).toBeTruthy();
    expect(api.createAutomation).not.toHaveBeenCalled();

    // Cancel closes dialog without calling createAutomation
    fireEvent.click(screen.getByRole('button', { name: label('common.cancel') }));
    await waitFor(() => {
      expect(screen.queryAllByText('Research Group 1')).toHaveLength(0);
    });
    expect(api.createAutomation).not.toHaveBeenCalled();

    // Submit again -> confirm -> issues one-time credential and attaches report_delivery_confirmation_token
    fireEvent.click(submitBtn);
    await screen.findAllByText('Research Group 1');
    fireEvent.click(
      screen.getByRole('button', { name: label('automation:reportDeliveryConfirmCreate') }),
    );

    await waitFor(() => {
      expect(confirmMocks.issue).toHaveBeenCalledTimes(1);
      expect(api.createAutomation).toHaveBeenCalledTimes(1);
    });
    const sentPayload = vi.mocked(api.createAutomation).mock.calls[0]?.[0];
    expect(sentPayload?.report_delivery_confirmation_token).toBe('one-time-tok-xyz');
  });

  it('preserves trailing comma while typing multiple targets and distinguishes inherit vs disabled text_targets', async () => {
    confirmMocks.precheck.mockResolvedValue({
      data: {
        needs_confirmation: true,
        action: 'create',
        automation_id: null,
        automation_name: 'Multi Target Alert',
        config_version: null,
        profile: 'daily-filing-alert',
        report_targets: ['feishu:chat:oc_group_1', 'feishu:chat:oc_group_2'],
        text_targets: [],
        failure_targets: [],
        group_targets: ['feishu:chat:oc_group_1', 'feishu:chat:oc_group_2'],
        non_caller_dm_targets: [],
        target_labels: {
          'feishu:chat:oc_group_1': 'Group One',
          'feishu:chat:oc_group_2': 'Group Two',
        },
      },
    });

    renderWithProviders(<Automations />, { route: '/automations' });

    fireEvent.click(await screen.findByText(label('automation:blankAutomation')));
    fireEvent.change(await screen.findByLabelText(label('common.name')), {
      target: { value: 'Multi Target Alert' },
    });
    fireEvent.change(screen.getByLabelText(label('automation:instruction')), {
      target: { value: 'Run report.' },
    });

    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation:moreOptions')) }));
    fireEvent.click(
      await screen.findByRole('button', { name: label('automation:reportDeliveryConfigure') }),
    );
    fireEvent.change(await screen.findByLabelText(label('automation:reportDeliveryConfirmProfile')), {
      target: { value: 'daily-filing-alert' },
    });

    // Trailing comma must stay intact so user can type a second target.
    const reportInput = (await screen.findByLabelText(
      label('automation:reportDeliveryConfirmReportTargets'),
    )) as HTMLInputElement;
    fireEvent.change(reportInput, { target: { value: 'feishu:chat:oc_group_1,' } });
    expect(reportInput.value).toBe('feishu:chat:oc_group_1,');
    fireEvent.change(reportInput, { target: { value: 'feishu:chat:oc_group_1, feishu:chat:oc_group_2' } });
    expect(reportInput.value).toBe('feishu:chat:oc_group_1, feishu:chat:oc_group_2');

    // Explicitly choose "Do not send plain text" -> sends text_targets: [] instead of omitting it.
    const textModeSelect = screen.getByLabelText(label('automation:reportDeliveryTextMode'));
    fireEvent.change(textModeSelect, { target: { value: 'none' } });

    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));

    await waitFor(() => {
      expect(confirmMocks.precheck).toHaveBeenCalledTimes(1);
    });
    const precheckArg = confirmMocks.precheck.mock.calls[0]?.[0];
    expect(precheckArg.report_profile.report_targets).toEqual([
      'feishu:chat:oc_group_1',
      'feishu:chat:oc_group_2',
    ]);
    expect(precheckArg.report_profile.text_targets).toEqual([]);

    // Confirmation dialog displays report targets once (NOT duplicated into plain text) and shows disabled note.
    expect(await screen.findAllByText('Group One')).toHaveLength(1);
    expect(screen.getAllByText('Group Two')).toHaveLength(1);
    expect(screen.getAllByText(label('automation:reportDeliveryTextDisabled')).length).toBeGreaterThanOrEqual(1);
  });

  it('clears report_profile by submitting metadata.report_profile=null directly without confirmation dialog', async () => {
    const existingAuto: Automation = {
      automation_id: 'auto-clear-1',
      user_id: 'user-1',
      name: 'Existing Group Report',
      description: null,
      trigger_type: 'cron',
      cron_expression: '0 9 * * 1-5',
      timezone: 'America/New_York',
      trigger_config: null,
      next_run_at: null,
      last_run_at: null,
      instruction: 'Run daily filing alert report.',
      workspace_id: 'ws-1',
      llm_model: null,
      thread_strategy: 'continue',
      conversation_thread_id: null,
      status: 'active',
      max_failures: 3,
      failure_count: 0,
      disable_reason: null,
      delivery_config: null,
      metadata: {
        keep_flag: true,
        report_profile: {
          profile: 'daily-filing-alert',
          report_targets: ['feishu:chat:oc_group_auth_1'],
          failure_targets: [],
        },
      },
      created_at: '2026-11-26T00:00:00Z',
      updated_at: '2026-11-26T00:00:00Z',
      last_execution: null,
    };
    vi.mocked(api.listAutomations).mockResolvedValue({
      data: { automations: [existingAuto], total: 1 },
      status: 200,
      statusText: 'OK',
      headers: {},
    });

    renderWithProviders(<Automations />, { route: '/automations?view=manage&id=auto-clear-1' });

    fireEvent.click(await screen.findByRole('button', { name: label('automation:edit') }));
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(label('automation:moreOptions')) }));
    fireEvent.click(await screen.findByRole('button', { name: label('automation:reportDeliveryClear') }));
    fireEvent.click(screen.getByRole('button', { name: label('automation:saveChanges') }));

    await waitFor(() => {
      expect(api.updateAutomation).toHaveBeenCalledTimes(1);
    });
    expect(confirmMocks.precheck).not.toHaveBeenCalled();
    expect(confirmMocks.issue).not.toHaveBeenCalled();
    const [updatedId, patchPayload] = vi.mocked(api.updateAutomation).mock.calls[0] ?? [];
    expect(updatedId).toBe('auto-clear-1');
    expect(patchPayload?.metadata).toEqual({
      keep_flag: true,
      report_profile: null,
    });
  });
});
