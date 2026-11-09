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
  chunkFeishuPostMarkdown,
  countMarkdownTables,
  detectIdType,
  FEISHU_CARD_MAX_BYTES,
  FEISHU_CARD_TABLE_LIMIT,
  FEISHU_POST_MAX_BYTES,
  FeishuChannelAdapter,
  formatFeishuTarget,
  looksLikeFeishuId,
  normalizeFeishuTarget,
  normalizeMentions,
  normalizeMultipartUploadData,
  parseFeishuConversationId,
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
  isAllowedAttachmentHostname,
  SafeFetchError,
  safeFetchBuffer,
} from "../src/shared/safe-fetch.js";
import { convertMarkdownTables } from "../src/shared/text.js";
import { downloadWorkspaceFileToBuffer } from "../src/shared/workspace-download.js";
import { uploadAttachmentToWorkspace } from "../src/shared/workspace-upload.js";
import type { InboundChannelEvent } from "../src/types.js";

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
  params: { type: "image" | "file" };
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
}) {
  const calls = {
    messageCreate: [] as MessageCreateCall[],
    messageReply: [] as MessageReplyCall[],
    imageCreate: [] as ImageCreateCall[],
    fileCreate: [] as FileCreateCall[],
    messageResourceGet: [] as MessageResourceGetCall[],
    chatList: [] as ChatListCall[],
    chatGet: [] as ChatGetCall[],
    chatMembersGet: [] as ChatMembersGetCall[],
  };

  let seq = 1;

  const client = {
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
        get: async (_call: { path: { message_id: string } }) => ({
          code: 0,
          data: { items: [] },
        }),
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

describe("Feishu conversation ID & target codec", () => {
  it("round-trips oc_, ou_, and on_ targets and conversation scopes", () => {
    expect(detectIdType("oc_chat123")).toBe("chat_id");
    expect(detectIdType("ou_user456")).toBe("open_id");
    expect(detectIdType("on_union789")).toBe("open_id");
    expect(detectIdType("custom_user_id")).toBe("user_id");

    expect(resolveReceiveIdType("feishu:oc_chat123")).toBe("chat_id");
    expect(resolveReceiveIdType("chat:oc_chat123")).toBe("chat_id");
    expect(resolveReceiveIdType("feishu:ou_user456")).toBe("open_id");
    expect(resolveReceiveIdType("user:ou_user456")).toBe("open_id");
    expect(resolveReceiveIdType("user:employee_01")).toBe("user_id");
    expect(resolveReceiveIdType("on_union789")).toBe("open_id");

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
    expect(parseFeishuTargetId("feishu:group:oc_chat123")).toBe("oc_chat123");

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
    ).toBe("feishu:p2p:ou_alice");
    expect(
      buildDaltonFeishuThreadScopeKey({
        chatType: "group",
        chatId: "oc_grp_1",
        senderOpenId: "ou_alice",
      }),
    ).toBe("feishu:group:oc_grp_1");
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

  it("parses DM and group plain text messages and @bot mentions", () => {
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
      threadScopeKey: "feishu:p2p:ou_alice_1",
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
      threadScopeKey: "feishu:group:oc_research_grp",
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

  it("parses post rich text, image, file, and merge_forward messages", () => {
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

    const mergeForwardRaw = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_merge_1",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "merge_forward",
        content: JSON.stringify({
          messages: [
            {
              msg_type: "text",
              body: { content: JSON.stringify({ text: "First forwarded line" }) },
            },
            {
              msg_type: "text",
              body: { content: JSON.stringify({ text: "Second forwarded line" }) },
            },
          ],
        }),
      },
    };
    const mergeEvent = parseFeishuInboundEvent(mergeForwardRaw, {
      botOpenId: BOT_OPEN_ID,
    });
    expect(mergeEvent?.text).toContain("[Merged and Forwarded Messages]");
    expect(mergeEvent?.text).toContain("First forwarded line");
    expect(mergeEvent?.text).toContain("Second forwarded line");
  });

  it("deduplicates repeated message_ids and serializes per-conversation events", async () => {
    const payload = {
      sender: { sender_id: { open_id: "ou_alice_1" }, sender_type: "user" },
      message: {
        message_id: "om_dup_100",
        chat_id: "oc_dm_1",
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: "Hello once" }),
      },
    };

    expect(resolveFeishuMessageDedupeKey(payload)).toBe("feishu:om_dup_100");

    const adapter = new FeishuChannelAdapter({
      appId: "cli_test_app",
      appSecret: "sec_test_secret",
      botOpenId: BOT_OPEN_ID,
    });

    const received: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev: InboundChannelEvent) => {
      received.push(ev);
    });

    await adapter.receiveRawEvent(payload);
    await adapter.receiveRawEvent(payload);
    expect(received).toHaveLength(1);
    expect(received[0]?.messageId).toBe("om_dup_100");

    // Verify per-key sequential execution order
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
});

describe("Feishu outbound formatting & chunking", () => {
  it("routes plain text to post and tables/code blocks to Schema 2.0 card", () => {
    const plainText = "Here is a concise summary of the quarter.\nNext line.";
    expect(shouldUseFeishuCard(plainText)).toBe(false);
    const postJson = JSON.parse(
      buildFeishuPostMessageContent({ messageText: plainText }),
    );
    expect(postJson.zh_cn.content[0][0].tag).toBe("md");
    expect(postJson.zh_cn.content[0][0].text).toContain(
      "Here is a concise summary of the quarter.\n\nNext line.",
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

  it("splits oversized post and card payloads and enforces <= 5 tables per card", () => {
    // 1. Oversized post (> 30,720 bytes)
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

    // 2. Markdown with 8 tables (> FEISHU_CARD_TABLE_LIMIT = 5)
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

    // 3. Oversized card (> 30,720 bytes)
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

  it("uploads images and files and sends media messages, plus downloads inbound attachments", async () => {
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

    // Inbound attachment download
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
        threadScopeKey: "feishu:p2p:ou_user_alice",
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

    // Multipart normalization check
    const rawUint8 = new Uint8Array([1, 2, 3]);
    const normalized = normalizeMultipartUploadData({ file: rawUint8 }) as {
      file: Buffer;
    };
    expect(Buffer.isBuffer(normalized.file)).toBe(true);
  });

  it("throws ChannelDeliveryError with accurate outcome and code on failures", async () => {
    // 1. Rate limit (230020) -> outcome: "failed", code: "rate_limited"
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
      outcome: "failed",
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

    // 3. Network timeout (ETIMEDOUT) -> outcome: "unknown", code: "unavailable"
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
  });
});

describe("Feishu listAvailableChats pagination, seenPageTokens loop guard & checkTargetLive", () => {
  it("paginates im.chat.list and breaks on repeated page_token (seenPageTokens)", async () => {
    const { client, calls } = createFakeLarkClient({
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
              // Intentional cycle back to tok_page_2 to test seenPageTokens guard!
              page_token: "tok_page_2",
            },
          };
        }
        return { code: 0, data: { items: [], has_more: false } };
      },
    });

    const adapter = new FeishuChannelAdapter({
      appId: "cli_app_1",
      appSecret: "secret_1",
      client,
    });

    const result = await adapter.listAvailableChats({
      userId: "user_1",
      platformUserId: "ou_member_1",
    });

    expect(calls.chatList).toHaveLength(2);
    expect(result.chats).toEqual([
      { address: "feishu:oc_page_1", name: "Alpha Team", kind: "group" },
      { address: "feishu:oc_page_2", name: "Beta Research", kind: "group" },
    ]);
    expect(result.complete).toBe(true);
    expect(result.error).toBeNull();
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

  it("enforces domain allowlist and 20 MB cap in safeFetchBuffer and delegates workspace upload/download to BackendClient", async () => {
    expect(
      isAllowedAttachmentHostname("internal-api-lark-file.feishu.cn", [
        "feishu.cn",
      ]),
    ).toBe(true);
    expect(
      isAllowedAttachmentHostname("127.0.0.1", ["127.0.0.1"]),
    ).toBe(false);

    await expect(
      safeFetchBuffer("https://evil.example.com/file.bin", {
        allowedDomains: ["feishu.cn"],
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "disallowed_domain",
    } satisfies Partial<SafeFetchError>);

    const fakeBackend = {
      ensureHomeWorkspace: vi.fn(async () => ({
        workspace_id: "ws_home_1",
        status: "ready",
      })),
      startWorkspace: vi.fn(async () => ({ status: "running" })),
      uploadWorkspaceFile: vi.fn(async () => undefined),
      downloadWorkspaceFile: vi.fn(async () => ({
        content: Buffer.from("report-bytes"),
        mimeType: "application/pdf",
      })),
    };

    const uploaded = await uploadAttachmentToWorkspace({
      backendClient: fakeBackend,
      userId: "user_1",
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

    // Table conversion helper for non-card fallback
    const converted = convertMarkdownTables(
      "| Col1 | Col2 |\n| --- | --- |\n| A | B |",
      "code",
    );
    expect(converted).toContain("```");
    expect(converted).toContain("| Col1 | Col2 |");

    // Memory dedupe TTL expiration
    let now = 1000;
    const dedupe = createMemoryDedupe({ ttlMs: 500, now: () => now });
    expect(dedupe.check("k1")).toBe(false);
    expect(dedupe.check("k1")).toBe(true);
    now = 1600;
    expect(dedupe.check("k1")).toBe(false);
  });
});
