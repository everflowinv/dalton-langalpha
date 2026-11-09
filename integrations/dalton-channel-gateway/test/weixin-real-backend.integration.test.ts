import { randomUUID } from "node:crypto";
import { type ChildProcess, spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { HttpBackendClient } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { WeixinChannelAdapter } from "../src/channels/weixin/index.js";
import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp, type GatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";

const EXPLICIT_BACKEND_URL = (
  process.env.GATEWAY_REAL_BACKEND_URL ?? ""
).trim();
const TEST_DB_DSN = (
  process.env.DALTON_CHANNEL_GATEWAY_TEST_DSN ??
  process.env.DALTON_TEST_DB_DSN ??
  process.env.TEST_DATABASE_URL ??
  ""
).trim();

const SHOULD_RUN = Boolean(EXPLICIT_BACKEND_URL || TEST_DB_DSN);

const ALLOWED_PUBLIC_QR_KEYS = new Set([
  "session_key",
  "status",
  "qrcode_url",
  "qrcode_img_content",
  "expires_at",
  "ilink_bot_id",
  "ilink_user_id",
  "ilink_username",
  "bot_generation",
  "connected",
  "already_connected",
  "verify_code_accepted",
  "need_rescan",
  "message",
]);

const ALLOWED_PUBLIC_BOT_KEYS = new Set([
  "configured",
  "status",
  "ilink_bot_id",
  "owner_user_id",
  "ilink_user_id",
  "ilink_username",
  "bot_generation",
  "updated_at",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJsonObject(
  resp: Response,
): Promise<Record<string, unknown>> {
  const raw: unknown = await resp.json();
  if (!isRecord(raw)) {
    throw new Error(`Expected JSON object, got ${typeof raw}`);
  }
  return raw;
}

function getArrayField(
  obj: Record<string, unknown>,
  key: string,
): Array<Record<string, unknown>> {
  const val = obj[key];
  if (!Array.isArray(val)) {
    return [];
  }
  const out: Array<Record<string, unknown>> = [];
  for (const item of val) {
    if (isRecord(item)) {
      out.push(item);
    }
  }
  return out;
}

describe.skipIf(!SHOULD_RUN)(
  "Weixin real HttpBackendClient -> FastAPI -> isolated *_test Postgres integration",
  () => {
    let backendUrl = EXPLICIT_BACKEND_URL;
    let serviceToken =
      (process.env.INTERNAL_SERVICE_TOKEN ?? "").trim() ||
      "test-weixin-internal-service-token-32b!!";
    let admin1 =
      (process.env.GATEWAY_TEST_ADMIN_USER_ID ?? "").trim() || "u-admin-1";
    let admin2 =
      (process.env.GATEWAY_TEST_ADMIN_2_USER_ID ?? "").trim() || "u-admin-2";
    let internalUser =
      (process.env.GATEWAY_TEST_INTERNAL_USER_ID ?? "").trim() ||
      "u-internal-1";
    let externalUser =
      (process.env.GATEWAY_TEST_EXTERNAL_USER_ID ?? "").trim() ||
      "u-external-1";
    let runPrefix =
      (process.env.GATEWAY_TEST_RUN_PREFIX ?? "").trim() || "wxint";

    let spawnedHarness: ChildProcess | null = null;
    let tempCacheDir = "";
    let gatewayApp: GatewayApp | null = null;
    let gatewayBaseUrl = "";
    let backendClient: HttpBackendClient | null = null;
    let weixinAdapter: WeixinChannelAdapter | null = null;

    // Controllable iLink upstream state
    let qrCounter = 0;
    const qrStatusMap = new Map<
      string,
      {
        status: string;
        bot_token?: string;
        ilink_bot_id?: string;
        baseurl?: string;
        ilink_user_id?: string;
        ilink_username?: string;
      }
    >();
    const verifyCodeSubmissions: Array<{
      qrcode: string;
      code: string;
    }> = [];
    const updatesQueue: Array<Record<string, unknown>> = [];
    const sentIlinkMessages: Array<Record<string, unknown>> = [];
    let failNextSendMessageWithNetworkDrop = false;

    // Controllable fault injection on backend HTTP client (simulating lost HTTP response after server commit)
    let dropNextI1ResponseForMessageId: string | null = null;
    let dropNextI9CursorCommitResponse = false;
    let droppedI1Count = 0;
    let droppedI9Count = 0;

    const ilinkFetch: typeof fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const parsed = new URL(url);

      if (parsed.pathname.endsWith("/ilink/bot/get_bot_qrcode")) {
        qrCounter += 1;
        const qrcode = `qr_${runPrefix}_${qrCounter}`;
        qrStatusMap.set(qrcode, { status: "wait" });
        return new Response(
          JSON.stringify({
            ret: 0,
            qrcode,
            qrcode_img_content: `https://ilinkai.weixin.qq.com/qr/${qrcode}?bot_token=secret_query_tok&secret=must_strip`,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }

      if (parsed.pathname.endsWith("/ilink/bot/get_qrcode_status")) {
        const qrcode = parsed.searchParams.get("qrcode") ?? "";
        const entry = qrStatusMap.get(qrcode) ?? { status: "wait" };
        return new Response(JSON.stringify({ ret: 0, ...entry }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (
        parsed.pathname.endsWith("/ilink/bot/verify_qrcode_code") ||
        parsed.pathname.endsWith("/ilink/bot/check_qrcode_verify_code") ||
        parsed.pathname.endsWith("/ilink/bot/submit_verify_code")
      ) {
        const bodyText =
          typeof init?.body === "string" ? init.body : "{}";
        const parsedBody: unknown = JSON.parse(bodyText);
        const qrcode =
          isRecord(parsedBody) && typeof parsedBody.qrcode === "string"
            ? parsedBody.qrcode
            : "";
        const code =
          isRecord(parsedBody) && typeof parsedBody.verify_code === "string"
            ? parsedBody.verify_code
            : isRecord(parsedBody) && typeof parsedBody.code === "string"
              ? parsedBody.code
              : "";
        verifyCodeSubmissions.push({ qrcode, code });
        const cur = qrcode ? qrStatusMap.get(qrcode) : undefined;
        // A missing qrcode or an empty code is not a confirmation. Spreading
        // the previous entry after `status` would keep `verify_code` and the
        // gateway would not call I-9 confirm.
        if (!qrcode || !cur || !code.trim()) {
          return new Response(
            JSON.stringify({ ret: 0, errcode: 0, status: "expired" }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        const next = { ...cur, status: "confirmed" };
        qrStatusMap.set(qrcode, next);
        return new Response(JSON.stringify({ ret: 0, errcode: 0, ...next }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (parsed.pathname.endsWith("/ilink/bot/getupdates")) {
        const bodyText =
          typeof init?.body === "string" ? init.body : "{}";
        const parsedReq: unknown = JSON.parse(bodyText);
        const reqCursor =
          isRecord(parsedReq) && typeof parsedReq.get_updates_buf === "string"
            ? parsedReq.get_updates_buf
            : "";

        // Pop any batch whose cursor has already been committed and sent by the poller
        while (
          updatesQueue.length > 0 &&
          typeof updatesQueue[0]?.get_updates_buf === "string" &&
          updatesQueue[0].get_updates_buf.length > 0 &&
          updatesQueue[0].get_updates_buf === reqCursor
        ) {
          updatesQueue.shift();
        }

        const next = updatesQueue[0];
        if (!next) {
          await new Promise((r) => setTimeout(r, 40));
          return new Response(
            JSON.stringify({ ret: 0, msgs: [], get_updates_buf: reqCursor }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        // Error responses without a new cursor (e.g. errcode -14) are consumed once
        if (
          typeof next.get_updates_buf !== "string" ||
          next.get_updates_buf.length === 0
        ) {
          updatesQueue.shift();
        }
        return new Response(JSON.stringify(next), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (parsed.pathname.endsWith("/ilink/bot/sendmessage")) {
        const bodyText =
          typeof init?.body === "string" ? init.body : "{}";
        const parsedBody: unknown = JSON.parse(bodyText);
        if (isRecord(parsedBody)) {
          sentIlinkMessages.push(parsedBody);
        }
        if (failNextSendMessageWithNetworkDrop) {
          failNextSendMessageWithNetworkDrop = false;
          throw new Error("Simulated TCP connection reset after sendmessage");
        }
        return new Response(JSON.stringify({ ret: 0, errcode: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      return new Response(JSON.stringify({ ret: -1, errmsg: "unhandled" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      });
    };

    const backendFetchWithFaultHooks: typeof fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const bodyStr = typeof init?.body === "string" ? init.body : "";

      // Execute the real HTTP call against FastAPI + Postgres first
      const realResp = await fetch(input, init);

      if (
        dropNextI1ResponseForMessageId &&
        url.includes("/api/v1/dalton/channels/internal/inbound/register-and-resolve") &&
        bodyStr.includes(dropNextI1ResponseForMessageId)
      ) {
        dropNextI1ResponseForMessageId = null;
        droppedI1Count += 1;
        throw new Error("Simulated network drop after I-1 server commit");
      }

      if (
        dropNextI9CursorCommitResponse &&
        url.includes("/api/v1/dalton/channels/internal/weixin-state") &&
        init?.method?.toUpperCase() === "PUT" &&
        bodyStr.includes("expected_get_updates_buf")
      ) {
        dropNextI9CursorCommitResponse = false;
        droppedI9Count += 1;
        throw new Error("Simulated network drop after I-9 cursor commit");
      }

      return realResp;
    };

    beforeAll(async () => {
      tempCacheDir = await mkdtemp(join(tmpdir(), "dalton-wx-real-int-"));

      if (!backendUrl) {
        const repoRoot = resolve(import.meta.dirname, "../../..");
        const proc = spawn(
          "uv",
          [
            "run",
            "python",
            "-m",
            "tests.integration.dalton.weixin_live_backend_harness",
          ],
          {
            cwd: repoRoot,
            env: {
              ...process.env,
              DALTON_CHANNEL_GATEWAY_TEST_DSN: TEST_DB_DSN,
            },
            stdio: ["pipe", "pipe", "inherit"],
          },
        );
        spawnedHarness = proc;

        const readyInfo = await new Promise<Record<string, unknown>>(
          (resolveReady, rejectReady) => {
            const timeout = setTimeout(() => {
              proc.kill("SIGKILL");
              rejectReady(
                new Error(
                  "Timed out waiting for weixin_live_backend_harness readiness JSON",
                ),
              );
            }, 30_000);

            proc.once("exit", (code) => {
              clearTimeout(timeout);
              rejectReady(
                new Error(
                  `weixin_live_backend_harness exited prematurely with code ${String(code)}`,
                ),
              );
            });

            if (!proc.stdout) {
              clearTimeout(timeout);
              rejectReady(new Error("Missing stdout on harness process"));
              return;
            }

            const rl = createInterface({ input: proc.stdout });
            rl.on("line", (line) => {
              const trimmed = line.trim();
              if (!trimmed.startsWith("{")) return;
              try {
                const parsed: unknown = JSON.parse(trimmed);
                if (isRecord(parsed) && parsed.ready === true) {
                  clearTimeout(timeout);
                  rl.close();
                  resolveReady(parsed);
                }
              } catch {
                // Ignore non-JSON log lines
              }
            });
          },
        );

        backendUrl = String(readyInfo.base_url);
        serviceToken = String(readyInfo.service_token);
        admin1 = String(readyInfo.admin_1);
        admin2 = String(readyInfo.admin_2);
        internalUser = String(readyInfo.internal_1);
        externalUser = String(readyInfo.external_1);
        runPrefix = String(readyInfo.prefix);
      }

      backendClient = new HttpBackendClient({
        baseUrl: backendUrl,
        serviceToken,
        fetchImpl: backendFetchWithFaultHooks,
      });

      weixinAdapter = new WeixinChannelAdapter({
        backend: backendClient,
        baseUrl: "https://ilinkai.weixin.qq.com",
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        fetchImpl: ilinkFetch,
        checkResolvedIps: false,
        pollTimeoutMs: 120,
        retryDelayMs: 40,
      });

      const adapters = new Map<ChannelPlatform, ChannelAdapter>([
        ["weixin", weixinAdapter],
      ]);

      const gatewayConfig: GatewayConfig = {
        host: "127.0.0.1",
        port: 0,
        serviceToken,
        backendUrl,
        webBaseUrl: "http://localhost:5173",
        weixinCacheDir: tempCacheDir,
        colleagueDispatchEnabled: true,
        channels: {
          feishu: { enabled: false, appId: "", appSecret: "" },
          discord: { enabled: false, botToken: "" },
          weixin: {
            enabled: true,
            apiBaseUrl: "https://ilinkai.weixin.qq.com",
            cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          },
        },
      };

      gatewayApp = createGatewayApp({
        config: gatewayConfig,
        backend: backendClient,
        adapters,
      });

      const liveGateway = gatewayApp;
      weixinAdapter.onInboundEvent(async (event) => {
        await liveGateway.orchestrator.handleInboundEvent(event);
      });

      const listenAddr = await gatewayApp.start();
      gatewayBaseUrl = `http://${listenAddr.host}:${listenAddr.port}`;

      // Wire FastAPI's CHANNEL_GATEWAY_URL to our live GatewayApp port
      const gwWireResp = await fetch(`${backendUrl}/test-control/gateway-url`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ gateway_url: gatewayBaseUrl }),
      });
      expect(gwWireResp.status).toBe(200);
    }, 45_000);

    afterAll(async () => {
      if (gatewayApp) {
        await gatewayApp.stop();
        gatewayApp = null;
      }
      if (spawnedHarness) {
        const proc = spawnedHarness;
        spawnedHarness = null;
        await new Promise<void>((resolveExit) => {
          const timer = setTimeout(() => {
            proc.kill("SIGKILL");
            resolveExit();
          }, 8_000);
          proc.once("exit", () => {
            clearTimeout(timer);
            resolveExit();
          });
          try {
            proc.stdin?.end();
          } catch {
            proc.kill("SIGTERM");
          }
        });
      }
      if (tempCacheDir) {
        await rm(tempCacheDir, { recursive: true, force: true });
      }
    }, 30_000);

    function removeQueuedCursor(cursor: string): void {
      for (let i = updatesQueue.length - 1; i >= 0; i -= 1) {
        if (updatesQueue[i]?.get_updates_buf === cursor) {
          updatesQueue.splice(i, 1);
        }
      }
    }

    function removeQueuedItem(item: Record<string, unknown>): void {
      const idx = updatesQueue.indexOf(item);
      if (idx >= 0) {
        updatesQueue.splice(idx, 1);
      }
    }

    function resetOutboundFaults(): void {
      dropNextI1ResponseForMessageId = null;
      dropNextI9CursorCommitResponse = false;
      failNextSendMessageWithNetworkDrop = false;
    }

    async function confirmBotViaVerify(params: {
      userId: string;
      botId: string;
      token: string;
      scannerId: string;
      username: string;
    }): Promise<void> {
      const startResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        { method: "POST", headers: { "X-User-Id": params.userId } },
      );
      expect(startResp.status).toBe(200);
      const startJson = await readJsonObject(startResp);
      const sessionKey = String(startJson.session_key ?? "");
      expect(sessionKey.length).toBeGreaterThan(0);
      const qrcode = `qr_${runPrefix}_${qrCounter}`;
      qrStatusMap.set(qrcode, {
        status: "verify_code",
        bot_token: params.token,
        ilink_bot_id: params.botId,
        baseurl: "https://ilinkai.weixin.qq.com",
        ilink_user_id: params.scannerId,
        ilink_username: params.username,
      });
      const verifyResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/verify-code`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-User-Id": params.userId,
          },
          body: JSON.stringify({
            session_key: sessionKey,
            verify_code: "654321",
          }),
        },
      );
      expect(verifyResp.status).toBe(200);
      const verifyJson = await readJsonObject(verifyResp);
      expect(verifyJson.status).toBe("confirmed");
      expect(verifyJson.ilink_bot_id).toBe(params.botId);
    }

    async function ensureActiveWeixinBot(
      backend: HttpBackendClient,
    ): Promise<string> {
      const state = await backend.getWeixinState();
      if (state.configured && state.status === "active" && state.ilink_bot_id) {
        return state.ilink_bot_id;
      }
      const botId = `wx_bot1_${runPrefix}@im.wechat`;
      await confirmBotViaVerify({
        userId: admin1,
        botId,
        token: `secret_bot1_token_${runPrefix}`,
        scannerId: `wx_scanner1_${runPrefix}@im.wechat`,
        username: "Admin1WX",
      });
      const after = await backend.getWeixinState();
      expect(after.status).toBe("active");
      expect(after.ilink_bot_id).toBe(botId);
      return botId;
    }

    async function loadInspect(): Promise<Record<string, unknown>> {
      const inspectResp = await fetch(`${backendUrl}/test-control/inspect`);
      expect(inspectResp.status).toBe(200);
      return readJsonObject(inspectResp);
    }

    async function ensureInternalUserHasContext(
      backend: HttpBackendClient,
      botId: string,
    ): Promise<void> {
      const before = await loadInspect();
      const alreadyLinked = getArrayField(before, "identities").find(
        (row) =>
          String(row.user_id ?? "") === internalUser &&
          row.has_encrypted_ctx === true,
      );
      if (alreadyLinked) {
        return;
      }

      const createCodeResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/bind-codes`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-User-Id": internalUser,
          },
          body: JSON.stringify({ platform: "weixin" }),
        },
      );
      expect(createCodeResp.status).toBe(200);
      const createCodeJson = await readJsonObject(createCodeResp);
      const bindCode = String(createCodeJson.code ?? "");
      expect(bindCode).toMatch(/^DLT-/);

      const platformUserId = `wx_alice_${runPrefix}@im.wechat`;
      const messageId = `${botId}:ctxprep_${runPrefix}`;
      const state = await backend.getWeixinState();
      await backend.registerAndResolveInbound({
        platform: "weixin",
        platform_user_id: platformUserId,
        chat_type: "dm",
        chat_address: "weixin:@me",
        thread_scope_key: `dm:${platformUserId}`,
        message_id: messageId,
        request_key: randomUUID(),
        raw_payload: {
          platform_user_id: platformUserId,
          ilink_bot_id: botId,
          seq: 45,
        },
        weixin_context_token: `ctx_prep_${runPrefix}`,
        weixin_bot_id: botId,
        weixin_bot_generation: state.bot_generation ?? null,
        weixin_context_seq: 45,
      });
      const consumed = await backend.consumeBindCode({
        action: "bind",
        platform: "weixin",
        platform_user_id: platformUserId,
        code: bindCode,
        is_group: false,
        inbound_message_id: messageId,
        weixin_context_token: `ctx_prep_${runPrefix}`,
        weixin_bot_id: botId,
        weixin_bot_generation: state.bot_generation ?? null,
        weixin_context_seq: 45,
        user_id: internalUser,
      });
      expect(consumed.ok).toBe(true);
      expect(consumed.user_id).toBe(internalUser);

      const after = await loadInspect();
      const linked = getArrayField(after, "identities").find(
        (row) => String(row.user_id ?? "") === internalUser,
      );
      expect(linked?.has_encrypted_ctx).toBe(true);
    }

    it("1) enforces admin permissions on QR start/wait/verify-code, resolves two-admin competition & mid-scan admin revocation, rejects expired/stale confirmations, handles duplicate confirm idempotently, and enforces public field whitelists", async () => {
      expect(backendClient).not.toBeNull();
      const backend = backendClient!;

      // 1a. Non-admin users (internal and external) are rejected with 403 on FastAPI public QR endpoints via real get_user_tier
      for (const nonAdmin of [internalUser, externalUser]) {
        const startForbidden = await fetch(
          `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
          {
            method: "POST",
            headers: { "X-User-Id": nonAdmin },
          },
        );
        expect(startForbidden.status).toBe(403);

        const waitForbidden = await fetch(
          `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=any`,
          {
            method: "GET",
            headers: { "X-User-Id": nonAdmin },
          },
        );
        expect(waitForbidden.status).toBe(403);

        const verifyForbidden = await fetch(
          `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/verify-code`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "X-User-Id": nonAdmin,
            },
            body: JSON.stringify({
              session_key: "any",
              verify_code: "123456",
            }),
          },
        );
        expect(verifyForbidden.status).toBe(403);
      }

      // Direct internal I-9 start by non-admin is also rejected by upsert_weixin_state's real get_user_tier check
      const nonAdminInternalStart = await backend.putWeixinState({
        login_session_action: "start",
        login_session_key: "sess-non-admin",
        login_owner_user_id: internalUser,
        login_status: "wait",
      });
      expect(nonAdminInternalStart.ok).toBe(false);
      expect(nonAdminInternalStart.updated).toBe(false);

      // 1b. Admin 1 starts QR session 1 via FastAPI public route -> Gateway -> iLink -> I-9 Postgres
      const start1Resp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(start1Resp.status).toBe(200);
      const start1Json = await readJsonObject(start1Resp);
      const sessionKey1 = String(start1Json.session_key ?? "");
      expect(sessionKey1.length).toBeGreaterThan(0);
      expect(start1Json.status).toBe("wait");

      // Public QR response whitelist check: no secret keys or query credentials
      for (const k of Object.keys(start1Json)) {
        expect(ALLOWED_PUBLIC_QR_KEYS.has(k)).toBe(true);
      }
      const start1Raw = JSON.stringify(start1Json);
      expect(start1Raw).not.toContain("bot_token");
      expect(start1Raw).not.toContain("context_token");
      expect(start1Raw).not.toContain("secret_query_tok");
      expect(start1Raw).not.toContain("must_strip");

      // 1c. Admin 2 starts QR session 2, superseding Admin 1's pending session 1
      const start2Resp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { "X-User-Id": admin2 },
        },
      );
      expect(start2Resp.status).toBe(200);
      const start2Json = await readJsonObject(start2Resp);
      const sessionKey2 = String(start2Json.session_key ?? "");
      expect(sessionKey2).not.toBe(sessionKey1);

      const botIdStale = `wx_stale_${runPrefix}@im.wechat`;
      const botIdRevoked = `wx_revoked_${runPrefix}@im.wechat`;
      qrStatusMap.set(`qr_${runPrefix}_1`, {
        status: "confirmed",
        bot_token: `tok_stale_${runPrefix}`,
        ilink_bot_id: botIdStale,
        baseurl: "https://ilinkai.weixin.qq.com",
        ilink_user_id: `wx_admin1_${runPrefix}@im.wechat`,
        ilink_username: "Admin1",
      });
      qrStatusMap.set(`qr_${runPrefix}_2`, {
        status: "confirmed",
        bot_token: `tok_revoked_${runPrefix}`,
        ilink_bot_id: botIdRevoked,
        baseurl: "https://ilinkai.weixin.qq.com",
        ilink_user_id: `wx_admin2_${runPrefix}@im.wechat`,
        ilink_username: "Admin2",
      });

      // 1d. Admin 1 polling superseded session 1 returns expired/connected=false and never saves botIdStale
      const wait1Resp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKey1)}`,
        {
          method: "GET",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(wait1Resp.status).toBe(200);
      const wait1Json = await readJsonObject(wait1Resp);
      expect(wait1Json.status).toBe("expired");
      expect(wait1Json.connected).toBe(false);
      const stateAfterStale1 = await backend.getWeixinState();
      expect(stateAfterStale1.configured).toBe(false);

      // 1e. Revoke Admin 2's admin permission mid-scan -> both FastAPI public route and Gateway internal confirm reject with 403
      await fetch(`${backendUrl}/test-control/tiers`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ admins: [admin1] }),
      });

      const wait2PublicForbidden = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKey2)}`,
        {
          method: "GET",
          headers: { "X-User-Id": admin2 },
        },
      );
      expect(wait2PublicForbidden.status).toBe(403);

      // Even if a request reached the Gateway directly right as admin2's role was revoked, I-9 confirm rejects with 403
      const wait2GatewayRevoked = await fetch(
        `${gatewayBaseUrl}/internal/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKey2)}&user_id=${encodeURIComponent(admin2)}`,
        {
          method: "GET",
          headers: {
            "X-Service-Token": serviceToken,
            "X-User-Id": admin2,
          },
        },
      );
      expect(wait2GatewayRevoked.status).toBe(403);
      const stateAfterRevoked = await backend.getWeixinState();
      expect(stateAfterRevoked.configured).toBe(false);

      // Restore Admin 2's admin permission
      await fetch(`${backendUrl}/test-control/tiers`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ admins: [admin1, admin2] }),
      });

      // 1f. Expired session confirmation is rejected
      const startExpResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(startExpResp.status).toBe(200);
      const startExpJson = await readJsonObject(startExpResp);
      const sessionKeyExp = String(startExpJson.session_key ?? "");
      qrStatusMap.set(`qr_${runPrefix}_3`, {
        status: "confirmed",
        bot_token: `tok_exp_${runPrefix}`,
        ilink_bot_id: `wx_exp_${runPrefix}@im.wechat`,
        baseurl: "https://ilinkai.weixin.qq.com",
      });
      await fetch(`${backendUrl}/test-control/expire-login-session`, {
        method: "POST",
      });
      const waitExpResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKeyExp)}`,
        {
          method: "GET",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(waitExpResp.status).toBe(200);
      const waitExpJson = await readJsonObject(waitExpResp);
      expect(waitExpJson.status).toBe("expired");
      expect(waitExpJson.connected).toBe(false);

      // 1g. Admin 1 starts session 4, receives need_verify_code, submits verify-code, and confirms Bot 1
      const botId1 = `wx_bot1_${runPrefix}@im.wechat`;
      const scanner1 = `wx_scanner1_${runPrefix}@im.wechat`;
      const start4Resp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(start4Resp.status).toBe(200);
      const start4Json = await readJsonObject(start4Resp);
      const sessionKey4 = String(start4Json.session_key ?? "");
      const loginVerBeforeConfirm =
        (await backend.getWeixinState()).login_version ?? 4;
      const expBotGenBeforeConfirm =
        (await backend.getWeixinState()).bot_generation ?? 1;

      qrStatusMap.set(`qr_${runPrefix}_4`, {
        status: "verify_code",
        bot_token: `secret_bot1_token_${runPrefix}`,
        ilink_bot_id: botId1,
        baseurl: "https://ilinkai.weixin.qq.com",
        ilink_user_id: scanner1,
        ilink_username: "Admin1WX",
      });

      const wait4NeedVerifyResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKey4)}`,
        {
          method: "GET",
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(wait4NeedVerifyResp.status).toBe(200);
      const wait4NeedVerifyJson = await readJsonObject(wait4NeedVerifyResp);
      expect(wait4NeedVerifyJson.status).toBe("verify_code");

      const verify4Resp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/verify-code`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-User-Id": admin1,
          },
          body: JSON.stringify({
            session_key: sessionKey4,
            verify_code: "654321",
          }),
        },
      );
      expect(verify4Resp.status).toBe(200);
      const verify4Json = await readJsonObject(verify4Resp);
      expect(verify4Json.status).toBe("confirmed");
      expect(verify4Json.connected).toBe(true);
      expect(verify4Json.ilink_bot_id).toBe(botId1);
      for (const k of Object.keys(verify4Json)) {
        expect(ALLOWED_PUBLIC_QR_KEYS.has(k)).toBe(true);
      }
      expect(JSON.stringify(verify4Json)).not.toContain("secret_bot1_token");
      expect(verifyCodeSubmissions.length).toBe(1);
      expect(verifyCodeSubmissions[0]?.code).toBe("654321");

      // Verify encrypted state in Postgres via I-9
      const stateBot1 = await backend.getWeixinState();
      expect(stateBot1.configured).toBe(true);
      expect(stateBot1.ilink_bot_id).toBe(botId1);
      expect(stateBot1.owner_user_id).toBe(admin1);
      expect(stateBot1.bot_token).toBe(`secret_bot1_token_${runPrefix}`);
      expect(stateBot1.status).toBe("active");
      expect(stateBot1.get_updates_buf).toBe("");
      const genBot1 = stateBot1.bot_generation ?? 0;
      expect(genBot1).toBeGreaterThanOrEqual(2);

      // Duplicate confirmation of the same sessionKey4 is idempotent and does not increment bot_generation
      const dupConfirm = await backend.putWeixinState({
        login_session_action: "confirm",
        login_session_key: sessionKey4,
        login_owner_user_id: admin1,
        login_version: loginVerBeforeConfirm,
        expected_bot_generation: expBotGenBeforeConfirm,
        ilink_bot_id: botId1,
        owner_user_id: admin1,
        bot_token: `secret_bot1_token_${runPrefix}`,
        base_url: "https://ilinkai.weixin.qq.com",
        status: "active",
        ilink_user_id: scanner1,
        ilink_username: "Admin1WX",
      });
      expect(dupConfirm.ok).toBe(true);
      expect(dupConfirm.idempotent).toBe(true);
      expect(dupConfirm.bot_generation).toBe(genBot1);

      // Public /bindings check for admin vs non-admin
      const adminBindingsResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/bindings`,
        {
          headers: { "X-User-Id": admin1 },
        },
      );
      expect(adminBindingsResp.status).toBe(200);
      const adminBindingsJson = await readJsonObject(adminBindingsResp);
      const rawAdminBindings = JSON.stringify(adminBindingsJson);
      expect(rawAdminBindings).not.toContain("secret_bot1_token");
      expect(rawAdminBindings).not.toContain("bot_token");
      expect(rawAdminBindings).not.toContain("context_token");
      expect(isRecord(adminBindingsJson.weixin_bot)).toBe(true);
      if (isRecord(adminBindingsJson.weixin_bot)) {
        for (const k of Object.keys(adminBindingsJson.weixin_bot)) {
          expect(ALLOWED_PUBLIC_BOT_KEYS.has(k)).toBe(true);
        }
      }

      const internalBindingsResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/bindings`,
        {
          headers: { "X-User-Id": internalUser },
        },
      );
      expect(internalBindingsResp.status).toBe(200);
      const internalBindingsJson = await readJsonObject(internalBindingsResp);
      expect(internalBindingsJson.weixin_bot).toBeNull();
    });

    it("2) processes inbound getupdates batch with I-1 and I-9 lost-response retry idempotency, durably rejects non-empty group_id without group reply or command execution, and CAS-commits cursor only after batch completion", async () => {
      expect(backendClient).not.toBeNull();
      const backend = backendClient!;
      const cursorBatch1 = `cursor_batch1_${runPrefix}`;
      try {
      updatesQueue.length = 0;
      sentIlinkMessages.length = 0;
      resetOutboundFaults();
      droppedI1Count = 0;
      droppedI9Count = 0;

      const botId1 = await ensureActiveWeixinBot(backend);
      const unlinkedUser = `wx_alice_${runPrefix}@im.wechat`;

      // Inject a lost HTTP response after FastAPI commits I-1 for message 9007199254740995
      // and a lost HTTP response after FastAPI commits I-9 cursorBatch1
      dropNextI1ResponseForMessageId = "9007199254740995";
      dropNextI9CursorCommitResponse = true;

      updatesQueue.push({
        ret: 0,
        get_updates_buf: cursorBatch1,
        msgs: [
          {
            message_id: "9007199254740993",
            seq: 5,
            from_user_id: unlinkedUser,
            group_id: `grp_${runPrefix}@chatroom`,
            context_token: `ctx_grp_must_ignore_${runPrefix}`,
            item_list: [{ type: 1, text_item: { text: "/new" } }],
          },
          {
            message_id: "9007199254740995",
            seq: 20,
            from_user_id: unlinkedUser,
            context_token: `ctx_seq20_${runPrefix}`,
            item_list: [{ type: 1, text_item: { text: "hello unlinked" } }],
          },
          {
            message_id: "9007199254740996",
            seq: 10,
            from_user_id: unlinkedUser,
            context_token: `ctx_stale_seq10_${runPrefix}`,
            item_list: [{ type: 1, text_item: { text: "older message" } }],
          },
        ],
      });

      const deadline = Date.now() + 4_000;
      let batchFinished = false;
      while (Date.now() < deadline) {
        const st = await backend.getWeixinState();
        if (st.get_updates_buf === cursorBatch1 && sentIlinkMessages.length >= 2) {
          batchFinished = true;
          break;
        }
        await new Promise((r) => setTimeout(r, 60));
      }
      if (!batchFinished) {
        const st = await backend.getWeixinState();
        const inspectJson = await loadInspect();
        const inboundIds = getArrayField(inspectJson, "inbound_events").map(
          (row) => String(row.message_id ?? ""),
        );
        const pending = {
          status: st.status,
          configured: st.configured,
          bot_id: st.ilink_bot_id ?? "",
          cursor: st.get_updates_buf,
          expected_cursor: cursorBatch1,
          sent_count: sentIlinkMessages.length,
          dropped_i1: droppedI1Count,
          dropped_i9: droppedI9Count,
          queue_depth: updatesQueue.length,
          inbound_message_ids: inboundIds,
        };
        expect(
          pending,
          `getupdates batch did not finish: ${JSON.stringify(pending)}`,
        ).toMatchObject({
          cursor: cursorBatch1,
          sent_count: 2,
          dropped_i1: 1,
          dropped_i9: 1,
        });
      }

      // Verify both fault-injection hooks fired and the monitor recovered via retry + I-9 read-back
      expect(droppedI1Count).toBe(1);
      expect(droppedI9Count).toBe(1);

      const stateAfter = await backend.getWeixinState();
      expect(stateAfter.get_updates_buf).toBe(cursorBatch1);

      // Stale CAS cursor commit with old expected_get_updates_buf ("") is rejected with cas_mismatch
      const staleCas = await backend.putWeixinState({
        ilink_bot_id: botId1,
        expected_bot_generation: stateAfter.bot_generation ?? 2,
        expected_get_updates_buf: "",
        get_updates_buf: "cursor_should_not_overwrite",
      });
      expect(staleCas.ok).toBe(false);
      expect(staleCas.cas_mismatch).toBe(true);
      expect((await backend.getWeixinState()).get_updates_buf).toBe(cursorBatch1);

      // Inspect real Postgres rows via /test-control/inspect
      const inspectResp = await fetch(`${backendUrl}/test-control/inspect`);
      const inspectJson = await readJsonObject(inspectResp);
      const inboundRows = getArrayField(inspectJson, "inbound_events");

      const grpEvent = inboundRows.find(
        (r) => r.message_id === `${botId1}:9007199254740993`,
      );
      expect(grpEvent).toBeDefined();
      expect(grpEvent?.status).toBe("ignored");

      const dm20Event = inboundRows.find(
        (r) => r.message_id === `${botId1}:9007199254740995`,
      );
      expect(dm20Event).toBeDefined();

      // Verify no group reply was sent, and both unlinked DM notices used `ctx_seq20_${runPrefix}` (never `ctx_grp_must_ignore` or `ctx_stale_seq10`)
      expect(sentIlinkMessages.length).toBe(2);
      for (const sent of sentIlinkMessages) {
        const msgObj = isRecord(sent.msg) ? sent.msg : {};
        expect(msgObj.to_user_id).toBe(unlinkedUser);
        expect(msgObj.context_token).toBe(`ctx_seq20_${runPrefix}`);
      }
      } finally {
        removeQueuedCursor(cursorBatch1);
        resetOutboundFaults();
      }
    }, 20_000);

    it("3) enforces monotonic context_token seq ordering, idempotent /bind consumption, normal DM turn with synthetic SSE, and outbound I-6/I-7 sent/denied/unknown (no auto-resend on lost sendmessage response)", async () => {
      expect(backendClient).not.toBeNull();
      const backend = backendClient!;
      const cursorBatch2 = `cursor_batch2_${runPrefix}`;
      const cursorBatch3 = `cursor_batch3_${runPrefix}`;
      try {
        updatesQueue.length = 0;
        sentIlinkMessages.length = 0;
        resetOutboundFaults();

        const botId1 = await ensureActiveWeixinBot(backend);
        const unlinkedUser = `wx_alice_${runPrefix}@im.wechat`;
        const groupId = `grp_${runPrefix}@chatroom`;
        const dmMessageId = `${botId1}:9007199254740995`;
        const groupMessageId = `${botId1}:9007199254740993`;
        const state = await backend.getWeixinState();

        await backend.registerAndResolveInbound({
          platform: "weixin",
          platform_user_id: unlinkedUser,
          chat_type: "dm",
          chat_address: "weixin:@me",
          thread_scope_key: `dm:${unlinkedUser}`,
          message_id: dmMessageId,
          request_key: randomUUID(),
          raw_payload: {
            platform_user_id: unlinkedUser,
            ilink_bot_id: botId1,
            seq: 20,
          },
          weixin_context_token: `ctx_seq20_${runPrefix}`,
          weixin_bot_id: botId1,
          weixin_bot_generation: state.bot_generation ?? null,
          weixin_context_seq: 20,
        });
        const groupRegistered = await backend.registerAndResolveInbound({
          platform: "weixin",
          platform_user_id: unlinkedUser,
          chat_type: "group",
          chat_address: `weixin:${groupId}`,
          thread_scope_key: `chat:${groupId}`,
          message_id: groupMessageId,
          request_key: randomUUID(),
          raw_payload: {
            platform_user_id: unlinkedUser,
            ilink_bot_id: botId1,
            group_id: groupId,
            seq: 5,
          },
          weixin_context_token: null,
          weixin_bot_id: botId1,
          weixin_bot_generation: state.bot_generation ?? null,
          weixin_context_seq: 5,
        });
        expect(groupRegistered.deny_reason).toBe("weixin_group_unsupported");

        const stored = await loadInspect();
        const inboundRows = getArrayField(stored, "inbound_events");
        const dmEvent = inboundRows.find((row) => row.message_id === dmMessageId);
        const grpEvent = inboundRows.find(
          (row) => row.message_id === groupMessageId,
        );
        expect(dmEvent?.platform).toBe("weixin");
        expect(dmEvent?.message_id).toBe(dmMessageId);
        expect(grpEvent?.platform).toBe("weixin");
        expect(grpEvent?.message_id).toBe(groupMessageId);
        expect(grpEvent?.status).toBe("ignored");
        const groupChatAddress = String(
          grpEvent?.chat_address ?? `weixin:${groupId}`,
        );
        expect(groupChatAddress).toBe(`weixin:${groupId}`);

        // 3a. Wrong notice target is denied. The stored DM still yields its token to the original conversation, and a stored group inbound is rejected as a group.
        const sentBeforeClaims = sentIlinkMessages.length;
        const mismatchClaim = await backend.authorizeAndClaimDelivery({
          delivery_key: `notice:${runPrefix}:mismatch`,
          kind: "notice",
          platform: "weixin",
          target: `weixin:other_${runPrefix}@im.wechat`,
          inbound_message_id: dmMessageId,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(mismatchClaim.claim_status).toBe("denied");
        expect(mismatchClaim.code).toBe("not_allowed");
        expect(mismatchClaim.message).toBe(
          "Notice target does not match original inbound conversation.",
        );
        expect(mismatchClaim.weixin_context_token ?? null).toBeNull();

        const matchedClaim = await backend.authorizeAndClaimDelivery({
          delivery_key: `notice:${runPrefix}:matched_dm`,
          kind: "notice",
          platform: "weixin",
          target: "weixin:@me",
          inbound_message_id: dmMessageId,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(matchedClaim.claim_status).toBe("claimed");
        expect(matchedClaim.weixin_context_token).toBe(`ctx_seq20_${runPrefix}`);

        const groupClaim = await backend.authorizeAndClaimDelivery({
          delivery_key: `notice:${runPrefix}:grp_deny`,
          kind: "notice",
          platform: "weixin",
          target: groupChatAddress,
          inbound_message_id: groupMessageId,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(groupClaim.claim_status).toBe("denied");
        expect(groupClaim.code).toBe("weixin_group_unsupported");
        expect(groupClaim.weixin_context_token ?? null).toBeNull();
        expect(sentIlinkMessages.length).toBe(sentBeforeClaims);

        // 3b. internalUser generates a Weixin bind code via public FastAPI endpoint, then unlinkedUser sends `/bind <CODE>`
        const createCodeResp = await fetch(
          `${backendUrl}/api/v1/dalton/channels/bind-codes`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "X-User-Id": internalUser,
            },
            body: JSON.stringify({ platform: "weixin" }),
          },
        );
        expect(createCodeResp.status).toBe(200);
        const createCodeJson = await readJsonObject(createCodeResp);
        const bindCode = String(createCodeJson.code ?? "");
        expect(bindCode).toMatch(/^DLT-/);

        sentIlinkMessages.length = 0;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: cursorBatch2,
          msgs: [
            {
              message_id: "9007199254740999",
              seq: 30,
              from_user_id: unlinkedUser,
              context_token: `ctx_bind_seq30_${runPrefix}`,
              item_list: [{ type: 1, text_item: { text: `/bind ${bindCode}` } }],
            },
          ],
        });

        const bindDeadline = Date.now() + 3_500;
        let bindFinished = false;
        while (Date.now() < bindDeadline) {
          const st = await backend.getWeixinState();
          if (
            st.get_updates_buf === cursorBatch2 &&
            sentIlinkMessages.length >= 1
          ) {
            bindFinished = true;
            break;
          }
          await new Promise((r) => setTimeout(r, 50));
        }
        if (!bindFinished) {
          const st = await backend.getWeixinState();
          const pending = {
            status: st.status,
            bot_id: st.ilink_bot_id ?? "",
            cursor: st.get_updates_buf,
            expected_cursor: cursorBatch2,
            sent_count: sentIlinkMessages.length,
            queue_depth: updatesQueue.length,
          };
          expect(
            pending,
            `bind batch did not finish: ${JSON.stringify(pending)}`,
          ).toMatchObject({
            cursor: cursorBatch2,
            sent_count: 1,
          });
        }

        expect(sentIlinkMessages.length).toBe(1);
        const bindReplyMsg = isRecord(sentIlinkMessages[0]?.msg)
          ? sentIlinkMessages[0].msg
          : {};
        expect(bindReplyMsg.context_token).toBe(`ctx_bind_seq30_${runPrefix}`);

        // Replaying I-2 consumeBindCode with the same inbound_message_id returns the cached idempotent result
        const bindReplay = await backend.consumeBindCode({
          action: "bind",
          platform: "weixin",
          platform_user_id: unlinkedUser,
          code: bindCode,
          is_group: false,
          inbound_message_id: `${botId1}:9007199254740999`,
          weixin_context_token: `ctx_bind_seq30_${runPrefix}`,
        });
        expect(bindReplay.ok).toBe(true);
        expect(bindReplay.user_id).toBe(internalUser);

        // 3c. Linked user sends normal DM -> dispatches turn to FastAPI SSE -> delivers reply via I-6/I-7 fallback
        sentIlinkMessages.length = 0;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: cursorBatch3,
          msgs: [
            {
              message_id: "9007199254741005",
              seq: 40,
              from_user_id: unlinkedUser,
              context_token: `ctx_dm_seq40_${runPrefix}`,
              item_list: [
                { type: 1, text_item: { text: "Check Q3 portfolio exposure" } },
              ],
            },
          ],
        });

        const dmDeadline = Date.now() + 4_000;
        let dmFinished = false;
        while (Date.now() < dmDeadline) {
          const st = await backend.getWeixinState();
          if (
            st.get_updates_buf === cursorBatch3 &&
            sentIlinkMessages.length >= 1
          ) {
            dmFinished = true;
            break;
          }
          await new Promise((r) => setTimeout(r, 60));
        }
        if (!dmFinished) {
          const st = await backend.getWeixinState();
          const pending = {
            status: st.status,
            bot_id: st.ilink_bot_id ?? "",
            cursor: st.get_updates_buf,
            expected_cursor: cursorBatch3,
            sent_count: sentIlinkMessages.length,
            queue_depth: updatesQueue.length,
          };
          expect(
            pending,
            `dm batch did not finish: ${JSON.stringify(pending)}`,
          ).toMatchObject({
            cursor: cursorBatch3,
            sent_count: 1,
          });
        }

        expect(sentIlinkMessages.length).toBe(1);
        const dmReplyMsg = isRecord(sentIlinkMessages[0]?.msg)
          ? sentIlinkMessages[0].msg
          : {};
        expect(dmReplyMsg.to_user_id).toBe(unlinkedUser);
        expect(dmReplyMsg.context_token).toBe(`ctx_dm_seq40_${runPrefix}`);

        // 3d. Outbound /agent/send when iLink sendmessage drops the HTTP response marks delivery `unknown` and does NOT auto-resend
        sentIlinkMessages.length = 0;
        failNextSendMessageWithNetworkDrop = true;
        const agentSendResp = await fetch(`${gatewayBaseUrl}/agent/send`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-Service-Token": serviceToken,
            "X-User-Id": internalUser,
          },
          body: JSON.stringify({
            thread_id: randomUUID(),
            run_id: `agent_send:${runPrefix}`,
            tool_call_id: "drop1",
            turn_platform: "weixin",
            target: "weixin:@me",
            text: "Alert: rebalance threshold reached",
          }),
        });
        expect(agentSendResp.status).toBe(200);
        const agentSendJson = await readJsonObject(agentSendResp);
        expect(agentSendJson.status).toBe("unknown");
        expect(agentSendJson.code).toBe("unavailable");
        // Verify sendmessage was called exactly once (never auto-resent on unknown network drop)
        expect(sentIlinkMessages.length).toBe(1);

        const inspectJson = await loadInspect();
        const deliveryRows = getArrayField(inspectJson, "deliveries");
        const unknownRow = deliveryRows.find(
          (d) => d.delivery_key === `agent_send:${runPrefix}:drop1`,
        );
        expect(unknownRow).toBeDefined();
        expect(unknownRow?.status).toBe("unknown");
      } finally {
        removeQueuedCursor(cursorBatch2);
        removeQueuedCursor(cursorBatch3);
        resetOutboundFaults();
      }
    }, 30_000);

    it("4) switches Bot account with bot_generation advancement & context_token invalidation, durably marks -14 session expired for current generation while ignoring stale generation, and cancels cleanly on stop()", async () => {
      expect(backendClient).not.toBeNull();
      expect(weixinAdapter).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      const backend = backendClient!;
      const expiredUpdate: Record<string, unknown> = {
        ret: 0,
        errcode: -14,
        errmsg: "session expired",
      };
      try {
        updatesQueue.length = 0;
        sentIlinkMessages.length = 0;
        resetOutboundFaults();

        const botId1 = await ensureActiveWeixinBot(backend);
        await ensureInternalUserHasContext(backend, botId1);
        const stateGen1 = await backend.getWeixinState();
        const gen1 = stateGen1.bot_generation ?? 2;

        // 4a. Admin 2 starts a new QR session and confirms Bot 2 -> advances bot_generation, resets cursor, invalidates old context_tokens
        const botId2 = `wx_bot2_${runPrefix}@im.wechat`;
        const scanner2 = `wx_scanner2_${runPrefix}@im.wechat`;

        const startBot2Resp = await fetch(
          `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
          {
            method: "POST",
            headers: { "X-User-Id": admin2 },
          },
        );
        expect(startBot2Resp.status).toBe(200);
        const startBot2Json = await readJsonObject(startBot2Resp);
        const sessionKeyBot2 = String(startBot2Json.session_key ?? "");

        qrStatusMap.set(`qr_${runPrefix}_${qrCounter}`, {
          status: "confirmed",
          bot_token: `secret_bot2_token_${runPrefix}`,
          ilink_bot_id: botId2,
          baseurl: "https://ilinkai.weixin.qq.com",
          ilink_user_id: scanner2,
          ilink_username: "Admin2WX",
        });

        const waitBot2Resp = await fetch(
          `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKeyBot2)}`,
          {
            method: "GET",
            headers: { "X-User-Id": admin2 },
          },
        );
        expect(waitBot2Resp.status).toBe(200);
        const waitBot2Json = await readJsonObject(waitBot2Resp);
        expect(waitBot2Json.status).toBe("confirmed");
        expect(waitBot2Json.connected).toBe(true);
        expect(waitBot2Json.ilink_bot_id).toBe(botId2);

        const stateGen2 = await backend.getWeixinState();
        const gen2 = stateGen2.bot_generation ?? 0;
        expect(gen2).toBe(gen1 + 1);
        expect(stateGen2.get_updates_buf).toBe("");
        expect(stateGen2.status).toBe("active");

        // 4b. Old context_tokens from botId1 / gen1 were invalidated on Bot switch
        const inspectAfterSwitch = await loadInspect();
        const remainingCtxTokens = getArrayField(
          inspectAfterSwitch,
          "context_tokens",
        );
        expect(remainingCtxTokens.length).toBe(0);

        // Sending to the linked user fails because the bot switch cleared the context_token. The claim is refused before any ilink send.
        sentIlinkMessages.length = 0;
        const sendWithoutCtxResp = await fetch(`${gatewayBaseUrl}/agent/send`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-Service-Token": serviceToken,
            "X-User-Id": internalUser,
          },
          body: JSON.stringify({
            thread_id: randomUUID(),
            run_id: `agent_send:${runPrefix}:no_ctx`,
            turn_platform: "weixin",
            target: "weixin:@me",
            text: "Should not send without Bot 2 context_token",
          }),
        });
        expect(sendWithoutCtxResp.status).toBe(200);
        const sendWithoutCtxJson = await readJsonObject(sendWithoutCtxResp);
        expect(sendWithoutCtxJson.status).toBe("failed");
        expect(sendWithoutCtxJson.code).toBe("missing_weixin_context_token");
        expect(String(sendWithoutCtxJson.message ?? "")).toMatch(/context_token/i);
        expect(sentIlinkMessages.length).toBe(0);

        // Stale -14 expiry report from gen1 is rejected and keeps Bot 2 active
        const staleExp = await backend.putWeixinState({
          ilink_bot_id: botId1,
          expected_bot_generation: gen1,
          status: "expired",
        });
        expect(staleExp.ok).toBe(false);
        expect(staleExp.stale_generation).toBe(true);
        expect((await backend.getWeixinState()).status).toBe("active");

        // 4c. Bot 2 receives errcode -14 on getupdates -> durably marks status = 'expired' in Postgres and pauses sending
        updatesQueue.push(expiredUpdate);

        const expDeadline = Date.now() + 3_500;
        let sawExpired = false;
        while (Date.now() < expDeadline) {
          const st = await backend.getWeixinState();
          if (st.status === "expired") {
            sawExpired = true;
            break;
          }
          await new Promise((r) => setTimeout(r, 50));
        }
        if (!sawExpired) {
          const st = await backend.getWeixinState();
          const pending = {
            status: st.status,
            bot_generation: st.bot_generation ?? 0,
            queue_depth: updatesQueue.length,
          };
          expect(
            pending,
            `bot 2 did not expire: ${JSON.stringify(pending)}`,
          ).toMatchObject({ status: "expired" });
        }

        const stateExpired = await backend.getWeixinState();
        expect(stateExpired.status).toBe("expired");
        expect(stateExpired.bot_generation).toBe(gen2);

        const liveCheck = await weixinAdapter!.checkTargetLive({
          userId: admin2,
          address: "weixin:@me",
          purpose: "agent_messages",
        });
        expect(liveCheck.ok).toBe(false);
        expect(liveCheck.message).toMatch(/not currently connected/i);

        // 4d. Stopping GatewayApp cleanly cancels monitor and background tasks without overwriting expired state
        await gatewayApp!.stop();
        gatewayApp = null;
        const stateAfterStop = await backend.getWeixinState();
        expect(stateAfterStop.status).toBe("expired");
        expect(stateAfterStop.bot_generation).toBe(gen2);
      } finally {
        removeQueuedItem(expiredUpdate);
        resetOutboundFaults();
      }
    }, 30_000);
  },
);
