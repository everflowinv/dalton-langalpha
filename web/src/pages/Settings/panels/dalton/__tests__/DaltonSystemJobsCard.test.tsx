import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { DaltonSystemJobsCard } from '../DaltonSystemJobsCard';
import {
  fetchDaltonSystemJobs,
  toggleDaltonSystemJob,
  runDaltonSystemJobNow,
} from '../../../utils/daltonApi';

vi.mock('../../../utils/daltonApi', () => ({
  fetchDaltonSystemJobs: vi.fn(),
  toggleDaltonSystemJob: vi.fn(),
  runDaltonSystemJobNow: vi.fn(),
}));

const mockFetchJobs = fetchDaltonSystemJobs as Mock;
const mockToggleJob = toggleDaltonSystemJob as Mock;
const mockRunNow = runDaltonSystemJobNow as Mock;

describe('DaltonSystemJobsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchJobs.mockResolvedValue({
      ok: true,
      is_pm: true,
      jobs: [
        {
          key: 'conversation-archive',
          kind: 'conversation_archive',
          scope: 'host',
          title: '团队对话归档',
          status: 'on',
          enabled: true,
          last_run_at: null,
          last_result: null,
          next_run_at: null,
          can_toggle: true,
          can_run_now: true,
        },
      ],
    });
  });

  it('shows Chinese error banner and never shows success when toggle fails with HTTP 500 or ok:false', async () => {
    mockToggleJob.mockRejectedValueOnce({
      response: {
        status: 500,
        data: { detail: '切换团队对话归档状态失败: 写入宿主机归档开关状态文件失败' },
      },
    });

    renderWithProviders(<DaltonSystemJobsCard />);

    const toggleBtn = await screen.findByLabelText('切换 团队对话归档');
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(
        screen.getByText('切换团队对话归档状态失败: 写入宿主机归档开关状态文件失败'),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/已关闭|已开启/)).not.toBeInTheDocument();

    // Also when response resolves with ok: false -> shows error, not success
    mockToggleJob.mockResolvedValueOnce({
      ok: false,
      error: '切换团队对话归档状态失败: 宿主机桥接异常',
    });
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(screen.getByText('切换团队对话归档状态失败: 宿主机桥接异常')).toBeInTheDocument();
    });
    expect(screen.queryByText(/已关闭|已开启/)).not.toBeInTheDocument();

    // Only when ok === true does it show "已关闭"
    mockToggleJob.mockResolvedValueOnce({ ok: true });
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(screen.getByText('团队对话归档 已关闭')).toBeInTheDocument();
    });
  });

  it('shows error banner when runNow fails with HTTP 500 or ok:false', async () => {
    mockRunNow.mockRejectedValueOnce({
      response: {
        status: 500,
        data: { detail: '启动团队对话归档失败: 写入团队对话归档 PID 文件失败' },
      },
    });

    renderWithProviders(<DaltonSystemJobsCard />);

    const runBtn = await screen.findByTestId('system-job-run-conversation-archive');
    fireEvent.click(runBtn);

    await waitFor(() => {
      expect(
        screen.getByText('启动团队对话归档失败: 写入团队对话归档 PID 文件失败'),
      ).toBeInTheDocument();
    });
  });

  it('filters rendered scope groups and subtitle according to the scopes prop', async () => {
    renderWithProviders(<DaltonSystemJobsCard scopes={['company', 'home']} />);

    expect(await screen.findByTestId('system-jobs-group-company')).toBeInTheDocument();
    expect(screen.getByTestId('system-jobs-group-home')).toBeInTheDocument();
    expect(screen.queryByTestId('system-jobs-group-host')).not.toBeInTheDocument();
    expect(screen.queryByTestId('system-jobs-group-ops')).not.toBeInTheDocument();
    expect(
      screen.getByText('统一查看、开关与立即触发系统内所有自动运行任务（公司跟踪、Home 全局）'),
    ).toBeInTheDocument();
  });

  it('hides log_tail when exit_code is 0 or 2, shows it when exit_code is 3, and formats daily schedule from next_run_at in local time', async () => {
    const nextRun = '2026-10-26T01:15:00Z';
    const dt = new Date(nextRun);
    const expectedHh = String(dt.getHours()).padStart(2, '0');
    const expectedMm = String(dt.getMinutes()).padStart(2, '0');

    mockFetchJobs.mockResolvedValueOnce({
      ok: true,
      is_pm: true,
      jobs: [
        {
          key: 'conversation-archive',
          kind: 'conversation_archive',
          scope: 'host',
          title: '团队对话归档',
          schedule: null,
          status: 'on',
          enabled: true,
          last_run_at: '2026-10-25T01:15:00Z',
          last_result: '成功 (exit 0)',
          next_run_at: nextRun,
          exit_code: 0,
          log_tail: '=== 2026-10-25 ===\nRuntimeError: cancel scope\n=== exit 0 ===',
          can_toggle: true,
          can_run_now: true,
        },
        {
          key: 'conversation-archive-skipped',
          kind: 'conversation_archive',
          scope: 'host',
          title: '团队对话归档（跳过）',
          schedule: null,
          status: 'on',
          enabled: true,
          last_run_at: '2026-10-25T01:15:00Z',
          last_result: '已在运行，跳过',
          next_run_at: null,
          exit_code: 2,
          log_tail: '=== 2026-10-25 ===\nalready running\n=== exit 2 ===',
          can_toggle: true,
          can_run_now: true,
        },
        {
          key: 'conversation-archive-partial',
          kind: 'conversation_archive',
          scope: 'host',
          title: '团队对话归档（部分未完成）',
          schedule: null,
          status: 'on',
          enabled: true,
          last_run_at: '2026-10-25T01:15:00Z',
          last_result: '部分未完成 (exit_code=3)',
          next_run_at: null,
          exit_code: 3,
          log_tail: '=== 2026-10-25 ===\nhourly budget reached\n=== exit 3 ===',
          can_toggle: true,
          can_run_now: true,
        },
      ],
    });

    renderWithProviders(<DaltonSystemJobsCard />);

    expect(
      await screen.findByText(`(每日 ${expectedHh}:${expectedMm})`),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('system-job-log-toggle-conversation-archive'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('system-job-log-toggle-conversation-archive-skipped'),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId('system-job-log-toggle-conversation-archive-partial'),
    ).toBeInTheDocument();
  });

  it('shows collapsed log toggle when last run failed and expands log on click', async () => {
    mockFetchJobs.mockResolvedValueOnce({
      ok: true,
      is_pm: true,
      jobs: [
        {
          key: 'conversation-archive',
          kind: 'conversation_archive',
          scope: 'host',
          title: '团队对话归档',
          schedule: null,
          status: 'on',
          enabled: true,
          last_run_at: '2026-10-25T01:15:00Z',
          last_result: '失败 (exit 1)',
          next_run_at: '2026-10-26T01:15:00Z',
          exit_code: 1,
          log_tail: '=== 2026-10-25 ===\nError: something went wrong\n=== exit 1 ===',
          can_toggle: true,
          can_run_now: true,
        },
      ],
    });

    renderWithProviders(<DaltonSystemJobsCard />);

    const toggleBtn = await screen.findByTestId('system-job-log-toggle-conversation-archive');
    expect(toggleBtn).toHaveTextContent('查看日志');
    expect(
      screen.queryByTestId('system-job-log-conversation-archive'),
    ).not.toBeInTheDocument();

    fireEvent.click(toggleBtn);
    expect(toggleBtn).toHaveTextContent('收起日志');
    const pre = screen.getByTestId('system-job-log-conversation-archive');
    expect(pre).toHaveTextContent('Error: something went wrong');
  });
});
