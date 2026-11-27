/**
 * Dalton MarketView personal watchlist route & toolbar helpers.
 * Keeps upstream `MarketView.tsx` to thin 1-5 line hooks.
 */

import React from 'react';
import type { NavigateFunction, SetURLSearchParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { isDaltonBuild } from '@/config/brand';
import { buildMarketViewUrl, readMarketViewRoute, type MarketViewRouteParams } from '../../utils/marketRoute';
import { WatchlistWorkspace, type WatchlistWorkspaceProps } from './WatchlistWorkspace';

interface DaltonMarketLocationState {
  fromWatchlist?: boolean;
  daltonRouteWs?: string | null;
  daltonRouteReturnTo?: string | null;
}

export function shouldShowDaltonWatchlist(searchParams: URLSearchParams): boolean {
  return isDaltonBuild && !readMarketViewRoute(searchParams).symbol;
}

export function shouldConsumeMarketRouteParam(key: string): boolean {
  if (key === 'thread') return false;
  if (isDaltonBuild && (key === 'symbol' || key === 'tf')) return false;
  return true;
}

export function readDaltonOpeningMarketRoute(
  searchParams: URLSearchParams,
  locationState: unknown,
): MarketViewRouteParams {
  const base = readMarketViewRoute(searchParams);
  if (!isDaltonBuild || !locationState || typeof locationState !== 'object') return base;
  const st = locationState as DaltonMarketLocationState;
  return {
    ...base,
    workspaceId: base.workspaceId ?? (typeof st.daltonRouteWs === 'string' ? st.daltonRouteWs : null),
    returnTo: base.returnTo ?? (typeof st.daltonRouteReturnTo === 'string' ? st.daltonRouteReturnTo : null),
  };
}

export function buildConsumedRouteReplaceState(
  locationState: unknown,
  route: MarketViewRouteParams,
): Record<string, unknown> | undefined {
  if (!isDaltonBuild) return undefined;
  const prev =
    locationState && typeof locationState === 'object'
      ? { ...(locationState as Record<string, unknown>) }
      : {};
  if (route.workspaceId) prev.daltonRouteWs = route.workspaceId;
  if (route.returnTo) prev.daltonRouteReturnTo = route.returnTo;
  return Object.keys(prev).length > 0 ? prev : undefined;
}

export function syncDaltonMarketSymbolParam(
  setSearchParams: SetURLSearchParams,
  symbol: string,
  locationState?: unknown,
): void {
  if (!isDaltonBuild || !symbol) return;
  setSearchParams(
    (prev) => {
      const next = new URLSearchParams(prev);
      next.set('symbol', symbol);
      return next;
    },
    { replace: true, ...(locationState !== undefined ? { state: locationState } : {}) },
  );
}

export function syncDaltonMarketTimeframeParam(
  setSearchParams: SetURLSearchParams,
  timeframe: string,
  locationState?: unknown,
): void {
  if (!isDaltonBuild || !timeframe) return;
  setSearchParams(
    (prev) => {
      if (!prev.has('symbol')) return prev;
      const next = new URLSearchParams(prev);
      if (next.get('tf') === timeframe) return prev;
      next.set('tf', timeframe);
      return next;
    },
    { replace: true, ...(locationState !== undefined ? { state: locationState } : {}) },
  );
}

export function navigateDaltonChartJump(
  navigate: NavigateFunction,
  symbol: string,
  timeframe: string | undefined,
  fromWatchlistView: boolean,
  locationState?: unknown,
): void {
  if (!isDaltonBuild) return;
  const nextState = fromWatchlistView
    ? {
        ...(locationState && typeof locationState === 'object'
          ? (locationState as Record<string, unknown>)
          : {}),
        fromWatchlist: true,
      }
    : locationState;
  navigate(
    buildMarketViewUrl({
      symbol,
      ...(timeframe ? { timeframe } : {}),
    }),
    {
      replace: !fromWatchlistView,
      ...(nextState !== undefined ? { state: nextState } : {}),
    },
  );
}

export function renderDaltonWatchlistWorkspace(
  props: WatchlistWorkspaceProps,
): React.ReactElement | null {
  if (!isDaltonBuild) return null;
  return <WatchlistWorkspace {...props} />;
}

export function navigateBackToDaltonWatchlist(
  navigate: NavigateFunction,
  locationState: unknown,
): void {
  const fromWatchlist = Boolean(
    locationState &&
      typeof locationState === 'object' &&
      (locationState as DaltonMarketLocationState).fromWatchlist,
  );
  if (fromWatchlist && typeof window !== 'undefined' && window.history.length > 1) {
    navigate(-1);
    return;
  }
  navigate('/market');
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
