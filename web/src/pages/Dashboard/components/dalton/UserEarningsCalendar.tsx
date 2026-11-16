import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { TagBadge } from '@/components/mcp/McpPrimitives';
import { SegmentedControl } from '@/components/ui/segmented-control';
import { DaltonStatusBadge, type DaltonStatusTone } from '@/components/dalton/primitives';
import { useUser } from '@/hooks/useUser';
import { queryKeys } from '@/lib/queryKeys';
import { localDateStr } from '@/lib/utils';
import {
  type DaltonEarningsEntry,
  type EarningsScope,
  type UserEarningsCalendarResponse,
  getEarningsCalendar,
  getUserEarningsCalendar,
} from '../../utils/api';

export type {
  DaltonEarningsEntry,
  EarningsScope,
  UserEarningsCalendarResponse,
};

export function useEarningsCalendarQuery(scope: EarningsScope, nowMs: number) {
  const { user } = useUser();
  const userId = user?.user_id ?? null;
  const from = localDateStr(nowMs - 5 * 86400000);
  const to =
    scope === 'mine'
      ? localDateStr(nowMs + 90 * 86400000)
      : localDateStr(nowMs + 5 * 86400000);

  return useQuery({
    queryKey: queryKeys.dalton.earningsCalendar(scope, userId, from, to),
    queryFn: async (): Promise<DaltonEarningsEntry[]> => {
      if (scope === 'mine') {
        const result = await getUserEarningsCalendar({ from, to });
        return (result?.data || []) as DaltonEarningsEntry[];
      }
      const result = await getEarningsCalendar({ from, to });
      return ((result?.data || []) as DaltonEarningsEntry[]).filter(
        (e) => e.symbol && !e.symbol.includes('.'),
      );
    },
    staleTime: 60_000,
  });
}

interface EarningsScopeTabsProps {
  scope: EarningsScope;
  onScopeChange: (scope: EarningsScope) => void;
}

export function EarningsScopeTabs({
  scope,
  onScopeChange,
}: EarningsScopeTabsProps) {
  const { t } = useTranslation();
  return (
    <SegmentedControl<EarningsScope>
      size="compact"
      value={scope}
      onChange={onScopeChange}
      label={t('dashboard.widgets.earningsCalendar.title')}
      options={[
        { value: 'mine', label: t('dashboard.earningsCalendarCard.tabMine') },
        { value: 'market', label: t('dashboard.earningsCalendarCard.tabMarket') },
      ]}
    />
  );
}

interface UserEarningsBadgesProps {
  item: DaltonEarningsEntry;
}

export function UserEarningsBadges({ item }: UserEarningsBadgesProps) {
  const { t } = useTranslation();

  const rawTier = String(item.tier || '').toLowerCase();
  const isCore = rawTier === 'core';
  const isWatch = rawTier === 'watch';

  const rawStatus = String(item.status || '').toLowerCase();
  const rawDateStatus = String(item.date_status || '').toLowerCase();
  let statusLabel = '';
  let statusTone: DaltonStatusTone = 'neutral';

  if (rawStatus === 'reported' || rawDateStatus === 'reported') {
    statusLabel = t('dashboard.earningsCalendarCard.statusReported');
  } else if (rawStatus === 'confirmed' || rawDateStatus === 'confirmed') {
    statusLabel = t('dashboard.earningsCalendarCard.statusConfirmed');
    statusTone = 'success';
  } else if (rawStatus === 'pending' || rawDateStatus === 'estimated') {
    statusLabel = t('dashboard.earningsCalendarCard.statusPending');
    statusTone = 'warning';
  }

  const rawTiming = String(item.timing || '').toUpperCase();
  const timingLabel =
    rawTiming === 'BMO'
      ? t('dashboard.earningsCalendarCard.timingBMO')
      : rawTiming === 'AMC'
        ? t('dashboard.earningsCalendarCard.timingAMC')
        : '';

  if (!isCore && !isWatch && !statusLabel && !timingLabel) {
    return null;
  }

  return (
    <div className="flex flex-wrap items-center gap-1 mt-1">
      {(isCore || isWatch) && (
        <TagBadge soft={isWatch}>
          {isCore
            ? t('dashboard.earningsCalendarCard.tierCore')
            : t('dashboard.earningsCalendarCard.tierWatch')}
        </TagBadge>
      )}
      {statusLabel && (
        <DaltonStatusBadge
          tone={statusTone}
          dot={statusTone !== 'neutral'}
          label={statusLabel}
          className="px-1.5 py-0 text-[10px]"
        />
      )}
      {timingLabel && <TagBadge soft>{timingLabel}</TagBadge>}
    </div>
  );
}
