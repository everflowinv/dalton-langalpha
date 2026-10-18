import React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import type { OpenClawSyncResponse } from '../../utils/daltonApi';

interface SyncDiffDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preview: OpenClawSyncResponse | null;
  applying: boolean;
  onConfirmApply: (force: boolean) => void;
}

export function SyncDiffDialog({
  open,
  onOpenChange,
  preview,
  applying,
  onConfirmApply,
}: SyncDiffDialogProps) {
  const { t } = useTranslation();

  if (!preview) return null;

  const hasChanges =
    preview.added.length > 0 || preview.removed.length > 0 || preview.changed.length > 0;
  const hasBlocked = (preview.blocked?.length ?? 0) > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {t('dalton.openclaw.diffTitle', 'OpenClaw 模型同步预览')}
          </DialogTitle>
          <DialogDescription>
            {hasChanges
              ? t(
                  'dalton.openclaw.diffSummary',
                  '将更新 src/dalton/llm/openclaw_overlay.json 并热加载，无需重启后端。',
                )
              : t('dalton.openclaw.upToDate', '模型目录已是最新，无需更改。')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2 text-xs">
          {!hasChanges && (
            <div
              className="flex items-center gap-2 p-3 rounded-md"
              style={{
                backgroundColor: 'var(--color-profit-soft)',
                color: 'var(--color-profit)',
              }}
            >
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              <span>
                {t('dalton.openclaw.unchangedCount', {
                  defaultValue: '全部 {{count}} 个模型与当前 overlay 一致。',
                  count: preview.unchanged,
                })}
              </span>
            </div>
          )}

          {hasBlocked && (
            <div
              className="flex flex-col gap-2 p-3 rounded-md"
              style={{
                backgroundColor: 'var(--color-warning-soft)',
                color: 'var(--color-warning)',
              }}
            >
              <div className="flex items-center gap-2 font-medium">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>
                  {t(
                    'dalton.openclaw.inUseWarning',
                    '以下待移除或隐藏的模型正被系统或用户偏好引用：',
                  )}
                </span>
              </div>
              <ul className="list-disc pl-5 space-y-1">
                {preview.blocked.map((b) => (
                  <li key={b.key}>
                    <span className="font-mono font-semibold">{b.key}</span> —{' '}
                    {b.referenced_by.join(', ')}
                  </li>
                ))}
              </ul>
              <div className="flex items-center gap-3 pt-1">
                <a
                  href="/settings?tab=model"
                  className="underline font-medium"
                  onClick={() => onOpenChange(false)}
                >
                  {t('dalton.openclaw.editDefaultModel', '去修改默认模型')}
                </a>
                <a
                  href="/plugins?tab=subagents"
                  className="underline font-medium"
                  onClick={() => onOpenChange(false)}
                >
                  {t('dalton.openclaw.editSubagentModels', '去修改子智能体模型')}
                </a>
              </div>
            </div>
          )}

          {preview.added.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-profit)' }}>
                + {t('dalton.openclaw.added', '新增模型')} ({preview.added.length})
              </div>
              <div
                className="rounded border p-2 space-y-1"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                {preview.added.map((m) => (
                  <div key={m.key} className="flex items-center justify-between gap-2">
                    <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
                      {m.display_name}
                    </span>
                    <span className="font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
                      {m.key} ({m.openclaw_ref})
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.removed.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-loss)' }}>
                − {t('dalton.openclaw.removed', '移除/隐藏模型')} ({preview.removed.length})
              </div>
              <div
                className="rounded border p-2 space-y-1"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                {preview.removed.map((m) => (
                  <div key={m.key} className="flex items-center justify-between gap-2">
                    <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>
                      {m.key} ({m.openclaw_ref})
                    </span>
                    <span style={{ color: 'var(--color-text-tertiary)' }}>{m.reason}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.changed.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                ~ {t('dalton.openclaw.changed', '变更模型')} ({preview.changed.length})
              </div>
              <div
                className="rounded border p-2 space-y-2"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                {preview.changed.map((c) => (
                  <div key={c.key} className="flex flex-col gap-0.5">
                    <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                      {c.key}
                    </span>
                    {Object.entries(c.fields).map(([field, [oldVal, newVal]]) => (
                      <div
                        key={field}
                        className="pl-3 font-mono"
                        style={{ color: 'var(--color-text-tertiary)' }}
                      >
                        {field}: {JSON.stringify(oldVal)} → {JSON.stringify(newVal)}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.price_discrepancies?.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
                {t('dalton.openclaw.priceDiscrepancies', '多来源价格差异（已按最大值保守取用）')}
              </div>
              <div
                className="rounded border p-2 space-y-1 font-mono"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-tertiary)',
                }}
              >
                {preview.price_discrepancies.map((d, idx) => (
                  <div key={`${d.ref}-${d.field}-${idx}`}>
                    {d.ref} [{d.field}]: {JSON.stringify(d.values)} → ${d.chosen}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={applying}>
            {hasChanges
              ? t('common.cancel', '取消')
              : t('common.close', '关闭')}
          </Button>
          {hasChanges && !hasBlocked && (
            <Button onClick={() => onConfirmApply(false)} disabled={applying}>
              {applying
                ? t('dalton.openclaw.applying', '应用中...')
                : t('dalton.openclaw.applySync', '确认同步')}
            </Button>
          )}
          {hasChanges && hasBlocked && (
            <Button
              variant="destructive"
              onClick={() => onConfirmApply(true)}
              disabled={applying}
            >
              {applying
                ? t('dalton.openclaw.applying', '应用中...')
                : t('dalton.openclaw.forceApply', '强制应用（清理引用）')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
