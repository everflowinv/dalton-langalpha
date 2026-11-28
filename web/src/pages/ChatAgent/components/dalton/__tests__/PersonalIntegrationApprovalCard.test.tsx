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

  it('displays original start/end time on delete_event confirmation card', () => {
    const data: ToolApprovalState = {
      toolName: 'dalton_calendar',
      server: '',
      tool: 'dalton_calendar',
      actionType: 'dalton_calendar_delete_event',
      approvalId: 'appr-del-1',
      payloadSha256: 'sha-del-1',
      riskLevel: 'L1',
      requiresSecondConfirm: false,
      actionIndex: 0,
      actionCount: 1,
      status: 'pending',
      reason: null,
      args: {
        provider: 'google',
        provider_label: 'Google 日历',
        calendar_id: 'primary',
        summary: 'Cancelled Sync',
        start_time: '2026-12-05T10:00:00+08:00',
        end_time: '2026-12-05T11:00:00+08:00',
        before: {
          summary: 'Cancelled Sync',
          start_time: '2026-12-05T10:00:00+08:00',
          end_time: '2026-12-05T11:00:00+08:00',
        },
      },
    };

    render(<PersonalIntegrationApprovalCard data={data} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(screen.getByText('确认删除个人日程')).toBeInTheDocument();
    expect(screen.getByText('Cancelled Sync')).toBeInTheDocument();
    expect(
      screen.getByText(/2026-12-05T10:00:00\+08:00 → 2026-12-05T11:00:00\+08:00/),
    ).toBeInTheDocument();
  });

  it('displays cleared description, removed reminders, and default reminder toggles on update_event card', () => {
    const baseArgs = {
      provider: 'google',
      provider_label: 'Google 日历',
      calendar_id: 'primary',
      summary: 'Weekly Review',
      start_time: '2026-12-06T09:00:00+08:00',
      end_time: '2026-12-06T10:00:00+08:00',
    };

    const { rerender } = render(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_update_event',
          approvalId: 'appr-upd-clear',
          payloadSha256: 'sha-upd-clear',
          riskLevel: 'L1',
          requiresSecondConfirm: false,
          actionIndex: 0,
          actionCount: 1,
          status: 'pending',
          reason: null,
          args: {
            ...baseArgs,
            description: '',
            description_set: true,
            location: '',
            location_set: true,
            reminders: [],
            reminders_set: true,
          },
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByTestId('calendar-approval-description')).toHaveTextContent('（清空描述）');
    expect(screen.getByTestId('calendar-approval-location')).toHaveTextContent('（清空地点）');
    expect(screen.getByTestId('calendar-approval-reminders')).toHaveTextContent('移除全部提醒');

    rerender(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_update_event',
          approvalId: 'appr-upd-default-on',
          payloadSha256: 'sha-upd-default-on',
          riskLevel: 'L1',
          requiresSecondConfirm: false,
          actionIndex: 0,
          actionCount: 1,
          status: 'pending',
          reason: null,
          args: {
            ...baseArgs,
            use_default_reminders: true,
          },
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByTestId('calendar-approval-reminders')).toHaveTextContent('使用日历默认提醒');

    rerender(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_update_event',
          approvalId: 'appr-upd-default-off',
          payloadSha256: 'sha-upd-default-off',
          riskLevel: 'L1',
          requiresSecondConfirm: false,
          actionIndex: 0,
          actionCount: 1,
          status: 'pending',
          reason: null,
          args: {
            ...baseArgs,
            use_default_reminders: false,
          },
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByTestId('calendar-approval-reminders')).toHaveTextContent('关闭默认提醒（不提醒）');
  });

  it('displays actual attendee changes (including clearing all attendees) separately from L2 notification recipients', () => {
    const { rerender } = render(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_update_event',
          approvalId: 'appr-upd-clear-att',
          payloadSha256: 'sha-upd-clear-att',
          riskLevel: 'L2',
          requiresSecondConfirm: true,
          actionIndex: 0,
          actionCount: 1,
          status: 'pending',
          reason: null,
          args: {
            provider: 'google',
            provider_label: 'Google 日历',
            calendar_id: 'primary',
            summary: 'Team Sync',
            attendees: [],
            attendees_set: true,
            attendees_added: [],
            attendees_removed: ['bob@example.com', 'carol@example.com'],
            attendees_cleared: true,
            before_attendees: ['bob@example.com', 'carol@example.com'],
            external_attendees: ['bob@example.com', 'carol@example.com'],
            before: {
              summary: 'Team Sync',
              attendees: [{ email: 'bob@example.com' }, { email: 'carol@example.com' }],
            },
            normalized_payload: {
              calendarId: 'primary',
              eventId: 'ev-1',
              attendees: [],
            },
          },
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    const attendeesBlock = screen.getByTestId('calendar-approval-attendees');
    expect(attendeesBlock).toHaveTextContent('清空全部参会人');
    expect(attendeesBlock).toHaveTextContent('bob@example.com、carol@example.com');
    expect(screen.getByTestId('l2-attendees-warning')).toHaveTextContent(
      'bob@example.com、carol@example.com',
    );

    rerender(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_update_event',
          approvalId: 'appr-upd-diff-att',
          payloadSha256: 'sha-upd-diff-att',
          riskLevel: 'L2',
          requiresSecondConfirm: true,
          actionIndex: 0,
          actionCount: 1,
          status: 'pending',
          reason: null,
          args: {
            provider: 'google',
            provider_label: 'Google 日历',
            calendar_id: 'primary',
            summary: 'Team Sync',
            attendees: ['bob@example.com', 'dave@example.com'],
            attendees_set: true,
            attendees_added: ['dave@example.com'],
            attendees_removed: ['carol@example.com'],
            attendees_cleared: false,
            before_attendees: ['bob@example.com', 'carol@example.com'],
            external_attendees: ['bob@example.com', 'carol@example.com', 'dave@example.com'],
          },
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    const diffBlock = screen.getByTestId('calendar-approval-attendees');
    expect(diffBlock).toHaveTextContent('新增参会人：dave@example.com');
    expect(diffBlock).toHaveTextContent('移除参会人：carol@example.com');
    expect(diffBlock).toHaveTextContent('变更后参会人：bob@example.com、dave@example.com');
  });

  it('prompts user to verify in calendar when approved write returns write_result_unconfirmed', () => {
    render(
      <PersonalIntegrationApprovalCard
        data={{
          toolName: 'dalton_calendar',
          server: '',
          tool: 'dalton_calendar',
          actionType: 'dalton_calendar_create_event',
          approvalId: 'appr-unconfirmed',
          payloadSha256: 'sha-unconfirmed',
          riskLevel: 'L1',
          requiresSecondConfirm: false,
          actionIndex: 0,
          actionCount: 1,
          status: 'approved',
          reason: null,
          args: {
            provider: 'google',
            provider_label: 'Google 日历',
            calendar_id: 'primary',
            summary: 'Board Dinner',
          },
        }}
        toolResultContent={JSON.stringify({
          ok: false,
          error: 'write_result_unconfirmed',
          message: '新建日程请求已发出但未能核实是否生效，请前往你的日历中确认后再决定是否重试。',
        })}
      />,
    );

    expect(screen.getByText('写入结果待确认：确认新建个人日程')).toBeInTheDocument();
    const notice = screen.getByTestId('calendar-write-unconfirmed-notice');
    expect(notice).toHaveTextContent('请前往你的日历中确认');
  });
});
