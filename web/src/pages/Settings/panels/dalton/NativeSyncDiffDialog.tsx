import React, { useEffect, useMemo, useState } from 'react';
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
import type { NativeModelAddedItem, NativeModelDeprecatedItem } from '../../utils/daltonApi';

interface NativeSyncDiffDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  added: NativeModelAddedItem[];
  deprecated: NativeModelDeprecatedItem[];
  unchanged: number;
  applying: boolean;
  /** PM may confirm a sync; everyone else gets a read-only view of the diff. */
  canSync: boolean;
  onConfirmSync: (selectedKeys: string[]) => void;
}

export function NativeSyncDiffDialog({
  open,
  onOpenChange,
  added,
  deprecated,
  unchanged,
  applying,
  canSync,
  onConfirmSync,
}: NativeSyncDiffDialogProps) {
  const { t } = useTranslation();

  const selectable = useMemo(() => added.filter((m) => !m.conflict), [added]);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (open) {
      // Duplicated display names (likely old upstream aliases) start unchecked.
      setSelected(
        new Set(selectable.filter((m) => !m.duplicate_display_name).map((m) => m.suggested_key)),
      );
    }
  }, [open, selectable]);

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const conflicted = added.filter((m) => m.conflict);
  const claudeAdded = added.some((m) => m.provider === 'claude-oauth');

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {t('dalton.nativeModels.diffTitle', '原生模型同步预览')}
          </DialogTitle>
          <DialogDescription>
            {t(
              'dalton.nativeModels.diffSummary',
              '确认后仅把勾选的新模型追加到运行时 overlay（var/dalton/catalog），并热加载；不修改任何文件或默认设置。',
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2 text-xs">
          {added.length === 0 && deprecated.length === 0 && (
            <div
              className="flex items-center gap-2 p-3 rounded-md"
              style={{
                backgroundColor: 'var(--color-profit-soft)',
                color: 'var(--color-profit)',
              }}
            >
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              <span>
                {t('dalton.nativeModels.upToDate', {
                  defaultValue: '没有发现新增或下线的原生模型；{{count}} 个模型与当前目录一致。',
                  count: unchanged,
                })}
              </span>
            </div>
          )}

          {added.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-profit)' }}>
                + {t('dalton.nativeModels.added', '新增模型')} ({added.length})
              </div>
              <div
                className="rounded border p-2 space-y-1"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                {added.map((m) => (
                  <label
                    key={`${m.provider}-${m.suggested_key}`}
                    className="flex items-center justify-between gap-2 cursor-pointer"
                    style={{ opacity: m.conflict ? 0.6 : 1 }}
                  >
                    <span className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={selected.has(m.suggested_key)}
                        disabled={applying || !canSync || m.conflict}
                        onChange={() => toggle(m.suggested_key)}
                        data-testid={`native-model-check-${m.suggested_key}`}
                      />
                      <span
                        style={{ color: 'var(--color-text-primary)' }}
                        title={
                          m.duplicate_display_name
                            ? t(
                                'dalton.nativeModels.duplicateNameHint',
                                '显示名与其他模型重复，可能是旧别名',
                              )
                            : undefined
                        }
                      >
                        {m.display_name}
                        {m.duplicate_display_name ? ' *' : ''}
                      </span>
                    </span>
                    <span className="font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
                      {m.conflict
                        ? `${m.suggested_key}（${m.conflict_reason ?? 'key 冲突，需人工确认'}）`
                        : `${m.suggested_key} ← ${m.upstream_id}`}
                    </span>
                  </label>
                ))}
              </div>
              {conflicted.length > 0 && (
                <span style={{ color: 'var(--color-warning)' }}>
                  {t(
                    'dalton.nativeModels.keyConflictHint',
                    '存在冲突的条目（key 与现有模型重名，或与其他供应商的新增重复）不会同步，需人工确认命名。',
                  )}
                </span>
              )}
              {added.some((m) => m.duplicate_display_name) && (
                <span style={{ color: 'var(--color-warning)' }}>
                  *{' '}
                  {t(
                    'dalton.nativeModels.duplicateNameHint',
                    '显示名与其他模型重复，可能是旧别名（默认未勾选）',
                  )}
                </span>
              )}
              {claudeAdded && (
                <span style={{ color: 'var(--color-text-tertiary)' }}>
                  {t(
                    'dalton.nativeModels.claudeThinkingNote',
                    '新增的 Claude 模型暂不带推理强度设置。',
                  )}
                </span>
              )}
            </div>
          )}

          {deprecated.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <div className="font-semibold" style={{ color: 'var(--color-loss)' }}>
                − {t('dalton.nativeModels.deprecated', '疑似下线模型')} ({deprecated.length})
              </div>
              <div
                className="rounded border p-2 space-y-1"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                {deprecated.map((m) => (
                  <div key={m.key} className="flex flex-col">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>
                        {m.key}
                      </span>
                      <span style={{ color: 'var(--color-text-tertiary)' }}>
                        {m.in_use_by?.length
                          ? t('dalton.nativeModels.inUse', '使用中')
                          : t('dalton.nativeModels.notReferenced', '未被引用')}
                      </span>
                    </div>
                    {(m.in_use_by?.length ?? 0) > 0 && (
                      <div className="pl-3" style={{ color: 'var(--color-warning)' }}>
                        {m.in_use_by!.join(', ')}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <span className="flex items-start gap-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                {t(
                  'dalton.nativeModels.deprecatedHint',
                  '下线仅提示，不会自动删除；模型仍保留在目录中，可继续使用。',
                )}
              </span>
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={applying}>
            {t('common.cancel', '取消')}
          </Button>
          {canSync && (
            <Button
              onClick={() => onConfirmSync([...selected])}
              disabled={applying || selected.size === 0}
              data-testid="native-model-sync-confirm"
            >
              {applying
                ? t('dalton.nativeModels.applying', '同步中...')
                : t('dalton.nativeModels.apply', '加入所选模型')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
