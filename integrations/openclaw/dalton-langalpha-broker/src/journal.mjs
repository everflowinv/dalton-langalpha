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

export async function createBrokerJournal({ journalDir }) {
  await fs.mkdir(journalDir, { recursive: true, mode: 0o700 });
  let chainQueue = Promise.resolve();

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
      .catch(() => {});
    return chainQueue;
  }

  return {
    journalDir,
    append,
  };
}
