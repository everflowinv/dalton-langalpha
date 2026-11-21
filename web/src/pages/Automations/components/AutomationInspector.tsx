// DALTON-HOOK(company-automations): company inspector adds rollback, history, and recycle actions.
import React, { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowUpRight, Clock, EyeOff, Pause, Pencil, Play, RotateCcw, Trash2, Undo2, Zap } from 'lucide-react'; // DALTON-HOOK(execution-delivery-status)
import { HeaderButton, ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useLocale } from '@/hooks/useLocale';
import { useModelName } from '@/hooks/useModelName';
import { useNow } from '@/hooks/useNow';
import { scrollMotion } from '@/lib/reducedMotion';
import { cn } from '@/lib/utils';
import type { Automation, AutomationExecution, DeliveryAttempt } from '@/types/automation';
import type { PageLinkSpec } from '@/utils/rateLimitError';
import { useModificationHistory } from '../hooks/useModificationHistory';
import { useAutomationMutations } from '../hooks/useAutomationMutations';
import { useAutomationPlace } from '../hooks/useAutomationPlace';
import { useDeliveryOptions } from '../hooks/useDeliveryOptions';
import { useExecutions } from '../hooks/useExecutions';
import { useOpenThread } from '../hooks/useOpenThread';
import type { WatchedReading } from '../hooks/useWatchedReadings';
import { useWorkspaceOptions, workspaceNameOf } from '../hooks/useWorkspaceOptions';
import {
  type DeliveryNames,
  deliveryAttemptLabel,
  deliveryAttemptName,
  deliveryEntryName,
  deliveryMethodName,
  deliveryNames,
  deliveryOutcome, // DALTON-HOOK(execution-delivery-status)
  deliveryTargetName,
  formatDeliveryFailureReason,
  matchAttemptForTarget,
} from '../utils/delivery';
import { distanceLabel } from '../utils/price';
import { scheduleSentence } from '../utils/schedule';
import { attentionLinks, automationActions, automationStatusUi, describeRun } from '../utils/status';
import { isDeletedTask, isForbiddenError } from '../utils/taskKind';
import { DeletedMeta, ModificationHistory, RecordTabs, RollbackDialog } from './ModificationHistory';
import { formatDateTimeShort, formatDuration } from '../utils/time';
import { PriceMeter } from './PriceMeter';
import { RunReport } from './RunReport';
import { StatusGlyph } from './StatusMark';
import './AutomationInspector.css';

/** Past this an instruction starts folded: it is what the agent was told
 *  once, and a long one should not push the run history off the pane. */
const INSTRUCTION_FOLD_CHARS = 480;
const INSTRUCTION_FOLD_LINES = 10;

// DALTON-HOOK(company-automations): company agents hide run, dismiss, and skip. Pause uses the detail version. Delete moves the task to the recycle bin.
interface AutomationInspectorProps {
  automation: Automation;
  reading: WatchedReading | undefined;
  /** The run a link or a history row opened; null reports the newest. */
  runId: string | null;
  onOpenRun: (runId: string) => void;
  onEdit: (a: Automation) => void;
  onDelete: (a: Automation) => void;
  /** Company tasks hide run, dismiss, and skip. Defaults keep personal rows unchanged. */
  allowRun?: boolean;
  allowDelete?: boolean;
  allowDismiss?: boolean;
  allowSkip?: boolean;
  /** When set, pause and resume send this `config_version` and stay off until it is known. */
  requireVersion?: boolean;
  expectedConfigVersion?: number | null;
  /** Company run list returned 403. Personal inspectors leave this unset. */
  onRunsForbidden?: (automationId: string) => void;
  /** After a recycle-bin restore succeeds. The page closes that group's bin. */
  onRestored?: () => void;
}

/**
 * One automation, read top to bottom in the order a person asks about it:
 * what is it and when does it run, what did it last find, what was it told to
 * do, how have its runs gone, and the settings nobody needs until they do.
 */
export default function AutomationInspector({
  automation: a,
  reading,
  runId,
  onOpenRun,
  onEdit,
  onDelete,
  // DALTON-HOOK(company-automations): company tasks hide run, dismiss, and skip. Pause waits for a version.
  allowRun = true,
  allowDelete = true,
  allowDismiss = true,
  allowSkip = true,
  requireVersion = false,
  expectedConfigVersion = null,
  onRunsForbidden,
  onRestored,
}: AutomationInspectorProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const modelName = useModelName();
  const now = useNow();
  const openThread = useOpenThread();
  const { pause, resume, trigger, skip, dismiss, busy } = useAutomationMutations();
  const deleted = isDeletedTask(a);
  const history = useModificationHistory(a, requireVersion, onRunsForbidden, onRestored);
  const deliveryMethods = a.delivery_config?.methods ?? [];
  const { executions, loading, error: runsError } = useExecutions(a.automation_id, deliveryMethods.length > 0, !deleted);
  const runsForbidden = requireVersion && isForbiddenError(runsError);
  const place = useAutomationPlace();
  const workspaceId = a.workspace_id;
  const workspaceName = workspaceNameOf(useWorkspaceOptions(), workspaceId);
  const delivery = useDeliveryOptions({ workspaceId, enabled: deliveryMethods.length > 0 });
  // The runs' own names, newest first, name a chat the apps no longer list.
  const names = useMemo(
    () =>
      deliveryNames(delivery.options, [
        ...(a.last_execution?.delivery_result ?? []),
        ...executions.flatMap((e) => e.delivery_result ?? []),
      ]),
    [delivery.options, a.last_execution, executions],
  );

  // DALTON-HOOK(company-automations): a company pause sends the open detail's config_version and stays off until it is known.
  const ui = automationStatusUi(a);
  const { canPause, canResume, canRun, runBusy, canDismiss } = automationActions(a);
  const versionReady = !requireVersion || expectedConfigVersion != null;
  const pauseTarget = () =>
    requireVersion && expectedConfigVersion != null
      ? { id: a.automation_id, expectedConfigVersion }
      : a.automation_id;
  const last = a.last_execution;
  // DALTON-HOOK(execution-delivery-status): prefer the polled execution record by id so detail card and history table share the freshest state
  const polledForLast = last
    ? executions.find((e) => e.automation_execution_id === last.automation_execution_id) ?? null
    : null;
  const latestRun = polledForLast
    ? {
        ...last,
        ...polledForLast,
        dismissed_at: polledForLast.dismissed_at ?? last?.dismissed_at ?? null,
      }
    : last;
  // The opened run is looked for in the polled history first, then on the
  // list row. There is no reading one run by its id, so one older than
  // that history is not fetched: the newest stands in, and says so.
  const opened = runId
    ? executions.find((e) => e.automation_execution_id === runId) ??
      (runId === last?.automation_execution_id ? last : null)
    : null;
  const placing = !!runId && !opened && loading;
  const runMissing = !!runId && !opened && !loading;
  const shownRun = opened ?? latestRun;
  const isLatest =
    shownRun != null &&
    latestRun != null &&
    shownRun.automation_execution_id === latestRun.automation_execution_id;

  // A run opened from the history below brings its report into view.
  const reportRef = useRef<HTMLElement>(null);
  const openedOnMount = useRef(runId);
  useEffect(() => {
    if (openedOnMount.current === runId) return;
    openedOnMount.current = runId;
    reportRef.current?.scrollIntoView?.({ block: 'nearest', behavior: scrollMotion() });
  }, [runId]);
  // DALTON-HOOK(company-automations): a company 403 on runs or history replaces this inspector.
  useEffect(() => {
    if (runsForbidden) onRunsForbidden?.(a.automation_id);
  }, [runsForbidden, onRunsForbidden, a.automation_id]);

  if (runsForbidden || history.historyForbidden) {
    return (
      <article className="automation-inspector">
        <p className="automations-quiet-note">{t('automation.detailForbidden')}</p>
      </article>
    );
  }

  const kicker = [t(ui.labelKey), place.label(a, workspaceName)];

  return (
    <article className="automation-inspector">
      <div className="automations-kicker flex items-center gap-1.5">
        <StatusGlyph ui={ui} size={12} />
        <span className="truncate">{kicker.join(' · ')}</span>
      </div>
      <h2 className="title-font automation-inspector-title">{a.name}</h2>
      {deleted && <DeletedMeta deletedAt={a.deleted_at} deletedBy={a.deleted_by} />}
      {a.description && <p className="automation-inspector-desc">{a.description}</p>}
      <p className="automation-inspector-schedule">{scheduleSentence(a, t, locale, now)}</p>

      {reading?.reading && (
        <div className="automation-inspector-meter">
          <div className="automation-mono automation-inspector-reading mb-1">{distanceLabel(reading.reading, t, locale)}</div>
          <PriceMeter reading={reading.reading} symbol={reading.symbol} />
        </div>
      )}

      {deleted ? (
        <div className="automation-inspector-actions">
          <HeaderButton
            variant="primary"
            icon={RotateCcw}
            disabled={busy || history.version == null}
            onClick={() => void history.restoreTask()}
          >
            {busy ? t('automation.restoring') : t('automation.restoreTask')}
          </HeaderButton>
        </div>
      ) : (
      /* DALTON-HOOK(company-automations): run now and dismiss stay off for a company task. Delete moves the task to the recycle bin. */
      <div className="automation-inspector-actions">
        {allowRun && (
          <HeaderButton
            variant="secondary"
            icon={Zap}
            disabled={busy || runBusy || !canRun}
            onClick={() => trigger.mutate(a.automation_id)}
          >
            {t('automation.runNow')}
          </HeaderButton>
        )}
        {canPause && (
          <HeaderButton variant="secondary" icon={Pause} disabled={busy || !versionReady} onClick={() => pause.mutate(pauseTarget())}>
            {t('automation.pause')}
          </HeaderButton>
        )}
        {canResume && (
          <HeaderButton variant="secondary" icon={Play} disabled={busy || !versionReady} onClick={() => resume.mutate(pauseTarget())}>
            {t('automation.resume')}
          </HeaderButton>
        )}
        {/* Only while the newest run, the one dismissed, is the one shown:
            an opened older run, or one still being placed, would leave a
            failure dismissed unseen. */}
        {allowDismiss && canDismiss && isLatest && !placing && last && (
          <HeaderButton
            variant="secondary"
            icon={EyeOff}
            disabled={busy}
            title={t('automation.dismissHint')}
            onClick={() => dismiss.mutate({ automationId: a.automation_id, executionId: last.automation_execution_id })}
          >
            {t('automation.dismiss')}
          </HeaderButton>
        )}
        {/* DALTON-HOOK(company-automations): company tasks keep pause, resume, edit, and recycle. Run, dismiss, and skip stay off. */}
        <HeaderButton variant="secondary" icon={Pencil} disabled={!versionReady} onClick={() => onEdit(a)}>
          {t('automation.edit')}
        </HeaderButton>
        {history.prevVersion != null && (
          <HeaderButton
            variant="secondary"
            icon={Undo2}
            disabled={busy || history.version == null}
            onClick={() => history.setRollbackOpen(true)}
          >
            {t('automation.rollbackButton', { version: history.prevVersion })}
          </HeaderButton>
        )}
        {allowDelete && (
          <HeaderButton
            variant="ghost"
            icon={Trash2}
            className="ml-auto hover:text-(color:--color-icon-danger)"
            disabled={busy || history.version == null}
            onClick={() => onDelete(a)}
          >
            {t('automation.moveToRecycle')}
          </HeaderButton>
        )}
      </div>
      )}

      <section ref={reportRef} className="automation-inspector-section">
        <h3 className="automations-kicker">{t(isLatest ? 'automation.latestRun' : 'automation.selectedRun')}</h3>
        {runMissing && <p className="automations-quiet-note automation-run-missing">{t('automation.runNotFound')}</p>}
        {placing ? (
          <ListSkeleton rows={2} />
        ) : shownRun ? (
          <RunCard
            execution={shownRun}
            // DALTON-HOOK(execution-delivery-status): pass known chat names for per-target delivery details in RunReport
            deliveryNames={names}
            // Only the newest run links to a fix: an older limit may be long
            // lifted, and in the feed the rail already offers the links.
            links={isLatest ? attentionLinks(a) : []}
            busy={busy}
            onOpen={() => openThread(shownRun.conversation_thread_id)}
            // DALTON-HOOK(company-automations): a company task leaves skip off.
            onSkip={
              allowSkip
                ? () => skip.mutate({ automationId: a.automation_id, executionId: shownRun.automation_execution_id })
                : undefined
            }
          />
        ) : (
          <p className="automations-quiet-note">{t('automation.noRunsForThis')}</p>
        )}
      </section>

      <section className="automation-inspector-section">
        <h3 className="automations-kicker">{t('automation.instruction')}</h3>
        <Instruction text={a.instruction ?? ''} />
      </section>

      <section className="automation-inspector-section">
        <RecordTabs value={history.recordTab} onChange={history.setRecordTab} />
        {history.recordTab === 'history' ? (
          <ModificationHistory
            history={history.history}
            loading={history.historyLoading}
            failed={history.historyFailed}
            prevVersion={history.prevVersion}
            onRollback={history.version == null ? null : () => history.setRollbackOpen(true)}
          />
        ) : loading ? (
          <ListSkeleton rows={2} />
        ) : executions.length === 0 ? (
          <p className="automations-quiet-note">{t('automation.noRunsForThis')}</p>
        ) : (
          <RunHistory
            executions={executions}
            names={names}
            shownId={placing ? null : shownRun?.automation_execution_id ?? null}
            onOpenRun={onOpenRun}
            onOpen={(threadId) => openThread(threadId)}
          />
        )}
      </section>

      {/* DALTON-HOOK(company-automations): rollback stays disabled until the target snapshot is loaded. */}
      <RollbackDialog
        open={history.rollbackOpen}
        version={history.prevVersion}
        snapshot={history.targetSnapshot}
        ready={history.snapshotReady}
        pending={history.snapshotPending}
        loading={busy}
        onOpenChange={history.setRollbackOpen}
        onConfirm={() => void history.confirmRollback()}
      />

      <section className="automation-inspector-section">
        <h3 className="automations-kicker">{t('automation.details')}</h3>
        <dl className="automation-details">
          <dt>{t('automation.detailModel')}</dt>
          <dd>{a.llm_model ? modelName(a.llm_model) : t('automation.defaultModel')}</dd>
          <dt>{t('automation.threadStrategy')}</dt>
          <dd>{t(a.thread_strategy === 'continue' ? 'automation.continueExisting' : 'automation.newThreadEachRun')}</dd>
          {workspaceName && (
            <>
              <dt>{t('automation.detailWorkspace')}</dt>
              <dd>{workspaceName}</dd>
            </>
          )}
          {deliveryMethods.length > 0 && (
            <>
              <dt>{t('automation.delivery')}</dt>
              <dd>{deliveryMethods.map((m) => deliveryEntryName(m, t, delivery.options, names)).join(', ')}</dd>
            </>
          )}
          <dt>{t('automation.detailFailures')}</dt>
          <dd>{t('automation.failuresOf', { count: a.failure_count, max: a.max_failures })}</dd>
          <dt>{t('automation.detailCreated')}</dt>
          <dd>{formatDateTimeShort(a.created_at, locale)}</dd>
        </dl>
      </section>
    </article>
  );
}

function Instruction({ text }: { text: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const long = text.length > INSTRUCTION_FOLD_CHARS || text.split('\n').length > INSTRUCTION_FOLD_LINES;
  const folded = long && !open;
  return (
    <>
      <div className={cn('automation-instruction', folded && 'is-folded')}>{text}</div>
      {long && (
        <button type="button" className="automation-link mt-2" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {t(open ? 'automation.showLess' : 'automation.showAll')}
        </button>
      )}
    </>
  );
}

// DALTON-HOOK(company-automations): skip is omitted when the open task is a company agent.
// DALTON-HOOK(execution-delivery-status): forward deliveryNames to RunReport for per-target detail rows
function RunCard({
  execution: e,
  deliveryNames,
  links,
  busy,
  onOpen,
  onSkip,
}: {
  execution: AutomationExecution;
  deliveryNames: DeliveryNames;
  links: PageLinkSpec[];
  busy: boolean;
  onOpen: () => void;
  onSkip?: () => void;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const view = describeRun(e);
  const meta = [t(view.ui.labelKey), formatDateTimeShort(e.started_at ?? e.scheduled_at, locale)];
  if (view.showDuration) meta.push(formatDuration(e.started_at, e.completed_at, t));
  return (
    <div className="automation-report">
      <RunReport
        run={e}
        view={view}
        meta={meta}
        layout="report"
        busy={busy}
        onSkip={onSkip}
        onOpenThread={onOpen}
        links={links}
        // DALTON-HOOK(execution-delivery-status): pass deliveryNames to RunReport
        deliveryNames={deliveryNames}
      />
    </div>
  );
}

/** A delivery recorded before runs said where and how it landed. */
function isLegacyAttempt(d: DeliveryAttempt): boolean {
  return d.via === undefined && d.address === undefined && d.name === undefined && d.error === undefined;
}

function RunHistory({
  executions,
  names,
  shownId,
  onOpenRun,
  onOpen,
}: {
  executions: AutomationExecution[];
  names: DeliveryNames;
  /** The run the report above shows. */
  shownId: string | null;
  onOpenRun: (runId: string) => void;
  onOpen: (threadId: string | null) => void;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  return (
    <div className="automation-history-scroll">
      <table className="automation-history">
        <thead>
          <tr>
            <th>{t('automation.colResult')}</th>
            <th>{t('automation.colStarted')}</th>
            <th>{t('automation.colTook')}</th>
            <th>{t('automation.delivery')}</th>
            <th aria-label={t('automation.openThread')} />
          </tr>
        </thead>
        <tbody>
          {executions.map((e) => {
            const { ui, showDuration, deliverySummary } = describeRun(e);
            // Only a failure's label takes the glyph's color: amber marks
            // liveness on the glyph and never tints words.
            const attempts = e.delivery_result ?? [];
            // DALTON-HOOK(execution-delivery-status): prefer authoritative delivery_summary targets & failure reasons over raw delivery_result
            const explicitSummary = e.delivery_summary ?? null;
            const summaryTargets = explicitSummary?.targets ?? [];
            const useSummaryTargets = explicitSummary != null && summaryTargets.length > 0;
            const targetErrors = useSummaryTargets
              ? summaryTargets.flatMap((tgt) => {
                  if (tgt.status !== 'failed') return [];
                  const matched = matchAttemptForTarget(tgt, attempts);
                  const reason = formatDeliveryFailureReason(tgt.error_code, tgt.error_message, t) ?? tgt.error_message;
                  if (!reason) return [];
                  return [
                    t('automation.deliveryNamedProblem', {
                      name: deliveryTargetName(tgt, t, names, matched),
                      message: reason,
                    }),
                  ];
                })
              : explicitSummary != null
                ? explicitSummary.status === 'failed' || explicitSummary.status === 'partial_failed'
                  ? (() => {
                      const reason = formatDeliveryFailureReason(
                        explicitSummary.failure_code,
                        explicitSummary.failure_reason,
                        t,
                      );
                      return reason ? [reason] : [];
                    })()
                  : []
                : attempts.flatMap((d) => {
                    if (deliveryOutcome(d) !== 'failed' || !d.error) return [];
                    const localized = formatDeliveryFailureReason(null, d.error, t) ?? d.error;
                    return [t('automation.deliveryNamedProblem', { name: deliveryAttemptName(d, t, names), message: localized })];
                  });
            // DALTON-HOOK(execution-delivery-status): fall back to top-level summary error when target rows lack specific reasons
            const summaryFallbackError =
              useSummaryTargets &&
              targetErrors.length === 0 &&
              (explicitSummary.status === 'failed' || explicitSummary.status === 'partial_failed')
                ? formatDeliveryFailureReason(explicitSummary.failure_code, explicitSummary.failure_reason, t)
                : null;
            const errors = [
              ...(ui.danger && e.error_message ? [e.error_message] : []),
              ...targetErrors,
              ...(summaryFallbackError ? [summaryFallbackError] : []),
            ];
            const deliverySummaryLabelKey =
              deliverySummary?.status === 'failed'
                ? 'automation.deliveryStatusFailed'
                : deliverySummary?.status === 'partial_failed'
                  ? 'automation.deliveryStatusPartialFailed'
                  : deliverySummary?.status === 'retrying'
                    ? 'automation.deliveryStatusRetrying'
                    : deliverySummary?.status === 'delivered'
                      ? 'automation.deliveryStatusDelivered'
                      : null;
            const shown = e.automation_execution_id === shownId;
            return (
              <Fragment key={e.automation_execution_id}>
                <tr className={errors.length ? 'has-error' : undefined}>
                  <td>
                    <span className="inline-flex items-center gap-1.5">
                      <StatusGlyph ui={ui} size={12} />
                      <span style={{ color: ui.danger ? ui.color : undefined }}>{t(ui.labelKey)}</span>
                    </span>
                  </td>
                  <td className="automation-mono">
                    {/* Opens this run in the report above, and in the link. */}
                    <button
                      type="button"
                      className="automation-link automation-history-run"
                      aria-current={shown ? 'true' : undefined}
                      onClick={() => onOpenRun(e.automation_execution_id)}
                    >
                      {formatDateTimeShort(e.started_at ?? e.scheduled_at, locale)}
                    </button>
                  </td>
                  <td className="automation-mono">{showDuration ? formatDuration(e.started_at, e.completed_at, t) : ''}</td>
                  <td className="automation-history-delivery">
                    {/* DALTON-HOOK(execution-delivery-status): render per-target delivery_summary status when available */}
                    {useSummaryTargets
                      ? summaryTargets.map((tgt, i) => {
                          const matched = matchAttemptForTarget(tgt, attempts);
                          const displayName = deliveryTargetName(tgt, t, names, matched);
                          const labelText =
                            tgt.status === 'sent'
                              ? matched && matched.success
                                ? isLegacyAttempt(matched)
                                  ? deliveryMethodName(matched.method, t)
                                  : deliveryAttemptLabel(matched, t, names)
                                : t('automation.deliveredVia', { method: displayName })
                              : tgt.status === 'retrying'
                                ? t('automation.deliveryRetryingVia', { method: displayName })
                                : t('automation.deliveryFailedVia', { method: displayName });
                          return (
                            <span key={`${tgt.channel ?? ''}|${tgt.target ?? ''}|${i}`} className="automation-history-attempt">
                              {tgt.status === 'failed' && (
                                <AlertCircle className="h-3 w-3 shrink-0" style={{ color: 'var(--color-icon-danger)' }} aria-label={t('automation.runFailed')} />
                              )}
                              {tgt.status === 'retrying' && (
                                <Clock className="h-3 w-3 shrink-0" style={{ color: 'var(--color-accent-primary)' }} aria-label={t('automation.deliveryStatusRetrying')} />
                              )}
                              {labelText}
                            </span>
                          );
                        })
                      : explicitSummary != null
                        ? deliverySummaryLabelKey && (
                            <span className="automation-history-attempt">
                              {(explicitSummary.status === 'failed' || explicitSummary.status === 'partial_failed') && (
                                <AlertCircle className="h-3 w-3 shrink-0" style={{ color: 'var(--color-icon-danger)' }} aria-label={t('automation.deliveryStatusFailed')} />
                              )}
                              {explicitSummary.status === 'retrying' && (
                                <Clock className="h-3 w-3 shrink-0" style={{ color: 'var(--color-accent-primary)' }} aria-label={t('automation.deliveryStatusRetrying')} />
                              )}
                              {t(deliverySummaryLabelKey)}
                            </span>
                          )
                        : attempts.length > 0
                          ? attempts.map((d, i) => {
                              // DALTON-HOOK(execution-delivery-status): distinguish retrying from failed on raw delivery_result attempts
                              const outcome = deliveryOutcome(d);
                              return (
                                <span key={`${d.method}|${d.address ?? ''}|${i}`} className="automation-history-attempt">
                                  {outcome === 'failed' && (
                                    <AlertCircle className="h-3 w-3 shrink-0" style={{ color: 'var(--color-icon-danger)' }} aria-label={t('automation.runFailed')} />
                                  )}
                                  {outcome === 'retrying' && (
                                    <Clock className="h-3 w-3 shrink-0" style={{ color: 'var(--color-accent-primary)' }} aria-label={t('automation.deliveryStatusRetrying')} />
                                  )}
                                  {/* Where it landed and how; an older run says only which app. */}
                                  {isLegacyAttempt(d) ? deliveryMethodName(d.method, t) : deliveryAttemptLabel(d, t, names)}
                                </span>
                              );
                            })
                          : deliverySummaryLabelKey && (
                              <span className="automation-history-attempt">
                                {(deliverySummary?.status === 'failed' || deliverySummary?.status === 'partial_failed') && (
                                  <AlertCircle className="h-3 w-3 shrink-0" style={{ color: 'var(--color-icon-danger)' }} aria-label={t('automation.deliveryStatusFailed')} />
                                )}
                                {t(deliverySummaryLabelKey)}
                              </span>
                            )}
                  </td>
                  <td className="text-right">
                    {e.conversation_thread_id && (
                      <button type="button" className="automation-link" onClick={() => onOpen(e.conversation_thread_id)}>
                        {t('automation.open')}
                        <ArrowUpRight className="h-3 w-3" />
                      </button>
                    )}
                  </td>
                </tr>
                {errors.length > 0 && (
                  <tr className="automation-history-error">
                    <td colSpan={5}>
                      {errors.map((error, i) => (
                        <p key={i} className="automation-mono automation-run-error" title={error}>
                          {error}
                        </p>
                      ))}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
