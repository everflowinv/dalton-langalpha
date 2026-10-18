// integrations/openclaw/dalton-langalpha-broker/test/auth.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  verifyLoopbackAndOrigin,
  verifyBearerToken,
  ensureScopedTokenFile,
} from "../src/auth.mjs";

test("verifyLoopbackAndOrigin accepts 127.0.0.1 and ::1 and rejects non-loopback or proxy headers", () => {
  assert.equal(
    verifyLoopbackAndOrigin({ socket: { remoteAddress: "127.0.0.1" }, headers: {} }).ok,
    true,
  );
  assert.equal(
    verifyLoopbackAndOrigin({ socket: { remoteAddress: "::1" }, headers: {} }).ok,
    true,
  );

  const remoteReject = verifyLoopbackAndOrigin({
    socket: { remoteAddress: "192.168.1.25" },
    headers: {},
  });
  assert.equal(remoteReject.ok, false);
  assert.equal(remoteReject.status, 403);
  assert.equal(remoteReject.code, "loop_only");

  const proxyReject = verifyLoopbackAndOrigin({
    socket: { remoteAddress: "127.0.0.1" },
    headers: { "x-forwarded-for": "10.0.0.1" },
  });
  assert.equal(proxyReject.ok, false);
  assert.equal(proxyReject.status, 403);
  assert.equal(proxyReject.code, "loop_only");
});

test("verifyBearerToken validates token in constant time and ensureScopedTokenFile creates 0600 token", async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-auth-test-"));
  const { token, tokenPath } = await ensureScopedTokenFile({ stateDir: tmpDir });
  assert.ok(token.startsWith("dlb_"));
  assert.ok(token.length >= 36);

  const stat = await fs.stat(tokenPath);
  assert.equal(stat.mode & 0o777, 0o600);

  assert.equal(
    verifyBearerToken({ headers: { authorization: `Bearer ${token}` } }, token).ok,
    true,
  );
  assert.equal(
    verifyBearerToken({ headers: { authorization: "Bearer wrong_token" } }, token).ok,
    false,
  );
  assert.equal(verifyBearerToken({ headers: {} }, token).ok, false);

  await fs.rm(tmpDir, { recursive: true, force: true });
});
