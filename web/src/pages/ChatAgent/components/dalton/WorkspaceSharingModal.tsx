/* eslint-disable react-refresh/only-export-components */
import React, { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Share2 } from 'lucide-react';
import { ModalShell } from '@/components/ui/ModalShell';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchWorkspaceSharing,
  updateWorkspaceSharing,
  type WorkspaceAccessLevel,
  type WorkspaceSharingResponse,
} from '../../utils/api/workspaces';

export {
  fetchWorkspaceSharing,
  updateWorkspaceSharing,
  type WorkspaceAccessLevel,
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
      cos_access: WorkspaceAccessLevel;
      web_access: WorkspaceAccessLevel;
    }) => updateWorkspaceSharing(workspaceId, payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.dalton.sharing(workspaceId), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.team() });
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
  const mutation = useUpdateWorkspaceSharing(workspaceId);

  const [draftCos, setDraftCos] = useState<WorkspaceAccessLevel>('ro');
  const [draftWeb, setDraftWeb] = useState<WorkspaceAccessLevel>('hidden');

  if (!workspaceId || isError || (isLoading && !data) || data?.is_home) {
    return null;
  }

  const cosAccess: WorkspaceAccessLevel = data?.cos_access ?? 'ro';
  const webAccess: WorkspaceAccessLevel = data?.web_access ?? 'hidden';
  const canEdit = data?.can_edit ?? !readOnly;

  const handleOpen = () => {
    setDraftCos(cosAccess);
    setDraftWeb(webAccess);
    setOpen(true);
  };

  const handleClose = () => {
    if (mutation.isPending) return;
    setOpen(false);
  };

  const handleSave = () => {
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(
      { cos_access: draftCos, web_access: draftWeb },
      {
        onSuccess: () => {
          setOpen(false);
        },
      },
    );
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
            closeDisabled={mutation.isPending}
            width="narrow"
            density="form"
            footer={
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={handleClose}
                  disabled={mutation.isPending}
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
                  disabled={!canEdit || mutation.isPending}
                  onClick={handleSave}
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
              <fieldset
                className="flex flex-col gap-2"
                disabled={!canEdit || mutation.isPending}
                data-testid="sharing-cos-fieldset"
              >
                <legend
                  className="text-xs font-medium mb-1"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {t('dalton.sharing.cosLabel', '别人的 Chief of Staff')}
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
                        disabled={!canEdit || mutation.isPending}
                        onChange={() => setDraftCos(opt.value)}
                        data-testid={`sharing-cos-${opt.value}`}
                      />
                      <span>{t(opt.labelKey, opt.labelZh)}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <fieldset
                className="flex flex-col gap-2"
                disabled={!canEdit || mutation.isPending}
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
                        disabled={!canEdit || mutation.isPending}
                        onChange={() => setDraftWeb(opt.value)}
                        data-testid={`sharing-web-${opt.value}`}
                      />
                      <span>{t(opt.labelKey, opt.labelZh)}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <p
                className="text-xs"
                style={{ color: 'var(--color-text-tertiary)' }}
                data-testid="sharing-home-note"
              >
                {t('dalton.sharing.homeNote', 'Home 工作区始终不对他人开放')}
              </p>

            </div>
          </ModalShell>,
          document.body,
        )}
    </div>
  );
}
