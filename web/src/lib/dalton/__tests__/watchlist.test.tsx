import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/api/client';
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
    price: 100,
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

  it('mergeQuotesIntoWatchlistMetrics hydrates price/chg_1d before /metrics returns and defers to /metrics when both exist', () => {
    const companies: PersonalWatchlistCompany[] = [
      { ticker: 'NVDA', name: 'NVIDIA', tier: 'core', is_private: false },
      { ticker: '0700.HK', name: 'Tencent', tier: 'watch', is_private: false },
      { ticker: 'AAPL', name: 'Apple', tier: 'core', is_private: false },
      { ticker: 'OPENAI', name: 'OpenAI', tier: 'core', is_private: true },
    ];

    // Before /metrics returns: useQuotes populates price, chg_1d, currency immediately
    const beforeMetrics = mergeQuotesIntoWatchlistMetrics(companies, {}, {
      NVDA: { symbol: 'NVDA', price: 142.5, change_percent: 3.15, currency: 'USD' },
      '0700.HK': { symbol: '0700.HK', price: null, previous_close: 400, currency: 'HKD' },
      AAPL: { symbol: 'AAPL', price: 210, previous_close: 200, currency: 'USD' },
      OPENAI: { symbol: 'OPENAI', price: 999, change_percent: 10, currency: 'USD' },
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

    // Once /metrics returns for NVDA, /metrics values win over useQuotes to avoid number jumps;
    // warming row with price: null still falls back to useQuotes
    const afterMetrics = mergeQuotesIntoWatchlistMetrics(
      companies,
      {
        NVDA: makeRow('NVDA', { price: 138.4, chg_1d: 2.45, rsi_14d: 64.2 }),
        '0700.HK': makeRow('0700.HK', {
          status: 'warming',
          price: null,
          chg_1d: null,
          currency: null,
          rsi_14d: null,
        }),
      },
      {
        NVDA: { symbol: 'NVDA', price: 149.99, change_percent: 6.5, currency: 'USD' },
        '0700.HK': { symbol: '0700.HK', price: 412, change_percent: 1.8, currency: 'HKD' },
      },
    );

    expect(afterMetrics.NVDA?.price).toBe(138.4);
    expect(afterMetrics.NVDA?.chg_1d).toBe(2.45);
    expect(afterMetrics.NVDA?.rsi_14d).toBe(64.2);
    expect(afterMetrics['0700.HK']?.price).toBe(412);
    expect(afterMetrics['0700.HK']?.chg_1d).toBe(1.8);
    expect(afterMetrics['0700.HK']?.currency).toBe('HKD');
  });
});
