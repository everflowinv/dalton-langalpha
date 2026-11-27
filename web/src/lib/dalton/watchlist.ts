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

export interface WatchlistTierPolicyDiff {
  tier: WatchlistTier;
  before: WatchlistTierPolicy;
  after: WatchlistTierPolicy;
  changed_fields: string[];
}

export interface WatchlistCronPreset {
  id: string;
  cron: string;
  label_zh: string;
  label_en: string;
}

export interface WatchlistTierPoliciesResponse {
  ok: boolean;
  user_id?: string;
  policies: Record<WatchlistTier, WatchlistTierPolicy>;
  defaults?: Record<WatchlistTier, WatchlistTierPolicy>;
  presets?: {
    watch_cron: WatchlistCronPreset[];
    delta_cron: WatchlistCronPreset[];
    move_threshold_pct: number[];
    earnings_stages: string[];
  };
}

export interface WatchlistTierPolicyPreviewRequest {
  tier: WatchlistTier;
  action?: 'set_tier_policy' | 'reset_tier_policy';
  policy?: Partial<Omit<WatchlistTierPolicy, 'tier' | 'policy_version'>>;
  watch_enabled?: boolean;
  watch_cron?: string;
  delta_enabled?: boolean;
  delta_cron?: string;
  delta_timezone?: string;
  move_enabled?: boolean;
  move_threshold_pct?: number;
  earnings_stages?: string[];
  override_user_pause?: boolean;
}

export interface WatchlistTierPolicyCommitRequest {
  snapshot_id: string;
  expected_policy_version?: number;
  expected_roster_hash?: string;
  override_user_pause?: boolean;
}

export interface WatchlistTierPolicyCommitResponse {
  ok: boolean;
  action: string;
  tier: WatchlistTier;
  policy: WatchlistTierPolicy;
  before_policy?: WatchlistTierPolicy;
  policy_diff?: WatchlistTierPolicyDiff;
  affected_tickers?: string[];
  job_id?: string | null;
  reconcile_status?: string;
  failed_items?: Array<Record<string, unknown>>;
  home_synced?: boolean;
}

export type ValuationStatus =
  | 'ok'
  | 'loss'
  | 'no_coverage'
  | 'unit_unverified'
  | 'stale'
  | 'rate_limited'
  | 'unlisted';

export interface WatchlistValuationMeta {
  ticker?: string;
  fy1_eps: number | null;
  fy2_eps: number | null;
  fy1_end: string | null;
  fy2_end: string | null;
  estimate_currency: string | null;
  quote_currency: string | null;
  shares_per_quote_unit: number | null;
  unit_verified: boolean;
  analyst_count: number | null;
  provider: string | null;
  as_of: string | null;
  fx_rate?: number | null;
  status: ValuationStatus;
  pe_fy1: number | null;
  pe_fy2: number | null;
  eps_growth_fy2: number | null;
  pe_fy1_status?: ValuationStatus;
  pe_fy2_status?: ValuationStatus;
  eps_growth_fy2_status?: ValuationStatus;
  status_reason?: string | null;
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
  next_earnings: string | null;
  pe_fy1?: number | null;
  pe_fy2?: number | null;
  eps_growth_fy2?: number | null;
  valuation?: WatchlistValuationMeta | null;
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
  pending_valuation_symbols?: string[];
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
  expected_policy_version?: number | null;
  expected_policy_versions?: Record<string, number>;
  expected_pause_versions?: Record<string, number>;
  override_user_pause?: boolean;
  auto_refresh?: boolean | null;
  group_id?: string | null;
  affected_tickers: WatchlistAffectedTicker[];
  affected_workspaces: WatchlistAffectedWorkspace[];
  task_summary: WatchlistTaskSummary;
  before_policy?: WatchlistTierPolicy | null;
  after_policy?: WatchlistTierPolicy | null;
  policy_diff?: WatchlistTierPolicyDiff | null;
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

/** Built-in watchlist column keys (Batch 1 + Batch 2 public consensus valuation columns). */
export type WatchlistColumnKey =
  | 'tier'
  | 'price'
  | 'chg_1d'
  | 'chg_1w'
  | 'chg_1m'
  | 'chg_3m'
  | 'chg_ytd'
  | 'chg_1y'
  | 'pe_fy1'
  | 'pe_fy2'
  | 'eps_growth_fy2'
  | 'range_52w'
  | 'rsi_14d'
  | 'rsi_30d'
  | 'market_cap'
  | 'next_earnings';

export const ALL_WATCHLIST_COLUMNS: readonly WatchlistColumnKey[] = [
  'tier',
  'price',
  'chg_1d',
  'chg_1w',
  'chg_1m',
  'chg_3m',
  'chg_ytd',
  'chg_1y',
  'pe_fy1',
  'pe_fy2',
  'eps_growth_fy2',
  'range_52w',
  'rsi_14d',
  'rsi_30d',
  'market_cap',
  'next_earnings',
] as const;

/** Kept as alias for backwards compatibility with Batch-1 imports. */
export const BATCH1_WATCHLIST_COLUMNS: readonly WatchlistColumnKey[] = ALL_WATCHLIST_COLUMNS;

export const DEFAULT_WATCHLIST_COLUMNS: readonly WatchlistColumnKey[] = [
  'tier',
  'price',
  'chg_1d',
  'chg_1w',
  'chg_1m',
  'chg_3m',
  'pe_fy1',
  'pe_fy2',
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

export async function fetchWatchlistTierPolicies(): Promise<WatchlistTierPoliciesResponse> {
  const { data } = await api.get<WatchlistTierPoliciesResponse>('/api/v1/dalton/watchlist/tier-policies');
  return data;
}

export async function previewWatchlistTierPolicy(
  body: WatchlistTierPolicyPreviewRequest,
): Promise<WatchlistPreviewResponse> {
  const { data } = await api.post<WatchlistPreviewResponse>(
    '/api/v1/dalton/watchlist/tier-policies/preview',
    body,
  );
  return data;
}

export async function updateWatchlistTierPolicy(
  tier: WatchlistTier,
  body: WatchlistTierPolicyCommitRequest,
): Promise<WatchlistTierPolicyCommitResponse> {
  const { data } = await api.put<WatchlistTierPolicyCommitResponse>(
    `/api/v1/dalton/watchlist/tier-policies/${encodeURIComponent(tier)}`,
    body,
  );
  return data;
}

export async function resetWatchlistTierPolicy(
  tier: WatchlistTier,
  body: WatchlistTierPolicyCommitRequest,
): Promise<WatchlistTierPolicyCommitResponse> {
  const { data } = await api.post<WatchlistTierPolicyCommitResponse>(
    `/api/v1/dalton/watchlist/tier-policies/${encodeURIComponent(tier)}/reset`,
    body,
  );
  return data;
}

export function useWatchlistTierPolicies(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.watchlistTierPolicies(),
    queryFn: fetchWatchlistTierPolicies,
    enabled: enabled && isDaltonBuild,
    staleTime: 30_000,
    retry: 1,
  });
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
    refetchInterval: (query) =>
      ((query.state.data?.pending_symbols?.length ?? 0) +
        (query.state.data?.pending_valuation_symbols?.length ?? 0)) > 0
        ? 3_000
        : 60_000,
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
  const tableHeader = '| Ticker | Name | Tier | Price | 1D% | 1W% | 1M% | FY1 P/E | FY2 P/E | 52W% | 14D RSI | 30D RSI | Next Earnings |';
  const sep = '|---|---|---|---|---|---|---|---|---|---|---|---|---|';
  const fmtPct = (v: number | null | undefined) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);
  const fmtNum = (v: number | null | undefined, digits = 2) => (v == null ? '—' : v.toFixed(digits));
  const fmtPe = (v: number | null | undefined, status?: ValuationStatus) => {
    if (status === 'loss') return 'NM';
    if (v == null) return '—';
    return `${v.toFixed(1)}x`;
  };
  const lines = companies.slice(0, 120).map((c) => {
    const m = metrics[c.ticker];
    const priceStr = c.is_private || m?.is_private ? 'Unlisted' : fmtNum(m?.price);
    const posStr = m?.position_52w == null ? '—' : `${m.position_52w.toFixed(0)}%`;
    const pe1 = c.is_private || m?.is_private ? '—' : fmtPe(m?.pe_fy1, m?.valuation?.pe_fy1_status ?? m?.valuation?.status);
    const pe2 = c.is_private || m?.is_private ? '—' : fmtPe(m?.pe_fy2, m?.valuation?.pe_fy2_status ?? m?.valuation?.status);
    return `| ${c.ticker} | ${c.name || c.ticker} | ${c.tier} | ${priceStr} | ${fmtPct(m?.chg_1d)} | ${fmtPct(m?.chg_1w)} | ${fmtPct(m?.chg_1m)} | ${pe1} | ${pe2} | ${posStr} | ${fmtNum(m?.rsi_14d, 1)} | ${fmtNum(m?.rsi_30d, 1)} | ${m?.next_earnings ?? '—'} |`;
  });
  const parts = [header, ...summaryLines, tableHeader, sep, ...lines];
  return `<watchlist_context>\n${parts.join('\n')}\n</watchlist_context>`;
}
