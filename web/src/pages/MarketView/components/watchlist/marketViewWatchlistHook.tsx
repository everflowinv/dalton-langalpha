/**
 * Dalton MarketView personal watchlist route & toolbar helpers.
 * Keeps upstream `MarketView.tsx` to thin 1-5 line hooks.
 */

import React from 'react';
import type { NavigateFunction, SetURLSearchParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { isDaltonBuild } from '@/config/brand';
import { buildMarketViewUrl, readMarketViewRoute } from '../../utils/marketRoute';

export function shouldShowDaltonWatchlist(searchParams: URLSearchParams): boolean {
  return isDaltonBuild && !readMarketViewRoute(searchParams).symbol;
}

export function shouldConsumeMarketRouteParam(key: string): boolean {
  if (key === 'thread') return false;
  if (isDaltonBuild && (key === 'symbol' || key === 'tf')) return false;
  return true;
}

export function syncDaltonMarketSymbolParam(
  setSearchParams: SetURLSearchParams,
  symbol: string,
): void {
  if (!isDaltonBuild || !symbol) return;
  setSearchParams(
    (prev) => {
      const next = new URLSearchParams(prev);
      next.set('symbol', symbol);
      return next;
    },
    { replace: true },
  );
}

export function navigateDaltonChartJump(
  navigate: NavigateFunction,
  symbol: string,
  timeframe: string | undefined,
  fromWatchlistView: boolean,
): void {
  if (!isDaltonBuild) return;
  navigate(
    buildMarketViewUrl({
      symbol,
      ...(timeframe ? { timeframe } : {}),
    }),
    { replace: !fromWatchlistView },
  );
}

export function BackToWatchlistButton({
  onBack,
  label,
}: {
  onBack: () => void;
  label: string;
}): React.ReactElement | null {
  if (!isDaltonBuild) return null;
  return (
    <button
      type="button"
      data-testid="back-to-watchlist-btn"
      onClick={onBack}
      className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium hover:bg-(--color-bg-hover) mr-1"
      style={{ color: 'var(--color-text-secondary)' }}
    >
      <ArrowLeft className="h-3.5 w-3.5" />
      <span>{label}</span>
    </button>
  );
}
