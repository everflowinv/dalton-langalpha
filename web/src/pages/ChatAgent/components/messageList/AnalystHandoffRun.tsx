import React from 'react';
import type { PTCAgentProposalState } from '@/types/chat';
import { useDispatchStatuses } from '../../hooks/usePTCDispatchStatus';
import { useOpenDispatchedThread, type DispatcherContext } from '../../hooks/useOpenDispatchedThread';
import { HandoffApproval } from '../analystHandoff/HandoffApproval';
import { HandoffDeclined } from '../analystHandoff/HandoffDeclined';
import { HandoffGroup } from '../analystHandoff/HandoffGroup';
import { describeHandoff, followedRun, type HandoffView } from '../analystHandoff/handoffModel';
import { HandoffSlip } from '../analystHandoff/HandoffSlip';
import { placeOf, useHandoffPlaces } from '../analystHandoff/reportLinks';
import { useMessageActions, type MessageActions } from './MessageActionsContext';

export interface AnalystHandoffMember {
  /** The render block's key, stable for the hand-off's life. */
  key: string;
  proposal: PTCAgentProposalState;
  proposalId: string;
}

type Piece =
  | { kind: 'handed'; members: AnalystHandoffMember[] }
  | { kind: 'single'; member: AnalystHandoffMember };

/**
 * Handed-off members side by side form one stack; one awaiting an answer, or
 * declined, stands on its own where it falls, so the order reads as written.
 */
function piecesOf(members: AnalystHandoffMember[]): Piece[] {
  const pieces: Piece[] = [];
  for (const m of members) {
    const last = pieces[pieces.length - 1];
    if (m.proposal.status !== 'approved') pieces.push({ kind: 'single', member: m });
    else if (last?.kind === 'handed') last.members.push(m);
    else pieces.push({ kind: 'handed', members: [m] });
  }
  return pieces;
}

/**
 * Hand-offs to workspace analysts made back to back in a reply, with nothing
 * between them. Keyed by the first, so a later hand-off grows the run without
 * remounting the ones already drawn. Two or more already handed off read as
 * one group under one header. Every handed-off run is read here,
 * in one batch, so a group's header and its rows say the same from the first
 * paint.
 */
export function AnalystHandoffRun({
  members,
  onApprove,
  onReject,
  dispatcherContext,
}: {
  members: AnalystHandoffMember[];
  onApprove?: MessageActions['onApprovePTCAgent'];
  onReject?: MessageActions['onRejectPTCAgent'];
  dispatcherContext?: DispatcherContext | null;
}): React.ReactElement | null {
  const places = useHandoffPlaces();
  const { onRevealMessage } = useMessageActions();
  const openDispatchedThread = useOpenDispatchedThread(dispatcherContext);
  const handed = members.filter((m) => m.proposal.status === 'approved');
  const reads = useDispatchStatuses(handed.map((m) => followedRun(m.proposal, placeOf(places, m.proposal))));
  const states = new Map(handed.map((m, i) => [m.key, describeHandoff(m.proposal, placeOf(places, m.proposal), reads[i])]));

  // A report that follows straight on is read next, so a link down to it
  // would only repeat it. Hand-offs drawn together link theirs all at once or
  // not at all: once each that will report has its report, if one of those
  // does not follow. One that will not (asked not to, never started) would
  // otherwise hold back the others' links for good.
  const viewsOf = (drawn: AnalystHandoffMember[]): HandoffView[] => {
    const reporting = drawn.filter((m) => m.proposal.report_back !== false
      && states.get(m.key)!.phase !== 'not_started'
      && followedRun(m.proposal, placeOf(places, m.proposal)) !== null);
    const linked = reporting.length > 0
      && reporting.every((m) => states.get(m.key)!.reportId)
      && reporting.some((m) => !placeOf(places, m.proposal).reportFollows);
    return drawn.map((m) => {
      const { proposal } = m;
      const state = states.get(m.key)!;
      const threadId = proposal.thread_id;
      const onOpen = threadId ? () => openDispatchedThread(threadId, proposal.workspace_id) : undefined;
      const reportId = linked ? state.reportId : undefined;
      // The report may not be drawn (a list still paging it in), or the host
      // may have no way to reach it; the thread holds it either way.
      const onSeeReport = reportId ? () => { if (!onRevealMessage?.(reportId)) onOpen?.(); } : undefined;
      return { ...state, id: m.key, onOpen, onSeeReport };
    });
  };

  if (members.length === 0) return null;
  return (
    <div className="sibling-space-y-3">
      {piecesOf(members).map((piece) => {
        if (piece.kind === 'handed') {
          const [first] = piece.members;
          const handedViews = viewsOf(piece.members);
          return handedViews.length === 1
            ? <HandoffSlip key={`handed-${first.key}`} view={handedViews[0]} />
            : <HandoffGroup key={`handed-${first.key}`} views={handedViews} />;
        }
        const { key, proposal, proposalId } = piece.member;
        if (proposal.status === 'rejected') return <HandoffDeclined key={key} proposal={proposal} />;
        const interruptId = proposal.interruptId ?? '';
        return (
          <HandoffApproval
            key={key}
            proposal={proposal}
            onApprove={onApprove ? (overrides) => onApprove(proposal, overrides, proposalId, interruptId) : undefined}
            onReject={onReject ? () => onReject(proposal, proposalId, interruptId) : undefined}
          />
        );
      })}
    </div>
  );
}

export default AnalystHandoffRun;
