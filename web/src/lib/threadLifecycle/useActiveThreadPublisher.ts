/**
 * Marks the mounted chat view's thread as ACTIVE in the lifecycle store
 * (active thread never shows the unseen dot) and stamps the durable seen
 * cursor when the thread's effective observation is terminal — the "user
 * opened the finished thread" transition.
 *
 * Subscribes to the thread's effective status rather than sampling it once on
 * mount: on a cold URL open the store hasn't hydrated yet (the snapshot frame
 * lands after mount), so a mount-time check misses the terminal state and the
 * dot survives the visit. markThreadSeen dedups in flight and the cursor is
 * monotonic, so overlapping stamps (feed client, local stream end) are safe.
 */
import { useEffect } from 'react';
import { markThreadSeen } from './seen';
import {
  getEffectiveObservation,
  setActiveThread,
  TERMINAL_FAMILY,
  useThreadRunStatus,
} from './store';

// DALTON-HOOK(team-sharing): allow gating active-thread seen writeback until workspace ownership is confirmed
export interface ActiveThreadPublisherOptions {
  canMarkSeen?: boolean;
}

export function useActiveThreadPublisher(
  threadId: string | null | undefined,
  options?: ActiveThreadPublisherOptions,
): void {
  const canMarkSeen = options?.canMarkSeen ?? true;
  const tid = threadId && threadId !== '__default__' ? threadId : null;
  const status = useThreadRunStatus(tid ?? '');

  useEffect(() => {
    if (!canMarkSeen) {
      setActiveThread(null);
      return;
    }
    setActiveThread(tid);
    return () => setActiveThread(null);
  }, [tid, canMarkSeen]);

  useEffect(() => {
    if (!canMarkSeen || !tid || !TERMINAL_FAMILY.has(status)) return;
    const eff = getEffectiveObservation(tid);
    if (eff?.runId && TERMINAL_FAMILY.has(eff.status)) {
      markThreadSeen(tid, eff.runId);
    }
  }, [tid, status, canMarkSeen]);
}
