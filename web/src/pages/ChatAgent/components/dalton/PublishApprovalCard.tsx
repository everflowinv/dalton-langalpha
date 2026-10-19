import React, { useState } from 'react';
import { motion } from '@/lib/framer';
import { Check, ChevronDown, ChevronRight, FileCheck2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
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
  batch?: number;
  batch_index?: number;
  start_figure?: number;
  start_fig?: number;
  end_figure?: number;
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

export function PublishApprovalCard({
  data,
  onApprove,
  onReject,
}: PublishApprovalCardProps): React.ReactElement {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [incidentalExpanded, setIncidentalExpanded] = useState(false);
  const { status, args } = data;
  const isApproved = status === 'approved';
  const isRejected = status === 'rejected';

  const requestId = String(args?.request_id ?? '');
  const title = String(args?.title ?? args?.slug ?? '研究报告发布审批');
  const ticker = String(args?.ticker ?? args?.company ?? args?.['公司'] ?? '');
  const kind = String(args?.kind ?? args?.report_type ?? args?.['类型'] ?? '');
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
    args?.labeling_incomplete ?? figSum?.labeling_incomplete ?? failedBatches > 0
  );
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
    ? `${figSum.verified ?? 0} 已核实 · ${figSum.contradicted ?? 0} 冲突 · ${figSum.unverified ?? 0} 未核实 · ${figSum.uncertain ?? 0} 未能判断 · ${figSum.exempt ?? 0} 豁免`
    : String(args?.audit_summary ?? args?.audit ?? args?.['数字核验'] ?? '');

  const revObj = (
    typeof args?.reviewer === 'object' && args?.reviewer !== null
      ? (args.reviewer as ReviewerPayload)
      : null
  );
  const reviewerSummary = revObj
    ? `${revObj.verdict === 'pass' ? '通过' : revObj.verdict ?? '通过'} · ${revObj.model ?? 'reviewer'}${revObj.family ? ` (${revObj.family})` : ''}`
    : String(args?.reviewer_summary ?? args?.reviewer ?? args?.['独立审稿'] ?? '');
  const reviewerPoints: string[] = Array.isArray(revObj?.points) ? revObj.points : [];

  const updatesThesis = args?.updates_thesis;
  const thesisVersion =
    args?.thesis_version ??
    args?.['thesis 正式版本'] ??
    (typeof updatesThesis === 'boolean' ? (updatesThesis ? '是' : '不更新') : undefined);

  const renderCandidatesList = (candidates?: CandidateSourceItem[]) => {
    const cands = Array.isArray(candidates) ? candidates.slice(0, 5) : [];
    if (cands.length === 0) return null;
    return (
      <div className="pl-3 pt-1 space-y-0.5 text-[10.5px]" data-testid="publish-item-candidates">
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
              <code>[{srcLabel}]</code>
              {valPart && <span className="font-medium">{valPart}</span>}
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
      {labelingIncomplete && (
        <div
          className="rounded px-3 py-1.5 text-[11px] font-medium space-y-1"
          style={{
            backgroundColor: 'rgba(245, 158, 11, 0.12)',
            border: '1px solid rgba(245, 158, 11, 0.35)',
            color: 'var(--color-text-primary)',
          }}
          data-testid="publish-labeling-incomplete-warning"
        >
          <div>
            {t('dalton.publish.labelingIncompleteWarning', '标注未完成，口语数字和指标可能未核验')}
            {failedBatches > 0 ? `（失败批次：${failedBatches}）` : ''}
          </div>
          {failedBatchDetails.length > 0 && (
            <div className="text-[10.5px] font-normal space-y-0.5" data-testid="publish-failed-batch-details">
              {failedBatchDetails.map((d, dIdx) => {
                const batchNo = d.batch ?? d.batch_index ?? dIdx + 1;
                const startFig = d.start_figure ?? d.start_fig ?? '?';
                const endFig = d.end_figure ?? d.end_fig ?? '?';
                return (
                  <div key={`${batchNo}-${dIdx}`}>
                    {t('dalton.publish.failedBatchDetailItem', {
                      batch: batchNo,
                      start: startFig,
                      end: endFig,
                      defaultValue: `第 ${batchNo} 批（第 ${startFig}–${endFig} 个数字）标注失败`,
                    })}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
      <div className="flex items-center justify-between gap-2">
        <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.titleLabel', '报告标题')}</span>
        <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {ticker ? `[${ticker}] ` : ''}{title}
        </span>
      </div>
      {requestId && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.requestIdLabel', '请求 ID')}</span>
          <code>{requestId.slice(0, 12)}</code>
        </div>
      )}
      {kind && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.kindLabel', '报告类型')}</span>
          <code>{kind}</code>
        </div>
      )}
      {draftPath && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.draftLabel', '草稿路径')}</span>
          <code>{draftPath}</code>
        </div>
      )}
      {publishedPath && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.publishedPathLabel', '发布路径')}</span>
          <code>{publishedPath}</code>
        </div>
      )}
      {hasLabelingCoverage && (
        <div
          className="flex items-center justify-between gap-2"
          data-testid="publish-labeling-coverage"
        >
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.publish.labelingCoverageLabel', '标注覆盖')}
          </span>
          <span>
            {t('dalton.publish.labelingCoverageValue', {
              labeled: labeling?.labeled,
              total: labeling?.total,
              defaultValue: `已标注 ${labeling?.labeled} / ${labeling?.total} 个数字`,
            })}
          </span>
        </div>
      )}
      {auditSummary && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.auditLabel', '数字核验')}</span>
          <span>{auditSummary}</span>
        </div>
      )}
      {verifiedByCounts && (
        <div
          className="flex items-center justify-between gap-2 text-[11px]"
          data-testid="publish-verified-breakdown"
        >
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.publish.verifiedBreakdownLabel', '已核实拆分')}
          </span>
          <span>
            {t('dalton.publish.verifiedBreakdownValue', {
              footnote: verifiedByCounts.footnote ?? 0,
              llm_judge: verifiedByCounts.llm_judge ?? 0,
              derived: verifiedByCounts.derived ?? 0,
              defaultValue: `脚注 ${verifiedByCounts.footnote ?? 0} · LLM 判断 ${verifiedByCounts.llm_judge ?? 0} · 推算 ${verifiedByCounts.derived ?? 0}`,
            })}
          </span>
        </div>
      )}
      {reviewerSummary && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.reviewerLabel', '独立审稿')}</span>
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
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.thesisLabel', '更新正式 Thesis')}</span>
          <span>{String(thesisVersion)}</span>
        </div>
      )}
      {incidentalCount > 0 && (
        <div
          className="mt-2 rounded px-3 py-1.5 space-y-1"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-incidental-section"
        >
          <button
            type="button"
            onClick={() => setIncidentalExpanded((prev) => !prev)}
            className="w-full flex items-center justify-between text-left font-medium text-[11px]"
            style={{ color: 'var(--color-text-primary)' }}
            data-testid="publish-incidental-toggle"
          >
            <span>
              {t('dalton.publish.incidentalHeader', {
                count: incidentalCount,
                defaultValue: `非核验对象（${incidentalCount}）`,
              })}
            </span>
            {incidentalExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0" />
            )}
          </button>
          {incidentalExpanded && incidentalItems.length > 0 && (
            <div className="pt-1 space-y-0.5 text-[11px]" data-testid="publish-incidental-list">
              {incidentalItems.map((item, idx) => {
                const cat = item.incidental_category || item.category || item.metric || '';
                return (
                  <div key={item.id ?? idx} style={{ color: 'var(--color-text-tertiary)' }}>
                    <code>L{item.line ?? '?'}</code> · <strong>{item.raw}</strong>
                    {cat ? ` [${cat}]` : ''}
                    {item.sentence ? ` — ${item.sentence}` : ''}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
      {llmVerifiedItems.length > 0 && (
        <div
          className="mt-2 rounded px-3 py-2 space-y-1.5"
          style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}
          data-testid="publish-llm-verified-items"
        >
          <div className="font-medium text-[11px]" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.publish.llmVerifiedHeader', 'LLM 判断核实 — 草稿与引文对照')}（{llmVerifiedItems.length}）
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
                  {t('dalton.publish.draftSentenceLabel', '草稿原文')}
                </div>
                <div>
                  <code>L{item.line ?? '?'}</code> · <strong>{item.raw}</strong>
                  {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
                </div>
                {item.sentence && (
                  <div style={{ color: 'var(--color-text-primary)' }}>{item.sentence}</div>
                )}
              </div>
              <div className="space-y-0.5">
                <div className="font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.publish.sourceQuoteLabel', '来源引文')}
                  {(item.matched_source || item.ref) ? ` · ${item.matched_source || item.ref}` : ''}
                </div>
                {item.quote_metric_raw && (
                  <div>
                    <code>{item.quote_metric_raw}</code>
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
            {t('dalton.publish.unverifiedHeader', '未核实条目与候选来源')}（{unverifiedItems.length}）
          </div>
          {unverifiedItems.map((item, idx) => (
            <div key={item.id ?? idx} className="text-[11px] space-y-0.5">
              <div>
                <code>L{item.line ?? '?'}</code> · <strong>{item.raw}</strong>
                {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
              </div>
              {item.sentence && (
                <div className="truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                  原文：{item.sentence}
                </div>
              )}
              {item.hint && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  原因：{item.hint}
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
            {t('dalton.publish.uncertainHeader', '未能判断 — 交由 PM 决定')}（{uncertainItems.length}）
          </div>
          {uncertainItems.map((item, idx) => (
            <div key={item.id ?? idx} className="text-[11px] space-y-0.5">
              <div>
                <code>L{item.line ?? '?'}</code> · <strong>{item.raw}</strong>
                {item.metric ? ` (${item.metric}${item.period ? ` · ${item.period}` : ''})` : ''}
              </div>
              {item.sentence && (
                <div className="truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                  原文：{item.sentence}
                </div>
              )}
              {item.hint && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  原因：{item.hint}
                </div>
              )}
              {renderCandidatesList(item.candidates)}
            </div>
          ))}
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
            ? t('dalton.publish.approvedLabel', { title, defaultValue: `已批准正式发布：${title}` })
            : t('dalton.publish.rejectedLabel', { title, defaultValue: `已拒绝正式发布：${title}` })
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
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
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
            {t('dalton.publish.cardTitle', '正式研究报告发布审批（PM 终审）')}
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
          <motion.button
            type="button"
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              onApprove?.();
            }}
            disabled={!onApprove}
            className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors hover:brightness-110 disabled:opacity-50"
            style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Check className="h-3.5 w-3.5 stroke-[2.5]" />
            {t('dalton.publish.approveBtn', '批准正式发布')}
          </motion.button>
          <motion.button
            type="button"
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              onReject?.(reason.trim() || undefined);
            }}
            disabled={!onReject}
            className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors disabled:opacity-50"
            style={{ backgroundColor: 'var(--color-border-muted)', color: 'var(--color-text-tertiary)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <X className="h-3.5 w-3.5" />
            {t('dalton.publish.rejectBtn', '退回修改')}
          </motion.button>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={200}
            placeholder={t('dalton.publish.reasonPlaceholder', '批注或退回原因（可选）')}
            aria-label={t('dalton.publish.reasonPlaceholder', '批注或退回原因（可选）')}
            className="flex-1 min-w-40 text-sm px-3 py-2 rounded-md bg-transparent outline-hidden"
            style={{ border: '1px solid var(--color-border-muted)', color: 'var(--color-text-primary)' }}
          />
        </div>
      )}
    </motion.div>
  );
}
