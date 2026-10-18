import React, { useState } from 'react';
import { motion } from '@/lib/framer';
import { Check, FileCheck2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import type { ToolApprovalState } from '@/types/chat';
import { SettledStep } from '../SettledStep';

interface PublishApprovalCardProps {
  data: ToolApprovalState;
  onApprove?: () => void;
  onReject?: (message?: string) => void;
}

interface UncertainItem {
  id?: string;
  raw?: string;
  line?: number;
  metric?: string;
  period?: string;
  reason?: string;
  hint?: string;
  sentence?: string;
  candidates?: Array<{ kind?: string; id?: string; value?: number; unit?: string; why?: string }>;
}

interface FigureSummary {
  verified?: number;
  contradicted?: number;
  unverified?: number;
  uncertain?: number;
  exempt?: number;
  uncertain_items?: UncertainItem[];
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
  const uncertainItems: UncertainItem[] = Array.isArray(args?.uncertain_items)
    ? (args.uncertain_items as UncertainItem[])
    : Array.isArray(figSum?.uncertain_items)
      ? (figSum?.uncertain_items as UncertainItem[])
      : [];

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

  const detailBlock = (
    <div
      className="rounded-lg px-4 py-3 text-xs space-y-1.5"
      style={{ border: '1px solid var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
    >
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
      {auditSummary && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.auditLabel', '数字核验')}</span>
          <span>{auditSummary}</span>
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
