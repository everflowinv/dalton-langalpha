import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModalShell } from '@/components/ui/ModalShell';
import { ToggleSwitch } from '@/components/ui/switch';
import { DaltonInlineNotice, DaltonStatusBadge } from '@/components/dalton/primitives';
import {
  parseCatalogApiError,
  useResolveTeamCatalogLegacyImport,
} from './useTeamModelCatalog';

export interface TeamModelLegacyImportDialogProps {
  open: boolean;
  onClose: () => void;
  revision: number;
  errorSummary: string | null;
  readonlyLegacyRetiredKeys: readonly string[];
  onResolved?: (action: 'retry_import' | 'abandon_legacy_file') => void;
}

/** Repair dialog for resolving `legacy_sync_import_status === 'failed'`. */
export function TeamModelLegacyImportDialog({
  open,
  onClose,
  revision,
  errorSummary,
  readonlyLegacyRetiredKeys,
  onResolved,
}: TeamModelLegacyImportDialogProps) {
  const { t } = useTranslation();
  const labelId = useId();
  const resolveMutation = useResolveTeamCatalogLegacyImport();

  const [persistKnownRetired, setPersistKnownRetired] = useState(true);
  const [comment, setComment] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (!open) return null;

  const handleAction = async (action: 'retry_import' | 'abandon_legacy_file') => {
    setErrorMsg(null);
    try {
      await resolveMutation.mutateAsync({
        action,
        persist_known_retired: persistKnownRetired,
        expected_revision: revision,
        comment: comment.trim() || undefined,
      });
      onResolved?.(action);
      onClose();
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      setErrorMsg(parsed.message);
    }
  };

  return (
    <ModalShell
      labelId={labelId}
      title={t('settings:dalton.teamCatalog.legacyDialogTitle')}
      subtitle={t('settings:dalton.teamCatalog.legacyDialogSubtitle')}
      width="wide"
      closeDisabled={resolveMutation.isPending}
      onClose={onClose}
      footer={
        <div className="flex flex-wrap items-center justify-end gap-2 w-full">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={resolveMutation.isPending}
            onClick={onClose}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={resolveMutation.isPending}
            onClick={() => void handleAction('abandon_legacy_file')}
            data-testid="legacy-import-abandon-btn"
          >
            {t('settings:dalton.teamCatalog.legacyAbandonAction')}
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={resolveMutation.isPending}
            onClick={() => void handleAction('retry_import')}
            data-testid="legacy-import-retry-btn"
          >
            {resolveMutation.isPending
              ? t('settings:dalton.teamCatalog.processing')
              : t('settings:dalton.teamCatalog.legacyRetryAction')}
          </Button>
        </div>
      }
    >
      <div className="space-y-4" data-testid="team-catalog-legacy-import-dialog">
        {errorSummary && (
          <DaltonInlineNotice tone="warning" testId="legacy-import-error-summary">
            <div className="font-medium mb-1">
              {t('settings:dalton.teamCatalog.legacyErrorLabel')}
            </div>
            <div className="font-mono text-[11px] break-all">{errorSummary}</div>
          </DaltonInlineNotice>
        )}

        <div className="space-y-2">
          <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('settings:dalton.teamCatalog.legacyOptionsHeading')}
          </div>
          <p className="text-xs leading-5" style={{ color: 'var(--color-text-secondary)' }}>
            {t('settings:dalton.teamCatalog.legacyRetryHelp')}
          </p>
          <p className="text-xs leading-5" style={{ color: 'var(--color-text-secondary)' }}>
            {t('settings:dalton.teamCatalog.legacyAbandonHelp')}
          </p>
        </div>

        {readonlyLegacyRetiredKeys.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings:dalton.teamCatalog.legacyLockedKeysLabel', {
                count: readonlyLegacyRetiredKeys.length,
              })}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {readonlyLegacyRetiredKeys.map((key) => (
                <DaltonStatusBadge key={key} tone="warning" label={key} />
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 rounded-md border px-3 py-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
          <div className="min-w-0">
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings:dalton.teamCatalog.legacyPersistRetiredLabel')}
            </div>
            <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings:dalton.teamCatalog.legacyPersistRetiredDesc')}
            </div>
          </div>
          <ToggleSwitch
            checked={persistKnownRetired}
            onChange={() => setPersistKnownRetired((v) => !v)}
            disabled={resolveMutation.isPending}
            ariaLabel={t('settings:dalton.teamCatalog.legacyPersistRetiredLabel')}
            testId="legacy-import-persist-retired-toggle"
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="legacy-import-comment"
            className="text-xs font-medium block"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('settings:dalton.teamCatalog.commentOptional')}
          </label>
          <Input
            id="legacy-import-comment"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('settings:dalton.teamCatalog.legacyCommentPlaceholder')}
            className="text-base md:text-sm"
            disabled={resolveMutation.isPending}
          />
        </div>

        {errorMsg && (
          <DaltonInlineNotice tone="error" testId="legacy-import-submit-error">
            {errorMsg}
          </DaltonInlineNotice>
        )}
      </div>
    </ModalShell>
  );
}
