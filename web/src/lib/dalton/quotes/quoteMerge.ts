/**
 * Unified selected-price trade timestamp extraction and freshness merge rules
 * shared across REST backfill (`QuoteBatcher`), WS write-through (`writeQuoteFromWs`),
 * and `/api/v1/dalton/watchlist/metrics` merging.
 */
import type { QuoteRow } from '@/lib/quotes/quoteBatcher';
import type { WsQuoteFields } from '@/lib/quotes/quoteWriteThrough';

export type PriceSourceKind = 'snapshot' | 'daily_close';

export interface PricedCandidate {
  price: number | null;
  tradeTsMs: number | null;
  priceSource?: PriceSourceKind | null;
}

/** Parse a Unix sec/ms or ISO timestamp into Unix epoch milliseconds. */
export function parsePriceTimestampMs(ts: unknown): number | null {
  if (ts == null || typeof ts === 'boolean') return null;
  if (typeof ts === 'number') {
    if (!Number.isFinite(ts) || ts <= 0) return null;
    return ts < 1e11 ? Math.round(ts * 1000) : Math.round(ts);
  }
  if (typeof ts === 'string') {
    const trimmed = ts.trim();
    if (!trimmed) return null;
    if (/^\d+(\.\d+)?$/.test(trimmed)) {
      const num = Number(trimmed);
      if (!Number.isFinite(num) || num <= 0) return null;
      return num < 1e11 ? Math.round(num * 1000) : Math.round(num);
    }
    const parsed = Date.parse(trimmed);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }
  return null;
}

/**
 * Extract the trade print timestamp (Unix ms) corresponding to the selected price
 * on a `QuoteRow`.
 *
 * - Live WS write-through rows carry `live_tick_at` stamped directly for `price`.
 * - Extended-hours enriched rows (`regular_only === false`) whose `price` is the
 *   regular session close carry its print time in `regular_as_of` (while `as_of`
 *   tracks the extended-hours print).
 * - Ginlix rows separate `last_trade_at` from composite `as_of` (which can advance
 *   on bid/ask `last_quote` updates without any trade). When `last_trade_price` is
 *   present and differs from the selected `price` (e.g. `price` is an earlier
 *   `session.close` while `last_trade` is an odd-lot or extended-hours print),
 *   `last_trade_at` does not stamp `price`; only `regular_as_of` (if known) does.
 * - Other providers (FMP, yfinance regular, CN/tushare) stamp the trade print time
 *   in `as_of` / `timestamp`.
 */
export function extractSelectedPriceTradeTsMs(
  quote: QuoteRow,
  selectedPrice?: number | null,
): number | null {
  const record = quote as Record<string, unknown>;
  const liveTickTs = parsePriceTimestampMs(quote.live_tick_at);
  if (liveTickTs != null) return liveTickTs;

  const rawPrice =
    typeof quote.price === 'number' && Number.isFinite(quote.price) ? quote.price : null;
  const effPrice = selectedPrice !== undefined ? selectedPrice : rawPrice;
  const regularAsOf = parsePriceTimestampMs(quote.regular_as_of ?? record.regular_as_of);

  // Unprinted pre-market row whose displayed price fell back to previous_close:
  // today's composite `as_of` / `last_trade_at` is not previous_close's trade time.
  if (rawPrice == null) {
    return regularAsOf;
  }

  if (regularAsOf != null && record.regular_only === false) {
    return regularAsOf;
  }

  const hasLastTradeKey = 'last_trade_at' in record;
  const isGinlix = quote.source === 'ginlix-data' || hasLastTradeKey;
  if (isGinlix) {
    const lastTradeAt = parsePriceTimestampMs(quote.last_trade_at ?? record.last_trade_at);
    const lastTradePrice =
      typeof quote.last_trade_price === 'number' && Number.isFinite(quote.last_trade_price)
        ? quote.last_trade_price
        : null;
    if (lastTradeAt != null) {
      if (lastTradePrice == null || effPrice == null || Math.abs(effPrice - lastTradePrice) <= 1e-6) {
        return lastTradeAt;
      }
      return regularAsOf;
    }
    return regularAsOf;
  }

  return (
    parsePriceTimestampMs(quote.as_of) ??
    parsePriceTimestampMs(record.timestamp) ??
    parsePriceTimestampMs(record.updated_at) ??
    parsePriceTimestampMs(quote.freshness?.actual_latest)
  );
}

/**
 * Shared freshness decision rule used across REST backfill, WS write-through,
 * and `/metrics` merge.
 *
 * Returns `true` when `incoming` should replace `existing`'s price and 1D change.
 */
export function shouldPreferIncomingPrice(
  existing: PricedCandidate,
  incoming: PricedCandidate,
  tieBreaker: 'incoming' | 'existing' = 'incoming',
): boolean {
  if (incoming.price == null) return false;
  if (existing.price == null) return true;

  const existingIsDaily = existing.priceSource === 'daily_close';
  const incomingIsDaily = incoming.priceSource === 'daily_close';
  if (!existingIsDaily && incomingIsDaily) return false;
  if (existingIsDaily && !incomingIsDaily) return true;

  const existingTs = existing.tradeTsMs;
  const incomingTs = incoming.tradeTsMs;
  if (existingTs != null && incomingTs != null) {
    if (incomingTs > existingTs) return true;
    if (incomingTs < existingTs) return false;
    return tieBreaker === 'incoming';
  }
  if (incomingTs != null && existingTs == null) return true;
  if (existingTs != null && incomingTs == null) return false;
  return tieBreaker === 'incoming';
}

function quotePriceCandidateForCache(row: QuoteRow): PricedCandidate {
  const rawPrice =
    typeof row.price === 'number' && Number.isFinite(row.price) ? row.price : null;
  const priceSource: PriceSourceKind = row.source === 'daily' ? 'daily_close' : 'snapshot';
  if (rawPrice != null) {
    return {
      price: rawPrice,
      tradeTsMs: extractSelectedPriceTradeTsMs(row, rawPrice),
      priceSource,
    };
  }
  const lastMin =
    typeof row.last_minute_close === 'number' && Number.isFinite(row.last_minute_close)
      ? row.last_minute_close
      : null;
  if (lastMin != null) {
    const record = row as Record<string, unknown>;
    const extTradeTs =
      parsePriceTimestampMs(row.live_tick_at) ??
      parsePriceTimestampMs(row.last_trade_at ?? record.last_trade_at) ??
      (row.source === 'ginlix-data' || 'last_trade_at' in record
        ? null
        : parsePriceTimestampMs(row.as_of));
    return {
      price: lastMin,
      tradeTsMs: extTradeTs,
      priceSource,
    };
  }
  const prevClose =
    typeof row.previous_close === 'number' && Number.isFinite(row.previous_close) && row.previous_close > 0
      ? row.previous_close
      : null;
  return {
    price: prevClose,
    tradeTsMs: extractSelectedPriceTradeTsMs(row, prevClose),
    priceSource,
  };
}

/**
 * Merge a REST snapshot row from `QuoteBatcher` into an existing shared cache entry
 * without letting an older REST trade (or bid/ask-only `as_of`) overwrite a newer
 * WS or cached trade price.
 */
export function mergeRestQuoteRow(
  prev: QuoteRow | null | undefined,
  incoming: QuoteRow,
): QuoteRow {
  if (!prev) return incoming;

  const existingCand = quotePriceCandidateForCache(prev);
  const incomingCand = quotePriceCandidateForCache(incoming);
  const tieBreaker = prev.live_tick_at != null ? 'existing' : 'incoming';

  if (shouldPreferIncomingPrice(existingCand, incomingCand, tieBreaker)) {
    return incoming;
  }

  // Keep existing newer price, 1D move, and its own trade timestamp fields while
  // refreshing non-price REST metadata (name, OHLC, volume, extended-hours fields).
  const keepAsOf =
    prev.live_tick_at != null ? (incoming.as_of ?? prev.as_of) : (prev.as_of ?? incoming.as_of);
  return {
    ...incoming,
    price: prev.price,
    change: prev.change,
    change_percent: prev.change_percent,
    ...(prev.last_minute_close != null ? { last_minute_close: prev.last_minute_close } : {}),
    ...(prev.live_tick_at != null ? { live_tick_at: prev.live_tick_at } : {}),
    ...(prev.last_trade_at !== undefined ? { last_trade_at: prev.last_trade_at } : {}),
    ...(prev.last_trade_price !== undefined ? { last_trade_price: prev.last_trade_price } : {}),
    ...(prev.regular_as_of !== undefined ? { regular_as_of: prev.regular_as_of } : {}),
    ...(keepAsOf !== undefined ? { as_of: keepAsOf } : {}),
  };
}

/**
 * Merge live WS fields into an existing shared cache entry using the same trade-timestamp
 * comparison rule as REST backfill and `/metrics`.
 */
export function mergeWsQuoteRow(
  prev: QuoteRow,
  key: string,
  fields: WsQuoteFields,
): QuoteRow {
  const wsTs = parsePriceTimestampMs(fields.timestamp) ?? Date.now();
  const existingCand = quotePriceCandidateForCache(prev);
  const incomingCand: PricedCandidate = {
    price: Number.isFinite(fields.price) ? fields.price : null,
    tradeTsMs: wsTs,
    priceSource: 'snapshot',
  };

  if (!shouldPreferIncomingPrice(existingCand, incomingCand, 'incoming')) {
    return prev;
  }

  return {
    ...prev,
    symbol: prev.symbol ?? key,
    price: fields.price,
    // A frame is a minute aggregate: its close is the row's last minute close,
    // which an extended-hours row prints as its session price.
    last_minute_close: fields.price,
    ...(fields.change != null && fields.changePercent != null
      ? { change: fields.change, change_percent: fields.changePercent }
      : {}),
    live_tick_at: wsTs,
  };
}
