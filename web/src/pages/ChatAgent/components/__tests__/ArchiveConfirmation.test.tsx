import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

import { renderWithProviders } from '@/test/utils';
import SecretaryConfirmCard from '../SecretaryConfirmCard';
import {
  settleLiveProposalFromResult,
  settleProposalFromResult,
} from '../../session/interrupts/buckets';
import type { ChatMessage } from '@/types/chat';

const COMPUTER_ID = '5319ad0e-7835-4ca6-9541-b7c2961a7bf6';
const WORKSPACE_ID = 'aecc944a-61b4-4e68-8a1d-28e1b1823001';

const mockGetSandboxStats = vi.fn();
const mockGetWorkspace = vi.fn();
const mockGetComputers = vi.fn();
const mockGetWorkspaceArchivePreview = vi.fn();
const mockArchiveWorkspaceWithSnapshot = vi.fn();

vi.mock('../../utils/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    getSandboxStats: (...a: unknown[]) => mockGetSandboxStats(...a),
    getWorkspace: (...a: unknown[]) => mockGetWorkspace(...a),
    getComputers: (...a: unknown[]) => mockGetComputers(...a),
    getWorkspaceArchivePreview: (...a: unknown[]) => mockGetWorkspaceArchivePreview(...a),
    archiveWorkspaceWithSnapshot: (...a: unknown[]) => mockArchiveWorkspaceWithSnapshot(...a),
    installSandboxPackages: vi.fn(),
    refreshWorkspace: vi.fn(),
  };
});

import { SandboxSettingsContent } from '../SandboxSettingsPanel';

describe('OverviewTab whole-computer archive confirmation dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetWorkspace.mockResolvedValue({
      workspace_id: WORKSPACE_ID,
      name: 'NVDA',
      status: 'stopped',
      computer_id: COMPUTER_ID,
      dir_name: 'NVDA',
    });
    mockGetComputers.mockResolvedValue({
      computers: [{
        computer_id: COMPUTER_ID,
        user_id: 'u1',
        kind: 'daytona',
        name: 'Daytona Box',
        status: 'stopped',
        resource_tier: 'standard',
        is_always_on: false,
        is_primary: true,
        root_dir: '/home/workspace',
      }],
      total: 1,
    });
  });

  it('hides Archive button when archivable is false (Docker / E2B / SSH)', async () => {
    mockGetSandboxStats.mockResolvedValue({
      state: 'stopped',
      sandbox_id: 'sbx-docker',
      provider: 'docker',
      archivable: false,
      resources: {},
    });
    renderWithProviders(<SandboxSettingsContent workspaceId={WORKSPACE_ID} />);
    await screen.findByRole('button', { name: /start/i });
    expect(screen.queryByRole('button', { name: /archive/i })).not.toBeInTheDocument();
  });

  it('opens confirmation dialog with snapshot, submits, and refreshes on 409 scope_changed', async () => {
    const user = userEvent.setup();
    mockGetSandboxStats.mockResolvedValue({
      state: 'stopped',
      sandbox_id: 'sbx-daytona',
      provider: 'daytona',
      archivable: true,
      resources: {},
    });
    const firstSnapshot = {
      computer_id: COMPUTER_ID,
      computer_name: 'Daytona Box',
      computer_kind: 'daytona',
      computer_status: 'stopped',
      archivable: true,
      expected_workspaces: [
        { workspace_id: WORKSPACE_ID, name: 'NVDA', dir_name: 'NVDA', status: 'stopped', is_stand_in: false },
        { workspace_id: 'ws-2', name: 'AAPL', dir_name: 'AAPL', status: 'stopped', is_stand_in: false },
        { workspace_id: 'ws-si', name: 'StandIn', dir_name: 'StandIn', status: 'stopped', is_stand_in: true },
      ],
    };
    const refreshedSnapshot = {
      ...firstSnapshot,
      expected_workspaces: [
        ...firstSnapshot.expected_workspaces,
        { workspace_id: 'ws-3', name: 'MSFT', dir_name: 'MSFT', status: 'stopped', is_stand_in: false },
      ],
    };
    mockGetWorkspaceArchivePreview
      .mockResolvedValueOnce(firstSnapshot)
      .mockResolvedValueOnce(refreshedSnapshot);

    const conflictErr = {
      response: {
        status: 409,
        data: { detail: { code: 'scope_changed', message: '情况已变化，请重新发起' } },
      },
    };
    mockArchiveWorkspaceWithSnapshot
      .mockRejectedValueOnce(conflictErr)
      .mockResolvedValueOnce({ workspace_id: WORKSPACE_ID, status: 'stopped', message: 'ok' });

    renderWithProviders(<SandboxSettingsContent workspaceId={WORKSPACE_ID} />);
    await user.click(await screen.findByRole('button', { name: /archive/i }));

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('NVDA')).toBeInTheDocument();
    expect(within(dialog).getByText('AAPL')).toBeInTheDocument();
    expect(within(dialog).queryByText('StandIn')).not.toBeInTheDocument();

    // First confirm hits 409 scope_changed -> shows message and refreshes preview
    await user.click(within(dialog).getByRole('button', { name: /confirm archive/i }));
    expect(await within(dialog).findByText('情况已变化，请重新发起')).toBeInTheDocument();
    expect(await within(dialog).findByText('MSFT')).toBeInTheDocument();
    expect(mockArchiveWorkspaceWithSnapshot).toHaveBeenCalledWith(WORKSPACE_ID, {
      expected_computer_id: COMPUTER_ID,
      expected_workspaces: firstSnapshot.expected_workspaces,
    });

    // Second confirm succeeds with refreshed snapshot
    await user.click(within(dialog).getByRole('button', { name: /confirm archive/i }));
    await waitFor(() =>
      expect(mockArchiveWorkspaceWithSnapshot).toHaveBeenLastCalledWith(WORKSPACE_ID, {
        expected_computer_id: COMPUTER_ID,
        expected_workspaces: refreshedSnapshot.expected_workspaces,
      }),
    );
  });
});

describe('SecretaryConfirmCard archive_workspace 4-state card and settlement', () => {
  it('renders whole-computer list with >5 expand toggle and 4 settled states', async () => {
    const user = userEvent.setup();
    const workspaces = Array.from({ length: 6 }, (_, i) => ({
      workspace_id: `ws-${i + 1}`,
      name: `Workspace ${i + 1}`,
      dir_name: `Workspace ${i + 1}`,
      status: 'stopped',
      is_stand_in: false,
    }));

    const onApprove = vi.fn();
    const onReject = vi.fn();
    const { rerender } = renderWithProviders(
      <SecretaryConfirmCard
        proposalData={{
          actionType: 'archive_workspace',
          workspace_id: 'ws-1',
          workspace_name: 'Workspace 1',
          computer_id: COMPUTER_ID,
          computer_name: 'Daytona Box',
          expected_workspaces: workspaces,
          tool_call_id: 'tc-arch-1',
          status: 'pending',
        }}
        onApprove={onApprove}
        onReject={onReject}
      />,
    );

    expect(screen.getByText('Daytona Box')).toBeInTheDocument();
    expect(screen.getByText('Workspace 5')).toBeInTheDocument();
    expect(screen.queryByText('Workspace 6')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /show all 6/i }));
    expect(screen.getByText('Workspace 6')).toBeInTheDocument();

    // Failed scope_changed state
    rerender(
      <SecretaryConfirmCard
        proposalData={{
          actionType: 'archive_workspace',
          workspace_id: 'ws-1',
          computer_id: COMPUTER_ID,
          computer_name: 'Daytona Box',
          expected_workspaces: workspaces,
          tool_call_id: 'tc-arch-1',
          status: 'failed',
          failure_code: 'scope_changed',
          failure_error: '情况已变化，请重新发起',
        }}
      />,
    );
    expect(screen.getByText('情况已变化，请重新发起')).toBeInTheDocument();

    // Approved state
    rerender(
      <SecretaryConfirmCard
        proposalData={{
          actionType: 'archive_workspace',
          workspace_id: 'ws-1',
          computer_id: COMPUTER_ID,
          computer_name: 'Daytona Box',
          expected_workspaces: workspaces,
          archived_workspace_count: 6,
          tool_call_id: 'tc-arch-1',
          status: 'approved',
        }}
      />,
    );
    expect(screen.getByText(/Archived computer Daytona Box \(6 workspaces\)/i)).toBeInTheDocument();
  });

  it('settles archive_workspace strictly by tool_call_id for live and history results', () => {
    const messages: ChatMessage[] = [
      {
        id: 'a-1',
        role: 'assistant',
        content: '',
        contentSegments: [{ type: 'archive_workspace', proposalId: 'int-1', order: 1 }],
        secretaryActionProposals: {
          'int-1': {
            actionType: 'archive_workspace',
            workspace_id: WORKSPACE_ID,
            computer_id: COMPUTER_ID,
            computer_name: 'Daytona Box',
            tool_call_id: 'tc-arch-1',
            interruptId: 'int-1',
            status: 'submitting',
          },
        },
      } as unknown as ChatMessage,
    ];

    // Wrong tool_call_id must not settle
    const unchanged = settleLiveProposalFromResult(messages, 'tc-other', {
      content: JSON.stringify({ success: true }),
    });
    expect(unchanged).toBe(messages);

    // Matching tool_call_id settles to failed on scope_changed
    const failed = settleLiveProposalFromResult(messages, 'tc-arch-1', {
      content: JSON.stringify({ success: false, code: 'scope_changed', error: '情况已变化，请重新发起' }),
      status: 'error',
    });
    const failedCard = (failed[0] as Extract<ChatMessage, { role: 'assistant' }>).secretaryActionProposals?.['int-1'];
    expect(failedCard?.status).toBe('failed');
    expect(failedCard?.failure_code).toBe('scope_changed');

    // History settlement refuses positional fallback when tool_call_id mismatches
    const pending = [
      {
        type: 'archive_workspace',
        assistantMessageId: 'a-1',
        proposalId: 'int-1',
        interruptId: 'int-1',
        toolCallId: 'tc-arch-1',
      },
    ];
    const setMessages = vi.fn();
    expect(settleProposalFromResult(pending, 'tc-wrong', { content: JSON.stringify({ success: true }) }, setMessages)).toBe(false);
    expect(settleProposalFromResult(pending, 'tc-arch-1', {
      content: JSON.stringify({ success: true, computer_name: 'Daytona Box', archived_workspace_count: 2 }),
    }, setMessages)).toBe(true);
  });
});
