import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { isDaltonBuild } from '@/config/brand';

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
  status: 'ok' | 'unlisted' | 'unavailable';
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
  next_earnings: string | null;
}

export interface WatchlistMetricsResponse {
  ok: boolean;
  items: Record<string, WatchlistMetricRow>;
  pending_symbols: string[];
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
  requires_confirmation: boolean;
  owner_user_id?: string;
  target_tickers: string[];
  target_tier?: WatchlistTier | null;
  not_found?: string[];
  expected_roster_hash?: string;
  expected_policy_versions?: Record<string, number>;
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
  tickers?: string[] | string;
  tier?: WatchlistTier;
  label?: string;
  criteria?: Record<string, unknown>;
  default_tier?: WatchlistTier;
  group_id?: string;
  tags?: string[];
  override_user_pause?: boolean;
  expected_roster_hash?: string;
  expected_policy_versions?: Record<string, number>;
  expected_target_tickers?: string[];
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
  body: Omit<WatchlistMutateRequestBody, 'expected_roster_hash' | 'expected_policy_versions' | 'expected_target_tickers'>,
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
  const all: PersonalWatchlistCompany[] = [
    ...(summary.core ?? []),
    ...(summary.watch ?? []),
    ...(summary.radar ?? []),
  ];
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

export function usePersonalWatchlistMetrics(
  symbols: readonly string[],
  enabled = true,
) {
  const normalizedKey = symbols.length > 0 ? symbols.slice().sort().join(',') : 'all';
  return useQuery({
    queryKey: queryKeys.dalton.watchlistMetrics(normalizedKey),
    queryFn: () => fetchPersonalWatchlistMetrics(symbols.length > 0 ? symbols : undefined),
    enabled: enabled && isDaltonBuild && symbols.length > 0,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: 1,
  });
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

/** Build a compact personal watchlist context block for private MarketView chat sessions only. */
export function buildPersonalWatchlistContextText(
  companies: readonly PersonalWatchlistCompany[],
  metrics: Record<string, WatchlistMetricRow>,
  activeTierFilter: WatchlistTier | 'all',
): string {
  if (!companies.length) return '';
  const header = `Personal watchlist snapshot (filter=${activeTierFilter}, count=${companies.length}):`;
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
  return `<watchlist_context>\n${header}\n${tableHeader}\n${sep}\n${lines.join('\n')}\n</watchlist_context>`;
}
