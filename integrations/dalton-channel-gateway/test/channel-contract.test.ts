import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { AgentRoutesHandler } from "../src/agent-routes.js";
import { HttpBackendClient } from "../src/backend-client.js";
import type {
  ChannelAdapter,
  SendFileOptions,
  SendFileResult,
  SendTextChunkOptions,
  SendTextChunkResult,
} from "../src/channels/adapter.js";
import {
  DiscordChannelAdapter,
  type RequestClient,
} from "../src/channels/discord/index.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/monitor.js";
import { DeliveryService } from "../src/delivery-service.js";
import { StaleGatewayEpochError } from "../src/gateway-lease.js";
import { ChannelGatewayOrchestrator } from "../src/orchestrator.js";
import { rewriteSandboxLinks } from "../src/shared/sandbox-links.js";
import type { ChannelPlatform, InboundChannelEvent } from "../src/types.js";

const FIXTURES_DIR = resolve(
  import.meta.dirname,
  "../../../tests/fixtures/dalton/channel_contract",
);
const WEB_BASE = "https://dalton.example.com";
const SERVICE_TOKEN = "contract-test-service-token-32bytes-ok";
const GATEWAY_EPOCH = 7;

function loadFixture<T>(filename: string): T {
  return JSON.parse(
    readFileSync(resolve(FIXTURES_DIR, filename), "utf-8"),
  ) as T;
}

interface HttpContractFixture {
  request: {
    method: string;
    path: string;
    headers?: Record<string, string>;
    body?: Record<string, unknown>;
  };
  response: {
    status: number;
    body: Record<string, unknown>;
  };
}

interface SseEventsFixture {
  run_end: Record<string, unknown>;
  replay_done: Record<string, unknown>;
}

interface UnknownDeliverablesClaimFixture {
  unknown_claim: HttpContractFixture;
  links_recovery_claim: HttpContractFixture;
}

function makeJsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeSseResponse(
  events: Array<{ event: string; data: Record<string, unknown> }>,
): Response {
  const payload = events
    .map(
      (ev, idx) =>
        `id: ${idx + 1}\nevent: ${ev.event}\ndata: ${JSON.stringify(ev.data)}\n\n`,
    )
    .join("");
  return new Response(payload, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function makeStubAdapter(
  platform: ChannelPlatform,
  overrides: Partial<ChannelAdapter> = {},
): ChannelAdapter & {
  sentTexts: SendTextChunkOptions[];
  sentFiles: SendFileOptions[];
} {
  const sentTexts: SendTextChunkOptions[] = [];
  const sentFiles: SendFileOptions[] = [];
  return {
    platform,
    maxChunkChars: 4000,
    sentTexts,
    sentFiles,
    isConfigured: () => true,
    isConnected: () => true,
    start: async () => undefined,
    stop: async () => undefined,
    onInboundEvent: () => undefined,
    splitTextChunks: (text: string) => (text ? [text] : []),
    sendTextChunk: vi.fn(
      async (opts: SendTextChunkOptions): Promise<SendTextChunkResult> => {
        sentTexts.push(opts);
        return {
          messageId: `${platform}_msg_${sentTexts.length}`,
          resolvedAddress: opts.targetAddress,
        };
      },
    ),
    sendFile: vi.fn(
      async (opts: SendFileOptions): Promise<SendFileResult> => {
        // Mirrors Feishu post-upload / pre-send recipient verification hook
        await opts.verifyRecipientBeforeSend?.();
        sentFiles.push(opts);
        return {
          messageId: `${platform}_file_${sentFiles.length}`,
          status: "sent",
          reason: null,
        };
      },
    ),
    downloadAttachment: async () => ({
      content: Buffer.from(""),
      filename: "a.txt",
      mimeType: "text/plain",
    }),
    listAvailableChats: async () => ({ chats: [], complete: true, error: null }),
    ...overrides,
  };
}

describe("Shared Backend <-> Gateway Contract Tests (real FastAPI fixtures)", () => {
  it("sends X-Gateway-Epoch and reads `authorized` on real DM file delivery & blocks Feishu send when unbound after upload", async () => {
    const authFixture = loadFixture<HttpContractFixture>(
      "verify_dm_recipient_authorized.json",
    );
    const unboundFixture = loadFixture<HttpContractFixture>(
      "verify_dm_recipient_unbound_403.json",
    );
    const staleFixture = loadFixture<HttpContractFixture>(
      "verify_dm_recipient_stale_epoch_409.json",
    );

    const seenEpochs: Array<{ path: string; epoch: string | null }> = [];
    const pdfBytes = Buffer.from("%PDF-1.4\ncontract deliverable report\n");

    const buildFetch = (verifyResponses: HttpContractFixture[]) => {
      let verifyIdx = 0;
      return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input));
        const headers = new Headers(init?.headers);
        if (
          url.pathname.includes("/verify-dm-recipient") ||
          url.pathname.includes("/deliverables/download")
        ) {
          seenEpochs.push({
            path: url.pathname,
            epoch: headers.get("X-Gateway-Epoch"),
          });
        }
        if (url.pathname.endsWith("/deliveries/authorize-and-claim")) {
          return makeJsonResponse(200, {
            claim_status: "claimed",
            platform: "feishu",
            target_address: "feishu:@me",
            recipient_id: "ou_alice",
            dm_recipient_platform_user_id: "ou_alice",
            is_current: true,
            delivery_attempt: 1,
            workspace_id: "ws-contract-1",
            home_workspace_id: "ws-home-alice",
            prior_run_files: [],
          });
        }
        if (url.pathname.endsWith("/deliveries/progress")) {
          return makeJsonResponse(200, {
            ok: true,
            delivery_key: "deliverables:run-contract-1",
            status: "sent",
            sent_to_current_count: 1,
          });
        }
        if (url.pathname.endsWith("/deliverables/download")) {
          return new Response(pdfBytes, {
            status: 200,
            headers: {
              "Content-Type": "application/pdf",
              "Content-Length": String(pdfBytes.byteLength),
            },
          });
        }
        if (url.pathname.endsWith("/verify-dm-recipient")) {
          const fx =
            verifyResponses[Math.min(verifyIdx, verifyResponses.length - 1)]!;
          verifyIdx += 1;
          return makeJsonResponse(fx.response.status, fx.response.body);
        }
        throw new Error(`Unexpected fetch URL: ${url.toString()}`);
      });
    };

    // 1) Happy path: both download and verify-dm-recipient send X-Gateway-Epoch=7 and succeed on `authorized: true`
    const feishuOk = makeStubAdapter("feishu");
    const backendOk = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: buildFetch([authFixture, authFixture]) as unknown as typeof fetch,
    });
    backendOk.setGatewayEpoch(GATEWAY_EPOCH);
    const svcOk = new DeliveryService({
      backend: backendOk,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuOk]]),
      webBaseUrl: WEB_BASE,
    });

    const okRes = await svcOk.executeDelivery({
      deliveryKey: "deliverables:run-contract-1",
      kind: "fallback",
      userId: "user-alice",
      threadId: "11111111-1111-4111-8111-111111111111",
      workspaceId: "ws-contract-1",
      runId: "run-contract-1",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-contract-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });
    expect(okRes.status).toBe("sent");
    expect(feishuOk.sentFiles).toHaveLength(1);
    expect(seenEpochs).toHaveLength(3); // 1 download + 2 verify-dm-recipient (pre-upload & post-upload)
    for (const entry of seenEpochs) {
      expect(entry.epoch).toBe(String(GATEWAY_EPOCH));
    }

    // 2) Feishu post-upload unbind: 1st verify returns 200 authorized=true, 2nd returns real 403 fixture -> file NOT sent!
    const feishuUnbind = makeStubAdapter("feishu");
    const backendUnbind = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: buildFetch([authFixture, unboundFixture]) as unknown as typeof fetch,
    });
    backendUnbind.setGatewayEpoch(GATEWAY_EPOCH);
    const svcUnbind = new DeliveryService({
      backend: backendUnbind,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuUnbind]]),
      webBaseUrl: WEB_BASE,
    });

    const unbindRes = await svcUnbind.executeDelivery({
      deliveryKey: "deliverables:run-contract-1",
      kind: "fallback",
      userId: "user-alice",
      threadId: "11111111-1111-4111-8111-111111111111",
      workspaceId: "ws-contract-1",
      runId: "run-contract-1",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-contract-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });
    expect(unbindRes.status).toBe("failed");
    expect(unbindRes.code).toBe("not_allowed");
    expect(feishuUnbind.sentFiles).toHaveLength(0);

    // 3) Real 409 stale_gateway_epoch fixture triggers StaleGatewayEpochError
    const onStale = vi.fn();
    const backendStale = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: buildFetch([staleFixture]) as unknown as typeof fetch,
    });
    backendStale.setGatewayEpoch(GATEWAY_EPOCH);
    backendStale.setOnStaleGatewayEpoch(onStale);
    await expect(
      backendStale.verifyDmDeliveryRecipient("deliverables:run-contract-1", 1),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);
    expect(onStale).toHaveBeenCalledTimes(1);
  });

  it("uses `match_mode` and real backend topic-lookup fixtures so empty-cache initiator follow-ups succeed while non-initiators are dropped (Feishu & Discord)", async () => {
    const feishuHit = loadFixture<HttpContractFixture>(
      "topics_lookup_feishu_hit.json",
    );
    const discordHit = loadFixture<HttpContractFixture>(
      "topics_lookup_discord_hit.json",
    );
    const missFixture = loadFixture<HttpContractFixture>(
      "topics_lookup_miss.json",
    );

    const lookupBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/internal/topics/lookup")) {
        const parsed = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        lookupBodies.push(parsed);
        // Contract assertion: gateway MUST send `match_mode`, NEVER legacy `purpose`
        expect(parsed.match_mode).toBe("inbound_followup");
        expect("purpose" in parsed).toBe(false);

        if (parsed.platform === "feishu" && parsed.external_topic_id === "omt_topic_1") {
          return makeJsonResponse(feishuHit.response.status, feishuHit.response.body);
        }
        if (parsed.platform === "discord" && parsed.external_topic_id === "999000111") {
          return makeJsonResponse(discordHit.response.status, discordHit.response.body);
        }
        return makeJsonResponse(missFixture.response.status, missFixture.response.body);
      }
      throw new Error(`Unexpected fetch URL: ${url.toString()}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchMock as unknown as typeof fetch,
    });

    // 1) Feishu empty-cache lookup: initiator accepted, non-initiator dropped, unknown dropped
    const feishuReceived: InboundChannelEvent[] = [];
    const feishu = new FeishuChannelAdapter({
      appId: "cli_contract",
      appSecret: "sec_contract",
      botOpenId: "ou_bot",
    });
    feishu.setBackendClient(backend);
    feishu.onInboundEvent(async (ev) => {
      feishuReceived.push(ev);
    });

    await feishu.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
      message: {
        message_id: "om_alice_no_at",
        thread_id: "omt_topic_1",
        root_id: "om_root_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "免@继续分析" }),
      },
    });
    expect(feishuReceived).toHaveLength(1);
    expect(feishuReceived[0]!.threadScopeKey).toBe("chat:oc_grp_1:topic:om_root_1");
    expect(feishuReceived[0]!.isGroupTrigger).toBe(true);

    // Non-initiator with fresh empty-cache adapter -> dropped!
    const feishuBob = new FeishuChannelAdapter({
      appId: "cli_contract",
      appSecret: "sec_contract",
      botOpenId: "ou_bot",
    });
    const feishuBobReceived: InboundChannelEvent[] = [];
    feishuBob.setBackendClient(backend);
    feishuBob.onInboundEvent(async (ev) => {
      feishuBobReceived.push(ev);
    });
    await feishuBob.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob" }, sender_type: "user" },
      message: {
        message_id: "om_bob_no_at",
        thread_id: "omt_topic_1",
        root_id: "om_root_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "旁观插话" }),
      },
    });
    expect(feishuBobReceived).toHaveLength(0);

    // 2) Discord empty-cache lookup: initiator accepted, non-initiator dropped
    const discordRest = {
      get: vi.fn(async () => ({ id: "999000111", type: 11, guild_id: "111111", parent_id: "222222" })),
      post: vi.fn(async () => ({ id: "msg_1" })),
      patch: vi.fn(async () => ({})),
    };
    const discordReceived: InboundChannelEvent[] = [];
    const discord = new DiscordChannelAdapter({
      botToken: "tok",
      botUserId: "bot_1",
      rest: discordRest as unknown as RequestClient,
    });
    discord.setBackendClient(backend);
    discord.onInboundEvent(async (ev) => {
      discordReceived.push(ev);
    });
    await discord.handleDispatch("THREAD_CREATE", {
      id: "999000111",
      type: 11,
      guild_id: "111111",
      parent_id: "222222",
      name: "Topic",
    });
    await discord.dispatchMessage({
      id: "disc_msg_alice",
      channel_id: "999000111",
      guild_id: "111111",
      content: "继续跟踪财报",
      author: { id: "100001", username: "alice" },
      mentions: [],
    });
    expect(discordReceived).toHaveLength(1);
    expect(discordReceived[0]!.threadScopeKey).toBe(
      "chat:111111/222222:topic:999000111",
    );

    const discordBob = new DiscordChannelAdapter({
      botToken: "tok",
      botUserId: "bot_1",
      rest: discordRest as unknown as RequestClient,
    });
    const discordBobReceived: InboundChannelEvent[] = [];
    discordBob.setBackendClient(backend);
    discordBob.onInboundEvent(async (ev) => {
      discordBobReceived.push(ev);
    });
    await discordBob.handleDispatch("THREAD_CREATE", {
      id: "999000111",
      type: 11,
      guild_id: "111111",
      parent_id: "222222",
      name: "Topic",
    });
    await discordBob.dispatchMessage({
      id: "disc_msg_bob",
      channel_id: "999000111",
      guild_id: "111111",
      content: "非发起人免@",
      author: { id: "200002", username: "bob" },
      mentions: [],
    });
    expect(discordBobReceived).toHaveLength(0);
    expect(lookupBodies.length).toBeGreaterThanOrEqual(4);
  });

  it("settles DM deliverables from real `run_end` and `replay_done` fixtures (`size` field) even after `send_message` already sent the text", async () => {
    const sseFixture = loadFixture<SseEventsFixture>(
      "deliverables_sse_events.json",
    );
    const verifyAuth = loadFixture<HttpContractFixture>(
      "verify_dm_recipient_authorized.json",
    );
    const pdfBytes = Buffer.from("%PDF-1.4\ncontract deliverable report\n");
    const threadId = "11111111-1111-4111-8111-111111111111";
    const runId = "run-contract-1";

    for (const mode of ["run_end", "replay_done"] as const) {
      const feishu = makeStubAdapter("feishu");
      const claimedKeys: string[] = [];
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input));
        if (url.pathname.endsWith("/inbound/register-and-resolve")) {
          return makeJsonResponse(200, {
            existing_status: null,
            linked: true,
            allowed: true,
            dalton_user_id: "user-alice",
            request_key: "rk-sse-1",
            retry_request_key: null,
            resubmitted_request_key: null,
            steered_into_run_id: null,
            thread_id: threadId,
            run_id: null,
            turn_index: null,
            workspace_id: "ws-contract-1",
            workspace_status: "running",
            workspace_owner_id: "user-alice",
            external_thread_id: "ext-1",
            generation: 1,
            fallback_pending: false,
            sent_to_current_count: 0,
            uploaded_attachments: [],
            raw_payload: {},
            pending_interrupt: null,
          });
        }
        if (url.pathname.endsWith("/messages") && init?.method === "POST") {
          if (mode === "run_end") {
            return makeSseResponse([
              {
                event: "turn_start",
                data: { thread_id: threadId, run_id: runId, turn_index: 1 },
              },
              {
                event: "message_chunk",
                data: {
                  agent: "main",
                  content_type: "text",
                  content: "报告已生成，请查收 report.pdf。",
                },
              },
              {
                event: "run_end",
                data: sseFixture.run_end,
              },
            ]);
          }
          // Simulate mid-stream disconnect before `run_end` -> forces `/messages/replay` recovery!
          return makeSseResponse([
            {
              event: "turn_start",
              data: { thread_id: threadId, run_id: runId, turn_index: 1 },
            },
          ]);
        }
        if (url.pathname.endsWith("/messages/replay")) {
          return makeSseResponse([
            {
              event: "user_message",
              data: {
                turn_index: 1,
                run_id: runId,
                run_completed_at: "2026-11-25T20:00:00Z",
              },
            },
            {
              event: "message_chunk",
              data: {
                agent: "main",
                turn_index: 1,
                run_id: runId,
                content_type: "text",
                content: "报告已生成，请查收 report.pdf。",
              },
            },
            {
              event: "replay_done",
              data: sseFixture.replay_done,
            },
          ]);
        }
        if (url.pathname.endsWith("/inbound/progress")) {
          // Body text was ALREADY delivered via `send_message` (`sent_to_current_count: 1`)!
          return makeJsonResponse(200, {
            ok: true,
            run_id: runId,
            has_in_flight_current_text: false,
            has_unknown_current_text: false,
            has_in_flight_or_unknown_current_text: false,
            sent_to_current_count: 1,
          });
        }
        if (url.pathname.endsWith("/deliveries/authorize-and-claim")) {
          const reqBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          const dKey = String(reqBody.delivery_key ?? "");
          claimedKeys.push(dKey);
          return makeJsonResponse(200, {
            claim_status: "claimed",
            platform: "feishu",
            target_address: "feishu:@me",
            recipient_id: "ou_alice",
            dm_recipient_platform_user_id: "ou_alice",
            is_current: true,
            delivery_attempt: 1,
            workspace_id: "ws-contract-1",
            home_workspace_id: "ws-home-alice",
            prior_run_files: [],
          });
        }
        if (url.pathname.endsWith("/deliveries/progress")) {
          return makeJsonResponse(200, {
            ok: true,
            delivery_key: `deliverables:${runId}`,
            status: "sent",
            sent_to_current_count: 1,
          });
        }
        if (url.pathname.endsWith("/deliverables/download")) {
          return new Response(pdfBytes, {
            status: 200,
            headers: {
              "Content-Type": "application/pdf",
              "Content-Length": String(pdfBytes.byteLength),
            },
          });
        }
        if (url.pathname.endsWith("/verify-dm-recipient")) {
          return makeJsonResponse(
            verifyAuth.response.status,
            verifyAuth.response.body,
          );
        }
        if (url.pathname.endsWith("/status")) {
          return makeJsonResponse(200, {
            status: "idle",
            run_id: runId,
            latest_turn_index: 1,
          });
        }
        if (url.pathname.includes("/threads/") && url.pathname.endsWith("/stream")) {
          return makeSseResponse([]);
        }
        throw new Error(`Unexpected fetch URL (${mode}): ${url.toString()}`);
      });

      const backend = new HttpBackendClient({
        baseUrl: "http://backend.test",
        serviceToken: SERVICE_TOKEN,
        fetchImpl: fetchMock as unknown as typeof fetch,
      });
      backend.setGatewayEpoch(GATEWAY_EPOCH);
      const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
      const deliveryService = new DeliveryService({
        backend,
        adapters,
        webBaseUrl: WEB_BASE,
      });
      const agentRoutes = new AgentRoutesHandler({
        backend,
        adapters,
        deliveryService,
        webBaseUrl: WEB_BASE,
      });
      const orch = new ChannelGatewayOrchestrator({
        backend,
        adapters,
        deliveryService,
        agentRoutes,
        webBaseUrl: WEB_BASE,
        backoffDelayFn: async () => undefined,
      });

      await orch.handleInboundEvent({
        platform: "feishu",
        platformUserId: "ou_alice",
        chatType: "dm",
        chatAddress: "feishu:@me",
        chatId: "oc_dm_alice",
        threadScopeKey: "dm:ou_alice",
        messageId: `om_in_${mode}`,
        text: "帮我生成 report.pdf",
        attachments: [],
        isGroupTrigger: false,
        rawPayload: { platform_user_id: "ou_alice" },
      });
      await orch.stopAllWatches();

      // Even though `sent_to_current_count === 1` (text already sent via send_message),
      // `deliverables:${runId}` still settled and sent `results/report.pdf`!
      expect(claimedKeys).toContain(`deliverables:${runId}`);
      expect(feishu.sentFiles).toHaveLength(1);
      expect(feishu.sentFiles[0]!.path).toBe("results/report.pdf");
    }
  });

  it("preserves original `unknown` deliverables row after crash before I-7 and sends deduplicable login link via `deliverables-links:${runId}`", async () => {
    const claimFixture = loadFixture<UnknownDeliverablesClaimFixture>(
      "deliveries_claim_unknown_deliverables.json",
    );
    const feishu = makeStubAdapter("feishu");
    const claimedKeys: string[] = [];
    const progressKeys: string[] = [];
    let linksAlreadySent = false;

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/deliveries/authorize-and-claim")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        const dKey = String(body.delivery_key ?? "");
        claimedKeys.push(dKey);
        if (dKey === "deliverables:run-crash-1") {
          return makeJsonResponse(
            claimFixture.unknown_claim.response.status,
            claimFixture.unknown_claim.response.body,
          );
        }
        if (dKey === "deliverables-links:run-crash-1") {
          if (linksAlreadySent) {
            return makeJsonResponse(200, {
              claim_status: "already_completed",
              status: "sent",
              platform: "feishu",
              target_address: "feishu:@me",
              is_current: true,
              first_response_json: {
                status: "sent",
                code: null,
                address: "feishu:@me",
                current: true,
                duplicate: true,
                message: "already sent",
                files: [],
              },
            });
          }
          return makeJsonResponse(
            claimFixture.links_recovery_claim.response.status,
            claimFixture.links_recovery_claim.response.body,
          );
        }
      }
      if (url.pathname.endsWith("/deliveries/progress")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        const dKey = String(body.delivery_key ?? "");
        progressKeys.push(dKey);
        if (dKey === "deliverables-links:run-crash-1" && body.status === "sent") {
          linksAlreadySent = true;
        }
        return makeJsonResponse(200, {
          ok: true,
          delivery_key: dKey,
          status: String(body.status ?? "sent"),
          sent_to_current_count: 1,
        });
      }
      throw new Error(`Unexpected fetch URL: ${url.toString()}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const firstReplay = await svc.executeDelivery({
      deliveryKey: "deliverables:run-crash-1",
      kind: "fallback",
      userId: "user-alice",
      threadId: "11111111-1111-4111-8111-111111111111",
      workspaceId: "ws-contract-1",
      runId: "run-crash-1",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-contract-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
      sourceScopeKey: "dm:ou_alice",
      sourceGeneration: 1,
      activityOrigin: "replay",
    });

    expect(firstReplay.status).toBe("sent");
    // ZERO binary uploads; original `deliverables:run-crash-1` was never mutated via I-7!
    expect(feishu.sentFiles).toHaveLength(0);
    expect(progressKeys.every((k) => k === "deliverables-links:run-crash-1")).toBe(true);
    expect(feishu.sentTexts).toHaveLength(1);
    expect(feishu.sentTexts[0]!.text).toContain(
      `${WEB_BASE}/chat/t/11111111-1111-4111-8111-111111111111?file=results%2Freport.pdf`,
    );

    // Second replay deduplicates cleanly on `deliverables-links:run-crash-1` without sending twice
    const secondReplay = await svc.executeDelivery({
      deliveryKey: "deliverables:run-crash-1",
      kind: "fallback",
      userId: "user-alice",
      threadId: "11111111-1111-4111-8111-111111111111",
      workspaceId: "ws-contract-1",
      runId: "run-crash-1",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-contract-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
      sourceScopeKey: "dm:ou_alice",
      sourceGeneration: 1,
      activityOrigin: "replay",
    });
    expect(secondReplay.status).toBe("sent");
    expect(secondReplay.duplicate).toBe(true);
    expect(feishu.sentTexts).toHaveLength(1);
    expect(claimedKeys).toEqual([
      "deliverables:run-crash-1",
      "deliverables-links:run-crash-1",
      "deliverables:run-crash-1",
      "deliverables-links:run-crash-1",
    ]);
  });

  it("refuses Weixin deliverable delivery without link fallback when backend returns real 403 preflight-denied fixture", async () => {
    const wxDeniedFixture = loadFixture<HttpContractFixture>(
      "deliverables_download_weixin_denied_403.json",
    );
    const weixin = makeStubAdapter("weixin");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/deliveries/authorize-and-claim")) {
        return makeJsonResponse(200, {
          claim_status: "claimed",
          platform: "weixin",
          target_address: "weixin:@me",
          recipient_id: "wx_user_alice",
          dm_recipient_platform_user_id: "wx_user_alice",
          is_current: true,
          delivery_attempt: 1,
          workspace_id: "ws-contract-1",
          home_workspace_id: "ws-home-alice",
          weixin_account_id: "22222222-2222-4222-8222-222222222222",
          weixin_bot_id: "wx_bot_1",
          weixin_bot_generation: 1,
          weixin_context_token: "ctx_tok_1",
          prior_run_files: [],
        });
      }
      if (url.pathname.endsWith("/deliverables/download")) {
        const headers = new Headers(init?.headers);
        expect(headers.get("X-Gateway-Epoch")).toBe(String(GATEWAY_EPOCH));
        return makeJsonResponse(
          wxDeniedFixture.response.status,
          wxDeniedFixture.response.body,
        );
      }
      if (url.pathname.endsWith("/deliveries/progress")) {
        return makeJsonResponse(200, {
          ok: true,
          delivery_key: "deliverables:run-wx-denied",
          status: "failed",
          sent_to_current_count: 0,
        });
      }
      throw new Error(`Unexpected fetch URL: ${url.toString()}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["weixin", weixin]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-wx-denied",
      kind: "fallback",
      userId: "user-alice",
      threadId: "11111111-1111-4111-8111-111111111111",
      workspaceId: "ws-contract-1",
      runId: "run-wx-denied",
      platform: "weixin",
      target: "weixin:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-contract-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(res.status).toBe("failed");
    expect(res.code).toBe("not_allowed");
    expect(weixin.sentFiles).toHaveLength(0);
    expect(weixin.sentTexts).toHaveLength(0);
  });

  it("recovers 6-file DM deliverables via real orchestrator replay when `deliverables:${runId}` is `unknown` after sending overflow link: keeps `unknown`, zero binary re-upload, login links sent once", async () => {
    const threadId = "11111111-1111-4111-8111-111111111111";
    const runId = "run-6files-replay";
    const sixDeliverables = [1, 2, 3, 4, 5, 6].map((n) => ({
      workspace_id: "ws-contract-1",
      path: `results/file_${n}.pdf`,
      sha256: `sha_${n}`,
      size: 1024,
      stamp: { size: 1024, mtime: "1700000000.0", ctime: "1700000000.0", inode: String(100 + n) },
      stamp_settled: true,
    }));

    // Simulate backend state after crash:
    // - `deliverables:run-6files-replay` already sent chunk 0 (overflow link for file_6.pdf)
    //   and entered `unknown` before binary attachments were confirmed.
    // - Its link chunk is excluded from body-text dedupe (`has_non_empty_text: false`),
    //   so `updateInboundProgress` reports `has_in_flight_or_unknown_current_text: false`.
    const deliveriesDb = new Map<
      string,
      { status: string; sent_chunks: number; has_non_empty_text: boolean; is_current: boolean }
    >([
      [
        `deliverables:${runId}`,
        {
          status: "unknown",
          sent_chunks: 1,
          has_non_empty_text: false,
          is_current: true,
        },
      ],
    ]);

    const feishu = makeStubAdapter("feishu");
    const claimedKeys: string[] = [];
    const progressWrites: Array<{ key: string; status?: string }> = [];
    const downloadCalls: string[] = [];

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/messages/replay")) {
        return makeSseResponse([
          {
            event: "user_message",
            data: {
              turn_index: 1,
              run_id: runId,
              run_completed_at: "2026-11-25T20:00:00Z",
            },
          },
          {
            event: "message_chunk",
            data: {
              agent: "main",
              turn_index: 1,
              run_id: runId,
              content_type: "text",
              content: "已生成 6 份 PDF 文件。",
            },
          },
          {
            event: "replay_done",
            data: {
              thread_id: threadId,
              latest_run_id: runId,
              turn_deliverables_by_run: {
                [runId]: sixDeliverables,
              },
            },
          },
        ]);
      }
      if (url.pathname.endsWith("/inbound/progress")) {
        // Mirror backend SQL: exclude `deliverables:` / `deliverables-links:` from body dedupe!
        let hasUnknownBody = false;
        let hasInFlightBody = false;
        for (const [dKey, row] of deliveriesDb.entries()) {
          if (
            !row.is_current ||
            !row.has_non_empty_text ||
            dKey.startsWith("deliverables:") ||
            dKey.startsWith("deliverables-links:")
          ) {
            continue;
          }
          if (row.status === "unknown") hasUnknownBody = true;
          if (row.status === "in_flight") hasInFlightBody = true;
        }
        return makeJsonResponse(200, {
          ok: true,
          run_id: runId,
          has_in_flight_current_text: hasInFlightBody,
          has_unknown_current_text: hasUnknownBody,
          has_in_flight_or_unknown_current_text: hasInFlightBody || hasUnknownBody,
          sent_to_current_count: 1,
        });
      }
      if (url.pathname.endsWith("/deliveries/authorize-and-claim")) {
        const reqBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        const dKey = String(reqBody.delivery_key ?? "");
        claimedKeys.push(dKey);
        if (dKey.startsWith("deliverables:") || dKey.startsWith("deliverables-links:")) {
          expect(reqBody.has_non_empty_text).toBe(false);
        }
        const existing = deliveriesDb.get(dKey);
        if (existing?.status === "unknown") {
          return makeJsonResponse(200, {
            claim_status: "in_flight_or_unknown",
            existing_status: "unknown",
            sent_chunks: existing.sent_chunks,
            platform: "feishu",
            target_address: "feishu:@me",
            is_current: true,
            delivery_attempt: 1,
          });
        }
        if (existing?.status === "sent") {
          return makeJsonResponse(200, {
            claim_status: "already_completed",
            status: "sent",
            platform: "feishu",
            target_address: "feishu:@me",
            is_current: true,
            first_response_json: {
              status: "sent",
              code: null,
              address: "feishu:@me",
              current: true,
              duplicate: true,
              message: "already sent",
              files: [],
            },
          });
        }
        deliveriesDb.set(dKey, {
          status: "in_flight",
          sent_chunks: 0,
          has_non_empty_text:
            dKey.startsWith("deliverables:") || dKey.startsWith("deliverables-links:")
              ? false
              : Boolean(reqBody.has_non_empty_text),
          is_current: true,
        });
        return makeJsonResponse(200, {
          claim_status: "claimed",
          platform: "feishu",
          target_address: "feishu:@me",
          recipient_id: "ou_alice",
          dm_recipient_platform_user_id: "ou_alice",
          is_current: true,
          delivery_attempt: 1,
          workspace_id: "ws-contract-1",
          home_workspace_id: "ws-home-alice",
          prior_run_files: [
            { workspace_id: "ws-contract-1", path: "results/file_1.pdf", status: "unknown" },
          ],
        });
      }
      if (url.pathname.endsWith("/deliveries/progress")) {
        const reqBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        const dKey = String(reqBody.delivery_key ?? "");
        const st = typeof reqBody.status === "string" ? reqBody.status : undefined;
        progressWrites.push({ key: dKey, status: st });
        const row = deliveriesDb.get(dKey);
        if (row && st) {
          row.status = st;
        }
        return makeJsonResponse(200, {
          ok: true,
          delivery_key: dKey,
          status: st ?? "in_flight",
          sent_to_current_count: 1,
        });
      }
      if (url.pathname.endsWith("/deliverables/download")) {
        downloadCalls.push(url.toString());
        throw new Error("Must never download or re-upload binaries during unknown deliverable recovery");
      }
      if (url.pathname.endsWith("/status")) {
        return makeJsonResponse(200, {
          status: "idle",
          run_id: runId,
          latest_turn_index: 1,
        });
      }
      throw new Error(`Unexpected fetch URL: ${url.toString()}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://backend.test",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);
    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({
      backend,
      adapters,
      webBaseUrl: WEB_BASE,
    });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });

    // Simulate two gateway restarts hitting the real awaited replay entrypoint (`deliverReplayFallback`,
    // invoked by both `recoverSingleInboundRow` during startup recovery and `handleInboundEvent` on fallback_pending rows).
    for (let restart = 1; restart <= 2; restart += 1) {
      const orch = new ChannelGatewayOrchestrator({
        backend,
        adapters,
        deliveryService,
        agentRoutes,
        webBaseUrl: WEB_BASE,
        backoffDelayFn: async () => undefined,
      });
      const settled = await orch.deliverReplayFallback({
        userId: "user-alice",
        threadId,
        runId,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "om_6files_in",
        scopeKey: "dm:ou_alice",
        generation: 1,
      });
      expect(settled).toBe(true);
      await orch.stopAllWatches();
    }

    // 1) Original `deliverables:${runId}` stayed `unknown` and was never mutated via I-7
    expect(deliveriesDb.get(`deliverables:${runId}`)?.status).toBe("unknown");
    expect(progressWrites.some((w) => w.key === `deliverables:${runId}`)).toBe(false);
    // 2) Zero binary downloads and zero binary uploads
    expect(downloadCalls).toHaveLength(0);
    expect(feishu.sentFiles).toHaveLength(0);
    // 3) Login links sent ONCE via `deliverables-links:${runId}` for the 5 unconfirmed files,
    //    without duplicating file_6.pdf (whose overflow link was already sent in chunk 0).
    expect(feishu.sentTexts).toHaveLength(1);
    for (const n of [1, 2, 3, 4, 5]) {
      expect(feishu.sentTexts[0]!.text).toContain(`results%2Ffile_${n}.pdf`);
    }
    expect(feishu.sentTexts[0]!.text).not.toContain("results%2Ffile_6.pdf");
    // Real `deliverReplayFallback` also checks `fallback:${runId}` via `settleTurnFallbackAndPartialNotice`
    // when `sent_to_current_count > 0` before settling `deliverables:${runId}` -> `deliverables-links:${runId}`.
    expect(claimedKeys).toEqual([
      `fallback:${runId}`,
      `deliverables:${runId}`,
      `deliverables-links:${runId}`,
      `fallback:${runId}`,
      `deliverables:${runId}`,
      `deliverables-links:${runId}`,
    ]);
  });

  it("matches shared workspace link rewrite fixture (spaced filenames & multiline reference definitions) identically to Python", () => {
    const fixture = loadFixture<{
      public_base: string;
      thread_id: string;
      cases: Array<{ name: string; input: string; expected: string }>;
    }>("workspace_link_rewrite_cases.json");

    expect(fixture.cases.length).toBeGreaterThanOrEqual(4);
    for (const c of fixture.cases) {
      const actual = rewriteSandboxLinks(
        c.input,
        fixture.public_base,
        fixture.thread_id,
      );
      expect(actual, `Case ${c.name}`).toBe(c.expected);
    }
  });
});
