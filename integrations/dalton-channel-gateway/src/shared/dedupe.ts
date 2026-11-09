/**
 * Short-term in-memory deduplication guard with TTL and bounded capacity.
 *
 * Adapted from OpenClaw deduplication helpers (`.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs:5215-5309`,
 * MIT, © OpenClaw contributors) for Design §4.1 (`src/shared/dedupe.ts`).
 *
 * Note: Persistent deduplication is still enforced by backend `I-1`
 * (`/internal/channels/inbound/register-and-resolve`); this guard filters
 * duplicate WebSocket/long-poll redeliveries within the same process window.
 */

import { pruneMapToMaxSize } from "./async.js";

export const DEFAULT_DEDUPE_TTL_MS = 30 * 60 * 1000; // 30 minutes
export const DEFAULT_DEDUPE_MAX_SIZE = 5_000;

export interface MemoryDedupeOptions {
  ttlMs?: number;
  maxSize?: number;
  now?: () => number;
}

export interface MemoryDedupeGuard {
  /**
   * Returns `true` if `key` is already present and non-expired (duplicate),
   * or `false` after recording `key` as newly seen.
   */
  check(key: string | null | undefined, nowMs?: number): boolean;
  /**
   * Returns `true` if `key` is currently recorded and non-expired without mutating state.
   */
  peek(key: string | null | undefined, nowMs?: number): boolean;
  /**
   * Explicitly records `key` with the current timestamp.
   */
  record(key: string | null | undefined, nowMs?: number): void;
  /**
   * Removes `key` from the deduplication cache (e.g. when a transient failure should allow retry).
   */
  delete(key: string | null | undefined): void;
  clear(): void;
  size(): number;
}

export function createMemoryDedupe(
  options: MemoryDedupeOptions = {},
): MemoryDedupeGuard {
  const ttlMs = Math.max(1, options.ttlMs ?? DEFAULT_DEDUPE_TTL_MS);
  const maxSize = Math.max(1, options.maxSize ?? DEFAULT_DEDUPE_MAX_SIZE);
  const clock = options.now ?? Date.now;
  const entries = new Map<string, number>();

  const evictExpired = (nowMs: number): void => {
    for (const [k, expiresAt] of entries) {
      if (expiresAt <= nowMs) {
        entries.delete(k);
      } else {
        // Map preserves insertion order; since TTL is uniform, once we hit a non-expired
        // entry, subsequent entries are also non-expired.
        break;
      }
    }
  };

  const normalizeKey = (key: string | null | undefined): string | null => {
    if (typeof key !== "string") {
      return null;
    }
    const trimmed = key.trim();
    return trimmed.length > 0 ? trimmed : null;
  };

  const peek = (key: string | null | undefined, nowMs = clock()): boolean => {
    const normalized = normalizeKey(key);
    if (!normalized) {
      return false;
    }
    evictExpired(nowMs);
    const expiresAt = entries.get(normalized);
    if (expiresAt === undefined) {
      return false;
    }
    if (expiresAt <= nowMs) {
      entries.delete(normalized);
      return false;
    }
    return true;
  };

  const record = (key: string | null | undefined, nowMs = clock()): void => {
    const normalized = normalizeKey(key);
    if (!normalized) {
      return;
    }
    evictExpired(nowMs);
    entries.delete(normalized);
    entries.set(normalized, nowMs + ttlMs);
    pruneMapToMaxSize(entries, maxSize);
  };

  const check = (key: string | null | undefined, nowMs = clock()): boolean => {
    const normalized = normalizeKey(key);
    if (!normalized) {
      return false;
    }
    if (peek(normalized, nowMs)) {
      return true;
    }
    record(normalized, nowMs);
    return false;
  };

  return {
    check,
    peek,
    record,
    delete: (key) => {
      const normalized = normalizeKey(key);
      if (normalized) {
        entries.delete(normalized);
      }
    },
    clear: () => entries.clear(),
    size: () => entries.size,
  };
}
