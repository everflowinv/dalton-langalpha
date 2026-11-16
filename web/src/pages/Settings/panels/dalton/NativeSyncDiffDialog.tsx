import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { ModalShell } from '@/components/ui/ModalShell';
import { ToggleSwitch } from '@/components/ui/switch';
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';
import type {
  NativeModelAddedItem,
  NativeModelDeprecatedItem,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

export interface NativeSyncDiffDialogProps {
  open: boolean;
  onClose: () => void;
  added: NativeModelAddedItem[];
  deprecated: NativeModelDeprecatedItem[];
  unchangedCount: number;
  onSync: (selectedModels: string[]) => Promise<void>;
  onRetire?: (selectedKeys: string[]) => Promise<void>;
  syncing?: boolean;
  retiring?: boolean;
}

function defaultSelectedAdded(added: NativeModelAddedItem[]): Set<string> {
  return new Set(
    added
      .filter((item) => !item.conflict && !item.previously_retired && Boolean(item.suggested_key))
      .map((item) => item.suggested_key),
  );
}

/** Modal previewing new and deprecated catalog models before team sync or retirement. */
export function NativeSyncDiffDialog({
  open,
  onClose,
  added,
  deprecated,
  unchangedCount,
  onSync,
  onRetire,
  syncing = false,
  retiring = false,
}: NativeSyncDiffDialogProps) {
  const { t } = useTranslation();
  const labelId = useId();

  const [selectedAdded, setSelectedAdded] = useState<Set<string>>(() =>
    defaultSelectedAdded(added),
  );
  const [selectedRetire, setSelectedRetire] = useState<Set<string>>(new Set());
  const [confirmRetireOpen, setConfirmRetireOpen] = useState(false);

  const wasOpenRef = useRef(false);
  const prevAddedCountRef = useRef(added.length);

  // Reset selections cleanly whenever the dialog opens; prune stale keys if lists refresh while open.
  useEffect(() => {
    const justOpened = open && !wasOpenRef.current;
    wasOpenRef.current = open;
    if (!open) return;

    if (justOpened) {
      setSelectedAdded(defaultSelectedAdded(added));
      setSelectedRetire(new Set());
      setConfirmRetireOpen(false);
      prevAddedCountRef.current = added.length;
      return;
    }

    const validAdded = new Set(
      added.filter((item) => !item.conflict).map((item) => item.suggested_key),
    );
    const populatedFromEmpty = prevAddedCountRef.current === 0 && added.length > 0;
    prevAddedCountRef.current = added.length;

    setSelectedAdded((prev) => {
      if (populatedFromEmpty && prev.size === 0) {
        return defaultSelectedAdded(added);
      }
      const next = new Set<string>();
      for (const k of prev) {
        if (validAdded.has(k)) next.add(k);
      }
      return next;
    });

    const validRetire = new Set(
      deprecated.filter((d) => !(d.in_use_by?.length ?? 0)).map((d) => d.key),
    );
    setSelectedRetire((prev) => {
      const next = new Set<string>();
      for (const k of prev) {
        if (validRetire.has(k)) next.add(k);
      }
      return next;
    });
  }, [open, added, deprecated]);

  if (!open) return null;

  const toggleAdded = (suggestedKey: string, checked: boolean) => {
    setSelectedAdded((prev) => {
      const next = new Set(prev);
      if (checked) next.add(suggestedKey);
      else next.delete(suggestedKey);
      return next;
    });
  };

  const toggleRetire = (key: string, checked: boolean) => {
    setSelectedRetire((prev) => {
      const next = new Set(prev);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const hasAdded = added.length > 0;
  const hasDeprecated = deprecated.length > 0;

  return (
    <>
      <ModalShell
        labelId={labelId}
        title={t('dalton.nativeModels.diffTitle')}
        subtitle={t('dalton.nativeModels.diffSummary')}
        width="wide"
        onClose={onClose}
        footer={
          <div className="flex flex-wrap items-center justify-between gap-2 w-full">
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('dalton.nativeModels.unchangedCount', {
                count: unchangedCount,
                defaultValue: `${unchangedCount} unchanged`,
              })}
            </span>
            <div className="flex items-center gap-2">
              {onRetire && selectedRetire.size > 0 && (
                <Button
                  type="button"
                  variant="outline"
                  tone="danger"
                  size="sm"
                  disabled={retiring}
                  onClick={() => setConfirmRetireOpen(true)}
                  data-testid="native-models-retire-btn"
                >
                  {retiring
                    ? t('dalton.nativeModels.retiring')
                    : t('dalton.nativeModels.retireSelected', { count: selectedRetire.size })}
                </Button>
              )}
              <Button type="button" variant="ghost" size="sm" onClick={onClose}>
                {t('dalton.common.close')}
              </Button>
              {hasAdded && (
                <Button
                  type="button"
                  variant="default"
                  size="sm"
                  disabled={syncing || selectedAdded.size === 0}
                  onClick={() => void onSync(Array.from(selectedAdded))}
                  data-testid="native-models-apply-btn"
                >
                  {syncing
                    ? t('dalton.nativeModels.applying')
                    : t('dalton.nativeModels.apply')}
                </Button>
              )}
            </div>
          </div>
        }
      >
        <div className="space-y-4">
          {!hasAdded && !hasDeprecated && (
            <DaltonInlineNotice tone="success">
              {t('dalton.nativeModels.upToDate')}
            </DaltonInlineNotice>
          )}

          {hasAdded && (
            <div className="space-y-2">
              <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('dalton.nativeModels.added')} ({added.length})
              </div>
              <DaltonKeyValueList>
                {added.map((item) => {
                  const key = item.suggested_key;
                  const checked = selectedAdded.has(key);
                  return (
                    <DaltonItemRow
                      key={`${item.provider}:${item.upstream_id}:${key}`}
                      title={
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span>{item.display_name || item.suggested_key}</span>
                          <DaltonStatusBadge tone="neutral" label={item.provider} />
                          {item.conflict && (
                            <DaltonStatusBadge
                              tone="warning"
                              label={item.conflict_reason || t('dalton.nativeModels.keyConflictHint')}
                            />
                          )}
                          {item.previously_retired && (
                            <DaltonStatusBadge
                              tone="neutral"
                              label={t('dalton.nativeModels.previouslyRetired')}
                            />
                          )}
                        </span>
                      }
                      meta={
                        <span className="font-mono">
                          {item.suggested_key} · {item.upstream_id}
                        </span>
                      }
                      trailing={
                        <ToggleSwitch
                          checked={checked}
                          disabled={item.conflict || syncing}
                          ariaLabel={item.display_name || item.suggested_key}
                          onChange={() => toggleAdded(key, !checked)}
                        />
                      }
                    />
                  );
                })}
              </DaltonKeyValueList>
            </div>
          )}

          {hasDeprecated && (
            <div className="space-y-2">
              <div>
                <div
                  className="text-xs font-medium"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  {t('dalton.nativeModels.deprecated')} ({deprecated.length})
                </div>
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.nativeModels.deprecatedHint')}
                </p>
              </div>
              <DaltonKeyValueList>
                {deprecated.map((item) => {
                  const inUse = (item.in_use_by?.length ?? 0) > 0;
                  return (
                    <DaltonItemRow
                      key={item.key}
                      title={
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span>{item.display_name || item.key}</span>
                          <DaltonStatusBadge
                            tone={inUse ? 'warning' : 'neutral'}
                            label={
                              inUse
                                ? t('dalton.nativeModels.inUse')
                                : t('dalton.nativeModels.notReferenced')
                            }
                          />
                        </span>
                      }
                      meta={<span className="font-mono">{item.key}</span>}
                      trailing={
                        onRetire ? (
                          <ToggleSwitch
                            checked={selectedRetire.has(item.key)}
                            disabled={inUse || retiring}
                            ariaLabel={item.display_name || item.key}
                            onChange={() => toggleRetire(item.key, !selectedRetire.has(item.key))}
                          />
                        ) : undefined
                      }
                    />
                  );
                })}
              </DaltonKeyValueList>
            </div>
          )}
        </div>
      </ModalShell>

      <ConfirmDialog
        open={confirmRetireOpen}
        title={t('dalton.nativeModels.retireConfirmTitle')}
        message={t('dalton.nativeModels.retireConfirmBody', {
          count: selectedRetire.size,
        })}
        confirmLabel={t('dalton.nativeModels.retireConfirm')}
        onOpenChange={setConfirmRetireOpen}
        onConfirm={() => {
          setConfirmRetireOpen(false);
          if (onRetire && selectedRetire.size > 0) {
            void onRetire(Array.from(selectedRetire));
          }
        }}
      />
    </>
  );
}
