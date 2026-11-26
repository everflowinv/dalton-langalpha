import { describe, expect, it, vi } from "vitest";
import type { BackendClient, I6AuthorizeAndClaimRequest, I6AuthorizeAndClaimResponse, I7DeliveriesProgressRequest } from "../src/backend-client.js";
import type { ChannelAdapter, SendFileOptions, SendFileResult, SendTextChunkOptions, SendTextChunkResult } from "../src/channels/adapter.js";
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
      ok: true,
      platform: "feishu",
      recipient_id: "ou_alice",
      user_id: "user-alice",
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
          return { ok: true, platform: "feishu", recipient_id: "ou_alice" };
        }
        return {
          ok: false,
          code: "dm_recipient_unbound",
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
});
