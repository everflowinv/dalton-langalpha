import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '@/api/client';
import { apiErrorDetail, formatApiErrorDetail, isForbiddenError } from '@/api/errors';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { hasRequiredCapability, useCapabilities } from '@/hooks/useCapabilities';
import { queryKeys } from '@/lib/queryKeys';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import { ToggleSwitch } from '@/components/ui/switch';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

export function canShowDaltonHomeTeamMemoryCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  return (
    isDaltonAuth &&
    Boolean(ctx) &&
    hasRequiredCapability(ctx?.capabilities, 'conversation_archive') &&
    (ctx?.callerTier === 'admin' || ctx?.callerTier === 'internal')
  );
}

export const isDaltonHomeTeamMemorySettingVisible = canShowDaltonHomeTeamMemoryCard;

export interface DaltonUserSettingsResponse {
  user_id?: string;
  home_team_memory_enabled: boolean;
  can_edit?: boolean;
  caller_tier?: string | null;
  updated_at?: string | null;
}

export type UserTeamMemorySettings = DaltonUserSettingsResponse;

async function fetchDaltonUserSettings(): Promise<DaltonUserSettingsResponse> {
  const { data } = await api.get<DaltonUserSettingsResponse>('/api/v1/dalton/user-settings');
  return data;
}

async function updateDaltonUserSettings(payload: {
  home_team_memory_enabled: boolean;
}): Promise<DaltonUserSettingsResponse> {
  const { data } = await api.put<DaltonUserSettingsResponse>(
    '/api/v1/dalton/user-settings',
    payload,
  );
  return data;
}

/** Personal toggle allowing Chief of Staff coordination to read teammates' archived research. */
export function DaltonHomeTeamMemoryCard({
  ctx,
}: {
  ctx?: DaltonSettingVisibilityContext | null;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  const [saveError, setSaveError] = useState<string | null>(null);
  const optimisticNextRef = useRef<boolean | null>(null);
  const inFlightRef = useRef(false);

  const archiveEnabled = ctx?.capabilities
    ? hasRequiredCapability(ctx.capabilities, 'conversation_archive')
    : hasCapability('conversation_archive');
  const allowed =
    archiveEnabled &&
    identity.isInternal &&
    (ctx ? canShowDaltonHomeTeamMemoryCard(ctx) : true);

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.dalton.userSettings(),
    queryFn: fetchDaltonUserSettings,
    enabled: allowed,
  });

  const mutation = useMutation({
    mutationFn: (payload: { home_team_memory_enabled: boolean }) =>
      updateDaltonUserSettings(payload),
    onSuccess: (updated) => {
      inFlightRef.current = false;
      setSaveError(null);
      optimisticNextRef.current = null;
      queryClient.setQueryData(queryKeys.dalton.userSettings(), updated);
    },
    onError: (err) => {
      inFlightRef.current = false;
      if (isForbiddenError(err)) {
        void identity.revalidate();
      }
      setSaveError(
        isForbiddenError(err) && apiErrorDetail(err) != null
          ? formatApiErrorDetail(err)
          : t('dalton.common.saveFailed'),
      );
      if (optimisticNextRef.current !== null) {
        queryClient.setQueryData(queryKeys.dalton.userSettings(), {
          ...(data ?? {}),
          home_team_memory_enabled: !optimisticNextRef.current,
        });
        optimisticNextRef.current = null;
      }
    },
  });

  if (!allowed) {
    return null;
  }

  const enabled = data?.home_team_memory_enabled === true;
  const canEdit = data?.can_edit !== false && identity.status === 'ready' && !identity.isConfirming;

  const handleToggle = (checked: boolean) => {
    const latest = identity.getLatestIdentity();
    if (
      isLoading ||
      inFlightRef.current ||
      mutation.isPending ||
      !canEdit ||
      latest.isConfirming ||
      latest.status !== 'ready' ||
      !latest.isInternal
    ) {
      return;
    }
    inFlightRef.current = true;
    setSaveError(null);
    optimisticNextRef.current = checked;
    queryClient.setQueryData(queryKeys.dalton.userSettings(), {
      ...(data ?? {}),
      home_team_memory_enabled: checked,
    });
    mutation.mutate({ home_team_memory_enabled: checked });
  };

  return (
    <DaltonSettingsCard
      id="dalton-home-team-memory"
      testId="dalton-home-team-memory-card"
      title={t('settings:dalton.homeTeamMemoryLabel')}
      description={t('settings:dalton.homeTeamMemoryDesc')}
      layout="rows"
      action={
        !isLoading && !error ? (
          <DaltonStatusBadge
            tone={enabled ? 'success' : 'neutral'}
            dot
            label={
              enabled
                ? t('dalton.homeTeamMemoryCard.enabledBadge')
                : t('dalton.homeTeamMemoryCard.disabledBadge')
            }
          />
        ) : undefined
      }
      below={
        <div className="space-y-2 mt-2">
          {saveError && (
            <DaltonInlineNotice tone="error" testId="dalton-home-team-memory-error">
              {saveError}
            </DaltonInlineNotice>
          )}
          <DaltonInlineNotice tone="info">
            {t('dalton.homeTeamMemoryCard.notice')}
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
          id="dalton-home-team-memory-toggle"
          label={t('dalton.homeTeamMemoryCard.toggleLabel')}
          description={t('dalton.homeTeamMemoryCard.toggleDesc')}
        >
          <ToggleSwitch
            id="dalton-home-team-memory-toggle-switch"
            ariaLabel={t('dalton.homeTeamMemoryCard.toggleLabel')}
            checked={enabled}
            disabled={!canEdit || mutation.isPending}
            onChange={() => handleToggle(!enabled)}
          />
        </SettingRow>
      )}
    </DaltonSettingsCard>
  );
}
