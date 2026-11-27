/**
 * Unified proposal HITL interrupt target & optimistic status resolver (Dalton-owned).
 *
 * Every live/replayed interrupt is tracked on AnswerBoard by its `interruptId`.
 * A clicked proposal card that carries its own `interruptId` always answers
 * through AnswerBoard under that id, even when another interrupt arrived last
 * and overwrote the single `pendingInterrupt` slot. Only when the clicked card
 * omits `interruptId` do we fall back to `pendingInterrupt`, and only if the
 * slot belongs to the same proposal bucket.
 */

export type ProposalBucket =
  | 'workspaceProposals'
  | 'questionProposals'
  | 'secretaryActionProposals'
  | 'watchlistProposals';

export type ProposalVerdict = 'approve' | 'reject';

export type ProposalNextStatus = 'submitting' | 'approved' | 'rejected';

export interface PendingInterruptLike {
  type: string;
  interruptId?: string;
  proposalId?: string;
}

export interface ProposalAnswerTarget {
  interruptId: string;
  proposalId: string;
  nextStatus: ProposalNextStatus;
}

export interface ResolveProposalAnswerTargetParams {
  bucket: ProposalBucket;
  verdict: ProposalVerdict;
  proposal?: Record<string, unknown>;
  pendingInterrupt: PendingInterruptLike | null;
  proposalDataKeyMap: Record<string, string | undefined>;
}

export function resolveProposalAnswerTarget({
  bucket,
  verdict,
  proposal,
  pendingInterrupt,
  proposalDataKeyMap,
}: ResolveProposalAnswerTargetParams): ProposalAnswerTarget | null {
  const pendingMatchesBucket =
    !!pendingInterrupt && proposalDataKeyMap[pendingInterrupt.type] === bucket;

  const cardInterruptId =
    typeof proposal?.interruptId === 'string' && proposal.interruptId
      ? proposal.interruptId
      : undefined;
  const cardProposalId =
    typeof proposal?.proposalId === 'string' && proposal.proposalId
      ? proposal.proposalId
      : undefined;

  const interruptId =
    cardInterruptId
    || (pendingMatchesBucket ? pendingInterrupt.interruptId : undefined);
  if (!interruptId) return null;

  const proposalId =
    cardProposalId
    || (pendingMatchesBucket && pendingInterrupt.interruptId === interruptId
      ? pendingInterrupt.proposalId
      : undefined)
    || interruptId
    || (pendingMatchesBucket ? pendingInterrupt.proposalId : undefined);
  if (!proposalId) return null;

  const isArchiveCard =
    bucket === 'secretaryActionProposals'
    && (
      proposal?.actionType === 'archive_workspace'
      || (
        !proposal?.actionType
        && pendingInterrupt?.type === 'archive_workspace'
        && (!cardInterruptId || pendingInterrupt.interruptId === cardInterruptId)
      )
    );
  const waitsForToolResult =
    bucket === 'workspaceProposals'
    || bucket === 'watchlistProposals'
    || isArchiveCard;
  const nextStatus: ProposalNextStatus =
    verdict === 'approve'
      ? (waitsForToolResult ? 'submitting' : 'approved')
      : 'rejected';

  return { interruptId, proposalId, nextStatus };
}
