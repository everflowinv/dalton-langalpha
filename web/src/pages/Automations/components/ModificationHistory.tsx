// The run / modification tabs, the history list, and the rollback confirm.
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Undo2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { HeaderButton, ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useLocale } from '@/hooks/useLocale';
import type { AutomationHistory } from '@/types/automation';
import { changeLines, historyActionKey, historyEntriesDesc, snapshotLines } from '../utils/historyView';
import { formatDateTimeShort } from '../utils/time';

export function RecordTabs({
  value,
  onChange,
}: {
  value: 'runs' | 'history';
  onChange: (next: 'runs' | 'history') => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="automation-record-tabs" role="tablist" aria-label={t('automation.records')}>
      <button type="button" role="tab" aria-selected={value === 'runs'} onClick={() => onChange('runs')}>
        {t('automation.runHistory')}
      </button>
      <button type="button" role="tab" aria-selected={value === 'history'} onClick={() => onChange('history')}>
        {t('automation.modificationHistory')}
      </button>
    </div>
  );
}

function labeledField(translate: (key: string) => string, fieldKey: string | null, field: string): string {
  return fieldKey ? translate(fieldKey) : field;
}

export function ModificationHistory({
  history,
  loading,
  failed,
  prevVersion,
  onRollback,
}: {
  history: AutomationHistory | undefined;
  loading: boolean;
  failed: boolean;
  prevVersion: number | null;
  onRollback: (() => void) | null;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const entries = historyEntriesDesc(history?.entries);
  const activeVersion = history?.active_version;

  return (
    <div role="tabpanel" aria-label={t('automation.modificationHistory')}>
      {prevVersion != null && onRollback && (
        <div className="automation-inspector-actions">
          <HeaderButton variant="secondary" icon={Undo2} onClick={onRollback}>
            {t('automation.rollbackButton', { version: prevVersion })}
          </HeaderButton>
        </div>
      )}
      {loading ? (
        <ListSkeleton rows={3} />
      ) : failed ? (
        <p className="automations-quiet-note">{t('automation.historyLoadFailed')}</p>
      ) : entries.length === 0 ? (
        <p className="automations-quiet-note">{t('automation.historyEmpty')}</p>
      ) : (
        <ul className="automation-mod-list" aria-label={t('automation.modificationHistory')}>
          {entries.map((entry) => {
            const actionKey = historyActionKey(entry.action);
            const lines = changeLines(entry.changes);
            return (
              <li key={entry.version} className="automation-mod-row">
                <div>
                  <span className="automation-mono">{t('automation.historyVersion', { version: entry.version })}</span>
                  {entry.version === activeVersion && (
                    <span className="automation-mod-badge">{t('automation.historyCurrent')}</span>
                  )}
                </div>
                <div className="automation-mod-meta">
                  {actionKey ? t(actionKey) : entry.action}
                  {' · '}
                  {t('automation.historyActor', { actor: entry.actor_user_id || t('automation.unknownActor') })}
                  {entry.changed_at ? ` · ${formatDateTimeShort(entry.changed_at, locale)}` : ''}
                </div>
                {lines.length > 0 && (
                  <ul className="automation-mod-changes">
                    {lines.map((line) => (
                      <li key={line.field}>
                        {t('automation.historyChange', {
                          field: labeledField((key) => t(key), line.fieldKey, line.field),
                          old: line.oldValue,
                          next: line.newValue,
                        })}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function DeletedMeta({ deletedAt, deletedBy }: { deletedAt?: string | null; deletedBy?: string | null }) {
  const { t } = useTranslation();
  const locale = useLocale();
  if (!deletedAt && !deletedBy) return null;
  return (
    <p className="automation-inspector-desc">
      {t('automation.deletedMeta', {
        actor: deletedBy || t('automation.unknownActor'),
        time: deletedAt ? formatDateTimeShort(deletedAt, locale) : '—',
      })}
    </p>
  );
}

export function RollbackDialog({
  open,
  version,
  snapshot,
  ready,
  pending,
  loading,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  version: number | null;
  snapshot: Record<string, unknown> | null;
  /** True once the target version's snapshot has been loaded. An empty snapshot still counts. */
  ready: boolean;
  /** True while a later history page is still being loaded for that snapshot. */
  pending: boolean;
  loading: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  const lines = snapshotLines(snapshot);
  const summary = !ready ? (
    pending ? (
      <p>{t('automation.rollbackSnapshotLoading')}</p>
    ) : (
      <p>{t('automation.rollbackSnapshotMissing', { version: version ?? '—' })}</p>
    )
  ) : lines.length === 0 ? (
    <p>{t('automation.rollbackVersionOnly', { version: version ?? '—' })}</p>
  ) : (
    <ul className="automation-mod-changes">
      {lines.map((line) => (
        <li key={line.field}>
          {labeledField((key) => t(key), line.fieldKey, line.field)}
          {': '}
          {line.value}
        </li>
      ))}
    </ul>
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-md border"
        style={{ backgroundColor: 'var(--color-bg-elevated)', borderColor: 'var(--color-border-elevated)' }}
      >
        <DialogHeader>
          <DialogTitle style={{ color: 'var(--color-text-primary)' }}>{t('automation.rollbackTitle')}</DialogTitle>
          <DialogDescription style={{ color: 'var(--color-text-secondary)' }}>
            {version == null
              ? t('automation.rollbackTitle')
              : t('automation.rollbackConfirm', { version })}
          </DialogDescription>
        </DialogHeader>
        <div className="automations-quiet-note">
          <p>{t('automation.rollbackSummary')}</p>
          {summary}
        </div>
        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="ghost" onClick={() => onOpenChange(false)} style={{ color: 'var(--color-text-primary)' }}>
            {t('common.cancel')}
          </Button>
          <Button variant="default" onClick={onConfirm} disabled={loading || version == null || !ready}>
            {loading ? t('automation.rollbackWorking') : t('automation.rollbackSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
