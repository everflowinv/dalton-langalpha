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

test("createBrokerJournal persists invocations with 0600 mode and converts pending to indeterminate on restart", async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-journal-idem-"));
  const stateDir = path.join(tmpDir, "state");
  const journalDir = path.join(stateDir, "journal");

  try {
    const journal1 = await createBrokerJournal({ journalDir, stateDir });
    await journal1.setInvocation("inv_pending_1", {
      reqHash: "hash_p1",
      status: "pending",
      model: "xai/grok-4.7",
      startedAt: new Date().toISOString(),
    });
    await journal1.setInvocation("inv_done_1", {
      reqHash: "hash_d1",
      status: "completed",
      model: "xai/grok-4.7",
      completedAt: new Date().toISOString(),
      responsePayload: { id: "chatcmpl-1", choices: [] },
    });

    const stat = await fs.stat(journal1.invocationsPath);
    assert.equal(stat.mode & 0o777, 0o600);

    // Simulate gateway restart by creating a new journal instance on the same stateDir
    const journal2 = await createBrokerJournal({ journalDir, stateDir });
    const recoveredPending = await journal2.getInvocation("inv_pending_1");
    assert.equal(recoveredPending.status, "indeterminate");
    assert.equal(recoveredPending.reqHash, "hash_p1");

    const recoveredCompleted = await journal2.getInvocation("inv_done_1");
    assert.equal(recoveredCompleted.status, "completed");
    assert.equal(recoveredCompleted.responsePayload.id, "chatcmpl-1");

    const stats = journal2.getStats();
    assert.equal(stats.pending, 0);
    assert.equal(stats.indeterminate, 1);
    assert.equal(stats.completed, 1);
    assert.equal(stats.total, 2);
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
});

test("createBrokerJournal enforces TTL cleanup and bounded LRU cache on completed entries", async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-journal-lru-"));
  const stateDir = path.join(tmpDir, "state");
  const journalDir = path.join(stateDir, "journal");

  try {
    const journal = await createBrokerJournal({
      journalDir,
      stateDir,
      ttlMs: 50,
      maxCacheEntries: 2,
    });

    await journal.setInvocation("inv_1", { reqHash: "h1", status: "completed" });
    await journal.setInvocation("inv_2", { reqHash: "h2", status: "completed" });
    // Access inv_1 to make it most-recently used; inv_2 becomes oldest
    await journal.getInvocation("inv_1");
    await journal.setInvocation("inv_3", { reqHash: "h3", status: "completed" });

    assert.equal(await journal.getInvocation("inv_2"), null);
    assert.notEqual(await journal.getInvocation("inv_1"), null);
    assert.notEqual(await journal.getInvocation("inv_3"), null);

    // Wait for TTL expiry
    await new Promise((r) => setTimeout(r, 70));
    assert.equal(await journal.getInvocation("inv_1"), null);
    assert.equal(await journal.getInvocation("inv_3"), null);
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
});

