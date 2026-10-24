import { describe, it, expect } from 'vitest';
import { getExtendedHoursInfo } from '@/lib/marketUtils';
import { quoteCurrency } from '@/lib/bars';
import { resolveDualName } from '@/lib/displayName';
import { isLiveRow, resolveFreshnessBadge } from '@/lib/freshness';
import { isUSEquity } from '../../utils/chartConstants';
import type { PriceUpdate } from '../useMarketDataWS';
import { deriveStockQuote, quoteDotState, type ChangeTone, type QuoteStatus, type StockQuoteInputs, type StockQuoteModel } from '../useStockQuoteModel';

// DALTON-HOOK(extended-hours-quotes): Verbatim pre-refactor deriveStockQuote from .agy/legacy_useStockQuoteModel.ts (only function name changed).
function toneOf(n: number | null | undefined): ChangeTone {
  if (n == null || n === 0) return '';
  return n > 0 ? 'positive' : 'negative';
}

function legacyDeriveStockQuote({
  symbol, stockInfo, realTimePrice, quoteData, snapshot, marketStatus, wsStatus, wsHasData = false, marketPhase = null, displayOverride = null,
}: StockQuoteInputs): StockQuoteModel {
  const price = realTimePrice?.price ?? stockInfo?.Price ?? null;
  const change = realTimePrice?.change ?? null;
  const changePercent = realTimePrice?.changePercent ?? null;
  const tone = toneOf(change);
  const previousClose = snapshot?.previous_close ?? quoteData?.previousClose ?? null;

  // Extended hours (market convention): the big number is the last official
  // close — today's regular close after-hours, the previous close pre-market —
  // with a coherent change pair against the previous close; the extended move
  // renders on its own against its declared anchor. A live tick (has a
  // timestamp; quote rows don't) overrides the derived ext price.
  // DALTON-HOOK(extended-hours-quotes): Exact legacy extended-hours block from .agy/legacy_useStockQuoteModel.ts.
  const { extPct, extType, extPrice, extChange, extAnchor, regularClose } = getExtendedHoursInfo(marketStatus, snapshot);
  const tickAt = (realTimePrice as PriceUpdate | null)?.timestamp ?? null;
  const tickPrice = tickAt != null ? (realTimePrice?.price ?? null) : null;
  const extDisplayPrice = tickPrice ?? extPrice;
  const extDisplayChange = tickPrice != null && extAnchor != null ? tickPrice - extAnchor : extChange;
  const extDisplayPct = tickPrice != null && extAnchor ? ((tickPrice - extAnchor) / extAnchor) * 100 : extPct;
  const settledClose = (extType === 'post' ? regularClose : previousClose) ?? null;
  const settledChange = extType === 'post' && regularClose != null && previousClose != null ? regularClose - previousClose : null;
  const settledChangePct = settledChange != null && previousClose ? (settledChange / previousClose) * 100 : null;

  const ext = extType && settledClose != null && extDisplayPrice != null && extDisplayPct != null
    ? {
        type: extType,
        price: extDisplayPrice,
        change: extDisplayChange ?? null,
        pct: extDisplayPct,
        settledClose,
        settledChange,
        settledChangePct,
        settledTone: toneOf(settledChange),
      }
    : null;

  const headline = ext
    ? { price: ext.settledClose, change: ext.settledChange, pct: ext.settledChangePct, tone: ext.settledTone }
    : { price, change, pct: changePercent, tone };

  const isLive = wsStatus === 'connected' && isUSEquity(symbol) && wsHasData;
  const status: QuoteStatus = isLive ? 'live' : marketPhase === 'closed' ? 'closed' : 'delayed';
  const providers = (marketStatus?.providers ?? []) as string[];
  const activeSource = isLive ? 'ginlix-data' : (snapshot?.source ?? null);
  const dataSources = activeSource ? [activeSource] : providers;

  const averageVolume = quoteData?.avgVolume ?? stockInfo?.AverageVolume ?? null;
  const volume = stockInfo?.Volume ?? null;

  const nameSource = displayOverride ?? { name: stockInfo?.Name, nameLocal: stockInfo?.NameLocal, nameEn: stockInfo?.NameEn };
  const { primary: displayName, secondary: displaySecondaryName, named: hasName } = resolveDualName(nameSource, symbol);
  const quoteFreshness = snapshot?.freshness ?? null;
  const quoteFields = { tier: snapshot?.tier, freshness: quoteFreshness };

  return {
    price,
    change,
    changePercent,
    tone,
    headline,
    status,
    tickAt,
    previousClose,
    open: realTimePrice?.open ?? stockInfo?.Open ?? null,
    high: realTimePrice?.high ?? stockInfo?.High ?? null,
    low: realTimePrice?.low ?? stockInfo?.Low ?? null,
    fiftyTwoWeekHigh: quoteData?.yearHigh ?? stockInfo?.['52WeekHigh'] ?? null,
    fiftyTwoWeekLow: quoteData?.yearLow ?? stockInfo?.['52WeekLow'] ?? null,
    averageVolume,
    volume,
    shownVolume: volume ?? averageVolume,
    volumeIsAverage: volume == null && averageVolume != null,
    displayName,
    displaySecondaryName,
    hasName,
    displayExchange: displayOverride?.exchange ?? stockInfo?.Exchange ?? '',
    dataSources,
    currency: quoteCurrency(snapshot?.currency, symbol, snapshot?.asset_class),
    quoteBadge: resolveFreshnessBadge(quoteFields),
    quoteIsFresh: isLiveRow(quoteFields),
    asOf: snapshot?.as_of ?? quoteFreshness?.actual_latest ?? null,
    ext,
  };
}

const base: StockQuoteInputs = {
  symbol: 'AMD',
  stockInfo: null,
  realTimePrice: null,
  quoteData: null,
  snapshot: null,
  marketStatus: { providers: [] },
  wsStatus: 'disconnected',
};

const row = { symbol: 'AMD', price: 101.23, open: 100, high: 102, low: 99, change: 1.23, changePercent: 1.23, volume: 0, previousClose: 100 };

describe('deriveStockQuote', () => {
  it('keeps a missing change pair null rather than zero', () => {
    const q = deriveStockQuote({ ...base, stockInfo: { Symbol: 'AMD', Name: 'AMD', Price: 101.23 } as never });
    expect(q.price).toBe(101.23);
    expect(q.change).toBeNull();
    expect(q.changePercent).toBeNull();
    expect(q.tone).toBe('');
    expect(q.headline).toEqual({ price: 101.23, change: null, pct: null, tone: '' });
  });

  it('headlines the row price in a regular session', () => {
    const q = deriveStockQuote({
      ...base,
      marketStatus: { market: 'open', afterHours: false, earlyHours: false, providers: [] },
      realTimePrice: row,
    });
    expect(q.ext).toBeNull();
    expect(q.headline).toEqual({ price: 101.23, change: 1.23, pct: 1.23, tone: 'positive' });
  });

  it('headlines the settled close after hours and keeps the session move in ext', () => {
    const q = deriveStockQuote({
      ...base,
      marketStatus: { market: 'closed', afterHours: false, earlyHours: false, providers: [] },
      snapshot: { symbol: 'AMD', price: 96.5, previous_close: 100, regular_trading_change: -4, late_trading_change_percent: -1, source: 'x' },
      realTimePrice: { ...row, price: 96.5, change: -3.5, changePercent: -3.5 },
    });
    expect(q.ext?.type).toBe('post');
    expect(q.headline.price).toBe(96);
    expect(q.headline.change).toBe(-4);
    expect(q.headline.pct).toBe(-4);
    expect(q.headline.tone).toBe('negative');
  });

  // DALTON-HOOK(extended-hours-quotes): Lock pre-market settled change and after-hours invariance in deriveStockQuote.
  it('headlines previous close and prior session regular change in pre-market for GOOG', () => {
    const preQ = deriveStockQuote({
      ...base,
      symbol: 'GOOG',
      marketStatus: { market: 'extended-hours', afterHours: false, earlyHours: true, providers: [] },
      snapshot: {
        symbol: 'GOOG',
        price: 320.76,
        previous_close: 320.76,
        regular_close: 320.76,
        regular_trading_change: -3.46,
        regular_trading_change_percent: -1.0672,
        early_trading_change: 1.5,
        early_trading_change_percent: 0.4676,
        market_status: 'early_trading',
      },
      realTimePrice: { ...row, symbol: 'GOOG', price: 320.76, change: 1.5, changePercent: 0.47, previousClose: 320.76 },
    });
    expect(preQ.ext?.type).toBe('pre');
    expect(preQ.headline.price).toBe(320.76);
    expect(preQ.headline.change).toBe(-3.46);
    expect(preQ.headline.pct).toBeCloseTo(-1.0672, 4);
    expect(preQ.headline.tone).toBe('negative');
    expect(preQ.ext?.price).toBe(322.26);
    expect(preQ.ext?.pct).toBeCloseTo(0.4676, 4);

    // DALTON-HOOK(extended-hours-quotes): Verify after-hours GOOG derivation remains unchanged.
    const postQ = deriveStockQuote({
      ...base,
      symbol: 'GOOG',
      marketStatus: { market: 'extended-hours', afterHours: true, earlyHours: false, providers: [] },
      snapshot: {
        symbol: 'GOOG',
        price: 317.28,
        regular_close: 317.28,
        previous_close: 320.76,
        regular_trading_change: -3.48,
        late_trading_change_percent: 0.2584,
        market_status: 'late_trading',
      },
      realTimePrice: { ...row, symbol: 'GOOG', price: 317.28, change: -2.66, changePercent: -0.83, previousClose: 320.76 },
    });
    expect(postQ.ext?.type).toBe('post');
    expect(postQ.headline.price).toBe(317.28);
    expect(postQ.headline.change).toBeCloseTo(-3.48, 2);
    expect(postQ.headline.pct).toBeCloseTo(-1.0849, 3);
    expect(postQ.headline.tone).toBe('negative');
    expect(postQ.ext?.price).toBe(318.1);
    expect(postQ.ext?.pct).toBeCloseTo(0.2584, 4);
  });

  it('status: a live feed wins, a closed phase reads closed, anything else delayed', () => {
    expect(deriveStockQuote({ ...base, wsStatus: 'connected', wsHasData: true, marketPhase: 'closed' }).status).toBe('live');
    expect(deriveStockQuote({ ...base, marketPhase: 'closed' }).status).toBe('closed');
    expect(deriveStockQuote({ ...base, marketPhase: 'post' }).status).toBe('delayed');
    expect(deriveStockQuote({ ...base }).status).toBe('delayed');
    // Only a US equity can be live off the WS feed.
    expect(deriveStockQuote({ ...base, symbol: '0700.HK', wsStatus: 'connected', wsHasData: true }).status).toBe('delayed');
  });

  it('carries the live tick time only when the row is a tick', () => {
    expect(deriveStockQuote({ ...base, realTimePrice: row }).tickAt).toBeNull();
    expect(deriveStockQuote({ ...base, realTimePrice: { ...row, timestamp: 1700000000000 } }).tickAt).toBe(1700000000000);
  });

  // DALTON-HOOK(extended-hours-quotes): Assert field-for-field parity against verbatim legacyDeriveStockQuote across all edge cases (with and without quoteData).
  it('matches legacy pre-refactor deriveStockQuote field-for-field across missing fields, sub-cent prices, negative half-way values, and quoteData fallback', () => {
    const preStatus = { market: 'extended-hours', afterHours: false, earlyHours: true, providers: [] };
    const postStatus = { market: 'extended-hours', afterHours: true, earlyHours: false, providers: [] };
    const openStatus = { market: 'open', afterHours: false, earlyHours: false, providers: [] };

    const baseCases: StockQuoteInputs[] = [
      // 1. Pre-market missing regular_trading_change and regular_trading_change_percent -> settledChange: null, settledChangePct: null
      {
        ...base,
        symbol: 'GOOG',
        marketStatus: preStatus,
        snapshot: {
          symbol: 'GOOG',
          price: 322.26,
          previous_close: 320.76,
          change: 1.5,
          change_percent: 0.4676,
          early_trading_change: 1.5,
          early_trading_change_percent: 0.4676,
        },
        realTimePrice: { ...row, symbol: 'GOOG', price: 322.26, change: 1.5, changePercent: 0.4676, previousClose: 320.76 },
      },
      // 2. Pre-market with regular_trading_change present (the ONLY intentional difference from legacy: prior-session change shown in headline/ext)
      {
        ...base,
        symbol: 'GOOG',
        marketStatus: preStatus,
        snapshot: {
          symbol: 'GOOG',
          price: 322.26,
          previous_close: 320.76,
          regular_trading_change: -3.46,
          early_trading_change: 1.5,
          early_trading_change_percent: 0.4676,
        },
        realTimePrice: { ...row, symbol: 'GOOG', price: 322.26, change: 1.5, changePercent: 0.4676, previousClose: 320.76 },
      },
      // 3. After-hours missing regular_close AND regular_trading_change -> settledClose is null, so ext is null
      {
        ...base,
        symbol: 'GOOG',
        marketStatus: postStatus,
        snapshot: {
          symbol: 'GOOG',
          price: 318.1,
          previous_close: 320.76,
          change: -2.66,
          change_percent: -0.83,
          late_trading_change_percent: 0.2584,
        },
        realTimePrice: { ...row, symbol: 'GOOG', price: 318.1, change: -2.66, changePercent: -0.83, previousClose: 320.76 },
      },
      // 4. Sub-cent prices in after-hours without live tick (legacy uses getExtendedHoursInfo extChange = 0.01)
      {
        ...base,
        symbol: 'PENNY',
        marketStatus: postStatus,
        snapshot: {
          symbol: 'PENNY',
          price: 0.5123,
          regular_close: 0.5045,
          previous_close: 0.5012,
          last_minute_close: 0.5123,
          late_trading_change_percent: 1.5461,
        },
        realTimePrice: { ...row, symbol: 'PENNY', price: 0.5123, change: 0.0111, changePercent: 2.2147, previousClose: 0.5012 },
      },
      // 5. Negative half-way values (-0.125 and -0.125%) with live tick
      {
        ...base,
        symbol: 'HALF',
        marketStatus: postStatus,
        snapshot: {
          symbol: 'HALF',
          price: 99.875,
          regular_close: 99.875,
          previous_close: 100,
          regular_trading_change: -0.125,
          regular_trading_change_percent: -0.125,
          late_trading_change: -0.125,
          late_trading_change_percent: -0.125,
        },
        realTimePrice: { ...row, symbol: 'HALF', price: 99.75015625, change: -0.24984375, changePercent: -0.25, timestamp: 1700000003000 },
      },
      // 6. Regular session with live tick
      {
        ...base,
        symbol: 'GOOG',
        marketStatus: openStatus,
        snapshot: { symbol: 'GOOG', price: 317.28, previous_close: 320.76 },
        realTimePrice: { ...row, symbol: 'GOOG', price: 319.125, change: -1.635, changePercent: -0.5097, timestamp: 1700000004000 },
      },
      // 7. Pre-market missing snapshot.previous_close (price 101, early_trading_change_percent +1%)
      {
        ...base,
        symbol: 'AMD',
        marketStatus: preStatus,
        snapshot: {
          symbol: 'AMD',
          price: 101,
          early_trading_change_percent: 1,
        },
        realTimePrice: { ...row, symbol: 'AMD', price: 101, change: 1, changePercent: 1, previousClose: 100 },
      },
      // 8. After-hours missing snapshot.previous_close (only regular_trading_change: -4, late_trading_change_percent: -1, live price 96.5)
      {
        ...base,
        symbol: 'AMD',
        marketStatus: postStatus,
        snapshot: {
          symbol: 'AMD',
          price: 96.5,
          regular_trading_change: -4,
          late_trading_change_percent: -1,
        },
        realTimePrice: { ...row, symbol: 'AMD', price: 96.5, change: -3.5, changePercent: -3.5, previousClose: 100 },
      },
      // 9. After-hours snapshot with live_tick_at and realTimePrice without timestamp (company page only recognizes realTimePrice.timestamp as tick)
      {
        ...base,
        symbol: 'AMD',
        marketStatus: postStatus,
        snapshot: {
          symbol: 'AMD',
          price: 102,
          regular_close: 100,
          previous_close: 99,
          last_minute_close: 101,
          late_trading_change_percent: 1,
          live_tick_at: 1700000005000,
        },
        realTimePrice: { ...row, symbol: 'AMD', price: 102, change: 3, changePercent: 3.03, previousClose: 99 },
      },
    ];

    const quoteDataVariants: Array<StockQuoteInputs['quoteData']> = [
      null,
      { previousClose: 100, avgVolume: 2_500_000, yearHigh: 400, yearLow: 200 },
    ];

    // DALTON-HOOK(extended-hours-quotes): Run every case both without quoteData and with quoteData.previousClose=100; only the 6 pre-market prior-session fields may differ from legacy.
    for (const qd of quoteDataVariants) {
      for (const baseInput of baseCases) {
        const input: StockQuoteInputs = { ...baseInput, quoteData: qd };
        const actual = deriveStockQuote(input);
        const legacy = legacyDeriveStockQuote(input);

        const isPreWithRegularMove =
          legacy.ext?.type === 'pre' &&
          (input.snapshot?.regular_trading_change != null ||
            input.snapshot?.regular_trading_change_percent != null);

        if (isPreWithRegularMove && legacy.ext) {
          const expectedPreChange = input.snapshot?.regular_trading_change ?? null;
          const prePriorClose =
            expectedPreChange != null ? legacy.ext.settledClose - expectedPreChange : null;
          const expectedPrePct =
            input.snapshot?.regular_trading_change_percent ??
            (expectedPreChange != null && prePriorClose
              ? (expectedPreChange / prePriorClose) * 100
              : null);
          const expectedPreTone = toneOf(expectedPreChange ?? expectedPrePct);

          // Explicitly list the 6 allowed pre-market exception fields:
          // headline.change, headline.pct, headline.tone, ext.settledChange, ext.settledChangePct, ext.settledTone
          expect(actual).toEqual({
            ...legacy,
            headline: {
              ...legacy.headline,
              change: expectedPreChange,
              pct: expectedPrePct,
              tone: expectedPreTone,
            },
            ext: {
              ...legacy.ext,
              settledChange: expectedPreChange,
              settledChangePct: expectedPrePct,
              settledTone: expectedPreTone,
            },
          });
        } else {
          expect(actual).toEqual(legacy);
        }
      }
    }

    // Explicitly verify sub-cent ext.change === 0.01 without tick:
    const subCentQuote = deriveStockQuote(baseCases[3]);
    expect(subCentQuote.ext?.change).toBe(0.01);

    // Explicitly verify snapshot.live_tick_at without realTimePrice.timestamp keeps legacy 101 / +1 / 1%:
    const noTimestampTickQuote = deriveStockQuote(baseCases[8]);
    expect(noTimestampTickQuote.ext).toMatchObject({
      type: 'post',
      price: 101,
      change: 1,
      pct: 1,
    });
  });
});

describe('quoteDotState', () => {
  const dot = (snapshot: StockQuoteInputs['snapshot']) => quoteDotState(deriveStockQuote({ ...base, snapshot }));

  it('keeps a delay neutral and warns only on stale or short-of-close data', () => {
    expect(dot({ symbol: 'AMD', tier: 'delayed_15m' })).toBe('delayed');
    expect(dot({ symbol: 'AMD', tier: 'eod' })).toBe('delayed');
    expect(dot({ symbol: 'AMD', freshness: { label: 'stale', measured: true } })).toBe('warning');
    expect(dot({ symbol: 'AMD', freshness: { label: 'incomplete', measured: true } })).toBe('warning');
  });

  it('reads a current REST print as realtime and leaves the venue states alone', () => {
    expect(dot({ symbol: 'AMD', freshness: { label: 'live', measured: true } })).toBe('realtime');
    expect(quoteDotState(deriveStockQuote({ ...base, marketPhase: 'closed', snapshot: { symbol: 'AMD', freshness: { label: 'stale', measured: true } } }))).toBe('closed');
  });
});
