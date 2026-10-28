import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';

export interface DaltonSettingVisibilityContext {
  canInvite?: boolean;
  callerTier?: string | null;
  colleagueDispatchEnabled?: boolean;
  /** The sharing settings have arrived (cached data counts, even while a
   *  background refresh fails); until then the card and its search entry
   *  both stay hidden, so neither flashes for a user it is not for. */
  cosSharingLoaded?: boolean;
}

export function canShowDaltonCosSharingCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!ctx) return true;
  if (!ctx.cosSharingLoaded) return false;
  if (ctx.callerTier === 'external') return false;
  if (ctx.colleagueDispatchEnabled === false) return false;
  return true;
}

export interface UserCosSharingSettings {
  user_id: string;
  cos_rw_enabled: boolean;
  cos_access?: 'rw' | 'hidden' | string;
  can_edit?: boolean;
  caller_tier?: 'admin' | 'internal' | 'external' | string;
  colleague_dispatch_enabled?: boolean;
  updated_at?: string | null;
}

export async function fetchUserCosSharing(): Promise<UserCosSharingSettings> {
  const { data } = await api.get<UserCosSharingSettings>(
    '/api/v1/dalton/sharing/user-settings',
  );
  return data;
}

export async function updateUserCosSharing(payload: {
  cos_rw_enabled: boolean;
}): Promise<UserCosSharingSettings> {
  const { data } = await api.put<UserCosSharingSettings>(
    '/api/v1/dalton/sharing/user-settings',
    payload,
  );
  return data;
}

export function useUserCosSharingQuery(enabled = true) {
  return useQuery<UserCosSharingSettings>({
    queryKey: queryKeys.dalton.userSharing(),
    queryFn: fetchUserCosSharing,
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function DaltonCosSharingCard(): React.ReactElement | null {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const { data, isLoading } = useUserCosSharingQuery();

  const mutation = useMutation({
    mutationFn: (payload: { cos_rw_enabled: boolean }) => updateUserCosSharing(payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.dalton.userSharing(), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.team() });
    },
  });

  if (
    !canShowDaltonCosSharingCard({
      cosSharingLoaded: data !== undefined,
      callerTier: data?.caller_tier,
      colleagueDispatchEnabled: data?.colleague_dispatch_enabled,
    })
  ) {
    return null;
  }

  const cosRwEnabled = data?.cos_rw_enabled ?? true;
  const isDisabled = isLoading || !data || data.can_edit === false || mutation.isPending;

  const handleToggle = () => {
    if (isDisabled) return;
    mutation.mutate({ cos_rw_enabled: !cosRwEnabled });
  };

  return (
    <div
      id="dalton-cos-sharing"
      className="flex flex-col gap-3 p-4 rounded-lg"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-default)',
      }}
      data-testid="dalton-cos-sharing-card"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <span
            className="text-sm font-medium"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('dalton.sharing.cosUserToggleTitle')}
          </span>
          <span
            className="text-xs"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('dalton.sharing.cosUserToggleLabel')}
          </span>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={cosRwEnabled}
          aria-label={t('dalton.sharing.cosUserToggleLabel')}
          disabled={isDisabled}
          onClick={handleToggle}
          data-testid="cos-user-sharing-switch"
          className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
            isDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
          }`}
          style={{
            backgroundColor: cosRwEnabled
              ? 'var(--color-accent-primary)'
              : 'var(--color-border-default)',
          }}
        >
          <span
            className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
              cosRwEnabled ? 'translate-x-4' : 'translate-x-1'
            }`}
          />
        </button>
      </div>

      <p
        className="text-xs"
        style={{ color: 'var(--color-text-tertiary)' }}
        data-testid="cos-user-scope-hint"
      >
        {t('dalton.sharing.cosUserToggleScopeHint')}
      </p>

      <div
        data-testid="cos-user-trust-notice"
        className="flex items-start gap-2 rounded-md border px-3 py-2.5 text-xs whitespace-pre-line"
        style={
          cosRwEnabled
            ? {
                borderColor: 'var(--color-warning, #f59e0b)',
                backgroundColor: 'rgba(245, 158, 11, 0.08)',
                color: 'var(--color-text-secondary)',
              }
            : {
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-subtle, rgba(128,128,128,0.05))',
                color: 'var(--color-text-tertiary)',
              }
        }
      >
        <AlertTriangle
          className="h-4 w-4 shrink-0 mt-0.5"
          style={{
            color: cosRwEnabled
              ? 'var(--color-warning, #f59e0b)'
              : 'var(--color-text-tertiary)',
          }}
        />
        <span>{t('dalton.sharing.cosUserToggleTrustNotice')}</span>
      </div>
    </div>
  );
}
