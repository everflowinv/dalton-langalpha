/* eslint-disable react-refresh/only-export-components */
import React, { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Share2 } from 'lucide-react';
import { ModalShell } from '@/components/ui/ModalShell';
import { queryKeys } from '@/lib/queryKeys';
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
  const titleId = useId();
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
        className={`inline-flex items-center gap-1.5 rounded-full border transition-colors hover:bg-foreground/5 ${
          compact ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-xs'
        }`}
        style={{
          borderColor: 'var(--color-border-muted)',
          backgroundColor: 'var(--color-bg-elevated)',
          color: 'var(--color-text-secondary)',
        }}
        title={t('dalton.sharing.title', '共享设置')}
      >
        <Share2 className="h-3.5 w-3.5 shrink-0 opacity-75" />
        <span>{t('dalton.sharing.title', '共享设置')}</span>
      </button>

      {open &&
        typeof document !== 'undefined' &&
        createPortal(
          <ModalShell
            labelId={titleId}
            title={t('dalton.sharing.title', '共享设置')}
            onClose={handleClose}
            closeDisabled={isPending}
            width="narrow"
            density="form"
            footer={
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={handleClose}
                  disabled={isPending}
                  data-testid="sharing-cancel-btn"
                  className="px-3 py-1.5 rounded-md text-xs border transition-colors hover:bg-foreground/5 disabled:opacity-50"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  {t('common.cancel', '取消')}
                </button>
                <button
                  type="button"
                  disabled={!canEdit || isPending}
                  onClick={() => void handleSave()}
                  data-testid="sharing-save-btn"
                  className="px-3 py-1.5 rounded-md text-xs font-medium transition-opacity hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{
                    backgroundColor: 'var(--color-btn-primary-bg)',
                    color: 'var(--color-btn-primary-text)',
                  }}
                >
                  {t('common.save', '保存')}
                </button>
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
                      className="text-xs font-medium mb-1"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.sharing.cosLabel', '别人的研究总监')}
                    </legend>
                    <div className="flex items-center gap-4">
                      {ACCESS_OPTIONS.map((opt) => (
                        <label
                          key={`cos-${opt.value}`}
                          className={`inline-flex items-center gap-1.5 text-xs ${
                            canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'
                          }`}
                        >
                          <input
                            type="radio"
                            name="cos_access"
                            value={opt.value}
                            checked={draftCos === opt.value}
                            disabled={!canEdit || isPending}
                            onChange={() => setDraftCos(opt.value)}
                            data-testid={`sharing-cos-${opt.value}`}
                          />
                          <span>{t(opt.labelKey, opt.labelZh)}</span>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                ))}

              <fieldset
                className="flex flex-col gap-2"
                disabled={!canEdit || isPending}
                data-testid="sharing-web-fieldset"
              >
                <legend
                  className="text-xs font-medium mb-1"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {t('dalton.sharing.webLabel', '别人的网页界面')}
                </legend>
                <div className="flex items-center gap-4">
                  {ACCESS_OPTIONS.map((opt) => (
                    <label
                      key={`web-${opt.value}`}
                      className={`inline-flex items-center gap-1.5 text-xs ${
                        canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'
                      }`}
                    >
                      <input
                        type="radio"
                        name="web_access"
                        value={opt.value}
                        checked={draftWeb === opt.value}
                        disabled={!canEdit || isPending}
                        onChange={() => setDraftWeb(opt.value)}
                        data-testid={`sharing-web-${opt.value}`}
                      />
                      <span>{t(opt.labelKey, opt.labelZh)}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              {draftWeb === 'rw' && (
                <div
                  data-testid="sharing-web-rw-warning"
                  className="flex flex-col gap-1.5 rounded-md border px-3 py-2.5 text-xs"
                  style={{
                    borderColor: 'var(--color-warning, #f59e0b)',
                    backgroundColor: 'rgba(245, 158, 11, 0.08)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  <div className="flex items-start gap-2 font-medium">
                    <AlertTriangle
                      className="h-4 w-4 shrink-0 mt-0.5"
                      style={{ color: 'var(--color-warning, #f59e0b)' }}
                    />
                    <span>{t('dalton.sharing.webRwWarningTitle')}</span>
                  </div>
                  <p className="text-xs opacity-90 pl-6">
                    {t('dalton.sharing.webDescRw')}
                  </p>
                  <p className="text-xs whitespace-pre-line pl-6">
                    {t('dalton.sharing.webRwWarningBody')}
                  </p>
                </div>
              )}

              <p
                className="text-xs"
                style={{ color: 'var(--color-text-tertiary)' }}
                data-testid="sharing-home-note"
              >
                {t('dalton.sharing.homeNote', 'Home 工作区始终不对他人开放')}
              </p>

              {showTeamMemorySection && (
                <div
                  data-testid="workspace-team-memory-section"
                  className="flex items-center justify-between gap-3 pt-3 border-t"
                  style={{ borderColor: 'var(--color-border-muted)' }}
                >
                  <div className="flex flex-col gap-0.5 min-w-0">
                    <span
                      className="text-xs font-medium"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {t('dalton.teamMemory.workspaceTitle', '团队记忆')}
                    </span>
                    <span
                      className="text-xs"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {!settingsLoaded
                        ? t('dalton.teamMemory.loading', '正在加载设置…')
                        : t(
                            'dalton.teamMemory.workspaceDesc',
                            '允许分析师查阅团队过去的对话记录（OpenClaw 对话卡片）',
                          )}
                    </span>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={draftTeamMemory === true}
                    aria-busy={!settingsLoaded || settingsMutation.isPending}
                    aria-label={t('dalton.teamMemory.workspaceTitle', '团队记忆')}
                    disabled={isTeamMemoryDisabled}
                    onClick={() => {
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
                    data-testid="workspace-team-memory-switch"
                    className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
                      isTeamMemoryDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
                    }`}
                    style={{
                      backgroundColor:
                        draftTeamMemory === true
                          ? 'var(--color-accent-primary)'
                          : 'var(--color-border-default)',
                    }}
                  >
                    <span
                      className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                        draftTeamMemory === true ? 'translate-x-4' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>
              )}

              {saveError && (
                <div
                  role="alert"
                  data-testid="workspace-sharing-save-error"
                  className="flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
                  style={{
                    borderColor: 'var(--color-danger, #ef4444)',
                    backgroundColor: 'rgba(239, 68, 68, 0.08)',
                    color: 'var(--color-danger, #ef4444)',
                  }}
                >
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span>{saveError}</span>
                </div>
              )}

            </div>
          </ModalShell>,
          document.body,
        )}
    </div>
  );
}
