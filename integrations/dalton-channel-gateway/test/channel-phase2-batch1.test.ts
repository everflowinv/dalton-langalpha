import { describe, expect, it, vi } from "vitest";
import { AgentRoutesHandler } from "../src/agent-routes.js";
import { BackendHttpError, type BackendClient, type I6AuthorizeAndClaimRequest, type I6AuthorizeAndClaimResponse, type I7DeliveriesProgressRequest } from "../src/backend-client.js";
import { ChannelDeliveryError, type ChannelAdapter, type SendFileOptions, type SendFileResult, type SendTextChunkOptions, type SendTextChunkResult } from "../src/channels/adapter.js";
import { DiscordChannelAdapter } from "../src/channels/discord/index.js";
import { DiscordError } from "../src/channels/discord/rest.js";
import { sendMessageDiscord, threadIdFromScopeKey } from "../src/channels/discord/send.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/monitor.js";
import { parseFeishuInboundEvent } from "../src/channels/feishu/inbound-parse.js";
import { sendReplyOrFallbackDirect } from "../src/channels/feishu/send.js";
import { DeliveryService } from "../src/delivery-service.js";
import { rewriteSandboxLinks } from "../src/shared/sandbox-links.js";
import type { ChannelPlatform, InboundChannelEvent } from "../src/types.js";

const WEB_BASE = "https://dalton.example.com";
const THREAD_ID = "tid-phase2-1";

function makeStubAdapter(
  platform: ChannelPlatform,
  overrides: Partial<ChannelAdapter> = {},
): ChannelAdapter & {
  sentTexts: SendTextChunkOptions[];
  sentFiles: SendFileOptions[];
  recordedTopics: Array<{
    chatAddress: string;
    externalTopicId: string;
    canonicalScopeKey: string;
    initiatorPlatformUserId: string;
    rootMessageId?: string | null;
  }>;
} {
  const sentTexts: SendTextChunkOptions[] = [];
  const sentFiles: SendFileOptions[] = [];
  const recordedTopics: Array<{
    chatAddress: string;
    externalTopicId: string;
    canonicalScopeKey: string;
    initiatorPlatformUserId: string;
    rootMessageId?: string | null;
  }> = [];

  const adapter: ChannelAdapter & {
    sentTexts: SendTextChunkOptions[];
    sentFiles: SendFileOptions[];
    recordedTopics: typeof recordedTopics;
  } = {
    platform,
    maxChunkChars: 4000,
    sentTexts,
    sentFiles,
    recordedTopics,
    isConfigured: () => true,
    isConnected: () => true,
    start: async () => undefined,
    stop: async () => undefined,
    onInboundEvent: () => undefined,
    splitTextChunks: (text: string) => (text ? [text] : []),
    sendTextChunk: vi.fn(async (opts: SendTextChunkOptions): Promise<SendTextChunkResult> => {
      sentTexts.push(opts);
      return {
        messageId: `${platform}_msg_${sentTexts.length}`,
        resolvedAddress: opts.targetAddress,
        externalTopicId:
          platform === "feishu" && opts.replyInThread ? "omt_created_1" : opts.externalTopicId ?? null,
      };
    }),
    sendFile: vi.fn(async (opts: SendFileOptions): Promise<SendFileResult> => {
      await opts.verifyRecipientBeforeSend?.();
      sentFiles.push(opts);
      return {
        messageId: `${platform}_file_${sentFiles.length}`,
        status: "sent",
        reason: null,
      };
    }),
    downloadAttachment: async () => ({
      content: Buffer.from(""),
      filename: "a.txt",
      mimeType: "text/plain",
    }),
    listAvailableChats: async () => ({ chats: [], complete: true, error: null }),
    recordBotTopic: (info) => {
      recordedTopics.push(info);
    },
    ...overrides,
  };
  return adapter;
}

function makeMockBackend(overrides: Partial<BackendClient> = {}): BackendClient {
  return {
    authorizeAndClaimDelivery: vi.fn(
      async (_req: I6AuthorizeAndClaimRequest): Promise<I6AuthorizeAndClaimResponse> => ({
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
        delivery_attempt: 1,
        home_workspace_id: "ws-home",
      }),
    ),
    updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => ({
      ok: true,
      delivery_key: req.delivery_key,
      status: req.status ?? "in_flight",
      sent_to_current_count: 1,
      confirmed_thread_address: req.confirmed_thread_address ?? null,
      discord_body_phase: req.discord_body_phase ?? (req.confirmed_thread_address ? "before_body" : null),
    })),
    verifyDmDeliveryRecipient: vi.fn(async () => ({
      authorized: true,
      platform: "feishu",
      recipient_id: "ou_alice",
      user_id: "user-alice",
      delivery_attempt: 1,
    })),
    downloadVerifiedDeliverable: vi.fn(async () => ({
      content: Buffer.from("%PDF-1.4\nhello"),
      mimeType: "application/pdf",
      filename: "report.pdf",
      sha256: "abc123",
    })),
    downloadWorkspaceFile: vi.fn(async () => ({
      content: Buffer.from("workspace content"),
      mimeType: "text/plain",
    })),
    lookupBotTopic: vi.fn(async () => ({ found: false })),
    ...overrides,
  } as unknown as BackendClient;
}

describe("Channel Phase 2 Batch 1 — Link Rewriting & Chunk Safety", () => {
  it("expands [ref], [ref][], and [text][ref] reference links inline and supports workspaceId fallback", () => {
    const input = [
      "见 [完整报告][r1]、[r2][]、[r3] 与 [跨工作区](__wsref__/ws-other/charts/c.png)。",
      "",
      "[r1]: /home/workspace/results/report.md",
      "[r2]: results/table.csv",
      "[r3]: notes/memo.md",
    ].join("\n");

    const withThread = rewriteSandboxLinks(input, WEB_BASE, THREAD_ID);
    expect(withThread).not.toContain("[r1]:");
    expect(withThread).not.toContain("[r2]:");
    expect(withThread).not.toContain("[r3]:");
    expect(withThread).toContain(
      `[完整报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md)`,
    );
    expect(withThread).toContain(
      `[r2](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Ftable.csv)`,
    );
    expect(withThread).toContain(
      `[r3](${WEB_BASE}/chat/t/${THREAD_ID}?file=notes%2Fmemo.md)`,
    );
    expect(withThread).toContain(
      `[跨工作区](${WEB_BASE}/chat/ws-other?file=charts%2Fc.png)`,
    );

    // Without threadId, falls back to workspaceId route
    const withWsOnly = rewriteSandboxLinks(
      "见 [完整报告](results/report.md)。",
      WEB_BASE,
      null,
      "ws-fallback-1",
    );
    expect(withWsOnly).toBe(
      `见 [完整报告](${WEB_BASE}/chat/ws-fallback-1?file=results%2Freport.md)。`,
    );
  });
});

describe("Channel Phase 2 Batch 1 — Group Auto-Topics (Feishu & Discord)", () => {
  it("creates Feishu topic on first turn from realtime, /agent/send, and replay using I-6 root_message_id", async () => {
    for (const source of [
      { kind: "fallback" as const, key: "fallback:run-rt", replyTo: "om_root_1" },
      { kind: "agent_send" as const, key: "run-as:tc-1", replyTo: null },
      { kind: "fallback" as const, key: "fallback:run-replay", replyTo: null },
    ]) {
      const feishu = makeStubAdapter("feishu");
      const progressCalls: I7DeliveriesProgressRequest[] = [];
      const backend = makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:oc_grp_1",
          recipient_id: "oc_grp_1",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:oc_grp_1:topic:om_root_1",
          root_message_id: "om_root_1",
          external_topic_id: null,
          topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
          topic_initiator_platform_user_id: "ou_alice",
        })),
        updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
          progressCalls.push(req);
          return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
        }),
      });

      const svc = new DeliveryService({
        backend,
        adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
        webBaseUrl: WEB_BASE,
      });

      const res = await svc.executeDelivery({
        deliveryKey: source.key,
        kind: source.kind,
        userId: "user-alice",
        threadId: THREAD_ID,
        runId: "run-1",
        platform: "feishu",
        target: source.kind === "agent_send" ? null : "feishu:oc_grp_1",
        text: "首轮结论",
        replyToMessageId: source.replyTo,
      });

      expect(res.status).toBe("sent");
      expect(feishu.sentTexts).toHaveLength(1);
      expect(feishu.sentTexts[0]!.replyInThread).toBe(true);
      expect(feishu.sentTexts[0]!.replyToMessageId).toBe("om_root_1");
      expect(progressCalls.some((p) => p.bot_topic_registration?.external_topic_id === "omt_created_1")).toBe(true);
      expect(feishu.recordedTopics).toHaveLength(1);
      expect(feishu.recordedTopics[0]!.externalTopicId).toBe("omt_created_1");
    }
  });

  it("allows Feishu initiator mention-free follow-ups inside confirmed bot topic and drops non-initiator & main-chat replies", async () => {
    const received: InboundChannelEvent[] = [];
    const lookupMock = vi.fn(async () => ({
      found: true,
      status: "confirmed",
      platform: "feishu",
      chat_address: "feishu:oc_grp_1",
      canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
      external_topic_id: "omt_topic_1",
      root_message_id: "om_root_1",
      initiator_platform_user_id: "ou_alice",
    }));
    const backend = makeMockBackend({ lookupBotTopic: lookupMock });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      botOpenId: "ou_bot",
    });
    adapter.setBackendClient(backend);
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    // 1) Main-chat quoted reply (root_id only, no thread_id, no @bot) -> dropped in memory without lookup
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
      message: {
        message_id: "om_main_quote",
        root_id: "om_root_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "主群引用不触发" }),
      },
    });
    expect(received).toHaveLength(0);
    expect(lookupMock).not.toHaveBeenCalled();

    // 2) Initiator (Alice) mention-free follow-up inside confirmed topic -> accepted with canonical :topic: scope
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
      message: {
        message_id: "om_followup_alice",
        thread_id: "omt_topic_1",
        root_id: "om_root_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "继续分析利润率" }),
      },
    });
    expect(lookupMock).toHaveBeenCalledTimes(1);
    expect(received).toHaveLength(1);
    expect(received[0]!.threadScopeKey).toBe("chat:oc_grp_1:topic:om_root_1");
    expect(received[0]!.isGroupTrigger).toBe(true);

    // 3) Non-initiator (Bob) mention-free message inside same topic -> dropped (cached positive hit)
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob" }, sender_type: "user" },
      message: {
        message_id: "om_followup_bob",
        thread_id: "omt_topic_1",
        root_id: "om_root_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "我来围观" }),
      },
    });
    expect(received).toHaveLength(1);
    expect(lookupMock).toHaveBeenCalledTimes(1);

    // 4) Top-level group @bot sets :topic:<messageId>
    const parsedTop = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_top_at",
          chat_id: "oc_grp_1",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_user_1 看下财报" }),
          mentions: [{ key: "@_user_1", id: { open_id: "ou_bot" }, name: "Dalton" }],
        },
      },
      { botOpenId: "ou_bot" },
    );
    expect(parsedTop?.threadScopeKey).toBe("chat:oc_grp_1:topic:om_top_at");
  });

  it("falls back to im.message.create on Feishu 230011 even when replyInThread is true", async () => {
    const createMock = vi.fn(async () => ({ code: 0, data: { message_id: "om_direct_1" } }));
    const replyMock = vi.fn(async () => ({ code: 230011, msg: "message withdrawn" }));
    const fakeClient = {
      im: { message: { create: createMock, reply: replyMock } },
    } as unknown as Parameters<typeof sendReplyOrFallbackDirect>[0];

    const res = await sendReplyOrFallbackDirect(fakeClient, {
      replyToMessageId: "om_deleted",
      replyInThread: true,
      content: JSON.stringify({ text: "hello" }),
      msgType: "text",
      target: "oc_grp_1",
      receiveIdType: "chat_id",
    });
    expect(res.data?.message_id).toBe("om_direct_1");
    expect(replyMock).toHaveBeenCalledTimes(1);
    expect(createMock).toHaveBeenCalledTimes(1);
  });

  it("passes messageId to Discord thread creation, routes by externalTopicId, and falls back on 50013 and 10008", async () => {
    expect(threadIdFromScopeKey("chat:111/222:topic:999")).toBeNull();
    expect(threadIdFromScopeKey("chat:111/222:thread:333")).toBe("333");

    // 1) 50013 Missing Permissions -> falls back to parent channel with message_reference
    const postCalls50013: Array<{ path: string; body: Record<string, unknown> }> = [];
    const rest50013 = {
      get: vi.fn(async (path: string) => {
        if (path === "/channels/222") return { id: "222", type: 0, guild_id: "111" };
        if (path === "/channels/999") throw new DiscordError(404, { code: 10003 }, "Not found");
        return { threads: [], members: [] };
      }),
      post: vi.fn(async (path: string, body: Record<string, unknown>) => {
        postCalls50013.push({ path, body });
        if (path.endsWith("/messages/999/threads")) {
          throw new DiscordError(403, { code: 50013 }, "Missing Permissions");
        }
        return { id: "msg_parent_fallback" };
      }),
      patch: vi.fn(async () => ({})),
    } as unknown as Parameters<typeof sendMessageDiscord>[0];

    const res50013 = await sendMessageDiscord(rest50013, {
      targetAddress: "discord:111/222",
      text: "首轮回复内容",
      replyToMessageId: "999",
      newThread: true,
      threadScopeKey: "chat:111/222:topic:999",
    });
    expect(res50013.messageId).toBe("msg_parent_fallback");
    expect(postCalls50013[0]!.path).toBe("/channels/222/messages/999/threads");
    expect(postCalls50013[1]!.path).toBe("/channels/222/messages");
    expect(postCalls50013[1]!.body.message_reference).toEqual({ message_id: "999" });

    // 2) 10008 Starter message deleted -> falls back to parent notice with web link and NO message_reference
    const postCalls10008: Array<{ path: string; body: Record<string, unknown> }> = [];
    const rest10008 = {
      get: vi.fn(async (path: string) => {
        if (path === "/channels/222") return { id: "222", type: 0, guild_id: "111" };
        if (path === "/channels/999") throw new DiscordError(404, { code: 10003 }, "Not found");
        return { threads: [], members: [] };
      }),
      post: vi.fn(async (path: string, body: Record<string, unknown>) => {
        postCalls10008.push({ path, body });
        if (path.endsWith("/messages/999/threads")) {
          throw new DiscordError(404, { code: 10008 }, "Unknown Message");
        }
        return { id: "msg_deleted_notice" };
      }),
      patch: vi.fn(async () => ({})),
    } as unknown as Parameters<typeof sendMessageDiscord>[0];

    const res10008 = await sendMessageDiscord(rest10008, {
      targetAddress: "discord:111/222",
      text: "首轮回复内容",
      replyToMessageId: "999",
      newThread: true,
      threadScopeKey: "chat:111/222:topic:999",
      webBaseUrl: WEB_BASE,
      threadId: THREAD_ID,
    });
    expect(res10008.messageId).toBe("msg_deleted_notice");
    expect(postCalls10008[1]!.body.message_reference).toBeUndefined();
    expect(String(postCalls10008[1]!.body.content)).toContain(
      `${WEB_BASE}/chat/t/${THREAD_ID}`,
    );

    // 3) Discord mention-free follow-up in confirmed bot thread routes to canonical :topic: scope
    const receivedDiscord: InboundChannelEvent[] = [];
    const discordLookup = vi.fn(async () => ({
      found: true,
      status: "confirmed",
      platform: "discord",
      chat_address: "discord:111/222",
      canonical_scope_key: "chat:111/222:topic:999",
      external_topic_id: "999",
      initiator_platform_user_id: "user_alice_disc",
    }));
    const discordAdapter = new DiscordChannelAdapter({
      botToken: "tok",
      botUserId: "bot_1",
      rest: rest50013,
    });
    discordAdapter.setBackendClient(makeMockBackend({ lookupBotTopic: discordLookup }));
    discordAdapter.onInboundEvent(async (ev) => {
      receivedDiscord.push(ev);
    });
    await discordAdapter.handleDispatch("THREAD_CREATE", {
      id: "999",
      type: 11,
      guild_id: "111",
      parent_id: "222",
      name: "Topic",
    });
    await discordAdapter.dispatchMessage({
      id: "msg_disc_follow_1",
      channel_id: "999",
      guild_id: "111",
      content: "追问一下估值",
      author: { id: "user_alice_disc", username: "alice" },
      mentions: [],
    });
    expect(receivedDiscord).toHaveLength(1);
    expect(receivedDiscord[0]!.threadScopeKey).toBe("chat:111/222:topic:999");
    expect(receivedDiscord[0]!.isGroupTrigger).toBe(true);
  });
});

describe("Channel Phase 2 Batch 1 — Group Deep Links & DM Deliverables", () => {
  it("converts group target send_message(files=...) into web login deep links after I-6 with zero download/upload", async () => {
    const feishu = makeStubAdapter("feishu");
    const downloadMock = vi.fn();
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:oc_grp_1",
        recipient_id: "oc_grp_1",
        is_current: true,
        delivery_attempt: 1,
        home_workspace_id: "ws-1",
      })),
      downloadWorkspaceFile: downloadMock,
    });
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "run-grp:tc-1",
      kind: "agent_send",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-grp",
      platform: "feishu",
      target: "feishu:oc_grp_1",
      text: "群里看下报告",
      files: [{ path: "results/report.pdf", workspace_id: "ws-1" }],
    });

    expect(res.status).toBe("sent");
    expect(res.files).toEqual([
      {
        path: "results/report.pdf",
        status: "linked",
        reason: "group_target_deep_link",
      },
    ]);
    expect(downloadMock).not.toHaveBeenCalled();
    expect(feishu.sentFiles).toHaveLength(0);
    expect(feishu.sentTexts[0]!.text).toContain(
      `${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.pdf`,
    );
  });

  it("aborts DM file send when recipient unbinds after download and before send (Decision #6)", async () => {
    const feishu = makeStubAdapter("feishu");
    let verifyCalls = 0;
    const backend = makeMockBackend({
      verifyDmDeliveryRecipient: vi.fn(async () => {
        verifyCalls += 1;
        // First check before upload passes; second check inside adapter after upload & before message send fails (unbound!)
        if (verifyCalls === 1) {
          return {
            authorized: true,
            platform: "feishu",
            recipient_id: "ou_alice",
            delivery_attempt: 1,
          };
        }
        return {
          authorized: false,
          code: "deliverable_recipient_denied",
          message: "DM recipient was unbound before send",
        };
      }),
    });
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-unbind",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-unbind",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(verifyCalls).toBe(2);
    expect(feishu.sentFiles).toHaveLength(0);
    expect(res.status).toBe("failed");
    expect(res.code).toBe("not_allowed");
  });

  it("deduplicates DM deliverables against prior_run_files: skips sent and converts pending/unknown to link-only", async () => {
    const feishu = makeStubAdapter("feishu");
    const progressCalls: I7DeliveriesProgressRequest[] = [];
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
        delivery_attempt: 1,
        home_workspace_id: "ws-1",
        prior_run_files: [
          { workspace_id: "ws-1", path: "results/already.pdf", status: "sent" },
          { workspace_id: "ws-1", path: "results/crashed.pdf", status: "pending" },
        ],
      })),
      updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
        progressCalls.push(req);
        return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
      }),
    });

    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-dedupe",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-dedupe",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [
        { path: "results/already.pdf", workspace_id: "ws-1" },
        { path: "results/crashed.pdf", workspace_id: "ws-1" },
        { path: "results/fresh.pdf", workspace_id: "ws-1" },
      ],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(res.status).toBe("sent");
    // Only `fresh.pdf` was uploaded as a binary file; `already.pdf` was skipped; `crashed.pdf` became link-only!
    expect(feishu.sentFiles.map((f) => f.path)).toEqual(["results/fresh.pdf"]);
    expect(feishu.sentTexts).toHaveLength(1);
    expect(feishu.sentTexts[0]!.text).toContain("results%2Fcrashed.pdf");
    // Pending entry was persisted via I-7 before sending `fresh.pdf`
    expect(
      progressCalls.some((p) =>
        p.files_status?.some((f) => f.path === "results/fresh.pdf" && f.status === "pending"),
      ),
    ).toBe(true);
  });

  it("emits link-only fallback for background DM deliverables when dm_recipient_platform_user_id is absent", async () => {
    const feishu = makeStubAdapter("feishu");
    const downloadDeliv = vi.fn();
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_alice",
        dm_recipient_platform_user_id: null,
        is_current: true,
        delivery_attempt: 1,
        home_workspace_id: "ws-1",
      })),
      downloadVerifiedDeliverable: downloadDeliv,
    });

    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-bg",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-bg",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(res.status).toBe("sent");
    expect(downloadDeliv).not.toHaveBeenCalled();
    expect(feishu.sentFiles).toHaveLength(0);
    expect(feishu.sentTexts[0]!.text).toContain("results%2Freport.pdf");
  });

  it("keeps Feishu :thread: and confirmed :topic: replies inside thread, prefers current inbound anchor, uses registered anchors on replay/background, and refuses main-chat fallback once topic exists", async () => {
    // 1) Other member `@bot` inside a bot topic (`:thread:` scope with authorized `external_topic_id`):
    //    multi-chunk and `/agent/send` both set `replyInThread: true` and `allowDirectFallback: false`.
    const feishuThread = makeStubAdapter("feishu", {
      splitTextChunks: (text: string) => text.split("||"),
    });
    const svcThread = new DeliveryService({
      backend: makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:oc_grp_1",
          recipient_id: "oc_grp_1",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:oc_grp_1:thread:omt_topic_1",
          inbound_message_id: "om_bob_msg_1",
          root_message_id: "om_root_1",
          external_topic_id: "omt_topic_1",
          first_reply_message_id: "om_alice_reply_1",
          topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
          topic_initiator_platform_user_id: "ou_alice",
        })),
      }),
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuThread]]),
      webBaseUrl: WEB_BASE,
    });
    const resThread = await svcThread.executeDelivery({
      deliveryKey: "run-bob:tc-1",
      kind: "agent_send",
      userId: "user-bob",
      threadId: THREAD_ID,
      runId: "run-bob",
      platform: "feishu",
      target: null,
      text: "第一段||第二段",
      activityOrigin: "conversation",
    });
    expect(resThread.status).toBe("sent");
    expect(feishuThread.sentTexts).toHaveLength(2);
    for (const sent of feishuThread.sentTexts) {
      expect(sent.replyInThread).toBe(true);
      expect(sent.allowDirectFallback).toBe(false);
      // Live turn prefers current inbound message (`om_bob_msg_1`) over historical first reply
      expect(sent.replyToMessageId).toBe("om_bob_msg_1");
      expect(sent.fallbackReplyToMessageIds).toEqual(["om_alice_reply_1", "om_root_1"]);
    }

    // 2) Replay & background report-back in confirmed `:topic:` prefer registered `first_reply_message_id`
    for (const origin of ["replay", "notification"] as const) {
      const feishuBg = makeStubAdapter("feishu");
      const svcBg = new DeliveryService({
        backend: makeMockBackend({
          authorizeAndClaimDelivery: vi.fn(async () => ({
            claim_status: "claimed" as const,
            platform: "feishu",
            target_address: "feishu:oc_grp_1",
            recipient_id: "oc_grp_1",
            is_current: true,
            delivery_attempt: 1,
            thread_scope_key: "chat:oc_grp_1:topic:om_root_1",
            inbound_message_id: origin === "replay" ? "om_followup_2" : null,
            root_message_id: "om_root_1",
            external_topic_id: "omt_topic_1",
            first_reply_message_id: "om_first_reply_1",
            topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
            topic_initiator_platform_user_id: "ou_alice",
          })),
        }),
        adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuBg]]),
        webBaseUrl: WEB_BASE,
      });
      const resBg = await svcBg.executeDelivery({
        deliveryKey: `fallback:run-${origin}`,
        kind: "fallback",
        userId: "user-alice",
        threadId: THREAD_ID,
        runId: `run-${origin}`,
        platform: "feishu",
        target: "feishu:oc_grp_1",
        text: "后台回报",
        activityOrigin: origin,
      });
      expect(resBg.status).toBe("sent");
      expect(feishuBg.sentTexts[0]!.replyInThread).toBe(true);
      expect(feishuBg.sentTexts[0]!.allowDirectFallback).toBe(false);
      expect(feishuBg.sentTexts[0]!.replyToMessageId).toBe("om_first_reply_1");
    }

    // 3) Established topic with withdrawn primary anchor tries fallback topic anchor first,
    //    and if all anchors are withdrawn (`230011`), NEVER calls `im.message.create` (main group).
    const createMock = vi.fn(async () => ({ code: 0, data: { message_id: "om_leaked_main" } }));
    const replyMock = vi.fn(async (req: { path: { message_id: string } }) => {
      if (req.path.message_id === "om_withdrawn_first") {
        return { code: 230011, msg: "message withdrawn" };
      }
      if (req.path.message_id === "om_live_backup") {
        return { code: 0, data: { message_id: "om_ok_in_topic", thread_id: "omt_topic_1" } };
      }
      return { code: 230011, msg: "message withdrawn" };
    });
    const fakeClient = {
      im: { message: { create: createMock, reply: replyMock } },
    } as unknown as Parameters<typeof sendReplyOrFallbackDirect>[0];

    const recovered = await sendReplyOrFallbackDirect(fakeClient, {
      replyToMessageId: "om_withdrawn_first",
      fallbackReplyToMessageIds: ["om_live_backup"],
      replyInThread: true,
      allowDirectFallback: false,
      content: JSON.stringify({ text: "hello" }),
      msgType: "text",
      target: "oc_grp_1",
      receiveIdType: "chat_id",
    });
    expect(recovered.data?.message_id).toBe("om_ok_in_topic");
    expect(createMock).not.toHaveBeenCalled();

    await expect(
      sendReplyOrFallbackDirect(fakeClient, {
        replyToMessageId: "om_withdrawn_first",
        fallbackReplyToMessageIds: ["om_also_withdrawn"],
        replyInThread: true,
        allowDirectFallback: false,
        content: JSON.stringify({ text: "hello" }),
        msgType: "text",
        target: "oc_grp_1",
        receiveIdType: "chat_id",
      }),
    ).rejects.toThrow(/withdrawn|unavailable/i);
    expect(createMock).not.toHaveBeenCalled();
  });

  it("generates target-workspace deep link for cross-workspace group attachments even when both workspaces have identically named files", async () => {
    const feishu = makeStubAdapter("feishu");
    const downloadMock = vi.fn();
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:oc_grp_1",
        recipient_id: "oc_grp_1",
        is_current: true,
        delivery_attempt: 1,
        workspace_id: "ws-home",
        home_workspace_id: "ws-home",
      })),
      downloadWorkspaceFile: downloadMock,
    });
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "run-cos-grp:tc-1",
      kind: "agent_send",
      userId: "user-alice",
      threadId: "tid-home-1",
      workspaceId: "ws-home",
      runId: "run-cos-grp",
      platform: "feishu",
      target: "feishu:oc_grp_1",
      text: "附上两份同名报告",
      files: [
        { path: "results/report.pdf", workspace_id: "ws-home" },
        { path: "results/report.pdf", workspace_id: "ws-nvda" },
      ],
    });

    expect(res.status).toBe("sent");
    expect(downloadMock).not.toHaveBeenCalled();
    expect(feishu.sentFiles).toHaveLength(0);
    const sentText = feishu.sentTexts[0]!.text;
    // Same-workspace file uses thread URL; cross-workspace file uses target workspace URL
    expect(sentText).toContain(
      `${WEB_BASE}/chat/t/tid-home-1?file=results%2Freport.pdf`,
    );
    expect(sentText).toContain(
      `${WEB_BASE}/chat/ws-nvda?file=results%2Freport.pdf`,
    );
  });

  it("keeps Feishu multi-chunk topic replies inside thread when chunk 0 succeeds without thread_id, and stops as `unknown` if location cannot be confirmed", async () => {
    // 1) Chunk 0 succeeds via `im.message.reply({ reply_in_thread: true })` without returning `thread_id`,
    //    and `im.message.get` also omits `thread_id` -> chunk 1 MUST still reply in thread anchored on
    //    chunk 0's confirmed messageId (NEVER falling back to `im.message.create` in main group).
    const createMock = vi.fn(async () => ({ code: 0, data: { message_id: "om_leaked_main" } }));
    const replyCalls: Array<{ message_id: string; reply_in_thread?: boolean }> = [];
    let replySeq = 0;
    const replyMock = vi.fn(
      async (req: { path: { message_id: string }; data: { reply_in_thread?: boolean } }) => {
        replySeq += 1;
        replyCalls.push({
          message_id: req.path.message_id,
          reply_in_thread: req.data.reply_in_thread,
        });
        return {
          code: 0,
          data: {
            message_id: `om_topic_reply_${replySeq}`,
            // Chunk 0 omits thread_id; chunk 1 eventually returns it
            ...(replySeq === 2 ? { thread_id: "omt_late_resolved_1" } : {}),
          },
        };
      },
    );
    const getMock = vi.fn(async () => ({
      code: 0,
      data: { items: [{ message_id: "om_topic_reply_1", chat_id: "oc_grp_1", msg_type: "post", body: { content: "{}" } }] },
    }));
    const feishuAdapter = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      botOpenId: "ou_bot",
      clientFactory: () =>
        ({
          im: {
            message: { create: createMock, reply: replyMock, get: getMock },
          },
        }) as unknown as ReturnType<
          NonNullable<NonNullable<ConstructorParameters<typeof FeishuChannelAdapter>[0]>["clientFactory"]>
        >,
    });
    vi.spyOn(feishuAdapter, "isConnected").mockReturnValue(true);
    vi.spyOn(feishuAdapter, "splitTextChunks").mockImplementation((text) => text.split("||"));

    const progressCalls: I7DeliveriesProgressRequest[] = [];
    const svc = new DeliveryService({
      backend: makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:oc_grp_1",
          recipient_id: "oc_grp_1",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:oc_grp_1:topic:om_root_1",
          root_message_id: "om_root_1",
          external_topic_id: null,
          topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
          topic_initiator_platform_user_id: "ou_alice",
        })),
        updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
          progressCalls.push(req);
          return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
        }),
      }),
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuAdapter]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "fallback:run-fs-no-tid",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-fs-no-tid",
      platform: "feishu",
      target: "feishu:oc_grp_1",
      text: "第一片结论||第二片结论",
    });

    expect(res.status).toBe("sent");
    expect(createMock).not.toHaveBeenCalled();
    expect(replyCalls).toEqual([
      { message_id: "om_root_1", reply_in_thread: true },
      { message_id: "om_topic_reply_1", reply_in_thread: true },
    ]);
    expect(
      progressCalls.some((p) => p.bot_topic_registration?.external_topic_id === "omt_late_resolved_1"),
    ).toBe(true);

    // 2) When chunk 0's location cannot be confirmed (neither externalTopicId, repliedInThread, nor usedDirectFallback),
    //    stop immediately and settle as `unknown` without sending chunk 1 to the main group.
    const unconfirmedFeishu = makeStubAdapter("feishu", {
      splitTextChunks: (text: string) => text.split("||"),
      sendTextChunk: vi.fn(async (opts: SendTextChunkOptions): Promise<SendTextChunkResult> => {
        unconfirmedFeishu.sentTexts.push(opts);
        return { messageId: "om_mystery_1", resolvedAddress: opts.targetAddress };
      }),
    });
    const svcUnconfirmed = new DeliveryService({
      backend: makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:oc_grp_1",
          recipient_id: "oc_grp_1",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:oc_grp_1:topic:om_root_1",
          root_message_id: "om_root_1",
          external_topic_id: null,
          topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_1",
          topic_initiator_platform_user_id: "ou_alice",
        })),
      }),
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", unconfirmedFeishu]]),
      webBaseUrl: WEB_BASE,
    });
    const resUnconfirmed = await svcUnconfirmed.executeDelivery({
      deliveryKey: "fallback:run-fs-unconfirmed",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-fs-unconfirmed",
      platform: "feishu",
      target: "feishu:oc_grp_1",
      text: "第一片||第二片绝不漏主群",
    });
    expect(resUnconfirmed.status).toBe("unknown");
    expect(unconfirmedFeishu.sentTexts).toHaveLength(1);
  });

  it("terminates multi-chunk Discord delivery after a single parent-channel web notice on 10008 (deleted starter message)", async () => {
    const postCalls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const rest = {
      get: vi.fn(async (path: string) => {
        if (path === "/channels/222") return { id: "222", type: 0, guild_id: "111" };
        if (path === "/channels/999") throw new DiscordError(404, { code: 10003 }, "Not found");
        return { threads: [], members: [] };
      }),
      post: vi.fn(async (path: string, body: Record<string, unknown>) => {
        postCalls.push({ path, body });
        if (path.endsWith("/messages/999/threads")) {
          throw new DiscordError(404, { code: 10008 }, "Unknown Message");
        }
        return { id: `msg_parent_${postCalls.length}` };
      }),
      patch: vi.fn(async () => ({})),
    };
    const discordAdapter = new DiscordChannelAdapter({
      botToken: "tok",
      botUserId: "bot_1",
      rest: rest as unknown as NonNullable<ConstructorParameters<typeof DiscordChannelAdapter>[0]>["rest"],
    });
    vi.spyOn(discordAdapter, "isConnected").mockReturnValue(true);
    vi.spyOn(discordAdapter, "splitTextChunks").mockImplementation((text) => text.split("||"));

    const progressCalls: I7DeliveriesProgressRequest[] = [];
    const svc = new DeliveryService({
      backend: makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "discord",
          target_address: "discord:111/222",
          recipient_id: "222",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:111/222:topic:999",
          root_message_id: "999",
          external_topic_id: null,
          topic_canonical_scope_key: "chat:111/222:topic:999",
          topic_initiator_platform_user_id: "user_alice_disc",
        })),
        updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
          progressCalls.push(req);
          return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
        }),
      }),
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["discord", discordAdapter]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "fallback:run-disc-10008-long",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-disc-10008-long",
      platform: "discord",
      target: "discord:111/222",
      text: "第一片长回答内容||第二片绝不能发到父频道||第三片也绝不能发到父频道",
      replyToMessageId: "999",
    });

    expect(res.status).toBe("sent");
    const msgPosts = postCalls.filter((c) => c.path === "/channels/222/messages");
    expect(msgPosts).toHaveLength(1);
    expect(String(msgPosts[0]!.body.content)).toContain(`${WEB_BASE}/chat/t/${THREAD_ID}`);
    expect(String(msgPosts[0]!.body.content)).not.toContain("第二片");
    const finalProg = progressCalls[progressCalls.length - 1]!;
    expect(finalProg.status).toBe("sent");
    expect(finalProg.sent_chunks).toBe(1);
    expect(finalProg.total_chunks).toBe(1);
  });

  it("deduplicates DM deliverables by (workspace_id, normalized_path) without colliding across workspaces", async () => {
    const feishu = makeStubAdapter("feishu");
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
        delivery_attempt: 1,
        workspace_id: "ws-home",
        home_workspace_id: "ws-home",
        prior_run_files: [
          // Chief of Staff explicitly sent ws-a's `results/report.pdf` earlier in the same turn
          { workspace_id: "ws-a", path: "results/report.pdf", status: "sent" },
          // Same-workspace file with unnormalized path should still dedupe
          { workspace_id: "ws-home", path: "./results//already-home.pdf", status: "sent" },
          // Legacy record missing workspace_id matches current session workspace only
          { workspace_id: null, path: "results/legacy-home.pdf", status: "sent" },
        ],
      })),
    });

    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-ws-dedupe",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      workspaceId: "ws-home",
      runId: "run-ws-dedupe",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [
        { path: "results/report.pdf", workspace_id: "ws-home" },
        { path: "results/already-home.pdf", workspace_id: "ws-home" },
        { path: "results/legacy-home.pdf", workspace_id: "ws-home" },
        { path: "results/legacy-home.pdf", workspace_id: "ws-b" },
      ],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(res.status).toBe("sent");
    // Home's `results/report.pdf` MUST NOT be skipped because ws-a sent a same-named file;
    // ws-b's `results/legacy-home.pdf` MUST NOT match a legacy null-workspace record belonging to Home.
    expect(feishu.sentFiles.map((f) => f.path)).toEqual([
      "results/report.pdf",
      "results/legacy-home.pdf",
    ]);
  });

  it("converts explicit DM `send_message(files=['results/report.html'])` into web login deep links with zero download and zero upload", async () => {
    const feishu = makeStubAdapter("feishu");
    const downloadWsMock = vi.fn();
    const downloadDelivMock = vi.fn();
    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async () => ({
        claim_status: "claimed" as const,
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
        delivery_attempt: 1,
        workspace_id: "ws-1",
        home_workspace_id: "ws-1",
      })),
      downloadWorkspaceFile: downloadWsMock,
      downloadVerifiedDeliverable: downloadDelivMock,
    });
    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({
      backend,
      adapters,
      webBaseUrl: WEB_BASE,
    });
    const routes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });

    const resp = await routes.handleAgentSend("user-alice", {
      thread_id: THREAD_ID,
      workspace_id: "ws-1",
      run_id: "run-dm-html",
      tool_call_id: "tc-html-1",
      turn_platform: "feishu",
      target: "feishu:@me",
      text: "请查看交互式 HTML 报告",
      files: [
        { path: "results/report.html", workspace_id: "ws-1" },
        { path: "results/chart.HTM", workspace_id: "ws-1" },
      ],
    });

    expect(resp.status).toBe(200);
    expect(resp.data.status).toBe("sent");
    expect(resp.data.files).toEqual([
      { path: "results/report.html", status: "linked", reason: "html_file_deep_link" },
      { path: "results/chart.HTM", status: "linked", reason: "html_file_deep_link" },
    ]);
    expect(downloadWsMock).not.toHaveBeenCalled();
    expect(downloadDelivMock).not.toHaveBeenCalled();
    expect(feishu.sentFiles).toHaveLength(0);
    expect(feishu.sentTexts).toHaveLength(1);
    expect(feishu.sentTexts[0]!.text).toContain(
      `${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.html`,
    );
    expect(feishu.sentTexts[0]!.text).toContain(
      `${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fchart.HTM`,
    );
  });

  it("falls back to web login deep link when deliverable download returns 503 `deliverable_sandbox_unavailable`", async () => {
    const feishu = makeStubAdapter("feishu");
    const backend = makeMockBackend({
      downloadVerifiedDeliverable: vi.fn(async () => {
        throw new BackendHttpError(
          "Workspace sandbox is stopping",
          503,
          { detail: { code: "deliverable_sandbox_unavailable", message: "stopping" } },
        );
      }),
    });
    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    const res = await svc.executeDelivery({
      deliveryKey: "deliverables:run-stopping-503",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      workspaceId: "ws-1",
      runId: "run-stopping-503",
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: [{ path: "results/report.pdf", workspace_id: "ws-1" }],
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    expect(res.status).toBe("sent");
    expect(feishu.sentFiles).toHaveLength(0);
    expect(feishu.sentTexts).toHaveLength(1);
    expect(feishu.sentTexts[0]!.text).toContain(
      `${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.pdf`,
    );
  });

  it("persists first_reply_message_id on Feishu single-chunk topic reply without thread_id and confirms initiator mention-free follow-up", async () => {
    const createMock = vi.fn(async () => ({ code: 0, data: { message_id: "om_leaked_main" } }));
    const replyMock = vi.fn(async () => ({
      code: 0,
      data: { message_id: "om_single_reply_1" },
    }));
    let allowDelayedThreadId = false;
    const getMock = vi.fn(async (req: { path: { message_id: string } }) => ({
      code: 0,
      data: {
        items: [
          {
            message_id: req.path.message_id,
            chat_id: "oc_grp_1",
            msg_type: "post",
            body: { content: "{}" },
            ...(allowDelayedThreadId && req.path.message_id === "om_single_reply_1"
              ? { thread_id: "omt_delayed_single_1" }
              : {}),
          },
        ],
      },
    }));

    const clientFactory = () =>
      ({
        im: { message: { create: createMock, reply: replyMock, get: getMock } },
      }) as unknown as ReturnType<
        NonNullable<NonNullable<ConstructorParameters<typeof FeishuChannelAdapter>[0]>["clientFactory"]>
      >;

    const feishuOut = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      botOpenId: "ou_bot",
      clientFactory,
    });
    vi.spyOn(feishuOut, "isConnected").mockReturnValue(true);

    const progressCalls: I7DeliveriesProgressRequest[] = [];
    const svc = new DeliveryService({
      backend: makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async () => ({
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:oc_grp_1",
          recipient_id: "oc_grp_1",
          is_current: true,
          delivery_attempt: 1,
          thread_scope_key: "chat:oc_grp_1:topic:om_root_single",
          root_message_id: "om_root_single",
          external_topic_id: null,
          topic_canonical_scope_key: "chat:oc_grp_1:topic:om_root_single",
          topic_initiator_platform_user_id: "ou_alice",
        })),
        updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
          progressCalls.push(req);
          return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
        }),
      }),
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuOut]]),
      webBaseUrl: WEB_BASE,
    });

    const outRes = await svc.executeDelivery({
      deliveryKey: "fallback:run-fs-single-chunk",
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-fs-single-chunk",
      platform: "feishu",
      target: "feishu:oc_grp_1",
      text: "单片回复内容",
    });

    expect(outRes.status).toBe("sent");
    expect(createMock).not.toHaveBeenCalled();
    expect(replyMock).toHaveBeenCalledTimes(1);
    const pendingReg = progressCalls.find((p) => p.bot_topic_registration)?.bot_topic_registration;
    expect(pendingReg).toBeDefined();
    expect(pendingReg?.external_topic_id).toBeNull();
    expect(pendingReg?.first_reply_message_id).toBe("om_single_reply_1");
    expect(pendingReg?.canonical_scope_key).toBe("chat:oc_grp_1:topic:om_root_single");

    // Simulate next mention-free inbound message from initiator Alice on a fresh adapter (post-restart):
    // backend has the pending topic row with persisted `first_reply_message_id`, and Feishu `im.message.get`
    // now returns `thread_id: "omt_delayed_single_1"`.
    allowDelayedThreadId = true;
    const received: InboundChannelEvent[] = [];
    const lookupMock = vi.fn(async () => ({
      found: false,
      status: null,
      canonical_scope_key: null,
      external_topic_id: null,
      root_message_id: null,
      first_reply_message_id: null,
      initiator_platform_user_id: null,
      initiator_dalton_user_id: null,
      pending_candidates: [
        {
          canonical_scope_key: "chat:oc_grp_1:topic:om_root_single",
          root_message_id: "om_root_single",
          first_reply_message_id: "om_single_reply_1",
          initiator_platform_user_id: "ou_alice",
          initiator_dalton_user_id: "user-alice",
        },
      ],
    }));

    const feishuIn = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      botOpenId: "ou_bot",
      clientFactory,
    });
    feishuIn.setBackendClient(makeMockBackend({ lookupBotTopic: lookupMock }));
    feishuIn.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    await feishuIn.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
      message: {
        message_id: "om_followup_single_alice",
        thread_id: "omt_delayed_single_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "继续追问单片话题" }),
      },
    });

    expect(lookupMock).toHaveBeenCalledTimes(1);
    expect(received).toHaveLength(1);
    expect(received[0]!.threadScopeKey).toBe("chat:oc_grp_1:topic:om_root_single");
    expect(received[0]!.rootMessageId).toBe("om_root_single");
    expect(received[0]!.isGroupTrigger).toBe(true);

    // Non-initiator mention-free message inside the same topic is still dropped
    await feishuIn.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob" }, sender_type: "user" },
      message: {
        message_id: "om_followup_single_bob",
        thread_id: "omt_delayed_single_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "非发起人发言" }),
      },
    });
    expect(received).toHaveLength(1);
  });

  it("tracks multi-chunk overflow login links per (workspace_id, path) and resumes only unsent links after restart", async () => {
    const runId = "run-overflow-multichunk";
    const overflowFiles = [
      { path: "results/overflow_1.pdf", workspace_id: "ws-a", filename: "overflow_1.pdf", reason: "deliverable_overflow_link" },
      { path: "results/overflow_2.pdf", workspace_id: "ws-a", filename: "overflow_2.pdf", reason: "deliverable_overflow_link" },
      { path: "results/overflow_3.pdf", workspace_id: "ws-b", filename: "overflow_3.pdf", reason: "deliverable_overflow_link" },
      { path: "results/overflow_4.pdf", workspace_id: "ws-b", filename: "overflow_4.pdf", reason: "deliverable_overflow_link" },
    ];
    const binaryFiles = [
      { path: "results/bin_1.pdf", workspace_id: "ws-a" },
    ];

    // Pack at most 2 lines per chunk so 4 overflow links span 2 chunks ([overflow_1, overflow_2] and [overflow_3, overflow_4]).
    let crashAfterChunk0 = true;
    const sentChunkTexts: string[] = [];
    const feishu = makeStubAdapter("feishu", {
      splitTextChunks: (text: string) => {
        const lines = text.split("\n").filter((l) => l.length > 0);
        const chunks: string[] = [];
        for (let idx = 0; idx < lines.length; idx += 2) {
          chunks.push(lines.slice(idx, idx + 2).join("\n"));
        }
        return chunks.length > 0 ? chunks : [text];
      },
      sendTextChunk: vi.fn(async (opts: SendTextChunkOptions): Promise<SendTextChunkResult> => {
        if (crashAfterChunk0 && opts.chunkIndex === 1) {
          throw new ChannelDeliveryError("Simulated gateway exit after chunk 0", {
            outcome: "unknown",
            code: "unavailable",
          });
        }
        sentChunkTexts.push(opts.text);
        return { messageId: `om_ov_${sentChunkTexts.length}`, resolvedAddress: opts.targetAddress };
      }),
    });

    const deliveriesState = new Map<
      string,
      {
        status: string;
        sent_chunks: number;
        files_status: Array<{ path: string; workspace_id?: string | null; status: string; reason: string | null }>;
      }
    >();

    const backend = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async (req) => {
        const existing = deliveriesState.get(req.delivery_key);
        if (existing && existing.status === "unknown") {
          return {
            claim_status: "in_flight_or_unknown" as const,
            existing_status: "unknown",
            sent_chunks: existing.sent_chunks,
            platform: "feishu",
            target_address: "feishu:@me",
            is_current: true,
            delivery_attempt: 1,
            files_status: existing.files_status,
          };
        }
        const priorRunFiles = Array.from(deliveriesState.entries())
          .filter(([k]) => k !== req.delivery_key)
          .flatMap(([k, row]) =>
            row.files_status.map((f) => ({
              delivery_key: k,
              delivery_status: row.status,
              workspace_id: f.workspace_id ?? null,
              path: f.path,
              status: f.status,
            })),
          );
        deliveriesState.set(req.delivery_key, {
          status: "in_flight",
          sent_chunks: 0,
          files_status: [],
        });
        return {
          claim_status: "claimed" as const,
          platform: "feishu",
          target_address: "feishu:@me",
          recipient_id: "ou_alice",
          dm_recipient_platform_user_id: "ou_alice",
          is_current: true,
          delivery_attempt: 1,
          workspace_id: "ws-a",
          home_workspace_id: "ws-home",
          prior_run_files: priorRunFiles,
        };
      }),
      updateDeliveryProgress: vi.fn(async (req: I7DeliveriesProgressRequest) => {
        const row = deliveriesState.get(req.delivery_key);
        if (row) {
          if (typeof req.sent_chunks === "number") row.sent_chunks = req.sent_chunks;
          if (Array.isArray(req.files_status)) row.files_status = [...req.files_status];
          if (typeof req.status === "string") row.status = req.status;
        }
        return { ok: true, delivery_key: req.delivery_key, status: req.status ?? "in_flight", sent_to_current_count: 1 };
      }),
    });

    const svc = new DeliveryService({
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
    });

    // Attempt 1: chunk 0 confirms overflow_1 & overflow_2, then gateway exits before chunk 1
    await svc.executeDelivery({
      deliveryKey: `deliverables:${runId}`,
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      workspaceId: "ws-a",
      runId,
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: binaryFiles,
      linkOnlyFiles: overflowFiles,
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
    });

    // Simulate gateway restart sweeping the unsettled delivery to `unknown`
    const crashedRow = deliveriesState.get(`deliverables:${runId}`)!;
    crashedRow.status = "unknown";
    expect(crashedRow.sent_chunks).toBe(1);
    expect(
      crashedRow.files_status.filter((f) => f.status === "linked").map((f) => `${f.workspace_id}::${f.path}`),
    ).toEqual(["ws-a::results/overflow_1.pdf", "ws-a::results/overflow_2.pdf"]);

    // Attempt 2 (post-restart recovery): must NOT resend overflow_1/overflow_2,
    // and MUST send remaining overflow_3/overflow_4 plus bin_1 login link.
    crashAfterChunk0 = false;
    sentChunkTexts.length = 0;

    const recovered = await svc.executeDelivery({
      deliveryKey: `deliverables:${runId}`,
      kind: "fallback",
      userId: "user-alice",
      threadId: THREAD_ID,
      workspaceId: "ws-a",
      runId,
      platform: "feishu",
      target: "feishu:@me",
      text: "",
      files: binaryFiles,
      linkOnlyFiles: overflowFiles,
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
      activityOrigin: "replay",
    });

    expect(recovered.status).toBe("sent");
    expect(feishu.sentFiles).toHaveLength(0);
    const recoveredCombined = sentChunkTexts.join("\n");
    expect(recoveredCombined).not.toContain("overflow_1.pdf");
    expect(recoveredCombined).not.toContain("overflow_2.pdf");
    expect(recoveredCombined).toContain("overflow_3.pdf");
    expect(recoveredCombined).toContain("overflow_4.pdf");
    expect(recoveredCombined).toContain("bin_1.pdf");
  });
});
