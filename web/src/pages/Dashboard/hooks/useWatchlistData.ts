import { useCallback, useMemo, useState } from 'react';
import { queryOptions, useQuery, useQueryClient } from '@tanstack/react-query';
import { useToast } from '@/components/ui/use-toast';
import {
  addWatchlistItem,
  deleteWatchlistItem,
  listWatchlists,
  listWatchlistItems,
} from '../utils/api';
import { useQuotes, snapshotToStockPrice } from '@/lib/quotes';
import { quoteCurrency } from '@/lib/bars/exchanges';
import {
  deriveQuoteDisplay,
  roundQuote2,
  type MarketStatusInput,
} from '@/lib/dalton/quotes/deriveQuoteDisplay'; // DALTON-HOOK(extended-hours-quotes)
import type { QuoteRow } from '../widgets/framework/snapshotSerializers';
import { registerAuthReset } from '@/lib/authResets';
import type { StockPrice } from '@/types/market';

type MarketStatusData = MarketStatusInput;

export interface WatchlistRow {
  watchlist_item_id: string;
  symbol: string;
  price: number;
  change: number;
  changePercent: number;
  isPositive: boolean;
  quoteAvailable?: boolean;
  previousClose: number | null;
  // DALTON-HOOK(extended-hours-quotes): Extended-hours and regular-session fields for accurate card display.
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
  extType?: 'pre' | 'post' | null;
  extPrice?: number | null;
  extChange?: number | null;
  extPct?: number | null;
  /** ISO code the snapshot was quoted in; null on a payload that predates it. */
  currency: string | null;
  /** The snapshot's asset class: an `index` row prints a level, not a price. */
  assetClass: string | null;
  [key: string]: unknown;
}

/** A row as the agent reads it: the figures the user sees, in the currency
 *  the user sees them in, so a CN¥ price never reaches the agent as dollars. */
export function watchlistQuoteRow(r: WatchlistRow, marketStatus?: MarketStatusData): QuoteRow {
  const hasQuote = r.quoteAvailable !== false;
  const currency = quoteCurrency(r.currency, r.symbol, r.assetClass);
  if (!hasQuote) {
    return { symbol: r.symbol, currency };
  }

  // DALTON-HOOK(extended-hours-quotes): Delegate all session, headline, and extended-hours derivation to deriveQuoteDisplay.
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

  // DALTON-HOOK(extended-hours-quotes): Return pre-market quote row with settled prevClose and preMarket fields.
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

interface WatchlistItem {
  watchlist_item_id: string;
  symbol: string;
  [key: string]: unknown;
}

interface WatchlistItemsData {
  items: WatchlistItem[];
  currentWatchlistId: string | null;
}

interface WatchlistItemData {
  symbol: string;
  [key: string]: unknown;
}

interface ApiError {
  response?: {
    status?: number;
    data?: {
      detail?: string;
      message?: string;
      [key: string]: unknown;
    };
  };
  message?: string;
}

// Last resolved watchlist id — lets the next fetch request the membership in
// parallel with the watchlist list instead of serializing on it. Persisted so
// the first dashboard paint after a reload is parallel too. A stale id is
// harmless: the speculative call is discarded on mismatch or failure and the
// exact sequential path runs as before.
const LAST_WATCHLIST_ID_KEY = 'watchlist_last_id';

function readLastWatchlistId(): string | null {
  try {
    return localStorage.getItem(LAST_WATCHLIST_ID_KEY);
  } catch {
    return null;
  }
}

function storeLastWatchlistId(id: string): void {
  try {
    localStorage.setItem(LAST_WATCHLIST_ID_KEY, id);
  } catch {
    // localStorage unavailable — every fetch stays sequential, as before
  }
}

// The speculative id outlives React and is account-scoped: after an account
// switch it would aim the parallel prefetch at the previous user's watchlist.
registerAuthReset(() => {
  try {
    localStorage.removeItem(LAST_WATCHLIST_ID_KEY);
  } catch {
    // localStorage unavailable — nothing was persisted to clear
  }
});

/**
 * The watchlist's membership alone, for a reader that needs what is on the
 * list but not its quotes (the welcome flow's holdings entry). The same entry
 * the hook below polls, so a symbol added anywhere shows everywhere. The
 * hook adds the polling; a reader that only needs the list does not poll.
 */
export const watchlistItemsQuery = queryOptions({
  queryKey: ['watchlistData'],
  queryFn: async (): Promise<WatchlistItemsData> => {
    const speculativeId = readLastWatchlistId();
    const speculative = speculativeId
      ? (listWatchlistItems(speculativeId) as Promise<{ items?: WatchlistItem[] }>).catch(() => null)
      : null;

    const { watchlists } = await listWatchlists() as { watchlists?: Array<{ watchlist_id: string; [key: string]: unknown }> };
    const firstWatchlist = watchlists?.[0];
    const watchlistId = firstWatchlist?.watchlist_id || 'default';
    storeLastWatchlistId(watchlistId);

    let items: WatchlistItem[] | undefined;
    if (speculative && speculativeId === watchlistId) {
      items = (await speculative)?.items;
    }
    if (items === undefined) {
      ({ items } = await listWatchlistItems(watchlistId) as { items?: WatchlistItem[] });
    }
    return { items: items ?? [], currentWatchlistId: watchlistId };
  },
  staleTime: 1000 * 30, // 30s fresh
});

/**
 * Shared hook for watchlist data fetching and CRUD operations.
 * Used by both Dashboard and MarketView sidebar.
 * Refactored to use TanStack Query for optimal polling and caching.
 */
export function useWatchlistData() {
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const [modalOpen, setModalOpen] = useState(false);

  // Watchlist membership (items) is its own query so add/delete invalidation and
  // polling still key on ['watchlistData']; the per-symbol quotes come from the
  // shared quote layer so overlapping watchlists/portfolios share one fetch.
  const { data: itemsData = { items: [], currentWatchlistId: null }, isLoading: itemsLoading, isSuccess: loaded, refetch: refetchItems } = useQuery({
    ...watchlistItemsQuery,
    refetchInterval: 60000,
    refetchIntervalInBackground: false,
  });

  const { items, currentWatchlistId } = itemsData;

  const symbols = useMemo(
    () => items.map((i) => String(i.symbol || '').trim().toUpperCase()).filter(Boolean),
    [items]
  );

  const { quotes, isLoading: quotesLoading, refetch: refetchQuotes } = useQuotes(symbols, {
    staleTime: 1000 * 30,
    refetchInterval: 60000,
  });

  const rows = useMemo<WatchlistRow[]>(() => {
    if (!items.length) return [];
    const bySym: Record<string, StockPrice> = Object.fromEntries(
      symbols.map((s) => [s, snapshotToStockPrice(s, quotes[s])])
    );
    return items.map((i) => {
      const sym = String(i.symbol || '').trim().toUpperCase();
      const p = bySym[sym] || {} as Partial<StockPrice>;
      const quoteAvailable = p.quoteAvailable !== false && p.price != null;
      return {
        watchlist_item_id: i.watchlist_item_id,
        symbol: sym,
        price: quoteAvailable ? p.price ?? 0 : 0,
        change: quoteAvailable ? p.change ?? 0 : 0,
        changePercent: quoteAvailable ? p.changePercent ?? 0 : 0,
        isPositive: quoteAvailable ? p.isPositive ?? true : true,
        quoteAvailable,
        previousClose: p.previousClose ?? null,
        // DALTON-HOOK(extended-hours-quotes): Forward extended-hours and regular-close fields from StockPrice.
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
        currency: p.currency ?? null,
        assetClass: quotes[sym]?.asset_class ?? null,
      };
    });
  }, [items, symbols, quotes]);

  // Keep the skeleton up until both membership and its quotes have resolved —
  // preserves the single-query loading semantics widgets relied on.
  const loading = itemsLoading || (symbols.length > 0 && quotesLoading);

  const fetchWatchlist = useCallback(async () => {
    await Promise.all([refetchItems(), Promise.resolve(refetchQuotes())]);
  }, [refetchItems, refetchQuotes]);

  const handleAdd = useCallback(
    async (itemData: WatchlistItemData, watchlistId?: string | null) => {
      try {
        let targetWatchlistId = watchlistId || currentWatchlistId;
        if (!targetWatchlistId) {
          const { watchlists } = await listWatchlists() as { watchlists?: Array<{ watchlist_id: string }> };
          targetWatchlistId = watchlists?.[0]?.watchlist_id || 'default';
        }

        await addWatchlistItem(itemData, targetWatchlistId);
        setModalOpen(false);
        queryClient.invalidateQueries({ queryKey: ['watchlistData'] });

        toast({
          title: 'Stock added',
          description: `${itemData.symbol} has been added to your watchlist.`,
        });
      } catch (e: unknown) {
        const err = e as ApiError;
        console.error('Add watchlist item failed:', err?.response?.status, err?.response?.data, err?.message);

        const status = err?.response?.status;
        const msg = err?.response?.data?.detail || err?.response?.data?.message || '';

        if (status === 409 || msg.toLowerCase().includes('already exists')) {
          toast({
            variant: 'destructive',
            title: 'Already in watchlist',
            description: `${itemData.symbol} is already in your watchlist.`,
          });
        } else {
          toast({
            variant: 'destructive',
            title: 'Cannot add stock',
            description: msg || 'Failed to add to watchlist. Please try again.',
          });
        }
      }
    },
    [currentWatchlistId, queryClient, toast]
  );

  const handleDelete = useCallback(
    async (itemId: string) => {
      try {
        let watchlistId = currentWatchlistId;
        if (!watchlistId) {
          const { watchlists } = await listWatchlists() as { watchlists?: Array<{ watchlist_id: string }> };
          watchlistId = watchlists?.[0]?.watchlist_id || 'default';
        }

        await deleteWatchlistItem(itemId, watchlistId);
        queryClient.invalidateQueries({ queryKey: ['watchlistData'] });
      } catch (e: unknown) {
        const err = e as ApiError;
        console.error('Delete watchlist item failed:', err?.response?.status, err?.response?.data, err?.message);
      }
    },
    [currentWatchlistId, queryClient]
  );

  return {
    rows,
    loading,
    /** The items arrived, as opposed to a read that failed or never ran. */
    loaded,
    modalOpen,
    setModalOpen,
    currentWatchlistId,
    fetchWatchlist,
    handleAdd,
    handleDelete,
  };
}
