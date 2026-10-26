import React, { Suspense, useCallback, useEffect } from 'react';
import type { OpenFileHandler } from '../../utils/fileLocation';
import type { MarketWatchState } from '../../session/marketWatchEvents';
import type { PanelTarget } from './types';

const MemoryPanel = React.lazy(() => import('../MemoryPanel'));
const MemoPanel = React.lazy(() => import('../MemoPanel'));
const StatusPanel = React.lazy(() => import('../StatusPanel'));

interface StoreTabProps {
  kind: 'memory' | 'memo' | 'status';
  workspaceId: string;
  /** Read for the entry a memory or memo target names; the body clears it once selected. */
  target: PanelTarget | null;
  onTargetMemoryHandled?: (seq?: number) => void;
  onTargetMemoHandled?: (seq?: number) => void;
  onOpenFile: OpenFileHandler | null;
  marketWatch: MarketWatchState | null;
  // DALTON-HOOK(team-sharing): prevent mounting viewer's personal MemoPanel in colleague mirror mode
  teamReadOnly?: boolean;
  onCancelMarketTarget: ((targetId: string) => Promise<void>) | null;
}

/**
 * The body of a memory, memo or status tab: the store's own panel, or the
 * live watch. A memory or memo target is not consumed on arrival like a
 * file's: the body selects the entry once the store's list resolves, and says
 * so then, naming the ask it had so a later one is not cleared with it.
 */
export function StoreTab({ kind, workspaceId, target, onTargetMemoryHandled, onTargetMemoHandled, onOpenFile, marketWatch, onCancelMarketTarget, teamReadOnly = false }: StoreTabProps): React.ReactElement {
  const onOpen = onOpenFile ?? undefined;
  // Stable per ask: the bodies keep these in effect deps.
  const seq = target?.seq;
  const memoryHandled = useCallback(() => onTargetMemoryHandled?.(seq), [onTargetMemoryHandled, seq]);
  const memoHandled = useCallback(() => onTargetMemoHandled?.(seq), [onTargetMemoHandled, seq]);

  // DALTON-HOOK(team-sharing): clear pending memo target when blocked in colleague mirror mode
  useEffect(() => {
    if (teamReadOnly && kind === 'memo' && target?.kind === 'memo') {
      memoHandled();
    }
  }, [teamReadOnly, kind, target?.kind, memoHandled]);

  const body = (): React.ReactNode => {
    switch (kind) {
      case 'memory':
        return (
          <MemoryPanel
            workspaceId={workspaceId}
            targetKey={target?.kind === 'memory' ? target.key : null}
            targetTier={target?.kind === 'memory' ? target.tier : null}
            onTargetHandled={memoryHandled}
            onOpenFile={onOpen}
          />
        );
      case 'memo':
        // DALTON-HOOK(team-sharing): do not mount viewer's personal MemoPanel in colleague mirror mode
        if (teamReadOnly) {
          return (
            <p
              data-testid="team-memo-unavailable"
              className="px-6 py-10 text-center text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              同事的备忘录不可查看
            </p>
          );
        }
        return (
          <MemoPanel
            targetKey={target?.kind === 'memo' ? target.key : null}
            onTargetHandled={memoHandled}
            onOpenFile={onOpen}
          />
        );
      case 'status':
        return <StatusPanel marketWatch={marketWatch} onCancelTarget={onCancelMarketTarget} />;
      default:
        return kind satisfies never;
    }
  };
  return <Suspense fallback={null}>{body()}</Suspense>;
}
