/* eslint-disable react-refresh/only-export-components */
import React, { useEffect, useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Share2, Users } from 'lucide-react';
import {
  apiErrorDetail,
  formatApiErrorDetail,
  isForbiddenError,
  isNotFoundError,
} from '@/api/errors';
import { Button } from '@/components/ui/button';
import { ModalShell } from '@/components/ui/ModalShell';
import { SegmentedControl } from '@/components/ui/segmented-control';
import { ToggleSwitch } from '@/components/ui/switch';
// Direct module imports: the primitives barrel also re-exports the
// confirm dialog, which would pull its animation library onto first load.
import { DaltonInlineNotice } from '@/components/dalton/primitives/DaltonStatusBadge';
import { TOOLBAR_CHIP_CLASS, toolbarChipStyle } from '@/components/dalton/primitives/DaltonToolbarPill';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
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
  compact = false,
}: {
  workspaceId: string;
  readOnly?: boolean;
  compact?: boolean;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const titleId = useId();
  const cosLegendId = useId();
  const webLegendId = useId();
  const teamMemoryTitleId = useId();
  const [open, setOpen] = useState(false);
  const identity = useCallerIdentity({ revalidateOnOpen: true, open });
  const { hasCapability } = useCapabilities();
  const sharingCapable = hasCapability('colleague_sharing');
  const teamMemoryCapable = hasCapability('conversation_archive');

  const sharingQuery = useWorkspaceSharing(workspaceId, sharingCapable);
  const settingsQuery = useWorkspaceSettings(
    workspaceId,
    teamMemoryCapable && identity.isInternal && !readOnly,
  );
  const mutation = useUpdateWorkspaceSharing(workspaceId);
  const settingsMutation = useUpdateWorkspaceSettings(workspaceId);

  const [draftCos, setDraftCos] = useState<WorkspaceAccessLevel>('ro');
  const [draftWeb, setDraftWeb] = useState<WorkspaceAccessLevel>('hidden');
  const [cosEdited, setCosEdited] = useState(false);
  const [webEdited, setWebEdited] = useState(false);
  const [draftTeamMemory, setDraftTeamMemory] = useState<boolean | null>(null);
  const [teamMemoryEdited, setTeamMemoryEdited] = useState(false);
  const [sharingWriteNotFound, setSharingWriteNotFound] = useState(false);
  const [settingsWriteNotFound, setSettingsWriteNotFound] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    setSharingWriteNotFound(false);
    setSettingsWriteNotFound(false);
  }, [workspaceId]);

  const data = sharingCapable && !sharingWriteNotFound ? sharingQuery.data : undefined;
  const settingsData =
    teamMemoryCapable && identity.isInternal && !readOnly && !settingsWriteNotFound
      ? settingsQuery.data
      : undefined;
  const sharingReady =
    sharingCapable && data !== undefined && !sharingQuery.isError && !sharingWriteNotFound;
  const sharingNotFound =
    sharingCapable &&
    (sharingWriteNotFound || (sharingQuery.isError && isNotFoundError(sharingQuery.error)));
  const settingsLoaded =
    settingsData !== undefined && !settingsQuery.isError && !settingsWriteNotFound;
  const settingsNotFound =
    teamMemoryCapable &&
    identity.isInternal &&
    !readOnly &&
    (settingsWriteNotFound || (settingsQuery.isError && isNotFoundError(settingsQuery.error)));

  if (open && sharingReady) {
    if (!cosEdited && draftCos !== data.cos_access) {
      setDraftCos(data.cos_access);
    }
    if (!webEdited && draftWeb !== data.web_access) {
      setDraftWeb(data.web_access);
    }
  }

  if (open && settingsLoaded) {
    const serverTeamMemory = settingsData.team_memory_enabled === true;
    if (!teamMemoryEdited && draftTeamMemory !== serverTeamMemory) {
      setDraftTeamMemory(serverTeamMemory);
    }
  }

  const hiddenByContext =
    !workspaceId ||
    (!sharingCapable && !teamMemoryCapable) ||
    data?.is_home === true ||
    settingsData?.is_home === true;

  const ownerExternal = (data?.owner_external ?? settingsData?.owner_external) === true;

  const baseSharingCanEdit = sharingReady ? (data.can_edit ?? !readOnly) : false;
  const canEditWebByServer =
    sharingReady && (data.can_edit_web_access ?? baseSharingCanEdit) && !readOnly;
  const dispatchEnabled = data?.colleague_dispatch_enabled === true;
  const canEditCosByServer =
    sharingReady &&
    !ownerExternal &&
    identity.isInternal &&
    (data.can_edit_cos_access ?? (baseSharingCanEdit && !dispatchEnabled)) &&
    !readOnly;

  const canEditTeamMemoryByServer =
    teamMemoryCapable &&
    identity.isInternal &&
    settingsLoaded &&
    (settingsData.can_edit_team_memory ?? settingsData.can_edit === true) &&
    !readOnly &&
    !ownerExternal;

  const showSharingSection = sharingReady;
  const showSharingError = sharingCapable && sharingQuery.isError && !sharingNotFound;
  const showSharingLoading = open && sharingCapable && sharingQuery.isPending;
  const showTeamMemorySection =
    teamMemoryCapable &&
    identity.isInternal &&
    !readOnly &&
    !ownerExternal &&
    settingsLoaded &&
    (settingsData.can_edit_team_memory ?? settingsData.can_edit === true);
  const showTeamMemoryError =
    teamMemoryCapable &&
    identity.isInternal &&
    !readOnly &&
    !ownerExternal &&
    settingsQuery.isError &&
    !settingsNotFound;
  const showTeamMemoryLoading =
    open &&
    teamMemoryCapable &&
    identity.isInternal &&
    !readOnly &&
    !ownerExternal &&
    settingsQuery.isPending;

  const hasModalContent =
    showSharingSection ||
    showSharingError ||
    showTeamMemorySection ||
    showTeamMemoryError;

  const hasVisibleModalBody =
    hasModalContent ||
    showSharingLoading ||
    showTeamMemoryLoading ||
    identity.isConfirming ||
    identity.status === 'error' ||
    saveError !== null;

  useEffect(() => {
    if (open && (hiddenByContext || !hasVisibleModalBody)) {
      setOpen(false);
    }
  }, [open, hiddenByContext, hasVisibleModalBody]);

  if (hiddenByContext || (!hasModalContent && (!open || !hasVisibleModalBody))) {
    return null;
  }

  const cosAccess: WorkspaceAccessLevel | null = sharingReady ? data.cos_access : null;
  const webAccess: WorkspaceAccessLevel | null = sharingReady ? data.web_access : null;
  const identityReady = identity.status === 'ready' && !identity.isConfirming;
  const isPending = mutation.isPending || settingsMutation.isPending;

  const canEditWebAccess = canEditWebByServer && identityReady;
  const canEditCosAccess = canEditCosByServer && identityReady;
  const canEditTeamMemory = canEditTeamMemoryByServer && identityReady;
  const canSaveAny = (canEditWebAccess || canEditCosAccess || canEditTeamMemory) && !isPending;
  const isTeamMemoryDisabled = !canEditTeamMemory || isPending;

  const handleOpen = () => {
    setSaveError(null);
    setCosEdited(false);
    setWebEdited(false);
    setTeamMemoryEdited(false);
    setSharingWriteNotFound(false);
    setSettingsWriteNotFound(false);
    if (cosAccess !== null) {
      setDraftCos(cosAccess);
    }
    if (webAccess !== null) {
      setDraftWeb(webAccess);
    }
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
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || isPending) return;
    setSaveError(null);

    const latestCanEditWeb =
      sharingReady && (data.can_edit_web_access ?? baseSharingCanEdit) && !readOnly;
    const latestCanEditCos =
      sharingReady &&
      !ownerExternal &&
      latest.isInternal &&
      (data.can_edit_cos_access ?? (baseSharingCanEdit && !dispatchEnabled)) &&
      !readOnly;
    const latestCanEditTeamMemory =
      teamMemoryCapable &&
      latest.isInternal &&
      settingsLoaded &&
      (settingsData.can_edit_team_memory ?? settingsData.can_edit === true) &&
      !readOnly &&
      !ownerExternal;

    const webChanged =
      webEdited && latestCanEditWeb && webAccess !== null && draftWeb !== webAccess;
    const cosChanged =
      cosEdited &&
      latestCanEditCos &&
      !dispatchEnabled &&
      cosAccess !== null &&
      draftCos !== cosAccess;
    const sharingChanged = webChanged || cosChanged;

    const teamMemoryChanged =
      teamMemoryEdited &&
      latestCanEditTeamMemory &&
      draftTeamMemory !== null &&
      draftTeamMemory !== (settingsData.team_memory_enabled === true);

    if (!sharingChanged && !teamMemoryChanged) {
      if (!latestCanEditWeb && !latestCanEditCos && !latestCanEditTeamMemory) {
        return;
      }
      setOpen(false);
      return;
    }

    const tasks: Array<{ kind: 'sharing' | 'settings'; promise: Promise<unknown> }> = [];
    if (sharingChanged) {
      tasks.push({
        kind: 'sharing',
        promise: mutation.mutateAsync(
          latestCanEditCos && !dispatchEnabled
            ? { cos_access: draftCos, web_access: draftWeb }
            : { web_access: draftWeb },
        ),
      });
    }
    if (teamMemoryChanged) {
      tasks.push({
        kind: 'settings',
        promise: settingsMutation.mutateAsync({
          team_memory_enabled: draftTeamMemory,
        }),
      });
    }

    const results = await Promise.allSettled(tasks.map((task) => task.promise));
    const rejections = results
      .map((res, idx) =>
        res.status === 'rejected' ? { kind: tasks[idx].kind, reason: res.reason } : null,
      )
      .filter((r): r is { kind: 'sharing' | 'settings'; reason: unknown } => r !== null);

    if (rejections.length > 0) {
      const notFoundRejections = rejections.filter((r) => isNotFoundError(r.reason));
      const otherRejections = rejections.filter((r) => !isNotFoundError(r.reason));

      let sharingHiddenBy404 = false;
      let settingsHiddenBy404 = false;
      if (notFoundRejections.length > 0) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.capabilities() });
        for (const rej of notFoundRejections) {
          if (rej.kind === 'sharing') {
            sharingHiddenBy404 = true;
            setCosEdited(false);
            setWebEdited(false);
            setSharingWriteNotFound(true);
            void queryClient.invalidateQueries({
              queryKey: queryKeys.dalton.sharing(workspaceId),
            });
          } else {
            settingsHiddenBy404 = true;
            setTeamMemoryEdited(false);
            setSettingsWriteNotFound(true);
            void queryClient.invalidateQueries({
              queryKey: queryKeys.dalton.workspaceSettings(workspaceId),
            });
          }
        }
      }

      if (otherRejections.length > 0) {
        if (otherRejections.some((r) => isForbiddenError(r.reason))) {
          void identity.revalidate();
        }
        const forbiddenWithDetail = otherRejections.find(
          (r) => isForbiddenError(r.reason) && apiErrorDetail(r.reason) != null,
        );
        setSaveError(
          forbiddenWithDetail
            ? formatApiErrorDetail(forbiddenWithDetail.reason)
            : t('dalton.teamMemory.saveFailed', '设置保存失败，请稍后重试'),
        );
        return;
      }

      const hasRemainingSection =
        (showSharingSection && !sharingHiddenBy404) ||
        (showTeamMemorySection && !settingsHiddenBy404);
      if (!hasRemainingSection) {
        setOpen(false);
      }
      return;
    }
    setOpen(false);
  };

  return (
    <div
      className="inline-flex items-center relative"
      data-testid="workspace-sharing-control"
      data-compact={compact ? 'true' : undefined}
    >
      <button
        type="button"
        onClick={handleOpen}
        data-testid="workspace-sharing-button"
        data-compact={compact ? 'true' : undefined}
        className={cn(
          TOOLBAR_CHIP_CLASS,
          'press cursor-pointer hover:bg-(--color-bg-subtle)',
          compact && 'tap-target w-8 justify-center px-0',
        )}
        style={toolbarChipStyle('neutral')}
        title={t('dalton.sharing.title')}
        aria-label={compact ? t('dalton.sharing.title') : undefined}
      >
        {compact ? (
          <Users className="h-3.5 w-3.5 shrink-0 opacity-75" aria-hidden="true" />
        ) : (
          <>
            <Share2 className="h-3.5 w-3.5 shrink-0 opacity-75" />
            <span>{t('dalton.sharing.title')}</span>
          </>
        )}
      </button>

      {open &&
        hasVisibleModalBody &&
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
                  disabled={!canSaveAny}
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
              {identity.isConfirming && (
                <DaltonInlineNotice tone="info" testId="workspace-sharing-identity-confirming">
                  {t('share.confirmingIdentity')}
                </DaltonInlineNotice>
              )}
              {identity.status === 'error' && !identity.isConfirming && (
                <DaltonInlineNotice
                  tone="error"
                  testId="workspace-sharing-identity-error"
                  action={
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => void identity.revalidate()}
                      data-testid="workspace-sharing-identity-retry"
                    >
                      {t('common.retry')}
                    </Button>
                  }
                >
                  {t('share.identityFailed')}
                </DaltonInlineNotice>
              )}

              {showSharingLoading && (
                <div
                  data-testid="workspace-sharing-loading"
                  className="flex items-center justify-between gap-3 text-xs"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  <span>{t('dalton.teamMemory.loading')}</span>
                </div>
              )}

              {showSharingError && (
                <DaltonInlineNotice
                  tone="error"
                  testId="workspace-sharing-load-error"
                  action={
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => void sharingQuery.refetch()}
                      disabled={sharingQuery.isFetching}
                      data-testid="workspace-sharing-load-retry"
                    >
                      {t('common.retry')}
                    </Button>
                  }
                >
                  {t('dalton.common.loadFailed')}
                </DaltonInlineNotice>
              )}

              {showSharingSection && (
                <>
                  {!ownerExternal && identity.isInternal &&
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
                        disabled={!canEditCosAccess || isPending}
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
                          onChange={(value) => {
                            setCosEdited(true);
                            setDraftCos(value);
                          }}
                          labelledBy={cosLegendId}
                          disabled={!canEditCosAccess || isPending}
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
                    disabled={!canEditWebAccess || isPending}
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
                      onChange={(value) => {
                        setWebEdited(true);
                        setDraftWeb(value);
                      }}
                      labelledBy={webLegendId}
                      disabled={!canEditWebAccess || isPending}
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
                </>
              )}

              {showTeamMemoryLoading && (
                <div
                  data-testid="workspace-team-memory-loading"
                  className={cn(
                    'flex items-center justify-between gap-3 text-xs',
                    (showSharingSection || showSharingError) && 'rule-t pt-3',
                  )}
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  <span>{t('dalton.teamMemory.loading')}</span>
                </div>
              )}

              {showTeamMemoryError && (
                <div className={cn((showSharingSection || showSharingError) && 'rule-t pt-3')}>
                  <DaltonInlineNotice
                    tone="error"
                    testId="workspace-team-memory-error"
                    action={
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-7 text-xs"
                        onClick={() => void settingsQuery.refetch()}
                        disabled={settingsQuery.isFetching}
                        data-testid="workspace-team-memory-retry"
                      >
                        {t('common.retry')}
                      </Button>
                    }
                  >
                    {t('dalton.common.loadFailed')}
                  </DaltonInlineNotice>
                </div>
              )}

              {showTeamMemorySection && (
                <div
                  data-testid="workspace-team-memory-section"
                  className={cn(
                    'flex items-center justify-between gap-3',
                    (showSharingSection || showSharingError) && 'rule-t pt-3',
                  )}
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
                      {t('dalton.teamMemory.workspaceDesc')}
                    </span>
                  </div>
                  <ToggleSwitch
                    checked={draftTeamMemory === true}
                    disabled={isTeamMemoryDisabled}
                    ariaBusy={settingsMutation.isPending}
                    ariaLabel={t('dalton.teamMemory.workspaceTitle')}
                    ariaDescribedBy={teamMemoryTitleId}
                    testId="workspace-team-memory-switch"
                    onChange={() => {
                      if (isTeamMemoryDisabled) return;
                      setTeamMemoryEdited(true);
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
