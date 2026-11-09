import type {
  AvailableChatItem,
  ChannelPlatform,
  InboundAttachmentDescriptor,
  InboundChannelEvent,
} from "../types.js";
import {
  type AvailableChatsResult,
  type ChannelAdapter,
  type CheckTargetLiveResult,
  type DownloadedAttachment,
  type SendFileOptions,
  type SendFileResult,
  type SendTextChunkOptions,
  type SendTextChunkResult,
  splitTextByLimit,
  type WeixinQrStartResult,
  type WeixinQrWaitResult,
} from "./adapter.js";

export interface FakeSentTextRecord extends SendTextChunkOptions {
  messageId: string;
}

export interface FakeSentFileRecord extends SendFileOptions {
  messageId: string;
  status: "sent" | "linked";
}

/**
 * Deterministic in-memory `ChannelAdapter` for Phase 2 end-to-end testing.
 * Supports scripted chunk failures, attachment downloads, live group chat listings,
 * and Weixin QR login flows.
 */
export class FakeChannelAdapter implements ChannelAdapter {
  readonly platform: ChannelPlatform;
  readonly maxChunkChars: number;

  configured = true;
  connected = true;
  started = false;

  readonly sentTexts: FakeSentTextRecord[] = [];
  readonly sentFiles: FakeSentFileRecord[] = [];

  private inboundHandler: ((event: InboundChannelEvent) => Promise<void>) | null = null;
  private msgCounter = 0;

  private readonly attachments = new Map<string, DownloadedAttachment>();
  private availableChats: AvailableChatItem[] = [];
  private availableComplete = true;
  private availableError: string | null = null;

  private sendTextBehavior:
    | ((options: SendTextChunkOptions) => Promise<SendTextChunkResult> | SendTextChunkResult)
    | null = null;

  private sendFileBehavior:
    | ((options: SendFileOptions) => Promise<SendFileResult> | SendFileResult)
    | null = null;

  private checkTargetBehavior:
    | ((options: {
        userId: string;
        platformUserId?: string | null;
        address: string;
        purpose: string;
      }) => Promise<CheckTargetLiveResult> | CheckTargetLiveResult)
    | null = null;

  private qrStartBehavior:
    | ((options: { userId: string }) => Promise<WeixinQrStartResult>)
    | null = null;

  private qrWaitBehavior:
    | ((options: { userId: string; sessionKey: string }) => Promise<WeixinQrWaitResult>)
    | null = null;

  private qrVerifyBehavior:
    | ((options: {
        userId: string;
        sessionKey: string;
        code: string;
      }) => Promise<WeixinQrWaitResult>)
    | null = null;

  constructor(platform: ChannelPlatform, maxChunkChars = 2000) {
    this.platform = platform;
    this.maxChunkChars = maxChunkChars;
  }

  isConfigured(): boolean {
    return this.configured;
  }

  isConnected(): boolean {
    return this.connected;
  }

  async start(): Promise<void> {
    this.started = true;
  }

  async stop(): Promise<void> {
    this.started = false;
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.inboundHandler = handler;
  }

  /** Emit a simulated inbound message into the gateway orchestrator. */
  async emitInbound(event: InboundChannelEvent): Promise<void> {
    if (!this.inboundHandler) {
      throw new Error(`No inbound handler registered on FakeChannelAdapter(${this.platform})`);
    }
    await this.inboundHandler(event);
  }

  setAttachment(id: string, attachment: DownloadedAttachment): void {
    this.attachments.set(id, attachment);
  }

  setAvailableChats(
    chats: AvailableChatItem[],
    options?: { complete?: boolean; error?: string | null },
  ): void {
    this.availableChats = chats;
    this.availableComplete = options?.complete ?? true;
    this.availableError = options?.error ?? null;
  }

  setSendTextBehavior(
    fn:
      | ((options: SendTextChunkOptions) => Promise<SendTextChunkResult> | SendTextChunkResult)
      | null,
  ): void {
    this.sendTextBehavior = fn;
  }

  setSendFileBehavior(
    fn: ((options: SendFileOptions) => Promise<SendFileResult> | SendFileResult) | null,
  ): void {
    this.sendFileBehavior = fn;
  }

  setCheckTargetBehavior(
    fn:
      | ((options: {
          userId: string;
          platformUserId?: string | null;
          address: string;
          purpose: string;
        }) => Promise<CheckTargetLiveResult> | CheckTargetLiveResult)
      | null,
  ): void {
    this.checkTargetBehavior = fn;
  }

  setQrHandlers(handlers: {
    start?: (options: { userId: string }) => Promise<WeixinQrStartResult>;
    wait?: (options: { userId: string; sessionKey: string }) => Promise<WeixinQrWaitResult>;
    verify?: (options: {
      userId: string;
      sessionKey: string;
      code: string;
    }) => Promise<WeixinQrWaitResult>;
  }): void {
    this.qrStartBehavior = handlers.start ?? null;
    this.qrWaitBehavior = handlers.wait ?? null;
    this.qrVerifyBehavior = handlers.verify ?? null;
  }

  splitTextChunks(text: string): string[] {
    return splitTextByLimit(text, this.maxChunkChars);
  }

  async sendTextChunk(options: SendTextChunkOptions): Promise<SendTextChunkResult> {
    if (this.sendTextBehavior) {
      const res = await this.sendTextBehavior(options);
      this.sentTexts.push({ ...options, messageId: res.messageId });
      return res;
    }
    this.msgCounter += 1;
    const messageId = `${this.platform}_msg_${this.msgCounter}`;
    this.sentTexts.push({ ...options, messageId });
    return { messageId, resolvedAddress: options.targetAddress };
  }

  async sendFile(options: SendFileOptions): Promise<SendFileResult> {
    if (this.sendFileBehavior) {
      const res = await this.sendFileBehavior(options);
      this.sentFiles.push({
        ...options,
        messageId: res.messageId,
        status: res.status,
      });
      return res;
    }
    this.msgCounter += 1;
    const messageId = `${this.platform}_file_${this.msgCounter}`;
    this.sentFiles.push({ ...options, messageId, status: "sent" });
    return { messageId, status: "sent", reason: null };
  }

  async downloadAttachment(
    descriptor: InboundAttachmentDescriptor,
  ): Promise<DownloadedAttachment> {
    const found = this.attachments.get(descriptor.id);
    if (!found) {
      throw new Error(`Attachment not found in FakeChannelAdapter: ${descriptor.id}`);
    }
    return found;
  }

  async listAvailableChats(): Promise<AvailableChatsResult> {
    return {
      chats: [...this.availableChats],
      complete: this.availableComplete,
      error: this.availableError,
    };
  }

  async checkTargetLive(options: {
    userId: string;
    platformUserId?: string | null;
    address: string;
    purpose: string;
  }): Promise<CheckTargetLiveResult> {
    if (this.checkTargetBehavior) {
      return await this.checkTargetBehavior(options);
    }
    return {
      ok: true,
      address: options.address,
      message: null,
    };
  }

  async startQrLogin(options: { userId: string }): Promise<WeixinQrStartResult> {
    if (this.qrStartBehavior) {
      return await this.qrStartBehavior(options);
    }
    return {
      session_key: "fake_qr_session",
      qrcode_url: "https://ilinkai.weixin.qq.com/qr/fake",
      status: "wait",
    };
  }

  async waitQrLogin(options: {
    userId: string;
    sessionKey: string;
  }): Promise<WeixinQrWaitResult> {
    if (this.qrWaitBehavior) {
      return await this.qrWaitBehavior(options);
    }
    return { status: "wait" };
  }

  async submitVerifyCode(options: {
    userId: string;
    sessionKey: string;
    code: string;
  }): Promise<WeixinQrWaitResult> {
    if (this.qrVerifyBehavior) {
      return await this.qrVerifyBehavior(options);
    }
    return { status: "confirmed" };
  }
}
