import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { delimiter, resolve } from "node:path";
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
  "login_version",
  "account_id",
  "account_status",
  "accounts_revision",
  "pending_verification",
  "revoked",
  "code",
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

describe.skipIf(!SHOULD_RUN)(
  "Weixin multi-account real HttpBackendClient -> FastAPI -> isolated *_test Postgres integration",
  () => {
    let backendUrl = EXPLICIT_BACKEND_URL;
    let serviceToken =
      (process.env.INTERNAL_SERVICE_TOKEN ?? "").trim() ||
      "test-weixin-internal-service-token-32b!!";
    let internalUser =
      (process.env.GATEWAY_TEST_INTERNAL_USER_ID ?? "").trim() ||
      "u-internal-1";
    let runPrefix = `wx_real_${Date.now()}`;
    let spawnedHarness: ChildProcess | null = null;
    let backendClient: HttpBackendClient | null = null;
    let gatewayApp: GatewayApp | null = null;
    let weixinAdapter: WeixinChannelAdapter | null = null;
    let gatewayBaseUrl = "";

    let qrSeq = 0;
    const qrStatusQueue: Array<Record<string, unknown>> = [];
    const updatesQueue: Array<Record<string, unknown>> = [];
    const sentIlinkMessages: Array<Record<string, unknown>> = [];

    const ilinkFetch: typeof fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const bodyStr = typeof init?.body === "string" ? init.body : "";
      const parsedBody: Record<string, unknown> = bodyStr
        ? (JSON.parse(bodyStr) as Record<string, unknown>)
        : {};

      if (url.includes("/ilink/bot/get_bot_qrcode")) {
        qrSeq += 1;
        return new Response(
          JSON.stringify({
            ret: 0,
            qrcode: `qr_token_${runPrefix}_${qrSeq}`,
            qrcode_img_content: `https://ilinkai.weixin.qq.com/qr/${runPrefix}_${qrSeq}`,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }

      if (url.includes("/ilink/bot/get_qrcode_status")) {
        const next = qrStatusQueue.shift() ?? { ret: 0, status: "wait" };
        return new Response(JSON.stringify(next), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (url.includes("/ilink/bot/getupdates")) {
        if (updatesQueue.length === 0) {
          await new Promise<void>((resolveDelay, rejectDelay) => {
            const signal = init?.signal;
            if (signal?.aborted) {
              rejectDelay(
                signal.reason ??
                  new DOMException("The operation was aborted", "AbortError"),
              );
              return;
            }
            const timer = setTimeout(() => {
              signal?.removeEventListener("abort", onAbort);
              resolveDelay();
            }, 50);
            const onAbort = () => {
              clearTimeout(timer);
              rejectDelay(
                signal?.reason ??
                  new DOMException("The operation was aborted", "AbortError"),
              );
            };
            signal?.addEventListener("abort", onAbort, { once: true });
          });
        }
        const next = updatesQueue.shift() ?? {
          ret: 0,
          msgs: [],
          get_updates_buf: parsedBody.get_updates_buf ?? "",
        };
        return new Response(JSON.stringify(next), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (url.includes("/ilink/bot/sendmessage")) {
        sentIlinkMessages.push(parsedBody);
        return new Response(
          JSON.stringify({
            ret: 0,
            message_id: `wx_sent_${sentIlinkMessages.length}`,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }

      return new Response(JSON.stringify({ ret: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    };

    beforeAll(async () => {
      if (!backendUrl) {
        const repoRoot = resolve(import.meta.dirname, "../../..");
        const venvPython = resolve(repoRoot, ".venv/bin/python");
        const pythonBin =
          process.env.DALTON_TEST_PYTHON_BIN?.trim() ||
          (existsSync(venvPython) ? venvPython : "python3");
        const proc = spawn(
          pythonBin,
          [
            "-m",
            "tests.integration.dalton.weixin_live_backend_harness",
          ],
          {
            cwd: repoRoot,
            env: {
              ...process.env,
              DALTON_CHANNEL_GATEWAY_TEST_DSN: TEST_DB_DSN,
              PYTHONPATH: process.env.PYTHONPATH
                ? `${repoRoot}${delimiter}${process.env.PYTHONPATH}`
                : repoRoot,
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
        internalUser = String(readyInfo.internal_1);
        runPrefix = String(readyInfo.prefix);
      }

      backendClient = new HttpBackendClient({
        baseUrl: backendUrl,
        serviceToken,
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
        staggerDelayMs: 0,
        staggerJitterMs: 0,
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
        colleagueDispatchEnabled: true,
        channels: {
          feishu: { enabled: false, appId: "", appSecret: "" },
          discord: { enabled: false, botToken: "", applicationId: "" },
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
      const listenAddr = await gatewayApp.start();
      gatewayBaseUrl = `http://${listenAddr.host}:${listenAddr.port}`;
      const gwWireResp = await fetch(`${backendUrl}/test-control/gateway-url`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ gateway_url: gatewayBaseUrl }),
      });
      expect(gwWireResp.status).toBe(200);
      await gatewayApp.orchestrator.waitForStartupRecovery();
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
    }, 30_000);

    it("1) acquires deployment lease, reconciles accounts, and completes QR login with public whitelist", async () => {
      const backend = backendClient!;
      expect(backend.getGatewayEpoch()).toBeGreaterThan(0);

      const startResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { "X-User-Id": internalUser },
        },
      );
      expect(startResp.status).toBe(200);
      const startBody = await readJsonObject(startResp);
      for (const key of Object.keys(startBody)) {
        expect(ALLOWED_PUBLIC_QR_KEYS.has(key)).toBe(true);
      }
      const sessionKey = String(startBody.session_key ?? "");
      expect(sessionKey.length).toBeGreaterThan(0);

      qrStatusQueue.push({
        ret: 0,
        status: "confirmed",
        bot_token: `bot_tok_${runPrefix}`,
        ilink_bot_id: `bot_${runPrefix}`,
        ilink_user_id: `wx_user_${runPrefix}`,
        baseurl: "https://ilinkai.weixin.qq.com",
      });

      const waitResp = await fetch(
        `${backendUrl}/api/v1/dalton/channels/weixin/qrcode/wait?session_key=${encodeURIComponent(sessionKey)}`,
        {
          method: "GET",
          headers: { "X-User-Id": internalUser },
        },
      );
      expect(waitResp.status).toBe(200);
      const waitBody = await readJsonObject(waitResp);
      expect(waitBody.status).toBe("confirmed");
      expect(waitBody).not.toHaveProperty("bot_token");

      const rec = await backend.reconcileWeixinAccounts(null);
      expect(rec.enabled).toBe(true);
      expect(
        rec.accounts.some((a) => a.ilink_bot_id === `bot_${runPrefix}`),
      ).toBe(true);
    }, 30_000);
  },
);
