import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '@/api/client';
import { isDaltonAuth } from '@/config/daltonAuth';
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
  const [saveError, setSaveError] = useState(false);
  const optimisticNextRef = useRef<boolean | null>(null);
  const inFlightRef = useRef(false);

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.dalton.userSettings(),
    queryFn: fetchDaltonUserSettings,
    enabled: ctx ? canShowDaltonHomeTeamMemoryCard(ctx) : true,
  });

  const mutation = useMutation({
    mutationFn: (payload: { home_team_memory_enabled: boolean }) =>
      updateDaltonUserSettings(payload),
    onSuccess: (updated) => {
      inFlightRef.current = false;
      setSaveError(false);
      optimisticNextRef.current = null;
      queryClient.setQueryData(queryKeys.dalton.userSettings(), updated);
    },
    onError: () => {
      inFlightRef.current = false;
      setSaveError(true);
      if (optimisticNextRef.current !== null) {
        queryClient.setQueryData(queryKeys.dalton.userSettings(), {
          ...(data ?? {}),
          home_team_memory_enabled: !optimisticNextRef.current,
        });
        optimisticNextRef.current = null;
      }
    },
  });

  if (data?.caller_tier === 'external') {
    return null;
  }

  const enabled = data?.home_team_memory_enabled === true;
  const canEdit = data?.can_edit !== false;

  const handleToggle = (checked: boolean) => {
    if (isLoading || inFlightRef.current || mutation.isPending || !canEdit) return;
    inFlightRef.current = true;
    setSaveError(false);
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
      title={t('settings.dalton.homeTeamMemoryLabel')}
      description={t('settings.dalton.homeTeamMemoryDesc')}
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
              {t('dalton.common.saveFailed')}
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
