import React, { useState } from 'react';
import { ListChecks, AlertTriangle, RefreshCw, Layers, Trash2, Sliders } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ProposalCard } from './ProposalCard';
import { reconcileWatchlist } from '@/lib/dalton/watchlist';
import type { WatchlistProposalActionType, WatchlistProposalState } from '@/types/chat';

interface WatchlistConfirmCardProps {
  proposalData: WatchlistProposalState | null;
  onApprove?: () => void;
  onReject?: () => void;
}

const ACTION_ICON: Record<WatchlistProposalActionType, typeof ListChecks> = {
  watchlist_batch_add: ListChecks,
  watchlist_set_tier: Layers,
  watchlist_remove: Trash2,
  watchlist_tier_policy: Sliders,
};

const TIER_KEY: Record<string, string> = {
  core: 'chat.watchlistConfirm.tiers.core',
  watch: 'chat.watchlistConfirm.tiers.watch',
  radar: 'chat.watchlistConfirm.tiers.radar',
};

function formatTaskNameLabel(name: string, t: (k: string) => string): string {
  const lower = name.toLowerCase();
  if (lower.endsWith('-watch')) return t('chat.watchlistConfirm.tasks.watch');
  if (lower.endsWith('-delta')) return t('chat.watchlistConfirm.tasks.delta');
  if (lower.endsWith('-move-up') || lower.endsWith('-move-down')) {
    return t('chat.watchlistConfirm.tasks.move');
  }
  if (lower.includes('-earn-')) return t('chat.watchlistConfirm.tasks.earnings');
  return name;
}

/**
 * WatchlistConfirmCard - Inline HITL confirmation card for personal watchlist mutations.
 *
 * Supports 4-state live and history-replayed lifecycle:
 *   pending / submitting -> shows affected tickers, workspaces, task changes, and Approve/Reject
 *   approved             -> settled success (or partial completion with one-click reconcile retry)
 *   rejected             -> settled cancelled
 *   failed               -> settled error (including scope_changed)
 */
export function WatchlistConfirmCard({
  proposalData,
  onApprove,
  onReject,
}: WatchlistConfirmCardProps): React.ReactElement | null {
  const { t } = useTranslation();
  const [retrying, setRetrying] = useState(false);
  const [retryDone, setRetryDone] = useState(false);

  if (!proposalData) return null;

  const {
    actionType,
    status,
    name,
    description,
    affected_tickers = [],
    affected_workspaces = [],
    task_summary,
    preserve_workspace_notice,
    failure_code,
    failure_error,
    reconcile_status,
    job_id,
  } = proposalData;

  const Icon = ACTION_ICON[actionType] ?? ListChecks;
  const title = name || t(`chat.watchlistConfirm.actions.${actionType}`);
  const isPartial = status === 'approved' && reconcile_status === 'partial' && !retryDone;

  const settledLabel =
    status === 'approved'
      ? isPartial
        ? t('chat.watchlistConfirm.settled.partial')
        : t('chat.watchlistConfirm.settled.approved')
      : status === 'failed'
        ? failure_code === 'scope_changed'
          ? t('chat.watchlistConfirm.settled.scopeChanged')
          : failure_error
            ? t('chat.watchlistConfirm.settled.failedWithReason', { reason: failure_error })
            : t('chat.watchlistConfirm.settled.failed')
        : t('chat.watchlistConfirm.settled.rejected');

  const handleRetryReconcile = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (retrying) return;
    setRetrying(true);
    try {
      await reconcileWatchlist(job_id || undefined);
      setRetryDone(true);
    } finally {
      setRetrying(false);
    }
  };

  const renderTier = (tier: string | null | undefined) => {
    if (!tier) return '';
    const key = TIER_KEY[tier.toLowerCase()];
    return key ? t(key) : tier;
  };

  const summaryParts: string[] = [];
  if ((task_summary?.create_count ?? 0) > 0) {
    summaryParts.push(t('chat.watchlistConfirm.summary.create', { count: task_summary!.create_count }));
  }
  if ((task_summary?.update_count ?? 0) > 0) {
    summaryParts.push(t('chat.watchlistConfirm.summary.update', { count: task_summary!.update_count }));
  }
  if ((task_summary?.resume_count ?? 0) > 0) {
    summaryParts.push(t('chat.watchlistConfirm.summary.resume', { count: task_summary!.resume_count }));
  }
  if ((task_summary?.pause_count ?? 0) > 0) {
    summaryParts.push(t('chat.watchlistConfirm.summary.pause', { count: task_summary!.pause_count }));
  }

  const details = (
    <div className="space-y-2 text-xs">
      {description && (
        <div style={{ color: 'var(--color-text-secondary)' }}>{description}</div>
      )}

      {affected_tickers.length > 0 && (
        <div className="space-y-1">
          <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('chat.watchlistConfirm.affectedCompanies', { count: affected_tickers.length })}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {affected_tickers.slice(0, 12).map((item) => (
              <span
                key={item.ticker}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded border font-mono text-[11px]"
                style={{
                  backgroundColor: 'var(--color-bg-page)',
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-primary)',
                }}
              >
                <span>{item.ticker}</span>
                {item.old_tier && item.new_tier && item.old_tier !== item.new_tier ? (
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {renderTier(item.old_tier)} → {renderTier(item.new_tier)}
                  </span>
                ) : item.new_tier ? (
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {renderTier(item.new_tier)}
                  </span>
                ) : null}
              </span>
            ))}
            {affected_tickers.length > 12 && (
              <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                +{affected_tickers.length - 12}
              </span>
            )}
          </div>
        </div>
      )}

      {affected_workspaces.length > 0 && (
        <div className="space-y-1">
          <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('chat.watchlistConfirm.affectedWorkspaces', { count: affected_workspaces.length })}
          </div>
          {summaryParts.length > 0 && (
            <div style={{ color: 'var(--color-text-secondary)' }}>
              {summaryParts.join(' · ')}
            </div>
          )}
          <div className="space-y-1 max-h-32 overflow-y-auto">
            {affected_workspaces.slice(0, 6).map((ws) => (
              <div
                key={`${ws.ticker}:${ws.workspace_id}`}
                className="flex flex-wrap items-center gap-1.5 text-[11px]"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {ws.ticker}
                </span>
                {(ws.task_changes || []).slice(0, 4).map((tc, idx) => (
                  <span key={`${tc.name}-${idx}`}>
                    {formatTaskNameLabel(tc.name, t)} ({tc.change})
                  </span>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {preserve_workspace_notice && (
        <div className="flex items-start gap-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" style={{ color: 'var(--color-warning)' }} />
          <span>{preserve_workspace_notice}</span>
        </div>
      )}

      {isPartial && (
        <div className="pt-1">
          <button
            type="button"
            disabled={retrying}
            onClick={handleRetryReconcile}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-xs font-medium hover:bg-(--color-bg-hover)"
            style={{
              borderColor: 'var(--color-border-muted)',
              color: 'var(--color-accent-light)',
            }}
          >
            <RefreshCw className="h-3 w-3" />
            <span>{retrying ? t('chat.watchlistConfirm.retrying') : t('chat.watchlistConfirm.retryReconcile')}</span>
          </button>
        </div>
      )}
    </div>
  );

  return (
    <ProposalCard
      icon={Icon}
      title={title}
      status={status}
      settledLabel={settledLabel}
      approveLabel={
        status === 'submitting'
          ? t('chat.watchlistConfirm.submitting')
          : t('chat.watchlistConfirm.confirm')
      }
      rejectLabel={t('chat.watchlistConfirm.decline')}
      onApprove={onApprove}
      onReject={onReject}
      busy={status === 'submitting'}
      settledDetail={details}
    >
      {details}
    </ProposalCard>
  );
}

export default WatchlistConfirmCard;
