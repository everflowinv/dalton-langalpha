import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/api/client';
import {
  mergeWatchlistMetricsResponse,
  usePersonalWatchlistMetrics,
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
});
