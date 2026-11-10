import type { BackendClient } from "../backend-client.js";
import type {
  AvailableChatItem,
  ChannelPlatform,
  InboundAttachmentDescriptor,
  InboundChannelEvent,
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

export interface CheckTargetLiveResult {
  ok: boolean;
  address: string | null;
  name?: string | null;
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

  /** Start listening to the platform (no-op for unconfigured or test adapters). */
  start(): Promise<void>;

  /** Gracefully stop the platform listener. */
  stop(): Promise<void>;

  /** Register the gateway's inbound event handler. */
  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void;

  /** Split markdown/text into platform-sized chunks. */
  splitTextChunks(text: string): string[];

  /** Send a single text chunk to the platform. */
  sendTextChunk(options: SendTextChunkOptions): Promise<SendTextChunkResult>;

  /** Send a single file attachment to the platform. */
  sendFile(options: SendFileOptions): Promise<SendFileResult>;

  /** Download an inbound attachment from the platform. */
  downloadAttachment(
    descriptor: InboundAttachmentDescriptor,
    event: InboundChannelEvent,
  ): Promise<DownloadedAttachment>;

  /**
   * List chats/groups available to the user on this platform
   * (`GET /agent/settings/available`, `GET /agent/automation-targets`).
   */
  listAvailableChats(options: {
    userId: string;
    platformUserId: string;
  }): Promise<AvailableChatsResult>;

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
    }
    chunks.push(remaining.slice(0, splitIdx));
    remaining = remaining.slice(splitIdx).replace(/^\n/, "");
  }
  if (remaining.length > 0) {
    chunks.push(remaining);
  }
  return chunks;
}
