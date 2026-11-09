/**
 * Feishu conversation ID codec, receive_id_type resolution, and Dalton address mapping.
 *
 * Adapted from:
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/conversation-id-D00i9uy5.mjs` (lines 6–119)
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` (lines 633–702)
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 3 (`src/channels/feishu/targets.ts`), §1.5 & §3.1.
 */

export type FeishuIdType = "chat_id" | "open_id" | "user_id";

export type FeishuConversationScope =
  | "group"
  | "group_sender"
  | "group_topic"
  | "group_topic_sender";

export interface ParsedFeishuConversationId {
  chatId: string;
  scope: FeishuConversationScope;
  senderOpenId?: string;
  topicId?: string;
  canonicalConversationId: string;
}

const CHAT_ID_PREFIX = "oc_";
const OPEN_ID_PREFIX = "ou_";
const USER_ID_REGEX = /^[a-zA-Z0-9_-]+$/;

export function stripFeishuProviderPrefix(raw: string): string {
  return raw.replace(/^(feishu|lark):/i, "").trim();
}

export function detectIdType(id: string): FeishuIdType | null {
  const trimmed = id.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith(CHAT_ID_PREFIX)) {
    return "chat_id";
  }
  if (trimmed.startsWith(OPEN_ID_PREFIX)) {
    return "open_id";
  }
  if (trimmed.startsWith("on_")) {
    return "open_id";
  }
  if (USER_ID_REGEX.test(trimmed)) {
    return "user_id";
  }
  return null;
}

export function normalizeFeishuTarget(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  const withoutProvider = stripFeishuProviderPrefix(trimmed);
  if (withoutProvider === "@me") {
    return "@me";
  }
  const lowered = withoutProvider.toLowerCase();
  if (lowered.startsWith("chat:")) {
    return withoutProvider.slice("chat:".length).trim() || null;
  }
  if (lowered.startsWith("group:")) {
    return withoutProvider.slice("group:".length).trim() || null;
  }
  if (lowered.startsWith("channel:")) {
    return withoutProvider.slice("channel:".length).trim() || null;
  }
  if (lowered.startsWith("user:")) {
    return withoutProvider.slice("user:".length).trim() || null;
  }
  if (lowered.startsWith("dm:")) {
    return withoutProvider.slice("dm:".length).trim() || null;
  }
  if (lowered.startsWith("open_id:")) {
    return withoutProvider.slice("open_id:".length).trim() || null;
  }
  return withoutProvider || null;
}

export function formatFeishuTarget(id: string, type?: FeishuIdType): string {
  const trimmed = id.trim();
  if (type === "chat_id" || trimmed.startsWith(CHAT_ID_PREFIX)) {
    return `chat:${trimmed}`;
  }
  return `user:${trimmed}`;
}

export function resolveReceiveIdType(target: string): FeishuIdType {
  const trimmed = target.trim();
  const lowered = trimmed.toLowerCase();
  if (
    lowered.startsWith("chat:") ||
    lowered.startsWith("group:") ||
    lowered.startsWith("channel:")
  ) {
    return "chat_id";
  }
  if (lowered.startsWith("open_id:")) {
    return "open_id";
  }
  if (lowered.startsWith("user:") || lowered.startsWith("dm:")) {
    const rawId = trimmed.replace(/^(user|dm):/i, "").trim();
    return rawId.startsWith(OPEN_ID_PREFIX) ? "open_id" : "user_id";
  }
  const raw = stripFeishuProviderPrefix(trimmed);
  if (raw.startsWith(CHAT_ID_PREFIX)) {
    return "chat_id";
  }
  if (raw.startsWith(OPEN_ID_PREFIX)) {
    return "open_id";
  }
  // Note: `on_` is union_id in Feishu API, or user_id when alphanumeric;
  // default to open_id for `ou_` / `on_` and user_id otherwise.
  if (raw.startsWith("on_")) {
    return "open_id";
  }
  if (USER_ID_REGEX.test(raw)) {
    return "user_id";
  }
  return "open_id";
}

export function looksLikeFeishuId(raw: string): boolean {
  const cleaned = stripFeishuProviderPrefix(raw.trim());
  if (!cleaned) {
    return false;
  }
  if (
    /^(chat|group|channel|user|dm|open_id):/i.test(cleaned) ||
    cleaned.startsWith(CHAT_ID_PREFIX) ||
    cleaned.startsWith(OPEN_ID_PREFIX) ||
    cleaned.startsWith("on_")
  ) {
    return true;
  }
  return false;
}

export function buildFeishuConversationId(params: {
  chatId: string;
  scope: FeishuConversationScope;
  senderOpenId?: string;
  topicId?: string;
}): string {
  const chatId = params.chatId.trim();
  const senderOpenId = params.senderOpenId?.trim();
  const topicId = params.topicId?.trim();
  switch (params.scope) {
    case "group_sender":
      return senderOpenId ? `${chatId}:sender:${senderOpenId}` : chatId;
    case "group_topic":
      return topicId ? `${chatId}:topic:${topicId}` : chatId;
    case "group_topic_sender":
      if (topicId && senderOpenId) {
        return `${chatId}:topic:${topicId}:sender:${senderOpenId}`;
      }
      if (topicId) {
        return `${chatId}:topic:${topicId}`;
      }
      if (senderOpenId) {
        return `${chatId}:sender:${senderOpenId}`;
      }
      return chatId;
    case "group":
    default:
      return chatId;
  }
}

export function parseFeishuConversationId(params: {
  conversationId: string;
  parentConversationId?: string;
}): ParsedFeishuConversationId | null {
  const conversationId = params.conversationId.trim();
  const parentConversationId = params.parentConversationId?.trim();
  if (!conversationId) {
    return null;
  }

  const topicSenderMatch =
    /^([^:]+):topic:([^:]+):sender:([^:]+)$/.exec(conversationId);
  if (topicSenderMatch) {
    const [, rawChatId, rawTopicId, rawSenderOpenId] = topicSenderMatch;
    const chatId = rawChatId?.trim() ?? "";
    const topicId = rawTopicId?.trim() ?? "";
    const senderOpenId = rawSenderOpenId?.trim() ?? "";
    if (!chatId || !topicId || !senderOpenId) {
      return null;
    }
    return {
      chatId,
      topicId,
      senderOpenId,
      scope: "group_topic_sender",
      canonicalConversationId: buildFeishuConversationId({
        chatId,
        scope: "group_topic_sender",
        topicId,
        senderOpenId,
      }),
    };
  }

  const topicMatch = /^([^:]+):topic:([^:]+)$/.exec(conversationId);
  if (topicMatch) {
    const [, rawChatId, rawTopicId] = topicMatch;
    const chatId = rawChatId?.trim() ?? "";
    const topicId = rawTopicId?.trim() ?? "";
    if (!chatId || !topicId) {
      return null;
    }
    return {
      chatId,
      topicId,
      scope: "group_topic",
      canonicalConversationId: buildFeishuConversationId({
        chatId,
        scope: "group_topic",
        topicId,
      }),
    };
  }

  const senderMatch = /^([^:]+):sender:([^:]+)$/.exec(conversationId);
  if (senderMatch) {
    const [, rawChatId, rawSenderOpenId] = senderMatch;
    const chatId = rawChatId?.trim() ?? "";
    const senderOpenId = rawSenderOpenId?.trim() ?? "";
    if (!chatId || !senderOpenId) {
      return null;
    }
    return {
      chatId,
      senderOpenId,
      scope: "group_sender",
      canonicalConversationId: buildFeishuConversationId({
        chatId,
        scope: "group_sender",
        senderOpenId,
      }),
    };
  }

  if (parentConversationId) {
    return {
      chatId: parentConversationId,
      topicId: conversationId,
      scope: "group_topic",
      canonicalConversationId: buildFeishuConversationId({
        chatId: parentConversationId,
        scope: "group_topic",
        topicId: conversationId,
      }),
    };
  }

  return {
    chatId: conversationId,
    scope: "group",
    canonicalConversationId: conversationId,
  };
}

export function parseFeishuTargetId(raw: string | undefined | null): string | null {
  const trimmed = raw?.trim();
  if (!trimmed) {
    return null;
  }
  const stripped = stripFeishuProviderPrefix(trimmed);
  for (const prefix of [
    "chat:",
    "group:",
    "channel:",
    "user:",
    "dm:",
    "open_id:",
  ]) {
    if (stripped.toLowerCase().startsWith(prefix)) {
      return stripped.slice(prefix.length).trim() || null;
    }
  }
  return stripped || null;
}

export function parseFeishuDirectConversationId(
  raw: string | undefined | null,
): string | undefined {
  const target = parseFeishuTargetId(raw);
  if (!target || target.includes(":")) {
    return undefined;
  }
  return target.startsWith("ou_") || target.startsWith("on_")
    ? target
    : undefined;
}

/**
 * Builds the Dalton `chatAddress` per Design §3.1:
 * - DM (`p2p` / `dm`): `"feishu:@me"`
 * - Group (`group`): `"feishu:<chat_id>"`
 */
export function buildDaltonFeishuChatAddress(params: {
  chatType: "dm" | "p2p" | "group";
  chatId: string;
}): string {
  if (params.chatType === "p2p" || params.chatType === "dm") {
    return "feishu:@me";
  }
  const cleanChatId = stripFeishuProviderPrefix(params.chatId);
  return `feishu:${cleanChatId}`;
}

/**
 * Builds the Dalton `threadScopeKey` per Design §1.5:
 * - DM (`p2p` / `dm`): `"feishu:p2p:<sender_open_id>"`
 * - Group (`group`): `"feishu:group:<chat_id>"`
 */
export function buildDaltonFeishuThreadScopeKey(params: {
  chatType: "dm" | "p2p" | "group";
  chatId: string;
  senderOpenId: string;
}): string {
  if (params.chatType === "p2p" || params.chatType === "dm") {
    return `feishu:p2p:${params.senderOpenId.trim()}`;
  }
  const cleanChatId = stripFeishuProviderPrefix(params.chatId);
  return `feishu:group:${cleanChatId}`;
}

/**
 * Resolves Dalton outbound target address (`feishu:@me`, `feishu:oc_xxx`, `feishu:ou_xxx`)
 * into a concrete Feishu target ID (`oc_xxx` or `ou_xxx`) for API delivery.
 */
export function resolveDaltonOutboundTarget(params: {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
}): string | null {
  const normalized = normalizeFeishuTarget(params.targetAddress);
  if (normalized === "@me") {
    const recipient = params.recipientPlatformUserId?.trim();
    return recipient ? stripFeishuProviderPrefix(recipient) : null;
  }
  if (!normalized) {
    const recipient = params.recipientPlatformUserId?.trim();
    return recipient ? stripFeishuProviderPrefix(recipient) : null;
  }
  const parsedConv = parseFeishuConversationId({ conversationId: normalized });
  return parsedConv?.chatId ?? normalized;
}
