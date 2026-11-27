import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { getQuoteBatcher } from '@/lib/quotes/quoteBatcher';
import { deriveQuoteDisplay, snapshotToStockPrice, writeQuoteFromWs, type QuoteRow } from '@/lib/quotes';
import { extractSelectedPriceTradeTsMs, mergeRestQuoteRow } from '@/lib/dalton/quotes/quoteMerge';
import { getExtendedHoursInfo } from '@/lib/dalton/quotes/extendedHoursCore';
import { resolveExtendedHoursDisplay } from '@/components/dalton/ExtendedHours';
import {
  mergeFullWatchlistMetricsResponse,
  mergeQuotesIntoWatchlistMetrics,
  mergeWatchlistMetricsResponse,
  usePersonalWatchlistMetrics,
  type PersonalWatchlistCompany,
  type WatchlistMetricRow,
  type WatchlistMetricsResponse,
} from '../watchlist';

vi.mock('@/config/brand', () => ({
  DALTON_PROFILE: 'core',
  isCoreProfileBuild: true,
  BRAND_NAME: 'Dalton',
  BRAND_MARK: 'langalpha',
  isDaltonBuild: true,
  faviconHref: () => '/logo-favicon.svg',
}));

function makeRow(ticker: string, overrides: Partial<WatchlistMetricRow> = {}): WatchlistMetricRow {
  return {
    ticker,
    is_private: false,
    status: 'ok',
    snapshot_pending: false,
    price: 100,
    price_source: 'snapshot',
    price_ts: 1_764_151_200_000,
    currency: 'USD',
    chg_1d: 1.5,
    chg_1w: 2.0,
    chg_1m: 3.0,
    chg_3m: 5.0,
    chg_ytd: 10.0,
    chg_1y: 20.0,
    high_52w: 120,
    low_52w: 80,
    position_52w: 50,
    rsi_14d: 55,
    rsi_30d: 52,
    vol_ratio_5_20: 1.1,
    market_cap: 1_000_000_000_000,
    market_cap_currency: 'USD',
    next_earnings: null,
    ...overrides,
  };
}

describe('watchlist pending-only subset polling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('polls only pending_symbols subset and merges into cached full roster without refetching full table', async () => {
    const fullResp: WatchlistMetricsResponse = {
      ok: true,
      items: {
        AAPL: makeRow('AAPL', { market_cap: 5_167_353_167_872, currency: 'USD' }),
        '0700.HK': makeRow('0700.HK', {
          price: 410,
          currency: 'HKD',
          rsi_14d: null,
          market_cap: null,
          market_cap_currency: 'HKD',
        }),
      },
      pending_symbols: ['0700.HK'],
      as_of: '2026-11-26T10:00:00Z',
    };

    const subsetResp: WatchlistMetricsResponse = {
      ok: true,
      items: {
        '0700.HK': makeRow('0700.HK', {
          price: 410,
          currency: 'HKD',
          rsi_14d: 61.5,
          market_cap: 3_111_992_426_496,
          market_cap_currency: 'HKD',
        }),
      },
      pending_symbols: [],
      as_of: '2026-11-26T10:00:03Z',
    };

    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy
      .mockResolvedValueOnce({ data: fullResp } as Awaited<ReturnType<typeof api.get>>)
      .mockResolvedValueOnce({ data: subsetResp } as Awaited<ReturnType<typeof api.get>>);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result, unmount } = renderHook(
      () => usePersonalWatchlistMetrics(['AAPL', '0700.HK'], true),
      { wrapper },
    );

    // Flush initial full fetch and React Query notification timer
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(getSpy).toHaveBeenNthCalledWith(1, '/api/v1/dalton/watchlist/metrics', {
      params: { symbols: 'AAPL,0700.HK' },
    });
    expect(result.current.data?.pending_symbols).toEqual(['0700.HK']);

    // Advance 3s -> must poll ONLY pending subset (`0700.HK`), NOT full table
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.advanceTimersByTimeAsync(10);
    });

    expect(getSpy).toHaveBeenCalledTimes(2);
    expect(getSpy).toHaveBeenNthCalledWith(2, '/api/v1/dalton/watchlist/metrics', {
      params: { symbols: '0700.HK' },
    });

    // Cached data now merges 0700.HK while keeping AAPL intact and clears pending_symbols
    expect(result.current.data?.pending_symbols).toEqual([]);
    expect(result.current.data?.items.AAPL?.market_cap).toBe(5_167_353_167_872);
    expect(result.current.data?.items['0700.HK']?.market_cap).toBe(3_111_992_426_496);
    expect(result.current.data?.items['0700.HK']?.rsi_14d).toBe(61.5);

    // Advancing another 6s does NOT trigger another 3s subset poll now that pending_symbols is empty
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(getSpy).toHaveBeenCalledTimes(2);

    unmount();
  });

  it('mergeWatchlistMetricsResponse preserves unpolled pending symbols and refreshes summary', () => {
    const prev: WatchlistMetricsResponse = {
      ok: true,
      items: {
        AAPL: makeRow('AAPL', { rsi_14d: 60 }),
        TSM: makeRow('TSM', { rsi_14d: null }),
      },
      pending_symbols: ['TSM', '0700.HK'],
      summary: {
        total_count: 3,
        listed_count: 3,
        private_count: 0,
        tier_counts: { core: 3, watch: 0, radar: 0 },
        lowest_rsi_14d: [{ ticker: 'AAPL', rsi_14d: 60 }],
        highest_rsi_14d: [{ ticker: 'AAPL', rsi_14d: 60 }],
        top_gainers_1d: [],
        top_losers_1d: [],
        upcoming_earnings: [],
      },
      as_of: '2026-11-26T10:00:00Z',
    };

    const merged = mergeWatchlistMetricsResponse(
      prev,
      {
        ok: true,
        items: { TSM: makeRow('TSM', { rsi_14d: 25 }) },
        pending_symbols: [],
        as_of: '2026-11-26T10:00:03Z',
      },
      ['TSM'],
    );

    expect(merged.pending_symbols).toEqual(['0700.HK']);
    expect(merged.summary?.total_count).toBe(3);
    expect(merged.summary?.lowest_rsi_14d[0]).toEqual({ ticker: 'TSM', rsi_14d: 25 });
  });

  it('mergeQuotesIntoWatchlistMetrics compares trade timestamps, prefers /metrics on ties, and never lets daily_close overwrite live quotes', () => {
    const companies: PersonalWatchlistCompany[] = [
      { ticker: 'NVDA', name: 'NVIDIA', tier: 'core', is_private: false },
      { ticker: '0700.HK', name: 'Tencent', tier: 'watch', is_private: false },
      { ticker: 'AAPL', name: 'Apple', tier: 'core', is_private: false },
      { ticker: 'MSFT', name: 'Microsoft', tier: 'core', is_private: false },
      { ticker: 'OPENAI', name: 'OpenAI', tier: 'core', is_private: true },
    ];

    const T1 = 1_764_151_200_000;
    const T2 = 1_764_151_260_000;

    // 1. Before /metrics returns: useQuotes populates price, chg_1d, currency immediately
    const beforeMetrics = mergeQuotesIntoWatchlistMetrics(companies, {}, {
      NVDA: { symbol: 'NVDA', price: 142.5, change_percent: 3.15, currency: 'USD', as_of: T1 },
      '0700.HK': { symbol: '0700.HK', price: null, previous_close: 400, currency: 'HKD', as_of: T1 },
      AAPL: { symbol: 'AAPL', price: 210, previous_close: 200, currency: 'USD', as_of: T1 },
      OPENAI: { symbol: 'OPENAI', price: 999, change_percent: 10, currency: 'USD', as_of: T1 },
    });

    expect(beforeMetrics.NVDA?.price).toBe(142.5);
    expect(beforeMetrics.NVDA?.chg_1d).toBe(3.15);
    expect(beforeMetrics.NVDA?.currency).toBe('USD');
    expect(beforeMetrics.NVDA?.rsi_14d).toBeNull();
    expect(beforeMetrics['0700.HK']?.price).toBe(400);
    expect(beforeMetrics['0700.HK']?.chg_1d).toBe(0);
    expect(beforeMetrics.AAPL?.price).toBe(210);
    expect(beforeMetrics.AAPL?.chg_1d).toBe(5);
    expect(beforeMetrics.OPENAI).toBeUndefined();

    // 2. Compare timestamps & price_source when both /metrics and useQuotes exist:
    // - NVDA: quote has strictly newer trade timestamp (T2 > T1) -> quote wins while preserving /metrics RSI
    // - AAPL: same timestamp (T1 === T1) -> /metrics wins
    // - MSFT: /metrics is daily_close fallback -> NEVER overwrites useQuotes live price
    // - 0700.HK: /metrics warming with price: null -> falls back to useQuotes
    const afterMetrics = mergeQuotesIntoWatchlistMetrics(
      companies,
      {
        NVDA: makeRow('NVDA', { price: 138.4, price_source: 'snapshot', price_ts: T1, chg_1d: 2.45, rsi_14d: 64.2 }),
        AAPL: makeRow('AAPL', { price: 211.0, price_source: 'snapshot', price_ts: T1, chg_1d: 5.5, rsi_14d: 58.0 }),
        MSFT: makeRow('MSFT', { price: 410.0, price_source: 'daily_close', price_ts: T2, chg_1d: -0.5, rsi_14d: 51.2 }),
        '0700.HK': makeRow('0700.HK', {
          status: 'warming',
          price: null,
          price_source: null,
          price_ts: null,
          chg_1d: null,
          currency: null,
          rsi_14d: null,
        }),
      },
      {
        NVDA: { symbol: 'NVDA', price: 149.99, change_percent: 6.5, currency: 'USD', live_tick_at: T2 },
        AAPL: { symbol: 'AAPL', price: 210.5, change_percent: 5.25, currency: 'USD', as_of: T1 },
        MSFT: { symbol: 'MSFT', price: 425.75, change_percent: 1.9, currency: 'USD', as_of: T1 },
        '0700.HK': { symbol: '0700.HK', price: 412, change_percent: 1.8, currency: 'HKD', as_of: T1 },
      },
    );

    expect(afterMetrics.NVDA?.price).toBe(149.99);
    expect(afterMetrics.NVDA?.chg_1d).toBe(6.5);
    expect(afterMetrics.NVDA?.rsi_14d).toBe(64.2);

    expect(afterMetrics.AAPL?.price).toBe(211.0);
    expect(afterMetrics.AAPL?.chg_1d).toBe(5.5);

    expect(afterMetrics.MSFT?.price).toBe(425.75);
    expect(afterMetrics.MSFT?.chg_1d).toBe(1.9);
    expect(afterMetrics.MSFT?.rsi_14d).toBe(51.2);

    expect(afterMetrics['0700.HK']?.price).toBe(412);
    expect(afterMetrics['0700.HK']?.chg_1d).toBe(1.8);
    expect(afterMetrics['0700.HK']?.currency).toBe('HKD');
  });

  it('mergeWatchlistMetricsResponse never overwrites existing values with nulls or daily_close regression', () => {
    const prev: WatchlistMetricsResponse = {
      ok: true,
      items: {
        '0700.HK': makeRow('0700.HK', {
          price: 420.5,
          price_source: 'snapshot',
          price_ts: 1_764_151_260_000,
          chg_1d: 2.1,
          rsi_14d: 60.5,
          market_cap: 3_111_992_426_496,
        }),
      },
      pending_symbols: ['0700.HK'],
      as_of: '2026-11-26T10:00:00Z',
    };

    const merged = mergeWatchlistMetricsResponse(
      prev,
      {
        ok: true,
        items: {
          '0700.HK': makeRow('0700.HK', {
            price: 405.0,
            price_source: 'daily_close',
            price_ts: 1_764_151_300_000,
            chg_1d: -0.4,
            rsi_14d: null,
            market_cap: null,
          }),
        },
        pending_symbols: ['0700.HK'],
        as_of: '2026-11-26T10:00:03Z',
      },
      ['0700.HK'],
    );

    expect(merged.items['0700.HK']?.price).toBe(420.5);
    expect(merged.items['0700.HK']?.price_source).toBe('snapshot');
    expect(merged.items['0700.HK']?.chg_1d).toBe(2.1);
    expect(merged.items['0700.HK']?.rsi_14d).toBe(60.5);
    expect(merged.items['0700.HK']?.market_cap).toBe(3_111_992_426_496);
  });

  it('drops out-of-order subset and full refresh responses using request generations', async () => {
    const T1 = 1_764_151_200_000;
    const T2 = 1_764_151_260_000;
    const T3 = 1_764_151_320_000;

    type GetResult = Awaited<ReturnType<typeof api.get>>;
    const deferreds: Array<(val: GetResult) => void> = [];
    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy.mockImplementation(
      () =>
        new Promise<GetResult>((resolve) => {
          deferreds.push(resolve);
        }),
    );

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result, unmount } = renderHook(
      () => usePersonalWatchlistMetrics(['AAPL', '0700.HK'], true),
      { wrapper },
    );

    // 1. Initial full request (gen 1) resolves with 0700.HK still pending
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(deferreds.length).toBe(1);
    await act(async () => {
      deferreds[0]({
        data: {
          ok: true,
          items: {
            AAPL: makeRow('AAPL', { price: 200, price_ts: T1 }),
            '0700.HK': makeRow('0700.HK', { price: 400, price_ts: T1, rsi_14d: null }),
          },
          pending_symbols: ['0700.HK'],
          as_of: '2026-11-26T10:00:00Z',
        },
      } as GetResult);
      await vi.advanceTimersByTimeAsync(10);
    });

    // 2. Case A: Subset poll starts at 3s (gen 2), then full refresh starts (gen 3) and finishes FIRST.
    //    When stale subset (gen 2) arrives late, it MUST be discarded!
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(deferreds.length).toBe(2); // subset gen 2 in flight

    const fullRefetchPromise = result.current.refetch();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(deferreds.length).toBe(3); // full gen 3 in flight

    // Resolve full gen 3 FIRST with newer price 430 & pending still ['0700.HK']
    await act(async () => {
      deferreds[2]({
        data: {
          ok: true,
          items: {
            AAPL: makeRow('AAPL', { price: 205, price_ts: T2 }),
            '0700.HK': makeRow('0700.HK', { price: 430, price_ts: T2, rsi_14d: 58 }),
          },
          pending_symbols: ['0700.HK'],
          as_of: '2026-11-26T10:00:04Z',
        },
      } as GetResult);
      await fullRefetchPromise;
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items['0700.HK']?.price).toBe(430);

    // Resolve stale subset gen 2 LATE with older price 405 -> must be dropped!
    await act(async () => {
      deferreds[1]({
        data: {
          ok: true,
          items: {
            '0700.HK': makeRow('0700.HK', { price: 405, price_ts: T1, rsi_14d: 40 }),
          },
          pending_symbols: ['0700.HK'],
          as_of: '2026-11-26T10:00:03Z',
        },
      } as GetResult);
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items['0700.HK']?.price).toBe(430);
    expect(result.current.data?.items['0700.HK']?.rsi_14d).toBe(58);

    // 3. Case B: Full refresh starts (gen 4), then subset poll starts (gen 5) and finishes FIRST.
    //    When stale full refresh (gen 4) arrives late, it MUST NOT clobber gen 5!
    const slowFullPromise = result.current.refetch();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(deferreds.length).toBe(4); // full gen 4 in flight

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(deferreds.length).toBe(5); // subset gen 5 in flight

    // Resolve subset gen 5 FIRST (clears pending_symbols, sets price 445)
    await act(async () => {
      deferreds[4]({
        data: {
          ok: true,
          items: {
            '0700.HK': makeRow('0700.HK', { price: 445, price_ts: T3, rsi_14d: 63 }),
          },
          pending_symbols: [],
          as_of: '2026-11-26T10:00:08Z',
        },
      } as GetResult);
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items['0700.HK']?.price).toBe(445);
    expect(result.current.data?.pending_symbols).toEqual([]);

    // Resolve stale full gen 4 LATE -> must NOT revert 0700.HK back to 430 or re-add pending_symbols!
    await act(async () => {
      deferreds[3]({
        data: {
          ok: true,
          items: {
            AAPL: makeRow('AAPL', { price: 206, price_ts: T2 }),
            '0700.HK': makeRow('0700.HK', { price: 430, price_ts: T2, rsi_14d: 58 }),
          },
          pending_symbols: ['0700.HK'],
          as_of: '2026-11-26T10:00:05Z',
        },
      } as GetResult);
      await slowFullPromise;
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items['0700.HK']?.price).toBe(445);
    expect(result.current.data?.items['0700.HK']?.rsi_14d).toBe(63);
    expect(result.current.data?.pending_symbols).toEqual([]);

    unmount();
  });

  it('chunks 300-symbol cold load in shared QuoteBatcher to <=250 per request and settles batches independently', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const batcher = getQuoteBatcher(qc);
    const batchSizes: number[] = [];

    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy.mockImplementation((async (_url: string, config?: { params?: { symbols?: string } }) => {
      const syms = String(config?.params?.symbols ?? '').split(',').filter(Boolean);
      batchSizes.push(syms.length);
      return {
        data: {
          snapshots: syms.map((symbol, idx) => ({
            symbol,
            price: 100 + idx,
            change_percent: 1.25,
            currency: 'USD',
          })),
        },
      };
    }) as typeof api.get);

    const symbols = Array.from({ length: 300 }, (_, i) => `SYM${String(i + 1).padStart(3, '0')}`);
    const promises = symbols.map((s) => batcher.request(s));

    await vi.advanceTimersByTimeAsync(60);
    const rows = await Promise.all(promises);

    expect(batchSizes).toEqual([250, 50]);
    expect(rows.length).toBe(300);
    expect(qc.getQueryData(queryKeys.quote.detail('SYM001'))).toMatchObject({ symbol: 'SYM001', price: 100 });
    expect(qc.getQueryData(queryKeys.quote.detail('SYM250'))).toMatchObject({ symbol: 'SYM250', price: 349 });
    expect(qc.getQueryData(queryKeys.quote.detail('SYM251'))).toMatchObject({ symbol: 'SYM251', price: 100 });
    expect(qc.getQueryData(queryKeys.quote.detail('SYM300'))).toMatchObject({ symbol: 'SYM300', price: 149 });

    // Failure isolation: if batch 2 fails, batch 1 (including Dashboard subscriptions) still fills cache
    const qc2 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const batcher2 = getQuoteBatcher(qc2);
    let callIdx = 0;
    getSpy.mockImplementation((async (_url: string, config?: { params?: { symbols?: string } }) => {
      callIdx += 1;
      const syms = String(config?.params?.symbols ?? '').split(',').filter(Boolean);
      if (callIdx === 2) {
        throw new Error('batch 2 transient failure');
      }
      return {
        data: {
          snapshots: syms.map((symbol) => ({ symbol, price: 88.5, currency: 'USD' })),
        },
      };
    }) as typeof api.get);

    const settledPromise = Promise.allSettled(symbols.map((s) => batcher2.request(s)));
    await vi.advanceTimersByTimeAsync(60);
    const settled = await settledPromise;

    expect(settled.slice(0, 250).every((r) => r.status === 'fulfilled')).toBe(true);
    expect(settled.slice(250).every((r) => r.status === 'rejected')).toBe(true);
    expect(qc2.getQueryData(queryKeys.quote.detail('SYM001'))).toMatchObject({ price: 88.5 });
    expect(qc2.getQueryData(queryKeys.quote.detail('SYM251'))).toBeUndefined();
  });

  it('resolveExtendedHoursDisplay and deriveQuoteDisplay handle pre, post, overnight, regular, and non-US HK symbols consistently', () => {
    const usPreQuote: QuoteRow = {
      symbol: 'GOOG',
      price: 320.76,
      previous_close: 320.76,
      regular_close: 320.76,
      regular_trading_change: -3.46,
      regular_trading_change_percent: -1.0672,
      early_trading_change: 1.5,
      early_trading_change_percent: 0.4676,
      currency: 'USD',
    };
    const usPostQuote: QuoteRow = {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change_percent: -0.83,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change_percent: 0.2584,
      currency: 'USD',
    };
    const hkQuote: QuoteRow = {
      symbol: '0700.HK',
      price: 410.2,
      regular_close: 410.2,
      previous_close: 400,
      change: 10.2,
      change_percent: 2.55,
      late_trading_change_percent: 0.9,
      currency: 'HKD',
    };

    const preRes = resolveExtendedHoursDisplay(
      usPreQuote,
      { market: 'extended-hours', earlyHours: true, afterHours: false },
      { symbol: 'GOOG', locale: 'en-US' },
    );
    expect(preRes.session).toBe('pre');
    expect(preRes.mainPrice).toBe(320.76);
    expect(preRes.ext).toEqual({ label: 'PM', price: '$322.26', pct: 0.47 });

    const postRes = resolveExtendedHoursDisplay(
      usPostQuote,
      { market: 'extended-hours', earlyHours: false, afterHours: true },
      { symbol: 'GOOG', locale: 'en-US' },
    );
    expect(postRes.session).toBe('post');
    expect(postRes.mainPrice).toBe(317.28);
    expect(postRes.ext).toEqual({ label: 'AH', price: '$318.10', pct: 0.26 });

    // Overnight (closed after AH end and before next PM start) keeps last AH print
    const overnightRes = resolveExtendedHoursDisplay(
      usPostQuote,
      { market: 'closed', earlyHours: false, afterHours: false },
      { symbol: 'GOOG', locale: 'en-US' },
    );
    expect(overnightRes.session).toBe('post');
    expect(overnightRes.mainPrice).toBe(317.28);
    expect(overnightRes.ext).toEqual({ label: 'AH', price: '$318.10', pct: 0.26 });

    // Regular trading hours suppresses extended-hours badge
    const regularRes = resolveExtendedHoursDisplay(
      usPostQuote,
      { market: 'open', earlyHours: false, afterHours: false },
      { symbol: 'GOOG', locale: 'en-US' },
    );
    expect(regularRes.session).toBe('regular');
    expect(regularRes.ext).toBeNull();

    // Non-US HK listing never shows US extended hours even if stray fields exist
    const hkDirect = deriveQuoteDisplay(hkQuote, { market: 'extended-hours', earlyHours: false, afterHours: true });
    expect(hkDirect.session).toBe('regular');
    expect(hkDirect.extPrice).toBeNull();
    const hkRes = resolveExtendedHoursDisplay(
      hkQuote,
      { market: 'closed', earlyHours: false, afterHours: false },
      { symbol: '0700.HK', locale: 'en-US' },
    );
    expect(hkRes.session).toBe('regular');
    expect(hkRes.ext).toBeNull();
  });

  it('r5: applies unified selected-price trade-timestamp merge across REST QuoteBatcher, WS writeQuoteFromWs, and /metrics (ties keep /metrics)', async () => {
    const T1 = 1_764_151_200_000;
    const T2 = 1_764_151_260_000;
    const T3 = 1_764_151_320_000;
    const T4 = 1_764_151_380_000;

    // 1. REST backfill in QuoteBatcher MUST NOT overwrite a newer WS quote in shared cache,
    //    even when Ginlix REST carries a newer bid/ask `as_of` (T3 > T2) alongside an older `last_trade_at` (T1 < T2).
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const batcher = getQuoteBatcher(qc);
    qc.setQueryData(queryKeys.quote.detail('NVDA'), {
      symbol: 'NVDA',
      source: 'ginlix-data',
      price: 140.0,
      change: 5.0,
      change_percent: 3.7,
      previous_close: 135.0,
      last_trade_price: 140.0,
      last_trade_at: T1,
      as_of: T1,
      currency: 'USD',
    });

    // Newer WS tick lands at T2 with price 145.0
    writeQuoteFromWs(qc, 'NVDA', {
      price: 145.0,
      change: 10.0,
      changePercent: 7.41,
      timestamp: T2,
    });
    expect(qc.getQueryData(queryKeys.quote.detail('NVDA'))).toMatchObject({
      price: 145.0,
      change_percent: 7.41,
      live_tick_at: T2,
    });

    // Stale WS tick at T1 (< T2) MUST be ignored by the same unified rule
    writeQuoteFromWs(qc, 'NVDA', {
      price: 139.0,
      change: 4.0,
      changePercent: 2.96,
      timestamp: T1,
    });
    expect(qc.getQueryData(queryKeys.quote.detail('NVDA'))).toMatchObject({
      price: 145.0,
      change_percent: 7.41,
      live_tick_at: T2,
    });

    // Older REST backfill arrives with newer bid/ask `as_of = T3`, but `last_trade_at = T1 < T2`
    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy.mockResolvedValueOnce({
      data: {
        snapshots: [
          {
            symbol: 'NVDA',
            name: 'NVIDIA Corp',
            source: 'ginlix-data',
            price: 140.0,
            change: 5.0,
            change_percent: 3.7,
            previous_close: 135.0,
            last_trade_price: 140.0,
            last_trade_at: T1,
            as_of: T3,
            currency: 'USD',
          },
        ],
      },
    } as Awaited<ReturnType<typeof api.get>>);

    const req1 = batcher.request('NVDA');
    await vi.advanceTimersByTimeAsync(60);
    const mergedAfterOlderRest = await req1;
    expect(mergedAfterOlderRest).toMatchObject({
      name: 'NVIDIA Corp',
      price: 145.0,
      change_percent: 7.41,
      live_tick_at: T2,
    });
    expect(qc.getQueryData(queryKeys.quote.detail('NVDA'))).toMatchObject({
      name: 'NVIDIA Corp',
      price: 145.0,
      change_percent: 7.41,
      live_tick_at: T2,
    });

    // Genuinely newer REST trade (`last_trade_at = T4 > T2`, matching `price === last_trade_price`) DOES update
    getSpy.mockResolvedValueOnce({
      data: {
        snapshots: [
          {
            symbol: 'NVDA',
            name: 'NVIDIA Corp',
            source: 'ginlix-data',
            price: 148.0,
            change: 13.0,
            change_percent: 9.63,
            previous_close: 135.0,
            last_trade_price: 148.0,
            last_trade_at: T4,
            as_of: T4,
            currency: 'USD',
          },
        ],
      },
    } as Awaited<ReturnType<typeof api.get>>);

    const req2 = batcher.request('NVDA');
    await vi.advanceTimersByTimeAsync(60);
    const mergedAfterNewerRest = await req2;
    expect(mergedAfterNewerRest).toMatchObject({
      price: 148.0,
      change_percent: 9.63,
      last_trade_at: T4,
    });

    // 2. `/metrics` merge with shared cache:
    //    - NVDA: shared cache has WS trade at T2, `/metrics` has older trade at T1 -> WS wins
    //    - AAPL: shared cache has Ginlix bid/ask `as_of = T3` but `last_trade_at = T1`, `/metrics` has T2 -> `/metrics` wins
    //    - MSFT: equal trade timestamp T2 === T2 -> `/metrics` wins on tie!
    //    - META: shared cache has Ginlix mismatched `price` (189.96) vs `last_trade_price` (190.50 at T3), `/metrics` has T2 -> `/metrics` wins!
    const companies: PersonalWatchlistCompany[] = [
      { ticker: 'NVDA', name: 'NVIDIA', tier: 'core', is_private: false },
      { ticker: 'AAPL', name: 'Apple', tier: 'core', is_private: false },
      { ticker: 'MSFT', name: 'Microsoft', tier: 'core', is_private: false },
      { ticker: 'META', name: 'Meta', tier: 'core', is_private: false },
    ];
    const merged = mergeQuotesIntoWatchlistMetrics(
      companies,
      {
        NVDA: makeRow('NVDA', { price: 140.0, price_source: 'snapshot', price_ts: T1, chg_1d: 3.7 }),
        AAPL: makeRow('AAPL', { price: 212.0, price_source: 'snapshot', price_ts: T2, chg_1d: 2.1 }),
        MSFT: makeRow('MSFT', { price: 420.0, price_source: 'snapshot', price_ts: T2, chg_1d: 1.5 }),
        META: makeRow('META', { price: 510.0, price_source: 'snapshot', price_ts: T2, chg_1d: 0.9 }),
      },
      {
        NVDA: { symbol: 'NVDA', price: 145.0, change_percent: 7.41, currency: 'USD', live_tick_at: T2 },
        AAPL: {
          symbol: 'AAPL',
          source: 'ginlix-data',
          price: 210.0,
          change_percent: 1.1,
          last_trade_price: 210.0,
          last_trade_at: T1,
          as_of: T3,
          currency: 'USD',
        },
        MSFT: {
          symbol: 'MSFT',
          source: 'ginlix-data',
          price: 419.5,
          change_percent: 1.38,
          last_trade_price: 419.5,
          last_trade_at: T2,
          as_of: T3,
          currency: 'USD',
        },
        META: {
          symbol: 'META',
          source: 'ginlix-data',
          price: 505.0,
          change_percent: -0.2,
          last_trade_price: 512.0,
          last_trade_at: T3,
          as_of: T3,
          currency: 'USD',
        },
      },
    );

    expect(merged.NVDA?.price).toBe(145.0);
    expect(merged.NVDA?.chg_1d).toBe(7.41);
    expect(merged.AAPL?.price).toBe(212.0);
    expect(merged.AAPL?.chg_1d).toBe(2.1);
    expect(merged.MSFT?.price).toBe(420.0);
    expect(merged.MSFT?.chg_1d).toBe(1.5);
    expect(merged.META?.price).toBe(510.0);
    expect(merged.META?.chg_1d).toBe(0.9);
  });

  it('r5: full refresh clears expired next_earnings to null while subset merge preserves unpolled rows', async () => {
    const prev: WatchlistMetricsResponse = {
      ok: true,
      items: {
        NVDA: makeRow('NVDA', { next_earnings: '2026-11-20', rsi_14d: 60 }),
        AAPL: makeRow('AAPL', { next_earnings: '2026-12-20', rsi_14d: 55 }),
      },
      pending_symbols: ['NVDA'],
      summary: {
        total_count: 2,
        listed_count: 2,
        private_count: 0,
        tier_counts: { core: 2, watch: 0, radar: 0 },
        lowest_rsi_14d: [],
        highest_rsi_14d: [],
        top_gainers_1d: [],
        top_losers_1d: [],
        upcoming_earnings: [
          { ticker: 'NVDA', date: '2026-11-20' },
          { ticker: 'AAPL', date: '2026-12-20' },
        ],
      },
      as_of: '2026-11-26T10:00:00Z',
    };

    // 1. Subset merge for NVDA only preserves unpolled AAPL.next_earnings
    const afterSubset = mergeWatchlistMetricsResponse(
      prev,
      {
        ok: true,
        items: { NVDA: makeRow('NVDA', { next_earnings: null, rsi_14d: 61 }) },
        pending_symbols: [],
        as_of: '2026-11-26T10:00:03Z',
      },
      ['NVDA'],
    );
    expect(afterSubset.items.AAPL?.next_earnings).toBe('2026-12-20');
    expect(afterSubset.items.NVDA?.next_earnings).toBe('2026-11-20');

    // 2. Full refresh clears expired NVDA.next_earnings to null and removes it from summary.upcoming_earnings
    const afterFull = mergeFullWatchlistMetricsResponse(prev, {
      ok: true,
      items: {
        NVDA: makeRow('NVDA', { next_earnings: null, rsi_14d: 62 }),
        AAPL: makeRow('AAPL', { next_earnings: '2026-12-20', rsi_14d: 56 }),
      },
      pending_symbols: [],
      as_of: '2026-11-26T10:01:00Z',
    });
    expect(afterFull.items.NVDA?.next_earnings).toBeNull();
    expect(afterFull.items.AAPL?.next_earnings).toBe('2026-12-20');
    expect(afterFull.summary?.upcoming_earnings).toEqual([{ ticker: 'AAPL', date: '2026-12-20' }]);

    // 3. End-to-end hook refetch clears expired next_earnings in React Query cache
    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy
      .mockResolvedValueOnce({ data: { ...prev, pending_symbols: [] } } as Awaited<ReturnType<typeof api.get>>)
      .mockResolvedValueOnce({
        data: {
          ...prev,
          pending_symbols: [],
          items: {
            NVDA: makeRow('NVDA', { next_earnings: null, rsi_14d: 62 }),
            AAPL: makeRow('AAPL', { next_earnings: '2026-12-20', rsi_14d: 56 }),
          },
          as_of: '2026-11-26T10:01:00Z',
        },
      } as Awaited<ReturnType<typeof api.get>>);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(
      () => usePersonalWatchlistMetrics(['NVDA', 'AAPL'], true),
      { wrapper },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items.NVDA?.next_earnings).toBe('2026-11-20');

    await act(async () => {
      await result.current.refetch();
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.data?.items.NVDA?.next_earnings).toBeNull();
    expect(result.current.data?.items.AAPL?.next_earnings).toBe('2026-12-20');
    expect(result.current.data?.summary?.upcoming_earnings).toEqual([{ ticker: 'AAPL', date: '2026-12-20' }]);
    unmount();
  });

  it('r5: entering pre-market only uses current pre-market data and never carries over prior-session AH', () => {
    const preStatus = { market: 'extended-hours', earlyHours: true, afterHours: false };
    const staleAfterHoursQuote: QuoteRow = {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      early_trading_change: null,
      early_trading_change_percent: null,
      late_trading_change: 0.82,
      late_trading_change_percent: 0.2584,
      currency: 'USD',
    };

    const info = getExtendedHoursInfo(preStatus, staleAfterHoursQuote, { shortLabels: true });
    expect(info.extType).toBeNull();
    expect(info.extLabel).toBeNull();
    expect(info.extPct).toBeNull();

    const display = resolveExtendedHoursDisplay(staleAfterHoursQuote, preStatus, {
      symbol: 'GOOG',
      locale: 'en-US',
    });
    expect(display.session).toBe('pre');
    expect(display.mainPrice).toBe(317.28);
    expect(display.mainChangePct).toBeCloseTo(-1.0849, 4);
    expect(display.ext).toBeNull();
  });

  it('r6: identifies Ginlix only by source so real REST rows with last_trade_at: null overwrite older cache and /metrics', async () => {
    const T1 = 1_764_151_200_000;
    const T2 = 1_764_151_260_000;

    // 1. Real SnapshotData REST shape from FMP / Tushare / yfinance includes `last_trade_at: null`
    //    and a newer `as_of: T2`. Must overwrite older cached REST row (`as_of: T1`).
    for (const src of ['fmp', 'tushare', 'yfinance'] as const) {
      const prev: QuoteRow = {
        symbol: 'AAPL',
        source: src,
        price: 200.0,
        change: 1.0,
        change_percent: 0.5,
        last_trade_at: null,
        regular_as_of: null,
        as_of: T1,
        currency: 'USD',
      };
      const incoming: QuoteRow = {
        symbol: 'AAPL',
        source: src,
        price: 204.5,
        change: 5.5,
        change_percent: 2.76,
        last_trade_at: null,
        regular_as_of: null,
        as_of: T2,
        currency: 'USD',
      };
      const mergedRest = mergeRestQuoteRow(prev, incoming);
      expect(mergedRest.price, `${src} REST merge`).toBe(204.5);
      expect(mergedRest.as_of, `${src} as_of`).toBe(T2);

      // Same real REST row must also overwrite older `/metrics` price (price_ts = T1)
      const mergedMetrics = mergeQuotesIntoWatchlistMetrics(
        [{ ticker: 'AAPL', name: 'Apple', tier: 'core', is_private: false }],
        {
          AAPL: makeRow('AAPL', {
            price: 200.0,
            price_source: 'snapshot',
            price_ts: T1,
            chg_1d: 0.5,
          }),
        },
        { AAPL: incoming },
      );
      expect(mergedMetrics.AAPL?.price, `${src} /metrics merge`).toBe(204.5);
      expect(mergedMetrics.AAPL?.chg_1d, `${src} /metrics chg_1d`).toBe(2.76);
    }
  });

  it('r6: preserves winning price provenance on REST merge and switches daily source to ws on WS update', async () => {
    const T1 = 1_764_151_200_000;
    const T2 = 1_764_151_260_000;
    const T3 = 1_764_151_320_000;
    const T4 = 1_764_151_380_000;
    const T5 = 1_764_151_440_000;

    // 1. Daily fallback -> WS update (source becomes 'ws') -> older REST: price MUST stay WS value
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const batcher = getQuoteBatcher(qc);
    qc.setQueryData(queryKeys.quote.detail('NVDA'), {
      symbol: 'NVDA',
      source: 'daily',
      price: 130.0,
      change: 0.0,
      change_percent: 0.0,
      previous_close: 130.0,
      as_of: T1,
      currency: 'USD',
    });

    writeQuoteFromWs(qc, 'NVDA', {
      price: 136.5,
      change: 6.5,
      changePercent: 5.0,
      timestamp: T2,
    });
    expect(qc.getQueryData<QuoteRow>(queryKeys.quote.detail('NVDA'))).toMatchObject({
      source: 'ws',
      price: 136.5,
      live_tick_at: T2,
    });

    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy.mockResolvedValueOnce({
      data: {
        snapshots: [
          {
            symbol: 'NVDA',
            name: 'NVIDIA Corp',
            source: 'fmp',
            price: 132.0,
            change: 2.0,
            change_percent: 1.54,
            previous_close: 130.0,
            last_trade_at: null,
            as_of: T1,
            currency: 'USD',
          },
        ],
      },
    } as Awaited<ReturnType<typeof api.get>>);

    const req = batcher.request('NVDA');
    await vi.advanceTimersByTimeAsync(60);
    const afterOlderRest = await req;
    expect(afterOlderRest).toMatchObject({
      name: 'NVIDIA Corp',
      source: 'ws',
      price: 136.5,
      change_percent: 5.0,
      live_tick_at: T2,
    });

    // 2. Non-WS newer price (FMP @ T3) -> older Ginlix REST (T1) -> second older Ginlix REST (T2):
    //    price and FMP provenance survive BOTH merges without leaking Ginlix fields!
    const newerFmp: QuoteRow = {
      symbol: 'AAPL',
      source: 'fmp',
      tier: 'realtime',
      price: 215.0,
      change: 5.0,
      change_percent: 2.38,
      as_of: T3,
      currency: 'USD',
    };
    const olderGinlix1: QuoteRow = {
      symbol: 'AAPL',
      name: 'Apple Inc.',
      source: 'ginlix-data',
      tier: 'delayed_15m',
      price: 210.0,
      change: 0.0,
      change_percent: 0.0,
      last_trade_at: T1,
      last_trade_price: 210.0,
      as_of: T4,
      currency: 'USD',
    };
    const olderGinlix2: QuoteRow = {
      symbol: 'AAPL',
      name: 'Apple Inc.',
      source: 'ginlix-data',
      tier: 'delayed_15m',
      price: 211.0,
      change: 1.0,
      change_percent: 0.48,
      last_trade_at: T2,
      last_trade_price: 211.0,
      as_of: T5,
      currency: 'USD',
    };

    const afterGinlix1 = mergeRestQuoteRow(newerFmp, olderGinlix1);
    expect(afterGinlix1.price).toBe(215.0);
    expect(afterGinlix1.source).toBe('fmp');
    expect(afterGinlix1.as_of).toBe(T3);
    expect(afterGinlix1.tier).toBe('realtime');
    expect(afterGinlix1.name).toBe('Apple Inc.');
    expect('last_trade_at' in afterGinlix1).toBe(false);
    expect('last_trade_price' in afterGinlix1).toBe(false);

    const afterGinlix2 = mergeRestQuoteRow(afterGinlix1, olderGinlix2);
    expect(afterGinlix2.price).toBe(215.0);
    expect(afterGinlix2.source).toBe('fmp');
    expect(afterGinlix2.as_of).toBe(T3);
    expect('last_trade_at' in afterGinlix2).toBe(false);
  });

  it('r6: pre-market REST replaces stale prior-session AH WS last_minute_close and unprinted pre-market uses prevClose + regular change', () => {
    const T_reg = 1_764_104_400_000; // yesterday 16:00 ET
    const T_ah = 1_764_115_200_000; // yesterday 19:00 ET
    const T_pm = 1_764_162_000_000; // today 08:00 ET
    const preStatus = { market: 'extended-hours', earlyHours: true, afterHours: false };

    // Cached row after last night's AH WS print at 105 (regular close was 100)
    const staleAhWsRow: QuoteRow = {
      symbol: 'GOOG',
      source: 'ginlix-data',
      price: 105,
      regular_close: 100,
      previous_close: 100,
      regular_trading_change: 2,
      regular_trading_change_percent: 2.04,
      late_trading_change: 5,
      late_trading_change_percent: 5.0,
      last_minute_close: 105,
      live_tick_at: T_ah,
      currency: 'USD',
    };

    // 1. This morning's pre-market REST prints 102 (regular close 100 at T_reg < T_ah, PM print at T_pm > T_ah)
    const morningPmRest: QuoteRow = {
      symbol: 'GOOG',
      source: 'yfinance',
      price: 100,
      regular_close: 100,
      previous_close: 100,
      regular_as_of: T_reg,
      regular_only: false,
      regular_trading_change: 2,
      regular_trading_change_percent: 2.04,
      early_trading_change: 2,
      early_trading_change_percent: 2.0,
      last_minute_close: 102,
      last_trade_at: T_pm,
      as_of: T_pm,
      currency: 'USD',
    };

    const mergedPm = mergeRestQuoteRow(staleAhWsRow, morningPmRest);
    expect(mergedPm.last_minute_close).toBe(102);
    const derivedPm = deriveQuoteDisplay(snapshotToStockPrice('GOOG', mergedPm), preStatus);
    expect(derivedPm.session).toBe('pre');
    expect(derivedPm.mainPrice).toBe(100);
    expect(derivedPm.mainChange).toBe(2);
    expect(derivedPm.mainChangePct).toBeCloseTo(2.04, 2);
    expect(derivedPm.extPrice).toBe(102);
    expect(derivedPm.extChangePct).toBeCloseTo(2.0, 2);

    // 2. Entering pre-market with NO pre-market print yet (still holding last night's AH WS price 105):
    //    mainPrice uses prevClose (100), 1D uses regular_trading_change_percent (2.04), NO ext sub-row!
    const unprintedPre = deriveQuoteDisplay(snapshotToStockPrice('GOOG', staleAhWsRow), preStatus);
    expect(unprintedPre.session).toBe('pre');
    expect(unprintedPre.mainPrice).toBe(100);
    expect(unprintedPre.mainChange).toBe(2);
    expect(unprintedPre.mainChangePct).toBeCloseTo(2.04, 2);
    expect(unprintedPre.extPrice).toBeNull();
    expect(unprintedPre.extChange).toBeNull();
    expect(unprintedPre.extChangePct).toBeNull();
  });

  it('r7: REST regular_only Yahoo snapshot (8:31) and SnapshotData-serialized Yahoo PM row never overwrite 8:30 WS trade (102)', async () => {
    const T_prev_close = 1_792_612_800_000; // yesterday 16:00 ET
    const T_830 = 1_792_672_200_000; // today 08:30 ET
    const T_831 = 1_792_672_260_000; // today 08:31 ET

    // 1. 8:30 WS trade at 102, followed by 8:31 Yahoo regular_only snapshot (yesterday close 100):
    //    regular_only snapshot must not participate in newer-trade comparison or overwrite WS 102.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const batcher = getQuoteBatcher(qc);
    qc.setQueryData(queryKeys.quote.detail('GOOG'), {
      symbol: 'GOOG',
      source: 'ginlix-data',
      price: 100,
      regular_close: 100,
      previous_close: 100,
      as_of: T_prev_close,
      currency: 'USD',
    });

    writeQuoteFromWs(qc, 'GOOG', {
      price: 102,
      change: 2,
      changePercent: 2.0,
      timestamp: T_830,
    });
    expect(qc.getQueryData<QuoteRow>(queryKeys.quote.detail('GOOG'))).toMatchObject({
      price: 102,
      last_minute_close: 102,
      live_tick_at: T_830,
    });

    const getSpy = vi.spyOn(api, 'get') as unknown as ReturnType<typeof vi.fn<typeof api.get>>;
    getSpy.mockResolvedValueOnce({
      data: {
        snapshots: [
          {
            symbol: 'GOOG',
            name: 'Alphabet Inc.',
            source: 'yfinance',
            price: 100,
            regular_close: 100,
            previous_close: 98,
            change: 2,
            change_percent: 2.0408,
            regular_trading_change: 2,
            regular_trading_change_percent: 2.0408,
            early_trading_change: null,
            early_trading_change_percent: null,
            late_trading_change: null,
            late_trading_change_percent: null,
            last_trade_at: null,
            last_minute_close: null,
            as_of: T_831,
            regular_as_of: null,
            regular_only: true,
            currency: 'USD',
          },
        ],
      },
    } as Awaited<ReturnType<typeof api.get>>);

    const req = batcher.request('GOOG');
    await vi.advanceTimersByTimeAsync(60);
    const afterYahooRegular = await req;
    expect(afterYahooRegular).toMatchObject({
      name: 'Alphabet Inc.',
      price: 102,
      last_minute_close: 102,
      live_tick_at: T_830,
    });

    // 2. SnapshotData-serialized Yahoo extended-hours pre-market row (8:31) keeps regular_only: false:
    //    its price (yesterday's close 100) is timestamped by regular_as_of (yesterday 16:00), NOT 8:31 as_of,
    //    so it never overwrites the 8:30 WS trade (102) with 100.
    const serializedYahooPm: QuoteRow = {
      symbol: 'GOOG',
      source: 'yfinance',
      price: 100,
      regular_close: 100,
      previous_close: 100,
      last_trade_price: 101,
      last_minute_close: null,
      last_trade_at: null,
      early_trading_change: 1,
      early_trading_change_percent: 1.0,
      regular_trading_change: 2,
      regular_trading_change_percent: 2.0408,
      as_of: T_831,
      regular_as_of: T_prev_close,
      regular_only: false,
      market_status: 'early_trading',
      currency: 'USD',
    };
    expect(extractSelectedPriceTradeTsMs(serializedYahooPm, serializedYahooPm.price)).toBe(T_prev_close);
    const mergedWithYahooPm = mergeRestQuoteRow(afterYahooRegular, serializedYahooPm);
    expect(mergedWithYahooPm.price).toBe(102);
    expect(mergedWithYahooPm.live_tick_at).toBe(T_830);

    // 3. Cached Yahoo regular_only snapshot (as_of = 8:31) does not block an incoming 8:30 WS trade (102).
    const qc2 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc2.setQueryData(queryKeys.quote.detail('GOOG'), {
      symbol: 'GOOG',
      source: 'yfinance',
      price: 100,
      regular_close: 100,
      previous_close: 98,
      as_of: T_831,
      regular_only: true,
      currency: 'USD',
    });
    writeQuoteFromWs(qc2, 'GOOG', {
      price: 102,
      change: 2,
      changePercent: 2.0,
      timestamp: T_830,
    });
    expect(qc2.getQueryData<QuoteRow>(queryKeys.quote.detail('GOOG'))).toMatchObject({
      price: 102,
      last_minute_close: 102,
      live_tick_at: T_830,
      regular_only: null,
    });
  });

  it('r7: core-install Yahoo and FMP unprinted pre-market fallbacks use latest regular close (100) instead of prior-day previous_close (98)', () => {
    const T_prev_close = 1_792_612_800_000;
    const preStatus = { market: 'extended-hours', earlyHours: true, afterHours: false };

    // Real Yahoo pre-market fallback shape from extended_hours.py (base_snap when no PM print yet):
    // price = yesterday close (100), regular_close = 100, previous_close = day-before-yesterday close (98).
    const yahooFallback: QuoteRow = {
      symbol: 'GOOG',
      source: 'yfinance',
      price: 100,
      regular_close: 100,
      previous_close: 98,
      change: 2,
      change_percent: 2.0408,
      regular_trading_change: 2,
      regular_trading_change_percent: 2.0408,
      early_trading_change: null,
      early_trading_change_percent: null,
      late_trading_change: null,
      late_trading_change_percent: null,
      last_trade_price: null,
      last_minute_close: null,
      last_trade_at: null,
      market_status: null,
      as_of: T_prev_close,
      regular_as_of: T_prev_close,
      regular_only: true,
      currency: 'USD',
    };

    const derivedYahoo = deriveQuoteDisplay(snapshotToStockPrice('GOOG', yahooFallback), preStatus);
    expect(derivedYahoo.session).toBe('pre');
    expect(derivedYahoo.mainPrice).toBe(100);
    expect(derivedYahoo.mainChange).toBe(2);
    expect(derivedYahoo.mainChangePct).toBeCloseTo(2.0408, 4);
    expect(derivedYahoo.extPrice).toBeNull();

    const resolvedYahoo = resolveExtendedHoursDisplay(yahooFallback, preStatus, {
      symbol: 'GOOG',
      locale: 'en-US',
    });
    expect(resolvedYahoo.session).toBe('pre');
    expect(resolvedYahoo.mainPrice).toBe(100);
    expect(resolvedYahoo.mainChangePct).toBeCloseTo(2.0408, 4);
    expect(resolvedYahoo.ext).toBeNull();

    // FMP regular_only pre-market fallback (no regular_close / regular_trading_change fields):
    const fmpFallback: QuoteRow = {
      symbol: 'GOOG',
      source: 'fmp',
      price: 100,
      previous_close: 98,
      change: 2,
      change_percent: 2.0408,
      early_trading_change_percent: null,
      late_trading_change_percent: null,
      market_status: null,
      as_of: T_prev_close,
      regular_only: true,
      currency: 'USD',
    };
    const derivedFmp = deriveQuoteDisplay(snapshotToStockPrice('GOOG', fmpFallback), preStatus);
    expect(derivedFmp.session).toBe('pre');
    expect(derivedFmp.mainPrice).toBe(100);
    expect(derivedFmp.mainChange).toBe(2);
    expect(derivedFmp.mainChangePct).toBeCloseTo(2.0408, 4);
    expect(derivedFmp.extPrice).toBeNull();
  });
});
