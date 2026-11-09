import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { describe, expect, it } from "vitest";
import { HttpBackendClient } from "../src/backend-client.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/index.js";
import { loadConfig } from "../src/config.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelAdapter, ChannelPlatform } from "../src/types.js";

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
    resolve(REPO_ROOT, "../laneD/.venv/bin/python"),
    resolve(REPO_ROOT, ".venv/bin/python"),
    "python3",
  ];
  for (const cand of candidates) {
    if (cand === "python3" || existsSync(cand)) {
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
  ws_b_private: string;
  puid_a: string;
  puid_b: string;
  grp_chat_id: string;
  grp_addr: string;
  grp_scope: string;
}

interface InspectResponse {
  session: {
    generation: number;
    override_workspace_id: string | null;
    activity_epoch: number | null;
    initialized_epoch: number | null;
  } | null;
  inbound: {
    message_id: string;
    event_kind: string;
    status: string;
    dalton_user_id: string | null;
    workspace_id: string | null;
    external_thread_id: string | null;
    session_generation: number | null;
    thread_id: string | null;
    run_id: string | null;
    command_request: Record<string, unknown> | null;
    command_result: Record<string, unknown> | null;
    command_effect_status: string | null;
    command_reply_pending: boolean;
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
    "tests/integration/dalton/test_channel_gateway_db.py",
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
        }): Promise<InspectResponse> => {
          const qs = new URLSearchParams({ platform: "feishu" });
          if (params.scopeKey) qs.set("scope_key", params.scopeKey);
          if (params.userId) qs.set("dalton_user_id", params.userId);
          if (params.messageId) qs.set("message_id", params.messageId);
          const r = await fetch(
            `${baseUrl}/test-harness/inspect?${qs.toString()}`,
          );
          expect(r.status).toBe(200);
          return (await r.json()) as InspectResponse;
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
    60_000,
  );
});
