import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '@/api/client';
import { apiErrorDetail, formatApiErrorDetail, isForbiddenError, isNotFoundError } from '@/api/errors';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { hasRequiredCapability, useCapabilities, type CapabilityId } from '@/hooks/useCapabilities';
import { queryKeys } from '@/lib/queryKeys';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import { ToggleSwitch } from '@/components/ui/switch';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

export interface DaltonSettingVisibilityContext {
  callerTier?: string | null;
  callerTierPending?: boolean;
  callerTierError?: boolean;
  canInvite?: boolean;
  isPm?: boolean;
  colleagueDispatchEnabled?: boolean;
  cosSharingLoaded?: boolean;
  coverageUnitsEnabled?: boolean;
  coverageUnitsPending?: boolean;
  discordChannelsAdmin?: boolean;
  weixinAllowExternal?: boolean;
  hasPersonalIntegrations?: boolean;
  capabilities?: ReadonlySet<CapabilityId>;
  capabilitiesLoading?: boolean;
}

export interface UserSharingSettingsResponse {
  user_id?: string;
  cos_rw_enabled: boolean;
  cos_access?: string;
  can_edit?: boolean;
  colleague_dispatch_enabled?: boolean;
  caller_tier?: string | null;
  updated_at?: string | null;
}

export type UserCosSharingSettings = UserSharingSettingsResponse;

export async function fetchUserSharingSettings(): Promise<UserSharingSettingsResponse> {
  const { data } = await api.get<UserSharingSettingsResponse>(
    '/api/v1/dalton/sharing/user-settings',
  );
  return data;
}

async function updateUserSharingSettings(payload: {
  cos_rw_enabled: boolean;
}): Promise<UserSharingSettingsResponse> {
  const { data } = await api.put<UserSharingSettingsResponse>(
    '/api/v1/dalton/sharing/user-settings',
    payload,
  );
  return data;
}

export function useUserCosSharingQuery(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.userSharing(),
    queryFn: fetchUserSharingSettings,
    enabled,
    retry: false,
  });
}

export function canShowDaltonCosSharingCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!isDaltonAuth || !ctx) return false;
  if (!hasRequiredCapability(ctx.capabilities, 'colleague_sharing')) return false;
  if (ctx.callerTier === 'external') return false;
  if (ctx.cosSharingLoaded && ctx.colleagueDispatchEnabled === false) return false;
  return ctx.callerTier === 'admin' || ctx.callerTier === 'internal';
}

export const isDaltonCosSharingSettingVisible = canShowDaltonCosSharingCard;

/** Personal Chief of Staff workspace sharing toggle & simplified trust notice. */
export function DaltonCosSharingCard({
  ctx,
}: {
  ctx?: DaltonSettingVisibilityContext | null;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  const colleagueSharingEnabled = ctx?.capabilities
    ? hasRequiredCapability(ctx.capabilities, 'colleague_sharing')
    : hasCapability('colleague_sharing');
  const allowed =
    colleagueSharingEnabled &&
    identity.isInternal &&
    (ctx ? canShowDaltonCosSharingCard(ctx) : true);

  const { data, isLoading, error } = useUserCosSharingQuery(allowed);

  const mutation = useMutation({
    mutationFn: (payload: { cos_rw_enabled: boolean }) =>
      updateUserSharingSettings(payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.dalton.userSharing(), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.team() });
    },
    onError: (err) => {
      if (isForbiddenError(err)) {
        void identity.revalidate();
      }
      if (isNotFoundError(err)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.capabilities() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.userSharing() });
      }
    },
  });

  if (
    !allowed ||
    data?.colleague_dispatch_enabled === false ||
    isNotFoundError(error) ||
    isNotFoundError(mutation.error)
  ) {
    return null;
  }

  const enabled = data?.cos_rw_enabled === true;
  const canEdit = data?.can_edit !== false && identity.status === 'ready' && !identity.isConfirming;
  const notices = [
    t('dalton.cosSharing.notice1'),
    t('dalton.cosSharing.notice2'),
    t('dalton.cosSharing.notice3'),
  ];

  const handleToggle = (checked: boolean) => {
    const latest = identity.getLatestIdentity();
    if (
      isLoading ||
      mutation.isPending ||
      !canEdit ||
      latest.isConfirming ||
      latest.status !== 'ready' ||
      !latest.isInternal
    ) {
      return;
    }
    mutation.mutate({ cos_rw_enabled: checked });
  };

  return (
    <DaltonSettingsCard
      id="dalton-cos-sharing"
      testId="dalton-cos-sharing-card"
      title={t('settings:dalton.cosSharingLabel')}
      description={t('settings:dalton.cosSharingDesc')}
      layout="rows"
      action={
        !isLoading && !error ? (
          <DaltonStatusBadge
            tone={enabled ? 'success' : 'neutral'}
            dot
            label={
              enabled
                ? t('dalton.cosSharing.enabledBadge')
                : t('dalton.cosSharing.disabledBadge')
            }
          />
        ) : undefined
      }
      below={
        <div className="space-y-2 mt-2">
          {mutation.isError && !isNotFoundError(mutation.error) && (
            <DaltonInlineNotice tone="error" testId="dalton-cos-sharing-error">
              {isForbiddenError(mutation.error) && apiErrorDetail(mutation.error) != null
                ? formatApiErrorDetail(mutation.error)
                : t('dalton.common.saveFailed')}
            </DaltonInlineNotice>
          )}
          <DaltonInlineNotice tone="info">
            <div className="font-medium mb-1">{t('dalton.cosSharing.noticeTitle')}</div>
            <ul className="list-disc pl-4 space-y-0.5 text-xs">
              {(Array.isArray(notices) ? notices : []).map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          </DaltonInlineNotice>
        </div>
      }
    >
      {isLoading ? (
        <div className="p-4">
          <DaltonLoadingRow label={t('dalton.common.loading')} />
        </div>
      ) : error ? (
        <div className="p-4">
          <DaltonInlineNotice tone="error">{t('dalton.common.loadFailed')}</DaltonInlineNotice>
        </div>
      ) : (
        <SettingRow
          id="dalton-cos-sharing-toggle"
          label={t('dalton.cosSharing.toggleLabel')}
          description={t('dalton.cosSharing.toggleDesc')}
        >
          <ToggleSwitch
            id="dalton-cos-sharing-toggle-switch"
            ariaLabel={t('dalton.cosSharing.toggleLabel')}
            checked={enabled}
            disabled={!canEdit || mutation.isPending}
            onChange={() => handleToggle(!enabled)}
          />
        </SettingRow>
      )}
    </DaltonSettingsCard>
  );
}
