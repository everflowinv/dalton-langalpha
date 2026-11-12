import type { BackendClient } from "../backend-client.js";
import type {
  AvailableChatItem,
  ChannelPlatform,
  InboundAttachmentDescriptor,
  InboundChannelEvent,
  NativeInteractionEvent,
} from "../types.js";

export type ChannelErrorOutcome = "failed" | "unknown" | "rate_limited";
export type ChannelErrorCode =
  | "target_not_found"
  | "rate_limited"
  | "unavailable"
  | "not_allowed";

export class ChannelDeliveryError extends Error {
  readonly outcome: ChannelErrorOutcome;
  readonly code: ChannelErrorCode;
  readonly retryAfterMs?: number;

  constructor(
    message: string,
    options: {
      outcome: ChannelErrorOutcome;
      code: ChannelErrorCode;
      retryAfterMs?: number;
    },
  ) {
    super(message);
    this.name = "ChannelDeliveryError";
    this.outcome = options.outcome;
    this.code = options.code;
    this.retryAfterMs = options.retryAfterMs;
  }
}

export interface SendTextChunkOptions {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
  text: string;
  chunkIndex: number;
  totalChunks: number;
  replyToMessageId?: string | null;
  newThread?: boolean;
  threadScopeKey?: string | null;
  /**
   * Stable delivery id from DeliveryService. Discord standalone thread recovery
   * is bound to this id, the parent channel, and the current bot.
   */
  deliveryKey?: string | null;
  /**
   * Thread address already stored for this delivery. When set, Discord posts
   * into that thread and does not create another one.
   */
  confirmedThreadAddress?: string | null;
  /**
   * Called after Discord confirms a new thread and before the message POST.
   * The send does not continue unless this resolves.
   */
  onConfirmedThread?: (confirmed: { address: string }) => Promise<void>;
  weixinContextToken?: string | null;
  inboundMessageId?: string | null;
}

export interface SendTextChunkResult {
  messageId: string;
  resolvedAddress?: string;
}

export interface SendFileOptions {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
  path: string;
  filename: string;
  content: Buffer;
  mimeType: string;
  threadScopeKey?: string | null;
  weixinContextToken?: string | null;
  inboundMessageId?: string | null;
}

export interface SendFileResult {
  messageId: string;
  status: "sent" | "linked";
  reason?: string | null;
}

/** One platform message that carries both text and a file. Used only for `text_and_file`. */
export interface SendTextWithFileOptions extends SendFileOptions {
  text: string;
  chunkIndex: number;
  totalChunks: number;
}

export interface SendCardImageInput {
  placeholder: string;
  content: Buffer;
  mimeType: string;
  filename: string;
}

export interface SendCardOptions {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
  card: Record<string, unknown>;
  images?: SendCardImageInput[];
  replyToMessageId?: string | null;
}

export interface SendCardResult {
  messageId: string;
  resolvedAddress?: string;
}

export interface DownloadedAttachment {
  content: Buffer;
  filename: string;
  mimeType: string;
}

export interface AvailableChatsResult {
  chats: AvailableChatItem[];
  complete: boolean;
  error: string | null;
}

export interface ListAvailableChatsOptions {
  userId: string;
  platformUserId: string;
  /** Omitted tier fails closed: guild and channel names are not returned. */
  userTier?: string | null;
  /** Addresses the caller may see. Admins ignore this set. */
  viewableAddresses?: ReadonlySet<string> | readonly string[] | null;
}

/**
 * Membership for either a set or a readonly list. `instanceof Set` does not
 * exclude `ReadonlySet`, so the check walks the values instead of calling
 * `includes` on the union.
 */
function viewableListHas(viewable: Iterable<string>, address: string): boolean {
  for (const item of viewable) {
    if (item === address) return true;
  }
  return false;
}

/**
 * Mirrors `_user_can_manage_or_view_group_chat`: admins see every address,
 * external callers see none, and other callers see only an address in the
 * viewable set. A missing tier or set hides the address.
 */
export function canViewPlatformChat(options: {
  userTier?: string | null;
  address: string;
  viewableAddresses?: ReadonlySet<string> | readonly string[] | null;
}): boolean {
  if (options.userTier === "admin") return true;
  if (options.userTier !== "internal") return false;
  const viewable = options.viewableAddresses;
  if (!viewable) return false;
  return viewableListHas(viewable, options.address);
}

export interface CheckTargetLiveResult {
  ok: boolean;
  address: string | null;
  name?: string | null;
  code?: ChannelErrorCode | null;
  message: string | null;
}

export interface WeixinQrStartResult {
  qrcode_url?: string;
  qrcode_img_content?: string;
  session_key: string;
  status?: string;
  expires_at?: string;
  message?: string;
}

export interface WeixinQrWaitResult {
  session_key?: string;
  status:
    | "wait"
    | "scaned"
    | "confirmed"
    | "expired"
    | "verify_code"
    | "need_verify"
    | "need_verifycode"
    | "error";
  connected?: boolean;
  ilink_bot_id?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
  bot_generation?: number | null;
  expires_at?: string | null;
  bot_token?: string | null;
  base_url?: string | null;
  message?: string | null;
}

/**
 * Unified Channel Adapter contract for Phase 2 core & Phase 3 protocol implementations
 * (`feishu`, `discord`, `weixin` — §4.2 ~ §4.4).
 */
export interface ChannelAdapter {
  readonly platform: ChannelPlatform;

  /** Maximum characters per text message chunk on this platform. */
  readonly maxChunkChars: number;

  /** Whether credentials/config for this channel are present. */
  isConfigured(): boolean;

  /** Whether the channel runtime/connection is currently healthy. */
  isConnected(): boolean;

  /**
   * Optional outbound-send readiness check separating credential readiness
   * from an inbound WebSocket listener's health in outgoing-notification mode.
   */
  isReadyForOutbound?(): boolean;

  /** Start listening to the platform (no-op for unconfigured or test adapters). */
  start(): Promise<void>;

  /** Gracefully stop the platform listener. */
  stop(): Promise<void>;

  /** Register the gateway's inbound event handler. */
  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void;

  /** Register the gateway's native interaction handler (e.g. Discord INTERACTION_CREATE). */
  onNativeInteraction?(handler: (event: NativeInteractionEvent) => Promise<void>): void;

  /** Split markdown/text into platform-sized chunks. */
  splitTextChunks(text: string): string[];

  /**
   * Split text for a Feishu post send. Adapters that omit this cannot
   * deliver ops-alert post text.
   */
  splitPlainTextChunks?(text: string): string[];

  /**
   * Split a literal platform text message. Feishu uses a character cap.
   * Adapters that omit this cannot deliver an automation-report `plain` item.
   */
  splitTextMessageChunks?(text: string): string[];

  /** Send a single text chunk to the platform. */
  sendTextChunk(options: SendTextChunkOptions): Promise<SendTextChunkResult>;

  /**
   * Send a Feishu post without the table/code-block card upgrade.
   * Ops alerts use this so a `|` in the fixed sentence stays a post.
   */
  sendPlainText?(options: {
    targetAddress: string;
    text: string;
    recipientPlatformUserId?: string | null;
  }): Promise<{ messageId: string }>;

  /**
   * Send `msg_type: "text"` with content `{"text": ...}`.
   * Automation report `plain` items use this and may pass a DM recipient.
   */
  sendTextMessage?(options: {
    targetAddress: string;
    text: string;
    recipientPlatformUserId?: string | null;
  }): Promise<{ messageId: string }>;

  /** Send a single file attachment to the platform. */
  sendFile(options: SendFileOptions): Promise<SendFileResult>;

  /**
   * Optional: one platform request that includes both `text` and the file.
   * Data-task `text_and_file` uses this when present. Adapters that omit it
   * keep the separate text-then-file sends.
   */
  sendTextWithFile?(options: SendTextWithFileOptions): Promise<SendFileResult>;

  /** Optional structured card send (e.g. Feishu Card Schema 2.0 with uploaded images). */
  sendCard?(options: SendCardOptions): Promise<SendCardResult>;

  /** Download an inbound attachment from the platform. */
  downloadAttachment(
    descriptor: InboundAttachmentDescriptor,
    event: InboundChannelEvent,
  ): Promise<DownloadedAttachment>;

  /**
   * List chats/groups available to the user on this platform
   * (`GET /agent/settings/available`, `GET /agent/automation-targets`).
   */
  listAvailableChats(options: ListAvailableChatsOptions): Promise<AvailableChatsResult>;

  /**
   * Optional live platform verification of a target address
   * (e.g. verifying the bot is still a member of a group chat).
   */
  checkTargetLive?(options: {
    userId: string;
    platformUserId?: string | null;
    address: string;
    purpose: string;
  }): Promise<CheckTargetLiveResult>;

  /** Optional Weixin iLink QR login methods (§1.5 #4, §4.4). */
  setBackendClient?(client: BackendClient): void;
  startQrLogin?(options: { userId: string }): Promise<WeixinQrStartResult>;
  waitQrLogin?(options: {
    userId: string;
    sessionKey: string;
  }): Promise<WeixinQrWaitResult>;
  submitVerifyCode?(options: {
    userId: string;
    sessionKey: string;
    code: string;
  }): Promise<WeixinQrWaitResult>;
}

/**
 * Default Markdown-aware chunk splitter respecting `maxChunkChars`.
 */
export function splitTextByLimit(text: string, maxChunkChars: number): string[] {
  if (!text) return [];
  if (text.length <= maxChunkChars) return [text];

  const chunks: string[] = [];
  let remaining = text;
  while (remaining.length > maxChunkChars) {
    let splitIdx = remaining.lastIndexOf("\n", maxChunkChars);
    if (splitIdx <= 0 || splitIdx < Math.floor(maxChunkChars * 0.4)) {
      splitIdx = remaining.lastIndexOf(" ", maxChunkChars);
    }
    if (splitIdx <= 0 || splitIdx < Math.floor(maxChunkChars * 0.4)) {
      splitIdx = maxChunkChars;
      // A hard cut must not split a surrogate pair (emoji etc.).
      const before = remaining.charCodeAt(splitIdx - 1);
      if (before >= 0xd800 && before <= 0xdbff && splitIdx > 1) {
        splitIdx -= 1;
      }
    }
    chunks.push(remaining.slice(0, splitIdx));
    remaining = remaining.slice(splitIdx).replace(/^\n/, "");
  }
  if (remaining.length > 0) {
    chunks.push(remaining);
  }
  return chunks;
}
