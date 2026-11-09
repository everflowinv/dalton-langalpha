/**
 * Shared async primitives: abortable sleep/timeout racing, bounded map pruning,
 * timer clamping, exponential backoff retry, and error formatting.
 *
 * Adapted from OpenClaw reference helpers (`.agy/ref_openclaw_channels/`,
 * MIT, © OpenClaw contributors) for Design §4.1 (`src/shared/async.ts`).
 */

export const MAX_SAFE_TIMEOUT_MS = 2_147_483_647;

export function formatErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message || error.name || "Error";
  }
  if (typeof error === "string") {
    return error;
  }
  if (error === null || error === undefined) {
    return String(error);
  }
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

export function resolveTimerTimeoutMs(
  durationMs: number,
  maxMs: number = MAX_SAFE_TIMEOUT_MS,
): number {
  if (!Number.isFinite(durationMs) || durationMs <= 0) {
    return 0;
  }
  return Math.min(Math.floor(durationMs), maxMs);
}

export function resolveExpiresAtMsFromDurationMs(
  durationMs: number,
  nowMs: number = Date.now(),
): number {
  return nowMs + Math.max(0, Math.floor(durationMs));
}

export function isFutureDateTimestampMs(
  timestampMs: number | undefined | null,
  nowMs: number = Date.now(),
): boolean {
  return (
    typeof timestampMs === "number" &&
    Number.isFinite(timestampMs) &&
    timestampMs > nowMs
  );
}

export function pruneMapToMaxSize<K, V>(map: Map<K, V>, maxSize: number): void {
  const limit = Math.max(0, Math.floor(maxSize));
  while (map.size > limit) {
    const oldest = map.keys().next();
    if (oldest.done) {
      break;
    }
    map.delete(oldest.value);
  }
}

export function sleepWithAbort(
  ms: number,
  signal?: AbortSignal,
): Promise<void> {
  if (signal?.aborted) {
    const abortErr = new Error("Aborted");
    abortErr.name = "AbortError";
    return Promise.reject(abortErr);
  }
  const delay = resolveTimerTimeoutMs(ms);
  if (delay <= 0) {
    return Promise.resolve();
  }
  return new Promise<void>((resolve, reject) => {
    const onAbort = (): void => {
      clearTimeout(timer);
      const abortErr = new Error("Aborted");
      abortErr.name = "AbortError";
      reject(abortErr);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delay);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Waits for `ms` milliseconds or resolves early with `false` if `signal` aborts.
 * Returns `true` when the full delay elapsed without aborting.
 */
export async function waitForAbortableDelay(
  ms: number,
  signal?: AbortSignal,
): Promise<boolean> {
  try {
    await sleepWithAbort(ms, signal);
    return true;
  } catch {
    return false;
  }
}

export type RaceWithTimeoutResult<T> =
  | { status: "resolved"; value: T }
  | { status: "timeout" }
  | { status: "aborted" }
  | { status: "rejected"; error: unknown };

export async function raceWithTimeoutAndAbort<T>(
  promise: Promise<T>,
  options: {
    timeoutMs?: number;
    abortSignal?: AbortSignal;
  } = {},
): Promise<RaceWithTimeoutResult<T>> {
  const { timeoutMs, abortSignal } = options;
  if (abortSignal?.aborted) {
    return { status: "aborted" };
  }

  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;

  const racePromises: Array<Promise<RaceWithTimeoutResult<T>>> = [
    promise.then(
      (value): RaceWithTimeoutResult<T> => ({ status: "resolved", value }),
      (error: unknown): RaceWithTimeoutResult<T> => ({ status: "rejected", error }),
    ),
  ];

  if (typeof timeoutMs === "number" && Number.isFinite(timeoutMs) && timeoutMs > 0) {
    const clampedMs = resolveTimerTimeoutMs(timeoutMs);
    racePromises.push(
      new Promise<RaceWithTimeoutResult<T>>((resolve) => {
        timer = setTimeout(() => resolve({ status: "timeout" }), clampedMs);
      }),
    );
  }

  if (abortSignal) {
    racePromises.push(
      new Promise<RaceWithTimeoutResult<T>>((resolve) => {
        onAbort = () => resolve({ status: "aborted" });
        abortSignal.addEventListener("abort", onAbort, { once: true });
      }),
    );
  }

  try {
    return await Promise.race(racePromises);
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    if (onAbort && abortSignal) {
      abortSignal.removeEventListener("abort", onAbort);
    }
  }
}

export interface RetryAsyncOptions {
  attempts?: number;
  minDelayMs?: number;
  maxDelayMs?: number;
  factor?: number;
  shouldRetry?: (error: unknown, attempt: number) => boolean;
  signal?: AbortSignal;
}

export async function retryAsync<T>(
  fn: () => Promise<T>,
  options: RetryAsyncOptions = {},
): Promise<T> {
  const attempts = Math.max(1, Math.floor(options.attempts ?? 3));
  const minDelayMs = Math.max(0, options.minDelayMs ?? 500);
  const maxDelayMs = Math.max(minDelayMs, options.maxDelayMs ?? 10_000);
  const factor = Math.max(1, options.factor ?? 2);
  const shouldRetry = options.shouldRetry ?? (() => true);

  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (options.signal?.aborted) {
      const abortErr = new Error("Aborted");
      abortErr.name = "AbortError";
      throw abortErr;
    }
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (attempt >= attempts || !shouldRetry(error, attempt)) {
        throw error;
      }
      const delayMs = Math.min(maxDelayMs, Math.round(minDelayMs * factor ** (attempt - 1)));
      await sleepWithAbort(delayMs, options.signal);
    }
  }
  throw lastError;
}
