import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import type {
  Automation,
  AutomationExecution,
  AutomationRun,
  ExecutionDeliverySummary,
} from '@/types/automation';
import * as api from '../utils/api';
import { resolveExecutionDelivery } from '../utils/delivery';
import { DELIVERY_SETTLE_MS, isDeliverySettling } from '../utils/polling';
import { describeRun } from '../utils/status';
import Automations from '../Automations';

const prefMocks = vi.hoisted(() => ({
  preferences: {
    model_preference: {} as Record<string, unknown>,
    other_preference: {} as Record<string, unknown>,
  },
  mutateAsync: vi.fn(),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: prefMocks.preferences,
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({
    mutate: vi.fn(),
    mutateAsync: prefMocks.mutateAsync,
  }),
}));

vi.mock('@/hooks/useHomeTimezone', () => ({ useHomeTimezone: () => 'Asia/Shanghai' }));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws-1', name: 'Research' }] } }),
}));

vi.mock('@/contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/contexts/AuthContext')>();
  return { ...actual, useAuth: () => ({ userId: 'user-richard', isLoggedIn: true, isInitialized: true }) };
});

vi.mock('../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/api')>();
  const { personalReadMocks } = await import('./readBridge');
  const listAutomations = vi.fn(async (_params: { limit?: number; offset?: number }) => ({
    data: { automations: new Array<Automation>() },
  }));
  return {
    ...actual,
    listAutomations,
    listExecutions: vi.fn(),
    listRecentRuns: vi.fn(),
    getDeliveryOptions: vi.fn(),
    ...personalReadMocks(listAutomations),
  };
});

afterEach(async () => {
  vi.clearAllMocks();
  localStorage.clear();
  await i18n.changeLanguage('en-US');
});

function makeExecution(
  id: string,
  day: number,
  summary: ExecutionDeliverySummary | null,
): AutomationExecution {
  const at = `2026-11-${day}T13:10:00Z`;
  return {
    automation_execution_id: id,
    automation_id: 'auto-market-pm',
    status: 'completed',
    conversation_thread_id: null,
    scheduled_at: at,
    started_at: at,
    completed_at: `2026-11-${day}T13:13:00Z`,
    error_message: null,
    skip_reason: null,
    failure_reason: null,
    delivery_result: null,
    delivery_summary: summary,
    created_at: at,
    excerpt: `Excerpt ${id}`,
    dismissed_at: null,
  };
}

const RUN_DELIVERED = makeExecution('run-delivered', 20, {
  status: 'delivered',
  total_targets: 1,
  sent_count: 1,
  failed_count: 0,
  retrying_count: 0,
  failure_code: null,
  failure_reason: null,
});

const RUN_ALL_FAILED = makeExecution('782f546a', 19, {
  status: 'failed',
  total_targets: 1,
  sent_count: 0,
  failed_count: 1,
  retrying_count: 0,
  failure_code: 'card_limit_exceeded',
  failure_reason: '卡片超过 30 KB 上限',
});

const RUN_PARTIAL_FAILED = makeExecution('run-partial', 18, {
  status: 'partial_failed',
  total_targets: 2,
  sent_count: 1,
  failed_count: 1,
  retrying_count: 0,
  failure_code: 'target_failed',
  failure_reason: '目标通道投递失败',
});

const RUN_RETRYING = makeExecution('run-retrying', 17, {
  status: 'retrying',
  total_targets: 1,
  sent_count: 0,
  failed_count: 0,
  retrying_count: 1,
  failure_code: null,
  failure_reason: null,
});

const RUN_NO_DELIVERY = makeExecution('run-none', 16, null);

const FIVE_RUNS = [RUN_DELIVERED, RUN_ALL_FAILED, RUN_PARTIAL_FAILED, RUN_RETRYING, RUN_NO_DELIVERY];

const AUTOMATION: Automation = {
  automation_id: 'auto-market-pm',
  user_id: 'user-richard',
  name: '市场晚报',
  description: null,
  trigger_type: 'cron',
  cron_expression: '10 21 * * 1-5',
  timezone: 'Asia/Shanghai',
  trigger_config: null,
  next_run_at: '2026-11-21T13:10:00Z',
  last_run_at: RUN_ALL_FAILED.completed_at,
  instruction: 'Generate market close digest.',
  workspace_id: 'ws-1',
  llm_model: null,
  thread_strategy: 'new',
  conversation_thread_id: null,
  status: 'active',
  max_failures: 3,
  failure_count: 0,
  delivery_config: null,
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
  last_execution: RUN_ALL_FAILED,
};

const asFeed = (e: AutomationExecution): AutomationRun => ({
  ...e,
  automation_name: AUTOMATION.name,
  trigger_type: 'cron',
  workspace_id: 'ws-1',
});

function renderPage(route: string, latestRun: AutomationExecution = RUN_ALL_FAILED) {
  const auto = { ...AUTOMATION, last_execution: latestRun };
  vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [auto], total: 1 } } as never);
  vi.mocked(api.listExecutions).mockResolvedValue({ data: { executions: FIVE_RUNS, has_more: false } } as never);
  vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: FIVE_RUNS.map(asFeed), has_more: false } } as never);
  vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: 'ws-1' } } as never);
  renderWithProviders(<Automations />, { route });
}

describe('describeRun & polling across all five delivery states', () => {
  it('classifies delivered, failed, partial_failed, retrying, and none without altering status', () => {
    expect(RUN_ALL_FAILED.status).toBe('completed');

    expect(describeRun(RUN_DELIVERED).ui.labelKey).toBe('automation.runCompleted');
    expect(describeRun(RUN_DELIVERED).deliveryStatus).toBe('delivered');
    expect(isDeliverySettling(RUN_DELIVERED, true)).toBe(false);

    expect(describeRun(RUN_ALL_FAILED).ui.labelKey).toBe('automation.runCompletedDeliveryFailed');
    expect(describeRun(RUN_ALL_FAILED).ui.danger).toBe(true);
    expect(isDeliverySettling(RUN_ALL_FAILED, true)).toBe(false);

    expect(describeRun(RUN_PARTIAL_FAILED).ui.labelKey).toBe('automation.runCompletedPartialDeliveryFailed');
    expect(describeRun(RUN_PARTIAL_FAILED).ui.danger).toBe(true);
    expect(isDeliverySettling(RUN_PARTIAL_FAILED, true)).toBe(false);

    expect(describeRun(RUN_RETRYING).ui.labelKey).toBe('automation.runCompletedDelivering');
    expect(describeRun(RUN_RETRYING).ui.danger).toBeFalsy();
    const retryingAt = Date.parse(RUN_RETRYING.completed_at!);
    expect(isDeliverySettling(RUN_RETRYING, false, retryingAt + 30_000)).toBe(true);
    // Carbon High #2: retrying stops fast 5s polling once settle window expires
    expect(isDeliverySettling(RUN_RETRYING, false, retryingAt + DELIVERY_SETTLE_MS + 1_000)).toBe(false);

    // Codex Med #3: persisted retrying=false is immediately failed even with unconfirmed text prefix
    const refusedRun: AutomationExecution = {
      ...makeExecution('run-refused-404', 20, null),
      delivery_result: [
        {
          method: 'slack:T1/C1',
          address: 'slack:T1/C1',
          name: '#desk',
          success: false,
          retrying: false,
          error: "Delivery couldn't be confirmed. The messaging service has no record of this run.",
        },
      ],
    };
    const refusedAt = Date.parse(refusedRun.completed_at!);
    expect(resolveExecutionDelivery(refusedRun, refusedAt + 10_000)?.status).toBe('failed');
    expect(isDeliverySettling(refusedRun, true, refusedAt + 10_000)).toBe(false);

    expect(describeRun(RUN_NO_DELIVERY).ui.labelKey).toBe('automation.runCompleted');
    expect(describeRun(RUN_NO_DELIVERY).deliveryStatus).toBeNull();
  });
});

describe('execution history list and detail in Chinese (zh-CN)', () => {
  it('renders all five delivery states in history table and failure reason in detail card', async () => {
    await i18n.changeLanguage('zh-CN');
    renderPage('/automations?id=auto-market-pm', RUN_ALL_FAILED);

    const heading = await screen.findByRole('heading', { level: 2, name: '市场晚报' });
    const pane = heading.closest('article')!;
    const table = await within(pane).findByRole('table');

    // Detail card (latest run = all failed)
    const reportCard = pane.querySelector('.automation-report') as HTMLElement;
    expect(reportCard).toHaveTextContent('已完成 · 投递失败');
    expect(reportCard).toHaveTextContent('投递失败：卡片超过 30 KB 上限');

    const rowForDay = (day: string) => {
      const row = within(table).getByRole('button', { name: new RegExp(day) }).closest('tr')!;
      return { row, next: row.nextElementSibling as HTMLElement | null };
    };

    // 1. Delivered
    const delivered = rowForDay('20');
    expect(delivered.row).toHaveTextContent('已完成');
    expect(delivered.row).not.toHaveTextContent('投递失败');
    expect(delivered.row).toHaveTextContent('已投递');

    // 2. All failed (30 KB card limit)
    const allFailed = rowForDay('19');
    expect(allFailed.row).toHaveTextContent('已完成 · 投递失败');
    expect(allFailed.row).toHaveClass('has-error');
    expect(allFailed.next).toHaveTextContent('卡片超过 30 KB 上限');

    // 3. Partial failed
    const partial = rowForDay('18');
    expect(partial.row).toHaveTextContent('已完成 · 部分投递失败');
    expect(partial.row).toHaveClass('has-error');
    expect(partial.next).toHaveTextContent('目标通道投递失败');

    // 4. Retrying
    const retrying = rowForDay('17');
    expect(retrying.row).toHaveTextContent('已完成 · 投递中');
    expect(retrying.row).not.toHaveClass('has-error');

    // 5. No delivery configured
    const none = rowForDay('16');
    expect(none.row).toHaveTextContent('已完成');
    expect(none.row.querySelector('.automation-history-delivery')?.textContent).toBe('');
  });

  it('renders partial failure and retrying notes in detail card and feed (zh-CN)', async () => {
    await i18n.changeLanguage('zh-CN');
    renderPage('/automations?view=feed');

    const failedEntry = (await screen.findByText('Excerpt 782f546a')).closest('article')!;
    expect(failedEntry).toHaveTextContent('已完成 · 投递失败');
    expect(failedEntry).toHaveTextContent('投递失败：卡片超过 30 KB 上限');

    const partialEntry = screen.getByText('Excerpt run-partial').closest('article')!;
    expect(partialEntry).toHaveTextContent('已完成 · 部分投递失败');
    expect(partialEntry).toHaveTextContent('部分投递失败（1/2 个目标失败）：目标通道投递失败');

    const retryingEntry = screen.getByText('Excerpt run-retrying').closest('article')!;
    expect(retryingEntry).toHaveTextContent('已完成 · 投递中');
    expect(retryingEntry).toHaveTextContent('投递中，正在重试发送。');

    const plainEntry = screen.getByText('Excerpt run-none').closest('article')!;
    expect(plainEntry).not.toHaveTextContent('投递失败');
    expect(plainEntry).not.toHaveTextContent('投递中');
  });
});

describe('execution history list and detail in English (en-US)', () => {
  it('renders localized English copy for all five delivery states', async () => {
    await i18n.changeLanguage('en-US');
    renderPage('/automations?view=feed');

    const failedEntry = (await screen.findByText('Excerpt 782f546a')).closest('article')!;
    expect(failedEntry).toHaveTextContent('Completed · Delivery failed');
    expect(failedEntry).toHaveTextContent('Delivery failed: Card exceeds 30 KB limit');

    const partialEntry = screen.getByText('Excerpt run-partial').closest('article')!;
    expect(partialEntry).toHaveTextContent('Completed · Partial delivery failed');
    expect(partialEntry).toHaveTextContent('Partial delivery failed (1/2 targets failed): Target channel delivery failed');

    const retryingEntry = screen.getByText('Excerpt run-retrying').closest('article')!;
    expect(retryingEntry).toHaveTextContent('Completed · Delivering');
    expect(retryingEntry).toHaveTextContent('Delivery in progress; retrying.');

    const deliveredEntry = screen.getByText('Excerpt run-delivered').closest('article')!;
    expect(deliveredEntry).not.toHaveTextContent('Delivery failed');
    expect(deliveredEntry).not.toHaveTextContent('Delivering');
  });
});

describe('round-2 review regressions in inspector history and detail', () => {
  it('prefers delivery_summary targets & reasons over mirrored not_allowed delivery_result and omits error icon on retrying targets', async () => {
    await i18n.changeLanguage('zh-CN');
    const mirroredFailed: AutomationExecution = {
      ...makeExecution('782f546a', 19, {
        status: 'failed',
        total_targets: 1,
        sent_count: 0,
        failed_count: 1,
        retrying_count: 0,
        failure_code: 'card_limit_exceeded',
        failure_reason: '卡片超过 30 KB 上限',
        targets: [
          {
            target: 'feishu:chat:oc_market_pm',
            channel: 'feishu',
            name: '投研大群',
            status: 'failed',
            error_code: 'card_limit_exceeded',
            error_message: '卡片超过 30 KB 上限',
          },
        ],
      }),
      excerpt: '晚报已生成\n\n投递状态：已生成但未送达（not_allowed）',
      delivery_result: [
        {
          method: 'feishu',
          address: 'feishu:chat:oc_market_pm',
          success: false,
          error: '已生成但未送达（not_allowed）',
        },
      ],
    };

    const retryingWithFalseAttempt: AutomationExecution = {
      ...makeExecution('run-retrying-multi', 18, {
        status: 'retrying',
        total_targets: 2,
        sent_count: 1,
        failed_count: 0,
        retrying_count: 1,
        failure_code: null,
        failure_reason: null,
        targets: [
          {
            target: 'feishu:user:ou_ok',
            channel: 'feishu',
            name: 'Richard',
            status: 'sent',
          },
          {
            target: 'feishu:chat:oc_retry',
            channel: 'feishu',
            name: '交易群',
            status: 'retrying',
          },
        ],
      }),
      delivery_result: [
        { method: 'feishu', address: 'feishu:user:ou_ok', name: 'Richard', success: true },
        {
          method: 'feishu',
          address: 'feishu:chat:oc_retry',
          name: '交易群',
          success: false,
          retrying: true,
          error: "Delivery couldn't be confirmed. Gateway timeout.",
        },
      ],
    };

    const auto = { ...AUTOMATION, last_execution: mirroredFailed };
    vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [auto], total: 1 } } as never);
    vi.mocked(api.listExecutions).mockResolvedValue({
      data: { executions: [mirroredFailed, retryingWithFalseAttempt], has_more: false },
    } as never);
    vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
    vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: 'ws-1' } } as never);
    renderWithProviders(<Automations />, { route: '/automations?id=auto-market-pm' });

    const heading = await screen.findByRole('heading', { level: 2, name: '市场晚报' });
    const pane = heading.closest('article')!;
    const table = await within(pane).findByRole('table');

    // Carbon Med #6: detail RunReport strips fallback synced suffix from excerpt
    const reportCard = pane.querySelector('.automation-report') as HTMLElement;
    expect(reportCard).toHaveTextContent('晚报已生成');
    expect(reportCard).toHaveTextContent('投递失败：卡片超过 30 KB 上限');
    expect(reportCard).not.toHaveTextContent('已生成但未送达');
    expect(reportCard).not.toHaveTextContent('not_allowed');

    // Carbon High #1 / Codex Med #4: history table shows 30 KB reason from delivery_summary, NOT 投递被拒绝
    const failedRow = within(table).getByRole('button', { name: /19/ }).closest('tr')!;
    const failedErrRow = failedRow.nextElementSibling as HTMLElement;
    expect(failedRow).toHaveClass('has-error');
    expect(failedErrRow).toHaveTextContent('投研大群：卡片超过 30 KB 上限');
    expect(failedErrRow).not.toHaveTextContent('投递被拒绝');

    // Codex Med #4: retrying target has NO failure icon and NO error row even though delivery_result has success: false
    const retryingRow = within(table).getByRole('button', { name: /18/ }).closest('tr')!;
    expect(retryingRow).toHaveTextContent('已完成 · 投递中');
    expect(retryingRow).toHaveTextContent('交易群 投递中');
    expect(retryingRow).not.toHaveClass('has-error');
    expect(within(retryingRow).queryByLabelText('失败')).not.toBeInTheDocument();
    expect(within(retryingRow).getByLabelText('投递中')).toBeInTheDocument();
  });

  it('keeps detail card in sync with polled execution history by execution id when last_execution is stale', async () => {
    await i18n.changeLanguage('zh-CN');
    const staleLast = makeExecution('exec-same-id', 20, {
      status: 'retrying',
      total_targets: 1,
      sent_count: 0,
      failed_count: 0,
      retrying_count: 1,
      failure_code: null,
      failure_reason: null,
    });
    const polledTerminal = makeExecution('exec-same-id', 20, {
      status: 'failed',
      total_targets: 1,
      sent_count: 0,
      failed_count: 1,
      retrying_count: 0,
      failure_code: 'card_limit_exceeded',
      failure_reason: '卡片超过 30 KB 上限',
    });

    const auto = { ...AUTOMATION, last_execution: staleLast };
    vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [auto], total: 1 } } as never);
    vi.mocked(api.listExecutions).mockResolvedValue({
      data: { executions: [polledTerminal], has_more: false },
    } as never);
    vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
    vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: 'ws-1' } } as never);
    renderWithProviders(<Automations />, { route: '/automations?id=auto-market-pm' });

    const heading = await screen.findByRole('heading', { level: 2, name: '市场晚报' });
    const pane = heading.closest('article')!;
    await within(pane).findByRole('table');

    const reportCard = pane.querySelector('.automation-report') as HTMLElement;
    expect(reportCard).toHaveTextContent('已完成 · 投递失败');
    expect(reportCard).toHaveTextContent('投递失败：卡片超过 30 KB 上限');
    expect(reportCard).not.toHaveTextContent('已完成 · 投递中');
  });
});
