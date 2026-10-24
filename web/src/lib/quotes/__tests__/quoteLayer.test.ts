/**
 * Quote layer core contracts: coalescing batcher (one HTTP call per window,
 * per-symbol fan-out, in-flight dedup, null-for-missing) and the WS
 * write-through guard (merge live fields, never seed unwatched symbols).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('@/lib/quotes/snapshotApi', () => ({
  getSnapshotStocks: vi.fn(),
  getSnapshotIndexes: vi.fn(),
}));

import { getSnapshotStocks, getSnapshotIndexes } from '@/lib/quotes/snapshotApi';
import { getQuoteBatcher, QuoteBatcher } from '../quoteBatcher';
import { snapshotToStockPrice } from '../quoteAdapters';
import { writeQuoteFromWs } from '../quoteWriteThrough';
import { deriveQuoteDisplay } from '@/lib/dalton/quotes/deriveQuoteDisplay'; // DALTON-HOOK(extended-hours-quotes)

const mockGetSnapshotStocks = getSnapshotStocks as Mock;
const mockGetSnapshotIndexes = getSnapshotIndexes as Mock;

describe('QuoteBatcher', () => {
  let client: QueryClient;
  let batcher: QuoteBatcher;

  beforeEach(() => {
    vi.clearAllMocks();
    client = new QueryClient();
    batcher = getQuoteBatcher(client);
  });

  afterEach(() => {
    client.clear();
  });

  it('coalesces same-window requests into one batched call and fans out per symbol', async () => {
    mockGetSnapshotStocks.mockResolvedValue({
      snapshots: [
        { symbol: 'AAPL', price: 100, change: 1 },
        { symbol: 'MSFT', price: 200, change: -2 },
      ],
    });

    const [a, m] = await Promise.all([batcher.request('AAPL'), batcher.request('msft ')]);

    expect(mockGetSnapshotStocks).toHaveBeenCalledTimes(1);
    expect(mockGetSnapshotStocks).toHaveBeenCalledWith(['AAPL', 'MSFT']);
    expect(a?.price).toBe(100);
    expect(m?.price).toBe(200);
    // Fan-out: both rows land in the shared per-symbol cache entries.
    expect(client.getQueryData(queryKeys.quote.detail('AAPL'))).toMatchObject({ price: 100 });
    expect(client.getQueryData(queryKeys.quote.detail('MSFT'))).toMatchObject({ price: 200 });
  });

  it('dedupes concurrent requests for the same symbol into one in-flight promise', async () => {
    mockGetSnapshotStocks.mockResolvedValue({ snapshots: [{ symbol: 'AAPL', price: 100 }] });

    const p1 = batcher.request('AAPL');
    const p2 = batcher.request('AAPL');
    expect(p2).toBe(p1);
    await p1;
    expect(mockGetSnapshotStocks).toHaveBeenCalledTimes(1);
  });

  it('lands a row under every spelling it answers, and warms the shown one', async () => {
    // One listing asked two ways; the backend shows it once and names both.
    mockGetSnapshotStocks.mockResolvedValue({
      snapshots: [{ symbol: '600519.SH', requested: ['600519.SS', '600519.SH'], price: 1 }],
    });

    const [ss, sh] = await Promise.all([batcher.request('600519.ss'), batcher.request('600519.SH')]);
    expect(ss?.symbol).toBe('600519.SH');
    expect(sh?.price).toBe(1);

    client.clear();
    mockGetSnapshotStocks.mockResolvedValue({
      snapshots: [{ symbol: '600519.SH', requested: ['600519.SS'], price: 2 }],
    });
    await batcher.request('600519.SS');
    expect(client.getQueryData(queryKeys.quote.detail('600519.SS'))).toMatchObject({ price: 2 });
    expect(client.getQueryData(queryKeys.quote.detail('600519.SH'))).toMatchObject({ price: 2 });
  });

  it('resolves dropped/unknown symbols to null, never throws', async () => {
    mockGetSnapshotStocks.mockResolvedValue({ snapshots: [{ symbol: 'AAPL', price: 100 }] });

    const [known, unknown] = await Promise.all([
      batcher.request('AAPL'),
      batcher.request('ZZZFAKE'),
    ]);
    expect(known?.price).toBe(100);
    expect(unknown).toBeNull();
    expect(client.getQueryData(queryKeys.quote.detail('ZZZFAKE'))).toBeNull();
  });

  it('routes indexes through the index endpoint and strips the caret spelling', async () => {
    mockGetSnapshotIndexes.mockResolvedValue({ snapshots: [{ symbol: 'GSPC', price: 5000 }] });

    const [a, b] = await Promise.all([
      batcher.request('^GSPC', { isIndex: true }),
      batcher.request('GSPC', { isIndex: true }),
    ]);
    expect(mockGetSnapshotIndexes).toHaveBeenCalledTimes(1);
    expect(mockGetSnapshotIndexes).toHaveBeenCalledWith(['GSPC']);
    expect(mockGetSnapshotStocks).not.toHaveBeenCalled();
    expect(a).toBe(b);
  });

  it('resolves every pending symbol to null when the batch call throws', async () => {
    mockGetSnapshotStocks.mockRejectedValue(new Error('network'));
    await expect(batcher.request('AAPL')).resolves.toBeNull();
  });

  it('binds one batcher per QueryClient', () => {
    expect(getQuoteBatcher(client)).toBe(batcher);
    const other = new QueryClient();
    expect(getQuoteBatcher(other)).not.toBe(batcher);
  });
});

describe('writeQuoteFromWs', () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient();
  });

  it('merges live fields into an existing entry, preserving REST-authored fields', () => {
    const key = queryKeys.quote.detail('AAPL');
    client.setQueryData(key, {
      symbol: 'AAPL', price: 100, change: 1, change_percent: 1.0,
      previous_close: 99, name: 'Apple Inc',
    });

    writeQuoteFromWs(client, 'AAPL', { price: 101.5, change: 2.5, changePercent: 2.53 });

    expect(client.getQueryData(key)).toMatchObject({
      price: 101.5,
      change: 2.5,
      change_percent: 2.53,
      previous_close: 99,
      name: 'Apple Inc',
    });
  });

  it('never seeds an entry nobody is watching', () => {
    writeQuoteFromWs(client, 'TSLA', { price: 300, change: 1, changePercent: 0.3 });
    expect(client.getQueryData(queryKeys.quote.detail('TSLA'))).toBeUndefined();
  });
});

describe('snapshotToStockPrice', () => {
  it('rounds and maps an available quote', () => {
    expect(
      snapshotToStockPrice('AAPL', {
        symbol: 'AAPL', price: 100.456, change: -1.234, change_percent: -1.216,
        previous_close: 101.69,
      }),
    ).toMatchObject({
      symbol: 'AAPL',
      price: 100.46,
      // DALTON-HOOK(extended-hours-quotes): Adapter preserves raw change and changePercent precision for display-time rounding.
      change: -1.234,
      changePercent: -1.216,
      isPositive: false,
      quoteAvailable: true,
      previousClose: 101.69,
    });
  });

  it('keeps a CN fund price at its 3-decimal tick and a stock at cents', () => {
    expect(
      snapshotToStockPrice('510300.SH', {
        symbol: '510300.SH', price: 3.912, change: 0.0123, asset_class: 'fund', currency: 'CNY',
      }),
    ).toMatchObject({ price: 3.912, change: 0.0123 });
    expect(
      snapshotToStockPrice('600519.SH', {
        symbol: '600519.SH', price: 1500.5, asset_class: 'equity', currency: 'CNY',
      }).price,
    ).toBe(1500.5);
  });

  it('marks a missing quote unavailable with zeroed display fields', () => {
    expect(snapshotToStockPrice('MSFT', null)).toEqual({
      symbol: 'MSFT', price: 0, change: 0, changePercent: 0,
      isPositive: true, quoteAvailable: false,
    });
  });

  // DALTON-HOOK(extended-hours-quotes): Verify extended-hours and regular-close fields are carried without deriving regularTradingChangePercent from previousClose.
  it('carries extended-hours and regular-session fields for GOOG late_trading snapshot', () => {
    const adapted = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change: -2.66,
      change_percent: -0.8293,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change: 0.82,
      late_trading_change_percent: 0.2584,
      early_trading_change: null,
      early_trading_change_percent: null,
      last_minute_close: null,
      last_trade_price: 318.1,
      market_status: 'late_trading',
    });
    expect(adapted).toMatchObject({
      symbol: 'GOOG',
      price: 317.28,
      regularClose: 317.28,
      previousClose: 320.76,
      regularTradingChange: -3.48,
      regularTradingChangePercent: -1.0849,
      lateTradingChange: 0.82,
      lateTradingChangePercent: 0.2584,
      earlyTradingChange: null,
      earlyTradingChangePercent: null,
      lastMinuteClose: null,
      lastTradePrice: 318.1,
    });

    const withoutRegPct = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 320.76,
      regular_close: 320.76,
      previous_close: 320.76,
      regular_trading_change: -3.46,
      regular_trading_change_percent: null,
      market_status: 'early_trading',
    });
    expect(withoutRegPct.regularTradingChangePercent).toBeNull();
  });
});

// DALTON-HOOK(extended-hours-quotes): Unit tests for deriveQuoteDisplay across regular/pre/post × with/without live tick.
describe('deriveQuoteDisplay', () => {
  const openStatus = { market: 'open', afterHours: false, earlyHours: false };
  const preStatus = { market: 'extended-hours', afterHours: false, earlyHours: true };
  const postStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };

  it('derives regular, pre-market, and after-hours quotes with and without a live tick', () => {
    // 1. Regular session without tick vs with tick
    const regRow = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 317.28,
      change: -3.48,
      change_percent: -1.0849,
      previous_close: 320.76,
    });
    expect(deriveQuoteDisplay(regRow, openStatus)).toEqual({
      session: 'regular',
      mainPrice: 317.28,
      mainChange: -3.48,
      mainChangePct: -1.0849,
      extPrice: null,
      extChange: null,
      extChangePct: null,
      extAnchor: null,
    });
    expect(
      deriveQuoteDisplay(regRow, openStatus, {
        price: 325.0,
        change: 4.24,
        changePercent: 1.32,
        timestamp: 1700000000000,
      }),
    ).toEqual({
      session: 'regular',
      mainPrice: 325.0,
      mainChange: 4.24,
      mainChangePct: 1.32,
      extPrice: null,
      extChange: null,
      extChangePct: null,
      extAnchor: null,
    });

    // DALTON-HOOK(extended-hours-quotes): 2. Pre-market without tick vs with tick.
    const preRow = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 320.76,
      previous_close: 320.76,
      regular_close: 320.76,
      change: 1.5,
      change_percent: 0.4676,
      regular_trading_change: -3.46,
      regular_trading_change_percent: -1.0672,
      early_trading_change: 1.5,
      early_trading_change_percent: 0.4676,
      market_status: 'early_trading',
    });
    const preNoTick = deriveQuoteDisplay(preRow, preStatus);
    expect(preNoTick.session).toBe('pre');
    expect(preNoTick.mainPrice).toBe(320.76);
    expect(preNoTick.mainChange).toBe(-3.46);
    expect(preNoTick.mainChangePct).toBeCloseTo(-1.0672, 4);
    expect(preNoTick.extPrice).toBe(322.26);
    expect(preNoTick.extChange).toBeCloseTo(1.5, 4);
    expect(preNoTick.extChangePct).toBeCloseTo(0.4676, 4);
    expect(preNoTick.extAnchor).toBe(320.76);

    const preWithTick = deriveQuoteDisplay(preRow, preStatus, {
      price: 323.97,
      timestamp: 1700000001000,
    });
    expect(preWithTick.session).toBe('pre');
    expect(preWithTick.mainPrice).toBe(320.76);
    expect(preWithTick.mainChange).toBe(-3.46);
    expect(preWithTick.mainChangePct).toBeCloseTo(-1.0672, 4);
    expect(preWithTick.extPrice).toBe(323.97);
    expect(preWithTick.extChange).toBeCloseTo(3.21, 4);
    expect(preWithTick.extChangePct).toBeCloseTo((3.21 / 320.76) * 100, 4);

    // DALTON-HOOK(extended-hours-quotes): 3. After-hours without tick vs with tick.
    const postRow = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change: -2.66,
      change_percent: -0.83,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change: 0.82,
      late_trading_change_percent: 0.2584,
      market_status: 'late_trading',
    });
    const postNoTick = deriveQuoteDisplay(postRow, postStatus);
    expect(postNoTick.session).toBe('post');
    expect(postNoTick.mainPrice).toBe(317.28);
    expect(postNoTick.mainChange).toBeCloseTo(-3.48, 4);
    expect(postNoTick.mainChangePct).toBeCloseTo(-1.0849, 3);
    expect(postNoTick.extPrice).toBe(318.1);
    expect(postNoTick.extChange).toBeCloseTo(0.82, 4);
    expect(postNoTick.extChangePct).toBeCloseTo(0.2584, 4);
    expect(postNoTick.extAnchor).toBe(317.28);

    const postWithTick = deriveQuoteDisplay(postRow, postStatus, {
      price: 319.5,
      timestamp: 1700000002000,
    });
    expect(postWithTick.session).toBe('post');
    expect(postWithTick.mainPrice).toBe(317.28);
    expect(postWithTick.mainChange).toBeCloseTo(-3.48, 4);
    expect(postWithTick.extPrice).toBe(319.5);
    expect(postWithTick.extChange).toBeCloseTo(2.22, 4);
    expect(postWithTick.extChangePct).toBeCloseTo((2.22 / 317.28) * 100, 4);
  });

  // DALTON-HOOK(extended-hours-quotes): Preserve null semantics when regular fields are missing and ignore REST row.timestamp.
  it('preserves null semantics for missing regular fields and ignores REST row.timestamp', () => {
    // Pre-market without regular_trading_change / regular_trading_change_percent must not fall back to row.change / row.changePercent
    const preMissingReg = deriveQuoteDisplay(
      {
        symbol: 'GOOG',
        price: 322.26,
        previous_close: 320.76,
        change: 1.5,
        change_percent: 0.4676,
        early_trading_change: 1.5,
        early_trading_change_percent: 0.4676,
      },
      preStatus,
    );
    expect(preMissingReg.session).toBe('pre');
    expect(preMissingReg.mainPrice).toBe(320.76);
    expect(preMissingReg.mainChange).toBeNull();
    expect(preMissingReg.mainChangePct).toBeNull();

    // After-hours without regular_close (and without regular_trading_change) must keep mainPrice null, not prevClose
    const postMissingRegClose = deriveQuoteDisplay(
      {
        symbol: 'GOOG',
        price: 318.1,
        previous_close: 320.76,
        change: -2.66,
        change_percent: -0.83,
        late_trading_change_percent: 0.2584,
      },
      postStatus,
    );
    expect(postMissingRegClose.session).toBe('post');
    expect(postMissingRegClose.mainPrice).toBeNull();
    expect(postMissingRegClose.mainChange).toBeNull();
    expect(postMissingRegClose.mainChangePct).toBeNull();
    expect(postMissingRegClose.extPrice).toBeNull();
    expect(postMissingRegClose.extChange).toBeNull();
    expect(postMissingRegClose.extChangePct).toBeNull();
    expect(postMissingRegClose.extAnchor).toBeNull();

    // REST snapshot carrying `timestamp` (without live_tick_at / liveTickAt) must NOT override extPrice with row.price
    const restSnapWithTimestamp = deriveQuoteDisplay(
      {
        symbol: 'GOOG',
        price: 317.28,
        regular_close: 317.28,
        previous_close: 320.76,
        regular_trading_change: -3.48,
        late_trading_change: 0.82,
        late_trading_change_percent: 0.2584,
        timestamp: 1700000000000,
      } as never,
      postStatus,
    );
    expect(restSnapWithTimestamp.extPrice).toBe(318.1);
    expect(restSnapWithTimestamp.extChange).toBeCloseTo(0.82, 4);
    expect(restSnapWithTimestamp.extChangePct).toBeCloseTo(0.2584, 4);
  });

  // DALTON-HOOK(extended-hours-quotes): When live tick price equals settledClose, do not revert to stale after-hours price.
  it('keeps live tick price when it returns to settledClose instead of reverting to stale extended-hours price', () => {
    const client = new QueryClient();
    const key = queryKeys.quote.detail('GOOG');
    client.setQueryData(key, {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change: -2.66,
      change_percent: -0.83,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change: 0.82,
      late_trading_change_percent: 0.2584,
      market_status: 'late_trading',
    });

    // Live WS tick lands exactly back at regular_close (317.28)
    writeQuoteFromWs(client, 'GOOG', {
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      timestamp: 1700000005000,
    });

    const updatedSnap = client.getQueryData<Record<string, unknown>>(key)!;
    expect(updatedSnap.live_tick_at).toBe(1700000005000);

    const adapted = snapshotToStockPrice('GOOG', updatedSnap as never);
    const derived = deriveQuoteDisplay(adapted, postStatus);
    expect(derived.extPrice).toBe(317.28);
    expect(derived.extChange).toBe(0);
    expect(derived.extChangePct).toBe(0);
  });
});
