import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { describe, expect, it, vi } from "vitest";
import { HttpBackendClient } from "../src/backend-client.js";
import {
  ChannelDeliveryError,
  type ChannelAdapter,
} from "../src/channels/adapter.js";
import {
  DiscordChannelAdapter,
  RequestClient,
} from "../src/channels/discord/index.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/index.js";
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
  home_ws_b: string;
  ws_a1: string;
  ws_a2: string;
  ws_cn: string;
  cn_name: string;
  ws_dup_1: string;
  ws_dup_2: string;
  dup_name: string;
  ws_named_main: string;
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
  grp_chat_id: string;
  grp_addr: string;
  grp_scope: string;
}

interface InspectResponse {
  session?: {
    generation: number;
    override_workspace_id: string | null;
    activity_epoch: number | null;
    initialized_epoch: number | null;
  } | null;
  inbound: {
    message_id: string;
    event_kind?: string;
    status: string;
    dalton_user_id: string | null;
    chat_address?: string;
    thread_scope_key?: string;
    workspace_id: string | null;
    external_thread_id: string | null;
    session_generation?: number | null;
    thread_id: string | null;
    run_id: string | null;
    command_request?: Record<string, unknown> | null;
    command_result?: Record<string, unknown> | null;
    command_effect_status?: string | null;
    command_reply_pending?: boolean;
    fallback_pending?: boolean;
    sent_to_current_count?: number;
  } | null;
  threads?: Array<{
    thread_id: string;
    thread_index: number;
    external_id: string | null;
    turn_indexes: number[];
  }>;
  delivery?: {
    delivery_key: string;
    kind: string;
    user_id?: string | null;
    thread_id?: string | null;
    run_id?: string | null;
    is_current?: boolean;
    platform?: string;
    target_address?: string;
    status: string;
    sent_chunks?: number;
    total_chunks?: number;
    platform_message_ids?: string[];
    files_status?: unknown;
    error_code?: string | null;
    activity_origin?: string;
    source_scope_key?: string | null;
    source_generation?: number | null;
  } | null;
  background_run?: {
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

async function startOrAttachRealBackend(
  scriptRel = "tests/integration/dalton/test_channel_gateway_db.py",
): Promise<{
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
  const scriptPath = resolve(REPO_ROOT, scriptRel);

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

function createMinimalFakeLarkClient(): Lark.Client {
  let msgSeq = 1;
  return {
    im: {
      message: {
        create: async () => ({
          code: 0,
          msg: "success",
          data: { message_id: `om_lark_create_${msgSeq++}` },
        }),
        reply: async () => ({
          code: 0,
          msg: "success",
          data: { message_id: `om_lark_reply_${msgSeq++}` },
        }),
      },
      image: {
        create: async () => ({
          code: 0,
          msg: "success",
          data: { image_key: "img_key_1" },
        }),
      },
      file: {
        create: async () => ({
          code: 0,
          msg: "success",
          data: { file_key: "file_key_1" },
        }),
      },
      messageResource: {
        get: async () => {
          throw new Error("not used");
        },
      },
      chat: {
        list: async () => ({
          code: 0,
          data: { items: [], has_more: false },
        }),
        get: async () => ({
          code: 0,
          data: { name: "HTTP E2E Group" },
        }),
      },
      chatMembers: {
        get: async () => ({
          code: 0,
          data: { items: [], has_more: false },
        }),
      },
    },
  } as unknown as Lark.Client;
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
        backendHandle = await startOrAttachRealBackend(
          "tests/integration/dalton/test_discord_gateway_db.py",
        );
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


describe("Real HTTP End-to-End: FeishuChannelAdapter -> GatewayOrchestrator -> HttpBackendClient -> FastAPI channels_router -> Isolated Postgres", () => {
  it.skipIf(!hasRealEnv)(
    "verifies I-1/I-3/I-6/I-7 wire parameters, default Home, Chinese+space workspace, duplicate candidates, real permission check, /workspace main vs /main, /new idempotency, 6-hour expiry, group mention gating, per-user group isolation, and delivery activity guards",
    async () => {
      const BOT_OPEN_ID = "ou_bot_real_http";
      const tempDir = mkdtempSync(join(tmpdir(), "dalton-gw-real-http-"));
      let backendHandle: { baseUrl: string; stop: () => Promise<void> } | null =
        null;
      let app1: ReturnType<typeof createGatewayApp> | null = null;
      let app2: ReturnType<typeof createGatewayApp> | null = null;

      try {
        backendHandle = await startOrAttachRealBackend();
        const baseUrl = backendHandle.baseUrl;

        const bootRes = await fetch(`${baseUrl}/test-harness/bootstrap`);
        expect(bootRes.status).toBe(200);
        const boot = (await bootRes.json()) as BootstrapState;

        const inspectDb = async (params: {
          scopeKey?: string;
          userId?: string;
          messageId?: string;
          workspaceId?: string;
          deliveryKey?: string;
        }): Promise<InspectResponse> => {
          const qs = new URLSearchParams({ platform: "feishu" });
          if (params.scopeKey) qs.set("scope_key", params.scopeKey);
          if (params.userId) qs.set("dalton_user_id", params.userId);
          if (params.messageId) qs.set("message_id", params.messageId);
          if (params.workspaceId) qs.set("workspace_id", params.workspaceId);
          if (params.deliveryKey) qs.set("delivery_key", params.deliveryKey);
          const deadline = Date.now() + 5_000;
          while (true) {
            const r = await fetch(`${baseUrl}/test-harness/inspect?${qs.toString()}`);
            expect(r.status).toBe(200);
            const snapshot = (await r.json()) as InspectResponse;
            if (
              !params.messageId ||
              !snapshot.inbound ||
              ["completed", "failed", "denied", "ignored"].includes(
                snapshot.inbound.status,
              ) ||
              (snapshot.inbound.event_kind === "command" &&
                snapshot.inbound.command_effect_status === "pending_cancel") ||
              Date.now() >= deadline
            ) {
              return snapshot;
            }
            await new Promise((resolve) => setTimeout(resolve, 20));
          }
        };

        const ageSession = async (params: {
          scopeKey: string;
          userId: string;
          secondsAgo: number;
        }): Promise<{
          generation: number;
          override_workspace_id: string | null;
          activity_epoch: number;
        }> => {
          const r = await fetch(`${baseUrl}/test-harness/age-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              platform: "feishu",
              scope_key: params.scopeKey,
              dalton_user_id: params.userId,
              seconds_ago: params.secondsAgo,
            }),
          });
          expect(r.status).toBe(200);
          return (await r.json()) as {
            generation: number;
            override_workspace_id: string | null;
            activity_epoch: number;
          };
        };

        const config = loadConfig({
          GATEWAY_HOST: "127.0.0.1",
          GATEWAY_PORT: "0",
          BACKEND_BASE_URL: baseUrl,
          WEB_BASE_URL: "https://dalton.test",
          INTERNAL_SERVICE_TOKEN: boot.service_token,
          GATEWAY_SPOOL_DIR: tempDir,
        });

        // Real HttpBackendClient with real global `fetch` (no mock fetchFn!)
        const realBackendClient = new HttpBackendClient({
          baseUrl,
          serviceToken: boot.service_token,
        });

        const feishuOutbound = new FakeChannelAdapter("feishu");
        const adapters = new Map<ChannelPlatform, ChannelAdapter>([
          ["feishu", feishuOutbound],
        ]);

        app1 = createGatewayApp({
          config,
          backend: realBackendClient,
          adapters,
        });
        const feishuInboundAdapter = new FeishuChannelAdapter({
          appId: "cli_real_http",
          appSecret: "sec_real_http",
          botOpenId: BOT_OPEN_ID,
          client: createMinimalFakeLarkClient(),
        });
        feishuInboundAdapter.onInboundEvent(async (ev) => {
          await app1!.orchestrator.handleInboundEvent(ev);
        });

        const dmScopeA = `dm:${boot.puid_a}`;

        // -------------------------------------------------------------------
        // 1. Default Chief of Staff (Home) in DM despite default notification workspace = ws_a1
        // -------------------------------------------------------------------
        const mDm1 = `${boot.prefix}_om_dm_1`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mDm1,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "Hello Chief of Staff" }),
          },
        });

        const snapDm1 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mDm1,
        });
        expect(snapDm1.inbound?.status).toBe("completed");
        expect(snapDm1.inbound?.workspace_id).toBe(boot.home_ws_a);
        expect(snapDm1.inbound?.workspace_id).not.toBe(boot.ws_a1);
        expect(snapDm1.session?.generation).toBe(1);
        expect(snapDm1.session?.override_workspace_id).toBeNull();
        expect(feishuOutbound.sentTexts.at(-1)?.text).toContain(
          `ws=${boot.home_ws_a}`,
        );

        const mDm1Turn2 = `${boot.prefix}_om_dm_1_turn2`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mDm1Turn2,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({
              text: "Second turn on the same thread",
            }),
          },
        });
        const snapDm1Turn2 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mDm1Turn2,
          workspaceId: boot.home_ws_a,
        });
        expect(snapDm1Turn2.session?.generation).toBe(1);
        expect(snapDm1Turn2.inbound?.thread_id).toBe(snapDm1.inbound?.thread_id);
        const firstHomeThread = (snapDm1Turn2.threads ?? []).find(
          (thread) => thread.thread_id === snapDm1.inbound?.thread_id,
        );
        expect(firstHomeThread?.turn_indexes).toEqual([1, 2]);
        const stableHomeThreadIndex = firstHomeThread?.thread_index;

        // -------------------------------------------------------------------
        // 2. Chinese + space workspace name (`/<cn_name>`), duplicate candidates,
        //    real permission check (`/workspace <ws_b_private>`), `/workspace main` vs `/main`, `/new`
        // -------------------------------------------------------------------
        const mCmdCn = `${boot.prefix}_om_cmd_cn`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdCn,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: `  /${boot.cn_name}  ` }),
          },
        });

        const snapCn = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdCn,
        });
        expect(snapCn.inbound?.event_kind).toBe("command");
        expect(snapCn.inbound?.command_request).toMatchObject({
          cmd: "workspace_switch",
          target_kind: "shortcut_name",
          workspace_query: boot.cn_name,
        });
        expect(snapCn.session?.generation).toBe(2);
        expect(snapCn.session?.override_workspace_id).toBe(boot.ws_cn);
        expect(feishuOutbound.sentTexts.at(-1)?.text).toContain(boot.cn_name);

        // Duplicate workspace name returns candidates and keeps generation=2 in ws_cn
        const mCmdDup = `${boot.prefix}_om_cmd_dup`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdDup,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: `/${boot.dup_name}` }),
          },
        });
        const dupReply = feishuOutbound.sentTexts.at(-1)?.text ?? "";
        expect(dupReply).toContain("Multiple workspaces match");
        expect(dupReply).toContain(boot.ws_dup_1);
        expect(dupReply).toContain(boot.ws_dup_2);
        const snapAfterDup = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
        });
        expect(snapAfterDup.session?.generation).toBe(2);
        expect(snapAfterDup.session?.override_workspace_id).toBe(boot.ws_cn);

        // Real permission check: User A switching to User B's private workspace (`ws_b_private`) is denied
        const mCmdUnauth = `${boot.prefix}_om_cmd_unauth`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdUnauth,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({
              text: `/workspace ${boot.ws_b_private}`,
            }),
          },
        });
        const snapUnauth = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdUnauth,
        });
        expect(snapUnauth.inbound?.command_request).toMatchObject({
          cmd: "workspace_switch",
          target_kind: "workspace_id",
          workspace_id: boot.ws_b_private,
        });
        expect(snapUnauth.inbound?.command_result).toMatchObject({
          ok: false,
          deny_reason: "workspace_write_denied",
        });
        expect(snapUnauth.session?.generation).toBe(2);
        expect(snapUnauth.session?.override_workspace_id).toBe(boot.ws_cn);

        // `/workspace main` (`target_kind: "exact_name"`) selects the workspace literally named "main" (gen 3)
        const mCmdLiteralMain = `${boot.prefix}_om_cmd_lit_main`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdLiteralMain,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/workspace main" }),
          },
        });
        const snapLiteralMain = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdLiteralMain,
        });
        expect(snapLiteralMain.inbound?.command_request).toMatchObject({
          cmd: "workspace_switch",
          target_kind: "exact_name",
          workspace_query: "main",
        });
        expect(snapLiteralMain.session?.generation).toBe(3);
        expect(snapLiteralMain.session?.override_workspace_id).toBe(
          boot.ws_named_main,
        );

        // `/main` (`target_kind: "home"`) returns to Chief of Staff (Home, override_workspace_id = null, gen 4)
        const mCmdHomeMain = `${boot.prefix}_om_cmd_home_main`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdHomeMain,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/main" }),
          },
        });
        const snapHomeMain = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdHomeMain,
        });
        expect(snapHomeMain.inbound?.command_request).toMatchObject({
          cmd: "workspace_switch",
          target_kind: "home",
          workspace_query: "home",
        });
        expect(snapHomeMain.session?.generation).toBe(4);
        expect(snapHomeMain.session?.override_workspace_id).toBeNull();
        expect(feishuOutbound.sentTexts.at(-1)?.text).toContain(
          "Home (Chief of Staff)",
        );

        // -------------------------------------------------------------------
        // 3. `/new` and Command Redelivery Deduplication across fresh Orchestrator
        // -------------------------------------------------------------------
        const mCmdNew = `${boot.prefix}_om_cmd_new_idem`;
        const rawNewEvent = {
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mCmdNew,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/new" }),
          },
        };
        await feishuInboundAdapter.receiveRawEvent(rawNewEvent);
        const snapNew1 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdNew,
        });
        expect(snapNew1.session?.generation).toBe(5);

        // Create a second GatewayOrchestrator (fresh in-memory dedupe) and replay the exact same `/new` message_id
        app2 = createGatewayApp({
          config,
          backend: realBackendClient,
          adapters,
        });
        const feishuInboundAdapter2 = new FeishuChannelAdapter({
          appId: "cli_real_http",
          appSecret: "sec_real_http",
          botOpenId: BOT_OPEN_ID,
          client: createMinimalFakeLarkClient(),
        });
        feishuInboundAdapter2.onInboundEvent(async (ev) => {
          await app2!.orchestrator.handleInboundEvent(ev);
        });
        await feishuInboundAdapter2.receiveRawEvent(rawNewEvent);

        const snapNew2 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mCmdNew,
        });
        // Generation MUST remain 5 (persisted command idempotency in Postgres)
        expect(snapNew2.session?.generation).toBe(5);

        // -------------------------------------------------------------------
        // 4. 6-Hour Inactivity Expiry (`< 6h` stays in workspace; `> 6h` resets to Home) & Delivery Activity Guards
        // -------------------------------------------------------------------
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: `${boot.prefix}_om_sw_a1`,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: `/workspace ${boot.ws_a1}` }),
          },
        });
        expect(
          (await inspectDb({ scopeKey: dmScopeA, userId: boot.user_a })).session
            ?.generation,
        ).toBe(6);

        // 4a. Age session to 5h 59m ago (< 6h) -> next message stays in ws_a1 (gen 6) and delivery updates activity timestamp
        const agedBefore = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 5 * 3600 + 59 * 60,
        });
        const mBefore6h = `${boot.prefix}_om_before_6h`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mBefore6h,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "Still inside 6h window" }),
          },
        });
        const snapBefore6h = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mBefore6h,
        });
        expect(snapBefore6h.inbound?.workspace_id).toBe(boot.ws_a1);
        expect(snapBefore6h.session?.generation).toBe(6);
        expect(snapBefore6h.session?.override_workspace_id).toBe(boot.ws_a1);
        expect(snapBefore6h.session?.activity_epoch ?? 0).toBeGreaterThan(
          agedBefore.activity_epoch,
        );

        // 4b. Age session to 6h 1m ago (> 6h) -> next message resets to Home (home_ws_a) and advances generation to 7
        await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 6 * 3600 + 60,
        });
        const mAfter6h = `${boot.prefix}_om_after_6h`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mAfter6h,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "After 6h inactivity" }),
          },
        });
        const snapAfter6h = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mAfter6h,
        });
        expect(snapAfter6h.inbound?.workspace_id).toBe(boot.home_ws_a);
        expect(snapAfter6h.session?.generation).toBe(7);
        expect(snapAfter6h.session?.override_workspace_id).toBeNull();
        const homeThreads =
          (
            await inspectDb({
              workspaceId: boot.home_ws_a,
            })
          ).threads ?? [];
        const gen1Thread = homeThreads.find(
          (thread) => thread.thread_id === snapDm1.inbound?.thread_id,
        );
        const gen7Thread = homeThreads.find(
          (thread) => thread.thread_id === snapAfter6h.inbound?.thread_id,
        );
        expect(gen1Thread?.thread_index).toBe(stableHomeThreadIndex);
        expect(gen1Thread?.turn_indexes).toEqual([1, 2]);
        expect(gen7Thread?.thread_id).not.toBe(gen1Thread?.thread_id);
        expect(gen7Thread?.thread_index).not.toBe(gen1Thread?.thread_index);
        expect(gen7Thread?.turn_indexes).toEqual([1]);
        expect(new Set(homeThreads.map((thread) => thread.thread_index)).size).toBe(
          homeThreads.length,
        );

        // -------------------------------------------------------------------
        // 5. Delivery Activity Guards: notification & stale-generation deliveries do NOT extend activity
        // -------------------------------------------------------------------
        const agedGen7 = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 1800,
        });
        const staleThreadId = snapBefore6h.inbound?.thread_id!;
        const staleRunId = snapBefore6h.inbound?.run_id!;

        // 5a. Stale generation (generation 6 while session is at generation 7) with activityOrigin="conversation"
        await app1.deliveryService.executeDelivery({
          deliveryKey: `stale_gen6:${staleRunId}`,
          kind: "agent_send",
          userId: boot.user_a,
          threadId: staleThreadId,
          runId: staleRunId,
          platform: "feishu",
          target: "feishu:@me",
          inboundMessageId: mBefore6h,
          text: "Late reply from generation 6",
          threadScopeKey: dmScopeA,
          sourceGeneration: 6,
          activityOrigin: "conversation",
        });
        const snapAfterStale = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
        });
        expect(snapAfterStale.session?.activity_epoch).toBeCloseTo(
          agedGen7.activity_epoch,
          3,
        );
        expect(snapAfterStale.session?.override_workspace_id).toBeNull();

        // 5b. Proactive notification (`activityOrigin: "notification"`) on current generation 7
        const curThreadId = snapAfter6h.inbound?.thread_id!;
        const curRunId = snapAfter6h.inbound?.run_id!;
        await app1.deliveryService.executeDelivery({
          deliveryKey: `notify_gen7:${curRunId}`,
          kind: "notice",
          userId: boot.user_a,
          threadId: curThreadId,
          runId: curRunId,
          platform: "feishu",
          target: "feishu:@me",
          text: "Standalone notification",
          threadScopeKey: dmScopeA,
          sourceGeneration: 7,
          activityOrigin: "notification",
        });
        const snapAfterNotify = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
        });
        expect(snapAfterNotify.session?.activity_epoch).toBeCloseTo(
          agedGen7.activity_epoch,
          3,
        );

        // 5c. Standalone assistant delivery (`activityOrigin: "conversation"`) updates `last_conversation_activity_at`
        //     per attachment incrementally via I-7, even if attachment #2 fails.
        //     Bytes are uploaded to and downloaded from the harness file HTTP routes.
        const fileWorkspaceId =
          snapAfter6h.inbound?.workspace_id ?? boot.home_ws_a;
        await realBackendClient.uploadWorkspaceFile(
          boot.user_a,
          fileWorkspaceId,
          "reports/att1.pdf",
          "att1.pdf",
          Buffer.from("ATT_1"),
          "application/pdf",
        );
        await realBackendClient.uploadWorkspaceFile(
          boot.user_a,
          fileWorkspaceId,
          "reports/att2.pdf",
          "att2.pdf",
          Buffer.from("ATT_2"),
          "application/pdf",
        );
        let attCallCount = 0;
        let epochAfterFirstAttachment = 0;
        feishuOutbound.setSendFileBehavior(async (params) => {
          attCallCount += 1;
          if (attCallCount === 1) {
            expect(params.content.equals(Buffer.from("ATT_1"))).toBe(true);
            return {
              messageId: "feishu_file_att_1",
              status: "sent",
              reason: null,
            };
          }
          const snapMidAttachments = await inspectDb({
            scopeKey: dmScopeA,
            userId: boot.user_a,
          });
          epochAfterFirstAttachment =
            snapMidAttachments.session?.activity_epoch ?? 0;
          expect(epochAfterFirstAttachment).toBeGreaterThan(
            agedGen7.activity_epoch,
          );
          throw new ChannelDeliveryError(
            "simulated second attachment failure",
            { outcome: "failed", code: "unavailable" },
          );
        });
        try {
          const partialAttRes = await app1.deliveryService.executeDelivery({
            deliveryKey: `conv_inc_att:${curRunId}`,
            kind: "agent_send",
            userId: boot.user_a,
            threadId: curThreadId,
            runId: curRunId,
            platform: "feishu",
            target: "feishu:@me",
            inboundMessageId: mAfter6h,
            text: "",
            files: [
              {
                path: "reports/att1.pdf",
                workspace_id: fileWorkspaceId,
              },
              {
                path: "reports/att2.pdf",
                workspace_id: fileWorkspaceId,
              },
            ],
            sourceScopeKey: dmScopeA,
            sourceGeneration: 7,
            activityOrigin: "conversation",
          });
          expect(partialAttRes.status).toBe("partial");
          expect(epochAfterFirstAttachment).toBeGreaterThan(
            agedGen7.activity_epoch,
          );
          const snapAfterPartialAtt = await inspectDb({
            scopeKey: dmScopeA,
            userId: boot.user_a,
          });
          expect(
            snapAfterPartialAtt.session?.activity_epoch ?? 0,
          ).toBeGreaterThan(agedGen7.activity_epoch);
        } finally {
          feishuOutbound.setSendFileBehavior(null);
        }

        // 5d. Spoofed `source_generation` on `staleThreadId` (gen 6) is rejected by real I-6 over HTTP
        const spoofClaim = await realBackendClient.authorizeAndClaimDelivery({
          delivery_key: `spoof_gen7:${staleRunId}`,
          kind: "fallback",
          user_id: boot.user_a,
          thread_id: staleThreadId,
          run_id: staleRunId,
          platform: "feishu",
          target: "feishu:@me",
          inbound_message_id: mBefore6h,
          has_non_empty_text: true,
          total_chunks: 1,
          source_scope_key: dmScopeA,
          source_generation: 7,
          activity_origin: "conversation",
        });
        expect(spoofClaim.claim_status).toBe("denied");

        // -------------------------------------------------------------------
        // 6. Group Chat Mention Triggering & Per-User Session Isolation
        // -------------------------------------------------------------------
        // 6a. Unmentioned group message & plain-text @DaltonBot slash command are ignored before I-1 / I-3
        const mGrpUnmentioned = `${boot.prefix}_om_grp_unmentioned`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mGrpUnmentioned,
            chat_id: boot.grp_chat_id,
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({ text: "@DaltonBot /main" }),
            mentions: [],
          },
        });
        const snapUnmentioned = await inspectDb({
          messageId: mGrpUnmentioned,
        });
        expect(snapUnmentioned.inbound).toBeNull();

        // 6b. User A switches workspace in group with structured @bot mention -> User A is in ws_cn (gen 2)
        const mGrpSwA = `${boot.prefix}_om_grp_sw_a`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mGrpSwA,
            chat_id: boot.grp_chat_id,
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({ text: `@_user_1 /${boot.cn_name}` }),
            mentions: [
              {
                key: "@_user_1",
                name: "DaltonBot",
                id: { open_id: BOT_OPEN_ID },
              },
            ],
          },
        });
        const snapGrpA = await inspectDb({
          scopeKey: boot.grp_scope,
          userId: boot.user_a,
          messageId: mGrpSwA,
        });
        expect(snapGrpA.session?.generation).toBe(2);
        expect(snapGrpA.session?.override_workspace_id).toBe(boot.ws_cn);

        // 6c. User B sends a mentioned message in the exact same group chat -> User B stays in User B's Home (gen 1)!
        const mGrpMsgB = `${boot.prefix}_om_grp_msg_b`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_b },
            sender_type: "user",
          },
          message: {
            message_id: mGrpMsgB,
            chat_id: boot.grp_chat_id,
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({
              text: "@_user_1 hello from User B in group",
            }),
            mentions: [
              {
                key: "@_user_1",
                name: "DaltonBot",
                id: { open_id: BOT_OPEN_ID },
              },
            ],
          },
        });
        const snapGrpB = await inspectDb({
          scopeKey: boot.grp_scope,
          userId: boot.user_b,
          messageId: mGrpMsgB,
        });
        expect(snapGrpB.inbound?.workspace_id).toBe(boot.home_ws_b);
        expect(snapGrpB.inbound?.workspace_id).not.toBe(boot.ws_cn);
        expect(snapGrpB.inbound?.workspace_id).not.toBe(boot.ws_a1);
        expect(snapGrpB.session?.generation).toBe(1);
        expect(snapGrpB.session?.override_workspace_id).toBeNull();

        // 6d. Same-workspace switch keeps generation 2 in `ws_cn` and does not start a model turn.
        // The reply names that workspace and its id.
        const threadsBeforeSameSwitch =
          (
            await inspectDb({
              workspaceId: boot.ws_cn,
            })
          ).threads ?? [];
        const mGrpSwSame = `${boot.prefix}_om_grp_sw_same`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mGrpSwSame,
            chat_id: boot.grp_chat_id,
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({ text: `@_user_1 /${boot.cn_name}` }),
            mentions: [
              {
                key: "@_user_1",
                name: "DaltonBot",
                id: { open_id: BOT_OPEN_ID },
              },
            ],
          },
        });
        const snapGrpSame = await inspectDb({
          scopeKey: boot.grp_scope,
          userId: boot.user_a,
          messageId: mGrpSwSame,
        });
        expect(snapGrpSame.session?.generation).toBe(2);
        expect(snapGrpSame.session?.override_workspace_id).toBe(boot.ws_cn);
        expect(snapGrpSame.inbound?.event_kind).toBe("command");
        expect(snapGrpSame.inbound?.thread_id ?? null).toBeNull();
        expect(snapGrpSame.inbound?.run_id ?? null).toBeNull();
        expect(snapGrpSame.inbound?.command_result).toMatchObject({
          ok: true,
          switched: false,
          generation: 2,
          workspace_id: boot.ws_cn,
          workspace_name: boot.cn_name,
        });
        expect(feishuOutbound.sentTexts.at(-1)?.text).toBe(
          `Already in workspace \`${boot.cn_name}\` (\`${boot.ws_cn}\`). Keeping current conversation.`,
        );
        expect(
          (
            await inspectDb({
              workspaceId: boot.ws_cn,
            })
          ).threads ?? [],
        ).toEqual(threadsBeforeSameSwitch);

        // -------------------------------------------------------------------
        // 7. `/stop` Pinning, 503 Transient Failure -> Startup Recovery 200, and Idempotent `done` Activity Timestamp
        // -------------------------------------------------------------------
        await fetch(`${baseUrl}/test-harness/cancel-config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ next_status: 200, clear_calls: true }),
        });

        // 7a. `/stop` when thread has NO active run (`curThreadId`'s turn already completed): `run_id: null`, `command_effect_status: "done"`
        const mStopIdle = `${boot.prefix}_om_stop_idle`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mStopIdle,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/stop" }),
          },
        });
        const snapStopIdle = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStopIdle,
        });
        expect(snapStopIdle.inbound?.run_id).toBeNull();
        expect(snapStopIdle.inbound?.command_effect_status).toBe("done");
        expect(feishuOutbound.sentTexts.at(-1)?.text).toContain(
          "No active run to stop in the current session.",
        );

        // 7b. Set an active `in_progress` run on `curThreadId`, configure `/cancel` to return 503, and send `/stop`
        const activeRunRes = await fetch(
          `${baseUrl}/test-harness/set-active-run`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              thread_id: curThreadId,
              user_id: boot.user_a,
              status: "in_progress",
            }),
          },
        );
        const { run_id: pinnedRunId } = (await activeRunRes.json()) as {
          run_id: string;
        };
        await fetch(`${baseUrl}/test-harness/cancel-config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ next_status: 503, clear_calls: true }),
        });

        const mStop503 = `${boot.prefix}_om_stop_503`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mStop503,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/stop" }),
          },
        });
        const snapStop503 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStop503,
        });
        expect(snapStop503.inbound?.thread_id).toBe(curThreadId);
        expect(snapStop503.inbound?.run_id).toBe(pinnedRunId);
        expect(snapStop503.inbound?.command_effect_status).toBe(
          "pending_cancel",
        );
        expect(snapStop503.inbound?.command_reply_pending).toBe(true);

        // Now restore `/cancel` to 200 and run startup recovery -> completes cancellation and marks `done`
        await fetch(`${baseUrl}/test-harness/cancel-config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ next_status: 200 }),
        });
        await app2.orchestrator.runStartupRecovery();
        const snapStopRecovered = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStop503,
        });
        expect(snapStopRecovered.inbound?.status).toBe("completed");
        expect(snapStopRecovered.inbound?.command_effect_status).toBe("done");
        expect(snapStopRecovered.inbound?.command_reply_pending).toBe(false);

        // 7c. Repeating `command_effect_status: "done"` via I-7 does NOT re-extend `last_conversation_activity_at`
        const agedAfterStop = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 1200,
        });
        await realBackendClient.updateInboundProgress({
          platform: "feishu",
          message_id: mStop503,
          status: "completed",
          command_effect_status: "done",
          command_reply_pending: false,
        });
        const snapAfterDupDone = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
        });
        expect(snapAfterDupDone.session?.activity_epoch).toBeCloseTo(
          agedAfterStop.activity_epoch,
          3,
        );

        const countSent = (needle: string): number =>
          feishuOutbound.sentTexts.filter((item) => item.text.includes(needle))
            .length;
        const readCancelCalls = async (): Promise<
          Array<{ thread_id: string; run_id: string | null; status: number }>
        > => {
          const response = await fetch(`${baseUrl}/test-harness/cancel-config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
          });
          expect(response.status).toBe(200);
          const body = (await response.json()) as {
            calls: Array<{
              thread_id: string;
              run_id: string | null;
              status: number;
            }>;
          };
          return body.calls;
        };

        // 8. /stop 403 persists the denial before the notice. A failed notice
        // stays pending. Recovery resends only that notice. A second adapter
        // does not cancel or deliver again.
        const stoppedBefore403 = countSent("Stopped the current run.");
        const active403Res = await fetch(`${baseUrl}/test-harness/set-active-run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: curThreadId,
            user_id: boot.user_a,
            status: "in_progress",
          }),
        });
        const { run_id: deniedRunId } = (await active403Res.json()) as {
          run_id: string;
        };
        await fetch(`${baseUrl}/test-harness/cancel-config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ next_status: 403, clear_calls: true }),
        });
        feishuOutbound.setSendTextBehavior((options) => {
          if (options.text.includes("Failed to stop the current run")) {
            throw new ChannelDeliveryError("stop notice failed", {
              outcome: "failed",
              code: "unavailable",
            });
          }
          return { messageId: `om_stop_notice_${options.chunkIndex}` };
        });
        const mStop403 = `${boot.prefix}_om_stop_403`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mStop403,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/stop" }),
          },
        });
        const snapStop403 = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStop403,
        });
        expect(snapStop403.inbound?.status).toBe("failed");
        expect(snapStop403.inbound?.command_effect_status).toBe("done");
        expect(snapStop403.inbound?.command_reply_pending).toBe(true);
        expect(snapStop403.inbound?.command_result).toMatchObject({
          ok: false,
          allowed: false,
          deny_reason: "stop_cancel_denied",
          stop_cancel_status: 403,
        });
        expect(countSent("Failed to stop the current run (status 403).")).toBe(0);
        expect(countSent("Stopped the current run.")).toBe(stoppedBefore403);
        expect(await readCancelCalls()).toHaveLength(1);
        feishuOutbound.setSendTextBehavior(null);

        const agedStop403 = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 25200,
        });
        await app2.orchestrator.runStartupRecovery();
        const snapStop403Recovered = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStop403,
        });
        expect(
          countSent("Failed to stop the current run (status 403)."),
        ).toBe(1);
        expect(countSent("Stopped the current run.")).toBe(stoppedBefore403);
        expect(snapStop403Recovered.inbound?.command_reply_pending).toBe(false);
        expect(snapStop403Recovered.inbound?.command_result).toMatchObject({
          ok: false,
          deny_reason: "stop_cancel_denied",
          stop_cancel_status: 403,
        });
        expect(snapStop403Recovered.session?.generation).toBe(
          agedStop403.generation,
        );
        expect(snapStop403Recovered.session?.override_workspace_id).toBe(
          agedStop403.override_workspace_id,
        );
        expect(snapStop403Recovered.session?.activity_epoch).toBeCloseTo(
          agedStop403.activity_epoch,
          3,
        );
        expect(await readCancelCalls()).toHaveLength(1);

        await feishuInboundAdapter2.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mStop403,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/stop" }),
          },
        });
        const snapStop403Dup = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mStop403,
        });
        expect(
          countSent("Failed to stop the current run (status 403)."),
        ).toBe(1);
        expect(countSent("Stopped the current run.")).toBe(stoppedBefore403);
        expect(snapStop403Dup.inbound?.command_result?.ok).toBe(false);
        expect(await readCancelCalls()).toHaveLength(1);
        await fetch(`${baseUrl}/test-harness/set-active-run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: curThreadId,
            user_id: boot.user_a,
            run_id: deniedRunId,
            status: "cancelled",
          }),
        });
        await fetch(`${baseUrl}/test-harness/cancel-config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ next_status: 200, clear_calls: true }),
        });

        // 9. A partial body stays partial across replay. Recovery sends only
        // the short notice and does not repeat the body or extend activity.
        const partialToken = "PARTIALBODY";
        const partialUserText = `${partialToken}${"x".repeat(2100)}`;
        const armRecord = await fetch(`${baseUrl}/test-harness/next-turn-script`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: "record_replay" }),
        });
        expect(armRecord.status).toBe(200);
        feishuOutbound.setSendTextBehavior((options) => {
          if (
            options.chunkIndex > 0 ||
            options.text.includes("后续内容未能发送")
          ) {
            throw new ChannelDeliveryError("partial chunk failed", {
              outcome: "failed",
              code: "unavailable",
            });
          }
          return { messageId: `om_partial_${options.chunkIndex}` };
        });
        const mPartial = `${boot.prefix}_om_partial_body`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mPartial,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: partialUserText }),
          },
        });
        const snapPartial = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mPartial,
        });
        const partialRunId = snapPartial.inbound?.run_id ?? "";
        const snapPartialDelivery = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mPartial,
          deliveryKey: `fallback:${partialRunId}`,
        });
        expect(snapPartialDelivery.inbound?.status).toBe("completed");
        expect(snapPartialDelivery.inbound?.fallback_pending).toBe(true);
        expect(snapPartialDelivery.delivery).toMatchObject({
          status: "partial",
          activity_origin: "conversation",
          source_scope_key: dmScopeA,
        });
        expect(countSent(partialToken)).toBe(1);
        expect(countSent("后续内容未能发送")).toBe(0);
        feishuOutbound.setSendTextBehavior(null);
        const agedPartial = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 25200,
        });
        await app2.orchestrator.runStartupRecovery();
        const snapPartialRecovered = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mPartial,
          deliveryKey: `fallback:${partialRunId}`,
        });
        expect(countSent(partialToken)).toBe(1);
        expect(countSent("后续内容未能发送")).toBe(1);
        expect(snapPartialRecovered.inbound?.fallback_pending).toBe(false);
        expect(snapPartialRecovered.delivery).toMatchObject({
          status: "partial",
          activity_origin: "conversation",
          source_scope_key: dmScopeA,
          source_generation: snapPartialDelivery.delivery?.source_generation,
        });
        expect(snapPartialRecovered.session?.generation).toBe(
          agedPartial.generation,
        );
        expect(snapPartialRecovered.session?.override_workspace_id).toBe(
          agedPartial.override_workspace_id,
        );
        expect(snapPartialRecovered.session?.activity_epoch).toBeCloseTo(
          agedPartial.activity_epoch,
          3,
        );

        // 10. A failed current interrupt keeps its scope and generation.
        // After /new, recovery uses replay and does not send the prompt twice.
        const interruptQuestion = "Which benchmark index should we use?";
        const interruptId = "int_real_http_1";
        const armInterrupt = await fetch(
          `${baseUrl}/test-harness/next-turn-script`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              kind: "interrupt",
              interrupt_id: interruptId,
              question: interruptQuestion,
            }),
          },
        );
        expect(armInterrupt.status).toBe(200);
        feishuOutbound.setSendTextBehavior((options) => {
          if (options.text.includes(interruptQuestion)) {
            throw new ChannelDeliveryError("interrupt prompt failed", {
              outcome: "failed",
              code: "unavailable",
            });
          }
          return { messageId: `om_interrupt_${options.chunkIndex}` };
        });
        const mInterrupt = `${boot.prefix}_om_interrupt_recover`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mInterrupt,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "Run benchmark comparison" }),
          },
        });
        const snapInterrupt = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mInterrupt,
        });
        const interruptRunId = snapInterrupt.inbound?.run_id ?? "";
        const interruptThreadId = snapInterrupt.inbound?.thread_id ?? "";
        const snapInterruptDelivery = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mInterrupt,
          deliveryKey: `interrupt:${interruptRunId}:${interruptId}`,
        });
        expect(snapInterruptDelivery.inbound?.status).toBe("completed");
        expect(snapInterruptDelivery.inbound?.fallback_pending).toBe(true);
        expect(snapInterruptDelivery.delivery).toMatchObject({
          status: "failed",
          activity_origin: "conversation",
          source_scope_key: dmScopeA,
          source_generation: snapInterrupt.session?.generation,
        });
        expect(countSent(interruptQuestion)).toBe(0);
        expect(countSent("earlier conversation thread")).toBe(0);
        feishuOutbound.setSendTextBehavior(null);

        const mInterruptNew = `${boot.prefix}_om_interrupt_new`;
        await feishuInboundAdapter.receiveRawEvent({
          sender: {
            sender_id: { open_id: boot.puid_a },
            sender_type: "user",
          },
          message: {
            message_id: mInterruptNew,
            chat_id: "oc_dm_a",
            chat_type: "p2p",
            message_type: "text",
            content: JSON.stringify({ text: "/new" }),
          },
        });
        const agedInterrupt = await ageSession({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          secondsAgo: 25200,
        });
        await app2.orchestrator.runStartupRecovery();
        const snapInterruptRecovered = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mInterrupt,
          deliveryKey: `interrupt:${interruptRunId}:${interruptId}`,
        });
        const stalePrompts = feishuOutbound.sentTexts.filter((item) =>
          item.text.includes("earlier conversation thread"),
        );
        expect(stalePrompts).toHaveLength(1);
        expect(stalePrompts[0]?.text).toContain(interruptQuestion);
        expect(stalePrompts[0]?.text).toContain(
          `https://dalton.test/chat/t/${interruptThreadId}`,
        );
        expect(snapInterruptRecovered.inbound?.fallback_pending).toBe(false);
        expect(snapInterruptRecovered.delivery).toMatchObject({
          status: "sent",
          activity_origin: "replay",
          source_scope_key: dmScopeA,
          source_generation: snapInterrupt.session?.generation,
        });
        expect(snapInterruptRecovered.session?.generation).toBe(
          agedInterrupt.generation,
        );
        expect(snapInterruptRecovered.session?.override_workspace_id).toBe(
          agedInterrupt.override_workspace_id,
        );
        expect(snapInterruptRecovered.session?.activity_epoch).toBeCloseTo(
          agedInterrupt.activity_epoch,
          3,
        );
        expect(agedInterrupt.generation).toBe(
          (snapInterrupt.session?.generation ?? 0) + 1,
        );

        await realBackendClient.updateInboundProgress({
          platform: "feishu",
          message_id: mInterrupt,
          fallback_pending: true,
        });
        await app2.orchestrator.runStartupRecovery();
        expect(
          feishuOutbound.sentTexts.filter((item) =>
            item.text.includes("earlier conversation thread"),
          ),
        ).toHaveLength(1);
        const snapInterruptSecond = await inspectDb({
          scopeKey: dmScopeA,
          userId: boot.user_a,
          messageId: mInterrupt,
          deliveryKey: `interrupt:${interruptRunId}:${interruptId}`,
        });
        expect(snapInterruptSecond.delivery).toMatchObject({
          status: "sent",
          activity_origin: "replay",
          source_generation: snapInterrupt.session?.generation,
        });
        expect(snapInterruptSecond.session?.activity_epoch).toBeCloseTo(
          agedInterrupt.activity_epoch,
          3,
        );
      } finally {
        if (app2) {
          await app2.stop();
        }
        if (app1) {
          await app1.stop();
        }
        if (backendHandle) {
          await backendHandle.stop();
        }
        rmSync(tempDir, { recursive: true, force: true });
      }
    },
    90_000,
  );
});
