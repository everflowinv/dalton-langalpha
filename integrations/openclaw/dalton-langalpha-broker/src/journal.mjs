// integrations/openclaw/dalton-langalpha-broker/src/journal.mjs
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const FORBIDDEN_JOURNAL_KEYS = new Set([
  "apiKey",
  "api_key",
  "authorization",
  "Authorization",
  "x-api-key",
  "headers",
  "access_token",
  "refresh_token",
  "bearer",
  "secret",
  "password",
  "token",
]);

export function sha256Hex(input) {
  return crypto
    .createHash("sha256")
    .update(typeof input === "string" ? input : JSON.stringify(input ?? null))
    .digest("hex");
}

export function scrubSecretsDeep(value) {
  if (Array.isArray(value)) return value.map(scrubSecretsDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (FORBIDDEN_JOURNAL_KEYS.has(k)) continue;
      out[k] = scrubSecretsDeep(v);
    }
    return out;
  }
  return value;
}

const DEFAULT_INVOCATION_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
const DEFAULT_MAX_CACHE_ENTRIES = 500;

export async function createBrokerJournal({
  journalDir,
  stateDir,
  invocationsPath: explicitInvocationsPath,
  ttlMs = DEFAULT_INVOCATION_TTL_MS,
  maxCacheEntries = DEFAULT_MAX_CACHE_ENTRIES,
}) {
  await fs.mkdir(journalDir, { recursive: true, mode: 0o700 });
  const invocationsPath =
    explicitInvocationsPath ||
    (stateDir ? path.join(stateDir, "invocations.json") : path.join(journalDir, "invocations.json"));
  await fs.mkdir(path.dirname(invocationsPath), { recursive: true, mode: 0o700 });

  let chainQueue = Promise.resolve();
  let writeFailures = 0;
  const invocationCache = new Map();

  function entryTimestampMs(entry) {
    if (typeof entry?.updatedAtMs === "number" && Number.isFinite(entry.updatedAtMs)) {
      return entry.updatedAtMs;
    }
    const iso = entry?.completedAt || entry?.failedAt || entry?.indeterminateAt || entry?.startedAt;
    const parsed = iso ? Date.parse(iso) : NaN;
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function pruneInvocations(now = Date.now()) {
    let mutated = false;
    for (const [id, entry] of invocationCache.entries()) {
      if (entry.status === "completed" || entry.status === "failed_no_output") {
        const ts = entryTimestampMs(entry);
        if (ts > 0 && now - ts > ttlMs) {
          invocationCache.delete(id);
          mutated = true;
        }
      }
    }
    if (invocationCache.size > maxCacheEntries) {
      // First evict oldest completed/failed_no_output entries
      for (const [id, entry] of invocationCache.entries()) {
        if (invocationCache.size <= maxCacheEntries) break;
        if (entry.status === "completed" || entry.status === "failed_no_output") {
          invocationCache.delete(id);
          mutated = true;
        }
      }
      // If still over limit, evict oldest entries in insertion/LRU order
      for (const id of invocationCache.keys()) {
        if (invocationCache.size <= maxCacheEntries) break;
        invocationCache.delete(id);
        mutated = true;
      }
    }
    return mutated;
  }

  async function writeInvocationsToDisk() {
    const entriesObj = {};
    for (const [id, entry] of invocationCache.entries()) {
      entriesObj[id] = entry;
    }
    const payload = JSON.stringify(
      {
        schema: "dalton.broker.invocations.v2",
        updatedAt: new Date().toISOString(),
        entries: entriesObj,
      },
      null,
      2,
    );
    const tmpPath = `${invocationsPath}.tmp.${process.pid}.${Date.now()}`;
    await fs.writeFile(tmpPath, `${payload}\n`, { encoding: "utf8", mode: 0o600 });
    await fs.chmod(tmpPath, 0o600);
    await fs.rename(tmpPath, invocationsPath);
  }

  function enqueueDiskSync() {
    chainQueue = chainQueue
      .then(() => writeInvocationsToDisk())
      .catch(() => {
        writeFailures += 1;
      });
    return chainQueue;
  }

  // Load existing invocations from disk on startup; any "pending" left over from
  // a gateway restart/crash becomes "indeterminate".
  try {
    const raw = await fs.readFile(invocationsPath, "utf8");
    const parsed = JSON.parse(raw);
    const rawEntries =
      parsed && typeof parsed === "object" && parsed.entries && typeof parsed.entries === "object"
        ? parsed.entries
        : parsed && typeof parsed === "object"
          ? parsed
          : {};
    let needsSave = false;
    const nowIso = new Date().toISOString();
    const nowMs = Date.now();
    for (const [id, entry] of Object.entries(rawEntries)) {
      if (!entry || typeof entry !== "object") continue;
      const cloned = { ...entry, invocationId: entry.invocationId || id };
      if (cloned.status === "pending") {
        cloned.status = "indeterminate";
        cloned.indeterminateAt = nowIso;
        cloned.updatedAtMs = nowMs;
        needsSave = true;
      }
      invocationCache.set(id, cloned);
    }
    if (pruneInvocations(nowMs)) {
      needsSave = true;
    }
    if (needsSave) {
      await writeInvocationsToDisk();
    }
  } catch (err) {
    if (err?.code !== "ENOENT") {
      writeFailures += 1;
    }
  }

  async function append(event) {
    const safeEvent = scrubSecretsDeep({
      ts: new Date().toISOString(),
      schema: "dalton.broker.journal.v2",
      ...event,
    });
    const day = safeEvent.ts.slice(0, 10);
    const filePath = path.join(journalDir, `broker-${day}.jsonl`);
    const line = `${JSON.stringify(safeEvent)}\n`;
    chainQueue = chainQueue
      .then(() => fs.appendFile(filePath, line, { encoding: "utf8", mode: 0o600 }))
      .catch(() => {
        writeFailures += 1;
      });
    return chainQueue;
  }

  async function getInvocation(invocationId) {
    if (!invocationId) return null;
    if (pruneInvocations(Date.now())) {
      await enqueueDiskSync();
    }
    const existing = invocationCache.get(invocationId);
    if (!existing) return null;
    // Refresh LRU position
    invocationCache.delete(invocationId);
    invocationCache.set(invocationId, existing);
    return { ...existing };
  }

  async function setInvocation(invocationId, record) {
    if (!invocationId) return null;
    const nowMs = Date.now();
    const safeRecord = scrubSecretsDeep(record);
    const entry = {
      invocationId,
      ...safeRecord,
      updatedAtMs: nowMs,
    };
    invocationCache.delete(invocationId);
    invocationCache.set(invocationId, entry);
    pruneInvocations(nowMs);
    await enqueueDiskSync();
    return { ...entry };
  }

  function getStats() {
    pruneInvocations(Date.now());
    let pending = 0;
    let indeterminate = 0;
    let completed = 0;
    let failedNoOutput = 0;
    for (const entry of invocationCache.values()) {
      if (entry.status === "pending") pending += 1;
      else if (entry.status === "indeterminate") indeterminate += 1;
      else if (entry.status === "completed") completed += 1;
      else if (entry.status === "failed_no_output") failedNoOutput += 1;
    }
    return {
      pending,
      indeterminate,
      completed,
      failed_no_output: failedNoOutput,
      total: invocationCache.size,
      write_failures: writeFailures,
    };
  }

  return {
    journalDir,
    invocationsPath,
    append,
    getInvocation,
    setInvocation,
    getStats,
  };
}
