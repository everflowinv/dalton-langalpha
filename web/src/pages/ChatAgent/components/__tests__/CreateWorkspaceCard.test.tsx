/**
 * CreateWorkspaceCard: pending approval flow. "Always allow" answers through
 * `useApproveAlways`, whose save-then-answer contract is tested in
 * hooks/__tests__/useApproveAlways.test.tsx; here only the wiring.
 */
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const approveAlways = vi.hoisted(() => ({ saving: false, fn: vi.fn((then: () => void) => then()) }));
vi.mock('@/hooks/useAutoApprove', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useAutoApprove')>();
  return {
    AUTO_APPROVE: actual.AUTO_APPROVE,
    useApproveAlways: () => ({ approveAlways: approveAlways.fn, saving: approveAlways.saving }),
  };
});

import CreateWorkspaceCard from '../CreateWorkspaceCard';

// A case that leaves the save out would disable every button in the next one.
beforeEach(() => {
  approveAlways.saving = false;
  approveAlways.fn.mockClear();
});

const PENDING = { status: 'pending' as const, workspace_name: 'New Workspace', workspace_description: 'A fresh workspace' };

describe('CreateWorkspaceCard: pending', () => {
  it('shows the workspace preview and actions', () => {
    renderWithProviders(
      <CreateWorkspaceCard proposalData={PENDING} onApprove={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getAllByText('New Workspace')[0]).toBeInTheDocument();
    expect(screen.getByText('A fresh workspace')).toBeInTheDocument();
    expect(screen.getByText('chat.createWorkspaceCard.create')).toBeInTheDocument();
  });

  it('approves on Create with no overrides', () => {
    const onApprove = vi.fn();
    renderWithProviders(
      <CreateWorkspaceCard proposalData={PENDING} onApprove={onApprove} onReject={vi.fn()} />,
    );
    fireEvent.click(screen.getByText('chat.createWorkspaceCard.create'));
    expect(onApprove).toHaveBeenCalledWith();
  });
});

describe('CreateWorkspaceCard: always allow', () => {
  it('answers through useApproveAlways, and waits while it saves', () => {
    const onApprove = vi.fn();
    const card = () => <CreateWorkspaceCard proposalData={PENDING} onApprove={onApprove} onReject={vi.fn()} />;
    const { rerender } = renderWithProviders(card());

    fireEvent.click(screen.getByText('chat.createWorkspaceCard.alwaysApprove'));

    expect(approveAlways.fn).toHaveBeenCalledOnce();
    expect(onApprove).toHaveBeenCalledWith();

    approveAlways.saving = true;
    rerender(card());

    expect(screen.getByText('chat.createWorkspaceCard.create').closest('button')).toBeDisabled();
    expect(screen.getByText('chat.createWorkspaceCard.alwaysApprove').closest('button')).toBeDisabled();
    expect(screen.getByText('chat.createWorkspaceCard.decline').closest('button')).toBeDisabled();
  });
});

// DALTON-HOOK(cos-settings-tools): Batch 2 snapshot display, own-machine hiding, and 4-state rendering
describe('CreateWorkspaceCard: target computer & 4-state lifecycle', () => {
  it('displays the target computer name and hides Always allow for own machines', () => {
    renderWithProviders(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          computer_id: 'comp-mac-1',
          computer_name: 'My Mac Studio',
          computer_is_own_machine: true,
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByText('chat.createWorkspaceCard.targetComputer')).toBeInTheDocument();
    expect(screen.queryByText('chat.createWorkspaceCard.alwaysApprove')).not.toBeInTheDocument();
    expect(screen.getByText('chat.createWorkspaceCard.create')).toBeInTheDocument();
    expect(screen.getByText('chat.createWorkspaceCard.decline')).toBeInTheDocument();
  });

  it('keeps buttons disabled in submitting state until tool result settles', () => {
    renderWithProviders(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          status: 'submitting',
          computer_name: 'Cloud Box',
          computer_is_own_machine: false,
        }}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByText('chat.createWorkspaceCard.creating').closest('button')).toBeDisabled();
    expect(screen.getByText('chat.createWorkspaceCard.alwaysApprove').closest('button')).toBeDisabled();
    expect(screen.getByText('chat.createWorkspaceCard.decline').closest('button')).toBeDisabled();
  });

  it('renders approved, rejected, and failed (scope_changed) settled states distinctly', () => {
    const { rerender } = renderWithProviders(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          status: 'approved',
          computer_name: 'Cloud Box',
        }}
      />,
    );
    expect(screen.getByTestId('proposal-settled')).toBeInTheDocument();
    expect(screen.getByText('chat.createWorkspaceCard.createdOnComputer')).toBeInTheDocument();

    rerender(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          status: 'rejected',
          computer_name: 'Cloud Box',
        }}
      />,
    );
    expect(screen.getByText('chat.createWorkspaceCard.declined')).toBeInTheDocument();

    rerender(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          status: 'failed',
          computer_name: 'Cloud Box',
          failure_code: 'scope_changed',
          failure_error: '情况已变化，请重新发起',
        }}
      />,
    );
    expect(screen.getByText('chat.createWorkspaceCard.scopeChanged')).toBeInTheDocument();
  });

  it('uses newCloudComputer label when bootstrap_cloud_primary is true', () => {
    renderWithProviders(
      <CreateWorkspaceCard
        proposalData={{
          ...PENDING,
          status: 'approved',
          bootstrap_cloud_primary: true,
        }}
      />,
    );
    expect(screen.getByText('chat.createWorkspaceCard.createdOnComputer')).toBeInTheDocument();
  });
});
