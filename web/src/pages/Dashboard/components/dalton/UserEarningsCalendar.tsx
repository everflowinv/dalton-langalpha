import React from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '@/api/client';

export type EarningsScope = 'mine' | 'market';

export interface DaltonEarningsEntry {
  symbol: string;
  ticker?: string;
  companyName?: string;
  date: string;
  quarter?: string | null;
  status?: 'confirmed' | 'pending' | 'reported' | string;
  date_status?: 'confirmed' | 'estimated' | 'reported' | 'unannounced' | string;
  tier?: 'Core' | 'Watch' | 'core' | 'watch' | string;
  timing?: 'BMO' | 'AMC' | 'unknown' | string | null;
  [key: string]: unknown;
}

export interface UserEarningsCalendarResponse {
  ok: boolean;
  user_id?: string;
  from?: string | null;
  to?: string | null;
  mirror_empty?: boolean;
  published_at?: string | null;
  count: number;
  data: DaltonEarningsEntry[];
}

export async function getUserEarningsCalendar(params: {
  from?: string;
  to?: string;
}): Promise<UserEarningsCalendarResponse> {
  const { data } = await api.get<UserEarningsCalendarResponse>(
    '/api/v1/dalton/earnings/calendar',
    { params },
  );
  return (
    data ?? {
      ok: true,
      count: 0,
      data: [],
      mirror_empty: true,
    }
  );
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
  const tabs: { key: EarningsScope; label: string }[] = [
    { key: 'mine', label: t('dashboard.earningsCalendarCard.tabMine') },
    { key: 'market', label: t('dashboard.earningsCalendarCard.tabMarket') },
  ];

  return (
    <div
      className="inline-flex items-center rounded-lg p-0.5 border"
      style={{
        backgroundColor: 'var(--color-bg-tag)',
        borderColor: 'var(--color-border-muted)',
      }}
      role="tablist"
    >
      {tabs.map((tab) => {
        const active = scope === tab.key;
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onScopeChange(tab.key)}
            className="px-2.5 py-1 rounded-md text-xs font-medium transition-colors"
            style={{
              backgroundColor: active ? 'var(--color-accent-primary)' : 'transparent',
              color: active ? '#fff' : 'var(--color-text-secondary)',
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
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
  let statusColor = 'var(--color-text-secondary)';
  let statusBg = 'var(--color-bg-tag)';

  if (rawStatus === 'reported' || rawDateStatus === 'reported') {
    statusLabel = t('dashboard.earningsCalendarCard.statusReported');
  } else if (rawStatus === 'confirmed' || rawDateStatus === 'confirmed') {
    statusLabel = t('dashboard.earningsCalendarCard.statusConfirmed');
    statusColor = 'var(--color-accent-light, #10b981)';
  } else if (rawStatus === 'pending' || rawDateStatus === 'estimated') {
    statusLabel = t('dashboard.earningsCalendarCard.statusPending');
    statusColor = '#f59e0b';
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
        <span
          className="px-1.5 py-0.5 rounded text-[0.625rem] font-semibold leading-none border"
          style={{
            backgroundColor: isCore ? 'var(--color-accent-overlay, rgba(59,130,246,0.12))' : 'var(--color-bg-tag)',
            color: isCore ? 'var(--color-accent-light, #60a5fa)' : 'var(--color-text-secondary)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          {isCore
            ? t('dashboard.earningsCalendarCard.tierCore')
            : t('dashboard.earningsCalendarCard.tierWatch')}
        </span>
      )}
      {statusLabel && (
        <span
          className="px-1.5 py-0.5 rounded text-[0.625rem] font-medium leading-none border"
          style={{
            backgroundColor: statusBg,
            color: statusColor,
            borderColor: 'var(--color-border-muted)',
          }}
        >
          {statusLabel}
        </span>
      )}
      {timingLabel && (
        <span
          className="px-1.5 py-0.5 rounded text-[0.625rem] font-medium leading-none border"
          style={{
            backgroundColor: 'var(--color-bg-tag)',
            color: 'var(--color-text-secondary)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          {timingLabel}
        </span>
      )}
    </div>
  );
}
