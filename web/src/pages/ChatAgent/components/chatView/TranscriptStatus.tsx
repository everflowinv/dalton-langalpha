import type React from 'react';
import { useTranslation } from 'react-i18next';
import { Clock, Info } from 'lucide-react';
import { HoverCard, HoverCardTrigger, HoverCardContent } from '@/components/ui/hover-card';
import { Loader } from '@/components/ui/loader';
import { compactNumber } from '@/lib/format';
import { useLocale } from '@/hooks/useLocale';
import type { ModelStatus } from '../../session/types';
import type { RetiredModel } from '../../hooks/useThreadModel';
import type { WarmingDisplay } from '../../utils/warmWorkspace';
import { QueuedAutomationNotice } from '../QueuedAutomationNotice';
import { ModelStatusPill } from './ModelStatusPill';
import { RetiredModelNotice } from './ThreadModelBanner';
import { StatusLine } from './StatusLine';

/** Finds the block TranscriptStatus renders (its `data-transcript-status`). */
export const TRANSCRIPT_STATUS_SELECTOR = '[data-transcript-status]';

interface TranscriptStatusProps {
  modelStatus: ModelStatus | null;
  isLoading: boolean;
  retired: RetiredModel | null;
  workspaceStarting: WarmingDisplay;
  /** Why this send waits on a thread mutation, null when it is not held. */
  turnHeld: string | null;
  isCompacting: string | false;
  /** About how many tokens the summary being written has written. */
  compactionTokens: number | null;
  queuedSend: string | false;
  /** The thread whose due automations wait for its turn to end. */
  threadId: string;
  active: boolean;
}

/* The thread's status lines, under the last reply and in the transcript's
   flow. Above the composer they floated over whatever scrolled beneath them
   and read as part of it. The block is always mounted: useChatScroll watches
   it so a reader at the end sees a line arrive. It pads itself only while it
   holds a line, so a line with nothing to say renders no node at all.
   Reconnecting is not one of them: it floats over the composer instead
   (ReconnectingChip), since a reconnect that catches up would take its line
   out from under a reader at the end and drop the reply by its height. */
export function TranscriptStatus({
  modelStatus,
  isLoading,
  retired,
  workspaceStarting,
  turnHeld,
  isCompacting,
  compactionTokens,
  queuedSend,
  threadId,
  active,
}: TranscriptStatusProps): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  return (
    <div data-transcript-status className="flex flex-col not-empty:pt-3">
      <ModelStatusPill modelStatus={modelStatus} isLoading={isLoading} />
      {retired && <RetiredModelNotice {...retired} />}
      {workspaceStarting && <WorkspaceStartingLine archived={workspaceStarting === 'archived'} />}
      {turnHeld !== null && <TurnHeldLine reason={turnHeld} />}
      {isCompacting && (
        <div className="flex items-center gap-2">
          <StatusLine glyph={<Loader size={14} className="text-(--color-accent-primary)" />}>
            {t(isCompacting === 'offload' ? 'chat.offloading' : 'chat.compacting')}
          </StatusLine>
          {/* Beside the live region rather than in it, which would read the
              count out every time it moves; a screen reader still reaches it. */}
          {isCompacting === 'summarize' && compactionTokens != null && (
            <span className="text-xs tabular-nums" style={{ color: 'var(--color-text-quaternary)' }}>
              {t('chat.compactingTokens', { value: compactNumber(compactionTokens, locale) })}
            </span>
          )}
        </div>
      )}
      {queuedSend && (
        <StatusLine
          glyph={<Clock className="h-3.5 w-3.5" style={{ color: 'var(--color-accent-primary)' }} />}
          title={queuedSend === '…' ? undefined : queuedSend}
        >
          {t('chat.queuedSend')}
        </StatusLine>
      )}
      <QueuedAutomationNotice threadId={threadId} active={active} />
    </div>
  );
}

function turnHeldKey(reason: string): string {
  if (reason === 'compact') return 'chat.compacting';
  if (reason === 'handoff') return 'chat.turnHeld.handoff';
  return 'chat.turnHeld.other';
}

function TurnHeldLine({ reason }: { reason: string }): React.ReactElement {
  const { t } = useTranslation();
  return (
    <StatusLine glyph={<Loader size={12} className="text-(--color-accent-primary)" />}>
      {t(turnHeldKey(reason))}
    </StatusLine>
  );
}

function WorkspaceStartingLine({ archived }: { archived: boolean }): React.ReactElement {
  const { t } = useTranslation();
  return (
    <StatusLine live={false} glyph={<Loader size={12} className="text-(--color-accent-primary)" />}>
      <span>{t(archived ? 'chat.workspaceRestoring' : 'chat.workspaceStarting')}</span>
      <HoverCard openDelay={150} closeDelay={100}>
        <HoverCardTrigger asChild>
          <button
            type="button"
            aria-label={t('chat.workspaceStateHelp')}
            className="inline-flex items-center justify-center rounded-full p-0.5 hover:opacity-80 focus:outline-hidden focus-visible:ring-1 focus-visible:ring-ring"
            style={{ color: 'var(--color-text-quaternary)' }}
          >
            <Info className="h-3 w-3" />
          </button>
        </HoverCardTrigger>
        <HoverCardContent side="top" align="start" className="w-80 text-xs leading-relaxed">
          <div className="font-medium mb-1" style={{ color: 'var(--color-text-primary)' }}>
            {t(archived ? 'chat.workspaceStateArchivedTitle' : 'chat.workspaceStateStartingTitle')}
          </div>
          <p style={{ color: 'var(--color-text-secondary)' }}>
            {t(archived ? 'chat.workspaceStateArchivedBody' : 'chat.workspaceStateStartingBody')}
          </p>
          {archived && (
            <p className="mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('chat.workspaceStateArchivedFootnote')}
            </p>
          )}
        </HoverCardContent>
      </HoverCard>
    </StatusLine>
  );
}
