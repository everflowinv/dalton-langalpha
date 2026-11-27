/**
 * Maximum symbols accepted by GET /api/v1/market-data/snapshots/{stocks,indexes}
 * (matches `_MAX_BATCH_SNAPSHOT_SYMBOLS = 250` in `src/server/app/market_data.py`).
 */
export const MAX_SNAPSHOT_BATCH_SYMBOLS = 250;

/**
 * Split a coalesced pending quote pool into chunks within the snapshot API width
 * limit so each chunk is fetched and written back to React Query cache independently.
 */
export function chunkQuotePool<T>(
  pool: Map<string, T>,
  maxChunkSize: number = MAX_SNAPSHOT_BATCH_SYMBOLS,
): Array<Map<string, T>> {
  if (pool.size === 0) return [];
  if (pool.size <= maxChunkSize) return [pool];

  const chunks: Array<Map<string, T>> = [];
  let current = new Map<string, T>();
  for (const [key, deferred] of pool) {
    current.set(key, deferred);
    if (current.size >= maxChunkSize) {
      chunks.push(current);
      current = new Map<string, T>();
    }
  }
  if (current.size > 0) {
    chunks.push(current);
  }
  return chunks;
}
