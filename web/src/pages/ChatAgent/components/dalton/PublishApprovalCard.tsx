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

  const title = String(args?.title ?? args?.slug ?? '研究报告发布审批');
  const ticker = String(args?.ticker ?? args?.company ?? '');
  const kind = String(args?.kind ?? args?.report_type ?? '');
  const draftPath = String(args?.draft_path ?? '');
  const auditSummary = String(args?.audit_summary ?? args?.audit ?? '');
  const reviewerSummary = String(args?.reviewer_summary ?? args?.reviewer ?? '');
  const thesisVersion = args?.thesis_version ?? args?.updates_thesis;

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
      {thesisVersion !== undefined && (
        <div className="flex items-center justify-between gap-2">
          <span style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.publish.thesisLabel', '更新正式 Thesis')}</span>
          <span>{String(thesisVersion)}</span>
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
