import React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import type { ReportDeliveryPrecheckResponse } from '@/lib/dalton/reportDeliveryConfirmation';

export interface ReportDeliveryConfirmDialogProps {
  open: boolean;
  precheck: ReportDeliveryPrecheckResponse | null;
  loading: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

function TargetList({
  label,
  targets,
  labels,
  emptyText,
}: {
  label: string;
  targets: string[] | null;
  labels: Record<string, string>;
  emptyText: string;
}) {
  return (
    <div className="space-y-1 text-sm">
      <div className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>
        {label}
      </div>
      {targets && targets.length > 0 ? (
        <ul className="list-disc pl-5 space-y-0.5" style={{ color: 'var(--color-text-primary)' }}>
          {targets.map((t) => (
            <li key={t} className="break-all">
              {labels[t] || t}
            </li>
          ))}
        </ul>
      ) : (
        <div style={{ color: 'var(--color-text-tertiary)' }}>{emptyText}</div>
      )}
    </div>
  );
}

export default function ReportDeliveryConfirmDialog({
  open,
  precheck,
  loading,
  error,
  onCancel,
  onConfirm,
}: ReportDeliveryConfirmDialogProps) {
  const { t } = useTranslation();

  if (!precheck) return null;

  const isCreate = precheck.action === 'create';
  const labels = precheck.target_labels || {};

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !loading) onCancel(); }}>
      <DialogContent
        className="sm:max-w-lg border"
        style={{
          backgroundColor: 'var(--color-shell-float, var(--color-bg-elevated))',
          borderColor: 'var(--color-shell-edge, var(--color-border-elevated))',
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
            <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: 'var(--color-warning)' }} />
            {t('automation:reportDeliveryConfirmTitle', '确认定时报告外发对象')}
          </DialogTitle>
          <DialogDescription style={{ color: 'var(--color-text-secondary)' }}>
            {t(
              'automation:reportDeliveryConfirmDesc',
              '本次配置包含群聊/频道或非本人私信接收对象。请核对以下信息并确认后再提交。',
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-2">
          <div className="grid grid-cols-[5.5rem_1fr] gap-y-1.5 text-sm">
            <span style={{ color: 'var(--color-text-secondary)' }}>
              {t('automation:reportDeliveryConfirmTask', '任务名称')}
            </span>
            <span className="font-medium break-words" style={{ color: 'var(--color-text-primary)' }}>
              {precheck.automation_name}
              {precheck.config_version != null ? ` (v${precheck.config_version})` : ''}
            </span>
            <span style={{ color: 'var(--color-text-secondary)' }}>
              {t('automation:reportDeliveryConfirmProfile', '报告类型')}
            </span>
            <span className="font-mono text-xs self-center" style={{ color: 'var(--color-text-primary)' }}>
              {precheck.profile}
            </span>
          </div>

          <div
            className="rounded-md border p-3 space-y-2.5"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <TargetList
              label={t('automation:reportDeliveryConfirmReportTargets', '报告卡片目标')}
              targets={precheck.report_targets}
              labels={labels}
              emptyText={t('automation:reportDeliveryTargetNone', '无')}
            />
            <TargetList
              label={t('automation:reportDeliveryConfirmTextTargets', '纯文本目标')}
              targets={precheck.text_targets}
              labels={labels}
              emptyText={
                precheck.text_targets === null
                  ? t('automation:reportDeliveryTargetUnset', '未设置')
                  : t('automation:reportDeliveryTargetNone', '无')
              }
            />
            <TargetList
              label={t('automation:reportDeliveryConfirmFailureTargets', '失败告警目标')}
              targets={precheck.failure_targets}
              labels={labels}
              emptyText={t('automation:reportDeliveryTargetNone', '无')}
            />
          </div>

          {error && (
            <p className="text-sm" role="alert" style={{ color: 'var(--color-destructive, #dc2626)' }}>
              {error}
            </p>
          )}
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button
            type="button"
            variant="ghost"
            onClick={onCancel}
            disabled={loading}
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            style={{
              backgroundColor: 'var(--color-btn-primary-bg)',
              color: 'var(--color-btn-primary-text)',
            }}
          >
            {loading
              ? t('common.saving')
              : isCreate
                ? t('automation:reportDeliveryConfirmCreate', '确认并创建')
                : t('automation:reportDeliveryConfirmSave', '确认并保存')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
