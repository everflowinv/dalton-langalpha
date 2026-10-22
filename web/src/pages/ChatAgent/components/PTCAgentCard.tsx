import React, { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { motion, AnimatePresence, type MotionProps } from '@/lib/framer';
import { X, ChevronRight, ArrowRight } from 'lucide-react';
import { useDispatchStatus, type PTCDispatchStatus } from '../hooks/usePTCDispatchStatus';
import { DispatchStatusIndicator } from './dispatchStatusUi';
import { useOpenDispatchedThread, type DispatcherContext } from '../hooks/useOpenDispatchedThread';
import { AUTO_APPROVE, useApproveAlways } from '@/hooks/useAutoApprove';
import Markdown from './Markdown';
import { OverflowCollapse } from './messageList/OverflowCollapse';

interface ProposalData {
  workspace_name?: string;
  question: string;
  status: 'pending' | 'approved' | 'rejected';
  thread_id?: string;
  workspace_id?: string;
  report_back?: boolean;
  /** Approved, but the dispatch failed (session/interrupts/buckets.ts). */
  dispatch_failed?: boolean;
}

interface PTCAgentCardProps {
  proposalData: ProposalData | null;
  onApprove?: (overrides?: { report_back?: boolean }) => void;
  onReject?: () => void;
  dispatcherContext?: DispatcherContext | null;
}

// Featured-surface visual language (matches ConversationWidget / AIDailyBriefCard).
// Message-flow card → the chat artifact fill (tinted on the light chat page),
// not --color-bg-card (white; the dashboard-on-canvas pairing).
const PANEL_BG = 'var(--color-bg-tool-card)';
const MONO = 'var(--font-mono)';

// How much of the brief the approval card shows before Show all, in px. A
// hand-off brief is written for an analyst who sees nothing else, so it runs
// long; once approved, the card names the run instead and the thread holds
// the brief.
const BRIEF_MAX_PX = 132;

/**
 * The thread's title, unless it is still the placeholder a new thread is given
 * until its title is generated: the question's first 255 code points, as the
 * server slices them. An exact match, so a generated title that happens to open
 * the brief still shows. Both sides trimmed, as the liveness slice trims.
 */
function namedTitle(title: string | undefined, question: string): string | null {
  if (!title || title === Array.from(question).slice(0, 255).join('').trim()) return null;
  return title;
}

/** The brief as one line of plain text, for a clamped preview. */
function plainBrief(question: string): string {
  return question
    .replace(/^\s*(?:#{1,6}|>|[-*+]|\d+\.)\s+/gm, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\*\*(\S(?:[^*]*\S)?)\*\*/g, '$1')
    .replace(/`/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * The card's own reading of each status: whether the run is still in flight
 * (the shell keeps the slightly stronger border) and the footer hint and CTA.
 * How the status itself reads lives with DispatchStatusIndicator. Keys are
 * resolved with `t()` at the render site.
 */
const CARD_UI: Record<
  PTCDispatchStatus,
  {
    live: boolean;
    hintKey: string | null;
    ctaKey: string;
    /** Paints the CTA amber (the one state asking for the user's action). */
    ctaAccent?: boolean;
  }
> = {
  starting: { live: true, hintKey: 'chat.ptcCard.hintProvisioning', ctaKey: 'chat.ptcCard.ctaOpenThread' },
  running: { live: true, hintKey: 'chat.ptcCard.hintWorking', ctaKey: 'chat.ptcCard.ctaOpenThread' },
  needs_input: { live: true, hintKey: null, ctaKey: 'chat.ptcCard.ctaAnswerContinue', ctaAccent: true },
  completed: { live: false, hintKey: null, ctaKey: 'chat.ptcCard.ctaOpenThread' },
  failed: { live: false, hintKey: 'chat.ptcCard.hintFailed', ctaKey: 'chat.ptcCard.ctaViewThread' },
  stopped: { live: false, hintKey: 'chat.ptcCard.hintStopped', ctaKey: 'chat.ptcCard.ctaViewThread' },
};

interface MissionPanelProps {
  /** Small mono kicker naming the workspace the run belongs to. */
  eyebrow: string;
  /** What the card is about: the run's title, or the brief while the run has none. */
  heading?: string | null;
  /** Under the heading: the brief, whole or as a clamped preview. */
  body: React.ReactNode;
  /** Border color of the shell. */
  border: string;
  /** Right-aligned header content (status indicator or "awaiting approval" label). */
  statusSlot: React.ReactNode;
  /** Shell entrance animation, forwarded to the motion shell. */
  animate: MotionProps['animate'];
  transition: MotionProps['transition'];
  /** Footer content beneath the question (open-thread affordance or approve/decline). */
  children?: React.ReactNode;
}

/**
 * Shared work-order chrome for the pending + approved cards: the motion shell,
 * kicker, heading and body. Both cards layout-match; only what they say, the
 * header status slot, border, and footer differ, so those are slots.
 * Deliberately quiet — the card sits inside a chat transcript, so the brief
 * renders below body size and is bounded, and the workspace name is small mono
 * metadata.
 */
function MissionPanel({
  eyebrow,
  heading,
  body,
  border,
  statusSlot,
  animate,
  transition,
  children,
}: MissionPanelProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={animate}
      transition={transition}
      className="relative overflow-hidden rounded-lg"
      style={{
        border: `1px solid ${border}`,
        background: PANEL_BG,
      }}
    >
      <div className="relative px-4 pb-3 pt-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <span
            className="min-w-0 truncate text-[0.6563rem] font-medium uppercase"
            style={{ fontFamily: MONO, letterSpacing: '0.08em', color: 'var(--color-text-quaternary)' }}
          >
            {eyebrow}
          </span>
          {statusSlot}
        </div>

        {heading && (
          <div className="line-clamp-2 text-[0.875rem] font-medium leading-snug" style={{ color: 'var(--color-text-primary)' }}>
            {heading}
          </div>
        )}

        {body}

        {children}
      </div>
    </motion.div>
  );
}

/**
 * PTCAgentCard — inline HITL card for dispatching a background PTC run.
 *
 *   pending  — question + report-back toggle + Approve/Decline
 *   approved — live work-order panel that tracks the dispatched thread's /status
 *              (starting → running → completed/needs-input/failed/stopped)
 *   rejected — quiet collapsed "Research declined" row
 */
function PTCAgentCard({ proposalData, onApprove, onReject, dispatcherContext }: PTCAgentCardProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(true);
  const [reportBack, setReportBack] = useState(proposalData?.report_back ?? true);
  const { approveAlways, saving } = useApproveAlways(AUTO_APPROVE.handoffs);
  const openDispatchedThread = useOpenDispatchedThread(dispatcherContext);
  const detailId = useId();

  const status = proposalData?.status;
  const isApproved = status === 'approved';
  const threadId = proposalData?.thread_id;

  const { status: runStatus, startedAt, title: threadTitle, workspaceName: liveWorkspaceName } =
    useDispatchStatus(threadId, isApproved && !!threadId);
  // A failed dispatch has no run to wait for, unless one turns up on the
  // thread an unknown outcome named.
  const notStarted = proposalData?.dispatch_failed === true && runStatus === 'starting';
  const dispatchStatus: PTCDispatchStatus = notStarted ? 'failed' : runStatus;

  if (!proposalData) return null;

  const { workspace_name, question, workspace_id } = proposalData;
  // The kicker names which workspace the run belongs to — PTC runs aren't only
  // "deep research", so we surface the workspace's real name (resolved by the
  // backend), current once the run's thread is read. Empty when unknown rather
  // than a fixed placeholder string.
  const eyebrow = liveWorkspaceName || workspace_name?.trim() || '';
  const title = namedTitle(threadTitle, question);

  const openThread = () => {
    if (threadId) openDispatchedThread(threadId, workspace_id);
  };

  // ---------------- Rejected: quiet collapsible row ----------------
  if (status === 'rejected') {
    return (
      <div>
        <button onClick={() => setCollapsed((v) => !v)} aria-expanded={!collapsed} aria-controls={detailId} className="flex w-full cursor-pointer items-center gap-2 py-1 text-left">
          <motion.div animate={{ rotate: collapsed ? 0 : 90 }} transition={{ duration: 0.2 }}>
            <ChevronRight className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-icon-muted)' }} />
          </motion.div>
          <X className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>{t('chat.ptcCard.researchDeclined')}</span>
        </button>
        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.div
              id={detailId}
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              className="overflow-hidden"
            >
              <div className="pb-1 pl-6 pt-2">
                <div className="rounded-lg px-4 py-3" style={{ border: '1px solid var(--color-border-muted)', opacity: 0.6 }}>
                  {workspace_name && <div className="mb-1 text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{workspace_name}</div>}
                  <OverflowCollapse enabled maxHeight={BRIEF_MAX_PX}>
                    <Markdown variant="compact" content={question} className="text-[0.8125rem] leading-relaxed" />
                  </OverflowCollapse>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  // ---------------- Approved: live work-order panel ----------------
  if (isApproved) {
    const ui = CARD_UI[dispatchStatus];
    const hintKey = notStarted ? 'chat.ptcCard.hintNotStarted' : ui.hintKey;

    return (
      <MissionPanel
        eyebrow={eyebrow}
        heading={title ?? plainBrief(question)}
        body={title && (
          <div className="mt-1 line-clamp-1 text-[0.8125rem]" style={{ color: 'var(--color-text-tertiary)' }}>
            {plainBrief(question)}
          </div>
        )}
        border={ui.live ? 'var(--color-border-default)' : 'var(--color-border-muted)'}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
        statusSlot={<DispatchStatusIndicator status={dispatchStatus} startedAt={startedAt} />}
      >
        {(threadId || notStarted) && (
          <div className="mt-3 flex items-center justify-between gap-3 pt-2.5" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
            <span className="text-[0.75rem]" style={{ color: 'var(--color-text-quaternary)' }}>{hintKey ? t(hintKey) : ''}</span>
            {threadId && (
              <button
                onClick={openThread}
                className="group inline-flex shrink-0 items-center gap-1 text-[0.7813rem] font-medium transition-opacity hover:opacity-80"
                style={{ color: ui.ctaAccent ? 'var(--color-accent-primary)' : 'var(--color-text-tertiary)' }}
              >
                {t(ui.ctaKey)}
                <ArrowRight aria-hidden className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
              </button>
            )}
          </div>
        )}
      </MissionPanel>
    );
  }

  // ---------------- Pending: quieter version of the panel ----------------
  return (
    <MissionPanel
      eyebrow={eyebrow}
      body={
        <OverflowCollapse enabled maxHeight={BRIEF_MAX_PX}>
          <Markdown variant="compact" content={question} className="text-[0.8125rem] leading-relaxed" />
        </OverflowCollapse>
      }
      border="var(--color-border-muted)"
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      statusSlot={
        <span className="shrink-0 text-[0.6875rem] font-medium" style={{ color: 'var(--color-text-quaternary)' }}>{t('chat.ptcCard.awaitingApproval')}</span>
      }
    >
      {/* DALTON-HOOK(team-sharing): hide interactive toggle and approval buttons in read-only mode */}
      {(onApprove || onReject) && (
      <>
      {/* Report-back toggle */}
      <button
        type="button"
        role="switch"
        aria-checked={reportBack}
        aria-label={t('chat.ptcCard.reportBack')}
        className="mt-3 flex w-full cursor-pointer items-center justify-between rounded-md pt-2.5 outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        style={{ borderTop: '1px solid var(--color-border-muted)' }}
        onClick={(e: React.MouseEvent) => { e.stopPropagation(); setReportBack((v) => !v); }}
      >
        <span className="text-[0.8125rem]" style={{ color: 'var(--color-text-tertiary)' }}>{t('chat.ptcCard.reportBack')}</span>
        <div aria-hidden className="relative h-[18px] w-8 rounded-full transition-colors" style={{ background: reportBack ? 'var(--color-accent-primary)' : 'var(--color-border-muted)' }}>
          {/* Hairline ring + drop shadow keep the knob legible on the very light
              OFF track in light theme (where a flat white knob nearly vanishes)
              without dimming the ON-state look in either theme. */}
          <div
            className="absolute left-[3px] top-[3px] h-3 w-3 rounded-full bg-white transition-transform"
            style={{ transform: reportBack ? 'translateX(14px)' : 'translateX(0)', boxShadow: '0 1px 2px rgba(0,0,0,0.25), 0 0 0 0.5px rgba(0,0,0,0.12)' }}
          />
        </div>
      </button>

      {/* Actions — quiet text buttons, no motion bounce. */}
      <div className="flex items-center gap-2 pt-3">
        <button
          disabled={saving}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); onApprove?.({ report_back: reportBack }); }}
          className="rounded-md px-3.5 py-1.5 text-[0.8125rem] font-medium transition-[filter] hover:brightness-110"
          style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
        >
          {t('chat.ptcCard.approve')}
        </button>
        <button
          disabled={saving}
          title={t('chat.ptcCard.alwaysApproveHint')}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); approveAlways(() => onApprove?.({ report_back: reportBack })); }}
          className="rounded-md px-3.5 py-1.5 text-[0.8125rem] font-medium transition-colors hover:bg-(--color-bg-hover)"
          style={{ border: '1px solid var(--color-border-default)', color: 'var(--color-text-secondary)' }}
        >
          {t('chat.ptcCard.alwaysApprove')}
        </button>
        <button
          disabled={saving}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); onReject?.(); }}
          className="rounded-md px-3.5 py-1.5 text-[0.8125rem] font-medium transition-colors hover:bg-(--color-bg-hover)"
          style={{ border: '1px solid var(--color-border-default)', color: 'var(--color-text-secondary)' }}
        >
          {t('chat.ptcCard.decline')}
        </button>
        {/* DALTON-HOOK(team-sharing): read-only for a teammate's workspace */}
      </div>
      </>
      )}
    </MissionPanel>
  );
}

export default PTCAgentCard;
