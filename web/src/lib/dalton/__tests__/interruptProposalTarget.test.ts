import { describe, expect, it } from 'vitest';
import { resolveProposalAnswerTarget } from '../interruptProposalTarget';

const MAP: Record<string, string> = {
  create_workspace: 'workspaceProposals',
  start_question: 'questionProposals',
  delete_workspace: 'secretaryActionProposals',
  stop_workspace: 'secretaryActionProposals',
  delete_thread: 'secretaryActionProposals',
  archive_workspace: 'secretaryActionProposals',
  watchlist_set_tier: 'watchlistProposals',
};

describe('resolveProposalAnswerTarget', () => {
  it('uses the card own interruptId even when pendingInterrupt holds another bucket', () => {
    const target = resolveProposalAnswerTarget({
      bucket: 'workspaceProposals',
      verdict: 'approve',
      proposal: { interruptId: 'intr-cw-1', workspace_name: 'AMD' },
      pendingInterrupt: {
        type: 'archive_workspace',
        interruptId: 'intr-arch-2',
        proposalId: 'intr-arch-2',
      },
      proposalDataKeyMap: MAP,
    });
    expect(target).toEqual({
      interruptId: 'intr-cw-1',
      proposalId: 'intr-cw-1',
      nextStatus: 'submitting',
    });
  });

  it('keeps each card own proposalId when two interrupts share the same bucket', () => {
    const target = resolveProposalAnswerTarget({
      bucket: 'workspaceProposals',
      verdict: 'approve',
      proposal: { interruptId: 'intr-cw-1' },
      pendingInterrupt: {
        type: 'create_workspace',
        interruptId: 'intr-cw-2',
        proposalId: 'intr-cw-2',
      },
      proposalDataKeyMap: MAP,
    });
    expect(target).toEqual({
      interruptId: 'intr-cw-1',
      proposalId: 'intr-cw-1',
      nextStatus: 'submitting',
    });
  });

  it('falls back to pendingInterrupt only when it belongs to the same bucket', () => {
    expect(
      resolveProposalAnswerTarget({
        bucket: 'workspaceProposals',
        verdict: 'approve',
        proposal: {},
        pendingInterrupt: {
          type: 'create_workspace',
          interruptId: 'intr-cw-1',
          proposalId: 'prop-cw-1',
        },
        proposalDataKeyMap: MAP,
      }),
    ).toEqual({
      interruptId: 'intr-cw-1',
      proposalId: 'prop-cw-1',
      nextStatus: 'submitting',
    });

    expect(
      resolveProposalAnswerTarget({
        bucket: 'workspaceProposals',
        verdict: 'approve',
        proposal: {},
        pendingInterrupt: {
          type: 'archive_workspace',
          interruptId: 'intr-arch-1',
          proposalId: 'intr-arch-1',
        },
        proposalDataKeyMap: MAP,
      }),
    ).toBeNull();
  });

  it('applies submitting vs approved vs rejected status rules by bucket and actionType', () => {
    expect(
      resolveProposalAnswerTarget({
        bucket: 'watchlistProposals',
        verdict: 'approve',
        proposal: { interruptId: 'intr-wl' },
        pendingInterrupt: null,
        proposalDataKeyMap: MAP,
      })?.nextStatus,
    ).toBe('submitting');

    expect(
      resolveProposalAnswerTarget({
        bucket: 'secretaryActionProposals',
        verdict: 'approve',
        proposal: { actionType: 'archive_workspace', interruptId: 'intr-arch' },
        pendingInterrupt: null,
        proposalDataKeyMap: MAP,
      })?.nextStatus,
    ).toBe('submitting');

    expect(
      resolveProposalAnswerTarget({
        bucket: 'secretaryActionProposals',
        verdict: 'approve',
        proposal: { actionType: 'delete_workspace', interruptId: 'intr-del' },
        pendingInterrupt: null,
        proposalDataKeyMap: MAP,
      })?.nextStatus,
    ).toBe('approved');

    expect(
      resolveProposalAnswerTarget({
        bucket: 'questionProposals',
        verdict: 'approve',
        proposal: { interruptId: 'intr-q' },
        pendingInterrupt: null,
        proposalDataKeyMap: MAP,
      })?.nextStatus,
    ).toBe('approved');

    expect(
      resolveProposalAnswerTarget({
        bucket: 'workspaceProposals',
        verdict: 'reject',
        proposal: { interruptId: 'intr-cw' },
        pendingInterrupt: null,
        proposalDataKeyMap: MAP,
      })?.nextStatus,
    ).toBe('rejected');
  });
});
