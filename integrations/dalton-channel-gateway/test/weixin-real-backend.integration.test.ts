import { randomUUID } from "node:crypto";
import { type ChildProcess, spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { HttpBackendClient } from "../src/backend-client.js";
import {
  ChannelDeliveryError,
  type ChannelAdapter,
} from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { WeixinChannelAdapter } from "../src/channels/weixin/index.js";
import type { GatewayConfig } from "../src/config.js";
import { createGatewayApp, type GatewayApp } from "../src/server.js";
import type { ChannelPlatform, InboundChannelEvent } from "../src/types.js";

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

function requestAuthorization(init: RequestInit | undefined): string {
  const headers = init?.headers;
  if (!headers) return "";
  if (headers instanceof Headers) return headers.get("authorization") ?? "";
  if (Array.isArray(headers)) {
    const found = headers.find(([key]) => key.toLowerCase() === "authorization");
    return found ? String(found[1]) : "";
  }
  const record = headers as Record<string, string>;
  const match = Object.entries(record).find(
    ([key]) => key.toLowerCase() === "authorization",
  );
  return match ? String(match[1]) : "";
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
    let discordAdapter: FakeChannelAdapter | null = null;
    const discordOutbound: Array<{ text: string; inboundMessageId: string | null }> = [];
    let discordSendMode: "ok" | "unknown" | "rate_limited" = "ok";

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
    let nextSendMessageReturnsNull = false;
    let nextSendMessageRateLimited = false;

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
          sentIlinkMessages.push({
            ...parsedBody,
            authorization: requestAuthorization(init),
          });
        }
        if (failNextSendMessageWithNetworkDrop) {
          failNextSendMessageWithNetworkDrop = false;
          throw new Error("Simulated TCP connection reset after sendmessage");
        }
        if (nextSendMessageReturnsNull) {
          nextSendMessageReturnsNull = false;
          return new Response("null", {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (nextSendMessageRateLimited) {
          nextSendMessageRateLimited = false;
          return new Response(JSON.stringify({ error: "too many requests" }), {
            status: 429,
            headers: {
              "content-type": "application/json",
              "retry-after": "1",
            },
          });
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

      await startGatewayFromDatabase();
    }, 45_000);

    async function startGatewayFromDatabase(): Promise<void> {
      if (gatewayApp) {
        await gatewayApp.stop();
        gatewayApp = null;
      }
      const backend = backendClient;
      if (!backend) {
        throw new Error("Backend client is not ready");
      }
      weixinAdapter = new WeixinChannelAdapter({
        backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        fetchImpl: ilinkFetch,
        checkResolvedIps: false,
        pollTimeoutMs: 120,
        retryDelayMs: 40,
      });
      discordAdapter = new FakeChannelAdapter("discord");
      discordAdapter.setSendTextBehavior(async (options) => {
        discordOutbound.push({
          text: options.text,
          inboundMessageId: options.inboundMessageId ?? null,
        });
        if (discordSendMode === "unknown") {
          discordSendMode = "ok";
          throw new ChannelDeliveryError("discord response lost after dispatch", {
            outcome: "unknown",
            code: "unavailable",
          });
        }
        if (discordSendMode === "rate_limited") {
          discordSendMode = "ok";
          throw new ChannelDeliveryError("discord rate limited", {
            outcome: "rate_limited",
            code: "rate_limited",
            retryAfterMs: 1000,
          });
        }
        return { messageId: `discord_out_${discordOutbound.length}` };
      });
      const adapters = new Map<ChannelPlatform, ChannelAdapter>([
        ["weixin", weixinAdapter],
        ["discord", discordAdapter],
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
        backend,
        adapters,
      });
      const liveGateway = gatewayApp;
      weixinAdapter.onInboundEvent(async (event) => {
        await liveGateway.orchestrator.handleInboundEvent(event);
      });
      discordAdapter.onInboundEvent(async (event) => {
        await liveGateway.orchestrator.handleInboundEvent(event);
      });
      const listenAddr = await gatewayApp.start();
      gatewayBaseUrl = `http://${listenAddr.host}:${listenAddr.port}`;
      const gwWireResp = await fetch(`${backendUrl}/test-control/gateway-url`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ gateway_url: gatewayBaseUrl }),
      });
      expect(gwWireResp.status).toBe(200);
      await gatewayApp.orchestrator.waitForStartupRecovery();
    }

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
      nextSendMessageReturnsNull = false;
      nextSendMessageRateLimited = false;
      discordSendMode = "ok";
    }

    function ilinkMessageText(row: Record<string, unknown>): string {
      const msg = isRecord(row.msg) ? row.msg : null;
      if (!msg || !Array.isArray(msg.item_list)) return "";
      return msg.item_list
        .map((item) => {
          if (!isRecord(item) || !isRecord(item.text_item)) return "";
          return typeof item.text_item.text === "string" ? item.text_item.text : "";
        })
        .join("");
    }

    function countIlinkText(needle: string): number {
      return sentIlinkMessages.filter((row) => ilinkMessageText(row).includes(needle)).length;
    }

    async function waitForCount(
      read: () => number,
      expected: number,
      label: string,
    ): Promise<void> {
      const deadline = Date.now() + 6_000;
      let seen = read();
      while (Date.now() < deadline && seen < expected) {
        await new Promise((resolveWait) => setTimeout(resolveWait, 40));
        seen = read();
      }
      expect(seen, label).toBe(expected);
    }

    async function waitForCursor(cursor: string): Promise<void> {
      const deadline = Date.now() + 6_000;
      let current = "";
      while (Date.now() < deadline) {
        const state = await backendClient!.getWeixinState();
        current = state.get_updates_buf ?? "";
        if (current === cursor) return;
        await new Promise((resolveWait) => setTimeout(resolveWait, 40));
      }
      expect(current).toBe(cursor);
    }

    function inboundEvent(params: {
      platform: "weixin" | "discord";
      platformUserId: string;
      messageId: string;
      text: string;
    }): InboundChannelEvent {
      return {
        platform: params.platform,
        platformUserId: params.platformUserId,
        chatType: "dm",
        chatAddress: `${params.platform}:@me`,
        chatId: params.platformUserId,
        threadScopeKey: `dm:${params.platformUserId}`,
        messageId: params.messageId,
        text: params.text,
      };
    }

    function countScannerText(sender: string, needle: string): number {
      return sentIlinkMessages.filter((row) => {
        const msg = isRecord(row.msg) ? row.msg : null;
        return msg?.to_user_id === sender && ilinkMessageText(row).includes(needle);
      }).length;
    }

    async function waitForScannerText(
      sender: string,
      needles: readonly string[],
    ): Promise<string> {
      const deadline = Date.now() + 6_000;
      while (Date.now() < deadline) {
        for (const needle of needles) {
          if (countScannerText(sender, needle) > 0) return needle;
        }
        await new Promise((resolveWait) => setTimeout(resolveWait, 40));
      }
      const seen = needles
        .map((needle) => `${countScannerText(sender, needle)}:${needle}`)
        .join(" | ");
      expect(seen, `reply to ${sender}`).toBe("received");
      return needles[0] ?? "";
    }

    async function createPlatformBindCode(
      userId: string,
      platform: "weixin" | "discord",
    ): Promise<string> {
      const resp = await fetch(`${backendUrl}/api/v1/dalton/channels/bind-codes`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "X-User-Id": userId,
        },
        body: JSON.stringify({ platform }),
      });
      expect(resp.status).toBe(200);
      const body = await readJsonObject(resp);
      const code = String(body.code ?? "");
      expect(code).toMatch(/^DLT-/);
      return code;
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

    it("3e) persists an HTTP 200 JSON null send as unknown and does not send it again", async () => {
      expect(backendClient).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      const backend = backendClient!;
      const probe = `Alert probe null json ${runPrefix}`;
      const deliveryKey = `agent_send:${runPrefix}:nullshape:null1`;
      try {
        sentIlinkMessages.length = 0;
        resetOutboundFaults();
        const botId = await ensureActiveWeixinBot(backend);
        await ensureInternalUserHasContext(backend, botId);
        nextSendMessageReturnsNull = true;
        const body = {
          thread_id: randomUUID(),
          run_id: `agent_send:${runPrefix}:nullshape`,
          tool_call_id: "null1",
          turn_platform: "weixin",
          target: "weixin:@me",
          text: probe,
        };
        const first = await fetch(`${gatewayBaseUrl}/agent/send`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-Service-Token": serviceToken,
            "X-User-Id": internalUser,
          },
          body: JSON.stringify(body),
        });
        expect(first.status).toBe(200);
        const firstJson = await readJsonObject(first);
        expect(firstJson.status).toBe("unknown");
        expect(firstJson.code).toBe("unavailable");
        expect(countIlinkText(probe)).toBe(1);

        const second = await fetch(`${gatewayBaseUrl}/agent/send`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "X-Service-Token": serviceToken,
            "X-User-Id": internalUser,
          },
          body: JSON.stringify(body),
        });
        expect(second.status).toBe(200);
        const secondJson = await readJsonObject(second);
        expect(secondJson.status).toBe("unknown");
        await gatewayApp!.orchestrator.runStartupRecovery();
        expect(countIlinkText(probe)).toBe(1);
        const inspectJson = await loadInspect();
        const unknownRow = getArrayField(inspectJson, "deliveries").find(
          (row) => row.delivery_key === deliveryKey,
        );
        expect(unknownRow?.status).toBe("unknown");
      } finally {
        resetOutboundFaults();
      }
    }, 20_000);

    it("3f) confirms an invalid Weixin bind and an unlinked unbind once across an unknown result and a 429", async () => {
      expect(backendClient).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      const backend = backendClient!;
      const bindCursor = `cursor_r11_bind_${runPrefix}`;
      const bindReplayCursor = `cursor_r11_bind_replay_${runPrefix}`;
      const unbindCursor = `cursor_r11_unbind_${runPrefix}`;
      const bindSender = `wx_badbind_${runPrefix}@im.wechat`;
      const unbindSender = `wx_unlinked_${runPrefix}@im.wechat`;
      const bindText = "Bind code is invalid or expired.";
      const unbindText = "Your weixin account is not currently linked.";
      try {
        updatesQueue.length = 0;
        sentIlinkMessages.length = 0;
        resetOutboundFaults();
        const botId = await ensureActiveWeixinBot(backend);
        const before = await loadInspect();
        const internalIdentity = getArrayField(before, "identities").find(
          (row) => String(row.user_id ?? "") === internalUser,
        );

        nextSendMessageReturnsNull = true;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: bindCursor,
          msgs: [
            {
              message_id: "9100000000001001",
              seq: 70,
              from_user_id: bindSender,
              context_token: `ctx_r11_bind_${runPrefix}`,
              item_list: [{ type: 1, text_item: { text: "/bind NOTACODE" } }],
            },
          ],
        });
        await waitForCount(() => countIlinkText(bindText), 1, "invalid bind confirmation");
        await waitForCursor(bindCursor);
        const bindMessageId = `${botId}:9100000000001001`;
        let inspectJson = await loadInspect();
        const bindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === bindMessageId,
        );
        const bindResult = isRecord(bindRow?.consumed_bind_result)
          ? bindRow.consumed_bind_result
          : {};
        expect(bindResult.ok).toBe(false);
        expect(bindResult.action).toBe("bind");
        expect(bindResult.code).toBeUndefined();
        expect(bindResult.bind_code).toBeUndefined();
        expect(bindResult.delivery_key).toBe(`notice:weixin:${bindMessageId}:bind_err`);
        expect(bindRow?.command_reply_pending).toBe(true);
        expect(bindRow?.dalton_user_id ?? null).toBeNull();
        const bindDelivery = getArrayField(inspectJson, "deliveries").find(
          (row) => row.delivery_key === bindResult.delivery_key,
        );
        expect(bindDelivery?.status).toBe("unknown");

        updatesQueue.push({
          ret: 0,
          get_updates_buf: bindReplayCursor,
          msgs: [
            {
              message_id: "9100000000001001",
              seq: 70,
              from_user_id: bindSender,
              context_token: `ctx_r11_bind_${runPrefix}`,
              item_list: [{ type: 1, text_item: { text: "/bind NOTACODE" } }],
            },
          ],
        });
        await waitForCursor(bindReplayCursor);
        expect(countIlinkText(bindText)).toBe(1);

        nextSendMessageRateLimited = true;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: unbindCursor,
          msgs: [
            {
              message_id: "9100000000001002",
              seq: 71,
              from_user_id: unbindSender,
              context_token: `ctx_r11_unbind_${runPrefix}`,
              item_list: [{ type: 1, text_item: { text: "/unbind" } }],
            },
          ],
        });
        await waitForCount(() => countIlinkText(unbindText), 1, "unlinked unbind 429");
        await waitForCursor(unbindCursor);
        const unbindMessageId = `${botId}:9100000000001002`;
        inspectJson = await loadInspect();
        const unbindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === unbindMessageId,
        );
        const unbindResult = isRecord(unbindRow?.consumed_bind_result)
          ? unbindRow.consumed_bind_result
          : {};
        expect(unbindResult.ok).toBe(false);
        expect(unbindResult.action).toBe("unbind");
        expect(unbindResult.delivery_key).toBe(
          `notice:weixin:${unbindMessageId}:unbind_err`,
        );
        expect(unbindRow?.command_reply_pending).toBe(true);
        const unbindDelivery = getArrayField(inspectJson, "deliveries").find(
          (row) => row.delivery_key === unbindResult.delivery_key,
        );
        expect(unbindDelivery?.status).toBe("failed");

        await gatewayApp!.orchestrator.runStartupRecovery();
        await waitForCount(() => countIlinkText(unbindText), 2, "unlinked unbind recovery");
        expect(countIlinkText(bindText)).toBe(1);
        inspectJson = await loadInspect();
        const unbindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === unbindMessageId,
        );
        expect(unbindAfter?.status).toBe("completed");
        expect(unbindAfter?.command_reply_pending).toBe(false);
        expect(unbindAfter?.command_effect_status).toBe("done");
        const unbindSent = getArrayField(inspectJson, "deliveries").find(
          (row) => row.delivery_key === unbindResult.delivery_key,
        );
        expect(unbindSent?.status).toBe("sent");
        const bindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === bindMessageId,
        );
        expect(bindAfter?.command_reply_pending).toBe(true);
        const identities = getArrayField(inspectJson, "identities");
        expect(
          identities.some((row) => String(row.platform_user_id ?? "") === bindSender),
        ).toBe(false);
        expect(
          identities.some((row) => String(row.platform_user_id ?? "") === unbindSender),
        ).toBe(false);
        expect(
          identities.find((row) => String(row.user_id ?? "") === internalUser),
        ).toMatchObject({
          platform_user_id: internalIdentity?.platform_user_id,
        });
      } finally {
        removeQueuedCursor(bindCursor);
        removeQueuedCursor(bindReplayCursor);
        removeQueuedCursor(unbindCursor);
        resetOutboundFaults();
      }
    }, 30_000);

    it("3g) confirms an invalid Discord bind and an unlinked unbind through the shared delivery key", async () => {
      expect(discordAdapter).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      const bindSender = `discord_badbind_${runPrefix}`;
      const unbindSender = `discord_unlinked_${runPrefix}`;
      const bindMessageId = `discord_bind_${runPrefix}`;
      const unbindMessageId = `discord_unbind_${runPrefix}`;
      const bindText = "Bind code is invalid or expired.";
      const unbindText = "Your discord account is not currently linked.";
      const beforeDiscord = discordOutbound.length;
      try {
        resetOutboundFaults();
        discordSendMode = "unknown";
        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: bindSender,
            messageId: bindMessageId,
            text: "/bind NOTACODE",
          }),
        );
        const bindSends = discordOutbound.slice(beforeDiscord);
        expect(bindSends).toHaveLength(1);
        expect(bindSends[0]?.text).toContain(bindText);
        expect(bindSends[0]?.inboundMessageId).toBe(bindMessageId);
        let inspectJson = await loadInspect();
        const bindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === bindMessageId,
        );
        const bindResult = isRecord(bindRow?.consumed_bind_result)
          ? bindRow.consumed_bind_result
          : {};
        expect(bindResult.ok).toBe(false);
        expect(bindResult.delivery_key).toBe(`notice:discord:${bindMessageId}:bind_err`);
        expect(bindResult.code).toBeUndefined();
        expect(bindRow?.command_reply_pending).toBe(true);
        expect(bindRow?.dalton_user_id ?? null).toBeNull();
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === bindResult.delivery_key,
          )?.status,
        ).toBe("unknown");

        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: bindSender,
            messageId: bindMessageId,
            text: "/bind NOTACODE",
          }),
        );
        expect(discordOutbound.slice(beforeDiscord)).toHaveLength(1);

        discordSendMode = "rate_limited";
        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: unbindSender,
            messageId: unbindMessageId,
            text: "/unbind",
          }),
        );
        const afterRateLimit = discordOutbound.slice(beforeDiscord);
        expect(afterRateLimit).toHaveLength(2);
        expect(afterRateLimit[1]?.text).toContain(unbindText);
        inspectJson = await loadInspect();
        const unbindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === unbindMessageId,
        );
        const unbindResult = isRecord(unbindRow?.consumed_bind_result)
          ? unbindRow.consumed_bind_result
          : {};
        expect(unbindResult.ok).toBe(false);
        expect(unbindResult.delivery_key).toBe(
          `notice:discord:${unbindMessageId}:unbind_err`,
        );
        expect(unbindRow?.command_reply_pending).toBe(true);
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === unbindResult.delivery_key,
          )?.status,
        ).toBe("failed");

        await gatewayApp!.orchestrator.runStartupRecovery();
        const afterRecovery = discordOutbound.slice(beforeDiscord);
        expect(afterRecovery.filter((row) => row.text.includes(bindText))).toHaveLength(1);
        expect(afterRecovery.filter((row) => row.text.includes(unbindText))).toHaveLength(2);
        inspectJson = await loadInspect();
        const unbindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === unbindMessageId,
        );
        expect(unbindAfter?.status).toBe("completed");
        expect(unbindAfter?.command_reply_pending).toBe(false);
        expect(unbindAfter?.command_effect_status).toBe("done");
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === unbindResult.delivery_key,
          )?.status,
        ).toBe("sent");
        const bindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === bindMessageId,
        );
        expect(bindAfter?.command_reply_pending).toBe(true);
        expect(bindAfter?.dalton_user_id ?? null).toBeNull();
        expect(unbindAfter?.dalton_user_id ?? null).toBeNull();
      } finally {
        resetOutboundFaults();
      }
    }, 20_000);

    it("3h) saves slash-command context on a fresh login and ignores a prior generation", async () => {
      expect(backendClient).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      const backend = backendClient!;
      const commands = [
        { text: "/new", needles: ["Started a new conversation thread"], seq: 31 },
        {
          text: "/main",
          needles: ["Keeping current conversation", "started a new thread"],
          seq: 32,
        },
        { text: "/workspace", needles: ["Available workspaces:"], seq: 33 },
        { text: "/stop", needles: ["No active conversation thread to stop."], seq: 34 },
      ] as const;
      const cursors: string[] = [];
      let lastBotId = "";
      let lastGeneration = 0;
      let lastSender = "";
      try {
        updatesQueue.length = 0;
        sentIlinkMessages.length = 0;
        resetOutboundFaults();
        for (let index = 0; index < commands.length; index += 1) {
          const command = commands[index]!;
          const botId = `wx_cmd${index}_${runPrefix}@im.wechat`;
          const sender = `wx_cmd_sender${index}_${runPrefix}@im.wechat`;
          const token = `ctx_cmd_${index}_${runPrefix}_secret`;
          const replayToken = `ctx_cmd_replay_${index}_${runPrefix}_secret`;
          const rawId = `930000000000${index}`;
          const cursor = `cursor_cmd_${index}_${runPrefix}`;
          const replayCursor = `cursor_cmd_replay_${index}_${runPrefix}`;
          cursors.push(cursor, replayCursor);
          await confirmBotViaVerify({
            userId: admin1,
            botId,
            token: `secret_cmd_${index}_${runPrefix}`,
            scannerId: sender,
            username: `Cmd${index}`,
          });
          await gatewayApp!.orchestrator.syncWeixinStateCache();
          const state = await backend.getWeixinState(false);
          const generation = state.bot_generation ?? 0;
          expect(state.ilink_bot_id).toBe(botId);
          expect(state.status).toBe("active");
          const before = await loadInspect();
          expect(
            getArrayField(before, "context_tokens").some(
              (row) => row.ilink_bot_id === botId && row.platform_user_id === sender,
            ),
          ).toBe(false);

          updatesQueue.push({
            ret: 0,
            get_updates_buf: cursor,
            msgs: [
              {
                message_id: rawId,
                seq: command.seq,
                from_user_id: sender,
                context_token: token,
                item_list: [{ type: 1, text_item: { text: command.text } }],
              },
            ],
          });
          const needle = await waitForScannerText(sender, command.needles);
          await waitForCursor(cursor);
          expect(countScannerText(sender, needle)).toBe(1);
          const messageId = `${botId}:${rawId}`;
          let inspectJson = await loadInspect();
          expect(JSON.stringify(inspectJson)).not.toContain(token);
          const inbound = getArrayField(inspectJson, "inbound_events").find(
            (row) => row.message_id === messageId,
          );
          expect(inbound?.dalton_user_id).toBe(admin1);
          expect(inbound?.status).toBe("completed");
          expect(inbound?.command_reply_pending).toBe(false);
          expect(inbound?.command_effect_status).toBe("done");
          expect(JSON.stringify(inbound?.raw_payload ?? {})).not.toContain(token);
          const ctx = getArrayField(inspectJson, "context_tokens").find(
            (row) => row.ilink_bot_id === botId && row.platform_user_id === sender,
          );
          expect(Number(ctx?.seq)).toBe(command.seq);
          expect(Number(ctx?.bot_generation)).toBe(generation);
          const identity = getArrayField(inspectJson, "identities").find(
            (row) => row.platform_user_id === sender,
          );
          expect(identity?.user_id).toBe(admin1);
          expect(identity?.has_encrypted_ctx).toBe(true);
          expect(Number(identity?.weixin_context_seq)).toBe(command.seq);
          expect(identity?.weixin_context_bot_id).toBe(botId);
          expect(Number(identity?.weixin_context_bot_generation)).toBe(generation);
          const ownerState = await backend.getChannelState({ user_id: admin1 });
          expect(ownerState.identities.weixin?.weixin_context_token).toBe(token);
          const otherState = await backend.getChannelState({ user_id: admin2 });
          expect(JSON.stringify(otherState)).not.toContain(token);

          updatesQueue.push({
            ret: 0,
            get_updates_buf: replayCursor,
            msgs: [
              {
                message_id: rawId,
                seq: 1,
                from_user_id: sender,
                context_token: replayToken,
                item_list: [{ type: 1, text_item: { text: command.text } }],
              },
            ],
          });
          await waitForCursor(replayCursor);
          expect(countScannerText(sender, needle)).toBe(1);
          inspectJson = await loadInspect();
          expect(JSON.stringify(inspectJson)).not.toContain(replayToken);
          const replayCtx = getArrayField(inspectJson, "context_tokens").find(
            (row) => row.ilink_bot_id === botId && row.platform_user_id === sender,
          );
          expect(Number(replayCtx?.seq)).toBe(command.seq);
          const replayOwner = await backend.getChannelState({ user_id: admin1 });
          expect(replayOwner.identities.weixin?.weixin_context_token).toBe(token);
          lastBotId = botId;
          lastGeneration = generation;
          lastSender = sender;
        }

        const nextBotId = `wx_cmd_after_${runPrefix}@im.wechat`;
        const staleToken = `stale_ctx_${runPrefix}_secret`;
        await confirmBotViaVerify({
          userId: admin1,
          botId: nextBotId,
          token: `secret_cmd_after_${runPrefix}`,
          scannerId: lastSender,
          username: "CmdAfter",
        });
        await gatewayApp!.orchestrator.syncWeixinStateCache();
        const nextState = await backend.getWeixinState({
          include_token: true,
          recipient_platform_user_id: lastSender,
        });
        expect(nextState.ilink_bot_id).toBe(nextBotId);
        expect(nextState.bot_generation).toBe(lastGeneration + 1);
        expect(nextState.weixin_context_token ?? null).toBeNull();
        const staleMessageId = `${lastBotId}:stale_${runPrefix}`;
        await gatewayApp!.orchestrator.handleInboundEvent({
          platform: "weixin",
          platformUserId: lastSender,
          chatType: "dm",
          chatAddress: "weixin:@me",
          chatId: lastSender,
          threadScopeKey: `dm:${lastSender}`,
          messageId: staleMessageId,
          text: "/new",
          commandText: "/new",
          weixinContextToken: staleToken,
          weixinBotId: lastBotId,
          weixinBotGeneration: lastGeneration,
          weixinContextSeq: 4,
          rawPayload: {
            platform_user_id: lastSender,
            ilink_bot_id: lastBotId,
            bot_generation: lastGeneration,
            seq: 4,
            text: "/new",
          },
        });
        const afterStale = await backend.getWeixinState({
          include_token: true,
          recipient_platform_user_id: lastSender,
        });
        expect(afterStale.ilink_bot_id).toBe(nextBotId);
        expect(afterStale.bot_generation).toBe(lastGeneration + 1);
        expect(afterStale.weixin_context_token ?? null).toBeNull();
        const inspectJson = await loadInspect();
        expect(
          getArrayField(inspectJson, "context_tokens").some(
            (row) => row.platform_user_id === lastSender,
          ),
        ).toBe(false);
        expect(JSON.stringify(inspectJson)).not.toContain(staleToken);
        const staleRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === staleMessageId,
        );
        expect(staleRow?.status).toBe("ignored");
        expect(staleRow?.command_reply_pending).toBe(false);
        expect(countScannerText(lastSender, "Started a new conversation thread")).toBe(0);
      } finally {
        for (const cursor of cursors) removeQueuedCursor(cursor);
        resetOutboundFaults();
      }
    }, 60_000);

    it("3i) keeps one linked bind and unbind when confirmation delivery is interrupted", async () => {
      expect(backendClient).not.toBeNull();
      expect(gatewayApp).not.toBeNull();
      expect(discordAdapter).not.toBeNull();
      const backend = backendClient!;
      const weixinSender = `wx_linked_${runPrefix}@im.wechat`;
      const discordSender = `discord_linked_${runPrefix}`;
      const weixinBindRawId = "9400000000001001";
      const weixinUnbindRawId = "9400000000001002";
      const bindCursor = `cursor_linked_bind_${runPrefix}`;
      const bindReplayCursor = `cursor_linked_bind_replay_${runPrefix}`;
      const unbindCursor = `cursor_linked_unbind_${runPrefix}`;
      const weixinBindToken = `ctx_linked_bind_${runPrefix}`;
      const weixinUnbindToken = `ctx_linked_unbind_${runPrefix}`;
      const weixinBindText = "Successfully linked your weixin account to Dalton.";
      const weixinUnbindText = "Unlinked your weixin account from Dalton.";
      const discordBindText = "Successfully linked your discord account to Dalton.";
      const discordUnbindText = "Unlinked your discord account from Dalton.";
      const discordBindMessageId = `discord_linked_bind_${runPrefix}`;
      const discordUnbindMessageId = `discord_linked_unbind_${runPrefix}`;
      const discordStart = discordOutbound.length;
      try {
        updatesQueue.length = 0;
        sentIlinkMessages.length = 0;
        resetOutboundFaults();
        const botId = await ensureActiveWeixinBot(backend);
        await gatewayApp!.orchestrator.syncWeixinStateCache();
        const weixinBindMessageId = `${botId}:${weixinBindRawId}`;
        const weixinUnbindMessageId = `${botId}:${weixinUnbindRawId}`;
        const weixinCode = await createPlatformBindCode(externalUser, "weixin");

        nextSendMessageReturnsNull = true;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: bindCursor,
          msgs: [
            {
              message_id: weixinBindRawId,
              seq: 81,
              from_user_id: weixinSender,
              context_token: weixinBindToken,
              item_list: [{ type: 1, text_item: { text: `/bind ${weixinCode}` } }],
            },
          ],
        });
        await waitForScannerText(weixinSender, [weixinBindText]);
        await waitForCursor(bindCursor);
        expect(countScannerText(weixinSender, weixinBindText)).toBe(1);
        expect(ilinkMessageText(
          sentIlinkMessages.find((row) => {
            const msg = isRecord(row.msg) ? row.msg : null;
            return msg?.to_user_id === weixinSender;
          }) ?? {},
        )).not.toContain(weixinCode);
        let inspectJson = await loadInspect();
        let bindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === weixinBindMessageId,
        );
        let bindResult = isRecord(bindRow?.consumed_bind_result)
          ? bindRow.consumed_bind_result
          : {};
        expect(bindResult.ok).toBe(true);
        expect(bindResult.action).toBe("bind");
        expect(bindResult.user_id).toBe(externalUser);
        expect(bindResult.code).toBeUndefined();
        expect(bindResult.bind_code).toBeUndefined();
        expect(bindResult.delivery_key).toBe(
          `notice:weixin:${weixinBindMessageId}:bind_ok`,
        );
        expect(bindRow?.command_reply_pending).toBe(true);
        expect(bindRow?.dalton_user_id).toBe(externalUser);
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === bindResult.delivery_key,
          )?.status,
        ).toBe("unknown");
        let linkedState = await backend.getChannelState({ user_id: externalUser });
        expect(linkedState.identities.weixin?.platform_user_id).toBe(weixinSender);

        updatesQueue.push({
          ret: 0,
          get_updates_buf: bindReplayCursor,
          msgs: [
            {
              message_id: weixinBindRawId,
              seq: 81,
              from_user_id: weixinSender,
              context_token: weixinBindToken,
              item_list: [{ type: 1, text_item: { text: `/bind ${weixinCode}` } }],
            },
          ],
        });
        await waitForCursor(bindReplayCursor);
        expect(countScannerText(weixinSender, weixinBindText)).toBe(1);
        linkedState = await backend.getChannelState({ user_id: externalUser });
        expect(linkedState.identities.weixin?.platform_user_id).toBe(weixinSender);

        nextSendMessageRateLimited = true;
        updatesQueue.push({
          ret: 0,
          get_updates_buf: unbindCursor,
          msgs: [
            {
              message_id: weixinUnbindRawId,
              seq: 82,
              from_user_id: weixinSender,
              context_token: weixinUnbindToken,
              item_list: [{ type: 1, text_item: { text: "/unbind" } }],
            },
          ],
        });
        await waitForScannerText(weixinSender, [weixinUnbindText]);
        await waitForCursor(unbindCursor);
        expect(countScannerText(weixinSender, weixinUnbindText)).toBe(1);
        inspectJson = await loadInspect();
        let unbindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === weixinUnbindMessageId,
        );
        let unbindResult = isRecord(unbindRow?.consumed_bind_result)
          ? unbindRow.consumed_bind_result
          : {};
        expect(unbindResult.ok).toBe(true);
        expect(unbindResult.action).toBe("unbind");
        expect(unbindResult.user_id).toBe(externalUser);
        expect(unbindResult.delivery_key).toBe(
          `notice:weixin:${weixinUnbindMessageId}:unbind_ok`,
        );
        expect(unbindRow?.command_reply_pending).toBe(true);
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === unbindResult.delivery_key,
          )?.status,
        ).toBe("failed");
        expect(
          getArrayField(inspectJson, "identities").some(
            (row) => row.platform_user_id === weixinSender,
          ),
        ).toBe(false);
        linkedState = await backend.getChannelState({ user_id: externalUser });
        expect(linkedState.identities.weixin ?? null).toBeNull();

        const discordCode = await createPlatformBindCode(admin2, "discord");
        discordSendMode = "unknown";
        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: discordSender,
            messageId: discordBindMessageId,
            text: `/bind ${discordCode}`,
          }),
        );
        let discordSends = discordOutbound.slice(discordStart);
        expect(discordSends.filter((row) => row.text.includes(discordBindText))).toHaveLength(1);
        expect(discordSends[0]?.text).not.toContain(discordCode);
        expect(discordSends[0]?.inboundMessageId).toBe(discordBindMessageId);
        inspectJson = await loadInspect();
        bindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === discordBindMessageId,
        );
        bindResult = isRecord(bindRow?.consumed_bind_result)
          ? bindRow.consumed_bind_result
          : {};
        expect(bindResult.ok).toBe(true);
        expect(bindResult.delivery_key).toBe(
          `notice:discord:${discordBindMessageId}:bind_ok`,
        );
        expect(bindResult.code).toBeUndefined();
        expect(bindRow?.command_reply_pending).toBe(true);
        expect(bindRow?.dalton_user_id).toBe(admin2);
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === bindResult.delivery_key,
          )?.status,
        ).toBe("unknown");
        expect(
          (await backend.getChannelState({ user_id: admin2 })).identities.discord
            ?.platform_user_id,
        ).toBe(discordSender);

        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: discordSender,
            messageId: discordBindMessageId,
            text: `/bind ${discordCode}`,
          }),
        );
        expect(
          discordOutbound.slice(discordStart).filter((row) => row.text.includes(discordBindText)),
        ).toHaveLength(1);

        discordSendMode = "rate_limited";
        await discordAdapter!.emitInbound(
          inboundEvent({
            platform: "discord",
            platformUserId: discordSender,
            messageId: discordUnbindMessageId,
            text: "/unbind",
          }),
        );
        discordSends = discordOutbound.slice(discordStart);
        expect(discordSends.filter((row) => row.text.includes(discordUnbindText))).toHaveLength(1);
        inspectJson = await loadInspect();
        unbindRow = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === discordUnbindMessageId,
        );
        unbindResult = isRecord(unbindRow?.consumed_bind_result)
          ? unbindRow.consumed_bind_result
          : {};
        expect(unbindResult.ok).toBe(true);
        expect(unbindResult.delivery_key).toBe(
          `notice:discord:${discordUnbindMessageId}:unbind_ok`,
        );
        expect(unbindRow?.command_reply_pending).toBe(true);
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === unbindResult.delivery_key,
          )?.status,
        ).toBe("failed");
        expect(
          (await backend.getChannelState({ user_id: admin2 })).identities.discord ?? null,
        ).toBeNull();

        // A failed confirmation is reclaimable. Replaying it here would send the
        // reply on this process and leave nothing for the restarted gateway.
        resetOutboundFaults();
        const activeBot = await backend.getWeixinState(true);
        await startGatewayFromDatabase();
        expect(countScannerText(weixinSender, weixinBindText)).toBe(1);
        expect(countScannerText(weixinSender, weixinUnbindText)).toBe(2);
        const recovered = sentIlinkMessages.filter((row) => {
          const msg = isRecord(row.msg) ? row.msg : null;
          return (
            msg?.to_user_id === weixinSender &&
            ilinkMessageText(row).includes(weixinUnbindText)
          );
        });
        expect(recovered).toHaveLength(2);
        expect(recovered[1]?.authorization).toBe(`Bearer ${activeBot.bot_token}`);
        const recoveredMsg = isRecord(recovered[1]?.msg) ? recovered[1].msg : null;
        expect(recoveredMsg?.to_user_id).toBe(weixinSender);
        expect(recoveredMsg?.context_token).toBe(weixinUnbindToken);
        discordSends = discordOutbound.slice(discordStart);
        expect(discordSends.filter((row) => row.text.includes(discordBindText))).toHaveLength(1);
        const discordUnbinds = discordSends.filter((row) =>
          row.text.includes(discordUnbindText),
        );
        expect(discordUnbinds).toHaveLength(2);
        expect(discordUnbinds[1]?.inboundMessageId).toBe(discordUnbindMessageId);

        inspectJson = await loadInspect();
        const weixinBindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === weixinBindMessageId,
        );
        const weixinUnbindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === weixinUnbindMessageId,
        );
        expect(weixinBindAfter?.command_reply_pending).toBe(true);
        expect(weixinUnbindAfter?.status).toBe("completed");
        expect(weixinUnbindAfter?.command_reply_pending).toBe(false);
        expect(weixinUnbindAfter?.command_effect_status).toBe("done");
        const weixinBindResultAfter = isRecord(weixinBindAfter?.consumed_bind_result)
          ? weixinBindAfter.consumed_bind_result
          : {};
        const weixinUnbindResultAfter = isRecord(weixinUnbindAfter?.consumed_bind_result)
          ? weixinUnbindAfter.consumed_bind_result
          : {};
        expect(weixinBindResultAfter.ok).toBe(true);
        expect(weixinBindResultAfter.action).toBe("bind");
        expect(weixinBindResultAfter.code).toBeUndefined();
        expect(weixinUnbindResultAfter.ok).toBe(true);
        expect(weixinUnbindResultAfter.action).toBe("unbind");
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === `notice:weixin:${weixinBindMessageId}:bind_ok`,
          )?.status,
        ).toBe("unknown");
        expect(
          getArrayField(inspectJson, "deliveries").find(
            (row) => row.delivery_key === `notice:weixin:${weixinUnbindMessageId}:unbind_ok`,
          )?.status,
        ).toBe("sent");
        const discordBindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === discordBindMessageId,
        );
        const discordUnbindAfter = getArrayField(inspectJson, "inbound_events").find(
          (row) => row.message_id === discordUnbindMessageId,
        );
        expect(discordBindAfter?.command_reply_pending).toBe(true);
        expect(discordUnbindAfter?.status).toBe("completed");
        expect(discordUnbindAfter?.command_reply_pending).toBe(false);
        expect(discordUnbindAfter?.command_effect_status).toBe("done");
        const discordBindResultAfter = isRecord(discordBindAfter?.consumed_bind_result)
          ? discordBindAfter.consumed_bind_result
          : {};
        const discordUnbindResultAfter = isRecord(discordUnbindAfter?.consumed_bind_result)
          ? discordUnbindAfter.consumed_bind_result
          : {};
        expect(discordBindResultAfter.ok).toBe(true);
        expect(discordBindResultAfter.action).toBe("bind");
        expect(discordBindResultAfter.code).toBeUndefined();
        expect(discordUnbindResultAfter.ok).toBe(true);
        expect(discordUnbindResultAfter.action).toBe("unbind");
        expect(
          (await backend.getChannelState({ user_id: externalUser })).identities.weixin ??
            null,
        ).toBeNull();
        expect(
          (await backend.getChannelState({ user_id: admin2 })).identities.discord ?? null,
        ).toBeNull();
        expect(
          getArrayField(inspectJson, "identities").some(
            (row) => row.platform_user_id === weixinSender,
          ),
        ).toBe(false);
      } finally {
        removeQueuedCursor(bindCursor);
        removeQueuedCursor(bindReplayCursor);
        removeQueuedCursor(unbindCursor);
        resetOutboundFaults();
      }
    }, 45_000);

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
