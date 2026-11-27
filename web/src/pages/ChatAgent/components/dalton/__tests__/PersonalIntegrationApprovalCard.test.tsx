import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { describe, expect, it, vi } from 'vitest';
import type { ToolApprovalState } from '@/types/chat';
import { PersonalIntegrationApprovalCard } from '../PersonalIntegrationApprovalCard';

describe('PersonalIntegrationApprovalCard', () => {
  it('renders normalized description, time bounds, and before event details on the confirmation card', () => {
    const data: ToolApprovalState = {
      toolName: 'dalton_calendar',
      server: '',
      tool: 'dalton_calendar',
      actionType: 'dalton_calendar_update_event',
      approvalId: 'appr-1',
      payloadSha256: 'sha-1',
      riskLevel: 'L1',
      requiresSecondConfirm: false,
      actionIndex: 0,
      actionCount: 1,
      status: 'pending',
      reason: null,
      args: {
        provider: 'feishu',
        provider_label: '飞书日历',
        calendar_id: 'primary',
        summary: 'Q4 Strategy Review',
        start_time: '2026-12-04T14:30:00+08:00',
        end_time: '2026-12-04T15:00:00+08:00',
        timezone: 'Asia/Shanghai',
        description: 'Review final Q4 guidance and deck',
        before: {
          summary: 'Q4 Draft Review',
          description: 'Initial notes',
        },
      },
    };

    render(<PersonalIntegrationApprovalCard data={data} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(screen.getByText('Q4 Strategy Review')).toBeInTheDocument();
    expect(screen.getByTestId('calendar-approval-description')).toHaveTextContent(
      'Review final Q4 guidance and deck',
    );
    expect(screen.getByText(/2026-12-04T14:30:00\+08:00 → 2026-12-04T15:00:00\+08:00/)).toBeInTheDocument();
    expect(screen.getByText(/原日程描述：Initial notes/)).toBeInTheDocument();
  });
});
