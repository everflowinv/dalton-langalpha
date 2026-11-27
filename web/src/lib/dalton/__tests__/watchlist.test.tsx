import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { getQuoteBatcher } from '@/lib/quotes/quoteBatcher';
import { deriveQuoteDisplay, type QuoteRow } from '@/lib/quotes';
import { resolveExtendedHoursDisplay } from '@/components/dalton/ExtendedHours';
import {
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
});
