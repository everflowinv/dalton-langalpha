/**
 * Market-watch SSE handling: the live watch-chip snapshot. Moves to
 * session/marketWatchEvents.ts with the live-router carve (5.7).
 */
import type { ChatMessage, NotificationMessage } from '@/types/chat';
import type { MarketTarget, MarketTargetKind, MarketTargetPctFrom } from '../utils/api/threads';
import { createMarketAlertMessage } from '../hooks/utils/messageHelpers';

/**
 * Latest market-watch snapshot for a thread. Seeded from the GET
 * `/market-watch` endpoint on thread load and overwritten live by
 * `market_watch_update` SSE events; drives the persistent "Watching …" chip.
 * `targets` comes from the GET alone, so a live stamp keeps the last one.
 */
export interface MarketWatchState {
  symbols: string[];
  targets?: MarketTarget[];
  content?: string;
  timestamp?: number;
}

/**
 * Coerce an untrusted `symbols` field (SSE event or GET response) to a
 * `string[]`: a missing/non-array value becomes an empty list ("watch off")
 * and non-string entries are dropped.
 */
export function coerceSymbols(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((s): s is string => typeof s === 'string')
    : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

const TARGET_KINDS: readonly MarketTargetKind[] = ['above', 'below', 'pct_up', 'pct_down'];

function asKind(value: unknown): MarketTargetKind | undefined {
  return TARGET_KINDS.find((k) => k === value);
}

function asPctFrom(value: unknown): MarketTargetPctFrom | null {
  return value === 'previous_close' || value === 'now' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

/** The targets still waiting on their condition. */
export function armedTargets(targets: readonly MarketTarget[] | null | undefined): MarketTarget[] {
  return (targets ?? []).filter((t) => t.status === 'armed');
}

function lapsedAt(target: MarketTarget, now: number): boolean {
  if (target.status !== 'armed') return false;
  const ms = Date.parse(target.expires_at);
  return Number.isFinite(ms) && ms <= now;
}

/**
 * The targets as they stand at `now`. The server settles a lapsed target on
 * its next sweep, and the view re-reads the targets only at turn end, on an
 * alert and after a cancel, so one past its expiry would go on reading armed,
 * with a live Cancel, until then. It reads expired here first. The same list
 * comes back when none has lapsed.
 */
export function targetsAt(targets: MarketTarget[] | undefined, now: number): MarketTarget[] | undefined {
  if (!targets?.some((t) => lapsedAt(t, now))) return targets;
  return targets.map((t) => (lapsedAt(t, now) ? { ...t, status: 'expired' as const } : t));
}

/** When the next armed target lapses after `now`, or null when none is left to. */
export function nextLapse(targets: readonly MarketTarget[] | null | undefined, now: number): number | null {
  let next: number | null = null;
  for (const t of targets ?? []) {
    if (t.status !== 'armed') continue;
    const ms = Date.parse(t.expires_at);
    if (Number.isFinite(ms) && ms > now && (next === null || ms < next)) next = ms;
  }
  return next;
}

/**
 * One alert about a target: it hit, or it is about to expire unhit. Every
 * display field is optional because a wake turn may name only the target,
 * whose row then supplies the rest (`resolveNotice`).
 */
export interface MarketTargetNotice {
  target_id: string;
  notice?: 'hit' | 'expiring';
  symbol?: string;
  kind?: MarketTargetKind;
  value?: number;
  pct_from?: MarketTargetPctFrom | null;
  fired_price?: number | null;
  fired_at?: string | null;
  expires_at?: string | null;
}

/**
 * A price alert reaching the thread, drawn as one row in the transcript:
 * `delivered` landed inside a running turn (a `steering_delivered` entry),
 * `wake` started the turn it heads (a `market_target` query).
 */
export interface MarketAlert {
  source: 'delivered' | 'wake';
  notices: MarketTargetNotice[];
}

/** Coerce an untrusted notice list (SSE payload or query metadata). */
export function coerceNotices(value: unknown): MarketTargetNotice[] {
  if (!Array.isArray(value)) return [];
  const out: MarketTargetNotice[] = [];
  for (const raw of value) {
    if (!isRecord(raw) || typeof raw.target_id !== 'string' || !raw.target_id) continue;
    out.push({
      target_id: raw.target_id,
      notice: raw.notice === 'hit' || raw.notice === 'expiring' ? raw.notice : undefined,
      symbol: asString(raw.symbol) ?? undefined,
      kind: asKind(raw.kind),
      value: asNumber(raw.value) ?? undefined,
      pct_from: asPctFrom(raw.pct_from),
      fired_price: asNumber(raw.fired_price),
      fired_at: asString(raw.fired_at),
      expires_at: asString(raw.expires_at),
    });
  }
  return out;
}

/**
 * The notices a wake turn's query names, from its metadata's `market_target`:
 * `{notices: [...]}` or a single notice. The query text is written for the
 * model and is never parsed for these.
 */
export function wakeNotices(metadata: unknown): MarketTargetNotice[] {
  if (!isRecord(metadata)) return [];
  const entry = metadata.market_target;
  if (!isRecord(entry)) return [];
  return Array.isArray(entry.notices) ? coerceNotices(entry.notices) : coerceNotices([entry]);
}

/** One id per alert, the same live and on replay: a notice is delivered once. */
export function marketAlertId(notices: readonly MarketTargetNotice[]): string {
  return `market-alert-${notices.map((n) => `${n.target_id}:${n.notice ?? 'hit'}`).join(',')}`;
}

/**
 * The row a wake turn opens with, from query metadata naming its
 * `market_target`: the turn's query row on replay, the run's first frame on a
 * live attach. Keyed on the turn alone, which both name, so the two draw one
 * row: an attach finds the replay's, and a replay takes the place of the
 * attach's.
 */
export function wakeAlertRow(turnIndex: number, metadata: unknown, timestamp: Date): NotificationMessage {
  return createMarketAlertMessage(
    `history-market-alert-wake-${turnIndex}`,
    { source: 'wake', notices: wakeNotices(metadata) },
    timestamp,
  );
}

/**
 * A wake turn attached live: no query row comes with its stream, so its alert
 * row goes ahead of the bubble the attach opened. A row already drawn for the
 * turn stays where it is: a replay drew it while the run was still going, or
 * an earlier attach replayed the stream from its start.
 */
export function withLiveWakeRow(messages: ChatMessage[], row: ChatMessage, beforeId: string | null): ChatMessage[] {
  if (messages.some((m) => m.id === row.id)) return messages;
  const found = beforeId ? messages.findIndex((m) => m.id === beforeId) : -1;
  const at = found === -1 ? messages.length : found;
  return [...messages.slice(0, at), row, ...messages.slice(at)];
}

/** A notice with everything a row needs to say it. */
export interface ResolvedNotice {
  notice: 'hit' | 'expiring';
  symbol: string;
  kind: MarketTargetKind;
  value: number;
  pct_from: MarketTargetPctFrom | null;
  fired_price: number | null;
  fired_at: string | null;
  expires_at: string | null;
}

/**
 * Fill a notice's missing fields from its target's row, when the thread still
 * lists it (settled targets drop off after a day). Null when the symbol, kind
 * or value is still unknown: the row then falls back to a generic label.
 */
export function resolveNotice(
  notice: MarketTargetNotice,
  targets: readonly MarketTarget[] | null | undefined,
): ResolvedNotice | null {
  const target = targets?.find((t) => t.target_id === notice.target_id);
  const symbol = notice.symbol ?? target?.symbol;
  const kind = notice.kind ?? target?.kind;
  const value = notice.value ?? target?.value;
  if (!symbol || !kind || value === undefined) return null;
  return {
    notice: notice.notice ?? (target?.status === 'armed' ? 'expiring' : 'hit'),
    symbol,
    kind,
    value,
    pct_from: notice.pct_from ?? target?.pct_from ?? null,
    fired_price: notice.fired_price ?? target?.fired_price ?? null,
    fired_at: notice.fired_at ?? target?.fired_at ?? null,
    expires_at: notice.expires_at ?? target?.expires_at ?? null,
  };
}

/**
 * Handles `market_watch_update` custom SSE events during streaming. Parses the
 * live watch snapshot (symbols + content + timestamp) off the event and hands
 * it to the caller's setter so the persistent watch chip stays current
 * mid-turn. Non-string symbol entries are dropped and a missing/non-array
 * `symbols` field coerces to an empty list (the "watch off" signal).
 */
export function handleMarketWatchUpdate({ event, setMarketWatch }: {
  event: { symbols?: unknown; content?: unknown; timestamp?: unknown };
  setMarketWatch: (next: MarketWatchState) => void;
}): boolean {
  const symbols = coerceSymbols(event.symbols);
  setMarketWatch({
    symbols,
    content: typeof event.content === 'string' ? event.content : undefined,
    timestamp: typeof event.timestamp === 'number' ? event.timestamp : undefined,
  });
  return true;
}
