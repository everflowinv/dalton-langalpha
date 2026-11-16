import React, { useState } from 'react';
import { motion } from '@/lib/framer';
import { Check, ChevronDown, ChevronRight, FileCheck2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { Input } from '@/components/ui/input';
import { Loader } from '@/components/ui/loader';
import { DaltonInlineNotice } from '@/components/dalton/primitives';
import { TWEEN } from '@/lib/motion'; // DALTON-HOOK(publish-card): house motion vocabulary replaces the literal tween
import type { ToolApprovalState } from '@/types/chat';
import { SettledStep } from '../SettledStep';

interface PublishApprovalCardProps {
  data: ToolApprovalState;
  onApprove?: () => void;
  onReject?: (message?: string) => void;
}

interface CandidateSourceItem {
  kind?: string;
  id?: string;
  source_id?: string;
  ref?: string;
  text?: string;
  value?: number;
  unit?: string;
  why?: string;
}

interface AuditFigureItem {
  id?: string;
  raw?: string;
  line?: number;
  metric?: string;
  incidental_category?: string;
  category?: string;
  period?: string;
  reason?: string;
  hint?: string;
  sentence?: string;
  matched_source?: string;
  ref?: string;
  quote?: string;
  quote_metric_raw?: string;
  candidates?: CandidateSourceItem[];
}

interface FailedBatchDetail {
  batch_index?: number;
  start_fig?: number;
  end_fig?: number;
  section?: string;
}

interface LabelingInfo {
  total?: number;
  labeled?: number;
  batches?: number;
  failed_batches?: number;
  failed_batch_details?: FailedBatchDetail[];
  incidental_excluded?: number;
  // DALTON-HOOK(heavy-compute-isolation): set when the audit's finalize phase
  // was killed at its grace and the derived-matching pass was skipped.
  derived_matching_incomplete?: boolean;
}

interface VerifiedByCounts {
  footnote?: number;
  llm_judge?: number;
  derived?: number;
}

interface FigureSummary {
  verified?: number;
  contradicted?: number;
  unverified?: number;
  uncertain?: number;
  exempt?: number;
  incidental?: number;
  labeling_incomplete?: boolean;
  labeling?: LabelingInfo;
  verified_by_counts?: VerifiedByCounts;
  uncertain_items?: AuditFigureItem[];
  unverified_items?: AuditFigureItem[];
  llm_verified_items?: AuditFigureItem[];
  incidental_items?: AuditFigureItem[];
}

interface ReviewerPayload {
  model?: string;
  family?: string;
  verdict?: string;
  summary?: string;
  points?: string[];
}

const KNOWN_REPORT_KINDS = new Set([
  'earnings',
  'earnings_review',
  'initiation',
  'topic',
  'update',
]);

export function PublishApprovalCard({
  data,
  onApprove,
  onReject,
}: PublishApprovalCardProps): React.ReactElement {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [incidentalExpanded, setIncidentalExpanded] = useState(false);
  const [techExpanded, setTechExpanded] = useState(false);
  const { status, args } = data;
  const isApproved = status === 'approved';
  const isRejected = status === 'rejected';

  const requestId = String(args?.request_id ?? '');
  const title = String(
    args?.title ?? args?.slug ?? t('dalton.publish.defaultTitle'),
  );
  const ticker = String(args?.ticker ?? args?.company ?? args?.['公司'] ?? '');
  const rawKind = String(args?.kind ?? args?.report_type ?? args?.['类型'] ?? '');
  const kindLabel = rawKind
    ? KNOWN_REPORT_KINDS.has(rawKind)
      ? t(`dalton.publish.kindNames.${rawKind}`)
      : rawKind
    : '';
  const draftPath = String(args?.draft_path ?? args?.['草稿'] ?? '');
  const publishedPath = String(args?.published_path ?? args?.['发布为'] ?? '');

  const figSum = (
    typeof args?.figure_summary === 'object' && args?.figure_summary !== null
      ? (args.figure_summary as FigureSummary)
      : null
  );
  const labeling = figSum?.labeling;
  const failedBatches = Number(labeling?.failed_batches ?? 0);
  const failedBatchDetails: FailedBatchDetail[] = Array.isArray(labeling?.failed_batch_details)
    ? labeling.failed_batch_details
    : [];
  const labelingIncomplete = Boolean(
    args?.labeling_incomplete ?? figSum?.labeling_incomplete ?? failedBatches > 0,
  );
  // DALTON-HOOK(heavy-compute-isolation): the audit's finalize phase killed
  // at its grace skips the derived-matching pass; the PM must see why those
  // figures read unverified.
  const derivedMatchingIncomplete = Boolean(labeling?.derived_matching_incomplete);
  const hasLabelingCoverage =
    typeof labeling?.labeled === 'number' && typeof labeling?.total === 'number';

  const verifiedByCounts = figSum?.verified_by_counts;

  const uncertainItems: AuditFigureItem[] = Array.isArray(args?.uncertain_items)
    ? (args.uncertain_items as AuditFigureItem[])
    : Array.isArray(figSum?.uncertain_items)
      ? (figSum?.uncertain_items as AuditFigureItem[])
      : [];

  const unverifiedItems: AuditFigureItem[] = Array.isArray(args?.unverified_items)
    ? (args.unverified_items as AuditFigureItem[])
    : Array.isArray(figSum?.unverified_items)
      ? (figSum?.unverified_items as AuditFigureItem[])
      : [];

  const llmVerifiedItems: AuditFigureItem[] = Array.isArray(args?.llm_verified_items)
    ? (args.llm_verified_items as AuditFigureItem[])
    : Array.isArray(figSum?.llm_verified_items)
      ? (figSum?.llm_verified_items as AuditFigureItem[])
      : [];

  const incidentalItems: AuditFigureItem[] = Array.isArray(args?.incidental_items)
    ? (args.incidental_items as AuditFigureItem[])
    : Array.isArray(figSum?.incidental_items)
      ? (figSum?.incidental_items as AuditFigureItem[])
      : [];
  const incidentalCount = incidentalItems.length || Number(figSum?.incidental ?? 0);

  const auditSummary = figSum
    ? t('dalton.publish.auditSummaryLine', {
        verified: figSum.verified ?? 0,
        contradicted: figSum.contradicted ?? 0,
        unverified: figSum.unverified ?? 0,
        uncertain: figSum.uncertain ?? 0,
        exempt: figSum.exempt ?? 0,
      })
    : String(args?.audit_summary ?? args?.audit ?? args?.['数字核验'] ?? '');

  const revObj = (
    typeof args?.reviewer === 'object' && args?.reviewer !== null
      ? (args.reviewer as ReviewerPayload)
      : null
  );
  const verdictLabel = revObj
    ? revObj.verdict === 'pass' || !revObj.verdict
      ? t('dalton.publish.verdictPass')
      : revObj.verdict
    : '';
  const reviewerSummary = revObj
    ? `${verdictLabel}${revObj.model ? ` · ${revObj.model}` : ''}`
    : String(args?.reviewer_summary ?? args?.reviewer ?? args?.['独立审稿'] ?? '');
  const reviewerPoints: string[] = Array.isArray(revObj?.points) ? revObj.points : [];

  const updatesThesis = args?.updates_thesis;
  const thesisVersion =
    args?.thesis_version ??
    args?.['thesis 正式版本'] ??
    (typeof updatesThesis === 'boolean'
      ? updatesThesis
        ? t('dalton.publish.thesisYes')
        : t('dalton.publish.thesisNo')
      : undefined);

  const hasTechDetails = Boolean(requestId || draftPath || publishedPath || revObj?.family);

  const renderCandidatesList = (candidates?: CandidateSourceItem[]) => {
    const cands = Array.isArray(candidates) ? candidates.slice(0, 5) : [];
    if (cands.length === 0) return null;
    return (
      <div className="pl-3 pt-1 space-y-0.5 text-[11px]" data-testid="publish-item-candidates">
        {cands.map((cand, cIdx) => {
          const srcLabel = cand.source_id || cand.id || cand.ref || cand.kind || 'source';
          const snippet = cand.text || cand.why || '';
          const valPart =
            cand.value !== undefined
              ? `${cand.value}${cand.unit ? ` ${cand.unit}` : ''}`
              : '';
          return (
            <div
              key={`${srcLabel}-${cIdx}`}
              className="flex flex-wrap items-baseline gap-1.5"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              <code className="font-mono">[{srcLabel}]</code>
              {valPart && <span className="font-medium tabular-nums">{valPart}</span>}
              {snippet && <span className="truncate">{snippet}</span>}
            </div>
          );
        })}
      </div>
    );
  };

  const detailBlock = (
    <div
      className="rounded-lg px-4 py-3 text-xs space-y-1.5"
      style={{ border: '1px solid var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
    >
      {(labelingIncomplete || derivedMatchingIncomplete) && (
        <DaltonInlineNotice
          tone="warning"
          testId="publish-labeling-incomplete-warning"
          className="px-3 py-1.5 text-[11px]"
        >
          <div className="space-y-1 font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {labelingIncomplete && (
              <div>
                {t('dalton.publish.labelingIncompleteWarning')}
                {failedBatches > 0
                  ? t('dalton.publish.failedBatchesCount', { count: failedBatches })
                  : ''}
              </div>
            )}
            {derivedMatchingIncomplete && (
              <div data-testid="publish-derived-incomplete-warning">
                {t('dalton.publish.derivedMatchingIncompleteWarning')}
              </div>
            )}
            {failedBatchDetails.length > 0 && (
              <div className="text-[11px] font-normal space-y-0.5" data-testid="publish-failed-batch-details">
                {failedBatchDetails.map((d, dIdx) => {
                  const batchNo = d.batch_index ?? dIdx + 1;
                  const startFig = d.start_fig ?? '?';
                  const endFig = d.end_fig ?? '?';
                  const isColloquialScan = Number(startFig) === 0 && Number(endFig) === 0;
                  return (
                    <div key={`${batchNo}-${dIdx}`}>
                      {isColloquialScan
                        ? t('dalton.publish.failedBatchColloquialItem', { batch: batchNo })
                        : t('dalton.publish.failedBatchDetailItem', {
                            batch: batchNo,
                            start: startFig,
                            end: endFig,
                          })}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </DaltonInlineNotice>
      )}
      <div className="flex items-center justify-between gap-2">
        <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.titleLabel')}</span>
        <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {ticker ? `[${ticker}] ` : ''}{title}
        </span>
      </div>
      {kindLabel && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.kindLabel')}</span>
          <span>{kindLabel}</span>
        </div>
      )}
      {hasLabelingCoverage && (
        <div
          className="flex items-center justify-between gap-2"
          data-testid="publish-labeling-coverage"
        >
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.publish.labelingCoverageLabel')}
          </span>
          <span className="tabular-nums">
            {t('dalton.publish.labelingCoverageValue', {
              labeled: labeling?.labeled,
              total: labeling?.total,
            })}
          </span>
        </div>
      )}
      {auditSummary && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.auditLabel')}</span>
          <span className="tabular-nums">{auditSummary}</span>
        </div>
      )}
      {verifiedByCounts && (
        <div
          className="flex items-center justify-between gap-2 text-[11px]"
          data-testid="publish-verified-breakdown"
        >
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.publish.verifiedBreakdownLabel')}
          </span>
          <span className="tabular-nums">
            {t('dalton.publish.verifiedBreakdownValue', {
              footnote: verifiedByCounts.footnote ?? 0,
              llm_judge: verifiedByCounts.llm_judge ?? 0,
              derived: verifiedByCounts.derived ?? 0,
            })}
          </span>
        </div>
      )}
      {reviewerSummary && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.reviewerLabel')}</span>
          <span>{reviewerSummary}</span>
        </div>
      )}
      {revObj?.summary && (
        <div className="pt-1 text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
          {revObj.summary}
        </div>
      )}
      {reviewerPoints.length > 0 && (
        <ul className="list-disc pl-4 space-y-0.5 text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
          {reviewerPoints.slice(0, 4).map((pt, idx) => (
            <li key={idx}>{pt}</li>
          ))}
        </ul>
      )}
      {thesisVersion !== undefined && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.thesisLabel')}</span>
          <span>{String(thesisVersion)}</span>
        </div>
      )}
      {incidentalCount > 0 && (
        <div
          className="mt-2 rounded px-3 py-1.5"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-incidental-section"
        >
          <button
            type="button"
            onClick={() => setIncidentalExpanded((prev) => !prev)}
            className="w-full flex items-center justify-between text-left font-medium text-[11px] cursor-pointer"
            style={{ color: 'var(--color-text-primary)' }}
            data-testid="publish-incidental-toggle"
          >
            <span>
              {t('dalton.publish.incidentalHeader', { count: incidentalCount })}
            </span>
            {incidentalExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0" />
            )}
          </button>
          <Disclosure open={incidentalExpanded && incidentalItems.length > 0}>
            <div className="pt-1.5 space-y-0.5 text-[11px]" data-testid="publish-incidental-list">
              {incidentalItems.map((item, idx) => {
                const cat =
                  item.incidental_category ||
                  item.category ||
                  t('dalton.publish.incidentalCategoryMissing');
                return (
                  <div key={item.id ?? idx} style={{ color: 'var(--color-text-tertiary)' }}>
                    <span>{t('dalton.publish.lineNumber', { line: item.line ?? '?' })}</span> ·{' '}
                    <strong>{item.raw}</strong>
                    {cat ? ` [${cat}]` : ''}
                    {item.sentence ? ` — ${item.sentence}` : ''}
                  </div>
                );
              })}
            </div>
          </Disclosure>
        </div>
      )}
      {llmVerifiedItems.length > 0 && (
        <div
          className="mt-2 rounded px-3 py-2 space-y-1.5"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-llm-verified-items"
        >
          <div className="font-medium text-[11px]" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.publish.llmVerifiedHeader')}（{llmVerifiedItems.length}）
          </div>
          {llmVerifiedItems.map((item, idx) => (
            <div
              key={item.id ?? idx}
              className="grid grid-cols-1 md:grid-cols-2 gap-2 rounded p-2 text-[11px]"
              style={{ border: '1px solid var(--color-border-muted)' }}
              data-testid="publish-llm-verified-row"
            >
              <div className="space-y-0.5">
                <div className="font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.draftSentenceLabel')}
                </div>
                <div>
                  <span>{t('dalton.publish.lineNumber', { line: item.line ?? '?' })}</span> ·{' '}
                  <strong>{item.raw}</strong>
                  {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
                </div>
                {item.sentence && (
                  <div style={{ color: 'var(--color-text-primary)' }}>{item.sentence}</div>
                )}
              </div>
              <div className="space-y-0.5">
                <div className="font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.sourceQuoteLabel')}
                  {(item.matched_source || item.ref) ? ` · ${item.matched_source || item.ref}` : ''}
                </div>
                {item.quote_metric_raw && (
                  <div>
                    <code className="font-mono">{item.quote_metric_raw}</code>
                  </div>
                )}
                {item.quote && (
                  <div style={{ color: 'var(--color-text-primary)' }}>{item.quote}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {unverifiedItems.length > 0 && (
        <div
          className="mt-2 rounded px-3 py-2 space-y-1.5"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-unverified-items"
        >
          <div className="font-medium text-[11px]" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.publish.unverifiedHeader')}（{unverifiedItems.length}）
          </div>
          {unverifiedItems.map((item, idx) => (
            <div key={item.id ?? idx} className="text-[11px] space-y-0.5">
              <div>
                <span>{t('dalton.publish.lineNumber', { line: item.line ?? '?' })}</span> ·{' '}
                <strong>{item.raw}</strong>
                {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
              </div>
              {item.sentence && (
                <div className="truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.originalSentenceLabel', { text: item.sentence })}
                </div>
              )}
              {item.hint && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.reasonLabel', { text: item.hint })}
                </div>
              )}
              {renderCandidatesList(item.candidates)}
            </div>
          ))}
        </div>
      )}
      {uncertainItems.length > 0 && (
        <div
          className="mt-2 rounded px-3 py-2 space-y-1"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-uncertain-items"
        >
          <div className="font-medium text-[11px]" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.publish.uncertainHeader')}（{uncertainItems.length}）
          </div>
          {uncertainItems.map((item, idx) => (
            <div key={item.id ?? idx} className="text-[11px] space-y-0.5">
              <div>
                <span>{t('dalton.publish.lineNumber', { line: item.line ?? '?' })}</span> ·{' '}
                <strong>{item.raw}</strong>
                {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
              </div>
              {item.sentence && (
                <div className="truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.originalSentenceLabel', { text: item.sentence })}
                </div>
              )}
              {item.hint && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.reasonLabel', { text: item.hint })}
                </div>
              )}
              {renderCandidatesList(item.candidates)}
            </div>
          ))}
        </div>
      )}
      {hasTechDetails && (
        <div className="pt-1" data-testid="publish-technical-details">
          <button
            type="button"
            onClick={() => setTechExpanded((prev) => !prev)}
            className="inline-flex items-center gap-1 text-[11px] cursor-pointer"
            style={{ color: 'var(--color-text-tertiary)' }}
            data-testid="publish-technical-toggle"
          >
            {techExpanded ? (
              <ChevronDown className="h-3 w-3 shrink-0" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0" />
            )}
            <span>{t('dalton.publish.moreInfoToggle')}</span>
          </button>
          <Disclosure open={techExpanded}>
            <div
              className="mt-1.5 rounded px-3 py-2 space-y-1 text-[11px]"
              style={{
                backgroundColor: 'var(--color-bg-subtle)',
                border: '1px solid var(--color-border-muted)',
              }}
            >
              {requestId && (
                <div className="flex items-center justify-between gap-2">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('dalton.publish.requestIdLabel')}
                  </span>
                  <code className="font-mono">{requestId.slice(0, 12)}</code>
                </div>
              )}
              {draftPath && (
                <div className="flex items-center justify-between gap-2">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('dalton.publish.draftLabel')}
                  </span>
                  <code className="font-mono truncate">{draftPath}</code>
                </div>
              )}
              {publishedPath && (
                <div className="flex items-center justify-between gap-2">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('dalton.publish.publishedPathLabel')}
                  </span>
                  <code className="font-mono truncate">{publishedPath}</code>
                </div>
              )}
              {revObj?.family && (
                <div className="flex items-center justify-between gap-2">
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('dalton.publish.reviewerLabel')}
                  </span>
                  <code className="font-mono">{revObj.family}</code>
                </div>
              )}
            </div>
          </Disclosure>
        </div>
      )}
    </div>
  );

  if (isApproved || isRejected) {
    return (
      <SettledStep
        verdict={isApproved ? 'approved' : 'rejected'}
        reason={isRejected ? data.reason : null}
        label={
          isApproved
            ? t('dalton.publish.approvedLabel', { title })
            : t('dalton.publish.rejectedLabel', { title })
        }
      >
        {detailBlock}
      </SettledStep>
    );
  }

  const canAct = !!onApprove || !!onReject;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={TWEEN.enter}
      data-testid="publish-approval-card"
    >
      <div className="flex items-center gap-3 pb-3">
        <div
          className="h-8 w-8 rounded-md flex items-center justify-center shrink-0"
          style={{ backgroundColor: 'var(--color-bg-subtle)', color: 'var(--color-text-primary)' }}
        >
          <FileCheck2 className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[0.9375rem] font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.publish.cardTitle')}
          </div>
          <div className="text-xs truncate" style={{ color: 'var(--color-text-tertiary)' }}>
            {ticker ? `${ticker} · ` : ''}{title}
          </div>
        </div>
        {canAct && <Loader size={14} className="ml-auto shrink-0 text-(--color-icon-muted)" />}
      </div>

      {detailBlock}

      {canAct && (
        <div className="pt-3 flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              onApprove?.();
            }}
            disabled={!onApprove}
            className="gap-1.5"
            style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
          >
            <Check className="h-3.5 w-3.5 stroke-[2.5]" />
            {t('dalton.publish.approveBtn')}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              onReject?.(reason.trim() || undefined);
            }}
            disabled={!onReject}
            className="gap-1.5"
          >
            <X className="h-3.5 w-3.5" />
            {t('dalton.publish.rejectBtn')}
          </Button>
          <Input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={200}
            placeholder={t('dalton.publish.reasonPlaceholder')}
            aria-label={t('dalton.publish.reasonPlaceholder')}
            className="flex-1 min-w-40 h-8 text-xs"
          />
        </div>
      )}
    </motion.div>
  );
}
