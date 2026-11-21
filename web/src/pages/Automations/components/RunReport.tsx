import React from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight } from 'lucide-react';
import { HeaderButton } from '@/components/mcp/McpPrimitives';
import { ErrorLink } from '@/components/ui/error-banner';
import type { AutomationExecution } from '@/types/automation';
import type { PageLinkSpec } from '@/utils/rateLimitError';
// DALTON-HOOK(execution-delivery-status): per-target delivery status and failure reason helpers
import {
  type DeliveryNames,
  deliveryTargetName,
  formatDeliveryFailureReason,
  matchAttemptForTarget,
  stripSyncedDeliveryExcerptSuffix,
} from '../utils/delivery';
import type { RunView } from '../utils/status';
import { StatusGlyph } from './StatusMark';
import './RunReport.css';

/** A feed entry already shows its status glyph beside the automation's name,
 *  so its meta line goes without; the inspector's report opens on the meta
 *  line, so the glyph leads it there. */
const LAYOUT = {
  entry: { excerpt: 'automation-run-excerpt', glyph: false },
  report: { excerpt: 'automation-report-excerpt', glyph: true },
} as const;

interface RunReportProps {
  run: AutomationExecution;
  view: RunView;
  meta: string[];
  layout: keyof typeof LAYOUT;
  busy: boolean;
  onSkip?: () => void;
  onOpenThread: () => void;
  /** Passed only where the caller allows the failed run another go. */
  onRetry?: () => void;
  onResume?: () => void;
  /** Where the reader acts on the failure's cause, such as the plan a usage
   *  limit sends them to. */
  links?: PageLinkSpec[];
  // DALTON-HOOK(execution-delivery-status): known chat names for per-target delivery details
  deliveryNames?: DeliveryNames;
}

// DALTON-HOOK(company-automations): skip is optional, so a company run report has no skip action.
/** What one run left behind: the answer's start, the error, a note on why it
 *  waits or was skipped, and what can be done about it. */
export function RunReport({
  run,
  view,
  meta,
  layout,
  busy,
  onSkip,
  onOpenThread,
  onRetry,
  onResume,
  links = [],
  deliveryNames,
}: RunReportProps) {
  const { t } = useTranslation();
  const classes = LAYOUT[layout];
  const hasThread = !!run.conversation_thread_id;
  // DALTON-HOOK(execution-delivery-status): format per-target delivery states & failure reasons for completed runs
  const delivery = run.status === 'completed' ? view.deliverySummary : null;
  const deliveryTargets = delivery?.targets ?? [];
  const attempts = run.delivery_result ?? [];
  const distinctTargetFailureReasons = new Set(
    deliveryTargets
      .filter((tgt) => tgt.status === 'failed')
      .map((tgt) => formatDeliveryFailureReason(tgt.error_code, tgt.error_message, t) ?? tgt.error_message ?? '')
      .filter((reason) => reason.length > 0),
  );
  const deliveryReason =
    delivery &&
    (delivery.status === 'failed' || delivery.status === 'partial_failed') &&
    distinctTargetFailureReasons.size <= 1
      ? formatDeliveryFailureReason(delivery.failure_code, delivery.failure_reason, t)
      : null;
  const deliveryErrorLine =
    delivery?.status === 'failed'
      ? deliveryReason
        ? t('automation.deliveryFailedDetail', { reason: deliveryReason })
        : t('automation.deliveryStatusFailed')
      : delivery?.status === 'partial_failed'
        ? deliveryReason
          ? t('automation.deliveryPartialFailedDetail', {
              failed: delivery.failed_count,
              total: delivery.total_targets,
              reason: deliveryReason,
            })
          : t('automation.deliveryPartialFailedNoReason', {
              failed: delivery.failed_count,
              total: delivery.total_targets,
            })
        : null;
  const deliveryRetrying = delivery?.status === 'retrying';
  // DALTON-HOOK(execution-delivery-status): also show delivered targets in detail report when only notify-table summary records exist
  const showDeliveryTargetDetails =
    deliveryTargets.length > 0 &&
    delivery != null &&
    (delivery.status === 'failed' ||
      delivery.status === 'partial_failed' ||
      delivery.status === 'retrying' ||
      (delivery.status === 'delivered' && layout === 'report' && run.delivery_summary != null));
  // DALTON-HOOK(execution-delivery-status): strip fallback synced-undelivered suffix from excerpt when structured delivery status is shown
  const displayExcerpt =
    delivery && (delivery.status === 'failed' || delivery.status === 'partial_failed' || delivery.status === 'retrying')
      ? stripSyncedDeliveryExcerptSuffix(run.excerpt)
      : run.excerpt;
  return (
    <>
      <div className="automation-mono automation-run-meta">
        {classes.glyph && <StatusGlyph ui={view.ui} size={12} />}
        <span>{meta.join(' · ')}</span>
      </div>
      {displayExcerpt && <p className={classes.excerpt}>{displayExcerpt}</p>}
      {view.ui.danger && run.error_message && <p className="automation-mono automation-run-error">{run.error_message}</p>}
      {/* DALTON-HOOK(execution-delivery-status): render delivery failure summary, per-target states/reasons, or retrying note */}
      {deliveryErrorLine && <p className="automation-mono automation-run-error">{deliveryErrorLine}</p>}
      {deliveryRetrying && <p className="automation-run-note">{t('automation.deliveryRetryingNote')}</p>}
      {showDeliveryTargetDetails && (
        <ul className="automation-run-delivery-targets" aria-label={t('automation.delivery')}>
          {deliveryTargets.map((tgt, idx) => {
            const matched = matchAttemptForTarget(tgt, attempts);
            const displayName = deliveryTargetName(tgt, t, deliveryNames, matched);
            const statusLabel =
              tgt.status === 'sent'
                ? t('automation.deliveredVia', { method: displayName })
                : tgt.status === 'retrying'
                  ? t('automation.deliveryRetryingVia', { method: displayName })
                  : t('automation.deliveryFailedVia', { method: displayName });
            const reason =
              tgt.status === 'failed'
                ? formatDeliveryFailureReason(tgt.error_code, tgt.error_message, t) ?? tgt.error_message
                : null;
            const line = reason
              ? t('automation.deliveryNamedProblem', { name: statusLabel, message: reason })
              : statusLabel;
            return (
              <li
                key={`${tgt.channel ?? ''}|${tgt.target ?? ''}|${idx}`}
                className={
                  tgt.status === 'failed'
                    ? 'automation-mono automation-run-error'
                    : 'automation-mono automation-run-note'
                }
              >
                {line}
              </li>
            );
          })}
        </ul>
      )}
      {/* DALTON-HOOK(execution-delivery-status): end of per-target delivery details */}
      {view.noteKey && <p className="automation-run-note">{t(view.noteKey)}</p>}
      {(view.waiting || onRetry || onResume || links.length > 0 || hasThread) && (
        <div className="automation-run-actions">
          {/* DALTON-HOOK(company-automations): company run reports omit onSkip */}
          {view.waiting && onSkip && (
            <HeaderButton variant="secondary" disabled={busy} onClick={onSkip}>
              {t('automation.skipRun')}
            </HeaderButton>
          )}
          {onRetry && (
            <HeaderButton variant="secondary" disabled={busy} onClick={onRetry}>
              {t('common.retry')}
            </HeaderButton>
          )}
          {onResume && (
            <HeaderButton variant="secondary" disabled={busy} onClick={onResume}>
              {t('automation.resume')}
            </HeaderButton>
          )}
          {links.map((l) => (
            <span key={`${l.url}|${l.label}`} className="automation-run-cause-link">
              <ErrorLink {...l} />
            </span>
          ))}
          {hasThread && (
            <button type="button" className="automation-link" onClick={onOpenThread}>
              {t(view.ui.live ? 'automation.watchLive' : 'automation.openThread')}
              <ArrowUpRight className="h-3 w-3" />
            </button>
          )}
        </div>
      )}
    </>
  );
}
