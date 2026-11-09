import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { HttpBackendClient } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import {
  DiscordChannelAdapter,
  RequestClient,
} from "../src/channels/discord/index.js";
import { loadConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";
import { fetchGatewayServer } from "./helpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "../../..");

function resolveTestDsn(): string | null {
  for (const key of [
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
  ]) {
    const val = process.env[key]?.trim();
    if (val) {
      const parsed = new URL(val);
      const dbName = parsed.pathname.replace(/^\/+/, "");
      if (!dbName.endsWith("_test")) {
        throw new Error(
          `Refusing to run real HTTP + Postgres test against non-test DB "${dbName}" from ${key}`,
        );
      }
      return val;
    }
  }
  return null;
}

function resolvePythonBin(): string {
  const candidates = [
    process.env.DALTON_TEST_PYTHON_BIN,
    resolve(REPO_ROOT, ".venv/bin/python"),
    "python3",
  ];
  for (const cand of candidates) {
    if (cand && (cand === "python3" || existsSync(cand))) {
      return cand;
    }
  }
  return "python3";
}

interface BootstrapState {
  prefix: string;
  service_token: string;
  user_a: string;
  user_b: string;
  home_ws_a: string;
  ws_a1: string;
  ws_b_shared_rw: string;
  ws_b_private: string;
  puid_a: string;
  puid_b: string;
  guild_id: string;
  parent_cid: string;
  thread_cid: string;
  other_cid: string;
  private_b_cid: string;
  unauth_cid: string;
  parent_addr: string;
  other_allowed_addr: string;
  private_b_addr: string;
  unauthorized_addr: string;
  thread_scope_key: string;
}

interface InspectResponse {
  inbound: {
    message_id: string;
    status: string;
    dalton_user_id: string | null;
    chat_address: string;
    thread_scope_key: string;
    workspace_id: string | null;
    external_thread_id: string | null;
    thread_id: string | null;
    run_id: string | null;
    fallback_pending: boolean;
    sent_to_current_count: number;
  } | null;
  delivery: {
    delivery_key: string;
    kind: string;
    user_id: string | null;
    thread_id: string | null;
    run_id: string | null;
    is_current: boolean;
    platform: string;
    target_address: string;
    status: string;
    sent_chunks: number;
    total_chunks: number;
    platform_message_ids: string[];
    files_status: unknown;
    error_code: string | null;
  } | null;
  background_run: {
    run_id: string;
    thread_id: string;
    dalton_user_id: string;
    platform: string;
    target_address: string;
    turn_index: number;
    status: string;
    fallback_pending: boolean;
    sent_to_current_count: number;
  } | null;
}

async function startOrAttachRealBackend(): Promise<{
  baseUrl: string;
  stop: () => Promise<void>;
}> {
  const preconfiguredUrl = process.env.DALTON_REAL_BACKEND_URL?.trim();
  if (preconfiguredUrl) {
    return {
      baseUrl: preconfiguredUrl.replace(/\/+$/, ""),
      stop: async () => {},
    };
  }

  const dsn = resolveTestDsn();
  if (!dsn) {
    throw new Error(
      "Neither DALTON_REAL_BACKEND_URL nor DALTON_CHANNEL_GATEWAY_TEST_DSN is set",
    );
  }

  const pyBin = resolvePythonBin();
  const scriptPath = resolve(
    REPO_ROOT,
    "tests/integration/dalton/test_discord_gateway_db.py",
  );

  let proc: ChildProcess | null = null;
  try {
    proc = spawn(pyBin, [scriptPath, "--serve-http-harness"], {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        DALTON_CHANNEL_GATEWAY_TEST_DSN: dsn,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });

    const childProc = proc;
    const baseUrl = await new Promise<string>((resolveUrl, rejectUrl) => {
      let stdoutBuf = "";
      let stderrBuf = "";
      const timer = setTimeout(() => {
        rejectUrl(
          new Error(
            `Timed out waiting for HARNESS_READY.\nstdout: ${stdoutBuf}\nstderr: ${stderrBuf}`,
          ),
        );
      }, 30_000);

      childProc.stdout?.on("data", (chunk: Buffer) => {
        stdoutBuf += chunk.toString("utf8");
        const match = /HARNESS_READY\s+(http:\/\/127\.0\.0\.1:\d+)/.exec(
          stdoutBuf,
        );
        if (match?.[1]) {
          clearTimeout(timer);
          resolveUrl(match[1]);
        }
      });

      childProc.stderr?.on("data", (chunk: Buffer) => {
        stderrBuf += chunk.toString("utf8");
      });

      childProc.once("exit", (code) => {
        clearTimeout(timer);
        rejectUrl(
          new Error(
            `Python HTTP harness exited prematurely with code ${code}.\nstdout: ${stdoutBuf}\nstderr: ${stderrBuf}`,
          ),
        );
      });
    });

    return {
      baseUrl,
      stop: async () => {
        if (!childProc || childProc.exitCode !== null) {
          return;
        }
        childProc.kill("SIGTERM");
        await new Promise<void>((res) => {
          const killTimer = setTimeout(() => {
            if (childProc.exitCode === null) {
              childProc.kill("SIGKILL");
            }
            res();
          }, 5_000);
          childProc.once("exit", () => {
            clearTimeout(killTimer);
            res();
          });
        });
      },
    };
  } catch (err) {
    if (proc && proc.exitCode === null) {
      proc.kill("SIGKILL");
    }
    throw err;
  }
}

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function routeOf(url: string): string {
  return new URL(url).pathname.replace(/^\/api\/v10/, "");
}

const hasRealEnv = Boolean(
  process.env.DALTON_REAL_BACKEND_URL?.trim() || resolveTestDsn(),
);

describe("Real HTTP End-to-End: DiscordChannelAdapter -> GatewayOrchestrator -> POST /agent/send -> HttpBackendClient -> FastAPI channels_router -> Isolated Postgres (d030)", () => {
  it.skipIf(!hasRealEnv)(
    "delivers current-conversation text and files to the Discord thread channel via real I-6 claim, enforces real workspace permissions on explicit targets, skips fallback after current send, and recovers thread scope across retry claims and background/watch startup recovery",
    async () => {
      const BOT_ID = "999000111";
      const tempDir = mkdtempSync(join(tmpdir(), "dalton-discord-real-http-"));
      let backendHandle: { baseUrl: string; stop: () => Promise<void> } | null =
        null;
      let app: ReturnType<typeof createGatewayApp> | null = null;

      try {
        backendHandle = await startOrAttachRealBackend();
        const baseUrl = backendHandle.baseUrl;

        const bootRes = await fetch(`${baseUrl}/test-harness/bootstrap`);
        expect(bootRes.status).toBe(200);
        const boot = (await bootRes.json()) as BootstrapState;

        const inspectDb = async (params: {
          messageId?: string;
          deliveryKey?: string;
          runId?: string;
          waitCompleted?: boolean;
        }): Promise<InspectResponse> => {
          const qs = new URLSearchParams({ platform: "discord" });
          if (params.messageId) qs.set("message_id", params.messageId);
          if (params.deliveryKey) qs.set("delivery_key", params.deliveryKey);
          if (params.runId) qs.set("run_id", params.runId);
          const deadline = Date.now() + 5_000;
          while (true) {
            const r = await fetch(
              `${baseUrl}/test-harness/inspect?${qs.toString()}`,
            );
            expect(r.status).toBe(200);
            const snapshot = (await r.json()) as InspectResponse;
            if (
              !params.waitCompleted ||
              !params.messageId ||
              (snapshot.inbound &&
                ["completed", "failed", "denied"].includes(
                  snapshot.inbound.status,
                )) ||
              Date.now() >= deadline
            ) {
              return snapshot;
            }
            await new Promise((resolveWait) => setTimeout(resolveWait, 20));
          }
        };

        const controlTurnGate = async (
          payload: Record<string, unknown>,
        ): Promise<Record<string, unknown>> => {
          const r = await fetch(`${baseUrl}/test-harness/turn-gate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          expect(r.status).toBe(200);
          return (await r.json()) as Record<string, unknown>;
        };

        // Stub ONLY the external Discord REST API; all Gateway -> Backend -> FastAPI -> Postgres calls use real HTTP & SQL
        let failNextThreadPostWith400 = false;
        const sentMessages: Array<{
          route: string;
          isMultipart: boolean;
          bodyText: string;
        }> = [];
        const restCalls: Array<{ method: string; route: string }> = [];
        const isParentRoute = (route: string) =>
          route === `/channels/${boot.parent_cid}` ||
          route.startsWith(`/channels/${boot.parent_cid}/`);
        const messagePostRoutes = (
          items: Array<{ method: string; route: string }>,
        ) =>
          items
            .filter(
              (call) => call.method === "POST" && call.route.endsWith("/messages"),
            )
            .map((call) => call.route);

        const rest = new RequestClient("test-bot-token", {
          fetchImpl: async (url, init) => {
            const route = routeOf(String(url));
            const method = (init?.method ?? "GET").toUpperCase();
            restCalls.push({ method, route });
            if (route === `/channels/${boot.thread_cid}` && method === "GET") {
              return jsonResponse({
                id: boot.thread_cid,
                type: 11,
                guild_id: boot.guild_id,
                parent_id: boot.parent_cid,
                name: "earnings-thread",
                thread_metadata: { archived: false },
              });
            }
            if (route === `/channels/${boot.parent_cid}` && method === "GET") {
              return jsonResponse({
                id: boot.parent_cid,
                type: 0,
                guild_id: boot.guild_id,
                name: "Equities Channel",
              });
            }
            if (route === `/channels/${boot.other_cid}` && method === "GET") {
              return jsonResponse({
                id: boot.other_cid,
                type: 0,
                guild_id: boot.guild_id,
                name: "Alerts Channel",
              });
            }
            if (route.endsWith("/messages") && method === "POST") {
              if (
                route === `/channels/${boot.thread_cid}/messages` &&
                failNextThreadPostWith400
              ) {
                failNextThreadPostWith400 = false;
                return jsonResponse(
                  { code: 50035, message: "Invalid Form Body" },
                  400,
                );
              }
              const isMultipart = init?.body instanceof FormData;
              const bodyText = isMultipart
                ? String((init.body as FormData).get("payload_json") ?? "")
                : String(init?.body ?? "");
              sentMessages.push({ route, isMultipart, bodyText });
              return jsonResponse({ id: `discord-msg-${sentMessages.length}` });
            }
            throw new Error(`Unexpected Discord REST ${method} ${route}`);
          },
        });

        const discordAdapter = new DiscordChannelAdapter({
          botToken: "test-bot-token",
          botUserId: BOT_ID,
          rest,
        });
        vi.spyOn(discordAdapter, "start").mockResolvedValue(undefined);
        vi.spyOn(discordAdapter, "isConnected").mockReturnValue(true);

        const config = loadConfig({
          GATEWAY_HOST: "127.0.0.1",
          GATEWAY_PORT: "0",
          INTERNAL_SERVICE_TOKEN: boot.service_token,
          DALTON_BACKEND_URL: baseUrl,
          WEB_BASE_URL: "https://dalton.test",
          DALTON_WEIXIN_CACHE_DIR: tempDir,
          DALTON_DISCORD_ENABLED: "true",
          DALTON_DISCORD_BOT_TOKEN: "test-bot-token",
          DALTON_DISCORD_APPLICATION_ID: BOT_ID,
        });

        // Real HttpBackendClient with real global `fetch` (no mock fetchFn!)
        const realBackendClient = new HttpBackendClient({
          baseUrl,
          serviceToken: boot.service_token,
        });

        const adapters = new Map<ChannelPlatform, ChannelAdapter>([
          ["discord", discordAdapter],
        ]);

        app = createGatewayApp({
          config,
          backend: realBackendClient,
          adapters,
        });
        const { host: gwHost, port: gwPort } = await app.start();

        const postAgentSend = async (payload: Record<string, unknown>) => {
          const res = await fetchGatewayServer(
            app!,
            `http://${gwHost}:${gwPort}/agent/send`,
            {
              method: "POST",
              headers: {
                "X-Service-Token": boot.service_token,
                "X-User-Id": boot.user_a,
                "Content-Type": "application/json",
              },
              body: JSON.stringify(payload),
            },
          );
          return {
            statusCode: res.status,
            body: (await res.json()) as Record<string, unknown>,
          };
        };

        // -------------------------------------------------------------------
        // 1. Arm turn SSE gate and dispatch a Discord guild thread message
        // -------------------------------------------------------------------
        await controlTurnGate({ action: "arm" });

        const inboundMsgId = `${boot.prefix}_msg_thread_1`;
        const inboundPromise = discordAdapter.dispatchMessage({
          id: inboundMsgId,
          channel_id: boot.thread_cid,
          guild_id: boot.guild_id,
          content: `<@${BOT_ID}> analyze Q4 margins and send the chart`,
          author: {
            id: boot.puid_a,
            username: "alice",
            global_name: "Alice",
          },
          mentions: [{ id: BOT_ID, username: "DaltonBot" }],
          thread: {
            id: boot.thread_cid,
            type: 11,
            guild_id: boot.guild_id,
            parent_id: boot.parent_cid,
            name: "earnings-thread",
          },
          attachments: [],
        } as never);

        const streamingState = await controlTurnGate({
          action: "wait_streaming",
          message_id: inboundMsgId,
        });
        const turnThreadId = String(streamingState.thread_id);
        const turnRunId = String(streamingState.run_id);
        expect(turnThreadId.length).toBeGreaterThan(10);
        expect(turnRunId.length).toBeGreaterThan(10);

        const snapStreaming = await inspectDb({ messageId: inboundMsgId });
        expect(snapStreaming.inbound?.chat_address).toBe(boot.parent_addr);
        expect(snapStreaming.inbound?.thread_scope_key).toBe(
          boot.thread_scope_key,
        );
        expect(snapStreaming.inbound?.workspace_id).toBe(boot.ws_a1);

        // -------------------------------------------------------------------
        // 2. Current-conversation POST /agent/send (omitted `target`) delivers
        //    both text and file to `thread_cid` and NEVER to `parent_cid`
        // -------------------------------------------------------------------
        const currentSendRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-cur-1",
          turn_platform: "discord",
          text: "Q4 gross margin expanded 180 bps.",
          files: [{ workspace_id: boot.ws_a1, path: "reports/q4-margins.png" }],
        });
        expect(currentSendRes.statusCode).toBe(200);
        expect(currentSendRes.body).toEqual({
          status: "sent",
          code: null,
          address: boot.parent_addr,
          current: true,
          duplicate: false,
          message: `Delivered to ${boot.parent_addr}.`,
          files: [
            { path: "reports/q4-margins.png", status: "sent", reason: null },
          ],
        });

        expect(sentMessages).toHaveLength(2);
        expect(sentMessages[0]!.route).toBe(
          `/channels/${boot.thread_cid}/messages`,
        );
        expect(sentMessages[0]!.isMultipart).toBe(false);
        expect(sentMessages[0]!.bodyText).toContain(
          "Q4 gross margin expanded 180 bps.",
        );
        expect(sentMessages[1]!.route).toBe(
          `/channels/${boot.thread_cid}/messages`,
        );
        expect(sentMessages[1]!.isMultipart).toBe(true);
        expect(sentMessages[1]!.bodyText).toContain("q4-margins.png");
        expect(
          sentMessages.some(
            (m) => m.route === `/channels/${boot.parent_cid}/messages`,
          ),
        ).toBe(false);
        expect(messagePostRoutes(restCalls)).toEqual([
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
        ]);
        expect(restCalls.some((call) => isParentRoute(call.route))).toBe(false);
        expect(
          restCalls.some(
            (call) =>
              call.method === "GET" &&
              call.route === `/channels/${boot.thread_cid}`,
          ),
        ).toBe(true);
        const restCountBeforeExplicit = restCalls.length;

        const snapAfterCurrentSend = await inspectDb({
          messageId: inboundMsgId,
          deliveryKey: `${turnRunId}:tc-real-http-cur-1`,
        });
        expect(snapAfterCurrentSend.inbound?.sent_to_current_count).toBe(1);
        expect(snapAfterCurrentSend.delivery?.is_current).toBe(true);
        expect(snapAfterCurrentSend.delivery?.status).toBe("sent");

        // -------------------------------------------------------------------
        // 3. Explicit `target` to `other_allowed_addr` (bound to colleague-shared
        //    `ws_b_shared_rw` with `rw` access) succeeds with `current: false`
        // -------------------------------------------------------------------
        const explicitOtherRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-other-1",
          turn_platform: "discord",
          target: boot.other_allowed_addr,
          text: "Cross-channel alert to shared workspace channel",
        });
        expect(explicitOtherRes.statusCode).toBe(200);
        expect(explicitOtherRes.body).toMatchObject({
          status: "sent",
          address: boot.other_allowed_addr,
          current: false,
        });
        expect(sentMessages).toHaveLength(3);
        expect(sentMessages[2]!.route).toBe(
          `/channels/${boot.other_cid}/messages`,
        );
        const explicitRoutes = restCalls.slice(restCountBeforeExplicit);
        expect(messagePostRoutes(explicitRoutes)).toEqual([
          `/channels/${boot.other_cid}/messages`,
        ]);
        expect(
          explicitRoutes.some((call) => call.route.includes(boot.thread_cid)),
        ).toBe(false);
        expect(explicitRoutes.some((call) => isParentRoute(call.route))).toBe(
          false,
        );

        // -------------------------------------------------------------------
        // 4. Real permission checks reject explicit `target` to `private_b_addr`
        //    (bound to User B's private workspace `ws_b_private`) and `unauthorized_addr`
        // -------------------------------------------------------------------
        const privateWsRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-priv-b-1",
          turn_platform: "discord",
          target: boot.private_b_addr,
          text: "Should be blocked by real workspace write permission check",
        });
        expect(privateWsRes.statusCode).toBe(200);
        expect(privateWsRes.body).toMatchObject({
          status: "failed",
          code: "not_allowed",
          current: false,
        });

        const unauthTargetRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-unauth-1",
          turn_platform: "discord",
          target: boot.unauthorized_addr,
          text: "Should be blocked by real allowed-target check",
        });
        expect(unauthTargetRes.statusCode).toBe(200);
        expect(unauthTargetRes.body).toMatchObject({
          status: "failed",
          code: "not_allowed",
          current: false,
        });
        expect(sentMessages).toHaveLength(3);
        expect(messagePostRoutes(restCalls)).toEqual([
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.other_cid}/messages`,
        ]);
        expect(restCalls.some((call) => isParentRoute(call.route))).toBe(false);
        const restCountBeforeRetry = restCalls.length;

        // -------------------------------------------------------------------
        // 5. Retry claim after a failed delivery (HTTP 400) recovers
        //    `thread_scope_key` from Postgres and delivers to `thread_cid`
        // -------------------------------------------------------------------
        failNextThreadPostWith400 = true;
        const failedRetryRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-retry-1",
          turn_platform: "discord",
          text: "Retryable thread reply over real Postgres",
        });
        expect(failedRetryRes.body).toMatchObject({ status: "failed" });
        expect(sentMessages).toHaveLength(3);

        const snapFailedDlv = await inspectDb({
          deliveryKey: `${turnRunId}:tc-real-http-retry-1`,
        });
        expect(snapFailedDlv.delivery?.status).toBe("failed");

        const succeededRetryRes = await postAgentSend({
          thread_id: turnThreadId,
          run_id: turnRunId,
          tool_call_id: "tc-real-http-retry-1",
          turn_platform: "discord",
          text: "Retryable thread reply over real Postgres",
        });
        expect(succeededRetryRes.body).toMatchObject({
          status: "sent",
          address: boot.parent_addr,
          current: true,
          message: `Delivered to ${boot.parent_addr}.`,
        });
        expect(sentMessages).toHaveLength(4);
        expect(sentMessages[3]!.route).toBe(
          `/channels/${boot.thread_cid}/messages`,
        );
        const retryRoutes = restCalls.slice(restCountBeforeRetry);
        expect(messagePostRoutes(retryRoutes)).toEqual([
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
        ]);
        expect(retryRoutes.some((call) => isParentRoute(call.route))).toBe(false);
        expect(
          retryRoutes.some((call) => call.route.includes(boot.other_cid)),
        ).toBe(false);

        // -------------------------------------------------------------------
        // 6. Release turn SSE stream: because `sent_to_current_count > 0` in
        //    Postgres, final text fallback is skipped
        // -------------------------------------------------------------------
        await controlTurnGate({ action: "release" });
        await inboundPromise;

        const snapCompleted = await inspectDb({
          messageId: inboundMsgId,
          waitCompleted: true,
        });
        expect(snapCompleted.inbound?.status).toBe("completed");
        expect(snapCompleted.inbound?.fallback_pending).toBe(false);
        expect(snapCompleted.inbound?.sent_to_current_count).toBe(2);
        expect(sentMessages).toHaveLength(4);
        expect(
          sentMessages.some(
            (m) => m.route === `/channels/${boot.parent_cid}/messages`,
          ),
        ).toBe(false);

        // -------------------------------------------------------------------
        // 7. Background run & thread watch startup recovery over real HTTP +
        //    Postgres recovers `thread_scope_key` and delivers to `thread_cid`
        // -------------------------------------------------------------------
        const bgRunId = `${boot.prefix}_bg_run_2`;
        await realBackendClient.updateInboundProgress({
          thread_watch: {
            thread_id: turnThreadId,
            dalton_user_id: boot.user_a,
            platform: "discord",
            target_address: boot.parent_addr,
            scope_key: boot.thread_scope_key,
            generation: 1,
            last_seen_turn_index: 1,
            watch_active: false,
          },
          background_run: {
            run_id: bgRunId,
            thread_id: turnThreadId,
            dalton_user_id: boot.user_a,
            platform: "discord",
            target_address: boot.parent_addr,
            turn_index: 2,
            status: "completed",
            fallback_pending: true,
          },
        });

        await controlTurnGate({
          action: "configure_replay",
          thread_id: turnThreadId,
          run_id: bgRunId,
          turn_index: 2,
          text: "Recovered background thread analysis from Postgres.",
        });

        await app.orchestrator.runStartupRecovery();

        expect(sentMessages).toHaveLength(5);
        expect(sentMessages[4]!.route).toBe(
          `/channels/${boot.thread_cid}/messages`,
        );
        expect(sentMessages[4]!.bodyText).toContain(
          "Recovered background thread analysis from Postgres.",
        );
        expect(
          sentMessages.some(
            (m) => m.route === `/channels/${boot.parent_cid}/messages`,
          ),
        ).toBe(false);
        expect(messagePostRoutes(restCalls)).toEqual([
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.other_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
          `/channels/${boot.thread_cid}/messages`,
        ]);
        expect(restCalls.some((call) => isParentRoute(call.route))).toBe(false);

        const snapBgAfter = await inspectDb({
          runId: bgRunId,
          deliveryKey: `fallback:${bgRunId}`,
        });
        expect(snapBgAfter.background_run?.fallback_pending).toBe(false);
        expect(snapBgAfter.delivery?.status).toBe("sent");
        expect(snapBgAfter.delivery?.is_current).toBe(true);
      } finally {
        if (app) {
          await app.stop();
        }
        if (backendHandle) {
          await backendHandle.stop();
        }
        rmSync(tempDir, { recursive: true, force: true });
      }
    },
    60_000,
  );
});
