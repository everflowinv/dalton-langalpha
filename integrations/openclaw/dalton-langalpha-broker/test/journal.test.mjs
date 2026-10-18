// integrations/openclaw/dalton-langalpha-broker/test/journal.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { scrubSecretsDeep, createBrokerJournal } from "../src/journal.mjs";

test("scrubSecretsDeep strips all credential keys recursively", () => {
  const dirty = {
    request_id: "req_1",
    apiKey: "sk-secret-123",
    nested: {
      Authorization: "Bearer secret",
      headers: { "x-api-key": "top-secret" },
      safe_field: "keep_me",
    },
  };
  const cleaned = scrubSecretsDeep(dirty);
  assert.equal("apiKey" in cleaned, false);
  assert.equal("Authorization" in cleaned.nested, false);
  assert.equal("headers" in cleaned.nested, false);
  assert.equal(cleaned.nested.safe_field, "keep_me");
});

test("createBrokerJournal writes scrubbed JSONL lines", async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-journal-test-"));
  const journal = await createBrokerJournal({ journalDir: tmpDir });
  await journal.append({
    request_id: "req_test",
    model_ref: "xai/grok-4.7",
    apiKey: "MUST_NOT_APPEAR",
    status: "ok",
  });
  const files = await fs.readdir(tmpDir);
  assert.equal(files.length, 1);
  const raw = await fs.readFile(path.join(tmpDir, files[0]), "utf8");
  assert.equal(raw.includes("MUST_NOT_APPEAR"), false);
  const parsed = JSON.parse(raw.trim());
  assert.equal(parsed.request_id, "req_test");
  assert.equal(parsed.schema, "dalton.broker.journal.v2");
  await fs.rm(tmpDir, { recursive: true, force: true });
});
