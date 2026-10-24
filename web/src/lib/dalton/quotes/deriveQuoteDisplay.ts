// DALTON-HOOK(extended-hours-quotes): Single shared quote display derivation and 2-decimal formatters across Dashboard, MarketView, and agent exports.
import { grouped2 } from '@/lib/format';
import { getExtendedHoursInfo } from './extendedHoursCore';

export type MarketStatusInput = Parameters<typeof getExtendedHoursInfo>[0];

export interface QuoteDisplayInput {
  symbol?: string | null;
  price?: number | null;
  change?: number | null;
  changePercent?: number | null;
  change_percent?: number | null;
  previousClose?: number | null;
  previous_close?: number | null;
  /** Secondary previous-close fallback (e.g. OverviewQuote.previousClose), used only for settledClose/settledChange, never to synthesize regularClose. */
  fallbackPreviousClose?: number | null;
  regularClose?: number | null;
  regular_close?: number | null;
  regularTradingChange?: number | null;
  regular_trading_change?: number | null;
  regularTradingChangePercent?: number | null;
  regular_trading_change_percent?: number | null;
  earlyTradingChange?: number | null;
  early_trading_change?: number | null;
  earlyTradingChangePercent?: number | null;
  early_trading_change_percent?: number | null;
  lateTradingChange?: number | null;
  late_trading_change?: number | null;
  lateTradingChangePercent?: number | null;
  late_trading_change_percent?: number | null;
  lastMinuteClose?: number | null;
  last_minute_close?: number | null;
  lastTradePrice?: number | null;
  last_trade_price?: number | null;
  extType?: 'pre' | 'post' | null;
  extPrice?: number | null;
  extChange?: number | null;
  extPct?: number | null;
  liveTickAt?: number | null;
  live_tick_at?: number | null;
  market_status?: string | null;
  marketStatus?: string | null;
}

export interface LiveTickInput {
  price?: number | null;
  change?: number | null;
  changePercent?: number | null;
  change_percent?: number | null;
  timestamp?: number | null;
  liveTickAt?: number | null;
  live_tick_at?: number | null;
}

export interface QuoteDisplayResult {
  session: 'regular' | 'pre' | 'post';
  mainPrice: number | null;
  mainChange: number | null;
  mainChangePct: number | null;
  extPrice: number | null;
  extChange: number | null;
  extChangePct: number | null;
  extAnchor: number | null;
}

/**
 * Round to 2 decimal places using half-away-from-zero (matching Intl.NumberFormat /
 * grouped2 / formatMoney), so negative half-way values like -0.125 become -0.13
 * everywhere instead of Math.round's -0.12.
 */
export function roundQuote2(value: number): number {
  if (!Number.isFinite(value) || value === 0) return 0;
  const sign = value < 0 ? -1 : 1;
  const rounded = (sign * Math.round((Math.abs(value) + Number.EPSILON) * 100)) / 100;
  return Object.is(rounded, -0) ? 0 : rounded;
}

/**
 * Format a signed percentage to 2 decimal places (`+0.26%`, `-0.13%`, `+0.00%`)
 * using half-away-from-zero rounding across cards, widgets, sidebar, and exports.
 */
export function formatSignedPct2(value: number, locale = 'en-US'): string {
  const rounded = roundQuote2(value);
  const sign = rounded >= 0 ? '+' : '-';
  return `${sign}${grouped2(Math.abs(rounded), locale)}%`;
}

/**
 * Derive unrounded headline and extended-hours price/change figures from a
 * quote row, venue marketStatus, and an optional live WebSocket tick.
 *
 * Null semantics match MarketView's pre-refactor `deriveStockQuote`:
 * - After-hours (`post`) requires `regularClose` for `mainPrice`; never falls back to `prevClose` or `row.price`.
 * - Pre-market (`pre`) requires `prevClose` for `mainPrice`, and `regularTradingChange` / `regularTradingChangePercent`
 *   for `mainChange` / `mainChangePct`; never cross-fills from `row.change` / `row.changePercent`.
 * - A live tick is identified strictly by `liveTick.timestamp` or write-through `live_tick_at` / `liveTickAt`.
 */
export function deriveQuoteDisplay(
  row: QuoteDisplayInput | null | undefined,
  marketStatus?: MarketStatusInput,
  liveTick?: LiveTickInput | null,
): QuoteDisplayResult {
  const effectiveStatus: MarketStatusInput =
    marketStatus !== undefined
      ? marketStatus
      : row?.extType === 'pre'
        ? { market: 'extended-hours', afterHours: false, earlyHours: true }
        : row?.extType === 'post'
          ? { market: 'extended-hours', afterHours: true, earlyHours: false }
          : null;

  const info = getExtendedHoursInfo(effectiveStatus ?? null, row ?? null);
  const extType = (row?.extType !== undefined ? row.extType : info.extType) ?? null;
  const session: 'regular' | 'pre' | 'post' = extType ?? 'regular';

  const rowPrice = row?.price ?? null;
  const rowChange = row?.change ?? null;
  const rowChangePct = row?.changePercent ?? row?.change_percent ?? null;

  // Only recognize explicit liveTick.timestamp or write-through live_tick_at / liveTickAt.
  const tickHasTimestamp =
    liveTick != null &&
    (liveTick.timestamp != null || liveTick.liveTickAt != null || liveTick.live_tick_at != null);
  const rowHasLiveTick =
    row != null && (row.liveTickAt != null || row.live_tick_at != null);
  const rawTickPrice = tickHasTimestamp
    ? liveTick?.price
    : rowHasLiveTick
      ? (liveTick?.price ?? rowPrice)
      : null;
  const tickPrice =
    typeof rawTickPrice === 'number' && Number.isFinite(rawTickPrice) ? rawTickPrice : null;

  if (session === 'regular') {
    const mainPrice =
      tickHasTimestamp && liveTick?.price != null
        ? liveTick.price
        : (liveTick?.price ?? rowPrice);
    const mainChange =
      tickHasTimestamp && liveTick?.change !== undefined
        ? (liveTick.change ?? null)
        : (liveTick?.change ?? rowChange);
    const livePct = liveTick?.changePercent ?? liveTick?.change_percent;
    const mainChangePct =
      tickHasTimestamp && livePct !== undefined
        ? (livePct ?? null)
        : (livePct ?? rowChangePct);
    return {
      session: 'regular',
      mainPrice,
      mainChange,
      mainChangePct,
      extPrice: null,
      extChange: null,
      extChangePct: null,
      extAnchor: null,
    };
  }

  const prevClose = info.prevClose ?? row?.fallbackPreviousClose ?? null;
  const regularClose = info.regularClose ?? null;
  const regChangeField = row?.regularTradingChange ?? row?.regular_trading_change ?? null;
  const regChangePctField =
    row?.regularTradingChangePercent ?? row?.regular_trading_change_percent ?? null;

  const settledClose = (session === 'post' ? regularClose : prevClose) ?? null;

  const prePriorClose =
    session === 'pre' && settledClose != null && regChangeField != null
      ? settledClose - regChangeField
      : null;

  const mainChange =
    session === 'post' && regularClose != null && prevClose != null
      ? regularClose - prevClose
      : session === 'pre'
        ? regChangeField
        : null;

  const mainChangePct =
    session === 'post'
      ? (mainChange != null && prevClose ? (mainChange / prevClose) * 100 : null)
      : session === 'pre'
        ? (regChangePctField ??
            (mainChange != null && prePriorClose ? (mainChange / prePriorClose) * 100 : null))
        : null;

  // In after-hours (`post`), require `regularClose` as the session anchor; do not accept
  // a post-market price synthesized against `prevClose` when `regularClose` is missing.
  const hasPostAnchor = session !== 'post' || regularClose != null;
  const extAnchor = hasPostAnchor ? (info.extAnchor ?? null) : null;
  const baseExtPrice = hasPostAnchor ? (row?.extPrice ?? info.extPrice ?? null) : null;
  const baseExtChange = hasPostAnchor ? (row?.extChange ?? info.extChange ?? null) : null;
  const baseExtPct = hasPostAnchor ? (row?.extPct ?? info.extPct ?? null) : null;

  const extPrice = hasPostAnchor ? (tickPrice ?? baseExtPrice) : null;
  const extChange = hasPostAnchor
    ? (tickPrice != null && extAnchor != null ? tickPrice - extAnchor : baseExtChange)
    : null;
  const extChangePct = hasPostAnchor
    ? (tickPrice != null && extAnchor ? ((tickPrice - extAnchor) / extAnchor) * 100 : baseExtPct)
    : null;

  return {
    session,
    mainPrice: settledClose,
    mainChange,
    mainChangePct,
    extPrice,
    extChange,
    extChangePct,
    extAnchor,
  };
}
