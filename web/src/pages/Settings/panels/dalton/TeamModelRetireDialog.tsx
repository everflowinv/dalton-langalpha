import { useEffect, useId, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModalShell } from '@/components/ui/ModalShell';
import { ToggleSwitch } from '@/components/ui/switch';
import { DaltonInlineNotice, DaltonStatusBadge } from '@/components/dalton/primitives';
import type { TeamCatalogModelItem } from '@/pages/Settings/utils/daltonApi';
import {
  parseCatalogApiError,
  useRetireTeamCatalogModel,
  type ParsedCatalogApiError,
} from './useTeamModelCatalog';

export interface TeamModelRetireDialogProps {
  open: boolean;
  onClose: () => void;
  model: TeamCatalogModelItem | null;
  revision: number;
  activeModels: readonly TeamCatalogModelItem[];
  isLastVisibleCuratedEverflow?: boolean;
  onRetired?: (modelKey: string, replacedCount: number) => void;
}

/** Retire confirmation & transactional batch replacement dialog. */
export function TeamModelRetireDialog({
  open,
  onClose,
  model,
  revision,
  activeModels,
  isLastVisibleCuratedEverflow = false,
  onRetired,
}: TeamModelRetireDialogProps) {
  const { t } = useTranslation();
  const labelId = useId();
  const retireMutation = useRetireTeamCatalogModel();

  const [successorModel, setSuccessorModel] = useState('');
  const [migrateUserDefaults, setMigrateUserDefaults] = useState(true);
  const [clearSubagentModel, setClearSubagentModel] = useState(false);
  const [comment, setComment] = useState('');
  const [confirmLastCurated, setConfirmLastCurated] = useState(false);
  const [conflict, setConflict] = useState<ParsedCatalogApiError | null>(null);

  useEffect(() => {
    if (open) {
      setSuccessorModel('');
      setMigrateUserDefaults(true);
      setClearSubagentModel(false);
      setComment('');
      setConfirmLastCurated(false);
      setConflict(null);
    }
  }, [open, model?.model_key]);

  const successorOptions = useMemo(() => {
    if (!model) return [];
    return activeModels
      .filter((m) => m.model_key !== model.model_key)
      .sort((a, b) => a.display_name.localeCompare(b.display_name));
  }, [activeModels, model]);

  if (!open || !model) return null;

  const isHardBlocked =
    conflict?.code === 'model_in_use_by_config' || conflict?.code === 'model_in_use_by_bundle';
  const hasReplaceableRefs = conflict?.code === 'model_in_use';
  const onlySubagentRefs =
    hasReplaceableRefs &&
    conflict.automations.length === 0 &&
    (conflict.userSubagents.length > 0 || conflict.pluginSubagents.length > 0);

  const canSubmit =
    !retireMutation.isPending &&
    (!isLastVisibleCuratedEverflow || confirmLastCurated) &&
    (!hasReplaceableRefs || Boolean(successorModel.trim()) || (onlySubagentRefs && clearSubagentModel));

  const handleSubmit = async () => {
    setConflict(null);
    try {
      const res = await retireMutation.mutateAsync({
        modelKey: model.model_key,
        payload: {
          successor_model: clearSubagentModel ? null : successorModel.trim() || null,
          migrate_user_defaults: Boolean(successorModel.trim()) && !clearSubagentModel && migrateUserDefaults,
          clear_subagent_model: clearSubagentModel,
          expected_revision: revision,
          comment: comment.trim() || undefined,
        },
      });
      onRetired?.(model.model_key, res.side_effects?.length ?? 0);
      onClose();
    } catch (err) {
      setConflict(parseCatalogApiError(err, t('settings:dalton.teamCatalog.retireFailed')));
    }
  };

  return (
    <ModalShell
      labelId={labelId}
      title={t('settings:dalton.teamCatalog.retireDialogTitle', {
        name: model.display_name || model.model_key,
      })}
      subtitle={t('settings:dalton.teamCatalog.retireDialogSubtitle')}
      width="wide"
      closeDisabled={retireMutation.isPending}
      onClose={onClose}
      footer={
        <div className="flex flex-wrap items-center justify-end gap-2 w-full">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={retireMutation.isPending}
            onClick={onClose}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={!canSubmit}
            onClick={() => void handleSubmit()}
            data-testid="retire-dialog-confirm-btn"
          >
            {retireMutation.isPending
              ? t('settings:dalton.teamCatalog.processing')
              : hasReplaceableRefs
                ? t('settings:dalton.teamCatalog.retireReplaceAndConfirm')
                : t('settings:dalton.teamCatalog.retireConfirmAction')}
          </Button>
        </div>
      }
    >
      <div className="space-y-4" data-testid="team-catalog-retire-dialog">
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs"
          style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-card)' }}
        >
          <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {model.model_key}
          </span>
          <DaltonStatusBadge tone="neutral" label={model.provider_display_name || model.provider} />
        </div>

        {isLastVisibleCuratedEverflow && (
          <DaltonInlineNotice tone="warning" testId="retire-last-curated-warning">
            <div className="space-y-2">
              <div>{t('settings:dalton.teamCatalog.lastCuratedRetireWarning')}</div>
              <label className="inline-flex items-center gap-2 cursor-pointer text-xs font-medium">
                <input
                  type="checkbox"
                  checked={confirmLastCurated}
                  onChange={(e) => setConfirmLastCurated(e.target.checked)}
                  data-testid="retire-last-curated-ack"
                />
                <span>{t('settings:dalton.teamCatalog.lastCuratedAckLabel')}</span>
              </label>
            </div>
          </DaltonInlineNotice>
        )}

        {isHardBlocked && conflict && (
          <DaltonInlineNotice tone="error" testId="retire-hard-block-notice">
            <div className="font-medium mb-1">{conflict.message}</div>
            {Object.keys(conflict.agentConfig).length > 0 && (
              <div className="text-[11px] font-mono">
                agent_config: {JSON.stringify(conflict.agentConfig)}
              </div>
            )}
            {Object.keys(conflict.daltonSettings).length > 0 && (
              <div className="text-[11px] font-mono">
                dalton_settings: {JSON.stringify(conflict.daltonSettings)}
              </div>
            )}
            {Object.keys(conflict.bundledPluginSubagents).length > 0 && (
              <div className="text-[11px] font-mono">
                plugins: {JSON.stringify(conflict.bundledPluginSubagents)}
              </div>
            )}
          </DaltonInlineNotice>
        )}

        {hasReplaceableRefs && conflict && (
          <DaltonInlineNotice tone="warning" testId="retire-in-use-notice">
            <div className="space-y-1.5">
              <div className="font-medium">
                {t('settings:dalton.teamCatalog.retireRefsFoundTitle')}
              </div>
              <ul className="list-disc pl-4 space-y-0.5 text-xs">
                {conflict.automations.length > 0 && (
                  <li>
                    {t('settings:dalton.teamCatalog.retireRefAutomations', {
                      count: conflict.automations.length,
                    })}
                    {': '}
                    {conflict.automations
                      .slice(0, 5)
                      .map((a) => a.name || a.automation_id)
                      .join(', ')}
                  </li>
                )}
                {conflict.userSubagents.length > 0 && (
                  <li>
                    {t('settings:dalton.teamCatalog.retireRefUserSubagents', {
                      count: conflict.userSubagents.length,
                    })}
                    {': '}
                    {conflict.userSubagents
                      .slice(0, 5)
                      .map((s) => s.name || s.user_subagent_id)
                      .join(', ')}
                  </li>
                )}
                {conflict.pluginSubagents.length > 0 && (
                  <li>
                    {t('settings:dalton.teamCatalog.retireRefPluginSubagents', {
                      count: conflict.pluginSubagents.length,
                    })}
                    {': '}
                    {conflict.pluginSubagents
                      .slice(0, 5)
                      .map((s) => s.name || s.plugin_subagent_id)
                      .join(', ')}
                  </li>
                )}
                {conflict.userPreferences.length > 0 && (
                  <li>
                    {t('settings:dalton.teamCatalog.retireRefUserPrefs', {
                      count: conflict.userPreferences.length,
                    })}
                  </li>
                )}
              </ul>
            </div>
          </DaltonInlineNotice>
        )}

        {conflict && !isHardBlocked && !hasReplaceableRefs && (
          <DaltonInlineNotice tone="error" testId="retire-error-notice">
            {conflict.message}
          </DaltonInlineNotice>
        )}

        <div className="space-y-1.5">
          <label
            htmlFor="retire-successor-select"
            className="text-xs font-medium block"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('settings:dalton.teamCatalog.retireSuccessorLabel')}
          </label>
          <select
            id="retire-successor-select"
            value={successorModel}
            disabled={retireMutation.isPending || clearSubagentModel}
            onChange={(e) => {
              setSuccessorModel(e.target.value);
              if (e.target.value) setClearSubagentModel(false);
            }}
            data-testid="retire-successor-select"
            className="w-full rounded-md border px-3 py-2 text-base md:text-sm"
            style={{
              backgroundColor: 'var(--color-bg-input)',
              borderColor: 'var(--color-border-muted)',
              color: 'var(--color-text-primary)',
            }}
          >
            <option value="">{t('settings:dalton.teamCatalog.retireNoSuccessorOption')}</option>
            {successorOptions.map((opt) => (
              <option key={opt.model_key} value={opt.model_key}>
                {opt.display_name} ({opt.model_key})
              </option>
            ))}
          </select>
          <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings:dalton.teamCatalog.retireSuccessorHint')}
          </p>
        </div>

        {Boolean(successorModel) && (
          <div
            className="flex items-center justify-between gap-3 rounded-md border px-3 py-2.5"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <div className="min-w-0">
              <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('settings:dalton.teamCatalog.retireMigrateDefaultsLabel')}
              </div>
              <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings:dalton.teamCatalog.retireMigrateDefaultsDesc')}
              </div>
            </div>
            <ToggleSwitch
              checked={migrateUserDefaults}
              onChange={() => setMigrateUserDefaults((v) => !v)}
              disabled={retireMutation.isPending}
              ariaLabel={t('settings:dalton.teamCatalog.retireMigrateDefaultsLabel')}
              testId="retire-migrate-defaults-toggle"
            />
          </div>
        )}

        <div
          className="flex items-center justify-between gap-3 rounded-md border px-3 py-2.5"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <div className="min-w-0">
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings:dalton.teamCatalog.retireClearSubagentLabel')}
            </div>
            <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings:dalton.teamCatalog.retireClearSubagentDesc')}
            </div>
          </div>
          <ToggleSwitch
            checked={clearSubagentModel}
            onChange={() => {
              setClearSubagentModel((prev) => {
                const next = !prev;
                if (next) setSuccessorModel('');
                return next;
              });
            }}
            disabled={retireMutation.isPending}
            ariaLabel={t('settings:dalton.teamCatalog.retireClearSubagentLabel')}
            testId="retire-clear-subagent-toggle"
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="retire-comment-input"
            className="text-xs font-medium block"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('settings:dalton.teamCatalog.commentOptional')}
          </label>
          <Input
            id="retire-comment-input"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('settings:dalton.teamCatalog.retireCommentPlaceholder')}
            className="text-base md:text-sm"
            disabled={retireMutation.isPending}
          />
        </div>
      </div>
    </ModalShell>
  );
}
