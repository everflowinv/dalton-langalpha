import { useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { isDaltonBuild } from '@/config/brand';
import { quoteKey, roundQuote2, type QuoteRow } from '@/lib/quotes';

export type WatchlistTier = 'core' | 'watch' | 'radar';

export const WATCHLIST_TIERS: readonly WatchlistTier[] = ['core', 'watch', 'radar'] as const;

export interface PersonalWatchlistCompany {
  ticker: string;
  name: string;
  tier: WatchlistTier;
  market?: string;
  sector?: string | null;
  industry?: string | null;
  peer_group?: string | null;
  workspace_id?: string | null;
  workspace?: { workspace_id?: string | null; status?: string | null } | null;
  source?: string;
  group_id?: string | null;
  group_ids?: string[];
  stale_group_ids?: string[];
  tags?: string[];
  sort_order?: number;
  is_pinned?: boolean;
  is_private?: boolean;
  task_overrides?: Record<string, unknown> | null;
  added_at?: string | null;
}

export interface WatchlistRuleGroup {
  group_id: string;
  label: string;
  criteria: Record<string, unknown>;
  default_tier: WatchlistTier;
  auto_refresh: boolean;
  last_resolved_at?: string | null;
}

export interface WatchlistTierPolicy {
  tier: WatchlistTier;
  policy_version: number;
  watch_enabled: boolean;
  watch_cron: string;
  delta_enabled: boolean;
  delta_cron: string;
  delta_timezone: string;
  move_enabled: boolean;
  move_threshold_pct: number;
  earnings_stages: string[];
}

export interface PersonalWatchlistSummary {
  ok: boolean;
  user_id?: string;
  core: PersonalWatchlistCompany[];
  watch: PersonalWatchlistCompany[];
  radar: PersonalWatchlistCompany[];
  groups: WatchlistRuleGroup[];
  stale_tickers: string[];
  counts: {
    core: number;
    watch: number;
    radar: number;
    total: number;
    groups: number;
  };
  tier_policies?: Record<WatchlistTier, WatchlistTierPolicy>;
  pending_reconcile_count?: number;
}

export interface WatchlistMetricRow {
  ticker: string;
  is_private: boolean;
  status: 'ok' | 'warming' | 'unlisted' | 'unavailable';
  price: number | null;
  currency: string | null;
  chg_1d: number | null;
  chg_1w: number | null;
  chg_1m: number | null;
  chg_3m: number | null;
  chg_ytd: number | null;
  chg_1y: number | null;
  high_52w: number | null;
  low_52w: number | null;
  position_52w: number | null;
  rsi_14d: number | null;
  rsi_30d: number | null;
  vol_ratio_5_20: number | null;
  market_cap: number | null;
  market_cap_currency?: string | null;
  next_earnings: string | null;
}

export interface WatchlistMetricsSummary {
  total_count: number;
  listed_count: number;
  private_count: number;
  tier_counts: Record<WatchlistTier, number>;
  lowest_rsi_14d: Array<{ ticker: string; rsi_14d: number }>;
  highest_rsi_14d: Array<{ ticker: string; rsi_14d: number }>;
  top_gainers_1d: Array<{ ticker: string; chg_1d: number }>;
  top_losers_1d: Array<{ ticker: string; chg_1d: number }>;
  upcoming_earnings: Array<{ ticker: string; date: string }>;
}

export interface WatchlistMetricsResponse {
  ok: boolean;
  items: Record<string, WatchlistMetricRow>;
  pending_symbols: string[];
  summary?: WatchlistMetricsSummary;
  as_of: string;
}

export interface WatchlistAffectedTicker {
  ticker: string;
  name?: string;
  old_tier?: WatchlistTier | null;
  new_tier?: WatchlistTier | null;
  has_workspace?: boolean;
  workspace_id?: string | null;
  is_private?: boolean;
}

export interface WatchlistWorkspaceTaskChange {
  name: string;
  change: string;
  old_cron?: string | null;
  new_cron?: string | null;
  reason?: string;
}

export interface WatchlistAffectedWorkspace {
  ticker: string;
  workspace_id: string;
  old_tier?: WatchlistTier | null;
  new_tier?: WatchlistTier | null;
  task_changes: WatchlistWorkspaceTaskChange[];
}

export interface WatchlistTaskSummary {
  create_count?: number;
  update_count?: number;
  resume_count?: number;
  pause_count?: number;
  preserved_user_pause_count?: number;
  preserved_tracker_count?: number;
  preserved_handoff_count?: number;
}

export interface WatchlistPreviewResponse {
  ok: boolean;
  action: string | null;
  raw_action: string;
  snapshot_id?: string | null;
  roster_revision?: number;
  requires_confirmation: boolean;
  owner_user_id?: string;
  target_tickers: string[];
  target_tier?: WatchlistTier | null;
  not_found?: string[];
  expected_roster_hash?: string;
  expected_policy_versions?: Record<string, number>;
  expected_pause_versions?: Record<string, number>;
  override_user_pause?: boolean;
  auto_refresh?: boolean | null;
  group_id?: string | null;
  affected_tickers: WatchlistAffectedTicker[];
  affected_workspaces: WatchlistAffectedWorkspace[];
  task_summary: WatchlistTaskSummary;
  card_title?: string;
  card_description?: string;
  preserve_workspace_notice?: string | null;
  precheck_error?: string;
}

export interface WatchlistMutateRequestBody {
  action: 'add' | 'remove' | 'set_tier' | 'add_group' | 'remove_group' | 'update_tags';
  snapshot_id?: string | null;
  tickers?: string[] | string;
  tier?: WatchlistTier;
  label?: string;
  criteria?: Record<string, unknown>;
  default_tier?: WatchlistTier;
  auto_refresh?: boolean;
  group_id?: string;
  tags?: string[];
  override_user_pause?: boolean;
}

export interface WatchlistMutateResponse {
  ok: boolean;
  added?: PersonalWatchlistCompany[];
  added_tickers?: string[];
  updated?: string[];
  removed?: string[];
  not_found?: string[];
  job_id?: string | null;
  reconcile_status?: string;
  failed_items?: Array<Record<string, unknown>>;
  home_synced?: boolean;
  notice?: string | null;
  workspace_notice?: string | null;
}

/** Batch-1 base column keys (no Batch-2 valuation / Batch-3 custom columns). */
export type WatchlistColumnKey =
  | 'tier'
  | 'price'
  | 'chg_1d'
  | 'chg_1w'
  | 'chg_1m'
  | 'chg_3m'
  | 'chg_ytd'
  | 'chg_1y'
  | 'range_52w'
  | 'rsi_14d'
  | 'rsi_30d'
  | 'market_cap'
  | 'next_earnings';

export const BATCH1_WATCHLIST_COLUMNS: readonly WatchlistColumnKey[] = [
  'tier',
  'price',
  'chg_1d',
  'chg_1w',
  'chg_1m',
  'chg_3m',
  'chg_ytd',
  'chg_1y',
  'range_52w',
  'rsi_14d',
  'rsi_30d',
  'market_cap',
  'next_earnings',
] as const;

export const DEFAULT_WATCHLIST_COLUMNS: readonly WatchlistColumnKey[] = [
  'tier',
  'price',
  'chg_1d',
  'chg_1w',
  'chg_1m',
  'chg_3m',
  'range_52w',
  'rsi_14d',
  'rsi_30d',
  'market_cap',
  'next_earnings',
] as const;

export async function fetchPersonalWatchlist(): Promise<PersonalWatchlistSummary> {
  const { data } = await api.get<PersonalWatchlistSummary>('/api/v1/dalton/watchlist');
  return data;
}

export async function fetchPersonalWatchlistMetrics(
  symbols?: readonly string[],
): Promise<WatchlistMetricsResponse> {
  const params = symbols && symbols.length > 0 ? { symbols: symbols.join(',') } : undefined;
  const { data } = await api.get<WatchlistMetricsResponse>('/api/v1/dalton/watchlist/metrics', {
    params,
  });
  return data;
}

export async function previewWatchlistMutation(
  body: Omit<WatchlistMutateRequestBody, 'snapshot_id'>,
): Promise<WatchlistPreviewResponse> {
  const { data } = await api.post<WatchlistPreviewResponse>('/api/v1/dalton/watchlist/preview', body);
  return data;
}

export async function mutateWatchlist(
  body: WatchlistMutateRequestBody,
): Promise<WatchlistMutateResponse> {
  const { data } = await api.post<WatchlistMutateResponse>('/api/v1/dalton/watchlist/mutate', body);
  return data;
}

export async function reconcileWatchlist(jobId?: string): Promise<Record<string, unknown>> {
  const { data } = await api.post<Record<string, unknown>>('/api/v1/dalton/watchlist/reconcile', {
    ...(jobId ? { job_id: jobId } : {}),
    sync_home: true,
  });
  return data;
}

export function flattenWatchlistCompanies(
  summary: PersonalWatchlistSummary | null | undefined,
): PersonalWatchlistCompany[] {
  if (!summary) return [];
  const rawAll: PersonalWatchlistCompany[] = [
    ...(summary.core ?? []),
    ...(summary.watch ?? []),
    ...(summary.radar ?? []),
  ];
  // Normalize nested `workspace.workspace_id` into top-level `workspace_id`
  const all = rawAll.map((c) => ({
    ...c,
    workspace_id: c.workspace_id ?? c.workspace?.workspace_id ?? null,
  }));
  const tierRank: Record<WatchlistTier, number> = { core: 0, watch: 1, radar: 2 };
  return all.sort((a, b) => {
    const pinDiff = Number(Boolean(b.is_pinned)) - Number(Boolean(a.is_pinned));
    if (pinDiff !== 0) return pinDiff;
    const tDiff = (tierRank[a.tier] ?? 9) - (tierRank[b.tier] ?? 9);
    if (tDiff !== 0) return tDiff;
    const orderDiff = (a.sort_order ?? 0) - (b.sort_order ?? 0);
    if (orderDiff !== 0) return orderDiff;
    return a.ticker.localeCompare(b.ticker);
  });
}

export function usePersonalWatchlist(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.watchlist(),
    queryFn: fetchPersonalWatchlist,
    enabled: enabled && isDaltonBuild,
    staleTime: 30_000,
    retry: 1,
  });
}

const PENDING_POLL_INTERVAL_MS = 3_000;

function recomputeSummaryFromItems(
  prevSummary: WatchlistMetricsSummary | undefined,
  items: Record<string, WatchlistMetricRow>,
): WatchlistMetricsSummary | undefined {
  if (!prevSummary) return undefined;
  const rsiEntries: Array<{ ticker: string; rsi_14d: number }> = [];
  const chg1dEntries: Array<{ ticker: string; chg_1d: number }> = [];
  const upcomingEarnings: Array<{ ticker: string; date: string }> = [];

  for (const [sym, item] of Object.entries(items)) {
    if (item.is_private) continue;
    if (typeof item.rsi_14d === 'number' && Number.isFinite(item.rsi_14d)) {
      rsiEntries.push({ ticker: sym, rsi_14d: item.rsi_14d });
    }
    if (typeof item.chg_1d === 'number' && Number.isFinite(item.chg_1d)) {
      chg1dEntries.push({ ticker: sym, chg_1d: item.chg_1d });
    }
    if (typeof item.next_earnings === 'string' && item.next_earnings.length >= 10) {
      upcomingEarnings.push({ ticker: sym, date: item.next_earnings.slice(0, 10) });
    }
  }

  rsiEntries.sort((a, b) => a.rsi_14d - b.rsi_14d);
  chg1dEntries.sort((a, b) => a.chg_1d - b.chg_1d);
  upcomingEarnings.sort((a, b) => (a.date === b.date ? a.ticker.localeCompare(b.ticker) : a.date.localeCompare(b.date)));

  return {
    ...prevSummary,
    lowest_rsi_14d: rsiEntries.slice(0, 5),
    highest_rsi_14d: rsiEntries.slice(-5).reverse(),
    top_gainers_1d: chg1dEntries.slice(-5).reverse(),
    top_losers_1d: chg1dEntries.slice(0, 5),
    upcoming_earnings: upcomingEarnings.slice(0, 10),
  };
}

/** Merge a pending-only subset poll response into the full cached metrics response. */
export function mergeWatchlistMetricsResponse(
  prev: WatchlistMetricsResponse,
  subset: WatchlistMetricsResponse,
  polledSymbols: readonly string[],
): WatchlistMetricsResponse {
  const mergedItems: Record<string, WatchlistMetricRow> = {
    ...prev.items,
    ...subset.items,
  };
  const polledSet = new Set(polledSymbols.map((s) => s.toUpperCase()));
  const nextPending: string[] = [];
  const seen = new Set<string>();
  for (const sym of prev.pending_symbols ?? []) {
    const up = sym.toUpperCase();
    if (!polledSet.has(up) && !seen.has(up)) {
      seen.add(up);
      nextPending.push(sym);
    }
  }
  for (const sym of subset.pending_symbols ?? []) {
    const up = sym.toUpperCase();
    if (!seen.has(up)) {
      seen.add(up);
      nextPending.push(sym);
    }
  }
  return {
    ...prev,
    ok: prev.ok && subset.ok,
    items: mergedItems,
    pending_symbols: nextPending,
    summary: recomputeSummaryFromItems(prev.summary ?? subset.summary, mergedItems),
    as_of: subset.as_of || prev.as_of,
  };
}

/**
 * Merge live per-symbol quotes from the shared `useQuotes` cache with `/metrics` rows.
 *
 * - Real-time quote columns (`price`, `chg_1d`, `currency`) hydrate immediately from
 *   `useQuotes` (already warm when navigating from the Dashboard watchlist widget) before
 *   `/api/v1/dalton/watchlist/metrics` finishes computing daily-bar indicators & market cap.
 * - Once `/metrics` has a timestamped price for a symbol (`metric.price != null`),
 *   `/metrics` stays authoritative for `price` and `chg_1d` so every column on the row
 *   reflects the same `/metrics` snapshot timestamp without flickering between polls.
 * - Private/unlisted companies never inherit quote prices.
 */
export function mergeQuotesIntoWatchlistMetrics(
  companies: readonly PersonalWatchlistCompany[],
  metrics: Record<string, WatchlistMetricRow>,
  quotes: Record<string, QuoteRow | undefined>,
): Record<string, WatchlistMetricRow> {
  const out: Record<string, WatchlistMetricRow> = { ...metrics };
  for (const company of companies) {
    const ticker = company.ticker;
    const metric = metrics[ticker];
    if (company.is_private || metric?.is_private) continue;

    const quote = quotes[quoteKey(ticker)];
    if (!quote) continue;

    const rawPrice =
      typeof quote.price === 'number' && Number.isFinite(quote.price) ? quote.price : null;
    const prevClose =
      typeof quote.previous_close === 'number' &&
      Number.isFinite(quote.previous_close) &&
      quote.previous_close > 0
        ? quote.previous_close
        : null;
    const unprinted = rawPrice == null && prevClose != null;
    const quotePrice = unprinted ? prevClose : rawPrice;

    let quoteChg1d: number | null = null;
    if (unprinted) {
      quoteChg1d = 0;
    } else if (typeof quote.change_percent === 'number' && Number.isFinite(quote.change_percent)) {
      quoteChg1d = quote.change_percent;
    } else if (quotePrice != null && prevClose != null) {
      quoteChg1d = roundQuote2(((quotePrice - prevClose) / prevClose) * 100);
    }
    const quoteCurrencyCode = quote.currency ?? null;

    if (!metric) {
      if (quotePrice == null && quoteChg1d == null && quoteCurrencyCode == null) continue;
      out[ticker] = {
        ticker,
        is_private: false,
        status: quotePrice != null ? 'ok' : 'warming',
        price: quotePrice,
        currency: quoteCurrencyCode,
        chg_1d: quoteChg1d,
        chg_1w: null,
        chg_1m: null,
        chg_3m: null,
        chg_ytd: null,
        chg_1y: null,
        high_52w: null,
        low_52w: null,
        position_52w: null,
        rsi_14d: null,
        rsi_30d: null,
        vol_ratio_5_20: null,
        market_cap: null,
        market_cap_currency: quoteCurrencyCode,
        next_earnings: null,
      };
      continue;
    }

    // Both `/metrics` and `useQuotes` exist: prefer `/metrics` timestamped quote values.
    if (metric.price != null) {
      if (metric.currency == null && quoteCurrencyCode != null) {
        out[ticker] = {
          ...metric,
          currency: quoteCurrencyCode,
          market_cap_currency: metric.market_cap_currency ?? quoteCurrencyCode,
        };
      }
      continue;
    }

    if (quotePrice != null || quoteChg1d != null || quoteCurrencyCode != null) {
      out[ticker] = {
        ...metric,
        status: quotePrice != null ? 'ok' : metric.status,
        price: quotePrice ?? metric.price,
        currency: metric.currency ?? quoteCurrencyCode,
        chg_1d: metric.chg_1d ?? quoteChg1d,
        market_cap_currency: metric.market_cap_currency ?? quoteCurrencyCode,
      };
    }
  }
  return out;
}

export function usePersonalWatchlistMetrics(
  symbols: readonly string[],
  enabled = true,
) {
  const queryClient = useQueryClient();
  const normalizedKey = symbols.length > 0 ? symbols.slice().sort().join(',') : 'all';
  const isEnabled = enabled && isDaltonBuild && symbols.length > 0;

  const query = useQuery({
    queryKey: queryKeys.dalton.watchlistMetrics(normalizedKey),
    queryFn: () => fetchPersonalWatchlistMetrics(symbols.length > 0 ? symbols : undefined),
    enabled: isEnabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: 1,
  });

  const pendingKey = (query.data?.pending_symbols ?? []).join(',');

  useEffect(() => {
    if (!isEnabled || !pendingKey) return;
    let cancelled = false;
    let timerId: ReturnType<typeof setTimeout> | null = null;
    let currentPending = pendingKey.split(',').filter(Boolean);
    const key = queryKeys.dalton.watchlistMetrics(normalizedKey);

    const scheduleNext = () => {
      if (cancelled || currentPending.length === 0) return;
      timerId = setTimeout(async () => {
        if (cancelled) return;
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
          scheduleNext();
          return;
        }
        const batch = currentPending;
        try {
          const subset = await fetchPersonalWatchlistMetrics(batch);
          if (cancelled) return;
          queryClient.setQueryData<WatchlistMetricsResponse>(key, (prev) =>
            prev ? mergeWatchlistMetricsResponse(prev, subset, batch) : subset,
          );
          currentPending = subset.pending_symbols ?? [];
          if (currentPending.length > 0 && currentPending.join(',') === pendingKey) {
            scheduleNext();
          }
        } catch {
          if (!cancelled) {
            scheduleNext();
          }
        }
      }, PENDING_POLL_INTERVAL_MS);
    };

    scheduleNext();
    return () => {
      cancelled = true;
      if (timerId !== null) clearTimeout(timerId);
    };
  }, [isEnabled, normalizedKey, pendingKey, queryClient]);

  return query;
}

export function useMutatePersonalWatchlist() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: mutateWatchlist,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
    },
  });
}

/** Build a compact personal watchlist context block (with full-roster summary) for private MarketView chat sessions only. */
export function buildPersonalWatchlistContextText(
  companies: readonly PersonalWatchlistCompany[],
  metrics: Record<string, WatchlistMetricRow>,
  activeTierFilter: WatchlistTier | 'all',
  summary?: WatchlistMetricsSummary | null,
): string {
  if (!companies.length) return '';
  const shownCount = Math.min(companies.length, 120);
  const header = `Personal watchlist snapshot (filter=${activeTierFilter}, total=${companies.length}, shown=${shownCount}):`;
  const summaryLines: string[] = [];
  if (summary) {
    const tc = summary.tier_counts;
    summaryLines.push(
      `Full-roster summary: total=${summary.total_count} (core=${tc.core}, watch=${tc.watch}, radar=${tc.radar}; listed=${summary.listed_count}, unlisted=${summary.private_count})`,
    );
    if (summary.lowest_rsi_14d.length > 0) {
      summaryLines.push(
        `Lowest 14D RSI across full roster: ${summary.lowest_rsi_14d.map((x) => `${x.ticker}(${x.rsi_14d.toFixed(1)})`).join(', ')}`,
      );
    }
    if (summary.highest_rsi_14d.length > 0) {
      summaryLines.push(
        `Highest 14D RSI across full roster: ${summary.highest_rsi_14d.map((x) => `${x.ticker}(${x.rsi_14d.toFixed(1)})`).join(', ')}`,
      );
    }
    if (summary.upcoming_earnings.length > 0) {
      summaryLines.push(
        `Upcoming earnings across full roster: ${summary.upcoming_earnings.map((x) => `${x.ticker}(${x.date})`).join(', ')}`,
      );
    }
  }
  const tableHeader = '| Ticker | Name | Tier | Price | 1D% | 1W% | 1M% | 52W% | 14D RSI | 30D RSI | Next Earnings |';
  const sep = '|---|---|---|---|---|---|---|---|---|---|---|';
  const fmtPct = (v: number | null | undefined) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);
  const fmtNum = (v: number | null | undefined, digits = 2) => (v == null ? '—' : v.toFixed(digits));
  const lines = companies.slice(0, 120).map((c) => {
    const m = metrics[c.ticker];
    const priceStr = c.is_private || m?.is_private ? 'Unlisted' : fmtNum(m?.price);
    const posStr = m?.position_52w == null ? '—' : `${m.position_52w.toFixed(0)}%`;
    return `| ${c.ticker} | ${c.name || c.ticker} | ${c.tier} | ${priceStr} | ${fmtPct(m?.chg_1d)} | ${fmtPct(m?.chg_1w)} | ${fmtPct(m?.chg_1m)} | ${posStr} | ${fmtNum(m?.rsi_14d, 1)} | ${fmtNum(m?.rsi_30d, 1)} | ${m?.next_earnings ?? '—'} |`;
  });
  const parts = [header, ...summaryLines, tableHeader, sep, ...lines];
  return `<watchlist_context>\n${parts.join('\n')}\n</watchlist_context>`;
}
