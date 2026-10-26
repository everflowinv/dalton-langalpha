/**
 * How a market-watch target and its alerts read, shared by the watch chip, the
 * Status panel and the transcript's alert rows so the three say a condition
 * the same way. Pure: the caller hands in `t`, the locale and the time.
 */
import type { TFunction } from 'i18next';
import { clockTime, createFormatter, relativeTime } from '@/lib/format';
import type { MarketTarget, MarketTargetKind } from '../utils/api';
import {
  resolveNotice, type MarketAlert, type ResolvedNotice,
} from '../session/marketWatchEvents';

// A level as the agent set it (205, 190.5), a traded price with its cents
// (205.12), and a move with its sign (+3%, -2.5%).
const levelFormat = createFormatter({ maximumFractionDigits: 4 });
const priceFormat = createFormatter({ minimumFractionDigits: 2, maximumFractionDigits: 4 });
const moveFormat = createFormatter({ style: 'percent', signDisplay: 'always', maximumFractionDigits: 2 });

function epochMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** The compact condition: `≥ 205`, `≤ 190.5`, `+3%`, `-3%`. */
export function conditionLabel(kind: MarketTargetKind, value: number, locale: string): string {
  switch (kind) {
    case 'above':
      return `≥ ${levelFormat(value, locale)}`;
    case 'below':
      return `≤ ${levelFormat(value, locale)}`;
    case 'pct_up':
      return moveFormat(value / 100, locale);
    case 'pct_down':
      return moveFormat(-value / 100, locale);
    default:
      return kind satisfies never;
  }
}

/** The chip's items: each watched symbol, with its armed conditions beside it. */
export function watchChipItems(
  symbols: readonly string[],
  armed: readonly MarketTarget[],
  locale: string,
): string[] {
  const conditions = new Map<string, string[]>();
  for (const target of armed) {
    const list = conditions.get(target.symbol) ?? [];
    list.push(conditionLabel(target.kind, target.value, locale));
    conditions.set(target.symbol, list);
  }
  const order = [...new Set([...symbols, ...armed.map((target) => target.symbol)])];
  return order.map((symbol) => {
    const list = conditions.get(symbol);
    return list ? `${symbol} · ${list.join(' / ')}` : symbol;
  });
}

/** A condition in full, for the panel: a move says what it is measured from. */
export function targetConditionLabel(target: MarketTarget, t: TFunction, locale: string): string {
  const condition = conditionLabel(target.kind, target.value, locale);
  if (target.kind === 'above' || target.kind === 'below') return condition;
  if (target.pct_from === 'now') {
    return target.armed_price !== null
      ? t('chat.marketWatch.pctFromPrice', { move: condition, price: priceFormat(target.armed_price, locale) })
      : t('chat.marketWatch.pctFromSet', { move: condition });
  }
  return t('chat.marketWatch.pctFromClose', { move: condition });
}

/** `205.12 at 1:05 PM`, or whichever half is known. */
function pricedAt(
  price: number | null,
  at: string | null,
  t: TFunction,
  locale: string,
): string {
  const ms = epochMs(at);
  const priced = price !== null ? priceFormat(price, locale) : null;
  if (priced && ms !== null) return t('chat.marketAlert.pricedAt', { price: priced, time: clockTime(ms, locale) });
  if (ms !== null) return t('chat.marketAlert.at', { time: clockTime(ms, locale) });
  return priced ?? '';
}

/** A target's state for the panel: time left, where it hit, or how it ended. */
export function targetStatusLabel(target: MarketTarget, t: TFunction, locale: string, now: number): string {
  switch (target.status) {
    case 'armed': {
      const left = relativeTime(epochMs(target.expires_at), locale, now);
      return left ? t('chat.marketWatch.targetExpires', { when: left }) : t('chat.marketWatch.targetArmed');
    }
    case 'fired': {
      const detail = pricedAt(target.fired_price, target.fired_at, t, locale);
      return detail ? t('chat.marketWatch.targetHitAt', { detail }) : t('chat.marketWatch.targetHit');
    }
    case 'expired':
      return t('chat.marketWatch.targetExpired');
    case 'cancelled':
      return t('chat.marketWatch.targetCancelled');
    default:
      return target.status satisfies never;
  }
}

/** One notice as a sentence: `NVDA crossed above 205.00 · 205.12 at 1:05 PM`. */
function noticeSentence(notice: ResolvedNotice, t: TFunction, locale: string): string {
  const { symbol, kind, value } = notice;
  if (notice.notice === 'expiring') {
    const condition = conditionLabel(kind, value, locale);
    const ms = epochMs(notice.expires_at);
    return ms !== null
      ? t('chat.marketAlert.expiring', { symbol, condition, time: clockTime(ms, locale) })
      : t('chat.marketAlert.expiringSoon', { symbol, condition });
  }
  let what: string;
  if (kind === 'above' || kind === 'below') {
    const level = priceFormat(value, locale);
    what = kind === 'above'
      ? t('chat.marketAlert.crossedAbove', { symbol, level })
      : t('chat.marketAlert.crossedBelow', { symbol, level });
  } else {
    const move = conditionLabel(kind, value, locale);
    what = notice.pct_from === 'now'
      ? t('chat.marketAlert.movedFromSet', { symbol, move })
      : t('chat.marketAlert.movedFromClose', { symbol, move });
  }
  const detail = pricedAt(notice.fired_price, notice.fired_at, t, locale);
  return detail ? `${what} · ${detail}` : what;
}

/**
 * The text of an alert's transcript row. A notice whose target the thread no
 * longer lists, and that carried no fields of its own, says nothing specific:
 * the row falls back to the generic label rather than to the model's text.
 */
export function marketAlertLabel(
  alert: MarketAlert,
  targets: readonly MarketTarget[] | null | undefined,
  t: TFunction,
  locale: string,
): string {
  const text = alert.notices
    .map((notice) => resolveNotice(notice, targets))
    .filter((notice): notice is ResolvedNotice => notice !== null)
    .map((notice) => noticeSentence(notice, t, locale))
    .join('; ');
  if (alert.source === 'wake') {
    return text ? t('chat.marketAlert.woke', { alert: text }) : t('chat.marketAlert.wokeGeneric');
  }
  return text || t('chat.marketAlert.generic');
}
