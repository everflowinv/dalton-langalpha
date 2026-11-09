import { PassThrough } from "node:stream";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import {
  buildDaltonFeishuChatAddress,
  buildDaltonFeishuThreadScopeKey,
  buildFeishuConversationId,
  buildFeishuPostMessageContent,
  buildStructuredCard,
  checkBotMentioned,
  chunkFeishuCardMarkdown,
  chunkFeishuOutboundText,
  chunkFeishuPostMarkdown,
  countMarkdownTables,
  createFeishuSdkLogger,
  detectIdType,
  extractInboundAttachments,
  FEISHU_CARD_MAX_BYTES,
  FEISHU_CARD_TABLE_LIMIT,
  FEISHU_POST_MAX_BYTES,
  FeishuChannelAdapter,
  type FeishuMentionItem,
  type FeishuWSClientLike,
  type FeishuWSClientOptions,
  formatFeishuTarget,
  looksLikeFeishuId,
  materializeFeishuPostMarkdownSoftBreaks,
  normalizeFeishuTarget,
  normalizeMentions,
  normalizeMultipartUploadData,
  parseFeishuConversationId,
  parseInteractiveCardContent,
  parseMergeForwardContent,
  parseMessageContent,
  parsePostContent,
  parseFeishuDirectConversationId,
  parseFeishuInboundEvent,
  parseFeishuTargetId,
  resolveDaltonOutboundTarget,
  resolveFeishuMessageDedupeKey,
  resolveReceiveIdType,
  shouldUseFeishuCard,
  withinCardTableLimit,
} from "../src/channels/feishu/index.js";
import { loadConfig } from "../src/config.js";
import { buildConfiguredAdapters } from "../src/main.js";
import { createMemoryDedupe } from "../src/shared/dedupe.js";
import { createSequentialQueue } from "../src/shared/keyed-queue.js";
import {
  createSubsystemLogger,
  redactCredentialString,
} from "../src/shared/logger.js";
import {
  isAllowedAttachmentHostname,
  isBlockedHostnameOrIp,
  readStreamWithLimit,
  SafeFetchError,
  safeFetchBuffer,
} from "../src/shared/safe-fetch.js";
import {
  chunkTextForOutbound,
  convertMarkdownTables,
} from "../src/shared/text.js";
import { downloadWorkspaceFileToBuffer } from "../src/shared/workspace-download.js";
import { uploadAttachmentToWorkspace } from "../src/shared/workspace-upload.js";
import type { InboundChannelEvent } from "../src/types.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import {
  createTestHarness,
  makeSseEventsFromFixture,
  RUN_ID,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

// --- Realistic Lark.Client Test Double (Strict Method Signatures) ---

interface MessageCreateCall {
  params: { receive_id_type: "chat_id" | "open_id" | "user_id" };
  data: { receive_id: string; content: string; msg_type: string };
}

interface MessageReplyCall {
  path: { message_id: string };
  data: { content: string; msg_type: string; reply_in_thread?: boolean };
}

interface ImageCreateCall {
  data: { image_type: "message" | "avatar"; image: Buffer };
}

interface FileCreateCall {
  data: {
    file_type: string;
    file_name: string;
    file: Buffer;
    duration?: number;
  };
}

interface MessageResourceGetCall {
  path: { message_id: string; file_key: string };
  params: { type: "image" | "file" | "media" };
}

interface ChatListCall {
  params: { page_size: number; page_token?: string };
}

interface ChatGetCall {
  path: { chat_id: string };
}

interface ChatMembersGetCall {
  path: { chat_id: string };
  params: {
    page_size: number;
    member_id_type: "open_id" | "user_id" | "union_id";
    page_token?: string;
  };
}

function createFakeLarkClient(overrides?: {
  onMessageCreate?: (
    call: MessageCreateCall,
  ) => Promise<{ code?: number; msg?: string; data?: { message_id?: string } }>;
  onMessageReply?: (
    call: MessageReplyCall,
  ) => Promise<{ code?: number; msg?: string; data?: { message_id?: string } }>;
  onImageCreate?: (
    call: ImageCreateCall,
  ) => Promise<{
    code?: number;
    msg?: string;
    image_key?: string;
    data?: { image_key?: string };
  }>;
  onFileCreate?: (
    call: FileCreateCall,
  ) => Promise<{
    code?: number;
    msg?: string;
    file_key?: string;
    data?: { file_key?: string };
  }>;
  onMessageResourceGet?: (
    call: MessageResourceGetCall,
  ) => Promise<unknown>;
  onChatList?: (
    call: ChatListCall,
  ) => Promise<{
    code?: number;
    msg?: string;
    data?: {
      items?: Array<{ chat_id?: string; name?: string }>;
      has_more?: boolean;
      page_token?: string;
    };
  }>;
  onChatGet?: (
    call: ChatGetCall,
  ) => Promise<{
    code?: number;
    msg?: string;
    data?: { name?: string; chat_mode?: string; chat_type?: string };
  }>;
    onChatMembersGet?: (
      call: ChatMembersGetCall,
    ) => Promise<{
      code?: number;
      msg?: string;
      data?: {
        has_more?: boolean;
        page_token?: string;
        items?: Array<{ member_id?: string; name?: string }>;
      };
    }>;
    onMessageGet?: (call: {
      params?: { card_msg_content_type?: string };
      path: { message_id: string };
    }) => Promise<{
      code?: number;
      msg?: string;
      data?: {
        items?: Array<Record<string, unknown>>;
      };
    }>;
    onRequest?: (call: {
      method: string;
      url: string;
      data?: Record<string, unknown>;
    }) => Promise<unknown>;
  }) {
    const calls = {
      messageCreate: [] as MessageCreateCall[],
      messageReply: [] as MessageReplyCall[],
      messageGet: [] as Array<{
        params?: { card_msg_content_type?: string };
        path: { message_id: string };
      }>,
      imageCreate: [] as ImageCreateCall[],
      fileCreate: [] as FileCreateCall[],
      messageResourceGet: [] as MessageResourceGetCall[],
      chatList: [] as ChatListCall[],
      chatGet: [] as ChatGetCall[],
      chatMembersGet: [] as ChatMembersGetCall[],
      request: [] as Array<{
        method: string;
        url: string;
        data?: Record<string, unknown>;
      }>,
    };

    let seq = 1;

    const client = {
      request: async (call: {
        method: string;
        url: string;
        data?: Record<string, unknown>;
      }) => {
        calls.request.push(call);
        if (overrides?.onRequest) {
          return overrides.onRequest(call);
        }
        return {
          code: 0,
          bot: { open_id: "ou_bot_probed", app_name: "DaltonBot" },
        };
      },
      im: {
        message: {
          create: async (call: MessageCreateCall) => {
            calls.messageCreate.push(call);
            if (overrides?.onMessageCreate) {
              return overrides.onMessageCreate(call);
            }
            return {
              code: 0,
              data: { message_id: `om_created_${seq++}` },
            };
          },
          reply: async (call: MessageReplyCall) => {
            calls.messageReply.push(call);
            if (overrides?.onMessageReply) {
              return overrides.onMessageReply(call);
            }
            return {
              code: 0,
              data: { message_id: `om_replied_${seq++}` },
            };
          },
          get: async (call: {
            params?: { card_msg_content_type?: string };
            path: { message_id: string };
          }) => {
            calls.messageGet.push(call);
            if (overrides?.onMessageGet) {
              return overrides.onMessageGet(call);
            }
            return {
              code: 0,
              data: { items: [] },
            };
          },
        },
      image: {
        create: async (call: ImageCreateCall) => {
          calls.imageCreate.push(call);
          if (overrides?.onImageCreate) {
            return overrides.onImageCreate(call);
          }
          return {
            code: 0,
            data: { image_key: `img_v3_${seq++}` },
          };
        },
      },
      file: {
        create: async (call: FileCreateCall) => {
          calls.fileCreate.push(call);
          if (overrides?.onFileCreate) {
            return overrides.onFileCreate(call);
          }
          return {
            code: 0,
            data: { file_key: `file_v3_${seq++}` },
          };
        },
      },
      messageResource: {
        get: async (call: MessageResourceGetCall) => {
          calls.messageResourceGet.push(call);
          if (overrides?.onMessageResourceGet) {
            return overrides.onMessageResourceGet(call);
          }
          return {
            data: Buffer.from("default-resource-bytes"),
            headers: {
              "content-type":
                call.params.type === "image"
                  ? "image/png"
                  : "application/pdf",
            },
          };
        },
      },
      chat: {
        list: async (call: ChatListCall) => {
          calls.chatList.push(call);
          if (overrides?.onChatList) {
            return overrides.onChatList(call);
          }
          return {
            code: 0,
            data: { items: [], has_more: false },
          };
        },
        get: async (call: ChatGetCall) => {
          calls.chatGet.push(call);
          if (overrides?.onChatGet) {
            return overrides.onChatGet(call);
          }
          return {
            code: 0,
            data: { name: `Group ${call.path.chat_id}`, chat_mode: "group" },
          };
        },
      },
      chatMembers: {
        get: async (call: ChatMembersGetCall) => {
          calls.chatMembersGet.push(call);
          if (overrides?.onChatMembersGet) {
            return overrides.onChatMembersGet(call);
          }
          return {
            code: 0,
            data: {
              has_more: false,
              items: [{ member_id: "ou_member_1", name: "Alice" }],
            },
          };
        },
      },
    },
  } as unknown as Lark.Client;

  return { client, calls };
}

function installStatefulRecoveryBackend(
  h: ReturnType<typeof createTestHarness>,
  messageId: string,
  options?: { failRecoveryQueryOnce?: boolean },
) {
  const defaultRegister = vi
    .mocked(h.backend.registerAndResolveInbound)
    .getMockImplementation()!;
  const defaultProgress = vi
    .mocked(h.backend.updateInboundProgress)
    .getMockImplementation()!;
  const row: {
    status:
      | "pending"
      | "received"
      | "accepted"
      | "dispatched"
      | "streaming"
      | "steered"
      | "pending_resubmit"
      | "returned_unresubmitted"
      | "completed"
      | "failed"
      | "ignored"
      | null;
    thread_id: string | null;
    run_id: string | null;
    turn_index: number | null;
    fallback_pending: boolean;
    uploaded_attachments: Array<{
      filename: string;
      workspace_path: string;
      mime_type: string;
      size_bytes: number;
      data_url?: string;
    }>;
    raw_payload: Record<string, unknown>;
    request_key: string;
  } = {
    status: "pending",
    thread_id: null,
    run_id: null,
    turn_index: null,
    fallback_pending: false,
    uploaded_attachments: [],
    raw_payload: {},
    request_key: "",
  };
  const state = {
    row,
    logicalTurns: [] as Array<{
      request_key: string;
      thread_id: string;
      run_id: string;
    }>,
    registerCalls: 0,
    registered: false,
    snapshotRow: () => ({
      platform: "feishu" as const,
      message_id: messageId,
      platform_user_id: "ou_alice",
      dalton_user_id: USER_ID,
      chat_type: "dm" as const,
      chat_address: "feishu:@me",
      thread_scope_key: "dm:ou_alice",
      request_key: row.request_key,
      retry_request_key: null,
      resubmitted_request_key: null,
      steered_into_run_id: null,
      thread_id: row.thread_id,
      run_id: row.run_id,
      turn_index: row.turn_index,
      workspace_id: "99999999-8888-7777-6666-555555555555",
      external_thread_id: "feishu:dm:ou_alice:gen:1",
      generation: 1,
      status: (row.status ?? "pending") as "pending",
      fallback_pending: row.fallback_pending,
      sent_to_current_count: 0,
      uploaded_attachments: structuredClone(row.uploaded_attachments),
      raw_payload: structuredClone(row.raw_payload),
      pending_interrupt: null,
    }),
  };

  vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
    async (req) => {
      state.registerCalls += 1;
      if (
        options?.failRecoveryQueryOnce &&
        state.registerCalls === 2
      ) {
        throw new Error("temporary recovery query failure");
      }
      const isFirstRegistration = !state.registered;
      state.registered = true;
      row.request_key = req.request_key;
      row.raw_payload = req.raw_payload ?? row.raw_payload;
      const base = await defaultRegister(req);
      return {
        ...base,
        existing_status: isFirstRegistration ? null : row.status,
        thread_id: row.thread_id,
        run_id: row.run_id,
        turn_index: row.turn_index,
        fallback_pending: row.fallback_pending,
        uploaded_attachments: structuredClone(row.uploaded_attachments),
        request_key: req.request_key,
        raw_payload: structuredClone(row.raw_payload),
      };
    },
  );

  vi.mocked(h.backend.updateInboundProgress).mockImplementation(
    async (req) => {
      if (req.message_id === messageId) {
        if (req.status !== undefined) {
          row.status = req.status;
        }
        if (req.thread_id !== undefined) {
          row.thread_id = req.thread_id;
        }
        if (req.run_id !== undefined) {
          row.run_id = req.run_id;
        }
        if (req.turn_index !== undefined) {
          row.turn_index = req.turn_index;
        }
        if (req.fallback_pending !== undefined) {
          row.fallback_pending = req.fallback_pending;
        }
        if (req.uploaded_attachments !== undefined) {
          row.uploaded_attachments = structuredClone(
            req.uploaded_attachments,
          );
        }
      }
      return defaultProgress(req);
    },
  );

  vi.mocked(h.backend.createThreadMessages).mockImplementation(
    async (_userId, payload) => {
      const requestKey = payload.request_key ?? "";
      const existing = state.logicalTurns.find(
        (turn) => turn.request_key === requestKey,
      );
      if (existing) {
        return {
          status: 409,
          isSse: false,
          errorBody: {
            detail: {
              code: "duplicate_request",
              thread_id: existing.thread_id,
              run_id: existing.run_id,
              run_status: "completed",
              retryable: false,
              turn_index: 1,
            },
          },
        };
      }
      state.logicalTurns.push({
        request_key: requestKey,
        thread_id: THREAD_ID,
        run_id: RUN_ID,
      });
      throw new Error("connection reset before metadata");
    },
  );

  vi.mocked(h.backend.recoverState).mockImplementation(async () => ({
    mode: "startup" as const,
    inbound_events: [state.snapshotRow()],
    background_runs: [],
    thread_watches: [],
    automation_runs: [],
  }));

  return state;
}

describe("Feishu conversation ID & target codec", () => {
  it("round-trips oc_, ou_, and on_ targets and conversation scopes", () => {
    expect(detectIdType("oc_chat123")).toBe("chat_id");
    expect(detectIdType("ou_user456")).toBe("open_id");
    expect(detectIdType("on_union789")).toBe("user_id");
    expect(detectIdType("custom_user_id")).toBe("user_id");

    expect(resolveReceiveIdType("feishu:oc_chat123")).toBe("chat_id");
    expect(resolveReceiveIdType("chat:oc_chat123")).toBe("chat_id");
    expect(resolveReceiveIdType("feishu:ou_user456")).toBe("open_id");
    expect(resolveReceiveIdType("user:ou_user456")).toBe("open_id");
    expect(resolveReceiveIdType("user:employee_01")).toBe("user_id");
    expect(resolveReceiveIdType("on_union789")).toBe("user_id");

    expect(normalizeFeishuTarget("feishu:oc_chat123")).toBe("oc_chat123");
    expect(normalizeFeishuTarget("feishu:@me")).toBe("@me");
    expect(normalizeFeishuTarget("lark:chat:oc_abc")).toBe("oc_abc");
    expect(formatFeishuTarget("oc_abc")).toBe("chat:oc_abc");
    expect(formatFeishuTarget("ou_xyz")).toBe("user:ou_xyz");

    expect(looksLikeFeishuId("oc_abc")).toBe(true);
    expect(looksLikeFeishuId("ou_xyz")).toBe(true);
    expect(looksLikeFeishuId("on_union")).toBe(true);

    expect(parseFeishuDirectConversationId("feishu:ou_user456")).toBe(
      "ou_user456",
    );
    expect(parseFeishuDirectConversationId("feishu:on_union789")).toBe(
      "on_union789",
    );
    expect(parseFeishuDirectConversationId("feishu:oc_chat123")).toBeUndefined();
    expect(parseFeishuTargetId("chat:oc_chat123")).toBe("oc_chat123");

    const scopes = [
      {
        chatId: "oc_chat123",
        scope: "group" as const,
      },
      {
        chatId: "oc_chat123",
        scope: "group_sender" as const,
        senderOpenId: "ou_sender1",
      },
      {
        chatId: "oc_chat123",
        scope: "group_topic" as const,
        topicId: "om_root_topic_1",
      },
      {
        chatId: "oc_chat123",
        scope: "group_topic_sender" as const,
        topicId: "om_root_topic_1",
        senderOpenId: "ou_sender1",
      },
    ];

    for (const item of scopes) {
      const encoded = buildFeishuConversationId(item);
      const decoded = parseFeishuConversationId({ conversationId: encoded });
      expect(decoded).not.toBeNull();
      expect(decoded?.chatId).toBe(item.chatId);
      expect(decoded?.scope).toBe(item.scope);
      expect(decoded?.senderOpenId).toBe(item.senderOpenId);
      expect(decoded?.topicId).toBe(item.topicId);
      expect(decoded?.canonicalConversationId).toBe(encoded);
    }
  });

  it("formats threadScopeKey as dm:<open_id>, chat:<chat_id>, and chat:<chat_id>:thread:<thread_id>", () => {
    expect(
      buildDaltonFeishuChatAddress({ chatType: "p2p", chatId: "oc_dm_1" }),
    ).toBe("feishu:@me");
    expect(
      buildDaltonFeishuChatAddress({ chatType: "group", chatId: "oc_grp_1" }),
    ).toBe("feishu:oc_grp_1");
    expect(
      buildDaltonFeishuThreadScopeKey({
        chatType: "p2p",
        chatId: "oc_dm_1",
        senderOpenId: "ou_alice",
      }),
    ).toBe("dm:ou_alice");
    expect(
      buildDaltonFeishuThreadScopeKey({
        chatType: "group",
        chatId: "oc_grp_1",
        senderOpenId: "ou_alice",
      }),
    ).toBe("chat:oc_grp_1");
    expect(
      buildDaltonFeishuThreadScopeKey({
        chatType: "group",
        chatId: "oc_grp_1",
        senderOpenId: "ou_alice",
        threadId: "omt_topic_42",
      }),
    ).toBe("chat:oc_grp_1:thread:omt_topic_42");
    expect(
      resolveDaltonOutboundTarget({
        targetAddress: "feishu:@me",
        recipientPlatformUserId: "ou_alice",
      }),
    ).toBe("ou_alice");
    expect(
      resolveDaltonOutboundTarget({
        targetAddress: "feishu:oc_grp_1",
      }),
    ).toBe("oc_grp_1");
  });
});

describe("Feishu inbound event parsing, deduplication & sequential queue", () => {
  const BOT_OPEN_ID = "ou_bot_dalton";

  it("parses DM and group plain text messages, thread_id scopes, and @bot mentions", () => {
    const dmRaw = {
      sender: {
        sender_id: { open_id: "ou_alice_1" },
        sender_type: "user",
        name: "Alice",
      },
      message: {
        message_id: "om_msg_dm_1",
        chat_id: "oc_dm_chat_1",
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: "Analyze NVDA Q4 earnings" }),
      },
    };

    const dmEvent = parseFeishuInboundEvent(dmRaw, { botOpenId: BOT_OPEN_ID });
    expect(dmEvent).toMatchObject({
      platform: "feishu",
      messageId: "om_msg_dm_1",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "oc_dm_chat_1",
      threadScopeKey: "dm:ou_alice_1",
      platformUserId: "ou_alice_1",
      platformUsername: "Alice",
      text: "Analyze NVDA Q4 earnings",
      isGroupTrigger: undefined,
    });

    const groupRaw = {
      sender: {
        sender_id: { open_id: "ou_bob_2" },
        sender_type: "user",
      },
      message: {
        message_id: "om_msg_grp_1",
        parent_id: "om_parent_0",
        thread_id: "omt_thread_9",
        chat_id: "oc_research_grp",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({
          text: "@_user_1 @_user_2 please check the model",
        }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
          {
            key: "@_user_2",
            id: { open_id: "ou_colleague_3" },
            name: "Carol",
          },
        ],
      },
    };

    const groupEvent = parseFeishuInboundEvent(groupRaw, {
      botOpenId: BOT_OPEN_ID,
    });
    expect(groupEvent).toMatchObject({
      platform: "feishu",
      messageId: "om_msg_grp_1",
      chatType: "group",
      chatAddress: "feishu:oc_research_grp",
      chatId: "oc_research_grp",
      threadScopeKey: "chat:oc_research_grp:thread:omt_thread_9",
      platformUserId: "ou_bob_2",
      text: "@Carol please check the model",
      isGroupTrigger: true,
    });
  });

  it("does not treat @_all broadcast mentions as mentioning the bot", () => {
    expect(
      checkBotMentioned({
        mentions: [{ key: "@_all", id: { open_id: "all" }, name: "All" }],
        botOpenId: BOT_OPEN_ID,
      }),
    ).toBe(false);

    expect(
      normalizeMentions(
        "@_user_1 /bind 654321",
        [{ key: "@_user_1", id: { open_id: BOT_OPEN_ID }, name: "Bot" }],
        BOT_OPEN_ID,
      ),
    ).toBe("/bind 654321");
  });

  it("normalizes complete mention tokens once regardless of mention order", () => {
    const bot = { key: "@_user_1", id: { open_id: BOT_OPEN_ID }, name: "Bot" };
    const colleague = { key: "@_user_10", id: { open_id: "ou_colleague" }, name: "Carol" };
    for (const mentions of [[bot, colleague], [colleague, bot]]) {
      const event = parseFeishuInboundEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_complete_mentions", chat_id: "oc_group", chat_type: "group",
          message_type: "text", content: JSON.stringify({ text: "@_user_1 @_user_10 /stop" }),
          mentions,
        },
      }, { botOpenId: BOT_OPEN_ID });
      expect(event).toMatchObject({ text: "@Carol /stop", commandText: "@Carol /stop", isGroupTrigger: true });
    }
    expect(normalizeMentions("@_user_1 @_user_10 /stop", [bot], BOT_OPEN_ID))
      .toBe("@_user_10 /stop");
    const other = { key: "@_user_1", id: { open_id: "ou_other" }, name: "Alice" };
    for (const mentions of [[other, colleague], [colleague, other]]) {
      expect(normalizeMentions("@_user_1 @_user_10", mentions, BOT_OPEN_ID))
        .toBe("@Alice @Carol");
    }
    const literalName = { ...colleague, name: "@_user_1 $& $1" };
    for (const mentions of [[bot, literalName], [literalName, bot]]) {
      expect(normalizeMentions("@_user_10 @_user_1 /stop", mentions, BOT_OPEN_ID))
        .toBe("@@_user_1 $& $1 /stop");
    }
    expect(normalizeMentions("hello @_user_1 world", [bot], BOT_OPEN_ID)).toBe("hello world");
    expect(normalizeMentions("@_user_10suffix /stop", [colleague], BOT_OPEN_ID))
      .toBe("@_user_10suffix /stop");
  });

  it("parses post rich text, image, and file messages", () => {
    const postRaw = {
      sender: {
        sender_id: { open_id: "ou_alice_1" },
        sender_type: "user",
      },
      message: {
        message_id: "om_post_1",
        chat_id: "oc_grp_1",
        chat_type: "group",
        message_type: "post",
        content: JSON.stringify({
          zh_cn: {
            title: "Q3 Note",
            content: [
              [
                { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
                { tag: "text", text: " Revenue grew " },
                { tag: "text", text: "25%", style: { bold: true } },
              ],
              [{ tag: "img", image_key: "img_post_embedded_1" }],
            ],
          },
        }),
      },
    };

    const postEvent = parseFeishuInboundEvent(postRaw, {
      botOpenId: BOT_OPEN_ID,
    });
    expect(postEvent).not.toBeNull();
    expect(postEvent?.isGroupTrigger).toBe(true);
    expect(postEvent?.text).toContain("Q3 Note");
    expect(postEvent?.text).toContain("**25%**");
    expect(postEvent?.attachments).toEqual([
      {
        id: "img_post_embedded_1",
        filename: "image_1.png",
        mimeType: "image/png",
      },
    ]);

    const imageRaw = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_img_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "image",
        content: JSON.stringify({ image_key: "img_key_abc" }),
      },
    };
    const imageEvent = parseFeishuInboundEvent(imageRaw, {
      botOpenId: BOT_OPEN_ID,
    });
    expect(imageEvent?.attachments).toEqual([
      {
        id: "img_key_abc",
        filename: "img_key_abc.png",
        mimeType: "image/png",
        sizeBytes: undefined,
      },
    ]);

    const fileRaw = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_file_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "file",
        content: JSON.stringify({
          file_key: "file_key_xyz",
          file_name: "model_2026.xlsx",
          file_size: 4096,
        }),
      },
    };
    const fileEvent = parseFeishuInboundEvent(fileRaw, {
      botOpenId: BOT_OPEN_ID,
    });
    expect(fileEvent?.attachments).toEqual([
      {
        id: "file_key_xyz",
        filename: "model_2026.xlsx",
        mimeType: "application/octet-stream",
        sizeBytes: 4096,
      },
    ]);
  });

  it("fetches merge_forward sub-messages via im.message.get with card_msg_content_type=user_card_content even when container is not items[0]", async () => {
    const { client, calls } = createFakeLarkClient({
      onMessageGet: async () => ({
        code: 0,
        data: {
          items: [
            {
              message_id: "om_sub_2",
              upper_message_id: "om_merge_api_1",
              create_time: "2000",
              msg_type: "text",
              body: { content: JSON.stringify({ text: "Second sub-message" }) },
            },
            {
              message_id: "om_merge_api_1",
              msg_type: "merge_forward",
              body: { content: "Merged and Forwarded Message" },
            },
            {
              message_id: "om_sub_1",
              upper_message_id: "om_merge_api_1",
              create_time: "1000",
              msg_type: "text",
              body: { content: JSON.stringify({ text: "First sub-message" }) },
            },
          ],
        },
      }),
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
      client,
    });
    const receivedMerge: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      receivedMerge.push(ev);
    });
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_merge_api_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "merge_forward",
        content: "Merged and Forwarded Message",
      },
    });
    expect(calls.messageGet).toHaveLength(1);
    expect(calls.messageGet[0]).toEqual({
      params: { card_msg_content_type: "user_card_content" },
      path: { message_id: "om_merge_api_1" },
    });
    expect(receivedMerge).toHaveLength(1);
    expect(receivedMerge[0]?.text).toBe(
      "[Merged and Forwarded Messages]\n- First sub-message\n- Second sub-message",
    );
  });

  it("requires a structured bot mention for group slash commands and text while ignoring unmentioned group messages with zero backend or send calls and keeping DM commands mention-free", async () => {
    const botMention = {
      key: "@_user_1",
      id: { open_id: BOT_OPEN_ID },
      name: "DaltonBot",
    };

    const groupBindEvent = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
        message: {
          message_id: "om_grp_bind_1",
          chat_id: "oc_grp_1",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_user_1 /bind 123456" }),
          mentions: [botMention],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(groupBindEvent?.isGroupTrigger).toBe(true);
    expect(groupBindEvent?.text).toBe("/bind 123456");

    for (const cmd of ["/workspace", "/workspace list", "/new", "/stop", "/unbind"]) {
      const mentionedEv = parseFeishuInboundEvent(
        {
          sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
          message: {
            message_id: `om_grp_mentioned_${cmd}`,
            chat_id: "oc_grp_1",
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({ text: `@_user_1 ${cmd}` }),
            mentions: [botMention],
          },
        },
        { botOpenId: BOT_OPEN_ID },
      );
      expect(mentionedEv?.isGroupTrigger).toBe(true);
      expect(mentionedEv?.text).toBe(cmd);
    }

    for (const unmentionedText of [
      "/main",
      "/new",
      "/workspace",
      "/workspace W2",
      "/bind 123456",
      "/unbind",
      "/stop",
      "casual chat without mention",
      "@DaltonBot /stop",
      "@DaltonBot /workspace W2",
      `@ou_bot_dalton /new`,
    ]) {
      const ev = parseFeishuInboundEvent(
        {
          sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
          message: {
            message_id: `om_grp_unmentioned_${unmentionedText}`,
            chat_id: "oc_grp_1",
            chat_type: "group",
            message_type: "text",
            content: JSON.stringify({ text: unmentionedText }),
            mentions: [],
          },
        },
        { botOpenId: BOT_OPEN_ID },
      );
      expect(ev?.isGroupTrigger).toBe(false);
    }

    const broadcastSlashEvent = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
        message: {
          message_id: "om_grp_broadcast_slash",
          chat_id: "oc_grp_1",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_all /workspace W2" }),
          mentions: [{ key: "@_all", id: { open_id: "all" }, name: "All" }],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(broadcastSlashEvent?.isGroupTrigger).toBe(false);

    const h = createTestHarness();
    try {
      const { client, calls } = createFakeLarkClient();
      const realAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client,
      });
      realAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      let idx = 0;
      for (const rawPayload of [
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "/main" }),
          mentions: [],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "/new" }),
          mentions: [],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "/workspace W2" }),
          mentions: [],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "/bind 123456" }),
          mentions: [],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "ordinary group text" }),
          mentions: [],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "@_all /new" }),
          mentions: [{ key: "@_all", id: { open_id: "all" }, name: "所有人" }],
        },
        {
          message_id: `om_grp_ignore_${++idx}`,
          content: JSON.stringify({ text: "@DaltonBot /new" }),
          mentions: [],
        },
      ]) {
        await realAdapter.receiveRawEvent({
          sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
          message: {
            ...rawPayload,
            chat_id: "oc_grp_1",
            chat_type: "group",
            message_type: "text",
          },
        });
      }

      // Direct orchestrator entry also ignores group events when isGroupTrigger is undefined
      await h.app.orchestrator.handleInboundEvent({
        platform: "feishu",
        platformUserId: "ou_alice_1",
        chatType: "group",
        chatAddress: "feishu:oc_grp_1",
        chatId: "oc_grp_1",
        threadScopeKey: "chat:oc_grp_1",
        messageId: "om_grp_undef_trigger",
        text: "/new",
        commandText: "/new",
        isGroupTrigger: undefined,
      });

      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.consumeBindCode).not.toHaveBeenCalled();
      expect(h.backend.sessionCommand).not.toHaveBeenCalled();
      expect(h.backend.registerAndResolveInbound).not.toHaveBeenCalled();
      expect(h.backend.appendCalls).toHaveLength(0);
      expect(h.backend.createCalls).toHaveLength(0);
      expect(h.feishuAdapter.sentTexts).toHaveLength(0);
      expect(calls.messageCreate).toHaveLength(0);
      expect(calls.messageReply).toHaveLength(0);

      // Mentioned group /bind triggers and revokes leaked group bind codes
      vi.mocked(h.backend.consumeBindCode).mockResolvedValueOnce({
        ok: false,
        reason: "revoked_in_group",
      });
      await h.feishuAdapter.emitInbound(groupBindEvent!);
      expect(h.backend.consumeBindCode).toHaveBeenCalledWith(
        expect.objectContaining({
          action: "bind",
          platform: "feishu",
          code: "123456",
          is_group: true,
        }),
      );
      expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("revoked");

      // DM slash command without mention continues to work normally
      await realAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_dm_new_no_mention",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "/new" }),
        },
      });
      expect(h.backend.sessionCommand).toHaveBeenCalledWith(
        expect.objectContaining({
          platform: "feishu",
          dalton_user_id: "user_alice_123",
          scope_key: "dm:ou_alice",
          cmd: "new",
        }),
      );
    } finally {
      await h.cleanup();
    }
  });

  it("builds Feishu dedupe keys matching monitor.account-UbN1RhlP.mjs:858-872 across media, text retries, and post topics", async () => {
    const textWithoutCreateTime = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_dup_100",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: "Hello once" }),
      },
    };
    expect(resolveFeishuMessageDedupeKey(textWithoutCreateTime)).toBe("om_dup_100");

    // Media messages include messageId + mediaParts so distinct messages sending the same image are NOT dropped
    const imgMsg1 = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_img_dup_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "image",
        content: JSON.stringify({ image_key: "img_v3_dup" }),
      },
    };
    const imgMsg2 = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_img_dup_2",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "image",
        content: JSON.stringify({ image_key: "img_v3_dup" }),
      },
    };
    expect(resolveFeishuMessageDedupeKey(imgMsg1)).toBe(
      JSON.stringify(["om_img_dup_1", "image_key:img_v3_dup"]),
    );
    expect(resolveFeishuMessageDedupeKey(imgMsg2)).toBe(
      JSON.stringify(["om_img_dup_2", "image_key:img_v3_dup"]),
    );
    expect(resolveFeishuMessageDedupeKey(imgMsg1)).not.toBe(
      resolveFeishuMessageDedupeKey(imgMsg2),
    );

    // Post message with embedded media uses [messageId, ...mediaParts]
    expect(
      resolveFeishuMessageDedupeKey({
        sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
        message: {
          message_id: "om_post_dup_1",
          thread_id: "omt_99",
          chat_id: "oc_grp_1",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            zh_cn: {
              title: "T",
              content: [[{ tag: "img", image_key: "img_post_99" }]],
            },
          }),
        },
      }),
    ).toBe(JSON.stringify(["om_post_dup_1", "image_key:img_post_99"]));

    // Text retry with same (sender, chat_id, create_time, content) but different message_id produces the SAME dedupe key
    const textRetry1 = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_retry_a",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        create_time: "1700000000",
        message_type: "text",
        content: JSON.stringify({ text: "Retry text" }),
      },
    };
    const textRetry2 = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_retry_b",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        create_time: "1700000000",
        message_type: "text",
        content: JSON.stringify({ text: "Retry text" }),
      },
    };
    const retryKey1 = resolveFeishuMessageDedupeKey(textRetry1);
    const retryKey2 = resolveFeishuMessageDedupeKey(textRetry2);
    expect(retryKey1).toBe(retryKey2);
    expect(JSON.parse(retryKey1!)).toEqual([
      "text-retry",
      "ou_alice_1",
      "oc_dm_1",
      "1700000000",
      expect.stringMatching(/^[0-9a-f]{32}$/),
    ]);

    // Post without media in a topic wraps [retryKey, topicId]
    const postTopicKey = resolveFeishuMessageDedupeKey({
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_post_text_1",
        thread_id: "omt_topic_7",
        chat_id: "oc_grp_1",
        chat_type: "group",
        create_time: "1700000000",
        message_type: "post",
        content: JSON.stringify({
          zh_cn: {
            title: "T",
            content: [[{ tag: "text", text: "Hello topic" }]],
          },
        }),
      },
    });
    const parsedPostTopicKey = JSON.parse(postTopicKey!) as [string, string];
    expect(parsedPostTopicKey[1]).toBe("omt_topic_7");
    expect(JSON.parse(parsedPostTopicKey[0])[0]).toBe("text-retry");

    // Missing message_id returns undefined
    expect(
      resolveFeishuMessageDedupeKey({
        sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
        message: {
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          create_time: "1700000000",
          message_type: "text",
          content: JSON.stringify({ text: "No id" }),
        },
      }),
    ).toBeUndefined();
  });

  it("propagates handler error to SDK (no false ACK), deletes dedupe key on error, and records it only after retry succeeds", async () => {
    const textPayload = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_dup_100",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: "Hello once" }),
      },
    };

    const adapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });

    let attempts = 0;
    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev: InboundChannelEvent) => {
      attempts += 1;
      if (attempts === 1) {
        throw new Error("Simulated transient I-1 failure on first delivery");
      }
      received.push(ev);
    });

    // 1st delivery fails -> error is rethrown to SDK (so WSClient replies 500 instead of 200) and dedupe key is rolled back
    await expect(adapter.receiveRawEvent(textPayload)).rejects.toThrow(
      "Simulated transient I-1 failure on first delivery",
    );
    expect(attempts).toBe(1);
    expect(received).toHaveLength(0);

    // 2nd delivery of the exact same message_id retries and succeeds -> dedupe key recorded
    await adapter.receiveRawEvent(textPayload);
    expect(attempts).toBe(2);
    expect(received).toHaveLength(1);

    // 3rd delivery after success is deduplicated
    await adapter.receiveRawEvent(textPayload);
    expect(attempts).toBe(2);
    expect(received).toHaveLength(1);
  });

  it("returns from adapter inbound callback right after I-1 registration while keeping slow registrations ordered and letting /stop enter during an active model turn", async () => {
    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      const origRegister = h.backend.registerAndResolveInbound;
      const defaultImpl = vi.mocked(origRegister).getMockImplementation()!;
      const registrationOrder: string[] = [];
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
        async (req) => {
          // Simulate a 25ms I-1 database round-trip (longer than the old 10ms timer)
          await new Promise((r) => setTimeout(r, 25));
          registrationOrder.push(req.message_id);
          return defaultImpl(req);
        },
      );

      let finishSseStream!: () => void;
      const sseHold = new Promise<void>((resolve) => {
        finishSseStream = resolve;
      });
      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "metadata",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: RUN_ID,
                turn_index: 1,
              }),
            };
            await sseHold;
            yield {
              event: "run_end",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: RUN_ID,
                turn_index: 1,
                outcome: "cancelled",
              }),
            };
          })(),
        }),
      );

      // 1. Send the long-running turn message; receiveRawEvent returns right after I-1 registration
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_turn_slow_reg_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "Start deep analysis" }),
        },
      });

      expect(registrationOrder).toEqual(["om_turn_slow_reg_1"]);
      expect(h.backend.appendCalls).toHaveLength(1);
      expect(h.backend.cancelThreadTurn).not.toHaveBeenCalled();

      // 2. While the model turn is still streaming, send `/stop` on the same conversation
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_stop_during_turn_2",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "/stop" }),
        },
      });

      expect(registrationOrder).toEqual([
        "om_turn_slow_reg_1",
        "om_stop_during_turn_2",
      ]);
      expect(h.backend.cancelThreadTurn).toHaveBeenCalledWith(
        USER_ID,
        THREAD_ID,
        RUN_ID,
      );

      finishSseStream();
      await new Promise((r) => setTimeout(r, 20));
    } finally {
      await h.cleanup();
    }
  });

  it("completes the detached model turn in the background after the inbound callback has returned", async () => {
    const h = createTestHarness();
    try {
      const { client, calls } = createFakeLarkClient();
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      let releaseTurnEnd!: () => void;
      const turnEndGate = new Promise<void>((resolve) => {
        releaseTurnEnd = resolve;
      });

      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* () {
            for await (const ev of makeSseEventsFromFixture([
              {
                event: "metadata",
                data: {
                  thread_id: THREAD_ID,
                  run_id: RUN_ID,
                  turn_index: 1,
                },
              },
            ])) {
              yield ev;
            }
            await turnEndGate;
            for await (const ev of makeSseEventsFromFixture([
              {
                event: "message_chunk",
                data: {
                  id: "msg_1",
                  role: "assistant",
                  content_type: "text",
                  content: "Background turn finished.",
                },
              },
              {
                event: "run_end",
                data: {
                  thread_id: THREAD_ID,
                  run_id: RUN_ID,
                  turn_index: 1,
                  outcome: "completed",
                },
              },
            ])) {
              yield ev;
            }
          })(),
        }),
      );

      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_bg_complete_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "Compute valuation" }),
        },
      });

      // Callback returned while turn is still in progress
      expect(
        h.backend.progressUpdates.some(
          (u) =>
            u.message_id === "om_bg_complete_1" && u.status === "completed",
        ),
      ).toBe(false);

      // Release SSE stream and verify background turn settles to "completed" and delivers reply
      releaseTurnEnd();
      await new Promise((r) => setTimeout(r, 25));

      expect(
        h.backend.progressUpdates.some(
          (u) =>
            u.message_id === "om_bg_complete_1" &&
            u.status === "completed" &&
            u.fallback_pending === false,
        ),
      ).toBe(true);
      expect(
        h.feishuAdapter.sentTexts.some((t) =>
          t.text.includes("Background turn finished."),
        ) || calls.messageCreate.length > 0,
      ).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("recovers the original turn with the same request key when the backend accepted creation but the response is lost before metadata", async () => {
    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      const state = installStatefulRecoveryBackend(
        h,
        "om_bg_lost_response_1",
      );

      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_bg_lost_response_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "Compute valuation" }),
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      const expectedKey = makeInboundRequestKey(
        "feishu",
        "om_bg_lost_response_1",
      );
      expect(state.logicalTurns).toHaveLength(1);
      expect(state.logicalTurns[0]?.request_key).toBe(expectedKey);
      expect(h.backend.createCalls.length).toBeGreaterThanOrEqual(2);
      expect(
        h.backend.createCalls.every(
          (call) => call.payload.request_key === expectedKey,
        ),
      ).toBe(true);
      expect(state.row.thread_id).toBe(THREAD_ID);
      expect(state.row.status).toBe("completed");
      expect(state.row.fallback_pending).toBe(false);
      expect(
        h.feishuAdapter.sentTexts.some((t) =>
          t.text.includes("Replayed assistant output."),
        ),
      ).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("keeps the inbound row recoverable when the recovery query temporarily fails and a later recovery retrieves the original turn", async () => {
    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      const state = installStatefulRecoveryBackend(
        h,
        "om_bg_recovery_transient_1",
        { failRecoveryQueryOnce: true },
      );

      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_bg_recovery_transient_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "text",
          content: JSON.stringify({ text: "Compute valuation" }),
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      const expectedKey = makeInboundRequestKey(
        "feishu",
        "om_bg_recovery_transient_1",
      );
      expect(state.row.status).toBe("pending");
      expect(state.row.thread_id).toBeNull();
      expect(state.row.run_id).toBeNull();
      expect(state.row.request_key).toBe(expectedKey);
      expect(state.logicalTurns).toHaveLength(1);
      expect(
        h.feishuAdapter.sentTexts.some((t) =>
          t.text.includes("Replayed assistant output."),
        ),
      ).toBe(false);

      await h.app.orchestrator.runStartupRecovery();

      expect(state.logicalTurns).toHaveLength(1);
      expect(
        h.backend.createCalls.every(
          (call) => call.payload.request_key === expectedKey,
        ),
      ).toBe(true);
      expect(state.row.status).toBe("completed");
      expect(
        h.feishuAdapter.sentTexts.some((t) =>
          t.text.includes("Replayed assistant output."),
        ),
      ).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("executes queued tasks sequentially per conversation key in createSequentialQueue", async () => {
    const queue = createSequentialQueue();
    const order: string[] = [];
    const p1 = queue.enqueue("chat_A", async () => {
      await new Promise((r) => setTimeout(r, 25));
      order.push("A1");
    });
    const p2 = queue.enqueue("chat_A", async () => {
      order.push("A2");
    });
    await Promise.all([p1, p2]);
    expect(order).toEqual(["A1", "A2"]);
  });

  it("retries botOpenId probe before group messages when initial startup probe fails", async () => {
    let probeCalls = 0;
    const { client } = createFakeLarkClient({
      onRequest: async () => {
        probeCalls += 1;
        if (probeCalls === 1) {
          throw new Error("Temporary network error during startup probe");
        }
        return {
          code: 0,
          bot: { open_id: BOT_OPEN_ID, app_name: "DaltonBot" },
        };
      },
    });

    const adapter = new FeishuChannelAdapter({
      appId: "cli_retry_app",
      appSecret: "sec_retry_secret",
      client,
      wsClientFactory: () => ({
        start: async () => {},
        close: () => {},
        getConnectionStatus: () => ({ state: "connected" as const }),
      }),
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });

    await adapter.start();
    expect(probeCalls).toBe(1);
    expect(adapter.getBotOpenId()).toBeUndefined();

    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_grp_after_retry",
        chat_id: "oc_grp_retry",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "@_user_1 hello bot" }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
        ],
      },
    });

    expect(probeCalls).toBe(2);
    expect(adapter.getBotOpenId()).toBe(BOT_OPEN_ID);
    expect(received).toHaveLength(1);
    expect(received[0]?.isGroupTrigger).toBe(true);
    expect(received[0]?.text).toBe("hello bot");

    await adapter.stop();
  });

  it("fails structured group mentions and @bot commands without recording dedupe when bot identity lookup temporarily fails on receiveRawEvent, then processes each once after recovery", async () => {
    let probeShouldFail = true;
    const { client } = createFakeLarkClient({
      onRequest: async () => {
        if (probeShouldFail) {
          throw new Error("Temporary bot identity lookup failure");
        }
        return {
          code: 0,
          bot: { open_id: BOT_OPEN_ID, app_name: "DaltonBot" },
        };
      },
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_identity_raw_1",
      appSecret: "sec_identity_raw_1",
      client,
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });
    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    const mentionPayload = {
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_raw_mention",
        chat_id: "oc_grp_identity_raw",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "@_user_1 分析这家公司" }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
        ],
      },
    };
    const commandPayload = {
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_raw_command",
        chat_id: "oc_grp_identity_raw",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "@_user_1 /workspace W2" }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
        ],
      },
    };

    await expect(adapter.receiveRawEvent(mentionPayload)).rejects.toThrow(
      "Feishu bot identity is not resolved",
    );
    await expect(adapter.receiveRawEvent(commandPayload)).rejects.toThrow(
      "Feishu bot identity is not resolved",
    );
    expect(received).toHaveLength(0);

    probeShouldFail = false;
    await adapter.receiveRawEvent(mentionPayload);
    await adapter.receiveRawEvent(commandPayload);
    expect(received).toHaveLength(2);
    expect(received[0]).toMatchObject({
      messageId: "om_identity_raw_mention",
      isGroupTrigger: true,
      text: "分析这家公司",
    });
    expect(received[1]).toMatchObject({
      messageId: "om_identity_raw_command",
      isGroupTrigger: true,
      text: "/workspace W2",
    });

    await adapter.receiveRawEvent(mentionPayload);
    expect(received).toHaveLength(2);
  });

  it("fails a group post at without recording dedupe when bot identity is unresolved, then processes it once after recovery", async () => {
    let probeShouldFail = true;
    const { client } = createFakeLarkClient({
      onRequest: async () => {
        if (probeShouldFail) {
          throw new Error("Temporary bot identity lookup failure");
        }
        return {
          code: 0,
          bot: { open_id: BOT_OPEN_ID, app_name: "DaltonBot" },
        };
      },
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_identity_post_1",
      appSecret: "sec_identity_post_1",
      client,
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });
    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    const postPayload = {
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_post_at",
        chat_id: "oc_grp_identity_post",
        chat_type: "group",
        message_type: "post",
        content: JSON.stringify({
          content: [
            [
              { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
              { tag: "text", text: " 分析这家公司" },
            ],
          ],
        }),
        mentions: [],
      },
    };

    await expect(adapter.receiveRawEvent(postPayload)).rejects.toThrow(
      "Feishu bot identity is not resolved",
    );
    expect(received).toHaveLength(0);

    probeShouldFail = false;
    await adapter.receiveRawEvent(postPayload);
    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({
      messageId: "om_identity_post_at",
      isGroupTrigger: true,
    });

    await adapter.receiveRawEvent(postPayload);
    expect(received).toHaveLength(1);

    const openIdAtEvent = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
        message: {
          message_id: "om_identity_post_at_open_id",
          chat_id: "oc_grp_identity_post",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            content: [
              [
                { tag: "at", open_id: BOT_OPEN_ID, user_name: "DaltonBot" },
                { tag: "text", text: " 分析这家公司" },
              ],
            ],
          }),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(openIdAtEvent?.isGroupTrigger).toBe(true);
  });

  it("fails structured group mentions without recording dedupe on the WebSocket path when bot identity lookup temporarily fails, then processes the same message once after recovery", async () => {
    let probeShouldFail = true;
    const { client } = createFakeLarkClient({
      onRequest: async () => {
        if (probeShouldFail) {
          throw new Error("Temporary bot identity lookup failure");
        }
        return {
          code: 0,
          bot: { open_id: BOT_OPEN_ID, app_name: "DaltonBot" },
        };
      },
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_identity_ws_1",
      appSecret: "sec_identity_ws_1",
      client,
      wsClientFactory: () => ({
        start: async () => {},
        close: () => {},
        getConnectionStatus: () => ({ state: "connected" as const }),
      }),
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });
    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    const mentionPayload = {
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_ws_mention",
        chat_id: "oc_grp_identity_ws",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "@_user_1 分析这家公司" }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
        ],
      },
    };

    try {
      await adapter.start();
      expect(adapter.getBotOpenId()).toBeUndefined();

      await expect(adapter.receiveRawEvent(mentionPayload)).rejects.toThrow(
        "Feishu bot identity is not resolved",
      );
      expect(received).toHaveLength(0);

      probeShouldFail = false;
      await adapter.receiveRawEvent(mentionPayload);
      expect(received).toHaveLength(1);
      expect(received[0]).toMatchObject({
        messageId: "om_identity_ws_mention",
        isGroupTrigger: true,
        text: "分析这家公司",
      });

      await adapter.receiveRawEvent(mentionPayload);
      expect(received).toHaveLength(1);
    } finally {
      await adapter.stop();
    }
  });

  it("does not block broadcast mentions, direct messages or mention-free group commands when bot identity is unresolved", async () => {
    const { client } = createFakeLarkClient({
      onRequest: async () => {
        throw new Error("Temporary bot identity lookup failure");
      },
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_identity_open_1",
      appSecret: "sec_identity_open_1",
      client,
      logger: createSubsystemLogger("test", {
        sink: { info: () => {}, warn: () => {}, error: () => {} },
      }),
    });
    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      received.push(ev);
    });

    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_broadcast",
        chat_id: "oc_grp_identity_open",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "@_all 大家好" }),
        mentions: [
          { key: "@_all", id: { open_id: "all" }, name: "所有人" },
        ],
      },
    });
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_dm",
        chat_id: "oc_dm_identity_open",
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: "@_user_1 你好" }),
        mentions: [
          {
            key: "@_user_1",
            id: { open_id: BOT_OPEN_ID },
            name: "DaltonBot",
          },
        ],
      },
    });
    await adapter.receiveRawEvent({
      sender: { sender_id: { open_id: "ou_bob_2" }, sender_type: "user" },
      message: {
        message_id: "om_identity_plain_command",
        chat_id: "oc_grp_identity_open",
        chat_type: "group",
        message_type: "text",
        content: JSON.stringify({ text: "/stop" }),
        mentions: [],
      },
    });

    expect(received).toHaveLength(3);
    expect(received[0]).toMatchObject({
      messageId: "om_identity_broadcast",
      chatType: "group",
      isGroupTrigger: false,
    });
    expect(received[1]).toMatchObject({
      messageId: "om_identity_dm",
      chatType: "dm",
    });
    expect(received[2]).toMatchObject({
      messageId: "om_identity_plain_command",
      chatType: "group",
      isGroupTrigger: false,
      text: "/stop",
    });

    const h = createTestHarness();
    try {
      await h.app.orchestrator.handleInboundEvent(received[0]!);
      await h.app.orchestrator.handleInboundEvent(received[2]!);
      expect(h.backend.registerAndResolveInbound).not.toHaveBeenCalled();
      expect(h.backend.sessionCommand).not.toHaveBeenCalled();
      expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    } finally {
      await h.cleanup();
    }
  });

  it("keeps same-conversation arrival order when bot identity lookup is delayed so the message after /workspace uses the new workspace", async () => {
    const h = createTestHarness();
    try {
      const NEW_WS = "99999999-1111-2222-3333-444444444444";
      const order: string[] = [];
      let switched = false;
      const defaultRegister = vi
        .mocked(h.backend.registerAndResolveInbound)
        .getMockImplementation()!;
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
        async (req) => {
          order.push(`register:${req.message_id}`);
          const base = await defaultRegister(req);
          return switched && req.message_id === "om_order_msg2"
            ? { ...base, workspace_id: NEW_WS }
            : base;
        },
      );
      const defaultSession = vi
        .mocked(h.backend.sessionCommand)
        .getMockImplementation()!;
      vi.mocked(h.backend.sessionCommand).mockImplementation(async (req) => {
        const res = await defaultSession(req);
        if (req.cmd === "workspace_switch") {
          switched = true;
          order.push("workspace_switch");
        }
        return res;
      });

      const probeResolvers: Array<(value: unknown) => void> = [];
      const { client } = createFakeLarkClient({
        onRequest: async () =>
          new Promise<unknown>((resolve) => {
            probeResolvers.push(resolve);
          }),
      });
      const adapter = new FeishuChannelAdapter({
        appId: "cli_order_app",
        appSecret: "sec_order_secret",
        client,
        logger: createSubsystemLogger("test", {
          sink: { info: () => {}, warn: () => {}, error: () => {} },
        }),
      });
      adapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      const first = adapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_order_ws1",
          chat_id: "oc_grp_order",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_user_1 /workspace W2" }),
          mentions: [
            {
              key: "@_user_1",
              id: { open_id: BOT_OPEN_ID },
              name: "DaltonBot",
            },
          ],
        },
      });
      const second = adapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_order_msg2",
          chat_id: "oc_grp_order",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_user_1 分析这家公司" }),
          mentions: [
            {
              key: "@_user_1",
              id: { open_id: BOT_OPEN_ID },
              name: "DaltonBot",
            },
          ],
        },
      });
      await new Promise((r) => setTimeout(r, 0));
      for (const resolveProbe of [...probeResolvers].reverse()) {
        resolveProbe({
          code: 0,
          bot: { open_id: BOT_OPEN_ID, app_name: "DaltonBot" },
        });
      }
      await Promise.all([first, second]);
      await new Promise((r) => setTimeout(r, 50));

      expect(order.indexOf("workspace_switch")).toBeGreaterThanOrEqual(0);
      expect(order.indexOf("workspace_switch")).toBeLessThan(
        order.indexOf("register:om_order_msg2"),
      );
      expect(
        h.backend.appendCalls.some((c) => c.payload.workspace_id === NEW_WS),
      ).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("does not start a model turn for group text that only contains the bot id as plain text", async () => {
    expect(
      checkBotMentioned({
        mentions: [],
        postMentionedOpenIds: [],
        rawContent: JSON.stringify({
          text: "日志中的 @ou_bot 是一个字符串",
        }),
        botOpenId: "ou_bot",
      }),
    ).toBe(false);
    expect(
      checkBotMentioned({
        mentions: [],
        postMentionedOpenIds: [],
        rawContent: JSON.stringify({ text: "日志中的 @ou_bot_other" }),
        botOpenId: "ou_bot",
      }),
    ).toBe(false);

    const parsed = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_plain_bot_id_1",
          chat_id: "oc_grp_plain",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({
            text: "日志中的 @ou_bot_dalton 是一个字符串",
          }),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(parsed?.isGroupTrigger).toBe(false);

    const h = createTestHarness();
    try {
      const adapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      adapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      await adapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_plain_bot_id_1",
          chat_id: "oc_grp_plain",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({
            text: "日志中的 @ou_bot_dalton 是一个字符串",
          }),
          mentions: [],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.appendCalls).toHaveLength(0);
      expect(h.backend.createCalls).toHaveLength(0);
      expect(
        vi
          .mocked(h.backend.registerAndResolveInbound)
          .mock.calls.some(([req]) => req.message_id === "om_plain_bot_id_1"),
      ).toBe(false);
    } finally {
      await h.cleanup();
    }
  });

  it("preserves interactive card title, row arrays, columns and fields in direct and merged-forward messages", () => {
    const rowCard = JSON.stringify({
      title: "业绩",
      elements: [[{ tag: "text", text: "收入增长25%" }]],
    });
    const rowText = parseInteractiveCardContent(rowCard);
    expect(rowText).toContain("业绩");
    expect(rowText).toContain("收入增长25%");

    const direct = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_card_direct_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "interactive",
          content: rowCard,
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(direct?.text).toContain("业绩");
    expect(direct?.text).toContain("收入增长25%");

    const columnCard = JSON.stringify({
      header: {
        title: { tag: "plain_text", content: "分栏业绩" },
      },
      body: {
        elements: [
          {
            tag: "column_set",
            columns: [
              {
                tag: "column",
                elements: [
                  {
                    tag: "div",
                    text: { tag: "plain_text", content: "左栏收入" },
                  },
                ],
              },
              {
                tag: "column",
                elements: [
                  { tag: "markdown", content: "右栏利润" },
                ],
              },
            ],
          },
          {
            tag: "div",
            fields: [
              {
                is_short: true,
                text: { tag: "lark_md", content: "**收入** 100" },
              },
              {
                is_short: true,
                text: { tag: "lark_md", content: "**利润** 20" },
              },
            ],
          },
        ],
      },
    });
    const columnText = parseInteractiveCardContent(columnCard);
    expect(columnText).toContain("分栏业绩");
    expect(columnText).toContain("左栏收入");
    expect(columnText).toContain("右栏利润");
    expect(columnText).toContain("**收入** 100");
    expect(columnText).toContain("**利润** 20");

    const merged = parseMergeForwardContent({
      items: [
        {
          msg_type: "interactive",
          body: { content: rowCard },
        },
      ],
      formatSubMessage: (subType, subBody) =>
        parseMessageContent(subBody, subType),
    });
    expect(merged).toContain("业绩");
    expect(merged).toContain("收入增长25%");
  });

  it("extracts top-level post files with deduplication and folder filtering and uploads report.pdf through the workspace path", async () => {
    const postContent = JSON.stringify({
      content: [[{ tag: "text", text: "请分析附件" }]],
      files: [
        { file_key: "file_abc", file_name: "report.pdf", is_folder: false },
        { file_key: "file_abc", file_name: "report.pdf", is_folder: false },
        { file_key: "file_dir", file_name: "folder", is_folder: true },
      ],
    });
    const parsedPost = parsePostContent(postContent);
    expect(parsedPost.textContent).toContain("请分析附件");
    expect(parsedPost.files).toEqual([
      { fileKey: "file_abc", fileName: "report.pdf" },
    ]);
    expect(extractInboundAttachments(postContent, "post")).toEqual([
      { id: "file_abc", filename: "report.pdf", mimeType: "application/pdf" },
    ]);

    const rawWithFiles = {
      sender: {
        sender_id: { open_id: "ou_alice" },
        sender_type: "user",
        tenant_key: "t1",
      },
      message: {
        message_id: "om_post_file_key_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "post",
        content: postContent,
        create_time: "1700000000000",
      },
    };
    const rawWithoutFiles = {
      ...rawWithFiles,
      message: {
        ...rawWithFiles.message,
        content: JSON.stringify({
          content: [[{ tag: "text", text: "请分析附件" }]],
        }),
      },
    };
    expect(resolveFeishuMessageDedupeKey(rawWithFiles)).not.toContain(
      "file_abc",
    );
    expect(resolveFeishuMessageDedupeKey(rawWithoutFiles)).not.toContain(
      "file_abc",
    );

    const h = createTestHarness();
    try {
      const { client, calls } = createFakeLarkClient({
        onMessageResourceGet: async () => ({
          data: Buffer.from("PDF_REPORT_CONTENT", "utf8"),
          headers: { "content-type": "application/pdf" },
        }),
      });
      const adapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client,
      });
      adapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      h.feishuAdapter.setDownloadAttachmentDelegate(
        (descriptor, event) => adapter.downloadAttachment(descriptor, event),
      );
      await adapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_file_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: postContent,
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      expect(calls.messageResourceGet).toHaveLength(1);
      expect(calls.messageResourceGet[0]).toEqual({
        path: { message_id: "om_post_file_1", file_key: "file_abc" },
        params: { type: "file" },
      });
      const uploadCalls = vi.mocked(h.backend.uploadWorkspaceFile).mock.calls;
      expect(uploadCalls).toHaveLength(1);
      expect(uploadCalls[0]?.[0]).toBe(USER_ID);
      expect(uploadCalls[0]?.[2]).toBe(
        "uploads/feishu/om_post_file_1/report.pdf",
      );
      expect(uploadCalls[0]?.[3]).toBe("report.pdf");
      expect((uploadCalls[0]?.[4] as Buffer).toString("utf8")).toBe(
        "PDF_REPORT_CONTENT",
      );
      expect(uploadCalls[0]?.[5]).toBe("application/pdf");
      const appendPayload = h.backend.appendCalls[0]?.payload;
      const firstContent = appendPayload?.messages[0]?.content;
      const contentText = Array.isArray(firstContent)
        ? firstContent
            .map((item) =>
              item.type === "text" ? (item.text ?? "") : "",
            )
            .join("\n")
        : String(firstContent ?? "");
      expect(contentText).toContain(
        "uploads/feishu/om_post_file_1/report.pdf",
      );
    } finally {
      await h.cleanup();
    }
  });

  it("resolves wrapped post payloads for post, card fallback and merge-forward parsing and keeps the original message as the file source", async () => {
    const wrapped = JSON.stringify({
      post: {
        zh_cn: {
          title: "业绩",
          content: [[{ tag: "text", text: "收入增长25%" }]],
          files: [
            { file_key: "file_abc", file_name: "report.pdf", is_folder: false },
          ],
        },
      },
    });
    const parsedWrapped = parsePostContent(wrapped);
    expect(parsedWrapped.textContent).toContain("业绩");
    expect(parsedWrapped.textContent).toContain("收入增长25%");
    expect(parsedWrapped.files).toEqual([
      { fileKey: "file_abc", fileName: "report.pdf" },
    ]);

    const otherLocale = JSON.stringify({
      post: {
        fr_fr: {
          title: "Résultats",
          content: [[{ tag: "text", text: "收入增长25%" }]],
        },
      },
    });
    expect(parsePostContent(otherLocale).textContent).toContain("Résultats");
    expect(parsePostContent(otherLocale).textContent).toContain(
      "收入增长25%",
    );
    const outerLocale = JSON.stringify({
      de_de: {
        title: "Ergebnisse",
        content: [[{ tag: "text", text: "收入增长25%" }]],
      },
    });
    expect(parsePostContent(outerLocale).textContent).toContain(
      "Ergebnisse",
    );

    expect(parseInteractiveCardContent(wrapped)).toContain("业绩");
    expect(parseInteractiveCardContent(wrapped)).toContain("收入增长25%");

    const merged = parseMergeForwardContent({
      items: [{ msg_type: "post", body: { content: wrapped } }],
      formatSubMessage: (subType, subBody) =>
        parseMessageContent(subBody, subType),
    });
    expect(merged).toContain("业绩");
    expect(merged).toContain("收入增长25%");

    const event = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_wrapped_post_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: wrapped,
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(event?.text).toContain("收入增长25%");
    expect(event?.attachments).toEqual([
      { id: "file_abc", filename: "report.pdf", mimeType: "application/pdf" },
    ]);

    const { client, calls } = createFakeLarkClient({
      onMessageResourceGet: async () => ({
        data: Buffer.from("PDF_REPORT_CONTENT", "utf8"),
        headers: { "content-type": "application/pdf" },
      }),
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
      client,
    });
    const downloaded = await adapter.downloadAttachment(
      event!.attachments![0]!,
      event!,
    );
    expect(calls.messageResourceGet).toEqual([
      {
        path: { message_id: "om_wrapped_post_1", file_key: "file_abc" },
        params: { type: "file" },
      },
    ]);
    expect(downloaded.content.toString("utf8")).toBe("PDF_REPORT_CONTENT");
    expect(downloaded.filename).toBe("report.pdf");
    expect(downloaded.mimeType).toBe("application/pdf");
  });

  it("recovers an interrupted attachment upload at startup without submitting text before the uploaded record is saved", async () => {
    const MESSAGE_ID = "om_post_recovery_1";
    const postContent = JSON.stringify({
      content: [[{ tag: "text", text: "请分析附件" }]],
      files: [
        { file_key: "file_abc", file_name: "report.pdf", is_folder: false },
      ],
    });
    const saved: {
      status: "pending" | "completed" | "failed";
      thread_id: string | null;
      run_id: string | null;
      turn_index: number | null;
      fallback_pending: boolean;
      uploaded_attachments: Array<{
        filename: string;
        workspace_path: string;
        mime_type: string;
        size_bytes: number;
        data_url?: string;
      }>;
      raw_payload: Record<string, unknown>;
      request_key: string;
      registered: boolean;
    } = {
      status: "pending",
      thread_id: null,
      run_id: null,
      turn_index: null,
      fallback_pending: false,
      uploaded_attachments: [],
      raw_payload: {},
      request_key: "",
      registered: false,
    };
    let downloadShouldFail = true;
    let uploadedSaveShouldFail = false;

    const { client, calls } = createFakeLarkClient({
      onMessageResourceGet: async () => {
        if (downloadShouldFail) {
          throw new Error("temporary download failure");
        }
        return {
          data: Buffer.from("PDF_REPORT_CONTENT", "utf8"),
          headers: { "content-type": "application/pdf" },
        };
      },
    });
    const realAdapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
      client,
    });

    const wireStatefulBackend = (
      h: ReturnType<typeof createTestHarness>,
    ): void => {
      const defaultRegister = vi
        .mocked(h.backend.registerAndResolveInbound)
        .getMockImplementation()!;
      const defaultProgress = vi
        .mocked(h.backend.updateInboundProgress)
        .getMockImplementation()!;
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
        async (req) => {
          const isFirstRegistration = !saved.registered;
          saved.registered = true;
          saved.request_key = req.request_key;
          if (req.raw_payload) {
            saved.raw_payload = req.raw_payload;
          }
          const base = await defaultRegister(req);
          return {
            ...base,
            existing_status: isFirstRegistration ? null : saved.status,
            thread_id: saved.thread_id,
            run_id: saved.run_id,
            turn_index: saved.turn_index,
            fallback_pending: saved.fallback_pending,
            uploaded_attachments: structuredClone(
              saved.uploaded_attachments,
            ),
            request_key: req.request_key,
            raw_payload: structuredClone(saved.raw_payload),
          };
        },
      );
      vi.mocked(h.backend.updateInboundProgress).mockImplementation(
        async (req) => {
          if (req.message_id === MESSAGE_ID) {
            if (
              req.uploaded_attachments !== undefined &&
              uploadedSaveShouldFail
            ) {
              throw new Error("temporary uploaded record save failure");
            }
            if (req.status !== undefined) {
              saved.status = req.status as typeof saved.status;
            }
            if (req.thread_id !== undefined) {
              saved.thread_id = req.thread_id;
            }
            if (req.run_id !== undefined) {
              saved.run_id = req.run_id;
            }
            if (req.turn_index !== undefined) {
              saved.turn_index = req.turn_index;
            }
            if (req.fallback_pending !== undefined) {
              saved.fallback_pending = req.fallback_pending;
            }
            if (req.uploaded_attachments !== undefined) {
              saved.uploaded_attachments = structuredClone(
                req.uploaded_attachments,
              );
            }
          }
          return defaultProgress(req);
        },
      );
      vi.mocked(h.backend.recoverState).mockImplementation(async () => ({
        mode: "startup" as const,
        inbound_events: [
          {
            platform: "feishu" as const,
            message_id: MESSAGE_ID,
            platform_user_id: "ou_alice",
            dalton_user_id: USER_ID,
            chat_type: "dm" as const,
            chat_address: "feishu:@me",
            thread_scope_key: "dm:ou_alice",
            request_key: saved.request_key,
            retry_request_key: null,
            resubmitted_request_key: null,
            steered_into_run_id: null,
            thread_id: saved.thread_id,
            run_id: saved.run_id,
            turn_index: saved.turn_index,
            workspace_id: "99999999-8888-7777-6666-555555555555",
            external_thread_id: "feishu:dm:ou_alice:gen:1",
            generation: 1,
            status: saved.status,
            fallback_pending: saved.fallback_pending,
            sent_to_current_count: 0,
            uploaded_attachments: structuredClone(
              saved.uploaded_attachments,
            ),
            raw_payload: structuredClone(saved.raw_payload),
            pending_interrupt: null,
          },
        ],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      }));
      h.feishuAdapter.setDownloadAttachmentDelegate((descriptor, event) =>
        realAdapter.downloadAttachment(descriptor, event),
      );
    };

    const h1 = createTestHarness();
    const extraHarnesses: Array<ReturnType<typeof createTestHarness>> = [];
    try {
      wireStatefulBackend(h1);
      realAdapter.onInboundEvent(async (ev) => {
        await h1.app.orchestrator.handleInboundEvent(ev);
      });
      await realAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: MESSAGE_ID,
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: postContent,
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      expect(saved.raw_payload.attachments).toEqual([
        {
          id: "file_abc",
          filename: "report.pdf",
          mimeType: "application/pdf",
        },
      ]);
      expect(saved.status).toBe("pending");
      expect(saved.uploaded_attachments).toEqual([]);
      expect(h1.backend.appendCalls).toHaveLength(0);
      expect(h1.backend.createCalls).toHaveLength(0);
      expect(calls.messageResourceGet.length).toBeGreaterThanOrEqual(1);
      expect(calls.messageResourceGet[0]).toEqual({
        path: { message_id: MESSAGE_ID, file_key: "file_abc" },
        params: { type: "file" },
      });

      downloadShouldFail = false;
      uploadedSaveShouldFail = true;
      const h2 = createTestHarness();
      extraHarnesses.push(h2);
      wireStatefulBackend(h2);
      await h2.app.orchestrator.runStartupRecovery();

      expect(h2.backend.uploadWorkspaceFile).toHaveBeenCalledTimes(1);
      expect(saved.uploaded_attachments).toEqual([]);
      expect(h2.backend.appendCalls).toHaveLength(0);
      expect(h2.backend.createCalls).toHaveLength(0);

      uploadedSaveShouldFail = false;
      const h3 = createTestHarness();
      extraHarnesses.push(h3);
      wireStatefulBackend(h3);
      await h3.app.orchestrator.runStartupRecovery();

      const uploadCalls = vi.mocked(h3.backend.uploadWorkspaceFile).mock
        .calls;
      expect(uploadCalls).toHaveLength(1);
      expect(uploadCalls[0]?.[0]).toBe(USER_ID);
      expect(uploadCalls[0]?.[2]).toBe(
        "uploads/feishu/om_post_recovery_1/report.pdf",
      );
      expect(uploadCalls[0]?.[3]).toBe("report.pdf");
      expect((uploadCalls[0]?.[4] as Buffer).toString("utf8")).toBe(
        "PDF_REPORT_CONTENT",
      );
      expect(uploadCalls[0]?.[5]).toBe("application/pdf");
      expect(saved.uploaded_attachments).toEqual([
        {
          filename: "report.pdf",
          workspace_path: "uploads/feishu/om_post_recovery_1/report.pdf",
          mime_type: "application/pdf",
          size_bytes: Buffer.byteLength("PDF_REPORT_CONTENT", "utf8"),
        },
      ]);
      const submitted = (
        h3.backend.appendCalls[0] ?? h3.backend.createCalls[0]
      )?.payload.messages[0]?.content;
      const submittedText = Array.isArray(submitted)
        ? submitted
            .map((item) => (item.type === "text" ? item.text : ""))
            .join("\n")
        : String(submitted ?? "");
      expect(submittedText).toContain("请分析附件");
      expect(submittedText).toContain(
        "uploads/feishu/om_post_recovery_1/report.pdf",
      );
      expect(
        calls.messageResourceGet.every(
          (call) =>
            call.path.message_id === MESSAGE_ID &&
            call.path.file_key === "file_abc" &&
            call.params.type === "file",
        ),
      ).toBe(true);
    } finally {
      await h1.cleanup();
      for (const extra of extraHarnesses) {
        await extra.cleanup();
      }
    }
  });

  it("strips only the bot post at by identity so /workspace switches and /stop cancels through the real inbound path", async () => {
    const postWithBotAt = (text: string) =>
      JSON.stringify({
        content: [
          [
            { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
            { tag: "text", text: ` ${text}` },
          ],
        ],
      });
    const direct = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_cmd_direct_parse",
          chat_id: "oc_grp_postcmd_parse",
          chat_type: "group",
          message_type: "post",
          content: postWithBotAt("/workspace W2"),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(direct?.text).toBe("/workspace W2");
    expect(direct?.isGroupTrigger).toBe(true);

    const wrapped = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_cmd_wrapped_parse",
          chat_id: "oc_grp_postcmd_parse",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            post: {
              zh_cn: {
                title: "",
                content: [
                  [
                    {
                      tag: "at",
                      open_id: BOT_OPEN_ID,
                      user_name: "DaltonBot",
                    },
                    { tag: "text", text: " /workspace W2" },
                  ],
                ],
              },
            },
          }),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(wrapped?.text).toBe("/workspace W2");

    const otherLocale = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_cmd_locale_parse",
          chat_id: "oc_grp_postcmd_parse",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            fr_fr: {
              content: [
                [
                  { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
                  { tag: "text", text: " /workspace W2" },
                ],
              ],
            },
          }),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(otherLocale?.text).toBe("/workspace W2");

    const preserved = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_cmd_preserve_parse",
          chat_id: "oc_grp_postcmd_parse",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            content: [
              [
                { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
                { tag: "text", text: " /workspace W2" },
              ],
              [
                {
                  tag: "at",
                  user_id: "ou_other_same_name",
                  user_name: "DaltonBot",
                },
                { tag: "text", text: " @DaltonBot" },
              ],
            ],
          }),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(preserved?.text).toBe("/workspace W2\n@DaltonBot @DaltonBot");

    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_workspace_1",
          chat_id: "oc_grp_postcmd",
          chat_type: "group",
          message_type: "post",
          content: postWithBotAt("/workspace W2"),
          mentions: [],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(
        vi
          .mocked(h.backend.sessionCommand)
          .mock.calls.some(([req]) => req.cmd === "workspace_switch"),
      ).toBe(true);
      expect(h.backend.appendCalls).toHaveLength(0);
      expect(h.backend.createCalls).toHaveLength(0);

      let finishSseStream!: () => void;
      const sseHold = new Promise<void>((resolve) => {
        finishSseStream = resolve;
      });
      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "metadata",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: RUN_ID,
                turn_index: 1,
              }),
            };
            await sseHold;
            yield {
              event: "run_end",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: RUN_ID,
                turn_index: 1,
                outcome: "cancelled",
              }),
            };
          })(),
        }),
      );
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_turn_1",
          chat_id: "oc_grp_postcmd",
          chat_type: "group",
          message_type: "post",
          content: postWithBotAt("Start deep analysis"),
          mentions: [],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.appendCalls).toHaveLength(1);

      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_stop_1",
          chat_id: "oc_grp_postcmd",
          chat_type: "group",
          message_type: "post",
          content: postWithBotAt("/stop"),
          mentions: [],
        },
      });
      expect(h.backend.cancelThreadTurn).toHaveBeenCalledWith(
        USER_ID,
        THREAD_ID,
        RUN_ID,
      );
      finishSseStream();
      await new Promise((r) => setTimeout(r, 20));
    } finally {
      await h.cleanup();
    }
  });

  it("preserves same-name other user mentions and plain-text @DaltonBot across text, post, wrapped post, and interactive card with empty or non-empty mentions so mentioning another DaltonBot before /stop does not cancel", async () => {
    const botMention = {
      key: "@_user_1",
      id: { open_id: BOT_OPEN_ID },
      name: "DaltonBot",
    };
    const otherSameNameMention = {
      key: "@_user_2",
      id: { open_id: "ou_other" },
      name: "DaltonBot",
    };

    expect(
      normalizeMentions(
        "@_user_1 @_user_2 /stop",
        [botMention, otherSameNameMention],
        BOT_OPEN_ID,
      ),
    ).toBe("@DaltonBot /stop");
    expect(
      normalizeMentions(
        "@_user_1 @_user_2 /stop",
        [otherSameNameMention, botMention],
        BOT_OPEN_ID,
      ),
    ).toBe("@DaltonBot /stop");
    expect(
      normalizeMentions(
        "@_user_1 @DaltonBot /stop",
        [botMention],
        BOT_OPEN_ID,
      ),
    ).toBe("@DaltonBot /stop");

    const postSameNameContent = JSON.stringify({
      content: [
        [
          { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
          { tag: "at", user_id: "ou_other", user_name: "DaltonBot" },
          { tag: "text", text: " /stop" },
        ],
      ],
    });
    const wrappedPostSameNameContent = JSON.stringify({
      post: {
        zh_cn: {
          title: "",
          content: [
            [
              { tag: "at", open_id: BOT_OPEN_ID, user_name: "DaltonBot" },
              { tag: "at", open_id: "ou_other", user_name: "DaltonBot" },
              { tag: "text", text: " /stop" },
            ],
          ],
        },
      },
    });
    const interactiveCardSameNameContent = JSON.stringify({
      elements: [
        [
          { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
          { tag: "at", user_id: "ou_other", user_name: "DaltonBot" },
          { tag: "text", text: " /stop" },
        ],
      ],
    });
    const postPlainSameNameContent = JSON.stringify({
      content: [
        [
          { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
          { tag: "text", text: " @DaltonBot /stop" },
        ],
      ],
    });

    for (const mentions of [
      [] as FeishuMentionItem[],
      [botMention],
      [botMention, otherSameNameMention],
      [otherSameNameMention, botMention],
    ]) {
      for (const [label, messageType, content] of [
        ["post", "post", postSameNameContent],
        ["wrapped_post", "post", wrappedPostSameNameContent],
        ["card_elements", "interactive", interactiveCardSameNameContent],
        ["card_fallback", "interactive", wrappedPostSameNameContent],
        ["post_plain_name", "post", postPlainSameNameContent],
      ] as const) {
        const parsed = parseFeishuInboundEvent(
          {
            sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
            message: {
              message_id: `om_same_name_${label}_${mentions.length}`,
              chat_id: "oc_grp_same_name",
              chat_type: "group",
              message_type: messageType,
              content,
              mentions,
            },
          },
          { botOpenId: BOT_OPEN_ID },
        );
        expect(parsed?.isGroupTrigger).toBe(true);
        expect(parsed?.text).toBe("@DaltonBot /stop");
        expect(parsed?.commandText).toBe("@DaltonBot /stop");
      }
    }

    const textSameNameEvent = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_text_same_name_1",
          chat_id: "oc_grp_same_name",
          chat_type: "group",
          message_type: "text",
          content: JSON.stringify({ text: "@_user_1 @_user_2 /stop" }),
          mentions: [botMention, otherSameNameMention],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(textSameNameEvent?.isGroupTrigger).toBe(true);
    expect(textSameNameEvent?.text).toBe("@DaltonBot /stop");
    expect(textSameNameEvent?.commandText).toBe("@DaltonBot /stop");

    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      // Mentioning bot + same-name other user followed by " /stop" with non-empty mentions must NOT execute /stop
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_entry_same_name_nonempty",
          chat_id: "oc_grp_same_name_entry",
          chat_type: "group",
          message_type: "post",
          content: postSameNameContent,
          mentions: [botMention, otherSameNameMention],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.cancelThreadTurn).not.toHaveBeenCalled();
      expect(
        vi
          .mocked(h.backend.sessionCommand)
          .mock.calls.some(([req]) => req.cmd === "stop"),
      ).toBe(false);
      expect(h.backend.appendCalls).toHaveLength(1);
      expect(
        JSON.stringify(h.backend.appendCalls[0]?.payload.messages[0]?.content),
      ).toContain("@DaltonBot /stop");

      // Also verify with empty mentions
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_entry_same_name_empty",
          chat_id: "oc_grp_same_name_entry",
          chat_type: "group",
          message_type: "post",
          content: postSameNameContent,
          mentions: [],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.cancelThreadTurn).not.toHaveBeenCalled();
      expect(h.backend.appendCalls).toHaveLength(2);
    } finally {
      await h.cleanup();
    }
  });

  it("extracts unescaped commandText for rich-text post slash commands while keeping Markdown rendering for normal conversation and preserving legitimate backslashes", async () => {
    const postBotCommand = (rawArg: string) =>
      JSON.stringify({
        content: [
          [
            { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
            { tag: "text", text: ` /workspace ${rawArg}` },
          ],
        ],
      });

    const parsedHyphen = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_hyphen_parse",
          chat_id: "oc_grp_cmd_escape",
          chat_type: "group",
          message_type: "post",
          content: postBotCommand("research-2026"),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(parsedHyphen?.text).toBe("/workspace research\\-2026");
    expect(parsedHyphen?.commandText).toBe("/workspace research-2026");

    const parsedBackslash = parseFeishuInboundEvent(
      {
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_backslash_parse",
          chat_id: "oc_grp_cmd_escape",
          chat_type: "group",
          message_type: "post",
          content: postBotCommand(String.raw`domain\team_2026-A`),
          mentions: [],
        },
      },
      { botOpenId: BOT_OPEN_ID },
    );
    expect(parsedBackslash?.commandText).toBe(
      String.raw`/workspace domain\team_2026-A`,
    );

    const h = createTestHarness();
    try {
      const feishuAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client: createFakeLarkClient().client,
      });
      feishuAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });

      const workspaceQueries = [
        "research-2026",
        "quant_alpha_1",
        "半导体研究-2026_A组",
        "Macro Research 2026 - Core_1",
        "99999999-8888-7777-6666-555555555555",
      ];

      for (let i = 0; i < workspaceQueries.length; i += 1) {
        const query = workspaceQueries[i]!;
        await feishuAdapter.receiveRawEvent({
          sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
          message: {
            message_id: `om_post_ws_arg_${i}`,
            chat_id: "oc_grp_cmd_escape",
            chat_type: "group",
            message_type: "post",
            content: postBotCommand(query),
            mentions: [
              {
                key: "@_user_1",
                id: { open_id: BOT_OPEN_ID },
                name: "DaltonBot",
              },
            ],
          },
        });
      }
      await new Promise((r) => setTimeout(r, 20));

      const switchQueries = vi
        .mocked(h.backend.sessionCommand)
        .mock.calls.filter(([req]) => req.cmd === "workspace_switch")
        .map(([req]) => req.workspace_query);
      expect(switchQueries).toEqual(workspaceQueries);
      expect(h.backend.appendCalls).toHaveLength(0);

      // Mentioning another user before /workspace in post does NOT falsely trigger a slash command
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_other_user_before_cmd",
          chat_id: "oc_grp_cmd_escape",
          chat_type: "group",
          message_type: "post",
          content: JSON.stringify({
            content: [
              [
                { tag: "at", user_id: BOT_OPEN_ID, user_name: "DaltonBot" },
                { tag: "at", user_id: "ou_bob", user_name: "Bob" },
                { tag: "text", text: " /workspace research-2026" },
              ],
            ],
          }),
          mentions: [],
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(
        vi
          .mocked(h.backend.sessionCommand)
          .mock.calls.filter(([req]) => req.cmd === "workspace_switch"),
      ).toHaveLength(workspaceQueries.length);
      expect(h.backend.appendCalls).toHaveLength(1);
      expect(h.backend.appendCalls[0]?.payload.messages[0]?.content).toBe(
        "@Bob /workspace research\\-2026",
      );

      // Normal conversation post keeps Markdown rendering (bold, links, escaped specials)
      await feishuAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_normal_markdown",
          chat_id: "oc_dm_cmd_escape",
          chat_type: "p2p",
          message_type: "post",
          content: JSON.stringify({
            content: [
              [
                { tag: "text", text: "Review " },
                {
                  tag: "text",
                  text: "research-2026",
                  style: { bold: true },
                },
                { tag: "text", text: " and model_v1" },
              ],
            ],
          }),
        },
      });
      await new Promise((r) => setTimeout(r, 20));
      expect(h.backend.appendCalls).toHaveLength(2);
      expect(h.backend.appendCalls[1]?.payload.messages[0]?.content).toBe(
        "Review **research\\-2026** and model\\_v1",
      );
    } finally {
      await h.cleanup();
    }
  });

  it.each(["stream", "buffer", "arrayBuffer"] as const)("reports an oversized %s attachment once without retrying it on startup recovery", async (responseType) => {
    const MESSAGE_ID = "om_post_too_large_1";
    const postContent = JSON.stringify({
      content: [[{ tag: "text", text: "请分析附件" }]],
      files: [
        { file_key: "file_big", file_name: "report.pdf", is_folder: false },
      ],
    });
    const h = createTestHarness();
    try {
      const { client, calls } = createFakeLarkClient({
        onMessageResourceGet: async () => {
          const bytes = Buffer.alloc(21 * 1024 * 1024, 7);
          const headers = { "content-type": "application/pdf" };
          if (responseType === "buffer") return { data: bytes, headers };
          if (responseType === "arrayBuffer") {
            return { data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength), headers };
          }
          const stream = new PassThrough();
          stream.end(bytes);
          return {
            getReadableStream: () => stream,
            headers,
          };
        },
      });
      const realAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client,
      });
      realAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      h.feishuAdapter.setDownloadAttachmentDelegate((descriptor, event) =>
        realAdapter.downloadAttachment(descriptor, event),
      );

      await realAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: MESSAGE_ID,
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: postContent,
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      expect(calls.messageResourceGet).toHaveLength(1);
      expect(h.backend.appendCalls).toHaveLength(0);
      expect(h.backend.createCalls).toHaveLength(0);
      expect(
        h.backend.progressUpdates.some(
          (u) => u.message_id === MESSAGE_ID && u.status === "failed",
        ),
      ).toBe(true);
      expect(
        h.feishuAdapter.sentTexts.some((t) =>
          t.text.includes("exceeds the 20 MB per-file limit"),
        ),
      ).toBe(true);

      vi.mocked(h.backend.recoverState).mockImplementationOnce(async () => ({
        mode: "startup" as const,
        inbound_events: [
          {
            platform: "feishu" as const,
            message_id: MESSAGE_ID,
            platform_user_id: "ou_alice",
            dalton_user_id: USER_ID,
            chat_type: "dm" as const,
            chat_address: "feishu:@me",
            thread_scope_key: "dm:ou_alice",
            request_key: makeInboundRequestKey("feishu", MESSAGE_ID),
            retry_request_key: null,
            resubmitted_request_key: null,
            steered_into_run_id: null,
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
            workspace_id: "99999999-8888-7777-6666-555555555555",
            external_thread_id: "feishu:dm:ou_alice:gen:1",
            generation: 1,
            status: "failed" as const,
            fallback_pending: false,
            sent_to_current_count: 0,
            uploaded_attachments: [],
            raw_payload: {
              platform_user_id: "ou_alice",
              text: "请分析附件",
              chat_id: "oc_dm_1",
              attachments: [
                {
                  id: "file_big",
                  filename: "report.pdf",
                  mimeType: "application/pdf",
                },
              ],
            },
            pending_interrupt: null,
          },
        ],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      }));
      await h.app.orchestrator.runStartupRecovery();
      expect(calls.messageResourceGet).toHaveLength(1);
      expect(h.backend.appendCalls).toHaveLength(0);
      expect(h.backend.createCalls).toHaveLength(0);
    } finally {
      await h.cleanup();
    }
  });

  it("uploads same-name attachments to unique stable paths with their own bytes, including sanitized collisions", async () => {
    const h = createTestHarness();
    try {
      const contentsByKey: Record<string, string> = {
        file_one: "FIRST_PDF_BYTES",
        file_two: "SECOND_PDF_BYTES",
        file_three: "THIRD_FILE_BYTES",
        file_four: "FOURTH_FILE_BYTES",
      };
      const { client } = createFakeLarkClient({
        onMessageResourceGet: async (args) => ({
          data: Buffer.from(
            contentsByKey[args.path.file_key] ?? "UNKNOWN",
            "utf8",
          ),
          headers: { "content-type": "application/pdf" },
        }),
      });
      const realAdapter = new FeishuChannelAdapter({
        appId: "cli_test_app",
        appSecret: "sec_test_secret",
        botOpenId: BOT_OPEN_ID,
        client,
      });
      realAdapter.onInboundEvent(async (ev) => {
        await h.app.orchestrator.handleInboundEvent(ev);
      });
      h.feishuAdapter.setDownloadAttachmentDelegate((descriptor, event) =>
        realAdapter.downloadAttachment(descriptor, event),
      );

      await realAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: "om_post_same_name_1",
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: JSON.stringify({
            content: [[{ tag: "text", text: "请分析附件" }]],
            files: [
              {
                file_key: "file_one",
                file_name: "report.pdf",
                is_folder: false,
              },
              {
                file_key: "file_two",
                file_name: "report.pdf",
                is_folder: false,
              },
              {
                file_key: "file_three",
                file_name: "my report.pdf",
                is_folder: false,
              },
              {
                file_key: "file_four",
                file_name: "my?report.pdf",
                is_folder: false,
              },
            ],
          }),
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      const uploads = vi.mocked(h.backend.uploadWorkspaceFile).mock.calls;
      expect(uploads).toHaveLength(4);
      const byPath = new Map(
        uploads.map((call) => [
          call[2] as string,
          (call[4] as Buffer).toString("utf8"),
        ]),
      );
      expect(byPath.get("uploads/feishu/om_post_same_name_1/report.pdf")).toBe(
        "FIRST_PDF_BYTES",
      );
      expect(
        byPath.get("uploads/feishu/om_post_same_name_1/2-report.pdf"),
      ).toBe("SECOND_PDF_BYTES");
      expect(
        byPath.get("uploads/feishu/om_post_same_name_1/my_report.pdf"),
      ).toBe("THIRD_FILE_BYTES");
      expect(
        byPath.get("uploads/feishu/om_post_same_name_1/4-my_report.pdf"),
      ).toBe("FOURTH_FILE_BYTES");
      const submitted = h.backend.appendCalls[0]?.payload.messages[0]
        ?.content;
      const submittedText = Array.isArray(submitted)
        ? submitted
            .map((item) => (item.type === "text" ? item.text : ""))
            .join("\n")
        : String(submitted ?? "");
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_1/report.pdf",
      );
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_1/2-report.pdf",
      );
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_1/my_report.pdf",
      );
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_1/4-my_report.pdf",
      );
    } finally {
      await h.cleanup();
    }
  });

  it("keeps the saved path for a same-name attachment already uploaded and gives the remaining one a unique path on startup recovery", async () => {
    const MESSAGE_ID = "om_post_same_name_recovery_1";
    const postContent = JSON.stringify({
      content: [[{ tag: "text", text: "请分析附件" }]],
      files: [
        { file_key: "file_one", file_name: "report.pdf", is_folder: false },
        { file_key: "file_two", file_name: "report.pdf", is_folder: false },
      ],
    });
    let failSecondDownload = true;
    const { client, calls } = createFakeLarkClient({
      onMessageResourceGet: async (args) => {
        if (failSecondDownload && args.path.file_key === "file_two") {
          throw new Error("temporary second download failure");
        }
        return {
          data: Buffer.from(
            args.path.file_key === "file_one"
              ? "FIRST_PDF_BYTES"
              : "SECOND_PDF_BYTES",
            "utf8",
          ),
          headers: { "content-type": "application/pdf" },
        };
      },
    });
    const realAdapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
      client,
    });

    const h1 = createTestHarness();
    const extraHarnesses: Array<ReturnType<typeof createTestHarness>> = [];
    try {
      realAdapter.onInboundEvent(async (ev) => {
        await h1.app.orchestrator.handleInboundEvent(ev);
      });
      h1.feishuAdapter.setDownloadAttachmentDelegate((descriptor, event) =>
        realAdapter.downloadAttachment(descriptor, event),
      );
      await realAdapter.receiveRawEvent({
        sender: { sender_id: { open_id: "ou_alice" }, sender_type: "user" },
        message: {
          message_id: MESSAGE_ID,
          chat_id: "oc_dm_1",
          chat_type: "p2p",
          message_type: "post",
          content: postContent,
        },
      });
      await new Promise((r) => setTimeout(r, 50));

      const savedUploaded = [
        ...h1.backend.progressUpdates
          .filter((u) => u.message_id === MESSAGE_ID)
          .flatMap((u) => u.uploaded_attachments ?? []),
      ].slice(-1);
      const firstRecord = h1.backend.progressUpdates
        .filter((u) => u.message_id === MESSAGE_ID && u.uploaded_attachments)
        .at(-1)?.uploaded_attachments;
      expect(firstRecord).toHaveLength(1);
      expect(firstRecord?.[0]?.workspace_path).toBe(
        "uploads/feishu/om_post_same_name_recovery_1/report.pdf",
      );
      expect(savedUploaded).toHaveLength(1);
      expect(h1.backend.appendCalls).toHaveLength(0);
      expect(h1.backend.createCalls).toHaveLength(0);
      const registeredRawPayload = vi.mocked(
        h1.backend.registerAndResolveInbound,
      ).mock.calls[0]?.[0].raw_payload;
      const downloadsBeforeRecovery = calls.messageResourceGet.length;

      failSecondDownload = false;
      const h2 = createTestHarness();
      extraHarnesses.push(h2);
      h2.feishuAdapter.setDownloadAttachmentDelegate((descriptor, event) =>
        realAdapter.downloadAttachment(descriptor, event),
      );
      const defaultRegister = vi
        .mocked(h2.backend.registerAndResolveInbound)
        .getMockImplementation()!;
      vi.mocked(h2.backend.registerAndResolveInbound).mockImplementation(
        async (req) => {
          const base = await defaultRegister(req);
          return {
            ...base,
            existing_status: "pending" as const,
            thread_id: THREAD_ID,
            run_id: null,
            uploaded_attachments: structuredClone(firstRecord ?? []),
            raw_payload: req.raw_payload ?? {},
          };
        },
      );
      vi.mocked(h2.backend.recoverState).mockImplementation(async () => ({
        mode: "startup" as const,
        inbound_events: [
          {
            platform: "feishu" as const,
            message_id: MESSAGE_ID,
            platform_user_id: "ou_alice",
            dalton_user_id: USER_ID,
            chat_type: "dm" as const,
            chat_address: "feishu:@me",
            thread_scope_key: "dm:ou_alice",
            request_key: makeInboundRequestKey("feishu", MESSAGE_ID),
            retry_request_key: null,
            resubmitted_request_key: null,
            steered_into_run_id: null,
            thread_id: THREAD_ID,
            run_id: null,
            turn_index: null,
            workspace_id: "99999999-8888-7777-6666-555555555555",
            external_thread_id: "feishu:dm:ou_alice:gen:1",
            generation: 1,
            status: "pending" as const,
            fallback_pending: false,
            sent_to_current_count: 0,
            uploaded_attachments: structuredClone(firstRecord ?? []),
            raw_payload: structuredClone(registeredRawPayload ?? {}),
            pending_interrupt: null,
          },
        ],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      }));
      await h2.app.orchestrator.runStartupRecovery();

      const recoveryDownloads = calls.messageResourceGet.slice(
        downloadsBeforeRecovery,
      );
      expect(recoveryDownloads).toEqual([
        {
          path: { message_id: MESSAGE_ID, file_key: "file_two" },
          params: { type: "file" },
        },
      ]);
      const h2Uploads = vi.mocked(h2.backend.uploadWorkspaceFile).mock.calls;
      expect(h2Uploads).toHaveLength(1);
      expect(h2Uploads[0]?.[2]).toBe(
        "uploads/feishu/om_post_same_name_recovery_1/2-report.pdf",
      );
      expect((h2Uploads[0]?.[4] as Buffer).toString("utf8")).toBe(
        "SECOND_PDF_BYTES",
      );
      const submitted = h2.backend.appendCalls[0]?.payload.messages[0]
        ?.content;
      const submittedText = Array.isArray(submitted)
        ? submitted
            .map((item) => (item.type === "text" ? item.text : ""))
            .join("\n")
        : String(submitted ?? "");
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_recovery_1/report.pdf",
      );
      expect(submittedText).toContain(
        "uploads/feishu/om_post_same_name_recovery_1/2-report.pdf",
      );
    } finally {
      await h1.cleanup();
      for (const extra of extraHarnesses) {
        await extra.cleanup();
      }
    }
  });

  it("tracks live WebSocket connection status across reconnecting and reconnected events", async () => {
    const { client } = createFakeLarkClient();
    let wsCallbacks: FeishuWSClientOptions | undefined;
    let wsState: "connected" | "reconnecting" | "failed" = "connected";
    const fakeWsClient: FeishuWSClientLike = {
      start: async () => {
        wsState = "connected";
      },
      close: () => {
        wsState = "failed";
      },
      getConnectionStatus: () => ({ state: wsState }),
    };

    const adapter = new FeishuChannelAdapter({
      appId: "cli_ws_app",
      appSecret: "sec_ws_secret",
      client,
      wsClientFactory: (opts) => {
        wsCallbacks = opts;
        return fakeWsClient;
      },
    });

    await adapter.start();
    expect(adapter.isConnected()).toBe(true);

    wsState = "reconnecting";
    wsCallbacks?.onReconnecting?.();
    expect(adapter.isConnected()).toBe(false);

    wsState = "connected";
    wsCallbacks?.onReconnected?.();
    expect(adapter.isConnected()).toBe(true);

    await adapter.stop();
    expect(adapter.isConnected()).toBe(false);
  });
});

describe("Feishu outbound formatting & chunking", () => {
  it("routes plain text to post (with AST two-space soft breaks) and tables/code blocks to Schema 2.0 card", () => {
    const plainText = "Here is a concise summary of the quarter.\nNext line.";
    expect(shouldUseFeishuCard(plainText)).toBe(false);
    const postJson = JSON.parse(
      buildFeishuPostMessageContent({ messageText: plainText }),
    );
    expect(postJson.zh_cn.content[0][0].tag).toBe("md");
    expect(postJson.zh_cn.content[0][0].text).toBe(
      "Here is a concise summary of the quarter.  \nNext line.",
    );

    const codeMarkdown = "Summary:\n```python\nprint('eps =', 4.25)\n```";
    expect(shouldUseFeishuCard(codeMarkdown)).toBe(true);
    const card = buildStructuredCard(codeMarkdown);
    expect(card.schema).toBe("2.0");
    expect(card.config.width_mode).toBe("fill");
    expect(card.body.elements[0]?.tag).toBe("markdown");

    const tableMarkdown =
      "| Ticker | Revenue |\n| --- | --- |\n| NVDA | $35.1B |";
    expect(shouldUseFeishuCard(tableMarkdown)).toBe(true);
    expect(countMarkdownTables(tableMarkdown)).toBe(1);
    expect(withinCardTableLimit(tableMarkdown)).toBe(true);
  });

  it("preserves list structure when materializing soft breaks inside multi-line Markdown list items and lines with HTML entities", () => {
    const listMarkdown = "- item 1\n  continuation line\n- item 2";
    expect(materializeFeishuPostMarkdownSoftBreaks(listMarkdown)).toBe(
      "- item 1  \n  continuation line\n- item 2",
    );
    expect(materializeFeishuPostMarkdownSoftBreaks("a &amp;\nb")).toBe(
      "a &amp;  \nb",
    );
    expect(materializeFeishuPostMarkdownSoftBreaks("a \\*\r\nb")).toBe(
      "a \\*  \r\nb",
    );
  });

  it("splits an ultra-long CJK line without newlines within the 30 KB envelope without losing characters", () => {
    const singleLongCjkLine = "中".repeat(12000);
    const chunks = chunkFeishuOutboundText(singleLongCjkLine);
    expect(chunks.length).toBe(2);
    for (const chunk of chunks) {
      const serialized = buildFeishuPostMessageContent({ messageText: chunk });
      expect(Buffer.byteLength(serialized, "utf8")).toBeLessThanOrEqual(
        FEISHU_POST_MAX_BYTES,
      );
    }
    expect(chunks.join("")).toBe(singleLongCjkLine);
  });

  it("handles fence-only inputs and overlong fence info strings followed by surrogate-pair emoji without hanging", () => {
    expect(chunkFeishuOutboundText("```\n```")).toEqual(["```\n```"]);

    const overlongInfoFence = `\`\`\`${"中".repeat(11000)}\n😀\n\`\`\``;
    const chunks = chunkFeishuOutboundText(overlongInfoFence);
    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      const built = buildStructuredCard(chunk);
      expect(
        Buffer.byteLength(JSON.stringify(built), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }
    const combined = chunks.join("\n");
    const cjkMatches = combined.match(/中/g) ?? [];
    expect(cjkMatches.length).toBe(11000);
    expect(combined).toContain("😀");
  });

  it("preserves every character and balances code fences when splitting ultra-long lines inside a fenced code block", () => {
    const fencedMixedLongLines = `\`\`\`text\n${"a".repeat(8000)}\n${"中".repeat(11000)}\n\`\`\``;
    const chunks = chunkFeishuOutboundText(fencedMixedLongLines);
    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(chunk.startsWith("```text\n")).toBe(true);
      expect(chunk.endsWith("\n```")).toBe(true);
      const built = buildStructuredCard(chunk);
      expect(
        Buffer.byteLength(JSON.stringify(built), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }
    const combined = chunks.join("\n");
    expect((combined.match(/a/g) ?? []).length).toBe(8000);
    expect((combined.match(/中/g) ?? []).length).toBe(11000);
  });

  it("splits oversized post payloads within the 30 KB UTF-8 byte limit", () => {
    const hugePlainParagraphs = Array.from(
      { length: 400 },
      (_, idx) =>
        `Paragraph ${idx + 1}: ${"Revenue and margin commentary ".repeat(4)}`,
    ).join("\n\n");
    expect(Buffer.byteLength(hugePlainParagraphs, "utf8")).toBeGreaterThan(
      FEISHU_POST_MAX_BYTES,
    );

    const postChunks = chunkFeishuPostMarkdown(hugePlainParagraphs);
    expect(postChunks.length).toBeGreaterThan(1);
    for (const chunk of postChunks) {
      const serialized = buildFeishuPostMessageContent({ messageText: chunk });
      expect(Buffer.byteLength(serialized, "utf8")).toBeLessThanOrEqual(
        FEISHU_POST_MAX_BYTES,
      );
    }
  });

  it("splits oversized multi-byte CJK post payloads within the 30 KB UTF-8 byte limit", () => {
    const hugeCjkText = Array.from(
      { length: 250 },
      (_, idx) => `第${idx + 1}段：${"飞书渠道网关深度研究与财务模型分析报告".repeat(3)}`,
    ).join("\n\n");
    expect(Buffer.byteLength(hugeCjkText, "utf8")).toBeGreaterThan(
      FEISHU_POST_MAX_BYTES,
    );
    const cjkChunks = chunkFeishuPostMarkdown(hugeCjkText);
    expect(cjkChunks.length).toBeGreaterThan(1);
    for (const chunk of cjkChunks) {
      const serialized = buildFeishuPostMessageContent({ messageText: chunk });
      expect(Buffer.byteLength(serialized, "utf8")).toBeLessThanOrEqual(
        FEISHU_POST_MAX_BYTES,
      );
    }
  });

  it("closes and reopens tilde (~~~) and 4-backtick (````) code fences across outbound chunks", () => {
    const tildeFenced = `~~~python\n${"x = 1\n".repeat(30)}~~~`;
    const tildeChunks = chunkTextForOutbound(tildeFenced, 80);
    expect(tildeChunks.length).toBeGreaterThan(1);
    expect(tildeChunks[0]?.endsWith("~~~")).toBe(true);
    expect(tildeChunks[1]?.startsWith("~~~python\n")).toBe(true);

    const quadBacktickFenced = `\`\`\`\`markdown\n\`\`\`ts\nconst a = 1;\n\`\`\`\n${"line\n".repeat(25)}\`\`\`\``;
    const quadChunks = chunkTextForOutbound(quadBacktickFenced, 80);
    expect(quadChunks.length).toBeGreaterThan(1);
    expect(quadChunks[0]?.endsWith("````")).toBe(true);
    expect(quadChunks[1]?.startsWith("````markdown\n")).toBe(true);
  });

  it("splits Markdown cards with more than 5 tables and oversized card payloads within limits", () => {
    const eightTablesMarkdown = Array.from({ length: 8 }, (_, idx) =>
      [
        `### Table ${idx + 1}`,
        "| Metric | Q1 | Q2 |",
        "| --- | --- | --- |",
        `| Rev_${idx + 1} | 100 | 120 |`,
      ].join("\n"),
    ).join("\n\n");

    expect(countMarkdownTables(eightTablesMarkdown)).toBe(8);
    expect(withinCardTableLimit(eightTablesMarkdown)).toBe(false);

    const tableSplitChunks = chunkFeishuCardMarkdown(eightTablesMarkdown);
    expect(tableSplitChunks.length).toBe(2);
    expect(countMarkdownTables(tableSplitChunks[0]!)).toBeLessThanOrEqual(
      FEISHU_CARD_TABLE_LIMIT,
    );
    expect(countMarkdownTables(tableSplitChunks[1]!)).toBeLessThanOrEqual(
      FEISHU_CARD_TABLE_LIMIT,
    );

    const hugeCodeMarkdown =
      "```text\n" +
      Array.from(
        { length: 600 },
        (_, i) => `line_${i}: ${"0123456789abcdef".repeat(4)}`,
      ).join("\n") +
      "\n```";
    const cardChunks = chunkFeishuCardMarkdown(hugeCodeMarkdown);
    expect(cardChunks.length).toBeGreaterThan(1);
    for (const chunk of cardChunks) {
      const built = buildStructuredCard(chunk);
      expect(
        Buffer.byteLength(JSON.stringify(built), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }
  });

  it("re-chunks post-routed segments after card table splitting so every chunk stays within 30,720 serialized bytes in its final sendTextChunk format", async () => {
    const smallTable = "| A | B |\n| - | - |\n| 1 | 2 |";
    const tenTablesBlock = Array.from({ length: 10 }, () => smallTable).join(
      "\n\n",
    );
    const thousandLinesOf27A = Array.from({ length: 1000 }, () =>
      "a".repeat(27),
    ).join("\n");
    const combinedTenTablesAndLines = `${tenTablesBlock}\n\n${thousandLinesOf27A}`;

    const chunks = chunkFeishuOutboundText(combinedTenTablesAndLines);
    expect(chunks.length).toBe(4);
    expect(shouldUseFeishuCard(chunks[0]!)).toBe(true);
    expect(shouldUseFeishuCard(chunks[1]!)).toBe(true);
    expect(shouldUseFeishuCard(chunks[2]!)).toBe(false);
    expect(shouldUseFeishuCard(chunks[3]!)).toBe(false);

    const { client, calls } = createFakeLarkClient();
    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_chunk_verify",
      appSecret: "secret_chunk_verify",
      client,
    });

    for (let i = 0; i < chunks.length; i += 1) {
      await adapter.sendTextChunk({
        targetAddress: "feishu:oc_group_chunk_verify",
        text: chunks[i]!,
        chunkIndex: i,
        totalChunks: chunks.length,
      });
    }

    expect(calls.messageCreate).toHaveLength(chunks.length);
    expect(calls.messageCreate[0]?.data.msg_type).toBe("interactive");
    expect(calls.messageCreate[1]?.data.msg_type).toBe("interactive");
    expect(calls.messageCreate[2]?.data.msg_type).toBe("post");
    expect(calls.messageCreate[3]?.data.msg_type).toBe("post");

    let deliveredTableCount = 0;
    let deliveredACount = 0;
    for (const call of calls.messageCreate) {
      const byteLen = Buffer.byteLength(call.data.content, "utf8");
      expect(byteLen).toBeLessThanOrEqual(FEISHU_POST_MAX_BYTES);
      const parsed = JSON.parse(call.data.content);
      if (call.data.msg_type === "interactive") {
        const cardMd = parsed.body.elements[0].content as string;
        deliveredTableCount += countMarkdownTables(cardMd);
      } else {
        const postMd = parsed.zh_cn.content[0][0].text as string;
        deliveredACount += (postMd.match(/a/g) ?? []).length;
      }
    }
    expect(deliveredTableCount).toBe(10);
    expect(deliveredACount).toBe(27_000);

    // Also verify mixed tables + multibyte CJK + soft line breaks + quotes
    const sixTables = Array.from({ length: 6 }, (_, idx) =>
      `| 指标_${idx + 1} | 数值 |\n| --- | --- |\n| "营收" | ${idx + 1}00 |`,
    ).join("\n\n");
    const cjkQuotedLines = Array.from(
      { length: 600 },
      (_, idx) => `第${idx + 1}行："核心结论"与"风险提示"——毛利率稳健增长`,
    ).join("\n");
    const mixedInput = `${sixTables}\n\n${cjkQuotedLines}`;
    const mixedChunks = chunkFeishuOutboundText(mixedInput);
    expect(mixedChunks.length).toBeGreaterThanOrEqual(3);

    calls.messageCreate.length = 0;
    for (let i = 0; i < mixedChunks.length; i += 1) {
      await adapter.sendTextChunk({
        targetAddress: "feishu:oc_group_chunk_verify",
        text: mixedChunks[i]!,
        chunkIndex: i,
        totalChunks: mixedChunks.length,
      });
    }
    expect(calls.messageCreate).toHaveLength(mixedChunks.length);
    const extractedTexts: string[] = [];
    for (const call of calls.messageCreate) {
      expect(Buffer.byteLength(call.data.content, "utf8")).toBeLessThanOrEqual(
        FEISHU_POST_MAX_BYTES,
      );
      const parsed = JSON.parse(call.data.content);
      if (call.data.msg_type === "interactive") {
        extractedTexts.push(parsed.body.elements[0].content as string);
      } else {
        extractedTexts.push(parsed.zh_cn.content[0][0].text as string);
      }
    }
    const joinedDelivered = extractedTexts.join("\n");
    expect(joinedDelivered).toContain("指标_1");
    expect(joinedDelivered).toContain("指标_6");
    expect(joinedDelivered).toContain("第1行：\"核心结论\"与\"风险提示\"");
    expect(joinedDelivered).toContain("第600行：\"核心结论\"与\"风险提示\"");
  });
});

describe("Feishu outbound sending, media upload/download & error classification", () => {
  it("sends plain text as post and markdown tables/code as Schema 2.0 card with exact Lark.Client parameters", async () => {
    const { client, calls } = createFakeLarkClient();
    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client,
    });

    // 1. Plain text DM send
    const postRes = await adapter.sendTextChunk({
      targetAddress: "feishu:@me",
      recipientPlatformUserId: "ou_user_alice",
      text: "Hello Alice, your portfolio check is complete.",
      chunkIndex: 0,
      totalChunks: 1,
    });
    expect(postRes.messageId).toBe("om_created_1");
    expect(calls.messageCreate).toHaveLength(1);
    expect(calls.messageCreate[0]).toEqual({
      params: { receive_id_type: "open_id" },
      data: {
        receive_id: "ou_user_alice",
        msg_type: "post",
        content: buildFeishuPostMessageContent({
          messageText: "Hello Alice, your portfolio check is complete.",
        }),
      },
    });

    // 2. Markdown table group reply
    const tableText = "| Ticker | P/E |\n| --- | --- |\n| AAPL | 28.4x |";
    const cardRes = await adapter.sendTextChunk({
      targetAddress: "feishu:oc_group_alpha",
      text: tableText,
      replyToMessageId: "om_inbound_parent_9",
      chunkIndex: 0,
      totalChunks: 1,
    });
    expect(cardRes.messageId).toBe("om_replied_2");
    expect(calls.messageReply).toHaveLength(1);
    expect(calls.messageReply[0]?.path).toEqual({
      message_id: "om_inbound_parent_9",
    });
    expect(calls.messageReply[0]?.data.msg_type).toBe("interactive");
    const parsedCard = JSON.parse(calls.messageReply[0]!.data.content);
    expect(parsedCard.schema).toBe("2.0");
    expect(parsedCard.body.elements[0].content).toContain("| Ticker | P/E |");
  });

  it("falls back to direct send when reply target message was withdrawn (230011)", async () => {
    const { client, calls } = createFakeLarkClient({
      onMessageReply: async () => ({
        code: 230011,
        msg: "The message has been withdrawn",
      }),
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client,
    });

    const res = await adapter.sendTextChunk({
      targetAddress: "feishu:oc_group_alpha",
      text: "Fallback message body",
      replyToMessageId: "om_withdrawn_msg",
      chunkIndex: 0,
      totalChunks: 1,
    });
    expect(res.messageId).toBe("om_created_1");
    expect(calls.messageReply).toHaveLength(1);
    expect(calls.messageCreate).toHaveLength(1);
    expect(calls.messageCreate[0]?.data.receive_id).toBe("oc_group_alpha");
  });

  it("uploads images and files, sends media messages, and downloads inbound attachments", async () => {
    const { client, calls } = createFakeLarkClient();
    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client,
    });

    const imgBytes = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a]);
    const imgSendRes = await adapter.sendFile({
      targetAddress: "feishu:oc_group_alpha",
      path: "charts/chart.png",
      filename: "chart.png",
      content: imgBytes,
      mimeType: "image/png",
    });
    expect(imgSendRes.messageId).toBe("om_created_2");
    expect(imgSendRes.status).toBe("sent");
    expect(calls.imageCreate).toHaveLength(1);
    expect(calls.imageCreate[0]?.data).toEqual({
      image_type: "message",
      image: imgBytes,
    });
    expect(calls.messageCreate[0]).toEqual({
      params: { receive_id_type: "chat_id" },
      data: {
        receive_id: "oc_group_alpha",
        msg_type: "image",
        content: JSON.stringify({ image_key: "img_v3_1" }),
      },
    });

    const pdfBytes = Buffer.from("%PDF-1.7 fake report content");
    const fileSendRes = await adapter.sendFile({
      targetAddress: "feishu:@me",
      recipientPlatformUserId: "ou_user_alice",
      path: "deep/path/Q4_Report.pdf",
      filename: "deep/path/Q4_Report.pdf",
      content: pdfBytes,
      mimeType: "application/pdf",
    });
    expect(fileSendRes.messageId).toBe("om_created_4");
    expect(fileSendRes.status).toBe("sent");
    expect(calls.fileCreate).toHaveLength(1);
    expect(calls.fileCreate[0]?.data).toEqual({
      file_type: "pdf",
      file_name: "deep_path_Q4_Report.pdf",
      file: pdfBytes,
    });
    expect(calls.messageCreate[1]).toEqual({
      params: { receive_id_type: "open_id" },
      data: {
        receive_id: "ou_user_alice",
        msg_type: "file",
        content: JSON.stringify({ file_key: "file_v3_3" }),
      },
    });

    const downloaded = await adapter.downloadAttachment(
      {
        id: "file_key_inbound_1",
        filename: "memo.pdf",
        mimeType: "application/pdf",
      },
      {
        platform: "feishu",
        messageId: "om_inbound_msg_77",
        chatType: "dm",
        chatAddress: "feishu:@me",
        chatId: "oc_dm_1",
        threadScopeKey: "dm:ou_user_alice",
        platformUserId: "ou_user_alice",
        text: "",
      },
    );
    expect(calls.messageResourceGet).toHaveLength(1);
    expect(calls.messageResourceGet[0]).toEqual({
      path: {
        message_id: "om_inbound_msg_77",
        file_key: "file_key_inbound_1",
      },
      params: { type: "file" },
    });
    expect(downloaded.content.toString("utf8")).toBe("default-resource-bytes");
    expect(downloaded.filename).toBe("memo.pdf");
  });

  it("retries inbound attachment download with type=media when type=file fails with HTTP 502", async () => {
    let resourceAttempt = 0;
    const fallbackFake = createFakeLarkClient({
      onMessageResourceGet: async (call) => {
        resourceAttempt += 1;
        if (resourceAttempt === 1 && call.params.type === "file") {
          const err = Object.assign(new Error("Request failed with status code 502"), {
            response: { status: 502 },
          });
          throw err;
        }
        return {
          data: Buffer.from("recovered-video-bytes"),
          headers: { "content-type": "video/mp4" },
        };
      },
    });
    const fallbackAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: fallbackFake.client,
    });
    const recoveredMedia = await fallbackAdapter.downloadAttachment(
      {
        id: "file_key_video_502",
        filename: "clip.mp4",
        mimeType: "video/mp4",
      },
      {
        platform: "feishu",
        messageId: "om_inbound_video_88",
        chatType: "dm",
        chatAddress: "feishu:@me",
        chatId: "oc_dm_1",
        threadScopeKey: "dm:ou_user_alice",
        platformUserId: "ou_user_alice",
        text: "",
        rawPayload: {
          message: { message_type: "media" },
        },
      },
    );
    expect(fallbackFake.calls.messageResourceGet).toHaveLength(2);
    expect(fallbackFake.calls.messageResourceGet[0]?.params.type).toBe("file");
    expect(fallbackFake.calls.messageResourceGet[1]?.params.type).toBe("media");
    expect(recoveredMedia.content.toString("utf8")).toBe("recovered-video-bytes");
  });

  it("converts multipart upload request bodies to FormData and strips json Content-Type header", async () => {
    const rawUint8 = new Uint8Array([1, 2, 3]);
    const normalized = normalizeMultipartUploadData({ file: rawUint8 }) as {
      file: Buffer;
    };
    expect(Buffer.isBuffer(normalized.file)).toBe(true);

    const multipartReq = normalizeMultipartUploadData({
      method: "POST",
      url: "https://open.feishu.cn/open-apis/im/v1/files",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": "dalton",
      },
      data: {
        file_type: "pdf",
        file_name: "report.pdf",
        file: Buffer.from("%PDF-1.7"),
      },
    }) as {
      headers: Record<string, string>;
      data: FormData;
    };
    expect(multipartReq.data).toBeInstanceOf(FormData);
    expect(multipartReq.headers["Content-Type"]).toBeUndefined();
    expect(multipartReq.headers["User-Agent"]).toBe("dalton");
    expect(multipartReq.data.get("file_type")).toBe("pdf");
    const blobField = multipartReq.data.get("file") as File | null;
    expect(blobField).toBeInstanceOf(Blob);
    expect(blobField?.name).toBe("report.pdf");
    expect(await blobField?.text()).toBe("%PDF-1.7");

    const imageMultipartReq = normalizeMultipartUploadData({
      method: "POST",
      url: "https://open.feishu.cn/open-apis/im/v1/images",
      data: {
        image_type: "message",
        image: Buffer.from("png-data"),
      },
    }) as {
      data: FormData;
    };
    const imgBlobField = imageMultipartReq.data.get("image") as File | null;
    expect(imgBlobField).toBeInstanceOf(Blob);
    expect(imgBlobField?.name).toBe("image.bin");
    expect(await imgBlobField?.text()).toBe("png-data");
  });

  it("throws ChannelDeliveryError with accurate outcome and code across send and upload stages", async () => {
    // 1. Rate limit (230020) -> outcome: "rate_limited", code: "rate_limited" (`grok-8`, `codex-12`)
    const rateLimitedFake = createFakeLarkClient({
      onMessageCreate: async () => ({
        code: 230020,
        msg: "Frequency limit exceeded",
      }),
    });
    const rateLimitAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: rateLimitedFake.client,
    });
    await expect(
      rateLimitAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "rate_limited",
      code: "rate_limited",
    } satisfies Partial<ChannelDeliveryError>);

    // 2. Target not found (230002) -> outcome: "failed", code: "target_not_found"
    const notFoundFake = createFakeLarkClient({
      onMessageCreate: async () => ({
        code: 230002,
        msg: "Bot is not in chat",
      }),
    });
    const notFoundAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: notFoundFake.client,
    });
    await expect(
      notFoundAdapter.sendTextChunk({
        targetAddress: "feishu:oc_missing_group",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "failed",
      code: "target_not_found",
    } satisfies Partial<ChannelDeliveryError>);

    // 3. Permission denied (99991672 / 403) -> outcome: "failed", code: "not_allowed"
    const permFake = createFakeLarkClient({
      onMessageCreate: async () => ({
        code: 99991672,
        msg: "Missing required scope im:message",
      }),
    });
    const permAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: permFake.client,
    });
    await expect(
      permAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "failed",
      code: "not_allowed",
    } satisfies Partial<ChannelDeliveryError>);

    // 4. Confirmed undelivered non-permission 4xx / business error (230099) -> outcome: "failed", code: "unavailable" (`grok-8`)
    const badReqFake = createFakeLarkClient({
      onMessageCreate: async () => ({
        code: 230099,
        msg: "Invalid message content format",
      }),
    });
    const badReqAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: badReqFake.client,
    });
    await expect(
      badReqAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "failed",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);

    // 5. Send-stage network timeout (ETIMEDOUT) -> outcome: "unknown", code: "unavailable"
    const timeoutFake = createFakeLarkClient({
      onMessageCreate: async () => {
        const err = Object.assign(new Error("connect ETIMEDOUT"), {
          code: "ETIMEDOUT",
        });
        throw err;
      },
    });
    const timeoutAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: timeoutFake.client,
    });
    await expect(
      timeoutAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "unknown",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);

    // 6. Upload-stage timeout / 5xx (message not sent yet) -> outcome: "failed", code: "unavailable" (`codex-12`)
    const uploadTimeoutFake = createFakeLarkClient({
      onFileCreate: async () => {
        const err = Object.assign(new Error("upload ETIMEDOUT"), {
          code: "ETIMEDOUT",
        });
        throw err;
      },
    });
    const uploadTimeoutAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: uploadTimeoutFake.client,
    });
    await expect(
      uploadTimeoutAdapter.sendFile({
        targetAddress: "feishu:oc_group_1",
        path: "reports/q4.pdf",
        filename: "q4.pdf",
        content: Buffer.from("%PDF-1.7"),
        mimeType: "application/pdf",
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "failed",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);

    // 7. Missing message_id on code=0 -> outcome: "unknown", code: "unavailable" (`grok-16`)
    const missingMsgIdFake = createFakeLarkClient({
      onMessageCreate: async () => ({
        code: 0,
        data: {},
      }),
    });
    const missingMsgIdAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: missingMsgIdFake.client,
    });
    await expect(
      missingMsgIdAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "unknown",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);

    // 8. Truncated/aborted HTTP response stream during send (ERR_BAD_RESPONSE) -> outcome: "unknown" (`codex-5`)
    const badRespSendFake = createFakeLarkClient({
      onMessageCreate: async () => {
        throw Object.assign(new Error("stream has been aborted"), {
          code: "ERR_BAD_RESPONSE",
        });
      },
    });
    const badRespSendAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: badRespSendFake.client,
    });
    await expect(
      badRespSendAdapter.sendTextChunk({
        targetAddress: "feishu:oc_group_1",
        text: "test",
        chunkIndex: 0,
        totalChunks: 1,
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "unknown",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);

    // 9. Truncated/aborted HTTP response stream during upload (ERR_BAD_RESPONSE) -> outcome: "failed" (`codex-5`)
    const badRespUploadFake = createFakeLarkClient({
      onFileCreate: async () => {
        throw Object.assign(new Error("stream has been aborted"), {
          code: "ERR_BAD_RESPONSE",
        });
      },
    });
    const badRespUploadAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: badRespUploadFake.client,
    });
    await expect(
      badRespUploadAdapter.sendFile({
        targetAddress: "feishu:oc_group_1",
        path: "reports/q4.pdf",
        filename: "q4.pdf",
        content: Buffer.from("%PDF-1.7"),
        mimeType: "application/pdf",
      }),
    ).rejects.toMatchObject({
      name: "ChannelDeliveryError",
      outcome: "failed",
      code: "unavailable",
    } satisfies Partial<ChannelDeliveryError>);
  });
});

describe("Feishu listAvailableChats pagination, seenPageTokens loop guard & checkTargetLive", () => {
  it("breaks pagination loop and sets complete=false when im.chat.list returns a repeated page_token", async () => {
    const { client: cycleClient, calls: cycleCalls } = createFakeLarkClient({
      onChatList: async (call) => {
        if (!call.params.page_token) {
          return {
            code: 0,
            data: {
              items: [{ chat_id: "oc_page_1", name: "Alpha Team" }],
              has_more: true,
              page_token: "tok_page_2",
            },
          };
        }
        if (call.params.page_token === "tok_page_2") {
          return {
            code: 0,
            data: {
              items: [{ chat_id: "oc_page_2", name: "Beta Research" }],
              has_more: true,
              page_token: "tok_page_2",
            },
          };
        }
        return { code: 0, data: { items: [], has_more: false } };
      },
    });

    const cycleAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: cycleClient,
    });

    const cycleResult = await cycleAdapter.listAvailableChats({
      userId: "user_1",
      platformUserId: "ou_member_1",
    });

    expect(cycleCalls.chatList).toHaveLength(2);
    expect(cycleResult.chats).toEqual([
      { address: "feishu:oc_page_1", name: "Alpha Team", kind: "group" },
      { address: "feishu:oc_page_2", name: "Beta Research", kind: "group" },
    ]);
    expect(cycleResult.complete).toBe(false);
    expect(cycleResult.error).toContain("repeated page_token");
  });

  it("paginates im.chat.list across multiple pages and sets complete=true on normal termination", async () => {
    const { client: cleanClient } = createFakeLarkClient({
      onChatList: async (call) => {
        if (!call.params.page_token) {
          return {
            code: 0,
            data: {
              items: [{ chat_id: "oc_page_1", name: "Alpha Team" }],
              has_more: true,
              page_token: "tok_page_2",
            },
          };
        }
        return {
          code: 0,
          data: {
            items: [{ chat_id: "oc_page_2", name: "Beta Research" }],
            has_more: false,
          },
        };
      },
    });
    const cleanAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: cleanClient,
    });
    const cleanResult = await cleanAdapter.listAvailableChats({
      userId: "user_1",
      platformUserId: "ou_member_1",
    });
    expect(cleanResult.complete).toBe(true);
    expect(cleanResult.error).toBeNull();
  });

  it("verifies group existence and sender membership in checkTargetLive", async () => {
    const { client } = createFakeLarkClient({
      onChatGet: async (call) => ({
        code: 0,
        data: { name: `Semiconductor Room (${call.path.chat_id})` },
      }),
      onChatMembersGet: async () => ({
        code: 0,
        data: {
          has_more: false,
          items: [{ member_id: "ou_member_alice", name: "Alice" }],
        },
      }),
    });

    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client,
    });

    const dmCheck = await adapter.checkTargetLive({
      userId: "user_1",
      platformUserId: "ou_member_alice",
      address: "feishu:@me",
      purpose: "default_target",
    });
    expect(dmCheck.ok).toBe(true);
    expect(dmCheck.address).toBe("feishu:@me");

    const memberCheck = await adapter.checkTargetLive({
      userId: "user_1",
      platformUserId: "ou_member_alice",
      address: "feishu:oc_semi_group",
      purpose: "send",
    });
    expect(memberCheck).toEqual({
      ok: true,
      address: "feishu:oc_semi_group",
      name: "Semiconductor Room (oc_semi_group)",
      message: null,
    });

    const nonMemberCheck = await adapter.checkTargetLive({
      userId: "user_2",
      platformUserId: "ou_outsider_bob",
      address: "feishu:oc_semi_group",
      purpose: "send",
    });
    expect(nonMemberCheck.ok).toBe(false);
    expect(nonMemberCheck.message).toContain("not a member");

    // Repeated page_token in chatMembers.get reports incomplete pagination instead of "not a member" (`grok 中-1`)
    const { client: truncatedMembersClient } = createFakeLarkClient({
      onChatGet: async (call) => ({
        code: 0,
        data: { name: `Large Group (${call.path.chat_id})` },
      }),
      onChatMembersGet: async () => ({
        code: 0,
        data: {
          has_more: true,
          page_token: "stuck_member_tok",
          items: [{ member_id: "ou_someone_else", name: "Other" }],
        },
      }),
    });
    const truncatedMembersAdapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client: truncatedMembersClient,
    });
    const indeterminateCheck = await truncatedMembersAdapter.checkTargetLive({
      userId: "user_1",
      platformUserId: "ou_member_alice",
      address: "feishu:oc_large_group",
      purpose: "send",
    });
    expect(indeterminateCheck.ok).toBe(false);
    expect(indeterminateCheck.message).toContain("pagination incomplete");
    expect(indeterminateCheck.message).not.toContain("not a member");
  });
});

describe("main.ts adapter registration & shared helpers", () => {
  it("registers FeishuChannelAdapter when DALTON_FEISHU_ENABLED=true and DALTON_FEISHU_APP_ID / DALTON_FEISHU_APP_SECRET are set, and skips otherwise", () => {
    const unconfigured = loadConfig({
      INTERNAL_SERVICE_TOKEN: "0123456789abcdef0123456789abcdef_token",
    });
    expect(unconfigured.channels.feishu.enabled).toBe(false);
    const emptyMap = buildConfiguredAdapters(unconfigured);
    expect(emptyMap.has("feishu")).toBe(false);

    const credsWithoutEnabledFlag = loadConfig({
      INTERNAL_SERVICE_TOKEN: "0123456789abcdef0123456789abcdef_token",
      DALTON_FEISHU_APP_ID: "cli_live_app_id",
      DALTON_FEISHU_APP_SECRET: "sec_live_app_secret",
    });
    expect(credsWithoutEnabledFlag.channels.feishu.enabled).toBe(false);
    expect(buildConfiguredAdapters(credsWithoutEnabledFlag).has("feishu")).toBe(
      false,
    );

    const configured = loadConfig({
      INTERNAL_SERVICE_TOKEN: "0123456789abcdef0123456789abcdef_token",
      DALTON_FEISHU_ENABLED: "true",
      DALTON_FEISHU_APP_ID: "cli_live_app_id",
      DALTON_FEISHU_APP_SECRET: "sec_live_app_secret",
    });
    expect(configured.channels.feishu.enabled).toBe(true);
    expect(configured.channels.feishu.appId).toBe("cli_live_app_id");
    const configuredMap = buildConfiguredAdapters(configured);
    expect(configuredMap.has("feishu")).toBe(true);
    expect(configuredMap.get("feishu")?.isConfigured()).toBe(true);
  });

  it("rejects safeFetchBuffer by default when allowedDomains is omitted or empty", async () => {
    expect(
      isAllowedAttachmentHostname("internal-api-lark-file.feishu.cn", [
        "feishu.cn",
      ]),
    ).toBe(true);
    expect(
      isAllowedAttachmentHostname("127.0.0.1", ["127.0.0.1"]),
    ).toBe(false);
    expect(
      isAllowedAttachmentHostname("internal-api-lark-file.feishu.cn", []),
    ).toBe(false);
    expect(
      isAllowedAttachmentHostname("internal-api-lark-file.feishu.cn"),
    ).toBe(false);

    await expect(
      safeFetchBuffer("https://internal-api-lark-file.feishu.cn/file.bin"),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "disallowed_domain",
    } satisfies Partial<SafeFetchError>);

    try {
      await safeFetchBuffer("https://user:top_secret_pw@not a valid url", {
        allowedDomains: ["feishu.cn"],
      });
    } catch (err) {
      expect((err as Error).message).not.toContain("top_secret_pw");
    }
  });

  it("validates each manual redirect hop against blocked hosts and allowedDomains in safeFetchBuffer", async () => {
    const loopbackCalls: Array<{ url: string; redirect?: RequestRedirect }> = [];
    const redirectFetchLoopback = vi.fn(
      async (url: string | URL | Request, init?: RequestInit) => {
        const urlStr = String(url);
        loopbackCalls.push({ url: urlStr, redirect: init?.redirect });
        if (urlStr.includes("feishu.cn")) {
          return new Response(null, {
            status: 302,
            headers: { location: "http://127.0.0.1:8080/internal-secret" },
          });
        }
        return new Response("leaked", { status: 200 });
      },
    );
    await expect(
      safeFetchBuffer("https://file.feishu.cn/download/1", {
        allowedDomains: ["feishu.cn"],
        fetchImpl: redirectFetchLoopback as unknown as typeof fetch,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "blocked_host",
    } satisfies Partial<SafeFetchError>);
    expect(loopbackCalls).toEqual([
      { url: "https://file.feishu.cn/download/1", redirect: "manual" },
    ]);

    const disallowedCalls: Array<{ url: string; redirect?: RequestRedirect }> =
      [];
    const redirectFetchDisallowed = vi.fn(
      async (url: string | URL | Request, init?: RequestInit) => {
        const urlStr = String(url);
        disallowedCalls.push({ url: urlStr, redirect: init?.redirect });
        if (urlStr.includes("feishu.cn")) {
          return new Response(null, {
            status: 302,
            headers: { location: "https://evil.example.com/payload.bin" },
          });
        }
        return new Response("leaked", { status: 200 });
      },
    );
    await expect(
      safeFetchBuffer("https://file.feishu.cn/download/2", {
        allowedDomains: ["feishu.cn"],
        fetchImpl: redirectFetchDisallowed as unknown as typeof fetch,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "disallowed_domain",
    } satisfies Partial<SafeFetchError>);
    expect(disallowedCalls).toEqual([
      { url: "https://file.feishu.cn/download/2", redirect: "manual" },
    ]);

    const allowedRedirectCalls: Array<{
      url: string;
      redirect?: RequestRedirect;
    }> = [];
    const redirectFetchAllowed = vi.fn(
      async (url: string | URL | Request, init?: RequestInit) => {
        const urlStr = String(url);
        allowedRedirectCalls.push({ url: urlStr, redirect: init?.redirect });
        if (urlStr === "https://open.feishu.cn/start") {
          return new Response(null, {
            status: 302,
            headers: {
              location: "https://lf3-static.feishucdn.com/obj/file.bin",
            },
          });
        }
        return new Response("hop-ok", { status: 200 });
      },
    );
    const redirectedRes = await safeFetchBuffer(
      "https://open.feishu.cn/start",
      {
        allowedDomains: ["feishu.cn", "feishucdn.com"],
        fetchImpl: redirectFetchAllowed as unknown as typeof fetch,
      },
    );
    expect(redirectedRes.buffer.toString("utf8")).toBe("hop-ok");
    expect(allowedRedirectCalls).toEqual([
      { url: "https://open.feishu.cn/start", redirect: "manual" },
      {
        url: "https://lf3-static.feishucdn.com/obj/file.bin",
        redirect: "manual",
      },
    ]);
  });

  it("blocks IPv4-mapped IPv6 private, loopback, and link-local addresses in decimal and hex notation", async () => {
    expect(isBlockedHostnameOrIp("::ffff:169.254.169.254")).toBe(true);
    expect(isBlockedHostnameOrIp("[::ffff:a9fe:a9fe]")).toBe(true);
    expect(isBlockedHostnameOrIp("::ffff:172.16.0.1")).toBe(true);
    expect(isBlockedHostnameOrIp("[::ffff:ac10:1]")).toBe(true);
    expect(isBlockedHostnameOrIp("::ffff:100.64.0.1")).toBe(true);
    expect(isBlockedHostnameOrIp("::ffff:0.0.0.0")).toBe(true);

    await expect(
      safeFetchBuffer("http://[::ffff:169.254.169.254]/latest/meta-data", {
        allowedDomains: ["feishu.cn"],
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "blocked_host",
    } satisfies Partial<SafeFetchError>);
  });

  it("enforces streaming byte limit and cancels the response reader when Content-Length is absent", async () => {
    let streamCancelled = false;
    const oversizedWebStream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new Uint8Array(64));
      },
      cancel() {
        streamCancelled = true;
      },
    });
    const streamingFetch = vi.fn(
      async () =>
        new Response(oversizedWebStream, {
          status: 200,
          headers: { "content-type": "application/octet-stream" },
        }),
    );
    await expect(
      safeFetchBuffer("https://file.feishu.cn/stream.bin", {
        allowedDomains: ["feishu.cn"],
        maxBytes: 100,
        fetchImpl: streamingFetch as unknown as typeof fetch,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "size_exceeded",
    } satisfies Partial<SafeFetchError>);
    expect(streamCancelled).toBe(true);
  });

  it("aborts stalled ReadableStream on chunk idle timeout and cancels the reader cleanly", async () => {
    let stalledStreamCancelled = false;
    const stalledStream = new ReadableStream<Uint8Array>({
      start() {
        // Never enqueues data
      },
      cancel() {
        stalledStreamCancelled = true;
      },
    });
    await expect(
      readStreamWithLimit(stalledStream, {
        maxBytes: 1024,
        chunkIdleTimeoutMs: 20,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "timeout",
    } satisfies Partial<SafeFetchError>);
    expect(stalledStreamCancelled).toBe(true);
  });

  it("destroys an unended Node PassThrough stream and rejects with size_exceeded when bytes exceed maxBytes", async () => {
    const nodeStream = new PassThrough();
    nodeStream.write(Buffer.alloc(64, 0x61));
    nodeStream.write(Buffer.alloc(64, 0x62));
    // Intentionally do not call nodeStream.end()

    await expect(
      readStreamWithLimit(nodeStream, {
        maxBytes: 100,
        chunkIdleTimeoutMs: 50,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "size_exceeded",
    } satisfies Partial<SafeFetchError>);
    expect(nodeStream.destroyed).toBe(true);
  });

  it("destroys a stalled Node PassThrough stream and rejects with timeout on chunkIdleTimeoutMs without hanging", async () => {
    const stalledNodeStream = new PassThrough();
    stalledNodeStream.write(Buffer.from("partial"));
    // Intentionally stall without calling stalledNodeStream.end()

    await expect(
      readStreamWithLimit(stalledNodeStream, {
        maxBytes: 1024,
        chunkIdleTimeoutMs: 20,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "timeout",
    } satisfies Partial<SafeFetchError>);
    expect(stalledNodeStream.destroyed).toBe(true);
  });

  it("scrubs appId, appSecret, and bearer/tenant tokens from Feishu SDK logger and Error.message", () => {
    const loggedLines: string[] = [];
    const testLogger = createSubsystemLogger("feishu/test", {
      sink: {
        debug: (l) => loggedLines.push(l),
        info: (l) => loggedLines.push(l),
        warn: (l) => loggedLines.push(l),
        error: (l) => loggedLines.push(l),
      },
      enableDebug: true,
    });

    const sdkLogger = createFeishuSdkLogger({
      appId: "cli_secret_app_id_999",
      appSecret: "sec_ultra_secret_value_888",
      logger: testLogger,
    });

    sdkLogger.error([
      {
        message:
          'Request failed: {"app_id":"cli_secret_app_id_999","app_secret":"sec_ultra_secret_value_888","tenant_access_token":"t-g104999secret"}',
        config: {
          url: "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
          data: '{"app_id":"cli_secret_app_id_999","app_secret":"sec_ultra_secret_value_888"}',
          headers: { Authorization: "Bearer t-g104999secret" },
        },
      },
    ]);

    testLogger.error("Error with token", {
      error: new Error(
        "Failed with Bearer secret_jwt_token_123 and app_secret=sec_ultra_secret_value_888",
      ),
    });

    const combinedLogs = loggedLines.join("\n");
    expect(combinedLogs).not.toContain("cli_secret_app_id_999");
    expect(combinedLogs).not.toContain("sec_ultra_secret_value_888");
    expect(combinedLogs).not.toContain("t-g104999secret");
    expect(combinedLogs).not.toContain("secret_jwt_token_123");
    expect(
      redactCredentialString('{"app_secret":"my_sec","token":"abc"}'),
    ).toBe('{"app_secret":"[REDACTED]","token":"[REDACTED]"}');
  });

  it("delegates workspace upload and download to BackendClient and sanitizes traversal filenames", async () => {
    const fakeBackend = {
      uploadWorkspaceFile: vi.fn(async () => undefined),
      downloadWorkspaceFile: vi.fn(async () => ({
        content: Buffer.from("report-bytes"),
        mimeType: "application/pdf",
      })),
    };

    const uploaded = await uploadAttachmentToWorkspace({
      backendClient: fakeBackend,
      userId: "user_1",
      workspaceId: "ws_home_1",
      platform: "feishu",
      messageId: "om_msg_1",
      filename: "../unsafe/chart.png",
      content: Buffer.from("png-bytes"),
      mimeType: "image/png",
    });
    expect(uploaded.workspaceId).toBe("ws_home_1");
    expect(uploaded.workspacePath).toBe(
      "uploads/feishu/om_msg_1/unsafe_chart.png",
    );
    expect(fakeBackend.uploadWorkspaceFile).toHaveBeenCalledOnce();

    const downloaded = await downloadWorkspaceFileToBuffer({
      backendClient: fakeBackend,
      userId: "user_1",
      workspaceId: "ws_home_1",
      workspacePath: "reports/report.pdf",
    });
    expect(downloaded.filename).toBe("report.pdf");
    expect(downloaded.sizeBytes).toBe(12);
    expect(fakeBackend.downloadWorkspaceFile).toHaveBeenCalledWith(
      "user_1",
      "ws_home_1",
      "reports/report.pdf",
    );

    const converted = convertMarkdownTables(
      "| Col1 | Col2 |\n| --- | --- |\n| A | B |",
      "code",
    );
    expect(converted).toContain("```");
    expect(converted).toContain("| Col1 | Col2 |");

    let now = 1000;
    const dedupe = createMemoryDedupe({ ttlMs: 500, now: () => now });
    expect(dedupe.check("k1")).toBe(false);
    expect(dedupe.check("k1")).toBe(true);
    now = 1600;
    expect(dedupe.check("k1")).toBe(false);
  });
});
