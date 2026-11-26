import { useMemo } from 'react';
import { useQuotes, snapshotToStockPrice } from '@/lib/quotes';
import { quoteCurrency } from '@/lib/bars/exchanges';
import { quotePlaces } from '@/lib/bars/currencyDisplay';
import {
  deriveQuoteDisplay,
  roundQuote2,
  type MarketStatusInput,
} from '@/lib/dalton/quotes/deriveQuoteDisplay';
import {
  flattenWatchlistCompanies,
  usePersonalWatchlist,
  type PersonalWatchlistCompany,
  type WatchlistTier,
} from '@/lib/dalton/watchlist';
import { isDaltonBuild } from '@/config/brand';
import type { QuoteRow } from '../widgets/framework/snapshotSerializers';
import type { ExtendedSessionFields, StockPrice } from '@/types/market';

/** Maximum number of listed personal watchlist symbols subscribed to live Dashboard quotes (§7.2). */
export const MAX_DASHBOARD_COVERAGE_QUOTES = 50;

export interface CoverageWatchlistRow {
  symbol: string;
  name: string;
  tier: WatchlistTier;
  is_private: boolean;
  is_pinned: boolean;
  workspace_id: string | null;
  price: number;
  change: number;
  changePercent: number;
  isPositive: boolean;
  quoteAvailable: boolean;
  previousClose: number | null;
  regularClose?: number | null;
  regularTradingChange?: number | null;
  regularTradingChangePercent?: number | null;
  earlyTradingChange?: number | null;
  earlyTradingChangePercent: number | null;
  lateTradingChange?: number | null;
  lateTradingChangePercent: number | null;
  lastMinuteClose?: number | null;
  lastTradePrice?: number | null;
  liveTickAt?: number | null;
  currency: string | null;
  assetClass: string | null;
  places?: number;
  session?: ExtendedSessionFields;
}

export function coverageQuoteRow(
  r: CoverageWatchlistRow,
  marketStatus?: MarketStatusInput | null,
): QuoteRow {
  const currency = quoteCurrency(r.currency, r.symbol, r.assetClass);
  if (r.is_private || !r.quoteAvailable) {
    return { symbol: r.symbol, currency };
  }
  const d = deriveQuoteDisplay(r, marketStatus);
  const mainPrice = d.mainPrice != null ? roundQuote2(d.mainPrice) : undefined;
  const mainChange = d.mainChange != null ? roundQuote2(d.mainChange) : undefined;
  const mainChangePct = d.mainChangePct != null ? roundQuote2(d.mainChangePct) : undefined;
  if (d.session === 'regular') {
    return {
      symbol: r.symbol,
      price: mainPrice,
      change: mainChange,
      changePercent: mainChangePct,
      currency,
    };
  }
  const extPrice = d.extPrice != null ? roundQuote2(d.extPrice) : undefined;
  const extChange = d.extChange != null ? roundQuote2(d.extChange) : undefined;
  const extChangePct = d.extChangePct != null ? roundQuote2(d.extChangePct) : undefined;
  if (d.session === 'post') {
    return {
      symbol: r.symbol,
      session: 'post',
      price: mainPrice,
      change: mainChange,
      changePercent: mainChangePct,
      postMarket: extPrice,
      postMarketChange: extChange,
      postMarketChangePercent: extChangePct,
      currency,
    };
  }
  return {
    symbol: r.symbol,
    session: 'pre',
    price: mainPrice,
    change: mainChange,
    changePercent: mainChangePct,
    preMarket: extPrice,
    preMarketChange: extChange,
    preMarketChangePercent: extChangePct,
    currency,
  };
}

export function buildCoverageRowsForCompanies(
  companies: readonly PersonalWatchlistCompany[],
  quotes: Record<string, Parameters<typeof snapshotToStockPrice>[1]>,
): CoverageWatchlistRow[] {
  if (!companies.length) return [];
  const listedQuoted: string[] = [];
  for (const c of companies) {
    if (c.is_private) continue;
    if (listedQuoted.length >= MAX_DASHBOARD_COVERAGE_QUOTES) break;
    listedQuoted.push(c.ticker.toUpperCase());
  }
  const bySym: Record<string, StockPrice> = Object.fromEntries(
    listedQuoted.map((s) => [s, snapshotToStockPrice(s, quotes[s])]),
  );
  return companies.slice(0, MAX_DASHBOARD_COVERAGE_QUOTES).map((c) => {
    const sym = c.ticker.toUpperCase();
    const isPrivate = Boolean(c.is_private);
    const p = isPrivate ? ({} as Partial<StockPrice>) : (bySym[sym] || ({} as Partial<StockPrice>));
    const quoteAvailable = !isPrivate && p.quoteAvailable !== false && p.price != null;
    const currency = p.currency ?? null;
    const assetClass = quotes[sym]?.asset_class ?? null;
    return {
      symbol: sym,
      name: c.name || sym,
      tier: c.tier,
      is_private: isPrivate,
      is_pinned: Boolean(c.is_pinned),
      workspace_id: c.workspace_id ?? null,
      price: quoteAvailable ? p.price ?? 0 : 0,
      change: quoteAvailable ? p.change ?? 0 : 0,
      changePercent: quoteAvailable ? p.changePercent ?? 0 : 0,
      isPositive: quoteAvailable ? (p.change ?? 0) >= 0 : true,
      quoteAvailable,
      previousClose: p.previousClose ?? null,
      regularClose: p.regularClose ?? null,
      regularTradingChange: p.regularTradingChange ?? null,
      regularTradingChangePercent: p.regularTradingChangePercent ?? null,
      earlyTradingChange: p.earlyTradingChange ?? null,
      earlyTradingChangePercent: p.earlyTradingChangePercent ?? null,
      lateTradingChange: p.lateTradingChange ?? null,
      lateTradingChangePercent: p.lateTradingChangePercent ?? null,
      lastMinuteClose: p.lastMinuteClose ?? null,
      lastTradePrice: p.lastTradePrice ?? null,
      liveTickAt: p.liveTickAt ?? null,
      currency,
      assetClass,
      places: p.places ?? quotePlaces(sym, { currency, assetClass }),
      session: p.session,
    };
  });
}

export function useCoverageWatchlistData() {
  const {
    data: summary,
    isLoading: listLoading,
    isSuccess: loaded,
    refetch,
  } = usePersonalWatchlist(isDaltonBuild);

  const companies = useMemo<PersonalWatchlistCompany[]>(
    () => flattenWatchlistCompanies(summary),
    [summary],
  );

  // Cap live quote subscriptions at MAX_DASHBOARD_COVERAGE_QUOTES (50) visible listed companies (§7.2).
  const quotedSymbols = useMemo(() => {
    const out: string[] = [];
    for (const c of companies) {
      if (c.is_private) continue;
      if (out.length >= MAX_DASHBOARD_COVERAGE_QUOTES) break;
      out.push(c.ticker);
    }
    return out;
  }, [companies]);

  const { quotes, isLoading: quotesLoading } = useQuotes(quotedSymbols, {
    staleTime: 1000 * 30,
    refetchInterval: 60000,
    enabled: isDaltonBuild && quotedSymbols.length > 0,
  });

  const rows = useMemo<CoverageWatchlistRow[]>(
    () => buildCoverageRowsForCompanies(companies, quotes),
    [companies, quotes],
  );

  const counts = summary?.counts ?? {
    core: companies.filter((c) => c.tier === 'core').length,
    watch: companies.filter((c) => c.tier === 'watch').length,
    radar: companies.filter((c) => c.tier === 'radar').length,
    total: companies.length,
    groups: 0,
  };

  return {
    rows,
    allCompanies: companies,
    quotesBySymbol: quotes,
    totalCount: counts.total,
    quotedLimit: MAX_DASHBOARD_COVERAGE_QUOTES,
    counts,
    loading: listLoading || (quotedSymbols.length > 0 && quotesLoading && rows.length === 0),
    loaded,
    refetch,
  };
}
