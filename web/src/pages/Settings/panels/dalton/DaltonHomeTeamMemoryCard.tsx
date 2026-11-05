/* eslint-disable react-refresh/only-export-components */
import React, { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';

export function canShowDaltonHomeTeamMemoryCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!ctx) return true;
  if (ctx.callerTier === 'external') return false;
  return true;
}

export interface UserTeamMemorySettings {
  user_id: string;
  home_team_memory_enabled: boolean;
  can_edit?: boolean;
  caller_tier?: 'admin' | 'internal' | 'external' | string;
  updated_at?: string | null;
}

export async function fetchUserTeamMemorySettings(): Promise<UserTeamMemorySettings> {
  const { data } = await api.get<UserTeamMemorySettings>(
    '/api/v1/dalton/user-settings',
  );
  return data;
}

export async function updateUserTeamMemorySettings(payload: {
  home_team_memory_enabled: boolean;
}): Promise<UserTeamMemorySettings> {
  const { data } = await api.put<UserTeamMemorySettings>(
    '/api/v1/dalton/user-settings',
    payload,
  );
  return data;
}

export function useUserTeamMemoryQuery(enabled = true) {
  return useQuery<UserTeamMemorySettings>({
    queryKey: queryKeys.dalton.userSettings(),
    queryFn: fetchUserTeamMemorySettings,
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function DaltonHomeTeamMemoryCard(): React.ReactElement | null {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [toggleError, setToggleError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  const { data, isLoading } = useUserTeamMemoryQuery();

  const mutation = useMutation({
    mutationFn: (payload: { home_team_memory_enabled: boolean }) =>
      updateUserTeamMemorySettings(payload),
    onSuccess: (updated) => {
      setToggleError(null);
      queryClient.setQueryData(queryKeys.dalton.userSettings(), updated);
    },
    onError: () => {
      setToggleError(
        t('dalton.teamMemory.toggleFailed', '切换失败，请稍后重试'),
      );
    },
    onSettled: () => {
      inFlightRef.current = false;
    },
  });

  if (
    !data ||
    !canShowDaltonHomeTeamMemoryCard({
      callerTier: data.caller_tier,
    })
  ) {
    return null;
  }

  const enabled = data.home_team_memory_enabled === true;
  const isDisabled =
    isLoading || data.can_edit === false || mutation.isPending;

  const handleToggle = () => {
    if (isDisabled || inFlightRef.current) return;
    inFlightRef.current = true;
    setToggleError(null);
    mutation.mutate({ home_team_memory_enabled: !enabled });
  };

  return (
    <div
      id="dalton-home-team-memory"
      className="flex flex-col gap-3 p-4 rounded-lg"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-default)',
      }}
      data-testid="dalton-home-team-memory-card"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <span
            className="text-sm font-medium"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('dalton.teamMemory.homeTitle', 'Home 团队记忆')}
          </span>
          <span
            className="text-xs"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t(
              'dalton.teamMemory.homeDesc',
              '允许研究总监（Home）查阅团队过去的对话记录（OpenClaw 对话卡片）',
            )}
          </span>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-busy={mutation.isPending}
          aria-label={t(
            'dalton.teamMemory.homeTitle',
            'Home 团队记忆',
          )}
          disabled={isDisabled}
          onClick={handleToggle}
          data-testid="home-team-memory-switch"
          className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
            isDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
          }`}
          style={{
            backgroundColor: enabled
              ? 'var(--color-accent-primary)'
              : 'var(--color-border-default)',
          }}
        >
          <span
            className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
              enabled ? 'translate-x-4' : 'translate-x-1'
            }`}
          />
        </button>
      </div>
      {toggleError && (
        <p
          role="alert"
          data-testid="home-team-memory-error"
          className="text-xs"
          style={{ color: 'var(--color-loss)' }}
        >
          {toggleError}
        </p>
      )}
    </div>
  );
}
