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
});
