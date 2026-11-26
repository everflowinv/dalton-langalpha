import { useId, useState } from 'react';
import { ChevronDown, ChevronRight, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModalShell } from '@/components/ui/ModalShell';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import type { TeamCatalogModelItem } from '@/pages/Settings/utils/daltonApi';
import {
  parseCatalogApiError,
  useRollbackTeamCatalog,
  useTeamCatalogRevisionDetail,
  useTeamCatalogRevisions,
  type MissingVariantCredentialDetail,
} from './useTeamModelCatalog';

export interface TeamModelHistoryDialogProps {
  open: boolean;
  onClose: () => void;
  currentRevision: number;
  activeModels: readonly TeamCatalogModelItem[];
  onRolledBack?: (newRevision: number, targetRevision: number, restoredTasksCount: number) => void;
}

const ACTION_LABEL_KEY: Record<string, string> = {
  init_empty: 'settings:dalton.teamCatalog.actionInitEmpty',
  bootstrap_import: 'settings:dalton.teamCatalog.actionBootstrapImport',
  abandon_legacy_file: 'settings:dalton.teamCatalog.actionAbandonLegacy',
  create_model: 'settings:dalton.teamCatalog.actionCreateModel',
  update_model: 'settings:dalton.teamCatalog.actionUpdateModel',
  hide_model: 'settings:dalton.teamCatalog.actionHideModel',
  unhide_model: 'settings:dalton.teamCatalog.actionUnhideModel',
  retire_model: 'settings:dalton.teamCatalog.actionRetireModel',
  restore_model: 'settings:dalton.teamCatalog.actionRestoreModel',
  native_sync: 'settings:dalton.teamCatalog.actionNativeSync',
  native_retire: 'settings:dalton.teamCatalog.actionNativeRetire',
  native_restore: 'settings:dalton.teamCatalog.actionNativeRestore',
  upsert_provider_variant: 'settings:dalton.teamCatalog.actionUpsertVariant',
  update_curated: 'settings:dalton.teamCatalog.actionUpdateCurated',
  confirm_price: 'settings:dalton.teamCatalog.actionConfirmPrice',
  rollback: 'settings:dalton.teamCatalog.actionRollback',
};

/** Version history & safe rollback dialog (including variant credential re-entry on URL mismatch). */
export function TeamModelHistoryDialog({
  open,
  onClose,
  currentRevision,
  activeModels,
  onRolledBack,
}: TeamModelHistoryDialogProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const labelId = useId();

  const revisionsQuery = useTeamCatalogRevisions(open, 50, 0);
  const rollbackMutation = useRollbackTeamCatalog();

  const [expandedRev, setExpandedRev] = useState<number | null>(null);
  const detailQuery = useTeamCatalogRevisionDetail(open ? expandedRev : null);

  const [pendingTargetRev, setPendingTargetRev] = useState<number | null>(null);
  const [missingCreds, setMissingCreds] = useState<MissingVariantCredentialDetail[]>([]);
  const [credKeys, setCredKeys] = useState<Record<string, string>>({});
  const [droppedSuccessors, setDroppedSuccessors] = useState<Record<string, string>>({});
  const [droppedModelNeedSuccessor, setDroppedModelNeedSuccessor] = useState<string | null>(null);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const [rollbackResult, setRollbackResult] = useState<{
    newRevision: number;
    targetRevision: number;
    restoredCount: number;
    skippedCount: number;
  } | null>(null);

  if (!open) return null;

  const revisions = revisionsQuery.data?.revisions ?? [];
  const liveRevision = revisionsQuery.data?.revision ?? currentRevision;

  const executeRollback = async (
    targetRevision: number,
    suppliedCreds?: Record<string, { base_url: string; api_key: string }>,
    suppliedSuccessors?: Record<string, string>,
  ) => {
    setRollbackError(null);
    setRollbackResult(null);
    setPendingTargetRev(targetRevision);
    try {
      const res = await rollbackMutation.mutateAsync({
        target_revision: targetRevision,
        expected_revision: liveRevision,
        variant_credentials: suppliedCreds && Object.keys(suppliedCreds).length > 0 ? suppliedCreds : undefined,
        successor_for_dropped:
          suppliedSuccessors && Object.keys(suppliedSuccessors).length > 0 ? suppliedSuccessors : undefined,
      });
      setMissingCreds([]);
      setCredKeys({});
      setDroppedModelNeedSuccessor(null);
      setDroppedSuccessors({});
      setPendingTargetRev(null);
      setRollbackResult({
        newRevision: res.revision,
        targetRevision: res.restored_from,
        restoredCount: res.restored_tasks?.length ?? 0,
        skippedCount: res.skipped_user_modified_tasks?.length ?? 0,
      });
      onRolledBack?.(res.revision, res.restored_from, res.restored_tasks?.length ?? 0);
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.rollbackFailed'));
      if (parsed.code === 'variant_credential_required' && parsed.missingVariantCredentials.length > 0) {
        setMissingCreds(parsed.missingVariantCredentials);
        setCredKeys((prev) => {
          const next = { ...prev };
          for (const item of parsed.missingVariantCredentials) {
            if (!(item.provider_slug in next)) next[item.provider_slug] = '';
          }
          return next;
        });
        setRollbackError(parsed.message);
      } else if (parsed.code === 'model_in_use') {
        const match = /Model '([^']+)'/.exec(parsed.message);
        if (match?.[1]) {
          setDroppedModelNeedSuccessor(match[1]);
        }
        setRollbackError(parsed.message);
      } else {
        setRollbackError(parsed.message);
      }
    }
  };

  const handleConfirmWithCredentials = () => {
    if (pendingTargetRev === null) return;
    const credsPayload: Record<string, { base_url: string; api_key: string }> = {};
    for (const item of missingCreds) {
      const apiKey = (credKeys[item.provider_slug] ?? '').trim();
      if (!apiKey) {
        setRollbackError(
          t('settings:dalton.teamCatalog.rollbackCredMissingInput', {
            slug: item.provider_slug,
          }),
        );
        return;
      }
      credsPayload[item.provider_slug] = {
        base_url: item.required_base_url,
        api_key: apiKey,
      };
    }
    void executeRollback(pendingTargetRev, credsPayload, droppedSuccessors);
  };

  return (
    <ModalShell
      labelId={labelId}
      title={t('settings:dalton.teamCatalog.historyDialogTitle')}
      subtitle={t('settings:dalton.teamCatalog.historyDialogSubtitle', {
        revision: liveRevision,
      })}
      width="table"
      closeDisabled={rollbackMutation.isPending}
      onClose={onClose}
      footer={
        <div className="flex items-center justify-end gap-2 w-full">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      }
    >
      <div className="space-y-4" data-testid="team-catalog-history-dialog">
        {rollbackResult && (
          <DaltonInlineNotice tone="success" testId="rollback-success-notice">
            {t('settings:dalton.teamCatalog.rollbackSuccess', {
              newRevision: rollbackResult.newRevision,
              targetRevision: rollbackResult.targetRevision,
              restored: rollbackResult.restoredCount,
              skipped: rollbackResult.skippedCount,
            })}
          </DaltonInlineNotice>
        )}

        {rollbackError && (
          <DaltonInlineNotice tone="warning" testId="rollback-error-notice">
            {rollbackError}
          </DaltonInlineNotice>
        )}

        {pendingTargetRev !== null && missingCreds.length > 0 && (
          <div
            className="rounded-md border p-3 space-y-3"
            style={{
              borderColor: 'var(--color-border-muted)',
              backgroundColor: 'var(--color-bg-card)',
            }}
            data-testid="rollback-variant-credentials-panel"
          >
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings:dalton.teamCatalog.rollbackCredsTitle', {
                revision: pendingTargetRev,
              })}
            </div>
            <p className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
              {t('settings:dalton.teamCatalog.rollbackCredsDesc')}
            </p>
            {missingCreds.map((item) => (
              <div key={item.provider_slug} className="space-y-1.5">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {item.provider_slug}
                  </span>
                  <DaltonStatusBadge tone="accent" label={item.required_base_url} />
                </div>
                <Input
                  type="password"
                  autoComplete="new-password"
                  value={credKeys[item.provider_slug] ?? ''}
                  onChange={(e) =>
                    setCredKeys((prev) => ({ ...prev, [item.provider_slug]: e.target.value }))
                  }
                  placeholder={t('settings:dalton.teamCatalog.variantApiKeyPlaceholder')}
                  className="text-base md:text-sm"
                  data-testid={`rollback-cred-input-${item.provider_slug}`}
                />
              </div>
            ))}
            <div className="flex flex-wrap items-center justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setMissingCreds([]);
                  setPendingTargetRev(null);
                  setRollbackError(null);
                }}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={rollbackMutation.isPending}
                onClick={handleConfirmWithCredentials}
                data-testid="rollback-creds-submit-btn"
              >
                {rollbackMutation.isPending
                  ? t('settings:dalton.teamCatalog.processing')
                  : t('settings:dalton.teamCatalog.rollbackCredsConfirm')}
              </Button>
            </div>
          </div>
        )}

        {pendingTargetRev !== null && droppedModelNeedSuccessor && (
          <div
            className="rounded-md border p-3 space-y-2.5"
            style={{
              borderColor: 'var(--color-border-muted)',
              backgroundColor: 'var(--color-bg-card)',
            }}
            data-testid="rollback-dropped-successor-panel"
          >
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings:dalton.teamCatalog.rollbackDroppedTitle', {
                model: droppedModelNeedSuccessor,
              })}
            </div>
            <select
              value={droppedSuccessors[droppedModelNeedSuccessor] ?? ''}
              onChange={(e) =>
                setDroppedSuccessors((prev) => ({
                  ...prev,
                  [droppedModelNeedSuccessor]: e.target.value,
                }))
              }
              className="w-full rounded-md border px-3 py-2 text-base md:text-sm"
              style={{
                backgroundColor: 'var(--color-bg-input)',
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-primary)',
              }}
              data-testid="rollback-dropped-successor-select"
            >
              <option value="">{t('settings:dalton.teamCatalog.selectSuccessorOption')}</option>
              {activeModels
                .filter((m) => m.model_key !== droppedModelNeedSuccessor)
                .map((m) => (
                  <option key={m.model_key} value={m.model_key}>
                    {m.display_name} ({m.model_key})
                  </option>
                ))}
            </select>
            <div className="flex justify-end">
              <Button
                type="button"
                size="sm"
                disabled={
                  rollbackMutation.isPending || !droppedSuccessors[droppedModelNeedSuccessor]
                }
                onClick={() => void executeRollback(pendingTargetRev, undefined, droppedSuccessors)}
                data-testid="rollback-dropped-submit-btn"
              >
                {t('settings:dalton.teamCatalog.rollbackRetryWithSuccessor')}
              </Button>
            </div>
          </div>
        )}

        {revisionsQuery.isLoading ? (
          <DaltonLoadingRow label={t('settings:dalton.teamCatalog.loadingRevisions')} />
        ) : revisionsQuery.isError ? (
          <DaltonInlineNotice tone="error">
            {t('settings:dalton.teamCatalog.loadFailed')}
          </DaltonInlineNotice>
        ) : revisions.length === 0 ? (
          <div className="text-xs py-4 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings:dalton.teamCatalog.noRevisions')}
          </div>
        ) : (
          <div className="space-y-2">
            {revisions.map((rev) => {
              const isCurrent = rev.revision === liveRevision;
              const isExpanded = expandedRev === rev.revision;
              const actionKey = ACTION_LABEL_KEY[rev.action];
              const actionLabel = actionKey ? t(actionKey) : rev.action;
              return (
                <div
                  key={rev.revision}
                  className="rounded-md border px-3 py-2.5 space-y-2"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    backgroundColor: 'var(--color-bg-card)',
                  }}
                  data-testid={`revision-row-v${rev.revision}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2 min-w-0">
                      <button
                        type="button"
                        className="tap-target inline-flex items-center gap-1 text-xs font-mono font-semibold"
                        style={{ color: 'var(--color-text-primary)' }}
                        onClick={() => setExpandedRev(isExpanded ? null : rev.revision)}
                        aria-expanded={isExpanded}
                      >
                        {isExpanded ? (
                          <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5" />
                        )}
                        <span>v{rev.revision}</span>
                      </button>
                      <DaltonStatusBadge
                        tone={isCurrent ? 'accent' : 'neutral'}
                        label={actionLabel}
                      />
                      {isCurrent && (
                        <DaltonStatusBadge
                          tone="success"
                          dot
                          label={t('settings:dalton.teamCatalog.currentVersionBadge')}
                        />
                      )}
                      {rev.restored_from !== null && (
                        <DaltonStatusBadge
                          tone="warning"
                          label={t('settings:dalton.teamCatalog.restoredFromBadge', {
                            revision: rev.restored_from,
                          })}
                        />
                      )}
                      {rev.side_effects.length > 0 && (
                        <DaltonStatusBadge
                          tone="neutral"
                          label={t('settings:dalton.teamCatalog.sideEffectsCount', {
                            count: rev.side_effects.length,
                          })}
                        />
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {formatTimestamp(rev.created_at, locale)}
                      </span>
                      {!isCurrent && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-7 text-xs gap-1"
                          disabled={rollbackMutation.isPending}
                          onClick={() => void executeRollback(rev.revision)}
                          data-testid={`rollback-to-v${rev.revision}-btn`}
                        >
                          <RotateCcw className="h-3 w-3" />
                          <span>{t('settings:dalton.teamCatalog.rollbackToVersion')}</span>
                        </Button>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                    <span>
                      {t('settings:dalton.teamCatalog.actorLabel', {
                        actor: rev.actor_label || rev.actor_user_id,
                      })}
                    </span>
                    {rev.target_keys.length > 0 && (
                      <span className="font-mono break-all">
                        {rev.target_keys.join(', ')}
                      </span>
                    )}
                    {rev.comment && <span>· {rev.comment}</span>}
                  </div>

                  {isExpanded && (
                    <div
                      className="rounded border p-2 text-[11px] font-mono overflow-x-auto space-y-1"
                      style={{
                        borderColor: 'var(--color-border-muted)',
                        backgroundColor: 'var(--color-bg-elevated)',
                        color: 'var(--color-text-secondary)',
                      }}
                    >
                      {detailQuery.isLoading ? (
                        <div>{t('settings:dalton.teamCatalog.loadingDetail')}</div>
                      ) : (
                        <>
                          <div>
                            diff_summary:{' '}
                            {JSON.stringify(
                              detailQuery.data?.revision_record?.diff_summary ?? rev.diff_summary,
                            )}
                          </div>
                          {rev.side_effects.length > 0 && (
                            <div>side_effects: {JSON.stringify(rev.side_effects)}</div>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </ModalShell>
  );
}
