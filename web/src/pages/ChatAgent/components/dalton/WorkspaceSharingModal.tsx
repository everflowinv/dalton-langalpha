/* eslint-disable react-refresh/only-export-components */
import React, { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Share2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ModalShell } from '@/components/ui/ModalShell';
import { SegmentedControl } from '@/components/ui/segmented-control';
import { ToggleSwitch } from '@/components/ui/switch';
// Direct module imports: the primitives barrel also re-exports the
// confirm dialog, which would pull its animation library onto first load.
import { DaltonInlineNotice } from '@/components/dalton/primitives/DaltonStatusBadge';
import { TOOLBAR_CHIP_CLASS, toolbarChipStyle } from '@/components/dalton/primitives/DaltonToolbarPill';
import { queryKeys } from '@/lib/queryKeys';
import { cn } from '@/lib/utils';
import {
  fetchWorkspaceSettings,
  fetchWorkspaceSharing,
  updateWorkspaceSettings,
  updateWorkspaceSharing,
  type WorkspaceAccessLevel,
  type WorkspaceSettingsResponse,
  type WorkspaceSharingResponse,
} from '../../utils/api/workspaces';

export {
  fetchWorkspaceSettings,
  fetchWorkspaceSharing,
  updateWorkspaceSettings,
  updateWorkspaceSharing,
  type WorkspaceAccessLevel,
  type WorkspaceSettingsResponse,
  type WorkspaceSharingResponse,
};

export const ACCESS_OPTIONS: Array<{
  value: WorkspaceAccessLevel;
  labelZh: string;
  labelKey: string;
}> = [
  { value: 'rw', labelZh: '可读写', labelKey: 'dalton.sharing.levelRw' },
  { value: 'ro', labelZh: '仅可读', labelKey: 'dalton.sharing.levelRo' },
  { value: 'hidden', labelZh: '不可见', labelKey: 'dalton.sharing.levelHidden' },
];

export function formatAccessLevelZh(level: WorkspaceAccessLevel | string | undefined): string {
  if (level === 'rw') return '可读写';
  if (level === 'ro') return '仅可读';
  return '不可见';
}

export function useWorkspaceSharing(
  workspaceId: string | null | undefined,
  enabled = true,
) {
  const id = workspaceId ?? '';
  return useQuery<WorkspaceSharingResponse>({
    queryKey: queryKeys.dalton.sharing(id),
    queryFn: () => fetchWorkspaceSharing(id),
    enabled: Boolean(id) && enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function useUpdateWorkspaceSharing(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: {
      cos_access?: WorkspaceAccessLevel;
      web_access: WorkspaceAccessLevel;
    }) => updateWorkspaceSharing(workspaceId, payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.dalton.sharing(workspaceId), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.team() });
    },
  });
}

export function useWorkspaceSettings(
  workspaceId: string | null | undefined,
  enabled = true,
) {
  const id = workspaceId ?? '';
  return useQuery<WorkspaceSettingsResponse>({
    queryKey: queryKeys.dalton.workspaceSettings(id),
    queryFn: () => fetchWorkspaceSettings(id),
    enabled: Boolean(id) && enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function useUpdateWorkspaceSettings(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { team_memory_enabled: boolean }) =>
      updateWorkspaceSettings(workspaceId, payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(
        queryKeys.dalton.workspaceSettings(workspaceId),
        updated,
      );
    },
  });
}

export function WorkspaceSharingModal({
  workspaceId,
  readOnly = false,
  compact: _compact = false,
}: {
  workspaceId: string;
  readOnly?: boolean;
  compact?: boolean;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const titleId = useId();
  const cosLegendId = useId();
  const webLegendId = useId();
  const teamMemoryTitleId = useId();
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError } = useWorkspaceSharing(workspaceId);
  const { data: settingsData } = useWorkspaceSettings(workspaceId);
  const mutation = useUpdateWorkspaceSharing(workspaceId);
  const settingsMutation = useUpdateWorkspaceSettings(workspaceId);

  const [draftCos, setDraftCos] = useState<WorkspaceAccessLevel>('ro');
  const [draftWeb, setDraftWeb] = useState<WorkspaceAccessLevel>('hidden');
  const [draftTeamMemory, setDraftTeamMemory] = useState<boolean | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  if (open && draftTeamMemory === null && settingsData !== undefined) {
    setDraftTeamMemory(settingsData.team_memory_enabled === true);
  }

  if (!workspaceId || isError || (isLoading && !data) || data?.is_home) {
    return null;
  }

  const cosAccess: WorkspaceAccessLevel = data?.cos_access ?? 'ro';
  const webAccess: WorkspaceAccessLevel = data?.web_access ?? 'hidden';
  const dispatchEnabled = data?.colleague_dispatch_enabled === true;
  const ownerExternal = data?.owner_external === true;
  const canEdit = data?.can_edit ?? !readOnly;
  const settingsLoaded = settingsData !== undefined;
  const canEditTeamMemory =
    settingsLoaded &&
    settingsData.can_edit === true &&
    !readOnly &&
    !ownerExternal;
  const showTeamMemorySection =
    !readOnly &&
    !ownerExternal &&
    (settingsLoaded ? settingsData.can_edit === true : canEdit);
  const isTeamMemoryDisabled = !canEditTeamMemory || mutation.isPending || settingsMutation.isPending;
  const isPending = mutation.isPending || settingsMutation.isPending;

  const handleOpen = () => {
    setSaveError(null);
    setDraftCos(cosAccess);
    setDraftWeb(webAccess);
    setDraftTeamMemory(
      settingsData !== undefined
        ? settingsData.team_memory_enabled === true
        : null,
    );
    setOpen(true);
  };

  const handleClose = () => {
    if (isPending) return;
    setSaveError(null);
    setOpen(false);
  };

  const handleSave = async () => {
    if (!canEdit || isPending) return;
    setSaveError(null);

    const sharingChanged =
      dispatchEnabled || ownerExternal
        ? draftWeb !== webAccess
        : draftWeb !== webAccess || draftCos !== cosAccess;

    const teamMemoryChanged =
      canEditTeamMemory &&
      draftTeamMemory !== null &&
      draftTeamMemory !== (settingsData.team_memory_enabled === true);

    if (!sharingChanged && !teamMemoryChanged) {
      setOpen(false);
      return;
    }

    const tasks: Promise<unknown>[] = [];
    if (sharingChanged) {
      tasks.push(
        mutation.mutateAsync(
          dispatchEnabled || ownerExternal
            ? { web_access: draftWeb }
            : { cos_access: draftCos, web_access: draftWeb },
        ),
      );
    }
    if (teamMemoryChanged) {
      tasks.push(
        settingsMutation.mutateAsync({
          team_memory_enabled: draftTeamMemory,
        }),
      );
    }

    const results = await Promise.allSettled(tasks);
    if (results.some((r) => r.status === 'rejected')) {
      setSaveError(
        t('dalton.teamMemory.saveFailed', '设置保存失败，请稍后重试'),
      );
      return;
    }
    setOpen(false);
  };

  return (
    <div
      className="inline-flex items-center relative"
      data-testid="workspace-sharing-control"
    >
      <button
        type="button"
        onClick={handleOpen}
        data-testid="workspace-sharing-button"
        className={cn(
          TOOLBAR_CHIP_CLASS,
          'press cursor-pointer hover:bg-(--color-bg-subtle)',
        )}
        style={toolbarChipStyle('neutral')}
        title={t('dalton.sharing.title')}
      >
        <Share2 className="h-3.5 w-3.5 shrink-0 opacity-75" />
        <span>{t('dalton.sharing.title')}</span>
      </button>

      {open &&
        typeof document !== 'undefined' &&
        createPortal(
          <ModalShell
            labelId={titleId}
            title={t('dalton.sharing.title')}
            onClose={handleClose}
            closeDisabled={isPending}
            width="narrow"
            density="form"
            footer={
              <div className="flex items-center justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleClose}
                  disabled={isPending}
                  data-testid="sharing-cancel-btn"
                >
                  {t('common.cancel')}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={!canEdit || isPending}
                  onClick={() => void handleSave()}
                  data-testid="sharing-save-btn"
                  style={{
                    backgroundColor: 'var(--color-btn-primary-bg)',
                    color: 'var(--color-btn-primary-text)',
                  }}
                >
                  {t('common.save')}
                </Button>
              </div>
            }
          >
            <div
              data-testid="workspace-sharing-modal"
              className="flex flex-col gap-4"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {!ownerExternal &&
                (dispatchEnabled ? (
                  <p
                    className="text-xs"
                    style={{ color: 'var(--color-text-secondary)' }}
                    data-testid="sharing-cos-settings-hint"
                  >
                    {t('dalton.sharing.cosManagedInSettingsHint')}
                  </p>
                ) : (
                  <fieldset
                    className="flex flex-col gap-2"
                    disabled={!canEdit || isPending}
                    data-testid="sharing-cos-fieldset"
                  >
                    <legend
                      id={cosLegendId}
                      className="text-xs font-medium mb-1"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.sharing.cosLabel')}
                    </legend>
                    <SegmentedControl<WorkspaceAccessLevel>
                      value={draftCos}
                      onChange={setDraftCos}
                      labelledBy={cosLegendId}
                      disabled={!canEdit || isPending}
                      options={ACCESS_OPTIONS.map((opt) => ({
                        value: opt.value,
                        label: t(opt.labelKey),
                        testId: `sharing-cos-${opt.value}`,
                      }))}
                    />
                  </fieldset>
                ))}

              <fieldset
                className="flex flex-col gap-2"
                disabled={!canEdit || isPending}
                data-testid="sharing-web-fieldset"
              >
                <legend
                  id={webLegendId}
                  className="text-xs font-medium mb-1"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {t('dalton.sharing.webLabel')}
                </legend>
                <SegmentedControl<WorkspaceAccessLevel>
                  value={draftWeb}
                  onChange={setDraftWeb}
                  labelledBy={webLegendId}
                  disabled={!canEdit || isPending}
                  options={ACCESS_OPTIONS.map((opt) => ({
                    value: opt.value,
                    label: t(opt.labelKey),
                    testId: `sharing-web-${opt.value}`,
                  }))}
                />
              </fieldset>

              {draftWeb === 'rw' && (
                <DaltonInlineNotice
                  tone="warning"
                  testId="sharing-web-rw-warning"
                >
                  <div className="flex flex-col gap-1.5">
                    <div
                      className="flex items-start gap-2 font-medium"
                      style={{ color: 'var(--color-text-primary)' }}
                    >
                      <AlertTriangle
                        className="h-4 w-4 shrink-0 mt-0.5"
                        style={{ color: 'var(--color-warning)' }}
                      />
                      <span>{t('dalton.sharing.webRwWarningTitle')}</span>
                    </div>
                    <p
                      className="text-xs pl-6"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.sharing.webDescRw')}
                    </p>
                    <p
                      className="text-xs whitespace-pre-line pl-6"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.sharing.webRwWarningBody')}
                    </p>
                  </div>
                </DaltonInlineNotice>
              )}

              <p
                className="text-xs"
                style={{ color: 'var(--color-text-tertiary)' }}
                data-testid="sharing-home-note"
              >
                {t('dalton.sharing.homeNote')}
              </p>

              {showTeamMemorySection && (
                <div
                  data-testid="workspace-team-memory-section"
                  className="rule-t flex items-center justify-between gap-3 pt-3"
                >
                  <div className="flex flex-col gap-0.5 min-w-0">
                    <span
                      id={teamMemoryTitleId}
                      className="text-xs font-medium"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.teamMemory.workspaceTitle')}
                    </span>
                    <span
                      className="text-xs"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {!settingsLoaded
                        ? t('dalton.teamMemory.loading')
                        : t('dalton.teamMemory.workspaceDesc')}
                    </span>
                  </div>
                  <ToggleSwitch
                    checked={draftTeamMemory === true}
                    disabled={isTeamMemoryDisabled}
                    ariaBusy={!settingsLoaded || settingsMutation.isPending}
                    ariaLabel={t('dalton.teamMemory.workspaceTitle')}
                    ariaDescribedBy={teamMemoryTitleId}
                    testId="workspace-team-memory-switch"
                    onChange={() => {
                      if (isTeamMemoryDisabled) return;
                      setDraftTeamMemory(
                        (prev) =>
                          !(
                            prev !== null
                              ? prev
                              : settingsData.team_memory_enabled === true
                          ),
                      );
                    }}
                  />
                </div>
              )}

              {saveError && (
                <DaltonInlineNotice
                  tone="error"
                  testId="workspace-sharing-save-error"
                >
                  <div className="flex items-center gap-2">
                    <AlertTriangle className="h-4 w-4 shrink-0" />
                    <span>{saveError}</span>
                  </div>
                </DaltonInlineNotice>
              )}

            </div>
          </ModalShell>,
          document.body,
        )}
    </div>
  );
}
